#!/usr/bin/env bash
# tailnet-keep-source.sh - a tailnet client reaches the stack's published ports
# under its OWN address, not the Docker bridge's.
#
# Implements: SR-175, LLR-1600, LLR-1601, LLR-1602, LLR-1603
#
# THE FAULT (found live 2026-11-12, a phone on mobile data over the tailnet):
# photos.<domain> resolved to the hub's LAN address, the request reached Caddy,
# and Caddy answered "LAN only" although LAN_CIDR carries the tailnet range.
# Two pieces, each correct alone:
#   * tailscaled (NoSNAT false, iptables mode) marks EVERY packet forwarded from
#     tailscale0 with 0x40000/0xff0000 (filter ts-forward), and its nat chain
#     ts-postrouting, jumped to first in POSTROUTING, MASQUERADEs every marked
#     packet. That is what lets a LAN host with no route to the tailnet answer.
#   * Docker DNATs a published port into the container, so a request to the
#     hub's address from the tailnet is FORWARDED out of a docker bridge - and
#     masqueraded to that bridge's gateway. Caddy's `@lan remote_ip {$LAN_CIDR}`
#     then sees a Docker address and refuses.
#
# THE FIX IS ONE RULE in mangle POSTROUTING, which the kernel runs before nat
# POSTROUTING (hook priority -150 against 100):
#     -s 100.64.0.0/10 -o br-+ -j MARK --set-xmark 0x0/0x40000
# It clears tailscaled's bit, and only that bit, on packets from the tailnet
# range that leave by a docker bridge, so ts-postrouting no longer matches them
# and the container sees the client's own address. The reply needs no
# masquerade: the container answers its bridge gateway, which is this host,
# and this host routes the tailnet (tailscaled's table 52).
#
# WHAT IT LEAVES ALONE, by construction - the only output match is br-+:
#   * exit-node traffic (client -> internet) and subnet-route traffic to other
#     LAN devices leave by the LAN interface, keep the mark, and are masqueraded
#     exactly as before. A LAN device answers its default gateway, the router,
#     so it NEEDS that masquerade; the netns test proves both paths.
#   * the filter table. Docker's and tailscaled's accept/drop decisions see the
#     original source either way; only what the container is told changes.
#
# WHICH BRIDGES. `br-+` is every network compose creates (br-<network id>), and
# the wildcard is matched per packet, so a network compose re-creates tomorrow
# is covered with no re-run. docker0, Docker's default bridge, is NOT covered:
# no stack service uses it (no `network_mode: bridge`, no custom bridge name;
# a test asserts both), so only an ad-hoc `docker run` lands there, and a
# tailnet client reaching such a container still shows as docker0's gateway.
#
# IPv6 IS NOT COVERED, AND A MARK CANNOT COVER IT. No network on the hub has
# IPv6 enabled, so a v6 connection to a published port ([::]:443) is accepted
# by docker-proxy on the host and re-opened from the bridge gateway: nothing is
# forwarded, so there is no packet for this rule to touch. Tonight Technitium
# answered only an A record for these names, and the advertised subnet route is
# v4 only, so a tailnet client uses v4. A client that reaches Caddy over v6 (the
# hub's own fd7a:115c:a1e0::/48 tailnet address) is still seen as a Docker
# address and refused by the LAN-only sites.
#
# WHY ONE BOOT UNIT AND NO HOOK ON tailscaled OR docker - read from the source
# of the versions the hub runs (2026-11-12), not measured:
#   * tailscaled v1.104.5 deletes its jumps (filter INPUT/FORWARD, nat
#     POSTROUTING) and flushes its own chains at every start and in
#     ExecStopPost (clearRules,
#     https://github.com/tailscale/tailscale/blob/v1.104.5/util/linuxfw/iptables.go#L203-L232).
#     In mangle it inserts and deletes exactly two CONNMARK rules, in PREROUTING
#     and OUTPUT, by their full spec (AddConnmarkSaveRule/DelConnmarkSaveRule,
#     util/linuxfw/iptables_runner.go#L540-L640). It never lists, flushes or
#     deletes in mangle POSTROUTING. Unlike the DNS-console fence, whose
#     position against ts-input moves on every restart, this rule has no
#     position to lose: it is in another table, and MARK does not terminate.
#   * Docker 29.8.0 never uses the mangle table (iptables.Mangle is declared in
#     https://github.com/moby/moby/blob/docker-v29.8.0/daemon/libnetwork/iptables/iptables.go#L53
#     and used nowhere outside tests); at start it removes and re-creates only
#     its own chains (DOCKER, DOCKER-FORWARD, DOCKER-BRIDGE, DOCKER-CT,
#     DOCKER-INTERNAL: drivers/bridge/internal/iptabler/iptabler.go#L128-L188).
# What WOULD remove it: `iptables -t mangle -F`, a full iptables-restore, or a
# boot with the unit disabled. `status` sees all three, and
# setup-tailscale.sh --check-only folds it in.
#
# THE KNOB: TAILSCALE_KEEP_SOURCE_TO_DOCKER=true|false, default true, because
# SR-042 already requires LAN_CIDR to admit the tailnet range; false withdraws
# the rule (the unit keeps it withdrawn across reboots). `status` calls it
# drift when the rule is on and LAN_CIDR does NOT admit the range (the
# template's `private_ranges` does not): such a box let tailnet clients in only
# by accident, masqueraded to a 172.x bridge address, and refuses them now. With TAILSCALE_ENABLED not
# true there is no tailnet and the rule is withdrawn too. Anything else than
# exactly true or false is refused and nothing changes.
#
# THE COORDINATOR'S HAND-MADE RULE of 2026-11-12 carries the comment
# tailnet-to-docker-keep-source-TEST-20261112. Every apply and every remove
# deletes any rule whose comment starts with tailnet-to-docker-keep-source-TEST,
# so the box never carries two; `status` calls one left behind drift.
#
# Usage: tailnet-keep-source.sh apply|status|remove|install
#   apply    converge to the knob (the unit's ExecStart); edits only what is out
#            of place, so a second run changes nothing.
#   status   read-only: the rules and the unit as they are, against the knob.
#   remove   rollback: delete every rule this script or the TEST carried. The
#            unit stays enabled; set the knob false to keep it off after a boot.
#   install  install and enable the unit, run it, then report `status`.
# Exit: 0 in place (or correctly absent). 1 failed / cannot read. 2 refused
#       (usage, or a knob that is not exactly true or false). 3 (status only)
#       drift found, nothing changed.
set -uo pipefail

MARKER="homehub-tailnet-keep-source"
TEST_MARKER_PREFIX="tailnet-to-docker-keep-source-TEST"
# The tailnet's own range is vendor-fixed (tsaddr.CGNATRange), so it is a
# constant here and not a knob; a narrower value would silently miss clients.
TAILNET_V4="100.64.0.0/10"
BRIDGES="br-+"
# tailscaled's subnetRouteMark. Only this bit is cleared; the rest of its mask
# (0xff0000) and every other mark bit pass through untouched.
TS_SUBNET_ROUTE_BIT="0x40000"
UNIT="homehub-tailnet-keep-source.service"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TAILNET_KEEP_SOURCE_ENV_FILE:-$HERE/../.env}"
UNIT_DIR="${TAILNET_KEEP_SOURCE_UNIT_DIR:-/etc/systemd/system}"
LOCK_FILE="${TAILNET_KEEP_SOURCE_LOCK:-/run/homehub-tailnet-keep-source.lock}"

# Written in the order `iptables -S` prints it (iptables 1.8.10, nf_tables).
RULE=(-s "$TAILNET_V4" -o "$BRIDGES" -m comment --comment "$MARKER"
      -j MARK --set-xmark "0x0/$TS_SUBNET_ROUTE_BIT")

log() { printf 'tailnet-keep-source: %s\n' "$*"; }

# One key from the stack .env, read as text and never sourced. Last assignment
# wins, as in compose; strips a CR, an unquoted ` # comment` and quotes. The
# same reader as dns-console-fence.sh.
_env() {
    [ -r "$ENV_FILE" ] || return 0
    awk -v k="$1=" '{ sub(/\r$/, "") } index($0, k) == 1 { v = substr($0, length(k) + 1); f = 1 }
                    END { if (f) print v }' "$ENV_FILE" 2>/dev/null \
        | sed -e 's/[[:space:]]#.*$//' -e 's/[[:space:]]*$//' \
              -e 's/^"\(.*\)"$/\1/' -e "s/^'\\(.*\\)'\$/\\1/"
}

# `-w`: wait for the xtables lock rather than fail while tailscaled or docker
# is mid-edit.
ipt() { iptables -w 5 -t mangle "$@"; }

# desired -> sets WANT (on|off) and WHY; returns 2 on a knob it will not guess.
# The environment supplies defaults and the .env wins, as in setup-tailscale.sh.
desired() {
    local enabled keep v
    enabled="${TAILSCALE_ENABLED:-false}"
    keep="${TAILSCALE_KEEP_SOURCE_TO_DOCKER:-true}"
    v="$(_env TAILSCALE_ENABLED)"; [ -z "$v" ] || enabled="$v"
    v="$(_env TAILSCALE_KEEP_SOURCE_TO_DOCKER)"; [ -z "$v" ] || keep="$v"
    case "$keep" in
        true|false) ;;
        *) log "REFUSED: TAILSCALE_KEEP_SOURCE_TO_DOCKER is '$keep'; it must be exactly true or false. Nothing changed."
           return 2 ;;
    esac
    if [ "$enabled" != true ]; then WANT=off; WHY="TAILSCALE_ENABLED is not true"
    elif [ "$keep" = true ]; then WANT=on; WHY="TAILSCALE_KEEP_SOURCE_TO_DOCKER=true"
    else WANT=off; WHY="TAILSCALE_KEEP_SOURCE_TO_DOCKER=false"; fi
    return 0
}

# Read mangle POSTROUTING whole into LISTING (never piped into an early-exiting
# reader: under pipefail the producer's SIGPIPE becomes the status).
read_chain() { LISTING="$(ipt -S POSTROUTING 2>&1)"; }

# marked PREFIX WHOLE -> the `-A POSTROUTING` lines whose comment is PREFIX
# (WHOLE=1) or starts with it (WHOLE=0). Matched on the raw line: iptables prints
# a comment holding a space in double quotes, so such a rule never matches and
# is never deleted (both markers are single words). awk reads its whole input,
# so nothing upstream can take SIGPIPE under pipefail.
marked() {
    printf '%s\n' "$LISTING" | awk -v p="$1" -v whole="$2" '
        /^-A POSTROUTING / { l = $0 " "
            if (whole == "1" ? index(l, " --comment " p " ") : index(l, " --comment " p)) print }'
}
count_marked() { marked "$1" "$2" | awk 'END { print NR }'; }

# inspect -> sets OURS (rules carrying MARKER), TESTS (TEST rules) and EXACT
# (1 when exactly one rule carries MARKER and `-C` confirms it is this RULE).
# Returns 1 when the chain cannot be read.
inspect() {
    read_chain || { log "cannot list mangle POSTROUTING: ${LISTING##*$'\n'}"; return 1; }
    OURS="$(count_marked "$MARKER" 1)"
    TESTS="$(count_marked "$TEST_MARKER_PREFIX" 0)"
    EXACT=0
    if [ "$OURS" -eq 1 ] && ipt -C POSTROUTING "${RULE[@]}" 2>/dev/null; then EXACT=1; fi
    return 0
}

# purge COMMENT_PREFIX WHOLE -> delete every matching rule BY SPEC, re-reading
# after each delete, so nothing anyone else inserts meanwhile is removed by a
# stale rule number. Bounded so a delete that "succeeds" without effect cannot
# spin. The line splits on spaces safely because `marked` never returns a rule
# with a quoted (space-holding) comment.
purge() {
    local prefix="$1" whole="$2" line n=0
    local -a argv
    while :; do
        read_chain || { log "cannot list mangle POSTROUTING"; return 1; }
        line="$(marked "$prefix" "$whole" | awk 'NR == 1')"
        [ -n "$line" ] || break
        if [ "$n" -ge 20 ]; then log "still finding '$prefix' rules after 20 deletes - stopping"; return 1; fi
        read -r -a argv <<<"-D ${line#-A }"
        ipt "${argv[@]}" || { log "could not delete [$line]"; return 1; }
        n=$((n + 1))
    done
    [ "$n" -eq 0 ] || log "removed $n rule(s) carrying '$prefix'"
    return 0
}

# Serialise the boot unit against a hand run or setup-tailscale.sh: two
# interleaved purge/append runs are how duplicates are made.
take_lock() {
    command -v flock >/dev/null 2>&1 || { log "WARN: flock not found - running unlocked"; return 0; }
    # The braces scope the stderr redirect; a bare `exec 9>>f 2>/dev/null`
    # would silence this script's stderr for the rest of the run.
    if ! { exec 9>>"$LOCK_FILE"; } 2>/dev/null; then log "WARN: cannot open $LOCK_FILE - running unlocked"; return 0; fi
    flock -w 30 9 || { log "could not take $LOCK_FILE within 30 s"; return 1; }
}

unit_enabled() { systemctl is-enabled --quiet "$UNIT" 2>/dev/null; }

# Does LAN_CIDR, Caddy's `@lan` allow-list (space-separated), hold an IPv4
# network that contains the whole tailnet range? With the rule on, Caddy sees
# tailnet clients as 100.x, so without that a tailnet client is refused. The
# template's `private_ranges` does not (RFC1918 and loopback only): on such a
# box tailnet clients passed only by accident, masqueraded to a 172.x bridge
# address. Not a reason to leave the rule off; a reason to say so loudly.
lan_cidr_admits_tailnet() {
    local -a toks
    local tok len num mask want=$(( (100 << 24) | (64 << 16) ))   # TAILNET_V4's network
    read -r -a toks <<<"$(_env LAN_CIDR)"
    for tok in "${toks[@]}"; do
        [[ "$tok" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})/([0-9]{1,2})$ ]] || continue
        len=$((10#${BASH_REMATCH[5]}))
        [ "$len" -le 10 ] || continue
        num=$(( (10#${BASH_REMATCH[1]} << 24) | (10#${BASH_REMATCH[2]} << 16) | (10#${BASH_REMATCH[3]} << 8) | 10#${BASH_REMATCH[4]} ))
        mask=$(( len == 0 ? 0 : (0xFFFFFFFF << (32 - len)) & 0xFFFFFFFF ))
        [ $(( num & mask )) -eq $(( want & mask )) ] && return 0
    done
    return 1
}

# report -> prints one verdict line; returns 0 in place, 3 drift, 1 unreadable.
# Requires desired() to have run.
report() {
    inspect || return 1
    local drift=""
    if [ "$WANT" = on ]; then
        [ "$EXACT" -eq 1 ] || drift="want the rule ON ($WHY), found $OURS rule(s) carrying $MARKER$([ "$OURS" -eq 1 ] && printf ' that fail -C')"
        if [ -z "$drift" ] && ! unit_enabled; then
            drift="the rule is in place but $UNIT is not enabled, so it will not come back after a reboot"
        fi
    elif [ "$OURS" -ne 0 ]; then
        drift="want the rule OFF ($WHY), found $OURS rule(s) carrying $MARKER"
    fi
    if [ "$TESTS" -ne 0 ]; then
        drift="${drift}${drift:+; }$TESTS hand-made $TEST_MARKER_PREFIX* rule(s) still present"
    fi
    if [ "$WANT" = on ] && ! lan_cidr_admits_tailnet; then
        drift="${drift}${drift:+; }LAN_CIDR does not admit the tailnet range $TAILNET_V4 (SR-042), so with the rule on a tailnet client gets 403 at every LAN-only site: add the range to LAN_CIDR, or set TAILSCALE_KEEP_SOURCE_TO_DOCKER=false"
    fi
    if [ -n "$drift" ]; then log "DRIFT - $drift"; return 3; fi
    if [ "$WANT" = on ]; then
        log "ON as configured - tailnet clients reach the stack's published ports with their own address (mangle POSTROUTING: ${RULE[*]})"
    else
        log "OFF as configured ($WHY) - a tailnet client reaches the stack as a Docker bridge address, and the LAN-only sites refuse it"
    fi
    return 0
}

apply() {
    take_lock || return 1
    purge "$TEST_MARKER_PREFIX" 0 || return 1
    inspect || return 1
    if [ "$WANT" = on ]; then
        if [ "$EXACT" -ne 1 ]; then
            purge "$MARKER" 1 || return 1
            ipt -A POSTROUTING "${RULE[@]}" || { log "could not append the rule"; return 1; }
            log "rule added"
        fi
    else
        purge "$MARKER" 1 || return 1
    fi
    # Read back; verify the artifact, not the exit codes above. The unit is
    # running this, so "not enabled" is not judged here - status judges it.
    inspect || return 1
    if [ "$WANT" = on ] && [ "$EXACT" -ne 1 ]; then log "FAILED - after apply: $OURS rule(s) carrying $MARKER, -C did not confirm"; return 1; fi
    if [ "$WANT" = off ] && [ "$OURS" -ne 0 ]; then log "FAILED - after apply: $OURS rule(s) carrying $MARKER remain"; return 1; fi
    if [ "$TESTS" -ne 0 ]; then log "FAILED - $TESTS TEST rule(s) remain"; return 1; fi
    log "applied: rule $WANT ($WHY)"
    return 0
}

remove_all() {
    take_lock || return 1
    purge "$TEST_MARKER_PREFIX" 0 || return 1
    purge "$MARKER" 1 || return 1
    inspect || return 1
    if [ "$OURS" -ne 0 ] || [ "$TESTS" -ne 0 ]; then log "FAILED to remove every rule"; return 1; fi
    log "removed: a tailnet client reaches the stack as a Docker bridge address again"
    return 0
}

install_unit() {
    install -m 0644 "$HERE/$UNIT" "$UNIT_DIR/$UNIT" || { log "could not install $UNIT into $UNIT_DIR"; return 1; }
    systemctl daemon-reload || { log "systemctl daemon-reload failed"; return 1; }
    systemctl enable "$UNIT" >/dev/null 2>&1 || { log "could not enable $UNIT"; return 1; }
    systemctl restart "$UNIT" || { log "$UNIT failed: journalctl -u $UNIT"; return 1; }
    report
}

MODE="${1:-}"
case "$MODE" in
    apply|status|remove|install) ;;
    *) echo "usage: $0 apply|status|remove|install" >&2; exit 2 ;;
esac

if [ "$MODE" = remove ]; then
    remove_all; exit $?
fi
desired || exit 2
case "$MODE" in
    apply)   apply; exit $? ;;
    status)  report; exit $? ;;
    install) install_unit; exit $? ;;
esac
