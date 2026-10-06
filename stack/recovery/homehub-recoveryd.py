#!/usr/bin/env python3
"""homehub-recoveryd — C2, the hub's recovery supervisor (one process, stdlib only).

What it does, in escalation order (design HUB_HANG_WATCHDOG_DESIGN_2026-10-05.md):
  L1  a sustained, MEASURED memory stall kills the largest eligible container
      by its cgroup (cgroup.kill), confirms the cgroup emptied, and reconciles
      Docker asynchronously; a compose service killed twice in an hour is
      QUARANTINED (left stopped, survives a reboot) instead of spent on again.
  L2  sshd silent while memory is fine -> `systemctl restart ssh`.
  L3  the stall outlives 600 s from its start -> `systemctl reboot`, with the
      intent left in /run for the shutdown stamp to carry to disk.
  L4  it drives the hardware watchdog while it runs; if it stops petting (a
      reboot that never arrives, a MemoryError, a crash) the board resets.

What it NEVER does on the main thread: touch a disk, journald or D-Bus. The
main thread reads procfs/cgroupfs, decides, writes cgroup.kill, probes sshd's
socket, pets the device and writes tmpfs. Everything that can block for ever
lives on worker threads (§3.5):

  main       SCHED_RR 1 (set by the unit), the only realtime thread
  action     SCHED_OTHER; subprocesses with deadlines; NON-DROPPABLE, one
             deduplicated queue per action kind
  telemetry  2 x SCHED_OTHER daemon threads; journal lines, evidence files and
             the Docker inventory; drop-OLDEST, and may block for ever

MODES (one of, decided at startup, reported in /run/homehub-recovery/status.json):
  full         L1 L2 L3 L4 (HUB_WD_TIERS=full, accounted, not braked)
  contain      L1 L2 + quarantine; reboots recorded as would-have (P3)
  braked       as contain, because the boot brake is on (§4)
  report-only  nothing acts; every decision is recorded as would-have. Taken
               whenever the boot accounting did not commit or setup failed.

Disarm (§3.6) is `homehub-recovery disarm`, never ExecStop: SIGTERM without a
matching /run authorisation hands the device to the shutdown with a 300 s
timeout and NO magic close.

The pure core (MemoryStateMachine, decide_quarantine, suspend detection) is
separate from the Supervisor shell, and every clock, path, device, probe and
subprocess runner is injected so tests drive it hermetically.

Implements: SR-050, SR-051, LLR-998, LLR-999, LLR-1000, LLR-1001, LLR-1002, LLR-1007, LLR-1008
"""

from __future__ import annotations

import collections
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import recovery_common as rc  # noqa: E402

# ── constants the design fixes (not knobs) ───────────────────────────────────
SAMPLE_SECONDS = 5           # §3.1 sample cadence
GAP_SECONDS = 15             # an iteration later than this is a scheduling gap
CLEAR_SECONDS = 60           # consecutive measured calm that closes an episode
CLEAR_MIN_SAMPLES = 3        # ...and at least this many measurements in it
COOLDOWN_SECONDS = 60        # after a kill
KILL_BUDGET = 3              # kills per rolling hour
KILL_BUDGET_WINDOW = 3600
# A window must hold at least this share of the samples it could have held to
# count as measured at all. Without it, two pressured samples either side of a
# long gap would read as "100 % pressured" — a gap dressed up as a measurement,
# which §3.1 forbids (R2-9).
KILL_MIN_MEASURED_FRACTION = 0.5
SUSPEND_JUMP_SECONDS = 5     # BOOTTIME - MONOTONIC growth that means suspend
CONFIRM_SECONDS = 10         # cgroup must read populated 0 within this
RECONCILE_RETRY_SECONDS = 60
RECONCILE_MAX_ATTEMPTS = 10
QUARANTINE_WINDOW = 3600
QUARANTINE_AFTER_KILLS = 2
SSH_PROBE_SECONDS = 60
ESCALATION_SECONDS = 120     # §3.3: no SIGTERM this long after a reboot request
INVENTORY_SECONDS = 30
# The running supervisor re-reads the boot accounting this often, so a brake
# that expires (24 h with no counted boot) is noticed without a restart, and a
# log that becomes unreadable brakes it (review C9).
BRAKE_CHECK_SECONDS = 600
# `homehub-recovery disarm` asks for a disarm with this signal; SIGTERM never
# disarms. getattr: the module also imports on the Windows dev box.
SIG_DISARM = getattr(signal, "SIGUSR1", 10)
RING_SIZE = 48               # 4 minutes of samples held for evidence
TELEMETRY_QUEUE = 256
WORKER_STACK = 256 * 1024

NORMAL, PRESSURED, COOLDOWN = "normal", "pressured", "cooldown"


# ── pure core: the memory state machine (§3.1) ──────────────────────────────


class MemoryStateMachine:
    """Decides on monotonic time alone. No I/O, no wall clock.

    Contract:
      observe(now, psi) -> list of decisions, each one of
        "gap"           this iteration started > GAP_SECONDS after the last
        "episode-open"  first measured pressured sample in NORMAL (sets t0)
        "clear"         CLEAR_SECONDS of measured calm; back to NORMAL
        "kill"          the caller must pick a victim, then call note_kill()
                        or note_no_kill() — the machine does not know whether
                        one existed
        "reboot"        t0 is REBOOT seconds old (gaps count as elapsed time)
      psi is the measured `full avg10` percent, or None when it could not be
      read (an unmeasured iteration, exactly like a gap for pressure purposes).
    Invariants: a kill never resets t0; only "clear" does. Gaps never count as
    measured pressure (they add no sample) and only feed the reboot clock by
    letting monotonic time pass inside an open episode.
    Implements: SR-050, LLR-999
    """

    def __init__(self, psi_full: float, kill_seconds: float, kill_measured_pct: float,
                 reboot_seconds: float) -> None:
        self.psi_full = psi_full
        self.kill_seconds = kill_seconds
        self.kill_fraction = kill_measured_pct / 100.0
        self.reboot_seconds = reboot_seconds
        self.min_measured = max(1, int(kill_seconds / SAMPLE_SECONDS * KILL_MIN_MEASURED_FRACTION))
        self.state = NORMAL
        self.t0: Optional[float] = None
        self.samples: Deque[Tuple[float, bool]] = collections.deque()
        self.kills: Deque[float] = collections.deque()
        self.cooldown_until = 0.0
        self.reboot_decided = False
        self._last: Optional[float] = None
        self._clear_since: Optional[float] = None
        self._clear_count = 0

    def reset(self) -> None:
        """Back to NORMAL with no episode (suspend detected). Keeps the kill budget."""
        self.state, self.t0 = NORMAL, None
        self.samples.clear()
        self.reboot_decided = False
        self._last = None
        self._clear_since, self._clear_count = None, 0

    def observe(self, now: float, psi: Optional[float]) -> List[str]:
        out: List[str] = []
        gap = self._last is not None and now - self._last > GAP_SECONDS
        if gap:
            out.append("gap")
        self._last = now
        measured = psi is not None
        pressured = measured and psi >= self.psi_full
        if self.state == NORMAL:
            if pressured:
                self.state, self.t0 = PRESSURED, now
                self.samples.clear()
                self.samples.append((now, True))
                self._clear_since, self._clear_count = None, 0
                out.append("episode-open")
            return out
        # "60 s of CONSECUTIVE MEASURED calm" (§3.1): an unreadable sample or a
        # scheduling gap is not calm, so it breaks the streak. Counting the
        # unmeasured interval would let 10 s of calm plus 50 s of nothing close
        # an episode and throw away its reboot clock (review C8).
        if gap or not measured:
            self._clear_since, self._clear_count = None, 0
        if measured:
            self.samples.append((now, pressured))
            if pressured:
                self._clear_since, self._clear_count = None, 0
            else:
                if self._clear_since is None:
                    self._clear_since = now
                self._clear_count += 1
        while self.samples and self.samples[0][0] < now - self.kill_seconds:
            self.samples.popleft()
        if (self._clear_since is not None and now - self._clear_since >= CLEAR_SECONDS
                and self._clear_count >= CLEAR_MIN_SAMPLES):
            self.reset_episode()
            out.append("clear")
            return out
        if not self.reboot_decided and now - self.t0 >= self.reboot_seconds:
            self.reboot_decided = True
            out.append("reboot")
        if self.state == COOLDOWN and now >= self.cooldown_until:
            self.state = PRESSURED
        if self.state == PRESSURED and self._kill_due(now):
            out.append("kill")
        return out

    def reset_episode(self) -> None:
        self.state, self.t0 = NORMAL, None
        self.samples.clear()
        self.reboot_decided = False
        self._clear_since, self._clear_count = None, 0

    def budget_left(self, now: float) -> int:
        while self.kills and self.kills[0] <= now - KILL_BUDGET_WINDOW:
            self.kills.popleft()
        return KILL_BUDGET - len(self.kills)

    def _kill_due(self, now: float) -> bool:
        if now - self.t0 < self.kill_seconds or self.budget_left(now) <= 0:
            return False
        measured = len(self.samples)
        if measured < self.min_measured:
            return False
        pressured = sum(1 for _, p in self.samples if p)
        return pressured >= self.kill_fraction * measured

    def note_kill(self, now: float) -> None:
        """A container was killed: spend budget, cool down, keep t0."""
        self.kills.append(now)
        self.state, self.cooldown_until = COOLDOWN, now + COOLDOWN_SECONDS

    def note_no_kill(self, now: float) -> None:
        """No victim (or quarantine instead): cool down WITHOUT spending budget."""
        self.state, self.cooldown_until = COOLDOWN, now + COOLDOWN_SECONDS


def suspended(prev_offset: Optional[float], offset: float) -> bool:
    """True if BOOTTIME-MONOTONIC grew by more than SUSPEND_JUMP_SECONDS (§3.1)."""
    return prev_offset is not None and offset - prev_offset > SUSPEND_JUMP_SECONDS


def decide_quarantine(history: Sequence[Tuple[float, str]], key: str, now: float) -> bool:
    """Has `key` (a compose service) been the victim twice within the hour?

    Implements: SR-050, LLR-998
    """
    recent = [k for t, k in history if k == key and now - t < QUARANTINE_WINDOW]
    return len(recent) >= QUARANTINE_AFTER_KILLS


# ── injectable edges ─────────────────────────────────────────────────────────


class Clock:
    """Production clocks. Tests substitute an object with the same three methods."""

    def monotonic(self) -> float:
        return time.clock_gettime(time.CLOCK_MONOTONIC)

    def boottime(self) -> float:
        return time.clock_gettime(time.CLOCK_BOOTTIME)

    def utc(self) -> float:
        return time.time()


class WatchdogDevice:
    """/dev/watchdogN through the kernel's watchdog ioctl interface.

    ioctl numbers from linux/watchdog.h: _IOR('W', 5, int), _IOWR('W', 6, int).
    Close WITHOUT writing 'V' leaves the timer running (watchdog_dev.c), which
    is the shutdown hand-off; magic_close() is the explicit disarm only.
    Implements: SR-051, LLR-1002
    """

    WDIOC_SETOPTIONS = 0x80045704
    WDIOC_KEEPALIVE = 0x80045705
    WDIOC_SETTIMEOUT = 0xC0045706
    WDIOS_DISABLECARD = 0x0001

    def __init__(self, path: str) -> None:
        self.path = path
        self.fd: Optional[int] = os.open(path, os.O_WRONLY)

    def _ioctl(self, request: int, value: int) -> int:
        import fcntl  # Linux only; imported here so the module loads on any OS
        import struct
        buf = bytearray(struct.pack("i", value))
        fcntl.ioctl(self.fd, request, buf, True)
        return struct.unpack("i", bytes(buf))[0]

    def set_timeout(self, seconds: int) -> int:
        return self._ioctl(self.WDIOC_SETTIMEOUT, seconds)

    def keepalive(self) -> None:
        self._ioctl(self.WDIOC_KEEPALIVE, 0)

    def disable_card(self) -> None:
        """WDIOS_DISABLECARD: stop the timer while the descriptor is still held."""
        self._ioctl(self.WDIOC_SETOPTIONS, self.WDIOS_DISABLECARD)

    def magic_close(self) -> None:
        try:
            os.write(self.fd, b"V")
        finally:
            self.close()

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def probe_sshd(host: str = "127.0.0.1", port: int = 22, timeout: float = 4.0) -> bool:
    """sshd answers with its banner within `timeout` seconds.

    The hub runs ssh.socket (verified 2026-10-05), so the kernel completes the
    TCP handshake even when sshd is wedged; only the banner proves sshd works.
    Implements: SR-050, LLR-1007
    """
    try:
        with socket.create_connection((host, port), timeout=timeout / 2) as sock:
            sock.settimeout(timeout / 2)
            return sock.recv(64).startswith(b"SSH-")
    except OSError:
        return False


def run_command(argv: Sequence[str], timeout: float) -> Tuple[int, str]:
    """Run one command with a deadline. Timeout -> (124, "timeout"). Never raises."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
        return done.returncode, (done.stdout or "") + (done.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout after {}s".format(timeout)
    except OSError as exc:
        return 127, str(exc)


def sched_other_this_thread() -> None:
    """Drop the CALLING thread to SCHED_OTHER (Linux applies it per thread)."""
    os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))


def mlockall() -> None:
    """mlockall(MCL_CURRENT | MCL_FUTURE) through libc; raises OSError on failure."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.mlockall(1 | 2) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


class Kmsg:
    """Non-blocking /dev/kmsg writer: the minimum record that survives everything."""

    def __init__(self, path: str = "/dev/kmsg") -> None:
        try:
            self.fd: Optional[int] = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            self.fd = None

    def write(self, line: str) -> None:
        if self.fd is None:
            return
        try:
            os.write(self.fd, "<5>homehub-recoveryd: {}\n".format(line).encode("utf-8", "replace")[:900])
        except OSError:
            pass


# ── workers ──────────────────────────────────────────────────────────────────

ACTION_PRIORITY = ("reboot", "restart_ssh", "quarantine", "reconcile")


class Workers:
    """The action thread and the two telemetry threads (§3.5).

    Actions: one unbounded deque per kind, in ACTION_PRIORITY order. The MAIN
    thread deduplicates (it never enqueues a key already in flight), so the
    deques stay tiny and nothing is ever evicted. Telemetry: one bounded deque
    that drops its OLDEST item when full; both telemetry threads may block for
    ever and nothing else waits on them.

    Every queue operation the main thread performs is a single deque.append —
    atomic under the GIL and never contended by a lock a worker could be
    holding while blocked.
    Implements: SR-050, LLR-1000
    """

    def __init__(self, execute_action: Callable[[str, dict], dict],
                 execute_telemetry: Callable[[dict], None],
                 sched_other: Callable[[], None] = sched_other_this_thread,
                 idle_sleep: float = 0.05) -> None:
        self.actions: Dict[str, Deque[dict]] = {k: collections.deque() for k in ACTION_PRIORITY}
        self.results: Deque[dict] = collections.deque()
        self.telemetry: Deque[dict] = collections.deque(maxlen=TELEMETRY_QUEUE)
        self.execute_action = execute_action
        self.execute_telemetry = execute_telemetry
        self.sched_other = sched_other
        self.idle_sleep = idle_sleep
        self.ready: Dict[str, Optional[str]] = {}
        self.threads: List[threading.Thread] = []
        self.telemetry_dropped = 0

    def start(self) -> None:
        threading.stack_size(WORKER_STACK)
        specs = [("action", self._action_loop)] + [("telemetry-%d" % i, self._telemetry_loop)
                                                   for i in (1, 2)]
        for name, target in specs:
            started = threading.Event()
            thread = threading.Thread(target=self._enter, args=(name, target, started),
                                      name=name, daemon=True)
            thread.start()
            if not started.wait(5):
                self.ready[name] = "did not start within 5 s"
            self.threads.append(thread)

    def _enter(self, name: str, target: Callable[[], None], started: threading.Event) -> None:
        try:
            self.sched_other()
            self.ready[name] = None
        except Exception as exc:  # reported, never swallowed: it makes setup fail
            self.ready[name] = "could not drop to SCHED_OTHER: {}".format(exc)
        started.set()
        target()

    def setup_failures(self) -> List[str]:
        return ["{}: {}".format(k, v) for k, v in self.ready.items() if v]

    # main-thread side -------------------------------------------------------
    def enqueue_action(self, kind: str, payload: dict) -> None:
        self.actions[kind].append(payload)

    def emit(self, item: dict) -> None:
        if len(self.telemetry) == self.telemetry.maxlen:
            self.telemetry_dropped += 1
        self.telemetry.append(item)

    # worker side ------------------------------------------------------------
    def run_one_action(self) -> bool:
        for kind in ACTION_PRIORITY:
            try:
                payload = self.actions[kind].popleft()
            except IndexError:
                continue
            try:
                result = self.execute_action(kind, payload)
            except MemoryError:
                result = {"ok": False, "detail": "MemoryError in the action thread; item lost"}
            except Exception as exc:  # the action thread must survive any one action
                result = {"ok": False, "detail": "action raised: {!r}".format(exc)}
            result.setdefault("kind", kind)
            result.setdefault("key", payload.get("key"))
            self.results.append(result)
            return True
        return False

    def _action_loop(self) -> None:
        while True:
            if not self.run_one_action():
                time.sleep(self.idle_sleep)

    def _telemetry_loop(self) -> None:
        while True:
            try:
                item = self.telemetry.popleft()
            except IndexError:
                time.sleep(self.idle_sleep)
                continue
            try:
                self.execute_telemetry(item)
            except Exception:  # noqa: BLE001 - telemetry is disposable by design (§3.5)
                pass


# ── the supervisor shell ─────────────────────────────────────────────────────


class Supervisor:
    """Orchestrates one tick at a time; every edge is injected.

    Contract:
      Inputs:  cfg (rc.Config), paths (rc.Paths), boot_id, invocation_id and
               the injectables below. Nothing is opened until setup().
      Outputs: side effects only — cgroup.kill writes, queued actions, device
               ioctls, tmpfs status/intent files, telemetry items.
    Implements: SR-050, SR-051, LLR-998, LLR-999, LLR-1000, LLR-1001, LLR-1002, LLR-1007, LLR-1008
    """

    def __init__(self, cfg: rc.Config, paths: rc.Paths, boot_id: str, invocation_id: str,
                 clock=None, device_opener: Callable[[str], object] = WatchdogDevice,
                 device_path: str = "/dev/watchdog0", sshd_probe: Callable[[], bool] = probe_sshd,
                 runner: Callable[[Sequence[str], float], Tuple[int, str]] = run_command,
                 kmsg=None, sched_other: Callable[[], None] = sched_other_this_thread,
                 lock_memory: Callable[[], None] = mlockall,
                 exit_fn: Callable[[int], None] = os._exit,
                 journal: Callable[[str], None] = None,
                 start_threads: bool = True) -> None:
        self.cfg, self.paths = cfg, paths
        self.boot_id, self.invocation_id = boot_id, invocation_id
        self.clock = clock or Clock()
        self.device_opener, self.device_path = device_opener, device_path
        self.sshd_probe, self.runner = sshd_probe, runner
        self.kmsg = kmsg if kmsg is not None else Kmsg()
        self.lock_memory, self.exit_fn = lock_memory, exit_fn
        self.journal = journal or self._stdout_journal
        self.start_threads = start_threads
        self.sm = MemoryStateMachine(cfg.psi_full, cfg.kill_seconds, cfg.kill_measured_pct,
                                     cfg.reboot_seconds)
        self.workers = Workers(self._execute_action, self._execute_telemetry, sched_other)
        self.mode = "report-only"
        self.reasons: List[str] = []
        self.braked = False
        self.device = None
        self.petting = False
        self.dying = False
        # Set when arming failed after the device was opened AND it could not
        # be verified stopped: the supervisor keeps petting rather than abandon
        # a running timer (review C3), and says so in a red status.
        self.arm_error: Optional[str] = None
        self.next_brake_check = 0.0
        self.pets = 0
        self.inventory: Dict[str, rc.ContainerInfo] = {}
        self.quarantined: Dict[str, dict] = {}
        self.quarantine_seq = 0
        self._quarantine_written_seq = 0
        self._quarantine_lock = threading.Lock()
        self.kill_history: List[Tuple[float, str]] = []
        self.pending_confirm: Dict[str, dict] = {}
        self.pending_reconcile: Dict[str, dict] = {}
        self.inflight: set = set()
        self.reboot_requested_at: Optional[float] = None
        self.ssh_fails = 0
        self.next_ssh_probe = 0.0
        self.next_inventory = 0.0
        self.prev_offset: Optional[float] = None
        self.ring: List[Optional[dict]] = [None] * RING_SIZE
        self.ring_pos = 0
        self.last_sample: dict = {}

    # ── setup (§3.0) ────────────────────────────────────────────────────────
    def setup(self) -> None:
        """Decide the mode, start workers, preallocate, mlockall, THEN arm.

        Any failure here leaves the device closed and the mode report-only,
        with the reason in status.json — never a half-armed supervisor.
        """
        boot = rc.read_json(self.paths.boot_json)
        self._load_quarantine()
        if not self.cfg.enabled:
            self.reasons.append("HUB_WATCHDOG_ENABLED is not true")
        if boot is None:
            self.reasons.append("no boot accounting record for this boot (boot unit did not run)")
        elif boot.get("boot_id") != self.boot_id:
            self.reasons.append("boot accounting record names another boot")
        elif not boot.get("committed"):
            self.reasons.append("boot accounting could not be committed: {}".format(boot.get("error")))
        else:
            self.braked = bool(boot.get("braked"))
        try:
            if self.start_threads:
                self.workers.start()
                self.reasons.extend(self.workers.setup_failures())
            self.ring = [None] * RING_SIZE  # preallocated before the lock
            self.lock_memory()
        except Exception as exc:  # noqa: BLE001 - every setup failure means report-only
            self.reasons.append("setup failed: {!r}".format(exc))
        if self.reasons:
            self.mode = "report-only"
        elif self.braked:
            self.mode = "braked"
        elif self.cfg.full:
            self.mode = "full"
        else:
            self.mode = "contain"
        if self.mode == "full" and self.device_path:
            outcome, detail = self._arm()
            if outcome == "refused":
                self.reasons.append(detail)
                self.mode = "report-only"
        else:
            # full with no reset device configured is "graceful reboots only";
            # every other mode must not leave a predecessor's timer running.
            self._disarm_orphan_at_start()
        self.next_brake_check = self.clock.monotonic() + BRAKE_CHECK_SECONDS
        self.event("start", mode=self.mode, reasons=list(self.reasons), braked=self.braked,
                   device=self.device_path if self.device else None, kmsg=True)
        self._write_status(self.clock.monotonic())

    def _load_quarantine(self) -> None:
        # Startup only (before mlockall), so a disk read is allowed here.
        data = rc.read_json(self.paths.quarantine_file) or {}
        for key, value in (data.get("quarantined") or {}).items():
            if isinstance(value, dict):
                self.quarantined[key] = value

    @property
    def acts(self) -> bool:
        return self.mode in ("full", "contain", "braked")

    # ── the reset device: arm, verified release (review C2, C3) ────────────
    @property
    def device_name(self) -> str:
        return os.path.basename(self.device_path or "watchdog0")

    def _sysfs(self, leaf: str) -> Optional[str]:
        try:
            with open(os.path.join(self.paths.watchdog_class_dir, self.device_name, leaf),
                      encoding="ascii") as handle:
                return handle.read().strip()
        except OSError:
            return None

    def device_state(self) -> str:
        """'active', 'inactive', 'absent' (no such watchdog) or 'unknown'.

        sysfs, so the main thread may read it (kernfs never waits on a disk).
        Anything but inactive/absent counts as RUNNING wherever a decision
        rests on it."""
        if not os.path.isdir(os.path.join(self.paths.watchdog_class_dir, self.device_name)):
            return "absent"
        state = self._sysfs("state")
        return state if state in ("active", "inactive") else "unknown"

    def _timeout_error(self) -> Optional[str]:
        """Both timeouts must lie inside the DEVICE's range, read before opening.

        Opening a watchdog STARTS it; a timeout the firmware rejects found out
        after that is a running timer nobody can set (review C3). max_timeout 0
        means the core has no upper bound (it pets a short hardware heartbeat
        itself); an unreadable range refuses to arm."""
        lo_raw, hi_raw = self._sysfs("min_timeout"), self._sysfs("max_timeout")
        if lo_raw is None or hi_raw is None or not lo_raw.isdigit() or not hi_raw.isdigit():
            return "cannot read {}'s min_timeout/max_timeout; not arming".format(self.device_name)
        lo, hi = int(lo_raw), int(hi_raw)
        for knob, value in (("HUB_WD_HW_TIMEOUT", self.cfg.hw_timeout),
                            ("HUB_WD_SHUTDOWN_SECONDS", self.cfg.shutdown_seconds)):
            if value < lo or (hi > 0 and value > hi):
                return "{}={} is outside {}'s range {}..{}; not arming".format(
                    knob, value, self.device_name, lo, hi if hi else "unbounded")
        return None

    def _arm(self) -> Tuple[str, str]:
        """Open and set the device. Returns (outcome, detail):

          armed     open, timeout set, petted
          refused   nothing is running: validation failed, the open failed, or
                    a later step failed and the device was VERIFIED stopped
          retained  a later step failed and the device could NOT be verified
                    stopped, so it is held and petted (at whatever timeout it
                    has) rather than abandoned - the box must not reset because
                    arming went wrong
        Implements: SR-051, LLR-1002, LLR-1010
        """
        if not self.device_path:
            return "refused", "no reset device configured"
        error = self._timeout_error()
        if error:
            return "refused", error
        try:
            self.device = self.device_opener(self.device_path)
        except Exception as exc:  # noqa: BLE001
            self.device = None
            return "refused", "could not open {}: {!r}".format(self.device_path, exc)
        try:
            self.device.set_timeout(self.cfg.hw_timeout)
            self.device.keepalive()
            self.petting = True
            self.arm_error = None
            return "armed", "armed {} at {} s".format(self.device_path, self.cfg.hw_timeout)
        except Exception as exc:  # noqa: BLE001
            detail = "arming {} failed after open: {!r}".format(self.device_path, exc)
        if self._release_device_verified():
            return "refused", detail + "; the device was disarmed and verified inactive"
        self.arm_error = detail + "; the device could NOT be disarmed, so it is held and petted"
        self.event("arm-failed-device-retained", kmsg=True, detail=self.arm_error)
        return "retained", self.arm_error

    def _release_device_verified(self) -> bool:
        """Stop the device and PROVE it stopped; otherwise keep owning it.

        disable_card first (the descriptor is still held, so a failure costs
        nothing), then the magic close, then sysfs must read inactive. If it
        does not, the device is reopened and petted: an active timer with no
        owner resets the box, which is the one outcome a disarm must never
        cause. Returns True only on verified inactive (or absent).
        Implements: SR-051, LLR-1002, LLR-1010
        """
        if self.device is not None:
            try:
                self.device.disable_card()
            except Exception:  # noqa: BLE001 - optional; the magic close is the main path
                pass
            try:
                self.device.magic_close()
            except Exception as exc:  # noqa: BLE001
                self.kmsg.write("magic close failed: {!r}".format(exc))
        self.device, self.petting = None, False
        if self.device_state() in ("inactive", "absent"):
            return True
        try:
            self.device = self.device_opener(self.device_path)
            self.petting = True
            self.device.keepalive()
        except Exception as exc:  # noqa: BLE001
            self.kmsg.write("device still ACTIVE after disarm and could not be reopened ({!r}); "
                            "the board will reset".format(exc))
        return False

    def _disarm_orphan_at_start(self) -> None:
        """This instance will NOT arm, but a previous one may have handed off.

        `systemctl restart` (or any start after a plain stop) runs the old
        instance's SIGTERM hand-off: the timer keeps running at 300 s for the
        replacement to take over. A replacement that comes up contain, braked
        or report-only never opens the device, so that countdown would reset a
        healthy box (review C1). Take it over and stop it, verified; if it will
        not stop, hold it and keep petting, red.
        Implements: SR-051, LLR-1010
        """
        if self.device_state() != "active":
            return
        configured = self.device_path
        self.device_path = configured or "/dev/watchdog0"
        try:
            self.device = self.device_opener(self.device_path)
        except Exception as exc:  # noqa: BLE001
            self.device = None
            self.arm_error = "a running watchdog left by a previous instance could not be opened: {!r}".format(exc)
            self.kmsg.write(self.arm_error + "; the board will reset")
            self.device_path = configured
            return
        if self._release_device_verified():
            self.event("orphan-disarmed", kmsg=True, mode=self.mode)
            self.device_path = configured
        else:
            self.arm_error = ("a running watchdog left by a previous instance could not be disarmed; "
                              "holding and petting it")
            self.event("orphan-retained", kmsg=True, detail=self.arm_error)

    def _write_kernel_panic(self, value: int) -> None:
        try:
            with open(self.paths.kernel_panic_file, "w", encoding="ascii") as handle:
                handle.write("{}\n".format(value))
        except OSError as exc:
            self.event("kernel-panic-write-failed", value=value, error=str(exc))

    def _apply_brake(self, result: dict) -> None:
        """Main thread: act on a periodic brake re-evaluation (review C9).

        Fail closed: an unreadable log arrives as braked. Engaging releases the
        device verifiably (a device that will not stop is kept and petted, and
        the mode still stops rebooting). Clearing re-arms only through _arm(),
        with its range validation; a refusal leaves it braked.
        Implements: SR-051, LLR-1011
        """
        braked = bool(result.get("braked"))
        if self.mode not in ("full", "braked", "contain"):
            return
        if braked and self.mode != "braked":
            released = self.device is None or self._release_device_verified()
            self.mode, self.braked = "braked", True
            self._write_kernel_panic(0)
            self.event("brake-engaged", kmsg=True, detail=result.get("detail"),
                       device_released=released)
        elif not braked and self.mode == "braked":
            if self.cfg.full:
                if self.device_path:  # no device configured: graceful reboots only
                    outcome, detail = self._arm()
                    if outcome == "refused":
                        self.event("brake-cleared-not-armed", kmsg=True, detail=detail)
                        return
                self.mode = "full"
                self._write_kernel_panic(rc.kernel_panic_value(self.cfg, True, False))
            else:
                self.mode = "contain"
            self.braked = False
            self.event("brake-cleared", kmsg=True, mode=self.mode, detail=result.get("detail"))
        else:
            return
        self._update_boot_record()

    def _update_boot_record(self) -> None:
        boot = rc.read_json(self.paths.boot_json)  # tmpfs
        if boot is None:
            return
        boot["braked"] = self.braked
        boot["kernel_panic"] = rc.kernel_panic_value(self.cfg, True, self.braked) if self.mode != "report-only" else 0
        try:
            rc.atomic_write(self.paths.boot_json, json.dumps(boot, sort_keys=True).encode(), durable=False)
        except OSError:
            pass

    def _brake_check(self) -> None:
        """Telemetry thread: read the accounting (disk) and post the verdict."""
        try:
            state = rc.evaluate_brake(self.paths, self.cfg, self.clock.utc())
            result = {"kind": "brake", "braked": state.braked, "ok": True, "detail": state.detail}
        except rc.AccountingError as exc:
            result = {"kind": "brake", "braked": True, "ok": False,
                      "detail": "accounting unreadable, so braked: {}".format(exc)}
        self.workers.results.append(result)

    # ── the loop ────────────────────────────────────────────────────────────
    def run(self, wait: Callable[[float], Optional[int]]) -> None:
        """Tick every SAMPLE_SECONDS until SIGTERM. `wait` returns a signal or None."""
        while True:
            sig = wait(SAMPLE_SECONDS)
            if sig == SIG_DISARM:
                self.on_disarm_request()
                continue
            if sig is not None:
                self.on_sigterm()
                return
            self.safe_tick()

    def safe_tick(self) -> None:
        try:
            self.tick()
        except MemoryError:
            # §3.0: stop petting so L4 takes over. Do NOT exit: a restarted
            # instance would re-arm and pet, and the reset would never come.
            self.petting = False
            self.dying = True
            self.kmsg.write("MemoryError in the main loop; petting stopped, L4 will reset")

    def tick(self) -> None:
        if self.dying:
            return
        now = self.clock.monotonic()
        offset = self.clock.boottime() - now
        if suspended(self.prev_offset, offset):
            self.sm.reset()
            self.event("suspend-detected", jump=round(offset - (self.prev_offset or 0), 1))
        self.prev_offset = offset
        self._drain_results(now)
        self._read_released()
        sample = self._sample(now)
        for decision in self.sm.observe(now, sample.get("psi_full")):
            self._on_decision(decision, now, sample)
        self._check_confirmations(now)
        self._retry_reconciles(now)
        self._ssh_check(now)
        self._escalation(now)
        if now >= self.next_inventory:
            self.next_inventory = now + INVENTORY_SECONDS
            self.workers.emit({"type": "inventory"})
        if now >= self.next_brake_check:
            self.next_brake_check = now + BRAKE_CHECK_SECONDS
            if self.mode in ("full", "braked", "contain"):
                self.workers.emit({"type": "brake-check"})
        self._write_status(now)
        self._pet()

    def _pet(self) -> None:
        if self.device is not None and self.petting:
            self.device.keepalive()
            self.pets += 1

    # ── sampling ────────────────────────────────────────────────────────────
    def _read(self, *parts: str) -> Optional[str]:
        try:
            with open(os.path.join(self.paths.proc_dir, *parts), encoding="ascii") as handle:
                return handle.read()
        except OSError:
            return None

    def _sample(self, now: float) -> dict:
        psi_text = self._read("pressure", "memory")
        mem = rc.read_meminfo(self._read("meminfo") or "")
        sample = {
            "mono": round(now, 1), "utc": round(self.clock.utc(), 1),
            "psi_full": rc.read_psi_full_avg10(psi_text) if psi_text else None,
            "mem_available_kb": mem.get("MemAvailable"), "swap_free_kb": mem.get("SwapFree"),
            "state": self.sm.state,
        }
        self.ring[self.ring_pos % RING_SIZE] = sample
        self.ring_pos += 1
        self.last_sample = sample
        return sample

    def ring_snapshot(self) -> List[dict]:
        n = min(self.ring_pos, RING_SIZE)
        start = self.ring_pos - n
        return [self.ring[i % RING_SIZE] for i in range(start, self.ring_pos)]

    # ── decisions ───────────────────────────────────────────────────────────
    def _on_decision(self, decision: str, now: float, sample: dict) -> None:
        if decision == "gap":
            self.event("scheduling-gap", note="recorded as delay, never as pressure")
        elif decision == "episode-open":
            self.event("pressure-episode-open", psi_full=sample.get("psi_full"))
        elif decision == "clear":
            self.event("pressure-clear")
        elif decision == "kill":
            self._kill(now)
        elif decision == "reboot":
            self._reboot(now, "memory stall for {} s (episode start t0)".format(self.cfg.reboot_seconds))

    def _kill(self, now: float) -> None:
        scopes = rc.scan_container_scopes(self.paths.cgroup_root)
        choice = rc.choose_victim(scopes, self.inventory, self.cfg.protect)
        if choice.would_have_killed:
            self.event("would-have-killed", target=choice.would_have_killed, why="protected", kmsg=True)
        if choice.victim is None:
            self.event("no-eligible-victim", skipped=choice.skipped[:10])
            self.sm.note_no_kill(now)
            return
        victim, info = choice.victim, choice.victim_info
        key = info.key
        interrupts_backup = rc.FILEBACKUP_SLICE_REL in victim.path.replace(os.sep, "/")
        details = dict(target=info.name, service=info.service, container=victim.container_id[:12],
                       memory_current=victim.current, interrupts_backup=interrupts_backup)
        quarantine = decide_quarantine(self.kill_history, key, now) and key not in self.quarantined
        if not self.acts:
            self.event("would-have-quarantined" if quarantine else "would-have-killed",
                       why=self.mode, kmsg=True, **details)
            self.sm.note_no_kill(now)
            return
        written = self._write_cgroup_kill(victim.path)
        if quarantine:
            # The cgroup.kill still happens so relief does not wait on Docker;
            # what quarantine changes is that the service STAYS down and the
            # kill budget is not spent on it again.
            self.quarantined[key] = {"container": victim.container_id, "since": self.clock.utc(),
                                     "restart": info.restart}
            self.event("quarantined", kmsg=True, cgroup_kill=written, **details)
            self._enqueue("quarantine", key, {"key": key, "container": victim.container_id,
                                              "restart": info.restart})
            self._persist_quarantine()
            self.sm.note_no_kill(now)
        else:
            self.kill_history.append((now, key))
            self.kill_history = [(t, k) for t, k in self.kill_history if now - t < QUARANTINE_WINDOW]
            self.event("kill", kmsg=True, cgroup_kill=written, **details)
            self.sm.note_kill(now)
        if written:
            self.pending_confirm[victim.container_id] = {
                "path": victim.path, "deadline": now + CONFIRM_SECONDS, "key": key}

    def _write_cgroup_kill(self, scope_path: str) -> bool:
        try:
            with open(os.path.join(scope_path, "cgroup.kill"), "w", encoding="ascii") as handle:
                handle.write("1")
            return True
        except OSError as exc:
            self.event("kill-write-failed", path=scope_path, error=str(exc))
            return False

    def _check_confirmations(self, now: float) -> None:
        for cid, pending in list(self.pending_confirm.items()):
            populated = rc.cgroup_populated(pending["path"])
            if populated is False:
                del self.pending_confirm[cid]
                self.event("kill-confirmed", container=cid[:12], note="cgroup.events populated 0")
                self._queue_reconcile(cid, pending, now)
            elif now >= pending["deadline"]:
                del self.pending_confirm[cid]
                self.event("kill-did-not-take", container=cid[:12], kmsg=True,
                           note="cgroup still populated {} s after cgroup.kill".format(CONFIRM_SECONDS))

    def _queue_reconcile(self, cid: str, pending: dict, now: float) -> None:
        entry = self.pending_reconcile.setdefault(cid, {"path": pending["path"], "attempts": 0,
                                                        "next": now})
        entry["next"] = now
        self._retry_reconciles(now)

    def _retry_reconciles(self, now: float) -> None:
        for cid, entry in list(self.pending_reconcile.items()):
            if ("reconcile", cid) in self.inflight or now < entry["next"]:
                continue
            if entry["attempts"] >= RECONCILE_MAX_ATTEMPTS:
                del self.pending_reconcile[cid]
                self.event("reconcile-abandoned", container=cid[:12])
                continue
            entry["attempts"] += 1
            entry["next"] = now + RECONCILE_RETRY_SECONDS
            self._enqueue("reconcile", cid, {"key": cid, "container": cid, "path": entry["path"]})

    def _enqueue(self, kind: str, key: str, payload: dict) -> bool:
        """Queue an action unless the same (kind, key) is already in flight."""
        if (kind, key) in self.inflight:
            return False
        self.inflight.add((kind, key))
        self.workers.enqueue_action(kind, payload)
        return True

    def _drain_results(self, now: float) -> None:
        while True:
            try:
                result = self.workers.results.popleft()
            except IndexError:
                return
            kind, key = result.get("kind"), result.get("key")
            if kind == "brake":
                self._apply_brake(result)
                continue
            if kind != "reboot":  # a reboot is never re-queued, whatever happened
                self.inflight.discard((kind, key))
            if kind == "reconcile" and result.get("ok"):
                self.pending_reconcile.pop(key, None)
            self.event("action-result", **{k: v for k, v in result.items() if k != "ok"},
                       ok=bool(result.get("ok")))

    def _read_released(self) -> None:
        # tmpfs, written by `homehub-recovery release`. Each line is a service.
        try:
            with open(self.paths.released_file, encoding="utf-8") as handle:
                names = [ln.strip() for ln in handle if ln.strip()]
            os.unlink(self.paths.released_file)
        except OSError:
            return
        for name in names:
            self.quarantined.pop(name, None)
            self.kill_history = [(t, k) for t, k in self.kill_history if k != name]
            self.event("released", service=name)
        self._persist_quarantine()

    # ── L2: sshd ────────────────────────────────────────────────────────────
    def _ssh_check(self, now: float) -> None:
        if now < self.next_ssh_probe:
            return
        self.next_ssh_probe = now + SSH_PROBE_SECONDS
        if self.sm.state != NORMAL:
            return  # §5: probes only count while memory is NORMAL
        if self.sshd_probe():
            self.ssh_fails = 0
            return
        self.ssh_fails += 1
        if self.ssh_fails < self.cfg.ssh_fails:
            return
        self.ssh_fails = 0
        if not self.acts:
            self.event("would-have-restarted-ssh", why=self.mode, kmsg=True)
        elif self._enqueue("restart_ssh", "ssh", {"key": "ssh"}):
            self.event("restart-ssh", failures=self.cfg.ssh_fails, kmsg=True)

    # ── L3 / L4 ─────────────────────────────────────────────────────────────
    def _reboot(self, now: float, reason: str) -> None:
        if self.mode != "full":
            self.event("would-have-rebooted", why=self.mode, reason=reason, kmsg=True)
            return
        if self.reboot_requested_at is not None:
            return
        t0_utc = self.clock.utc() - (now - (self.sm.t0 if self.sm.t0 is not None else now))
        intent = {"boot_id": self.boot_id, "utc": self.clock.utc(), "reason": reason,
                  "t0_utc": round(t0_utc, 1), "interrupts_backup": self._backup_running()}
        try:
            os.makedirs(self.paths.run_dir, exist_ok=True)
            rc.atomic_write(self.paths.intent_file, json.dumps(intent).encode("utf-8"), durable=False)
        except OSError as exc:
            self.event("intent-write-failed", error=str(exc))
        self.reboot_requested_at = now
        self.event("reboot", kmsg=True, **intent)
        self._enqueue("reboot", "reboot", {"key": "reboot"})

    def _backup_running(self) -> bool:
        path = os.path.join(self.paths.cgroup_root, rc.FILEBACKUP_SLICE_REL)
        return bool(rc.cgroup_populated(path))

    def _escalation(self, now: float) -> None:
        if (self.reboot_requested_at is not None and self.petting
                and now - self.reboot_requested_at >= ESCALATION_SECONDS):
            self.petting = False
            self.event("escalation", kmsg=True,
                       note="no SIGTERM {} s after the reboot request; petting stopped, L4 resets "
                            "within {} s".format(ESCALATION_SECONDS, self.cfg.hw_timeout))

    # ── disarm (SIGUSR1) and SIGTERM (§3.4, §3.6; review C2) ───────────────
    def consume_disarm(self) -> bool:
        """True only if /run names THIS boot and THIS invocation. Always deleted."""
        try:
            with open(self.paths.disarm_file, encoding="ascii") as handle:
                text = handle.read().split()
        except OSError:
            return False
        try:
            os.unlink(self.paths.disarm_file)
        except OSError:
            pass
        return len(text) >= 2 and text[0] == self.boot_id and text[1] == self.invocation_id

    def on_disarm_request(self) -> None:
        """SIGUSR1 from `homehub-recovery disarm`: disarm, VERIFY, acknowledge.

        The device is stopped while this process keeps running, and the
        acknowledgement says ok only when sysfs reads inactive (or there is no
        device). The CLI stops the unit only after reading ok=true - so a stop
        can never be the thing that leaves a timer running, and a disarm that
        failed leaves the supervisor running and petting (review C2).
        Implements: SR-051, LLR-1002, LLR-1010
        """
        if not self.consume_disarm():
            self._write_ack(False, self.device_state(), "no authorisation naming this boot and invocation")
            self.event("disarm-refused", kmsg=True)
            return
        ok = self._release_device_verified() if self.device is not None else \
            self.device_state() in ("inactive", "absent")
        state = self.device_state()
        if ok:
            self.mode = "disarmed"
            self.reasons.append("disarmed by the operator")
        self._write_ack(ok, state, "verified" if ok else "device still {}; kept armed".format(state))
        self.event("disarmed" if ok else "disarm-failed", kmsg=True, device_state=state)
        self._write_status(self.clock.monotonic())

    def _write_ack(self, ok: bool, state: str, detail: str) -> None:
        ack = {"boot_id": self.boot_id, "invocation_id": self.invocation_id, "ok": ok,
               "device_state": state, "detail": detail, "utc": self.clock.utc()}
        try:
            os.makedirs(self.paths.run_dir, exist_ok=True)
            rc.atomic_write(self.paths.disarm_ack_file, json.dumps(ack).encode(), durable=False)
        except OSError as exc:
            self.kmsg.write("could not write the disarm acknowledgement: {!r}".format(exc))

    def on_sigterm(self) -> None:
        """Every stop that is not preceded by a verified disarm: the HAND-OFF.

        Raise the timeout to the shutdown bound, pet once, close WITHOUT the
        magic character. A reboot, `systemctl restart`, and a stop issued by
        anything but the disarm protocol all land here; none of them disarms.
        """
        if self.device is not None:
            try:
                self.device.set_timeout(self.cfg.shutdown_seconds)
                self.device.keepalive()
                self.device.close()
                self.event("handed-off", kmsg=True, timeout=self.cfg.shutdown_seconds,
                           note="device left running for the shutdown; no magic close")
            except Exception as exc:  # noqa: BLE001 - the exit must happen regardless
                self.kmsg.write("SIGTERM device step failed: {!r}".format(exc))
            self.device = None
        else:
            self.event("stop", mode=self.mode)
        deadline = time.monotonic() + 1.0
        while self.workers.telemetry and time.monotonic() < deadline:
            time.sleep(0.05)
        # os._exit: a telemetry thread blocked in fsync must not hold the
        # interpreter open (§3.5).
        self.exit_fn(0)

    # ── telemetry ───────────────────────────────────────────────────────────
    def event(self, action: str, kmsg: bool = False, **details) -> None:
        """Record one event: kmsg now (main thread, non-blocking), the rest queued."""
        if kmsg:
            self.kmsg.write("{} {}".format(action, " ".join(
                "{}={}".format(k, v) for k, v in sorted(details.items()) if v not in (None, ""))))
        item = {"type": "event", "action": action, "utc": self.clock.utc(),
                "boot_id": self.boot_id, "invocation_id": self.invocation_id,
                "mode": self.mode, "details": details}
        if kmsg:  # only actions get an evidence file; routine lines go to the journal
            item["evidence"] = True
            item["samples"] = self.ring_snapshot()
        self.workers.emit(item)

    def _execute_telemetry(self, item: dict) -> None:
        kind = item.get("type")
        if kind == "inventory":
            self.inventory = rc.read_inventory(self.paths.docker_containers_dir)
            return
        if kind == "quarantine-file":
            self._write_quarantine_snapshot(item)
            return
        if kind == "brake-check":
            self._brake_check()
            return
        line = {k: v for k, v in item.items() if k not in ("type", "samples", "evidence")}
        self.journal(json.dumps(line, sort_keys=True, default=str))
        if item.get("evidence"):
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(item["utc"]))
            name = "{}-{}-{}.json".format(stamp, self.boot_id[:8], item["action"])
            os.makedirs(self.paths.events_dir, exist_ok=True)
            rc.atomic_write(os.path.join(self.paths.events_dir, name),
                            json.dumps(item, sort_keys=True, default=str).encode())

    def _persist_quarantine(self) -> None:
        """Main thread: queue a NUMBERED snapshot of the quarantine set."""
        self.quarantine_seq += 1
        self.workers.emit({"type": "quarantine-file", "seq": self.quarantine_seq,
                           "quarantined": dict(self.quarantined)})

    def _write_quarantine_snapshot(self, item: dict) -> None:
        """Telemetry thread: one writer at a time, and the newest snapshot wins.

        Review C6: two telemetry threads could each hold a quarantine snapshot;
        an older one stalled in fsync could land AFTER a newer one and put a
        released (or a newly quarantined) service back the way it was. The lock
        serialises writers - only telemetry threads take it, never the main
        thread - and a snapshot older than the last one written is dropped.
        """
        with self._quarantine_lock:
            if item["seq"] <= self._quarantine_written_seq:
                return
            os.makedirs(self.paths.state_dir, exist_ok=True)
            rc.atomic_write(self.paths.quarantine_file,
                            json.dumps({"quarantined": item["quarantined"], "seq": item["seq"]},
                                       sort_keys=True).encode())
            self._quarantine_written_seq = item["seq"]

    @staticmethod
    def _stdout_journal(line: str) -> None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def _write_status(self, now: float) -> None:
        red = (self.mode in ("report-only", "disarmed") or bool(self.quarantined) or self.dying
               or bool(self.arm_error))
        status = {
            "boot_id": self.boot_id, "invocation_id": self.invocation_id,
            "mode": self.mode, "reasons": self.reasons, "health": "red" if red else "green",
            "braked": self.braked, "armed": self.device is not None, "petting": self.petting,
            "arm_error": self.arm_error,
            "state": self.sm.state, "episode_age": None if self.sm.t0 is None else round(now - self.sm.t0),
            "kill_budget_left": self.sm.budget_left(now), "quarantined": sorted(self.quarantined),
            "reboot_requested": self.reboot_requested_at is not None,
            "last_sample": self.last_sample, "telemetry_dropped": self.workers.telemetry_dropped,
            "utc": self.clock.utc(),
        }
        try:
            os.makedirs(self.paths.run_dir, exist_ok=True)
            rc.atomic_write(self.paths.status_json, json.dumps(status, default=str).encode(),
                            durable=False)
        except OSError:
            pass

    # ── actions (action thread) ─────────────────────────────────────────────
    def _execute_action(self, kind: str, payload: dict) -> dict:
        if kind == "reboot":
            code, out = self.runner(["systemctl", "reboot"], 30)
            return {"ok": code == 0, "detail": out.strip()[:200], "rc": code}
        if kind == "restart_ssh":
            code, out = self.runner(["systemctl", "restart", "ssh"], 60)
            return {"ok": code == 0, "detail": out.strip()[:200], "rc": code}
        if kind == "quarantine":
            cid = payload["container"]
            c1, o1 = self.runner(["docker", "update", "--restart=no", cid], 20)
            c2, o2 = self.runner(["docker", "stop", "-t", "10", cid], 35)
            return {"ok": c1 == 0 and c2 == 0, "rc": [c1, c2],
                    "detail": (o1.strip() + " | " + o2.strip())[:200]}
        if kind == "reconcile":
            return self._reconcile(payload)
        return {"ok": False, "detail": "unknown action {}".format(kind)}

    def _reconcile(self, payload: dict) -> dict:
        """§3.2 step 4: does Docker agree the container is gone? Bounded to ~60 s."""
        cid = payload["container"]
        fmt = ("{{.State.Running}}|{{.HostConfig.RestartPolicy.Name}}|{{.HostConfig.AutoRemove}}|"
               '{{index .Config.Labels "com.docker.compose.service"}}')
        code, out = self.runner(["docker", "inspect", "--format", fmt, cid], 20)
        if code != 0:
            if "No such object" in out or "no such container" in out.lower():
                return {"ok": True, "detail": "container already removed"}
            return {"ok": False, "detail": "docker did not answer ({}); retried next cooldown".format(code)}
        running, policy, auto_remove, service = (out.strip().split("|") + ["", "", "", ""])[:4]
        result = {"service": service, "restart": policy}
        if running != "true" or rc.cgroup_populated(payload["path"]):
            result.update(ok=True, detail="docker and the cgroup agree")
            return result
        # The 10-04 ghost: Docker says running, the cgroup is empty.
        if auto_remove == "true" or policy in ("", "no"):
            code, out = self.runner(["docker", "rm", "-f", cid], 35)
            result.update(ok=code == 0, detail="ghost removed: " + out.strip()[:120], ghost=True)
        else:
            code, out = self.runner(["docker", "restart", cid], 35)
            result.update(ok=code == 0, detail="ghost restarted: " + out.strip()[:120], ghost=True)
        return result


# ── entry point ──────────────────────────────────────────────────────────────


def main() -> int:
    paths = rc.Paths("/")
    # Block the stop signals BEFORE any thread exists so every worker inherits
    # the mask and SIGTERM can only be collected by the main loop's sigtimedwait.
    signals = {signal.SIGTERM, signal.SIGINT, SIG_DISARM}
    signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    try:
        cfg = rc.load_config(paths, os.environ)
        config_error = None
    except rc.ConfigError as exc:
        cfg, config_error = rc.parse_config({}), str(exc)
    boot_id = rc.read_boot_id(paths)
    supervisor = Supervisor(cfg, paths, boot_id, os.environ.get("INVOCATION_ID", ""),
                            device_path=os.environ.get("HUB_WD_DEVICE", "/dev/watchdog0"))
    if config_error:
        supervisor.reasons.append("config: " + config_error)
    supervisor.setup()

    def wait(timeout: float) -> Optional[int]:
        info = signal.sigtimedwait(signals, timeout)
        return None if info is None else info.si_signo

    supervisor.run(wait)
    return 0


if __name__ == "__main__":
    sys.exit(main())
