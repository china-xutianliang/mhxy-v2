"""副机共享助手的单元测试（不需要 TightVNC，也不需要真实窗口）。运行：python -m unittest discover -s tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from game_share import build_command  # noqa: E402


class GameShareTests(unittest.TestCase):
    def test_command_format(self):
        cmd = build_command("tvn.exe", (100, 50, 800, 600), None)
        self.assertEqual(cmd, ["tvn.exe", "-controlservice", "-sharerect", "800x600+100+50"])

    def test_negative_coordinates_follow_doc_format(self):
        cmd = build_command("tvn.exe", (-1280, 0, 800, 600), "p.txt")
        self.assertEqual(cmd[3], "800x600+-1280+0")
        self.assertEqual(cmd[-2:], ["-passfile", "p.txt"])


class ApplyRectTest(unittest.TestCase):
    def setUp(self):
        import game_share as gs
        self.gs = gs
        self._o = (gs.run_command, gs.kill_stuck_control, gs.service_installed)
        gs._last_good.clear()
        gs.kill_stuck_control = lambda: None
        gs.service_installed = lambda: True
        self.calls = []

    def tearDown(self):
        self.gs.run_command, self.gs.kill_stuck_control, self.gs.service_installed = self._o
        self.gs._last_good.clear()

    def test_no_passfile_first_then_passfile(self):
        def run(cmd, timeout=0):
            self.calls.append(cmd)
            return (0, "") if "-passfile" in cmd else (-1, "timeout")
        self.gs.run_command = run
        rc, note, mode = self.gs.apply_rect("t.exe", (1, 2, 800, 600), "p.txt")
        self.assertEqual((rc, mode), (0, "service"))
        self.assertEqual(len(self.calls), 2)
        self.calls.clear()
        self.gs.apply_rect("t.exe", (1, 2, 800, 600), "p.txt")
        self.assertEqual(len(self.calls), 1)  # 记住了上次可用的组合

    def test_no_password_server_succeeds_first_try(self):
        self.gs.run_command = lambda cmd, timeout=0: self.calls.append(cmd) or (0, "")
        self.gs.apply_rect("t.exe", (1, 2, 800, 600), "p.txt")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("-passfile", self.calls[0])

    def test_all_fail_lists_every_attempt(self):
        self.gs.run_command = lambda cmd, timeout=0: (-1, "x")
        rc, note, mode = self.gs.apply_rect("t.exe", (1, 2, 800, 600), "p.txt")
        self.assertEqual((rc, mode), (-1, ""))
        self.assertEqual(note.count("退出码"), 4)


class FindGameWindowTest(unittest.TestCase):
    """游戏画面窗口 WSGAME 是顶层 MMXYMainFrame 的子窗口（用户用大漠实测的窗口树）。"""

    def setUp(self):
        import game_share as gs
        self.gs = gs
        w = gs.winapi
        self._orig = {n: getattr(w, n) for n in ("enum_top_windows", "enum_children", "class_name", "title",
                                                 "client_size")}
        self._vis = w.user32.IsWindowVisible
        self._exe = gs._exe_of
        info = {  # hwnd: (class, title, client)
            1: ("Chrome_WidgetWin_1", "梦幻西游攻略 - Chrome", (1280, 700)),
            10: ("MHXYMainFrame", "梦幻西游 ONLINE - x", (816, 639)),
            11: ("SysTabControl32", "Tab", (800, 20)),
            12: ("WSGAME", "梦幻西游 ONLINE - x", (800, 600)),
        }
        self.info = info
        w.enum_top_windows = lambda: [1, 10]
        w.enum_children = lambda p: [11, 12] if p == 10 else []
        w.class_name = lambda h: info[h][0]
        w.title = lambda h: info[h][1]
        w.client_size = lambda h: info[h][2]
        w.user32.IsWindowVisible = lambda h: True
        gs._exe_of = lambda h: "mhmain.exe" if h in (10, 11, 12) else "chrome.exe"

    def tearDown(self):
        w = self.gs.winapi
        for n, f in self._orig.items():
            setattr(w, n, f)
        w.user32.IsWindowVisible = self._vis
        self.gs._exe_of = self._exe

    def test_finds_child_not_frame_or_browser(self):
        self.assertEqual(self.gs.find_game_window("WSGAME", "mhmain.exe"), 12)

    def test_nothing_when_game_closed(self):
        self.info.pop(10)
        self.gs.winapi.enum_top_windows = lambda: [1]
        self.assertEqual(self.gs.find_game_window("WSGAME", "mhmain.exe"), 0)


if __name__ == "__main__":
    unittest.main()
