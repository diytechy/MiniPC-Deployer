#!/bin/bash
# Wake-source enablement for the wall panel — runs ONCE PER BOOT via
# wall-wakeprep.service, because none of this persists across a reboot.
#
# D-W4's two wake sources, in priority order:
#   1. THE RTC ALARM IS PRIMARY. It is armed by wall-sleep.sh at suspend time and
#      needs nothing enabled here — it is the reason a failed USB wake cannot
#      leave the panel dark all day. This script only VERIFIES the interface
#      exists, loudly, because if it does not then SLEEP_MODE=suspend has no
#      guaranteed way back and SLEEP_MODE=backlight is the honest choice.
#   2. The USB mouse is the manual/early wake, and it is REQUIRED rather than a
#      nice-to-have: the internal touchscreen is an I2C-HID device and those
#      generally cannot wake a machine from S3 (they work fine from
#      backlight-off). Without the mouse, tapping the panel during the window
#      does nothing at all. It needs TWO things enabled, neither persistent:
#        a) the USB controller as an ACPI wake source (/proc/acpi/wakeup, XHC),
#        b) the device itself (/sys/bus/usb/devices/*/power/wakeup).
#
# Idempotent, never fatal: a panel that cannot arm a wake source must still come
# up and show the wall. Every failure is a loud journal line, not an exit code —
# `journalctl -u wall-wakeprep` is the place to look.
set -u
log() { echo "[wall-wakeprep] $*"; }

# ── 1. the RTC alarm interface (primary wake) ────────────────────────────────
if [ -w /sys/class/rtc/rtc0/wakealarm ]; then
    log "RTC wakealarm present (/sys/class/rtc/rtc0/wakealarm) — primary wake available"
else
    log "WARNING: no writable /sys/class/rtc/rtc0/wakealarm. SLEEP_MODE=suspend would"
    log "WARNING: have NO guaranteed wake — only the USB mouse. Consider"
    log "WARNING: SLEEP_MODE=backlight in /etc/wall-panel/wall.env."
fi

# ── 2a. the USB controller as an ACPI wake source ────────────────────────────
# The device name is XHC on this hardware generation (XHCI); older/other firmware
# uses EHC1/EHC2/XHC1. Enable every USB-ish one that is currently disabled — the
# file is a TOGGLE, so writing the name flips it and we must check first.
if [ -w /proc/acpi/wakeup ]; then
    enabled_any=0
    while read -r dev _ status _; do
        case "$dev" in
            XHC|XHC1|EHC1|EHC2|EHCI|XHCI)
                case "$status" in
                    *disabled*)
                        if echo "$dev" > /proc/acpi/wakeup 2>/dev/null; then
                            log "ACPI wake source enabled: $dev"
                            enabled_any=1
                        else
                            log "WARNING: could not enable ACPI wake source $dev"
                        fi
                        ;;
                    *) log "ACPI wake source already enabled: $dev"; enabled_any=1 ;;
                esac
                ;;
        esac
    done < /proc/acpi/wakeup
    [ "$enabled_any" = "1" ] || log "WARNING: no USB ACPI wake source (XHC/EHC*) found — the mouse cannot wake it"
else
    log "WARNING: /proc/acpi/wakeup not writable — cannot enable USB as a wake source"
fi

# ── 2b. the USB devices themselves ───────────────────────────────────────────
# Enable wakeup on every USB device that has the control file. That is broader
# than "the mouse", deliberately: which port the mouse is in is not knowable from
# here, and a wall-mounted panel that ignores the one wake button because the
# mouse got moved to the other port is a service call.
shopt -s nullglob
count=0
for w in /sys/bus/usb/devices/*/power/wakeup; do
    [ -w "$w" ] || continue
    if echo enabled > "$w" 2>/dev/null; then count=$((count + 1)); fi
done
log "USB device wakeup enabled on $count device(s)"

# NOTE (quirk 6, thermals): sustained video decode in a sealed wall mount is the
# load case that cooks this machine. Confirming VA-API is actually ENGAGED rather
# than assumed is a burn-in check (`vainfo`, then watch the thermal zones under
# playback) — see WALL-BURN-IN.md §6. Nothing to enable here.
exit 0
