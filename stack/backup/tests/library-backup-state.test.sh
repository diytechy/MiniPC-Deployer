#!/usr/bin/env bash
# TC-001 / SR-018 — execute the real FileBackup host wrapper at its state-update
# boundary.  Mocks replace only host tools (mount topology, docker and curl); the
# script under test remains library-backup.sh, so the order after preflight,
# backup and optional restore verification cannot drift unobserved.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
WRAPPER="$ROOT/stack/backup/library-backup.sh"
PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$*"; }

[ -x "$WRAPPER" ] || { echo "FATAL: missing wrapper $WRAPPER" >&2; exit 2; }
TMP="$(mktemp -d)"
KEEP_TMP="${KEEP_TMP:-0}"
cleanup() { [ "$KEEP_TMP" = 1 ] && { printf 'tmp kept: %s\n' "$TMP"; return; }; rm -rf "$TMP"; }
trap cleanup EXIT
mkdir -p "$TMP/bin" "$TMP/src" "$TMP/backup" "$TMP/changes" "$TMP/state" "$TMP/logs"
: >"$TMP/filebackup.json"

# The actual wrapper asks findmnt two subtly different questions: the exact
# source mount must remain its own mountpoint, while the changes path must be on
# the same enclosing mount as the backup store.  This mock models both facts.
cat >"$TMP/bin/findmnt" <<'EOF'
#!/usr/bin/env bash
last=""; for arg in "$@"; do last="$arg"; done
if [[ " $* " == *" --target "* ]]; then
    case "$last" in
        "$FILEBACKUP_SOURCE") printf '%s\n' "$FILEBACKUP_SOURCE" ;;
        "$FILEBACKUP_BACKUP"|"$FILEBACKUP_CHANGES") printf '%s\n' "$FILEBACKUP_BACKUP" ;;
        *) printf '%s\n' "$last" ;;
    esac
elif [[ " $* " == *" -o SOURCE,FSTYPE "* ]]; then
    printf 'mockfs ext4\n'
fi
EOF
cat >"$TMP/bin/df" <<'EOF'
#!/usr/bin/env bash
printf 'Filesystem 1B-blocks Used Available Use%% Mounted on\n'
printf 'mockfs 1000000000000 100000000000 900000000000 10%% /mock\n'
EOF
cat >"$TMP/bin/flock" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$TMP/bin/chown" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$TMP/bin/runuser" <<'EOF'
#!/usr/bin/env bash
while [ "$1" != -- ]; do shift; done
shift
"$@"
EOF
cat >"$TMP/bin/id" <<'EOF'
#!/usr/bin/env bash
# Git Bash has no Unix root account; the wrapper only needs this probe to choose
# its runuser path, whose mock executes the requested touch in the temp tree.
[ "${1:-}" = -u ] && { printf '0\n'; exit 0; }
exit 1
EOF
cat >"$TMP/bin/docker" <<'EOF'
#!/usr/bin/env bash
last=""; for arg in "$@"; do last="$arg"; done
printf '%s\n' "docker:$last" >>"$EVENTS"
case "$last" in
    backup) exit "${MOCK_BACKUP_RC:-0}" ;;
    verify|-Deep) exit "${MOCK_VERIFY_RC:-0}" ;;
    *) exit 97 ;;
esac
EOF
cat >"$TMP/bin/curl" <<'EOF'
#!/usr/bin/env bash
body=""
while [ "$#" -gt 0 ]; do
    if [ "$1" = -d ]; then body="$2"; shift 2; continue; fi
    shift
done
printf '%s\n' "$body" >>"$CAPTURE"
printf 'state\n' >>"$EVENTS"
printf '%s' "${MOCK_HTTP_CODE:-200}"
EOF
chmod +x "$TMP/bin"/*

cat >"$TMP/fstab" <<EOF
mock $TMP/src ext4 defaults 0 0
mock $TMP/backup ext4 defaults 0 0
EOF
cat >"$TMP/mountinfo" <<EOF
24 22 0:31 / $TMP/src rw,relatime - tmpfs tmpfs rw
25 22 0:32 / $TMP/backup rw,relatime - tmpfs tmpfs rw
EOF

write_config() {
    local target="$1"
    cat >"$target" <<EOF
FILEBACKUP_CONFIG=$TMP/filebackup.json
FILEBACKUP_SOURCE=$TMP/src
FILEBACKUP_STATE=$TMP/state
FILEBACKUP_BACKUP=$TMP/backup
FILEBACKUP_CHANGES=$TMP/changes
FILEBACKUP_LOGS=$TMP/logs
FILEBACKUP_USER_NAME=root
LIBRARY_MOUNTS_FSTAB=$TMP/fstab
DRIVE_IDENTITY_FILE=$TMP/no-identity
BACKUP_LIBRARY_MIN_FREE_GB=1
BACKUP_LIBRARY_MIN_FREE_STATE_GB=1
BACKUP_DRIVE_DEVICES=""
FILE_SHARE_BACKUP_FEED_ID=file-share-backup-health
FILE_SHARE_BACKUP_STATE_URL=http://mock.invalid/api/backup-state
EOF
}
write_config "$TMP/good.env"
cat >"$TMP/preflight-failure.env" <<EOF
FILEBACKUP_CONFIG=$TMP/missing-filebackup.json
FILEBACKUP_SOURCE=$TMP/src
FILEBACKUP_STATE=$TMP/state
FILEBACKUP_BACKUP=$TMP/backup
FILEBACKUP_CHANGES=$TMP/changes
FILEBACKUP_LOGS=$TMP/logs
FILEBACKUP_USER_NAME=root
LIBRARY_MOUNTS_FSTAB=$TMP/fstab
DRIVE_IDENTITY_FILE=$TMP/no-identity
BACKUP_LIBRARY_MIN_FREE_GB=1
BACKUP_LIBRARY_MIN_FREE_STATE_GB=1
BACKUP_DRIVE_DEVICES=""
FILE_SHARE_BACKUP_FEED_ID=file-share-backup-health
FILE_SHARE_BACKUP_STATE_URL=http://mock.invalid/api/backup-state
EOF

run_wrapper() {
    local name="$1" config="$2" verify="$3" backup_rc="$4" verify_rc="$5" http_code="$6"
    : >"$TMP/$name.capture"; : >"$TMP/$name.events"
    PATH="$TMP/bin:$PATH" MOUNTINFO_FILE="$TMP/mountinfo" CAPTURE="$TMP/$name.capture" EVENTS="$TMP/$name.events" \
        MOCK_BACKUP_RC="$backup_rc" MOCK_VERIFY_RC="$verify_rc" MOCK_HTTP_CODE="$http_code" \
        LIBRARY_ROOT="$TMP/src" \
        LIBRARY_BACKUP_LOCK="$TMP/$name.lock" \
        bash "$WRAPPER" --config "$config" --verify "$verify" >"$TMP/$name.out" 2>&1
    RUN_RC=$?
}
post_count() { [ -s "$1" ] && wc -l <"$1" | tr -d ' ' || printf 0; }
# The assertions below count LASTSUCCESS posts, not total posts. Run phases
# (SR-065 as amended) share this endpoint as an independent field, so a total
# count would now conflate "claimed a backup succeeded" with "said where the run
# is" — which is exactly the confusion the separate field exists to prevent.
# grep -c PRINTS 0 and EXITS 1 on no match, so a `|| printf 0` fallback appends a
# second zero and every comparison against "0" fails. Take the output, drop the
# status.
success_count() { local n; n="$(grep -c '"lastSuccess"' "$1" 2>/dev/null)"; printf '%s' "${n:-0}"; }
phases() { grep -o '"runState":"[a-z-]*"' "$1" 2>/dev/null | sed 's/.*:"//;s/"//' | tr '\n' ' '; }

run_reconcile() {
    local name="$1" lock="$2"
    : >"$TMP/$name.capture"; : >"$TMP/$name.events"
    PATH="$TMP/bin:$PATH" MOUNTINFO_FILE="$TMP/mountinfo" CAPTURE="$TMP/$name.capture" EVENTS="$TMP/$name.events" \
        MOCK_HTTP_CODE=200 LIBRARY_ROOT="$TMP/src" LIBRARY_BACKUP_LOCK="$lock" \
        bash "$WRAPPER" --config "$TMP/good.env" --reconcile-run-state >"$TMP/$name.out" 2>&1
    RUN_RC=$?
}

# Green means both the backup action and the configured verify action returned
# success.  The event log proves the one state post follows both, not merely a
# passing shell exit or a mocked helper called in isolation.
run_wrapper success "$TMP/good.env" shallow 0 0 200
if [ "$RUN_RC" = 0 ] && [ "$(success_count "$TMP/success.capture")" = 1 ] \
   && grep -Eq '^\{"id":"file-share-backup-health","lastSuccess":"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"\}$' "$TMP/success.capture" \
   && [ "$(tr '\n' ' ' <"$TMP/success.events")" = 'state state docker:backup state docker:verify state state ' ]; then
    pass "TC-001 SR-018 posts exactly one lastSuccess only after backup and verification"
else
    fail "TC-001 success boundary rc=$RUN_RC lastSuccess=$(success_count "$TMP/success.capture") events=$(tr '\n' ' ' <"$TMP/success.events")"
fi

# The phase order is the contract the wall reads. `verifying` must sit between
# the two container calls, and `idle` must come AFTER lastSuccess so the lane
# never shows "finished, at yesterday's age" for even one poll.
if [ "$(phases "$TMP/success.capture")" = 'starting backing-up verifying idle ' ] \
   && [ "$(grep -n '"runState":"idle"' "$TMP/success.capture" | cut -d: -f1)" -gt "$(grep -n '"lastSuccess"' "$TMP/success.capture" | cut -d: -f1)" ]; then
    pass "SR-065 phases run starting -> backing-up -> verifying -> idle, with idle after the success"
else
    fail "SR-065 phase order was '$(phases "$TMP/success.capture")'"
fi

# INDEPENDENCE. No phase post may carry another field: a producer saying where a
# run is must never be able to clear a share fault or claim freshness.
if ! grep '"runState"' "$TMP/success.capture" | grep -q '"lastSuccess"\|"shareHealth"'; then
    pass "SR-065 a phase post carries no other state field"
else
    fail "SR-065 a phase post carried a second signal: $(grep '"runState"' "$TMP/success.capture")"
fi

run_wrapper preflight "$TMP/preflight-failure.env" none 0 0 200
if [ "$RUN_RC" != 0 ] && [ "$(success_count "$TMP/preflight.capture")" = 0 ] \
   && ! grep -q docker "$TMP/preflight.events" \
   && [ "$(phases "$TMP/preflight.capture")" = 'starting idle ' ]; then
    pass "TC-001 SR-018 preflight refusal posts no lastSuccess, starts no container, and withdraws its phase"
else
    fail "TC-001 preflight failure rc=$RUN_RC lastSuccess=$(success_count "$TMP/preflight.capture") phases='$(phases "$TMP/preflight.capture")'"
fi

run_wrapper container-failure "$TMP/good.env" none 1 0 200
if [ "$RUN_RC" != 0 ] && [ "$(success_count "$TMP/container-failure.capture")" = 0 ] \
   && [ "$(phases "$TMP/container-failure.capture")" = 'starting backing-up idle ' ]; then
    pass "TC-001 SR-018 failed container posts no lastSuccess and ends idle"
else
    fail "TC-001 container failure rc=$RUN_RC lastSuccess=$(success_count "$TMP/container-failure.capture") phases='$(phases "$TMP/container-failure.capture")'"
fi

run_wrapper verify-failure "$TMP/good.env" shallow 0 3 200
if [ "$RUN_RC" != 0 ] && [ "$(success_count "$TMP/verify-failure.capture")" = 0 ] \
   && [ "$(phases "$TMP/verify-failure.capture")" = 'starting backing-up verifying idle ' ]; then
    pass "TC-001 SR-018 failed verification posts no lastSuccess and ends idle"
else
    fail "TC-001 verify failure rc=$RUN_RC lastSuccess=$(success_count "$TMP/verify-failure.capture") phases='$(phases "$TMP/verify-failure.capture")'"
fi

run_wrapper rejected-state "$TMP/good.env" shallow 0 0 500
if [ "$RUN_RC" != 0 ] && [ "$(success_count "$TMP/rejected-state.capture")" = 1 ]; then
    pass "TC-001 SR-018 non-200 state update fails the wrapper without a second success post"
else
    fail "TC-001 state rejection rc=$RUN_RC lastSuccess=$(success_count "$TMP/rejected-state.capture")"
fi

# RECONCILE. The stale-phase recovery is lock-aware rather than time-bounded:
# library backups are legitimately bimodal, so no duration could tell a long run
# from a dead one, but the kernel drops the lock when a holder dies.
run_reconcile reconcile-free "$TMP/reconcile-free.lock"
if [ "$RUN_RC" = 0 ] && [ "$(phases "$TMP/reconcile-free.capture")" = 'idle ' ] \
   && [ "$(success_count "$TMP/reconcile-free.capture")" = 0 ] \
   && ! grep -q docker "$TMP/reconcile-free.events"; then
    pass "SR-065 reconcile withdraws a stale phase, claims no success, and starts no container"
else
    fail "SR-065 reconcile rc=$RUN_RC phases='$(phases "$TMP/reconcile-free.capture")' events=$(tr '\n' ' ' <"$TMP/reconcile-free.events")"
fi

# A HELD LOCK IS THE ANSWER, NOT A REFUSAL: a live run owns the phase, so
# reconcile must leave it alone and exit zero — it runs on a 10-minute timer.
cat >"$TMP/bin/flock" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$TMP/bin/flock"
run_reconcile reconcile-held "$TMP/reconcile-held.lock"
if [ "$RUN_RC" = 0 ] && [ "$(post_count "$TMP/reconcile-held.capture")" = 0 ]; then
    pass "SR-065 reconcile leaves a live run's phase alone and exits zero"
else
    fail "SR-065 reconcile with a held lock rc=$RUN_RC posts=$(post_count "$TMP/reconcile-held.capture")"
fi

# And a NORMAL run that cannot take the lock still posts nothing at all — the
# running backup owns the lane, and a second start must not touch it.
run_wrapper lock-held "$TMP/good.env" none 0 0 200
if [ "$RUN_RC" != 0 ] && [ "$(post_count "$TMP/lock-held.capture")" = 0 ]; then
    pass "SR-018 a start refused by a held lock posts nothing"
else
    fail "SR-018 lock-held start rc=$RUN_RC posts=$(post_count "$TMP/lock-held.capture")"
fi

printf '%s PASS  %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
