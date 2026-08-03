#!/usr/bin/env bash
# vmtest/build-wall-seed.sh — the WALL PANEL image's seed ISO (SR-017, E0).
#
# The second image target's equivalent of build-seed.sh. Builds a small NoCloud
# "CIDATA" seed ISO from the REAL stack/autoinstall/wall/ user-data + meta-data
# (SIM values substituted), plus a deploy-payload/ copy of the repo AND the
# OfficeWallNaglight shell artifact — which is the whole point: before this
# script existed the repo could describe a panel image but not produce one, and
# nothing took the artifact that repo emits. Run this in WSL (Ubuntu).
#
# WHY A SEPARATE SCRIPT AND NOT A FLAG ON build-seed.sh: the two targets share
# their MECHANISM (CIDATA discovery, the payload copy, the ephemeral key, the
# assert-every-substitution discipline) and share nothing else — different
# user-data, different disk pin, different config file, different consequences
# for getting it wrong. The mechanism lives once in lib/common.sh and both
# builders call it; a single script with a --wall flag would put two different
# sets of load-bearing assertions behind one code path.
#
# THE SAME CAVEAT AS THE HUB'S LIGHT PATH: a CIDATA seed is auto-detected, but
# the stock ISO's GRUB menu does not carry the `autoinstall` kernel argument, so
# Subiquity pauses ONCE for "Continue with autoinstall?" unless you type the
# single word `autoinstall` at the GRUB edit prompt. See vmtest/README.md §6.
#
# CONTAINMENT: the sim rewrites the panel's disk pin to `model: Virtual_Disk`,
# which only a Hyper-V synthetic disk reports. On real hardware it matches
# NOTHING and the unattended install halts — so this ISO, written to a USB stick
# and booted on the real panel, cannot wipe it. The build refuses to proceed if
# that substitution silently no-ops or if the panel's real disk model survives.
#
# SECRETS: every value materialized here is a throwaway SIM placeholder for a
# local VM. There is NO production path — this builder refuses WALL_SITE_DIR
# outright (see render_wall_seed_tree), because nothing materialises a real
# panel wall.env yet and half a production build is worse than none.
#
# Usage:
#   bash vmtest/build-wall-seed.sh
#   bash vmtest/build-wall-seed.sh --clean          # regen SSH key + SIM secrets
#   OUT_DIR=/mnt/d/vmtest-out bash vmtest/build-wall-seed.sh
#   WALL_SHELL_DIST=/path/to/OfficeWallNaglight/dist bash vmtest/build-wall-seed.sh
#   WALL_ENV_OVERRIDES='WALL_HOST=wall.home.arpa
#   WALL_PORT=8443' bash vmtest/build-wall-seed.sh   # §3 sets the real gate values here
#   ALLOW_MISSING_SHELL=1 bash vmtest/build-wall-seed.sh   # image layer only, loudly
#
# Output (all gitignored, see ../.gitignore):
#   $OUT_DIR/wall-seed.iso        the CIDATA seed ISO to attach as the VM's 2nd DVD
#   $OUT_DIR/ssh/                 ephemeral ed25519 keypair for SSH into the panel VM
#   $OUT_DIR/secrets/creds.env    the SIM console password (Ctrl+Alt+F2 on the VM)
#   $OUT_DIR/iso-root/            staging tree burned into the ISO (inspectable)
#
# NAMED wall-seed.iso ON PURPOSE. Both targets' seeds are labelled CIDATA —
# they have to be, that label is how cloud-init finds them — so the LABEL cannot
# tell them apart. Attach the hub's seed.iso to the panel VM and you will
# install a hub. The filename is the only thing standing between you and that.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

REPO_ROOT="$(repo_root)"
# Under vmtest/.out/ so the hub builder's --exclude=vmtest/.out keeps this
# build's output (including a 111 MB tarball) out of the HUB's payload — and
# vice versa. Separate subdirectories because each holds its own iso-root/.
OUT_DIR="${OUT_DIR:-$REPO_ROOT/vmtest/.out/wall}"

for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=1 ;;
        -h|--help) sed -n '2,50p' "$0"; exit 0 ;;
        *) die "unknown argument '$arg' (try --help)" ;;
    esac
done

require_iso_tool

# The payload is the repo (~10 MB) + the shell tarball (~111 MB), and the ISO is
# another copy of both. 3 GB is headroom, not a measurement.
require_free_gb "$(dirname "$OUT_DIR")" 3
require_writable_output "$OUT_DIR/wall-seed.iso"

render_wall_seed_tree "$REPO_ROOT" "$OUT_DIR" "build-wall-seed.sh"

# IF-005: the artifact this whole image exists to run. FAILS the build when it
# is absent — an image without it boots to the NOT INSTALLED screen, which is
# right on real hardware and useless as a gate.
stage_wall_shell_into_payload "$OUT_DIR" "$REPO_ROOT"

WALL_SEED_ISO="$OUT_DIR/wall-seed.iso"
write_seed_iso "$OUT_DIR/iso-root" "$WALL_SEED_ISO"

log "OK — wall seed ISO ready: $WALL_SEED_ISO ($(( $(stat -c%s "$WALL_SEED_ISO") / 1024 / 1024 )) MB)"
log "SSH:     ssh -i $SSH_KEY panel@<vm-ip>   (fingerprint: $(ssh-keygen -lf "$SSH_KEY.pub"))"
log "Console: tty1 runs the KIOSK. For a shell use Ctrl+Alt+F2, user 'panel',"
log "         SIM password in $CREDS_FILE"
log "Next: vmtest/README.md §11 — New-HomeHubVm.ps1 with -VMName Wall-VMTest and"
log "      -SeedIsoPath $WALL_SEED_ISO"
