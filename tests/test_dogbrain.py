"""DogBrain tests. Offline: FakeJudge keeps gate behaviour deterministic."""

from __future__ import annotations

import pytest

from dogbrain.brain import DogBrain, Gate
from dogbrain.fake import FakeJudge
from dogbrain.plan import PlanError, parse, single
from dogbrain.scenarios import ALL, BALL, OWNER, _s
from dogbrain.state import Seen


def brain(confidence=0.9, threshold=0.7, **kw):
    return DogBrain(FakeJudge(confidence=confidence, **kw), gate=Gate(threshold=threshold))


# -- plan validation ---------------------------------------------------------

def test_unknown_task_rejected():
    with pytest.raises(PlanError, match="unknown task"):
        parse({"steps": [{"do": "teleport", "place": "kitchen"}]})


def test_missing_param_rejected():
    with pytest.raises(PlanError, match="missing required"):
        parse({"steps": [{"do": "go_to"}]})


def test_forward_variable_reference_rejected():
    with pytest.raises(PlanError, match="no earlier step saved"):
        parse({"steps": [{"do": "follow", "target": "$ball"}]})


def test_variable_from_earlier_step_accepted():
    plan = parse({"goal": "g", "steps": [
        {"do": "find", "what": "ball", "save_as": "ball"},
        {"do": "follow", "target": "$ball"},
    ]})
    assert len(plan.steps) == 2 and plan.steps[1].args["target"] == "$ball"


def test_bad_on_fail_rejected():
    with pytest.raises(PlanError, match="on_fail"):
        parse({"steps": [{"do": "patrol", "duration_ticks": 5}], "on_fail": "explode"})


# -- the gate ----------------------------------------------------------------

def test_high_confidence_stays_local():
    b = brain(confidence=0.95)
    b.tick(ALL["follow"][0])
    res = b.tick(ALL["follow"][1])
    assert not res.escalated
    assert b.memory.plan is not None


def test_low_confidence_escalates_to_llm():
    b = brain(confidence=0.30)
    b.tick(ALL["follow"][0])
    res = b.tick(ALL["follow"][1])
    assert res.escalated
    assert any(a.kind == "escalate" for a in res.actions)


def test_multi_step_always_escalates_even_when_confident():
    b = brain(confidence=0.99)
    b.tick(ALL["ambiguous"][0])
    res = b.tick(ALL["ambiguous"][1])
    assert res.escalated, "multi-step must go to the LLM, never the fast path"


# -- safety ------------------------------------------------------------------

def test_safety_reflex_overrides_active_task():
    b = brain()
    b.tick(ALL["safety"][0])
    res = b.tick(ALL["safety"][1])
    assert [a.kind for a in res.actions] == ["halt"]
    assert b.memory.plan is None, "reflex clears the plan"


def test_safety_runs_before_any_judgment():
    judge = FakeJudge()
    b = DogBrain(judge, gate=Gate())
    b.tick(_s(0, seen=[Seen("wall_1", "wall", 0.2, 0)]))
    assert judge.calls == [], "no judgment call may happen once a reflex fires"


def test_stop_word_needs_no_judgment():
    b = brain()
    b.tick(ALL["follow"][0])
    b.tick(ALL["follow"][1])
    before = len(b.judge.calls)
    res = b.tick(_s(9, seen=[BALL(2.0)], heard="stop"))
    assert [a.kind for a in res.actions] == ["halt"]
    assert len(b.judge.calls) == before, "stop is handled in code"


# -- following ---------------------------------------------------------------

def test_follow_approaches_then_faces_then_searches():
    b = brain()
    kinds = []
    for st in ALL["follow"]:
        kinds.append([a.kind for a in b.tick(st).actions])
    assert "approach" in kinds[2][0]
    assert kinds[3] == ["face"], "close enough -> face, not approach"
    assert kinds[4] == ["search"], "lost -> search last known position"
    assert kinds[7] == ["halt"], "owner stop ends it"


def test_follow_fails_after_lost_limit():
    b = brain()
    b.tick(ALL["follow"][0])
    b.tick(ALL["follow"][1])
    res = None
    for t in range(2, 40):
        res = b.tick(_s(t, seen=[]))
        if res.escalated:
            break
    assert res is not None and res.escalated, "a permanently lost target must be reported"


# -- memory ------------------------------------------------------------------

def test_entity_remembered_after_leaving_view():
    b = brain()
    b.tick(ALL["follow"][0])
    b.tick(_s(1, seen=[]))
    assert "sports_ball_3" in b.memory.entities


def test_understand_runs_once_per_entity():
    b = brain()
    b.tick(ALL["follow"][0])
    first = sum(1 for c in b.judge.calls if c[0] == "understand")
    b.tick(ALL["follow"][0])
    second = sum(1 for c in b.judge.calls if c[0] == "understand")
    assert first == 2 and second == 2, "understanding is not re-asked every tick"


# -- LLM-proposed plans ------------------------------------------------------

def test_set_plan_accepts_valid_and_reports_steps():
    b = brain()
    msg = b.set_plan({"goal": "x", "steps": [
        {"do": "go_to", "place": "kitchen"},
        {"do": "find", "what": "ball", "save_as": "ball"},
        {"do": "follow", "target": "$ball"},
    ]})
    assert msg.startswith("accepted") and b.plan_status()["of"] == 3


def test_set_plan_rejects_with_reason():
    b = brain()
    assert b.set_plan({"steps": [{"do": "fly", "to": "moon"}]}).startswith("rejected")
    assert b.plan_status() == {"active": False}
