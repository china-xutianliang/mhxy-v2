"""E3 轨迹到达检查：定时发送、折叠副机只读采样、逐点对齐。

纯计算，不碰窗口、不注入输入。Windows 侧由 runtime/e3_send.py、
runtime/e3_analyze.py 和 src/slave/cursor_log.py 调用。
"""
from __future__ import annotations

import random
import time


def probe_path(count: int = 30, x0: int = 200, y0: int = 300,
               step: int = 14, amp: int = 12,
               seed: int = 0xE3) -> list[tuple[int, int]]:
    """固定种子的非周期轨迹；周期轨迹在缺点时会被错配到另一相位。"""
    if count <= 0:
        raise ValueError("点数必须大于 0")
    if step <= 0:
        raise ValueError("步长必须大于 0")
    rng = random.Random(seed)
    return [(int(x0 + i * step), int(y0 + rng.choice((-amp, 0, amp))))
            for i in range(count)]


def send_timed(points, interval_ms: float, poster, clock=time.perf_counter,
               sleeper=time.sleep) -> list[list[float]]:
    """按固定间隔逐点调用 poster，返回 [相对毫秒, 点坐标...]。

    时间基准是第一条消息之前；poster 自己负责坐标缩放与窗口消息。
    """
    if interval_ms < 0:
        raise ValueError("间隔不能为负")
    sent: list[list[float]] = []
    start = clock()
    for index, point in enumerate(points):
        deadline = start + index * interval_ms / 1000.0
        delay = deadline - clock()
        if delay > 0:
            sleeper(delay)
        poster(*point)
        sent.append([round((clock() - start) * 1000.0, 2),
                     int(point[0]), int(point[1])])
    return sent


def dedupe(points) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for point in points:
        item = (int(point[0]), int(point[1]))
        if not out or out[-1] != item:
            out.append(item)
    return out


def collapse_runs(samples) -> list[list[float]]:
    """把逐毫秒采样折叠成连续相同位置段：[首次, 末次, x, y]。"""
    runs: list[list[float]] = []
    for sample in samples:
        if len(sample) < 3:
            raise ValueError(f"采样点格式应为 [t,x,y]：{sample!r}")
        t, x, y = float(sample[0]), int(sample[1]), int(sample[2])
        if runs and runs[-1][2] == x and runs[-1][3] == y:
            runs[-1][1] = t
        else:
            runs.append([t, t, x, y])
    return runs


def _close(x: int, y: int, wx: int, wy: int, tol: float) -> bool:
    return abs(x - wx) <= tol and abs(y - wy) <= tol


def match_sequence(points, runs, tol: float = 1.0, lookahead: int = 12) -> dict:
    """在副机位置段里按相对位移找主机轨迹的最长匹配。

    允许副机起点偏移任意、允许中间夹入额外位置（抖动），也允许
    开头缺点（从任意主机点起匹配）；返回主机点下标与位置段下标。
    """
    if not points or not runs:
        return {"matched": [], "runs": [], "missing": list(range(len(points))),
                "start_run": None, "end_run": None, "extra_runs": 0}
    lookahead = max(1, int(lookahead))
    best: dict | None = None
    best_score = None
    for host_start in range(len(points)):
        for run_start in range(len(runs)):
            base_x = int(runs[run_start][2]) - int(points[host_start][0])
            base_y = int(runs[run_start][3]) - int(points[host_start][1])
            matched = [host_start]
            run_ids = [run_start]
            cursor = run_start
            for host_index in range(host_start + 1, len(points)):
                want_x = int(points[host_index][0]) + base_x
                want_y = int(points[host_index][1]) + base_y
                found = None
                stop = min(len(runs), cursor + 1 + lookahead)
                for run_index in range(cursor + 1, stop):
                    if _close(int(runs[run_index][2]), int(runs[run_index][3]),
                              want_x, want_y, tol):
                        found = run_index
                        break
                if found is not None:
                    matched.append(host_index)
                    run_ids.append(found)
                    cursor = found
            extra = (run_ids[-1] - run_ids[0] + 1) - len(run_ids)
            score = (len(matched), -extra, -host_start, -run_ids[0])
            if best_score is None or score > best_score:
                best_score = score
                best = {"matched": matched, "runs": run_ids,
                        "missing": [i for i in range(len(points)) if i not in matched],
                        "start_run": run_ids[0], "end_run": run_ids[-1],
                        "extra_runs": extra}
    return best


def stats(values) -> dict:
    values = sorted(float(v) for v in values)
    if not values:
        return {"count": 0, "min": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}

    def pick(p: float) -> float:
        k = (len(values) - 1) * p
        lo = int(k)
        hi = min(lo + 1, len(values) - 1)
        return round(values[lo] + (values[hi] - values[lo]) * (k - lo), 2)

    return {"count": len(values), "min": round(values[0], 2),
            "p50": pick(0.5), "p95": pick(0.95), "max": round(values[-1], 2)}


def analyze(points, samples, tol: float = 1.0, lookahead: int = 12) -> dict:
    points = dedupe(points)
    runs = collapse_runs(samples)
    match = match_sequence(points, runs, tol=tol, lookahead=lookahead)
    arrivals = []
    for host_index, run_index in zip(match["matched"], match["runs"]):
        run = runs[run_index]
        arrivals.append({"index": host_index, "x": int(run[2]), "y": int(run[3]),
                         "t_first_ms": round(run[0], 2),
                         "t_last_ms": round(run[1], 2),
                         "dwell_ms": round(run[1] - run[0], 2),
                         "x_game": int(points[host_index][0]),
                         "y_game": int(points[host_index][1])})
    by_index = {item["index"]: item["t_first_ms"] for item in arrivals}
    intervals = []
    for host_index in range(len(points) - 1):
        if host_index in by_index and host_index + 1 in by_index:
            intervals.append(round(by_index[host_index + 1] - by_index[host_index], 2))
    dwells = [item["dwell_ms"] for item in arrivals]
    return {
        "point_count": len(points),
        "sample_count": len(samples),
        "run_count": len(runs),
        "matched_count": len(match["matched"]),
        "missing": match["missing"],
        "extra_runs": match["extra_runs"],
        "all_delivered": not match["missing"],
        "start_run": match["start_run"],
        "end_run": match["end_run"],
        "tolerance_px": tol,
        "arrivals": arrivals,
        "intervals_ms": intervals,
        "interval_stats": stats(intervals),
        "dwell_stats": stats(dwells),
    }
