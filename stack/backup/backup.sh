#!/usr/bin/env bash
# backup.sh — the AWOW bash backup service (WI-10.10, built/validated in WI-10.15).
#
# Runs the explicit six-step pipeline from HOMELAB_TOPOLOGY.md, 100% bash:
#   1. source pulls   — cifs-mount each Windows/Samba share, rsync into staging
#   2. archive+compress — tar per set, zstd WHERE APPLICABLE (already-compressed
#                        sets stored as plain .tar — FileBackup spec)
#   3. hash+verify+manifest — per-file sha256 table + archive sha256 + integrity
#                        test; a recovery MANIFEST that restore.sh reconstructs from
#   4. external-drive target — dated run snapshot under BACKUP_TARGET, with
#                        retention/rotation (keep last BACKUP_KEEP)
#   5. offsite push   — cifs-mount the IceDrive-synced share, push selected sets
#   6. report         — POST NagLight /api/feed; NEVER-SILENT-GREEN: any failure
#                        posts ok=false and exits nonzero
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

RUN_TS="$(date -u +%Y%m%d_%H%M%S)"
RUN_DIR="$BACKUP_TARGET/run_$RUN_TS"
MANIFEST="$RUN_DIR/MANIFEST.tsv"
mkdir -p "$RUN_DIR" "$STAGING"
LOG_FILE="$RUN_DIR/backup.log"

# never-silent-green: any error past this point reports ok=false and exits 1.
FAIL_NOTE=""
on_err() {
    local ln="$1"
    umount_all
    local note="${FAIL_NOTE:-backup failed at line $ln}"
    feed_naglight false "backup FAILED: $note"
    write_run_json "failed" "$note"
    log "BACKUP FAILED: $note"
    exit 1
}
trap 'on_err $LINENO' ERR
set -o errtrace

TOTAL_BYTES=0; TOTAL_FILES=0; SET_SUMMARY=""

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
SET_SUMMARY_JSON=""; OFFSITE_DONE="skipped"

log "== AWOW backup run $RUN_TS =="
log "config=$CONFIG target=$BACKUP_TARGET keep=$KEEP dry_run=$DRY_RUN"
printf 'set\tsource\tarchive\talgo\tarchive_sha256\tfiles\tbytes\treason\n' >"$MANIFEST"

# ── steps 1-3 per source set ─────────────────────────────────────────────────
while IFS= read -r line; do
    line="$(printf '%s' "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -z "$line" ] && continue
    name="${line%%=*}"; unc="${line#*=}"
    [ -n "$name" ] && [ -n "$unc" ] || die "bad BACKUP_SOURCES line: '$line' (want name=//host/share)"

    # 1. pull ------------------------------------------------------------------
    mp="$(mktemp -d)"; stage="$STAGING/$name"
    mount_cifs "$unc" "$mp" ro
    rm -rf "$stage"; mkdir -p "$stage"
    log "[$name] rsync pull from $unc"
    rsync -a --delete "$mp/" "$stage/" || { FAIL_NOTE="rsync pull failed for $name"; false; }
    umount_all

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
        "$name" "$unc" "$(basename "$archive")" "$algo" "$asha" "$set_files" "$set_bytes" "$reason" >>"$MANIFEST"
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

# ── 5. offsite push into the IceDrive-synced share ───────────────────────────
if [ "${OFFSITE_ENABLED:-false}" = "true" ] && [ -n "${OFFSITE_UNC:-}" ]; then
    omp="$(mktemp -d)"; mount_cifs "$OFFSITE_UNC" "$omp" rw
    dest="$omp/awow-backup/run_$RUN_TS"; mkdir -p "$dest"
    for set in ${OFFSITE_SETS:-}; do
        for f in "$RUN_DIR/$set".tar "$RUN_DIR/$set".tar.zst "$RUN_DIR/$set.files.tsv"; do
            [ -f "$f" ] && rsync -a "$f" "$dest/" && log "offsite: pushed $(basename "$f")"
        done
    done
    rsync -a "$MANIFEST" "$RUN_DIR/RUN.json" "$dest/" 2>/dev/null || true
    umount_all
    OFFSITE_DONE="pushed run_$RUN_TS ($OFFSITE_UNC)"
    log "offsite: $OFFSITE_DONE"
else
    log "offsite: disabled (OFFSITE_ENABLED!=true)"
fi

# ── 6. report success (never-silent-green: this only runs if all steps passed) ─
trap - ERR
write_run_json "ok" "sets: ${SET_SUMMARY:-none}; offsite: $OFFSITE_DONE"
feed_naglight true "backup ok $RUN_TS — ${SET_SUMMARY:-no sets}; offsite: $OFFSITE_DONE"
log "== backup OK: $TOTAL_FILES file(s), $TOTAL_BYTES byte(s) across sets =="
log "manifest: $MANIFEST"
exit 0
