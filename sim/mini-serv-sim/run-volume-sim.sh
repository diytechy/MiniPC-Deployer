#!/usr/bin/env bash
# run-volume-sim.sh — SR-013 (OI-8): prove the docker-VOLUME source leg of the
# bash backup service — the volume:VOL[@CONTAINER] grammar in the single
# BACKUP_SOURCES table — without real docker inside the runner.
#
# Same trick as run-drivepower-sim.sh: real docker can't run inside the
# backup-runner, but the *call contract* can — a MOCK `docker` on PATH logs
# every invocation, answers `volume inspect` with a fixture dir standing in for
# the volume mountpoint, and accepts stop/start. A MOCK `curl` captures the
# NagLight POST bodies. The REAL backup.sh then runs full cycles and we assert
# (SR-013 AcceptanceCriteria):
#
#   (a) a `volume:` set is archived+hashed like any set, restores BYTE-EQUAL via
#       restore.sh, issues ZERO stop/start (no @container), and comment lines in
#       the sources table are skipped;
#   (b) `volume:VOL@CONTAINER` quiesces: `docker stop C` BEFORE the copy and
#       `docker start C` after, exactly once each, on a green run;
#   (c) a FORCED mid-copy failure (mock rsync exits 1) STILL restarts the
#       container (EXIT trap) AND still posts ok=false + nonzero (never-silent-
#       green; a failed backup must never leave a service down);
#   (d) a cifs-only config makes ZERO docker calls (backward compatible).
#
# Prereq: only the mini-serv-sim samba fixtures (scenario a pulls one cifs set
# alongside the volume set). The NagLight feed is MOCKED, so unlike
# run-backup-sim.sh this leg does NOT need the awow-sim stack — if the shared
# network is absent we create it standalone.
#
# Usage:
#   run-volume-sim.sh          # all four scenarios (a-d)
#   run-volume-sim.sh --down   # tear down mini-serv-sim (same as siblings)
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
CO=(docker compose -p mini-serv-sim -f docker-compose.yml)
BACKUP=/opt/awow-core/stack/backup/backup.sh
RESTORE=/opt/awow-core/stack/backup/restore.sh

case "${1:-}" in
    --down) "${CO[@]}" down -v; exit 0 ;;
esac

# The feed is mocked here, so the tracker isn't needed — create the shared
# network standalone if the awow-sim stack isn't up (deviation from siblings,
# on purpose).
if ! docker network inspect awow-sim_default >/dev/null 2>&1; then
    echo "NOTE: awow-sim_default not found — creating it standalone (feed is mocked; tracker not needed)."
    docker network create awow-sim_default >/dev/null
fi

FAILS=0
pass() { printf '  [PASS] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; FAILS=$((FAILS+1)); }
rex()  { docker exec backup-runner bash -c "$1"; }
has()  { printf '%s\n' "$1" | grep -Fq -- "$2"; }

echo "== build + up mini-serv-sim (samba + privileged runner) =="
"${CO[@]}" up -d --build

echo "== wait for Samba to accept a cifs mount AND serve a fixture file =="
if rex 'for i in $(seq 1 30); do mkdir -p /mnt/probe; if mount -t cifs //mini-serv/minecraft /mnt/probe -o username=awow,password=simpass,ro,vers=3.0 2>/dev/null; then if [ -s /mnt/probe/server.properties ]; then umount /mnt/probe; echo ready; exit 0; fi; umount /mnt/probe; fi; sleep 2; done; exit 1'; then
    pass "Samba share mountable"
else
    fail "Samba never became mountable"; echo "== summary =="; echo "VOLUME LEG: FAIL"; exit 1
fi

# ── install the mock shims + fixture "volumes" + test configs in the runner ───
echo "== install mock docker/curl shims, fixture volume dirs, and test configs =="
docker exec -i backup-runner bash -s <<'SETUP'
set -eu
mkdir -p /tmp/vp
: > /tmp/vp/docker.calls
: > /tmp/vp/curl.calls

# Fixture dirs standing in for docker volume MOUNTPOINTS. actual_data gets a
# binary-ish "database" (dd) + a text file so hashing/compression see both.
mkdir -p /tmp/vp/vol_actual/subdir /tmp/vp/vol_tracker
dd if=/dev/urandom of=/tmp/vp/vol_actual/budget.sqlite bs=1024 count=64 2>/dev/null
echo '{"fixture":"actual","fake":true}' > /tmp/vp/vol_actual/config.json
echo 'nested file for the diff'        > /tmp/vp/vol_actual/subdir/nested.txt
echo 'sim tracker definitions'         > /tmp/vp/vol_tracker/definitions.yml

# Mock docker: log every invocation; answer `volume inspect ... VOL` with the
# fixture mountpoint; accept stop/start. /usr/local/bin precedes /usr/bin and no
# real docker exists in the runner, so backup.sh's `command -v docker` finds it.
cat > /usr/local/bin/docker <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> /tmp/vp/docker.calls
case "$1" in
    volume)
        vol="${@: -1}"
        case "$vol" in
            actual_data)  echo /tmp/vp/vol_actual ;;
            tracker_data) echo /tmp/vp/vol_tracker ;;
            *) echo "Error: no such volume: $vol" >&2; exit 1 ;;
        esac ;;
    stop|start) exit 0 ;;
    *) exit 0 ;;
esac
SH
chmod +x /usr/local/bin/docker

# Mock curl: capture each POST body (-d value); print the 200 feed_naglight reads.
cat > /usr/local/bin/curl <<'SH'
#!/usr/bin/env bash
prev=""; body=""
for a in "$@"; do
    [ "$prev" = "-d" ] && body="$a"
    prev="$a"
done
printf '%s\n' "$body" >> /tmp/vp/curl.calls
printf '200'
SH
chmod +x /usr/local/bin/curl

# Mock rsync TEMPLATE (toggled in only for scenario c's forced mid-copy failure).
cat > /tmp/vp/rsync.mock <<'SH'
#!/usr/bin/env bash
echo "mock rsync: forced failure (volume-leg fault injection)" >&2
exit 1
SH
chmod +x /tmp/vp/rsync.mock

# (a) cifs set + live volume set + a comment line that must be SKIPPED.
cat > /tmp/vp/backup.env.a <<'ENV'
BACKUP_TARGET=/backup
BACKUP_STAGING=/var/tmp/awow-backup/staging
BACKUP_KEEP=3
BACKUP_SOURCES="minecraft=//mini-serv/minecraft
# commented=//mini-serv/minecraft
actual=volume:actual_data"
BACKUP_CIFS_USER=awow
BACKUP_CIFS_PASS=simpass
BACKUP_CIFS_EXTRA=vers=3.0
BACKUP_ZSTD_LEVEL=10
BACKUP_INCOMPRESSIBLE_THRESHOLD=60
OFFSITE_ENABLED=false
NAGLIGHT_FEED_URL=http://mock-tracker.invalid/api/feed
NAGLIGHT_FEED_CHECK=backup
NAGLIGHT_TOKEN=sim-volume-token
NAGLIGHT_USER=sim-user-alice-0001
BACKUP_DRIVE_DEVICES=""
BACKUP_DRIVE_STANDBY=241
ENV

# Shared tail for the b/c/d configs (everything but BACKUP_SOURCES).
cat > /tmp/vp/env.tail <<'ENV'
BACKUP_CIFS_USER=awow
BACKUP_CIFS_PASS=simpass
BACKUP_CIFS_EXTRA=vers=3.0
BACKUP_ZSTD_LEVEL=10
BACKUP_INCOMPRESSIBLE_THRESHOLD=60
OFFSITE_ENABLED=false
NAGLIGHT_FEED_URL=http://mock-tracker.invalid/api/feed
NAGLIGHT_FEED_CHECK=backup
NAGLIGHT_TOKEN=sim-volume-token
NAGLIGHT_USER=sim-user-alice-0001
BACKUP_DRIVE_DEVICES=""
BACKUP_DRIVE_STANDBY=241
ENV
common_head() { printf 'BACKUP_TARGET=/backup\nBACKUP_STAGING=/var/tmp/awow-backup/staging\nBACKUP_KEEP=3\n'; }

# (b) cifs + live volume + a QUIESCED volume set (@tracker).
{ common_head
  printf 'BACKUP_SOURCES="minecraft=//mini-serv/minecraft\nactual=volume:actual_data\ntracker=volume:tracker_data@tracker"\n'
  cat /tmp/vp/env.tail; } > /tmp/vp/backup.env.b

# (c) quiesced set ONLY (no cifs — the forced rsync failure must hit mid-quiesce).
{ common_head
  printf 'BACKUP_SOURCES="tracker=volume:tracker_data@tracker"\n'
  cat /tmp/vp/env.tail; } > /tmp/vp/backup.env.c

# (d) cifs-only (grammar untouched — the backward-compat path).
{ common_head
  printf 'BACKUP_SOURCES="minecraft=//mini-serv/minecraft"\n'
  cat /tmp/vp/env.tail; } > /tmp/vp/backup.env.d
echo "shims + fixtures + configs staged"
SETUP

# ══ scenario (a): live volume set + cifs set + skipped comment line ═══════════
echo
echo "== scenario (a): volume: set archived+hashed+restored; zero stop/start; comment skipped =="
rex 'rm -f /usr/local/bin/rsync; : > /tmp/vp/docker.calls; : > /tmp/vp/curl.calls; rm -rf /backup/run_*'
rex "bash $BACKUP --config /tmp/vp/backup.env.a >/tmp/vp/run_a.log 2>&1"; rc=$?
RUN_DIR="$(rex 'ls -d /backup/run_* 2>/dev/null | sort | tail -n1' | tr -d '\r')"
dcalls="$(rex 'cat /tmp/vp/docker.calls 2>/dev/null')"
ccalls="$(rex 'cat /tmp/vp/curl.calls 2>/dev/null')"
manifest="$(rex "cat '$RUN_DIR/MANIFEST.tsv' 2>/dev/null")"

[ "$rc" -eq 0 ] && pass "backup.sh exited 0 (green cycle)" || { fail "backup.sh exit=$rc (expected 0)"; rex 'tail -n 20 /tmp/vp/run_a.log'; }
if has "$manifest" 'actual' && rex "ls '$RUN_DIR'/actual.tar* >/dev/null 2>&1"; then
    pass "(a) volume set 'actual' archived + in MANIFEST like any set"
else
    fail "(a) volume set missing from run artifacts"; printf '%s\n' "$manifest" | sed 's/^/      manifest> /'
fi
if has "$dcalls" 'volume inspect' && ! has "$dcalls" 'stop' && ! has "$dcalls" 'start'; then
    pass "(a) volume resolved via docker volume inspect; ZERO stop/start (no @container)"
else
    fail "(a) unexpected docker calls"; printf '%s\n' "$dcalls" | sed 's/^/      docker> /'
fi
if ! has "$manifest" 'commented'; then
    pass "(a) comment line in BACKUP_SOURCES skipped"
else
    fail "(a) comment line was treated as a set"
fi
if printf '%s\n' "$ccalls" | grep -Fq '"ok":true'; then pass "(a) NagLight report posted ok=true"; else fail "(a) no ok=true feed captured"; fi

echo "  -- restore drill: reconstruct 'actual' and byte-diff vs the fixture volume --"
if rex "rm -rf /tmp/vp/restore && mkdir -p /tmp/vp/restore && bash $RESTORE --run '$RUN_DIR' --set actual --target /tmp/vp/restore >/tmp/vp/restore_a.log 2>&1 && diff -r /tmp/vp/restore /tmp/vp/vol_actual"; then
    pass "(a) restore drill: volume set reconstructs BYTE-EQUAL"
else
    fail "(a) restore drill diff failed"; rex 'tail -n 10 /tmp/vp/restore_a.log' || true
fi

# ══ scenario (b): quiesced volume set — stop before copy, start after ═════════
echo
echo "== scenario (b): volume:VOL@CONTAINER — stop precedes copy, start follows, green run =="
rex ': > /tmp/vp/docker.calls; : > /tmp/vp/curl.calls'
rex "bash $BACKUP --config /tmp/vp/backup.env.b >/tmp/vp/run_b.log 2>&1"; rc=$?
dcalls="$(rex 'cat /tmp/vp/docker.calls 2>/dev/null')"
ccalls="$(rex 'cat /tmp/vp/curl.calls 2>/dev/null')"
stops="$(printf '%s\n' "$dcalls" | grep -c '^stop tracker' || true)"
starts="$(printf '%s\n' "$dcalls" | grep -c '^start tracker' || true)"
stop_ln="$(printf '%s\n' "$dcalls" | grep -n '^stop tracker' | head -n1 | cut -d: -f1)"
start_ln="$(printf '%s\n' "$dcalls" | grep -n '^start tracker' | head -n1 | cut -d: -f1)"

[ "$rc" -eq 0 ] && pass "backup.sh exited 0 (green cycle)" || { fail "backup.sh exit=$rc (expected 0)"; rex 'tail -n 20 /tmp/vp/run_b.log'; }
if [ "$stops" -eq 1 ] && [ "$starts" -eq 1 ] && [ -n "$stop_ln" ] && [ -n "$start_ln" ] && [ "$stop_ln" -lt "$start_ln" ]; then
    pass "(b) quiesce contract: exactly one stop, one start, stop precedes start"
else
    fail "(b) quiesce ordering/count wrong (stops=$stops starts=$starts stop@$stop_ln start@$start_ln)"
    printf '%s\n' "$dcalls" | sed 's/^/      docker> /'
fi
if printf '%s\n' "$ccalls" | grep -Fq '"ok":true'; then pass "(b) NagLight report posted ok=true"; else fail "(b) no ok=true feed captured"; fi

# ══ scenario (c): forced mid-copy failure while quiesced ══════════════════════
echo
echo "== scenario (c): FORCED mid-copy failure — container STILL restarted + ok=false =="
rex 'cp /tmp/vp/rsync.mock /usr/local/bin/rsync; : > /tmp/vp/docker.calls; : > /tmp/vp/curl.calls'
rex "bash $BACKUP --config /tmp/vp/backup.env.c >/tmp/vp/run_c.log 2>&1"; rc=$?
dcalls="$(rex 'cat /tmp/vp/docker.calls 2>/dev/null')"
ccalls="$(rex 'cat /tmp/vp/curl.calls 2>/dev/null')"
rex 'rm -f /usr/local/bin/rsync'   # real rsync back for scenario (d)

[ "$rc" -ne 0 ] && pass "backup.sh exited nonzero on the forced failure (rc=$rc)" || fail "backup.sh exited 0 (expected failure)"
if has "$dcalls" 'stop tracker' && has "$dcalls" 'start tracker'; then
    pass "(c) container restarted on the failure path (EXIT trap) — never left down"
else
    fail "(c) stop/start pair not observed on failure"; printf '%s\n' "$dcalls" | sed 's/^/      docker> /'
fi
if printf '%s\n' "$ccalls" | grep -Fq '"ok":false'; then
    pass "(c) run still posted ok=false (never-silent-green)"
else
    fail "(c) no ok=false feed captured"; printf '%s\n' "$ccalls" | sed 's/^/      curl> /'
fi
if rex 'grep -q "BACKUP FAILED" /tmp/vp/run_c.log'; then pass "(c) on_err ran (BACKUP FAILED logged)"; else fail "(c) on_err path not observed"; fi

# ══ scenario (d): cifs-only config — zero docker calls ════════════════════════
echo
echo "== scenario (d): cifs-only config — ZERO docker calls (backward compatible) =="
rex ': > /tmp/vp/docker.calls'
rex "bash $BACKUP --config /tmp/vp/backup.env.d >/tmp/vp/run_d.log 2>&1"; rc=$?
ncalls="$(rex 'grep -c . /tmp/vp/docker.calls || true' | tr -d ' \r\n')"
[ "$rc" -eq 0 ] && pass "(d) backup.sh exited 0 (green cycle unchanged)" || { fail "(d) backup.sh exit=$rc"; rex 'tail -n 20 /tmp/vp/run_d.log'; }
[ "${ncalls:-1}" -eq 0 ] && pass "(d) ZERO docker calls with a cifs-only table" || fail "(d) expected 0 docker calls, got '$ncalls'"

# ── clean the runner so a subsequent run-backup-sim.sh sees REAL curl ─────────
echo
echo "== cleanup: remove mock shims from the runner (leave it pristine) =="
rex 'rm -f /usr/local/bin/docker /usr/local/bin/curl /usr/local/bin/rsync' || true
pass "mock shims removed (/usr/local/bin/{docker,curl,rsync})"

echo
echo "== summary =="
if [ "$FAILS" -eq 0 ]; then echo "VOLUME LEG: PASS (all a-d checks green)"; exit 0; fi
echo "VOLUME LEG: FAIL ($FAILS check(s) failed)"; exit 1
