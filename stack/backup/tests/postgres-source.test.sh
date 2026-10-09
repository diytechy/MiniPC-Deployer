#!/usr/bin/env bash
# Logical ledger source through the existing archive pipeline, using mock Docker.
# No database/container/drive/network; fixtures contain no household data.
# Implements: SR-065, LLR-1045. Verifies: TC-1112.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP="$HERE/../backup.sh"
for tool in rsync tar zstd sha256sum; do
    command -v "$tool" >/dev/null || { echo "missing tool: $tool"; exit 2; }
done
TEMP_ROOT="$(mktemp -d)"
trap 'rm -rf -- "$TEMP_ROOT"' EXIT
mkdir -p "$TEMP_ROOT/bin" "$TEMP_ROOT/drive"
PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$*"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s\n' "$*"; }
cat >"$TEMP_ROOT/bin/docker" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$DOCKER_CALL_LOG"
[ "$1" = exec ] || exit 9
if [ "$2" = -i ]; then
    [ "$3" = sure-postgres ] && [ "$4" = pg_restore ] || exit 9
    cat >/dev/null
    exit 0
fi
[ "$2" = sure-postgres ] && [ "$3" = sh ] || exit 9
[ "$DUMP_MODE" = good ] || { printf 'partial'; exit 1; }
printf 'PGDMPsynthetic-ledger-fixture'
SH
chmod +x "$TEMP_ROOT/bin/docker"
export PATH="$TEMP_ROOT/bin:$PATH"
export DOCKER_CALL_LOG="$TEMP_ROOT/docker.calls" DUMP_MODE=good
cat >"$TEMP_ROOT/backup.env" <<ENV
BACKUP_TARGET=$TEMP_ROOT/drive
BACKUP_LAYOUT=fib
BACKUP_KEEP=2
BACKUP_STAGING=$TEMP_ROOT/staging
BACKUP_TARGET_REQUIRE_MOUNT=false
BACKUP_DRIVE_DEVICES=""
NAGLIGHT_FEED_URL=""
BACKUP_WAKE_MAC=""
INGEST_SOURCES=""
BACKUP_SOURCES="sure-ledger=postgres:sure-postgres"
ENV
if bash "$BACKUP" --config "$TEMP_ROOT/backup.env" >"$TEMP_ROOT/good.log" 2>&1; then
    pass 'logical dump joins normal archive pipeline'
else
    fail 'logical dump joins normal archive pipeline'; cat "$TEMP_ROOT/good.log"
fi
archive="$(find "$TEMP_ROOT/drive" -name 'sure-ledger.tar*' -print -quit)"
if [ -n "$archive" ] && grep -q $'sure-ledger\tpostgres:sure-postgres' "${archive%/*}/MANIFEST.tsv"; then
    pass 'archive is recorded with typed source in manifest'
else
    fail 'archive and typed manifest source'
fi
saved_hash="$(sha256sum "$archive" 2>/dev/null || true)"
# A failed second run on the same day must preserve that day's good copy.
export DUMP_MODE=failed
if bash "$BACKUP" --config "$TEMP_ROOT/backup.env" >"$TEMP_ROOT/failure.log" 2>&1; then
    fail 'partial dump must make run fail'
else
    pass 'partial dump makes run fail'
fi
if [ -n "$archive" ] && [ "$saved_hash" = "$(sha256sum "$archive")" ]; then
    pass 'failed run preserves previous good archive'
else
    fail 'previous good archive preserved'
fi
if grep -q 'logical Postgres dump failed' "$TEMP_ROOT/failure.log"; then
    pass 'failure is reported loudly'
else
    fail 'failure reporting'
fi
before="$(wc -l <"$DOCKER_CALL_LOG")"
if bash "$BACKUP" --config "$TEMP_ROOT/backup.env" --plan >"$TEMP_ROOT/plan.log" 2>&1 \
    && [ "$before" = "$(wc -l <"$DOCKER_CALL_LOG")" ]; then
    pass 'plan never invokes Docker'
else
    fail 'plan never invokes Docker'; cat "$TEMP_ROOT/plan.log"
fi
printf '%s PASS %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ]
