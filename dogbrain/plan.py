"""Plans: ordered steps drawn from the task library, validated by code.

An LLM may propose a plan; it is never trusted. `validate` rejects unknown
tasks, missing parameters and unresolvable variables, and returns a reason
the caller can hand back to the proposer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from dogbrain.tasks import LIBRARY, TASK_NAMES

ON_FAIL = ("report", "skip", "abort")


@dataclass
class Step:
    do: str
    args: dict[str, Any] = field(default_factory=dict)
    save_as: Optional[str] = None


@dataclass
class Plan:
    goal: str
    steps: list[Step]
    on_fail: str = "report"
    index: int = 0
    failed_reason: Optional[str] = None

    @property
    def current(self) -> Optional[Step]:
        return self.steps[self.index] if 0 <= self.index < len(self.steps) else None

    @property
    def complete(self) -> bool:
        return self.index >= len(self.steps)

    def advance(self) -> None:
        self.index += 1


class PlanError(ValueError):
    """Raised with a human-readable reason a proposed plan was rejected."""


def parse(raw: dict[str, Any], known: Optional[set[str]] = None) -> Plan:
    """Build a Plan from a proposal dict. Raises PlanError with a reason.

    `known` names values already bound in memory (e.g. a target the
    interpreter just resolved), so a one-step plan may refer to them.
    """
    if not isinstance(raw, dict):
        raise PlanError("plan must be an object")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise PlanError("plan.steps must be a non-empty list")
    on_fail = raw.get("on_fail", "report")
    if on_fail not in ON_FAIL:
        raise PlanError(f"on_fail must be one of {ON_FAIL}, got {on_fail!r}")

    steps: list[Step] = []
    produced: set[str] = set(known or ())
    for i, s in enumerate(steps_raw):
        if not isinstance(s, dict) or "do" not in s:
            raise PlanError(f"step {i}: must be an object with a 'do' key")
        name = s["do"]
        spec = LIBRARY.get(name)
        if spec is None:
            raise PlanError(f"step {i}: unknown task {name!r}; choose from {TASK_NAMES}")
        args = {k: v for k, v in s.items() if k not in ("do", "save_as")}
        missing = [p for p in spec.params if p not in args]
        if missing:
            raise PlanError(f"step {i} ({name}): missing required {missing}")
        for key, val in args.items():
            if isinstance(val, str) and val.startswith("$"):
                if val[1:] not in produced:
                    raise PlanError(
                        f"step {i} ({name}): {key}={val} refers to a value no earlier step saved"
                    )
        save_as = s.get("save_as")
        if save_as:
            produced.add(save_as)
        steps.append(Step(do=name, args=args, save_as=save_as))

    return Plan(goal=str(raw.get("goal", "")), steps=steps, on_fail=on_fail)


def single(task: str, known: Optional[set[str]] = None, **args: Any) -> Plan:
    """Build a one-step plan -- the fast path when judgment picks a task directly."""
    return parse({"goal": task, "steps": [{"do": task, **args}]}, known=known)
