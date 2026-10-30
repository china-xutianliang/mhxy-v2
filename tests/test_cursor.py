"""指针起点状态测试：不读真实画面、不等待真实时间。"""
import os
import sys
import unittest
from dataclasses import FrozenInstanceError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from control.cursor import CursorState, CursorTracker  # noqa: E402


class FakeClock:
    def __init__(self, now=10.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeDetector:
    def __init__(self, clock, result=None, frame_ts=10.0):
        self.clock = clock
        self.result = result
        self.frame_ts = frame_ts
        self.events = []
        self.near = []

    def refresh(self):
        self.events.append("refresh")
        self.frame_ts = self.clock()

    def detect(self, near):
        self.events.append("detect")
        self.near.append(near)
        return self.result


class CursorTrackerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.detector = FakeDetector(self.clock)
        self.tracker = CursorTracker(self.detector, clock=self.clock,
                                     min_detect_interval=0.5)

    def test_detected_position_is_preferred(self):
        self.detector.result = (12, 34)
        self.assertEqual(self.tracker.start_for(100, 100), (12, 34))
        self.assertEqual(self.tracker.cursor,
                         CursorState((12, 34), "detected", 10.0))
        self.assertEqual(self.detector.events, ["detect"])

    def test_recent_move_uses_tracking_without_stale_frame_detection(self):
        self.tracker.mark_moved(40, 50)
        self.detector.result = (100, 100)
        self.clock.advance(0.49)
        self.assertEqual(self.tracker.start_for(70, 80), (40, 50))
        self.assertEqual(self.detector.events, [])
        self.assertEqual(self.tracker.cursor.source, "tracked")

    def test_long_interval_uses_detection(self):
        self.tracker.mark_moved(40, 50)
        self.detector.result = (42, 51)
        self.clock.advance(0.5)
        self.assertEqual(self.tracker.start_for(70, 80), (42, 51))
        self.assertEqual(self.detector.events, ["detect"])
        self.assertEqual(self.tracker.cursor.source, "detected")

    def test_failed_detection_falls_back_to_tracking(self):
        self.tracker.mark_moved(40, 50)
        self.clock.advance(1.0)
        self.detector.result = None
        self.assertEqual(self.tracker.start_for(70, 80), (40, 50))
        self.assertEqual(self.tracker.cursor,
                         CursorState((40, 50), "tracked", 11.0))

    def test_unknown_enters_from_nearest_outside_edge(self):
        cases = (
            ((0, 300), (-1, 300)),
            ((799, 300), (800, 300)),
            ((400, 0), (400, -1)),
            ((400, 599), (400, 600)),
        )
        for target, expected in cases:
            with self.subTest(target=target):
                tracker = CursorTracker(self.detector, clock=self.clock)
                self.assertEqual(tracker.start_for(*target), expected)
                self.assertEqual(tracker.cursor.pos, None)
                self.assertEqual(tracker.cursor.source, "unknown")

    def test_detection_wins_and_logs_when_deviation_exceeds_three_px(self):
        self.tracker.mark_moved(100, 100)
        self.clock.advance(1.0)
        self.detector.result = (104, 100)
        with self.assertLogs("control.cursor", level="WARNING") as logs:
            self.assertEqual(self.tracker.start_for(200, 200), (104, 100))
        self.assertIn("偏差超过 3px", "\n".join(logs.output))
        self.assertEqual(self.tracker.cursor.source, "detected")

    def test_three_px_deviation_still_uses_detection_without_warning(self):
        self.tracker.mark_moved(100, 100)
        self.clock.advance(1.0)
        self.detector.result = (103, 100)
        with self.assertNoLogs("control.cursor", level="WARNING"):
            self.assertEqual(self.tracker.start_for(200, 200), (103, 100))

    def test_stale_frame_is_refreshed_before_detection(self):
        self.detector.frame_ts = 8.4
        self.detector.result = (20, 30)
        self.assertEqual(self.tracker.start_for(100, 100), (20, 30))
        self.assertEqual(self.detector.events, ["refresh", "detect"])
        self.assertEqual(self.detector.frame_ts, 10.0)

    def test_frame_exactly_1_5_seconds_old_is_not_refreshed(self):
        self.detector.frame_ts = 8.5
        self.tracker.start_for(100, 100)
        self.assertEqual(self.detector.events, ["detect"])

    def test_missing_frame_is_refreshed_before_detection(self):
        self.detector.frame_ts = None
        self.tracker.start_for(100, 100)
        self.assertEqual(self.detector.events, ["refresh", "detect"])

    def test_last_known_position_is_passed_as_detector_hint(self):
        self.detector.result = (15, 25)
        self.tracker.start_for(100, 100)
        self.tracker.start_for(110, 110)
        self.assertEqual(self.detector.near, [None, (15, 25)])

    def test_invalidate_forces_detection_even_within_continuous_interval(self):
        self.tracker.mark_moved(40, 50)
        self.clock.advance(0.1)
        self.tracker.invalidate()
        self.assertEqual(self.tracker.cursor.source, "unknown")
        self.assertIsNone(self.tracker.cursor.pos)
        self.detector.result = (60, 70)
        self.assertEqual(self.tracker.start_for(100, 100), (60, 70))
        self.assertEqual(self.detector.events, ["detect"])

    def test_reset_clears_tracking_and_restores_unknown(self):
        self.tracker.mark_moved(40, 50)
        self.clock.advance(0.1)
        self.tracker.reset()
        self.assertEqual(self.tracker.cursor,
                         CursorState(None, "unknown", 10.1))
        self.detector.result = None
        self.assertEqual(self.tracker.start_for(0, 300), (-1, 300))

    def test_out_of_bounds_target_is_rejected_before_detection(self):
        for target in ((-1, 0), (800, 0), (0, -1), (0, 600)):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    self.tracker.start_for(*target)
        self.assertEqual(self.detector.events, [])

    def test_invalid_move_does_not_replace_tracked_position(self):
        self.tracker.mark_moved(40, 50)
        with self.assertRaises(ValueError):
            self.tracker.mark_moved(800, 50)
        self.assertEqual(self.tracker.cursor.pos, (40, 50))

    def test_cursor_snapshot_and_property_are_read_only(self):
        with self.assertRaises(FrozenInstanceError):
            self.tracker.cursor.pos = (1, 2)
        with self.assertRaises(AttributeError):
            self.tracker.cursor = CursorState((1, 2), "tracked", 10.0)


if __name__ == "__main__":
    unittest.main()
