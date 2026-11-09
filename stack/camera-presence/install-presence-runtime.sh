#!/usr/bin/env bash
# install-presence-runtime.sh - the presence detector's runtime on the hub
# (SR-159): onnxruntime in /opt/homehub-presence/venv and the pinned models in
# /opt/homehub-presence/models. Nothing is fetched from an index.
#
#   sudo bash install-presence-runtime.sh --wheelhouse DIR --models DIR
#
# THE WHEELS are the panel's: the hub runs the same Ubuntu 24.04 / CPython 3.12 /
# x86-64 as the panel, so the reviewed, hash-pinned sensor wheelhouse
# (stack/autoinstall/wall/build-sensor-wheelhouse.sh, REPRODUCE mode) is
# installed unchanged: one reviewed lock for both machines. The lock must be
# byte-identical to the committed one (check-wheelhouse-lock.py --expect), and
# pip runs --no-index --only-binary=:all: --require-hashes.
#
# THE VENV uses --system-site-packages so the detector also sees the apt
# python3-opencv the hub's motion detector already needs. Creating a venv needs
# python3.12-venv (ensurepip), which the hub did not have on 2026-11-09:
# `apt-get install --no-install-recommends python3.12-venv python3-opencv`.
#
# THE MODELS are not in this repository; each file named in models.manifest.json
# is checked against its sha256 before it is installed, and the detector checks
# again at every start. The venv is swapped in whole, never edited in place.
set -euo pipefail

fail() { echo "[presence-runtime] $*" >&2; exit 1; }
note() { echo "[presence-runtime] $*"; }

here=$(cd "$(dirname "$0")" && pwd)
stack=$(cd "$here/.." && pwd)
root=${PRESENCE_ROOT:-/opt/homehub-presence}
wheelhouse= models=
while [ "$#" -gt 0 ]; do
    case $1 in
        --wheelhouse) [ "$#" -ge 2 ] || fail "--wheelhouse needs a path"; wheelhouse=$2; shift 2 ;;
        --models) [ "$#" -ge 2 ] || fail "--models needs a path"; models=$2; shift 2 ;;
        *) fail "usage: $0 --wheelhouse DIR --models DIR" ;;
    esac
done
[ "$(id -u)" = 0 ] || fail "run as root"
[ -d "$wheelhouse" ] || fail "--wheelhouse DIR is required"
[ -d "$models" ] || fail "--models DIR is required"
reviewed_lock="$stack/autoinstall/wall/sensor-wheelhouse/requirements.lock"
[ -f "$reviewed_lock" ] || fail "the reviewed lock is missing: $reviewed_lock"
python3 "$stack/autoinstall/wall/check-wheelhouse-lock.py" --expect "$reviewed_lock" "$wheelhouse" \
    || fail "wheelhouse rejected; rebuild it with build-sensor-wheelhouse.sh"
python3 -c 'import ensurepip' 2>/dev/null \
    || fail "python3.12-venv is not installed (apt-get install --no-install-recommends python3.12-venv)"
python3 -c 'import cv2' 2>/dev/null \
    || fail "python3-opencv is not installed (apt-get install --no-install-recommends python3-opencv)"

install -d -m 0755 "$root"
# ── the models: verify every one against the manifest before anything moves ──
mapfile -t names < <(python3 -c 'import json,sys; print("\n".join(sorted(json.load(open(sys.argv[1]))["models"])))' \
    "$here/models.manifest.json")
for name in "${names[@]}"; do
    want=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["models"][sys.argv[2]]["sha256"])' \
        "$here/models.manifest.json" "$name")
    [ -f "$models/$name" ] || fail "model $name is missing from $models"
    got=$(sha256sum "$models/$name" | cut -d' ' -f1)
    [ "$got" = "$want" ] || fail "model $name is not the pinned bytes"
done

# ── the venv, built beside the live one and swapped in whole ──
if [ -x "$root/venv/bin/python" ] && cmp -s "$wheelhouse/requirements.lock" "$root/requirements.lock" \
        && "$root/venv/bin/python" -c 'import numpy, onnxruntime, cv2' >/dev/null 2>&1; then
    note "venv already matches the reviewed lock"
else
    # A venv records its own path in its scripts, so it is built at the final
    # path with the old one set aside, and the old one comes back on any failure.
    rm -rf "$root/venv.previous"
    [ ! -e "$root/venv" ] || mv -T "$root/venv" "$root/venv.previous"
    restore() {
        rm -rf "$root/venv"
        [ ! -e "$root/venv.previous" ] || mv -T "$root/venv.previous" "$root/venv"
        echo "[presence-runtime] the new venv failed; the previous one is back" >&2
    }
    trap restore ERR
    python3 -m venv --system-site-packages "$root/venv"
    "$root/venv/bin/pip" install -q --no-index --only-binary=:all: --require-hashes \
        --find-links "$wheelhouse" -r "$wheelhouse/requirements.lock"
    "$root/venv/bin/python" -c 'import numpy, onnxruntime, cv2; print("[presence-runtime] onnxruntime", onnxruntime.__version__, "numpy", numpy.__version__, "cv2", cv2.__version__)'
    trap - ERR
    chmod 0755 "$root/venv"
    install -m 0644 "$wheelhouse/requirements.lock" "$root/requirements.lock"
    rm -rf "$root/venv.previous"
fi

install -d -m 0755 "$root/models"
for name in "${names[@]}"; do
    install -m 0644 "$models/$name" "$root/models/$name"
done
note "installed: $root/venv, ${#names[@]} model(s) in $root/models"
