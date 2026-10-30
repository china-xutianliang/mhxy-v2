"""读游戏左上角 HUD：场景名、坐标。纯 numpy，不依赖 OCR 引擎。

游戏 800x600 画面里，左上角小地图面板的文字是固定的像素字体，查看器关闭 JPEG 时
每个字的像素完全一样，所以用模板比对就能读：
- 坐标：一行 "X:541 Y:3"，字高 10 像素，模板写死在 GLYPHS 里（取自 2026-10-30 实录画面）；
- 场景名：中文，字形多，不写死，用"名字图库"比对；图库 = resources/nav/hud/names/*.png
  （随程序发布）+ runtime/nav/hud_names/*.png（用户在采集器里起的名字）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import numpy as np

# G 坐标（800x600 画面）里的区域：x1, y1, x2, y2（含左上不含右下）
COORD_ROI = (20, 66, 112, 76)
NAME_ROI = (22, 14, 97, 30)
TEXT_LEVEL = 200          # 白字：RGB 三个通道都 >= 200
NAME_MIN_PIXELS = 15
# 游戏鼠标划过地图名时会叠上几个白点：实测被挡的名字与正确名字距离 <= 0.05，不同名字之间 >= 0.70
OCCLUDED_MAX = 0.25
OCCLUDED_OUTSIDE = 0.10   # 落在名字外框以外的多余白点，超过名字像素的 10% 就当成另一个更长的名字

_RAW = {
    "0": ["..##..", ".#..#.", "#....#", "#....#", "#....#", "#....#", "#....#", "#....#", ".#..#.", "..##.."],
    "1": ["..#..", "###..", "..#..", "..#..", "..#..", "..#..", "..#..", "..#..", "..#..", "#####"],
    "2": [".###.", "#...#", "#...#", "....#", "...#.", "...#.", "..#..", ".#...", "#...#", "#####"],
    "3": [".###.", "#...#", "#...#", "....#", "..##.", "....#", "....#", "#...#", "#...#", ".###."],
    "4": ["...#.", "...#.", "..##.", ".#.#.", ".#.#.", "#..#.", "#####", "...#.", "...#.", "..###"],
    "5": ["#####", "#....", "#....", "####.", "#...#", "....#", "....#", "#...#", "#...#", ".###."],
    "6": ["..##..", ".#..#.", "#.....", "#.###.", "##...#", "#....#", "#....#", "#....#", ".#...#", "..###."],
    "7": ["#####", "#...#", "...#.", "...#.", "...#.", "..#..", "..#..", "..#..", "..#..", "..#.."],
    "8": [".####.", "#....#", "#....#", "#....#", ".####.", ".#..#.", "#....#", "#....#", "#....#", ".####."],
    "9": [".###..", "#...#.", "#....#", "#....#", "#...##", ".###.#", ".....#", ".....#", ".#..#.", "..##.."],
    "X": [".......", "###.###", ".#...#.", "..#.#..", "..#.#..", "...#...", "..#.#..", "..#.#..", ".#...#.", "###..##"],
    "Y": [".......", "###.###", ".#...#.", "..#.#..", "..#.#..", "...#...", "...#...", "...#...", "...#...", "..###.."],
    ":": ["..", "..", "..", "##", "##", "..", "..", "..", "##", "##"],
}
GLYPHS = {k: np.array([[c == "#" for c in row] for row in v], dtype=bool) for k, v in _RAW.items()}
_COORD_RE = re.compile(r"X:(\d{1,4})Y:(\d{1,4})$")


def text_mask(region: np.ndarray, level: int = TEXT_LEVEL) -> np.ndarray:
    return region.min(axis=2) >= level


def _crop(frame: np.ndarray, roi) -> np.ndarray:
    x1, y1, x2, y2 = roi
    return frame[y1:y2, x1:x2]


def _match_glyph(g: np.ndarray, max_bad: int = 3) -> tuple[str, int] | None:
    """g 已经是 10 行。返回 (字符, 不同像素数)；没有足够像的返回 None。"""
    best = None
    for ch, tpl in GLYPHS.items():
        if abs(tpl.shape[1] - g.shape[1]) > 1:
            continue
        w = max(tpl.shape[1], g.shape[1])
        a = np.zeros((10, w), bool)
        b = np.zeros((10, w), bool)
        a[:, :g.shape[1]] = g
        b[:, :tpl.shape[1]] = tpl
        bad = int(np.logical_xor(a, b).sum())
        if best is None or bad < best[1]:
            best = (ch, bad)
    if best is None or best[1] > max_bad:
        return None
    return best


def _split(seg: np.ndarray) -> list[str] | None:
    """一段连在一起的列（字挨着字）：从左往右贪心地切出最像的模板。"""
    if seg.shape[1] <= 7:
        m = _match_glyph(seg)
        return [m[0]] if m else None
    best = None
    for ch, tpl in GLYPHS.items():
        w = tpl.shape[1]
        if w >= seg.shape[1]:
            continue
        bad = int(np.logical_xor(seg[:, :w], tpl).sum())
        if bad <= 2 and (best is None or bad < best[1]):
            best = (ch, bad, w)
    if best is None:
        return None
    rest = seg[:, best[2]:]
    cols = np.where(rest.any(axis=0))[0]
    if cols.size == 0:
        return [best[0]]
    tail = _split(rest[:, cols[0]:])
    return None if tail is None else [best[0]] + tail


def read_coord_text(frame: np.ndarray) -> str | None:
    m = text_mask(_crop(frame, COORD_ROI))
    cols = m.any(axis=0)
    out: list[str] = []
    x, w = 0, m.shape[1]
    while x < w:
        if not cols[x]:
            x += 1
            continue
        s = x
        while x < w and cols[x]:
            x += 1
        chars = _split(m[:, s:x])
        if chars is None:
            return None
        out.extend(chars)
    return "".join(out) or None


def read_coords(frame: np.ndarray) -> tuple[int, int] | None:
    """读 "X:541 Y:3" → (541, 3)。被遮挡、过图黑屏、字形不认识都返回 None。"""
    text = read_coord_text(frame)
    if not text:
        return None
    m = _COORD_RE.match(text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def name_mask(frame: np.ndarray) -> np.ndarray | None:
    """场景名的白字掩码，裁到文字外框；像素太少（过图、被遮挡）返回 None。"""
    m = text_mask(_crop(frame, NAME_ROI))
    if int(m.sum()) < NAME_MIN_PIXELS:
        return None
    ys = np.where(m.any(axis=1))[0]
    xs = np.where(m.any(axis=0))[0]
    return m[ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1]


def mask_distance(a: np.ndarray, b: np.ndarray) -> float:
    """两个掩码左上对齐后，不同像素占并集的比例；尺寸差太多直接算 1。"""
    if abs(a.shape[0] - b.shape[0]) > 1 or abs(a.shape[1] - b.shape[1]) > 2:
        return 1.0
    h, w = max(a.shape[0], b.shape[0]), max(a.shape[1], b.shape[1])
    A = np.zeros((h, w), bool)
    B = np.zeros((h, w), bool)
    A[:a.shape[0], :a.shape[1]] = a
    B[:b.shape[0], :b.shape[1]] = b
    union = int(np.logical_or(A, B).sum())
    return float(np.logical_xor(A, B).sum()) / max(1, union)


def occluded_distance(obs: np.ndarray, ref: np.ndarray) -> float:
    """容忍遮挡的距离：ref 在 obs 里滑动找最合适的位置，算 (缺的 + 多的) / ref 像素数。
    obs 被鼠标叠了白点时外框会变大、左上角会偏，mask_distance 直接判成 1，这里还能认出来。"""
    n = int(ref.sum())
    if n == 0:
        return 1.0
    H, W = obs.shape
    h, w = ref.shape
    if H < h - 1 or W < w - 2:
        return 1.0
    total = int(obs.sum())
    best = 1.0
    for dy in range(-1, max(0, H - h) + 2):
        for dx in range(-2, max(0, W - w) + 3):
            y0, x0, y1, x1 = max(0, dy), max(0, dx), min(H, dy + h), min(W, dx + w)
            if y1 <= y0 or x1 <= x0:
                continue
            win = np.zeros((h, w), bool)
            win[y0 - dy:y1 - dy, x0 - dx:x1 - dx] = obs[y0:y1, x0:x1]
            inside = int(win.sum())
            if total - inside > OCCLUDED_OUTSIDE * n:
                continue
            score = float(np.logical_xor(win, ref).sum()) / n
            best = min(best, score)
    return best


def _safe_file(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "unnamed"


@dataclass
class NameBook:
    """场景名图库。match 认不出的名字按字形分组成"未收录地图N"，同一个字形始终是同一个编号。"""
    entries: list[tuple[str, np.ndarray]] = field(default_factory=list)
    unknown: list[np.ndarray] = field(default_factory=list)
    threshold: float = 0.12
    user_dir: str | None = None
    _cache: dict = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, *dirs: str, user_dir: str | None = None) -> "NameBook":
        from PIL import Image
        book = cls(user_dir=user_dir)
        for d in list(dirs) + ([user_dir] if user_dir else []):
            if not d or not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if fn.lower().endswith(".png"):
                    name = os.path.splitext(fn)[0].split("__")[0]
                    arr = np.asarray(Image.open(os.path.join(d, fn)).convert("L")) > 127
                    book.entries.append((name, arr))
        return book

    @staticmethod
    def _best(mask: np.ndarray, refs, threshold: float):
        """先按完全对齐比；都不像时再按"可能被鼠标挡住"比。返回 refs 里的下标或 None。"""
        best = None
        for i, ref in enumerate(refs):
            d = mask_distance(mask, ref)
            if d < threshold and (best is None or d < best[1]):
                best = (i, d)
        if best is None:
            for i, ref in enumerate(refs):
                d = occluded_distance(mask, ref)
                if d < OCCLUDED_MAX and (best is None or d < best[1]):
                    best = (i, d)
        return None if best is None else best[0]

    def match(self, mask: np.ndarray) -> str | None:
        i = self._best(mask, [ref for _, ref in self.entries], self.threshold)
        return None if i is None else self.entries[i][0]

    def unknown_id(self, mask: np.ndarray) -> int:
        i = self._best(mask, self.unknown, self.threshold)
        if i is not None:
            return i + 1
        self.unknown.append(mask)
        return len(self.unknown)

    def identify(self, mask: np.ndarray) -> tuple[str, bool]:
        """返回 (名字, 是否已收录)。未收录的名字形如"未收录地图3"。"""
        key = (mask.shape, mask.tobytes())
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        name = self.match(mask)
        out = (name, True) if name else (f"未收录地图{self.unknown_id(mask)}", False)
        if len(self._cache) > 512:
            self._cache.clear()
        self._cache[key] = out
        return out

    def add(self, name: str, mask: np.ndarray, persist: bool = True) -> str | None:
        """用户给字形起名；persist 时存进 user_dir，下次启动仍认得。返回保存路径。"""
        name = " ".join(str(name).split())
        if not name:
            raise ValueError("名字不能为空")
        self.entries.append((name, mask))
        self._cache.clear()
        if not (persist and self.user_dir):
            return None
        from PIL import Image
        os.makedirs(self.user_dir, exist_ok=True)
        base = _safe_file(name)
        path = os.path.join(self.user_dir, base + ".png")
        n = 2
        while os.path.exists(path):
            path = os.path.join(self.user_dir, f"{base}__{n}.png")
            n += 1
        Image.fromarray((mask * 255).astype(np.uint8)).save(path)
        return path


def builtin_dir() -> str:
    from common.config import resources_path
    return resources_path("nav", "hud", "names")


def user_dir() -> str:
    from common.config import runtime_dir
    return os.path.join(runtime_dir(), "nav", "hud_names")


def default_book() -> NameBook:
    return NameBook.load(builtin_dir(), user_dir=user_dir())


@dataclass
class HudReading:
    name: str | None          # 场景名（或"未收录地图N"）；None = 这一帧读不到
    known: bool               # 名字是否在图库里
    xy: tuple[int, int] | None
    mask: np.ndarray | None = None


def read_hud(frame: np.ndarray, book: NameBook) -> HudReading:
    mask = name_mask(frame)
    xy = read_coords(frame)
    if mask is None:
        return HudReading(None, False, xy, None)
    name, known = book.identify(mask)
    return HudReading(name, known, xy, mask)
