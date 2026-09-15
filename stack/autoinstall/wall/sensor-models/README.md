# The face model bundle (InsightFace buffalo_l detector + recognizer)

Item M, 2026-09-15 Owner ruling: the face model bundle is a **dependency of
any camera-related option**, not a separate optional package. This directory
holds only the reviewed, committed **manifest** — the authenticity anchor,
same pattern as `sensor-wheelhouse/requirements.lock`. The two model files
themselves (`det_10g.onnx`, `w600k_r50.onnx`, ~17 MB and ~166 MB from the
InsightFace `buffalo_l` v0.7 release pack) are **not committed** and this repo
**fetches nothing** to obtain them.

## What is pinned, and why

`manifest.json` pins exactly two files by sha256:

| File | sha256 |
|---|---|
| `det_10g.onnx` | `5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91` |
| `w600k_r50.onnx` | `4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43` |

These are the same two digests `install-wall-capabilities.sh` verifies on the
panel against `/opt/wall-panel/sensor-models/manifest.json` before it will
publish `/opt/wall-sensors/models`. They were cross-checked against the
Immich mirror's LFS pointers on 2026-09-15 (see the coordinator plan's Item M
ledger) — this repo does not re-derive them.

## Supplying the bundle to a build

The build fetches nothing: the operator supplies a **reviewed local input
directory** holding the two `.onnx` files plus a `manifest.json` that is
byte-identical to this one, and points `WALL_SENSOR_MODELS` at it:

```
WALL_SENSOR_MODELS=/path/to/local/sensor-models \
    bash vmtest/build-wall-seed.sh
```

`vmtest/lib/common.sh`'s `stage_wall_sensors_into_payload` runs
`check-sensor-models.py --expect stack/autoinstall/wall/sensor-models/manifest.json
WALL_SENSOR_MODELS` before staging anything: it refuses a `WALL_SENSOR_MODELS`
whose `manifest.json` is not byte-identical to this reviewed copy, and refuses
either `.onnx` file whose sha256 does not match the pinned digest. Substituted
media carrying a self-consistent manifest of its own is refused for the same
reason `check-wheelhouse-lock.py --expect` refuses one on the wheelhouse: a
lock (or manifest) that only proves the media agrees with itself proves
nothing about authenticity.

## When the bundle is required

`wall_camera_option_configured` (in `vmtest/lib/common.sh`, mirrored in
`wall-firstboot.sh`) reads the wall.env this image will boot with and treats
any of these as "a camera-related option is configured":

* `WALL_ACCESS_MODE=local`
* `WALL_CAMERA_ENABLED=true` (`TRUE`/`yes`/`1` also count)
* `WALL_CAMERA_DEVICE` explicitly set

When any of those is true and `WALL_SENSOR_MODELS` was not supplied, the
build **fails** rather than silently producing an image whose face features
can never work. Firstboot mirrors the same rule against the deployed
`/opt/wall-panel/sensor-models`: a configured camera option with no bundle on
the image payload is a `fail_step`, not a warning.

When no camera-related option is configured, an absent bundle is not an
error — motion presence, Bluetooth, and PIN all remain usable without it.
