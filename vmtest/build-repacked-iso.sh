#!/usr/bin/env bash
# vmtest/build-repacked-iso.sh — WI-10.18 V3 gate, HEAVIER path (fallback).
#
# Prefer vmtest/build-seed.sh instead (see its header comment). Use THIS
# script only if you need truly zero-keypress automation from power-on — e.g.
# scripted/repeated VM runs where nobody is at the console to do the one-time
# GRUB edit the light path needs.
#
# This produces a SINGLE self-contained ISO: it takes the stock Ubuntu Server
# ISO and, WITHOUT fully unpacking/rebuilding it, replaces just
# /boot/grub/grub.cfg (adding the "autoinstall ds=nocloud;s=/cdrom/nocloud/"
# kernel args to the default boot entries) and adds two new top-level
# directories — /nocloud/ (user-data + meta-data, SIM values, same as
# build-seed.sh) and /deploy-payload/ (the repo copy) — using xorriso's
# "-boot_image any replay" trick, which reuses the ORIGINAL ISO's El Torito
# boot catalog + hybrid MBR/GPT (BIOS+UEFI) instead of hand-building a new one.
# Verified structurally on a real ubuntu-24.04.4-live-server-amd64.iso: the
# repacked ISO still reports BOTH a BIOS and a UEFI El Torito boot image
# (`xorriso -report_el_torito plain`), and /boot/grub/grub.cfg, /nocloud/,
# /deploy-payload/ are all present and correct in the output. NOT boot-tested
# (that needs a VM — Peter's step, see vmtest/README.md).
#
# Since everything (OS + seed + payload) is in ONE ISO here, attach only this
# ISO to the VM (New-AwowVm.ps1 -SkipSecondDvd, same path for both
# -UbuntuIsoPath and -SeedIsoPath).
#
# Run in WSL (Ubuntu). Requires: xorriso, openssl, ssh-keygen.
#
# SECRETS: same policy as build-seed.sh — everything materialized is a
# throwaway SIM placeholder for this local VM, never a real secret. Real
# materialization happens later via SECRET_HANDOFF_PROPOSAL (WI-10.3, still
# open).
#
# Usage:
#   bash vmtest/build-repacked-iso.sh --src-iso /path/to/ubuntu-24.04.x-live-server-amd64.iso
#   bash vmtest/build-repacked-iso.sh --src-iso ... --expected-sha256 <hash from releases.ubuntu.com/24.04/SHA256SUMS>
#   OUT_DIR=/mnt/d/vmtest-out bash vmtest/build-repacked-iso.sh --src-iso ...
#
# Output (gitignored, see ../.gitignore):
#   $OUT_DIR/repacked.iso    the single ISO to attach to the VM

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

REPO_ROOT="$(repo_root)"
OUT_DIR="${OUT_DIR:-$REPO_ROOT/vmtest/.out}"
# Q10.9 B+: where export-images.sh put the docker-save tars (its default).
IMAGES_OUT="${IMAGES_OUT:-$REPO_ROOT/vmtest/.out/images}"
SRC_ISO=""
EXPECTED_SHA256=""

while [ $# -gt 0 ]; do
    case "$1" in
        --src-iso) SRC_ISO="$2"; shift 2 ;;
        --expected-sha256) EXPECTED_SHA256="$2"; shift 2 ;;
        --clean) CLEAN=1; shift ;;
        -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
        *) die "unknown argument '$1' (try --help)" ;;
    esac
done

[ -n "$SRC_ISO" ] || die "need --src-iso /path/to/ubuntu-24.04.x-live-server-amd64.iso (see vmtest/README.md for the download URL + SHA256)"
[ -f "$SRC_ISO" ] || die "not found: $SRC_ISO"

require_cmd xorriso "Install with: sudo apt-get install -y xorriso"

require_free_gb "$(dirname "$OUT_DIR")" 10  # source (~3.5GB) + output (~3.5GB + ~1GB Q10.9 B+ images) + margin

log "computing SHA256 of $SRC_ISO (this reads the whole ~3GB file, takes a bit)"
ACTUAL_SHA256="$(sha256sum "$SRC_ISO" | awk '{print $1}')"
if [ -n "$EXPECTED_SHA256" ]; then
    if [ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]; then
        die "SHA256 mismatch! expected $EXPECTED_SHA256, got $ACTUAL_SHA256 - re-download, don't boot an unverified ISO"
    fi
    log "SHA256 verified OK: $ACTUAL_SHA256"
else
    log "SHA256 (not verified - no --expected-sha256 given): $ACTUAL_SHA256"
    log "Cross-check this by hand against https://releases.ubuntu.com/24.04/SHA256SUMS (see vmtest/README.md)"
fi

mkdir -p "$OUT_DIR"

# ── 1. render the SIM user-data/meta-data/.env + deploy-payload (shared with
#      build-seed.sh) ────────────────────────────────────────────────────────
render_seed_tree "$REPO_ROOT" "$OUT_DIR" "build-repacked-iso.sh"

# Q10.9 B+ ALL-IMAGES: fold the docker-save tars into deploy-payload/images/.
# The `-map "$NOCLOUD_DIR/deploy-payload" /deploy-payload` below carries the whole
# deploy-payload dir (images and all) into the ISO's /deploy-payload/ area.
stage_images_into_payload "$OUT_DIR" "$IMAGES_OUT"

# ── 2. stage a /nocloud directory (xorriso -map wants one disk dir per iso
#      dir; iso-root/ from render_seed_tree already has user-data+meta-data
#      side by side, so just point -map at it directly under /nocloud) ──────
NOCLOUD_DIR="$OUT_DIR/iso-root"   # contains user-data + meta-data (+ deploy-payload/, mapped separately below)

# ── 3. extract the original grub.cfg, inject the autoinstall kernel args ────
GRUB_ORIG="$OUT_DIR/grub-orig.cfg"
GRUB_MOD="$OUT_DIR/grub-mod.cfg"
rm -f "$GRUB_ORIG"
log "extracting /boot/grub/grub.cfg from $SRC_ISO"
xorriso -osirrox on -indev "$SRC_ISO" -extract /boot/grub/grub.cfg "$GRUB_ORIG" >/dev/null 2>&1 \
    || die "couldn't extract /boot/grub/grub.cfg - is this a standard Ubuntu Server live ISO?"

# Inject "autoinstall ds=nocloud;s=/cdrom/nocloud/" onto every /casper/*vmlinuz
# boot line (the default "Try or Install Ubuntu Server" entry and the HWE
# kernel variant), and shorten the menu timeout. Idempotent: if the args are
# already present (re-run on an already-modified file), sed just no-ops.
sed \
    -e "s#\(linux[[:space:]]*/casper/[a-z-]*vmlinuz\)\( \)\+---#\1 autoinstall ds=nocloud;s=/cdrom/nocloud/ ---#" \
    -e "s/^set timeout=.*/set timeout=5/" \
    "$GRUB_ORIG" > "$GRUB_MOD"

grep -q "autoinstall ds=nocloud" "$GRUB_MOD" || die "grub.cfg injection failed - Ubuntu changed its grub.cfg layout, update the sed pattern above"
log "grub.cfg patched: $(grep -c 'autoinstall ds=nocloud' "$GRUB_MOD") boot entr(y/ies) now carry autoinstall ds=nocloud"

# ── 4. repack: reuse the ORIGINAL El Torito boot catalog + hybrid MBR/GPT via
#      "-boot_image any replay" instead of hand-building a new one — this is
#      what keeps BOTH BIOS and UEFI boot working without re-deriving Ubuntu's
#      boot images ourselves. ─────────────────────────────────────────────────
REPACKED_ISO="$OUT_DIR/repacked.iso"
rm -f "$REPACKED_ISO"
log "repacking -> $REPACKED_ISO (a few minutes; copies ~3GB)"
xorriso -indev "$SRC_ISO" -outdev "$REPACKED_ISO" \
    -map "$GRUB_MOD" /boot/grub/grub.cfg \
    -map "$NOCLOUD_DIR/user-data" /nocloud/user-data \
    -map "$NOCLOUD_DIR/meta-data" /nocloud/meta-data \
    -map "$NOCLOUD_DIR/deploy-payload" /deploy-payload \
    -boot_image any replay \
    >/dev/null

# ── 5. sanity: re-open the repacked ISO and confirm the boot catalog still
#      has both a BIOS and a UEFI image, and our files landed. ──────────────
log "verifying repacked ISO structure"
EL_TORITO_REPORT="$(xorriso -indev "$REPACKED_ISO" -report_el_torito plain 2>/dev/null)"
echo "$EL_TORITO_REPORT" | grep -q "BIOS" || die "repacked ISO lost its BIOS boot image - do not use this ISO"
echo "$EL_TORITO_REPORT" | grep -q "UEFI" || die "repacked ISO lost its UEFI boot image - do not use this ISO"
xorriso -indev "$REPACKED_ISO" -find /nocloud >/dev/null 2>&1 \
    || die "repacked ISO is missing /nocloud - do not use this ISO"
xorriso -indev "$REPACKED_ISO" -find /deploy-payload >/dev/null 2>&1 \
    || die "repacked ISO is missing /deploy-payload - do not use this ISO"

log "OK — repacked ISO ready: $REPACKED_ISO (BIOS + UEFI boot images intact, /nocloud + /deploy-payload present)"
log "SSH:     ssh -i $SSH_KEY operator@<vm-ip>"
log "Console: user 'operator', SIM password in $CREDS_FILE"
log "Next: vmtest/README.md — New-AwowVm.ps1 -UbuntuIsoPath $REPACKED_ISO -SeedIsoPath $REPACKED_ISO -SkipSecondDvd"
log "NOT boot-tested here (needs a VM) — this only verifies the ISO's on-disk structure."
