import inspect
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from common import estop
from control.actuator import Actuator
from control.cursor import CursorTracker
from control.humanize import Profile
from control.viewport import Viewport


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeChannel:
    def __init__(self):
        self.events = []
        self.validations = 0

    def assert_target(self):
        self.validations += 1

    def move_to(self, x, y): self.events.append(("move", x, y))
    def down(self, button): self.events.append(("down", button))
    def up(self, button): self.events.append(("up", button))
    def wheel(self, notch): self.events.append(("wheel", notch))
    def key_down(self, key): self.events.append(("kd", key))
    def key_up(self, key): self.events.append(("ku", key))
    def press(self, key): self.events.append(("press", key))


def make_actuator():
    ft = FakeTime()
    ch = FakeChannel()
    cursor = CursorTracker(clock=ft.clock, min_detect_interval=999)
    cursor.mark_moved(10, 10)
    act = Actuator(ch, Viewport(), sleep=ft.sleep, clock=ft.clock,
                   rng=random.Random(7), cursor=cursor)
    return act, ch, ft


def draw_cursor(rgb, width, height, gx, gy):
    """画一个合成水晶光标：7x7 绿宝石 + 金边 + 宝石左上方的蓝色身体（真光标约 100 个蓝像素）。"""
    x0, y0 = gx - 3, gy - 3
    for y in range(gy - 12, gy - 2):
        for x in range(gx - 12, gx - 2):
            if 0 <= x < width and 0 <= y < height:
                i = (y * width + x) * 3
                rgb[i:i + 3] = bytes((30, 60, 200))
    for y in range(y0 - 1, y0 + 8):
        for x in range(x0 - 1, x0 + 8):
            if 0 <= x < width and 0 <= y < height:
                i = (y * width + x) * 3
                solid = x0 <= x < x0 + 7 and y0 <= y < y0 + 7
                rgb[i:i + 3] = bytes((170, 245, 110) if solid else (240, 200, 60))


def visual_capture(act, offset):
    """按“箭头尖 = 指针 + offset”合成 800x600 画面，宝石在箭尖右下 (13,14)。"""
    width, height = 800, 600

    def capture():
        pos = act.cursor()
        if pos is None:
            return None
        rgb = bytearray(bytes((60, 60, 60)) * (width * height))
        draw_cursor(rgb, width, height, pos[0] + offset[0] + 13, pos[1] + offset[1] + 14)
        return bytes(rgb), width, height

    return capture


class ActuatorActionTests(unittest.TestCase):
    def setUp(self):
        estop.clear()

    def tearDown(self):
        estop.clear()

    def test_move_has_intermediate_points_and_exact_target(self):
        act, ch, _ = make_actuator()
        act.move_to(300, 200)
        moves = [e for e in ch.events if e[0] == "move"]
        self.assertGreater(len(moves), 2)
        self.assertEqual(moves[-1], ("move", 300, 200))
        self.assertEqual(act.cursor(), (300, 200))
        self.assertEqual(act.last_path[-1][:2], (300, 200))

    def test_click_double_click_and_hover_sequences(self):
        act, ch, _ = make_actuator()
        act.click(30, 40, box=(1, 1))
        self.assertEqual([e for e in ch.events if e[0] in ("down", "up")],
                         [("down", "left"), ("up", "left")])
        ch.events.clear()
        act.double_click(30, 40, box=(1, 1))
        self.assertEqual([e for e in ch.events if e[0] in ("down", "up")],
                         [("down", "left"), ("up", "left"), ("down", "left"), ("up", "left")])
        ch.events.clear()
        act.hover(30, 40, dwell_ms=(1, 1))
        self.assertFalse(any(e[0] in ("down", "up") for e in ch.events))

    def test_drag_holds_button_during_second_path(self):
        act, ch, _ = make_actuator()
        act.drag(20, 20, 200, 180)
        kinds = [e[0] for e in ch.events]
        down = kinds.index("down")
        up = kinds.index("up", down)
        self.assertGreater(up - down, 3)
        self.assertEqual(ch.events[up - 1], ("move", 200, 180))

    def test_wheel_and_type_text(self):
        act, ch, _ = make_actuator()
        act.wheel(50, 60, -3)
        self.assertEqual([e for e in ch.events if e[0] == "wheel"], [("wheel", -1)] * 3)
        ch.events.clear()
        act.type_text("Ab 1!")
        self.assertEqual([e for e in ch.events if e[0] == "press"],
                         [("press", c) for c in "Ab 1!"])
        with self.assertRaises(ValueError):
            act.type_text("中文")

    def test_single_key_and_combo_are_paired_in_reverse_order(self):
        act, ch, _ = make_actuator()
        act.key_press("F1")
        self.assertEqual(ch.events, [("kd", "f1"), ("ku", "f1")])
        ch.events.clear()
        act.key_combo("alt", "ctrl", "e")
        self.assertEqual(ch.events, [("kd", "alt"), ("kd", "ctrl"), ("kd", "e"),
                                     ("ku", "e"), ("ku", "ctrl"), ("ku", "alt")])

    def test_estop_during_path_releases_everything(self):
        act, ch, _ = make_actuator()
        original = ch.move_to

        def stop_after_first(x, y):
            original(x, y)
            estop.trigger("test")
        ch.move_to = stop_after_first
        with self.assertRaises(estop.Aborted):
            act.move_to(200, 200)
        self.assertTrue(all(("ku", key) in ch.events for key in ("alt", "ctrl", "shift", "win")))
        self.assertIn(("up", "left"), ch.events)
        self.assertIn(("up", "right"), ch.events)
        self.assertIsNone(act.cursor())

    def test_target_is_revalidated_for_each_action(self):
        act, ch, _ = make_actuator()
        act.move_to(20, 20)
        act.key_press("a")
        act.click(30, 30)
        self.assertEqual(ch.validations, 3)

    def test_public_movement_apis_have_no_start_parameter(self):
        for name in ("move_to", "click", "double_click", "hover", "wheel"):
            params = inspect.signature(getattr(Actuator, name)).parameters
            self.assertNotIn("start", params)
            self.assertNotIn("x1", params)
            self.assertNotIn("y1", params)

    def test_lan_mode_uses_natural_profile_without_changing_viewport(self):
        ft = FakeTime()
        ch = FakeChannel()
        cursor = CursorTracker(clock=ft.clock, min_detect_interval=999)
        cursor.mark_moved(10, 10)
        act = Actuator(ch, Viewport(), sleep=ft.sleep, clock=ft.clock,
                       rng=random.Random(3), cursor=cursor, lan_mode=True)
        self.assertEqual((act.vp.input_scale, act.vp.capture_scale), (1.0, 1.0))
        self.assertEqual(act.profile.sample_ms, Profile.lan().sample_ms)
        self.assertEqual(act._wait_plan(), (Actuator.LAN_ALIGN_MIN_WAIT_S,
                                            Actuator.LAN_ALIGN_STABLE_POLLS))


class ArrowAlignTests(unittest.TestCase):
    def setUp(self):
        estop.clear()

    def tearDown(self):
        estop.clear()

    def make_visual(self, offset):
        act, ch, ft = make_actuator()
        act.visual_align = True
        act._capture = visual_capture(act, offset)
        return act, ch, ft

    def test_lan_visual_alignment_keeps_arrow_tip_within_tolerance(self):
        act, _ch, ft = self.make_visual((5, 20))
        act.lan_mode = True
        act.profile = Profile.lan()
        act.move_to(300, 200)
        self.assertTrue(act.last_align_ok)
        self.assertLessEqual(max(abs(act.last_align_error[0]),
                                 abs(act.last_align_error[1])), 3)
        self.assertNotEqual(act.cursor(), (295, 180))
        self.assertLess(ft.now, 3.0)

    def test_lan_terminal_correction_leaves_a_small_confirmed_settle(self):
        # 进入最后 24px 时先欠修正一小段，再用下一帧确认，避免末段像一次计算后的硬刹停。
        act, _ch, _ft = self.make_visual((5, 15))
        act.lan_mode = True
        act.profile = Profile.lan()
        act.move_to(300, 200)
        self.assertTrue(act.last_align_ok)
        self.assertLessEqual(max(abs(act.last_align_error[0]),
                                 abs(act.last_align_error[1])), 3)
        self.assertNotEqual(act.cursor(), (295, 185))
        self.assertTrue(act.last_align_path)

    def test_lan_moves_from_near_target_to_target_in_smaller_steps(self):
        # 初次只落到目标附近，之后每次看到的箭尖误差都应继续缩小。
        act, _ch, _ft = self.make_visual((5, 20))
        act.lan_mode = True
        act.profile = Profile.lan()
        original = act._traverse
        sent = []

        def record(*args, **kwargs):
            original(*args, **kwargs)
            sent.append((args[0], args[1], kwargs.get("correction", False)))

        act._traverse = record
        act.move_to(300, 200)
        self.assertGreaterEqual(len(sent), 3)
        self.assertFalse(sent[0][2])
        self.assertNotEqual(sent[0][:2], (300, 200))
        errors = [max(abs(x + 5 - 300), abs(y + 20 - 200))
                  for x, y, _ in sent]
        self.assertEqual(errors, sorted(errors, reverse=True))
        self.assertTrue(act.last_align_ok)

    def test_click_aligns_arrow_tip_before_press(self):
        act, ch, _ = self.make_visual((5, 20))
        act.click(300, 200, box=(1, 1))
        down = [e[0] for e in ch.events].index("down")
        up = [e[0] for e in ch.events].index("up")
        moves = [e for e in ch.events[:down] if e[0] == "move"]
        self.assertEqual(moves[-1], ("move", 295, 180))
        self.assertFalse(any(e[0] == "move" for e in ch.events[up + 1:]))
        self.assertEqual(act.last_click, (300, 200))
        self.assertEqual(act.last_send, (295, 180))
        self.assertIsNotNone(act.last_align)
        self.assertLessEqual(abs(act.last_align[0] - 300), 1)
        self.assertLessEqual(abs(act.last_align[1] - 200), 1)
        self.assertTrue(act.last_align_path)
        self.assertEqual(act.last_path[-1][:2], self.first_send(300, 200))

    def first_send(self, tx, ty):
        return (tx - Actuator.ARROW_OFFSET[0], ty - Actuator.ARROW_OFFSET[1])

    def test_default_offset_is_one_to_one(self):
        # 1:1 真机九点实测箭尖就在发送点上，旧的 (10,11) 是 2× 时代的数值
        self.assertEqual(Actuator.ARROW_OFFSET, (0, 0))

    def test_exact_default_offset_needs_no_correction(self):
        act, _ch, _ = self.make_visual(Actuator.ARROW_OFFSET)
        act.click(300, 200, box=(1, 1))
        self.assertEqual(act.cursor(), self.first_send(300, 200))
        self.assertEqual(act.last_align_path, [])
        self.assertLessEqual(abs(act.last_align[0] - 300), 1)
        self.assertLessEqual(abs(act.last_align[1] - 200), 1)

    def test_click_without_capture_uses_plain_target(self):
        act, ch, _ = make_actuator()
        act.visual_align = True
        act.click(300, 200, box=(1, 1))
        moves = [e for e in ch.events if e[0] == "move"]
        self.assertEqual(moves[-1], ("move", 300, 200))
        self.assertIsNone(act.last_align)

    def test_unreadable_capture_falls_back_to_exact_target(self):
        act, ch, _ = self.make_visual((5, 20))
        act._capture = lambda: None
        act.click(300, 200, box=(1, 1))
        down = [e[0] for e in ch.events].index("down")
        moves = [e for e in ch.events[:down] if e[0] == "move"]
        self.assertEqual(moves[-1], ("move", 300, 200))
        self.assertEqual(act.last_send, (300, 200))
        self.assertIsNone(act.last_align)
        self.assertIn(("down", "left"), ch.events)

    def test_frame_without_crystal_falls_back_to_exact_target(self):
        act, ch, _ = self.make_visual((5, 20))
        blank = bytes((60, 60, 60)) * (800 * 600)
        act._capture = lambda: (blank, 800, 600)
        act.click(300, 200, box=(1, 1))
        down = [e[0] for e in ch.events].index("down")
        moves = [e for e in ch.events[:down] if e[0] == "move"]
        self.assertEqual(moves[-1], ("move", 300, 200))
        self.assertIsNone(act.last_align)

    def test_move_to_aligns_without_post_settle(self):
        act, ch, _ = self.make_visual((5, 20))
        act.move_to(300, 200)
        self.assertEqual(act.cursor(), (295, 180))
        self.assertIsNotNone(act.last_align)
        self.assertEqual(act.last_path[-1][:2], self.first_send(300, 200))
        self.assertEqual(ch.events[-1], ("move", 295, 180))

    def test_double_click_aligns_once_and_never_moves_between(self):
        act, ch, _ = self.make_visual((5, 20))
        act.double_click(300, 200, box=(1, 1))
        buttons = [e for e in ch.events if e[0] in ("down", "up")]
        self.assertEqual(buttons, [("down", "left"), ("up", "left"),
                                   ("down", "left"), ("up", "left")])
        first_down = ch.events.index(("down", "left"))
        self.assertFalse(any(e[0] == "move" for e in ch.events[first_down:]))
        self.assertEqual(act.cursor(), (295, 180))

    def test_edge_target_clamps_and_still_clicks(self):
        act, ch, _ = self.make_visual((10, 11))
        act.click(2, 2, box=(1, 1))
        self.assertIn(("down", "left"), ch.events)
        self.assertIsNotNone(act.last_align_error)
        self.assertGreater(act.last_align_error[0], 3)

    def test_two_corrections_converge_before_press(self):
        act, ch, _ = make_actuator()
        act.visual_align = True
        state = {"pointer": None, "tip": None}

        def capture():
            pos = act.cursor()
            if pos is None or act.last_send is None:
                return None
            if state["pointer"] is None:
                state["tip"] = (pos[0] + 5, pos[1] + 20)
            else:
                dx = pos[0] - state["pointer"][0]
                dy = pos[1] - state["pointer"][1]
                state["tip"] = (state["tip"][0] + round(dx * 0.6),
                                state["tip"][1] + round(dy * 0.6))
            state["pointer"] = pos
            rgb = bytearray(bytes((60, 60, 60)) * (800 * 600))
            draw_cursor(rgb, 800, 600, state["tip"][0] + 13, state["tip"][1] + 14)
            return bytes(rgb), 800, 600

        act._capture = capture
        act.click(300, 200, box=(1, 1))
        self.assertEqual(state["pointer"], act.cursor())
        self.assertLessEqual(abs(act.last_align[0] - 300), 3)
        self.assertLessEqual(abs(act.last_align[1] - 200), 3)
        down = [e[0] for e in ch.events].index("down")
        self.assertFalse(any(e[0] == "move" for e in ch.events[down + 1:]))

    def test_large_landing_error_is_corrected(self):
        act, ch, _ = self.make_visual((60, -55))
        act.click(300, 200, box=(1, 1))
        self.assertEqual(act.cursor(), (240, 255))
        self.assertLessEqual(abs(act.last_align[0] - 300), 3)
        self.assertLessEqual(abs(act.last_align[1] - 200), 3)
        down = [e[0] for e in ch.events].index("down")
        self.assertFalse(any(e[0] == "move" for e in ch.events[down + 1:]))

    def test_correction_is_a_short_smooth_move_not_a_second_long_trajectory(self):
        # 纯竖直的偏差：修正应笔直向上收，不来回抖、不拐大弯（plan_path 的噪声和弧度会做到）
        act, _ch, _ = self.make_visual((0, 20))
        act.move_to(300, 200)
        path = act.last_align_path
        self.assertGreaterEqual(len(path), 2)
        self.assertEqual(path[0][:2], (300, 200))
        self.assertEqual(path[-1][:2], (300, 180))
        self.assertTrue(all(299 <= x <= 301 for x, _, _ in path))
        ys = [y for _, y, _ in path]
        self.assertEqual(ys, sorted(ys, reverse=True))
        self.assertLessEqual(sum(dt for _, _, dt in path), 400)

    def test_reaction_pause_comes_before_each_correction(self):
        def run(reaction):
            act, _ch, ft = self.make_visual((5, 20))
            act.profile.reaction_ms = reaction
            act.move_to(300, 200)
            return ft.now

        self.assertGreaterEqual(run((400, 400)) - run((0, 0)), 0.39)

    def test_reaction_pause_is_not_applied_when_no_correction_is_needed(self):
        def run(reaction):
            act, _ch, ft = self.make_visual(Actuator.ARROW_OFFSET)
            act.profile.reaction_ms = reaction
            act.move_to(300, 200)
            return ft.now

        self.assertAlmostEqual(run((400, 400)), run((0, 0)), places=6)

    def test_fast_display_switches_to_short_waits(self):
        act, _ch, _ = self.make_visual((5, 20))
        slow = (Actuator.ALIGN_MIN_WAIT_S, Actuator.ALIGN_STABLE_POLLS)
        self.assertEqual(act._wait_plan(), slow)
        act.move_to(300, 200)
        self.assertLessEqual(act._display_lag_s, Actuator.ALIGN_FAST_LAG_S)
        wait, polls = act._wait_plan()
        self.assertLess(wait, Actuator.ALIGN_MIN_WAIT_S)
        self.assertEqual(polls, Actuator.ALIGN_STABLE_POLLS_FAST)

    def test_frozen_display_drops_back_to_conservative_waits(self):
        act, _ch, _ = self.make_visual((5, 20))
        act.move_to(300, 200)
        self.assertLess(act._wait_plan()[0], Actuator.ALIGN_MIN_WAIT_S)
        ghost = bytearray(bytes((60, 60, 60)) * (800 * 600))
        draw_cursor(ghost, 800, 600, 305 + 13, 220 + 14)
        ghost = bytes(ghost)
        act._capture = lambda: (ghost, 800, 600)
        act.move_to(330, 230)
        self.assertFalse(act.last_align_ok)
        self.assertEqual(act._wait_plan(), (Actuator.ALIGN_MIN_WAIT_S, Actuator.ALIGN_STABLE_POLLS))

    def test_stale_ghost_cursor_is_not_trusted(self):
        # 副机刷新慢：发送后 1.5 秒内画面里还是旧位置的光标，之后才出现新位置
        act, ch, ft = make_actuator()
        act.visual_align = True
        state = {"t0": None}
        ghost = bytearray(bytes((60, 60, 60)) * (800 * 600))
        draw_cursor(ghost, 800, 600, 53, 44)
        ghost = bytes(ghost)

        def capture():
            if act.last_send is None:
                return ghost, 800, 600
            if state["t0"] is None:
                state["t0"] = ft.now
            if ft.now - state["t0"] < 1.5:
                return ghost, 800, 600
            pos = act.cursor()
            rgb = bytearray(bytes((60, 60, 60)) * (800 * 600))
            draw_cursor(rgb, 800, 600, pos[0] + 5 + 13, pos[1] + 20 + 14)
            return bytes(rgb), 800, 600

        act._capture = capture
        act.click(300, 200, box=(1, 1))
        self.assertEqual(act.cursor(), (295, 180))
        self.assertLessEqual(abs(act.last_align[0] - 300), 3)
        self.assertLessEqual(abs(act.last_align[1] - 200), 3)

    def test_cursor_lost_after_first_correction_keeps_pointer(self):
        act, ch, _ = make_actuator()
        act.visual_align = True
        calls = {"n": 0}

        def capture():
            if act.last_send is None:
                return None
            pos = act.cursor()
            rgb = bytearray(bytes((60, 60, 60)) * (800 * 600))
            if pos == (300, 200):
                draw_cursor(rgb, 800, 600, pos[0] + 40 + 13, pos[1] + 30 + 14)
            calls["n"] += 1
            return bytes(rgb), 800, 600

        act._capture = capture
        act.click(300, 200, box=(1, 1))
        self.assertEqual(act.cursor(), (300, 200))
        self.assertFalse(act.last_align_ok)
        self.assertIn(("down", "left"), ch.events)

    def test_cursor_clipped_by_right_edge_is_found_from_inside(self):
        # 靠右的目标：游戏光标落点偏右，宝石被画面边缘截掉认不出；向内收着找，再从里面修回目标
        act, ch, _ = make_actuator()
        act.visual_align = True
        offset = (60, 0)

        def capture():
            if act.last_send is None:
                return None
            pos = act.cursor()
            rgb = bytearray(bytes((60, 60, 60)) * (800 * 600))
            gem_x = pos[0] + offset[0] + 13
            if gem_x <= 786:
                draw_cursor(rgb, 800, 600, gem_x, pos[1] + offset[1] + 14)
            return bytes(rgb), 800, 600

        act._capture = capture
        act.move_to(752, 300)
        self.assertTrue(act.last_align_ok)
        self.assertEqual(act.cursor(), (692, 300))
        self.assertLessEqual(abs(act.last_align_error[0]), 3)

    def test_unresponsive_crystal_falls_back_to_exact_target(self):
        act, ch, _ = make_actuator()
        act.visual_align = True
        frozen = bytearray(bytes((60, 60, 60)) * (800 * 600))
        draw_cursor(frozen, 800, 600, 320, 210)
        frozen = bytes(frozen)

        act._capture = lambda: (frozen, 800, 600)
        act.click(300, 200, box=(1, 1))
        moves = [e for e in ch.events if e[0] == "move"]
        self.assertEqual(moves[-1], ("move", 300, 200))
        self.assertIsNone(act.last_align)
        self.assertIsNone(act.last_align_error)
        self.assertIn(("down", "left"), ch.events)

    def erratic_cursor(self, act, offsets):
        """每次发送后光标落点都不一样（历史相关）：第 k 次发送的箭尖 = 指针 + offsets[k]。"""
        sends = {"n": 0}
        original = act._traverse

        def counting(*args, **kwargs):
            sends["n"] += 1
            return original(*args, **kwargs)

        act._traverse = counting

        def capture():
            if act.last_send is None:
                return None
            pos = act.cursor()
            off = offsets[min(sends["n"] - 1, len(offsets) - 1)]
            rgb = bytearray(bytes((60, 60, 60)) * (800 * 600))
            draw_cursor(rgb, 800, 600, pos[0] + off[0] + 13, pos[1] + off[1] + 14)
            return bytes(rgb), 800, 600

        act._capture = capture
        return sends

    def test_keeps_correcting_until_within_tolerance(self):
        # 以前最多修正 6 次就停；现在一直修到进入容差为止
        act, _ch, _ = make_actuator()
        act.visual_align = True
        sends = self.erratic_cursor(act, [(40, 30), (-35, 25), (28, -22), (-20, 18), (15, -14),
                                          (-11, 10), (8, -7), (-5, 4), (3, -2)])
        act.move_to(300, 200)
        self.assertGreater(sends["n"] - 1, 6)
        self.assertTrue(act.last_align_ok)
        self.assertLessEqual(max(abs(act.last_align_error[0]), abs(act.last_align_error[1])), 3)

    def test_tolerance_is_adjustable(self):
        loose, _ch, _ = self.make_visual((5, 6))
        loose.align_tolerance = 8
        loose.move_to(300, 200)
        self.assertEqual(loose.last_align_path, [])
        self.assertTrue(loose.last_align_ok)
        strict, _ch, _ = self.make_visual((5, 6))
        strict.align_tolerance = 3
        strict.move_to(300, 200)
        self.assertTrue(strict.last_align_path)
        self.assertTrue(strict.last_align_ok)
        self.assertLessEqual(max(abs(strict.last_align_error[0]), abs(strict.last_align_error[1])), 3)

    def test_gives_up_after_max_time_and_reports_not_aligned(self):
        act, ch, ft = make_actuator()
        act.visual_align = True
        act.align_max_s = 6
        self.erratic_cursor(act, [(30, 30), (-30, -30)] * 40)
        act.click(300, 200, box=(1, 1))
        self.assertFalse(act.last_align_ok)
        self.assertLess(ft.now, 6 + 12)
        self.assertIn(("down", "left"), ch.events)

    def test_display_delay_longer_than_min_wait_does_not_run_away(self):
        # 画面比指针晚 1.5 秒：等得不够久会把旧位置当成新位置，同一个修正量被重复施加
        act, ch, ft = make_actuator()
        act.visual_align = True
        history = [(-99.0, (10, 10))]
        original = ch.move_to

        def record(x, y):
            original(x, y)
            history.append((ft.now, (x, y)))

        ch.move_to = record
        sends = {"n": 0}
        traverse = act._traverse

        def counting(*args, **kwargs):
            sends["n"] += 1
            return traverse(*args, **kwargs)

        act._traverse = counting

        def capture():
            if act.last_send is None:
                return None
            shown = history[0][1]
            for stamp, point in history:
                if stamp <= ft.now - 1.5:
                    shown = point
            rgb = bytearray(bytes((60, 60, 60)) * (800 * 600))
            draw_cursor(rgb, 800, 600, shown[0] + 5 + 13, shown[1] + 20 + 14)
            return bytes(rgb), 800, 600

        act._capture = capture
        act.move_to(300, 200)
        self.assertTrue(act.last_align_ok)
        self.assertEqual(act.cursor(), (295, 180))
        self.assertLessEqual(sends["n"], 3)
        self.assertEqual(act._wait_plan(), (Actuator.ALIGN_MIN_WAIT_S, Actuator.ALIGN_STABLE_POLLS))

    def test_cursor_reappearing_on_the_retry_is_used(self):
        act, _ch, ft = make_actuator()
        act.visual_align = True
        state = {"t": None}

        def capture():
            if act.last_send is None:
                return None
            rgb = bytearray(bytes((60, 60, 60)) * (800 * 600))
            if act.last_align_path:
                if state["t"] is None:
                    state["t"] = ft.now
                if ft.now - state["t"] < 6.0:
                    return bytes(rgb), 800, 600
            pos = act.cursor()
            draw_cursor(rgb, 800, 600, pos[0] + 40 + 13, pos[1] + 30 + 14)
            return bytes(rgb), 800, 600

        act._capture = capture
        act.move_to(300, 200)
        self.assertTrue(act.last_align_ok)
        self.assertEqual(act.cursor(), (260, 170))


if __name__ == "__main__":
    unittest.main()
