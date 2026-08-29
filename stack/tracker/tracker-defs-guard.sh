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
# resolve the docker volume the way backup.sh does — by asking docker.
#
# AND IT FAILS CLOSED ON AMBIGUITY (adversarial review, 2026-08-29). The first
# cut tried `tracker_data` then `stack_tracker_data` and took the first that
# answered. If BOTH exist — an old volume left behind by a project rename, which
# is exactly the state a reimage or a `docker compose -p` change produces — it
# would assess whichever came first and could report GREEN about a volume no
# container is using, while the live one is empty. Two candidates is not a
# tie-break, it is a question nobody has answered.
resolve_root() {
    if [ -n "${TRACKER_DEFS_ROOT:-}" ]; then printf '%s\n' "$TRACKER_DEFS_ROOT"; return 0; fi
    command -v docker >/dev/null 2>&1 || return 1
    local v mp found="" foundname=""
    for v in "$VOLUME" "stack_$VOLUME"; do
        mp="$(docker volume inspect -f '{{.Mountpoint}}' "$v" 2>/dev/null)" || continue
        [ -n "$mp" ] && [ -d "$mp" ] || continue
        if [ -n "$found" ]; then
            # PRINTED, NOT ASSIGNED. This function is called in a command
            # substitution, so it runs in a SUBSHELL and any variable it sets
            # dies with it — the parent would have seen an empty AMBIGUOUS and
            # taken the "cannot locate" branch instead. Same class of bug as the
            # subshell result log in firstboot; caught here by writing the caller
            # out longhand.
            printf 'AMBIGUOUS:both %s and %s exist
' "$foundname" "$v"
            return 2
        fi
        found="$mp"; foundname="$v"
    done
    [ -n "$found" ] || return 1
    printf '%s\n' "$found"
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

# ids_in_file — the item IDs themselves, one per line, frontmatter only.
# COUNTS ALONE ARE NOT AN INVENTORY (adversarial review, 2026-08-29): one user
# losing a file while another gains one leaves every total identical, and so does
# renaming an item. The guard would have said "matches the baseline exactly"
# about a set that had changed underneath it. The ID list is what makes the
# comparison mean what the message claims.
ids_in_file() {
    awk '
        NR == 1 && $0 ~ /^---[[:space:]]*$/ { infm = 1; next }
        infm && $0 ~ /^---[[:space:]]*$/    { exit }
        infm && $0 ~ /^[[:space:]]*-[[:space:]]+id:[[:space:]]*[^[:space:]]/ {
            sub(/^[[:space:]]*-[[:space:]]+id:[[:space:]]*/, "");
            sub(/[[:space:]].*$/, "");
            print
        }
    ' "$1" 2>/dev/null || true
}

# sanitise — strip anything that would forge a line in the key=value state file.
# A user directory name may legally contain a newline on Linux; copied unescaped
# into `detail=` it would let a directory called `x<LF>band=green` write its own
# verdict. (Adversarial review, 2026-08-29.)
sanitise() { printf '%s' "$1" | tr '\r\n' '  ' | tr -d '\000'; }

ROOT=""; AMBIGUOUS=""
# resolve_root runs in a command substitution, i.e. a SUBSHELL, so it cannot
# hand back a variable. It returns 0 with the path, 1 for "not found", or 2 with
# an AMBIGUOUS: line on stdout.
_root_out="$(resolve_root)"; _rr=$?
case "$_rr" in
    0) ROOT="$_root_out" ;;
    2) AMBIGUOUS="${_root_out#AMBIGUOUS:}" ;;
    *) ROOT="" ;;
esac
FILES=0; ITEMS=0; NEWEST=0; USERS=0; DETAIL=""; INV=""
band=""; verdict=""

if [ -n "$AMBIGUOUS" ]; then
    band=red
    verdict="the tracker data volume is AMBIGUOUS: $AMBIGUOUS. Refusing to guess which one the container uses — assessing the wrong volume could report green about definitions nothing reads."
elif [ -z "$ROOT" ]; then
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
        uname_="$(sanitise "$(basename "$udir")")"
        ucount=0; uitems=0
        if [ -d "$ddir" ]; then
            for f in "$ddir"/*.md; do
                [ -f "$f" ] || continue
                ucount=$(( ucount + 1 ))
                fitems="$(count_items_in_file "$f")"
                uitems=$(( uitems + fitems ))
                INV="$INV$uname_/$(sanitise "$(basename "$f")"):$fitems
$(ids_in_file "$f" | sed "s|^|$uname_/|")
"
                m="$(stat -c '%Y' -- "$f" 2>/dev/null || echo 0)"
                [ "$m" -gt "$NEWEST" ] && NEWEST="$m"
            done
        fi
        FILES=$(( FILES + ucount ))
        ITEMS=$(( ITEMS + uitems ))
        DETAIL="${DETAIL:+$DETAIL; }${uname_}=${ucount}f/${uitems}i"
    done
fi

# The canonical inventory: every user/file with its item count, and every item
# ID, sorted. One hash of that is what the baseline compares against, so a swap
# that leaves the totals identical still moves it.
INV_HASH="$(printf '%s' "$INV" | grep -v '^$' | sort | sha256sum 2>/dev/null | cut -d' ' -f1)"
[ -n "$INV_HASH" ] || INV_HASH="unavailable"

NEWEST_HUMAN="never"
[ "$NEWEST" -gt 0 ] && NEWEST_HUMAN="$(date -u -d "@$NEWEST" +%FT%TZ 2>/dev/null || echo "$NEWEST")"
AGE_DAYS=-1
if [ "$NEWEST" -gt 0 ]; then AGE_DAYS=$(( ( $(date -u +%s) - NEWEST ) / 86400 )); fi

# ── baseline mode ────────────────────────────────────────────────────────────
write_baseline() {
    local tmp
    mkdir -p "$STATE_DIR" || { warn "cannot create $STATE_DIR"; return 1; }
    # mktemp, not a fixed .tmp name: the timer and a hand run can overlap, and
    # two writers racing on one temporary file produce a mixed one.
    tmp="$(mktemp "$BASELINE_FILE.XXXXXX")" || { warn "cannot create a temporary file beside $BASELINE_FILE"; return 1; }
    {
        echo "# Recorded by $TAG --baseline. The inventory a healthy tracker has."
        echo "# REFRESH THIS DELIBERATELY, never automatically: a guard that"
        echo "# re-baselined whenever it saw a change would launder a deletion."
        echo "baseline_utc=$(date -u +%FT%TZ)"
        echo "users=$USERS"
        echo "files=$FILES"
        echo "items=$ITEMS"
        echo "inv_hash=$INV_HASH"
        echo "detail=$DETAIL"
    } >"$tmp" && mv "$tmp" "$BASELINE_FILE" && return 0
    rm -f "$tmp" 2>/dev/null
    warn "could not write $BASELINE_FILE"
    return 1
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
# EVERY BASELINE FIELD IS VALIDATED BEFORE IT IS COMPARED. Without this, a
# baseline whose `items=` is missing, empty or non-numeric makes `[ "$ITEMS" -lt
# "$B_ITEMS" ]` error out — and with no `set -e` the script simply carries on to
# the final `else`, which is GREEN. A corrupt baseline is exactly when this thing
# must not say everything is fine. (Adversarial review, 2026-08-29.)
B_FILES=""; B_ITEMS=""; B_USERS=""; B_WHEN=""; B_HASH=""; B_BAD=""
if [ -f "$BASELINE_FILE" ]; then
    B_FILES="$(awk -F= '$1=="files"{print $2}' "$BASELINE_FILE" | head -1)"
    B_ITEMS="$(awk -F= '$1=="items"{print $2}' "$BASELINE_FILE" | head -1)"
    B_USERS="$(awk -F= '$1=="users"{print $2}' "$BASELINE_FILE" | head -1)"
    B_HASH="$(awk -F= '$1=="inv_hash"{print $2}' "$BASELINE_FILE" | head -1)"
    B_WHEN="$(awk -F= '$1=="baseline_utc"{print $2}' "$BASELINE_FILE" | head -1)"
    for _f in B_FILES B_ITEMS B_USERS; do
        eval "_v=\${$_f}"
        case "${_v:-}" in
            ''|*[!0-9]*) B_BAD="${B_BAD:+$B_BAD }$_f='${_v}'" ;;
        esac
    done
fi

if [ -z "$band" ]; then
    if [ "$FILES" -eq 0 ]; then
        band=red
        verdict="NO definition files at all under $ROOT ($USERS user dir(s)). A tracker with no items renders GREEN with score 0 — indistinguishable on a wall from a tracker where everything is fine."
    elif [ -n "$B_BAD" ]; then
        band=yellow
        verdict="the baseline at $BASELINE_FILE is unreadable or corrupt ($B_BAD) — refusing to compare against it. Current inventory: $FILES file(s), $ITEMS item(s), $USERS user(s). Re-record it with '$TAG --baseline' once you have checked the set is right."
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
    elif [ -n "$B_HASH" ] && [ "$B_HASH" != "unavailable" ] && [ "$INV_HASH" != "$B_HASH" ]; then
        # THE TOTALS AGREE AND THE SET DOES NOT. One user losing a file while
        # another gains one, or an item renamed: every count is identical and the
        # inventory is different. This is the branch that stops "matches the
        # baseline exactly" from being a sentence about arithmetic rather than
        # about definitions.
        band=yellow
        verdict="the counts match the baseline ($FILES file(s)/$ITEMS item(s)/$USERS user(s)) but the INVENTORY does not: a file or an item id was renamed, moved between users, or swapped. [$DETAIL]. Check the set, then re-run '$TAG --baseline'."
    else
        band=green
        verdict="$FILES definition file(s), $ITEMS item(s), $USERS user(s) — matches the baseline recorded $B_WHEN, by count AND by inventory. Newest change $NEWEST_HUMAN (${AGE_DAYS}d ago). [$DETAIL]"
    fi
fi

# ── the state file: the half that does not need the tracker to be alive ──────
# verify-hub.sh reads THIS, not /api/today, and that is deliberate. The state
# this guard is most needed for — an empty or missing definition set — is
# exactly the state in which the tracker's own answers stop being informative.
#
# A FAILED WRITE IS A RED, NOT A WARNING. The state file IS the durable verdict;
# if it could not be written, the last thing on disk is a stale answer and the
# exit code would be the only signal. Systemd throws that away. So a write
# failure escalates the band. (Adversarial review, 2026-08-29.)
write_state() {
    local tmp
    mkdir -p "$STATE_DIR" 2>/dev/null || return 1
    tmp="$(mktemp "$STATE_FILE.XXXXXX" 2>/dev/null)" || return 1
    {
        echo "checked_utc=$(date -u +%FT%TZ)"
        echo "band=$band"
        echo "users=$USERS"
        echo "files=$FILES"
        echo "items=$ITEMS"
        echo "inv_hash=$INV_HASH"
        echo "newest_epoch=$NEWEST"
        echo "newest_utc=$NEWEST_HUMAN"
        echo "age_days=$AGE_DAYS"
        echo "baseline_files=${B_FILES:-none}"
        echo "baseline_items=${B_ITEMS:-none}"
        echo "detail=$(sanitise "$DETAIL")"
        echo "verdict=$(sanitise "$verdict")"
        # AN `if`, NOT `[ ] && echo`. As the LAST command of the group this is
        # the group's exit status, so with no escalation the test returns 1, the
        # `&& mv` never runs, and write_state reports failure on every ordinary
        # run — which then escalates the band to red. Measured: 24 assertions
        # failed at once and the state file was never written.
        if [ -n "${ESCALATION:-}" ]; then echo "feed_escalation=$(sanitise "$ESCALATION")"; fi
    } >"$tmp" && mv "$tmp" "$STATE_FILE" && return 0
    rm -f "$tmp" 2>/dev/null
    return 1
}

if ! write_state; then
    warn "could not write $STATE_FILE — the check ran, but nothing durable records it."
    warn "  Escalating to RED: a verdict nobody can read back is not a verdict, and the"
    warn "  last thing on disk is now a STALE answer that reads as current."
    band=red
    verdict="could not write $STATE_FILE (was: $verdict)"
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
                    ESCALATION="the tracker has NO item declaring check id '$CHECK_ID' — the definition carrying this very lane is gone. This is the deletion alarm, not a feed fault. Inventory read from the volume: $FILES file(s), $ITEMS item(s)."
                    verdict="$ESCALATION"
                    log "RED (escalated): $verdict"
                    # REWRITTEN WHOLE, THROUGH THE SAME CHECKED WRITER. The first
                    # cut patched the existing file with `sed -i` and appended a
                    # line, both `|| true` — so a failed patch left band=green on
                    # disk while the process exited 1, and the verifier that reads
                    # the state file (which is the durable half of this design)
                    # would have gone on reporting healthy.
                    if ! write_state; then
                        warn "could not rewrite $STATE_FILE with the escalated RED verdict — the file on disk is now STALE and will read as whatever it last said"
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
