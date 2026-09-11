# jtop UPS Page — jetson_stats fork

This is a **fork of [rbonghi/jetson_stats](https://github.com/rbonghi/jetson_stats) (jtop 7.2.2)** that adds a dedicated **UPS battery page** to jtop, designed for the **Waveshare UPS Power Module (C)** on NVIDIA Jetson Orin (INA219 fuel gauge on I2C).

> Upstream code is untouched except for the 2-line page registration described below.
> License: **AGPL-3.0** (inherited from upstream).

## What this fork adds

| File | Purpose |
|---|---|
| `jtop/core/ups.py` (new) | UPS data reader: daemon `status.json` first, direct INA219 over I2C as fallback; EMA filtering, battery-% interpolation, least-squares ETA slope, state machine labels |
| `jtop/gui/pups.py` (new) | The jtop page (`UPS` tab): status line, gauges, key/value panel, 3 fixed-scale history charts |
| `jtop/gui/__init__.py` | +1 line: export the page |
| `jtop/__main__.py` | +8/-1 lines: import + auto-register the page **only when a UPS battery is detected** (`ups_available()`) |

## The UPS page (tab `8UPS`)

Appears automatically between `7SYSFS` and `9INFO` — only when a UPS module is present (daemon status file or INA219 responds on the configured bus/address).

Content on the page (mirrors the standalone UPS monitor CLI):

- **State line** — AC / BATTERY / LOW / SHUTDOWN_PENDING / CRIT with color coding, plus data source (`[源: ups-monitor]` = daemon status file, `[源: 直连INA219]` = direct I2C fallback)
- **Gauges** — battery percent (voltage-interpolated 9.0–12.6 V for 3S Li-ion), load current, load power
- **Key/value panel** — EMA voltage, instantaneous voltage, current, power, percent, ETA to shutdown threshold, mode (dry-run or live), **voltage slope**, configured thresholds, last AC↔battery transition
- **3 history charts** (10-minute window, fixed scales so they never rescale-jump): Power [W], Current [A], headroom-to-shutdown-threshold [mV]

## Engineering notes (the hard-won parts)

1. **Single source of truth**: the page reads the same `/run/ups-monitor/status.json` written by the `ups-monitor` shutdown daemon. The page must never disagree with the process that actually powers the board off.
2. **INA219 first-read bias**: after writing CAL/CONFIG, the first bus-voltage conversion is ~40% high. The driver sleeps 0.08 s and discards one throwaway read.
3. **Slope SNR**: INA219 bus LSB is 4 mV while a 3S plateau discharges at ~12 mV/min — a naive 15 s least-squares fit is pure noise (observed ±7 mV/min swings, even *positive* slope while discharging). Fix: 300 s window + 120 s minimum time-span gate + 20 s bucket averaging → measured MAE 0.04 mV/min on synthetic data and a stable −8.4…−11.2 mV/min on a real 150 s discharge (previously -4.3…+7.3). Until the gate is satisfied the UI shows sampling progress instead of inventing a number.
4. **CJK alignment**: upstream `plot_name_info` uses `len()`, which breaks column alignment for Chinese labels; the page uses an east-asian-width-aware renderer.

## Using with the ups-monitor daemon (recommended)

The companion daemon (low-battery auto-shutdown for Waveshare UPS (C)) writes `/run/ups-monitor/status.json` every sample. Point `STATUS_PATH` in `jtop/core/ups.py` there (default matches). Without the daemon, the page falls back to reading INA219 directly (bus/addr constants at the top of the same file).

## Install

```bash
# on the Jetson, from this repo
python3 -m pip wheel . --no-deps --no-build-isolation   # needs setuptools>=68, packaging>=23.2
python3 -m pip install ./jetson_stats-7.2.2-py3-none-any.whl
sudo systemctl restart jtop.service
jtop   # press 8 for the UPS tab
```

A ready-made `jtop-ups-page.patch` (against upstream 7.2.2) ships in the `patches/` branch/doc of the author's workspace; applying it to a pristine 7.2.2 checkout reproduces this fork exactly.

## Not included / upstream status

This is intentionally a personal fork — no PR to upstream (page is hardware-specific to Waveshare UPS (C) + INA219 wiring on bus 7 / addr 0x41, and the daemon integration assumes `/run/ups-monitor`). If you adapt it, `jtop/core/ups.py` top-of-file constants are the only things to touch.
