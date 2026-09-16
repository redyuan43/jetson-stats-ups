#!/bin/bash
# 低电预警钩子: 在进入 LOW 档(warn_v 之下、shutdown_v 之上)时执行一次。
# 目的: 在真正关机前给 Ivan 争取"插上 AC"的时间。
# 电池模式下关机后必须现场按电源键才能开机, 所以通知是这里最重要的动作。
set -u

log() { echo "[ups-low-battery] $*"; }

log "进入低电档, 建议立即接入 19V AC"

# 本机终端广播(有人登录时会看到)
if command -v wall >/dev/null 2>&1; then
    echo "【UPS 低电】nx3 正在电池供电, 电压偏低, 请尽快接入 19V 电源, 否则将自动关机。" | wall 2>/dev/null || true
fi

# 如需远程告警, 在此追加 webhook / ntfy / mail, 例如:
# curl -s -m 5 -d 'nx3 UPS 低电' https://ntfy.sh/<topic> >/dev/null || true

exit 0
