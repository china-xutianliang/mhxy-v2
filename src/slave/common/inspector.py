"""窗口检查器（可嵌入任何 Tk 程序）：列出窗口和子窗口，点一个就显示它的信息和截图。

用来弄清"游戏画面窗口到底是哪个句柄、哪个类名、多大"。副机和主机都能用，只依赖标准库。
只读窗口信息和像素，不向任何窗口发消息。
"""
from __future__ import annotations

import math
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from common import winapi, wincap

GAME_CLASSES = ("WSGAME", "MHXYMainFrame")


def fit_ratio(w: int, h: int, maxw: int, maxh: int) -> tuple[int, int]:
    """返回 (zoom, subsample)：缩放比例 zoom/subsample 不超过能放进 maxw x maxh 的上限。"""
    limit = min(maxw / max(w, 1), maxh / max(h, 1), 1.0)
    if limit >= 1.0:
        return 1, 1
    best = (1, math.ceil(1 / limit))
    if w * h > 1_500_000:  # 图太大时只用整数抽样，避免放大中间图占内存
        return best
    for s in range(2, 9):
        for z in range(1, s):
            if z / s <= limit and z / s > best[0] / best[1]:
                best = (z, s)
    return best


def exe_name(pid: int, cache: dict[int, str]) -> str:
    if pid not in cache:
        path = winapi.image_path(pid)
        cache[pid] = path.rsplit("\\", 1)[-1] if path else ""
    return cache[pid]


def describe(h: int, cache: dict[int, str] | None = None) -> list[str]:
    cache = cache if cache is not None else {}
    if not winapi.is_window(h):
        return [f"句柄 {h} 已经不存在（窗口被关闭了？），请点“刷新”"]
    pid = winapi.pid_of(h)
    l, t, r, b = winapi.window_rect(h)
    cw, ch = winapi.client_size(h)
    ox, oy = winapi.client_origin_on_screen(h)
    parent = winapi.parent_of(h)
    return [
        f"句柄　　　{h}",
        f"类名　　　{winapi.class_name(h)}",
        f"标题　　　{winapi.title(h) or '（空）'}",
        f"进程　　　{exe_name(pid, cache) or '读不到'}（PID {pid}）",
        f"可见　　　{'是' if winapi.user32.IsWindowVisible(h) else '否'}　最小化：{'是' if winapi.is_iconic(h) else '否'}",
        f"父窗口　　{parent or '无（顶层窗口）'}",
        f"窗口外框　{r - l}x{b - t}　位置 ({l},{t})",
        f"客户区　　{cw}x{ch}　左上角在屏幕 ({ox},{oy})",
        f"是否 800x600　{'是' if (cw, ch) == (800, 600) else '否'}",
    ]


class InspectorFrame(ttk.Frame):
    def __init__(self, parent, on_pin=None, on_unpin=None, small_font=None, **kw) -> None:
        super().__init__(parent, padding=8, **kw)
        self.on_pin, self.on_unpin = on_pin, on_unpin
        self.cache: dict[int, str] = {}
        self.photo = None
        self.cur = 0
        self.scale = (1, 1)
        self.v_vis = tk.BooleanVar(value=True)
        self.v_filter = tk.StringVar()
        self.v_hover = tk.StringVar(value="把鼠标移到截图上，这里显示该点在窗口里的坐标。")
        self.v_msg = tk.StringVar(value="点“刷新窗口树”，再点列表里的任意一行。")
        self.small = small_font or tkfont.nametofont("TkDefaultFont")

        st = ttk.Style(self)
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        st.configure("Insp.Treeview", rowheight=int(line * 1.8))

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="刷新窗口树", command=self.refresh).pack(side="left")
        ttk.Checkbutton(bar, text="只看可见窗口", variable=self.v_vis, command=self.refresh).pack(side="left", padx=10)
        ttk.Label(bar, text="过滤（类名/标题/进程）").pack(side="left")
        e = ttk.Entry(bar, textvariable=self.v_filter, width=16)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda _e: self.refresh())
        ttk.Button(bar, text="只看游戏", command=self._only_game).pack(side="left", padx=4)
        if on_unpin:
            ttk.Button(bar, text="改回自动查找", command=self._unpin).pack(side="right")
        if on_pin:
            self.btn_pin = ttk.Button(bar, text="设为游戏窗口", command=self._pin)
            self.btn_pin.pack(side="right", padx=6)

        ttk.Label(self, textvariable=self.v_msg, foreground="#555555").pack(fill="x", pady=(6, 4))

        pan = ttk.PanedWindow(self, orient="horizontal")
        pan.pack(fill="both", expand=True)

        left = ttk.Frame(pan)
        cols = ("cls", "title", "exe", "size")
        self.tree = ttk.Treeview(left, columns=cols, show="tree headings", style="Insp.Treeview", selectmode="browse")
        self.tree.heading("#0", text="句柄")
        for c, text, w in (("cls", "类名", 125), ("title", "标题", 110), ("exe", "进程", 80), ("size", "客户区", 80)):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, anchor="w", stretch=False)
        self.tree.column("#0", width=100, stretch=False)
        self.tree.tag_configure("game", foreground="#1e8449", font=(self.small.cget("family"), self.small.cget("size"), "bold"))
        sy = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        sx = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        pan.add(left, weight=3)

        right = ttk.Frame(pan)
        self.info = tk.Text(right, height=9, wrap="word", state="disabled")
        self.info.pack(fill="x")
        ttk.Label(right, textvariable=self.v_hover).pack(fill="x", pady=4)
        self.canvas = tk.Canvas(right, bg="#202020", highlightthickness=0, width=520, height=420)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Motion>", self._on_motion)
        pan.add(right, weight=2)

        self.after(200, self.refresh)

    # ---- 窗口树
    def _matches(self, h: int, flt: str) -> bool:
        if not flt:
            return True
        pid = winapi.pid_of(h)
        blob = f"{winapi.class_name(h)} {winapi.title(h)} {exe_name(pid, self.cache)} {h}".lower()
        return flt in blob

    def _insert(self, parent_iid: str, h: int, tags=()) -> str:
        cw, ch = winapi.client_size(h)
        cls = winapi.class_name(h)
        pid = winapi.pid_of(h)
        iid = str(h)
        if self.tree.exists(iid):
            return iid
        self.tree.insert(parent_iid, "end", iid=iid, text=str(h),
                         values=(cls, winapi.title(h)[:40], exe_name(pid, self.cache), f"{cw}x{ch}"),
                         tags=("game",) if cls in GAME_CLASSES else tags)
        return iid

    def refresh(self) -> None:
        self.cache.clear()
        self.tree.delete(*self.tree.get_children())
        flt = self.v_filter.get().strip().lower()
        vis_only = self.v_vis.get()
        opened, first_game = 0, ""
        for top in winapi.enum_top_windows():
            if vis_only and not winapi.user32.IsWindowVisible(top):
                continue
            kids = winapi.enum_children(top)
            if vis_only:
                kids = [c for c in kids if winapi.user32.IsWindowVisible(c)]
            if vis_only and not kids and min(winapi.client_size(top)) <= 0:
                continue  # 0x0 的系统辅助窗口，没有意义
            if flt and not (self._matches(top, flt) or any(self._matches(c, flt) for c in kids)):
                continue
            top_iid = self._insert("", top)
            for c in kids:
                par = str(winapi.parent_of(c))
                self._insert(par if self.tree.exists(par) else top_iid, c)
                if not first_game and winapi.class_name(c) == "WSGAME":
                    first_game = str(c)
            if flt or any(winapi.class_name(c) in GAME_CLASSES for c in kids):
                self.tree.item(top_iid, open=True)
            opened += 1
        n = len(self.tree.get_children())
        self.v_msg.set(f"共 {n} 个顶层窗口。绿色粗体是游戏相关窗口，点任意一行看信息和截图。")
        if first_game and self.tree.exists(first_game):
            self.tree.see(first_game)
            self.tree.selection_set(first_game)

    def _only_game(self) -> None:
        self.v_filter.set("WSGAME")
        self.refresh()

    # ---- 选中
    def selected(self) -> int:
        sel = self.tree.selection()
        return int(sel[0]) if sel else 0

    def _set_info(self, lines: list[str]) -> None:
        self.info.config(state="normal")
        self.info.delete("1.0", "end")
        self.info.insert("end", "\n".join(lines))
        self.info.config(state="disabled")

    def _on_select(self, _e=None) -> None:
        h = self.selected()
        if not h:
            return
        self.cur = h
        self._set_info(describe(h, self.cache))
        self.show_capture(h)

    def show_capture(self, h: int) -> None:
        self.canvas.delete("all")
        self.photo = None
        res = wincap.capture_rgb(h)
        if not res["ok"]:
            self.v_msg.set("截图失败：" + res["note"])
            self.canvas.create_text(10, 10, anchor="nw", fill="white", text=res["note"], width=320)
            return
        w, hgt = res["w"], res["h"]
        self.update_idletasks()
        maxw, maxh = max(self.canvas.winfo_width() - 4, 200), max(self.canvas.winfo_height() - 4, 150)
        z, s = fit_ratio(w, hgt, maxw, maxh)
        img = tk.PhotoImage(data=wincap.to_ppm(res["rgb"], w, hgt))
        if z > 1:
            img = img.zoom(z, z)
        if s > 1:
            img = img.subsample(s, s)
        self.photo, self.scale = img, (z, s)
        self.canvas.create_image(0, 0, anchor="nw", image=img)
        tip = f"截图 {w}x{hgt}（{res['method']}），显示比例 {z}/{s}"
        if res["blank"]:
            tip += "　⚠ 画面是纯色，这个窗口可能不是真正显示画面的窗口，或被 GPU 渲染绕开了"
        if res["note"]:
            tip += "　" + res["note"]
        self.v_msg.set(tip)

    def _on_motion(self, ev) -> None:
        if not self.photo:
            return
        z, s = self.scale
        x, y = int(ev.x * s / z), int(ev.y * s / z)
        self.v_hover.set(f"窗口内坐标 ({x},{y})")

    # ---- 回调
    def _pin(self) -> None:
        h = self.selected()
        if h and self.on_pin:
            self.on_pin(h)
            self.v_msg.set(f"已把 {h} 设为游戏窗口。")

    def _unpin(self) -> None:
        if self.on_unpin:
            self.on_unpin()
            self.v_msg.set("已改回自动查找游戏窗口。")
