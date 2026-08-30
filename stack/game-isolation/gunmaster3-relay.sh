#!/usr/bin/env bash
# gunmaster3-relay.sh — the crossplay relay's HOST-SIDE LIFECYCLE, in one place.
#
# WHY THIS FILE EXISTS, and it is a design change rather than a new feature.
#
# Until 2026-08-29 the relay was a compose service with `restart: unless-stopped`
# gated by the `gunmaster3` profile, and firstboot carried ~40 lines that had to
# detect the profile, validate the secret, install the fence unit, enable it,
# verify the iptables rule, and degrade gracefully if any of that failed — in
# shell, under `set -euo pipefail`, before the rest of the stack started. FOUR
# consecutive adversarial review rounds each found a defect in the previous
# round's fix, and twice the defect would have flashed a box with NO DNS, CADDY,
# ACTUAL OR TRACKER. Two of those rounds are worth restating, because they are
# what this file's shape is a response to:
#
#   * the guard read `$COMPOSE_PROFILES` as a shell variable in a script that
#     never sources .env, so it was a SILENT NO-OP: the fence unit was never
#     installed and a reimaged box would have run the public relay with the host
#     wide open while every log line said success;
#   * the fix for that reimplemented compose's .env parser, and two parsers over
#     one truth disagree about whitespace, quotes and inline comments — all
#     valid compose syntax. When they disagree the guard concludes "off" and
#     compose concludes "on", which is the same unfenced relay again.
#
# The refresh (CROSSPLAY_HANDOFF.md §7) changes WHO OWNS THE LIFECYCLE rather
# than patching the guard a fifth time:
#
#   * `restart: "no"` on the service, so DOCKER never starts the relay;
#   * `profiles: ["gunmaster3"]` stays, so firstboot's bulk `docker compose up -d`
#     never starts it either — only this script ever passes --profile;
#   * a new knob, GAME_RELAY_ENABLED, replaces "is gunmaster3 in COMPOSE_PROFILES";
#   * homehub-gunmaster3-relay.service `Requires=` the fence unit, so systemd —
#     not shell branching — guarantees the ordering AND propagates the failure.
#
# WHAT THAT BUYS, AND IT IS THE WHOLE POINT: reading .env here is no longer
# security-critical. A parser disagreement can now only make the relay NOT
# START, or start BEHIND A FENCE that `Requires=` already guaranteed. There is
# no longer any path from "this script was wrong about a value" to "a public
# service is running with the host reachable from it", because starting the
# relay at all requires a unit that requires the fence.
#
# `set -uo pipefail` AND NOT `-e`, matching game-isolation.sh next door. Under
# `-e` an unchecked non-zero anywhere exits with no message, which is precisely
# the failure mode this whole component is being redesigned out of. Every
# failure path below calls `die`, which SAYS WHAT TO CHECK and then exits 1.
set -uo pipefail

STACK_DIR="${STACK_DIR:-/opt/homehub/stack}"
ENV_FILE="${ENV_FILE:-$STACK_DIR/.env}"
SERVICE="gunmaster3-relay"
PROFILE="gunmaster3"
CONTAINER="gunmaster3-relay"
FENCE_COMMENT="homehub-game-isolation"
# The compose project is the stack directory's name, so the game network is
# <project>_game. Overridable for a lab that stages the payload elsewhere.
GAME_NETWORK="${GAME_NETWORK:-$(basename "$STACK_DIR")_game}"
# How long to wait for a caddy container to EXIST before reading the secret out
# of it. Existence, not running: `.Config.Env` is create-time state and is
# readable from a stopped container, and it is the value the Caddyfile will
# match on the moment caddy is next started.
#
# On a NORMAL REBOOT this unit is ordered After=docker.service, which is
# satisfied the moment dockerd is up — NOT when dockerd has finished restarting
# the containers it owns. Without this wait the precheck would race caddy's own
# restart and fail a perfectly healthy box roughly whenever it lost the race.
CADDY_WAIT_SECS="${CADDY_WAIT_SECS:-120}"

log() { printf '%s [gunmaster3-relay] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "FATAL: $*"; exit 1; }

DOCKER="$(command -v docker 2>/dev/null || true)"
[ -n "$DOCKER" ] && [ -x "$DOCKER" ] || DOCKER=/usr/bin/docker

# env_value KEY — the last KEY= value in $ENV_FILE, quoting and an inline
# comment stripped, `$$` collapsed back to `$`.
#
# IT READS EXACTLY ONE KEY: GAME_RELAY_ENABLED, for the ExecCondition. THE
# SECRET IS NOT READ FROM .env AT ALL any more — see cmd_precheck, which takes
# it from the caddy container's environment and has no file fallback, because
# two sources for one truth is what let a stopped caddy hold a malformed public
# path while this script validated a corrected one (round 6). Whatever this
# function gets wrong can now only decide whether the relay runs, never whether
# it runs unfenced or behind the wrong door.
#
# THIS IS THE SAME PARSER firstboot.sh uses, deliberately duplicated rather than
# sourced: .env is a COMPOSE env file, not a shell script, and sourcing it makes
# bash expand every bcrypt hash in it (a value starting `$2a$14$…`), which under
# `set -u` aborts the caller outright and, with `-u` off, SILENTLY CORRUPTS the
# value. It is also not scripts/lib/envfile.sh, for a plainer reason: that file
# lives in the repo and is NOT part of the /opt/homehub/stack payload contract
# this unit runs from. Same semantics, checked by eye, one key.
env_value() {
    local __v
    __v=$(sed -n "s/^[[:space:]]*$1[[:space:]]*=//p" "$ENV_FILE" 2>/dev/null | tail -n1)
    case "$__v" in
        \"*\") __v=${__v#\"}; __v=${__v%\"} ;;
        \'*\') __v=${__v#\'}; __v=${__v%\'} ;;
        *) __v=${__v%%[[:space:]]#*}
           __v=${__v%"${__v##*[![:space:]]}"} ;;
    esac
    __v=${__v//\$\$/\$}
    printf '%s' "$__v"
}

# THE SUBNET'S SINGLE SOURCE OF TRUTH IS docker-compose.yml, where the `game`
# network pins 172.28.90.0/24. Everything else asks docker what it actually
# handed out, and falls back to that same pin.
#
# THERE IS DELIBERATELY NO `.env` READ HERE. An earlier version fell back to
# `env_value GAME_SUBNET`, which invented a THIRD source: game-isolation.sh
# reads GAME_SUBNET from its PROCESS ENVIRONMENT (unreachable in practice - the
# fence unit sets no Environment=, so it always uses the same 172.28.90.0/24
# literal), while this read .env. Two readers, two sources, one of them dead.
# Had anyone ever set GAME_SUBNET in .env, the fence would have gone on
# rejecting the compose subnet while this checked a different one - and the
# check would have passed over a fence that guarded nothing.
#
# fence_subnet — the subnet the relay's containers actually get, asked of docker
# rather than assumed, falling back to the compose pin so a postcheck run before
# the network exists still asserts SOMETHING rather than silently asserting
# nothing about an empty string.
GAME_SUBNET_PIN="172.28.90.0/24"
fence_subnet() {
    local __s
    __s="$("$DOCKER" network inspect "$GAME_NETWORK" \
             -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null)"
    [ -n "$__s" ] || __s="$GAME_SUBNET_PIN"
    printf '%s' "$__s"
}

# ── condition ────────────────────────────────────────────────────────────────
# ExecCondition=. Exit 0 = run the unit; exit 1 = "condition not met", which
# systemd records as a CLEAN SKIP, not a failure. That distinction is the reason
# the knob is read here rather than branched on in firstboot: a box with the
# relay switched off gets `systemctl status` saying the condition failed, and
# nothing anywhere reports red for a service it was never asked to run.
cmd_condition() {
    local v
    v="$(env_value GAME_RELAY_ENABLED)"
    case "$v" in
        true|TRUE|True|yes|1)
            log "GAME_RELAY_ENABLED=$v — the relay is in scope for this box"
            return 0 ;;
        *)
            log "GAME_RELAY_ENABLED is '${v:-unset}' — the relay is OFF on this box, skipping (this is not a failure)"
            return 1 ;;
    esac
}

# ── precheck ─────────────────────────────────────────────────────────────────
# ExecStartPre=. THE SECRET IS THE ONLY DOOR: the Caddyfile matches
# `/ws-{$GAME_WS_SECRET}` and nothing else stands between the public internet
# and this relay. Empty, that matcher collapses to `^/ws-$` and the bearer
# credential becomes the literal string "/ws-". 43 base64url characters is
# exactly what New-RandomToken emits (32 random bytes; + -> -, / -> _, = stripped).
#
# ── CADDY'S ENVIRONMENT IS THE ONLY SOURCE. THERE IS NO .env FALLBACK. ───────
# The Caddyfile consumes {$GAME_WS_SECRET} from CADDY'S OWN ENVIRONMENT, which
# is fixed at container CREATE time. That value — not the file — is what the
# public path will actually be. docker-compose.yml puts GAME_WS_SECRET in
# caddy's `environment:` block, and that line is annotated over there as this
# check's oracle precisely so it does not get removed by accident.
#
# THE FALLBACK THIS DELETES WAS A REAL HOLE (codex gpt-5.6-sol, round 6). The
# previous version read caddy only when `.State.Running` was true and otherwise
# fell back to .env. So: caddy STOPPED, holding a stale or malformed secret in
# its environment, while .env carries a corrected one. The precheck waited 120s,
# fell back to the file, validated the GOOD value, and started the relay. A
# later `docker start caddy` — a reboot, an operator, a compose up — then
# published the relay at the MALFORMED path, and every log line in this unit had
# said success. Two sources for one truth, disagreeing, with the wrong one
# winning: the same defect class as the four firstboot rounds, in a new place.
#
# So: wait for a caddy container to EXIST (any state — created, exited, running;
# `.Config.Env` is readable in all of them), read the secret from it, and
# validate THAT. No caddy container at all is a hard failure, not a fallback:
# caddy is the only route in, so without it there is no door to validate and
# starting the relay would be starting a service nothing can reach anyway.
#
# THE BOUNDED WAIT IS STILL NEEDED, for a different reason than before. On a
# normal reboot After=docker.service is satisfied when dockerd is up, NOT when
# dockerd has finished restoring the containers it owns, so this unit can
# legitimately arrive before caddy has been re-created. On first boot the unit
# is also After=homehub-firstboot.service, and firstboot starts it only after
# the bulk `docker compose up -d` — so by then caddy exists by construction.
#
# A BAD OR MISSING SECRET FAILS THIS UNIT AND NOTHING ELSE. That is the entire
# blast-radius change: the code this replaces could take household DNS down over
# a typo in a game token.
cmd_precheck() {
    local secret="" state="" i=0

    # EXISTENCE, NOT RUNNING. `docker inspect` on an absent container exits
    # non-zero; on a stopped one it succeeds and `.Config.Env` is fully
    # populated, because that is create-time state.
    while [ "$i" -lt "$CADDY_WAIT_SECS" ]; do
        "$DOCKER" inspect caddy >/dev/null 2>&1 && break
        i=$((i + 2)); sleep 2
    done
    "$DOCKER" inspect caddy >/dev/null 2>&1 \
        || die "no 'caddy' container exists after ${CADDY_WAIT_SECS}s. Caddy is the only route in; without it the relay has no door to validate, and nothing could reach the relay even if it started. Run 'docker compose up -d' in $STACK_DIR first, then: systemctl start homehub-gunmaster3-relay"

    state="$("$DOCKER" inspect -f '{{.State.Status}}' caddy 2>/dev/null || echo unknown)"
    secret="$("$DOCKER" inspect -f '{{range .Config.Env}}{{println .}}{{end}}' caddy 2>/dev/null \
              | sed -n 's/^GAME_WS_SECRET=//p' | head -n1)"

    printf '%s' "$secret" | grep -qE '^[A-Za-z0-9_-]{43}$' \
        || die "GAME_WS_SECRET is missing or malformed in the caddy container's environment (caddy is '$state'). That environment is what the Caddyfile actually matches on, so this is the real public path, whatever $ENV_FILE says. Expect 43 base64url characters. The relay does not start. Fix: correct GAME_WS_SECRET in $ENV_FILE, then RECREATE caddy - 'docker compose up -d --force-recreate caddy' - because a container's environment is fixed at create time and editing the file alone changes nothing."

    log "GAME_WS_SECRET validated (43 base64url chars) from the caddy container's environment (caddy is '$state') - this is the value that gates the public path"
}

# ── start ────────────────────────────────────────────────────────────────────
# ExecStart=. --profile IS PASSED HERE AND NOWHERE ELSE, and that is what keeps
# the relay out of firstboot's bulk `docker compose up -d`. Putting `gunmaster3`
# into COMPOSE_PROFILES would undo it: the bulk up would start the relay outside
# this unit, with no fence ordering and no secret validation. That is a DEFECT,
# and verify-hub.sh (TC-H-T07), ensure-local-images.sh and export-images.sh all
# refuse it.
cmd_start() {
    local rp
    cd "$STACK_DIR" || die "cannot cd to $STACK_DIR — the stack payload is not where this unit expects it"

    # ── DISARM A LEGACY CONTAINER BEFORE TOUCHING IT ────────────────────────
    # BELT TO firstboot's BRACES (round 6, codex gpt-5.6-sol). A box that ran
    # the OLD shape has a `gunmaster3-relay` container carrying
    # `restart: unless-stopped`, and the bulk `docker compose up -d` does not go
    # near it — the profile is inactive, so compose neither recreates nor
    # removes it. If this unit then cleanly SKIPS (knob off) or fails its
    # precheck, that legacy container is still armed, and DOCKER restarts it at
    # the next boot: a public, unauthenticated relay, outside this unit, and
    # therefore outside the Requires= that guarantees the fence and outside the
    # postcheck that verifies it.
    #
    # firstboot's step 4-pre does this migration for every box it runs on. This
    # is the second place, because this unit is also started by hand, on boxes
    # updated by copying stack/ without a reimage, where firstboot may not have
    # run since the payload changed.
    #
    # COMPOSE WOULD RECREATE THE CONTAINER ANYWAY — the declared restart policy
    # changed from `unless-stopped` to `no`, which is part of the container
    # spec, so `up -d` sees a config drift and replaces it. That is exactly why
    # this is a belt and not the mechanism: it makes the disarm true BEFORE the
    # recreate, so a failure of the recreate cannot leave an armed container
    # behind.
    rp="$("$DOCKER" inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER" 2>/dev/null || true)"
    if [ -n "$rp" ] && [ "$rp" != no ]; then
        log "found a legacy $CONTAINER with restart='$rp' - disarming it before compose recreates it"
        "$DOCKER" update --restart=no "$CONTAINER" >/dev/null 2>&1 \
            || log "WARN: 'docker update --restart=no $CONTAINER' failed"
        "$DOCKER" stop "$CONTAINER" >/dev/null 2>&1 \
            || log "WARN: 'docker stop $CONTAINER' failed"
        rp="$("$DOCKER" inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER" 2>/dev/null || true)"
        [ "$rp" = no ] || log "WARN: $CONTAINER still has restart='$rp' - if the compose recreate below does not replace it, docker will start it at the next boot outside this unit. Check: docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' $CONTAINER"
    fi

    "$DOCKER" compose --profile "$PROFILE" up -d "$SERVICE" \
        || die "'docker compose --profile $PROFILE up -d $SERVICE' failed in $STACK_DIR. Check: 'docker compose --profile $PROFILE config', and that the gunmaster3-relay image exists locally — nothing publishes it, see scripts/ensure-local-images.sh."
    log "compose reports $SERVICE started"
}

# ── postcheck ────────────────────────────────────────────────────────────────
# ExecStartPost=. VERIFY THE ARTIFACT, NOT THE EXIT CODE (house rule).
# `compose up -d` returns 0 once a container is STARTED, which is not the same
# as running; and `Requires=homehub-game-isolation.service` proves that unit
# reached `active`, which is not the same as a REJECT sitting in INPUT.
#
# BELT AND BRACES ON THE FENCE, deliberately. systemd's Requires= is the real
# guarantee and this is the second look, because the failure it catches — a
# public unauthenticated service with a reachable host behind it — is the one
# failure on this box worth checking twice. The pattern is THE SAME ONE
# verify-hub.sh uses for TC-H-T06, character for character, so the unit and the
# verifier cannot disagree about what "fenced" means.
cmd_postcheck() {
    local st sub first
    st="$("$DOCKER" inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo missing)"
    [ "$st" = running ] \
        || die "$CONTAINER is '$st' after a successful 'compose up'. Check: docker logs $CONTAINER --tail 50"

    if ! command -v iptables >/dev/null 2>&1; then
        stop_and_note
        die "iptables is not callable, so the host fence CANNOT be verified. $STOP_NOTE This is the one service here that answers the public internet with no identity check, and 'internal: true' does NOT stop it reaching this host (measured 2026-08-29: SSH, Technitium's DNS AND its admin console on 5380, cockpit)."
    fi

    sub="$(fence_subnet)"
    # THE FIRST RULE, NOT ANY RULE, AND SOURCE-ONLY. iptables stops at the first
    # match, so a REJECT sitting below an ACCEPT for the same source is inert;
    # and a loose `-s <subnet>.*-j REJECT` would also match a rule rejecting ONE
    # PORT, which would pass while 53, 5380 and 9090 stayed reachable. `-S`
    # prints in evaluation order, so the first -A line is rule 1.
    first="$(iptables -S INPUT 2>/dev/null | grep '^-A' | head -1)"
    if printf '%s' "$first" | grep -qE -- "^-A INPUT -s ${sub}( -m comment --comment \"?${FENCE_COMMENT}\"?)? -j REJECT"; then
        log "verified: the relay subnet $sub is REJECTed from this host, and the rule is FIRST in INPUT"
    else
        stop_and_note
        die "the relay started but the host fence is NOT the first INPUT rule (first is: ${first:-none}). $STOP_NOTE Check: 'systemctl status homehub-game-isolation', then 'iptables -S INPUT | head -3'. Anything ahead of the REJECT can match the relay's traffic first, which makes the fence decorative."
    fi
}

# ── stop ─────────────────────────────────────────────────────────────────────
# ExecStop=. --profile is needed here too: without it compose does not consider
# the service part of the project at all and the stop is a silent no-op.
#
# AND IT VERIFIES THE ARTIFACT, because the callers make a SAFETY CLAIM on its
# behalf. The postcheck below tells an operator "the relay has been STOPPED"
# when the fence is missing, and the previous version of this function could not
# support that sentence: a failed `cd` returned 1 having stopped nothing, and a
# non-zero `compose stop` only logged a warning — in both cases the die message
# still said STOPPED. Claiming a public, unauthenticated service has been shut
# down when it has not is worse than any of the failures this file reports.
#
# So: ask compose, then ask DOCKER whether it is still running, then escalate to
# `docker kill`, and only return 0 when an inspect confirms it is down.
stop_relay() {
    local i=0 running
    if cd "$STACK_DIR" 2>/dev/null; then
        "$DOCKER" compose --profile "$PROFILE" stop "$SERVICE" >/dev/null 2>&1 || true
    else
        log "WARN: cannot cd to $STACK_DIR — going straight to the container, not through compose"
    fi
    # `compose stop` blocks until the container is down, so this normally exits
    # on the first pass; the loop is for the case where it did not, or was never
    # reached. An absent container inspects as an error, which reads as "not
    # running" here and is the correct answer.
    while [ "$i" -lt 10 ]; do
        running="$("$DOCKER" inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)"
        if [ "$running" != true ]; then
            log "$SERVICE is stopped (confirmed by docker inspect)"
            return 0
        fi
        i=$((i + 1)); sleep 1
    done
    log "WARN: $CONTAINER is STILL RUNNING after 'compose stop' — killing it"
    "$DOCKER" kill "$CONTAINER" >/dev/null 2>&1 || true
    running="$("$DOCKER" inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)"
    if [ "$running" = true ]; then
        log "COULD NOT STOP $CONTAINER — it is still running and still reachable from the internet."
        log "  Stop it now:  docker kill $CONTAINER"
        return 1
    fi
    log "$SERVICE stopped by 'docker kill' after compose stop did not take"
    return 0
}

# stop_and_note — stop the relay and leave the honest half-sentence in
# $STOP_NOTE for the die messages above to paste in. It says what ACTUALLY
# happened rather than what was attempted.
#
# A GLOBAL RATHER THAN A `$(…)` SUBSTITUTION, on purpose: command substitution
# captures stdout, and stop_relay's own log lines — including the loud "COULD
# NOT STOP … docker kill" ones — would be swallowed into the middle of the
# FATAL string instead of appearing in the journal in order.
STOP_NOTE=""
stop_and_note() {
    if stop_relay; then
        STOP_NOTE="The relay has been STOPPED (confirmed by docker inspect)."
    else
        STOP_NOTE="The relay COULD NOT BE STOPPED and is STILL RUNNING - stop it now: docker kill $CONTAINER"
    fi
}

# ExecStop= propagates the failure deliberately: a relay that is still answering
# the internet after `systemctl stop` asked it not to is a real fault, and a
# `systemctl stop` that reports success over it would hide the one thing an
# operator needs to know.
cmd_stop() { stop_relay; }

case "${1:-}" in
    condition) cmd_condition ;;
    precheck)  cmd_precheck ;;
    start)     cmd_start ;;
    postcheck) cmd_postcheck ;;
    stop)      cmd_stop ;;
    *) die "usage: $0 {condition|precheck|start|postcheck|stop} — this script is driven by homehub-gunmaster3-relay.service" ;;
esac
