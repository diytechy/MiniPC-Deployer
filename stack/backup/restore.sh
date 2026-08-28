#!/usr/bin/env bash
# restore.sh — reconstruct a backed-up set from a run's archive + recovery
# MANIFEST and VERIFY byte equality (WI-10.15 restore drill; FileBackup's
# reconstruct capability reproduced in bash).
#
# Reads the run's MANIFEST.tsv for the set (archive name, algo, archive sha256),
# verifies the archive's own sha256, extracts it into --target, then verifies
# EVERY restored file against <set>.files.tsv (sha256 + size). Fails loudly:
# restores everything recoverable, then exits nonzero naming what it could not
# account for (a clean restore exits 0) — the SR-029/SR-031 fail-loudly contract.
#
# THREE WITNESSES TO THE COUNT, AND THEY MUST AGREE (2026-08-09).
#
# "Verify every file in the table" is not the same promise as "verify every file
# that was backed up", and the whole defect lived in that gap: a <set>.files.tsv
# truncated to three rows made this script verify three files, find nothing
# wrong with any of them, and print RESTORE OK for a set of 5000. The table was
# both the WORK LIST and the DEFINITION OF DONE, so damaging it made the check
# smaller instead of making it fail.
#
#   M — MANIFEST.tsv columns 6 and 7 (this set's files and bytes). NOT an
#       independent census: backup.sh accumulates those counters in the SAME
#       loop that writes the table and emits the row afterwards, so a short
#       enumeration at backup time produces a short table AND a matching short
#       count. What M is, is a different FILE written at a different MOMENT —
#       which is exactly what catches a table damaged after the run.
#   T — the table on disk right now: the rows we are about to verify.
#   A — the ARCHIVE's own member headers, read back with `tar -tv`. Produced by
#       tar, from tar's own traversal, and carried inside the blob whose sha256
#       was just verified. A IS THE ONLY GENUINELY INDEPENDENT WITNESS, and the
#       only one that catches a count M and T were both born short.
#
# M != T means the run directory was damaged after the run. A != T means the
# archive holds files the table never listed — the 3-of-5000 shape. Either way
# the restore is not trustworthy, and this names the pair that disagreed rather
# than the last thing to touch the corpse.
#
# EXIT CODES — ONE STATUS PER CAUSE, not one status for several. (The same rule
# common.sh:volume_mountpoint had to learn the same day, and for the same
# reason: a caller that cannot tell "absent" from "could not look" cannot act.)
#   0  clean — every file in the archive is listed, restored and byte-verified
#   1  NOT TRUSTWORTHY — a file failed verification, and/or the counts disagree
#   2  usage / bad arguments
#   3  this run holds NO copy of this set, and that is recorded, not damage:
#      its source was missing, or the run was a --dry-run. Use an older run.
#   4  this run has never heard of this set — a typo, or the wrong run directory
#   5  the run directory itself is unusable
#
# Usage: restore.sh --run RUN_DIR --set NAME --target DIR
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

# bail CODE MSG… : common.sh's `die` always exits 1, which is right for a
# service with one failure mode and wrong for the recovery tool someone is
# scripting against in an emergency. Same shape, caller picks the status.
bail() { local c="$1"; shift; local m; for m in "$@"; do log "ERROR: $m"; done; exit "$c"; }

# newest_run_with_set moved to common.sh on 2026-08-28 — firstboot's ACME
# restore asks the same question, and two copies of it would drift.

RUN_DIR=""; SET=""; TARGET=""
while [ $# -gt 0 ]; do
    case "$1" in
        --run) RUN_DIR="$2"; shift 2 ;;
        --set) SET="$2"; shift 2 ;;
        --target) TARGET="$2"; shift 2 ;;
        -h|--help) sed -n '2,50p' "$0"; exit 0 ;;
        *) bail 2 "unknown arg: $1" ;;
    esac
done
[ -n "$RUN_DIR" ] && [ -n "$SET" ] && [ -n "$TARGET" ] || bail 2 "usage: restore.sh --run RUN_DIR --set NAME --target DIR"
MANIFEST="$RUN_DIR/MANIFEST.tsv"
FTAB="$RUN_DIR/$SET.files.tsv"
[ -f "$MANIFEST" ] || bail 5 "no MANIFEST.tsv in $RUN_DIR — that is not a run directory, or the run died before step 3"

# ── is this set in this run at all, and if not, WHY NOT ──────────────────────
# THREE DIFFERENT ANSWERS USED TO SHARE ONE MESSAGE. The old order tested
# <set>.files.tsv FIRST, so a set that had been SKIPPED (source absent — normal,
# recorded, and the run finished red naming it) and a set name that was simply
# MISTYPED both died with "no NAME.files.tsv in …": a complaint about a missing
# artifact, for a set that was never going to have one. Mid-recovery that is a
# wrong turn — it reads as "the backup is corrupt" when the truth is "this night
# holds no copy of that set, and the night before does".
row="$(awk -F'\t' -v s="$SET" 'NR>1 && $1==s {print; exit}' "$MANIFEST")"
if [ -z "$row" ]; then
    prev="$(newest_run_with_set "$(dirname "$RUN_DIR")" "$SET")"
    if [ -n "$prev" ]; then hint="the newest run that DOES hold '$SET' is $prev — restore from there"
    else hint="no other run under $(dirname "$RUN_DIR") holds '$SET' either"; fi
    if grep -Fq "[$SET] WARN: SOURCE MISSING, SET SKIPPED" "$RUN_DIR/backup.log" 2>/dev/null; then
        log "$(grep -F "[$SET] WARN: SOURCE MISSING, SET SKIPPED" "$RUN_DIR/backup.log" | head -1)"
        bail 3 "set '$SET' was SKIPPED by this run: its source was not there, so nothing about it was archived." \
               "This is not damage. The run finished RED and named the set (see RUN.json)." \
               "$hint."
    fi
    if grep -q 'dry_run=1' "$RUN_DIR/backup.log" 2>/dev/null; then
        bail 3 "run $(basename "$RUN_DIR") was a --dry-run: it wrote a MANIFEST header and no archives at all," \
               "so it holds no data for '$SET' or for anything else. $hint."
    fi
    bail 4 "set '$SET' is not in $MANIFEST — this run never archived a set by that name." \
           "Sets in this run: $(awk -F'\t' 'NR>1 {printf "%s ", $1}' "$MANIFEST")" \
           "Check the spelling against BACKUP_SOURCES ($hint)."
fi
# From here the set IS in the manifest, so a missing table is DAMAGE, not absence.
[ -f "$FTAB" ] || bail 5 "set '$SET' is in $MANIFEST but $SET.files.tsv is GONE — the run directory has been damaged since the run (the archive may still be readable; its sha256 is in the manifest)"

# The last column (`excludes`) is absent from runs written before exclusions
# existed; an empty/`-` value simply means "nothing was filtered out".
IFS=$'\t' read -r m_set m_src m_arch m_algo m_sha m_files m_bytes m_reason m_excl <<< "$row"
ARCHIVE="$RUN_DIR/$m_arch"
[ -f "$ARCHIVE" ] || bail 5 "archive missing: $ARCHIVE (the manifest lists it, so the run directory has been damaged since the run)"

log "restore set '$SET' from $m_arch (algo=$m_algo, files=$m_files) -> $TARGET"
if [ -n "${m_excl:-}" ] && [ "${m_excl:-}" != "-" ]; then
    log "NOTE: this set was archived WITH exclusions ($m_excl) — it is a FILTERED copy of its source, so a byte-for-byte diff against the live source will show those paths missing. See $SET.excluded.log in the run dir."
fi

# 1. verify the archive's own integrity (sha256 recorded at backup time).
have_sha="$(sha256_of "$ARCHIVE")"
[ "$have_sha" = "$m_sha" ] || bail 1 "archive sha256 mismatch: got $have_sha want $m_sha (corrupt archive)"
log "archive sha256 OK ($have_sha)"

# 2. extract into the target.
mkdir -p "$TARGET"
if [ "$m_algo" = "zstd" ]; then
    zstd -q -dc "$ARCHIVE" | tar -C "$TARGET" -xf - || bail 1 "extract failed (zstd)"
else
    tar -C "$TARGET" -xf "$ARCHIVE" || bail 1 "extract failed (tar)"
fi

# 3. THE THREE WITNESSES ─────────────────────────────────────────────────────
# A: census the archive itself. `tar -tv`'s first column is the mode string, so
# a leading `-` selects REGULAR FILES and nothing else — which is exactly what
# backup.sh's `find -type f` put in the table. Directories (d), symlinks (l) and
# device nodes (c/b) are correctly excluded from both sides. Field 3 is the size.
a_files=0; a_bytes=0
if [ "$m_algo" = "zstd" ]; then _list() { zstd -q -dc "$ARCHIVE" | tar -tvf -; }
else                            _list() { tar -tvf "$ARCHIVE"; }; fi
if _census="$(_list 2>/dev/null | awk '$1 ~ /^-/ { n++; b += $3 } END { printf "%d %d", n+0, b+0 }')"; then
    read -r a_files a_bytes <<< "$_census"
else
    bail 1 "could not read the archive's member list — it verified by sha256 but will not enumerate"
fi

# T: the table as it stands. A TORN FINAL LINE IS INVISIBLE TO THE READER LOOP
# THAT IS MEANT TO CATCH IT — bash `read` returns non-zero on a last line with
# no trailing newline and DISCARDS what it consumed, and an unterminated final
# row is precisely the signature of a table cut mid-write. So the terminator is
# checked directly, before anything reads the file.
t_files="$(awk 'NR>1' "$FTAB" | grep -c . || true)"
if [ -s "$FTAB" ] && [ "$(tail -c1 "$FTAB" | wc -l)" -eq 0 ]; then
    bail 1 "$SET.files.tsv does not end in a newline — it was cut mid-write, so its last row is truncated and the reader below would silently discard it"
fi

log "count witnesses: manifest=$m_files table=$t_files archive=$a_files (bytes manifest=$m_bytes archive=$a_bytes)"
_bad_count=0
if [ "$t_files" != "$m_files" ]; then
    warn "COUNT MISMATCH manifest vs table: the manifest recorded $m_files file(s) for '$SET', the table lists $t_files"
    warn "  Both were written by the same run, so they disagreeing means the run directory was damaged AFTER the run."
    _bad_count=1
fi
if [ "$a_files" != "$t_files" ]; then
    warn "COUNT MISMATCH archive vs table: the archive contains $a_files regular file(s), the table lists $t_files"
    warn "  The archive is the independent witness. $(( a_files - t_files )) file(s) were restored but never listed,"
    warn "  so they were never checked — this is the failure where a truncated table makes the check smaller."
    _bad_count=1
fi
if [ -n "${m_bytes:-}" ] && [ "$a_bytes" != "$m_bytes" ]; then
    warn "BYTE MISMATCH archive vs manifest: archive holds $a_bytes byte(s), the manifest recorded $m_bytes"
    _bad_count=1
fi

# 4. verify EVERY file in the table against its recorded hash + size.
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

if (( bad > 0 )) || [ "$_bad_count" != 0 ]; then
    log "RESTORE NOT TRUSTWORTHY: $checked file(s) verified, $bad failed verification, counts $( [ "$_bad_count" = 0 ] && echo agree || echo DISAGREE )"
    log "  The files that DID verify are byte-exact and are on disk at $TARGET — this is not a reason to delete them."
    exit 1
fi
log "RESTORE OK: $checked file(s) verified byte-exact (sha256 + size); manifest, table and archive all agree on $a_files"
exit 0
