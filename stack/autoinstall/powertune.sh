#!/bin/bash
# Low-power tuning for the AWOW box — runs once per boot via powertune.service.
# (Peter direction 2026-07-04: this is a low-power machine; squeeze the idle
# watts the OS can control.)
#
# `powertop --auto-tune` flips every tunable to its powersave setting (deep
# C-states, SATA link power management, PCIe ASPM, USB autosuspend, ...).
# USB autosuspend is a known footgun for USB MASS-STORAGE enclosures — some
# drop off the bus mid-I/O — so after auto-tune we re-pin power/control to
# 'on' (= no autosuspend) for any USB device exposing a mass-storage
# interface (bInterfaceClass 08). The backup drives' electricity policy is
# owned by the backup service's hdparm standby instead: platter SPIN-DOWN is
# where the real watts are; bus autosuspend saves ~nothing extra on a
# spun-down drive and risks the enclosure. Limitation: guards devices present
# at boot; drives hot-plugged later keep the kernel default (also 'on').
set -u
command -v powertop >/dev/null 2>&1 || exit 0
powertop --auto-tune >/dev/null 2>&1 || true
shopt -s nullglob
for dev in /sys/bus/usb/devices/*/; do
    for cls in "$dev"*/bInterfaceClass; do
        [ -r "$cls" ] || continue
        if [ "$(cat "$cls")" = "08" ]; then
            [ -w "${dev}power/control" ] && echo on > "${dev}power/control"
            break
        fi
    done
done
exit 0
