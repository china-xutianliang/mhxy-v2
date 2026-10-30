import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from control.calib import NINE_POINTS, CalibrationRow, assess_rows


class CalibrationTests(unittest.TestCase):
    def test_nine_points_layout(self):
        self.assertEqual(len(NINE_POINTS), 9)
        self.assertIn((400, 300), NINE_POINTS)
        self.assertEqual(len(set(NINE_POINTS)), 9)

    def test_assess_requires_detection_and_two_pixel_accuracy(self):
        rows = [CalibrationRow(point, point, 0, 10) for point in NINE_POINTS]
        self.assertTrue(assess_rows(rows)[0])
        rows[0] = CalibrationRow(NINE_POINTS[0], (43, 40), 3, 10)
        self.assertFalse(assess_rows(rows)[0])
        rows[0] = CalibrationRow(NINE_POINTS[0], None, None, 10)
        self.assertFalse(assess_rows(rows)[0])


if __name__ == "__main__":
    unittest.main()
