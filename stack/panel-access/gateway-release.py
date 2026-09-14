#!/usr/bin/env python3
"""Install, update, roll back and inspect the hub's private access gateway.

The gateway is OfficeWallNaglight's payload; this repo owns the hub install
path. One tarball in, one validated release out, the previous release kept.

Implements: SR-016, SR-017, LLR-012.

WHY THIS EXISTS. Until now the hub had a *staging* path (`install-gateway.py`,
driven from firstboot by `stage-gateway.sh`) and a compose overlay, but no way
for an operator holding a freshly built `officewall-gateway-*.tar.gz` to put it
on a running hub, prove it came up, and get back if it did not. The result was
one unextracted tarball in `/opt/homehub/wall-gateway`, a staged application at
a *different* revision than the served site, and no container.

WHAT IT REFUSES TO DO. It never creates, reads, prints or copies a PIN, a state
key, encrypted state or a device credential. `/var/lib/panel-access` is created
(empty, private) by `install-gateway-state.sh`, and filled only by the Owner
running the gateway's own `setup.mjs`. This script can be run by a session; the
PIN step cannot.

THE BIND MOUNT IS WHY THE CONTAINER IS RECREATED. The overlay bind-mounts
`./panel-access/app/gateway`. Replacing that directory's inode detaches the
mount from a running container, which then serves the OLD code from a deleted
inode forever and looks healthy while doing it. So activation always stops the
container, swaps, and recreates it - never a live directory swap.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

INSTALLER = Path(__file__).with_name("install-gateway.py")
_SPEC = importlib.util.spec_from_file_location("install_gateway", INSTALLER)
_UNPACK = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_UNPACK)

SERVICE = "panel-access"
STAMPS = ("build-info.json", "VERSION", "capabilities.json")


class Refused(Exception):
    """A refusal whose message is safe to print: it never quotes file bytes."""


# ── layout ───────────────────────────────────────────────────────────────────

def layout(stack: Path) -> dict:
    base = stack / "panel-access"
    return {
        "stack": stack,
        "base": base,
        "app": base / "app",
        "live": base / "app" / "gateway",
        "releases": base / "releases",
        "active": base / "releases" / "ACTIVE",
        "previous": base / "releases" / "PREVIOUS",
        "pending": base / "releases" / "PENDING",
        "env": stack / ".env",
        "site_stamp": stack / "wall-shell" / "build-info.json",
        "compose": [stack / "docker-compose.yml", base / "docker-compose.access.yml"],
    }


def revision(stamp: Path) -> str:
    """The 40-hex source revision of a build stamp. Never returns a prefix."""
    try:
        value = json.loads(stamp.read_text(encoding="utf-8-sig"))["source"]["revision"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Refused("build stamp is unreadable or has no source revision") from error
    if not isinstance(value, str) or len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
        raise Refused("build stamp revision is not a full commit id")
    return value


def manifest(tree: Path) -> dict:
    """sha256 of every regular file under `tree`, keyed by relative posix path."""
    found = {}
    for path in sorted(tree.rglob("*")):
        if path.is_symlink():
            raise Refused("symlink found in a gateway tree")
        if path.is_file():
            found[path.relative_to(tree).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def read_marker(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def state_ready(state_dir: Path) -> bool:
    """True when the gateway has something to start with.

    `server.mjs` calls `loadEnvironment` then `loadStore`: without the feed
    token, the state key AND the encrypted state it exits at boot and compose
    restarts it forever. There is no "no PIN yet" mode - creating the PIN IS
    creating the gateway - so an install before the Owner's bootstrap stages
    the code and deliberately starts nothing.
    """
    return all((state_dir / name).is_file() for name in ("feed-token", "state.key", "state.json.enc"))


def access_enabled(env: Path) -> bool:
    """PANEL_ACCESS_ENABLED from the hub env. Absent or unreadable means false."""
    try:
        text = env.read_text(encoding="utf-8-sig")
    except OSError:
        return False
    selected = "false"
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "PANEL_ACCESS_ENABLED":
            selected = value.split(" #", 1)[0].strip().strip("\"'")
    return selected == "true"


# ── container control ────────────────────────────────────────────────────────

def compose(paths: dict, docker: str, *arguments: str, check: bool = True):
    command = [docker, "compose"]
    for file in paths["compose"]:
        command += ["-f", str(file)]
    command += list(arguments)
    return subprocess.run(command, cwd=str(paths["stack"]), capture_output=True, text=True, check=check)


def container_state(paths: dict, docker: str) -> dict:
    """Running/health of the gateway container, without failing when absent."""
    probe = subprocess.run(
        [docker, "inspect", "--format", "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}", SERVICE],
        capture_output=True, text=True)
    if probe.returncode != 0:
        return {"present": False, "status": "absent", "health": "absent"}
    status, _, health = probe.stdout.strip().partition(" ")
    return {"present": True, "status": status, "health": health or "none"}


def health(paths: dict, docker: str) -> bool:
    """Ask the running gateway for /health over its own loopback.

    Not `docker inspect`'s health field: that can still read `starting`, and a
    stale grade is exactly the thing an activation must not accept as proof.
    """
    probe = subprocess.run(
        # Function expressions, not arrows: this string is handed to argv on
        # hosts whose shells treat `>` as a redirection, and a probe that only
        # works on one operating system is not a probe.
        [docker, "exec", SERVICE, "node", "-e",
         "fetch('http://127.0.0.1:8788/health')"
         ".then(function(r){if(!r.ok)throw 0;return r.json()})"
         ".then(function(b){if(b&&b.ok===true&&b.protocolVersion===1)process.exit(0);process.exit(1)})"
         ".catch(function(){process.exit(1)})"],
        capture_output=True, text=True)
    return probe.returncode == 0


# ── install ──────────────────────────────────────────────────────────────────

def publish(source: Path, destination: Path) -> None:
    """Replace `destination` with a copy of `source`, regular files only, 0644."""
    staging = destination.with_name(destination.name + ".incoming")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = staging / relative
        if path.is_symlink():
            raise Refused("symlink found in a gateway tree")
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o755)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o644)
    shutil.rmtree(destination, ignore_errors=True)
    os.replace(staging, destination)


def teardown(paths: dict, docker: str) -> None:
    """Remove the container, and PROVE it is gone before anything is swapped.

    `stop` and `rm` are allowed to fail - the container may simply not exist -
    but a container that is still present afterwards is a hard refusal, not a
    warning. Swapping the bind-mount source under a survivor detaches its mount
    and leaves it serving the old code out of a deleted inode, reporting
    healthy the whole time. That is the one failure this script must not be
    able to produce, so it is asserted rather than assumed.
    """
    compose(paths, docker, "stop", SERVICE, check=False)
    compose(paths, docker, "rm", "-f", SERVICE, check=False)
    if container_state(paths, docker)["present"]:
        subprocess.run([docker, "rm", "-f", SERVICE], capture_output=True, text=True)
    if container_state(paths, docker)["present"]:
        raise Refused("the gateway container could not be removed; refusing to replace a live bind-mount source")


def activate(paths: dict, release: Path, docker: str, containers: bool) -> None:
    """Stop, swap the bind-mount source, recreate. See the module docstring."""
    if containers:
        teardown(paths, docker)
    publish(release / "gateway", paths["live"])
    for stamp in STAMPS:
        if (release / stamp).is_file():
            shutil.copyfile(release / stamp, paths["app"] / stamp)
            (paths["app"] / stamp).chmod(0o644)
    if containers:
        compose(paths, docker, "up", "-d", "--force-recreate", SERVICE)


def prune(paths: dict, keep: set) -> None:
    for entry in paths["releases"].iterdir():
        if entry.is_dir() and entry.name not in keep:
            shutil.rmtree(entry, ignore_errors=True)


def install(paths: dict, archive: Path, docker: str, containers: bool, force: bool) -> int:
    site = revision(paths["site_stamp"])
    scratch = Path(tempfile.mkdtemp(prefix=".gateway-release.", dir=str(paths["base"])))
    try:
        # install-gateway.py is the validator: root prefix, traversal, symlinks,
        # required members, oversized stamp, and site/gateway source agreement.
        try:
            _UNPACK.install(str(archive), str(paths["site_stamp"]), str(scratch / "payload"))
        except Exception as error:  # noqa: BLE001 - message is a constant, never bytes
            raise Refused("gateway archive rejected: " + type(error).__name__ + " (source mismatch, unsafe member or incomplete payload)") from error
        payload = scratch / "payload"
        incoming = revision(payload / "build-info.json")
        if incoming != site:
            raise Refused("gateway revision does not match the served site")

        current = read_marker(paths["active"])
        if current == incoming and paths["live"].is_dir() and not force:
            if manifest(paths["live"]) == manifest(payload / "gateway"):
                print("PASS gateway already at " + incoming[:7] + "; nothing staged")
                return reconcile(paths, docker, containers)

        paths["releases"].mkdir(parents=True, exist_ok=True)
        release = paths["releases"] / incoming
        publish(payload, release)

        previous = current if current and current != incoming else read_marker(paths["previous"])
        # ACTIVATION INTENT IS PERSISTED BEFORE THE MUTATION. If the swap or the
        # container recreation throws, PENDING is what tells the next run (and
        # `status`) that the tree on disk may be neither release, so an
        # interrupted install cannot masquerade as a settled one.
        paths["pending"].write_text(incoming + "\n", encoding="utf-8")
        if previous and (paths["releases"] / previous).is_dir():
            paths["previous"].write_text(previous + "\n", encoding="utf-8")
        try:
            activate(paths, release, docker, containers)
        except (Refused, OSError, subprocess.CalledProcessError) as error:
            print("FAIL activation failed: " + type(error).__name__, file=sys.stderr)
            if isinstance(error, Refused):
                print("FAIL " + str(error), file=sys.stderr)
            # The live tree may hold either release. Rolling back re-publishes
            # the previous one deterministically rather than guessing.
            outcome = rollback(paths, docker, containers, automatic=True)
            return outcome
        paths["active"].write_text(incoming + "\n", encoding="utf-8")
        paths["pending"].unlink(missing_ok=True)
        prune(paths, {incoming, previous or incoming})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if containers and not health(paths, docker):
        print("FAIL gateway did not answer /health; rolling back", file=sys.stderr)
        return rollback(paths, docker, containers, automatic=True)
    print("PASS gateway " + incoming[:7] + " installed" + ("" if containers else " (staged only; access disabled or state not bootstrapped)"))
    return 0


def reconcile(paths: dict, docker: str, containers: bool) -> int:
    """Make the running container match what is on disk, without reinstalling."""
    if not containers:
        return 0
    state = container_state(paths, docker)
    if not state["present"] or state["status"] != "running":
        compose(paths, docker, "up", "-d", SERVICE)
    if not health(paths, docker):
        print("FAIL gateway is installed but not answering /health", file=sys.stderr)
        return 1
    return 0


def rollback(paths: dict, docker: str, containers: bool, automatic: bool = False) -> int:
    target = read_marker(paths["previous"])
    if not target or not (paths["releases"] / target).is_dir():
        print("FAIL no retained previous gateway release to roll back to", file=sys.stderr)
        return 1
    current = read_marker(paths["active"]) or read_marker(paths["pending"])
    try:
        activate(paths, paths["releases"] / target, docker, containers)
    except (Refused, OSError, subprocess.CalledProcessError) as error:
        # Both release trees are retained and PENDING is deliberately LEFT in
        # place: a rollback that could not finish is the one state that must
        # not read as settled on the next run.
        print("FAIL rollback could not complete (" + type(error).__name__ + "); recovery required", file=sys.stderr)
        if isinstance(error, Refused):
            print("FAIL " + str(error), file=sys.stderr)
        return 1
    paths["active"].write_text(target + "\n", encoding="utf-8")
    paths["pending"].unlink(missing_ok=True)
    if current and current != target:
        paths["previous"].write_text(current + "\n", encoding="utf-8")
    if containers and not health(paths, docker):
        # Both release trees are still retained, and ACTIVE names what is
        # genuinely on disk. Say so plainly: a second `rollback` swaps back to
        # the release that just failed, which is a decision for a human, not a
        # loop for this script to run on its own.
        print("FAIL rolled back to " + target[:7] + " and it is still not healthy", file=sys.stderr)
        print("FAIL recovery required: both releases are retained; read `docker compose logs panel-access`", file=sys.stderr)
        print("FAIL     before running rollback again, which would re-select " + (current or "the other release")[:7], file=sys.stderr)
        return 1
    print(("PASS automatic rollback to " if automatic else "PASS rolled back to ") + target[:7])
    return 1 if automatic else 0


def uninstall(paths: dict, docker: str, docker_allowed: bool) -> int:
    """Remove the application. The container is torn down REGARDLESS of the knob.

    `containers` elsewhere means "should this thing be running"; uninstall must
    not consult it. The documented order turns PANEL_ACCESS_ENABLED off first,
    so by the time this runs the knob says false while a container is very much
    still up - and deleting its bind-mount source from under it is the detached
    inode again, with a gateway left running that nobody thinks exists.
    """
    if docker_allowed:
        teardown(paths, docker)
    shutil.rmtree(paths["live"], ignore_errors=True)
    shutil.rmtree(paths["releases"], ignore_errors=True)
    for stamp in STAMPS:
        (paths["app"] / stamp).unlink(missing_ok=True)
    print("PASS gateway application removed; /var/lib/panel-access was NOT touched")
    print("     Removing the PIN and device registry is a separate, Owner-only act.")
    return 0


def status(paths: dict, docker: str) -> int:
    lines = []
    try:
        site = revision(paths["site_stamp"])
    except Refused:
        site = None
    active = read_marker(paths["active"])
    lines.append("site      " + (site[:7] if site else "unknown"))
    lines.append("gateway   " + (active[:7] if active else "not installed"))
    lines.append("previous  " + ((read_marker(paths["previous"]) or "none")[:7]))
    interrupted = read_marker(paths["pending"])
    if interrupted:
        lines.append("PENDING   " + interrupted[:7] + " - an install was interrupted; run rollback")
    lines.append("match     " + ("yes" if site and active == site else "NO"))
    lines.append("access    " + ("enabled" if access_enabled(paths["env"]) else "disabled"))
    state = container_state(paths, docker)
    lines.append("container " + state["status"] + " (health " + state["health"] + ")")
    if state["status"] == "running":
        lines.append("endpoint  " + ("/health ok" if health(paths, docker) else "/health FAILED"))
    print("\n".join(lines))
    # An interrupted install is not a green state even when the markers happen
    # to agree with the site: the live tree may hold either release.
    return 0 if site and active == site and not interrupted else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Hub install path for the private access gateway.")
    parser.add_argument("action", choices=["install", "update", "rollback", "uninstall", "status"])
    parser.add_argument("--archive", type=Path, help="officewall-gateway-<version>-g<short>.tar.gz")
    parser.add_argument("--stack", type=Path, default=Path("/opt/homehub/stack"))
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/panel-access"),
                        help="private gateway state; never read, only tested for presence")
    parser.add_argument("--docker", default="docker", help="docker binary (tests pass a stub)")
    parser.add_argument("--no-container", action="store_true",
                        help="stage on disk only; never start or stop the container")
    parser.add_argument("--force", action="store_true", help="reinstall an already-active revision")
    arguments = parser.parse_args()

    paths = layout(arguments.stack)
    containers = not arguments.no_container and access_enabled(paths["env"])
    if containers and not state_ready(arguments.state_dir):
        containers = False
        print("NOTE gateway state is not bootstrapped; staging only and starting nothing.")
        print("     Run install-gateway-state.sh, then the Owner's setup.mjs, then `status`.")
    try:
        if arguments.action in ("install", "update"):
            if not arguments.archive:
                raise Refused("--archive is required")
            return install(paths, arguments.archive, arguments.docker, containers, arguments.force)
        if arguments.action == "rollback":
            return rollback(paths, arguments.docker, containers)
        if arguments.action == "uninstall":
            return uninstall(paths, arguments.docker, not arguments.no_container)
        return status(paths, arguments.docker)
    except Refused as error:
        print("FAIL " + str(error), file=sys.stderr)
        return 1
    except subprocess.CalledProcessError:
        print("FAIL docker compose refused the gateway service; see `docker compose logs panel-access`", file=sys.stderr)
        return 1
    except OSError:
        print("FAIL the gateway release tree is unreadable or unwritable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
