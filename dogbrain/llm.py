"""LLM judge: the same questions, answered by a general model instead of JEV.

This exists so the comparison is controlled. The brain, the task library,
the gate and the scenarios are identical in both modes; the only thing that
changes is who answers the questions. Differences in the numbers are
therefore attributable to the decision layer and nothing else.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass
class _Ans:
    choice: Any = None
    confidence: float = 0.0
    noul: Any = None
    score: float = 0.0


@dataclass
class _Resp:
    choices: dict[str, _Ans] = field(default_factory=dict)
    nouls: dict[str, _Ans] = field(default_factory=dict)
    scores: dict[str, _Ans] = field(default_factory=dict)


@dataclass
class Usage:
    calls: int = 0
    questions: int = 0
    latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    by_site: dict[str, int] = field(default_factory=dict)


SYSTEM = (
    "You are the decision layer of a dog-like quadruped robot. You will be given "
    "the robot's current situation and a set of questions. Answer every question. "
    "Reply with JSON only: an object mapping each question id to "
    '{"choice": <one of the listed options, exactly>, "confidence": <0..1>}. '
    "For score questions use {\"score\": <0..1>, \"confidence\": <0..1>}. "
    "Confidence must reflect genuine uncertainty; do not default to 1.0."
)


class LlmJudge:
    """Answers the brain's questions with a general-purpose LLM."""

    def __init__(self, model: str = "gpt-5.6-luna", client: Any = None) -> None:
        if client is None:
            from openai import OpenAI

            if not os.getenv("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY is not set")
            client = OpenAI()
        self._client = client
        self._model = model
        self.usage = Usage()

    def ask(self, state: Any, questions: Mapping[str, Any], *, site: str) -> Any:
        spec_lines = []
        score_ids = set()
        for qid, q in questions.items():
            crit = getattr(q, "criteria", None) or {}
            instr = getattr(q, "instructions", "") or ""
            if isinstance(crit, dict):
                opts = "\n".join(f"      - {k}: {v}" for k, v in crit.items())
                spec_lines.append(f"  {qid} (choice) -- {instr}\n{opts}")
            else:
                score_ids.add(qid)
                opts = "; ".join(str(c) for c in crit)
                spec_lines.append(f"  {qid} (score 0..1) -- {instr} Scale: {opts}")

        prompt = f"SITUATION:\n{state}\n\nQUESTIONS:\n" + "\n".join(spec_lines)
        started = time.monotonic()
        resp = self._client.responses.create(
            model=self._model,
            input=[{"role": "system", "content": SYSTEM},
                   {"role": "user", "content": prompt}],
        )
        elapsed = time.monotonic() - started

        self.usage.calls += 1
        self.usage.questions += len(questions)
        self.usage.latency_s += elapsed
        self.usage.by_site[site] = self.usage.by_site.get(site, 0) + 1
        u = getattr(resp, "usage", None)
        if u is not None:
            self.usage.input_tokens += getattr(u, "input_tokens", 0) or 0
            self.usage.output_tokens += getattr(u, "output_tokens", 0) or 0

        return _parse(getattr(resp, "output_text", "") or "", questions, score_ids)


def _parse(text: str, questions: Mapping[str, Any], score_ids: set[str]) -> _Resp:
    out = _Resp()
    data: dict[str, Any] = {}
    try:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        data = {}
    for qid in questions:
        entry = data.get(qid) or {}
        conf = _num(entry.get("confidence"), 0.0)
        if qid in score_ids:
            out.scores[qid] = _Ans(score=_num(entry.get("score"), 0.0), confidence=conf)
        else:
            out.choices[qid] = _Ans(choice=entry.get("choice"), confidence=conf)
    return out


def _num(value: Any, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, f)) if f == f else default
