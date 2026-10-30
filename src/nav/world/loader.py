"""读 resources/nav/world/ 下的四个 yaml，组装成 World，并做数据检查。

只依赖标准库和 PyYAML，32 位 Python（大漠进程）里也能用。
"""
from __future__ import annotations

import collections
import os

import yaml

from common.config import resources_path
from nav.world.model import CharmDest, Economy, Edge, FlagPoint, Point, Scene, World

WORLD_DIR = resources_path("nav", "world")


class WorldDataError(ValueError):
    pass


def _point(v) -> Point | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(c, int) for c in v):
        return (v[0], v[1])
    if isinstance(v, (list, tuple)) and len(v) == 2 and all(c is None for c in v):
        return None
    raise WorldDataError(f"坐标格式应为 [x, y] 整数或 null，实际是 {v!r}")


def _read(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _scenes(raw: dict) -> dict[str, Scene]:
    out: dict[str, Scene] = {}
    for name, d in (raw.get("scenes") or {}).items():
        d = d or {}
        out[name] = Scene(
            name=name, kind=d.get("kind", "field"), size=_point(d.get("size")), tab=d.get("tab"),
            conf=d.get("conf", "C"), aliases=tuple(d.get("alias") or ()),
            enabled=d.get("enabled", True) is not False, note=d.get("note", "") or "")
    return out


def _edges(raw: dict) -> list[Edge]:
    out: list[Edge] = []
    for i, l in enumerate(raw.get("links") or []):
        if l.get("enabled", True) is False:
            continue
        common = dict(type=l["type"], via=l.get("via"), opt=l.get("opt"), conf=l.get("conf", "C"), ref=l.get("ref"))
        a_at, b_at = _point(l.get("a_at")), _point(l.get("b_at"))
        lid = l.get("id") or f"L{i}"
        out.append(Edge(id=lid, src=l["a"], dst=l["b"], src_at=a_at, dst_at=b_at, **common))
        if l.get("bidir"):
            out.append(Edge(id=lid + "r", src=l["b"], dst=l["a"], src_at=b_at, dst_at=a_at, **common))
    for i, p in enumerate(raw.get("posts") or []):
        if p.get("enabled", True) is False:
            continue
        for j, d in enumerate(p.get("dests") or []):
            out.append(Edge(id=f"P{i}.{j}", src=p["scene"], dst=d["to"], type="post",
                            src_at=_point(p.get("at")), dst_at=_point(d.get("arrive")),
                            via="驿站老板", opt=d.get("opt"), conf=p.get("conf", "C"), ref=d.get("ref") or p.get("ref")))
    return out


def _flags(raw: dict) -> tuple[list[FlagPoint], list[CharmDest]]:
    flags = [FlagPoint(flag=fname, city=f["city"], idx=int(p["idx"]), xy=_point(p.get("xy")), ui=_point(p.get("ui")))
             for fname, f in (raw.get("flags") or {}).items() for p in (f.get("points") or [])]
    charm_raw = (raw.get("charm") or {}).get("dests") or {}
    charm = [CharmDest(city=c, arrive=_point((d or {}).get("arrive")), ui=_point((d or {}).get("ui")))
             for c, d in charm_raw.items()]
    return flags, charm


def _economy(raw: dict) -> Economy:
    tv = raw.get("time_value") or {}
    items = raw.get("items") or {}
    walk = raw.get("walk") or {}
    plan = raw.get("planning") or {}
    dflt = raw.get("defaults") or {}

    per_hour = tv.get("point_card_cny_per_hour")
    time_value = float(per_hour) / 3600.0 if per_hour else None

    rate = items.get("liang_per_cny")

    def to_cny(amount, unit="两"):
        if not amount:
            return None
        if unit == "元":
            return float(amount)
        return float(amount) / float(rate) if rate else None

    flag_cny = None
    if items.get("flag_price") and items.get("flag_uses_per_item"):
        whole = to_cny(items["flag_price"], items.get("flag_price_unit", "两"))
        flag_cny = whole / float(items["flag_uses_per_item"]) if whole is not None else None
    charm_cny = to_cny(items.get("charm_price"))

    overhead = dict(dflt.get("overhead_s") or {})
    overhead.update({k: v for k, v in (raw.get("overhead_s") or {}).items() if v is not None})
    return Economy(
        time_value_cny_per_s=time_value, flag_cny_per_use=flag_cny, charm_cny_each=charm_cny,
        speed=float(walk.get("speed_units_per_s") or dflt.get("speed_units_per_s") or 10.0),
        speed_override={k: float(v) for k, v in (walk.get("per_scene_override") or {}).items()},
        unknown_leg_s=float(dflt.get("unknown_leg_s") or 20.0),
        overhead_s=overhead,
        conf_penalty_s=dict(dflt.get("conf_penalty_s") or {}),
        flag_min_remaining=int(plan.get("flag_min_remaining", 1)),
        flag_warn_remaining=int(plan.get("flag_warn_remaining", 21)),
        charm_enabled=plan.get("charm_enabled", True) is not False)


def build_world(scenes_raw: dict, links_raw: dict, flags_raw: dict, economy_raw: dict) -> World:
    scenes = _scenes(scenes_raw)
    alias_map: dict[str, str] = {}
    for s in scenes.values():
        for a in s.aliases:
            if a in scenes:
                continue  # 别名与另一场景同名时，以场景本身为准（如 花果山）
            alias_map.setdefault(a, s.name)
    def live(name):  # 不存在的场景留给 check() 报错；禁用场景的边直接去掉
        return name not in scenes or scenes[name].enabled
    edges = [e for e in _edges(links_raw) if live(e.src) and live(e.dst)]
    flags, charm = _flags(flags_raw)
    world = World(scenes=scenes, edges=edges, flags=flags, charm=charm,
                  economy=_economy(economy_raw), alias_map=alias_map)
    return world


def load_world(world_dir: str = WORLD_DIR) -> World:
    def r(name):
        return _read(os.path.join(world_dir, name))
    world = build_world(r("scenes.yaml"), r("links.yaml"), r("flags.yaml"), r("economy.yaml"))
    problems = check(world)
    if problems:
        raise WorldDataError("世界数据有错：\n  " + "\n  ".join(problems))
    return world


def check(world: World) -> list[str]:
    """硬错误：引用了不存在的场景、坐标越界、旗/飞行符城市不存在。"""
    out: list[str] = []
    for e in world.edges:
        for s in (e.src, e.dst):
            if s not in world.scenes:
                out.append(f"连接 {e.id} 引用了不存在的场景 {s}")
        for s, p in ((e.src, e.src_at), (e.dst, e.dst_at)):
            sc = world.scenes.get(s)
            if p and sc and sc.size and not (0 <= p[0] <= sc.size[0] and 0 <= p[1] <= sc.size[1]):
                out.append(f"连接 {e.id} 的坐标 {p} 超出 {s} 的范围 {sc.size}")
    for f in world.flags:
        sc = world.scenes.get(f.city)
        if sc is None:
            out.append(f"旗 {f.flag} 的城市 {f.city} 不存在")
        elif f.xy and sc.size and not (0 <= f.xy[0] <= sc.size[0] and 0 <= f.xy[1] <= sc.size[1]):
            out.append(f"旗 {f.flag}#{f.idx} 坐标 {f.xy} 超出 {f.city} 的范围 {sc.size}")
    for c in world.charm:
        if c.city not in world.scenes:
            out.append(f"飞行符目的地 {c.city} 不存在")
    return out


def reachability(world: World, hub: str = "长安城") -> dict[str, list[str]]:
    """只看场景连接（不算旗和飞行符）：从 hub 到不了的、回不到 hub 的启用场景。"""
    fwd, back = collections.defaultdict(set), collections.defaultdict(set)
    for e in world.edges:
        fwd[e.src].add(e.dst)
        back[e.dst].add(e.src)

    def bfs(g):
        seen, stack = {hub}, [hub]
        while stack:
            for v in g[stack.pop()]:
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        return seen

    f, b = bfs(fwd), bfs(back)
    active = [n for n, s in world.scenes.items() if s.enabled]
    return {"unreachable": [n for n in active if n not in f], "no_return": [n for n in active if n not in b]}
