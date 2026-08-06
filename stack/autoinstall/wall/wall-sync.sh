#!/usr/bin/env bash
# wall-sync.sh — THE PANEL PULLS ITS MEDIA, FROM TWO HOSTS.
#
# OI-15 (the Owner, 2026-07-29) ruled that the PANEL pulls, with MIRROR
# semantics, and that `/media/*` is then served PANEL-LOCALLY by the shell's
# Electron host. That ruling still stands in full.
#
# OI-18 exit (b) (the Owner, 2026-08-03) is what this file's shape now encodes:
# THE PANEL HAS TWO MEDIA SOURCES ON TWO HOSTS. Consolidating them behind one
# host — exit (a) — was rejected because it would re-open HOMELAB_TOPOLOGY.md
# decision 2 ("the panel pulls frame videos from Mini-serv directly").
#
# THE TWO MOUNTS AUTHENTICATE DIFFERENTLY (Q-S7, the Owner, 2026-08-05). The
# hub's `Media` share became an ANONYMOUS read-only share, so the MUSIC mount
# now presents NO CREDENTIAL AT ALL and the HOMEHUB account it used to need was
# retired outright. The FRAME mount is unchanged: Mini-serv is a Windows box
# whose shares are "read-only to everyone", which still means "once you have
# authenticated", so it keeps the MINI-SERV `share` credential.
#
# THIS IS A SECURITY PROPERTY, NOT AN INCIDENTAL ONE. The panel hangs on a wall
# in a semi-public part of the house and it now holds exactly one Samba
# password instead of two, and NONE for the hub — the box with the private
# document trees on it. Anything that would put a HOMEHUB credential back onto
# the panel is a regression, and `MEDIA_MUSIC_CIFS_CREDENTIALS` is refused by
# name in RETIRED_KEYS so it cannot come back quietly.
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
#   | credential     | NONE — anonymous guest    | the MINI-SERV `share`        |
#   |                | mount (Q-S7, 2026-08-05)  | account (A11(v)/A10(v))      |
#   | cadence        | boot / resume / on demand | EVERY MINUTE                 |
#   | source sleeps? | NO — the AWOW is always-on| YES, by design               |
#   | mount refused  | an ALERT (failed unit)    | see the failure policy below |
#
# THERE IS NOW EXACTLY ONE `share` ACCOUNT, AND IT IS ON MINI-SERV. From
# 2026-08-03 to 2026-08-05 there were two accounts of that name on two hosts —
# ruled 2026-08-04 and treated here as a hazard, since a mis-set pair could
# authenticate against the wrong box and mirror the wrong share silently. Q-S7
# removed the HOMEHUB half rather than living with the collision, so the rule
# that survives is simpler: `share` means the MINI-SERV account, and the panel
# holds no HOMEHUB credential to confuse it with. `assert_two_distinct_sources`
# below still refuses two UNCs on the same host, because the two flows now
# differ in AUTH MODE and one host for both would mean one of them is using the
# wrong one.
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
#   * frame — MINI-SERV MAY SLEEP, and this sync NEVER SENDS IT A MAGIC PACKET
#     (unlike the backup service's ingest, which does). An UNREACHABLE frame
#     share is therefore a DESIGNED state and is SKIPPED. Applying the music
#     policy here would turn "Mini-serv is asleep", which is normal, into a
#     failed unit every single minute.
#
# THE SILENT SKIP IS THE ONE PLACE THIS REPO'S NEVER-SILENT-GREEN RULE BENDS,
# and it bends only as far as "we positively established that nothing answered".
# Everything else about the frame flow is loud:
#   * NOTHING ANSWERED AT ALL within the probe window -> SKIP, calmly, and
#     report the AGE of the cache (the staleness ladder below). This is the
#     designed "Mini-serv is asleep" state.
#   * THE PROBE COULD NOT ESTABLISH THAT                -> SKIP, but as a
#     (name will not resolve; the panel itself is off      WARNING that says the
#     the LAN; the box answered with a REFUSAL rather      state is UNKNOWN. It
#     than silence; any other probe error)                 is NOT evidence the
#                                                          source is asleep.
#   * REACHABLE but the mount is REFUSED          -> FATAL. A box that answers on
#                                                    445 is not asleep; that is a
#                                                    wrong share name or a wrong
#                                                    credential, and it would
#                                                    otherwise hide forever.
#   * a missing/unreadable/wrongly-permissioned
#     credentials file, an unset or placeholder
#     UNC, a failed rsync, a failed unmount, a
#     failed manifest write                       -> FATAL, both flows. These are
#                                                    configuration defects, not
#                                                    sleeping hardware.
# RULED 2026-08-04 (the Owner): THE STALENESS LADDER STAYS REPORTING-ONLY. No
# age, and no probe verdict short of "the box answered and then refused the
# mount", may fail this unit or post to the tracker. So the distinction above is
# carried entirely in the LOG LEVEL and the WORDING — never in the exit status.
#
# WHAT ONE RUN DOES, per selected flow:
#   1. take a per-flow lock (the every-minute frame timer and a boot/resume run
#      must never rsync into the same directory at the same time), and HOLD IT
#      until the flow is completely finished — manifest and stamp included;
#   2. mount the flow's UNC read-only over cifs with the flow's OWN credentials;
#   3. `rsync -a --delete` the flow's source into WALL_MEDIA_CACHE/<leaf>;
#   4. unmount (always — a trap, so a failure never leaves a mount behind; and a
#      failed unmount FAILS the flow rather than being warned away);
#   5. regenerate THAT FLOW'S manifest inside the cache, written LAST and
#      atomically, after the media it describes:
#        music/index.json    LocalLibraryProvider's manifest (js/music/local.js)
#        frame/playlist.json the frame playlist ([{url,title}, …])
#   6. stamp the flow's last-success time (the staleness ladder reads it);
#   7. release the lock.
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
#     cache. The run REFUSES, loudly, unless WALL_SYNC_ALLOW_EMPTY=true. "Empty"
#     is computed with EXACTLY the rsync include/exclude rules, so a source
#     holding only a file rsync then excludes still counts as empty.
#   * a failed rsync, a missing python3 and a failed manifest write are all FATAL
#     and nonzero. The panel has no NagLight feed of its own (the shell pushes its
#     own heartbeat), so "loud" here means a FAILED UNIT plus journal lines:
#     `journalctl -u wall-sync` / `journalctl -u wall-sync-frame`. A silently
#     stale cache is precisely the never-silent-green failure this repo forbids.
#   * file counts are logged before and after every flow, so a mirror-delete is
#     visible in the journal as a number that went down.
#
# ONE FLOW'S FAILURE DOES NOT CANCEL THE OTHER, AND NEITHER DOES ONE FLOW'S HANG.
# Each flow is attempted under its OWN time budget (F_BUDGET_* below), its own
# verdict recorded, and the exit status is nonzero if ANY of them failed — so a
# HOMEHUB outage, or a music mount wedged on a half-dead Wi-Fi link, cannot also
# stop the frame videos from refreshing in the same run.
set -euo pipefail

# ═════════════════════════════════════════════════════════════════════════════
# INTERNAL STATE — CODE, NOT CONFIGURATION.
#
# Everything in this block is set BEFORE the config file is read and is made
# `readonly` immediately afterwards, and `load_env_file` will only export keys on
# an explicit ALLOWLIST. Both halves are needed. Before OI-18's review this file
# exported EVERY valid identifier it found in wall.env, over names it had already
# set — so a typo (or a hand-edit) could set `FLOWS=music` and drop the frame
# flow out of every boot/resume run, `PROBE_PORT=1` and make the frame source
# permanently "asleep", or redirect `RUNDIR`, `PATH` or `TMPDIR`. The knob
# containment property this file claims — "only the UNCs, the credentials files
# and the SMB version are knobs" — is not a comment; it is this block.
# ═════════════════════════════════════════════════════════════════════════════
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

# The bench fixture root (see `--bench-source`). A FIXED, root-owned directory:
# the point of the hook is to replace the mount, and the point of the constraint
# is that it can never be pointed at `/`, at `/etc/wall-panel` (which would copy
# wall.env and BOTH credential files into the kiosk-readable cache at 0644), or
# at anything else the operator did not deliberately stage as a fixture.
BENCH_ROOT="/var/lib/wall-sync/bench"

# ── THE FLOW MAP — CODE, NOT CONFIG, AND DELIBERATELY SO ─────────────────────
# The ADDRESSES and CREDENTIALS are knobs (they are site facts, and the two hosts
# differ per household). The SUBTREE / CACHE-LEAF / MANIFEST / POLICY / BUDGET
# map is NOT, and it must never become one: a knob there would let a typo
# silently widen the mirror onto this 2016 laptop's 256 GB disk, and the cache
# leaf names and manifest filenames are fixed by the shell's own defaults
# (`/media/music/index.json`, `/media/frame/playlist.json`) rather than by any
# local choice. The failure POLICY is likewise a ruling, not a setting — see the
# header. Written as a `case` rather than the old colon-delimited string because
# there are now ten fields per flow and two of them are prose.
#
# F_SUBDIR="" means "the share ROOT is the content" — the frame share's shape.
FLOWS="music frame"

# ── the configuration ALLOWLIST ──────────────────────────────────────────────
# Exactly the keys this script reads. wall.env legitimately carries a dozen other
# knobs (Wi-Fi, the sleep window, the kiosk command) that belong to other
# scripts; those are IGNORED here without comment, which is the whole point —
# nothing outside this list can reach this script's variables.
CONFIG_KEYS="WALL_MEDIA_CACHE WALL_SYNC_ALLOW_EMPTY WALL_FRAME_STALE_WARN_HOURS MEDIA_CIFS_VERS MEDIA_MUSIC_SHARE_UNC MEDIA_FRAME_SHARE_UNC MEDIA_FRAME_CIFS_CREDENTIALS"

# Keys this script USED to honour and now refuses, loudly, rather than ignoring:
# a panel whose wall.env still sets one of these was configured against the old
# contract, and silently dropping the setting is exactly the kind of no-op that
# takes a day to find. Each is named with what replaced it.
RETIRED_KEYS="MEDIA_CIFS_EXTRA MEDIA_CIFS_USER MEDIA_CIFS_PASS MEDIA_SOURCE_OVERRIDE MEDIA_MUSIC_SOURCE_OVERRIDE MEDIA_FRAME_SOURCE_OVERRIDE MEDIA_MUSIC_CIFS_CREDENTIALS"

retired_key_help() {
    case "$1" in
        MEDIA_CIFS_EXTRA)
            printf '%s' "replaced by MEDIA_CIFS_VERS, a validated enum. It was an unrestricted option string appended AFTER the code-built options, so 'prefixpath=Movies', 'ip=…', 'rw' or a second 'credentials=' could override the fixed subtree, the host, the read-only policy or the credential — i.e. it was a second, undeclared way to widen the mirror. Set MEDIA_CIFS_VERS=3.0 (or 3.1.1/2.1) instead" ;;
        MEDIA_CIFS_USER|MEDIA_CIFS_PASS)
            printf '%s' "retired by OI-18: one inline pair cannot serve two hosts. The frame flow uses a root-only credentials file (MEDIA_FRAME_CIFS_CREDENTIALS); the music flow authenticates with nothing at all (see MEDIA_MUSIC_CIFS_CREDENTIALS below)" ;;
        MEDIA_MUSIC_CIFS_CREDENTIALS)
            # The music flow is the ONLY unauthenticated mount in this script,
            # and it must stay that way by construction: if this key still had
            # meaning, a panel could quietly go back to shipping a HOMEHUB
            # password on a wall-mounted box.
            printf '%s' "retired 2026-08-05: //homehub/Media is now an ANONYMOUS read-only share (storage-map Q-S7), so the music mount presents no credential and the HOMEHUB 'share' account it named no longer exists. The panel is credential-free for music by design — do not recreate the account to satisfy this line. Remove the line; MEDIA_FRAME_CIFS_CREDENTIALS (Mini-serv) is unaffected" ;;
        *SOURCE_OVERRIDE)
            printf '%s' "the bench hook is no longer a configuration key. It is now the command-line option '--bench-source FLOW=DIR', it refuses to run under systemd, and the directory must live under $BENCH_ROOT. As a config key it was an unrestricted PRODUCTION setting: pointing it at /etc/wall-panel copied wall.env and BOTH credential files into the kiosk-readable cache at 0644" ;;
        *)  printf '%s' "no longer read by this script" ;;
    esac
}

# ── config ───────────────────────────────────────────────────────────────────
# load_env_file FILE — export the ALLOWLISTED KEY=VALUE pairs in FILE,
# **literally**.
#
# NEVER `source` a compose .env. Its values are literal text, and every
# basic_auth hash in this project is bcrypt — `$2a$14$…`. Sourcing makes bash
# expand them: under `set -u` it aborts on the unbound `$2` (which is exactly
# how first boot died), and WITHOUT `set -u` it is worse — `$2`/`$1` expand to
# nothing, the hash is silently corrupted, and auth then fails with nothing
# anywhere explaining why.
#
# Contract:
#   Inputs:  __f: str (path; a missing file is not an error)
#   Outputs: exports only keys listed in $CONFIG_KEYS; records any $RETIRED_KEYS
#            it saw in $SEEN_RETIRED for the caller to refuse on
#   Config:  $CONFIG_KEYS, $RETIRED_KEYS (both internal constants)
SEEN_RETIRED=""
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
        # THE ALLOWLIST. An unknown key is ignored in silence (wall.env is shared
        # with wall-firstboot.sh and wall-sleep.sh, so most keys in it are not
        # ours); a RETIRED key is remembered and refused by the caller.
        case " $RETIRED_KEYS " in *" $__k "*) SEEN_RETIRED="$SEEN_RETIRED $__k"; continue ;; esac
        case " $CONFIG_KEYS " in *" $__k "*) ;; *) continue ;; esac
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

if [ -f "$ENV_FILE" ]; then
    load_env_file "$ENV_FILE"
else
    warn "$ENV_FILE missing — reading knobs from the environment only"
fi

# A retired key is refused wherever it came from: the file (recorded above) or
# the inherited environment (a systemd Environment= line, or a shell).
for k in $RETIRED_KEYS; do
    case " $SEEN_RETIRED " in *" $k "*) die "$k is set in $ENV_FILE and is NO LONGER READ: $(retired_key_help "$k"). Remove the line and re-run." ;; esac
    # Indirect expansion, never `eval` — same rule as everywhere else in this
    # file: a config value must never be able to become a command.
    [ -z "${!k:-}" ] || die "$k is set in this run's ENVIRONMENT and is NO LONGER READ: $(retired_key_help "$k"). Unset it and re-run."
done
unset k

# EVERYTHING ABOVE IS NOW FROZEN. `readonly` is the belt to the allowlist's
# braces: even if a future edit widens the allowlist by accident, nothing in
# wall.env can reassign an internal name after this line.
readonly ENV_FILE PAYLOAD SELF RUNDIR PROBE_TIMEOUT PROBE_PORT BENCH_ROOT FLOWS CONFIG_KEYS RETIRED_KEYS

flow_spec() {
    FLOW="$1"
    case "$FLOW" in
        music)
            F_UNC_VAR=MEDIA_MUSIC_SHARE_UNC
            # NO CREDENTIAL — //homehub/Media is an anonymous read-only share
            # (storage-map Q-S7, ruled 2026-08-05). See F_AUTH below.
            F_CREDS_VAR=""
            F_AUTH="guest"
            F_SUBDIR="Music"
            F_LEAF="music"
            F_MANIFEST="index.json"
            F_POLICY="alert"
            # Time budgets. The FIRST music mirror is a whole library over 802.11
            # from a 2016 laptop's radio, so it is generous; the point of having a
            # budget at all is that a WEDGED music mount can no longer eat the
            # whole unit's TimeoutStartSec and leave the frame flow unrun.
            F_MOUNT_TIMEOUT=60
            F_RSYNC_TIMEOUT=3000
            F_SOURCE="HOMEHUB (the AWOW) — always-on, so a refused mount is an alert"
            ;;
        frame)
            F_UNC_VAR=MEDIA_FRAME_SHARE_UNC
            F_CREDS_VAR=MEDIA_FRAME_CIFS_CREDENTIALS
            # Mini-serv's shares are read-only to EVERYONE, which on Windows
            # still means "after you authenticate" (storage-map §3b). This flow
            # keeps its credential; only the HOMEHUB side went anonymous.
            F_AUTH="credentials"
            F_SUBDIR=""
            F_LEAF="frame"
            F_MANIFEST="playlist.json"
            F_POLICY="skip-if-unreachable"
            # Bounded well inside wall-sync-frame.service's TimeoutStartSec: the
            # frame set is a handful of rendered videos, so a run still going
            # after this is stuck, not busy.
            F_MOUNT_TIMEOUT=20
            F_RSYNC_TIMEOUT=90
            F_SOURCE="Mini-serv — MAY BE ASLEEP, and no magic packet is ever sent for this (§4d)"
            ;;
        *)
            die "unknown flow '$FLOW' — this script knows exactly: $FLOWS. The flow map is code, not configuration (see the header)." ;;
    esac
}

# How long to allow one unmount. Not per-flow: a stuck cifs unmount is stuck for
# reasons that have nothing to do with which share it is.
UMOUNT_TIMEOUT=30
readonly UMOUNT_TIMEOUT

# ── path safety: the mirror destination must not be re-pointed by a symlink ──
# `rsync -a --delete DEST/` FOLLOWS a symlinked DEST (proven on the bench:
# rsync into `link/` where link -> real/ writes into real/ and deletes there).
# So an absolute-path check is not enough — the destination has to be a real
# directory chain, or the one knob that says WHERE the mirror lands can be made
# to land somewhere else entirely by anything that can drop a symlink.
assert_no_symlink_components() {   # PATH LABEL
    local path="$1" label="$2" acc="" rest comp
    rest="${path#/}"
    while [ -n "$rest" ]; do
        comp="${rest%%/*}"
        if [ "$comp" = "$rest" ]; then rest=""; else rest="${rest#*/}"; fi
        [ -n "$comp" ] || continue
        case "$comp" in
            .|..) die "$label='$path' contains a '$comp' component. It is an rsync --delete destination and must be written out in full, with no traversal." ;;
        esac
        acc="$acc/$comp"
        if [ -L "$acc" ]; then
            die "$label='$path': the component '$acc' is a SYMLINK. This is an rsync --delete destination and a symlinked component silently redirects both the copy AND the deletion. Replace it with a real directory (or point $label somewhere else)."
        fi
    done
}

: "${WALL_MEDIA_CACHE:=/var/cache/wall-media}"
: "${WALL_SYNC_ALLOW_EMPTY:=false}"
: "${WALL_FRAME_STALE_WARN_HOURS:=24}"
: "${MEDIA_CIFS_VERS:=3.0}"

case "$WALL_MEDIA_CACHE" in
    /?*) ;;
    *) die "WALL_MEDIA_CACHE='$WALL_MEDIA_CACHE' must be an ABSOLUTE path (it is an rsync --delete destination — a relative path would resolve against whatever cwd systemd handed us)" ;;
esac
# Normalise before anything derives a path from it: a trailing slash would make
# the count/exclude paths below (`$dir/$manifest`) fail to match.
while : ; do
    case "$WALL_MEDIA_CACHE" in
        */) WALL_MEDIA_CACHE="${WALL_MEDIA_CACHE%/}" ;;
        *)  break ;;
    esac
done
[ -n "$WALL_MEDIA_CACHE" ] || die "WALL_MEDIA_CACHE cannot be '/' — it is an rsync --delete destination"
assert_no_symlink_components "$WALL_MEDIA_CACHE" WALL_MEDIA_CACHE

case "$WALL_FRAME_STALE_WARN_HOURS" in
    ''|*[!0-9]*) die "WALL_FRAME_STALE_WARN_HOURS='$WALL_FRAME_STALE_WARN_HOURS' must be a whole number of hours (it is compared against a file age in seconds)" ;;
esac
case "$WALL_SYNC_ALLOW_EMPTY" in
    true|false) ;;
    *) die "WALL_SYNC_ALLOW_EMPTY='$WALL_SYNC_ALLOW_EMPTY' must be exactly 'true' or 'false'. It is the switch that permits an irreversible mirror-delete, so anything ambiguous is refused rather than read as 'not true'." ;;
esac
# THE ONLY cifs knob, and it is an ENUM. Everything else in the option string is
# built in code below — see MEDIA_CIFS_EXTRA in retired_key_help() for what an
# unrestricted option string could do to the subtree, the host and the mount's
# read-only policy.
case "$MEDIA_CIFS_VERS" in
    3.1.1|3.0|2.1) ;;
    *) die "MEDIA_CIFS_VERS='$MEDIA_CIFS_VERS' is not one of the supported SMB dialects: 3.1.1, 3.0, 2.1. (SMB1 is deliberately not offered.)" ;;
esac

# flock: the frame timer fires every minute and a boot/resume run mirrors both
# flows, so two processes CAN reach the same destination directory. util-linux is
# in the wall image's package list for rtcwake already.
for tool in rsync python3 flock timeout stat find; do
    command -v "$tool" >/dev/null 2>&1 \
        || die "$tool is not installed — the sync cannot run (all of them are in the wall image's package list; a hand-trimmed panel needs: apt-get install rsync python3 util-linux coreutils findutils)"
done

# ── UNC validation, and the two-source property ──────────────────────────────
# `//host/share` and NOTHING ELSE. The old check was `//?*/?*`, which accepts
# `//MINI-SERV/NetworkShare` (a typo that mirrors an entire general-purpose share
# onto a 256 GB panel disk) and also `//host/share/Movies` and
# `//host/share/../other`. The subtree is supposed to be code; a UNC with a path
# tail is a second way to choose one.
unc_host()  { local u="${1#//}"; printf '%s' "${u%%/*}"; }
unc_share() { local u="${1#//}"; printf '%s' "${u#*/}"; }

assert_unc_shape() {   # VARNAME VALUE
    local var="$1" unc="$2" host share
    case "$unc" in
        //?*/?*) ;;
        *) die "$var='$unc' is not a //host/share UNC" ;;
    esac
    host="$(unc_host "$unc")"
    share="$(unc_share "$unc")"
    case "$host" in
        *[!A-Za-z0-9.-]*|-*|.*|*.) die "$var='$unc': '$host' is not a usable host name or address (letters, digits, dots and hyphens only). It is passed to a TCP connect and to mount.cifs." ;;
    esac
    case "$share" in
        */*) die "$var='$unc' names a PATH INSIDE a share ('$share'). Only //host/share is accepted: which subtree of the share this flow mirrors is CODE (music takes Music/ under the mount, frame takes the share root), and a path tail here would be a second, undeclared way to choose it — including a way to widen the mirror onto the panel's small disk." ;;
        .|..) die "$var='$unc': '$share' is not a share name" ;;
        *[!A-Za-z0-9._\ -]*) die "$var='$unc': the share name '$share' has characters this script will not pass to mount.cifs (letters, digits, spaces, dot, underscore and hyphen only)" ;;
    esac
}

# THE TWO SOURCES MUST BE TWO SOURCES. OI-18 exit (b) is two sources on two
# hosts: HOMEHUB for music, Mini-serv for frame video. They also now differ in
# AUTH MODE — music is anonymous (storage-map Q-S7, 2026-08-05), frame presents
# the Mini-serv `share` credential — and that is precisely why collapsing them
# onto one host is worth refusing here rather than letting the mounts sort it
# out. Pointing the frame UNC at HOMEHUB would mount it anonymously-or-not
# depending on which flow got there first; pointing the music UNC at Mini-serv
# would send no credential to a box that requires one and report it as an
# ALERT-policy failure, which reads like an outage rather than a config error.
#
# The credentials-file collision check that used to live here is GONE with the
# key it compared: there is only one credentials file left, so two of them
# cannot be the same file. MEDIA_MUSIC_CIFS_CREDENTIALS is now in RETIRED_KEYS
# and is refused by name, which covers the stale-config case far more directly
# than an equality test would have.
#
# Refused here, once, at config time — not per flow, because a `--only frame`
# run must also refuse a wall.env that has collapsed the two sources into one.
assert_two_distinct_sources() {
    local mu="${MEDIA_MUSIC_SHARE_UNC:-}" fu="${MEDIA_FRAME_SHARE_UNC:-}"
    local mh fh
    if [ -n "$mu" ] && [ -n "$fu" ]; then
        case "$mu$fu" in *REPLACE_WITH*) return 0 ;; esac
        mh="$(unc_host "$mu" | tr 'A-Z' 'a-z')"
        fh="$(unc_host "$fu" | tr 'A-Z' 'a-z')"
        [ "$mh" != "$fh" ] || die "MEDIA_MUSIC_SHARE_UNC and MEDIA_FRAME_SHARE_UNC both name the host '$mh'. OI-18 exit (b) is TWO sources on TWO hosts (HOMEHUB for music, Mini-serv for frame video), and they authenticate differently: music mounts ANONYMOUSLY (//homehub/Media is a guest read-only share, storage-map Q-S7) while frame presents the Mini-serv 'share' credential. One host for both means one of those two mounts is using the wrong auth mode for the box it is talking to. Fix the UNCs, or re-open OI-18 with the Owner."
    fi
}

[ -z "${MEDIA_MUSIC_SHARE_UNC:-}" ] || case "$MEDIA_MUSIC_SHARE_UNC" in
    *REPLACE_WITH*) ;;
    *) assert_unc_shape MEDIA_MUSIC_SHARE_UNC "$MEDIA_MUSIC_SHARE_UNC" ;;
esac
[ -z "${MEDIA_FRAME_SHARE_UNC:-}" ] || case "$MEDIA_FRAME_SHARE_UNC" in
    *REPLACE_WITH*) ;;
    *) assert_unc_shape MEDIA_FRAME_SHARE_UNC "$MEDIA_FRAME_SHARE_UNC" ;;
esac
assert_two_distinct_sources

# ── which flows this run, and the bench hook ─────────────────────────────────
# THE BENCH HOOK IS A COMMAND-LINE MODE, NOT A SETTING (OI-18 review, 2026-08-04).
# It used to be MEDIA_{MUSIC,FRAME}_SOURCE_OVERRIDE in wall.env, i.e. an
# unrestricted PRODUCTION knob naming any directory on the panel: `=/` mirrored
# the panel's whole filesystem into the cache, and `=/etc/wall-panel` copied
# wall.env and BOTH credential files there at 0644, readable by the kiosk user.
# That is a credential disclosure through a debug knob. It is now:
#   * a command-line option, so the systemd units (which pass nothing but
#     `--only frame`) cannot reach it;
#   * refused outright when systemd is the caller (INVOCATION_ID is set);
#   * constrained to a root-owned fixture directory under $BENCH_ROOT.
# Everything AFTER the mount is still the real code path — the hook replaces the
# mount, not the logic — including the subdir shape, so a music fixture must
# itself contain `Music/`.
BENCH_music=""
BENCH_frame=""

assert_bench_dir() {   # DIR
    local d="$1"
    case "$d" in
        "$BENCH_ROOT"/?*) ;;
        *) die "--bench-source directory '$d' is not under $BENCH_ROOT. Bench fixtures live in exactly one place, root-owned, so that a debug hook can never be aimed at /etc/wall-panel (wall.env plus BOTH credential files) or at /. Stage the fixture there: sudo install -d -m 0700 $BENCH_ROOT && sudo cp -a … $BENCH_ROOT/" ;;
    esac
    assert_no_symlink_components "$d" "--bench-source directory"
    [ -d "$d" ] || die "--bench-source directory '$d' is not a directory"
    local owner
    owner="$(stat -c '%u' "$d" 2>/dev/null || printf '?')"
    [ "$owner" = "0" ] || die "--bench-source directory '$d' is owned by uid $owner, not root. A fixture the kiosk user can rewrite is a way to choose what root mirrors."
    [ -z "$(find "$d" -maxdepth 0 -perm /022 -print -quit 2>/dev/null)" ] \
        || die "--bench-source directory '$d' is group- or other-writable. Fix: sudo chmod go-w '$d'"
}

bench_set() {   # FLOW=DIR
    local spec="$1" f d
    case "$spec" in *=*) ;; *) die "--bench-source wants FLOW=DIR (one of: $FLOWS), got '$spec'" ;; esac
    f="${spec%%=*}"; d="${spec#*=}"
    flow_spec "$f"          # validates the flow name, and dies naming the set
    case "$d" in /*) ;; *) die "--bench-source '$spec': the directory must be an absolute path" ;; esac
    assert_bench_dir "$d"
    printf -v "BENCH_$f" '%s' "$d"
    BENCH_USED=1
}
BENCH_USED=0

SELECTED="$FLOWS"
while [ $# -gt 0 ]; do
    case "$1" in
        --only)
            [ $# -ge 2 ] || die "--only needs a flow name: one of $FLOWS"
            flow_spec "$2"          # validates the name, and dies naming the set
            SELECTED="$2"; shift 2 ;;
        --bench-source)
            [ $# -ge 2 ] || die "--bench-source needs FLOW=DIR, one flow of: $FLOWS"
            [ -z "${INVOCATION_ID:-}" ] \
                || die "--bench-source is a BENCH-ONLY mode and this process was started by systemd (INVOCATION_ID is set). The panel's units mirror real shares; if you meant to bench, run the script by hand: sudo $0 --bench-source $2"
            bench_set "$2"; shift 2 ;;
        -h|--help)
            echo "usage: $0 [--only $(echo "$FLOWS" | tr ' ' '|')] [--bench-source FLOW=DIR]"
            echo "  no argument     = every flow (the boot/resume/on-demand run)"
            echo "  --bench-source  = mirror from a root-owned fixture under $BENCH_ROOT"
            echo "                    INSTEAD OF mounting that flow's share. Bench only:"
            echo "                    refused when systemd is the caller."
            exit 0 ;;
        *) die "unknown argument '$1' — usage: $0 [--only $(echo "$FLOWS" | tr ' ' '|')] [--bench-source FLOW=DIR]" ;;
    esac
done

# ── mount / lock state, and the trap that always releases it ─────────────────
MP=""            # the current flow's mountpoint, "" when nothing is mounted
MOUNTED=0
LOCK_HELD=0
TMPDIRS=""

# release_mount : 0 iff nothing is mounted afterwards. NON-ZERO means the mount
# is STILL THERE — which used to be warned about and then converted into success
# (MOUNTED cleared, lock released, EXIT trap disarmed), so the unit went green
# with a cifs mount still held open under /run. A left-behind mount blocks the
# next run's mountpoint and pins a dead server connection; it is a failure.
release_mount() {
    [ "$MOUNTED" = 1 ] && [ -n "$MP" ] || { MP=""; MOUNTED=0; return 0; }
    if timeout "$UMOUNT_TIMEOUT" umount "$MP" 2>/dev/null; then MOUNTED=0; MP=""; return 0; fi
    # One retry: a just-finished rsync can still have the last file open for a
    # moment, and that is the common, harmless case.
    sleep 1
    if timeout "$UMOUNT_TIMEOUT" umount "$MP" 2>/dev/null; then MOUNTED=0; MP=""; return 0; fi
    return 1
}

release_lock() {
    if [ "$LOCK_HELD" = 1 ]; then
        # Closing the descriptor is what releases the flock.
        exec 9>&-
        LOCK_HELD=0
    fi
    return 0
}

# flow_cleanup : end-of-flow teardown, in order. UNMOUNT AND UNLOCK ARE SEPARATE
# STEPS AND ONLY THIS ONE RELEASES THE LOCK — the mirror unmounts as soon as the
# copying is done (below), but the lock is held all the way through manifest
# generation and stamping. Releasing it at unmount time, as this used to, let
# another unit take the lock and mutate the cache WHILE the manifest walker was
# reading it: a manifest describing a mixture of two syncs, published as
# authoritative, over a cache then stamped fresh.
flow_cleanup() {
    local rc=0
    if ! release_mount; then
        warn "could not unmount $MP — check: mount | grep $MP"
        rc=1
    fi
    release_lock
    return "$rc"
}

cleanup() {
    flow_cleanup || true
    local d
    for d in $TMPDIRS; do [ -d "$d" ] && rmdir "$d" 2>/dev/null || true; done
    return 0
}
trap cleanup EXIT

# ── helpers ──────────────────────────────────────────────────────────────────
# THE COUNT AND THE EMPTINESS TEST MUST AGREE WITH RSYNC, EXACTLY.
#
# rsync is run with `--exclude /$MANIFEST` and `--exclude /.wall-manifest.*` —
# both ANCHORED, so they hide exactly two things and only at the TOP LEVEL of the
# mirrored directory. Two bugs came out of counting differently from that:
#   * `find … ! -name index.json` excluded a same-named file at EVERY depth, so a
#     cache holding `Album/index.json` (a real mirrored file) counted as zero and
#     the "refusing to mirror-delete a populated cache" guard never fired;
#   * `dir_has_files` counted files rsync then EXCLUDES, so a source holding only
#     a top-level `index.json` looked non-empty, and the run mirror-deleted the
#     whole cache with WALL_SYNC_ALLOW_EMPTY=false.
# Both helpers therefore take the manifest name and exclude it by EXACT PATH.
MANIFEST_TMP_PREFIX=".wall-manifest."
readonly MANIFEST_TMP_PREFIX

# count_media DIR MANIFEST : regular files in DIR at any depth, excluding exactly
# the top-level manifest this script writes there (counting our own manifest
# would report a spurious deletion on every single run — found by the WSL
# harness, 2026-07-29: a no-op re-run claimed "1 file(s) were DELETED") and any
# top-level manifest temp file.
count_media() {
    find "$1" -type f ! -path "$1/$2" ! -path "$1/$MANIFEST_TMP_PREFIX*" -print 2>/dev/null | wc -l | tr -d ' '
}

# source_has_syncable_entries DIR MANIFEST : 0 iff DIR holds at least one entry
# rsync would actually transfer — any non-directory at any depth other than the
# excluded top-level manifest. "mounted" is not "has data", and "has a file" is
# not "has a file rsync will copy".
source_has_syncable_entries() {
    [ -n "$(find "$1" ! -type d ! -path "$1/$2" ! -path "$1/$MANIFEST_TMP_PREFIX*" -print -quit 2>/dev/null)" ]
}

# ── the reachability probe ───────────────────────────────────────────────────
# THIS IS THE ONE QUESTION THAT DECIDES WHETHER A FRAME FAILURE IS SILENCE OR AN
# ALERT, so it is asked deliberately rather than inferred from a mount error:
# `mount.cifs` returns the same rc for "asleep" and "wrong password".
#
# WHAT THE OLD VERSION GOT WRONG, and it is the subtle one: it distinguished only
# "TCP connect completed within 4 s" from "anything else", and called everything
# else ASLEEP. Samba stopped, a firewall REJECT, a firewall DROP, a DNS failure,
# the panel's OWN Wi-Fi being down, a slow LAN — all of them reported "Mini-serv
# is asleep", which none of them is evidence for. A wrong credential behind a
# permanently-failing probe would then never be attempted, and so never reported.
#
# The three outcomes now, and the reasoning for each:
#   0 AWAKE         the connect completed. Proceed to the mount; a refusal from
#                   here is FATAL (see the header).
#   1 OFFLINE       nothing answered AT ALL inside PROBE_TIMEOUT (`timeout`
#                   killed the connect, rc 124), or the link layer said the host
#                   is not there (no ARP reply -> EHOSTUNREACH). This is the
#                   POSITIVELY-ESTABLISHED offline state §4d designs for, and the
#                   only one that earns the calm, silent skip.
#   2 INDETERMINATE anything else: the name will not resolve, the panel itself has
#                   no default route, or the box answered with a REFUSAL (RST),
#                   which means it is UP and not serving 445. Skipped too — the
#                   staleness ladder is reporting-only (ruled 2026-08-04) and a
#                   sleeping-source flow must never fail the unit — but reported
#                   as a WARNING that says the state is unknown, so the operator
#                   is not told a box is asleep when it demonstrably is not.
#
# HOST IS PASSED AS AN ARGUMENT, never interpolated into the -c string: it comes
# from a config file, and `//$(reboot)/x` is a valid-looking UNC. (assert_unc_shape
# already rejects that shape; this is the second line of the same defence.)
PROBE_DETAIL=""
probe_source() {   # HOST -> 0 awake / 1 offline / 2 indeterminate
    local host="$1" msg rc=0
    PROBE_DETAIL=""
    # (a) is the PANEL on a network at all? Its own dead Wi-Fi is not evidence
    #     about Mini-serv, and after a resume it is the likeliest explanation.
    if command -v ip >/dev/null 2>&1 && [ -z "$(ip route show default 2>/dev/null)" ]; then
        PROBE_DETAIL="this panel has NO DEFAULT ROUTE — the fault is on THIS side of the LAN, not at $host"
        return 2
    fi
    # (b) does the name resolve? A typo'd or unpublished host name is a
    #     configuration defect, not a sleeping box.
    if ! getent ahostsv4 "$host" >/dev/null 2>&1 && ! getent hosts "$host" >/dev/null 2>&1; then
        PROBE_DETAIL="'$host' does not RESOLVE (no DNS answer and nothing in /etc/hosts) — that is a name or a DNS problem, not a sleeping box"
        return 2
    fi
    # (c) the connect itself. LC_ALL=C so the kernel's error text is stable
    #     enough to classify; `timeout` bounds it, because an unanswered SYN
    #     would otherwise sit in the kernel's TCP retry for over a minute, which
    #     is longer than the every-minute timer's whole period.
    msg="$(LC_ALL=C timeout "$PROBE_TIMEOUT" bash -c 'exec 3<>/dev/tcp/"$1"/"$2"' _ "$host" "$PROBE_PORT" 2>&1)" || rc=$?
    case "$rc" in
        0) return 0 ;;
        124)
            PROBE_DETAIL="nothing answered on ${PROBE_PORT}/tcp within ${PROBE_TIMEOUT}s"
            return 1 ;;
    esac
    case "$msg" in
        *"No route to host"*|*"Host is unreachable"*)
            PROBE_DETAIL="the LAN reports no route to $host (no ARP reply), which is what an OFF or SLEEPING box on this segment looks like"
            return 1 ;;
        *"Connection refused"*)
            PROBE_DETAIL="$host ANSWERED with a connection REFUSAL on ${PROBE_PORT}/tcp. A box that sends an RST is AWAKE — this is Samba stopped, a firewall REJECT, or the wrong host — and it is NOT the designed 'source asleep' state"
            return 2 ;;
        *)
            PROBE_DETAIL="the probe to $host:${PROBE_PORT} failed in a way this script cannot classify (rc=$rc: ${msg:-no message})"
            return 2 ;;
    esac
}

# ── the staleness ladder (OI-18) ─────────────────────────────────────────────
# The frame skip is silent about the FAILURE by design; it must not be silent
# about the CONSEQUENCE. Every successful flow stamps its finish time at the
# CACHE ROOT — deliberately outside the mirrored leaf directory, where
# `rsync --delete` cannot remove it and the manifest walker cannot see it — and
# every skip reports how old the cached content now is.
#
# WHAT THIS DOES NOT DO, ON PURPOSE: it never escalates a skip into a failed
# unit, however old the cache gets, and it never posts to the tracker.
# RULED 2026-08-04 (the Owner): the ladder stays REPORTING-ONLY. "Mini-serv has
# been off for a week" is an allowed state (§4d), and deciding at what age it
# stops being allowed is a ruling nobody has made. So the ladder MEASURES and
# REPORTS; whether the panel should ever alert on it is an open item.
stamp_path() { printf '%s' "$WALL_MEDIA_CACHE/.$SELF-$1.stamp"; }

stamp_flow() {
    local sp; sp="$(stamp_path "$1")"
    if [ -L "$sp" ]; then
        warn "$1: the last-sync stamp path $sp is a SYMLINK; refusing to write through it (that would let anything that can write the cache root choose a file for root to truncate). Remove it."
        return 0
    fi
    date +%s > "$sp" 2>/dev/null \
        || warn "$1: could not write the last-sync stamp at $sp — the staleness ladder will report 'never synced' after the next skip"
    return 0
}

# report_staleness FLOW CACHED_FILES — one journal line saying how old the cache
# is, at WARNING level once it passes WALL_FRAME_STALE_WARN_HOURS.
report_staleness() {
    local flow="$1" files="$2" sp when now age hrs
    sp="$(stamp_path "$flow")"
    if [ ! -f "$sp" ] || [ -L "$sp" ]; then
        warn "$flow: this panel has NEVER completed a $flow sync, and the cache holds $files file(s). If that is not a brand-new panel, the source has been unreachable for the whole life of this install."
        return 0
    fi
    # Read it into a variable and CHECK IT before arithmetic: a truncated or
    # hand-edited stamp would otherwise turn a staleness report into a shell
    # syntax error, i.e. a crash on the one path whose whole job is to stay calm.
    # DIGITS ARE NOT ENOUGH — `08` is all digits and is an invalid OCTAL literal,
    # which aborts `$(( ))` outright (proven on the bench). Length-bound it and
    # force base 10.
    when="$(head -n1 "$sp" 2>/dev/null || true)"
    case "$when" in
        ''|*[!0-9]*)
            warn "$flow: the last-sync stamp at $sp is unreadable ('$when'), so the age of the $files cached file(s) cannot be reported. Delete it; the next successful sync rewrites it."
            return 0 ;;
    esac
    if [ "${#when}" -gt 12 ]; then
        warn "$flow: the last-sync stamp at $sp holds an implausible epoch ('$when'), so the age of the $files cached file(s) cannot be reported. Delete it; the next successful sync rewrites it."
        return 0
    fi
    when=$(( 10#$when ))
    now="$(date +%s)"
    age=$(( now - when ))
    if [ "$age" -lt 0 ]; then
        # A stamp in the FUTURE used to be clamped to zero, i.e. reported as
        # "0h old" forever. It means the clock moved backwards (the panel has no
        # RTC battery worth trusting and NTP may not have landed yet), and
        # pretending the cache is fresh is the exact silent-green this file
        # exists to prevent.
        warn "$flow: the last-sync stamp at $sp is $(( -age ))s in the FUTURE, so the age of the $files cached file(s) is UNKNOWN (the clock moved backwards, or NTP has not landed since boot). Not reporting a freshness that cannot be computed."
        return 0
    fi
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

# ── the credentials file ─────────────────────────────────────────────────────
# "root-only 0600" was CLAIMED by the old error message and never CHECKED: the
# test was `[ -r "$creds" ]`, which root passes on a 0644 file, on a file owned by
# the kiosk user, and on a symlink pointing at one. The file holds a Samba
# password; the claim and the check now agree.
assert_creds_file() {   # VARNAME PATH FLOW
    local var="$1" p="$2" flow="$3" mode owner group
    if [ -L "$p" ]; then
        err "$flow: $var=$p is a SYMLINK. A credentials file must be a regular root-owned file — a symlink lets whoever can create it choose what root reads a password out of."
        return 1
    fi
    if [ ! -f "$p" ]; then
        err "$flow: $var=$p does not exist (it must be a 0600 root:root file with username=/password= lines). A production image installs it from the materialised site payload; if this is a hand-built panel, create it by hand."
        return 1
    fi
    mode="$(stat -c '%a' "$p" 2>/dev/null || printf '?')"
    owner="$(stat -c '%u' "$p" 2>/dev/null || printf '?')"
    group="$(stat -c '%g' "$p" 2>/dev/null || printf '?')"
    if [ "$mode" != "600" ] || [ "$owner" != "0" ] || [ "$group" != "0" ]; then
        err "$flow: $var=$p must be mode 0600 owned root:root; it is mode 0$mode owned $owner:$group. It holds a Samba password and the panel runs an unprivileged kiosk user. Fix: sudo chown root:root '$p' && sudo chmod 0600 '$p'"
        return 1
    fi
    return 0
}

# ── one flow ─────────────────────────────────────────────────────────────────
# Returns 0 on success OR on a DESIGNED skip (frame, source unreachable); 1 on a
# real failure, having already said why. The caller aggregates.
#
# ERREXIT IS NOT IN FORCE INSIDE THIS FUNCTION. It is called as `if ! sync_flow`,
# and bash disables `set -e` for the whole dynamic extent of a command in a
# conditional context (proven on the bench). So EVERY fallible command in here is
# checked EXPLICITLY. That is not belt-and-braces, it is the only guard there is:
# when `from="$(mktemp -d)"` failed silently, `"$from/"` became `/` and the rsync
# below mirrored the panel's root filesystem into the cache.
sync_flow() {
    flow_spec "$1"
    local unc="" creds="" bench="" bench_var="" src="" from="" to="" before="" after="" bytes="" opts=""

    # Per-flow lock. NON-BLOCKING: if the other unit is already mirroring this
    # flow, the honest thing is to say so and let it finish, not to queue a
    # second rsync behind it every minute.
    if ! install -d -m 0700 "$RUNDIR"; then
        err "$FLOW: cannot create the lock directory $RUNDIR — nothing was mirrored. This is a defect on the panel (is /run mounted? is this running as root?), not a source problem."
        return 1
    fi
    if ! exec 9>"$RUNDIR/$FLOW.lock"; then
        err "$FLOW: cannot open the lock file $RUNDIR/$FLOW.lock — nothing was mirrored."
        return 1
    fi
    # LOCK_HELD tracks the DESCRIPTOR, not the lock: fd 9 is open from here on and
    # every exit path below must close it. (Setting it only after a successful
    # flock is how the contention path used to leak the descriptor.)
    LOCK_HELD=1
    # `-E 100` is what separates CONTENTION from ERROR. Without it every flock
    # failure returned 1 and was read as "another run has it", so a bad file
    # descriptor (rc 65, proven on the bench) or an I/O error ended the run as
    # `sync complete — nothing to do`: a green unit that mirrored nothing.
    local lrc=0
    flock -n -E 100 9 || lrc=$?
    case "$lrc" in
        0) ;;
        100)
            release_lock
            log "$FLOW: another wall-sync run is already mirroring this flow — skipping this one (nothing is queued; the running one does the work)."
            return 0 ;;
        *)
            release_lock
            err "$FLOW: flock FAILED on $RUNDIR/$FLOW.lock with rc=$lrc. That is an ERROR, not contention (contention is rc 100) — nothing was mirrored and nothing was deleted."
            return 1 ;;
    esac

    to="$WALL_MEDIA_CACHE/$F_LEAF"
    if [ -L "$to" ]; then
        err "$FLOW: the cache leaf $to is a SYMLINK. It is an rsync --delete destination; refusing to copy into — and delete through — a redirected path."
        return 1
    fi
    if ! install -d -m 0755 "$to"; then
        err "$FLOW: cannot create the cache directory $to — nothing was mirrored."
        return 1
    fi
    before="$(count_media "$to" "$F_MANIFEST")"

    # ── source: the bench fixture, or the cifs mount ─────────────────────────
    # Indirect expansion (`${!VAR}`), never `eval`: the flow map names the knob,
    # and the knob's VALUE comes from a config file. `eval` would make a value of
    # `$(reboot)` executable; `${!…}` cannot execute anything.
    bench_var="BENCH_$FLOW"
    bench="${!bench_var:-}"
    if [ -n "$bench" ]; then
        src="$bench"
        log "$FLOW: BENCH FIXTURE ACTIVE: --bench-source $FLOW=$src — no cifs mount is performed;"
        log "$FLOW: BENCH FIXTURE ACTIVE: the mirror, the guards and the manifests are the real code path."
    else
        unc="${!F_UNC_VAR:-}"
        # GUARDED ON F_CREDS_VAR BEING NON-EMPTY, not merely on the value it
        # names: the music flow has no credentials KNOB at all, and `${!x}` with
        # an empty x is a hard "invalid variable name" error under `set -u`, not
        # an empty string. `creds` then stays "" for the guest flow, which is
        # what the F_AUTH branches below expect.
        creds=""
        [ -z "$F_CREDS_VAR" ] || creds="${!F_CREDS_VAR:-}"

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
        esac
        # Shape was validated once at config time; re-assert here so a future
        # caller cannot reach the mount without it.
        assert_unc_shape "$F_UNC_VAR" "$unc"

        # WHERE A FLOW AUTHENTICATES AT ALL, A ROOT-ONLY CREDENTIALS FILE IS THE
        # ONLY SUPPORTED FORM (changed by OI-18, 2026-08-03). The inline
        # MEDIA_CIFS_USER/MEDIA_CIFS_PASS fallback is retired: one inline pair
        # cannot serve two hosts, and duplicating it per flow would have added
        # four knobs whose only purpose is to put a password somewhere less safe
        # than the file that already exists.
        #
        # `guest` is NOT a fallback for a missing credential — it is the music
        # flow's declared auth mode, fixed in flow_spec() and not configurable.
        # A flow cannot silently degrade into it: F_AUTH is set in code per
        # flow, so an unreadable frame credential still fails the frame mount
        # rather than retrying anonymously against Mini-serv.
        if [ "$F_AUTH" = "credentials" ]; then
            if [ -z "$creds" ]; then
                err "$F_CREDS_VAR is not set in $ENV_FILE — a root-only credentials file is the ONLY supported way to authenticate a $FLOW mount (the inline user/pass fallback was retired by OI-18). Set it to the 0600 file the image installed, e.g. /etc/wall-panel/cifs-$FLOW.creds"
                return 1
            fi
            assert_creds_file "$F_CREDS_VAR" "$creds" "$FLOW" || return 1
        fi

        wait_for_network

        # THE FAILURE-POLICY FORK, and the only place the two flows differ in
        # kind rather than in value.
        if [ "$F_POLICY" = "skip-if-unreachable" ]; then
            local prc=0
            probe_source "$(unc_host "$unc")" || prc=$?
            case "$prc" in
                1)
                    log "$FLOW: $(unc_host "$unc") — $PROBE_DETAIL. The source is ASLEEP or off. Skipping, and NOT waking it (no magic packet is sent for this flow): that is the ruling (storage-map §4d, A0/A10), not a failure."
                    report_staleness "$FLOW" "$before"
                    return 0 ;;
                2)
                    warn "$FLOW: reachability of $(unc_host "$unc") is INDETERMINATE — $PROBE_DETAIL. Skipping this run WITHOUT claiming the source is asleep, because that has not been established; the frame flow never fails the unit on reachability (ruled 2026-08-04), so this is reported, not escalated. If it persists, this is worth investigating: a wrong credential behind a permanently-failing probe would never be attempted, and so never reported."
                    report_staleness "$FLOW" "$before"
                    return 0 ;;
            esac
        fi

        # EVERY MOUNT OPTION IS BUILT HERE, IN CODE. The only knob is the SMB
        # dialect, and it is an enum validated at startup. `ro` is the mirror's
        # read-only guarantee, the auth clause comes from F_AUTH (the flow's OWN
        # credentials file, or `guest` for the anonymous HOMEHUB share), and
        # nosuid/nodev/noexec are free on a media share.
        #
        # `guest` sends a null username and password. It is not the same as
        # omitting the clause: without it mount.cifs would PROMPT, which under
        # systemd means the unit hangs until F_MOUNT_TIMEOUT rather than failing
        # with a reason.
        if [ "$F_AUTH" = "guest" ]; then
            opts="guest,ro,nosuid,nodev,noexec,iocharset=utf8,vers=${MEDIA_CIFS_VERS}"
        else
            opts="credentials=${creds},ro,nosuid,nodev,noexec,iocharset=utf8,vers=${MEDIA_CIFS_VERS}"
        fi
        MP="$RUNDIR/$FLOW"
        if ! install -d -m 0700 "$MP"; then
            err "$FLOW: cannot create the mountpoint $MP — nothing was mirrored."
            MP=""
            return 1
        fi
        log "$FLOW: mount cifs $unc -> $MP (ro, vers=$MEDIA_CIFS_VERS)"
        if ! timeout "$F_MOUNT_TIMEOUT" mount -t cifs "$unc" "$MP" -o "$opts"; then
            if [ "$F_POLICY" = "skip-if-unreachable" ]; then
                # REACHABLE and REFUSED. Not a sleeping box — the probe above
                # just proved something is listening — so this is a wrong share
                # name or a wrong credential, and it is exactly the class of
                # fault a silent skip would hide forever. NOTE the credential is
                # the MINI-SERV `share` account. It is now the ONLY Samba
                # credential the panel holds — the HOMEHUB account that was also
                # called `share` was retired 2026-08-05 when //homehub/Media
                # went anonymous (storage-map Q-S7).
                err "$FLOW: cifs mount REFUSED by $unc, WHICH IS AWAKE (it answered on ${PROBE_PORT}/tcp moments ago). This is NOT the designed 'source asleep' state: the share name is wrong, or the credentials in $creds are (that file must hold the MINI-SERV 'share' account). Nothing was touched in $to."
            else
                # The music flow presents NO credential, so a refusal here is
                # never a wrong password — it is the share, the box, or guest
                # access having been turned off on the hub.
                err "$FLOW: cifs mount REFUSED: $unc ($F_SOURCE). This mount is ANONYMOUS — no credential is involved, so this is not a password fault. Either the source box or its Samba service is down, the share name is wrong, or //homehub/Media has stopped accepting guests (check 'guest ok = yes' on the share and 'map to guest' in the hub's smb.conf [global] — storage-map Q-S7 requires both). Nothing was touched in $to — the existing cache is still whatever the last good sync left."
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
        # rm branch: one deletion mechanism, one thing to reason about. If mktemp
        # fails this MUST abort: an empty `$from` makes `"$from/"` the string `/`
        # and the rsync below would mirror the panel's root filesystem.
        if ! from="$(mktemp -d)" || [ -z "$from" ] || [ ! -d "$from" ]; then
            err "$FLOW: mktemp -d failed, so there is no empty directory to mirror from. REFUSING to continue — an unset source path here would make rsync's source the filesystem ROOT. Nothing was touched in $to."
            return 1
        fi
        TMPDIRS="$TMPDIRS $from"
        if [ "$before" -gt 0 ]; then
            warn "$FLOW: $F_SUBDIR absent from the source and WALL_SYNC_ALLOW_EMPTY=true — CLEARING $before cached file(s) from $to"
        else
            warn "$FLOW: $F_SUBDIR absent from the source and the cache is empty — nothing to mirror. The $F_LEAF manifest will describe an empty library, which the shell reports as 'unavailable' (WSN-011), not as an error."
        fi
    # GUARD 2 — the subtree exists but holds nothing rsync would copy (the ingest
    # step's exact lesson, plus the exclusion lesson: a share can mount, and be a
    # directory, and hold only the one file this run then excludes).
    elif ! source_has_syncable_entries "$from" "$F_MANIFEST"; then
        if [ "$before" -gt 0 ] && [ "$WALL_SYNC_ALLOW_EMPTY" != "true" ]; then
            err "$FLOW: $from exists but contains NOTHING rsync would copy (only excluded entries, if any), while the cache holds $before file(s) at $to — REFUSING to mirror-delete them (--delete is irreversible from here). Set WALL_SYNC_ALLOW_EMPTY=true only if that subtree is legitimately empty."
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
    # --exclude /.wall-manifest.*: the manifest writer's own temp files. It
    # creates them with O_EXCL|O_NOFOLLOW under a random name, so a hostile source
    # cannot pre-place one; excluding them keeps a crashed run's leftover out of
    # the next mirror instead of shipping it to the shell.
    # --timeout=$F_RSYNC_TIMEOUT via `timeout`: a per-flow budget, so a wedged
    # music mirror cannot consume the unit's whole TimeoutStartSec and leave the
    # frame flow unrun.
    if ! timeout "$F_RSYNC_TIMEOUT" rsync -a --delete --chmod=D755,F644 \
            --exclude "/$F_MANIFEST" --exclude "/$MANIFEST_TMP_PREFIX*" "$from/" "$to/"; then
        err "$FLOW: rsync mirror FAILED or exceeded its ${F_RSYNC_TIMEOUT}s budget ($from/ -> $to/). The cache is now in an unknown, partially-mirrored state; the $F_LEAF manifest was NOT regenerated, so the shell keeps whatever the last complete sync described."
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
    # dependency on the source box staying up. THE LOCK IS NOT RELEASED HERE:
    # the manifest walk and the stamp below must see a cache no other run can be
    # mutating (see flow_cleanup).
    if ! release_mount; then
        err "$FLOW: the mirror finished but $MP could NOT be unmounted. Reporting this run as FAILED rather than green: a left-behind cifs mount pins a dead server connection and blocks the next run's mountpoint. Check: mount | grep $MP ; fuser -vm $MP"
        return 1
    fi

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
if [ -L "$WALL_MEDIA_CACHE" ]; then
    die "WALL_MEDIA_CACHE='$WALL_MEDIA_CACHE' is a SYMLINK. It is an rsync --delete destination and must be a real directory."
fi
install -d -m 0755 "$WALL_MEDIA_CACHE"

if [ "$BENCH_USED" = 1 ]; then
    warn "BENCH MODE: at least one flow is mirroring from a fixture under $BENCH_ROOT instead of mounting its share. This run is NOT a real pull."
fi

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
    # Teardown is judged too: a mount this run could not release is a failure,
    # not a warning (it blocks the next run and pins a dead connection).
    if ! flow_cleanup; then RC=1; fi
done

if [ "$RC" -ne 0 ]; then
    log "sync FINISHED WITH FAILURES — ${SUMMARY:-nothing completed}. See the ERROR line(s) above; each flow's verdict is independent."
    exit 1
fi
log "sync complete — ${SUMMARY:-nothing to do}"
log "on demand, any time: sudo systemctl start wall-sync.service   (both flows)"
