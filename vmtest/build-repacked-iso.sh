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
# kernel args to the default boot entries, plus a last-position "Diagnostic
# Shell (no autoinstall)" entry that deliberately carries neither) and adds two
# new top-level
# directories — /nocloud/ (user-data + meta-data, SIM values, same as
# build-seed.sh) and /deploy-payload/ (the repo copy) — using xorriso's
# "-boot_image any replay" trick, which reuses the ORIGINAL ISO's El Torito
# boot catalog + hybrid MBR/GPT (BIOS+UEFI) instead of hand-building a new one.
# Verified structurally on a real ubuntu-24.04.4-live-server-amd64.iso: the
# repacked ISO still reports BOTH a BIOS and a UEFI El Torito boot image
# (`xorriso -report_el_torito plain`), and /boot/grub/grub.cfg, /nocloud/,
# /deploy-payload/ are all present and correct in the output. NOT boot-tested
# (that needs a VM — the Owner's step, see vmtest/README.md).
#
# Since everything (OS + seed + payload) is in ONE ISO here, attach only this
# ISO to the VM (New-HomeHubVm.ps1 -SkipSecondDvd, same path for both
# -UbuntuIsoPath and -SeedIsoPath).
#
# Run in WSL (Ubuntu). Requires: xorriso, openssl, ssh-keygen.
#
# SECRETS: same policy as build-seed.sh — everything materialized is a
# throwaway SIM placeholder for this local VM, never a real secret. Real
# materialization happens later via SECRET_HANDOFF (WI-10.3, RATIFIED
# open).
#
# Usage:
#   bash vmtest/build-repacked-iso.sh --src-iso /path/to/ubuntu-24.04.x-live-server-amd64.iso
#   bash vmtest/build-repacked-iso.sh --target wall --src-iso ...   # the WALL panel image
#   bash vmtest/build-repacked-iso.sh --src-iso ... --expected-sha256 <hash from releases.ubuntu.com/24.04/SHA256SUMS>
#   bash vmtest/build-repacked-iso.sh --src-iso ... --volid HOMEHUB   # ISO volume label
#   OUT_DIR=/mnt/d/vmtest-out bash vmtest/build-repacked-iso.sh --src-iso ...
#
# Output (gitignored, see ../.gitignore):
#   $OUT_DIR/repacked.iso        (hub)  the single ISO to attach to the VM
#   $OUT_DIR/wall-repacked.iso   (wall, --target wall)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

REPO_ROOT="$(repo_root)"
SRC_ISO=""
EXPECTED_SHA256=""
# WHICH IMAGE. Both targets need the identical El Torito / hybrid-MBR replay
# below — re-solving that is how you get a subtly unbootable ISO — so the target
# only chooses which user-data is rendered and which payload is staged.
#
# For the WALL this path is not merely the "heavier fallback" it is for the hub:
# on the light path /cdrom is the STOCK Ubuntu ISO, so `/cdrom/deploy-payload`
# does not exist and the payload has to be recovered by mounting the CIDATA
# volume (see the wall user-data's late-command 3). Here it is simply present,
# which is the branch that has actually been exercised.
TARGET="hub"

while [ $# -gt 0 ]; do
    case "$1" in
        --src-iso) SRC_ISO="$2"; shift 2 ;;
        --expected-sha256) EXPECTED_SHA256="$2"; shift 2 ;;
        --target) TARGET="$2"; shift 2 ;;
        --volid) VOLID="$2"; shift 2 ;;
        --clean) CLEAN=1; shift ;;
        -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
        *) die "unknown argument '$1' (try --help)" ;;
    esac
done
case "$TARGET" in
    hub)  DEFAULT_OUT="$REPO_ROOT/vmtest/.out";      ISO_NAME="repacked.iso";      DEFAULT_VOLID="HOMEHUB" ;;
    wall) DEFAULT_OUT="$REPO_ROOT/vmtest/.out-wall"; ISO_NAME="wall-repacked.iso"; DEFAULT_VOLID="WALLPANEL" ;;
    *)    die "--target must be 'hub' or 'wall' (got '$TARGET')" ;;
esac

# ── the ISO's own volume label (2026-08-06) ─────────────────────────────────
# WHY THE IMAGE CARRIES IT rather than the writer applying it afterwards. A
# raw-written hybrid ISO exposes exactly two partitions: a 3.9 GB ISO9660 one,
# which is the volume Windows letters and shows in Explorer but CANNOT be
# relabelled because ISO9660 is read-only, and a 5 MB EFI System Partition,
# which is writable but which Windows hides by design. So the post-write
# relabel that HomeHub's Build-BootableUsb does had, at best, the ESP to write
# to — a partition the operator never sees — and at worst nothing at all.
# Result: a stick that boots fine and looks unlabelled, on a flow where the
# label IS the consent marker the next run resolves the target by.
#
# -volid puts it in the Primary Volume Descriptor instead, so it survives the
# raw write with no post-hoc step and appears on the big partition. Match it to
# the label the writer looks for; HomeHub passes --volid from its ImageSpec so
# that stays one source of truth, and these defaults only apply to a standalone
# run of this script.
#
# UPPERCASED, NOT REWRITTEN. Strict ISO9660 volume ids are A-Z 0-9 _ and at
# most 32 characters. Uppercasing is safe because the consumer compares with
# PowerShell -eq, which is case-insensitive; anything else would be this script
# quietly deciding the label does not have to match what the writer searches
# for, which is the whole failure being fixed. So other characters are refused.
VOLID="${VOLID:-$DEFAULT_VOLID}"
VOLID="$(printf '%s' "$VOLID" | tr '[:lower:]' '[:upper:]')"
case "$VOLID" in
    *[!A-Z0-9_]*) die "--volid '$VOLID' has characters outside A-Z 0-9 _ , which strict ISO9660 does not allow. Rewriting it here would produce a stick whose label no longer matches the one the writer looks for. Choose a label in that set." ;;
esac
[ "${#VOLID}" -le 32 ] || die "--volid '$VOLID' is ${#VOLID} characters; ISO9660 allows at most 32"
[ -n "$VOLID" ]        || die "--volid is empty"
OUT_DIR="${OUT_DIR:-$DEFAULT_OUT}"
# Q10.9 B+: where export-images.sh put the docker-save tars (its default).
IMAGES_OUT="${IMAGES_OUT:-$REPO_ROOT/vmtest/.out/images}"

[ -n "$SRC_ISO" ] || die "need --src-iso /path/to/ubuntu-24.04.x-live-server-amd64.iso (see vmtest/README.md for the download URL + SHA256)"
[ -f "$SRC_ISO" ] || die "not found: $SRC_ISO"

require_cmd xorriso "Install with: sudo apt-get install -y xorriso"

require_free_gb "$(dirname "$OUT_DIR")" 10  # source (~3.5GB) + output (~3.5GB + ~1GB Q10.9 B+ images) + margin
require_writable_output "$OUT_DIR/$ISO_NAME"

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
if [ "$TARGET" = "wall" ]; then
    render_wall_seed_tree "$REPO_ROOT" "$OUT_DIR" "build-repacked-iso.sh"
    # IF-005: the panel's own half of the shell build.
    stage_wall_shell_into_payload "$OUT_DIR" "$REPO_ROOT"
else
    render_seed_tree "$REPO_ROOT" "$OUT_DIR" "build-repacked-iso.sh"
    # Q10.9 B+ ALL-IMAGES: fold the docker-save tars into deploy-payload/images/.
    # The `-map "$NOCLOUD_DIR/deploy-payload" /deploy-payload` below carries the
    # whole deploy-payload dir (images and all) into the ISO's /deploy-payload/.
    stage_images_into_payload "$OUT_DIR" "$IMAGES_OUT"
    # IF-005: the wall kiosk site's document root rides in the same payload dir.
    stage_wall_site_into_payload "$OUT_DIR" "$REPO_ROOT"
fi

# ── 1b. decide the payload's permissions (2026-08-04) ───────────────────────
# Both targets, after every stager. See vmtest/lib/common.sh "PAYLOAD MODES":
# booting the hub gate VM found /opt/homehub and everything under it 0777,
# because DrvFs reports 0777, Rock Ridge records that faithfully and `cp -a`
# copies it faithfully. The `-chmod_r`/`-chown_r` below is this path's half of
# the same fix — the seed path gets it via write_seed_iso's -r branch.
normalize_payload_modes "$OUT_DIR/iso-root/deploy-payload"
assert_payload_modes "$OUT_DIR/iso-root/deploy-payload"

# ── 2. stage a /nocloud directory (xorriso -map wants one disk dir per iso
#      dir; iso-root/ from render_seed_tree already has user-data+meta-data
#      side by side, so just point -map at it directly under /nocloud) ──────
NOCLOUD_DIR="$OUT_DIR/iso-root"   # contains user-data + meta-data (+ deploy-payload/, mapped separately below)

# ── 2b. a SECOND seed that stops and asks (2026-08-06) ──────────────────────
# The pinned seed is deliberately unforgiving: a `storage: match:` that matches
# nothing HALTS, and does not fall back to a heuristic. That is right as the
# default — it is what stops an unattended wipe of the wrong machine — but it
# also meant an operator standing at the keyboard, looking at the correct box,
# could not overrule it without baking a new ISO. Rebuilding a ~3.4GB image to
# answer a question a human is already present to answer is the wrong shape.
#
# So: same payload, same credentials, one extra seed whose storage section is
# INTERACTIVE. The GRUB entry that boots it (added below) is the confirmation —
# a menu choice inside a 5-second window is not something that happens
# unattended — and Subiquity then shows its own storage screen, which already
# lists every disk with model, serial and size and makes you select one, then
# confirms the destructive action. Hand-rolling a `type CONFIRM` prompt would
# be a worse copy of a screen the installer already draws.
#
# WHAT THIS COSTS, said plainly: the stick can now install anywhere, given one
# deliberate keypress. The pin stops being a lock and becomes a speed bump. The
# judgement (2026-08-06) is that this is an acceptable trade because the pin was
# never protecting the SECRETS — anyone who can mount the ISO reads them
# already — it protects against destroying the wrong machine's disk, and an
# interactive storage screen addresses that directly.
#
# The match: block is DROPPED rather than kept-as-a-default on purpose: left in
# place it would still be applied and could still halt, which would defeat the
# entire point of this entry on the machine that needed it.
CONFIRM_DIR="$OUT_DIR/iso-root-confirm"
rm -rf "$CONFIRM_DIR"
mkdir -p "$CONFIRM_DIR"
cp "$NOCLOUD_DIR/meta-data" "$CONFIRM_DIR/meta-data"

# Scoped to the storage: block by STATE, not by indentation. `match:` also
# appears under network: (at a different indent), and an indentation-only rule
# is one reformat away from silently unpinning — or worse, silently editing —
# the wrong section.
awk '
  /^  storage:/                 { in_storage = 1; print; next }
  in_storage && /^  [a-z_-]+:/  { in_storage = 0 }
  in_storage && dropping        { if ($0 ~ /^        /) next; dropping = 0 }
  in_storage && /^      match:[[:space:]]*$/ { dropping = 1; next }
  /^  interactive-sections:/    { print "  interactive-sections: [storage]"; next }
                                { print }
' "$NOCLOUD_DIR/user-data" > "$CONFIRM_DIR/user-data"

# Three assertions, because every way this can go wrong is silent and lands on
# a machine holding someone's data.
grep -q '^  interactive-sections: \[storage\]' "$CONFIRM_DIR/user-data" \
    || die "the confirm seed is not interactive - it would wipe a disk of Subiquity's choosing with no prompt at all. Refusing to build."
awk '/^  storage:/{s=1} s && /^  [a-z_-]+:/ && !/^  storage:/{s=0} s && /^      match:/{found=1} END{exit !found}' "$CONFIRM_DIR/user-data" \
    && die "the confirm seed still carries a storage match: - it can still halt on the very machine this entry exists to rescue. Refusing to build."
cmp -s "$NOCLOUD_DIR/user-data" "$CONFIRM_DIR/user-data" \
    && die "the confirm seed is byte-identical to the pinned seed - the awk transform matched nothing (did the template's indentation change?). Refusing to build."

# And the drift guard: the two seeds must differ ONLY in the storage selector
# and the interactive-sections line. Anything else means the second seed has
# quietly become a second SOURCE OF TRUTH — a different user, a different
# late-command, a different payload path — which is exactly how two-file
# configs rot. Generated from the first, allowed to differ in named ways only.
SEED_DRIFT="$(diff "$NOCLOUD_DIR/user-data" "$CONFIRM_DIR/user-data" \
    | grep -E '^[<>]' \
    | grep -vE '^[<>][[:space:]]+(interactive-sections|match|serial|path|model|wwn|size|ssd):' || true)"
[ -z "$SEED_DRIFT" ] \
    || die "confirm seed differs from the pinned seed beyond the storage selector:
$SEED_DRIFT
Both seeds must stay the same install. Refusing to build."
log "confirm seed staged: same payload, storage section INTERACTIVE, no disk pin"

# ── 3. extract the original grub.cfg, inject the autoinstall kernel args ────
GRUB_ORIG="$OUT_DIR/grub-orig.cfg"
GRUB_MOD="$OUT_DIR/grub-mod.cfg"
rm -f "$GRUB_ORIG"
log "extracting /boot/grub/grub.cfg from $SRC_ISO"
xorriso -osirrox on -indev "$SRC_ISO" -extract /boot/grub/grub.cfg "$GRUB_ORIG" >/dev/null 2>&1 \
    || die "couldn't extract /boot/grub/grub.cfg - is this a standard Ubuntu Server live ISO?"

# Inject autoinstall + the NoCloud seed location onto every /casper/*vmlinuz
# boot line (the default "Try or Install Ubuntu Server" entry and the HWE
# kernel variant), and shorten the menu timeout. Idempotent: if the args are
# already present (re-run on an already-modified file), sed just no-ops.
#
# TIMEOUT 10, not 5 (2026-08-06). Ubuntu's own default is 30; 5 was chosen when
# the menu held nothing worth choosing. It now carries the diagnostic shell and
# the unpinned installer, which are reached under pressure — a halted machine,
# an operator who has just watched a reboot — and 5 seconds is not enough to
# read three entries and decide. It is also the window the lab gate
# types into, and a gate that races a 5-second timer is a flaky gate. The cost
# is 5 seconds per unattended boot, which nobody is watching anyway.
#
# THE QUOTES AROUND ds=... ARE LOAD-BEARING. GRUB's config language uses `;`
# as a COMMAND SEPARATOR, exactly like a shell. Unquoted, GRUB splits
#     linux /casper/vmlinuz autoinstall ds=nocloud;s=/cdrom/nocloud/ ---
# into two commands: a `linux` that silently loses the seed location, and a
# bogus `s=/cdrom/nocloud/` that errors with "can't find command". The kernel
# then boots with `autoinstall` but no seedfrom, cloud-init finds no CIDATA
# volume (this ISO is labelled "Ubuntu-Server ...", and the repacked path
# attaches no second DVD), and Subiquity drops to the INTERACTIVE installer —
# a language-selection menu instead of an unattended install. Found 2026-07-30
# on the first real boot of this ISO; the old structural check passed happily
# because it only grepped that the string was PRESENT, never that GRUB could
# parse it. Quoting makes GRUB pass the whole thing as one kernel argument.
sed \
    -e "s#\(linux[[:space:]]*/casper/[a-z-]*vmlinuz\)\( \)\+---#\1 autoinstall \"ds=nocloud;s=/cdrom/nocloud/\" ---#" \
    -e "s/^set timeout=.*/set timeout=10/" \
    "$GRUB_ORIG" > "$GRUB_MOD"

# Verify the PARSEABLE form, not just the presence of the substring: the seed
# argument must be quoted, or GRUB will split it on the semicolon.
grep -q 'autoinstall "ds=nocloud;s=/cdrom/nocloud/"' "$GRUB_MOD" \
    || die "grub.cfg injection failed - Ubuntu changed its grub.cfg layout, update the sed pattern above"
grep -qE 'linux[[:space:]]*/casper/[a-z-]*vmlinuz[^"]*ds=nocloud;' "$GRUB_MOD" \
    && die "grub.cfg carries an UNQUOTED ds=nocloud;s=... — GRUB would split it on the ';' and boot without a seed location, dropping Subiquity to the interactive installer. Refusing to build."
log "grub.cfg patched: $(grep -c 'autoinstall "ds=nocloud' "$GRUB_MOD") boot entr(y/ies) now carry a QUOTED autoinstall ds=nocloud"

# ── 3b. append an entry that autoinstalls NOTHING ───────────────────────────
# The sed above rewrites EVERY /casper/*vmlinuz line, which is correct for the
# zero-keypress goal and leaves the image with no way to look at the machine.
# That gap is not hypothetical: the storage `match:` in both targets' user-data
# is a containment pin, and a pin that does not match is DESIGNED to halt. When
# it halts, Subiquity prints "press enter to start a shell" and then the
# non-interactive path (interactive-sections: [], shutdown: reboot) takes the
# box down before anyone can answer it — so the one question worth asking at
# that moment ("what disks does this machine actually report?") had to be
# answered by hand-editing the GRUB line at the console.
#
# The entry is built from the ORIGINAL, unpatched linux/initrd lines rather
# than written out literally, so it tracks Ubuntu's kernel paths instead of
# pinning /casper/vmlinuz here as a second place to update. It boots the stock
# live installer: Help -> "Enter shell", or Ctrl+Alt+F2 for a console.
#
# APPENDED LAST ON PURPOSE. GRUB's default is entry 0, so position IS the
# default-boot guarantee — the unattended install stays what happens when
# nobody touches the keyboard. `set default=0` makes that explicit rather than
# leaving it as an artifact of ordering; grub.cfg is fully sourced before the
# menu is drawn, so setting it here applies to entries declared above.
#
# Named for the job, not the machine: both targets repack through this script.
DIAG_LINUX="$(grep -m1 -E '^[[:space:]]*linux[[:space:]]+/casper/[a-z-]*vmlinuz' "$GRUB_ORIG" || true)"
DIAG_INITRD="$(grep -m1 -E '^[[:space:]]*initrd[[:space:]]+/casper/[a-z-]*initrd' "$GRUB_ORIG" || true)"
[ -n "$DIAG_LINUX" ] && [ -n "$DIAG_INITRD" ] \
    || die "couldn't find a /casper/vmlinuz + /casper/initrd pair in grub.cfg to base the diagnostic entry on - Ubuntu changed its grub.cfg layout, update the patterns above"

# The unpinned installer entry (2026-08-06) — same kernel line, but seeded from
# /nocloud-confirm/ (staged in 2b), whose storage section is interactive. Built
# by running the SAME sed as the pinned entries over the original line, so the
# quoting rule that cost a boot on 2026-07-30 is applied here too rather than
# re-typed by hand and re-learned the same way.
CONFIRM_LINUX="$(printf '%s\n' "$DIAG_LINUX" | sed -e "s#\(linux[[:space:]]*/casper/[a-z-]*vmlinuz\)\( \)\+---#\1 autoinstall \"ds=nocloud;s=/cdrom/nocloud-confirm/\" ---#")"
[ "$CONFIRM_LINUX" != "$DIAG_LINUX" ] \
    || die "couldn't inject the confirm seed onto the kernel line - it would boot the unpinned entry with no seed at all, dropping to a bare interactive installer with no payload. Refusing to build."

cat >> "$GRUB_MOD" <<EOF

menuentry "Diagnostic Shell (no autoinstall)" {
	set gfxpayload=keep
$DIAG_LINUX
$DIAG_INITRD
}
menuentry "Install - choose target disk manually (unpinned)" {
	set gfxpayload=keep
$CONFIRM_LINUX
$DIAG_INITRD
}
set default=0
EOF

# The whole point of the entry is the absence of one string. Assert it, because
# a future change to the sed above (a pattern that matches the appended block
# too, say) would silently turn the escape hatch back into an unattended wipe —
# and it would look identical in the menu.
#
# Grep the KERNEL LINE, not the whole block: the entry's own title contains the
# word "autoinstall" ("no autoinstall"), so a block-wide grep matches its own
# label and this guard fails every build. Found by running it.
awk '/^menuentry "Diagnostic Shell/,/^}/' "$GRUB_MOD" | grep -E '^[[:space:]]*linux' | grep -q 'autoinstall' \
    && die "the 'Diagnostic Shell' entry carries 'autoinstall' on its kernel line - it would wipe the disk instead of giving you a shell. Refusing to build."
grep -q '^set default=0' "$GRUB_MOD" \
    || die "grub.cfg lost 'set default=0' - the unattended entry may no longer be the default boot. Refusing to build."
# Same quoting rule as the pinned entries, asserted the same way: unquoted, GRUB
# splits on the ';' and this entry boots with no seed and no payload.
grep -q 'autoinstall "ds=nocloud;s=/cdrom/nocloud-confirm/"' "$GRUB_MOD" \
    || die "the unpinned install entry lost its QUOTED confirm-seed argument. Refusing to build."
log "grub.cfg: added 'Diagnostic Shell (no autoinstall)' + 'Install - choose target disk manually (unpinned)'; default stays entry 0 (pinned, unattended)"

# ── 4. repack: reuse the ORIGINAL El Torito boot catalog + hybrid MBR/GPT via
#      "-boot_image any replay" instead of hand-building a new one — this is
#      what keeps BOTH BIOS and UEFI boot working without re-deriving Ubuntu's
#      boot images ourselves. ─────────────────────────────────────────────────
REPACKED_ISO="$OUT_DIR/$ISO_NAME"
rm -f "$REPACKED_ISO"
log "repacking -> $REPACKED_ISO (a few minutes; copies ~3GB)"
# THE THREE MODE COMMANDS ARE PART OF THE MAP, not decoration. `-map` records
# whatever the staging filesystem reported, and on a WSL build that is 0777 with
# the builder's uid — which late-command 3's `cp -a` then reproduces on /target,
# world-writable and owned by uid 1000 (the hub's `hub` / the panel's `panel`).
# `go-w` is SUBTRACTIVE, so it is safe to run unconditionally: it can only ever
# remove a group/other write bit, never widen anything, and it leaves a staged
# 0600 secret at 0600. `-chown_r`/`-chgrp_r` are scoped to /deploy-payload so
# nothing about Ubuntu's own files in the repacked ISO changes.
xorriso -indev "$SRC_ISO" -outdev "$REPACKED_ISO" \
    -volid "$VOLID" \
    -map "$GRUB_MOD" /boot/grub/grub.cfg \
    -map "$NOCLOUD_DIR/user-data" /nocloud/user-data \
    -map "$NOCLOUD_DIR/meta-data" /nocloud/meta-data \
    -map "$CONFIRM_DIR/user-data" /nocloud-confirm/user-data \
    -map "$CONFIRM_DIR/meta-data" /nocloud-confirm/meta-data \
    -map "$NOCLOUD_DIR/deploy-payload" /deploy-payload \
    -chmod_r go-w /deploy-payload -- \
    -chown_r 0 /deploy-payload -- \
    -chgrp_r 0 /deploy-payload -- \
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
# A menu entry pointing at a seed that is not on the ISO boots to a bare
# interactive installer with no payload and no credentials — it LOOKS like a
# working escape hatch right up until it hands you a useless install.
xorriso -indev "$REPACKED_ISO" -find /nocloud-confirm/user-data >/dev/null 2>&1 \
    || die "repacked ISO is missing /nocloud-confirm/user-data, but the GRUB menu offers the unpinned entry that boots from it - do not use this ISO"
xorriso -indev "$REPACKED_ISO" -find /deploy-payload >/dev/null 2>&1 \
    || die "repacked ISO is missing /deploy-payload - do not use this ISO"
# And the modes it will hand to `cp -a` (2026-08-04).
assert_iso_payload_modes "$REPACKED_ISO" /deploy-payload

# The volume label, read back off the built ISO rather than trusted from the
# command line. `-volid` is one option among several xorriso accepts that can
# be overridden by a later one (`-boot_image any replay` replays a great deal
# from the source image), and a silently-inherited "Ubuntu-Server 24.04.x LTS
# amd64" is exactly the unlabelled-looking stick this is meant to prevent —
# only now it would also have passed a build that claimed to set it.
ACTUAL_VOLID="$(xorriso -indev "$REPACKED_ISO" -pvd_info 2>/dev/null \
    | sed -n 's/^Volume Id[[:space:]]*:[[:space:]]*//p' | head -n1)"
[ "$ACTUAL_VOLID" = "$VOLID" ] \
    || die "repacked ISO reports volume id '$ACTUAL_VOLID', expected '$VOLID' - the stick would mount unlabelled and the writer would not find it by name. Do not use this ISO"
log "volume label: $VOLID (in the ISO itself; survives the raw write)"

log "OK — repacked ISO ready: $REPACKED_ISO (BIOS + UEFI boot images intact, /nocloud + /deploy-payload present)"
# The account and the console story differ per target — the wall autologins
# into the kiosk on tty1, so a shell there means Ctrl+Alt+F2.
if [ "$TARGET" = "wall" ]; then
    log "SSH:     ssh -i $SSH_KEY panel@<vm-ip>"
    log "Console: tty1 runs the KIOSK — use Ctrl+Alt+F2 for a shell, user 'panel',"
    log "         SIM password in $CREDS_FILE"
else
    log "SSH:     ssh -i $SSH_KEY hub@<vm-ip>"
    log "Console: user 'hub', SIM password in $CREDS_FILE"
fi
log "If the install HALTS (e.g. the storage match: pin finds no such disk): reboot,"
log "         pick 'Diagnostic Shell (no autoinstall)' from the GRUB menu (5s timeout),"
log "         then Help -> 'Enter shell' or Ctrl+Alt+F2, and run:"
log "           lsblk -o NAME,SIZE,MODEL,SERIAL"
log "           udevadm info --query=property --name=/dev/nvme0n1 | grep ID_SERIAL"
log "Next: vmtest/README.md — New-HomeHubVm.ps1 -UbuntuIsoPath $REPACKED_ISO -SeedIsoPath $REPACKED_ISO -SkipSecondDvd"
log "NOT boot-tested here (needs a VM) — this only verifies the ISO's on-disk structure."
