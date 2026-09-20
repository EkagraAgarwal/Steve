"""Action intents. The brain emits these; an adapter maps them to DimOS skills.

Keeping them abstract is what lets the whole brain be tested with no robot,
no simulator and no DimOS import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Action:
    kind: str
    args: dict[str, Any]
    why: str = ""

    def __str__(self) -> str:
        arg = ", ".join(f"{k}={v!r}" for k, v in self.args.items())
        return f"{self.kind}({arg})" + (f"  # {self.why}" if self.why else "")


def approach(target: str, stop_at: float = 1.0, why: str = "") -> Action:
    return Action("approach", {"target": target, "stop_at": stop_at}, why)


def face(target: str, why: str = "") -> Action:
    return Action("face", {"target": target}, why)


def search(pos: Optional[tuple[float, float]], why: str = "") -> Action:
    return Action("search", {"last_pos": pos}, why)


def navigate(place: str, why: str = "") -> Action:
    return Action("navigate", {"place": place}, why)


def gesture(name: str, why: str = "") -> Action:
    return Action("gesture", {"name": name}, why)


def sit(why: str = "") -> Action:
    return Action("sit", {}, why)


def speak(text: str, why: str = "") -> Action:
    return Action("speak", {"text": text}, why)


def explore(why: str = "") -> Action:
    return Action("explore", {}, why)


def patrol_once(why: str = "") -> Action:
    return Action("patrol_once", {}, why)


def halt(why: str = "") -> Action:
    return Action("halt", {}, why)


def escalate(text: str, reason: str) -> Action:
    """Hand the decision to the LLM. The brain never guesses past the gate."""
    return Action("escalate", {"text": text, "reason": reason}, reason)
