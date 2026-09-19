"""CLI: route one operator text through JEV and print the JSON result."""

import argparse
import json

from typesafe_sdk import TypeSafeClient

from steve_router.commands import to_motion
from steve_router.router import route_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Route operator text to an allowlisted robot command via JEV."
    )
    parser.add_argument(
        "text",
        nargs="+",
        help="operator text, e.g. steve-route move forward quickly",
    )
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.7,
        help="minimum JEV command confidence to accept (0..1)",
    )
    parser.add_argument(
        "--model",
        default="jev-latest",
        help="JEV model id (default: jev-latest)",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    text = " ".join(args.text)
    # Auth comes from the documented TYPESAFE_API_KEY env var, read by the SDK.
    client = TypeSafeClient(model=args.model)
    command = route_text(text, client, confidence_threshold=args.confidence_threshold)
    motion = to_motion(command)
    print(
        json.dumps(
            {
                "accepted": command.accepted,
                "action": command.action.value if command.action else None,
                "speed_mode": command.speed_mode.value,
                "confidence": command.confidence,
                "reason": command.reason,
                "source_text": command.source_text,
                "motion": list(motion) if motion is not None else None,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
