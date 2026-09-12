#!/usr/bin/env bash
# Firstboot-only Bluetooth adapter policy installation. Implements: SR-017, SR-023.
#
# Renders the validated policy, installs the applier, the window helper and the
# boot unit, then asserts the at-rest state once so the running panel matches
# what it will do on its next boot. Registers no agent and pairs nothing.
set -euo pipefail
payload=$(cd -- "$(dirname -- "$0")" && pwd)
config_dir=/etc/wall-panel
env_file=${WALL_ENV_FILE:-/etc/wall-panel/wall.env}
[ -r "$env_file" ] || { echo 'Bluetooth: wall.env unavailable' >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$env_file"
set +a

# Must match render-bluetooth.py and wall.env.example. See the note there.
enabled=${WALL_BLUETOOTH_ENABLED:-false}
case "$enabled" in true|false) ;; *) echo 'Invalid WALL_BLUETOOTH_ENABLED' >&2; exit 1;; esac

command -v bluetoothctl >/dev/null || {
    echo 'Bluetooth: bluetoothctl missing from the wall image' >&2; exit 1; }

install -d -m 0755 "$config_dir" /usr/local/sbin
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT
# The renderer is the validator. A bad value fails here, before anything on the
# adapter has been touched.
python3 "$payload/render-bluetooth.py" "$scratch/bluetooth.json"
install -m 0644 "$scratch/bluetooth.json" "$config_dir/bluetooth.json"
install -m 0755 "$payload/wall-bluetooth-apply.py" /usr/local/sbin/wall-bluetooth-apply
install -m 0755 "$payload/wall-bluetooth-pairing.py" /usr/local/sbin/wall-bluetooth-pairing
install -m 0644 "$payload/wall-bluetooth.service" /etc/systemd/system/wall-bluetooth.service
systemctl daemon-reload
systemctl enable wall-bluetooth.service

# Assert now as well as at boot, so a re-run takes effect without a reboot.
#
# THIS IS FATAL, and it did not used to be. The old reasoning was that the boot
# unit would assert the policy again anyway -- true, but it means the window
# between a failed install and the next reboot is spent with the adapter in
# whatever state BlueZ left it, which on a panel that was previously
# discoverable and pairable is precisely the state this file exists to end.
# A security policy that could not be applied is a failed provisioning step.
if ! systemctl restart wall-bluetooth.service; then
    echo 'Bluetooth: at-rest adapter policy could not be asserted' >&2
    exit 1
fi
/usr/local/sbin/wall-bluetooth-pairing status || true
echo "[wall-bluetooth] Policy installed. Open a pairing window with: sudo wall-bluetooth-pairing open"
