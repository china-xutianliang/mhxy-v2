"""命令行试算路线（只读数据，不碰游戏）。

用法（在 src 目录下）：
    python -m nav.route 长安城 300 100 普陀山
    python -m nav.route 长安城 - - 傲来国 100 50 --no-flag
    python -m nav.route --check          只做数据检查（引用、越界、可达、可回）
"""
from __future__ import annotations

import argparse
import sys

from nav.planner import NoRoute, Planner
from nav.world.loader import WorldDataError, load_world, reachability


def _xy(a, b):
    return None if a in (None, "-") or b in (None, "-") else (int(a), int(b))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="第三层路线试算")
    ap.add_argument("args", nargs="*", help="起点场景 x y 目标场景 [x y]")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--no-flag", action="store_true")
    ap.add_argument("--no-charm", action="store_true")
    ns = ap.parse_args(argv)
    try:
        world = load_world()
    except WorldDataError as e:
        print(e)
        return 2
    if ns.check:
        r = reachability(world)
        print(f"场景 {len(world.scenes)} 个，有向边 {len(world.edges)} 条，旗点 {len(world.flags)} 个")
        print("从长安到不了：", r["unreachable"] or "无")
        print("回不到长安：", r["no_return"] or "无")
        known = sum(1 for e in world.edges if e.src_at and e.dst_at)
        print(f"坐标已填全的边：{known}/{len(world.edges)}；已填坐标的旗点："
              f"{sum(1 for f in world.flags if f.xy)}/{len(world.flags)}")
        return 0 if not (r["unreachable"] or r["no_return"]) else 1
    a = ns.args
    if len(a) not in (4, 6):
        ap.print_help()
        return 2
    planner = Planner(world, allow_flags=not ns.no_flag, allow_charm=False if ns.no_charm else None)
    try:
        plan = planner.plan(a[0], _xy(a[1], a[2]), a[3], _xy(*a[4:6]) if len(a) == 6 else None)
    except NoRoute as e:
        print(e)
        return 1
    print(plan.describe())
    return 0


if __name__ == "__main__":
    sys.exit(main())
