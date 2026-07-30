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
rc=0        # was never initialised and never read — every failure below was
            # reported as success and firstboot's `|| log WARN` could not fire.
while IFS= read -r line || [ -n "$line" ]; do
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

    # BELT AND BRACES: the BARE mountpoint (the directory on the eMMC that the
    # real disk mounts over) is chmod 000. While the drive IS mounted this is
    # invisible — the mount's own uid/gid/umask apply, and ntfs3 ignores the
    # underlying inode entirely. The moment the drive is absent, that empty
    # directory becomes unreadable to everything, so nothing can quietly serve
    # it or write into it and fill the system disk.
    #
    # This is deliberately independent of the Samba preexec guard: config can
    # drift, a fragment can be regenerated wrong, someone can add a share by
    # hand. A 000 directory needs no configuration to be correct.
    # NEVER touch a MOUNTED filesystem. firstboot runs on EVERY boot (its unit
    # has no ConditionPathExists and the script never reads its own marker), so
    # on boot 2+ the drive is already mounted here — and `install -d` chmods an
    # existing directory, which would have tried to chmod the root of the ntfs3
    # volume, and on ext4 would have succeeded and dropped the group-write bit
    # that household writes depend on. (Review finding, 2026-07-30.)
    if [ ! -e "$mnt" ]; then
        install -d -m 000 "$mnt"
    elif ! mountpoint -q "$mnt"; then
        chmod 000 "$mnt" 2>/dev/null || true
    fi

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
        log "WARN: mount $mnt FAILED."
        log "      If this drive is NTFS and Windows left it dirty (Fast Startup, or an"
        log "      unclean removal), ntfs3 refuses it by design. Fix it by attaching the"
        log "      drive to Windows, disabling Fast Startup, and doing a clean eject —"
        log "      or run chkdsk on it there. Linux has no trustworthy NTFS repair tool."
        rc=1
        continue
    fi

    # ── never-silent-green: a READ-ONLY mount is the dangerous success ───────
    # ntfs3 silently falls back to read-only when the NTFS dirty bit is set
    # (hibernation, Fast Startup, an unclean unplug). Everything then LOOKS
    # mounted: Samba serves the shares, the library is browsable, reads work —
    # and every write and every backup run fails, or worse, is skipped quietly.
    # This must be loud.
    opts="$(findmnt -no OPTIONS --target "$mnt" 2>/dev/null || echo '')"
    case ",$opts," in
        *,ro,*)
            log "FATAL: $mnt mounted READ-ONLY."
            log "       Reads and share browsing will look completely normal while every"
            log "       write silently fails. For NTFS this almost always means the dirty"
            log "       bit is set — clear it from Windows (chkdsk, Fast Startup OFF,"
            log "       clean eject). Not continuing as if this were healthy."
            rc=1
            continue ;;
    esac

    # ── NTFS needs ownership options or Samba cannot write ──────────────────
    # ntfs3 has no POSIX ownership: every file reports the uid/gid fixed at
    # MOUNT time, defaulting to root:root with a restrictive umask. Samba
    # writes as the connecting user, so without uid/gid/umask here the private
    # trees are read-only in practice no matter what the share ACLs say.
    fstype="$(findmnt -no FSTYPE --target "$mnt" 2>/dev/null || echo '')"
    case "$fstype" in
        ntfs|ntfs3|fuseblk)
            log "note: $mnt is $fstype"
            case "$opts" in
                *uid=*) : ;;
                *)
                    log "WARN: $mnt is NTFS but mounted without uid=/gid=. ntfs3 synthesizes"
                    log "      ownership from the MOUNT options, so every file is root-owned and"
                    log "      Samba writes from household accounts will FAIL regardless of the"
                    log "      share ACLs. Expected options for the library:"
                    log "        uid=0,gid=3000,umask=0002    (gid 3000 = the household group)"
                    log "      Re-generate the fstab fragment (Generate-FromStorageMap.ps1) rather"
                    log "      than hand-editing, then: mount -o remount $mnt   (open-items A15)"
                    rc=1 ;;
            esac ;;
    esac
done < "$FSTAB_FRAGMENT"

[ "$added" -gt 0 ] && systemctl daemon-reload || true
log "done: $added fstab entry/entries added."
if [ "$rc" -ne 0 ]; then
    log "FAILURES ABOVE — at least one drive is missing, read-only, or wrongly mounted."
fi
exit "$rc"
