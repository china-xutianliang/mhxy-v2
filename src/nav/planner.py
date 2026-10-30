"""规划器：在 (场景, 场景内位置) 上求总耗时最小的路线。

代价全部是秒：走路 = 距离 ÷ 速度；过场景 = 动作开销 + 低可信度罚时；
旗/飞行符 = 动作开销 + 消耗品按点卡时间价值折算的秒。没有固定的"用旗阈值"。
"""
from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field

from nav.world.model import Point, World


class NoRoute(RuntimeError):
    pass


@dataclass(frozen=True)
class Step:
    kind: str                 # walk | portal/enter/npc/post/earth/stair/exit | flag | charm
    scene: str                # 这一步开始时所在场景
    to_scene: str
    at: Point | None          # walk: 目标点；过场景：触发位置
    arrive: Point | None      # 这一步结束时的位置（未知为 None）
    cost_s: float
    via: str | None = None
    opt: str | None = None
    ref: str | None = None    # 连接 id / 旗名#点位 / 飞行符目的地
    conf: str | None = None


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    total_s: float = 0.0

    def describe(self) -> str:
        def p(v):
            return f"({v[0]},{v[1]})" if v else "(?)"
        lines = []
        for i, s in enumerate(self.steps, 1):
            if s.kind == "walk":
                lines.append(f"{i}. 在 {s.scene} 走到 {p(s.at)}  ~{s.cost_s:.0f}s")
            elif s.kind == "flag":
                lines.append(f"{i}. 用旗 {s.ref} → {s.to_scene}{p(s.arrive)}  ~{s.cost_s:.0f}s")
            elif s.kind == "charm":
                lines.append(f"{i}. 用飞行符 → {s.to_scene}{p(s.arrive)}  ~{s.cost_s:.0f}s")
            else:
                via = f" [{s.via}]" if s.via else ""
                lines.append(f"{i}. {s.scene}{p(s.at)} --{s.kind}{via}--> {s.to_scene}{p(s.arrive)}"
                             f"  ~{s.cost_s:.0f}s  可信度{s.conf}")
        lines.append(f"合计约 {self.total_s:.0f} 秒")
        return "\n".join(lines)


class Planner:
    def __init__(self, world: World, flag_remaining: dict[str, int | None] | None = None,
                 allow_flags: bool = True, allow_charm: bool | None = None):
        self.world = world
        self.eco = world.economy
        self.flag_remaining = flag_remaining or {}
        self.allow_flags = allow_flags
        self.allow_charm = self.eco.charm_enabled if allow_charm is None else allow_charm

    # ── 代价 ──
    def walk_s(self, scene: str, a: Point | None, b: Point | None) -> float:
        if a is not None and a == b:
            return 0.0
        if a is None or b is None:
            return self.eco.unknown_leg_s
        return math.dist(a, b) / self.eco.speed_of(scene)

    def flag_usable(self, flag: str) -> bool:
        left = self.flag_remaining.get(flag)
        return left is None or left >= self.eco.flag_min_remaining

    # ── 搜索 ──
    def plan(self, start_scene: str, start_xy: Point | None, goal_scene: str,
             goal_xy: Point | None = None) -> Plan:
        w = self.world
        start = w.resolve(start_scene)
        goal = w.resolve(goal_scene)
        if start is None:
            raise NoRoute(f"不认识的起点场景 {start_scene}")
        if goal is None:
            raise NoRoute(f"不认识的目标场景 {goal_scene}")

        GOAL = ("<goal>", None, "<goal>")
        src = (start, start_xy, "<start>")
        dist = {src: 0.0}
        prev: dict[tuple, tuple[tuple, list[Step]]] = {}
        tie = itertools.count()
        heap = [(0.0, next(tie), src)]
        done = set()

        while heap:
            d, _, node = heapq.heappop(heap)
            if node in done:
                continue
            done.add(node)
            if node == GOAL:
                return self._rebuild(prev, GOAL, d)
            for nxt, steps in self._neighbors(node, goal, goal_xy):
                nd = d + sum(s.cost_s for s in steps)
                if nd < dist.get(nxt, math.inf):
                    dist[nxt] = nd
                    prev[nxt] = (node, steps)
                    heapq.heappush(heap, (nd, next(tie), nxt))
        raise NoRoute(f"从 {start} 到不了 {goal}（检查 links.yaml 是否缺边）")

    def _neighbors(self, node, goal: str, goal_xy: Point | None):
        scene, pos, _tag = node
        w, eco = self.world, self.eco

        if scene == goal:
            c = 0.0 if goal_xy is None else self.walk_s(scene, pos, goal_xy)
            steps = [] if c == 0 else [Step("walk", scene, scene, goal_xy, goal_xy, c)]
            yield ("<goal>", None, "<goal>"), steps

        for e in w.out_edges(scene):
            steps = []
            wc = self.walk_s(scene, pos, e.src_at)
            if wc > 0:
                steps.append(Step("walk", scene, scene, e.src_at, e.src_at, wc))
            tc = eco.overhead(e.type) + float(eco.conf_penalty_s.get(e.conf, 0))
            steps.append(Step(e.type, scene, e.dst, e.src_at, e.dst_at, tc, e.via, e.opt, e.id, e.conf))
            yield (e.dst, e.dst_at, e.dst_at and "" or e.id), steps

        here = w.scenes.get(scene)
        if here is None or here.indoor:
            return
        if self.allow_flags:
            fc = eco.overhead("flag_use") + eco.money_s(eco.flag_cny_per_use)
            for f in w.flags:
                if f.xy is None or not self.flag_usable(f.flag) or (f.city == scene and f.xy == pos):
                    continue
                yield (f.city, f.xy, ""), [Step("flag", scene, f.city, None, f.xy, fc, ref=f"{f.flag}#{f.idx}")]
        if self.allow_charm:
            cc = eco.overhead("charm_use") + eco.money_s(eco.charm_cny_each)
            for c in w.charm:
                if c.city == scene:
                    continue
                yield (c.city, c.arrive, c.arrive and "" or f"charm:{c.city}"), \
                    [Step("charm", scene, c.city, None, c.arrive, cc, ref=c.city)]

    @staticmethod
    def _rebuild(prev, goal_node, total: float) -> Plan:
        chunks = []
        node = goal_node
        while node in prev:
            node, steps = prev[node]
            chunks.append(steps)
        steps = [s for chunk in reversed(chunks) for s in chunk]
        return Plan(steps=steps, total_s=total)
