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
# INPUT  /etc/homehub-samba/library-mounts.fstab  (generated from storage-map §1,
#        carried on the USB payload as site/library-mounts.fstab)
#
# IDEMPOTENT: a line whose mountpoint is already in /etc/fstab is left alone.
# Safe to re-run; it never rewrites or reorders existing entries.
#
# NOT FATAL when a drive is absent. `nofail` is in the generated options for
# the same reason: a headless always-on box must boot without its USB drives
# rather than drop to an emergency shell. This script reports loudly instead.
set -euo pipefail

FSTAB_FRAGMENT="/etc/homehub-samba/library-mounts.fstab"
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

# resolve_mount_spec SPEC — print the device an fstab first-field names, or
# nothing. Understands the forms fstab actually uses; a bare path is passed
# through so nothing that worked before stops working.
#
# blkid rather than the /dev/disk/by-label symlink, deliberately: blkid PROBES
# the devices, so it answers correctly even when udev has not finished creating
# the symlink — which is the exact window firstboot runs in.
resolve_mount_spec() {
    case "$1" in
        LABEL=*)      blkid -L "${1#LABEL=}" 2>/dev/null ;;
        UUID=*)       blkid -U "${1#UUID=}" 2>/dev/null ;;
        PARTLABEL=*)  readlink -e "/dev/disk/by-partlabel/${1#PARTLABEL=}" 2>/dev/null ;;
        PARTUUID=*)   readlink -e "/dev/disk/by-partuuid/${1#PARTUUID=}" 2>/dev/null ;;
        /*)           [ -e "$1" ] && printf '%s' "$1" ;;
        *)            : ;;   # none/tmpfs/swap and friends — not a device
    esac
    # ALWAYS 0: this helper's contract is "print the device, or print nothing".
    # Absence is reported by EMPTY OUTPUT, never by exit status — but `blkid`
    # and `readlink -e` both exit non-zero when they find nothing, and that
    # status was leaking out as the function's own. Under this script's
    # `set -euo pipefail` that made every caller's plain assignment
    # (`found="$(resolve_mount_spec …)"`) a script-killer on the absent-drive
    # path — the same defect as the `|| true` below, one level up.
    return 0
}

# wait_for_mount_spec SPEC SECONDS — the same, with a bounded wait.
#
# A SINGLE udevadm settle FIRST, then poll. settle alone is not enough: it
# returns when the CURRENT queue drains, and a USB disk that has not been
# enumerated yet has nothing in the queue to wait for. Polling alone is slower
# than it needs to be on the common path. Together they cover both.
wait_for_mount_spec() {
    spec="$1"; secs="${2:-20}"; i=0; found=''
    found="$(resolve_mount_spec "$spec")"
    if [ -n "$found" ]; then printf '%s' "$found"; return 0; fi
    command -v udevadm >/dev/null 2>&1 && udevadm settle --timeout=5 >/dev/null 2>&1
    while [ "$i" -lt "$secs" ]; do
        found="$(resolve_mount_spec "$spec")"
        if [ -n "$found" ]; then printf '%s' "$found"; return 0; fi
        sleep 1
        i=$((i + 1))
    done
    return 1
}

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

    # ── IS THE DRIVE ACTUALLY THERE? ────────────────────────────────────────
    #
    # THIS WAS `[ ! -e "$dev" ]`, AND IT COULD NEVER BE TRUE FOR THE FSTAB THIS
    # PROJECT GENERATES. $dev is the first field of the fstab line, which for
    # every data drive is `LABEL=Library` — a MOUNT SPEC, not a path. So the
    # test asked whether a file literally named "LABEL=Library" existed, which
    # it never does, and every drive was reported absent:
    #
    #     [provision-mounts] WARN: LABEL=Library is not present — /srv/library
    #                              will stay empty until the drive is attached.
    #
    # while lsblk showed it attached, labelled and healthy, and `mount
    # /srv/library` succeeded instantly by hand. The mount below was therefore
    # never reached, and provision-samba — which runs seconds later in the same
    # firstboot and REFUSES to export an unmounted path, correctly — killed
    # firstboot with a FATAL. Every first boot, on every box, since the fstab
    # moved to LABEL= for A23.
    #
    # It looked like it worked because it eventually does: systemd's
    # fstab-generator mounts these on the NEXT boot regardless. So the drives are
    # up by the time anyone logs in, and only firstboot — the one pass that
    # provisions Samba — ever saw them missing.
    #
    # Resolve the spec properly, and WAIT, because the wait is not padding. The
    # fstab entries already carry x-systemd.device-timeout=15s, which is the
    # same admission in systemd's language: these are USB drives on the real hub
    # and they enumerate slowly. firstboot runs early enough that udev may still
    # be settling.
    # `|| true` IS LOAD-BEARING, and its absence was a silent, total defect
    # (found on the first real bench install, 2026-08-24). This script runs
    # under `set -euo pipefail`, and wait_for_mount_spec RETURNS 1 when the
    # drive never appears — which, in a command-substitution assignment, kills
    # the script on the spot. So the entire absent-drive path below (the WARN,
    # the "nofail keeps this from blocking boot" note, the `continue` to the
    # NEXT fragment line) was unreachable, and the loop died inside its first
    # iteration.
    #
    # THE COST WAS THE SECOND DRIVE. With no drives attached — a bench install,
    # exactly what the ISO is for — the library line was appended to /etc/fstab
    # and the backup-drive line NEVER WAS. Attaching both drives later then
    # mounts the library and silently leaves /mnt/backup-drive absent, with no
    # fstab entry to explain why and nothing having said a word.
    #
    # THE LAB COULD NOT SEE THIS: its VHDX stand-ins are always attached and
    # labelled, so the wait always succeeded and both lines were always written.
    # Absent-drive is a bench-install-only path.
    resolved="$(wait_for_mount_spec "$dev" 20 || true)"
    if [ -z "$resolved" ]; then
        log "WARN: $dev did not appear within 20s — $mnt will stay empty until the drive is attached."
        log "      (nofail keeps this from blocking boot, which is deliberate.)"
        continue
    fi
    log "$dev resolved to $resolved"
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
