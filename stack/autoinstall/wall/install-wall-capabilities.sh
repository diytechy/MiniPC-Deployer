#!/usr/bin/env bash
# Explicit offline opt-in installer; never enrolls faces or creates a PIN.
# Inputs: --host-config PRIVATE_JSON --wheelhouse OFFLINE_LOCKED_WHEELS.
# Assumes the normal wall app artifact is already installed. Implements: SR-017.
set -euo pipefail
fail() { echo "[wall-capabilities] $*" >&2; exit 1; }
host_config= wheelhouse=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --host-config) [ "$#" -ge 2 ] || fail "--host-config needs a path"; host_config=$2; shift 2 ;;
        --wheelhouse) [ "$#" -ge 2 ] || fail "--wheelhouse needs a path"; wheelhouse=$2; shift 2 ;;
        *) fail "usage: $0 --host-config PRIVATE_JSON --wheelhouse OFFLINE_LOCKED_WHEELS" ;;
    esac
done
[ "$(id -u)" = 0 ] || fail "Run through the owner SSH administration path as root"
[ -f "$host_config" ] && [ -d "$wheelhouse" ] || fail "Both private host config and offline wheelhouse are required"
[ -f "$wheelhouse/requirements.lock" ] || fail "Wheelhouse needs a complete hash-pinned requirements.lock"
app_dir=/opt/wall-panel/app/runtime/resources/app
[ -f "$app_dir/sensors/service.py" ] || fail "Install the matching wall app artifact first"
command -v ffmpeg >/dev/null || fail "ffmpeg is missing from the wall image"
id panel >/dev/null 2>&1 || fail "The standard panel account is missing"
python3 - "$host_config" <<'PY'
import json, os, stat, sys
from urllib.parse import urlsplit
try:
    info = os.stat(sys.argv[1])
    if info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode): raise ValueError()
    data = json.load(open(sys.argv[1]))
    url = urlsplit(data['gatewayUrl'])
    if data.get('enabled') is not True or not data.get('deviceId') or not data.get('deviceCredential'): raise ValueError()
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment: raise ValueError()
    if data.get('sensorSocket') != '/run/wall-sensors/service.sock': raise ValueError()
except (ValueError, KeyError, OSError, TypeError):
    sys.exit('Private host config invalid; no configuration values were printed')
PY
# Packages are never fetched on the panel. A complete reviewed wheel lock is a
# build input; pip refuses unpinned transitive dependencies or hash mismatches.
install -d -m 0755 /opt/wall-sensors
python3 -m venv /opt/wall-sensors/venv
/opt/wall-sensors/venv/bin/pip install --no-index --only-binary=:all: --require-hashes \
    --find-links "$wheelhouse" -r "$wheelhouse/requirements.lock"
/opt/wall-sensors/venv/bin/python -c 'import numpy, PIL, cryptography, onnxruntime, dbus_next'
getent group wall-sensors >/dev/null || groupadd --system wall-sensors
id wall-sensors >/dev/null 2>&1 || useradd --system --gid wall-sensors --no-create-home --shell /usr/sbin/nologin wall-sensors
usermod -a -G wall-sensors panel
install -d -m 0755 /etc/wall-panel
install -o panel -g panel -m 0600 "$host_config" /etc/wall-panel/host.json
printf 'PANEL_SENSOR_UID=%s\n' "$(id -u panel)" > /etc/wall-panel/sensors.env
chmod 0644 /etc/wall-panel/sensors.env
install -m 0644 "$(dirname "$0")/wall-sensors.service" /etc/systemd/system/wall-sensors.service
install -m 0644 "$(dirname "$0")/wall-sensors-dbus.conf" /etc/dbus-1/system.d/wall-sensors.conf
# Reload policy; do not restart BlueZ or the audio controller.
systemctl reload dbus.service
systemctl daemon-reload
systemctl enable --now wall-sensors.service
systemctl is-active --quiet wall-sensors.service || fail "Sensor service did not start"
# Type=simple can be active before imports or socket binding fail. Verify the
# actual protocol as the broker UID; a fresh runuser session picks up its group.
runuser -u panel -- python3 - <<'PY'
import json, socket, sys, time
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(1)
            connection.connect('/run/wall-sensors/service.sock')
            connection.sendall(b'{"method":"status","params":{}}\n')
            data = bytearray()
            while not data.endswith(b'\n') and len(data) < 65536:
                part = connection.recv(4096)
                if not part: break
                data.extend(part)
            result = json.loads(data)
            if result.get('ok') is True and result.get('result', {}).get('protocolVersion') == 1:
                print('[wall-capabilities] PASS sensor protocol 1 reachable as panel UID')
                sys.exit(0)
    except (OSError, ValueError, TypeError):
        pass
    time.sleep(0.25)
sys.exit('Sensor protocol verification failed as panel UID; inspect the service journal')
PY
echo "[wall-capabilities] Installed. Set WALL_HOST_CONFIG=/etc/wall-panel/host.json in wall.env and rerun wall-firstboot.sh."
echo "[wall-capabilities] Restart the kiosk session to acquire its new socket group. PIN/model provisioning and hub cutover remain explicit."
