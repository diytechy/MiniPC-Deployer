#!/usr/bin/env bash
# setup-tailscale.sh — install and converge the mesh-VPN subnet router.
#
# Implements: SR-042, LLR-959, LLR-960, LLR-961
#
# WHY THIS EXISTS AT ALL, in one line: this site is behind carrier-grade NAT,
# so no inbound path can be created from this side and the router's WireGuard
# server — correctly configured, port 51820, full-tunnel peers — is structurally
# unreachable. Measured 2026-09-18: the fibre ONT is a bridge, the router's WAN
# interface holds an RFC1918 address, and an HTTPS request to the apex from a
# cellular client timed out. An outbound-initiated tunnel is the only remote
# access that works without the ISP handing out a public address.
#
# THIS IS A HOST SERVICE AND NOT A COMPOSE SERVICE, DELIBERATELY. A subnet
# router edits the host routing table and the host forwarding sysctls, which is
# not a compose concern, and it must be up before the stack is — if the tunnel
# depended on docker, a docker problem would also be the thing that stops you
# reaching the box to fix it. `docker-compose.yml` must never gain a service for
# this; TC-976 asserts both halves so the two lanes cannot drift.
#
# OUTSIDE THE OFFLINE ISO CLOSURE, following SN-016's precedent rather than
# inventing a second rule. The daemon is not in the distribution's base archive;
# it comes from the vendor's signed APT repository, so it CANNOT be baked into
# the offline payload the way every core image is. Consequences, all of which
# REIMAGE_PERSISTENCE_PLAN.md must carry: this box needs connectivity when the
# script runs, a reimage does not restore it, and re-authorisation is manual.
#
# AUTHORISATION IS INTERACTIVE BY DESIGN AND THAT IS NOT AN OVERSIGHT. The
# script converges everything it can non-interactively, then prints the
# authorisation URL and STOPS. It accepts no auth key, has no flag that would
# take one, and writes none — because a long-lived key that enrols nodes is the
# one credential in this repo whose leak needs no other access to exploit: it
# admits a device to a tunnel that reaches the entire LAN. TC-977 asserts that
# no such flag or field exists anywhere in the repo, the payload or the store.
#
# Usage: setup-tailscale.sh [--check-only]
# Exit 0 = converged, or disabled, or waiting for the Owner to authorise.
#        1 = failed.
#        2 = refused (a precondition that must not be worked around).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TAILSCALE_ENV_FILE:-$HERE/../.env}"
SYSCTL_FILE="${TAILSCALE_SYSCTL_FILE:-/etc/sysctl.d/99-tailscale.conf}"
MODE="${1:-}"

# Defaults mirror stack/.env.example; the .env wins where it sets a key.
TAILSCALE_ENABLED="${TAILSCALE_ENABLED:-false}"
TAILSCALE_ADVERTISE_ROUTES="${TAILSCALE_ADVERTISE_ROUTES:-}"
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-homehub}"

if [ -f "$ENV_FILE" ]; then
    # Read only the keys we own, and never `source` a file that may hold
    # secrets into this shell's environment wholesale.
    for key in TAILSCALE_ENABLED TAILSCALE_ADVERTISE_ROUTES TAILSCALE_HOSTNAME; do
        value="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
        [ -n "$value" ] && printf -v "$key" '%s' "$value"
    done
fi

log() { printf '%s\n' "$*"; }
fail() { log "ERROR: $*"; exit 1; }
refuse() { log "REFUSED: $*"; exit 2; }

if [ "$TAILSCALE_ENABLED" != "true" ]; then
    log "TAILSCALE_ENABLED is not true - no mesh VPN, no subnet route, nothing installed."
    exit 0
fi

# ---------------------------------------------------------------------------
# Refusals. Each of these is a condition where continuing would produce a box
# that looks converged and is not, so they stop rather than warn.
# ---------------------------------------------------------------------------

# An auth key passed through the environment is the one thing this script will
# not do, no matter how convenient. Refusing loudly is the point: a future
# caller that tries this must find out here and not discover months later that
# an enrolment credential is sitting in a materialised .env on a backup drive.
for forbidden in TS_AUTHKEY TAILSCALE_AUTHKEY TAILSCALE_AUTH_KEY; do
    if [ -n "${!forbidden:-}" ]; then
        refuse "$forbidden is set. This script never accepts a reusable auth key
        (LLR-959). Authorise the node interactively from the URL it prints. An
        enrolment key admits a device to a tunnel that reaches the whole LAN,
        and unlike every other credential here it needs no other access to use."
    fi
done

if [ -z "$TAILSCALE_ADVERTISE_ROUTES" ]; then
    refuse "TAILSCALE_ADVERTISE_ROUTES is empty. Without a route this node is
    reachable but the LAN behind it is not, which is the entire purpose - the
    panel, mini-serv and every other host that will never run a tunnel client
    are reached THROUGH this node or not at all."
fi

# The advertised route must be the LAN alone. LAN_CIDR deliberately carries the
# LAN *and* the tunnel ranges because it is an allow-list; advertising a tunnel
# range as a subnet route would tell the tunnel to route its own address space
# through this host, which is a loop, not a route.
case "$TAILSCALE_ADVERTISE_ROUTES" in
    *100.64.*|*' '*)
        if printf '%s' "$TAILSCALE_ADVERTISE_ROUTES" | grep -q '100\.64\.'; then
            refuse "TAILSCALE_ADVERTISE_ROUTES names the tunnel's own CGNAT range.
            Advertise the LAN only; the tunnel routes its own space itself."
        fi
        ;;
esac

if [ "$MODE" = "--check-only" ]; then
    rc=0
    command -v tailscale >/dev/null 2>&1 || { log "check: tailscale not installed"; rc=1; }
    [ -f "$SYSCTL_FILE" ] || { log "check: $SYSCTL_FILE missing"; rc=1; }

    # NO ACL CHECK HERE, DELIBERATELY, AND THE ABSENCE IS THE FIX.
    #
    # An earlier version read a local tailnet-acl.hujson and failed when it was
    # missing or looked permissive. That was backwards: the policy in force
    # lives SERVER-SIDE in the coordination service, and tailscaled never reads
    # local disk for it. The check therefore confirmed that a file this repo
    # wrote said what this repo wrote in it - true no matter what policy the
    # tailnet is actually running, and false-green in the one case that matters
    # (a wide-open policy applied in the console with the tracked file intact).
    #
    # Verifying the real thing needs a tailnet API key. That key can rewrite who
    # reaches the entire LAN, so carrying one on this box to catch the Owner's
    # own console edits costs more than it saves under the standing threat model
    # (ruled 2026-08-09). The policy reference now lives in the repo as a
    # document to paste from, and is not deployed here at all.

    if command -v tailscale >/dev/null 2>&1; then
        # `tailscale status` exits non-zero when logged out, which is a real
        # state and not a script failure - report it rather than dying on it.
        if tailscale status >/dev/null 2>&1; then
            log "check: daemon up and authorised"

            # TAILNET LOCK IS QUERIED AND REPORTED, and it WARNS rather than
            # fails (LLR-975). The risk it addresses is a compromised
            # coordinator or identity account - an EXTERNAL failure worth
            # knowing about, not one worth refusing to converge over. An
            # earlier draft failed the check here; that was scaled back under
            # the Owner's standing threat model, which does not chase a
            # determined attacker and does not treat every unset hardening
            # option as a blocker.
            # MATCH THE NEGATIVE FIRST, AND THIS IS NOT PEDANTRY. The disabled
            # output is the sentence "Tailnet Lock is NOT enabled." - which
            # CONTAINS the word "enabled". An earlier version of this check was
            # `grep -qi enabled`, so it matched that sentence and reported the
            # lock as ON while it was off. It was caught only because the Owner
            # noticed the check disagreed with what the console had told him.
            # A substring test over human prose is not a state test.
            lock_state="$(tailscale lock status 2>/dev/null || true)"
            if printf '%s' "$lock_state" | grep -qiE 'lock is not enabled|not enabled'; then
                log "check: WARNING - tailnet lock is DISABLED. Optional; see the notes"
                log "        below on why it is not urgent on a single-user tailnet."
            elif printf '%s' "$lock_state" | grep -qiE 'lock is enabled|^enabled'; then
                log "check: tailnet lock ENABLED"
            else
                # Neither phrasing matched: the output changed, and guessing
                # which way is how the previous bug happened.
                log "check: tailnet lock state UNDETERMINED - output not recognised:"
                printf '%s\n' "$lock_state" | sed 's/^/          /'
            fi
        else
            log "check: daemon installed, NOT authorised (run without --check-only)"
        fi
    fi

    # Deliberately NOT checked and deliberately not claimed: whether the
    # identity account carries multi-factor authentication. This box cannot
    # observe that, and a test that read only local state and reported it green
    # would be worse than no test - it would retire the question. It is an
    # operator attestation in the runbook instead (LLR-975).
    log "check: identity-account MFA is NOT verifiable from this box - see the runbook attestation."
    exit "$rc"
fi

# ---------------------------------------------------------------------------
# 1. Forwarding sysctls, in their own file.
# ---------------------------------------------------------------------------
# A dedicated drop-in rather than an append to a shared file, so the setting is
# greppable, removable with the feature, and cannot be silently reverted by a
# distribution upgrade rewriting a file it owns. The 99- prefix sorts late on
# purpose so a lower-numbered file cannot win.
#
# NOTE FOR THE NEXT READER: this is a plain sysctl file, NOT a systemd drop-in.
# The rule that a list-valued systemd directive in a drop-in ADDS to the base
# value does not apply here - sysctl is last-write-wins. Confusing the two is
# how an override that is visibly present ends up doing nothing.
if [ ! -f "$SYSCTL_FILE" ] || ! grep -q 'net.ipv4.ip_forward *= *1' "$SYSCTL_FILE" 2>/dev/null; then
    log "Writing forwarding sysctls to $SYSCTL_FILE"
    install -m 0644 /dev/null "$SYSCTL_FILE"
    cat >"$SYSCTL_FILE" <<'SYSCTL'
# Installed by stack/tailscale/setup-tailscale.sh (SR-042, LLR-960).
# A subnet router must forward. Own file so it is greppable and removable with
# the feature; 99- so a lower-numbered file cannot win. Plain sysctl semantics
# (last write wins), NOT systemd drop-in accumulate semantics.
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
SYSCTL
    sysctl -p "$SYSCTL_FILE" >/dev/null || fail "sysctl -p $SYSCTL_FILE failed"
else
    log "Forwarding sysctls already present in $SYSCTL_FILE"
fi

# ---------------------------------------------------------------------------
# 2. Install the daemon from the vendor's signed repository.
# ---------------------------------------------------------------------------
if command -v tailscale >/dev/null 2>&1; then
    log "tailscale already installed ($(tailscale version 2>/dev/null | head -1))"
else
    log "Installing tailscale from the vendor APT repository (outside the offline closure)"
    command -v curl >/dev/null 2>&1 || fail "curl is required to fetch the repository key"
    . /etc/os-release
    keyring=/usr/share/keyrings/tailscale-archive-keyring.gpg
    listfile=/etc/apt/sources.list.d/tailscale.list
    curl -fsSL "https://pkgs.tailscale.com/stable/${ID}/${VERSION_CODENAME}.noarmor.gpg" \
        -o "$keyring" || fail "could not fetch the repository signing key"
    chmod 0644 "$keyring"
    curl -fsSL "https://pkgs.tailscale.com/stable/${ID}/${VERSION_CODENAME}.tailscale-keyring.list" \
        -o "$listfile" || fail "could not fetch the repository list"
    chmod 0644 "$listfile"
    apt-get update -qq || fail "apt-get update failed"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tailscale \
        || fail "apt-get install tailscale failed"
fi

systemctl enable --now tailscaled >/dev/null 2>&1 || fail "could not enable tailscaled"

# ---------------------------------------------------------------------------
# 3. Bring the node up.
# ---------------------------------------------------------------------------
# Three flags carry design decisions and none of them is incidental:
#
#   --advertise-routes   the LAN, so hosts that will never run a client are
#                        reachable through this one.
#   --advertise-exit-node is NOT passed. Household internet traffic must never
#                        transit the hub; an exit node is a different product
#                        decision with a different privacy shape.
#   --accept-dns=false   Technitium is this LAN's resolver. Accepting the
#                        tunnel's DNS would displace the split-horizon records
#                        that make every service name resolve to a LAN address.
#                        The same split-horizon, read by a different consumer,
#                        already produced a container that reported itself
#                        permanently unhealthy while working perfectly - so the
#                        cost of getting resolver authority wrong on this box is
#                        measured, not theoretical.
if tailscale status >/dev/null 2>&1; then
    log "Node already authorised; reconciling settings"
    tailscale set \
        --advertise-routes="$TAILSCALE_ADVERTISE_ROUTES" \
        --accept-dns=false \
        || fail "tailscale set failed"
else
    log ""
    log "================================================================"
    log " AUTHORISATION REQUIRED - this is the interactive step, by design."
    log " A URL follows. Open it as the Owner and approve this node."
    log " No auth key is used, accepted or stored (LLR-959)."
    log "================================================================"
    log ""
    tailscale up \
        --advertise-routes="$TAILSCALE_ADVERTISE_ROUTES" \
        --accept-dns=false \
        --hostname="$TAILSCALE_HOSTNAME" \
        || fail "tailscale up failed"
fi

# ---------------------------------------------------------------------------
# 4. Report honestly.
# ---------------------------------------------------------------------------
# THE ROUTE IS ADVERTISED, NOT ACTIVE, AND THIS SCRIPT MUST NOT SAY OTHERWISE.
# Approval happens in the admin console, which this script cannot see and has no
# authority over. Reporting "subnet routing active" here would be a claim about
# a system this box does not control - exactly the class of false green the
# repo's honesty bar exists to prevent. The operator finds out it is live by
# reaching a LAN host from a tunnel client, which is TC-979's hardware half.
log ""
log "Converged. Route advertised and PENDING APPROVAL in the admin console:"
log "    $TAILSCALE_ADVERTISE_ROUTES"
log ""
log "Still to do, and none of it can be done from this box:"
log "  1. Approve the subnet route in the admin console. REQUIRED - until this"
log "     happens the tunnel reaches THIS BOX and nothing behind it."
log "  2. Enable tailnet lock, if you want node additions signature-gated."
log ""
log "Optional, and NOT urgent on a single-user tailnet: an access policy. The"
log "default is 'every member reaches everything', and on a one-person tailnet"
log "every member is your own device - so the default means your laptop reaches"
log "your hub. It starts mattering when a second person, a shared node or a"
log "tagged service appears. A reference policy to paste into the console lives"
log "in the REPO at stack/tailscale/tailnet-acl.hujson; it is deliberately not"
log "deployed here, because nothing on this box reads it - the policy in force"
log "lives server-side and tailscaled never looks at local disk for it."
log ""
log "Not done and deliberately so: this node is NOT an exit node, and it does"
log "NOT accept the tunnel's DNS (Technitium stays this LAN's resolver)."
exit 0
