"""副机只读原图记录器（1:1 逐像素比对用）：只截图，不移动鼠标、不注入输入。

在副机上运行（标准库，无需第三方包；与本目录的 common/ 放在一起）：
    python capture_once.py
    python capture_once.py --frames 5 --interval 0.8

抓的是游戏窗口 WSGAME 的 800x600 客户区（PrintWindow，被遮挡也能取到），
每帧存成无损 PNG 并记录时间戳/指针位置/前台窗口，结束后把 PNG 和 manifest 发回主机。
运行期间尽量别动鼠标；用键盘启动命令即可。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import winapi, wincap  # noqa: E402

GAME_SIZE = (800, 600)
GAME_CLASS = "WSGAME"
TITLE_KEYWORD = "梦幻"
GAME_EXES = ("mhmain.exe", "mhtab.exe")


def find_game_window() -> int:
    """WSGAME 子窗口优先，其次标题含“梦幻”且客户区 800x600 的窗口。"""
    best, best_score = 0, 0

    def consider(h: int) -> None:
        nonlocal best, best_score
        w, hgt = winapi.client_size(h)
        if w <= 0 or hgt <= 0:
            return
        score = 0
        if winapi.class_name(h) == GAME_CLASS:
            score += 4
        if TITLE_KEYWORD in winapi.title(h) and (w, hgt) == GAME_SIZE:
            score += 4
        if not score:
            return
        try:
            exe = os.path.basename(winapi.image_path(winapi.pid_of(h))).lower()
        except Exception:  # noqa: BLE001
            exe = ""
        if exe in GAME_EXES:
            score += 2
        if (w, hgt) == GAME_SIZE:
            score += 1
        if score > best_score:
            best, best_score = h, score

    for top in winapi.enum_top_windows():
        if not winapi.user32.IsWindowVisible(top):
            continue
        consider(top)
        for child in winapi.enum_children(top):
            if winapi.user32.IsWindowVisible(child):
                consider(child)
    return best


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + kind + payload +
            struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))


def write_png(path: str, rgb: bytes, width: int, height: int) -> None:
    """24 位 RGB PNG，只用标准库 zlib，不依赖 Pillow（无损）。"""
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: none
        raw += rgb[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.write(_png_chunk(b"IHDR", ihdr))
        handle.write(_png_chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
        handle.write(_png_chunk(b"IEND", b""))


def foreground_info() -> dict:
    h = winapi.foreground()
    if not h:
        return {}
    try:
        return {"handle": h, "class": winapi.class_name(h), "title": winapi.title(h)[:60]}
    except Exception:  # noqa: BLE001
        return {"handle": h}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", type=int, default=5)
    parser.add_argument("--interval", type=float, default=0.8)
    parser.add_argument("--out-dir", default=HERE)
    args = parser.parse_args(argv)
    winapi.set_dpi_aware()
    hwnd = find_game_window()
    if not hwnd:
        print("game window not found (WSGAME / 800x600)")
        return 2
    width, height = winapi.client_size(hwnd)
    if (width, height) != GAME_SIZE:
        print(f"game client is {width}x{height}, not 800x600; refused")
        return 2
    os.makedirs(args.out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    frames = []
    for index in range(max(1, args.frames)):
        before = time.time()
        result = wincap.capture_rgb(hwnd)
        after = time.time()
        entry = {"index": index,
                 "epoch_ms": int(round(before * 1000)),
                 "epoch_ms_after": int(round(after * 1000)),
                 "cursor": list(winapi.cursor_pos()),
                 "ok": bool(result.get("ok")),
                 "method": result.get("method"),
                 "blank": result.get("blank"),
                 "note": result.get("note", "")}
        if result.get("ok"):
            rgb = bytes(result["rgb"])
            digest = hashlib.sha256(rgb).hexdigest()
            name = f"slave_800x600_{stamp}_{index}.png"
            path = os.path.join(args.out_dir, name)
            write_png(path, rgb, width, height)
            entry.update({"file": name, "sha256": digest, "bytes": len(rgb)})
            print(f"frame {index}: {name} {digest[:12]} {result.get('method')}")
        else:
            print(f"frame {index}: capture failed: {result.get('note')}")
        frames.append(entry)
        if index + 1 < args.frames:
            time.sleep(max(0.0, args.interval))
    origin = winapi.client_origin_on_screen(hwnd)
    manifest = {"script": "capture_once",
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "host": os.environ.get("COMPUTERNAME", ""),
                "window": hwnd,
                "class": winapi.class_name(hwnd),
                "title": winapi.title(hwnd)[:60],
                "client": [width, height],
                "client_origin": list(origin),
                "window_rect": list(winapi.window_rect(hwnd)),
                "foreground": foreground_info(),
                "frames": frames}
    path = os.path.join(args.out_dir, f"slave_capture_manifest_{stamp}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=1)
    print(f"manifest: {path}")
    return 0 if any(f.get("ok") for f in frames) else 1


if __name__ == "__main__":
    sys.exit(main())
