"""Deepgram Flux push-to-talk client: finalized utterance only, no actuation.

Pipeline: hold to capture mic audio -> release drains queued audio ->
ForceEndTurn -> accept exactly one nonempty EndOfTurn after release.

Flux endpoint (verified against official docs)::

    wss://api.deepgram.com/v2/listen
        ?model=flux-general-en
        &encoding=linear16
        &sample_rate=16000
        &eot_threshold=1.0

Audio is 16 kHz mono PCM16 sent in 80 ms frames (1280 samples = 2560
bytes) while capture is active. A dedicated sender thread drains a
bounded queue continuously over the socket; a dedicated receiver thread
feeds server messages into the finalizer concurrently. Control message
schema is ``{"type": "ForceEndTurn"}`` sent as a text frame.

Documented Flux EndOfTurn exact gate (all conditions required):

- ``type`` is exactly ``"TurnInfo"``,
- ``event`` is exactly ``"EndOfTurn"``,
- ``transcript`` is a top-level string (nonempty after strip to route;
  no nested ``transcript.text`` / ``alternatives`` guessing),
- ``request_id`` is a top-level nonempty string,
- ``turn_index`` is top-level present (int, bool excluded, or nonempty
  string); dedupe key is the exact ``(request_id, turn_index)`` pair,
- ``trigger`` is exactly ``"manual"`` (PTT release path; ``natural`` /
  timeout / eager triggers never route).

Fail-closed rules enforced by :class:`TurnFinalizer` and the runner:

- partial / interim / Update / Eager / Start results are never emitted,
- EndOfTurn observed before release aborts the turn (no utterance),
- only a nonempty manual EndOfTurn observed after release becomes the
  utterance; unknown or missing ids fail closed (message ignored, never
  guessed from other text keys),
- duplicate (request_id, turn_index) deliveries are deduped,
- at most one pending utterance is held; extras are dropped,
- server Error, socket close, JSON decode failure, Warning
  FORCE_END_TURN_NO_ACTIVE_TURN, bounded-queue overflow, and sender
  failure all abort the turn and yield no utterance,
- timeout with no valid EndOfTurn yields no utterance,
- a consumed utterance is never replayed after reconnect (dedupe memory
  retained, pending discarded),
- ``sink``, when given, is called once with the utterance and its
  exceptions propagate (never swallowed silently).

Release sequence (after the release Enter):

1. stop capture (callback disabled, stream stopped) so no new frames
   are produced,
2. enqueue a small bounded tail of silence frames
   (``TAIL_SILENCE_FRAMES`` x 80 ms of zeros) then wait for the queue
   to drain so every prior frame is sent in order before the control
   frame; the tail plus a short bounded ``_tail_delay`` pause gives the
   server a conservative window to decode the final real frame before
   ForceEndTurn (without this the last 80 ms could be truncated when
   ForceEndTurn races the last binary frame),
3. send the text frame ``{"type": "ForceEndTurn"}``; the finalizer is
   marked released only after that send succeeds (send failure aborts),
4. wait up to ``timeout`` seconds for the post-release manual EndOfTurn.

Threading: sender and receiver threads start before capture and join
with a bounded timeout on every exit path. No actuation anywhere.

``sounddevice`` and ``websocket-client`` are optional imports so the pure
helpers and the stdlib test suite work offline. They are imported lazily
inside :func:`run_push_to_talk` only, unless fakes are injected via the
``_connect`` / ``_sd`` / ``_input`` hooks (used by the offline tests).
"""

import json
import os
import queue
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

FLUX_HOST = "wss://api.deepgram.com"
FLUX_PATH = "/v2/listen"
MODEL = "flux-general-en"
ENC = "linear16"
SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_MS = 80
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000  # 1280
FRAME_BYTES = SAMPLES_PER_FRAME * 2  # 2560 bytes of mono PCM16
EOT_THRESHOLD = 1.0
QUEUE_MAX_FRAMES = 125  # ~10 s of buffered audio
FRAME_SEND_TIMEOUT = 0.25  # s; bounded put when the socket is slow
TURN_TIMEOUT = 15.0  # s to wait for EndOfTurn after release
TAIL_SILENCE_FRAMES = 2  # bounded tail: 2 x 80 ms silence before ForceEndTurn
TAIL_FLUSH_DELAY_S = 0.10  # bounded pause so the server decodes last frame
THREAD_JOIN_TIMEOUT_S = 2.0  # bounded join per thread on every exit path
DRAIN_TIMEOUT_S = 5.0  # bounded wait for the queue to drain before ForceEndTurn

# Exact documented Flux values.
VALID_TYPE = "TurnInfo"
VALID_EVENT = "EndOfTurn"
VALID_TRIGGER = "manual"
NO_ACTIVE_TURN_CODE = "FORCE_END_TURN_NO_ACTIVE_TURN"


@dataclass(frozen=True)
class FluxConfig:
    """Connection parameters for one Flux session."""

    api_key: str = ""
    model: str = MODEL
    encoding: str = ENC
    sample_rate: int = SAMPLE_RATE
    eot_threshold: float = EOT_THRESHOLD
    timeout: float = TURN_TIMEOUT


@dataclass(frozen=True)
class FinalizedUtterance:
    """Exactly one finalized voice utterance. Never actuates anything."""

    text: str
    request_id: str = ""
    turn_index: Any = ""
    is_final: bool = True

    @property
    def session_id(self) -> str:  # backward-compat alias
        return self.request_id

    @property
    def turn_id(self) -> Any:  # backward-compat alias
        return self.turn_index

    def to_dict(self) -> Dict[str, Any]:
        return {
            "transcript": self.text,
            "finalized": True,
            "request_id": self.request_id,
            "turn_index": self.turn_index,
        }


def build_flux_url(config: FluxConfig) -> str:
    """Build the official Flux listen URL for the given config."""
    query = urllib.parse.urlencode(
        {
            "model": config.model,
            "encoding": config.encoding,
            "sample_rate": config.sample_rate,
            "eot_threshold": config.eot_threshold,
        }
    )
    return FLUX_HOST + FLUX_PATH + "?" + query


FLUX_URL = build_flux_url(FluxConfig(api_key=""))


def flux_headers(api_key: str) -> Dict[str, str]:
    """Authorization header for the Flux websocket handshake."""
    return {"Authorization": "Token " + api_key}


def force_end_turn_message() -> Dict[str, str]:
    """Control payload sent after draining queued audio on release."""
    return {"type": "ForceEndTurn"}


def split_frames(pcm: bytes, frame_bytes: int = FRAME_BYTES) -> List[bytes]:
    """Chunk raw PCM16 bytes into fixed frames; trailing partial dropped."""
    frames = []
    for offset in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        frames.append(pcm[offset : offset + frame_bytes])
    return frames


def _coerce_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return ""


def _extract_text(msg: Mapping[str, Any]) -> str:
    """Strict transcript extraction: top-level string only, no guessing.

    Returns the stripped top-level ``transcript`` when it is a string,
    else ``""``. Nested ``transcript.text`` shapes and
    ``channel.alternatives`` shapes are intentionally NOT consulted:
    unknown shapes fail closed rather than routing guessed text.
    """
    if not isinstance(msg, Mapping):
        return ""
    text = msg.get("transcript")
    if isinstance(text, str):
        return text.strip()
    return ""


def _extract_ids(msg: Mapping[str, Any]) -> Tuple[str, Any]:
    """Return (request_id, turn_index) for the exact documented envelope.

    ``request_id`` must be a nonempty string; ``turn_index`` must be an
    int (bool excluded) or a nonempty string. Anything else yields
    ``("", "")`` so the caller fails closed instead of guessing ids
    from ``session_id`` / ``turn`` / ``turn_id`` style keys.
    """
    if not isinstance(msg, Mapping):
        return "", ""
    request_id = msg.get("request_id")
    if not (isinstance(request_id, str) and request_id):
        return "", ""
    turn_index = msg.get("turn_index")
    if isinstance(turn_index, bool) or turn_index is None:
        return "", ""
    if isinstance(turn_index, int):
        return request_id, turn_index
    if isinstance(turn_index, str) and turn_index:
        return request_id, turn_index
    return "", ""


def _is_no_active_turn_warning(msg: Mapping[str, Any]) -> bool:
    for key in ("warning", "code", "message", "reason", "error"):
        value = msg.get(key)
        if isinstance(value, str) and NO_ACTIVE_TURN_CODE in value:
            return True
    return False


def classify_message(msg: Mapping[str, Any]) -> str:
    """Classify a Flux server message under the exact documented gate.

    Returns one of ``"end_of_turn"``, ``"final"``, ``"partial"``,
    ``"error"``, ``"warning_no_active"``, or ``"other"``. Only
    ``"end_of_turn"`` may become an utterance: it requires type
    TurnInfo + event EndOfTurn + trigger manual + top-level transcript
    string + valid request_id/turn_index. Update / Eager / Start /
    natural / timeout triggers / nested-text shapes all classify as
    ``"other"`` or ``"partial"`` and never route. ``"final"`` results
    are discarded (never routed eagerly). ``"error"`` and
    ``"warning_no_active"`` signal abort.
    """
    if not isinstance(msg, Mapping):
        return "other"
    msg_type = msg.get("type")
    event = msg.get("event")
    if msg_type == "Error" or event == "Error":
        return "error"
    if msg_type == "Warning":
        if _is_no_active_turn_warning(msg):
            return "warning_no_active"
        return "other"
    if msg_type == VALID_TYPE and event == VALID_EVENT:
        if msg.get("trigger") != VALID_TRIGGER:
            return "other"  # natural / timeout / eager triggers never route
        if not isinstance(msg.get("transcript"), str):
            return "other"  # no text-key guessing
        request_id = msg.get("request_id")
        if not (isinstance(request_id, str) and request_id):
            return "other"  # missing ids fail closed
        turn_index = msg.get("turn_index")
        if isinstance(turn_index, bool) or turn_index is None:
            return "other"
        if isinstance(turn_index, str) and not turn_index:
            return "other"
        if not isinstance(turn_index, (int, str)):
            return "other"
        return "end_of_turn"
    is_final = msg.get("is_final")
    if is_final is True:
        return "final"
    # Explicit non-routable Flux signals.
    if msg_type in ("Update", "Eager", "Start"):
        return "other"
    if event in ("Update", "Eager", "Start", "EagerTurn", "StartOfTurn"):
        return "other"
    if msg_type in ("Results", "TurnInfo", "TurnUpdate") or event is not None:
        # Ambiguous shape without the exact manual EndOfTurn gate:
        # treat as partial/other so we never route eagerly.
        if isinstance(msg_type, str) or isinstance(event, str):
            return "partial"
    if msg_type in ("Results", "TurnInfo", "TurnUpdate"):
        return "partial"
    return "other"


class TurnFinalizer:
    """Fail-closed state machine for exactly one finalized utterance.

    Typical use: create, feed websocket messages via :meth:`observe`
    while capturing (receiver thread), call :meth:`release` when the
    talk control is released (after draining queued audio and sending
    ForceEndTurn), keep feeding messages until :meth:`finalize`
    returns the utterance or the wait times out. Thread-safe: the
    receiver thread may call :meth:`observe` while the main thread
    calls :meth:`release` / :meth:`finalize`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._released = False
        self._failed = False
        self._seen: set = set()
        self._pending: Optional[FinalizedUtterance] = None

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    @property
    def failed(self) -> bool:
        with self._lock:
            return self._failed

    def release(self) -> None:
        """Mark the talk control released; enables EndOfTurn acceptance."""
        with self._lock:
            if not self._failed:
                self._released = True

    def abort(self) -> None:
        """Abort the turn: discard any pending utterance, fail closed."""
        with self._lock:
            self._failed = True
            self._pending = None

    def on_timeout(self) -> None:
        """No EndOfTurn in time: fail closed with no utterance."""
        self.abort()

    def on_error(self, _exc: Any = None) -> None:
        """Socket / decode / close / overflow / sender error: fail closed."""
        self.abort()

    def on_warning_no_active_turn(self) -> None:
        """Warning FORCE_END_TURN_NO_ACTIVE_TURN: fail closed."""
        self.abort()

    def reconnect(self) -> None:
        """Reset for a fresh socket without replaying prior utterances.

        Dedup keys are retained, pending is discarded, so redelivered
        server messages are never re-emitted.
        """
        with self._lock:
            self._released = False
            self._failed = False
            self._pending = None
            # _seen intentionally preserved: no replay on reconnect.

    def observe(self, msg: Mapping[str, Any]) -> Optional[FinalizedUtterance]:
        """Feed one server message; returns the utterance when finalized."""
        with self._lock:
            if self._failed:
                return None
            kind = classify_message(msg)
            if kind in ("other", "partial"):
                return None
            if kind == "final":
                # Early (or duplicate-shape) finals: discard, never route.
                return None
            if kind == "error":
                self._failed = True
                self._pending = None
                return None
            if kind == "warning_no_active":
                self._failed = True
                self._pending = None
                return None
            # kind == "end_of_turn" from here on.
            if not self._released:
                # Early EndOfTurn aborts the current turn: fail closed.
                self._failed = True
                self._pending = None
                return None
            if not isinstance(msg, Mapping):
                return None
            text = _extract_text(msg)
            if not text:
                return None  # empty EndOfTurn carries no utterance
            request_id, turn_index = _extract_ids(msg)
            if not request_id:
                return None  # missing ids fail closed
            key = (request_id, turn_index)
            if key in self._seen:
                return None  # dedupe redeliveries
            self._seen.add(key)
            if self._pending is not None:
                return None  # one pending utterance: drop extras
            utterance = FinalizedUtterance(
                text=text, request_id=request_id, turn_index=turn_index
            )
            self._pending = utterance
            return utterance

    def finalize(self) -> Optional[FinalizedUtterance]:
        """Consume the pending utterance exactly once (no replay)."""
        with self._lock:
            utterance = self._pending
            self._pending = None
            return utterance


def wait_for_utterance(
    messages: Any,
    finalizer: TurnFinalizer,
    timeout: float = TURN_TIMEOUT,
) -> Optional[FinalizedUtterance]:
    """Feed an iterable of server messages until finalized or timeout.

    Pure helper (no sockets): used by offline tests. Real socket flow
    uses the threaded receiver inside :func:`run_push_to_talk`.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    for msg in messages:
        if time.monotonic() > deadline:
            break
        if finalizer.failed:
            break
        if not isinstance(msg, Mapping):
            continue
        utterance = finalizer.observe(msg)
        if utterance is not None:
            return utterance
    return finalizer.finalize()


# Sink type for later simulation wiring. The client calls the sink with the
# finalized utterance (and later the routed command) but never actuates.
Sink = Callable[[FinalizedUtterance], None]


def _iter_socket_messages(ws: Any, timeout: float):  # type: ignore[no-untyped-def]
    """Yield decoded JSON messages from a websocket-client socket.

    Superseded by the threaded receiver in :func:`run_push_to_talk`
    (which aborts on decode failure / close / Error / Warning).
    Kept for backward compatibility; skips binary frames and
    undecodable text frames.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() <= deadline:
        try:
            raw = ws.recv()
        except Exception:
            break
        if raw is None:
            continue
        if isinstance(raw, bytes):
            continue  # server audio echo never carries transcripts
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(msg, dict):
            yield msg


def _call_input(input_fn: Any, prompt_text: str) -> str:
    try:
        return input_fn(prompt_text)
    except TypeError:
        return input_fn()


def run_push_to_talk(
    api_key: Optional[str] = None,
    *,
    config: Optional[FluxConfig] = None,
    sink: Optional[Sink] = None,
    prompt: bool = True,
    timeout: float = TURN_TIMEOUT,
    _connect: Optional[Callable[..., Any]] = None,
    _sd: Optional[Any] = None,
    _input: Optional[Callable[..., str]] = None,
    _tail_frames: int = TAIL_SILENCE_FRAMES,
    _tail_delay: float = TAIL_FLUSH_DELAY_S,
) -> Optional[FinalizedUtterance]:
    """Run one console push-to-talk turn; return the finalized utterance.

    Console control is Enter-toggle (native Windows/Linux console): press
    Enter once to start capturing, speak, press Enter again to release.
    True hold-to-talk is not possible on a plain console without extra
    key-handling dependencies, so toggle is used and documented here and
    in the CLI help.

    Steps: connect Flux websocket -> start sender + receiver threads ->
    wait for start Enter -> stream 80 ms PCM16 frames from the mic
    continuously over a bounded queue -> on release Enter, stop capture,
    enqueue a bounded silence tail, wait for the drain so all prior
    frames are sent in order, pause briefly for server decode, send the
    text frame ForceEndTurn, mark released only after that send
    succeeds, then wait up to ``timeout`` seconds for the post-release
    manual EndOfTurn. Any early EndOfTurn, non-manual trigger, timeout,
    socket error / close, decode failure, Warning
    FORCE_END_TURN_NO_ACTIVE_TURN, queue overflow, or sender failure
    returns None (fails closed) and the client actuates nothing.
    ``sink``, when given, receives the finalized utterance; sink
    exceptions propagate to the caller (never swallowed silently).

    ``_connect`` / ``_sd`` / ``_input`` are offline-test hooks: a fake
    websocket factory, a fake sounddevice module, and a fake input
    function. They default to the real ``websocket.create_connection``,
    ``sounddevice``, and ``input``.
    """
    key = api_key if api_key else os.environ.get("DEEPGRAM_API_KEY", "")
    if not key:
        raise RuntimeError(
            "missing DEEPGRAM_API_KEY: set it in the environment; "
            "refusing to run voice input without credentials"
        )
    cfg = config or FluxConfig(api_key=key, timeout=timeout)
    if not cfg.api_key:
        cfg = FluxConfig(
            api_key=key,
            model=cfg.model,
            encoding=cfg.encoding,
            sample_rate=cfg.sample_rate,
            eot_threshold=cfg.eot_threshold,
            timeout=cfg.timeout if timeout == TURN_TIMEOUT else timeout,
        )
    wait_timeout = cfg.timeout if timeout == TURN_TIMEOUT else timeout
    input_fn = _input if _input is not None else input

    url = build_flux_url(cfg)
    if _connect is not None:
        try:
            ws = _connect(url, flux_headers(key), wait_timeout)
        except Exception as exc:
            raise RuntimeError("Flux connection failed: %s" % exc) from exc
    else:
        try:
            import websocket  # type: ignore  # optional: websocket-client
        except ImportError as exc:
            raise RuntimeError(
                "websocket-client is required for live voice "
                "(pip install steve-router[voice])"
            ) from exc
        try:
            ws = websocket.create_connection(
                url, header=flux_headers(key), timeout=wait_timeout
            )
        except Exception as exc:
            raise RuntimeError("Flux connection failed: %s" % exc) from exc

    sd = _sd
    if sd is None:
        try:
            import sounddevice as sd  # type: ignore  # optional
        except ImportError as exc:
            try:
                ws.close()
            except Exception:
                pass
            raise RuntimeError(
                "sounddevice is required for live voice "
                "(pip install steve-router[voice])"
            ) from exc

    frames: "queue.Queue[Any]" = queue.Queue(maxsize=QUEUE_MAX_FRAMES)
    finalizer = TurnFinalizer()
    capturing = {"active": False}
    sender_exc: Dict[str, Any] = {"exc": None}
    stop_event = threading.Event()

    def _callback(indata: Any, frame_count: Any, time_info: Any, status: Any) -> None:
        if not capturing["active"]:
            return
        if finalizer.failed:
            return
        try:
            chunk = bytes(indata)
        except Exception:
            return
        for frame in split_frames(chunk):
            try:
                frames.put_nowait(frame)
            except queue.Full:
                # Bounded queue: overflow aborts the turn, never grows.
                finalizer.on_error(queue.Full("capture queue overflow"))
                capturing["active"] = False
                return

    def _sender() -> None:
        while True:
            if stop_event.is_set() and frames.empty():
                break
            try:
                frame = frames.get(timeout=0.05)
            except queue.Empty:
                if stop_event.is_set():
                    break
                continue
            if frame is None:  # shutdown sentinel
                try:
                    frames.task_done()
                except Exception:
                    pass
                break
            try:
                ws.send_binary(frame)
            except Exception as exc:
                sender_exc["exc"] = exc
                finalizer.on_error(exc)
                try:
                    frames.task_done()
                except Exception:
                    pass
                break
            try:
                frames.task_done()
            except Exception:
                pass

    def _receiver() -> None:
        while not stop_event.is_set():
            if finalizer.failed:
                break
            try:
                raw = ws.recv()
            except Exception as exc:
                if stop_event.is_set():
                    break
                # Socket close / error aborts the turn.
                finalizer.on_error(exc)
                break
            if raw is None:
                time.sleep(0.005)
                continue
            if isinstance(raw, bytes):
                continue  # server audio echo never carries transcripts
            if not isinstance(raw, str):
                continue
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError) as exc:
                # Decode failure aborts the turn.
                finalizer.on_error(exc)
                break
            if isinstance(msg, dict):
                finalizer.observe(msg)
                if finalizer.failed:
                    break
            # extras / duplicates absorbed via dedupe; main consumes.

    sender_thread = threading.Thread(
        target=_sender, name="flux-sender", daemon=True
    )
    receiver_thread = threading.Thread(
        target=_receiver, name="flux-receiver", daemon=True
    )
    sender_thread.start()
    receiver_thread.start()

    stream = None
    try:
        if prompt:
            _call_input(input_fn, "Press Enter to start talking (mic opens).")
        if stop_event.is_set():
            return None
        capturing["active"] = True
        try:
            stream = sd.InputStream(
                samplerate=cfg.sample_rate,
                channels=CHANNELS,
                dtype="int16",
                blocksize=SAMPLES_PER_FRAME,
                callback=_callback,
            )
            stream.start()
        except Exception as exc:
            finalizer.on_error(exc)
            raise RuntimeError("mic capture failed: %s" % exc) from exc
        if prompt:
            _call_input(
                input_fn, "Recording... Press Enter to release (drain + finalize)."
            )
        # Release: stop capture first so no new frames are produced.
        capturing["active"] = False
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
        if finalizer.failed or sender_exc["exc"] is not None:
            return None
        # Bounded silence tail: lets the server decode the final real
        # frame before ForceEndTurn instead of racing it.
        try:
            silence = b"\x00" * FRAME_BYTES
            for _ in range(max(0, int(_tail_frames))):
                try:
                    frames.put(silence, timeout=FRAME_SEND_TIMEOUT)
                except queue.Full:
                    finalizer.on_error(queue.Full("tail queue overflow"))
                    return None
        except Exception as exc:
            finalizer.on_error(exc)
            return None
        # Wait for the drain so every prior frame is sent in order
        # before the ForceEndTurn text frame.
        drain_deadline = time.monotonic() + DRAIN_TIMEOUT_S
        while True:
            if finalizer.failed or sender_exc["exc"] is not None:
                return None
            try:
                unfinished = frames.unfinished_tasks
            except AttributeError:
                unfinished = frames.qsize()
            if unfinished <= 0:
                break
            if time.monotonic() > drain_deadline:
                finalizer.on_error(TimeoutError("audio drain timed out"))
                return None
            time.sleep(0.01)
        if _tail_delay and float(_tail_delay) > 0:
            time.sleep(min(float(_tail_delay), 1.0))
        if finalizer.failed or sender_exc["exc"] is not None:
            return None
        try:
            ws.send(json.dumps(force_end_turn_message()))
        except Exception as exc:
            finalizer.on_error(exc)
            return None
        # Released only after the ForceEndTurn send succeeds.
        finalizer.release()
        deadline = time.monotonic() + max(0.0, float(wait_timeout))
        while time.monotonic() <= deadline:
            if finalizer.failed:
                return None
            if sender_exc["exc"] is not None:
                finalizer.on_error(sender_exc["exc"])
                return None
            utterance = finalizer.finalize()
            if utterance is not None:
                if sink is not None:
                    sink(utterance)  # propagate; never swallow silently
                return utterance
            time.sleep(0.01)
        if not finalizer.failed:
            finalizer.on_timeout()
        return None
    finally:
        stop_event.set()
        capturing["active"] = False
        try:
            frames.put_nowait(None)
        except queue.Full:
            try:
                frames.get_nowait()
            except queue.Empty:
                pass
            except Exception:
                pass
            try:
                frames.put_nowait(None)
            except Exception:
                pass
        except Exception:
            pass
        for thread in (sender_thread, receiver_thread):
            try:
                thread.join(timeout=THREAD_JOIN_TIMEOUT_S)
            except Exception:
                pass
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        try:
            ws.close()
        except Exception:
            pass
