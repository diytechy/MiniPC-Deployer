#!/usr/bin/env bash
# Remove the superseded per-backup-drive health unit on fresh installs and
# upgrades. Its old invocation would corrupt SR-018's one share-health signal.
set -uo pipefail

SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
TIMER=homehub-backup-drive-health.timer
SERVICE=homehub-backup-drive-health.service

present=0
[ -e "$SYSTEMD_UNIT_DIR/$TIMER" ] && present=1
[ -e "$SYSTEMD_UNIT_DIR/$SERVICE" ] && present=1
"$SYSTEMCTL_BIN" is-enabled --quiet "$TIMER" >/dev/null 2>&1 && present=1
[ "$present" -eq 1 ] || exit 0

"$SYSTEMCTL_BIN" disable --now "$TIMER" >/dev/null 2>&1 || true
rm -f -- "$SYSTEMD_UNIT_DIR/$TIMER" "$SYSTEMD_UNIT_DIR/$SERVICE"
"$SYSTEMCTL_BIN" daemon-reload >/dev/null 2>&1
echo "retired superseded $TIMER and $SERVICE"
