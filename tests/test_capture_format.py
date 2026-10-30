import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from control.calib import TemplatePointerDetector
from control.channel import Channel


class FakeDm:
    def __init__(self, ok=1):
        self.calls = []
        self.ok = ok

    def CapturePng(self, *args):
        self.calls.append(("png",) + args)
        return self.ok

    def CaptureJpg(self, *args):
        self.calls.append(("jpg",) + args)
        return self.ok

    def SetPath(self, path):
        self.calls.append(("path", path))
        return 1


PARAMS = {"display": "gdi", "mouse": "windows", "keypad": "windows"}


class FakeViewport:
    def client_size(self):
        return 800, 600


class CaptureFormatTests(unittest.TestCase):
    def test_capture_jpg_passes_quality_and_reports_result(self):
        dm = FakeDm()
        channel = Channel(dm, PARAMS)
        self.assertTrue(channel.capture_jpg(0, 0, 800, 600, "a.jpg"))
        self.assertEqual(dm.calls[-1], ("jpg", 0, 0, 800, 600, "a.jpg", 70))
        self.assertTrue(channel.capture_jpg(0, 0, 10, 10, "b.jpg", quality=50))
        self.assertEqual(dm.calls[-1], ("jpg", 0, 0, 10, 10, "b.jpg", 50))
        self.assertFalse(Channel(FakeDm(ok=0), PARAMS).capture_jpg(0, 0, 1, 1, "c.jpg"))

    def test_pointer_detector_frame_is_saved_as_small_jpg_not_png(self):
        dm = FakeDm()
        detector = TemplatePointerDetector(Channel(dm, PARAMS), FakeViewport())
        detector.refresh()
        kinds = [call[0] for call in dm.calls]
        self.assertIn("jpg", kinds)
        self.assertNotIn("png", kinds)
        self.assertTrue(detector.last_screenshot.endswith(".jpg"))
        self.assertIsNotNone(detector.frame_ts)

    def test_failed_capture_clears_frame_timestamp(self):
        detector = TemplatePointerDetector(Channel(FakeDm(ok=0), PARAMS), FakeViewport())
        detector.frame_ts = 1.0
        with self.assertRaises(RuntimeError):
            detector.refresh()
        self.assertIsNone(detector.frame_ts)


if __name__ == "__main__":
    unittest.main()
