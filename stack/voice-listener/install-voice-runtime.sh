#!/usr/bin/env bash
# install-voice-runtime.sh - the voice listener's runtime on the hub (SR-168):
# sherpa-onnx in /opt/homehub-voice/venv and the pinned models in
# /opt/homehub-voice/models. Nothing is fetched from anywhere by this script.
#
#   sudo bash install-voice-runtime.sh --wheelhouse DIR --models DIR
#   sudo bash install-voice-runtime.sh --models-only --models DIR    (the venv is left as it is)
#
# THE WHEELS are the three in requirements.lock (sherpa-onnx 2.0.1, its core,
# numpy), for CPython 3.12 on x86-64 Linux: the hub. They are downloaded on the
# dev PC, never on the hub, and copied over (README, "The runtime"). pip runs
# with no index, binary wheels only and hashes required, so a wheel that is
# not the pinned bytes is refused. The venv does NOT see the system packages:
# the listener needs nothing but these three.
#
# THE MODELS are not in this repository. --models DIR holds the two archives
# exactly as their publisher released them (models.manifest.json names them
# and where they come from). Each archive is checked against its pinned
# SHA-256 before it is opened; only the files the manifest lists are taken out
# of it, and each of those is checked against its own pin. The listener checks
# them again at every start.
#
# Both are built beside the live ones and swapped in whole; the previous set
# comes back if anything fails. Creating a venv needs python3.12-venv.
set -euo pipefail

fail() { echo "[voice-runtime] $*" >&2; exit 1; }
note() { echo "[voice-runtime] $*"; }

here=$(cd "$(dirname "$0")" && pwd)
root=${VOICE_ROOT:-/opt/homehub-voice}
lock="$here/requirements.lock"
manifest="$here/models.manifest.json"
wheelhouse='' models='' venv=yes
while [ "$#" -gt 0 ]; do
    case $1 in
        --wheelhouse) [ "$#" -ge 2 ] || fail "--wheelhouse needs a path"; wheelhouse=$2; shift 2 ;;
        --models) [ "$#" -ge 2 ] || fail "--models needs a path"; models=$2; shift 2 ;;
        --models-only) venv=no; shift ;;
        *) fail "usage: $0 [--models-only] --wheelhouse DIR --models DIR" ;;
    esac
done
# VOICE_ROOT is for trying the installer somewhere harmless; the real root needs root.
[ "$(id -u)" = 0 ] || [ -n "${VOICE_ROOT:-}" ] || fail "run as root"
[ -d "$models" ] || fail "--models DIR is required"
[ -f "$lock" ] || fail "the lock is missing: $lock"
[ -f "$manifest" ] || fail "the manifest is missing: $manifest"
if [ "$venv" = yes ]; then
    [ -d "$wheelhouse" ] || fail "--wheelhouse DIR is required"
    python3 -c 'import ensurepip' 2>/dev/null \
        || fail "python3.12-venv is not installed (apt-get install --no-install-recommends python3.12-venv)"
fi

install -d -m 0755 "$root"

# ── the models: every archive verified before it is opened, every file after ──
# Python's own tar and bzip2 readers do the work, so the hub needs no bzip2
# program. A member's bytes are copied to a path built here from the manifest:
# no path stored in an archive is ever followed.
stage=$(mktemp -d "$root/.models.XXXXXX")
trap 'rm -rf "$stage"' EXIT
count=$(python3 - "$manifest" "$models" "$stage" <<'PY'
import hashlib, json, os, sys, tarfile

manifest, source, stage = sys.argv[1:4]


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


doc = json.load(open(manifest, encoding="utf-8"))
if doc.get("schema") != "voice-models/1":
    sys.exit("the manifest is not schema voice-models/1")
count = 0
for model in doc["models"]:
    name = model["archive"]["name"]
    archive = os.path.join(source, name)
    if os.path.basename(name) != name or not os.path.isfile(archive):
        sys.exit("archive %s is missing" % name)
    if os.path.getsize(archive) != model["archive"]["size"] or sha256(archive) != model["archive"]["sha256"]:
        sys.exit("archive %s is not the pinned bytes" % name)
    with tarfile.open(archive, "r:bz2") as tar:
        for pinned in model["files"]:
            parts = pinned["name"].split("/")
            if any(part in ("", ".", "..") for part in parts) or "/" in model["folder"]:
                sys.exit("the manifest names a path outside the model folder")
            try:
                member = tar.getmember(model["folder"] + "/" + pinned["name"])
            except KeyError:
                sys.exit("%s is not in %s" % (pinned["name"], name))
            if not member.isfile():
                sys.exit("%s is not a plain file in %s" % (pinned["name"], name))
            target = os.path.join(stage, model["folder"], *parts)
            os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
            with tar.extractfile(member) as reader, open(target, "wb") as writer:
                for block in iter(lambda: reader.read(1 << 20), b""):
                    writer.write(block)
            if os.path.getsize(target) != pinned["size"] or sha256(target) != pinned["sha256"]:
                sys.exit("%s is not the pinned bytes" % pinned["name"])
            os.chmod(target, 0o644)
            count += 1
print(count)
PY
) || fail "the models were refused (see the line above); nothing was changed"

# ── the venv, built beside the live one and swapped in whole ──
if [ "$venv" = no ]; then
    note "venv left as it is (--models-only)"
elif [ -x "$root/venv/bin/python" ] && cmp -s "$lock" "$root/requirements.lock" \
        && "$root/venv/bin/python" -c 'import numpy, sherpa_onnx' >/dev/null 2>&1; then
    note "venv already matches the lock"
else
    # A venv records its own path in its scripts, so it is built at the final
    # path with the old one set aside, and the old one comes back on any failure.
    rm -rf "$root/venv.previous"
    [ ! -e "$root/venv" ] || mv -T "$root/venv" "$root/venv.previous"
    restore() {
        rm -rf "$root/venv"
        [ ! -e "$root/venv.previous" ] || mv -T "$root/venv.previous" "$root/venv"
        echo "[voice-runtime] the new venv failed; the previous one is back" >&2
    }
    trap 'restore; rm -rf "$stage"' ERR
    python3 -m venv "$root/venv"
    "$root/venv/bin/pip" install -q --no-index --only-binary=:all: --require-hashes --no-deps \
        --find-links "$wheelhouse" -r "$lock"
    "$root/venv/bin/python" -c 'import numpy, sherpa_onnx; print("[voice-runtime] sherpa-onnx", sherpa_onnx.__version__, "numpy", numpy.__version__)'
    trap - ERR
    chmod 0755 "$root/venv"
    install -m 0644 "$lock" "$root/requirements.lock"
    rm -rf "$root/venv.previous"
fi

# ── swap the models in whole ──
chmod 0755 "$stage"
rm -rf "$root/models.previous"
[ ! -e "$root/models" ] || mv -T "$root/models" "$root/models.previous"
if ! mv -T "$stage" "$root/models"; then
    [ ! -e "$root/models.previous" ] || mv -T "$root/models.previous" "$root/models"
    fail "could not swap the models in; the previous set is back"
fi
trap - EXIT
rm -rf "$root/models.previous"
note "installed: $count model file(s) in $root/models; the venv is $root/venv"
