#!/usr/bin/env bash
# icedrive-gate.sh — do not let the IceDrive client start until the disk its
# sync pairs point at is REALLY mounted.
#
# THE FAILURE THIS EXISTS TO PREVENT. `/srv/library` is an ordinary directory on
# the eMMC that the real disk mounts over. The client autostarts with the XFCE
# session ~90 s after boot, and its pairs are SERVER-SIDE state: it signs in,
# asks the API, and starts a sync thread against an absolute `path_local` the
# moment it has an answer. Measured 2026-08-29:
#
#     the response is: {"count":1,"pairs":[{"id":63605,
#       "path_local":"/srv/library/permtest", …}]}
#     run sync threads (1); starting sync thread for syncId 63605
#     start watch on "/srv/library/permtest" id: 63605
#
# Nothing in that sequence asks whether the disk is there. A two-way pair that
# scans an unmounted mountpoint sees an empty tree, and "empty" and "deleted"
# are the same observation.
#
# WHAT ALREADY PROTECTS THIS, AND WHY IT IS NOT ENOUGH. provision-mounts leaves
# the BARE mountpoint `chmod 000`, so with the drive absent the client gets
# EACCES rather than an empty directory — a good failure. But that covers the
# drive being ABSENT. It does nothing about a drive that is present and wrong
# (a stand-in), and nothing about the window between the session starting and a
# slow USB enclosure finishing enumeration. `nofail` in the generated fstab
# means systemd does not wait for these disks and neither does anything else.
#
# WHY A GATE AND NOT A DELAY. A timed delay is a guess about enumeration speed,
# and open-item C27 has these enclosures dropping off the bus repeatedly — six
# disconnects on one port inside two minutes. A drive present at T+60 s can be
# gone at T+120 s. A delay moves the race; a predicate removes it.
#
# WHY NOT `RequiresMountsFor=` ON THE SESSION UNIT. That gates the whole
# graphical layer, so a missing USB disk would also cost the remote desktop —
# the one tool you would want in order to go and look. This gates only IceDrive.
#
# EXIT / BEHAVIOUR CONTRACT:
#   * every path passes  -> exec the client (this process becomes it)
#   * timeout            -> DO NOT start it, log loudly to the journal, exit 1.
#     Not starting is the safe half: no sync at all beats a sync against an
#     empty tree, and the journal says which path was missing.
#   * no paths configured -> exec immediately (the feature is off, not broken)
#
# Usage:  icedrive-gate.sh [mountpoint ...]
#   Paths default to $ICEDRIVE_GATE_PATHS, then to /srv/library.
#   Timeout is $ICEDRIVE_GATE_TIMEOUT seconds (default 600).
#
# NOTE ON WHICH DISK. The pairs live under /srv/library, so the LIBRARY drive is
# what matters — not the backup drive, which IceDrive never touches. Waiting on
# /mnt/backup-drive would block the cloud copy for a fault that has nothing to
# do with it. Add it here only if a pair is ever pointed at it.
set -uo pipefail

APP="${ICEDRIVE_APP:-/opt/icedrive/Icedrive.AppImage}"
TIMEOUT="${ICEDRIVE_GATE_TIMEOUT:-600}"
INTERVAL="${ICEDRIVE_GATE_INTERVAL:-5}"

if [ "$#" -gt 0 ]; then
    PATHS=("$@")
else
    # shellcheck disable=SC2206
    read -r -a PATHS <<< "${ICEDRIVE_GATE_PATHS:-/srv/library}"
fi

# logger AND stderr. The session's stderr goes nowhere anybody reads, and this
# runs unattended at boot with nobody connected, so the journal is the only
# place a refusal can actually be found later.
log() {
    printf '[icedrive-gate] %s\n' "$*" >&2
    command -v logger >/dev/null 2>&1 && logger -t icedrive-gate -- "$*"
    return 0
}

# is_mounted PATH — /proc/self/mountinfo ONLY, deliberately.
#
# No `mountpoint`, no `findmnt`, no guard binary: this decides whether the
# household's cloud sync starts, and it must not be able to answer "no" because
# a tool is missing. mountinfo is part of the kernel and is always there. It is
# also a pure read of a virtual file, so it cannot wake a spun-down disk — the
# same property that lets library-guard run on a 10-minute timer.
#
# Field layout: ID parent major:minor root MOUNTPOINT ...
# Field 5 is the mount point, with spaces/tabs octal-escaped by the kernel.
is_mounted() {
    local want="$1" line target
    while read -r _ _ _ _ target _; do
        [ "$target" = "$want" ] && return 0
    done < /proc/self/mountinfo
    return 1
}

if [ "${#PATHS[@]}" -eq 0 ]; then
    log "no paths configured — starting IceDrive immediately"
    exec "$APP"
fi

log "waiting for: ${PATHS[*]} (up to ${TIMEOUT}s) before starting IceDrive"
waited=0
while :; do
    missing=()
    for p in "${PATHS[@]}"; do
        is_mounted "$p" || missing+=("$p")
    done
    [ "${#missing[@]}" -eq 0 ] && break

    if [ "$waited" -ge "$TIMEOUT" ]; then
        log "REFUSING TO START: still not mounted after ${TIMEOUT}s: ${missing[*]}"
        log "  IceDrive has NOT been started. Its sync pairs point at absolute paths"
        log "  under these mount points, and a two-way pair that scans an unmounted"
        log "  mountpoint sees an empty tree — which is indistinguishable from every"
        log "  file having been deleted."
        log "  Attach the disk, then either re-run this script or log in over RDP."
        exit 1
    fi
    [ "$waited" -eq 0 ] && log "not mounted yet: ${missing[*]} — polling every ${INTERVAL}s"
    sleep "$INTERVAL"
    waited=$(( waited + INTERVAL ))
done

[ "$waited" -gt 0 ] && log "all paths mounted after ${waited}s"

# INFORMATIONAL ONLY, and it must never gate. The guard knows whether the disk
# is the one storage-map names, which a mount check cannot tell; a STAND-IN is a
# legitimate state to run against (it is how the bench is tested) so this is
# logged, not enforced. Note the argument order: `--check` takes an optional
# positional share name and will EAT a following `--library`, so the path has to
# come first.
if [ -x /usr/local/sbin/homehub-library-guard ]; then
    for p in "${PATHS[@]}"; do
        if ! /usr/local/sbin/homehub-library-guard --library "$p" --check >/dev/null 2>&1; then
            log "NOTE: $p is mounted but the guard is unhappy with it — starting anyway"
        fi
    done
fi

log "starting $APP"
exec "$APP"
