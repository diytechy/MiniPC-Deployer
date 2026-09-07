#!/usr/bin/env bash
# Firstboot-only configuration and readiness assertion. Implements: SR-017.
set -euo pipefail
payload=$(cd -- "$(dirname -- "$0")" && pwd)
app_dir=/opt/wall-panel/app/runtime/resources/app
config_dir=/etc/wall-panel
rules=/etc/udev/rules.d/99-wall-touch-filter.rules
env_file=${WALL_ENV_FILE:-/etc/wall-panel/wall.env}
[ -r "$env_file" ] || { echo 'Touch filter: wall.env unavailable' >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$env_file"
set +a
mode=${TOUCH_FILTER_MODE:-off}
case "$mode" in off|shadow|filter) ;; *) echo 'Invalid TOUCH_FILTER_MODE' >&2; exit 1;; esac
if [ "$mode" = off ]; then
    # OFF is also the SSH recovery path and works without an app/Python module.
    if [ -f /etc/systemd/system/wall-touch-filter.service ]; then
        systemctl disable --now wall-touch-filter.service
    fi
    rm -f -- "$config_dir/touch-filter.enabled" "$rules" /etc/modules-load.d/wall-touch-filter.conf /usr/lib/systemd/system-sleep/wall-touch-filter
    udevadm control --reload
    udevadm trigger --subsystem-match=input
    echo 'Touch filter OFF. Restart the kiosk session if it previously ignored the raw digitizer.'
    exit 0
fi
[ -f "$app_dir/touchfilter/daemon.py" ] || { echo 'Touch filter missing from app artifact; rebuild OfficeWallNaglight' >&2; exit 1; }
python3 -c 'import evdev' || { echo 'python3-evdev missing from offline wall image' >&2; exit 1; }
install -d -m 0755 "$config_dir" /etc/udev/rules.d
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
PYTHONPATH="$app_dir" python3 "$payload/render-touch-filter.py" "$scratch/config.json" "$scratch/rules"
install -m 0644 "$scratch/config.json" "$config_dir/touch-filter.json"
install -m 0644 "$payload/wall-touch-filter.service" /etc/systemd/system/wall-touch-filter.service
install -m 0755 "$payload/wall-touch-filter-sleep" /usr/lib/systemd/system-sleep/wall-touch-filter
touch "$config_dir/touch-filter.enabled"
if [ "$mode" = filter ]; then
    modprobe uinput
    printf 'uinput\n' > /etc/modules-load.d/wall-touch-filter.conf
fi
# Start and verify the virtual/shadow reader BEFORE optionally isolating raw
# input. Failure here leaves the existing udev policy unchanged.
systemctl daemon-reload
systemctl enable wall-touch-filter.service
systemctl restart wall-touch-filter.service
python3 - "$mode" <<'PY'
import json,time,sys
from pathlib import Path
start=time.time()
for _ in range(100):
    try:
        state=json.loads(Path('/run/wall-touch-filter/status.json').read_text())
        if state.get('protocolVersion')==1 and state.get('mode')==sys.argv[1] and state.get('health')=='ready' and state.get('observedAt',0)>=start*1000:
            print('PASS touch filter: fresh daemon readiness verified');break
    except (OSError,ValueError):pass
    time.sleep(.1)
else:raise SystemExit('Touch filter did not produce fresh readiness; udev isolation not changed')
PY
install -m 0644 "$scratch/rules" "$rules"
udevadm control --reload
udevadm trigger --subsystem-match=input
echo 'Touch filter configured. Restart the kiosk session for device discovery/isolation changes.'
