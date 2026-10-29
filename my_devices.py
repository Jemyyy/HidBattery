#!/usr/bin/env python3
"""my_devices.py

无线外设电量小工具（黑爵 AK992 键盘 / 黑爵 AJ179 鼠标）。
纯 Python 标准库 + ctypes，不依赖任何第三方包。

用法：
    python my_devices.py --once            只读一次并打印两行电量
    python my_devices.py --tray            托盘常驻（通知区域两个数字图标）
    python my_devices.py --panel           只开简洁面板
    python my_devices.py --debugui         完整调试版（枚举/转储/试发命令/标定）
    python my_devices.py --setup           创建「启动」文件夹快捷方式 + 桌面链接
    python my_devices.py --tray --debug     托盘 + 控制台调试输出
    python my_devices.py --tray --interval 120   指定刷新间隔（30~1800 秒）
    python my_devices.py --tray --exit-after 20  冒烟测试：20 秒后自动优雅退出

设计要点：
    * 刷新由托盘隐藏窗口的 SetTimer 驱动（默认 60 秒），空闲时不轮询、不 sleep 循环。
    * 设备读取全部在后台线程，界面与托盘消息循环都不被阻塞。
    * 不写任何日志文件；只有 --debug 时才有控制台输出。
    * 退出走 Shell_NotifyIcon(NIM_DELETE) + 销毁隐藏窗口，不留僵尸进程和残留图标。
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time

import devices as D
import sysinteg as S
import wintray as W

# 自定义窗口消息（给托盘隐藏窗口用）
WM_DATA_READY = 0x8000 + 10
WM_SET_INTERVAL = 0x8000 + 11
WM_QUIT = 0x8000 + 12
WM_SET_KEEPALIVE = 0x8000 + 13

TIMER_REFRESH = 1
TIMER_KEEPALIVE = 2
ICON_MOUSE = 1
ICON_KEYBOARD = 2

# 保活周期：键盘 2.4G 链路空闲会休眠并拒绝应答查询，必须像官方驱动那样
# （每 ~4.9 秒写一次查询帧）持续喂它。一次只写 64 字节，CPU 开销可忽略。
KEEPALIVE_MS = 5000

ICON_IDS = ((ICON_MOUSE, "mouse"), (ICON_KEYBOARD, "keyboard"))

MIN_INTERVAL = 30
MAX_INTERVAL = 1800

# 单实例保护：命名互斥体 + 已运行实例的窗口，避免启动两次出现两对托盘图标
MUTEX_NAME = "Local\\HidBatteryTray"
TRAY_WND_CLASS = "HidBatteryTrayWnd"
TRAY_WND_TITLE = "HidBattery"
SHOW_PANEL_MSG_NAME = "HidBatteryShowPanel"

ICON_UNKNOWN_BG = (0x9A, 0xA3, 0xB0)
BG_BY_LEVEL = {
    "green": (0x22, 0xA0, 0x6B),
    "yellow": (0xD9, 0xA4, 0x00),
    "red": (0xD6, 0x45, 0x45),
    "unknown": ICON_UNKNOWN_BG,
}


def clamp_interval(seconds) -> int:
    try:
        v = int(float(seconds))
    except (TypeError, ValueError):
        return 60
    return max(MIN_INTERVAL, min(MAX_INTERVAL, v))


def level_key(percent) -> str:
    if percent is None:
        return "unknown"
    if percent >= 60:
        return "green"
    if percent >= 30:
        return "yellow"
    return "red"


def tip_text(spec, rd) -> str:
    """悬停提示：设备名：电量（更新时间）"""
    name = spec.get("short", "设备")
    when = time.strftime("%H:%M:%S", time.localtime(rd.ts if rd else time.time()))
    if rd and rd.ok and rd.percent is not None:
        body = f"{rd.percent}%"
    else:
        body = "未读到"
    return f"{name}：{body}（{when}）"


# ==========================================================================
# 只读一次
# ==========================================================================
def run_once(debug: bool) -> int:
    protos = D.load_protocols()
    failed = 0
    for key in D.ORDER:
        spec = protos.get(key)
        if not spec:
            continue
        rd = D.read_once(spec)
        if rd.ok and rd.percent is not None:
            print(f"{spec['short']} {rd.percent}%")
        else:
            failed += 1
            print(f"{spec['short']} --（{rd.error or '未知原因'}）")
        if debug and rd.raw:
            print(f"    raw: " + " ".join(f"{b:02X}" for b in rd.raw[:24]))
    return 1 if failed == len(D.ORDER) else 0


# ==========================================================================
# 托盘常驻
# ==========================================================================
class TrayApp:
    def __init__(self, interval_s: int, debug: bool = False, exit_after: float = 0.0):
        W.enable_dpi_awareness()
        self.protocols = D.load_protocols()
        self.service = D.BatteryService(interval_s, self.protocols,
                                        debug=debug, auto_loop=False)
        self.host = W.TrayHost("HidBatteryTrayWnd")
        self.show_panel_msg = W.register_message(SHOW_PANEL_MSG_NAME)
        self.cmds: queue.Queue = queue.Queue()
        self.root = None
        self.panel = None
        self.debugui = None
        self.quitting = False
        self.exit_after = exit_after
        self._hooks = {
            "is_autostart": S.is_autostart_enabled,
            "set_autostart": self._set_autostart,
            "get_interval": lambda: int(self.service.interval_s),
            "set_interval": self._set_interval,
            "refresh_now": self._refresh_now,
            "on_quit": lambda: self.cmds.put(("quit", None)),
        }

    # ------------------------------------------------------------ 托盘回调
    def _refresh_now(self):
        self.service.refresh_async(lambda: self.host.post(WM_DATA_READY))

    def _on_left_click(self, icon_id):
        # 左键：立刻刷新一次（后台线程做读取，不阻塞托盘消息循环）
        self._refresh_now()

    def _on_right_click(self, icon_id):
        # 右键：直接打开/前置简洁面板（已打开则复用同一个窗口）
        self.cmds.put(("panel", None))

    def _on_timer(self, timer_id):
        if timer_id == TIMER_REFRESH:
            self._refresh_now()
        elif timer_id == TIMER_KEEPALIVE:
            # 只写不读：把键盘 2.4G 链路喂在唤醒态，避免它空闲休眠
            self.service.keepalive_async()

    def _on_custom(self, msg, wparam, lparam):
        if msg == WM_DATA_READY:
            self._update_icons()
        elif msg == WM_SET_INTERVAL:
            self.host.set_timer(TIMER_REFRESH, int(wparam) * 1000)
        elif msg == WM_SET_KEEPALIVE:
            self.host.set_timer(TIMER_KEEPALIVE, int(wparam))
        elif msg == WM_QUIT:
            self.host.stop()
        elif self.show_panel_msg and msg == self.show_panel_msg:
            # 另一个实例启动时想开面板：复用它自己的窗口
            self.cmds.put(("panel", None))

    def _update_icons(self):
        snap = self.service.snapshot()
        for icon_id, key in ICON_IDS:
            spec = self.protocols.get(key, {})
            rd = snap.get(key)
            pct = rd.percent if (rd and rd.ok) else None
            text = str(pct) if pct is not None else "--"
            bg = BG_BY_LEVEL[level_key(pct)]
            icon = W.text_icon(text, (255, 255, 255), bg)
            self.host.update_icon(icon_id, icon, tip_text(spec, rd))

    # ------------------------------------------------------------ 面板钩子
    def _set_autostart(self, want: bool):
        if want:
            ok, msg = S.enable_autostart()
        else:
            ok, msg = S.disable_autostart()
        if self.service.debug:
            print("[自启]", want, ok, msg, flush=True)
        return ok, msg

    def _set_interval(self, seconds: int):
        secs = clamp_interval(seconds)
        self.service.set_interval(secs)
        try:
            D.save_settings({"interval": secs})
        except OSError:
            pass
        # SetTimer 必须在拥有窗口的那个线程调用，故转交给托盘线程
        self.host.post(WM_SET_INTERVAL, secs)
        if self.service.debug:
            print(f"[间隔] 已设为 {secs} 秒", flush=True)

    # ------------------------------------------------------------ 界面线程
    def _show_panel(self):
        if self.panel is None:
            import panel as P
            self.panel = P.BatteryPanel(self.service, self._hooks, master=self.root)
        self.panel.show()

    def _show_debugui(self):
        if self.debugui is None:
            import debugui as DU
            self.debugui = DU.DebugUI(self.service, self._hooks, master=self.root)
            self.debugui.build()
        self.debugui.show()

    def _pump(self):
        try:
            while True:
                cmd, _arg = self.cmds.get_nowait()
                if cmd == "panel":
                    self._show_panel()
                elif cmd == "debugui":
                    self._show_debugui()
                elif cmd == "quit":
                    self._shutdown()
                    return
        except queue.Empty:
            pass
        if self.root is not None and not self.quitting:
            self.root.after(120, self._pump)

    def _shutdown(self):
        if self.quitting:
            return
        self.quitting = True
        if self.panel is not None:
            self.panel.destroy()
            self.panel = None
        if self.debugui is not None:
            self.debugui.destroy()
            self.debugui = None
        self.host.stop()          # 托盘线程：删图标 + 销毁隐藏窗口 + 退出消息循环
        try:
            self.root.quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        self.root = None

    # ------------------------------------------------------------ 启动
    def run(self) -> int:
        import tkinter as tk

        self.host.on_left_click = self._on_left_click
        self.host.on_right_click = self._on_right_click
        self.host.on_timer = self._on_timer
        self.host.on_custom = self._on_custom

        if not self.host.start():
            print("托盘宿主线程启动失败", file=sys.stderr)
            return 2

        # 先放占位图标，避免启动瞬间空窗
        for icon_id, key in ICON_IDS:
            spec = self.protocols.get(key, {})
            self.host.add_icon(icon_id,
                               W.text_icon("--", (255, 255, 255), ICON_UNKNOWN_BG),
                               f"{spec.get('short', '设备')}：读取中…")

        # 由托盘线程武装 SetTimer；首次刷新立刻开始
        self.host.post(WM_SET_INTERVAL, int(self.service.interval_s))
        # 保活计时器：每 ~5 秒只写一次查询帧，把键盘 2.4G 链路喂在唤醒态
        self.host.post(WM_SET_KEEPALIVE, KEEPALIVE_MS)
        self.service.keepalive_async()
        self._refresh_now()

        if self.exit_after > 0:
            def auto_exit():
                time.sleep(self.exit_after)
                self.cmds.put(("quit", None))
            threading.Thread(target=auto_exit, name="auto-exit", daemon=True).start()

        # UI 线程就是当前线程：一个隐藏的 Tk root，面板/调试版都是它的 Toplevel
        self.root = tk.Tk()
        self.root.title("HidBattery")
        self.root.withdraw()
        self.root.after(120, self._pump)
        self.root.mainloop()

        # mainloop 退出后确保托盘已清理
        self.host.stop()
        self.service.stop()
        if self.service.debug:
            print("[退出] 已清理托盘图标与隐藏窗口", flush=True)
        return 0


# ==========================================================================
# 独立界面模式
# ==========================================================================
def run_panel(interval_s: int, debug: bool) -> int:
    import panel as P

    W.enable_dpi_awareness()
    protos = D.load_protocols()
    service = D.BatteryService(interval_s, protos, debug=debug, auto_loop=True)
    service.start()

    def quit_all():
        service.stop()
        window.destroy()

    window = P.BatteryPanel(service, {
        "is_autostart": S.is_autostart_enabled,
        "set_autostart": lambda w: (S.enable_autostart() if w else S.disable_autostart()),
        "get_interval": lambda: int(service.interval_s),
        "set_interval": _interval_saver(service),
        "refresh_now": service.request_refresh,
        "on_quit": quit_all,
    })
    window.build()
    window.show()

    # 保活：每 ~5 秒只写一次查询帧，把键盘无线链路喂在唤醒态（用 Tk 定时器）
    def _keepalive_tick():
        service.keepalive_async()
        window.root.after(KEEPALIVE_MS, _keepalive_tick)

    window.root.after(KEEPALIVE_MS, _keepalive_tick)
    window.root.mainloop()
    service.stop()
    return 0


def run_debugui(interval_s: int, debug: bool) -> int:
    import debugui as DU

    W.enable_dpi_awareness()
    protos = D.load_protocols()
    service = D.BatteryService(interval_s, protos, debug=debug, auto_loop=True)
    service.start()

    def quit_all():
        service.stop()
        ui.destroy()

    ui = DU.DebugUI(service, {
        "is_autostart": S.is_autostart_enabled,
        "set_autostart": lambda w: (S.enable_autostart() if w else S.disable_autostart()),
        "get_interval": lambda: int(service.interval_s),
        "set_interval": _interval_saver(service),
        "refresh_now": service.request_refresh,
        "on_quit": quit_all,
    })
    ui.build()
    ui.show()

    # 保活：每 ~5 秒只写一次查询帧，把键盘无线链路喂在唤醒态（用 Tk 定时器）
    def _keepalive_tick():
        service.keepalive_async()
        ui.root.after(KEEPALIVE_MS, _keepalive_tick)

    ui.root.after(KEEPALIVE_MS, _keepalive_tick)
    ui.root.mainloop()
    service.stop()
    return 0


def _interval_saver(service):
    def setter(seconds):
        secs = clamp_interval(seconds)
        service.set_interval(secs)
        try:
            D.save_settings({"interval": secs})
        except OSError:
            pass
    return setter


# ==========================================================================
# 系统集成
# ==========================================================================
def run_setup() -> int:
    print("项目目录 :", S.project_dir())
    print("解释器   :", S.pythonw_exe())
    print("启动文件夹:", S.startup_dir())
    print("桌面     :", S.desktop_dir())

    ok, msg = S.enable_autostart()
    print(("[OK] " if ok else "[失败] ") + f"开机自启（启动文件夹）: {msg}")

    res = S.setup_desktop_entries()
    for name, (good, how, path) in res.items():
        tag = "OK" if good else "失败"
        print(f"[{tag}] 桌面{'目录链接' if name == 'dir' else '文件链接'}: {how} -> {path}")
    return 0 if (ok and all(v[0] for v in res.values())) else 1


def run_status() -> int:
    link = S.autostart_link_path()
    print("开机自启 :", "已启用" if os.path.exists(link) else "未启用", "->", link)
    desk = S.desktop_dir()
    dl = os.path.join(desk, S.DIR_LINK_NAME)
    fl = os.path.join(desk, S.FILE_LINK_NAME)
    print("桌面目录链接:", ("存在" if os.path.exists(dl) else "缺失"), "->", dl)
    print("桌面文件链接:", ("存在" if os.path.exists(fl) else "缺失"), "->", fl)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="黑爵 AK992 键盘 / AJ179 鼠标 无线电量小工具（纯标准库 + ctypes）")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="只读一次并打印两行电量")
    mode.add_argument("--tray", action="store_true", help="托盘常驻（默认形态）")
    mode.add_argument("--panel", action="store_true", help="只打开简洁面板")
    mode.add_argument("--debugui", action="store_true", help="完整调试版界面")
    mode.add_argument("--setup", action="store_true", help="创建自启快捷方式与桌面链接")
    mode.add_argument("--status", action="store_true", help="查看自启与桌面链接状态")
    p.add_argument("--interval", type=int, default=None,
                   help=f"刷新间隔秒数（{MIN_INTERVAL}~{MAX_INTERVAL}，默认 60）")
    p.add_argument("--debug", action="store_true", help="输出控制台调试信息（不写日志文件）")
    p.add_argument("--exit-after", type=float, default=0.0,
                   help="冒烟测试用：运行 N 秒后自动优雅退出")
    return p


def show_existing_panel() -> bool:
    """若已有实例在跑，通知它把简洁面板打开/前置，返回 True。

    靠 FindWindowW 找托盘隐藏窗口 + 注册消息唤醒；两者缺一就不动，
    绝不误杀别的进程。
    """
    import ctypes
    from ctypes import wintypes

    u = ctypes.WinDLL("user32", use_last_error=True)
    u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    u.FindWindowW.restype = wintypes.HWND
    u.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    u.RegisterWindowMessageW.restype = wintypes.UINT
    u.IsWindow.argtypes = [wintypes.HWND]
    u.IsWindow.restype = wintypes.BOOL
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                               wintypes.WPARAM, wintypes.LPARAM]
    u.PostMessageW.restype = wintypes.BOOL

    hwnd = u.FindWindowW(TRAY_WND_CLASS, TRAY_WND_TITLE)
    if not hwnd or not u.IsWindow(hwnd):
        return False
    msg = u.RegisterWindowMessageW(SHOW_PANEL_MSG_NAME)
    if not msg:
        return False
    u.PostMessageW(hwnd, msg, 0, 0)
    return True


def acquire_single_instance():
    """占住命名互斥体。返回 (是否首个实例, 句柄)。句柄要一直持有。"""
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateMutexW.restype = wintypes.HANDLE
    ERROR_ALREADY_EXISTS = 183
    h = k.CreateMutexW(None, False, MUTEX_NAME)
    first = (ctypes.get_last_error() != ERROR_ALREADY_EXISTS) and bool(h)
    return first, h


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    settings = D.load_settings()
    interval = clamp_interval(args.interval if args.interval is not None
                              else settings.get("interval", 60))

    if args.setup:
        return run_setup()
    if args.status:
        return run_status()
    if args.once:
        return run_once(args.debug)
    if args.debugui:
        return run_debugui(interval, args.debug)
    if args.panel:
        return run_panel(interval, args.debug)

    # 默认形态就是托盘常驻。
    # 单实例：已有实例在跑时不再起第二份（否则通知区域会出现两对图标，
    # 两个进程还会互抢键盘的广播报文），改为唤醒已有实例打开面板。
    first, mutex = acquire_single_instance()
    if not first:
        if show_existing_panel():
            print("已有实例在运行，已请求它打开简洁面板")
        else:
            print("已有实例在运行", file=sys.stderr)
        return 0
    try:
        return TrayApp(interval, debug=args.debug,
                       exit_after=args.exit_after).run()
    finally:
        import ctypes
        k = ctypes.WinDLL("kernel32")
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        k.CloseHandle.restype = ctypes.c_int
        k.CloseHandle(mutex)


if __name__ == "__main__":
    sys.exit(main())
