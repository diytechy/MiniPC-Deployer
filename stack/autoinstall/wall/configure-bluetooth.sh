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

enabled=${WALL_BLUETOOTH_ENABLED:-true}
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

# Assert now as well as at boot, so a re-run takes effect without a reboot. A
# failure here is reported but does not fail firstboot: the policy is installed
# and the boot unit will assert it again, and an adapter that is merely still in
# BlueZ's default state is not worth abandoning a whole firstboot over.
if ! systemctl restart wall-bluetooth.service; then
    echo 'Bluetooth: at-rest policy could not be asserted now; it will be applied at boot' >&2
fi
/usr/local/sbin/wall-bluetooth-pairing status || true
echo "[wall-bluetooth] Policy installed. Open a pairing window with: sudo wall-bluetooth-pairing open"
