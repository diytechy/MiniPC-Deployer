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
import tempfile
import time
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


def test_firstboot_readiness_gate_matches_the_protocols_that_exist():
    # It was pinned to protocolVersion 1, which stopped existing: core.status
    # emits 2 and adaptive emits 3. The gate could not pass for ANY mode, so
    # wall-firstboot.sh called fail_step and no panel finished provisioning.
    helper = (ROOT / "stack/autoinstall/wall/configure-touch-filter.sh").read_text(
        encoding="utf-8"
    )
    assert "EXPECTED={2,3}" in helper
    assert "state.get('protocolVersion') in EXPECTED" in helper
    assert "state.get('protocolVersion')==1" not in helper


def test_firstboot_accepts_adaptive_without_loading_uinput_for_it():
    # Adaptive's accepted taps leave over the AF_UNIX bridge, not a virtual
    # device, so uinput stays scoped to filter mode.
    helper = (ROOT / "stack/autoinstall/wall/configure-touch-filter.sh").read_text(
        encoding="utf-8"
    )
    assert "off|shadow|filter|adaptive)" in helper
    assert 'if [ "$mode" = filter ]; then\n    modprobe uinput' in helper


@pytest.mark.skipif(
    not os.environ.get("TOUCH_APP_REPO"), reason="application policy comes from sibling"
)
def test_renderer_emits_an_adaptive_bridge_that_survives_a_kiosk_restart():
    # The panel's hand-written config named a Chromium scope by PID. The
    # browser respawned, peer_allowed rejected the real kiosk, and the daemon
    # failed open with the touchscreen ungrabbed - filtering nothing while its
    # status still read "protecting-fail-open". Rendering the scope as a
    # pattern, and deriving the slice from the uid, is what stops that
    # recurring on every reimage.
    sys.path.insert(0, os.environ["TOUCH_APP_REPO"])
    from touchfilter.bridge import session_pattern, validate_bridge_config

    renderer = load("stack/autoinstall/wall/render-touch-filter.py", "renderer")
    config, _ = renderer.render({"TOUCH_FILTER_MODE": "adaptive"})
    validate_bridge_config(config["bridge"])

    pattern = session_pattern("0::" + config["bridge"]["session_cgroup"])
    slice_ = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
    for pid in ("26690", "1664", "7"):
        assert pattern.fullmatch(f"{slice_}app-org.chromium.Chromium-{pid}.scope")
    # Still exact: no sibling scope, no child cgroup, no other user's slice.
    assert not pattern.fullmatch(f"{slice_}app-org.chromium.Chromium-1664.scope/child")
    assert not pattern.fullmatch(f"{slice_}app-evil.Evil-1664.scope")
    assert not pattern.fullmatch(
        "0::/user.slice/user-1001.slice/user@1001.service/app.slice/"
        "app-org.chromium.Chromium-1664.scope"
    )

    # protection_quiet_ms rides in policy but is not a core Policy field, so a
    # renderer that added it before validation would raise on every adaptive
    # render. The whole config must load the way the daemon loads it.
    from touchfilter.daemon import load_config

    for mode in ("off", "shadow", "filter", "adaptive"):
        rendered, _ = renderer.render({"TOUCH_FILTER_MODE": mode})
        path = Path(tempfile.mkdtemp()) / "touch-filter.json"
        path.write_text(json.dumps(rendered), encoding="utf-8")
        loaded = load_config(path)
        assert ("adaptive_policy" in loaded) is (mode == "adaptive")
        assert ("bridge" in rendered) is (mode == "adaptive")


@pytest.mark.skipif(
    not os.environ.get("TOUCH_APP_REPO"), reason="application policy comes from sibling"
)
def test_renderer_rejects_a_scope_that_could_escape_its_own_component():
    sys.path.insert(0, os.environ["TOUCH_APP_REPO"])
    renderer = load("stack/autoinstall/wall/render-touch-filter.py", "renderer")
    for scope in ("../../evil.scope", "a/b.scope", "app-*-*.scope", "notascope", ""):
        with pytest.raises(ValueError):
            renderer.render({"TOUCH_FILTER_MODE": "adaptive", "TOUCH_KIOSK_SCOPE": scope})
    for uid in ("0", "-1", "99999999"):
        with pytest.raises(ValueError):
            renderer.render({"TOUCH_FILTER_MODE": "adaptive", "TOUCH_KIOSK_UID": uid})


# ---------------------------------------------------------------------------
# The readiness gate's HEALTH set, executed rather than grepped (2026-09-19)
# ---------------------------------------------------------------------------


def _run_readiness_gate(mode, status, tmp_path):
    """Execute the gate's real embedded Python against one status document.

    THE DECISION LOGIC IS THE THING UNDER TEST, so it is executed rather than
    asserted as source text the way the two tests above do. Only two literals
    are substituted -- the status path, which is hardcoded to /run, and the
    retry count, because a failing gate otherwise sleeps for ten seconds. The
    protocol set, the mode comparison, the HEALTH set and the freshness rule
    are the shipped ones.

    Returns the gate's stdout on success; raises SystemExit's message on
    refusal, which is what firstboot turns into a fail_step.
    """
    import re as _re
    helper = (ROOT / "stack/autoinstall/wall/configure-touch-filter.sh").read_text(encoding="utf-8")
    body = helper.split('python3 - "$mode" <<\'PY\'\n', 1)[1].split("\nPY\n", 1)[0]
    document = tmp_path / "status.json"
    document.write_text(json.dumps(status), encoding="utf-8")
    before = body
    body = body.replace("'/run/wall-touch-filter/status.json'", repr(str(document)))
    body = body.replace("range(100)", "range(2)")
    assert body != before and "range(2)" in body, "the substitution did not take"
    assert "/run/wall-touch-filter" not in body, "the gate would read the real panel path"
    import contextlib
    out = io.StringIO()
    # The body does `import sys` itself, so the mode has to arrive the way the
    # shell delivers it -- as argv -- rather than through the exec namespace.
    argv = sys.argv
    sys.argv = [str(ROOT / "configure-touch-filter.sh"), mode]
    try:
        with contextlib.redirect_stdout(out):
            exec(compile(body, "readiness-gate", "exec"), {})
    finally:
        sys.argv = argv
    return out.getvalue()


def _fresh(**fields):
    base = {"protocolVersion": 3, "mode": "adaptive", "health": "ready",
            "observedAt": (time.time() + 5) * 1000}
    base.update(fields)
    return base


def test_adaptive_passes_the_gate_while_the_kiosk_bridge_is_down():
    """The defect that rolled back a whole paired release (2026-09-19).

    The Owner moved the panel to adaptive; the next deploy's firstboot failed
    on this gate and the release rolled back, with a log line naming the touch
    filter and nothing to do with what was being deployed.

    It is not a flake. Adaptive reaches 'ready' only once AdaptiveGuard clears
    `recovery`, which happens only when it takes the grab, which requires
    `bridge.ready` -- the KIOSK. The release lane stops the kiosk before
    running firstboot, so the gate was waiting for a state the sequence it
    runs in makes unreachable. Every adaptive deploy would have failed here.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        assert "PASS" in _run_readiness_gate(
            "adaptive", _fresh(health="protecting-fail-open"), tmp)


def test_the_gate_still_refuses_a_daemon_that_is_swallowing_touches():
    """'protecting-fail-open' is accepted because it means NOT grabbed, so
    touches reach the compositor natively. 'protecting-unavailable' is the
    opposite -- grabbed while the output path is unavailable -- and is the one
    state where the daemon eats every touch. Widening the set must not have
    widened it to that."""
    import tempfile
    for health in ("protecting-unavailable", "stopped", "off", "device-unavailable"):
        with tempfile.TemporaryDirectory() as directory:
            with pytest.raises(SystemExit):
                _run_readiness_gate("adaptive", _fresh(health=health), Path(directory))


def test_shadow_and_filter_are_not_widened_by_the_adaptive_fix():
    """Only adaptive has a bridge it must wait for. For the other two a daemon
    that has not reached 'ready' is still a daemon that has not started."""
    import tempfile
    for mode in ("shadow", "filter"):
        with tempfile.TemporaryDirectory() as directory:
            assert "PASS" in _run_readiness_gate(
                mode, _fresh(mode=mode, health="ready"), Path(directory))
        with tempfile.TemporaryDirectory() as directory:
            with pytest.raises(SystemExit):
                _run_readiness_gate(
                    mode, _fresh(mode=mode, health="protecting-fail-open"), Path(directory))


def test_the_gate_still_requires_a_fresh_document_and_the_right_mode():
    """The two assertions that were always doing the real work. A status left
    by the PREVIOUS daemon would otherwise pass the restart it is meant to
    prove, and a document for another mode is a config that did not take."""
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(SystemExit):
            _run_readiness_gate("adaptive", _fresh(observedAt=0), Path(directory))
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(SystemExit):
            _run_readiness_gate("adaptive", _fresh(mode="shadow"), Path(directory))
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(SystemExit):
            _run_readiness_gate("adaptive", _fresh(protocolVersion=1), Path(directory))
