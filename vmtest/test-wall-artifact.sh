#!/usr/bin/env bash
# vmtest/test-wall-artifact.sh — does the wall image's package list actually
# carry the shell? Run it and find out, instead of taking a ledger's word.
#
# WHY THIS FILE EXISTS. The builder's static check proves the artifact's
# DT_NEEDED list is COVERED by `stack/autoinstall/wall/user-data`'s packages.
# That is a claim about two files. The claim that matters is different and
# stronger: install exactly those packages on a bare Ubuntu 24.04, unpack the
# artifact the way the autoinstall does, and see whether the loader is
# satisfied. That was done once by hand on 2026-08-02 and written into
# docs/status.md — which an adversarial review correctly called unsubstantiated,
# because nothing in the tree reproduces it. Now it does.
#
# WHAT IT PROVES, EXACTLY:
#   1. `tar -xzf` AS ROOT preserves chrome-sandbox as 4755 root:root — the one
#      property NTFS cannot express and every copy/unzip silently destroys.
#   2. `[ -x /opt/wall-panel/app/wall-shell ]` — the predicate wall-kiosk.sh
#      keys everything off.
#   3. `ldd` on the Electron runtime resolves EVERY library. Without this
#      package list it stops at libnspr4.so; that is the difference between a
#      panel and a black rectangle, and neither one reports an error.
#   4. The wrapper runs, prints its build stamp, and Electron initialises far
#      enough to ask for a display.
#
# WHAT IT DOES NOT PROVE: anything about `cage`, Wayland, the seat, the GPU, or
# whether the shell paints. A container has no display and no seat. That is the
# A19 gate's job and this script is not a substitute for it — it is the floor
# underneath it.
#
# Usage (WSL/Linux, needs docker and the built artifact):
#   bash vmtest/test-wall-artifact.sh
#   WALL_SHELL_DIST=/path/to/dist bash vmtest/test-wall-artifact.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST="${WALL_SHELL_DIST:-$(cd "$REPO_ROOT/.." && pwd)/OfficeWallNaglight/dist}"
IMAGE="${TEST_BASE_IMAGE:-ubuntu:24.04}"

command -v docker >/dev/null 2>&1 || {
    echo "docker not found — this check needs a throwaway Ubuntu to install into." >&2
    echo "On the dev box, run it inside WSL." >&2
    exit 2
}
ls "$DIST"/officewall-shell-*-linux-x64.tar.gz >/dev/null 2>&1 || {
    echo "no shell artifact in $DIST — build it with 'npm run dist' in OfficeWallNaglight," >&2
    echo "or point WALL_SHELL_DIST somewhere else." >&2
    exit 2
}

# The package list is READ OUT OF THE IMAGE DEFINITION, never retyped here.
# A copy would drift, and a drifted copy would prove the wrong thing while
# looking like proof.
#
# THE DEFINITION MOVED ON 2026-08-06: `packages:` in the user-data is empty (it
# runs before any late-command, so the baked offline repo cannot serve it) and
# the names live in stack/autoinstall/wall/packages.list, which export-apt.sh
# resolves and late-command 3c installs. Parsing the user-data here would now
# find nothing and this check would install nothing and pass.
PKG_LIST="$REPO_ROOT/stack/autoinstall/wall/packages.list"
[ -f "$PKG_LIST" ] || { echo "not found: $PKG_LIST" >&2; exit 1; }
PKGS="$(awk '{ sub(/#.*/, ""); gsub(/[[:space:]]/, ""); if (length($0)) print }' "$PKG_LIST" | tr '\n' ' ')"
[ -n "$PKGS" ] || { echo "no package names could be parsed out of $PKG_LIST" >&2; exit 1; }
grep -qx 'python3-opencv' <(awk '{ sub(/#.*/, ""); gsub(/[[:space:]]/, ""); if (length($0)) print }' "$PKG_LIST") || {
    echo "python3-opencv is absent from the offline wall package contract" >&2
    exit 1
}
SHELL_ARTIFACT="$(ls "$DIST"/officewall-shell-*-linux-x64.tar.gz | head -n1)"
python3 "$REPO_ROOT/scripts/assert_wall_capabilities.py" --shell "$SHELL_ARTIFACT" || {
    echo "shell artifact lacks the Door motion/ambient capability payload" >&2
    exit 1
}

echo "base image: $IMAGE"
echo "artifact:   $(basename "$SHELL_ARTIFACT")"
echo "packages:   $PKGS"
echo

# `seccomp=unconfined` for the last step only-ish reason: Docker's default
# profile blocks the unshare(2) Chromium's zygote needs, so WITHOUT it the run
# dies at "Failed to move to new namespace" long before it reaches anything this
# check is interested in. The panel is a VM, not a container, and has no such
# restriction. Steps 1-3 (the assertions) do not depend on this; step 4 does.
docker run --rm --security-opt seccomp=unconfined \
    -v "$DIST":/art:ro -e PKGS="$PKGS" "$IMAGE" bash -c '
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
echo "--- installing the wall image package list ---"
apt-get install -y -qq $PKGS >/dev/null 2>&1 || {
    echo "APT FAILED — a package name in the wall user-data does not exist in this release."
    echo "(24.04 renamed libasound2/libatk*/libatspi/libcups/libglib to *t64; the old names are gone.)"
    apt-get install -y $PKGS 2>&1 | tail -20
    exit 1
}

echo "--- unpacking the way the autoinstall does: tar, as root ---"
install -d /opt/wall-panel
tar -xzf /art/officewall-shell-*-linux-x64.tar.gz -C /opt/wall-panel

echo "--- importing OpenCV and executing the baked Door motion module ---"
python3 -c "import cv2, runpy, sys; runpy.run_path(sys.argv[1], run_name=sys.argv[2])" \
    /opt/wall-panel/app/runtime/resources/app/doorstream/motion.py artifact_probe \
    && echo "ok    cv2 imports and the actual artifact motion.py executes in the package closure" \
    || { echo "FAIL  python3-opencv or the artifact motion module is not executable in the baked closure"; exit 1; }

rc=0
[ -x /opt/wall-panel/app/wall-shell ] \
    && echo "ok    [ -x /opt/wall-panel/app/wall-shell ]  (the predicate wall-kiosk.sh tests)" \
    || { echo "FAIL  wall-shell is not executable"; rc=1; }

mode=$(stat -c "%a %U:%G" /opt/wall-panel/app/runtime/chrome-sandbox)
[ "$mode" = "4755 root:root" ] \
    && echo "ok    chrome-sandbox survived extraction as 4755 root:root" \
    || { echo "FAIL  chrome-sandbox is $mode, not 4755 root:root — Electron will refuse to start"; rc=1; }

echo "--- ldd on the Electron runtime ---"
missing=$(ldd /opt/wall-panel/app/runtime/electron 2>/dev/null | awk "/not found/ {print \$1}" | sort -u)
if [ -n "$missing" ]; then
    echo "FAIL  the package list is INCOMPLETE. Missing:"; printf "        %s\n" $missing
    echo "      Add each to stack/autoinstall/wall/electron-runtime-deps.tsv and to"
    echo "      stack/autoinstall/wall/packages.list, then re-bake the offline repo:"
    echo "        bash vmtest/export-apt.sh --target wall --out vmtest/.out-wall/apt"
    rc=1
else
    echo "ok    every library the Electron runtime needs resolves"
fi

echo "--- how far does it actually get? (no display here, so not far — that is the point) ---"
useradd -m panel
out=$(timeout 60 su panel -c "/opt/wall-panel/app/wall-shell --disable-gpu" 2>&1 || true)
printf "%s\n" "$out" | sed "s/^/      /" | head -12
# Judge CHROMIUM lines only. The wrapper echoes its own command line, which
# contains the word "ozone" — matching that was a false green in the first
# version of this script, which is precisely the failure mode the whole file
# exists to argue against. Drop the wrapper prefix before deciding.
verdict=$(printf "%s\n" "$out" | grep -v "^\[wall-shell\]" || true)
if printf "%s\n" "$verdict" | grep -q "cannot open shared object file"; then
    echo "FAIL  Electron died at library load despite ldd being clean — a dlopen dependency"
    echo "      that DT_NEEDED does not list. Add it to electron-runtime-deps.tsv."
    rc=1
elif printf "%s\n" "$verdict" | grep -qE "Missing X server|platform failed to initialize"; then
    echo "ok    Electron initialised and stopped for want of a display."
    echo "      That is the floor this check can reach. Whether it PAINTS under cage is"
    echo "      the A19 gate, and nothing here is a substitute for it."
elif printf "%s\n" "$verdict" | grep -qE "Failed to move to new namespace|zygote_host"; then
    echo "WARN  the CONTAINER sandbox stopped it before Electron got that far — the host"
    echo "      denied unshare(2). Steps 1-3 above still hold; step 4 proved nothing."
    echo "      Not a panel problem: a VM has no such restriction."
else
    echo "WARN  Electron stopped somewhere unexpected — read the output above."
    echo "      Not failing the run: a container is not a panel and step 4 is"
    echo "      informational. Steps 1-3 above are the assertions."
fi
exit $rc
'
echo
echo "wall artifact floor: PASSED (see the per-line results above)"
