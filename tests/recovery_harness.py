"""Shared fixtures for the hub recovery tests: a fake filesystem tree, fake
clocks, a fake watchdog device, and loaders for the hyphenated scripts.

Everything the supervisor touches is under one temporary root (rc.Paths(root)),
so these tests read and write real files - the cgroup.kill write, the
cgroup.events read, boot.json, status.json, the evidence files - and only the
kernel and the commands are substituted.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RECOVERY = REPO / "stack" / "recovery"
sys.path.insert(0, str(RECOVERY))

import recovery_common as rc  # noqa: E402


def load_script(module_name: str, filename: str):
    """Import a hyphenated script (homehub-recoveryd.py) as a module."""
    if module_name in sys.modules:
        return sys.modules[module_name]
    # An explicit loader: the CLI has no .py suffix, and without one
    # spec_from_file_location cannot tell it is Python.
    loader = importlib.machinery.SourceFileLoader(module_name, str(RECOVERY / filename))
    spec = importlib.util.spec_from_loader(module_name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


recoveryd = load_script("homehub_recoveryd", "homehub-recoveryd.py")
recovery_boot = load_script("homehub_recovery_boot", "homehub-recovery-boot.py")

BOOT_ID = "b" * 32
INVOCATION = "1" * 32
GiB = 1024 ** 3


def cid(n: int) -> str:
    return "{:064x}".format(n)


class FakeClock:
    """Monotonic, boottime and UTC that only move when a test moves them."""

    def __init__(self) -> None:
        self.mono = 1000.0
        self.boot = 1000.0
        self.wall = 1_800_000_000.0

    def monotonic(self) -> float:
        return self.mono

    def boottime(self) -> float:
        return self.boot

    def utc(self) -> float:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.boot += seconds
        self.wall += seconds


class FakeDevice:
    """Records what the supervisor asked of /dev/watchdog0."""

    instances: list = []

    def __init__(self, path: str) -> None:
        self.path = path
        self.calls: list = []
        self.pets = 0
        FakeDevice.instances.append(self)

    def set_timeout(self, seconds: int) -> int:
        self.calls.append(("set_timeout", seconds))
        return seconds

    def keepalive(self) -> None:
        self.pets += 1
        self.calls.append(("keepalive",))

    def magic_close(self) -> None:
        self.calls.append(("magic_close",))

    def close(self) -> None:
        self.calls.append(("close",))


class FakeKmsg:
    def __init__(self) -> None:
        self.lines: list = []

    def write(self, line: str) -> None:
        self.lines.append(line)


class Tree:
    """A fake root: procfs, cgroupfs, Docker metadata, run and state dirs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.paths = rc.Paths(str(root))
        for d in (self.paths.run_dir, self.paths.state_dir, self.paths.cgroup_root,
                  os.path.join(self.paths.proc_dir, "pressure"),
                  os.path.join(self.paths.proc_dir, "sys", "kernel", "random"),
                  self.paths.docker_containers_dir):
            os.makedirs(d, exist_ok=True)
        self.write(self.paths.boot_id_file, "{}-{}-{}-{}-{}\n".format(
            BOOT_ID[:8], BOOT_ID[8:12], BOOT_ID[12:16], BOOT_ID[16:20], BOOT_ID[20:]))
        self.write(self.paths.kernel_panic_file, "0\n")
        self.psi(0.0)
        self.write(os.path.join(self.paths.proc_dir, "meminfo"),
                   "MemTotal: 8000000 kB\nMemAvailable: 4000000 kB\nSwapTotal: 4000000 kB\n"
                   "SwapFree: 3000000 kB\n")

    @staticmethod
    def write(path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def psi(self, full: float) -> None:
        self.write(os.path.join(self.paths.proc_dir, "pressure", "memory"),
                   "some avg10={0:.2f} avg60=0.00 avg300=0.00 total=1\n"
                   "full avg10={0:.2f} avg60=0.00 avg300=0.00 total=1\n".format(full))

    def container(self, n: int, name: str, current: int, service: str = "", restart: str = "unless-stopped",
                  slice_rel: str = "system.slice", identified: bool = True) -> str:
        """Create a container scope (and, if identified, its Docker metadata)."""
        scope = os.path.join(self.paths.cgroup_root, slice_rel, "docker-{}.scope".format(cid(n)))
        os.makedirs(scope, exist_ok=True)
        self.write(os.path.join(scope, "memory.current"), "{}\n".format(current))
        self.write(os.path.join(scope, "cgroup.events"), "populated 1\nfrozen 0\n")
        if identified:
            meta = os.path.join(self.paths.docker_containers_dir, cid(n))
            self.write(os.path.join(meta, "config.v2.json"), json.dumps(
                {"Name": "/" + name, "Config": {"Labels": {"com.docker.compose.service": service or name}}}))
            self.write(os.path.join(meta, "hostconfig.json"), json.dumps(
                {"RestartPolicy": {"Name": restart}, "AutoRemove": False}))
        return scope

    def empty(self, scope: str) -> None:
        self.write(os.path.join(scope, "cgroup.events"), "populated 0\nfrozen 0\n")

    def boot_json(self, **overrides) -> None:
        record = {"boot_id": BOOT_ID, "committed": True, "braked": False}
        record.update(overrides)
        self.write(self.paths.boot_json, json.dumps(record))


def config(**overrides) -> "rc.Config":
    env = {"HUB_WATCHDOG_ENABLED": "true", "HUB_WD_TIERS": "full"}
    env.update({k: str(v) for k, v in overrides.items()})
    return rc.parse_config(env)


def make_supervisor(tree: Tree, cfg=None, clock=None, runner=None, sshd_probe=None,
                    start_threads=False, lock_memory=None, sched_other=None, journal=None,
                    device_opener=None, device_path="/dev/watchdog0"):
    FakeDevice.instances = []
    exits = []
    sup = recoveryd.Supervisor(
        cfg or config(), tree.paths, BOOT_ID, INVOCATION, clock=clock or FakeClock(),
        device_opener=device_opener or FakeDevice, device_path=device_path,
        sshd_probe=sshd_probe or (lambda: True),
        runner=runner or (lambda argv, timeout: (0, "")),
        kmsg=FakeKmsg(), sched_other=sched_other or (lambda: None),
        lock_memory=lock_memory or (lambda: None), exit_fn=exits.append,
        journal=journal or (lambda line: None), start_threads=start_threads)
    sup.exits = exits
    return sup


def drain_telemetry(sup) -> list:
    """Run every queued telemetry item synchronously; return the event items."""
    events = []
    while sup.workers.telemetry:
        item = sup.workers.telemetry.popleft()
        if item.get("type") == "event":
            events.append(item)
        sup._execute_telemetry(item)
    return events


def actions_of(events, name):
    return [e for e in events if e["action"] == name]


def tick_for(sup, clock, tree, seconds, psi, step=5.0):
    """Tick every `step` seconds for `seconds`, with PSI `psi` (None = unreadable)."""
    elapsed = 0.0
    while elapsed < seconds:
        clock.advance(step)
        elapsed += step
        if psi is None:
            path = os.path.join(tree.paths.proc_dir, "pressure", "memory")
            if os.path.exists(path):
                os.unlink(path)
        else:
            tree.psi(psi)
        sup.safe_tick()
