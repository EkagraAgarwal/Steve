"""Probe which sport commands this particular robot actually accepts.

DimOS models no Go2 variants: it looks a name up, sends the API id, and
returns whatever comes back. Both simulators return success unconditionally,
so THIS IS ONLY MEANINGFUL AGAINST THE REAL ROBOT.

    python -m dogbrain.probe --list
    python -m dogbrain.probe                 # safe commands only
    python -m dogbrain.probe --include-risky # adds jumps and flips

Risky commands can damage the robot or hurt someone. They are excluded by
default and each one is confirmed before it is sent.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

# Posture/utility commands that are safe to send standing in a clear space.
SAFE = [
    "BalanceStand", "StandUp", "RecoveryStand", "Sit", "RiseSit", "StandDown",
    "Hello", "Stretch", "WiggleHips", "Content", "Scrape", "FingerHeart", "Pose",
    "GetState", "GetBodyHeight", "GetFootRaiseHeight", "GetSpeedLevel",
    "EconomicGait", "ContinuousGait", "SwitchGait",
]

# Dynamic or acrobatic: needs space, battery, and a deliberate decision.
RISKY = [
    "FrontJump", "FrontPounce", "FrontFlip", "Backflip", "LeftFlip", "RightFlip",
    "Handstand", "Bound", "MoonWalk", "CrossStep", "OnesidedStep", "Wallow",
]

# Commands that take a parameter or change control ownership -- not probed.
SKIP = ["BodyHeight", "FootRaiseHeight", "SpeedLevel", "Trigger",
        "SwitchJoystick", "TrajectoryFollow", "Damp", "Dance1", "Dance2"]


def probe(names: list[str], *, settle: float, dry_run: bool,
          recover_after: bool) -> list[dict[str, Any]]:
    if dry_run:
        adapter = None
    else:
        from dimos.agents.mcp.mcp_adapter import McpAdapter

        adapter = McpAdapter.from_run_entry()

    results = []
    for name in names:
        row: dict[str, Any] = {"command": name}
        started = time.monotonic()
        if dry_run:
            row.update(ok=True, response="(dry run)", seconds=0.0)
        else:
            try:
                row["response"] = str(adapter.call_tool(
                    "execute_sport_command", {"command_name": name}))[:200]
                row["ok"] = True
            except Exception as err:
                row["response"] = str(err)[:200]
                row["ok"] = False
            row["seconds"] = round(time.monotonic() - started, 3)
        results.append(row)
        print(f"  {name:20} {'ok ' if row['ok'] else 'ERR'} "
              f"{row['seconds']:>6.3f}s  {row['response'][:70]}")

        if recover_after and not dry_run and name in ("Sit", "StandDown", "Wallow",
                                                      "FrontJump", "FrontFlip"):
            try:
                adapter.call_tool("execute_sport_command", {"command_name": "RecoveryStand"})
            except Exception:
                pass
        if not dry_run:
            time.sleep(settle)
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Probe sport-command support on this robot.")
    p.add_argument("--include-risky", action="store_true",
                   help="also probe jumps and flips (needs clear space and battery)")
    p.add_argument("--only", help="comma-separated command names to probe instead")
    p.add_argument("--settle", type=float, default=3.0, help="seconds between commands")
    p.add_argument("--dry-run", action="store_true", help="print the plan, send nothing")
    p.add_argument("--no-recover", action="store_true",
                   help="do not send RecoveryStand after posture changes")
    p.add_argument("--out", help="write results as JSON here")
    p.add_argument("--list", action="store_true")
    args = p.parse_args(argv)

    if args.list:
        print("safe :", ", ".join(SAFE))
        print("risky:", ", ".join(RISKY))
        print("skip :", ", ".join(SKIP))
        return 0

    names = ([n.strip() for n in args.only.split(",")] if args.only
             else SAFE + (RISKY if args.include_risky else []))

    print(f"probing {len(names)} commands "
          f"({'DRY RUN' if args.dry_run else 'LIVE -- the robot will move'})")
    if not args.dry_run:
        print("\n  The robot will physically move. Confirm: space is clear, nobody is")
        print("  within 2 m, battery is healthy, and someone holds the remote.")
        if args.include_risky:
            print("  RISKY commands included: jumps and flips can damage the robot.")
        try:
            if input("\n  type 'yes' to continue: ").strip().lower() != "yes":
                print("aborted")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\naborted")
            return 1

    results = probe(names, settle=args.settle, dry_run=args.dry_run,
                    recover_after=not args.no_recover)
    ok = [r["command"] for r in results if r["ok"]]
    bad = [r["command"] for r in results if not r["ok"]]
    print(f"\naccepted ({len(ok)}): {', '.join(ok) or '-'}")
    print(f"errored  ({len(bad)}): {', '.join(bad) or '-'}")
    print("\nNOTE: 'accepted' means the call returned without error. In simulation "
          "every command returns success. Only a human watching the real robot can "
          "confirm the motion actually happened.")
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(results, fh, indent=2)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
