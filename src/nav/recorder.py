"""跟玩录制：用户正常玩，本工具只读查看器画面，自动记下"步骤"（换图、同图坐标跳变），留证据给整理。

只调用 PrintWindow 抓图，不发任何鼠标键盘消息，也不改查看器窗口的大小和位置。
需要主控机 64 位 Python + numpy + Pillow（不需要大漠）。

每一帧读左上角的场景名和坐标（nav.hud_read），交给 nav.rec_steps.StepTracker 判断步骤。
每个步骤保存：跳变前 15 秒里画面明显不同的几帧（打开旗/飞行符界面、点选的那一刻）和到达后的一帧。

用法（在 src 目录下，主控机上开着副机的查看器）：
    python -m nav.recorder                       只有一个查看器时自动选它
    python -m nav.recorder --viewer 100.104      按标题里的地址选查看器
录制中在本终端输入文字回车：含"飞行符/旗/传送圈/NPC/驿站/进门"的记成动作，其他记成备注；
输入 q 回车或按 Ctrl+C 结束。
"""
from __future__ import annotations

import argparse
import collections
import ctypes
import json
import os
import platform
import queue
import struct
import sys
import threading
import time
import zlib

import numpy as np

from common import wincap, winapi
from common.config import runtime_dir
from control.viewer_link import find_viewers
from nav import hud_read, rec_steps

G_W, G_H = 800, 600
HUD_ROI = (0, 0, 220, 110)          # 整块左上角存进 hud.bin，事后可以重新识别
EVIDENCE_WINDOW_S = 15.0
EVIDENCE_MAX = 6
EVIDENCE_MIN_DIFF = 0.04
FORMAT_VERSION = 2


class Session:
    def __init__(self, root: str):
        self.root = root
        for sub in ("frames", "periodic", "names"):
            os.makedirs(os.path.join(root, sub), exist_ok=True)
        self._events = open(os.path.join(root, "events.jsonl"), "a", encoding="utf-8")
        self._ticks = open(os.path.join(root, "ticks.jsonl"), "a", encoding="utf-8")
        self._hud = open(os.path.join(root, "hud.bin"), "ab")

    def event(self, **kv) -> None:
        self._events.write(json.dumps(kv, ensure_ascii=False) + "\n")
        self._events.flush()

    def tick(self, **kv) -> None:
        self._ticks.write(json.dumps(kv, ensure_ascii=False) + "\n")

    def hud(self, t: float, region: np.ndarray) -> None:
        """左上角区域原始像素：记录头 (t, w, h, 压缩长度) + zlib(RGB)。"""
        h, w = region.shape[:2]
        data = zlib.compress(np.ascontiguousarray(region).tobytes(), 6)
        self._hud.write(struct.pack("<dIII", t, w, h, len(data)) + data)

    def save_frame(self, sub: str, name: str, frame: np.ndarray, quality: int | None = None) -> str:
        from PIL import Image
        rel = os.path.join(sub, name)
        img = Image.fromarray(frame)
        if quality:
            img.save(os.path.join(self.root, rel), quality=quality)
        else:
            img.save(os.path.join(self.root, rel))
        return rel.replace("\\", "/")

    def close(self) -> None:
        for f in (self._events, self._ticks, self._hud):
            f.close()


def _dpi_of(hwnd: int) -> int | None:
    try:
        return int(ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(hwnd)))
    except (AttributeError, OSError):
        return None


def _capture_note(viewer, cap: dict) -> str:
    """把“PrintWindow 失败”翻译成用户能处理的原因：最小化/隐藏优先。"""
    try:
        if winapi.is_iconic(viewer.top):
            return "查看器已最小化"
        if not winapi.is_visible(viewer.top) or not winapi.is_visible(viewer.child):
            return "查看器已隐藏"
    except Exception:  # noqa: BLE001 窗口刚失效时查询会抛错，交给通用提示
        pass
    return cap.get("note") or ("纯色画面" if cap.get("blank") else "截图失败")


def _stdin_lines(q: "queue.Queue") -> None:
    for line in sys.stdin:
        q.put(line.rstrip("\r\n"))
    q.put("q")


def pick_viewer(keyword: str | None):
    vs = find_viewers(keyword)
    if len(vs) == 1:
        return vs[0]
    if not vs:
        print("没找到 TightVNC 查看器窗口。请先在主控机上打开副机画面。")
    else:
        print("找到多个查看器，请用 --viewer 指定标题里的地址：")
        for v in vs:
            print("   ", winapi.title(v.top))
    return None


def small_gray(frame: np.ndarray, step: int = 8) -> np.ndarray:
    return frame[::step, ::step].astype(np.float32).mean(axis=2)


def pick_evidence(ring, t_change: float, window_s: float = EVIDENCE_WINDOW_S,
                  max_n: int = EVIDENCE_MAX, min_diff: float = EVIDENCE_MIN_DIFF) -> list:
    """从环形缓冲里挑跳变前的证据帧：时间窗内，和上一张已选帧差别明显的才留；
    跳变前最后一帧一定留（通常就是点选那一刻）；最多 max_n 张，优先留靠后的。

    ring 的元素是 (t, small_gray, frame)。返回同样元素的列表，按时间顺序。
    """
    cands = [r for r in ring if t_change - window_s <= r[0] < t_change]
    if not cands:
        return []
    kept = [cands[0]]
    for r in cands[1:]:
        if float(np.abs(r[1] - kept[-1][1]).mean()) / 255.0 >= min_diff:
            kept.append(r)
    if kept[-1] is not cands[-1]:
        kept.append(cands[-1])
    return kept[-max_n:]


def _parse_control(item) -> dict | None:
    """队列里的东西 → {"kind": "stop"|"action"|"note"|"name", ...}。字符串来自命令行。"""
    if isinstance(item, dict):
        return item
    line = str(item).strip()
    if not line:
        return None
    if line.lower() == "q":
        return {"kind": "stop"}
    act = rec_steps.action_from_note(line)
    if act:
        return {"kind": "action", "action": act, "text": line}
    return {"kind": "note", "text": line}


def run(viewer, out_dir: str, fps: float = 2.0, periodic_s: float = 10.0,
        *, control_queue: "queue.Queue | None" = None,
        stop_event: "threading.Event | None" = None, book: "hud_read.NameBook | None" = None,
        on_message=None, on_event=None, on_frame=None, on_hud=None) -> str:
    """执行一次只读录制。

    control_queue 里放字符串（命令行备注）或 dict：
        {"kind": "action", "action": "flag", "text": 可选}   用户点了动作按钮
        {"kind": "note", "text": "..."}                       普通备注
        {"kind": "name", "name": "普陀山"}                     给当前未收录的地图起名
        {"kind": "how", "no": 3, "action": "portal"}          改第 3 步的方式
        {"kind": "stop"}
    on_event 收到写进 events.jsonl 的每条记录；on_hud 每帧收到 {t, scene, known, xy}。
    """
    def emit(message: str) -> None:
        print(message)
        if on_message is not None:
            try:
                on_message(message)
            except Exception:
                pass

    def emit_event(**kv) -> None:
        session.event(**kv)
        if on_event is not None:
            try:
                on_event(dict(kv))
            except Exception:
                pass

    book = book or hud_read.default_book()
    session = Session(out_dir)
    src_w, src_h = winapi.client_size(viewer.child)
    info = {
        "format": FORMAT_VERSION,
        "start": time.strftime("%Y-%m-%d %H:%M:%S"), "viewer_title": winapi.title(viewer.top),
        "viewer_client": [src_w, src_h], "viewer_dpi": _dpi_of(viewer.child),
        "python": platform.python_version(), "bits": struct.calcsize("P") * 8, "host": platform.node(),
        "fps": fps, "rois": {"hud": HUD_ROI, "name": hud_read.NAME_ROI, "coord": hud_read.COORD_ROI},
        "known_names": sorted({n for n, _ in book.entries}),
        "note": "只读录制；events.jsonl 里 type=step 是自动识别的步骤，hud.bin 为左上角原始像素",
    }
    # 第一层/第二层的唯一标准是 800x600；绑定阶段已经拒绝其他尺寸。
    if (src_w, src_h) != (G_W, G_H):
        info["warning"] = (f"查看器客户区 {src_w}x{src_h}，标准必须为 {G_W}x{G_H}；"
                           "请使用标准查看器重新连接后再录")
        emit("警告：" + info["warning"])
    with open(os.path.join(out_dir, "session.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    controls: "queue.Queue" = control_queue or queue.Queue()
    if control_queue is None:
        threading.Thread(target=_stdin_lines, args=(controls,), daemon=True).start()

    tracker = rec_steps.StepTracker()
    ring: collections.deque = collections.deque(maxlen=max(4, int(fps * (EVIDENCE_WINDOW_S + 5))))
    saved_unknown: set[str] = set()
    n_actions = 0
    last_reading: hud_read.HudReading | None = None
    last_periodic = -1e9
    last_hud = None
    last_preview = -1e9
    last_bad_emit = -1e9
    bad = False
    unread_since: float | None = None
    period = 1.0 / fps
    t0 = time.monotonic()

    def handle_step(step: dict, frame: np.ndarray) -> None:
        no = step["no"]
        ev = pick_evidence(ring, step["t"])
        before = [session.save_frame("frames", f"step{no:03d}_before{i}.jpg", f, quality=92)
                  for i, (_, _, f) in enumerate(ev)]
        after = session.save_frame("frames", f"step{no:03d}_after.jpg", frame, quality=92)
        rec = {k: (list(v) if isinstance(v, tuple) else v) for k, v in step.items()}
        emit_event(type="step", **rec, before=before, after=after,
                   before_t=[round(t, 2) for t, _, _ in ev])
        kind = "换图" if step["kind"] == "scene" else "同图跳变"
        emit(f"  步骤{no} [{kind}] {step['from']}{tuple(step['from_xy'] or ())} → "
             f"{step['to']}{tuple(step['to_xy'] or ())}")

    emit(f"开始录制 → {out_dir}")
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise KeyboardInterrupt
            tick_start = time.monotonic()
            now = round(tick_start - t0, 2)
            while True:
                try:
                    ctl = _parse_control(controls.get_nowait())
                except queue.Empty:
                    break
                if ctl is None:
                    continue
                kind = ctl.get("kind")
                if kind == "stop":
                    raise KeyboardInterrupt
                if kind == "action":
                    n_actions += 1
                    shot = None
                    if ring:
                        shot = session.save_frame("frames", f"action{n_actions:03d}.jpg", ring[-1][2], quality=92)
                    emit_event(t=now, type="action", action=ctl["action"], text=ctl.get("text") or "", frame=shot)
                    emit(f"已记动作：{rec_steps.ACTIONS.get(ctl['action'], ctl['action'])}  @{now:.1f}s")
                elif kind == "how":
                    emit_event(t=now, type="how", no=int(ctl["no"]), action=ctl["action"])
                    emit(f"已改步骤{ctl['no']}的方式：{rec_steps.ACTIONS.get(ctl['action'], ctl['action'])}")
                elif kind == "note":
                    emit_event(t=now, type="note", text=ctl["text"])
                    emit(f"已记备注：{ctl['text']}  @{now:.1f}s")
                elif kind == "name":
                    r = last_reading
                    if r is None or r.mask is None or r.known:
                        emit("当前地图已经认得，或者现在读不到地图名，没有起名。")
                        continue
                    old, new = r.name, " ".join(str(ctl["name"]).split())
                    try:
                        path = book.add(new, r.mask, persist=True)
                    except (ValueError, OSError) as exc:
                        emit(f"起名失败：{exc}")
                        continue
                    tracker.rename(old, new)
                    emit_event(t=now, type="name", unknown=old, name=new, saved=path)
                    emit(f"已记住地图名：{old} = {new}")

            cap = wincap.capture_scaled(viewer.child, G_W, G_H)
            t = round(time.monotonic() - t0, 2)
            if not cap.get("ok") or cap.get("blank"):
                bad = True
                note = _capture_note(viewer, cap)
                session.tick(t=t, ok=False, note=note)
                if t - last_bad_emit >= 5.0:
                    last_bad_emit = t
                    if note in ("查看器已最小化", "查看器已隐藏"):
                        emit(f"{note}，画面暂停；还原后会自动继续…")
                    else:
                        emit(f"暂时抓不到画面（{note}），正在重试…")
                time.sleep(period)
                continue
            if bad:
                bad = False
                emit("画面已恢复，继续采集。")
            frame = np.frombuffer(cap["rgb"], dtype=np.uint8).reshape(G_H, G_W, 3)
            if on_frame is not None and t - last_preview >= 0.5:
                last_preview = t
                try:
                    on_frame(frame, t)
                except Exception:
                    pass

            x1, y1, x2, y2 = HUD_ROI
            hud = frame[y1:y2, x1:x2]
            if last_hud is None or not np.array_equal(hud, last_hud):
                session.hud(t, hud)
                last_hud = hud.copy()

            r = hud_read.read_hud(frame, book)
            last_reading = r
            session.tick(t=t, scene=r.name, known=r.known, xy=list(r.xy) if r.xy else None)
            if on_hud is not None:
                try:
                    on_hud({"t": t, "scene": r.name, "known": r.known, "xy": r.xy})
                except Exception:
                    pass
            if r.name is None or r.xy is None:
                if unread_since is None:
                    unread_since = t
                elif t - unread_since >= 20 and t - last_bad_emit >= 20:
                    last_bad_emit = t
                    emit("已经 20 秒读不到左上角的地图名或坐标：小地图面板是否被关掉或被挡住？")
            else:
                unread_since = None
            if r.name and not r.known and r.name not in saved_unknown:
                saved_unknown.add(r.name)
                from PIL import Image
                crop = Image.fromarray(np.ascontiguousarray(frame[0:40, 0:120])).resize((360, 120), Image.NEAREST)
                rel = f"names/{r.name}.png"
                crop.save(os.path.join(out_dir, rel))
                Image.fromarray((r.mask * 255).astype(np.uint8)).save(
                    os.path.join(out_dir, f"names/{r.name}_mask.png"))
                emit_event(t=t, type="unknown_scene", name=r.name, crop=rel)
                emit(f"遇到图库里没有的地图，暂记为“{r.name}”，可以在采集器里给它起名。")

            ring.append((t, small_gray(frame), frame))
            for step in tracker.feed(t, r.name, r.xy, r.known):
                handle_step(step, frame)

            if t - last_periodic >= periodic_s:
                session.save_frame("periodic", f"{t:08.1f}.jpg", frame, quality=80)
                last_periodic = t

            spent = time.monotonic() - tick_start
            if spent < period:
                time.sleep(period - spent)
    except KeyboardInterrupt:
        pass
    finally:
        if ring:
            for step in tracker.flush():
                handle_step(step, ring[-1][2])
        emit_event(t=round(time.monotonic() - t0, 2), type="end", steps=len(tracker.steps))
        session.close()
    emit(f"录制结束，共 {len(tracker.steps)} 个步骤。整理：python -m nav.recorder_report \"{out_dir}\"")
    return out_dir


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="跟玩录制（只读）")
    ap.add_argument("--viewer", help="查看器标题里包含的文字（如副机地址）")
    ap.add_argument("--fps", type=float, default=2.0)
    ap.add_argument("--periodic", type=float, default=10.0, help="每隔多少秒存一张全画面 JPG")
    ap.add_argument("--out", help="输出目录，默认 runtime/recorded/<时间>")
    ns = ap.parse_args(argv)
    winapi.set_dpi_aware()
    viewer = pick_viewer(ns.viewer)
    if viewer is None:
        return 2
    out = ns.out or os.path.join(runtime_dir(), "recorded", time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out, exist_ok=True)
    run(viewer, out, ns.fps, ns.periodic)
    return 0


if __name__ == "__main__":
    sys.exit(main())
