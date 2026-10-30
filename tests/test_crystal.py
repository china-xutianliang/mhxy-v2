import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from control.crystal import GEM_TO_TIP, arrow_tip, body_score, find_cursor, find_gem, is_gem_pixel

W, H = 140, 120
BG = 60
BLUE = (30, 60, 200)
GEM = (170, 245, 110)
GOLD = (240, 200, 60)
LEAF = (90, 200, 80)


def canvas():
    return bytearray(bytes((BG, BG, BG)) * (W * H))


def put(rgb, x, y, color):
    i = (y * W + x) * 3
    rgb[i:i + 3] = bytes(color)


def draw_gem(rgb, cx, cy, size=6):
    x0, y0 = cx - size // 2, cy - size // 2
    for y in range(y0 - 1, y0 + size + 1):
        for x in range(x0 - 1, x0 + size + 1):
            solid = x0 <= x < x0 + size and y0 <= y < y0 + size
            put(rgb, x, y, GEM if solid else GOLD)


def draw_body(rgb, cx, cy):
    """宝石左上方 10x10 的蓝色身体，约等于真光标的 100 个蓝像素。"""
    for y in range(cy - 12, cy - 2):
        for x in range(cx - 12, cx - 2):
            put(rgb, x, y, BLUE)


def draw_leaf(rgb, cx, cy, width=2, height=24):
    for y in range(cy - height // 2, cy - height // 2 + height):
        for x in range(cx - width // 2, cx - width // 2 + width):
            put(rgb, x, y, LEAF)


class CrystalTests(unittest.TestCase):
    def test_gem_pixel_rejects_dark_foliage(self):
        self.assertTrue(is_gem_pixel(*GEM))
        self.assertFalse(is_gem_pixel(*LEAF))
        self.assertFalse(is_gem_pixel(60, 90, 70))

    def test_finds_compact_gem_with_gold_rim(self):
        rgb = canvas()
        draw_gem(rgb, 70, 60)
        found = find_gem(rgb, W, H, (65, 55))
        self.assertIsNotNone(found)
        x, y, area = found
        self.assertLessEqual(abs(x - 70), 1)
        self.assertLessEqual(abs(y - 60), 1)
        self.assertGreaterEqual(area, 30)

    def test_rejects_thin_leaf_even_when_closer(self):
        rgb = canvas()
        draw_leaf(rgb, 65, 55)
        draw_gem(rgb, 80, 70)
        found = find_gem(rgb, W, H, (65, 55))
        self.assertIsNotNone(found)
        x, y, _area = found
        self.assertLessEqual(abs(x - 80), 1)
        self.assertLessEqual(abs(y - 70), 1)

    def test_no_gem_returns_none(self):
        rgb = canvas()
        draw_leaf(rgb, 65, 55)
        self.assertIsNone(find_gem(rgb, W, H, (65, 55)))

    def test_gem_outside_window_returns_none(self):
        rgb = canvas()
        draw_gem(rgb, 120, 100)
        self.assertIsNone(find_gem(rgb, W, H, (40, 30), half=20))

    def test_short_buffer_is_safe(self):
        self.assertIsNone(find_gem(b"\x00" * 10, W, H, (10, 10)))
        self.assertIsNone(find_gem(b"", 0, 0, (0, 0)))

    def test_cursor_needs_blue_body_not_just_a_gem(self):
        rgb = canvas()
        draw_gem(rgb, 70, 60)
        self.assertIsNone(find_cursor(rgb, W, H, (65, 55), half=60))
        draw_body(rgb, 70, 60)
        self.assertGreaterEqual(body_score(rgb, W, H, 70, 60), 90)
        found = find_cursor(rgb, W, H, (65, 55), half=60)
        self.assertIsNotNone(found)
        self.assertLessEqual(abs(found[0] - 70), 1)
        self.assertLessEqual(abs(found[1] - 60), 1)

    def test_cursor_found_in_blink_phase_without_gold_rim(self):
        rgb = canvas()
        draw_body(rgb, 70, 60)
        for y in range(57, 63):
            for x in range(67, 73):
                put(rgb, x, y, GEM)
        self.assertIsNone(find_gem(rgb, W, H, (65, 55), half=60))
        self.assertIsNotNone(find_cursor(rgb, W, H, (65, 55), half=60))

    def test_cursor_skips_large_green_object_and_picks_real_one(self):
        rgb = canvas()
        for y in range(20, 36):
            for x in range(20, 36):
                put(rgb, x, y, GEM)
        draw_body(rgb, 28, 28)
        draw_gem(rgb, 100, 90)
        draw_body(rgb, 100, 90)
        found = find_cursor(rgb, W, H, (25, 25), half=120)
        self.assertIsNotNone(found)
        self.assertLessEqual(abs(found[0] - 100), 1)
        self.assertLessEqual(abs(found[1] - 90), 1)

    def test_arrow_tip_is_up_left_of_gem(self):
        self.assertEqual(GEM_TO_TIP, (13, 14))
        self.assertEqual(arrow_tip((113, 214, 42)), (100, 200))


if __name__ == "__main__":
    unittest.main()
