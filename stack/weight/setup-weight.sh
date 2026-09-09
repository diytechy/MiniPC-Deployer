#!/usr/bin/env bash
# setup-weight.sh — install the weight feeder's unit and timer.
#
# Idempotent and non-interactive, like every other setup script here. It is
# called from firstboot only when WEIGHT_ENABLED=true; on a box where the knob
# is false it does nothing at all, and nothing it would have installed exists.
# That is the whole of "gated, OFF by default" for a service that is not a
# container: there is no compose profile to join, so the knob IS the gate,
# exactly as it is for AI_CLI_ENABLED, AI_USAGE_ENABLED and REMOTE_UI_ENABLED.
#
# IT CREATES THE ACCOUNT, unlike setup-ai-usage.sh — and the difference is
# deliberate. The usage feeder had to run as the account the AI CLIs were
# signed into, because the credentials it reads live in that account's home.
# This feeder's credential is a refresh token the Owner mints for THIS purpose,
# so it gets its own account with nothing else in it, and the token's home is
# that account's home. A weight feeder sharing a home with three AI CLI
# sessions would be a wider blast radius for no gain.
#
# THE ACCOUNT AND THE DEFINITIONS PATH THIS SCRIPT CHECKS ARE THE ONES SYSTEMD
# USES. Both are written into a drop-in from the knobs, the same construction
# setup-ai-cli.sh and setup-ai-usage.sh arrived at after the 2026-09-09
# cross-review, so a knob nobody read cannot diverge from a unit nobody edited.
#
# Adds NO apt package: python3 stdlib only, and python3 is already in
# packages.list. No apt export is owed.
#
# Usage: setup-weight.sh [--check-only]
# Exit 0 = converged (or disabled). 1 = failed. 2 = refused.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${WEIGHT_ENV_FILE:-$HERE/../.env}"
UNIT_DIR="${WEIGHT_UNIT_DIR:-/etc/systemd/system}"
DROPIN_DIR="$UNIT_DIR/homehub-weight.service.d"
MODE="${1:-}"

WEIGHT_ENABLED="${WEIGHT_ENABLED:-false}"
WEIGHT_USER="${WEIGHT_USER:-}"
WEIGHT_USER_ACCOUNT="${WEIGHT_USER_ACCOUNT:-homehub-weight}"
WEIGHT_FEED_URL="${WEIGHT_FEED_URL:-}"
WEIGHT_DEFINITIONS_DIR="${WEIGHT_DEFINITIONS_DIR:-}"

if [ -f "$ENV_FILE" ]; then
    # Read only the keys we own, and never `source` a file that may hold
    # secrets into this shell's environment wholesale.
    for key in WEIGHT_ENABLED WEIGHT_USER WEIGHT_USER_ACCOUNT WEIGHT_FEED_URL \
               WEIGHT_DEFINITIONS_DIR; do
        value="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
        [ -n "$value" ] && printf -v "$key" '%s' "$value"
    done
fi

say() { printf '%s\n' "$*"; }
refuse() { say "REFUSED: $*"; exit 2; }

if [ "$WEIGHT_ENABLED" != "true" ]; then
    say "WEIGHT_ENABLED is not true - the weight feeder is not installed."
    exit 0
fi

# ── REFUSING TO GUESS THE IDENTITY, in the manner of TRACKER_DRIVE_USER ──────
# Blank is not "the only user" and it is not "all users". This gauge lands on
# ONE household member's board, and the wrong board is a private measurement
# displayed to the wrong person on a wall in a shared room. The feeder refuses
# too (resolve_identity), so this is the early, legible copy of that refusal —
# a box that would have guessed fails at provisioning time, not at 03:00.
[ -n "$WEIGHT_USER" ] || refuse \
    "WEIGHT_USER is blank. The feeder posts for ONE explicit identity (the
    stable Google \`sub\`, the same value as NAGLIGHT_USER) and will not guess
    whose body weight this is."

[ -n "$WEIGHT_FEED_URL" ] || refuse \
    "WEIGHT_FEED_URL is blank. There is no default - the tracker is
    bridge-only, so the right value depends on this box."

# ── THE GOAL LIVES IN THE USER'S DEFINITIONS (SN-040), SO THE PATH IS OWED ───
# There is deliberately no WEIGHT_GOAL knob to fall back on. If this is blank
# the feeder has nowhere to read the goal from, and a feeder with no goal posts
# nothing at all, because the goal IS the gauge's target line.
[ -n "$WEIGHT_DEFINITIONS_DIR" ] || refuse \
    "WEIGHT_DEFINITIONS_DIR is blank. The goal lives in the user's own
    definitions so it syncs with them (SN-040), which means this box has to be
    told where that user's definitions dir is. There is no WEIGHT_GOAL knob to
    fall back on ON PURPOSE: a goal on the hub would be a second home for the
    household's intent that never syncs anywhere."

[ -d "$WEIGHT_DEFINITIONS_DIR" ] || refuse \
    "WEIGHT_DEFINITIONS_DIR=$WEIGHT_DEFINITIONS_DIR is not a directory on this
    box. In multi-user mode it is <tracker data root>/<the Google sub>/definitions."

case "$WEIGHT_USER_ACCOUNT" in
    hub|root) refuse "WEIGHT_USER_ACCOUNT=$WEIGHT_USER_ACCOUNT. \`hub\` carries
        (ALL) NOPASSWD: ALL and root needs no explanation; the feeder runs as a
        dedicated unprivileged account with nothing else in its home." ;;
esac

if [ "$MODE" = "--check-only" ]; then
    say "check-only: identity set, destination set, definitions dir present."
    exit 0
fi

if ! id -u "$WEIGHT_USER_ACCOUNT" >/dev/null 2>&1; then
    # --system, no shell, no password. The account exists to own one home
    # holding one refresh token and to run one oneshot unit.
    useradd --system --create-home --shell /usr/sbin/nologin "$WEIGHT_USER_ACCOUNT"
    say "created system account $WEIGHT_USER_ACCOUNT (no shell, no password)."
fi

install -d -m 0755 "$UNIT_DIR"
install -m 0644 "$HERE/homehub-weight.service" "$UNIT_DIR/homehub-weight.service"
install -m 0644 "$HERE/homehub-weight.timer"   "$UNIT_DIR/homehub-weight.timer"

# The drop-in is what makes the knobs and the unit the same fact. The
# definitions bind is READ-ONLY: the goal is the tracker's data and this feeder
# must never be able to write a definitions file.
install -d -m 0755 "$DROPIN_DIR"
cat > "$DROPIN_DIR/10-account.conf" <<EOF
# GENERATED by setup-weight.sh from WEIGHT_USER_ACCOUNT and
# WEIGHT_DEFINITIONS_DIR. Do not hand-edit: the account certified and the
# account systemd runs must be one value, and so must the definitions path.
[Service]
User=$WEIGHT_USER_ACCOUNT
Group=$WEIGHT_USER_ACCOUNT
BindReadOnlyPaths=$WEIGHT_DEFINITIONS_DIR
EOF
chmod 0644 "$DROPIN_DIR/10-account.conf"

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    # The TIMER is enabled, never the service: enabling a oneshot service means
    # one run at boot and none after it, which is the classic way a feeder ends
    # up posting a single reading and then going quietly stale forever.
    systemctl enable --now homehub-weight.timer
fi

say "weight feeder installed; runs as $WEIGHT_USER_ACCOUNT on the timer."
say "STILL OWED BY A HUMAN, and the feeder posts 'unavailable' until it is done:"
say "  1. enable health.googleapis.com on the Cloud project that owns the"
say "     existing OAuth client;"
say "  2. add the scope"
say "       https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"
say "     to that client's consent screen, and the Owner to its Test users;"
say "  3. consent in a browser and mint a refresh token into WEIGHT_TOKEN_FILE."
say "  Until then the gauge reads 'unavailable' - which is correct, and is not"
say "  a bug to chase."
exit 0
