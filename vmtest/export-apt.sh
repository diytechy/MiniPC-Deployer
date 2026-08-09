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
# THE PROOF RUNS AGAINST THE ISO'S OWN INSTALL BASE when --src-iso is given, and
# it should always be given. The download above happens in an EMPTY root on
# purpose (see -o Dir::State::status=/dev/null); the PROOF is the opposite
# question — "can apt reach this list from the machine the install actually
# starts on, without deleting anything" — and answering it in a bare container
# is what let defect 31 ship a repo that removed the boot path. Step 3 says so
# at length. Without --src-iso the script still runs and says, in the log, that
# it is proving something weaker.
#
# Usage:
#   bash vmtest/export-apt.sh --target hub  --out .out/apt --src-iso /path/to/ubuntu-24.04.x-live-server-amd64.iso
#   bash vmtest/export-apt.sh --target wall --out .out-wall/apt --src-iso ...
#   bash vmtest/export-apt.sh --target hub --list some/other/packages.list --out ...

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

REPO_ROOT="$(repo_root)"
LIST=""
OUT=""
TARGET="hub"
SRC_ISO=""
while [ $# -gt 0 ]; do
    case "$1" in
        --list)    LIST="$2";    shift 2 ;;
        --out)     OUT="$2";     shift 2 ;;
        --target)  TARGET="$2";  shift 2 ;;
        # THE BASE THE PROOF RESOLVES AGAINST. See step 3: without it the offline
        # check runs on a bare ubuntu:24.04 container, which is NOT the machine
        # the install runs on, and the difference is what let defect 31 ship.
        --src-iso) SRC_ISO="$2"; shift 2 ;;
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

# ── 3. prove the repo can satisfy the list OFFLINE, ON THE RIGHT BASE ─────
# THE ARTIFACT, NOT THE EXIT CODE. A directory of .debs and a Packages file is
# not evidence that apt can resolve the list from it with no network — which is
# the entire claim being made. So: no archive sources at all, only this repo,
# --network none, and a real (simulated) install.
#
# TWO THINGS THIS ASKED WRONGLY UNTIL 2026-08-09, AND DEFECT 31 WENT THROUGH THE
# GAP BETWEEN THEM.
#
# 1. IT RESOLVED AGAINST THE WRONG MACHINE. A bare ubuntu:24.04 container ships
#    no udev, no initramfs-tools, no cloud-init, no netplan.io — subiquity's
#    /target has all of them. A conflict that can only appear when those are
#    ALREADY INSTALLED cannot appear in a container that has never had them, so
#    the resolution being proved was never the resolution that would run.
#
# 2. A REMOVAL IS A VALID RESOLUTION. It asked whether apt COULD satisfy the
#    list with no network, got yes, and never asked what the answer cost. Naming
#    systemd-resolved pulled systemd 8.16 while the ISO base carries 8.12; every
#    binary of that source depends on its own exact version; udev was unnamed, so
#    no 8.16 of it was ever fetched, and apt resolved by REMOVING udev,
#    initramfs-tools, cloud-init, netplan.io, ubuntu-server and the rest of the
#    boot path. `-y` answered the question a human would have stopped at.
#
# Both are fixed here, and they only work together: --no-remove on a container
# with nothing to remove refuses nothing. Measured 2026-08-09 against the real
# defect-31 repo — removals allowed, apt happily deletes ubuntu-server-minimal,
# cloud-init and cryptsetup-initramfs and exits 0; with the ISO base and
# --no-remove it exits 100 saying "Packages need to be removed but remove is
# disabled". That is the same flag the two late-commands pass, so the question
# asked here is now the question the installer will ask.
STATUS_ARG=""
STATUS_MOUNT=""
if [ -n "$SRC_ISO" ]; then
    [ -f "$SRC_ISO" ] || die "--src-iso not found: $SRC_ISO"
    require_cmd xorriso    "Install with: sudo apt-get install -y xorriso"
    require_cmd unsquashfs "Install with: sudo apt-get install -y squashfs-tools"

    # THE ISO'S OWN dpkg STATUS, NOT A LIST OF NAMES. The casper manifests give
    # name+version and nothing else, and a synthetic status built from them is
    # WORSE THAN USELESS HERE: with no Depends: fields apt sees no conflict, the
    # proof passes, and it passes for a reason that has nothing to do with the
    # question. The squashfs carries the real /var/lib/dpkg/status — 602 stanzas
    # with full dependency metadata at the exact versions the target will have.
    # unsquashfs extracts that one path without unpacking the image.
    SQ_NAME="casper/ubuntu-server-minimal.ubuntu-server.squashfs"
    SQ_TMP="$(mktemp -d)"
    trap 'rm -rf "$SQ_TMP"' EXIT
    log "reading the install base from $SRC_ISO ($SQ_NAME)"
    xorriso -osirrox on -indev "$SRC_ISO" -extract "/$SQ_NAME" "$SQ_TMP/base.squashfs" >/dev/null 2>&1 \
        || die "could not extract /$SQ_NAME from $SRC_ISO. It is the layer subiquity copies to /target; a server ISO that does not carry it is not the image this build targets. Inspect with: xorriso -indev '$SRC_ISO' -find /casper -name '*.squashfs'"
    unsquashfs -q -n -f -d "$SQ_TMP/x" "$SQ_TMP/base.squashfs" /var/lib/dpkg/status >/dev/null 2>&1 \
        || die "could not read /var/lib/dpkg/status out of $SQ_NAME"
    STATUS_FILE="$SQ_TMP/x/var/lib/dpkg/status"
    [ -s "$STATUS_FILE" ] || die "the status file extracted from $SQ_NAME is empty"
    BASE_N="$(grep -c '^Package: ' "$STATUS_FILE" || echo 0)"
    [ "$BASE_N" -gt 100 ] || die "only $BASE_N packages in the install base — that is not a server root filesystem, and a proof against it would be as empty as the container one it replaces"
    log "  install base: $BASE_N packages (this is what /target looks like before late-command 3c)"
    STATUS_MOUNT="-v $STATUS_FILE:/status:ro"
    STATUS_ARG="cat /status > /var/lib/dpkg/status;"
else
    # SAID OUT LOUD, because a weaker proof that looks identical in the log is
    # how this defect survived. Not fatal: --no-remove below still catches the
    # subset of removals a bare container can see, and the ISO is not always to
    # hand (test fixtures, a repo re-bake).
    log "WARNING: no --src-iso, so this resolves against a BARE ubuntu:24.04 container, which"
    log "  has no udev, initramfs-tools, cloud-init or netplan.io — a conflict that only appears"
    log "  when those are installed CANNOT be seen here. This is the WEAKER proof; pass"
    log "  --src-iso <ubuntu-server.iso> to run it against the set the install starts from."
fi

log "verifying the repo resolves with NO network"
# shellcheck disable=SC2086
docker run --rm --network none -v "$OUT_ABS:/repo:ro" $STATUS_MOUNT ubuntu:24.04 bash -c "
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*
    echo 'deb [trusted=yes] file:/repo ./' > /etc/apt/sources.list.d/baked.list
    $STATUS_ARG
    apt-get update -qq
    apt-get install -y --no-install-recommends --no-remove --simulate $PKGS >/dev/null
" || die "the baked repo CANNOT satisfy the package list offline WITHOUT REMOVING SOMETHING. Either a dependency is missing (the install would fail exactly where it failed on 2026-08-06), or resolving this list costs the removal of packages the target already has (defect 31: that is how naming one package deleted the boot path). Re-run without --no-remove to see which, and read any 'Remv' line as a package the install would delete: docker run --rm --network none -v '$OUT_ABS:/repo:ro' ubuntu:24.04 bash -c \"rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*; echo 'deb [trusted=yes] file:/repo ./' > /etc/apt/sources.list.d/baked.list; apt-get update -qq; apt-get install -y --no-install-recommends --simulate $PKGS\" . The fix is to NAME the packages apt wants to remove in packages.list, so a matching version is fetched too. Refusing to ship it."

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
