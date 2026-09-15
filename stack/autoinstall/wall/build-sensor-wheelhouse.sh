#!/usr/bin/env bash
# Build the offline, hash-pinned wheelhouse that install-wall-capabilities.sh
# consumes (--wheelhouse DIR). Runs the whole resolve/download/verify cycle in a
# Docker container on the panel's own release line and ABI: Ubuntu 24.04 noble,
# CPython 3.12 (cp312), glibc 2.39, x86-64. Nothing is ever fetched on the panel.
#
#   From WSL Ubuntu (the only WSL distro here with real Docker):
#     bash stack/autoinstall/wall/build-sensor-wheelhouse.sh
#   From Windows:
#     wsl -d Ubuntu -- bash /mnt/c/Projects/MiniPC-Deployer/stack/autoinstall/wall/build-sensor-wheelhouse.sh
#
# Default mode is REPRODUCE: the committed requirements.lock is the input and
# pip downloads under --require-hashes, so a second run on another machine
# either produces byte-identical wheels or fails. --refresh re-resolves from
# sensors/requirements.txt and rewrites the lock; that is the only mode that
# can change what gets installed, and its output must be reviewed.
#
# The wheels are build output and are NOT committed: only requirements.lock is.
set -euo pipefail

fail() { echo "[wheelhouse] $*" >&2; exit 1; }
note() { echo "[wheelhouse] $*"; }

# Ubuntu 24.04 LTS (noble) pinned by digest. What this freezes is the rootfs the
# wheels are SELECTED against: CPython 3.12 (so cp312 tags) and glibc 2.39 (so
# the manylinux_2_28 floor). What it does NOT freeze is the apt archive the two
# build packages come from or the pip inside them - so the build deliberately
# uses the image's own pip and never upgrades it, and the wheel identity, not
# the tool version, is what the committed hashes hold fixed.
BASE_IMAGE=${WALL_WHEELHOUSE_IMAGE:-ubuntu:24.04@sha256:224a1869083a311ef3f13648a154ba79832fbef6364d31493642ca03082da254}

here=$(cd "$(dirname "$0")" && pwd)
repo_root=$(cd "$here/../../.." && pwd)
lock_committed="$here/sensor-wheelhouse/requirements.lock"

# ---------------------------------------------------------------- inner half
if [ "${1:-}" = "--inner" ]; then
    shift
    mode=$1
    skip_sensor_import=${2:-0}
    export DEBIAN_FRONTEND=noninteractive
    note "container: $(. /etc/os-release && echo "$PRETTY_NAME") $(uname -m)"
    # The base image is multi-arch. On an ARM host without --platform the whole
    # build succeeds - resolve, lock, install, import - and produces aarch64
    # wheels with a perfectly valid lock that the x86-64 panel cannot install.
    # The failure has to happen here, not on the panel.
    [ "$(uname -m)" = "x86_64" ] || fail "container is $(uname -m), not x86_64; the panel is x86-64"
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends python3-venv python3-pip ca-certificates >/dev/null
    python3 --version
    # cp312 is the wheel tag the panel's 3.12.3 accepts; the patch level does not
    # enter wheel selection, so the minor version is what has to match and is
    # what is enforced. The full version is printed above for the build record.
    [ "$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')" = "3.12" ] \
        || fail "base image is not CPython 3.12; it must match the panel"

    # No pip upgrade: an unpinned self-upgrade from the network would be the one
    # unbounded input in this container. The image's pip is fixed by the digest.
    python3 -m venv /tmp/builder
    /tmp/builder/bin/pip --version

    mkdir -p /out/wheelhouse
    rm -f /out/wheelhouse/*.whl /out/wheelhouse/*.tar.gz

    if [ "$mode" = refresh ]; then
        note "refresh: resolving from requirements.txt (the lock will be rewritten)"
        /tmp/builder/bin/pip download --no-cache-dir --only-binary=:all: \
            --dest /out/wheelhouse -r /work/requirements.txt
        /tmp/builder/bin/python /work/write-lock.py /out/wheelhouse /out/wheelhouse/requirements.lock
    else
        [ -f /work/requirements.lock ] || fail "no committed lock; run with --refresh first"
        note "reproduce: downloading exactly what the committed lock pins"
        cp /work/requirements.lock /out/wheelhouse/requirements.lock
        /tmp/builder/bin/pip download --no-cache-dir --only-binary=:all: --require-hashes \
            --dest /out/wheelhouse -r /out/wheelhouse/requirements.lock
    fi

    note "--- lock -------------------------------------------------------"
    cat /out/wheelhouse/requirements.lock
    note "----------------------------------------------------------------"

    # The same check the installer runs on the panel, with the same authenticity
    # anchor in reproduce mode (in refresh mode the lock is the thing being made).
    if [ "$mode" = refresh ]; then
        python3 /work/check-wheelhouse-lock.py /out/wheelhouse
    else
        python3 /work/check-wheelhouse-lock.py --expect /work/requirements.lock /out/wheelhouse
    fi

    # Verify the installer's exact install line against the built wheelhouse.
    note "verifying the panel's install command in a fresh venv"
    rm -rf /tmp/verify
    python3 -m venv /tmp/verify
    /tmp/verify/bin/pip install --no-index --only-binary=:all: --require-hashes \
        --find-links /out/wheelhouse -r /out/wheelhouse/requirements.lock
    # The installer's own smoke test, verbatim.
    /tmp/verify/bin/python -c 'import numpy, PIL, cryptography, onnxruntime, dbus_next'
    /tmp/verify/bin/python - <<'PY'
import cryptography, dbus_next, numpy, onnxruntime, PIL
print("[wheelhouse] imports: numpy %s, Pillow %s, cryptography %s, onnxruntime %s, dbus-next %s"
      % (numpy.__version__, PIL.__version__, cryptography.__version__,
         onnxruntime.__version__, dbus_next.__version__ if hasattr(dbus_next, "__version__") else "0.2.3"))
PY

    if [ -d /sensors-src/sensors ]; then
        note "importing the sensor service itself out of the verified venv"
        ( cd /sensors-src && /tmp/verify/bin/python -c '
import importlib
for module in ("sensors.config", "sensors.detection", "sensors.identity",
               "sensors.bluetooth", "sensors.face_core", "sensors.service"):
    importlib.import_module(module)
    print("[wheelhouse] import ok:", module)
' )
    elif [ "$skip_sensor_import" = 1 ]; then
        note "--skip-sensor-import: the sensors.* import check was NOT performed"
    else
        fail "no sensor source mounted; pass --sensors-src DIR (OfficeWallNaglight) or --skip-sensor-import"
    fi

    ( cd /out && tar czf wall-sensor-wheelhouse.tar.gz wheelhouse )
    note "wheelhouse size: $(du -sh /out/wheelhouse | cut -f1) ($(ls /out/wheelhouse/*.whl | wc -l) wheels)"
    note "tarball size:    $(du -sh /out/wall-sensor-wheelhouse.tar.gz | cut -f1)"
    exit 0
fi

# ---------------------------------------------------------------- outer half
mode=reproduce
skip_sensor_import=0
out_dir=${WALL_WHEELHOUSE_OUT:-/var/tmp/wall-sensor-wheelhouse}
requirements=${WALL_SENSOR_REQUIREMENTS:-}
sensors_src=${WALL_SENSOR_SRC:-}
while [ "$#" -gt 0 ]; do
    case "$1" in
        --refresh) mode=refresh; shift ;;
        --skip-sensor-import) skip_sensor_import=1; shift ;;
        --out) [ "$#" -ge 2 ] || fail "--out needs a path"; out_dir=$2; shift 2 ;;
        --requirements) [ "$#" -ge 2 ] || fail "--requirements needs a path"; requirements=$2; shift 2 ;;
        --sensors-src) [ "$#" -ge 2 ] || fail "--sensors-src needs a path"; sensors_src=$2; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) fail "usage: $0 [--refresh] [--out DIR] [--requirements FILE] [--sensors-src DIR] [--skip-sensor-import]" ;;
    esac
done

command -v docker >/dev/null || fail "no docker; run this inside 'wsl -d Ubuntu' (the default distro is a podman alias that mis-tags)"
docker info >/dev/null 2>&1 || fail "docker daemon unreachable"

# Default sensor source: OfficeWallNaglight beside this repo.
if [ -z "$sensors_src" ]; then
    for candidate in "$repo_root/../OfficeWallNaglight" /mnt/c/Projects/OfficeWallNaglight; do
        if [ -d "$candidate/sensors" ]; then sensors_src=$(cd "$candidate" && pwd); break; fi
    done
fi
if [ -z "$requirements" ] && [ -n "$sensors_src" ]; then
    requirements="$sensors_src/sensors/requirements.txt"
fi
[ "$mode" != refresh ] || [ -f "$requirements" ] || fail "--refresh needs sensors/requirements.txt (--requirements PATH)"

# Stage the build inputs so the container never mounts a repo writable.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp "$here/check-wheelhouse-lock.py" "$here/write-lock.py" "$work/"
if [ -f "$requirements" ]; then cp "$requirements" "$work/requirements.txt"; fi
if [ -f "$lock_committed" ]; then cp "$lock_committed" "$work/requirements.lock"; fi

mkdir -p "$out_dir"
out_dir=$(cd "$out_dir" && pwd)
note "mode=$mode  base=$BASE_IMAGE"
note "output -> $out_dir  (wheels are build artefacts and are not committed)"

docker pull -q --platform linux/amd64 "$BASE_IMAGE" >/dev/null
mount_args=(--platform linux/amd64 -v "$work:/work:ro" -v "$out_dir:/out")
if [ -n "$sensors_src" ]; then mount_args+=(-v "$sensors_src:/sensors-src:ro"); fi
docker run --rm "${mount_args[@]}" -v "$here/build-sensor-wheelhouse.sh:/build.sh:ro" \
    "$BASE_IMAGE" bash /build.sh --inner "$mode" "$skip_sensor_import"

if [ "$mode" = refresh ]; then
    mkdir -p "$(dirname "$lock_committed")"
    cp "$out_dir/wheelhouse/requirements.lock" "$lock_committed"
    note "rewrote $lock_committed - review the diff before committing"
fi

note "done. Copy $out_dir/wheelhouse to removable media and pass it to"
note "install-wall-capabilities.sh --wheelhouse <that directory>."
