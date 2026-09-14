"""SR-017: the panel installs its sensor venv from a reviewed offline wheelhouse.

Covers the lock validator the installer runs before pip, the lock writer the
build container runs, and the wiring between them and the committed lock.
"""
import hashlib
import importlib.util
import zipfile
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parents[1] / "stack/autoinstall/wall"
INSTALLER = WALL / "install-wall-capabilities.sh"
BUILD = WALL / "build-sensor-wheelhouse.sh"
LOCK = WALL / "sensor-wheelhouse/requirements.lock"


def _load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), WALL / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = _load("check-wheelhouse-lock")
WRITE = _load("write-lock")


def make_wheel(directory, name, version, body=b"stub"):
    path = directory / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}/__init__.py", body)
    return path


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_wheelhouse(tmp_path, names=CHECK.DEFAULT_REQUIRED, salt=b""):
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir(parents=True)
    for index, name in enumerate(names):
        make_wheel(wheelhouse, name.replace("-", "_"), f"1.{index}.0", body=name.encode() + salt)
    WRITE.main(["write-lock.py", str(wheelhouse), str(wheelhouse / "requirements.lock")])
    return wheelhouse


# --------------------------------------------------------------- write-lock


def test_lock_writer_emits_pip_require_hashes_format_for_every_wheel(tmp_path):
    wheelhouse = build_wheelhouse(tmp_path)
    text = (wheelhouse / "requirements.lock").read_text(encoding="utf-8")
    for wheel in wheelhouse.glob("*.whl"):
        name, version = WRITE.wheel_fields(wheel)
        assert f"{name}=={version} \\" in text
        assert f"--hash=sha256:{sha256(wheel)}" in text
    assert CHECK.parse_lock(text).keys() == {CHECK.canonical(n) for n in CHECK.DEFAULT_REQUIRED}


def test_lock_writer_refuses_a_source_distribution_in_the_wheelhouse(tmp_path):
    wheelhouse = build_wheelhouse(tmp_path)
    (wheelhouse / "legacy-1.0.tar.gz").write_bytes(b"sdist")
    with pytest.raises(SystemExit, match="source distributions"):
        WRITE.main(["write-lock.py", str(wheelhouse), str(tmp_path / "out.lock")])


def test_lock_writer_refuses_two_versions_of_one_distribution(tmp_path):
    wheelhouse = build_wheelhouse(tmp_path)
    make_wheel(wheelhouse, "numpy", "9.9.9")
    with pytest.raises(SystemExit, match="two versions"):
        WRITE.main(["write-lock.py", str(wheelhouse), str(tmp_path / "out.lock")])


# ------------------------------------------------------- check-wheelhouse-lock


@pytest.mark.smoke
def test_check_accepts_a_freshly_built_wheelhouse(tmp_path, capsys):
    assert CHECK.main([str(build_wheelhouse(tmp_path))]) == 0
    assert "5 pinned distributions" in capsys.readouterr().out


@pytest.mark.smoke
@pytest.mark.parametrize(
    "entry, reason",
    [
        ("numpy \\\n    --hash=sha256:" + "0" * 64, "not an exact name==version pin"),
        ("numpy>=1.26.4 \\\n    --hash=sha256:" + "0" * 64, "not an exact name==version pin"),
        ("numpy==1.26.4", "carries no --hash"),
        ("numpy==1.26.4 --hash=md5:" + "0" * 32, "carries no --hash"),
        ("--index-url https://pypi.invalid/simple", "forbidden option"),
        ("numpy @ https://pypi.invalid/numpy.whl", "remote URL"),
        ("-e ./numpy", "forbidden option"),
    ],
)
def test_check_refuses_every_way_a_lock_can_stop_being_offline_and_pinned(entry, reason):
    with pytest.raises(ValueError, match=reason):
        CHECK.parse_lock(entry + "\n")


def test_check_refuses_a_lock_that_drops_a_dependency_the_installer_imports(tmp_path):
    wheelhouse = build_wheelhouse(tmp_path, [n for n in CHECK.DEFAULT_REQUIRED if n != "onnxruntime"])
    with pytest.raises(ValueError, match="onnxruntime"):
        CHECK.check(str(wheelhouse))


def test_check_refuses_a_lock_whose_wheel_is_missing_from_the_directory(tmp_path):
    wheelhouse = build_wheelhouse(tmp_path)
    next(wheelhouse.glob("cryptography-*.whl")).unlink()
    with pytest.raises(ValueError, match="no wheel for: cryptography"):
        CHECK.check(str(wheelhouse))


def test_check_refuses_a_wheel_that_is_present_at_the_wrong_version(tmp_path):
    wheelhouse = build_wheelhouse(tmp_path)
    stale = next(wheelhouse.glob("cryptography-*.whl"))
    stale.rename(stale.with_name("cryptography-99.9.9-py3-none-any.whl"))
    with pytest.raises(ValueError, match="wrong version present for: cryptography"):
        CHECK.check(str(wheelhouse))


@pytest.mark.smoke
def test_expect_refuses_media_carrying_its_own_self_consistent_lock(tmp_path):
    """The substitution attack: a full, valid, hash-correct lock that is not ours."""
    reviewed = build_wheelhouse(tmp_path / "reviewed")
    substituted = build_wheelhouse(tmp_path / "substituted", salt=b"attacker rebuild")
    # Both are internally valid; only the reviewed one is authentic.
    assert CHECK.check(str(substituted)) and CHECK.check(str(reviewed))
    assert CHECK.check(str(reviewed), expect=str(reviewed / "requirements.lock"))
    with pytest.raises(ValueError, match="not the reviewed lock"):
        CHECK.check(str(substituted), expect=str(reviewed / "requirements.lock"))


def test_expect_accepts_a_byte_identical_copy_of_the_reviewed_lock(tmp_path):
    wheelhouse = build_wheelhouse(tmp_path)
    elsewhere = tmp_path / "image-copy.lock"
    elsewhere.write_bytes((wheelhouse / "requirements.lock").read_bytes())
    assert CHECK.check(str(wheelhouse), expect=str(elsewhere))


def test_check_refuses_a_duplicate_pin(tmp_path):
    with pytest.raises(ValueError, match="more than once"):
        CHECK.parse_lock("numpy==1.0 --hash=sha256:%s\nnumpy==2.0 --hash=sha256:%s\n" % ("0" * 64, "1" * 64))


def test_check_normalizes_names_so_pillow_and_dbus_next_are_recognized():
    pinned = CHECK.parse_lock("Pillow==10.4.0 --hash=sha256:%s\ndbus_next==0.2.3 --hash=sha256:%s\n" % ("0" * 64, "1" * 64))
    assert set(pinned) == {"pillow", "dbus-next"}


def test_check_reports_by_message_not_by_traceback(tmp_path, capsys):
    wheelhouse = tmp_path / "empty"
    wheelhouse.mkdir()
    (wheelhouse / "requirements.lock").write_text("numpy==1.26.4\n", encoding="utf-8")
    assert CHECK.main([str(wheelhouse)]) == 1
    assert "REFUSED" in capsys.readouterr().err


# ------------------------------------------------------------------- wiring


@pytest.mark.smoke
def test_installer_validates_the_wheelhouse_before_creating_the_venv():
    text = INSTALLER.read_text(encoding="utf-8")
    check_at = text.index("check-wheelhouse-lock.py")
    assert check_at < text.index("python3 -m venv /opt/wall-sensors/venv")
    assert check_at < text.index("--require-hashes")
    # The offline install line itself must not have loosened.
    assert "--no-index --only-binary=:all: --require-hashes" in text
    assert "--find-links \"$wheelhouse\" -r \"$wheelhouse/requirements.lock\"" in text


@pytest.mark.smoke
def test_installer_anchors_the_media_lock_to_the_lock_staged_in_the_image():
    text = INSTALLER.read_text(encoding="utf-8")
    assert 'reviewed_lock="$(dirname "$0")/sensor-wheelhouse/requirements.lock"' in text
    assert '--expect "$reviewed_lock"' in text
    assert LOCK.exists(), "the anchor the installer points at must be tracked beside it"
    # The anchor is only worth anything if it rides to the panel. It is not named
    # by any late-command: it arrives because late-command 3 copies the payload
    # tree wholesale, and the installer resolves it relative to its own location.
    user_data = (WALL / "user-data").read_text(encoding="utf-8")
    assert 'cp -a "$d/deploy-payload/." /target/opt/wall-panel/' in user_data, (
        "the anchor and the checker reach /opt/wall-panel only via the recursive payload copy"
    )
    assert LOCK.parent.name == "sensor-wheelhouse" and LOCK.parent.parent == WALL
    assert (WALL / "check-wheelhouse-lock.py").exists()


def test_build_refuses_a_container_that_is_not_the_panel_architecture():
    text = BUILD.read_text(encoding="utf-8")
    assert "--platform linux/amd64" in text, "the multi-arch base would silently build ARM wheels"
    assert text.count("--platform linux/amd64") >= 2, "the pull and the run both need it"
    assert '[ "$(uname -m)" = "x86_64" ]' in text


def test_build_never_self_upgrades_pip_and_never_silently_skips_the_sensor_import():
    text = BUILD.read_text(encoding="utf-8")
    assert "--upgrade pip" not in text, "an unpinned pip upgrade is an unbounded build input"
    assert "--skip-sensor-import" in text
    assert 'fail "no sensor source mounted' in text


def test_committed_lock_is_valid_and_pins_the_five_sensor_requirements():
    pinned = CHECK.parse_lock(LOCK.read_text(encoding="utf-8"))
    assert {"numpy": "1.26.4", "pillow": "10.4.0", "cryptography": "43.0.3",
            "onnxruntime": "1.20.1", "dbus-next": "0.2.3"}.items() <= pinned.items()
    assert len(pinned) > 5, "transitive dependencies must be pinned too or --require-hashes fails"


def test_build_script_pins_the_base_image_by_digest_and_stays_binary_only():
    text = BUILD.read_text(encoding="utf-8")
    assert "ubuntu:24.04@sha256:" in text, "the panel-matching base image must be digest-pinned"
    assert "--only-binary=:all:" in text
    assert text.count("--require-hashes") >= 2, "both the reproduce download and the verify install are hash-pinned"
    assert "wsl -d Ubuntu" in text
