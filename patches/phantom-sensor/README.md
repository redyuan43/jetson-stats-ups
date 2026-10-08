# jtop phantom-sensor patch (nx6)

Hide the phantom **`nvme Sensor 2`** reading that jtop (jetson-stats) shows for the
**Crucial / Micron CT1000E100SSD8** (1 TB) NVMe on nx6 (Jetson Orin NX 16 GB Super).

## Why

The drive firmware exposes a second temperature sensor that is a **phantom**:
`/sys/class/hwmon/hwmon1/temp3_input` reads a constant **89.85 °C** and never moves
under load (verified across 4 fio stress rounds / ~7 min of sustained I/O). The real
sensor is the **composite** one (`temp1_input`), which climbs 60.8 °C → 80.85 °C under
sustained writes. The drive's own NVMe thermal management only uses the composite
sensor, so `Sensor 2` does not participate in throttling — it is purely a cosmetic
red alert in jtop.

jtop 7.2.2's `jtop/core/temperature.py::get_hwmon_thermal_system()` **blindly enumerates**
`/sys/class/hwmon/*/temp*_input` and has **no config option or CLI flag** to filter
sensors. So the only way to hide it is a small runtime patch.

## What this is

A **runtime hot-patch** (not a source-fork change): an idempotent script that

1. writes an external ignore list to `/usr/local/jtop/sensors-ignore.json`
   (`["nvme Sensor 2"]` — edit this file to change behaviour, no code change needed),
2. backs up the original `temperature.py` to `temperature.py.orig`,
3. injects a `_jtop_ignore_sensors()` helper + a filter on the way out of
   `get_hwmon_thermal_system()` (marked `nx6 phantom-sensor patch`),
4. verifies the sensor is gone.

It is **root-aware** (`run()` helper): works as the `nx` user (manual) and as root
(systemd timer).

## Auto re-apply after `pip install -U jetson-stats`

A **systemd timer** re-runs the idempotent script every 10 min (and 2 min after boot).
A timer is used instead of a `path` unit because pip upgrades **replace the file by
rename**, which inotify `PathChanged` on a single file can miss.

```
/etc/systemd/system/jtop-phantom-fix.service   (oneshot, runs the script)
/etc/systemd/system/jtop-phantom-fix.timer     (OnBootSec=2min / OnUnitActiveSec=10min / Persistent)
```

## Install on a Jetson

```bash
# 1) install the script (system location, root) and the user copy
sudo cp jtop-phantom-fix.sh /usr/local/bin/jtop-phantom-fix.sh
sudo chmod 755 /usr/local/bin/jtop-phantom-fix.sh

# 2) install + enable the units
sudo cp jtop-phantom-fix.service jtop-phantom-fix.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jtop-phantom-fix.timer

# 3) verify
python3 -c "from jtop.core.temperature import get_hwmon_thermal_system as g; \
  ks=sorted(g('/sys/class/hwmon').keys()); print(ks)"
# -> ['nvme Composite']  (no 'nvme Sensor 2')
```

> The patch does **not** affect an already-running jtop process (code is loaded into
> memory at start). Quit and relaunch jtop to see the change.

## Notes

- Single-machine patch: the phantom sensor is specific to this drive's firmware. On a
  board without it, the script is a harmless no-op (nothing in the ignore list matches).
- To fully revert: `sudo cp temperature.py.orig temperature.py` and remove the two
  systemd units, or just delete `/usr/local/jtop/sensors-ignore.json` to keep the code
  but re-enable the sensor.
