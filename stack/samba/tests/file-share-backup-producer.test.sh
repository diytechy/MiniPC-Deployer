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
# The mountinfo fixture's device must also exist in the sysfs fixture. A
# present mountinfo line alone is no longer proof that the USB disk survived.
export SYS_DEV_BLOCK_DIR="$TMP/sys-dev-block"
mkdir -p "$SYS_DEV_BLOCK_DIR/0:31"

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
# path, and the config backup posts only nightlySuccess (see
# stack/backup/tests/nightly-success-feed.test.sh).
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

# ── F2: A REFUSAL IS NOT SILENCE ─────────────────────────────────────────────
# On 2026-09-13 the composite lane's row had just been added to the Sheet and
# the tracker had not reloaded it, so every post from library-backup.sh came
# back {"error":"invalid_id"}. The docker-exec branch reported all of them as
# `HTTP 000` — which reads as "nothing answered" — and the night's verified
# success was lost with no diagnosable cause in the journal. The status line and
# the body are both available (library-guard.sh has always read them); this
# locks them being read here too.
mkdir -p "$TMP/dockerbin"
cat >"$TMP/dockerbin/docker" <<'EOF'
#!/usr/bin/env bash
# A tracker that refuses the id, exactly as the live one did: GNU wget prints
# the status line to stderr under -S and the body to stdout under
# --content-on-error, then exits 8.
case "$1" in
  exec)
    printf '  HTTP/1.1 400 Bad Request\n  Content-Type: application/json\n  Cache-Control: no-store\n' >&2
    printf '{"error":"invalid_id"}\n'
    exit 8 ;;
esac
exit 0
EOF
cat >"$TMP/dockerbin/docker-ok" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  exec) printf '  HTTP/1.1 200 OK\n' >&2; exit 0 ;;
esac
exit 0
EOF
cat >"$TMP/dockerbin/docker-dead" <<'EOF'
#!/usr/bin/env bash
# Nothing answered: no status line at all, which is the ONLY case that is 000.
case "$1" in
  exec) printf 'wget: unable to resolve host address\n' >&2; exit 4 ;;
esac
exit 0
EOF
chmod +x "$TMP/dockerbin/docker" "$TMP/dockerbin/docker-ok" "$TMP/dockerbin/docker-dead"

state_post() { # state_post MOCK FIELD VALUE -> prints "<code>|<log lines>"
    local mock="$1" field="$2" value="$3" out
    mkdir -p "$TMP/run-$$"; cp "$TMP/dockerbin/$mock" "$TMP/run-$$/docker"; chmod +x "$TMP/run-$$/docker"
    out="$(PATH="$TMP/run-$$:$PATH" NAGLIGHT_FEED_CONTAINER=tracker \
        FILE_SHARE_BACKUP_STATE_URL=http://127.0.0.1:8787/api/backup-state \
        FILE_SHARE_BACKUP_FEED_ID=file-share-backup-health \
        bash -c '
            log() { printf "LOG %s\n" "$*"; }; warn() { printf "WARN %s\n" "$*"; }
            . "$1"
            post_file_share_backup_state "$2" "$3"
            printf "CODE %s\n" "$FILE_SHARE_BACKUP_STATE_LAST_CODE"
        ' _ "$COMMON" "$field" "$value" 2>&1)"
    rm -rf "$TMP/run-$$"
    printf '%s' "$out"
}

REFUSED="$(state_post docker lastSuccess 2026-09-08T03:04:05Z)"
printf '%s' "$REFUSED" | grep -q 'CODE 400' || {
    echo "FAIL: a refused post must report HTTP 400, not 000 — got: $REFUSED" >&2; exit 1; }
printf '%s' "$REFUSED" | grep -q 'invalid_id' || {
    echo "FAIL: the tracker's own reason must reach the journal — got: $REFUSED" >&2; exit 1; }
OK="$(state_post docker-ok runState verifying)"
printf '%s' "$OK" | grep -q 'CODE 200' || {
    echo "FAIL: an accepted post must still report 200 — got: $OK" >&2; exit 1; }
DEAD="$(state_post docker-dead runState idle)"
printf '%s' "$DEAD" | grep -q 'CODE 000' || {
    echo "FAIL: only an unanswered request may report 000 — got: $DEAD" >&2; exit 1; }
echo "PASS: F2 a refused state post is reported as a refusal, with the tracker's reason"

# ── F3: A DEAD TRACKER IS HTTP 000, NOT 000000 ───────────────────────────────
# The curl branch of library-guard.sh used `curl -w '%{http_code}' ... || echo
# 000`. On connection refused curl prints its own 000 AND exits 7, so the
# fallback appended and the journal said "HTTP 000000". Drive the real --report
# path with a curl that is refused, and one that times out, and record argv to
# prove the transport deadline is passed.
mkdir -p "$TMP/curl-failing"
cat >"$TMP/curl-failing/curl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$CURL_ARGV_CAPTURE"
case "$CURL_MODE" in
    refused) printf '000'; exit 7 ;;
    timeout) exit 28 ;;
esac
EOF
chmod +x "$TMP/curl-failing/curl"
guard_report_with_failing_curl() { # MODE -> guard stdout; argv in $TMP/curl-argv
    : >"$TMP/curl-argv"
    PATH="$TMP/curl-failing:$TMP/bin:$PATH" MOUNTINFO_FILE="$TMP/mountinfo" ENV_FILE="$TMP/env" \
        SMBCLIENT_BIN="$TMP/bin/smbclient" SMBCLIENT_EXIT=0 CURL_MODE="$1" \
        CURL_ARGV_CAPTURE="$TMP/curl-argv" TIMEOUT_CAPTURE="$TMP/timeout-seconds" \
        GUARD_REATTACH=0 "$GUARD" --report 2>&1 || true
}
REFUSED_OUT="$(guard_report_with_failing_curl refused)"
grep -q -- '--connect-timeout 5' "$TMP/curl-argv" && grep -q -- '--max-time 15' "$TMP/curl-argv" || {
    echo "FAIL: guard report curl must pass --connect-timeout 5 and --max-time 15 — argv: $(cat "$TMP/curl-argv")" >&2; exit 1; }
printf '%s\n' "$REFUSED_OUT" | grep -q 'report got HTTP 000$' || {
    echo "FAIL: a refused tracker must log 'HTTP 000' — got: $REFUSED_OUT" >&2; exit 1; }
printf '%s\n' "$REFUSED_OUT" | grep -q '000000' && {
    echo "FAIL: a refused tracker must not log 000000 — got: $REFUSED_OUT" >&2; exit 1; }
TIMED_OUT_OUT="$(guard_report_with_failing_curl timeout)"
printf '%s\n' "$TIMED_OUT_OUT" | grep -q 'report got HTTP 000$' || {
    echo "FAIL: a timed-out tracker must log 'HTTP 000' — got: $TIMED_OUT_OUT" >&2; exit 1; }
echo "PASS: F3 an unanswered state post logs HTTP 000 (refused and timed out), with the transport deadline"

echo "PASS: TC-001 SR-018 producer payload separation and upgrade retirement"
echo "3 PASS  0 FAIL"
