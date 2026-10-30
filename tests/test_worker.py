import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from common import estop
from control.worker import InputWorker, WorkerError


class FakeChannel:
    instances = []

    def __init__(self, dm):
        self.dm = dm
        self.hwnd = 0
        self.bound = False
        self.events = []
        self.size = (800, 600)
        self.__class__.instances.append(self)

    def bind(self, hwnd):
        self.hwnd = hwnd
        self.bound = True
        self.events.append(("bind", hwnd, threading.get_ident()))

    def assert_target(self):
        return self.hwnd

    def unbind(self):
        self.bound = False
        self.events.append(("unbind", threading.get_ident()))

    def client_size(self):
        return self.size

    def move_to(self, x, y):
        if self.dm.get("block"):
            self.dm["entered"].set()
            self.dm["release"].wait(5)
        self.events.append(("move", x, y, threading.get_ident()))

    def down(self, button): self.events.append(("down", button))
    def up(self, button): self.events.append(("up", button))
    def key_down(self, key): self.events.append(("key_down", key))
    def key_up(self, key): self.events.append(("key_up", key))
    def wheel(self, n): self.events.append(("wheel", n))
    def press(self, key): self.events.append(("press", key))
    def capture_png(self, *args): return False


class WorkerTests(unittest.TestCase):
    def setUp(self):
        estop.clear()
        FakeChannel.instances.clear()

    def make_worker(self, **kwargs):
        kwargs.setdefault("dm_factory", lambda: {})
        kwargs.setdefault("action_timeout", 2.0)
        with patch("control.worker.Channel", FakeChannel):
            worker = InputWorker(23, "test", **kwargs)
            return worker.wait_ready(2)

    def test_bind_and_action_stay_on_worker_thread(self):
        worker = self.make_worker()
        worker.call("move_to", 100, 100)
        channel = FakeChannel.instances[0]
        self.assertEqual(channel.events[0][:2], ("bind", 23))
        self.assertTrue(all(event[-1] == channel.events[0][-1]
                            for event in channel.events if event[0] in ("bind", "move")))
        self.assertGreater(len([e for e in channel.events if e[0] == "move"]), 2)
        worker.stop()

    def test_future_and_serial_execution(self):
        worker = self.make_worker()
        futures = [worker.submit("move_to", i, i) for i in (10, 20, 30)]
        self.assertEqual([f.result(2) for f in futures], [None, None, None])
        self.assertIsNotNone(FakeChannel.instances[0].events[-1][-1])
        worker.stop()

    def test_timeout_isolates_and_rebuilds(self):
        dm = {"block": True, "entered": threading.Event(), "release": threading.Event()}
        worker = self.make_worker(dm_factory=lambda: dm, action_timeout=0.15)
        with self.assertRaisesRegex(WorkerError, "重建"):
            worker.call("move_to", 100, 100)
        self.assertEqual(worker.rebuild_count, 1)
        self.assertIsNot(worker._runner.channel, FakeChannel.instances[0])
        dm["release"].set()
        worker.stop()

    def test_stop_releases_and_rejects_new_commands(self):
        worker = self.make_worker()
        worker.stop()
        channel = FakeChannel.instances[0]
        keys = [e for e in channel.events if e[0] == "key_up"]
        self.assertEqual({e[1] for e in keys}, {"alt", "ctrl", "shift", "win"})
        with self.assertRaises(WorkerError):
            worker.submit("move_to", 1, 1)

    def test_snapshot_and_unknown_command_report_errors(self):
        worker = self.make_worker()
        with self.assertRaisesRegex(Exception, "抓图失败"):
            worker.snapshot()
        with self.assertRaisesRegex(WorkerError, "不支持"):
            worker.call("not_allowed")
        worker.stop()

    def test_frame_uses_scaled_capture(self):
        worker = self.make_worker()
        cap = {"ok": True, "w": 2, "h": 1, "rgb": bytearray((1, 2, 3, 4, 5, 6))}
        with patch("control.worker.wincap.capture_scaled", return_value=cap) as capture:
            data, path, source, click = worker.call("frame")
        capture.assert_called_once_with(23, 800, 600)
        self.assertEqual(data, b"P6 2 1 255\n" + bytes((1, 2, 3, 4, 5, 6)))
        self.assertEqual(path, [])
        self.assertEqual(click, None)
        worker.stop()

    def test_set_visual_reaches_runner_and_state(self):
        worker = self.make_worker()
        worker.set_visual(True)
        state = worker.call("state")
        self.assertTrue(state["visual"])
        self.assertIsNone(state["align"])
        self.assertIsNone(state["send"])
        worker.set_visual(False)
        self.assertFalse(worker.call("state")["visual"])
        worker.stop()

    def test_frame_does_not_wait_behind_a_running_action(self):
        dm = {"block": True, "entered": threading.Event(), "release": threading.Event()}
        worker = self.make_worker(dm_factory=lambda: dm, action_timeout=5.0)
        future = worker.submit("move_to", 100, 100)
        self.assertTrue(dm["entered"].wait(2))
        cap = {"ok": True, "w": 1, "h": 1, "rgb": bytearray((7, 8, 9))}
        started = time.monotonic()
        with patch("control.worker.wincap.capture_scaled", return_value=cap):
            data, _path, _source, _click = worker.frame()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(data, b"P6 1 1 255\n" + bytes((7, 8, 9)))
        dm["release"].set()
        future.result(2)
        worker.stop()

    def test_frame_falls_back_to_the_queue_when_direct_capture_fails(self):
        worker = self.make_worker()
        with patch("control.worker.wincap.capture_scaled", return_value={"ok": False}):
            with self.assertRaisesRegex(Exception, "抓图失败"):
                worker.frame()
        worker.stop()

    def test_set_align_reaches_actuator_and_new_runner(self):
        patcher = patch("control.worker.Channel", FakeChannel)
        patcher.start()
        self.addCleanup(patcher.stop)
        dm = {"block": True, "entered": threading.Event(), "release": threading.Event()}
        worker = self.make_worker(dm_factory=lambda: dm, action_timeout=0.15)
        worker.set_align(7, 45)
        worker.call("state")
        actuator = worker._runner.actuator
        self.assertEqual((actuator.align_tolerance, actuator.align_max_s), (7, 45.0))
        with self.assertRaisesRegex(WorkerError, "重建"):
            worker.call("move_to", 100, 100)
        self.assertTrue(worker._runner.ready.wait(2))
        rebuilt = worker._runner.actuator
        self.assertIsNot(rebuilt, actuator)
        self.assertEqual((rebuilt.align_tolerance, rebuilt.align_max_s), (7, 45.0))
        dm["release"].set()
        worker.stop()

    def test_set_align_clamps_to_supported_range(self):
        worker = self.make_worker()
        worker.set_align(0, 1)
        self.assertEqual((worker.align_tolerance, worker.align_max_s), (1, 3.0))
        worker.set_align(99, 999)
        self.assertEqual((worker.align_tolerance, worker.align_max_s), (20, 120.0))
        worker.stop()

    def test_aligning_actions_get_a_longer_timeout(self):
        dm = {"block": True, "entered": threading.Event(), "release": threading.Event()}
        worker = self.make_worker(dm_factory=lambda: dm, action_timeout=0.15)
        worker.set_visual(True)
        worker.set_align(3, 3)
        worker.call("state")
        worker._runner.actuator._capture = None
        threading.Timer(0.5, dm["release"].set).start()
        worker.call("move_to", 100, 100)
        self.assertEqual(worker.rebuild_count, 0)
        worker.stop()

    def test_clear_path_and_reset_cursor(self):
        worker = self.make_worker()
        worker.call("move_to", 100, 100)
        runner = worker._runner
        self.assertTrue(runner.actuator.last_path)
        self.assertIsNotNone(runner.actuator._cursor.cursor.pos)
        self.assertIsNone(worker.call("clear_path"))
        self.assertEqual(runner.actuator.last_path, [])
        self.assertIsNone(runner.actuator.last_click)
        self.assertIsNone(worker.call("reset_cursor"))
        self.assertIsNone(runner.actuator._cursor.cursor.pos)
        worker.stop()

    def test_default_scales_are_one_to_one(self):
        worker = self.make_worker()
        vp = worker._runner.actuator.vp
        self.assertEqual((vp.input_scale, vp.capture_scale), (1.0, 1.0))
        self.assertEqual(vp.client_size(), (800, 600))
        worker.call("move_to", 123, 456)
        moves = [e for e in FakeChannel.instances[0].events if e[0] == "move"]
        self.assertEqual(moves[-1][1:3], (123, 456))
        worker.stop()

    def test_calibration_uses_original_templates_not_2x(self):
        seen = {}

        class Recorder:
            def __init__(self, channel, viewport, **kwargs):
                seen.update(kwargs)
                raise RuntimeError("stop here")

        worker = self.make_worker()
        with patch("control.calib.TemplatePointerDetector", Recorder):
            with self.assertRaisesRegex(Exception, "stop here"):
                worker.call("calibrate", timeout=2)
        self.assertNotIn("templates", seen)
        self.assertEqual(seen["hotspot"], (1, 1))
        worker.stop()


if __name__ == "__main__":
    unittest.main()
