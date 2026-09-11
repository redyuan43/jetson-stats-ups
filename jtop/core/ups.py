# -*- coding: UTF-8 -*-
# This file is part of the jetson_stats package (https://github.com/rbonghi/jetson_stats or http://rnext.it).
# Copyright (c) 2019-2026 Raffaello Bonghi.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""
UPS battery monitor reader for Waveshare UPS Power Module (C) / INA219.

Added by a downstream fork (not part of upstream jetson-stats).

Data sources, in order of preference:

1. ``/run/ups-monitor/status.json`` — written atomically (tmp + rename) every
   ~2 s by the ``ups-monitor`` daemon shipped with the ``jetson-ups-autoshutdown``
   deployment.  This is the *preferred* source because the daemon owns the state
   machine that actually powers the board off: keeping it as the single source
   of truth means jtop can never disagree with the process that shuts the
   machine down.  It also carries values that cannot be reconstructed locally,
   such as the least-squares ETA and the ``dry_run`` flag.

2. A direct INA219 read over I2C, used only when the daemon is absent.  In that
   mode the state (AC / BATTERY / WARN / LOW / SHUTDOWN_PENDING / CRIT) is
   *inferred locally* and ``source`` is reported as ``'direct'`` so the UI can
   say so out loud.  Never present an inferred state as if the daemon produced
   it — a wrong UPS indicator is worse than no indicator.
"""

import json
import logging
import os
import time
from collections import deque

logger = logging.getLogger(__name__)

try:
    from smbus2 import SMBus
except ImportError:  # keep this module importable without smbus2 installed
    SMBus = None

# --------------------------------------------------------------------- 默认值
STATUS_PATH = '/run/ups-monitor/status.json'
CONF_PATH = '/etc/ups-monitor.conf'

DEFAULT_BUS = 7
DEFAULT_ADDR = 0x41
DEFAULT_DEADBAND_MA = 120.0
DEFAULT_EMA_ALPHA = 0.25

# INA219 registers / settings (verified against the ups-monitor deployment)
REG_CONFIG = 0x00
REG_BUS = 0x02
REG_POWER = 0x03
REG_CURRENT = 0x04
REG_CAL = 0x05

BUS_VOLTAGE_RANGE_16V = 0x00
GAIN_DIV2_80MV = 0x01
ADCRES_12BIT_32S = 0x0D
MODE_SANDBVOLT_CONTINUOUS = 0x07

CAL_VALUE = 26868          # trunc(0.04096 / (CurrentLSB * RShunt)), RShunt = 0.01 ohm
CURRENT_LSB_MA = 0.1524    # mA / bit
POWER_LSB_W = 0.003048     # W / bit

PERCENT_V_EMPTY = 9.0      # module definition: 3S at 9.0 V == 0 %
PERCENT_V_FULL = 12.6      # module definition: 3S at 12.6 V == 100 %
PERCENT_SPAN_V = PERCENT_V_FULL - PERCENT_V_EMPTY

DEFAULT_THRESHOLDS = {
    'crit_v': 9.3,
    'shutdown_v': 9.9,
    'low_v': 10.2,
    'warn_v': 10.8,
}

# A status file older than this means the daemon is gone, not just idle.
STALE_SECONDS = 15.0

# Least-squares slope needs this many battery-only samples before it will
# answer at all.  This is the cheap guard; the real gate is MIN_SLOPE_SPAN.
MIN_SLOPE_SAMPLES = 15

# The INA219 bus-voltage LSB is ~4 mV while the 3S pack discharges at roughly
# 12 mV/min on the plateau, so a short window measures ADC noise, not drift.
# Require this much wall-clock span inside the fit window, and average samples
# into SLOPE_BUCKET_S buckets first -- the bucket mean cuts random noise by
# ~sqrt(n) without touching the real ramp.
MIN_SLOPE_SPAN = 120.0
SLOPE_BUCKET_S = 20.0

# Human readable state -> severity.  Kept free of curses on purpose: this is a
# core module and must stay importable outside a terminal.
STATE_LABELS = {
    'INIT': ('初始化', 'grey'),
    'AC': ('AC 供电', 'green'),
    'AC_BATT_LOW': ('AC 供电(电池低压)', 'yellow'),
    'BATTERY': ('电池供电', 'yellow'),
    'WARN': ('低电预警 WARN', 'yellow'),
    'LOW': ('低电/已降功耗 LOW', 'red'),
    'SHUTDOWN_PENDING': ('即将有序关机', 'red'),
    'CRIT': ('电量危急 CRIT', 'red'),
    'SENSOR_LOST': ('传感器丢失', 'red'),
    'UNKNOWN': ('未知', 'grey'),
}

STATE_ORDER = ['INIT', 'AC', 'AC_BATT_LOW', 'BATTERY', 'WARN', 'LOW', 'SHUTDOWN_PENDING', 'CRIT', 'SENSOR_LOST']


def state_label(state):
    """Return (text, severity) for a state name; never raises."""
    return STATE_LABELS.get(state, STATE_LABELS['UNKNOWN'])


def _read_conf(path):
    """Parse the ups-monitor key = value config. Returns {} when unreadable."""
    conf = {}
    if not path or not os.path.isfile(path):
        return conf
    try:
        with open(path) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, val = line.split('=', 1)
                conf[key.strip()] = val.split('#')[0].strip()
    except OSError as exc:
        logger.debug("UPS: cannot read %s (%s)", path, exc)
    return conf


class UPS(object):
    """Read-only view over the UPS battery module.

    Every public attribute stays ``None`` until data is available, and
    :meth:`refresh` swallows all errors — a monitoring page must never take the
    whole terminal down because a sensor disappeared.
    """

    def __init__(self, status_path=STATUS_PATH, conf_path=CONF_PATH, history=600):
        self._status_path = status_path
        self._conf_path = conf_path

        conf = _read_conf(conf_path)
        self.bus = _as_int(conf.get('bus'), DEFAULT_BUS)
        addr = _as_int(conf.get('addr'), DEFAULT_ADDR)
        self.addr = addr if addr else DEFAULT_ADDR
        self.ema_alpha = _as_float(conf.get('ema_alpha'), DEFAULT_EMA_ALPHA)
        deadband_ma = _as_float(conf.get('deadband_ma'), DEFAULT_DEADBAND_MA)
        self.deadband_a = deadband_ma / 1000.0

        # traffic-light thresholds: prefer the daemon config, fall back to the
        # documented 3S Li-ion defaults
        self.thresholds = dict(DEFAULT_THRESHOLDS)
        for key in self.thresholds:
            self.thresholds[key] = _as_float(conf.get(key), self.thresholds[key])

        self.available = False
        self.source = None          # 'daemon' | 'direct' | None
        self.error = ''
        self.state = 'INIT'
        self.on_battery = None
        self.bus_v = None
        self.ema_v = None
        self.current_a = None
        self.ema_i = None
        self.power_w = None
        self.percent = None
        self.eta_min = None
        self.dry_run = None
        self.sensor_fail = 0
        self.iso = ''
        self.ts = None
        self.age = None             # seconds since the sample was taken
        self._ina = None
        self._history = deque(maxlen=history)
        self._prev_on_battery = None
        self._events = deque(maxlen=8)
        self._last_state = None

    # ------------------------------------------------------------------ 数据源
    def refresh(self):
        """Pull one sample. Returns True when data is usable."""
        ok = self._refresh_from_daemon()
        if not ok:
            ok = self._refresh_from_i2c()
        if ok and self.available and self.ema_v is not None:
            self._record_sample()
            if self.source == 'direct':
                # No daemon here to publish an ETA, so derive it from the slope
                # we just recorded.
                self.eta_min = self._local_eta()
        # Record transitions for the "recent events" row. The daemon logs these
        # to journald, but journald needs root to read, so we observe them here.
        if self.state != self._last_state:
            if self._last_state is not None:
                self._events.append((time.time(), self._last_state, self.state))
            self._last_state = self.state
        return self.available

    def _record_sample(self):
        """Keep a battery-only voltage history, feeding the local slope / ETA.

        Battery-only on purpose: mixing the flat AC plateau with the step from
        pulling the adapter inflates the regression slope by an order of
        magnitude (observed 20 min ETA against a real 8 h). The buffer is also
        dropped on the AC -> battery edge so the previous segment cannot leak
        back in. Done here rather than in either source reader so the slope
        works identically whether the data came from the daemon or from I2C.
        """
        if not self.on_battery:
            self._prev_on_battery = False
            return
        if self._prev_on_battery is False:
            self._history.clear()
        stamp = self.ts if isinstance(self.ts, (int, float)) else time.time()
        self._history.append((stamp, self.ema_v))
        self._prev_on_battery = True

    def _refresh_from_daemon(self):
        if not self._status_path or not os.path.isfile(self._status_path):
            return False
        try:
            with open(self._status_path) as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            logger.debug("UPS: cannot parse %s (%s)", self._status_path, exc)
            return False
        if not isinstance(data, dict) or 'state' not in data:
            return False
        ts = data.get('ts')
        age = (time.time() - ts) if isinstance(ts, (int, float)) else None
        if age is not None and age > STALE_SECONDS:
            # File exists but nobody is updating it -> the daemon is dead.
            self.available = False
            self.source = 'daemon'
            self.error = 'ups-monitor 状态文件已停止更新 (%.0fs)' % age
            self.state = 'UNKNOWN'
            return True
        self.source = 'daemon'
        self.error = ''
        self.available = True
        self.ts = ts
        self.age = age
        self.iso = data.get('iso', '')
        self.state = data.get('state', 'UNKNOWN')
        self.on_battery = data.get('on_battery')
        self.bus_v = _as_float(data.get('bus_v'), None)
        self.ema_v = _as_float(data.get('ema_v'), self.bus_v)
        self.current_a = _as_float(data.get('current_a'), None)
        self.ema_i = _as_float(data.get('ema_i'), self.current_a)
        self.power_w = _as_float(data.get('power_w'), None)
        self.percent = _as_float(data.get('percent'), None)
        self.eta_min = _as_float(data.get('eta_min'), None)
        self.sensor_fail = _as_int(data.get('sensor_fail'), 0)
        dry = data.get('dry_run')
        self.dry_run = bool(dry) if dry is not None else None
        th = data.get('thresholds')
        if isinstance(th, dict):
            for key in self.thresholds:
                self.thresholds[key] = _as_float(th.get(key), self.thresholds[key])
        return True

    def _refresh_from_i2c(self):
        """Fallback: talk to the INA219 ourselves (needs i2c access)."""
        if SMBus is None:
            self.available = False
            self.source = None
            self.error = '缺少 smbus2，且未找到 ups-monitor 状态文件'
            self.state = 'UNKNOWN'
            return False
        try:
            if self._ina is None:
                self._ina = SMBus(self.bus)
                self._ina.write_i2c_block_data(self.addr, REG_CAL,
                                               [(CAL_VALUE >> 8) & 0xFF, CAL_VALUE & 0xFF])
                cfg = ((BUS_VOLTAGE_RANGE_16V << 13) | (GAIN_DIV2_80MV << 11) |
                       (ADCRES_12BIT_32S << 7) | (ADCRES_12BIT_32S << 3) |
                       MODE_SANDBVOLT_CONTINUOUS)
                self._ina.write_i2c_block_data(self.addr, REG_CONFIG,
                                               [(cfg >> 8) & 0xFF, cfg & 0xFF])
                # The conversion that is already in flight when CAL/CONFIG are
                # rewritten is not representative of the new configuration, and
                # the CPU burst from process start-up is riding on top of it.
                # Measured 40 % high (-0.83 A against a steady -0.59 A), so burn
                # one conversion before trusting a sample.
                time.sleep(0.08)
                self._read_raw(REG_BUS)
            bus_v = (self._read_raw(REG_BUS) >> 3) * 0.004
            current_a = self._read_s16(REG_CURRENT) * CURRENT_LSB_MA / 1000.0
            power_w = self._read_s16(REG_POWER) * POWER_LSB_W
        except Exception as exc:  # OSError, PermissionError, smbus2 errors...
            logger.debug("UPS: direct INA219 read failed (%s)", exc)
            self.available = False
            self.source = None
            self.error = 'INA219 读取失败: %s' % exc
            self.state = 'UNKNOWN'
            try:
                if self._ina is not None:
                    self._ina.close()
            except Exception:
                pass
            self._ina = None
            return False

        self.source = 'direct'
        self.error = ''
        self.available = True
        self.ts = time.time()
        self.age = 0.0
        self.iso = time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(self.ts))
        self.bus_v = bus_v
        self.current_a = current_a
        self.power_w = power_w
        # Same EMA as the daemon so a machine without ups-monitor still shows
        # something stable rather than raw I2C noise.
        self.ema_v = bus_v if self.ema_v is None else self.ema_v + self.ema_alpha * (bus_v - self.ema_v)
        self.ema_i = current_a if self.ema_i is None else self.ema_i + self.ema_alpha * (current_a - self.ema_i)
        self.percent = max(0.0, min(100.0, (self.ema_v - PERCENT_V_EMPTY) / PERCENT_SPAN_V * 100.0))
        self.on_battery = self.ema_i < -self.deadband_a
        self.state = self._infer_state()
        return True

    def _infer_state(self):
        """Local state guess used only when the daemon is absent."""
        if self.ema_v is None:
            return 'UNKNOWN'
        if not self.on_battery:
            return 'AC_BATT_LOW' if self.ema_v <= self.thresholds['warn_v'] else 'AC'
        for name, key in (('CRIT', 'crit_v'), ('SHUTDOWN_PENDING', 'shutdown_v'),
                          ('LOW', 'low_v'), ('WARN', 'warn_v')):
            if self.ema_v <= self.thresholds[key]:
                return name
        return 'BATTERY'

    def _local_eta(self):
        """ETA from the local discharge slope, mirroring the daemon's formula."""
        slope_mv = self.slope_mv_per_min()
        if slope_mv is None or slope_mv >= -0.5:
            return None
        if self.ema_v is None:
            return None
        target = self.thresholds['shutdown_v']
        if self.ema_v <= target:
            return 0.0
        return (self.ema_v - target) / (-slope_mv / 1000.0)

    # ------------------------------------------------------------------ 工具
    def slope_mv_per_min(self, seconds=300.0):
        """Voltage drift (mV/min) over the latest battery samples.

        Returns ``None`` until enough *span* of discharge data exists -- the
        caller is expected to render that as "collecting" rather than inventing
        a number.  Samples are bucket-averaged before the fit because the raw
        1 Hz signal is dominated by the INA219 bus-voltage LSB (~4 mV) while the
        real drift on the 3S plateau is only ~12 mV/min.
        """
        if not self.on_battery or len(self._history) < MIN_SLOPE_SAMPLES:
            return None
        now = time.time()
        recent = [h for h in self._history if now - h[0] <= seconds]
        if len(recent) < MIN_SLOPE_SAMPLES:
            return None
        t0 = recent[0][0]
        if recent[-1][0] - t0 < MIN_SLOPE_SPAN:
            return None

        buckets = {}
        for stamp, volt in recent:
            key = int((stamp - t0) / SLOPE_BUCKET_S)
            buckets.setdefault(key, []).append(volt)
        pts = [(t0 + (key + 0.5) * SLOPE_BUCKET_S, sum(v) / len(v))
               for key, v in sorted(buckets.items())]
        n = len(pts)
        if n < 4:
            return None

        base = pts[0][0]
        xs = [(t - base) / 60.0 for t, _ in pts]
        ys = [v for _, v in pts]
        mx = sum(xs) / n
        my = sum(ys) / n
        denom = sum((x - mx) ** 2 for x in xs)
        if denom <= 0:
            return None
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
        return slope * 1000.0
    def _read_raw(self, reg):
        data = self._ina.read_i2c_block_data(self.addr, reg, 2)
        return (data[0] << 8) | data[1]

    def _read_s16(self, reg):
        value = self._read_raw(reg)
        return value - 65536 if value > 32767 else value

    def close(self):
        if self._ina is not None:
            try:
                self._ina.close()
            except Exception:
                pass
            self._ina = None

    # -------------------------------------------------------------- 便捷属性
    @property
    def slope_sample_count(self):
        """Battery-only samples currently feeding the slope fit (see
        :data:`MIN_SLOPE_SAMPLES`).  Lets the UI show real progress instead of
        a hard-coded wait time."""
        return len(self._history)

    @property
    def slope_span_seconds(self):
        """Wall-clock span (s) covered by the current battery-only history.

        The meaningful readiness signal for :meth:`slope_mv_per_min` is span,
        not sample count -- the fit refuses to answer below
        :data:`MIN_SLOPE_SPAN` seconds of real discharge."""
        if len(self._history) < 2:
            return 0.0
        return self._history[-1][0] - self._history[0][0]

    @property
    def label(self):
        return state_label(self.state)[0]

    @property
    def severity(self):
        return state_label(self.state)[1]

    @property
    def events(self):
        return list(self._events)

    @property
    def mode(self):
        if self.dry_run is None:
            return '未知'
        return 'DRY-RUN (只记录, 不会关机)' if self.dry_run else 'LIVE (低电会真的关机)'

    @property
    def current_meaning(self):
        """'充电' / '放电' / '浮充' — sign convention from the vendor docs."""
        if self.ema_i is None:
            return ''
        if self.ema_i > self.deadband_a:
            return '充电'
        if self.ema_i < -self.deadband_a:
            return '放电'
        return '浮充/空载'

    def is_discharging(self):
        return bool(self.on_battery)

    def is_charging(self):
        return self.on_battery is False and self.ema_i is not None and self.ema_i > self.deadband_a

    def health(self):
        """Short text used next to the state, e.g. '充电中 +1.43A'."""
        if not self.available:
            return self.error or '未检测到 UPS'
        if self.is_charging():
            return '充电中 %+.2fA' % self.ema_i
        if self.on_battery:
            return '放电 %+.2fA' % self.ema_i
        return '浮充 %+.2fA' % (self.ema_i or 0.0)


def _as_float(value, default):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default):
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def ups_available(**kwargs):
    """Cheap detection used to decide whether the UPS page should exist.

    Never raises and never blocks for long: at most one status file read plus,
    if that fails, one I2C probe.
    """
    probe = UPS(**kwargs)
    try:
        if not probe.refresh():
            probe.close()
            return False
        return True
    finally:
        probe.close()


# EOF
