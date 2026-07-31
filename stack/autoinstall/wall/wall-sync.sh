#!/usr/bin/env bash
# wall-sync.sh — THE PANEL PULLS ITS MEDIA. (OI-15, ruled by the Owner 2026-07-29.)
#
# The ruling in one sentence: the panel's media is a network share on the AWOW,
# the PANEL pulls from it — once after boot and on demand — with MIRROR
# semantics, and `/media/*` is then served PANEL-LOCALLY by the shell's Electron
# host (OfficeWallNaglight's side, not this repo's). Nothing streams; the kiosk
# site on the AWOW serves no `/media` route at all.
#
# WHAT ONE RUN DOES
#   1. mount MEDIA_SHARE_UNC read-only over cifs;
#   2. `rsync -a --delete` ONLY the `Music/` and `FrameVideos/` subtrees into
#      WALL_MEDIA_CACHE/{music,frame};
#   3. unmount (always — an EXIT trap, so a failure never leaves a mount behind);
#   4. POST-STEP: regenerate the shell's two contracts inside the cache —
#        music/index.json    LocalLibraryProvider's manifest (js/music/local.js)
#        frame/playlist.json the frame playlist ([{url,title}, …])
#      written LAST and atomically, after the media they describe, exactly as
#      that contract demands.
#
# MIRROR SEMANTICS — READ THIS ONCE: `--delete`. Content REMOVED from the LAN
# source DISAPPEARS from the panel cache on the next sync. That is the ruling.
# It is also the reason for the guards below: `--delete` is irreversible, and the
# panel is the wrong place to discover that a share came up empty.
#
# WHEN IT RUNS
#   * once after boot   — wall-sync.service, After=network-online.target;
#   * ON EVERY RESUME   — wall-sync-resume.service (OI-16a, ruled 2026-07-29):
#     with SLEEP_MODE=suspend the panel resumes every morning WITHOUT booting,
#     so a resume triggers the same unit, detached (`systemctl start --no-block`)
#     so the wake is never delayed;
#   * ON DEMAND         — `sudo systemctl start wall-sync.service`. That IS the
#     dedicated SSH-invocable command the ruling asks for; there is nothing else
#     to invoke and no second entry point to keep in step.
# There is deliberately NO periodic timer (the ruling is boot + resume + on
# demand). The resume path has one wrinkle the boot path does not: Wi-Fi
# re-association takes a few seconds after wake and network-online.target is NOT
# re-evaluated on resume, so this script does its own short bounded wait (below)
# before touching the network. If the network still is not up, the mount fails
# loudly — a failed unit, not a silent skip — and the retry is the on-demand
# command.
#
# GUARDS — the backup service's INGEST step learned these the expensive way
# (stack/backup/backup.sh §1b); they are transplanted rather than reinvented:
#   * an EMPTY or ABSENT source subtree does NOT get to mirror-delete a populated
#     cache. The run REFUSES, loudly, unless WALL_SYNC_ALLOW_EMPTY=true.
#   * a refused mount, a failed rsync, a missing python3 and a failed manifest
#     write are all FATAL and nonzero. The panel has no NagLight feed of its own
#     (the shell pushes its own heartbeat), so "loud" here means a FAILED UNIT
#     plus journal lines: `journalctl -u wall-sync`. A silently stale cache is
#     precisely the never-silent-green failure this repo forbids.
#   * file counts are logged before and after every subtree, so a mirror-delete
#     is visible in the journal as a number that went down.
#
# The source is the AWOW, which is always-on, so — unlike the backup service's
# ingest — there is no Wake-on-LAN pre-step here. A refused mount means the AWOW
# or its Samba service is down, which is its own alert, not something the panel
# can fix.
set -euo pipefail

ENV_FILE="/etc/wall-panel/wall.env"
PAYLOAD="/opt/wall-panel/stack/autoinstall/wall"
SELF="wall-sync"
log()  { echo "[$SELF] $*"; }
warn() { echo "[$SELF] WARNING: $*" >&2; }
die()  { echo "[$SELF] ERROR: $*" >&2; exit 1; }

# The subtree map — `SOURCE_SUBDIR:CACHE_SUBDIR:MANIFEST_FILE`, and NOT a knob.
# The ruling says only `Music` and `FrameVideos`; widening that is a decision, and
# a knob would let a typo widen it silently onto a 2016 laptop's 256 GB disk. The
# cache leaf names and the manifest filenames are fixed by the shell's defaults
# (`/media/music/index.json`, `/media/frame/playlist.json`), so they are not knobs
# either. The manifest name is carried here because the mirror has to know about
# it twice: to keep `--delete` off it, and to keep it out of the file counts.
SUBTREES="Music:music:index.json FrameVideos:frame:playlist.json"

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

case "$WALL_MEDIA_CACHE" in
    /?*) ;;
    *) die "WALL_MEDIA_CACHE='$WALL_MEDIA_CACHE' must be an ABSOLUTE path (it is an rsync --delete destination — a relative path would resolve against whatever cwd systemd handed us)" ;;
esac

for tool in rsync python3; do
    command -v "$tool" >/dev/null 2>&1 \
        || die "$tool is not installed — the sync cannot run (both are in the wall image's package list; a hand-trimmed panel needs: apt-get install rsync python3)"
done

# ── source: the cifs share, or the bench/test override ───────────────────────
# MEDIA_SOURCE_OVERRIDE is a SUPPORTED bench hook, not a debug leftover: it names
# a LOCAL directory to mirror from instead of mounting anything, which is what
# lets the whole mirror + guard + manifest path be exercised on a machine with no
# Samba host (the WSL harness does exactly this). Everything after the mount is
# the real code path — the hook replaces the mount, not the logic — and it
# announces itself loudly so a run in the journal can never be mistaken for a
# real pull.
SRC=""
MP="/run/$SELF/source"
MOUNTED=0
TMPDIRS=""

cleanup() {
    if [ "$MOUNTED" = 1 ]; then
        umount "$MP" 2>/dev/null || warn "could not unmount $MP — check: mount | grep $MP"
        MOUNTED=0
    fi
    local d
    for d in $TMPDIRS; do [ -d "$d" ] && rmdir "$d" 2>/dev/null || true; done
    return 0
}
trap cleanup EXIT

if [ -n "${MEDIA_SOURCE_OVERRIDE:-}" ]; then
    [ -d "$MEDIA_SOURCE_OVERRIDE" ] \
        || die "MEDIA_SOURCE_OVERRIDE='$MEDIA_SOURCE_OVERRIDE' is not a directory"
    SRC="$MEDIA_SOURCE_OVERRIDE"
    log "BENCH HOOK ACTIVE: MEDIA_SOURCE_OVERRIDE=$SRC — no cifs mount is performed;"
    log "BENCH HOOK ACTIVE: the mirror, the guards and the manifests are the real code path."
else
    case "${MEDIA_SHARE_UNC:-}" in
        '')
            die "MEDIA_SHARE_UNC is not set in $ENV_FILE — the panel has no media source, so there is nothing to sync. Set it (//host/share) and re-run: sudo systemctl start wall-sync.service" ;;
        *REPLACE_WITH*)
            die "MEDIA_SHARE_UNC is still the shipped placeholder ('$MEDIA_SHARE_UNC') in $ENV_FILE. A freshly imaged panel therefore shows wall-sync.service FAILED until the real share is filled in — that is deliberate: an empty wall with a green unit would be a lie. Fill it in and re-run: sudo systemctl start wall-sync.service" ;;
        //?*/?*) ;;
        *)
            die "MEDIA_SHARE_UNC='$MEDIA_SHARE_UNC' is not a //host/share UNC" ;;
    esac

    # Short BOUNDED network wait — for the resume path (OI-16a). After a wake
    # from S3 the Wi-Fi radio takes a few seconds to re-associate, and
    # network-online.target (reached at boot) is NOT re-evaluated on resume, so
    # unit ordering cannot cover this. `nm-online` exits 0 the moment
    # NetworkManager reports a connection (immediately when already up — boot and
    # on-demand runs lose nothing) and nonzero at the 30 s cap. NON-FATAL either
    # way: the mount below is the real arbiter and fails loudly on its own; this
    # wait just stops the common case (a healthy Wi-Fi that needs 3 seconds) from
    # burning the run. Only on the real-mount path — the bench hook touches no
    # network.
    if command -v nm-online >/dev/null 2>&1; then
        nm-online -q --timeout=30 \
            || warn "network still not up after 30s (nm-online) — trying the mount anyway; if it fails, re-run once the panel is back on Wi-Fi: sudo systemctl start wall-sync.service"
    else
        warn "nm-online not found (network-manager is in the wall image) — skipping the post-resume network wait; the mount will be the arbiter"
    fi

    # Same option shape and credential precedence as the backup service's
    # mount_cifs (stack/backup/common.sh): a root-only credentials FILE is the
    # documented choice, inline user/pass the less-safe fallback, and
    # MEDIA_CIFS_EXTRA carries the protocol version.
    opts="ro,iocharset=utf8,${MEDIA_CIFS_EXTRA:-vers=3.0}"
    if [ -n "${MEDIA_CIFS_CREDENTIALS:-}" ]; then
        [ -r "$MEDIA_CIFS_CREDENTIALS" ] \
            || die "MEDIA_CIFS_CREDENTIALS=$MEDIA_CIFS_CREDENTIALS is not readable (it must exist and be root-only 0600, with username=/password= lines)"
        opts="credentials=${MEDIA_CIFS_CREDENTIALS},${opts}"
    else
        warn "no MEDIA_CIFS_CREDENTIALS file — falling back to inline MEDIA_CIFS_USER/MEDIA_CIFS_PASS (less safe: the password is then in wall.env and in this process's mount options)"
        opts="username=${MEDIA_CIFS_USER:-guest},password=${MEDIA_CIFS_PASS:-},${opts}"
    fi
    install -d -m 0700 "$MP"
    log "mount cifs $MEDIA_SHARE_UNC -> $MP (ro)"
    mount -t cifs "$MEDIA_SHARE_UNC" "$MP" -o "$opts" \
        || die "cifs mount REFUSED: $MEDIA_SHARE_UNC (the source box or its Samba service is down, the share name is wrong, or the credentials are). Nothing was touched in $WALL_MEDIA_CACHE — the existing cache is still whatever the last good sync left."
    MOUNTED=1
    SRC="$MP"
fi

# ── helpers ──────────────────────────────────────────────────────────────────
# dir_has_files DIR : 0 iff DIR holds at least one regular file at any depth.
# Same helper, same reason as the ingest guard: "mounted" is not "has data".
dir_has_files() { [ -n "$(find "$1" -type f -print -quit 2>/dev/null)" ]; }
# count_media DIR MANIFEST : regular files in DIR, EXCLUDING the manifest this
# script writes there. Every count logged below is a MEDIA count — counting our
# own manifest would report a spurious deletion on every single run (found by the
# WSL harness, 2026-07-29: a no-op re-run claimed "1 file(s) were DELETED").
count_media() { find "$1" -type f ! -name "$2" 2>/dev/null | wc -l | tr -d ' '; }

install -d -m 0755 "$WALL_MEDIA_CACHE"

# ── the mirror, one subtree at a time ────────────────────────────────────────
SUMMARY=""
for pair in $SUBTREES; do
    IFS=: read -r sub leaf mf <<< "$pair"
    from="$SRC/$sub"
    to="$WALL_MEDIA_CACHE/$leaf"
    install -d -m 0755 "$to"
    before="$(count_media "$to" "$mf")"

    # GUARD 1 — the source subtree is missing entirely. A wrong share name, or a
    # source that came up without its data volume, looks exactly like "the
    # library no longer has any music".
    if [ ! -d "$from" ]; then
        if [ "$before" -gt 0 ] && [ "$WALL_SYNC_ALLOW_EMPTY" != "true" ]; then
            die "$sub: the source has no $sub/ directory AT ALL, while the cache holds $before file(s) at $to — REFUSING to mirror-delete them. Check the share (is $sub/ really at the root of ${MEDIA_SHARE_UNC:-$SRC}?). If the library genuinely has no $sub, set WALL_SYNC_ALLOW_EMPTY=true for one run."
        fi
        # Mirror the emptiness through the SAME rsync path rather than a special
        # rm branch: one deletion mechanism, one thing to reason about.
        from="$(mktemp -d)"
        TMPDIRS="$TMPDIRS $from"
        if [ "$before" -gt 0 ]; then
            warn "$sub: absent from the source and WALL_SYNC_ALLOW_EMPTY=true — CLEARING $before cached file(s) from $to"
        else
            warn "$sub: absent from the source and the cache is empty — nothing to mirror. The $leaf manifest will describe an empty library, which the shell reports as 'unavailable' (WSN-011), not as an error."
        fi
    # GUARD 2 — the subtree exists but holds no files (the ingest step's exact
    # lesson: a share can mount, and be a directory, and still be empty).
    elif ! dir_has_files "$from"; then
        if [ "$before" -gt 0 ] && [ "$WALL_SYNC_ALLOW_EMPTY" != "true" ]; then
            die "$sub: $from exists but contains NO files, while the cache holds $before file(s) at $to — REFUSING to mirror-delete them (--delete is irreversible from here). Set WALL_SYNC_ALLOW_EMPTY=true only if that subtree is legitimately empty."
        fi
        [ "$before" -gt 0 ] \
            && warn "$sub: source is empty and WALL_SYNC_ALLOW_EMPTY=true — mirroring the emptiness ($before cached file(s) WILL be deleted)"
    fi

    log "$sub: mirror $from/ -> $to/ (rsync -a --delete — source deletions PROPAGATE)"
    # --chmod: the cache is read by the UNPRIVILEGED kiosk user, while the sync
    # runs as root from a cifs mount whose reported modes come from the mount
    # options rather than from the files. Normalising them here is what keeps the
    # shell able to read what was just synced.
    # --exclude /$mf: our own manifest lives INSIDE the mirrored directory, so
    # `--delete` would otherwise remove it on every run (leaving the shell briefly
    # with no library at all) and a source that happens to contain a file of that
    # name would overwrite it. Anchored with a leading `/` so it protects only the
    # top-level manifest, not a same-named file inside an album folder.
    rsync -a --delete --chmod=D755,F644 --exclude "/$mf" "$from/" "$to/" \
        || die "$sub: rsync mirror FAILED ($from/ -> $to/). The cache is now in an unknown, partially-mirrored state; the manifests were NOT regenerated, so the shell keeps whatever the last complete sync described."
    after="$(count_media "$to" "$mf")"
    bytes="$(du -sb "$to" 2>/dev/null | awk '{print $1}')"
    if [ "$after" -lt "$before" ]; then
        log "$sub: cache is now $after file(s), ${bytes:-?} byte(s) at $to — $(( before - after )) file(s) were DELETED to match the source (mirror semantics)"
    else
        log "$sub: cache is now $after file(s), ${bytes:-?} byte(s) at $to (was $before)"
    fi
    SUMMARY="${SUMMARY:+$SUMMARY; }$leaf($after files)"
done

# Unmount as soon as the copying is done — the manifest step reads only the local
# cache, and holding a cifs mount open for it would be a needless dependency on
# the source box staying up.
cleanup

# ── POST-STEP: the shell's two contracts ─────────────────────────────────────
# Generated from the cache that was just written, never from the share: the
# manifest must describe what the panel actually HAS. Both files are written
# atomically (temp + rename) INSIDE the directories the mirror just refreshed, and
# AFTER it — the contract's ordering rule. The mirror leaves the previous manifest
# in place (the `--exclude` above), so the window in which the shell could fetch a
# manifest that disagrees with the media is one rename wide, and a run that dies
# before this point leaves the last complete manifest rather than nothing.
GEN=""
for cand in "$(dirname "$0")/wall-media-manifest.py" "$PAYLOAD/wall-media-manifest.py"; do
    [ -f "$cand" ] && { GEN="$cand"; break; }
done
[ -n "$GEN" ] \
    || die "wall-media-manifest.py not found next to $0 or in $PAYLOAD — the media is synced but the shell has no manifest to read, so it would show no music at all"

log "manifests: generating music/index.json + frame/playlist.json with $GEN"
python3 "$GEN" --cache "$WALL_MEDIA_CACHE" \
    || die "manifest generation FAILED — the media is in place but the shell cannot see it (an absent manifest makes the local library report 'unavailable'). Re-run after fixing: sudo systemctl start wall-sync.service"

log "sync complete — $SUMMARY"
log "on demand, any time: sudo systemctl start wall-sync.service"
