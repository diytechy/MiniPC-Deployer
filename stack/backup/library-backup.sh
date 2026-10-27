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
# REQUIRES a FileBackup image built from BoundedRebuild AS IT IS (7a6a80a, the
# bounded run loop): ONE writable /store mount holding backup/ (BackupPath) and
# changes/ (ChangePath, with the run files Temp.lock, backup.log, Incoming/,
# Building_*, Verify/), filebackup-store.json at ConfigVersion 2, no /state
# mount, and the backup exit table 0/1/2/4 below (verify 0/1/2). NOT an image
# with FileBackup's later single-store call C44 (StorePath, Latest/, ConfigVersion
# 3): that would need its own contract change here first. README "The one-mount
# store".
#
# ONLY WHEN FILEBACKUP_LAYOUT=single (stack/.env; SR-133). The default is
# `legacy`: the run is handed, arguments unchanged, to library-backup-legacy.sh,
# the three-mount wrapper the deployed kit-12 image needs, kept byte for byte as
# it was before this layout merged. So a stack delivery changes nothing until
# the Owner switches the knob at the cutover (README "The layout knob").
#
# Sequence (each step below carries the failure it exists to prevent):
#   1. single-instance lock          — the first real run is a multi-day job
#   2. mount-identity preflight      — zero disk I/O; refuses BEFORE creating
#                                      anything (finding H / E2 cross-check 1)
#   2b. store-layout guard           — refuses while the old three-mount store
#                                      still holds data (set it aside first)
#   3. capacity preflight            — a hard floor, posted RED (Q-FB2 carries
#                                      the whole space burden here)
#   4. pre-create the bind paths     — docker's chown-on-create is EPERM on the
#                                      FAT-family drives and kills the compose.
#                                      NEVER backup/ or changes/ (FileBackup's)
#   5. write-probe the store, backup/ and changes/ where present, /logs (Q-FB5)
#   5b. an old engine's Temp folder  — a NOTE naming the recovery event; the
#                                      bounded engine leaves Temp alone and runs
#   6. hold the drive awake for the run, restore standby on exit
#   7. `docker compose run` the backup — exit 0/1/2/4, four distinct messages
#      (NO prune step: Q-FB2 ruled retention NONE. Nothing here deletes.)
#   8. weekly `verify` gate, monthly `-Deep`  — verify codes 0/1/2
#   9. NagLight feed, AFTER the container exits. lastSuccess advances ONLY for
#      backup exit 0 plus a passing verify. A failed POST fails the run.
#
# Never-silent-green throughout: every refusal and every failure exits non-zero
# and posts no lastSuccess, so the lane's age grows.
#
# Usage: library-backup.sh [--config PATH] [--preflight-only] [--verify MODE]
#   --config         backup.env (default /etc/homehub-backup/backup.env, else
#                    the backup.env beside this script)
#   --preflight-only steps 1-5 and stop. Nothing is created, no container runs.
#                    This is TC-H-M21's preflight entry point; the full case
#                    also proves the combined last_success is unchanged.
#   --verify MODE    auto (default) | none | shallow | deep — override step 8's
#                    calendar gate. `auto` is the weekly/monthly schedule.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"
STACK_DIR="$(dirname "$HERE")"          # /opt/homehub/stack on the box

CONFIG=""; PREFLIGHT_ONLY=0; VERIFY_MODE=auto; RECONCILE_RUN_STATE=0
# Kept whole for the legacy hand-off below, which must see exactly these.
ORIG_ARGS=("$@")
while [ $# -gt 0 ]; do
    case "$1" in
        --config)         CONFIG="$2"; shift 2 ;;
        --preflight-only) PREFLIGHT_ONLY=1; shift ;;
        --verify)         VERIFY_MODE="$2"; shift 2 ;;
        --reconcile-run-state) RECONCILE_RUN_STATE=1; shift ;;
        -h|--help)        sed -n '2,60p' "$0"; exit 0 ;;
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

# ── 0. THE LAYOUT KNOB (SR-133): legacy until the cutover ────────────────────
# The hub runs the kit-12 image on the three-mount store (/state, /backup,
# /changes) until the Owner builds a BoundedRebuild image, brings in the new
# store, sets the old one aside and switches this knob. Until then the run
# belongs to the wrapper that image needs, unchanged: everything below this
# block would refuse that store (step 2b) and bind mounts that image cannot
# use. Read here, after both config files, because stack/.env is where the knob
# lives and where compose reads the store paths. Unset is legacy, so a hub whose
# .env predates the knob (a stack delivery without a re-materialised .env)
# keeps exactly what it ran. Invoked through bash because the payload's scripts
# carry no execute bit (late-command 5c sets it on this file only). A hand-off
# posts nothing.
FB_LAYOUT="${FILEBACKUP_LAYOUT:-legacy}"
case "$FB_LAYOUT" in
    legacy)
        log "store layout: FILEBACKUP_LAYOUT=legacy - the three-mount layout; handing the run to library-backup-legacy.sh"
        exec bash "$HERE/library-backup-legacy.sh" ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"} ;;
    single) ;;
    *) die "config: FILEBACKUP_LAYOUT='$FB_LAYOUT' is neither legacy nor single (stack/.env). Nothing was started and nothing was posted." ;;
esac
# The compose service and the container config of the one-mount layout.
# Separate from the legacy `filebackup` service and filebackup.json so that both
# layouts can be installed side by side and the knob alone chooses.
FB_SERVICE=filebackup-store

# The reserved IF-010 item is not a generic `/api/feed` lane. This wrapper owns
# only its lastSuccess field; the library/Samba monitor owns shareHealth. Their
# independent writes are what let share recovery reveal an old backup age
# instead of manufacturing a green backup.
FILE_SHARE_BACKUP_FEED_ID="${FILE_SHARE_BACKUP_FEED_ID:-file-share-backup-health}"
export FILE_SHARE_BACKUP_FEED_ID

# The one-mount config. FILEBACKUP_STORE_CONFIG, not the legacy
# FILEBACKUP_CONFIG, so an override written for one layout can never point the
# other at the wrong document.
FB_CONFIG="${FILEBACKUP_STORE_CONFIG:-/etc/homehub-backup/filebackup-store.json}"
FB_SOURCE="${FILEBACKUP_SOURCE:-/srv/library}"
# FB_STORE, FB_BACKUP (= store/backup), FB_CHANGES (= store/changes) and the
# retired three-mount paths, from common.sh so the migration step agrees.
filebackup_store_layout
FB_LOGS="${FILEBACKUP_LOGS:-/var/log/homehub-filebackup}"
# A SYSTEM-DISK copy of the latest manifest, refreshed after every completed
# run, for stack/tracker/frame-freshness.sh (IF-035), which must never wake the
# backup drive. It used to read FileBackup's source-side record on /state, which
# the bounded engine no longer writes. frame-freshness.sh defaults to this path.
FB_MANIFEST_COPY="${LIBRARY_MANIFEST_COPY:-/var/lib/homehub-filebackup/latest/MANIFEST.csv}"

FB_UID=65532                            # the image's USER, and the fstab's uid=
FB_USER="${FILEBACKUP_USER_NAME:-filebackup}"
GUARD="$STACK_DIR/samba/library-guard.sh"
FSTAB="${LIBRARY_MOUNTS_FSTAB:-/etc/homehub-samba/library-mounts.fstab}"
IDENTITY_FILE="${DRIVE_IDENTITY_FILE:-/etc/homehub-samba/drive-identity.conf}"

MIN_FREE_GB="${BACKUP_LIBRARY_MIN_FREE_GB:-100}"
# The SYSTEM-DISK floor. Named for the retired /state mount and kept under that
# name so existing backup.env files keep working; it now guards /logs.
MIN_FREE_STATE_GB="${BACKUP_LIBRARY_MIN_FREE_STATE_GB:-2}"
case "$MIN_FREE_GB"       in ''|*[!0-9]*) die "config: BACKUP_LIBRARY_MIN_FREE_GB='$MIN_FREE_GB' is not a number (whole GB)" ;; esac
case "$MIN_FREE_STATE_GB" in ''|*[!0-9]*) die "config: BACKUP_LIBRARY_MIN_FREE_STATE_GB='$MIN_FREE_STATE_GB' is not a number (whole GB)" ;; esac

# ── never-silent-green machinery (the backup.sh pattern, same contract) ───────
DEGRADED=""                 # yellow facts: true, worth saying, not worth refusing
FILL_PCT="?"                # backup-drive fill, carried into the feed summary
SNAPSHOT_BEFORE=0; SNAPSHOT_AFTER=0
REPORTED_FAILURE=0

# report_failure NOTE — the ONE failure path. Idempotent, so whichever of the
# ERR trap and `die` arrives first owns the verdict. A failed attempt does not
# write IF-010 at all: failure must preserve the last artifact-verified success
# timestamp. The non-zero exit plus systemd/journal remain the operational fault
# signal.
# RUN_STATE_OWNED is set only once this process holds the lock. Everything that
# withdraws the running claim checks it, because a run we do not own is a run
# still in progress: posting idle for it would paint a live backup as finished.
RUN_STATE_OWNED=0
run_state() {
    [ "$RUN_STATE_OWNED" = 1 ] || return 0
    post_file_share_backup_state runState "$1"
}

report_failure() {
    local note="$1"
    [ "$REPORTED_FAILURE" = 0 ] || return 0
    REPORTED_FAILURE=1
    # Withdraw the in-progress claim. This is NOT a success report and cannot be
    # mistaken for one: runState is an independent field, so lastSuccess keeps
    # the last artifact-verified timestamp and the lane falls back to the age it
    # already had (or to the share fault that caused this).
    run_state idle
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
# nightly timer will fire again while it is still going. FileBackup's own lock
# (<store>/changes/Temp.lock) would stop a second container with exit 4, but
# only after compose, the drive spin-up and the preflight; this lock answers
# first and names the run that holds it. systemd's Type=oneshot serialises the
# UNIT with itself; this also covers a manual `library-backup.sh`, the drill,
# and a run started from a shell.
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
               "Without flock a second run could start against the store of a run that is" \
               "still going, and the missing tool would masquerade as a held lock on every run." \
               "Without findmnt every mount check answers 'not mounted', so the refusal would name" \
               "the wrong fault entirely.  Fix:  apt-get install util-linux"
done
exec 9>"$LOCK_FILE" || die "cannot open the lock file $LOCK_FILE"
# A BOUNDED WAIT, NOT `-n`, for a real run. The ten-minute reconciler takes this
# same lock for the length of one HTTP post, and with `-n` a nightly backup that
# happened to land in that window exited 1 and SKIPPED THE NIGHT — a missed
# backup caused entirely by the staleness check meant to protect it. Waiting
# costs at most one bounded transport deadline; a genuinely held lock (a run
# that takes days) still refuses below, exactly as before.
#
# Reconcile keeps `-n`: for it a held lock is the answer, not something to wait
# for.
FLOCK_WAIT=(-w 30)
[ "$RECONCILE_RUN_STATE" = 1 ] && FLOCK_WAIT=(-n)
if ! flock "${FLOCK_WAIT[@]}" 9; then
    # A held lock is the ANSWER in reconcile mode, not a refusal: a run is in
    # progress, so its phase is current and must be left exactly as it is. Quiet
    # and zero, because this path runs on a short timer and the refusal text
    # below would be both wrong here and repeated every cycle.
    if [ "$RECONCILE_RUN_STATE" = 1 ]; then
        trap - ERR
        log "run-state: a backup is in progress; leaving its phase alone"
        exit 0
    fi
    log "ANOTHER LIBRARY BACKUP IS ALREADY RUNNING (lock held: $LOCK_FILE)."
    log "  Not starting a second one: FileBackup runs one at a time per store, and"
    log "  the first real run of a ~2 TiB library takes DAYS. This is not a failure"
    log "  of that run — it is still going."
    log "  Watch it:  journalctl -u homehub-library-backup.service -f"
    log "  Nothing was posted to the feed: the running backup owns the lane."
    exit 1
fi
log "== library backup: lock acquired ($LOCK_FILE) =="
# A --preflight-only run is a DIAGNOSTIC, not an attempt to back anything up, and
# it exits successfully without reaching any of the phase writes below. Claiming
# a phase for it would strand `starting` on the wall until something else
# reconciled it. Owning no phase is also why report_failure's withdrawal is a
# no-op on that path: there is nothing to withdraw.
[ "$PREFLIGHT_ONLY" = 1 ] || RUN_STATE_OWNED=1

# --reconcile-run-state: we just proved no run is in progress by TAKING the
# lock, so any stored `running` phase is a leftover from a run that died without
# withdrawing it — a power cut, a SIGKILL, an OOM. Withdraw it and stop.
#
# This is deliberately not a timeout. Library backups are legitimately bimodal
# (~90 min, ~5h45m on the weekly hash-recalc night), so no duration bound could
# tell a long run from a dead one; the lock can, because the kernel drops it
# when the holder dies. Posting idle when already idle is a no-op, so this is
# safe to run on a short timer.
if [ "$RECONCILE_RUN_STATE" = 1 ]; then
    trap - ERR
    # THE LOCK ALONE IS NOT PROOF. fb_run starts the FileBackup work through
    # `docker compose run`, and that container is owned by the DAEMON: fd 9 is
    # inherited by the compose client, never by the container. A wrapper killed
    # with SIGKILL or by the OOM killer therefore releases this lock while
    # FileBackup is still writing to the store. Withdrawing the phase
    # on the strength of the lock would then report a live backup as finished.
    #
    # So ask the daemon as well, and FAIL CLOSED: if the container is up, or if
    # we cannot tell, leave the phase exactly as it is. A phase left standing is
    # repaired by the next cycle; one withdrawn wrongly says a running backup
    # ended, which is the lie this whole lane exists to prevent.
    # BOTH services are asked: a container of either layout writing the backup
    # drive is a backup still going, whichever wrapper started it.
    fb_containers=""
    for _svc in "$FB_SERVICE" filebackup; do
        if ! _ids="$(docker ps --filter "label=com.docker.compose.service=$_svc" --format '{{.ID}}' 2>/dev/null)"; then
            log "run-state: cannot ask docker whether FileBackup is running — leaving the phase alone"
            exit 0
        fi
        fb_containers="${fb_containers:+$fb_containers }$_ids"
    done
    fb_containers="${fb_containers% }"
    if [ -n "$fb_containers" ]; then
        log "run-state: a FileBackup container is still running ($fb_containers) — leaving the phase alone"
        exit 0
    fi
    run_state idle
    log "run-state reconciled: no lock held and no FileBackup container running"
    exit 0
fi

run_state starting

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
#
# A FILESYSTEM LIVES ON A PARTITION, SO THE MOUNTED DEVICE IS ALMOST NEVER THE
# WHOLE DISK — and comparing against the whole disk alone is what made this
# report the REAL drives as stand-ins on every run up to 2026-09-04.
# drive-identity.conf records the whole-disk by-id name
# ('ata-WDC_...-11BCSS0_WD-XXXX'), `readlink -f` on it resolves to /dev/sdX
# (8:0), and what is mounted is /dev/sdX1 (8:1). Those never compare equal, so
# the yellow was permanent and indistinguishable from a genuine substitute. The
# 2026-08-01 "proven on real loopback devices" run passed only because loop
# devices mount WHOLE-DEVICE, which is exactly the case the bug cannot reach.
# A candidate is accepted on PARENTAGE, not on the shape of its name: the
# expected whole disk resolves to one major:minor, and a '-part<N>' candidate is
# considered only when sysfs says its parent IS that disk. Matching the name
# alone would be a lexical prefix test — '<expected>-part*' accepts any by-id
# entry beginning with that string — so the parent check is what actually earns
# the word "exact" here. (Raised by the 2026-09-06 adversarial review;
# library-guard.sh's resolve_identity() still matches on name shape alone.)
#
# "Cannot tell" still says NOTHING: unless at least one candidate resolved to a
# real major:minor, this stays silent rather than crying stand-in at a drive it
# simply failed to read.
#
# ASSUMED LAYOUT — the mounted filesystem is the expected disk itself or a
# partition of it. A dm-crypt/LVM/MD layer in between resolves to a 253:x or 9:x
# device whose sysfs parent is not the expected disk, and this would call that a
# stand-in. Both mounts it guards are plain partitions by design (storage-map.md
# §1), so that is a stated constraint, not a live defect; supporting a stacked
# layout means walking the sysfs 'slaves' graph and nothing here needs it yet.
identity_note() {
    local mp="$1" expect have link target dev mm diskmm resolved=0
    [ -f "$IDENTITY_FILE" ] || return 0
    expect="$(awk -F'\t' -v p="$mp" '$1 == p { print $2 }' "$IDENTITY_FILE")"
    [ -n "$expect" ] || return 0
    [ -e "/dev/disk/by-id/$expect" ] || { printf 'the disk %s names for %s is not present' "$IDENTITY_FILE" "$mp"; return 0; }
    have="$(awk -v p="$mp" '$5 == p { d = $3 } END { print d }' /proc/self/mountinfo)"
    [ -n "$have" ] || return 0

    # The expected whole disk: both the first candidate, and the anchor every
    # partition candidate below is checked against.
    target="$(readlink -f "/dev/disk/by-id/$expect" 2>/dev/null)" || return 0
    [ -n "$target" ] || return 0
    [ -r "/sys/class/block/${target#/dev/}/dev" ] || return 0
    diskmm="$(cat "/sys/class/block/${target#/dev/}/dev" 2>/dev/null)" || return 0
    [ -n "$diskmm" ] || return 0
    resolved=1
    if [ "$diskmm" = "$have" ]; then
        return 0
    fi

    for link in "/dev/disk/by-id/$expect"-part[0-9]*; do
        [ -e "$link" ] || continue
        target="$(readlink -f "$link" 2>/dev/null)" || continue
        [ -n "$target" ] || continue
        dev="${target#/dev/}"
        [ -r "/sys/class/block/$dev/dev" ] || continue
        [ -r "/sys/class/block/$dev/../dev" ] || continue
        # A partition node's sysfs parent is its whole disk. Anything whose
        # parent is not the expected disk merely spells like it, and is skipped
        # rather than trusted.
        [ "$(cat "/sys/class/block/$dev/../dev" 2>/dev/null)" = "$diskmm" ] || continue
        mm="$(cat "/sys/class/block/$dev/dev" 2>/dev/null)" || continue
        [ -n "$mm" ] || continue
        resolved=1
        if [ "$mm" = "$have" ]; then
            return 0
        fi
    done
    if [ "$resolved" = 1 ]; then
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
              "A backup of it would be a backup of nothing. FileBackup itself stops with exit 4 when the source is" \
              "empty at the start of a run while the latest state holds rows (its call C39), leaving the latest state" \
              "as it was; this refusal answers first, before the drive spins up or a container starts, and names the cause."
preflight_mount "$FB_SOURCE" library

BACKUP_MOUNT="$(mount_of "$FB_STORE")"
[ -n "$BACKUP_MOUNT" ] && [ "$BACKUP_MOUNT" != "/" ] \
    || refuse "the library store $FB_STORE resolves to the ROOT filesystem ('${BACKUP_MOUNT:-?}') — the backup drive is not mounted." \
              "Writing there would put the library's whole backup store and snapshot history on the system disk."
preflight_mount "$BACKUP_MOUNT" backup-drive

# ONE MOUNT FOR backup/ AND changes/. FileBackup moves objects between them by
# rename only, and refuses (exit 2) a pair a rename cannot cross; inside the
# container both are under the one /store bind, so the only way to split them is
# a second mount under the store on the host, which the bind would carry along.
# Asked here because here it costs nothing and names the host path.
for _d in "$FB_BACKUP" "$FB_CHANGES"; do
    _m="$(mount_of "$_d")"
    [ "$_m" = "$BACKUP_MOUNT" ] \
        || refuse "$_d is on '$_m' but the library store $FB_STORE is on '$BACKUP_MOUNT'." \
                  "backup/ and changes/ must be ONE mount: FileBackup moves content between them by rename only, and" \
                  "refuses (exit 2) a pair a rename cannot cross. Unmount whatever is mounted under $FB_STORE."
done

# ── 2b. THE STORE LAYOUT: refuse while the OLD three-mount store holds data ───
# Before the bounded engine, the store was two sibling folders bound as /backup
# and /changes (plus a source-side record on /state). Starting the new layout
# beside an unmigrated old store would not lose anything, but it would be the
# worst kind of silent: a multi-day first backup into an empty store, doubling
# the space on the drive, while the real history sat unused next to it. The
# Owner ruled (2026-10-27) that the old store is SET ASIDE, never migrated or
# deleted, and a new store takes its place. So an old folder that still holds
# anything is a REFUSAL that names the set-aside step; an EMPTY one is only a
# note (nothing to set aside, nothing to lose).
# A path that is both old and new (a store configured onto the old folders) is
# not "old" at all and is skipped.
legacy_store_guard() {
    local old="$1" new="$2"
    [ "$old" != "$new" ] || return 0
    [ -e "$old" ] || [ -L "$old" ] || return 0
    if [ -d "$old" ] && [ ! -L "$old" ] && ! dir_has_entries "$old"; then
        DEGRADED="${DEGRADED:+$DEGRADED; }an EMPTY folder from the old store layout is still at $old (rmdir it by hand)"
        warn "store layout: $old is empty and left over from the old three-mount layout — remove it by hand: rmdir $old"
        return 0
    fi
    refuse "the old three-mount store is still in place: $old holds the old engine's store." \
           "This image needs ONE store, $FB_STORE, holding backup/ and changes/, and the old store is set aside first," \
           "never migrated or deleted: rename its folders into one folder of their own on the same drive." \
           "Nothing was created and no container ran. README: \"Bringing in a new store\", then start the backup again."
}
legacy_store_guard "$FB_LEGACY_BACKUP" "$FB_BACKUP"
legacy_store_guard "$FB_LEGACY_CHANGES" "$FB_CHANGES"

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

_total="$(df_field "$(existing_ancestor "$FB_STORE")" 2)"
_used="$(df_field  "$(existing_ancestor "$FB_STORE")" 3)"
_avail="$(df_field "$(existing_ancestor "$FB_STORE")" 4)"
[ -n "${_avail:-}" ] \
    || refuse "cannot read the free space on $BACKUP_MOUNT (df returned nothing) — refusing rather than treating 'could not ask' as 'there is room'"
[ -n "${_total:-}" ] && [ "$_total" -gt 0 ] && FILL_PCT="$(( _used * 100 / _total ))%"
if [ "$_avail" -lt $(( MIN_FREE_GB * 1000000000 )) ]; then
    refuse "the backup drive is below its floor: $(gb "$_avail") GB free on $BACKUP_MOUNT, floor is ${MIN_FREE_GB} GB (${FILL_PCT} full)." \
        "THERE IS NO AUTOMATIC RETENTION (Q-FB2) — nothing here deletes a snapshot, so this will not clear itself." \
        "The manual relief valve, in this order:" \
        "  cd $STACK_DIR && docker compose --profile filebackup run --rm -T $FB_SERVICE snapshots" \
        "  docker compose --profile filebackup run --rm -T -e FILEBACKUP_DRY_RUN=1 $FB_SERVICE prune -Snapshot <name>   # dry run first (-e, not a host variable)" \
        "NEVER delete a Snapshot_* folder by hand — it is the only copy of the states it holds." \
        "Or lower BACKUP_LIBRARY_MIN_FREE_GB in backup.env if this floor is simply too high for this drive."
fi
log "capacity: backup drive $(gb "$_avail") GB free of $(gb "$_total") GB (${FILL_PCT} full, floor ${MIN_FREE_GB} GB)"

_sa="$(df_field "$(existing_ancestor "$FB_LOGS")" 4)"
[ -n "${_sa:-}" ] \
    || refuse "cannot read the free space for $FB_LOGS — refusing rather than guessing"
if [ "$_sa" -lt $(( MIN_FREE_STATE_GB * 1000000000 )) ]; then
    refuse "only $(gb "$_sa") GB free for $FB_LOGS (floor ${MIN_FREE_STATE_GB} GB)." \
        "This is usually the SYSTEM DISK — filling it stops Docker and every service on this box," \
        "so the backup is refused here rather than allowed to take the hub down with it."
fi
log "capacity: logs $(gb "$_sa") GB free (floor ${MIN_FREE_STATE_GB} GB)"

# ── 4. PRE-CREATE THE BIND PATHS ─────────────────────────────────────────────
# DOCKER CREATES A MISSING BIND SOURCE AND THEN CHOWNS IT, and the data drives
# are FAT-family (exfat/ntfs, no native ownership), so that chown returns EPERM
# and takes the ENTIRE compose command down — not just this service. Already bit
# this project once on 2026-08-08, on a box that had installed perfectly.
# Docker leaves a directory that already exists alone, so the fix is ordering.
#
# TWO ARE ASSERTED, NOT CREATED. The config file and the library source:
# creating either would be manufacturing the thing whose absence is the fault.
# mkdir'ing a library over an unmounted drive is exactly the silent green step 2
# just refused.
#
# AND backup/ AND changes/ ARE NEVER CREATED HERE. They are no bind of their
# own (docker binds the store), FileBackup creates them itself on a new store,
# and it refuses (exit 2) a store whose backup/ is gone while changes/ holds
# snapshots - one that lost its latest state must not be answered with a fresh
# full backup beside its history - which it can only see if backup/ is really
# missing.
[ -r "$FB_CONFIG" ] \
    || refuse "the container's config $FB_CONFIG is missing or unreadable." \
              "It is a TRACKED template (stack/backup/filebackup-store.json) installed by autoinstall late-command 5c, and by hand at the cutover (README \"The layout knob\")." \
              "Without it the container exits 2 having done nothing; refuse here instead, where the message is useful." \
              "By hand:  install -m0644 -o root -g root $STACK_DIR/backup/filebackup-store.json $FB_CONFIG"
[ -d "$FB_SOURCE" ] || refuse "the library source $FB_SOURCE is not a directory"
for _d in "$FB_STORE" "$FB_LOGS"; do
    if [ ! -d "$_d" ]; then
        mkdir -p "$_d" || { FAIL_NOTE="cannot create the bind path $_d"; false; }
        log "created bind path $_d"
    fi
done
# /logs is on the SYSTEM disk (ext4), where ownership is real and a
# root-created directory is unwritable to the container — the first lab run
# died on exactly that ('/logs/Backup_Global.log' Access denied). chown works
# there and is the fix. The store paths are NOT chowned: NTFS/exFAT ownership
# comes from the mount options alone (Q-FB5) and chown is a no-op or an error
# there — the write-probe below is what proves those.
chown "$FB_UID:$FB_UID" "$FB_LOGS" \
    || refuse "cannot chown $FB_LOGS to uid $FB_UID — the container cannot write its logs without it."
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
# The store itself, which docker binds, and each child that exists:
# FileBackup writes its one-mount check directory into both children. A child
# that is not there yet is FileBackup's to create (step 4).
probe_write "$FB_STORE"
for _d in "$FB_BACKUP" "$FB_CHANGES"; do
    if [ -d "$_d" ]; then probe_write "$_d"; fi
done
# The ext4 log directory too: a probe is cheaper than the container failing on
# its first log line, and it catches a future provisioner re-owning it.
probe_write "$FB_LOGS"

# ── 5b. AN OLD ENGINE'S STAGING FOLDER (hub recovery §8, D8) ─────────────────
# The engine before the bounded run loop staged a set in <change root>/Temp, and
# a run killed from outside - the recovery supervisor's early kill, a graceful
# recovery reboot, a power cut - left Temp behind and made every later run
# refuse until a person recovered it. Temp may hold the only copy of superseded
# data, so NOTHING here touches it.
#
# THE REFUSAL THAT USED TO BE HERE HAS GONE, AS THIS COMMENT SAID IT MUST. The
# bounded engine (FileBackup BoundedRebuild) never uses Temp: a killed run leaves
# only Incoming/ and Building_* behind, which hold no only-copy and which the
# next run deletes itself. An old Temp gets a warning from FileBackup and is left
# alone while the run goes on, so refusing here would block every backup forever
# over a folder the engine no longer needs. (An interrupted PRUNE can still leave
# one; it is handled the same way.)
#
# What stays is the SIGNAL, said here where it is cheap and named: which
# recovery event interrupted the run, so the Owner is not left reconstructing it
# from a container log. It goes into the run summary as a DEGRADED note. The
# event is the newest recovery evidence file written after Temp was, and one
# that names the backup (`"interrupts_backup": true`) is preferred over one that
# merely happened since. No event at all says so rather than inventing one.
RECOVERY_EVENTS_DIR="${RECOVERY_EVENTS_DIR:-/var/lib/homehub-recovery/events}"
staging_recovery_event() {
    local temp="$1" named="" any="" f
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        any="$f"
        if grep -q '"interrupts_backup": true' "$f" 2>/dev/null; then named="$f"; fi
    done < <(find "$RECOVERY_EVENTS_DIR" -maxdepth 1 -name '*.json' -newer "$temp" 2>/dev/null | sort || true)
    f="${named:-$any}"
    if [ -n "$f" ]; then
        f="$(basename "$f")"
        printf '%s' "${f%.json}"
    else
        printf 'none recorded'
    fi
}
if [ -d "$FB_CHANGES/Temp" ]; then
    _interrupted_by="$(staging_recovery_event "$FB_CHANGES/Temp")"
    log "LIBRARY BACKUP NOTE: old staging needs recovery by hand (interrupted by recovery event $_interrupted_by); the backup runs anyway"
    warn "  $FB_CHANGES/Temp was left by an older FileBackup or an interrupted prune. It may hold the only copy of"
    warn "  superseded content, so it is left exactly as it is; the bounded engine ignores it. Recover it by hand per"
    warn "  FileBackup's README, \"A run refuses because \`Temp\` exists\". Recovery evidence: homehub-recovery events"
    DEGRADED="${DEGRADED:+$DEGRADED; }old staging $FB_CHANGES/Temp needs recovery by hand (recovery event: $_interrupted_by)"
fi

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
    ( cd "$STACK_DIR" && docker compose --profile filebackup run --rm -T "$FB_SERVICE" "$@" )
}

# count_snapshots : how many Snapshot_* trees the change root holds. A run that
# supersedes nothing creates none, so this is how "was a snapshot created" gets
# ANSWERED rather than assumed — the feed summary carries the answer.
count_snapshots() {
    find "$FB_CHANGES" -mindepth 1 -maxdepth 1 -type d -name 'Snapshot_*' 2>/dev/null | grep -c . || true
}

# set_log_count TEXT : how many lines of the set log carry TEXT (a fixed
# string). FileBackup starts backup.log afresh in every run that takes its lock,
# so after a completed run the file holds that run alone. An absent or
# unreadable log counts 0: the message then says less, and the exit code is
# unchanged. `|| true` because grep -c exits 1 on no match and errtrace would
# fire the ERR trap inside the substitution.
set_log_count() {
    local log="$FB_CHANGES/backup.log" n=""
    [ -r "$log" ] && n="$(grep -cF -- "$1" "$log" 2>/dev/null || true)"
    case "$n" in ''|*[!0-9]*) n=0 ;; esac
    printf '%s' "$n"
}

# log_slice_peak WHEN — the backup's exact memory peak (hub recovery C4).
# The filebackup service runs under homehub-filebackup.slice (compose
# cgroup_parent), and the SLICE's cgroup outlives each `--rm` container, so its
# memory.peak is still readable after the run. It is the peak since boot ACROSS
# runs: an upper bound for this one, never a lower one - which is the direction
# a memory limit must be sized from. A diagnostic, so it can never fail the run.
FILEBACKUP_SLICE_CGROUP="${FILEBACKUP_SLICE_CGROUP:-}"
log_slice_peak() {
    local cg="$FILEBACKUP_SLICE_CGROUP" peak=""
    if [ -z "$cg" ]; then
        cg="$(systemctl show -p ControlGroup --value homehub-filebackup.slice 2>/dev/null || true)"
        [ -z "$cg" ] || cg="/sys/fs/cgroup$cg"
    fi
    [ -z "$cg" ] || peak="$(cat "$cg/memory.peak" 2>/dev/null || true)"
    case "$peak" in
        ''|*[!0-9]*)
            log "filebackup slice memory.peak: unavailable ($1; ${cg:-homehub-filebackup.slice is not active}) - firstboot step 6g installs the slice" ;;
        *)
            log "filebackup slice memory.peak: $peak bytes ($(( peak / 1048576 )) MiB) $1 - the peak since boot across runs, an upper bound for this one" ;;
    esac
}
SNAPSHOT_BEFORE="$(count_snapshots)"
run_state backing-up

# THE LEVEL IS SAID OUT LOUD BEFORE THE RUN, because it is the one knob whose
# wrong value is invisible afterwards: a run at -mx=9 and a run at -mx=5 both
# exit 0 and both make a snapshot, and only the CPU-hours differ. stack/.env is
# what compose interpolates, so what it says is what the container gets; unset
# means compose's inline default (5), and the container's own fallback (9) is
# only reachable through an older docker-compose.yml.
log "7-Zip level for compressed files: FILEBACKUP_7Z_LEVEL=${FILEBACKUP_7Z_LEVEL:-<unset: compose defaults it to 5>} (the Owner's ruling is 5 = Normal; FileBackup alone would use 9)"
log "running: docker compose --profile filebackup run --rm -T $FB_SERVICE backup"
BACKUP_RC=0
fb_run backup || BACKUP_RC=$?
log_slice_peak "after backup (exit $BACKUP_RC)"
SNAPSHOT_AFTER="$(count_snapshots)"
SNAPSHOT_MADE=no
[ "${SNAPSHOT_AFTER:-0}" -gt "${SNAPSHOT_BEFORE:-0}" ] && SNAPSHOT_MADE=yes

# Re-read the fill AFTER the run: the number in the summary should describe the
# drive the operator has now, not the one the preflight saw.
_total="$(df_field "$FB_BACKUP" 2)"; _used="$(df_field "$FB_BACKUP" 3)"; _avail="$(df_field "$FB_BACKUP" 4)"
[ -n "${_total:-}" ] && [ "$_total" -gt 0 ] && FILL_PCT="$(( _used * 100 / _total ))%"

# THE BACKUP-ACTION EXIT MAP (FileBackup's bounded-run-loop plan §6). Four
# codes, four distinct causes, four messages — one generic "backup failed" would
# put a human in the container log every time, which is the finding this table
# exists to close. The set log ($FB_CHANGES/backup.log) names every file.
#
#   0  every file captured                       -> on to the verify gate
#   1  the run COMPLETED, but some files were    -> on to the verify gate, then
#      not captured (in use, unreadable, or an      FAIL: not a verified success
#      unportable name, which since FileBackup
#      C42 includes paths differing only in
#      letter case), and/or a stored object was
#      found DAMAGED by its listing and set aside
#      into changes/Verify/ (C37; [DamagedObject]
#      lines in the set log, counted below)
#   2  refused before changing anything; a       -> FAIL now, "do not retry"
#      person must fix it (since the 2026-10-26
#      review this includes a required dependency
#      that cannot be loaded, once an unhandled 1)
#   4  stopped by a host problem, or FileBackup's -> FAIL now, "retry later" (the
#      own lock is held, or (C39) the source was    next timer run is the retry)
#      EMPTY at the start while the latest state
#      holds rows - an unmounted library looks
#      exactly like that
#
# ONLY 0 IS A VERIFIED SUCCESS (the Owner's lane, SR-018). An exit 1 is "partial,
# no data lost", but a file that was never captured is a file the lane would
# otherwise call backed up, so it advances nothing: the age grows and the unit
# fails, which is the warning. The verify gate still runs after a 1, because the
# store is consistent and a file that stays locked for weeks must not also stop
# the weekly proof from ever running.
#
# A backup never returns 3 any more: the manifest witness is gone. A 3 here
# means an image OLDER than the one this file is written for.
#
# THE ONE THING READ OUT OF THE SET LOG is the count of [DamagedObject] lines,
# and only after an exit 1. It is the difference between "a file was in use"
# and "the store itself lost bytes" (FileBackup C37: an object a power cut left
# empty or the wrong length), which the message must not blur: the first is
# routine, the second means a snapshot may no longer restore that file. Any
# other detail stays in the log, named, rather than parsed here.
BACKUP_PARTIAL=0
DAMAGED_SET_ASIDE=0
case "$BACKUP_RC" in
    0) log "container: backup COMPLETE (exit 0) — every file captured; snapshot created: $SNAPSHOT_MADE, drive $FILL_PCT full" ;;
    1) BACKUP_PARTIAL=1
       DAMAGED_SET_ASIDE="$(set_log_count '[DamagedObject]')"
       log "container: backup COMPLETED, NOT A FULL SUCCESS (exit 1) — snapshot created: $SNAPSHOT_MADE, drive $FILL_PCT full."
       log "  Each file it could not capture kept its previous row and is named in $FB_CHANGES/backup.log: a file in"
       log "  use or unreadable, or a name Windows or Linux cannot restore (paths differing only in letter case count)."
       if [ "$DAMAGED_SET_ASIDE" -gt 0 ]; then
           log "  AND $DAMAGED_SET_ASIDE STORED OBJECT(S) WERE FOUND DAMAGED ([DamagedObject] in that log): empty, or a raw"
           log "  object of the wrong length, as a power cut can leave one. Each was moved into $FB_CHANGES/Verify/"
           log "  or, where that move failed, left in place (its log line says which); never deleted, never reused."
           log "  Its content is stored again only while a source file still holds it,"
           log "  so a snapshot that needed it may no longer restore that file. README \"Damaged objects\"."
       fi
       log "  Not a verified success." ;;
    2) FAIL_NOTE="FileBackup REFUSED the run before changing anything (exit 2) — a person must fix it; retrying will not help. The causes are: $FB_CONFIG cannot be loaded or violates the closed schema (FileBackup's container/FileBackup.schema.json), a required tool or dependency that cannot be loaded (7-Zip, the hashing package), the source root missing, backup/ and changes/ not on one mount (the rename test failed), or a latest manifest that cannot be read (move $FB_BACKUP/MANIFEST.csv aside to rebuild the latest state from the source; the snapshots are untouched). The reason is in $FB_CHANGES/backup.log and $FB_LOGS/Backup_Global.log"
       false ;;
    3) FAIL_NOTE="the container exited 3, which a bounded-run-loop FileBackup never returns from a backup (the manifest witness is gone). This is almost certainly an OLDER image: this stack needs one built from FileBackup's BoundedRebuild work. Rebuild it (scripts/ensure-local-images.sh --rebuild) and read $FB_LOGS/Backup_Global.log"
       false ;;
    4) FAIL_NOTE="FileBackup STOPPED for a host problem (exit 4): the backup drive is full or went away; another FileBackup run holds its lock ($FB_CHANGES/Temp.lock — a manual verify or prune?); or the library source was EMPTY at the start of the run while the latest state holds rows, which is exactly what an unmounted or failed library drive looks like (FileBackup C39 — the latest state was left as it was). Nothing stored is lost; a stop is no worse than a kill, and the next run keeps the progress. Retry later: the next timer run does that by itself, or fix the host (is $FB_SOURCE mounted and populated?) and run systemctl start homehub-library-backup.service. Read $FB_CHANGES/backup.log"
       false ;;
    125|126|127) FAIL_NOTE="docker itself failed (exit $BACKUP_RC) before FileBackup ran — usually a missing image. Is filebackup:\${FILEBACKUP_IMAGE_TAG} loaded? \`docker image ls filebackup\`; it is baked by vmtest/export-images.sh and loaded by firstboot"
       false ;;
    *) FAIL_NOTE="the container exited $BACKUP_RC, which is outside the documented 0/1/2/4 table — treat it as a failure and read $FB_LOGS/Backup_Global.log"
       false ;;
esac

# ── 7b. THE SYSTEM-DISK MANIFEST COPY (frame-freshness, IF-035) ──────────────
# After a COMPLETED run (0 or 1) the latest manifest is final, and the drive is
# awake anyway, so this is where the copy is cheap. Published by rename on the
# system disk, so a reader never sees a half-written file (the reason the old
# reader had to compare row counts against a witness). A failure here is a
# NOTE, never a backup failure: the copy serves another lane, and that lane
# reads a stale copy as "not changed since", which is the same answer it gave
# when a backup failed under the old layout.
publish_manifest_copy() {
    local src="$FB_BACKUP/MANIFEST.csv" dir tmp
    dir="$(dirname "$FB_MANIFEST_COPY")"
    if [ ! -f "$src" ]; then
        DEGRADED="${DEGRADED:+$DEGRADED; }no latest manifest at $src to copy for frame-freshness"
        warn "manifest copy: $src does not exist; $FB_MANIFEST_COPY was not refreshed"
        return 0
    fi
    if mkdir -p "$dir" 2>/dev/null \
       && tmp="$(mktemp "$dir/.MANIFEST.csv.XXXXXX" 2>/dev/null)" \
       && cp -- "$src" "$tmp" 2>/dev/null \
       && mv -f -- "$tmp" "$FB_MANIFEST_COPY" 2>/dev/null; then
        log "manifest copy: $FB_MANIFEST_COPY refreshed from $src (for frame-freshness)"
    else
        [ -n "${tmp:-}" ] && rm -f -- "$tmp" 2>/dev/null
        DEGRADED="${DEGRADED:+$DEGRADED; }could not refresh $FB_MANIFEST_COPY for frame-freshness"
        warn "manifest copy: could not refresh $FB_MANIFEST_COPY from $src"
    fi
    return 0
}
publish_manifest_copy

# ── 8. THE VERIFY GATE — weekly, deep monthly ────────────────────────────────
# A backup nobody reads is a hypothesis. `verify` re-proves the stored manifest,
# reports any object stored twice (DuplicateObject) and, since FileBackup C37,
# any object whose length its name rules out (DamagedObject: empty, or a raw
# object of another length); -Deep additionally proves payload identity,
# hashing every object against its name (a mismatch is also DamagedObject),
# which costs real time and so is monthly rather than weekly.
# REPORT ONLY: this gate never passes FILEBACKUP_REPAIR. Moving duplicates and
# damaged objects into $FB_CHANGES/Verify/ is a person's call (README).
#
# MEMORY - NOT YET SAFE ON THE HUB'S STORE (FileBackup C43, 2026-10-27). Until
# FileBackup's follow-up to C43 lands, a verify (its pool-resolve check) still
# holds every snapshot manifest in memory at once: about 420 MB per HomeHub
# manifest, the mechanism of the 2026-10-04 outage. This gate is scheduled by
# the calendar below and NOTHING here stops it yet - deliberately, because
# disabling it is the Owner's call, recorded in docs/status.md. What would
# gate it: running the unit with --verify none (ExecStart), or a knob read
# here, until an image with that follow-up is deployed; FILEBACKUP_MEM_LIMIT
# would turn a runaway into an OOM kill of this container instead of the box.
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
# A verify failure ends the run before the partial-backup check below, so its
# message must carry that fact itself.
PARTIAL_SUFFIX=""
if [ "$BACKUP_PARTIAL" = 1 ]; then
    PARTIAL_SUFFIX=". The backup itself also completed with files not captured (exit 1; see $FB_CHANGES/backup.log)"
    if [ "$DAMAGED_SET_ASIDE" -gt 0 ]; then
        PARTIAL_SUFFIX="$PARTIAL_SUFFIX and $DAMAGED_SET_ASIDE damaged stored object(s) set aside into $FB_CHANGES/Verify/"
    fi
fi
if [ "$VERIFY_RUN" != none ]; then
    VERIFY_ARGS=(verify)
    [ "$VERIFY_RUN" = deep ] && VERIFY_ARGS+=(-Deep)
    log "verify gate: running ${VERIFY_ARGS[*]} (mode=$VERIFY_RUN, weekday $(date +%u), day $(date +%-d))"
    # A deep verify on the monthly pass is the long half of the bimodal runtime.
    # Naming the phase is what stops that looking like a hung backup.
    run_state verifying
    VERIFY_RC=0
    fb_run "${VERIFY_ARGS[@]}" || VERIFY_RC=$?
    log_slice_peak "after $VERIFY_RUN verify (exit $VERIFY_RC)"
    # The VERIFY table (FileBackup SR-049/SR-040), a different table from the
    # backup one above: 0 clean · 1 findings · 2 precondition. Verify reads, so
    # a host problem stops it before it classifies anything and is reported as
    # 2; it never returns 3 (no witness) or 4. The findings are a JSON document
    # on the container's stdout (the journal) and one WARN line each in
    # $FB_LOGS/Backup_Global.log.
    case "$VERIFY_RC" in
        0) VERIFY_NOTE="$VERIFY_RUN ok"
           log "verify: CLEAN (exit 0)" ;;
        1) VERIFY_NOTE="$VERIFY_RUN FINDINGS"
           FAIL_NOTE="verify exit 1 — FINDINGS: the store disagrees with its manifests somewhere. Read the [Class] lines in $FB_LOGS/Backup_Global.log. PoolUnresolvable is a row with no bytes anywhere (not restorable until the next backup re-stores it from the source); DuplicateObject is a second copy of one object (harmless, costs space — expected once after a store from the old engine); DamagedObject is an object whose length its name rules out (empty, or a raw object of another length — any verify) or, under -Deep, whose bytes do not hash to its name. Moving duplicates and damaged objects aside into $FB_CHANGES/Verify/ (never deleted) is a person's call: README \"Verify findings\"$PARTIAL_SUFFIX"
           false ;;
        2) VERIFY_NOTE="$VERIFY_RUN REFUSED (precondition)"
           FAIL_NOTE="verify exit 2 — precondition failure: the verify could not run at all (no backup root, no manifest to verify, -Deep without 7-Zip, or a host problem that stopped it). Retrying unchanged will not help. Read $FB_LOGS/Backup_Global.log"
           false ;;
        3|4) VERIFY_NOTE="$VERIFY_RUN exit $VERIFY_RC"
           FAIL_NOTE="verify exited $VERIFY_RC, which a bounded-run-loop FileBackup never returns from verify (no witness; host problems are 2). This is almost certainly an OLDER image: rebuild it from FileBackup's BoundedRebuild work"
           false ;;
        *) VERIFY_NOTE="$VERIFY_RUN exit $VERIFY_RC"
           FAIL_NOTE="verify exited $VERIFY_RC, outside the documented 0/1/2 table"
           false ;;
    esac
else
    log "verify gate: not today (mode=$VERIFY_MODE, weekday $(date +%u), verify day $VERIFY_DAY)"
fi

# A backup that completed with files not captured (exit 1) ends HERE, after the
# verify gate had its turn: not a verified success, so no lastSuccess, and a
# non-zero exit so the unit and the hub watch (watch.table `library-backup`)
# show it. The data already stored is safe, and the message says so.
if [ "$BACKUP_PARTIAL" = 1 ]; then
    if [ "$DAMAGED_SET_ASIDE" -gt 0 ]; then
        FAIL_NOTE="the backup COMPLETED BUT FOUND $DAMAGED_SET_ASIDE DAMAGED STORED OBJECT(S) (container exit 1; verify=$VERIFY_NOTE). Each [DamagedObject] line in $FB_CHANGES/backup.log names an object that was empty or the wrong length — what a power cut can leave — and was moved into $FB_CHANGES/Verify/ (or, where that move failed, left in place; the line says which), never deleted and never reused. Its content is stored again only while a source file still holds it, so a snapshot that needed it may no longer restore that file. Any file not captured is named in the same log. This is not a verified success and lastSuccess is NOT advanced. README \"Damaged objects\""
    else
        FAIL_NOTE="the backup COMPLETED WITH FILES NOT CAPTURED (container exit 1; verify=$VERIFY_NOTE). Every file it could read is stored and nothing already stored was lost, but at least one file was not captured, so this is not a verified success and lastSuccess is NOT advanced. Each file is named in $FB_CHANGES/backup.log — usually a file in use or unreadable, or a name Windows or Linux cannot restore (paths differing only in letter case count); the next run tries it again"
    fi
    false
fi

# ── 9. ADVANCE VERIFIED SUCCESS (SR-018 / IF-010) ────────────────────────────
# AFTER the container and verification exit successfully, and it can only be
# after: the container has `network_mode: none` and no way to reach the tracker.
# A failure above deliberately leaves the previous timestamp untouched. Only a
# backup exit 0 reaches this line (exit 1 ended above).
trap - ERR
# The old engine's source-side record is no longer read or written by anything.
# Said once per verified run, because only a person may delete it (nothing here
# deletes), and this is the moment it is known to be unneeded.
if [ -f "$FB_LEGACY_STATE/MANIFEST.csv" ]; then
    log "NOTE: $FB_LEGACY_STATE/MANIFEST.csv is the retired source-side record of the old engine; nothing reads it"
    log "  any more and this verified run did not need it. Delete it by hand when convenient: rm -r $FB_LEGACY_STATE"
fi
SUMMARY="exit=$BACKUP_RC snapshot=$SNAPSHOT_MADE drive=$FILL_PCT full; verify=$VERIFY_NOTE"
[ -n "$DEGRADED" ] && SUMMARY="$SUMMARY; DEGRADED: $DEGRADED"
SUCCESS_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
post_file_share_backup_state lastSuccess "$SUCCESS_AT"
# CAPTURE IT NOW. FILE_SHARE_BACKUP_STATE_LAST_CODE is one global, overwritten by
# every post, and the never-silent-green check below is about THIS post. Reading
# the global after the idle post below inverted the contract both ways: a failed
# lastSuccess followed by a successful idle exited 0 with the tracker never told
# a backup happened, and a successful lastSuccess followed by a failed idle
# reported the whole run failed after freshness was already durable.
SUCCESS_POST_CODE="$FILE_SHARE_BACKUP_STATE_LAST_CODE"
# Withdraw the in-progress claim only AFTER freshness is recorded. In this order
# the lane can never show "idle at the old age" for even one poll: the success
# lands first, so the phase clearing reveals green rather than yesterday.
run_state idle
# An unconfigured state endpoint remains a benign, explicit sim condition. On
# the hub a configured endpoint that rejects or drops the update fails the unit:
# otherwise the wall cannot distinguish a completed backup from one that never
# ran, even though the archive itself remains valid.
if [ "$SUCCESS_POST_CODE" = "skipped" ]; then
    log "NOTE: no FILE_SHARE_BACKUP_STATE_URL in $CONFIG, so this verified success advanced no panel state."
    log "  Correct for a sim box; on the hub it means the combined item cannot age correctly."
    log "  Summary that went nowhere: $SUMMARY"
elif [ "$SUCCESS_POST_CODE" != "200" ]; then
    log "FATAL: the library backup itself succeeded, but its verified-success update did not land (HTTP $SUCCESS_POST_CODE)."
    log "  Exiting non-zero so this unit reports FAILED. A backup nobody was told about is"
    log "  not a backup that reported — from outside this box it looks exactly like a run"
    log "  that never happened, and that is the one thing this contract forbids."
    log "  Summary that could not be delivered: $SUMMARY"
    exit 1
fi
log "== library backup OK — $SUMMARY =="
exit 0
