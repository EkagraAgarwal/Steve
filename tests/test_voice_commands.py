"""Unit tests for steve_voice.commands.route_command.

Offline only: fake clients stand in for TypeSafeClient.system_one (both
the SDK 0.7.0 ``.answers`` shape and the legacy ``.choices`` shape). No
network, no key, no hardware.
"""

import dataclasses
import unittest

from steve_voice.commands import (
    ACTION_QUESTION,
    ALLOWLIST,
    CONFIDENCE_THRESHOLD,
    DEFAULT_DISTANCE_M,
    DEFAULT_TURN_DEGREES,
    MAX_DISTANCE_M,
    MAX_TURN_DEGREES,
    REJECT_LABEL,
    STOP,
    VoiceCommand,
    is_local_stop,
    route_command,
)


class _FakeEntry:
    def __init__(self, choice, confidence):
        self.choice = choice
        self.confidence = confidence


class _AnswersResponse:
    """SDK 0.7.0 shape: response.answers[qid].choice/.confidence."""

    def __init__(self, choice, confidence, qid=ACTION_QUESTION):
        self.answers = {qid: _FakeEntry(choice, confidence)}


class _ChoicesResponse:
    """Legacy/fake shape: response.choices[qid].choice/.confidence."""

    def __init__(self, choice, confidence, qid=ACTION_QUESTION):
        self.choices = {qid: _FakeEntry(choice, confidence)}


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def system_one(self, state, questions, **kwargs):
        self.calls.append((state, questions, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


def _route(text, choice, confidence=0.9, threshold=CONFIDENCE_THRESHOLD,
           shape=_AnswersResponse, **kwargs):
    client = FakeClient(shape(choice, confidence))
    return route_command(text, client, confidence_threshold=threshold, **kwargs), client


class RouteCommandTest(unittest.TestCase):
    def test_all_motion_actions_accepted_with_defaults(self):
        expected = {
            "forward": DEFAULT_DISTANCE_M,
            "backward": DEFAULT_DISTANCE_M,
            "strafe_left": DEFAULT_DISTANCE_M,
            "strafe_right": DEFAULT_DISTANCE_M,
            "turn_left": DEFAULT_TURN_DEGREES,
            "turn_right": DEFAULT_TURN_DEGREES,
        }
        for label, magnitude in expected.items():
            with self.subTest(action=label):
                decision, _ = _route("go", label)
                self.assertTrue(decision.accepted)
                self.assertEqual(decision.action, label)
                self.assertEqual(decision.magnitude, magnitude)
                self.assertEqual(decision.confidence, 0.9)

    def test_legacy_choices_shape_also_accepted(self):
        decision, _ = _route("move forward", "forward", shape=_ChoicesResponse)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.action, "forward")
        self.assertEqual(decision.magnitude, DEFAULT_DISTANCE_M)

    def test_to_dict_keys_and_immutability(self):
        decision, _ = _route("turn left 45 degrees", "turn_left", confidence=0.95)
        self.assertEqual(
            decision.to_dict(),
            {
                "accepted": True,
                "action": "turn_left",
                "confidence": 0.95,
                "magnitude": 45.0,
                "reason": "routed",
                "source_text": "turn left 45 degrees",
            },
        )
        self.assertIsInstance(decision, VoiceCommand)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            decision.accepted = False  # type: ignore[misc]

    def test_explicit_magnitudes_and_units(self):
        cases = [
            ("move forward 1.5 meters", "forward", 1.5),
            ("strafe left 50cm", "strafe_left", 0.5),
            ("back up 250mm", "backward", 0.25),
            ("turn right 90 degrees", "turn_right", 90.0),
            ("turn left 45deg", "turn_left", 45.0),
        ]
        for text, label, magnitude in cases:
            with self.subTest(text=text):
                decision, _ = _route(text, label)
                self.assertTrue(decision.accepted, decision.reason)
                self.assertEqual(decision.magnitude, magnitude)

    def test_cap_boundaries_accepted(self):
        decision, _ = _route("move forward 2m", "forward")
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.magnitude, MAX_DISTANCE_M)
        decision, _ = _route("turn right 90", "turn_right")
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.magnitude, MAX_TURN_DEGREES)

    def test_over_cap_rejected_never_clamped(self):
        for text, label in [
            ("move forward 2.5 meters", "forward"),
            ("move forward 3m", "forward"),
            ("strafe right 250cm", "strafe_right"),
            ("turn left 100 degrees", "turn_left"),
            ("turn right 180", "turn_right"),
        ]:
            with self.subTest(text=text):
                decision, _ = _route(text, label)
                self.assertFalse(decision.accepted)
                self.assertIsNone(decision.action)
                self.assertIsNone(decision.magnitude)
                self.assertIn("cap", decision.reason)

    def test_word_numbers(self):
        decision, _ = _route("strafe right half a meter", "strafe_right")
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.magnitude, 0.5)
        decision, _ = _route("turn ninety degrees", "turn_left")
        self.assertTrue(decision.accepted, decision.reason)
        self.assertEqual(decision.magnitude, 90.0)

    def test_incompatible_units_rejected(self):
        for text, label in [
            ("turn left 2 meters", "turn_left"),
            ("turn right 50cm", "turn_right"),
            ("move forward 30 degrees", "forward"),
            ("strafe left 45 deg", "strafe_left"),
            ("move forward 3 feet", "forward"),
        ]:
            with self.subTest(text=text):
                decision, _ = _route(text, label)
                self.assertFalse(decision.accepted, text)
                self.assertIsNone(decision.action)

    def test_non_positive_magnitude_rejected(self):
        decision, _ = _route("move forward 0 meters", "forward")
        self.assertFalse(decision.accepted)
        decision, _ = _route("turn left -10 degrees", "turn_left")
        self.assertFalse(decision.accepted)

    def test_multi_action_rejected_despite_confident_label(self):
        for text, label in [
            ("move forward then turn left", "forward"),
            ("move forward and turn left", "forward"),
            ("strafe left and stop", "strafe_left"),
            ("move 1 meter and 2 meters", "forward"),
        ]:
            with self.subTest(text=text):
                decision, _ = _route(text, label, confidence=0.99)
                self.assertFalse(decision.accepted, text)
                self.assertIsNone(decision.action)

    def test_stop_accepted_distinct_from_reject(self):
        decision, _ = _route("stop", STOP)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.action, STOP)
        self.assertIsNone(decision.magnitude)
        rejected, _ = _route("do a backflip", REJECT_LABEL, confidence=0.99)
        self.assertFalse(rejected.accepted)
        self.assertIsNone(rejected.action)
        self.assertNotEqual(decision.action, rejected.action)

    def test_reject_label_high_confidence_still_rejects(self):
        decision, _ = _route("sing a song", REJECT_LABEL, confidence=0.99)
        self.assertFalse(decision.accepted)
        self.assertIsNone(decision.action)

    def test_unknown_label_rejected(self):
        decision, _ = _route("fly up", "fly", confidence=0.99)
        self.assertFalse(decision.accepted)
        self.assertIsNone(decision.action)

    def test_confidence_threshold_boundary(self):
        decision, _ = _route("move forward", "forward", confidence=0.7)
        self.assertTrue(decision.accepted)
        decision, _ = _route("move forward", "forward", confidence=0.69)
        self.assertFalse(decision.accepted)
        self.assertIsNone(decision.action)

    def test_malformed_confidence_rejected_not_trusted(self):
        for bad in ("high", float("inf"), float("-inf"), float("nan"), None):
            with self.subTest(confidence=bad):
                decision, _ = _route("move forward", "forward", confidence=bad)
                self.assertFalse(decision.accepted)
                self.assertIsNone(decision.action)

    def test_empty_text_makes_no_call(self):
        client = FakeClient(_AnswersResponse("forward", 0.99))
        for text in ("", "   ", None, 123):
            with self.subTest(text=text):
                decision = route_command(text, client)  # type: ignore[arg-type]
                self.assertFalse(decision.accepted)
                self.assertIsNone(decision.action)
        self.assertEqual(client.calls, [])

    def test_source_text_stripped_preserved(self):
        decision, _ = _route("  move forward  ", "forward")
        self.assertEqual(decision.source_text, "move forward")

    def test_client_error_rejects_without_raising(self):
        client = FakeClient(error=RuntimeError("boom"))
        decision = route_command("move forward", client)
        self.assertFalse(decision.accepted)
        self.assertIsNone(decision.action)
        self.assertIn("RuntimeError", decision.reason)

    def test_malformed_responses_reject_without_raising(self):
        for bad in (None, object(), {}, {"answers": None}, _AnswersResponse("x", 0.9, qid="nope")):
            with self.subTest(response=bad):
                client = FakeClient(bad)
                decision = route_command("move forward", client)
                self.assertFalse(decision.accepted)
                self.assertIsNone(decision.action)

    def test_threshold_validation(self):
        client = FakeClient(_AnswersResponse("forward", 0.9))
        for bad in (-0.1, 1.5, float("nan"), float("inf"), "high", True):
            with self.subTest(threshold=bad):
                with self.assertRaises(ValueError):
                    route_command("go", client, confidence_threshold=bad)  # type: ignore[arg-type]
        self.assertEqual(client.calls, [])

    def test_exactly_one_call_with_action_question(self):
        decision, client = _route("move forward", "forward")
        self.assertTrue(decision.accepted)
        self.assertEqual(len(client.calls), 1)
        state, questions, kwargs = client.calls[0]
        self.assertEqual(state, "move forward")
        self.assertEqual(set(questions), {ACTION_QUESTION})
        self.assertEqual(kwargs, {})
        criteria = questions[ACTION_QUESTION].criteria
        self.assertTrue(ALLOWLIST <= set(criteria))
        self.assertIn(REJECT_LABEL, criteria)

    def test_timeout_forwarded_to_client(self):
        decision, client = _route("move forward", "forward", timeout=5.0)
        self.assertTrue(decision.accepted)
        _, _, kwargs = client.calls[0]
        self.assertEqual(kwargs, {"timeout": 5.0})

    def test_is_local_stop(self):
        for text in ("stop", "STOP!", "halt", "freeze", "cancel that", "e-stop"):
            with self.subTest(text=text):
                self.assertTrue(is_local_stop(text))
        for text in ("move forward", "turn left 30 degrees", "strafe right"):
            with self.subTest(text=text):
                self.assertFalse(is_local_stop(text))


if __name__ == "__main__":
    unittest.main()
