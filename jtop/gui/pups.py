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
UPS page — waveform / battery module monitor.

Added by a downstream fork (not part of upstream jetson-stats). Everything the
standalone ``upsctl`` CLI shows is reproduced here, plus three history charts
that the CLI cannot offer.

Two layout rules worth keeping if this file is edited later:

* Every column position is computed with :func:`display_width`, not ``len()``.
  The labels are Chinese and ``len()`` counts characters while a terminal
  counts cells — CJK glyphs occupy two, so ``len()`` silently misaligns every
  value that follows a Chinese label.
* Chart scales are deliberately **fixed**, never auto-ranged. An auto-ranged
  voltage chart holds the trace at a constant height while the battery drains,
  which hides exactly the trend the chart exists to show.
"""

import math
import unicodedata

import curses

# Aliased on purpose: this module exports the *page* class as ``UPS`` (to match
# the upstream naming convention for pages), so the core reader must not share
# that name or ``UPS()`` below would recurse into the page itself.
from ..core.ups import UPS as UPSReader
from ..core.ups import MIN_SLOPE_SPAN
from ..core.ups import state_label
# Page class definition
from .jtopgui import Page
# Graphics elements
from .lib.chart import Chart
from .lib.colors import NColors
from .lib.linear_gauge import basic_gauge

# Fixed chart full-scales. See module docstring: these must not be dynamic.
CHART_MAX_POWER = 20.0      # W
CHART_MAX_CURRENT = 1.5     # A
CHART_MAX_MARGIN_MV = 3000.0  # mV above the shutdown threshold
CHART_TIME = 600.0          # seconds of history kept per chart
CHART_TIK = 60              # axis label every 60 s

SEVERITY_COLOR = {
    'green': lambda: NColors.green(),
    'yellow': lambda: NColors.yellow(),
    'red': lambda: NColors.red(),
    'grey': lambda: NColors.italic(),
}


def display_width(text):
    """Terminal cell count of ``text`` (CJK glyphs are two cells wide)."""
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
    return width


def plot_key_value(stdscr, pos_y, pos_x, name, value, color=curses.A_NORMAL, key_width=0):
    """Like ``lib.common.plot_name_info`` but safe with CJK text.

    Returns the number of cells consumed.
    """
    key = name + ':'
    pad = max(1, key_width - display_width(key)) if key_width else 1
    try:
        stdscr.addstr(pos_y, pos_x, key, curses.A_BOLD)
    except curses.error:
        pass
    try:
        stdscr.addstr(pos_y, pos_x + display_width(key) + pad, value, color)
    except curses.error:
        pass
    return display_width(key) + pad + display_width(value)


def fmt_eta(minutes):
    if minutes is None:
        return '—'
    if minutes <= 0:
        return '已到阈值'
    minutes = int(round(minutes))
    if minutes >= 60:
        return '%dh%02dm' % (minutes // 60, minutes % 60)
    return '%dm' % minutes


class UPS(Page):
    """Battery / UPS monitor page."""

    def __init__(self, stdscr, jetson):
        super(UPS, self).__init__("UPS", stdscr, jetson)
        self.ups = UPSReader()
        self.ups.refresh()
        # Attach to the jtop update loop: keeps sampling even while the user is
        # reading another page, so the charts have history when they come back.
        jetson.attach(self._on_update)
        self._charts = [
            Chart(jetson, 'Power', self._chart_power, type_value=float,
                  color_text=curses.COLOR_YELLOW, color_chart=[curses.COLOR_YELLOW],
                  fill=True, time=CHART_TIME, tik=CHART_TIK),
            Chart(jetson, 'Current', self._chart_current, type_value=float,
                  color_text=curses.COLOR_CYAN, color_chart=[curses.COLOR_CYAN],
                  fill=True, time=CHART_TIME, tik=CHART_TIK),
            Chart(jetson, 'V above cut-off', self._chart_margin, type_value=float,
                  color_text=curses.COLOR_GREEN, color_chart=[curses.COLOR_GREEN],
                  fill=True, time=CHART_TIME, tik=CHART_TIK),
        ]

    # ------------------------------------------------------------- 数据刷新
    def _on_update(self, jetson):
        self.ups.refresh()

    # ------------------------------------------------------------- 图表回调
    def _chart_power(self, jetson, name):
        value = self.ups.power_w or 0.0
        return {
            'active': self.ups.available,
            'value': [max(0.0, min(value, CHART_MAX_POWER))],
            'max': CHART_MAX_POWER,
            'unit': 'W',
        }

    def _chart_current(self, jetson, name):
        # Chart can only draw positive magnitudes, so plot |I| and carry the
        # direction in the label instead of silently losing the sign.
        value = abs(self.ups.ema_i or 0.0)
        return {
            'active': self.ups.available,
            'value': [min(value, CHART_MAX_CURRENT)],
            'max': CHART_MAX_CURRENT,
            'unit': 'A',
        }

    def _chart_margin(self, jetson, name):
        if self.ups.ema_v is None:
            return {'active': False, 'value': [0.0], 'max': CHART_MAX_MARGIN_MV, 'unit': 'mV'}
        margin = (self.ups.ema_v - self.ups.thresholds['shutdown_v']) * 1000.0
        return {
            'active': self.ups.available,
            'value': [max(0.0, min(margin, CHART_MAX_MARGIN_MV))],
            'max': CHART_MAX_MARGIN_MV,
            'unit': 'mV',
        }

    # ----------------------------------------------------------------- 绘制
    def draw(self, key, mouse):
        height, width, first = self.size_page()
        # Clear the first line (the top row is reused by every page)
        try:
            self.stdscr.move(first, 0)
            self.stdscr.clrtoeol()
        except curses.error:
            pass
        line = first
        line += self._draw_header(line, width)
        line += self._draw_state(line, width)
        line += self._draw_gauges(line, width)
        line += self._draw_values(line, width)
        remaining = height - line - 1
        if remaining > 0:
            self._draw_charts(line, remaining, width)

    def _draw_header(self, pos_y, width):
        left = 'UPS'
        right = 'INA219 @ i2c-%d / 0x%02X' % (self.ups.bus, self.ups.addr)
        source = {'daemon': 'ups-monitor', 'direct': 'INA219 直读'}.get(self.ups.source, '未检测')
        try:
            self.stdscr.addstr(pos_y, 0, left, curses.A_BOLD)
            self.stdscr.addstr(pos_y, display_width(left) + 2, right, curses.A_NORMAL)
            tag = '[源: %s]' % source
            tag_x = width - display_width(tag) - 1
            if tag_x > display_width(left) + display_width(right) + 4:
                color = curses.A_NORMAL if self.ups.source == 'daemon' else NColors.yellow()
                self.stdscr.addstr(pos_y, tag_x, tag, color)
        except curses.error:
            pass
        return 2

    def _draw_state(self, pos_y, width):
        text, severity = self.ups.label, self.ups.severity
        color = SEVERITY_COLOR.get(severity, lambda: curses.A_NORMAL)()
        try:
            self.stdscr.addstr(pos_y, 0, '状态', curses.A_BOLD)
            self.stdscr.addstr(pos_y, 6, text, color | curses.A_BOLD)
        except curses.error:
            pass
        # Warning banner when the data is inferred rather than authoritative —
        # a locally guessed state must never look like the daemon's verdict.
        if self.ups.source != 'daemon':
            note = '（本地推断，非 ups-monitor 判定）' if self.ups.available else (self.ups.error or '未检测到 UPS')
            try:
                self.stdscr.addstr(pos_y, 6 + display_width(text) + 2, note, NColors.italic())
            except curses.error:
                pass
        # Right-aligned headline numbers
        if self.ups.available:
            charge = self.ups.percent
            eta = fmt_eta(self.ups.eta_min)
            right = '电量 %s  剩余 %s' % ('%.1f%%' % charge if charge is not None else '—', eta)
            right_x = width - display_width(right) - 1
            if right_x > 40:
                try:
                    self.stdscr.addstr(pos_y, right_x, right, curses.A_BOLD)
                except curses.error:
                    pass
        return 2

    def _draw_gauges(self, pos_y, width):
        if not self.ups.available:
            return 1
        gauge_w = min(width - 2, 60)
        charge = self.ups.percent or 0.0
        if charge <= 25:
            bar_color = NColors.red()
        elif charge <= 50:
            bar_color = NColors.yellow()
        else:
            bar_color = NColors.green()
        data = {
            'name': 'Chg',
            'color': bar_color,
            'values': [(charge, bar_color)],
            'mright': '%.1f%%' % charge,
        }
        try:
            basic_gauge(self.stdscr, pos_y, 1, gauge_w, data, bar='#')
        except curses.error:
            pass
        # Current gauge: |I| against a 1.5 A full scale, direction spelled out.
        current = self.ups.ema_i or 0.0
        cur_color = NColors.cyan() if self.ups.on_battery else NColors.green()
        data = {
            'name': 'Cur',
            'color': cur_color,
            'values': [(abs(current) / CHART_MAX_CURRENT * 100.0, cur_color)],
            'mright': '%+.2fA %s' % (current, self.ups.current_meaning),
        }
        try:
            basic_gauge(self.stdscr, pos_y + 1, 1, gauge_w, data, bar='=')
        except curses.error:
            pass
        return 3

    def _draw_values(self, pos_y, width):
        ups = self.ups
        key_w = 12
        two_columns = width >= 96
        col2 = max(48, width // 2 + 2)

        if not ups.available:
            plot_key_value(self.stdscr, pos_y, 1, '数据源',
                           ups.error or '未检测到 UPS', NColors.red(), key_width=key_w)
            return 2

        # The module's percentage is a linear interpolation over voltage and
        # reads optimistic across the Li-ion plateau, so say so on screen.
        left = [
            ('电压(平滑)', '%.3f V' % (ups.ema_v or 0.0), curses.A_NORMAL),
            ('电流', '%+.3f A  %s' % (ups.ema_i or 0.0, ups.current_meaning), curses.A_NORMAL),
            ('电量', '%.1f %% (电压估算, 平台上段偏高)' % (ups.percent or 0.0), curses.A_NORMAL),
            ('模式', ups.mode, NColors.yellow() if ups.dry_run else NColors.red()),
        ]
        right = [
            ('瞬时电压', '%.3f V' % ups.bus_v if ups.bus_v is not None else '—', curses.A_NORMAL),
            ('功率', '%.2f W' % (ups.power_w or 0.0), curses.A_NORMAL),
            ('剩余(外推)', fmt_eta(ups.eta_min), curses.A_NORMAL),
            ('电压斜率', self._slope_text(), curses.A_NORMAL),
        ]
        for idx, (name, value, color) in enumerate(left):
            plot_key_value(self.stdscr, pos_y + idx, 1, name, value, color, key_width=key_w)
        row = len(left)
        if two_columns:
            for idx, (name, value, color) in enumerate(right):
                plot_key_value(self.stdscr, pos_y + idx, col2, name, value, color, key_width=key_w)
        else:
            for idx, (name, value, color) in enumerate(right):
                plot_key_value(self.stdscr, pos_y + row + idx, 1, name, value, color, key_width=key_w)
            row += len(right)

        # Thresholds, with the one currently in play highlighted.
        label = '阈值'
        try:
            self.stdscr.addstr(pos_y + row, 1, label + ':', curses.A_BOLD)
        except curses.error:
            pass
        x = 1 + display_width(label) + 2
        for name, key in (('warn', 'warn_v'), ('low', 'low_v'),
                          ('shutdown', 'shutdown_v'), ('crit', 'crit_v')):
            value = ups.thresholds[key]
            active = bool(ups.on_battery) and ups.ema_v is not None and ups.ema_v <= value
            text = ' %s %.1fV ' % (name, value)
            try:
                self.stdscr.addstr(pos_y + row, x, text,
                                   NColors.red() if active else curses.A_NORMAL)
            except curses.error:
                pass
            x += display_width(text)
        row += 1

        # Most recent observed transition. The daemon logs these to journald,
        # but journald needs root to read, so observe them here instead.
        head = '最近迁移:'
        events = ups.events
        try:
            self.stdscr.addstr(pos_y + row, 1, head, curses.A_BOLD)
        except curses.error:
            pass
        x = 1 + display_width(head) + 2
        if events:
            _, old_state, new_state = events[-1]
            text = '%s → %s' % (self._state_cn(old_state), self._state_cn(new_state))
            try:
                self.stdscr.addstr(pos_y + row, x, text, NColors.yellow())
            except curses.error:
                pass
            x += display_width(text) + 2
            if len(events) > 1 and x + 30 < width:
                try:
                    self.stdscr.addstr(
                        pos_y + row, x,
                        '(本会话 %d 次 · 完整日志: sudo journalctl -u ups-monitor)' % len(events),
                        NColors.italic())
                except curses.error:
                    pass
        else:
            try:
                self.stdscr.addstr(pos_y + row, x, '本会话内无状态切换', NColors.italic())
            except curses.error:
                pass
        return row + 2

    def _slope_text(self):
        slope = self.ups.slope_mv_per_min()
        if slope is None:
            if not self.ups.on_battery:
                return '仅电池供电时统计'
            span = self.ups.slope_span_seconds
            if span < MIN_SLOPE_SPAN:
                return '采集中 %d/%ds' % (int(span), int(MIN_SLOPE_SPAN))
            return '采样窗口不足'
        if slope > 1.0:
            # Discharging into a rise means the fit is noise-dominated.
            return '+%.1f mV/min (异常上升)' % slope
        if slope > -1.0:
            return '≈0 mV/min (平台段)'
        return '%.1f mV/min' % slope

    @staticmethod
    def _state_cn(state):
        return state_label(state)[0]

    # ----------------------------------------------------------------- 图表
    def _draw_charts(self, pos_y, available, width):
        charts = self._charts
        # Columns first: a wide, shallow SSH window fits more charts side by
        # side than stacked, which is the same trade-off the CPU page makes.
        columns = max(1, min(len(charts), width // 46))
        groups = int(math.ceil(len(charts) / float(columns)))
        if groups == 1:
            # Single row of charts: use all remaining rows, more vertical cells
            # means a finer voltage resolution.
            per_chart = max(4, available - 1)
        else:
            per_chart = min(10, max(4, available // groups))
        if available < groups * 4 or width < 34:
            try:
                self.stdscr.addstr(pos_y, 1, '窗口太小, 历史曲线已隐藏 (拉高/拉宽终端即可恢复)',
                                   NColors.italic())
            except curses.error:
                pass
            return
        gap = 1
        col_w = (width - 2 - gap * (columns - 1)) // columns
        # Chart reserves 6 cells on the right for its Y-axis labels, and
        # lib/chart.py writes them starting at (right edge - 3) with no width
        # check. A '3000.0mV' label therefore needs the chart to stop 7 columns
        # short of the terminal edge, otherwise curses wraps it onto the next
        # line and shreds the layout below.
        right_limit = width - 8
        for idx, chart in enumerate(charts):
            row = pos_y + (idx // columns) * per_chart
            col = 1 + (idx % columns) * (col_w + gap)
            last_in_row = (idx % columns) == columns - 1
            y_label = last_in_row or columns == 1
            end = col + col_w - (1 if y_label else 2)
            chart.draw(self.stdscr, [col, min(end, right_limit)],
                       [row, row + per_chart - 1],
                       label=self._chart_label(idx), y_label=y_label)

    def _chart_label(self, idx):
        ups = self.ups
        if idx == 0:
            return '%.2fW' % (ups.power_w or 0.0)
        if idx == 1:
            return '%+.2fA %s' % (ups.ema_i or 0.0, ups.current_meaning)
        if ups.ema_v is None:
            return '—'
        return '距关机线 %+.0fmV' % ((ups.ema_v - ups.thresholds['shutdown_v']) * 1000.0)


# EOF
