#!/bin/sh
# homehub-fstrim — weekly SSD trim, without asking a spinning USB disk to do it.
#
# WHAT fstrim IS FOR. An SSD cannot overwrite a block in place; it must erase it
# first. `fstrim` tells the drive which blocks the filesystem has stopped using,
# so the erase happens in advance instead of in the middle of a later write.
# Stock `fstrim.timer` runs it weekly and it is doing real work on this box: the
# last stock run trimmed 33.6 GiB on `/` and another 33.6 GiB on
# /var/lib/docker. Nothing here disables any of that.
#
# WHY THE STOCK UNIT GOES RED ANYWAY, measured on the hub 2026-09-16:
#
#   fstrim: /srv/library: FITRIM ioctl failed: Remote I/O error
#   fstrim.service: Main process exited, code=exited, status=64/USAGE
#
# /srv/library is a USB-attached 3.6 TB spinning disk (ROTA=1, DISC-MAX 0B) —
# there is nothing on a magnetic platter to pre-erase, so the request is
# meaningless and the USB bridge answers with an I/O error. The stock unit
# already passes `--quiet-unsupported`, which exists for exactly this, but that
# option suppresses EOPNOTSUPP ("the device does not support this") and this
# enclosure returns EREMOTEIO instead. Different errno, not suppressed, exit 64,
# red unit. Nothing is wrong with the drive and nothing is wrong with the
# trimming of the SSD.
#
# THE RULE HERE IS THE DEVICE'S OWN ANSWER, NOT A PATH. An earlier draft of the
# fix excluded /srv/library by name. That would have left /mnt/backup-drive —
# the OTHER USB spinning disk in this box, also DISC-MAX 0B — one firmware
# quirk away from producing the identical red line, and it would have gone on
# skipping the library forever if that mount ever became an SSD. So the
# question asked of every candidate is the one that actually decides the
# outcome: DOES THIS DEVICE ADVERTISE DISCARD SUPPORT AT ALL? A device that
# says 0 is skipped with a reason; every device that says otherwise is trimmed
# by stock fstrim, with stock options, and a genuine failure there is still a
# failure.
#
# NOT SILENT. Every skip prints why, so a drive that quietly stops advertising
# discard after a firmware or kernel change is readable in the journal rather
# than being a trim that simply stopped happening.
set -u

SKIPPED=0
TRIMMED=0
FAILED=0
SEEN=""

# THE LIST IS SPOOLED TO A FILE FIRST, and that is not fussiness. Reading it
# straight off a pipe puts the loop in a SUBSHELL, so every counter below would
# be incremented in a process that then exits — the summary would read "0
# trimmed" after a clean run and, worse, the exit status would say success
# after a genuine failure.
CANDIDATES=$(mktemp) || exit 1
trap 'rm -f "$CANDIDATES"' EXIT INT TERM

# --real drops the pseudo filesystems (proc, sysfs, cgroup, tmpfs...) that could
# never be trimmed. Deduplicating by SOURCE is what stock fstrim does for us
# when it is handed the whole list at once: without it a bind mount would ask
# the same device to trim itself twice.
findmnt --real --noheadings --raw --output TARGET,SOURCE 2>/dev/null > "$CANDIDATES"

while read -r target source; do
    [ -n "${target:-}" ] && [ -n "${source:-}" ] || continue
    case " $SEEN " in *" $source "*) continue ;; esac
    SEEN="$SEEN $source"

    # DISC-MAX is the maximum discard bytes the kernel will accept for this
    # device; 0B means the device (or, here, the USB bridge in front of it)
    # advertises no discard support whatsoever.
    limit=$(lsblk --nodeps --noheadings --output DISC-MAX "$source" 2>/dev/null | tr -d ' ')
    case "${limit:-0B}" in
        ""|0|0B)
            echo "skip $target ($source): the device advertises no discard support (DISC-MAX ${limit:-unknown})"
            SKIPPED=$((SKIPPED + 1))
            continue ;;
    esac

    if fstrim --verbose --quiet-unsupported "$target"; then
        TRIMMED=$((TRIMMED + 1))
    else
        echo "FAILED to trim $target ($source) — this one is a real fault: the device"
        echo "  advertises discard support (DISC-MAX $limit) and then refused the request."
        FAILED=$((FAILED + 1))
    fi
done < "$CANDIDATES"

echo "fstrim: $TRIMMED trimmed, $SKIPPED skipped (no discard support), $FAILED failed"
[ "$FAILED" -eq 0 ]
