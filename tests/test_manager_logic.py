"""多机管理的纯逻辑单元测试（布局计算、状态文字、机器清单与加密保存、PNG 编码）。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from common.pngio import encode_png  # noqa: E402
from manager import layout, status  # noqa: E402
from manager.machines import Machine, Store, protect_text, unprotect_text  # noqa: E402
from manager.session import CONNECTING, ERROR, IDLE, LOST, ONLINE, next_backoff, sample_signature  # noqa: E402


class GridLayout(unittest.TestCase):
    def test_single_machine_fills_area_with_4_3(self):
        g = layout.compute_grid(1, 1200, 700, 50, 10)
        self.assertEqual((g.cols, g.rows), (1, 1))
        self.assertEqual(g.img_w * 3, g.img_h * 4)
        self.assertLessEqual(g.img_h + 50, 700)

    def test_five_machines_auto_picks_best_columns(self):
        g = layout.compute_grid(5, 1600, 900, 50, 10)
        self.assertEqual(g.cols * g.rows >= 5, True)
        # 不会出现比"每台一行"更差的选择
        worst = layout.compute_grid(5, 1600, 900, 50, 10, "cols:1")
        self.assertGreaterEqual(g.img_w, worst.img_w)

    def test_fixed_columns_respected_and_capped(self):
        g = layout.compute_grid(5, 1600, 900, 50, 10, "cols:2")
        self.assertEqual(g.cols, 2)
        self.assertEqual(g.rows, 3)
        self.assertEqual(layout.compute_grid(2, 1600, 900, 50, 10, "cols:4").cols, 2)

    def test_tiny_area_never_negative(self):
        g = layout.compute_grid(6, 100, 100, 50, 10)
        self.assertGreaterEqual(g.img_w, 0)
        self.assertGreaterEqual(g.img_h, 0)

    def test_tile_size_capped(self):
        g = layout.compute_grid(1, 5000, 4000, 50, 10)
        self.assertLessEqual(g.img_w, layout.MAX_TILE_W)

    def test_focus_size_keeps_aspect(self):
        w, h = layout.focus_image_size(1000, 500)
        self.assertEqual((w, h), (666, 499))
        self.assertLessEqual(h, 500)


class StatusText(unittest.TestCase):
    def test_online_ok(self):
        lvl, short, line = status.describe(ONLINE, "在线", 4.8, 0.3, 1.0)
        self.assertEqual((lvl, short), ("ok", "在线"))
        self.assertIn("4.8", line)

    def test_online_static_warns(self):
        self.assertEqual(status.describe(ONLINE, "在线", 2, 0.5, 30)[:2], ("warn", "画面不动"))

    def test_online_late_frame_warns(self):
        self.assertEqual(status.describe(ONLINE, "在线", 0, 12, 0)[:2], ("warn", "画面延迟"))

    def test_waiting_first_frame(self):
        self.assertEqual(status.describe(ONLINE, "在线", 0, None, 0)[0], "warn")

    def test_other_states(self):
        self.assertEqual(status.describe(IDLE, "", 0, None, 0)[0], "idle")
        self.assertEqual(status.describe(CONNECTING, "连接中…", 0, None, 0)[0], "busy")
        self.assertEqual(status.describe(LOST, "断了", 0, None, 0)[0], "bad")
        self.assertEqual(status.describe(ERROR, "密码错", 0, None, 0)[0], "bad")

    def test_action_labels(self):
        self.assertEqual(status.action_label(IDLE), "连接")
        self.assertEqual(status.action_label(ONLINE), "断开")
        self.assertEqual(status.action_label(ERROR), "重试")

    def test_summary(self):
        self.assertEqual(status.summarize([])[0], "idle")
        self.assertEqual(status.summarize([ONLINE, ONLINE])[0], "ok")
        self.assertEqual(status.summarize([ONLINE, LOST])[0], "bad")
        self.assertEqual(status.summarize([ONLINE, CONNECTING])[0], "warn")
        self.assertIn("在线 1", status.summarize([ONLINE, IDLE])[1])


class MachineStore(unittest.TestCase):
    def test_dpapi_roundtrip(self):
        blob = protect_text("p@ss 密码")
        self.assertNotIn("p@ss", blob)
        self.assertEqual(unprotect_text(blob), "p@ss 密码")

    def test_password_saved_only_when_remembered(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.json")
            s = Store(path)
            s.add(Machine("副机1", "100.1.1.1", 5900, password="secret-A", remember=True))
            s.add(Machine("副机2", "100.1.1.2", 5901, password="secret-B", remember=False))
            s.settings["layout"] = "cols:3"
            s.save()
            raw = open(path, encoding="utf-8").read()
            self.assertNotIn("secret-A", raw)
            self.assertNotIn("secret-B", raw)
            s2 = Store(path)
            s2.load()
            self.assertEqual([m.name for m in s2.machines], ["副机1", "副机2"])
            self.assertEqual(s2.machines[0].password, "secret-A")
            self.assertTrue(s2.machines[0].remember)
            self.assertEqual(s2.machines[1].password, "")
            self.assertEqual(s2.machines[1].port, 5901)
            self.assertEqual(s2.settings["layout"], "cols:3")

    def test_missing_or_broken_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(os.path.join(d, "none.json"))
            s.load()
            self.assertEqual(s.machines, [])
            bad = os.path.join(d, "bad.json")
            open(bad, "w").write("{not json")
            s = Store(bad)
            s.load()
            self.assertEqual(s.machines, [])

    def test_default_names_skip_used(self):
        s = Store("x")
        s.add(Machine("设备1", "h"))
        s.add(Machine("设备3", "h"))
        self.assertEqual(s.next_default_name(), "设备2")

    def test_remove(self):
        s = Store("x")
        m = Machine("a", "h")
        s.add(m)
        s.remove(m.id)
        self.assertEqual(s.machines, [])


class SessionHelpers(unittest.TestCase):
    def test_backoff_grows_then_caps(self):
        seq = [next_backoff(i) for i in range(8)]
        self.assertEqual(seq[0], 3.0)
        self.assertEqual(seq[-1], 30.0)
        self.assertEqual(sorted(seq), seq)

    def test_signature_detects_change(self):
        a = bytearray(b"\x00" * 3000)
        b = bytearray(a)
        self.assertEqual(sample_signature(a), sample_signature(b))
        b[97 * 5] = 255
        self.assertNotEqual(sample_signature(a), sample_signature(b))


class Png(unittest.TestCase):
    def test_png_header_and_size(self):
        data = encode_png(bytes([255, 0, 0] * 4), 2, 2)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"IHDR", data)
        self.assertTrue(data.endswith(b"IEND\xaeB`\x82"))


if __name__ == "__main__":
    unittest.main()
