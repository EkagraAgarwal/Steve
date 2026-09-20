"""CLI: console push-to-talk -> finalized transcript JSON, optional JEV route.

Console control is Enter-toggle on native Windows/Linux consoles: press
Enter once to start capturing, speak, press Enter again to release. True
hold-to-talk is not possible on a plain console without extra key
dependencies, so toggle is used and documented here.

Default output is the finalized transcript as JSON (no routing, no
actuation). With ``--route`` the CLI builds a ``TypeSafeClient`` and
calls the peer-owned ``steve_voice.commands.route_command(text, client)``
(expected to return an object with ``to_dict()``); the client only
produces a typed decision, it never actuates. A ``--sink`` hook is not
needed on the CLI; :func:`steve_voice.deepgram.run_push_to_talk` accepts
a ``sink`` callback for later simulation wiring.
"""

import argparse
import json
import sys
from typing import Any, Optional

from steve_voice.deepgram import FinalizedUtterance, run_push_to_talk


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Console push-to-talk voice input via Deepgram Flux. "
            "Enter-toggle: press Enter to start, speak, press Enter to "
            "release; only the post-release finalized transcript is used."
        )
    )
    parser.add_argument(
        "--route",
        action="store_true",
        help="route the finalized transcript through JEV (dry run, no actuation)",
    )
    parser.add_argument(
        "--model",
        default="jev-latest",
        help="JEV model id for --route (default: jev-latest)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="seconds to wait for EndOfTurn after release (default: 15)",
    )
    return parser


def format_transcript_json(utterance: FinalizedUtterance) -> str:
    return json.dumps(utterance.to_dict())


def route_transcript(text: str, model: str) -> Any:
    """Route finalized text via JEV; returns the peer command result.

    The peer-owned ``steve_voice.commands.route_command`` builds on the
    shared ``TypeSafeClient``. Imported lazily so ``--help`` and offline
    transcript tests never need the SDK or the peer module.
    """
    try:
        from typesafe_sdk import TypeSafeClient  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "typesafe-sdk is required for --route "
            "(pip install steve-router)"
        ) from exc
    try:
        from steve_voice.commands import route_command  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "steve_voice.commands is unavailable: the peer-owned routing "
            "module is still being built"
        ) from exc
    client = TypeSafeClient(model=model)
    return route_command(text, client)


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        utterance = run_push_to_talk(timeout=args.timeout)
    except RuntimeError as exc:
        print(json.dumps({"finalized": False, "error": str(exc)}))
        return 2
    if utterance is None:
        # Timeout / error / empty turn: fail closed, report, no routing.
        print(json.dumps({"finalized": False, "error": "no finalized utterance"}))
        return 1
    if not args.route:
        print(format_transcript_json(utterance))
        return 0
    try:
        result = route_transcript(utterance.text, args.model)
    except RuntimeError as exc:
        print(json.dumps({"finalized": True, "error": str(exc)}))
        return 2
    try:
        payload = result.to_dict()
    except AttributeError:
        payload = {"result": str(result)}
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
