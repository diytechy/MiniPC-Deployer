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
install -m 0755 "$payload/wall-bluetooth-agent.py" /usr/local/sbin/wall-bluetooth-agent
python3 -c 'import dbus, dbus.service, dbus.mainloop.glib; from gi.repository import GLib' || {
    echo 'Bluetooth: python3-dbus/python3-gi missing; the pairing agent cannot run' >&2; exit 1; }
install -m 0644 "$payload/wall-bluetooth.service" /etc/systemd/system/wall-bluetooth.service

# -- WSN-024: device management from the glass -------------------------------
# Until 2026-09-19 the only way to pair, trust or forget a device was an SSH
# session running `wall-bluetooth-pairing`. The Owner asked for all three on the
# panel, so the broker now has a routed-device backend and this is its root
# half: `wall-bluetooth-device` answers the broker's request file and publishes
# the inventory the broker reads.
#
# THE PRESENCE OF THIS BINARY IS WHAT ARMS THE BACKEND. `audio_router.serve()`
# checks for it at startup and keeps `switch_only_authorization` when it is
# absent, so a panel without it refuses `pair` and `connect` AT THE GATE rather
# than writing a request file nothing will ever read -- which would leave the
# broker's journal sticky-pending on a mutation that never had anywhere to go.
# Installing it is therefore the decision, and it is made here rather than in
# the broker's own unit because this is the script that knows bluetoothctl and
# the dbus bindings are actually present.
#
# NOT `wall-bluetooth-apply`: that name is already the at-rest policy applier a
# few lines above, and two different things under one name in /usr/local/sbin is
# how the wrong `systemctl status` answers a question nobody asked.
install -m 0755 "$payload/wall-bluetooth-device.py" /usr/local/sbin/wall-bluetooth-device
install -m 0644 "$payload/wall-bluetooth-device-apply.path"     /etc/systemd/system/wall-bluetooth-device-apply.path
install -m 0644 "$payload/wall-bluetooth-device-apply.service"  /etc/systemd/system/wall-bluetooth-device-apply.service
install -m 0644 "$payload/wall-bluetooth-observe.service"       /etc/systemd/system/wall-bluetooth-observe.service
install -m 0644 "$payload/wall-bluetooth-observe.timer"         /etc/systemd/system/wall-bluetooth-observe.timer
install -m 0644 "$payload/wall-bus-bluetooth.service"           /etc/systemd/system/wall-bus-bluetooth.service

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
# The request watcher and the inventory timer follow WALL_BLUETOOTH_ENABLED, not
# the audio broker: with Bluetooth off there is no adapter policy to manage and
# a timer polling bluetoothctl every ten seconds for nothing is pure wakeups.
# The apply PATH unit is deliberately enabled even so -- it only ever fires when
# the broker writes a request, and the broker cannot write one unless the
# backend is armed.
if [ "$enabled" = true ]; then
    systemctl enable --now wall-bluetooth-device-apply.path wall-bluetooth-observe.timer
    systemctl enable bluealsa.service bluealsa-aplay.service
    # Restart rather than start: a re-run must pick up a changed override.
    systemctl restart bluealsa.service bluealsa-aplay.service || {
        echo 'Bluetooth: the A2DP sink did not start; pairing would succeed and play nothing' >&2
        exit 1; }
else
    systemctl disable --now wall-bluetooth-device-apply.path wall-bluetooth-observe.timer >/dev/null 2>&1 || true
    systemctl stop wall-bus-bluetooth.service >/dev/null 2>&1 || true
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
# Publish once, so the broker has a document to read before the timer's first
# tick. Without this the panel shows "Bluetooth status unavailable" for the
# first twenty seconds after every firstboot, which is indistinguishable from
# the failure this whole document is supposed to make visible.
if [ "$enabled" = true ]; then
    /usr/local/sbin/wall-bluetooth-device publish || \
        echo '[wall-bluetooth] the first inventory publish failed; the timer will retry' >&2
fi
echo "[wall-bluetooth] Policy installed. Open a pairing window with: sudo wall-bluetooth-pairing open"
echo "[wall-bluetooth] ...or from the panel itself: Bluetooth tab, Make discoverable (WSN-024)."
if [ "$enabled" = true ]; then
    echo "[wall-bluetooth] A2DP sink active. Paired devices play to the ALSA default PCM,"
    echo "[wall-bluetooth] which in trigger mode is what powers the amplifier."
fi
