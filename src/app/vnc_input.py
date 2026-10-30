"""直接对副机的 VNC 服务端发输入，绕开远程查看器窗口。

为什么需要它
------------
用大漠绑查看器窗口发输入，中间隔了两层：大漠 -> 查看器窗口消息 -> 查看器转发。
任何一层出问题都表现为"输入不成功"，而且很难判断是哪一层。尤其：

  · 查看器只在窗口激活时处理键盘（WM_KEYDOWN 到非焦点窗口常被丢弃）
  · 查看器可能用 Raw Input 读鼠标，合成消息根本进不去
  · 消息发太快会被查看器合并/丢弃（移动后立刻点击最典型）

这个模块换掉中间那两层：主控机直接说 RFB 协议（就是 VNC 的协议），让**副机上的
VNC 服务端**去生成输入。服务端在副机本地调 SendInput，对游戏来说和真人操作没有
区别——这正是你现在用查看器操作时走的同一条路径，只不过不再经过窗口消息。

输入用到的协议部分极小：握手 + 认证 + 两个消息（PointerEvent / KeyEvent），
不需要帧缓冲、不需要编码协商，所以没有解码图片那堆麻烦事。

用法
----
    # 只验证能不能连上、认证过不过
    python vnc_input.py --host 100.64.0.10 --password 你的VNC密码 --probe

    # 移动 + 左键单击
    python vnc_input.py --host 100.64.0.10 --password xxx --move 400 300 --click left

    # 右键双击、按键、输入文字
    python vnc_input.py --host H --password P --move 300 200 --click double
    python vnc_input.py --host H --password P --key enter
    python vnc_input.py --host H --password P --type "hello"

注意：坐标是**副机桌面坐标**（不是游戏客户区坐标），所以等于 游戏坐标 + 游戏窗口
在副机桌面上的左上角。副机分辨率固定后这个偏移是常量。
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from typing import Optional

try:
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    from cryptography.hazmat.primitives.ciphers import Cipher, modes
except ImportError:  # 老版本 cryptography
    from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES  # type: ignore
    from cryptography.hazmat.primitives.ciphers import Cipher, modes  # type: ignore


# ---------------------------------------------------------------------------
# X11 keysym 表（只列常用的，字母数字直接用 ASCII 值）
# ---------------------------------------------------------------------------
KEYSYMS = {
    "enter": 0xFF0D, "return": 0xFF0D, "tab": 0xFF09, "esc": 0xFF1B,
    "escape": 0xFF1B, "backspace": 0xFF08, "space": 0x0020, "delete": 0xFFFF,
    "home": 0xFF50, "end": 0xFF57, "pageup": 0xFF55, "pagedown": 0xFF56,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "shift": 0xFFE1, "ctrl": 0xFFE3, "control": 0xFFE3, "alt": 0xFFE9,
    "f1": 0xFFBE, "f2": 0xFFBF, "f3": 0xFFC0, "f4": 0xFFC1, "f5": 0xFFC2,
    "f6": 0xFFC3, "f7": 0xFFC4, "f8": 0xFFC5, "f9": 0xFFC6, "f10": 0xFFC7,
    "f11": 0xFFC8, "f12": 0xFFC9,
}

BUTTON_MASK = {"left": 0x1, "middle": 0x2, "right": 0x4,
               "wheelup": 0x8, "wheeldown": 0x10}


def _mirror_bits(b: int) -> int:
    """VNC 认证要求把密码每个字节的位序反转，这是 RFB 协议的历史包袱。"""
    out = 0
    for i in range(8):
        if b & (1 << i):
            out |= 1 << (7 - i)
    return out


def vnc_auth_response(password: str, challenge: bytes) -> bytes:
    """用 DES-ECB 加密 16 字节挑战，得到 VNC 认证应答。"""
    raw = password.encode("latin-1", "replace")[:8]
    raw = raw + b"\x00" * (8 - len(raw))
    key = bytes(_mirror_bits(c) for c in raw)
    enc = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()  # 三个相同子密钥 = 单 DES
    return enc.update(challenge) + enc.finalize()


class RfbInput:
    """最小的 RFB 输入客户端：握手、认证、发鼠标键盘事件。"""

    def __init__(self, host: str, port: int = 5900, timeout: float = 6.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.version = ""
        self.server_size = (0, 0)

    # ---------------------------------------------------------------- 连接
    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock.settimeout(self.timeout)
        banner = self._recv_exact(12)
        if not banner.startswith(b"RFB "):
            raise RuntimeError(f"不是 VNC 服务端，收到: {banner!r}")
        self.version = banner.decode("ascii").strip()
        # 3.3 服务端只认 3.3；否则统一用 3.8
        reply = b"RFB 003.003\n" if self.version == "RFB 003.003" else b"RFB 003.008\n"
        self.sock.sendall(reply)

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))  # type: ignore[union-attr]
            if not chunk:
                raise ConnectionError("连接被对端关闭")
            buf += chunk
        return buf

    def _recv_reason(self) -> str:
        length = struct.unpack(">I", self._recv_exact(4))[0]
        return self._recv_exact(length).decode("latin-1", "replace") if length else ""

    # ---------------------------------------------------------------- 认证
    def authenticate(self, password: str = "") -> str:
        """返回实际使用的认证方式名。"""
        if self.version == "RFB 003.003":
            sec_type = struct.unpack(">I", self._recv_exact(4))[0]
            if sec_type == 0:
                raise RuntimeError("服务端拒绝连接: " + self._recv_reason())
            chosen = sec_type
        else:
            count = self._recv_exact(1)[0]
            if count == 0:
                raise RuntimeError("服务端拒绝连接: " + self._recv_reason())
            types = list(self._recv_exact(count))
            if 2 in types:
                chosen = 2
            elif 1 in types:
                chosen = 1
            else:
                raise RuntimeError(f"不支持的认证方式: {types}（只支持 None/VNC 密码）")
            self.sock.sendall(bytes([chosen]))  # type: ignore[union-attr]

        if chosen == 2:
            if not password:
                raise RuntimeError("这个 VNC 需要密码，请传 --password")
            challenge = self._recv_exact(16)
            self.sock.sendall(vnc_auth_response(password, challenge))  # type: ignore[union-attr]
            method = "VNC 密码"
        elif chosen == 1:
            method = "None"
        else:
            raise RuntimeError(f"不支持的认证方式编号: {chosen}")

        # 3.8 才在认证后单独回 SecurityResult；3.3 的 None 认证没有这一步
        if self.version != "RFB 003.003" or chosen == 2:
            status = struct.unpack(">I", self._recv_exact(4))[0]
            if status != 0:
                reason = ""
                try:
                    reason = self._recv_reason()
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(f"认证失败（密码错了？）{reason}")

        self.sock.sendall(b"\x01")  # ClientInit: shared=1，不踢掉已连着的查看器
        head = self._recv_exact(24)
        w, h = struct.unpack(">HH", head[0:4])
        self.server_size = (w, h)
        return method

    # ---------------------------------------------------------------- 输入
    def move(self, x: int, y: int, mask: int = 0) -> None:
        self.sock.sendall(struct.pack(">BBHH", 5, mask, x, y))  # type: ignore[union-attr]

    def mouse_button(self, button: str, x: int, y: int, hold_ms: int = 40) -> None:
        bit = BUTTON_MASK[button]
        self.move(x, y, 0)
        time.sleep(0.03)  # 先移动再按，远程服务端更稳
        self.move(x, y, bit)
        time.sleep(hold_ms / 1000.0)
        self.move(x, y, 0)

    def click(self, button: str = "left", x: int = 0, y: int = 0, double: bool = False) -> None:
        self.mouse_button(button, x, y)
        if double:
            time.sleep(0.06)
            self.mouse_button(button, x, y)

    def wheel(self, up: bool, x: int, y: int, count: int = 1) -> None:
        bit = BUTTON_MASK["wheelup" if up else "wheeldown"]
        for _ in range(count):
            self.move(x, y, bit)
            time.sleep(0.02)
            self.move(x, y, 0)
            time.sleep(0.02)

    def key_event(self, keysym: int, down: bool) -> None:
        self.sock.sendall(struct.pack(">BBHI", 4, 1 if down else 0, 0, keysym))  # type: ignore[union-attr]

    def key_press(self, name: str, hold_ms: int = 40) -> None:
        keysym = KEYSYMS.get(name.lower())
        if keysym is None:
            if len(name) == 1:
                keysym = ord(name)
            else:
                raise ValueError(f"不认识的按键: {name}")
        self.key_event(keysym, True)
        time.sleep(hold_ms / 1000.0)
        self.key_event(keysym, False)

    def type_text(self, text: str, per_char_ms: int = 25) -> None:
        for ch in text:
            self.key_event(ord(ch), True)
            time.sleep(0.01)
            self.key_event(ord(ch), False)
            time.sleep(per_char_ms / 1000.0)

    # ---------------------------------------------------------------- 收尾
    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def __enter__(self) -> "RfbInput":
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="直接对副机 VNC 服务端发输入（绕开查看器窗口）")
    ap.add_argument("--host", required=True, help="副机地址，例如 100.64.0.10")
    ap.add_argument("--port", type=int, default=5900)
    ap.add_argument("--password", default="", help="VNC 密码（None 认证可省略）")
    ap.add_argument("--probe", action="store_true", help="只测连接和认证，不发任何输入")
    ap.add_argument("--move", nargs=2, type=int, metavar=("X", "Y"), help="移动鼠标到副机桌面坐标")
    ap.add_argument("--click", choices=["left", "right", "middle", "double"], help="点击")
    ap.add_argument("--wheel", choices=["up", "down"], help="滚轮")
    ap.add_argument("--key", help="按一次键，如 enter / esc / f1 / a")
    ap.add_argument("--type", dest="text", help="输入一段文字")
    ap.add_argument("--wait", type=float, default=0.4, help="动作之间等待秒数")
    args = ap.parse_args()

    cli = RfbInput(args.host, args.port)
    try:
        cli.connect()
        method = cli.authenticate(args.password)
        print(f"已连接 {args.host}:{args.port}  协议 {cli.version}  认证 {method}  "
              f"副机桌面 {cli.server_size[0]}x{cli.server_size[1]}")
        if args.probe:
            print("probe 通过：握手和认证都正常，输入通道可用")
            return 0

        x, y = args.move if args.move else (0, 0)
        if args.move:
            cli.move(x, y)
            print(f"移动 -> ({x}, {y})")
            time.sleep(args.wait)
        if args.click:
            double = args.click == "double"
            btn = "left" if double else args.click
            cli.click(btn, x, y, double=double)
            print(f"点击 {args.click} @ ({x}, {y})")
            time.sleep(args.wait)
        if args.wheel:
            cli.wheel(args.wheel == "up", x, y)
            print(f"滚轮 {args.wheel} @ ({x}, {y})")
            time.sleep(args.wait)
        if args.key:
            cli.key_press(args.key)
            print(f"按键 {args.key}")
            time.sleep(args.wait)
        if args.text:
            cli.type_text(args.text)
            print(f"输入文字 {args.text!r}")
        print("完成")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"失败: {type(e).__name__}: {e}")
        return 1
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main())
