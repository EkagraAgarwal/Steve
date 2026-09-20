"""A/B the decision layer on the same scenario.

Modes
  gated     JEV answers the tick questions; the gate escalates planning to the LLM
  llm_only  the LLM answers every question, and plans -- no JEV anywhere

Everything else is identical: same brain, same task library, same scenario,
same gate threshold. Differences belong to the decision layer.

    python -m dogbrain.bench complex
    python -m dogbrain.bench complex --no-memory
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from dogbrain.brain import DogBrain, Gate
from dogbrain.scenarios import ALL


@dataclass
class Run:
    mode: str
    ticks: int = 0
    actions: int = 0
    escalations: int = 0
    wall_s: float = 0.0
    judge_calls: int = 0
    judge_questions: int = 0
    judge_latency_s: float = 0.0
    planner_calls: int = 0
    planner_rejected: int = 0
    in_tokens: int = 0
    out_tokens: int = 0
    plan_completed: bool = False
    reacquired: bool = False
    notes: list[str] = field(default_factory=list)


def run_once(mode: str, scenario: str, *, threshold: float, remember: bool,
             verbose: bool, trace: bool = False) -> Run:
    from dogbrain.planner import LlmPlanner

    planner = LlmPlanner()
    if mode == "gated":
        from dogbrain.judgment import JevJudge

        judge: Any = JevJudge()
    else:
        from dogbrain.llm import LlmJudge

        judge = LlmJudge()

    if trace:
        from dogbrain.judgment import TracingJudge

        judge = TracingJudge(judge)

    brain = DogBrain(judge, gate=Gate(threshold=threshold), planner=planner,
                     remember=remember)
    r = Run(mode=mode)
    started = time.monotonic()
    for state in ALL[scenario]:
        if verbose and state.heard:
            print(f'  >> owner: "{state.heard.text}"')
        res = brain.tick(state)
        r.ticks += 1
        r.actions += len(res.actions)
        r.escalations += int(res.escalated)
        if verbose:
            print(res)
    r.wall_s = time.monotonic() - started

    ju = getattr(judge, "usage", None)
    if ju:
        r.judge_calls, r.judge_questions = ju.calls, ju.questions
        r.judge_latency_s = ju.latency_s
        r.in_tokens += getattr(ju, "input_tokens", 0)
        r.out_tokens += getattr(ju, "output_tokens", 0)
    pu = planner.usage
    r.planner_calls, r.planner_rejected = pu.calls, pu.rejected
    r.in_tokens += pu.input_tokens
    r.out_tokens += pu.output_tokens

    plan = brain.memory.plan
    r.plan_completed = bool(plan and getattr(plan, "complete", False))
    r.reacquired = len(brain.memory.entities) > 0
    r.notes.append(f"entities remembered: {len(brain.memory.entities)}")
    r.notes.append(f"bindings: {brain.memory.bindings}")
    return r


def table(runs: list[Run]) -> str:
    rows = [
        ("decision calls", lambda r: r.judge_calls + r.planner_calls),
        ("  judge calls", lambda r: r.judge_calls),
        ("  planner calls", lambda r: r.planner_calls),
        ("questions asked", lambda r: r.judge_questions),
        ("escalations", lambda r: r.escalations),
        ("plans rejected", lambda r: r.planner_rejected),
        ("actions emitted", lambda r: r.actions),
        ("wall clock (s)", lambda r: f"{r.wall_s:.2f}"),
        ("decision time (s)", lambda r: f"{r.judge_latency_s:.2f}"),
        ("s / decision", lambda r: f"{(r.judge_latency_s / r.judge_calls):.2f}" if r.judge_calls else "-"),
        ("LLM in-tokens", lambda r: r.in_tokens),
        ("LLM out-tokens", lambda r: r.out_tokens),
        ("entities kept", lambda r: r.notes[0].split(": ")[1]),
    ]
    w = max(len(n) for n, _ in rows) + 2
    head = "metric".ljust(w) + "".join(r.mode.ljust(14) for r in runs)
    out = [head, "-" * len(head)]
    for name, get in rows:
        out.append(name.ljust(w) + "".join(str(get(r)).ljust(14) for r in runs))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Compare JEV-gated vs LLM-only.")
    p.add_argument("scenario", nargs="?", default="complex")
    p.add_argument("--threshold", type=float, default=0.7)
    p.add_argument("--no-memory", action="store_true", help="forget entities that leave view")
    p.add_argument("--modes", default="gated,llm_only")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--trace", action="store_true", help="print every judgment question and answer")
    args = p.parse_args(argv)

    if args.scenario not in ALL:
        print(f"unknown scenario; choose from {', '.join(ALL)}", file=sys.stderr)
        return 2

    runs = []
    for mode in args.modes.split(","):
        print(f"\n===== {mode} =====")
        try:
            runs.append(run_once(mode.strip(), args.scenario, threshold=args.threshold,
                                 remember=not args.no_memory, verbose=args.verbose,
                                 trace=args.trace))
        except RuntimeError as err:
            print(f"error: {err}", file=sys.stderr)
            return 1
    print("\n" + table(runs))
    print(f"\nscenario={args.scenario}  memory={'off' if args.no_memory else 'on'}  "
          f"gate={args.threshold}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
