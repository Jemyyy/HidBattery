"""devices.py

两款无线外设的电量读取（黑爵 AK992 三模 RGB 键盘 / 黑爵 AJ179 双模鼠标）。

协议是通过实测逆向得到的，两种模式：

* 鼠标 AJ179（VID 249A / PID 5C2F，接口 mi_02，33 字节 in/out）
  查询式：向输出管道写 33 字节请求 [0x00, 0x10, 0x00 ...]，
  设备在 ~2ms 内回一条 33 字节记录：
      00 10 00 01 0B 'M''1''7''9' 00 00 95 01 00 64 01 ... 00 <ck>
      字节 5..8  = 型号 "M179"
      字节 11..12 = 0x0195 = 405 mAh 标称容量
      字节 14    = 0x64  = 电量百分比
      字节 15    = 充电标志
  注意：report id 不会被设备校验，写 0x00 即可。

* 键盘 AK992（VID 320F，两种连接方式共用同一套厂商通道）
    无线 2.4G 接收器 PID 50C3；有线 USB 直接连键盘 PID 50C2
    接口均为 mi_01&col04（UP 0xFF1C/0x0092，in/out 都是 64 字节，无 Feature Report）
  两种模式的电量帧完全一致：
      04 20 00 1A 06 00 00 00 51 00 ... 00
      字节 8 = 0x51 = 电量百分比
      字节 9 = 0x01（有线、充电） / 0x00（无线、放电）
  字节 9 在实测中与「连接方式」同时变化，无法排除它是「已连线」标志而非充电标志，
  故本工具只展示电量，不据此判断充电状态。
  另有每 ~1.9 秒一次的 04 AA 00 AA 00 00 00 00 FF 链路心跳（不含电量）。

  写通道必须用 WriteFile（对文件句柄直写 OUT 中断管道），**不能**用
  HidD_SetOutputReport —— 后者在这个厂商接口上是**静默空转**：Windows 返回成功、
  数据却从未到达设备，极易误判成「设备不应答 / 必须依赖官方驱动」。

  官方驱动在后台之所以「看起来必需」，真正原因是**它每 ~4.9 秒就向本通道写一次
  同样的 04 20 00 1A 06 查询帧**（另有 ~1.9 秒一次的 04 AA 00 AA 心跳），这些
  周期性流量把 2.4G 链路一直喂在唤醒态。链路一旦空闲休眠，键盘就**拒答**：要么
  完全不应答，要么回一帧 04 20 00 FF 06 ...（字节 3 由 0x1A 变 0xFF 的失败应答）。
  若只按前缀 [04 20] 匹配，就会把 0xFF 那帧当成「电量 0%」的假数据。
  故本工具做两件事：
    (1) 应答必须严格以 04 20 00 1A 06 开头，否则丢弃；
    (2) 自己按 ~5 秒周期写保活帧（keepalive_once），等效于官方驱动的喂链路行为，
        于是**不需要官方驱动进程**也能稳定读到电量（有线 50C2 / 2.4G 50C3 均实测）。
  工具刚启动时若键盘正处于休眠，头几次读会失败，按一下键盘任意键即恢复。
  注意：我们自己写的帧**唤不醒**已休眠的链路（实测连发 15 次只换来一个 FF 失败
  应答），只有键盘自身的活动能唤醒它 —— 所以保活的意义在「不让它睡」。
  固件对**未知**命令会原样回显（如 sub 改成 0x07/0x08 时收到的是自己发出的那帧），
  代码据此丢弃与请求完全相同的帧。

字节偏移可在调试界面里手动标定，标定结果写入 %APPDATA%\\HidBattery\\config.json。
"""

from __future__ import annotations

import json
import os
import threading
import time

import hidcore as H

APP_NAME = "HidBattery"


def config_dir() -> str:
    base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
    return base


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


# --------------------------------------------------------------------------
# 设备画像（协议参数可被 config.json 覆盖，便于手动标定）
# --------------------------------------------------------------------------
DEFAULT_PROTOCOLS = {
    "mouse": {
        "name": "黑爵 AJ179 鼠标",
        "short": "鼠标",
        "vid": 0x249A,
        "pid": 0x5C2F,
        # 匹配厂商接口：33 字节 in/out、UP 0001/0000
        "match_input_len": 33,
        "match_usage_page": 0x0001,
        "method": "query",
        "request_cmd": 0x10,
        "percent_offset": 14,
        # 实测：鼠标未充电时字节 15 仍为 0x01，故它并非充电标志，不解读
        "charging_offset": None,
        "capacity_offset": 11,
        "expect_header": "00 10",
        "expect_ascii": "M179",
        "timeout_ms": 600,
    },
    "keyboard": {
        "name": "黑爵 AK992 键盘",
        "short": "键盘",
        "vid": 0x320F,
        # 有线 USB 是 50C2，2.4G 接收器是 50C3，两者厂商通道完全同构。
        # 有线排在前面：插线时接收器通常还挂在机器上，而那时只有 50C2 这一路是活的。
        "pids": [0x50C2, 0x50C3],
        # 匹配厂商接口：64 字节 in/out、UP FF1C/0092
        "match_input_len": 64,
        "match_usage_page": 0xFF1C,
        # 用 WriteFile 把查询帧写进 OUT 中断管道，设备毫秒级应答（第 8 字节为电量）。
        # 注意不能用 HidD_SetOutputReport：该接口上它静默空转，数据到不了设备。
        "method": "query_frame",
        "request_frame": [0x04, 0x20, 0x00, 0x1A, 0x06, 0x00, 0x00, 0x00],
        "frame_len": 64,
        # 严格匹配到命令回显 04 20 00 1A 06：链路休眠时设备会回 04 20 00 FF 06 ...
        # 的失败应答，只按 [04 20] 匹配会把它解成「电量 0%」的假数据。
        "match_prefix": [0x04, 0x20, 0x00, 0x1A, 0x06],
        # 该通道空闲会休眠、且我们唤不醒它，只能靠 ~5 秒一次的保活帧不让它睡
        "keepalive": True,
        # 休眠时不必被动干等 6.5 秒
        "listen_fallback_ms": 800,
        "percent_offset": 8,
        "charging_offset": None,
        "capacity_offset": None,
        "timeout_ms": 800,
    },
}

ORDER = ("mouse", "keyboard")


def load_protocols() -> dict:
    """默认协议 + 用户在调试界面标定过的覆盖值。"""
    protos = {k: dict(v) for k, v in DEFAULT_PROTOCOLS.items()}
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            saved = json.load(f)
        for key, patch in (saved.get("protocols") or {}).items():
            if key in protos and isinstance(patch, dict):
                protos[key].update(patch)
    except Exception:
        pass
    return protos


def save_protocol(key: str, patch: dict) -> None:
    os.makedirs(config_dir(), exist_ok=True)
    data = {}
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data.setdefault("protocols", {}).setdefault(key, {}).update(patch)
    tmp = config_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, config_path())


def load_settings() -> dict:
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            return json.load(f).get("settings", {}) or {}
    except Exception:
        return {}


def save_settings(patch: dict) -> None:
    os.makedirs(config_dir(), exist_ok=True)
    data = {}
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data.setdefault("settings", {}).update(patch)
    tmp = config_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, config_path())


# --------------------------------------------------------------------------
# 读取结果
# --------------------------------------------------------------------------
class Reading:
    __slots__ = ("ok", "percent", "charging", "capacity", "raw", "error", "ts", "source")

    def __init__(self, ok=False, percent=None, charging=None, capacity=None,
                 raw=b"", error="", ts=None, source=""):
        self.ok = ok
        self.percent = percent
        self.charging = charging
        self.capacity = capacity
        self.raw = raw
        self.error = error
        self.ts = ts if ts is not None else time.time()
        self.source = source

    def __repr__(self):
        if self.ok:
            return f"<Reading {self.percent}% charging={self.charging}>"
        return f"<Reading FAILED {self.error}>"


# --------------------------------------------------------------------------
# 接口定位
# --------------------------------------------------------------------------
def pids(spec: dict) -> list:
    """协议接受的 PID 列表（键盘有线 50C2 / 无线 50C3 共用同一套通道）。"""
    ps = spec.get("pids")
    if ps:
        return [int(p) for p in ps]
    if spec.get("pid") is not None:
        return [int(spec["pid"])]
    return []


def pid_text(spec: dict) -> str:
    ps = pids(spec)
    if not ps:
        return "????"
    return "/".join(f"{p:04X}" for p in ps)


def _best(cands: list, spec: dict) -> H.HidInterface | None:
    """在一组同 PID 的接口里挑最匹配的那个。"""
    if not cands:
        return None
    # 先按 usage page / 输入长度精确匹配
    for d in cands:
        if spec.get("match_usage_page") is not None and d.usage_page == spec["match_usage_page"]:
            if spec.get("match_input_len") is None or d.input_len == spec["match_input_len"]:
                return d
    for d in cands:
        if spec.get("match_input_len") and d.input_len == spec["match_input_len"]:
            return d
    return cands[0]


def candidates(spec: dict, interfaces=None) -> list:
    """按 spec 里 PID 的顺序，给出每个 PID 上最匹配的接口。"""
    if interfaces is None:
        interfaces = H.enumerate_interfaces()
    out = []
    for pid in pids(spec):
        peer = [d for d in interfaces if d.vid == spec["vid"] and d.pid == pid]
        best = _best(peer, spec)
        if best is not None:
            out.append(best)
    return out


def locate(spec: dict, interfaces=None):
    cands = candidates(spec, interfaces)
    return cands[0] if cands else None


# --------------------------------------------------------------------------
# 请求帧 / 保活
# --------------------------------------------------------------------------
def request_bytes(spec: dict, iface: H.HidInterface | None = None,
                  length: int | None = None) -> bytes:
    """该协议要写进 OUT 中断管道的请求帧。"""
    if spec.get("method") == "query_frame":
        n = int(spec.get("frame_len") or length or (iface.input_len if iface else 0) or 64)
        frame = bytearray(n)
        head = bytes(spec.get("request_frame") or [])
        frame[:len(head)] = head
        return bytes(frame)
    return bytes([0x00, int(spec["request_cmd"]) & 0xFF]) + bytes(31)


def keepalive_write(spec: dict, path: str) -> bool:
    """按设备路径只写请求帧、不读结果：把厂商链路喂在唤醒态，避免它空闲休眠。

    键盘 2.4G 链路空闲后会休眠并对查询拒答；官方驱动正是靠每 ~4.9 秒写一次同样的
    查询帧把它一直喂醒。本工具复刻这一行为，于是不需要官方驱动进程。
    注意：链路已经睡了之后再连发也唤不醒它（实测连发 15 次只换来 FF 失败应答），
    保活的意义是「不让它睡」。

    调用方应缓存设备路径（本函数不做 enumerate —— 一次枚举约 0.1 秒 CPU，
    按 5 秒周期调用会顶爆「空闲 CPU 接近 0」的要求）。
    """
    hw = H.open_device(path, read_only=False)
    if not hw:
        return False
    try:
        return H.write_output(hw, request_bytes(spec))
    finally:
        H.close_device(hw)


# --------------------------------------------------------------------------
# 两种读取实现
# --------------------------------------------------------------------------
def _read_query(spec: dict, iface: H.HidInterface) -> Reading:
    """写请求 -> 读应答。读用 overlapped 句柄，写用普通句柄。"""
    cmd = request_bytes(spec, iface)
    hr = H.open_device(iface.path, read_only=True, overlapped=True)
    hw = H.open_device(iface.path, read_only=False)
    if not hr or not hw:
        if hr:
            H.close_device(hr)
        if hw:
            H.close_device(hw)
        return Reading(error="无法打开设备接口（可能被占用）")
    try:
        # 排空可能残留的旧报文，避免读到上一条应答
        while H.read_input(hr, 33, 120, overlapped=True):
            pass
        if not H.write_output(hw, cmd):
            return Reading(error="写请求失败")
        deadline = time.time() + spec.get("timeout_ms", 600) / 1000.0
        while True:
            left = int((deadline - time.time()) * 1000)
            if left <= 0:
                return Reading(error="设备未应答")
            r = H.read_input(hr, 33, left, overlapped=True)
            if not r:
                return Reading(error="设备未应答")
            head = spec.get("expect_header", "")
            if head:
                want = bytes(int(x, 16) for x in head.split())
                if tuple(r[:len(want)]) != tuple(want):
                    continue
            ascii_expect = spec.get("expect_ascii")
            if ascii_expect and ascii_expect.encode() not in r:
                continue
            return _decode(spec, r)
    finally:
        H.close_device(hr)
        H.close_device(hw)


def _read_listen(spec: dict, iface: H.HidInterface) -> Reading:
    """被动等待设备在该通道上的帧。"""
    hr = H.open_device(iface.path, read_only=True, overlapped=True)
    if not hr:
        return Reading(error="无法打开设备接口（可能被占用）")
    try:
        prefix = bytes(spec.get("match_prefix") or [])
        deadline = time.time() + spec.get("timeout_ms", 6500) / 1000.0
        last = b""
        while True:
            left = int((deadline - time.time()) * 1000)
            if left <= 0:
                return Reading(error="未等到设备应答（设备可能未连接或已休眠）", raw=last)
            r = H.read_input(hr, iface.input_len or 64, left, overlapped=True)
            if not r:
                return Reading(error="未等到设备应答（设备可能未连接或已休眠）", raw=last)
            last = r
            if prefix and not r.startswith(prefix):
                continue
            return _decode(spec, r)
    finally:
        H.close_device(hr)


def _fallback_listen(spec: dict, iface: H.HidInterface) -> Reading:
    """主动查询没拿到应答时，退回被动等该通道上的帧（放宽超时）。"""
    fb = dict(spec)
    fb["timeout_ms"] = spec.get("listen_fallback_ms", 6500)
    return _read_listen(fb, iface)


def _read_query_frame(spec: dict, iface: H.HidInterface) -> Reading:
    """查询式读取：用 WriteFile 把查询帧写进 OUT 中断管道，设备毫秒级应答。

    必须用 WriteFile（write_output），**不能**用 HidD_SetOutputReport —— 后者在这个
    厂商接口上是静默空转：Windows 返回成功，数据却从未到达设备，会让人误判成
    「设备不应答」。实测（2026-10-19，官方驱动已完全停止）：WriteFile 发出后
    0.001 秒即收到 04 20 00 1A 06 00 00 00 <电量> <标志> 的应答。

    固件对未知命令会原样回显，所以除前缀匹配外还要丢弃与请求完全相同的帧。
    """
    hr = H.open_device(iface.path, read_only=True, overlapped=True)
    if not hr:
        return Reading(error="无法打开设备接口（可能被占用）")
    hw = H.open_device(iface.path, read_only=False)
    try:
        if not hw:
            return _fallback_listen(spec, iface)
        n = int(spec.get("frame_len") or iface.input_len or 64)
        req = request_bytes(spec, iface, n)
        # 排空残留的旧广播，避免把上一帧当成这次应答
        while H.read_input(hr, n, 40, overlapped=True):
            pass
        if not H.write_output(hw, req):
            return _fallback_listen(spec, iface)
        prefix = bytes(spec.get("match_prefix") or [])
        deadline = time.time() + spec.get("timeout_ms", 800) / 1000.0
        while True:
            left = int((deadline - time.time()) * 1000)
            if left <= 0:
                return _fallback_listen(spec, iface)
            r = H.read_input(hr, n, left, overlapped=True)
            if not r:
                return _fallback_listen(spec, iface)
            if r == req:          # 固件对未知命令原样回显，不是应答
                continue
            if prefix and not r.startswith(prefix):
                continue
            return _decode(spec, r)
    finally:
        H.close_device(hr)
        if hw:
            H.close_device(hw)


def _decode(spec: dict, raw: bytes) -> Reading:
    off = int(spec.get("percent_offset") or 0)
    percent = None
    if 0 <= off < len(raw):
        percent = raw[off]
        if percent > 100:
            percent = None
    charging = None
    coff = spec.get("charging_offset")
    if coff is not None and 0 <= coff < len(raw):
        charging = bool(raw[coff])
    capacity = None
    capoff = spec.get("capacity_offset")
    if capoff is not None and capoff + 1 < len(raw):
        capacity = raw[capoff] | (raw[capoff + 1] << 8)
    if percent is None:
        return Reading(ok=False, raw=raw, error="已收到报文但电量字节无效")
    return Reading(ok=True, percent=percent, charging=charging,
                   capacity=capacity, raw=raw)


def _read(spec: dict, iface: H.HidInterface) -> Reading:
    if spec.get("method") == "query":
        return _read_query(spec, iface)
    if spec.get("method") == "query_frame":
        return _read_query_frame(spec, iface)
    return _read_listen(spec, iface)


def read_once(spec: dict, iface: H.HidInterface | None = None,
              interfaces=None) -> Reading:
    if iface is not None:
        return _read(spec, iface)
    cands = candidates(spec, interfaces)
    if not cands:
        return Reading(error="未找到设备（可能未连接或处于有线/蓝牙模式）")
    # 有线/无线两路依次尝试：插线时接收器可能还在，但那一路是静默的
    last = None
    for cand in cands:
        last = _read(spec, cand)
        if last.ok:
            return last
    return last


# --------------------------------------------------------------------------
# 后台刷新服务（界面永不阻塞）
# --------------------------------------------------------------------------
class BatteryService:
    """在后台线程按间隔刷新电量；用 Event.wait 休眠，空闲时 CPU 为 0。"""

    def __init__(self, interval_s: float = 60.0, protocols: dict | None = None,
                 debug: bool = False, auto_loop: bool = True):
        self.interval_s = float(interval_s)
        self.protocols = protocols or load_protocols()
        self.debug = debug
        self.auto_loop = auto_loop
        self._lock = threading.Lock()
        self._results: dict[str, Reading] = {
            k: Reading(error="尚未读取") for k in ORDER if k in self.protocols
        }
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._inflight = threading.Lock()
        self._keep_paths: dict[str, str] = {}   # 保活用的设备路径缓存（避免每 5 秒枚举）
        self.on_update = None          # callable(results: dict)
        self.refresh_count = 0

    # ---------------------------------------------------------------- 控制
    def start(self) -> None:
        """启动自带刷新循环（Event.wait 阻塞休眠，空闲不吃 CPU）。
        托盘模式会传 auto_loop=False，改由 SetTimer 驱动 refresh_async。"""
        if not self.auto_loop:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="battery", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t and t.is_alive():
            t.join(join_timeout)

    def set_interval(self, seconds: float) -> None:
        self.interval_s = max(5.0, float(seconds))
        self._wake.set()

    def request_refresh(self) -> None:
        """立即刷新一次（左键点击托盘时用）。"""
        self._wake.set()

    # ---------------------------------------------------------------- 取数
    def snapshot(self) -> dict:
        with self._lock:
            return {k: v for k, v in self._results.items()}

    def _log(self, *a) -> None:
        if self.debug:
            print("[电量]", *a, flush=True)

    # ---------------------------------------------------------------- 主循环
    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh_blocking()
            self._wake.clear()
            self._wake.wait(self.interval_s)

    def refresh_async(self, done=None) -> bool:
        """后台线程里刷新一次；已有刷新在跑则跳过。托盘 SetTimer 用它。"""
        if not self._inflight.acquire(blocking=False):
            return False

        def work():
            try:
                self.refresh_blocking()
            finally:
                self._inflight.release()
                if done:
                    try:
                        done()
                    except Exception:
                        pass

        threading.Thread(target=work, name="battery-once", daemon=True).start()
        return True

    def keepalive_async(self) -> bool:
        """后台线程里发一次保活帧（只写不读），把键盘的无线链路喂在唤醒态。

        设备路径缓存在 _keep_paths 里，正常情况下不做枚举（枚举一次约 0.1 秒 CPU，
        按 5 秒周期调用会顶爆「空闲 CPU 接近 0」的要求）；只有缓存缺失或写失败
        （换接口/换连接方式）时才重新枚举一次。
        与刷新共用同一把 inflight 锁：刷新正在跑时跳过本轮，避免两个线程抢设备。
        """
        if not self._inflight.acquire(blocking=False):
            return False

        def work():
            try:
                for key in ORDER:
                    spec = self.protocols.get(key)
                    if not spec or not spec.get("keepalive"):
                        continue
                    path = self._keep_paths.get(key)
                    if path and keepalive_write(spec, path):
                        continue
                    cand = locate(spec)
                    if cand is not None:
                        self._keep_paths[key] = cand.path
                        keepalive_write(spec, cand.path)
                    else:
                        self._keep_paths.pop(key, None)
            except Exception:
                pass
            finally:
                self._inflight.release()

        threading.Thread(target=work, name="battery-keepalive", daemon=True).start()
        return True

    def refresh_blocking(self) -> None:
        try:
            interfaces = H.enumerate_interfaces()
        except Exception as exc:            # pragma: no cover
            self._log("枚举失败:", exc)
            interfaces = []
        for key in ORDER:
            spec = self.protocols.get(key)
            if not spec:
                continue
            try:
                rd = read_once(spec, interfaces=interfaces)
            except Exception as exc:        # pragma: no cover
                rd = Reading(error=f"{type(exc).__name__}: {exc}")
            with self._lock:
                self._results[key] = rd
            if self.debug:
                if rd.ok:
                    self._log(f"{spec['short']}: {rd.percent}%  raw=",
                              " ".join(f"{b:02X}" for b in rd.raw[:16]))
                else:
                    self._log(f"{spec['short']}: 失败 - {rd.error}")
        self.refresh_count += 1
        if self.on_update:
            try:
                self.on_update(self.snapshot())
            except Exception:
                pass
