"""panel.py

简洁面板：两张卡片（大号百分比 + 进度条 + 数据来源），颜色随电量变化。

* 刷新完全靠 tkinter 的 after() 定时器：每秒只读一次内存快照，值没变就不重绘，
  不做任何轮询式设备访问，空闲时 CPU 接近 0。
* 窗口关闭（X）默认收进托盘而非退出；「退出」按钮才真正结束进程。
"""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk

BG = "#f4f5f7"
CARD_BG = "#ffffff"
TITLE_BG = "#2b3440"
TXT = "#1f2430"
SUBTLE = "#7b8494"
TRACK = "#e6e9ee"

GREEN = "#22a06b"
YELLOW = "#d9a400"
RED = "#d64545"
UNKNOWN = "#9aa3b0"


def level_color(percent) -> str:
    if percent is None:
        return UNKNOWN
    if percent >= 60:
        return GREEN
    if percent >= 30:
        return YELLOW
    return RED


def level_text(percent) -> str:
    if percent is None:
        return "未知"
    if percent >= 60:
        return "充足"
    if percent >= 30:
        return "中等"
    return "偏低"


def fmt_time(ts) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts))
    except Exception:
        return "--:--:--"


def _round_rect(canvas, x0, y0, x1, y1, r, **kw):
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    pts = [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0, x0 + r, y0,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


class BatteryPanel:
    """简洁面板。需要 service（BatteryService）与一组 hooks 回调。"""

    def __init__(self, service, hooks, master=None):
        self.service = service
        self.hooks = hooks
        self.master = master
        self._own_root = master is None
        self.root = None
        self._cards = {}
        self._last = {}
        self._job = None
        self._visible = False

    # ------------------------------------------------------------ 构建界面
    def build(self):
        if self.root is not None:
            return
        self.root = tk.Tk() if self._own_root else tk.Toplevel(self.master)
        self.root.title("无线外设电量 · AK992 / AJ179")
        self.root.configure(bg=BG)
        self.root.minsize(430, 300)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        try:
            self.root.withdraw()
        except Exception:
            pass

        outer = tk.Frame(self.root, bg=BG, padx=14, pady=12)
        outer.pack(fill="both", expand=True)

        for key in ("mouse", "keyboard"):
            spec = self.service.protocols.get(key)
            if not spec:
                continue
            self._cards[key] = self._make_card(outer, key, spec)

        # -------- 控制区 --------
        bar = tk.Frame(outer, bg=BG, pady=4)
        bar.pack(fill="x", pady=(10, 0))

        tk.Label(bar, text="刷新间隔", bg=BG, fg=SUBTLE,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")

        self.interval_var = tk.StringVar(value=str(self.hooks["get_interval"]()))
        combo = ttk.Combobox(bar, textvariable=self.interval_var, width=7, state="readonly",
                             values=["30秒", "60秒", "2分钟", "5分钟", "10分钟", "30分钟"])
        combo.pack(side="left", padx=(6, 8))
        combo.set(self._interval_label(self.hooks["get_interval"]()))
        combo.bind("<<ComboboxSelected>>", self._on_interval)

        self.autostart_var = tk.BooleanVar(value=bool(self.hooks["is_autostart"]()))
        chk = tk.Checkbutton(bar, text="开机自启", variable=self.autostart_var,
                             bg=BG, fg=TXT, activebackground=BG, selectcolor="#ffffff",
                             font=("Microsoft YaHei UI", 9), command=self._on_autostart)
        chk.pack(side="left")

        tk.Button(bar, text="退出", command=self._on_quit, relief="flat",
                  bg=BG, fg=SUBTLE, activebackground=BG,
                  font=("Microsoft YaHei UI", 9)).pack(side="right", padx=(6, 0))
        tk.Button(bar, text="收进托盘", command=self.hide, relief="flat",
                  bg=BG, fg=SUBTLE, activebackground=BG,
                  font=("Microsoft YaHei UI", 9)).pack(side="right", padx=(6, 0))
        tk.Button(bar, text="立即刷新", command=self.hooks["refresh_now"], relief="flat",
                  bg=BG, fg="#2f6fbf", activebackground=BG,
                  font=("Microsoft YaHei UI", 9)).pack(side="right")

        self.status = tk.Label(outer, text="", bg=BG, fg=SUBTLE, anchor="w",
                               font=("Microsoft YaHei UI", 8))
        self.status.pack(fill="x", pady=(6, 0))

        self.root.geometry("+%d+%d" % (self._corner_x(), self._corner_y()))

    def _corner_x(self):
        try:
            return max(0, self.root.winfo_screenwidth() - 470)
        except Exception:
            return 100

    def _corner_y(self):
        return 120

    def _make_card(self, parent, key, spec):
        card = tk.Frame(parent, bg=CARD_BG, padx=14, pady=11,
                        highlightthickness=1, highlightbackground="#dfe3ea")
        card.pack(fill="x", pady=(0, 10))

        top = tk.Frame(card, bg=CARD_BG)
        top.pack(fill="x")
        tk.Label(top, text=spec["name"], bg=CARD_BG, fg=TXT, anchor="w",
                 font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        state = tk.Label(top, text="", bg=CARD_BG, fg=SUBTLE,
                         font=("Microsoft YaHei UI", 9))
        state.pack(side="right")

        mid = tk.Frame(card, bg=CARD_BG)
        mid.pack(fill="x", pady=(2, 6))
        pct = tk.Label(mid, text="--", bg=CARD_BG, fg=UNKNOWN, anchor="w",
                       font=("Microsoft YaHei UI", 26, "bold"))
        pct.pack(side="left")
        unit = tk.Label(mid, text="%", bg=CARD_BG, fg=UNKNOWN,
                        font=("Microsoft YaHei UI", 13))
        unit.pack(side="left", anchor="s", pady=(0, 6))
        upd = tk.Label(mid, text="", bg=CARD_BG, fg=SUBTLE, anchor="e",
                       font=("Microsoft YaHei UI", 8))
        upd.pack(side="right")

        cv = tk.Canvas(card, height=10, bg=CARD_BG, highlightthickness=0)
        cv.pack(fill="x")
        src = tk.Label(card, text="", bg=CARD_BG, fg=SUBTLE, anchor="w",
                       justify="left", wraplength=400,
                       font=("Microsoft YaHei UI", 8))
        src.pack(fill="x", pady=(6, 0))

        return {"pct": pct, "unit": unit, "state": state, "canvas": cv,
                "src": src, "upd": upd, "spec": spec}

    # ------------------------------------------------------------ 数据刷新
    def apply(self, snapshot):
        if self.root is None:
            return
        for key, c in self._cards.items():
            rd = snapshot.get(key)
            sig = (round(rd.percent, 1) if rd and rd.ok and rd.percent is not None else None,
                   rd.ok if rd else False, round(rd.ts, 1) if rd else 0)
            if self._last.get(key) == sig:
                continue
            self._last[key] = sig
            self._render(c, rd)

    def _render(self, c, rd):
        pct = rd.percent if (rd and rd.ok) else None
        color = level_color(pct)
        c["pct"].configure(text=str(pct) if pct is not None else "--", fg=color)
        c["unit"].configure(fg=color)

        if rd is None:
            state, state_color = "未读取", SUBTLE
        elif not rd.ok:
            state, state_color = "读取失败", RED
        else:
            if rd.charging is True:
                state, state_color = "充电中", GREEN
            elif rd.charging is False:
                state, state_color = "放电中", SUBTLE
            else:
                state, state_color = level_text(pct), color
        c["state"].configure(text=state, fg=state_color)

        c["upd"].configure(text=("更新于 " + fmt_time(rd.ts)) if rd else "")
        self._draw_bar(c["canvas"], pct, color)
        c["src"].configure(text=self._source_text(c["spec"], rd))

    def _source_text(self, spec, rd) -> str:
        ps = spec.get("pids") or [spec.get("pid")]
        pidtxt = "/".join(f"{p:04X}" for p in ps if p is not None)
        if spec.get("method") == "query":
            head = f"HID 查询：{spec['vid']:04X}:{pidtxt} · 命令 0x{spec['request_cmd']:02X} · 取第 {spec['percent_offset']} 字节"
        else:
            pre = " ".join(f"{b:02X}" for b in (spec.get("match_prefix") or []))
            head = f"HID 查询：{spec['vid']:04X}:{pidtxt} · 报文 {pre} · 取第 {spec['percent_offset']} 字节"
        if rd is not None and not rd.ok and rd.error:
            return head + "　\n状态：" + rd.error
        if rd is not None and rd.raw:
            shown = " ".join(f"{b:02X}" for b in rd.raw[:12])
            return head + f"　\n原始报文：{shown} …"
        return head

    def _draw_bar(self, cv, pct, color):
        cv.delete("all")
        w = cv.winfo_width()
        if w <= 1:
            cv.update_idletasks()
            w = cv.winfo_width()
        if w <= 1:
            w = 380
        h = 10
        _round_rect(cv, 0, 0, w, h, h / 2, fill=TRACK, outline="")
        frac = 0.0 if pct is None else max(0.0, min(1.0, pct / 100.0))
        if frac > 0:
            fw = max(h, w * frac)
            _round_rect(cv, 0, 0, fw, h, h / 2, fill=color, outline="")

    # ------------------------------------------------------------ 定时器
    def _tick(self):
        self.apply(self.service.snapshot())
        try:
            self.status.configure(
                text=f"已刷新 {self.service.refresh_count} 次 · 间隔 {self._interval_label(self.hooks['get_interval']())}")
        except Exception:
            pass
        if self._visible:
            self._job = self.root.after(1000, self._tick)

    # ------------------------------------------------------------ 显隐
    def show(self):
        if self.root is None:
            self.build()
        self._visible = True
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(200, lambda: self.root.attributes("-topmost", False))
            self.root.focus_force()
        except Exception:
            pass
        self._last.clear()
        self.apply(self.service.snapshot())
        if self._job:
            try:
                self.root.after_cancel(self._job)
            except Exception:
                pass
        self._job = self.root.after(1000, self._tick)

    def hide(self):
        if self.root is None:
            return
        self._visible = False
        if self._job:
            try:
                self.root.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        try:
            self.root.withdraw()
        except Exception:
            pass

    def destroy(self):
        self._visible = False
        if self._job:
            try:
                self.root.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        if self.root is not None:
            if self._own_root:
                try:
                    self.root.quit()
                except Exception:
                    pass
            try:
                self.root.destroy()
            except Exception:
                pass
            self.root = None

    # ------------------------------------------------------------ 交互
    def _interval_label(self, seconds: int) -> str:
        if seconds < 120:
            return f"{int(seconds)}秒"
        if seconds % 60 == 0:
            m = seconds // 60
            return f"{m}分钟" if m < 60 else "30分钟"
        return f"{seconds}秒"

    def _on_interval(self, _evt=None):
        text = self.interval_var.get()
        table = {"30秒": 30, "60秒": 60, "2分钟": 120, "5分钟": 300,
                 "10分钟": 600, "30分钟": 1800}
        secs = table.get(text.strip(), 60)
        self.hooks["set_interval"](secs)

    def _on_autostart(self):
        want = bool(self.autostart_var.get())
        ok, msg = self.hooks["set_autostart"](want)
        if not ok:
            self.autostart_var.set(bool(self.hooks["is_autostart"]()))
            try:
                self.status.configure(text=f"开机自启设置失败：{msg}", fg=RED)
            except Exception:
                pass
        else:
            try:
                state = "已启用" if self.hooks["is_autostart"]() else "已禁用"
                self.status.configure(text=f"开机自启{state}（启动文件夹中「电量托盘」）", fg=SUBTLE)
            except Exception:
                pass

    def _on_quit(self):
        self.hooks["on_quit"]()
