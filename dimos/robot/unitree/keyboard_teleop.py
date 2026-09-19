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

import json
import os
import subprocess
import sys
import threading
from typing import Any

# Must be set before `import pygame`: otherwise pygame prints a "Hello from
# the pygame community" banner to stdout on import. On macOS/WSLg the
# teleop window runs as a child process whose stdout is a JSON event
# stream read by the parent (see _USE_SUBPROCESS_WINDOW below) -- that
# banner would land in the stream as a non-JSON line.
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame

from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT

# Gate event codes published on KeyboardTeleop.operator_command for tools that need
# operator-confirmation per step. Defined in a dependency-free module so offline
# consumers (e.g. the benchmark scorer) don't pull pygame just to read them;
# re-exported here for back-compat with `from keyboard_teleop import GATE_*`.
from dimos.control.benchmarking.gate import GATE_ADVANCE, GATE_QUIT, GATE_SKIP
from dimos.core.core import rpc
from dimos.core.module import Module
from dimos.core.stream import Out
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.std_msgs.Float32 import Float32
from dimos.msgs.std_msgs.Int8 import Int8
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

# Force X11 driver on Linux to avoid OpenGL threading issues. macOS has no X11
# driver (SDL uses cocoa); forcing x11 there makes pygame.display fail outright.
if sys.platform.startswith("linux"):
    os.environ["SDL_VIDEODRIVER"] = "x11"

# macOS requires Cocoa windows to be created on the process's real main
# thread (SDL2's Cocoa backend calls NSApplication.setMainMenu, which
# raises NSInternalInconsistencyException off-thread). Under WSLg, a
# pygame window created on a dimOS worker thread likewise comes up blank
# (WSLg shows a placeholder instead of the rendered frame). Running the
# window in a background thread of the worker process (as done below for
# other platforms) therefore fails on macOS and under WSLg, so there we
# run it in a dedicated child process instead -- that process's own main
# thread is free for pygame, mirroring how mujoco_connection.py uses
# `mjpython` for the same reason.
# The worker process dimOS runs this module in is itself already a
# daemonic multiprocessing process, and Python forbids daemonic processes
# from having multiprocessing children -- so the window runs as a plain
# `subprocess.Popen` (this module re-invoked with `-m`) instead of a
# `multiprocessing.Process`, talking back over stdout (JSON lines) and
# stdin (a "STOP\n" line to ask it to exit).


def _is_wsl() -> bool:
    """Return True when running inside Windows Subsystem for Linux.

    Checks `WSL_DISTRO_NAME` first, then falls back to `/proc/version`
    and the kernel release string (both contain "microsoft"/"wsl" under
    WSL1/WSL2). Any check failure means "not WSL" so ordinary Linux is
    unaffected.
    """
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="ignore") as f:
            contents = f.read().lower()
            if "microsoft" in contents or "wsl" in contents:
                return True
    except OSError:
        pass
    try:
        import platform

        release = platform.release().lower()
        if "microsoft" in release or "wsl" in release:
            return True
    except Exception:
        pass
    return False


_USE_SUBPROCESS_WINDOW = sys.platform == "darwin" or _is_wsl()
_TELEOP_WINDOW_ARGV_FLAG = "--teleop-window-worker"

DEFAULT_LINEAR_SPEED: float = 0.5  # m/s
DEFAULT_ANGULAR_SPEED: float = 0.8  # rad/s
DEFAULT_BOOST_MULTIPLIER: float = 2.0
DEFAULT_SLOW_MULTIPLIER: float = 0.5

_WINDOW_WIDTH = 500
_WINDOW_HEIGHT = 400
_FONT_SIZE = 24
_CONTROL_RATE_HZ = 50
_BACKGROUND_COLOR = (30, 30, 30)
_HELP_TEXT_COLOR = (150, 150, 150)
_INDICATOR_RADIUS = 15


def _run_teleop_window(
    window_title: str,
    linear_speed: float,
    angular_speed: float,
    boost_multiplier: float,
    slow_multiplier: float,
    disable_movement: bool,
) -> None:
    """Standalone pygame window + control loop, run as a child process on
    macOS and under WSLg.

    Mirrors KeyboardTeleop._pygame_loop / _update_display, but has no access
    to `self` (it runs in a separate interpreter, invoked via `-m`) -- it
    writes computed Twist components and events as JSON lines on stdout
    instead of publishing directly, and watches stdin for a "STOP" line
    instead of a shared threading.Event.
    """
    keys_held: set[int] = set()
    stop_requested = threading.Event()

    def _watch_stdin() -> None:
        for line in sys.stdin:
            if line.strip() == "STOP":
                stop_requested.set()
                return
        # Parent died / closed stdin without an explicit STOP.
        stop_requested.set()

    threading.Thread(target=_watch_stdin, daemon=True).start()

    def _emit(kind: str, payload: Any) -> None:
        print(json.dumps([kind, payload]), flush=True)

    pygame.init()
    screen = pygame.display.set_mode((_WINDOW_WIDTH, _WINDOW_HEIGHT), pygame.SWSURFACE)
    pygame.display.set_caption(window_title)
    clock = pygame.time.Clock()
    font = pygame.font.Font(None, _FONT_SIZE)

    while not stop_requested.is_set():
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                stop_requested.set()
            elif event.type == pygame.KEYDOWN:
                keys_held.add(event.key)

                if event.key == pygame.K_SPACE:
                    keys_held.clear()
                    _emit("estop", None)
                elif event.key == pygame.K_ESCAPE:
                    stop_requested.set()
                elif event.key == pygame.K_RETURN:
                    _emit("operator", GATE_ADVANCE)
                elif event.key == pygame.K_k:
                    _emit("operator", GATE_SKIP)
                elif event.key == pygame.K_BACKSPACE:
                    _emit("operator", GATE_QUIT)
                elif pygame.K_0 <= event.key <= pygame.K_9:
                    _emit("e_max", (event.key - pygame.K_0) * 0.1)

            elif event.type == pygame.KEYUP:
                keys_held.discard(event.key)

        lx, ly, az = 0.0, 0.0, 0.0

        if not disable_movement:
            if pygame.K_w in keys_held:
                lx = linear_speed
            if pygame.K_s in keys_held:
                lx = -linear_speed

            if pygame.K_q in keys_held:
                ly = linear_speed
            if pygame.K_e in keys_held:
                ly = -linear_speed

            if pygame.K_a in keys_held:
                az = angular_speed
            if pygame.K_d in keys_held:
                az = -angular_speed

        speed_multiplier = 1.0
        if pygame.K_LSHIFT in keys_held or pygame.K_RSHIFT in keys_held:
            speed_multiplier = boost_multiplier
        elif pygame.K_LCTRL in keys_held or pygame.K_RCTRL in keys_held:
            speed_multiplier = slow_multiplier

        lx *= speed_multiplier
        ly *= speed_multiplier
        az *= speed_multiplier

        active = lx != 0 or ly != 0 or az != 0
        _emit("cmd_vel", [lx, ly, az, active])

        _render_teleop_window(
            screen,
            font,
            keys_held,
            window_title,
            boost_multiplier,
            slow_multiplier,
            disable_movement,
            lx,
            ly,
            az,
        )

        clock.tick(_CONTROL_RATE_HZ)

    pygame.quit()
    _emit("quit", None)


def _render_teleop_window(
    screen: pygame.Surface,
    font: pygame.font.Font,
    keys_held: set[int],
    window_title: str,
    boost_multiplier: float,
    slow_multiplier: float,
    disable_movement: bool,
    lx: float,
    ly: float,
    az: float,
) -> None:
    screen.fill(_BACKGROUND_COLOR)

    y_pos = 20

    speed_mult_text = ""
    if pygame.K_LSHIFT in keys_held or pygame.K_RSHIFT in keys_held:
        speed_mult_text = f" [BOOST {boost_multiplier:g}x]"
    elif pygame.K_LCTRL in keys_held or pygame.K_RCTRL in keys_held:
        speed_mult_text = f" [SLOW {slow_multiplier:g}x]"

    texts = [
        window_title + speed_mult_text,
        "",
        f"Linear X (Forward/Back): {lx:+.2f} m/s",
        f"Linear Y (Strafe L/R): {ly:+.2f} m/s",
        f"Angular Z (Turn L/R): {az:+.2f} rad/s",
        "",
        "Keys: " + ", ".join([pygame.key.name(k).upper() for k in keys_held if k < 256]),
    ]

    for i, text in enumerate(texts):
        if text:
            color = (0, 255, 255) if i == 0 else (255, 255, 255)
            surf = font.render(text, True, color)
            screen.blit(surf, (20, y_pos))
        y_pos += 30

    if lx != 0 or ly != 0 or az != 0:
        pygame.draw.circle(screen, (255, 0, 0), (450, 30), _INDICATOR_RADIUS)
    else:
        pygame.draw.circle(screen, (0, 255, 0), (450, 30), _INDICATOR_RADIUS)

    y_pos = 280
    if disable_movement:
        help_texts = [
            "Movement disabled (e_max slider mode)",
            "Space: E-Stop | ESC: Quit",
            "Enter: Advance | K: Skip | Backspace: Quit (tools)",
            "0-9: e_max corridor (0.0-0.9 m, for RG)",
        ]
    else:
        help_texts = [
            "WS: Move | AD: Turn | QE: Strafe",
            "Shift: Boost | Ctrl: Slow",
            "Space: E-Stop | ESC: Quit",
            "Enter: Advance | K: Skip | Backspace: Quit (tools)",
            "0-9: e_max corridor (0.0-0.9 m, for RG)",
        ]
    for text in help_texts:
        surf = font.render(text, True, _HELP_TEXT_COLOR)
        screen.blit(surf, (20, y_pos))
        y_pos += 25

    pygame.display.flip()


class KeyboardTeleop(Module):
    """Pygame-based keyboard control. Outputs Twist on cmd_vel.

    Also emits operator gate events on ``operator_command: Out[Int8]`` for
    tools that need to pause for operator confirmation between steps (e.g.
    the one-terminal Go2 benchmark blueprint). Three keys: ``ENTER`` ->
    advance, ``K`` -> skip, ``Backspace`` -> quit. Existing blueprints that
    don't wire the ``operator_command`` port are unaffected -- the events
    publish into a stream nobody listens to.
    """

    # pygame.display supports one window per process; multi-robot blueprints
    # run one teleop per robot, so each instance needs its own worker.
    dedicated_worker = True

    cmd_vel: Out[Twist]
    operator_command: Out[Int8]
    # Reference-governor corridor half-width (m). Number keys 0-9 map
    # to 0.0-0.9 m so an operator can dial precision live during a run.
    e_max: Out[Float32]

    _stop_event: threading.Event
    _keys_held: set[int] | None = None
    _thread: threading.Thread | None = None
    _screen: pygame.Surface | None = None
    _clock: pygame.time.Clock | None = None
    _font: pygame.font.Font | None = None
    # Only used where _USE_SUBPROCESS_WINDOW is set (macOS and WSLg):
    # the pygame window runs in its own process instead of a background
    # thread of this one.
    _window_process: subprocess.Popen[str] | None = None

    def __init__(
        self,
        linear_speed: float = DEFAULT_LINEAR_SPEED,
        angular_speed: float = DEFAULT_ANGULAR_SPEED,
        boost_multiplier: float = DEFAULT_BOOST_MULTIPLIER,
        slow_multiplier: float = DEFAULT_SLOW_MULTIPLIER,
        publish_only_when_active: bool = True,
        disable_movement: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._stop_event = threading.Event()
        self.linear_speed = linear_speed
        self.angular_speed = angular_speed
        self.boost_multiplier = boost_multiplier
        self.slow_multiplier = slow_multiplier
        # When True, only publish while a movement key is held; on
        # release publish a single zero Twist (stop) then go silent.
        # Lets the teleop coexist with another /cmd_vel publisher
        # (e.g. the SI / benchmark tools) instead of flooding zeros.
        self.publish_only_when_active = publish_only_when_active
        # When True, WASD/QE movement keys are no-ops and the window is a
        # pure 0-9 e_max slider. Used by blueprints that drive cmd_vel
        # from another source (e.g. nav-stack-driven precision controller)
        # but still want the operator's live e_max input.
        self.disable_movement = disable_movement
        self._was_active = False
        # Namespaced instances (e.g. "robot0/keyboardteleop") get their own
        # window title so multi-robot teleop windows are distinguishable.
        self._window_title = self.config.instance_name or "Keyboard Teleop"

    @rpc
    def start(self) -> None:
        super().start()

        self._keys_held = set()
        self._stop_event.clear()

        if _USE_SUBPROCESS_WINDOW:
            self._window_process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "dimos.robot.unitree.keyboard_teleop",
                    _TELEOP_WINDOW_ARGV_FLAG,
                    self._window_title,
                    str(self.linear_speed),
                    str(self.angular_speed),
                    str(self.boost_multiplier),
                    str(self.slow_multiplier),
                    "1" if self.disable_movement else "0",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self._thread = threading.Thread(target=self._drain_window_process, daemon=True)
        else:
            self._thread = threading.Thread(target=self._pygame_loop, daemon=True)
        self._thread.start()

    @rpc
    def stop(self) -> None:
        stop_twist = Twist()
        stop_twist.linear = Vector3(0, 0, 0)
        stop_twist.angular = Vector3(0, 0, 0)
        self.cmd_vel.publish(stop_twist)

        self._stop_event.set()

        if _USE_SUBPROCESS_WINDOW and self._window_process is not None:
            try:
                if self._window_process.stdin is not None:
                    self._window_process.stdin.write("STOP\n")
                    self._window_process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass  # Child already exited.

        if self._thread is None:
            raise RuntimeError("Cannot stop: thread was never started")
        self._thread.join(DEFAULT_THREAD_JOIN_TIMEOUT)

        if _USE_SUBPROCESS_WINDOW and self._window_process is not None:
            try:
                self._window_process.wait(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
            except subprocess.TimeoutExpired:
                self._window_process.terminate()

        super().stop()

    def _drain_window_process(self) -> None:
        """Consume JSON events from the subprocess window (macOS/WSLg) and publish them.

        Mirrors the publish logic in `_pygame_loop`, just fed from
        `_run_teleop_window`'s stdout instead of computing values inline.
        """
        if self._window_process is None or self._window_process.stdout is None:
            raise RuntimeError("_window_process not initialized")

        for line in self._window_process.stdout:
            if self._stop_event.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                kind, payload = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                # Stray non-JSON output on the child's stdout (a library
                # print, a warning); not an event for us.
                logger.debug("Ignoring non-JSON line from teleop window: %r", line)
                continue

            if kind == "cmd_vel":
                lx, ly, az, active = payload
                twist = Twist()
                twist.linear = Vector3(lx, ly, 0)
                twist.angular = Vector3(0, 0, az)
                if self.publish_only_when_active:
                    if active or self._was_active:
                        self.cmd_vel.publish(twist)
                    self._was_active = active
                else:
                    self.cmd_vel.publish(twist)
            elif kind == "estop":
                stop_twist = Twist()
                stop_twist.linear = Vector3(0, 0, 0)
                stop_twist.angular = Vector3(0, 0, 0)
                self.cmd_vel.publish(stop_twist)
                logger.warning("EMERGENCY STOP!")
            elif kind == "operator":
                self.operator_command.publish(Int8(payload))
            elif kind == "e_max":
                self.e_max.publish(Float32(data=payload))
            elif kind == "quit":
                break

    def _pygame_loop(self) -> None:
        if self._keys_held is None:
            raise RuntimeError("_keys_held not initialized")

        pygame.init()
        self._screen = pygame.display.set_mode((_WINDOW_WIDTH, _WINDOW_HEIGHT), pygame.SWSURFACE)
        pygame.display.set_caption(self._window_title)
        self._clock = pygame.time.Clock()
        self._font = pygame.font.Font(None, _FONT_SIZE)

        while not self._stop_event.is_set():
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self._stop_event.set()
                elif event.type == pygame.KEYDOWN:
                    self._keys_held.add(event.key)

                    if event.key == pygame.K_SPACE:
                        # Emergency stop - clear all keys and send zero twist
                        self._keys_held.clear()
                        stop_twist = Twist()
                        stop_twist.linear = Vector3(0, 0, 0)
                        stop_twist.angular = Vector3(0, 0, 0)
                        self.cmd_vel.publish(stop_twist)
                        logger.warning("EMERGENCY STOP!")
                    elif event.key == pygame.K_ESCAPE:
                        # ESC quits
                        self._stop_event.set()
                    elif event.key == pygame.K_RETURN:
                        self.operator_command.publish(Int8(GATE_ADVANCE))
                    elif event.key == pygame.K_k:
                        self.operator_command.publish(Int8(GATE_SKIP))
                    elif event.key == pygame.K_BACKSPACE:
                        self.operator_command.publish(Int8(GATE_QUIT))
                    elif pygame.K_0 <= event.key <= pygame.K_9:
                        # 0 -> 0.0 m, 1 -> 0.1 m, ..., 9 -> 0.9 m corridor half-width.
                        self.e_max.publish(Float32(data=(event.key - pygame.K_0) * 0.1))

                elif event.type == pygame.KEYUP:
                    self._keys_held.discard(event.key)

            # Generate Twist message from held keys
            twist = Twist()
            twist.linear = Vector3(0, 0, 0)
            twist.angular = Vector3(0, 0, 0)

            # Movement keys (WASD/QE) -- guarded by disable_movement so the
            # window can run as a pure e_max slider (0-9 keys stay live in
            # the KEYDOWN handler above).
            if not self.disable_movement:
                # Forward/backward (W/S)
                if pygame.K_w in self._keys_held:
                    twist.linear.x = self.linear_speed
                if pygame.K_s in self._keys_held:
                    twist.linear.x = -self.linear_speed

                # Strafe left/right (Q/E)
                if pygame.K_q in self._keys_held:
                    twist.linear.y = self.linear_speed
                if pygame.K_e in self._keys_held:
                    twist.linear.y = -self.linear_speed

                # Turning (A/D)
                if pygame.K_a in self._keys_held:
                    twist.angular.z = self.angular_speed
                if pygame.K_d in self._keys_held:
                    twist.angular.z = -self.angular_speed

            # Apply speed modifiers (Shift = boost, Ctrl = slow)
            speed_multiplier = 1.0
            if pygame.K_LSHIFT in self._keys_held or pygame.K_RSHIFT in self._keys_held:
                speed_multiplier = self.boost_multiplier
            elif pygame.K_LCTRL in self._keys_held or pygame.K_RCTRL in self._keys_held:
                speed_multiplier = self.slow_multiplier

            twist.linear.x *= speed_multiplier
            twist.linear.y *= speed_multiplier
            twist.angular.z *= speed_multiplier

            if self.publish_only_when_active:
                active = twist.linear.x != 0 or twist.linear.y != 0 or twist.angular.z != 0
                # Publish while active; publish exactly one zero on the
                # active->idle transition (clean stop); then stay silent
                # so a co-publisher owns /cmd_vel.
                if active or self._was_active:
                    self.cmd_vel.publish(twist)
                self._was_active = active
            else:
                self.cmd_vel.publish(twist)

            self._update_display(twist)

            # Maintain control loop rate
            if self._clock is None:
                raise RuntimeError("_clock not initialized")
            self._clock.tick(_CONTROL_RATE_HZ)

        pygame.quit()

    def _update_display(self, twist: Twist) -> None:
        if self._screen is None or self._font is None or self._keys_held is None:
            raise RuntimeError("Not initialized correctly")

        self._screen.fill(_BACKGROUND_COLOR)

        y_pos = 20

        # Determine active speed multiplier
        speed_mult_text = ""
        if pygame.K_LSHIFT in self._keys_held or pygame.K_RSHIFT in self._keys_held:
            speed_mult_text = f" [BOOST {self.boost_multiplier:g}x]"
        elif pygame.K_LCTRL in self._keys_held or pygame.K_RCTRL in self._keys_held:
            speed_mult_text = f" [SLOW {self.slow_multiplier:g}x]"

        texts = [
            self._window_title + speed_mult_text,
            "",
            f"Linear X (Forward/Back): {twist.linear.x:+.2f} m/s",
            f"Linear Y (Strafe L/R): {twist.linear.y:+.2f} m/s",
            f"Angular Z (Turn L/R): {twist.angular.z:+.2f} rad/s",
            "",
            "Keys: " + ", ".join([pygame.key.name(k).upper() for k in self._keys_held if k < 256]),
        ]

        for i, text in enumerate(texts):
            if text:
                color = (0, 255, 255) if i == 0 else (255, 255, 255)
                surf = self._font.render(text, True, color)
                self._screen.blit(surf, (20, y_pos))
            y_pos += 30

        if twist.linear.x != 0 or twist.linear.y != 0 or twist.angular.z != 0:
            pygame.draw.circle(self._screen, (255, 0, 0), (450, 30), _INDICATOR_RADIUS)
        else:
            pygame.draw.circle(self._screen, (0, 255, 0), (450, 30), _INDICATOR_RADIUS)

        y_pos = 280
        if self.disable_movement:
            help_texts = [
                "Movement disabled (e_max slider mode)",
                "Space: E-Stop | ESC: Quit",
                "Enter: Advance | K: Skip | Backspace: Quit (tools)",
                "0-9: e_max corridor (0.0-0.9 m, for RG)",
            ]
        else:
            help_texts = [
                "WS: Move | AD: Turn | QE: Strafe",
                "Shift: Boost | Ctrl: Slow",
                "Space: E-Stop | ESC: Quit",
                "Enter: Advance | K: Skip | Backspace: Quit (tools)",
                "0-9: e_max corridor (0.0-0.9 m, for RG)",
            ]
        for text in help_texts:
            surf = self._font.render(text, True, _HELP_TEXT_COLOR)
            self._screen.blit(surf, (20, y_pos))
            y_pos += 25

        pygame.display.flip()


def _teleop_window_main(argv: list[str]) -> None:
    if len(argv) != 6:
        raise SystemExit(
            f"usage: -m dimos.robot.unitree.keyboard_teleop {_TELEOP_WINDOW_ARGV_FLAG} "
            "<title> <linear_speed> <angular_speed> <boost_multiplier> "
            "<slow_multiplier> <disable_movement:0|1>"
        )
    window_title, linear_speed, angular_speed, boost_multiplier, slow_multiplier, disable_movement = argv
    _run_teleop_window(
        window_title,
        float(linear_speed),
        float(angular_speed),
        float(boost_multiplier),
        float(slow_multiplier),
        disable_movement == "1",
    )


if __name__ == "__main__":
    # Internal entry point: KeyboardTeleop.start() re-invokes this module
    # with `-m` on macOS/WSLg to give the pygame window its own process
    # (and thus its own real main thread -- see _USE_SUBPROCESS_WINDOW
    # above). Not meant to be run directly otherwise.
    if len(sys.argv) >= 2 and sys.argv[1] == _TELEOP_WINDOW_ARGV_FLAG:
        _teleop_window_main(sys.argv[2:])
    else:
        raise SystemExit(
            "This module is a dimOS Module (KeyboardTeleop); it isn't meant to be "
            "run directly except as the internal teleop-window worker process."
        )
