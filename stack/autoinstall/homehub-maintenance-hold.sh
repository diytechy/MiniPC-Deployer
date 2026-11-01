#!/bin/sh
# homehub-maintenance-hold — automatic patching waits while the disk is in surgery.
#
# WHAT HAPPENED, measured on the hub 2026-11-01:
#
#   02:30:16  unattended-upgrade: Upgrade: libbrotli1   <- the ONLY package
#   02:30:21  needrestart stops ext4-gparted, ext4-gparted-xvfb, ext4-gparted-wm
#   02:30:27  gparted: Terminated   (24h54m and 2.73 TB into a Resize/Move)
#
# None of those units was upgraded. needrestart restarted them because they had
# the old library MAPPED, exactly as it did to xrdp in C59 - and a transient
# `systemd-run` unit is just another service to it. GParted cannot resume a
# move, so one routine library patch threw away a day of an 8 TB relocation and
# left the operator to prove the data survived.
#
# THE FIX IS A QUESTION BOTH CALLERS ASK FIRST, not a schedule. Moving the
# window (firstboot 1d already did: Sun 02:30) cannot help an operation that
# runs for days. So:
#
#   apt-daily-upgrade.service  runs `homehub-maintenance-hold check` as an
#                              ExecCondition. Held = this week's run is SKIPPED,
#                              cleanly, with the reasons in its journal.
#   needrestart (manual apt)   evaluates 80-homehub-maintenance-hold.conf, which
#                              asks the same question and, while held, denies
#                              every service and container restart. Packages
#                              still install; the restart waits for a reboot.
#
# WHAT HOLDS THE BOX (any one is enough, and every one found is printed):
#
#   1. A disk-surgery tool is running. Matched on the exact process name, so
#      `gparted` the shell wrapper and an editor with "e2fsck" in a filename do
#      not count; `gpartedbin` is the process that actually moves blocks.
#   2. A protected unit is active: the storage-transition units (ext4-*, which
#      is how HomeHub's transition tooling names every GParted session and
#      logged job) and the two backups - a backup killed mid-archive leaves a
#      stale Temp that refuses every later run (C61).
#   3. An operator hold file exists in /run/homehub-maintenance-hold/. For
#      anything the first two cannot see; `hold NAME REASON` writes one,
#      `release NAME` removes it. /run on purpose: a reboot ends every
#      operation a hold could protect, so a forgotten hold cannot block
#      patching forever.
#
# EXIT STATUS IS THE INTERFACE, because ExecCondition reads nothing else:
#
#   0    clear            the upgrade runs
#   1    held             systemd skips the upgrade (1-254 mean "skip")
#   255  cannot tell      systemd FAILS the unit, so it shows red
#
# A probe that cannot answer is 255, never 0 (the upgrade would run blind) and
# never 1 (the box would quietly stop patching, week after week, and nobody
# would look). A usage error is 255 for the same reason: a typo in the drop-in
# must turn the unit red, not skip patching forever.
#
# THE TRADE-OFF, STATED SO IT IS A CHOICE: a held week's patch waits for the
# next Sunday. After a long operation, `sudo systemctl start
# apt-daily-upgrade.service` runs it at once.
set -u

HOLD_DIR=${HOMEHUB_MAINTENANCE_HOLD_DIR:-/run/homehub-maintenance-hold}

# Exact process names (pgrep -x anchors the pattern at both ends). Long-running
# block movers and checkers only; partclone and fsck are families.
DISK_TOOLS='gpartedbin|ntfsresize|ntfsclone|resize2fs|e2fsck|e2image|mke2fs|badblocks|ddrescue|partclone\..+|fsck\..+'

# Units whose interruption loses work. Glob patterns, as systemctl takes them.
PROTECTED_UNITS='ext4-* homehub-library-backup.service homehub-backup.service'

usage() {
    echo "usage: homehub-maintenance-hold check | hold NAME [REASON...] | release NAME" >&2
    exit 255
}

probe_failed() {
    echo "maintenance hold: cannot tell whether it is safe: $*" >&2
    exit 255
}

valid_name() {
    # One path component, no leading dot or dash: a hold name can never reach
    # outside HOLD_DIR or be mistaken for an option.
    case $1 in
        ''|.*|-*|*/*) return 1 ;;
    esac
    case $1 in
        *[!A-Za-z0-9._-]*) return 1 ;;
    esac
    return 0
}

# Each finder prints one "held: ..." line per reason and fails only when its
# probe could not answer. The caller counts lines; it never infers "clear" from
# a probe that errored.
running_disk_tools() {
    found=$(pgrep -a -x "$DISK_TOOLS")
    case $? in
        0) printf '%s\n' "$found" | sed 's/^/held: disk tool running: /' ;;
        1) ;;  # pgrep's "nothing matched"
        *) probe_failed "pgrep exited with an error" ;;
    esac
}

active_protected_units() {
    # set -f: the patterns are for systemctl to expand, not the shell.
    set -f
    # shellcheck disable=SC2086
    found=$(systemctl list-units --no-legend --plain \
                --state=active,activating,deactivating,reloading $PROTECTED_UNITS) \
        || probe_failed "systemctl list-units failed"
    set +f
    [ -n "$found" ] || return 0
    printf '%s\n' "$found" | awk '{print "held: unit " $1 " is " $3 "/" $4}'
}

operator_holds() {
    [ -d "$HOLD_DIR" ] || return 0
    for file in "$HOLD_DIR"/*; do
        [ -f "$file" ] || continue
        reason=$(head -n 1 "$file" 2>/dev/null)
        echo "held: operator hold ${file##*/}: ${reason:-no reason given}"
    done
}

check() {
    # Spooled through variables, not a pipe into a loop: a loop on the far side
    # of a pipe runs in a subshell, and probe_failed's exit would end only that.
    tools=$(running_disk_tools) || exit 255
    units=$(active_protected_units) || exit 255
    holds=$(operator_holds) || exit 255
    reasons=$(printf '%s\n%s\n%s\n' "$tools" "$units" "$holds" | sed '/^$/d')
    if [ -z "$reasons" ]; then
        echo "maintenance hold: clear"
        return 0
    fi
    printf '%s\n' "$reasons"
    echo "maintenance hold: HELD - automatic upgrades and service restarts wait"
    return 1
}

hold() {
    [ $# -ge 1 ] || usage
    name=$1
    shift
    valid_name "$name" || { echo "maintenance hold: '$name' is not a valid hold name (letters, digits, . _ -)" >&2; exit 2; }
    reason=${*:-held by ${SUDO_USER:-${USER:-unknown}} at $(date '+%Y-%m-%d %H:%M %Z')}
    mkdir -p "$HOLD_DIR" || exit 2
    printf '%s\n' "$reason" > "$HOLD_DIR/$name" || exit 2
    echo "maintenance hold: $name set ($reason). Release with: homehub-maintenance-hold release $name"
}

release() {
    [ $# -eq 1 ] || usage
    valid_name "$1" || { echo "maintenance hold: '$1' is not a valid hold name" >&2; exit 2; }
    [ -f "$HOLD_DIR/$1" ] || { echo "maintenance hold: no hold named '$1' (see: homehub-maintenance-hold check)" >&2; exit 1; }
    rm -f "$HOLD_DIR/$1" || exit 2
    echo "maintenance hold: $1 released"
}

case ${1:-} in
    check) [ $# -eq 1 ] || usage; check ;;
    hold) shift; hold "$@" ;;
    release) shift; release "$@" ;;
    *) usage ;;
esac
