#!/usr/bin/env bash
# wall-sync.sh — THE PANEL PULLS ITS MEDIA, FROM TWO HOSTS.
#
# OI-15 (the Owner, 2026-07-29) ruled that the PANEL pulls, with MIRROR
# semantics, and that `/media/*` is then served PANEL-LOCALLY by the shell's
# Electron host. That ruling still stands in full.
#
# OI-18 exit (b) (the Owner, 2026-08-03) is what this file's shape now encodes:
# THE PANEL HAS TWO MEDIA SOURCES ON TWO HOSTS, and mounts each with its OWN
# credential. Consolidating them behind one host — exit (a) — was rejected
# because it would re-open HOMELAB_TOPOLOGY.md decision 2 ("the panel pulls
# frame videos from Mini-serv directly").
#
# THE CONTRACT, derived from Personal\homelab\deploy\storage-map.md §3 row 2,
# §3b and §4d. That map is the SSOT; this table is a restatement of it, and if
# the two ever disagree the MAP WINS:
#
#   |                | music                     | frame videos                 |
#   |----------------|---------------------------|------------------------------|
#   | flow (§4d)     | sync-music                | sync-frame-videos            |
#   | host           | HOMEHUB (the AWOW)        | Mini-serv                    |
#   | UNC knob       | MEDIA_MUSIC_SHARE_UNC     | MEDIA_FRAME_SHARE_UNC        |
#   | subdir         | Music/  (under the mount) | NONE — content is at the     |
#   |                | (§3 row 2 exports         | share ROOT (§3b: a DEDICATED |
#   |                | NonDocs\Media\Music VIA   | share, PictureFrameVideos)   |
#   |                | the `Media` share)        |                              |
#   | cache leaf     | music                     | frame                        |
#   | manifest       | index.json                | playlist.json                |
#   | credential     | a HOMEHUB Samba identity  | the Mini-serv `share`        |
#   |                | (its own store key)       | account (A11(v)/A10(v))      |
#   | cadence        | boot / resume / on demand | EVERY MINUTE                 |
#   | source sleeps? | NO — the AWOW is always-on| YES, by design               |
#   | mount refused  | an ALERT (failed unit)    | see the failure policy below |
#
# THE TWO MOUNTS ARE NOT THE SAME SHAPE, and treating them uniformly is the
# first way this goes wrong: music is reached THROUGH the `Media` share and
# needs the `Music` subdir under the mount, while `PictureFrameVideos` is a
# dedicated share whose content is AT ITS ROOT. Give frame a subdir it does not
# have and the mirror looks one directory too deep, finds nothing, and — but for
# GUARD 1 below — would silently mirror an empty tree over the cache.
#
# THEY ALSO HAVE DIFFERENT FAILURE POLICIES, which is the part "just add a
# second mount" misses (storage-map §4d, A0/A10):
#   * music — the AWOW is ALWAYS-ON. A refused mount means the hub or its Samba
#     service is down, and that is a real ALERT: fail the unit, loudly.
#   * frame — MINI-SERV MAY SLEEP, and it is NEVER WOKEN for this (unlike the
#     backup service's ingest, which sends a magic packet). An UNREACHABLE frame
#     share is therefore a DESIGNED state and is SKIPPED SILENTLY. Applying the
#     music policy here would turn "Mini-serv is asleep", which is normal, into a
#     failed unit every single minute.
#
# THE SILENT SKIP IS THE ONE PLACE THIS REPO'S NEVER-SILENT-GREEN RULE BENDS,
# and it bends only as far as "unreachable". Everything else about the frame
# flow is loud:
#   * unreachable (nothing answering on 445)      -> skip, and report the AGE of
#                                                    the cache (the staleness
#                                                    ladder below)
#   * REACHABLE but the mount is REFUSED          -> FATAL. A box that answers on
#                                                    445 is not asleep; that is a
#                                                    wrong share name or a wrong
#                                                    credential, and it would
#                                                    otherwise hide forever.
#   * a missing/unreadable credentials file, an
#     unset or placeholder UNC, a failed rsync,
#     a failed manifest write                     -> FATAL, both flows. These are
#                                                    configuration defects, not
#                                                    sleeping hardware.
#
# WHAT ONE RUN DOES, per selected flow:
#   1. take a per-flow lock (the every-minute frame timer and a boot/resume run
#      must never rsync into the same directory at the same time);
#   2. mount the flow's UNC read-only over cifs with the flow's OWN credentials;
#   3. `rsync -a --delete` the flow's source into WALL_MEDIA_CACHE/<leaf>;
#   4. unmount (always — a trap, so a failure never leaves a mount behind);
#   5. regenerate THAT FLOW'S manifest inside the cache, written LAST and
#      atomically, after the media it describes:
#        music/index.json    LocalLibraryProvider's manifest (js/music/local.js)
#        frame/playlist.json the frame playlist ([{url,title}, …])
#   6. stamp the flow's last-success time (the staleness ladder reads it).
#
# MIRROR SEMANTICS — READ THIS ONCE: `--delete`. Content REMOVED from the LAN
# source DISAPPEARS from the panel cache on the next sync. That is the ruling.
# It is also the reason for the guards below: `--delete` is irreversible, and the
# panel is the wrong place to discover that a share came up empty.
#
# WHEN IT RUNS
#   * once after boot   — wall-sync.service (BOTH flows), After=network-online;
#   * ON EVERY RESUME   — wall-sync-resume.service (OI-16a, ruled 2026-07-29):
#     with SLEEP_MODE=suspend the panel resumes every morning WITHOUT booting,
#     so a resume triggers the same unit, detached (`systemctl start --no-block`)
#     so the wake is never delayed;
#   * EVERY MINUTE      — wall-sync-frame.timer -> wall-sync-frame.service, which
#     is THIS script with `--only frame`. Storage-map §4d sets that cadence for
#     the frame flow and ONLY the frame flow; re-mirroring the whole music
#     library over 802.11 every minute would be absurd, and OI-15's "no periodic
#     timer" ruling is about the music pull it was written for.
#   * ON DEMAND         — `sudo systemctl start wall-sync.service`. That IS the
#     dedicated SSH-invocable command the ruling asks for, and it still syncs
#     BOTH flows, so its meaning is unchanged.
# The resume path has one wrinkle the boot path does not: Wi-Fi re-association
# takes a few seconds after wake and network-online.target is NOT re-evaluated on
# resume, so this script does its own short bounded wait (below) before touching
# the network.
#
# GUARDS — the backup service's INGEST step learned these the expensive way
# (stack/backup/backup.sh §1b); they are transplanted rather than reinvented:
#   * an EMPTY or ABSENT source subtree does NOT get to mirror-delete a populated
#     cache. The run REFUSES, loudly, unless WALL_SYNC_ALLOW_EMPTY=true.
#   * a failed rsync, a missing python3 and a failed manifest write are all FATAL
#     and nonzero. The panel has no NagLight feed of its own (the shell pushes its
#     own heartbeat), so "loud" here means a FAILED UNIT plus journal lines:
#     `journalctl -u wall-sync` / `journalctl -u wall-sync-frame`. A silently
#     stale cache is precisely the never-silent-green failure this repo forbids.
#   * file counts are logged before and after every flow, so a mirror-delete is
#     visible in the journal as a number that went down.
#
# ONE FLOW'S FAILURE DOES NOT CANCEL THE OTHER. Each flow is attempted, its own
# verdict recorded, and the exit status is nonzero if ANY of them failed — so a
# HOMEHUB outage cannot also stop the frame videos from refreshing in the same
# run.
set -euo pipefail

ENV_FILE="/etc/wall-panel/wall.env"
PAYLOAD="/opt/wall-panel/stack/autoinstall/wall"
SELF="wall-sync"
RUNDIR="/run/$SELF"
log()  { echo "[$SELF] $*"; }
warn() { echo "[$SELF] WARNING: $*" >&2; }
err()  { echo "[$SELF] ERROR: $*" >&2; }
die()  { err "$*"; exit 1; }

# How long to wait for a TCP connect when asking "is the frame source awake?".
# NOT a knob: it is a property of a LAN round trip, not of a deployment, and the
# only thing a bigger number buys is a longer stall on a box that is asleep. A
# CONSTANT keeps the every-minute timer bounded by construction.
PROBE_TIMEOUT=4
PROBE_PORT=445

# ── THE FLOW MAP — CODE, NOT CONFIG, AND DELIBERATELY SO ─────────────────────
# The ADDRESSES and CREDENTIALS are knobs (they are site facts, and the two hosts
# differ per household). The SUBTREE / CACHE-LEAF / MANIFEST / POLICY map is NOT,
# and it must never become one: a knob there would let a typo silently widen the
# mirror onto this 2016 laptop's 256 GB disk, and the cache leaf names and
# manifest filenames are fixed by the shell's own defaults
# (`/media/music/index.json`, `/media/frame/playlist.json`) rather than by any
# local choice. The failure POLICY is likewise a ruling, not a setting — see the
# header. Written as a `case` rather than the old colon-delimited string because
# there are now seven fields per flow and two of them are prose.
#
# F_SUBDIR="" means "the share ROOT is the content" — the frame share's shape.
FLOWS="music frame"

flow_spec() {
    FLOW="$1"
    case "$FLOW" in
        music)
            F_UNC_VAR=MEDIA_MUSIC_SHARE_UNC
            F_CREDS_VAR=MEDIA_MUSIC_CIFS_CREDENTIALS
            F_OVERRIDE_VAR=MEDIA_MUSIC_SOURCE_OVERRIDE
            F_SUBDIR="Music"
            F_LEAF="music"
            F_MANIFEST="index.json"
            F_POLICY="alert"
            F_SOURCE="HOMEHUB (the AWOW) — always-on, so a refused mount is an alert"
            ;;
        frame)
            F_UNC_VAR=MEDIA_FRAME_SHARE_UNC
            F_CREDS_VAR=MEDIA_FRAME_CIFS_CREDENTIALS
            F_OVERRIDE_VAR=MEDIA_FRAME_SOURCE_OVERRIDE
            F_SUBDIR=""
            F_LEAF="frame"
            F_MANIFEST="playlist.json"
            F_POLICY="skip-if-unreachable"
            F_SOURCE="Mini-serv — MAY BE ASLEEP, and is never woken for this (§4d)"
            ;;
        *)
            die "unknown flow '$FLOW' — this script knows exactly: $FLOWS. The flow map is code, not configuration (see the header)." ;;
    esac
}

# ── config ───────────────────────────────────────────────────────────────────
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
else
    warn "$ENV_FILE missing — reading knobs from the environment only"
fi
: "${WALL_MEDIA_CACHE:=/var/cache/wall-media}"
: "${WALL_SYNC_ALLOW_EMPTY:=false}"
: "${WALL_FRAME_STALE_WARN_HOURS:=24}"

case "$WALL_MEDIA_CACHE" in
    /?*) ;;
    *) die "WALL_MEDIA_CACHE='$WALL_MEDIA_CACHE' must be an ABSOLUTE path (it is an rsync --delete destination — a relative path would resolve against whatever cwd systemd handed us)" ;;
esac
case "$WALL_FRAME_STALE_WARN_HOURS" in
    ''|*[!0-9]*) die "WALL_FRAME_STALE_WARN_HOURS='$WALL_FRAME_STALE_WARN_HOURS' must be a whole number of hours (it is compared against a file age in seconds)" ;;
esac

# flock: the frame timer fires every minute and a boot/resume run mirrors both
# flows, so two processes CAN reach the same destination directory. util-linux is
# in the wall image's package list for rtcwake already.
for tool in rsync python3 flock; do
    command -v "$tool" >/dev/null 2>&1 \
        || die "$tool is not installed — the sync cannot run (all three are in the wall image's package list; a hand-trimmed panel needs: apt-get install rsync python3 util-linux)"
done

# ── which flows this run ─────────────────────────────────────────────────────
SELECTED="$FLOWS"
while [ $# -gt 0 ]; do
    case "$1" in
        --only)
            [ $# -ge 2 ] || die "--only needs a flow name: one of $FLOWS"
            flow_spec "$2"          # validates the name, and dies naming the set
            SELECTED="$2"; shift 2 ;;
        -h|--help)
            echo "usage: $0 [--only $(echo "$FLOWS" | tr ' ' '|')]"
            echo "  no argument = every flow (the boot/resume/on-demand run)"
            exit 0 ;;
        *) die "unknown argument '$1' — usage: $0 [--only $(echo "$FLOWS" | tr ' ' '|')]" ;;
    esac
done

# ── mount / lock state, and the trap that always releases it ─────────────────
MP=""            # the current flow's mountpoint, "" when nothing is mounted
MOUNTED=0
LOCK_HELD=0
TMPDIRS=""

flow_cleanup() {
    if [ "$MOUNTED" = 1 ] && [ -n "$MP" ]; then
        umount "$MP" 2>/dev/null || warn "could not unmount $MP — check: mount | grep $MP"
        MOUNTED=0
    fi
    if [ "$LOCK_HELD" = 1 ]; then
        # Closing the descriptor is what releases the flock.
        exec 9>&-
        LOCK_HELD=0
    fi
    return 0
}

cleanup() {
    flow_cleanup
    local d
    for d in $TMPDIRS; do [ -d "$d" ] && rmdir "$d" 2>/dev/null || true; done
    return 0
}
trap cleanup EXIT

# ── helpers ──────────────────────────────────────────────────────────────────
# dir_has_files DIR : 0 iff DIR holds at least one regular file at any depth.
# Same helper, same reason as the ingest guard: "mounted" is not "has data".
dir_has_files() { [ -n "$(find "$1" -type f -print -quit 2>/dev/null)" ]; }
# count_media DIR MANIFEST : regular files in DIR, EXCLUDING the manifest this
# script writes there. Every count logged below is a MEDIA count — counting our
# own manifest would report a spurious deletion on every single run (found by the
# WSL harness, 2026-07-29: a no-op re-run claimed "1 file(s) were DELETED").
count_media() { find "$1" -type f ! -name "$2" 2>/dev/null | wc -l | tr -d ' '; }

# unc_host //HOST/SHARE -> HOST. Used only for the reachability probe; the mount
# itself is handed the whole UNC.
unc_host() { local u="${1#//}"; printf '%s' "${u%%/*}"; }

# source_awake HOST : 0 iff something answers on SMB's port within PROBE_TIMEOUT.
#
# This is the ONE question that decides whether a frame failure is silence or an
# alert, so it is asked deliberately rather than inferred from a mount error:
# `mount.cifs` returns the same rc for "asleep" and "wrong password". bash's
# /dev/tcp needs no extra package (nc is not in the image's list), and `timeout`
# bounds it — an unreachable host would otherwise sit in the kernel's TCP retry
# for over a minute, which is longer than the timer's whole period.
#
# HOST IS PASSED AS AN ARGUMENT, never interpolated into the -c string: it comes
# from a config file, and `//$(reboot)/x` is a valid-looking UNC.
source_awake() {
    timeout "$PROBE_TIMEOUT" bash -c 'exec 3<>/dev/tcp/"$1"/"$2"' _ "$1" "$PROBE_PORT" 2>/dev/null
}

# ── the staleness ladder (OI-18) ─────────────────────────────────────────────
# The frame skip is silent about the FAILURE by design; it must not be silent
# about the CONSEQUENCE. Every successful flow stamps its finish time at the
# CACHE ROOT — deliberately outside the mirrored leaf directory, where
# `rsync --delete` cannot remove it and the manifest walker cannot see it — and
# every skip reports how old the cached content now is.
#
# WHAT THIS DOES NOT DO, ON PURPOSE: it never escalates a skip into a failed
# unit, however old the cache gets. "Mini-serv has been off for a week" is an
# allowed state (§4d), and deciding at what age it stops being allowed is a
# ruling nobody has made. So the ladder MEASURES and REPORTS; whether the panel
# should ever alert on it is an open item for the Owner.
stamp_path() { printf '%s' "$WALL_MEDIA_CACHE/.$SELF-$1.stamp"; }

stamp_flow() { date +%s > "$(stamp_path "$1")" 2>/dev/null || warn "$1: could not write the last-sync stamp at $(stamp_path "$1") — the staleness ladder will report 'never synced' after the next skip"; }

# report_staleness FLOW CACHED_FILES — one journal line saying how old the cache
# is, at WARNING level once it passes WALL_FRAME_STALE_WARN_HOURS.
report_staleness() {
    local flow="$1" files="$2" sp age hrs
    sp="$(stamp_path "$flow")"
    if [ ! -f "$sp" ]; then
        warn "$flow: this panel has NEVER completed a $flow sync, and the cache holds $files file(s). If that is not a brand-new panel, the source has been unreachable for the whole life of this install."
        return 0
    fi
    # Read it into a variable and CHECK IT before arithmetic: a truncated or
    # hand-edited stamp would otherwise turn a staleness report into a shell
    # syntax error, i.e. a crash on the one path whose whole job is to stay calm.
    local when; when="$(head -n1 "$sp" 2>/dev/null || true)"
    case "$when" in
        ''|*[!0-9]*)
            warn "$flow: the last-sync stamp at $sp is unreadable ('$when'), so the age of the $files cached file(s) cannot be reported. Delete it; the next successful sync rewrites it."
            return 0 ;;
    esac
    age=$(( $(date +%s) - when ))
    [ "$age" -lt 0 ] && age=0
    hrs=$(( age / 3600 ))
    if [ "$hrs" -ge "$WALL_FRAME_STALE_WARN_HOURS" ]; then
        warn "$flow: content on the wall is now ${hrs}h old ($files cached file(s)), past the ${WALL_FRAME_STALE_WARN_HOURS}h staleness threshold (WALL_FRAME_STALE_WARN_HOURS). The source has not been reachable in that time."
    else
        log "$flow: content on the wall is ${hrs}h old ($files cached file(s)) — within the ${WALL_FRAME_STALE_WARN_HOURS}h threshold."
    fi
}

# ── the bounded post-resume network wait, done at most ONCE per run ──────────
# After a wake from S3 the Wi-Fi radio takes a few seconds to re-associate, and
# network-online.target (reached at boot) is NOT re-evaluated on resume, so unit
# ordering cannot cover this. `nm-online` exits 0 the moment NetworkManager
# reports a connection (immediately when already up — boot, timer and on-demand
# runs lose nothing) and nonzero at the 30 s cap. NON-FATAL either way: the mount
# and the probe are the real arbiters. Only on a real-mount path — a bench run
# touches no network — and only once, so a two-flow run cannot wait twice.
NET_WAITED=0
wait_for_network() {
    [ "$NET_WAITED" = 1 ] && return 0
    NET_WAITED=1
    if command -v nm-online >/dev/null 2>&1; then
        nm-online -q --timeout=30 \
            || warn "network still not up after 30s (nm-online) — trying anyway; if this fails, re-run once the panel is back on Wi-Fi: sudo systemctl start wall-sync.service"
    else
        warn "nm-online not found (network-manager is in the wall image) — skipping the post-resume network wait; the mount will be the arbiter"
    fi
    return 0
}

# ── one flow ─────────────────────────────────────────────────────────────────
# Returns 0 on success OR on a DESIGNED skip (frame, source asleep); 1 on a real
# failure, having already said why. The caller aggregates.
#
# MEDIA_{MUSIC,FRAME}_SOURCE_OVERRIDE are SUPPORTED bench hooks, not debug
# leftovers: each names a LOCAL directory to use INSTEAD OF MOUNTING, which is
# what lets the whole mirror + guard + manifest path be exercised on a machine
# with no Samba host (the WSL harness does exactly this). Everything after the
# mount is the real code path — the hook replaces the mount, not the logic —
# including the subdir shape, so a music override directory must itself contain
# `Music/`. It announces itself loudly so a run in the journal can never be
# mistaken for a real pull.
sync_flow() {
    flow_spec "$1"
    local unc="" creds="" override="" src="" from="" to="" before="" after="" bytes="" opts=""

    # Per-flow lock. NON-BLOCKING: if the other unit is already mirroring this
    # flow, the honest thing is to say so and let it finish, not to queue a
    # second rsync behind it every minute.
    install -d -m 0700 "$RUNDIR"
    exec 9>"$RUNDIR/$FLOW.lock"
    if ! flock -n 9; then
        exec 9>&-
        log "$FLOW: another wall-sync run is already mirroring this flow — skipping this one (nothing is queued; the running one does the work)."
        return 0
    fi
    LOCK_HELD=1

    to="$WALL_MEDIA_CACHE/$F_LEAF"
    install -d -m 0755 "$to"
    before="$(count_media "$to" "$F_MANIFEST")"

    # ── source: the bench hook, or the cifs mount ────────────────────────────
    # Indirect expansion (`${!VAR}`), never `eval`: the flow map names the knob,
    # and the knob's VALUE comes from a config file. `eval` would make a value of
    # `$(reboot)` executable; `${!…}` cannot execute anything.
    override="${!F_OVERRIDE_VAR:-}"
    if [ -n "$override" ]; then
        if [ ! -d "$override" ]; then
            err "$FLOW: $F_OVERRIDE_VAR='$override' is not a directory"
            return 1
        fi
        src="$override"
        log "$FLOW: BENCH HOOK ACTIVE: $F_OVERRIDE_VAR=$src — no cifs mount is performed;"
        log "$FLOW: BENCH HOOK ACTIVE: the mirror, the guards and the manifests are the real code path."
    else
        unc="${!F_UNC_VAR:-}"
        creds="${!F_CREDS_VAR:-}"

        # CONFIGURATION DEFECTS ARE FATAL FOR BOTH FLOWS. An unset UNC is not a
        # sleeping box, and the frame flow's licence to be quiet does not extend
        # to "nobody ever filled this in".
        case "$unc" in
            '')
                err "$F_UNC_VAR is not set in $ENV_FILE — the panel has no $FLOW source, so there is nothing to sync. Its value comes from the storage map ($F_SOURCE); set it (//host/share) and re-run: sudo systemctl start wall-sync.service"
                return 1 ;;
            *REPLACE_WITH*)
                err "$F_UNC_VAR is still the shipped placeholder ('$unc') in $ENV_FILE. A freshly imaged panel therefore shows this unit FAILED until the real share is filled in — that is deliberate: an empty wall with a green unit would be a lie. Fill it in and re-run: sudo systemctl start wall-sync.service"
                return 1 ;;
            //?*/?*) ;;
            *)
                err "$F_UNC_VAR='$unc' is not a //host/share UNC"
                return 1 ;;
        esac

        # A ROOT-ONLY CREDENTIALS FILE IS THE ONLY SUPPORTED FORM (changed by
        # OI-18, 2026-08-03). The inline MEDIA_CIFS_USER/MEDIA_CIFS_PASS fallback
        # is retired: one inline pair cannot serve two hosts, and duplicating it
        # per flow would have added four knobs whose only purpose is to put a
        # password somewhere less safe than the file that already exists.
        if [ -z "$creds" ]; then
            err "$F_CREDS_VAR is not set in $ENV_FILE — a root-only credentials file is the ONLY supported way to authenticate a $FLOW mount (the inline user/pass fallback was retired by OI-18). Set it to the 0600 file the image installed, e.g. /etc/wall-panel/cifs-$FLOW.creds"
            return 1
        fi
        if [ ! -r "$creds" ]; then
            err "$F_CREDS_VAR=$creds is not readable (it must exist and be root-only 0600, with username=/password= lines). A production image installs it from the materialised site payload; if this is a hand-built panel, create it by hand."
            return 1
        fi

        wait_for_network

        # THE FAILURE-POLICY FORK, and the only place the two flows differ in
        # kind rather than in value.
        if [ "$F_POLICY" = "skip-if-unreachable" ]; then
            if ! source_awake "$(unc_host "$unc")"; then
                log "$FLOW: $(unc_host "$unc") is not answering on ${PROBE_PORT}/tcp within ${PROBE_TIMEOUT}s — the source is ASLEEP or off. Skipping, and NOT waking it: that is the ruling (storage-map §4d, A0/A10), not a failure."
                report_staleness "$FLOW" "$before"
                return 0
            fi
        fi

        # Same option shape and credential precedence as the backup service's
        # mount_cifs (stack/backup/common.sh). MEDIA_CIFS_EXTRA carries the
        # protocol version and is SHARED by both mounts on purpose: it is a
        # protocol choice, and both hosts speak SMB3. Split it only if a real
        # box ever needs two different versions.
        opts="credentials=${creds},ro,iocharset=utf8,${MEDIA_CIFS_EXTRA:-vers=3.0}"
        MP="$RUNDIR/$FLOW"
        install -d -m 0700 "$MP"
        log "$FLOW: mount cifs $unc -> $MP (ro)"
        if ! mount -t cifs "$unc" "$MP" -o "$opts"; then
            if [ "$F_POLICY" = "skip-if-unreachable" ]; then
                # REACHABLE and REFUSED. Not a sleeping box — the probe above
                # just proved something is listening — so this is a wrong share
                # name or a wrong credential, and it is exactly the class of
                # fault a silent skip would hide forever.
                err "$FLOW: cifs mount REFUSED by $unc, WHICH IS AWAKE (it answered on ${PROBE_PORT}/tcp moments ago). This is NOT the designed 'source asleep' state: the share name is wrong, or the credentials in $creds are. Nothing was touched in $to."
            else
                err "$FLOW: cifs mount REFUSED: $unc ($F_SOURCE). The source box or its Samba service is down, the share name is wrong, or the credentials in $creds are. Nothing was touched in $to — the existing cache is still whatever the last good sync left."
            fi
            MP=""
            return 1
        fi
        MOUNTED=1
        src="$MP"
    fi

    # ── the mirror ───────────────────────────────────────────────────────────
    # F_SUBDIR="" is the frame share's shape: its content IS the share root, so
    # there is no directory to descend into and GUARD 1 can never fire for it
    # (the mountpoint always exists once mounted). GUARD 2 does that work there.
    if [ -n "$F_SUBDIR" ]; then from="$src/$F_SUBDIR"; else from="$src"; fi

    # GUARD 1 — the source subtree is missing entirely. A wrong share name, or a
    # source that came up without its data volume, looks exactly like "the
    # library no longer has any music".
    if [ ! -d "$from" ]; then
        if [ "$before" -gt 0 ] && [ "$WALL_SYNC_ALLOW_EMPTY" != "true" ]; then
            err "$FLOW: the source has no $F_SUBDIR/ directory AT ALL, while the cache holds $before file(s) at $to — REFUSING to mirror-delete them. Check the share (is $F_SUBDIR/ really at the root of ${unc:-$src}? storage-map §3 row 2 puts music under the \`Media\` share, not at its root). If the library genuinely has no $F_SUBDIR, set WALL_SYNC_ALLOW_EMPTY=true for one run."
            return 1
        fi
        # Mirror the emptiness through the SAME rsync path rather than a special
        # rm branch: one deletion mechanism, one thing to reason about.
        from="$(mktemp -d)"
        TMPDIRS="$TMPDIRS $from"
        if [ "$before" -gt 0 ]; then
            warn "$FLOW: $F_SUBDIR absent from the source and WALL_SYNC_ALLOW_EMPTY=true — CLEARING $before cached file(s) from $to"
        else
            warn "$FLOW: $F_SUBDIR absent from the source and the cache is empty — nothing to mirror. The $F_LEAF manifest will describe an empty library, which the shell reports as 'unavailable' (WSN-011), not as an error."
        fi
    # GUARD 2 — the subtree exists but holds no files (the ingest step's exact
    # lesson: a share can mount, and be a directory, and still be empty).
    elif ! dir_has_files "$from"; then
        if [ "$before" -gt 0 ] && [ "$WALL_SYNC_ALLOW_EMPTY" != "true" ]; then
            err "$FLOW: $from exists but contains NO files, while the cache holds $before file(s) at $to — REFUSING to mirror-delete them (--delete is irreversible from here). Set WALL_SYNC_ALLOW_EMPTY=true only if that subtree is legitimately empty."
            return 1
        fi
        [ "$before" -gt 0 ] \
            && warn "$FLOW: source is empty and WALL_SYNC_ALLOW_EMPTY=true — mirroring the emptiness ($before cached file(s) WILL be deleted)"
    fi

    log "$FLOW: mirror $from/ -> $to/ (rsync -a --delete — source deletions PROPAGATE)"
    # --chmod: the cache is read by the UNPRIVILEGED kiosk user, while the sync
    # runs as root from a cifs mount whose reported modes come from the mount
    # options rather than from the files. Normalising them here is what keeps the
    # shell able to read what was just synced.
    # --exclude /$F_MANIFEST: our own manifest lives INSIDE the mirrored
    # directory, so `--delete` would otherwise remove it on every run (leaving the
    # shell briefly with no library at all) and a source that happens to contain a
    # file of that name would overwrite it. Anchored with a leading `/` so it
    # protects only the top-level manifest, not a same-named file inside an album
    # folder.
    if ! rsync -a --delete --chmod=D755,F644 --exclude "/$F_MANIFEST" "$from/" "$to/"; then
        err "$FLOW: rsync mirror FAILED ($from/ -> $to/). The cache is now in an unknown, partially-mirrored state; the $F_LEAF manifest was NOT regenerated, so the shell keeps whatever the last complete sync described."
        return 1
    fi
    after="$(count_media "$to" "$F_MANIFEST")"
    bytes="$(du -sb "$to" 2>/dev/null | awk '{print $1}')"
    if [ "$after" -lt "$before" ]; then
        log "$FLOW: cache is now $after file(s), ${bytes:-?} byte(s) at $to — $(( before - after )) file(s) were DELETED to match the source (mirror semantics)"
    else
        log "$FLOW: cache is now $after file(s), ${bytes:-?} byte(s) at $to (was $before)"
    fi

    # Unmount as soon as the copying is done — the manifest step reads only the
    # local cache, and holding a cifs mount open for it would be a needless
    # dependency on the source box staying up.
    flow_cleanup

    # ── POST-STEP: this flow's contract ──────────────────────────────────────
    # Generated from the cache that was just written, never from the share: the
    # manifest must describe what the panel actually HAS. Written atomically
    # (temp + rename) INSIDE the directory the mirror just refreshed, and AFTER
    # it — the contract's ordering rule. The mirror leaves the previous manifest
    # in place (the `--exclude` above), so the window in which the shell could
    # fetch a manifest that disagrees with the media is one rename wide, and a run
    # that dies before this point leaves the last complete manifest rather than
    # nothing.
    #
    # `--only $F_LEAF` and not "regenerate both": the every-minute frame run must
    # not rewrite music/index.json from a music cache that a concurrent boot-time
    # mirror is still halfway through filling.
    log "$FLOW: manifest — generating $F_LEAF/$F_MANIFEST with $GEN"
    if ! python3 "$GEN" --cache "$WALL_MEDIA_CACHE" --only "$F_LEAF"; then
        err "$FLOW: manifest generation FAILED — the media is in place but the shell cannot see it (an absent manifest makes the provider report 'unavailable'). Re-run after fixing: sudo systemctl start wall-sync.service"
        return 1
    fi

    stamp_flow "$FLOW"
    SUMMARY="${SUMMARY:+$SUMMARY; }$F_LEAF($after files)"
    return 0
}

# ── run ──────────────────────────────────────────────────────────────────────
install -d -m 0755 "$WALL_MEDIA_CACHE"

# Resolve the manifest generator ONCE, before any mirroring: discovering it is
# missing after an hour of rsync would leave a full cache the shell cannot read.
GEN=""
for cand in "$(dirname "$0")/wall-media-manifest.py" "$PAYLOAD/wall-media-manifest.py"; do
    [ -f "$cand" ] && { GEN="$cand"; break; }
done
[ -n "$GEN" ] \
    || die "wall-media-manifest.py not found next to $0 or in $PAYLOAD — nothing was mirrored, because a synced cache with no manifest shows the shell no music at all"

SUMMARY=""
RC=0
for flow in $SELECTED; do
    if ! sync_flow "$flow"; then RC=1; fi
    flow_cleanup
done

if [ "$RC" -ne 0 ]; then
    log "sync FINISHED WITH FAILURES — ${SUMMARY:-nothing completed}. See the ERROR line(s) above; each flow's verdict is independent."
    exit 1
fi
log "sync complete — ${SUMMARY:-nothing to do}"
log "on demand, any time: sudo systemctl start wall-sync.service   (both flows)"
