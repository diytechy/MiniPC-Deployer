#!/usr/bin/env bash
# run-ingest-sim.sh — prove the two backup-service steps ratified 2026-07-29:
#
#   * INGEST (step 1b): mirror a network share INTO the library tree so the
#     library holds the CURRENT copy, and let the ordinary archive flow cover
#     that library folder like any other directory (an `path:` source);
#   * EXCLUSIONS (step 2): a global `BACKUP_EXCLUDE` plus per-set
#     `name.exclude=` lines, applied to the pull/archive and — the point —
#     VISIBLE in the run log, in `<set>.excluded.log` and in the MANIFEST.
#
# Unlike the mock-shim legs this uses the REAL cifs path: the Samba fixture host
# is a genuine network source, so ingest is exercised end to end (wake → mount →
# rsync mirror → unmount) with only the NagLight feed mocked. Scenarios:
#
#   (a) ingest mirrors //mini-serv/minecraft into /srv/library/NonDocs/MiniServ
#       (creating the leaf), the library copy is byte-identical to the live
#       share, the `path:` set over it archives + restores byte-equal, and
#       RUN.json records the ingest;
#   (b) MIRROR semantics: files that exist only in the library are DELETED by
#       the next ingest (source deletions propagate) — the documented, dangerous
#       half of `rsync --delete`, asserted rather than trusted;
#   (c) EXCLUSIONS: `*.bak` globally + `docs.exclude=Downloads` per set — the
#       excluded paths are absent from the archive AND from <set>.files.tsv,
#       each one is named in the log + <set>.excluded.log, the MANIFEST carries
#       the patterns, restore.sh says out loud that the copy is FILTERED, and a
#       set with no per-set line still gets the global patterns;
#   (d) MIRROR SAFETY: a share that mounts but is EMPTY does NOT get to
#       mirror-delete a good library copy — the run dies loudly with ok=false
#       and the library survives untouched; INGEST_ALLOW_EMPTY=true is the
#       explicit override;
#   (e) loud config failures: a malformed INGEST_SOURCES line, and a
#       `name.exclude=` line naming a set that does not exist, each die with
#       ok=false (never-silent-green).
#
# Scenario (d) needs a share that really is empty, so the compose file grows one:
# `//mini-serv/empty`, backed by a named volume nothing ever writes to. No other
# leg's fixtures are touched.
#
# Prereq: only the mini-serv-sim samba fixtures. The feed is MOCKED, so — like
# run-volume-sim.sh — the homehub-sim stack is not needed; the shared network is
# created standalone if absent.
#
# Usage:
#   run-ingest-sim.sh          # all five scenarios (a-e)
#   run-ingest-sim.sh --down   # tear down mini-serv-sim (same as siblings)
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
CO=(docker compose -p mini-serv-sim -f docker-compose.yml)
BACKUP=/opt/homehub/stack/backup/backup.sh
RESTORE=/opt/homehub/stack/backup/restore.sh
LIB=/srv/library/NonDocs/MiniServ

case "${1:-}" in
    --down) "${CO[@]}" down -v; exit 0 ;;
esac

if ! docker network inspect homehub-sim_default >/dev/null 2>&1; then
    echo "NOTE: homehub-sim_default not found — creating it standalone (feed is mocked; tracker not needed)."
    docker network create homehub-sim_default >/dev/null
fi

FAILS=0
pass() { printf '  [PASS] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; FAILS=$((FAILS+1)); }
rex()  { docker exec backup-runner bash -c "$1"; }
has()  { printf '%s\n' "$1" | grep -Fq -- "$2"; }

echo "== build + up mini-serv-sim (samba + privileged runner) =="
"${CO[@]}" up -d --build

echo "== wait for Samba to accept a cifs mount AND serve a fixture file =="
if rex 'for i in $(seq 1 30); do mkdir -p /mnt/probe; if mount -t cifs //mini-serv/minecraft /mnt/probe -o username=homehub,password=simpass,ro,vers=3.0 2>/dev/null; then if [ -s /mnt/probe/server.properties ]; then umount /mnt/probe; echo ready; exit 0; fi; umount /mnt/probe; fi; sleep 2; done; exit 1'; then
    pass "Samba share mountable"
else
    fail "Samba never became mountable"; echo "== summary =="; echo "INGEST LEG: FAIL"; exit 1
fi

# ── install the mock feed, the library tree, the planted Docs set, the configs ──
echo "== stage the library tree, a planted Docs source, mock curl, and the configs =="
docker exec -i backup-runner bash -s <<'SETUP'
set -eu
mkdir -p /tmp/ing
: > /tmp/ing/curl.calls

# Mock curl: capture each POST body (-d value); print the 200 feed_naglight reads.
cat > /usr/local/bin/curl <<'SH'
#!/usr/bin/env bash
prev=""; body=""
for a in "$@"; do
    [ "$prev" = "-d" ] && body="$a"
    prev="$a"
done
printf '%s\n' "$body" >> /tmp/ing/curl.calls
printf '200'
SH
chmod +x /usr/local/bin/curl

# The library tree. Only the PARENT is created: the ingest step must create the
# leaf itself on the first run (and must refuse a missing parent — scenario e).
rm -rf /srv/library
mkdir -p /srv/library/NonDocs

# A planted, NON-ingested library folder for the exclusion scenario: keepers, a
# *.bak at two depths, and a named "very large" folder (the Owner's two stated
# exclusion cases).
mkdir -p /srv/library/Docs/sub /srv/library/Docs/Downloads
echo 'the real document'      > /srv/library/Docs/notes.txt
echo 'nested real document'   > /srv/library/Docs/sub/deep.txt
echo 'editor backup'          > /srv/library/Docs/notes.txt.bak
echo 'nested editor backup'   > /srv/library/Docs/sub/deep.txt.bak
dd if=/dev/urandom of=/srv/library/Docs/Downloads/huge.bin bs=1024 count=32 2>/dev/null
echo 'second big file'        > /srv/library/Docs/Downloads/huge2.bin

# Shared config tail (everything but the sources/exclude knobs).
cat > /tmp/ing/env.tail <<'ENV'
BACKUP_TARGET=/backup
BACKUP_STAGING=/var/tmp/homehub-backup/staging
BACKUP_KEEP=3
BACKUP_CIFS_USER=homehub
BACKUP_CIFS_PASS=simpass
BACKUP_CIFS_EXTRA=vers=3.0
BACKUP_ZSTD_LEVEL=10
BACKUP_INCOMPRESSIBLE_THRESHOLD=60
OFFSITE_ENABLED=false
NAGLIGHT_FEED_URL=http://mock-tracker.invalid/api/feed
NAGLIGHT_FEED_CHECK=backup
NAGLIGHT_TOKEN=sim-ingest-token
NAGLIGHT_USER=sim-user-alice-0001
BACKUP_DRIVE_DEVICES=""
BACKUP_DRIVE_STANDBY=241
ENV

# (a/b) ingest one share into the library; back the library folder up as a
# path: set — the INTENDED pattern from backup.env.example.
{ printf 'INGEST_SOURCES="miniserv=//mini-serv/minecraft -> /srv/library/NonDocs/MiniServ"\n'
  printf 'BACKUP_SOURCES="miniserv=path:/srv/library/NonDocs/MiniServ"\n'
  cat /tmp/ing/env.tail; } > /tmp/ing/env.a

# (c) same ingest + the planted Docs set, with a global and a per-set filter.
{ printf 'INGEST_SOURCES="miniserv=//mini-serv/minecraft -> /srv/library/NonDocs/MiniServ"\n'
  printf 'BACKUP_EXCLUDE="*.bak"\n'
  printf 'BACKUP_SOURCES="miniserv=path:/srv/library/NonDocs/MiniServ\ndocs=path:/srv/library/Docs\ndocs.exclude=Downloads"\n'
  cat /tmp/ing/env.tail; } > /tmp/ing/env.c

# (d) ingest the EMPTY icedrive share over the populated library copy.
{ printf 'INGEST_SOURCES="miniserv=//mini-serv/empty -> /srv/library/NonDocs/MiniServ"\n'
  printf 'BACKUP_SOURCES="miniserv=path:/srv/library/NonDocs/MiniServ"\n'
  cat /tmp/ing/env.tail; } > /tmp/ing/env.d
{ printf 'INGEST_ALLOW_EMPTY=true\n'; cat /tmp/ing/env.d; } > /tmp/ing/env.d2

# (e1) malformed INGEST_SOURCES line (no `->`); (e2) exclude line for a set that
# does not exist; (e3) a library destination whose PARENT is missing.
{ printf 'INGEST_SOURCES="miniserv=//mini-serv/minecraft /srv/library/NonDocs/MiniServ"\n'
  printf 'BACKUP_SOURCES="docs=path:/srv/library/Docs"\n'
  cat /tmp/ing/env.tail; } > /tmp/ing/env.e1
{ printf 'BACKUP_SOURCES="docs=path:/srv/library/Docs\ndoxs.exclude=*.bak"\n'
  cat /tmp/ing/env.tail; } > /tmp/ing/env.e2
{ printf 'INGEST_SOURCES="miniserv=//mini-serv/minecraft -> /srv/nosuchtree/deep/dest"\n'
  printf 'BACKUP_SOURCES="docs=path:/srv/library/Docs"\n'
  cat /tmp/ing/env.tail; } > /tmp/ing/env.e3
echo "library tree + planted Docs + mock curl + configs staged"
SETUP

# ══ scenario (a): ingest mirrors the share into the library, archive covers it ══
echo
echo "== scenario (a): INGEST //mini-serv/minecraft -> $LIB, then archive the library folder =="
rex ': > /tmp/ing/curl.calls; rm -rf /backup/run_*'
rex "bash $BACKUP --config /tmp/ing/env.a >/tmp/ing/run_a.log 2>&1"; rc=$?
RUN_DIR="$(rex 'ls -d /backup/run_* 2>/dev/null | sort | tail -n1' | tr -d '\r')"
loga="$(rex 'cat /tmp/ing/run_a.log')"
ccalls="$(rex 'cat /tmp/ing/curl.calls 2>/dev/null')"

[ "$rc" -eq 0 ] && pass "backup.sh exited 0 (green cycle)" || { fail "backup.sh exit=$rc (expected 0)"; rex 'tail -n 25 /tmp/ing/run_a.log'; }
if has "$loga" "created library destination $LIB"; then
    pass "(a) ingest created the library LEAF on first run"
else
    fail "(a) no leaf-creation log line"; printf '%s\n' "$loga" | grep -i ingest | sed 's/^/      log> /'
fi
if has "$loga" 'source deletions PROPAGATE'; then
    pass "(a) the mirror contract is stated in the run log (deletions propagate)"
else
    fail "(a) the run log does not state the mirror contract"
fi
if rex "mkdir -p /mnt/orig && mount -t cifs //mini-serv/minecraft /mnt/orig -o username=homehub,password=simpass,ro,vers=3.0 && diff -r '$LIB' /mnt/orig >/tmp/ing/diff_a.txt 2>&1; d=\$?; umount /mnt/orig; exit \$d"; then
    pass "(a) library copy is byte-identical to the live share"
else
    fail "(a) library copy differs from the share"; rex 'sed "s/^/      /" /tmp/ing/diff_a.txt' || true
fi
if rex "grep -q '\"ingest\": \"miniserv(' '$RUN_DIR/RUN.json'"; then
    pass "(a) RUN.json records the ingest"
else
    fail "(a) RUN.json has no ingest record"; rex "cat '$RUN_DIR/RUN.json' | sed 's/^/      /'"
fi
if rex "rm -rf /tmp/ing/restore && mkdir -p /tmp/ing/restore && bash $RESTORE --run '$RUN_DIR' --set miniserv --target /tmp/ing/restore >/tmp/ing/restore_a.log 2>&1 && diff -r /tmp/ing/restore '$LIB'"; then
    pass "(a) the ingested set archives + restores BYTE-EQUAL (ordinary archive flow)"
else
    fail "(a) restore drill diff failed"; rex 'tail -n 12 /tmp/ing/restore_a.log' || true
fi
if printf '%s\n' "$ccalls" | grep -Fq '"ok":true'; then pass "(a) NagLight report posted ok=true"; else fail "(a) no ok=true feed captured"; fi

# ══ scenario (b): mirror semantics — library-only files are deleted ════════════
echo
echo "== scenario (b): MIRROR — files that exist only in the library are DELETED =="
rex ": > /tmp/ing/curl.calls; mkdir -p '$LIB/StaleFolder'; echo stale > '$LIB/stale.txt'; echo stale2 > '$LIB/StaleFolder/inner.txt'"
before="$(rex "ls '$LIB/stale.txt' >/dev/null 2>&1 && echo present || echo absent" | tr -d '\r')"
rex "bash $BACKUP --config /tmp/ing/env.a >/tmp/ing/run_b.log 2>&1"; rc=$?
after="$(rex "ls '$LIB/stale.txt' >/dev/null 2>&1 && echo present || echo absent" | tr -d '\r')"
afterdir="$(rex "ls -d '$LIB/StaleFolder' >/dev/null 2>&1 && echo present || echo absent" | tr -d '\r')"
[ "$rc" -eq 0 ] && pass "(b) backup.sh exited 0" || { fail "(b) backup.sh exit=$rc"; rex 'tail -n 20 /tmp/ing/run_b.log'; }
if [ "$before" = present ] && [ "$after" = absent ] && [ "$afterdir" = absent ]; then
    pass "(b) stale file AND stale folder removed by the mirror (source deletions propagate)"
else
    fail "(b) mirror did not delete library-only entries (before=$before after=$after dir=$afterdir)"
fi

# ══ scenario (c): exclusions — global + per-set, and VISIBLE ═══════════════════
echo
echo "== scenario (c): EXCLUSIONS — global '*.bak' + per-set 'docs.exclude=Downloads' =="
rex ': > /tmp/ing/curl.calls'
rex "bash $BACKUP --config /tmp/ing/env.c >/tmp/ing/run_c.log 2>&1"; rc=$?
RUN_C="$(rex 'ls -d /backup/run_* 2>/dev/null | sort | tail -n1' | tr -d '\r')"
logc="$(rex 'cat /tmp/ing/run_c.log')"
ftab="$(rex "cat '$RUN_C/docs.files.tsv' 2>/dev/null")"
tarlist="$(rex "cd /tmp && (zstd -dc '$RUN_C'/docs.tar.zst 2>/dev/null || cat '$RUN_C'/docs.tar) | tar -t")"
manifest="$(rex "cat '$RUN_C/MANIFEST.tsv' 2>/dev/null")"
exlog="$(rex "cat '$RUN_C/docs.excluded.log' 2>/dev/null")"

[ "$rc" -eq 0 ] && pass "(c) backup.sh exited 0 (green cycle)" || { fail "(c) backup.sh exit=$rc"; rex 'tail -n 25 /tmp/ing/run_c.log'; }
if ! has "$ftab" '.bak' && ! has "$ftab" 'Downloads' \
   && ! has "$tarlist" '.bak' && ! has "$tarlist" 'Downloads' \
   && has "$ftab" 'notes.txt' && has "$ftab" 'sub/deep.txt'; then
    pass "(c) excluded paths absent from BOTH the archive and docs.files.tsv; keepers present"
else
    fail "(c) exclusion did not take effect"
    printf '%s\n' "$ftab"    | sed 's/^/      files.tsv> /'
    printf '%s\n' "$tarlist" | sed 's/^/      tar> /'
fi
if rex 'test -f /srv/library/Docs/notes.txt.bak && test -f /srv/library/Docs/Downloads/huge.bin'; then
    pass "(c) the SOURCE still holds the excluded files (exclusion filters the backup, not the library)"
else
    fail "(c) the source lost its excluded files"
fi
if has "$logc" "exclusions: global BACKUP_EXCLUDE='*.bak'" && has "$logc" '[docs] exclude patterns: *.bak Downloads'; then
    pass "(c) the effective patterns are named in the run log (global + per-set)"
else
    fail "(c) patterns not visible in the log"; printf '%s\n' "$logc" | grep -i exclu | sed 's/^/      log> /'
fi
if has "$logc" '[docs] EXCLUDED 3 path(s)' \
   && has "$logc" 'excluded: hiding file notes.txt.bak' \
   && has "$logc" 'excluded: hiding directory Downloads'; then
    pass "(c) every excluded path is named in the run log — never a silent mystery"
else
    fail "(c) excluded paths not itemised in the log"; printf '%s\n' "$logc" | grep -i 'exclud' | sed 's/^/      log> /'
fi
if [ "$(printf '%s\n' "$exlog" | grep -c 'because of pattern')" = 3 ]; then
    pass "(c) docs.excluded.log holds the 3 filter decisions, next to the archive"
else
    fail "(c) docs.excluded.log unexpected"; printf '%s\n' "$exlog" | sed 's/^/      exlog> /'
fi
if printf '%s\n' "$manifest" | awk -F'\t' '$1=="docs" && $9=="*.bak Downloads" {found=1} END{exit !found}'; then
    pass "(c) MANIFEST excludes column records the patterns for the set"
else
    fail "(c) MANIFEST excludes column wrong"; printf '%s\n' "$manifest" | sed 's/^/      manifest> /'
fi
if printf '%s\n' "$manifest" | awk -F'\t' '$1=="miniserv" && $9=="*.bak" {found=1} END{exit !found}' \
   && has "$logc" '[miniserv] exclude patterns: *.bak'; then
    pass "(c) a set with no per-set line still gets the GLOBAL patterns"
else
    fail "(c) global patterns did not apply to the ingested set"; printf '%s\n' "$manifest" | sed 's/^/      manifest> /'
fi
restc="$(rex "rm -rf /tmp/ing/restore_c && mkdir -p /tmp/ing/restore_c && bash $RESTORE --run '$RUN_C' --set docs --target /tmp/ing/restore_c 2>&1")"
if has "$restc" 'FILTERED copy' && has "$restc" 'RESTORE OK'; then
    pass "(c) restore.sh restores the set AND warns it is a FILTERED copy"
else
    fail "(c) restore.sh did not flag the filtered copy"; printf '%s\n' "$restc" | sed 's/^/      restore> /'
fi

# ══ scenario (d): MIRROR SAFETY — an empty share may not wipe the library ══════
echo
echo "== scenario (d): an EMPTY share must NOT mirror-delete a good library copy =="
rex ': > /tmp/ing/curl.calls'
n_before="$(rex "find '$LIB' -type f | wc -l" | tr -d ' \r\n')"
rex "bash $BACKUP --config /tmp/ing/env.d >/tmp/ing/run_d.log 2>&1"; rc=$?
n_after="$(rex "find '$LIB' -type f | wc -l" | tr -d ' \r\n')"
logd="$(rex 'cat /tmp/ing/run_d.log')"
ccalls="$(rex 'cat /tmp/ing/curl.calls')"
[ "$rc" -ne 0 ] && pass "(d) backup.sh exited nonzero (rc=$rc) on the empty source" || fail "(d) backup.sh exited 0 (expected a loud failure)"
if [ "$n_before" = "$n_after" ] && [ "${n_after:-0}" -gt 0 ]; then
    pass "(d) the library copy survived untouched ($n_after file(s))"
else
    fail "(d) library file count changed: $n_before -> $n_after"
fi
if has "$logd" 'REFUSING to mirror-delete'; then pass "(d) the refusal names the reason + the override knob"; else fail "(d) no refusal message"; fi
if printf '%s\n' "$ccalls" | grep -Fq '"ok":false'; then pass "(d) posted ok=false (never-silent-green)"; else fail "(d) no ok=false feed captured"; fi

echo "  -- INGEST_ALLOW_EMPTY=true is the explicit override (the library IS cleared) --"
rex ': > /tmp/ing/curl.calls'
rex "bash $BACKUP --config /tmp/ing/env.d2 >/tmp/ing/run_d2.log 2>&1"; rc=$?
n_over="$(rex "find '$LIB' -type f | wc -l" | tr -d ' \r\n')"
logd2="$(rex 'cat /tmp/ing/run_d2.log')"
if [ "$rc" -eq 0 ] && [ "${n_over:-1}" -eq 0 ] && has "$logd2" 'mirroring the emptiness'; then
    pass "(d) override proceeds, logs that the library WILL be cleared, and clears it"
else
    fail "(d) override behaved unexpectedly (rc=$rc files=$n_over)"; printf '%s\n' "$logd2" | grep -i ingest | sed 's/^/      log> /'
fi

# ══ scenario (e): loud config failures ════════════════════════════════════════
echo
echo "== scenario (e): malformed ingest line / unknown exclude set / missing parent — all LOUD =="
for case in e1 e2 e3; do
    rex ': > /tmp/ing/curl.calls'
    rex "bash $BACKUP --config /tmp/ing/env.$case >/tmp/ing/run_$case.log 2>&1"; rc=$?
    loge="$(rex "cat /tmp/ing/run_$case.log")"
    ccalls="$(rex 'cat /tmp/ing/curl.calls')"
    case "$case" in
        e1) want='bad INGEST_SOURCES line' ;;
        e2) want='no source set named' ;;
        e3) want="library destination's parent does not exist" ;;
    esac
    if [ "$rc" -ne 0 ] && has "$loge" "$want" && printf '%s\n' "$ccalls" | grep -Fq '"ok":false'; then
        pass "($case) died nonzero with '$want' AND posted ok=false"
    else
        fail "($case) rc=$rc, expected message '$want' + ok=false"
        printf '%s\n' "$loge"   | tail -n 5 | sed 's/^/      log> /'
        printf '%s\n' "$ccalls" | sed 's/^/      feed> /'
    fi
done

# ── clean the runner so a subsequent run-backup-sim.sh sees REAL curl ─────────
echo
echo "== cleanup: remove the mock curl shim (leave the runner pristine) =="
rex 'rm -f /usr/local/bin/curl' || true
pass "mock shim removed (/usr/local/bin/curl)"

echo
echo "== summary =="
if [ "$FAILS" -eq 0 ]; then echo "INGEST LEG: PASS (all a-e checks green)"; exit 0; fi
echo "INGEST LEG: FAIL ($FAILS check(s) failed)"; exit 1
