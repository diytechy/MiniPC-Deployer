"""SR-017: validated renderer, actual archive contracts and private installation."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = load("scripts/assert_wall_capabilities.py", "contract")
installer = load("stack/panel-access/install-gateway.py", "installer")


def artifact(tmp_path, kind, revision="a" * 40, omit=None, dirty=False):
    root = {"shell": "app", "site": "site", "gateway": "access"}[kind]
    stamp = {
        "source": {"revision": revision, "dirty": dirty},
        "capabilities": list(contract.REQUIRED),
    }
    files = {root + "/build-info.json": json.dumps(stamp)}
    if kind == "shell":
        prefix = "app/runtime/resources/app/"
        files[prefix + "capabilities.json"] = json.dumps(
            {"schemaVersion": 1, "capabilities": contract.REQUIRED}
        )
        for paths in contract.REQUIRED.values():
            for path in paths:
                files[prefix + path] = "fixture"
    elif kind == "site":
        files["site/index.html"] = "fixture"
    else:
        files.update(
            {
                "access/gateway/server.mjs": "fixture",
                "access/gateway/state.mjs": "fixture",
            }
        )
    if omit:
        files.pop(omit)
    path = tmp_path / (
        "officewall-"
        + kind
        + "-1.0.0-g"
        + revision[:7]
        + ("-linux-x64" if kind == "shell" else "")
        + ".tar.gz"
    )
    with tarfile.open(path, "w:gz") as archive:
        for name, text in files.items():
            data = text.encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return path


def test_shell_marker_cannot_claim_an_absent_implementation(tmp_path):
    good = artifact(tmp_path, "shell")
    assert contract.inspect(good, "shell") == "a" * 40
    bad = artifact(
        tmp_path, "shell", omit="app/runtime/resources/app/touchfilter/daemon.py"
    )
    with pytest.raises(ValueError, match="missing capability"):
        contract.inspect(bad, "shell")


def test_dirty_or_missing_manifest_release_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="clean"):
        contract.inspect(artifact(tmp_path, "shell", dirty=True), "shell")
    with pytest.raises(ValueError, match="missing capability"):
        contract.inspect(
            artifact(
                tmp_path, "shell", omit="app/runtime/resources/app/capabilities.json"
            ),
            "shell",
        )


def test_three_payload_cli_rejects_full_revision_difference_even_matching_prefix(
    tmp_path,
):
    shell = artifact(tmp_path, "shell")
    site = artifact(tmp_path, "site")
    gateway = artifact(tmp_path, "gateway", revision="a" * 7 + "b" * 33)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/assert_wall_capabilities.py"),
            "--shell",
            str(shell),
            "--site",
            str(site),
            "--gateway",
            str(gateway),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "revision mismatch" in result.stderr


def test_private_gateway_extracts_only_into_private_app_and_requires_matching_site(
    tmp_path,
):
    gateway = artifact(tmp_path, "gateway")
    site = tmp_path / "site.json"
    site.write_text(json.dumps({"source": {"revision": "a" * 40, "dirty": False}}))
    target = tmp_path / "private-app"
    installer.install(gateway, site, target)
    assert (target / "gateway/server.mjs").read_text(encoding="utf-8") == "fixture"
    assert not (tmp_path / "docker-compose.access.yml").exists()
    site.write_text(json.dumps({"source": {"revision": "b" * 40, "dirty": False}}))
    with pytest.raises(ValueError, match="mismatch"):
        installer.install(gateway, site, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_image_wires_filter_ordering_offline_dependency_and_suspend_recovery():
    wall = ROOT / "stack/autoinstall/wall"
    assert "Before=getty@tty1.service" in (
        wall / "wall-touch-filter.service"
    ).read_text(encoding="utf-8")
    assert "python3-evdev" in (wall / "packages.list").read_text(encoding="utf-8")
    assert 'bash "$PAYLOAD/configure-touch-filter.sh"' in (
        wall / "wall-firstboot.sh"
    ).read_text(encoding="utf-8")
    helper = (wall / "configure-touch-filter.sh").read_text(encoding="utf-8")
    assert helper.index("PASS touch filter") < helper.index(
        'install -m 0644 "$scratch/rules"'
    )
    assert "systemctl disable --now wall-touch-filter.service" in helper
    assert "udevadm trigger --subsystem-match=input" in helper
    assert "systemctl stop wall-touch-filter.service" in (
        wall / "wall-touch-filter-sleep"
    ).read_text(encoding="utf-8")


@pytest.mark.skipif(
    not os.environ.get("TOUCH_APP_REPO"), reason="application policy comes from sibling"
)
def test_renderer_applies_isolation_and_rejects_injection():
    sys.path.insert(0, os.environ["TOUCH_APP_REPO"])
    renderer = load("stack/autoinstall/wall/render-touch-filter.py", "renderer")
    default, rules = renderer.render({})
    assert default["mode"] == "off" and default["policy"]["startup_ms"] == 1500
    active, rules = renderer.render(
        {"TOUCH_FILTER_MODE": "filter", "TOUCH_FILTER_ISOLATE": "true"}
    )
    assert (
        active["isolate"] and 'ENV{LIBINPUT_IGNORE_DEVICE}="1"' in rules.splitlines()[0]
    )
    assert 'ENV{LIBINPUT_IGNORE_DEVICE}="0"' in rules.splitlines()[1]
    with pytest.raises(ValueError):
        renderer.render({"TOUCH_FILTER_NAME": 'bad"; RUN+="evil'})
    with pytest.raises(ValueError):
        renderer.render({"TOUCH_MIN_TAP_MS": "nan"})


def test_actual_hub_stager_copies_matching_private_gateway_and_refuses_partial_release(
    tmp_path,
):
    bash = (
        Path("C:/Program Files/Git/bin/bash.exe")
        if os.name == "nt"
        else Path("/bin/bash")
    )
    if not bash.exists():
        pytest.skip("Bash is required for stager integration")
    artifacts = tmp_path / "dist"
    artifacts.mkdir()
    artifact(artifacts, "shell")
    artifact(artifacts, "site")
    gateway = artifact(artifacts, "gateway")
    output = tmp_path / "out"
    output.mkdir()

    def shell_path(path):
        value = Path(path).as_posix()
        return "/" + value[0].lower() + value[2:] if os.name == "nt" else value

    env = {
        **os.environ,
        "WALL_SHELL_DIST": shell_path(artifacts),
        "TEST_ROOT": shell_path(ROOT),
        "TEST_OUTPUT": shell_path(output),
        "TEST_PYTHON": shell_path(sys.executable),
    }
    script = 'source "$TEST_ROOT/vmtest/lib/common.sh"\npython3() { "$TEST_PYTHON" "$@"; }\nstage_wall_site_into_payload "$TEST_OUTPUT" "$TEST_ROOT"\n'
    result = subprocess.run(
        [str(bash), "-c", script], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    staged = output / "iso-root/deploy-payload/wall-gateway" / gateway.name
    assert staged.read_bytes() == gateway.read_bytes()
    gateway.unlink()
    result = subprocess.run(
        [str(bash), "-c", script], env=env, capture_output=True, text=True
    )
    assert result.returncode != 0 and "Incomplete wall release" in result.stderr
