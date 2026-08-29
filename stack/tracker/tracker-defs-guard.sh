#!/usr/bin/env bash
# tracker-defs-guard.sh — the one lane the tracker cannot report about itself.
#
# THE FAILURE THIS EXISTS FOR, measured on this box on 2026-08-29:
#
#     /api/today -> {"items":null}          the tracker had NO definitions
#     the panel  -> colour green, score 0   because there was nothing to be late for
#
# A tracker with zero items renders GREEN. On a wall that is indistinguishable
# from a tracker where everything is fine, and it stayed that way for a week
# while two drive lanes posted into the void. Nothing in this project would have
# noticed if the definitions were deleted tomorrow.
#
# THE CIRCULARITY IS THE WHOLE DESIGN PROBLEM, so it is stated rather than
# designed around. The tracker's alerting mechanism IS its item definitions. An
# item that says "the definitions are intact" is deleted by the same `rm` that
# deletes everything else. So this guard:
#
#   * reads the definition files DIRECTLY off the docker volume, not through the
#     tracker's API — so it still works with the container stopped, which is one
#     of the states it must be able to report;
#   * writes its verdict to a STATE FILE that verify-hub.sh asserts on with no
#     tracker involved at all — that is the path that survives a wipe;
#   * posts to /api/feed as a convenience, and treats HTTP 400 naming its own
#     check id as RED rather than as a transport fault. "The tracker has never
#     heard of tracker-definitions" is precisely the alarm, not a failure to
#     raise one.
#
# WHAT IT COMPARES, and why three numbers rather than one:
#   files    a whole definition FILE vanishing is the coarse, common accident
#            (a bad sync, a git checkout, a docker volume recreated)
#   items    a file surviving with its items deleted is the quiet one
#   newest   "when did this last change" — the Owner's own framing. A definition
#            set that has not moved in months is not a fault, but it is the
#            first thing you want to know when a lane goes quiet.
#
# BANDS
#   green   the inventory matches the baseline exactly
#   yellow  it GREW (definitions were added — refresh the baseline), or there is
#           no baseline yet to compare against
#   red     it SHRANK, the definitions directory is missing/empty, or the volume
#           cannot be found at all
#
# Usage: tracker-defs-guard.sh [--check|--report|--baseline] [--quiet]
#   --check     read-only: assess and print, write the state file, POST nothing
#   --report    --check plus the /api/feed post (what the timer runs)
#   --baseline  record the CURRENT inventory as the baseline and exit. This is
#               deliberately a separate, explicit mode: a guard that refreshed
#               its own baseline whenever it noticed a change could never report
#               a deletion twice, and would launder the very event it exists for.
#
# Exit: 0 = green, 1 = red, 2 = yellow, 3 = usage/internal. The unit accepts
# 0/1/2 as success because THE REPORT is the signal, not the exit code — the
# same contract homehub-library-health.service uses.
set -uo pipefail

MODE=check
QUIET=0
while [ $# -gt 0 ]; do
    case "$1" in
        --check)    MODE=check;    shift ;;
        --report)   MODE=report;   shift ;;
        --baseline) MODE=baseline; shift ;;
        --quiet)    QUIET=1;       shift ;;
        -h|--help)  sed -n '2,60p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 3 ;;
    esac
done

TAG=tracker-defs-guard
log()  { [ "$QUIET" = 1 ] && return 0; printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
warn() { printf '%s WARN: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

STATE_DIR="${TRACKER_DEFS_STATE_DIR:-/var/lib/homehub}"
STATE_FILE="$STATE_DIR/tracker-defs.state"
BASELINE_FILE="$STATE_DIR/tracker-defs-baseline"
ENV_FILE="${TRACKER_DEFS_ENV_FILE:-/etc/homehub-backup/backup.env}"
VOLUME="${TRACKER_DEFS_VOLUME:-tracker_data}"
CHECK_ID="${TRACKER_DEFS_FEED_CHECK:-tracker-definitions}"

# ── where the definitions physically are ─────────────────────────────────────
# TRACKER_DEFS_ROOT overrides everything (the test harness uses it). Otherwise
# resolve the docker volume the way backup.sh does — by asking docker, with the
# compose project prefix tried second, because `volumes: tracker_data:` in
# docker-compose.yml becomes the real volume `stack_tracker_data`.
resolve_root() {
    if [ -n "${TRACKER_DEFS_ROOT:-}" ]; then printf '%s\n' "$TRACKER_DEFS_ROOT"; return 0; fi
    command -v docker >/dev/null 2>&1 || return 1
    local v mp
    for v in "$VOLUME" "stack_$VOLUME"; do
        mp="$(docker volume inspect -f '{{.Mountpoint}}' "$v" 2>/dev/null)" || continue
        [ -n "$mp" ] && [ -d "$mp" ] && { printf '%s\n' "$mp"; return 0; }
    done
    return 1
}

# ── the inventory ────────────────────────────────────────────────────────────
# ONE LINE PER USER DIRECTORY, because the tracker is multi-user by design and a
# per-user count is the only one that can notice "this household member's
# definitions went away". Items are counted by the frontmatter's `- id:` rows,
# which is what defs.Load parses; the body after the second `---` is free notes
# and is deliberately not counted.
#
# THE FRONTMATTER BOUNDARY MATTERS. Counting `- id:` across the whole file would
# also count any example inside the prose, and every one of these files ends in
# prose. awk stops at the second '---'.
count_items_in_file() {
    awk '
        NR == 1 && $0 ~ /^---[[:space:]]*$/ { infm = 1; next }
        infm && $0 ~ /^---[[:space:]]*$/    { exit }
        infm && $0 ~ /^[[:space:]]*-[[:space:]]+id:[[:space:]]*[^[:space:]]/ { n++ }
        END { print n + 0 }
    ' "$1" 2>/dev/null || echo 0
}

ROOT="$(resolve_root || true)"
FILES=0; ITEMS=0; NEWEST=0; USERS=0; DETAIL=""
band=""; verdict=""

if [ -z "$ROOT" ]; then
    band=red
    verdict="cannot locate the tracker data volume ('$VOLUME' / 'stack_$VOLUME') — docker is absent or the volume does not exist. The definitions cannot be assessed at all, which is not the same as them being fine."
elif [ ! -d "$ROOT" ]; then
    band=red
    verdict="tracker data root $ROOT does not exist"
else
    for udir in "$ROOT"/*/; do
        [ -d "$udir" ] || continue
        # A user directory with no definitions/ is a real state (a freshly
        # provisioned member), and it is counted so the total can notice it
        # LOSING one later.
        USERS=$(( USERS + 1 ))
        ddir="$udir/definitions"
        ucount=0; uitems=0
        if [ -d "$ddir" ]; then
            for f in "$ddir"/*.md; do
                [ -f "$f" ] || continue
                ucount=$(( ucount + 1 ))
                uitems=$(( uitems + $(count_items_in_file "$f") ))
                m="$(stat -c '%Y' -- "$f" 2>/dev/null || echo 0)"
                [ "$m" -gt "$NEWEST" ] && NEWEST="$m"
            done
        fi
        FILES=$(( FILES + ucount ))
        ITEMS=$(( ITEMS + uitems ))
        DETAIL="${DETAIL:+$DETAIL; }$(basename "$udir")=${ucount}f/${uitems}i"
    done
fi

NEWEST_HUMAN="never"
[ "$NEWEST" -gt 0 ] && NEWEST_HUMAN="$(date -u -d "@$NEWEST" +%FT%TZ 2>/dev/null || echo "$NEWEST")"
AGE_DAYS=-1
if [ "$NEWEST" -gt 0 ]; then AGE_DAYS=$(( ( $(date -u +%s) - NEWEST ) / 86400 )); fi

# ── baseline mode ────────────────────────────────────────────────────────────
write_baseline() {
    mkdir -p "$STATE_DIR" || { warn "cannot create $STATE_DIR"; return 1; }
    {
        echo "# Recorded by $TAG --baseline. The inventory a healthy tracker has."
        echo "# REFRESH THIS DELIBERATELY, never automatically: a guard that"
        echo "# re-baselined whenever it saw a change would launder a deletion."
        echo "baseline_utc=$(date -u +%FT%TZ)"
        echo "users=$USERS"
        echo "files=$FILES"
        echo "items=$ITEMS"
        echo "detail=$DETAIL"
    } >"$BASELINE_FILE.tmp" && mv "$BASELINE_FILE.tmp" "$BASELINE_FILE"
}

if [ "$MODE" = baseline ]; then
    if [ "$band" = red ]; then
        warn "refusing to baseline: $verdict"
        warn "  A baseline taken from a broken read would record 0 files as normal, and this"
        warn "  guard would then be permanently green about an empty tracker."
        exit 1
    fi
    if [ "$FILES" -eq 0 ]; then
        warn "refusing to baseline an EMPTY definition set (0 files across $USERS user dir(s))."
        warn "  That is the exact state this guard exists to report; recording it as the"
        warn "  baseline would make the alarm impossible to ring."
        exit 1
    fi
    write_baseline || exit 1
    log "baseline recorded: $USERS user(s), $FILES file(s), $ITEMS item(s) [$DETAIL]"
    exit 0
fi

# ── compare ──────────────────────────────────────────────────────────────────
B_FILES=""; B_ITEMS=""; B_USERS=""; B_WHEN=""
if [ -f "$BASELINE_FILE" ]; then
    B_FILES="$(awk -F= '$1=="files"{print $2}' "$BASELINE_FILE")"
    B_ITEMS="$(awk -F= '$1=="items"{print $2}' "$BASELINE_FILE")"
    B_USERS="$(awk -F= '$1=="users"{print $2}' "$BASELINE_FILE")"
    B_WHEN="$(awk -F= '$1=="baseline_utc"{print $2}' "$BASELINE_FILE")"
fi

if [ -z "$band" ]; then
    if [ "$FILES" -eq 0 ]; then
        band=red
        verdict="NO definition files at all under $ROOT ($USERS user dir(s)). A tracker with no items renders GREEN with score 0 — indistinguishable on a wall from a tracker where everything is fine."
    elif [ -z "$B_FILES" ]; then
        band=yellow
        verdict="no baseline recorded yet — $FILES file(s), $ITEMS item(s) across $USERS user(s), newest $NEWEST_HUMAN (${AGE_DAYS}d ago). Run '$TAG --baseline' once the set is right, and this lane turns green."
    elif [ "$FILES" -lt "$B_FILES" ] || [ "$ITEMS" -lt "$B_ITEMS" ]; then
        band=red
        verdict="definitions SHRANK: $FILES file(s)/$ITEMS item(s) now, baseline had $B_FILES/$B_ITEMS (recorded $B_WHEN). Newest change $NEWEST_HUMAN (${AGE_DAYS}d ago). [$DETAIL]"
    elif [ "$FILES" -gt "$B_FILES" ] || [ "$ITEMS" -gt "$B_ITEMS" ]; then
        band=yellow
        verdict="definitions GREW: $FILES file(s)/$ITEMS item(s) now, baseline had $B_FILES/$B_ITEMS. That is normal after adding items — re-run '$TAG --baseline' to accept it. Newest change $NEWEST_HUMAN (${AGE_DAYS}d ago)."
    elif [ -n "$B_USERS" ] && [ "$USERS" -ne "$B_USERS" ]; then
        band=yellow
        verdict="the FILE and ITEM counts match the baseline but the number of user directories changed ($USERS now, $B_USERS at baseline) — someone was added or removed. [$DETAIL]"
    else
        band=green
        verdict="$FILES definition file(s), $ITEMS item(s), $USERS user(s) — matches the baseline recorded $B_WHEN. Newest change $NEWEST_HUMAN (${AGE_DAYS}d ago). [$DETAIL]"
    fi
fi

# ── the state file: the half that does not need the tracker to be alive ──────
# verify-hub.sh reads THIS, not /api/today, and that is deliberate. The state
# this guard is most needed for — an empty or missing definition set — is
# exactly the state in which the tracker's own answers stop being informative.
if mkdir -p "$STATE_DIR" 2>/dev/null; then
    {
        echo "checked_utc=$(date -u +%FT%TZ)"
        echo "band=$band"
        echo "users=$USERS"
        echo "files=$FILES"
        echo "items=$ITEMS"
        echo "newest_epoch=$NEWEST"
        echo "newest_utc=$NEWEST_HUMAN"
        echo "age_days=$AGE_DAYS"
        echo "baseline_files=${B_FILES:-none}"
        echo "baseline_items=${B_ITEMS:-none}"
        echo "detail=$DETAIL"
        echo "verdict=$verdict"
    } >"$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
else
    warn "cannot write $STATE_FILE — the check ran, but nothing durable records it"
fi

case "$band" in
    green)  log "healthy: $verdict" ;;
    yellow) log "YELLOW: $verdict" ;;
    red)    log "RED: $verdict" ;;
esac

# ── report ───────────────────────────────────────────────────────────────────
if [ "$MODE" = report ]; then
    # Same transport as library-guard.sh: the tracker is bridge-only, so the
    # post goes through `docker exec`. Config comes from backup.env because that
    # is where every other feeder on this box reads NAGLIGHT_* from, and a
    # second mechanism would be a second thing to keep in step.
    if [ -f "$ENV_FILE" ]; then
        while IFS= read -r __line || [ -n "$__line" ]; do
            case "$__line" in ''|'#'*) continue ;; esac
            case "$__line" in NAGLIGHT_*=*) ;; *) continue ;; esac
            __k=${__line%%=*}; __v=${__line#*=}
            case "$__v" in \"*\") __v=${__v#\"}; __v=${__v%\"} ;; \'*\') __v=${__v#\'}; __v=${__v%\'} ;; esac
            printf -v "$__k" '%s' "$__v" 2>/dev/null && export "$__k"
        done <"$ENV_FILE"
    fi
    if [ -z "${NAGLIGHT_FEED_URL:-}" ]; then
        log "NAGLIGHT_FEED_URL unset — journal + state file only (check id would be '$CHECK_ID')"
    elif [ -z "${NAGLIGHT_FEED_CONTAINER:-}" ]; then
        log "NAGLIGHT_FEED_CONTAINER unset — the tracker is bridge-only, so there is no host-reachable URL. Journal + state file only."
    elif ! command -v docker >/dev/null 2>&1; then
        log "feed: NOT SENT — no docker binary on PATH"
    elif ! docker inspect -f '{{.State.Running}}' "$NAGLIGHT_FEED_CONTAINER" 2>/dev/null | grep -q true; then
        # A STOPPED TRACKER DOES NOT DOWNGRADE THE VERDICT. The definitions were
        # still read off the volume; the state file already holds the answer.
        log "feed: NOT SENT — container '$NAGLIGHT_FEED_CONTAINER' is not running. The verdict above stands: it was read from the volume, not from the tracker."
    else
        note="${verdict//\"/\'}"
        body="$(printf '{"check":"%s","color":"%s","reason":"%s"}' "$CHECK_ID" "$band" "$note")"
        hdr=(--header "Content-Type: application/json")
        [ -n "${NAGLIGHT_TOKEN:-}" ] && hdr+=(--header "Authorization: Bearer ${NAGLIGHT_TOKEN}")
        [ -n "${NAGLIGHT_USER:-}" ]  && hdr+=(--header "X-Forwarded-User: ${NAGLIGHT_USER}")
        ferr="$(docker exec "$NAGLIGHT_FEED_CONTAINER" wget -q -S -O - --content-on-error "${hdr[@]}" \
                  --post-data "$body" "$NAGLIGHT_FEED_URL" 2>&1)"
        frc=$?
        if [ "$frc" -eq 0 ]; then
            log "feed: reported $band"
        else
            fcode="$(printf '%s' "$ferr" | grep -oE 'HTTP/[0-9.]+ [0-9]{3}' | grep -oE '[0-9]{3}$' | head -1)"
            fbody="$(printf '%s' "$ferr" | grep -vE '^[[:space:]]*(HTTP/|Content-|X-Content-|Date:|Connection:|Vary:|Transfer-)' | tr -s ' \n' ' ' | sed 's/^ *//;s/ *$//')"
            log "feed: report FAILED (wget exit $frc${fcode:+, HTTP $fcode}) - ${fbody:-no output}"
            # THE 400 THAT IS NOT A TRANSPORT FAULT, AND IS THE ALARM ITSELF.
            # "unknown feeder check id: tracker-definitions" means no item
            # declares this check — i.e. the definition that would have carried
            # this lane is among the ones that were deleted. Reporting that as a
            # feed problem would be the single most misleading thing this script
            # could do, so it is escalated to RED here instead.
            case "$fbody" in
                *"unknown feeder check id: $CHECK_ID"*)
                    band=red
                    verdict="the tracker has NO item declaring check id '$CHECK_ID' — the definition carrying this very lane is gone. This is the deletion alarm, not a feed fault. Inventory read from the volume: $FILES file(s), $ITEMS item(s)."
                    log "RED (escalated): $verdict"
                    if mkdir -p "$STATE_DIR" 2>/dev/null; then
                        sed -i "s/^band=.*/band=red/" "$STATE_FILE" 2>/dev/null || true
                        printf 'feed_escalation=%s\n' "$verdict" >>"$STATE_FILE" 2>/dev/null || true
                    fi ;;
            esac
        fi
    fi
fi

case "$band" in
    green)  exit 0 ;;
    red)    exit 1 ;;
    yellow) exit 2 ;;
    *)      exit 3 ;;
esac
