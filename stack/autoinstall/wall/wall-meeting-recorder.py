#!/usr/bin/env python3
"""Record a meeting's audio, both directions, on the wall panel (WSN-097).

THE RULING (Owner, 2026-10-22, HomeHub open-items A69): recording starts and
stops from a control on the panel AND automatically whenever the panel carries
a Bluetooth call for the dev PC; an on-screen "recording" indicator is shown
whenever capture runs; recordings go to the hub's NVMe scratch volume (not
backed up, short retention); audio only -- nothing here transcribes, analyses
or uploads anything. The Owner tells the other participants.

WHAT IS CAPTURED (measured on the panel 2026-10-22, HomeHub FOLLOWUPS W6).
Two dsnoop readers, opened next to the consumers that already read them, so a
recording changes nothing anybody hears:
  * the NEAR side: `mic_selected`, the one name every microphone consumer opens
    (the echo-cancelled panel microphone, or the headset's), resolved through
    /run/wall-panel/audio-mic-source.env exactly as the call legs resolve it.
    The input mute is the codec's capture switch, so a muted panel records
    exact silence on this side -- the mute is honoured, not bypassed;
  * the FAR side: `bus_monitor`, the merged output bus before the switch --
    everything the panel plays, which during a Bluetooth call includes the
    gateway's far end (wall-bt-call's leg 1 writes it onto `bus`).
One Opus stream, STEREO WITH ONE DIRECTION PER CHANNEL (left = near, right =
far): one encoder, one file any player opens, both directions audible at
once, and the two sides still separable for the Owner's own review.
Measured cost on the panel: 4.9-5.5 % of one core, with and without a call.

ONE STATE MACHINE (`RecordingMachine`, pure). Intents: idle, manual, call and
manual-call. A manual start during a call joins the call's recording (one
file); a manual stop during a call stops it and suppresses the automatic
trigger until that call ends; repeated call notifications are idempotent; a
capture that fails returns to idle (and suppresses the call that was
recording, so a broken capture cannot loop).

NO INDICATOR, NO CAPTURE. Whenever an intent is not idle this daemon asks the
kiosk for the indicator (state document, `indicator: true`). The kiosk beats an
acknowledgement every 500 ms while the indicator is painted on a lit screen;
capture STARTS only after a beat for the current sequence number has been seen
within ACK_FRESH_S and STOPS within 2 s of the last one (poll POLL_S, freshness
ACK_FRESH_S, SIGINT then SIGKILL after STOP_GRACE_S). A kiosk that crashes,
blanks or freezes simply stops beating.

BOUNDS. The panel spool has a hard cap (WALL_MEETING_SPOOL_MAX_MB) and a free
space floor on the root filesystem (WALL_MEETING_MIN_FREE_MB); every capture
is started with ffmpeg's -fs set to the room that is left, so a recording
cannot pass either. Capture refuses to start when there is no room and the
state document says why, including whether the hub's quota is full too (the
hub reports that through the spool's forced command).

Contract:
  Inputs:  /run/wall-panel/call-state.json (wall-bt-call, aliases only);
           /run/wall-panel-renderer/meeting-request.json and
           meeting-indicator-ack.json (the kiosk); /etc/wall-panel/audio-mode;
           /run/wall-panel/audio-mic-source.env, audio-input-mute.env;
           /var/spool/wall-meeting/hub-state.json (wall-meeting-spool report).
  Outputs: /run/wall-meeting-recorder/state.json (0644, atomic);
           /var/spool/wall-meeting/ready/meeting-<UTC>.opus + .sha256.
  Config:  /etc/wall-panel/meeting-recorder.env (rendered by firstboot from
           wall.env): WALL_MEETING_SPOOL_MAX_MB, WALL_MEETING_MIN_FREE_MB,
           WALL_MEETING_MAX_MINUTES, WALL_MEETING_BITRATE_KBPS,
           WALL_MEETING_CALL_GATEWAYS.
  Raises:  nothing at run time; every failure is a journal line and a state
           field the kiosk shows.
Implements: SR-108, SR-109, LLR-1240, LLR-1241, LLR-1242, LLR-1243, LLR-1244,
LLR-1245, LLR-1246

Python 3.8 compatible: the dev PC's gate interpreter is 3.8, the panel's 3.12.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── the files ────────────────────────────────────────────────────────────────
STATE_DIR = Path("/run/wall-meeting-recorder")
STATE_FILE = STATE_DIR / "state.json"
RENDERER_DIR = Path("/run/wall-panel-renderer")
REQUEST_FILE = RENDERER_DIR / "meeting-request.json"
ACK_FILE = RENDERER_DIR / "meeting-indicator-ack.json"
CALL_STATE_FILE = Path("/run/wall-panel/call-state.json")
MIC_SOURCE_ENV = Path("/run/wall-panel/audio-mic-source.env")
# The headset card's id, which `mic_headset` needs (wall-bt-call.service reads
# the same file; Sol review 1, F4).
HEADSET_ENV = Path("/run/wall-panel/audio-headset.env")
INPUT_MUTE_ENV = Path("/run/wall-panel/audio-input-mute.env")
AUDIO_MODE_FILE = Path("/etc/wall-panel/audio-mode")
CONFIG_FILE = Path("/etc/wall-panel/meeting-recorder.env")
SPOOL = Path("/var/spool/wall-meeting")
FFMPEG = "/usr/bin/ffmpeg"
MAX_DOC_BYTES = 4096
SCHEMA_VERSION = 1

# ── the timings (LLR-1242) ───────────────────────────────────────────────────
# The kiosk beats every 500 ms. The daemon sees a beat up to POLL_S after it
# was written; a beat older than ACK_FRESH_S is a lost indicator; the poll
# finds that up to POLL_S later; and a capture asked to stop with SIGINT is
# killed STOP_GRACE_S after that. From the kiosk's last write:
# 0.25 + 1.0 + 0.25 + 0.4 = 1.9 s < 2 s (Sol review 1, F5). Two beats fit in
# the freshness window, so one late frame does not end a recording.
POLL_S = 0.25
ACK_FRESH_S = 1.0
STOP_GRACE_S = 0.4
# A manual start that never sees the indicator gives up (the kiosk asked, so a
# kiosk that cannot paint has failed); a call keeps waiting without capturing.
INDICATOR_START_TIMEOUT_S = 10.0
# wall-bt-call touches its document every poll while a call lasts; the audio
# broker believes it for 30 s (routed_backend MAX_DOCUMENT_AGE_SECONDS).
CALL_STALE_S = 30.0
# A request is acted on once, and only while it is recent: a request written
# before a daemon restart is not replayed by the restart.
REQUEST_MAX_AGE_S = 30.0
# The hub reports after every pull (every 2 min); older than this is unknown.
HUB_STATE_STALE_S = 900.0
# A refusal stays on the glass this long after it happened.
REFUSAL_SHOW_S = 30.0
# Less room than this is "full": about eleven minutes at 48 kbit/s.
MIN_START_BYTES = 4 * 1024 * 1024
# A finished file smaller than this holds no audio page (Ogg headers only).
MIN_RECORDING_BYTES = 1024
# A capture that reached this fraction of its -fs limit stopped on the limit.
LIMIT_FRACTION = 0.97
# ffmpeg checks -fs after it writes, so a file ends a little PAST its limit:
# measured 16 KiB over at 48 kbit/s for limits from 20 KB to 2 MB. The limit
# handed to ffmpeg is the room less this, so the cap itself is never passed.
FS_MARGIN = 256 * 1024

DEFAULTS = {
    "WALL_MEETING_SPOOL_MAX_MB": 512,
    "WALL_MEETING_MIN_FREE_MB": 5120,
    "WALL_MEETING_MAX_MINUTES": 240,
    "WALL_MEETING_BITRATE_KBPS": 48,
}
BOUNDS = {
    "WALL_MEETING_SPOOL_MAX_MB": (16, 16384),
    "WALL_MEETING_MIN_FREE_MB": (256, 65536),
    "WALL_MEETING_MAX_MINUTES": (5, 720),
    "WALL_MEETING_BITRATE_KBPS": (16, 128),
}

IDLE, MANUAL, CALL, MANUAL_CALL = "idle", "manual", "call", "manual-call"
PHASE_OFF, PHASE_WAITING, PHASE_RECORDING, PHASE_BLOCKED = "off", "waiting", "recording", "blocked"
ALIAS = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
NAME = re.compile(r"^meeting-[0-9]{8}T[0-9]{6}Z(-[0-9]{1,3})?\.opus$")


def log(message: str) -> None:
    """One journal line (the unit's stdout is the journal)."""
    print("wall-meeting-recorder: %s" % message, flush=True)


# ── the pure core: one state machine (LLR-1240) ──────────────────────────────

class RecordingMachine:
    """The recording intent: idle, manual, call or manual-call. Pure.

    It decides WHETHER a recording is wanted; the shell decides whether one can
    run (indicator, room, mode). Every input is an event method; every method
    returns True when the intent changed.

    `seq` rises by one each time the intent leaves idle, so an acknowledgement
    of an earlier indicator never counts for a later recording. It starts from
    the clock so it does not repeat across runs.
    """

    def __init__(self, seq: int = 0, auto_call: bool = True):
        self.intent = IDLE
        self.seq = seq
        self.auto_call = auto_call
        self.call_key: Optional[str] = None
        self.gateway: Optional[str] = None
        self.suppressed_key: Optional[str] = None

    @property
    def wanted(self) -> bool:
        return self.intent != IDLE

    def _enter(self, intent: str) -> bool:
        if intent == self.intent:
            return False
        if self.intent == IDLE:
            self.seq += 1
        self.intent = intent
        return True

    def manual_start(self) -> bool:
        if self.intent == IDLE:
            return self._enter(MANUAL)
        if self.intent == CALL:
            # Joins the call's recording: same intent family, same file.
            self.intent = MANUAL_CALL
            return True
        return False

    def manual_stop(self) -> bool:
        if self.intent == IDLE:
            return False
        if self.call_key is not None:
            # A stop during a call holds until that call ends.
            self.suppressed_key = self.call_key
        self.intent = IDLE
        return True

    def call_up(self, key: str, gateway: Optional[str], auto: bool = True) -> bool:
        """A call is up. `key` identifies it (its `since`); `auto` is whether
        its gateway is one the Owner's ruling records automatically."""
        if key != self.call_key:
            # A different call: nothing about the previous one carries over.
            self.call_key = key
            self.suppressed_key = None
        self.gateway = gateway
        if self.intent == MANUAL:
            self.intent = MANUAL_CALL
            return True
        if self.intent == IDLE and auto and self.auto_call and self.suppressed_key != key:
            return self._enter(CALL)
        return False

    def call_down(self) -> bool:
        self.call_key = None
        self.gateway = None
        self.suppressed_key = None
        if self.intent == CALL:
            self.intent = IDLE
            return True
        if self.intent == MANUAL_CALL:
            # The person started it by hand; the call ending does not stop it.
            self.intent = MANUAL
            return True
        return False

    def capture_failed(self) -> bool:
        """The capture died, hit its duration bound, or a manual start could
        not get the indicator: back to idle, and the current call (if any) is
        not re-triggered, so a broken capture cannot loop."""
        if self.intent == IDLE:
            return False
        if self.call_key is not None:
            self.suppressed_key = self.call_key
        self.intent = IDLE
        return True

    def indicator_lost(self) -> bool:
        """The kiosk stopped confirming the indicator. A manual recording ends
        (idle); a call's intent stays, so capture resumes -- into a new file --
        only once the indicator is confirmed again."""
        if self.intent == MANUAL:
            self.intent = IDLE
            return True
        if self.intent == MANUAL_CALL:
            self.intent = CALL
            return True
        return False


def indicator_fresh(beat_at: Optional[float], now: float) -> bool:
    """Whether the newest acknowledgement beat for the current seq is fresh."""
    return beat_at is not None and 0.0 <= now - beat_at <= ACK_FRESH_S


def spool_room(used: int, cap: int, free: int, floor: int) -> Tuple[int, Optional[str]]:
    """Bytes a new recording may write, and the refusal when that is too few.

    Contract:
      Inputs:  used: bytes already in the spool; cap: the spool's hard cap;
               free: bytes available on its filesystem; floor: bytes that
               filesystem must keep free. All >= 0.
      Outputs: (room, None) when room >= MIN_START_BYTES, else (room, reason)
               with reason "spool-full" or "disk-low" (whichever binds).
    """
    by_cap = cap - used
    by_disk = free - floor
    room = max(0, min(by_cap, by_disk))
    if room >= MIN_START_BYTES:
        return room, None
    return room, "disk-low" if by_disk < by_cap else "spool-full"


def parse_call_state(text: Optional[str], age_s: Optional[float]) -> Tuple[Optional[str], Optional[str]]:
    """(call key, gateway alias) for a call that is up, else (None, None).

    The same rule as the audio broker's (routed_backend._call): idle is the
    answer to every failure, and a document older than CALL_STALE_S is idle.
    """
    if text is None or age_s is None or age_s > CALL_STALE_S or age_s < -1:
        return None, None
    try:
        raw = json.loads(text)
    except ValueError:
        return None, None
    if not isinstance(raw, dict) or raw.get("state") not in ("call", "bridge"):
        return None, None
    since = raw.get("since")
    key = since if isinstance(since, str) and since else "unknown"
    gateway = raw.get("gateway")
    return key, gateway if isinstance(gateway, str) and ALIAS.match(gateway) else None


def parse_request(text: Optional[str]) -> Optional[Tuple[int, str, float]]:
    """(id, action, requestedAt ms) from the kiosk's request, or None."""
    value = _small_json(text)
    if not isinstance(value, dict) or value.get("schemaVersion") != SCHEMA_VERSION:
        return None
    rid, action, at = value.get("id"), value.get("action"), value.get("requestedAt")
    if not _is_int(rid) or action not in ("start", "stop") or not _is_number(at):
        return None
    return rid, action, float(at)


def parse_ack(text: Optional[str]) -> Optional[Tuple[int, int]]:
    """(seq, beat) from the kiosk's indicator acknowledgement, or None."""
    value = _small_json(text)
    if not isinstance(value, dict) or value.get("schemaVersion") != SCHEMA_VERSION:
        return None
    seq, beat = value.get("seq"), value.get("beat")
    if not _is_int(seq) or not _is_int(beat):
        return None
    return seq, beat


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _small_json(text: Optional[str]):
    if text is None or len(text) > MAX_DOC_BYTES:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def read_env_text(text: str) -> Dict[str, str]:
    """KEY=VALUE lines; quotes stripped; comments and blanks ignored."""
    values: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def settings_from(env: Dict[str, str]) -> Dict[str, object]:
    """The recorder's settings, each integer knob bounded; out of range or
    malformed is the default (journaled by the caller)."""
    out: Dict[str, object] = {}
    for key, default in DEFAULTS.items():
        low, high = BOUNDS[key]
        raw = env.get(key, "")
        out[key] = int(raw) if raw.isdigit() and low <= int(raw) <= high else default
    gateways = [g.strip() for g in env.get("WALL_MEETING_CALL_GATEWAYS", "").split(",") if g.strip()]
    out["WALL_MEETING_CALL_GATEWAYS"] = [g for g in gateways if ALIAS.match(g)]
    return out


def capture_argv(output: str, limit_bytes: int, max_seconds: int, bitrate_kbps: int,
                 near: Optional[List[str]] = None, far: Optional[List[str]] = None,
                 ffmpeg: str = FFMPEG) -> List[str]:
    """The ffmpeg command that records one meeting (LLR-1243).

    `near`/`far` are the two input specifications; the defaults are the
    panel's ALSA PCMs and the tests replace them with generated tones. The
    near side is mono; the far side is the stereo bus, folded to mono. The two
    are joined left = near, right = far, and encoded once.
    """
    near = near or ["-f", "alsa", "-channels", "1", "-sample_rate", "48000", "-i", "mic_selected"]
    far = far or ["-f", "alsa", "-channels", "2", "-sample_rate", "48000", "-i", "bus_monitor"]
    graph = ("[0:a]aformat=sample_rates=48000:channel_layouts=mono,aresample=async=1000[near];"
             "[1:a]aformat=sample_rates=48000:channel_layouts=stereo,pan=mono|c0=0.5*c0+0.5*c1,"
             "aresample=async=1000,aformat=channel_layouts=mono[far];"
             "[near][far]join=inputs=2:channel_layout=stereo:map=0.0-FL|1.0-FR[out]")
    return ([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"] + near + far
            + ["-filter_complex", graph, "-map", "[out]", "-c:a", "libopus",
               "-b:a", "%dk" % bitrate_kbps, "-application", "audio",
               # Every packet reaches the file as it is muxed (Ogg pages are
               # about a second), so a capture that is killed loses about a
               # second rather than ffmpeg's whole write buffer.
               "-flush_packets", "1",
               "-t", str(int(max_seconds)), "-fs", str(int(limit_bytes)), output])


def state_document(machine: RecordingMachine, phase: str, *, started_at_ms: Optional[int],
                   refusal: Optional[Dict[str, object]], used: int, cap: int, hub: str,
                   available: bool, unavailable_reason: Optional[str], near_muted: bool,
                   now_ms: int) -> Dict[str, object]:
    """The document the kiosk reads (LLR-1245)."""
    indicator = machine.wanted and phase in (PHASE_WAITING, PHASE_RECORDING)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "seq": machine.seq,
        "intent": machine.intent,
        "phase": phase if machine.wanted else PHASE_OFF,
        "indicator": indicator,
        "startedAt": started_at_ms if phase == PHASE_RECORDING else None,
        "gateway": machine.gateway,
        "nearMuted": near_muted,
        "refusal": refusal,
        "spool": {"usedBytes": used, "capBytes": cap},
        "hub": hub,
        "available": available,
        "unavailableReason": unavailable_reason,
        "autoCall": machine.auto_call,
        "observedAt": now_ms,
    }


def recording_name(now_s: float, taken) -> str:
    """meeting-<UTC second>.opus, with a -N suffix when that name is taken."""
    base = time.strftime("meeting-%Y%m%dT%H%M%SZ", time.gmtime(now_s))
    name = base + ".opus"
    n = 2
    while name in taken:
        name = "%s-%d.opus" % (base, n)
        n += 1
    return name


# ── the shell: files ─────────────────────────────────────────────────────────

def read_small(path: Path) -> Optional[str]:
    """At most MAX_DOC_BYTES + 1 of a file, without following a symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        return os.read(fd, MAX_DOC_BYTES + 1).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def write_json_atomic(path: Path, document) -> bool:
    """Replace `path` through a temporary in its directory, 0644."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document, sort_keys=True) + "\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, str(path))
        return True
    except OSError as exc:
        log("could not write %s (%s)" % (path, exc))
        return False


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Spool:
    """The panel's spool (LLR-1244): recording/ holds the one file being
    written, ready/ the finished ones, each with a <name>.sha256 sidecar that
    is written LAST -- the forced command lists a file only once its sidecar
    exists, so a half-finalised file is never offered to the hub."""

    def __init__(self, root: Path = SPOOL):
        self.root = Path(root)
        self.recording = self.root / "recording"
        self.ready = self.root / "ready"

    def ensure(self) -> None:
        for directory in (self.recording, self.ready):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def used_bytes(self) -> int:
        total = 0
        for directory in (self.recording, self.ready):
            try:
                for entry in os.scandir(str(directory)):
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
        return total

    def free_bytes(self) -> int:
        # f_bavail * f_frsize on Linux: what an unprivileged writer may use.
        return shutil.disk_usage(str(self.root)).free

    def names(self) -> set:
        found = set()
        for directory in (self.recording, self.ready):
            try:
                found.update(os.listdir(str(directory)))
            except OSError:
                continue
        return found

    def new_path(self, now_s: float) -> Path:
        return self.recording / recording_name(now_s, self.names())

    def finalise(self, path: Path) -> Optional[str]:
        """Move a finished (or partial) recording to ready/ and write its hash.

        A file with no audio in it is removed instead. Returns the ready name
        or None. Never raises: a failure is a journal line and the file stays
        where it is, for recover() at the next start.
        """
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size < MIN_RECORDING_BYTES:
            log("discarding %s: %d bytes, no audio was recorded" % (path.name, size))
            try:
                path.unlink()
            except OSError:
                pass
            return None
        target = self.ready / path.name
        try:
            os.replace(str(path), str(target))
            self._write_sidecar(target)
        except OSError as exc:
            log("could not finalise %s (%s); it stays for the next start" % (path.name, exc))
            return None
        log("finalised %s (%d bytes)" % (target.name, size))
        return target.name

    def _write_sidecar(self, target: Path) -> None:
        digest = sha256_of(target)
        sidecar = target.with_name(target.name + ".sha256")
        temporary = sidecar.with_name("." + sidecar.name + ".tmp")
        with open(str(temporary), "w", encoding="ascii") as handle:
            handle.write("%s  %s\n" % (digest, target.name))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(sidecar))

    def recover(self) -> List[str]:
        """At start: finalise whatever a crash left in recording/, and hash any
        ready file whose sidecar was never written."""
        done = []
        try:
            leftovers = sorted(os.listdir(str(self.recording)))
        except OSError:
            leftovers = []
        for name in leftovers:
            if NAME.match(name):
                finished = self.finalise(self.recording / name)
                if finished:
                    done.append(finished)
        try:
            ready = sorted(os.listdir(str(self.ready)))
        except OSError:
            ready = []
        for name in ready:
            if NAME.match(name) and (name + ".sha256") not in ready:
                try:
                    self._write_sidecar(self.ready / name)
                    done.append(name)
                except OSError as exc:
                    log("could not hash %s (%s)" % (name, exc))
        return done


class Capture:
    """One ffmpeg child. start/poll/stop; stop is SIGINT (ffmpeg writes its
    trailer) and then SIGKILL after STOP_GRACE_S, so it is bounded."""

    def __init__(self, popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep):
        self.popen, self.clock, self.sleep = popen, clock, sleep
        self.process = None
        self.path: Optional[Path] = None
        self.limit = 0
        self.max_seconds = 0
        self.started = 0.0

    @property
    def running(self) -> bool:
        return self.process is not None

    def start(self, argv: List[str], path: Path, limit: int, max_seconds: int, env=None) -> bool:
        try:
            self.process = self.popen(argv, stdin=subprocess.DEVNULL, env=env)
        except OSError as exc:
            log("could not start the capture (%s)" % exc)
            self.process = None
            return False
        self.path, self.limit, self.max_seconds, self.started = path, limit, max_seconds, self.clock()
        return True

    def exited(self) -> Optional[int]:
        """The exit code when the child has ended by itself, else None."""
        if self.process is None:
            return None
        return self.process.poll()

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            try:
                self.process.send_signal(signal.SIGINT)
            except (OSError, ValueError):
                # ValueError: a platform without SIGINT for a child (the
                # tests' Windows host); there it is a plain terminate.
                try:
                    self.process.terminate()
                except OSError:
                    pass
            deadline = self.clock() + STOP_GRACE_S
            while self.process.poll() is None and self.clock() < deadline:
                self.sleep(0.02)
            if self.process.poll() is None:
                try:
                    self.process.kill()
                except OSError:
                    pass
                try:
                    self.process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        self.process = None

    def ended_on_bound(self) -> Optional[str]:
        """Why a capture that exited 0 by itself ended: its size or its time."""
        try:
            size = self.path.stat().st_size if self.path else 0
        except OSError:
            size = 0
        if self.limit and size >= self.limit * LIMIT_FRACTION:
            return "spool-full"
        if self.clock() - self.started >= self.max_seconds - 1:
            return "max-duration"
        return None


def alert(key: str, message: str) -> None:
    """A warning-level journal line. JOURNAL ONLY, deliberately: the unit has
    AF_UNIX and nothing else, so no sample has an address family that could
    carry it off the panel -- and that rules out the network alert route too.
    The kiosk shows the same refusal on the glass, where the person is."""
    log("ALERT %s: %s" % (key, message))


# ── the shell: the loop (LLR-1242, LLR-1246) ─────────────────────────────────

class Recorder:
    """Reads the inputs, drives the machine and runs the capture it allows.

    Every path is a constructor argument so the tests run it on a temporary
    tree with a fake capture and an injected clock.
    """

    def __init__(self, settings: Dict[str, object], *, spool: Spool, state_file: Path = STATE_FILE,
                 request_file: Path = REQUEST_FILE, ack_file: Path = ACK_FILE,
                 call_file: Path = CALL_STATE_FILE, mode_file: Path = AUDIO_MODE_FILE,
                 mic_env: Path = MIC_SOURCE_ENV, mute_env: Path = INPUT_MUTE_ENV,
                 headset_env: Path = HEADSET_ENV,
                 capture: Optional[Capture] = None, argv_for=capture_argv,
                 clock=time.monotonic, wall=time.time, alert_fn=alert):
        self.settings = settings
        self.spool = spool
        self.state_file, self.request_file, self.ack_file = state_file, request_file, ack_file
        self.call_file, self.mode_file, self.mic_env, self.mute_env = call_file, mode_file, mic_env, mute_env
        self.headset_env = headset_env
        self.capture = capture or Capture()
        self.argv_for = argv_for
        self.clock, self.wall, self.alert = clock, wall, alert_fn
        gateways = settings.get("WALL_MEETING_CALL_GATEWAYS") or []
        self.machine = RecordingMachine(seq=int(wall() * 1000), auto_call=bool(gateways))
        self.gateways = set(gateways)
        self.phase = PHASE_OFF
        self.waiting_since: Optional[float] = None
        self.started_at_ms: Optional[int] = None
        self.refusal: Optional[Dict[str, object]] = None
        self.refusal_at = 0.0
        self.last_request_id: Optional[int] = None
        self.ack_seen: Optional[Tuple[int, int]] = None
        self.beat_at: Optional[float] = None
        # The seq the newest beat was for: a beat counts only for that
        # recording, never for the next one (Sol review 1, F1).
        self.beat_seq: Optional[int] = None
        # What the running capture was started for: its seq and the near
        # side's environment (which microphone mic_selected resolves to).
        self.capture_seq: Optional[int] = None
        self.capture_near: Optional[Dict[str, str]] = None
        self.last_document = None
        self.last_written_second: Optional[int] = None

    # inputs
    def prime(self) -> None:
        """Start-up: recover the spool and mark the existing request as seen."""
        self.spool.ensure()
        for name in self.spool.recover():
            log("recovered %s from a previous run" % name)
        request = parse_request(read_small(self.request_file))
        self.last_request_id = request[0] if request else None

    def _read_request(self) -> Optional[str]:
        request = parse_request(read_small(self.request_file))
        if request is None or request[0] == self.last_request_id:
            return None
        self.last_request_id = request[0]
        age = self.wall() - request[2] / 1000.0
        if abs(age) > REQUEST_MAX_AGE_S:
            log("ignoring a %s request %.0f s old" % (request[1], age))
            return None
        return request[1]

    def _read_ack(self, now: float) -> None:
        ack = parse_ack(read_small(self.ack_file))
        if ack is None or ack[0] != self.machine.seq:
            return
        if ack != self.ack_seen:
            self.ack_seen = ack
            self.beat_at = now
            self.beat_seq = ack[0]

    def _near_env(self) -> Dict[str, str]:
        """The environment mic_selected resolves through, as the call legs get
        it: the published microphone choice and the headset card."""
        env = read_env_text(read_small(self.mic_env) or "")
        env.update(read_env_text(read_small(self.headset_env) or ""))
        return env

    def _read_call(self) -> Tuple[Optional[str], Optional[str]]:
        try:
            age = self.wall() - self.call_file.stat().st_mtime
        except OSError:
            return None, None
        return parse_call_state(read_small(self.call_file), age)

    def _bus_mode(self) -> bool:
        text = read_small(self.mode_file)
        return text is not None and text.strip() == "bus"

    def _near_muted(self) -> bool:
        return read_env_text(read_small(self.mute_env) or "").get("WALL_AUDIO_INPUT_MUTED") == "1"

    def _hub_state(self) -> str:
        path = self.spool.root / "hub-state.json"
        value = _small_json(read_small(path))
        try:
            age = self.wall() - path.stat().st_mtime
        except OSError:
            return "unknown"
        if not isinstance(value, dict) or value.get("quota") not in ("ok", "full") or age > HUB_STATE_STALE_S:
            return "unknown"
        return value["quota"]

    def _refuse(self, reason: str, now: float) -> None:
        if self.refusal is None or self.refusal.get("reason") != reason:
            log("capture refused: %s" % reason)
        self.refusal = {"reason": reason, "at": int(self.wall() * 1000)}
        self.refusal_at = now

    # the capture
    def _start_capture(self, now: float, room: int) -> None:
        path = self.spool.new_path(self.wall())
        near = self._near_env()
        env = dict(os.environ)
        env.update(near)
        max_seconds = int(self.settings["WALL_MEETING_MAX_MINUTES"]) * 60
        limit = room - FS_MARGIN
        argv = self.argv_for(str(path), limit, max_seconds, int(self.settings["WALL_MEETING_BITRATE_KBPS"]))
        if self.capture.start(argv, path, limit, max_seconds, env=env):
            self.capture_seq, self.capture_near = self.machine.seq, near
            self.phase = PHASE_RECORDING
            self.started_at_ms = int(self.wall() * 1000)
            self.refusal = None
            log("recording started (%s, intent %s, seq %d, room %d bytes)"
                % (path.name, self.machine.intent, self.machine.seq, room))
        else:
            self._capture_failed(now, "recorder-failed")

    def _stop_capture(self, why: str) -> None:
        path = self.capture.path
        self.capture.stop()
        log("recording stopped (%s)" % why)
        if path is not None:
            self.spool.finalise(path)
        self.started_at_ms = None
        self.capture_seq = self.capture_near = None

    def _capture_failed(self, now: float, reason: str) -> None:
        self._refuse(reason, now)
        if reason == "recorder-failed":
            self.alert("capture-failed", "Meeting recording stopped: the capture failed (journalctl -u wall-meeting-recorder)")
        self.machine.capture_failed()
        self.phase = PHASE_OFF

    # one step
    def tick(self) -> Dict[str, object]:
        """One poll: inputs, machine, capture, document. Returns the document."""
        now = self.clock()
        available = self._bus_mode()
        action = self._read_request()
        key, gateway = self._read_call()
        if key is None:
            self.machine.call_down()
        else:
            self.machine.call_up(key, gateway, auto=gateway in self.gateways)
        if action == "stop":
            self.machine.manual_stop()
        elif action == "start":
            if available:
                self.refusal = None
                self.machine.manual_start()
            else:
                self._refuse("unavailable", now)
        self._read_ack(now)
        self._drive(now, available)
        if self.refusal is not None and not self.machine.wanted and now - self.refusal_at > REFUSAL_SHOW_S:
            self.refusal = None
        return self._publish()

    def _drive(self, now: float, available: bool) -> None:
        fresh = self.beat_seq == self.machine.seq and indicator_fresh(self.beat_at, now)
        if self.capture.running:
            code = self.capture.exited()
            if not self.machine.wanted:
                self._stop_capture("intent %s" % self.machine.intent)
                self.phase = PHASE_OFF
            elif self.capture_seq != self.machine.seq:
                # Stopped and started again between two polls: that is a new
                # recording, and it waits for its own indicator.
                self._stop_capture("a new recording replaced it")
                self.phase, self.waiting_since = PHASE_WAITING, now
            elif code is None and self._near_env() != self.capture_near:
                # The switch moved (Speaker <-> Headset) and mic_selected now
                # names another microphone; an open PCM would keep the old
                # one, so the recording continues in a new file (Sol review 1,
                # F3). The intent and the indicator are unchanged.
                self._stop_capture("the selected microphone changed; continuing in a new file")
                self.phase, self.waiting_since = PHASE_WAITING, now
                if fresh:
                    self._restart_after_switch(now)
            elif code is not None:
                bound = self.capture.ended_on_bound() if code == 0 else None
                path = self.capture.path
                self.capture.process = None
                if path is not None:
                    self.spool.finalise(path)
                self.started_at_ms = None
                self.capture_seq = self.capture_near = None
                log("the capture ended by itself (exit %s%s)" % (code, ", %s" % bound if bound else ""))
                if bound == "spool-full":
                    self._refuse("spool-full", now)
                    self.phase = PHASE_BLOCKED
                else:
                    self._capture_failed(now, bound or "recorder-failed")
            elif not fresh:
                self._stop_capture("the indicator is no longer confirmed")
                self.machine.indicator_lost()
                self._refuse("indicator-lost", now)
                self.phase = PHASE_WAITING if self.machine.wanted else PHASE_OFF
                self.waiting_since = now
            return
        if not self.machine.wanted:
            self.phase, self.waiting_since = PHASE_OFF, None
            return
        if not available:
            self._refuse("unavailable", now)
            self.phase = PHASE_BLOCKED
            if self.machine.intent == MANUAL:
                self.machine.capture_failed()
            return
        room, refusal = spool_room(self.spool.used_bytes(), self._cap(), self._free(), self._floor())
        if refusal is not None:
            self._refuse(refusal, now)
            self.phase = PHASE_BLOCKED
            if self.machine.intent == MANUAL:
                self.machine.capture_failed()
            return
        if self.phase != PHASE_WAITING:
            self.phase, self.waiting_since = PHASE_WAITING, now
        if fresh:
            self._start_capture(now, room)
        elif self.machine.intent == MANUAL and now - (self.waiting_since or now) > INDICATOR_START_TIMEOUT_S:
            log("the kiosk never confirmed the indicator; manual recording not started")
            self._capture_failed(now, "indicator-unavailable")

    def _restart_after_switch(self, now: float) -> None:
        room, refusal = spool_room(self.spool.used_bytes(), self._cap(), self._free(), self._floor())
        if refusal is None:
            self._start_capture(now, room)
        else:
            self._refuse(refusal, now)
            self.phase = PHASE_BLOCKED

    def _cap(self) -> int:
        return int(self.settings["WALL_MEETING_SPOOL_MAX_MB"]) * 1024 * 1024

    def _floor(self) -> int:
        return int(self.settings["WALL_MEETING_MIN_FREE_MB"]) * 1024 * 1024

    def _free(self) -> int:
        try:
            return self.spool.free_bytes()
        except OSError:
            return 0

    def _publish(self) -> Dict[str, object]:
        available = self._bus_mode()
        document = state_document(
            self.machine, self.phase, started_at_ms=self.started_at_ms, refusal=self.refusal,
            used=self.spool.used_bytes(), cap=self._cap(), hub=self._hub_state(), available=available,
            unavailable_reason=None if available else "not-bus-mode", near_muted=self._near_muted(),
            now_ms=int(self.wall() * 1000))
        comparable = dict(document)
        comparable.pop("observedAt")
        comparable["spool"] = None
        # Rewritten on a change, and once a second regardless, so observedAt
        # says the daemon is alive.
        second = int(self.clock())
        if comparable != self.last_document or second != self.last_written_second:
            write_json_atomic(self.state_file, document)
            self.last_document = comparable
            self.last_written_second = second
        return document

    def shutdown(self) -> None:
        if self.capture.running:
            self._stop_capture("the service is stopping")


def load_settings(path: Path = CONFIG_FILE) -> Dict[str, object]:
    env = read_env_text(read_small_any(path))
    settings = settings_from(env)
    for key in DEFAULTS:
        if env.get(key) and str(settings[key]) != env.get(key):
            log("%s=%r is out of range; using %s" % (key, env.get(key), settings[key]))
    return settings


def read_small_any(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")[:65536]
    except OSError:
        return ""


def main(argv=None) -> int:
    settings = load_settings()
    recorder = Recorder(settings, spool=Spool())
    stopping = {"now": False}

    def handle(signum, frame):  # noqa: ARG001 - the signal API's shape
        stopping["now"] = True

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    recorder.prime()
    log("running (spool cap %s MiB, free floor %s MiB, automatic for gateways: %s)"
        % (settings["WALL_MEETING_SPOOL_MAX_MB"], settings["WALL_MEETING_MIN_FREE_MB"],
           ", ".join(settings["WALL_MEETING_CALL_GATEWAYS"]) or "none (manual only)"))
    try:
        while not stopping["now"]:
            recorder.tick()
            time.sleep(POLL_S)
    finally:
        recorder.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
