"""地图素材采集器图形入口（第三层）。

主机运行，连接已经按第一层/第二层标准启动的 TightVNC 查看器。工具只读抓图，
不发送鼠标键盘，不改变查看器尺寸；录制核心仍由 :mod:`nav.recorder` 提供。
"""
from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

from PIL import Image, ImageTk

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import wincap, winapi
from common.config import runtime_dir
from control import viewer_link
from nav import elevation, hud_read, map_names, rec_steps, recorder, recorder_report, uikit

G_W, G_H = 800, 600
FPS_LABELS = ("1 帧/秒", "2 帧/秒", "3 帧/秒", "5 帧/秒")
ACTION_BUTTONS = (("charm", "飞行符"), ("flag", "旗"), ("portal", "传送圈/出口"), ("enter", "进门/上下楼"),
                  ("npc", "NPC 传送"), ("earth", "土地"), ("post", "驿站/车夫"), ("other", "其他"))
ACTION_COLS = 4
SHORT_HOW = {"charm": "飞行符", "flag": "旗", "portal": "传送圈", "npc": "NPC", "enter": "进门",
             "post": "驿站", "earth": "土地", "other": "其他",
             "flag_or_charm": "旗或飞行符?", "walk_or_portal": "走过去/传送圈?"}


class RecorderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.ui = uikit.dpi_scale()
        self.title("地图素材采集器")
        left, top, right, bottom = uikit.work_area()
        avail_w, avail_h = right - left, bottom - top
        w = min(int(1000 * self.ui), avail_w - 60)
        h = min(int(740 * self.ui), avail_h - 110)
        self.geometry(f"{w}x{h}+{left + max(0, (avail_w - w) // 2)}+{top + max(0, (avail_h - h) // 3)}")
        self.minsize(min(int(960 * self.ui), avail_w - 40), min(int(640 * self.ui), avail_h - 150))
        self.configure(bg=uikit.BG)
        uikit.set_window_icon(self)
        self._fonts()

        self.viewer_var = tk.StringVar()
        self.route_var = tk.StringVar()
        default_out = (os.path.join(os.path.expanduser("~"), "Pictures", "mhxy", "recorded")
                       if getattr(sys, "frozen", False)
                       else os.path.join(runtime_dir(), "recorded"))
        self.out_var = tk.StringVar(value=default_out)
        self.fps_var = tk.StringVar(value="2 帧/秒")
        self.elapsed_var = tk.StringVar(value="00:00")
        self.steps_var = tk.StringVar(value="0")
        self.note_var = tk.StringVar()

        self._viewers: dict[str, object] = {}
        self._selected = None
        self._viewer_ok = False
        self._size_ok = False
        self._minimized = False
        self._hidden = False
        self._restoring = False
        self._record_thread: threading.Thread | None = None
        self._control_queue: queue.Queue | None = None
        self._stop_event: threading.Event | None = None
        self._ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._preview_q: queue.Queue = queue.Queue(maxsize=1)
        self._preview_frame = None
        self._preview_photo: ImageTk.PhotoImage | None = None
        self._started_at = 0.0
        self._elapsed = 0.0
        self._steps: list[dict] = []
        self._actions: list[dict] = []
        self._overrides: dict[int, str] = {}
        self._book = hud_read.default_book()
        self._hud: dict | None = None        # 最近一次左上角读数 {scene, known, xy}
        self._hud_mask = None                # 空闲探测时的场景名字形，起名用
        self._session_dir: str | None = None
        self._last_session: str | None = None
        self._recording = False
        self._finishing = False
        self._reporting = False
        self._report_again = False
        self._reported = False
        self._report_failed = False
        self._error: str | None = None
        self._closing = False
        self._blink_on = False
        self._tick_n = 0

        self._build()
        self.refresh_viewers(silent=True)
        self._update_state()
        self._log("操作说明", "guide_title", stamp=False)
        self._log_guide()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<F5>", lambda _e: self.refresh_viewers())
        self.after(120, self._pump)
        self.after(500, self._tick)
        self.after(600, self._blink)
        self.after(150, lambda: uikit.set_light_titlebar(self))

    # ------------------------------------------------------------ 基础
    def P(self, value: float) -> int:
        return int(round(value * self.ui))

    def _fonts(self) -> None:
        ui = self.ui
        self.f_display = uikit.font(ui, 17, True)
        self.f_title = uikit.font(ui, 13, True)
        self.f_body = uikit.font(ui, 12)
        self.f_body_b = uikit.font(ui, 12, True)
        self.f_small = uikit.font(ui, 11)
        self.f_small_b = uikit.font(ui, 11, True)
        self.f_micro = uikit.font(ui, 10)
        self.f_micro_b = uikit.font(ui, 10, True)

    def _build(self) -> None:
        self._build_header()
        body = tk.Frame(self, bg=uikit.BG)
        body.pack(fill="both", expand=True, padx=self.P(20), pady=(self.P(16), self.P(20)))
        right = tk.Frame(body, bg=uikit.BG, width=self.P(440))
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        left = tk.Frame(body, bg=uikit.BG)
        left.pack(side="left", fill="both", expand=True, padx=(0, self.P(16)))
        self._build_setup(left)
        self._build_status(left)
        self._build_actions(right)
        self._build_preview(right)

    def _build_header(self) -> None:
        bar = tk.Frame(self, bg=uikit.CARD, height=self.P(56))
        bar.pack(fill="x")
        bar.pack_propagate(False)
        tk.Frame(self, bg=uikit.EDGE_SOFT, height=max(1, self.P(1))).pack(fill="x")

        right = tk.Frame(bar, bg=uikit.CARD)
        right.pack(side="right", fill="y", padx=(0, self.P(20)))
        self.action_btn = uikit.Button(right, "开始采集", self.on_action, self.ui, variant="primary",
                                       icon="play", height=32, min_width=132, text_font=self.f_small_b)
        self.action_btn.pack(side="right", pady=self.P(12))
        self.report_btn = uikit.Button(right, "生成报告", self.build_report, self.ui, variant="secondary",
                                       icon="report", height=32, min_width=126, text_font=self.f_small_b)
        self.report_btn.pack(side="right", padx=(0, self.P(8)), pady=self.P(12))
        self.open_btn = uikit.Button(right, "打开目录", self.open_output, self.ui, variant="ghost",
                                     icon="folder", height=32, min_width=112, text_font=self.f_small_b)
        self.open_btn.pack(side="right", pady=self.P(12))

        left = tk.Frame(bar, bg=uikit.CARD)
        left.pack(side="left", fill="both", expand=True, padx=(self.P(20), 0))
        self.pill = uikit.Pill(left, self.ui, text_font=self.f_small_b, height=26)
        self.pill.pack(side="left")
        prefix = tk.Label(left, text="路线", bg=uikit.CARD, fg=uikit.FG_FAINT, font=self.f_micro)
        prefix.pack(side="left", padx=(self.P(16), self.P(6)))
        self.route_head = tk.Label(left, text="未命名路线", bg=uikit.CARD, fg=uikit.FG_DIM,
                                   font=self.f_small, anchor="w")
        self.route_head.pack(side="left")
        self.route_var.trace_add("write", lambda *_: self._refresh_route_head())

    def _refresh_route_head(self) -> None:
        text = self.route_var.get().strip() or "未命名路线"
        if len(text) > 16:
            text = text[:15] + "…"
        self.route_head.configure(text=text)

    def _card_head(self, card: uikit.Card, title: str, subtitle: str = "") -> tk.Frame:
        head = tk.Frame(card.body, bg=uikit.CARD)
        head.pack(fill="x")
        box = tk.Frame(head, bg=uikit.CARD)
        box.pack(side="left", anchor="w")
        tk.Label(box, text=title, bg=uikit.CARD, fg=uikit.FG, font=self.f_title).pack(anchor="w")
        if subtitle:
            tk.Label(box, text=subtitle, bg=uikit.CARD, fg=uikit.FG_DIM,
                     font=self.f_micro).pack(anchor="w", pady=(self.P(2), 0))
        return head

    def _field_row(self, parent: tk.Misc, label: str) -> tk.Frame:
        row = tk.Frame(parent, bg=uikit.CARD)
        row.pack(fill="x", pady=(self.P(12), 0))
        lead = tk.Frame(row, bg=uikit.CARD, width=self.P(82), height=self.P(32))
        lead.pack(side="left")
        lead.pack_propagate(False)
        tk.Label(lead, text=label, bg=uikit.CARD, fg=uikit.FG_DIM, font=self.f_small,
                 anchor="w").pack(fill="both", expand=True)
        return row

    # ------------------------------------------------------------ 左栏：设置
    def _build_setup(self, parent: tk.Frame) -> None:
        card = uikit.Card(parent, self.ui, pad=(16, 14))
        card.pack(fill="x")
        self._card_head(card, "采集设置", "选择查看器、填路线名；左上角读数正常后点右上角开始。")

        row = self._field_row(card.body, "查看器")
        self.size_pill = uikit.Pill(row, self.ui, text_font=self.f_micro, height=24)
        self.size_pill.pack(side="right", padx=(self.P(8), 0))
        self.refresh_btn = uikit.Button(row, "刷新", self.refresh_viewers, self.ui, variant="secondary",
                                        icon="refresh", height=30, min_width=0, pad=self.P(10),
                                        text_font=self.f_small)
        self.refresh_btn.pack(side="right", padx=(self.P(6), 0))
        self.restore_btn = uikit.Button(row, "还原", self.restore_viewer, self.ui, variant="secondary",
                                        height=30, min_width=0, pad=self.P(10), text_font=self.f_small)
        self.viewer_select = uikit.Select(row, [], self.viewer_var, self.ui, width=240, height=32,
                                          text_font=self.f_small, on_change=self._on_viewer_change,
                                          placeholder="未找到查看器，点“刷新”")
        self.viewer_select.pack(side="left", fill="x", expand=True)

        row = self._field_row(card.body, "地图 / 路线")
        self.fps_select = uikit.Select(row, FPS_LABELS, self.fps_var, self.ui, width=112, height=32,
                                       text_font=self.f_small, on_change=self._update_state)
        self.fps_select.pack(side="right", padx=(self.P(8), 0))
        self.route_picker = uikit.SearchSelect(row, self.route_var, self.ui,
                                               provider=map_names.all_entries,
                                               on_add=self._add_custom_map, height=32,
                                               text_font=self.f_small,
                                               placeholder="选择地图，或直接输入路线名")
        self.route_picker.pack(side="left", fill="x", expand=True)

        row = self._field_row(card.body, "保存位置")
        self.choose_btn = uikit.Button(row, "选择", self._choose_out, self.ui, variant="secondary",
                                       height=30, min_width=0, pad=self.P(12), text_font=self.f_small)
        self.choose_btn.pack(side="right", padx=(self.P(8), 0))
        self.out_entry = uikit.EntryBox(row, self.out_var, self.ui, height=32, text_font=self.f_small)
        self.out_entry.pack(side="left", fill="x", expand=True)

        tk.Frame(card.body, bg=uikit.EDGE_SOFT, height=max(1, self.P(1))).pack(
            fill="x", pady=(self.P(14), self.P(10)))
        row = tk.Frame(card.body, bg=uikit.CARD)
        row.pack(fill="x")
        tk.Label(row, text="当前位置", bg=uikit.CARD, fg=uikit.FG_DIM, font=self.f_small,
                 width=0, anchor="w").pack(side="left", padx=(0, self.P(12)))
        self.name_btn = uikit.Button(row, "给这张地图起名", self.ask_scene_name, self.ui, variant="primary",
                                     height=28, min_width=0, pad=self.P(10), text_font=self.f_micro_b)
        self.hud_scene = tk.Label(row, text="—", bg=uikit.CARD, fg=uikit.FG, font=self.f_display, anchor="w")
        self.hud_scene.pack(side="left")
        self.hud_xy = tk.Label(row, text="", bg=uikit.CARD, fg=uikit.BLUE, font=self.f_title, anchor="w")
        self.hud_xy.pack(side="left", padx=(self.P(12), 0))

        self.tip_label = tk.Label(card.body, text="", bg=uikit.CARD, fg=uikit.FG_FAINT,
                                  font=self.f_micro, anchor="w")
        self.tip_label.pack(fill="x", pady=(self.P(10), 0))
        self.setup_card = card

    # ------------------------------------------------------------ 左栏：状态
    def _stat(self, parent: tk.Misc, caption: str, variable: tk.StringVar) -> None:
        box = tk.Frame(parent, bg=uikit.CARD)
        box.pack(side="right", padx=(0, self.P(14)))
        tk.Label(box, text=caption, bg=uikit.CARD, fg=uikit.FG_FAINT, font=self.f_micro).pack(side="left")
        tk.Label(box, textvariable=variable, bg=uikit.CARD, fg=uikit.FG,
                 font=self.f_micro_b).pack(side="left", padx=(self.P(5), 0))

    def _build_status(self, parent: tk.Frame) -> None:
        card = uikit.Card(parent, self.ui, pad=(16, 14), stretch=True)
        card.pack(fill="both", expand=True, pady=(self.P(16), 0))
        head = self._card_head(card, "步骤", "自动记下每次换图、瞬移；点一行可以改方式")
        uikit.Button(head, "操作说明", self._show_guide, self.ui, variant="quiet", height=24,
                     min_width=0, pad=self.P(8), text_font=self.f_micro).pack(side="right")
        self._stat(head, "步骤", self.steps_var)
        self._stat(head, "耗时", self.elapsed_var)

        holder = tk.Frame(card.body, bg="#fafafa", highlightbackground=uikit.EDGE_SOFT,
                          highlightthickness=max(1, self.P(1)))
        holder.pack(fill="both", expand=True, pady=(self.P(10), 0))
        self.step_list = tk.Text(holder, bg="#fafafa", fg=uikit.FG, relief="flat", bd=0,
                                 highlightthickness=0, wrap="none", padx=self.P(10), pady=self.P(8),
                                 font=self.f_small, insertwidth=0, cursor="hand2", height=6)
        self.step_list.pack(fill="both", expand=True)
        self.step_list.tag_configure("no", foreground=uikit.FG_FAINT)
        self.step_list.tag_configure("where", foreground=uikit.FG)
        self.step_list.tag_configure("how", foreground=uikit.BLUE)
        self.step_list.tag_configure("guess", foreground=uikit.WARN)
        self.step_list.tag_configure("empty", foreground=uikit.FG_FAINT)
        self.step_list.tag_configure("hover", background=uikit.BLUE_SOFT)
        self.step_list.bind("<Button-1>", self._on_step_click)
        self.step_list.bind("<Motion>", self._on_step_hover)
        self.step_list.bind("<Leave>", lambda _e: self.step_list.tag_remove("hover", "1.0", "end"))
        self._render_steps()

        log_holder = tk.Frame(card.body, bg="#fafafa", highlightbackground=uikit.EDGE_SOFT,
                              highlightthickness=max(1, self.P(1)))
        log_holder.pack(fill="x", pady=(self.P(8), 0))
        self.log = tk.Text(log_holder, bg="#fafafa", fg=uikit.FG_DIM, relief="flat", bd=0,
                           highlightthickness=0, wrap="word", padx=self.P(10), pady=self.P(6),
                           font=self.f_micro, insertwidth=0, cursor="arrow", height=6)
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")
        self.log.tag_configure("sys", foreground=uikit.FG_FAINT)
        self.log.tag_configure("event", foreground=uikit.FG_DIM)
        self.log.tag_configure("note", foreground=uikit.BLUE)
        self.log.tag_configure("ok", foreground=uikit.OK)
        self.log.tag_configure("warn", foreground=uikit.WARN)
        self.log.tag_configure("error", foreground=uikit.BAD)
        self.log.tag_configure("guide", foreground=uikit.FG_DIM, font=self.f_small,
                               spacing1=self.P(2), spacing3=self.P(2))
        self.log.tag_configure("guide_title", foreground=uikit.FG, font=self.f_small_b,
                               spacing1=self.P(6), spacing3=self.P(4))
        self.status_card = card

    def _log(self, text: str, tag: str = "sys", stamp: bool = True) -> None:
        if not hasattr(self, "log"):
            return
        at_bottom = self.log.yview()[1] >= 0.999
        self.log.configure(state="normal")
        prefix = time.strftime("[%H:%M:%S] ") if stamp else ""
        self.log.insert("end", prefix + text + "\n", tag)
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 500:
            self.log.delete("1.0", "101.0")
        if at_bottom:
            self.log.see("end")
        self.log.configure(state="disabled")

    def _log_guide(self) -> None:
        steps = ("1  选查看器；“当前位置”显示出地图名和坐标就说明读得到。",
                 "2  开始采集后正常玩：走路、用旗、飞行符、传送圈、NPC。",
                 "3  每次用旗/飞行符/传送/土地，前后 15 秒内点右边对应的按钮。",
                 "4  换图、瞬移会自动出现在“步骤”里；记错了点那一行改。",
                 "5  遇到没见过的地图，点“给这张地图起名”。",
                 "6  结束采集后会自动生成报告；把录制目录发给整理的人。")
        for line in steps:
            self._log(line, "guide", stamp=False)

    def _show_guide(self) -> None:
        self._log("操作说明", "guide_title", stamp=False)
        self._log_guide()

    # ------------------------------------------------------------ 右栏：画面
    def _build_preview(self, parent: tk.Frame) -> None:
        card = uikit.Card(parent, self.ui, pad=(16, 14), stretch=True)
        card.pack(fill="both", expand=True)
        head = self._card_head(card, "采集画面")
        pill = uikit.Pill(head, self.ui, text_font=self.f_micro, height=24)
        pill.set("info", "只读", dot=False)
        pill.pack(side="right")
        self.stage = tk.Canvas(card.body, bg=uikit.STAGE, highlightthickness=0, bd=0,
                               height=self.P(160), cursor="arrow")
        self.stage.pack(fill="both", expand=True, pady=(self.P(10), self.P(8)))
        self.stage.bind("<Configure>", self._on_stage)
        tk.Label(card.body, text="800×600 原始画面 · 只读采集", bg=uikit.CARD, fg=uikit.FG_FAINT,
                 font=self.f_micro, anchor="w").pack(fill="x")
        tk.Label(card.body, text="蓝色虚线框 = 读地图名、坐标的位置，不能被挡住", bg=uikit.CARD, fg=uikit.BLUE,
                 font=self.f_micro, anchor="w").pack(fill="x", pady=(self.P(2), 0))
        self.preview_card = card

    def _on_stage(self, _event) -> None:
        self._render_preview()

    def _render_preview(self) -> None:
        stage = self.stage
        w, h = stage.winfo_width(), stage.winfo_height()
        if w < 20 or h < 20:
            return
        stage.delete("all")
        step = max(16, self.P(28))
        for x in range(step, w, step):
            stage.create_line(x, 0, x, h, fill=uikit.STAGE_GRID)
        for y in range(step, h, step):
            stage.create_line(0, y, w, y, fill=uikit.STAGE_GRID)
        arm = self.P(12)
        for x, y, dx, dy in ((8, 8, 1, 1), (w - 8, 8, -1, 1), (8, h - 8, 1, -1), (w - 8, h - 8, -1, -1)):
            stage.create_line(x, y, x + dx * arm, y, fill=uikit.STAGE_CORNER)
            stage.create_line(x, y, x, y + dy * arm, fill=uikit.STAGE_CORNER)
        if self._preview_frame is None:
            icon = uikit.canvas_icon("monitor", self.P(34), uikit.STAGE_TEXT)
            self._stage_icon = icon
            stage.create_image(w // 2, h // 2 - self.P(22), image=icon)
            title = "正在等待第一帧…" if self._recording else "选好查看器后显示只读画面"
            stage.create_text(w // 2, h // 2 + self.P(14), text=title, fill=uikit.FG_DIM,
                              font=self.f_small)
            stage.create_text(w // 2, h // 2 + self.P(38), text="不会发送鼠标键盘，也不会改动查看器",
                              fill=uikit.STAGE_TEXT, font=self.f_micro)
            return
        scale = min(w / G_W, h / G_H)
        tw, th = max(1, int(G_W * scale)), max(1, int(G_H * scale))
        x, y = (w - tw) // 2, (h - th) // 2
        img = Image.fromarray(self._preview_frame)
        if (tw, th) != (G_W, G_H):
            img = img.resize((tw, th), Image.LANCZOS)
        photo = ImageTk.PhotoImage(img)
        self._preview_photo = photo
        stage.create_image(x, y, image=photo, anchor="nw")
        stage.create_rectangle(x, y, x + tw - 1, y + th - 1, outline="#d9d9df")
        for x1, y1, x2, y2 in (hud_read.NAME_ROI, hud_read.COORD_ROI):
            stage.create_rectangle(x + int((x1 - 2) * scale), y + int((y1 - 2) * scale),
                                   x + int((x2 + 2) * scale), y + int((y2 + 2) * scale),
                                   outline=uikit.BLUE, dash=(4, 3), width=max(1, int(self.ui)))

    # ------------------------------------------------------------ 右栏：动作按钮
    def _build_actions(self, parent: tk.Frame) -> None:
        card = uikit.Card(parent, self.ui, pad=(16, 14))
        card.pack(side="bottom", fill="x", pady=(self.P(16), 0))
        head = self._card_head(card, "刚用了什么？点一下", "动作前后 15 秒内点都行，会顺便存一张当时的画面")
        self.note_pill = uikit.Pill(head, self.ui, text_font=self.f_micro, height=24)
        self.note_pill.set("idle", "录制中启用", dot=False)
        self.note_pill.pack(side="right")

        grid = tk.Frame(card.body, bg=uikit.CARD)
        grid.pack(fill="x", pady=(self.P(10), 0))
        self.action_buttons: list[uikit.Button] = []
        for i, (act, label) in enumerate(ACTION_BUTTONS):
            button = uikit.Button(grid, label, lambda a=act: self.send_action(a), self.ui,
                                  variant="secondary", height=34, min_width=0, pad=self.P(6),
                                  text_font=self.f_small_b)
            col = i % ACTION_COLS
            button.grid(row=i // ACTION_COLS, column=col, sticky="ew", padx=(0 if col == 0 else self.P(6), 0),
                        pady=(0, self.P(6)))
            self.action_buttons.append(button)
        for c in range(ACTION_COLS):
            grid.columnconfigure(c, weight=1, uniform="act")

        row = tk.Frame(card.body, bg=uikit.CARD)
        row.pack(fill="x", pady=(self.P(4), 0))
        self.send_btn = uikit.Button(row, "记录", self.send_note, self.ui, variant="primary",
                                     height=32, min_width=72, text_font=self.f_small_b)
        self.send_btn.pack(side="right", padx=(self.P(8), 0))
        self.note_entry = uikit.EntryBox(row, self.note_var, self.ui, height=32, text_font=self.f_small)
        self.note_entry.pack(side="left", fill="x", expand=True)
        self.note_entry.entry.bind("<Return>", lambda _e: self.send_note())
        tk.Label(card.body, text="其他想说的写这里，例如“和接引护卫对话，选第 2 项”。", bg=uikit.CARD,
                 fg=uikit.FG_FAINT, font=self.f_micro, anchor="w").pack(fill="x", pady=(self.P(6), 0))
        self.notes_card = card

    # ------------------------------------------------------------ 状态
    def _ready(self) -> bool:
        return (not self._recording and not self._finishing and not self._reporting
                and self._viewer_ok and self._size_ok)

    def _hud_ok(self) -> bool:
        return bool(self._hud and self._hud.get("scene") and self._hud.get("xy"))

    def _update_state(self) -> None:
        if not hasattr(self, "action_btn"):
            return
        report_enabled = bool(self._last_session) and not self._recording and not self._finishing \
            and not self._reporting
        lead_report = report_enabled and not self._reported
        if self._finishing:
            self.action_btn.set_text("正在收尾")
            self.action_btn.set_icon(None)
            self.action_btn.set_variant("secondary")
            self.action_btn.set_enabled(False)
        elif self._recording:
            self.action_btn.set_text("结束采集")
            self.action_btn.set_icon("stop")
            self.action_btn.set_variant("danger")
            self.action_btn.set_enabled(True)
        else:
            self.action_btn.set_text("开始采集")
            self.action_btn.set_icon("play")
            self.action_btn.set_variant("secondary" if lead_report else "primary")
            self.action_btn.set_enabled(self._ready())
        self.report_btn.set_enabled(report_enabled)
        self.report_btn.set_variant("primary" if lead_report else "secondary")
        if not (self._recording or self._finishing or self._reporting):
            self.pill.set(*self._idle_pill())

        editable = not self._recording and not self._finishing
        self.viewer_select.set_enabled(editable)
        self.refresh_btn.set_enabled(editable)
        self.restore_btn.set_enabled(editable and self._minimized and not self._restoring)
        self.route_picker.set_enabled(editable)
        self.out_entry.set_enabled(editable)
        self.choose_btn.set_enabled(editable)
        self.fps_select.set_enabled(editable)
        note_on = self._recording and not self._finishing
        self.note_entry.set_enabled(note_on)
        self.send_btn.set_enabled(note_on)
        for button in self.action_buttons:
            button.set_enabled(note_on)
        self._sync_name_button()
        if note_on:
            self.note_pill.set("ok", "可记录", dot=False)
        elif self._last_session:
            self.note_pill.set("idle", "已停止", dot=False)
        else:
            self.note_pill.set("idle", "录制中启用", dot=False)

        self.tip_label.configure(text=self._tip_text(), fg=self._tip_color())
        self.open_btn.set_enabled(True)

    def _idle_pill(self) -> tuple[str, str]:
        if self._last_session:
            if self._report_failed:
                return "bad", "报告生成失败"
            return "ok", "报告已生成" if self._reported else "可生成报告"
        if not self._viewers:
            return "idle", "等待查看器"
        if not self._viewer_ok:
            return "warn", "查看器已关闭"
        if self._minimized:
            return "warn", "查看器已最小化"
        if self._hidden:
            return "warn", "查看器已隐藏"
        if not self._size_ok:
            return "warn", "画面尺寸不符"
        if self._hud_ok():
            return "ok", "检查通过"
        return "warn", "读不到左上角"

    def _tip_text(self) -> str:
        if self._recording:
            return "采集进行中：工具只读抓图，不会操作副机。"
        if not self._viewers:
            return "先打开标准 TightVNC 查看器（画面 800×600）。"
        if not self._viewer_ok:
            return "选中的查看器已关闭，请点“刷新”。"
        if self._minimized:
            return "查看器最小化时 Windows 不渲染画面；点“还原”后再开始。"
        if self._hidden:
            return "查看器窗口被隐藏，请先把它显示出来。"
        if not self._size_ok:
            return "查看器画面必须为 800×600；请用标准方式重新打开。"
        if self._needs_admin():
            return "查看器以管理员权限运行；开始采集时会请求以管理员身份重启。"
        if not self._hud_ok():
            return "读不到左上角的地图名或坐标：小地图面板要打开，别被其他窗口挡住（也可以先开始，能读到就行）。"
        if self._hud and not self._hud.get("known"):
            return "这张地图图库里还没有，可以先点“给这张地图起名”。"
        return "检查通过，可以开始采集。"

    def _tip_color(self) -> str:
        if self._recording:
            return uikit.FG_FAINT
        admin_needed = self._needs_admin()
        if self._viewer_ok and self._size_ok and not admin_needed and self._hud_ok():
            return uikit.OK
        if self._viewer_ok and self._size_ok and not admin_needed:
            return uikit.WARN
        if self._viewers and (not (self._viewer_ok and self._size_ok) or admin_needed):
            return uikit.WARN
        return uikit.FG_FAINT

    # ------------------------------------------------------------ 查看器
    def refresh_viewers(self, silent: bool = False) -> None:
        if self._recording:
            return
        try:
            viewers = viewer_link.find_viewers(None)
        except Exception as exc:  # noqa: BLE001 界面不能因为一次扫描失败就停
            if not silent:
                self._log(f"扫描查看器失败：{exc}", "error")
            return
        mapping: dict[str, object] = {}
        values = []
        for index, viewer in enumerate(viewers, 1):
            try:
                title = winapi.title(viewer.top) or f"TightVNC 查看器 {index}"
            except Exception:
                title = f"TightVNC 查看器 {index}"
            key = f"{index} · {title}"
            values.append(key)
            mapping[key] = viewer
        previous = self.viewer_var.get()
        self._viewers = mapping
        self.viewer_select.set_values(values, "未找到查看器，点“刷新”")
        if values and previous not in mapping:
            self.viewer_var.set(values[0])
        if not values:
            self.viewer_var.set("")
            self._selected = None
            self._viewer_ok = self._size_ok = False
            self._minimized = self._hidden = False
            self._sync_restore_button()
            self.size_pill.set("idle", "未检测", dot=False)
        self._check_size()
        self._update_state()

    def _on_viewer_change(self) -> None:
        self._check_size()
        self._update_state()

    def _sync_restore_button(self) -> None:
        if not hasattr(self, "restore_btn"):
            return
        if self._minimized:
            if not self.restore_btn.winfo_manager():
                self.restore_btn.pack(side="right", padx=(self.P(6), 0), after=self.refresh_btn)
        elif self.restore_btn.winfo_manager():
            self.restore_btn.pack_forget()

    def _check_size(self) -> None:
        viewer = self._viewers.get(self.viewer_var.get())
        self._selected = viewer
        if viewer is None:
            self._viewer_ok = self._size_ok = False
            self._minimized = self._hidden = False
            self._sync_restore_button()
            self.size_pill.set("idle", "未检测", dot=False)
            return
        try:
            alive = viewer.alive()
        except Exception:
            alive = False
        if not alive:
            self._viewer_ok = self._size_ok = False
            self._minimized = self._hidden = False
            self._sync_restore_button()
            self.size_pill.set("bad", "查看器已关闭", dot=False)
            return
        self._viewer_ok = True
        try:
            self._minimized = winapi.is_iconic(viewer.top)
            self._hidden = bool(not self._minimized and (
                not winapi.is_visible(viewer.top) or not winapi.is_visible(viewer.child)))
        except Exception:
            self._minimized = self._hidden = False
        self._sync_restore_button()
        if self._minimized:
            self._size_ok = False
            self.size_pill.set("warn", "查看器已最小化", dot=False)
            return
        if self._hidden:
            self._size_ok = False
            self.size_pill.set("warn", "查看器已隐藏", dot=False)
            return
        try:
            size = winapi.client_size(viewer.child)
        except Exception:
            self._viewer_ok = self._size_ok = False
            self.size_pill.set("bad", "读取尺寸失败", dot=False)
            return
        if size == (G_W, G_H):
            self._size_ok = True
            self.size_pill.set("ok", "800×600", dot=False)
        else:
            self._size_ok = False
            self.size_pill.set("bad", f"{size[0]}×{size[1]} · 需 800×600", dot=False)

    def restore_viewer(self) -> None:
        viewer = self._viewers.get(self.viewer_var.get())
        if viewer is None or not self._minimized or self._restoring:
            return
        self._restoring = True
        self.restore_btn.set_enabled(False)
        self._log("正在还原查看器（不抢焦点）…", "sys")
        threading.Thread(target=self._restore_worker, args=(viewer,), daemon=True).start()
        self._update_state()

    def _restore_worker(self, viewer) -> None:
        try:
            ok = viewer_link.reveal(viewer, wait_s=1.5)
        except Exception:  # noqa: BLE001 还原失败交给界面提示，不能让线程挂掉
            ok = False
        self._ui_queue.put(("restored", ok))

    # ------------------------------------------------------------ 录制
    def on_action(self) -> None:
        if self._recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        if not self._ready() or self._selected is None:
            return
        if self._needs_admin():
            self._ask_admin_restart()
            return
        problem = self._capture_problem()
        if problem:
            messagebox.showwarning(
                "现在抓不到画面",
                f"{problem}。\n\n{self._capture_hint(problem)}",
                parent=self)
            self._log(f"开始前自检失败：{problem}", "warn")
            self._check_size()
            self._update_state()
            return
        out_root = self.out_var.get().strip()
        if not out_root:
            messagebox.showwarning("还没有保存位置", "请先选择保存位置。", parent=self)
            return
        try:
            os.makedirs(out_root, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("无法保存", f"保存位置不可用：\n{exc}", parent=self)
            return
        route = self.route_var.get().strip() or "未命名路线"
        safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", route)[:50]
        session_dir = os.path.join(out_root, time.strftime("%Y%m%d_%H%M%S_") + safe)
        try:
            os.makedirs(session_dir, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("无法保存", f"创建录制目录失败：\n{exc}", parent=self)
            return

        self._session_dir = session_dir
        self._error = None
        self._reported = False
        self._report_failed = False
        self._steps, self._actions, self._overrides = [], [], {}
        self._render_steps()
        self._elapsed = 0.0
        self._started_at = time.monotonic()
        self.elapsed_var.set("00:00")
        self._control_queue = queue.Queue()
        self._stop_event = threading.Event()
        self._preview_frame = None
        self._preview_photo = None
        self._recording = True
        self._finishing = False
        self._clear_log()
        self._log("开始采集 · 只读", "ok")
        self._log(f"素材目录：{session_dir}", "sys")
        self.pill.set("bad", "正在采集")
        self._update_state()
        self._render_preview()
        fps = self._fps()
        self._record_thread = threading.Thread(target=self._run_backend,
                                               args=(self._selected, session_dir, fps), daemon=True)
        self._record_thread.start()

    def _capture_problem(self) -> str | None:
        """开始前试抓一帧，避免录出一段全部失败的素材。"""
        viewer = self._selected
        if viewer is None:
            return "没有选择查看器"
        try:
            if winapi.is_iconic(viewer.top):
                return "查看器已最小化"
            if not winapi.is_visible(viewer.top) or not winapi.is_visible(viewer.child):
                return "查看器已隐藏"
            cap = wincap.capture_scaled(viewer.child, G_W, G_H)
        except Exception as exc:  # noqa: BLE001 自检失败只影响提示，不影响界面
            return f"{type(exc).__name__}: {exc}"
        if cap.get("ok") and not cap.get("blank"):
            return None
        if not cap.get("ok") and self._needs_admin():
            return "查看器以管理员权限运行，采集器权限不足"
        return cap.get("note") or "画面是纯色，可能还没连上副机"

    @staticmethod
    def _capture_hint(problem: str) -> str:
        if "管理员" in problem or "权限不足" in problem:
            return "请关闭采集器，右键选择“以管理员身份运行”后再开始。"
        if "最小化" in problem:
            return "请点查看器那一行的“还原”后再开始。"
        if "隐藏" in problem:
            return "请先把查看器显示出来。"
        if "纯色" in problem:
            return "请确认查看器已连上副机、画面正常后再开始。"
        return "请确认查看器画面正常后再开始。"

    def _needs_admin(self) -> bool:
        try:
            return elevation.needs_elevation(self._selected)
        except Exception:  # noqa: BLE001 权限探测失败按普通情况处理
            return False

    def _ask_admin_restart(self) -> None:
        self._log("查看器以管理员权限运行；Windows 会拒绝普通权限的采集器截图。", "warn")
        if not messagebox.askyesno(
                "需要管理员权限",
                "查看器以管理员权限运行，Windows 会拒绝普通权限的采集器截图（PrintWindow 失败）。\n\n"
                "是否以管理员身份重新启动采集器？重启后请重新选择查看器、路线和保存位置。",
                parent=self):
            self._log("未重启：请关闭采集器，右键“以管理员身份运行”后再采集。", "warn")
            self._update_state()
            return
        if elevation.relaunch_as_admin():
            self.destroy()
            return
        messagebox.showwarning("没有获得管理员权限",
                               "采集器仍以普通权限运行，截不到管理员权限的查看器。", parent=self)
        self._update_state()

    def _fps(self) -> float:
        match = re.match(r"(\d+)", self.fps_var.get())
        return float(match.group(1)) if match else 2.0

    def _add_custom_map(self, name: str) -> bool:
        if not map_names.add_custom(name):
            return False
        self._log(f"已添加自定义地图：{name}", "ok")
        return True

    def _run_backend(self, viewer, out: str, fps: float) -> None:
        try:
            recorder.run(viewer, out, fps=fps, periodic_s=10.0,
                         control_queue=self._control_queue, stop_event=self._stop_event, book=self._book,
                         on_message=lambda message: self._ui_queue.put(("log", message)),
                         on_event=lambda event: self._ui_queue.put(("event", event)),
                         on_frame=self._frame_in,
                         on_hud=lambda hud: self._ui_queue.put(("hud", hud)))
        except Exception as exc:  # noqa: BLE001 录制线程的任何异常都要回到界面
            self._ui_queue.put(("fail", f"{type(exc).__name__}: {exc}"))
        finally:
            self._ui_queue.put(("done", None))

    def _frame_in(self, frame, t: float) -> None:
        while True:
            try:
                self._preview_q.put_nowait((frame, t))
                return
            except queue.Full:
                try:
                    self._preview_q.get_nowait()
                except queue.Empty:
                    return

    def stop_recording(self) -> None:
        if not self._recording or self._finishing:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        self._finishing = True
        self.pill.set("busy", "正在收尾")
        self._log("正在收尾，写入最后几帧…", "sys")
        self._update_state()

    def _finish_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self._finishing = False
        self._elapsed = time.monotonic() - self._started_at
        self.elapsed_var.set(self._fmt(self._elapsed))
        self.pill.set_dot_color(None)
        if self._error or not self._session_dir:
            message = self._error or "没有创建录制目录"
            self.pill.set("bad", "采集失败")
            self._log(f"采集失败：{message}", "error")
        else:
            self._last_session = self._session_dir
            self.pill.set("ok", "已结束 · 可生成报告")
            self._log(f"采集结束，共 {len(self._steps)} 个步骤，正在自动生成报告；记错的步骤点一下就能改。", "ok")
            self._log(f"素材目录：{self._last_session}", "sys")
        self._control_queue = None
        self._stop_event = None
        self._update_state()
        if self._last_session and not self._error and not self._closing:
            self.build_report()

    def send_note(self) -> None:
        if not self._recording or self._finishing or self._control_queue is None:
            return
        note = self.note_var.get().strip()
        if not note:
            return
        self._control_queue.put({"kind": "note", "text": note})
        self.note_var.set("")
        self.note_entry.focus_set()

    def send_action(self, action: str) -> None:
        if not self._recording or self._finishing or self._control_queue is None:
            return
        self._control_queue.put({"kind": "action", "action": action})
        self._actions.append({"t": time.monotonic() - self._started_at, "action": action})
        self._render_steps()

    # ------------------------------------------------------------ 地图名
    def _sync_name_button(self) -> None:
        if not hasattr(self, "name_btn"):
            return
        want = bool(self._hud and self._hud.get("scene") and not self._hud.get("known")
                    and not self._finishing and (self._recording or self._hud_mask is not None))
        if want and not self.name_btn.winfo_manager():
            self.name_btn.pack(side="right")
        elif not want and self.name_btn.winfo_manager():
            self.name_btn.pack_forget()

    def _show_hud(self, hud: dict | None) -> None:
        self._hud = hud
        if not hud or not hud.get("scene"):
            self.hud_scene.configure(text="读不到", fg=uikit.FG_FAINT)
            self.hud_xy.configure(text="")
        else:
            self.hud_scene.configure(text=hud["scene"], fg=uikit.FG if hud.get("known") else uikit.WARN)
            xy = hud.get("xy")
            self.hud_xy.configure(text=f"X {xy[0]}  Y {xy[1]}" if xy else "坐标读不到")
        self._sync_name_button()

    def _probe_hud(self) -> None:
        """没在录制时也每秒读一次左上角，让用户开始前就知道读不读得到。"""
        viewer = self._selected
        if viewer is None or not self._size_ok:
            if self._hud is not None or self.hud_scene.cget("text") != "—":
                self._hud, self._hud_mask = None, None
                self.hud_scene.configure(text="—", fg=uikit.FG_FAINT)
                self.hud_xy.configure(text="")
                self._sync_name_button()
            return
        try:
            cap = wincap.capture_scaled(viewer.child, G_W, G_H)
        except Exception:  # noqa: BLE001 探测失败只影响读数显示
            return
        if not cap.get("ok") or cap.get("blank"):
            self._hud_mask = None
            self._show_hud(None)
            return
        import numpy as np
        frame = np.frombuffer(cap["rgb"], dtype=np.uint8).reshape(G_H, G_W, 3)
        r = hud_read.read_hud(frame, self._book)
        self._hud_mask = r.mask if (r.name and not r.known) else None
        self._show_hud({"scene": r.name, "known": r.known, "xy": r.xy})
        self._preview_frame = frame
        self._render_preview()

    def ask_scene_name(self) -> None:
        hud = self._hud
        if not hud or not hud.get("scene") or hud.get("known"):
            return
        old = hud["scene"]
        name = simpledialog.askstring(
            "给这张地图起名",
            f"左上角这张地图暂记为“{old}”。\n请照着游戏里左上角的字输入地图名（例如 普陀山）：",
            parent=self)
        name = " ".join((name or "").split())
        if not name:
            return
        if self._recording and self._control_queue is not None:
            self._control_queue.put({"kind": "name", "name": name})
            return
        if self._hud_mask is None:
            self._log("现在读不到地图名，没有起名。", "warn")
            return
        try:
            self._book.add(name, self._hud_mask, persist=True)
        except (ValueError, OSError) as exc:
            self._log(f"起名失败：{exc}", "error")
            return
        self._hud_mask = None
        self._log(f"已记住地图名：{name}", "ok")
        self._show_hud(dict(hud, scene=name, known=True))
        self._update_state()

    # ------------------------------------------------------------ 步骤列表
    def _display_steps(self) -> list[dict]:
        steps = [dict(s) for s in self._steps]
        rec_steps.assign_actions(steps, [dict(a) for a in self._actions])
        for s in steps:
            if s["no"] in self._overrides:
                s["how"], s["how_src"] = self._overrides[s["no"]], "用户改"
        return steps

    def _render_steps(self) -> None:
        box = self.step_list
        box.configure(state="normal")
        box.delete("1.0", "end")
        steps = self._display_steps()
        if not steps:
            hint = ("正在采集…换图或用旗瞬移后，这里会自动出现一行。" if self._recording
                    else "开始采集后，每次换图、用旗/飞行符瞬移都会自动记成一步。")
            box.insert("end", hint, "empty")
        for s in steps:
            def xy(v):
                return f"({v[0]},{v[1]})" if v else "(?)"
            box.insert("end", f"#{s['no']:<3d}{self._fmt(s['t'])}  ", "no")
            if s["kind"] == "jump":
                where = f"{s['from']} {xy(s.get('from_xy'))} → {xy(s.get('to_xy'))}"
            else:
                where = f"{s['from']} {xy(s.get('from_xy'))} → {s['to']} {xy(s.get('to_xy'))}"
            box.insert("end", where + "   ", "where")
            guess = s["how_src"] == "推测"
            box.insert("end", SHORT_HOW.get(s["how"], s["how"]) + ("（没点按钮）" if guess else ""),
                       "guess" if guess else "how")
            box.insert("end", "\n")
        box.configure(state="disabled")
        box.see("end")
        self.steps_var.set(str(len(steps)))

    def _step_at(self, event) -> int | None:
        line = int(self.step_list.index(f"@{event.x},{event.y}").split(".")[0])
        if 1 <= line <= len(self._steps):
            return line
        return None

    def _on_step_hover(self, event) -> None:
        self.step_list.tag_remove("hover", "1.0", "end")
        line = self._step_at(event)
        if line is not None:
            self.step_list.tag_add("hover", f"{line}.0", f"{line}.end+1c")

    def _on_step_click(self, event) -> str:
        line = self._step_at(event)
        if line is None or self._finishing:
            return "break"
        no = self._steps[line - 1]["no"]
        menu = tk.Menu(self, tearoff=0, font=self.f_small)
        menu.add_command(label=f"第 {no} 步是怎么过去的？", state="disabled")
        menu.add_separator()
        for act, label in rec_steps.ACTIONS.items():
            menu.add_command(label=label, command=lambda a=act: self._set_how(no, a))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _set_how(self, no: int, action: str) -> None:
        self._overrides[no] = action
        if self._recording and self._control_queue is not None:
            self._control_queue.put({"kind": "how", "no": no, "action": action})
        elif self._last_session:
            import json
            try:
                with open(os.path.join(self._last_session, "events.jsonl"), "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"t": None, "type": "how", "no": no, "action": action},
                                        ensure_ascii=False) + "\n")
            except OSError as exc:
                self._log(f"保存修改失败：{exc}", "error")
                return
            self._reported = False
            self._log(f"已改第 {no} 步：{rec_steps.ACTIONS[action]}", "note")
            self._update_state()
            if self._reporting:
                self._report_again = True
            else:
                self.build_report()
        self._render_steps()

    # ------------------------------------------------------------ 报告与目录
    def build_report(self) -> None:
        if not self._last_session or self._recording or self._finishing or self._reporting:
            return
        self._reporting = True
        self.pill.set("busy", "正在整理报告")
        self._log("正在生成 report/report.md 与 draft_links.yaml…", "sys")
        self._update_state()
        threading.Thread(target=self._report_worker, args=(self._last_session,), daemon=True).start()

    def _report_worker(self, path: str) -> None:
        try:
            report = recorder_report.build(path, self._book)
            self._ui_queue.put(("report", report))
        except Exception as exc:  # noqa: BLE001
            self._ui_queue.put(("report_err", f"{type(exc).__name__}: {exc}"))

    def open_output(self) -> None:
        path = self._session_dir or self._last_session or self.out_var.get().strip()
        if not path:
            return
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)  # noqa: S606  Windows 文件管理器
        except OSError as exc:
            messagebox.showerror("打开目录失败", f"{path}\n{exc}", parent=self)

    # ------------------------------------------------------------ 队列
    def _pump(self) -> None:
        while True:
            try:
                kind, payload = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._handle_message(str(payload))
            elif kind == "event":
                self._handle_event(payload)
            elif kind == "hud":
                self._show_hud(payload)
            elif kind == "done":
                self._finish_recording()
            elif kind == "fail":
                self._error = str(payload)
            elif kind == "report":
                self._reporting = False
                self._reported = True
                self._report_failed = False
                self.pill.set("ok", "报告已生成")
                self._log(f"报告已生成：{payload}", "ok")
                self._update_state()
                if self._report_again:
                    self._report_again = False
                    self.build_report()
            elif kind == "report_err":
                self._reporting = False
                self._report_failed = True
                self.pill.set("bad", "报告生成失败")
                self._log(f"报告生成失败：{payload}", "error")
                self._update_state()
            elif kind == "restored":
                self._restoring = False
                self._check_size()
                if bool(payload) and not self._minimized and not self._hidden:
                    self._log("查看器已还原，可以开始采集。", "ok")
                else:
                    self._log("没能自动还原查看器，请手动还原或重新连接。", "warn")
                self._update_state()
        latest = None
        while True:
            try:
                latest = self._preview_q.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            self._preview_frame = latest[0]
            self._render_preview()
        self.after(120, self._pump)

    def _handle_message(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if text.startswith("录制结束，共") or text.startswith("开始录制") or text.startswith("步骤"):
            return  # 步骤已经在步骤列表里显示
        if text.startswith(("已记备注", "已记动作", "已改步骤", "已记住地图名")):
            self._log(text, "note")
        elif text.startswith("遇到图库里没有的地图") or text.startswith("起名失败") or "读不到左上角" in text:
            self._log(text, "warn")
        elif "画面已恢复" in text:
            self._log(text, "ok")
        elif ("抓不到" in text or "警告" in text or "最小化" in text
              or "隐藏" in text or "暂停" in text):
            self._log(text, "warn")
        else:
            self._log(text, "sys")

    def _handle_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "step":
            self._steps.append(event)
            self._render_steps()
        elif kind == "action" and self._actions:
            # 用录制线程的时间替换按下按钮时界面估的时间，和步骤时间对得上
            pending = [a for a in self._actions if "synced" not in a and a["action"] == event.get("action")]
            if pending:
                pending[0]["t"] = event.get("t", pending[0]["t"])
                pending[0]["synced"] = True
                self._render_steps()
        elif kind == "name":
            old, new = event.get("unknown"), event.get("name")
            for s in self._steps:
                for k in ("from", "to"):
                    if s.get(k) == old:
                        s[k] = new
            self._render_steps()
            if self._hud and self._hud.get("scene") == old:
                self._show_hud(dict(self._hud, scene=new, known=True))

    # ------------------------------------------------------------ 时钟
    @staticmethod
    def _fmt(seconds: float) -> str:
        seconds = max(0, int(seconds))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def _tick(self) -> None:
        try:
            self._tick_once()
        finally:
            self.after(500, self._tick)

    def _tick_once(self) -> None:
        if self._recording:
            self.elapsed_var.set(self._fmt(time.monotonic() - self._started_at))
            return
        if self._finishing:
            self.elapsed_var.set(self._fmt(time.monotonic() - self._started_at))
            return
        self._tick_n += 1
        if self._tick_n % 8 == 0 and self.viewer_select._top is None:
            self.refresh_viewers(silent=True)
        elif self._viewers and self._tick_n % 2 == 0:
            self._check_size()
            if not self._reporting:
                self._probe_hud()
            self._update_state()

    def _blink(self) -> None:
        if self._recording and not self._finishing:
            self._blink_on = not self._blink_on
            self.pill.set_dot_color("#efa9a5" if self._blink_on else uikit.BAD)
        elif self.pill._dot_override is not None:
            self.pill.set_dot_color(None)
        self.after(600, self._blink)

    # ------------------------------------------------------------ 其他
    def _choose_out(self) -> None:
        path = filedialog.askdirectory(initialdir=self.out_var.get() or runtime_dir(),
                                       title="选择录制保存位置", parent=self)
        if path:
            self.out_var.set(path)
            self._update_state()

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _close(self) -> None:
        if self._recording and not self._closing:
            if not messagebox.askyesno("正在采集", "结束并保存当前录制后退出？", parent=self):
                return
            self._closing = True
            self.stop_recording()
            self.after(250, self._close_when_done)
            return
        self.destroy()

    def _close_when_done(self) -> None:
        if self._recording or self._finishing:
            self.after(200, self._close_when_done)
            return
        self.destroy()


def main() -> int:
    winapi.set_dpi_aware()
    app = RecorderApp()
    if "--smoke" in sys.argv:
        app.update_idletasks()
        app.update()
        app.destroy()
        return 0
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
