"""Tick state: rebuilt from scratch every tick, never persisted.

Everything here is what the robot can sense *right now*. Anything that must
outlive a tick belongs in `dogbrain.memory`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

Motion = Literal["still", "approaching", "receding", "crossing", "unknown"]


@dataclass(frozen=True)
class Seen:
    """One entity visible in this tick."""

    id: str
    label: str
    distance: float           # metres
    bearing: float            # degrees, 0 = straight ahead, + = left
    meaning: Optional[str] = None      # filled from memory (Understand call)
    motion: Motion = "unknown"

    @property
    def is_close(self) -> bool:
        return self.distance <= 1.0

    def describe(self) -> str:
        """Short phrase a judgment model can choose between."""
        near = "near" if self.distance < 1.5 else ("far" if self.distance > 4 else "mid")
        side = "ahead" if abs(self.bearing) < 25 else ("left" if self.bearing > 0 else "right")
        return f"{self.label}, {near}, {side}"


@dataclass(frozen=True)
class Heard:
    """Committed human text only. Never partial transcripts."""

    text: str
    source: str = "owner"
    age_s: float = 0.0


@dataclass(frozen=True)
class SelfState:
    posture: Literal["standing", "sitting", "lying", "moving"] = "standing"
    battery: float = 100.0
    doing: Optional[str] = None            # active task name
    current_step: Optional[int] = None


@dataclass(frozen=True)
class Scene:
    quiet_for_s: float = 0.0
    owner_visible: bool = False


@dataclass(frozen=True)
class State:
    self_: SelfState = field(default_factory=SelfState)
    seen: tuple[Seen, ...] = ()
    heard: Optional[Heard] = None
    scene: Scene = field(default_factory=Scene)
    tick: int = 0

    def by_id(self, entity_id: str) -> Optional[Seen]:
        for s in self.seen:
            if s.id == entity_id:
                return s
        return None

    def closest(self, label: Optional[str] = None) -> Optional[Seen]:
        cands = [s for s in self.seen if label is None or s.label == label]
        return min(cands, key=lambda s: s.distance) if cands else None
