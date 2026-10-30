"""副机清单与界面设置的保存。

清单存在当前用户的 %APPDATA%\\mhxy\\manager.json，不进仓库。
VNC 密码默认只留在内存里；勾选"记住密码"才会保存，并且用 Windows DPAPI 加密
（只有当前 Windows 用户在本机能解开，拷到别的电脑或别的用户下都读不出来）。
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field

APPDATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "mhxy")
DEFAULT_PATH = os.path.join(APPDATA_DIR, "manager.json")


# ---------------------------------------------------------------- DPAPI
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _crypt(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(src), "mhxy", None, None, None, 0, ctypes.byref(out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI 失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect_text(text: str) -> str:
    return base64.b64encode(_crypt(text.encode("utf-8"), True)).decode("ascii")


def unprotect_text(blob: str) -> str:
    return _crypt(base64.b64decode(blob), False).decode("utf-8")


# ---------------------------------------------------------------- 数据
@dataclass
class Machine:
    name: str
    host: str
    port: int = 5900
    password: str = ""        # 只在内存里；remember=True 时才加密落盘
    remember: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def address(self) -> str:
        return f"{self.host}:{self.port}"


DEFAULT_SETTINGS = {
    "layout": "auto",         # auto 或 "cols:N"
    "quality": "balanced",    # smooth / balanced / saver
    "only_online": False,
    "auto_reconnect": True,
    "auto_connect_on_start": False,
}


class Store:
    def __init__(self, path: str = DEFAULT_PATH) -> None:
        self.path = path
        self.machines: list[Machine] = []
        self.settings: dict = dict(DEFAULT_SETTINGS)

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        self.settings = {**DEFAULT_SETTINGS, **data.get("settings", {})}
        self.machines = []
        for m in data.get("machines", []):
            pw = ""
            if m.get("pw_enc"):
                try:
                    pw = unprotect_text(m["pw_enc"])
                except (OSError, ValueError):
                    pw = ""  # 换了电脑或用户，解不开，让用户重新输入
            self.machines.append(Machine(
                name=m.get("name", ""), host=m.get("host", ""), port=int(m.get("port", 5900)),
                password=pw, remember=bool(m.get("pw_enc")) and bool(pw), id=m.get("id") or uuid.uuid4().hex[:8]))

    def save(self) -> None:
        rows = []
        for m in self.machines:
            row = {"id": m.id, "name": m.name, "host": m.host, "port": m.port}
            if m.remember and m.password:
                row["pw_enc"] = protect_text(m.password)
            rows.append(row)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"machines": rows, "settings": self.settings}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def add(self, m: Machine) -> None:
        self.machines.append(m)

    def remove(self, mid: str) -> None:
        self.machines = [m for m in self.machines if m.id != mid]

    def get(self, mid: str) -> Machine | None:
        return next((m for m in self.machines if m.id == mid), None)

    def next_default_name(self) -> str:
        used = {m.name for m in self.machines}
        i = 1
        while f"设备{i}" in used:
            i += 1
        return f"设备{i}"
