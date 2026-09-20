"""JEV voice-command router: finalized transcript in, allowlisted decision out.

Pure TypeSafe Jev classification only -- no LLM, no heuristic action
guessing. ``route_command(text, client)`` makes exactly one
``client.system_one`` call (a single ``Choice`` question) and returns an
immutable :class:`VoiceCommand` with ``to_dict()``. Rejections keep
``action=None`` and must never actuate; an explicit ``stop`` is an
accepted action, distinct from rejection.

Safety policy (fail closed, never clamp):
- strict allowlist: forward, backward, strafe_left, strafe_right,
  turn_left, turn_right, stop. Anything else (JEV ``other``) rejects.
- finite JEV confidence >= ``confidence_threshold`` (default 0.7).
- multi-action texts (two motion intents, or two numeric magnitudes)
  reject, even if JEV picked one label confidently.
- incompatible units reject (meters on a turn, degrees on a translation).
- unsafe magnitudes reject: non-positive, over-cap, or unrecognized
  distance/angle units. Defaults 0.5 m / 30 deg; caps 2.0 m / 90 deg.
- JEV/network/response failures reject, never raise.

Verified against typesafe-sdk 0.7.0 (installed, offline): ``Choice``
takes ``criteria`` + ``instructions``; ``TypeSafeClient.system_one``
takes ``(state, questions, *, timeout, ...)`` and returns
``SystemOneResponse`` whose answers live in ``.answers`` (``.choices``
is a derived view). Parsing below accepts either mapping so fakes and
both SDK shapes work. No live requests are made by this module.
"""

import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol

ACTION_QUESTION = "action"
REJECT_LABEL = "other"

CONFIDENCE_THRESHOLD: float = 0.7
DEFAULT_DISTANCE_M: float = 0.5
DEFAULT_TURN_DEGREES: float = 30.0
MAX_DISTANCE_M: float = 2.0
MAX_TURN_DEGREES: float = 90.0

FORWARD = "forward"
BACKWARD = "backward"
STRAFE_LEFT = "strafe_left"
STRAFE_RIGHT = "strafe_right"
TURN_LEFT = "turn_left"
TURN_RIGHT = "turn_right"
STOP = "stop"

TRANSLATION_ACTIONS = frozenset({FORWARD, BACKWARD, STRAFE_LEFT, STRAFE_RIGHT})
TURN_ACTIONS = frozenset({TURN_LEFT, TURN_RIGHT})
MOTION_ACTIONS = frozenset(
    {FORWARD, BACKWARD, STRAFE_LEFT, STRAFE_RIGHT, TURN_LEFT, TURN_RIGHT}
)
ALLOWLIST = frozenset({*MOTION_ACTIONS, STOP})

# (forward_m, left_m, turn_deg) unit vector per motion action; scaled by
# the validated magnitude. Shared single source of truth with the dimOS
# JevMovementTeleop dispatch (it must not duplicate this table).
ACTION_UNIT_DELTAS: dict = {
    FORWARD: (1.0, 0.0, 0.0),
    BACKWARD: (-1.0, 0.0, 0.0),
    STRAFE_LEFT: (0.0, 1.0, 0.0),
    STRAFE_RIGHT: (0.0, -1.0, 0.0),
    TURN_LEFT: (0.0, 0.0, 1.0),
    TURN_RIGHT: (0.0, 0.0, -1.0),
}

_ACTION_CRITERIA = {
    FORWARD: "robot should drive straight forward",
    BACKWARD: "robot should drive straight backward",
    STRAFE_LEFT: "robot should sidestep left without turning",
    STRAFE_RIGHT: "robot should sidestep right without turning",
    TURN_LEFT: "robot should rotate left in place",
    TURN_RIGHT: "robot should rotate right in place",
    STOP: "operator wants all motion stopped immediately",
    REJECT_LABEL: (
        "request is ambiguous, multi-action, unrelated, unsafe, or "
        "unsupported -- pick this for anything that is not one single "
        "immediate directional move or stop"
    ),
}

_ACTION_INSTRUCTIONS = (
    "Select exactly one label for the operator text: one of forward, "
    "backward, strafe_left, strafe_right, turn_left, turn_right, or stop. "
    "Choose other for ambiguous, multi-action, unrelated, unsafe, or "
    "unsupported requests."
)

try:
    from typesafe_sdk import Choice as _SdkChoice  # type: ignore[no-redef]

    Choice = _SdkChoice  # type: ignore[assignment]
except ImportError:  # pragma: no cover - offline unit tests run without the SDK

    @dataclass
    class Choice:  # type: ignore[no-redef]
        criteria: Mapping[str, str] = field(default_factory=dict)
        instructions: str = ""


class SystemOneClient(Protocol):
    def system_one(self, *args: Any, **kwargs: Any) -> Any:
        ...


def _check_threshold(confidence_threshold: float) -> None:
    if (
        isinstance(confidence_threshold, bool)
        or not isinstance(confidence_threshold, (int, float))
        or not math.isfinite(float(confidence_threshold))
        or not 0.0 <= float(confidence_threshold) <= 1.0
    ):
        raise ValueError("confidence_threshold must be a finite number in [0, 1]")


def _coerce_confidence(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return min(1.0, max(0.0, parsed))


def _selected(response: Any, question_id: str) -> tuple[Any, float]:
    """Pull (choice, confidence) from an SDK 0.7.0 or fake response.

    SDK 0.7.0 exposes ``.answers`` with ``.choices`` as a derived view;
    older fakes expose ``.choices`` directly. Accept either mapping;
    anything malformed yields (None, 0.0) so the caller rejects.
    """
    try:
        for attr in ("answers", "choices"):
            mapping = getattr(response, attr, None)
            if isinstance(mapping, Mapping):
                entry = mapping.get(question_id)
                if entry is not None:
                    return getattr(entry, "choice", None), _coerce_confidence(
                        getattr(entry, "confidence", 0.0)
                    )
    except Exception:
        pass
    return None, 0.0


_NUMBER_RE = re.compile(r"(-?\d+(?:\.\d+)?)")

_WORD_NUMBERS: dict = {
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
}
_WORD_NUMBER_RE = re.compile(
    r"\b(" + "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# Fail-closed multi-action tripwires. These only ever cause rejection, so
# a missed cue degrades to trusting JEV, never to unsafe actuation.
_ACTION_CUES: tuple = (
    (FORWARD, re.compile(r"\b(forward|forwards|ahead|straight)\b", re.IGNORECASE)),
    (
        BACKWARD,
        re.compile(r"\b(backwards?|reverse|back\s*up|step\s*back)\b", re.IGNORECASE),
    ),
    (
        STRAFE_LEFT,
        re.compile(
            r"\b((strafe|slide|sidestep|side\s*step|move|step|shift)\s*left)\b",
            re.IGNORECASE,
        ),
    ),
    (
        STRAFE_RIGHT,
        re.compile(
            r"\b((strafe|slide|sidestep|side\s*step|move|step|shift)\s*right)\b",
            re.IGNORECASE,
        ),
    ),
    (
        TURN_LEFT,
        re.compile(r"\b((turn|rotate|spin|pivot)\s*left)\b", re.IGNORECASE),
    ),
    (
        TURN_RIGHT,
        re.compile(r"\b((turn|rotate|spin|pivot)\s*right)\b", re.IGNORECASE),
    ),
    (STOP, re.compile(r"\b(stop|stops|halt|freeze|froze|cancel|e-?stop)\b", re.IGNORECASE)),
)

_STOP_RE = re.compile(
    r"\b(stop|stops|halt|freeze|froze|frozen|cancel|e-?stop|hold\s+(still|on|up)"
    r"|stand\s+still|stay(\s+still|\s+put|\s+there)?|whoa|wait)\b",
    re.IGNORECASE,
)


def is_local_stop(text: str) -> bool:
    """True if text carries any stop cue; used for the JEV-bypass path."""
    return bool(_STOP_RE.search(text)) if isinstance(text, str) else False


def _cued_actions(text: str) -> set:
    return {label for label, rx in _ACTION_CUES if rx.search(text)}


def _unit_after(text: str, end: int) -> str:
    """Classify the unit token right after a number: m/cm/mm/deg/??/none."""
    tail = text[end : end + 12].lstrip().lower()
    if not tail:
        return "none"
    if tail.startswith("°") or tail.startswith("deg"):
        return "deg"
    if tail.startswith("cm") or tail.startswith("centimet"):
        return "cm"
    if tail.startswith("mm") or tail.startswith("millimet"):
        return "mm"
    if tail.startswith("meters") or tail.startswith("meter") or tail[0] == "m":
        # "m" alone only counts when it starts the token (1m / 1 m).
        if tail.startswith("meter") or (tail[0] == "m" and (len(tail) == 1 or not tail[1].isalpha())):
            return "m"
    if tail.startswith("ft") or tail.startswith("feet") or tail.startswith("foot"):
        return "unsupported"
    if tail.startswith("km") or tail.startswith("mi") or tail.startswith("mile"):
        return "unsupported"
    if tail.startswith("in") or tail.startswith("inch"):
        return "unsupported"
    if tail.startswith("yd") or tail.startswith("yard"):
        return "unsupported"
    first = re.match(r"[a-z°]+", tail)
    if first and first.group(0) in ("degree", "degrees"):
        return "deg"
    return "none"


def _parse_magnitude(text: str, action: str) -> tuple:
    """Validate (magnitude_value) or return (None, reject_reason)."""
    lowered = text.lower()
    is_turn = action in TURN_ACTIONS
    digits = list(_NUMBER_RE.finditer(lowered))
    if len(digits) >= 2:
        return None, "multiple magnitudes look like a multi-action request"
    if digits:
        raw = float(digits[0].group(1))
        unit = _unit_after(lowered, digits[0].end())
    else:
        word = _WORD_NUMBER_RE.search(lowered)
        raw = _WORD_NUMBERS[word.group(1).lower()] if word else None
        unit = "none"
    if raw is None:
        return (DEFAULT_TURN_DEGREES if is_turn else DEFAULT_DISTANCE_M), ""
    if not math.isfinite(raw):
        return None, "non-finite magnitude"
    if raw <= 0:
        return None, "non-positive magnitude is unsafe"
    value = raw
    if unit == "cm" or unit == "mm":
        if is_turn:
            return None, "centimeter/millimeter unit is incompatible with a turn"
        value = raw / 100.0 if unit == "cm" else raw / 1000.0
    elif unit == "m":
        if is_turn:
            return None, "meter unit is incompatible with a turn"
    elif unit == "deg":
        if not is_turn:
            return None, "degree unit is incompatible with a translation"
        value = raw
    elif unit == "unsupported":
        return None, "unsupported unit (use m/cm/mm for moves, degrees for turns)"
    # unit == "none": bare number takes the action's default unit.
    cap = MAX_TURN_DEGREES if is_turn else MAX_DISTANCE_M
    if value > cap:
        unit_name = "degrees" if is_turn else "m"
        return None, f"magnitude {value:g}{unit_name} exceeds cap ({cap:g}); rejected, not clamped"
    return value, ""


@dataclass(frozen=True)
class VoiceCommand:
    """Immutable result of routing one finalized transcript.

    Rejections keep ``action=None`` (never actuate). An explicit stop is
    an accepted ``action="stop"`` with ``magnitude=None``, distinct from
    rejection. ``magnitude`` is meters for translations, degrees for
    turns, and None for stop/reject.
    """

    accepted: bool
    action: Optional[str]
    confidence: float
    magnitude: Optional[float]
    reason: str
    source_text: str

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "action": self.action,
            "confidence": self.confidence,
            "magnitude": self.magnitude,
            "reason": self.reason,
            "source_text": self.source_text,
        }


def _reject(source: str, confidence: float, reason: str) -> VoiceCommand:
    return VoiceCommand(
        accepted=False,
        action=None,
        confidence=confidence,
        magnitude=None,
        reason=reason,
        source_text=source,
    )


def route_command(
    text: str,
    client: SystemOneClient,
    *,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    timeout: Optional[float] = None,
) -> VoiceCommand:
    """Route one finalized transcript to a VoiceCommand via one JEV call."""
    _check_threshold(confidence_threshold)
    source = text.strip() if isinstance(text, str) else ""
    if not source:
        return _reject(source, 0.0, "empty input")
    questions = {
        ACTION_QUESTION: Choice(
            criteria=dict(_ACTION_CRITERIA), instructions=_ACTION_INSTRUCTIONS
        ),
    }
    try:
        if timeout is None:
            response = client.system_one(source, questions)
        else:
            response = client.system_one(source, questions, timeout=timeout)
    except Exception as exc:
        return _reject(source, 0.0, f"JEV request failed: {type(exc).__name__}")

    label, confidence = _selected(response, ACTION_QUESTION)
    if label == REJECT_LABEL:
        return _reject(source, confidence, "JEV did not recognize a single motion command")
    if label not in ALLOWLIST:
        return _reject(source, confidence, f"unknown action label: {label!r}")
    if not math.isfinite(confidence) or confidence < float(confidence_threshold):
        return _reject(source, confidence, "action confidence below threshold")

    if label == STOP:
        return VoiceCommand(
            accepted=True,
            action=STOP,
            confidence=confidence,
            magnitude=None,
            reason="routed stop",
            source_text=source,
        )

    cued = _cued_actions(source)
    if len(cued) >= 2:
        return _reject(source, confidence, "multiple distinct actions in one request")
    magnitude, mag_reason = _parse_magnitude(source, label)
    if magnitude is None:
        return _reject(source, confidence, mag_reason)
    return VoiceCommand(
        accepted=True,
        action=label,
        confidence=confidence,
        magnitude=magnitude,
        reason="routed",
        source_text=source,
    )
