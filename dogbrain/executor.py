"""Execute DogBrain action intents against a running DimOS via MCP.

This is the join between the thinking layer and the robot. The brain decides
(JEV), this turns each decision into an MCP tool call. No LLM is involved:
the model chose the action, code performs it.

The geometry lives here, not in the brain -- `approach` and `face` arrive as
target + distance + bearing and become a relative `move_to`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from dogbrain.actions import Action
from dogbrain.state import State

# DogBrain action -> MCP tool. Actions with no direct tool are computed below.
DIRECT: dict[str, str] = {
    "navigate": "navigate_with_text",
    "gesture": "execute_sport_command",
    "speak": "speak",
    "explore": "begin_exploration",
    "patrol_once": "start_patrol",
    "halt": "stop_navigation",
    "escalate": "agent_send",
}


@dataclass
class Call:
    tool: str
    args: dict[str, Any]
    ok: Optional[bool] = None
    result: Any = None

    def __str__(self) -> str:
        status = "" if self.ok is None else ("  ok" if self.ok else "  FAILED")
        return f"{self.tool}({self.args}){status}"


@dataclass
class ExecutorStats:
    calls: int = 0
    failures: int = 0
    by_tool: dict[str, int] = field(default_factory=dict)


class DimosExecutor:
    """Turns Action intents into MCP tool calls on a running DimOS."""

    def __init__(self, adapter: Any = None, *, dry_run: bool = False,
                 timeout: int | None = None) -> None:
        self.dry_run = dry_run
        self.stats = ExecutorStats()
        self._adapter = adapter
        if adapter is None and not dry_run:
            from dimos.agents.mcp.mcp_adapter import McpAdapter

            self._adapter = McpAdapter.from_run_entry(timeout=timeout)

    # -- translation ------------------------------------------------------
    def plan_call(self, action: Action, state: Optional[State] = None) -> Optional[Call]:
        """Map one Action to one MCP Call. None means 'nothing to send'."""
        kind, args = action.kind, action.args

        if kind in DIRECT:
            tool = DIRECT[kind]
            if kind == "gesture":
                return Call(tool, {"command_name": args["name"]})
            if kind == "navigate":
                return Call(tool, {"query": args["place"]})
            if kind == "speak":
                return Call(tool, {"text": args["text"], "blocking": False})
            if kind == "escalate":
                return Call(tool, {"message": args.get("text", "")})
            return Call(tool, {})

        if kind == "sit":
            return Call("execute_sport_command", {"command_name": "Sit"})

        if kind in ("approach", "face"):
            seen = state.by_id(args["target"]) if state else None
            if seen is None:
                return None
            if kind == "face":
                return Call("move_to", {"x": 0.0, "y": 0.0,
                                        "degrees": round(seen.bearing, 1), "relative": True})
            stop_at = float(args.get("stop_at", 1.0))
            travel = max(0.0, seen.distance - stop_at)
            rad = math.radians(seen.bearing)
            return Call("move_to", {
                "x": round(travel * math.cos(rad), 2),
                "y": round(travel * math.sin(rad), 2),
                "degrees": round(seen.bearing, 1),
                "relative": True,
            })

        if kind == "search":
            pos = args.get("last_pos")
            if not pos:
                return Call("begin_exploration", {})
            distance, bearing = float(pos[0]), float(pos[1])
            rad = math.radians(bearing)
            return Call("move_to", {
                "x": round(max(0.0, distance - 1.0) * math.cos(rad), 2),
                "y": round(max(0.0, distance - 1.0) * math.sin(rad), 2),
                "degrees": round(bearing, 1),
                "relative": True,
            })

        return None

    # -- execution --------------------------------------------------------
    def execute(self, action: Action, state: Optional[State] = None) -> Optional[Call]:
        call = self.plan_call(action, state)
        if call is None:
            return None
        if self.dry_run:
            call.ok = True
            call.result = "(dry run)"
        else:
            try:
                call.result = self._adapter.call_tool(call.tool, call.args)
                call.ok = True
            except Exception as err:            # MCP transport or tool error
                call.ok = False
                call.result = str(err)
                self.stats.failures += 1
        self.stats.calls += 1
        self.stats.by_tool[call.tool] = self.stats.by_tool.get(call.tool, 0) + 1
        return call

    def execute_all(self, actions: list[Action], state: Optional[State] = None) -> list[Call]:
        return [c for c in (self.execute(a, state) for a in actions) if c is not None]
