#!/usr/bin/env bash
# frame-freshness.sh — is the picture-frame video set current with the media
# that feeds it? Feeds the tracker's `frame-freshness` lane.
#
# THE SPEC IS VIDEO_FRAME_GEN_CHECK_PLAN.md (the Owner, 2026-09-07, fully ruled
# the same day). This file implements §3's three conditions and §7's five
# rulings; read that document before changing any threshold here.
#
# WHAT IT ASSERTS, AND WHAT IT DELIBERATELY DOES NOT. It answers "are the frame
# videos current with the media feeding them", NOT "did a generator run". There
# is no generator to observe from this box; freshness of the RESULT is the
# whole of the contract (plan §8).
#
# ── GREEN | YELLOW, AND NOTHING ELSE ─────────────────────────────────────────
# The Owner ruled this lane may report YELLOW AT WORST. That is not a band this
# script picks — it is a discipline it has to keep, and the reason lives in
# NagLight rather than here:
#
#   an `automated` item whose day carries NO report falls through to the
#   staleness scan, and missScore saturates at 3+ misses = 100 = RED.
#
# SO SILENCE IS RED, ON A THREE-DAY FUSE. A feeder that reports when it can see
# and stays quiet when it cannot has not implemented "never red" — it has
# implemented "red, three days later". Therefore:
#
#   *** THIS SCRIPT MUST POST EVERY SINGLE RUN, WITHOUT EXCEPTION. ***
#
# Every failure path below — no python3, no manifest, unreadable state, mount
# refused, mini-serv asleep — ends in YELLOW WITH A REASON, never in silence and
# never in a non-report exit. "I could not tell" is a yellow state with a
# message. Since mini-serv is allowed to sleep (below), it is also the COMMON
# case, not an exceptional one.
#
# AND WHEN THE POST ITSELF FAILS, THE UNIT FAILS (exit 1). Reviewed 2026-09-07:
# the first cut logged an unsent report and still exited "successfully", which
# is the one hole that defeats the whole design — an unreachable tracker, a
# stopped container or a 400 would have produced silence, and silence is red on
# a three-day fuse. The lane cannot report its own feed being down, so the
# ESCALATION PATH IS systemd: a failed unit is visible in `systemctl --failed`
# and in the journal. See report_failed below.
#
# ── FALSE GREEN IS THE FAILURE MODE THIS FILE FEARS ──────────────────────────
# Yellow-when-unsure is cheap; GREEN-when-unsure is the bug that makes the lane
# worthless. Two of these were found in review on 2026-09-07 and both are now
# refused explicitly rather than by luck:
#   * an unparseable/absent source timestamp must NEVER become epoch 0, which
#     would make any real video look newer and the drift negative (= green);
#   * a corrupt or half-written probe cache must NEVER be treated as fresh.
# Every numeric that reaches a comparison is validated as a number first, and
# anything that is not is yellow with the reason.
#
# ── IT MUST NEVER WAKE MINI-SERV ─────────────────────────────────────────────
# The frame flow's failure policy (storage-map §4d) is that an unreachable frame
# share is SKIPPED, not alerted, because that box is allowed to be asleep. The
# nightly backup is allowed to wake it — BACKUP_WAKE_* exists for exactly that —
# and this script must not reuse any of that. Reachability is a plain TCP
# connect to 445 with a short timeout; a refused connect is a skip that leaves
# the previous date-stamped answer standing. No magic packet is ever sent.
#
# That is why the probe answer is CACHED WITH ITS OWN TIMESTAMP and allowed to
# be up to FRAME_PROBE_MAX_AGE_DAYS old: demanding a live answer every run would
# make this lane yellow most of the time for a reason that is not a fault.
#
# ── IT MUST NEVER WAKE THE LIBRARY DRIVE EITHER ──────────────────────────────
# The "when did the media last change" half does NOT walk /srv/library. It reads
# the manifest FileBackup already writes to the SYSTEM disk on every nightly
# library run. That is one pass over a local CSV, so it cannot spin up the
# parked 4 TB platter — the same contract library-guard.sh and the backup
# drive-health unit both keep (WI-10.10).
#
# ── CREDENTIALS: REUSED, NOT ADDED ───────────────────────────────────────────
# The mount uses the CIFS credentials the backup already has
# (/etc/homehub-backup/cifs.creds, username `share`). That is the SAME MINI-SERV
# `share` account the panel's frame flow uses, and it already grants read to
# this share — verified 2026-09-07. NO NEW SECRET IS INTRODUCED BY THIS FILE and
# none should be: if this ever needs a credential the backup does not have, that
# is a decision for the Owner, not a file to quietly add.
#
# Exit: 0 = green, 2 = yellow, 1 = the verdict could not be REPORTED. Never a
# red band — this lane has none.
set -uo pipefail

TAG=frame-freshness
CHECK_ID="${FRAME_FEED_CHECK:-frame-freshness}"

STATE_DIR="${FRAME_STATE_DIR:-/var/lib/homehub}"
STATE_FILE="$STATE_DIR/frame-freshness.state"
PROBE_FILE="$STATE_DIR/frame-probe.state"

ENV_FILE="${FRAME_ENV_FILE:-/etc/homehub-backup/backup.env}"
FRAME_CONF="${FRAME_CONF_FILE:-/etc/homehub-frame-freshness.env}"

MODE=report
[ "${1:-}" = "--check" ] && MODE=check

# ── defaults (overridable in $FRAME_CONF) ────────────────────────────────────
FRAME_UNC="//mini-serv.diyt.win/PictureFrameVideos"
FRAME_CIFS_CREDENTIALS=/etc/homehub-backup/cifs.creds
FRAME_CIFS_EXTRA=vers=3.0
FRAME_PROBE_HOST=mini-serv.diyt.win
FRAME_PROBE_PORT=445
FRAME_PROBE_TIMEOUT=5
FRAME_MOUNT_TIMEOUT=25
FRAME_WALK_TIMEOUT=60
# Both windows are 7 days, and the Owner's ruling includes "this does not need
# to be very precise" — so these are whole days and every comparison below is
# generous. Nothing here does timezone-exact arithmetic on purpose.
FRAME_PROBE_MAX_AGE_DAYS=7
FRAME_DRIFT_MAX_DAYS=7
MANIFEST="/var/lib/homehub-filebackup/state/MANIFEST.csv"
# Scope: the WHOLE of Shared, every subtree (plan §7.2). The extension
# allow-list is the only filter, which is why it is an allow-list and not a
# deny-list — see the note in the plan §7.1.
SHARED_PREFIX="Shared/"

# A STRICT KEY, THEN THE VALUE. `printf -v "$k"` evaluates its argument as a
# bash variable EXPRESSION, so a key like `FRAME_X[$(cmd)]` would run `cmd`
# while resolving the subscript (found in review, 2026-09-07). The file is
# root-owned, so this was never privilege escalation — but "a literal key=value
# loader" is what this claims to be, and a loader that can execute is not that.
# The name is matched whole against a plain identifier before it is ever used.
if [ -f "$FRAME_CONF" ]; then
    while IFS= read -r __line || [ -n "$__line" ]; do
        case "$__line" in ''|'#'*) continue ;; esac
        __k=${__line%%=*}; __v=${__line#*=}
        case "$__k" in
            FRAME_[A-Z_]*|MANIFEST|SHARED_PREFIX) ;;
            *) continue ;;
        esac
        case "$__k" in *[!A-Za-z0-9_]*) continue ;; esac
        # QUOTES FIRST, THEN COMMENTS. The other order truncates a legitimately
        # quoted value that contains a `#` and leaves an unbalanced quote in the
        # path (review, 2026-09-07). An unquoted value still gets its trailing
        # ` # comment` removed, exactly as the compose .env parser does.
        case "$__v" in
            \"*\") __v=${__v#\"}; __v=${__v%\"} ;;
            \'*\') __v=${__v#\'}; __v=${__v%\'} ;;
            *) __v=${__v%%[[:space:]]#*}
               __v=${__v%"${__v##*[![:space:]]}"} ;;
        esac
        printf -v "$__k" '%s' "$__v" 2>/dev/null && export "$__k"
    done <"$FRAME_CONF"
fi

log()  { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
warn() { log "WARNING: $*"; }

sanitise() { printf '%s' "$1" | tr -d '\r' | tr '\n\t' '  ' | sed 's/  */ /g;s/^ *//;s/ *$//'; }

# is_num STR : true only for a plain non-negative integer. Everything that
# reaches an arithmetic comparison goes through this first — `[ "$x" -gt 7 ]`
# with a non-numeric or empty $x is a shell ERROR whose non-zero status reads
# exactly like "the condition was false", which is how a corrupt state file
# turned into a false GREEN (review, 2026-09-07).
is_num() { case "${1:-}" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }

NOW_EPOCH="$(date -u +%s)"
is_num "$NOW_EPOCH" || NOW_EPOCH=0

# ═════════════════════════════════════════════════════════════════════════════
# 1. PROBE — opportunistic, never a wake, cached with its own date-stamp
# ═════════════════════════════════════════════════════════════════════════════
probe_note=""
PROBE_MP=""

# ONE CLEANUP PATH, ON EXIT AND ON SIGNALS. The first cut used a RETURN trap
# inside the probe function, which covers an ordinary `return` but NOT SIGTERM —
# and SIGTERM is exactly what arrives when systemd's start timeout expires
# against a CIFS operation hung on a box that went to sleep mid-read. That left
# a mount pinned against a machine trying to sleep (review, 2026-09-07).
cleanup_mount() {
    [ -n "$PROBE_MP" ] || return 0
    # `timeout` on the umount too: a wedged CIFS mount can make umount itself
    # block, and -l detaches the tree so the next run is not blocked by this one.
    timeout 10 umount "$PROBE_MP" 2>/dev/null || umount -l "$PROBE_MP" 2>/dev/null
    rmdir "$PROBE_MP" 2>/dev/null
    PROBE_MP=""
}
trap 'cleanup_mount' EXIT
trap 'cleanup_mount; exit 143' TERM INT

probe_frame() {
    if ! timeout "$FRAME_PROBE_TIMEOUT" bash -c \
            "cat < /dev/null > /dev/tcp/$FRAME_PROBE_HOST/$FRAME_PROBE_PORT" 2>/dev/null; then
        probe_note="mini-serv unreachable (no answer on $FRAME_PROBE_PORT) — asleep is normal and is NOT a fault; keeping the last recorded answer"
        return 1
    fi
    if [ ! -r "$FRAME_CIFS_CREDENTIALS" ]; then
        probe_note="cannot read $FRAME_CIFS_CREDENTIALS — no credential to mount the frame share with"
        return 1
    fi

    PROBE_MP="$(mktemp -d /tmp/frame-probe.XXXXXX)" || {
        PROBE_MP=""; probe_note="could not create a mountpoint under /tmp"; return 1; }

    # --kill-after: a mount helper that ignores TERM would otherwise sit here.
    if ! timeout --kill-after=10 "$FRAME_MOUNT_TIMEOUT" \
            mount -t cifs "$FRAME_UNC" "$PROBE_MP" \
            -o "credentials=$FRAME_CIFS_CREDENTIALS,ro,$FRAME_CIFS_EXTRA" 2>/dev/null; then
        probe_note="the frame share $FRAME_UNC refused to mount (host answered but the share did not)"
        return 1
    fi

    # THE SHARE'S CONTENT IS AT ITS ROOT — it is a DEDICATED share (storage-map
    # §3b) with no subdirectory. Looking one level deeper is the documented way
    # this goes wrong and finds nothing, so there is no subdir to descend into
    # here and none should be added.
    #
    # A PARTIAL WALK MUST NOT OVERWRITE A GOOD CACHED ANSWER. If find reports
    # ANY error — a permission denial, an I/O error, the server dropping the
    # connection halfway — the newest file it did see may not be the newest file
    # there is, and writing that as a successful probe would move the cache
    # BACKWARDS and could read as green (review, 2026-09-07). So find's own exit
    # status and its stderr are both checked, and either one means "skip".
    local ferr rc out
    ferr="$(mktemp)" || { probe_note="could not create a temp file"; return 1; }
    out="$(timeout "$FRAME_WALK_TIMEOUT" find "$PROBE_MP" -type f -printf '%T@\n' 2>"$ferr")"
    rc=$?
    if [ "$rc" -ne 0 ] || [ -s "$ferr" ]; then
        probe_note="the walk of $FRAME_UNC did not complete cleanly (find exit $rc$( [ -s "$ferr" ] && printf ', errors: %s' "$(sanitise "$(head -c 200 "$ferr")")" )) — refusing to cache a possibly partial answer"
        rm -f "$ferr"; return 1
    fi
    rm -f "$ferr"

    local newest count
    newest="$(printf '%s\n' "$out" | sed 's/\..*$//' | grep -E '^[0-9]+$' | sort -rn | head -1)"
    count="$(printf '%s\n' "$out" | grep -cE '^[0-9]' )"
    if ! is_num "$newest"; then
        probe_note="mounted $FRAME_UNC but read no usable file times from it — refusing to treat that as 'nothing changed'"
        return 1
    fi
    if ! is_num "$count" || [ "$count" -eq 0 ]; then
        probe_note="mounted $FRAME_UNC but it holds NO files — refusing to treat an empty read as 'nothing changed'"
        return 1
    fi

    local tmp
    mkdir -p "$STATE_DIR" 2>/dev/null
    tmp="$PROBE_FILE.$$"
    {
        echo "# Last SUCCESSFUL, COMPLETE read of $FRAME_UNC. Written only when the"
        echo "# walk finished with no errors, so a run that could not reach mini-serv —"
        echo "# or could only read part of the share — leaves the previous answer"
        echo "# standing. That is what the ${FRAME_PROBE_MAX_AGE_DAYS}-day tolerance in the check is for."
        echo "probe_utc=$(date -u +%FT%TZ)"
        echo "probe_epoch=$NOW_EPOCH"
        echo "frame_newest_epoch=$newest"
        echo "frame_newest_utc=$(date -u -d "@$newest" +%FT%TZ 2>/dev/null || echo unknown)"
        echo "frame_files=$count"
        echo "unc=$(sanitise "$FRAME_UNC")"
    } >"$tmp" 2>/dev/null && mv "$tmp" "$PROBE_FILE" 2>/dev/null || {
        rm -f "$tmp" 2>/dev/null
        probe_note="read the frame share but could not write $PROBE_FILE"
        return 1
    }
    probe_note="probed live: $count file(s), newest $(date -u -d "@$newest" +%F 2>/dev/null)"
    return 0
}

probe_frame || true
cleanup_mount

# ── read back whatever probe answer we now have (fresh or cached) ────────────
FRAME_NEWEST=""; PROBE_EPOCH=""; FRAME_FILES=""
if [ -r "$PROBE_FILE" ]; then
    while IFS='=' read -r k v; do
        case "$k" in
            frame_newest_epoch) FRAME_NEWEST="$v" ;;
            probe_epoch)        PROBE_EPOCH="$v" ;;
            frame_files)        FRAME_FILES="$v" ;;
        esac
    done <"$PROBE_FILE"
fi
# Anything that is not a plain number is treated as ABSENT, not as zero.
is_num "$FRAME_NEWEST" || FRAME_NEWEST=""
is_num "$PROBE_EPOCH"  || PROBE_EPOCH=""

# ═════════════════════════════════════════════════════════════════════════════
# 2. THE SOURCE SIDE — read FileBackup's manifest, never the library drive
# ═════════════════════════════════════════════════════════════════════════════
SHARED_NEWEST=""; SHARED_ROWS=""; MANIFEST_WRITTEN=""; shared_err=""
read_shared() {
    if ! command -v python3 >/dev/null 2>&1; then
        shared_err="python3 is not on PATH — cannot parse the manifest"; return 1
    fi
    if [ ! -r "$MANIFEST" ]; then
        shared_err="cannot read $MANIFEST (has the library backup ever run?)"; return 1
    fi

    # The manifest's own sidecar says how many rows a COMPLETE file has. The
    # nightly rewrites the CSV in place, so a run that opens it mid-rewrite sees
    # a valid but TRUNCATED file and would compute a max over a prefix — green
    # from missing rows (review, 2026-09-07). Comparing the parsed row count to
    # the declared one refuses that.
    local declared=""
    [ -r "$MANIFEST.meta" ] && {
        declared="$(sed -n 's/^Rows=//p' "$MANIFEST.meta" | head -1 | tr -dc '0-9')"
        MANIFEST_WRITTEN="$(sed -n 's/^Written=//p' "$MANIFEST.meta" | head -1 | cut -c1-19)"
    }

    local out
    out="$(MANIFEST="$MANIFEST" SHARED_PREFIX="$SHARED_PREFIX" python3 - <<'PY' 2>/dev/null
import csv, os, re, sys, datetime
# The allow-list, ruled by the Owner 2026-09-07 (plan §7.1). `.db` is EXCLUDED
# on purpose: Thumbs.db rewrites must never look like the family photos moved.
EXT = {
 '.jpg','.jpeg','.jpe','.jfif','.png','.gif','.bmp','.tif','.tiff','.webp','.heic','.heif','.avif',
 '.dng','.cr2','.cr3','.nef','.arw','.raf','.orf','.rw2','.pef','.srw',
 '.mp4','.m4v','.mov','.mkv','.avi','.wmv','.webm','.mpg','.mpeg','.m2v','.3gp','.3g2',
 '.mts','.m2ts','.ts','.vob','.ogv','.flv','.asf','.divx','.mpv',
}
# A REAL PARSE, NOT STRING SURGERY. The first cut trimmed sub-second digits by
# counting digits across the WHOLE remainder — which swallowed the timezone
# offset's digits too, turned `...20.1462928+00:00` into `...20.14629200`, and
# silently dropped the offset. It survived only because python 3.12 tolerates 8
# fractional digits; on 3.8 it raises, every row is skipped, and the caller then
# sees an EMPTY max. Both halves are fixed here: parse with a regex that knows
# where the fraction ends, and keep the offset.
TS = re.compile(r'^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?\s*(Z|[+-]\d{2}:?\d{2})?$')
def parse(ts):
    m = TS.match(ts.strip())
    if not m:
        return None
    Y, Mo, D, h, mi, s, frac, off = m.groups()
    micro = int((frac or '0')[:6].ljust(6, '0'))
    if off in (None, 'Z', 'z'):
        tz = datetime.timezone.utc
    else:
        o = off.replace(':', '')
        sign = -1 if o[0] == '-' else 1
        tz = datetime.timezone(sign * datetime.timedelta(hours=int(o[1:3]), minutes=int(o[3:5])))
    return datetime.datetime(int(Y), int(Mo), int(D), int(h), int(mi), int(s), micro, tz)

path = os.environ['MANIFEST']; pref = os.environ['SHARED_PREFIX']
best = None; rows = 0; total = 0; unparsed = 0
csv.field_size_limit(1 << 24)
try:
    with open(path, newline='', encoding='utf-8', errors='replace') as f:
        r = csv.DictReader(f)
        if not r.fieldnames or 'RelativePath' not in r.fieldnames or 'LastWriteTimeStr' not in r.fieldnames:
            print('status=badcolumns'); sys.exit(0)
        for row in r:
            total += 1
            rp = row.get('RelativePath') or ''
            if not rp.startswith(pref):
                continue
            i = rp.rfind('.')
            if i < 0 or rp[i:].lower() not in EXT:
                continue
            rows += 1
            d = parse(row.get('LastWriteTimeStr') or '')
            if d is None:
                unparsed += 1
                continue
            e = int(d.timestamp())
            if best is None or e > best:
                best = e
except Exception as ex:
    print('status=error'); print('detail=%s' % str(ex)[:120].replace('\n', ' ')); sys.exit(0)
# KEY=VALUE, NEVER POSITIONAL. The first cut printed `OK <epoch> <rows>` and
# left the epoch EMPTY when nothing matched — so awk's $2 picked up the ROW
# COUNT and the caller compared against epoch 48734, i.e. January 1970, which
# makes any real video look newer and the lane GREEN. An absent answer must be
# absent, not accidentally numeric.
print('status=ok')
print('total=%d' % total)
print('rows=%d' % rows)
print('unparsed=%d' % unparsed)
if best is not None:
    print('newest=%d' % best)
PY
)"
    case "$out" in
        *status=ok*) ;;
        *status=badcolumns*) shared_err="the manifest does not have the expected columns"; return 1 ;;
        *status=error*) shared_err="reading the manifest failed: $(printf '%s' "$out" | sed -n 's/^detail=//p' | head -1)"; return 1 ;;
        *) shared_err="the manifest parser produced no usable answer"; return 1 ;;
    esac

    local total unparsed
    SHARED_NEWEST="$(printf '%s\n' "$out" | sed -n 's/^newest=//p' | head -1)"
    SHARED_ROWS="$(printf '%s\n' "$out"   | sed -n 's/^rows=//p'   | head -1)"
    total="$(printf '%s\n' "$out"         | sed -n 's/^total=//p'  | head -1)"
    unparsed="$(printf '%s\n' "$out"      | sed -n 's/^unparsed=//p' | head -1)"

    if ! is_num "$SHARED_NEWEST"; then
        shared_err="no media under $SHARED_PREFIX has a readable timestamp in the manifest (${SHARED_ROWS:-0} row(s) matched, ${unparsed:-0} unparseable)"
        SHARED_NEWEST=""; return 1
    fi
    if is_num "$unparsed" && [ "$unparsed" -gt 0 ]; then
        # Not fatal — the max over the rows that DID parse is still a lower
        # bound — but it is named, because a parser quietly skipping rows is how
        # the first cut went wrong.
        shared_err="note: $unparsed timestamp(s) under $SHARED_PREFIX did not parse"
    fi
    if is_num "$declared" && is_num "$total" && [ "$declared" -ne "$total" ]; then
        shared_err="the manifest looks INCOMPLETE: it declares $declared row(s) but $total were read (a rewrite in progress?) — refusing to judge freshness from a partial file"
        SHARED_NEWEST=""; return 1
    fi
    return 0
}
read_shared || true

# ═════════════════════════════════════════════════════════════════════════════
# 3. ASSESS — green only when all three conditions hold; otherwise yellow
# ═════════════════════════════════════════════════════════════════════════════
band=green; verdict=""
probe_age_days=""
if is_num "$PROBE_EPOCH" && [ "$NOW_EPOCH" -gt 0 ]; then
    probe_age_days=$(( ( NOW_EPOCH - PROBE_EPOCH ) / 86400 ))
fi

if [ -z "$FRAME_NEWEST" ]; then
    band=yellow
    verdict="the frame share has never been read successfully, so there is nothing to compare against. ${probe_note:-no probe attempted}"
elif [ -z "$probe_age_days" ]; then
    # A cached video time with no usable probe time is a HALF-WRITTEN or
    # hand-edited cache. Treating it as fresh forever is the false-green the
    # review found; it is unknown, so it is yellow.
    band=yellow
    verdict="the probe cache holds a frame timestamp but no readable probe time ($PROBE_FILE) — cannot tell how old that answer is. ${probe_note:-}"
elif [ "$probe_age_days" -lt 0 ]; then
    # Clock stepped backwards, or the cache was written by a box in the future.
    band=yellow
    verdict="the probe cache is date-stamped in the FUTURE (${probe_age_days}d) — the clock stepped or the file was tampered with; refusing to treat it as fresh. ${probe_note:-}"
elif [ "$probe_age_days" -gt "$FRAME_PROBE_MAX_AGE_DAYS" ]; then
    band=yellow
    verdict="the last successful read of the frame share is ${probe_age_days}d old (tolerance ${FRAME_PROBE_MAX_AGE_DAYS}d) — mini-serv has not been reachable for too long to judge freshness. ${probe_note:-}"
elif [ -z "$SHARED_NEWEST" ]; then
    band=yellow
    verdict="cannot establish when the media under $SHARED_PREFIX last changed: ${shared_err:-unknown reason}"
else
    drift_days=$(( ( SHARED_NEWEST - FRAME_NEWEST ) / 86400 ))
    f_utc="$(date -u -d "@$FRAME_NEWEST" +%F 2>/dev/null || echo '?')"
    s_utc="$(date -u -d "@$SHARED_NEWEST" +%F 2>/dev/null || echo '?')"
    if [ "$drift_days" -gt "$FRAME_DRIFT_MAX_DAYS" ]; then
        band=yellow
        verdict="frame videos are ${drift_days}d behind their source media: newest video $f_utc, newest media in ${SHARED_PREFIX%/} $s_utc (tolerance ${FRAME_DRIFT_MAX_DAYS}d). The generator has not been re-run since the media changed."
    else
        band=green
        verdict="frame videos are current: newest video $f_utc, newest media in ${SHARED_PREFIX%/} $s_utc (${drift_days}d apart, tolerance ${FRAME_DRIFT_MAX_DAYS}d)"
    fi
    verdict="$verdict [${FRAME_FILES:-?} video(s), ${SHARED_ROWS:-?} media file(s)"
    [ -n "$MANIFEST_WRITTEN" ] && verdict="$verdict, manifest $MANIFEST_WRITTEN"
    verdict="$verdict, probe ${probe_age_days}d old]"
fi

# THERE IS NO RED PATH ABOVE, AND THAT IS THE POINT. If you add a condition
# here, it is yellow. A red band from this lane would contradict the ruling the
# whole file is built around.
case "$band" in green|yellow) ;; *) band=yellow ;; esac
verdict="$(sanitise "$verdict")"

write_state() {
    local tmp="$STATE_FILE.$$"
    mkdir -p "$STATE_DIR" 2>/dev/null || return 1
    {
        echo "checked_utc=$(date -u +%FT%TZ)"
        echo "band=$band"
        echo "frame_newest_epoch=${FRAME_NEWEST:-}"
        echo "shared_newest_epoch=${SHARED_NEWEST:-}"
        echo "shared_media_rows=${SHARED_ROWS:-}"
        echo "probe_age_days=${probe_age_days:-}"
        echo "manifest_written=$(sanitise "${MANIFEST_WRITTEN:-}")"
        echo "reported=${REPORTED:-not-attempted}"
        echo "verdict=$(sanitise "$verdict")"
    } >"$tmp" 2>/dev/null && mv "$tmp" "$STATE_FILE" 2>/dev/null && return 0
    rm -f "$tmp" 2>/dev/null
    return 1
}

case "$band" in
    green)  log "healthy: $verdict" ;;
    yellow) log "YELLOW: $verdict" ;;
esac
[ -n "$probe_note" ] && log "probe: $probe_note"

# ═════════════════════════════════════════════════════════════════════════════
# 4. REPORT — same transport as library-guard.sh and tracker-defs-guard.sh
# ═════════════════════════════════════════════════════════════════════════════
# json_escape STR : escape a bash string for use inside a JSON string literal.
# `"` alone is not enough — a backslash, tab or control character makes the body
# invalid JSON, the tracker answers 400, and the lane goes unreported (review,
# 2026-09-07). Order matters: backslash first, or it re-escapes its own output.
json_escape() {
    printf '%s' "$1" \
        | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' \
        | tr -d '\000-\010\013\014\016-\037' \
        | tr '\n\t' '  '
}

REPORTED=not-attempted
report_failed=0
if [ "$MODE" = report ]; then
    if [ -f "$ENV_FILE" ]; then
        while IFS= read -r __line || [ -n "$__line" ]; do
            case "$__line" in ''|'#'*) continue ;; esac
            case "$__line" in NAGLIGHT_*=*) ;; *) continue ;; esac
            __k=${__line%%=*}; __v=${__line#*=}
            case "$__k" in *[!A-Za-z0-9_]*) continue ;; esac
            case "$__v" in \"*\") __v=${__v#\"}; __v=${__v%\"} ;; \'*\') __v=${__v#\'}; __v=${__v%\'} ;; esac
            printf -v "$__k" '%s' "$__v" 2>/dev/null && export "$__k"
        done <"$ENV_FILE"
    fi
    # EVERY ONE OF THESE IS A FAILURE TO REPORT, AND EACH SETS report_failed.
    # The lane cannot describe its own feed being down — that is the whole
    # reason the unit's exit status carries it instead.
    if [ -z "${NAGLIGHT_FEED_URL:-}" ]; then
        warn "NAGLIGHT_FEED_URL unset — the verdict was computed but CANNOT be reported (check id '$CHECK_ID')"
        REPORTED=no-feed-url; report_failed=1
    elif [ -z "${NAGLIGHT_FEED_CONTAINER:-}" ]; then
        warn "NAGLIGHT_FEED_CONTAINER unset — the tracker is bridge-only, so there is no host-reachable URL. NOT reported."
        REPORTED=no-container; report_failed=1
    elif ! command -v docker >/dev/null 2>&1; then
        warn "no docker binary on PATH — NOT reported"
        REPORTED=no-docker; report_failed=1
    elif ! docker inspect -f '{{.State.Running}}' "$NAGLIGHT_FEED_CONTAINER" 2>/dev/null | grep -q true; then
        warn "container '$NAGLIGHT_FEED_CONTAINER' is not running — NOT reported. The verdict above stands, but nothing on the wall carries it."
        REPORTED=tracker-down; report_failed=1
    else
        body="$(printf '{"check":"%s","color":"%s","reason":"%s"}' \
                 "$(json_escape "$CHECK_ID")" "$(json_escape "$band")" "$(json_escape "$verdict")")"
        hdr=(--header "Content-Type: application/json")
        [ -n "${NAGLIGHT_TOKEN:-}" ] && hdr+=(--header "Authorization: Bearer ${NAGLIGHT_TOKEN}")
        [ -n "${NAGLIGHT_USER:-}" ]  && hdr+=(--header "X-Forwarded-User: ${NAGLIGHT_USER}")
        ferr="$(docker exec "$NAGLIGHT_FEED_CONTAINER" wget -q -S -O - --content-on-error "${hdr[@]}" \
                  --post-data "$body" "$NAGLIGHT_FEED_URL" 2>&1)"
        frc=$?
        if [ "$frc" -eq 0 ]; then
            log "feed: reported $band"
            REPORTED=ok
        else
            fcode="$(printf '%s' "$ferr" | grep -oE 'HTTP/[0-9.]+ [0-9]{3}' | grep -oE '[0-9]{3}$' | head -1)"
            fbody="$(printf '%s' "$ferr" | grep -vE '^[[:space:]]*(HTTP/|Content-|X-Content-|Date:|Connection:|Vary:|Transfer-)' | tr -s ' \n' ' ' | sed 's/^ *//;s/ *$//')"
            warn "feed: report FAILED (wget exit $frc${fcode:+, HTTP $fcode}) - ${fbody:-no output}"
            REPORTED="failed${fcode:+-$fcode}"; report_failed=1
            case "$fbody" in
                *"unknown feeder check id: $CHECK_ID"*)
                    warn "  the tracker has NO item declaring check id '$CHECK_ID'."
                    warn "  Either maintenance.md still says \`check: video-stub\`, or the"
                    warn "  tracker is serving CACHED definitions — NagLight loads them once"
                    warn "  and never invalidates, so an edit needs \`docker restart tracker\`"
                    warn "  (see VIDEO_FRAME_GEN_CHECK_PLAN.md §9.2)." ;;
            esac
        fi
    fi
fi

if ! write_state; then
    warn "could not write $STATE_FILE — the check ran but nothing durable records it"
fi

# EXIT 1 WHEN THE VERDICT DID NOT REACH THE TRACKER. Not a red band — this lane
# has none — but a FAILED UNIT, which is the only channel left when the feed
# itself is the thing that is broken. Without this, an hourly 400 looks like an
# hourly success while the lane slides toward red on NagLight's staleness fuse.
if [ "$report_failed" -eq 1 ]; then
    warn "exiting 1: the verdict was '$band' but it was NOT reported ($REPORTED). The unit fails ON PURPOSE so this is visible in \`systemctl --failed\`."
    exit 1
fi

case "$band" in
    green)  exit 0 ;;
    *)      exit 2 ;;
esac
