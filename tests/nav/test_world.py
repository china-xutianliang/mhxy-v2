import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

from nav.world.loader import build_world, check, load_world, reachability  # noqa: E402


class RealDataTest(unittest.TestCase):
    """真实 yaml：引用、越界、可达、可回。改 yaml 后跑这个就知道有没有写错。"""

    @classmethod
    def setUpClass(cls):
        cls.world = load_world()

    def test_no_hard_errors(self):
        self.assertEqual(check(self.world), [])

    def test_every_scene_reachable_and_returnable(self):
        r = reachability(self.world)
        self.assertEqual(r["unreachable"], [], "从长安到不了")
        self.assertEqual(r["no_return"], [], "回不到长安")

    def test_flags_5x7_and_charm_5(self):
        by_flag = {}
        for f in self.world.flags:
            by_flag.setdefault(f.flag, []).append(f.idx)
        self.assertEqual(len(by_flag), 5)
        self.assertTrue(all(sorted(v) == list(range(1, 8)) for v in by_flag.values()))
        self.assertEqual(sorted(c.city for c in self.world.charm), sorted(["建邺城", "西梁女国", "傲来国", "朱紫国", "宝象国"]))

    def test_alias(self):
        self.assertEqual(self.world.resolve("西牛贺洲"), "长寿郊外")
        self.assertEqual(self.world.resolve("花果山"), "花果山")  # 同名时场景本身优先
        self.assertEqual(self.world.resolve("幻境花果山"), "花果山(门派)")


class BuildTest(unittest.TestCase):
    def _w(self, links, scenes=None):
        scenes = scenes or {"A": {"kind": "city", "size": [100, 100]}, "B": {"kind": "field"}}
        return build_world({"scenes": scenes}, {"links": links}, {}, {})

    def test_bidir_expands_with_swapped_points(self):
        w = self._w([{"a": "A", "b": "B", "type": "portal", "bidir": True, "a_at": [1, 2], "b_at": [3, 4]}])
        fwd = [e for e in w.edges if e.src == "A"][0]
        back = [e for e in w.edges if e.src == "B"][0]
        self.assertEqual((fwd.src_at, fwd.dst_at), ((1, 2), (3, 4)))
        self.assertEqual((back.src_at, back.dst_at), ((3, 4), (1, 2)))

    def test_disabled_link_dropped(self):
        w = self._w([{"a": "A", "b": "B", "type": "npc", "bidir": False, "enabled": False}])
        self.assertEqual(w.edges, [])

    def test_out_of_range_and_unknown_scene_reported(self):
        w = self._w([{"a": "A", "b": "Z", "type": "portal", "bidir": False, "a_at": [500, 5]}])
        msgs = " ".join(check(w))
        self.assertIn("不存在的场景 Z", msgs)
        self.assertIn("超出 A", msgs)

    def test_null_pair_is_unknown(self):
        w = self._w([{"a": "A", "b": "B", "type": "portal", "bidir": False, "a_at": [None, None]}])
        self.assertIsNone(w.edges[0].src_at)


if __name__ == "__main__":
    unittest.main()
