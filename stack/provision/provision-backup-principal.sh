#!/usr/bin/env bash
# provision-backup-principal.sh — name the FileBackup container's uid on the host.
#
# Q-FB5 (the Owner, 2026-08-23): the backup drive mounts
# uid=65532,gid=65532,umask=0077 so the FileBackup container — which ships as
# 65532:65532, the conventional "nonroot" uid — can write it WITHOUT running as
# root. NTFS/exFAT ownership is synthesized from the mount options, and those
# options are NUMERIC (the kernel resolves no names), so NOTHING here is
# load-bearing for access: the mount and the container both work if this
# script never ran. It exists for legibility and for the library-backup
# wrapper — `ls -l /mnt/backup-drive` shows `filebackup` instead of a bare
# number, and the wrapper can assert against a name that means something.
#
# What IS load-bearing is the refusal below: if uid/gid 65532 already belongs
# to some other account, that account silently owns every archive of the
# private trees. That is a real finding, not something to shrug past.
#
# NAME CHOICE: `filebackup` collides with no user or group Ubuntu ships (the
# `operator` lesson — autoinstall/user-data useradd exit 9). uid/gid 65532 sit
# just below `nobody`/`nogroup` (65534) and are unassigned on Ubuntu 24.04.
set -u

FB_UID=65532
FB_GID=65532
FB_NAME=filebackup

log() { printf '[provision-backup-principal] %s\n' "$*"; }

# Group first: useradd pins its primary gid to it.
if getent group "$FB_NAME" >/dev/null 2>&1; then
    have="$(getent group "$FB_NAME" | cut -d: -f3)"
    if [ "$have" != "$FB_GID" ]; then
        log "FATAL: group $FB_NAME exists with gid $have, expected $FB_GID — refusing to guess which is right."
        exit 1
    fi
    log "group $FB_NAME (gid $FB_GID) exists"
elif getent group "$FB_GID" >/dev/null 2>&1; then
    log "FATAL: gid $FB_GID is already '$(getent group "$FB_GID" | cut -d: -f1)' — the fstab grants that group the backup drive."
    exit 1
elif groupadd -g "$FB_GID" "$FB_NAME"; then
    log "group $FB_NAME created (gid $FB_GID)"
else
    log "FATAL: groupadd failed for $FB_NAME"
    exit 1
fi

if id -u "$FB_NAME" >/dev/null 2>&1; then
    have="$(id -u "$FB_NAME")"
    if [ "$have" != "$FB_UID" ]; then
        log "FATAL: user $FB_NAME exists with uid $have, expected $FB_UID — refusing to guess which is right."
        exit 1
    fi
    log "user $FB_NAME (uid $FB_UID) exists"
elif getent passwd "$FB_UID" >/dev/null 2>&1; then
    log "FATAL: uid $FB_UID is already '$(getent passwd "$FB_UID" | cut -d: -f1)' — that account would own the backup drive."
    exit 1
elif useradd -M -N -u "$FB_UID" -g "$FB_GID" -s /usr/sbin/nologin "$FB_NAME"; then
    # -M no home, -N no per-user group (the primary gid is pinned above),
    # nologin shell: this identity runs nothing on the host — the container
    # carries the uid. Never an SSH surface.
    log "user $FB_NAME created (uid $FB_UID, no home, nologin)"
else
    log "FATAL: useradd failed for $FB_NAME"
    exit 1
fi

exit 0
