#!/usr/bin/env bash
# TC-001 / SR-018 — execute the real FileBackup host wrapper at its state-update
# boundary.  Mocks replace only host tools (mount topology, docker and curl); the
# script under test remains library-backup.sh, so the order after preflight,
# backup and optional restore verification cannot drift unobserved.
#
# The wrapper is written for a FileBackup image built from its bounded-run-loop
# work (BoundedRebuild): one /store mount, backup exits 0/1/2/4, verify exits
# 0/1/2. The cases below pin that contract (TC-1066..TC-1069) as well as the
# state-update boundary.
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
# store/ is the ONE writable bind (holding backup/ and changes/); old-backup,
# old-changes and state are where an unmigrated hub's three-mount store lived.
mkdir -p "$TMP/bin" "$TMP/src" "$TMP/store/backup" "$TMP/store/changes" "$TMP/state" "$TMP/logs"
: >"$TMP/filebackup.json"
# The latest manifest the (mocked) container leaves behind; the wrapper copies
# it to the system disk for frame-freshness after every completed run.
printf 'RelativePath,LastWriteTimeStr\nShared/a.jpg,2026-10-01T00:00:00Z\n' >"$TMP/store/backup/MANIFEST.csv"

# The actual wrapper asks findmnt two subtly different questions: the exact
# source mount must remain its own mountpoint, while backup/ and changes/ must
# be on the same enclosing mount as the store.  This mock models both facts.
# MOCK_SPLIT_CHANGES=1 models a second mount under the store at changes/.
cat >"$TMP/bin/findmnt" <<'EOF'
#!/usr/bin/env bash
last=""; for arg in "$@"; do last="$arg"; done
if [[ " $* " == *" --target "* ]]; then
    case "$last" in
        "$FILEBACKUP_SOURCE") printf '%s\n' "$FILEBACKUP_SOURCE" ;;
        "$FILEBACKUP_STORE/changes")
            if [ "${MOCK_SPLIT_CHANGES:-0}" = 1 ]; then printf '%s\n' "$FILEBACKUP_STORE/changes"
            else printf '%s\n' "$FILEBACKUP_STORE"; fi ;;
        "$FILEBACKUP_STORE"|"$FILEBACKUP_STORE"/*) printf '%s\n' "$FILEBACKUP_STORE" ;;
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
# The reconciler asks the DAEMON whether a FileBackup container is still up,
# because the flock cannot answer that: `compose run` containers are daemon-owned
# and outlive a SIGKILLed client. MOCK_FB_RUNNING models "still up";
# MOCK_DOCKER_PS_RC models "cannot tell".
if [ "${1:-}" = ps ]; then
    [ "${MOCK_DOCKER_PS_RC:-0}" = 0 ] || exit "${MOCK_DOCKER_PS_RC}"
    [ -n "${MOCK_FB_RUNNING:-}" ] && printf '%s\n' "$MOCK_FB_RUNNING"
    exit 0
fi
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
mock $TMP/store ext4 defaults 0 0
EOF
cat >"$TMP/mountinfo" <<EOF
24 22 0:31 / $TMP/src rw,relatime - tmpfs tmpfs rw
25 22 0:32 / $TMP/store rw,relatime - tmpfs tmpfs rw
EOF
# The guard also asks the kernel whether each mounted device still EXISTS
# (e32a1b4: a vanished device is a stale mount). 0:31 and 0:32 are fictional, so
# model them as present; without this every run refused as "stale" and the
# suite asserted nothing past the preflight.
mkdir -p "$TMP/sys-dev-block"
: >"$TMP/sys-dev-block/0:31"; : >"$TMP/sys-dev-block/0:32"

# The retired three-mount keys are set the way an unmigrated hub's .env still
# carries them, pointed at temp paths, so the store-layout guard reads only this
# tree and never the real /mnt/backup-drive.
write_config() {
    local target="$1" copy="${2:-$TMP/latest/MANIFEST.csv}"
    cat >"$target" <<EOF
FILEBACKUP_CONFIG=$TMP/filebackup.json
FILEBACKUP_SOURCE=$TMP/src
FILEBACKUP_STORE=$TMP/store
FILEBACKUP_STATE=$TMP/state
FILEBACKUP_BACKUP=$TMP/old-backup
FILEBACKUP_CHANGES=$TMP/old-changes
FILEBACKUP_LOGS=$TMP/logs
LIBRARY_MANIFEST_COPY=$copy
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
FILEBACKUP_STORE=$TMP/store
FILEBACKUP_STATE=$TMP/state
FILEBACKUP_BACKUP=$TMP/old-backup
FILEBACKUP_CHANGES=$TMP/old-changes
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
    PATH="$TMP/bin:$PATH" MOUNTINFO_FILE="$TMP/mountinfo" SYS_DEV_BLOCK_DIR="$TMP/sys-dev-block" CAPTURE="$TMP/$name.capture" EVENTS="$TMP/$name.events" \
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
    PATH="$TMP/bin:$PATH" MOUNTINFO_FILE="$TMP/mountinfo" SYS_DEV_BLOCK_DIR="$TMP/sys-dev-block" CAPTURE="$TMP/$name.capture" EVENTS="$TMP/$name.events" \
        MOCK_HTTP_CODE=200 LIBRARY_ROOT="$TMP/src" LIBRARY_BACKUP_LOCK="$lock" \
        MOCK_FB_RUNNING="${MOCK_FB_RUNNING:-}" MOCK_DOCKER_PS_RC="${MOCK_DOCKER_PS_RC:-0}" \
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
    pass "TC-001 SR-018 a backup that is not complete posts no lastSuccess and ends idle"
else
    fail "TC-001 container failure rc=$RUN_RC lastSuccess=$(success_count "$TMP/container-failure.capture") phases='$(phases "$TMP/container-failure.capture")'"
fi

run_wrapper verify-failure "$TMP/good.env" shallow 0 1 200
if [ "$RUN_RC" != 0 ] && [ "$(success_count "$TMP/verify-failure.capture")" = 0 ] \
   && [ "$(phases "$TMP/verify-failure.capture")" = 'starting backing-up verifying idle ' ] \
   && grep -qF 'verify exit 1 — FINDINGS' "$TMP/verify-failure.out"; then
    pass "TC-001 SR-018 verification findings post no lastSuccess and end idle"
else
    fail "TC-001 verify failure rc=$RUN_RC lastSuccess=$(success_count "$TMP/verify-failure.capture") phases='$(phases "$TMP/verify-failure.capture")'"
fi

# ── the bounded-run-loop backup exit table (TC-1066) ─────────────────────────
# Exit 1 = the run COMPLETED but some files were not captured. Not a verified
# success: no lastSuccess, non-zero exit. But the verify gate still runs, so a
# file that stays locked cannot also starve the weekly proof.
run_wrapper partial "$TMP/good.env" shallow 1 0 200
if [ "$RUN_RC" != 0 ] && [ "$(success_count "$TMP/partial.capture")" = 0 ] \
   && [ "$(tr '\n' ' ' <"$TMP/partial.events")" = 'state state docker:backup state docker:verify state ' ] \
   && [ "$(phases "$TMP/partial.capture")" = 'starting backing-up verifying idle ' ] \
   && grep -qF 'COMPLETED WITH FILES NOT CAPTURED (container exit 1; verify=shallow ok)' "$TMP/partial.out" \
   && grep -qF 'lastSuccess is NOT advanced' "$TMP/partial.out"; then
    pass "TC-1066 backup exit 1 runs the verify gate, advances no lastSuccess, and fails the unit naming the set log"
else
    fail "TC-1066 partial rc=$RUN_RC lastSuccess=$(success_count "$TMP/partial.capture") events=$(tr '\n' ' ' <"$TMP/partial.events")"
fi

# Exit 2 = refused before changing anything; exit 4 = host problem, retry later.
# Neither reaches the verify gate, and each has its own message.
run_wrapper refused "$TMP/good.env" shallow 2 0 200
if [ "$RUN_RC" != 0 ] && [ "$(success_count "$TMP/refused.capture")" = 0 ] \
   && ! grep -q 'docker:verify' "$TMP/refused.events" \
   && grep -qF 'REFUSED the run before changing anything (exit 2)' "$TMP/refused.out" \
   && grep -qF 'retrying will not help' "$TMP/refused.out"; then
    pass "TC-1066 backup exit 2 is a do-not-retry refusal: no verify, no lastSuccess"
else
    fail "TC-1066 exit 2 rc=$RUN_RC events=$(tr '\n' ' ' <"$TMP/refused.events"): $(grep 'ERROR' "$TMP/refused.out" | head -1)"
fi
run_wrapper host-stop "$TMP/good.env" shallow 4 0 200
if [ "$RUN_RC" != 0 ] && [ "$(success_count "$TMP/host-stop.capture")" = 0 ] \
   && ! grep -q 'docker:verify' "$TMP/host-stop.events" \
   && grep -qF 'STOPPED for a host problem (exit 4)' "$TMP/host-stop.out" \
   && grep -qF 'Retry later' "$TMP/host-stop.out"; then
    pass "TC-1066 backup exit 4 is a retry-later host stop: no verify, no lastSuccess"
else
    fail "TC-1066 exit 4 rc=$RUN_RC events=$(tr '\n' ' ' <"$TMP/host-stop.events"): $(grep 'ERROR' "$TMP/host-stop.out" | head -1)"
fi

# A bounded-run-loop image never returns 3 (no witness), from backup or verify.
# A 3 is an OLDER image, and the message must say so rather than "witness".
run_wrapper old-image "$TMP/good.env" none 3 0 200
OLD_IMAGE_RC=$RUN_RC
run_wrapper old-image-verify "$TMP/good.env" shallow 0 3 200
if [ "$OLD_IMAGE_RC" != 0 ] && [ "$RUN_RC" != 0 ] && grep -qF 'OLDER image' "$TMP/old-image.out" \
   && grep -qF 'OLDER image' "$TMP/old-image-verify.out" \
   && [ "$(success_count "$TMP/old-image.capture")" = 0 ] \
   && [ "$(success_count "$TMP/old-image-verify.capture")" = 0 ]; then
    pass "TC-1066 an exit 3 from backup or verify is reported as an older FileBackup image, with no lastSuccess"
else
    fail "TC-1066 exit 3: $(grep 'ERROR' "$TMP/old-image.out" | head -1) | $(grep 'ERROR' "$TMP/old-image-verify.out" | head -1)"
fi

# ── the one-mount store (TC-1067) ────────────────────────────────────────────
# A second mount under the store at changes/ would make FileBackup's rename test
# fail inside the container (exit 2). The wrapper refuses first, before docker.
MOCK_SPLIT_CHANGES=1 run_wrapper split-mount "$TMP/good.env" none 0 0 200
if [ "$RUN_RC" != 0 ] && ! grep -q '^docker:' "$TMP/split-mount.events" \
   && [ "$(success_count "$TMP/split-mount.capture")" = 0 ] \
   && grep -qF "backup/ and changes/ must be ONE mount" "$TMP/split-mount.out"; then
    pass "TC-1067 a store whose changes/ is a separate mount is refused before the container"
else
    fail "TC-1067 split mount rc=$RUN_RC events=$(tr '\n' ' ' <"$TMP/split-mount.events")"
fi

# The compose binding and the container config that go with the wrapper: ONE
# writable /store, no /state, BackupPath/ChangePath under /store, and no
# SourceStatePath (the bounded engine ignores it with a warning every run).
COMPOSE="$ROOT/stack/docker-compose.yml"
FBJSON="$ROOT/stack/backup/filebackup.json"
fb_block="$(awk '/^  filebackup:/{on=1; next} on && /^  [a-z]/{exit} on' "$COMPOSE")"
# shellcheck disable=SC2016  # the ${...} is compose's own interpolation, matched literally
if printf '%s\n' "$fb_block" | grep -qF -- '- ${FILEBACKUP_STORE:-/mnt/backup-drive/library-store}:/store' \
   && ! printf '%s\n' "$fb_block" | grep -Eq -- '^ *- .*:/(state|backup|changes)$' \
   && grep -qF '"BackupPath": "/store/backup"' "$FBJSON" \
   && grep -qF '"ChangePath": "/store/changes"' "$FBJSON" \
   && ! grep -q 'SourceStatePath' "$FBJSON"; then
    pass "TC-1067 compose binds one /store and no /state; filebackup.json keeps both roots under /store"
else
    fail "TC-1067 the compose/config contract drifted: $(printf '%s\n' "$fb_block" | grep -E ':/' | tr '\n' '|')"
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

# ADVERSARIAL REVIEW, 2026-09-08 — one case per accepted finding.

# RESTORE THE LOCK MOCK. The held-lock cases above replaced it with a permanent
# failure, and everything below needs to actually acquire the lock; without this
# they all fail as "another backup is already running", which looks like a
# product defect and is purely harness state.
cat >"$TMP/bin/flock" <<'FLOCKEOF'
#!/usr/bin/env bash
exit 0
FLOCKEOF
chmod +x "$TMP/bin/flock"

# F1: the flock does NOT prove FileBackup stopped. `compose run` containers are
# daemon-owned, so a SIGKILLed wrapper releases the lock while the container
# keeps writing. Reconcile must fail CLOSED on both "still up" and "cannot tell".
MOCK_FB_RUNNING=abc123 run_reconcile reconcile-container "$TMP/reconcile-container.lock"
if [ "$RUN_RC" = 0 ] && [ "$(post_count "$TMP/reconcile-container.capture")" = 0 ]; then
    pass "F1 reconcile leaves the phase alone while a FileBackup container is still running"
else
    fail "F1 reconcile withdrew a phase with a live container: posts=$(post_count "$TMP/reconcile-container.capture")"
fi

MOCK_DOCKER_PS_RC=1 run_reconcile reconcile-unknown "$TMP/reconcile-unknown.lock"
if [ "$RUN_RC" = 0 ] && [ "$(post_count "$TMP/reconcile-unknown.capture")" = 0 ]; then
    pass "F1 reconcile fails closed when it cannot tell whether a container is running"
else
    fail "F1 reconcile withdrew a phase without proof: posts=$(post_count "$TMP/reconcile-unknown.capture")"
fi

# F5: a preflight is a diagnostic, not an attempt. It must claim no phase at all,
# or a SUCCESSFUL preflight strands `starting` on the wall with nothing to
# withdraw it.
: >"$TMP/preflight-only.capture"; : >"$TMP/preflight-only.events"
PATH="$TMP/bin:$PATH" MOUNTINFO_FILE="$TMP/mountinfo" SYS_DEV_BLOCK_DIR="$TMP/sys-dev-block" CAPTURE="$TMP/preflight-only.capture"     EVENTS="$TMP/preflight-only.events" MOCK_HTTP_CODE=200 LIBRARY_ROOT="$TMP/src"     LIBRARY_BACKUP_LOCK="$TMP/preflight-only.lock"     bash "$WRAPPER" --config "$TMP/good.env" --preflight-only >"$TMP/preflight-only.out" 2>&1
PF_RC=$?
if [ "$PF_RC" = 0 ] && [ "$(post_count "$TMP/preflight-only.capture")" = 0 ]; then
    pass "F5 a successful preflight-only run claims no phase and strands nothing"
else
    fail "F5 preflight-only rc=$PF_RC posts=$(post_count "$TMP/preflight-only.capture") phases='$(phases "$TMP/preflight-only.capture")'"
fi

# F2: FILE_SHARE_BACKUP_STATE_LAST_CODE is ONE global, overwritten by every post.
# The never-silent-green check must judge the lastSuccess post, not the idle that
# follows it. Here lastSuccess fails and the later idle succeeds.
cat >"$TMP/bin/curl" <<'CURLEOF'
#!/usr/bin/env bash
body=""
while [ "$#" -gt 0 ]; do
    if [ "$1" = -d ]; then body="$2"; shift 2; continue; fi
    shift
done
printf '%s
' "$body" >>"$CAPTURE"
printf 'state
' >>"$EVENTS"
case "$body" in
    *lastSuccess*) printf '%s' "${MOCK_SUCCESS_CODE:-200}" ;;
    *)             printf '%s' "${MOCK_HTTP_CODE:-200}" ;;
esac
CURLEOF
chmod +x "$TMP/bin/curl"
: >"$TMP/split.capture"; : >"$TMP/split.events"
PATH="$TMP/bin:$PATH" MOUNTINFO_FILE="$TMP/mountinfo" SYS_DEV_BLOCK_DIR="$TMP/sys-dev-block" CAPTURE="$TMP/split.capture"     EVENTS="$TMP/split.events" MOCK_BACKUP_RC=0 MOCK_VERIFY_RC=0     MOCK_HTTP_CODE=200 MOCK_SUCCESS_CODE=500 LIBRARY_ROOT="$TMP/src"     LIBRARY_BACKUP_LOCK="$TMP/split.lock"     bash "$WRAPPER" --config "$TMP/good.env" --verify shallow >"$TMP/split.out" 2>&1
SPLIT_RC=$?
if [ "$SPLIT_RC" != 0 ] && grep -q FATAL "$TMP/split.out"; then
    pass "F2 a failed lastSuccess still fails the unit even when the later idle post succeeds"
else
    fail "F2 a failed lastSuccess was masked by the following idle post (rc=$SPLIT_RC)"
fi

# ── hub recovery C4: the backup slice's exact peak (SR-049) ──────────────────
# Logged after the backup AND after the verify, from the slice that outlives
# each --rm container. A diagnostic: an unreadable slice never fails the run.
mkdir -p "$TMP/slice"
printf '3221225472\n' >"$TMP/slice/memory.peak"
FILEBACKUP_SLICE_CGROUP="$TMP/slice" run_wrapper peak "$TMP/good.env" shallow 0 0 200
if [ "$RUN_RC" = 0 ] \
   && grep -qF 'filebackup slice memory.peak: 3221225472 bytes (3072 MiB) after backup (exit 0)' "$TMP/peak.out" \
   && grep -qF 'memory.peak: 3221225472 bytes (3072 MiB) after shallow verify (exit 0)' "$TMP/peak.out"; then
    pass "TC-1064 the slice's memory.peak is logged after the backup and after the verify"
else
    fail "TC-1064 slice peak not logged (rc=$RUN_RC): $(grep 'memory.peak' "$TMP/peak.out" | tr '\n' '|')"
fi
FILEBACKUP_SLICE_CGROUP="$TMP/no-such-slice" run_wrapper nopeak "$TMP/good.env" none 0 0 200
if [ "$RUN_RC" = 0 ] && grep -qF 'filebackup slice memory.peak: unavailable' "$TMP/nopeak.out"; then
    pass "TC-1064 an unreadable slice is reported and does not fail the run"
else
    fail "TC-1064 an unreadable slice changed the run (rc=$RUN_RC)"
fi

# ── hub recovery §8: an old engine's staging is a NAMED note, not a refusal ───
# The bounded engine never uses Temp and leaves an old one alone, so refusing
# here would block every backup forever. The note still names the recovery
# event: Temp predates two recovery events; the one that names the backup is
# chosen over the later one that does not, and an event older than Temp is
# ignored by its MTIME even though its name sorts last.
mkdir -p "$TMP/store/changes/Temp" "$TMP/events"
printf 'staged\n' >"$TMP/store/changes/Temp/only-copy"
touch -d '2026-10-01 00:00:00' "$TMP/store/changes/Temp"
printf '{"action": "kill", "details": {"interrupts_backup": true}}\n' >"$TMP/events/20261005T010203Z-bbbbbbbb-kill.json"
printf '{"action": "restart-ssh", "details": {}}\n' >"$TMP/events/20261005T020000Z-bbbbbbbb-restart-ssh.json"
printf '{"action": "reboot", "details": {"interrupts_backup": true}}\n' >"$TMP/events/20261009T000000Z-aaaaaaaa-reboot.json"
touch -d '2026-09-01 00:00:00' "$TMP/events/20261009T000000Z-aaaaaaaa-reboot.json"
RECOVERY_EVENTS_DIR="$TMP/events" run_wrapper staging "$TMP/good.env" shallow 0 0 200
if [ "$RUN_RC" = 0 ] \
   && grep -qF 'LIBRARY BACKUP NOTE: old staging needs recovery by hand (interrupted by recovery event 20261005T010203Z-bbbbbbbb-kill); the backup runs anyway' "$TMP/staging.out" \
   && grep -q '^docker:backup$' "$TMP/staging.events" \
   && [ "$(success_count "$TMP/staging.capture")" = 1 ] \
   && grep -qF 'DEGRADED: old staging' "$TMP/staging.out" \
   && [ "$(cat "$TMP/store/changes/Temp/only-copy")" = staged ]; then
    pass "TC-1065 an old Temp is a note naming the recovery event that named the backup; the run goes on and Temp is untouched"
else
    fail "TC-1065 staging note rc=$RUN_RC events=$(tr '\n' ' ' <"$TMP/staging.events"): $(grep 'staging' "$TMP/staging.out" | head -2 | tr '\n' '|')"
fi
RECOVERY_EVENTS_DIR="$TMP/no-events" run_wrapper staging2 "$TMP/good.env" shallow 0 0 200
if [ "$RUN_RC" = 0 ] \
   && grep -qF 'LIBRARY BACKUP NOTE: old staging needs recovery by hand (interrupted by recovery event none recorded)' "$TMP/staging2.out"; then
    pass "TC-1065 with no recovery event recorded the note says so rather than inventing one"
else
    fail "TC-1065 no-event note rc=$RUN_RC: $(grep 'staging' "$TMP/staging2.out" | head -1)"
fi
rm -rf "$TMP/store/changes/Temp"

# ── the store-layout guard (TC-1068) ─────────────────────────────────────────
# An unmigrated three-mount store that still holds data refuses before anything
# is created or started, names the migration step, and is left exactly as it is.
mkdir -p "$TMP/old-backup" "$TMP/old-changes/Snapshot_2026_10_01_03_00_00"
printf 'row\n' >"$TMP/old-backup/MANIFEST.csv"
run_wrapper unmigrated "$TMP/good.env" none 0 0 200
if [ "$RUN_RC" != 0 ] && ! grep -q '^docker:' "$TMP/unmigrated.events" \
   && [ "$(success_count "$TMP/unmigrated.capture")" = 0 ] \
   && [ "$(phases "$TMP/unmigrated.capture")" = 'starting idle ' ] \
   && grep -qF 'has NOT been migrated to the one-mount layout' "$TMP/unmigrated.out" \
   && grep -qF 'library-store-migrate.sh --apply' "$TMP/unmigrated.out" \
   && [ "$(cat "$TMP/old-backup/MANIFEST.csv")" = row ] \
   && [ -d "$TMP/old-changes/Snapshot_2026_10_01_03_00_00" ]; then
    pass "TC-1068 an unmigrated store refuses before docker, names the migration step, and is untouched"
else
    fail "TC-1068 unmigrated rc=$RUN_RC events=$(tr '\n' ' ' <"$TMP/unmigrated.events"): $(grep 'migrated' "$TMP/unmigrated.out" | head -1)"
fi
# The same guard with only the old CHANGES folder left (a migration stopped
# between its two renames) still refuses.
rm -rf "$TMP/old-backup"
run_wrapper half-migrated "$TMP/good.env" none 0 0 200
if [ "$RUN_RC" != 0 ] && ! grep -q '^docker:' "$TMP/half-migrated.events" \
   && grep -qF "$TMP/old-changes still holds" "$TMP/half-migrated.out"; then
    pass "TC-1068 a half-migrated store (old changes/ left) still refuses"
else
    fail "TC-1068 half-migrated rc=$RUN_RC: $(grep 'migrated' "$TMP/half-migrated.out" | head -1)"
fi
# EMPTY old folders hold nothing to migrate: a note, and the run goes on.
rm -rf "$TMP/old-changes"; mkdir -p "$TMP/old-backup"
run_wrapper empty-legacy "$TMP/good.env" none 0 0 200
if [ "$RUN_RC" = 0 ] && [ "$(success_count "$TMP/empty-legacy.capture")" = 1 ] \
   && grep -qF "an EMPTY folder from the old store layout is still at $TMP/old-backup" "$TMP/empty-legacy.out"; then
    pass "TC-1068 an empty old folder is a note and the run goes on"
else
    fail "TC-1068 empty legacy rc=$RUN_RC: $(grep -i 'old store\|migrated' "$TMP/empty-legacy.out" | head -1)"
fi
rmdir "$TMP/old-backup"

# The retired source-side record: a verified run says once that it can be
# deleted by hand, and deletes nothing itself.
printf 'old\n' >"$TMP/state/MANIFEST.csv"
run_wrapper retired-state "$TMP/good.env" none 0 0 200
if [ "$RUN_RC" = 0 ] && grep -qF "$TMP/state/MANIFEST.csv is the retired source-side record" "$TMP/retired-state.out" \
   && [ "$(cat "$TMP/state/MANIFEST.csv")" = old ]; then
    pass "TC-1068 a verified run names the retired source-side record for deletion by hand and keeps it"
else
    fail "TC-1068 retired state rc=$RUN_RC: $(grep 'retired' "$TMP/retired-state.out" | head -1)"
fi
rm -f "$TMP/state/MANIFEST.csv"

# ── a documented dry run must really be one (TC-1067) ────────────────────────
# `FILEBACKUP_DRY_RUN=1 docker compose … run` sets the variable on the HOST and
# compose does not pass it into the container (measured in WSL, docker compose
# v5.3.0: the container saw it unset), so that form runs the REAL prune. Every
# FILEBACKUP_* switch the operator is told to use must go through `-e`.
# Comment lines are skipped: the README's own warning quotes the bad form.
_bad_forms="$(grep -n 'FILEBACKUP_[A-Z_]*=[^ ]* *docker compose' "$WRAPPER" "$ROOT/stack/backup/README.md" 2>/dev/null \
              | grep -vE '^[^:]+:[0-9]+:[[:space:]]*#' || true)"
if [ -z "$_bad_forms" ] \
   && grep -qF -- '-e FILEBACKUP_DRY_RUN=1 filebackup' "$ROOT/stack/backup/README.md" \
   && grep -qF -- '-e FILEBACKUP_DRY_RUN=1 filebackup' "$WRAPPER"; then
    pass "TC-1067 every documented FILEBACKUP_* switch reaches the container through -e"
else
    fail "TC-1067 a host-variable form that never reaches the container is documented: $_bad_forms"
fi

# ── the system-disk manifest copy for frame-freshness (TC-1070) ──────────────
# Refreshed after a COMPLETED run (exit 0 or 1), whole (published by rename);
# left as it was after a run that did not complete; a copy that cannot be made
# is a note, never a backup failure.
printf 'v-complete\n' >"$TMP/store/backup/MANIFEST.csv"
run_wrapper copy-complete "$TMP/good.env" none 0 0 200
c1="$(cat "$TMP/latest/MANIFEST.csv" 2>/dev/null)"
printf 'v-partial\n' >"$TMP/store/backup/MANIFEST.csv"
run_wrapper copy-partial "$TMP/good.env" none 1 0 200
c2="$(cat "$TMP/latest/MANIFEST.csv" 2>/dev/null)"
printf 'v-refused\n' >"$TMP/store/backup/MANIFEST.csv"
run_wrapper copy-refused "$TMP/good.env" none 2 0 200
c3="$(cat "$TMP/latest/MANIFEST.csv" 2>/dev/null)"
if [ "$c1" = v-complete ] && [ "$c2" = v-partial ] && [ "$c3" = v-partial ] \
   && ! find "$TMP/latest" -name '.MANIFEST.csv.*' | grep -q .; then
    pass "TC-1070 the manifest copy is refreshed after exit 0 and 1, kept after exit 2, and leaves no temp file"
else
    fail "TC-1070 manifest copy after 0/1/2 was '$c1'/'$c2'/'$c3'"
fi
# A copy path whose parent is a FILE cannot be created: a note, and the run
# still succeeds and still posts its lastSuccess.
: >"$TMP/not-a-dir"
write_config "$TMP/badcopy.env" "$TMP/not-a-dir/MANIFEST.csv"
run_wrapper copy-fails "$TMP/badcopy.env" none 0 0 200
if [ "$RUN_RC" = 0 ] && [ "$(success_count "$TMP/copy-fails.capture")" = 1 ] \
   && grep -qF "DEGRADED: could not refresh $TMP/not-a-dir/MANIFEST.csv for frame-freshness" "$TMP/copy-fails.out"; then
    pass "TC-1070 a manifest copy that cannot be made is a DEGRADED note, not a failed backup"
else
    fail "TC-1070 failed copy rc=$RUN_RC: $(grep 'manifest copy' "$TMP/copy-fails.out" | head -1)"
fi

printf '%s PASS  %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
