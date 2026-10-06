"""Shared core of the hub recovery capability: config, paths, durable writes,
boot accounting arithmetic, and the read-only views of cgroups and Docker.

ONE FACT, ONE HOME. The supervisor, the boot classifier, the pressure sampler
and the CLI all need the same knob rules, the same atomic write, the same brake
arithmetic and the same answer to "which cgroups are containers". Each of those
lives here exactly once; the four entry points import it.

Pure where it can be. Everything that DECIDES (config validation, the brake,
D10's counting policy, classification, victim choice inputs) is a pure function
of its arguments and is unit-tested without a filesystem. The few functions that
touch the filesystem take every path as an argument so a test can point them at
a temporary tree.

Python 3.12 on the hub; this file also imports under 3.8 so the Windows dev box
can run the hermetic tests (no `match`, no runtime `X | Y` types).

Implements: SR-049, SR-050, SR-051, LLR-994, LLR-995, LLR-1001
"""

from __future__ import annotations

import itertools
import json
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# ── paths ────────────────────────────────────────────────────────────────────
# Every path the capability touches, in one place, with a root prefix so a test
# can run the real code against a temporary tree. Production uses root="/".


@dataclass(frozen=True)
class Paths:
    """Filesystem locations used by every recovery component.

    Contract:
      Inputs:  root: str — prefix prepended to every absolute path ("/" in
               production; a tmp dir in tests).
      Outputs: attribute paths (str). Nothing is created.
    """

    root: str = "/"

    def _p(self, *parts: str) -> str:
        return os.path.join(self.root, *[p.lstrip("/") for p in parts])

    @property
    def state_dir(self) -> str:  # durable records (boots.log, stamp, events)
        return self._p("var/lib/homehub-recovery")

    @property
    def run_dir(self) -> str:  # tmpfs: intent, disarm, boot.json, status.json
        return self._p("run/homehub-recovery")

    @property
    def conf_file(self) -> str:  # written by setup-recovery.sh from .env
        return self._p("etc/homehub-recovery/recovery.env")

    @property
    def events_dir(self) -> str:
        return os.path.join(self.state_dir, "events")

    @property
    def boots_log(self) -> str:
        return os.path.join(self.state_dir, "boots.log")

    @property
    def stamp_file(self) -> str:
        return os.path.join(self.state_dir, "stamp.json")

    @property
    def last_boot_file(self) -> str:
        return os.path.join(self.state_dir, "last-boot")

    @property
    def pstore_seen_file(self) -> str:
        return os.path.join(self.state_dir, "pstore-seen.json")

    @property
    def bootstatus_proven_file(self) -> str:
        return os.path.join(self.state_dir, "bootstatus-proven")

    @property
    def quarantine_file(self) -> str:
        return os.path.join(self.state_dir, "quarantine.json")

    @property
    def intent_file(self) -> str:
        return os.path.join(self.run_dir, "intent")

    @property
    def disarm_file(self) -> str:
        return os.path.join(self.run_dir, "disarm")

    @property
    def released_file(self) -> str:
        return os.path.join(self.run_dir, "released")

    @property
    def boot_json(self) -> str:
        return os.path.join(self.run_dir, "boot.json")

    @property
    def status_json(self) -> str:
        return os.path.join(self.run_dir, "status.json")

    @property
    def boot_id_file(self) -> str:
        return self._p("proc/sys/kernel/random/boot_id")

    @property
    def kernel_panic_file(self) -> str:
        return self._p("proc/sys/kernel/panic")

    @property
    def proc_dir(self) -> str:
        return self._p("proc")

    @property
    def cgroup_root(self) -> str:
        return self._p("sys/fs/cgroup")

    @property
    def watchdog_class_dir(self) -> str:
        return self._p("sys/class/watchdog")

    @property
    def pstore_dirs(self) -> Tuple[str, str]:
        # The live pstore filesystem, and where systemd-pstore.service archives
        # records at boot (it MOVES them, so both must be read).
        return (self._p("sys/fs/pstore"), self._p("var/lib/systemd/pstore"))

    @property
    def docker_containers_dir(self) -> str:
        return self._p("var/lib/docker/containers")


# ── config ───────────────────────────────────────────────────────────────────


class ConfigError(ValueError):
    """A knob holds a value the capability refuses. The message names the knob."""


# Knob -> default, as strings exactly as they would appear in .env. The
# defaults are the design's §5 table (HUB_HANG_WATCHDOG_DESIGN_2026-10-05.md).
# HUB_WD_TIERS and HUB_WD_RESET_DEVICE are additions this implementation needed
# to stage P3 (contain) apart from P4 (full) and to pin the reset device; both
# default to the cautious value.
KNOB_DEFAULTS: Dict[str, str] = {
    "HUB_WATCHDOG_ENABLED": "false",
    "HUB_WD_TIERS": "contain",
    "HUB_WD_PSI_FULL": "40",
    "HUB_WD_KILL_SECONDS": "120",
    "HUB_WD_KILL_MEASURED_PCT": "75",
    "HUB_WD_REBOOT_SECONDS": "600",
    "HUB_WD_SSH_FAILS": "3",
    "HUB_WD_MAX_RECOVERY_BOOTS": "3",
    "HUB_WD_PROTECT": "caddy,technitium",
    "HUB_WD_HW_TIMEOUT": "60",
    "HUB_WD_SHUTDOWN_SECONDS": "300",
    "HUB_WD_RESET_DEVICE": "auto",
    "HUB_OOM_MEM_PCT": "5,2",
    "HUB_OOM_SWAP_PCT": "10,5",
}

TIERS = ("contain", "full")
RESET_DEVICES = ("auto", "wdat_wdt", "iTCO_wdt", "softdog", "none")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class Config:
    """Validated knobs. Construct with parse_config(); never by hand in prod.

    Contract (see stack/.env.example "Hub recovery" block for the meaning):
      enabled: bool; tiers: "contain" | "full";
      psi_full: float percent (0, 100]; kill_seconds: int seconds;
      kill_measured_pct: int percent; reboot_seconds: int seconds > kill;
      ssh_fails: int; max_recovery_boots: int; protect: tuple of names;
      hw_timeout / shutdown_seconds: int seconds; reset_device: enum;
      oom_mem_pct / oom_swap_pct: (term, kill) int percents, kill <= term.
    Implements: SR-050, LLR-1001
    """

    enabled: bool
    tiers: str
    psi_full: float
    kill_seconds: int
    kill_measured_pct: int
    reboot_seconds: int
    ssh_fails: int
    max_recovery_boots: int
    protect: Tuple[str, ...]
    hw_timeout: int
    shutdown_seconds: int
    reset_device: str
    oom_mem_pct: Tuple[int, int]
    oom_swap_pct: Tuple[int, int]

    @property
    def full(self) -> bool:
        """True when L3 (graceful reboot), L4 (device) and L5 (panic) are in play."""
        return self.enabled and self.tiers == "full"


def _int(env: Mapping[str, str], key: str, lo: int, hi: int) -> int:
    raw = env[key]
    if not re.fullmatch(r"[0-9]+", raw):
        raise ConfigError("{}={!r} is not a whole number".format(key, raw))
    value = int(raw)
    if not lo <= value <= hi:
        raise ConfigError("{}={} is outside {}..{}".format(key, value, lo, hi))
    return value


def _pct_pair(env: Mapping[str, str], key: str) -> Tuple[int, int]:
    raw = env[key]
    m = re.fullmatch(r"([0-9]{1,2}),([0-9]{1,2})", raw)
    if not m:
        raise ConfigError("{}={!r} must be TERM,KILL percents, e.g. 5,2".format(key, raw))
    term, kill = int(m.group(1)), int(m.group(2))
    if not (1 <= kill <= term <= 50):
        raise ConfigError(
            "{}={} needs 1 <= KILL <= TERM <= 50 (earlyoom sends SIGKILL at the lower one)".format(
                key, raw
            )
        )
    return term, kill


def parse_config(env: Mapping[str, str]) -> Config:
    """Validate the knobs and return a Config, or raise ConfigError.

    Contract:
      Inputs:  env — any mapping of KEY -> str (os.environ, or a parsed .env).
               Absent or EMPTY keys take KNOB_DEFAULTS, because a materialised
               .env that predates a knob must not change behaviour.
      Outputs: Config.
      Raises:  ConfigError naming the first knob that is wrong. setup-recovery
               turns it into a refusal (exit 2); nothing guesses a fix.
    Implements: SR-050, LLR-1001
    """
    merged = dict(KNOB_DEFAULTS)
    for key in KNOB_DEFAULTS:
        value = (env.get(key) or "").strip().strip('"').strip("'")
        if value:
            merged[key] = value

    enabled_raw = merged["HUB_WATCHDOG_ENABLED"].lower()
    if enabled_raw not in ("true", "false"):
        raise ConfigError("HUB_WATCHDOG_ENABLED={!r} must be true or false".format(enabled_raw))
    tiers = merged["HUB_WD_TIERS"]
    if tiers not in TIERS:
        raise ConfigError("HUB_WD_TIERS={!r} must be one of {}".format(tiers, "|".join(TIERS)))
    reset_device = merged["HUB_WD_RESET_DEVICE"]
    if reset_device not in RESET_DEVICES:
        raise ConfigError(
            "HUB_WD_RESET_DEVICE={!r} must be one of {}".format(reset_device, "|".join(RESET_DEVICES))
        )
    psi_raw = merged["HUB_WD_PSI_FULL"]
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", psi_raw) or not 0 < float(psi_raw) <= 100:
        raise ConfigError("HUB_WD_PSI_FULL={!r} must be a percent in (0, 100]".format(psi_raw))
    protect = tuple(p.strip() for p in merged["HUB_WD_PROTECT"].split(",") if p.strip())
    for name in protect:
        if not _NAME_RE.match(name):
            raise ConfigError("HUB_WD_PROTECT entry {!r} is not a container/service name".format(name))

    kill_seconds = _int(merged, "HUB_WD_KILL_SECONDS", 30, 3600)
    reboot_seconds = _int(merged, "HUB_WD_REBOOT_SECONDS", 120, 86400)
    if reboot_seconds <= kill_seconds:
        raise ConfigError(
            "HUB_WD_REBOOT_SECONDS={} must exceed HUB_WD_KILL_SECONDS={}: the reboot is the "
            "escalation after kills, not instead of them".format(reboot_seconds, kill_seconds)
        )
    return Config(
        enabled=enabled_raw == "true",
        tiers=tiers,
        psi_full=float(psi_raw),
        kill_seconds=kill_seconds,
        kill_measured_pct=_int(merged, "HUB_WD_KILL_MEASURED_PCT", 50, 100),
        reboot_seconds=reboot_seconds,
        ssh_fails=_int(merged, "HUB_WD_SSH_FAILS", 1, 100),
        max_recovery_boots=_int(merged, "HUB_WD_MAX_RECOVERY_BOOTS", 1, 100),
        protect=protect,
        hw_timeout=_int(merged, "HUB_WD_HW_TIMEOUT", 10, 600),
        shutdown_seconds=_int(merged, "HUB_WD_SHUTDOWN_SECONDS", 60, 3600),
        reset_device=reset_device,
        oom_mem_pct=_pct_pair(merged, "HUB_OOM_MEM_PCT"),
        oom_swap_pct=_pct_pair(merged, "HUB_OOM_SWAP_PCT"),
    )


def render_config(cfg: Config) -> str:
    """The normalised KEY=VALUE file setup-recovery.sh installs as recovery.env."""
    lines = [
        "# Written by stack/recovery/setup-recovery.sh from stack/.env. Do not edit:",
        "# change .env and re-run setup-recovery.sh (firstboot does it every boot).",
        "HUB_WATCHDOG_ENABLED={}".format("true" if cfg.enabled else "false"),
        "HUB_WD_TIERS={}".format(cfg.tiers),
        "HUB_WD_PSI_FULL={:g}".format(cfg.psi_full),
        "HUB_WD_KILL_SECONDS={}".format(cfg.kill_seconds),
        "HUB_WD_KILL_MEASURED_PCT={}".format(cfg.kill_measured_pct),
        "HUB_WD_REBOOT_SECONDS={}".format(cfg.reboot_seconds),
        "HUB_WD_SSH_FAILS={}".format(cfg.ssh_fails),
        "HUB_WD_MAX_RECOVERY_BOOTS={}".format(cfg.max_recovery_boots),
        "HUB_WD_PROTECT={}".format(",".join(cfg.protect)),
        "HUB_WD_HW_TIMEOUT={}".format(cfg.hw_timeout),
        "HUB_WD_SHUTDOWN_SECONDS={}".format(cfg.shutdown_seconds),
        "HUB_WD_RESET_DEVICE={}".format(cfg.reset_device),
        "HUB_OOM_MEM_PCT={},{}".format(*cfg.oom_mem_pct),
        "HUB_OOM_SWAP_PCT={},{}".format(*cfg.oom_swap_pct),
    ]
    return "\n".join(lines) + "\n"


def read_env_file(path: str) -> Dict[str, str]:
    """Parse a KEY=VALUE file without executing it. Missing file -> {}."""
    out: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return out


def load_config(paths: Paths, environ: Optional[Mapping[str, str]] = None) -> Config:
    """Config from the installed recovery.env, overlaid by the process env.

    systemd passes the same file as EnvironmentFile=, so in a unit the two
    agree; reading the file as well lets the CLI and the boot unit work from a
    shell with no environment."""
    merged = read_env_file(paths.conf_file)
    if environ:
        merged.update({k: v for k, v in environ.items() if k in KNOB_DEFAULTS})
    return parse_config(merged)


# ── durable writes ───────────────────────────────────────────────────────────
# "Atomic" here means all four steps, because each one closes a different hole:
#   write a temp file -> fsync it -> rename over the target -> fsync the DIRECTORY.
# Without the directory fsync the rename itself can be lost on a hard reset,
# which is exactly the event boot accounting exists to survive.


def _fsync_dir(directory: str) -> None:
    if os.name == "nt":
        # Windows cannot open a directory at all. Only the dev box's hermetic
        # tests ever take this branch; the hub is Linux, and the Linux run of
        # the same tests (WSL / CI) exercises the real directory fsync.
        return
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


_TMP_COUNTER = itertools.count()


def atomic_write(path: str, data: bytes, mode: int = 0o644, durable: bool = True) -> None:
    """Replace `path` with `data` so a reader sees the old or the new file, never half.

    Contract:
      Inputs:  durable=False skips both fsyncs (tmpfs files under /run, where a
               sync is meaningless and only adds a way to block).
      Raises:  OSError (ENOSPC, EROFS, EIO...). On failure the target is
               untouched and the temp file is removed where possible.
    Implements: SR-049, LLR-995
    """
    directory = os.path.dirname(path) or "."
    # UNIQUE AND EXCLUSIVE (review C6): pid + thread + a process-wide counter,
    # opened O_EXCL. The supervisor has two telemetry threads; with a pid-only
    # name they truncated and renamed each other's temp file.
    tmp = "{}.tmp.{}.{}.{}".format(path, os.getpid(), threading.get_ident(), next(_TMP_COUNTER))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            if durable:
                os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if durable:
        _fsync_dir(directory)


def atomic_delete(path: str) -> None:
    """Unlink `path` (absent is fine) and fsync its directory so the delete holds."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    _fsync_dir(os.path.dirname(path) or ".")


def read_json(path: str) -> Optional[dict]:
    """A JSON object from `path`, or None if absent or unreadable/corrupt."""
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_boot_id(paths: Paths) -> str:
    with open(paths.boot_id_file, encoding="ascii") as handle:
        return handle.read().strip().replace("-", "")


# ── boot accounting: classes, D10 policy, brake ─────────────────────────────

CLASS_CLEAN = "clean"
CLASS_RECOVERY = "recovery"
CLASS_WATCHDOG = "watchdog"
CLASS_PANIC = "panic"
CLASS_UNEXPLAINED = "unexplained"
# Not one of the five: the first boot on which the capability ever ran has no
# previous record to classify against, and counting it would charge the brake
# for installing the feature.
CLASS_FIRST = "first"

WDIOF_CARDRESET = 0x0020  # linux/watchdog.h: "Card previously reset the CPU"
BRAKE_WINDOW_SECONDS = 24 * 3600


def classify_previous_boot(
    stamp: Optional[dict],
    previous_boot_id: Optional[str],
    bootstatus: Optional[int],
    new_pstore_records: Sequence[str],
) -> Tuple[str, str]:
    """Which of the five classes the PREVIOUS boot ended as.

    Contract:
      Inputs:  stamp — the clean-shutdown stamp found on disk, or None.
               previous_boot_id — the boot this one follows (None if unknown).
               bootstatus — WDIOF_* bits read from the watchdog, or None when
               no watchdog is present to ask.
               new_pstore_records — pstore entries not seen before.
      Outputs: (class, human reason).
    Order is the design's §4 table plus its one override: a WDIOF_CARDRESET
    report wins even over a stamp, because a hang AFTER the stamp was written
    and reset by L4 is still a watchdog reset when the firmware says so.
    Implements: SR-049, LLR-994
    """
    if bootstatus is not None and bootstatus & WDIOF_CARDRESET:
        return CLASS_WATCHDOG, "watchdog bootstatus reports WDIOF_CARDRESET"
    stamp_valid = bool(stamp) and previous_boot_id is not None and stamp.get("boot_id") == previous_boot_id
    if stamp_valid:
        if stamp.get("intent"):
            reason = str(stamp["intent"].get("reason", "?")) if isinstance(stamp["intent"], dict) else "?"
            return CLASS_RECOVERY, "clean stamp carries a recovery intent ({})".format(reason)
        return CLASS_CLEAN, "clean-shutdown stamp for the previous boot"
    if new_pstore_records:
        return CLASS_PANIC, "pstore record(s): {}".format(", ".join(sorted(new_pstore_records)[:3]))
    if previous_boot_id is None:
        return CLASS_FIRST, "no previous boot recorded: the capability's first boot"
    detail = "no stamp" if not stamp else "stamp names another boot ({})".format(stamp.get("boot_id"))
    return CLASS_UNEXPLAINED, detail + "; no watchdog report and no pstore record"


def counted_classes(bootstatus_proven: bool) -> Tuple[str, ...]:
    """D10: which classes cost brake budget.

    If this board has ever reported WDIOF_CARDRESET, watchdog resets are
    identifiable, so an unexplained boot is a power cut or a hang the firmware
    never saw, and it costs nothing. Until then a watchdog reset and a power cut
    look identical and unexplained boots MUST be counted.
    Implements: SR-051, LLR-997
    """
    base = (CLASS_RECOVERY, CLASS_WATCHDOG, CLASS_PANIC)
    return base if bootstatus_proven else base + (CLASS_UNEXPLAINED,)


@dataclass
class BrakeState:
    """Result of brake_state(). `braked` is the only field a decision reads."""

    braked: bool
    counted_recent: int
    last_counted_utc: Optional[float]
    detail: str = ""


def brake_state(
    entries: Iterable[dict], now_utc: float, max_boots: int, bootstatus_proven: bool,
    window: float = BRAKE_WINDOW_SECONDS,
) -> BrakeState:
    """Is the loop brake on?

    Contract:
      Inputs:  entries — parsed boots.log lines: {"t": epoch, "class": str} or
               {"t": epoch, "event": "brake-reset"}.
      Outputs: BrakeState. Pure.
    The brake ENGAGES when `max_boots` counted boots fall inside one 24 h window
    and CLEARS only after 24 h pass with no counted boot (§4) — not when the
    oldest of the three ages out, which would re-arm a box still looping at a
    23 h cadence. A brake-reset entry discards everything before it.
    Implements: SR-051, LLR-997
    """
    counted = counted_classes(bootstatus_proven)
    ordered = sorted((e for e in entries if isinstance(e.get("t"), (int, float))), key=lambda e: e["t"])
    times: List[float] = []
    braked = False
    for entry in ordered:
        if entry.get("event") == "brake-reset":
            times, braked = [], False
            continue
        if entry.get("class") not in counted:
            continue
        t = float(entry["t"])
        if braked and times and t - times[-1] >= window:
            braked = False
        times.append(t)
        if sum(1 for x in times if t - x < window) >= max_boots:
            braked = True
    last = times[-1] if times else None
    if braked and last is not None and now_utc - last >= window:
        braked = False
    recent = sum(1 for x in times if now_utc - x < window)
    detail = "{} counted boot(s) in 24 h, brake at {}".format(recent, max_boots)
    return BrakeState(braked=braked, counted_recent=recent, last_counted_utc=last, detail=detail)


class AccountingError(Exception):
    """boots.log exists but cannot be read or validated. Decisions fail CLOSED."""


def read_boots_log_strict(path: str) -> List[dict]:
    """Every boots.log entry, or AccountingError - never a silently shorter history.

    Contract:
      Outputs: [] ONLY when the file does not exist (the capability's first
               install). Any other OSError (EIO, EACCES...), a line that is not
               JSON, or a line that is not an object with a numeric "t" raises.
    WHY STRICT (review C4): the lenient reader turned an unreadable log into an
    EMPTY history, and an empty history is "not braked" - so a disk error
    released the brake and let the supervisor arm and reboot. Whatever decides
    the brake reads through here and treats AccountingError as braked and
    uncommitted.
    Implements: SR-051, LLR-997, LLR-1009
    """
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise AccountingError("cannot read {}: {}".format(path, exc))
    out = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            raise AccountingError("{} line {} is not JSON".format(path, number))
        if not isinstance(value, dict) or not isinstance(value.get("t"), (int, float)):
            raise AccountingError("{} line {} is not a boot record".format(path, number))
        out.append(value)
    return out


def evaluate_brake(paths: "Paths", cfg: "Config", now_utc: float) -> "BrakeState":
    """The brake as the durable accounting says it is NOW. Raises AccountingError.

    One home for "read the log and decide", used by the boot classifier and by
    the running supervisor's periodic re-evaluation (review C9).
    Implements: SR-051, LLR-997, LLR-1009
    """
    entries = read_boots_log_strict(paths.boots_log)
    proven = os.path.exists(paths.bootstatus_proven_file)
    return brake_state(entries, now_utc, cfg.max_recovery_boots, proven)


def parse_boots_log(text: str) -> List[dict]:
    """boots.log for DISPLAY only (the CLI's status): a corrupt line is skipped.

    Never use this for a decision; read_boots_log_strict is the fail-closed one."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


BOOTS_LOG_KEEP = 500  # lines; the brake needs 24 h, a human wants months


def append_boots_log(path: str, entry: dict) -> None:
    """Append one JSON line by rewriting the WHOLE file atomically (§4 step 2).

    An O_APPEND write can tear across a reset; a full rewrite through
    atomic_write cannot. The file is bounded so the rewrite stays cheap."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = [ln for ln in handle.read().splitlines() if ln.strip()]
    except FileNotFoundError:
        lines = []
    lines.append(json.dumps(entry, sort_keys=True))
    lines = lines[-BOOTS_LOG_KEEP:]
    atomic_write(path, ("\n".join(lines) + "\n").encode("utf-8"))


def kernel_panic_value(cfg: Config, committed: bool, braked: bool) -> int:
    """kernel.panic for this boot: 10 only when it is safe to let a panic reboot.

    0 (stay down) whenever the knob or the full tier is off, the accounting
    could not commit (so the brake cannot be trusted), or the brake is on — a
    panicking box must not loop (R2-2).
    Implements: SR-051, LLR-997
    """
    return 10 if (cfg.full and committed and not braked) else 0


# ── cgroups and Docker, read-only ────────────────────────────────────────────
# Docker's systemd driver on cgroup v2 puts a container in
#   <slice>/docker-<64 hex>.scope
# The default slice is system.slice (verified on the hub 2026-10-05: 20 scopes).
# A compose `cgroup_parent: homehub-filebackup.slice` lands under
# homehub.slice/homehub-filebackup.slice, because systemd nests a dashed slice
# name under its prefix. Both roots are scanned so moving the backup into its own
# slice does NOT take the 10-04 culprit out of the victim set.

SCOPE_RE = re.compile(r"^docker-([0-9a-f]{64})\.scope$")
CONTAINER_ROOTS = ("system.slice", "homehub.slice")
FILEBACKUP_SLICE_REL = "homehub.slice/homehub-filebackup.slice"


@dataclass(frozen=True)
class Scope:
    """One container's cgroup: where it is, whose it is, how big it is now."""

    path: str
    container_id: str
    current: int


def _read_int(path: str) -> Optional[int]:
    try:
        with open(path, encoding="ascii") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def scan_container_scopes(cgroup_root: str, roots: Sequence[str] = CONTAINER_ROOTS) -> List[Scope]:
    """Every docker-<id>.scope under `roots` (two levels deep), with memory.current.

    Reads cgroupfs only (kernfs: never blocks on a disk). A scope whose
    memory.current cannot be read is skipped — it is mid-teardown.
    Implements: SR-050, LLR-998
    """
    found: List[Scope] = []
    for rel in roots:
        base = os.path.join(cgroup_root, rel)
        stack = [(base, 0)]
        while stack:
            directory, depth = stack.pop()
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                full = os.path.join(directory, name)
                m = SCOPE_RE.match(name)
                if m:
                    current = _read_int(os.path.join(full, "memory.current"))
                    if current is not None:
                        found.append(Scope(full, m.group(1), current))
                elif name.endswith(".slice") and depth < 2:
                    stack.append((full, depth + 1))
    return found


def cgroup_populated(scope_path: str) -> Optional[bool]:
    """cgroup.events `populated` (recursive). A vanished cgroup is empty."""
    try:
        with open(os.path.join(scope_path, "cgroup.events"), encoding="ascii") as handle:
            for line in handle:
                key, _, value = line.partition(" ")
                if key == "populated":
                    return value.strip() == "1"
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return None


@dataclass(frozen=True)
class ContainerInfo:
    """What Docker's on-disk metadata says about one container."""

    name: str
    service: str
    restart: str
    auto_remove: bool

    @property
    def key(self) -> str:
        """The identity quarantine counts by: the compose service, else the name."""
        return self.service or self.name


def read_inventory(containers_dir: str) -> Dict[str, ContainerInfo]:
    """Container id -> ContainerInfo, from Docker's own config files.

    WHY THE FILES AND NOT `docker ps`. The 10-04 wedge left dockerd itself
    unresponsive; a victim chooser that needs the daemon cannot choose when it
    matters. config.v2.json / hostconfig.json are plain files the daemon keeps
    current (layout verified on the hub 2026-10-05). Runs on a WORKER thread:
    it is disk I/O and may block.
    Implements: SR-050, LLR-998
    """
    out: Dict[str, ContainerInfo] = {}
    try:
        ids = os.listdir(containers_dir)
    except OSError:
        return out
    for cid in ids:
        if not re.fullmatch(r"[0-9a-f]{64}", cid):
            continue
        # NO VALID METADATA, NO ENTRY (review C5). A missing, corrupt or
        # unreadable config.v2.json used to become a ContainerInfo with an
        # EMPTY name, which no protect entry can match - so a protected
        # container whose metadata read failed became a candidate victim.
        # Omitted here, the chooser treats it as unidentified and skips it.
        cfg = read_json(os.path.join(containers_dir, cid, "config.v2.json"))
        host = read_json(os.path.join(containers_dir, cid, "hostconfig.json"))
        if cfg is None or host is None:
            continue
        name = cfg.get("Name")
        config_block = cfg.get("Config")
        if not isinstance(name, str) or not name.strip("/") or not isinstance(config_block, dict):
            continue
        labels = config_block.get("Labels") or {}
        if not isinstance(labels, dict):
            continue
        policy = (host.get("RestartPolicy") or {}).get("Name") or "no"
        out[cid] = ContainerInfo(
            name=name.lstrip("/"),
            service=str(labels.get("com.docker.compose.service") or ""),
            restart=str(policy),
            auto_remove=bool(host.get("AutoRemove")),
        )
    return out


def is_protected(info: ContainerInfo, protect: Iterable[str]) -> bool:
    names = set(protect)
    return info.name in names or (bool(info.service) and info.service in names)


@dataclass
class VictimChoice:
    """Output of choose_victim(): at most one victim, plus what was passed over."""

    victim: Optional[Scope] = None
    victim_info: Optional[ContainerInfo] = None
    would_have_killed: Optional[str] = None  # a protected container was largest
    skipped: List[str] = field(default_factory=list)


MIN_VICTIM_BYTES = 256 * 1024 * 1024


def choose_victim(
    scopes: Sequence[Scope], inventory: Mapping[str, ContainerInfo], protect: Iterable[str],
    min_bytes: int = MIN_VICTIM_BYTES,
) -> VictimChoice:
    """Pick the largest eligible container (§3.2), and say who was passed over.

    Contract:
      Inputs:  scopes — container cgroups only. Host processes never reach
               this function, so they can never be chosen.
               inventory — id -> ContainerInfo; an id missing from it is
               UNIDENTIFIED and is skipped: it could be a protected container
               recreated since the last inventory, and D9's protection is worth
               more than one kill (L3 still bounds the episode).
      Outputs: VictimChoice. Pure.
    Implements: SR-050, LLR-998
    """
    protect = tuple(protect)
    choice = VictimChoice()
    identified = []
    for scope in sorted(scopes, key=lambda s: s.current, reverse=True):
        info = inventory.get(scope.container_id)
        if info is None:
            choice.skipped.append("{} (unidentified)".format(scope.container_id[:12]))
            continue
        identified.append((scope, info))
    if identified and is_protected(identified[0][1], protect):
        choice.would_have_killed = identified[0][1].name
    for scope, info in identified:
        if is_protected(info, protect):
            choice.skipped.append("{} (protected)".format(info.name))
            continue
        if scope.current < min_bytes:
            choice.skipped.append("{} (under {} MiB)".format(info.name, min_bytes // (1024 * 1024)))
            continue
        choice.victim, choice.victim_info = scope, info
        break
    return choice


def read_psi_full_avg10(text: str) -> Optional[float]:
    """`full avg10` from /proc/pressure/memory text, or None if absent."""
    for line in text.splitlines():
        if line.startswith("full "):
            m = re.search(r"avg10=([0-9.]+)", line)
            if m:
                return float(m.group(1))
    return None


def read_meminfo(text: str) -> Dict[str, int]:
    """/proc/meminfo as {key: kB}."""
    out: Dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key.strip()] = int(parts[0])
    return out
