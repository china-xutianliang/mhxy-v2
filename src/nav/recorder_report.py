"""整理一次录制：每个"步骤"（换图、同图坐标跳变）出一张对照图，并生成已经填好的连接草稿。

用法（在 src 目录下）：
    python -m nav.recorder_report <录制目录>

产出（都在录制目录的 report/ 下）：
    report.md          步骤总表 + 每步：从哪到哪、坐标、方式、截图、附近备注
    step_NNN.png       一张对照图：跳变前 15 秒里画面明显不同的几帧（旗/飞行符界面、点选那一刻）+ 到达后
    draft_links.yaml   每步一条，from/to/坐标/方式都已自动填好；另附 links.yaml 格式的建议条目和旗/飞行符落点
    stats.txt          帧数、帧间隔、走路速度

新格式（session.json 里 format>=2）直接用录制时识别的步骤；旧格式用 hud.bin 里存的左上角像素重新识别。
"""
from __future__ import annotations

import json
import os
import struct
import sys
import zlib

import numpy as np

from nav import hud_read, rec_steps

G_W, G_H = 800, 600
THUMB = (320, 240)
LINK_TYPES = {"portal": "portal", "npc": "npc", "enter": "enter", "post": "post", "earth": "earth",
              "walk_or_portal": "portal"}
LANDING_HOWS = ("flag", "charm", "flag_or_charm")


def read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # 录制被强行中断时最后一行可能是半截
    return out


def read_hud(path: str):
    """逐条读 hud.bin：返回 (t, ndarray)。"""
    if not os.path.exists(path):
        return
    head = struct.calcsize("<dIII")
    with open(path, "rb") as f:
        while True:
            h = f.read(head)
            if len(h) < head:
                return
            t, w, hh, n = struct.unpack("<dIII", h)
            data = f.read(n)
            if len(data) < n:
                return
            yield t, np.frombuffer(zlib.decompress(data), dtype=np.uint8).reshape(hh, w, 3)


def _place(hud: np.ndarray, origin) -> np.ndarray:
    """把存下的左上角区域放回 800x600 画面里的原位，hud_read 按画面坐标读。"""
    x, y = int(origin[0]), int(origin[1])
    frame = np.zeros((G_H, G_W, 3), np.uint8)
    h, w = hud.shape[:2]
    frame[y:y + h, x:x + w] = hud[:G_H - y, :G_W - x]
    return frame


def replay(root: str, info: dict, book: "hud_read.NameBook | None" = None, step_s: float = 0.5,
           max_repeat: int = 40):
    """用 hud.bin 重新识别步骤。hud.bin 只在左上角变化时才记一条，所以按 step_s 把上一条补齐，
    和录制时每帧都喂给 StepTracker 的效果一样。返回 (steps, track)。"""
    book = book or hud_read.default_book()
    origin = (info.get("rois") or {}).get("hud", (0, 0, 220, 110))[:2]
    tracker = rec_steps.StepTracker()
    track: list[tuple[float, str, tuple[int, int]]] = []
    last = None  # (t, name, xy, known)
    for t, hud in read_hud(os.path.join(root, "hud.bin")):
        if last is not None:
            tt, n = last[0] + step_s, 0
            while tt < t and n < max_repeat:
                tracker.feed(round(tt, 2), last[1], last[2], last[3])
                tt += step_s
                n += 1
        r = hud_read.read_hud(_place(hud, origin), book)
        tracker.feed(t, r.name, r.xy, r.known)
        if r.name and r.xy:
            track.append((t, r.name, r.xy))
        last = (t, r.name, r.xy, r.known)
    tracker.flush()
    return tracker.steps, track


def _old_frame_pool(root: str, events: list[dict]) -> list[tuple[float, str]]:
    """旧格式没有按步骤存的证据帧：把旧事件的前/后帧和定时截图按大致时间排起来备用。"""
    pool = []
    for e in events:
        if e.get("type") not in ("scene", "cut"):
            continue
        before = e.get("before") or []
        for i, rel in enumerate(before):
            pool.append((e["t"] - 0.5 * (len(before) - i), rel))
        tag = "scene" if e["type"] == "scene" else "cut"
        for i in range(3):
            pool.append((e["t"] + 0.5 * (i + 1), f"frames/{e['no']:04d}_{tag}_after{i}.png"))
    pdir = os.path.join(root, "periodic")
    if os.path.isdir(pdir):
        for fn in os.listdir(pdir):
            try:
                pool.append((float(os.path.splitext(fn)[0]), f"periodic/{fn}"))
            except ValueError:
                pass
    return sorted((t, rel) for t, rel in pool if os.path.exists(os.path.join(root, rel)))


def _recheck_unknown(root: str, steps: list[dict], renames: dict, book) -> dict:
    """录制时没认出的地图名（多半是鼠标挡住了字，或当时图库里还没有），用存下的字形和现在的图库再认一遍。"""
    unknown = {s[k] for s in steps for k in ("from", "to")
               if str(s.get(k) or "").startswith("未收录地图") and s.get(k) not in renames}
    found = {}
    for name in sorted(unknown):
        path = os.path.join(root, "names", f"{name}_mask.png")
        if not os.path.exists(path):
            continue
        from PIL import Image
        book = book or hud_read.default_book()
        mask = np.asarray(Image.open(path).convert("L")) > 127
        real = book.match(mask)
        if real:
            found[name] = real
    return found


def _drop_self_changes(steps: list[dict]) -> list[dict]:
    """改名后"从 A 换到 A"的步骤：坐标是走路能到的就删掉（是误读），跳得远就改成同图跳变。"""
    kept = []
    for s in steps:
        if s["kind"] == "scene" and s["from"] == s["to"]:
            a, b = s.get("from_xy"), s.get("to_xy")
            dt = max(0.0, (s.get("to_t") or s["t"]) - (s.get("from_t") or s["t"]))
            tr = rec_steps.StepTracker()
            if not (a and b and rec_steps.dist(a, b) > tr.jump_min + tr.speed_max * dt):
                continue
            s["kind"] = "jump"
        kept.append(s)
    return kept


def load(root: str, book: "hud_read.NameBook | None" = None) -> dict:
    """读一次录制，返回整理好的 {info, steps, notes, track, source}。"""
    with open(os.path.join(root, "session.json"), encoding="utf-8") as f:
        info = json.load(f)
    events = read_jsonl(os.path.join(root, "events.jsonl"))
    notes = [e for e in events if e.get("type") == "note"]
    if int(info.get("format", 1)) >= 2:
        source = "录制时识别"
        steps = [dict(e) for e in events if e.get("type") == "step"]
        actions = [e for e in events if e.get("type") == "action"]
        track = [(x["t"], x["scene"], tuple(x["xy"])) for x in read_jsonl(os.path.join(root, "ticks.jsonl"))
                 if x.get("scene") and x.get("xy")]
    else:
        source = "旧录像，用 hud.bin 重新识别"
        steps, track = replay(root, info, book)
        actions = []
        for n in notes:
            act = rec_steps.action_from_note(n.get("text", ""))
            if act:
                actions.append({"t": n["t"], "action": act, "text": n["text"]})
        notes = [n for n in notes if not rec_steps.action_from_note(n.get("text", ""))]
        pool = _old_frame_pool(root, events)
        for s in steps:
            before = [(t, rel) for t, rel in pool if s["t"] - 15 <= t < s["t"]][-4:]
            s["before"] = [rel for _, rel in before]
            s["before_t"] = [t for t, _ in before]
            arrive = s.get("to_t") or s["t"]
            after = [rel for t, rel in pool if t >= arrive]
            s["after"] = after[0] if after else None

    renames = {e["unknown"]: e["name"] for e in events if e.get("type") == "name" and e.get("unknown")}
    renames.update(_recheck_unknown(root, steps, renames, book))
    for s in steps:
        for k in ("from", "to"):
            if s.get(k) in renames:
                s[k] = renames[s[k]]
    track = [(t, renames.get(n, n), xy) for t, n, xy in track]
    for s in steps:
        for k in ("from_xy", "to_xy"):
            if s.get(k) is not None:
                s[k] = [int(v) for v in s[k]]
    steps = _drop_self_changes(steps)

    rec_steps.assign_actions(steps, actions)
    prev_t = -1e9
    for i, s in enumerate(steps):
        nxt = steps[i + 1]["t"] if i + 1 < len(steps) else 1e18
        if s["how_src"] == "推测":
            hits = [rec_steps.action_from_note(n.get("text", "")) for n in notes
                    if prev_t < n["t"] <= min(s["t"] + 15, nxt)]
            hits = [h for h in hits if h]
            if hits:
                s["how"], s["how_src"] = hits[-1], "按备注推测"
        prev_t = s["t"]
    for e in events:
        if e.get("type") == "how":
            for s in steps:
                if s["no"] == e.get("no"):
                    s["how"], s["how_src"] = e["action"], "用户改"
    prev_arrive = None
    for s in steps:
        start = s.get("from_t")
        s["stay_s"] = round(start - prev_arrive, 1) if start is not None and prev_arrive is not None else None
        prev_arrive = s.get("to_t") or s["t"]
    return {"info": info, "steps": steps, "notes": notes, "track": track, "source": source,
            "walk_speed": rec_steps.walk_speed(track)}


def _font(size: int):
    from PIL import ImageFont
    for path in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
        try:
            return ImageFont.truetype(path, size), True
        except OSError:
            continue
    return ImageFont.load_default(), False


def _sheet(root: str, step: dict, out: str) -> bool:
    """跳变前的几帧 + 到达后一帧，4 列一行，每张下面标时间。"""
    from PIL import Image, ImageDraw
    tiles = []
    bt = step.get("before_t") or []
    for i, rel in enumerate(step.get("before") or []):
        label = f"{bt[i] - step['t']:+.1f}s" if i < len(bt) else "之前"
        tiles.append((rel, label))
    if step.get("how_frame"):
        tiles.append((step["how_frame"], "点按钮时"))
    if step.get("after"):
        tiles.append((step["after"], "到达"))
    imgs = []
    for rel, label in tiles:
        path = os.path.join(root, rel)
        if os.path.exists(path):
            imgs.append((Image.open(path).convert("RGB").resize(THUMB), label))
    if not imgs:
        return False
    fnt, cjk = _font(18)
    cols = min(4, len(imgs))
    rows = (len(imgs) + cols - 1) // cols
    cell_w, cell_h = THUMB[0] + 8, THUMB[1] + 30
    sheet = Image.new("RGB", (cols * cell_w + 8, rows * cell_h + 8), (40, 40, 44))
    draw = ImageDraw.Draw(sheet)
    for i, (img, label) in enumerate(imgs):
        x, y = 8 + (i % cols) * cell_w, 8 + (i // cols) * cell_h
        sheet.paste(img, (x, y))
        if not cjk:
            label = {"到达": "after", "之前": "before", "点按钮时": "button"}.get(label, label)
        color = (120, 220, 140) if label in ("到达", "after") else (230, 230, 235)
        draw.text((x + 2, y + THUMB[1] + 3), label, fill=color, font=fnt)
    sheet.save(out)
    return True


def _xy(v) -> str:
    return f"({v[0]},{v[1]})" if v else "(?)"


def build(root: str, book: "hud_read.NameBook | None" = None) -> str:
    data = load(root, book)
    info, steps, notes = data["info"], data["steps"], data["notes"]
    out_dir = os.path.join(root, "report")
    os.makedirs(out_dir, exist_ok=True)

    md = [f"# 录制整理 {info.get('start', '')}", "",
          f"- 查看器：{info.get('viewer_title')}，客户区 {info.get('viewer_client')}，DPI {info.get('viewer_dpi')}",
          f"- 步骤来源：{data['source']}；共 {len(steps)} 步"
          f"（换图 {sum(s['kind'] == 'scene' for s in steps)}，同图跳变 {sum(s['kind'] == 'jump' for s in steps)}）",
          f"- 走路速度：{data['walk_speed'] or '数据不够'} 坐标/秒", ""]
    if info.get("warning"):
        md += [f"> 警告：{info['warning']}", ""]
    md += ["方式后面写“推测”的，是录制时没点动作按钮，按规律猜的，请看图确认。", "",
           "| # | 时间 | 从 | 到 | 方式 | 之前停留 |", "|---|---|---|---|---|---|"]
    for s in steps:
        how = rec_steps.HOW_LABEL.get(s["how"], s["how"])
        md.append(f"| {s['no']} | {s['t']:.1f}s | {s['from']}{_xy(s.get('from_xy'))} | "
                  f"{s['to']}{_xy(s.get('to_xy'))} | {how}（{s['how_src']}） | "
                  f"{'' if s['stay_s'] is None else str(s['stay_s']) + 's'} |")
    md.append("")

    drafts, links, landings = [], [], []
    prev_t = -1e9
    for i, s in enumerate(steps):
        name = f"step_{s['no']:03d}.png"
        has_img = _sheet(root, s, os.path.join(out_dir, name))
        nxt = steps[i + 1]["t"] if i + 1 < len(steps) else 1e18
        near = [n["text"] for n in notes if prev_t < n["t"] <= min(s["t"] + 15, nxt)]
        prev_t = s["t"]
        if s.get("how_note"):
            near.insert(0, s["how_note"])
        kind = "换图" if s["kind"] == "scene" else "同图跳变"
        how = rec_steps.HOW_LABEL.get(s["how"], s["how"])
        md += [f"## 步骤 {s['no']} · {kind} @ {s['t']:.1f}s", "",
               f"- {s['from']} {_xy(s.get('from_xy'))} → {s['to']} {_xy(s.get('to_xy'))}",
               f"- 方式：{how}（{s['how_src']}）",
               f"- 备注：{'；'.join(near) if near else '无'}", ""]
        if has_img:
            md += [f"![]({name})", ""]
        drafts.append({"no": s["no"], "t": s["t"], "kind": kind, "from": s["from"], "from_at": s.get("from_xy"),
                       "to": s["to"], "to_at": s.get("to_xy"), "how": s["how"], "how_src": s["how_src"],
                       "stay_s": s["stay_s"], "notes": near or None,
                       "image": f"report/{name}" if has_img else None})
        if s["how"] in LANDING_HOWS:
            landings.append({"how": s["how"], "scene": s["to"], "at": s.get("to_xy"),
                             "from": s["from"], "from_at": s.get("from_xy"), "step": s["no"]})
        elif s["kind"] == "scene" and s["how"] in LINK_TYPES:
            link = {"a": s["from"], "b": s["to"], "type": LINK_TYPES[s["how"]], "bidir": False,
                    "a_at": s.get("from_xy"), "b_at": s.get("to_xy"), "step": s["no"]}
            if s["how_src"] in ("推测", "按备注推测"):
                link["conf"] = "C"
                link["note"] = "方式是推测的，请看图确认"
            links.append(link)

    import yaml

    class _NoAlias(yaml.SafeDumper):
        def ignore_aliases(self, _data):  # 同一个坐标列表出现两次时，不要写成 &id001/*id001
            return True

    with open(os.path.join(out_dir, "draft_links.yaml"), "w", encoding="utf-8") as f:
        f.write("# 自动整理的草稿：坐标来自左上角读数，方式来自录制时点的按钮（how_src=推测 的请看图确认）。\n"
                "# how: charm 飞行符 / flag 旗 / portal 传送圈出口 / npc NPC传送 / enter 进门 / post 驿站车夫 / earth 土地\n"
                "#      flag_or_charm、walk_or_portal = 没点按钮，按规律猜的\n"
                "# links 段是 links.yaml 格式的建议条目；landings 段是旗/飞行符落点。由整理者合并进 resources/nav/。\n")
        yaml.dump({"session": info.get("start"), "source": data["source"],
                        "walk_speed": data["walk_speed"], "steps": drafts,
                        "links": links, "landings": landings},
                       f, Dumper=_NoAlias, allow_unicode=True, sort_keys=False, default_flow_style=None,
                       width=200)

    ticks = read_jsonl(os.path.join(root, "ticks.jsonl"))
    ok = [x for x in ticks if x.get("ok", True)]
    gaps = sorted(round(b["t"] - a["t"], 2) for a, b in zip(ok, ok[1:]))

    def pct(a, q):
        return a[min(len(a) - 1, int(q * len(a)))] if a else None
    stats = [f"帧数 {len(ticks)}（失败 {len(ticks) - len(ok)}）",
             f"帧间隔 中位 {pct(gaps, 0.5)}s  95% {pct(gaps, 0.95)}s  最大 {gaps[-1] if gaps else None}s"
             f"（目标 {1 / float(info.get('fps') or 2):.2f}s，明显偏大说明主机性能不够）",
             f"读到坐标的帧 {len(data['track'])}",
             f"走路速度 {data['walk_speed']} 坐标/秒"]
    with open(os.path.join(out_dir, "stats.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(stats) + "\n")
    md += ["## 统计", ""] + [f"- {s}" for s in stats]
    path = os.path.join(out_dir, "report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    return path


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__)
        return 2
    print("报告：", build(argv[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
