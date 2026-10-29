"""debugui.py

完整调试版界面：
    * 列出系统中所有 HID 设备/接口（VID/PID、用途页、in/out/feature 长度）
    * 转储报告描述符、探测 Report ID、转储 Feature Report
    * 试发命令（SetOutputReport / WriteFile 两种通道）并显示回包
      —— 已标定设备只放行白名单命令，其余需二次确认后才发送
    * 被动监听 Input 报文
    * 手动标定电量协议（命令字 / 匹配前缀 / 电量字节偏移），保存后即时生效

所有耗时操作都放后台线程，结果显示经队列回到界面线程，界面不卡。
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import hidcore as H
import devices as D

BG = "#f4f5f7"
TXT = "#1f2430"
SUBTLE = "#7b8494"
MONO = ("Consolas", 9)


def parse_hex(text: str) -> bytes:
    t = (text or "").replace(",", " ").replace("0x", " ").replace("0X", " ")
    out = []
    for chunk in t.split():
        for part in (chunk[i:i + 2] for i in range(0, len(chunk), 2)):
            out.append(int(part, 16))
    return bytes(out)


class DebugUI:
    def __init__(self, service, hooks, master=None):
        self.service = service
        self.hooks = hooks
        self.master = master
        self._own_root = master is None
        self.root = None
        self.ifaces: list[H.HidInterface] = []
        self._q = queue.Queue()
        self._drain_job = None
        self._busy = False

    # ------------------------------------------------------------ 构建
    def build(self):
        if self.root is not None:
            return
        self.root = tk.Tk() if self._own_root else tk.Toplevel(self.master)
        self.root.title("HID 电量工具 · 完整调试版")
        self.root.configure(bg=BG)
        self.root.geometry("1080x720")
        try:
            self.root.withdraw()
        except Exception:
            pass

        top = tk.Frame(self.root, bg=BG, padx=10, pady=8)
        top.pack(fill="x")
        tk.Label(top, text="HID 设备/接口", bg=BG, fg=TXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        tk.Button(top, text="刷新枚举", command=self.refresh_list).pack(side="right")
        tk.Button(top, text="清空输出",
                  command=lambda: (self.out.delete("1.0", "end"))).pack(side="right", padx=6)

        body = tk.Frame(self.root, bg=BG, padx=10)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="y")
        self.listbox = tk.Listbox(left, width=54, font=MONO, activestyle="none",
                                  selectmode="browse", exportselection=False)
        self.listbox.pack(side="left", fill="y")
        sb = tk.Scrollbar(left, command=self.listbox.yview)
        sb.pack(side="left", fill="y")
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.bind("<<ListboxSelect>>", lambda e: self.show_detail())

        right = tk.Frame(body, bg=BG, padx=10)
        right.pack(side="left", fill="both", expand=True)

        acts = tk.Frame(right, bg=BG)
        acts.pack(fill="x")
        for text, cmd in (
            ("转储报告描述符", self.dump_descriptor),
            ("探测 Report ID", self.probe_report_ids),
            ("转储 Feature Report", self.dump_feature),
            ("监听 Input 报文(8s)", self.listen_input),
            ("读取电量", self.read_battery),
        ):
            tk.Button(acts, text=text, command=cmd).pack(side="left", padx=(0, 5), pady=2)

        cmdrow = tk.Frame(right, bg=BG, pady=4)
        cmdrow.pack(fill="x")
        tk.Label(cmdrow, text="试发命令(十六进制)", bg=BG, fg=TXT).pack(side="left")
        self.cmd_entry = tk.Entry(cmdrow, font=MONO, width=34)
        self.cmd_entry.pack(side="left", padx=6)
        self.cmd_entry.insert(0, "00 10")
        tk.Button(cmdrow, text="SetOutputReport 发送",
                  command=lambda: self.send_cmd("setoutput")).pack(side="left", padx=(0, 4))
        tk.Button(cmdrow, text="WriteFile 发送",
                  command=lambda: self.send_cmd("write")).pack(side="left")
        tk.Label(right, text="⚠ 写命令可能改到板载配置（DPI / 回报率 / 灯光）且持久生效；"
                             "已标定设备仅放行白名单命令，其余需二次确认。",
                 bg=BG, fg="#b3541e", anchor="w",
                 font=("Microsoft YaHei UI", 8)).pack(fill="x")

        self.out = tk.Text(right, font=MONO, bg="#11151c", fg="#d8e0ea",
                           insertbackground="#d8e0ea", wrap="none")
        self.out.pack(fill="both", expand=True)

        self._build_calibrate(right)

        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.refresh_list()
        self._drain_job = self.root.after(120, self._drain)

    def _build_calibrate(self, parent):
        box = tk.LabelFrame(parent, text="手动标定电量协议", bg=BG, fg=TXT,
                            padx=8, pady=6)
        box.pack(fill="x", pady=(8, 8))

        row1 = tk.Frame(box, bg=BG)
        row1.pack(fill="x")
        tk.Label(row1, text="设备", bg=BG, fg=TXT).pack(side="left")
        self.cal_dev = tk.StringVar(value="mouse")
        cb = ttk.Combobox(row1, textvariable=self.cal_dev, width=9, state="readonly",
                          values=["mouse", "keyboard"])
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._load_cal_fields())

        tk.Label(row1, text="读取方式", bg=BG, fg=TXT).pack(side="left", padx=(10, 0))
        self.cal_method = tk.StringVar(value="query")
        ttk.Combobox(row1, textvariable=self.cal_method, width=8, state="readonly",
                     values=["query", "listen"]).pack(side="left", padx=6)

        tk.Label(row1, text="命令字(hex)", bg=BG, fg=TXT).pack(side="left", padx=(10, 0))
        self.cal_cmd = tk.Entry(row1, font=MONO, width=6)
        self.cal_cmd.pack(side="left", padx=4)

        tk.Label(row1, text="广播前缀(hex)", bg=BG, fg=TXT).pack(side="left", padx=(10, 0))
        self.cal_prefix = tk.Entry(row1, font=MONO, width=12)
        self.cal_prefix.pack(side="left", padx=4)

        row2 = tk.Frame(box, bg=BG, pady=4)
        row2.pack(fill="x")
        tk.Label(row2, text="电量字节偏移", bg=BG, fg=TXT).pack(side="left")
        self.cal_off = tk.Entry(row2, font=MONO, width=5)
        self.cal_off.pack(side="left", padx=4)
        tk.Button(row2, text="采样一份报文", command=self.cal_sample).pack(side="left", padx=8)
        tk.Button(row2, text="保存并生效", command=self.cal_save).pack(side="left")

        self.cal_hint = tk.Label(box, text="", bg=BG, fg=SUBTLE, anchor="w",
                                 justify="left", font=("Microsoft YaHei UI", 8))
        self.cal_hint.pack(fill="x")
        self._load_cal_fields()

    def _load_cal_fields(self):
        key = self.cal_dev.get()
        spec = self.service.protocols.get(key, {})
        self.cal_method.set(spec.get("method", "query"))
        self.cal_cmd.delete(0, "end")
        if spec.get("request_cmd") is not None:
            self.cal_cmd.insert(0, f"{spec['request_cmd']:02X}")
        self.cal_prefix.delete(0, "end")
        if spec.get("match_prefix"):
            self.cal_prefix.insert(0, " ".join(f"{b:02X}" for b in spec["match_prefix"]))
        self.cal_off.delete(0, "end")
        self.cal_off.insert(0, str(spec.get("percent_offset", 0)))
        self.cal_hint.configure(text=f"当前：{spec.get('name', key)}　"
                                     f"（每次采样/保存都会写 config.json，立即生效）")

    # ------------------------------------------------------------ 输出
    def _say(self, text):
        self._q.put(text if text.endswith("\n") else text + "\n")

    def _say_now(self, text):
        if self.root is None:
            return
        self.out.insert("end", text if text.endswith("\n") else text + "\n")
        self.out.see("end")

    def _drain(self):
        try:
            while True:
                self._say_now(self._q.get_nowait())
        except queue.Empty:
            pass
        if self.root is not None:
            self._drain_job = self.root.after(120, self._drain)

    def _run(self, fn, name):
        if self._busy:
            self._say(f"[跳过] 上一个操作还在进行中")
            return
        self._busy = True

        def work():
            try:
                fn()
            except Exception as exc:
                self._say(f"[{name}] 异常：{type(exc).__name__}: {exc}")
            finally:
                self._busy = False

        threading.Thread(target=work, name=name, daemon=True).start()

    # ------------------------------------------------------------ 枚举
    def selected(self):
        sel = self.listbox.curselection()
        if not sel:
            return None
        idx = sel[0]
        if 0 <= idx < len(self.ifaces):
            return self.ifaces[idx]
        return None

    def refresh_list(self):
        def work():
            self._say("正在枚举 HID 接口…")
            items = H.enumerate_interfaces()
            self.ifaces = items
            if self.root is not None:
                self.root.after(0, lambda: self._fill_list(items))
            self._say(f"共 {len(items)} 个接口。")
        self._run(work, "枚举")

    def _fill_list(self, items):
        self.listbox.delete(0, "end")
        for i, d in enumerate(items):
            tag = ""
            if d.vid == 0x320F:
                tag = "  <键盘"
            elif d.vid == 0x249A:
                tag = "  <鼠标"
            self.listbox.insert(
                "end",
                f"[{i:02d}] {d.vid_pid} UP{d.up_usage} "
                f"i{d.input_len:>3} o{d.output_len:>3} f{d.feature_len:>3} "
                f"{(d.product or d.friendly_name)[:22]:<22}{tag}")

    def show_detail(self):
        d = self.selected()
        if not d:
            return
        self._say_now("")
        self._say_now("=" * 78)
        self._say_now(f"路径       : {d.path}")
        self._say_now(f"实例       : {d.instance_id}")
        self._say_now(f"VID:PID    : {d.vid_pid}   版本: {d.version:04X}")
        self._say_now(f"用途页/用途 : {d.up_usage}")
        self._say_now(f"报文长度   : in={d.input_len} out={d.output_len} feature={d.feature_len}")
        self._say_now(f"产品       : {d.product}")
        self._say_now(f"厂商       : {d.manufacturer}")
        self._say_now(f"序列号     : {d.serial}")
        self._say_now(f"友好名     : {d.friendly_name}")

    # ------------------------------------------------------------ 转储
    def dump_descriptor(self):
        d = self.selected()
        if not d:
            self._say("请先在左侧选择一个接口")
            return

        def work():
            self._say("")
            self._say(f"--- 报告描述符：{d.vid_pid} UP {d.up_usage} ---")
            h = H.open_device(d.path)
            if not h:
                self._say("打开设备失败")
                return
            try:
                desc = H.get_report_descriptor(h)
                if not desc:
                    self._say("IOCTL_HID_GET_REPORT_DESCRIPTOR 返回空（该接口不提供描述符）")
                    return
                self._say(f"描述符 {len(desc)} 字节：")
                for off in range(0, len(desc), 16):
                    chunk = desc[off:off + 16]
                    hexs = " ".join(f"{b:02X}" for b in chunk)
                    self._say(f"  {off:04X}  {hexs}")
                self._say("提示：厂商自定义接口(UP FFxx)通常只用裸字节报文，HidP_* 无法解析。")
            finally:
                H.close_device(h)
        self._run(work, "描述符")

    def probe_report_ids(self):
        d = self.selected()
        if not d:
            self._say("请先在左侧选择一个接口")
            return

        def work():
            self._say("")
            self._say(f"--- 探测 Feature / Input Report ID：{d.vid_pid} UP {d.up_usage} ---")
            h = H.open_device(d.path)
            if not h:
                self._say("打开设备失败")
                return
            try:
                caps = H.get_caps(h)
                if caps:
                    self._say(f"caps: in={caps.InputReportByteLength} "
                              f"out={caps.OutputReportByteLength} "
                              f"feature={caps.FeatureReportByteLength} "
                              f"input_values={caps.NumberInputValueCaps} "
                              f"feature_values={caps.NumberFeatureValueCaps}")
                    vc = H.list_value_caps(h)
                    for rt, nm in ((0, "Input"), (1, "Output"), (2, "Feature")):
                        for c in vc.get(rt, []):
                            self._say(f"  value cap {nm}: ID=0x{c.ReportID:02X} "
                                      f"UP=0x{c.LinkUsagePage:04X} Usage=0x{c.Usage:04X} "
                                      f"bits={c.BitSize} count={c.ReportCount} "
                                      f"log=[{c.LogicalMin},{c.LogicalMax}]")
                flen = max(1, d.feature_len)
                hr = H.open_device(d.path, read_only=True)
                hh = hr or h
                try:
                    for rid in range(0, 16):
                        for ln in {flen, 65, 34, 33, 9, 8}:
                            if ln <= 0:
                                continue
                            r = H.get_feature(hh, rid, ln)
                            if r:
                                self._say(f"  Feature id=0x{rid:02X} len={ln} -> "
                                          + " ".join(f"{b:02X}" for b in r[:24]))
                                break
                finally:
                    if hr:
                        H.close_device(hr)
            finally:
                H.close_device(h)
        self._run(work, "探测")

    def dump_feature(self):
        d = self.selected()
        if not d:
            self._say("请先在左侧选择一个接口")
            return
        rid_txt = self.cmd_entry.get().strip().split()
        rid = int(rid_txt[0], 16) if rid_txt else 0

        def work():
            self._say("")
            self._say(f"--- Feature Report id=0x{rid:02X}：{d.vid_pid} ---")
            ln = d.feature_len or 65
            h = H.open_device(d.path, read_only=True)
            if not h:
                h = H.open_device(d.path, read_only=False)
            if not h:
                self._say("打开设备失败")
                return
            try:
                got = False
                for cand in (ln, 65, 34, 33, 9, 8):
                    if cand <= 0:
                        continue
                    r = H.get_feature(h, rid, cand)
                    if r:
                        got = True
                        self._say(f"  len={cand} -> " + " ".join(f"{b:02X}" for b in r))
                if not got:
                    self._say("  无数据（接口不支持 Feature Report）")
            finally:
                H.close_device(h)
        self._run(work, "Feature")

    def listen_input(self):
        d = self.selected()
        if not d:
            self._say("请先在左侧选择一个接口")
            return

        def work():
            self._say("")
            self._say(f"--- 被动监听 Input 报文 (8s)：{d.vid_pid} UP {d.up_usage} ---")
            h = H.open_device(d.path, read_only=True, overlapped=True)
            if not h:
                h = H.open_device(d.path, read_only=False, overlapped=True)
            if not h:
                self._say("打开设备失败")
                return
            seen = {}
            t0 = time.time()
            try:
                while time.time() - t0 < 8.0:
                    r = H.read_input(h, d.input_len or 33, 400, overlapped=True)
                    if r:
                        k = bytes(r)
                        seen.setdefault(k, [time.time() - t0, 0])
                        seen[k][1] += 1
            finally:
                H.close_device(h)
            self._say(f"  共 {len(seen)} 种报文：")
            for k, (ts, c) in sorted(seen.items(), key=lambda kv: kv[1][0]):
                self._say(f"    x{c:<3} +{ts:6.2f}s  " + " ".join(f"{b:02X}" for b in k))
        self._run(work, "监听")

    def _safe_rule(self, d):
        """返回该接口的发送白名单校验函数；返回 None = 未标定设备，不做限制。

        背景：早期探测曾向鼠标厂商接口发过 00 01 ~ 00 20 的一串命令（载荷全 0），
        若其中某条是写配置类命令，就可能把 DPI 等参数写成最低值且持久生效。
        所以对已标定设备只放行「纯查询」命令，其余一律二次确认。
        """
        for spec in self.service.protocols.values():
            want = spec.get("pids") or [spec.get("pid")]
            if spec.get("vid") != d.vid or d.pid not in want:
                continue
            if spec.get("method") != "query":
                return lambda full: False           # 只被动接收，不主动发送
            cmd = int(spec.get("request_cmd", 0)) & 0xFF
            return lambda full: (full[0] == 0x00 and full[1] == cmd
                                 and not any(full[2:]))
        return None

    def send_cmd(self, how):
        d = self.selected()
        if not d:
            self._say("请先在左侧选择一个接口")
            return
        try:
            payload = parse_hex(self.cmd_entry.get())
        except ValueError as exc:
            self._say(f"命令解析失败：{exc}")
            return
        if not payload:
            self._say("请输入十六进制命令")
            return

        full = payload.ljust(d.output_len or 33, b"\x00")
        rule = self._safe_rule(d)
        if rule is not None and not rule(full):
            self._say("")
            self._say(f"⚠ 拦截：{payload.hex(' ').upper()} 不在 "
                      f"{d.vid:04X}:{d.pid:04X} 的安全白名单内")
            if not messagebox.askyesno(
                    "确认发送未标定命令",
                    f"目标设备是已标定设备（{d.vid:04X}:{d.pid:04X}）。\n\n"
                    f"命令：{payload.hex(' ').upper()}\n\n"
                    "该命令不在安全白名单内，可能写入设备的板载配置"
                    "（DPI、回报率、灯光等），且持久生效，"
                    "通常只能用官方驱动改回来。\n\n"
                    "确定要发送吗？",
                    icon="warning", default="no"):
                self._say("  已取消发送")
                return
            self._say("  用户已确认，照发")

        def work():
            self._say("")
            self._say(f"--- 试发命令 ({how})：{payload.hex(' ').upper()} ---")
            hr = H.open_device(d.path, read_only=True, overlapped=True)
            # 写句柄必须用同步模式：write_output 走的是同步 WriteFile，
            # 在 FILE_FLAG_OVERLAPPED 句柄上会直接失败（GetLastError=87）
            hw = H.open_device(d.path, read_only=False, overlapped=False)
            if not hr or not hw:
                self._say(f"打开设备失败 (read={bool(hr)} write={bool(hw)})")
                if hr:
                    H.close_device(hr)
                if hw:
                    H.close_device(hw)
                return
            try:
                while H.read_input(hr, d.input_len or 33, 80, overlapped=True):
                    pass
                if how == "setoutput":
                    ok = H.set_output_report(hw, full)
                    self._say(f"  SetOutputReport(len={len(full)}) -> {ok}")
                else:
                    ok = H.write_output(hw, full)
                    self._say(f"  WriteFile(len={len(full)}) -> {ok}")
                t0 = time.time()
                got = 0
                while time.time() - t0 < 0.7:
                    left = int((0.7 - (time.time() - t0)) * 1000)
                    if left <= 0:
                        break
                    r = H.read_input(hr, d.input_len or 33, left, overlapped=True)
                    if r:
                        got += 1
                        self._say(f"    +{(time.time()-t0)*1000:6.1f}ms  "
                                  + " ".join(f"{b:02X}" for b in r))
                if not got:
                    self._say("    无回包")
            finally:
                H.close_device(hr)
                H.close_device(hw)
        self._run(work, f"发送-{how}")

    def read_battery(self):
        def work():
            self._say("")
            self._say("--- 按当前协议读取两台设备电量 ---")
            for key in ("mouse", "keyboard"):
                spec = self.service.protocols.get(key)
                if not spec:
                    continue
                rd = D.read_once(spec)
                self._say(f"  {spec['short']}: ok={rd.ok} 电量={rd.percent} "
                          f"容量={rd.capacity} 错误={rd.error or '-'}")
                self._say(f"    报文: " + " ".join(f"{b:02X}" for b in rd.raw[:24]))
        self._run(work, "读电量")

    # ------------------------------------------------------------ 标定
    def cal_sample(self):
        key = self.cal_dev.get()
        spec = dict(self.service.protocols.get(key, {}))

        def work():
            self._say("")
            self._say(f"--- 采样 [{key}] ---")
            rd = D.read_once(spec)
            if not rd.raw:
                self._say(f"  未取到报文：{rd.error}")
                return
            raw = rd.raw
            self._say("  报文（每字节标出下标与十进制值）：")
            for off in range(0, min(len(raw), 32), 8):
                seg = raw[off:off + 8]
                line = "   " + "  ".join(f"[{off+i:2d}]{b:02X}={b:>3}" for i, b in enumerate(seg))
                self._say(line)
            cands = [(b, i) for i, b in enumerate(raw) if 0 < b <= 100]
            appear = sorted(cands, key=lambda t: -t[0])[:8]
            self._say("  <=100 的候选字节（值, 下标）："
                      + ", ".join(f"{v}@{i}" for v, i in appear))
            self._say("  把最像电量的那个下标填进「电量字节偏移」，然后点「保存并生效」。")
        self._run(work, "标定采样")

    def cal_save(self):
        key = self.cal_dev.get()
        patch = {"method": self.cal_method.get()}
        try:
            if self.cal_cmd.get().strip():
                patch["request_cmd"] = int(self.cal_cmd.get().strip(), 16)
            if self.cal_prefix.get().strip():
                patch["match_prefix"] = list(parse_hex(self.cal_prefix.get()))
            patch["percent_offset"] = int(self.cal_off.get().strip())
        except ValueError as exc:
            self._say(f"参数解析失败：{exc}")
            return
        try:
            D.save_protocol(key, patch)
        except OSError as exc:
            self._say(f"写入配置失败：{exc}")
            return
        spec = self.service.protocols[key]
        spec.update(patch)
        self._say(f"[标定] {key} 已更新并保存：{patch}")
        self._say("  正在按新参数试读一次…")
        rd = D.read_once(spec)
        self._say(f"  结果：ok={rd.ok} 电量={rd.percent} 错误={rd.error or '-'}")

    # ------------------------------------------------------------ 显隐
    def show(self):
        if self.root is None:
            self.build()
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:
            pass

    def hide(self):
        if self.root is not None:
            self.root.withdraw()

    def destroy(self):
        if self._drain_job and self.root is not None:
            try:
                self.root.after_cancel(self._drain_job)
            except Exception:
                pass
            self._drain_job = None
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
