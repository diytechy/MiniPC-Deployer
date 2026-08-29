#!/usr/bin/env bash
# restore-volumes.test.sh — the reimage restore path, executed rather than reviewed.
#
# THE REASON THIS FILE EXISTS is C22. The caddy_data restore was written months
# before it ever ran, looked correct to every reader, and could not fire on the
# install it was written for. It cost five re-issued certificates, 122
# rate-limit refusals, and an apex certificate that landed 2h40m after boot.
# Generalising that step to four volumes without making it executable in a test
# would be repeating the mistake at four times the scale.
#
# HERMETIC. A mock `docker` on PATH (the technique run-drivepower-sim.sh uses for
# hdparm) answers `compose create` and `volume inspect`; the "volumes" are temp
# directories. Needs bash, tar, zstd, sha256sum, rsync — no docker, no daemon.
#
# WHAT IT LOCKS
#   R1  a real archive run restores into an EMPTY volume, byte-verified
#   R2  four sets restore independently in one pass
#   R3  a set with no archive on the drive is skipped, and says so
#   R4  a NON-EMPTY volume is left alone — this is not a fresh install
#   R5  one set failing does not stop the others (the single-set shape's `exit 0`
#       on every miss would have let a missing tracker cost caddy its certs)
#   R6  a set whose compose service cannot be created is skipped, not fatal
#   R7  a CORRUPTED archive fails the restore AND empties the volume again —
#       a half-filled volume would make every later boot decline as "not empty"
#   R8  a malformed table row is reported, never silently ignored
#   R9  a plan_<ts> directory is never chosen as a restore source
#   R10 the script always exits 0; the log is the verdict
#   R11 IMAGE SEED DATA in a volume this step just created is cleared, not
#       obeyed - docker seeds a new named volume from the image, so testing
#       emptiness AFTER compose create would decline on every fresh install
#   R12 a volume that ALREADY existed and is non-empty is left alone
#   R13 a cleanup that failed once leaves a durable note, so the next boot
#       reports a PARTIAL volume instead of "not empty, not a fresh install"
#
# Usage: bash restore-volumes.test.sh [--keep-tmp]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROV="$(cd "$HERE/.." && pwd)"
STACK="$(cd "$PROV/.." && pwd)"
SUT="$PROV/restore-volumes.sh"
BACKUP_SH="$STACK/backup/backup.sh"
[ -f "$SUT" ] || { echo "FATAL: $SUT not found"; exit 2; }
[ -f "$BACKUP_SH" ] || { echo "FATAL: $BACKUP_SH not found"; exit 2; }
for t in rsync tar zstd sha256sum; do
    command -v "$t" >/dev/null 2>&1 || { echo "FATAL: '$t' is required and not on PATH"; exit 2; }
done
KEEP_TMP=0
[ "${1:-}" = "--keep-tmp" ] && KEEP_TMP=1

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }

TMP="$(mktemp -d)"
cleanup() { if [ "$KEEP_TMP" = 1 ]; then echo "tmp kept: $TMP"; return 0; fi; rm -rf "$TMP"; }
trap cleanup EXIT

VOLROOT="$TMP/volumes"; DRIVE="$TMP/drive"; MOCKBIN="$TMP/mockbin"; FAILDIR="$TMP/faildir"
mkdir -p "$VOLROOT" "$DRIVE" "$MOCKBIN" "$FAILDIR"

# ── the mock docker ──────────────────────────────────────────────────────────
# It has to answer THREE things, because common.sh's volume_mountpoint asks
# three (and the three are the reason it exists):
#   compose create SVC                    -> make the volume directory
#   volume inspect -f {{.Mountpoint}} N   -> the path, or fail if there is none
#   volume ls -q --filter label=com.docker.compose.volume=N
#                                         -> the PREFIXED real name
# The third is the interesting one: `volumes: caddy_data:` in the compose file
# is really `stack_caddy_data` on disk, and volume_mountpoint resolves that
# through the compose LABEL rather than by guessing the prefix. A mock that only
# answered the literal inspect would make this test pass against a resolver that
# had lost the label lookup entirely.
# MOCK_COMPOSE_FAIL names a service whose create must fail, so R6 exercises that
# branch rather than asserting it exists.
cat >"$MOCKBIN/docker" <<'MOCK'
#!/usr/bin/env bash
VOLROOT="${MOCK_VOLROOT:?}"
case "$1 ${2:-}" in
  "compose create")
    svc="${3:-}"
    [ "$svc" = "${MOCK_COMPOSE_FAIL:-}" ] && exit 1
    vol="$(grep -E "^$svc=" "$VOLROOT/.svcmap" 2>/dev/null | cut -d= -f2)"
    if [ -n "$vol" ] && [ ! -d "$VOLROOT/stack_$vol" ]; then
      mkdir -p "$VOLROOT/stack_$vol"
      # Docker seeds a NEWLY created named volume from whatever the image holds
      # at the mount path. The flag file makes the mock do the same, so R11 can
      # exercise the branch that clears it.
      [ -e "$VOLROOT/.seed-on-create" ] && printf 'from the image\n' >"$VOLROOT/stack_$vol/IMAGE-SEED"
    fi
    exit 0 ;;
  "volume inspect")
    name="${!#}"
    if [ -d "$VOLROOT/$name" ]; then echo "$VOLROOT/$name"; exit 0; fi
    echo ""; exit 1 ;;
  "volume ls")
    want=""
    for a in "$@"; do
      case "$a" in label=com.docker.compose.volume=*) want="${a#label=com.docker.compose.volume=}" ;; esac
    done
    [ -n "$want" ] || exit 0
    [ -d "$VOLROOT/stack_$want" ] && echo "stack_$want"
    exit 0 ;;
esac
exit 0
MOCK
chmod +x "$MOCKBIN/docker"
cat >"$VOLROOT/.svcmap" <<'MAP'
caddy=caddy_data
tracker=tracker_data
actual=actual_data
uptime-kuma=uptimekuma_data
MAP

TABLE='caddy:caddy_data:caddy tracker:tracker_data:tracker actual:actual_data:actual uptimekuma:uptimekuma_data:uptime-kuma'

# ── a REAL archive run, made by the real backup.sh ───────────────────────────
# NOT a hand-built fixture. The thing under test reads a MANIFEST, an archive and
# a per-file hash table that restore.sh verifies byte-for-byte; a fixture that
# only looked like one would prove nothing about the pair actually agreeing.
SRC="$TMP/sources"
mkdir -p "$SRC/caddy" "$SRC/tracker" "$SRC/actual" "$SRC/uptimekuma"
printf 'acme-account-key\n' >"$SRC/caddy/acme.key"
mkdir -p "$SRC/caddy/certificates"; printf 'cert-material\n' >"$SRC/caddy/certificates/apex.crt"
printf -- '---\ncategory: X\nitems:\n  - id: a\n---\n' >"$SRC/tracker/defs.md"
printf 'budget-db\n' >"$SRC/actual/budget.sqlite"
printf 'kuma-db\n' >"$SRC/uptimekuma/kuma.db"
cat >"$TMP/backup.env" <<ENVF
BACKUP_TARGET=$DRIVE
BACKUP_STAGING=$TMP/staging
BACKUP_KEEP=7
BACKUP_TARGET_REQUIRE_MOUNT=false
BACKUP_DRIVE_DEVICES=""
NAGLIGHT_FEED_URL=""
BACKUP_WAKE_MAC=""
INGEST_SOURCES=""
BACKUP_SOURCES="caddy=path:$SRC/caddy
tracker=path:$SRC/tracker
actual=path:$SRC/actual
uptimekuma=path:$SRC/uptimekuma"
ENVF
bash "$BACKUP_SH" --config "$TMP/backup.env" >"$TMP/backup.out" 2>&1
rc=$?
if [ "$rc" = 0 ]; then pass "setup: a real backup run produced the archive under test"; else fail "setup: backup run exit=$rc"; tail -20 "$TMP/backup.out"; exit 1; fi
# and a plan run, so R9 has something to be tempted by
bash "$BACKUP_SH" --config "$TMP/backup.env" --plan >/dev/null 2>&1

run_sut() { # run_sut [TABLE] -> log in $TMP/result.log
    : >"$TMP/result.log"
    MOCK_VOLROOT="$VOLROOT" PATH="$MOCKBIN:$PATH" STACK_DIR="$STACK" HOMEHUB_RESTORE_FAILDIR="$FAILDIR" \
        bash "$SUT" "$DRIVE" "$TMP/result.log" "${1:-$TABLE}" >"$TMP/sut.out" 2>&1
    echo $?
}
logline() { grep -E "^(ok|skip|FAIL) $1( |$)" "$TMP/result.log" | head -1; }

echo "== R1/R2/R10: four sets restore in one pass, into empty volumes =="
rc="$(run_sut)"
if [ "$rc" = 0 ]; then pass "R10 the script exits 0 (the log is the verdict)"; else fail "R10 exit=$rc"; cat "$TMP/sut.out"; fi
for s in caddy tracker actual uptimekuma; do
    l="$(logline "$s")"
    case "$l" in ok\ *) pass "R2 $s restored ($l)" ;; *) fail "R2 $s: ${l:-no log line}" ;; esac
done
if [ -f "$VOLROOT/stack_caddy_data/acme.key" ] && [ -f "$VOLROOT/stack_caddy_data/certificates/apex.crt" ]; then
    pass "R1 caddy_data holds the ACME key and the certificate, nested path intact"
else
    fail "R1 caddy_data contents: $(find "$VOLROOT/stack_caddy_data" -type f 2>/dev/null | tr '\n' ' ')"
fi
if [ "$(cat "$VOLROOT/stack_actual_data/budget.sqlite" 2>/dev/null)" = budget-db ]; then
    pass "R1 actual_data restored byte-for-byte"
else
    fail "R1 actual_data content wrong"
fi

echo
echo "== R4: a second pass changes nothing — this is not a fresh install =="
rc="$(run_sut)"
n_ok="$(grep -c '^ok ' "$TMP/result.log" || true)"
n_skip="$(grep -c 'is not empty' "$TMP/result.log" || true)"
if [ "$n_ok" = 0 ] && [ "$n_skip" = 4 ]; then pass "R4 all four declined as non-empty, none restored again"; else fail "R4 ok=$n_ok non-empty-skips=$n_skip"; cat "$TMP/result.log"; fi

echo
echo "== R3/R5: a missing archive skips ITS set and nothing else =="
rm -rf "$VOLROOT"/stack_*
rc="$(run_sut 'caddy:caddy_data:caddy nosuchset:nosuch_data:tracker actual:actual_data:actual')"
case "$(logline caddy)"     in ok\ *)   pass "R5 caddy still restored beside a missing set" ;; *) fail "R5 caddy: $(logline caddy)" ;; esac
case "$(logline nosuchset)" in skip\ *) pass "R3 the missing set was skipped and named" ;; *) fail "R3 nosuchset: $(logline nosuchset)" ;; esac
case "$(logline actual)"    in ok\ *)   pass "R5 actual restored AFTER the skipped set — the loop did not exit" ;; *) fail "R5 actual: $(logline actual)" ;; esac

echo
echo "== R6: a compose service that cannot be created is skipped, not fatal =="
rm -rf "$VOLROOT"/stack_*
: >"$TMP/result.log"
rc="$(MOCK_VOLROOT="$VOLROOT" MOCK_COMPOSE_FAIL=tracker PATH="$MOCKBIN:$PATH" STACK_DIR="$STACK" HOMEHUB_RESTORE_FAILDIR="$FAILDIR" \
      bash "$SUT" "$DRIVE" "$TMP/result.log" "$TABLE" >"$TMP/sut.out" 2>&1; echo $?)"
if [ "$rc" = 0 ]; then pass "R6 exit still 0 with a service that cannot be created"; else fail "R6 exit=$rc"; fi
case "$(logline tracker)" in skip\ *compose*) pass "R6 the tracker set was skipped, naming compose" ;; *) fail "R6 tracker: $(logline tracker)" ;; esac
case "$(logline caddy)"   in ok\ *)           pass "R6 caddy restored regardless" ;; *) fail "R6 caddy: $(logline caddy)" ;; esac

echo
echo "== R7: a corrupted archive must not leave a half-filled volume =="
rm -rf "$VOLROOT"/stack_*
RUNDIR="$(find "$DRIVE" -maxdepth 1 -type d -name 'run_*' | sort | tail -1)"
ARCH="$(find "$RUNDIR" -maxdepth 1 -name 'caddy.tar*' | head -1)"
cp "$ARCH" "$TMP/caddy.archive.bak"
printf 'corrupted\n' >"$ARCH"
rc="$(run_sut)"
case "$(logline caddy)" in FAIL\ *) pass "R7 the corrupted set is reported FAIL, not ok" ;; *) fail "R7 caddy: $(logline caddy)" ;; esac
LEFT="$(find "$VOLROOT/stack_caddy_data" -mindepth 1 2>/dev/null | wc -l | tr -d ' ')"
if [ "$LEFT" = 0 ]; then pass "R7 and the volume was emptied again, so the next boot retries instead of declining"; else fail "R7 $LEFT path(s) left in the volume — every later boot would skip it as 'not empty'"; fi
case "$(logline tracker)" in ok\ *) pass "R7 the other sets restored anyway" ;; *) fail "R7 tracker: $(logline tracker)" ;; esac
cp "$TMP/caddy.archive.bak" "$ARCH"

echo
echo "== R8: a malformed table row is reported, never silently ignored =="
rm -rf "$VOLROOT"/stack_*
rc="$(run_sut 'caddy caddy:caddy_data:caddy a:b:c:d :x:y')"
if [ "$(grep -c '^FAIL - ' "$TMP/result.log")" -ge 2 ]; then pass "R8 malformed rows were reported (>=2 FAIL lines)"; else fail "R8 malformed rows: $(cat "$TMP/result.log")"; fi
case "$(logline caddy)" in ok\ *) pass "R8 the valid row in the same table still ran" ;; *) fail "R8 caddy: $(logline caddy)" ;; esac

echo
echo "== R9: a plan_<ts> directory is never chosen as a restore source =="
NPLAN="$(find "$DRIVE" -maxdepth 1 -type d -name 'plan_*' | wc -l | tr -d ' ')"
if [ "$NPLAN" -ge 1 ]; then pass "R9 setup: the drive really does carry $NPLAN plan director(ies) to be tempted by"; else fail "R9 setup: no plan directory on the drive"; fi
rm -rf "$VOLROOT"/stack_*
rc="$(run_sut)"
BAD=0
while IFS= read -r l; do
    case "$l" in *' from plan_'*) BAD=1 ;; esac
done <"$TMP/result.log"
if [ "$BAD" = 0 ]; then pass "R9 every restore named a run_ directory, never a plan_ one"; else fail "R9 a restore was sourced from a plan directory"; cat "$TMP/result.log"; fi


echo
echo "== R11: image seed data in a volume this step just created is cleared, not obeyed =="
# THE C22 SHAPE, FOUND BY REVIEW BEFORE IT COULD BITE. Docker seeds a NEWLY
# created named volume from whatever the image has at the mount path. The first
# cut tested "is the volume empty" AFTER `compose create`, so any image shipping
# content there would make the restore decline on every fresh install — silently,
# forever, exactly like the caddy restore that could never fire.
rm -rf "$VOLROOT"/stack_*
: >"$VOLROOT/.seed-on-create"          # tells the mock to drop a seed file
rc="$(run_sut)"
rm -f "$VOLROOT/.seed-on-create"
case "$(logline caddy)" in
    ok\ *) pass "R11 a fresh volume holding image seed data still restored" ;;
    *)     fail "R11 caddy: $(logline caddy)"; cat "$TMP/result.log" ;;
esac
if grep -q '^note caddy .*image seed data' "$TMP/result.log"; then
    pass "R11 and it said out loud that it cleared the seed"
else
    fail "R11 the seed was cleared silently"
fi
if [ -f "$VOLROOT/stack_caddy_data/acme.key" ] && [ ! -f "$VOLROOT/stack_caddy_data/IMAGE-SEED" ]; then
    pass "R11 the archive's content is there and the seed is gone"
else
    fail "R11 volume contents: $(ls -A "$VOLROOT/stack_caddy_data" | tr '\n' ' ')"
fi

echo
echo "== R12: a volume that ALREADY existed and is not empty is left alone =="
# Same observable outcome as R11's opposite: the distinction is whether the
# volume existed BEFORE this step created it, not whether it is empty now.
rc="$(run_sut)"
n_skip="$(grep -c 'already existed and is not empty' "$TMP/result.log" || true)"
if [ "$n_skip" -ge 1 ] && [ "$(grep -c '^ok ' "$TMP/result.log")" = 0 ]; then
    pass "R12 a pre-existing non-empty volume is skipped, and nothing was restored over it"
else
    fail "R12 skips=$n_skip oks=$(grep -c '^ok ' "$TMP/result.log")"; cat "$TMP/result.log"
fi

echo
echo "== R13: a cleanup that failed once does not become permanent silence =="
# The state this is about: restore.sh failed, the volume could not be emptied, so
# it holds a PARTIAL copy. Without a durable note, every later boot reports
# "not empty, so this is not a fresh install" — a true sentence about a broken
# volume, and the service starts against it forever.
rm -rf "$VOLROOT"/stack_*
mkdir -p "$FAILDIR"; : >"$FAILDIR/caddy_data"
rc="$(run_sut)"
case "$(logline caddy)" in
    FAIL\ *PARTIAL*) pass "R13 the note turns the next pass into a FAIL naming the partial volume" ;;
    *)               fail "R13 caddy: $(logline caddy)" ;;
esac
if grep -q "$FAILDIR/caddy_data" "$TMP/result.log"; then
    pass "R13 and it names the file to delete once the volume is cleaned by hand"
else
    fail "R13 the FAIL line does not say how to clear it"
fi
rm -f "$FAILDIR/caddy_data"
case "$(logline tracker)" in
    ok\ *) pass "R13 and the other sets were unaffected" ;;
    *)     fail "R13 tracker: $(logline tracker)" ;;
esac
echo
echo "──────────────────────────────────────────────────────────────"
printf '%s PASS  %s FAIL\n' "$PASS" "$FAIL"
if [ "$FAIL" -eq 0 ]; then exit 0; fi
exit 1
