import ast
import inspect
import os
import pathlib
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from control.viewer_std import ViewerSettingsError
from kmtool import connect

APP = pathlib.Path(__file__).resolve().parents[1] / "src" / "kmtool" / "app.py"
W, H = 800, 600


def frame(fill=(80, 90, 100), skip=None):
    """800x600 的 rgb；skip(x, y) 为真的像素置黑。"""
    data = bytearray(W * H * 3)
    for y in range(H):
        for x in range(W):
            if skip is not None and skip(x, y):
                continue
            i = (y * W + x) * 3
            data[i:i + 3] = bytes(fill)
    return bytes(data), W, H


class FrameFilledTest(unittest.TestCase):
    def test_full_frame(self):
        self.assertTrue(connect.frame_filled(*frame()))

    def test_black_frame(self):
        self.assertFalse(connect.frame_filled(*frame(fill=(0, 0, 0))))

    def test_only_left_half_arrived(self):
        self.assertFalse(connect.frame_filled(*frame(skip=lambda x, y: x >= 400)))

    def test_only_top_half_arrived(self):
        self.assertFalse(connect.frame_filled(*frame(skip=lambda x, y: y > 320)))

    def test_black_gutters_wider_than_eight_px_fail(self):
        self.assertFalse(connect.frame_filled(*frame(skip=lambda x, y: x < 20 or x > 779)))

    def test_one_lit_pixel_within_eight_px_of_each_edge_is_enough(self):
        def skip(x, y):
            return not (x in (8, 791) and y in (150, 300, 450))
        self.assertTrue(connect.frame_filled(*frame(skip=skip)))

    def test_dark_threshold_is_strictly_greater_than_30(self):
        self.assertFalse(connect.frame_filled(*frame(fill=(10, 10, 10))))
        self.assertTrue(connect.frame_filled(*frame(fill=(10, 10, 11))))

    def test_short_buffer_or_bad_size(self):
        self.assertFalse(connect.frame_filled(b"\x01" * 100, W, H))
        self.assertFalse(connect.frame_filled(b"", 0, 0))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class WaitFrameReadyTest(unittest.TestCase):
    def test_returns_when_frame_fills_and_reports_progress(self):
        clock = Clock()
        good, bad = frame(), frame(fill=(0, 0, 0))
        frames = iter([None, bad, bad, good])
        progress = []
        elapsed = connect.wait_frame_ready(lambda: next(frames), progress=progress.append, clock=clock,
                                           sleep=clock.sleep, poll_s=0.5)
        self.assertAlmostEqual(elapsed, 1.5)
        self.assertEqual(progress, [0.0, 0.5, 1.0])

    def test_timeout_message_is_not_a_connection_timeout(self):
        clock = Clock()
        with self.assertRaises(connect.FrameNotReady) as ctx:
            connect.wait_frame_ready(lambda: frame(fill=(0, 0, 0)), timeout_s=5, clock=clock,
                                     sleep=clock.sleep, poll_s=1)
        self.assertIn("画面还没传完整", str(ctx.exception))
        self.assertNotIn("连接超时", str(ctx.exception))
        self.assertGreaterEqual(clock.now, 5)

    def test_cancel_stops_before_next_grab(self):
        clock = Clock()
        cancel = threading.Event()
        calls = []

        def grab():
            calls.append(clock.now)
            cancel.set()
            return frame(fill=(0, 0, 0))

        with self.assertRaises(connect.Cancelled):
            connect.wait_frame_ready(grab, cancel=cancel, clock=clock, sleep=clock.sleep)
        self.assertEqual(len(calls), 1)

    def test_default_limit_is_120_seconds(self):
        self.assertEqual(connect.FRAME_TIMEOUT_S, 120.0)


def make_ops(events, **overrides):
    clock = Clock()
    viewer = SimpleNamespace(top=11, child=12)
    target = SimpleNamespace(top=11, child=12, pid=99)

    confirmed = threading.Event()

    def ensure_standard(host, port):
        events.append(("ensure_standard", host, port))
        return overrides.get("killed", [])

    def confirm(pid, secs):
        events.append(("confirm", pid, secs))
        confirmed.set()

    def find_viewers(title=None):
        events.append(("find_viewers", title))
        return overrides.get("found", [])

    def launch_viewer(host, port, password):
        events.append(("launch_viewer", host, port))
        if "launch_error" in overrides:
            raise overrides["launch_error"]
        return SimpleNamespace(pid=99)

    def wait_target(children, timeout_s):
        events.append(("wait_target", list(children), timeout_s))
        return target

    def fit(t):
        events.append(("fit", t.child))
        return t

    def grab(hwnd):
        events.append(("grab", hwnd))
        return overrides.get("grab_result", frame())

    ops = connect.Ops(
        ensure_standard=ensure_standard, find_viewers=find_viewers, launch_viewer=launch_viewer,
        confirm_unencrypted=confirm,
        wait_target=wait_target, pid_of=lambda hwnd: 99 if hwnd == 11 else 5,
        client_width=lambda hwnd: overrides.get("widths", {}).get(hwnd, 0),
        fit=fit, grab=grab, clock=clock, sleep=clock.sleep)
    ops.confirmed = confirmed
    return ops, viewer, target


class AcquireTargetTest(unittest.TestCase):
    def test_ensure_standard_runs_before_find_viewers(self):
        events = []
        ops, viewer, target = make_ops(events, found=[SimpleNamespace(top=11, child=12)])
        connect.acquire_target("100.1.1.1", 5900, "", ops)
        names = [e[0] for e in events]
        self.assertEqual(names[:2], ["ensure_standard", "find_viewers"])
        self.assertEqual(events[0], ("ensure_standard", "100.1.1.1", 5900))

    def test_reuses_existing_viewer_widest_client_first_without_launching(self):
        events = []
        found = [SimpleNamespace(top=1, child=10), SimpleNamespace(top=2, child=20)]
        ops, _, target = make_ops(events, found=found, widths={10: 784, 20: 800})
        self.assertIs(connect.acquire_target("h", 5900, "", ops), target)
        self.assertNotIn("launch_viewer", [e[0] for e in events])
        self.assertEqual(events[-1][1], [20, 10])

    def test_launches_when_none_and_waits_for_the_new_process_window(self):
        events = []
        ops, _, target = make_ops(events)
        calls = {"n": 0}
        real_find = ops.find_viewers

        def find_viewers(title=None):
            calls["n"] += 1
            real_find(title)
            return [SimpleNamespace(top=11, child=12)] if calls["n"] >= 3 else []

        ops.find_viewers = find_viewers
        self.assertIs(connect.acquire_target("h", 5900, "pw", ops), target)
        self.assertTrue(ops.confirmed.wait(2))
        names = [e[0] for e in events]
        self.assertLess(names.index("ensure_standard"), names.index("launch_viewer"))
        self.assertLess(names.index("launch_viewer"), names.index("confirm"))
        self.assertNotIn("pw", repr(events))

    def test_confirm_dialog_fallback_does_not_block_the_window_wait(self):
        events = []
        ops, _, target = make_ops(events, found=[])
        release = threading.Event()
        started = threading.Event()

        def slow_confirm(pid, secs):
            started.set()
            release.wait(5)

        ops.confirm_unencrypted = slow_confirm
        ops.find_viewers = lambda title=None: [SimpleNamespace(top=11, child=12)] if title is None else []
        try:
            self.assertIs(connect.acquire_target("h", 5900, "", ops), target)
            self.assertTrue(started.wait(2))
        finally:
            release.set()

    def test_reopen_after_killing_nonstandard_viewer_is_logged(self):
        events = []
        ops, _, _ = make_ops(events, killed=[1234])
        ops.find_viewers = lambda title=None: [SimpleNamespace(top=11, child=12)] if title is None else []
        with patch("kmtool.connect.log") as log:
            connect.acquire_target("1.2.3.4", 5900, "", ops)
        log.assert_called_once_with("已按标准参数重开查看器 1.2.3.4::5900")

    def test_no_reopen_log_when_nothing_was_killed(self):
        events = []
        ops, _, _ = make_ops(events)
        ops.find_viewers = lambda title=None: [SimpleNamespace(top=11, child=12)] if title is None else []
        with patch("kmtool.connect.log") as log:
            connect.acquire_target("1.2.3.4", 5900, "", ops)
        log.assert_not_called()

    def test_viewer_settings_error_reaches_the_caller_with_its_text(self):
        events = []
        err = ViewerSettingsError("查看器设置不对：-scale=150（应为 100）")
        ops, _, _ = make_ops(events, launch_error=err)
        with self.assertRaises(ViewerSettingsError) as ctx:
            connect.acquire_target("h", 5900, "", ops)
        self.assertEqual(str(ctx.exception), "查看器设置不对：-scale=150（应为 100）")

    def test_no_window_for_the_new_process_times_out(self):
        events = []
        ops, _, _ = make_ops(events)
        with self.assertRaisesRegex(RuntimeError, "未出现已连接的画面窗口"):
            connect.acquire_target("h", 5900, "", ops, window_timeout=3)


class AddressViewersTest(unittest.TestCase):
    CMD = r'"C:\Program Files\TightVNC\tvnviewer.exe" -host={host} -port={port} -scale=100'

    def targets(self, line, host="100.1.1.1", port=5900, endpoints=None):
        return connect.viewer_targets(1, host, port, lambda pid: line,
                                      endpoints or (lambda pid: []))

    def test_matches_host_and_port_from_the_command_line(self):
        self.assertTrue(self.targets(self.CMD.format(host="100.1.1.1", port=5900)))

    def test_other_host_or_port_does_not_match(self):
        self.assertFalse(self.targets(self.CMD.format(host="100.1.1.2", port=5900)))
        self.assertFalse(self.targets(self.CMD.format(host="100.1.1.1", port=5901)))

    def test_missing_port_means_5900(self):
        self.assertTrue(self.targets(r'"x\tvnviewer.exe" -host=100.1.1.1'))
        self.assertFalse(self.targets(r'"x\tvnviewer.exe" -host=100.1.1.1', port=5901))

    def test_unreadable_command_line_is_never_reused(self):
        self.assertFalse(connect.viewer_targets(1, "100.1.1.1", 5900, lambda pid: None, lambda pid: []))

    def test_without_host_argument_uses_the_tcp_connection(self):
        line = r'"x\tvnviewer.exe"'
        self.assertTrue(self.targets(line, endpoints=lambda pid: [("100.1.1.1", 5900)]))
        self.assertFalse(self.targets(line, endpoints=lambda pid: [("100.1.1.9", 5900)]))

    def test_address_viewers_filters_by_process_not_by_title(self):
        a, b = SimpleNamespace(top=1, child=10), SimpleNamespace(top=2, child=20)
        got = connect.address_viewers("h", 5900, lambda: [a, b], lambda hwnd: hwnd,
                                      targets=lambda pid, host, port: pid == 2 and (host, port) == ("h", 5900))
        self.assertEqual(got, [b])

    def test_acquire_reuses_a_viewer_whose_title_has_no_ip(self):
        events = []
        ops, _, target = make_ops(events)
        ops.address_viewers = lambda host, port: (events.append(("address_viewers", host, port)) or
                                                  [SimpleNamespace(top=11, child=12)])
        self.assertIs(connect.acquire_target("100.1.1.1", 5900, "", ops), target)
        self.assertIn(("address_viewers", "100.1.1.1", 5900), events)
        self.assertNotIn("launch_viewer", [e[0] for e in events])

    def test_app_passes_address_viewers_to_the_pipeline(self):
        from kmtool import app
        source = inspect.getsource(app.KmTool._connect_ops)
        self.assertIn("address_viewers", source)


class PrepareTargetTest(unittest.TestCase):
    def test_fits_to_standard_before_judging_the_frame(self):
        events = []
        ops, _, target = make_ops(events)
        self.assertIs(connect.prepare_target(target, ops), target)
        self.assertEqual([e[0] for e in events], ["fit", "grab"])
        self.assertEqual(events[1], ("grab", 12))

    def test_blank_frame_never_binds(self):
        events = []
        ops, _, target = make_ops(events, grab_result=frame(fill=(0, 0, 0)))
        with self.assertRaises(connect.FrameNotReady):
            connect.prepare_target(target, ops, timeout_s=3)

    def test_cancel_is_honoured(self):
        events = []
        ops, _, target = make_ops(events, grab_result=frame(fill=(0, 0, 0)))
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(connect.Cancelled):
            connect.prepare_target(target, ops, cancel=cancel)


class StandardProblemsTest(unittest.TestCase):
    STD = r'"C:\Program Files\TightVNC\tvnviewer.exe" -host=1.2.3.4 -port=5900 -jpegimagequality=-1 -scale=100 -encoding=tight -unencrypted=allow'

    def test_standard_command_line_has_no_problem(self):
        self.assertEqual(connect.standard_problems(1, lambda pid: self.STD), [])

    def test_scale_150_is_reported(self):
        line = self.STD.replace("-scale=100", "-scale=150")
        problems = connect.standard_problems(1, lambda pid: line)
        self.assertEqual(len(problems), 1)
        self.assertIn("-scale=150", problems[0])

    def test_unreadable_command_line_is_not_a_problem(self):
        self.assertEqual(connect.standard_problems(1, lambda pid: None), [])

    def test_password_never_appears_in_reasons(self):
        line = self.STD.replace("-scale=100", "-scale=150") + " -password=hunter2"
        self.assertNotIn("hunter2", " ".join(connect.standard_problems(1, lambda pid: line)))


class AppWiringTest(unittest.TestCase):
    """kmtool/app.py 里不能再有按系统缩放率猜尺度或拒绝的分支（静态检查，不启动界面）。"""

    def setUp(self):
        self.tree = ast.parse(APP.read_text(encoding="utf-8"))

    def function(self, name):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail(f"app.py 缺少 {name}")

    def calls(self, fn):
        out = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                f = node.func
                out.append(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
        return out

    def test_no_dpi_branch_in_binding_path(self):
        for name in ("standard_client", "fit_standard", "add_worker", "_ensure_geometry", "_run_bind"):
            self.assertNotIn("display_scale", self.calls(self.function(name)), name)
            self.assertNotIn("GetDpiForSystem", self.calls(self.function(name)), name)

    def test_add_worker_creates_one_to_one_worker(self):
        fn = self.function("add_worker")
        worker_calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                        and getattr(n.func, "id", "") == "InputWorker"]
        self.assertEqual(len(worker_calls), 1)
        kwargs = {k.arg: k.value for k in worker_calls[0].keywords}
        for key in ("input_scale", "capture_scale"):
            self.assertIsInstance(kwargs[key], ast.Constant)
            self.assertEqual(kwargs[key].value, 1.0)

    def test_launch_bind_goes_through_acquire_target(self):
        source = ast.get_source_segment(APP.read_text(encoding="utf-8"), self.function("launch_bind"))
        self.assertIn("connect.acquire_target", source)
        self.assertNotIn("find_viewers", source)

    def test_run_bind_prepares_frame_before_binding(self):
        source = ast.get_source_segment(APP.read_text(encoding="utf-8"), self.function("_run_bind"))
        self.assertLess(source.index("prepare_target"), source.index("add_worker"))

    def test_standard_client_is_constant(self):
        from kmtool import app
        for dpi_scale in (1.0, 1.5, 2.0):
            with patch.object(app, "display_scale", return_value=dpi_scale):
                self.assertEqual(app.standard_client(), (800, 600))


if __name__ == "__main__":
    unittest.main()
