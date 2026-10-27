#!/usr/bin/env bash
# SR-133 — the store-layout knob, FILEBACKUP_LAYOUT (legacy | single).
#
# A stack delivery must change nothing on a hub still running the kit-12 image
# on the three-mount store. These cases pin what makes that true:
#   TC-1583  the dispatcher: unset and `legacy` hand the run, arguments
#            unchanged, to library-backup-legacy.sh and post nothing; `single`
#            does not; anything else refuses before step 1. The knob is read
#            from stack/.env, where the materialiser writes it.
#   TC-1584  library-backup-legacy.sh is the integration branch's wrapper as it
#            was before the single-store layout merged, byte for byte (pinned).
#            (TC-1587, library-backup-legacy.test.sh, runs that branch's own
#            suite against it through the dispatcher's default.)
#   TC-1585  compose: `filebackup` keeps the three mounts and filebackup.json
#            stays ConfigVersion 2; `filebackup-store` is the single-store
#            service (ConfigVersion 2, BackupPath /store/backup, ChangePath
#            /store/changes: BoundedRebuild as it is); both stay behind the `filebackup` profile. (TC-1588,
#            tests/test_filebackup_layouts.py, holds the rest of their keys equal.)
#   TC-1586  frame-freshness reads the NEWER of the single-store copy and the
#            legacy /state record, and keeps the legacy row-count witness
#            check whenever a .meta sidecar is beside the file it reads.
#   TC-1591  --layout overrides the knob for a --preflight-only (the cutover
#            dry check) and is refused on a real run; a stack/.env that exists
#            but cannot be read stops the run rather than guess legacy.
# Hermetic: a temp tree, a stub legacy wrapper and a refused probe port.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$*"; }

TMP="$(mktemp -d)"
cleanup() { [ "${KEEP_TMP:-0}" = 1 ] && { printf 'tmp kept: %s\n' "$TMP"; return; }; rm -rf "$TMP"; }
trap cleanup EXIT

# ── TC-1583: the dispatcher ──────────────────────────────────────────────────
# A copy of the real wrapper and common.sh in a tree of their own, so its
# stack/.env is ours and the legacy wrapper beside it is a stub that records the
# arguments it was handed. The single path is stopped at step 1 by a refusing
# flock: far enough to prove it did not hand off.
STK="$TMP/stack"
mkdir -p "$STK/backup" "$TMP/bin"
cp "$ROOT/stack/backup/library-backup.sh" "$ROOT/stack/backup/common.sh" "$STK/backup/"
cat >"$STK/backup/library-backup-legacy.sh" <<'EOF'
#!/usr/bin/env bash
printf 'LEGACY-STUB argc=%s' "$#" >>"$STUB_LOG"
for a in "$@"; do printf ' [%s]' "$a" >>"$STUB_LOG"; done
printf '\n' >>"$STUB_LOG"
exit 42
EOF
cat >"$TMP/bin/flock" <<'EOF'
#!/usr/bin/env bash
printf 'flock %s\n' "$*" >>"$CALLS"; exit 1
EOF
cat >"$TMP/bin/docker" <<'EOF'
#!/usr/bin/env bash
printf 'docker %s\n' "$*" >>"$CALLS"; exit 97
EOF
cat >"$TMP/bin/curl" <<'EOF'
#!/usr/bin/env bash
printf 'curl %s\n' "$*" >>"$CALLS"; printf 200
EOF
chmod +x "$TMP/bin"/*
cat >"$TMP/backup.env" <<EOF
LIBRARY_BACKUP_LOCK="$TMP/lock"
FILE_SHARE_BACKUP_STATE_URL=http://mock.invalid/api/backup-state
EOF

run_dispatch() {   # NAME LAYOUT-LINE ARGS...
    local name="$1" line="$2"; shift 2
    if [ -n "$line" ]; then printf '%s\n' "$line" >"$STK/.env"; else rm -f "$STK/.env"; fi
    : >"$TMP/$name.stub"; : >"$TMP/$name.calls"
    # env -u: a FILEBACKUP_LAYOUT exported in the caller's shell must not stand
    # in for the one under test (it would hide a broken default).
    env -u FILEBACKUP_LAYOUT PATH="$TMP/bin:$PATH" STUB_LOG="$TMP/$name.stub" CALLS="$TMP/$name.calls" \
        bash "$STK/backup/library-backup.sh" --config "$TMP/backup.env" "$@" >"$TMP/$name.out" 2>&1
    RC=$?
}

run_dispatch unset "" --verify none --preflight-only
if [ "$RC" = 42 ] \
   && [ "$(cat "$TMP/unset.stub")" = "LEGACY-STUB argc=5 [--config] [$TMP/backup.env] [--verify] [none] [--preflight-only]" ] \
   && [ ! -s "$TMP/unset.calls" ] \
   && grep -qF 'FILEBACKUP_LAYOUT=legacy - the three-mount layout; handing the run to library-backup-legacy.sh' "$TMP/unset.out"; then
    pass "TC-1583 an unset FILEBACKUP_LAYOUT hands the run to the legacy wrapper with its arguments unchanged and posts nothing"
else
    fail "TC-1583 unset layout rc=$RC stub='$(cat "$TMP/unset.stub")' calls='$(cat "$TMP/unset.calls")'"
fi

run_dispatch legacy "FILEBACKUP_LAYOUT=legacy" --reconcile-run-state
if [ "$RC" = 42 ] && [ "$(cat "$TMP/legacy.stub")" = "LEGACY-STUB argc=3 [--config] [$TMP/backup.env] [--reconcile-run-state]" ] \
   && [ ! -s "$TMP/legacy.calls" ]; then
    pass "TC-1583 FILEBACKUP_LAYOUT=legacy in stack/.env hands the unit's ExecStopPost reconcile to the legacy wrapper too"
else
    fail "TC-1583 legacy layout rc=$RC stub='$(cat "$TMP/legacy.stub")' calls='$(cat "$TMP/legacy.calls")'"
fi

run_dispatch single "FILEBACKUP_LAYOUT=single" --verify none
if [ "$RC" = 1 ] && [ ! -s "$TMP/single.stub" ] \
   && [ "$(cut -d' ' -f1 "$TMP/single.calls" | tr '\n' ' ')" = "flock " ] \
   && grep -qF 'ANOTHER LIBRARY BACKUP IS ALREADY RUNNING' "$TMP/single.out" \
   && ! grep -qF 'handing the run' "$TMP/single.out"; then
    pass "TC-1583 FILEBACKUP_LAYOUT=single runs the single-store wrapper itself (stopped here at its step-1 lock)"
else
    fail "TC-1583 single layout rc=$RC stub='$(cat "$TMP/single.stub")' out: $(tr '\n' '|' <"$TMP/single.out" | cut -c1-300)"
fi

run_dispatch bogus "FILEBACKUP_LAYOUT=three-mount" --verify none
if [ "$RC" != 0 ] && [ "$RC" != 42 ] && [ ! -s "$TMP/bogus.stub" ] && [ ! -s "$TMP/bogus.calls" ] \
   && grep -qF "FILEBACKUP_LAYOUT='three-mount' is neither legacy nor single" "$TMP/bogus.out" \
   && ! grep -qF 'ANOTHER LIBRARY BACKUP' "$TMP/bogus.out"; then
    pass "TC-1583 an unknown FILEBACKUP_LAYOUT refuses before step 1, starts nothing and posts nothing"
else
    fail "TC-1583 unknown layout rc=$RC calls='$(cat "$TMP/bogus.calls")' out: $(tr '\n' '|' <"$TMP/bogus.out" | cut -c1-300)"
fi

# ── TC-1591: --layout, the cutover's dry check, and an unreadable .env ───────
run_dispatch override-run "FILEBACKUP_LAYOUT=legacy" --layout single --verify none
if [ "$RC" != 0 ] && [ "$RC" != 42 ] && [ ! -s "$TMP/override-run.stub" ] && [ ! -s "$TMP/override-run.calls" ] \
   && grep -qF -- '--layout is only for --preflight-only' "$TMP/override-run.out"; then
    pass "TC-1591 --layout without --preflight-only refuses, starting and posting nothing"
else
    fail "TC-1591 --layout on a real run rc=$RC calls='$(cat "$TMP/override-run.calls")'"
fi
run_dispatch override-single "FILEBACKUP_LAYOUT=legacy" --layout single --preflight-only
if [ "$RC" = 1 ] && [ ! -s "$TMP/override-single.stub" ] \
   && [ "$(cut -d' ' -f1 "$TMP/override-single.calls" | tr '\n' ' ')" = "flock " ]; then
    pass "TC-1591 --layout single --preflight-only checks the one-mount layout over a legacy knob"
else
    fail "TC-1591 --layout single preflight rc=$RC stub='$(cat "$TMP/override-single.stub")'"
fi
run_dispatch override-legacy "FILEBACKUP_LAYOUT=single" --preflight-only --layout legacy
if [ "$RC" = 42 ] && [ "$(cat "$TMP/override-legacy.stub")" = "LEGACY-STUB argc=3 [--config] [$TMP/backup.env] [--preflight-only]" ]; then
    pass "TC-1591 --layout legacy --preflight-only hands off without passing --layout to the legacy wrapper"
else
    fail "TC-1591 --layout legacy rc=$RC stub='$(cat "$TMP/override-legacy.stub")'"
fi
if [ "$(id -u)" = 0 ]; then
    printf 'SKIP  TC-1591 an unreadable stack/.env (root reads everything)\n'
else
    printf 'FILEBACKUP_LAYOUT=single\n' >"$STK/.env"
    chmod 000 "$STK/.env"
    : >"$TMP/unreadable.stub"; : >"$TMP/unreadable.calls"
    env -u FILEBACKUP_LAYOUT PATH="$TMP/bin:$PATH" STUB_LOG="$TMP/unreadable.stub" CALLS="$TMP/unreadable.calls" \
        bash "$STK/backup/library-backup.sh" --config "$TMP/backup.env" --preflight-only >"$TMP/unreadable.out" 2>&1
    RC=$?; chmod 644 "$STK/.env"
    if [ "$RC" != 0 ] && [ "$RC" != 42 ] && [ ! -s "$TMP/unreadable.stub" ] && [ ! -s "$TMP/unreadable.calls" ] \
       && grep -qF 'exists but cannot be read' "$TMP/unreadable.out"; then
        pass "TC-1591 a stack/.env that exists but cannot be read stops the run instead of guessing legacy"
    else
        fail "TC-1591 unreadable .env rc=$RC stub='$(cat "$TMP/unreadable.stub")': $(tail -1 "$TMP/unreadable.out")"
    fi
fi

# ── TC-1584: the legacy wrapper is frozen ────────────────────────────────────
# The sha256 of IceDrive-DesktopDirection's stack/backup/library-backup.sh at
# 8d87d0b, the last commit before the single-store layout merged. A change to
# the legacy wrapper is a change to what the deployed hub runs: make it on
# purpose, here and in the file together.
LEGACY_SHA256=c84918815e8e4bb219e020b8d61e60c5416223bb8c1b6ad8cb4749f4686c61be
_got="$(sha256sum "$ROOT/stack/backup/library-backup-legacy.sh" | cut -d' ' -f1)"
if [ "$_got" = "$LEGACY_SHA256" ]; then
    pass "TC-1584 library-backup-legacy.sh is byte for byte the pre-merge wrapper"
else
    fail "TC-1584 library-backup-legacy.sh changed: sha256 $_got, pinned $LEGACY_SHA256"
fi

# ── TC-1585: the two compose services and the two configs ────────────────────
COMPOSE="$ROOT/stack/docker-compose.yml"
svc_block() { awk -v s="  $1:" '$0==s{on=1; next} on && /^  [a-z#]/{exit} on' "$COMPOSE"; }
legacy_block="$(svc_block filebackup)"
store_block="$(svc_block filebackup-store)"
_mounts() { printf '%s\n' "$1" | grep -E '^ *- [^#]*:/[A-Za-z./]+(:ro)?$' | sed 's/^ *- //'; }
# shellcheck disable=SC2016  # compose's own ${...} interpolation, matched literally
_want_legacy='/etc/homehub-backup/filebackup.json:/config/FileBackup.json:ro
${FILEBACKUP_SOURCE:-/srv/library}:/source:ro
${FILEBACKUP_STATE:-/var/lib/homehub-filebackup/state}:/state
${FILEBACKUP_BACKUP:-/mnt/backup-drive/library}:/backup
${FILEBACKUP_CHANGES:-/mnt/backup-drive/library-changes}:/changes
${FILEBACKUP_LOGS:-/var/log/homehub-filebackup}:/logs'
# shellcheck disable=SC2016
_want_store='/etc/homehub-backup/filebackup-store.json:/config/FileBackup.json:ro
${FILEBACKUP_SOURCE:-/srv/library}:/source:ro
${FILEBACKUP_STORE:-/mnt/backup-drive/library-store}:/store
${FILEBACKUP_LOGS:-/var/log/homehub-filebackup}:/logs'
FBJ="$ROOT/stack/backup/filebackup.json"; FBS="$ROOT/stack/backup/filebackup-store.json"
if [ "$(_mounts "$legacy_block")" = "$_want_legacy" ] \
   && [ "$(_mounts "$store_block")" = "$_want_store" ] \
   && printf '%s\n' "$legacy_block" | grep -qF 'profiles: ["filebackup"]' \
   && printf '%s\n' "$store_block"  | grep -qF 'profiles: ["filebackup"]' \
   && grep -qF '"ConfigVersion": 2,' "$FBJ" && grep -qF '"BackupPath": "/backup",' "$FBJ" \
   && grep -qF '"ChangePath": "/changes",' "$FBJ" && ! grep -qF StorePath "$FBJ" \
   && grep -qF '"ConfigVersion": 2,' "$FBS" && grep -qF '"BackupPath": "/store/backup",' "$FBS" \
   && grep -qF '"ChangePath": "/store/changes",' "$FBS" && ! grep -qE 'StorePath|SourceStatePath' "$FBS"; then
    pass "TC-1585 filebackup keeps the three-mount binds and a ConfigVersion 2 config; filebackup-store binds one /store with a ConfigVersion 2 config naming /store/backup and /store/changes"
else
    fail "TC-1585 the layout services drifted: legacy=[$(_mounts "$legacy_block" | tr '\n' '|')] store=[$(_mounts "$store_block" | tr '\n' '|')]"
fi

# ── TC-1586: frame-freshness reads the newer of the two manifests ────────────
# --check mode (no report), a probe port nothing listens on (the cached probe
# answer stands), and a conf pointing both candidates into the temp tree. Which
# file was read shows in the state file's shared_newest_epoch.
FF="$ROOT/stack/tracker/frame-freshness.sh"
FFD="$TMP/ff"; mkdir -p "$FFD/state" "$FFD/latest" "$FFD/run"
NOW="$(date -u +%s)"
printf 'probe_epoch=%s\nframe_newest_epoch=%s\nframe_files=3\n' "$NOW" "$NOW" >"$FFD/run/frame-probe.state"
manifest() {  # FILE DATE
    printf 'RelativePath,LastWriteTimeStr\nShared/a.jpg,%sT00:00:00Z\nShared/b.mp4,2020-01-01T00:00:00Z\n' "$2" >"$1"
}
cat >"$FFD/frame.env" <<EOF
FRAME_MANIFEST_COPY=$FFD/latest/MANIFEST.csv
FRAME_MANIFEST_LEGACY=$FFD/state/MANIFEST.csv
FRAME_PROBE_HOST=127.0.0.1
FRAME_PROBE_PORT=9
FRAME_PROBE_TIMEOUT=2
EOF
run_ff() {
    rm -f "$FFD/run/frame-freshness.state"
    FRAME_CONF_FILE="$FFD/frame.env" FRAME_STATE_DIR="$FFD/run" FRAME_ENV_FILE="$FFD/none.env" \
        bash "$FF" --check >"$FFD/out" 2>&1
    sed -n 's/^shared_newest_epoch=//p' "$FFD/run/frame-freshness.state" 2>/dev/null
}
epoch_of() { date -u -d "$1T00:00:00Z" +%s; }

if ! command -v python3 >/dev/null 2>&1; then
    printf 'SKIP  TC-1586 python3 is not on PATH (frame-freshness parses the manifest with it)\n'
else
    # A legacy hub: no copy. The /state record is read, witness and all.
    manifest "$FFD/state/MANIFEST.csv" 2026-09-01
    printf 'Rows=2\nWritten=2026-10-05T01:02:03\n' >"$FFD/state/MANIFEST.csv.meta"
    touch -d '2026-10-05 01:02:03' "$FFD/state/MANIFEST.csv"
    _a="$(run_ff)"
    # The witness: a .meta that declares more rows than the file holds.
    printf 'Rows=9\nWritten=2026-10-05T01:02:03\n' >"$FFD/state/MANIFEST.csv.meta"
    touch -d '2026-10-05 01:02:03' "$FFD/state/MANIFEST.csv"
    _b="$(run_ff)"; _b_out="$(cat "$FFD/out")"
    printf 'Rows=2\nWritten=2026-10-05T01:02:03\n' >"$FFD/state/MANIFEST.csv.meta"
    # After the cutover: the copy is written later, so it wins.
    manifest "$FFD/latest/MANIFEST.csv" 2026-10-20
    touch -d '2026-10-28 03:00:00' "$FFD/latest/MANIFEST.csv"
    _c="$(run_ff)"
    # A rollback: a legacy run rewrites /state after the copy.
    touch -d '2026-11-02 03:00:00' "$FFD/state/MANIFEST.csv"
    _d="$(run_ff)"
    if [ "$_a" = "$(epoch_of 2026-09-01)" ] && [ -z "$_b" ] \
       && printf '%s' "$_b_out" | grep -qF 'declares 9 row(s) but 2 were read' \
       && [ "$_c" = "$(epoch_of 2026-10-20)" ] && [ "$_d" = "$(epoch_of 2026-09-01)" ]; then
        pass "TC-1586 frame-freshness reads the legacy record with its witness until the copy is newer, and the legacy record again after a rollback"
    else
        fail "TC-1586 manifest choice: legacy=$_a witness='$_b' copy=$_c rollback=$_d; out: $(tr '\n' '|' <"$FFD/out" | cut -c1-300)"
    fi
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ]
