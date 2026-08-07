#!/usr/bin/env bash
# vmtest/make-gate-iso.sh — derive a VM-gate ISO from a production ISO.
#
# WHY THIS EXISTS. The lab gate (HomeHub Start-VirtualHomeHub.ps1) needs the
# production image to install
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
TARGET="hub"
while [ $# -gt 0 ]; do
    case "$1" in
        --src-iso) SRC_ISO="$2"; shift 2 ;;
        --out)     OUT_ISO="$2"; shift 2 ;;
        --target)  TARGET="$2"; shift 2 ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) die "unknown argument '$1' (try --help)" ;;
    esac
done
case "$TARGET" in hub|wall) ;; *) die "--target must be hub or wall (got '$TARGET')" ;; esac

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

# ── 2a. WALL ONLY: Wi-Fi cannot be virtualised ─────────────────────────────
# THE ONE DIVERGENCE THAT CANNOT BE DESIGNED AWAY. Hyper-V has no synthetic
# 802.11 device - a guest always sees an Ethernet NIC - and Gen2 VMs support no
# USB passthrough, while DDA (the PCIe route) is not available on Windows 11
# Pro. So the panel's `wifis:` block, which matches wl*, matches nothing in a
# VM and the panel comes up with no network at all.
#
# The gate therefore rewrites the block to `ethernets:` matching e*, keeping
# `set-name: wlan0` so every downstream reference to the interface name still
# resolves. THE GATE PROVES NOTHING ABOUT THE Wi-Fi PATH - not the SSID, not
# the PSK, not roaming, not the driver. That was true of the old sim gate too;
# it is stated here because this ISO is otherwise the production artifact and
# the temptation to read a pass as covering everything is correspondingly
# stronger.
#
# A side benefit worth naming: dropping access-points means the gate ISO does
# not carry the Wi-Fi PSK, so of the two ISOs on the disk only the shippable
# one holds it.
if [ "$TARGET" = wall ]; then
    awk '
      dropping { if ($0 ~ /^          /) next; dropping = 0 }
      /^    wifis:[[:space:]]*$/            { print "    ethernets:"; next }
      /^        access-points:[[:space:]]*$/ { dropping = 1; next }
      /^        macaddress:/                { print "        # (gate ISO) macaddress dropped: no virtual Wi-Fi device exists to pin."; next }
      /^          name: "wl\*"/             { print "          name: \"e*\""; next }
                                            { print }
    ' "$GATE_UD" > "$GATE_UD.net" && mv "$GATE_UD.net" "$GATE_UD"

    grep -q '^    ethernets:' "$GATE_UD" \
        || die "the wall gate seed still has no ethernets: block - the panel would boot with no network. Refusing to build."
    grep -qE '^\s*(wifis:|access-points:)' "$GATE_UD" \
        && die "the wall gate seed still carries wifis:/access-points: - it would match no interface in a VM. Refusing to build."
    grep -q 'name: "e\*"' "$GATE_UD" \
        || die "the wall gate seed does not match e* - it would find no interface. Refusing to build."

    # The static address, the default route and the nameservers are CARRIED
    # THROUGH from the shipped seed, not injected here. The panel is statically
    # addressed in production as of 2026-08-06 (the Owner: hold the address on
    # the panel, keep it outside the DHCP pool), so the gate inherits the real
    # values and this rewrite is reduced to the one thing Hyper-V forces —
    # swapping the radio for a wire. There was an --static-addr path here for a
    # day; it existed only to work around the panel's address living in the
    # router, and deleting it is the point of the change rather than a side
    # effect of it.
    grep -q '^        addresses: \[' "$GATE_UD" \
        || die "the wall gate seed has no static address - the panel would come up with no address at all, since the DHCP that used to fill this in was removed from the shipped image. Refusing to build."
fi

grep -q '^  interactive-sections: \[\]' "$GATE_UD" \
    || die "the gate seed is still interactive - it would stop at the storage screen and the gate would hang waiting for a human. Refusing to build."
awk '/^  storage:/{s=1} s && /^  [a-z_-]+:/ && !/^  storage:/{s=0} s && /^      match:/{found=1} END{exit !found}' "$GATE_UD" \
    && die "the gate seed carries a storage match: - it would halt in a VM exactly like the shipped default does. Refusing to build."

# THE EQUIVALENCE ASSERTION. This is what lets a passing gate say anything about
# the shipped image: outside the two divergences named above, the seeds must be
# identical. If anything else drifts - a different user, a different
# late-command, a different payload path - the gate is testing a different
# install and its result is worthless while still looking green.
#
# The network: block is EXCISED from both sides rather than allow-listed line by
# line. A list of permitted line prefixes would also have permitted, say, a
# changed `dhcp4:` or a different `set-name:` - it grows loose exactly where it
# needs to be tight. Cutting the whole block out and diffing the remainder means
# the guard cannot be weakened by adding lines to it, and the block's own
# correctness is asserted separately above.
strip_network() {
    awk '
      /^  network:/            { in_net = 1; next }
      in_net && /^  [a-z_-]+:/ { in_net = 0 }
      in_net                   { next }
                               { print }
    ' "$1"
}
strip_network "$CONFIRM_UD" > "$WORK/a.stripped"
strip_network "$GATE_UD"    > "$WORK/b.stripped"
DRIFT="$(diff "$WORK/a.stripped" "$WORK/b.stripped" | grep -E '^[<>]' | grep -vE '^[<>][[:space:]]+interactive-sections:' || true)"
[ -z "$DRIFT" ] || die "the gate seed differs from the shipped seed beyond interactive-sections and network::
$DRIFT
The gate would not be testing the image you ship. Refusing to build."
log "gate seed: unattended, unpinned, and otherwise identical to the shipped seed"

# ── 3. one extra menuentry, LAST, and made the default ─────────────────────
# Last so the shipped entries keep their positions - the containment stage boots
# the SHIPPED ISO untouched and its entry 0 must stay the pinned install. This
# ISO's own default is then pointed at the gate entry by title (see below).
GATE_TITLE="GATE - unattended, unpinned (VM ONLY)"
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

menuentry "$GATE_TITLE" {
	set gfxpayload=keep
$GATE_LINUX
$DIAG_INITRD
}
set default="$GATE_TITLE"
EOF

# THE GATE ENTRY IS THE DEFAULT, AND NO KEY IS PRESSED (2026-08-06).
#
# It was selected by console keystrokes until this failed twice on real runs.
# Start-VirtualHomeHub.ps1 sent UP+ENTER on the reasoning that "UP from entry 0
# WRAPS to the last entry, which is count-independent" — a claim written in a
# comment, never verified, and exercised by nothing else in this repo (the A19
# gate drove no menu at all). GRUB2's wrap behaviour is not something to bet an
# install cycle on, and the failure it produces is expensive and mute: the
# selection stays on entry 0, GRUB boots the PINNED unattended install, that
# matches no disk in a VM and halts at "An error occurred. Press enter to start
# a shell", nothing is ever written, and the launcher waits its full install
# timeout for a box that was never going to appear. Measured twice: 0.0 GB.
#
# `set default=` BY TITLE removes every moving part at once — the wrap question,
# the entry count, and the race between a fixed sleep and however long this VM's
# firmware takes to draw a menu. It comes AFTER the shipped `set default=0`, and
# grub.cfg is sourced top to bottom, so the later assignment is the one that
# stands.
#
# WHAT THIS COSTS, PLAINLY: the one deliberate keypress that used to stand
# between this ISO and any disk it met is gone. This artifact already installs
# unattended to whatever disk it finds — that is what it is FOR, and the
# launcher already prints "NEVER write this to physical media" — so what is
# actually lost is the few seconds a person would have had to notice their
# mistake. The judgement is that a gate which silently tests nothing is the
# worse hazard of the two, and that the media rule is the control that matters.
# The timeout is left at the shipped value: with the default correct, it only
# governs how long a human gets to intervene.
grep -qF "set default=\"$GATE_TITLE\"" "$GATE_GRUB" \
    || die "the gate grub.cfg has no 'set default' naming the gate entry - it would boot the PINNED entry, halt in the VM, and write nothing. Refusing to build."
[ "$(grep -c '^set default=' "$GATE_GRUB")" -ge 2 ] \
    || die "expected the shipped 'set default=0' AND the gate override in the gate grub.cfg; found fewer. The shipped image's layout changed - re-read this block. Refusing to build."
log "gate entry is GRUB's default - the gate needs no console keypress at all"

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
