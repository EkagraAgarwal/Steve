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
import re
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from reactivex.disposable import Disposable
from typesafe_sdk import Choice, TypeSafeClient

from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.core.stream import In, Out
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.tf2_msgs.TFMessage import TFMessage
from dimos.navigation.navigation_spec import NavigationInterfaceSpec
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

DEFAULT_DISTANCE_M: float = 0.5
DEFAULT_TURN_DEGREES: float = 30.0
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.6

_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")

# Spelled-out magnitudes common in spoken/typed commands ("turn ninety degrees",
# "strafe left half a meter"). Checked only when no digit is present.
_WORD_NUMBERS: dict[str, float] = {
    "half": 0.5,
    "a": 1.0,
    "an": 1.0,
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
    "nine": 9.0,
    "ten": 10.0,
    "twenty": 20.0,
    "thirty": 30.0,
    "forty": 40.0,
    "forty-five": 45.0,
    "sixty": 60.0,
    "ninety": 90.0,
    "hundred": 100.0,
    "hundred-eighty": 180.0,
}
_WORD_NUMBER_RE = re.compile(
    r"\b(" + "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True)) + r")\b", re.IGNORECASE
)

# What Jev picks between. Keys double as the dict Choice.criteria expects;
# values are the descriptions it sees for each option.
_ACTIONS: dict[str, str] = {
    "forward": "move straight ahead, in the direction the robot is currently facing",
    "backward": "move straight backward, away from the direction the robot is facing",
    "strafe_left": "move sideways to the robot's left, without turning",
    "strafe_right": "move sideways to the robot's right, without turning",
    "turn_left": "rotate in place to face further left (counter-clockwise), no translation",
    "turn_right": "rotate in place to face further right (clockwise), no translation",
    "stop": "immediately stop and cancel whatever the robot is currently doing",
    "other": (
        "anything that is not a single, immediate directional move or stop — a named "
        "trick/pose, navigating to a place by name, a question, or a multi-step instruction"
    ),
}

# (forward_m, left_m, turn_deg) unit vector per action; scaled by the extracted/default magnitude.
_ACTION_UNIT_DELTA: dict[str, tuple[float, float, float]] = {
    "forward": (1.0, 0.0, 0.0),
    "backward": (-1.0, 0.0, 0.0),
    "strafe_left": (0.0, 1.0, 0.0),
    "strafe_right": (0.0, -1.0, 0.0),
    "turn_left": (0.0, 0.0, 1.0),
    "turn_right": (0.0, 0.0, -1.0),
}


def _goal_pose(current: PoseStamped, forward_m: float, left_m: float, turn_deg: float) -> PoseStamped:
    """Goal `turn_deg`/`forward_m`/`left_m` relative to `current`, in the world frame."""
    euler = current.orientation.to_euler()
    position = current.position + current.orientation.rotate_vector(Vector3(forward_m, left_m, 0))
    yaw = euler.yaw + math.radians(turn_deg)
    orientation = Quaternion.from_euler(Vector3(euler.roll, euler.pitch, yaw))
    return PoseStamped(position=position, orientation=orientation, frame_id="world")


def _extract_magnitude(text: str, is_turn: bool, default_distance_m: float, default_turn_degrees: float) -> float:
    """First digit or spelled-out number in `text`, or a default.

    cm is recognized and converted for distances; spelled-out numbers ("one",
    "half", "ninety") are checked only when no digit is present.
    """
    match = _NUMBER_RE.search(text)
    if match is not None:
        value = float(match.group(1))
        if not is_turn and "cm" in text.lower():
            value /= 100.0
        return value

    word_match = _WORD_NUMBER_RE.search(text)
    if word_match is not None:
        return _WORD_NUMBERS[word_match.group(1).lower()]

    return default_turn_degrees if is_turn else default_distance_m


class JevMovementTeleop(Module):
    """Low-latency directional text control via TypeSafe's Jev.

    Subscribes to the same `/human_input` channel `humancli` publishes to. For
    each message it asks Jev a single fast Choice question instead of routing
    through a general-purpose LLM's tool-calling loop (`McpClient`, used by
    `unitree-go2-agentic`/`unitree-go2-agentic-movement`) — that path's reasoning
    round-trip is the dominant source of latency for a plain "move forward".

    On a confident match it computes the goal directly and calls
    `_navigation.set_goal()` immediately. Anything Jev doesn't recognize as one
    of its eight actions (`other`, or below `confidence_threshold`) is left
    untouched: run this alongside an agentic blueprint's `McpClient` (also
    subscribed to `/human_input`) to fall back to full natural-language
    understanding for anything beyond a simple directional command.
    """

    _navigation: NavigationInterfaceSpec

    tf: In[TFMessage]
    human_input: In[str]
    agent: Out[BaseMessage]

    _client: TypeSafeClient | None = None

    def __init__(
        self,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        default_distance_m: float = DEFAULT_DISTANCE_M,
        default_turn_degrees: float = DEFAULT_TURN_DEGREES,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.confidence_threshold = confidence_threshold
        self.default_distance_m = default_distance_m
        self.default_turn_degrees = default_turn_degrees

    @rpc
    def start(self) -> None:
        super().start()
        # `typesafe_sdk` itself only reads TYPESAFE_API_KEY; JEV_KEY is this project's
        # env var name for the same credential, so check it first.
        api_key = os.environ.get("JEV_KEY") or os.environ.get("TYPESAFE_API_KEY")
        self._client = TypeSafeClient(api_key=api_key)
        self.register_disposable(Disposable(self.human_input.subscribe(self._on_text)))

    @rpc
    def stop(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        super().stop()

    def _on_text(self, text: str) -> None:
        try:
            action, confidence = self._classify(text)
        except Exception:
            logger.exception("Jev classification failed for %r; leaving it for the LLM fallback", text)
            return

        if action == "other" or confidence < self.confidence_threshold:
            return  # not a simple directional command -- let McpClient's LLM handle it

        if action == "stop":
            self._navigation.cancel_goal()
            self.agent.publish(AIMessage(content="Stopped."))
            return

        self.agent.publish(AIMessage(content=self._execute(action, text)))

    def _classify(self, text: str) -> tuple[str, float]:
        assert self._client is not None
        response = self._client.system_one(
            state={"command": text},
            questions={
                "action": Choice(
                    instructions=(
                        "Which of these best describes what the robot should do right now? "
                        "Pick 'other' for anything that isn't a single, immediate directional "
                        "move or stop."
                    ),
                    criteria=_ACTIONS,
                ),
            },
        )
        answer = response.answers["action"]
        return answer.choice, answer.confidence

    def _execute(self, action: str, text: str) -> str:
        forward_unit, left_unit, turn_unit = _ACTION_UNIT_DELTA[action]
        is_turn = turn_unit != 0.0
        magnitude = _extract_magnitude(text, is_turn, self.default_distance_m, self.default_turn_degrees)

        tf = self.tfbuffer.get("world", "base_link")
        if tf is None:
            return "Couldn't get the robot's position -- try again in a moment."

        goal = _goal_pose(
            tf.to_pose(), forward_unit * magnitude, left_unit * magnitude, turn_unit * magnitude
        )
        self._navigation.set_goal(goal)
        unit = "°" if is_turn else "m"
        return f"{action.replace('_', ' ').capitalize()} ({magnitude:g}{unit})."
