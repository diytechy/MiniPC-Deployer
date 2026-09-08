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
#   --report         checks the library mount AND a representative Samba read,
#                    then reports only the combined file-share/backup health
#                    override. It never reports drive identity or a backup run.
#
# Exit codes: 0 = library healthy; 1 = not mounted / read-only / wrong.
# `--report` deliberately narrows that verdict: read-only and identity details
# remain operational diagnostics, but only mount absence or a failed Samba read
# may force the combined wall indicator red.
set -uo pipefail

LIBRARY_ROOT="${LIBRARY_ROOT:-/srv/library}"
ENV_FILE="${ENV_FILE:-/etc/homehub-backup/backup.env}"
# What to call the library in internal messages. The retired backup-drive timer
# used this script with another path; report mode now refuses that mixed wiring.
DRIVE_LABEL="${DRIVE_LABEL:-library}"
# A23: mountpoint<TAB>expected-by-id<TAB>label, generated from storage-map §1.
# Absent = identity is not asserted and the old two-state behaviour stands.
IDENTITY_FILE="${IDENTITY_FILE:-/etc/homehub-samba/drive-identity.conf}"
MODE="--check"
SHARE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --check)
            MODE="--check"; shift
            # The optional share name is positional only. Do not swallow the
            # next option: library-backup calls `--check --library <mount>` and
            # the old greedy parser silently checked /srv/library instead.
            if [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; then SHARE="$1"; shift; fi
            ;;
        --report)  MODE="--report"; shift ;;
        --library) LIBRARY_ROOT="$2"; shift 2 ;;
        --label)   DRIVE_LABEL="$2";  shift 2 ;;
        --expect)  IDENTITY_FILE="$2"; shift 2 ;;
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
mountinfo="${MOUNTINFO_FILE:-/proc/self/mountinfo}"
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

# ── identity: is the mounted device the drive the storage-map names? (A23) ────
# fstab mounts by LABEL so a stand-in flash drive can substitute during
# bring-up. A label proves nothing about WHICH disk answered to it, so the
# serial is checked separately here, and a mismatch is YELLOW rather than red:
# running on substitutes is the intended state for the first few days, and the
# thing that must never happen is being unable to TELL.
#
# ZERO DISK I/O, same contract as the mount check above. mountinfo field 3 is
# the device's major:minor; /sys/dev/block/<maj>:<min> resolves it to a kernel
# object, and /dev/disk/by-id/* are symlinks whose targets are read with
# readlink. Nothing opens the block device, so a parked drive stays parked —
# `blkid`/`lsblk -f` would have read the superblock and could have spun it up.
#
# Sets: identity_state = match | mismatch | unknown, and identity_note.
identity_state="unknown"
identity_note=""
resolve_identity() {
    [ -f "$IDENTITY_FILE" ] || { identity_note="no $IDENTITY_FILE — identity not asserted"; return; }

    local expected="" want_label=""
    while IFS=$'	' read -r mnt exp lbl; do
        case "$mnt" in ''|\#*) continue ;; esac
        [ "$mnt" = "$LIBRARY_ROOT" ] || continue
        expected="$exp"; want_label="$lbl"
    done < "$IDENTITY_FILE"
    [ -n "$expected" ] || { identity_note="no entry for $LIBRARY_ROOT in $IDENTITY_FILE"; return; }

    # major:minor of whatever is mounted there (mountinfo field 3).
    local majmin
    majmin="$(awk -v p="$LIBRARY_ROOT" '$5 == p { d = $3 } END { print d }' /proc/self/mountinfo)"
    [ -n "$majmin" ] || { identity_note="could not read the mounted device number"; return; }

    # Walk by-id symlinks and find the one pointing at this major:minor.
    # Partitions resolve to their own node, so compare against the device the
    # symlink actually names rather than assuming a whole-disk match.
    #
    # A FILESYSTEM LIVES ON A PARTITION, SO THE MOUNTED DEVICE IS ALMOST NEVER
    # THE WHOLE DISK. drive-identity.conf is generated from storage-map §1,
    # which records the whole-disk by-id name ('ata-WDC_...-11BCSS0_WD-XXXX');
    # what is actually mounted is 'ata-WDC_...-11BCSS0_WD-XXXX-part1'. Matching
    # on equality alone therefore reported the REAL disk as a stand-in forever,
    # and the yellow it raised was indistinguishable from a genuine substitute.
    # A '-part<N>' CANDIDATE IS ACCEPTED ON PARENTAGE, NOT ON ITS NAME.
    # '<expected>-part*' is a lexical prefix test: it matches any by-id entry
    # that merely BEGINS with the expected disk's name, so it is not the
    # guarantee the previous comment here claimed. sysfs is asked instead —
    # a partition node's parent directory IS its whole disk, so requiring that
    # parent to be the expected disk is what makes the acceptance exact.
    # Same fix, same reasoning, as backup.sh and library-backup.sh's
    # identity_note(); raised by the 2026-09-06 adversarial review.
    #
    # ASSUMED LAYOUT (stated, not assumed silently): the mounted filesystem is
    # the expected disk itself or a partition of it. A dm-crypt/LVM/MD layer in
    # between resolves to a device whose sysfs parent is not the expected disk
    # and would read as a stand-in. Every mount this guards is a plain partition
    # by design — storage-map.md §1 — so that is a constraint, not a defect.
    local diskmm="" _dt
    if [ -e "/dev/disk/by-id/$expected" ]; then
        _dt="$(readlink -f "/dev/disk/by-id/$expected" 2>/dev/null)" || _dt=""
        if [ -n "$_dt" ] && [ -r "/sys/class/block/${_dt#/dev/}/dev" ]; then
            diskmm="$(cat "/sys/class/block/${_dt#/dev/}/dev" 2>/dev/null)"
        fi
    fi

    local found="" found_exact=0 link target dev name parentmm
    for link in /dev/disk/by-id/*; do
        [ -e "$link" ] || continue
        target="$(readlink -f "$link" 2>/dev/null)" || continue
        dev="${target#/dev/}"
        [ -r "/sys/class/block/$dev/dev" ] || continue
        if [ "$(cat "/sys/class/block/$dev/dev" 2>/dev/null)" = "$majmin" ]; then
            name="${link##*/}"
            case "$name" in
                "$expected")
                    # The whole disk itself — its own name is the proof.
                    found="$name"; found_exact=1; break ;;
                "$expected"-part[0-9]*)
                    parentmm=""
                    if [ -r "/sys/class/block/$dev/../dev" ]; then
                        parentmm="$(cat "/sys/class/block/$dev/../dev" 2>/dev/null)"
                    fi
                    if [ -n "$diskmm" ] && [ "$parentmm" = "$diskmm" ]; then
                        found="$name"; found_exact=1; break
                    fi
                    # Spells like our partition, is not a child of our disk.
                    # Recorded as a candidate so the message can name it, but
                    # NOT accepted as a match.
                    [ -n "$found" ] || found="$name" ;;
                *) [ -n "$found" ] || found="$name" ;;
            esac
        fi
    done

    if [ -z "$found" ]; then
        identity_note="mounted device $majmin has no /dev/disk/by-id name to compare"
        return
    fi
    if [ "$found_exact" = 1 ]; then
        identity_state="match"
        identity_note="$found"
    else
        identity_state="mismatch"
        identity_note="expected '$expected' (label $want_label) but the mounted device is '$found' — a STAND-IN drive, not the real $DRIVE_LABEL disk"
    fi
}
[ -n "$fail_reason" ] || resolve_identity

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

# Defense in depth for a scripts-only upgrade where the old installed timer
# gets one last start before retirement. BACKUP_TARGET availability must never
# be translated into the library share-health dimension.
REPORT_LIBRARY_ROOT="${FILE_SHARE_LIBRARY_ROOT:-/srv/library}"
if [ "$LIBRARY_ROOT" != "$REPORT_LIBRARY_ROOT" ]; then
    log "REFUSED legacy report path $LIBRARY_ROOT; combined share health is owned only by $REPORT_LIBRARY_ROOT"
    logger -t homehub-library-guard -p daemon.err \
        "refused legacy report path $LIBRARY_ROOT (expected $REPORT_LIBRARY_ROOT)" 2>/dev/null || true
    exit 2
fi

# ── REATTACH: try to put a dropped drive back before reporting it red ────────
# ADDED 2026-08-28, after a stand-in USB dropped off the bus SEVEN times in 46
# minutes and every one of them left the mountpoint down until a human ran
# `mount` by hand. The fstab entries mount by LABEL with `nofail`, which is right
# for boot — it must never block — but it means a drive that RETURNS mid-session
# is not mounted either. /srv/library silently reverts to a bare directory on the
# system disk, which is precisely the silent-green shape the top of this file
# exists to prevent: shares connect, appear empty, and writes land on the eMMC.
#
# WHY HERE AND NOT IN --check: --check is wired into every share stanza as
# `root preexec` and runs on EVERY client connect. It must stay fast and quiet,
# and mounting a filesystem underneath a connecting client is not either of
# those. --report runs on a timer, which is exactly the "notice when nobody is
# looking" path this belongs to.
#
# WHAT IT WILL AND WILL NOT DO. It runs `mount <mountpoint>` and nothing else,
# so the ONLY thing it can mount is what fstab already declares for that path -
# no device guessing, no label matching of its own, nothing that could attach the
# wrong disk. If fstab has no entry, it does not act. The drive-identity check
# below still runs afterwards, so a stand-in that reattaches still reports
# YELLOW rather than being quietly blessed.
#
# AND IT CAN BE INHIBITED, because a timer that fights an operator is worse than
# one that does nothing: `touch /run/homehub-no-remount` while you fsck or swap a
# drive, and this stands down and says so.
if [ -n "$fail_reason" ] && [ "${GUARD_REATTACH:-1}" = "1" ] && ! mountpoint -q "$LIBRARY_ROOT" 2>/dev/null; then
    if [ -e /run/homehub-no-remount ]; then
        log "NOT reattaching $LIBRARY_ROOT: /run/homehub-no-remount exists (deliberate maintenance)"
    elif ! findmnt -sn --target "$LIBRARY_ROOT" >/dev/null 2>&1 \
         && ! awk -v m="$LIBRARY_ROOT" '$1 !~ /^#/ && $2 == m {found=1} END{exit !found}' /etc/fstab 2>/dev/null; then
        log "NOT reattaching $LIBRARY_ROOT: no fstab entry declares it, and this never guesses a device"
    else
        log "REATTACH: $LIBRARY_ROOT is not mounted; trying 'mount $LIBRARY_ROOT' (fstab entry only)"
        if mount "$LIBRARY_ROOT" >/dev/null 2>&1 && mountpoint -q "$LIBRARY_ROOT" 2>/dev/null; then
            log "REATTACH: SUCCEEDED - $LIBRARY_ROOT is mounted again ($(findmnt -no SOURCE,FSTYPE "$LIBRARY_ROOT" 2>/dev/null))"
            logger -t homehub-library-guard -p daemon.warning \
                "reattached $LIBRARY_ROOT after it dropped - the drive returned and nothing else would have remounted it" 2>/dev/null || true
            # RE-ASSESS AGAINST THE NEW REALITY. Reporting the verdict computed
            # while the drive was down would say "NOT MOUNTED" about a drive this
            # function just mounted — the report has to describe the box as it is
            # when the report is written. Same two questions as the inline block
            # above (mounted? writable?), then identity.
            fail_reason=""; identity_state=""; identity_note=""
            mnt_opts="$(awk -v p="$LIBRARY_ROOT" '$5 == p { o = $6 } END { print o }' "$mountinfo")"
            if [ -z "$mnt_opts" ]; then
                fail_reason="$LIBRARY_ROOT is NOT MOUNTED — the $DRIVE_LABEL drive is absent or failed to mount"
            else
                case ",$mnt_opts," in
                    *,ro,*) fail_reason="$LIBRARY_ROOT is mounted READ-ONLY — writes will fail (NTFS dirty bit? clear it from Windows)" ;;
                esac
            fi
            [ -n "$fail_reason" ] || resolve_identity
        else
            log "REATTACH: FAILED - 'mount $LIBRARY_ROOT' did not take; the drive is absent, or the filesystem refused"
        fi
    fi
fi

# The Samba preexec guard above still rejects a read-only library because a
# client allowed into a share must not discover only after connecting that
# writes cannot work.  The wall contract is intentionally coarser: a mounted
# share that passes a representative Samba read is available, and mount mode is
# not a separate user-facing health dimension.  Apply that narrowing only in
# report mode, after any reattach/reassessment has finished.
case ",${mnt_opts:-}," in
    *,ro,*)
        case "$fail_reason" in
            *"mounted READ-ONLY"*)
                fail_reason=""
                resolve_identity
                ;;
        esac
        ;;
esac

# A successful local `ls` is a representative READ through Samba, not merely a
# daemon-pid check.  The probe share is deliberately explicit: choosing a share
# by title or by the first stanza would drift with the generated storage map.
# The materialiser supplies a guest-readable share name; no password is put in
# backup.env just to answer whether the file service is reachable.
probe_samba() {
    # The full literal-value loader below also loads this key for transport.
    # It lives after the local verdict so the fast `--check` path never reads a
    # config file.  `--report` needs the probe name before that point.
    local share="${FILE_SHARE_SAMBA_PROBE_SHARE:-}"
    local seconds="${FILE_SHARE_SAMBA_PROBE_TIMEOUT_SECONDS:-}"
    if [ -f "$ENV_FILE" ]; then
        if [ -z "$share" ]; then
            share="$(sed -n 's/^[[:space:]]*FILE_SHARE_SAMBA_PROBE_SHARE[[:space:]]*=[[:space:]]*//p' "$ENV_FILE" | tail -n1)"
            share="${share%%[[:space:]]#*}"
            share="${share#\"}"; share="${share%\"}"
            share="${share#\'}"; share="${share%\'}"
        fi
        if [ -z "$seconds" ]; then
            seconds="$(sed -n 's/^[[:space:]]*FILE_SHARE_SAMBA_PROBE_TIMEOUT_SECONDS[[:space:]]*=[[:space:]]*//p' "$ENV_FILE" | tail -n1)"
            seconds="${seconds%%[[:space:]]#*}"
            seconds="${seconds#\"}"; seconds="${seconds%\"}"
            seconds="${seconds#\'}"; seconds="${seconds%\'}"
        fi
    fi
    local client="${SMBCLIENT_BIN:-smbclient}"
    seconds="${seconds:-10}"
    if [ -z "$share" ]; then
        fail_reason="representative Samba probe share is not configured"
        return
    fi
    if ! command -v "$client" >/dev/null 2>&1; then
        fail_reason="smbclient is unavailable, so Samba read availability cannot be verified"
        return
    fi
    case "$seconds" in ''|*[!0-9]*) seconds=10;; esac
    if command -v timeout >/dev/null 2>&1; then
        timeout "$seconds" "$client" "//127.0.0.1/$share" -N -c 'ls' >/dev/null 2>&1 || \
            fail_reason="representative Samba read failed"
    else
        "$client" "//127.0.0.1/$share" -N -c 'ls' >/dev/null 2>&1 || \
            fail_reason="representative Samba read failed"
    fi
}
[ -n "$fail_reason" ] || probe_samba

# Identity and mount details remain an internal safety and journal concern.
# They intentionally never appear in the single wall item: only a broken share
# is red, while a healthy share clears its override and leaves backup age alone.
if [ -n "$fail_reason" ]; then
    health="red"
    log "UNHEALTHY file share: $fail_reason"
    logger -t homehub-library-guard -p daemon.err "$fail_reason" 2>/dev/null || true
else
    health="clear"
    if [ "$identity_state" = "mismatch" ]; then
        log "NOTICE: file share healthy; internal drive identity mismatch: $identity_note"
        logger -t homehub-library-guard -p daemon.warning "internal drive identity mismatch at $LIBRARY_ROOT: $identity_note" 2>/dev/null || true
    else
        log "file share healthy (mount and representative Samba read passed)"
    fi
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
if [ -n "${FILE_SHARE_BACKUP_STATE_URL:-}" ]; then
    item_id="${FILE_SHARE_BACKUP_FEED_ID:-file-share-backup-health}"
    # IF-006 / IF-010: exactly one independent state dimension per request.
    # `clear` is NOT a success report: the server preserves lastSuccess
    # byte-for-byte, so recovery resumes the age derived from the last verified
    # FileBackup artifact rather than starting a new clock.
    body="$(printf '{"id":"%s","shareHealth":"%s"}' "$item_id" "$health")"
    if [ -n "${NAGLIGHT_FEED_CONTAINER:-}" ]; then
        hdr=(--header "Content-Type: application/json")
        [ -n "${NAGLIGHT_TOKEN:-}" ] && hdr+=(--header "Authorization: Bearer ${NAGLIGHT_TOKEN}")
        [ -n "${NAGLIGHT_USER:-}" ]  && hdr+=(--header "X-Forwarded-User: ${NAGLIGHT_USER}")
        # NAME WHAT ACTUALLY HAPPENED (C29). This used to be one line that threw
        # busybox wget's stderr at /dev/null, captured no status, and printed
        # "feed: report FAILED (tracker unreachable?)" for every possible cause.
        # It was measured on 2026-08-29 blaming the network while the transport
        # was fine: `docker exec tracker wget .../healthz` returned `ok`, and the
        # real answer was HTTP 400 - the tracker has no item declaring this
        # check id, so /api/feed rejects the post before touching any state.
        #
        # A report that cannot say why it failed sends the next person to the
        # wrong subsystem, and this one sent three people to the network. Four
        # causes, four messages, and the HTTP code carried through.
        if ! command -v docker >/dev/null 2>&1; then
            log "feed: NOT SENT - no docker binary on PATH, and NAGLIGHT_FEED_CONTAINER=$NAGLIGHT_FEED_CONTAINER asks for the docker exec transport"
            log "  (the tracker is bridge-only by design, so there is no host-reachable URL to fall back to)"
        elif ! docker inspect -f '{{.State.Running}}' "$NAGLIGHT_FEED_CONTAINER" 2>/dev/null | grep -q true; then
            log "feed: NOT SENT - container '$NAGLIGHT_FEED_CONTAINER' is not running (state: $(docker inspect -f '{{.State.Status}}' "$NAGLIGHT_FEED_CONTAINER" 2>/dev/null || echo 'no such container'))"
        else
            # `-S` IS LOAD-BEARING, AND `-q` ALONE THREW THE ANSWER AWAY.
            # The image ships GNU wget 1.24.5 on musl - NOT busybox, which
            # the old comment here and the one in backup/common.sh both
            # claim. With `-q` and no `-S`, a 4xx gives exit 8 and SILENCE:
            # measured 2026-08-29 as `report FAILED (wget exit 8) - no
            # output`, which is barely better than the message it replaced.
            # `-q -S` stays quiet about progress and still writes the
            # response headers to stderr, where the status line lives.
            # `--content-on-error` IS THE POINT, and the first cut of this fix
            # did not have it. The tracker answers 400 with a one-line
            # explanation in the BODY, and wget throws the body away on a 4xx
            # unless told not to. Without it this code GUESSED which 400 it
            # was, and guessed wrong: it blamed a missing check id while the
            # server was actually reporting that the item was not in the day's
            # log - a different fault with a different fix. Print what the
            # server said; never infer it from the status code alone.
            __ferr="$(docker exec "$NAGLIGHT_FEED_CONTAINER" wget -q -S -O - --content-on-error "${hdr[@]}" \
                        --post-data "$body" "$FILE_SHARE_BACKUP_STATE_URL" 2>&1)"
            __frc=$?
            if [ "$__frc" -eq 0 ]; then
                log "file-share/backup state: reported shareHealth=$health"
            else
                __fcode="$(printf '%s' "$__ferr" | grep -oE 'HTTP/[0-9.]+ [0-9]{3}' | grep -oE '[0-9]{3}$' | head -1)"
                __fline="$(printf '%s' "$__ferr" | tr -s ' \n' ' ' | cut -c1-160)"
                log "feed: report FAILED (wget exit $__frc${__fcode:+, HTTP $__fcode}) - ${__fline:-no output}"
                __fbody="$(printf '%s' "$__ferr" | grep -vE '^[[:space:]]*(HTTP/|Content-|X-Content-|Date:|Connection:|Vary:|Transfer-)' | tr -s ' \n' ' ' | sed 's/^ *//;s/ *$//')"
                [ -n "$__fbody" ] && log "  the tracker said: $__fbody"
                case "${__fcode:-}" in
                    400) log "  400 means the post was REJECTED before touching any state - nothing was recorded." ;;
                    401|403) log "  $__fcode = the token or the forwarded identity was refused. Check NAGLIGHT_TOKEN and NAGLIGHT_USER in $ENV_FILE." ;;
                esac
            fi
        fi
    else
        hdr=(-H "Content-Type: application/json")
        [ -n "${NAGLIGHT_TOKEN:-}" ] && hdr+=(-H "Authorization: Bearer ${NAGLIGHT_TOKEN}")
        [ -n "${NAGLIGHT_USER:-}" ]  && hdr+=(-H "X-Forwarded-User: ${NAGLIGHT_USER}")
        code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${hdr[@]}" -d "$body" "$FILE_SHARE_BACKUP_STATE_URL" 2>/dev/null || echo 000)"
        [ "$code" = "200" ] && log "file-share/backup state: reported shareHealth=$health" || log "file-share/backup state: report got HTTP $code"
    fi
else
    log "FILE_SHARE_BACKUP_STATE_URL unset — journal only (shareHealth=$health)"
fi

[ -z "$fail_reason" ]
