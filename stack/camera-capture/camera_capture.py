#!/usr/bin/env python3
"""camera_capture.py - a bounded training capture of a camera's streams on the media drive (SR-162).

The Owner, 2026-11-09 (HomeHub open-items A81): record the living-room camera
continuously for three days, BOTH the main and the sub stream, straight onto
the media drive, to build the presence training set. This is that recorder.

ONE PROCESS PER STREAM (homehub-camera-capture@<camera>-<stream>.service). It
reads the camera's relay as a consumer - `-map 0:v:0 -c copy -an`: video only,
never decoded, never any sound - and cuts the MPEG-TS into segments with the
door recorder's own Recorder (stack/door/door_recorder.py), so the keyframe
cut, the `.part` finalise, the truncation to the last whole frame and the
EDQUOT/ENOSPC stop are the code already proven on the door camera, not a copy
of it. What differs is subclassed here: the root, the file names, the guard,
the window and the alerts. The door recorder's module is not changed; its log
lines are relabelled inside THIS process only.

    sub   the relay's /<stream> (today 768x432 H.264, ~220 kbps at night)
    main  the relay's /<stream>-main (CAMERA_<C>_RELAY_MAIN_PATH; 1920x1080
          H.264, ~940 kbps at night): the relay holds both camera sessions,
          so the camera's code stays in one place and in no argv.

Layout: /srv/media/Training/<camera>/<stream>/YYYY-MM-DD/
        <camera>-<stream>-YYYYmmddTHHMMSSZ.ts (UTC; `.ts.part` while written).

THE WINDOW. CAMERA_<C>_CAPTURE_UNTIL (UTC YYYY-mm-ddTHH:MM:SSZ) is required
when the capture is enabled. A start at or after it is a clean skip; a running
capture closes its segment and exits 0 at it, so Restart=on-failure does not
bring it back. A reboot inside the window resumes the capture (the units are
enabled); one after it starts nothing.

THE BUDGET. /srv/media/Training/<camera> is its own ext4 quota project
(TRAINING_PROJECT, inherited by everything under it) with a hard limit of
CAMERA_<C>_CAPTURE_QUOTA_MIB for all its streams together: the wall a runaway
write hits, never the door clips' project and never the rest of the drive.
Each stream also prunes its own oldest segments to CAMERA_<C>_CAPTURE_CAP_MIB
(default above anything three days produce, so normally nothing is pruned).

THE GUARD, before every segment: /srv/media is the disk drive-identity.conf
names, ext4 with prjquota, writable (homehub_clips.media_mount_state); this
process has no CAP_SYS_RESOURCE (which would ignore the quota); the stream
root is a real directory on that drive in TRAINING_PROJECT with inheritance;
the filesystem still takes a write. A refusal stops the capture (exit 76).

PRIVACY. The tree is root:root 0750 with the /srv/media default ACL removed,
so no household or share account can read it; nothing here is shared, sent or
used off the hub. Audio is never requested and -an drops it regardless.

Verbs (all take --instance <camera>-<stream>, or --camera C [--stream S]):
    condition   exit 0 only when the capture is enabled, the stream is listed
                in CAMERA_<C>_CAPTURE_STREAMS and the window is open (the
                unit's ExecCondition: a clean skip otherwise).
    ensure      (root) create the tree, its project, its hard limit; idempotent.
    run         the service.
    status      per stream: segments, bytes, first and last, the open part;
                the project's use against its limit; the window.

Python 3.8-compatible (the dev PC's test runner imports this module).
Implements: SR-162, LLR-1519..LLR-1523.
"""

from __future__ import annotations

import argparse
import calendar
import os
import posixpath
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.realpath(__file__))
for _sub in ("monitoring", "scratch", "door", "clips", "camera-relay"):
    sys.path.insert(0, os.path.join(HERE, "..", _sub))
import monitor_common as mc  # noqa: E402
import homehub_scratch as scratch  # noqa: E402
import door_relay  # noqa: E402
import door_recorder as dr  # noqa: E402
import homehub_clips as clips  # noqa: E402
import camera_relay as cr  # noqa: E402

TRAINING_ROOT = clips.MEDIA_MOUNT + "/Training"
TRAINING_PROJECT = 7106      # one project per camera tree; Clips is 7105
STREAMS = ("main", "sub")
EXIT_REFUSED = 2
MIB = dr.MIB
UNTIL_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
Invalid = door_relay.Invalid


def knob(camera: str, suffix: str) -> str:
    return "CAMERA_%s_CAPTURE_%s" % (camera.upper(), suffix)


def parse_until(text: str) -> float:
    """UTC 'YYYY-mm-ddTHH:MM:SSZ' -> epoch second. Raises ValueError."""
    if not UNTIL_RE.fullmatch(text or ""):
        raise ValueError("not YYYY-mm-ddTHH:MM:SSZ")
    return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))


def split_instance(instance: str) -> Tuple[str, str]:
    """'livingroom-main' -> ('livingroom', 'main'). Camera names hold no hyphen."""
    camera, sep, stream = (instance or "").rpartition("-")
    if not sep or stream not in STREAMS:
        raise Invalid("an instance is <camera>-<stream>, the stream one of %s" % ", ".join(STREAMS))
    return cr.camera_name(camera), stream


class CaptureSettings:
    """Camera C's capture knobs for one stream.

    Contract:
      Config:  CAMERA_<C>_CAPTURE_ENABLED (false); _STREAMS ("main,sub");
               _UNTIL (UTC YYYY-mm-ddTHH:MM:SSZ, required when enabled);
               _SEGMENT_SEC (600, 60..3600); _CAP_MIB (per stream, 307200);
               _QUOTA_MIB (the camera's project, 409600); _STALL_SEC (30).
               Sized from a night measurement (main ~940 kbps): even 8 Mbps
               all day for 72 h is ~240 GiB, so neither limit should act.
               [stack/.env]
      Raises:  Invalid naming the key, never a value it did not validate.
    The attributes segment_sec, cap_mib and stall_sec are the ones the door
    recorder's Recorder reads from its settings. Implements: LLR-1519.
    """

    def __init__(self, env: Dict[str, str], camera: str, stream: str):
        self.camera = cr.camera_name(camera)
        if stream not in STREAMS:
            raise Invalid("the stream must be one of %s" % ", ".join(STREAMS))
        self.stream = stream
        self.enabled = mc.knob_bool(env, knob(camera, "ENABLED"), False)
        listed = [x.strip() for x in (env.get(knob(camera, "STREAMS"), "") or "main,sub").split(",") if x.strip()]
        if not listed or any(x not in STREAMS for x in listed):
            raise Invalid("%s must list main and/or sub" % knob(camera, "STREAMS"))
        self.streams = listed
        raw_until = (env.get(knob(camera, "UNTIL"), "") or "").strip().strip('"')
        self.until = None  # type: Optional[float]
        if raw_until:
            try:
                self.until = parse_until(raw_until)
            except ValueError:
                raise Invalid("%s must be UTC YYYY-mm-ddTHH:MM:SSZ" % knob(camera, "UNTIL"))
        self.until_text = raw_until
        self.segment_sec = mc.knob_int(env, knob(camera, "SEGMENT_SEC"), 600, 60, 3600)
        self.cap_mib = mc.knob_int(env, knob(camera, "CAP_MIB"), 307200, 1024, 1 << 22)
        self.quota_mib = mc.knob_int(env, knob(camera, "QUOTA_MIB"), 409600, 1024, 1 << 22)
        self.stall_sec = mc.knob_int(env, knob(camera, "STALL_SEC"), 30, 5, 600)
        # Hub paths, POSIX wherever this is imported (the dev PC's runner too).
        self.camera_root = posixpath.join(TRAINING_ROOT, self.camera)
        self.root = posixpath.join(self.camera_root, stream)
        self.prefix = "%s-%s" % (self.camera, stream)
        self.label = "%s capture" % self.prefix


def window_state(s: CaptureSettings, now: float) -> Tuple[str, str]:
    """('run' | 'skip' | 'refuse', why) for this stream at `now` (pure).
    Implements: LLR-1520."""
    if not s.enabled:
        return "skip", "%s is not true" % knob(s.camera, "ENABLED")
    if s.stream not in s.streams:
        return "skip", "%s does not list %s" % (knob(s.camera, "STREAMS"), s.stream)
    if s.until is None:
        return "refuse", "%s is not set: a capture must have an end" % knob(s.camera, "UNTIL")
    if now >= s.until:
        return "skip", "the capture window ended at %s" % s.until_text
    return "run", "until %s (%.1f h left)" % (s.until_text, (s.until - now) / 3600.0)


# ── names and pruning (the door recorder's rules, with this stream's prefix) ──


def segment_re(prefix: str):
    return re.compile(re.escape(prefix) + r"-(\d{8}T\d{6})Z(?:-\d+)?\.ts$")


def segment_name(now: float, prefix: str) -> Tuple[str, str]:
    """(day directory, file name) for a segment starting at `now` (UTC)."""
    t = time.gmtime(now)
    return time.strftime("%Y-%m-%d", t), prefix + time.strftime("-%Y%m%dT%H%M%SZ.ts", t)


def segment_start(name: str, prefix: str) -> Optional[float]:
    m = segment_re(prefix).search(name[:-len(".part")] if name.endswith(".ts.part") else name)
    if not m:
        return None
    return float(calendar.timegm(time.strptime(m.group(1), "%Y%m%dT%H%M%S")))


def list_segments(root: str, prefix: str) -> List[Tuple[str, int]]:
    """[(path, size)] of this stream's finished segments, oldest first."""
    rx, out = segment_re(prefix), []
    try:
        days = sorted(d for d in os.listdir(root) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d))
    except OSError:
        return out
    for day in days:
        ddir = os.path.join(root, day)
        try:
            names = sorted(n for n in os.listdir(ddir) if rx.search(n))
        except OSError:
            continue
        for name in names:
            path = os.path.join(ddir, name)
            try:
                out.append((path, os.path.getsize(path)))
            except OSError:
                pass
    return out


def list_parts(root: str, prefix: str) -> List[str]:
    out = []
    try:
        days = sorted(d for d in os.listdir(root) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d))
    except OSError:
        return out
    for day in days:
        ddir = os.path.join(root, day)
        try:
            out += [os.path.join(ddir, n) for n in sorted(os.listdir(ddir))
                    if n.startswith(prefix + "-") and n.endswith(".ts.part")]
        except OSError:
            continue
    return out


def prune(root: str, prefix: str, cap_bytes: int, headroom: int) -> List[str]:
    """The door recorder's prune_plan over THIS stream's segments only."""
    removed = []
    for path in dr.prune_plan(list_segments(root, prefix), cap_bytes, headroom):
        try:
            os.unlink(path)
            removed.append(path)
        except FileNotFoundError:
            pass
        try:
            os.rmdir(os.path.dirname(path))
        except OSError:
            pass
    return removed


# ── the guard ────────────────────────────────────────────────────────────────


def training_guard(root: str, paths: Optional[clips.MediaPaths] = None) -> Tuple[bool, str]:
    """The media drive, no CAP_SYS_RESOURCE, `root` on it in TRAINING_PROJECT
    with inheritance, and still writable. Run before every segment.
    Implements: LLR-1521."""
    paths = paths or clips.MediaPaths()
    ok, reason, entry = clips.media_mount_state(paths)
    if not ok:
        return False, reason
    if scratch.holds_cap_sys_resource(paths.status):
        return False, "this process holds CAP_SYS_RESOURCE, which ignores quota hard limits"
    try:
        st = os.lstat(root)
    except OSError:
        return False, "%s does not exist (run camera_capture.py ensure)" % root
    if not stat.S_ISDIR(st.st_mode):
        return False, "%s is not a directory" % root
    maj, mnr = (int(x) for x in entry["majmin"].split(":"))
    if st.st_dev != os.makedev(maj, mnr):
        return False, "%s is not on the media drive" % root
    project, inherit = scratch.get_project(root)
    if (project, inherit) != (TRAINING_PROJECT, True):
        return False, ("%s is in quota project %s (inherit %s), not %d: it would write outside its limit "
                       "(run camera_capture.py ensure)" % (root, project, inherit, TRAINING_PROJECT))
    failed = clips.media_write_error(root)
    if failed:
        return False, "%s is mounted but not writable (%s): the filesystem has stopped" % (clips.MEDIA_MOUNT, failed)
    return True, "ok"


# ── the recorder: the door recorder's, with this stream's root, names, guard ──


class _Relabelled:
    """monitor_common with the door recorder's "door recorder:" log prefix
    replaced by this capture's label. Installed in THIS process only, so the
    door recorder service (another process) is untouched."""

    def __init__(self, base, label: str):
        self._base, self._label = base, label

    def __getattr__(self, name):
        return getattr(self._base, name)

    def log(self, message, *args, **kwargs):
        if isinstance(message, str) and message.startswith("door recorder: "):
            message = self._label + ": " + message[len("door recorder: "):]
        return self._base.log(message, *args, **kwargs)


class CaptureRecorder(dr.Recorder):
    """dr.Recorder over one stream's tree. Implements: LLR-1522."""

    def __init__(self, env_file: str, s: CaptureSettings, paths: Optional[clips.MediaPaths] = None,
                 alert: Optional[Callable[[str, str, str], None]] = None, clock=time.time):
        super().__init__(env_file, s.root, s, None, paths=paths, alert=alert, clock=clock)
        self.cs = s
        self.media_paths = paths

    def _alert(self, key: str, title: str, body: str) -> None:
        # The door recorder's keys and words, translated: its quota_stop says
        # "door/ on the scratch volume is full".
        if key.endswith("-quota"):
            title = "HomeHub: %s stopped (quota)" % self.cs.label
            body = "%s on the media drive is full (project %d); it will not restart by itself." % (
                self.cs.camera_root, TRAINING_PROJECT)
        key = "camera-capture-%s-%s" % (self.cs.prefix, key.rsplit("-", 1)[-1])
        mc.send_alert(key, "high", title, body, self.env_file, mc.DEFAULT_STATE_DIR,
                      log_tag="homehub-camera-capture")

    def guard(self) -> Tuple[bool, str]:
        return training_guard(self.root, self.media_paths)

    def open_segment(self, now: float) -> None:
        """Guard, prune, then open the next file (dr.Recorder.open_segment with
        this stream's names). Raises SystemExit on a refusal."""
        ok, reason = self.guard()
        if not ok:
            mc.log("%s: STOPPING before the next segment - guard: %s" % (self.cs.label, reason), warning=True)
            self.alert("guard", "HomeHub: %s stopped (media drive)" % self.cs.label, reason)
            raise SystemExit(dr.EXIT_GUARD)
        headroom = max(int(self.last_size * 1.5), 16 * MIB)
        for path in prune(self.root, self.cs.prefix, self.s.cap_mib * MIB, headroom):
            mc.log("%s: pruned %s (cap %d MiB)" % (self.cs.label, path, self.s.cap_mib))
        day, name = segment_name(now, self.cs.prefix)
        ddir = os.path.join(self.root, day)
        try:
            os.makedirs(ddir, mode=0o750, exist_ok=True)
            n = 0
            while True:
                stem = name[:-3] + ("" if n == 0 else "-%d" % n)
                self.part = os.path.join(ddir, stem + ".ts.part")
                if not os.path.exists(self.part) and not os.path.exists(self.part[:-5]):
                    break
                n += 1
            self.handle = open(self.part, "xb", buffering=0)
            if os.name == "posix":
                self.dir_fd = os.open(ddir, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            self.quota_stop(exc, ddir)
            raise
        self.written = self.first_frame_start = self.last_frame_start = 0
        self.last_write = None
        self.frame_starts.clear()


def container(s: CaptureSettings) -> str:
    return "homehub-camera-capture-%s" % s.prefix


def relay_for(env_file: str, s: CaptureSettings) -> "cr.CameraRelaySettings":
    """The relay settings this stream reads: /<stream> or /<stream>-main."""
    relay = cr.CameraRelaySettings(env_file, s.camera)
    return relay.for_main() if s.stream == "main" else relay


def ffmpeg_command(relay: "cr.CameraRelaySettings", name: str) -> Tuple[List[str], bytes]:
    """The door recorder's copy-only reader: the URL only on stdin, video only.
    Implements: LLR-1522."""
    return door_relay.relay_consumer(
        relay, name, "ffmpeg", ["-nostdin", "-hide_banner", "-loglevel", "warning"],
        ["-map", "0:v:0", "-c", "copy", "-an", "-f", "mpegts", "pipe:1"],
        docker_extra=["--pids-limit", "64", "--memory", "256m"], timeout_us=15000000)


def run_service(env_file: str, camera: str, stream: str, clock=time.time) -> int:
    """homehub-camera-capture@<camera>-<stream>.service's ExecStart."""
    s = CaptureSettings(mc.read_knobs(env_file), camera, stream)
    state, why = window_state(s, clock())
    if state != "run":
        mc.log("%s: %s - %s" % (s.label, "REFUSED" if state == "refuse" else "not starting", why),
               warning=state == "refuse")
        return EXIT_REFUSED if state == "refuse" else 0
    relay = relay_for(env_file, s)
    rec = CaptureRecorder(env_file, s)
    ok, reason = rec.guard()
    if not ok:
        mc.log("%s: REFUSING to start - guard: %s" % (s.label, reason), warning=True)
        rec.alert("guard", "HomeHub: %s refused to start (media drive)" % s.label, reason)
        return dr.EXIT_GUARD
    dr.mc = _Relabelled(mc, s.label)  # this process only: see _Relabelled
    rec.recover_parts()
    name = container(s)
    subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=60)
    argv, payload = ffmpeg_command(relay, name)
    proc = door_relay.start_consumer(argv, payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ended = {"window": False}

    def stop(_sig, _frm):
        rec.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def window_over():
        ended["window"] = True
        rec.stopping = True
    timer = threading.Timer(max(0.0, s.until - clock()), window_over)
    timer.daemon = True
    timer.start()

    def relay_stderr():
        for line in proc.stderr:
            mc.log("%s: ffmpeg: %s" % (s.label, door_relay.redact(line.decode("utf-8", "replace").rstrip(), relay)))
    threading.Thread(target=relay_stderr, daemon=True).start()
    mc.log("%s: reading /%s from the relay at %s:%d; %ds segments into %s until %s; stream cap %d MiB, "
           "project %d limit %d MiB; video only" % (s.label, relay.stream, relay.lan_ip, relay.rtsp_port,
                                                    s.segment_sec, s.root, s.until_text, s.cap_mib,
                                                    TRAINING_PROJECT, s.quota_mib))
    code = 1
    try:
        code = rec.consume(proc.stdout)
    finally:
        timer.cancel()
        try:
            rec.finalise()
        except OSError as exc:
            mc.log("%s: could not finalise the open segment: %s" % (s.label, exc), warning=True)
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if ended["window"]:
        mc.log("%s: the capture window ended at %s - stopped, and will not restart" % (s.label, s.until_text))
        return 0
    return code


# ── ensure: the tree, its project, its limit (root, once) ────────────────────


def ensure(env_file: str, camera: str, apply: bool = True, paths: Optional[clips.MediaPaths] = None,
           act_log: Callable[[str], None] = None) -> Dict[str, object]:
    """Create /srv/media/Training/<camera>/{main,sub}: root:root 0750, the
    /srv/media default ACL removed, the camera directory in TRAINING_PROJECT
    with inheritance, the project's hard limit. Every directory is made through
    a descriptor of the mount, so a drive that went cannot put one on /. A
    camera directory that already holds files in another project is refused.
    Idempotent: a second run has no actions. Implements: LLR-1523."""
    s = CaptureSettings(mc.read_knobs(env_file), camera, "main")
    paths = paths or clips.MediaPaths()
    actions = []  # type: List[str]

    def act(text: str, fn: Optional[Callable[[], None]] = None) -> None:
        actions.append(text)
        (act_log or (lambda t: mc.log("camera capture ensure: " + t)))(text)
        if apply and fn is not None:
            fn()

    ok, reason, entry = clips.media_mount_state(paths)
    if not ok:
        raise scratch.Refused(reason)
    maj, mnr = (int(x) for x in entry["majmin"].split(":"))
    want_dev = os.makedev(maj, mnr)

    def make_private_dir(parent: str, name: str) -> str:
        path = os.path.join(parent, name)
        if not apply and not os.path.isdir(parent):
            # A dry run on a fresh drive: the parent is itself only planned.
            act("create %s" % path)
            return path
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            if os.fstat(fd).st_dev != want_dev:
                raise scratch.Refused("%s is not on the mounted media drive" % parent)
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)  # type: Optional[os.stat_result]
            except FileNotFoundError:
                st = None
            if st is not None and not stat.S_ISDIR(st.st_mode):
                raise scratch.Refused("%s exists and is not a directory" % path)
            if st is None:
                act("create %s" % path, fn=lambda: os.mkdir(name, 0o750, dir_fd=fd))
        finally:
            os.close(fd)
        if not apply and not os.path.isdir(path):
            return path
        for attr in ("system.posix_acl_default", "system.posix_acl_access"):
            try:
                os.getxattr(path, attr)
            except OSError:
                continue
            act("remove %s's %s" % (path, attr), fn=lambda a=attr: os.removexattr(path, a))
        st = os.stat(path)
        if (st.st_uid, st.st_gid) != (0, 0):
            act("set %s's owner to root:root" % path, fn=lambda: os.chown(path, 0, 0))
        if stat.S_IMODE(st.st_mode) != 0o750:
            act("set %s's mode to 0750" % path, fn=lambda: os.chmod(path, 0o750))
        return path

    make_private_dir(clips.MEDIA_MOUNT, os.path.basename(TRAINING_ROOT))
    cam = make_private_dir(TRAINING_ROOT, s.camera)
    if os.path.isdir(cam):
        project, inherit = scratch.get_project(cam)
        if (project, inherit) != (TRAINING_PROJECT, True):
            if os.listdir(cam):
                raise scratch.Refused("%s already holds files in quota project %s; move them out first"
                                      % (cam, project))
            act("put %s in quota project %d with inheritance" % (cam, TRAINING_PROJECT),
                fn=lambda: scratch.set_project(cam, TRAINING_PROJECT))
    for stream in STREAMS:
        make_private_dir(cam, stream)
    used, limit = (None, None)
    try:
        used, limit = scratch.get_project_quota(entry["source"], TRAINING_PROJECT)
    except OSError:
        pass
    if limit != s.quota_mib * MIB:
        act("set project %d's hard limit to %d MiB" % (TRAINING_PROJECT, s.quota_mib),
            fn=lambda: scratch.set_project_quota(entry["source"], TRAINING_PROJECT, s.quota_mib))
    return {"root": cam, "project": TRAINING_PROJECT, "quota_mib": s.quota_mib, "actions": actions,
            "applied": apply}


# ── status ────────────────────────────────────────────────────────────────────


def status(env_file: str, camera: str, now: Optional[float] = None) -> Dict[str, object]:
    knobs = mc.read_knobs(env_file)
    now = time.time() if now is None else now
    out = {"camera": camera, "streams": {}}  # type: Dict[str, object]
    for stream in STREAMS:
        s = CaptureSettings(knobs, camera, stream)
        segs = list_segments(s.root, s.prefix)
        starts = [segment_start(os.path.basename(p), s.prefix) for p, _n in segs]
        parts = list_parts(s.root, s.prefix)
        out["streams"][stream] = {
            "window": window_state(s, now)[0], "segments": len(segs),
            "bytes": sum(n for _p, n in segs),
            "first": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(starts[0])) if starts and starts[0] else None,
            "last": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(starts[-1])) if starts and starts[-1] else None,
            "open_part": os.path.basename(parts[-1]) if parts else None}
        out["until"] = s.until_text or None
        out["quota_mib"] = s.quota_mib
    try:
        ok, _reason, entry = clips.media_mount_state(clips.MediaPaths())
        if ok:
            used, limit = scratch.get_project_quota(entry["source"], TRAINING_PROJECT)
            out["project"] = {"id": TRAINING_PROJECT, "used_mib": used // MIB, "limit_mib": limit // MIB}
    except OSError:
        pass
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--env-file", default=mc.DEFAULT_ENV_FILE)
    p.add_argument("--instance", help="<camera>-<stream>, the unit's %%i")
    p.add_argument("--camera")
    p.add_argument("--stream", choices=STREAMS)
    sub = p.add_subparsers(dest="verb")
    sub.add_parser("condition")
    e = sub.add_parser("ensure")
    e.add_argument("--dry-run", action="store_true")
    sub.add_parser("run")
    sub.add_parser("status")
    args = p.parse_args(argv)
    try:
        if args.instance:
            camera, stream = split_instance(args.instance)
        else:
            camera, stream = cr.camera_name(args.camera or ""), args.stream
    except Invalid as exc:
        mc.log("camera capture: REFUSED - %s" % exc, warning=True)
        return EXIT_REFUSED
    try:
        if args.verb == "condition":
            if stream is None:
                raise Invalid("condition needs a stream")
            s = CaptureSettings(mc.read_knobs(args.env_file), camera, stream)
            state, why = window_state(s, time.time())
            mc.log("%s: %s" % (s.label, {"run": "starting - ", "skip": "clean skip - "}.get(state, "REFUSED - ") + why),
                   warning=state == "refuse")
            # 255 fails the start (a visible error); 1 is a clean skip.
            return {"run": 0, "skip": 1}.get(state, 255)
        if args.verb == "ensure":
            door_relay.emit(ensure(args.env_file, camera, apply=not args.dry_run))
            return 0
        if args.verb == "run":
            if stream is None:
                raise Invalid("run needs a stream")
            return run_service(args.env_file, camera, stream)
        if args.verb == "status":
            door_relay.emit(status(args.env_file, camera))
            return 0
    except (Invalid, scratch.Refused) as exc:
        mc.log("camera capture: REFUSED - %s" % exc, warning=True)
        return 255 if args.verb == "condition" else EXIT_REFUSED
    p.print_help()
    return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
