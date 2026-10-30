import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

from nav.planner import NoRoute, Planner  # noqa: E402
from nav.world.loader import build_world, load_world  # noqa: E402

ECO = {"walk": {"speed_units_per_s": 10},
       "defaults": {"unknown_leg_s": 20, "overhead_s": {"flag_use": 8, "charm_use": 7, "portal": 3, "npc": 6},
                    "conf_penalty_s": {"A": 0, "B": 0, "C": 10}}}


def world(flag_xy=(400, 100), eco=ECO, links=None):
    scenes = {"城": {"kind": "city", "size": [500, 300]}, "野": {"kind": "field", "size": [300, 300]},
              "屋": {"kind": "interior"}, "港": {"kind": "city", "size": [200, 200]}}
    links = links if links is not None else [
        {"a": "城", "b": "野", "type": "portal", "bidir": True, "a_at": [490, 10], "b_at": [5, 5], "conf": "A"},
        {"a": "城", "b": "屋", "type": "enter", "bidir": True, "a_at": [20, 20], "b_at": [5, 5], "conf": "A"},
        {"a": "野", "b": "港", "type": "portal", "bidir": True, "a_at": [290, 290], "b_at": [10, 10], "conf": "A"},
    ]
    flags = {"flags": {"红": {"city": "城", "points": [{"idx": 1, "xy": list(flag_xy)}]}},
             "charm": {"dests": {"港": {"arrive": [100, 100]}}}}
    return build_world({"scenes": scenes}, {"links": links}, flags, eco)


class FlagDecisionTest(unittest.TestCase):
    def test_far_target_uses_flag(self):
        plan = Planner(world()).plan("城", (10, 100), "城", (390, 100))
        self.assertEqual([s.kind for s in plan.steps], ["flag", "walk"])
        self.assertAlmostEqual(plan.total_s, 8 + 1, places=3)

    def test_near_target_walks(self):
        plan = Planner(world()).plan("城", (10, 100), "城", (50, 100))
        self.assertEqual([s.kind for s in plan.steps], ["walk"])

    def test_breakeven_is_consequence_not_threshold(self):
        # 起点在旗点上方 d 处，目标在旗点旁：走路 d/10 秒，用旗 8 秒 -> 盈亏点 d*=80
        for d, expect in ((70, "walk"), (90, "flag")):
            plan = Planner(world(flag_xy=(250, 150))).plan("城", (250 - d, 150), "城", (250, 150))
            self.assertEqual(plan.steps[0].kind, expect, f"d={d}")

    def test_no_flag_when_remaining_low(self):
        plan = Planner(world(), flag_remaining={"红": 0}).plan("城", (10, 100), "城", (390, 100))
        self.assertNotIn("flag", [s.kind for s in plan.steps])

    def test_expensive_flag_loses(self):
        eco = dict(ECO, time_value={"point_card_cny_per_hour": 3.6},  # 0.001 元/秒
                   items={"flag_price": 14, "flag_price_unit": "元", "flag_uses_per_item": 140})  # 0.1 元/次 = 100 秒
        plan = Planner(world(eco=eco)).plan("城", (10, 100), "城", (390, 100))
        self.assertEqual([s.kind for s in plan.steps], ["walk"])

    def test_no_flag_or_charm_indoors(self):
        plan = Planner(world()).plan("屋", (5, 5), "港", (100, 100))
        self.assertEqual(plan.steps[0].kind if plan.steps[0].kind != "walk" else plan.steps[1].kind, "enter")


class RouteTest(unittest.TestCase):
    def test_charm_vs_walk(self):
        with_charm = Planner(world()).plan("城", (490, 10), "港", (100, 100))
        self.assertEqual(with_charm.steps[0].kind, "charm")
        no_charm = Planner(world(), allow_charm=False, allow_flags=False).plan("城", (490, 10), "港", (100, 100))
        self.assertEqual([s.to_scene for s in no_charm.steps if s.kind == "portal"], ["野", "港"])

    def test_low_confidence_penalised(self):
        links = [{"a": "城", "b": "港", "type": "portal", "bidir": False, "a_at": [10, 10], "b_at": [10, 10], "conf": "C"},
                 {"a": "城", "b": "港", "type": "npc", "bidir": False, "a_at": [10, 10], "b_at": [10, 10], "conf": "A"}]
        plan = Planner(world(links=links), allow_flags=False, allow_charm=False).plan("城", (10, 10), "港", (10, 10))
        self.assertEqual([s.kind for s in plan.steps], ["npc"])  # npc 6s < portal 3s + C 罚 10s

    def test_unknown_coordinates_use_default_leg(self):
        links = [{"a": "城", "b": "港", "type": "portal", "bidir": False, "conf": "A"}]
        plan = Planner(world(links=links), allow_flags=False, allow_charm=False).plan("城", (10, 10), "港")
        self.assertAlmostEqual(plan.total_s, 20 + 3)

    def test_no_route(self):
        with self.assertRaises(NoRoute):
            Planner(world(links=[]), allow_flags=False, allow_charm=False).plan("城", (1, 1), "港")


class RealWorldSmokeTest(unittest.TestCase):
    """真实数据（坐标大多为空）只验证能规划出途经关键场景的路线，不验证耗时。"""

    @classmethod
    def setUpClass(cls):
        cls.p = Planner(load_world(), allow_flags=False, allow_charm=False)

    def scenes_of(self, start, goal):
        plan = self.p.plan(start, None, goal)
        return [s.to_scene for s in plan.steps if s.kind != "walk"]

    def test_putuo_via_guojing(self):
        self.assertEqual(self.scenes_of("长安城", "普陀山"), ["大唐国境", "普陀山"])

    def test_every_sect_reachable_from_changan(self):
        for name, s in self.p.world.scenes.items():
            if s.kind == "sect":
                self.assertTrue(self.scenes_of("长安城", name), name)


if __name__ == "__main__":
    unittest.main()
