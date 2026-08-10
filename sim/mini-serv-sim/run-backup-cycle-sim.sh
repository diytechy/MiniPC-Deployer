#!/usr/bin/env bash
# run-backup-cycle-sim.sh — the CYCLE leg: what a nightly backup does over TIME.
#
# run-backup-sim.sh proves ONE run: mount, archive, hash, offsite, restore. It
# cannot prove anything that only exists across runs, and everything the
# 2026-08-09 handoff listed as never-proven lives in that gap:
#
#   * a SECOND run after files were ADDED, DELETED and MODIFIED — does the new
#     archive follow the source, and does the OLD archive still hold the deleted
#     file? (That second half is the whole recovery story: `rsync -a --delete`
#     mirrors, so history exists ONLY in the dated run snapshots.)
#   * RETENTION actually pruning — never once observed on a real run.
#   * a MISSING source SKIPPING its set while the others still archive, asserted
#     against a REAL run rather than a --dry-run plan (TC-H-M11).
#   * the `volume:` / `path:` INCONSISTENCY: a missing path skips, a missing
#     volume aborts the whole run. This leg pins the CURRENT behaviour of both so
#     the ruling — whichever way it goes — has a regression test to move.
#
# DELIBERATELY HERMETIC. Sources are `path:` trees created inside the runner, not
# Samba shares: this leg is about the pipeline's behaviour over time, and a cifs
# dependency would make it slower and give it a second way to fail that
# run-backup-sim.sh already covers properly. Likewise NAGLIGHT_FEED_URL is unset,
# so the feed is a logged skip and no tracker has to exist.
#
# The `volume:` scenarios use a MOCK `docker` on PATH — the same technique
# run-drivepower-sim.sh uses for hdparm. A container has no docker volumes, but
# the CONTRACT (what backup.sh does when a volume resolves, and when it does not)
# is exactly what needs proving.
#
# Prereq: the backup-runner container up (run-backup-sim.sh brings it up; this
# leg starts it on its own if it is not). Nothing here touches the Samba fixtures.
#
# Usage:
#   run-backup-cycle-sim.sh          # all scenarios
#   run-backup-cycle-sim.sh --down   # tear down mini-serv-sim
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
CO=(docker compose -p mini-serv-sim -f docker-compose.yml)

case "${1:-}" in
    --down) "${CO[@]}" down -v; exit 0 ;;
esac

FAILS=0; CHECKS=0
pass() { CHECKS=$((CHECKS+1)); printf '  [PASS] %s\n' "$*"; }
fail() { CHECKS=$((CHECKS+1)); FAILS=$((FAILS+1)); printf '  [FAIL] %s\n' "$*"; }
info() { printf '         %s\n' "$*"; }

# This leg only needs the runner. It does NOT check for homehub-sim_default the
# way the other legs do: that guard is satisfied by an EMPTY network of the right
# name (found 2026-08-09 — a stale pre-rename `awow-sim` stack was running while
# `homehub-sim_default` existed with nothing on it, so the guard passed and the
# feed check then failed accusing the product). A leg that needs no tracker
# should not assert a network that proves nothing.
if ! docker ps --format '{{.Names}}' | grep -qx backup-runner; then
    echo "== backup-runner not up — starting it =="
    docker network inspect homehub-sim_default >/dev/null 2>&1 || docker network create homehub-sim_default >/dev/null
    "${CO[@]}" up -d --build backup-runner || { echo "ERROR: could not start backup-runner" >&2; exit 2; }
fi

rex() { docker exec backup-runner bash -c "$1"; }

echo "== stage the hermetic fixture library + the mock docker inside the runner =="
docker exec -i backup-runner bash -s <<'SETUP'
set -euo pipefail
rm -rf /srv/simlib /srv/simvol /srv/simtargets /opt/simbin
mkdir -p /srv/simlib/docs /srv/simlib/photos /srv/simlib/music /srv/simvol/appdata /opt/simbin

# --- text-ish set: compresses, so it takes the zstd branch ---
printf 'the household inventory, revision one\n%.0s' $(seq 1 40) > /srv/simlib/docs/inventory.txt
printf 'notes about the boiler service\n%.0s'        $(seq 1 40) > /srv/simlib/docs/boiler.txt
printf 'this file exists in run 1 and is DELETED before run 2\n%.0s' $(seq 1 40) > /srv/simlib/docs/doomed.txt
mkdir -p /srv/simlib/docs/receipts
printf 'receipt 2026-01-01 lumber 42.00\n%.0s' $(seq 1 20) > /srv/simlib/docs/receipts/jan.txt

# --- already-compressed set: takes the store-.tar branch ---
python3 - <<'PY'
import random
r = random.Random(7)
for n, sz in (("holiday.jpg", 9000), ("garden.png", 7000)):
    open("/srv/simlib/photos/" + n, "wb").write(bytes(r.getrandbits(8) for _ in range(sz)))
PY

# --- a set that is present in run 1 and whose SOURCE DIRECTORY vanishes later ---
printf 'track listing\n' > /srv/simlib/music/index.txt

# --- the stand-in for a docker volume's mountpoint ---
printf 'budget database, fictional\n%.0s' $(seq 1 30) > /srv/simvol/appdata/budget.db

# --- mock docker: resolves volumes from a table, so "present" and "absent" are
#     both expressible. Mirrors run-drivepower-sim.sh's mock hdparm. -----------
cat > /opt/simbin/docker <<'MOCK'
#!/usr/bin/env bash
# Mock docker for the cycle leg. Understands exactly the two calls
# common.sh:volume_mountpoint makes, resolved against /tmp/sim-volumes.tsv.
#
# THREE COLUMNS, AND THAT IS THE POINT:
#     real-volume-name <TAB> compose-label <TAB> mountpoint
#
# A two-column table keyed on the compose name looked simpler and was wrong: the
# literal `docker volume inspect <compose name>` at the top of volume_mountpoint
# would hit on the first try, so the COMPOSE-LABEL LOOKUP — the branch that
# exists because compose prefixes volume names, and the branch the 2026-08-09
# fix was written for — was never executed by any scenario. Modelling the prefix
# honestly (`stack_appdata` labelled `appdata`) is what makes the label path the
# one under test, and it is what lets the AMBIGUOUS case exist at all: ambiguity
# is two DIFFERENT real volumes carrying the SAME label, which a table keyed on
# the label cannot express.
TABLE=/tmp/sim-volumes.tsv
case "$1 $2" in
  "volume inspect")
      name="${!#}"                      # inspect resolves REAL names only
      mp="$(awk -F'\t' -v n="$name" '$1==n {print $3}' "$TABLE" 2>/dev/null)"
      [ -n "$mp" ] || exit 1
      printf '%s\n' "$mp" ;;
  "volume ls")
      # `volume ls -q` with no filter is the daemon liveness probe: list everything.
      # NOTE the `all="$*"` first: `${*##*=}` applies the pattern to EACH
      # positional parameter and rejoins them, so it yields the whole command
      # line with each word trimmed, not the filter's value. That silently made
      # every label lookup miss.
      all="$*"
      case "$all" in
        *--filter*) label="${all##*=}"
                    awk -F'\t' -v l="$label" '$2==l {print $1}' "$TABLE" 2>/dev/null ;;
        *)          awk -F'\t' '{print $1}' "$TABLE" 2>/dev/null ;;
      esac ;;
  *)  exit 127 ;;
esac
MOCK
chmod +x /opt/simbin/docker
# S7c replaces the mock with one that always fails (a dead daemon); keep a pristine
# copy so the scenarios after it are not silently running against the stub.
cp /opt/simbin/docker /opt/simbin/docker.real
printf 'stack_appdata\tappdata\t/srv/simvol/appdata\n' > /tmp/sim-volumes.tsv
echo "fixtures staged"
SETUP
[ $? -eq 0 ] || { echo "ERROR: fixture staging failed" >&2; exit 1; }

# write_env TARGET KEEP SOURCES... : emit a backup.env for one scenario.
#
# BACKUP_TARGET_REQUIRE_MOUNT=false on purpose. The preflight it turns off is
# proven properly by run-backup-sim.sh against /backup (a real volume mount);
# here the target is a plain directory per scenario so retention has its own
# clean slate, and leaving the guard on would refuse every run for the right
# reason at the wrong time.
write_env() {
    local target="$1" keep="$2" sources="$3"
    rex "cat > /tmp/cycle.env <<'ENVEOF'
BACKUP_TARGET=$target
BACKUP_TARGET_REQUIRE_MOUNT=false
BACKUP_STAGING=/var/tmp/homehub-backup/staging
BACKUP_KEEP=$keep
BACKUP_DRIVE_DEVICES=\"\"
BACKUP_ZSTD_LEVEL=3
BACKUP_INCOMPRESSIBLE_THRESHOLD=60
OFFSITE_ENABLED=false
BACKUP_SOURCES=\"$sources\"
ENVEOF
mkdir -p $target"
}

# run_backup [PATH_PREPEND] : run the real service, echo its exit code last.
run_backup() {
    local prepend="${1:-}"
    rex "PATH='${prepend}':\$PATH bash /opt/homehub/stack/backup/backup.sh --config /tmp/cycle.env >/tmp/cycle.out 2>&1; echo RC=\$?" | tr -d '\r'
}

# ═════════════════════════════════════════════════════════════════════════════
echo
echo "== S1: a real run over three path: sets — archives, manifest, both codecs =="
write_env /srv/simtargets/t1 3 'docs=path:/srv/simlib/docs
photos=path:/srv/simlib/photos
music=path:/srv/simlib/music'
rc="$(run_backup)"
if [ "$rc" = "RC=0" ]; then pass "run 1 exited 0"; else fail "run 1 $rc"; rex 'tail -20 /tmp/cycle.out'; fi

R1="$(rex 'ls -d /srv/simtargets/t1/run_* | sort | tail -1' | tr -d '\r')"
info "run 1 dir: $R1"
rows="$(rex "awk 'NR>1' '$R1/MANIFEST.tsv' | wc -l" | tr -d '\r')"
if [ "$rows" = "3" ]; then pass "MANIFEST holds 3 set rows"; else fail "MANIFEST holds $rows set rows, want 3"; fi

algos="$(rex "awk -F'\t' 'NR>1 {print \$1\":\"\$4}' '$R1/MANIFEST.tsv' | sort | tr '\n' ' '" | tr -d '\r')"
if printf '%s' "$algos" | grep -q 'docs:zstd' && printf '%s' "$algos" | grep -q 'photos:none'; then
    pass "compression decided per set — $algos"
else
    fail "unexpected codec split: $algos"
fi

# ═════════════════════════════════════════════════════════════════════════════
echo
echo "== S2: restore every set from run 1 and verify byte-exactness =="
for s in docs photos music; do
    out="$(rex "rm -rf /tmp/r1-$s && bash /opt/homehub/stack/backup/restore.sh --run '$R1' --set $s --target /tmp/r1-$s 2>&1; echo RC=\$?" | tr -d '\r')"
    if printf '%s' "$out" | grep -q 'RC=0' && printf '%s' "$out" | grep -q 'RESTORE OK'; then
        pass "restore [$s]: $(printf '%s' "$out" | grep -o 'RESTORE OK:.*')"
    else
        fail "restore [$s] failed"; info "$out"
    fi
    diffout="$(rex "diff -r /tmp/r1-$s /srv/simlib/$s 2>&1 | head -5" | tr -d '\r')"
    if [ -z "$diffout" ]; then pass "restore [$s] is byte-identical to the live source"
    else fail "restore [$s] differs from source"; info "$diffout"; fi
done

# ═════════════════════════════════════════════════════════════════════════════
echo
echo "== S3: ADD + DELETE + MODIFY, then run 2 — the archive follows the source =="
rex "rm -f /srv/simlib/docs/doomed.txt
     printf 'a brand new note, added between runs\n%.0s' \$(seq 1 30) > /srv/simlib/docs/added.txt
     printf 'the household inventory, revision TWO\n%.0s'  \$(seq 1 40) > /srv/simlib/docs/inventory.txt" >/dev/null

sleep 1        # run dirs are named to the SECOND; a nightly timer can never
               # collide, but three runs in one shell second would.
rc="$(run_backup)"
if [ "$rc" = "RC=0" ]; then pass "run 2 exited 0"; else fail "run 2 $rc"; rex 'tail -20 /tmp/cycle.out'; fi
R2="$(rex 'ls -d /srv/simtargets/t1/run_* | sort | tail -1' | tr -d '\r')"
info "run 2 dir: $R2"

has() { rex "cut -f4 '$1/docs.files.tsv' | grep -qx '$2' && echo yes || echo no" | tr -d '\r'; }
[ "$(has "$R2" added.txt)"  = yes ] && pass "run 2 CONTAINS the added file"        || fail "run 2 is missing added.txt"
[ "$(has "$R2" doomed.txt)" = no  ] && pass "run 2 has DROPPED the deleted file"   || fail "run 2 still lists doomed.txt"
# The half that matters for recovery: --delete mirrors, so the ONLY copy of a
# deleted file is the previous dated snapshot. If this ever fails, deletion is
# permanent and the retention window is a data-loss clock.
[ "$(has "$R1" doomed.txt)" = yes ] && pass "run 1 STILL holds the deleted file (history lives in the dated runs)" \
                                    || fail "run 1 lost doomed.txt — a deletion would be unrecoverable"

s1="$(rex "awk -F'\t' '\$4==\"inventory.txt\" {print \$1}' '$R1/docs.files.tsv'" | tr -d '\r')"
s2="$(rex "awk -F'\t' '\$4==\"inventory.txt\" {print \$1}' '$R2/docs.files.tsv'" | tr -d '\r')"
if [ -n "$s1" ] && [ -n "$s2" ] && [ "$s1" != "$s2" ]; then
    pass "the MODIFIED file has a different sha256 in run 2 (${s1:0:8}… -> ${s2:0:8}…)"
else
    fail "modified file sha did not change (run1=${s1:0:8} run2=${s2:0:8})"
fi

echo "  -- recover the deleted file from run 1, which is the actual restore story --"
out="$(rex "rm -rf /tmp/recover && bash /opt/homehub/stack/backup/restore.sh --run '$R1' --set docs --target /tmp/recover 2>&1; echo RC=\$?" | tr -d '\r')"
if printf '%s' "$out" | grep -q 'RC=0' && [ "$(rex 'test -f /tmp/recover/doomed.txt && echo yes || echo no' | tr -d '\r')" = yes ]; then
    pass "doomed.txt recovered from run 1 after being deleted at the source"
else
    fail "could not recover the deleted file from the previous run"; info "$out"
fi

# ═════════════════════════════════════════════════════════════════════════════
echo
echo "== S4: retention actually prunes (KEEP=2, three runs) =="
write_env /srv/simtargets/t4 2 'docs=path:/srv/simlib/docs'
for i in 1 2 3; do
    rc="$(run_backup)"; [ "$rc" = "RC=0" ] || { fail "retention run $i $rc"; rex 'tail -10 /tmp/cycle.out'; }
    sleep 1
done
kept="$(rex 'ls -d /srv/simtargets/t4/run_* 2>/dev/null | wc -l' | tr -d '\r')"
if [ "$kept" = "2" ]; then pass "3 runs with BACKUP_KEEP=2 left exactly 2 run dirs"; else fail "expected 2 run dirs, found $kept"; fi
if rex 'grep -q "prune old run" /tmp/cycle.out'; then
    pass "the third run logged its prune: $(rex 'grep -o "prune old run.*" /tmp/cycle.out | head -1' | tr -d '\r')"
else
    fail "no prune line in the third run's log"
fi
# The pruned one must be the OLDEST, not an arbitrary one.
oldest="$(rex 'ls -d /srv/simtargets/t4/run_* | sort | head -1' | tr -d '\r')"
newest="$(rex 'ls -d /srv/simtargets/t4/run_* | sort | tail -1' | tr -d '\r')"
if [ "$oldest" != "$newest" ]; then pass "the two survivors are the two NEWEST ($(basename "$oldest") .. $(basename "$newest"))"
else fail "only one distinct run dir survived"; fi

# ═════════════════════════════════════════════════════════════════════════════
echo
echo "== S5: a MISSING path: source skips ONLY its set — against a real run (TC-H-M11) =="
write_env /srv/simtargets/t5 3 'docs=path:/srv/simlib/docs
gone=path:/srv/simlib/not-here
photos=path:/srv/simlib/photos'
rc="$(run_backup)"
if [ "$rc" = "RC=1" ]; then pass "the run finished RED (exit 1) as a partial run must"; else fail "expected RC=1, got $rc"; fi
R5="$(rex 'ls -d /srv/simtargets/t5/run_* | sort | tail -1' | tr -d '\r')"
got="$(rex "awk -F'\t' 'NR>1 {printf \"%s \", \$1}' '$R5/MANIFEST.tsv'" | tr -d '\r')"
if printf '%s' "$got" | grep -q docs && printf '%s' "$got" | grep -q photos; then
    pass "the other sets WERE archived despite the missing one — got: $got"
else
    fail "the healthy sets did not archive; got: '$got'"
fi
# The set AFTER the missing one is the load-bearing half: an abort would lose it.
if printf '%s' "$got" | grep -q photos; then pass "the set ORDERED AFTER the missing one still ran (no abort)"; fi
st="$(rex "grep -o '\"status\": \"[a-z]*\"' '$R5/RUN.json'" | tr -d '\r')"
if printf '%s' "$st" | grep -q failed; then pass "RUN.json records status failed — a partial run is never green"; else fail "RUN.json status is $st"; fi
if rex "test -f '$R5/docs.tar.zst' -o -f '$R5/docs.tar'"; then
    out="$(rex "rm -rf /tmp/r5 && bash /opt/homehub/stack/backup/restore.sh --run '$R5' --set docs --target /tmp/r5 2>&1; echo RC=\$?" | tr -d '\r')"
    printf '%s' "$out" | grep -q 'RC=0' && pass "a set from a PARTIAL run still restores cleanly" || fail "restore from a partial run failed"
fi

# ═════════════════════════════════════════════════════════════════════════════
echo
echo "== S6: a resolvable volume: source archives like any other set =="
write_env /srv/simtargets/t6 3 'docs=path:/srv/simlib/docs
appdata=volume:appdata'
rc="$(run_backup /opt/simbin)"
if [ "$rc" = "RC=0" ]; then pass "a run with a resolvable volume: set exited 0"; else fail "volume run $rc"; rex 'tail -20 /tmp/cycle.out'; fi
R6="$(rex 'ls -d /srv/simtargets/t6/run_* | sort | tail -1' | tr -d '\r')"
if rex "awk -F'\t' 'NR>1 {print \$1}' '$R6/MANIFEST.tsv' | grep -qx appdata"; then
    pass "the volume set is in the MANIFEST alongside the path set"
else
    fail "the volume set never reached the manifest"
fi

# ═════════════════════════════════════════════════════════════════════════════
echo
echo "== S7: a MISSING volume: source SKIPS its set, exactly as a missing path: does =="
# Ruled 2026-08-09: the Owner's "a missing folder shouldn't block the backup of
# other folders" always covered both source kinds; only `path:` had been changed
# (open-items A26 deferred the rest). `later` is ordered AFTER the missing volume
# precisely to measure the blast radius: under the old abort it never ran at all.
write_env /srv/simtargets/t7 3 'docs=path:/srv/simlib/docs
appdata=volume:no-such-volume
later=path:/srv/simlib/photos'
rc="$(run_backup /opt/simbin)"
R7="$(rex 'ls -d /srv/simtargets/t7/run_* | sort | tail -1' | tr -d '\r')"
got7="$(rex "awk -F'\t' 'NR>1 {printf \"%s \", \$1}' '$R7/MANIFEST.tsv' 2>/dev/null" | tr -d '\r')"
info "exit: $rc   sets archived: '${got7:-none}'"
[ "$rc" = "RC=1" ] && pass "a missing volume: source finishes RED (exit 1)" || fail "expected RC=1, got $rc"
if printf '%s' "$got7" | grep -q later; then
    pass "the set ORDERED AFTER the missing volume still archived — volume: now matches path:"
else
    fail "the missing volume ABORTED the run; the set after it never archived"
fi
if rex "grep -q 'volume:no-such-volume' '$R7/RUN.json'"; then
    pass "RUN.json names the skipped volume set"
else
    fail "the skipped volume set is not named in RUN.json"
fi

echo
echo "== S7b: the two volume failures that must STAY fatal =="
# THE REASON THE FIX IS TWO FILES. `volume_mountpoint` used to return 1 for all
# three of "absent", "ambiguous label match" and "daemon unreachable", so a skip
# keyed on it would have skipped every one of them. These two scenarios are what
# stops that regressing: if either starts passing as a skip, the split has been
# undone and one broken box quietly stops archiving five volumes.

# (a) AMBIGUOUS — two volumes carrying the same compose label. This is the
#     stack_actual_data / stack_finance_actual_data hazard: a silent wrong choice
#     here archives a finance volume as the budget one.
rex "printf 'stack_appdata\tappdata\t/srv/simvol/appdata\nstack_finance_appdata\tappdata\t/srv/simvol/appdata\n' > /tmp/sim-volumes.tsv" >/dev/null
write_env /srv/simtargets/t7b 3 'docs=path:/srv/simlib/docs
appdata=volume:appdata
later=path:/srv/simlib/photos'
rc="$(run_backup /opt/simbin)"
if [ "$rc" = "RC=1" ] && rex 'grep -q "AMBIGUOUS" /tmp/cycle.out'; then
    pass "an ambiguous volume name ABORTS and says so: $(rex 'grep -o "is AMBIGUOUS[^\"]*" /tmp/cycle.out | head -1 | cut -c1-70' | tr -d '\r')…"
else
    fail "an ambiguous volume label did not abort with an AMBIGUOUS message (rc=$rc)"
fi

# (b) DAEMON UNREACHABLE — "could not ask" must never be reported as "not there".
rex "printf '#!/bin/sh\nexit 1\n' > /opt/simbin/docker; chmod +x /opt/simbin/docker" >/dev/null
write_env /srv/simtargets/t7c 3 'docs=path:/srv/simlib/docs
appdata=volume:appdata'
rc="$(run_backup /opt/simbin)"
if [ "$rc" = "RC=1" ] && rex 'grep -q "cannot reach the docker daemon" /tmp/cycle.out'; then
    pass "an unreachable daemon ABORTS rather than skipping all volume sets as absent"
else
    fail "an unreachable daemon was not distinguished from an absent volume (rc=$rc)"
fi
# restore the working mock for anything after this point
rex "cp /opt/simbin/docker.real /opt/simbin/docker 2>/dev/null || true
     printf 'stack_appdata\tappdata\t/srv/simvol/appdata\n' > /tmp/sim-volumes.tsv" >/dev/null

# ═════════════════════════════════════════════════════════════════════════════
echo
echo "== S8: do FAILED runs count as keepers? (retention vs the new skip behaviour) =="
# THE ONE THAT MATTERS. Retention runs at backup.sh step 4, which is BEFORE the
# verdict at step 6 — so it cannot know whether the run it just wrote is any
# good. It prunes on `run_*` directory names alone.
#
# That was harmless while a missing source ABORTED: the run died in the ERR trap
# and never reached retention at all. Yesterday's A26 fix made a missing source
# SKIP its set and carry on — which is the right call for the night it happens,
# and it also means a partial run now walks all the way through retention.
#
# So the failure to test for is a library drive that does not mount. Every night
# produces a run directory holding a header-only manifest and no archives, every
# one of those counts as a keeper, and after BACKUP_KEEP nights the last GOOD
# archives have been rotated out by runs that backed up nothing. The feed is red
# throughout — this is not silent — but the data is gone by the time anyone acts.
write_env /srv/simtargets/t8 2 'docs=path:/srv/simlib/docs'
for i in 1 2; do run_backup >/dev/null; sleep 1; done
good="$(rex 'ls -d /srv/simtargets/t8/run_* | wc -l' | tr -d '\r')"
info "after 2 good runs: $good run dir(s), each holding a docs archive"

# now the library "does not mount" — the source path is simply not there
write_env /srv/simtargets/t8 2 'docs=path:/srv/simlib/vanished'
for i in 1 2; do run_backup >/dev/null; sleep 1; done

witharchive="$(rex 'ls -d /srv/simtargets/t8/run_* | while read -r d; do ls "$d" | grep -q "^docs\.tar" && echo "$d"; done | wc -l' | tr -d '\r')"
total="$(rex 'ls -d /srv/simtargets/t8/run_* | wc -l' | tr -d '\r')"
info "after 2 further runs whose source was missing: $total run dir(s), $witharchive of them holding an archive"
if [ "${witharchive:-0}" -ge 1 ]; then
    pass "the good archives survived two failed nights ($witharchive of $total dirs hold one)"
else
    fail "DATA LOSS: two failed runs rotated out every good archive (BACKUP_KEEP=2)"
    info "  Retention must run AFTER the verdict and must count only runs whose"
    info "  RUN.json says ok. A library drive that stops mounting otherwise erases"
    info "  the last good backups over BACKUP_KEEP nights — red every night, but"
    info "  the data is gone by the time anyone acts."
fi
# A failed run must still not be able to evict a good one no matter how many
# times it happens, so check the good runs are the ones that survived.
if [ "${witharchive:-0}" -eq 2 ]; then
    pass "both good runs survived — a red run prunes nothing at all"
fi

echo
echo "== S9: BACKUP_KEEP=0 deletes the run it just wrote, and then misreports why =="
# KEEP is read as ${BACKUP_KEEP:-7}, so 0 is a LEGAL value meaning "keep
# nothing" — and retention runs at step 4, while the run is still writing. The
# run therefore prunes itself.
#
# WHAT HAPPENS NEXT IS THE INTERESTING HALF, and it is measured, not assumed.
# LOG_FILE lives inside the directory that was just removed, so the next `log`
# call fails, the ERR trap catches it, and the run dies reporting
#
#     BACKUP FAILED: backup failed at line 30
#
# — common.sh line 30 being the LOGGER. Nothing in that message mentions
# retention, BACKUP_KEEP, or the deletion. It is the 2026-08-09 "diagnosis
# accusing the wrong component" pattern in a fresh instance: the failure is
# real, loud and non-silent, and it points at the last thing to touch the
# corpse. (An earlier write-up of this scenario claimed the run reports
# ok=true. It does not — measured RC=1 — and that is why this asserts.)
write_env /srv/simtargets/t9 0 'docs=path:/srv/simlib/docs'
rc="$(run_backup)"
left="$(rex 'ls -d /srv/simtargets/t9/run_* 2>/dev/null | wc -l' | tr -d '\r')"
info "exit: $rc   run dirs left: $left"
if [ "$rc" = "RC=1" ] && rex "grep -q \"BACKUP_KEEP=0 means\" /tmp/cycle.out"; then
    pass "BACKUP_KEEP=0 is REFUSED at config load, naming the knob"
else
    fail "BACKUP_KEEP=0 was accepted (rc=$rc)"
fi
[ "${left:-0}" -eq 0 ] && pass "and it refused BEFORE writing anything — no run directory exists" \
                       || fail "it wrote $left run dir(s) before refusing"

# A non-numeric value used to reach the arithmetic inside retention and kill the
# run there — an hour of archiving in, with no report posted at all.
write_env /srv/simtargets/t9b seven 'docs=path:/srv/simlib/docs'
rc="$(run_backup)"
if [ "$rc" = "RC=1" ] && rex "grep -q \"is not a number\" /tmp/cycle.out"; then
    pass "a non-numeric BACKUP_KEEP is refused at config load, not mid-retention"
else
    fail "a non-numeric BACKUP_KEEP was not caught early (rc=$rc)"
fi

# ═════════════════════════════════════════════════════════════════════════════
echo
echo "== S10: a TRUNCATED hash table must not restore 3 of N and say RESTORE OK =="
# restore.sh verified every file its table LISTED and nothing else, so the table
# was both the work list and the definition of done — damaging it made the check
# SMALLER rather than making it fail. Three witnesses now have to agree:
# MANIFEST columns 6/7, the table's row count, and a census of the archive.
#
# (c) IS THE LOAD-BEARING CHECK. It doctors the MANIFEST to agree with the
# truncated table, which removes the easy witness and leaves only the archive.
# If the reconciliation were circular — two records of the same counter — (c)
# would pass and the restore would still be lying.
rex 'rm -rf /srv/simlib/bulk && mkdir -p /srv/simlib/bulk
     for i in $(seq 1 11); do printf "bulk file %s\n%.0s" "$i" $(seq 1 12) > /srv/simlib/bulk/f$i.txt; done' >/dev/null
write_env /srv/simtargets/t10 3 'bulk=path:/srv/simlib/bulk'
rc="$(run_backup)"
R10="$(rex 'ls -d /srv/simtargets/t10/run_* | sort | tail -1' | tr -d '\r')"
[ "$rc" = "RC=0" ] && pass "baseline run archived the 11-file set" || fail "baseline $rc"

# (a) FALSE-POSITIVE GUARD FIRST. A reconciliation that fires on a healthy
#     restore is a reconciliation someone deletes in six months.
out="$(rex "rm -rf /tmp/s10a && bash /opt/homehub/stack/backup/restore.sh --run '$R10' --set bulk --target /tmp/s10a 2>&1; echo RC=\$?" | tr -d '\r')"
if printf '%s' "$out" | grep -q 'RC=0' && printf '%s' "$out" | grep -q 'all agree on 11'; then
    pass "(a) an undamaged restore still passes, and says all three witnesses agree"
else
    fail "(a) the reconciliation fires on a healthy restore"; info "$out"
fi

# (b) the original defect: cut the table to 3 of 11 rows.
rex "head -n4 '$R10/bulk.files.tsv' > /tmp/cut && cp /tmp/cut '$R10/bulk.files.tsv'" >/dev/null
out="$(rex "rm -rf /tmp/s10b && bash /opt/homehub/stack/backup/restore.sh --run '$R10' --set bulk --target /tmp/s10b 2>&1; echo RC=\$?" | tr -d '\r')"
if printf '%s' "$out" | grep -q 'RC=1'; then
    pass "(b) a table cut to 3 of 11 now FAILS: $(printf '%s' "$out" | grep -o 'COUNT MISMATCH[^,]*' | head -1)"
else
    fail "(b) a truncated table still reported success"; info "$out"
fi

# (c) THE NON-CIRCULARITY PROOF: make the manifest agree with the short table.
rex "awk -F'\t' 'BEGIN{OFS=\"\t\"} NR==1{print;next} \$1==\"bulk\"{\$6=3;\$7=1;print;next} {print}' '$R10/MANIFEST.tsv' > /tmp/m2 && cp /tmp/m2 '$R10/MANIFEST.tsv'" >/dev/null
out="$(rex "rm -rf /tmp/s10c && bash /opt/homehub/stack/backup/restore.sh --run '$R10' --set bulk --target /tmp/s10c 2>&1; echo RC=\$?" | tr -d '\r')"
if printf '%s' "$out" | grep -q 'RC=1' && printf '%s' "$out" | grep -q 'archive vs table'; then
    pass "(c) with the manifest doctored to match, the ARCHIVE still catches it — the check is not circular"
else
    fail "(c) doctoring the manifest defeated the reconciliation — it is only comparing two copies of one counter"
    info "$out"
fi

# (d) a set the run SKIPPED must not report like a typo. Distinct exit codes.
write_env /srv/simtargets/t10d 3 'bulk=path:/srv/simlib/bulk
absent=path:/srv/simlib/not-there'
run_backup >/dev/null
R10D="$(rex 'ls -d /srv/simtargets/t10d/run_* | sort | tail -1' | tr -d '\r')"
skipped="$(rex "bash /opt/homehub/stack/backup/restore.sh --run '$R10D' --set absent --target /tmp/s10d >/dev/null 2>&1; echo \$?" | tr -d '\r')"
typo="$(rex "bash /opt/homehub/stack/backup/restore.sh --run '$R10D' --set blukk --target /tmp/s10e >/dev/null 2>&1; echo \$?" | tr -d '\r')"
if [ "$skipped" = "3" ] && [ "$typo" = "4" ]; then
    pass "(d) a SKIPPED set exits 3 and a mistyped name exits 4 — one status per cause"
else
    fail "(d) skipped=$skipped typo=$typo; expected 3 and 4 (they used to share one message)"
fi

# ═════════════════════════════════════════════════════════════════════════════
echo
echo "== summary =="
echo "  $((CHECKS - FAILS)) of $CHECKS checks passed"
if [ "$FAILS" -eq 0 ]; then echo "BACKUP CYCLE LEG: PASS"; exit 0; fi
echo "BACKUP CYCLE LEG: FAIL ($FAILS check(s) failed)"; exit 1
