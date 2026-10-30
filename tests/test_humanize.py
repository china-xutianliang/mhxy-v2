import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from control.humanize import Profile, click_point, plan_combo, plan_correction, plan_path, think  # noqa: E402


class PlanPathTests(unittest.TestCase):
    def test_endpoint_bounds_duplicates_and_time(self):
        profile = Profile(
            fitts_a_ms=(100, 100),
            fitts_b_ms=(120, 120),
            duration_jitter=0,
            overshoot_probability=(0, 0),
        )
        path = plan_path((20, 30), (700, 500), random.Random(7), profile)
        self.assertEqual(path[-1][:2], (700, 500))
        self.assertTrue(all(0 <= x <= 799 and 0 <= y <= 599 for x, y, _ in path))
        self.assertTrue(all(path[i][:2] != path[i - 1][:2] for i in range(1, len(path))))
        self.assertEqual(path[0][2], 0)
        self.assertTrue(all(dt > 0 for _, _, dt in path[1:]))
        expected = round(100 + 120 * math.log2(math.dist((20, 30), (700, 500)) / 32 + 1))
        self.assertEqual(sum(dt for _, _, dt in path), expected)

    def test_fitts_duration_stays_in_profile_interval(self):
        profile = Profile()
        start, end = (100, 100), (500, 400)
        difficulty = math.log2(math.dist(start, end) / 32 + 1)
        minimum = (80 + 90 * difficulty) * 0.85
        maximum = (120 + 150 * difficulty) * 1.15
        for seed in range(30):
            path = plan_path(start, end, random.Random(seed), profile)
            duration = sum(dt for _, _, dt in path)
            self.assertGreaterEqual(duration, math.floor(minimum) - 1)
            self.assertLessEqual(duration, math.ceil(maximum) + 1)

    def test_same_seed_reproduces_and_different_seed_varies(self):
        profile = Profile()
        first = plan_path((10, 20), (700, 500), random.Random(123), profile)
        second = plan_path((10, 20), (700, 500), random.Random(123), profile)
        different = plan_path((10, 20), (700, 500), random.Random(124), profile)
        self.assertEqual(first, second)
        self.assertNotEqual(first, different)

    def test_short_distance_has_multiple_points(self):
        profile = Profile(overshoot_probability=(0, 0))
        for end in ((11, 10), (14, 14), (15, 10)):
            path = plan_path((10, 10), end, random.Random(4), profile)
            self.assertGreaterEqual(len(path), 2)
            self.assertEqual(path[-1][:2], end)

    def test_curve_deviates_from_straight_line(self):
        profile = Profile(noise_px=0, overshoot_probability=(0, 0))
        paths = [plan_path((100, 300), (700, 300), random.Random(seed), profile) for seed in range(5)]
        self.assertTrue(any(any(y != 300 for _, y, _ in path) for path in paths))

    def test_forced_overshoot_returns_to_exact_target(self):
        profile = Profile(
            noise_px=0,
            overshoot_probability=(1, 1),
            overshoot_ratio=(0.04, 0.04),
        )
        path = plan_path((100, 300), (600, 300), random.Random(9), profile)
        self.assertTrue(any(x > 600 for x, _, _ in path))
        self.assertEqual(path[-1][:2], (600, 300))
        self.assertLess(path[-1][0], max(x for x, _, _ in path))

    def test_overshoot_is_clipped_at_screen_boundary(self):
        profile = Profile(overshoot_probability=(1, 1), overshoot_ratio=(0.06, 0.06))
        for seed in range(20):
            path = plan_path((600, 500), (799, 599), random.Random(seed), profile)
            self.assertTrue(all(0 <= x <= 799 and 0 <= y <= 599 for x, y, _ in path))
            self.assertEqual(path[-1][:2], (799, 599))

    def test_invalid_coordinates_are_rejected(self):
        with self.assertRaises(ValueError):
            plan_path((-1, 0), (10, 10), random.Random(1), Profile())
        with self.assertRaises(ValueError):
            plan_path((0, 0), (800, 10), random.Random(1), Profile())

    def test_lan_profile_stays_one_to_one_and_uses_bounded_samples(self):
        profile = Profile.lan()
        self.assertEqual(profile.sample_ms, (10, 14))
        self.assertTrue(profile.correlated_controls)
        self.assertTrue(profile.correlated_noise)
        path = plan_path((30, 40), (760, 560), random.Random(9), profile)
        self.assertEqual(path[-1][:2], (760, 560))
        self.assertTrue(all(0 <= x < 800 and 0 <= y < 600 for x, y, _ in path))
        self.assertTrue(all(dt > 0 for _, _, dt in path[1:]))

    def test_lan_profile_keeps_curve_on_one_side(self):
        # 800x600 局域网档的主路径只做单侧轻微偏移，避免独立控制点造成 S 型摆动。
        profile = Profile.lan()
        profile.noise_px = 0
        profile.overshoot_probability = (0, 0)
        for seed in range(30):
            path = plan_path((80, 300), (720, 300), random.Random(seed), profile)
            signs = {1 if y > 300 else -1 for _, y, _ in path[1:-1] if y != 300}
            self.assertLessEqual(len(signs), 1, path)


class PlanCorrectionTests(unittest.TestCase):
    def test_endpoints_bounds_and_no_repeated_points(self):
        cases = (((100, 100), (104, 98)), ((300, 200), (240, 255)),
                 ((50, 50), (5, 3)), ((790, 590), (799, 599)), ((0, 0), (1, 0)))
        for seed in range(30):
            for start, end in cases:
                path = plan_correction(start, end, random.Random(seed), Profile())
                self.assertEqual(path[0][:2], start)
                self.assertEqual(path[-1][:2], end)
                self.assertEqual(path[0][2], 0)
                self.assertTrue(all(0 <= x <= 799 and 0 <= y <= 599 for x, y, _ in path))
                self.assertTrue(all(path[i][:2] != path[i - 1][:2] for i in range(1, len(path))))
                self.assertTrue(all(dt > 0 for _, _, dt in path[1:]))

    def test_zero_distance_is_a_single_point(self):
        self.assertEqual(plan_correction((30, 40), (30, 40), random.Random(1), Profile()),
                         [(30, 40, 0)])

    def test_short_correction_never_steps_backwards_or_wanders(self):
        # plan_path 的 ±1px 噪声放在 12px 的修正上会来回抖；修正应沿一个方向走完
        for seed in range(60):
            right = plan_correction((100, 100), (112, 100), random.Random(seed), Profile())
            self.assertEqual([p[0] for p in right], sorted(p[0] for p in right))
            self.assertTrue(all(abs(y - 100) <= 1 for _, y, _ in right))
            up = plan_correction((200, 300), (200, 288), random.Random(seed), Profile())
            self.assertEqual([p[1] for p in up], sorted((p[1] for p in up), reverse=True))
            self.assertTrue(all(abs(x - 200) <= 1 for x, _, _ in up))

    def test_duration_scales_with_distance_and_stays_human(self):
        for seed in range(30):
            near = sum(dt for _, _, dt in plan_correction((100, 100), (110, 100), random.Random(seed), Profile()))
            far = sum(dt for _, _, dt in plan_correction((100, 100), (180, 100), random.Random(seed), Profile()))
            self.assertTrue(80 <= near <= 240, near)
            self.assertTrue(150 <= far <= 560, far)
            self.assertGreater(far, near)

    def test_same_seed_reproduces_and_different_seed_varies(self):
        first = plan_correction((100, 100), (180, 140), random.Random(5), Profile())
        second = plan_correction((100, 100), (180, 140), random.Random(5), Profile())
        different = plan_correction((100, 100), (180, 140), random.Random(6), Profile())
        self.assertEqual(first, second)
        self.assertNotEqual(first, different)

    def test_most_distance_is_covered_before_half_time(self):
        # 速度峰值前移：用一半的时间走完一半以上的距离，后半程慢慢收
        profile = Profile(noise_px=0, duration_jitter=0)
        fractions = []
        for seed in range(30):
            path = plan_correction((100, 100), (180, 100), random.Random(seed), profile)
            total = sum(dt for _, _, dt in path)
            elapsed = 0
            covered = 0.0
            for (x0, _y0, _), (x1, _y1, dt) in zip(path, path[1:]):
                if elapsed + dt >= total / 2:
                    covered = x0 + (x1 - x0) * (total / 2 - elapsed) / dt
                    break
                elapsed += dt
            fractions.append((covered - 100) / 80)
        self.assertGreater(sum(fractions) / len(fractions), 0.56)

    def test_invalid_coordinates_are_rejected(self):
        with self.assertRaises(ValueError):
            plan_correction((-1, 0), (10, 10), random.Random(1), Profile())
        with self.assertRaises(ValueError):
            plan_correction((0, 0), (800, 10), random.Random(1), Profile())


class ClickPointTests(unittest.TestCase):
    def test_box_points_stay_inside_and_vary(self):
        rng = random.Random(20)
        points = [click_point((100, 100), rng, (20, 10)) for _ in range(500)]
        self.assertTrue(all(90 <= x <= 110 and 95 <= y <= 105 for x, y in points))
        self.assertGreater(len(set(points)), 10)

    def test_box_is_truncated_at_screen_boundary(self):
        rng = random.Random(2)
        points = [click_point((0, 599), rng, (20, 20)) for _ in range(100)]
        self.assertTrue(all(0 <= x <= 10 and 589 <= y <= 599 for x, y in points))

    def test_without_box_jitter_is_at_most_one_pixel(self):
        rng = random.Random(5)
        points = [click_point((200, 200), rng) for _ in range(100)]
        self.assertTrue(all(abs(x - 200) <= 1 and abs(y - 200) <= 1 for x, y in points))
        self.assertIn((200, 200), points)
        self.assertGreater(len(set(points)), 2)

    def test_same_seed_reproduces_click_point(self):
        first = click_point((100, 100), random.Random(88), (30, 20))
        second = click_point((100, 100), random.Random(88), (30, 20))
        self.assertEqual(first, second)


class RhythmTests(unittest.TestCase):
    def test_profile_rhythm_ranges(self):
        profile = Profile()
        self.assertEqual(profile.pre_press_ms, (40, 120))
        self.assertEqual(profile.hold_ms, (60, 140))
        self.assertEqual(profile.double_interval_ms, (80, 160))
        self.assertEqual(profile.key_hold_ms, (50, 120))
        self.assertEqual(profile.modifier_delay_ms, (30, 80))
        self.assertEqual(profile.modifier_release_delay_ms, (20, 60))

    def test_combo_order_and_intervals(self):
        profile = Profile()
        events = plan_combo(("ctrl", "shift"), "a", random.Random(3), profile)
        self.assertEqual([(event, key) for event, key, _ in events], [
            ("down", "ctrl"),
            ("down", "shift"),
            ("down", "a"),
            ("up", "a"),
            ("up", "shift"),
            ("up", "ctrl"),
        ])
        self.assertEqual(events[0][2], 0)
        self.assertEqual(events[1][2], 0)
        self.assertTrue(30 <= events[2][2] <= 80)
        self.assertTrue(50 <= events[3][2] <= 120)
        self.assertTrue(20 <= events[4][2] <= 60)
        self.assertEqual(events[5][2], 0)

    def test_single_modifier_combo_and_reproducibility(self):
        first = plan_combo("alt", "e", random.Random(11), Profile())
        second = plan_combo("alt", "e", random.Random(11), Profile())
        self.assertEqual(first, second)
        self.assertEqual([(e, k) for e, k, _ in first], [
            ("down", "alt"), ("down", "e"), ("up", "e"), ("up", "alt")
        ])

    def test_think_uses_kind_range_and_injected_rng(self):
        first = think(random.Random(10), "click")
        second = think(random.Random(10), "click")
        self.assertEqual(first, second)
        self.assertTrue(120 <= first <= 350)
        self.assertTrue(300 <= think(random.Random(1), "page") <= 800)
        self.assertTrue(120 <= think(random.Random(1), "unknown") <= 350)


if __name__ == "__main__":
    unittest.main()
