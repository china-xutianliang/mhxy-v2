"""只读指针记录器的单元测试（不需要真实鼠标移动）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import cursor_log  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class RecordTests(unittest.TestCase):
    def test_samples_at_requested_rate(self):
        clock = FakeClock()
        samples = cursor_log.record(0.0, 0.019, 500, lambda: (100, 200),
                                    clock=clock, sleeper=clock.sleep)
        self.assertEqual(len(samples), 10)
        self.assertEqual([s[0] for s in samples], [0.0, 2.0, 4.0, 6.0, 8.0,
                                                  10.0, 12.0, 14.0, 16.0, 18.0])
        self.assertTrue(all(s[1:] == [100, 200] for s in samples))

    def test_delay_is_not_sampled(self):
        clock = FakeClock()
        samples = cursor_log.record(0.005, 0.003, 500, lambda: (7, 8),
                                    clock=clock, sleeper=clock.sleep)
        self.assertEqual(samples[0][0], 0.0)
        self.assertAlmostEqual(clock.now, 0.005 + 0.004, places=6)

    def test_rejects_bad_duration(self):
        with self.assertRaises(ValueError):
            cursor_log.record(0.0, 0.0, 500, lambda: (0, 0))
        with self.assertRaises(ValueError):
            cursor_log.record(0.0, 1.0, 0, lambda: (0, 0))


class SummarizeTests(unittest.TestCase):
    def test_collapses_runs_and_stats(self):
        summary = cursor_log.summarize([[0, 1, 1], [1, 1, 1], [2, 2, 2], [3, 2, 2]])
        self.assertEqual(summary["samples"], 4)
        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["sample_interval_ms_p50"], 1.0)
        self.assertEqual(summary["run_dwell_ms_max"], 1.0)

    def test_empty(self):
        summary = cursor_log.summarize([])
        self.assertEqual(summary["samples"], 0)
        self.assertEqual(summary["runs"], 0)


class PayloadTests(unittest.TestCase):
    def test_payload_fields(self):
        samples = [[0, 10, 20], [2, 10, 20]]
        payload = cursor_log.build_payload(3.0, 1.0, 500, samples,
                                           100.0, 101.0, True, (2560, 1600))
        self.assertEqual(payload["tool"], "cursor_log")
        self.assertEqual(payload["screen"], [2560, 1600])
        self.assertTrue(payload["dpi_aware"])
        self.assertEqual(payload["summary"]["runs"], 1)


class ReadOnlyTests(unittest.TestCase):
    def test_source_has_no_input_apis(self):
        with open(cursor_log.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for banned in ("SetCursorPos", "mouse_event", "SendInput", "keybd_event",
                       "PostMessage", "SendMessage", "SetWindowsHookEx"):
            self.assertNotIn(banned, source, banned)


if __name__ == "__main__":
    unittest.main()
