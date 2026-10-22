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

Python 3.8-compatible (the dev PC's test runner imports this module).
Implements: SR-105, SR-106, LLR-1223..LLR-1229.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
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
               _PASSWORD (required; from the deploy store).  [stack/.env]
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


def redact(text: str) -> str:
    """Credentials in any rtsp:// URL become ***; applied to every relayed line."""
    return SECRET_URL_RE.sub(r"\1***@", text)


def streams(s: RelaySettings) -> Dict[str, object]:
    """The relay API's view of our stream: producer and consumer counts."""
    req = urllib.request.Request("http://127.0.0.1:%d/api/streams" % s.api_port)
    token = base64.b64encode(("%s:%s" % (s.username, s.password)).encode()).decode()
    req.add_header("Authorization", "Basic " + token)
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    entry = data.get(s.stream) or {}
    producers = entry.get("producers") or []
    consumers = entry.get("consumers") or []
    return {"stream": s.stream, "producers": len(producers), "consumers": len(consumers),
            "producer_states": [redact(json.dumps({k: v for k, v in p.items() if k in ("url", "remote_addr", "state")}))
                                for p in producers]}


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


def docker_ffmpeg(args: Sequence[str], env_url: str, seconds: float, name: str) -> subprocess.CompletedProcess:
    """Run ffmpeg/ffprobe from the pinned image with the source URL in an env
    file (0600, removed after) - never in argv, never printed."""
    fd, envfile = tempfile.mkstemp(prefix="door-probe-", suffix=".env")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("DOOR_URL=%s\n" % env_url)
        script = " ".join(args)
        argv = ["docker", "run", "--rm", "--name", name, "--network", "host", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--log-driver", "none",
                "--env-file", envfile, "--entrypoint", "/bin/sh", image_ref(), "-c", script]
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=seconds + 60)
    finally:
        os.unlink(envfile)


def probe(s: RelaySettings, seconds: int = 10, name: str = "homehub-door-probe") -> Dict[str, object]:
    """One authenticated test consumer: what it decoded and in what geometry."""
    done = docker_ffmpeg(
        ['exec ffprobe -v error -rtsp_transport tcp -select_streams v:0 -count_packets',
         '-read_intervals "%%+%d" -show_entries stream=codec_name,width,height,nb_read_packets,avg_frame_rate'
         % seconds, '-of json "$DOOR_URL"'], s.consumer_url(), seconds, name)
    out: Dict[str, object] = {"rc": done.returncode, "stderr": redact(done.stderr.strip())[-400:]}
    try:
        stream = (json.loads(done.stdout).get("streams") or [{}])[0]
        out.update(codec=stream.get("codec_name"), width=stream.get("width"), height=stream.get("height"),
                   packets=int(stream.get("nb_read_packets") or 0), rate=stream.get("avg_frame_rate"))
    except ValueError:
        out["parse_error"] = True
    return out


def cgroup_usage_usec(path: str) -> Optional[int]:
    try:
        with open(os.path.join(path, "cpu.stat"), encoding="ascii") as handle:
            for line in handle:
                if line.startswith("usage_usec "):
                    return int(line.split()[1])
    except OSError:
        return None
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
    """The cgroup v2 directory of a running container, from docker inspect."""
    done = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", name], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, universal_newlines=True, timeout=20)
    cid = done.stdout.strip()
    if done.returncode != 0 or not cid:
        return None
    for candidate in ("/sys/fs/cgroup/system.slice/docker-%s.scope" % cid,
                      "/sys/fs/cgroup/docker/%s" % cid):
        if os.path.isdir(candidate):
            return candidate
    return None


def emit(record: Dict[str, object]) -> None:
    print(json.dumps(record, sort_keys=True), flush=True)


def gate(s: RelaySettings, seconds: int) -> int:
    """The hub half of the compatibility gate (plan W4b task 8). The panel's
    broker must already be suppressed; the relay running. Prints JSON lines and
    returns 0 only when every measured property holds.

    1. readiness: a test consumer with the doorbell's credential decodes video;
    2. geometry: the codec and size the camera sends (1536x1536 expected);
    3. two simultaneous consumers, exactly one upstream session (ss) and one
       producer (the relay's API);
    4. one consumer ends, the other keeps receiving and the upstream stays.
    The relay restart, camera dropout and segment decode checks are separate
    commands in the README's gate procedure because they act on units.
    """
    ok = True
    first = probe(s, seconds, "homehub-door-gate-1")
    emit({"check": "readiness+geometry", **first})
    ok &= first.get("packets", 0) > 0
    procs = []
    for n in (2, 3):
        fd, envfile = tempfile.mkstemp(prefix="door-gate-", suffix=".env")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write("DOOR_URL=%s\n" % s.consumer_url())
        argv = ["docker", "run", "--rm", "--name", "homehub-door-gate-%d" % n, "--network", "host",
                "--read-only", "--cap-drop", "ALL", "--log-driver", "none", "--env-file", envfile,
                "--entrypoint", "/bin/sh", image_ref(), "-c",
                'exec ffmpeg -nostdin -v error -rtsp_transport tcp -i "$DOOR_URL" -map 0:v:0 -c copy -f null -']
        procs.append((subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE), envfile))
    time.sleep(max(8, seconds))
    both = {"check": "two-consumers", "upstream_connections": upstream_count(s)}
    try:
        both.update(streams(s))
    except OSError as exc:
        both["api_error"] = str(exc)
    emit(both)
    ok &= both["upstream_connections"] == 1 and both.get("producers") == 1 and both.get("consumers", 0) >= 2
    subprocess.run(["docker", "rm", "-f", "homehub-door-gate-2"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=30)
    time.sleep(5)
    after = {"check": "one-consumer-left", "upstream_connections": upstream_count(s),
             "survivor_running": procs[1][0].poll() is None}
    emit(after)
    ok &= after["upstream_connections"] == 1 and after["survivor_running"]
    for proc, envfile in procs:
        subprocess.run(["docker", "rm", "-f", "homehub-door-gate-2", "homehub-door-gate-3"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.unlink(envfile)
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
    sub.add_parser("streams")
    sub.add_parser("upstream-count")
    pr = sub.add_parser("probe")
    pr.add_argument("--seconds", type=int, default=10)
    g = sub.add_parser("gate")
    g.add_argument("--seconds", type=int, default=10)
    co = sub.add_parser("cost")
    co.add_argument("--seconds", type=int, default=1800)
    co.add_argument("--cgroup", action="append", default=[], help="LABEL=/sys/fs/cgroup/... (repeatable)")
    co.add_argument("--container", action="append", default=[], help="a container name to measure (repeatable)")
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
        mc.log("door: relay config written to %s (0600): rtsp %s:%d (auth), api 127.0.0.1:%d (auth), "
               "webrtc/srtp/rtmp off, stream /%s from %s:%d (video only)"
               % (args.out, s.lan_ip, s.rtsp_port, s.api_port, s.stream, s.host, s.port))
        return 0
    if args.verb == "streams":
        emit(streams(s))
        return 0
    if args.verb == "upstream-count":
        emit({"upstream_connections": upstream_count(s), "upstream": "%s:%d" % (s.host, s.port)})
        return 0
    if args.verb == "probe":
        result = probe(s, args.seconds)
        emit(result)
        return 0 if result.get("packets", 0) > 0 else 1
    if args.verb == "gate":
        return gate(s, args.seconds)
    if args.verb == "cost":
        groups = dict(item.split("=", 1) for item in args.cgroup)
        for name in args.container:
            groups[name] = container_cgroup(name) or "/nonexistent/%s" % name
        emit(cost(groups, args.seconds))
        return 0
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
