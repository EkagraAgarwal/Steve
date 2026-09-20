"""A deterministic stand-in for JEV, so the brain can be tested offline.

It answers with simple keyword rules and a fixed confidence, which makes
gate behaviour reproducible in tests. It is NOT a model -- use JevJudge for
anything you intend to believe.
"""

from __future__ import annotations

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


class FakeJudge:
    """Keyword rules. `confidence` lets a test drive the gate either way."""

    def __init__(self, confidence: float = 0.9, task_override: str | None = None) -> None:
        self.confidence = confidence
        self.task_override = task_override
        self.calls: list[tuple[str, Any, list[str]]] = []

    def ask(self, state: Any, questions: Mapping[str, Any], *, site: str) -> Any:
        self.calls.append((site, state, list(questions)))
        text = str(state).lower()
        resp = _Resp()
        for qid, q in questions.items():
            crit = getattr(q, "criteria", {}) or {}
            options = list(crit) if isinstance(crit, dict) else list(crit)
            if qid == "command_kind":
                kind = "single_task"
                if any(w in text for w in ("and then", ", then", " and ")):
                    kind = "multi_step"
                elif text.strip().endswith("?"):
                    kind = "conversation"
                resp.choices[qid] = _Ans(kind, self.confidence)
            elif qid == "task":
                pick = self.task_override or _guess_task(text, options)
                resp.choices[qid] = _Ans(pick, self.confidence)
            elif qid == "target":
                pick = next((o for o in options if o.split("_")[0] in text), options[0] if options else None)
                resp.choices[qid] = _Ans(pick, self.confidence)
            elif qid == "meaning":
                pick = "toy" if "ball" in text else ("person" if "person" in text else "unknown")
                resp.choices[qid] = _Ans(pick if pick in options else options[0], self.confidence)
            elif qid == "interest":
                resp.scores[qid] = _Ans(score=0.8 if "ball" in text else 0.2,
                                        confidence=self.confidence)
            elif qid == "check":
                yes = "ball" in text or "person" in text
                resp.choices[qid] = _Ans("yes" if yes else "no", self.confidence)
            elif qid == "behaviour":
                pick = "patrol" if "quiet" in text else "look_around"
                resp.choices[qid] = _Ans(pick if pick in options else options[0], self.confidence)
            else:
                resp.choices[qid] = _Ans(options[0] if options else None, self.confidence)
        return resp


def _guess_task(text: str, options: list[str]) -> str:
    for word, task in (
        ("follow", "follow"), ("find", "find"), ("look for", "find"),
        ("go to", "go_to"), ("watch", "watch"), ("stay", "stay"),
        ("sit", "stay"), ("greet", "greet"), ("say hello", "greet"),
        ("patrol", "patrol"), ("come back", "return_to_owner"),
    ):
        if word in text and task in options:
            return task
    return options[0] if options else "follow"
