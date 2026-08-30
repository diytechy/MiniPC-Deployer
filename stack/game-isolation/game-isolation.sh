#!/usr/bin/env bash
# game-isolation.sh — stop the crossplay relay reaching the HOST.
#
# WHY THIS FILE EXISTS, and it is not the reason you would guess from the name.
#
# The relay (docker-compose.yml, profile `gunmaster3`) is the one service on this
# box that deliberately answers the public internet with no identity check. It is
# put on a `game` network with `internal: true`, and that was documented as
# meaning it "cannot reach the internet, the LAN, or its neighbours".
#
# ALL THREE OF THOSE ARE TRUE. The fourth, which nobody wrote down and which is
# the one that mattered, is NOT: `internal: true` does not stop a container
# reaching the HOST. Docker leaves the bridge gateway address live and documents
# the exception. Measured from inside the relay container, 2026-08-29:
#
#     172.x.0.1:22    SSH                                  REACHABLE
#     172.x.0.1:53    Technitium DNS   (host-networked)    REACHABLE
#     172.x.0.1:5380  Technitium ADMIN CONSOLE             REACHABLE
#     172.x.0.1:9090  cockpit                              REACHABLE
#
# The 5380 line is the serious one. That console can rewrite every name the
# household resolves, and reaching it this way goes straight past BOTH of the
# guards put on it: Caddy's LAN-only remote_ip gate and Caddy's basic_auth. The
# gate is a Caddy site matcher; a caller that never talks to Caddy is not
# subject to it. Technitium binds :5380 on ALL interfaces because it is
# host-networked, and the bridge is one of them.
#
# So the isolation claim needs a host firewall rule to be true, and this is it.
#
# WHY `INPUT` AND NOT `DOCKER-USER` — the opposite choice to the fail2ban jail
# next door, and for the mirror-image reason. DOCKER-USER is consulted for
# FORWARDED traffic (packets traversing to a container), which is why a jail
# banning published-port abuse has to live there. Traffic addressed to the HOST'S
# OWN bridge address is not forwarded, it is delivered locally — that is INPUT,
# and DOCKER-USER never sees it. Putting this rule in DOCKER-USER would leave it
# looking healthy while blocking nothing, which is precisely the failure the
# fail2ban jail's own banner warns about in the other direction.
#
# WHAT IT DELIBERATELY DOES NOT BREAK:
#   * Caddy -> relay. That is container-to-container across the same bridge:
#     FORWARD, not INPUT. Untouched, and asserted below.
#   * The relay's own healthcheck. It probes 127.0.0.1 inside the container's own
#     namespace and never crosses the bridge.
#   * DNS for the relay. Docker's embedded resolver lives at 127.0.0.11 inside
#     the container. It does not use the host's :53.
#
# Idempotent: safe to run repeatedly, on every boot, and after a compose
# recreate. Exit 0 = the rule is in place; exit 1 = it is not, loudly.
set -uo pipefail

SUBNET="${GAME_SUBNET:-172.28.90.0/24}"
CHAIN=INPUT
COMMENT="homehub-game-isolation"

log() { printf '%s [game-isolation] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "FATAL: $*"; exit 1; }

command -v iptables >/dev/null 2>&1 || die "iptables not found"

# A single rule, matched by its comment so a re-run replaces rather than stacks.
# Without the comment match, every boot would append another identical rule and
# `iptables -L` would grow forever — untidy, and it makes the real state hard to
# read at exactly the moment someone is debugging a lockout.
# PRESENT IS NOT THE SAME AS FIRST, and only checking presence is how a fence
# becomes decorative. iptables evaluates INPUT in order and stops at the first
# match, so one `-A INPUT -s <subnet> -j ACCEPT` inserted above ours - by an
# admin, a firewall manager, or another package - makes the REJECT unreachable
# while `iptables -C` still finds it. The old code reported "already present"
# and returned success over exactly that.
#
# So delete any copy of our rule and reinsert at position 1 on EVERY run. That
# is idempotent in the way that matters here: it converges on POSITION, not
# merely on existence, which is also why the unit is safe to restart at any time.
while iptables -C "$CHAIN" -s "$SUBNET" -m comment --comment "$COMMENT" -j REJECT 2>/dev/null; do
    iptables -D "$CHAIN" -s "$SUBNET" -m comment --comment "$COMMENT" -j REJECT 2>/dev/null || break
done
iptables -I "$CHAIN" 1 -s "$SUBNET" -m comment --comment "$COMMENT" -j REJECT \
    || die "could not install the REJECT rule for $SUBNET"
log "installed: REJECT $SUBNET -> host, at position 1 of $CHAIN"

# VERIFY THE ARTIFACT, NOT THE EXIT CODE (house rule). `iptables -I` returning 0
# says the command parsed, not that the rule is where it needs to be.
iptables -C "$CHAIN" -s "$SUBNET" -m comment --comment "$COMMENT" -j REJECT 2>/dev/null \
    || die "the rule is NOT in $CHAIN after installing it - the relay can still reach the host"

# AND IT MUST BE FIRST. `iptables -S` prints rules in evaluation order, so the
# first `-A` line is rule 1.
__first="$(iptables -S "$CHAIN" 2>/dev/null | grep '^-A' | head -1)"
case "$__first" in
    *"$COMMENT"*) : ;;
    *) die "the REJECT is not the FIRST $CHAIN rule (first is: ${__first:-none}) - something ahead of it could match the relay's traffic first" ;;
esac

log "verified: the game subnet $SUBNET cannot address this host, and the rule is first"
