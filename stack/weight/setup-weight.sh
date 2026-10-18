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
# Usage: setup-weight.sh [--check-only | --emit-dropin DIR | --retry]
# Exit 0 = converged COMPLETELY (or disabled). 1 = failed. 2 = refused.
#      4 = NOT YET: everything that can be installed is installed, but the
#          tracker's data this feature needs has not appeared on this box.
#
# "COMPLETE" IS ONE CHECKABLE CONDITION, AND EXIT 0 IS IT: the unit and timer
# are installed and the timer is enabled, AND the drop-in carries the
# BindReadOnlyPaths= line for the ONE definitions file that declares
# WEIGHT_ITEM_CATEGORY (so that file existed when this run resolved it). The
# two ways it can be incomplete both come from a fresh install, where the
# tracker has not run yet and its per-user definitions do not exist: the
# definitions DIRECTORY is absent, or it exists and the category file has not
# synced into it. Both exit 4. Exit 2 is only ever a broken CONFIGURATION (a
# blank knob, an ambiguous category, a forbidden account, a path that exists
# and is not a directory) - something waiting will not fix, so nothing retries it.
#
# --retry is what homehub-weight-setup.service runs every 15 minutes: one more
# full setup, reported in one log line. EVERY full run (firstboot's, a hand run,
# the retry's) holds one lock for its whole life and ends by settling the retry
# timer from its own result - 0 or a refusal/failure disarms it, 4 arms it - so
# two overlapping runs cannot leave "data present, no bind, nothing retrying".
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
# The default is weight_feeder.DEFAULT_GOAL_CATEGORY, and a test asserts the
# two are the same string: a setup script that resolved a DIFFERENT category
# from the one the feeder then looks for would bind the wrong file and say
# nothing at all about it.
WEIGHT_ITEM_CATEGORY="${WEIGHT_ITEM_CATEGORY:-Health}"

# ── WHERE THE ONE FILE IS MOUNTED, AND WHY IT IS INSIDE THE STATE DIRECTORY ──
# THIS MUST STAY EQUAL TO THE UNIT'S StateDirectory=. The unit declares
# `StateDirectory=homehub-weight`, which systemd creates under /var/lib and
# owns by the service account; the credential write guard is bounded by that
# same directory. The account-name knob CANNOT move it, so this is a constant
# rather than a knob, and tests/test_weight_feeder.py asserts this string and
# the unit's StateDirectory= still agree.
WEIGHT_STATE_DIR=/var/lib/homehub-weight
WEIGHT_MOUNT_DIR="$WEIGHT_STATE_DIR/definitions"

if [ -f "$ENV_FILE" ]; then
    # Read only the keys we own, and never `source` a file that may hold
    # secrets into this shell's environment wholesale.
    for key in WEIGHT_ENABLED WEIGHT_USER WEIGHT_USER_ACCOUNT WEIGHT_FEED_URL \
               WEIGHT_DEFINITIONS_DIR WEIGHT_ITEM_CATEGORY; do
        value="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
        [ -n "$value" ] && printf -v "$key" '%s' "$value"
    done
fi

say() { printf '%s\n' "$*"; }
refuse() { say "REFUSED: $*"; exit 2; }

trim() { printf '%s' "$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'; }
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# The quoting and the trailing `# comment` a hand-edited YAML scalar may carry,
# stripped. This is weight_feeder.clean_scalar in shell, and it is deliberately
# the SAME rule: the file this script resolves must be the file the feeder then
# matches, or the bind would expose one file while the feeder looked for
# another.
clean_scalar() {
    local v
    v="$(trim "$1")"
    if [ ${#v} -ge 2 ]; then
        case "$v" in
            "'"*"'") printf '%s' "$(trim "${v:1:${#v}-2}")"; return ;;
            '"'*'"') printf '%s' "$(trim "${v:1:${#v}-2}")"; return ;;
        esac
    fi
    printf '%s' "$(trim "${v%%#*}")"
}

# Every TOP-LEVEL `category:` a definitions file declares, one per line.
#
# TOP-LEVEL MEANS COLUMN ZERO, and that is the whole of the rule. A `category:`
# indented under `items:` is a field of an ITEM, not the file's category, and
# reading it as one is exactly the depth-blind defect the feeder's own reader
# was rewritten to stop making. Only the frontmatter is read - the lines
# between the first two `---` fences - because a `category:` down in the prose
# body is a person thinking out loud, which is the line internal/defs draws too.
file_categories() {
    awk '
        { sub(/\r$/, "") }
        NR == 1 { if ($0 !~ /^---[[:space:]]*$/) exit; infm = 1; next }
        infm && $0 ~ /^---[[:space:]]*$/ { exit }
        infm && $0 ~ /^category[[:space:]]*:/ {
            line = $0
            sub(/^category[[:space:]]*:[[:space:]]*/, "", line)
            print line
        }
    ' "$1"
}

# ── WHICH FILE THE GOAL LIVES IN IS OBSERVED, NEVER DERIVED FROM THE NAME ────
# Sets CATEGORY_FILE to the ONE *.md under $1 whose top-level `category:`
# matches $2, or to the empty string when none does. Refuses on ambiguity.
#
# It reads the frontmatter because THE FILENAME IS NOT OURS TO PREDICT.
# `Health` could be `health.md`, `Health.md`, `health-2.md` or whatever
# NagLight's slug rule produces today - and that rule lives in another repo and
# can change without telling us. Lowercasing the category to get a filename
# would be a guess dressed up as a lookup; this build's standing lesson is to
# observe rather than infer, and the observation costs one pass over a handful
# of small files, once, at provisioning time, as root.
#
# The match is trimmed and case-insensitive on both halves, because that is
# what weight_feeder.find_goal_item does, and `Health` against `health` must
# not be the difference between a goal and a dark panel.
CATEGORY_FILE=""
resolve_category_file() {
    local dir="$1" want path raw declared count
    want="$(lower "$(trim "$2")")"
    CATEGORY_FILE=""
    for path in "$dir"/*.md; do
        [ -f "$path" ] || continue
        count=0
        declared=""
        while IFS= read -r raw; do
            count=$((count + 1))
            declared="$raw"
        done < <(file_categories "$path")
        [ "$count" -eq 0 ] && continue
        if [ "$count" -gt 1 ]; then
            refuse "$path declares a top-level \`category:\` $count times.
    Which category the file IS is not guessable, and the feeder refuses the
    same thing when it reads the file, so it is refused here rather than
    binding a file on a coin toss."
        fi
        [ "$(lower "$(clean_scalar "$declared")")" = "$want" ] || continue
        if [ -n "$CATEGORY_FILE" ]; then
            # REFUSED, NOT PICKED. Two files under one category means the goal
            # could be in either, and binding one of them would silently follow
            # directory order - the same refusal the feeder already makes when
            # two files hold the item.
            refuse "both $CATEGORY_FILE and $path declare category
    \`$WEIGHT_ITEM_CATEGORY\`. Which one carries the goal is not guessable, and
    binding one of them would silently follow directory order. Give the
    category one file, or point WEIGHT_ITEM_CATEGORY at the one you mean."
        fi
        CATEGORY_FILE="$path"
    done
}

# ── THE DROP-IN: ONE FILE MOUNTED, AND THE PATH THE SERVICE SEES ────────────
# Writes both generated pieces into $1 (in production, the unit's drop-in
# directory). Split out so a test can run it against a throwaway definitions
# tree and READ what it wrote, rather than grepping this script and believing a
# string - the V2 lesson, applied to the bind as well as to the account.
#
# ONE FILE, NOT THE DIRECTORY, AND THAT IS THE POINT. The definitions directory
# is one member's subtree of a volume that holds EVERY household member's
# tracker data. A single-user gauge must not hand a service account read access
# to all of it, so what is exposed is the one category file the goal lives in
# and nothing else. (It also happens to be the only thing that WORKS - the
# directory is 0700 hub:hub under root-only ancestors, so binding it at its own
# path leaves the account unable to traverse to it, measured on the hub - but
# the scope is the reason, and it would still be the shape if the modes were
# open.)
emit_dropin() {
    local dir="$1" bind_line target
    install -d -m 0755 "$dir"
    if [ -n "$CATEGORY_FILE" ]; then
        target="$WEIGHT_MOUNT_DIR/$(basename "$CATEGORY_FILE")"
        # The leading `-` is not decoration. WITHOUT it, a source that has gone
        # away fails the unit at 226/NAMESPACE before the feeder ever runs, and
        # the timer repeats that every fifteen minutes with no diagnosis of
        # why. Measured on systemd 255: no `-` plus a missing source => the
        # unit fails to start; with `-` => the mount is skipped, the feeder
        # runs, and it reports its own named "no goal" refusal instead.
        bind_line="BindReadOnlyPaths=-$CATEGORY_FILE:$target"
    else
        bind_line="# NO FILE under the definitions directory declared category
# \`$WEIGHT_ITEM_CATEGORY\` when this drop-in was written, so there is nothing to
# bind and no bind line here. That is deliberately not a failure: a tracker
# that has not synced yet is a normal state at firstboot. The feeder then sees
# an empty definitions directory and takes its existing no-goal path.
# Re-run setup-weight.sh once the category file appears."
    fi
    # The service must see the MOUNT TARGET, not the host path in .env - it
    # cannot traverse to the host path at all.
    #
    # THIS IS AN EnvironmentFile=, NOT AN Environment=, AND THAT WAS MEASURED
    # RATHER THAN ASSUMED. On systemd 255, EnvironmentFile= assignments are
    # applied AFTER every Environment= assignment REGARDLESS OF ORDER: an
    # `Environment=WEIGHT_DEFINITIONS_DIR=...` in this drop-in LOSES to
    # `EnvironmentFile=/opt/homehub/stack/.env` in the unit, even though the
    # drop-in is parsed later, and even when the two sit in one file with the
    # Environment= line second. Two EnvironmentFile= lines, though, are applied
    # in parse order with the last one winning, and drop-ins are parsed after
    # the unit. So the override has to be a FILE, and this is it.
    cat > "$dir/20-definitions.env" <<EOF
# GENERATED by setup-weight.sh. This is NOT a drop-in (systemd reads only
# *.conf in this directory); it is the environment override that
# 10-account.conf points EnvironmentFile= at, and it must be parsed after the
# stack .env, which is why it lives in the drop-in directory.
WEIGHT_DEFINITIONS_DIR=$WEIGHT_MOUNT_DIR
EOF
    chmod 0644 "$dir/20-definitions.env"
    cat > "$dir/10-account.conf" <<EOF
# GENERATED by setup-weight.sh from WEIGHT_USER_ACCOUNT, WEIGHT_DEFINITIONS_DIR
# and WEIGHT_ITEM_CATEGORY. Do not hand-edit: the account certified and the
# account systemd runs must be one value, and so must the definitions path.
[Service]
User=$WEIGHT_USER_ACCOUNT
Group=$WEIGHT_USER_ACCOUNT
$bind_line
EnvironmentFile=$dir/20-definitions.env
EOF
    chmod 0644 "$dir/10-account.conf"
}

RETRY_TIMER="homehub-weight-setup.timer"
retry_timer_off() {
    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now "$RETRY_TIMER" >/dev/null 2>&1 || say "WARNING: could not disable $RETRY_TIMER"
    fi
}

if [ "$WEIGHT_ENABLED" != "true" ] && [ "$MODE" != "--emit-dropin" ]; then
    say "WEIGHT_ENABLED is not true - the weight feeder is not installed."
    if [ "$MODE" = "--retry" ]; then retry_timer_off; fi
    exit 0
fi

# ── --retry: one more full attempt, then report it ──────────────────────────
# The attempt is a child run of this same script with no mode, so the retry
# exercises exactly the code a hand run or firstboot does, not a lookalike. The
# child owns the lock AND the decision about the retry timer (see below); this
# wrapper only reports, so it must not touch the timer itself.
if [ "$MODE" = "--retry" ]; then
    # Quiet by design: the attempt's own chatter (the human-owed list is a dozen
    # lines) is held back, so a healthy wait costs ONE journal line per attempt.
    # A refusal or failure prints everything, because there it is the diagnosis.
    rc=0
    attempt_out="$(bash "${BASH_SOURCE[0]}" 2>&1)" || rc=$?
    case "$rc" in
        0) say "weight setup retry: COMPLETE - retry timer disabled."
           exit 0 ;;
        4) say "weight setup retry: not yet (tracker definitions or the category file are not present) - will try again."
           # The once-a-day persistent-wait warning is the one line worth keeping.
           printf '%s\n' "$attempt_out" | grep -A2 '^WARNING: still waiting' || true
           exit 4 ;;
        *) printf '%s\n' "$attempt_out"
           say "weight setup retry: exit $rc is a refusal or failure, not a wait - retry timer disabled."
           say "  The reason is in the lines above; fix it and re-run firstboot or:"
           say "    sudo bash ${BASH_SOURCE[0]}"
           exit "$rc" ;;
    esac
fi

# ── ONE LOCK FOR THE WHOLE RUN, INCLUDING THE TIMER DECISION ─────────────────
# firstboot (run directly by deploy_stack.py, so After=homehub-firstboot.service
# orders nothing) and the retry timer can run this script at the same time. Each
# run resolves the category file, writes the drop-in, THEN decides whether the
# retry timer stays armed. Interleaved, a run holding an older "no Health file
# yet" answer could overwrite a newer bind, or disarm the timer under a run that
# had just armed it: definitions present, no bind, nothing retrying. So the lock
# covers resolve + write + decide, and the decision is made HERE, by whichever
# run holds the lock last, from that run's own result - never by the caller.
#
# A mkdir lock with a recorded pid rather than flock(1): no dependency, and a
# crashed holder is detected (kill -0) instead of wedging every later run.
LOCK_DIR="${WEIGHT_LOCK_DIR:-/run/homehub-weight-setup.lock}"
WAIT_FILE="${WEIGHT_WAIT_STATE_FILE:-/var/lib/homehub-weight/setup-wait}"
WAIT_WARN_AFTER="${WEIGHT_WAIT_WARN_AFTER:-12}"      # consecutive not-yet runs (12 x 15 min = 3 h)

take_lock() {
    local waited=0 owner
    until mkdir "$LOCK_DIR" 2>/dev/null; do
        owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
        if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then
            rm -rf "$LOCK_DIR"      # its holder is gone
            continue
        fi
        if [ "$waited" -ge "${WEIGHT_LOCK_WAIT:-900}" ]; then
            say "could not take $LOCK_DIR in ${WEIGHT_LOCK_WAIT:-900}s (held by pid ${owner:-?})."
            exit 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    printf '%s' "$$" > "$LOCK_DIR/pid"
}

# Called with the run's exit status, under the lock: complete or broken disarms
# the retry, "not yet" arms it. Also keeps the consecutive-wait count that
# drives the once-a-day warning naming the configured path (a typo'd
# WEIGHT_DEFINITIONS_DIR is otherwise a quiet, endless, bounded-only-by-interval wait).
settle_retry_timer() {
    local rc="$1" count=0 last=0 now
    if [ "$rc" = 4 ]; then
        if command -v systemctl >/dev/null 2>&1; then
            systemctl enable --now "$RETRY_TIMER" >/dev/null 2>&1 || say "WARNING: could not enable $RETRY_TIMER"
        fi
        if [ -f "$WAIT_FILE" ]; then read -r count last < "$WAIT_FILE" || true; fi
        case "$count" in ''|*[!0-9]*) count=0 ;; esac
        case "$last" in ''|*[!0-9]*) last=0 ;; esac
        count=$((count + 1))
        now="$(date +%s)"
        if [ "$count" -ge "$WAIT_WARN_AFTER" ] && [ $((now - last)) -ge 86400 ]; then
            say "WARNING: still waiting after $count consecutive attempts for the tracker's definitions"
            say "  at WEIGHT_DEFINITIONS_DIR=$WEIGHT_DEFINITIONS_DIR (category \`$WEIGHT_ITEM_CATEGORY\`)."
            say "  If the tracker has run and synced, that path is wrong (check WEIGHT_USER / the data root)."
            last="$now"
        fi
        { mkdir -p "$(dirname "$WAIT_FILE")" && printf '%s %s\n' "$count" "$last" > "$WAIT_FILE"; } 2>/dev/null || true
    else
        rm -f "$WAIT_FILE" 2>/dev/null || true
        retry_timer_off
    fi
}

on_exit() {
    local rc=$?
    trap - EXIT
    settle_retry_timer "$rc"
    rm -rf "$LOCK_DIR"
    exit "$rc"
}

if [ -z "$MODE" ]; then
    take_lock
    trap on_exit EXIT
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

# ABSENT is a wait; PRESENT-BUT-NOT-A-DIRECTORY is a broken config. On a fresh
# install the directory appears only after the tracker has run and synced the
# user's definitions (or after a restore), so absence must not stop the install
# of everything else - it is carried to the end as exit 4.
DEFS_ABSENT=0
if [ -e "$WEIGHT_DEFINITIONS_DIR" ] && [ ! -d "$WEIGHT_DEFINITIONS_DIR" ]; then
    refuse "WEIGHT_DEFINITIONS_DIR=$WEIGHT_DEFINITIONS_DIR exists and is not a directory."
fi
[ -d "$WEIGHT_DEFINITIONS_DIR" ] || DEFS_ABSENT=1

case "$WEIGHT_USER_ACCOUNT" in
    hub|root) refuse "WEIGHT_USER_ACCOUNT=$WEIGHT_USER_ACCOUNT. \`hub\` carries
        (ALL) NOPASSWD: ALL and root needs no explanation; the feeder runs as a
        dedicated unprivileged account with nothing else in its home." ;;
esac

# ── RESOLVE THE ONE FILE NOW, AS ROOT, BEFORE ANYTHING IS WRITTEN ───────────
# This script runs as root and CAN read the definitions directory; the service
# account cannot (it is 0700 hub:hub under root-only ancestors). So the "which
# file" question is answered here, once, by reading frontmatter, and the answer
# is baked into the bind. It may legitimately come back empty.
if [ "$DEFS_ABSENT" != 1 ]; then
    resolve_category_file "$WEIGHT_DEFINITIONS_DIR" "$WEIGHT_ITEM_CATEGORY"
fi
if [ "$DEFS_ABSENT" = 1 ]; then
    say "NOT YET: $WEIGHT_DEFINITIONS_DIR does not exist on this box."
    say "  The tracker creates it when it first syncs the user's definitions (or a"
    say "  restore puts it back). Nothing is bound; the feeder is installed anyway"
    say "  and exit 4 tells the retry to run this script again."
elif [ -n "$CATEGORY_FILE" ]; then
    say "category \`$WEIGHT_ITEM_CATEGORY\` is declared by $CATEGORY_FILE; that ONE file is what the service will see."
else
    say "WARNING: no *.md under $WEIGHT_DEFINITIONS_DIR declares category"
    say "  \`$WEIGHT_ITEM_CATEGORY\`. Nothing is bound, so the feeder posts no goal until"
    say "  the file syncs and this script is re-run. This is NOT a failure: an"
    say "  unsynced tracker is a normal state at firstboot."
fi

if [ "$MODE" = "--check-only" ]; then
    if [ "$DEFS_ABSENT" = 1 ]; then
        say "check-only: identity set, destination set, definitions dir NOT YET present."
        exit 4
    fi
    if [ -z "$CATEGORY_FILE" ]; then
        say "check-only: definitions dir present, but no file declares category $WEIGHT_ITEM_CATEGORY yet (NOT YET)."
        exit 4
    fi
    say "check-only: identity set, destination set, definitions dir present."
    exit 0
fi

# `--emit-dropin DIR` writes only the generated drop-in and stops. No root, no
# install: it exists so a test can assert that the bind line and the
# environment override really are derived from the knobs and from the
# frontmatter, rather than grepping this script and believing a string.
if [ "$MODE" = "--emit-dropin" ]; then
    [ -n "${2:-}" ] || refuse "--emit-dropin needs a directory"
    emit_dropin "$2"
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
# The retry units ship with the feeder: whichever run ends "not yet" arms the timer
# (settle_retry_timer), so the units must exist by then. Installed here, under the lock.
install -m 0644 "$HERE/homehub-weight-setup.service" "$UNIT_DIR/homehub-weight-setup.service"
install -m 0644 "$HERE/homehub-weight-setup.timer"   "$UNIT_DIR/homehub-weight-setup.timer"

# ── THE MOUNT TARGET IS ROOT-OWNED, AND STALE STUBS ARE SWEPT ───────────────
# systemd CREATES a missing bind-mount destination and LEAVES IT BEHIND as an
# empty file on disk (measured on systemd 255). So after the category file is
# renamed, yesterday's empty stub would still be sitting in this directory. An
# empty stub is harmless to the feeder - it is not a definitions file, so the
# reader skips it - but it is clutter that outlives its reason, and sweeping it
# is one line.
#
# The directory is root:root 0755, NOT owned by the service account: the
# account must READ what is mounted here and must never be able to drop a file
# of its own beside it and have the feeder read that as the household's goal.
install -d -m 0700 -o "$WEIGHT_USER_ACCOUNT" -g "$WEIGHT_USER_ACCOUNT" "$WEIGHT_STATE_DIR"
install -d -m 0755 -o root -g root "$WEIGHT_MOUNT_DIR"
rm -f "$WEIGHT_MOUNT_DIR"/*.md

# The drop-in is what makes the knobs and the unit the same fact. The bind is
# READ-ONLY and covers ONE FILE: the goal is the tracker's data, this feeder
# must never be able to write a definitions file, and it must not be able to
# read the rest of the household's trackers either.
emit_dropin "$DROPIN_DIR"

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
if [ -z "$CATEGORY_FILE" ]; then
    say "INCOMPLETE (exit 4): no file declaring category $WEIGHT_ITEM_CATEGORY is in"
    say "  $WEIGHT_DEFINITIONS_DIR yet, so the drop-in has no bind line. The retry"
    say "  (homehub-weight-setup.timer) re-runs this script until it appears."
    exit 4
fi
exit 0
