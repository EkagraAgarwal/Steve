"""The LLM planner: what the gate escalates to.

JEV chooses among fixed options; it cannot compose a sequence. When the gate
refuses -- multi-step request, low confidence, unresolved target -- this asks
a general model for a plan. The plan is still validated by code before the
brain will run a single step of it.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from dogbrain.tasks import LIBRARY


@dataclass
class PlannerUsage:
    calls: int = 0
    latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    rejected: int = 0


def _library_text() -> str:
    lines = []
    for spec in LIBRARY.values():
        params = ", ".join(spec.params) or "none"
        lines.append(f"  {spec.name}(required: {params}) -- {spec.description}")
    return "\n".join(lines)


SYSTEM = (
    "You plan for a dog-like quadruped robot. Break the request into an ordered "
    "list of steps drawn ONLY from the task library. Reply with JSON only:\n"
    '{"goal": "...", "steps": [{"do": "<task>", "<param>": "<value>", '
    '"save_as": "<name>"}], "on_fail": "report"}\n'
    "Use save_as to name a step's result, and \"$name\" in a later step to refer "
    "to it. Never invent a task or a parameter."
)


class LlmPlanner:
    def __init__(self, model: str = "gpt-5.6-luna", client: Any = None) -> None:
        if client is None:
            from openai import OpenAI

            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY is not set")
            client = OpenAI()
        self._client = client
        self._model = model
        self.usage = PlannerUsage()

    def plan(self, text: str, situation: str = "") -> Optional[dict[str, Any]]:
        prompt = (
            f"TASK LIBRARY:\n{_library_text()}\n\n"
            f"SITUATION:\n{situation or '(nothing notable)'}\n\n"
            f"REQUEST:\n{text}"
        )
        started = time.monotonic()
        resp = self._client.responses.create(
            model=self._model,
            input=[{"role": "system", "content": SYSTEM},
                   {"role": "user", "content": prompt}],
        )
        self.usage.calls += 1
        self.usage.latency_s += time.monotonic() - started
        u = getattr(resp, "usage", None)
        if u is not None:
            self.usage.input_tokens += getattr(u, "input_tokens", 0) or 0
            self.usage.output_tokens += getattr(u, "output_tokens", 0) or 0

        text_out = getattr(resp, "output_text", "") or ""
        try:
            start, end = text_out.find("{"), text_out.rfind("}")
            if start < 0 or end <= start:
                return None
            return json.loads(text_out[start:end + 1])
        except (ValueError, TypeError):
            return None
