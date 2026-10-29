"""wintray.py

纯 ctypes 实现的通知区域（托盘）宿主：
    * 隐藏消息窗口 + 消息循环（独立线程），用 SetTimer 驱动刷新
    * Shell_NotifyIcon 增删改图标（32/16px，自适应 DPI）
    * 用 GDI 把电量数字画成 HICON（带 4x 超采样抗锯齿，不依赖 Pillow）
    * 任务栏重建（explorer 重启）后自动恢复图标

对外主要接口：
    enable_dpi_awareness()
    text_icon(text, fg, bg) -> HICON
    TrayHost(...)   # 管理隐藏窗口 + 多个托盘图标 + 定时器
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------- 常量
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_TIMER = 0x0113
WM_COMMAND = 0x0111
WM_APP = 0x8000
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_LBUTTONDBLCLK = 0x0203
NIN_SELECT = WM_APP + 0
WM_TASKBARCREATED_DEFAULT = "TaskbarCreated"

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIM_SETVERSION = 4

NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIF_STATE = 0x08
NIF_SHOWTIP = 0x80

NOTIFYICON_VERSION_4 = 4

HWND_MESSAGE = -3
SM_CXSMICON = 49
SM_CYSMICON = 50

DIB_RGB_COLORS = 0
BI_RGB = 0
FW_BOLD = 700
ANTIALIASED_QUALITY = 4
DEFAULT_CHARSET = 1
OUT_TT_PRECIS = 4
CLIP_DEFAULT_PRECIS = 0
TRANSPARENT = 1
DT_CENTER = 0x00000001
DT_VCENTER = 0x00000004
DT_SINGLELINE = 0x00000020
TRANSPARENT_KEY = (0xFF, 0x00, 0xFF)   # 作画时的“透明色”

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


# ---------------------------------------------------------------- 结构体
class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


# ---------------------------------------------------------------- 原型
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None
user32.SetTimer.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = wintypes.UINT
user32.KillTimer.argtypes = [wintypes.HWND, wintypes.UINT]
user32.KillTimer.restype = wintypes.BOOL
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.CreateIconIndirect.restype = wintypes.HICON
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.DestroyIcon.restype = wintypes.BOOL
user32.SetProcessDPIAware.argtypes = []
user32.SetProcessDPIAware.restype = wintypes.BOOL
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), wintypes.HBRUSH]
user32.FillRect.restype = ctypes.c_int
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                             ctypes.POINTER(RECT), wintypes.UINT]
user32.DrawTextW.restype = ctypes.c_int

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.COLORREF]
gdi32.CreatePen.restype = wintypes.HPEN
gdi32.RoundRect.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.RoundRect.restype = wintypes.BOOL
gdi32.CreateFontW.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.LPCWSTR]
gdi32.CreateFontW.restype = wintypes.HFONT
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
gdi32.SetTextColor.restype = wintypes.COLORREF
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.SetBkMode.restype = ctypes.c_int
gdi32.GetTextExtentPoint32W.argtypes = [wintypes.HDC, wintypes.LPCWSTR,
                                        ctypes.c_int, ctypes.POINTER(wintypes.SIZE)]
gdi32.GetTextExtentPoint32W.restype = wintypes.BOOL
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT,
                               wintypes.UINT, ctypes.c_void_p]
gdi32.CreateBitmap.restype = wintypes.HBITMAP

shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL


def _rgb(r, g, b) -> int:
    """COLORREF = 0x00BBGGRR"""
    return (b << 16) | (g << 8) | r


def enable_dpi_awareness() -> None:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def icon_size() -> int:
    s = user32.GetSystemMetrics(SM_CXSMICON)
    return s if 16 <= s <= 64 else 16


# ---------------------------------------------------------------- 图标绘制
def render_text_pixels(text: str, fg=(255, 255, 255), bg=(46, 139, 87),
                       size: int | None = None):
    """把文字画成图标位图，返回 (RGBA 像素, 单色掩码)。

    做法：先在一个 size*4 的大画布上用 GDI 作画（背景填 key 色表示透明），
    再降采样回 size*size，由覆盖度生成 alpha，从而得到抗锯齿的圆角图标。
    单独拆出来是为了可测试：可以直接检查像素/alpha。
    """
    if size is None:
        size = icon_size()
    ss = 4                      # 超采样倍数
    big = size * ss
    hdc_screen = user32.GetDC(0)
    hdc = gdi32.CreateCompatibleDC(hdc_screen)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = big
    bmi.bmiHeader.biHeight = -big          # 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    ppv = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), DIB_RGB_COLORS,
                                ctypes.byref(ppv), None, 0)
    old_bmp = gdi32.SelectObject(hdc, hbm)

    # 背景整片填 key 色
    key = TRANSPARENT_KEY
    brush = gdi32.CreateSolidBrush(_rgb(*key))
    rect = RECT(0, 0, big, big)
    user32.FillRect(hdc, ctypes.byref(rect), brush)
    gdi32.DeleteObject(brush)

    # 圆角矩形背景（实际图标底）
    fg_brush = gdi32.CreateSolidBrush(_rgb(*bg))
    pen = gdi32.CreatePen(0, max(1, ss // 2), _rgb(*bg))
    old_brush = gdi32.SelectObject(hdc, fg_brush)
    old_pen = gdi32.SelectObject(hdc, pen)
    marg = ss
    rad = int(big * 0.28)
    gdi32.RoundRect(hdc, marg, marg, big - marg, big - marg, rad, rad)
    gdi32.SelectObject(hdc, old_brush)
    gdi32.SelectObject(hdc, old_pen)
    gdi32.DeleteObject(fg_brush)
    gdi32.DeleteObject(pen)

    # 文字
    gdi32.SetBkMode(hdc, TRANSPARENT)
    gdi32.SetTextColor(hdc, _rgb(*fg))
    inner = big - 2 * marg - 2 * ss
    font = None
    height = max(6, int(inner * 0.78))
    for _try in range(14):
        f = gdi32.CreateFontW(-height, 0, 0, 0, FW_BOLD, 0, 0, 0,
                              DEFAULT_CHARSET, OUT_TT_PRECIS, CLIP_DEFAULT_PRECIS,
                              ANTIALIASED_QUALITY, 0, "Segoe UI")
        old_font = gdi32.SelectObject(hdc, f)
        sz = wintypes.SIZE()
        gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(sz))
        if sz.cx <= inner or height <= 7:
            font = f
            break
        gdi32.SelectObject(hdc, old_font)
        gdi32.DeleteObject(f)
        height -= max(1, int(inner * 0.06))
    trect = RECT(marg, marg, big - marg, big - marg)
    user32.DrawTextW(hdc, text, len(text), ctypes.byref(trect),
                     DT_CENTER | DT_VCENTER | DT_SINGLELINE)
    if font:
        gdi32.SelectObject(hdc, old_font)
        gdi32.DeleteObject(font)

    gdi32.SelectObject(hdc, old_bmp)

    # ---- 降采样 + 生成 alpha ----
    src = ctypes.cast(ppv, ctypes.POINTER(ctypes.c_ubyte * (big * big * 4))).contents
    out_pixels = bytearray(size * size * 4)
    mask_row_bytes = (size + 7) // 8
    mask = bytearray(mask_row_bytes * size)

    keyr, keyg, keyb = key
    n = ss * ss
    for y in range(size):
        for x in range(size):
            r_acc = g_acc = b_acc = 0
            cover = 0
            for dy in range(ss):
                row = (y * ss + dy) * big * 4
                for dx in range(ss):
                    i = row + (x * ss + dx) * 4
                    b = src[i]; g = src[i + 1]; r = src[i + 2]
                    if abs(r - keyr) <= 12 and abs(g - keyg) <= 12 and abs(b - keyb) <= 12:
                        continue
                    r_acc += r; g_acc += g; b_acc += b
                    cover += 1
            o = (y * size + x) * 4
            if cover == 0:
                out_pixels[o + 0] = bg[2]
                out_pixels[o + 1] = bg[1]
                out_pixels[o + 2] = bg[0]
                out_pixels[o + 3] = 0
                mask[y * mask_row_bytes + (x >> 3)] |= 1 << (7 - (x & 7))
            else:
                out_pixels[o + 0] = b_acc // cover
                out_pixels[o + 1] = g_acc // cover
                out_pixels[o + 2] = r_acc // cover
                out_pixels[o + 3] = min(255, cover * 255 // n)

    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(0, hdc_screen)
    return bytes(out_pixels), bytes(mask)


def text_icon(text: str, fg=(255, 255, 255), bg=(46, 139, 87), size: int | None = None):
    """把文字画进一枚 32bpp 图标，返回 HICON(int)。

    实际绘制交给 render_text_pixels 完成（key 色 + 4x 超采样 -> 抗锯齿透明边），
    这里只负责把像素包成 Windows 图标。
    """
    if size is None:
        size = icon_size()
    out_pixels, mask = render_text_pixels(text, fg, bg, size)

    hdc_screen = user32.GetDC(0)
    hdc = gdi32.CreateCompatibleDC(hdc_screen)
    # 颜色位图
    pbmi = BITMAPINFO()
    pbmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    pbmi.bmiHeader.biWidth = size
    pbmi.bmiHeader.biHeight = -size
    pbmi.bmiHeader.biPlanes = 1
    pbmi.bmiHeader.biBitCount = 32
    pbmi.bmiHeader.biCompression = BI_RGB
    pcolor = ctypes.c_void_p()
    hbm_color = gdi32.CreateDIBSection(hdc, ctypes.byref(pbmi), DIB_RGB_COLORS,
                                       ctypes.byref(pcolor), None, 0)
    ctypes.memmove(pcolor, bytes(out_pixels), len(out_pixels))

    hbm_mask = gdi32.CreateBitmap(size, size, 1, 1, bytes(mask))

    info = ICONINFO()
    info.fIcon = True
    info.hbmMask = hbm_mask
    info.hbmColor = hbm_color
    icon = user32.CreateIconIndirect(ctypes.byref(info))

    gdi32.DeleteObject(hbm_color)
    gdi32.DeleteObject(hbm_mask)
    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(0, hdc_screen)
    return icon


# ---------------------------------------------------------------- 托盘宿主
def register_message(name: str) -> int:
    """注册（或取回）一个会话级窗口消息 id，供跨进程唤醒用。"""
    return user32.RegisterWindowMessageW(name)


class TrayHost:
    """隐藏消息窗口 + 消息循环（独立线程）+ 托盘图标管理。"""

    def __init__(self, name: str = "HidBatteryTrayHost"):
        self.name = name
        self.hwnd = None
        self._thread = None
        self._wndproc_ref = None
        self._class_atom = None
        self._icons: dict[int, dict] = {}      # id -> {icon, tip}
        self._ready = threading.Event()
        self._taskbar_msg = 0
        # 回调（在消息线程里被调用）
        self.on_left_click = None      # (icon_id) -> None
        self.on_right_click = None     # (icon_id) -> None
        self.on_timer = None           # (timer_id) -> None
        self.on_close = None           # () -> None
        self.on_custom = None          # (msg, wparam, lparam) -> None
        self._timers: dict[int, int] = {}

    # ---------------------------------------------------------- 生命周期
    def start(self, wait: bool = True, timeout: float = 5.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="winmsg", daemon=True)
        self._thread.start()
        if wait:
            return self._ready.wait(timeout)
        return True

    def _run(self) -> None:
        self._taskbar_msg = user32.RegisterWindowMessageW(WM_TASKBARCREATED_DEFAULT)

        @WNDPROC
        def _proc(hwnd, msg, wparam, lparam):
            if msg == WM_TIMER:
                if self.on_timer:
                    try:
                        self.on_timer(int(wparam))
                    except Exception:
                        pass
                return 0
            if msg == self._CALLBACK_MSG:
                code = int(lparam) & 0xFFFF
                icon_id = (int(lparam) >> 16) & 0xFFFF
                if icon_id == 0:
                    icon_id = int(wparam) & 0xFFFF
                if code in (WM_LBUTTONUP, NIN_SELECT):
                    if self.on_left_click:
                        try:
                            self.on_left_click(icon_id)
                        except Exception:
                            pass
                elif code in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    if self.on_right_click:
                        try:
                            self.on_right_click(icon_id)
                        except Exception:
                            pass
                return 0
            if self._taskbar_msg and msg == self._taskbar_msg:
                for iid, info in self._icons.items():
                    nid = self._make_nid(iid, info["tip"])
                    shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
                    nid.uVersion = NOTIFYICON_VERSION_4
                    shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
                return 0
            if msg == WM_CLOSE:
                self._delete_all()
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            if msg >= WM_APP and msg != self._CALLBACK_MSG:
                if self.on_custom:
                    try:
                        self.on_custom(int(msg), int(wparam), int(lparam))
                    except Exception:
                        pass
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = _proc

        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = _proc
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = self.name
        self._class_atom = user32.RegisterClassW(ctypes.byref(wc))

        self.hwnd = user32.CreateWindowExW(
            0, self.name, "HidBattery", 0, 0, 0, 0, 0,
            ctypes.c_void_p(HWND_MESSAGE), None, wc.hInstance, None)
        self._ready.set()

        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if self.on_close:
            try:
                self.on_close()
            except Exception:
                pass

    _CALLBACK_MSG = WM_APP + 1

    def stop(self) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    # ---------------------------------------------------------- 图标
    def _make_nid(self, icon_id: int, tip: str) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = icon_id
        nid.uFlags = NIF_ICON | NIF_TIP | NIF_MESSAGE | NIF_SHOWTIP
        nid.uCallbackMessage = self._CALLBACK_MSG
        info = self._icons.get(icon_id)
        nid.hIcon = info["icon"] if info else None
        nid.szTip = (tip or "")[:127]
        return nid

    def add_icon(self, icon_id: int, icon, tip: str = "") -> None:
        self._icons[icon_id] = {"icon": icon, "tip": tip}
        if not self.hwnd:
            return
        nid = self._make_nid(icon_id, tip)
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        nid.uVersion = NOTIFYICON_VERSION_4
        shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))

    def update_icon(self, icon_id: int, icon=None, tip: str | None = None) -> None:
        info = self._icons.setdefault(icon_id, {"icon": None, "tip": ""})
        old = info["icon"]
        if icon is not None:
            if old and old != icon:
                user32.DestroyIcon(old)
            info["icon"] = icon
        if tip is not None:
            info["tip"] = tip
        if not self.hwnd:
            return
        nid = self._make_nid(icon_id, info["tip"])
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def remove_icon(self, icon_id: int) -> None:
        info = self._icons.pop(icon_id, None)
        if info and info.get("icon"):
            user32.DestroyIcon(info["icon"])
        if self.hwnd:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = icon_id
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))

    def _delete_all(self) -> None:
        for iid in list(self._icons.keys()):
            self.remove_icon(iid)

    # ---------------------------------------------------------- 定时器
    def set_timer(self, timer_id: int, interval_ms: int) -> None:
        self._timers[timer_id] = interval_ms
        if self.hwnd:
            user32.SetTimer(self.hwnd, timer_id, interval_ms, None)

    def kill_timer(self, timer_id: int) -> None:
        self._timers.pop(timer_id, None)
        if self.hwnd:
            user32.KillTimer(self.hwnd, timer_id)

    def post(self, msg: int, wparam: int = 0, lparam: int = 0) -> None:
        if self.hwnd:
            user32.PostMessageW(self.hwnd, msg, wparam, lparam)
