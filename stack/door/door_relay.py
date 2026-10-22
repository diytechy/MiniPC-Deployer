#!/usr/bin/env python3
"""door_relay.py - the door relay's host side: config, credentials, measurement.

The RemoBell S accepts ONE video connection. The relay (go2rtc, pinned in
door-relay-image.pin) takes that one connection and re-serves it on the hub's
LAN address, so the panel's door view, the recorder and later a detector can all
watch at once while the camera still sees exactly one client (SN-050, SR-105).

Verbs:
    condition KNOB    exit 0 when the knob is exactly `true`, 1 otherwise: the
                      relay and recorder units' ExecCondition (a clean skip).
    render            write the relay's config (JSON, which go2rtc reads as YAML)
                      to /run/homehub-door-relay/go2rtc.yaml, 0600 root, from the
                      stack .env. Refuses on any missing or malformed field.
    import-upstream   read the doorbell's in-use RTSP fields from STDIN as
                      NUL-separated name=value records (host, port, path,
                      username, password - what the panel's
                      `door-cutover-panel.sh export-upstream` prints), and write
                      them into the stack .env as DOOR_RELAY_UPSTREAM_*. Prints
                      key names only, never a value; the value never sees argv.
    redact-run -- CMD the relay unit's ExecStart wrapper: runs CMD (the attached
                      `docker compose up`) with its stdout and stderr merged
                      through redact(), line by line, so nothing go2rtc prints
                      - its producer warns with the whole camera URL when the
                      camera drops - reaches the journal with the credential or
                      the camera's address in it. Exits with CMD's status.
    streams           the relay API's producer/consumer counts, URLs redacted.
    upstream-count    established TCP connections from this host to the camera.
    probe             read SECONDS of the relay with the doorbell's credential (a
                      test consumer): codec, geometry, frame count, as JSON.
    gate              the compatibility-gate measurements (plan W4b task 8) that
                      run on the hub, in order, as JSON lines.
    cost              CPU (cgroup) and PSI sampled for a duration (task 11).

LISTENERS, ALL NAMED (SR-105): RTSP on LAN_IP:DOOR_RELAY_RTSP_PORT with
consumer authentication (the doorbell's own username and password - no new
secret); the API on 127.0.0.1:DOOR_RELAY_API_PORT behind the same credential
(it reports the source URL, so it is never open even on loopback); WebRTC,
SRTP and RTMP disabled by an empty listen address. Video only: the source is
`#media=video` and the default consumer query is `video`. No Caddy route and no
DNS name: the relay is reached by address from the LAN alone.

EVERY CONSUMER (the recorder, probe, the gate's consumers, the cost decoder)
is built by relay_consumer(): ffmpeg/ffprobe as the container's entrypoint,
reading `-f concat -i pipe:0`, and the authenticated URL in a one-entry
ffconcat document written to its stdin - never in an argument, an environment
variable or a file (see the block above relay_consumer).

Python 3.8-compatible (the dev PC's test runner imports this module).
Implements: SR-105, SR-106, LLR-1223..LLR-1229.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "monitoring"))
import monitor_common as mc  # noqa: E402

RUN_DIR = "/run/homehub-door-relay"
CONFIG_PATH = os.path.join(RUN_DIR, "go2rtc.yaml")
PIN_FILE = os.path.join(HERE, "door-relay-image.pin")
CONTAINER = "homehub-door-relay"

UPSTREAM_FIELDS = ("host", "port", "path", "username", "password")
UPSTREAM_KNOB = {f: "DOOR_RELAY_UPSTREAM_" + f.upper() for f in UPSTREAM_FIELDS}
IPV4_RE = re.compile(r"(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])){3}")
STREAM_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
SECRET_URL_RE = re.compile(r"(rtsps?://)[^/@\s]*@")


class Invalid(ValueError):
    """A configuration field is missing or malformed; the message names the KEY only."""


# ── reading the stack .env for a SECRET-bearing field ────────────────────────


def env_secret_value(raw: str) -> str:
    """One .env value as the materialiser wrote it, back to its plaintext.

    Materialize-Deploy doubles every `$` for compose and wraps a value that holds
    whitespace in one pair of double quotes (its QUOTING RULE). The monitors'
    read_env_text also strips a ` # comment`, which is right for knobs and
    WRONG for a password, so the secret fields are read with this instead.
    """
    value = raw.rstrip("\r")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value.replace("$$", "$")


def read_env_raw(path: str) -> Dict[str, str]:
    """{KEY: raw text after '='} - last assignment wins, nothing stripped."""
    out: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.rstrip("\n"))
                if m:
                    out[m.group(1)] = m.group(2)
    except OSError:
        pass
    return out


class RelaySettings:
    """The relay's fields, validated (SR-105, LLR-1223).

    Contract:
      Config:  LAN_IP; DOOR_RELAY_RTSP_PORT (8554), DOOR_RELAY_API_PORT (1984),
               DOOR_RELAY_STREAM (door); DOOR_RELAY_UPSTREAM_HOST (an IPv4
               address), _PORT (554), _PATH (/H.264), _USERNAME (admin) and
               _PASSWORD (required; from the deploy store);
               DOOR_RELAY_EXPECT_CODEC (h264) and DOOR_RELAY_EXPECT_SIZE
               (1536x1536): what the readiness gate requires.  [stack/.env]
      Raises:  Invalid naming the key, never its value.
    """

    def __init__(self, env_file: str):
        knobs = mc.read_knobs(env_file)
        raw = read_env_raw(env_file)

        def plain(key: str, default: str = "") -> str:
            return knobs.get(key, default).strip() or default

        self.lan_ip = plain("LAN_IP")
        if not IPV4_RE.fullmatch(self.lan_ip):
            raise Invalid("LAN_IP is not an IPv4 address")
        self.rtsp_port = self._port(plain("DOOR_RELAY_RTSP_PORT", "8554"), "DOOR_RELAY_RTSP_PORT")
        self.api_port = self._port(plain("DOOR_RELAY_API_PORT", "1984"), "DOOR_RELAY_API_PORT")
        self.stream = plain("DOOR_RELAY_STREAM", "door")
        if not STREAM_RE.fullmatch(self.stream):
            raise Invalid("DOOR_RELAY_STREAM must match [a-z][a-z0-9_-]{0,31}")
        self.host = plain(UPSTREAM_KNOB["host"])
        if not IPV4_RE.fullmatch(self.host):
            raise Invalid("DOOR_RELAY_UPSTREAM_HOST is missing or not an IPv4 address")
        if self.host == self.lan_ip:
            raise Invalid("DOOR_RELAY_UPSTREAM_HOST is this hub's own address: the relay would relay itself")
        self.port = self._port(plain(UPSTREAM_KNOB["port"], "554"), "DOOR_RELAY_UPSTREAM_PORT")
        self.path = plain(UPSTREAM_KNOB["path"], "/H.264")
        if not self.path.startswith("/") or re.search(r"[\s?#]", self.path):
            raise Invalid("DOOR_RELAY_UPSTREAM_PATH must start with / and hold no space, ? or #")
        self.username = env_secret_value(raw.get(UPSTREAM_KNOB["username"], "")) or "admin"
        self.password = env_secret_value(raw.get(UPSTREAM_KNOB["password"], ""))
        for key, value in (("USERNAME", self.username), ("PASSWORD", self.password)):
            if not value or re.search(r"[\r\n\0]", value):
                raise Invalid("DOOR_RELAY_UPSTREAM_%s is missing or holds a control character" % key)
        # What the readiness gate requires the relay to deliver: the camera's
        # own stream, unchanged (raw fisheye, measured on the panel 2026-10-22).
        self.expect_codec = plain("DOOR_RELAY_EXPECT_CODEC", "h264")
        if not re.fullmatch(r"[a-z0-9_]{1,32}", self.expect_codec):
            raise Invalid("DOOR_RELAY_EXPECT_CODEC must be an ffprobe codec name such as h264")
        size = re.fullmatch(r"([0-9]{2,5})x([0-9]{2,5})", plain("DOOR_RELAY_EXPECT_SIZE", "1536x1536"))
        if not size:
            raise Invalid("DOOR_RELAY_EXPECT_SIZE must be WIDTHxHEIGHT")
        self.expect_width, self.expect_height = int(size.group(1)), int(size.group(2))

    @staticmethod
    def _port(text: str, key: str) -> int:
        if not re.fullmatch(r"[0-9]{1,5}", text) or not 1 <= int(text) <= 65535:
            raise Invalid("%s is not a port number" % key)
        return int(text)

    def upstream_url(self) -> str:
        """The camera's URL with the credential percent-encoded (as the panel builds it)."""
        q = lambda s: urllib.parse.quote(s, safe="")  # noqa: E731
        return "rtsp://%s:%s@%s:%d%s" % (q(self.username), q(self.password), self.host, self.port, self.path)

    def consumer_url(self) -> str:
        """The relay's URL for an authenticated consumer on this host."""
        q = lambda s: urllib.parse.quote(s, safe="")  # noqa: E731
        return "rtsp://%s:%s@%s:%d/%s" % (q(self.username), q(self.password), self.lan_ip, self.rtsp_port, self.stream)


def relay_config(s: RelaySettings) -> Dict[str, object]:
    """go2rtc's whole configuration; every module with a listener is named.

    `listen: ""` disables a module's listener (measured 2026-10-22 against the
    pinned 1.9.14: with this config the process holds exactly two sockets, the
    RTSP one on the LAN address and the API one on loopback).
    Implements: SR-105, LLR-1223.
    """
    return {
        # local_auth: go2rtc otherwise exempts loopback clients from the API's
        # credential (measured: 200 without it, 401 with it), and the API
        # reports the camera URL. RTSP needs no such flag: it is bound to the
        # LAN address only, and a client reaching that address is never treated
        # as local (measured: 401 without the credential, from the host itself).
        "api": {"listen": "127.0.0.1:%d" % s.api_port, "username": s.username, "password": s.password,
                "local_auth": True},
        "rtsp": {"listen": "%s:%d" % (s.lan_ip, s.rtsp_port), "username": s.username,
                 "password": s.password, "default_query": "video"},
        "webrtc": {"listen": ""},
        "srtp": {"listen": ""},
        "rtmp": {"listen": ""},
        # info, deliberately: the producer's reconnect warnings are the only
        # record of a camera dropout. They carry the credentialed URL, so the
        # relay's output reaches the journal ONLY through `redact-run`, and the
        # compose service logs to no driver at all (LLR-1225).
        "log": {"level": "info", "format": "text"},
        "streams": {s.stream: s.upstream_url() + "#media=video"},
    }


def write_private(path: str, data: bytes) -> None:
    """Write a root-only (0600) file atomically in its own directory."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── import-upstream: the panel's values into the hub's .env, through a pipe ──


def parse_upstream_records(blob: bytes) -> Dict[str, str]:
    """NUL-separated `field=value` records -> {field: value}; exactly the five.

    Raises Invalid naming the field, never the value.
    """
    out: Dict[str, str] = {}
    for record in blob.split(b"\0"):
        if not record:
            continue
        name, sep, value = record.decode("utf-8").partition("=")
        if not sep or name not in UPSTREAM_FIELDS:
            raise Invalid("unexpected record %r" % (name if sep else "<no '='>"))
        if name in out:
            raise Invalid("duplicate record %s" % name)
        if re.search(r"[\r\n\0]", value) or not value:
            raise Invalid("record %s is empty or holds a control character" % name)
        out[name] = value
    missing = [f for f in UPSTREAM_FIELDS if f not in out]
    if missing:
        raise Invalid("missing records: %s" % " ".join(missing))
    return out


def env_encode(value: str) -> str:
    """The materialiser's representation of a plaintext in the compose .env."""
    out = value.replace("$", "$$")
    return '"%s"' % out if re.search(r"\s", value) else out


def set_env_keys(text: str, values: Dict[str, str]) -> str:
    """Replace each KEY=... line (every occurrence) or append it; other lines kept."""
    lines = text.splitlines()
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m and m.group(1) in values:
            lines[i] = "%s=%s" % (m.group(1), values[m.group(1)])
            seen.add(m.group(1))
    for key in values:
        if key not in seen:
            lines.append("%s=%s" % (key, values[key]))
    return "\n".join(lines) + "\n"


def import_upstream(env_file: str, blob: bytes) -> List[str]:
    """Write the five DOOR_RELAY_UPSTREAM_* keys; returns the key names written."""
    records = parse_upstream_records(blob)
    if not IPV4_RE.fullmatch(records["host"]):
        raise Invalid("record host is not an IPv4 address")
    if not re.fullmatch(r"[0-9]{1,5}", records["port"]):
        raise Invalid("record port is not a port number")
    values = {UPSTREAM_KNOB[f]: (env_encode(records[f]) if f in ("username", "password") else records[f])
              for f in UPSTREAM_FIELDS}
    with open(env_file, encoding="utf-8") as handle:
        text = handle.read()
    st = os.stat(env_file)
    directory = os.path.dirname(os.path.abspath(env_file))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".env.tmp-")
    try:
        os.fchmod(fd, st.st_mode & 0o7777)
        try:
            os.fchown(fd, st.st_uid, st.st_gid)
        except PermissionError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(set_env_keys(text, values))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, env_file)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return sorted(values)


# ── measurement ──────────────────────────────────────────────────────────────


def secret_forms(s: RelaySettings) -> List[str]:
    """Every spelling of the credential a log line could carry, longest first:
    the password raw and percent-encoded (both quote() variants), and the
    HTTP Basic token the API is reached with."""
    forms = {s.password, urllib.parse.quote(s.password, safe=""), urllib.parse.quote(s.password),
             base64.b64encode(("%s:%s" % (s.username, s.password)).encode()).decode()}
    return sorted((f for f in forms if f), key=len, reverse=True)


def redact(text: str, s: Optional[RelaySettings] = None) -> str:
    """Credentials in any rtsp:// URL become ***; applied to every relayed line.

    With the settings it also removes every other spelling of the password
    (secret_forms) and the camera's address: `host:port` always, and the bare
    host unless it is a loopback address (a lab camera on 127.x would otherwise
    blank the API's own listen line). The camera's address is a household value
    the journal does not need: the port is kept, the host becomes <camera>.
    Implements: LLR-1226.
    """
    text = SECRET_URL_RE.sub(r"\1***@", text)
    if s is None:
        return text
    for form in secret_forms(s):
        text = text.replace(form, "***")
    text = text.replace("%s:%d" % (s.host, s.port), "<camera>:%d" % s.port)
    if not s.host.startswith("127."):
        text = re.sub(r"(?<![0-9.])%s(?![0-9])" % re.escape(s.host), "<camera>", text)
    return text


def redact_run(s: RelaySettings, argv: Sequence[str], out=None) -> int:
    """Run argv with stdout+stderr merged through redact(), one line at a time
    (the relay unit's ExecStart). SIGTERM/SIGINT/SIGHUP are passed to the child;
    the child's exit status is returned (128+N for a signal). Nothing is
    buffered past a line, so the journal keeps go2rtc's timing.
    Implements: LLR-1225, LLR-1226.
    """
    out = out or sys.stdout
    proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)

    def forward(sig, _frame):
        try:
            proc.send_signal(sig)
        except OSError:
            pass
    previous = {}
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:  # SIGHUP does not exist on the dev PC's Windows Python
            continue
        try:
            previous[sig] = signal.signal(sig, forward)
        except ValueError:  # not the main thread: nothing to forward
            pass
    try:
        for raw in iter(proc.stdout.readline, b""):
            out.write(redact(raw.decode("utf-8", "replace").rstrip("\r\n"), s) + "\n")
            out.flush()
        rc = proc.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return rc if rc >= 0 else 128 - rc


def api_stream(s: RelaySettings) -> Dict[str, object]:
    """The relay API's entry for our stream (GET /api/streams, Basic auth)."""
    req = urllib.request.Request("http://127.0.0.1:%d/api/streams" % s.api_port)
    token = base64.b64encode(("%s:%s" % (s.username, s.password)).encode()).decode()
    req.add_header("Authorization", "Basic " + token)
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get(s.stream) or {}


def streams(s: RelaySettings) -> Dict[str, object]:
    """The relay API's view of our stream: producer and consumer counts."""
    entry = api_stream(s)
    producers = entry.get("producers") or []
    consumers = entry.get("consumers") or []
    return {"stream": s.stream, "producers": len(producers), "consumers": len(consumers),
            "producer_states": [redact(json.dumps({k: v for k, v in p.items() if k in ("url", "remote_addr", "state")}), s)
                                for p in producers]}


def consumer_bytes(s: RelaySettings) -> Optional[Dict[str, int]]:
    """{consumers, sent_bytes}: how many consumers the relay is serving and the
    bytes it has sent them in total. Measured on the pinned 1.9.14 (lab,
    2026-10-22): an RTSP consumer carries no connection-level counter; each of
    its `senders` carries its own `bytes` (and `packets`), which is what is
    summed. A missing field counts 0, so a renamed one can only ever read as
    no progress. None when the API cannot answer, which a caller must read as
    "no evidence", never as progress."""
    try:
        consumers = api_stream(s).get("consumers") or []
    except (OSError, ValueError):
        return None
    total = sum(int(sender.get("bytes") or 0) for c in consumers for sender in (c.get("senders") or []))
    return {"consumers": len(consumers), "sent_bytes": total}


def count_established(text: str, host: str, port: int) -> int:
    """Established connections to host:port in `ss -Htn state established` output.

    With a state filter ss drops the State column: Recv-Q Send-Q Local Peer.
    """
    want = "%s:%d" % (host, port)
    return sum(1 for line in text.splitlines() if len(line.split()) >= 4 and line.split()[3] == want)


def upstream_count(s: RelaySettings) -> int:
    """Established TCP connections from this host to the camera, from ss."""
    out = subprocess.run(["ss", "-Htn", "state", "established"], stdout=subprocess.PIPE,
                         universal_newlines=True, timeout=10).stdout
    return count_established(out, s.host, s.port)


def image_ref() -> str:
    """The pinned image reference, name:tag@digest, from door-relay-image.pin."""
    pin = {}
    with open(PIN_FILE, encoding="utf-8") as handle:
        for line in handle:
            m = re.match(r"([A-Z0-9_]+)=(\S+)$", line.strip())
            if m:
                pin[m.group(1)] = m.group(2)
    return "%s:%s@%s" % (pin["IMAGE"], pin["VERSION"], pin["DIGEST"])


# ── every consumer of the relay: the URL on stdin, never in argv or env ─────
#
# The relay's consumer URL carries the doorbell's credential. A value in argv is
# readable by every local user from /proc/<pid>/cmdline - a container's
# processes included, since they are host processes - and a value in a
# container's environment is kept in `docker inspect` (Config.Env) and in
# /proc/<pid>/environ. So, exactly as the panel's door broker does
# (OfficeWallNaglight doorstream/service.py ffconcat_input), the URL reaches
# ffmpeg/ffprobe as a one-entry ffconcat document on its STDIN: the tool reads
# `-f concat -i pipe:0`, the docker client runs with -i, the document is
# written and stdin is closed (`docker run -i` closes the container's stdin at
# that EOF, which is what ends the concat demuxer's playlist read). The rtsp
# options ride in the document (`option`, ffmpeg >= 5; the pinned image has
# 8.0.1), because an input option on the command line would apply to the
# concat demuxer, not to the rtsp input it opens. The entrypoint is the tool
# itself: no shell, so nothing can expand a variable into an argument.
#
# Measured 2026-10-22 against the pinned image: ffprobe through this input
# reports the same codec, size and packet count; a `-c copy -f mpegts` copy
# through it is the same stream, with the same two mpegts warnings, as the
# direct rtsp input's.

FFCONCAT_INPUT = ("-f", "concat", "-safe", "0",
                  "-protocol_whitelist", "file,pipe,tcp,udp,rtp,rtsp,crypto", "-i", "pipe:0")
CONSUMER_TOOLS = ("ffmpeg", "ffprobe")


def relay_input_document(s: RelaySettings, timeout_us: Optional[int] = None) -> bytes:
    """The ffconcat document naming the relay's authenticated URL, for stdin.

    The credential is percent-encoded and the stream name is [a-z0-9_-], so the
    URL can hold no quote, backslash or whitespace; that is re-checked rather
    than assumed, because one would end the document's quoted string early.
    """
    url = s.consumer_url()
    if re.search(r"['\\\s]", url):
        raise Invalid("the relay URL holds a quote, backslash or space (DOOR_RELAY_STREAM / LAN_IP)")
    lines = ["ffconcat version 1.0", "file '%s'" % url, "option rtsp_transport tcp"]
    if timeout_us:
        lines.append("option timeout %d" % int(timeout_us))
    return ("\n".join(lines) + "\n").encode("utf-8")


def relay_consumer(s: RelaySettings, name: str, tool: str, before_input: Sequence[str],
                   after_input: Sequence[str], docker_extra: Sequence[str] = (),
                   timeout_us: Optional[int] = None):
    """(argv, stdin payload) for one consumer of the relay in the pinned image.

    The ONE builder for every call site - the recorder, the probe, the gate's
    consumers and the cost decoder - so none can drift back to a URL in argv.
    `before_input` are the tool's global options, `after_input` everything
    after the input (maps, codecs, the output). Implements: LLR-1226, LLR-1236.
    """
    if tool not in CONSUMER_TOOLS:
        raise ValueError("a relay consumer runs ffmpeg or ffprobe, not %r" % tool)
    argv = ["docker", "run", "--rm", "-i", "--name", name, "--network", "host", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--log-driver", "none"]
    argv += list(docker_extra) + ["--entrypoint", tool, image_ref()]
    argv += list(before_input) + list(FFCONCAT_INPUT) + list(after_input)
    return argv, relay_input_document(s, timeout_us)


def start_consumer(argv: Sequence[str], payload: bytes, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL) -> subprocess.Popen:
    """Start a relay consumer and hand it its input document on stdin, then close
    stdin. A consumer that died before reading it is the caller's to see (poll,
    EOF): a broken pipe here is not raised."""
    proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=stdout, stderr=stderr)
    try:
        proc.stdin.write(payload)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    return proc


def remove_containers(*names: str) -> None:
    """`docker rm -f` the named consumers, bounded, output discarded. Never
    raises: it runs on cleanup paths, where its own failure must not replace
    the error being cleaned up after."""
    try:
        subprocess.run(["docker", "rm", "-f"] + list(names), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def probe(s: RelaySettings, seconds: int = 10, name: str = "homehub-door-probe") -> Dict[str, object]:
    """One authenticated test consumer: what it decoded and in what geometry.

    Killing the docker client (on the deadline, or on Ctrl-C) does NOT stop
    the container: --rm removes it only once ffprobe exits, and against a
    camera that accepts TCP but never answers RTSP that is never. So on any
    way out but a normal return the named container is removed here, or it
    would keep a camera session and refuse every retry under its fixed name.
    A timeout is a result (rc -1, timeout true: probe_verdict fails it);
    anything else is re-raised after the removal.
    """
    argv, payload = relay_consumer(
        s, name, "ffprobe", ["-v", "error"],
        ["-select_streams", "v:0", "-count_packets", "-read_intervals", "%%+%d" % seconds,
         "-show_entries", "stream=codec_name,width,height,nb_read_packets,avg_frame_rate", "-of", "json"])
    deadline = seconds + 60
    try:
        raw = subprocess.run(argv, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=deadline)
    except subprocess.TimeoutExpired as exc:
        remove_containers(name)
        partial = exc.stderr or b""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        return {"rc": -1, "timeout": True, "timeout_sec": deadline, "removed": name,
                "stderr": redact(partial.strip(), s)[-400:]}
    except BaseException:
        remove_containers(name)
        raise
    done = subprocess.CompletedProcess(argv, raw.returncode, raw.stdout.decode("utf-8", "replace"),
                                       raw.stderr.decode("utf-8", "replace"))
    out: Dict[str, object] = {"rc": done.returncode, "stderr": redact(done.stderr.strip(), s)[-400:]}
    try:
        stream = (json.loads(done.stdout).get("streams") or [{}])[0]
        out.update(codec=stream.get("codec_name"), width=stream.get("width"), height=stream.get("height"),
                   packets=int(stream.get("nb_read_packets") or 0), rate=stream.get("avg_frame_rate"))
    except ValueError:
        out["parse_error"] = True
    return out


def cgroup_usage_usec(path: str) -> Optional[int]:
    """CPU time used by a cgroup, in microseconds: cgroup v2 cpu.stat usage_usec
    (the hub), else v1 cpuacct.usage in ns (hybrid hosts such as WSL2)."""
    try:
        with open(os.path.join(path, "cpu.stat"), encoding="ascii") as handle:
            for line in handle:
                if line.startswith("usage_usec "):
                    return int(line.split()[1])
    except OSError:
        pass
    try:
        with open(os.path.join(path, "cpuacct.usage"), encoding="ascii") as handle:
            return int(handle.read().strip()) // 1000
    except (OSError, ValueError):
        return None


def psi_some_avg60(path: str = "/proc/pressure/cpu") -> Optional[float]:
    try:
        with open(path, encoding="ascii") as handle:
            for line in handle:
                if line.startswith("some "):
                    return float(re.search(r"avg60=([0-9.]+)", line).group(1))
    except (OSError, AttributeError, ValueError):
        return None
    return None


def cost(cgroups: Dict[str, str], seconds: int, interval: int = 10) -> Dict[str, object]:
    """Average CPU (percent of ONE core) per cgroup and PSI cpu `some avg60`
    (mean and max) over `seconds` (plan W4b task 11; SR-106 budget).

    Contract:
      Inputs:  cgroups: {label: /sys/fs/cgroup/... directory}; a missing one is
               reported as missing, not as zero.
      Outputs: {label: {cpu_pct_one_core}|{missing}, psi_some_avg60_mean, _max,
               seconds}.
    """
    start = {k: cgroup_usage_usec(v) for k, v in cgroups.items()}
    t0 = time.monotonic()
    psi: List[float] = []
    while time.monotonic() - t0 < seconds:
        value = psi_some_avg60()
        if value is not None:
            psi.append(value)
        time.sleep(min(interval, max(0.0, seconds - (time.monotonic() - t0))))
    elapsed = time.monotonic() - t0
    out: Dict[str, object] = {"seconds": round(elapsed)}
    for label, path in cgroups.items():
        end = cgroup_usage_usec(path)
        if start[label] is None or end is None:
            out[label] = {"missing": path}
        else:
            out[label] = {"cpu_pct_one_core": round(100.0 * (end - start[label]) / 1e6 / elapsed, 2)}
    if psi:
        out["psi_some_avg60_mean"] = round(sum(psi) / len(psi), 2)
        out["psi_some_avg60_max"] = max(psi)
    return out


def container_cgroup(name: str) -> Optional[str]:
    """The cgroup directory accounting a running container's CPU, found from its
    main process's /proc/<pid>/cgroup, so the cgroup driver does not matter:
    the v2 line (0::/system.slice/docker-<id>.scope on the hub) when that
    directory has cpu.stat, else the v1 cpuacct line."""
    done = subprocess.run(["docker", "inspect", "-f", "{{.State.Pid}}", name], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, universal_newlines=True, timeout=20)
    pid = done.stdout.strip()
    if done.returncode != 0 or not pid.isdigit() or pid == "0":
        return None
    try:
        with open("/proc/%s/cgroup" % pid, encoding="ascii") as handle:
            lines = [l.strip().split(":", 2) for l in handle if l.count(":") >= 2]
    except OSError:
        return None
    for _hid, controllers, rel in lines:
        if controllers == "" and os.path.exists("/sys/fs/cgroup%s/cpu.stat" % rel):
            return "/sys/fs/cgroup" + rel
    for _hid, controllers, rel in lines:
        if "cpuacct" in controllers.split(","):
            for mount in ("/sys/fs/cgroup/cpuacct", "/sys/fs/cgroup/cpu,cpuacct"):
                if os.path.exists(mount + rel + "/cpuacct.usage"):
                    return mount + rel
    return None


GREY_DECODER = "homehub-door-costprobe"


def start_grey_decoder(s: RelaySettings, fps: int):
    """Plan W4b task 11: what a hub-side detector would cost - decode the relay
    and keep `fps` small grey frames a second (256x256, the panel motion
    engine's raster), discarding them. Returns the process."""
    argv, payload = relay_consumer(
        s, GREY_DECODER, "ffmpeg", ["-nostdin", "-v", "error"],
        ["-map", "0:v:0", "-an", "-vf", "fps=%d,scale=256:256,format=gray" % fps, "-f", "null", "-"])
    return start_consumer(argv, payload)


def emit(record: Dict[str, object]) -> None:
    print(json.dumps(record, sort_keys=True), flush=True)


def probe_verdict(result: Dict[str, object], s: RelaySettings) -> List[str]:
    """Why a probe result is NOT a readiness pass ([] = it is): the probe must
    exit 0, parse, count packets, and see exactly the expected codec and size.
    Used by `probe` and by the gate, so the two cannot disagree.
    Implements: LLR-1226."""
    reasons = []
    if result.get("timeout"):
        reasons.append("probe timed out after %s s; its container was removed" % result.get("timeout_sec"))
    elif result.get("rc") != 0:
        reasons.append("probe exited %r" % result.get("rc"))
    if result.get("parse_error"):
        reasons.append("probe output did not parse")
    if not isinstance(result.get("packets"), int) or result["packets"] <= 0:
        reasons.append("no packets read")
    if result.get("codec") != s.expect_codec:
        reasons.append("codec %r, expected %r" % (result.get("codec"), s.expect_codec))
    if (result.get("width"), result.get("height")) != (s.expect_width, s.expect_height):
        reasons.append("geometry %sx%s, expected %dx%d" % (result.get("width"), result.get("height"),
                                                          s.expect_width, s.expect_height))
    return reasons


class ProgressReader:
    """A consumer's own account of what it received: ffmpeg `-progress pipe:1`.

    ffmpeg writes a block of key=value lines every -stats_period, ending in
    `progress=continue` (or `=end`). `frame=` in a block is the frames the
    consumer has taken from its input so far (for `-c copy`, the packets it
    wrote); `frames` is that count from the latest COMPLETE block, None until
    one arrives. A reader thread drains the pipe, so ffmpeg can never block on
    it. A consumer that is stopped, wedged or starved writes no new block or
    the same count - which is the point: the relay's own sender counters rise
    when go2rtc ENQUEUES a packet for a consumer, before its socket writer
    runs, so they keep rising for a consumer that reads nothing.
    `advanced_at` is when (on `clock`) the count last ROSE: a consumer that
    took frames early in a window and then stopped has a rising count but a
    stale `advanced_at`.
    Implements: LLR-1226.
    """

    def __init__(self, stream, clock=time.monotonic):
        self.frames = None  # type: Optional[int]
        self.advanced_at = None  # type: Optional[float]
        self.blocks = 0
        self.lines = 0
        self.clock = clock
        self._pending = None  # type: Optional[int]
        self.thread = threading.Thread(target=self._run, args=(stream,), daemon=True)
        self.thread.start()

    def feed(self, line: str) -> None:
        key, sep, value = line.strip().partition("=")
        if not sep:
            return
        if key == "frame" and value.strip().isdigit():
            self._pending = int(value.strip())
        elif key == "progress":
            if self._pending is not None:
                if self.frames is None or self._pending > self.frames:
                    self.advanced_at = self.clock()
                self.frames = self._pending
            self.blocks += 1

    def stalled_for(self) -> Optional[float]:
        """Seconds since the count last rose; None before it ever has."""
        return None if self.advanced_at is None else self.clock() - self.advanced_at

    def _run(self, stream) -> None:
        try:
            for raw in iter(stream.readline, b""):
                self.feed(raw.decode("ascii", "replace"))
                self.lines += 1
        except (OSError, ValueError):
            pass


GATE_CONSUMER = "homehub-door-gate-%d"
# The survivor reports every second (-stats_period 1); at the end of the window
# its count must have risen within this long - one missed block plus jitter.
PROGRESS_FRESH_SEC = 2.5


def gate_consumer(s: RelaySettings, n: int, progress: bool):
    """(argv, payload) of a gate consumer: copy-only to the null muxer, and with
    `progress` its own frame counter on stdout every second."""
    out = ["-map", "0:v:0", "-c", "copy"]
    if progress:
        out += ["-stats_period", "1", "-progress", "pipe:1"]
    return relay_consumer(s, GATE_CONSUMER % n, "ffmpeg", ["-nostdin", "-v", "error"], out + ["-f", "null", "-"])


def gate(s: RelaySettings, seconds: int, progress_sec: int = 5) -> int:
    """The hub half of the compatibility gate (plan W4b task 8). The panel's
    broker must already be suppressed; the relay running. Prints JSON lines and
    returns 0 only when every measured property holds.

    1. readiness: a test consumer with the doorbell's credential reads video:
       the probe exits 0 and counts packets (probe_verdict);
    2. geometry: exactly the codec and size the camera sends
       (DOOR_RELAY_EXPECT_CODEC, DOOR_RELAY_EXPECT_SIZE: h264, 1536x1536);
    3. two simultaneous consumers, exactly one upstream session (ss) and one
       producer (the relay's API);
    4. one consumer ends, the upstream stays, and the other is still RECEIVING
       video, measured AT THAT CONSUMER: its own `-progress` frame count
       (ProgressReader) rises over `progress_sec`, and its last rise is at
       most PROGRESS_FRESH_SEC old when the window ends (one that took a few
       frames and then stopped fails too). A live process is not
       progress, and neither is the relay's per-consumer byte counter
       (consumer_bytes), which rises as go2rtc queues packets whether or not
       the consumer reads them; it is reported, as `relay_sent_bytes`, and
       decides nothing.
    The relay restart, camera dropout and segment decode checks are separate
    commands in the README's gate procedure because they act on units.
    Implements: LLR-1226.
    """
    ok = True
    leaver = survivor = None
    # Every consumer the gate names - gate-1 (the probe) included - is inside
    # the try, so no way out leaves one holding a camera session or its name.
    try:
        first = probe(s, seconds, GATE_CONSUMER % 1)
        reasons = probe_verdict(first, s)
        emit({"check": "readiness+geometry", "pass": not reasons, "reasons": reasons, **first})
        ok &= not reasons
        # gate-2 leaves; gate-3 is the survivor and reports its own progress.
        leaver = start_consumer(*gate_consumer(s, 2, progress=False))
        survivor = start_consumer(*gate_consumer(s, 3, progress=True), stdout=subprocess.PIPE)
        reader = ProgressReader(survivor.stdout)
        time.sleep(max(8, seconds))
        both = {"check": "two-consumers", "upstream_connections": upstream_count(s)}
        try:
            both.update(streams(s))
        except OSError as exc:
            both["api_error"] = str(exc)
        emit(both)
        ok &= both["upstream_connections"] == 1 and both.get("producers") == 1 and both.get("consumers", 0) >= 2
        subprocess.run(["docker", "rm", "-f", GATE_CONSUMER % 2], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
        time.sleep(5)
        frames_before, bytes_before = reader.frames, consumer_bytes(s)
        time.sleep(progress_sec)
        frames_later, stalled_for, bytes_later = reader.frames, reader.stalled_for(), consumer_bytes(s)
        after = {"check": "one-consumer-left", "upstream_connections": upstream_count(s),
                 "survivor_running": survivor.poll() is None,
                 "survivor_frames": [frames_before, frames_later],
                 "survivor_last_frame_age": None if stalled_for is None else round(stalled_for, 1),
                 "consumers": bytes_later["consumers"] if bytes_later else None,
                 "relay_sent_bytes": [b["sent_bytes"] if b else None for b in (bytes_before, bytes_later)],
                 "progress_sec": progress_sec}
        after["survivor_progressing"] = (frames_before is not None and frames_later is not None
                                         and frames_later > frames_before
                                         and stalled_for is not None and stalled_for <= PROGRESS_FRESH_SEC)
        emit(after)
        ok &= after["upstream_connections"] == 1 and after["survivor_running"] and after["survivor_progressing"]
    finally:
        remove_containers(GATE_CONSUMER % 1, GATE_CONSUMER % 2, GATE_CONSUMER % 3)
        for proc in (leaver, survivor):
            if proc is None:
                continue
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
    emit({"check": "gate-hub-half", "pass": bool(ok)})
    return 0 if ok else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--env-file", default=mc.DEFAULT_ENV_FILE)
    sub = p.add_subparsers(dest="verb")
    c = sub.add_parser("condition")
    c.add_argument("knob")
    r = sub.add_parser("render")
    r.add_argument("--out", default=CONFIG_PATH)
    sub.add_parser("import-upstream")
    rr = sub.add_parser("redact-run")
    rr.add_argument("cmd", nargs=argparse.REMAINDER, help="-- CMD ARGS...: the command to run redacted")
    sub.add_parser("streams")
    sub.add_parser("upstream-count")
    pr = sub.add_parser("probe")
    pr.add_argument("--seconds", type=int, default=10)
    g = sub.add_parser("gate")
    g.add_argument("--seconds", type=int, default=10)
    g.add_argument("--progress-seconds", type=int, default=5,
                   help="how long the survivor's own frame count is watched for growth")
    co = sub.add_parser("cost")
    co.add_argument("--seconds", type=int, default=1800)
    co.add_argument("--cgroup", action="append", default=[], help="LABEL=/sys/fs/cgroup/... (repeatable)")
    co.add_argument("--container", action="append", default=[], help="a container name to measure (repeatable)")
    co.add_argument("--decode-grey-fps", type=int, default=0,
                    help="also run a test decoder: the relay at N fps, scaled to 256x256 grey (task 11)")
    args = p.parse_args(argv)
    if args.verb == "condition":
        on = mc.knob_bool(mc.read_knobs(args.env_file), args.knob, False)
        mc.log("door: %s is %s" % (args.knob, "true - starting" if on else "not true - clean skip"))
        return 0 if on else 1
    try:
        if args.verb == "import-upstream":
            keys = import_upstream(args.env_file, sys.stdin.buffer.read(16384))
            mc.log("door: wrote %s into %s (values not shown)" % (" ".join(keys), args.env_file))
            return 0
        s = RelaySettings(args.env_file)
    except Invalid as exc:
        mc.log("door: REFUSED - %s" % exc, warning=True)
        return 2
    if args.verb == "render":
        write_private(args.out, (json.dumps(relay_config(s), indent=1) + "\n").encode("utf-8"))
        # The camera's address is not logged: the journal has no need of it.
        mc.log("door: relay config written to %s (0600): rtsp %s:%d (auth), api 127.0.0.1:%d (auth), "
               "webrtc/srtp/rtmp off, stream /%s from the camera on port %d (video only)"
               % (args.out, s.lan_ip, s.rtsp_port, s.api_port, s.stream, s.port))
        return 0
    if args.verb == "redact-run":
        cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
        if not cmd:
            mc.log("door: redact-run needs -- CMD", warning=True)
            return 2
        return redact_run(s, cmd)
    if args.verb == "streams":
        emit(streams(s))
        return 0
    if args.verb == "upstream-count":
        emit({"upstream_connections": upstream_count(s), "upstream": "%s:%d" % (s.host, s.port)})
        return 0
    if args.verb == "probe":
        result = probe(s, args.seconds)
        reasons = probe_verdict(result, s)
        emit(dict(result, **{"pass": not reasons, "reasons": reasons}))
        return 0 if not reasons else 1
    if args.verb == "gate":
        return gate(s, args.seconds, args.progress_seconds)
    if args.verb == "cost":
        decoder = start_grey_decoder(s, args.decode_grey_fps) if args.decode_grey_fps else None
        try:
            if decoder:
                time.sleep(5)
                args.container.append(GREY_DECODER)
            groups = dict(item.split("=", 1) for item in args.cgroup)
            for name in args.container:
                groups[name] = container_cgroup(name) or "/nonexistent/%s" % name
            result = cost(groups, args.seconds)
            if decoder:
                result["decoder_still_running"] = decoder.poll() is None
            emit(result)
        finally:
            if decoder:
                subprocess.run(["docker", "rm", "-f", GREY_DECODER], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=60)
        return 0
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
