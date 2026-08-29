#!/usr/bin/env bash
# provision-backup-principal.sh — name the FileBackup container's uid on the host.
#
# Q-FB5 (the Owner, 2026-08-23): the backup drive mounts
# uid=65532,gid=65532,umask=0027 so the FileBackup container — which ships as
# 65532:65532, the conventional "nonroot" uid — can write it WITHOUT running as
# root. NTFS/exFAT ownership is synthesized from the mount options, and those
# options are NUMERIC (the kernel resolves no names), so the WRITE half of this
# script is not load-bearing: the mount and the container both work if it never
# ran. That half exists for legibility and for the library-backup wrapper —
# `ls -l /mnt/backup-drive` shows `filebackup` instead of a bare number, and the
# wrapper can assert against a name that means something.
#
# THAT CHANGED ON 2026-08-29 AND THE OLD HEADER IS NO LONGER TRUE. The mask went
# 0077 -> 0027 (the Owner) so the RDP operator account can READ the drive in its
# session. A numeric mount option cannot express "and this other account too" —
# that takes GROUP MEMBERSHIP, which is made at the bottom of this file. So this
# script IS now load-bearing, for read, and skipping it leaves a drive the
# operator cannot open. HomeHub's TC-H-L07 asserts the outcome.
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

# ── the OPERATOR account reads the drive (2026-08-29, the Owner) ─────────────
# READ ONLY, and deliberately: the Owner asked to see both drives in the RDP
# session, with the backup drive read-only there. `umask=0027` gives the group
# r-x on directories and r-- on files, so membership in this group is exactly
# "may look, may not change" — the write bit is still the OWNER's alone, which
# is Q-FB5 untouched.
#
# WHY IT CANNOT BE A MOUNT OPTION. uid=/gid= name ONE account and ONE group. A
# second principal with different rights can only be expressed by putting it in
# the group, which lives in /etc/group and therefore in NO generated artifact —
# the same class of state as the household membership in provision-samba-users.sh,
# and the same reason it must be provisioned rather than done by hand: a reimage
# discards it in silence and the operator quietly loses the drive.
#
# WARNING, not fatal. The container's write access does not depend on this, and
# the backup is the thing this box exists to do.
OPERATOR_USER="${REMOTE_UI_USER:-hub}"
if ! id -u "$OPERATOR_USER" >/dev/null 2>&1; then
    log "operator account '$OPERATOR_USER' does not exist — skipping its $FB_NAME membership"
elif id -nG "$OPERATOR_USER" 2>/dev/null | tr ' ' '\n' | grep -qxF "$FB_NAME"; then
    log "operator '$OPERATOR_USER' is already in $FB_NAME"
elif usermod -aG "$FB_NAME" "$OPERATOR_USER"; then
    log "operator '$OPERATOR_USER' added to $FB_NAME (read-only sight of the backup drive)"
else
    log "WARN: could not add '$OPERATOR_USER' to $FB_NAME — the backup drive will be invisible in the RDP session (TC-H-L07 will fail)"
fi

exit 0
