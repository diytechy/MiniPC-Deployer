#!/usr/bin/env bash
# backup.sh — the AWOW bash backup service (WI-10.10, built/validated in WI-10.15).
#
# Runs the explicit six-step pipeline from HOMELAB_TOPOLOGY.md, 100% bash:
#   1a. wake          — optional WAKE-ON-LAN pre-step for a source box that is
#                        allowed to sleep (BACKUP_WAKE_MAC; wait for tcp/445,
#                        LOUD failure on timeout)
#   1b. INGEST        — mirror the configured network share(s) INTO the library
#                        tree (INGEST_SOURCES: name=//host/share -> /abs/dest;
#                        cifs-mount + `rsync -a --delete` + unmount), so the
#                        library holds the CURRENT copy and the ordinary archive
#                        flows below cover it like any other folder
#                        (ratified 2026-07-29). MIRROR: deletions propagate.
#   1c. source pulls  — one BACKUP_SOURCES table, three source kinds (SR-013):
#                        //host/share cifs-mount+rsync; volume:VOL[@CONTAINER]
#                        rsync from the docker volume's mountpoint, optionally
#                        quiescing @CONTAINER (stop→copy→restart, EXIT-trap
#                        safety net); path:/dir local rsync
#   2. archive+compress — tar per set, zstd WHERE APPLICABLE (already-compressed
#                        sets stored as plain .tar — FileBackup spec), minus the
#                        EXCLUDED patterns (BACKUP_EXCLUDE globally + per-set
#                        `name.exclude=` lines) — every exclusion is logged and
#                        recorded in the MANIFEST, never silent
#   3. hash+verify+manifest — per-file sha256 table + archive sha256 + integrity
#                        test; a recovery MANIFEST that restore.sh reconstructs from
#   4. external-drive target — dated run snapshot under BACKUP_TARGET, with
#                        retention/rotation (keep last BACKUP_KEEP)
#   (there is no step 5. The offsite step was retired as a design on 2026-07-29
#    and the code DELETED on 2026-08-09: the IceDrive client on the box is
#    pointed at library paths in its own GUI, and this service stages nothing.
#    Any surviving OFFSITE_* knob is REFUSED at run start rather than ignored.)
#   6. report         — POST NagLight /api/feed; NEVER-SILENT-GREEN: any failure
#                        posts ok=false and exits nonzero — the ERR trap AND
#                        every `die` path (OI-9)
#
# Usage: backup.sh [--config PATH] [--plan]
#   --config  path to backup.env (default: /etc/homehub-backup/backup.env, else the
#             backup.env next to this script)
#   --plan    plan the run and report on it; write no ARCHIVES. It was called
#             `--dry-run` until 2026-08-29, and that name was retired because it
#             was not true (C25, the Owner's ruling: the behaviour is fine, the
#             word was not). `--dry-run` is still accepted so an older caller
#             keeps working, and prints a one-line notice.
#
#             WHAT A PLAN RUN STILL DOES, in full — none of it is new, all of it
#             was always true, and the name is what changed:
#               * CREATES $BACKUP_TARGET/run_<ts>/ ON THE BACKUP DRIVE and
#                 writes backup.log, a header-only MANIFEST.tsv, and one
#                 <set>.excluded.log per set into it (~16 files);
#               * MOUNTS every cifs source read-only, and unmounts it again;
#               * ISSUES `hdparm -S 0` against BACKUP_DRIVE_DEVICES to hold
#                 standby off, restoring the timeout on exit — so on a box with
#                 the real archive drive attached, a plan run SPINS IT UP;
#               * MIRRORS NOTHING (rsync gets its own --dry-run) and writes no
#                 archive, no hash table and no RUN.json.
#             It is therefore safe to point at production, and it is NOT
#             read-only. Both halves of that sentence matter.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

CONFIG=""; PLAN_ONLY=0; PLAN_FLAG_USED="--plan"
while [ $# -gt 0 ]; do
    case "$1" in
        --config) CONFIG="$2"; shift 2 ;;
        --plan) PLAN_ONLY=1; shift ;;
        # THE OLD NAME, KEPT DELIBERATELY. verify-hub.sh (TC-H-M02) and any hand
        # habit call this, and a box can be running an older payload than the
        # repo — refusing it would turn a rename into an outage of the one check
        # that notices a set vanishing from BACKUP_SOURCES.
        --dry-run) PLAN_ONLY=1; PLAN_FLAG_USED="--dry-run"; shift ;;
        -h|--help) sed -n '2,60p' "$0"; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
if [ -z "$CONFIG" ]; then
    if   [ -f /etc/homehub-backup/backup.env ]; then CONFIG=/etc/homehub-backup/backup.env
    elif [ -f "$HERE/backup.env" ];         then CONFIG="$HERE/backup.env"
    else die "no config: pass --config or create /etc/homehub-backup/backup.env"; fi
fi
load_config "$CONFIG"

: "${BACKUP_TARGET:?BACKUP_TARGET not set}"
: "${BACKUP_SOURCES:?BACKUP_SOURCES not set (name=//host/share lines)}"
STAGING="${BACKUP_STAGING:-/var/tmp/homehub-backup/staging}"
KEEP="${BACKUP_KEEP:-7}"
# VALIDATED IN THE FIRST SECOND, because both bad values are silent until late.
# BACKUP_KEEP=0 is legal shell and means "keep nothing": retention deleted the
# run it had just written, and the run then died naming the LOGGER, because
# LOG_FILE lived in the directory that had just been removed (S9). A non-numeric
# value reached the arithmetic in retention and killed the run there — after an
# hour of archiving, with no report posted at all.
case "$KEEP" in
    ''|*[!0-9]*) die "config: BACKUP_KEEP='$KEEP' is not a number (it is how many dated runs to keep on the backup drive)" ;;
    0)           die "config: BACKUP_KEEP=0 means 'keep no runs at all', so this run would archive the household and then delete the archive. Set it to 1 or more." ;;
esac
ZL="${BACKUP_ZSTD_LEVEL:-10}"

# ── drive power (WI-10.10 DRIVE POWER DESIGN) ────────────────────────────────
# The configured backup drive(s) + their at-rest spin-down timeout. Empty device
# list = the whole feature is a clean no-op. See common.sh drive_standby_set for
# the hdparm -S encoding and the "power management NEVER fails a backup" contract.
read -r -a BACKUP_DRIVES <<< "${BACKUP_DRIVE_DEVICES:-}" || true
STANDBY_VALUE="${BACKUP_DRIVE_STANDBY:-241}"

# ── 0. TARGET PREFLIGHT — the drive must actually be there ───────────────────
# THIS MUST RUN BEFORE THE mkdir BELOW, and that ordering is the entire point.
#
# The failure it prevents (found 2026-08-01): the generated fstab mounts the
# backup drive with `nofail` — mandatory, or a missing USB disk holds up
# local-fs.target and a headless box drops to an emergency shell. The cost is
# that with the drive unplugged, $BACKUP_TARGET is still a perfectly good empty
# DIRECTORY on the system disk. `mkdir -p "$RUN_DIR"` then succeeds, rsync
# copies into it, verification passes (the files really are there), retention
# prunes happily, and step 6 posts **ok=true**. A green backup lane, onto the
# 119 GB system disk, until it fills.
#
# That is the same silent-green shape samba/library-guard.sh was written to
# forbid on the library drive; the guard simply never got pointed at this one.
#
# NOT done as `RequiresMountsFor=` on the unit, deliberately: systemd would
# refuse to START the service, which means NO report reaches NagLight at all —
# an unreported non-run, which is the failure mode this project cares most about.
# Failing HERE posts ok=false through the normal never-silent-green path.
#
# Zero disk I/O (mount_options_for reads /proc/self/mountinfo), so this is safe
# against a spun-down drive and never wakes it just to check.
if [ "${BACKUP_TARGET_REQUIRE_MOUNT:-true}" = "true" ]; then
    if target_opts="$(mount_options_for "$BACKUP_TARGET")"; then
        case ",$target_opts," in
            *,ro,*)
                # ntfs3 falls back to read-only on a dirty NTFS bit (Windows Fast
                # Startup, unclean eject). Reads look fine, every write fails.
                feed_naglight false "backup target $BACKUP_TARGET is mounted READ-ONLY — refusing to run (NTFS dirty bit? clear it from Windows)"
                die "backup target $BACKUP_TARGET is mounted READ-ONLY — refusing to run. Nothing was written." ;;
        esac
        log "target preflight: $BACKUP_TARGET is a real mountpoint (rw)"
        # A23: say so when the archive is landing on a stand-in drive. This does
        # NOT stop the run — proving the backup works on a cheap disk before
        # committing 8 TB to it is the whole point of the bring-up period — but
        # it must not be invisible either. The composite signal is the honest
        # one: `backup` green (the run worked) + `backup-drive-mounted` yellow
        # (on a substitute). Identity itself is asserted by the health timer;
        # this is only the line in the run log that stops "the backup is green"
        # from being read as "the backup is on the real drive".
        if [ -f /etc/homehub-samba/drive-identity.conf ]; then
            _expect="$(awk -F'\t' -v p="$BACKUP_TARGET" '$1 == p { print $2 }' /etc/homehub-samba/drive-identity.conf)"
            if [ -n "$_expect" ] && [ -e "/dev/disk/by-id/$_expect" ]; then
                _want="$(readlink -f "/dev/disk/by-id/$_expect" 2>/dev/null)"
                _have="$(awk -v p="$BACKUP_TARGET" '$5 == p { d = $3 } END { print d }' /proc/self/mountinfo)"
                _wantmm=""
                [ -n "$_want" ] && [ -r "/sys/class/block/${_want#/dev/}/dev" ] &&
                    _wantmm="$(cat "/sys/class/block/${_want#/dev/}/dev")"
                if [ -n "$_wantmm" ] && [ "$_wantmm" != "$_have" ]; then
                    log "NOTICE: this archive is landing on a STAND-IN drive, not $_expect."
                    log "  Fine during bring-up; check backup-drive-mounted is yellow, not green."
                fi
            fi
        fi
    else
        feed_naglight false "backup target $BACKUP_TARGET is NOT MOUNTED — the backup drive is absent or failed to mount; refusing to run so nothing lands on the system disk"
        die "backup target $BACKUP_TARGET is NOT MOUNTED — the backup drive is absent or failed to mount." \
            "Refusing to run: with nofail in fstab this path is an empty directory on the SYSTEM disk," \
            "so a run would look green while writing the household's backups to the wrong drive." \
            "Check the drive is plugged in and powered, then: systemctl start homehub-backup.service" \
            "(Backing up to a plain directory on purpose? Set BACKUP_TARGET_REQUIRE_MOUNT=false in backup.env.)"
    fi
else
    warn "BACKUP_TARGET_REQUIRE_MOUNT=false — not checking that $BACKUP_TARGET is a mountpoint."
    warn "  A missing drive will be backed up to the system disk and reported GREEN."
fi

# ── 0b. CAPACITY PREFLIGHT — will this run FIT, on both disks ────────────────
# Step 0 above asks whether the target is PRESENT. It never asked whether there
# is room, and that gap is bigger than it sounds because the two disks fail
# differently:
#
#   BACKUP_TARGET full  — the run dies part-way through tar. Bad, recoverable.
#   BACKUP_STAGING full — staging defaults under /var/tmp, i.e. THE SYSTEM DISK.
#                         Every set is copied there in full before it is
#                         archived, so a library larger than the root filesystem
#                         fills root, and a full root takes Docker, Caddy and
#                         every service volume down with it. The backup does not
#                         merely fail; the box does.
#
# THE ESTIMATE COMES FROM THE LAST RUN, deliberately. Measuring the sources costs
# a full metadata walk of the library before the run has done anything useful,
# while the previous RUN.json already records exactly what this run is about to
# write — total_bytes for the target, and the largest per-set bytes for staging
# (one set at a time now that staging is released after each archive). A first
# run has no such record and is allowed to proceed with a warning: refusing a
# backup that has never run is worse than the risk.
free_bytes_at() { df -PB1 "$1" 2>/dev/null | awk 'NR==2 {print $4}'; }
# The staging directory must EXIST before df can be asked about it, and a df
# that fails returns nothing — which would skip the guard silently, the exact
# shape this check exists to refuse. Creating it here is harmless (it is an
# empty directory) and it happens before any RUN_DIR is made, so a refusal
# below leaves no phantom run behind.
mkdir -p "$STAGING" 2>/dev/null || true
_prev="$(find "$BACKUP_TARGET" -mindepth 2 -maxdepth 2 -name RUN.json 2>/dev/null | sort | tail -1)"
if [ -n "$_prev" ] && [ -r "$_prev" ]; then
    _need_t="$(grep -o '"total_bytes": *[0-9]*' "$_prev" | grep -o '[0-9]*' | tail -1)"
    _need_s="$(grep -o '"bytes":[0-9]*' "$_prev" | grep -o '[0-9]*' | sort -n | tail -1)"
    _have_t="$(free_bytes_at "$BACKUP_TARGET")"
    _have_s="$(free_bytes_at "$(dirname "$STAGING")")"
    # 15% headroom: the estimate is last night's, and tonight's library is bigger.
    if [ -n "${_need_t:-}" ] && [ -n "${_have_t:-}" ] && [ "$_have_t" -lt $(( _need_t * 115 / 100 )) ]; then
        feed_naglight false "backup target $BACKUP_TARGET has $(( _have_t / 1000000000 )) GB free but the last run wrote $(( _need_t / 1000000000 )) GB — refusing to start a run that cannot fit"
        die "not enough room on $BACKUP_TARGET: $(( _have_t / 1000000000 )) GB free, last run wrote $(( _need_t / 1000000000 )) GB (+15% headroom required)." \
            "Nothing was written. Lower BACKUP_KEEP, or give the archive a bigger drive."
    fi
    if [ -n "${_need_s:-}" ] && [ -n "${_have_s:-}" ] && [ "$_have_s" -lt $(( _need_s * 115 / 100 )) ]; then
        feed_naglight false "backup staging $STAGING has $(( _have_s / 1000000000 )) GB free but the largest set needs $(( _need_s / 1000000000 )) GB — refusing, because filling this filesystem takes the whole box down"
        die "not enough room for STAGING at $STAGING: $(( _have_s / 1000000000 )) GB free, the largest set needs $(( _need_s / 1000000000 )) GB." \
            "This filesystem is usually the SYSTEM DISK — filling it stops Docker and every service, not just the backup." \
            "Point BACKUP_STAGING at the backup drive, or shrink the largest set."
    fi
    log "capacity preflight: target $(( _have_t / 1000000000 )) GB free (last run wrote $(( _need_t / 1000000000 )) GB), staging $(( _have_s / 1000000000 )) GB free (largest set $(( _need_s / 1000000000 )) GB)"
else
    warn "capacity preflight: no previous RUN.json under $BACKUP_TARGET — cannot estimate, proceeding."
    warn "  If this library is larger than the filesystem holding $STAGING, this run will fill it."
fi

RUN_TS="$(date -u +%Y%m%d_%H%M%S)"
RUN_DIR="$BACKUP_TARGET/run_$RUN_TS"
MANIFEST="$RUN_DIR/MANIFEST.tsv"
mkdir -p "$RUN_DIR" "$STAGING"
LOG_FILE="$RUN_DIR/backup.log"

# Run totals + the report fields, initialised BEFORE the failure machinery below
# so the very first failure can already write a complete RUN.json.
TOTAL_BYTES=0; TOTAL_FILES=0; SET_SUMMARY=""; SET_SUMMARY_JSON=""
INGEST_SUMMARY="none"

write_run_json() {
    local status="$1" note="$2"
    cat >"$RUN_DIR/RUN.json" <<JSON
{
  "run": "$RUN_TS",
  "status": "$status",
  "finished_utc": "$(date -u +%FT%TZ)",
  "ingest": "$INGEST_SUMMARY",
  "sets": [$SET_SUMMARY_JSON],
  "total_files": $TOTAL_FILES,
  "total_bytes": $TOTAL_BYTES,
  "note": "$note"
}
JSON
}

# never-silent-green: any error past this point reports ok=false and exits 1.
FAIL_NOTE=""
REPORTED_FAILURE=0

# Sets skipped because their source directory was absent. Collected rather than
# aborted on (see the `path)` branch), and settled at the very end: the run does
# all the work it can, then finishes RED naming every set it could not reach.
# Empty is the only green state — a partial backup NEVER reports ok.
MISSING_SETS=""

# report_failure NOTE : the ONE failure path — unmount, post ok=false, write the
# failed RUN.json, log. Idempotent: whichever of the ERR trap and `die` gets
# there first owns the verdict, so a die raised inside the trap (or vice versa)
# cannot double-post or overwrite it. Always returns 0 — reporting must not
# invent a second failure.
report_failure() {
    local note="$1"
    [ "$REPORTED_FAILURE" = 0 ] || return 0
    REPORTED_FAILURE=1
    umount_all
    feed_naglight false "backup FAILED: $note"
    write_run_json "failed" "$note"
    log "BACKUP FAILED: $note"
    return 0
}
on_err() {
    local ln="$1"
    report_failure "${FAIL_NOTE:-backup failed at line $ln}"
    exit 1
}
trap 'on_err $LINENO' ERR
set -o errtrace
# OI-9: route `die` through the SAME report. Before this line a die has no feed
# to post to (no config loaded / no run dir yet) and common.sh's no-op default
# applies; after it, EVERY failure path — bad config, a refused cifs mount, a
# wake timeout — posts ok=false before exiting 1.
DIE_REPORTER=report_failure

# ── the offsite step is GONE (removed 2026-08-09, Owner) ─────────────────────
# It was retired as a DESIGN on 2026-07-29 — the IceDrive client is pointed at
# library paths in its own GUI, so this service was never going to stage
# anything again — and the code was then carried for another six weeks as
# "legacy, still functional". This deletes it.
#
# THE REFUSAL BELOW IS THE WHOLE REASON THIS IS SAFE TO DELETE. A box whose
# backup.env still asks for an offsite push must NOT be quietly given a backup
# that has no offsite step: that is a config silently ignored, which is the same
# family as a green run that wrote nothing. So the knobs remain RECOGNISED, and
# recognised means refused with an explanation rather than skipped.
#
# OFFSITE_ENABLED=false stays legal and inert: it is what every current config
# says, and failing those would be noise.
for _o in OFFSITE_PATH OFFSITE_UNC OFFSITE_SETS; do
    if [ -n "$(eval "printf '%s' \"\${$_o:-}\"")" ]; then
        die "config: $_o is set, but the offsite step was REMOVED on 2026-08-09." \
            "The backup service performs no offsite staging at all (ratified 2026-07-29): the IceDrive" \
            "client on the box syncs the chosen library paths itself, from its own GUI." \
            "Delete $_o (and any other OFFSITE_* line) from this config. Nothing replaces it here —" \
            "if the cloud copy matters, check it in the IceDrive client, not in this run's report."
    fi
done
if [ "${OFFSITE_ENABLED:-false}" = "true" ]; then
    die "config: OFFSITE_ENABLED=true, but the offsite step was REMOVED on 2026-08-09." \
        "Set OFFSITE_ENABLED=false or delete the line. See the note above."
fi

# ── drive-power RESTORE on exit (WI-10.10) ───────────────────────────────────
# Re-arm the configured spin-down timeout on ANY exit — normal success, the ERR
# trap's `exit 1`, an interrupt, or a `die`. The EXIT trap fires AFTER the ERR
# trap, so it never disturbs the never-silent-green ok=false reporting path; it
# only restores the drives' at-rest standby (the "restore on exit" half of the
# dynamic-standby policy). No-op when no drives are configured. Never fails
# (drive_standby_set always returns 0).
drive_power_restore() {
    [ "${#BACKUP_DRIVES[@]}" -gt 0 ] || return 0
    log "drive-power: restoring standby timeout ($STANDBY_VALUE = $(standby_desc "$STANDBY_VALUE")) on exit"
    drive_standby_set "$STANDBY_VALUE" "${BACKUP_DRIVES[@]}"
}
# Containers first (a stopped service is the more urgent restore), drives second.
trap 'quiesce_restore; drive_power_restore' EXIT

log "== AWOW backup run $RUN_TS =="
if [ "$PLAN_ONLY" = 1 ]; then RUN_MODE=plan; else RUN_MODE=full; fi
# `mode=` REPLACED `dry_run=` on 2026-08-29 (C25). restore.sh reads this line to
# tell a plan run from a real one and accepts BOTH spellings, because the run
# directories already on the drive carry the old one.
log "config=$CONFIG target=$BACKUP_TARGET keep=$KEEP mode=$RUN_MODE"
if [ "$PLAN_ONLY" = 1 ]; then
    if [ "$PLAN_FLAG_USED" = "--dry-run" ]; then
        log "NOTE: --dry-run is the retired name for --plan and still works. It was retired because it was not true (C25)."
    fi
    # SAID AT THE START, not only at the end. A plan run that dies half way
    # through has still written this directory, and the operator who later finds
    # it on the drive should be able to read why from the run's own log.
    log "PLAN RUN: no archives will be written. This run DOES write $RUN_DIR on $BACKUP_TARGET"
    log "  (backup.log, a header-only MANIFEST.tsv, one <set>.excluded.log per set), mounts any"
    log "  cifs source read-only, and holds drive standby OFF for its duration. Not read-only."
fi
printf 'set\tsource\tarchive\talgo\tarchive_sha256\tfiles\tbytes\treason\texcludes\n' >"$MANIFEST"

# ── drive power: DISABLE standby for the whole run (WI-10.10) ─────────────────
# Turn OFF spin-down on the target drive(s) at run start so long no-write phases
# (source hashing, archive verify) can't spin the drive down mid-backup. Restored
# by the EXIT trap above. No-op / never-fail when unconfigured or unsupported.
if [ "${#BACKUP_DRIVES[@]}" -gt 0 ]; then
    log "drive-power: disabling standby (hdparm -S 0) on ${#BACKUP_DRIVES[@]} target drive(s) for the run"
    drive_standby_set 0 "${BACKUP_DRIVES[@]}"
fi

# ── 1a. Wake-on-LAN pre-step — a source box that is ALLOWED TO SLEEP ─────────
# The Windows game box now exposes one share and may be asleep when the nightly
# timer fires, so we wake it and WAIT for its SMB port before the first cifs
# mount. Empty BACKUP_WAKE_MAC = the whole feature is off (a box that never
# sleeps needs none of this). A timeout is FATAL on purpose: a source that
# failed to wake must never be mistaken for a source with nothing new — and
# thanks to OI-9 this `die` posts ok=false to the feed before exiting.
if [ -n "${BACKUP_WAKE_MAC:-}" ]; then
    [ -n "${BACKUP_WAKE_HOST:-}" ] \
        || die "config: BACKUP_WAKE_MAC is set but BACKUP_WAKE_HOST is not — the wake needs a host/IP to probe"
    WAKE_TIMEOUT="${BACKUP_WAKE_TIMEOUT:-120}"
    wake_and_wait "$BACKUP_WAKE_MAC" "$BACKUP_WAKE_HOST" "$WAKE_TIMEOUT" \
        || die "wake-on-LAN TIMEOUT: $BACKUP_WAKE_HOST did not answer tcp/445 within ${WAKE_TIMEOUT}s — the source box did not wake, so this run copied NOTHING from it (check the box's WoL/fast-startup settings, or that the magic packet reaches its subnet)"
fi

# ── 1b. INGEST — mirror the network share(s) INTO the library tree ─────────────
# Ratified 2026-07-29. The library (not this service's staging area) is where the
# current copy of a network source LIVES: each INGEST_SOURCES entry mirror-syncs
# //host/share into an absolute library path, and the library folder is then
# backed up by an ordinary `path:` BACKUP_SOURCES entry — so one archive flow
# covers ingested folders and native ones identically.
#
# MIRROR SEMANTICS (`rsync -a --delete`): the library copy is made to MATCH the
# share, so a file deleted on the share is deleted from the library on the next
# run. History lives in the dated run snapshots under BACKUP_TARGET (BACKUP_KEEP),
# NOT in the library. Every failure here is loud (OI-9 die reporting).
#
# The wake pre-step above already ran, so a source box that sleeps is awake by
# now — ingest deliberately reuses it rather than owning a second wake.
if [ -n "${INGEST_SOURCES:-}" ]; then
    log "== ingest (step 1b): mirroring network source(s) into the library tree =="
    INGEST_SUMMARY=""
    while IFS= read -r iline; do
        iline="$(str_trim "$iline")"
        [ -z "$iline" ] && continue
        case "$iline" in \#*) continue ;; esac   # the table can carry commented-out entries
        iparsed="$(ingest_parse "$iline")" \
            || die "bad INGEST_SOURCES line: '$iline' (want name=//host/share -> /abs/library/dest; the source must be a //host/share UNC and the destination an ABSOLUTE path)"
        IFS=$'\t' read -r iname isrc idest <<< "$iparsed"

        # The library destination must be REAL. A typo would otherwise mirror the
        # share into a stray directory while the BACKUP_SOURCES entry keeps
        # archiving the stale library folder — green, and wrong. So: create the
        # LEAF on first ingest, but never a missing parent (that is the typo, or
        # a library volume that is not mounted).
        if [ ! -d "$idest" ]; then
            iparent="$(dirname "$idest")"
            [ -d "$iparent" ] \
                || die "ingest[$iname]: the library destination's parent does not exist: $iparent (typo in INGEST_SOURCES, or the library filesystem is not mounted)"
            mkdir -p "$idest" || { FAIL_NOTE="ingest[$iname]: cannot create library destination $idest"; false; }
            log "ingest[$iname]: created library destination $idest (first ingest)"
        fi

        imp="$(mktemp -d)"
        mount_cifs "$isrc" "$imp" ro          # dies loudly (and reports) if refused
        # MIRROR SAFETY: a share that mounts but comes up EMPTY (wrong share name,
        # a host that rebooted with its drive unmounted) would have `--delete`
        # erase a good library copy. Refuse, loudly, unless told this is intended.
        # (the die path unmounts for us — report_failure calls umount_all)
        if ! dir_has_files "$imp" && dir_has_files "$idest"; then
            [ "${INGEST_ALLOW_EMPTY:-false}" = "true" ] \
                || die "ingest[$iname]: $isrc mounted but contains NO files, while $idest does — REFUSING to mirror-delete the library copy (if the share really is empty on purpose, set INGEST_ALLOW_EMPTY=true)"
            log "ingest[$iname]: source is empty and INGEST_ALLOW_EMPTY=true — mirroring the emptiness (the library copy WILL be cleared)"
        fi
        # A plan run must not mirror-delete anything either: it reports, no
        # writes. rsync's own --dry-run is the flag being passed here, and that
        # one IS literally dry — which is exactly the promise this script's own
        # mode could not keep, and why it is no longer called that (C25).
        IDRY=(); inote=""
        if [ "$PLAN_ONLY" = 1 ]; then IDRY=(--dry-run); inote=" [PLAN: reporting only, no library writes]"; fi
        log "ingest[$iname]: mirror $isrc -> $idest (rsync -a --delete — source deletions PROPAGATE)$inote"
        rsync -a --delete ${IDRY[@]+"${IDRY[@]}"} "$imp/" "$idest/" \
            || { FAIL_NOTE="ingest[$iname]: rsync mirror failed: $isrc -> $idest"; false; }
        umount_all
        ifiles="$(find "$idest" -type f 2>/dev/null | wc -l)"
        ibytes="$(du -sb "$idest" 2>/dev/null | awk '{print $1}')"
        log "ingest[$iname]: library copy is now $ifiles file(s), ${ibytes:-?} byte(s) at $idest"
        INGEST_SUMMARY="${INGEST_SUMMARY:+$INGEST_SUMMARY; }$iname($ifiles files -> $idest)"
    done <<< "$INGEST_SOURCES"
    : "${INGEST_SUMMARY:=none}"
    log "ingest: done — ${INGEST_SUMMARY}"
else
    log "ingest: no INGEST_SOURCES configured — skipping step 1b (sources are pulled directly)"
fi

# ── exclusions (step 2) — collected BEFORE the loop so they can be validated ───
# The one BACKUP_SOURCES table carries both sources (`name=SPEC`) and their
# filters (`name.exclude=PATTERN …`); BACKUP_EXCLUDE applies to every set. Split
# them here so a `name.exclude=` line naming a set that does not exist FAILS the
# run instead of silently doing nothing — a mistyped filter that quietly backs up
# 200 GB it was meant to skip is exactly the silent mystery this must not be.
declare -A SET_EXCLUDE=()
SOURCE_LINES=()
while IFS= read -r line; do
    line="$(str_trim "$line")"
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac    # the table can carry commented-out entries
    xname="$(exclude_line_name "$line")"
    if [ -n "$xname" ]; then
        SET_EXCLUDE["$xname"]="$(str_trim "${line#*=}")"
        continue
    fi
    SOURCE_LINES+=("$line")
done <<< "$BACKUP_SOURCES"
[ "${#SOURCE_LINES[@]}" -gt 0 ] || die "BACKUP_SOURCES contains no source lines (only comments/exclude lines?)"
for xname in "${!SET_EXCLUDE[@]}"; do
    found=0
    for line in "${SOURCE_LINES[@]}"; do [ "${line%%=*}" = "$xname" ] && found=1; done
    [ "$found" = 1 ] \
        || die "BACKUP_SOURCES has '$xname.exclude=…' but no source set named '$xname' — fix the name (a filter for a set that does not exist would silently exclude nothing)"
done
log "exclusions: global BACKUP_EXCLUDE='${BACKUP_EXCLUDE:-}'${SET_EXCLUDE[*]:+ + per-set lines for: ${!SET_EXCLUDE[*]}}"

# rsync_pull SRC/ STAGE/ : the ONE pull command for all three source kinds —
# `rsync -a --delete` plus this set's exclude patterns. When patterns are in play
# it runs with `--debug=FILTER`, which makes rsync print one line per path its
# patterns hid ("[sender] hiding file world/x.bak because of pattern *.bak");
# those lines are kept as <set>.excluded.log next to the archive so an excluded
# path is auditable, never a silent mystery. Returns rsync's status.
rsync_pull() {
    local src="$1" dst="$2" rc=0 hidden
    if [ "${#EX_ARGS[@]}" -eq 0 ]; then
        rsync -a --delete "$src" "$dst"
        return $?
    fi
    rsync -a --delete "${EX_ARGS[@]}" --debug=FILTER "$src" "$dst" >"$EX_RAW" 2>&1 || rc=$?
    grep -E '^\[sender\] hiding ' "$EX_RAW" | sed 's/^\[sender\] //' >"$EX_LOG" || true
    hidden="$(grep -c . "$EX_LOG" || true)"
    if [ "${hidden:-0}" -gt 0 ]; then
        log "[$SET_NAME] EXCLUDED $hidden path(s) by pattern — full list: $(basename "$EX_LOG")"
        sed -n '1,5p' "$EX_LOG" | while IFS= read -r h; do log "[$SET_NAME]   excluded: $h"; done
        [ "$hidden" -gt 5 ] && log "[$SET_NAME]   … $(( hidden - 5 )) more in $(basename "$EX_LOG")"
    else
        log "[$SET_NAME] exclude patterns matched nothing in this source"
    fi
    [ "$rc" -eq 0 ] || { warn "[$SET_NAME] rsync failed (rc=$rc); its output:"; sed -n '1,20p' "$EX_RAW" | while IFS= read -r l; do warn "  $l"; done; }
    return "$rc"
}

# ── steps 1c-3 per source set ────────────────────────────────────────────────
for line in "${SOURCE_LINES[@]}"; do
    name="${line%%=*}"; src="${line#*=}"
    [ -n "$name" ] && [ -n "$src" ] || die "bad BACKUP_SOURCES line: '$line' (want name=//host/share | name=volume:VOL[@CONTAINER] | name=path:/abs/dir)"

    # This set's effective exclude patterns: the global list first, then its own
    # `name.exclude=` line. `read -a` splits on whitespace WITHOUT pathname
    # expansion — a pattern like `*.bak` must never glob against the cwd.
    SET_NAME="$name"; EX_ARGS=(); EX_PATS=""
    EX_LOG="$RUN_DIR/$name.excluded.log"; EX_RAW="$STAGING/$name.rsync.out"
    read -r -a ex_pats <<< "${BACKUP_EXCLUDE:-} ${SET_EXCLUDE[$name]:-}" || true
    for pat in "${ex_pats[@]:-}"; do
        [ -n "$pat" ] || continue
        EX_ARGS+=(--exclude "$pat"); EX_PATS="${EX_PATS:+$EX_PATS }$pat"
    done
    log "[$name] exclude patterns: ${EX_PATS:-(none)}"

    # 1. pull — dispatch on the source kind (SR-013); every kind lands the set in
    # $stage and everything downstream (archive→report) is kind-agnostic.
    stage="$STAGING/$name"
    rm -rf "$stage"; mkdir -p "$stage"
    case "$(source_kind "$src")" in
        cifs)
            mp="$(mktemp -d)"
            mount_cifs "$src" "$mp" ro
            log "[$name] rsync pull from $src"
            rsync_pull "$mp/" "$stage/" || { FAIL_NOTE="rsync pull failed for $name"; false; }
            umount_all
            ;;
        volume)
            spec="${src#volume:}"; vol="${spec%%@*}"
            qc=""; [ "$spec" != "$vol" ] && qc="${spec#*@}"
            command -v docker >/dev/null 2>&1 || { FAIL_NOTE="volume source for $name needs the docker CLI"; false; }
            # A MISSING VOLUME SKIPS ITS SET, EXACTLY AS A MISSING PATH DOES
            # (the Owner, 2026-08-09 — the ruling always covered both kinds; only
            # the path branch had been changed, and open-items A26 left this as a
            # separate call). The `|| vrc=$?` form is mandatory: a bare failing
            # assignment is the last command of its list and WOULD trip the ERR
            # trap before this case could run.
            #
            # ONLY status 1 SKIPS. 2 and 3 stay fatal and that is the whole point
            # of splitting them out — "absent" is one input missing, while an
            # ambiguous label match is a choice between two volumes nobody should
            # make silently, and an unreachable daemon means we did not find out
            # anything at all. Skipping either of those would turn one broken box
            # into five quietly-unarchived volumes.
            vrc=0; vmp="$(volume_mountpoint "$vol")" || vrc=$?
            case "$vrc" in
                0) [ -d "$vmp" ] || { FAIL_NOTE="docker volume $vol for $name resolved to '$vmp', which is not a directory"; false; } ;;
                1) # THE SKIP MUST STAY ABOVE quiesce_stop. The EXIT trap fires on
                   # process exit, not on `continue`, so a skip taken after a stop
                   # would leave the container down for the rest of the run.
                   log "[$name] WARN: SOURCE MISSING, SET SKIPPED: docker volume '$vol' does not exist"
                   log "[$name]   The run continues and will finish RED naming this set."
                   log "[$name]   Nothing about this set is archived, so the previous run's copy is"
                   log "[$name]   the newest one that exists — treat it as stale from now on."
                   MISSING_SETS="${MISSING_SETS:+$MISSING_SETS, }$name(volume:$vol)"
                   continue ;;
                2) FAIL_NOTE="docker volume name '$vol' ($name) is AMBIGUOUS — more than one volume carries that compose label, and archiving the wrong one is worse than not running"; false ;;
                3) FAIL_NOTE="cannot reach the docker daemon to resolve volume '$vol' ($name) — refusing to treat 'could not ask' as 'not there'"; false ;;
            esac
            # Quiesce (optional): stop the container so a live DB can't be caught
            # mid-write; restarted right after the copy, and by the EXIT trap on
            # any failure path in between.
            if [ -n "$qc" ]; then
                quiesce_stop "$qc" || { FAIL_NOTE="quiesce stop failed for $name: $qc"; false; }
            fi
            log "[$name] rsync pull from volume $vol ($vmp)${qc:+ [quiesced: $qc]}"
            rsync_pull "$vmp/" "$stage/" || { FAIL_NOTE="rsync pull failed for $name"; false; }
            if [ -n "$qc" ]; then
                quiesce_start "$qc" || { FAIL_NOTE="quiesce RESTART failed for $name — run: docker start $qc"; false; }
            fi
            ;;
        path)
            dir="${src#path:}"
            # A MISSING SOURCE DIRECTORY SKIPS ITS SET — IT DOES NOT ABORT THE RUN
            # (the Owner, 2026-08-09: "a missing folder shouldn't block the backup
            # of other folders").
            #
            # This used to `false` into the ERR trap, which ended the whole run at
            # the FIRST absent path. Measured cost on 2026-08-09: a hub whose
            # library drive had no NonDocs/Media/Music — one directory that
            # nothing creates — backed up NOTHING AT ALL. Thirteen healthy sets,
            # including every Private tree, were lost to the absence of one.
            # "Never silently skip" is the rule, and it does NOT require aborting:
            # the run is still red, still posts ok=false, still exits non-zero and
            # still names the set — it just does so AFTER protecting the data it
            # could reach. Losing tonight's backup of everything is the worse
            # failure, and it is the one that used to happen.
            #
            # DELIBERATELY NARROW. Only "the source is not there" is tolerated.
            # An rsync/archive/integrity failure below still aborts, because those
            # mean the machinery is broken rather than one input being absent, and
            # a half-written run should not be tidied past.
            #
            # THE `volume` BRANCH NOW MATCHES (Owner, 2026-08-09 — the ruling had
            # always covered both kinds; A26 deferred the second half). Getting
            # there needed `volume_mountpoint` to stop returning one status for
            # three causes, so that only a genuinely absent volume skips while an
            # ambiguous label match and an unreachable daemon stay fatal. `cifs:`
            # is the third kind and is NOT changed: it aborts via mount_cifs's
            # die, and it has no empty-share guard, so skipping there would trade
            # an abort for a silent-green empty archive.
            if [ ! -d "$dir" ]; then
                log "[$name] WARN: SOURCE MISSING, SET SKIPPED: $dir"
                log "[$name]   The run continues and will finish RED naming this set."
                log "[$name]   Nothing about this set is archived, so the previous run's copy is"
                log "[$name]   the newest one that exists — treat it as stale from now on."
                MISSING_SETS="${MISSING_SETS:+$MISSING_SETS, }$name($dir)"
                continue
            fi
            log "[$name] rsync pull from local path $dir"
            rsync_pull "$dir/" "$stage/" || { FAIL_NOTE="rsync pull failed for $name"; false; }
            ;;
        *)
            die "bad BACKUP_SOURCES spec for '$name': '$src' (want //host/share | volume:VOL[@CONTAINER] | path:/abs/dir)"
            ;;
    esac

    # 2. archive + compress (auto-compression-where-applicable) ----------------
    IFS=$'\t' read -r algo ratio reason < <(compression_decision "$stage")
    if [ "$algo" = "zstd" ]; then archive="$RUN_DIR/$name.tar.zst"; else archive="$RUN_DIR/$name.tar"; fi
    log "[$name] compression: $reason"
    if [ "$PLAN_ONLY" = 1 ]; then log "[$name] plan: skip archive"; continue; fi
    # The patterns are handed to tar as well: the pull above already left them out
    # of $stage (that is what saves the copy), and this is the belt-and-braces
    # half — the ARCHIVE step is where the exclusion is contractually promised.
    # `${arr[@]+"${arr[@]}"}` is the empty-array-safe expansion under `set -u`.
    if [ "$algo" = "zstd" ]; then
        tar -C "$stage" ${EX_ARGS[@]+"${EX_ARGS[@]}"} -cf - . | zstd -q -"$ZL" -T0 -o "$archive" -f || { FAIL_NOTE="archive(zstd) failed for $name"; false; }
    else
        tar -C "$stage" ${EX_ARGS[@]+"${EX_ARGS[@]}"} -cf "$archive" . || { FAIL_NOTE="archive(tar) failed for $name"; false; }
    fi

    # 3. hash + verify + manifest ---------------------------------------------
    ftab="$RUN_DIR/$name.files.tsv"
    printf 'sha256\tsize\tmtime_epoch\trelpath\n' >"$ftab"
    set_files=0; set_bytes=0
    while IFS= read -r -d '' f; do
        rel="${f#"$stage"/}"
        sz="$(stat -c '%s' -- "$f")"; mt="$(stat -c '%Y' -- "$f")"; h="$(sha256_of "$f")"
        printf '%s\t%s\t%s\t%s\n' "$h" "$sz" "$mt" "$rel" >>"$ftab"
        set_files=$(( set_files + 1 )); set_bytes=$(( set_bytes + sz ))
    done < <(find "$stage" -type f -print0 | sort -z)

    # integrity test of the archive (proves it's readable before we trust it)
    if [ "$algo" = "zstd" ]; then zstd -q -t "$archive" || { FAIL_NOTE="zstd integrity test failed for $name"; false; }
    else tar -tf "$archive" >/dev/null || { FAIL_NOTE="tar integrity test failed for $name"; false; }; fi
    asha="$(sha256_of "$archive")"

    # The MANIFEST records the EXCLUDES with the set: whoever restores it must be
    # able to see that the archive is a FILTERED copy of its source, not a
    # complete one (restore.sh says so out loud).
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$name" "$src" "$(basename "$archive")" "$algo" "$asha" "$set_files" "$set_bytes" "$reason" "${EX_PATS:--}" >>"$MANIFEST"
    # RELEASE THE STAGING COPY NOW THAT THE ARCHIVE EXISTS AND IS VERIFIED.
    # It used to be removed only at the start of this set's OWN next turn, so at
    # the end of every run all fourteen staging trees were still on disk — a
    # permanent, uncompressed SECOND COPY OF THE WHOLE LIBRARY, on the 119 GB
    # system NVMe, because BACKUP_STAGING defaults under /var/tmp. Everything
    # above has already read what it needs: the archive is written, integrity
    # tested, hashed, and the per-file table is built.
    rm -rf "$stage"
    log "[$name] archived $(basename "$archive") files=$set_files bytes=$set_bytes sha256=${asha:0:16}…"
    TOTAL_FILES=$(( TOTAL_FILES + set_files )); TOTAL_BYTES=$(( TOTAL_BYTES + set_bytes ))
    SET_SUMMARY_JSON="${SET_SUMMARY_JSON:+$SET_SUMMARY_JSON,}$(printf '{"set":"%s","algo":"%s","files":%s,"bytes":%s,"incompressible_pct":%s,"excludes":"%s"}' "$name" "$algo" "$set_files" "$set_bytes" "$ratio" "$EX_PATS")"
    SET_SUMMARY="${SET_SUMMARY:+$SET_SUMMARY, }$name($set_files/${set_bytes}B/$algo)"
done

# A plan run reports a missing source exactly as a real run does. It is what
# verify-hub.sh asserts on (TC-H-M02/M10), so letting it exit 0 with sets missing
# would make the check green on a box that cannot fully back up — the precise
# shape of silent green this file exists to refuse.
if [ "$PLAN_ONLY" = 1 ]; then
    if [ -n "$MISSING_SETS" ]; then
        trap - ERR
        report_failure "source directory missing for: $MISSING_SETS — every OTHER set was planned normally; create the path(s) or remove the set from BACKUP_SOURCES"
        exit 1
    fi
    # THE CLOSING LINE WAS THE WHOLE TRAP (C25). It said "dry-run complete (no
    # archives written)" — precisely, narrowly true, and read by everyone as
    # "nothing was written". Sixteen files were. It now names what it wrote and
    # where, so the answer is in the run's own log rather than on the stick.
    log "plan complete: no archives written — and this run DID write $(find "$RUN_DIR" -type f 2>/dev/null | wc -l) file(s) to $RUN_DIR"
    log "  the directory stays on $BACKUP_TARGET as the plan's evidence; it holds logs only, and"
    log "  retention prunes it like any other run without a RUN.json (BACKUP_KEEP=$KEEP deep)."
    trap - ERR; exit 0
fi

# ── 6. report (never-silent-green: OK only if every set was reached) ─────────
trap - ERR

# A PARTIAL RUN IS A RED RUN, and it reaches here having done real work. The
# archives, manifest, hashes and retention above are all complete for the sets
# whose sources existed — that data is on the drive and restorable, which is the
# entire point of continuing past a missing source. What must NOT happen is this
# reporting ok: a run that protected 13 of 14 sets is a run with a hole in it,
# and the operator has to be told every night until it is fixed.
if [ -n "$MISSING_SETS" ]; then
    report_failure "source directory missing for: $MISSING_SETS — the other set(s) WERE archived to run_$RUN_TS (${SET_SUMMARY:-none}); create the path(s) or remove the set from BACKUP_SOURCES"
    log "  the archived sets are complete and restorable; only the named set(s) are absent"
    log "  manifest: $MANIFEST"
    exit 1
fi

# ── 4. retention / rotation — LAST, and only on a run that is actually good ──
# THIS USED TO RUN BEFORE THE VERDICT, AND IT COST THE ARCHIVES (found 2026-08-09,
# reproduced as run-backup-cycle-sim.sh S8).
#
# It sat at step 4, between archiving and reporting, and pruned on directory
# NAMES alone — so it could not know whether the run it had just written held
# anything. That was survivable only by accident: while a missing source
# ABORTED, the run died in the ERR trap and never reached this code. Making a
# missing source SKIP its set (A26, the same day, and correct) removed the
# accident. A library drive that stops mounting then writes one empty run_
# directory per night, each counts as a keeper, and after BACKUP_KEEP nights
# every good archive has been rotated out by runs that backed up nothing.
# Measured: 2 good runs + 2 sourceless runs at KEEP=2 left
# "2 run dir(s), 0 of them holding an archive".
#
# The fix is an ordering and a definition:
#   * ORDERING — retention runs AFTER the verdict, so a RED run prunes NOTHING.
#     A night that failed does not get to rotate away the nights that worked.
#   * DEFINITION — a "run" for retention is one whose RUN.json says status ok.
#     Failed directories are pruned on their own budget, so they cannot grow
#     without bound either, and never at the expense of a good one.
# The run being written right now is excluded from both passes: it has no
# RUN.json yet, and deleting the archive you just made is exactly what
# BACKUP_KEEP=0 did.
retention_prune() {
    local d good=() bad=() i
    while IFS= read -r d; do
        [ "$d" = "run_$RUN_TS" ] && continue                 # never the current run
        if grep -q '"status": "ok"' "$BACKUP_TARGET/$d/RUN.json" 2>/dev/null; then
            good+=("$d")
        else
            bad+=("$d")
        fi
    done < <(find "$BACKUP_TARGET" -mindepth 1 -maxdepth 1 -type d -name 'run_*' -printf '%f
' | sort)

    # This run counts toward KEEP, so keep KEEP-1 of the older good ones.
    local keep_old=$(( KEEP - 1 ))
    (( keep_old < 0 )) && keep_old=0
    log "retention: ${#good[@]} older good run(s) + this one, ${#bad[@]} failed; keeping $KEEP"
    for (( i = 0; i < ${#good[@]} - keep_old; i++ )); do
        log "  prune old run ${good[$i]}"; rm -rf "${BACKUP_TARGET:?}/${good[$i]}"
    done
    # Failed directories are small (a header-only manifest and a log) and they
    # are EVIDENCE while the fault is live, so they are kept to the same depth
    # rather than deleted eagerly.
    for (( i = 0; i < ${#bad[@]} - KEEP; i++ )); do
        log "  prune old FAILED run ${bad[$i]}"; rm -rf "${BACKUP_TARGET:?}/${bad[$i]}"
    done
}
retention_prune

write_run_json "ok" "sets: ${SET_SUMMARY:-none}"
feed_naglight true "backup ok $RUN_TS — ${SET_SUMMARY:-no sets}"
log "== backup OK: $TOTAL_FILES file(s), $TOTAL_BYTES byte(s) across sets =="
log "manifest: $MANIFEST"
exit 0
