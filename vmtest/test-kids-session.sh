#!/usr/bin/env bash
# vmtest/test-kids-session.sh — the KIDS KIOSK's session, end to end, in a
# container that installs exactly the image's packages.list. Verifies: TC-1621..TC-1626.
#
# What it does, as the image does it:
#   - installs stack/autoinstall/kids/packages.list (no recommends) in ubuntu:24.04
#   - unpacks export-ottertube.sh's tarball as root (late-command 3b) and checks
#     chrome-sandbox kept its setuid bit and every shared library resolves
#   - installs the session files where late-command 5 puts them and runs the
#     REAL kids-firstboot.sh against the container's filesystem
#   - then, as the unprivileged `kiosk` user with Electron's setuid sandbox ON,
#     runs sway's headless backend with the image's sway.config, and drives the
#     Owner's requirement: plug a "TV", unplug it (sampled every 100 ms for 3 s),
#     plug a new one. After every step OtterTube must be the same process,
#     fullscreen, visible and on the touch output; each TV must show a mirror.
#
# The container gets SYS_ADMIN and no seccomp filter: chrome-sandbox is setuid
# root and builds namespaces, which Docker's defaults forbid even to root.
# Without them the test would have to pass --no-sandbox and would stop proving
# the thing the tarball's setuid bit exists for.
#
# NOT covered (README): real HDMI hotplug, real touch, a GPU, Wi-Fi, systemd
# (firstboot's systemctl calls are skipped by its KIDS_ROOT test mode).
#
# Usage:
#   bash vmtest/test-kids-session.sh --app <ottertube-*-linux-x64.tar.gz> [--shots <dir>]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
REPO_ROOT="$(repo_root)"
# KIDS_DIR exists for mutation checks: point it at an altered copy and the
# checks below must fail.
KIDS="${KIDS_DIR:-$REPO_ROOT/stack/autoinstall/kids}"
APP=""
SHOTS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --app)   APP="$2"; shift 2 ;;
        --shots) SHOTS="$2"; shift 2 ;;
        -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done
[ -f "$APP" ] || die "need --app <ottertube-*-linux-x64.tar.gz> (vmtest/export-ottertube.sh builds it)"
require_cmd docker "Install Docker in WSL, or run this where docker is available"
APP="$(cd "$(dirname "$APP")" && pwd)/$(basename "$APP")"
SHOTS="${SHOTS:-$(mktemp -d)}"; mkdir -p "$SHOTS"; chmod 777 "$SHOTS"

# The image is keyed by the package list, so editing the list rebuilds it.
pkgs="$(read_packages_list "$KIDS/packages.list" | tr '\n' ' ')"
tag="kids-kiosk-session:$(printf '%s' "$pkgs" | sha256sum | cut -c1-12)"
if ! docker image inspect "$tag" >/dev/null 2>&1; then
    ctx="$(mktemp -d)"
    cat > "$ctx/Dockerfile" <<EOF
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends $pkgs grim \\
 && rm -rf /var/lib/apt/lists/* && useradd -m -s /bin/bash kiosk
EOF
    log "building $tag from packages.list (+ grim for screenshots)"
    docker build -q -t "$tag" "$ctx" >/dev/null
    rm -rf "$ctx"
fi

docker run --rm --shm-size=512m --cap-add SYS_ADMIN --security-opt seccomp=unconfined \
    -v "$KIDS":/kids:ro -v "$SCRIPT_DIR/lib":/lib-test:ro -v "$APP":/artifact.tar.gz:ro -v "$SHOTS":/out \
    "$tag" bash /lib-test/kids-session-inner.sh
log "screenshots: $SHOTS"
