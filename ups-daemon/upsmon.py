#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
upsmon.py — Waveshare UPS Power Module (C) 监控 + 低电自动关机守护进程

目标硬件: Jetson Orin NX (nx3), INA219 @ i2c-7 addr 0x41
依赖: python3-smbus2 (已随 JetPack 提供), 标准库

设计要点
  1. INA219 的 CONFIG/CAL 寄存器是 RAM 态, 每次上电必须重写 -> 启动即配置。
  2. 判据分两层: ①"是否在电池供电"用电流符号判定; ②"是否该关机"用平滑电压判定。
     绝不能只用电压百分比: 模块的百分比是电压线性插值, 锂电平缓段严重偏高。
  3. 死区(deadband)设计: AC 在线时电池支路电流 >= 0(充电或浮充), 电池供电时
     必然 < 0 且幅值等于系统负载(~0.3-1.5A)。故以 -120mA 为死区, 余量 >500mA。
  4. 触发关机需连续满足 N 个采样点(默认 16s), 且电压用 EMA 平滑, 抗负载压降毛刺。
  5. 传感器故障(I2C 连续读失败)默认"不关机": 误关机需现场按电源键, 代价高于晚关机。
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque

try:
    from smbus2 import SMBus
except ImportError:  # 惰性化: --simulate-voltage 等不碰硬件的路径无需 smbus2
    SMBus = None

# ---------------------------------------------------------------- INA219 常量
REG_CONFIG = 0x00
REG_SHUNT = 0x01
REG_BUS = 0x02
REG_POWER = 0x03
REG_CURRENT = 0x04
REG_CAL = 0x05

BUS_VOLTAGE_RANGE_16V = 0x00
GAIN_DIV2_80MV = 0x01
ADCRES_12BIT_32S = 0x0D
MODE_SANDBVOLT_CONTINUOUS = 0x07

CAL_VALUE = 26868          # Cal = trunc(0.04096 / (CurrentLSB * RShunt)), RShunt=0.01Ω
CURRENT_LSB_MA = 0.1524    # mA / bit
POWER_LSB_W = 0.003048     # W / bit
PERCENT_V_EMPTY = 9.0      # 模块定义: 3S 放到 9.0V 记为 0%
PERCENT_V_FULL = 12.6      # 模块定义: 充到 12.6V 记为 100%
PERCENT_SPAN_V = PERCENT_V_FULL - PERCENT_V_EMPTY   # 3.6V

# ---------------------------------------------------------------- 默认配置
DEFAULTS = {
    "bus": 7,
    "addr": 0x41,
    "interval": 2.0,          # 采样周期(s)
    "ema_alpha": 0.25,        # 电压 EMA 系数(2s 周期 -> 时间常数约 8s)
    "deadband_ma": 120.0,     # |放电| 超过该值才认定"在电池供电"
    "battery_confirm": 5,     # 连续 N 点确认"电池供电"(10s)
    "ac_confirm": 3,          # 连续 N 点确认"AC 恢复"(6s)
    "warn_v": 10.8,           # 告警
    "low_v": 10.2,            # 低电: 降功耗 + 疏散
    "shutdown_v": 9.9,        # 触发有序关机
    "crit_v": 9.3,            # 急停: 立即关机
    "shutdown_sustain": 8,    # 达到 shutdown_v 后需持续 N 点(16s)
    "crit_sustain": 2,
    "low_power_mode": 1,      # 触发低电时 nvpmodel 档位(1=10W); 0=不切换
    "hook_dir": "/usr/local/lib/ups-monitor/pre-shutdown.d",
    "low_dir": "/usr/local/lib/ups-monitor/low-battery.d",
    "hook_timeout": 20,
    "state_file": "/run/ups-monitor/status.json",
    "sensor_lost_limit": 15,  # 连续读失败点数 -> SENSOR_LOST
    "shutdown_on_sensor_loss": 0,
    "dry_run": 0,             # 1 = 只记录, 绝不真的 poweroff
    "notify_cmd": "",         # 可选: 外部通知命令, 收到 JSON 于 stdin
    "http_enabled": 0,
    "http_port": 8088,
    "http_bind": "0.0.0.0",
}


def log(level, msg):
    print("[upsmon] %-5s %s" % (level, msg), flush=True)


def load_conf(path):
    conf = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key, val = key.strip(), val.split("#")[0].strip()
                if key not in DEFAULTS:
                    log("WARN", "忽略未知配置项: %s" % key)
                    continue
                ref = DEFAULTS[key]
                try:
                    conf[key] = int(val, 0) if isinstance(ref, int) else float(val)
                except ValueError:
                    conf[key] = val
    return conf


class INA219:
    """最小实现: 只为读取该 UPS 模块的电池支路。"""

    def __init__(self, bus_num, addr):
        if SMBus is None:
            raise RuntimeError("需要 python3-smbus2 (sudo apt install python3-smbus2)")
        self.bus_num = bus_num
        self.addr = addr
        self.bus = SMBus(bus_num)
        self.configure()

    def configure(self):
        """必须在每次上电后调用: CONFIG/CAL 均为 RAM 态。"""
        self.write16(REG_CAL, CAL_VALUE)
        cfg = ((BUS_VOLTAGE_RANGE_16V << 13) | (GAIN_DIV2_80MV << 11) |
               (ADCRES_12BIT_32S << 7) | (ADCRES_12BIT_32S << 3) |
               MODE_SANDBVOLT_CONTINUOUS)
        self.write16(REG_CONFIG, cfg)

    def read16(self, reg):
        d = self.bus.read_i2c_block_data(self.addr, reg, 2)
        return (d[0] << 8) | d[1]

    def read_s16(self, reg):
        v = self.read16(reg)
        return v - 65536 if v > 32767 else v

    def write16(self, reg, value):
        self.bus.write_i2c_block_data(self.addr, reg, [(value >> 8) & 0xFF, value & 0xFF])

    def sample(self):
        """返回 (bus_v, current_a, power_w, raw_bus, raw_current)。"""
        bus_v = (self.read16(REG_BUS) >> 3) * 0.004
        current_a = self.read_s16(REG_CURRENT) * CURRENT_LSB_MA / 1000.0
        power_w = self.read_s16(REG_POWER) * POWER_LSB_W
        return bus_v, current_a, power_w

    def close(self):
        try:
            self.bus.close()
        except Exception:
            pass


class UPSMonitor:
    def __init__(self, conf):
        self.c = conf
        self.ina = None
        self.ema_v = None
        self.ema_i = None
        self.streaks = {"batt": 0, "ac": 0, "shutdown": 0, "crit": 0}
        self.sensor_fail = 0
        self.state = "INIT"
        self.on_battery = False
        self.decided = False
        self.history = deque(maxlen=450)      # (ts, ema_v, current_a) 约 15 分钟
        self.low_actions_done = False
        self.shutdown_fired = False
        self.running = True
        self.last = {}
        self.http_server = None

    # ------------------------------------------------------------ 采样与平滑
    def connect(self):
        self.ina = INA219(self.c["bus"], int(self.c["addr"]))
        log("INFO", "INA219 已配置: bus=i2c-%d addr=0x%02X CAL=%d CONFIG=0x%04X" % (
            self.c["bus"], int(self.c["addr"]), CAL_VALUE, self.ina.read16(REG_CONFIG)))

    def step(self):
        """一次采样 + 状态推进, 返回本次状态字典。"""
        try:
            bus_v, current_a, power_w = self.ina.sample()
            self.sensor_fail = 0
        except OSError as exc:
            self.sensor_fail += 1
            log("WARN", "I2C 读取失败(%d/%d): %s" % (self.sensor_fail, self.c["sensor_lost_limit"], exc))
            try:
                self.ina.close()
            except Exception:
                pass
            time.sleep(1)
            try:
                self.connect()
            except Exception as exc2:
                log("WARN", "重连失败: %s" % exc2)
            if self.sensor_fail >= self.c["sensor_lost_limit"]:
                self.state = "SENSOR_LOST"
                if self.c["shutdown_on_sensor_loss"]:
                    log("ERROR", "传感器丢失且配置要求关机 -> 执行关机")
                    self.do_shutdown("sensor-lost")
            return self.status()

        a = self.c["ema_alpha"]
        self.ema_v = bus_v if self.ema_v is None else self.ema_v + a * (bus_v - self.ema_v)
        self.ema_i = current_a if self.ema_i is None else self.ema_i + a * (current_a - self.ema_i)

        percent = max(0.0, min(100.0, (self.ema_v - PERCENT_V_EMPTY) / PERCENT_SPAN_V * 100.0))
        s = self.streaks

        # ---- ① 供电来源判定(电流符号 + 死区 + 连续确认) ----
        # 冷启动时无历史可保护, 首采样直接采纳瞬时判定, 避免前 10s 标签自相矛盾;
        # 之后的状态翻转才需要连续确认(迟滞)。
        batt_now = self.ema_i < -self.c["deadband_ma"] / 1000.0
        if not self.decided:
            self.on_battery = batt_now
            self.decided = True
            log("INFO", "初始判定: %s (I=%+.3fA, V=%.2fV)" % (
                "电池供电" if batt_now else "AC 供电", self.ema_i, self.ema_v))
        elif batt_now:
            s["batt"] += 1
            s["ac"] = 0
            if not self.on_battery and s["batt"] >= self.c["battery_confirm"]:
                self.on_battery = True
                # 丢掉上一段样本: AC 段电压(≈12.2V 平稳)与切换瞬间的陡降混进
                # 同一回归窗口, 会让斜率被放大数十倍 -> ETA 严重低估(实测 20min vs 真实 8h)。
                # 清空后 ETA 需重新积累 ~30s 有效放电数据才出数, 与设计意图一致。
                self.history.clear()
                log("INFO", ">>> 切至电池供电 (I=%.3fA, V=%.2fV)" % (self.ema_i, self.ema_v))
                self.notify("on-battery")
        else:
            s["ac"] += 1
            s["batt"] = 0
            if self.on_battery and s["ac"] >= self.c["ac_confirm"]:
                self.on_battery = False
                self.shutdown_fired = False
                self.low_actions_done = False
                s["shutdown"] = 0
                s["crit"] = 0
                log("INFO", "<<< AC 恢复 (I=%.3fA, V=%.2fV)" % (self.ema_i, self.ema_v))
                self.notify("ac-restored")

        # ---- ② 电量分级(仅在电池供电时才有意义)
        if not self.on_battery:
            if self.ema_v <= self.c["warn_v"]:
                self.state = "AC_BATT_LOW"      # AC 在但电池不充 -> 适配器功率不足/异常
            else:
                self.state = "AC"
        else:
            if self.ema_v <= self.c["crit_v"]:
                s["crit"] += 1
                self.state = "CRIT"
                if s["crit"] >= self.c["crit_sustain"]:
                    if not self.shutdown_fired:
                        log("ERROR", "电压 %.2fV <= 急停阈值 %.2fV -> 立即关机" % (
                            self.ema_v, self.c["crit_v"]))
                    self.do_shutdown("critical-voltage")
            elif self.ema_v <= self.c["shutdown_v"]:
                s["shutdown"] += 1
                self.state = "SHUTDOWN_PENDING"
                if s["shutdown"] >= self.c["shutdown_sustain"]:
                    if not self.shutdown_fired:
                        log("ERROR", "电压 %.2fV <= 关机阈值 %.2fV 已持续 %.0fs -> 有序关机" % (
                            self.ema_v, self.c["shutdown_v"], s["shutdown"] * self.c["interval"]))
                    self.do_shutdown("low-voltage")
            elif self.ema_v <= self.c["low_v"]:
                self.state = "LOW"
                s["shutdown"] = 0
                if not self.low_actions_done:
                    self.on_low_battery()
            elif self.ema_v <= self.c["warn_v"]:
                self.state = "WARN"
                s["shutdown"] = 0
            else:
                self.state = "BATTERY"
                s["shutdown"] = 0

        # 只积累"电池放电"段样本: ETA 是放电斜率外推, AC 段(充电/浮充)数据无意义,
        # 且会把回归斜率拉平甚至反号。
        if self.on_battery:
            self.history.append((time.time(), self.ema_v, current_a))
        return self.status(bus_v, current_a, power_w, percent)

    # ------------------------------------------------------------ 剩余时间估算
    def eta_minutes(self):
        """用最近 5 分钟电压斜率外推剩余时间。

        用最小二乘回归而非首尾端点差分: 端点法会被启动瞬态/单个毛刺带偏。
        注意这是"按当前负载外推"的乐观估计 —— 锂电接近截止时电压会出现拐点,
        实际可支撑时间会明显短于该值, 因此只作参考, 不作判据。
        """
        if not self.on_battery or len(self.history) < 15:
            return None
        now = time.time()
        recent = [h for h in self.history if now - h[0] <= 300]
        if len(recent) < 15:
            recent = list(self.history)[-15:]
        ts = [h[0] for h in recent]
        vs = [h[1] for h in recent]
        n = len(recent)
        # 相对首点做平移, 提高数值稳定性
        t0, v0 = ts[0], vs[0]
        xs = [(t - t0) / 60.0 for t in ts]
        ys = [v - v0 for v in vs]
        mx = sum(xs) / n
        my = sum(ys) / n
        denom = sum((x - mx) ** 2 for x in xs)
        if denom <= 0:
            return None
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom   # V per minute
        if slope >= -0.0005:          # 电压未在下降(或变化小于噪声) -> 不给外推
            return None
        target = self.c["shutdown_v"]
        cur = vs[-1]
        if cur <= target:
            return 0
        return (cur - target) / (-slope)

    # ------------------------------------------------------------ 动作
    def run_hooks(self, directory, reason):
        if not os.path.isdir(directory):
            return
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".sh") or not os.access(os.path.join(directory, name), os.X_OK):
                continue
            path = os.path.join(directory, name)
            log("INFO", "执行 hook: %s (%s)" % (name, reason))
            try:
                subprocess.run([path], timeout=self.c["hook_timeout"], check=False,
                               env=dict(os.environ, UPS_REASON=reason))
            except subprocess.TimeoutExpired:
                log("WARN", "hook 超时: %s" % name)
            except Exception as exc:
                log("WARN", "hook 异常 %s: %s" % (name, exc))

    def on_low_battery(self):
        self.low_actions_done = True
        log("WARN", "进入低电处理: 电压 %.2fV, 剩余约 %s 分钟" % (
            self.ema_v, ("%.0f" % self.eta_minutes()) if self.eta_minutes() else "未知"))
        mode = self.c["low_power_mode"]
        if mode:
            try:
                subprocess.run(["nvpmodel", "-m", str(mode)], timeout=15, check=False)
                log("INFO", "nvpmodel 已切至档位 %d (降功耗争取时间)" % mode)
            except Exception as exc:
                log("WARN", "nvpmodel 切换失败: %s" % exc)
        self.run_hooks(self.c["low_dir"], "low-battery")
        self.notify("low-battery")

    def do_shutdown(self, reason):
        if self.shutdown_fired:
            return
        self.shutdown_fired = True
        self.state = "SHUTTING_DOWN"
        self.notify("shutdown:" + reason)
        if self.c["dry_run"]:
            log("INFO", "[DRY-RUN] 本应执行关机, 原因=%s; 已跳过(hook 仍会执行)" % reason)
        self.run_hooks(self.c["hook_dir"], reason)
        if self.c["dry_run"]:
            log("INFO", "[DRY-RUN] 关机动作结束, 进程继续运行以便观察")
            self.state = "DRY_RUN_SHUTDOWN"
            return
        log("ERROR", "执行 systemctl poweroff (原因=%s)" % reason)
        self.write_state(self.status())
        try:
            subprocess.run(["systemctl", "poweroff"], timeout=30, check=False)
        except Exception as exc:
            log("ERROR", "关机命令失败: %s" % exc)

    def notify(self, event):
        cmd = self.c["notify_cmd"]
        if not cmd:
            return
        payload = json.dumps(dict(event=event, ts=time.time(), state=self.state,
                                  voltage=self.ema_v, current=self.ema_i))
        try:
            subprocess.run(cmd, shell=True, input=payload, text=True, timeout=10, check=False)
        except Exception as exc:
            log("WARN", "通知失败: %s" % exc)

    # ------------------------------------------------------------ 状态输出
    def status(self, bus_v=None, current_a=None, power_w=None, percent=None):
        if bus_v is None:
            bus_v = self.last.get("bus_v")
        st = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "state": self.state,
            "on_battery": self.on_battery,
            "bus_v": None if bus_v is None else round(bus_v, 3),
            "ema_v": None if self.ema_v is None else round(self.ema_v, 3),
            "current_a": None if current_a is None else round(current_a, 3),
            "ema_i": None if self.ema_i is None else round(self.ema_i, 3),
            "power_w": None if power_w is None else round(power_w, 3),
            "percent": None if percent is None else round(percent, 1),
            "percent_note": "voltage-interpolated, optimistic in plateau",
            "eta_min": self.eta_minutes(),
            "thresholds": {"warn_v": self.c["warn_v"], "low_v": self.c["low_v"],
                           "shutdown_v": self.c["shutdown_v"], "crit_v": self.c["crit_v"]},
            "dry_run": bool(self.c["dry_run"]),
            "sensor_fail": self.sensor_fail,
        }
        self.last = st
        return st

    def write_state(self, st):
        path = self.c["state_file"]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(st, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as exc:
            log("WARN", "写状态文件失败: %s" % exc)

    # ------------------------------------------------------------ 主循环
    def run(self):
        if self.c["http_enabled"]:
            start_http(self.c, self)
        log("INFO", "upsmon 启动: 采样 %ss, 死区 %.0fmA, 阈值 warn=%.1f low=%.1f shutdown=%.1f crit=%.1fV, dry_run=%d" % (
            self.c["interval"], self.c["deadband_ma"], self.c["warn_v"], self.c["low_v"],
            self.c["shutdown_v"], self.c["crit_v"], self.c["dry_run"]))
        while self.running:
            started = time.time()
            st = self.step()
            self.write_state(st)
            self.log_line(st)
            time.sleep(max(0.1, self.c["interval"] - (time.time() - started)))

    def log_line(self, st):
        log("STATE", "%s%s V=%.3f I=%+.3fA P=%.2fW %%%.1f%s" % (
            st["state"],
            " [BAT]" if st["on_battery"] else " [AC] ",
            st["ema_v"] or 0.0, st["ema_i"] or 0.0, st["power_w"] or 0.0, st["percent"] or 0.0,
            (" ETA=%dmin" % st["eta_min"]) if st["eta_min"] else ""))


# ---------------------------------------------------------------- HTTP 预览
HTTP_PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>nx3 UPS</title>
<style>
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:24px;background:#fafafa;color:#222}
h1{font-size:18px;font-weight:600;margin:0 0 12px}
table{border-collapse:collapse;font-size:14px}td{padding:6px 14px 6px 0}
.v{font-variant-numeric:tabular-nums;font-weight:600}
.s-AC,.s-BATTERY{color:#0a7a3f}.s-WARN,.s-LOW{color:#b06a00}
.s-SHUTDOWN_PENDING,.s-CRIT,.s-SHUTTING_DOWN,.s-SENSOR_LOST{color:#c0271b}
.bar{height:10px;background:#e6e6e6;border-radius:5px;width:280px;margin-top:4px}
.bar>i{display:block;height:10px;border-radius:5px;background:#0a7a3f}
small{color:#777}
</style>
<h1 id="t">nx3 UPS Power Module (C)</h1>
<table id="tb"></table>
<div class="bar"><i id="b" style="width:0"></i></div>
<p><small id="f"></small></p>
<script>
async function tick(){
 try{
  const r=await fetch('/status.json',{cache:'no-store'});const d=await r.json();
  document.getElementById('t').className='s-'+d.state;
  document.getElementById('t').textContent='nx3 UPS — '+d.state+(d.on_battery?' (电池供电)':' (AC 供电)');
  const eta=d.eta_min==null?'—':Math.round(d.eta_min)+' min';
  document.getElementById('tb').innerHTML=
   '<tr><td>电池电压</td><td class="v">'+(d.ema_v??'—')+' V</td></tr>'+
   '<tr><td>电流</td><td class="v">'+(d.ema_i>0?'+':'')+(d.ema_i??'—')+' A '+(d.ema_i>0?'(充电)':'(放电)')+'</td></tr>'+
   '<tr><td>功率</td><td class="v">'+(d.power_w??'—')+' W</td></tr>'+
   '<tr><td>电量(电压估算)</td><td class="v">'+(d.percent??'—')+' %</td></tr>'+
   '<tr><td>剩余时间(斜率外推)</td><td class="v">'+eta+'</td></tr>'+
   '<tr><td>关机阈值</td><td class="v">'+d.thresholds.shutdown_v+' V</td></tr>'+
   (d.dry_run?'<tr><td>模式</td><td class="v">DRY-RUN 仅记录</td></tr>':'');
  const p=Math.max(0,Math.min(100,d.percent??0));
  const b=document.getElementById('b');b.style.width=p+'%';
  b.style.background=d.on_battery?(p<40?'#c0271b':'#b06a00'):'#0a7a3f';
  document.getElementById('f').textContent='更新于 '+d.iso+' · 电量按电压线性估算,平台段偏高';
 }catch(e){document.getElementById('f').textContent='读取失败: '+e;}
}
tick();setInterval(tick,2000);
</script></html>"""


def start_http(conf, mon):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/status.json"):
                body = json.dumps(mon.last or {}, ensure_ascii=False).encode()
                ctype = "application/json; charset=utf-8"
            else:
                body = HTTP_PAGE.encode()
                ctype = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer((conf["http_bind"], conf["http_port"]), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    log("INFO", "HTTP 预览已启动: http://%s:%d/" % (conf["http_bind"], conf["http_port"]))


# ---------------------------------------------------------------- 入口
def main():
    ap = argparse.ArgumentParser(description="Waveshare UPS Power Module (C) monitor")
    ap.add_argument("-c", "--conf", default="/etc/ups-monitor.conf")
    ap.add_argument("--dry-run", action="store_true", help="只记录, 不真的关机")
    ap.add_argument("--once", action="store_true", help="读一次就退出")
    ap.add_argument("--http", action="store_true", help="启用 HTTP 预览页")
    ap.add_argument("-i", "--interval", type=float)
    ap.add_argument("--simulate-voltage", type=float, help="用模拟电压跑状态机(联调用, 不写硬件)")
    args = ap.parse_args()

    conf = load_conf(args.conf)
    if args.dry_run:
        conf["dry_run"] = 1
    if args.http:
        conf["http_enabled"] = 1
    if args.interval:
        conf["interval"] = args.interval
    if args.simulate_voltage is not None:
        conf["simulate_voltage"] = args.simulate_voltage
        # 安全护栏: 模拟模式一律强制 dry-run, 绝不允许联调触发真关机
        conf["dry_run"] = 1
        log("WARN", "模拟模式已强制 dry_run=1 (联调不会真的关机)")

    mon = UPSMonitor(conf)
    if conf.get("simulate_voltage") is not None:
        sim_v = conf["simulate_voltage"]
        log("INFO", "模拟模式: 电压固定 %.2fV, 不访问 I2C" % sim_v)

        class _SimSensor:
            def __init__(self, v):
                self.v = v

            def sample(self):
                return self.v, -0.6, self.v * 0.6

            def read16(self, reg):
                return 0

            def close(self):
                pass

        mon.ina = _SimSensor(sim_v)
    else:
        mon.connect()

    def stop(signum, frame):
        log("INFO", "收到信号 %d, 退出" % signum)
        mon.running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    if args.once:
        st = mon.step()
        mon.write_state(st)
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    mon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
