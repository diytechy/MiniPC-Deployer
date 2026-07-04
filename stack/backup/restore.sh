#!/usr/bin/env bash
# restore.sh — reconstruct a backed-up set from a run's archive + recovery
# MANIFEST and VERIFY byte equality (WI-10.15 restore drill; FileBackup's
# reconstruct capability reproduced in bash).
#
# Reads the run's MANIFEST.tsv for the set (archive name, algo, archive sha256),
# verifies the archive's own sha256, extracts it into --target, then verifies
# EVERY restored file against <set>.files.tsv (sha256 + size). Fails loudly:
# restores everything recoverable, then exits nonzero naming the count it could
# not verify (a clean restore exits 0) — the SR-029/SR-031 fail-loudly contract.
#
# Usage: restore.sh --run RUN_DIR --set NAME --target DIR
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

RUN_DIR=""; SET=""; TARGET=""
while [ $# -gt 0 ]; do
    case "$1" in
        --run) RUN_DIR="$2"; shift 2 ;;
        --set) SET="$2"; shift 2 ;;
        --target) TARGET="$2"; shift 2 ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
[ -n "$RUN_DIR" ] && [ -n "$SET" ] && [ -n "$TARGET" ] || die "usage: restore.sh --run RUN_DIR --set NAME --target DIR"
MANIFEST="$RUN_DIR/MANIFEST.tsv"
FTAB="$RUN_DIR/$SET.files.tsv"
[ -f "$MANIFEST" ] || die "no MANIFEST.tsv in $RUN_DIR"
[ -f "$FTAB" ]     || die "no $SET.files.tsv in $RUN_DIR"

# Pull the set's row from the manifest.
row="$(awk -F'\t' -v s="$SET" 'NR>1 && $1==s {print; exit}' "$MANIFEST")"
[ -n "$row" ] || die "set '$SET' not found in $MANIFEST"
IFS=$'\t' read -r m_set m_src m_arch m_algo m_sha m_files m_bytes m_reason <<< "$row"
ARCHIVE="$RUN_DIR/$m_arch"
[ -f "$ARCHIVE" ] || die "archive missing: $ARCHIVE"

log "restore set '$SET' from $m_arch (algo=$m_algo, files=$m_files) -> $TARGET"

# 1. verify the archive's own integrity (sha256 recorded at backup time).
have_sha="$(sha256_of "$ARCHIVE")"
[ "$have_sha" = "$m_sha" ] || die "archive sha256 mismatch: got $have_sha want $m_sha (corrupt archive)"
log "archive sha256 OK ($have_sha)"

# 2. extract into the target.
mkdir -p "$TARGET"
if [ "$m_algo" = "zstd" ]; then
    zstd -q -dc "$ARCHIVE" | tar -C "$TARGET" -xf - || die "extract failed (zstd)"
else
    tar -C "$TARGET" -xf "$ARCHIVE" || die "extract failed (tar)"
fi

# 3. verify EVERY file against the recovery manifest (sha256 + size). Fail loudly.
bad=0; checked=0
while IFS=$'\t' read -r h sz mt rel; do
    [ "$h" = "sha256" ] && continue           # header
    f="$TARGET/$rel"
    if [ ! -f "$f" ]; then warn "MISSING after restore: $rel"; bad=$(( bad + 1 )); continue; fi
    asz="$(stat -c '%s' -- "$f")"
    ash="$(sha256_of "$f")"
    if [ "$asz" != "$sz" ] || [ "$ash" != "$h" ]; then
        warn "MISMATCH: $rel (size $asz/$sz sha ${ash:0:12}/${h:0:12})"; bad=$(( bad + 1 )); continue
    fi
    checked=$(( checked + 1 ))
done < "$FTAB"

if (( bad > 0 )); then
    log "RESTORE INCOMPLETE: $bad file(s) failed verification ($checked verified)"
    exit 1
fi
log "RESTORE OK: $checked file(s) verified byte-exact (sha256 + size)"
exit 0
