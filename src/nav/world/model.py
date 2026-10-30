"""第三层世界模型数据类。坐标一律是游戏场景坐标 (x, y)，未知用 None。"""
from __future__ import annotations

from dataclasses import dataclass, field

Point = tuple[int, int]

# 室内类场景不能开 Tab 小地图，也默认不能用旗/飞行符（待真机确认）
INDOOR_KINDS = frozenset({"interior", "sect_hall"})


@dataclass(frozen=True)
class Scene:
    name: str
    kind: str
    size: Point | None = None
    tab: bool | None = None
    conf: str = "C"
    aliases: tuple[str, ...] = ()
    enabled: bool = True
    note: str = ""

    @property
    def indoor(self) -> bool:
        return self.kind in INDOOR_KINDS


@dataclass(frozen=True)
class Edge:
    """一条有向的过场景方式。src_at = 在 src 里触发的位置；dst_at = 到达 dst 后的落脚位置。"""
    id: str
    src: str
    dst: str
    type: str
    src_at: Point | None = None
    dst_at: Point | None = None
    via: str | None = None
    opt: str | None = None
    conf: str = "C"
    ref: str | None = None


@dataclass(frozen=True)
class FlagPoint:
    flag: str
    city: str
    idx: int
    xy: Point | None = None
    ui: Point | None = None


@dataclass(frozen=True)
class CharmDest:
    city: str
    arrive: Point | None = None
    ui: Point | None = None


@dataclass
class Economy:
    """成本参数。未知的价格按 0 折算，只比较耗时。"""
    time_value_cny_per_s: float | None = None
    flag_cny_per_use: float | None = None
    charm_cny_each: float | None = None
    speed: float = 10.0
    speed_override: dict[str, float] = field(default_factory=dict)
    unknown_leg_s: float = 20.0
    overhead_s: dict[str, float] = field(default_factory=dict)
    conf_penalty_s: dict[str, float] = field(default_factory=dict)
    flag_min_remaining: int = 1
    flag_warn_remaining: int = 21
    charm_enabled: bool = True

    def speed_of(self, scene: str) -> float:
        return self.speed_override.get(scene, self.speed)

    def money_s(self, cny: float | None) -> float:
        if not cny or not self.time_value_cny_per_s:
            return 0.0
        return cny / self.time_value_cny_per_s

    def overhead(self, kind: str) -> float:
        return float(self.overhead_s.get(kind, self.overhead_s.get("default", 5.0)))


@dataclass
class World:
    scenes: dict[str, Scene]
    edges: list[Edge]
    flags: list[FlagPoint]
    charm: list[CharmDest]
    economy: Economy
    alias_map: dict[str, str] = field(default_factory=dict)

    def resolve(self, name: str) -> str | None:
        """OCR 读到的场景名 → 规范名。"""
        if name in self.scenes:
            return name
        return self.alias_map.get(name)

    def out_edges(self, scene: str) -> list[Edge]:
        return [e for e in self.edges if e.src == scene]
