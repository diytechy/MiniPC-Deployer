#!/usr/bin/env bash
# Data-drive mounts (Personal open-items A14, 2026-07-30).
#
# Appends the generated fstab lines for the storage-map §1 data drives and
# mounts them. Until this existed NOTHING mounted them: the autoinstall does a
# whole-disk LVM layout of the SYSTEM disk only, so `/srv/library` was a string
# in config files that resolved to an empty directory on the eMMC. Every
# consumer downstream — Samba shares, MEDIA_ROOT, the backup source paths —
# was pointing at nothing.
#
# INPUT  /etc/awow-samba/library-mounts.fstab  (generated from storage-map §1,
#        carried on the USB payload as site/library-mounts.fstab)
#
# IDEMPOTENT: a line whose mountpoint is already in /etc/fstab is left alone.
# Safe to re-run; it never rewrites or reorders existing entries.
#
# NOT FATAL when a drive is absent. `nofail` is in the generated options for
# the same reason: a headless always-on box must boot without its USB drives
# rather than drop to an emergency shell. This script reports loudly instead.
set -euo pipefail

FSTAB_FRAGMENT="/etc/awow-samba/library-mounts.fstab"
while [ $# -gt 0 ]; do
    case "$1" in
        --fragment) FSTAB_FRAGMENT="$2"; shift 2 ;;
        *) echo "usage: provision-mounts.sh [--fragment PATH]" >&2; exit 2 ;;
    esac
done
log() { echo "[provision-mounts] $*"; }

if [ ! -f "$FSTAB_FRAGMENT" ]; then
    log "no fstab fragment at $FSTAB_FRAGMENT — skipping (data drives stay unmounted)."
    exit 0
fi

added=0
while IFS= read -r line; do
    case "$line" in ''|\#*) continue ;; esac
    dev="$(printf '%s' "$line"  | awk '{print $1}')"
    mnt="$(printf '%s' "$line"  | awk '{print $2}')"
    [ -n "$dev" ] && [ -n "$mnt" ] || continue

    # Match on MOUNTPOINT, not the whole line: options may legitimately be
    # edited on the box, and re-adding the same mountpoint would be a
    # duplicate-entry error at mount -a time.
    if awk '!/^[[:space:]]*#/ && NF >= 2 { print $2 }' /etc/fstab | grep -qxF "$mnt"; then
        log "fstab already has an entry for $mnt — left as-is"
    else
        printf '%s\n' "$line" >> /etc/fstab
        log "fstab += $mnt"
        added=$((added + 1))
    fi

    install -d -m 0775 "$mnt"

    if [ ! -e "$dev" ]; then
        log "WARN: $dev is not present — $mnt will stay empty until the drive is attached."
        log "      (nofail keeps this from blocking boot, which is deliberate.)"
        continue
    fi
    if mountpoint -q "$mnt"; then
        log "$mnt already mounted"
    elif mount "$mnt" 2>/dev/null; then
        log "mounted $mnt"
    else
        log "WARN: mount $mnt FAILED. If the drive is NTFS from its Windows life, the"
        log "      kernel may lack a driver for it, or it needs fsck after an unclean"
        log "      Windows removal. See open-items A15 — the filesystem decision is open,"
        log "      and NTFS would also break the per-user Samba ACLs."
    fi
done < "$FSTAB_FRAGMENT"

[ "$added" -gt 0 ] && systemctl daemon-reload || true
log "done: $added fstab entry/entries added."
exit 0
