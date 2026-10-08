#!/bin/bash
# nx6: hide the phantom "nvme Sensor 2" reading from jtop (jetson-stats).
# Idempotent: safe to re-run, and re-run after any `pip install -U jetson-stats`.
set -e

PYFILE=/usr/local/lib/python3.12/dist-packages/jtop/core/temperature.py
IGNORE=/usr/local/jtop/sensors-ignore.json
MARK="nx6 phantom-sensor patch"

# root-aware runner: works both as the nx user (manual) and as root (systemd timer).
run() {
    if [ "$(id -u)" = "0" ]; then
        "$@"
    else
        echo nx | sudo -S "$@"
    fi
}

if grep -q "$MARK" "$PYFILE" 2>/dev/null; then
    echo "already patched -> nothing to do"
    exit 0
fi

# --- 1) external ignore list (edit this file to change behaviour) ---
cat > /tmp/sensors-ignore.json <<'JSON'
[
  "nvme Sensor 2"
]
JSON
run cp /tmp/sensors-ignore.json "$IGNORE"
run chmod 644 "$IGNORE"

# --- 2) backup original ---
run cp -n "$PYFILE" "$PYFILE.orig"

# --- 3) patcher ---
cat > /tmp/jtop_patch.py <<'PY'
import sys

p = "/usr/local/lib/python3.12/dist-packages/jtop/core/temperature.py"
src = open(p).read()

if "nx6 phantom-sensor patch" in src:
    print("already patched")
    raise SystemExit(0)

# a) make json available
a1 = "import os\nimport re\n"
assert a1 in src, "import anchor missing"
src = src.replace(a1, "import os\nimport re\nimport json\n", 1)

# b) helper right before the hwmon scanner
helper = '''# --- nx6 phantom-sensor patch ---
# Drop sensors whose full name is listed in /usr/local/jtop/sensors-ignore.json.
# Used to hide the phantom "nvme Sensor 2" reading reported by the Crucial
# CT1000E100SSD8 firmware (constant 89.85 C, never moves under load, ignored
# by the drive's own thermal management).
JTOP_IGNORE_FILE = "/usr/local/jtop/sensors-ignore.json"


def _jtop_ignore_sensors():
    try:
        with open(JTOP_IGNORE_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            return tuple(str(x) for x in data)
    except Exception:
        pass
    return ()
# --- end nx6 phantom-sensor patch ---


'''
a2 = "def get_hwmon_thermal_system(root_dir):"
assert a2 in src, "function anchor missing"
src = src.replace(a2, helper + a2, 1)

# c) filter on the way out
old = "            if sensor:\n                sensor_name[raw_name] = sensor\n    return sensor_name"
new = ("            if sensor:\n                sensor_name[raw_name] = sensor\n"
       "    # --- nx6 phantom-sensor patch ---\n"
       "    for _ignored in _jtop_ignore_sensors():\n"
       "        sensor_name.pop(_ignored, None)\n"
       "    # --- end nx6 phantom-sensor patch ---\n"
       "    return sensor_name")
assert old in src, "return anchor missing"
src = src.replace(old, new, 1)

open(p, "w").write(src)
print("patched OK")
PY

run python3 /tmp/jtop_patch.py

# --- 4) verify ---
echo "--- verification ---"
python3 -c "from jtop.core.temperature import get_hwmon_thermal_system as g; ks=sorted(g('/sys/class/hwmon').keys()); print('sensors now:', ks); print('nvme Sensor 2 present:', 'nvme Sensor 2' in ks)"

rm -f /tmp/jtop_patch.py /tmp/sensors-ignore.json
echo "done"
