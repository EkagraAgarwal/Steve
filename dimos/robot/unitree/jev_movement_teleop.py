#!/usr/bin/env python3
# Copyright 2025-2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math
import os
import queue
import threading
import time
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from reactivex.disposable import Disposable
from typesafe_sdk import TypeSafeClient

from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.core.stream import In, Out
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.tf2_msgs.TFMessage import TFMessage
from dimos.navigation.navigation_spec import NavigationInterfaceSpec
from dimos.utils.logging_config import setup_logger

# Action classification, magnitude validation, and safety caps live only
# in steve_voice.commands (single source of truth); this module maps an
# accepted decision to a pose goal and dispatches it.
from steve_voice.commands import (
    ACTION_UNIT_DELTAS,
    CONFIDENCE_THRESHOLD,
    DEFAULT_DISTANCE_M,
    DEFAULT_TURN_DEGREES,
    STOP,
    TURN_ACTIONS,
    VoiceCommand,
    is_local_stop,
    route_command,
)

logger = setup_logger()

# JEV round-trip budget per request; expiry for a queued request before
# it may still execute. A stale result must never move the robot after a
# newer stop/cancel, so both generation checks below are load-bearing.
_JEV_TIMEOUT_S: float = 8.0
_REQUEST_TTL_S: float = 10.0
_QUEUE_MAXSIZE: int = 4
_WORKER_JOIN_S: float = 2.0


def _goal_pose(current: PoseStamped, forward_m: float, left_m: float, turn_deg: float) -> PoseStamped:
    """Goal `turn_deg`/`forward_m`/`left_m` relative to `current`, in the world frame."""
    euler = current.orientation.to_euler()
    position = current.position + current.orientation.rotate_vector(Vector3(forward_m, left_m, 0))
    yaw = euler.yaw + math.radians(turn_deg)
    orientation = Quaternion.from_euler(Vector3(euler.roll, euler.pitch, yaw))
    return PoseStamped(position=position, orientation=orientation, frame_id="world")


class JevMovementTeleop(Module):
    """Low-latency directional text control via TypeSafe's Jev.

    Pure Jev path: each text message gets exactly one fast ``Choice``
    evaluation through :func:`steve_voice.commands.route_command` -- no
    general-purpose LLM, no LLM fallback. Anything not accepted
    (``other``, below threshold, multi-action, bad units, over-cap
    magnitude, or a JEV failure) is left untouched and never actuates.

    Execution model: :meth:`_on_text` never blocks on the network. It
    either takes the local stop path (below) or enqueues one bounded
    work item; a single daemon worker performs the JEV call and, before
    touching the robot, re-checks the request generation and expiry so a
    stale result cannot execute after a local stop/cancel.

    Stop semantics: a local stop cue (``stop``/``halt``/``freeze``/...)
    bypasses JEV entirely -- it bumps the generation (invalidating any
    in-flight request), calls ``_navigation.cancel_goal()``, and
    acknowledges. ``cancel_goal`` only cancels the active navigation
    goal; it is NOT a verified physical emergency stop, so keep an
    independent local stop available. This module never calls
    ``liedown()`` and never claims e-stop behavior.
    """

    _navigation: NavigationInterfaceSpec

    tf: In[TFMessage]
    human_input: In[str]
    agent: Out[BaseMessage]

    _client: TypeSafeClient | None = None

    def __init__(
        self,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
        default_distance_m: float = DEFAULT_DISTANCE_M,
        default_turn_degrees: float = DEFAULT_TURN_DEGREES,
        request_ttl_s: float = _REQUEST_TTL_S,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.confidence_threshold = confidence_threshold
        # Kept for blueprint/constructor compatibility; magnitude
        # defaults and caps are owned by steve_voice.commands.
        self.default_distance_m = default_distance_m
        self.default_turn_degrees = default_turn_degrees
        self.request_ttl_s = request_ttl_s
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._generation = 0
        self._gen_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._worker_stop = threading.Event()

    @rpc
    def start(self) -> None:
        super().start()
        # `typesafe_sdk` itself only reads TYPESAFE_API_KEY; JEV_KEY is this project's
        # env var name for the same credential, so check it first.
        api_key = os.environ.get("JEV_KEY") or os.environ.get("TYPESAFE_API_KEY")
        self._client = TypeSafeClient(api_key=api_key)
        self._worker_stop.clear()
        self._worker = threading.Thread(
            target=self._worker_loop, name="jev-movement-worker", daemon=True
        )
        self._worker.start()
        self.register_disposable(Disposable(self.human_input.subscribe(self._on_text)))

    @rpc
    def stop(self) -> None:
        with self._gen_lock:
            self._generation += 1
        self._worker_stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._worker is not None:
            self._worker.join(timeout=_WORKER_JOIN_S)
            self._worker = None
        if self._client is not None:
            self._client.close()
            self._client = None
        super().stop()

    def _on_text(self, text: str) -> None:
        """Non-blocking entry: local-stop bypass or one bounded enqueue."""
        if is_local_stop(text if isinstance(text, str) else ""):
            self._local_stop()
            return
        if self._client is None:
            return
        with self._gen_lock:
            generation = self._generation
        item = (generation, time.monotonic() + self.request_ttl_s, text)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:
                self._queue.get_nowait()  # shed oldest; newest intent wins
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                logger.warning("Jev command queue full; dropping %r", text)

    def _local_stop(self) -> None:
        """Immediate stop path: no JEV call, invalidates in-flight work."""
        with self._gen_lock:
            self._generation += 1
        try:
            self._navigation.cancel_goal()
        except Exception:
            logger.exception("cancel_goal failed on local stop")
        try:
            self.agent.publish(AIMessage(content="Stopped."))
        except Exception:
            logger.exception("failed to publish stop acknowledgement")

    def _worker_loop(self) -> None:
        while not self._worker_stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                return
            generation, deadline, text = item
            with self._gen_lock:
                current = self._generation
            if generation != current:
                continue  # superseded by a newer stop/cancel; stale, drop
            if time.monotonic() > deadline:
                logger.warning("Dropping expired JEV request for %r", text)
                continue
            try:
                assert self._client is not None
                decision = route_command(
                    text,
                    self._client,
                    confidence_threshold=self.confidence_threshold,
                    timeout=_JEV_TIMEOUT_S,
                )
            except Exception:
                logger.exception("Jev routing failed for %r; not actuating", text)
                continue
            with self._gen_lock:
                current = self._generation
            if generation != current or time.monotonic() > deadline:
                continue  # stop/cancel or expiry landed mid-call; never execute
            try:
                self._dispatch(decision)
            except Exception:
                logger.exception("Jev dispatch failed for %r; not actuating", text)

    def _dispatch(self, decision: VoiceCommand) -> None:
        if not decision.accepted or decision.action is None:
            return
        if decision.action == STOP:
            # JEV-classified stop for phrasings the local bypass did not
            # catch; same navigation-goal cancellation semantics.
            self._local_stop()
            return
        try:
            forward_unit, left_unit, turn_unit = ACTION_UNIT_DELTAS[decision.action]
        except KeyError:
            logger.warning("Unknown action %r; not actuating", decision.action)
            return
        magnitude = decision.magnitude
        if magnitude is None or not math.isfinite(magnitude):
            return
        tf = self.tfbuffer.get("world", "base_link")
        if tf is None:
            self.agent.publish(
                AIMessage(content="Couldn't get the robot's position -- try again in a moment.")
            )
            return
        goal = _goal_pose(
            tf.to_pose(),
            forward_unit * magnitude,
            left_unit * magnitude,
            turn_unit * magnitude,
        )
        self._navigation.set_goal(goal)
        is_turn = decision.action in TURN_ACTIONS
        unit = "°" if is_turn else "m"
        self.agent.publish(
            AIMessage(content=f"{decision.action.replace('_', ' ').capitalize()} ({magnitude:g}{unit}).")
        )
