#!/usr/bin/env bash
# vmtest/export-ottertube.sh - build OtterTube's Linux app for the kids kiosk
# image and pack it the way late-command 3b unpacks it. Implements: SR-138, LLR-1414.
#
# OUTPUT: <out>/ottertube-<commit7>-linux-x64.tar.gz, holding app/ (electron-
# builder's linux-unpacked, with ottertube-desktop, chrome-sandbox 4755 root:root,
# VERSION and build-info.json). A TARBALL, not the AppImage, for the panel's
# reason: tar carries chrome-sandbox's setuid bit and root ownership, which
# Electron needs on Ubuntu 24.04, and an AppImage would also need FUSE.
#
# BUILT FROM A CLEAN CLONE, never from the checkout in place. The Windows
# checkout's desktop/node_modules may be a junction into another checkout, and
# electron-builder then leaves out the usb native binding (OtterTube's
# desktop/README); a Linux build also needs Linux node_modules. Cloning the
# commit makes the artifact exactly that commit, so it is never "-dirty".
#
# Needs: git, Node >= 24 and npm on PATH, network (npm ci, Electron download).
# Run it on a Linux filesystem (WSL's home, not /mnt/c): npm ci over DrvFs is
# slow and its file modes are wrong.
#
# Usage:
#   bash vmtest/export-ottertube.sh --out vmtest/.out-kids/kids-app [--src ../OtterTube] [--ref dev]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

REPO_ROOT="$(repo_root)"
SRC="${OTTERTUBE_SRC:-$REPO_ROOT/../OtterTube}"
REF="${OTTERTUBE_REF:-dev}"
OUT=""
WORK=""

while [ $# -gt 0 ]; do
    case "$1" in
        --out)  OUT="$2"; shift 2 ;;
        --src)  SRC="$2"; shift 2 ;;
        --ref)  REF="$2"; shift 2 ;;
        --work) WORK="$2"; shift 2 ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

[ -n "$OUT" ] || die "need --out <directory>"
[ -d "$SRC/.git" ] || [ -f "$SRC/.git" ] || die "no OtterTube git checkout at $SRC (set --src or OTTERTUBE_SRC)"
require_cmd git "Install with: sudo apt-get install -y git"
require_cmd node "Install Node 24 (nodejs.org tarball under ~/.local is enough)"
require_cmd npm "npm comes with Node"
node_major="$(node -p 'process.versions.node.split(".")[0]')"
[ "$node_major" -ge 24 ] || die "Node $(node --version) is too old: OtterTube's package.json requires >= 24"

commit="$(git -C "$SRC" rev-parse --verify "$REF^{commit}")" || die "OtterTube has no ref '$REF'"
short="${commit:0:7}"
WORK="${WORK:-$(mktemp -d)}"
clone="$WORK/ottertube"
rm -rf "$clone"
log "cloning OtterTube $REF ($short) from $SRC"
git clone -q --no-checkout "$SRC" "$clone"
git -C "$clone" checkout -q --detach "$commit"

log "npm ci (root and desktop/), then the Linux build - a few minutes"
( cd "$clone" && npm ci --no-audit --no-fund )
( cd "$clone/desktop" && npm ci --no-audit --no-fund && npm run dist:linux )

unpacked="$clone/desktop/release/linux-unpacked"
[ -x "$unpacked/ottertube-desktop" ] || die "the build produced no $unpacked/ottertube-desktop"
[ -f "$unpacked/chrome-sandbox" ] || die "the build produced no chrome-sandbox"

# Stage app/ with the modes the box needs, then let tar record them as root's.
stage="$WORK/stage"
rm -rf "$stage"; mkdir -p "$stage"
cp -a "$unpacked" "$stage/app"
chmod -R go-w "$stage/app"
chmod 4755 "$stage/app/chrome-sandbox"
version="$(node -p "require('$clone/desktop/package.json').version")"
printf 'OtterTube %s (%s)\n' "$version" "$short" > "$stage/app/VERSION"
printf '{"product":"OtterTube","version":"%s","commit":"%s","ref":"%s","builtAt":"%s"}\n' \
    "$version" "$commit" "$REF" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$stage/app/build-info.json"

mkdir -p "$OUT"
rm -f "$OUT"/ottertube-*-linux-x64.tar.gz
tarball="$OUT/ottertube-$short-linux-x64.tar.gz"
tar --owner=0 --group=0 --numeric-owner -czf "$tarball" -C "$stage" app

# Verify the ARTIFACT, not the exit codes above.
listing="$(tar -tvzf "$tarball")"
printf '%s\n' "$listing" | grep -Eq '^-rwsr-xr-x 0/0 .* app/chrome-sandbox$' \
    || die "the tarball's app/chrome-sandbox is not -rwsr-xr-x 0/0"
printf '%s\n' "$listing" | grep -Eq '^-rwxr-xr-x 0/0 .* app/ottertube-desktop$' \
    || die "the tarball's app/ottertube-desktop is not an executable owned by 0/0"
log "OtterTube $version ($short): $tarball ($(du -h "$tarball" | cut -f1)), sha256 $(sha256sum "$tarball" | cut -d' ' -f1)"
