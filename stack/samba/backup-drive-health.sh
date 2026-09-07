#!/bin/sh
# backup-drive-health.sh — resolve the backup drive's mountpoint from config and
# hand it to the guard. Run by homehub-backup-drive-health.service on a timer.
#
# WHY THIS IS A SCRIPT AND NOT AN INLINE ExecStart (2026-09-07). It used to be
# a /bin/sh -c one-liner in the unit, and the quoting was the kind that is right
# until someone edits it: systemd un-escapes backslashes before sh ever sees the
# line, so every `\` in the sed had to be written `\`, and a wrong count fails
# at RUNTIME rather than at install. Out here it is ordinary shell, and it can
# be run by hand to see what it resolves.
#
# WHICH PATH THIS CHECKS, AND WHY IT IS NOT BACKUP_TARGET. See the unit header
# and backup.env.example's BACKUP_DRIVE_MOUNT block: the flat layout moved
# BACKUP_TARGET onto a FOLDER on the LIBRARY drive, which made this lane both
# permanently red and pointed at the wrong disk.
set -u

CFG="${BACKUP_ENV_FILE:-/etc/homehub-backup/backup.env}"
GUARD="${LIBRARY_GUARD:-/usr/local/sbin/homehub-library-guard}"
FALLBACK_MOUNT=/mnt/backup-drive

# No backup.env = an unprovisioned or sim box. Clean no-op, same posture as
# backup-standby.service — nothing to check is not a failure.
if [ ! -f "$CFG" ]; then
    echo "[backup-drive-health] no $CFG — backup not provisioned, nothing to check"
    exit 0
fi

# Last assignment wins (same as the shell would do), strip a trailing comment,
# then strip one layer of surrounding quotes and any stray whitespace. Done with
# parameter expansion rather than sed so there are no backslashes to miscount.
MOUNT="$(sed -n 's/^BACKUP_DRIVE_MOUNT=//p' "$CFG" | tail -n1)"
MOUNT="${MOUNT%%#*}"
MOUNT="${MOUNT#"${MOUNT%%[![:space:]]*}"}"
MOUNT="${MOUNT%"${MOUNT##*[![:space:]]}"}"
case "$MOUNT" in
    \"*\") MOUNT="${MOUNT#\"}"; MOUNT="${MOUNT%\"}" ;;
    \'*\') MOUNT="${MOUNT#\'}"; MOUNT="${MOUNT%\'}" ;;
esac

# An unset knob must NOT turn the report off. A lane that silently stops
# reporting is the silent-green shape this whole unit exists to prevent, so fall
# back to the storage-map path and say loudly that that is what happened.
if [ -z "$MOUNT" ]; then
    MOUNT="$FALLBACK_MOUNT"
    echo "[backup-drive-health] BACKUP_DRIVE_MOUNT unset in $CFG — falling back to $MOUNT"
fi

exec "$GUARD" --report --library "$MOUNT" --label backup
