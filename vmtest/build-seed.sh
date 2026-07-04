#!/usr/bin/env bash
# vmtest/build-seed.sh — WI-10.18 V3 gate, LIGHT path (default, recommended).
#
# Builds a small NoCloud "CIDATA" seed ISO from the REAL stack/autoinstall/
# user-data + meta-data (SIM values substituted, see below) plus a
# deploy-payload/ copy of the repo, WITHOUT touching the 2.5-3GB stock Ubuntu
# Server ISO at all. Run this in WSL (Ubuntu).
#
# WHY THIS WORKS (no repack needed): Ubuntu's Subiquity installer uses
# cloud-init's NoCloud datasource, which auto-detects ANY attached CD-ROM/USB
# filesystem whose volume label is "CIDATA" (case-insensitive) and reads
# user-data/meta-data from its root. This is the exact same mechanism
# stack/README.md's "Second USB (simplest)" already documents for real
# hardware — here we just burn it to an ISO so Hyper-V can attach it as a
# second virtual DVD drive instead of a second USB stick. No `ds=nocloud=...`
# kernel argument is required for the *datasource* to be found.
#
# THE ONE CAVEAT (read this — it is the honest bit): auto-detecting the seed
# is not the same as fully hands-off. The stock ISO's GRUB menu does NOT carry
# the "autoinstall" kernel argument by default. Even with a valid CIDATA seed
# attached, Subiquity will still pause ONCE for a confirmation prompt
# ("Continue with autoinstall?", press 'C' or Enter — it does NOT ask you to
# type any values, just to confirm) unless "autoinstall" is present on the
# kernel command line. Getting that one keypress out of the way requires
# EITHER a one-time manual GRUB edit at the VM console (documented step-by-
# step in vmtest/README.md) OR the heavier vmtest/build-repacked-iso.sh, which
# bakes the kernel args into a modified copy of the ISO so NOTHING needs to
# be typed at boot. Prefer this script; fall back to the repack only if you
# need zero-keypress automation (e.g. scripted/repeated VM runs).
#
# SECRETS: every value materialized into the SIM .env / user-data is a
# throwaway placeholder generated fresh for this local, NAT-isolated VM. NONE
# of it is a real production secret and NONE of it should be reused anywhere
# else. Real materialization for the physical AWOW happens later via
# SECRET_HANDOFF_PROPOSAL, once Peter ratifies it (WI-10.3, still open) — see
# stack/README.md and docs/status.md OI list.
#
# Usage:
#   bash vmtest/build-seed.sh
#   OUT_DIR=/mnt/d/vmtest-out bash vmtest/build-seed.sh   # if C: is tight on space
#   CLEAN=1 bash vmtest/build-seed.sh                     # regen SSH key + all SIM secrets
#
# Output (all gitignored, see ../.gitignore):
#   $OUT_DIR/seed.iso        the CIDATA seed ISO to attach as the VM's 2nd DVD
#   $OUT_DIR/ssh/            ephemeral ed25519 keypair for console/SSH into the VM
#   $OUT_DIR/secrets/creds.env   SIM console password + Technitium/oauth2 secrets
#   $OUT_DIR/iso-root/       staging tree burned into seed.iso (inspectable)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

REPO_ROOT="$(repo_root)"
OUT_DIR="${OUT_DIR:-$REPO_ROOT/vmtest/.out}"

for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=1 ;;
        -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
        *) die "unknown argument '$arg' (try --help)" ;;
    esac
done

ISO_TOOL=""
if command -v genisoimage >/dev/null 2>&1; then
    ISO_TOOL="genisoimage"
elif command -v xorriso >/dev/null 2>&1; then
    ISO_TOOL="xorriso"
else
    die "need genisoimage or xorriso. Install with: sudo apt-get install -y genisoimage xorriso"
fi

require_free_gb "$(dirname "$OUT_DIR")" 1   # seed ISO + payload is small (<50MB); 1GB margin is generous

render_seed_tree "$REPO_ROOT" "$OUT_DIR" "build-seed.sh"

SEED_ISO="$OUT_DIR/seed.iso"
log "building $SEED_ISO with $ISO_TOOL (volume label CIDATA)"
case "$ISO_TOOL" in
    genisoimage)
        genisoimage -output "$SEED_ISO" -volid CIDATA -joliet -rock "$OUT_DIR/iso-root" >/dev/null
        ;;
    xorriso)
        xorriso -as genisoimage -output "$SEED_ISO" -volid CIDATA -joliet -rock "$OUT_DIR/iso-root" >/dev/null
        ;;
esac

log "OK — seed ISO ready: $SEED_ISO"
log "SSH:     ssh -i $SSH_KEY operator@<vm-ip>   (fingerprint: $(ssh-keygen -lf "$SSH_KEY.pub"))"
log "Console: user 'operator', SIM password in $CREDS_FILE"
log "Next: vmtest/README.md — attach $SEED_ISO as the VM's 2nd DVD via New-AwowVm.ps1"
