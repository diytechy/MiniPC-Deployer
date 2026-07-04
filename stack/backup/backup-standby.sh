#!/usr/bin/env bash
# backup-standby.sh — BOOT-TIME DEFAULT STANDBY for the AWOW backup drive(s)
# (WI-10.10 DRIVE POWER DESIGN). Run once per boot by backup-standby.service.
#
# hdparm -S timeouts do NOT persist across power cycles, so a conservative
# spin-down default is (re-)applied every boot to each BACKUP_DRIVE_DEVICES entry
# using BACKUP_DRIVE_STANDBY (default 241 = 30 min). During a backup run,
# backup.sh disables this standby (hdparm -S 0) and restores it on exit; this
# unit just owns the AT-REST default.
#
# NEVER fails boot: a missing config or empty device list is a clean no-op, and
# power-management errors (no hdparm, absent device, enclosure ignoring the
# command) are logged as warnings and skipped (see common.sh drive_standby_set).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

# Config discovery mirrors backup.sh. A missing backup.env means the box is not
# provisioned for backup yet — that must NOT fail boot, so we no-op instead of
# calling load_config's die().
CONFIG=""
if   [ -f /etc/awow-backup/backup.env ]; then CONFIG=/etc/awow-backup/backup.env
elif [ -f "$HERE/backup.env" ];         then CONFIG="$HERE/backup.env"
fi
if [ -z "$CONFIG" ]; then
    log "backup-standby: no backup.env found — nothing to configure (no-op)"
    exit 0
fi
load_config "$CONFIG"

read -r -a DEVICES <<< "${BACKUP_DRIVE_DEVICES:-}" || true
STANDBY_VALUE="${BACKUP_DRIVE_STANDBY:-241}"
if [ "${#DEVICES[@]}" -eq 0 ]; then
    log "backup-standby: BACKUP_DRIVE_DEVICES empty — no drives to configure (no-op)"
    exit 0
fi

log "backup-standby: applying default spin-down hdparm -S $STANDBY_VALUE ($(standby_desc "$STANDBY_VALUE")) to ${#DEVICES[@]} drive(s)"
drive_standby_set "$STANDBY_VALUE" "${DEVICES[@]}"
exit 0
