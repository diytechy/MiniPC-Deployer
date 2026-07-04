#!/usr/bin/env bash
# run-drivepower-sim.sh — WI-10.10 DRIVE POWER DESIGN: prove the hdparm standby
# CALL CONTRACT of the bash backup service without any real spinning platters.
#
# Platters can't spin in a container, but the *contract* can: we put a MOCK
# `hdparm` on PATH inside the backup-runner (it logs every invocation and exits
# 0), a MOCK `curl` (captures each NagLight POST body so ok=true/false is
# assertable without the tracker), and run REAL backup cycles against the Samba
# fixtures. Then we assert the exact sequence the design requires:
#
#   (a) `-S 0` (standby DISABLED) issued to EACH configured device at run start;
#   (b) the configured timeout value re-issued to EACH device on NORMAL exit;
#   (c) on a FORCED mid-run failure (mock rsync exits 1), the restore STILL fires
#       via the EXIT trap AND the run STILL posts ok=false (never-silent-green);
#   (d) with NO devices configured, ZERO hdparm calls and an unchanged green run.
#
# Prereqs (same as run-backup-sim.sh): the awow-sim stack up (sim/run-sim.sh) for
# the shared network, and this repo's stack/backup mounted into the runner.
#
# Usage:
#   run-drivepower-sim.sh          # all four scenarios (a-d)
#   run-drivepower-sim.sh --down   # tear down mini-serv-sim (same as run-backup-sim.sh)
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
CO=(docker compose -p mini-serv-sim -f docker-compose.yml)
BACKUP=/opt/awow-core/stack/backup/backup.sh

case "${1:-}" in
    --down) "${CO[@]}" down -v; exit 0 ;;
esac

if ! docker network inspect awow-sim_default >/dev/null 2>&1; then
    echo "ERROR: network awow-sim_default not found — run sim/run-sim.sh first." >&2
    exit 2
fi

FAILS=0
pass() { printf '  [PASS] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; FAILS=$((FAILS+1)); }
rex()  { docker exec backup-runner bash -c "$1"; }
# has HAYSTACK NEEDLE : fixed-string line match (needles here start with '-S').
has()  { printf '%s\n' "$1" | grep -Fq -- "$2"; }

echo "== build + up mini-serv-sim (samba + privileged runner) =="
"${CO[@]}" up -d --build

echo "== wait for Samba to accept a cifs mount AND serve a fixture file =="
if rex 'for i in $(seq 1 30); do mkdir -p /mnt/probe; if mount -t cifs //mini-serv/minecraft /mnt/probe -o username=awow,password=simpass,ro,vers=3.0 2>/dev/null; then if [ -s /mnt/probe/server.properties ]; then umount /mnt/probe; echo ready; exit 0; fi; umount /mnt/probe; fi; sleep 2; done; exit 1'; then
    pass "Samba share mountable"
else
    fail "Samba never became mountable"; echo "== summary =="; echo "DRIVE-POWER LEG: FAIL"; exit 1
fi

# ── install the mock shims + fake devices + test configs inside the runner ────
echo "== install mock hdparm/curl shims, fake by-id devices, and test configs =="
docker exec -i backup-runner bash -s <<'SETUP'
set -eu
mkdir -p /tmp/dp
: > /tmp/dp/hdparm.calls
: > /tmp/dp/curl.calls

# Fake stable-path devices standing in for /dev/disk/by-id/... backup drives.
touch /tmp/dp/dev1 /tmp/dp/dev2

# Mock hdparm: log the full arg vector (e.g. "-S 0 /tmp/dp/dev1"), succeed.
# /usr/local/bin precedes /usr/bin on PATH, and no real hdparm is installed, so
# common.sh's `command -v hdparm` resolves to this shim.
cat > /usr/local/bin/hdparm <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> /tmp/dp/hdparm.calls
exit 0
SH
chmod +x /usr/local/bin/hdparm

# Mock curl: capture the POST body (the -d value) so the NagLight ok=true/false
# report is assertable with no tracker, and echo the http code feed_naglight
# expects (it reads stdout via -w '%{http_code}').
cat > /usr/local/bin/curl <<'SH'
#!/usr/bin/env bash
prev=""; body=""
for a in "$@"; do
    [ "$prev" = "-d" ] && body="$a"
    prev="$a"
done
printf '%s\n' "$body" >> /tmp/dp/curl.calls
printf '200'
SH
chmod +x /usr/local/bin/curl

# Mock rsync TEMPLATE (toggled into /usr/local/bin/rsync only for scenario c).
cat > /tmp/dp/rsync.mock <<'SH'
#!/usr/bin/env bash
echo "mock rsync: forced failure (drive-power fault injection)" >&2
exit 1
SH
chmod +x /tmp/dp/rsync.mock

# Test config WITH devices (offsite disabled — this leg tests power, not push).
cat > /tmp/dp/backup.env <<'ENV'
BACKUP_TARGET=/backup
BACKUP_STAGING=/var/tmp/awow-backup/staging
BACKUP_KEEP=3
BACKUP_SOURCES="minecraft=//mini-serv/minecraft
satisfactory=//mini-serv/satisfactory"
BACKUP_CIFS_USER=awow
BACKUP_CIFS_PASS=simpass
BACKUP_CIFS_EXTRA=vers=3.0
BACKUP_ZSTD_LEVEL=10
BACKUP_INCOMPRESSIBLE_THRESHOLD=60
OFFSITE_ENABLED=false
NAGLIGHT_FEED_URL=http://mock-tracker.invalid/api/feed
NAGLIGHT_FEED_CHECK=backup
NAGLIGHT_TOKEN=sim-drivepower-token
NAGLIGHT_USER=sim-user-alice-0001
BACKUP_DRIVE_DEVICES="/tmp/dp/dev1 /tmp/dp/dev2"
BACKUP_DRIVE_STANDBY=241
ENV

# Same, but NO devices (scenario d — clean no-op path).
sed 's#^BACKUP_DRIVE_DEVICES=.*#BACKUP_DRIVE_DEVICES=""#' /tmp/dp/backup.env > /tmp/dp/backup.env.nodev
echo "shims + configs staged"
SETUP

# ══ scenario (a)+(b): green run WITH devices ═════════════════════════════════
echo
echo "== scenario (a)+(b): green cycle with 2 devices — disable at start, restore on exit =="
rex 'rm -f /usr/local/bin/rsync; : > /tmp/dp/hdparm.calls; : > /tmp/dp/curl.calls'
rex "bash $BACKUP --config /tmp/dp/backup.env >/tmp/dp/run_ab.log 2>&1"; rc=$?
hcalls="$(rex 'cat /tmp/dp/hdparm.calls 2>/dev/null')"
ccalls="$(rex 'cat /tmp/dp/curl.calls 2>/dev/null')"
nlines="$(printf '%s\n' "$hcalls" | grep -c . || true)"
first="$(printf '%s\n' "$hcalls" | grep . | sed -n '1p')"
last="$(printf '%s\n' "$hcalls" | grep . | sed -n '$p')"

[ "$rc" -eq 0 ] && pass "backup.sh exited 0 (green cycle)" || { fail "backup.sh exit=$rc (expected 0)"; rex 'tail -n 20 /tmp/dp/run_ab.log'; }
# (a) disable -S 0 to EACH device at start
if has "$hcalls" '-S 0 /tmp/dp/dev1' && has "$hcalls" '-S 0 /tmp/dp/dev2'; then
    pass "(a) hdparm -S 0 issued to EACH device at run start"
else
    fail "(a) missing -S 0 for one/both devices"; printf '%s\n' "$hcalls" | sed 's/^/      hdparm> /'
fi
# (b) configured timeout re-issued to EACH device on normal exit
if has "$hcalls" '-S 241 /tmp/dp/dev1' && has "$hcalls" '-S 241 /tmp/dp/dev2'; then
    pass "(b) configured timeout -S 241 re-issued to EACH device on normal exit"
else
    fail "(b) missing -S 241 restore for one/both devices"; printf '%s\n' "$hcalls" | sed 's/^/      hdparm> /'
fi
# ordering + call count: exactly 4 calls, disable(0) before restore(241)
if [ "$nlines" -eq 4 ] && [ "${first#*-S 0}" != "$first" ] && [ "${last#*-S 241}" != "$last" ]; then
    pass "ordering: exactly 4 calls, all -S 0 (disable) precede -S 241 (restore)"
else
    fail "ordering/count wrong (lines=$nlines first='$first' last='$last')"
fi
# feed posted ok=true on the green run
if printf '%s\n' "$ccalls" | grep -Fq '"ok":true'; then pass "NagLight report posted ok=true"; else fail "no ok=true feed captured"; fi

# ══ scenario (c): forced mid-run failure ═════════════════════════════════════
echo
echo "== scenario (c): FORCED mid-run failure (mock rsync exit 1) — restore still fires + ok=false =="
rex 'cp /tmp/dp/rsync.mock /usr/local/bin/rsync; : > /tmp/dp/hdparm.calls; : > /tmp/dp/curl.calls'
rex "bash $BACKUP --config /tmp/dp/backup.env >/tmp/dp/run_c.log 2>&1"; rc=$?
hcalls="$(rex 'cat /tmp/dp/hdparm.calls 2>/dev/null')"
ccalls="$(rex 'cat /tmp/dp/curl.calls 2>/dev/null')"
rex 'rm -f /usr/local/bin/rsync'   # restore real rsync for the next scenario

[ "$rc" -ne 0 ] && pass "backup.sh exited nonzero on the forced failure (rc=$rc)" || fail "backup.sh exited 0 (expected failure)"
if has "$hcalls" '-S 0 /tmp/dp/dev1'; then pass "(c) standby was DISABLED before the failure point"; else fail "(c) no -S 0 before failure"; fi
if has "$hcalls" '-S 241 /tmp/dp/dev1' && has "$hcalls" '-S 241 /tmp/dp/dev2'; then
    pass "(c) restore FIRED on the failure path (EXIT trap) — -S 241 re-issued to each device"
else
    fail "(c) restore did NOT fire after failure"; printf '%s\n' "$hcalls" | sed 's/^/      hdparm> /'
fi
if printf '%s\n' "$ccalls" | grep -Fq '"ok":false'; then
    pass "(c) run still posted ok=false (never-silent-green)"
else
    fail "(c) no ok=false feed captured"; printf '%s\n' "$ccalls" | sed 's/^/      curl> /'
fi
if rex 'grep -q "BACKUP FAILED" /tmp/dp/run_c.log'; then pass "(c) on_err ran (BACKUP FAILED logged, RUN.json=failed)"; else fail "(c) on_err path not observed"; fi

# ══ scenario (d): no devices configured ══════════════════════════════════════
echo
echo "== scenario (d): NO devices configured — zero hdparm calls, unchanged green cycle =="
rex ': > /tmp/dp/hdparm.calls'
rex "bash $BACKUP --config /tmp/dp/backup.env.nodev >/tmp/dp/run_d.log 2>&1"; rc=$?
ncalls="$(rex 'wc -l < /tmp/dp/hdparm.calls' | tr -d ' \r\n')"
[ "$rc" -eq 0 ] && pass "(d) backup.sh exited 0 (green cycle unchanged)" || { fail "(d) backup.sh exit=$rc"; rex 'tail -n 20 /tmp/dp/run_d.log'; }
[ "${ncalls:-1}" -eq 0 ] && pass "(d) ZERO hdparm calls with empty device list (clean no-op)" || fail "(d) expected 0 hdparm calls, got '$ncalls'"

# ── clean the runner so a subsequent run-backup-sim.sh sees REAL hdparm/curl ──
echo
echo "== cleanup: remove mock shims from the runner (leave it pristine) =="
rex 'rm -f /usr/local/bin/hdparm /usr/local/bin/curl /usr/local/bin/rsync; command -v curl' >/dev/null 2>&1 || true
pass "mock shims removed (/usr/local/bin/{hdparm,curl,rsync})"

echo
echo "== summary =="
if [ "$FAILS" -eq 0 ]; then echo "DRIVE-POWER LEG: PASS (all a-d checks green)"; exit 0; fi
echo "DRIVE-POWER LEG: FAIL ($FAILS check(s) failed)"; exit 1
