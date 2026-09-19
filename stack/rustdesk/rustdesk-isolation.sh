#!/usr/bin/env bash
# rustdesk-isolation.sh — restrict the remote-desktop listeners to the LAN and
# the mesh tunnel, in both address families, durably.
#
# Implements: SR-045, LLR-972, LLR-976
#
# WHY INPUT AND NOT DOCKER-USER — and note this is the SAME conclusion
# game-isolation.sh reaches next door, for the same reason, while the fail2ban
# jail correctly reaches the opposite one. DOCKER-USER is consulted for
# FORWARDED traffic, which is what a packet destined for a PUBLISHED CONTAINER
# PORT is. These services run with host networking: their sockets are HOST
# sockets, so traffic to them is delivered locally through INPUT and never
# enters DOCKER-USER at all. A DOCKER-USER rule here would be present, would
# read correctly to a reviewer, and would filter nothing.
#
# An earlier draft of this repo's own requirement rows specified DOCKER-USER
# here. It was caught in cross-review before anything shipped. The comment is
# long because the mistake is easy, silent, and looks like a working control.
#
# BOTH FAMILIES, AND THAT IS NOT OPTIONAL. A v4-only ruleset leaves the same
# listener reachable over IPv6 on any box that acquires a routable v6 address.
# The mesh tunnel makes that MORE likely rather than less, since the tunnel
# hands out v6 addressing of its own.
#
# IDEMPOTENT BY MARKER. Every rule this script installs carries a comment
# marker; a run removes its own previous rules by that marker before installing
# the current set. So a replay after a reboot, a firewall reload or a flush
# converges instead of accumulating duplicates, and a changed CIDR does not
# leave the old, wider rule sitting behind the new one.
#
# FAILS CLOSED. If either family cannot be programmed this script exits
# non-zero, and because homehub-rustdesk.service `Requires=` the unit that runs
# it, the listeners do not start. A relay with no fence must not exist; the
# ordering alone is not enough, which is the lesson the game-isolation unit
# records after its own first version got exactly that wrong.
#
# Exit 0 = both families programmed. 1 = they are not, loudly.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${RUSTDESK_ENV_FILE:-$HERE/../.env}"
COMMENT="homehub-rustdesk-isolation"

# Ports are the upstream-documented set. hbbs: ID/rendezvous. hbbr: relay.
TCP_PORTS="${RUSTDESK_TCP_PORTS:-21115,21116,21117,21118,21119}"
UDP_PORTS="${RUSTDESK_UDP_PORTS:-21116}"

LAN_CIDR_V4=""
TUNNEL_CIDR_V4=""
TUNNEL_CIDR_V6=""

if [ -f "$ENV_FILE" ]; then
    for key in RUSTDESK_LAN_CIDR RUSTDESK_TUNNEL_CIDR RUSTDESK_TUNNEL_CIDR6 \
               RUSTDESK_TCP_PORTS RUSTDESK_UDP_PORTS; do
        value="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
        [ -n "$value" ] && printf -v "$key" '%s' "$value"
    done
fi

LAN_CIDR_V4="${RUSTDESK_LAN_CIDR:-}"
TUNNEL_CIDR_V4="${RUSTDESK_TUNNEL_CIDR:-}"
TUNNEL_CIDR_V6="${RUSTDESK_TUNNEL_CIDR6:-}"

log() { printf '%s [rustdesk-isolation] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "FATAL: $*"; exit 1; }

command -v iptables  >/dev/null 2>&1 || die "iptables not found"
command -v ip6tables >/dev/null 2>&1 || die "ip6tables not found - the v6 half of this fence cannot be programmed, and a v4-only fence is not a fence"

[ -n "$LAN_CIDR_V4" ] || die "RUSTDESK_LAN_CIDR is empty - refusing to install a fence with no admitted source, which would either lock out every client or, if defaulted, admit one nobody chose"

# ---------------------------------------------------------------------------
# Remove our own prior rules, by marker, in both families.
# ---------------------------------------------------------------------------
# Loop rather than delete-once: a previous run, a partial run, or an operator's
# manual retry can leave several. Deleting until none match is what makes a
# replay converge.
purge() {
    local bin="$1" removed=0
    while $bin -S INPUT 2>/dev/null | grep -q -- "--comment $COMMENT"; do
        local spec
        spec="$($bin -S INPUT | grep -m1 -- "--comment $COMMENT" | sed 's/^-A //')"
        # shellcheck disable=SC2086
        $bin -D INPUT $spec 2>/dev/null || break
        removed=$((removed + 1))
    done
    [ "$removed" -gt 0 ] && log "$bin: removed $removed stale rule(s)"
    return 0
}

purge iptables
purge ip6tables

# ---------------------------------------------------------------------------
# Install the current set.
# ---------------------------------------------------------------------------
# Order matters: ACCEPT the admitted sources first, then REJECT everything else
# to these ports. Appending the REJECT last means a packet from an admitted
# source has already matched and left the chain.
install_family() {
    local bin="$1"; shift
    local admitted="$*"
    local proto ports

    for proto in tcp udp; do
        [ "$proto" = tcp ] && ports="$TCP_PORTS" || ports="$UDP_PORTS"
        [ -n "$ports" ] || continue

        local cidr
        for cidr in $admitted; do
            [ -n "$cidr" ] || continue
            $bin -A INPUT -p "$proto" -m multiport --dports "$ports" \
                -s "$cidr" -m comment --comment "$COMMENT" -j ACCEPT \
                || die "$bin: could not admit $cidr for $proto/$ports"
        done

        # REJECT rather than DROP: a refused connection tells an operator on the
        # LAN immediately that the fence is up, where a silent drop looks like a
        # dead service and costs an hour. Nothing outside can see either.
        $bin -A INPUT -p "$proto" -m multiport --dports "$ports" \
            -m comment --comment "$COMMENT" -j REJECT \
            || die "$bin: could not install the $proto reject"
    done
}

install_family iptables  "$LAN_CIDR_V4" "$TUNNEL_CIDR_V4"

# The v6 half runs even when no v6 tunnel CIDR is configured, and that is the
# point: with no admitted v6 source it installs the REJECT alone, so the
# listeners are closed over v6 rather than open by omission.
install_family ip6tables "$TUNNEL_CIDR_V6"

log "fence up: tcp=$TCP_PORTS udp=$UDP_PORTS admitted_v4='$LAN_CIDR_V4 $TUNNEL_CIDR_V4' admitted_v6='$TUNNEL_CIDR_V6'"
log "NOTE: these ports are never forwarded at the router. The remote path is the tunnel."
exit 0
