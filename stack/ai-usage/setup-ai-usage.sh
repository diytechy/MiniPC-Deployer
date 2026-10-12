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
# ONE EFFECTIVE ACCOUNT, created when absent (plan 3.4, Owner 2026-10-11). The
# feeder runs as the account the AI CLIs are signed into, because the
# credentials it READS live in that account's home. That account is
# AI_CLI_USER when AI_CLI_ENABLED=true, else AI_USAGE_USER_ACCOUNT. Both
# consumers share StateDirectory=homehub-ai, so two differently named accounts
# would fight over its ownership: when the names differ this script does NOT
# refuse - it uses the effective account EVERYWHERE (unit User=, Codex-home
# ownership, REFRESH_HOME, every refresher drop-in) and logs one line. Creating
# and certifying the account is ensure-ai-account.sh's job, shared with
# setup-ai-cli.sh and firstboot, so there is still one place that decides what
# "unprivileged" means. hub and root are still refused. Credentials owned by a
# previous account are never migrated; ai-cli-status.sh reports them.
#
# THE ACCOUNT THIS SCRIPT CHECKS IS THE ACCOUNT SYSTEMD RUNS. Same construction
# as setup-ai-cli.sh after the 2026-09-09 cross-review: the unit ships a
# default name and this script writes User=/Group= into a drop-in from the
# effective account, so a knob nobody read cannot diverge from a unit nobody
# edited.
#
# Adds NO apt package: python3 stdlib only, and python3 is already in
# packages.list. No apt export is owed.
#
# Usage: setup-ai-usage.sh [--check-only]
# Exit 0 = converged (or disabled). 1 = failed. 2 = refused. 3 = converged EXCEPT the
# legacy Claude refresher could not be retired yet (migration deferred, old unit kept).
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
AI_CLI_ENABLED="${AI_CLI_ENABLED:-false}"
AI_CLI_USER="${AI_CLI_USER:-}"
ENSURE_ACCOUNT="${AI_CLI_ENSURE_ACCOUNT:-$HERE/../ai-cli/ensure-ai-account.sh}"
# The unit hard-codes this path (CODEX_HOME + ReadWritePaths); the override exists
# only so a test can point setup at a sandbox.
CODEX_HOME_DIR="${AI_USAGE_CODEX_HOME:-/var/lib/homehub-ai-codex}"

# One truthiness rule for the whole stack (systemd's EnvironmentFile semantics):
# strip ONE matched pair of quotes, then compare EXACTLY (`true`, not `True`).
unquote() {
    local v="${1%$'\r'}"
    case "$v" in
        \"*\") v="${v#\"}"; v="${v%\"}" ;;
        \'*\') v="${v#\'}"; v="${v%\'}" ;;
    esac
    printf '%s' "$v"
}

if [ -f "$ENV_FILE" ]; then
    # Read only the keys we own, and never `source` a file that may hold
    # secrets into this shell's environment wholesale.
    for key in AI_USAGE_ENABLED AI_USAGE_USER AI_USAGE_USER_ACCOUNT AI_USAGE_FEED_URL AI_USAGE_SOURCES AI_CLI_ENABLED AI_CLI_USER; do
        value="$(unquote "$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)")"
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

# A state directory the account will write: created (0700, the account's) only
# when ABSENT. One that already exists is left exactly as it is - never chowned
# or chmodded - because it may hold the previous account's credentials, which are
# never migrated (a chown would hand the directory to an account that cannot
# read the 0600 files inside it, and lock the previous owner out).
own_dir() {
    local owner
    if [ ! -e "$1" ]; then
        install -d -m 0700 -o "$ACCOUNT" -g "$ACCOUNT" "$1"
        return 0
    fi
    owner="$(stat -c %U "$1" 2>/dev/null || true)"
    if [ -n "$owner" ] && [ "$owner" != "$ACCOUNT" ]; then
        say "NOTE: $1 already exists and belongs to $owner, not $ACCOUNT. It is left"
        say "  as it is: credentials are never moved between accounts. The status"
        say "  step below reports what that means for the sign-ins."
    fi
    return 0
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

# The effective account (see the header). Everything below uses ACCOUNT; the
# raw AI_USAGE_USER_ACCOUNT is read once, here, and never again.
# ensure-ai-account.sh strips one pair of quotes itself, so it is handed the RAW
# values: unquoting here as well would strip a second pair ("'true'" must stay off).
raw_env() {
    local v
    v="$(grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
    if [ -n "$v" ]; then printf '%s' "$v"; else printf '%s' "${!1:-}"; fi
}
ACCOUNT="$(bash "$ENSURE_ACCOUNT" --effective "$(raw_env AI_CLI_ENABLED)" "$(raw_env AI_CLI_USER)" "$(raw_env AI_USAGE_USER_ACCOUNT)")"
if [ "$AI_CLI_ENABLED" = "true" ] && [ "$ACCOUNT" != "$AI_USAGE_USER_ACCOUNT" ]; then
    say "usage feeder and AI CLI service share one account: $ACCOUNT (AI_CLI_USER)."
fi

case "$ACCOUNT" in
    hub|root) refuse "the service account is $ACCOUNT. \`hub\` carries
        (ALL) NOPASSWD: ALL and root needs no explanation; the feeder runs as the
        dedicated unprivileged account the CLIs are signed into." ;;
esac

if [ "$MODE" = "--check-only" ]; then
    say "check-only: identity set, destination set, account $ACCOUNT (created at install if absent)."
    exit 0
fi

# Create and certify the account instead of refusing its absence.
ensure_rc=0
bash "$ENSURE_ACCOUNT" "$ACCOUNT" || ensure_rc=$?
case "$ensure_rc" in
    0) ;;
    2) refuse "$ACCOUNT was not certified as an unprivileged account (reason above)." ;;
    *) say "FAILED: could not create or certify $ACCOUNT"; exit 1 ;;
esac

# CODEX'S WRITABLE HOME, owned by the account and holding nothing else.
# `codex app-server` cannot run with nowhere to write -- it opens SQLite
# databases, caches models and fetches plugins even to answer a rate-limit
# query -- and the unit keeps ProtectHome=read-only, so $CODEX_HOME points
# here instead. Deliberately a SIBLING of the feeder's StateDirectory, not a
# child: the feeder's write guard bounds writes to inside StateDirectory, and
# that bound is only meaningful while nothing in there is a credential.
# See the note in homehub-ai-usage.service.
# CREATED ALWAYS, whatever AI_USAGE_SOURCES says: homehub-ai-usage.service lists
# it in ReadWritePaths= unconditionally, and a missing path there fails the
# unit's namespace setup before the feeder runs (e.g. sources=claude,gemini).
own_dir "$CODEX_HOME_DIR"

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
# GENERATED by setup-ai-usage.sh from the effective account. Do not hand-edit:
# the account certified and the account systemd runs must be one value.
[Service]
User=$ACCOUNT
Group=$ACCOUNT
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
REFRESH_HOME="$(getent passwd "$ACCOUNT" | cut -d: -f6)"
[ -n "$REFRESH_HOME" ] || refuse     "cannot resolve the home directory of $ACCOUNT, so the
    credential refresher has no path to re-open for writing."

# The credential gate each keeper's drop-in ADDS (see the unit files): an ExecCondition
# that exits 1 when nothing at all is at the path. `$$1` is systemd's escape for a
# literal `$1`. Stored in a variable so no heredoc re-expands it.
KEEPER_GATE='ExecCondition=/bin/sh -c '\''[ -e "$$1" ] || [ -L "$$1" ]'\'' gate'

# Account 1: the CLI's DEFAULT config, exactly the two paths the un-templated
# unit always granted. `claude` is the instance name AND the reader name.
CLAUDE1_DROPIN_DIR="$UNIT_DIR/homehub-claude-refresh@claude.service.d"
install -d -m 0755 "$CLAUDE1_DROPIN_DIR"
cat > "$CLAUDE1_DROPIN_DIR/10-account.conf" <<EOF
# GENERATED by setup-ai-usage.sh from the effective account. Do not hand-edit.
# No sign-in yet = "login owed": skipped quietly, not failed. The timer stays
# enabled, so the first run after the sign-in needs no re-run of this script.
# ExecCondition= ADDS to the base unit's CLI gate (it must not reset it): both
# must pass, and only an EMPTY path is skipped (a dangling link runs and fails loudly).
[Service]
User=$ACCOUNT
Group=$ACCOUNT
ReadWritePaths=$REFRESH_HOME/.claude $REFRESH_HOME/.claude.json
$KEEPER_GATE $REFRESH_HOME/.claude/.credentials.json
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
    own_dir "$CLAUDE2_CONFIG_DIR"
    install -d -m 0755 "$CLAUDE2_DROPIN_DIR"
    cat > "$CLAUDE2_DROPIN_DIR/10-account.conf" <<EOF
# GENERATED by setup-ai-usage.sh from the effective account. Do not hand-edit.
[Service]
User=$ACCOUNT
Group=$ACCOUNT
Environment=CLAUDE_CONFIG_DIR=$CLAUDE2_CONFIG_DIR
ReadWritePaths=$CLAUDE2_CONFIG_DIR
$KEEPER_GATE $CLAUDE2_CONFIG_DIR/.credentials.json
EOF
    chmod 0644 "$CLAUDE2_DROPIN_DIR/10-account.conf"
fi

# The Gemini CLI keeps its credential, settings and scratch state in ~/.gemini,
# which is therefore the one directory its keeper may write.
GEMINI_DROPIN_DIR="$UNIT_DIR/homehub-gemini-refresh.service.d"
if source_enabled gemini; then
    own_dir "$REFRESH_HOME/.gemini"
    install -d -m 0755 "$GEMINI_DROPIN_DIR"
    cat > "$GEMINI_DROPIN_DIR/10-account.conf" <<EOF
# GENERATED by setup-ai-usage.sh from the effective account. Do not hand-edit.
[Service]
User=$ACCOUNT
Group=$ACCOUNT
ReadWritePaths=$REFRESH_HOME/.gemini
$KEEPER_GATE $REFRESH_HOME/.gemini/oauth_creds.json
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
OLD_TIMER="$LEGACY_REFRESH.timer"
NEW_TIMER="homehub-claude-refresh@claude.timer"

# INVARIANT, on every way out of this script (0, 3, or an error part-way):
# EXACTLY ONE account-1 refresh timer is enabled and active - the old one or the
# new one, never both and never neither. Every decision below is made from the
# REAL state of both timers (never from what a previous run was "supposed" to
# have done): a rerun after a half-finished run must not re-enable the old timer
# beside an active new one.
#
#   timer_state UNIT -> on      (enabled AND active)
#                       off     (not enabled AND not active)
#                       partial (CONFIRMED half-applied: enabled but not active, or
#                                active but not enabled)
#                       unknown (a query failed or said something unrecognised)
#
# THE RULE: `unknown` never leads to a disable or an enable. Only a CONFIRMED
# state moves a timer; an unknown one is left exactly as it is, with an ERROR that
# names the check commands, and the script exits 3. (Callers, all audited:
# restore_legacy_timer x2, the start-of-migration reads x2, the old-timer verify
# in the already-migrated branch, and the new-timer check after its enable.)
timer_state() {
    local en ac
    en="$(systemctl is-enabled "$1" 2>/dev/null || true)"
    ac="$(systemctl is-active "$1" 2>/dev/null || true)"
    case "$en" in
        enabled)
            case "$ac" in
                active) echo on; return 0 ;;
                inactive|failed|dead) echo partial; return 0 ;;
            esac ;;
        disabled|not-found|static|indirect|linked|masked|generated)
            case "$ac" in
                inactive|failed|dead) echo off; return 0 ;;
                active) echo partial; return 0 ;;
            esac ;;
    esac
    echo unknown
}

# LEGACY_BLOCKED=1: the migration is deferred and the old timer (if it is allowed
# to be) is the refresher. LEGACY_ARMED=1: between disabling the old timer and the
# new one being confirmed on, any exit puts the old timer back - but ONLY when the
# new timer is confirmed off.
LEGACY_BLOCKED=0
LEGACY_PRESENT=0
LEGACY_ARMED=0
NEW_ENABLE_ATTEMPTED=0
restore_legacy_timer() {
    [ "$LEGACY_ARMED" = 1 ] || return 0
    LEGACY_ARMED=0
    new_now="$(timer_state "$NEW_TIMER")"
    # We read the new timer as OFF before touching anything. If we have not tried to
    # enable it since, a failing query does not change that.
    if [ "$new_now" = unknown ] && [ "$NEW_ENABLE_ATTEMPTED" = 0 ]; then
        new_now=off
    fi
    case "$new_now" in
        on)
            say "The new $NEW_TIMER is running and stays the Claude account-1 refresher;"
            say "  the old timer is NOT re-enabled (two would race on one single-use token)."
            return 0 ;;
        off) ;;
        *)
            say "ERROR: cannot tell whether $NEW_TIMER is running, so nothing was changed."
            say "  Claude account 1 must have exactly ONE refresher. Check, as root:"
            say "    systemctl is-active $NEW_TIMER $OLD_TIMER"
            say "  then enable the one that is not running, and disable the other:"
            say "    systemctl enable --now $OLD_TIMER ; systemctl disable --now $NEW_TIMER"
            return 0 ;;
    esac
    if systemctl enable --now "$OLD_TIMER" >/dev/null 2>&1; then
        if [ "$(timer_state "$OLD_TIMER")" = on ]; then
            say "The old $OLD_TIMER is enabled again and is the Claude account-1 refresher."
        else
            say "WARNING: $OLD_TIMER was enabled but its state cannot be confirmed. Check, as root:"
            say "    systemctl is-active $OLD_TIMER"
        fi
    else
        say "ERROR: could NOT re-enable $OLD_TIMER. Claude account 1 has NO"
        say "  refresher right now and its token will lapse. Run, as root:"
        say "    systemctl enable --now $OLD_TIMER"
    fi
}
trap restore_legacy_timer EXIT

LEGACY_COMPLETE=0
if [ -e "$UNIT_DIR/$OLD_TIMER" ] || [ -e "$UNIT_DIR/$LEGACY_REFRESH.service" ] \
        || [ -d "$UNIT_DIR/$LEGACY_REFRESH.service.d" ]; then
    LEGACY_PRESENT=1
    if command -v systemctl >/dev/null 2>&1; then
        systemctl daemon-reload || true      # so the instance's state can be read
        new_state="$(timer_state "$NEW_TIMER")"
        old_state=absent
        [ -e "$UNIT_DIR/$OLD_TIMER" ] && old_state="$(timer_state "$OLD_TIMER")"
        case "$new_state" in
            on)
                # A previous run already activated the replacement. Only the
                # cleanup is left, and the old timer must NEVER come back.
                LEGACY_COMPLETE=1
                if [ "$old_state" != off ] && [ "$old_state" != absent ]; then
                    systemctl disable --now "$OLD_TIMER" >/dev/null 2>&1 || true
                    if [ "$(timer_state "$OLD_TIMER")" != off ]; then
                        LEGACY_BLOCKED=1
                        say "ERROR: $NEW_TIMER is running but $OLD_TIMER could not be turned off."
                        say "  Two Claude refreshers would race on one single-use token. Run, as root:"
                        say "    systemctl disable --now $OLD_TIMER"
                    fi
                fi
                ;;
            off)
                if [ -e "$UNIT_DIR/$OLD_TIMER" ]; then
                    LEGACY_ARMED=1      # from here, any exit restores the old timer
                    if ! systemctl disable --now "$OLD_TIMER" >/dev/null 2>&1; then
                        LEGACY_BLOCKED=1
                        say "WARNING: could not disable $OLD_TIMER. The old refresher stays"
                        say "  the ONLY refresher and the new one is not enabled (two would race on"
                        say "  one single-use token). Fix the timer, then re-run this script."
                    fi
                fi
                wait_seconds="${AI_USAGE_MIGRATION_WAIT_SECONDS:-200}"
                poll_seconds="${AI_USAGE_MIGRATION_POLL_SECONDS:-2}"
                deadline=$((SECONDS + wait_seconds))
                # The service is deliberately never `stop`ped: a `claude -p /usage`
                # killed in the middle of a token rotation can lose the single-use
                # refresh token. The wait is bounded (longer than the unit's own
                # TimeoutStartSec=180).
                while [ "$LEGACY_BLOCKED" = 0 ]; do
                    state="$(systemctl is-active "$LEGACY_REFRESH.service" 2>/dev/null || true)"
                    # Only a CONFIRMED idle state lets the swap proceed. A failed
                    # query or an answer we do not recognise is "unknown": wait,
                    # then DEFER - never a guess that it is idle.
                    case "$state" in
                        inactive|failed|dead) break ;;
                        active|activating|deactivating|reloading) ;;
                        *) state="in an unknown state (${state:-no answer})" ;;
                    esac
                    if [ "$SECONDS" -ge "$deadline" ]; then
                        LEGACY_BLOCKED=1
                        say "WARNING: $LEGACY_REFRESH.service was still $state after ${wait_seconds}s."
                        say "  The migration is DEFERRED: killing it mid-rotation could lose the"
                        say "  single-use Claude refresh token, and enabling the new refresher beside"
                        say "  it would race it. The old timer is put back as the only refresher;"
                        say "  re-run this script when the service has finished."
                        break
                    fi
                    sleep "$poll_seconds"
                done
                ;;
            *)
                # Neither timer's state can be established: change NOTHING.
                LEGACY_BLOCKED=1
                say "ERROR: cannot read the state of $NEW_TIMER, so the Claude refresher"
                say "  migration was not attempted and nothing was changed. Check, as root:"
                say "    systemctl is-enabled $NEW_TIMER $OLD_TIMER ; systemctl is-active $NEW_TIMER $OLD_TIMER"
                say "  and leave exactly ONE of the two enabled."
                ;;
        esac
    fi
    # Deferred: the old timer goes straight back, before anything else can fail.
    if [ "$LEGACY_BLOCKED" != 0 ]; then
        restore_legacy_timer
    fi
fi

ENABLE_FAILED=0
if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    # The TIMER is enabled, never the service: enabling a oneshot service means
    # one run at boot and none after it, which is the classic way a feeder ends
    # up posting a single reading and then going quietly stale forever.
    systemctl enable --now homehub-ai-usage.timer
    if [ "$LEGACY_BLOCKED" = 0 ] && [ "$LEGACY_COMPLETE" = 0 ]; then
        # `enable --now` can change state and then report failure: judge by the
        # state it left, not by its exit status.
        NEW_ENABLE_ATTEMPTED=1
        systemctl enable --now "$NEW_TIMER" >/dev/null 2>&1 || true
        new_after="$(timer_state "$NEW_TIMER")"
        case "$new_after" in
            on) ;;
            off)
                # Nothing was left half-applied: put the old timer back, if there is one.
                LEGACY_BLOCKED=1
                say "WARNING: $NEW_TIMER did not start."
                if [ "$LEGACY_ARMED" = 1 ]; then
                    restore_legacy_timer
                else
                    say "ERROR: Claude account 1 has NO refresher (there is no old timer to fall back on)."
                    say "  Fix the cause, then run, as root:  systemctl enable --now $NEW_TIMER"
                fi ;;
            partial)
                # CONFIRMED half-applied (enabled but not started, or the reverse).
                LEGACY_BLOCKED=1
                say "WARNING: $NEW_TIMER is only half enabled."
                if [ "$LEGACY_ARMED" = 1 ]; then
                    # A confirmed-good fallback exists, so turn the new one fully off
                    # first: restoring the old timer must never leave two enabled.
                    systemctl disable --now "$NEW_TIMER" >/dev/null 2>&1 || true
                    restore_legacy_timer
                else
                    say "ERROR: there is no old timer to fall back on, so it was left as it is."
                    say "  Finish it, as root:  systemctl enable --now $NEW_TIMER"
                fi ;;
            *)
                # UNKNOWN: a query failed. Change nothing at all.
                LEGACY_BLOCKED=1
                say "ERROR: cannot confirm the state of $NEW_TIMER, so NO timer was changed."
                say "  Check, as root:  systemctl is-enabled $NEW_TIMER $OLD_TIMER ;"
                say "                   systemctl is-active  $NEW_TIMER $OLD_TIMER"
                say "  and leave exactly ONE of the two running."
                LEGACY_ARMED=0 ;;
        esac
    fi
fi

# COMMIT the migration the moment the new timer is confirmed on, BEFORE anything
# unrelated can fail: disarm the restore, then remove the old files. A later
# failure (the Gemini timer, say) can then never leave it half-recorded.
if [ "$LEGACY_BLOCKED" = 0 ]; then
    LEGACY_ARMED=0
    if [ "$LEGACY_PRESENT" = 1 ]; then
        rm -f "$UNIT_DIR/$OLD_TIMER" "$UNIT_DIR/$LEGACY_REFRESH.service"
        rm -rf "$UNIT_DIR/$LEGACY_REFRESH.service.d"
        if command -v systemctl >/dev/null 2>&1; then systemctl daemon-reload || true; fi
        say "Migrated the un-templated $LEGACY_REFRESH unit to $LEGACY_REFRESH@claude (same account, same paths)."
    fi
fi

# The remaining timers are unrelated to the migration: a failure here is reported
# and makes the script exit non-zero at the end, but never aborts mid-way.
if command -v systemctl >/dev/null 2>&1; then
    if source_enabled claude2; then
        systemctl enable --now homehub-claude-refresh@claude2.timer || ENABLE_FAILED=1
    fi
    if source_enabled gemini; then
        systemctl enable --now homehub-gemini-refresh.timer || ENABLE_FAILED=1
    fi
    if [ "$ENABLE_FAILED" != 0 ]; then
        say "WARNING: a refresher timer could not be enabled (see above); re-run once fixed."
    fi
fi

say "AI-usage feeder installed; runs as $ACCOUNT on the timer."

# The vendor updaters are switched off system-wide (plan 3.5): claude always (its
# refresher is always installed), gemini only when it is a source.
# /etc is derived from the unit dir (/etc/systemd/system -> /etc), so a run
# against a sandboxed unit dir writes its updater files into the sandbox too.
UPDATER_ARGS=()
source_enabled gemini && UPDATER_ARGS=(--gemini)
AI_CLI_ETC_DIR="${AI_CLI_ETC_DIR:-$(dirname "$(dirname "$UNIT_DIR")")}" \
    bash "${AI_CLI_UPDATER_CONTROLS:-$HERE/../ai-cli/install-updater-controls.sh}" ${UPDATER_ARGS[@]+"${UPDATER_ARGS[@]}"} \
    || say "WARNING: could not write the vendor updater settings; the wrappers still export the controls."

# WHAT IS STILL OWED is a sign-in, and only the ones this box actually lacks:
# ai-cli-status.sh reads the provisioner's status and checks each credential AS
# THE ACCOUNT, then prints the exact recipe for the unverified sources and
# nothing else. (The recipes that used to be printed here unconditionally -
# `claude auth login` for the account, CLAUDE_CONFIG_DIR for the second one,
# CODEX_HOME for the usage Codex home - live there now; `claude setup-token` is
# still the wrong command.) It never fails this script.
if [ "${AI_CLI_SKIP_STATUS:-0}" != 1 ]; then
    AI_CLI_ENV_FILE="$ENV_FILE" bash "${AI_CLI_STATUS:-$HERE/../ai-cli/ai-cli-status.sh}" || true
fi
if [ "$LEGACY_BLOCKED" != 0 ]; then
    say "EXIT 3: everything else is installed, but the Claude refresher migration did"
    say "  not complete (see the WARNING / ERROR above). Exactly ONE of $OLD_TIMER and"
    say "  $NEW_TIMER must be running: check with systemctl is-active, then re-run this script."
    exit 3
fi
if [ "$ENABLE_FAILED" != 0 ]; then
    exit 1
fi
exit 0
