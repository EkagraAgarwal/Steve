"""Memory: everything that must outlive a single tick."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Entity:
    """A thing the dog has seen, remembered after it leaves the frame."""

    id: str
    label: str
    meaning: Optional[str] = None        # from the Understand judgment
    interest: float = 0.0                # 0..1, from the Understand judgment
    last_seen_tick: int = 0
    last_pos: Optional[tuple[float, float]] = None
    last_interaction_tick: Optional[int] = None

    def age(self, now: int) -> int:
        return max(0, now - self.last_seen_tick)


@dataclass
class Memory:
    entities: dict[str, Entity] = field(default_factory=dict)
    bindings: dict[str, str] = field(default_factory=dict)   # name -> entity id
    plan: Optional[Any] = None                               # dogbrain.plan.Plan
    places: dict[str, tuple[float, float]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    # -- entities ---------------------------------------------------------
    def observe(self, entity_id: str, label: str, pos: tuple[float, float], tick: int) -> Entity:
        ent = self.entities.get(entity_id)
        if ent is None:
            ent = Entity(id=entity_id, label=label)
            self.entities[entity_id] = ent
        ent.last_seen_tick = tick
        ent.last_pos = pos
        return ent

    def is_new(self, entity_id: str) -> bool:
        """True when we have never run an Understand judgment on it."""
        ent = self.entities.get(entity_id)
        return ent is not None and ent.meaning is None

    def resolve(self, ref: str) -> Optional[str]:
        """Resolve '$name' through bindings; plain ids pass through."""
        if isinstance(ref, str) and ref.startswith("$"):
            return self.bindings.get(ref[1:])
        return ref

    def bind(self, name: str, entity_id: str) -> None:
        self.bindings[name] = entity_id

    def candidates(self, now: int, max_age: int = 50) -> list[Entity]:
        """Entities recent enough to still be a plausible target."""
        return [e for e in self.entities.values() if e.age(now) <= max_age]
