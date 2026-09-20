"""Unit tests for console push-to-talk voice input (Deepgram Flux).

Stdlib only: no network, no API key, no mic, no hardware. Optional
dependencies (sounddevice, websocket-client, typesafe-sdk) and the
peer-owned steve_voice.commands module are stubbed or monkeypatched.
Fake websocket + fake sounddevice prove the threaded runner streams
frames before release and enforces the exact Flux gate.
"""

import collections
import io
import json
import sys
import threading
import time
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

from steve_voice import cli as voice_cli
from steve_voice.deepgram import (
    CHANNELS,
    ENC,
    EOT_THRESHOLD,
    FRAME_BYTES,
    MODEL,
    SAMPLE_RATE,
    FinalizedUtterance,
    FluxConfig,
    TurnFinalizer,
    build_flux_url,
    classify_message,
    force_end_turn_message,
    run_push_to_talk,
    split_frames,
    wait_for_utterance,
)


def _valid(text="move forward", req="r1", turn=1, trigger="manual"):
    return {
        "type": "TurnInfo",
        "event": "EndOfTurn",
        "transcript": text,
        "request_id": req,
        "turn_index": turn,
        "trigger": trigger,
    }


def _partial(text="move for"):
    return {
        "type": "Results",
        "is_final": False,
        "channel": {"alternatives": [{"transcript": text}]},
    }


def _results_final(text="move forward"):
    return {
        "type": "Results",
        "is_final": True,
        "channel": {"alternatives": [{"transcript": text}]},
    }


def _old_nested_shape(text="move forward"):
    # Pre-fix shape: nested transcript.text + session/turn ids. Must NOT route.
    return {
        "type": "TurnInfo",
        "event": "EndOfTurn",
        "turn": {"session_id": "s1", "turn_id": "t1"},
        "transcript": {"text": text, "is_final": True},
        "trigger": "manual",
    }


class FluxContractTest(unittest.TestCase):
    def test_flux_url_params(self):
        url = build_flux_url(FluxConfig(api_key="k"))
        self.assertTrue(url.startswith("wss://api.deepgram.com/v2/listen?"))
        self.assertIn("model=" + MODEL, url)
        self.assertIn("encoding=" + ENC, url)
        self.assertIn("sample_rate=%d" % SAMPLE_RATE, url)
        self.assertIn("eot_threshold=%s" % EOT_THRESHOLD, url)

    def test_force_end_turn_schema(self):
        self.assertEqual(force_end_turn_message(), {"type": "ForceEndTurn"})

    def test_frame_geometry_80ms_mono_pcm16(self):
        self.assertEqual(SAMPLE_RATE, 16000)
        self.assertEqual(CHANNELS, 1)
        self.assertEqual(FRAME_BYTES, 2560)  # 1280 int16 samples
        pcm = b"\x00" * (FRAME_BYTES * 2 + 100)
        self.assertEqual(len(split_frames(pcm)), 2)  # trailing partial dropped

    def test_exact_gate_valid(self):
        self.assertEqual(classify_message(_valid()), "end_of_turn")

    def test_gate_rejects_nested_text_no_guessing(self):
        self.assertNotEqual(classify_message(_old_nested_shape()), "end_of_turn")

    def test_gate_rejects_missing_ids(self):
        msg = _valid()
        del msg["request_id"]
        self.assertNotEqual(classify_message(msg), "end_of_turn")
        msg2 = _valid()
        del msg2["turn_index"]
        self.assertNotEqual(classify_message(msg2), "end_of_turn")

    def test_gate_rejects_non_manual_trigger(self):
        for trigger in ("natural", "timeout", "eager", ""):
            self.assertNotEqual(
                classify_message(_valid(trigger=trigger)), "end_of_turn"
            )

    def test_gate_rejects_wrong_type_event(self):
        msg = _valid()
        msg["type"] = "Update"
        self.assertNotEqual(classify_message(msg), "end_of_turn")
        msg2 = _valid()
        msg2["event"] = "Update"
        self.assertNotEqual(classify_message(msg2), "end_of_turn")

    def test_update_eager_start_never_route(self):
        for mtype in ("Update", "Eager", "Start"):
            self.assertNotEqual(
                classify_message({"type": mtype}), "end_of_turn"
            )
        for event in ("Update", "Eager", "Start"):
            self.assertNotEqual(
                classify_message({"type": "TurnInfo", "event": event}),
                "end_of_turn",
            )

    def test_error_and_no_active_warning_classify_abort(self):
        self.assertEqual(classify_message({"type": "Error"}), "error")
        self.assertEqual(
            classify_message(
                {"type": "Warning", "warning": "FORCE_END_TURN_NO_ACTIVE_TURN"}
            ),
            "warning_no_active",
        )


class TurnFinalizerTest(unittest.TestCase):
    def test_end_of_turn_before_release_aborts(self):
        fin = TurnFinalizer()
        self.assertIsNone(fin.observe(_valid()))
        self.assertTrue(fin.failed)
        fin.release()
        self.assertIsNone(fin.observe(_valid(req="r2", turn=2)))
        self.assertIsNone(fin.finalize())

    def test_nonempty_end_of_turn_after_release_finalizes(self):
        fin = TurnFinalizer()
        fin.release()
        utterance = fin.observe(_valid("go left", "s", 7))
        self.assertIsNotNone(utterance)
        assert utterance is not None
        self.assertEqual(utterance.text, "go left")
        self.assertEqual(utterance.request_id, "s")
        self.assertEqual(utterance.turn_index, 7)
        self.assertEqual(fin.finalize(), utterance)

    def test_empty_end_of_turn_ignored(self):
        fin = TurnFinalizer()
        fin.release()
        self.assertIsNone(fin.observe(_valid("   ")))
        self.assertIsNone(fin.finalize())

    def test_partials_never_route(self):
        fin = TurnFinalizer()
        self.assertEqual(classify_message(_partial()), "partial")
        self.assertIsNone(fin.observe(_partial()))
        fin.release()
        self.assertIsNone(fin.observe(_partial()))
        self.assertIsNone(fin.finalize())

    def test_results_final_aborted_never_eager(self):
        fin = TurnFinalizer()
        self.assertEqual(classify_message(_results_final()), "final")
        self.assertIsNone(fin.observe(_results_final()))
        fin.release()
        self.assertIsNone(fin.observe(_results_final()))
        self.assertIsNone(fin.finalize())

    def test_natural_trigger_never_routes(self):
        fin = TurnFinalizer()
        fin.release()
        self.assertIsNone(fin.observe(_valid(trigger="natural")))
        self.assertIsNone(fin.finalize())

    def test_missing_ids_fail_closed(self):
        fin = TurnFinalizer()
        fin.release()
        bad = _valid()
        del bad["request_id"]
        self.assertIsNone(fin.observe(bad))
        bad2 = _valid()
        del bad2["turn_index"]
        self.assertIsNone(fin.observe(bad2))
        self.assertIsNone(fin.finalize())

    def test_nested_shape_never_routes_no_guessing(self):
        fin = TurnFinalizer()
        fin.release()
        self.assertIsNone(fin.observe(_old_nested_shape()))
        self.assertIsNone(fin.finalize())

    def test_dedup_request_turn(self):
        fin = TurnFinalizer()
        fin.release()
        first = fin.observe(_valid())
        self.assertIsNotNone(first)
        self.assertIsNone(fin.observe(_valid()))  # redelivery
        self.assertEqual(fin.finalize(), first)

    def test_one_pending_utterance_extras_dropped(self):
        fin = TurnFinalizer()
        fin.release()
        first = fin.observe(_valid("go left", "s", 1))
        self.assertIsNotNone(first)
        self.assertIsNone(fin.observe(_valid("go right", "s", 2)))
        self.assertEqual(fin.finalize(), first)

    def test_timeout_fails_closed(self):
        fin = TurnFinalizer()
        fin.release()
        fin.on_timeout()
        self.assertIsNone(fin.observe(_valid()))
        self.assertIsNone(fin.finalize())

    def test_error_fails_closed(self):
        fin = TurnFinalizer()
        fin.release()
        fin.on_error(ValueError("socket"))
        self.assertIsNone(fin.observe(_valid()))
        self.assertIsNone(fin.finalize())

    def test_server_error_and_warning_abort(self):
        fin = TurnFinalizer()
        fin.release()
        self.assertIsNone(fin.observe({"type": "Error", "message": "boom"}))
        self.assertTrue(fin.failed)
        fin2 = TurnFinalizer()
        fin2.release()
        self.assertIsNone(
            fin2.observe(
                {"type": "Warning", "warning": "FORCE_END_TURN_NO_ACTIVE_TURN"}
            )
        )
        self.assertTrue(fin2.failed)
        self.assertIsNone(fin2.finalize())

    def test_no_replay_on_reconnect(self):
        fin = TurnFinalizer()
        fin.release()
        utterance = fin.observe(_valid())
        self.assertEqual(fin.finalize(), utterance)
        fin.reconnect()  # fresh socket, same dedup memory
        self.assertIsNone(fin.observe(_valid()))  # redelivery, no replay
        fin.release()
        self.assertIsNone(fin.observe(_valid()))
        self.assertIsNone(fin.finalize())

    def test_wait_for_utterance_helper(self):
        fin = TurnFinalizer()
        fin.release()
        utterance = wait_for_utterance(
            [_partial(), _valid("stop", "s", 3)], fin, timeout=5.0
        )
        self.assertIsNotNone(utterance)
        assert utterance is not None
        self.assertEqual(utterance.text, "stop")

    def test_utterance_to_dict(self):
        utterance = FinalizedUtterance(text="stop", request_id="s", turn_index=9)
        self.assertEqual(
            utterance.to_dict(),
            {
                "transcript": "stop",
                "finalized": True,
                "request_id": "s",
                "turn_index": 9,
            },
        )


class FakeWebsocket:
    """Thread-safe fake Flux socket: records sends, serves scripted recvs."""

    def __init__(self, early=None, late=None, fail_send=False):
        self._lock = threading.Lock()
        self.sent_binary = []
        self.sent_text = []
        self.order = []  # ("binary", n) / ("text", payload)
        self.early = collections.deque(early or [])
        self.late = collections.deque(late or [])
        self.force_sent = threading.Event()
        self.fail_send = fail_send
        self.closed = False

    def send_binary(self, data):
        with self._lock:
            if self.fail_send:
                raise ConnectionError("fake sender failure")
            self.sent_binary.append(bytes(data))
            self.order.append(("binary", len(self.sent_binary)))
        time.sleep(0.001)

    def send(self, text):
        with self._lock:
            self.sent_text.append(text)
            self.order.append(("text", text))
            try:
                payload = json.loads(text)
            except Exception:
                payload = {}
            if payload.get("type") == "ForceEndTurn":
                self.force_sent.set()

    def recv(self):
        with self._lock:
            if self.early:
                item = self.early.popleft()
                return self._coerce(item)
            if self.force_sent.is_set() and self.late:
                item = self.late.popleft()
                return self._coerce(item)
        time.sleep(0.005)
        return None

    def _coerce(self, item):
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


def _make_sd(pcm_bytes):
    class FakeStream:
        def __init__(self, samplerate, channels, dtype, blocksize, callback):
            self._cb = callback

        def start(self):
            # Synchronous callback: frames queued before release.
            self._cb(bytes(pcm_bytes), None, None, None)

        def stop(self):
            pass

        def close(self):
            pass

    return types.SimpleNamespace(InputStream=FakeStream)


def _run_ptt(fake_ws, pcm_bytes, timeout=1.0, sink=None, fail_send=False):
    fake_ws.fail_send = fail_send
    sd = _make_sd(pcm_bytes)
    calls = {"n": 0}

    def fake_input(*args):
        calls["n"] += 1
        if calls["n"] == 1:
            return ""
        # Release: wait until at least one frame was sent BEFORE release.
        deadline = time.monotonic() + 2.0
        while not fake_ws.sent_binary and time.monotonic() < deadline:
            time.sleep(0.005)
        return ""

    return run_push_to_talk(
        api_key="test-key",
        prompt=True,
        timeout=timeout,
        sink=sink,
        _connect=lambda url, headers, t: fake_ws,
        _sd=sd,
        _input=fake_input,
        _tail_frames=1,
        _tail_delay=0,
    )


class RunnerThreadingTest(unittest.TestCase):
    def test_frames_sent_before_release_and_drain_precedes_force(self):
        pcm = b"\x01\x02" * FRAME_BYTES  # 2 frames
        late = [json.dumps(_valid("move forward", "r1", 1))]
        ws = FakeWebsocket(early=[], late=late)
        utterance = _run_ptt(ws, pcm, timeout=1.0)
        self.assertIsNotNone(utterance)
        assert utterance is not None
        self.assertEqual(utterance.text, "move forward")
        # Frames streamed before release (sender ran during capture).
        self.assertGreaterEqual(len(ws.sent_binary), 2)
        # Drain precedes ForceEndTurn: every binary comes before text.
        self.assertEqual(len(ws.sent_text), 1)
        binaries = [i for i, e in enumerate(ws.order) if e[0] == "binary"]
        texts = [i for i, e in enumerate(ws.order) if e[0] == "text"]
        self.assertTrue(binaries and texts)
        self.assertLess(max(binaries), min(texts))
        payload = json.loads(ws.sent_text[0])
        self.assertEqual(payload, {"type": "ForceEndTurn"})

    def test_real_schema_routes_once_dedup(self):
        pcm = b"\x03" * FRAME_BYTES
        msg = json.dumps(_valid("stop", "req-A", 5))
        ws = FakeWebsocket(early=[], late=[msg, msg])  # redelivery
        seen = []
        utterance = _run_ptt(ws, pcm, timeout=1.0, sink=seen.append)
        self.assertIsNotNone(utterance)
        assert utterance is not None
        self.assertEqual(len(seen), 1)
        self.assertEqual(utterance.request_id, "req-A")
        self.assertEqual(utterance.turn_index, 5)

    def test_early_end_of_turn_aborts(self):
        pcm = b"\x04" * FRAME_BYTES
        ws = FakeWebsocket(
            early=[json.dumps(_valid("early", "r1", 1))],
            late=[json.dumps(_valid("late", "r1", 2))],
        )
        self.assertIsNone(_run_ptt(ws, pcm, timeout=0.5))

    def test_natural_and_timeout_triggers_fail(self):
        for trigger in ("natural", "timeout"):
            pcm = b"\x05" * FRAME_BYTES
            ws = FakeWebsocket(
                early=[], late=[json.dumps(_valid("x", "rN", 1, trigger))]
            )
            self.assertIsNone(_run_ptt(ws, pcm, timeout=0.4))

    def test_overflow_fails_closed(self):
        pcm = b"\x06" * (FRAME_BYTES * 400)  # far beyond the bounded queue
        ws = FakeWebsocket(early=[], late=[json.dumps(_valid())])
        # Slow the sender so the burst overflows deterministically.
        orig_send = ws.send_binary

        def slow_send(data):
            time.sleep(0.01)
            return orig_send(data)

        ws.send_binary = slow_send  # type: ignore[method-assign]
        self.assertIsNone(_run_ptt(ws, pcm, timeout=0.5))

    def test_server_error_fails(self):
        pcm = b"\x07" * FRAME_BYTES
        ws = FakeWebsocket(
            early=[json.dumps({"type": "Error", "message": "boom"})],
            late=[json.dumps(_valid())],
        )
        self.assertIsNone(_run_ptt(ws, pcm, timeout=0.5))

    def test_decode_failure_fails(self):
        pcm = b"\x08" * FRAME_BYTES
        ws = FakeWebsocket(early=["{not json"], late=[json.dumps(_valid())])
        self.assertIsNone(_run_ptt(ws, pcm, timeout=0.5))

    def test_close_fails(self):
        pcm = b"\x09" * FRAME_BYTES
        ws = FakeWebsocket(
            early=[ConnectionError("fake close")],
            late=[json.dumps(_valid())],
        )
        self.assertIsNone(_run_ptt(ws, pcm, timeout=0.5))

    def test_warning_no_active_turn_fails(self):
        pcm = b"\x0a" * FRAME_BYTES
        ws = FakeWebsocket(
            early=[
                json.dumps(
                    {
                        "type": "Warning",
                        "warning": "FORCE_END_TURN_NO_ACTIVE_TURN",
                    }
                )
            ],
            late=[json.dumps(_valid())],
        )
        self.assertIsNone(_run_ptt(ws, pcm, timeout=0.5))

    def test_sender_failure_fails(self):
        pcm = b"\x0b" * FRAME_BYTES
        ws = FakeWebsocket(early=[], late=[json.dumps(_valid())])
        self.assertIsNone(_run_ptt(ws, pcm, timeout=0.5, fail_send=True))

    def test_missing_ids_and_nested_shape_fail(self):
        bad_ids = dict(_valid())
        del bad_ids["request_id"]
        pcm = b"\x0c" * FRAME_BYTES
        ws = FakeWebsocket(early=[], late=[json.dumps(bad_ids)])
        self.assertIsNone(_run_ptt(ws, pcm, timeout=0.4))
        ws2 = FakeWebsocket(early=[], late=[json.dumps(_old_nested_shape())])
        self.assertIsNone(_run_ptt(ws2, pcm, timeout=0.4))

    def test_sink_error_propagates_not_swallowed(self):
        pcm = b"\x0d" * FRAME_BYTES
        ws = FakeWebsocket(early=[], late=[json.dumps(_valid("stop"))])

        def bad_sink(utterance):
            raise RuntimeError("sink boom")

        with self.assertRaises(RuntimeError):
            _run_ptt(ws, pcm, timeout=1.0, sink=bad_sink)


class VoiceCliTest(unittest.TestCase):
    def _run_main(self, argv, utterance):
        with mock.patch.object(
            voice_cli, "run_push_to_talk", return_value=utterance
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = voice_cli.main(argv)
        return code, buf.getvalue()

    def test_default_prints_finalized_transcript_json(self):
        utterance = FinalizedUtterance(
            text="move forward", request_id="s", turn_index=1
        )
        code, out = self._run_main([], utterance)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["transcript"], "move forward")
        self.assertTrue(payload["finalized"])

    def test_no_utterance_fails_closed_without_routing(self):
        with mock.patch.object(voice_cli, "run_push_to_talk", return_value=None):
            with mock.patch.object(
                voice_cli, "route_transcript"
            ) as route_mock:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = voice_cli.main([])
        self.assertEqual(code, 1)
        route_mock.assert_not_called()
        self.assertFalse(json.loads(buf.getvalue())["finalized"])

    def test_route_calls_peer_route_command_with_client(self):
        utterance = FinalizedUtterance(text="move forward")
        fake_commands = types.ModuleType("steve_voice.commands")
        seen = {}

        class _Result:
            def to_dict(self):
                return {"accepted": True, "action": "move_forward"}

        def route_command(text, client):
            seen["text"] = text
            seen["client"] = client
            return _Result()

        fake_commands.route_command = route_command  # type: ignore[attr-defined]
        fake_sdk = types.ModuleType("typesafe_sdk")

        class TypeSafeClient:
            def __init__(self, model="jev-latest"):
                self.model = model

        fake_sdk.TypeSafeClient = TypeSafeClient  # type: ignore[attr-defined]
        with mock.patch.dict(
            sys.modules,
            {"steve_voice.commands": fake_commands, "typesafe_sdk": fake_sdk},
        ):
            with mock.patch.object(
                voice_cli, "run_push_to_talk", return_value=utterance
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = voice_cli.main(["--route"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["text"], "move forward")
        self.assertIsInstance(seen["client"], TypeSafeClient)
        self.assertEqual(
            json.loads(buf.getvalue()),
            {"accepted": True, "action": "move_forward"},
        )


if __name__ == "__main__":
    unittest.main()
