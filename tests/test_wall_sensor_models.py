"""LLR-901: the face model bundle is staged whenever any camera-related
option is configured (Item M, 2026-09-15 Owner ruling).

Covers the two hand-synchronised copies of the `wall_camera_option_configured`
predicate (vmtest/lib/common.sh at build time, wall-firstboot.sh at boot
time) and check-sensor-models.py, the authenticity check
stage_wall_sensors_into_payload runs on a local WALL_SENSOR_MODELS input
before staging it, mirroring check-wheelhouse-lock.py's --expect pattern.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parents[1] / "stack/autoinstall/wall"
VMTEST_LIB = Path(__file__).resolve().parents[1] / "vmtest/lib/common.sh"
FIRSTBOOT = WALL / "wall-firstboot.sh"
CHECK = WALL / "check-sensor-models.py"
REVIEWED_MANIFEST = WALL / "sensor-models/manifest.json"


# --------------------------------------------------------- shared predicate


def _run_common_sh_predicate(env_text: str, tmp_path: Path) -> int:
    """Source vmtest/lib/common.sh and call wall_camera_option_configured
    against a synthetic wall.env; returns the function's own exit status."""
    env_file = tmp_path / "wall.env"
    env_file.write_text(env_text, encoding="utf-8")
    script = f'set -euo pipefail; source "{VMTEST_LIB}"; wall_camera_option_configured "{env_file}"'
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return result.returncode


def _run_firstboot_predicate(env_lines: list[str], tmp_path: Path) -> int:
    """Source only the predicate out of wall-firstboot.sh against a state
    matching what load_env_file leaves on the real panel: $ENV_FILE holding
    the raw wall.env text (the WALL_CAMERA_DEVICE branch greps it directly)
    AND the same lines exported as shell variables (the other two branches
    read $WALL_ACCESS_MODE/$WALL_CAMERA_ENABLED, which load_env_file exports).
    Does not run the whole script, which would need root and real hardware."""
    env_file = tmp_path / "wall.env"
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    exports = "\n".join(f'export {line}' if line and not line.startswith("#") else "" for line in env_lines)
    text = FIRSTBOOT.read_text(encoding="utf-8")
    start = text.index("wall_camera_option_configured() {")
    end = text.index("\n}\n", start) + len("\n}")
    function_source = text[start:end]
    script = f'set -euo pipefail\nENV_FILE="{env_file}"\n{exports}\n{function_source}\nwall_camera_option_configured'
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return result.returncode


@pytest.mark.parametrize("predicate_fn", ["common_sh", "firstboot"])
@pytest.mark.parametrize("env_lines,expect_configured", [
    (["WALL_ACCESS_MODE=local"], True),
    (["WALL_ACCESS_MODE=gateway"], False),
    (["WALL_CAMERA_ENABLED=true"], True),
    (["WALL_CAMERA_ENABLED=TRUE"], True),
    (["WALL_CAMERA_ENABLED=1"], True),
    (["WALL_CAMERA_ENABLED=false"], False),
    (["WALL_CAMERA_DEVICE=/dev/video0"], True),
    (["WALL_CAMERA_DEVICE="], False),
    (["# WALL_CAMERA_DEVICE=/dev/video0"], False),
    ([], False),
])
def test_camera_option_predicate_llr901(predicate_fn, env_lines, expect_configured, tmp_path):
    if predicate_fn == "common_sh":
        env_text = "\n".join(env_lines) + "\n"
        code = _run_common_sh_predicate(env_text, tmp_path)
    else:
        code = _run_firstboot_predicate(env_lines, tmp_path)
    assert (code == 0) is expect_configured, (
        f"{predicate_fn} predicate disagreed for {env_lines!r}: exit={code}"
    )


# --------------------------------------------------------- check-sensor-models.py


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_bundle(directory: Path, manifest_text: str, det_bytes: bytes, rec_bytes: bytes) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(manifest_text, encoding="utf-8")
    (directory / "det_10g.onnx").write_bytes(det_bytes)
    (directory / "w600k_r50.onnx").write_bytes(rec_bytes)


def _reviewed_manifest_text(det_bytes: bytes, rec_bytes: bytes) -> str:
    import json
    return json.dumps({
        "det_10g.onnx": _sha256(det_bytes),
        "w600k_r50.onnx": _sha256(rec_bytes),
    })


def test_check_sensor_models_accepts_matching_bundle_llr901(tmp_path):
    det, rec = b"stub-detector", b"stub-recognizer"
    reviewed_text = _reviewed_manifest_text(det, rec)
    reviewed = tmp_path / "reviewed-manifest.json"
    reviewed.write_text(reviewed_text, encoding="utf-8")
    bundle = tmp_path / "local-input"
    _write_bundle(bundle, reviewed_text, det, rec)

    result = subprocess.run(
        ["python3", str(CHECK), "--expect", str(reviewed), str(bundle)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_check_sensor_models_rejects_digest_mismatch_llr901(tmp_path):
    det, rec = b"stub-detector", b"stub-recognizer"
    reviewed_text = _reviewed_manifest_text(det, rec)
    reviewed = tmp_path / "reviewed-manifest.json"
    reviewed.write_text(reviewed_text, encoding="utf-8")
    bundle = tmp_path / "local-input"
    # Bundle's own manifest is self-consistent with its (wrong) file bytes,
    # matching the reviewed manifest byte-for-byte only in the recognizer
    # entry — but the detector file's bytes are tampered.
    _write_bundle(bundle, reviewed_text, b"tampered-detector", rec)

    result = subprocess.run(
        ["python3", str(CHECK), "--expect", str(reviewed), str(bundle)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "does not match the reviewed" in result.stderr


def test_check_sensor_models_rejects_self_consistent_but_unreviewed_manifest_llr901(tmp_path):
    """Substituted media whose manifest.json only agrees with its OWN files
    (not the reviewed one) is refused — the same authenticity anchor
    check-wheelhouse-lock.py --expect enforces for the wheelhouse."""
    det, rec = b"stub-detector", b"stub-recognizer"
    reviewed = tmp_path / "reviewed-manifest.json"
    reviewed.write_text(_reviewed_manifest_text(det, rec), encoding="utf-8")

    other_det, other_rec = b"other-detector", b"other-recognizer"
    bundle = tmp_path / "local-input"
    _write_bundle(bundle, _reviewed_manifest_text(other_det, other_rec), other_det, other_rec)

    result = subprocess.run(
        ["python3", str(CHECK), "--expect", str(reviewed), str(bundle)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "byte-identical" in result.stderr


def test_check_sensor_models_rejects_missing_file_llr901(tmp_path):
    det, rec = b"stub-detector", b"stub-recognizer"
    reviewed_text = _reviewed_manifest_text(det, rec)
    reviewed = tmp_path / "reviewed-manifest.json"
    reviewed.write_text(reviewed_text, encoding="utf-8")
    bundle = tmp_path / "local-input"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(reviewed_text, encoding="utf-8")
    (bundle / "det_10g.onnx").write_bytes(det)
    # w600k_r50.onnx deliberately absent.

    result = subprocess.run(
        ["python3", str(CHECK), "--expect", str(reviewed), str(bundle)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "missing" in result.stderr


def test_committed_reviewed_manifest_names_exactly_the_two_pinned_models_llr901():
    """The manifest committed beside the wheelhouse lock (not the .onnx files
    themselves) is the authenticity anchor every build and every panel checks
    against; guard its shape and the digests recorded in the coordinator plan."""
    import json
    manifest = json.loads(REVIEWED_MANIFEST.read_text(encoding="utf-8"))
    assert manifest == {
        "det_10g.onnx": "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91",
        "w600k_r50.onnx": "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
    }
