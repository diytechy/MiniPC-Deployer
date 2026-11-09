#!/usr/bin/env bash
# install-camera-clock-sdk.sh - put Hikvision's HCNetSDK libraries on the hub
# for camera_clock.py (SR-161), from the release zip, pinned.
#
#   sudo bash install-camera-clock-sdk.sh --zip /tmp/EN-HCNetSDKV6.1.11.40_build20260922_linux64.zip
#
# The zip is NOT in this repository (proprietary). sdk.manifest.json names its
# URL, its sha256 and the sha256 of every file under its lib/. This refuses a
# zip with other bytes, extracts lib/ only into a staging directory beside the
# live one, verifies every file there, and swaps the set in whole (the previous
# set comes back if the swap fails). Python's zipfile does the extraction, with
# every member path checked to stay under lib/.
set -euo pipefail

fail() { echo "[camera-clock-sdk] $*" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
root=${CAMERA_CLOCK_SDK_ROOT:-/opt/homehub-camera-clock}
zip=
while [ "$#" -gt 0 ]; do
    case $1 in
        --zip) [ "$#" -ge 2 ] || fail "--zip needs a path"; zip=$2; shift 2 ;;
        *) fail "usage: $0 --zip PATH" ;;
    esac
done
[ "$(id -u)" = 0 ] || fail "run as root"
[ -f "$zip" ] || fail "--zip PATH is required"
want=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["zip"]["sha256"])' "$here/sdk.manifest.json")
[ "$(sha256sum "$zip" | cut -d' ' -f1)" = "$want" ] || fail "the zip is not the pinned release (sdk.manifest.json)"

install -d -m 0755 "$root"
stage=$(mktemp -d "$root/.sdk.XXXXXX")
trap 'rm -rf "$stage"' EXIT
python3 -I - "$zip" "$here/sdk.manifest.json" "$stage/lib" <<'PY'
import hashlib, json, os, sys, zipfile
zpath, mpath, out = sys.argv[1:4]
m = json.load(open(mpath))
prefix, files = m["zip"]["lib_prefix"], m["files"]
with zipfile.ZipFile(zpath) as z:
    for rel, want in files.items():
        if rel.startswith("/") or ".." in rel.split("/"):
            sys.exit("[camera-clock-sdk] manifest path %r is not a plain relative path" % rel)
        data = z.read(prefix + rel)
        if hashlib.sha256(data).hexdigest() != want:
            sys.exit("[camera-clock-sdk] %s in the zip is not the pinned bytes" % rel)
        dest = os.path.join(out, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as h:
            h.write(data)
        os.chmod(dest, 0o644)
print("[camera-clock-sdk] %d pinned files staged" % len(files))
PY
python3 -I "$here/camera_clock.py" --help >/dev/null
python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import camera_clock as c; c.verify_sdk(sys.argv[2], c.read_manifest())' \
    "$here" "$stage" || fail "the staged SDK does not verify"
chmod -R a+rX "$stage"
chmod 0755 "$stage"
rm -rf "$root/sdk.previous"
[ ! -e "$root/sdk" ] || mv -T "$root/sdk" "$root/sdk.previous"
if ! mv -T "$stage" "$root/sdk"; then
    [ ! -e "$root/sdk.previous" ] || mv -T "$root/sdk.previous" "$root/sdk"
    fail "could not swap the SDK in; the previous set is back"
fi
trap - EXIT
rm -rf "$root/sdk.previous"
echo "[camera-clock-sdk] installed: $root/sdk/lib ($(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["release"])' "$here/sdk.manifest.json"))"
