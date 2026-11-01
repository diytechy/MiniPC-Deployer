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
#                              every restart needrestart would make. Packages
#                              still install; the restart waits for a reboot.
#
# WHAT IT DOES NOT COVER, so nobody relies on it: a package's own maintainer
# script still restarts that package's daemon (upgrading docker.io restarts
# dockerd whatever needrestart says). The scheduled path is unaffected - a held
# week installs nothing at all - but a MANUAL `apt upgrade` during a hold, or an
# unattended run that passed the check and is still installing when a protected
# operation starts, can still restart the daemon being upgraded. Hold off manual
# upgrades while an operation runs; /usr/sbin/policy-rc.d is the lever if that
# ever needs enforcing.
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
# A HOLD THAT OUTLIVES ITS LIMIT IS ALSO 255. Holds normally end on their own,
# but an idle GParted window left open, or a backup unit wedged in
# "deactivating", holds for as long as nobody notices - and a skipped unit is
# invisible to monitoring, which reports FAILED units. So once the last
# unattended upgrade (apt's upgrade-stamp) is older than MAX_DAYS, a held check
# still installs nothing but exits 255, and the unit turns red every week until
# someone finishes or releases the hold. Waiting must never become silently
# not patching (SN-052).
#
# WHY 10 AND NOT 14. The run is weekly and the age is whole days, so on the
# SECOND held Sunday the stamp is 13 days and some hours old - and "-gt 14"
# would not fire until the THIRD, about 21 days in. 10 lets the first held
# Sunday (6 days) skip quietly and turns the second one red. The stamp is
# touched by unattended runs only; a box patched by hand still counts as
# overdue, and starting the unit as the message says refreshes it.
#
# NO STAMP IS NOT "NO LIMIT". A freshly imaged box held before its first
# unattended run never gets a stamp, and would skip every week forever. So the
# first held check that finds no stamp records a baseline under /var/lib (it
# must survive the reboots a /run hold does not), and the age is counted from
# that instead - at most one week later than a real stamp would fire, because
# the baseline is the first HELD run, not the last upgrade. A real stamp always
# wins, and its arrival deletes the baseline.
#
# THE TRADE-OFF, STATED SO IT IS A CHOICE: a held week's patch waits for the
# next Sunday. After a long operation, `sudo systemctl start
# apt-daily-upgrade.service` runs it at once.
set -u

HOLD_DIR=${HOMEHUB_MAINTENANCE_HOLD_DIR:-/run/homehub-maintenance-hold}
UPGRADE_STAMP=${HOMEHUB_MAINTENANCE_HOLD_STAMP:-/var/lib/apt/periodic/upgrade-stamp}
MAX_DAYS=${HOMEHUB_MAINTENANCE_HOLD_MAX_DAYS:-10}
STATE_DIR=${HOMEHUB_MAINTENANCE_HOLD_STATE_DIR:-/var/lib/homehub-maintenance-hold}
NO_STAMP_BASELINE=$STATE_DIR/never-upgraded-since

# Exact process names (pgrep -x anchors the pattern at both ends), so the name
# a tool was RUN as is what counts: mke2fs is usually run as mkfs.ext4. Long
# block movers, formatters and checkers only; mkfs, partclone and fsck are
# families. dd is here because it is how a raw disk image is written.
DISK_TOOLS='gpartedbin|ntfsresize|ntfsclone|mkntfs|resize2fs|e2fsck|e2image|e4defrag|mke2fs|badblocks|ddrescue|dd|pvmove|xfs_repair|partclone\..+|fsck\..+|mkfs\..+'

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
    if age=$(days_since_last_upgrade) && [ "$age" -gt "$MAX_DAYS" ]; then
        echo "maintenance hold: HELD TOO LONG - the last unattended upgrade was $age days ago (limit $MAX_DAYS)." >&2
        echo "  Nothing was installed. Finish or release the hold, then: systemctl start apt-daily-upgrade.service" >&2
        exit 255
    fi
    echo "maintenance hold: HELD - automatic upgrades and service restarts wait"
    return 1
}

days_since_last_upgrade() {
    # Called only while held. Prints whole days since apt's upgrade-stamp, or
    # since the no-stamp baseline (created here on first need); fails only when
    # neither can be read or made - a non-root `check` run by hand, which then
    # reports an ordinary hold rather than inventing an age.
    if [ -f "$UPGRADE_STAMP" ]; then
        rm -f "$NO_STAMP_BASELINE" 2>/dev/null
        reference=$UPGRADE_STAMP
    else
        if [ ! -f "$NO_STAMP_BASELINE" ]; then
            mkdir -p "$STATE_DIR" 2>/dev/null && : > "$NO_STAMP_BASELINE" 2>/dev/null || return 1
        fi
        reference=$NO_STAMP_BASELINE
    fi
    stamped=$(stat -c %Y "$reference") || return 1
    echo $(( ($(date +%s) - stamped) / 86400 ))
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
