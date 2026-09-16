#!/bin/bash
# install.sh — 在 Jetson Orin NX (nx3) 上部署 UPS 监控/低电自动关机
#
#   sudo ./install.sh            # 安装并以 DRY-RUN 模式启动(dry_run=1, 不会真的关机)
#   sudo ./install.sh --enable   # 安装并直接启用真关机(dry_run=0)
#   sudo ./install.sh --uninstall
#
# 幂等: 可重复执行。
set -euo pipefail

PREFIX_BIN=/usr/local/bin
PREFIX_LIB=/usr/local/lib/ups-monitor
CONF=/etc/ups-monitor.conf
UNIT=/etc/systemd/system/ups-monitor.service
SRC="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 sudo 运行" >&2
    exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
    echo "== 卸载 ups-monitor =="
    systemctl disable --now ups-monitor.service 2>/dev/null || true
    rm -f "$UNIT" "$PREFIX_BIN/upsmon.py" "$PREFIX_BIN/upsctl"
    systemctl daemon-reload
    echo "已卸载(配置 $CONF 与钩子目录 $PREFIX_LIB 保留, 如需彻底清理请手动删除)"
    exit 0
fi

echo "== 依赖检查 =="
python3 - <<'PY'
import sys
try:
    import smbus2  # noqa
    print("  smbus2: OK")
except ImportError:
    print("  smbus2: 缺失 -> sudo apt install -y python3-smbus2")
    sys.exit(1)
PY
command -v i2cdetect >/dev/null || echo "  提示: i2c-tools 未装, 建议 apt install -y i2c-tools"

echo "== 探测 UPS 模块 (i2c-7 @0x41) =="
# 直接用 smbus2 读总线电压寄存器, 比解析 i2cdetect 文本可靠
if ! python3 - <<'PY'
import sys
from smbus2 import SMBus
BUS, ADDR = 7, 0x41
try:
    b = SMBus(BUS)
    d = b.read_i2c_block_data(ADDR, 0x02, 2)
    raw = (d[0] << 8) | d[1]
    print("  INA219 在线: i2c-%d @0x%02X, 电池电压 %.3fV" % (BUS, ADDR, (raw >> 3) * 0.004))
    b.close()
except Exception as e:
    print("  x 未探测到 0x%02X: %s" % (ADDR, e), file=sys.stderr)
    sys.exit(1)
PY
then
    echo "  ⚠️ UPS 模块探测失败 —— 检查电池/开关/顶针接触; 继续安装, 服务将进入 SENSOR_LOST" >&2
fi

echo "== 安装文件 =="
install -m 0755 "$SRC/upsmon.py" "$PREFIX_BIN/upsmon.py"
install -m 0755 "$SRC/upsctl"   "$PREFIX_BIN/upsctl"
install -d "$PREFIX_LIB/pre-shutdown.d" "$PREFIX_LIB/low-battery.d"
install -m 0755 "$SRC/hooks/pre-shutdown.d/"*.sh "$PREFIX_LIB/pre-shutdown.d/" 2>/dev/null || true
install -m 0755 "$SRC/hooks/low-battery.d/"*.sh "$PREFIX_LIB/low-battery.d/" 2>/dev/null || true

if [ -f "$CONF" ]; then
    echo "  保留已有配置 $CONF (未覆盖)"
else
    install -m 0644 "$SRC/ups-monitor.conf" "$CONF"
    echo "  写入配置 $CONF"
fi

if [ "${1:-}" = "--enable" ]; then
    sed -i 's/^dry_run *=.*/dry_run = 0/' "$CONF"
    echo "  dry_run 已置 0 —— 达到阈值将真正执行 poweroff"
fi

install -m 0644 "$SRC/ups-monitor.service" "$UNIT"

echo "== 启用服务 =="
systemctl daemon-reload
systemctl enable ups-monitor.service
# 必须用 restart: enable --now 对已 active 的服务不会重新加载新代码
systemctl restart ups-monitor.service
sleep 3
systemctl --no-pager --lines=0 status ups-monitor.service || true

echo
echo "== 当前状态 =="
/usr/local/bin/upsctl || true
echo
echo "完成。常用命令:"
echo "  upsctl              查看电池状态"
echo "  upsctl watch        实时刷新"
echo "  journalctl -u ups-monitor -f   跟踪守护进程日志"
echo "  sudo systemctl restart ups-monitor"
echo "  sudo ./install.sh --uninstall  卸载"
