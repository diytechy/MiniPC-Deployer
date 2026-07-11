#!/usr/bin/env bash
# run-sim.sh — bring up the AWOW-sim (WI-10.14): the REAL stack compose file
# plus the sim overlay, then provision Technitium (the split-horizon zone) the
# same way the autoinstall firstboot does on the real box.
#
#   compose -f stack/docker-compose.yml -f sim/docker-compose.sim.yml \
#           --env-file sim/.env.sim up -d
#
# Idempotent: safe to re-run. Requires the naglight:local image to exist
# (`docker build -t naglight:local ../NagLight`); WI-10.13 built it.
#
# Usage: sim/run-sim.sh [--down] [extra `compose up` args...]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

COMPOSE=(docker compose -p awow-sim
         -f stack/docker-compose.yml
         -f sim/docker-compose.sim.yml
         --env-file sim/.env.sim)

if [[ "${1:-}" == "--down" ]]; then
    echo "== tearing down AWOW-sim =="
    "${COMPOSE[@]}" down -v --remove-orphans
    exit 0
fi

echo "== AWOW-sim: resolve locally-built images (present -> sibling -> public, SR-006) =="
bash scripts/ensure-local-images.sh

echo "== AWOW-sim: config sanity (real compose + overlay resolves) =="
"${COMPOSE[@]}" config -q

echo "== AWOW-sim: bringing the stack up =="
"${COMPOSE[@]}" up -d "$@"

# Wait for Technitium to answer before provisioning its zone.
wait_healthy() {
    local svc="$1" tries="${2:-60}" i=0 cid st
    while (( i < tries )); do
        cid="$("${COMPOSE[@]}" ps -q "$svc" 2>/dev/null || true)"
        if [[ -n "$cid" ]]; then
            st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo '')"
            [[ "$st" == "healthy" || "$st" == "running" ]] && { echo "  $svc: $st"; return 0; }
        fi
        sleep 3; (( i++ )) || true
    done
    echo "  $svc: did not become ready in time" >&2
    return 1
}

echo "== waiting for Technitium =="
wait_healthy technitium 60

echo "== provisioning Technitium split-horizon zone (sim) =="
# The real firstboot runs this against 127.0.0.1:5380 on the host; the sim
# publishes the API on an alt loopback port to avoid clashing with anything.
bash stack/provision/provision-technitium.sh \
    --host "http://127.0.0.1:${SIM_TECHNITIUM_API_PORT:-5381}" \
    --env  "$REPO_ROOT/sim/.env.sim"

echo "== AWOW-sim up. Run sim/validate-sim.sh for the V1 gate. =="
"${COMPOSE[@]}" ps
