import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from kmtool.coords import GAME_H, GAME_W, parse_align, parse_hold, parse_point, view_to_game


class ViewToGameTests(unittest.TestCase):
    def test_identity_when_view_is_game_size(self):
        self.assertEqual(view_to_game(0, 0, 800, 600), (0, 0))
        self.assertEqual(view_to_game(799, 599, 800, 600), (799, 599))
        self.assertEqual(view_to_game(400, 300, 800, 600), (400, 300))

    def test_scaled_physical_view(self):
        self.assertEqual(view_to_game(0, 0, 1600, 1200), (0, 0))
        self.assertEqual(view_to_game(1599, 1199, 1600, 1200), (799, 599))
        self.assertEqual(view_to_game(800, 600, 1600, 1200), (400, 300))

    def test_subsampled_preview(self):
        self.assertEqual(view_to_game(200, 150, 800, 600), (200, 150))
        self.assertEqual(view_to_game(799, 599, 800, 600), (799, 599))

    def test_out_of_range_and_invalid_size(self):
        self.assertIsNone(view_to_game(-1, 0, 800, 600))
        self.assertIsNone(view_to_game(0, -1, 800, 600))
        self.assertIsNone(view_to_game(800, 0, 800, 600))
        self.assertIsNone(view_to_game(0, 600, 800, 600))
        self.assertIsNone(view_to_game(0, 0, 0, 600))
        self.assertIsNone(view_to_game(0, 0, 800, 0))

    def test_never_exceeds_game_bounds(self):
        for px, py in ((1599, 0), (0, 1199), (1599, 1199)):
            x, y = view_to_game(px, py, 1600, 1200)
            self.assertLess(x, GAME_W)
            self.assertLess(y, GAME_H)


class ParsePointTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_point("0", "0"), (0, 0))
        self.assertEqual(parse_point(" 799 ", "599"), (799, 599))
        self.assertEqual(parse_point(400, 300), (400, 300))

    def test_non_integer(self):
        for x, y in (("", "1"), ("a", "1"), ("1.5", "2"), (None, 2), ("1", None)):
            with self.assertRaisesRegex(ValueError, "整数"):
                parse_point(x, y)

    def test_out_of_bounds(self):
        for x, y in ((-1, 0), (800, 0), (0, -1), (0, 600)):
            with self.assertRaisesRegex(ValueError, "799"):
                parse_point(x, y)


class ParseHoldTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_hold("60,140"), (60, 140))
        self.assertEqual(parse_hold(" 60 , 140 "), (60, 140))
        self.assertEqual(parse_hold("60，140"), (60, 140))
        self.assertEqual(parse_hold("1,2000"), (1, 2000))
        self.assertEqual(parse_hold("80,80"), (80, 80))

    def test_bad_shape(self):
        for text in ("", "60", "60,140,200", "60;140", None):
            with self.assertRaisesRegex(ValueError, "最小,最大"):
                parse_hold(text)

    def test_bad_range(self):
        for text in ("0,140", "140,60", "-5,10", "60,2001", "a,140"):
            with self.assertRaises(ValueError):
                parse_hold(text)


class ParseAlignTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_align("3", "30"), (3, 30))
        self.assertEqual(parse_align(" 1 ", " 3 "), (1, 3))
        self.assertEqual(parse_align("20", "120"), (20, 120))

    def test_bad_values(self):
        for tol, max_s in (("0", "30"), ("21", "30"), ("3", "2"), ("3", "121"),
                           ("", "30"), ("3", ""), ("a", "30"), ("3.5", "30"), (None, "30")):
            with self.assertRaises(ValueError):
                parse_align(tol, max_s)


if __name__ == "__main__":
    unittest.main()
