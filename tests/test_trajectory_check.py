import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from control.trajectory_check import (  # noqa: E402
    analyze, collapse_runs, probe_path, send_timed, stats,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make_samples(points, offset=(100, 50), per=5, step_ms=2.0, skip=()):
    out = []
    now = 0.0
    for index, (x, y) in enumerate(points):
        if index in skip:
            continue
        for _ in range(per):
            out.append([round(now, 2), x + offset[0], y + offset[1]])
            now += step_ms
    return out


class ProbePathTests(unittest.TestCase):
    def test_count_deterministic_and_bounds(self):
        points = probe_path(30, 200, 300, 14, 12)
        self.assertEqual(len(points), 30)
        self.assertEqual(points, probe_path(30, 200, 300, 14, 12))
        self.assertEqual(points[0][0], 200)
        self.assertEqual(points[-1][0], 606)
        for x, y in points:
            self.assertTrue(0 <= x < 800 and 288 <= y <= 312)

    def test_rejects_bad_arguments(self):
        with self.assertRaises(ValueError):
            probe_path(0)
        with self.assertRaises(ValueError):
            probe_path(3, step=0)


class SendTimedTests(unittest.TestCase):
    def test_uses_fixed_deadlines(self):
        clock = FakeClock()
        calls = []
        sent = send_timed([(1, 2), (3, 4), (5, 6)], 10, lambda x, y: calls.append((x, y)),
                          clock=clock, sleeper=clock.sleep)
        self.assertEqual(calls, [(1, 2), (3, 4), (5, 6)])
        self.assertEqual([item[0] for item in sent], [0.0, 10.0, 20.0])

    def test_zero_interval_sends_back_to_back(self):
        clock = FakeClock()
        sent = send_timed([(1, 1), (2, 2)], 0, lambda x, y: None,
                          clock=clock, sleeper=clock.sleep)
        self.assertEqual([item[0] for item in sent], [0.0, 0.0])


class CollapseTests(unittest.TestCase):
    def test_merges_repeated_positions(self):
        runs = collapse_runs([[0, 10, 20], [2, 10, 20], [4, 10, 20],
                              [6, 30, 40], [8, 30, 40], [10, 10, 20]])
        self.assertEqual(runs, [[0, 4, 10, 20], [6, 8, 30, 40], [10, 10, 10, 20]])

    def test_rejects_short_sample(self):
        with self.assertRaises(ValueError):
            collapse_runs([[0, 1]])


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.points = probe_path(30, 200, 300, 14, 12)

    def test_all_delivered_with_arbitrary_origin(self):
        samples = make_samples(self.points)
        result = analyze(self.points, samples)
        self.assertEqual(result["point_count"], 30)
        self.assertEqual(result["matched_count"], 30)
        self.assertEqual(result["missing"], [])
        self.assertTrue(result["all_delivered"])
        self.assertEqual(result["extra_runs"], 0)
        self.assertEqual(result["interval_stats"]["p50"], 10.0)
        self.assertEqual(result["sample_count"], 150)

    def test_reports_missing_point(self):
        samples = make_samples(self.points, skip={17})
        result = analyze(self.points, samples)
        self.assertEqual(result["matched_count"], 29)
        self.assertEqual(result["missing"], [17])
        self.assertFalse(result["all_delivered"])
        self.assertEqual(result["extra_runs"], 0)

    def test_reports_extra_position_but_keeps_match(self):
        samples = make_samples(self.points)
        extra = [samples[5 * 5 + 4].copy(), samples[5 * 5 + 4].copy()]
        extra[0][0] += 1.0
        extra[0][2] += 3
        extra[1][0] += 2.0
        extra[1][2] += 3
        samples = samples[:30] + extra + samples[30:]
        result = analyze(self.points, samples, tol=1)
        self.assertEqual(result["matched_count"], 30)
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["extra_runs"], 1)

    def test_tolerance_accepts_one_pixel_shift(self):
        samples = make_samples(self.points)
        target_x, target_y = self.points[10]
        for sample in samples:
            if sample[1] - 100 == target_x and sample[2] - 50 == target_y:
                sample[1] += 1
        self.assertEqual(analyze(self.points, samples, tol=1)["matched_count"], 30)
        strict = analyze(self.points, samples, tol=0)
        self.assertEqual(strict["matched_count"], 29)
        self.assertEqual(strict["missing"], [10])

    def test_allows_late_start(self):
        samples = make_samples(self.points, skip={0, 1, 2, 3})
        result = analyze(self.points, samples)
        self.assertEqual(result["matched_count"], 26)
        self.assertEqual(result["missing"], [0, 1, 2, 3])

    def test_empty_samples(self):
        result = analyze(self.points, [])
        self.assertEqual(result["matched_count"], 0)
        self.assertEqual(result["missing"], list(range(30)))
        self.assertFalse(result["all_delivered"])

    def test_dedupes_repeated_host_points(self):
        points = [(10, 10), (10, 10), (20, 20)]
        samples = [[0, 100, 100], [2, 100, 100], [4, 110, 110], [6, 110, 110]]
        result = analyze(points, samples)
        self.assertEqual(result["point_count"], 2)
        self.assertEqual(result["matched_count"], 2)


class StatsTests(unittest.TestCase):
    def test_percentiles(self):
        result = stats([10, 20, 30, 40])
        self.assertEqual(result["count"], 4)
        self.assertEqual(result["min"], 10)
        self.assertEqual(result["p50"], 25)
        self.assertEqual(result["p95"], 38.5)
        self.assertEqual(result["max"], 40)

    def test_empty(self):
        self.assertEqual(stats([])["count"], 0)


if __name__ == "__main__":
    unittest.main()
