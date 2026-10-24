#!/usr/bin/env bash
# TC-1470..1474 / SR-121 / LLR-1300 - the nightly config backup reports its
# verified success to the unified backup lane as `nightlySuccess`, and ONLY that.
# Real code under test: common.sh feed_naglight + post_file_share_backup_state.
# Only the transports (curl, docker) are stubbed.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
COMMON="${COMMON_UNDER_TEST:-$ROOT/stack/backup/common.sh}"
BACKUP="${BACKUP_UNDER_TEST:-$ROOT/stack/backup/backup.sh}"
PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$*"; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"
# curl stub: log every argument line, print $STUB_CODE as the -w http_code.
cat >"$TMP/bin/curl" <<'STUB'
#!/usr/bin/env bash
{ printf 'CALL\n'; for a in "$@"; do printf 'ARG %s\n' "$a"; done; } >>"$CAPTURE"
printf '%s' "${STUB_CODE:-200}"
exit 0
STUB
# docker stub (wget-in-container path): log args; exit 0 = HTTP 2xx, else 8.
cat >"$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
{ printf 'CALL\n'; for a in "$@"; do printf 'ARG %s\n' "$a"; done; } >>"$CAPTURE"
if [ "${STUB_CODE:-200}" = 200 ]; then exit 0; fi
echo "  HTTP/1.1 ${STUB_CODE} Err" >&2; exit 8
STUB
chmod +x "$TMP/bin/curl" "$TMP/bin/docker"

# run_feed OK CODE [extra env...] : run feed_naglight in a fresh shell, capture
# posts to $CAPTURE, and print "<rc>|<log+warn text>".
run_feed() {
    local ok="$1" code="$2"; shift 2
    : >"$TMP/capture"; : >"$TMP/out"
    env CAPTURE="$TMP/capture" STUB_CODE="$code" PATH="$TMP/bin:$PATH" \
        FILE_SHARE_BACKUP_STATE_URL=http://mock.invalid/api/backup-state \
        FILE_SHARE_BACKUP_FEED_ID=file-share-backup-health \
        NAGLIGHT_TOKEN=tok-test "$@" bash -c '
            . "$1"; feed_naglight "$2" "note text"; echo "RC=$?"
        ' _ "$COMMON" "$ok" >"$TMP/out" 2>&1
    grep -c "^CALL$" "$TMP/capture" || true
}
body_of() { grep -A1 -- '^ARG -d$' "$TMP/capture" | tail -n1 | sed 's/^ARG //'; }
rc_of() { sed -n 's/^RC=//p' "$TMP/out"; }

# 1. Success posts exactly one body, nightlySuccess only, RFC3339 Z, with bearer.
n="$(run_feed true 200)"
b="$(body_of)"
if [ "$n" = 1 ] && printf '%s' "$b" | grep -qE '^\{"id":"file-share-backup-health","nightlySuccess":"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"\}$' \
   && grep -qx 'ARG Authorization: Bearer tok-test' "$TMP/capture" && [ "$(rc_of)" = 0 ] \
   && grep -q 'nightlySuccess=.*(HTTP 200)' "$TMP/out"; then
    pass "TC-1470 success posts exactly one {id,nightlySuccess} body with the bearer"
else fail "TC-1470 success body/bearer wrong (calls=$n body=$b)"; fi
# not lastSuccess / shareHealth / runState
if ! printf '%s' "$b" | grep -qE 'lastSuccess|shareHealth|runState'; then
    pass "TC-1474 the nightly body never carries another producer's field"
else fail "TC-1474 nightly body carries another producer's field: $b"; fi

# 2. Failure posts nothing (both transports) and returns 0.
n="$(run_feed false 200)"
if [ "$n" = 0 ] && [ "$(rc_of)" = 0 ] && grep -q 'NOT posted' "$TMP/out"; then
    pass "TC-1471 a failed nightly run posts nothing and says the age keeps growing"
else fail "TC-1471 failure path posted or lost its log line (calls=$n)"; fi

# 3. Non-200 on success still returns 0 with a WARNING.
n="$(run_feed true 500)"
if [ "$n" = 1 ] && [ "$(rc_of)" = 0 ] && grep -q 'WARN: nightly backup succeeded but nightlySuccess=.*HTTP 500' "$TMP/out"; then
    pass "TC-1472 a refused post warns and still returns 0"
else fail "TC-1472 non-200 handling wrong (calls=$n rc=$(rc_of))"; fi
n="$(run_feed true 000)"
if [ "$n" = 1 ] && [ "$(rc_of)" = 0 ] && grep -q 'WARN: nightly backup succeeded .*HTTP 000' "$TMP/out"; then
    pass "TC-1472 an unanswered post warns and still returns 0"
else fail "TC-1472 000 handling wrong"; fi

# 4. Missing config logs and returns 0, posts nothing.
n="$(run_feed true 200 FILE_SHARE_BACKUP_STATE_URL=)"
if [ "$n" = 0 ] && [ "$(rc_of)" = 0 ] && grep -q 'FILE_SHARE_BACKUP_STATE_URL unset' "$TMP/out"; then
    pass "TC-1473 unset URL skips quietly with rc 0"
else fail "TC-1473 unset URL handling wrong (calls=$n)"; fi

# 5. The container (docker exec wget) transport carries the same body + bearer.
n="$(run_feed true 200 NAGLIGHT_FEED_CONTAINER=tracker)"
if [ "$n" = 1 ] && grep -qx 'ARG exec' "$TMP/capture" \
   && grep -qxF 'ARG {"id":"file-share-backup-health","nightlySuccess":"'"$(grep -o '[0-9T:-]*Z' "$TMP/capture" | head -n1)"'"}' "$TMP/capture" \
   && grep -qx 'ARG Authorization: Bearer tok-test' "$TMP/capture"; then
    pass "TC-1470 the docker-exec transport posts the same body with the bearer"
else fail "TC-1470 docker transport wrong (calls=$n)"; fi

# 6. Poster rejects a malformed nightlySuccess.
: >"$TMP/capture"
env CAPTURE="$TMP/capture" PATH="$TMP/bin:$PATH" FILE_SHARE_BACKUP_STATE_URL=http://mock.invalid/x bash -c '
    log() { :; }; warn() { :; }; . "$1"; post_file_share_backup_state nightlySuccess "yesterday"' _ "$COMMON"
if [ "$(grep -c '^CALL$' "$TMP/capture")" = 0 ]; then pass "TC-1470 a non-RFC3339 nightlySuccess is refused locally"
else fail "TC-1470 malformed nightlySuccess was posted"; fi

# 7. Call-site shape in backup.sh: exactly one success call; failure calls are false.
if [ "$(grep -cE '^[[:space:]]*(if ! )?feed_naglight true' "$BACKUP")" = 1 ] \
   && ! grep -E 'feed_naglight' "$BACKUP" | grep -vE 'feed_naglight (true|false) ' | grep -vE '^[[:space:]]*#' | grep -q .; then
    pass "TC-1471 backup.sh has exactly one success report; every other call reports false"
else fail "TC-1471 backup.sh feed_naglight call sites changed shape"; fi
if grep -nE 'feed_naglight true' "$BACKUP" | grep -qE 'backup ok'; then
    pass "TC-1471 the single success report is the 'backup ok' path"
else fail "TC-1471 success report is not the 'backup ok' path"; fi

printf '%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
