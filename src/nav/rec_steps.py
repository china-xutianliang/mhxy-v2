"""把每帧读到的 (场景名, 坐标) 变成"步骤"：换图、同图坐标跳变（用旗/飞行符/传送）。

纯逻辑，不碰窗口和图片，录制时在线用，整理旧录像时离线回放 hud.bin 也用同一套。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# 用户在采集器里点的动作按钮；value 是 links.yaml 里的连接类型
ACTIONS = {
    "charm": "用了飞行符",
    "flag": "用了旗",
    "portal": "走传送圈/出口",
    "npc": "和NPC对话传送",
    "enter": "进门/上下楼",
    "post": "驿站/车夫",
    "earth": "土地公公",
    "other": "其他",
}
# 备注里的关键字 → 动作；顺序有讲究："传送圈"要先于"传送"，"驿站"要先于"传送"
_NOTE_KEYS = (("飞行符", "charm"), ("旗", "flag"), ("传送圈", "portal"), ("土地", "earth"),
              ("驿站", "post"), ("车夫", "post"), ("NPC", "npc"), ("接引", "npc"), ("传送", "npc"),
              ("进门", "enter"), ("楼", "enter"))


def action_from_note(text: str) -> str | None:
    for key, act in _NOTE_KEYS:
        if key in (text or ""):
            return act
    return None


def dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass
class StepTracker:
    """feed 每帧读数，返回本帧新完成的步骤（dict 列表）。

    - 场景名连续 confirm 帧一致才算换图；读不到（过图黑屏、被遮挡）的帧直接跳过；
    - 同一场景里，新坐标离上次坐标超过"按最快走路速度能走到的距离"，并且连续 confirm 帧一致，才算跳变；
    - 换图后要等到新场景第一次读到坐标，步骤才完整（最多等 wait_xy_s 秒，超时坐标记 None）；
    - 新名字图库里没有、而坐标还是走路能到的距离：多半是鼠标挡住了地图名，不算换图。
    """
    confirm: int = 2
    speed_max: float = 12.0      # 坐标/秒；实测走路约 5~7，留余量
    jump_min: float = 12.0
    wait_xy_s: float = 10.0
    scene: str | None = None
    pos: tuple[int, int] | None = None
    pos_t: float = 0.0
    _cand_scene: str | None = None
    _cand_scene_n: int = 0
    _cand_xy: tuple[int, int] | None = None
    _cand_xy_n: int = 0
    _cand_xy_t: float = 0.0
    _open: dict | None = None
    steps: list[dict] = field(default_factory=list)

    def _emit(self, step: dict) -> dict:
        step["no"] = len(self.steps) + 1
        self.steps.append(step)
        return step

    def feed(self, t: float, scene: str | None, xy: tuple[int, int] | None,
             known: bool = True) -> list[dict]:
        out: list[dict] = []
        if self._open is not None and t - self._open["t"] > self.wait_xy_s:
            out.append(self._emit(self._open))
            self._open = None
        if scene is None:
            return out
        if self.scene is None:
            self.scene = scene
            if xy:
                self.pos, self.pos_t = xy, t
            return out

        if (scene != self.scene and not known and xy is not None and self.pos is not None
                and dist(xy, self.pos) <= self.jump_min + self.speed_max * max(0.0, t - self.pos_t)):
            scene = self.scene
        if scene != self.scene:
            if scene == self._cand_scene:
                self._cand_scene_n += 1
            else:
                self._cand_scene, self._cand_scene_n = scene, 1
            if self._cand_scene_n < self.confirm:
                return out
            if self._open is not None:
                out.append(self._emit(self._open))
            self._open = {"kind": "scene", "t": t, "from": self.scene, "from_xy": self.pos,
                          "from_t": self.pos_t, "to": scene, "to_xy": None}
            self.scene, self.pos = scene, None
            self._cand_scene, self._cand_scene_n = None, 0
            self._cand_xy, self._cand_xy_n = None, 0
        else:
            self._cand_scene, self._cand_scene_n = None, 0

        if xy is None:
            return out
        if self._open is not None and self._open["to"] == self.scene:
            self._open["to_xy"] = xy
            self._open["to_t"] = t
            out.append(self._emit(self._open))
            self._open = None
            self.pos, self.pos_t = xy, t
            return out
        if self.pos is None:
            self.pos, self.pos_t = xy, t
            return out

        reach = self.jump_min + self.speed_max * max(0.0, t - self.pos_t)
        if dist(xy, self.pos) <= reach:
            self.pos, self.pos_t = xy, t
            self._cand_xy, self._cand_xy_n = None, 0
            return out
        if self._cand_xy is not None and dist(xy, self._cand_xy) <= self.jump_min:
            self._cand_xy_n += 1
        else:
            self._cand_xy, self._cand_xy_n, self._cand_xy_t = xy, 1, t
        if self._cand_xy_n >= self.confirm:
            out.append(self._emit({"kind": "jump", "t": self._cand_xy_t, "from": self.scene,
                                   "from_xy": self.pos, "from_t": self.pos_t, "to": self.scene,
                                   "to_xy": self._cand_xy, "to_t": self._cand_xy_t}))
            self.pos, self.pos_t = xy, t
            self._cand_xy, self._cand_xy_n = None, 0
        return out

    def rename(self, old: str, new: str) -> None:
        """用户给"未收录地图N"起了名：已有步骤和当前状态里的旧名一起改掉，避免被当成换图。"""
        if self.scene == old:
            self.scene = new
        if self._cand_scene == old:
            self._cand_scene = new
        for s in self.steps + ([self._open] if self._open else []):
            for k in ("from", "to"):
                if s.get(k) == old:
                    s[k] = new

    def flush(self) -> list[dict]:
        if self._open is None:
            return []
        step = self._emit(self._open)
        self._open = None
        return [step]


def assign_actions(steps: list[dict], actions: list[dict], late_s: float = 15.0) -> None:
    """给每个步骤配上用户点的动作：上一步之后、本步之前点的最后一个；
    都没有时，本步之后 late_s 秒内补点的也算（用户常常到了才想起来点）。每个动作只用一次。"""
    used: set[int] = set()
    prev_t = -1e9
    for i, s in enumerate(steps):
        start = s.get("from_t", s["t"]) if s.get("from_t") is not None else s["t"]
        cands = [j for j, a in enumerate(actions) if j not in used and prev_t < a["t"] <= s["t"]]
        if not cands:
            nxt = steps[i + 1]["t"] if i + 1 < len(steps) else 1e18
            cands = [j for j, a in enumerate(actions)
                     if j not in used and s["t"] < a["t"] <= min(s["t"] + late_s, nxt)][:1]
        if cands:
            j = cands[-1]
            used.add(j)
            s["how"] = actions[j]["action"]
            s["how_src"] = "用户点选"
            if actions[j].get("text"):
                s["how_note"] = actions[j]["text"]
            if actions[j].get("frame"):
                s["how_frame"] = actions[j]["frame"]
        else:
            s["how"] = "flag_or_charm" if s["kind"] == "jump" else "walk_or_portal"
            s["how_src"] = "推测"
        prev_t = max(s["t"], start)


HOW_LABEL = dict(ACTIONS, flag_or_charm="旗或飞行符（未点选）", walk_or_portal="走过去/传送圈（未点选）")


def walk_speed(track: list[tuple[float, str, tuple[int, int]]], speed_max: float = 12.0) -> float | None:
    """从 (t, 场景, 坐标) 轨迹估计走路速度：只取同场景、确实在动、且没超过最快速度的相邻两帧，取中位数。"""
    v = []
    for (t0, s0, p0), (t1, s1, p1) in zip(track, track[1:]):
        dt = t1 - t0
        if s0 != s1 or dt <= 0 or dt > 2.0:
            continue
        d = dist(p0, p1)
        if 0 < d <= speed_max * dt:
            v.append(d / dt)
    if len(v) < 5:
        return None
    v.sort()
    return round(v[len(v) // 2], 2)
