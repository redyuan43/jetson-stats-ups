#!/bin/bash
# 低电关机前钩子: 释放功耗 + 落盘, 为有序关机争取时间。
# 由 upsmon 以 UPS_REASON 环境变量调用。单个钩子超时 20s。
# 测试: DRY=1 时只打印将执行的动作, 不真停服务、不 pkill、不 sync。
set -u

DRY="${DRY:-0}"
log() { echo "[ups-pre-shutdown] $*"; }

do_stop() {
    local u="$1"
    if [ "$DRY" = "1" ]; then log "[DRY] 将停止 $u"; return 0; fi
    log "停止 $u"
    systemctl stop "$u" || log "停止 $u 失败(继续)"
}

log "reason=${UPS_REASON:-unknown} 开始疏散 (dry=${DRY})"

# 1) 动态发现: 凡是承载 llama-server 进程的 systemd 单元, 无论叫什么名字都停掉。
#    静态名单会随部署演进腐烂(旧名单 llama-server.service 早已不存在),
#    因此从 /proc/<pid>/cgroup 反查真实 unit 名。
units=""
for pid in $(pgrep -x llama-server 2>/dev/null); do
    unit=$(cut -d: -f3 "/proc/$pid/cgroup" 2>/dev/null | tail -1 | awk -F/ '{print $NF}')
    case "$unit" in *.service) units="$units $unit" ;; esac
done

# 2) 其他已知耗电大户(存在且 active 才停)
if systemctl is-active --quiet docker.service 2>/dev/null; then
    units="$units docker.service"
fi

for u in $(printf '%s\n' $units | sort -u); do
    if systemctl is-active --quiet "$u" 2>/dev/null; then
        do_stop "$u"
    fi
done

# 3) 兜底: 停完 unit 后仍残留 llama-server 进程(非 systemd 托管的孤儿)则 SIGTERM
if pgrep -x llama-server >/dev/null 2>&1; then
    [ "$DRY" = "1" ] || pkill -x llama-server 2>/dev/null
    log "残留 llama-server 进程, 等待 5s"
    [ "$DRY" = "1" ] || sleep 5
    if pgrep -x llama-server >/dev/null 2>&1; then
        log "警告: llama-server 仍未退出"
    fi
fi

# 4) 落盘, 减小突然掉电造成文件系统损伤的概率
if [ "$DRY" = "1" ]; then
    log "[DRY] 跳过 sync"
else
    sync
    log "sync 完成"
fi
exit 0
