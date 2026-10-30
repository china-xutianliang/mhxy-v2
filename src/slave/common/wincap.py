"""按窗口句柄截图（纯 ctypes，不依赖第三方库，副机也能用）。

只用 PrintWindow / BitBlt 读窗口自己的像素，不注入、不挂钩。
PrintWindow 对 DirectX/GPU 渲染的窗口有时得到纯黑，这时退回 BitBlt（读屏幕上该窗口的区域，
窗口被遮挡时会带上遮挡物），并在结果里标明用了哪种方式、是不是纯色，方便判断。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from common import winapi

user32 = winapi.user32
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
VP = ctypes.c_void_p

user32.GetDC.argtypes = [VP]
user32.GetDC.restype = VP
user32.ReleaseDC.argtypes = [VP, VP]
user32.PrintWindow.argtypes = [VP, VP, wintypes.UINT]
gdi32.CreateCompatibleDC.argtypes = [VP]
gdi32.CreateCompatibleDC.restype = VP
gdi32.CreateCompatibleBitmap.argtypes = [VP, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = VP
gdi32.SelectObject.argtypes = [VP, VP]
gdi32.SelectObject.restype = VP
gdi32.DeleteObject.argtypes = [VP]
gdi32.DeleteDC.argtypes = [VP]
gdi32.BitBlt.argtypes = [VP, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, VP,
                         ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.GetDIBits.argtypes = [VP, VP, wintypes.UINT, wintypes.UINT, VP, VP, wintypes.UINT]
gdi32.StretchBlt.argtypes = [VP, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, VP,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.SetStretchBltMode.argtypes = [VP, ctypes.c_int]
gdi32.SetBrushOrgEx.argtypes = [VP, ctypes.c_int, ctypes.c_int, VP]

SRCCOPY = 0x00CC0020
HALFTONE = 4
PW_CLIENTONLY = 0x1
PW_RENDERFULLCONTENT = 0x2
MAX_PIXELS = 12_000_000  # 防止误选超大窗口把内存吃光


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def bgra_to_rgb(bgra: bytes, w: int, h: int) -> bytearray:
    out = bytearray(w * h * 3)
    out[0::3] = bgra[2::4]
    out[1::3] = bgra[1::4]
    out[2::3] = bgra[0::4]
    return out


def is_blank(rgb: bytes, step_px: int = 53) -> bool:
    """抽样判断是不是纯色图（全黑/全白等）。"""
    seen = set()
    for i in range(0, len(rgb) - 2, 3 * step_px):
        seen.add(bytes(rgb[i:i + 3]))
        if len(seen) > 1:
            return False
    return True


def to_ppm(rgb: bytes, w: int, h: int) -> bytes:
    """Tk 的 PhotoImage 能直接读 PPM(P6) 数据，不需要 Pillow。"""
    return b"P6 %d %d 255\n" % (w, h) + bytes(rgb)


def _grab(hwnd: int, w: int, h: int, how: str) -> bytes | None:
    src = user32.GetDC(hwnd)
    if not src:
        return None
    mem = gdi32.CreateCompatibleDC(src)
    bmp = gdi32.CreateCompatibleBitmap(src, w, h)
    try:
        if not mem or not bmp:
            return None
        old = gdi32.SelectObject(mem, bmp)
        if how == "PrintWindow":
            ok = bool(user32.PrintWindow(hwnd, mem, PW_CLIENTONLY | PW_RENDERFULLCONTENT))
        else:
            ok = bool(gdi32.BitBlt(mem, 0, 0, w, h, src, 0, 0, SRCCOPY))
        gdi32.SelectObject(mem, old)
        if not ok:
            return None
        hdr = BITMAPINFOHEADER()
        hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        hdr.biWidth, hdr.biHeight = w, -h  # 负数 = 自上而下
        hdr.biPlanes, hdr.biBitCount = 1, 32
        buf = ctypes.create_string_buffer(w * h * 4)
        if gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(hdr), 0) != h:
            return None
        return buf.raw
    finally:
        if bmp:
            gdi32.DeleteObject(bmp)
        if mem:
            gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, src)


def capture_rgb(hwnd: int) -> dict:
    """截取窗口客户区。返回 dict：ok, w, h, rgb, method, blank, note。"""
    w, h = winapi.client_size(hwnd)
    if w <= 0 or h <= 0:
        return {"ok": False, "w": w, "h": h, "note": f"客户区是 {w}x{h}，没有画面可截（窗口最小化或隐藏？）"}
    if w * h > MAX_PIXELS:
        return {"ok": False, "w": w, "h": h, "note": f"客户区 {w}x{h} 太大，不截图"}
    notes = []
    for how in ("PrintWindow", "BitBlt"):
        raw = _grab(hwnd, w, h, how)
        if raw is None:
            notes.append(f"{how} 失败")
            continue
        rgb = bgra_to_rgb(raw, w, h)
        if is_blank(rgb) and how == "PrintWindow":
            notes.append("PrintWindow 得到纯色图，改用 BitBlt")
            fallback = (w, h, rgb)
            continue
        return {"ok": True, "w": w, "h": h, "rgb": rgb, "method": how, "blank": is_blank(rgb),
                "note": "；".join(notes)}
    if notes and "fallback" in locals():
        fw, fh, frgb = fallback
        return {"ok": True, "w": fw, "h": fh, "rgb": frgb, "method": "PrintWindow", "blank": True,
                "note": "；".join(notes)}
    return {"ok": False, "w": w, "h": h, "note": "；".join(notes) or "截图失败"}


def capture_scaled(hwnd: int, out_w: int, out_h: int) -> dict:
    """截取窗口客户区并直接在 GDI 里缩放到 out_w x out_h（缩略图用，比先截全图再缩快得多）。

    只用 PrintWindow，窗口被遮挡或在屏幕外也能取到。返回 dict：ok, w, h, rgb, blank, src_w, src_h, note。
    """
    sw, sh = winapi.client_size(hwnd)
    if sw <= 0 or sh <= 0:
        return {"ok": False, "note": f"客户区是 {sw}x{sh}（窗口最小化或隐藏）"}
    if sw * sh > MAX_PIXELS or out_w <= 0 or out_h <= 0 or out_w * out_h > MAX_PIXELS:
        return {"ok": False, "note": "尺寸不合理"}
    src = user32.GetDC(hwnd)
    if not src:
        return {"ok": False, "note": "GetDC 失败"}
    mem = mem2 = bmp = bmp2 = None
    try:
        mem = gdi32.CreateCompatibleDC(src)
        bmp = gdi32.CreateCompatibleBitmap(src, sw, sh)
        if not mem or not bmp:
            return {"ok": False, "note": "创建位图失败"}
        old = gdi32.SelectObject(mem, bmp)
        ok = bool(user32.PrintWindow(hwnd, mem, PW_CLIENTONLY | PW_RENDERFULLCONTENT))
        if not ok:
            gdi32.SelectObject(mem, old)
            return {"ok": False, "note": "PrintWindow 失败"}
        if (out_w, out_h) == (sw, sh):
            final_bmp, final_dc, old_final = bmp, mem, old
        else:
            mem2 = gdi32.CreateCompatibleDC(src)
            bmp2 = gdi32.CreateCompatibleBitmap(src, out_w, out_h)
            if not mem2 or not bmp2:
                gdi32.SelectObject(mem, old)
                return {"ok": False, "note": "创建缩略位图失败"}
            old2 = gdi32.SelectObject(mem2, bmp2)
            gdi32.SetStretchBltMode(mem2, HALFTONE)
            gdi32.SetBrushOrgEx(mem2, 0, 0, None)
            gdi32.StretchBlt(mem2, 0, 0, out_w, out_h, mem, 0, 0, sw, sh, SRCCOPY)
            gdi32.SelectObject(mem, old)
            final_bmp, final_dc, old_final = bmp2, mem2, old2
        gdi32.SelectObject(final_dc, old_final)  # GetDIBits 要求位图不在任何 DC 里
        hdr = BITMAPINFOHEADER()
        hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        hdr.biWidth, hdr.biHeight = out_w, -out_h
        hdr.biPlanes, hdr.biBitCount = 1, 32
        buf = ctypes.create_string_buffer(out_w * out_h * 4)
        if gdi32.GetDIBits(final_dc, final_bmp, 0, out_h, buf, ctypes.byref(hdr), 0) != out_h:
            return {"ok": False, "note": "GetDIBits 失败"}
        rgb = bgra_to_rgb(buf.raw, out_w, out_h)
        return {"ok": True, "w": out_w, "h": out_h, "rgb": rgb, "blank": is_blank(rgb),
                "src_w": sw, "src_h": sh, "note": ""}
    finally:
        for b in (bmp, bmp2):
            if b:
                gdi32.DeleteObject(b)
        for d in (mem, mem2):
            if d:
                gdi32.DeleteDC(d)
        user32.ReleaseDC(hwnd, src)