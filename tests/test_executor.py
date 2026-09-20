"""Executor tests: the brain's intents must become correct MCP calls."""

from __future__ import annotations

import math

from dogbrain import actions as A
from dogbrain.executor import DimosExecutor
from dogbrain.scenarios import _s
from dogbrain.state import Seen


def ex():
    return DimosExecutor(dry_run=True)


def state_with(distance, bearing, eid="ball", label="sports ball"):
    return _s(0, seen=[Seen(eid, label, distance, bearing)])


def test_gesture_maps_to_sport_command():
    c = ex().plan_call(A.gesture("WiggleHips"))
    assert c.tool == "execute_sport_command" and c.args == {"command_name": "WiggleHips"}


def test_sit_maps_to_sit_sport_command():
    assert ex().plan_call(A.sit()).args == {"command_name": "Sit"}


def test_speak_is_non_blocking():
    c = ex().plan_call(A.speak("hello"))
    assert c.tool == "speak" and c.args["blocking"] is False


def test_halt_maps_to_stop_navigation():
    assert ex().plan_call(A.halt()).tool == "stop_navigation"


def test_navigate_uses_text_query():
    c = ex().plan_call(A.navigate("kitchen"))
    assert c.tool == "navigate_with_text" and c.args == {"query": "kitchen"}


def test_approach_stops_short_of_the_target():
    st = state_with(3.0, 0.0)
    c = ex().plan_call(A.approach("ball", stop_at=1.0), st)
    assert c.tool == "move_to" and c.args["relative"] is True
    assert math.isclose(c.args["x"], 2.0, abs_tol=0.01), "3 m away, stop 1 m short -> travel 2 m"
    assert math.isclose(c.args["y"], 0.0, abs_tol=0.01)


def test_approach_uses_bearing_for_lateral_offset():
    c = ex().plan_call(A.approach("ball", stop_at=1.0), state_with(3.0, 90.0))
    assert math.isclose(c.args["x"], 0.0, abs_tol=0.01)
    assert math.isclose(c.args["y"], 2.0, abs_tol=0.01), "+bearing is left = +y"


def test_approach_already_close_does_not_reverse():
    c = ex().plan_call(A.approach("ball", stop_at=1.0), state_with(0.4, 0.0))
    assert c.args["x"] >= 0.0, "never back up when already inside stop_at"


def test_face_turns_without_translating():
    c = ex().plan_call(A.face("ball"), state_with(2.0, 35.0))
    assert c.args["x"] == 0.0 and c.args["y"] == 0.0 and c.args["degrees"] == 35.0


def test_approach_invisible_target_sends_nothing():
    assert ex().plan_call(A.approach("ghost"), state_with(2.0, 0.0)) is None


def test_search_without_position_explores():
    assert ex().plan_call(A.search(None)).tool == "begin_exploration"


def test_search_with_position_moves_there():
    c = ex().plan_call(A.search((3.0, 0.0)))
    assert c.tool == "move_to" and math.isclose(c.args["x"], 2.0, abs_tol=0.01)


def test_escalate_goes_to_the_llm_agent():
    c = ex().plan_call(A.escalate("do a backflip and sing", "multi_step"))
    assert c.tool == "agent_send" and "backflip" in c.args["message"]


def test_stats_count_executed_calls():
    e = ex()
    st = state_with(2.0, 0.0)
    e.execute_all([A.gesture("Hello"), A.approach("ball", 1.0), A.halt()], st)
    assert e.stats.calls == 3 and e.stats.failures == 0
    assert e.stats.by_tool["execute_sport_command"] == 1
