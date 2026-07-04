#!/usr/bin/env bash
# run-backup-sim.sh — WI-10.15 backup leg: stand up the Samba fixtures + a
# privileged runner, run the REAL stack/backup service through its full six-step
# cycle against the fixtures, then do the RESTORE DRILL (delete a subtree,
# reconstruct from archive+manifest, diff-verify byte equality).
#
# Requires the awow-sim stack up first (sim/run-sim.sh) for the NagLight feed +
# the shared network.
#
# Usage:
#   run-backup-sim.sh                # full cycle + restore drill
#   run-backup-sim.sh --shares-only  # just the Samba fixtures (WI-10.16 uses this)
#   run-backup-sim.sh --down         # tear down mini-serv-sim
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
CO=(docker compose -p mini-serv-sim -f docker-compose.yml)

case "${1:-}" in
    --down) "${CO[@]}" down -v; exit 0 ;;
esac

if ! docker network inspect awow-sim_default >/dev/null 2>&1; then
    echo "ERROR: network awow-sim_default not found — run sim/run-sim.sh first." >&2
    exit 2
fi

if [ "${1:-}" = "--shares-only" ]; then
    echo "== bringing up the Samba fixture shares only (WI-10.16) =="
    "${CO[@]}" up -d samba
    echo "Shares up on 'mini-serv': //mini-serv/minecraft, //mini-serv/satisfactory, //mini-serv/icedrive (user awow / simpass)."
    exit 0
fi

FAILS=0
pass() { printf '  [PASS] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; FAILS=$((FAILS+1)); }
rex()  { docker exec backup-runner bash -c "$1"; }

echo "== build + up mini-serv-sim (samba + privileged runner) =="
"${CO[@]}" up -d --build

echo "== wait for Samba to accept a cifs mount AND serve a fixture file =="
if rex 'for i in $(seq 1 30); do mkdir -p /mnt/probe; if mount -t cifs //mini-serv/minecraft /mnt/probe -o username=awow,password=simpass,ro,vers=3.0 2>/dev/null; then if [ -s /mnt/probe/server.properties ] && [ -s /mnt/probe/plugins/SimGreeter.jar ]; then umount /mnt/probe; echo ready; exit 0; fi; umount /mnt/probe; fi; sleep 2; done; exit 1'; then
    pass "Samba share mountable"
else
    fail "Samba never became mountable"; echo "== summary =="; echo "BACKUP LEG: FAIL"; exit 1
fi

echo "== (cycle) run the REAL backup service end-to-end =="
if rex 'bash /opt/awow-core/stack/backup/backup.sh --config /etc/awow-backup/backup.env'; then
    pass "backup.sh completed (exit 0)"
else
    fail "backup.sh failed"
fi

echo "== run artifacts =="
RUN_DIR="$(rex 'ls -d /backup/run_* 2>/dev/null | sort | tail -n1' | tr -d "\r")"
echo "  latest run: $RUN_DIR"
echo "  --- MANIFEST.tsv ---"; rex "cat '$RUN_DIR/MANIFEST.tsv' | sed 's/^/    /'"
echo "  --- RUN.json ---";     rex "cat '$RUN_DIR/RUN.json' | sed 's/^/    /'"
echo "  --- sizes ---";        rex "du -sh '$RUN_DIR'/* 2>/dev/null | sed 's/^/    /'"

echo "== (step 5) verify offsite push landed in the IceDrive share =="
off="$(rex 'mkdir -p /mnt/ice; mount -t cifs //mini-serv/icedrive /mnt/ice -o username=awow,password=simpass,rw,vers=3.0 2>/dev/null && find /mnt/ice/awow-backup -type f 2>/dev/null | wc -l && umount /mnt/ice' | tr -d "\r")"
if [ "${off:-0}" -ge 1 ]; then pass "offsite share holds $off pushed file(s)"; else fail "offsite share empty"; fi

echo "== (step 6) verify the NagLight feed round-trip landed =="
fed="$(rex "curl -s -H 'X-Forwarded-User: sim-user-alice-0001' http://tracker:8787/api/today | grep -o '\"id\":\"backup-files\"[^}]*\"done\":true' | head -n1")"
if [ -n "$fed" ]; then pass "tracker shows backup-files done=true (feed round-trip)"; else fail "feed did not land in /api/today"; fi

echo "== RESTORE DRILL — reconstruct minecraft from archive+manifest, diff byte-equality =="
docker exec -i backup-runner bash -s "$RUN_DIR" <<'DRILL'
set -uo pipefail
RUN_DIR="$1"
rc=0
mkdir -p /mnt/orig /tmp/restore
mount -t cifs //mini-serv/minecraft /mnt/orig -o username=awow,password=simpass,ro,vers=3.0

echo "  [drill] initial reconstruct + verify"
rm -rf /tmp/restore; mkdir -p /tmp/restore
bash /opt/awow-core/stack/backup/restore.sh --run "$RUN_DIR" --set minecraft --target /tmp/restore || rc=1
# restore.sh extracts a tar of '.' so contents land under /tmp/restore/./ -> normalize
RDIR=/tmp/restore
if diff -r "$RDIR" /mnt/orig >/tmp/diff1.txt 2>&1; then
    echo "  [drill] diff vs live source: IDENTICAL (byte-equal)"
else
    echo "  [drill] diff vs live source: DIFFERENCES:"; sed 's/^/      /' /tmp/diff1.txt; rc=1
fi

echo "  [drill] simulate loss: delete the plugins/ subtree, then reconstruct again"
rm -rf "$RDIR/plugins"
bash /opt/awow-core/stack/backup/restore.sh --run "$RUN_DIR" --set minecraft --target /tmp/restore || rc=1
if diff -r "$RDIR" /mnt/orig >/tmp/diff2.txt 2>&1; then
    echo "  [drill] post-loss reconstruct diff: IDENTICAL (plugins recovered)"
else
    echo "  [drill] post-loss diff: DIFFERENCES:"; sed 's/^/      /' /tmp/diff2.txt; rc=1
fi

echo "  [drill] the fixture 'secret' survived the round-trip (present, not leaked elsewhere):"
grep -q 'rcon.password=sim-fake-rcon' "$RDIR/server.properties" && echo "      rcon.password restored intact" || { echo "      rcon.password MISSING"; rc=1; }
echo "  [drill] plugin jars restored: $(ls "$RDIR/plugins" | tr '\n' ' ')"
umount /mnt/orig 2>/dev/null || true
exit $rc
DRILL
if [ $? -eq 0 ]; then pass "restore drill: reconstruct + byte-diff equality (incl. post-loss)"; else fail "restore drill had verification failures"; fi

echo "== summary =="
if [ "$FAILS" -eq 0 ]; then echo "BACKUP LEG: PASS (all checks green)"; exit 0; fi
echo "BACKUP LEG: FAIL ($FAILS check(s) failed)"; exit 1
