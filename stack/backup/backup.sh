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
#             word was not). The alias was REMOVED 2026-08-29 (Q3, the Owner) once
#             both repos were swept and nothing called it — `--dry-run` now dies
#             on `unknown arg`, naming `--plan`, rather than working silently.
#
#             WHAT A PLAN RUN STILL DOES, in full — none of it is new, all of it
#             was always true, and the name is what changed:
#               * CREATES $BACKUP_TARGET/plan_<ts>/ ON THE BACKUP DRIVE and
#                 writes backup.log, a header-only MANIFEST.tsv, and one
#                 <set>.excluded.log per set into it (~16 files). The `plan_`
#                 prefix (Q1, 2026-08-29) is what makes that directory
#                 self-describing: it was `run_<ts>`, identical to a real
#                 archive run, and twelve of them on a stick is how C25 was
#                 found. It carries its OWN retention budget (BACKUP_PLAN_KEEP),
#                 pruned by plan runs themselves, so plan litter can never be
#                 bounded by the health of the nightly;
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

CONFIG=""; PLAN_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --config) CONFIG="$2"; shift 2 ;;
        --plan) PLAN_ONLY=1; shift ;;
        # `--dry-run` WAS an accepted alias for --plan between 2026-08-29 morning
        # and 2026-08-29 evening. It is GONE (Q3, the Owner's ruling), after a
        # sweep of both repos found no caller left: verify-hub.sh asks the
        # DEPLOYED script which name it knows and picks accordingly, so it works
        # against a box on either payload without the alias existing here.
        #
        # It dies with a NAMED replacement rather than a bare "unknown arg",
        # because the one caller that could still send it is a human hand and a
        # backup that refused to run should say what to type instead.
        --dry-run) die "--dry-run was retired on 2026-08-29 and removed: it was not dry (C25 — it writes a plan directory to the backup drive, mounts cifs and holds drive standby off). Use --plan." ;;
        -h|--help) sed -n '2,70p' "$0"; exit 0 ;;
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
# -- BACKUP_LAYOUT - how a run is laid out under BACKUP_TARGET (2026-09-01) ---
# dated (the original, and still the default): one `run_<UTC>` directory per run
#        under BACKUP_TARGET, kept BACKUP_KEEP deep by the retention step below.
# flat:  ONE copy, written straight into BACKUP_TARGET. No dated directories, no
#        retention, no BACKUP_KEEP - because keeping history stopped being this
#        service's job. The Owner's ruling: the service-state archives move onto
#        the LIBRARY drive (/srv/library/Configs), and the library is already
#        backed up per file, with Snapshot_<date> history, by the FileBackup
#        container. A dated folder here would be a second and worse history
#        INSIDE the thing that already versions it - and worse than merely
#        redundant, because every night would hand the snapshotter a whole new
#        set of paths to store again rather than one folder it can diff.
#
# THE COPY IS STILL NEVER OVERWRITTEN IN PLACE. A flat run builds in
# $BACKUP_TARGET/.incoming and is promoted only once the verdict is green (see
# promote_flat); a run that fails is set aside as .last-failed and the previous
# good copy is left exactly where it was. "One copy" must not be allowed to mean
# "no copy for as long as tonight's run takes", and it does not.
# fib:   ONE dated copy per night under BACKUP_TARGET/daily/<YYYY-MM-DD>, kept
#        BACKUP_FIB_DAILY_KEEP (7) deep, PLUS a Fibonacci ladder of older samples
#        under BACKUP_TARGET/fib/<age>. The Owner's ruling, 2026-09-08, and it
#        reverses the flat layout's premise rather than tweaking it.
#
#        WHY FLAT HAD TO GO. Flat put the archives on the LIBRARY drive and let
#        the FileBackup snapshots be their history. Measured on the box: all 25
#        Configs files change every night (they are fresh tar+zstd of live
#        service state, so even identical input yields a different archive), so
#        all 33 of their manifest rows were superseded into EVERY Snapshot_<date>
#        - the config archives were essentially the entire per-night snapshot.
#        The library backup was paying a full re-store of this folder nightly to
#        version something that is 2.4 MB and versions better on its own.
#
#        WHY NOT `dated`. That is where these lived before flat, and it littered
#        the archive drive's ROOT with one run_<UTC> directory per night. This
#        layout keeps that clutter inside ONE folder, BACKUP_TARGET, with exactly
#        two children: daily/ and fib/.
#
#        THE LADDER. Seven dailies give a working overlap; past that, samples are
#        kept at Fibonacci ages 13 21 34 55 89 144 233 (F(7)..F(13)). Fibonacci's
#        8 is skipped - it is inside the 7-day window, which is why the lowest
#        rung draws from the dailies instead of from a rung below it.
#
#        IT IS A CASCADE, AND IT RUNS OLDEST FIRST. Rung F(n) is refilled from
#        rung F(n-1) - never from the dailies, except at the bottom - every
#        F(n-2) days:
#
#            233 <- 144 every 89d      55 <- 34 every 21d
#            144 <-  89 every 55d      34 <- 21 every 13d
#             89 <-  55 every 34d      21 <- 13 every  8d
#                                      13 <- the oldest daily every 5d
#
#        THE ORDER IS THE WHOLE DESIGN, not an implementation detail. The rungs
#        are walked from OLDEST to NEWEST, so 233 takes 144's contents before 144
#        is itself overwritten by 89, and so on down. Walk it the other way and a
#        single night's daily would propagate the whole length of the ladder in
#        one pass, leaving seven rungs holding seven copies of the same day and
#        no history at all - the exact failure the cascade exists to avoid.
#
#        WHY THE AGES COME OUT RIGHT. Content entering rung F(n) is at most
#        F(n-1) days old and then sits for at most F(n-2) days before the next
#        refill, and F(n-1) + F(n-2) = F(n) - that is the Fibonacci identity, and
#        it is why these particular ages are the ones that work. Each rung's label
#        is therefore its ceiling, reached just before a refill.
#
#        A RUNG WHOSE SOURCE IS EMPTY IS SKIPPED, and its clock is NOT advanced,
#        so it refills the moment the rung below it first has something to give.
#        That is how the ladder populates from the bottom up on a new box instead
#        of standing empty for eight months, and how it recovers after a gap.
LAYOUT="${BACKUP_LAYOUT:-dated}"
case "$LAYOUT" in
    dated|flat|fib) ;;
    *) die "config: BACKUP_LAYOUT='$LAYOUT' is none of 'dated' (a run_<UTC> directory per run, kept BACKUP_KEEP deep), 'flat' (one current copy in BACKUP_TARGET, history left to the library backup) or 'fib' (daily/<date> kept BACKUP_FIB_DAILY_KEEP deep plus a Fibonacci ladder under fib/)." ;;
esac

KEEP="${BACKUP_KEEP:-7}"
# VALIDATED IN THE FIRST SECOND, because both bad values are silent until late.
# BACKUP_KEEP=0 is legal shell and means "keep nothing": retention deleted the
# run it had just written, and the run then died naming the LOGGER, because
# LOG_FILE lived in the directory that had just been removed (S9). A non-numeric
# value reached the arithmetic in retention and killed the run there — after an
# hour of archiving, with no report posted at all.
# BOTH RETENTION KNOBS ARE DATED-LAYOUT ONLY. A flat run keeps exactly one copy
# and prunes nothing, so neither value means anything there - and REFUSING a
# config that still carries the shipped BACKUP_KEEP=7 would have failed every
# box on the day the layout changed. The flat run says so in its retention step
# instead, out loud, because a knob quietly ignored is the same family of fault
# as a green run that wrote nothing.
PLAN_KEEP="${BACKUP_PLAN_KEEP:-$KEEP}"
if [ "$LAYOUT" = dated ]; then
case "$KEEP" in
    ''|*[!0-9]*) die "config: BACKUP_KEEP='$KEEP' is not a number (it is how many dated runs to keep on the backup drive)" ;;
esac
# BASE 10, THEN COMPARED AS A NUMBER. A `0)` case arm matches only the literal
# string, so BACKUP_KEEP=00 walked straight past it and then read as zero in the
# arithmetic below - retention would delete every previous run on a night that
# exited green, which is precisely what that arm exists to prevent. Leading zeros
# are octal to bash arithmetic besides, so '013' would quietly mean 11.
# (Adversarial review of the fib layout, 2026-09-08, found the same shape in the
# new BACKUP_FIB_DAILY_KEEP guard; this is its older sibling.)
KEEP=$(( 10#$KEEP ))
[ "$KEEP" -ge 1 ] ||
    die "config: BACKUP_KEEP=0 means 'keep no runs at all', so this run would archive the household and then delete the archive. Set it to 1 or more."         "(The configured value was '${BACKUP_KEEP:-}' - '00' and other leading-zero spellings all mean zero here.)"
# How many PLAN directories to keep. Separate from BACKUP_KEEP because they are
# produced on a completely different clock: BACKUP_KEEP counts nights, while plan
# runs come from verify-hub.sh and a human hand, twelve in one day on 2026-08-28.
# Defaults to BACKUP_KEEP so an untouched backup.env behaves sensibly.
case "$PLAN_KEEP" in
    ''|*[!0-9]*) die "config: BACKUP_PLAN_KEEP='$PLAN_KEEP' is not a number (it is how many plan_<ts> directories to keep on the backup drive)" ;;
esac
PLAN_KEEP=$(( 10#$PLAN_KEEP ))   # 0 IS legal here - see the note below
fi
# 0 IS LEGAL HERE, unlike BACKUP_KEEP. A plan directory holds logs and nothing
# else, so "keep none" destroys no data — it just means each plan run tidies up
# after the ones before it. BACKUP_KEEP=0 is refused because it would delete the
# household's archive; this cannot.
# ── the fib ladder's own knobs (BACKUP_LAYOUT=fib only) ──────────────────────
# Validated here, in the first second, for the same reason BACKUP_KEEP is: both
# of these are read AFTER the archives are written, so a bad value would fail the
# run at the point where it has already done all the work and is deciding what to
# delete. That is the worst possible moment to discover a typo.
FIB_DAILY_KEEP="${BACKUP_FIB_DAILY_KEEP:-7}"
FIB_SLOTS="${BACKUP_FIB_SLOTS:-13 21 34 55 89 144 233}"
# The run's UTC date names tonight's daily. Taken once so a run crossing midnight
# cannot write half its work under one date and half under the next.
#
# BACKUP_FIB_TODAY IS A TEST SEAM AND NOTHING ELSE. The ladder's whole contract is
# about elapsed DAYS, and there is no honest way to exercise that against a clock
# that only moves forwards at one second per second. fib-ladder.test.sh drives
# hundreds of simulated nights through this variable in about a minute. Setting it
# in a real backup.env would pin every run to one date, so every night would
# overwrite the same daily and the ladder would never advance; it is deliberately
# undocumented in backup.env.example for that reason.
FIB_TODAY="${BACKUP_FIB_TODAY:-$(date -u +%F)}"
FIB_SLOT_LIST=()
FIB_CADENCE=()
# A STRICT ISO DATE, CHECKED - and it really exists in the calendar. Everything
# in this layout is named by or compared against a date: the daily directory, the
# two rung markers, the cadence arithmetic. A value that is merely non-empty gets
# as far as `date -d`, which accepts a great deal of prose ("next friday"), and a
# value containing a slash would escape daily/ and point the same-day rm -rf at
# something else entirely.
fib_valid_date() {
    case "${1:-}" in
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
        *) return 1 ;;
    esac
    # Round-trips only if the calendar really has that day: 2026-02-30 parses on
    # some systems and normalises to March, which would silently rename a daily.
    [ "$(date -u -d "$1" +%F 2>/dev/null)" = "$1" ]
}
if [ "$LAYOUT" = fib ]; then
    fib_valid_date "$FIB_TODAY" ||
        die "config: the run date '$FIB_TODAY' is not a real YYYY-MM-DD date." \
            "It comes from BACKUP_FIB_TODAY when that is set, and from 'date -u +%F' otherwise."
    case "$FIB_DAILY_KEEP" in
        ''|*[!0-9]*) die "config: BACKUP_FIB_DAILY_KEEP='$FIB_DAILY_KEEP' is not a number (it is how many daily copies to keep, and the oldest of them is what the lowest ladder rung is filled from)" ;;
    esac
    # BASE 10, FORCED. The digit test above passes '00', which is not the literal
    # string '0' and so slipped past a `0)` case arm - and then arithmetic read it
    # as zero and the prune loop deleted every daily on a run that exited green.
    # Leading zeros are also OCTAL to bash arithmetic, so an unforced '013' would
    # quietly mean 11 and '08' would abort the run mid-arithmetic.
    FIB_DAILY_KEEP=$(( 10#$FIB_DAILY_KEEP ))
    [ "$FIB_DAILY_KEEP" -ge 1 ] ||
        die "config: BACKUP_FIB_DAILY_KEEP='$BACKUP_FIB_DAILY_KEEP' is zero, so this run would archive the household and then delete the archive - and the ladder would have nothing to promote from. Set it to 1 or more."
    # NEWLINES COLLAPSED FIRST: `read -a` consumes only the first LINE, so a
    # multi-line BACKUP_FIB_SLOTS would silently drop every rung after the first
    # line - a shorter ladder than the operator asked for, with no complaint.
    read -r -a FIB_SLOT_LIST <<< "$(printf '%s' "$FIB_SLOTS" | tr '\n\t' '  ')"
    [ "${#FIB_SLOT_LIST[@]}" -ge 2 ] ||
        die "config: BACKUP_FIB_SLOTS needs at least two rungs - the ladder is a CASCADE, and one rung has nothing to cascade from." \
            "The shipped ladder is '13 21 34 55 89 144 233'."
    for _s in "${FIB_SLOT_LIST[@]}"; do
        case "$_s" in
            ''|*[!0-9]*) die "config: BACKUP_FIB_SLOTS contains '$_s', which is not a number. It is a space-separated list of AGES IN DAYS, ascending, e.g. '13 21 34 55 89 144 233'." ;;
        esac
        if [ "$_s" -le "$FIB_DAILY_KEEP" ]; then
            die "config: BACKUP_FIB_SLOTS has a $_s-day rung, but BACKUP_FIB_DAILY_KEEP=$FIB_DAILY_KEEP already keeps every day up to $FIB_DAILY_KEEP." \
                "A rung inside the daily window would be refilled from a daily that is already on the drive." \
                "Drop that rung, or shorten the daily window."
        fi
    done
    # -- THE CADENCES, DERIVED RATHER THAN CONFIGURED -------------------------
    # Rung F(n) refills every F(n-2) days, and for a Fibonacci ladder that is
    # exactly the GAP to the rung below it: F(n) - F(n-1) = F(n-2). So the whole
    # schedule falls out of the ages themselves and there is no second list to
    # keep in step with the first - a list that could disagree with it is a list
    # that eventually will.
    #
    # THE BOTTOM RUNG has no rung below it, so the one below is derived from the
    # one above instead: F(n-1) = F(n+1) - F(n), i.e. slots[1] - slots[0] = 21-13
    # = 8, giving that rung a cadence of 13-8 = 5 days. 8 is exactly the Fibonacci
    # number that falls inside the daily window, which is why this rung draws from
    # the dailies rather than from a rung.
    #
    # WRITTEN THE OTHER WAY ROUND FIRST - the line below computed 5 where it
    # needed 8 - which gave rung 13 a cadence of 8 and let its contents reach 14
    # days, one day past its own label. Every other rung was already correct, so
    # nothing about the cascade looked wrong; fib-ladder.test.sh caught it on the
    # per-rung cadence table.
    _prev=$(( 10#${FIB_SLOT_LIST[1]} - 10#${FIB_SLOT_LIST[0]} ))
    # -- AND THEN THE CEILING IS CHECKED, RUNG BY RUNG ------------------------
    # The derivation above is only SOUND for consecutive Fibonacci terms, and
    # nothing stopped an operator writing a list that is merely ascending. With
    # '20 21' the bottom cadence comes out 19, so that rung's contents reach
    # 6+19 = 25 days against a 20-day label - the ladder would be quietly lying
    # about the age of everything on it.
    #
    # Rather than demand the Fibonacci recurrence, check the PROPERTY the ladder
    # actually promises, which is what makes any list either safe or not: content
    # enters a rung at its predecessor's ceiling and waits one cadence, so
    #     ceiling(i) = ceiling(i-1) + cadence(i)   must be <= label(i)
    # with ceiling(-1) being the age of the oldest daily. For the shipped ladder
    # that gives 11/19/32/53/87/142/231 against 13/21/34/55/89/144/233 - which is
    # the Fibonacci identity F(n-1)+F(n-2)=F(n) showing up as headroom.
    _ceiling=$(( FIB_DAILY_KEEP - 1 ))
    for _i in "${!FIB_SLOT_LIST[@]}"; do
        _this=$(( 10#${FIB_SLOT_LIST[_i]} ))
        # NORMALISED BACK INTO THE LIST, because these strings also NAME the rung
        # directories. Left raw, '013' would do its arithmetic as 13 while writing
        # fib/013, so the label on the drive and the label in the schedule would
        # disagree - and the next run with '13' would build a second, parallel rung.
        FIB_SLOT_LIST[_i]="$_this"
        if [ "$_i" = 0 ]; then _below="$_prev"; else _below=$(( 10#${FIB_SLOT_LIST[$(( _i - 1 ))]} )); fi
        _cad=$(( _this - _below ))
        [ "$_cad" -ge 1 ] ||
            die "config: BACKUP_FIB_SLOTS must ASCEND - rung $_this is not more than $_below days above the one below it, so its refill cadence would be $_cad days." \
                "The shipped ladder is '13 21 34 55 89 144 233'."
        _ceiling=$(( _ceiling + _cad ))
        [ "$_ceiling" -le "$_this" ] ||
            die "config: BACKUP_FIB_SLOTS is not a safe ladder - the ${_this}-day rung would hold content up to $_ceiling days old, past its own label." \
                "Content enters a rung at the age of the rung below it and then waits one cadence ($_cad days here), so each rung's label has to cover that sum." \
                "This is why the ages are Fibonacci: F(n-1)+F(n-2)=F(n) is exactly the condition. The shipped ladder is '13 21 34 55 89 144 233'."
        FIB_CADENCE+=("$_cad")
    done
    unset _ceiling _this
    unset _s _i _prev _below _cad
fi
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
# THE TARGET IS NO LONGER ALWAYS A WHOLE DRIVE (2026-09-01). With
# BACKUP_TARGET=/srv/library/Configs the path is a FOLDER on the library mount,
# so "is BACKUP_TARGET itself a mountpoint" answers NO on a perfectly healthy
# box - and this check fails the run, so it would have refused every night.
# enclosing_mountpoint walks up to the mount that actually carries the path: the
# same answer as before for /mnt/backup-drive, the right one for a folder.
# Reaching `/` is what now means "no data drive carries this", which is exactly
# the writing-to-the-system-disk case the paragraphs above are about.
TARGET_MOUNT="$(enclosing_mountpoint "$BACKUP_TARGET")" || TARGET_MOUNT=""
if [ "${BACKUP_TARGET_REQUIRE_MOUNT:-true}" = "true" ]; then
    if [ -n "$TARGET_MOUNT" ] && [ "$TARGET_MOUNT" != "/" ] && target_opts="$(mount_options_for "$TARGET_MOUNT")"; then
        case ",$target_opts," in
            *,ro,*)
                # ntfs3 falls back to read-only on a dirty NTFS bit (Windows Fast
                # Startup, unclean eject). Reads look fine, every write fails.
                feed_naglight false "backup target $BACKUP_TARGET is mounted READ-ONLY — refusing to run (NTFS dirty bit? clear it from Windows)"
                die "backup target $BACKUP_TARGET is mounted READ-ONLY — refusing to run. Nothing was written." ;;
        esac
        if [ "$TARGET_MOUNT" = "$BACKUP_TARGET" ]; then
            log "target preflight: $BACKUP_TARGET is a real mountpoint (rw)"
        else
            log "target preflight: $BACKUP_TARGET sits on $TARGET_MOUNT, a real mountpoint (rw)"
        fi
        # A23: say so when the archive is landing on a stand-in drive. This does
        # NOT stop the run — proving the backup works on a cheap disk before
        # committing 8 TB to it is the whole point of the bring-up period — but
        # it must not be invisible either. The composite signal is the honest
        # identity is retained as an internal preflight/log safeguard. It is not
        # a panel lane: the sole visible item covers file-share health and
        # artifact-verified FileBackup age.
        if [ -f /etc/homehub-samba/drive-identity.conf ]; then
            _expect="$(awk -F'\t' -v p="$TARGET_MOUNT" '$1 == p { print $2 }' /etc/homehub-samba/drive-identity.conf)"
            if [ -n "$_expect" ] && [ -e "/dev/disk/by-id/$_expect" ]; then
                # ACCEPT A PARTITION OF THE EXPECTED DISK, not just the disk.
                # drive-identity.conf names the whole disk, `readlink -f` gives
                # /dev/sdX (8:0), and what is mounted is /dev/sdX1 (8:1) — never
                # equal, so this cried STAND-IN at the real drive on every run
                # until 2026-09-04. Loop devices mount whole-device, which is why
                # the loopback proving run missed it. Same fix, same reasoning,
                # as library-backup.sh identity_note() — read the long note there
                # for the assumed layout and why parentage is checked at all.
                _have="$(awk -v p="$TARGET_MOUNT" '$5 == p { d = $3 } END { print d }' /proc/self/mountinfo)"
                _resolved=0
                _match=0
                _diskmm=""
                _target="$(readlink -f "/dev/disk/by-id/$_expect" 2>/dev/null)"
                if [ -n "$_target" ] && [ -r "/sys/class/block/${_target#/dev/}/dev" ]; then
                    _diskmm="$(cat "/sys/class/block/${_target#/dev/}/dev" 2>/dev/null)"
                fi
                if [ -n "$_have" ] && [ -n "$_diskmm" ]; then
                    _resolved=1
                    if [ "$_diskmm" = "$_have" ]; then _match=1; fi
                    if [ "$_match" = 0 ]; then
                        for _link in "/dev/disk/by-id/$_expect"-part[0-9]*; do
                            [ -e "$_link" ] || continue
                            _target="$(readlink -f "$_link" 2>/dev/null)" || continue
                            [ -n "$_target" ] || continue
                            _dev="${_target#/dev/}"
                            [ -r "/sys/class/block/$_dev/dev" ] || continue
                            [ -r "/sys/class/block/$_dev/../dev" ] || continue
                            # Parentage, not name shape: this candidate's sysfs
                            # parent must be the expected whole disk.
                            [ "$(cat "/sys/class/block/$_dev/../dev" 2>/dev/null)" = "$_diskmm" ] || continue
                            _mm="$(cat "/sys/class/block/$_dev/dev" 2>/dev/null)" || continue
                            [ -n "$_mm" ] || continue
                            if [ "$_mm" = "$_have" ]; then _match=1; break; fi
                        done
                    fi
                fi
                # Silent unless we positively resolved a candidate and none of
                # them is what is mounted — "cannot tell" is not "wrong drive".
                if [ "$_resolved" = 1 ] && [ "$_match" = 0 ]; then
                    log "NOTICE: this archive is landing on a STAND-IN drive, not $_expect."
                    log "  Fine during bring-up; this remains internal and does not alter panel health."
                fi
            fi
        fi
    else
        feed_naglight false "backup target $BACKUP_TARGET is NOT MOUNTED — the backup drive is absent or failed to mount; refusing to run so nothing lands on the system disk"
        die "backup target $BACKUP_TARGET is NOT MOUNTED — the backup drive is absent or failed to mount." \
            "Refusing to run: with nofail in fstab this path is an empty directory on the SYSTEM disk," \
            "so a run would look green while writing the household's backups to the wrong drive." \
            "Check the drive is plugged in and powered, then: systemctl start homehub-backup.service" \
            "(The target can be a FOLDER on a drive - /srv/library/Configs - in which case the mount it" \
            "needs is the drive UNDER it, /srv/library. Nothing at or above the target is mounted.)" \
            "(Backing up to a plain directory on purpose? Set BACKUP_TARGET_REQUIRE_MOUNT=false in backup.env.)"
    fi
else
    warn "BACKUP_TARGET_REQUIRE_MOUNT=false — not checking that $BACKUP_TARGET is a mountpoint."
    warn "  A missing drive will be backed up to the system disk and reported GREEN."
fi

# THE TARGET FOLDER MAY NOT EXIST YET. /srv/library/Configs is made by this
# service, not by provisioning - nothing else has a reason to make it, and a
# reimage arrives with the library drive's own tree and no such folder on it.
# SAFE HERE AND ONLY HERE: the preflight immediately above has just proved that a
# real data drive carries this path, so this cannot conjure a phantom directory
# on the system disk - the entire failure that preflight exists to prevent.
mkdir -p "$BACKUP_TARGET" || die "could not create the backup target directory $BACKUP_TARGET"

# -- THE FIB LAYOUT IS CONFINED TO A SUBDIRECTORY, DELIBERATELY ---------------
# This layout is the only one that MANAGES a directory tree rather than just
# adding to it: it prunes dailies and replaces rungs. Everything it deletes is
# built from $BACKUP_TARGET, so the blast radius is whatever that variable says -
# and pointed at a drive ROOT it would take ownership of daily/ and fib/ at the
# top of the whole disk, beside the household's data.
#
# Requiring the target to sit INSIDE a mount rather than being one gives the
# deletions a named box to stay in, which is the thing the operator can actually
# see and reason about. `dated` and `flat` are unaffected: neither prunes a tree
# it did not create, and the archive drive's root is `dated`'s documented target.
if [ "$LAYOUT" = fib ]; then
    _fib_mnt="$(enclosing_mountpoint "$BACKUP_TARGET" 2>/dev/null || true)"
    if [ -z "$_fib_mnt" ]; then
        die "config: could not work out which mount carries BACKUP_TARGET=$BACKUP_TARGET, so the fib layout cannot bound what it is allowed to delete."
    fi
    if [ "${BACKUP_TARGET%/}" = "${_fib_mnt%/}" ]; then
        die "config: BACKUP_TARGET=$BACKUP_TARGET IS the mountpoint $_fib_mnt, not a folder on it." \
            "BACKUP_LAYOUT=fib prunes and replaces directories under its target, so it refuses to be pointed at a whole drive:" \
            "it would create and then manage daily/ and fib/ at the top of that disk, beside everything else living there." \
            "Give it a subdirectory - the shipped value is /mnt/backup-drive/config-history."
    fi
    log "fib: target $BACKUP_TARGET is confined inside the mount $_fib_mnt"
    unset _fib_mnt
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
# WHERE RUN.json LIVES IS A PROPERTY OF THE LAYOUT: one level down inside a
# dated run directory, at the top in the flat one. Asking at the wrong depth
# finds nothing, and finding nothing is not an error here - it degrades to the
# "no previous run" warning below, so the guard would silently stop guarding.
case "$LAYOUT" in
    flat) _prev="$(find "$BACKUP_TARGET" -mindepth 1 -maxdepth 1 -name RUN.json 2>/dev/null | sort | tail -1)" ;;
    # fib keeps its runs one level deeper still - daily/<YYYY-MM-DD>/RUN.json -
    # and asking at depth 2 finds NOTHING, which this block treats as "no previous
    # run" rather than as an error. The guard would have gone on logging a warning
    # and waving every run through, on the layout that made the target smaller.
    # (Adversarial review, 2026-09-08.)
    fib)  _prev="$(find "$BACKUP_TARGET/daily" -mindepth 2 -maxdepth 2 -name RUN.json 2>/dev/null | sort | tail -1)" ;;
    *)    _prev="$(find "$BACKUP_TARGET" -mindepth 2 -maxdepth 2 -name RUN.json 2>/dev/null | sort | tail -1)" ;;
esac
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
# THE PREFIX IS THE WHOLE POINT OF Q1. A plan run writes a directory to the
# backup drive — that was never in doubt, C25 measured it — and until 2026-08-29
# it was named `run_<ts>`, byte-identical in shape to a real archive run. Twelve
# of them sat on the stick looking like twelve backups. `plan_` makes the litter
# say what it is to anyone holding the drive, with no log to read.
#
# Everything that GLOBS these directories was changed with it, and the list is
# short on purpose: retention_prune (here), newest_run_with_set (common.sh, which
# now cannot pick a plan directory at all), and restore.sh. Nothing else in
# either repo matches on the prefix — swept 2026-08-29.
if [ "$PLAN_ONLY" = 1 ]; then RUN_PREFIX=plan; else RUN_PREFIX=run; fi
if [ "$LAYOUT" = flat ] || [ "$LAYOUT" = fib ]; then
    # -- FLAT and FIB: the run is built in a FIXED directory, not a dated one --
    # fib is dated on the OUTSIDE (daily/<date>) but builds here first and is
    # renamed into place by promote_fib only once the verdict is green, so it
    # inherits this whole branch: the same .incoming staging, the same run lock,
    # and the same leftover handling. A half-written daily must never appear
    # under daily/ where retention and the ladder would both treat it as real.
    # The work still happens in a directory of its own, so the copy already on
    # the drive survives a run that dies half way through:
    #   .incoming     a full run; promoted into $BACKUP_TARGET once it is green
    #   plan_latest   a plan run; never promoted, and ONE slot rather than one
    #                 per invocation. There is no BACKUP_PLAN_KEEP here to bound
    #                 them, and litter on the LIBRARY drive is worse than litter
    #                 on the archive drive: the library backup would take a
    #                 snapshot of every plan directory, and keep all of them.
    # `plan_latest` KEEPS THE plan_ PREFIX ON PURPOSE - restore.sh refuses a plan
    # directory by its NAME (Q1), and that check has to keep working.
    if [ "$PLAN_ONLY" = 1 ]; then RUN_NAME="plan_latest"; else RUN_NAME=".incoming"; fi
    RUN_DIR="$BACKUP_TARGET/$RUN_NAME"
    # THE CONCURRENCY GUARD BECOMES A LOCK. In the dated layout the guard is the
    # exclusive `mkdir` of a per-second directory name (see the `else` branch);
    # a FIXED name cannot provide one, and two runs sharing .incoming would
    # interleave their archives and both report success - the exact failure the
    # 2026-08-29 review found. The lock lives on /run (tmpfs, always local,
    # cleared by a reboot) rather than on the target: a lock file on an NTFS/FUSE
    # mount is not a thing to bet a backup on, and one left behind by a power cut
    # would block every later run.
    # /run FIRST because it is tmpfs - always local, always writable by the
    # service (which runs as root), and cleared by a reboot so a lock can never
    # outlive the machine. The fallback is for a run started by a person or a
    # test without write access there; it sits beside the staging directory,
    # which this run has to be able to write anyway. A leftover lock FILE locks
    # nothing either way: flock is advisory and the kernel drops it when the
    # process ends.
    LOCK_FILE=/run/homehub-backup.lock
    : >>"$LOCK_FILE" 2>/dev/null || LOCK_FILE="$(dirname "$STAGING")/backup.lock"
    exec 9>>"$LOCK_FILE" || die "could not open a run lock at $LOCK_FILE"
    if command -v flock >/dev/null 2>&1; then
        flock -n 9 || die "another backup run already holds $LOCK_FILE - refusing to start a second one. The flat layout writes into ONE directory, so two runs would interleave their archives and both report success."
    else
        # FATAL, NOT A WARNING (adversarial review, 2026-09-08). In the dated
        # layout the exclusive mkdir of run_<UTC> is itself the guard, so a
        # missing flock costs nothing. These two layouts write into a FIXED
        # directory and have no such guard: two runs would interleave their
        # archives into one .incoming and both report success, and in fib they
        # would also delete each other's staged rungs mid-cascade. Refusing to
        # start is the only honest answer, and flock ships in util-linux on every
        # box this service targets.
        die "flock is not installed, so a second simultaneous run could not be refused." \
            "BACKUP_LAYOUT=$LAYOUT writes into a FIXED directory ($RUN_NAME), so two runs would interleave their archives and both report success." \
            "Install util-linux, or use BACKUP_LAYOUT=dated, whose per-second run directory is its own guard."
    fi
    # A LEFTOVER .incoming MEANS THE PREVIOUS RUN WAS KILLED - a run that merely
    # FAILED sets itself aside as .last-failed (report_failure). Say so and clear
    # it: refusing instead would leave a box whose backup never runs again until
    # somebody logs in, which is worse than losing the debris of a killed run.
    # The plan slot is cleared for the same reason and with less at stake.
    if [ -d "$RUN_DIR" ]; then
        case "$RUN_NAME" in
            .incoming) warn "clearing a leftover .incoming under $BACKUP_TARGET - the previous run was killed before it could finish or set itself aside" ;;
        esac
        rm -rf -- "$RUN_DIR"
    fi
    mkdir "$RUN_DIR" || die "could not create $RUN_DIR - is $BACKUP_TARGET writable?"
else
    RUN_NAME="${RUN_PREFIX}_$RUN_TS"
    RUN_DIR="$BACKUP_TARGET/$RUN_NAME"
    # `mkdir`, NOT `mkdir -p`, ON THE RUN DIRECTORY. `-p` accepts an existing one, so
    # two runs started inside the same UTC second would share it: one manifest
    # overwriting the other, two sets of archives interleaved, and both reporting
    # success. The timer plus a hand-started run is exactly how that happens.
    # (Adversarial review, 2026-08-29.)
    mkdir "$RUN_DIR" || die "$RUN_DIR already exists, or could not be created. Another run started in the same second, or the drive is read-only. Refusing rather than sharing a run directory with something else."
fi
MANIFEST="$RUN_DIR/MANIFEST.tsv"
mkdir -p "$STAGING"
LOG_FILE="$RUN_DIR/backup.log"

# Run totals + the report fields, initialised BEFORE the failure machinery below
# so the very first failure can already write a complete RUN.json.
TOTAL_BYTES=0; TOTAL_FILES=0; SET_SUMMARY=""; SET_SUMMARY_JSON=""
INGEST_SUMMARY="none"

# json_str VALUE — the value as a JSON string literal, quotes included.
#
# WITHOUT THIS, RUN.json IS ONLY PROBABLY VALID. `note` carries FAIL_NOTE and
# SET_SUMMARY, which hold set names, paths and rsync/tar error text — any of
# which can contain a quote, a backslash or a newline. One of those and the file
# stops parsing, which loses the run's durable verdict AND (before the anchored
# grep in retention_prune) could make a FAILED run read as good.
# (Adversarial review, 2026-08-29.)
#
# PURE PARAMETER EXPANSION, NOT A sed|awk PIPELINE. The first cut was a pipeline
# and it silently dropped the backslash out of `\n`, turning a two-line note into
# "twonlines" — three quoting layers (shell, sed, awk) each eating one escape.
# Bash's own substitution has exactly one layer.
json_str() {
    local v="$1"
    v="${v//\\/\\\\}"      # backslash first, or it re-escapes the ones added below
    v="${v//\"/\\\"}"
    v="${v//$'\t'/\\t}"
    v="${v//$'\r'/}"
    v="${v//$'\n'/\\n}"
    printf '"%s"' "$v"
}

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
  "note": $(json_str "$note")
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
# set_aside_flat : in the FLAT layout, move the failed run out of the way.
#
# Two things depend on it, and both are about the copy that is ALREADY there:
#   * the previous good copy under $BACKUP_TARGET must survive tonight's failure
#     untouched. It is what a restore would find, and a flat layout has no dated
#     sibling to fall back to - so a run that failed while writing over it would
#     leave the household with no copy at all;
#   * the next run must not find .incoming sitting in its way. A failed run that
#     tidied itself away completely would take its own evidence with it, so it is
#     MOVED rather than deleted - into ONE slot, because two .last-failed
#     directories are not twice the evidence.
# Never raises: report_failure must always return 0, and a failure to file the
# evidence must not become a second, louder failure than the real one.
set_aside_flat() {
    [ "$LAYOUT" = flat ] || return 0
    [ "${RUN_NAME:-}" = ".incoming" ] || return 0
    [ -d "$RUN_DIR" ] || return 0
    rm -rf -- "$BACKUP_TARGET/.last-failed" 2>/dev/null || true
    if mv -- "$RUN_DIR" "$BACKUP_TARGET/.last-failed" 2>/dev/null; then
        LOG_FILE="$BACKUP_TARGET/.last-failed/backup.log"
        log "the failed run was set aside as $BACKUP_TARGET/.last-failed - the copy already in $BACKUP_TARGET is untouched, and it is what a restore would find"
    else
        warn "could not set the failed run aside as .last-failed - it is still at $RUN_DIR, and the NEXT run will clear it"
    fi
    return 0
}
report_failure() {
    local note="$1"
    [ "$REPORTED_FAILURE" = 0 ] || return 0
    REPORTED_FAILURE=1
    umount_all
    feed_naglight false "backup FAILED: $note"
    write_run_json "failed" "$note"
    log "BACKUP FAILED: $note"
    set_aside_flat
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
# directories already on the drive carry the old one. Since Q1 it does not have
# to read a log at all in the common case - the directory NAME carries it.
log "config=$CONFIG target=$BACKUP_TARGET layout=$LAYOUT keep=$KEEP mode=$RUN_MODE"
if [ "$PLAN_ONLY" = 1 ]; then
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
# run. History is the LIBRARY BACKUP's Snapshot_<date> series - the FileBackup
# container covers /srv/library whole - not this service's run directories: the
# library trees stopped being BACKUP_SOURCES rows on 2026-08-31, and since
# 2026-09-01 this service writes no dated directory at all. Every failure here is
# loud (OI-9 die reporting).
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

# ── plan-litter retention (Q1) ───────────────────────────────────────────────
# Plan directories now have their own prefix, so they can have their own budget,
# and this is where the second half of Q1 is paid for: WHO prunes them.
#
# The obvious answer — let retention_prune do it at the end of a nightly — is the
# wrong one, and the reason is the failure that was actually observed. Plan runs
# are produced by verify-hub.sh, twelve in one day on 2026-08-28, while
# retention_prune runs ONLY after a green full run (an ordering fixed on
# 2026-08-09, and correct: a red night must not rotate away good nights). So
# under the obvious answer, plan litter is bounded by the NIGHTLY's health — a
# box whose backup is failing accumulates plan directories forever, on exactly
# the drive whose free space the failure may be about.
#
# So a plan run prunes plan directories itself. It is safe in a way retention is
# not: this only ever removes directories matching plan_* that hold no RUN.json
# and no archive, i.e. log-only litter of the same kind this very run just
# created. It CANNOT touch run_*, and the refusal below is not decoration — if a
# plan_ directory ever holds an archive, something is wrong and deleting the
# evidence is the worst available move.
# NOTE ON THE ERR TRAP: this function is called on the plan path with the trap
# DISARMED by its caller, deliberately. Arithmetic like `(( x < 0 ))` returns 1
# when false, which is enough to fire `trap ... ERR` and turn a successful plan
# run into a reported failure — the existing retention_prune only gets away with
# the same expression because it runs after step 6's `trap - ERR`. Written with
# `if` rather than `&&` here so it is safe either way, and the caller disarms
# anyway. Two belts, because the cost of being wrong is a false red backup.
prune_plan_dirs() {
    local d n_dir n=0 kept=0 skipped=0 older i
    while IFS= read -r n_dir; do
        [ "$n_dir" = "$RUN_NAME" ] && continue            # never the current run
        n=$(( n + 1 ))
    done < <(find "$BACKUP_TARGET" -mindepth 1 -maxdepth 1 -type d -name 'plan_*' -printf '%f
' 2>/dev/null | sort)
    # Second pass with the count known: keep the newest $PLAN_KEEP, prune the rest.
    # THE BUDGET IS A TOTAL. `n` excludes the run doing the pruning, so when a
    # PLAN run prunes, its own directory is one of the survivors and only
    # PLAN_KEEP-1 older ones may stay. A full run has no plan directory of its
    # own, so it keeps PLAN_KEEP. Without this, BACKUP_PLAN_KEEP=N left N+1.
    # (Adversarial review, 2026-08-29.)
    local budget="$PLAN_KEEP"
    case "$RUN_NAME" in plan_*) budget=$(( PLAN_KEEP - 1 )) ;; esac
    if [ "$budget" -lt 0 ]; then budget=0; fi
    older=$(( n - budget )); i=0
    if [ "$older" -lt 0 ]; then older=0; fi
    log "plan retention: $n other plan director(ies) on $BACKUP_TARGET, budget $PLAN_KEEP total (keeping $budget of them)"
    while IFS= read -r n_dir; do
        d="$BACKUP_TARGET/$n_dir"
        [ "$n_dir" = "$RUN_NAME" ] && continue
        i=$(( i + 1 ))
        if [ "$i" -gt "$older" ]; then kept=$(( kept + 1 )); continue; fi
        # REFUSE ON ANYTHING THAT IS NOT LITTER. A plan directory holds logs and
        # a header-only manifest; a RUN.json or an archive in one means the
        # assumption behind this whole function is broken.
        if [ -e "$d/RUN.json" ] || compgen -G "$d/*.tar" >/dev/null || compgen -G "$d/*.tar.zst" >/dev/null; then
            warn "plan retention: REFUSING to prune $n_dir — it holds an archive or a RUN.json, which a plan run never writes. Left in place; look at it."
            skipped=$(( skipped + 1 )); continue
        fi
        log "  prune old plan run $n_dir"; rm -rf "${BACKUP_TARGET:?}/$n_dir"
    done < <(find "$BACKUP_TARGET" -mindepth 1 -maxdepth 1 -type d -name 'plan_*' -printf '%f
' 2>/dev/null | sort)
    [ "$skipped" -eq 0 ] || warn "plan retention: $skipped director(ies) refused as above"
}

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
    log "  the directory stays on $BACKUP_TARGET as the plan's evidence; it holds logs only, and it"
    log "  is named plan_ so it cannot be mistaken for an archive run (Q1)."
    if [ "$LAYOUT" = flat ]; then
        log "  The flat layout gives it ONE slot, plan_latest, replaced by the next plan run - so there"
        log "  is no budget to keep and nothing to prune (BACKUP_PLAN_KEEP is not read in this layout)."
        trap - ERR
    else
        log "  Plan directories carry their own budget — BACKUP_PLAN_KEEP=$PLAN_KEEP — pruned by plan"
        log "  runs, not by the nightly."
        trap - ERR
        prune_plan_dirs
    fi
    exit 0
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
    report_failure "source directory missing for: $MISSING_SETS — the other set(s) WERE archived to $RUN_NAME (${SET_SUMMARY:-none}); create the path(s) or remove the set from BACKUP_SOURCES"
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
# `run_*` HERE IS NOW EXACT, NOT APPROXIMATE. Before Q1 this glob also caught
# every plan directory, which landed them in `bad` and made the log call them
# "failed" runs — mis-COUNTED, as C25 recorded, even though they were pruned
# correctly. With the plan prefix split off, `bad` means what it says: a real
# archive run that did not finish ok.
# promote_flat : make this run's output THE copy under $BACKUP_TARGET.
#
# THE FLAT LAYOUT'S ANSWER TO RETENTION. There is nothing to rotate - one copy,
# replaced - but "replaced" still has to be done in an order that never leaves
# the household with less than it started with. So the run built itself in
# .incoming, this is called only after the verdict is GREEN, and a run that
# failed anywhere above never reaches it.
#
# Two halves, in this order:
#   1. REMOVE THE STALE ARTEFACTS THIS RUN DID NOT REPRODUCE. Without it, a set
#      renamed or dropped from BACKUP_SOURCES leaves its last archive lying here
#      looking current, forever, and a restore would happily use it. It matches
#      only the file shapes this service itself writes: anything else in the
#      folder is left alone rather than tidied away by a backup script.
#   2. MOVE this run's files up. Same filesystem, so every one is a rename.
#
# LOG_FILE AND MANIFEST ARE REPOINTED AT THE END, because the files they name
# have just moved and everything after this still logs.
promote_flat() {
    local f base removed=0 moved=0
    for f in "$BACKUP_TARGET"/*.tar "$BACKUP_TARGET"/*.tar.zst \
             "$BACKUP_TARGET"/*.files.tsv "$BACKUP_TARGET"/*.excluded.log \
             "$BACKUP_TARGET/MANIFEST.tsv" "$BACKUP_TARGET/RUN.json" \
             "$BACKUP_TARGET/backup.log"; do
        [ -f "$f" ] || continue
        base="${f##*/}"
        [ -e "$RUN_DIR/$base" ] && continue
        rm -f -- "$f" || return 1
        removed=$(( removed + 1 ))
    done
    log "promoting $RUN_NAME into $BACKUP_TARGET ($removed stale file(s) from an earlier run removed)"
    for f in "$RUN_DIR"/*; do
        [ -e "$f" ] || continue
        mv -f -- "$f" "$BACKUP_TARGET/" || return 1
        moved=$(( moved + 1 ))
    done
    rmdir "$RUN_DIR" 2>/dev/null || warn "$RUN_DIR is not empty after promotion - look at what is left in it"
    LOG_FILE="$BACKUP_TARGET/backup.log"
    MANIFEST="$BACKUP_TARGET/MANIFEST.tsv"
    log "promoted $moved file(s); $BACKUP_TARGET now holds the current copy"
    return 0
}

# -- fib_rm_under <victim> - delete, but only inside BACKUP_TARGET ------------
# THE LAST LINE OF DEFENCE, and it is deliberately paranoid. Every path this
# layout removes is composed from $BACKUP_TARGET plus a name that came off the
# filesystem, and composed paths are exactly where an empty variable, a stray
# symlink or a name nobody expected turns `rm -rf` into something else entirely.
# Rather than trust each construction site, every removal is checked here against
# the one directory this layout is allowed to touch.
#
# Refusing is never fatal on its own - the caller decides - but it IS reported,
# because a retention step that quietly declined to delete anything is how a
# drive fills up six months later.
fib_rm_under() {
    local victim="${1:-}" root="${BACKUP_TARGET%/}"
    if [ -z "$root" ] || [ "$root" = "/" ]; then
        warn "refusing to delete '$victim': BACKUP_TARGET is empty or /"
        return 1
    fi
    case "$victim" in
        */../*|*/..|../*|*..*)
            warn "refusing to delete '$victim': the path contains '..'"; return 1 ;;
        "$root"/?*) ;;                       # strictly BELOW the target, never the target itself
        *)
            warn "refusing to delete '$victim': it is not inside $root"; return 1 ;;
    esac
    rm -rf -- "$victim"
}

# -- fib_swap_dir <staged> <live> - install without a moment of neither --------
# EVERY REPLACEMENT IN THIS LAYOUT GOES THROUGH HERE, because the obvious form
#     rm -rf "$live"; mv "$staged" "$live"
# has a window in which NEITHER exists, and a kill, an I/O error or a remount
# read-only inside that window destroys the copy for good. On a rung that copy is
# the only sample of its age anywhere. (Adversarial review, 2026-09-08.)
#
# The old directory is renamed aside first, so a failure to install the new one
# can put it straight back. Only once the new copy is in place is the old one
# deleted. Both moves are renames within one filesystem.
#
# THE `rm` RESULT IS CHECKED, which is the second half of the same finding: if a
# partially-failed removal left the live directory in place, `mv staged live`
# would move the staged copy INSIDE it and return success - logging a refill that
# did not happen while the stale rung stayed live.
fib_swap_dir() {
    local staged="$1" live="$2" old="$2.old.$$"
    fib_rm_under "$old" || return 1
    if [ -e "$live" ]; then
        mv -f -- "$live" "$old" || return 1
    fi
    if ! mv -f -- "$staged" "$live"; then
        # Put the previous copy back rather than leaving nothing at all.
        [ -e "$old" ] && mv -f -- "$old" "$live"
        return 1
    fi
    fib_rm_under "$old" || warn "could not remove the superseded copy at $old - it is stale but harmless"
    return 0
}

# -- promote_fib - .incoming becomes tonight's daily --------------------------
# Same filesystem, so this is a rename: daily/<date> either does not exist or is
# a complete run, never a half-populated directory that the cascade or the trim
# could pick up mid-write.
promote_fib() {
    local dest="$BACKUP_TARGET/daily/$FIB_TODAY" moved
    mkdir -p "$BACKUP_TARGET/daily" || return 1
    # A SECOND RUN ON THE SAME DAY REPLACES THAT DAY'S COPY. The run that
    # finishes last wins, which is the rule the flat layout applies to its single
    # copy. It goes through fib_swap_dir so that a failure part-way leaves the
    # EARLIER copy of today in place rather than nothing - on day one that copy
    # can be the only completed backup on the drive.
    [ -d "$dest" ] && log "replacing an existing daily for $FIB_TODAY - a second run today, or a re-run after a fixed fault"
    fib_swap_dir "$RUN_DIR" "$dest" || return 1
    moved=$(find "$dest" -maxdepth 1 -type f | wc -l)
    # REPOINTED BECAUSE THE FILES THEY NAME HAVE JUST MOVED, and everything after
    # this still logs. Identical reasoning to promote_flat's tail.
    LOG_FILE="$dest/backup.log"
    MANIFEST="$dest/MANIFEST.tsv"
    log "promoted $RUN_NAME to daily/$FIB_TODAY ($moved file(s))"
    return 0
}

# -- fib_retention - walk the ladder oldest-first, then trim the dailies -------
# RUNS AFTER PROMOTION, unlike every other retention in this file, because both
# halves operate on tonight's copy: the trim counts it toward the window, and the
# ladder needs the daily tree complete before it can pick a source.
#
# TWO MARKER FILES PER RUNG, and they answer different questions:
#   REFILLED  the date this rung was last written. The CADENCE is measured from
#             this. It has to be stored rather than inferred, because in a
#             cascade the contents' age no longer tells you when the rung was
#             filled.
#   CAPTURED  the date of the DAILY the contents ultimately came from, carried up
#             the ladder unchanged at every hop. This is the provenance: it is
#             what says a rung really holds 200-day-old state and not a copy of
#             last Tuesday.
# BOTH ARE VALIDATED ON READ. A merely-non-empty marker reaches `date -d`, which
# accepts far more than an ISO date; a marker holding a FUTURE date makes the
# elapsed-days arithmetic negative, and a negative interval is always less than
# the cadence, so the rung would be "not due" forever - frozen, silently, on a
# service whose whole point is not being silently wrong.
#
# RETURNS NON-ZERO IF ANY PART OF THE LADDER FAILED. The archives are already
# written and promoted by this point, so nothing here aborts the run - but the
# ladder IS the long-term history in this layout, and a night that could not
# maintain it has not done what it was asked. The caller turns that into a red
# report rather than a green one. (Adversarial review, 2026-09-08.)
fib_retention() {
    local dailies=() d n i slot cad slotdir srcdir srccap last since failed=0
    # ISO-DATED DIRECTORIES ONLY, and this is a safety property rather than
    # tidiness. Anything else under daily/ was not written by this service, so
    # counting it would push a real backup out of the window, and PRUNING it would
    # delete somebody else's directory because it happened to sort early. Strays
    # are named in the log and then left completely alone.
    while IFS= read -r d; do dailies+=("$d"); done < <(
        find "$BACKUP_TARGET/daily" -mindepth 1 -maxdepth 1 -type d \
             -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' -printf '%f\n' 2>/dev/null | sort)
    n=${#dailies[@]}
    _stray=$(find "$BACKUP_TARGET/daily" -mindepth 1 -maxdepth 1 \
                  ! -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' -printf '%f\n' 2>/dev/null | tr '\n' ' ')
    [ -n "$_stray" ] && warn "ladder: ignoring non-dated entries under daily/ (neither counted nor pruned): $_stray"
    unset _stray
    log "ladder: $n daily cop(ies) present (window $FIB_DAILY_KEEP)"
    if [ "$n" = 0 ]; then
        warn "ladder: no daily copies at all - nothing to cascade from"
        return 1
    fi

    mkdir -p "$BACKUP_TARGET/fib" || { warn "ladder: could not create $BACKUP_TARGET/fib"; return 1; }

    # -- OLDEST RUNG FIRST. See the design note on BACKUP_LAYOUT above: 233 must
    # take 144's contents BEFORE 144 is overwritten by 89. Descending index is
    # the entire mechanism by which one night's daily advances exactly one rung
    # instead of all seven.
    for (( i = ${#FIB_SLOT_LIST[@]} - 1; i >= 0; i-- )); do
        slot="${FIB_SLOT_LIST[$i]}"
        cad="${FIB_CADENCE[$i]}"
        slotdir="$BACKUP_TARGET/fib/$slot"

        last=""
        [ -f "$slotdir/REFILLED" ] && last="$(cat "$slotdir/REFILLED" 2>/dev/null)"
        if [ -n "$last" ] && ! fib_valid_date "$last"; then
            warn "ladder: rung ${slot}d has an unreadable REFILLED marker ('$last') - treating it as due rather than leaving it frozen"
            failed=1; last=""
        fi
        if [ -n "$last" ]; then
            since=$(( ( $(date -u -d "$FIB_TODAY" +%s) - $(date -u -d "$last" +%s) ) / 86400 ))
            if [ "$since" -lt 0 ]; then
                # A FUTURE MARKER, i.e. the clock has moved backwards since it was
                # written. Left alone this rung never comes due again.
                warn "ladder: rung ${slot}d was refilled on $last, which is AFTER today ($FIB_TODAY) - the clock has moved backwards; treating the rung as due"
                failed=1; since=$cad
            elif [ "$since" -lt "$cad" ]; then
                log "  rung ${slot}d: refilled $last (${since}d ago) - next due at ${cad}d"
                continue
            fi
        else
            since="-"
        fi

        # WHERE THIS RUNG DRAWS FROM: the rung below it, or - for the lowest -
        # the OLDEST daily in the window, which is the closest thing to the
        # 8-day-old sample the sequence would otherwise call for.
        if [ "$i" = 0 ]; then
            srcdir="$BACKUP_TARGET/daily/${dailies[0]}"
            srccap="${dailies[0]}"
        else
            srcdir="$BACKUP_TARGET/fib/${FIB_SLOT_LIST[$(( i - 1 ))]}"
            srccap=""
            [ -f "$srcdir/CAPTURED" ] && srccap="$(cat "$srcdir/CAPTURED" 2>/dev/null)"
        fi

        # SKIPPED WITHOUT ADVANCING THE CLOCK, so this rung refills the instant
        # the one below it first has something to give. On a new box that walks
        # the ladder up from the bottom one rung per night; after an outage it
        # closes the gap the same way. Not a failure: it is how the ladder fills.
        if [ ! -d "$srcdir" ] || [ -z "$srccap" ]; then
            log "  rung ${slot}d: due, but its source is not populated yet - skipping, clock not advanced"
            continue
        fi
        # A CORRUPT PROVENANCE MARKER IS NOT COPIED UPWARDS. Carrying it would
        # spread one bad rung's confusion over the whole ladder, one rung a cycle.
        if ! fib_valid_date "$srccap"; then
            warn "ladder: rung ${slot}d's source ${srcdir##*/} has an unreadable CAPTURED marker ('$srccap') - not propagating it"
            failed=1
            continue
        fi

        # BUILD BESIDE, THEN SWAP - see fib_swap_dir. The markers are written to
        # the STAGED copy and CHECKED before anything live is touched: a cp that
        # consumed the last free block leaves the marker writes failing, and
        # installing a rung whose markers do not describe its contents is worse
        # than not refilling it at all.
        fib_rm_under "$slotdir.new"
        if ! cp -a "$srcdir/." "$slotdir.new/" 2>/dev/null; then
            warn "ladder: could not stage rung ${slot}d - leaving the existing copy in place"
            fib_rm_under "$slotdir.new"
            failed=1
            continue
        fi
        if ! printf '%s\n' "$srccap"    > "$slotdir.new/CAPTURED" ||
           ! printf '%s\n' "$FIB_TODAY" > "$slotdir.new/REFILLED"; then
            warn "ladder: could not write the markers for rung ${slot}d (is the drive full?) - leaving the existing copy in place"
            fib_rm_under "$slotdir.new"
            failed=1
            continue
        fi
        if fib_swap_dir "$slotdir.new" "$slotdir"; then
            if [ "$since" = "-" ]; then
                log "  rung ${slot}d: FIRST fill from ${srcdir##*/} (state of $srccap)"
            else
                log "  rung ${slot}d: refilled from ${srcdir##*/} (state of $srccap; ${since}d since last)"
            fi
        else
            warn "ladder: could not move rung ${slot}d into place - the previous copy has been left where it was"
            fib_rm_under "$slotdir.new"
            failed=1
        fi
    done

    # NOW the window is trimmed. `dailies` is the pre-trim listing, which is what
    # the cascade above needed: the oldest daily has to still be on the drive
    # while the lowest rung is drawing from it.
    log "ladder: trimming the daily window to $FIB_DAILY_KEEP"
    for (( i = 0; i < n - FIB_DAILY_KEEP; i++ )); do
        # NEVER TONIGHT'S OWN COPY. The list is sorted by NAME, so if the clock
        # has moved backwards - an NTP correction after a flat RTC battery, say -
        # tonight's date sorts before the seven already on the drive and lands at
        # the front of the prune list. The run would then delete the backup it had
        # just taken and report ok. (Adversarial review, 2026-09-08.)
        if [ "${dailies[$i]}" = "$FIB_TODAY" ]; then
            warn "ladder: refusing to prune daily $FIB_TODAY - it is the copy this run just made, and it sorted oldest because the clock has moved backwards"
            failed=1
            continue
        fi
        log "  prune daily ${dailies[$i]}"
        fib_rm_under "$BACKUP_TARGET/daily/${dailies[$i]}" || { warn "could not prune daily ${dailies[$i]}"; failed=1; }
    done
    return "$failed"
}

retention_prune() {
    local d good=() bad=() i
    while IFS= read -r d; do
        [ "$d" = "$RUN_NAME" ] && continue                   # never the current run
        # ANCHORED TO THE FIELD, not a substring of the whole file. `note` is
        # free text written by the run itself; a note containing the characters
        # `"status": "ok"` would promote a FAILED run into the `good` list, where
        # it would consume a retention slot and evict a real archive.
        # (Adversarial review, 2026-08-29.)
        if grep -qE '^[[:space:]]*"status":[[:space:]]*"ok"[[:space:]]*,?[[:space:]]*$' "$BACKUP_TARGET/$d/RUN.json" 2>/dev/null; then
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
    # A green nightly also tidies plan litter. Not because it has to — plan runs
    # prune their own — but because a box where verify is run once and then never
    # again would otherwise keep that one plan directory forever.
    prune_plan_dirs
}
if [ "$LAYOUT" = fib ]; then
    # DEFERRED, NOT SKIPPED. The ladder and the daily prune both need this run's
    # copy to be under daily/ first, so they run from the promotion block below
    # rather than here. Said out loud because a retention step that logs nothing
    # reads exactly like one that was forgotten.
    log "retention: deferred - the fib ladder runs after promotion, once tonight's copy is under daily/$FIB_TODAY."
elif [ "$LAYOUT" = flat ]; then
    log "retention: none - the flat layout holds ONE current copy in $BACKUP_TARGET, which this run replaces."
    log "  BACKUP_KEEP=$KEEP and BACKUP_PLAN_KEEP=$PLAN_KEEP are NOT read in this layout and nothing is pruned."
    log "  The history of this folder is the library backup's Snapshot_<date> series, which versions it per file."
else
    retention_prune
fi

# THE LAST THREE STEPS ARE CHECKED, and until 2026-08-29 they were not. This
# code runs after `trap - ERR` (deliberately — a failure here must not be
# reported by the ERR trap's line number) and the file has no `set -e`, so a
# write_run_json that could not write, or a feed that did not land, was ignored
# and the run still logged "backup OK" and exited 0.
#
# Both matter for different reasons and neither is theoretical:
#   RUN.json  is what retention reads to decide this run is worth keeping, and
#             what the capacity preflight reads to size the next one. A run with
#             no RUN.json is classified `bad` by its own next sibling.
#   the feed  is the ONLY thing that tells anybody the backup happened. A silent
#             failure here is the exact never-silent-green shape this service is
#             built around.
# (Adversarial review, 2026-08-29.)
if ! write_run_json "ok" "sets: ${SET_SUMMARY:-none}"; then
    report_failure "the archives are complete and verified, but RUN.json could not be written to $RUN_DIR — retention will class this run as failed and the next run cannot size itself from it"
    exit 1
fi

# ONLY NOW does the flat layout touch the copy the household already had. Every
# check above has passed and RUN.json says ok, so the previous copy has been
# intact for the whole run and is replaced by a complete, verified one.
if [ "$LAYOUT" = flat ]; then
    if ! promote_flat; then
        report_failure "the archives are complete and verified in $RUN_DIR, but they could not be moved into place in $BACKUP_TARGET - a restore would still find the PREVIOUS copy, which is intact"
        exit 1
    fi
elif [ "$LAYOUT" = fib ]; then
    if ! promote_fib; then
        report_failure "the archives are complete and verified in $RUN_DIR, but they could not be moved into $BACKUP_TARGET/daily/$FIB_TODAY - yesterday's daily and the whole fib ladder are untouched and intact"
        exit 1
    fi
    # A LADDER THAT COULD NOT BE MAINTAINED IS NOT A GREEN NIGHT. Tonight's copy
    # is safely under daily/ and is not withdrawn - but in this layout the ladder
    # IS the long-term history, so a run that only managed the daily has not done
    # what it was asked, and saying otherwise is the silent-green failure this
    # service exists to refuse. The warnings above name the specific rung.
    # (Adversarial review, 2026-09-08.)
    if ! fib_retention; then
        feed_naglight false "backup $RUN_TS - tonight's copy is in daily/$FIB_TODAY, but the fib ladder could NOT be maintained; long-term history is not advancing"
        log "ERROR: the archives are complete and promoted, but the fib ladder failed."
        log "  daily/$FIB_TODAY is intact and restorable. What did not happen is the"
        log "  cascade into fib/, which is the only long-term history in this layout."
        log "  Exiting non-zero so the unit shows red rather than reporting a green"
        log "  night on a retention policy that has stopped working."
        exit 1
    fi
fi
if ! feed_naglight true "backup ok $RUN_TS — ${SET_SUMMARY:-no sets}"; then
    log "ERROR: the backup succeeded but the report did NOT reach NagLight."
    log "  The archives in $RUN_DIR are complete and verified; what failed is the"
    log "  telling. Exiting non-zero so the unit shows as failed, because a backup"
    log "  nobody is told about is the failure mode this service exists to refuse."
    exit 1
fi
log "== backup OK: $TOTAL_FILES file(s), $TOTAL_BYTES byte(s) across sets =="
log "manifest: $MANIFEST"
exit 0
