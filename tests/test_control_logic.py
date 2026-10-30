"""控制层纯逻辑单元测试：不依赖大漠、真机、管理员权限。

运行：  python -m unittest discover -s tests -v   （在项目根目录，任意位数的 Python 都行）
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from common import estop  # noqa: E402
from control.actuator import Actuator  # noqa: E402
from control.guard import evaluate_l0  # noqa: E402
from control.viewer_link import plan_window_size  # noqa: E402
from control.viewport import Viewport  # noqa: E402


class FakeChannel:
    def __init__(self):
        self.events = []

    def move_to(self, x, y): self.events.append(("move", x, y))
    def down(self, b): self.events.append(("down", b))
    def up(self, b): self.events.append(("up", b))
    def key_down(self, k): self.events.append(("kd", k))
    def key_up(self, k): self.events.append(("ku", k))


class FakeTime:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def clock(self): return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def make_actuator(latency=0.8):
    ft = FakeTime()
    ch = FakeChannel()
    act = Actuator(ch, Viewport(), latency, sleep=ft.sleep, clock=ft.clock, rng=random.Random(1))
    return act, ch, ft


class ViewportTests(unittest.TestCase):
    def test_roundtrip_with_origin(self):
        vp = Viewport(origin=(8, 8))
        self.assertEqual(vp.g_to_v(10, 20), (18, 28))
        self.assertEqual(vp.v_to_g(*vp.g_to_v(10, 20)), (10, 20))

    def test_bounds(self):
        vp = Viewport()
        self.assertTrue(vp.contains_g(0, 0))
        self.assertTrue(vp.contains_g(799, 599))
        self.assertFalse(vp.contains_g(800, 0))
        self.assertFalse(vp.contains_g(0, -1))
        with self.assertRaises(ValueError):
            vp.check_g(800, 599)


class WindowPlanTests(unittest.TestCase):
    def test_plan_keeps_frame_delta(self):
        # 实测：顶层外框 2590x1560 时子窗口客户区 2564x1460，差 (26, 100)
        self.assertEqual(plan_window_size((2590, 1560), (2564, 1460), (816, 616)), (842, 716))

    def test_plan_when_already_right(self):
        self.assertEqual(plan_window_size((842, 716), (816, 616), (816, 616)), (842, 716))


class GuardL0Tests(unittest.TestCase):
    EXPECT = (816, 616)

    def test_pass_with_uniform_band(self):
        r = evaluate_l0(self.EXPECT, self.EXPECT, False, ["000000"] * 6, ["000000"] * 6)
        self.assertTrue(r.ok, r.detail)

    def test_wrong_client_size(self):
        r = evaluate_l0((2564, 1460), self.EXPECT, False, [], [])
        self.assertFalse(r.ok)
        self.assertIn("2564x1460", r.detail)

    def test_minimized(self):
        self.assertFalse(evaluate_l0((0, 0), self.EXPECT, True, [], []).ok)

    def test_band_with_game_pixels_fails(self):
        # 帧缓冲比 800x600 大：边带里会出现游戏像素，不是纯色
        r = evaluate_l0(self.EXPECT, self.EXPECT, False, ["000000", "a1b2c3", "000000"], ["000000"])
        self.assertFalse(r.ok)

    def test_known_background_mismatch(self):
        r = evaluate_l0(self.EXPECT, self.EXPECT, False, ["111111"] * 3, ["111111"] * 3, bg="222222")
        self.assertFalse(r.ok)

    def test_band_color_case_insensitive(self):
        r = evaluate_l0(self.EXPECT, self.EXPECT, False, ["AABBCC", "aabbcc"], ["AaBbCc"])
        self.assertTrue(r.ok)

    def test_empty_color_means_capture_failed(self):
        self.assertFalse(evaluate_l0(self.EXPECT, self.EXPECT, False, [""], ["000000"]).ok)


class ActuatorTests(unittest.TestCase):
    def setUp(self):
        estop.clear()

    def test_click_order_and_coordinates(self):
        act, ch, _ = make_actuator()
        act.vp.origin = (8, 8)
        act._cursor.mark_moved(100, 50)
        act.click(100, 50, box=(1, 1))
        self.assertEqual(ch.events, [("move", 108, 58), ("down", "left"), ("up", "left")])

    def test_click_out_of_bounds_sends_nothing(self):
        act, ch, _ = make_actuator()
        with self.assertRaises(ValueError):
            act.click(800, 10)
        self.assertEqual(ch.events, [])

    def test_click_releases_button_even_if_interrupted(self):
        act, ch, _ = make_actuator()

        calls = {"n": 0}

        def boom_on_hold(*_args):  # 第 1 次是按下前的停顿，第 2 次是按住期间
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("interrupted")
        act._wait = boom_on_hold
        with self.assertRaises(RuntimeError):
            act.click(10, 10)
        down = ch.events.index(("down", "left"))
        self.assertEqual(ch.events[down:down + 2], [("down", "left"), ("up", "left")])

    def test_interrupt_before_press_sends_no_button_events(self):
        act, ch, _ = make_actuator()
        act._cursor.mark_moved(10, 10)

        def boom(*_args):
            raise RuntimeError("interrupted")
        act._wait = boom
        with self.assertRaises(RuntimeError):
            act.click(10, 10, box=(1, 1))
        self.assertEqual([e for e in ch.events if e[0] == "move"], [("move", 10, 10)])
        self.assertFalse(any(e[0] == "down" for e in ch.events))

    def test_combo_always_releases_modifier(self):
        act, ch, _ = make_actuator()
        act.key_combo("alt", "e")
        self.assertEqual(ch.events[0], ("kd", "alt"))
        self.assertEqual(ch.events[-1], ("ku", "alt"))

    def test_act_until_succeeds_after_polls(self):
        act, _, ft = make_actuator()
        calls = {"action": 0, "done": 0}

        def action(): calls["action"] += 1

        def done():
            calls["done"] += 1
            return calls["done"] >= 3
        self.assertTrue(act.act_until(action, done, timeout_s=5))
        self.assertEqual(calls["action"], 1)

    def test_act_until_retries_then_gives_up(self):
        act, _, _ = make_actuator()
        n = {"a": 0}

        def action(): n["a"] += 1
        self.assertFalse(act.act_until(action, lambda: False, timeout_s=1, retries=2))
        self.assertEqual(n["a"], 3)

    def test_act_until_retry_can_succeed(self):
        act, _, _ = make_actuator()
        n = {"a": 0}

        def action(): n["a"] += 1
        self.assertTrue(act.act_until(action, lambda: n["a"] >= 2, timeout_s=1, retries=2))
        self.assertEqual(n["a"], 2)

    def test_timeout_scales_with_latency(self):
        slow, _, _ = make_actuator(latency=2.0)
        fast, _, _ = make_actuator(latency=0.2)
        self.assertGreater(slow.default_timeout(), fast.default_timeout())
        self.assertGreaterEqual(fast.default_timeout(), 2.0)

    def test_estop_aborts_wait(self):
        act, _, _ = make_actuator()
        estop.trigger("test")
        try:
            with self.assertRaises(estop.Aborted):
                act.wait_until(lambda: False, 10)
            with self.assertRaises(estop.Aborted):
                act.click(1, 1)
        finally:
            estop.clear()

    def test_release_modifiers_sends_all(self):
        act, ch, _ = make_actuator()
        act.release_modifiers()
        self.assertEqual([e for e in ch.events if e[0] == "ku"],
                         [("ku", "alt"), ("ku", "ctrl"), ("ku", "shift"), ("ku", "win")])




if __name__ == "__main__":
    unittest.main()


class DetectShareModeTest(unittest.TestCase):
    """用假的窗口与取色函数验证：画面对了判 shared，一直不对判 desktop 并还原窗口。"""

    def setUp(self):
        from control import guard
        self.guard = guard
        self._orig = (guard.winapi.client_size, guard.winapi.is_iconic)
        self.size = [2564, 1460]
        guard.winapi.client_size = lambda h: tuple(self.size)
        guard.winapi.is_iconic = lambda h: False

    def tearDown(self):
        self.guard.winapi.client_size, self.guard.winapi.is_iconic = self._orig

    class H:
        top = 1
        child = 2

    def _run(self, band_color):
        class Ch:
            def get_color(self_, x, y):
                return band_color(x, y)
        now = [0.0]

        def fit(h, target):
            self.size[:] = list(target)

        def sleep(s):
            now[0] += s
        return self.guard.detect_share_mode(Ch(), self.H(), (816, 616), (800, 600), fit,
                                            sleep=sleep, clock=lambda: now[0], timeout_s=5, poll_s=1)

    def test_shared_when_bands_uniform(self):
        r = self._run(lambda x, y: "000000")
        self.assertEqual(r.mode, "shared")
        self.assertEqual(tuple(self.size), (816, 616))

    def test_desktop_when_bands_noisy_and_window_restored(self):
        r = self._run(lambda x, y: "%06x" % (x * 7919 + y * 104729 & 0xFFFFFF))
        self.assertEqual(r.mode, "desktop")
        self.assertEqual(tuple(self.size), (2564, 1460))


class ViewportScaleTest(unittest.TestCase):
    def test_scale_two(self):
        from control.viewport import Viewport
        vp = Viewport(input_scale=1.0, capture_scale=2.0)
        self.assertEqual(vp.client_size(), (1600, 1200))
        self.assertEqual(vp.input_size(), (800, 600))
        self.assertEqual(vp.g_to_v(400, 300), (400, 300))
        self.assertEqual(vp.v_to_g(400, 300), (400, 300))
        self.assertEqual(vp.g_to_capture(400, 300), (800, 600))
        self.assertEqual(vp.capture_to_g(800, 600), (400, 300))
        self.assertEqual(vp.region_to_v(0, 0, 10, 20), (0, 0, 10, 20))
        self.assertEqual(vp.region_to_capture(0, 0, 10, 20), (0, 0, 20, 40))

    def test_scale_one_with_origin(self):
        from control.viewport import Viewport
        vp = Viewport(origin=(5, 7))
        self.assertEqual(vp.g_to_v(1, 2), (6, 9))
        self.assertEqual(vp.v_to_g(6, 9), (1, 2))


class L0FitTest(unittest.TestCase):
    """现行 L0（第一层 v1.6）：未最小化，且画面子窗口客户区恰好 800x600 物理像素。"""

    def setUp(self):
        from control import guard
        self.guard = guard
        self._orig = (guard.winapi.client_size, guard.winapi.is_iconic)
        self.size = [800, 600]
        self.iconic = False
        guard.winapi.client_size = lambda h: tuple(self.size)
        guard.winapi.is_iconic = lambda h: self.iconic

    def tearDown(self):
        self.guard.winapi.client_size, self.guard.winapi.is_iconic = self._orig

    class H:
        top = 1
        child = 2

    def test_exact_800x600_passes(self):
        self.assertTrue(self.guard.run_l0_fit(self.H(), (800, 600)).ok)

    def test_any_other_size_fails(self):
        for size in ([1600, 1200], [800, 599], [784, 585], [816, 616]):
            self.size[:] = size
            self.assertFalse(self.guard.run_l0_fit(self.H(), (800, 600)).ok, size)

    def test_minimized_fails_even_if_size_matches(self):
        self.iconic = True
        self.assertFalse(self.guard.run_l0_fit(self.H(), (800, 600)).ok)




