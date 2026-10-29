"""sysinteg.py

系统集成：开机自启快捷方式 + 桌面「链接式」入口。

* 开机自启：在「启动」文件夹里创建 .lnk（任务管理器 →「启动」标签可直接看到/启用/禁用），
  目标是 pythonw.exe + 脚本路径，因此不会弹黑窗。不用注册表 Run。
* 桌面入口：目录链接优先真符号链接（需管理员或开发者模式），
  失败则降级为 junction（mklink /J，无需管理员）；文件链接优先符号链接，
  失败则降级为硬链接（os.link）。全程不复制脚本本体。

.lnk 用 COM 的 IShellLinkW/IPersistFile 直接通过 ctypes 调用，不依赖 pywin32。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from ctypes import wintypes

ole32 = ctypes.WinDLL("ole32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

COINIT_APARTMENTTHREADED = 0x2
CLSCTX_INPROC_SERVER = 0x1
S_OK = 0

CSIDL_DESKTOPDIRECTORY = 0x0010
CSIDL_STARTUP = 0x0007
CSIDL_COMMON_STARTUP = 0x0018
SHGFP_TYPE_CURRENT = 0

AUTOSTART_NAME = "电量托盘"
FILE_LINK_NAME = "电量托盘.lnk"
DIR_LINK_NAME = "HidBattery 项目目录"


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> GUID:
    t = text.strip("{}")
    d1, d2, d3, d4a, d4b = t.split("-")
    g = GUID()
    g.Data1 = int(d1, 16)
    g.Data2 = int(d2, 16)
    g.Data3 = int(d3, 16)
    raw = bytes.fromhex(d4a + d4b)
    for i, b in enumerate(raw):
        g.Data4[i] = b
    return g


CLSID_SHELLLINK = _guid("00021401-0000-0000-C000-000000000046")
IID_ISHELLLINKW = _guid("000214F9-0000-0000-C000-000000000046")
IID_IPERSISTFILE = _guid("0000010B-0000-0000-C000-000000000046")

# IShellLinkW vtable 原始下标（含 IUnknown 3 项）。
# 注意：这些下标是【实测标定】出来的，不是照抄头文件顺序。
# 标定方法：逐个下标写入带标记的字符串并保存 .lnk，再用系统自带的
# WScript.Shell 解析回读，看标记落在哪个字段上。实测结果：
#   下标 7 -> SetDescription      下标 9  -> SetWorkingDirectory
#   下标 11 -> SetArguments       下标 17 -> SetIconLocation
#   下标 20 -> SetPath
# 下标 3/21/23/25 会触发访问冲突，禁止调用。
VT_QUERYINTERFACE = 0
VT_SETDESCRIPTION = 7
VT_SETWORKINGDIR = 9
VT_SETARGUMENTS = 11
VT_SETICONLOCATION = 17
VT_SETPATH = 20
# IPersistFile
VT_SAVE = 6
VT_RELEASE = 2

HRESULT = ctypes.c_long


def _fn(ptr, index, restype, *argtypes):
    vt = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    addr = vt[index]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(addr)


def _release(ptr) -> None:
    try:
        _fn(ptr, VT_RELEASE, HRESULT)(ptr)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
shell32.SHGetFolderPathW.argtypes = [
    wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD,
    ctypes.POINTER(wintypes.WCHAR * 260)]
shell32.SHGetFolderPathW.restype = HRESULT


def known_folder(csidl: int) -> str:
    buf = ctypes.create_unicode_buffer(260)
    hr = shell32.SHGetFolderPathW(None, csidl, None, SHGFP_TYPE_CURRENT, buf)
    if hr != S_OK:
        raise OSError(f"SHGetFolderPathW 失败: 0x{hr & 0xFFFFFFFF:08X}")
    return buf.value


def startup_dir() -> str:
    return known_folder(CSIDL_STARTUP)


def desktop_dir() -> str:
    return known_folder(CSIDL_DESKTOPDIRECTORY)


def python_exe() -> str:
    """当前解释器路径。"""
    return sys.executable or shutil.which("python") or "python.exe"


def pythonw_exe() -> str:
    """优先返回同目录的 pythonw.exe（无控制台窗口）。"""
    exe = python_exe()
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(cand):
        return cand
    w = shutil.which("pythonw")
    return w or exe


def project_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def entry_script() -> str:
    return os.path.join(project_dir(), "my_devices.py")


# --------------------------------------------------------------------------
# .lnk 创建
# --------------------------------------------------------------------------
def create_shortcut(link_path: str, target: str, arguments: str = "",
                    workdir: str = "", description: str = "",
                    icon_path: str = "", icon_index: int = 0) -> bool:
    """用 IShellLinkW 写入一个 .lnk。成功返回 True。"""
    ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    ptr = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_SHELLLINK), None, CLSCTX_INPROC_SERVER,
            ctypes.byref(IID_ISHELLLINKW), ctypes.byref(ptr))
        if hr != S_OK or not ptr.value:
            return False

        _fn(ptr, VT_SETPATH, HRESULT, wintypes.LPCWSTR)(ptr, target)
        if arguments:
            _fn(ptr, VT_SETARGUMENTS, HRESULT, wintypes.LPCWSTR)(ptr, arguments)
        if workdir:
            _fn(ptr, VT_SETWORKINGDIR, HRESULT, wintypes.LPCWSTR)(ptr, workdir)
        if description:
            _fn(ptr, VT_SETDESCRIPTION, HRESULT, wintypes.LPCWSTR)(ptr, description)
        if icon_path:
            _fn(ptr, VT_SETICONLOCATION, HRESULT, wintypes.LPCWSTR, ctypes.c_int)(
                ptr, icon_path, icon_index)

        pf = ctypes.c_void_p()
        hr = _fn(ptr, VT_QUERYINTERFACE, HRESULT, ctypes.POINTER(GUID),
                 ctypes.POINTER(ctypes.c_void_p))(
            ptr, ctypes.byref(IID_IPERSISTFILE), ctypes.byref(pf))
        if hr != S_OK or not pf.value:
            return False
        try:
            os.makedirs(os.path.dirname(link_path), exist_ok=True)
            if os.path.exists(link_path):
                os.remove(link_path)
            hr = _fn(pf, VT_SAVE, HRESULT, wintypes.LPCWSTR, wintypes.BOOL)(
                pf, link_path, True)
            return hr == S_OK and os.path.exists(link_path)
        finally:
            _release(pf)
    finally:
        if ptr.value:
            _release(ptr)
        ole32.CoUninitialize()


# --------------------------------------------------------------------------
# 开机自启
# --------------------------------------------------------------------------
def autostart_link_path() -> str:
    return os.path.join(startup_dir(), AUTOSTART_NAME + ".lnk")


def is_autostart_enabled() -> bool:
    return os.path.exists(autostart_link_path())


def enable_autostart() -> tuple[bool, str]:
    """在「启动」文件夹里创建「电量托盘.lnk」。"""
    exe = pythonw_exe()
    script = entry_script()
    if not os.path.exists(exe):
        return False, f"未找到 pythonw.exe：{exe}"
    if not os.path.exists(script):
        return False, f"未找到脚本：{script}"
    ok = create_shortcut(
        autostart_link_path(), exe, f'"{script}" --tray',
        workdir=project_dir(),
        description="黑爵 AK992 / AJ179 无线外设电量托盘",
        icon_path=exe, icon_index=0)
    if ok:
        return True, autostart_link_path()
    return False, "创建快捷方式失败"


def disable_autostart() -> tuple[bool, str]:
    p = autostart_link_path()
    try:
        if os.path.exists(p):
            os.remove(p)
        return True, "已移除"
    except OSError as exc:
        return False, str(exc)


# --------------------------------------------------------------------------
# 桌面链接（不复制本体）
# --------------------------------------------------------------------------
def _run_mklink_junction(link_path: str, target: str) -> bool:
    try:
        out = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link_path, target],
            capture_output=True, text=True, timeout=15)
        return out.returncode == 0 and os.path.isdir(link_path)
    except Exception:
        return False


def make_dir_link(target_dir: str, link_path: str) -> tuple[bool, str]:
    """目录链接：真符号链接优先，失败降级 junction。"""
    if os.path.exists(link_path) or os.path.islink(link_path):
        return True, "已存在"
    try:
        os.symlink(target_dir, link_path, target_is_directory=True)
        return True, "符号链接"
    except (OSError, NotImplementedError):
        pass
    if _run_mklink_junction(link_path, target_dir):
        return True, "junction"
    return False, "需管理员或开发者模式才能建符号链接，junction 也失败"


def make_file_link(target_file: str, link_path: str) -> tuple[bool, str]:
    """文件链接：真符号链接优先，失败降级硬链接。"""
    if os.path.exists(link_path) or os.path.islink(link_path):
        return True, "已存在"
    try:
        os.symlink(target_file, link_path)
        return True, "符号链接"
    except (OSError, NotImplementedError):
        pass
    try:
        os.link(target_file, link_path)
        return True, "硬链接"
    except OSError as exc:
        return False, f"硬链接失败：{exc}"


def setup_desktop_entries() -> dict:
    """桌面放两个链接：项目目录目录链接 + 启动脚本文件链接。"""
    desk = desktop_dir()
    proj = project_dir()
    # 启动脚本：一个真正给用户点的入口（同样用 .lnk，指向 pythonw）
    launcher_name = "启动电量托盘.lnk"
    launcher = os.path.join(proj, launcher_name)
    create_shortcut(launcher, pythonw_exe(), f'"{entry_script()}" --tray',
                    workdir=proj, description="启动电量托盘",
                    icon_path=pythonw_exe(), icon_index=0)

    results = {}
    ok, how = make_dir_link(proj, os.path.join(desk, DIR_LINK_NAME))
    results["dir"] = (ok, how, os.path.join(desk, DIR_LINK_NAME))
    ok2, how2 = make_file_link(launcher, os.path.join(desk, FILE_LINK_NAME))
    results["file"] = (ok2, how2, os.path.join(desk, FILE_LINK_NAME))
    return results
