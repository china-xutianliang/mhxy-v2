"""最小 PNG 写入（纯标准库）。"""
from __future__ import annotations

import struct
import zlib


def _chunk(tag: bytes, data: bytes) -> bytes:
    body = tag + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def encode_png(rgb, w: int, h: int) -> bytes:
    stride = w * 3
    raw = b"".join(b"\x00" + bytes(rgb[y * stride:(y + 1) * stride]) for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 3)) + _chunk(b"IEND", b""))


def write_png(path: str, rgb, w: int, h: int) -> None:
    with open(path, "wb") as f:
        f.write(encode_png(rgb, w, h))
