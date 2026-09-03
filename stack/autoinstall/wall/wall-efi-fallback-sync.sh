#!/bin/bash
# wall-efi-fallback-sync — keep the UEFI removable-media boot path self-sufficient.
#
# WHY THIS RUNS FOREVER AND NOT ONCE (2026-09-03, adversarial review finding).
# The panel's Insyde firmware discards the NVRAM boot entry grub-install
# creates, so the machine boots by the spec's removable-media path,
# \EFI\Boot\BOOTX64.EFI. shim loads its second stage FROM ITS OWN DIRECTORY, and
# grub-install never puts a grubx64.efi there — measured on the hub, whose
# \EFI\BOOT holds BOOTX64.EFI, fbx64.efi and mmx64.efi and no GRUB at all. The
# install-time repair copies one in. Left at that, two things rot:
#
#   1. grub-efi-amd64-signed's postinst re-runs `grub-install --auto-nvram` on
#      EVERY upgrade, and this box has `grub2/no_efi_extra_removable: false`, so
#      it refreshes BOOTX64.EFI (a NEW shim) and restores fbx64.efi. The
#      install-time grubx64.efi beside them is never touched again — so the
#      panel's actual boot path freezes at its install-day GRUB and no security
#      update ever reaches it.
#   2. The day a new shim refuses that stale GRUB (an SBAT revocation, a
#      shim/grub compat bump), the restored fbx64.efi does what it is built to
#      do: recreate the NVRAM entry and RESET. Against firmware that discards
#      the entry every boot that is a ~10 second loop appending one Boot####
#      per cycle. This panel reached 131 before anyone looked.
#
# So the repair is re-asserted rather than performed once: from the apt hook
# immediately after any upgrade that could have undone it, and from
# wall-efi-fallback.service at every boot as the backstop that also catches a
# hand-run grub-install. Copying rather than symlinking is deliberate — the
# firmware reads this before any filesystem driver of ours is involved, and a
# FAT32 ESP has no symlinks anyway.
#
# NOT A SECURE BOOT CHANGE. The GRUB copied here is Ubuntu's own signed binary,
# unmodified; mmx64.efi stays in both directories so MokManager and enrollment
# are untouched. Disabling fbx64 removes a recovery path that on THIS firmware
# only ever recovers into a loop; without it a dropped entry degrades to a clean
# "Default Boot Device Missing", and the install stick still boots.
set -uo pipefail

ESP="${WALL_ESP:-/boot/efi}"
TAG=wall-efi-fallback
log()  { printf '%s\n' "$*"; command -v logger >/dev/null 2>&1 && logger -t "$TAG" -- "$*"; }
warn() { printf '%s\n' "$*" >&2; command -v logger >/dev/null 2>&1 && logger -t "$TAG" -p user.warning -- "$*"; }

rc=0

if ! mountpoint -q "$ESP" 2>/dev/null; then
    warn "$ESP is not a mountpoint — nothing done. The ESP must be mounted for this to mean anything."
    exit 1
fi

SRC="$ESP/EFI/ubuntu/grubx64.efi"
DSTDIR="$ESP/EFI/BOOT"
DST="$DSTDIR/grubx64.efi"

if [ ! -f "$SRC" ]; then
    warn "no $SRC — GRUB is not installed where grub-install puts it. Nothing to mirror."
    exit 1
fi
if [ ! -d "$DSTDIR" ]; then
    warn "no $DSTDIR — the removable-media path does not exist on this ESP. Nothing to do."
    exit 1
fi

# THE COPY IS CONDITIONAL ON CONTENT, not on existence: that is what keeps the
# fallback GRUB current across upgrades instead of frozen, and it is why this
# is worth running at every boot rather than only when the file is missing.
if [ ! -f "$DST" ] || ! cmp -s "$SRC" "$DST"; then
    if cp -f "$SRC" "$DST"; then
        log "refreshed $DST from $SRC (the fallback path now carries the current GRUB)"
    else
        warn "could NOT refresh $DST from $SRC — the fallback path may be carrying a stale GRUB."
        rc=1
    fi
fi

# fbx64 is the reset-loop trigger. It comes back with every shim upgrade, so it
# is renamed rather than deleted: reversible, and obvious to whoever finds it.
if [ -f "$DSTDIR/fbx64.efi" ]; then
    if mv -f "$DSTDIR/fbx64.efi" "$DSTDIR/fbx64.efi.disabled"; then
        log "disabled $DSTDIR/fbx64.efi (restored by a package upgrade; it is the reset-loop trigger)"
    else
        warn "could NOT disable $DSTDIR/fbx64.efi — a dropped NVRAM entry could restart the reset loop."
        rc=1
    fi
fi

sync
exit "$rc"
