# The offline sensor wheelhouse

`install-wall-capabilities.sh --wheelhouse DIR` installs the panel's sensor
virtualenv with

```
pip install --no-index --only-binary=:all: --require-hashes --find-links DIR -r DIR/requirements.lock
```

The panel never reaches an index. This directory holds the **lock**; the wheels
themselves are build output and are deliberately not committed (46 MB, mostly
`onnxruntime` and `numpy`).

## Build it

Docker lives in the `Ubuntu` WSL distro; the default distro is a Podman alias
that mis-tags images, so name the distro explicitly.

```
wsl -d Ubuntu -- bash /mnt/c/Projects/MiniPC-Deployer/stack/autoinstall/wall/build-sensor-wheelhouse.sh
```

Two modes:

| Mode | Input | Effect |
|---|---|---|
| default (*reproduce*) | the committed `requirements.lock` | `pip download --require-hashes`; a drifted or tampered wheel fails the build |
| `--refresh` | `OfficeWallNaglight/sensors/requirements.txt` | re-resolves transitive dependencies, rewrites this lock. **Review the diff.** |

The container is `ubuntu:24.04` pinned by digest, on the panel's release line
and ABI: noble, CPython 3.12 (so `cp312` wheel tags), glibc 2.39 (so the
`manylinux_2_28` floor), x86-64. The panel itself is 24.04.4 / 3.12.3, read
off it on 2026-09-13; the patch levels do not enter wheel selection, so the
build enforces the 3.12 minor version and no more. `--only-binary=:all:` means
manylinux wheels only: the build fails rather than compiling anything, and
`write-lock.py` refuses an sdist in the output.

What the digest pin does **not** freeze is the noble apt archive the two build
packages (`python3-venv`, `python3-pip`) come from. The build therefore uses the
image's own pip and never self-upgrades it, so the only network inputs that can
move a build are the PyPI artefacts — and those are exactly what the committed
hashes hold fixed.

Both modes end by proving the install: a fresh venv inside the same container
runs the installer's exact `pip` line against the produced wheelhouse, then the
installer's verbatim smoke import, then imports every `sensors.*` module out of
that venv against the real OfficeWallNaglight source. The sensor source is found
beside the repo automatically; if it is not there the build **fails** rather than
quietly skipping, unless `--skip-sensor-import` is passed.

## Output

* `/var/tmp/wall-sensor-wheelhouse/wheelhouse/` — 14 wheels + `requirements.lock`, **46 MB** (47,610,711 bytes)
* `/var/tmp/wall-sensor-wheelhouse/wall-sensor-wheelhouse.tar.gz` — **45 MB** (47,057,701 bytes)

Override with `--out DIR`. Copy the `wheelhouse/` directory to removable media
and pass that path to the installer.

## What is pinned, and why these versions

The five direct requirements are fixed by `sensors/requirements.txt`
(`numpy==1.26.4`, `Pillow==10.4.0`, `cryptography==43.0.3`, `onnxruntime==1.20.1`,
`dbus-next==0.2.3`). Nine transitive distributions come with them: `cffi` and
`pycparser` (cryptography), and `coloredlogs`, `humanfriendly`, `flatbuffers`,
`packaging`, `protobuf`, `sympy`, `mpmath` (onnxruntime).

`onnxruntime==1.20.1` is the version the sensor source was written against, and
it is kept because it is a version that publishes a `cp312` manylinux wheel for
x86-64 under the plain `onnxruntime` name. That is the constraint that matters
here: `--only-binary=:all:` means a release line without a cp312 x86-64 wheel
cannot be installed on this panel at all, and onnxruntime's own C++ build is not
something to carry into an offline panel install. It was not moved to a newer
release because nothing in the sensor source asks for one and the face pipeline
has only been exercised against 1.20.1. The selected artefact is
`onnxruntime-1.20.1-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl`;
noble's glibc 2.39 satisfies `manylinux_2_28`.

Transitive versions are whatever the resolver picked on the day of the refresh
and are then frozen by hash. They only move when somebody runs `--refresh` and
commits the resulting diff.

## Verification the panel performs

`check-wheelhouse-lock.py` runs before pip, on the panel, and refuses a
wheelhouse whose lock is not fully `==`-pinned and sha256-hashed, that names an
index or a URL, that omits any of the five distributions the installer
smoke-imports, or whose pinned distributions are not all present **at the pinned
version** as wheels in the directory. pip would catch most of that eventually;
the check reports it by name before a venv has been created.

It also runs with `--expect`, pointed at the copy of `requirements.lock` in this
directory as it was staged into the panel image. That is the authenticity
anchor, and it is the answer to the obvious attack on the whole scheme: hashes
that the media's own lock declares only prove the media is self-consistent.
Substituted media carrying a self-consistent lock of its own is refused because
its lock is not byte-identical to the reviewed one that arrived with the image.
Consequently **a lock change must be deployed to the panel before the media
built from it will install** — that ordering is the point, not an inconvenience.

## Reproducibility check

Run 1 (`--refresh`) and run 2 (default reproduce mode, separate output
directory) produced byte-identical wheels — `sha256sum` over all 14 matched.
