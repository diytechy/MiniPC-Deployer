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

# ── The A2DP sink: what actually carries a phone's audio into the room ───────
# bluez alone pairs and connects; it moves no audio. bluealsa provides the sink
# and bluealsa-aplay writes what arrives to the ALSA `default` PCM -- which in
# trigger mode is the loopback the amplifier's detector already listens to, so
# the amp follows Bluetooth audio with no change to the trigger daemon.
#
# Both units ship with the package; we only override them. Overriding rather
# than replacing keeps the distribution's sandboxing, which is extensive and
# worth having, and confines our edits to the two things it cannot know about:
# the sink-only profile and the dmix IPC the panel's audio path depends on.
if [ "$enabled" = true ]; then
    command -v bluealsa >/dev/null || {
        echo 'Bluetooth: bluez-alsa-utils missing from the wall image' >&2; exit 1; }
    for unit in bluealsa bluealsa-aplay; do
        install -d -m 0755 "/etc/systemd/system/$unit.service.d"
    done
    install -m 0644 "$payload/wall-bluealsa-override.conf"         /etc/systemd/system/bluealsa.service.d/wall.conf
    install -m 0644 "$payload/wall-bluealsa-aplay-override.conf"         /etc/systemd/system/bluealsa-aplay.service.d/wall.conf
fi

systemctl daemon-reload
systemctl enable wall-bluetooth.service
if [ "$enabled" = true ]; then
    systemctl enable bluealsa.service bluealsa-aplay.service
    # Restart rather than start: a re-run must pick up a changed override.
    systemctl restart bluealsa.service bluealsa-aplay.service || {
        echo 'Bluetooth: the A2DP sink did not start; pairing would succeed and play nothing' >&2
        exit 1; }
else
    systemctl disable --now bluealsa.service bluealsa-aplay.service >/dev/null 2>&1 || true
fi

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
if [ "$enabled" = true ]; then
    echo "[wall-bluetooth] A2DP sink active. Paired devices play to the ALSA default PCM,"
    echo "[wall-bluetooth] which in trigger mode is what powers the amplifier."
fi
