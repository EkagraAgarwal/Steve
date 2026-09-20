"""DogBrain: one tick = sense -> state -> memory -> interpret -> decide -> act.

Priority is fixed and enforced in code: safety, then the active task, then
autonomous behaviour. Judgment (JEV) is consulted only where code cannot
decide; every judgment carries a confidence, and anything below the gate
threshold is escalated to the LLM rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from dogbrain import actions as A
from dogbrain import judgment as J
from dogbrain.memory import Memory
from dogbrain.plan import Plan, PlanError, parse as parse_plan, single as single_plan
from dogbrain.state import State
from dogbrain.tasks import LIBRARY, TASK_NAMES

STOP_WORDS = ("stop", "wait", "halt", "no", "stay", "freeze")

BEHAVIOURS = {
    "rest": "nothing is happening; settle down and wait",
    "look_around": "idle but alert; scan the area",
    "investigate": "something interesting is nearby and worth a closer look",
    "greet_owner": "the owner is visible and has not been greeted recently",
    "patrol": "the area has been quiet for a while; make a round",
}


@dataclass
class TickResult:
    tick: int
    actions: list[A.Action] = field(default_factory=list)
    escalated: bool = False
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        head = f"[tick {self.tick}]" + ("  ESCALATED" if self.escalated else "")
        body = "\n".join(f"    {a}" for a in self.actions) or "    (no action)"
        note = "\n".join(f"    . {n}" for n in self.notes)
        return "\n".join(x for x in (head, note, body) if x)


@dataclass
class Gate:
    """The confidence gate. Below threshold, the LLM decides instead."""

    threshold: float = 0.7
    escalations: int = 0
    accepted: int = 0

    def check(self, *verdicts: J.Verdict) -> bool:
        ok = all(v.accepted(self.threshold) for v in verdicts)
        if ok:
            self.accepted += 1
        else:
            self.escalations += 1
        return ok

    @property
    def escalation_rate(self) -> float:
        total = self.accepted + self.escalations
        return self.escalations / total if total else 0.0


class DogBrain:
    def __init__(
        self,
        judge: J.Judge,
        *,
        gate: Optional[Gate] = None,
        safety: Optional[Callable[[State], Optional[A.Action]]] = None,
        lost_limit: int = 20,
        planner: Any = None,
        remember: bool = True,
    ) -> None:
        self.judge = judge
        self.planner = planner
        self.remember = remember
        self.memory = Memory()
        self.gate = gate or Gate()
        self.safety = safety or default_safety
        self.lost_limit = lost_limit
        self._lost_for = 0
        self._task_age = 0

    # -- the tick ---------------------------------------------------------
    def tick(self, state: State) -> TickResult:
        out = TickResult(tick=state.tick)

        # 1. safety first, always, regardless of task or judgment
        reflex = self.safety(state)
        if reflex is not None:
            self.memory.plan = None
            out.actions.append(reflex)
            out.notes.append("safety reflex pre-empted everything")
            return out

        # 2. memory update (code), with one judgment per genuinely new entity
        self._remember(state, out)

        # 3. new human text -> interpret (judgment + gate)
        if state.heard and state.heard.text.strip():
            if self._interpret(state, out) or out.escalated:
                return out

        # 4. active plan -> step it in code
        plan = self.memory.plan
        if isinstance(plan, Plan) and not plan.complete:
            self._step(plan, state, out)
            return out

        # 5. nothing to do -> autonomous behaviour (judgment)
        self._behave(state, out)
        return out

    # -- stages -----------------------------------------------------------
    def _remember(self, state: State, out: TickResult) -> None:
        if not self.remember:
            visible = {s.id for s in state.seen}
            for gone in [k for k in self.memory.entities if k not in visible]:
                del self.memory.entities[gone]
        for s in state.seen:
            ent = self.memory.observe(s.id, s.label, (s.distance, s.bearing), state.tick)
            if ent.meaning is None:
                meaning, interest = J.understand(self.judge, s.label, s.describe())
                ent.meaning = meaning.value or "unknown"
                ent.interest = float(interest.value or 0.0)
                out.notes.append(
                    f"understood {s.id}: {ent.meaning} (interest {ent.interest:.2f})"
                )

    def _interpret(self, state: State, out: TickResult) -> bool:
        """Returns True when the tick is fully handled and must end here."""
        text = state.heard.text.strip()

        # An explicit stop never waits on a model.
        if any(w == text.lower().strip(".!") for w in STOP_WORDS):
            self.memory.plan = None
            out.actions.append(A.halt("owner said stop"))
            out.notes.append("stop handled in code, no judgment call")
            return True

        targets = [
            (e.id, f"{e.label}, last seen {e.age(state.tick)} ticks ago"
                   if state.by_id(e.id) is None else (state.by_id(e.id) or e).describe())
            for e in self.memory.candidates(state.tick)
        ]
        kind, task, target = J.interpret(self.judge, text, targets, TASK_NAMES)
        out.notes.append(
            f"interpret: kind={kind.value}({kind.confidence:.2f}) "
            f"task={task.value}({task.confidence:.2f}) "
            f"target={target.value}({target.confidence:.2f})"
        )

        if kind.value == "none":
            return False
        if kind.value != "single_task" or not self.gate.check(kind, task):
            out.escalated = True
            reason = f"kind={kind.value} conf={min(kind.confidence, task.confidence):.2f}"
            if self.planner is not None:
                situation = "; ".join(s.describe() for s in state.seen) or "nothing in view"
                proposal = self.planner.plan(text, situation)
                verdict = self.set_plan(proposal) if proposal else "rejected: no plan returned"
                out.notes.append(f"escalated to planner -> {verdict}")
                if verdict.startswith("accepted"):
                    self._task_age = 0
                    self._lost_for = 0
                    return False
                getattr(self.planner, "usage", None) and setattr(
                    self.planner.usage, "rejected", self.planner.usage.rejected + 1)
            out.actions.append(A.escalate(text, reason))
            return False

        spec = LIBRARY.get(task.value or "")
        if spec is None:
            out.escalated = True
            out.actions.append(A.escalate(text, f"unknown task {task.value!r}"))
            return False

        args: dict[str, Any] = {}
        if spec.needs_target:
            if not target.accepted(self.gate.threshold):
                out.escalated = True
                out.actions.append(A.escalate(text, "target unresolved"))
                return False
            self.memory.bind("followed", str(target.value))
            args["target"] = f"${'followed'}"
        for p in spec.params:
            if p in args:
                continue
            args[p] = {"what": text, "place": text, "gesture": text,
                       "duration_ticks": 10}.get(p, text)
        try:
            self.memory.plan = single_plan(spec.name, known=set(self.memory.bindings), **args)
        except PlanError as err:
            out.escalated = True
            out.actions.append(A.escalate(text, f"plan rejected: {err}"))
            return False
        self._task_age = 0
        self._lost_for = 0
        out.notes.append(f"accepted -> {spec.name}({args})")
        return False

    def _step(self, plan: Plan, state: State, out: TickResult) -> None:
        step = plan.current
        assert step is not None
        self._task_age += 1
        name = step.do

        if name in ("follow", "watch"):
            target_id = self.memory.resolve(str(step.args.get("target")))
            seen = state.by_id(target_id) if target_id else None
            if seen is None:
                self._lost_for += 1
                ent = self.memory.entities.get(target_id or "")
                # Reacquisition: a same-label thing under a new track id may be
                # the one we lost. Code cannot tell; judgment can.
                if ent is not None and self._lost_for >= 1:
                    for cand in state.seen:
                        if cand.label != ent.label or cand.id == target_id:
                            continue
                        verdict = J.task_check(
                            self.judge,
                            f"Is this the same {ent.label} the robot was following?",
                            {"same": "it is the same one, under a new track id",
                             "different": "it is a different one"},
                            f"lost {ent.label} {self._lost_for} ticks ago at "
                            f"{ent.last_pos}; now sees {cand.describe()} at "
                            f"({cand.distance:.1f} m, {cand.bearing:.0f} deg)",
                        )
                        out.notes.append(
                            f"reacquire {cand.id}: {verdict.value} ({verdict.confidence:.2f})")
                        if verdict.value == "same" and verdict.accepted(self.gate.threshold):
                            for name, bound in list(self.memory.bindings.items()):
                                if bound == target_id:
                                    self.memory.bind(name, cand.id)
                            self._lost_for = 0
                            out.actions.append(
                                A.approach(cand.id, 1.0, f"re-bound from {target_id}"))
                            return
                if self._lost_for > self.lost_limit:
                    plan.failed_reason = f"lost {target_id} for {self._lost_for} ticks"
                    self._fail(plan, out)
                    return
                out.actions.append(A.search(ent.last_pos if ent else None,
                                            f"{target_id} not visible ({self._lost_for})"))
                return
            self._lost_for = 0
            if name == "watch" or seen.is_close:
                out.actions.append(A.face(seen.id, "close enough"))
            else:
                out.actions.append(A.approach(seen.id, 1.0, f"{seen.distance:.1f} m away"))
            return

        if name == "go_to":
            out.actions.append(A.navigate(str(step.args.get("place")), "plan step"))
            plan.advance()
            return

        if name == "find":
            want = str(step.args.get("what", ""))
            cand = state.closest()
            if cand is None:
                out.actions.append(A.explore("nothing in view"))
                return
            verdict = J.task_check(
                self.judge,
                f"Does this match what the owner asked for: {want!r}?",
                {"yes": "it matches the description", "no": "it does not match"},
                cand.describe(),
            )
            out.notes.append(f"find check {cand.id}: {verdict.value} ({verdict.confidence:.2f})")
            if verdict.value == "yes" and verdict.accepted(self.gate.threshold):
                if step.save_as:
                    self.memory.bind(step.save_as, cand.id)
                plan.advance()
                out.actions.append(A.approach(cand.id, 1.0, "match found"))
            else:
                out.actions.append(A.explore("no match yet"))
            return

        if name == "stay":
            out.actions.append(A.sit("stay"))
            if self._task_age >= int(step.args.get("duration_ticks", 10)):
                plan.advance()
            return

        if name == "perform":
            out.actions.append(A.gesture(str(step.args.get("gesture")), "perform"))
            plan.advance()
            return

        if name == "greet":
            out.actions.append(A.gesture("Hello", "greet"))
            plan.advance()
            return

        if name == "patrol":
            out.actions.append(A.patrol_once("patrol"))
            if self._task_age >= int(step.args.get("duration_ticks", 30)):
                plan.advance()
            return

        if name == "return_to_owner":
            owner = state.by_id("owner") or state.closest("person")
            if owner is None:
                out.actions.append(A.search(None, "owner not visible"))
                return
            if owner.is_close:
                plan.advance()
                out.actions.append(A.face(owner.id, "back with owner"))
            else:
                out.actions.append(A.approach(owner.id, 1.0, "returning"))
            return

        plan.failed_reason = f"no stepper for task {name!r}"
        self._fail(plan, out)

    def _fail(self, plan: Plan, out: TickResult) -> None:
        reason = plan.failed_reason or "unknown"
        out.notes.append(f"task failed: {reason}")
        if plan.on_fail == "abort":
            self.memory.plan = None
        elif plan.on_fail == "skip":
            plan.advance()
        else:
            self.memory.plan = None
            out.escalated = True
            out.actions.append(A.escalate(reason, "task failed, reporting"))

    def _behave(self, state: State, out: TickResult) -> None:
        situation = (
            f"quiet for {state.scene.quiet_for_s:.0f}s; "
            f"owner {'visible' if state.scene.owner_visible else 'not visible'}; "
            f"sees {[s.describe() for s in state.seen] or 'nothing'}"
        )
        verdict = J.behave(self.judge, situation, BEHAVIOURS)
        out.notes.append(f"behave: {verdict.value} ({verdict.confidence:.2f})")
        mapping = {
            "rest": A.sit("idle"),
            "look_around": A.search(None, "idle scan"),
            "investigate": A.approach(
                (state.closest().id if state.closest() else "unknown"), 1.0, "curious"),
            "greet_owner": A.gesture("Hello", "owner present"),
            "patrol": A.patrol_once("quiet"),
        }
        out.actions.append(mapping.get(str(verdict.value), A.sit("default")))

    # -- plans proposed by the LLM ---------------------------------------
    def set_plan(self, raw: dict[str, Any]) -> str:
        """Accept a plan from the LLM. Code validates before it is trusted."""
        try:
            self.memory.plan = parse_plan(raw, known=set(self.memory.bindings))
        except PlanError as err:
            return f"rejected: {err}"
        self._task_age = 0
        self._lost_for = 0
        return f"accepted: {len(self.memory.plan.steps)} steps"

    def cancel_plan(self) -> str:
        self.memory.plan = None
        return "cancelled"

    def plan_status(self) -> dict[str, Any]:
        plan = self.memory.plan
        if not isinstance(plan, Plan):
            return {"active": False}
        return {
            "active": not plan.complete,
            "goal": plan.goal,
            "step": plan.index,
            "of": len(plan.steps),
            "doing": plan.current.do if plan.current else None,
        }


def default_safety(state: State) -> Optional[A.Action]:
    """Plain code. Runs before judgment and overrides everything."""
    for s in state.seen:
        if s.distance < 0.35 and abs(s.bearing) < 45:
            return A.halt(f"obstacle {s.label} at {s.distance:.2f} m")
    if state.self_.battery < 5:
        return A.halt("battery critical")
    return None
