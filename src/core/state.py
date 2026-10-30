"""感知层：把驱动读到的原始OCR结果，组装成结构化的 GameState 快照。

只负责"稳定拿到场景名+坐标"这一件事；战斗/对话框/验证码等判断
留给 interrupt 层扩展，不在这里混进去。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from core.config import load_yaml
from core.driver import IDriver


@dataclass
class GameState:
    scene: str
    x: int
    y: int


class StateReader:
    def __init__(self):
        self._regions = load_yaml("config", "game.yaml")["ocr_regions"]

    def snapshot(self, drv: IDriver) -> GameState:
        scene = self._read_scene(drv)
        x, y = self._read_coords(drv)
        return GameState(scene=scene, x=x, y=y)

    def _read_scene(self, drv: IDriver) -> str:
        r = self._regions["current_scene_name"]
        scene = drv.ocr(r["x1"], r["y1"], r["x2"], r["y2"], r["color"])
        if not scene:
            raise RuntimeError("场景名OCR识别为空，可能被弹窗/验证码遮挡")
        return scene

    def _read_coords(self, drv: IDriver) -> tuple[int, int]:
        r = self._regions["current_coords"]
        text = drv.ocr(r["x1"], r["y1"], r["x2"], r["y2"], r["color"])
        match = re.search(r"(\d+).*?(\d+)", text or "")
        if not match:
            raise RuntimeError(f"坐标OCR识别失败，原始结果: {text!r}")
        return int(match.group(1)), int(match.group(2))
