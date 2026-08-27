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
# SECRET_HANDOFF (WI-10.3, RATIFIED 2026-07-25; tooling not yet written) — see
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
# Q10.9 B+: where export-images.sh put the docker-save tars (its default).
IMAGES_OUT="${IMAGES_OUT:-$REPO_ROOT/vmtest/.out/images}"
# 2026-08-06: where export-apt.sh put the baked .debs (its default for hub).
APT_OUT="${APT_OUT:-$REPO_ROOT/vmtest/.out/apt}"
ICEDRIVE_OUT="${ICEDRIVE_OUT:-$REPO_ROOT/vmtest/.out/icedrive}"

for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=1 ;;
        -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
        *) die "unknown argument '$arg' (try --help)" ;;
    esac
done

require_iso_tool

# With the Q10.9 B+ ALL-IMAGES payload, the seed grows from ~1MB to ~1GB: size
# the check for the staged payload copy + the resulting seed ISO (+ margin).
# (Without an images payload the seed is still tiny; 4GB is just safe headroom.)
require_free_gb "$(dirname "$OUT_DIR")" 4
require_writable_output "$OUT_DIR/seed.iso"

render_seed_tree "$REPO_ROOT" "$OUT_DIR" "build-seed.sh"

# Q10.9 B+ ALL-IMAGES: fold the docker-save tars into deploy-payload/images/ so
# the CIDATA seed carries every container image (it just gets big — see README).
stage_images_into_payload "$OUT_DIR" "$IMAGES_OUT"

# IF-005: the wall kiosk site's document root. The hub is the ORIGIN the panel's
# renderer is served from (NagLight sends no CORS headers, so the renderer and
# the /api/* proxy must share an origin) — so the site half of
# OfficeWallNaglight's build belongs in THIS image, not the panel's. Absent is
# tolerated: a checkout without that private sibling still builds a hub.
stage_wall_site_into_payload "$OUT_DIR" "$REPO_ROOT"

# 2026-08-06: the baked apt repo. `packages:` is empty in the shipped user-data,
# so this is where openssh-server, cockpit, docker-ce and the rest come from —
# and a seed built without it produces the 2026-08-06 machine exactly. Unlike
# the images stager this REFUSES rather than warns; ALLOW_MISSING_APT=1 opts
# out, loudly, for exercising the seed machinery alone.
#   bash vmtest/export-apt.sh --target hub --out vmtest/.out/apt
stage_apt_into_payload "$OUT_DIR" "$APT_OUT" hub

# SR-015 is no longer opt-in (the Owner, 2026-08-26), so the app that layer
# exists for rides along too. Absent is tolerated and logged: the pin starts
# unset, and a hub with no AppImage still gets its graphical session.
#   bash vmtest/export-icedrive.sh --out vmtest/.out/icedrive --from <file>
stage_icedrive_into_payload "$OUT_DIR" "$ICEDRIVE_OUT"

# LAST, after every stager: the payload's permissions are decided here, not
# inherited from whatever filesystem this ran on. Booting the gate VM on
# 2026-08-04 found /opt/homehub 0777 with 230 world-writable paths under it,
# straight off a DrvFs staging tree that reports 0777 for everything. The
# assertion is the half that cannot be skipped — nothing else in this repo has
# ever looked at a mode.
normalize_payload_modes "$OUT_DIR/iso-root/deploy-payload"
assert_payload_modes "$OUT_DIR/iso-root/deploy-payload"

SEED_ISO="$OUT_DIR/seed.iso"
write_seed_iso "$OUT_DIR/iso-root" "$SEED_ISO"
# And judge the ARTIFACT. On the default build host (WSL, OUT_DIR on a Windows
# drive) the staged tree cannot carry modes at all, so the ISO is the only place
# the property is observable — and the ISO is what `cp -a` reads.
assert_iso_payload_modes "$SEED_ISO" /deploy-payload

log "OK — seed ISO ready: $SEED_ISO"
log "SSH:     ssh -i $SSH_KEY hub@<vm-ip>   (fingerprint: $(ssh-keygen -lf "$SSH_KEY.pub"))"
log "Console: user 'hub', SIM password in $CREDS_FILE"
log "Next: vmtest/README.md — attach $SEED_ISO as the VM's 2nd DVD via New-HomeHubVm.ps1"
