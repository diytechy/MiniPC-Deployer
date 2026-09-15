#!/usr/bin/env bash
# Offline image installer; never enrolls faces, creates a PIN or starts capture.
# Inputs: --wheelhouse OFFLINE_LOCKED_WHEELS; optional gateway registration and
# private digest-pinned model bundle are independent additions.
# Assumes the normal wall app artifact is already installed. Implements: SR-017.
set -euo pipefail
fail() { echo "[wall-capabilities] $*" >&2; exit 1; }
atomic_install() { # SOURCE TARGET OWNER GROUP MODE
    local source=$1 target=$2 owner=$3 group=$4 mode=$5 temporary
    temporary=$(mktemp "$(dirname "$target")/.$(basename "$target").XXXXXX") || return 1
    if install -o "$owner" -g "$group" -m "$mode" "$source" "$temporary" \
            && python3 - "$temporary" "$target" <<'PY'
import os, sys
temporary, target = sys.argv[1:]
with open(temporary, 'rb') as stream:
    os.fsync(stream.fileno())
os.replace(temporary, target)
directory = os.open(os.path.dirname(target), os.O_RDONLY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
    then
        return 0
    fi
    rm -f "$temporary"
    return 1
}
host_config= wheelhouse= models= model_manifest=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --host-config) [ "$#" -ge 2 ] || fail "--host-config needs a path"; host_config=$2; shift 2 ;;
        --wheelhouse) [ "$#" -ge 2 ] || fail "--wheelhouse needs a path"; wheelhouse=$2; shift 2 ;;
        --models) [ "$#" -ge 2 ] || fail "--models needs a path"; models=$2; shift 2 ;;
        --model-manifest) [ "$#" -ge 2 ] || fail "--model-manifest needs a path"; model_manifest=$2; shift 2 ;;
        *) fail "usage: $0 --wheelhouse DIR [--host-config PRIVATE_JSON] [--models DIR --model-manifest JSON]" ;;
    esac
done
[ "$(id -u)" = 0 ] || fail "Run through the owner SSH administration path as root"
[ -d "$wheelhouse" ] || fail "The offline wheelhouse is required"
[ -z "$host_config" ] || [ -f "$host_config" ] || fail "Private host config does not exist"
[ -z "$models$model_manifest" ] || { [ -d "$models" ] && [ -f "$model_manifest" ]; } \
    || fail "Models and model manifest must be supplied together"
[ -f "$wheelhouse/requirements.lock" ] || fail "Wheelhouse needs a complete hash-pinned requirements.lock"
# Read the lock before pip does. pip enforces the hashes but reports a missing
# wheel or an unpinned transitive dependency as a late, opaque resolver error;
# this refuses such a wheelhouse by name, and refuses one that would send pip to
# an index. Built by build-sensor-wheelhouse.sh; see sensor-wheelhouse/README.md.
# --expect anchors authenticity: the media's lock must be byte-identical to the
# reviewed lock that shipped inside this panel image. Without it, substituted
# media could carry its own self-consistent lock and pass every hash check.
reviewed_lock="$(dirname "$0")/sensor-wheelhouse/requirements.lock"
[ -f "$reviewed_lock" ] || fail "The reviewed requirements.lock is missing from the staged image payload"
python3 "$(dirname "$0")/check-wheelhouse-lock.py" --expect "$reviewed_lock" "$wheelhouse"     || fail "Offline wheelhouse rejected; rebuild it with build-sensor-wheelhouse.sh"
app_dir=/opt/wall-panel/app/runtime/resources/app
[ -f "$app_dir/sensors/service.py" ] || fail "Install the matching wall app artifact first"
python3 - "$app_dir" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
try:
    manifest = json.loads((root / 'capabilities.json').read_text(encoding='utf-8'))
    declaration = manifest['capabilities']['local-capabilities-v1']['app']
    required = {'electron/panel-access.cjs', 'electron/sensor-observations.cjs', 'sensors/config.py'}
    if manifest.get('schemaVersion') != 2 or not isinstance(declaration, list) or not required <= set(declaration):
        raise ValueError()
    if any(not (root / name).is_file() for name in required):
        raise ValueError()
except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit('Wall app lacks the compatible local-capabilities-v1 contract')
PY
command -v ffmpeg >/dev/null || fail "ffmpeg is missing from the wall image"
id panel >/dev/null 2>&1 || fail "The standard panel account is missing"
if [ -n "$host_config" ]; then
python3 - "$host_config" <<'PY'
import json, os, stat, sys
from urllib.parse import urlsplit
try:
    info = os.stat(sys.argv[1])
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in (0o600, 0o640): raise ValueError()
    data = json.load(open(sys.argv[1]))
    url = urlsplit(data['gatewayUrl'])
    if data.get('enabled') is not True or not data.get('deviceId') or not data.get('deviceCredential'): raise ValueError()
    if data.get('accessMode') not in {'read-protected', 'write-only'}: raise ValueError()
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment: raise ValueError()
    if data.get('sensorSocket') != '/run/wall-sensors/service.sock': raise ValueError()
except (ValueError, KeyError, OSError, TypeError):
    sys.exit('Private host config invalid; no configuration values were printed')
PY
fi
# Packages are never fetched on the panel. A complete reviewed wheel lock is a
# build input; pip refuses unpinned transitive dependencies or hash mismatches.
install -d -m 0755 /opt/wall-sensors
if [ -x /opt/wall-sensors/venv/bin/python ] \
        && cmp -s "$wheelhouse/requirements.lock" /opt/wall-sensors/requirements.lock \
        && /opt/wall-sensors/venv/bin/python -c 'import numpy, PIL, cryptography, onnxruntime, dbus_next' >/dev/null 2>&1; then
    echo "[wall-capabilities] Existing verified sensor runtime matches the image lock; preserving it"
else
    venv_new=$(mktemp -d /opt/wall-sensors/.venv.XXXXXX)
    cleanup_venv() { rm -rf "$venv_new"; }
    trap cleanup_venv EXIT
    python3 -m venv "$venv_new"
    "$venv_new/bin/pip" install --no-index --only-binary=:all: --require-hashes \
        --find-links "$wheelhouse" -r "$wheelhouse/requirements.lock"
    "$venv_new/bin/python" -c 'import numpy, PIL, cryptography, onnxruntime, dbus_next'
    rm -rf /opt/wall-sensors/venv.previous
    [ ! -e /opt/wall-sensors/venv ] || mv -T /opt/wall-sensors/venv /opt/wall-sensors/venv.previous
    # mktemp creates 0700; the unprivileged service must traverse this tree.
    chmod 0755 "$venv_new"
    mv -T "$venv_new" /opt/wall-sensors/venv
    venv_new=
    install -m 0644 "$wheelhouse/requirements.lock" /opt/wall-sensors/requirements.lock
    rm -rf /opt/wall-sensors/venv.previous
    trap - EXIT
fi

# Models are an optional private image input. Verify exact bytes before making
# the directory visible; a missing bundle leaves only face functions unready.
if [ -n "$models" ]; then
    models_new=$(mktemp -d /opt/wall-sensors/.models.XXXXXX)
    cleanup_models() { rm -rf "$models_new"; }
    trap cleanup_models EXIT
    python3 - "$models" "$model_manifest" "$models_new" <<'PY'
import hashlib, json, pathlib, shutil, sys
source, manifest_path, target = map(pathlib.Path, sys.argv[1:])
manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
if isinstance(manifest, dict) and set(manifest) == {'schemaVersion', 'modelManifest'}:
    if manifest['schemaVersion'] != 1: raise SystemExit('Unsupported model manifest version')
    manifest = manifest['modelManifest']
names = {'det_10g.onnx', 'w600k_r50.onnx'}
if not isinstance(manifest, dict) or set(manifest) != names:
    raise SystemExit('Model manifest must name exactly the supported detector and recognizer')
for name in sorted(names):
    digest = manifest[name]
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise SystemExit('Model manifest has an invalid digest')
    path = source / name
    if not path.is_file() or path.is_symlink(): raise SystemExit('A required model is missing')
    hasher = hashlib.sha256()
    with path.open('rb') as model:
        for block in iter(lambda: model.read(1024 * 1024), b''):
            hasher.update(block)
    if hasher.hexdigest() != digest: raise SystemExit('Model digest mismatch')
    shutil.copyfile(path, target / name)
(target / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
PY
    chmod 0644 "$models_new"/*.onnx "$models_new/manifest.json"
    rm -rf /opt/wall-sensors/models.previous
    [ ! -e /opt/wall-sensors/models ] || mv -T /opt/wall-sensors/models /opt/wall-sensors/models.previous
    # Model bytes are public runtime inputs, not enrollment or private keys.
    chmod 0755 "$models_new"
    mv -T "$models_new" /opt/wall-sensors/models
    models_new=
    rm -rf /opt/wall-sensors/models.previous
    trap - EXIT
fi
getent group wall-sensors >/dev/null || groupadd --system wall-sensors
id wall-sensors >/dev/null 2>&1 || useradd --system --gid wall-sensors --no-create-home --shell /usr/sbin/nologin wall-sensors
usermod -a -G wall-sensors panel
install -d -m 0755 /etc/wall-panel
# Preserve the rendererConfig already materialized from this panel's wall.env;
# the transferred access registration must not replace locally-owned secrets.
if [ -n "$host_config" ]; then
merged_config=$(mktemp /etc/wall-panel/.host.install.XXXXXX)
cleanup_merged_config() { rm -f "$merged_config"; }
trap cleanup_merged_config EXIT
python3 - "$host_config" /etc/wall-panel/host.json > "$merged_config" <<'PY'
import json, os, stat, sys
incoming = json.load(open(sys.argv[1]))
# The transfer supplies access registration only. Renderer secrets have one
# source: this panel's wall.env-derived current file.
incoming.pop('rendererConfig', None)
try:
    info = os.stat(sys.argv[2])
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in (0o600, 0o640): raise ValueError()
    current = json.load(open(sys.argv[2]))
    if isinstance(current.get('rendererConfig'), dict):
        incoming['rendererConfig'] = current['rendererConfig']
except FileNotFoundError:
    pass
json.dump(incoming, sys.stdout, separators=(',', ':'), sort_keys=True)
sys.stdout.write('\n')
PY
atomic_install "$merged_config" /etc/wall-panel/host.json root panel 0640
rm -f "$merged_config"
trap - EXIT
fi
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
            if result.get('ok') is True and result.get('result', {}).get('protocolVersion') == 2:
                print('[wall-capabilities] PASS sensor protocol 2 reachable as panel UID')
                sys.exit(0)
    except (OSError, ValueError, TypeError):
        pass
    time.sleep(0.25)
sys.exit('Sensor protocol verification failed as panel UID; inspect the service journal')
PY
echo "[wall-capabilities] Sensor runtime installed independently of gateway access; no camera capture was started by this installer."
echo "[wall-capabilities] Restart the kiosk session to acquire its socket group. PIN enrollment and hub cutover remain explicit."
