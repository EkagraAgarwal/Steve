"""The judgment layer: JEV answers what code cannot decide.

One `system_one` call carries several questions at once, so a tick asks its
whole question set in a single round trip. Four call sites:

  understand  - a new entity appeared: what is it, how interesting
  interpret   - human text arrived: command_kind / task / target
  task_check   - a task reached a check point: "same ball?", "did they mean stop?"
  behave      - nothing to do: which autonomous action

Everything returns a Verdict carrying a confidence, so the gate upstream can
escalate to the LLM instead of acting on a guess.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence

from typesafe_sdk import Choice, Noul, Score

COMMAND_KINDS = ("none", "single_task", "multi_step", "conversation")
MEANINGS = ("toy", "food", "person", "obstacle", "animal", "furniture", "unknown")


@dataclass(frozen=True)
class Verdict:
    value: Any
    confidence: float
    question: str = ""
    raw: Any = None

    def accepted(self, threshold: float) -> bool:
        return self.value is not None and self.confidence >= threshold


@dataclass
class Usage:
    calls: int = 0
    questions: int = 0
    latency_s: float = 0.0
    by_site: dict[str, int] = field(default_factory=dict)


def _clamp(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if not math.isfinite(f) else min(1.0, max(0.0, f))


def _choice(response: Any, qid: str) -> Verdict:
    entry = (getattr(response, "choices", None) or {}).get(qid)
    if entry is None:
        return Verdict(None, 0.0, qid, response)
    return Verdict(getattr(entry, "choice", None), _clamp(getattr(entry, "confidence", 0.0)), qid, entry)


def _noul(response: Any, qid: str) -> Verdict:
    entry = (getattr(response, "nouls", None) or {}).get(qid)
    if entry is None:
        return Verdict(None, 0.0, qid, response)
    return Verdict(getattr(entry, "noul", None), 1.0, qid, entry)


def _score(response: Any, qid: str) -> Verdict:
    entry = (getattr(response, "scores", None) or {}).get(qid)
    if entry is None:
        return Verdict(None, 0.0, qid, response)
    return Verdict(_clamp(getattr(entry, "score", 0.0)),
                   _clamp(getattr(entry, "confidence", 0.0)), qid, entry)


class Judge(Protocol):
    """What the brain needs from a judgment provider."""

    def ask(self, state: Any, questions: Mapping[str, Any], *, site: str) -> Any: ...


class JevJudge:
    """Real JEV. Requires TYPESAFE_API_KEY."""

    def __init__(self, client: Any = None, *, model: str = "jev-latest") -> None:
        if client is None:
            from typesafe_sdk import TypeSafeClient

            if not os.getenv("TYPESAFE_API_KEY"):
                raise RuntimeError(
                    "TYPESAFE_API_KEY is not set. Put it in .env and export it "
                    "(set -a && . ./.env && set +a), or create one at "
                    "https://console.typesafe.ai/settings/keys"
                )
            client = TypeSafeClient()
        self._client = client
        self._model = model
        self.usage = Usage()

    def ask(self, state: Any, questions: Mapping[str, Any], *, site: str) -> Any:
        import time

        started = time.monotonic()
        response = self._client.system_one(state, dict(questions), model=self._model)
        self.usage.calls += 1
        self.usage.questions += len(questions)
        self.usage.latency_s += time.monotonic() - started
        self.usage.by_site[site] = self.usage.by_site.get(site, 0) + 1
        return response


# ---------------------------------------------------------------------------
# The four call sites. Each builds its questions, asks once, reads the answers.
# ---------------------------------------------------------------------------

def understand(judge: Judge, label: str, description: str) -> tuple[Verdict, Verdict]:
    """A new entity appeared. Returns (meaning, interest)."""
    questions = {
        "meaning": Choice(
            criteria={m: f"the {label} is best understood as {m}" for m in MEANINGS},
            instructions=(
                "A dog-like robot just noticed this thing. Choose what it means to the dog. "
                "Choose unknown only when nothing else fits."
            ),
        ),
        "interest": Score(
            criteria=["not worth attention at all", "mildly worth a look",
                      "clearly worth investigating"],
            instructions="How interesting is this to a curious dog right now?",
        ),
    }
    r = judge.ask(f"The robot sees: {description}", questions, site="understand")
    return _choice(r, "meaning"), _score(r, "interest")


def interpret(
    judge: Judge, text: str, targets: Sequence[tuple[str, str]], tasks: Sequence[str]
) -> tuple[Verdict, Verdict, Verdict]:
    """Human text arrived. Returns (command_kind, task, target)."""
    questions: dict[str, Any] = {
        "command_kind": Choice(
            criteria={
                "none": "not addressed to the robot, or asks for nothing",
                "single_task": "one supported action the robot can start right now",
                "multi_step": "several actions in sequence, or needs planning",
                "conversation": "a question or remark to answer in words, not motion",
            },
            instructions="Classify what the speaker wants from the robot.",
        ),
        "task": Choice(
            criteria={t: _task_criteria(t) for t in tasks},
            instructions=(
                "If this is a single action, choose the task that carries it out. "
                "Choose the closest match; a later gate decides whether to trust it."
            ),
        ),
    }
    if targets:
        questions["target"] = Choice(
            criteria={eid: desc for eid, desc in targets},
            instructions="Which thing is the speaker referring to?",
        )
    r = judge.ask(text, questions, site="interpret")
    target = _choice(r, "target") if targets else Verdict(None, 0.0, "target")
    return _choice(r, "command_kind"), _choice(r, "task"), target


def task_check(judge: Judge, question: str, options: Mapping[str, str], state: str) -> Verdict:
    """A task reached a check point and needs one judgment to continue."""
    r = judge.ask(state, {"check": Choice(criteria=dict(options), instructions=question)},
                  site="task_check")
    return _choice(r, "check")


def behave(judge: Judge, situation: str, options: Mapping[str, str]) -> Verdict:
    """Nothing to do. Which autonomous action fits the moment?"""
    r = judge.ask(
        situation,
        {"behaviour": Choice(
            criteria=dict(options),
            instructions="The robot has no task. Choose what a dog would do here.",
        )},
        site="behave",
    )
    return _choice(r, "behaviour")


def _task_criteria(task: str) -> str:
    from dogbrain.tasks import LIBRARY

    spec = LIBRARY.get(task)
    return spec.description if spec and spec.description else task.replace("_", " ")


class TracingJudge:
    """Wraps a judge and records the whole exchange: questions in, answers out.

    Use it to see exactly what JEV was asked and exactly what it returned,
    including the probability mass behind each choice.
    """

    def __init__(self, inner: Judge, *, echo: bool = True) -> None:
        self.inner = inner
        self.echo = echo
        self.log: list[dict[str, Any]] = []

    @property
    def usage(self) -> Any:
        return getattr(self.inner, "usage", None)

    def ask(self, state: Any, questions: Mapping[str, Any], *, site: str) -> Any:
        response = self.inner.ask(state, questions, site=site)
        entry: dict[str, Any] = {"site": site, "state": str(state), "questions": {}, "answers": {}}

        for qid, q in questions.items():
            crit = getattr(q, "criteria", None)
            entry["questions"][qid] = {
                "instructions": getattr(q, "instructions", ""),
                "options": dict(crit) if isinstance(crit, dict) else list(crit or []),
            }
        for qid in questions:
            c = (getattr(response, "choices", None) or {}).get(qid)
            s = (getattr(response, "scores", None) or {}).get(qid)
            n = (getattr(response, "nouls", None) or {}).get(qid)
            if c is not None:
                entry["answers"][qid] = {
                    "type": "choice",
                    "choice": getattr(c, "choice", None),
                    "confidence": _clamp(getattr(c, "confidence", 0.0)),
                    "probabilities": getattr(c, "probabilities", None),
                }
            elif s is not None:
                entry["answers"][qid] = {
                    "type": "score",
                    "score": _clamp(getattr(s, "score", 0.0)),
                    "confidence": _clamp(getattr(s, "confidence", 0.0)),
                    "legend": getattr(s, "legend", None),
                }
            elif n is not None:
                entry["answers"][qid] = {"type": "noul", "noul": getattr(n, "noul", None)}

        self.log.append(entry)
        if self.echo:
            self._print(entry)
        return response

    @staticmethod
    def _print(e: dict[str, Any]) -> None:
        print(f"\n  ┌─ JEV call [{e['site']}]")
        print(f"  │ state: {e['state'][:300]}")
        for qid, q in e["questions"].items():
            opts = q["options"]
            shown = list(opts)[:8] if isinstance(opts, dict) else opts[:8]
            print(f"  │ ask {qid}: {q['instructions'][:110]}")
            print(f"  │     options: {shown}")
            a = e["answers"].get(qid, {})
            if a.get("type") == "choice":
                print(f"  │  -> {a['choice']!r}  conf={a['confidence']:.3f}")
                probs = a.get("probabilities")
                if isinstance(probs, dict) and probs:
                    top = sorted(probs.items(), key=lambda kv: -float(kv[1]))[:4]
                    print("  │     p: " + ", ".join(f"{k}={float(v):.3f}" for k, v in top))
            elif a.get("type") == "score":
                print(f"  │  -> score={a['score']:.3f}  conf={a['confidence']:.3f}")
            elif a.get("type") == "noul":
                print(f"  │  -> noul={a['noul']!r}")
        print("  └─")
