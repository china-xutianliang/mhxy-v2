import io
import json
import os
import queue
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

try:
    import numpy as np
    import PIL  # noqa: F401
    import yaml
except ImportError:  # 32 位环境没有 numpy：录制只在主控机 64 位 Python 里跑
    np = None


def background(seed=0):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 150, (600, 800, 3), dtype=np.uint8)  # 暗色杂点，不会被当成白字


def draw_coords(frame, x, y):
    from nav import hud_read
    cx = hud_read.COORD_ROI[0] + 1
    top = hud_read.COORD_ROI[1]
    for i, part in enumerate((f"X:{x}", f"Y:{y}")):
        if i:
            cx += 4
        for ch in part:
            g = hud_read.GLYPHS[ch]
            frame[top:top + 10, cx:cx + g.shape[1]][g] = 255
            cx += g.shape[1] + 1
    return frame


def draw_name(frame, mask):
    from nav import hud_read
    x, y = hud_read.NAME_ROI[0] + 1, hud_read.NAME_ROI[1] + 1
    frame[y:y + mask.shape[0], x:x + mask.shape[1]][mask] = 255
    return frame


def hud_frame(mask, xy, seed=0):
    f = background(seed)
    if mask is not None:
        draw_name(f, mask)
    if xy is not None:
        draw_coords(f, *xy)
    return f


def builtin(name):
    from nav import hud_read
    book = hud_read.NameBook.load(hud_read.builtin_dir())
    return next(m for n, m in book.entries if n == name)


def strange_mask():
    m = np.zeros((14, 45), bool)
    m[1:13, 2:4] = m[1:3, 2:40] = m[6:8, 10:30] = m[11:13, 20:43] = True
    return m


@unittest.skipIf(np is None, "需要 numpy + Pillow + PyYAML")
class HudReadTest(unittest.TestCase):
    def test_coords_round_trip(self):
        from nav import hud_read
        for xy in ((541, 3), (0, 0), (361, 243), (20, 108), (1234, 987)):
            self.assertEqual(hud_read.read_coords(hud_frame(None, xy)), xy)
        self.assertIsNone(hud_read.read_coords(background()))

    def test_name_book_known_unknown_and_add(self):
        from nav import hud_read
        with tempfile.TemporaryDirectory() as tmp:
            book = hud_read.NameBook.load(hud_read.builtin_dir(), user_dir=tmp)
            r = hud_read.read_hud(hud_frame(builtin("长安城"), (10, 20)), book)
            self.assertEqual((r.name, r.known, r.xy), ("长安城", True, (10, 20)))
            odd = hud_frame(strange_mask(), (1, 2))
            r = hud_read.read_hud(odd, book)
            self.assertEqual((r.name, r.known), ("未收录地图1", False))
            self.assertEqual(hud_read.read_hud(odd, book).name, "未收录地图1")  # 同一字形同一编号
            book.add("普陀山", r.mask)
            again = hud_read.NameBook.load(hud_read.builtin_dir(), user_dir=tmp)  # 重启后仍认得
            self.assertEqual(hud_read.read_hud(odd, again).name, "普陀山")
            self.assertIsNone(hud_read.read_hud(background(), book).name)

    def test_cursor_over_name_still_matches(self):
        from nav import hud_read
        book = hud_read.NameBook.load(hud_read.builtin_dir())
        f = hud_frame(builtin("长安城"), (10, 20))
        x, y = hud_read.NAME_ROI[0] + 1, hud_read.NAME_ROI[1] + 1
        f[y + 3:y + 6, x + 20:x + 23] = 255          # 鼠标叠在字上
        f[y + 2:y + 4, x + 50:x + 52] = 255          # 鼠标尖露在名字右边
        self.assertEqual(hud_read.read_hud(f, book).name, "长安城")

    def test_longer_name_is_not_taken_for_its_prefix(self):
        from nav import hud_read
        book = hud_read.NameBook.load(hud_read.builtin_dir())
        ca = builtin("长安城")
        longer = np.zeros((ca.shape[0], ca.shape[1] + 16), bool)
        longer[:, :ca.shape[1]] = ca
        longer[:, ca.shape[1] + 1:] = ca[:, :15]     # 多出一个字
        name, known = book.identify(longer)
        self.assertFalse(known, name)


@unittest.skipIf(np is None, "需要 numpy + Pillow + PyYAML")
class StepTrackerTest(unittest.TestCase):
    def feed(self, tracker, seq):
        out = []
        for t, scene, xy in seq:
            out += tracker.feed(t, scene, xy)
        return out

    def test_walk_jump_and_scene_change(self):
        from nav import rec_steps
        tr = rec_steps.StepTracker()
        walk = [(i * 0.5, "长安城", (100 + 3 * i, 100)) for i in range(10)]
        jump = [(5.0, "长安城", (500, 10)), (5.5, "长安城", (500, 10))]
        black = [(6.0, None, None), (6.5, None, None)]
        new = [(7.0, "江南野外", None), (7.5, "江南野外", None), (8.0, "江南野外", (20, 108))]
        steps = self.feed(tr, walk + jump + black + new)
        self.assertEqual([s["kind"] for s in steps], ["jump", "scene"])
        self.assertEqual((steps[0]["from_xy"], steps[0]["to_xy"]), ((127, 100), (500, 10)))
        self.assertEqual((steps[1]["from"], steps[1]["from_xy"], steps[1]["to"], steps[1]["to_xy"]),
                         ("长安城", (500, 10), "江南野外", (20, 108)))

    def test_single_glitch_frame_is_ignored(self):
        from nav import rec_steps
        tr = rec_steps.StepTracker()
        seq = [(0.0, "长安城", (100, 100)), (0.5, "朱紫国", (900, 900)), (1.0, "长安城", (101, 100)),
               (1.5, "长安城", (900, 900)), (2.0, "长安城", (102, 100))]
        self.assertEqual(self.feed(tr, seq), [])

    def test_unknown_name_while_walking_is_not_a_scene_change(self):
        from nav import rec_steps
        tr = rec_steps.StepTracker()
        seq = [(0.0, "小西天", (55, 207), True), (0.5, "未收录地图4", (53, 208), False),
               (1.0, "未收录地图4", (50, 209), False), (1.5, "小西天", (48, 209), True)]
        self.assertEqual([s for t, n, xy, k in seq for s in tr.feed(t, n, xy, k)], [])
        far = [(2.0, "未收录地图5", (170, 12), False), (2.5, "未收录地图5", (170, 12), False)]
        steps = [s for t, n, xy, k in far for s in tr.feed(t, n, xy, k)]
        self.assertEqual([(s["from"], s["to"]) for s in steps], [("小西天", "未收录地图5")])

    def test_rename_unknown(self):
        from nav import rec_steps
        tr = rec_steps.StepTracker()
        self.feed(tr, [(0, "长安城", (1, 1)), (1, "未收录地图1", None), (2, "未收录地图1", (5, 5))])
        tr.rename("未收录地图1", "普陀山")
        self.assertEqual(tr.steps[0]["to"], "普陀山")
        self.assertEqual(self.feed(tr, [(3, "普陀山", (6, 6)), (4, "普陀山", (7, 6))]), [])

    def test_assign_actions(self):
        from nav import rec_steps
        steps = [{"no": 1, "t": 10.0, "kind": "jump"}, {"no": 2, "t": 30.0, "kind": "scene"},
                 {"no": 3, "t": 60.0, "kind": "scene"}]
        actions = [{"t": 8.0, "action": "flag"}, {"t": 33.0, "action": "portal"}]
        rec_steps.assign_actions(steps, actions)
        self.assertEqual([(s["how"], s["how_src"]) for s in steps],
                         [("flag", "用户点选"), ("portal", "用户点选"), ("walk_or_portal", "推测")])


@unittest.skipIf(np is None, "需要 numpy + Pillow + PyYAML")
class EvidenceTest(unittest.TestCase):
    def test_pick_evidence_keeps_distinct_frames_and_last(self):
        from nav import recorder
        a, b = np.zeros((4, 4)), np.full((4, 4), 200.0)
        ring = [(t, a, f"a{t}") for t in (1, 2, 3)] + [(4, b, "b4"), (5, b, "b5")]
        picked = [r[2] for r in recorder.pick_evidence(ring, 6.0)]
        self.assertEqual(picked, ["a1", "b4", "b5"])
        self.assertEqual(recorder.pick_evidence(ring, 100.0), [])


@unittest.skipIf(np is None, "需要 numpy + Pillow + PyYAML")
class CaptureNoteTest(unittest.TestCase):
    def test_minimized_and_hidden_are_named(self):
        from nav import recorder
        viewer = mock.Mock(top=1, child=2)
        cap = {"ok": False, "note": "PrintWindow 失败"}
        with mock.patch.object(recorder.winapi, "is_iconic", return_value=True):
            self.assertEqual(recorder._capture_note(viewer, cap), "查看器已最小化")
        with mock.patch.object(recorder.winapi, "is_iconic", return_value=False), \
                mock.patch.object(recorder.winapi, "is_visible", side_effect=lambda h: h == 1):
            self.assertEqual(recorder._capture_note(viewer, cap), "查看器已隐藏")
        with mock.patch.object(recorder.winapi, "is_iconic", return_value=False), \
                mock.patch.object(recorder.winapi, "is_visible", return_value=True):
            self.assertEqual(recorder._capture_note(viewer, cap), "PrintWindow 失败")
            self.assertEqual(recorder._capture_note(viewer, {"ok": False, "note": "", "blank": True}),
                             "纯色画面")


def scripted_frames():
    """长安城走路 → 用旗瞬移 → 过图黑屏 → 江南野外 → 进一张图库里没有的地图。"""
    ca, jn, odd = builtin("长安城"), builtin("江南野外"), strange_mask()
    seq = [hud_frame(ca, (100 + 2 * i, 100), i) for i in range(6)]
    seq += [hud_frame(ca, (541, 3), 50)] * 4
    seq += [np.zeros((600, 800, 3), np.uint8)] * 2
    seq += [hud_frame(jn, (20, 108), 60)] * 4
    seq += [hud_frame(odd, (5, 5), 70)] * 6
    return seq


@unittest.skipIf(np is None, "需要 numpy + Pillow + PyYAML")
class RecorderEndToEndTest(unittest.TestCase):
    """假查看器喂合成画面，按钮动作、起名都走 control_queue；全程不碰真实窗口。"""

    def run_session(self, tmp):
        from nav import hud_read, recorder
        frames = scripted_frames()
        controls: queue.Queue = queue.Queue()
        state = {"i": 0}

        def fake_capture(_hwnd, w, h):
            i = state["i"]
            state["i"] += 1
            if i == 3:
                controls.put({"kind": "action", "action": "flag"})
            if i == 12:
                controls.put("走传送圈去江南")          # 命令行风格：含"传送圈"→ 动作
            if i == 17:
                controls.put({"kind": "note", "text": "和土地对话"})
            if i == len(frames) - 2:
                controls.put({"kind": "name", "name": "普陀山"})
            if i >= len(frames):
                raise KeyboardInterrupt
            return {"ok": True, "w": w, "h": h, "rgb": frames[i].tobytes(), "blank": False}

        book = hud_read.NameBook.load(hud_read.builtin_dir(), user_dir=os.path.join(tmp, "names_user"))
        huds = []
        out = os.path.join(tmp, "rec")
        os.makedirs(out)
        with mock.patch.object(recorder.wincap, "capture_scaled", fake_capture), \
                mock.patch.object(recorder.winapi, "client_size", lambda h: (800, 600)), \
                mock.patch.object(recorder.winapi, "title", lambda h: "fake::5900 - TightVNC"), \
                mock.patch.object(recorder.time, "sleep", lambda s: None), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            recorder.run(viewer=mock.Mock(top=1, child=2), out_dir=out, fps=2.0,
                         control_queue=controls, book=book, on_hud=huds.append)
        return out, huds

    def test_session_and_report(self):
        from nav import recorder_report
        with tempfile.TemporaryDirectory() as tmp:
            out, huds = self.run_session(tmp)
            with open(os.path.join(out, "session.json"), encoding="utf-8") as f:
                session = json.load(f)
            self.assertEqual(session["format"], 2)
            self.assertNotIn("warning", session)
            self.assertIn({"t": mock.ANY, "scene": "长安城", "known": True, "xy": (100, 100)}, huds)
            events = recorder_report.read_jsonl(os.path.join(out, "events.jsonl"))
            steps = [e for e in events if e["type"] == "step"]
            self.assertEqual([(s["kind"], s["from"], s["to"]) for s in steps],
                             [("jump", "长安城", "长安城"), ("scene", "长安城", "江南野外"),
                              ("scene", "江南野外", "未收录地图1")])
            self.assertEqual(steps[0]["to_xy"], [541, 3])
            self.assertTrue(steps[0]["before"] and os.path.exists(os.path.join(out, steps[0]["after"])))
            acts = [e for e in events if e["type"] == "action"]
            self.assertEqual([a["action"] for a in acts], ["flag", "portal"])
            self.assertTrue(os.path.exists(os.path.join(out, acts[0]["frame"])))
            self.assertTrue(any(e["type"] == "name" and e["name"] == "普陀山" for e in events))
            self.assertEqual(events[-1]["type"], "end")
            self.assertTrue(os.listdir(os.path.join(tmp, "names_user")))
            data = recorder_report.load(out)
            self.assertEqual((data["steps"][2]["how"], data["steps"][2]["how_src"]), ("earth", "按备注推测"))

            with open(os.path.join(out, "events.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": None, "type": "how", "no": 3, "action": "npc"}) + "\n")
            report = recorder_report.build(out)
            self.assertTrue(os.path.exists(report))
            self.assertTrue(os.path.exists(os.path.join(out, "report", "step_001.png")))
            with open(os.path.join(out, "report", "draft_links.yaml"), encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("&id", text)
            draft = yaml.safe_load(text)
            self.assertEqual([(s["how"], s["how_src"]) for s in draft["steps"]],
                             [("flag", "用户点选"), ("portal", "用户点选"), ("npc", "用户改")])
            self.assertEqual(draft["steps"][2]["to"], "普陀山")
            self.assertEqual(draft["landings"][0]["at"], [541, 3])
            self.assertEqual([(l["a"], l["b"], l["type"]) for l in draft["links"]],
                             [("长安城", "江南野外", "portal"), ("江南野外", "普陀山", "npc")])
            self.assertEqual(draft["links"][0]["a_at"], [541, 3])

    def test_old_format_replays_hud_bin(self):
        from nav import recorder, recorder_report
        with tempfile.TemporaryDirectory() as tmp:
            s = recorder.Session(tmp)
            for i, f in enumerate(scripted_frames()[:16]):
                s.hud(i * 0.5, f[0:110, 0:220])
            s.event(t=2.0, type="note", text="旗子：")
            s.close()
            with open(os.path.join(tmp, "session.json"), "w", encoding="utf-8") as f:
                json.dump({"start": "x", "fps": 2.0, "rois": {"hud": [0, 0, 220, 110]}}, f)
            data = recorder_report.load(tmp)
            self.assertEqual([(st["kind"], st["to"], st["how"]) for st in data["steps"]],
                             [("jump", "长安城", "flag"), ("scene", "江南野外", "walk_or_portal")])
            self.assertTrue(os.path.exists(recorder_report.build(tmp)))


if __name__ == "__main__":
    unittest.main()
