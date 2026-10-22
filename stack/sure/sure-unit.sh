#!/usr/bin/env bash
# sure-unit.sh - the Sure household ledger's lifecycle, owned by systemd
# (homehub-sure.service). SR-100, LLR-1210.
#
# WHY SURE HAS ITS OWN UNIT AND NOT A COMPOSE PROFILE. firstboot runs on EVERY
# boot and its bulk `docker compose up -d` is all-or-nothing: with `sure` in
# COMPOSE_PROFILES that command waits for Sure's Postgres and Redis to be
# healthy, so one sick finance database would stop firstboot before DNS
# provisioning, Samba and the recovery supervisor (Codex gpt-6.1-sol, the
# 2026-10-18 Sure deploy review). The hub's config dropped `sure` from
# COMPOSE_PROFILES that day; this unit is the owner that review asked for, on
# the pattern the crossplay relay and RustDesk already use: a knob
# (SURE_ENABLED) instead of a profile name, and one unit that is the only
# thing passing `--profile sure`.
#
# WHAT IT DOES NOT CHANGE: the four services keep `restart: unless-stopped`.
# Unlike RustDesk there is no fence that has to be up first, so there is nothing
# for docker's own restart to race; keeping the policy means a crashed Sure
# container, or a docker.service restart (needrestart does those), brings Sure
# back without this unit having to notice. The unit adds the two things the
# policy cannot: a start after a stop (ExecStop marks the containers stopped, so
# the next boot is this unit's job) and an off switch that a reboot honours.
#
# Usage: sure-unit.sh condition|start|stop
#   condition  exit 0 when SURE_ENABLED is exactly `true` in the stack .env,
#              1 otherwise (systemd reads 1 from ExecCondition= as a clean SKIP,
#              never a failure, so a box with Sure off shows nothing red).
#   start      `docker compose --profile sure up -d` of the four services, with
#              no build and no pull: the fork has no registry home and a missing
#              image must fail here rather than be fetched from docker.io.
#   stop       stop the four, web and worker before their database and cache.
#
# Config: STACK_DIR (default /opt/homehub/stack), ENV_FILE (default
#         $STACK_DIR/.env), DOCKER (default: docker on PATH). Test seams only.
# Exit:   0 ok; 1 failed or (condition) off; 2 usage.
set -uo pipefail

STACK_DIR="${STACK_DIR:-/opt/homehub/stack}"
ENV_FILE="${ENV_FILE:-$STACK_DIR/.env}"
PROFILE="sure"
# Order matters only for stop; `up` follows the compose file's depends_on.
SERVICES=(sure-postgres sure-redis sure-web sure-worker)
STOP_ORDER=(sure-web sure-worker sure-redis sure-postgres)
STOP_GRACE_SECONDS="${SURE_STOP_GRACE_SECONDS:-60}"

log() { printf '[homehub-sure] %s\n' "$*"; }

DOCKER="${DOCKER:-$(command -v docker 2>/dev/null || true)}"
[ -n "$DOCKER" ] || DOCKER=/usr/bin/docker

# env_value KEY: KEY's value in $ENV_FILE as compose reads it for a simple
# line - the last assignment wins, one pair of surrounding quotes is removed, a
# trailing CR from a Windows-saved file is dropped. The same reading the relay
# unit uses (game-isolation/gunmaster3-relay.sh); not shared code because this
# runs on the box from stack/, where scripts/lib is not guaranteed.
env_value() {
    local v
    v="$(sed -n "s/^[[:space:]]*$1[[:space:]]*=//p" "$ENV_FILE" 2>/dev/null | tail -n1)"
    v="${v%$'\r'}"
    case "$v" in
        \"*\") v="${v#\"}"; v="${v%\"}" ;;
        \'*\') v="${v#\'}"; v="${v%\'}" ;;
    esac
    printf '%s' "$v"
}

# EXACTLY `true`, the deployer's boolean convention: HomeHub's backup-row
# generator (ENABLED_BY=SURE_ENABLED) and firstboot read the same knob the same
# way, so Sure running and Sure being backed up cannot disagree about it.
cmd_condition() {
    local v
    v="$(env_value SURE_ENABLED)"
    if [ "$v" = true ]; then
        log "SURE_ENABLED=true - Sure is in scope for this box"
        return 0
    fi
    log "SURE_ENABLED is '${v:-unset}' - Sure is OFF on this box, skipping (this is not a failure)"
    return 1
}

cmd_start() {
    cd "$STACK_DIR" || { log "FATAL: cannot cd to $STACK_DIR"; return 1; }
    if ! "$DOCKER" compose --profile "$PROFILE" up -d --no-build --pull never "${SERVICES[@]}"; then
        log "FATAL: 'docker compose --profile $PROFILE up -d' failed in $STACK_DIR."
        log "  Check: docker compose --profile $PROFILE ps; docker logs sure-web --tail 50;"
        log "  and that the fork image exists locally (sure-auroleap:local has no registry home)."
        return 1
    fi
    log "compose reports the four Sure services started"
}

cmd_stop() {
    cd "$STACK_DIR" || { log "FATAL: cannot cd to $STACK_DIR"; return 1; }
    if ! "$DOCKER" compose --profile "$PROFILE" stop -t "$STOP_GRACE_SECONDS" "${STOP_ORDER[@]}"; then
        log "FATAL: 'docker compose --profile $PROFILE stop' failed in $STACK_DIR"
        return 1
    fi
    log "the four Sure services are stopped"
}

case "${1:-}" in
    condition) cmd_condition ;;
    start) cmd_start ;;
    stop) cmd_stop ;;
    *) echo "usage: $0 condition|start|stop" >&2; exit 2 ;;
esac
