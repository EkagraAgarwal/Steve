"""JEV text router: plain text in, allowlisted RoutedCommand out.

Uses the official ``typesafe-sdk`` client (``TypeSafeClient.system_one``)
injected by the caller so tests can substitute a fake. No dimOS or
pygame imports here.
"""

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from steve_router.commands import Action, RoutedCommand, SpeedMode

COMMAND_QUESTION = "command"
SPEED_QUESTION = "speed_mode"
REJECT_LABEL = "reject"

_COMMAND_CRITERIA = {
    Action.MOVE_FORWARD.value: "robot should drive forward",
    Action.MOVE_BACKWARD.value: "robot should drive backward",
    Action.STRAFE_LEFT.value: "robot should strafe left",
    Action.STRAFE_RIGHT.value: "robot should strafe right",
    Action.TURN_LEFT.value: "robot should turn left",
    Action.TURN_RIGHT.value: "robot should turn right",
    Action.STOP.value: "robot should stop and hold still",
    REJECT_LABEL: "request is ambiguous, multi-action, unrelated, unsafe, or unsupported",
}

_COMMAND_INSTRUCTIONS = (
    "Select exactly one supported locomotion action for the operator text. "
    "Choose one of move_forward, move_backward, strafe_left, strafe_right, "
    "turn_left, turn_right, or stop. Choose reject for ambiguous, "
    "multi-action, unrelated, unsafe, or unsupported requests."
)

_SPEED_CRITERIA = {
    SpeedMode.NORMAL.value: "default steady speed",
    SpeedMode.BOOST.value: "explicitly fast/double-speed request",
    SpeedMode.SLOW.value: "explicitly slow/careful request",
}

_SPEED_INSTRUCTIONS = (
    "Select the requested speed mode. Choose normal unless the text "
    "explicitly asks for fast or slow motion."
)

try:
    from typesafe_sdk import Choice as _SdkChoice

    Choice = _SdkChoice
except ImportError:  # pragma: no cover - lets unit tests run without the SDK

    @dataclass
    class Choice:  # type: ignore[no-redef]
        criteria: Mapping[str, str] = field(default_factory=dict)
        instructions: str = ""


class SystemOneClient(Protocol):
    def system_one(self, state: Any, questions: Mapping[str, Any]) -> Any:
        ...


def _check_threshold(confidence_threshold: float) -> None:
    if (
        isinstance(confidence_threshold, bool)
        or not isinstance(confidence_threshold, (int, float))
        or math.isnan(float(confidence_threshold))
        or not 0.0 <= float(confidence_threshold) <= 1.0
    ):
        raise ValueError("confidence_threshold must be a number in [0, 1]")


def _coerce_confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return min(1.0, max(0.0, parsed))


def _selected(response: Any, question_id: str) -> tuple[Any, float]:
    choices = getattr(response, "choices", None)
    entry = None
    if isinstance(choices, Mapping):
        entry = choices.get(question_id)
    if entry is None:
        return None, 0.0
    return getattr(entry, "choice", None), _coerce_confidence(
        getattr(entry, "confidence", 0.0)
    )


def route_text(
    text: str,
    client: SystemOneClient,
    *,
    confidence_threshold: float = 0.7,
) -> RoutedCommand:
    """Route one operator text to a RoutedCommand via a single JEV call."""
    _check_threshold(confidence_threshold)
    source = text.strip() if isinstance(text, str) else ""
    if not source:
        return RoutedCommand(
            action=None,
            speed_mode=SpeedMode.NORMAL,
            confidence=0.0,
            source_text=source,
            accepted=False,
            reason="empty input",
        )

    questions = {
        COMMAND_QUESTION: Choice(
            criteria=dict(_COMMAND_CRITERIA), instructions=_COMMAND_INSTRUCTIONS
        ),
        SPEED_QUESTION: Choice(
            criteria=dict(_SPEED_CRITERIA), instructions=_SPEED_INSTRUCTIONS
        ),
    }
    response = client.system_one(source, questions)

    command_label, command_confidence = _selected(response, COMMAND_QUESTION)
    speed_label, speed_confidence = _selected(response, SPEED_QUESTION)

    try:
        speed_mode = SpeedMode(speed_label)
    except ValueError:
        speed_mode = SpeedMode.NORMAL
    if speed_confidence < float(confidence_threshold):
        speed_mode = SpeedMode.NORMAL

    if command_label == REJECT_LABEL:
        return RoutedCommand(
            action=None,
            speed_mode=SpeedMode.NORMAL,
            confidence=command_confidence,
            source_text=source,
            accepted=False,
            reason="JEV rejected the request",
        )
    try:
        action = Action(command_label)
    except ValueError:
        return RoutedCommand(
            action=None,
            speed_mode=SpeedMode.NORMAL,
            confidence=command_confidence,
            source_text=source,
            accepted=False,
            reason=f"unknown command label: {command_label!r}",
        )
    if command_confidence < float(confidence_threshold):
        return RoutedCommand(
            action=None,
            speed_mode=SpeedMode.NORMAL,
            confidence=command_confidence,
            source_text=source,
            accepted=False,
            reason="command confidence below threshold",
        )
    return RoutedCommand(
        action=action,
        speed_mode=speed_mode,
        confidence=command_confidence,
        source_text=source,
        accepted=True,
        reason="routed",
    )
