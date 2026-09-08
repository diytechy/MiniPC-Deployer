#!/usr/bin/env bash
# TC-001 / SR-018 producer and upgrade contract: one health signal, no
# timestamp mutation path, and no surviving legacy timer.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
GUARD="$ROOT/stack/samba/library-guard.sh"
COMMON="$ROOT/stack/backup/common.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"

cat >"$TMP/mountinfo" <<'EOF'
24 22 0:31 / /srv/library rw,relatime - tmpfs tmpfs rw
EOF
cat >"$TMP/mountinfo-ro" <<'EOF'
24 22 0:31 / /srv/library ro,relatime - tmpfs tmpfs ro
EOF
: >"$TMP/mountinfo-absent"
cat >"$TMP/env" <<'EOF'
FILE_SHARE_BACKUP_FEED_ID=file-share-backup-health
FILE_SHARE_BACKUP_STATE_URL=http://mock.invalid/api/backup-state
FILE_SHARE_SAMBA_PROBE_SHARE=Shared
FILE_SHARE_SAMBA_PROBE_TIMEOUT_SECONDS=7
EOF
cat >"$TMP/bin/smbclient" <<'EOF'
#!/usr/bin/env bash
exit "${SMBCLIENT_EXIT:-0}"
EOF
cat >"$TMP/bin/curl" <<'EOF'
#!/usr/bin/env bash
for ((i=1; i <= $#; i++)); do
    if [ "${!i}" = "-d" ]; then j=$((i + 1)); printf '%s\n' "${!j}" >>"$CAPTURE"; fi
done
printf '200'
EOF
cat >"$TMP/bin/timeout" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$1" >"$TIMEOUT_CAPTURE"
shift
"$@"
EOF
chmod +x "$TMP/bin/smbclient" "$TMP/bin/curl" "$TMP/bin/timeout"

run_guard() {
    local mount_state="${2:-present}"
    local mountinfo_path="$TMP/mountinfo"
    [ "$mount_state" = absent ] && mountinfo_path="$TMP/mountinfo-absent"
    [ "$mount_state" = read-only ] && mountinfo_path="$TMP/mountinfo-ro"
    : >"$CAPTURE"
    PATH="$TMP/bin:$PATH" MOUNTINFO_FILE="$mountinfo_path" ENV_FILE="$TMP/env" \
        SMBCLIENT_BIN="$TMP/bin/smbclient" SMBCLIENT_EXIT="$1" CAPTURE="$CAPTURE" \
        TIMEOUT_CAPTURE="$TMP/timeout-seconds" \
        GUARD_REATTACH=0 "$GUARD" --report >/dev/null 2>&1 || true
    tail -n1 "$CAPTURE"
}

CAPTURE="$TMP/capture"
[ "$(run_guard 1)" = '{"id":"file-share-backup-health","shareHealth":"red"}' ] || {
    echo "FAIL: unavailable Samba must send only shareHealth=red" >&2; exit 1; }
[ "$(run_guard 0 absent)" = '{"id":"file-share-backup-health","shareHealth":"red"}' ] || {
    echo "FAIL: absent library mount must send only shareHealth=red" >&2; exit 1; }
[ "$(run_guard 0)" = '{"id":"file-share-backup-health","shareHealth":"clear"}' ] || {
    echo "FAIL: recovery must send only shareHealth=clear" >&2; exit 1; }
[ "$(run_guard 0 read-only)" = '{"id":"file-share-backup-health","shareHealth":"clear"}' ] || {
    echo "FAIL: a mounted readable Samba share must stay clear regardless of mount mode" >&2; exit 1; }
[ "$(cat "$TMP/timeout-seconds")" = 7 ] || {
    echo "FAIL: backup.env Samba probe timeout was not honored" >&2; exit 1; }
PATH="$TMP/bin:$PATH" MOUNTINFO_FILE="$TMP/mountinfo" \
    "$GUARD" --check --library /mnt/backup-drive --label backup >/dev/null 2>&1 && {
        echo "FAIL: --check swallowed --library and checked /srv/library instead" >&2; exit 1; }
PATH="$TMP/bin:$PATH" MOUNTINFO_FILE="$TMP/mountinfo-ro" \
    "$GUARD" --check Shared >/dev/null 2>&1 && {
        echo "FAIL: Samba preexec must still reject a read-only library" >&2; exit 1; }

# The FileBackup-only helper has a distinct payload. A monitor cannot call this
# path, and the legacy config backup is retained as a no-op to prevent deleted
# check ids from breaking archival work.
CAPTURE="$CAPTURE" PATH="$TMP/bin:$PATH" FILE_SHARE_BACKUP_STATE_URL=http://mock.invalid/api/backup-state \
    FILE_SHARE_BACKUP_FEED_ID=file-share-backup-health bash -c '
        log() { :; }; warn() { :; }; . "$1"; post_file_share_backup_state lastSuccess 2026-09-08T03:04:05Z
    ' _ "$COMMON"
[ "$(tail -n1 "$CAPTURE")" = '{"id":"file-share-backup-health","lastSuccess":"2026-09-08T03:04:05Z"}' ] || {
    echo "FAIL: verified FileBackup must send only RFC3339 lastSuccess" >&2; exit 1; }
if grep -q 'NAGLIGHT_FEED_' "$ROOT/stack/backup/backup.sh"; then
    echo "FAIL: config-backup must not retain a legacy NagLight feed id" >&2
    exit 1
fi
TEMPLATE="$ROOT/stack/backup/backup.env.example"
for expected in \
    'FILE_SHARE_BACKUP_FEED_ID=file-share-backup-health' \
    'FILE_SHARE_BACKUP_STATE_URL=http://127.0.0.1:8787/api/backup-state' \
    'FILE_SHARE_SAMBA_PROBE_SHARE=Shared'; do
    grep -qx "$expected" "$TEMPLATE" || {
        echo "FAIL: backup.env template is missing '$expected'" >&2; exit 1; }
done
grep -qx 'NAGLIGHT_FEED_URL=http://127.0.0.1:8787/api/feed' "$TEMPLATE" || {
    echo "FAIL: independent definitions guard lost its generic feed transport" >&2; exit 1; }
if grep -qE '^(LIBRARY_BACKUP_FEED_CHECK|NAGLIGHT_FEED_CHECK)=' "$TEMPLATE"; then
    echo "FAIL: backup.env template retains a legacy visible-feed knob" >&2
    exit 1
fi

# A lingering installed legacy unit must be removed idempotently. Its old guard
# invocation is also refused before it can send any state during a race.
UNIT_DIR="$TMP/units"
mkdir -p "$UNIT_DIR"
: >"$UNIT_DIR/homehub-backup-drive-health.timer"
: >"$UNIT_DIR/homehub-backup-drive-health.service"
: >"$TMP/enabled"
cat >"$TMP/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$SYSTEMCTL_CAPTURE"
case "$1" in
    is-enabled) [ -f "$SYSTEMCTL_ENABLED" ] ;;
    disable) rm -f "$SYSTEMCTL_ENABLED" ;;
esac
EOF
chmod +x "$TMP/bin/systemctl"
SYSTEMCTL_CAPTURE="$TMP/systemctl-calls" SYSTEMCTL_ENABLED="$TMP/enabled" \
    SYSTEMCTL_BIN="$TMP/bin/systemctl" SYSTEMD_UNIT_DIR="$UNIT_DIR" \
    "$ROOT/stack/samba/retire-legacy-backup-health.sh" >/dev/null
[ ! -e "$UNIT_DIR/homehub-backup-drive-health.timer" ] && \
[ ! -e "$UNIT_DIR/homehub-backup-drive-health.service" ] || {
    echo "FAIL: legacy installed unit files survived migration" >&2; exit 1; }
grep -qx 'disable --now homehub-backup-drive-health.timer' "$TMP/systemctl-calls" || {
    echo "FAIL: migration did not disable and stop the legacy timer" >&2; exit 1; }
grep -qx 'daemon-reload' "$TMP/systemctl-calls" || {
    echo "FAIL: migration did not reload systemd" >&2; exit 1; }

: >"$CAPTURE"
PATH="$TMP/bin:$PATH" MOUNTINFO_FILE="$TMP/mountinfo" ENV_FILE="$TMP/env" \
    SMBCLIENT_BIN="$TMP/bin/smbclient" CAPTURE="$CAPTURE" GUARD_REATTACH=0 \
    "$GUARD" --report --library /mnt/backup-drive --label backup >/dev/null 2>&1 && {
        echo "FAIL: legacy backup-drive report invocation was accepted" >&2; exit 1; }
[ ! -s "$CAPTURE" ] || {
    echo "FAIL: refused legacy invocation still posted combined state" >&2; exit 1; }

echo "PASS: TC-001 SR-018 producer payload separation and upgrade retirement"
echo "1 PASS  0 FAIL"
