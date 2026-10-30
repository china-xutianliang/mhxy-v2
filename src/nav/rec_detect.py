"""录制用的画面判断（纯函数，便于单测）。需要 numpy，只在主控机 64 位 Python 里用。

所有阈值都从参数传入，默认值是估计值；录制时每一帧的指标都会写进 ticks.jsonl，
拿到真机数据后按实际分布再调。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

Roi = tuple[int, int, int, int]  # x1, y1, x2, y2（G 坐标，含左上不含右下）


def to_array(rgb: bytes, w: int, h: int) -> np.ndarray:
    return np.frombuffer(rgb, dtype=np.uint8).reshape(h, w, 3)


def crop(frame: np.ndarray, roi: Roi) -> np.ndarray:
    x1, y1, x2, y2 = roi
    return frame[y1:y2, x1:x2]


def text_mask(region: np.ndarray, min_level: int = 200) -> np.ndarray:
    """亮色文字的二值掩码（场景名、坐标一般是白/亮色字）。"""
    return region.min(axis=2) >= min_level


def mask_diff(a: np.ndarray, b: np.ndarray) -> float:
    """两个掩码不同像素占"任一方为文字"像素的比例；都为空返回 0。"""
    union = np.logical_or(a, b).sum()
    if union == 0:
        return 0.0
    return float(np.logical_xor(a, b).sum()) / float(union)


@dataclass
class SceneNameWatcher:
    """盯场景名区域：新的文字形状连续出现 confirm 帧才算换图；空白（过图黑屏）不算。"""
    change_ratio: float = 0.25
    min_pixels: int = 12
    confirm: int = 2
    stable: np.ndarray | None = None
    _cand: np.ndarray | None = None
    _count: int = 0

    def feed(self, mask: np.ndarray) -> bool:
        if int(mask.sum()) < self.min_pixels:
            self._cand, self._count = None, 0
            return False
        if self.stable is None:
            self.stable = mask
            return False
        if mask_diff(mask, self.stable) < self.change_ratio:
            self._cand, self._count = None, 0
            return False
        if self._cand is not None and mask_diff(mask, self._cand) < self.change_ratio:
            self._count += 1
        else:
            self._cand, self._count = mask, 1
        if self._count >= self.confirm:
            self.stable, self._cand, self._count = mask, None, 0
            return True
        return False


def small_gray(frame: np.ndarray, step: int = 4) -> np.ndarray:
    return frame[::step, ::step].astype(np.float32).mean(axis=2)


def motion(prev: np.ndarray, cur: np.ndarray) -> dict:
    """相位相关求整体平移，再算对齐后的残差。

    返回 raw（不对齐的平均差 0~1）、shift（dy, dx，缩小后像素）、residual（对齐后平均差 0~1）。
    走路时画面整体平移：raw 大、residual 小；换图/瞬移：两者都大。
    """
    a = prev - prev.mean()
    b = cur - cur.mean()
    win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1])).astype(np.float32)
    fa, fb = np.fft.fft2(a * win), np.fft.fft2(b * win)
    r = fa * np.conj(fb)
    r /= np.abs(r) + 1e-6
    corr = np.fft.ifft2(r).real
    dy, dx = np.unravel_index(int(np.argmax(corr)), corr.shape)
    h, w = a.shape
    dy = dy - h if dy > h // 2 else dy
    dx = dx - w if dx > w // 2 else dx
    # prev 平移 (dy, dx) 后与 cur 对齐：cur[y, x] ≈ prev[y + dy, x + dx]
    ys, ye = max(0, -dy), min(h, h - dy)
    xs, xe = max(0, -dx), min(w, w - dx)
    if ye - ys < h // 3 or xe - xs < w // 3:
        residual = 1.0
    else:
        residual = float(np.abs(cur[ys:ye, xs:xe] - prev[ys + dy:ye + dy, xs + dx:xe + dx]).mean() / 255.0)
    raw = float(np.abs(cur - prev).mean() / 255.0)
    return {"raw": raw, "shift": (int(dy), int(dx)), "residual": residual}


def align_ratio(m: dict) -> float:
    """对齐后残差 ÷ 不对齐差。平移（走路）时对齐能消掉大部分差异，比值小；突变时对齐无用，比值接近 1。"""
    return m["residual"] / max(m["raw"], 1e-6)


def classify(m: dict, still: float = 0.01, cut: float = 0.7) -> str:
    """still 静止 | scroll 平移（走路） | cut 画面突变（换图、用旗、开大界面）。

    用比值而不是残差绝对值，这样不同地图的纹理强弱不影响判断。
    """
    if m["raw"] < still:
        return "still"
    if align_ratio(m) >= cut:
        return "cut"
    return "scroll"
