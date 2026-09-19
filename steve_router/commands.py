"""Allowlisted command schema and pure teleop motion mapping.

Teleop constants mirror dimos/robot/unitree/keyboard_teleop.py
(forward lx +0.5, strafe left ly +0.5, turn left az +0.8;
boost x2, slow x0.5; stop is exact zero). KeyboardTeleop itself
is not imported or edited here.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

LINEAR_SPEED: float = 0.5
ANGULAR_SPEED: float = 0.8
BOOST_MULTIPLIER: float = 2.0
SLOW_MULTIPLIER: float = 0.5


class Action(str, Enum):
    MOVE_FORWARD = "move_forward"
    MOVE_BACKWARD = "move_backward"
    STRAFE_LEFT = "strafe_left"
    STRAFE_RIGHT = "strafe_right"
    TURN_LEFT = "turn_left"
    TURN_RIGHT = "turn_right"
    STOP = "stop"


class SpeedMode(str, Enum):
    NORMAL = "normal"
    BOOST = "boost"
    SLOW = "slow"


@dataclass(frozen=True)
class RoutedCommand:
    """Result of routing one text input.

    Rejected commands keep action=None (no actuation). An explicit
    "stop" request is an accepted Action.STOP, not a rejection.
    Local e-stop stays outside cloud/JEV entirely.
    """

    action: Optional[Action]
    speed_mode: SpeedMode
    confidence: float
    source_text: str
    accepted: bool
    reason: str


def _speed_multiplier(speed_mode: SpeedMode) -> float:
    if speed_mode == SpeedMode.BOOST:
        return BOOST_MULTIPLIER
    if speed_mode == SpeedMode.SLOW:
        return SLOW_MULTIPLIER
    return 1.0


def to_motion(command: RoutedCommand) -> Optional[Tuple[float, float, float]]:
    """Map a routed command to (lx, ly, az). None means do not actuate."""
    if not command.accepted or command.action is None:
        return None
    mult = _speed_multiplier(command.speed_mode)
    action = command.action
    if action == Action.MOVE_FORWARD:
        return (LINEAR_SPEED * mult, 0.0, 0.0)
    if action == Action.MOVE_BACKWARD:
        return (-LINEAR_SPEED * mult, 0.0, 0.0)
    if action == Action.STRAFE_LEFT:
        return (0.0, LINEAR_SPEED * mult, 0.0)
    if action == Action.STRAFE_RIGHT:
        return (0.0, -LINEAR_SPEED * mult, 0.0)
    if action == Action.TURN_LEFT:
        return (0.0, 0.0, ANGULAR_SPEED * mult)
    if action == Action.TURN_RIGHT:
        return (0.0, 0.0, -ANGULAR_SPEED * mult)
    if action == Action.STOP:
        return (0.0, 0.0, 0.0)
    return None
