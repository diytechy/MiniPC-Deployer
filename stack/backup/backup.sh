#!/usr/bin/env bash
# backup.sh — the AWOW bash backup service (WI-10.10, built/validated in WI-10.15).
#
# Runs the explicit six-step pipeline from HOMELAB_TOPOLOGY.md, 100% bash:
#   1. source pulls   — optional WAKE-ON-LAN pre-step for a source box that is
#                        allowed to sleep (BACKUP_WAKE_MAC; wait for tcp/445,
#                        LOUD failure on timeout), then one BACKUP_SOURCES
#                        table, three source kinds (SR-013):
#                        //host/share cifs-mount+rsync; volume:VOL[@CONTAINER]
#                        rsync from the docker volume's mountpoint, optionally
#                        quiescing @CONTAINER (stop→copy→restart, EXIT-trap
#                        safety net); path:/dir local rsync
#   2. archive+compress — tar per set, zstd WHERE APPLICABLE (already-compressed
#                        sets stored as plain .tar — FileBackup spec)
#   3. hash+verify+manifest — per-file sha256 table + archive sha256 + integrity
#                        test; a recovery MANIFEST that restore.sh reconstructs from
#   4. external-drive target — dated run snapshot under BACKUP_TARGET, with
#                        retention/rotation (keep last BACKUP_KEEP)
#   5. offsite        — copy selected sets into the IceDrive-synced folder:
#                        OFFSITE_PATH=/abs/dir (LOCAL dir synced by the on-box
#                        IceDrive client — the ratified target, OI-11) or the
#                        legacy OFFSITE_UNC cifs push to a remote share
#   6. report         — POST NagLight /api/feed; NEVER-SILENT-GREEN: any failure
#                        posts ok=false and exits nonzero — the ERR trap AND
#                        every `die` path (OI-9)
#
# Usage: backup.sh [--config PATH] [--dry-run]
#   --config  path to backup.env (default: /etc/awow-backup/backup.env, else the
#             backup.env next to this script)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

CONFIG=""; DRY_RUN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --config) CONFIG="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
if [ -z "$CONFIG" ]; then
    if   [ -f /etc/awow-backup/backup.env ]; then CONFIG=/etc/awow-backup/backup.env
    elif [ -f "$HERE/backup.env" ];         then CONFIG="$HERE/backup.env"
    else die "no config: pass --config or create /etc/awow-backup/backup.env"; fi
fi
load_config "$CONFIG"

: "${BACKUP_TARGET:?BACKUP_TARGET not set}"
: "${BACKUP_SOURCES:?BACKUP_SOURCES not set (name=//host/share lines)}"
STAGING="${BACKUP_STAGING:-/var/tmp/awow-backup/staging}"
KEEP="${BACKUP_KEEP:-7}"
ZL="${BACKUP_ZSTD_LEVEL:-10}"

# ── drive power (WI-10.10 DRIVE POWER DESIGN) ────────────────────────────────
# The configured backup drive(s) + their at-rest spin-down timeout. Empty device
# list = the whole feature is a clean no-op. See common.sh drive_standby_set for
# the hdparm -S encoding and the "power management NEVER fails a backup" contract.
read -r -a BACKUP_DRIVES <<< "${BACKUP_DRIVE_DEVICES:-}" || true
STANDBY_VALUE="${BACKUP_DRIVE_STANDBY:-241}"

RUN_TS="$(date -u +%Y%m%d_%H%M%S)"
RUN_DIR="$BACKUP_TARGET/run_$RUN_TS"
MANIFEST="$RUN_DIR/MANIFEST.tsv"
mkdir -p "$RUN_DIR" "$STAGING"
LOG_FILE="$RUN_DIR/backup.log"

# Run totals + the report fields, initialised BEFORE the failure machinery below
# so the very first failure can already write a complete RUN.json.
TOTAL_BYTES=0; TOTAL_FILES=0; SET_SUMMARY=""; SET_SUMMARY_JSON=""; OFFSITE_DONE="skipped"

write_run_json() {
    local status="$1" note="$2"
    cat >"$RUN_DIR/RUN.json" <<JSON
{
  "run": "$RUN_TS",
  "status": "$status",
  "finished_utc": "$(date -u +%FT%TZ)",
  "sets": [$SET_SUMMARY_JSON],
  "total_files": $TOTAL_FILES,
  "total_bytes": $TOTAL_BYTES,
  "offsite": "${OFFSITE_DONE:-skipped}",
  "note": "$note"
}
JSON
}

# never-silent-green: any error past this point reports ok=false and exits 1.
FAIL_NOTE=""
REPORTED_FAILURE=0

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

# ── offsite target: exactly ONE form (OI-11, ratified 2026-07-25) ─────────────
# Validated HERE, at run start, so a config mistake fails in seconds instead of
# after an hour of archiving. OFFSITE_PATH (a LOCAL directory the on-box
# IceDrive client syncs to the cloud) is the target state; OFFSITE_UNC (cifs
# push to another host's share) is the legacy form, still supported. Both set is
# a config error, not a precedence puzzle.
if [ -n "${OFFSITE_PATH:-}" ] && [ -n "${OFFSITE_UNC:-}" ]; then
    die "config: OFFSITE_PATH and OFFSITE_UNC are both set — set exactly one (OFFSITE_PATH = the on-box IceDrive-synced dir; OFFSITE_UNC = the legacy remote cifs share)"
fi
if [ "${OFFSITE_ENABLED:-false}" = "true" ] && [ -z "${OFFSITE_PATH:-}" ] && [ -z "${OFFSITE_UNC:-}" ]; then
    die "config: OFFSITE_ENABLED=true but neither OFFSITE_PATH nor OFFSITE_UNC is set"
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
log "config=$CONFIG target=$BACKUP_TARGET keep=$KEEP dry_run=$DRY_RUN"
printf 'set\tsource\tarchive\talgo\tarchive_sha256\tfiles\tbytes\treason\n' >"$MANIFEST"

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

# ── steps 1-3 per source set ─────────────────────────────────────────────────
while IFS= read -r line; do
    line="$(printf '%s' "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac    # the table can carry commented-out entries
    name="${line%%=*}"; src="${line#*=}"
    [ -n "$name" ] && [ -n "$src" ] || die "bad BACKUP_SOURCES line: '$line' (want name=//host/share | name=volume:VOL[@CONTAINER] | name=path:/abs/dir)"

    # 1. pull — dispatch on the source kind (SR-013); every kind lands the set in
    # $stage and everything downstream (archive→report) is kind-agnostic.
    stage="$STAGING/$name"
    rm -rf "$stage"; mkdir -p "$stage"
    case "$(source_kind "$src")" in
        cifs)
            mp="$(mktemp -d)"
            mount_cifs "$src" "$mp" ro
            log "[$name] rsync pull from $src"
            rsync -a --delete "$mp/" "$stage/" || { FAIL_NOTE="rsync pull failed for $name"; false; }
            umount_all
            ;;
        volume)
            spec="${src#volume:}"; vol="${spec%%@*}"
            qc=""; [ "$spec" != "$vol" ] && qc="${spec#*@}"
            command -v docker >/dev/null 2>&1 || { FAIL_NOTE="volume source for $name needs the docker CLI"; false; }
            vmp="$(volume_mountpoint "$vol")" || true
            { [ -n "$vmp" ] && [ -d "$vmp" ]; } || { FAIL_NOTE="docker volume not found for $name: $vol"; false; }
            # Quiesce (optional): stop the container so a live DB can't be caught
            # mid-write; restarted right after the copy, and by the EXIT trap on
            # any failure path in between.
            if [ -n "$qc" ]; then
                quiesce_stop "$qc" || { FAIL_NOTE="quiesce stop failed for $name: $qc"; false; }
            fi
            log "[$name] rsync pull from volume $vol ($vmp)${qc:+ [quiesced: $qc]}"
            rsync -a --delete "$vmp/" "$stage/" || { FAIL_NOTE="rsync pull failed for $name"; false; }
            if [ -n "$qc" ]; then
                quiesce_start "$qc" || { FAIL_NOTE="quiesce RESTART failed for $name — run: docker start $qc"; false; }
            fi
            ;;
        path)
            dir="${src#path:}"
            [ -d "$dir" ] || { FAIL_NOTE="path source missing for $name: $dir"; false; }
            log "[$name] rsync pull from local path $dir"
            rsync -a --delete "$dir/" "$stage/" || { FAIL_NOTE="rsync pull failed for $name"; false; }
            ;;
        *)
            die "bad BACKUP_SOURCES spec for '$name': '$src' (want //host/share | volume:VOL[@CONTAINER] | path:/abs/dir)"
            ;;
    esac

    # 2. archive + compress (auto-compression-where-applicable) ----------------
    IFS=$'\t' read -r algo ratio reason < <(compression_decision "$stage")
    if [ "$algo" = "zstd" ]; then archive="$RUN_DIR/$name.tar.zst"; else archive="$RUN_DIR/$name.tar"; fi
    log "[$name] compression: $reason"
    if [ "$DRY_RUN" = 1 ]; then log "[$name] dry-run: skip archive"; continue; fi
    if [ "$algo" = "zstd" ]; then
        tar -C "$stage" -cf - . | zstd -q -"$ZL" -T0 -o "$archive" -f || { FAIL_NOTE="archive(zstd) failed for $name"; false; }
    else
        tar -C "$stage" -cf "$archive" . || { FAIL_NOTE="archive(tar) failed for $name"; false; }
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

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$name" "$src" "$(basename "$archive")" "$algo" "$asha" "$set_files" "$set_bytes" "$reason" >>"$MANIFEST"
    log "[$name] archived $(basename "$archive") files=$set_files bytes=$set_bytes sha256=${asha:0:16}…"
    TOTAL_FILES=$(( TOTAL_FILES + set_files )); TOTAL_BYTES=$(( TOTAL_BYTES + set_bytes ))
    SET_SUMMARY_JSON="${SET_SUMMARY_JSON:+$SET_SUMMARY_JSON,}$(printf '{"set":"%s","algo":"%s","files":%s,"bytes":%s,"incompressible_pct":%s}' "$name" "$algo" "$set_files" "$set_bytes" "$ratio")"
    SET_SUMMARY="${SET_SUMMARY:+$SET_SUMMARY, }$name($set_files/${set_bytes}B/$algo)"
done <<< "$BACKUP_SOURCES"

[ "$DRY_RUN" = 1 ] && { log "dry-run complete (no archives written)"; trap - ERR; exit 0; }

# ── 4. retention / rotation on the external drive ────────────────────────────
log "retention: keep last $KEEP run(s) under $BACKUP_TARGET"
mapfile -t runs < <(find "$BACKUP_TARGET" -mindepth 1 -maxdepth 1 -type d -name 'run_*' -printf '%f\n' | sort)
prune=$(( ${#runs[@]} - KEEP ))
if (( prune > 0 )); then
    for i in $(seq 0 $(( prune - 1 ))); do
        log "  prune old run ${runs[$i]}"; rm -rf "${BACKUP_TARGET:?}/${runs[$i]}"
    done
fi

# ── 5. offsite — into the IceDrive-synced folder ──────────────────────────────
# Two target forms, ONE staging routine (the file selection is a single fact):
#   OFFSITE_PATH  a LOCAL directory the on-box IceDrive client syncs to the
#                 cloud — the ratified target state (OI-11). The upload is the
#                 client's job; this step only has to LAND the files.
#   OFFSITE_UNC   the legacy cifs push to another host's synced share.
# Exactly one may be set; that is validated at run start.

# offsite_stage DEST : copy the selected sets' archives + per-file hash tables
# plus the run metadata into DEST. Returns non-zero if a copy fails, so both
# callers can fail the run identically.
offsite_stage() {
    local dest="$1" set f
    mkdir -p "$dest" || return 1
    for set in ${OFFSITE_SETS:-}; do
        for f in "$RUN_DIR/$set".tar "$RUN_DIR/$set".tar.zst "$RUN_DIR/$set.files.tsv"; do
            [ -f "$f" ] || continue
            rsync -a "$f" "$dest/" || return 1
            log "offsite: copied $(basename "$f")"
        done
    done
    # RUN.json is written by step 6, so it only exists here on a re-run — copy
    # it opportunistically, never fail for it.
    rsync -a "$MANIFEST" "$RUN_DIR/RUN.json" "$dest/" 2>/dev/null || true
    return 0
}

if [ "${OFFSITE_ENABLED:-false}" != "true" ]; then
    log "offsite: disabled (OFFSITE_ENABLED!=true)"
elif [ -n "${OFFSITE_PATH:-}" ]; then
    # Local target. The directory must ALREADY exist — creating it silently
    # would hide a mistyped path or an IceDrive folder that never got set up,
    # and the files would then sit in a folder nothing syncs.
    [ -d "$OFFSITE_PATH" ] || { FAIL_NOTE="offsite: OFFSITE_PATH is not a directory: $OFFSITE_PATH (is the on-box IceDrive sync folder set up?)"; false; }
    dest="$OFFSITE_PATH/awow-backup/run_$RUN_TS"
    offsite_stage "$dest" || { FAIL_NOTE="offsite: copy into $dest failed"; false; }
    OFFSITE_DONE="staged run_$RUN_TS in $OFFSITE_PATH (on-box IceDrive client uploads it)"
    log "offsite: $OFFSITE_DONE"
else
    # Legacy remote share (superseded by OFFSITE_PATH; kept working).
    omp="$(mktemp -d)"; mount_cifs "$OFFSITE_UNC" "$omp" rw
    offsite_stage "$omp/awow-backup/run_$RUN_TS" || { FAIL_NOTE="offsite: push to $OFFSITE_UNC failed"; false; }
    umount_all
    OFFSITE_DONE="pushed run_$RUN_TS ($OFFSITE_UNC)"
    log "offsite: $OFFSITE_DONE"
fi

# ── 6. report success (never-silent-green: this only runs if all steps passed) ─
trap - ERR
write_run_json "ok" "sets: ${SET_SUMMARY:-none}; offsite: $OFFSITE_DONE"
feed_naglight true "backup ok $RUN_TS — ${SET_SUMMARY:-no sets}; offsite: $OFFSITE_DONE"
log "== backup OK: $TOTAL_FILES file(s), $TOTAL_BYTES byte(s) across sets =="
log "manifest: $MANIFEST"
exit 0
