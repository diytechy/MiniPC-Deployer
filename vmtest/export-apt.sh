#!/usr/bin/env bash
# vmtest/export-apt.sh — bake every .deb the image needs into the payload.
#
# WHY. The ISO already carries every Docker IMAGE (Q10.9 B+, export-images.sh)
# so the stack starts with no registry. It carried no PACKAGES, so the install
# still needed the Ubuntu archive AND download.docker.com to be reachable — and
# the very first thing it did with the network was `packages:`, which is apt.
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
# THE PACKAGE LIST IS READ OUT OF stack/autoinstall/packages.list (the wall's
# is stack/autoinstall/wall/packages.list), never retyped. That file is the SSOT
# the INSTALLER reads too — late-command 3c runs `apt-get install` against the
# very same file, carried on the payload — so the repo this script bakes and the
# list the box installs cannot drift apart by construction. See the header of
# stack/autoinstall/packages.list for why the list is no longer `packages:`.
#
# Usage:
#   bash vmtest/export-apt.sh --target hub  --out .out/apt
#   bash vmtest/export-apt.sh --target wall --out .out-wall/apt
#   bash vmtest/export-apt.sh --target hub --list some/other/packages.list --out ...

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

REPO_ROOT="$(repo_root)"
LIST=""
OUT=""
TARGET="hub"
while [ $# -gt 0 ]; do
    case "$1" in
        --list)   LIST="$2";   shift 2 ;;
        --out)    OUT="$2";    shift 2 ;;
        --target) TARGET="$2"; shift 2 ;;
        # Accepted and REFUSED rather than ignored. The first cut of this script
        # parsed `packages:` out of a user-data; that list has moved to a file
        # both this script and the installer read, and a --seed silently doing
        # nothing would bake a repo from the wrong source while reporting
        # success — which is the exact shape of failure this whole change exists
        # to remove.
        --seed)   die "--seed is gone: the package list moved out of the user-data's 'packages:' and into stack/autoinstall[/wall]/packages.list (which the installer reads too). Use --target hub|wall, or --list <path>." ;;
        -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
        *) die "unknown argument '$1' (try --help)" ;;
    esac
done
case "$TARGET" in
    hub)  : ;;
    wall) : ;;
    *) die "--target must be hub or wall" ;;
esac
[ -n "$OUT" ] || die "need --out <directory>"
[ -n "$LIST" ] || LIST="$(packages_list_path "$REPO_ROOT" "$TARGET")"
[ -f "$LIST" ] || die "not found: $LIST"

require_cmd docker "Install Docker in WSL, or run this where docker is available"

# ── 1. what does this image ask for? ───────────────────────────────────────
# ONE parser, defined in lib/common.sh, shared with the stagers and the tests.
PKGS="$(read_packages_list "$LIST" | tr '\n' ' ')"
[ -n "$PKGS" ] || die "no package names could be parsed out of $LIST — refusing to bake an empty repo, which would pass the offline check below vacuously."

# THE DOCKER SOURCE IS DERIVED FROM THE LIST, not from --target. The engine's
# packages come from download.docker.com rather than the Ubuntu archive, so the
# resolver container needs that source configured — but which image wants them
# is the LIST's business, not this script's. Keying it off `docker-ce` means a
# list that stops asking for Docker stops fetching Docker's key, with nothing
# here to remember to change.
NEED_DOCKER_SRC=0
case " $PKGS " in *" docker-ce "*) NEED_DOCKER_SRC=1 ;; esac

log "resolving for $TARGET from ${LIST#"$REPO_ROOT"/}: $PKGS"
[ "$NEED_DOCKER_SRC" -eq 1 ] && log "  (the list names docker-ce, so download.docker.com is added to the resolver)"

rm -rf "$OUT"
mkdir -p "$OUT"
OUT_ABS="$(cd "$OUT" && pwd)"

# ── 2. resolve + download the closure in a clean noble container ───────────
# --no-install-recommends matches what the install does in the target for these
# lists; taking recommends here would bloat the payload with things the install
# will never ask for, and MISSING a dependency is caught by the smoke test below
# rather than by hoping the flags line up.
docker run --rm -v "$OUT_ABS:/out" ubuntu:24.04 bash -c "
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends ca-certificates curl gnupg dpkg-dev >/dev/null

    if [ '$NEED_DOCKER_SRC' = '1' ]; then
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
        $PKGS
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
    apt-get install -y -qq --no-install-recommends --simulate $PKGS >/dev/null
" || die "the baked repo CANNOT satisfy the package list offline - the install would fail exactly where it failed on 2026-08-06. Refusing to ship it."

# ── 4. record WHICH list this repo was baked from ─────────────────────────
# The debs are FROZEN at this moment; the names the installer asks for are read
# from the tracked packages.list at INSTALL time. Those two are the same file
# today and can silently stop being: export the repo, add a package, build the
# ISO, and the payload asks for something the baked repo has never heard of —
# an install that dies on a dead network, which is the whole failure being
# removed here. So the resolved list is written beside the debs and
# stage_apt_into_payload refuses to stage a repo whose stamp disagrees with the
# list about to ride the same ISO.
printf '%s\n' $PKGS > "$OUT_ABS/packages.baked.list"

log "OK - the baked repo resolves $TARGET's package list with no network at all"
log "     stamped $OUT_ABS/packages.baked.list ($(printf '%s\n' $PKGS | wc -l) names) — stage_apt_into_payload checks it against $LIST"
