#!/usr/bin/env bash
# library-backup.sh — the HOST side of the FileBackup container (E2 / Phase 2).
#
# THE DIVISION OF LABOUR, because everything below only makes sense with it:
# backup.sh keeps cifs ingest, the five `volume:` sets, the Mini-serv set, WoL
# and drive power. THIS wrapper drives one container that takes the nine library
# `path:` sets — the 4 TB tree nothing archives today — with per-file dedup and
# Snapshot_<date> history instead of eight full copies. A merge, not a swap.
#
# The container has `network_mode: none` and no way to tell anyone anything, so
# every preflight, every refusal and the whole NagLight report live out here. It
# is one `docker compose run` in the middle of a lot of proof.
#
# Sequence (each step below carries the failure it exists to prevent):
#   1. single-instance lock          — the first real run is a multi-day job
#   2. mount-identity preflight      — zero disk I/O; refuses BEFORE creating
#                                      anything (finding H / E2 cross-check 1)
#   3. capacity preflight            — a hard floor, posted RED (Q-FB2 carries
#                                      the whole space burden here)
#   4. pre-create the six bind paths — docker's chown-on-create is EPERM on the
#                                      FAT-family drives and kills the compose
#   5. write-probe /backup + /changes as uid 65532 (Q-FB5's refinement)
#   6. hold the drive awake for the run, restore standby on exit
#   7. `docker compose run` the backup — exit 0/1/2, three distinct messages
#      (NO prune step: Q-FB2 ruled retention NONE. Nothing here deletes.)
#   8. weekly `verify` gate, monthly `-Deep`  — restore-side codes 0/1/2/3/4
#   9. NagLight feed, AFTER the container exits. A failed POST fails the run.
#
# Never-silent-green throughout: every refusal posts ok=false and exits non-zero.
#
# Usage: library-backup.sh [--config PATH] [--preflight-only] [--verify MODE]
#   --config         backup.env (default /etc/homehub-backup/backup.env, else
#                    the backup.env beside this script)
#   --preflight-only steps 1-5 and stop. Nothing is created, no container runs.
#                    This is TC-H-M14's entry point.
#   --verify MODE    auto (default) | none | shallow | deep — override step 8's
#                    calendar gate. `auto` is the weekly/monthly schedule.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"
STACK_DIR="$(dirname "$HERE")"          # /opt/homehub/stack on the box

CONFIG=""; PREFLIGHT_ONLY=0; VERIFY_MODE=auto
while [ $# -gt 0 ]; do
    case "$1" in
        --config)         CONFIG="$2"; shift 2 ;;
        --preflight-only) PREFLIGHT_ONLY=1; shift ;;
        --verify)         VERIFY_MODE="$2"; shift 2 ;;
        -h|--help)        sed -n '2,40p' "$0"; exit 0 ;;
        *) die "unknown arg: $1 (try --help)" ;;
    esac
done
case "$VERIFY_MODE" in
    auto|none|shallow|deep) ;;
    *) die "--verify wants auto|none|shallow|deep, got '$VERIFY_MODE'" ;;
esac

# ── configuration: TWO files, and which one owns what matters ────────────────
# The HOST paths come from stack/.env, because that is the file docker compose
# interpolates when it mounts them — reading them anywhere else would let the
# wrapper preflight one set of directories while the container binds another.
# Everything else (the feed, the drive devices, the capacity floors) comes from
# backup.env, which is where this service's siblings already keep it.
#
# THE ORDER IS FORCED, and not by preference: common.sh defines load_env_file
# INSIDE load_config's body, so that helper does not exist until load_config has
# been called once. Calling it first fails with `load_env_file: command not
# found` — measured, not theorised. The two files share no key today, so which
# one would win a collision is moot; if that ever stops being true, hoist the
# nested definition in common.sh rather than reordering these two lines.
if [ -z "$CONFIG" ]; then
    if   [ -f /etc/homehub-backup/backup.env ]; then CONFIG=/etc/homehub-backup/backup.env
    elif [ -f "$HERE/backup.env" ];             then CONFIG="$HERE/backup.env"
    else die "no config: pass --config or create /etc/homehub-backup/backup.env"; fi
fi
load_config "$CONFIG"
load_env_file "$STACK_DIR/.env"

# The dedicated lane. NOT the `backup` check id: the bash service owns that one,
# and one of these two going red must never be able to look like the other going
# green. Created now, exactly when the container ships (the ratified deferral).
NAGLIGHT_FEED_CHECK="${LIBRARY_BACKUP_FEED_CHECK:-library-backup}"
export NAGLIGHT_FEED_CHECK

FB_CONFIG="${FILEBACKUP_CONFIG:-/etc/homehub-backup/filebackup.json}"
FB_SOURCE="${FILEBACKUP_SOURCE:-/srv/library}"
FB_STATE="${FILEBACKUP_STATE:-/var/lib/homehub-filebackup/state}"
FB_BACKUP="${FILEBACKUP_BACKUP:-/mnt/backup-drive/library}"
FB_CHANGES="${FILEBACKUP_CHANGES:-/mnt/backup-drive/library-changes}"
FB_LOGS="${FILEBACKUP_LOGS:-/var/log/homehub-filebackup}"

FB_UID=65532                            # the image's USER, and the fstab's uid=
FB_USER="${FILEBACKUP_USER_NAME:-filebackup}"
GUARD="$STACK_DIR/samba/library-guard.sh"
FSTAB="${LIBRARY_MOUNTS_FSTAB:-/etc/homehub-samba/library-mounts.fstab}"
IDENTITY_FILE="${DRIVE_IDENTITY_FILE:-/etc/homehub-samba/drive-identity.conf}"

MIN_FREE_GB="${BACKUP_LIBRARY_MIN_FREE_GB:-100}"
MIN_FREE_STATE_GB="${BACKUP_LIBRARY_MIN_FREE_STATE_GB:-2}"
case "$MIN_FREE_GB"       in ''|*[!0-9]*) die "config: BACKUP_LIBRARY_MIN_FREE_GB='$MIN_FREE_GB' is not a number (whole GB)" ;; esac
case "$MIN_FREE_STATE_GB" in ''|*[!0-9]*) die "config: BACKUP_LIBRARY_MIN_FREE_STATE_GB='$MIN_FREE_STATE_GB' is not a number (whole GB)" ;; esac

# ── never-silent-green machinery (the backup.sh pattern, same contract) ───────
DEGRADED=""                 # yellow facts: true, worth saying, not worth refusing
FILL_PCT="?"                # backup-drive fill, carried into the feed summary
SNAPSHOT_BEFORE=0; SNAPSHOT_AFTER=0
REPORTED_FAILURE=0

# report_failure NOTE — the ONE failure path. Idempotent, so whichever of the
# ERR trap and `die` arrives first owns the verdict. Always returns 0: reporting
# must not invent a second failure, and it must never mask the original one.
# The failure paths all exit non-zero regardless, so a POST that does not land
# here only needs SAYING — it cannot make the verdict any redder than it is.
report_failure() {
    local note="$1"
    [ "$REPORTED_FAILURE" = 0 ] || return 0
    REPORTED_FAILURE=1
    feed_naglight false "library-backup FAILED: $note"
    case "$FEED_LAST_CODE" in
        200)     ;;
        skipped) log "  (no NAGLIGHT_FEED_URL configured, so nothing outside this box has been told — expected on a sim box, not on the hub)" ;;
        *)       log "  AND the failure report itself did not land (HTTP $FEED_LAST_CODE) — the ERROR above is the real one, and nothing outside this box has been told about it" ;;
    esac
    log "LIBRARY BACKUP FAILED: $note"
    return 0
}
FAIL_NOTE=""
on_err() { report_failure "${FAIL_NOTE:-library backup failed at line $1}"; exit 1; }
trap 'on_err $LINENO' ERR
set -o errtrace
DIE_REPORTER=report_failure

# refuse NOTE… — a preflight refusal. Same shape as `die`, but it exists so the
# reading of this file matches its contract: everything before the container is
# a REFUSAL (nothing was created, nothing was written) rather than a failure
# part-way through a run.
refuse() { die "REFUSING TO RUN: $*"; }

# ── 1. SINGLE-INSTANCE LOCK ───────────────────────────────────────────────────
# The first real backup of a ~2 TiB library over USB is a MULTI-DAY run. The
# nightly timer will fire again while it is still going, and a second container
# sharing one /state and one /backup is not a slow backup, it is a corrupted
# one. systemd's Type=oneshot serialises the UNIT with itself; this also covers
# a manual `library-backup.sh`, the drill, and a run started from a shell.
#
# A HELD LOCK POSTS NOTHING, deliberately, and it is the one refusal here that
# does not. The lane belongs to the run that is still going: painting it red
# would report a healthy multi-day backup as a failure, which is a worse lie
# than saying nothing. The exit is still non-zero, so the unit shows failed and
# the journal names the holder.
LOCK_FILE="${LIBRARY_BACKUP_LOCK:-/run/lock/homehub-library-backup.lock}"
mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
# CHECKED, BECAUSE THE FAILURE IS A LIE OTHERWISE. A missing flock makes the
# `flock -n 9` below fail exactly as a HELD LOCK does, so every run would report
# "another library backup is already running" — forever, about a run that does
# not exist, while the library was never backed up once. flock ships in
# util-linux and is present on any Ubuntu; this refusal is for the case where
# something has gone wrong enough that it is not.
# findmnt is checked here for the SAME reason, one step ahead of where it is
# used: without it every mount check below answers "not mounted", and the
# refusal that follows would be a fluent, plausible message about an absent
# drive that is in fact plugged in and working. A missing tool must say so.
for _t in flock findmnt; do
    command -v "$_t" >/dev/null 2>&1 \
        || die "$_t is not on this box (it ships in util-linux) — refusing rather than running blind." \
               "Without flock a second run could share one /state and one /backup with a run that is" \
               "still going, and the missing tool would masquerade as a held lock on every run." \
               "Without findmnt every mount check answers 'not mounted', so the refusal would name" \
               "the wrong fault entirely.  Fix:  apt-get install util-linux"
done
exec 9>"$LOCK_FILE" || die "cannot open the lock file $LOCK_FILE"
if ! flock -n 9; then
    log "ANOTHER LIBRARY BACKUP IS ALREADY RUNNING (lock held: $LOCK_FILE)."
    log "  Not starting a second one: two containers sharing one /state and one"
    log "  /backup would corrupt the manifest, and the first real run of a ~2 TiB"
    log "  library takes DAYS. This is not a failure of that run — it is still going."
    log "  Watch it:  journalctl -u homehub-library-backup.service -f"
    log "  Nothing was posted to the feed: the running backup owns the lane."
    exit 1
fi
log "== library backup: lock acquired ($LOCK_FILE) =="

# ── 2. MOUNT-IDENTITY PREFLIGHT (E2 cross-check 1 / upstream finding H) ───────
# Refuses BEFORE anything is created, which is the whole point: with `nofail` in
# the generated fstab an absent drive leaves a perfectly good empty DIRECTORY on
# the system disk, and a run that proceeds writes the household's library there
# and reports green until the 119 GB NVMe fills. Same silent-green shape step 0
# of backup.sh exists to refuse; this is that check pointed at both mounts.
#
# THREE RATIFIED SOURCES, NO FOURTH. The mount/read-only gate is
# samba/library-guard.sh — the same code the share `root preexec` and the health
# timers use, invoked rather than re-implemented. The generated fstab says which
# mountpoints this box is supposed to have. drive-identity.conf says which disk
# should be answering. There is no second device-identity SSOT here and there
# must not be one.
#
# RED vs YELLOW follows the A23 doctrine: not mounted or read-only is a REFUSAL;
# a stand-in drive is a NOTE that travels into the feed summary. Running on
# substitute drives is the intended state during bring-up — being unable to tell
# is the thing that must never happen.

# existing_ancestor PATH : the nearest ancestor of PATH that exists. findmnt
# needs a path it can stat, and the bind targets are created in step 4 — after
# this. Walking up is how a not-yet-created directory still gets its mount asked
# about, instead of the check quietly skipping on the first run.
existing_ancestor() {
    local p="$1"
    while [ ! -e "$p" ] && [ "$p" != "/" ]; do p="$(dirname "$p")"; done
    printf '%s' "$p"
}

# mount_of PATH : the mountpoint PATH lives on, via findmnt. Empty + nonzero if
# findmnt cannot answer at all.
#
# `|| true` on the findmnt, and it is not decoration: `set -o errtrace` makes
# the ERR trap fire INSIDE a command substitution, so a findmnt that fails would
# post a red verdict from a subshell and report "failed at line NNN" instead of
# the caller's message. Every helper in this file that shells out from inside
# `$( )` swallows the status and lets EMPTY OUTPUT be the signal — the callers
# all check for it, and their messages are the ones worth reading.
mount_of() {
    local a; a="$(existing_ancestor "$1")"
    findmnt -n -o TARGET --target "$a" 2>/dev/null | tail -n1 || true
}

# fstab_declares MOUNTPOINT : 0 iff the generated fstab has an entry for it.
fstab_declares() {
    [ -r "$FSTAB" ] || return 2
    awk -v m="$1" '$0 !~ /^[[:space:]]*#/ && $2 == m { found = 1 } END { exit !found }' "$FSTAB"
}

# identity_note MOUNTPOINT : echoes a stand-in-drive note, or nothing. Reads
# drive-identity.conf and /proc/self/mountinfo only — ZERO disk I/O, so a parked
# drive stays parked (the same contract library-guard.sh keeps). Same comparison
# backup.sh makes at its step 0; kept here rather than pushed into the guard,
# because the guard's --report mode posts to its OWN check id and this run must
# not write another lane's verdict.
identity_note() {
    local mp="$1" expect want have wantmm
    [ -f "$IDENTITY_FILE" ] || return 0
    expect="$(awk -F'\t' -v p="$mp" '$1 == p { print $2 }' "$IDENTITY_FILE")"
    [ -n "$expect" ] || return 0
    [ -e "/dev/disk/by-id/$expect" ] || { printf 'the disk %s names for %s is not present' "$IDENTITY_FILE" "$mp"; return 0; }
    want="$(readlink -f "/dev/disk/by-id/$expect" 2>/dev/null)"
    have="$(awk -v p="$mp" '$5 == p { d = $3 } END { print d }' /proc/self/mountinfo)"
    wantmm=""
    [ -n "$want" ] && [ -r "/sys/class/block/${want#/dev/}/dev" ] &&
        wantmm="$(cat "/sys/class/block/${want#/dev/}/dev")"
    if [ -n "$wantmm" ] && [ -n "$have" ] && [ "$wantmm" != "$have" ]; then
        printf '%s is a STAND-IN drive, not %s' "$mp" "$expect"
    fi
}

# preflight_mount MOUNTPOINT LABEL — the whole gate for one mount.
preflight_mount() {
    local mp="$1" label="$2" note
    if ! bash "$GUARD" --check --library "$mp" --label "$label" 2>&1; then
        refuse "$mp is not a usable $label mount — it is absent, or mounted read-only." \
               "With nofail in the fstab that path is still an ordinary EMPTY DIRECTORY on the" \
               "SYSTEM disk, so a run would write the library there and look green until the" \
               "system disk filled. Nothing was created and no container was started." \
               "Check the drive is plugged in and powered, then: systemctl start homehub-library-backup.service"
    fi
    # findmnt is the independent second opinion the plan asks for: library-guard
    # reads /proc/self/mountinfo, findmnt reads the kernel's mount table through
    # libmount. If they disagree, something is very wrong and we want to know.
    if ! findmnt -n --mountpoint "$mp" >/dev/null 2>&1; then
        refuse "library-guard says $mp is mounted but findmnt does not see it as a mountpoint — refusing rather than picking a winner"
    fi
    log "preflight: $mp is a real, writable mount ($(findmnt -n -o SOURCE,FSTYPE --mountpoint "$mp" 2>/dev/null | tr -s ' '))"

    # `|| _rc=$?` and NOT `$(fstab_declares …; echo $?)`: `set -o errtrace` makes
    # the ERR trap fire inside a command substitution too, so an unchecked
    # non-zero in there would post a red verdict from a subshell. Checked forms
    # only, everywhere in this file.
    local _rc=0
    fstab_declares "$mp" || _rc=$?
    case "$_rc" in
        0) log "preflight:   declared by the generated fstab ($FSTAB)" ;;
        1) DEGRADED="${DEGRADED:+$DEGRADED; }$mp is mounted but the generated fstab does not declare it (hand-mounted?)"
           warn "preflight: $mp is NOT in $FSTAB — it was mounted by something other than the generated fstab" ;;
        *) DEGRADED="${DEGRADED:+$DEGRADED; }no $FSTAB, so the mount topology is not asserted"
           warn "preflight: $FSTAB is absent — cannot cross-check the mount topology (expected on a sim box)" ;;
    esac

    note="$(identity_note "$mp")"
    if [ -n "$note" ]; then
        DEGRADED="${DEGRADED:+$DEGRADED; }$note"
        log "preflight:   NOTE — $note"
        log "preflight:   Fine during bring-up. The point is that it is VISIBLE, not that it stops."
    fi
}

SOURCE_MOUNT="$(mount_of "$FB_SOURCE")"
[ "$SOURCE_MOUNT" = "$FB_SOURCE" ] \
    || refuse "the library source $FB_SOURCE is not a mountpoint of its own — it resolves to '${SOURCE_MOUNT:-?}'." \
              "That means the library drive is not mounted and $FB_SOURCE is an empty directory on that filesystem." \
              "A backup of it would be a backup of nothing, and FileBackup's AllowEmptySource safety net would" \
              "then refuse to empty an already-populated /backup — but this refusal is the one that costs nothing."
preflight_mount "$FB_SOURCE" library

BACKUP_MOUNT="$(mount_of "$FB_BACKUP")"
[ -n "$BACKUP_MOUNT" ] && [ "$BACKUP_MOUNT" != "/" ] \
    || refuse "the backup destination $FB_BACKUP resolves to the ROOT filesystem ('${BACKUP_MOUNT:-?}') — the backup drive is not mounted." \
              "Writing there would put the library's mirror and its whole snapshot history on the system disk."
preflight_mount "$BACKUP_MOUNT" backup-drive

CHANGES_MOUNT="$(mount_of "$FB_CHANGES")"
[ "$CHANGES_MOUNT" = "$BACKUP_MOUNT" ] \
    || refuse "the snapshot tree $FB_CHANGES is on '$CHANGES_MOUNT' but the mirror $FB_BACKUP is on '$BACKUP_MOUNT'." \
              "They must share one filesystem: a snapshot is made by MOVING superseded files out of the mirror, and" \
              "a cross-filesystem move is a copy — which would silently double the space every run costs."

# ── 3. CAPACITY PREFLIGHT — the whole of Q-FB2's space burden ─────────────────
# There is NO retention (Q-FB2, ruled 2026-08-23: snapshots are kept
# indefinitely and the Owner prunes by hand if space ever becomes a problem), so
# this floor and the fill percentage in every feed summary are the only things
# standing between "growing" and "full". A floor breach is a HARD refusal posted
# RED — not a warning — and every successful run carries the fill percentage so
# the trend is readable long before the floor is anywhere near.
#
# Growth is bounded by CHANGE RATE, not by run count: a no-change run creates no
# snapshot at all. That is why unbounded-by-policy is tolerable here and would
# not be for the eight full copies this replaces.
df_field() { df -PB1 "$1" 2>/dev/null | awk -v f="$2" 'NR==2 {print $f}' || true; }
gb() { printf '%s' "$(( ${1:-0} / 1000000000 ))"; }

_total="$(df_field "$(existing_ancestor "$FB_BACKUP")" 2)"
_used="$(df_field  "$(existing_ancestor "$FB_BACKUP")" 3)"
_avail="$(df_field "$(existing_ancestor "$FB_BACKUP")" 4)"
[ -n "${_avail:-}" ] \
    || refuse "cannot read the free space on $BACKUP_MOUNT (df returned nothing) — refusing rather than treating 'could not ask' as 'there is room'"
[ -n "${_total:-}" ] && [ "$_total" -gt 0 ] && FILL_PCT="$(( _used * 100 / _total ))%"
if [ "$_avail" -lt $(( MIN_FREE_GB * 1000000000 )) ]; then
    refuse "the backup drive is below its floor: $(gb "$_avail") GB free on $BACKUP_MOUNT, floor is ${MIN_FREE_GB} GB (${FILL_PCT} full)." \
        "THERE IS NO AUTOMATIC RETENTION (Q-FB2) — nothing here deletes a snapshot, so this will not clear itself." \
        "The manual relief valve, in this order:" \
        "  cd $STACK_DIR && docker compose --profile filebackup run --rm -T filebackup snapshots" \
        "  FILEBACKUP_DRY_RUN=1 docker compose --profile filebackup run --rm -T filebackup prune -Snapshot <name>   # dry run first" \
        "NEVER delete a Snapshot_* folder by hand — it is the only copy of the states it holds." \
        "Or lower BACKUP_LIBRARY_MIN_FREE_GB in backup.env if this floor is simply too high for this drive."
fi
log "capacity: backup drive $(gb "$_avail") GB free of $(gb "$_total") GB (${FILL_PCT} full, floor ${MIN_FREE_GB} GB)"

for _p in "$FB_STATE" "$FB_LOGS"; do
    _sa="$(df_field "$(existing_ancestor "$_p")" 4)"
    [ -n "${_sa:-}" ] \
        || refuse "cannot read the free space for $_p — refusing rather than guessing"
    if [ "$_sa" -lt $(( MIN_FREE_STATE_GB * 1000000000 )) ]; then
        refuse "only $(gb "$_sa") GB free for $_p (floor ${MIN_FREE_STATE_GB} GB)." \
            "This is usually the SYSTEM DISK — filling it stops Docker and every service on this box," \
            "so the backup is refused here rather than allowed to take the hub down with it."
    fi
done
log "capacity: state $(gb "$(df_field "$(existing_ancestor "$FB_STATE")" 4)") GB free, logs $(gb "$(df_field "$(existing_ancestor "$FB_LOGS")" 4)") GB free (floor ${MIN_FREE_STATE_GB} GB each)"

# ── 4. PRE-CREATE THE SIX BIND PATHS ─────────────────────────────────────────
# DOCKER CREATES A MISSING BIND SOURCE AND THEN CHOWNS IT, and the data drives
# are FAT-family (exfat/ntfs, no native ownership), so that chown returns EPERM
# and takes the ENTIRE compose command down — not just this service. Already bit
# this project once on 2026-08-08, on a box that had installed perfectly.
# Docker leaves a directory that already exists alone, so the fix is ordering.
#
# TWO OF THE SIX ARE ASSERTED, NOT CREATED. The config file and the library
# source: creating either would be manufacturing the thing whose absence is the
# fault. mkdir'ing a library over an unmounted drive is exactly the silent green
# step 2 just refused.
[ -r "$FB_CONFIG" ] \
    || refuse "the container's config $FB_CONFIG is missing or unreadable." \
              "It is a TRACKED template (stack/backup/filebackup.json) installed by autoinstall late-command 5c." \
              "Without it the container exits 2 having done nothing; refuse here instead, where the message is useful." \
              "By hand:  install -m0644 -o root -g root $STACK_DIR/backup/filebackup.json $FB_CONFIG"
[ -d "$FB_SOURCE" ] || refuse "the library source $FB_SOURCE is not a directory"
for _d in "$FB_STATE" "$FB_BACKUP" "$FB_CHANGES" "$FB_LOGS"; do
    if [ ! -d "$_d" ]; then
        mkdir -p "$_d" || { FAIL_NOTE="cannot create the bind path $_d"; false; }
        log "created bind path $_d"
    fi
done
# /state and /logs are on the SYSTEM disk (ext4), where ownership is real and
# a root-created directory is unwritable to the container — the first lab run
# died on exactly that ('/logs/Backup_Global.log' Access denied). chown works
# there and is the fix. The two drive paths are NOT chowned: NTFS/exFAT
# ownership comes from the mount options alone (Q-FB5) and chown is a no-op
# or an error there — the write-probe below is what proves those.
for _d in "$FB_STATE" "$FB_LOGS"; do
    chown "$FB_UID:$FB_UID" "$_d" \
        || refuse "cannot chown $_d to uid $FB_UID — the container cannot write its state/logs without it."
done
# The wrapper's own log lands beside the container's, now that the directory is
# known to exist. Everything above this line is journal-only, which is correct:
# a refusal that could not create a log directory must still be readable.
LOG_FILE="${LIBRARY_BACKUP_LOG:-$FB_LOGS/library-backup.log}"
log "== library backup starting (wrapper log: $LOG_FILE) =="

# ── 5. WRITE-PROBE AS uid 65532 (the Q-FB5 refinement) ───────────────────────
# NTFS/exFAT ownership is SYNTHESIZED from the mount options, so no chown and no
# pre-created directory can grant the container access — only the fstab's
# `uid=65532,gid=65532` can, and the old emission was `uid=0,gid=0,umask=0077`,
# under which the container as shipped could not write /mnt/backup-drive AT ALL.
# That is fixed at the source (Generate-FromStorageMap.ps1), and this is the
# guard against it regressing: a broken fstab must fail HERE, loudly, in the
# first seconds — not eight hours into a multi-day run.
#
# runuser first, setpriv as the fallback: runuser wants a NAME, which is why
# provision-backup-principal.sh names uid 65532 `filebackup` at firstboot; the
# fstab stays numeric because the kernel resolves no names. setpriv covers a box
# where that provisioning has not run.
probe_write() {
    # Three lines, not one: bash expands the whole `local` command BEFORE it
    # runs, so `probe="$dir/..."` on the same line reads the OUTER (unset)
    # dir and set -u kills the run — measured on the first real lab run.
    local dir="$1"
    local probe="$dir/.filebackup-writeprobe.$$"
    local rc=0
    if command -v runuser >/dev/null 2>&1 && id -u "$FB_USER" >/dev/null 2>&1; then
        runuser -u "$FB_USER" -- touch "$probe" >/dev/null 2>&1 || rc=$?
    elif command -v setpriv >/dev/null 2>&1; then
        warn "write-probe: no '$FB_USER' account — probing by number via setpriv (run provision/provision-backup-principal.sh to name uid $FB_UID)"
        setpriv --reuid="$FB_UID" --regid="$FB_UID" --clear-groups touch "$probe" >/dev/null 2>&1 || rc=$?
    else
        refuse "cannot write-probe $dir as uid $FB_UID: neither runuser nor setpriv is on this box (both ship in util-linux)." \
               "Refusing rather than skipping the probe — an unprobed run is how a fstab regression reaches hour eight of a multi-day backup."
    fi
    if [ "$rc" -ne 0 ]; then
        refuse "uid $FB_UID CANNOT WRITE $dir — the container runs as that uid and would fail mid-backup." \
               "The cause is almost always the mount options: this filesystem has no native ownership, so access comes" \
               "from the fstab's uid=/gid=/umask= and nothing else. Expected: uid=$FB_UID,gid=$FB_UID,umask=0077 (Q-FB5)." \
               "Check:  findmnt -n -o SOURCE,FSTYPE,OPTIONS --mountpoint $BACKUP_MOUNT" \
               "The generated fstab is $FSTAB; it is emitted by HomeHub's Generate-FromStorageMap.ps1."
    fi
    rm -f "$probe" 2>/dev/null || warn "write-probe: could not remove $probe (harmless, but say so rather than leave it a mystery)"
    log "write-probe: uid $FB_UID can write $dir"
}
probe_write "$FB_BACKUP"
probe_write "$FB_CHANGES"
# The ext4 pair too: a probe is cheaper than the container failing on its
# first log line, and it catches a future provisioner re-owning them.
probe_write "$FB_STATE"
probe_write "$FB_LOGS"

if [ "$PREFLIGHT_ONLY" = 1 ]; then
    log "--preflight-only: every check passed and NOTHING was started."
    log "  degraded notes: ${DEGRADED:-none}"
    trap - ERR
    exit 0
fi

# ── 6. HOLD THE DRIVE AWAKE FOR THE RUN (WI-10.10 drive power) ───────────────
# backup-standby.service parks the backup drive with `hdparm -S` at boot, and a
# long no-write phase (hashing the source, verifying) would let it spin down
# UNDER the run. Same helpers, same contract as backup.sh: power management NEVER
# fails a backup — missing hdparm, an absent device or an enclosure that ignores
# the command is a WARNING and the run continues. The EXIT trap re-arms standby
# on every path, success or failure or interrupt.
read -r -a BACKUP_DRIVES <<< "${BACKUP_DRIVE_DEVICES:-}" || true
STANDBY_VALUE="${BACKUP_DRIVE_STANDBY:-241}"
drive_power_restore() {
    [ "${#BACKUP_DRIVES[@]}" -gt 0 ] || return 0
    log "drive-power: restoring standby ($STANDBY_VALUE = $(standby_desc "$STANDBY_VALUE")) on exit"
    drive_standby_set "$STANDBY_VALUE" "${BACKUP_DRIVES[@]}"
}
trap 'drive_power_restore' EXIT
if [ "${#BACKUP_DRIVES[@]}" -gt 0 ]; then
    log "drive-power: disabling standby (hdparm -S 0) on ${#BACKUP_DRIVES[@]} drive(s) for the run"
    drive_standby_set 0 "${BACKUP_DRIVES[@]}"
fi

# ── 7. THE CONTAINER ─────────────────────────────────────────────────────────
# `run`, not `up`: this is a one-shot with no restart policy, and `run` enables
# the target service's profile implicitly. --rm so nothing accumulates; -T
# because a systemd oneshot has no TTY and compose would otherwise refuse.
#
# THE PROFILE FLAG IS GLOBAL (`docker compose --profile X run …`), which is a
# deliberate departure from the plan's literal `run --rm -T --profile filebackup`:
# `--profile` is a top-level compose flag and `run` does not accept one. The
# effect is identical and this form is the one that parses.
#
# NO PRUNE STEP ANYWHERE BELOW. Q-FB2 ruled retention NONE; the space burden is
# carried entirely by step 3's floor and by the fill percentage in the summary.
fb_run() {
    ( cd "$STACK_DIR" && docker compose --profile filebackup run --rm -T filebackup "$@" )
}

# count_snapshots : how many Snapshot_* trees the change root holds. A run that
# supersedes nothing creates none, so this is how "was a snapshot created" gets
# ANSWERED rather than assumed — the feed summary carries the answer.
count_snapshots() {
    find "$FB_CHANGES" -mindepth 1 -maxdepth 1 -type d -name 'Snapshot_*' 2>/dev/null | grep -c . || true
}
SNAPSHOT_BEFORE="$(count_snapshots)"

log "running: docker compose --profile filebackup run --rm -T filebackup backup"
BACKUP_RC=0
fb_run backup || BACKUP_RC=$?
SNAPSHOT_AFTER="$(count_snapshots)"
SNAPSHOT_MADE=no
[ "${SNAPSHOT_AFTER:-0}" -gt "${SNAPSHOT_BEFORE:-0}" ] && SNAPSHOT_MADE=yes

# Re-read the fill AFTER the run: the number in the summary should describe the
# drive the operator has now, not the one the preflight saw.
_total="$(df_field "$FB_BACKUP" 2)"; _used="$(df_field "$FB_BACKUP" 3)"; _avail="$(df_field "$FB_BACKUP" 4)"
[ -n "${_total:-}" ] && [ "$_total" -gt 0 ] && FILL_PCT="$(( _used * 100 / _total ))%"

# THE BACKUP-ACTION EXIT MAP (SR-043). Three codes, three distinct causes, three
# messages — one generic "backup failed" would put a human in the container log
# every time, which is the finding this table exists to close. There is no code 3
# on the backup path.
case "$BACKUP_RC" in
    0) log "container: backup COMPLETE (exit 0) — snapshot created: $SNAPSHOT_MADE, drive $FILL_PCT full" ;;
    1) FAIL_NOTE="the backup set FAILED (container exit 1) — the run reached the set and could not complete it. This is a data/IO problem, not a configuration one: read $FB_LOGS/Backup_Global.log"
       false ;;
    2) FAIL_NOTE="the container REFUSED THE CONFIGURATION (exit 2) — $FB_CONFIG could not be loaded, or it violates the config contract. Retrying will not help. Nothing was backed up. Compare it against FileBackup's container/FileBackup.schema.json (closed schema: an extra key is a violation)"
       false ;;
    125|126|127) FAIL_NOTE="docker itself failed (exit $BACKUP_RC) before FileBackup ran — usually a missing image. Is filebackup:\${FILEBACKUP_IMAGE_TAG} loaded? \`docker image ls filebackup\`; it is baked by vmtest/export-images.sh and loaded by firstboot"
       false ;;
    *) FAIL_NOTE="the container exited $BACKUP_RC, which is outside the documented 0/1/2 table — treat it as a failure and read $FB_LOGS/Backup_Global.log"
       false ;;
esac

# ── 8. THE VERIFY GATE — weekly, deep monthly ────────────────────────────────
# A backup nobody reads is a hypothesis. `verify` re-proves the stored manifest;
# -Deep additionally proves payload identity, which costs real time (it reads
# the data files) and so is monthly rather than weekly.
#
# `auto` = the FIRST occurrence of BACKUP_LIBRARY_VERIFY_DAY each month is deep,
# every other occurrence is shallow, and every other day skips. Computed from the
# calendar rather than from a state file on purpose: a state file on the backup
# drive would be lost with the drive, and one on the system disk would drift.
VERIFY_DAY="${BACKUP_LIBRARY_VERIFY_DAY:-7}"        # ISO weekday, 7 = Sunday
VERIFY_RUN="$VERIFY_MODE"
if [ "$VERIFY_MODE" = auto ]; then
    VERIFY_RUN=none
    if [ "$(date +%u)" = "$VERIFY_DAY" ]; then
        # A weekday's first occurrence in a month is always day-of-month 1-7.
        if [ "$(date +%-d)" -le 7 ]; then VERIFY_RUN=deep; else VERIFY_RUN=shallow; fi
    fi
fi
VERIFY_NOTE="skipped"
if [ "$VERIFY_RUN" != none ]; then
    VERIFY_ARGS=(verify)
    [ "$VERIFY_RUN" = deep ] && VERIFY_ARGS+=(-Deep)
    log "verify gate: running ${VERIFY_ARGS[*]} (mode=$VERIFY_RUN, weekday $(date +%u), day $(date +%-d))"
    VERIFY_RC=0
    fb_run "${VERIFY_ARGS[@]}" || VERIFY_RC=$?
    # The RESTORE-SIDE code table (SR-040), which is a different table from the
    # backup one above and is shared with reconstruct.sh:
    #   0 complete · 1 incomplete/content · 2 usage or precondition ·
    #   3 manifest-witness verification failed · 4 incomplete/host (retriable)
    # Precedence when several apply: 2 > 3 > 4 > 1.
    case "$VERIFY_RC" in
        0) VERIFY_NOTE="$VERIFY_RUN ok"
           log "verify: COMPLETE (exit 0)" ;;
        1) VERIFY_NOTE="$VERIFY_RUN INCOMPLETE (content)"
           FAIL_NOTE="verify exit 1 — INCOMPLETE: stored content is missing for at least one manifest row. The backup is not fully restorable. Read $FB_LOGS/Backup_Global.log for the rows"
           false ;;
        2) VERIFY_NOTE="$VERIFY_RUN REFUSED (usage/precondition)"
           FAIL_NOTE="verify exit 2 — usage or precondition failure: the verify was asked for something it cannot do (bad invocation, or a precondition like a missing manifest). Retrying unchanged will not help"
           false ;;
        3) VERIFY_NOTE="$VERIFY_RUN WITNESS MISMATCH"
           FAIL_NOTE="verify exit 3 — MANIFEST WITNESS MISMATCH: the manifest does not match its own sidecar digest. Treat the backup index as untrustworthy until this is understood; do NOT restore from it and do NOT let another run overwrite the evidence"
           false ;;
        4) VERIFY_NOTE="$VERIFY_RUN incomplete (host, retriable)"
           FAIL_NOTE="verify exit 4 — incomplete for a HOST reason (a dependency this box lacks, e.g. 7-Zip for an archived row). Retriable once the host is fixed; the stored data is not implicated"
           false ;;
        *) VERIFY_NOTE="$VERIFY_RUN exit $VERIFY_RC"
           FAIL_NOTE="verify exited $VERIFY_RC, outside the documented 0/1/2/3/4 table"
           false ;;
    esac
else
    log "verify gate: not today (mode=$VERIFY_MODE, weekday $(date +%u), verify day $VERIFY_DAY)"
fi

# ── 9. THE REPORT (E2 cross-check 2) ─────────────────────────────────────────
# AFTER the container exits, and it can only be after: the container has
# `network_mode: none` and no way to reach the tracker, which is the design —
# reporting never moves inside it.
#
# A FAILED POST IS ITSELF A FAILURE. A backup that ran perfectly and told nobody
# is indistinguishable, from the outside, from a backup that never ran; the whole
# never-silent-green contract is that the outside can tell. So the exit status
# below is non-zero when the POST did not land, and the unit shows failed.
trap - ERR
SUMMARY="exit=$BACKUP_RC snapshot=$SNAPSHOT_MADE drive=$FILL_PCT full; verify=$VERIFY_NOTE"
[ -n "$DEGRADED" ] && SUMMARY="$SUMMARY; DEGRADED: $DEGRADED"
feed_naglight true "library backup ok — $SUMMARY"
# An UNCONFIGURED lane is not a failed POST, and the difference matters. `skipped`
# means backup.env carries no NAGLIGHT_FEED_URL — the documented state of a sim
# box, and the same thing backup.sh treats as a benign skip. Failing here would
# make every sim run red for a reason that has nothing to do with the backup.
# It is still said out loud, every time, because on the HUB it would be a real
# gap: a run reporting to nobody.
if [ "$FEED_LAST_CODE" = "skipped" ]; then
    log "NOTE: no NAGLIGHT_FEED_URL in $CONFIG, so this run reported to NOBODY."
    log "  Correct for a sim box; on the hub it means the library-backup lane does not exist."
    log "  Summary that went nowhere: $SUMMARY"
elif [ "$FEED_LAST_CODE" != "200" ]; then
    log "FATAL: the library backup itself succeeded, but the NagLight POST did not land (HTTP $FEED_LAST_CODE)."
    log "  Exiting non-zero so this unit reports FAILED. A backup nobody was told about is"
    log "  not a backup that reported — from outside this box it looks exactly like a run"
    log "  that never happened, and that is the one thing this contract forbids."
    log "  Summary that could not be delivered: $SUMMARY"
    exit 1
fi
log "== library backup OK — $SUMMARY =="
exit 0
