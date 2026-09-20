"""Run a DogBrain scenario and print the thinking, tick by tick.

    python -m dogbrain.cli follow            # real JEV (needs TYPESAFE_API_KEY)
    python -m dogbrain.cli follow --fake     # offline, deterministic
    python -m dogbrain.cli --list
"""

from __future__ import annotations

import argparse
import sys

from dogbrain.brain import DogBrain, Gate
from dogbrain.scenarios import ALL


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run a DogBrain scenario.")
    p.add_argument("scenario", nargs="?", help=f"one of: {', '.join(ALL)}")
    p.add_argument("--fake", action="store_true", help="offline keyword judge, no API key")
    p.add_argument("--threshold", type=float, default=0.7, help="gate confidence threshold")
    p.add_argument("--model", default="jev-latest")
    p.add_argument("--list", action="store_true", help="list scenarios")
    p.add_argument("--trace", action="store_true", help="print every JEV question and answer")
    p.add_argument("--execute", action="store_true", help="send actions to a running DimOS over MCP")
    p.add_argument("--dry-run", action="store_true", help="with --execute, print calls without sending")
    args = p.parse_args(argv)

    if args.list or not args.scenario:
        print("scenarios:", ", ".join(ALL))
        return 0
    if args.scenario not in ALL:
        print(f"unknown scenario {args.scenario!r}; choose from {', '.join(ALL)}", file=sys.stderr)
        return 2

    if args.fake:
        from dogbrain.fake import FakeJudge

        judge = FakeJudge()
        print("judge: FakeJudge (offline keyword rules -- not a model)\n")
    else:
        from dogbrain.judgment import JevJudge

        try:
            judge = JevJudge(model=args.model)
        except RuntimeError as err:
            print(f"error: {err}", file=sys.stderr)
            return 1
        print(f"judge: JEV ({args.model})\n")
    if args.trace:
        from dogbrain.judgment import TracingJudge

        judge = TracingJudge(judge)

    executor = None
    if args.execute:
        from dogbrain.executor import DimosExecutor

        try:
            executor = DimosExecutor(dry_run=args.dry_run)
        except Exception as err:
            print(f"error: no running DimOS to execute against ({err})", file=sys.stderr)
            return 1
        print(f"executor: MCP{' (dry run)' if args.dry_run else ''}\n")

    brain = DogBrain(judge, gate=Gate(threshold=args.threshold))
    for state in ALL[args.scenario]:
        if state.heard:
            print(f'  >> owner: "{state.heard.text}"')
        result = brain.tick(state)
        print(result)
        if executor is not None:
            for call in executor.execute_all(result.actions, state):
                print(f"      -> {call}")
    g = brain.gate
    print(f"\ngate: {g.accepted} accepted, {g.escalations} escalated "
          f"({g.escalation_rate:.0%} escalation rate)")
    if executor is not None:
        print(f"mcp : {executor.stats.calls} calls, {executor.stats.failures} failures, "
              f"by tool {executor.stats.by_tool}")
    usage = getattr(judge, "usage", None)
    if usage:
        print(f"jev : {usage.calls} calls, {usage.questions} questions, "
              f"{usage.latency_s:.2f}s total, by site {usage.by_site}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
