#!/usr/bin/env bash
# vmtest/make-gate-iso.sh — derive a VM-gate ISO from a production ISO.
#
# WHY THIS EXISTS. Start-InstallGate.ps1 needs the production image to install
# to completion in a VM, unattended. The shipped ISO cannot: its default entry
# is pinned to a disk serial no virtual disk reports, and its unpinned entry is
# INTERACTIVE at storage by design — a human picks and confirms the disk.
#
# The obvious answer was to drive that screen with the synthetic keyboard. It
# was rejected: subiquity's storage TUI would have to be navigated blind, with
# no way to read back what is focused, so the gate would race the installer's
# redraws. A gate that is sometimes right is worse than no gate.
#
# THE ANSWER THIS SCRIPT IMPLEMENTS is a derived ISO that differs from the
# shipped one in exactly two ways, both asserted below:
#
#   1. a third seed, /nocloud-gate/, identical to /nocloud-confirm/ except that
#      `interactive-sections` goes back to [] — unattended, still unpinned, so
#      it installs to whatever single disk the VM has;
#   2. one extra menuentry, LAST, pointing at that seed.
#
# Everything the gate is actually testing — the package list, late-commands,
# the payload, firstboot, the identity, the SSH keys — is byte-identical to
# what ships. That equivalence is asserted, not assumed: the gate seed is
# GENERATED from the shipped one and the diff is checked line by line.
#
# WHY NOT PUT AN UNATTENDED-UNPINNED ENTRY ON THE SHIPPED MEDIA: it would be an
# entry that silently wipes whatever disk it finds, one keypress from an
# operator at a real machine. The whole point of the pin is that no such thing
# exists on media that leaves the desk. It exists here, on an artifact that
# only ever boots a throwaway VM.
#
# Run in WSL (Ubuntu). Requires: xorriso.
#
# Usage:
#   bash vmtest/make-gate-iso.sh --src-iso /mnt/d/vmtest-out-hub-prod/repacked.iso
#   bash vmtest/make-gate-iso.sh --src-iso ... --out /mnt/d/gate/hub-gate.iso

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

SRC_ISO=""
OUT_ISO=""
while [ $# -gt 0 ]; do
    case "$1" in
        --src-iso) SRC_ISO="$2"; shift 2 ;;
        --out)     OUT_ISO="$2"; shift 2 ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) die "unknown argument '$1' (try --help)" ;;
    esac
done

[ -n "$SRC_ISO" ] || die "need --src-iso /path/to/repacked.iso"
[ -f "$SRC_ISO" ] || die "not found: $SRC_ISO"
[ -n "$OUT_ISO" ] || OUT_ISO="${SRC_ISO%.iso}-gate.iso"

require_cmd xorriso "Install with: sudo apt-get install -y xorriso"
require_writable_output "$OUT_ISO"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── 1. pull out what we are deriving from ───────────────────────────────────
# The CONFIRM seed, not the pinned one: it already has the disk pin removed,
# which is half of what the gate seed needs. Deriving from the pinned seed
# would mean re-implementing the match:-stripping awk in a second place.
log "extracting /nocloud-confirm/ and grub.cfg from $SRC_ISO"
for f in /nocloud-confirm/user-data /nocloud-confirm/meta-data /boot/grub/grub.cfg; do
    xorriso -osirrox on -indev "$SRC_ISO" -extract "$f" "$WORK/$(basename "$f").$(basename "$(dirname "$f")")" >/dev/null 2>&1 \
        || die "couldn't extract $f - is this an ISO built by build-repacked-iso.sh? A stock Ubuntu ISO has no /nocloud-confirm."
done
CONFIRM_UD="$WORK/user-data.nocloud-confirm"
CONFIRM_MD="$WORK/meta-data.nocloud-confirm"
GRUB_ORIG="$WORK/grub.cfg.grub"

# ── 2. the gate seed: unattended, still unpinned ────────────────────────────
GATE_UD="$WORK/gate-user-data"
sed -e 's/^  interactive-sections:.*/  interactive-sections: []/' "$CONFIRM_UD" > "$GATE_UD"

grep -q '^  interactive-sections: \[\]' "$GATE_UD" \
    || die "the gate seed is still interactive - it would stop at the storage screen and the gate would hang waiting for a human. Refusing to build."
awk '/^  storage:/{s=1} s && /^  [a-z_-]+:/ && !/^  storage:/{s=0} s && /^      match:/{found=1} END{exit !found}' "$GATE_UD" \
    && die "the gate seed carries a storage match: - it would halt in a VM exactly like the shipped default does. Refusing to build."

# THE EQUIVALENCE ASSERTION. This is what lets a passing gate say anything about
# the shipped image: the two seeds must differ ONLY in interactive-sections. If
# anything else drifts - a different user, a different late-command, a different
# payload path - then the gate is testing a different install and its result is
# worthless while still looking green.
DRIFT="$(diff "$CONFIRM_UD" "$GATE_UD" | grep -E '^[<>]' | grep -vE '^[<>][[:space:]]+interactive-sections:' || true)"
[ -z "$DRIFT" ] || die "the gate seed differs from the shipped seed beyond interactive-sections:
$DRIFT
The gate would not be testing the image you ship. Refusing to build."
log "gate seed: unattended, unpinned, and otherwise identical to the shipped seed"

# ── 3. one extra menuentry, LAST ───────────────────────────────────────────
# Last so the shipped entries keep their positions and `set default=0` still
# means the pinned unattended install - Run A of the gate boots this same ISO
# untouched and must still see the containment behaviour.
GATE_GRUB="$WORK/gate-grub.cfg"
DIAG_LINUX="$(grep -m1 -E '^[[:space:]]*linux[[:space:]]+/casper/[a-z-]*vmlinuz' "$GRUB_ORIG" || true)"
DIAG_INITRD="$(grep -m1 -E '^[[:space:]]*initrd[[:space:]]+/casper/[a-z-]*initrd' "$GRUB_ORIG" || true)"
[ -n "$DIAG_LINUX" ] && [ -n "$DIAG_INITRD" ] \
    || die "couldn't find a /casper kernel+initrd pair in the source grub.cfg"

# Strip any autoinstall args the source line already carries, then add ours -
# the source grub.cfg is the PATCHED one, so its first linux line points at
# /cdrom/nocloud/. Reusing it verbatim would seed the gate entry from the
# PINNED seed and it would halt.
GATE_LINUX="$(printf '%s\n' "$DIAG_LINUX" \
    | sed -e 's# autoinstall "ds=nocloud;s=[^"]*"##' \
    | sed -e 's#\(linux[[:space:]]*/casper/[a-z-]*vmlinuz\)\( \)\+---#\1 autoinstall "ds=nocloud;s=/cdrom/nocloud-gate/" ---#')"
printf '%s' "$GATE_LINUX" | grep -q 'autoinstall "ds=nocloud;s=/cdrom/nocloud-gate/"' \
    || die "couldn't build the gate kernel line from: $DIAG_LINUX"
printf '%s' "$GATE_LINUX" | grep -qE 'nocloud/|nocloud-confirm/' \
    && die "the gate kernel line still references another seed: $GATE_LINUX"

cp "$GRUB_ORIG" "$GATE_GRUB"
cat >> "$GATE_GRUB" <<EOF

menuentry "GATE - unattended, unpinned (VM ONLY)" {
	set gfxpayload=keep
$GATE_LINUX
$DIAG_INITRD
}
EOF

# ── 4. repack ──────────────────────────────────────────────────────────────
log "repacking -> $OUT_ISO (copies ~3.9GB, a few minutes)"
rm -f "$OUT_ISO"
xorriso -indev "$SRC_ISO" -outdev "$OUT_ISO" \
    -map "$GATE_GRUB" /boot/grub/grub.cfg \
    -map "$GATE_UD" /nocloud-gate/user-data \
    -map "$CONFIRM_MD" /nocloud-gate/meta-data \
    -boot_image any replay \
    >/dev/null

# ── 5. verify the artifact, not the exit code ──────────────────────────────
log "verifying"
REPORT="$(xorriso -indev "$OUT_ISO" -report_el_torito plain 2>/dev/null)"
echo "$REPORT" | grep -q BIOS || die "gate ISO lost its BIOS boot image"
echo "$REPORT" | grep -q UEFI || die "gate ISO lost its UEFI boot image"
xorriso -indev "$OUT_ISO" -find /nocloud-gate/user-data >/dev/null 2>&1 \
    || die "gate ISO is missing /nocloud-gate/user-data but its menu offers the entry that boots from it"
xorriso -indev "$OUT_ISO" -find /deploy-payload >/dev/null 2>&1 \
    || die "gate ISO lost /deploy-payload - it would install nothing and the gate would report that as an image defect"

log "OK - gate ISO ready: $OUT_ISO"
log "     Its LAST menu entry installs unattended to whichever disk the VM has."
log "     NEVER write this to physical media: that entry wipes any disk it finds."
