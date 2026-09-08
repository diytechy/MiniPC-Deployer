"""SR-017: validated renderer, actual archive contracts and private installation."""

import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
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


def artifact(tmp_path, kind, revision="a" * 40, omit=None, dirty=False, manifest=None, extra=None, extra_archive=None):
    root = {"shell": "app", "site": "site", "gateway": "access"}[kind]
    stamp = {
        "source": {"revision": revision, "dirty": dirty},
        "capabilities": sorted(contract.REQUIRED),
    }
    files = {root + "/build-info.json": json.dumps(stamp)}
    payload = {"shell": "app", "site": "site", "gateway": "gateway"}[kind]
    prefix = {"shell": "app/runtime/resources/app/", "site": "site/", "gateway": "access/"}[kind]
    files[prefix + "capabilities.json"] = json.dumps(
        manifest if manifest is not None else {"schemaVersion": 2, "capabilities": contract.REQUIRED}
    )
    for declaration in contract.REQUIRED.values():
        for required in declaration[payload]:
            files[prefix + required] = "fixture"
    if kind == "site":
        for allowed in contract.ALLOWED_SITE_FILES:
            files.setdefault("site/" + allowed, "fixture")
    if extra:
        files[prefix + extra] = "must not be public"
    if extra_archive:
        files[extra_archive] = "must not escape the expected archive root"
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


@pytest.mark.parametrize("forbidden", ["electron/main.cjs", "gateway/server.mjs", "sensors/service.py", "touchfilter/daemon.py"])
def test_public_site_rejects_privileged_trees_even_when_required_files_exist(tmp_path, forbidden):
    with pytest.raises(ValueError, match="privileged file in public site"):
        contract.inspect(artifact(tmp_path, "site", extra=forbidden), "site")


@pytest.mark.parametrize(
    "unexpected",
    ["debug.txt", "js/local-debug.js", "css/private-token.txt", "assets/nested/leak"],
)
def test_public_site_rejects_every_arbitrary_extra_served_file(tmp_path, unexpected):
    with pytest.raises(ValueError, match="unexpected file in public site"):
        contract.inspect(artifact(tmp_path, "site", extra=unexpected), "site")


@pytest.mark.parametrize("unexpected", ["leak.txt", "other/leak.txt"])
def test_public_site_rejects_files_outside_its_archive_root(tmp_path, unexpected):
    with pytest.raises(ValueError, match="outside public site root"):
        contract.inspect(
            artifact(tmp_path, "site", extra_archive=unexpected),
            "site",
        )


PAYLOAD_ROOT = {"shell": "app/runtime/resources/app/", "site": "site/", "gateway": "access/"}
PAYLOAD_NAME = {"shell": "app", "site": "site", "gateway": "gateway"}
ALL_REQUIRED_OMISSIONS = [
    (kind, PAYLOAD_ROOT[kind] + file)
    for kind, payload in PAYLOAD_NAME.items()
    for declaration in contract.REQUIRED.values()
    for file in declaration[payload]
]


def test_tracker_correction_gateway_contract_includes_stateful_handler():
    assert contract.REQUIRED["tracker-corrections-v2"]["gateway"] == [
        "gateway/server.mjs",
        "gateway/state.mjs",
    ]


@pytest.mark.parametrize(("kind", "missing"), ALL_REQUIRED_OMISSIONS)
def test_each_payload_rejects_missing_manifest_or_required_implementation(tmp_path, kind, missing):
    with pytest.raises(ValueError, match="missing capability"):
        contract.inspect(artifact(tmp_path, kind, omit=missing), kind)


@pytest.mark.parametrize(
    "malformed",
    [
        {"schemaVersion": 1, "capabilities": contract.REQUIRED},
        {"schemaVersion": 2, "capabilities": {"local-visualizer-v1": []}},
        {"schemaVersion": 2, "capabilities": {**contract.REQUIRED, "unexpected": {"app": [], "site": [], "gateway": []}}},
    ],
)
def test_each_payload_rejects_malformed_or_mismatched_declaration(tmp_path, malformed):
    with pytest.raises(ValueError, match="declaration mismatch"):
        contract.inspect(artifact(tmp_path, "site", manifest=malformed), "site")
    mismatch = {"schemaVersion": 2, "capabilities": dict(contract.REQUIRED)}
    mismatch["capabilities"]["local-visualizer-v1"] = dict(mismatch["capabilities"]["local-visualizer-v1"])
    mismatch["capabilities"]["local-visualizer-v1"]["site"] = ["js/views/visualizer.js"]
    with pytest.raises(ValueError, match="declaration mismatch"):
        contract.inspect(artifact(tmp_path, "site", manifest=mismatch), "site")


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


def test_firstboot_gateway_stager_degrades_safely_and_removes_stale_app(tmp_path):
    bash = shutil.which("bash")
    if not bash:
        candidate = Path("C:/Program Files/Git/bin/bash.exe")
        if candidate.exists():
            bash = str(candidate)
    if not bash:
        pytest.skip("Bash is required for gateway staging lifecycle tests")

    helper = ROOT / "stack/panel-access/stage-gateway.sh"
    site = tmp_path / "site.json"
    site.write_text(json.dumps({"source": {"revision": "a" * 40, "dirty": False}}))
    good = artifact(tmp_path, "gateway")
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    duplicate = artifact(other_dir, "gateway")
    target = tmp_path / "private-app"

    def run(*archives):
        return subprocess.run(
            [str(bash), str(helper), sys.executable, str(ROOT / "stack/panel-access/install-gateway.py"), str(site), str(target), *map(str, archives)],
            capture_output=True,
            text=True,
        )

    result = run(good)
    assert result.returncode == 0, result.stderr
    assert (target / "gateway/server.mjs").is_file()

    result = run(good, duplicate)
    assert result.returncode == 0
    assert "WARNING" in result.stderr and "ambiguous" in result.stderr
    assert not target.exists()

    target.mkdir()
    (target / "stale-server.mjs").write_text("stale")
    site.write_text(json.dumps({"source": {"revision": "b" * 40, "dirty": False}}))
    result = run(good)
    assert result.returncode == 0
    assert "WARNING" in result.stderr and "disabled" in result.stderr
    assert not target.exists()


def test_firstboot_uses_nonfatal_gateway_stager_before_core_compose():
    source = (ROOT / "stack/autoinstall/firstboot.sh").read_text(encoding="utf-8")
    gateway = source[source.index("# Stage the coherent private gateway"):source.index("# ── 3e.")]
    assert "stage-gateway.sh" in gateway
    assert "exit 1" not in gateway
    assert "_gateway_candidates=()" in gateway
    assert gateway.index("for cand in ") < gateway.index("elif ! bash")
    assert "break" not in gateway
    assert '"${_gateway_candidates[@]}"' in gateway
    no_payload = gateway[gateway.index('if [ "${#_gateway_candidates[@]}" -eq 0 ]'):gateway.index("elif ! bash")]
    assert 'rm -rf "$STACK_DIR/panel-access/app"' in no_payload
    assert source.index("stage-gateway.sh") < source.index('log "docker compose up -d')


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
