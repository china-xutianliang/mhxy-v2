"""副机只读指针记录器（E3）：只调用 GetCursorPos，不移动鼠标、不注入任何输入。

在副机上运行（标准库，无需第三方包）：
    python cursor_log.py --seconds 30
    python cursor_log.py --seconds 30 --out D:\cursor_log.json --hz 500 --delay 3

记录开始后请在主机运行 E3 发送脚本。记录期间不要动副机鼠标，
否则额外位置会让分析结果不可信。结束后把 JSON 发回主机。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int


def set_dpi_aware() -> bool:
    try:
        awareness = user32.SetProcessDpiAwarenessContext
        awareness.argtypes = [ctypes.c_void_p]
        awareness.restype = wintypes.BOOL
        if awareness(ctypes.c_void_p(-4)):
            return True
    except (AttributeError, OSError):
        pass
    try:
        return bool(user32.SetProcessDPIAware())
    except (AttributeError, OSError):
        return False


def cursor_pos() -> tuple[int, int]:
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise ctypes.WinError()
    return int(point.x), int(point.y)


def screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def record(delay_s: float, duration_s: float, hz: float, sampler,
           clock=time.perf_counter, sleeper=time.sleep) -> list[list[float]]:
    if duration_s <= 0:
        raise ValueError("记录时长必须大于 0")
    if hz <= 0:
        raise ValueError("采样率必须大于 0")
    interval = 1.0 / hz
    if delay_s > 0:
        sleeper(delay_s)
    start = clock()
    samples: list[list[float]] = []
    next_at = start
    while True:
        now = clock()
        elapsed = now - start
        if elapsed >= duration_s:
            break
        x, y = sampler()
        samples.append([round(elapsed * 1000.0, 2), int(x), int(y)])
        next_at += interval
        remaining = next_at - clock()
        if remaining > 0:
            sleeper(remaining)
    return samples


def summarize(samples) -> dict:
    runs: list[list[float]] = []
    for sample in samples:
        t, x, y = sample
        if runs and runs[-1][2] == x and runs[-1][3] == y:
            runs[-1][1] = t
        else:
            runs.append([t, t, x, y])
    gaps = [round(b[0] - a[0], 2) for a, b in zip(samples, samples[1:])]
    dwells = [round(run[1] - run[0], 2) for run in runs]

    def pick(values, p):
        values = sorted(values)
        if not values:
            return 0.0
        k = (len(values) - 1) * p
        lo = int(k)
        hi = min(lo + 1, len(values) - 1)
        return round(values[lo] + (values[hi] - values[lo]) * (k - lo), 2)

    return {
        "samples": len(samples),
        "duration_ms": round(samples[-1][0] - samples[0][0], 2) if samples else 0.0,
        "runs": len(runs),
        "sample_interval_ms_p50": pick(gaps, 0.5),
        "sample_interval_ms_p95": pick(gaps, 0.95),
        "sample_interval_ms_max": max(gaps) if gaps else 0.0,
        "run_dwell_ms_p50": pick(dwells, 0.5),
        "run_dwell_ms_p95": pick(dwells, 0.95),
        "run_dwell_ms_max": max(dwells) if dwells else 0.0,
    }


def build_payload(delay_s: float, seconds: float, hz: float, samples,
                  started_at: float, ended_at: float, dpi_aware: bool,
                  screen: tuple[int, int]) -> dict:
    return {
        "tool": "cursor_log",
        "started_at": started_at,
        "ended_at": ended_at,
        "delay_s": delay_s,
        "seconds": seconds,
        "hz": hz,
        "dpi_aware": dpi_aware,
        "screen": [int(screen[0]), int(screen[1])],
        "summary": summarize(samples),
        "samples": samples,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="副机只读指针记录器（E3）")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--hz", type=float, default=500.0)
    parser.add_argument("--delay", type=float, default=3.0)
    parser.add_argument("--out", default="cursor_log.json")
    args = parser.parse_args(argv)
    if args.seconds <= 0 or args.hz <= 0:
        parser.error("--seconds 和 --hz 必须大于 0")
    aware = set_dpi_aware()
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except (AttributeError, OSError):
        pass
    try:
        print(f"只读记录 {args.seconds:g} 秒，{args.hz:g} Hz；{args.delay:g} 秒后开始。", flush=True)
        print("本工具只读 GetCursorPos，不移动鼠标、不注入输入。", flush=True)
        print("记录期间不要动副机鼠标；请在主机运行 E3 发送脚本。", flush=True)
        remaining = int(args.delay)
        for _ in range(remaining):
            print(f"  {remaining}...", flush=True)
            time.sleep(1)
            remaining -= 1
        print("RECORDING", flush=True)
        started_at = time.time()
        samples = record(0.0, args.seconds, args.hz, cursor_pos)
        ended_at = time.time()
    finally:
        try:
            ctypes.windll.winmm.timeEndPeriod(1)
        except (AttributeError, OSError):
            pass
    payload = build_payload(args.delay, args.seconds, args.hz, samples,
                            started_at, ended_at, aware, screen_size())
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    summary = payload["summary"]
    print(f"记录结束：{summary['samples']} 个样本、{summary['runs']} 个连续位置。", flush=True)
    print(f"采样间隔 p50={summary['sample_interval_ms_p50']} ms，"
          f"p95={summary['sample_interval_ms_p95']} ms -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
