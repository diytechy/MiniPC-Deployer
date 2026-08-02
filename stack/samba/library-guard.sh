#!/usr/bin/env bash
# homehub-library-guard — refuse to serve, and report, when the library drive is
# not really there. (Peter's ask 2026-07-30: "can you make the shares fail in
# some way if the drive is not available? Or report something in some other
# way?" — the answer is both.)
#
# THE FAILURE THIS EXISTS TO PREVENT is not the drive dying; it is the drive
# dying QUIETLY. /srv/library is a normal directory on the eMMC that the real
# 4 TB disk mounts over. If the mount is missing — unplugged, USB dropped, NTFS
# dirty bit forcing a refusal — that directory still exists, so without this
# guard Samba happily exports it. Clients see shares that connect fine and
# appear EMPTY, and any write lands on the system disk instead of the library.
# Nothing errors. That is the silent-green shape this project forbids.
#
# TWO MODES:
#   --check [name]   exit 0/1 only. Wired into every share stanza as
#                    `root preexec` with `root preexec close = yes`, so a
#                    client connection is REFUSED rather than served empty.
#                    Must stay fast and quiet — it runs on every connect.
#   --report         same check, then log loudly and POST to NagLight. Run on
#                    a timer so a drive that vanishes at 3am is visible in the
#                    morning rather than discovered by a missing backup.
#
# Exit codes: 0 = library healthy; 1 = not mounted / read-only / wrong.
set -uo pipefail

LIBRARY_ROOT="${LIBRARY_ROOT:-/srv/library}"
ENV_FILE="${ENV_FILE:-/etc/homehub-backup/backup.env}"
# What to call this drive in messages. The guard serves TWO drives now (A21,
# 2026-08-01): the library, and the backup target — same zero-I/O check, and
# the wording has to name the right one or a red check sends you to the wrong
# cupboard. Default keeps every existing message byte-identical.
DRIVE_LABEL="${DRIVE_LABEL:-library}"
MODE="--check"
SHARE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --check)   MODE="--check";  shift; [ $# -gt 0 ] && { SHARE="$1"; shift; } ;;
        --report)  MODE="--report"; shift ;;
        --library) LIBRARY_ROOT="$2"; shift 2 ;;
        --label)   DRIVE_LABEL="$2";  shift 2 ;;
        *) shift ;;
    esac
done

fail_reason=""

# Detection reads /proc/self/mountinfo DIRECTLY rather than shelling out to
# `mountpoint` and `findmnt`. Both live in util-linux and are present on a
# normal Ubuntu box — but this script runs as `root preexec` on EVERY share
# connection, so a missing tool must not decide whether the family can reach
# their files. Worse, the old `findmnt ... || echo ''` form degraded SILENTLY:
# no findmnt meant empty options, which meant the read-only check could never
# fire. /proc/self/mountinfo is part of the kernel and always there.
#
# mountinfo field layout:  ID parent major:minor root MOUNTPOINT OPTIONS ...
#                                                  $5        $6
# Paths with spaces appear octal-escaped (\040); none of the storage-map
# mountpoints contain spaces, and a mismatch would fail CLOSED, which is right.
mountinfo='/proc/self/mountinfo'
if [ ! -r "$mountinfo" ]; then
    fail_reason="cannot read $mountinfo — unable to verify that $LIBRARY_ROOT is mounted, refusing rather than guessing"
else
    # Last match wins: a path can be mounted over more than once.
    mnt_opts="$(awk -v p="$LIBRARY_ROOT" '$5 == p { o = $6 } END { print o }' "$mountinfo")"
    if [ -z "$mnt_opts" ]; then
        fail_reason="$LIBRARY_ROOT is NOT MOUNTED — the $DRIVE_LABEL drive is absent or failed to mount"
    else
        case ",$mnt_opts," in
            *,ro,*)
                # ntfs3 falls back to read-only when the NTFS dirty bit is set
                # (Windows Fast Startup, unclean eject). Reads look perfect
                # while every write fails, so this is a failure too.
                fail_reason="$LIBRARY_ROOT is mounted READ-ONLY — writes will fail (NTFS dirty bit? clear it from Windows)"
                ;;
        esac
    fi
fi

if [ "$MODE" = "--check" ]; then
    if [ -n "$fail_reason" ]; then
        # Samba logs preexec output; keep it to one line so the reason is
        # visible in the client's connection failure context.
        echo "homehub-library-guard: REFUSING share '${SHARE:-?}': $fail_reason" >&2
        logger -t homehub-library-guard -p daemon.err "refused share '${SHARE:-?}': $fail_reason" 2>/dev/null || true
        exit 1
    fi
    exit 0
fi

# ── --report ────────────────────────────────────────────────────────────────
log() { echo "[library-guard] $*"; }

if [ -n "$fail_reason" ]; then
    log "UNHEALTHY: $fail_reason"
    logger -t homehub-library-guard -p daemon.err "$fail_reason" 2>/dev/null || true
else
    log "healthy: $LIBRARY_ROOT mounted read-write"
fi

# Report through the SAME path the backup feeder uses, so there is one way of
# talking to NagLight rather than a second mechanism to keep in step. The
# tracker is bridge-only (D2/WI-10.5), hence the docker exec transport.
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
# load_env_file FILE — export every KEY=VALUE in FILE **literally**.
#
# NEVER `source` a compose .env. Its values are literal text, and every
# basic_auth hash in this project is bcrypt — `$2a$14$…`. Sourcing makes bash
# expand them: under `set -u` it aborts on the unbound `$2` (which is exactly
# how first boot died), and WITHOUT `set -u` it is worse — `$2`/`$1` expand to
# nothing, the hash is silently corrupted, and auth then fails with nothing
# anywhere explaining why.
load_env_file() {
    local __f="$1" __line __k __v
    [ -f "$__f" ] || return 0
    while IFS= read -r __line || [ -n "$__line" ]; do
        case "$__line" in ''|'#'*) continue ;; esac
        case "$__line" in *=*) ;; *) continue ;; esac
        __k=${__line%%=*}
        __v=${__line#*=}
        __k=${__k#"${__k%%[![:space:]]*}"}
        __k=${__k%"${__k##*[![:space:]]}"}
        case "$__k" in ''|*[!A-Za-z0-9_]*) continue ;; esac
        case "$__v" in
            \"*\") __v=${__v#\"}; __v=${__v%\"} ;;
            \'*\') __v=${__v#\'}; __v=${__v%\'} ;;
            # UNQUOTED: strip a trailing ` # comment`, exactly as shell
            # sourcing and docker compose's own .env parser both do. Without
            # this, LAN_IP=0.0.0.0 followed by an explanatory comment reached
            # Technitium's API as part of the address and curl rejected the
            # URL. A `#` with no space before it is kept — it may be part of a
            # password.
            *) __v=${__v%%[[:space:]]#*}
               __v=${__v%"${__v##*[![:space:]]}"} ;;
        esac
        # Compose stores a literal '$' as '$$' (see compose_escape) because it
        # interpolates .env values. Collapse it back, so a script reading this
        # file sees exactly what the containers receive.
        __v=${__v//\$\$/\$}
        printf -v "$__k" '%s' "$__v" 2>/dev/null && export "$__k"
    done < "$__f"
}
    load_env_file "$ENV_FILE"
fi
if [ -n "${NAGLIGHT_FEED_URL:-}" ]; then
    check_id="${LIBRARY_FEED_CHECK:-library-mounted}"
    if [ -n "$fail_reason" ]; then ok=false; note="$fail_reason"; else ok=true; note="mounted read-write"; fi
    note="${note//\"/\'}"
    body="$(printf '{"check":"%s","ok":%s,"note":"%s"}' "$check_id" "$ok" "$note")"
    if [ -n "${NAGLIGHT_FEED_CONTAINER:-}" ]; then
        hdr=(--header "Content-Type: application/json")
        [ -n "${NAGLIGHT_TOKEN:-}" ] && hdr+=(--header "Authorization: Bearer ${NAGLIGHT_TOKEN}")
        [ -n "${NAGLIGHT_USER:-}" ]  && hdr+=(--header "X-Forwarded-User: ${NAGLIGHT_USER}")
        docker exec "$NAGLIGHT_FEED_CONTAINER" wget -q -O /dev/null "${hdr[@]}" \
            --post-data "$body" "$NAGLIGHT_FEED_URL" 2>/dev/null \
            && log "feed: reported ok=$ok" || log "feed: report FAILED (tracker unreachable?)"
    else
        hdr=(-H "Content-Type: application/json")
        [ -n "${NAGLIGHT_TOKEN:-}" ] && hdr+=(-H "Authorization: Bearer ${NAGLIGHT_TOKEN}")
        [ -n "${NAGLIGHT_USER:-}" ]  && hdr+=(-H "X-Forwarded-User: ${NAGLIGHT_USER}")
        code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${hdr[@]}" -d "$body" "$NAGLIGHT_FEED_URL" 2>/dev/null || echo 000)"
        [ "$code" = "200" ] && log "feed: reported ok=$ok" || log "feed: report got HTTP $code"
    fi
else
    log "NAGLIGHT_FEED_URL unset — journal only (check id would be '${LIBRARY_FEED_CHECK:-library-mounted}')"
fi

[ -z "$fail_reason" ]
