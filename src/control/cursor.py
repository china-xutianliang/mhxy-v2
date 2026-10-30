"""每台副机独立维护的指针起点状态，不执行任何输入。

Actuator 每次规划轨迹前调用 ``start_for(gx, gy)``，发送完轨迹后调用
``mark_moved(gx, gy)``。调用方只提供目标，不能传入或设置起点。

检测器只需提供三个成员：``frame_ts``（当前帧时间戳，无帧为 None）、
``refresh()`` 和 ``detect(near)``。``near`` 是上次已知位置；局部搜索后
全画面搜索由检测器内部完成，返回游戏坐标 G 或 None。
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

Point = tuple[int, int]
CursorSource = Literal["detected", "tracked", "unknown"]
FRAME_MAX_AGE_S = 1.5
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class CursorState:
    pos: Point | None
    source: CursorSource
    ts: float


class CursorDetector(Protocol):
    frame_ts: float | None

    def refresh(self) -> None:
        ...

    def detect(self, near: Point | None) -> Point | None:
        ...


class CursorTracker:
    """为一次动作确定起点；实例不得在不同副机之间共享。"""

    def __init__(self, detector: CursorDetector | None = None, *,
                 min_detect_interval: float = 0.5,
                 clock: Callable[[], float] = time.monotonic):
        if min_detect_interval < 0:
            raise ValueError("min_detect_interval 不能为负数")
        self.detector = detector
        self.min_detect_interval = min_detect_interval
        self._clock = clock
        self._tracked_pos: Point | None = None
        self._last_move_ts: float | None = None
        self._state = CursorState(None, "unknown", self._clock())

    @property
    def cursor(self) -> CursorState:
        """当前状态的只读快照；没有公开的 setter。"""
        return self._state

    def start_for(self, gx: int, gy: int) -> Point:
        """在动作前自动确定起点，返回值仅供内部轨迹规划使用。"""
        self._check_g(gx, gy)
        target = (gx, gy)
        now = self._clock()

        # 刚发完动作时，画面往往还没反映新位置，避免旧检测值覆盖跟踪值。
        if (self._tracked_pos is not None and self._last_move_ts is not None
                and now - self._last_move_ts < self.min_detect_interval):
            self._state = CursorState(self._tracked_pos, "tracked", now)
            return self._tracked_pos

        detected = self._detect(now)
        if detected is not None:
            if (self._tracked_pos is not None
                    and math.dist(detected, self._tracked_pos) > 3.0):
                LOG.warning("指针检测值 %s 与跟踪值 %s 偏差超过 3px，以检测值为准",
                            detected, self._tracked_pos)
            self._state = CursorState(detected, "detected", now)
            return detected

        if self._tracked_pos is not None:
            self._state = CursorState(self._tracked_pos, "tracked", now)
            return self._tracked_pos

        entry = self._nearest_entry(target)
        self._state = CursorState(None, "unknown", now)
        LOG.info("指针位置未知，目标 %s 将从画面外 %s 进入", target, entry)
        return entry

    def mark_moved(self, gx: int, gy: int) -> None:
        """移动发送完成后记录终点；只有此方法能更新跟踪位置。"""
        self._check_g(gx, gy)
        now = self._clock()
        self._tracked_pos = (gx, gy)
        self._last_move_ts = now
        self._state = CursorState(self._tracked_pos, "tracked", now)

    def invalidate(self) -> None:
        """重连、查看器重启、绑定切换或急停后，丢弃位置与连续动作时序。"""
        self.reset()

    def reset(self) -> None:
        """恢复完全未知状态，下次动作优先尝试画面检测。"""
        self._tracked_pos = None
        self._last_move_ts = None
        self._state = CursorState(None, "unknown", self._clock())

    def _detect(self, now: float) -> Point | None:
        if self.detector is None:
            return None
        frame_ts = self.detector.frame_ts
        if frame_ts is None or now - frame_ts > FRAME_MAX_AGE_S:
            self.detector.refresh()
        detected = self.detector.detect(self._state.pos)
        if detected is None:
            return None
        gx, gy = detected
        self._check_g(gx, gy)
        return (gx, gy)

    @staticmethod
    def _check_g(gx: int, gy: int) -> None:
        if not (0 <= gx < 800 and 0 <= gy < 600):
            raise ValueError(f"游戏坐标越界：({gx},{gy})，范围 0..799 x 0..599")

    @staticmethod
    def _nearest_entry(target: Point) -> Point:
        x, y = target
        candidates = (
            (x + 1, (-1, y)),
            (800 - x, (800, y)),
            (y + 1, (x, -1)),
            (600 - y, (x, 600)),
        )
        return min(candidates, key=lambda item: item[0])[1]
