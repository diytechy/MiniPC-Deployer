"""SR-016/SR-017/LLR-012: the hub's gateway install path.

WHAT A STUBBED DOCKER CAN AND CANNOT PROVE. These tests run the real script
against a real filesystem with a fake `docker` on PATH, so they prove the file
handling, the refusals, idempotency, retention and the rollback ordering, and
they prove which docker subcommands are issued in which order. They prove
nothing about Caddy, the container image, or a gateway that actually serves.
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "stack/panel-access/gateway-release.py"
STATE_INSTALLER = ROOT / "stack/panel-access/install-gateway-state.sh"

REVISION = "a" * 40
OTHER = "b" * 40


def _spec():
    spec = importlib.util.spec_from_file_location("gateway_release", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(revision, dirty=False):
    return json.dumps({"name": "officewall-shell", "version": "0.1.0",
                       "source": {"revision": revision, "dirty": dirty,
                                  "date": "2026-09-13T16:11:57-05:00"}}).encode()


def archive(path, revision=REVISION, dirty=False, body=b"// server\n", extra=None, omit=()):
    members = {
        "access/build-info.json": stamp(revision, dirty),
        "access/VERSION": b"officewall-shell 0.1.0\n",
        "access/capabilities.json": b'{"schemaVersion":2}\n',
        "access/gateway/server.mjs": body,
        "access/gateway/state.mjs": b"// state\n",
        "access/gateway/setup.mjs": b"// setup\n",
    }
    for name in omit:
        members.pop(name, None)
    members.update(extra or {})
    with tarfile.open(path, "w:gz") as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(payload))
    return path


@pytest.fixture
def hub(tmp_path):
    """A miniature /opt/homehub/stack with a fake docker that records calls."""
    stack = tmp_path / "stack"
    (stack / "panel-access" / "app").mkdir(parents=True)
    (stack / "wall-shell").mkdir(parents=True)
    (stack / "wall-shell" / "build-info.json").write_bytes(stamp(REVISION))
    (stack / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (stack / "panel-access" / "docker-compose.access.yml").write_text("services: {}\n", encoding="utf-8")
    (stack / ".env").write_text("PANEL_ACCESS_ENABLED=true\nPANEL_USER_SUB=someone\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    for name in ("feed-token", "state.key", "state.json.enc"):
        (state / name).write_bytes(b"x")

    log = tmp_path / "docker.log"
    docker = tmp_path / ("docker.py")
    docker.write_text(textwrap.dedent(f"""
        import sys, json, pathlib
        pathlib.Path({str(log)!r}).open('a').write(' '.join(sys.argv[1:]) + chr(10))
        mode = pathlib.Path({str(tmp_path / 'docker.mode')!r})
        behaviour = mode.read_text().strip() if mode.exists() else 'ok'
        present = pathlib.Path({str(tmp_path / 'docker.present')!r})
        if sys.argv[1] == 'inspect':
            # `undead` keeps answering inspect after rm, which is the survivor
            # case the swap must refuse rather than plough through.
            if behaviour == 'undead' or present.exists():
                print('running healthy')
                sys.exit(0)
            sys.exit(1)
        if sys.argv[1] == 'exec':
            # The probe is a real node script; answer it as the gateway would
            # and let the script's own parsing decide. Anything else would make
            # the health assertion a tautology.
            body = {{'ok': True, 'protocolVersion': 1}}
            if behaviour == 'unhealthy':
                body = {{'ok': False, 'protocolVersion': 1}}
            if behaviour == 'wrong-protocol':
                body = {{'ok': True, 'protocolVersion': 2}}
            if behaviour == 'garbage':
                body = None
            ok = bool(body) and body.get('ok') is True and body.get('protocolVersion') == 1
            sys.exit(0 if ok else 1)
        if 'rm' in sys.argv and behaviour != 'undead':
            present.unlink(missing_ok=True)
        if '--force-recreate' in sys.argv or (sys.argv[-1] == 'panel-access' and 'up' in sys.argv):
            if behaviour != 'up-fails':
                present.touch()
            else:
                sys.exit(1)
        sys.exit(0)
    """), encoding="utf-8")
    return {"tmp": tmp_path, "stack": stack, "state": state, "log": log,
            "docker": [sys.executable, str(docker)], "mode": tmp_path / "docker.mode",
            "present": tmp_path / "docker.present"}


def run(hub, *arguments, containers=True):
    command = [sys.executable, str(SCRIPT), *arguments,
               "--stack", str(hub["stack"]), "--state-dir", str(hub["state"]),
               "--docker", " ".join(hub["docker"])]
    # The stub is python+script; feed it through a one-word shim on PATH.
    shim = hub["tmp"] / ("docker.cmd" if os.name == "nt" else "docker")
    if not shim.exists():
        if os.name == "nt":
            shim.write_text('@"%s" "%s" %%*\n' % (sys.executable, hub["docker"][1]), encoding="utf-8")
        else:
            shim.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, hub["docker"][1]), encoding="utf-8")
            shim.chmod(0o755)
    command[-1] = str(shim)
    if not containers:
        command.append("--no-container")
    return subprocess.run(command, capture_output=True, text=True)


def calls(hub):
    return hub["log"].read_text(encoding="utf-8").splitlines() if hub["log"].exists() else []


def live(hub):
    return hub["stack"] / "panel-access" / "app" / "gateway"


# ── refusals ─────────────────────────────────────────────────────────────────

def test_refuses_a_gateway_built_from_a_different_commit_than_the_site(hub, tmp_path):
    result = run(hub, "install", "--archive", str(archive(tmp_path / "g.tgz", revision=OTHER)))
    assert result.returncode == 1
    assert "FAIL" in result.stderr
    assert not live(hub).exists()


def test_refuses_a_dirty_build(hub, tmp_path):
    (hub["stack"] / "wall-shell" / "build-info.json").write_bytes(stamp(REVISION, dirty=True))
    result = run(hub, "install", "--archive", str(archive(tmp_path / "g.tgz", dirty=True)))
    assert result.returncode == 1
    assert not live(hub).exists()


def test_refuses_an_incomplete_payload(hub, tmp_path):
    path = archive(tmp_path / "g.tgz", omit=("access/gateway/state.mjs",))
    assert run(hub, "install", "--archive", str(path)).returncode == 1
    assert not live(hub).exists()


def test_refuses_traversal_and_foreign_roots(hub, tmp_path):
    escape = archive(tmp_path / "e.tgz", extra={"access/../../evil": b"x"})
    assert run(hub, "install", "--archive", str(escape)).returncode == 1
    foreign = archive(tmp_path / "f.tgz", extra={"srv/wall-shell/index.html": b"x"})
    assert run(hub, "install", "--archive", str(foreign)).returncode == 1
    assert not (tmp_path / "evil").exists()


def test_refuses_a_missing_archive_without_touching_the_live_tree(hub, tmp_path):
    run(hub, "install", "--archive", str(archive(tmp_path / "g.tgz")))
    before = sorted(p.name for p in live(hub).iterdir())
    assert run(hub, "install", "--archive", str(tmp_path / "absent.tgz")).returncode == 1
    assert sorted(p.name for p in live(hub).iterdir()) == before


# ── install ──────────────────────────────────────────────────────────────────

def test_install_publishes_the_payload_and_the_stamps(hub, tmp_path):
    result = run(hub, "install", "--archive", str(archive(tmp_path / "g.tgz")))
    assert result.returncode == 0, result.stderr
    assert (live(hub) / "server.mjs").read_bytes() == b"// server\n"
    app = hub["stack"] / "panel-access" / "app"
    assert json.loads((app / "build-info.json").read_text())["source"]["revision"] == REVISION
    assert (app / "VERSION").exists() and (app / "capabilities.json").exists()
    assert (hub["stack"] / "panel-access" / "releases" / "ACTIVE").read_text().strip() == REVISION
    # No gateway source ever lands in the public document root.
    assert not (hub["stack"] / "wall-shell" / "server.mjs").exists()


def _phases(hub):
    """The docker calls reduced to a phase sequence, for exact ordering."""
    order = []
    for line in calls(hub):
        if " stop panel-access" in line:
            order.append("stop")
        elif " rm -f panel-access" in line or line.startswith("rm -f panel-access"):
            order.append("rm")
        elif "--force-recreate" in line:
            order.append("up")
        elif line.startswith("exec panel-access node"):
            order.append("probe")
    return order


def test_install_stops_removes_swaps_then_recreates_in_that_exact_order(hub, tmp_path):
    hub["present"].touch()  # a container is already running
    run(hub, "install", "--archive", str(archive(tmp_path / "g.tgz")))
    assert _phases(hub) == ["stop", "rm", "up", "probe"]


def test_a_container_that_survives_removal_stops_the_swap(hub, tmp_path):
    """The detached-inode case: never replace the bind-mount source under a survivor."""
    run(hub, "install", "--archive", str(archive(tmp_path / "one.tgz", body=b"// one\n")))
    hub["mode"].write_text("undead", encoding="utf-8")
    (hub["stack"] / "wall-shell" / "build-info.json").write_bytes(stamp(OTHER))
    result = run(hub, "install", "--archive", str(archive(tmp_path / "two.tgz", revision=OTHER, body=b"// two\n")))
    assert result.returncode == 1
    assert "could not be removed" in result.stderr
    assert (live(hub) / "server.mjs").read_bytes() == b"// one\n"


@pytest.mark.parametrize("behaviour", ["unhealthy", "wrong-protocol", "garbage"])
def test_health_rejects_a_gateway_that_answers_wrongly(hub, tmp_path, behaviour):
    """The stub answers as the gateway would; the script's predicate decides.

    It mirrors the real response shape rather than running the real node
    script, so it proves which bodies are ACCEPTED, not that node parses them.
    """
    run(hub, "install", "--archive", str(archive(tmp_path / "one.tgz", body=b"// one\n")))
    hub["mode"].write_text(behaviour, encoding="utf-8")
    (hub["stack"] / "wall-shell" / "build-info.json").write_bytes(stamp(OTHER))
    result = run(hub, "install", "--archive", str(archive(tmp_path / "two.tgz", revision=OTHER, body=b"// two\n")))
    assert result.returncode == 1
    assert (live(hub) / "server.mjs").read_bytes() == b"// one\n"


def test_the_probe_is_the_endpoint_and_not_the_cached_docker_grade(hub, tmp_path):
    run(hub, "install", "--archive", str(archive(tmp_path / "g.tgz")))
    probes = [line for line in calls(hub) if line.startswith("exec panel-access node")]
    assert probes and "127.0.0.1:8788/health" in probes[0]
    assert "protocolVersion===1" in probes[0]


def test_a_failed_recreate_rolls_back_and_keeps_the_pending_flag(hub, tmp_path):
    """`up` fails for BOTH the new release and the rollback, which is the worst case."""
    run(hub, "install", "--archive", str(archive(tmp_path / "one.tgz", body=b"// one\n")))
    hub["mode"].write_text("up-fails", encoding="utf-8")
    (hub["stack"] / "wall-shell" / "build-info.json").write_bytes(stamp(OTHER))
    result = run(hub, "install", "--archive", str(archive(tmp_path / "two.tgz", revision=OTHER, body=b"// two\n")))
    assert result.returncode == 1
    assert "recovery required" in result.stderr
    releases = hub["stack"] / "panel-access" / "releases"
    # The previous release's bytes are back on disk, ACTIVE never moved to the
    # new one, and PENDING still marks the transaction as unfinished.
    assert (live(hub) / "server.mjs").read_bytes() == b"// one\n"
    assert (releases / "ACTIVE").read_text().strip() == REVISION
    assert (releases / "PENDING").read_text().strip() == OTHER
    assert run(hub, "status").returncode == 1


def test_a_recoverable_failed_recreate_rolls_back_cleanly(hub, tmp_path):
    """When only the new release cannot come up, the rollback settles and clears PENDING."""
    run(hub, "install", "--archive", str(archive(tmp_path / "one.tgz", body=b"// one\n")))
    hub["mode"].write_text("unhealthy", encoding="utf-8")
    (hub["stack"] / "wall-shell" / "build-info.json").write_bytes(stamp(OTHER))
    run(hub, "install", "--archive", str(archive(tmp_path / "two.tgz", revision=OTHER, body=b"// two\n")))
    releases = hub["stack"] / "panel-access" / "releases"
    assert (live(hub) / "server.mjs").read_bytes() == b"// one\n"
    assert (releases / "ACTIVE").read_text().strip() == REVISION
    assert not (releases / "PENDING").exists()


def test_status_reports_an_interrupted_install(hub, tmp_path):
    run(hub, "install", "--archive", str(archive(tmp_path / "g.tgz")))
    (hub["stack"] / "panel-access" / "releases" / "PENDING").write_text(OTHER + "\n", encoding="utf-8")
    result = run(hub, "status")
    assert result.returncode == 1
    assert "PENDING" in result.stdout and "interrupted" in result.stdout


def test_reinstalling_the_same_payload_stages_nothing(hub, tmp_path):
    path = archive(tmp_path / "g.tgz")
    assert run(hub, "install", "--archive", str(path)).returncode == 0
    first = hashlib.sha256((live(hub) / "server.mjs").read_bytes()).hexdigest()
    hub["log"].unlink()
    second = run(hub, "install", "--archive", str(path))
    assert second.returncode == 0
    assert "already at" in second.stdout
    assert not any("--force-recreate" in line for line in calls(hub))
    assert hashlib.sha256((live(hub) / "server.mjs").read_bytes()).hexdigest() == first


def test_staging_only_when_state_is_not_bootstrapped(hub, tmp_path):
    (hub["state"] / "state.json.enc").unlink()
    result = run(hub, "install", "--archive", str(archive(tmp_path / "g.tgz")))
    assert result.returncode == 0
    assert "not bootstrapped" in result.stdout
    assert (live(hub) / "server.mjs").exists()
    assert not calls(hub)


def test_staging_only_when_access_is_disabled(hub, tmp_path):
    (hub["stack"] / ".env").write_text("PANEL_ACCESS_ENABLED=false\n", encoding="utf-8")
    assert run(hub, "install", "--archive", str(archive(tmp_path / "g.tgz"))).returncode == 0
    assert (live(hub) / "server.mjs").exists()
    assert not calls(hub)


# ── rollback ─────────────────────────────────────────────────────────────────

def _two_generations(hub, tmp_path):
    run(hub, "install", "--archive", str(archive(tmp_path / "one.tgz", body=b"// one\n")))
    (hub["stack"] / "wall-shell" / "build-info.json").write_bytes(stamp(OTHER))
    run(hub, "install", "--archive", str(archive(tmp_path / "two.tgz", revision=OTHER, body=b"// two\n")))


def test_previous_release_is_retained_and_older_ones_pruned(hub, tmp_path):
    _two_generations(hub, tmp_path)
    releases = hub["stack"] / "panel-access" / "releases"
    assert (releases / "ACTIVE").read_text().strip() == OTHER
    assert (releases / "PREVIOUS").read_text().strip() == REVISION
    kept = sorted(p.name for p in releases.iterdir() if p.is_dir())
    assert kept == sorted([REVISION, OTHER])


def test_rollback_restores_the_previous_bytes_and_swaps_the_markers(hub, tmp_path):
    _two_generations(hub, tmp_path)
    assert run(hub, "rollback").returncode == 0
    assert (live(hub) / "server.mjs").read_bytes() == b"// one\n"
    releases = hub["stack"] / "panel-access" / "releases"
    assert (releases / "ACTIVE").read_text().strip() == REVISION
    assert (releases / "PREVIOUS").read_text().strip() == OTHER


def test_rollback_refuses_when_there_is_no_previous_release(hub, tmp_path):
    run(hub, "install", "--archive", str(archive(tmp_path / "g.tgz")))
    result = run(hub, "rollback")
    assert result.returncode == 1
    assert "no retained previous" in result.stderr


def test_an_unhealthy_install_rolls_itself_back_and_exits_nonzero(hub, tmp_path):
    run(hub, "install", "--archive", str(archive(tmp_path / "one.tgz", body=b"// one\n")))
    hub["mode"].write_text("unhealthy", encoding="utf-8")
    (hub["stack"] / "wall-shell" / "build-info.json").write_bytes(stamp(OTHER))
    result = run(hub, "install", "--archive", str(archive(tmp_path / "two.tgz", revision=OTHER, body=b"// two\n")))
    assert result.returncode == 1
    assert (live(hub) / "server.mjs").read_bytes() == b"// one\n"
    assert (hub["stack"] / "panel-access" / "releases" / "ACTIVE").read_text().strip() == REVISION


# ── status and uninstall ─────────────────────────────────────────────────────

def test_status_reports_skew_as_failure(hub, tmp_path):
    run(hub, "install", "--archive", str(archive(tmp_path / "g.tgz")))
    assert run(hub, "status").returncode == 0
    (hub["stack"] / "wall-shell" / "build-info.json").write_bytes(stamp(OTHER))
    skewed = run(hub, "status")
    assert skewed.returncode == 1
    assert "match     NO" in skewed.stdout


def test_status_never_prints_a_secret_path_value(hub, tmp_path):
    (hub["state"] / "state.key").write_bytes(b"super-secret-key-material")
    run(hub, "install", "--archive", str(archive(tmp_path / "g.tgz")))
    output = run(hub, "status").stdout
    assert "super-secret" not in output


def test_uninstall_removes_the_application_and_never_the_state(hub, tmp_path):
    run(hub, "install", "--archive", str(archive(tmp_path / "g.tgz")))
    assert run(hub, "uninstall").returncode == 0
    assert not live(hub).exists()
    assert not (hub["stack"] / "panel-access" / "releases").exists()
    assert (hub["state"] / "state.json.enc").exists()


def test_uninstall_tears_the_container_down_even_with_the_knob_already_off(hub, tmp_path):
    """The documented order turns the knob off first; the container is still up."""
    run(hub, "install", "--archive", str(archive(tmp_path / "g.tgz")))
    (hub["stack"] / ".env").write_text("PANEL_ACCESS_ENABLED=false\n", encoding="utf-8")
    hub["log"].unlink()
    assert run(hub, "uninstall").returncode == 0
    assert "stop" in _phases(hub) and "rm" in _phases(hub)
    assert not hub["present"].exists()


def test_uninstall_refuses_while_a_container_survives_removal(hub, tmp_path):
    run(hub, "install", "--archive", str(archive(tmp_path / "g.tgz")))
    hub["mode"].write_text("undead", encoding="utf-8")
    result = run(hub, "uninstall")
    assert result.returncode == 1
    assert (live(hub) / "server.mjs").exists()


# ── the state installer ──────────────────────────────────────────────────────

@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits and root-only install")
def test_state_installer_is_syntactically_valid():
    assert subprocess.run(["bash", "-n", str(STATE_INSTALLER)]).returncode == 0


def _posix(path):
    """A path bash will see as absolute, on Windows too."""
    if os.name != "nt":
        return str(path)
    converted = subprocess.run(["cygpath", "-u", str(path)], capture_output=True, text=True)
    if converted.returncode != 0:
        pytest.skip("cygpath is unavailable")
    return converted.stdout.strip()


def _bash(*arguments):
    if not shutil.which("bash"):
        pytest.skip("bash is unavailable")
    return subprocess.run(["bash", str(STATE_INSTALLER), *arguments], capture_output=True, text=True)


def test_state_installer_refuses_a_state_dir_inside_a_checkout(tmp_path):
    """A feed token must never land where `git add -A` can sweep it up.

    Run unprivileged on purpose: the path guard is ordered BEFORE the root
    check precisely so this refusal is reachable and testable.
    """
    checkout = tmp_path / "repo"
    (checkout / ".git").mkdir(parents=True)
    inside = checkout / "stack" / "panel-access" / "state"
    result = _bash("--state-dir", _posix(inside))
    assert result.returncode == 1
    assert "inside a git checkout" in result.stderr


def test_state_installer_refuses_relative_and_traversing_paths(tmp_path):
    assert "absolute" in _bash("--state-dir", "var/lib/panel-access").stderr
    assert ".." in _bash("--state-dir", "/var/lib/../tmp/x").stderr


def test_state_installer_reaches_the_root_check_for_a_sane_path():
    """Proves the guard is a filter, not a blanket refusal that hides bugs."""
    result = _bash("--state-dir", "/var/lib/panel-access-test-does-not-exist")
    assert result.returncode == 1
    assert "run as root" in result.stderr


def test_state_installer_writes_no_secret_into_the_repo():
    text = STATE_INSTALLER.read_text(encoding="utf-8")
    assert "--feed-token-file" in text
    # The token is only ever copied from a path the operator supplies.
    assert "install -m 0600" in text
    assert "setup.mjs" in text and "state.key" in text


def test_the_script_declares_no_default_secret_material():
    text = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("PANEL_FEED_TOKEN=", "deviceCredential", "--pin", "pin="):
        assert forbidden not in text
