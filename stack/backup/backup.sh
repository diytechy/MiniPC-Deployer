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
#   5. offsite        — LEGACY/OPTIONAL. The target state (Owner, 2026-07-29) is
#                        OFFSITE_ENABLED=false: the IceDrive client is pointed at
#                        library paths in its OWN GUI and this service does no
#                        offsite staging. Kept working for boxes still using it —
#                        OFFSITE_PATH=/abs/dir (local dir) or OFFSITE_UNC (cifs).
#   6. report         — POST NagLight /api/feed; NEVER-SILENT-GREEN: any failure
#                        posts ok=false and exits nonzero — the ERR trap AND
#                        every `die` path (OI-9)
#
# Usage: backup.sh [--config PATH] [--dry-run]
#   --config  path to backup.env (default: /etc/homehub-backup/backup.env, else the
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
        -h|--help) sed -n '2,39p' "$0"; exit 0 ;;
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

RUN_TS="$(date -u +%Y%m%d_%H%M%S)"
RUN_DIR="$BACKUP_TARGET/run_$RUN_TS"
MANIFEST="$RUN_DIR/MANIFEST.tsv"
mkdir -p "$RUN_DIR" "$STAGING"
LOG_FILE="$RUN_DIR/backup.log"

# Run totals + the report fields, initialised BEFORE the failure machinery below
# so the very first failure can already write a complete RUN.json.
TOTAL_BYTES=0; TOTAL_FILES=0; SET_SUMMARY=""; SET_SUMMARY_JSON=""; OFFSITE_DONE="skipped"
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

# ── offsite target: exactly ONE form (legacy step — see step 5) ───────────────
# Validated HERE, at run start, so a config mistake fails in seconds instead of
# after an hour of archiving. Both forms are LEGACY as of the Owner's 2026-07-29
# correction (target state: OFFSITE_ENABLED=false, the IceDrive client syncs
# library paths itself). OFFSITE_PATH = a local dir an on-box sync client
# uploads (OI-11 build); OFFSITE_UNC = the older cifs push to another host's
# share. Both set is a config error, not a precedence puzzle.
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
        # --dry-run must not mirror-delete anything either: it reports, no writes.
        IDRY=(); inote=""
        if [ "$DRY_RUN" = 1 ]; then IDRY=(--dry-run); inote=" [DRY-RUN: reporting only, no library writes]"; fi
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
            vmp="$(volume_mountpoint "$vol")" || true
            { [ -n "$vmp" ] && [ -d "$vmp" ]; } || { FAIL_NOTE="docker volume not found for $name: $vol"; false; }
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
            [ -d "$dir" ] || { FAIL_NOTE="path source missing for $name: $dir"; false; }
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
    if [ "$DRY_RUN" = 1 ]; then log "[$name] dry-run: skip archive"; continue; fi
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
    log "[$name] archived $(basename "$archive") files=$set_files bytes=$set_bytes sha256=${asha:0:16}…"
    TOTAL_FILES=$(( TOTAL_FILES + set_files )); TOTAL_BYTES=$(( TOTAL_BYTES + set_bytes ))
    SET_SUMMARY_JSON="${SET_SUMMARY_JSON:+$SET_SUMMARY_JSON,}$(printf '{"set":"%s","algo":"%s","files":%s,"bytes":%s,"incompressible_pct":%s,"excludes":"%s"}' "$name" "$algo" "$set_files" "$set_bytes" "$ratio" "$EX_PATS")"
    SET_SUMMARY="${SET_SUMMARY:+$SET_SUMMARY, }$name($set_files/${set_bytes}B/$algo)"
done

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

# ── 5. offsite — LEGACY/OPTIONAL (retired from the target state 2026-07-29) ────
# The Owner's corrected model: the IceDrive client (SR-015 desktop session) is
# pointed DIRECTLY at the chosen library paths in its own GUI, so the backup
# service performs NO offsite staging at all. The target state is therefore
# OFFSITE_ENABLED=false and this whole step is a logged skip.
#
# The code below stays functional for a box still configured the old way — two
# target forms, ONE staging routine (the file selection is a single fact):
#   OFFSITE_PATH  a LOCAL directory an on-box sync client uploads (the OI-11
#                 build; superseded as a DESIGN by the correction above, kept as
#                 harmless legacy). The upload was the client's job; this step
#                 only ever had to LAND the files.
#   OFFSITE_UNC   the older cifs push to another host's synced share.
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
    log "offsite: disabled (OFFSITE_ENABLED!=true) — the target state: the IceDrive client syncs library paths itself, this service stages nothing"
    OFFSITE_DONE="disabled (client syncs library paths directly)"
elif [ -n "${OFFSITE_PATH:-}" ]; then
    # Local target. The directory must ALREADY exist — creating it silently
    # would hide a mistyped path or an IceDrive folder that never got set up,
    # and the files would then sit in a folder nothing syncs.
    [ -d "$OFFSITE_PATH" ] || { FAIL_NOTE="offsite: OFFSITE_PATH is not a directory: $OFFSITE_PATH (is the on-box IceDrive sync folder set up?)"; false; }
    dest="$OFFSITE_PATH/homehub-backup/run_$RUN_TS"
    offsite_stage "$dest" || { FAIL_NOTE="offsite: copy into $dest failed"; false; }
    OFFSITE_DONE="staged run_$RUN_TS in $OFFSITE_PATH (on-box IceDrive client uploads it)"
    log "offsite: $OFFSITE_DONE"
else
    # Legacy remote share (superseded by OFFSITE_PATH; kept working).
    omp="$(mktemp -d)"; mount_cifs "$OFFSITE_UNC" "$omp" rw
    offsite_stage "$omp/homehub-backup/run_$RUN_TS" || { FAIL_NOTE="offsite: push to $OFFSITE_UNC failed"; false; }
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
