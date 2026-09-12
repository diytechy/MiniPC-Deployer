#!/usr/bin/env bash
# setup-ai-usage.sh — install the AI-usage feeder's unit and timer.
#
# Idempotent and non-interactive, like every other setup script here. It is
# called from firstboot only when AI_USAGE_ENABLED=true; on a box where the
# knob is false it does nothing at all, and nothing it would have installed
# exists. That is the whole of "profile-gated, OFF by default" for a service
# that is not a container: there is no compose profile to join, so the knob is
# the gate, exactly as it is for AI_CLI_ENABLED and REMOTE_UI_ENABLED.
#
# IT CREATES NO ACCOUNT. The feeder deliberately runs as the account the AI
# CLIs are signed into (AI_USAGE_USER_ACCOUNT, normally the same as
# AI_CLI_USER) because the credentials it READS live in that account's home.
# Creating and certifying an account is setup-ai-cli.sh's job and duplicating
# it here would give the box two places that decide what "unprivileged" means.
# This script therefore REFUSES a missing account rather than making one.
#
# THE ACCOUNT THIS SCRIPT CHECKS IS THE ACCOUNT SYSTEMD RUNS. Same construction
# as setup-ai-cli.sh after the 2026-09-09 cross-review: the unit ships a
# default name and this script writes User=/Group= into a drop-in from the
# knob, so a knob nobody read cannot diverge from a unit nobody edited.
#
# Adds NO apt package: python3 stdlib only, and python3 is already in
# packages.list. No apt export is owed.
#
# Usage: setup-ai-usage.sh [--check-only]
# Exit 0 = converged (or disabled). 1 = failed. 2 = refused.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${AI_USAGE_ENV_FILE:-$HERE/../.env}"
UNIT_DIR="${AI_USAGE_UNIT_DIR:-/etc/systemd/system}"
DROPIN_DIR="$UNIT_DIR/homehub-ai-usage.service.d"
MODE="${1:-}"

AI_USAGE_ENABLED="${AI_USAGE_ENABLED:-false}"
AI_USAGE_USER="${AI_USAGE_USER:-}"
AI_USAGE_USER_ACCOUNT="${AI_USAGE_USER_ACCOUNT:-homehub-ai}"
AI_USAGE_FEED_URL="${AI_USAGE_FEED_URL:-}"

if [ -f "$ENV_FILE" ]; then
    # Read only the keys we own, and never `source` a file that may hold
    # secrets into this shell's environment wholesale.
    for key in AI_USAGE_ENABLED AI_USAGE_USER AI_USAGE_USER_ACCOUNT AI_USAGE_FEED_URL; do
        value="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
        [ -n "$value" ] && printf -v "$key" '%s' "$value"
    done
fi

say() { printf '%s\n' "$*"; }
refuse() { say "REFUSED: $*"; exit 2; }

if [ "$AI_USAGE_ENABLED" != "true" ]; then
    say "AI_USAGE_ENABLED is not true - the usage feeder is not installed."
    exit 0
fi

# ── REFUSING TO GUESS THE IDENTITY, in the manner of TRACKER_DRIVE_USER ──────
# Blank is not "the only user" and it is not "all users": these gauges land on
# ONE household member's board, and the wrong board is somebody else's panel
# telling them a lie about their own subscriptions. The feeder itself refuses
# too (resolve_identity), so this is the early, legible copy of that refusal —
# a box that would have guessed fails at provisioning time, not at 03:00.
[ -n "$AI_USAGE_USER" ] || refuse \
    "AI_USAGE_USER is blank. The feeder posts for ONE explicit identity (the
    stable Google \`sub\`, the same value as NAGLIGHT_USER) and will not guess
    which household member's board these gauges belong on."

[ -n "$AI_USAGE_FEED_URL" ] || refuse \
    "AI_USAGE_FEED_URL is blank. There is no default - the tracker is
    bridge-only, so the right value depends on this box."

case "$AI_USAGE_USER_ACCOUNT" in
    hub|root) refuse "AI_USAGE_USER_ACCOUNT=$AI_USAGE_USER_ACCOUNT. \`hub\` carries
        (ALL) NOPASSWD: ALL and root needs no explanation; the feeder runs as the
        dedicated unprivileged account the CLIs are signed into." ;;
esac

if ! id -u "$AI_USAGE_USER_ACCOUNT" >/dev/null 2>&1; then
    refuse "no account \`$AI_USAGE_USER_ACCOUNT\` on this box. It is created and
    certified by setup-ai-cli.sh (AI_CLI_ENABLED=true); this script will not
    make a second place that decides what unprivileged means."
fi

if [ "$MODE" = "--check-only" ]; then
    say "check-only: identity set, destination set, account $AI_USAGE_USER_ACCOUNT exists."
    exit 0
fi

# CODEX'S WRITABLE HOME, owned by the account and holding nothing else.
# `codex app-server` cannot run with nowhere to write -- it opens SQLite
# databases, caches models and fetches plugins even to answer a rate-limit
# query -- and the unit keeps ProtectHome=read-only, so $CODEX_HOME points
# here instead. Deliberately a SIBLING of the feeder's StateDirectory, not a
# child: the feeder's write guard bounds writes to inside StateDirectory, and
# that bound is only meaningful while nothing in there is a credential.
# See the note in homehub-ai-usage.service.
install -d -m 0700 -o "$AI_USAGE_USER_ACCOUNT" -g "$AI_USAGE_USER_ACCOUNT"     /var/lib/homehub-ai-codex

install -d -m 0755 "$UNIT_DIR"
install -m 0644 "$HERE/homehub-ai-usage.service" "$UNIT_DIR/homehub-ai-usage.service"
install -m 0644 "$HERE/homehub-ai-usage.timer"   "$UNIT_DIR/homehub-ai-usage.timer"

# The drop-in is what makes the knob and the unit the same fact.
install -d -m 0755 "$DROPIN_DIR"
cat > "$DROPIN_DIR/10-account.conf" <<EOF
# GENERATED by setup-ai-usage.sh from AI_USAGE_USER_ACCOUNT. Do not hand-edit:
# the account certified and the account systemd runs must be one value.
[Service]
User=$AI_USAGE_USER_ACCOUNT
Group=$AI_USAGE_USER_ACCOUNT
EOF
chmod 0644 "$DROPIN_DIR/10-account.conf"

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    # The TIMER is enabled, never the service: enabling a oneshot service means
    # one run at boot and none after it, which is the classic way a feeder ends
    # up posting a single reading and then going quietly stale forever.
    systemctl enable --now homehub-ai-usage.timer
fi

say "AI-usage feeder installed; runs as $AI_USAGE_USER_ACCOUNT on the timer."
say "STILL OWED BY A HUMAN: the vendor sign-ins do not survive a reimage, and a"
say "  source with no credential posts 'unavailable' - correct, not a bug."
say ""
say "  SIGN CODEX IN WITH CODEX_HOME SET, or the unit will not find the token:"
say "    sudo -u $AI_USAGE_USER_ACCOUNT env CODEX_HOME=/var/lib/homehub-ai-codex \\"
say "        PATH=/usr/local/bin:/usr/bin:/bin codex login --device-auth"
say "  The unit keeps ProtectHome=read-only, so codex writes to that directory"
say "  and never to the account home. A sign-in done WITHOUT it lands in"
say "  ~/.codex, which the service cannot read - the gauge then stays"
say "  'unavailable' with the CLI insisting it is logged in."
say ""
say "  Claude and OpenCode are unaffected; they sign in normally:"
say "    sudo -u $AI_USAGE_USER_ACCOUNT env HOME=~$AI_USAGE_USER_ACCOUNT \\"
say "        PATH=/usr/local/bin:/usr/bin:/bin sh -c 'cd ~ && claude setup-token'"
exit 0
