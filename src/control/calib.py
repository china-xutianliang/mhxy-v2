from __future__ import annotations

import os
import time
from dataclasses import dataclass

from common.config import resources_path, runtime_dir

NINE_POINTS = (
    (40, 40), (400, 40), (760, 40),
    (40, 300), (400, 300), (760, 300),
    (40, 560), (400, 560), (760, 560),
)


@dataclass(frozen=True)
class CalibrationRow:
    target: tuple[int, int]
    detected: tuple[int, int] | None
    deviation: float | None
    detect_ms: int
    screenshot: str = ""


def assess_rows(rows: list[CalibrationRow], tolerance_px: float = 2.0) -> tuple[bool, str]:
    detected = [row for row in rows if row.detected is not None]
    rate = len(detected) / len(rows) if rows else 0.0
    accurate = [row for row in rows if row.deviation is not None and row.deviation <= tolerance_px]
    ok = bool(rows) and rate >= 0.95 and len(accurate) == len(rows)
    worst = max((row.deviation for row in detected if row.deviation is not None), default=None)
    detail = f"检出 {len(detected)}/{len(rows)}（{rate:.0%}），最大偏差 {worst if worst is not None else '无'}px"
    return ok, detail


class TemplatePointerDetector:
    def __init__(self, channel, viewport, *, hotspot=(0, 0), clock=time.monotonic,
                 templates=("mouse1.bmp", "mouse2.bmp", "mouse3.bmp", "mouse4.bmp")):
        self.channel = channel
        self.vp = viewport
        self.hotspot = hotspot
        self._clock = clock
        self.templates = "|".join(templates)
        self.frame_ts = None
        self.last_ms = 0
        self.last_raw = None
        self.last_screenshot = ""
        self.channel.set_pic_dir(resources_path("templates"))

    def refresh(self) -> None:
        self.refresh_to(os.path.join(runtime_dir(), "layer2_pointer_frame.jpg"))

    def refresh_to(self, path: str) -> None:
        width, height = self.vp.client_size()
        if not self.channel.capture_jpg(0, 0, width, height, path):
            self.frame_ts = None
            raise RuntimeError("指针检测抓图失败")
        self.frame_ts = self._clock()
        self.last_screenshot = path

    def detect(self, near) -> tuple[int, int] | None:
        started = time.perf_counter()
        hit = None
        if near is not None:
            cx, cy = self.vp.g_to_capture(*near)
            radius_x = max(40, round(80 * self.vp.capture_scale))
            radius_y = max(40, round(80 * self.vp.capture_scale))
            width, height = self.vp.client_size()
            hit = self.channel.find_pic(max(0, cx - radius_x), max(0, cy - radius_y),
                                        min(width - 1, cx + radius_x), min(height - 1, cy + radius_y),
                                        self.templates, sim=0.85)
        if hit is None:
            width, height = self.vp.client_size()
            hit = self.channel.find_pic(0, 0, width - 1, height - 1, self.templates, sim=0.85)
        self.last_ms = round((time.perf_counter() - started) * 1000)
        self.last_raw = hit
        if hit is None:
            return None
        gx, gy = self.vp.capture_to_g(*hit)
        return gx + self.hotspot[0], gy + self.hotspot[1]
