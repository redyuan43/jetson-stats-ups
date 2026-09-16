#!/usr/bin/env python3
# checkboards-notify.py — ups-monitor -> Check Boards 白板提醒
#
# 两种调用方式:
#   1. daemon notify_cmd: JSON 从 stdin 进来 {"event":"on-battery",...}
#   2. 手动/systemd:      checkboards-notify.py boot   (开机自报)
#                         checkboards-notify.py test   (链路自检)
#
# 设计: 只发"提醒"卡片 —— announcement 预置 status=ready + 现成播报文案,
# 服务端因此跳过 LLM 摘要、直接 TTS 播报(boardStore.normalizeAnnouncement)。
# 注意: 必须是独立 .py, 不能用 `python3 - <<EOF` 包装 —— heredoc 会占用
# stdin, daemon 管道传来的事件 JSON 永远读不到(实测坑: event 恒为 unknown)。
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request

CONF_PATH = os.environ.get("UPSCHECK_CONF", "/etc/ups-monitor-checkboards.conf")
STATUS_PATH = os.environ.get("UPS_STATUS_FILE", "/run/ups-monitor/status.json")

conf = {}
try:
    with open(CONF_PATH) as fh:
        for raw in fh:
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
except FileNotFoundError:
    sys.exit(0)  # 未配置 = 静默跳过, 不影响关机主流程

url = conf.get("CHECKBOARDS_URL", "http://agx.taild500c8.ts.net:8788/api/cards")
device = conf.get("CHECKBOARDS_DEVICE", "nx3")
secret = conf.get("CHECKBOARDS_SECRET", "")
mode = sys.argv[1] if len(sys.argv) > 1 else "stdin"

if mode == "stdin":
    try:
        evt = json.load(sys.stdin)
    except Exception:
        evt = {"event": "unknown"}
else:
    evt = {"event": mode}  # boot / test

event = str(evt.get("event", "unknown"))

# 状态文件提供更完整的现场数据(可能不存在/过期, 全部防御性读取)
st = {}
try:
    with open(STATUS_PATH) as fh:
        st = json.load(fh)
except Exception:
    pass


def fmt_min(m):
    if m is None:
        return "未知"
    m = int(m)
    return ("%dh%02dm" % (m // 60, m % 60)) if m >= 60 else ("%dm" % m)


v = evt.get("voltage") or st.get("ema_v")
i = evt.get("current") or st.get("ema_i")
pct_v = st.get("percent")
pct_c = evt.get("percent_coulomb") or st.get("percent_coulomb")
eta = evt.get("eta_min_coulomb") or st.get("eta_min_coulomb") or evt.get("eta_min") or st.get("eta_min")
state = evt.get("state") or st.get("state") or "?"

pct_str = ("%.0f%%(库仑)" % pct_c) if pct_c is not None else (("%.0f%%(电压)" % pct_v) if pct_v is not None else "未知")
v_str = ("%.2fV" % v) if v is not None else "?.?V"
i_str = ("%+.2fA" % i) if i is not None else "?A"
eta_str = fmt_min(eta)

# 事件 -> (标题, TTS 播报文案)。播报只讲人话, 不带 markdown/表情。
MAP = {
    "on-battery": ("⚡ %s 切换至电池供电" % device,
                   "提醒，%s 已切换至电池供电。当前电压%s，电量%s，按当前负载预计还能支撑%s。若市电未恢复，%s 将自动关机。"
                   % (device, v_str, pct_str, eta_str, device)),
    "ac-restored": ("🔌 %s 已恢复市电" % device,
                    "%s 已恢复市电供电，电池开始充电，当前电量%s。" % (device, pct_str)),
    "low-battery": ("🪫 %s 低电告警" % device,
                    "警告，%s 电池电压过低，已进入低电处理。当前电压%s，电量%s，预计%s后触及关机线，请尽快恢复供电。"
                    % (device, v_str, pct_str, eta_str)),
    "boot": ("✅ %s 已开机" % device,
             "%s 已上电开机，UPS 监控就绪。当前供电状态 %s，电量%s。" % (device, state, pct_str)),
    "test": ("🔧 %s 通知链路自检" % device,
             "提醒，%s 到检查白板的通知链路自检成功。" % device),
}
if event.startswith("shutdown"):
    reason = event.split(":", 1)[1] if ":" in event else "?"
    title = "🛑 %s 即将自动关机" % device
    tts = ("紧急提醒，%s 电池电压低于关机阈值（原因 %s），系统将在十几秒内执行有序关机。"
           "当前电压%s，电量%s。" % (device, reason, v_str, pct_str))
elif event in MAP:
    title, tts = MAP[event]
else:
    title = "📟 %s UPS 事件: %s" % (device, event)
    tts = "提醒，%s 发生 UPS 事件 %s，电压%s。" % (device, event, v_str)

body = "\n".join([
    "**事件**: `%s`  ·  **状态**: `%s`" % (event, state),
    "**电压**: %s  ·  **电流**: %s  ·  **电量**: %s  ·  **预计剩余**: %s" % (v_str, i_str, pct_str, eta_str),
    "**dry_run**: %s  ·  **时间**: %s" % (evt.get("dry_run", st.get("dry_run", "?")), time.strftime("%Y-%m-%d %H:%M:%S%z")),
    "",
    "来源: nx3 ups-monitor hook (checkboards-notify)",
])

iso = time.strftime("%Y-%m-%dT%H:%M:%S%z")
card = {
    "device": device,
    "title": title,
    "body": body,
    "source": "ups_monitor",
    "tags": ["UPS"],
    "received_at": iso,
    "announcement": {"status": "ready", "summary": tts, "generatedAt": iso},
}
payload = json.dumps(card, ensure_ascii=False).encode("utf-8")


def post():
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if secret:
        ts = str(int(time.time()))
        sig = hmac.new(secret.encode(), ts.encode() + b"." + payload, hashlib.sha256).hexdigest()
        req.add_header("x-check-boards-timestamp", ts)
        req.add_header("x-check-boards-signature", "sha256=" + sig)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status


attempts = 3 if mode == "boot" else 1
for n in range(attempts):
    try:
        code = post()
        print("[upsnotify] posted event=%s http=%s" % (event, code), flush=True)
        sys.exit(0)
    except Exception as exc:
        if n + 1 < attempts:
            time.sleep(5)
        else:
            # 通知失败绝不能阻塞关机流程
            print("[upsnotify] WARN post failed event=%s: %s" % (event, exc), flush=True)
            sys.exit(0)
