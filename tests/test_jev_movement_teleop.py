"""Offline concurrency tests for dimos.robot.unitree.jev_movement_teleop.

No live API, no hardware, no installed dimOS: heavy imports
(reactivex, dimos.core, dimos.msgs, dimos.navigation, dimos.utils) are
stubbed in sys.modules before the module under test is imported, and the
JEV client / navigation / agent / tfbuffer are fakes. The worker thread is
real, so generation invalidation, expiry, bounded-queue, and shutdown
semantics are exercised, not mocked away.
"""

import logging
import queue
import sys
import threading
import time
import types
import unittest


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _ModuleStub:
    def __init__(self, **kwargs):
        self._disposables = []

    def start(self):
        pass

    def stop(self):
        pass

    def register_disposable(self, disposable):
        self._disposables.append(disposable)


class _Subscriptable:
    def __class_getitem__(cls, item):
        return cls


class _In(_Subscriptable):
    pass


class _Out(_Subscriptable):
    pass


class _AIMessage:
    def __init__(self, content=""):
        self.content = content


class _Disposable:
    def __init__(self, dispose=None):
        self._dispose = dispose

    def dispose(self):
        if self._dispose is not None:
            self._dispose()


def _rpc(fn):
    return fn


def _setup_logger(*args, **kwargs):
    return logging.getLogger("jev-movement-teleop-test")


def _install_stubs():
    _stub("reactivex")
    _stub("reactivex.disposable", Disposable=_Disposable)
    for name in (
        "dimos.core",
        "dimos.msgs",
        "dimos.msgs.geometry_msgs",
        "dimos.msgs.tf2_msgs",
        "dimos.navigation",
        "dimos.utils",
    ):
        parent = sys.modules.get(name)
        if parent is None:
            parent = types.ModuleType(name)
            parent.__path__ = []
            sys.modules[name] = parent
    _stub("dimos.core.core", rpc=_rpc)
    _stub("dimos.core.module", Module=_ModuleStub)
    _stub("dimos.core.stream", In=_In, Out=_Out)
    _stub("dimos.msgs.geometry_msgs.PoseStamped", PoseStamped=object)
    _stub("dimos.msgs.geometry_msgs.Quaternion", Quaternion=object)
    _stub("dimos.msgs.geometry_msgs.Vector3", Vector3=object)
    _stub("dimos.msgs.tf2_msgs.TFMessage", TFMessage=object)
    _stub(
        "dimos.navigation.navigation_spec",
        NavigationInterfaceSpec=object,
    )
    _stub("dimos.utils.logging_config", setup_logger=_setup_logger)
    messages = types.ModuleType("langchain_core.messages")
    setattr(messages, "AIMessage", _AIMessage)
    setattr(messages, "BaseMessage", object)
    # Only fill in what is missing so a real langchain_core install keeps
    # working for every other test module in this process.
    existing = sys.modules.get("langchain_core.messages")
    if existing is None:
        sys.modules["langchain_core.messages"] = messages
    else:
        setattr(existing, "AIMessage", getattr(existing, "AIMessage", _AIMessage))
        setattr(existing, "BaseMessage", getattr(existing, "BaseMessage", object))


_install_stubs()

from dimos.robot.unitree import jev_movement_teleop as teleop  # noqa: E402
from steve_voice.commands import VoiceCommand  # noqa: E402


class FakeNavigation:
    def __init__(self):
        self.goals = []
        self.cancels = 0
        self.lock = threading.Lock()

    def set_goal(self, goal):
        with self.lock:
            self.goals.append(goal)

    def cancel_goal(self):
        with self.lock:
            self.cancels += 1


class FakeAgent:
    def __init__(self):
        self.messages = []
        self.lock = threading.Lock()

    def publish(self, message):
        with self.lock:
            self.messages.append(message)


class FakeClient:
    """Stand-in for TypeSafeClient; route path is patched, not this."""

    def __init__(self):
        self.calls = []
        self.closed = False
        self.lock = threading.Lock()

    def system_one(self, *args, **kwargs):
        with self.lock:
            self.calls.append((args, kwargs))
        raise AssertionError("patched route_command must bypass the client")

    def close(self):
        self.closed = True


def make_teleop():
    obj = teleop.JevMovementTeleop.__new__(teleop.JevMovementTeleop)
    teleop.JevMovementTeleop.__init__(obj)
    obj._navigation = FakeNavigation()
    obj.agent = FakeAgent()
    obj.tfbuffer = None
    obj._client = FakeClient()  # type: ignore[assignment]
    return obj


def run_worker(obj):
    thread = threading.Thread(target=obj._worker_loop, daemon=True)
    thread.start()
    return thread


def stop_worker(obj, thread):
    obj._worker_stop.set()
    try:
        obj._queue.put_nowait(None)
    except queue.Full:
        pass
    thread.join(timeout=5.0)
    return not thread.is_alive()


class JevMovementTeleopTest(unittest.TestCase):
    def test_local_stop_bypasses_cloud(self):
        obj = make_teleop()
        before = obj._generation
        obj._on_text("stop!")
        self.assertEqual(obj._client.calls, [])  # type: ignore[attr-defined]
        self.assertEqual(obj._navigation.cancels, 1)
        self.assertEqual(obj._queue.qsize(), 0)
        self.assertGreater(obj._generation, before)
        self.assertTrue(
            any(getattr(m, "content", "") == "Stopped." for m in obj.agent.messages)
        )

    def test_on_text_nonblocking_while_worker_blocked(self):
        obj = make_teleop()
        release = threading.Event()
        entered = threading.Event()

        def blocking_route(text, client, **kwargs):
            entered.set()
            release.wait(timeout=10.0)
            return VoiceCommand(
                accepted=False,
                action=None,
                confidence=0.0,
                magnitude=None,
                reason="unblocked",
                source_text=text,
            )

        real_route = teleop.route_command
        teleop.route_command = blocking_route
        self.addCleanup(setattr, teleop, "route_command", real_route)
        worker = run_worker(obj)
        try:
            obj._on_text("move forward")
            self.assertTrue(entered.wait(timeout=5.0))
            started = time.monotonic()
            for _ in range(3):
                obj._on_text("strafe left")
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2.0)
        finally:
            release.set()
            self.assertTrue(stop_worker(obj, worker))

    def test_stale_result_after_stop_never_sets_goal(self):
        obj = make_teleop()
        release = threading.Event()
        entered = threading.Event()

        def blocking_route(text, client, **kwargs):
            entered.set()
            release.wait(timeout=10.0)
            return VoiceCommand(
                accepted=True,
                action="forward",
                confidence=0.99,
                magnitude=0.5,
                reason="routed",
                source_text=text,
            )

        real_route = teleop.route_command
        teleop.route_command = blocking_route
        self.addCleanup(setattr, teleop, "route_command", real_route)
        worker = run_worker(obj)
        try:
            obj._on_text("move forward")
            self.assertTrue(entered.wait(timeout=5.0))
            obj._local_stop()  # bumps generation while JEV is in flight
            release.set()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with obj._navigation.lock:
                    done = obj._navigation.cancels >= 1
                if done and obj._queue.empty():
                    break
                time.sleep(0.01)
            time.sleep(0.3)  # let the worker finish the post-call check
            with obj._navigation.lock:
                self.assertEqual(obj._navigation.goals, [])
        finally:
            release.set()
            self.assertTrue(stop_worker(obj, worker))

    def test_expired_request_dropped_before_cloud(self):
        obj = make_teleop()
        real_route = teleop.route_command
        calls = []

        def counting_route(text, client, **kwargs):
            calls.append(text)
            return VoiceCommand(
                accepted=True,
                action="forward",
                confidence=0.99,
                magnitude=0.5,
                reason="routed",
                source_text=text,
            )

        teleop.route_command = counting_route
        self.addCleanup(setattr, teleop, "route_command", real_route)
        with obj._gen_lock:
            generation = obj._generation
        obj._queue.put_nowait((generation, time.monotonic() - 1.0, "move forward"))
        worker = run_worker(obj)
        try:
            time.sleep(0.5)
            self.assertEqual(calls, [])
            with obj._navigation.lock:
                self.assertEqual(obj._navigation.goals, [])
        finally:
            self.assertTrue(stop_worker(obj, worker))

    def test_fresh_motion_dispatches_goal(self):
        obj = make_teleop()
        sentinel = object()
        seen = []

        def fake_goal_pose(pose, forward_m, left_m, turn_deg):
            seen.append((forward_m, left_m, turn_deg))
            return sentinel

        class FakeTF:
            def to_pose(self):
                return object()

        class FakeBuffer:
            def get(self, *args):
                return FakeTF()

        def fake_route(text, client, **kwargs):
            return VoiceCommand(
                accepted=True,
                action="forward",
                confidence=0.9,
                magnitude=0.5,
                reason="routed",
                source_text=text,
            )

        real_route = teleop.route_command
        real_goal = teleop._goal_pose
        teleop.route_command = fake_route
        teleop._goal_pose = fake_goal_pose
        self.addCleanup(setattr, teleop, "route_command", real_route)
        self.addCleanup(setattr, teleop, "_goal_pose", real_goal)
        obj.tfbuffer = FakeBuffer()
        obj._on_text("move forward")
        worker = run_worker(obj)
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with obj._navigation.lock:
                    if obj._navigation.goals:
                        break
                time.sleep(0.01)
            with obj._navigation.lock:
                self.assertEqual(obj._navigation.goals, [sentinel])
            self.assertEqual(seen, [(0.5, 0.0, 0.0)])
        finally:
            self.assertTrue(stop_worker(obj, worker))

    def test_reject_never_actuates(self):
        obj = make_teleop()
        obj._dispatch(
            VoiceCommand(
                accepted=False,
                action=None,
                confidence=0.9,
                magnitude=None,
                reason="JEV did not recognize a single motion command",
                source_text="do a backflip",
            )
        )
        self.assertEqual(obj._navigation.goals, [])
        self.assertEqual(obj._navigation.cancels, 0)
        self.assertEqual(obj.agent.messages, [])

    def test_jev_stop_dispatch_cancels_without_goal(self):
        obj = make_teleop()
        obj._dispatch(
            VoiceCommand(
                accepted=True,
                action="stop",
                confidence=0.9,
                magnitude=None,
                reason="routed stop",
                source_text="halt",
            )
        )
        self.assertEqual(obj._navigation.goals, [])
        self.assertEqual(obj._navigation.cancels, 1)

    def test_bounded_queue_sheds_oldest_never_blocks(self):
        obj = make_teleop()
        for i in range(20):
            obj._on_text("move forward %d" % i)
        self.assertLessEqual(obj._queue.qsize(), teleop._QUEUE_MAXSIZE)
        drained = []
        while True:
            try:
                drained.append(obj._queue.get_nowait())
            except queue.Empty:
                break
        self.assertLessEqual(len(drained), teleop._QUEUE_MAXSIZE)
        # Newest intent wins: the last enqueue survives the shedding.
        self.assertTrue(any("19" in item[2] for item in drained if item is not None))

    def test_stop_shutdown_is_safe(self):
        obj = make_teleop()
        client = obj._client
        worker = run_worker(obj)
        obj._worker = worker
        before = obj._generation
        obj.stop()
        self.assertGreater(obj._generation, before)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(obj._worker)
        self.assertTrue(client.closed)  # type: ignore[attr-defined]
        self.assertIsNone(obj._client)


if __name__ == "__main__":
    unittest.main()
