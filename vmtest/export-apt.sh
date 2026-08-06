#!/usr/bin/env bash
# vmtest/export-apt.sh — bake every .deb the image needs into the payload.
#
# WHY. The ISO already carries every Docker IMAGE (Q10.9 B+, export-images.sh)
# so the stack starts with no registry. It carried no PACKAGES, so the install
# still needed the Ubuntu archive AND download.docker.com to be reachable — and
# the very first thing it does with the network is `packages:`, which is apt.
#
# This repo already wrote down what that costs, at stack/autoinstall/user-data:
#
#   "netplan configured nothing, DHCP never ran, and the install died fetching
#    `cockpit` with apt exit 100 — network failure surfacing as a package
#    error, three steps downstream."
#
# That was the en*/e* bug in 2026-07. The same class recurred on 2026-08-06 on
# real hardware: no docker.asc, no /opt/homehub, no openssh-server, no cockpit,
# and a machine that booted and reported success. One cause, five symptoms,
# three steps apart from each other.
#
# With the debs baked, an install needs no network at all. The coupling between
# "the LAN was not ready" and "the box is empty" is gone, not merely made less
# likely.
#
# HOW. In a THROWAWAY ubuntu:24.04 CONTAINER, not in WSL directly, even though
# WSL is also noble/amd64. apt only downloads what is missing, and WSL already
# has curl, ca-certificates, python3 and more — so resolving here would silently
# produce a repo with holes exactly where the host happens to be provisioned.
# A clean container has the same package set the target will have: nothing.
#
# THE PACKAGE LIST IS READ OUT OF THE user-data, never retyped — the same
# discipline as test-wall-artifact.sh, and for the same reason: a hand-copied
# list stops testing whatever was added to the image last week.
#
# Usage:
#   bash vmtest/export-apt.sh --seed stack/autoinstall/user-data --out .out/apt
#   bash vmtest/export-apt.sh --seed <wall user-data> --out .out-wall/apt --target wall

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

SEED=""
OUT=""
TARGET="hub"
while [ $# -gt 0 ]; do
    case "$1" in
        --seed)   SEED="$2";   shift 2 ;;
        --out)    OUT="$2";    shift 2 ;;
        --target) TARGET="$2"; shift 2 ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) die "unknown argument '$1' (try --help)" ;;
    esac
done
[ -n "$SEED" ] || die "need --seed <path to a user-data>"
[ -f "$SEED" ] || die "not found: $SEED"
[ -n "$OUT" ]  || die "need --out <directory>"
case "$TARGET" in hub|wall) ;; *) die "--target must be hub or wall" ;; esac

require_cmd docker "Install Docker in WSL, or run this where docker is available"

# ── 1. what does this image ask for? ───────────────────────────────────────
PKGS="$(sed -n '/^  packages:/,/^  [a-z_-]*:/p' "$SEED" \
        | sed -n 's/^    -[[:space:]]*\([a-zA-Z0-9][a-zA-Z0-9.+-]*\).*/\1/p' | tr '\n' ' ')"
[ -n "$PKGS" ] || die "no packages: list could be parsed out of $SEED"

# openssh-server is NOT in packages: — it comes from `ssh: install-server: true`,
# which is also an apt install and also failed on 2026-08-06, leaving a box with
# no way in. Baking it is the whole point, so it is added explicitly here.
PKGS="$PKGS openssh-server"

# The hub additionally installs the Docker engine from download.docker.com in a
# late-command. Same treatment: baked, so first boot needs neither the archive
# nor Docker's CDN.
DOCKER_PKGS=""
if [ "$TARGET" = hub ]; then
    DOCKER_PKGS="docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
fi

log "resolving for $TARGET: $PKGS $DOCKER_PKGS"

rm -rf "$OUT"
mkdir -p "$OUT"
OUT_ABS="$(cd "$OUT" && pwd)"

# ── 2. resolve + download the closure in a clean noble container ───────────
# --no-install-recommends matches what apt does in the target for these lists;
# taking recommends here would bloat the payload with things the install will
# never ask for, and MISSING a dependency is caught by the smoke test below
# rather than by hoping the flags line up.
docker run --rm -v "$OUT_ABS:/out" ubuntu:24.04 bash -c "
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends ca-certificates curl gnupg dpkg-dev >/dev/null

    if [ -n '$DOCKER_PKGS' ]; then
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
        echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \$(. /etc/os-release && echo \$VERSION_CODENAME) stable\" \
            > /etc/apt/sources.list.d/docker.list
        apt-get update -qq
    fi

    # A second, EMPTY root is what makes this honest: the packages installed
    # above (curl, gnupg, dpkg-dev) must not be treated as already-satisfied
    # dependencies, or the repo ships without them and the target cannot
    # resolve. -o Dir::State::status=/dev/null makes apt believe nothing is
    # installed, so the closure is complete.
    mkdir -p /debs
    apt-get install -y --download-only --no-install-recommends \
        -o Dir::Cache::archives=/debs \
        -o Dir::State::status=/dev/null \
        $PKGS $DOCKER_PKGS
    cp /debs/*.deb /out/ 2>/dev/null || true

    cd /out
    dpkg-scanpackages --multiversion . > Packages
    gzip -9c Packages > Packages.gz
    ls *.deb | wc -l > .count
"

COUNT="$(cat "$OUT_ABS/.count" 2>/dev/null || echo 0)"
rm -f "$OUT_ABS/.count"
[ "$COUNT" -gt 0 ] || die "no .deb files were downloaded - the resolve produced nothing"
[ -s "$OUT_ABS/Packages" ] || die "dpkg-scanpackages produced an empty Packages index"

SIZE="$(du -sh "$OUT_ABS" | awk '{print $1}')"
log "baked $COUNT .deb(s), $SIZE, indexed at $OUT_ABS"

# ── 3. prove the repo can actually satisfy the list, OFFLINE ──────────────
# THE ARTIFACT, NOT THE EXIT CODE. A directory of .debs and a Packages file is
# not evidence that apt can resolve the list from it with no network — which is
# the entire claim being made. So: a fresh container, no archive sources at all,
# only this repo, --network none, and a real (simulated) install.
log "verifying the repo resolves with NO network"
docker run --rm --network none -v "$OUT_ABS:/repo:ro" ubuntu:24.04 bash -c "
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*
    echo 'deb [trusted=yes] file:/repo ./' > /etc/apt/sources.list.d/baked.list
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends --simulate $PKGS $DOCKER_PKGS >/dev/null
" || die "the baked repo CANNOT satisfy the package list offline - the install would fail exactly where it failed on 2026-08-06. Refusing to ship it."

log "OK - the baked repo resolves $TARGET's package list with no network at all"
