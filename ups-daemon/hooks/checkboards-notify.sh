#!/usr/bin/env bash
# checkboards-notify.sh — 兼容入口(boot-notify unit 与旧 notify_cmd 引用)。
# 逻辑在 checkboards-notify.py。为什么不让 .sh 内嵌 python3 - <<EOF:
# heredoc 会占用 stdin, daemon 管道传来的事件 JSON 永远读不到
# (实测坑: 卡片 event 恒为 unknown)。exec 直通 stdin/stdout/stderr。
set -u
exec python3 "$(dirname "$0")/checkboards-notify.py" "$@"
