"""hidcore.py

纯 ctypes 实现的 HID 底层：只依赖 Python 标准库 + Windows 自带 DLL
(setupapi.dll / hid.dll / kernel32.dll)，不依赖 hidapi / pywin32 / Pillow。

能力：
    * 用 SetupAPI 枚举系统中所有 HID 接口（并按设备接口路径定位）
    * 解析 HID 报告描述符（原始字节 + 通过 HidP_* 得到 Report ID / 用途 / 位宽）
    * Feature Report 读写（HidD_GetFeature / HidD_SetFeature）
    * Input / Output Report 读写（ReadFile / WriteFile，带超时）
    * 设备属性（VID/PID/版本/厂商串/产品串/序列号）

所有句柄均以 int 形式返回，便于跨模块传递。
"""

from __future__ import annotations

import ctypes
import contextlib
from ctypes import wintypes

# --------------------------------------------------------------------------
# DLL 绑定
# --------------------------------------------------------------------------
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
NULL = None

# SetupAPI
DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_NO_MORE_ITEMS = 259

SPDRP_DEVICEDESC = 0x00000000
SPDRP_MFG = 0x0000000B
SPDRP_FRIENDLYNAME = 0x0000000C
SPDRP_HARDWAREID = 0x00000001

# CreateFile
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000

# IOCTL
IOCTL_HID_GET_REPORT_DESCRIPTOR = 0x000B0004
IOCTL_HID_GET_REPORT = 0x000B0048
IOCTL_HID_SET_REPORT = 0x000B004C
IOCTL_HID_GET_FEATURE = 0x000B0040
IOCTL_HID_SET_FEATURE = 0x000B003C
IOCTL_HID_GET_INPUT_REPORT = 0x000B0034
IOCTL_HID_SET_OUTPUT_REPORT = 0x000B0038

# HIDP_REPORT_TYPE
HidP_Input = 0
HidP_Output = 1
HidP_Feature = 2

WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258

HIDP_STATUS_SUCCESS = 0x00110000

# 标准用途页
USAGE_PAGE_GENERIC_DESKTOP = 0x01
USAGE_PAGE_GENERIC_DEVICE_CONTROLS = 0x06
USAGE_PAGE_BATTERY_SYSTEM = 0x85
USAGE_BATTERY_STRENGTH = 0x20   # page 0x06 usage 0x20
USAGE_BATTERY_LEVEL = 0x66      # page 0x85 usage 0x66 (无单位百分比)


# --------------------------------------------------------------------------
# 结构体
# --------------------------------------------------------------------------
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __str__(self) -> str:
        d4 = "".join(f"{b:02X}" for b in self.Data4)
        return (f"{{{self.Data1:08X}-{self.Data2:04X}-{self.Data3:04X}-"
                f"{d4[:4]}-{d4[4:]}}}")

    def bytes(self) -> bytes:
        return bytes(self)


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.POINTER(ctypes.c_ulong)),
    ]


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", GUID),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Size", wintypes.DWORD),
        ("VendorID", ctypes.c_ushort),
        ("ProductID", ctypes.c_ushort),
        ("VersionNumber", ctypes.c_ushort),
    ]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberInputDataIndices", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberOutputDataIndices", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
        ("NumberFeatureDataIndices", ctypes.c_ushort),
    ]


class HIDP_RANGE(ctypes.Structure):
    _fields_ = [
        ("UsageMin", ctypes.c_ushort),
        ("UsageMax", ctypes.c_ushort),
        ("StringMin", ctypes.c_ushort),
        ("StringMax", ctypes.c_ushort),
        ("DesignatorMin", ctypes.c_ushort),
        ("DesignatorMax", ctypes.c_ushort),
        ("DataIndexMin", ctypes.c_ushort),
        ("DataIndexMax", ctypes.c_ushort),
    ]


class _ValueUnion(ctypes.Union):
    _fields_ = [("Range", HIDP_RANGE), ("NotRange", HIDP_RANGE)]


class HIDP_VALUE_CAPS(ctypes.Structure):
    _anonymous_ = ()
    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("ReportID", ctypes.c_ubyte),
        ("IsAlias", ctypes.c_ubyte),
        ("BitField", ctypes.c_ushort),
        ("LinkCollection", ctypes.c_ushort),
        ("LinkUsage", ctypes.c_ushort),
        ("LinkUsagePage", ctypes.c_ushort),
        ("IsRange", ctypes.c_ubyte),
        ("IsStringRange", ctypes.c_ubyte),
        ("IsDesignatorRange", ctypes.c_ubyte),
        ("IsAbsolute", ctypes.c_ubyte),
        ("HasNull", ctypes.c_ubyte),
        ("Reserved", ctypes.c_ubyte),
        ("BitSize", ctypes.c_ushort),
        ("ReportCount", ctypes.c_ushort),
        ("Reserved2", ctypes.c_ushort * 5),
        ("UnitsExp", ctypes.c_ulong),
        ("Units", ctypes.c_ulong),
        ("LogicalMin", ctypes.c_long),
        ("LogicalMax", ctypes.c_long),
        ("PhysicalMin", ctypes.c_long),
        ("PhysicalMax", ctypes.c_long),
        ("u", _ValueUnion),
    ]


class HIDP_BUTTON_CAPS(ctypes.Structure):
    _fields_ = [
        ("UsagePage", ctypes.c_ushort),
        ("ReportID", ctypes.c_ubyte),
        ("IsAlias", ctypes.c_ubyte),
        ("BitField", ctypes.c_ushort),
        ("LinkCollection", ctypes.c_ushort),
        ("LinkUsage", ctypes.c_ushort),
        ("LinkUsagePage", ctypes.c_ushort),
        ("IsRange", ctypes.c_ubyte),
        ("IsStringRange", ctypes.c_ubyte),
        ("IsDesignatorRange", ctypes.c_ubyte),
        ("IsAbsolute", ctypes.c_ubyte),
        ("Reserved", ctypes.c_ubyte * 10),
        ("u", _ValueUnion),
    ]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


# --------------------------------------------------------------------------
# 函数原型（64 位必须显式声明，否则 HANDLE/指针会被截断）
# --------------------------------------------------------------------------
setupapi.SetupDiGetClassDevsW.argtypes = [
    ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE

setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), ctypes.POINTER(GUID),
    wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL

setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(SP_DEVINFO_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL

setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD)]
setupapi.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL

hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
hid.HidD_GetHidGuid.restype = None

hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetAttributes.restype = wintypes.BOOL

hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hid.HidD_GetPreparsedData.restype = wintypes.BOOL

hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidD_FreePreparsedData.restype = wintypes.BOOL

hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hid.HidP_GetCaps.restype = ctypes.c_long  # NTSTATUS

hid.HidD_GetFeature.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
hid.HidD_GetFeature.restype = wintypes.BOOL

hid.HidD_SetFeature.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
hid.HidD_SetFeature.restype = wintypes.BOOL

hid.HidD_GetInputReport.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
hid.HidD_GetInputReport.restype = wintypes.BOOL

hid.HidD_SetOutputReport.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
hid.HidD_SetOutputReport.restype = wintypes.BOOL

hid.HidD_GetManufacturerString.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
hid.HidD_GetManufacturerString.restype = wintypes.BOOL

hid.HidD_GetProductString.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
hid.HidD_GetProductString.restype = wintypes.BOOL

hid.HidD_GetSerialNumberString.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
hid.HidD_GetSerialNumberString.restype = wintypes.BOOL

hid.HidD_GetIndexedString.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG]
hid.HidD_GetIndexedString.restype = wintypes.BOOL

hid.HidP_GetValueCaps.argtypes = [
    ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort), ctypes.c_void_p]
hid.HidP_GetValueCaps.restype = ctypes.c_long

hid.HidP_GetButtonCaps.argtypes = [
    ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort), ctypes.c_void_p]
hid.HidP_GetButtonCaps.restype = ctypes.c_long

hid.HidP_GetLinkCollectionNodes.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
hid.HidP_GetLinkCollectionNodes.restype = ctypes.c_long

kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.CreateFileW.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
kernel32.DeviceIoControl.restype = wintypes.BOOL

kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
kernel32.ReadFile.restype = wintypes.BOOL

kernel32.WriteFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
kernel32.WriteFile.restype = wintypes.BOOL

kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE

kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD

kernel32.GetOverlappedResult.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
kernel32.GetOverlappedResult.restype = wintypes.BOOL

kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
kernel32.CancelIoEx.restype = wintypes.BOOL

kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
kernel32.ResetEvent.restype = wintypes.BOOL


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------
def _hexdump(data: bytes, per_line: int = 16) -> str:
    out = []
    for off in range(0, len(data), per_line):
        chunk = data[off:off + per_line]
        hexpart = " ".join(f"{b:02X}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{off:04X}  {hexpart:<{per_line * 3}} {asc}")
    return "\n".join(out) if out else "(空)"


def _win_err(prefix: str = "") -> str:
    code = ctypes.get_last_error()
    return f"{prefix}失败 (WinError {code}: {ctypes.FormatError(code)})"


class HidError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 设备枚举
# --------------------------------------------------------------------------
class HidInterface:
    """一个 HID 设备接口（对应一条 \\\\?\\hid#... 路径）。"""

    __slots__ = (
        "path", "vid", "pid", "version", "usage_page", "usage", "product",
        "manufacturer", "serial", "input_len", "output_len", "feature_len",
        "container_id", "friendly_name", "instance_id", "raw_path",
    )

    def __init__(self, path: str, **kw):
        self.path = path
        self.vid = kw.get("vid", 0)
        self.pid = kw.get("pid", 0)
        self.version = kw.get("version", 0)
        self.usage_page = kw.get("usage_page", 0)
        self.usage = kw.get("usage", 0)
        self.product = kw.get("product", "")
        self.manufacturer = kw.get("manufacturer", "")
        self.serial = kw.get("serial", "")
        self.input_len = kw.get("input_len", 0)
        self.output_len = kw.get("output_len", 0)
        self.feature_len = kw.get("feature_len", 0)
        self.container_id = kw.get("container_id", "")
        self.friendly_name = kw.get("friendly_name", "")
        self.instance_id = kw.get("instance_id", "")
        self.raw_path = kw.get("raw_path", path)

    @property
    def vid_pid(self) -> str:
        return f"{self.vid:04X}:{self.pid:04X}"

    @property
    def up_usage(self) -> str:
        return f"{self.usage_page:04X}/{self.usage:04X}"

    def __repr__(self) -> str:
        return (f"<HidInterface {self.vid_pid} UP {self.up_usage} "
                f"Feat {self.feature_len} {self.product!r}>")


def _get_wide_string(handle, func, size: int = 512) -> str:
    buf = ctypes.create_unicode_buffer(size)
    ok = func(handle, ctypes.byref(buf), ctypes.sizeof(buf))
    if not ok:
        return ""
    return buf.value


def _dev_reg_prop(dev_info, devinfo_data, prop: int) -> str:
    required = wintypes.DWORD(0)
    reg_type = wintypes.DWORD(0)
    setupapi.SetupDiGetDeviceRegistryPropertyW(
        dev_info, ctypes.byref(devinfo_data), prop, ctypes.byref(reg_type),
        None, 0, ctypes.byref(required))
    if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER or required.value == 0:
        return ""
    buf = ctypes.create_unicode_buffer(required.value // 2 + 2)
    ok = setupapi.SetupDiGetDeviceRegistryPropertyW(
        dev_info, ctypes.byref(devinfo_data), prop, ctypes.byref(reg_type),
        buf, required.value, ctypes.byref(required))
    if not ok:
        return ""
    if reg_type.value == 2:  # REG_EXPAND_SZ / REG_SZ
        return buf.value
    if reg_type.value == 7:  # REG_MULTI_SZ
        return "\0".join(buf.value.split("\0"))
    return buf.value


def enumerate_interfaces() -> list[HidInterface]:
    """枚举所有在线 HID 接口。"""
    result: list[HidInterface] = []
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    flags = DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    dev_info = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, flags)
    if dev_info == INVALID_HANDLE_VALUE or not dev_info:
        raise HidError(_win_err("SetupDiGetClassDevsW"))
    try:
        index = 0
        iface = SP_DEVICE_INTERFACE_DATA()
        iface.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
        while True:
            ok = setupapi.SetupDiEnumDeviceInterfaces(
                dev_info, None, ctypes.byref(guid), index, ctypes.byref(iface))
            if not ok:
                break
            index += 1
            required = wintypes.DWORD(0)
            devinfo_data = SP_DEVINFO_DATA()
            devinfo_data.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                dev_info, ctypes.byref(iface), None, 0, ctypes.byref(required),
                ctypes.byref(devinfo_data))
            if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER:
                continue
            # cbSize 是 4 字节，x64 下需按 4 字节对齐成 8
            cb_size = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            buf = ctypes.create_string_buffer(required.value)
            ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = cb_size
            ok = setupapi.SetupDiGetDeviceInterfaceDetailW(
                dev_info, ctypes.byref(iface), buf, required.value,
                ctypes.byref(required), ctypes.byref(devinfo_data))
            if not ok:
                continue
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            info = _probe_interface(path, dev_info, devinfo_data)
            if info is not None:
                result.append(info)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(dev_info)
    return result


def _probe_interface(path, dev_info, devinfo_data) -> HidInterface | None:
    """打开接口并抓取静态信息（打开失败仍然返回仅有路径的条目）。"""
    info = HidInterface(path)
    info.friendly_name = _dev_reg_prop(dev_info, devinfo_data, SPDRP_FRIENDLYNAME) \
        or _dev_reg_prop(dev_info, devinfo_data, SPDRP_DEVICEDESC)
    hwids = _dev_reg_prop(dev_info, devinfo_data, SPDRP_HARDWAREID)
    info.instance_id = hwids.split("\0")[0] if hwids else ""

    handle = open_device(path, read_only=True)
    if handle is None:
        # 仍然解析 VID/PID（从路径里提取）
        _fill_vid_pid_from_path(info)
        return info
    try:
        attrs = HIDD_ATTRIBUTES()
        attrs.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
        if hid.HidD_GetAttributes(handle, ctypes.byref(attrs)):
            info.vid = attrs.VendorID
            info.pid = attrs.ProductID
            info.version = attrs.VersionNumber
        info.manufacturer = _get_wide_string(handle, hid.HidD_GetManufacturerString)
        info.product = _get_wide_string(handle, hid.HidD_GetProductString)
        info.serial = _get_wide_string(handle, hid.HidD_GetSerialNumberString)
        caps = get_caps(handle)
        if caps is not None:
            info.usage_page = caps.UsagePage
            info.usage = caps.Usage
            info.input_len = caps.InputReportByteLength
            info.output_len = caps.OutputReportByteLength
            info.feature_len = caps.FeatureReportByteLength
    finally:
        close_device(handle)
    if not info.vid:
        _fill_vid_pid_from_path(info)
    return info


def _fill_vid_pid_from_path(info: HidInterface) -> None:
    import re
    m = re.search(r"vid_([0-9a-f]{4})&pid_([0-9a-f]{4})", info.path, re.I)
    if m:
        info.vid = int(m.group(1), 16)
        info.pid = int(m.group(2), 16)


# --------------------------------------------------------------------------
# 打开 / 关闭
# --------------------------------------------------------------------------
def open_device(path: str, read_only: bool = False, overlapped: bool = False):
    """打开设备接口，成功返回句柄(int)，失败返回 None。"""
    access = GENERIC_READ | (0 if read_only else GENERIC_WRITE)
    share = FILE_SHARE_READ | FILE_SHARE_WRITE
    flags = FILE_FLAG_OVERLAPPED if overlapped else 0
    handle = kernel32.CreateFileW(path, access, share, None, OPEN_EXISTING, flags, None)
    if handle == INVALID_HANDLE_VALUE or not handle:
        return None
    return handle


def close_device(handle) -> None:
    if handle:
        kernel32.CloseHandle(handle)


@contextlib.contextmanager
def opened(path: str, read_only: bool = False, overlapped: bool = False):
    handle = open_device(path, read_only=read_only, overlapped=overlapped)
    if handle is None:
        raise HidError(_win_err(f"打开 {path} "))
    try:
        yield handle
    finally:
        close_device(handle)


def get_caps(handle) -> HIDP_CAPS | None:
    pp = ctypes.c_void_p()
    if not hid.HidD_GetPreparsedData(handle, ctypes.byref(pp)):
        return None
    try:
        caps = HIDP_CAPS()
        status = hid.HidP_GetCaps(pp, ctypes.byref(caps))
        # HIDP_STATUS_SUCCESS == 0x00110000
        if status != HIDP_STATUS_SUCCESS:
            return None
        return caps
    finally:
        hid.HidD_FreePreparsedData(pp)


def _preparsed(handle):
    pp = ctypes.c_void_p()
    if not hid.HidD_GetPreparsedData(handle, ctypes.byref(pp)):
        raise HidError(_win_err("HidD_GetPreparsedData "))
    return pp


def list_value_caps(handle) -> dict[int, list[HIDP_VALUE_CAPS]]:
    """返回 {report_type: [value caps]}，report_type 0/1/2 = Input/Output/Feature。"""
    out: dict[int, list[HIDP_VALUE_CAPS]] = {}
    pp = _preparsed(handle)
    try:
        for rtype in (HidP_Input, HidP_Output, HidP_Feature):
            length = ctypes.c_ushort(0)
            hid.HidP_GetValueCaps(rtype, None, ctypes.byref(length), pp)
            if length.value == 0:
                out[rtype] = []
                continue
            arr = (HIDP_VALUE_CAPS * length.value)()
            ok = hid.HidP_GetValueCaps(rtype, arr, ctypes.byref(length), pp)
            out[rtype] = list(arr) if ok == HIDP_STATUS_SUCCESS else []
    finally:
        hid.HidD_FreePreparsedData(pp)
    return out


def list_button_caps(handle) -> dict[int, list[HIDP_BUTTON_CAPS]]:
    out: dict[int, list[HIDP_BUTTON_CAPS]] = {}
    pp = _preparsed(handle)
    try:
        for rtype in (HidP_Input, HidP_Output, HidP_Feature):
            length = ctypes.c_ushort(0)
            hid.HidP_GetButtonCaps(rtype, None, ctypes.byref(length), pp)
            if length.value == 0:
                out[rtype] = []
                continue
            arr = (HIDP_BUTTON_CAPS * length.value)()
            ok = hid.HidP_GetButtonCaps(rtype, arr, ctypes.byref(length), pp)
            out[rtype] = list(arr) if ok == HIDP_STATUS_SUCCESS else []
    finally:
        hid.HidD_FreePreparsedData(pp)
    return out


# --------------------------------------------------------------------------
# 报告描述符
# --------------------------------------------------------------------------
def get_report_descriptor(handle) -> bytes:
    buf = ctypes.create_string_buffer(4096)
    returned = wintypes.DWORD(0)
    ok = kernel32.DeviceIoControl(
        handle, IOCTL_HID_GET_REPORT_DESCRIPTOR, None, 0,
        buf, len(buf), ctypes.byref(returned), None)
    if not ok:
        return b""
    return buf.raw[:returned.value]


# --------------------------------------------------------------------------
# Feature Report
# --------------------------------------------------------------------------
def get_feature(handle, report_id: int, length: int) -> bytes | None:
    """读取 Feature Report；length 为含 report id 的完整长度。"""
    if length <= 0:
        return None
    buf = ctypes.create_string_buffer(length)
    buf[0] = bytes([report_id & 0xFF])
    if not hid.HidD_GetFeature(handle, buf, length):
        return None
    return buf.raw[:length]


def set_feature(handle, data: bytes) -> bool:
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    return bool(hid.HidD_SetFeature(handle, buf, len(data)))


def get_input_report(handle, report_id: int, length: int) -> bytes | None:
    if length <= 0:
        return None
    buf = ctypes.create_string_buffer(length)
    buf[0] = bytes([report_id & 0xFF])
    if not hid.HidD_GetInputReport(handle, buf, length):
        return None
    return buf.raw[:length]


def set_output_report(handle, data: bytes) -> bool:
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    return bool(hid.HidD_SetOutputReport(handle, buf, len(data)))


# --------------------------------------------------------------------------
# 同步/异步 读写（Input/Output Report）
# --------------------------------------------------------------------------
def read_input(handle, length: int, timeout_ms: int = 500, overlapped: bool = False) -> bytes | None:
    """读取一条 Input Report（阻塞最长 timeout_ms）。句柄需以对应模式打开。"""
    buf = ctypes.create_string_buffer(length)
    read = wintypes.DWORD(0)
    if overlapped:
        ev = kernel32.CreateEventW(None, True, False, None)
        try:
            ov = OVERLAPPED()
            ov.hEvent = ev
            ok = kernel32.ReadFile(handle, buf, length, ctypes.byref(read), ctypes.byref(ov))
            if not ok:
                err = ctypes.get_last_error()
                if err != 997:  # ERROR_IO_PENDING
                    return None
                rc = kernel32.WaitForSingleObject(ev, timeout_ms)
                if rc != WAIT_OBJECT_0:
                    kernel32.CancelIoEx(handle, ctypes.byref(ov))
                    return None
                if not kernel32.GetOverlappedResult(handle, ctypes.byref(ov), ctypes.byref(read), False):
                    return None
            return buf.raw[:read.value]
        finally:
            kernel32.CloseHandle(ev)
    else:
        ok = kernel32.ReadFile(handle, buf, length, ctypes.byref(read), None)
        if not ok:
            return None
        return buf.raw[:read.value]


def write_output(handle, data: bytes) -> bool:
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    written = wintypes.DWORD(0)
    return bool(kernel32.WriteFile(handle, buf, len(data), ctypes.byref(written), None))


# --------------------------------------------------------------------------
# 便捷：列出全部设备（供调试/枚举）
# --------------------------------------------------------------------------
def describe_interface(iface: HidInterface) -> str:
    return (f"{iface.vid_pid}  UP {iface.up_usage}  "
            f"in={iface.input_len} out={iface.output_len} feat={iface.feature_len}  "
            f"{iface.product or iface.friendly_name}")


if __name__ == "__main__":
    for i, d in enumerate(enumerate_interfaces()):
        print(f"[{i:02d}] {describe_interface(d)}")
        print(f"     path={d.path}")
