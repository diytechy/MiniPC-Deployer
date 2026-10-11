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
AI_USAGE_SOURCES="${AI_USAGE_SOURCES:-codex,claude,opencode}"

if [ -f "$ENV_FILE" ]; then
    # Read only the keys we own, and never `source` a file that may hold
    # secrets into this shell's environment wholesale.
    for key in AI_USAGE_ENABLED AI_USAGE_USER AI_USAGE_USER_ACCOUNT AI_USAGE_FEED_URL AI_USAGE_SOURCES; do
        value="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
        [ -n "$value" ] && printf -v "$key" '%s' "$value"
    done
fi

say() { printf '%s\n' "$*"; }
refuse() { say "REFUSED: $*"; exit 2; }

# Is NAME one of the comma-separated AI_USAGE_SOURCES? Whitespace is ignored, as
# the feeder's own parser ignores it. The two newer readers (claude2, gemini) are
# OPT-IN: each needs its own sign-in, so a box that did not name them gets none
# of their units, which is what keeps a box that never asked unchanged.
source_enabled() {
    case ",${AI_USAGE_SOURCES//[[:space:]]/}," in
        *",$1,"*) return 0 ;;
        *) return 1 ;;
    esac
}

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
# The credential refresher travels with the feeder: without it the Claude
# access token (8 h) simply runs out on a box where nobody types `claude`, and
# both Claude gauges go "unavailable" with a 401 overnight. See the long note
# in homehub-claude-refresh@.service.
#
# IT IS A TEMPLATE NOW (one instance per Claude account: `claude`, `claude2`),
# and a box installed before that has the OLD, un-templated pair. The old pair
# is retired below, AFTER the new instance's files exist, so there is no moment
# at which no refresher is installed.
install -m 0644 "$HERE/homehub-claude-refresh@.service" "$UNIT_DIR/homehub-claude-refresh@.service"
install -m 0644 "$HERE/homehub-claude-refresh@.timer"   "$UNIT_DIR/homehub-claude-refresh@.timer"
# The Gemini CLI token keeper exists only for a box that opted into `gemini`.
if source_enabled gemini; then
    install -m 0644 "$HERE/homehub-gemini-refresh.service" "$UNIT_DIR/homehub-gemini-refresh.service"
    install -m 0644 "$HERE/homehub-gemini-refresh.timer"   "$UNIT_DIR/homehub-gemini-refresh.timer"
fi

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

# The refreshers run as the SAME account for the same reason: the credential
# they renew is the one that account signed in with. One source of truth for the
# name, units that must agree on it.
#
# The credential paths travel in each unit's drop-in, from the account's REAL
# home. They are not written into the unit with `%h`: in a system unit that
# expands to the manager's home (/root), and systemd then refuses to start at
# all with 226/NAMESPACE. One knob decides the account; the same knob decides
# where its credential lives.
REFRESH_HOME="$(getent passwd "$AI_USAGE_USER_ACCOUNT" | cut -d: -f6)"
[ -n "$REFRESH_HOME" ] || refuse     "cannot resolve the home directory of $AI_USAGE_USER_ACCOUNT, so the
    credential refresher has no path to re-open for writing."

# Account 1: the CLI's DEFAULT config, exactly the two paths the un-templated
# unit always granted. `claude` is the instance name AND the reader name.
CLAUDE1_DROPIN_DIR="$UNIT_DIR/homehub-claude-refresh@claude.service.d"
install -d -m 0755 "$CLAUDE1_DROPIN_DIR"
cat > "$CLAUDE1_DROPIN_DIR/10-account.conf" <<EOF
# GENERATED by setup-ai-usage.sh from AI_USAGE_USER_ACCOUNT. Do not hand-edit.
[Service]
User=$AI_USAGE_USER_ACCOUNT
Group=$AI_USAGE_USER_ACCOUNT
ReadWritePaths=$REFRESH_HOME/.claude $REFRESH_HOME/.claude.json
EOF
chmod 0644 "$CLAUDE1_DROPIN_DIR/10-account.conf"

# Account 2, only when named in AI_USAGE_SOURCES. Its whole state is ONE
# directory, because with CLAUDE_CONFIG_DIR set the CLI keeps `.claude.json` in
# it (measured on WINDOWS only, 2026-10-11, Claude Code 2.1.296; unverified
# on the hub's Linux build) -- so the grant is that directory and not a byte of
# account 1's, and it stays that way even if the Linux build disagrees. The directory is created empty (0700)
# because ReadWritePaths= on a missing path fails the unit with 226/NAMESPACE;
# the sign-in that fills it is a human's, printed below.
CLAUDE2_DROPIN_DIR="$UNIT_DIR/homehub-claude-refresh@claude2.service.d"
CLAUDE2_CONFIG_DIR="$REFRESH_HOME/.claude-2"
if source_enabled claude2; then
    install -d -m 0700 -o "$AI_USAGE_USER_ACCOUNT" -g "$AI_USAGE_USER_ACCOUNT" "$CLAUDE2_CONFIG_DIR"
    install -d -m 0755 "$CLAUDE2_DROPIN_DIR"
    cat > "$CLAUDE2_DROPIN_DIR/10-account.conf" <<EOF
# GENERATED by setup-ai-usage.sh from AI_USAGE_USER_ACCOUNT. Do not hand-edit.
[Service]
User=$AI_USAGE_USER_ACCOUNT
Group=$AI_USAGE_USER_ACCOUNT
Environment=CLAUDE_CONFIG_DIR=$CLAUDE2_CONFIG_DIR
ReadWritePaths=$CLAUDE2_CONFIG_DIR
EOF
    chmod 0644 "$CLAUDE2_DROPIN_DIR/10-account.conf"
fi

# The Gemini CLI keeps its credential, settings and scratch state in ~/.gemini,
# which is therefore the one directory its keeper may write.
GEMINI_DROPIN_DIR="$UNIT_DIR/homehub-gemini-refresh.service.d"
if source_enabled gemini; then
    install -d -m 0700 -o "$AI_USAGE_USER_ACCOUNT" -g "$AI_USAGE_USER_ACCOUNT" "$REFRESH_HOME/.gemini"
    install -d -m 0755 "$GEMINI_DROPIN_DIR"
    cat > "$GEMINI_DROPIN_DIR/10-account.conf" <<EOF
# GENERATED by setup-ai-usage.sh from AI_USAGE_USER_ACCOUNT. Do not hand-edit.
[Service]
User=$AI_USAGE_USER_ACCOUNT
Group=$AI_USAGE_USER_ACCOUNT
ReadWritePaths=$REFRESH_HOME/.gemini
EOF
    chmod 0644 "$GEMINI_DROPIN_DIR/10-account.conf"
fi

# A source dropped from AI_USAGE_SOURCES takes its keeper with it: converging
# means the box ends up as the knob says, in both directions.
if ! source_enabled claude2; then
    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now homehub-claude-refresh@claude2.timer >/dev/null 2>&1 || true
    fi
    rm -rf "$CLAUDE2_DROPIN_DIR"
fi
if ! source_enabled gemini; then
    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now homehub-gemini-refresh.timer >/dev/null 2>&1 || true
    fi
    rm -f "$UNIT_DIR/homehub-gemini-refresh.service" "$UNIT_DIR/homehub-gemini-refresh.timer"
    rm -rf "$GEMINI_DROPIN_DIR"
fi

# ── MIGRATION: the un-templated refresher this script used to install ───────
# `homehub-claude-refresh.{service,timer}` (+ its .service.d drop-in) is account
# 1's refresher under its OLD name. It is retired here and replaced by the
# `@claude` instance above, which has the same user, the same two writable
# paths and the same command -- so nothing about account 1 changes but the unit
# name. Both timers running would be two processes refreshing ONE rotating
# credential at once, which is the one thing that must not happen; so the old
# timer is stopped before the new one is enabled (below), never alongside.
#
# Disabling is best-effort and idempotent: on a box that never had the old pair
# every line here is a no-op.
LEGACY_REFRESH="homehub-claude-refresh"
if [ -e "$UNIT_DIR/$LEGACY_REFRESH.timer" ] || [ -e "$UNIT_DIR/$LEGACY_REFRESH.service" ] \
        || [ -d "$UNIT_DIR/$LEGACY_REFRESH.service.d" ]; then
    if command -v systemctl >/dev/null 2>&1; then
        # Stop the TIMER first, so no new run can begin, then WAIT for a run that
        # is already going. The service is deliberately never `stop`ped: a
        # `claude -p /usage` killed in the middle of a token rotation can lose
        # the single-use refresh token, and the Owner would have to sign in
        # again. The wait is bounded (longer than the unit's own
        # TimeoutStartSec=180, so a healthy run has finished or been timed out by
        # systemd itself); if it still has not finished we say so and carry on.
        systemctl disable --now "$LEGACY_REFRESH.timer" >/dev/null 2>&1 || true
        wait_seconds="${AI_USAGE_MIGRATION_WAIT_SECONDS:-200}"
        poll_seconds="${AI_USAGE_MIGRATION_POLL_SECONDS:-2}"
        deadline=$((SECONDS + wait_seconds))
        while :; do
            state="$(systemctl is-active "$LEGACY_REFRESH.service" 2>/dev/null || true)"
            case "$state" in
                active|activating|deactivating|reloading) ;;
                *) break ;;
            esac
            if [ "$SECONDS" -ge "$deadline" ]; then
                say "WARNING: $LEGACY_REFRESH.service was still $state after ${wait_seconds}s."
                say "  Continuing the migration WITHOUT stopping it (killing it mid-rotation"
                say "  could lose the single-use Claude refresh token). If the Claude ring"
                say "  goes unavailable afterwards, sign account 1 in again."
                break
            fi
            sleep "$poll_seconds"
        done
    fi
    rm -f "$UNIT_DIR/$LEGACY_REFRESH.timer" "$UNIT_DIR/$LEGACY_REFRESH.service"
    rm -rf "$UNIT_DIR/$LEGACY_REFRESH.service.d"
    say "Migrated the un-templated $LEGACY_REFRESH unit to $LEGACY_REFRESH@claude (same account, same paths)."
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    # The TIMER is enabled, never the service: enabling a oneshot service means
    # one run at boot and none after it, which is the classic way a feeder ends
    # up posting a single reading and then going quietly stale forever.
    systemctl enable --now homehub-ai-usage.timer
    systemctl enable --now homehub-claude-refresh@claude.timer
    if source_enabled claude2; then
        systemctl enable --now homehub-claude-refresh@claude2.timer
    fi
    if source_enabled gemini; then
        systemctl enable --now homehub-gemini-refresh.timer
    fi
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
say "  Claude (account 1) signs in normally. THE COMMAND IS \`claude auth login\`,"
say "  NOT \`claude setup-token\`: that mints an automation token without the"
say "  user:profile scope the usage endpoint needs, so the gauge would read"
say "  'unavailable' with a good-looking login:"
say "    sudo -u $AI_USAGE_USER_ACCOUNT env HOME=$REFRESH_HOME \\"
say "        PATH=/usr/local/bin:/usr/bin:/bin sh -c 'cd ~ && claude auth login --claudeai'"
say "  It needs a terminal to paste the returned code into (tmux works)."
say ""
say "  OpenCode signs in normally too (\`opencode auth login\`, same wrapper,"
say "  from the account's home directory)."
if source_enabled claude2; then
    say ""
    say "  Claude account 2 is a SEPARATE login in its own config directory. Sign in"
    say "  as the second account, with CLAUDE_CONFIG_DIR set, once:"
    say "    sudo -u $AI_USAGE_USER_ACCOUNT env HOME=$REFRESH_HOME \\"
    say "        CLAUDE_CONFIG_DIR=$CLAUDE2_CONFIG_DIR \\"
    say "        PATH=/usr/local/bin:/usr/bin:/bin sh -c 'cd ~ && claude auth login --claudeai'"
    say "  Never copy account 1's files into it: the refresh token is single-use."
    say "  Set AI_USAGE_CLAUDE2_CREDENTIALS=$CLAUDE2_CONFIG_DIR/.credentials.json."
fi
if source_enabled gemini; then
    say ""
    say "  Gemini signs in through the CLI itself, as the same account, once"
    say "  (choose 'Login with Google'; NO_BROWSER prints a link and asks for a code):"
    say "    sudo -u $AI_USAGE_USER_ACCOUNT env HOME=$REFRESH_HOME NO_BROWSER=true \\"
    say "        PATH=/usr/local/bin:/usr/bin:/bin sh -c 'cd ~ && gemini'"
    say "  It must be an account the CLI has onboarded (run one prompt). The feeder"
    say "  reads $REFRESH_HOME/.gemini/oauth_creds.json and never writes it."
fi
exit 0
