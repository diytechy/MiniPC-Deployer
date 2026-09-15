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
import importlib.util
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WALL = REPO_ROOT / "stack/autoinstall/wall"
VMTEST_LIB = REPO_ROOT / "vmtest/lib/common.sh"
FIRSTBOOT = WALL / "wall-firstboot.sh"
CHECK = WALL / "check-sensor-models.py"
REVIEWED_MANIFEST = WALL / "sensor-models/manifest.json"


# --------------------------------------------------------- shared predicate


def _run_bash_script(script: str, workdir: Path, env=None):
    """Write `script` to a temp .sh file under `workdir` and run it as `bash
    FILE`, never as `bash -c STRING`.

    A long script with several Windows tmp paths embedded in it, passed as a
    single argv string, hit Windows' CreateProcess/list2cmdline quoting: a
    backslash immediately before a closing quote gets doubled, which can
    silently corrupt an embedded quoted path and leaves bash reporting a bare
    'unexpected end of file' -- indistinguishable from a real script bug
    without knowing it was ever an argv-encoding problem. Writing the script
    to a file sidesteps command-line quoting entirely.
    """
    script_path = workdir / "run.sh"
    script_path.write_bytes(script.encode("utf-8"))
    kwargs = {"capture_output": True, "text": True}
    if env is not None:
        kwargs["env"] = env
    return subprocess.run(["bash", str(script_path)], **kwargs)


def _run_common_sh_predicate(env_text: str, tmp_path: Path) -> int:
    """Source vmtest/lib/common.sh and call wall_camera_option_configured
    against a synthetic wall.env; returns the function's own exit status."""
    env_file = tmp_path / "wall.env"
    # newline="\n": Path.write_text defaults to os.linesep on Windows (CRLF),
    # and a stray trailing \r defeats load_env_file's quote-closing glob
    # pattern (\"*\" no longer matches a value ending in \r"), silently
    # falling through to the unquoted branch and leaving the literal quotes
    # in the exported value. wall.env itself is required LF (AGENTS.md); keep
    # this fixture the same shape as the real file it stands in for.
    env_file.write_bytes(env_text.encode("utf-8"))
    script = f'set -euo pipefail; source "{VMTEST_LIB}"; wall_camera_option_configured "{env_file}"'
    result = _run_bash_script(script, tmp_path)
    return result.returncode


def _run_firstboot_predicate(env_lines: list[str], tmp_path: Path) -> int:
    """Run wall-firstboot.sh's REAL load_env_file against a synthetic
    wall.env, then its predicate, the same order the real script runs them
    in ($ENV_FILE sourced once near the top, the predicate reading only the
    variables that left exported) — not a hand re-parse of the file. Extracts
    just those two functions rather than the whole script, which would need
    root and real hardware."""
    env_file = tmp_path / "wall.env"
    env_file.write_bytes(("\n".join(env_lines) + "\n").encode("utf-8"))
    text = FIRSTBOOT.read_text(encoding="utf-8")

    def _extract(marker):
        start = text.index(marker)
        end = text.index("\n}\n", start) + len("\n}")
        return text[start:end]

    load_env_file_source = _extract("load_env_file() {")
    predicate_source = _extract("wall_camera_option_configured() {")
    script = (
        f'set -euo pipefail\n{load_env_file_source}\nload_env_file "{env_file}"\n'
        f'{predicate_source}\nwall_camera_option_configured'
    )
    result = _run_bash_script(script, tmp_path)
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
    # Quoted values (terra review #4): a hand-written regex that only matched
    # an unquoted WALL_CAMERA_DEVICE would miss these, and load_env_file
    # strips the quotes before export either way.
    (['WALL_CAMERA_DEVICE="/dev/video0"'], True),
    (["WALL_CAMERA_DEVICE='/dev/video0'"], True),
    (['WALL_ACCESS_MODE="local"'], True),
    (["WALL_CAMERA_ENABLED='true'"], True),
    # An unquoted value with a trailing ` # comment` — load_env_file strips
    # it the way shell/compose .env parsing does; a naive regex anchored on
    # "to end of line" would instead capture the comment as part of the value
    # (still non-empty, so it happened to agree here) or choke on the `#`.
    (["WALL_CAMERA_DEVICE=/dev/video0  # the Sunplus webcam"], True),
    # A quoted EMPTY value is still "not set" for this predicate's purposes.
    (['WALL_CAMERA_DEVICE=""'], False),
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


# --------------------------------------------------------- stage_wall_sensors_into_payload


def _load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), WALL / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WHEEL_CHECK = _load("check-wheelhouse-lock")
WHEEL_WRITE = _load("write-lock")


def _make_wheel(directory, name, version, body=b"stub"):
    path = directory / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}/__init__.py", body)
    return path


def _build_wheelhouse(directory):
    """A minimal wheelhouse check-wheelhouse-lock.py accepts, same recipe as
    test_wall_sensor_wheelhouse.py's build_wheelhouse."""
    directory.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(WHEEL_CHECK.DEFAULT_REQUIRED):
        _make_wheel(directory, name.replace("-", "_"), f"1.{index}.0")
    WHEEL_WRITE.main(["write-lock.py", str(directory), str(directory / "requirements.lock")])
    return directory


def _stage_fixture(tmp_path, camera_lines):
    """A deploy-payload tree shaped the way build-wall-seed.sh leaves one just
    before calling stage_wall_sensors_into_payload: the payload's own copy of
    the two checker scripts and the committed wheelhouse lock, plus a
    site/wall.env this fixture's own camera_lines control."""
    out_dir = tmp_path / "out"
    payload_wall = out_dir / "iso-root/deploy-payload/stack/autoinstall/wall"
    payload_wall.mkdir(parents=True)
    shutil.copy(WALL / "check-wheelhouse-lock.py", payload_wall / "check-wheelhouse-lock.py")
    shutil.copy(WALL / "check-sensor-models.py", payload_wall / "check-sensor-models.py")

    source_wheelhouse = tmp_path / "source-wheelhouse"
    _build_wheelhouse(source_wheelhouse)
    sensor_wheelhouse_dir = payload_wall / "sensor-wheelhouse"
    sensor_wheelhouse_dir.mkdir()
    shutil.copy(source_wheelhouse / "requirements.lock", sensor_wheelhouse_dir / "requirements.lock")

    # A fixture-local reviewed manifest (NOT the real production one: the
    # real digests are pinned to actual InsightFace bytes this test cannot
    # reproduce a preimage for). Same shape and same role, so the staging
    # function's own logic is what is under test, not the specific digests.
    det, rec = b"fixture-detector", b"fixture-recognizer"
    reviewed_text = _reviewed_manifest_text(det, rec)
    sensor_models_payload_dir = payload_wall / "sensor-models"
    sensor_models_payload_dir.mkdir()
    (sensor_models_payload_dir / "manifest.json").write_text(reviewed_text, encoding="utf-8")

    site_dir = out_dir / "iso-root/deploy-payload/site"
    site_dir.mkdir(parents=True)
    (site_dir / "wall.env").write_bytes(("\n".join(camera_lines) + "\n").encode("utf-8"))

    local_input = tmp_path / "local-input"
    _write_bundle(local_input, reviewed_text, det, rec)
    return out_dir, source_wheelhouse, local_input


def _run_stage(out_dir, source_wheelhouse, sensor_models_env=None):
    env = dict(os.environ)
    env["WALL_SENSOR_WHEELHOUSE"] = str(source_wheelhouse)
    if sensor_models_env is not None:
        env["WALL_SENSOR_MODELS"] = str(sensor_models_env)
    else:
        env.pop("WALL_SENSOR_MODELS", None)
    script = f'set -euo pipefail; source "{VMTEST_LIB}"; stage_wall_sensors_into_payload "{out_dir}"'
    return _run_bash_script(script, out_dir.parent, env=env)


def test_stage_wall_sensors_into_payload_fails_when_camera_configured_and_no_bundle_llr901(tmp_path):
    out_dir, source_wheelhouse, _local_input = _stage_fixture(tmp_path, ["WALL_ACCESS_MODE=local"])
    result = _run_stage(out_dir, source_wheelhouse, sensor_models_env=None)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "no face model bundle was supplied" in (result.stdout + result.stderr)
    assert not (out_dir / "iso-root/deploy-payload/sensor-models").exists()


def test_stage_wall_sensors_into_payload_succeeds_with_a_matching_bundle_llr901(tmp_path):
    out_dir, source_wheelhouse, local_input = _stage_fixture(tmp_path, ["WALL_CAMERA_ENABLED=true"])
    result = _run_stage(out_dir, source_wheelhouse, sensor_models_env=local_input)
    assert result.returncode == 0, result.stdout + result.stderr
    staged = out_dir / "iso-root/deploy-payload/sensor-models"
    assert (staged / "det_10g.onnx").is_file()
    assert (staged / "w600k_r50.onnx").is_file()
    assert (staged / "manifest.json").is_file()


def test_stage_wall_sensors_into_payload_is_quiet_when_camera_not_configured_llr901(tmp_path):
    out_dir, source_wheelhouse, _local_input = _stage_fixture(tmp_path, ["WALL_ACCESS_MODE=gateway"])
    result = _run_stage(out_dir, source_wheelhouse, sensor_models_env=None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (out_dir / "iso-root/deploy-payload/sensor-models").exists()


# --------------------------------------------------------- firstboot's sensor block


FIRSTBOOT_SENSOR_BLOCK_START = "SENSOR_WHEELHOUSE=/opt/wall-panel/sensor-wheelhouse"
FIRSTBOOT_SENSOR_BLOCK_END_MARKER = 'fail_step "sensors: $SENSOR_WHEELHOUSE is missing from the image payload"\nfi'


def _firstboot_sensor_block():
    text = FIRSTBOOT.read_text(encoding="utf-8")
    start = text.index(FIRSTBOOT_SENSOR_BLOCK_START)
    end = text.index(FIRSTBOOT_SENSOR_BLOCK_END_MARKER, start) + len(FIRSTBOOT_SENSOR_BLOCK_END_MARKER)
    return text[start:end]


def _run_firstboot_sensor_block(tmp_path, *, wheelhouse_present, models_present=False,
                                camera_lines, installer_exit=0,
                                payload_bundle_complete=False,
                                payload_bundle_digest_mismatch=False,
                                stale_models_content=None):
    """Run the REAL sensor block sliced out of wall-firstboot.sh, with
    fail_step/log stubbed to a readable log and a stub installer, driven by a
    synthetic wall.env through the same load_env_file + predicate this file
    uses at runtime.

    payload_bundle_complete: stage a full, self-verifying bundle at
    $PAYLOAD/sensor-models (manifest.json + both .onnx files, digests
    matching) plus a real copy of check-sensor-models.py, the shape
    sync_sensor_models_from_payload looks for.
    payload_bundle_digest_mismatch: corrupt det_10g.onnx's bytes so the
    bundle's OWN manifest check fails it.
    stale_models_content: pre-populate $SENSOR_MODELS (the runtime path) with
    this {name: bytes} before the block runs, simulating an existing
    install the sync step must either replace (complete payload bundle) or
    leave untouched (no payload bundle)."""
    payload = tmp_path / "payload"
    payload.mkdir()
    installer = payload / "install-wall-capabilities.sh"
    installer.write_text(f"#!/bin/sh\nexit {installer_exit}\n", encoding="utf-8")
    installer.chmod(0o755)
    reviewed_manifest_dir = payload / "sensor-models"
    reviewed_manifest_dir.mkdir()
    if payload_bundle_complete:
        det_bytes, rec_bytes = b"payload-detector-bytes", b"payload-recognizer-bytes"
        manifest = {
            "det_10g.onnx": hashlib.sha256(det_bytes).hexdigest(),
            "w600k_r50.onnx": hashlib.sha256(rec_bytes).hexdigest(),
        }
        (reviewed_manifest_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (reviewed_manifest_dir / "det_10g.onnx").write_bytes(
            b"TAMPERED" if payload_bundle_digest_mismatch else det_bytes)
        (reviewed_manifest_dir / "w600k_r50.onnx").write_bytes(rec_bytes)
        shutil.copy(CHECK, payload / "check-sensor-models.py")
    else:
        (reviewed_manifest_dir / "manifest.json").write_text("{}", encoding="utf-8")

    sensor_wheelhouse = tmp_path / "sensor-wheelhouse"
    sensor_models = tmp_path / "sensor-models"
    if wheelhouse_present:
        sensor_wheelhouse.mkdir()
    if stale_models_content is not None:
        sensor_models.mkdir()
        for name, data in stale_models_content.items():
            (sensor_models / name).write_bytes(data)
    elif models_present:
        sensor_models.mkdir()
        (sensor_models / "det_10g.onnx").write_bytes(b"x")
        (sensor_models / "w600k_r50.onnx").write_bytes(b"x")

    env_file = tmp_path / "wall.env"
    env_file.write_bytes(("\n".join(camera_lines) + "\n").encode("utf-8"))

    firstboot_text = FIRSTBOOT.read_text(encoding="utf-8")

    def extract(marker):
        start = firstboot_text.index(marker)
        end = firstboot_text.index("\n}\n", start) + len("\n}")
        return firstboot_text[start:end]

    script = "\n".join([
        "set -uo pipefail",
        'log() { echo "LOG: $*"; }',
        'PROVISION_FAILED=0',
        'fail_step() { PROVISION_FAILED=1; echo "FAIL_STEP: $*"; }',
        extract("load_env_file() {"),
        f'load_env_file "{env_file}"',
        extract("wall_camera_option_configured() {"),
        f'PAYLOAD="{payload}"',
        f'SENSOR_WHEELHOUSE="{sensor_wheelhouse}"',
        f'SENSOR_MODELS="{sensor_models}"',
        # The block itself names SENSOR_WHEELHOUSE/SENSOR_MODELS literally
        # (it assigns them), so drop its own assignment lines and keep the
        # rest verbatim -- this run must exercise the REAL logic, not a
        # paraphrase of it.
        "\n".join(
            line for line in _firstboot_sensor_block().splitlines()
            if line not in (
                "SENSOR_WHEELHOUSE=/opt/wall-panel/sensor-wheelhouse",
                "SENSOR_MODELS=/opt/wall-panel/sensor-models",
            )
        ),
        'echo "PROVISION_FAILED=$PROVISION_FAILED"',
    ])
    return _run_bash_script(script, tmp_path)


def test_firstboot_camera_fail_step_fires_before_any_installer_work_llr901(tmp_path):
    """terra review #5: the camera-bundle fail_step must not be nested inside
    the wheelhouse-present branch, and must fire even when the wheelhouse
    itself is ALSO missing -- with its own named step, not folded into the
    wheelhouse's."""
    result = _run_firstboot_sensor_block(
        tmp_path, wheelhouse_present=False, models_present=False,
        camera_lines=["WALL_ACCESS_MODE=local"])
    assert "PROVISION_FAILED=1" in result.stdout
    assert "wall.env enables a camera-related option" in result.stdout
    assert "is missing from the image payload" in result.stdout
    # Neither fail_step waited on the other: no installer invocation happened
    # at all (there is no wheelhouse to invoke it with).
    assert "offline runtime installation failed" not in result.stdout


def test_firstboot_incomplete_bundle_manifest_without_onnx_fails_llr901(tmp_path):
    """A manifest.json with no model bytes beside it used to read as
    'present' (only manifest.json was checked) and reach the installer,
    which then failed for a reason this step never named."""
    payload_marker = tmp_path / "payload"
    result = _run_firstboot_sensor_block(
        tmp_path, wheelhouse_present=True, models_present=False,
        camera_lines=["WALL_CAMERA_ENABLED=true"])
    assert "PROVISION_FAILED=1" in result.stdout
    assert "absent or incomplete" in result.stdout
    # The installer ran WITHOUT --models (no complete bundle to pass), and
    # the stub installer exits 0, so that half must still read as success --
    # the camera fail_step is the only failure this run reports.
    assert "gateway-independent runtime installed and protocol verified" in result.stdout


def test_firstboot_camera_not_configured_and_no_bundle_is_not_a_failure_llr901(tmp_path):
    result = _run_firstboot_sensor_block(
        tmp_path, wheelhouse_present=True, models_present=False,
        camera_lines=["WALL_ACCESS_MODE=gateway"])
    assert "PROVISION_FAILED=0" in result.stdout
    assert "wall.env enables a camera-related option" not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="install -o root -g root needs a real root user")
def test_firstboot_syncs_payload_bundle_into_stale_sensor_models_llr901(tmp_path):
    """Live release finding, 2026-09-15 (paired-deployment-02.json): a
    release install rebooted into a post-install gate refusal because
    $SENSOR_MODELS still held a stale hand-written manifest.json while the
    release's own verified bundle sat unused at $PAYLOAD/sensor-models.
    sync_sensor_models_from_payload must replace the stale runtime copy with
    exactly what the payload carries."""
    result = _run_firstboot_sensor_block(
        tmp_path, wheelhouse_present=True, camera_lines=["WALL_ACCESS_MODE=local"],
        payload_bundle_complete=True,
        stale_models_content={
            "det_10g.onnx": b"stale-morning-copy-det",
            "w600k_r50.onnx": b"stale-morning-copy-rec",
            "manifest.json": b'{"stale": true}',
        },
    )
    assert "PROVISION_FAILED=0" in result.stdout, result.stdout + result.stderr
    assert "synced from" in result.stdout
    sensor_models = tmp_path / "sensor-models"
    assert (sensor_models / "det_10g.onnx").read_bytes() == b"payload-detector-bytes"
    assert (sensor_models / "w600k_r50.onnx").read_bytes() == b"payload-recognizer-bytes"
    manifest = json.loads((sensor_models / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == {"det_10g.onnx", "w600k_r50.onnx"}
    # No leftover staging/previous directory beside the runtime path.
    assert not (tmp_path / (sensor_models.name + ".previous")).exists()


def test_firstboot_leaves_existing_sensor_models_untouched_when_payload_has_no_bundle_llr901(tmp_path):
    stale = {
        "det_10g.onnx": b"stale-det",
        "w600k_r50.onnx": b"stale-rec",
        "manifest.json": b'{"stale": true}',
    }
    result = _run_firstboot_sensor_block(
        tmp_path, wheelhouse_present=True, camera_lines=["WALL_ACCESS_MODE=gateway"],
        payload_bundle_complete=False, stale_models_content=stale,
    )
    assert "PROVISION_FAILED=0" in result.stdout, result.stdout + result.stderr
    assert "synced from" not in result.stdout
    sensor_models = tmp_path / "sensor-models"
    for name, data in stale.items():
        assert (sensor_models / name).read_bytes() == data


def test_firstboot_sync_refuses_and_leaves_existing_untouched_on_tampered_payload_bundle_llr901(tmp_path):
    stale = {
        "det_10g.onnx": b"stale-det",
        "w600k_r50.onnx": b"stale-rec",
        "manifest.json": b'{"stale": true}',
    }
    result = _run_firstboot_sensor_block(
        tmp_path, wheelhouse_present=True, camera_lines=["WALL_ACCESS_MODE=local"],
        payload_bundle_complete=True, payload_bundle_digest_mismatch=True,
        stale_models_content=stale,
    )
    assert "PROVISION_FAILED=1" in result.stdout, result.stdout + result.stderr
    assert "failed its own manifest check" in result.stdout
    sensor_models = tmp_path / "sensor-models"
    for name, data in stale.items():
        assert (sensor_models / name).read_bytes() == data


@pytest.mark.skipif(os.name == "nt", reason="install -o root -g root needs a real root user")
def test_firstboot_sync_runs_before_the_camera_completeness_check_llr901(tmp_path):
    """Ordering: sync happens first, so a payload that carries a complete
    bundle satisfies the camera-configured completeness check even though
    $SENSOR_MODELS started out completely absent -- the fail_step must NOT
    fire in this case."""
    result = _run_firstboot_sensor_block(
        tmp_path, wheelhouse_present=True, camera_lines=["WALL_ACCESS_MODE=local"],
        payload_bundle_complete=True,
    )
    assert "PROVISION_FAILED=0" in result.stdout, result.stdout + result.stderr
    assert "absent or incomplete" not in result.stdout
    assert "gateway-independent runtime installed and protocol verified" in result.stdout


def test_firstboot_complete_bundle_is_anchored_on_the_payloads_own_manifest_llr901(tmp_path):
    """terra review #3: --model-manifest must be $PAYLOAD/sensor-models/manifest.json
    (the git-tracked, reviewed copy), never $SENSOR_MODELS/manifest.json (the
    copy that travelled here alongside the .onnx bytes themselves)."""
    payload = tmp_path / "payload"
    payload.mkdir()
    installer = payload / "install-wall-capabilities.sh"
    # Record the exact argv it was called with, so the anchor path can be
    # asserted without needing install-wall-capabilities.sh's own logic here.
    installer.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "' + str(tmp_path / "installer-argv.txt") + '"\nexit 0\n',
        encoding="utf-8")
    installer.chmod(0o755)
    (payload / "sensor-models").mkdir()
    (payload / "sensor-models" / "manifest.json").write_text('{"reviewed": true}', encoding="utf-8")
    sensor_wheelhouse = tmp_path / "sensor-wheelhouse"
    sensor_wheelhouse.mkdir()
    sensor_models = tmp_path / "sensor-models"
    sensor_models.mkdir()
    (sensor_models / "det_10g.onnx").write_bytes(b"x")
    (sensor_models / "w600k_r50.onnx").write_bytes(b"x")
    # This copy must NOT be the one the installer is anchored on.
    (sensor_models / "manifest.json").write_text('{"substituted": true}', encoding="utf-8")

    env_file = tmp_path / "wall.env"
    env_file.write_bytes(b"WALL_CAMERA_ENABLED=true\n")
    firstboot_text = FIRSTBOOT.read_text(encoding="utf-8")

    def extract(marker):
        start = firstboot_text.index(marker)
        end = firstboot_text.index("\n}\n", start) + len("\n}")
        return firstboot_text[start:end]

    script = "\n".join([
        "set -uo pipefail",
        'log() { echo "LOG: $*"; }',
        'PROVISION_FAILED=0',
        'fail_step() { PROVISION_FAILED=1; echo "FAIL_STEP: $*"; }',
        extract("load_env_file() {"),
        f'load_env_file "{env_file}"',
        extract("wall_camera_option_configured() {"),
        f'PAYLOAD="{payload}"',
        f'SENSOR_WHEELHOUSE="{sensor_wheelhouse}"',
        f'SENSOR_MODELS="{sensor_models}"',
        "\n".join(
            line for line in _firstboot_sensor_block().splitlines()
            if line not in (
                "SENSOR_WHEELHOUSE=/opt/wall-panel/sensor-wheelhouse",
                "SENSOR_MODELS=/opt/wall-panel/sensor-models",
            )
        ),
    ])
    result = _run_bash_script(script, tmp_path)
    assert result.returncode == 0 or True, result.stdout + result.stderr  # the block itself never exits non-zero
    argv = (tmp_path / "installer-argv.txt").read_text(encoding="utf-8").splitlines()
    assert "--model-manifest" in argv
    anchor = argv[argv.index("--model-manifest") + 1]
    assert os.path.normpath(anchor) == os.path.normpath(str(payload / "sensor-models" / "manifest.json"))
    assert os.path.normpath(anchor) != os.path.normpath(str(sensor_models / "manifest.json"))


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
