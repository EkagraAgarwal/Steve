"""Unit tests for the Phase 1 JEV text router.

No network, no API key, no hardware: a fake client stands in for
TypeSafeClient.system_one.
"""

import unittest

from steve_router.commands import Action, SpeedMode, to_motion
from steve_router.router import COMMAND_QUESTION, SPEED_QUESTION, route_text


class _FakeChoice:
    def __init__(self, choice, confidence):
        self.choice = choice
        self.confidence = confidence


class _FakeResponse:
    def __init__(self, command, command_confidence, speed="normal", speed_confidence=0.9):
        self.choices = {
            COMMAND_QUESTION: _FakeChoice(command, command_confidence),
            SPEED_QUESTION: _FakeChoice(speed, speed_confidence),
        }


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def system_one(self, state, questions):
        self.calls.append((state, questions))
        return self.response


def _route(command, command_confidence=0.9, speed="normal", speed_confidence=0.9,
           text="go", threshold=0.7):
    client = FakeClient(
        _FakeResponse(command, command_confidence, speed, speed_confidence)
    )
    result = route_text(text, client, confidence_threshold=threshold)
    return result, client


class RouterTest(unittest.TestCase):
    def test_all_actions_map_to_motion_at_normal(self):
        expected = {
            "move_forward": (0.5, 0.0, 0.0),
            "move_backward": (-0.5, 0.0, 0.0),
            "strafe_left": (0.0, 0.5, 0.0),
            "strafe_right": (0.0, -0.5, 0.0),
            "turn_left": (0.0, 0.0, 0.8),
            "turn_right": (0.0, 0.0, -0.8),
            "stop": (0.0, 0.0, 0.0),
        }
        for label, motion in expected.items():
            with self.subTest(command=label):
                result, _ = _route(label)
                self.assertTrue(result.accepted)
                self.assertEqual(result.action, Action(label))
                self.assertEqual(to_motion(result), motion)

    def test_boost_and_slow_bounds(self):
        result, _ = _route("move_forward", speed="boost", speed_confidence=0.95)
        self.assertEqual(result.speed_mode, SpeedMode.BOOST)
        self.assertEqual(to_motion(result), (1.0, 0.0, 0.0))
        result, _ = _route("move_forward", speed="slow", speed_confidence=0.95)
        self.assertEqual(result.speed_mode, SpeedMode.SLOW)
        self.assertEqual(to_motion(result), (0.25, 0.0, 0.0))
        result, _ = _route(
            "turn_left", speed="boost", speed_confidence=0.95
        )
        self.assertEqual(to_motion(result), (0.0, 0.0, 1.6))

    def test_explicit_stop_is_accepted_with_zero_motion(self):
        result, _ = _route("stop")
        self.assertTrue(result.accepted)
        self.assertEqual(result.action, Action.STOP)
        self.assertEqual(to_motion(result), (0.0, 0.0, 0.0))

    def test_reject_label_yields_no_action_or_motion(self):
        result, _ = _route("reject", command_confidence=0.99)
        self.assertFalse(result.accepted)
        self.assertIsNone(result.action)
        self.assertIsNone(to_motion(result))

    def test_unknown_label_is_rejected(self):
        result, _ = _route("fly_up", command_confidence=0.99)
        self.assertFalse(result.accepted)
        self.assertIsNone(result.action)
        self.assertIsNone(to_motion(result))

    def test_low_command_confidence_is_rejected(self):
        result, _ = _route("move_forward", command_confidence=0.2)
        self.assertFalse(result.accepted)
        self.assertIsNone(result.action)
        self.assertIsNone(to_motion(result))

    def test_empty_text_makes_no_api_call(self):
        client = FakeClient(_FakeResponse("move_forward", 0.99))
        result = route_text("   ", client)
        self.assertFalse(result.accepted)
        self.assertIsNone(result.action)
        self.assertEqual(client.calls, [])

    def test_speed_low_confidence_defaults_to_normal(self):
        result, _ = _route(
            "move_forward", speed="boost", speed_confidence=0.1
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.action, Action.MOVE_FORWARD)
        self.assertEqual(result.speed_mode, SpeedMode.NORMAL)
        self.assertEqual(to_motion(result), (0.5, 0.0, 0.0))

    def test_unknown_speed_defaults_to_normal(self):
        result, _ = _route("move_forward", speed="warp", speed_confidence=0.99)
        self.assertTrue(result.accepted)
        self.assertEqual(result.speed_mode, SpeedMode.NORMAL)

    def test_threshold_validation(self):
        client = FakeClient(_FakeResponse("move_forward", 0.9))
        for bad in (-0.1, 1.5, float("nan"), "high"):
            with self.subTest(threshold=bad):
                with self.assertRaises(ValueError):
                    route_text("go", client, confidence_threshold=bad)  # type: ignore[arg-type]
        self.assertEqual(client.calls, [])

    def test_exactly_one_sdk_call_with_two_question_keys(self):
        result, client = _route("move_forward")
        self.assertTrue(result.accepted)
        self.assertEqual(len(client.calls), 1)
        state, questions = client.calls[0]
        self.assertEqual(state, "go")
        self.assertEqual(set(questions), {COMMAND_QUESTION, SPEED_QUESTION})

    def test_malformed_confidence_is_rejected_not_trusted(self):
        for bad in ("high", float("inf"), float("-inf")):
            with self.subTest(confidence=bad):
                client = FakeClient(_FakeResponse("move_forward", bad))
                result = route_text("go", client)
                self.assertFalse(result.accepted)
                self.assertIsNone(result.action)

    def test_source_text_preserved_stripped(self):
        result, _ = _route("move_forward", text="  move forward  ")
        self.assertEqual(result.source_text, "move forward")


if __name__ == "__main__":
    unittest.main()
