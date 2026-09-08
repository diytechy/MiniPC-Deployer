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

# Green means both the backup action and the configured verify action returned
# success.  The event log proves the one state post follows both, not merely a
# passing shell exit or a mocked helper called in isolation.
run_wrapper success "$TMP/good.env" shallow 0 0 200
if [ "$RUN_RC" = 0 ] && [ "$(post_count "$TMP/success.capture")" = 1 ] \
   && grep -Eq '^\{"id":"file-share-backup-health","lastSuccess":"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"\}$' "$TMP/success.capture" \
   && [ "$(tr '\n' ' ' <"$TMP/success.events")" = 'docker:backup docker:verify state ' ]; then
    pass "TC-001 SR-018 posts exactly one lastSuccess only after backup and verification"
else
    fail "TC-001 success boundary rc=$RUN_RC posts=$(post_count "$TMP/success.capture") events=$(tr '\n' ' ' <"$TMP/success.events")"
fi

run_wrapper preflight "$TMP/preflight-failure.env" none 0 0 200
if [ "$RUN_RC" != 0 ] && [ "$(post_count "$TMP/preflight.capture")" = 0 ] && [ ! -s "$TMP/preflight.events" ]; then
    pass "TC-001 SR-018 preflight refusal posts no lastSuccess and starts no container"
else
    fail "TC-001 preflight failure rc=$RUN_RC posts=$(post_count "$TMP/preflight.capture")"
fi

run_wrapper container-failure "$TMP/good.env" none 1 0 200
if [ "$RUN_RC" != 0 ] && [ "$(post_count "$TMP/container-failure.capture")" = 0 ] \
   && [ "$(tr '\n' ' ' <"$TMP/container-failure.events")" = 'docker:backup ' ]; then
    pass "TC-001 SR-018 failed container posts no lastSuccess"
else
    fail "TC-001 container failure rc=$RUN_RC posts=$(post_count "$TMP/container-failure.capture")"
fi

run_wrapper verify-failure "$TMP/good.env" shallow 0 3 200
if [ "$RUN_RC" != 0 ] && [ "$(post_count "$TMP/verify-failure.capture")" = 0 ] \
   && [ "$(tr '\n' ' ' <"$TMP/verify-failure.events")" = 'docker:backup docker:verify ' ]; then
    pass "TC-001 SR-018 failed verification posts no lastSuccess"
else
    fail "TC-001 verify failure rc=$RUN_RC posts=$(post_count "$TMP/verify-failure.capture")"
fi

run_wrapper rejected-state "$TMP/good.env" shallow 0 0 500
if [ "$RUN_RC" != 0 ] && [ "$(post_count "$TMP/rejected-state.capture")" = 1 ] \
   && [ "$(tr '\n' ' ' <"$TMP/rejected-state.events")" = 'docker:backup docker:verify state ' ]; then
    pass "TC-001 SR-018 non-200 state update fails the wrapper without a second post"
else
    fail "TC-001 state rejection rc=$RUN_RC posts=$(post_count "$TMP/rejected-state.capture")"
fi

printf '%s PASS  %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
