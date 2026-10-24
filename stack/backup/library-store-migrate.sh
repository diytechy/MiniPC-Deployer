#!/usr/bin/env bash
# library-store-migrate.sh — move the library backup's store into the ONE-MOUNT
# layout a FileBackup image built from its bounded-run-loop work needs.
# RENAMES ONLY. It never copies and never deletes anything.
#
# WHY. The bounded engine (FileBackup branch BoundedRebuild) moves content
# between the latest state and the snapshots by rename only, and refuses (exit
# 2) a BackupPath/ChangePath pair a rename cannot cross. Linux cannot rename
# between two bind mounts even on one filesystem, so the old /backup + /changes
# mounts must become two siblings under ONE folder, bound at /store:
#
#     old destination   $FILEBACKUP_BACKUP   (default /mnt/backup-drive/library)
#                         -> $FILEBACKUP_STORE/backup
#     old change root   $FILEBACKUP_CHANGES  (default /mnt/backup-drive/library-changes)
#                         -> $FILEBACKUP_STORE/changes
#     (FILEBACKUP_STORE defaults to /mnt/backup-drive/library-store)
#
# On one filesystem each is a single rename(2): instant, whatever the size, and
# atomic, so a kill leaves every folder whole in one place or the other.
#
# WHAT IT REFUSES (exit 2, nothing changed), because each needs a person:
#   * an old folder and its new place on DIFFERENT filesystems or mounts. Moving
#     there would be a COPY of a multi-TB store; that is a manual, supervised
#     step and this script will not do it (README "Migrating an existing store").
#   * an old folder and its new place BOTH holding data: two stores, and only a
#     person can say which is real.
#   * an old path that is a symlink or not a directory.
#   * a library backup running (its lock is held), or a FileBackup container up,
#     or no way to tell.
#
# WHAT IT LEAVES ALONE, on purpose:
#   * the old source-side record ($FILEBACKUP_STATE/MANIFEST.csv, default under
#     /var/lib/homehub-filebackup/state). Delete it by hand after the first
#     successful run of the new image.
#   * an old engine's Temp folder inside the change root. It moves with the
#     folder; the new engine warns about it and leaves it alone, because it may
#     hold the only copy of superseded content.
#   * an EMPTY old folder: nothing to migrate. The wrapper names it; rmdir it.
#
# IDEMPOTENT: a second run, or a run after a kill between the two renames, does
# only what is left and says "nothing to do" when nothing is.
#
# Usage: library-store-migrate.sh [--config PATH] [--apply]
#   --config  backup.env (default /etc/homehub-backup/backup.env, else the
#             backup.env beside this script). The paths come from stack/.env,
#             the file docker compose reads, exactly as library-backup.sh does.
#   --apply   do the renames. Without it the script prints the plan, checks
#             every refusal above, and changes nothing.
# Exit: 0 migrated, nothing to do, or (without --apply) a plan that can run;
#       1 a step failed part-way (re-run it: it carries on); 2 refused.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"
STACK_DIR="$(dirname "$HERE")"          # /opt/homehub/stack on the box

CONFIG=""; APPLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --config) CONFIG="${2:?--config needs a path}"; shift 2 ;;
        --apply)  APPLY=1; shift ;;
        -h|--help) sed -n '2,50p' "$0"; exit 0 ;;
        *) printf 'unknown argument: %s (try --help)\n' "$1" >&2; exit 2 ;;
    esac
done

# refuse MSG… : nothing has been changed and a person must act. Exit 2, the
# same "do not retry unchanged" meaning FileBackup gives it.
refuse() {
    log "REFUSING TO MIGRATE: $1"
    shift
    local line
    for line in "$@"; do log "  $line"; done
    log "Nothing was changed."
    exit 2
}

if [ -z "$CONFIG" ]; then
    if   [ -f /etc/homehub-backup/backup.env ]; then CONFIG=/etc/homehub-backup/backup.env
    elif [ -f "$HERE/backup.env" ];             then CONFIG="$HERE/backup.env"
    fi
fi
# The same two files, in the same order, as library-backup.sh: load_env_file
# only exists once load_config has run (see the note there). backup.env only
# matters here for LIBRARY_BACKUP_LOCK, so a box without one still migrates.
if [ -z "$CONFIG" ]; then
    log "no backup.env found; using stack/.env and the documented defaults"
    CONFIG="$(mktemp)" || { log "cannot create a temporary file"; exit 1; }
    load_config "$CONFIG"
    rm -f "$CONFIG"
else
    load_config "$CONFIG"
fi
load_env_file "$STACK_DIR/.env"
filebackup_store_layout

for _t in flock findmnt python3 stat; do
    command -v "$_t" >/dev/null 2>&1 \
        || refuse "$_t is not on this box." \
                  "flock and findmnt ship in util-linux; python3 does the rename, because it calls rename(2)" \
                  "and NEVER falls back to copy-and-delete the way mv does across filesystems."
done

# ── the backup must not be running ───────────────────────────────────────────
# The wrapper's own lock first: held means a run is in progress (possibly a
# multi-day one). Then the daemon, because a `compose run` container outlives a
# killed wrapper (library-backup.sh, reconcile). Fail CLOSED on "cannot tell".
LOCK_FILE="${LIBRARY_BACKUP_LOCK:-/run/lock/homehub-library-backup.lock}"
mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
exec 9>"$LOCK_FILE" || refuse "cannot open the library backup lock $LOCK_FILE"
flock -n 9 \
    || refuse "a library backup is running (lock held: $LOCK_FILE)." \
              "Wait for it to finish (journalctl -u homehub-library-backup.service -f), then run this again."
fb_containers=""
if ! fb_containers="$(docker ps --filter "label=com.docker.compose.service=filebackup" --format '{{.ID}}' 2>/dev/null)"; then
    refuse "cannot ask docker whether a FileBackup container is running, so cannot tell that the store is idle."
fi
[ -z "$fb_containers" ] \
    || refuse "a FileBackup container is running ($fb_containers) — a manual verify or prune, or a backup whose wrapper died." \
              "Wait for it to exit (docker ps), then run this again."

# ── the plan, decided for each pair before anything moves ─────────────────────
# mount_target PATH : the mount PATH lives on, asked of the nearest ancestor
# that exists (the new place usually does not exist yet). Empty if unknown.
nearest_existing() {
    local p="$1"
    while [ ! -e "$p" ] && [ "$p" != "/" ]; do p="$(dirname "$p")"; done
    printf '%s' "$p"
}
mount_target() { findmnt -n -o TARGET --target "$(nearest_existing "$1")" 2>/dev/null | tail -n1 || true; }
device_of()    { stat -c %d "$(nearest_existing "$1")" 2>/dev/null || true; }

# classify OLD NEW : echo one word for the pair, and say why.
#   none     neither exists: a fresh box (or a store that never ran)
#   done     already migrated, or old and new are one path
#   leftover old is an EMPTY folder: nothing to move (the wrapper names it)
#   move     old holds data, new is absent or empty: one rename
#   conflict anything that needs a person (printed by the caller)
classify() {
    local old="$1" new="$2"
    if [ "$old" = "$new" ]; then echo "done"; return; fi
    if [ -L "$old" ]; then echo conflict; return; fi
    if [ ! -e "$old" ]; then
        if [ -e "$new" ]; then echo "done"; else echo none; fi
        return
    fi
    if [ ! -d "$old" ]; then echo conflict; return; fi
    if ! dir_has_entries "$old"; then echo leftover; return; fi
    if [ -L "$new" ] || { [ -e "$new" ] && [ ! -d "$new" ]; }; then echo conflict; return; fi
    if [ -d "$new" ] && dir_has_entries "$new"; then echo conflict; return; fi
    echo move
}

PAIRS_OLD=("$FB_LEGACY_BACKUP" "$FB_LEGACY_CHANGES")
PAIRS_NEW=("$FB_BACKUP" "$FB_CHANGES")
PAIRS_WHAT=("latest state (backup root)" "snapshot history (change root)")
STATES=()
log "== library store migration $([ "$APPLY" = 1 ] && echo '(APPLY)' || echo '(plan only; pass --apply to do it)') =="
log "store: $FB_STORE  (bound at /store; backup/ and changes/ inside it)"
for i in 0 1; do
    old="${PAIRS_OLD[$i]}"; new="${PAIRS_NEW[$i]}"
    state="$(classify "$old" "$new")"
    STATES+=("$state")
    case "$state" in
        none)     log "  ${PAIRS_WHAT[$i]}: nothing at $old and nothing at $new — nothing to migrate" ;;
        done)     log "  ${PAIRS_WHAT[$i]}: already at $new" ;;
        leftover) log "  ${PAIRS_WHAT[$i]}: $old is EMPTY — nothing to move; remove it by hand (rmdir $old)" ;;
        move)     log "  ${PAIRS_WHAT[$i]}: rename $old -> $new" ;;
        conflict)
            if [ -L "$old" ]; then
                refuse "$old is a symlink. Renaming it would move the link, not the store it points at." \
                       "Resolve it by hand: put the real folder at $old, or at $new."
            elif [ -e "$old" ] && [ ! -d "$old" ]; then
                refuse "$old exists but is not a directory, so it cannot be the old ${PAIRS_WHAT[$i]}."
            else
                refuse "both $old and $new hold data — two stores, and only a person can say which is the real one." \
                       "Nothing here merges or overwrites. Inventory each (ls; the newest MANIFEST.csv; Snapshot_* names)," \
                       "move the one you do not want somewhere safe by hand, and run this again."
            fi ;;
    esac
done

# ── one filesystem and one mount, or no rename at all ─────────────────────────
# Checked for every move before the first one, so a refusal changes nothing.
# Two tests, because each catches what the other cannot: a different DEVICE is
# a different filesystem, and a different MOUNT on the same device is a bind
# mount that rename(2) refuses all the same (EXDEV).
store_parent_mount="$(mount_target "$FB_STORE")"
for i in 0 1; do
    [ "${STATES[$i]}" = move ] || continue
    old="${PAIRS_OLD[$i]}"; new="${PAIRS_NEW[$i]}"
    old_mount="$(mount_target "$old")"; new_mount="$(mount_target "$new")"
    old_dev="$(device_of "$old")";      new_dev="$(device_of "$new")"
    if [ -z "$old_mount" ] || [ -z "$new_mount" ] || [ -z "$old_dev" ] || [ -z "$new_dev" ]; then
        refuse "cannot tell which filesystem $old or $new is on (findmnt/stat gave no answer) — refusing rather than guessing."
    fi
    if [ "$old_mount" = / ] || [ "$new_mount" = / ]; then
        refuse "$old or $new is on the ROOT filesystem ('$old_mount' / '$new_mount'): the backup drive is not mounted," \
               "or the store is configured onto the system disk. Mount the backup drive and check FILEBACKUP_STORE."
    fi
    if [ "$old_mount" != "$new_mount" ] || [ "$old_dev" != "$new_dev" ]; then
        refuse "$old is on '$old_mount' (device $old_dev) but $new would be on '$new_mount' (device $new_dev)." \
               "That is not a rename, it is a COPY of the whole ${PAIRS_WHAT[$i]}, and this script never copies." \
               "Either set FILEBACKUP_STORE in stack/.env to a folder on $old_mount (then run this again), or copy" \
               "by hand as a supervised step: README \"Migrating an existing store\", \"Across filesystems\"."
    fi
done

if [ "$APPLY" != 1 ]; then
    case " ${STATES[*]} " in
        *" move "*) log "plan is possible; nothing was changed. Run again with --apply to do it." ;;
        *)          log "nothing to do." ;;
    esac
    exit 0
fi

# ── apply ─────────────────────────────────────────────────────────────────────
# rename_no_copy OLD NEW : rename(2), and nothing else. os.rename raises on
# EXDEV instead of copying, and replaces NEW only if it is an EMPTY directory
# (classify has already refused a non-empty one).
rename_no_copy() {
    python3 -c 'import os, sys; os.rename(sys.argv[1], sys.argv[2])' "$1" "$2"
}
moved=0
for i in 0 1; do
    [ "${STATES[$i]}" = move ] || continue
    old="${PAIRS_OLD[$i]}"; new="${PAIRS_NEW[$i]}"
    if [ ! -d "$FB_STORE" ]; then
        mkdir -p "$FB_STORE" || { log "FAILED: cannot create the store folder $FB_STORE"; exit 1; }
        log "created $FB_STORE"
        # The new folder must be on the same mount as what moves into it.
        [ "$(mount_target "$FB_STORE")" = "$store_parent_mount" ] \
            || { log "FAILED: $FB_STORE appeared on an unexpected mount; nothing was moved into it"; exit 1; }
    fi
    if ! rename_no_copy "$old" "$new"; then
        log "FAILED: the rename $old -> $new did not happen (see the error above); $old is unchanged."
        log "  Fix the cause and run this again; it carries on from where it stopped."
        exit 1
    fi
    if [ -e "$old" ] || [ ! -d "$new" ]; then
        log "FAILED: after the rename, $old still exists or $new is missing — stop and look before anything runs."
        exit 1
    fi
    moved=$((moved + 1))
    log "renamed $old -> $new"
done

if [ "$moved" = 0 ]; then
    log "nothing to do."
else
    log "migrated: $moved rename(s). Nothing was copied or deleted."
fi
log "Next:"
log "  1. stack/.env: set FILEBACKUP_STORE=$FB_STORE and drop FILEBACKUP_STATE/BACKUP/CHANGES (read only to find the old store)."
log "  2. Make sure the FileBackup image was built from its BoundedRebuild work, then:"
log "       systemctl start homehub-library-backup.service"
if [ -d "$FB_CHANGES/Temp" ]; then
    log "  NOTE: $FB_CHANGES/Temp came along from the old engine. It may hold the only copy of superseded content;"
    log "        the new engine leaves it alone. Recover it by hand (FileBackup README, \"A run refuses because Temp exists\")."
fi
if [ -f "$FB_LEGACY_STATE/MANIFEST.csv" ]; then
    log "  3. After the first successful run, delete the retired source-side record by hand: rm -r $FB_LEGACY_STATE"
fi
exit 0
