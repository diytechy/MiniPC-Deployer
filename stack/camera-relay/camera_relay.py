#!/usr/bin/env python3
"""camera_relay.py - one go2rtc relay per camera, beside the door relay (SR-155).

The living-room camera (HomeHub LIVING_ROOM_PRESENCE_PLAN_2026-11-09) gets its
OWN relay process, not a stream in the door relay, because go2rtc's RTSP
listener has one consumer credential: the door relay's is the doorbell's own,
and a second camera on it would be read with the doorbell's password (or need a
new shared credential and a panel change). Each relay therefore serves its
camera with that camera's own login, exactly as the door relay does - no new
secret - and restarting one never takes the other down.

Everything that touches a credential is the door relay's code, reused, not
copied: redact_run()/redact() for the unit's output, relay_consumer() for every
consumer (the URL only in an ffconcat document on stdin), probe(), streams(),
upstream_count() and cost(). They take any object with RelaySettings'
attributes; CameraRelaySettings is that object for camera C.

AUDIO. With CAMERA_<C>_RELAY_AUDIO=true the source carries no media filter, so
the relay offers the camera's audio track; `default_query: video` still means a
consumer that does not ask (`?video&audio`) gets video only. Hub consumers never
ask and pass -an as well: the living room's audio is passed through for
MINI-SERV's voice assistant and never kept on the hub (the plan, section 3.2).

Verbs (all take --camera C):
    condition         exit 0 only when CAMERA_<C>_RELAY_ENABLED is exactly true
                      (the unit's ExecCondition: a clean skip otherwise).
    import-password   read the camera's code from STDIN (one line) and write it
                      into the stack .env as CAMERA_<C>_RELAY_UPSTREAM_PASSWORD;
                      prints the key name only.
    render            write the relay's config to
                      /run/homehub-camera-relay-<C>/go2rtc.yaml, 0600 root.
    redact-run -- CMD the unit's ExecStart wrapper (door_relay.redact_run).
    streams           producer/consumer counts from the relay API, redacted.
    upstream-count    established TCP connections from this host to the camera.
    probe             read SECONDS of the relay as a test consumer; with
                      --audio, ask for audio too and report every track.
    audio-check       the two probes together: a consumer that does not ask
                      gets NO audio track, one that asks gets one (when the
                      relay passes audio). Exit 0 only when both hold.
    cost              CPU of the relay container (and any --container) and PSI.

Python 3.8-compatible (the dev PC's test runner imports this module).
Implements: SR-155, SR-156, LLR-1490..LLR-1495.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from typing import Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "monitoring"))
sys.path.insert(0, os.path.join(HERE, "..", "door"))
import monitor_common as mc  # noqa: E402
import door_relay  # noqa: E402

CAMERA_RE = re.compile(r"[a-z][a-z0-9]{0,15}")
Invalid = door_relay.Invalid
PROBE_GRACE_SEC = 20


def knob(camera: str, suffix: str) -> str:
    return "CAMERA_%s_RELAY_%s" % (camera.upper(), suffix)


def camera_name(text: str) -> str:
    if not CAMERA_RE.fullmatch(text or ""):
        raise Invalid("a camera name is [a-z][a-z0-9]{0,15}")
    if text == "door":
        raise Invalid("the door camera has its own relay (stack/door)")
    return text


def run_dir(camera: str) -> str:
    return "/run/homehub-camera-relay-%s" % camera


def container(camera: str) -> str:
    return "homehub-camera-relay-%s" % camera


class CameraRelaySettings:
    """Camera C's relay fields, validated; door_relay.RelaySettings' attributes.

    Contract:
      Config:  LAN_IP; CAMERA_<C>_RELAY_RTSP_PORT and _API_PORT (required, and
               neither may be the door relay's DOOR_RELAY_RTSP_PORT /
               DOOR_RELAY_API_PORT, nor each other); _STREAM (C);
               _UPSTREAM_HOST (IPv4), _UPSTREAM_PORT (554), _UPSTREAM_PATH
               (required), _UPSTREAM_USERNAME (admin), _UPSTREAM_PASSWORD
               (required; from the deploy store); _AUDIO (false);
               _EXPECT_CODEC (h264), _EXPECT_SIZE (WIDTHxHEIGHT, required).
               [stack/.env]
      Raises:  Invalid naming the key, never its value.
    Implements: LLR-1490.
    """

    def __init__(self, env_file: str, camera: str):
        self.camera = camera_name(camera)
        knobs = mc.read_knobs(env_file)
        raw = door_relay.read_env_raw(env_file)

        def plain(suffix: str, default: str = "") -> str:
            return knobs.get(knob(camera, suffix), default).strip() or default

        self.lan_ip = knobs.get("LAN_IP", "").strip()
        if not door_relay.IPV4_RE.fullmatch(self.lan_ip):
            raise Invalid("LAN_IP is not an IPv4 address")
        self.rtsp_port = self._port(plain("RTSP_PORT"), knob(camera, "RTSP_PORT"))
        self.api_port = self._port(plain("API_PORT"), knob(camera, "API_PORT"))
        door_ports = {knobs.get("DOOR_RELAY_RTSP_PORT", "").strip() or "8554",
                      knobs.get("DOOR_RELAY_API_PORT", "").strip() or "1984"}
        for key, port in ((knob(camera, "RTSP_PORT"), self.rtsp_port), (knob(camera, "API_PORT"), self.api_port)):
            if str(port) in door_ports:
                raise Invalid("%s is one of the door relay's ports" % key)
        if self.rtsp_port == self.api_port:
            raise Invalid("%s and %s are the same port" % (knob(camera, "RTSP_PORT"), knob(camera, "API_PORT")))
        self.stream = plain("STREAM", camera)
        if not door_relay.STREAM_RE.fullmatch(self.stream):
            raise Invalid("%s must match [a-z][a-z0-9_-]{0,31}" % knob(camera, "STREAM"))
        self.host = plain("UPSTREAM_HOST")
        if not door_relay.IPV4_RE.fullmatch(self.host):
            raise Invalid("%s is missing or not an IPv4 address" % knob(camera, "UPSTREAM_HOST"))
        if self.host == self.lan_ip:
            raise Invalid("%s is this hub's own address: the relay would relay itself" % knob(camera, "UPSTREAM_HOST"))
        self.port = self._port(plain("UPSTREAM_PORT", "554"), knob(camera, "UPSTREAM_PORT"))
        self.path = plain("UPSTREAM_PATH")
        if not self.path.startswith("/") or re.search(r"[\s?#]", self.path):
            raise Invalid("%s must start with / and hold no space, ? or #" % knob(camera, "UPSTREAM_PATH"))
        # THE MAIN STREAM, OPTIONAL (2026-11-10, the training capture). Served as
        # /<stream>-main beside /<stream>, always video only: nothing on the hub
        # or beyond asks the main stream for sound. Unset, the relay is as before.
        self.main_path = plain("MAIN_PATH") or None
        if self.main_path is not None and (not self.main_path.startswith("/")
                                           or re.search(r"[\s?#]", self.main_path)):
            raise Invalid("%s must start with / and hold no space, ? or #" % knob(camera, "MAIN_PATH"))
        if self.main_path == self.path:
            raise Invalid("%s is the same path as %s" % (knob(camera, "MAIN_PATH"), knob(camera, "UPSTREAM_PATH")))
        self.main_stream = (self.stream + "-main") if self.main_path else None
        if self.main_stream is not None and not door_relay.STREAM_RE.fullmatch(self.main_stream):
            raise Invalid("%s is too long to name a -main stream as well" % knob(camera, "STREAM"))
        self.username = door_relay.env_secret_value(raw.get(knob(camera, "UPSTREAM_USERNAME"), "")) or "admin"
        self.password = door_relay.env_secret_value(raw.get(knob(camera, "UPSTREAM_PASSWORD"), ""))
        for key, value in (("UPSTREAM_USERNAME", self.username), ("UPSTREAM_PASSWORD", self.password)):
            if not value or re.search(r"[\r\n\0]", value):
                raise Invalid("%s is missing or holds a control character" % knob(camera, key))
        self.audio = mc.knob_bool(knobs, knob(camera, "AUDIO"), False)
        self.expect_codec = plain("EXPECT_CODEC", "h264")
        if not re.fullmatch(r"[a-z0-9_]{1,32}", self.expect_codec):
            raise Invalid("%s must be an ffprobe codec name such as h264" % knob(camera, "EXPECT_CODEC"))
        size = re.fullmatch(r"([0-9]{2,5})x([0-9]{2,5})", plain("EXPECT_SIZE"))
        if not size:
            raise Invalid("%s must be WIDTHxHEIGHT" % knob(camera, "EXPECT_SIZE"))
        self.expect_width, self.expect_height = int(size.group(1)), int(size.group(2))
        # What a consumer appends to the stream name. Empty: the relay's
        # default_query (video only). audio_probe() sets "?video&audio".
        self.consumer_query = ""

    @staticmethod
    def _port(text: str, key: str) -> int:
        if not re.fullmatch(r"[0-9]{1,5}", text or "") or not 1 <= int(text) <= 65535:
            raise Invalid("%s is missing or not a port number" % key)
        return int(text)

    def upstream_url(self, path: Optional[str] = None) -> str:
        q = lambda s: urllib.parse.quote(s, safe="")  # noqa: E731
        return "rtsp://%s:%s@%s:%d%s" % (q(self.username), q(self.password), self.host, self.port,
                                         path or self.path)

    def for_main(self) -> "CameraRelaySettings":
        """These settings as a consumer of the relay's /<stream>-main stream.
        Raises Invalid when the relay serves no main stream."""
        if self.main_stream is None:
            raise Invalid("%s is not set: the relay serves no main stream" % knob(self.camera, "MAIN_PATH"))
        main = copy.copy(self)
        main.stream = self.main_stream
        return main

    def consumer_url(self) -> str:
        q = lambda s: urllib.parse.quote(s, safe="")  # noqa: E731
        return "rtsp://%s:%s@%s:%d/%s%s" % (q(self.username), q(self.password), self.lan_ip, self.rtsp_port,
                                            self.stream, self.consumer_query)


def relay_config(s: CameraRelaySettings) -> Dict[str, object]:
    """go2rtc's whole configuration for camera C; every listener is named.

    The door relay's config (door_relay.relay_config: loopback API behind the
    camera's credential with local_auth, RTSP on the LAN address behind it,
    WebRTC/SRTP/RTMP off, info logging that only redact-run may print), with
    this camera's ports and, when it passes audio, no media filter on the
    source. `default_query: video` stays either way.
    Implements: SR-155, LLR-1491.
    """
    cfg = door_relay.relay_config(s)
    cfg["streams"] = {s.stream: s.upstream_url() + ("" if s.audio else "#media=video")}
    if s.main_path:
        cfg["streams"][s.main_stream] = s.upstream_url(s.main_path) + "#media=video"
    return cfg


def audio_probe(s: CameraRelaySettings, seconds: int, ask: bool, name: str) -> Dict[str, object]:
    """Every track a consumer receives, asking for audio or not (ffprobe).

    Implements: LLR-1492.
    """
    probe_s = copy.copy(s)
    probe_s.consumer_query = "?video&audio" if ask else ""
    argv, payload = door_relay.relay_consumer(
        probe_s, name, "ffprobe", ["-v", "error"],
        ["-read_intervals", "%%+%d" % seconds, "-show_entries", "stream=codec_type,codec_name,sample_rate,channels",
         "-of", "json"])
    door_relay.CONSUMERS.start(name)
    try:
        raw = subprocess.run(argv, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=seconds + PROBE_GRACE_SEC)
    except subprocess.TimeoutExpired:
        door_relay.remove_containers(name)
        return {"asked_audio": ask, "rc": -1, "timeout": True}
    except BaseException:
        door_relay.remove_containers(name)
        raise
    out: Dict[str, object] = {"asked_audio": ask, "rc": raw.returncode,
                              "stderr": door_relay.redact(raw.stderr.decode("utf-8", "replace").strip(), s)[-400:]}
    try:
        tracks = json.loads(raw.stdout.decode("utf-8", "replace")).get("streams") or []
        out["tracks"] = [{k: t.get(k) for k in ("codec_type", "codec_name", "sample_rate", "channels") if k in t}
                         for t in tracks]
    except ValueError:
        out["parse_error"] = True
    return out


def import_password(env_file: str, camera: str, blob: bytes) -> str:
    """Write CAMERA_<C>_RELAY_UPSTREAM_PASSWORD from STDIN into the stack .env.

    The live hub's .env is never rewritten by a delivery, so the camera's code
    reaches it this way: piped from the deploy store, one line, never in argv,
    never printed. One trailing newline is dropped; the value is encoded the
    materialiser's way (door_relay.env_encode) and written atomically with the
    file's own mode and owner. Returns the key name. Implements: LLR-1493.
    """
    if len(blob) > 1024:
        raise Invalid("the password on stdin is longer than 1 KiB")
    text = blob.decode("utf-8")
    if text.endswith("\n"):
        text = text[:-1]
    if text.endswith("\r"):
        text = text[:-1]
    if not text or re.search(r"[\r\n\0]", text):
        raise Invalid("the password on stdin is empty or holds a control character")
    key = knob(camera, "UPSTREAM_PASSWORD")
    with open(env_file, encoding="utf-8") as handle:
        current = handle.read()
    st = os.stat(env_file)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(env_file)), prefix=".env.tmp-")
    try:
        if hasattr(os, "fchmod"):  # the hub; the dev PC's Windows test runner has neither
            os.fchmod(fd, st.st_mode & 0o7777)
            try:
                os.fchown(fd, st.st_uid, st.st_gid)
            except PermissionError:
                pass
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(door_relay.set_env_keys(current, {key: door_relay.env_encode(text)}))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, env_file)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return key


def audio_verdict(withheld: Dict[str, object], asked: Dict[str, object], passes_audio: bool) -> List[str]:
    """Reasons the audio check fails; empty when it passes. Implements: LLR-1492."""
    reasons = []

    def kinds(result):
        return [t.get("codec_type") for t in result.get("tracks") or []]
    for label, result in (("default consumer", withheld), ("asking consumer", asked)):
        if result.get("rc") != 0 or "tracks" not in result:
            reasons.append("%s: the probe did not complete" % label)
    if reasons:
        return reasons
    if "audio" in kinds(withheld):
        reasons.append("a consumer that did not ask for audio received an audio track")
    if "video" not in kinds(withheld):
        reasons.append("a consumer that did not ask for audio received no video")
    if passes_audio and "audio" not in kinds(asked):
        reasons.append("the relay passes audio, but a consumer that asked received none")
    if not passes_audio and "audio" in kinds(asked):
        reasons.append("the relay is video-only, but a consumer that asked received audio")
    return reasons


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--env-file", default=mc.DEFAULT_ENV_FILE)
    p.add_argument("--camera", required=True)
    sub = p.add_subparsers(dest="verb")
    sub.add_parser("condition")
    sub.add_parser("import-password")
    r = sub.add_parser("render")
    r.add_argument("--out", default=None)
    rr = sub.add_parser("redact-run")
    rr.add_argument("cmd", nargs=argparse.REMAINDER)
    sub.add_parser("streams")
    sub.add_parser("upstream-count")
    pr = sub.add_parser("probe")
    pr.add_argument("--seconds", type=int, default=10)
    pr.add_argument("--audio", action="store_true", help="ask the relay for audio and list every track")
    ac = sub.add_parser("audio-check")
    ac.add_argument("--seconds", type=int, default=5)
    co = sub.add_parser("cost")
    co.add_argument("--seconds", type=int, default=1800)
    co.add_argument("--container", action="append", default=[])
    args = p.parse_args(argv)
    tag = "camera-relay"
    try:
        camera = camera_name(args.camera)
    except Invalid as exc:
        mc.log("%s: REFUSED - %s" % (tag, exc), warning=True)
        return 2
    tag = "camera-relay %s" % camera
    if args.verb == "condition":
        on = mc.knob_bool(mc.read_knobs(args.env_file), knob(camera, "ENABLED"), False)
        mc.log("%s: %s is %s" % (tag, knob(camera, "ENABLED"), "true - starting" if on else "not true - clean skip"))
        return 0 if on else 1
    if args.verb == "import-password":
        try:
            key = import_password(args.env_file, camera, sys.stdin.buffer.read(4096))
        except (Invalid, UnicodeDecodeError, OSError) as exc:
            mc.log("%s: REFUSED - %s" % (tag, type(exc).__name__ if not isinstance(exc, Invalid) else exc),
                   warning=True)
            return 2
        mc.log("%s: wrote %s into %s (value not shown)" % (tag, key, args.env_file))
        return 0
    try:
        s = CameraRelaySettings(args.env_file, camera)
    except Invalid as exc:
        mc.log("%s: REFUSED - %s" % (tag, exc), warning=True)
        return 2
    if args.verb == "render":
        out = args.out or os.path.join(run_dir(camera), "go2rtc.yaml")
        door_relay.write_private(out, (json.dumps(relay_config(s), indent=1) + "\n").encode("utf-8"))
        mc.log("%s: relay config written to %s (0600): rtsp %s:%d (auth), api 127.0.0.1:%d (auth), "
               "webrtc/srtp/rtmp off, stream /%s from the camera on port %d (%s; consumers get video "
               "unless they ask)%s" % (tag, out, s.lan_ip, s.rtsp_port, s.api_port, s.stream, s.port,
                                       "video and audio" if s.audio else "video only",
                                       "; and /%s, the main stream, video only" % s.main_stream
                                       if s.main_stream else ""))
        return 0
    if args.verb == "redact-run":
        cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
        if not cmd:
            mc.log("%s: redact-run needs -- CMD" % tag, warning=True)
            return 2
        return door_relay.redact_run(s, cmd)
    if args.verb == "streams":
        door_relay.emit(door_relay.streams(s))
        return 0
    if args.verb == "upstream-count":
        door_relay.emit({"upstream_connections": door_relay.upstream_count(s), "upstream": "<camera>:%d" % s.port})
        return 0
    try:
        with door_relay.stop_signals_raise():
            door_relay.CONSUMERS.reset()
            if args.verb == "probe":
                name = "homehub-camera-probe-%s" % camera
                if args.audio:
                    door_relay.emit(audio_probe(s, args.seconds, True, name))
                    return 0
                result = door_relay.probe(s, args.seconds, name=name)
                reasons = door_relay.probe_verdict(result, s)
                door_relay.emit(dict(result, **{"pass": not reasons, "reasons": reasons}))
                return 0 if not reasons else 1
            if args.verb == "audio-check":
                withheld = audio_probe(s, args.seconds, False, "homehub-camera-probe-%s" % camera)
                asked = audio_probe(s, args.seconds, True, "homehub-camera-probe-%s" % camera)
                reasons = audio_verdict(withheld, asked, s.audio)
                door_relay.emit({"default_consumer": withheld, "asking_consumer": asked,
                                 "relay_passes_audio": s.audio, "pass": not reasons, "reasons": reasons})
                return 0 if not reasons else 1
            if args.verb == "cost":
                groups = {}
                for name in [container(camera)] + list(args.container):
                    groups[name] = door_relay.container_cgroup(name) or "/nonexistent/%s" % name
                door_relay.emit(door_relay.cost(groups, args.seconds))
                return 0
    except door_relay.Stopped as exc:
        mc.log(door_relay.stop_line(args.verb, exc.signum).replace("door:", tag + ":", 1), warning=True)
        return exc.code
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
