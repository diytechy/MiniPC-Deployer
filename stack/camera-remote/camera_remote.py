#!/usr/bin/env python3
"""camera_remote.py - the camera page's viewer process: config, gate checks, warm-keeping.

The Owner's rulings of 2026-11-12 (HomeHub open-items A89, plan
docs/CAMERAS_REMOTE_VIEW_AND_RECORDING_PLAN_2026-11-11.md 8.1, 8.2 for the door,
8.9): the cameras are watched through a page on the hub at cameras.<domain>,
LAN and tailnet only; Caddy passes only go2rtc's player page, its two scripts
and its stream websocket, in MSE mode (repackaged, never re-encoded); and the
hub makes ONE straightened door picture, the wall panel's own projection,
decoded and straightened on the processor and H.264-encoded on the graphics.
THE PAGE IS BEHIND CADDY'S ONE SHARED LOGIN, because it shows every camera
(about 19:00: "the camera page can be behind a caddy login as it contains all
cameras"; 8.2 answered: all cameras on the page, behind that login): the door,
straightened and raw, and each indoor camera's relay streams (the living room,
the corner: the operator, 2026-11-13), VIDEO ONLY, never their sound. THE PAGE
at / is one file, www/index.html: four tiles on the small streams; a tap shows
one camera full screen on its main stream. THE DOOR ALONE stays open WITHOUT a login to the LAN alone
(CAMERA_VIEW_DOOR_OPEN_CIDR; the earlier ruling, "a public view of the front of
a house on a closed network"), so the wall panel can take it; a tailnet source
always goes through the login. THE PROJECTION is knobs with the panel's values
as defaults (CAMERA_VIEW_FLAT_*; the Owner finds it stretched horizontally).
THE STRAIGHTENED PICTURE'S MODE (about 18:25: "an .env configuration ... start
evaluations with always on"): CAMERA_VIEW_DOOR_FLAT_MODE, `always` (the
default) or `on_demand`.

THE VIEWER is a third go2rtc process (the relays' pinned image), compose
service camera-view, run by homehub-camera-view.service. Its streams are read
from the hub's relays with each relay's own consumer login (no new camera
secret):
    door             the door relay's H.264 copied as it is (the raw circle);
    door_flat        the straightened picture: go2rtc's `exec:` source runs
                     FFmpeg (in the same image), MPEG-TS read from a pipe;
    livingroom       the living-room relay's sub stream, video only, and
    livingroom-main  its main stream, video only - both only while
                     CAMERA_LIVINGROOM_RELAY_ENABLED is true (the main stream
                     only when that relay serves one);
    corner           the corner camera's relay, the same way, while
    corner-main      CAMERA_CORNER_RELAY_ENABLED is true.
go2rtc opens a producer only when a consumer asks. With `on_demand` the
relay session and the straightening FFmpeg exist only while someone watches.
With `always`, homehub-camera-view-warm.service (`keep-warm`) is a consumer of
door_flat on the viewer's own socket that never leaves (and reconnects), so the
FFmpeg runs continuously - about 30 % of one hub core (plan 1.5) - and a page
or the panel gets the picture at once, at its next keyframe. (go2rtc's own
`preload` was not used: a preload whose first dial fails, say while the relay
is still starting, is dropped for good - internal/streams AddPreload.)

NO LISTENER AT ALL. The API (the player page, its scripts and the websocket)
is served on a unix socket, /var/lib/homehub-camera-view/api.sock on the host
(StateDirectory, 0700 root), which Caddy reaches through a read-only bind; no
TCP or UDP port is opened (RTSP, WebRTC, SRTP, RTMP and the API's TCP listener
are all off). go2rtc itself is cut down twice: `app.modules` loads only the
api, ws, rtsp (client), mp4 (MSE) and exec modules, and `api.allow_paths`
registers only the static files and /api/ws, so /api/streams, /api/config and
every other handler that could add or change a stream does not exist.
Measured against the pinned 1.9.14's source (internal/api/api.go,
internal/app/app.go, main.go): see the README.

THE RELAY URL NEVER REACHES AN ARGV. The `door` source is the relay's URL in
the rendered config (0600). door_flat's FFmpeg cannot take its input on stdin
(go2rtc's exec pipe mode owns only stdout), so its input is the door relay's
own ffconcat document (door_relay.relay_input_document - the same builder every
relay consumer uses), written 0600 beside the config and bound read-only into
the container: the argv names only the file's path. Neither file leaves the
unit's RuntimeDirectory (RAM, 0700 root, removed when the unit stops).

Verbs:
    condition      exit 0 only when CAMERA_VIEW_ENABLED is exactly true (the
                   unit's ExecCondition: a clean skip otherwise).
    condition-warm exit 0 only when, as well, the door_flat mode is `always`
                   (the warm unit's ExecCondition).
    keep-warm      hold door_flat open on the viewer's socket for good: the
                   `always` mode's consumer. Reconnects; reads and drops.
    render         preflight (below), then write go2rtc.yaml and door.ffconcat,
                   0600, to /run/homehub-camera-view; refuses on any failure.
    check          preflight plus the live hub: caddy's OWN environment (the
                   values Caddy really serves with) against the .env, the
                   render node, the rendered files' modes, and no credential
                   in any process's argv or environment. Exit 0 only on pass.
    import-login   CAMERA_VIEW_AUTH_HASH from STDIN (one line, a bcrypt hash;
                   never the password) into the stack .env; prints the key name.
                   The login every stream is behind; render refuses without it.
    redact-run -- CMD   the unit's ExecStart wrapper (door_relay.redact_run with
                   every relay's credential and camera address: go2rtc logs a
                   dropped source's whole URL).

PREFLIGHT (render and check): the relays' settings; the door_flat mode; the
projection; the shared login (a bcrypt hash, not the shipped lock: every stream
is behind it); THE OPEN DOOR (CAMERA_VIEW_DOOR_OPEN_CIDR: empty, or IPv4 ranges
inside the hub's LAN that overlap no docker bridge); and THE SOURCE ALLOW-LIST (review round 4,
plan 6.9): every entry of the hub's LAN_CIDR - the `@lan remote_ip` gate in
front of the page - must lie inside the hub's LAN (the network on the
interface holding LAN_IP), Tailscale's 100.64.0.0/10, or a range the Owner
ruled in by name (CAMERA_VIEW_SOURCES_RULED_IN); `private_ranges`, a broad
10/8 or 192.168/16, a docker bridge and IPv6 are refused as shipped.

Python 3.8-compatible (the dev PC's test runner imports this module).
Implements: SR-170..SR-174, LLR-1575..LLR-1586.
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import math
import os
import re
import socket
import stat
import struct
import subprocess
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "monitoring"))
sys.path.insert(0, os.path.join(HERE, "..", "door"))
sys.path.insert(0, os.path.join(HERE, "..", "camera-relay"))
import monitor_common as mc  # noqa: E402
import door_relay  # noqa: E402
import camera_relay  # noqa: E402

Invalid = door_relay.Invalid

RUN_DIR = "/run/homehub-camera-view"          # the unit's RuntimeDirectory (0700, RAM)
CONFIG_NAME = "go2rtc.yaml"
FFCONCAT_NAME = "door.ffconcat"
STATE_DIR = "/var/lib/homehub-camera-view"    # the unit's StateDirectory: the API socket
CONTAINER = "homehub-camera-view"
WARM_UNIT = "homehub-camera-view-warm.service"
CADDY_CONTAINER = "caddy"
# Inside the container (docker-compose.yml service camera-view binds these).
CONFIG_IN_CONTAINER = "/config/go2rtc.yaml"
FFCONCAT_IN_CONTAINER = "/config/door.ffconcat"
SOCKET_IN_CONTAINER = "/run/camera-view/api.sock"
RENDER_NODE = "/dev/dri/renderD128"

# The streams the page may offer. Caddy's websocket gate names exactly these
# (tests/test_camera_remote.py holds the lists equal). A name the config does
# not define creates nothing in go2rtc (it has no scheme), so the gate may name
# an indoor camera's two even while its relay is off.
STREAM_RAW = "door"
STREAM_FLAT = "door_flat"
STREAM_LIVINGROOM = "livingroom"
STREAM_LIVINGROOM_MAIN = "livingroom-main"
STREAM_CORNER = "corner"
STREAM_CORNER_MAIN = "corner-main"
STREAMS = (STREAM_FLAT, STREAM_RAW, STREAM_LIVINGROOM, STREAM_LIVINGROOM_MAIN, STREAM_CORNER, STREAM_CORNER_MAIN)
# The indoor cameras, each on its own relay (stack/camera-relay; the relay's
# camera name, its switch): served, video only, only while the switch is
# exactly true. The page's name for each is the camera's; its main stream is
# "<name>-main" (the full-screen view), served while the relay serves one.
INDOOR = ((STREAM_LIVINGROOM, "CAMERA_LIVINGROOM_RELAY_ENABLED"), (STREAM_CORNER, "CAMERA_CORNER_RELAY_ENABLED"))
# The landing page (stack/camera-remote/www/index.html), served by Caddy behind
# the login: one tile per camera on its small stream, full screen on its main.
PAGE_FILE = os.path.join(HERE, "www", "index.html")
# Every stream is behind the shared login (the Owner, about 19:00); the door's
# two are ALSO open, without a login, to CAMERA_VIEW_DOOR_OPEN_CIDR (the LAN).
LOGIN_STREAMS = STREAMS
DOOR_STREAMS = (STREAM_FLAT, STREAM_RAW)
# What compose hands caddy when CAMERA_VIEW_DOOR_OPEN_CIDR is empty: an
# address no packet comes from, so the open door route matches nobody.
DOOR_OPEN_NOBODY = "0.0.0.0/32"
# The straightened picture's mode (the Owner, 2026-11-12 18:25).
FLAT_MODES = ("always", "on_demand")
DEFAULT_FLAT_MODE = "always"
SOCKET_NAME = "api.sock"

# go2rtc, cut down (pinned 1.9.14: main.go's module table, api.Init's
# allow_paths). `rtsp` is the client for the door source (its server is off by
# listen ""), `mp4` holds the MSE websocket handler, `exec` runs door_flat.
MODULES = ("api", "ws", "rtsp", "mp4", "exec")
API_PATHS = ("/", "/api/ws")   # "/" is the static file server (the player page and scripts)

# The wall panel's projection (OfficeWallNaglight doorstream/service.py
# ffmpeg_args, corrected mode, with wall.env.example's defaults, which
# config.wall.psd1 does not override): fisheye 180 x 180 to a flat 960x540
# view 110 degrees across, v_fov derived for square pixels, linear
# interpolation, 10 frames a second. These are the DEFAULTS of the
# CAMERA_VIEW_FLAT_* knobs (the Owner, 2026-11-12: tune it, A65 Part 1).
FLAT_WIDTH, FLAT_HEIGHT = 960, 540
FLAT_INPUT_FOV = 180
FLAT_HORIZONTAL_FOV = 110
# The output projection: v360's `flat` (the panel's) or `cylindrical` (the
# angle across is linear, so a wide view stretches its edges less; A65 Part 1).
# NOT pannini: FFmpeg's pannini output reads its h_fov as the Pannini d
# parameter (0..1) with a fixed field and no square-pixel control
# (libavfilter/vf_v360.c pannini_to_xyz, 8.0.1).
FLAT_OUTPUTS = ("flat", "cylindrical")
FLAT_FPS = 10
# RATE CONTROL IS THE ENCODER'S DEFAULT, on purpose (the Owner, 2026-11-12
# about 18:45: h264_vaapi at its default quality, no rate cap; measured on the
# real door at evening light: 0.35 Mbit/s, plan 1.5). Two settings differ from
# the encoder's defaults, both for joining a live stream: a keyframe every 2 s
# (h264_vaapi's default is 120 frames, 12 s at 10 fps, and a browser or the
# panel joining starts at the next keyframe) and no B-frames. NOT part of that
# measurement: the shorter keyframe interval raises the bitrate somewhat.
FLAT_GOP = 2 * FLAT_FPS
# rtsp's socket timeout for the relay read, in microseconds: a relay that stops
# sending ends FFmpeg, and go2rtc's producer with it, instead of hanging.
FLAT_INPUT_TIMEOUT_US = 10 * 1000 * 1000

TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
# Caddy 2.11.4's `private_ranges` (internal/ranges.go), which the shipped
# template's LAN_CIDR is: refused as a whole here.
CADDY_PRIVATE_RANGES = ("192.168.0.0/16", "172.16.0.0/12", "10.0.0.0/8", "127.0.0.1/8", "fd00::/8", "::1")
PRIVATE_V4 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
RULED_IN_MAX_V4_PREFIX = 24     # a ruled-in IPv4 range is a /24 or narrower
RULED_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
USER_RE = re.compile(r"[A-Za-z0-9._-]{1,32}")
# A bcrypt hash in modular crypt format, cost 10..31 (Caddy's basic_auth
# compares it; PrepDeploySecrets derives cost 12).
BCRYPT_RE = re.compile(r"\$2[aby]\$(1[0-9]|2[0-9]|3[01])\$[./A-Za-z0-9]{53}")
# What compose hands Caddy when the .env has no hash (docker-compose.yml): the
# base64 of "locked", which no password matches (bcrypt refuses it as a hash,
# so every attempt is a 401). Never accepted as a login.
LOCKED_HASH = "bG9ja2Vk"
DEFAULT_USER = "viewer"
HASH_KEY = "CAMERA_VIEW_AUTH_HASH"
# The keys check reads out of caddy's own environment; nothing else is kept.
DOOR_OPEN_KEY = "CAMERA_VIEW_DOOR_OPEN_CIDR"
CADDY_KEYS = ("LAN_CIDR", "CAMERA_VIEW_ENABLED", "CAMERA_VIEW_AUTH_USER", HASH_KEY, DOOR_OPEN_KEY)


# ── settings ─────────────────────────────────────────────────────────────────


class ViewSettings:
    """The viewer's fields, validated (SR-170, SR-172, SR-173).

    Contract:
      Config:  CAMERA_VIEW_ENABLED (false); CAMERA_VIEW_AUTH_USER (viewer;
               [A-Za-z0-9._-]{1,32}); CAMERA_VIEW_AUTH_HASH (T3, a bcrypt hash
               written by import-login; may be empty here - the preflight
               refuses it); LAN_CIDR (read as Caddy reads it: whitespace-
               separated); CAMERA_VIEW_SOURCES_RULED_IN (empty; NAME=CIDR
               words); and the door relay's fields (door_relay.RelaySettings:
               LAN_IP, DOOR_RELAY_RTSP_PORT, _STREAM, the doorbell's login),
               because the viewer reads the relay as one of its consumers;
               CAMERA_VIEW_DOOR_FLAT_MODE (always | on_demand; empty: always);
               CAMERA_VIEW_DOOR_OPEN_CIDR (the LAN alone, whitespace words;
               empty: the door needs the login everywhere); the projection
               (FlatProjection); and, for each indoor camera (INDOOR) whose
               CAMERA_<C>_RELAY_ENABLED is exactly true, that relay's fields
               (camera_relay.CameraRelaySettings).
               [stack/.env]
      Raises:  Invalid naming the key, never its value.
    Implements: LLR-1575.
    """

    def __init__(self, env_file: str):
        env = mc.read_env_file(env_file)    # what compose (and so Caddy) reads; no monitor override
        raw = door_relay.read_env_raw(env_file)
        self.relay = door_relay.RelaySettings(env_file)
        self.enabled = env.get("CAMERA_VIEW_ENABLED", "") == "true"
        self.user = env.get("CAMERA_VIEW_AUTH_USER", "").strip() or DEFAULT_USER
        if not USER_RE.fullmatch(self.user):
            raise Invalid("CAMERA_VIEW_AUTH_USER must match [A-Za-z0-9._-]{1,32}")
        self.auth_hash = door_relay.env_secret_value(raw.get(HASH_KEY, ""))
        self.lan_cidr = env.get("LAN_CIDR", "").strip()
        self.ruled_in = parse_ruled_in(env.get("CAMERA_VIEW_SOURCES_RULED_IN", ""))
        self.flat_mode = env.get("CAMERA_VIEW_DOOR_FLAT_MODE", "").strip() or DEFAULT_FLAT_MODE
        if self.flat_mode not in FLAT_MODES:
            raise Invalid("CAMERA_VIEW_DOOR_FLAT_MODE must be always or on_demand")
        self.door_open = env.get(DOOR_OPEN_KEY, "").strip()
        self.flat = FlatProjection(env)
        # Each indoor camera's relay settings while its switch is exactly true
        # (the repo's knob convention, as the relay's own condition), else None.
        self.indoor = {name: (camera_relay.CameraRelaySettings(env_file, name) if env.get(switch, "") == "true"
                              else None) for name, switch in INDOOR}
        self.livingroom = self.indoor[STREAM_LIVINGROOM]
        self.corner = self.indoor[STREAM_CORNER]

    def relays(self) -> List[object]:
        """Every relay this viewer reads, for redaction and the credential scan."""
        return [self.relay] + [relay for relay in self.indoor.values() if relay]


def _number(env: Dict[str, str], key: str, default: float, low: float, high: float) -> float:
    text = env.get(key, "").strip()
    if not text:
        return default
    if not re.fullmatch(r"-?[0-9]{1,3}(\.[0-9]{1,3})?", text) or not low <= float(text) <= high:
        raise Invalid("%s must be a number from %g to %g" % (key, low, high))
    return float(text)


class FlatProjection:
    """The straightened picture's v360 settings, the panel's values by default.

    Contract:
      Config:  CAMERA_VIEW_FLAT_INPUT_FOV (180; one value, or HxV such as
               190x180, each 1..360: the fisheye's field across and down);
               CAMERA_VIEW_FLAT_HORIZONTAL_FOV (110; 1..179); CAMERA_VIEW_FLAT_SIZE
               (960x540; even, 160..1920 by 90..1080); CAMERA_VIEW_FLAT_YAW (0;
               -180..180); CAMERA_VIEW_FLAT_PITCH (0; -90..90);
               CAMERA_VIEW_FLAT_OUTPUT (flat; flat | cylindrical). The vertical
               field is always derived for square pixels. [stack/.env]
      Raises:  Invalid naming the key.
    Implements: LLR-1589.
    """

    def __init__(self, env: Optional[Dict[str, str]] = None):
        env = env or {}
        fov = env.get("CAMERA_VIEW_FLAT_INPUT_FOV", "").strip() or str(FLAT_INPUT_FOV)
        parts = fov.split("x")
        if len(parts) not in (1, 2):
            raise Invalid("CAMERA_VIEW_FLAT_INPUT_FOV must be N or HxV")
        try:
            values = [_number({"v": part}, "v", 0, 1, 360) for part in parts] if all(parts) else []
        except Invalid:
            values = []
        if not values:
            raise Invalid("CAMERA_VIEW_FLAT_INPUT_FOV must be N or HxV, each from 1 to 360")
        self.input_h, self.input_v = values[0], values[-1]
        self.h_fov = _number(env, "CAMERA_VIEW_FLAT_HORIZONTAL_FOV", FLAT_HORIZONTAL_FOV, 1, 179)
        size = re.fullmatch(r"([0-9]{3,4})x([0-9]{2,4})", env.get("CAMERA_VIEW_FLAT_SIZE", "").strip()
                            or "%dx%d" % (FLAT_WIDTH, FLAT_HEIGHT))
        if not size or int(size.group(1)) % 2 or int(size.group(2)) % 2 \
                or not 160 <= int(size.group(1)) <= 1920 or not 90 <= int(size.group(2)) <= 1080:
            raise Invalid("CAMERA_VIEW_FLAT_SIZE must be WIDTHxHEIGHT, even, 160..1920 by 90..1080")
        self.width, self.height = int(size.group(1)), int(size.group(2))
        self.yaw = _number(env, "CAMERA_VIEW_FLAT_YAW", 0, -180, 180)
        self.pitch = _number(env, "CAMERA_VIEW_FLAT_PITCH", 0, -90, 90)
        self.output = env.get("CAMERA_VIEW_FLAT_OUTPUT", "").strip() or FLAT_OUTPUTS[0]
        if self.output not in FLAT_OUTPUTS:
            raise Invalid("CAMERA_VIEW_FLAT_OUTPUT must be flat or cylindrical")


def parse_ruled_in(text: str) -> Dict[str, object]:
    """CAMERA_VIEW_SOURCES_RULED_IN -> {name: network}.

    Words `NAME=CIDR` separated by whitespace; NAME [a-z][a-z0-9-]{0,31},
    each name once. Empty (the shipped value) is {}. Raises Invalid naming the
    word's position, never its value. Implements: LLR-1580.
    """
    out: Dict[str, object] = {}
    for i, word in enumerate(text.split(), 1):
        name, sep, cidr = word.partition("=")
        if not sep or not RULED_NAME_RE.fullmatch(name):
            raise Invalid("CAMERA_VIEW_SOURCES_RULED_IN word %d is not NAME=CIDR" % i)
        if name in out:
            raise Invalid("CAMERA_VIEW_SOURCES_RULED_IN names %s twice" % name)
        try:
            out[name] = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise Invalid("CAMERA_VIEW_SOURCES_RULED_IN %s is not a CIDR" % name) from None
    return out


# ── the source allow-list (review round 4) ───────────────────────────────────


def lan_cidr_entries(text: str) -> List[Tuple[str, Optional[object]]]:
    """LAN_CIDR as Caddy's `remote_ip {$LAN_CIDR}` reads it: [(word, network)].

    Whitespace-separated words; `private_ranges` stands for Caddy's own list;
    a bare address is its /32 or /128 (Caddy's CIDRExpressionToPrefix). A word
    Caddy could not parse gives network None. Implements: LLR-1579.
    """
    out: List[Tuple[str, Optional[object]]] = []
    for word in text.split():
        if word == "private_ranges":
            out.extend((word, ipaddress.ip_network(n, strict=False)) for n in CADDY_PRIVATE_RANGES)
            continue
        try:
            out.append((word, ipaddress.ip_network(word, strict=False)))
        except ValueError:
            out.append((word, None))
    return out


def ruled_in_violations(ruled_in: Dict[str, object], bridges: Sequence[object]) -> List[str]:
    """Why a range the Owner ruled in may not be admitted (empty: none).

    An IPv6 range only inside Tailscale's fd7a:115c:a1e0::/48, never wider; an
    IPv4 range only inside 10/8, 172.16/12 or 192.168/16 and a /24 or
    narrower; neither overlapping a docker bridge on this host or loopback.
    Implements: LLR-1580.
    """
    reasons = []
    for name, net in sorted(ruled_in.items()):
        if net.version == 6:
            if not net.subnet_of(TAILNET_V6):
                reasons.append("ruled-in range %s is IPv6 outside Tailscale's fd7a:115c:a1e0::/48" % name)
        elif not any(net.subnet_of(p) for p in PRIVATE_V4) or net.prefixlen < RULED_IN_MAX_V4_PREFIX:
            reasons.append("ruled-in range %s must be a private IPv4 /%d or narrower"
                           % (name, RULED_IN_MAX_V4_PREFIX))
        # Both families (review round 3 F5): docker can give a bridge IPv6 too.
        if any(net.overlaps(b) for b in bridges if b.version == net.version):
            reasons.append("ruled-in range %s overlaps a docker bridge on this hub" % name)
    return reasons


def source_violations(lan_cidr: str, lan_network: Optional[object], ruled_in: Dict[str, object],
                      bridges: Sequence[object]) -> List[str]:
    """Why LAN_CIDR may not gate the camera page (empty: it may). SR-173.

    An ALLOW-LIST, not a deny-list: every resolved entry must lie inside the
    hub's LAN (`lan_network`: the network on the interface holding LAN_IP),
    Tailscale's 100.64.0.0/10, or a range ruled in by name - and none may
    overlap a docker bridge on this host (a container's source would pass the
    gate). Entries are named by position, never by value (the LAN's
    addresses are a household value the journal does not need).
    Implements: LLR-1579.
    """
    reasons = []
    if lan_network is None:
        reasons.append("no interface on this hub holds LAN_IP (or `ip -j addr show` could not be read), "
                       "so the LAN is unknown")
    entries = lan_cidr_entries(lan_cidr)
    if not entries:
        reasons.append("LAN_CIDR is empty")
    if "private_ranges" in lan_cidr.split():
        reasons.append("LAN_CIDR is or holds private_ranges (every ULA, 10/8, 172.16/12 and the hub's "
                       "docker bridges): list the LAN and 100.64.0.0/10 instead")
    allowed = [n for n in (lan_network, TAILNET_V4) if n is not None] + list(ruled_in.values())
    for i, (word, net) in enumerate(entries, 1):
        if word == "private_ranges":
            continue
        if net is None:
            reasons.append("LAN_CIDR entry %d of %d is not an address or CIDR" % (i, len(entries)))
            continue
        if not any(net.version == a.version and net.subnet_of(a) for a in allowed):
            reasons.append("LAN_CIDR entry %d of %d lies outside the hub's LAN, 100.64.0.0/10 and every "
                           "range ruled in by name%s" % (i, len(entries), " (IPv6 is out as shipped)"
                                                         if net.version == 6 else ""))
        elif any(net.overlaps(b) for b in bridges if b.version == net.version):
            reasons.append("LAN_CIDR entry %d of %d overlaps a docker bridge on this hub" % (i, len(entries)))
    return reasons + ruled_in_violations(ruled_in, bridges)


def host_networks(ip_json: str, lan_ip: str) -> Tuple[Optional[object], List[object]]:
    """(the LAN network, [docker bridge networks]) from `ip -j addr show`.

    The LAN is the network of the address equal to LAN_IP; the bridges are
    every network on docker0 and on br-* (compose's networks). Pure: the shell
    calls `ip`. Implements: LLR-1581.
    """
    lan, bridges = None, []
    try:
        links = json.loads(ip_json or "[]")
    except ValueError:
        return None, []
    for link in links if isinstance(links, list) else []:
        name = str(link.get("ifname", ""))
        for info in link.get("addr_info") or []:
            local, plen = info.get("local"), info.get("prefixlen")
            if not local or not isinstance(plen, int):
                continue
            try:
                net = ipaddress.ip_network("%s/%d" % (local, plen), strict=False)
            except ValueError:
                continue
            if local == lan_ip:
                lan = net
            if name == "docker0" or name.startswith("br-"):
                bridges.append(net)
    return lan, bridges


def read_host_networks(lan_ip: str) -> Tuple[Optional[object], List[object]]:
    """host_networks() of this host's `ip -j addr show` (the shell half)."""
    try:
        out = subprocess.run(["ip", "-j", "addr", "show"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None, []
    return host_networks(out, lan_ip)


# What Caddy's remote_ip takes for one IPv4 range: an address or address/prefix,
# no netmask spelling and no leading zeros (Go's netip). Python's ipaddress
# accepts 192.0.2.0/255.255.255.0, which would pass the check and then stop
# Caddy loading every site (review round 5, F1 of the open-door piece).
OCTET = r"(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
IPV4_RANGE_RE = re.compile(r"%s(\.%s){3}(/(3[0-2]|[12]?[0-9]))?" % (OCTET, OCTET))


def door_open_words(door_open: str) -> List[str]:
    """CAMERA_VIEW_DOOR_OPEN_CIDR's words, with compose's nobody default
    (exactly 0.0.0.0/32, alone) read as none: the one reading every caller uses
    (review round 5 F2, F3)."""
    words = door_open.split()
    return [] if words == [DOOR_OPEN_NOBODY] else words


def door_open_violations(door_open: str, lan_network: Optional[object], bridges: Sequence[object]) -> List[str]:
    """Why CAMERA_VIEW_DOOR_OPEN_CIDR may not open the door without a login.

    The LAN ALONE (the Owner: the door is open on the LAN; a tailnet source
    always goes through the login): every word an IPv4 network inside the
    hub's LAN and overlapping no docker bridge; never private_ranges, the
    tailnet or IPv6. Empty is allowed (the door needs the login everywhere).
    Positions, never values. Implements: LLR-1590.
    """
    words = door_open_words(door_open)
    if not words:
        return []
    reasons = []
    if lan_network is None:
        return ["%s is set but no interface on this hub holds LAN_IP" % DOOR_OPEN_KEY]
    for i, word in enumerate(words, 1):
        if not IPV4_RANGE_RE.fullmatch(word):
            if ":" in word:     # IPv6: never the LAN alone
                reasons.append("%s entry %d of %d lies outside the hub's LAN (the door is open to the LAN alone)"
                               % (DOOR_OPEN_KEY, i, len(words)))
            else:
                reasons.append("%s entry %d of %d is not an IPv4 address or CIDR as Caddy reads one"
                               % (DOOR_OPEN_KEY, i, len(words)))
            continue
        net = ipaddress.ip_network(word, strict=False)
        if not net.subnet_of(lan_network):
            reasons.append("%s entry %d of %d lies outside the hub's LAN (the door is open to the LAN alone)"
                           % (DOOR_OPEN_KEY, i, len(words)))
        elif any(net.overlaps(b) for b in bridges if b.version == 4):
            reasons.append("%s entry %d of %d overlaps a docker bridge on this hub" % (DOOR_OPEN_KEY, i, len(words)))
    return reasons


def login_violations(user: str, auth_hash: str) -> List[str]:
    """Why the shared login is not usable (empty: it is). Names keys only.

    Implements: LLR-1582.
    """
    if not auth_hash:
        return ["%s is empty: pipe the hash from the deploy store through import-login" % HASH_KEY]
    if auth_hash == LOCKED_HASH:
        return ["%s is the shipped lock, not a login" % HASH_KEY]
    if not BCRYPT_RE.fullmatch(auth_hash):
        return ["%s is not a bcrypt hash ($2a$/$2b$/$2y$, cost 10..31)" % HASH_KEY]
    return []


def preflight(s: ViewSettings, lan_network, bridges) -> List[str]:
    """Everything render needs before it may write a config.

    The login is always required: every stream is behind it (LOGIN_STREAMS, the
    Owner 2026-11-12, about 19:00). Implements: LLR-1579, LLR-1582, LLR-1590.
    """
    return (login_violations(s.user, s.auth_hash)
            + source_violations(s.lan_cidr, lan_network, s.ruled_in, bridges)
            + door_open_violations(s.door_open, lan_network, bridges))


# ── the go2rtc config and the straightening pipeline ─────────────────────────


def vertical_fov(width: int, height: int, horizontal_fov: float, output: str = "flat") -> float:
    """The output's vertical field of view for square pixels (degrees).

    flat, the wall panel's formula (OfficeWallNaglight doorstream/service.py
    flat_vertical_fov): v360's flat output maps pixels linearly onto
    tan(fov/2), so square pixels need w/h = tan(h_fov/2)/tan(v_fov/2); 77.55
    degrees for 960x540 at 110. cylindrical: the angle across is linear and the
    height is tan(v_fov/2) (vf_v360.c prepare_cylindrical_out), so square pixels
    need w/h = (h_fov in radians / 2)/tan(v_fov/2). Implements: LLR-1577, LLR-1589.
    """
    if output == "cylindrical":
        half = math.radians(horizontal_fov) / 2 * height / width
    else:
        half = math.tan(math.radians(horizontal_fov) / 2) * height / width
    return math.degrees(2 * math.atan(half))


def flat_filter(p: Optional[FlatProjection] = None) -> str:
    """The -vf chain: 10 fps, the v360 projection (the panel's by default), then
    up to the graphics.

    Decoding and straightening stay on the processor (copying full frames
    back from the graphics measured 148 % of a core); only the encode is on
    the graphics (plan 1.5). Implements: LLR-1577, LLR-1589.
    """
    p = p or FlatProjection()
    transform = ("v360=input=fisheye:output=%s:ih_fov=%g:iv_fov=%g:h_fov=%g:v_fov=%.4f:yaw=%g:pitch=%g:"
                 "w=%d:h=%d:interp=linear" % (p.output, p.input_h, p.input_v, p.h_fov,
                                              vertical_fov(p.width, p.height, p.h_fov, p.output),
                                              p.yaw, p.pitch, p.width, p.height))
    return "fps=%d,%s,format=nv12,hwupload" % (FLAT_FPS, transform)


def flat_argv(encoder: str = "vaapi", projection: Optional[FlatProjection] = None) -> List[str]:
    """door_flat's FFmpeg argv: no URL, no credential, no shell.

    Input: the ffconcat document at FFCONCAT_IN_CONTAINER (the relay's URL is
    inside it, never here). Output: MPEG-TS on stdout, which go2rtc's exec
    pipe mode reads (pkg/magic: sync byte 0x47).

    `encoder` is "vaapi" for the hub, always (the ruling). "software" exists
    ONLY for camera-view-lab.sh on a machine without the hub's graphics; no
    knob and no verb reaches it. Implements: LLR-1577.
    """
    head = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error"]
    if encoder == "vaapi":
        hw = ["-init_hw_device", "vaapi=va:%s" % RENDER_NODE, "-filter_hw_device", "va"]
        vf, codec = flat_filter(projection), ["-c:v", "h264_vaapi"]
    elif encoder == "software":
        hw = []
        vf = flat_filter(projection).replace(",format=nv12,hwupload", ",format=yuv420p")
        codec = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency"]
    else:
        raise ValueError("encoder is vaapi or software")
    return (head + hw
            + ["-f", "concat", "-safe", "0", "-protocol_whitelist", "file,tcp,udp,rtp,rtsp",
               "-i", FFCONCAT_IN_CONTAINER, "-map", "0:v:0", "-an", "-vf", vf]
            + codec + ["-g", str(FLAT_GOP), "-bf", "0", "-f", "mpegts", "-"])


def flat_source(encoder: str = "vaapi", projection: Optional[FlatProjection] = None) -> str:
    """The go2rtc `exec:` source for door_flat.

    go2rtc splits it on whitespace (pkg/shell QuoteSplit), so no argument may
    hold a space or a quote; checked, not assumed. Implements: LLR-1577.
    """
    argv = flat_argv(encoder, projection)
    if any(re.search(r"[\s'\"]", a) for a in argv):
        raise Invalid("an FFmpeg argument holds a space or a quote")
    return "exec:" + " ".join(argv)


def view_config(s: ViewSettings, encoder: str = "vaapi") -> Dict[str, object]:
    """go2rtc's whole configuration for the viewer; every listener is named and off.

    Implements: SR-170, LLR-1576.
    """
    return {
        "app": {"modules": list(MODULES)},
        # No TCP listener; the unix socket only. No username: the socket's
        # directory (0700 root) is the control, and go2rtc treats a unix-socket
        # client as local (api.isLoopback "@"). Caddy is the one other reader.
        "api": {"listen": "", "unix_listen": SOCKET_IN_CONTAINER, "allow_paths": list(API_PATHS)},
        "rtsp": {"listen": ""},
        # Not loaded (app.modules); off as well, so a module list ignored by a
        # later image still opens nothing.
        "webrtc": {"listen": ""},
        "srtp": {"listen": ""},
        "rtmp": {"listen": ""},
        "exec": {"allow_paths": ["ffmpeg"]},
        # info: a dropped source is logged with its whole URL, so the output
        # reaches the journal only through redact-run (the relays' rule).
        "log": {"level": "info", "format": "text"},
        "streams": view_streams(s, encoder),
    }


def view_streams(s: ViewSettings, encoder: str = "vaapi") -> Dict[str, str]:
    """The streams: the door's two, and each indoor camera's video while its
    relay is on (its sub stream as <name>, its main as <name>-main when the relay
    serves one, else the sub stream under that name too). Every relay source is `#media=video`: no sound is ever asked for
    (a relay offers its camera's sound only to a consumer that asks).
    Implements: SR-170, LLR-1576.
    """
    streams = {STREAM_RAW: s.relay.consumer_url() + "#media=video",
               STREAM_FLAT: flat_source(encoder, s.flat)}
    for name, _switch in INDOOR:
        relay = s.indoor[name]
        if relay:
            streams[name] = relay.consumer_url() + "#media=video"
            # The page's full view always asks for <name>-main: the main stream
            # where the relay serves one, else the sub stream again (review F1).
            main = relay.for_main() if relay.main_stream else relay
            streams[name + "-main"] = main.consumer_url() + "#media=video"
    return streams


def indoor_note(streams: Dict[str, str]) -> str:
    """The render line's words for the indoor cameras' streams, if any are served."""
    indoor = [name for name in STREAMS if name in streams and name not in DOOR_STREAMS]
    return ", %s video only" % " and ".join(indoor) if indoor else ""


def redact_all(text: str, relays: Sequence[object]) -> str:
    """door_relay.redact for every relay's login and camera address in turn."""
    for relay in relays:
        text = door_relay.redact(text, relay)
    return text


def ffconcat_document(s: ViewSettings) -> bytes:
    """door_flat's input: the door relay's own consumer document (one builder for
    every relay consumer), with a socket timeout. Implements: LLR-1578."""
    return door_relay.relay_input_document(s.relay, FLAT_INPUT_TIMEOUT_US)


def render(s: ViewSettings, out_dir: str, encoder: str = "vaapi") -> List[str]:
    """Write go2rtc.yaml and door.ffconcat, each 0600, into out_dir (made 0700).

    Returns the paths written. Implements: LLR-1578.
    """
    # Both built before either is written (review round 3 F4), and an existing
    # directory tightened too (F3: makedirs leaves its mode alone).
    config_bytes = (json.dumps(view_config(s, encoder), indent=1) + "\n").encode("utf-8")
    document_bytes = ffconcat_document(s)
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    if hasattr(os, "fchmod"):   # POSIX; the dev PC's Windows test runner has no modes
        os.chmod(out_dir, 0o700)
    config = os.path.join(out_dir, CONFIG_NAME)
    document = os.path.join(out_dir, FFCONCAT_NAME)
    door_relay.write_private(config, config_bytes)
    door_relay.write_private(document, document_bytes)
    return [config, document]


# ── import-login ─────────────────────────────────────────────────────────────


def import_login(env_file: str, blob: bytes) -> str:
    """Write CAMERA_VIEW_AUTH_HASH from STDIN into the stack .env (SR-172).

    One line: the bcrypt hash HomeHub's deploy store derived from the shared
    password (CameraViewHash). The password itself never reaches the hub. The
    hash is encoded as the materialiser encodes ($ doubled for compose) and
    never printed. Returns the key name. Implements: LLR-1582.
    """
    if len(blob) > 256:
        raise Invalid("the hash on stdin is longer than 256 bytes")
    text = blob.decode("ascii").rstrip("\r\n")
    if not BCRYPT_RE.fullmatch(text):
        raise Invalid("the line on stdin is not one bcrypt hash ($2a$/$2b$/$2y$, cost 10..31)")
    door_relay.write_env_keys(env_file, {HASH_KEY: door_relay.env_encode(text)})
    return HASH_KEY


# ── check: the live hub ──────────────────────────────────────────────────────


def caddy_env_from_inspect(inspect_json: str) -> Dict[str, str]:
    """The CADDY_KEYS out of `docker inspect` JSON of the caddy container.

    Every other variable (the dns-01 token, the game secret) is dropped here
    and never kept. Implements: LLR-1583.
    """
    try:
        data = json.loads(inspect_json or "[]")
        env = (data[0] if isinstance(data, list) else data).get("Config", {}).get("Env") or []
    except (ValueError, IndexError, AttributeError):
        return {}
    out = {}
    for item in env:
        key, sep, value = str(item).partition("=")
        if sep and key in CADDY_KEYS:
            out[key] = value
    return out


def caddy_violations(s: ViewSettings, caddy: Dict[str, str], lan_network, bridges) -> List[str]:
    """Caddy serves with the environment it was CREATED with; compare it to the .env.

    The gate that decides is the one in caddy's environment (the oracle), so
    its LAN_CIDR is put through the same allow-list, and the switch and the
    login must equal the .env's (otherwise caddy needs recreating). Values are
    compared, never printed. Implements: LLR-1583.
    """
    if not caddy:
        return ["caddy's environment could not be read (is the caddy container running?)"]
    reasons = []
    if (caddy.get("CAMERA_VIEW_ENABLED") == "true") != s.enabled:
        reasons.append("caddy's CAMERA_VIEW_ENABLED differs from the .env: recreate caddy")
    # As sets (review round 2 F5): the order of the words decides nothing, and
    # caddy's own value goes through the allow-list below either way.
    if set(caddy.get("LAN_CIDR", "").split()) != set(s.lan_cidr.split()):
        reasons.append("caddy's LAN_CIDR differs from the .env: recreate caddy")
    if caddy.get("CAMERA_VIEW_AUTH_USER") != s.user:
        reasons.append("caddy's CAMERA_VIEW_AUTH_USER differs from the .env: recreate caddy")
    if caddy.get(HASH_KEY) != s.auth_hash:
        reasons.append("caddy's %s differs from the .env: recreate caddy" % HASH_KEY)
    reasons += ["caddy's own " + r for r in login_violations(caddy.get("CAMERA_VIEW_AUTH_USER", ""),
                                                            caddy.get(HASH_KEY, ""))]
    reasons += ["caddy's own " + r for r in source_violations(caddy.get("LAN_CIDR", ""), lan_network,
                                                             s.ruled_in, bridges)]
    caddy_open = caddy.get(DOOR_OPEN_KEY, "")
    if set(door_open_words(caddy_open)) != set(door_open_words(s.door_open)):
        reasons.append("caddy's %s differs from the .env: recreate caddy" % DOOR_OPEN_KEY)
    reasons += ["caddy's own " + r for r in door_open_violations(caddy_open, lan_network, bridges)]
    return reasons


def read_caddy_env() -> Dict[str, str]:
    """caddy_env_from_inspect() of the running caddy container (the shell half)."""
    try:
        out = subprocess.run(["docker", "inspect", CADDY_CONTAINER], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return caddy_env_from_inspect(out)


def file_mode_violations(run_dir: str = RUN_DIR) -> List[str]:
    """The rendered files are 0600 and their directory 0700, when they exist.

    Implements: LLR-1584.
    """
    reasons = []
    try:
        if stat.S_IMODE(os.stat(run_dir).st_mode) != 0o700:
            reasons.append("%s is not 0700" % run_dir)
    except FileNotFoundError:
        return []
    for name in (CONFIG_NAME, FFCONCAT_NAME):
        path = os.path.join(run_dir, name)
        try:
            if stat.S_IMODE(os.stat(path).st_mode) != 0o600:
                reasons.append("%s is not 0600" % path)
        except FileNotFoundError:
            pass
    return reasons


def credential_in_processes(forms: Sequence[str], proc_root: str = "/proc") -> List[str]:
    """Pids whose argv or environment holds a credential spelling (empty: none).

    `forms` are door_relay.secret_forms() of the relay login. Reports the pid
    and which file, never the match. Implements: LLR-1584.
    """
    hits = []
    wanted = [f.encode("utf-8") for f in forms if f]
    try:
        entries = sorted(os.listdir(proc_root))
    except OSError:   # review round 2 F3: a named reason, never a traceback
        return ["%s could not be listed" % proc_root]
    for entry in entries:
        if not entry.isdigit() or str(os.getpid()) == entry:
            continue
        for leaf in ("cmdline", "environ"):
            try:
                with open(os.path.join(proc_root, entry, leaf), "rb") as handle:
                    data = handle.read()
            except OSError:
                continue
            if any(w in data for w in wanted):
                hits.append("pid %s %s" % (entry, leaf))
    return hits


def warm_unit_enabled() -> Optional[bool]:
    """Whether homehub-camera-view-warm.service is enabled (None: systemctl could not say)."""
    try:
        out = subprocess.run(["systemctl", "is-enabled", WARM_UNIT], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out in ("enabled", "enabled-runtime") if out else None


def check(s: ViewSettings, lan_network, bridges, caddy: Dict[str, str],
          warm_enabled: Optional[bool] = None) -> Dict[str, object]:
    """The whole check, as a record: {pass, reasons}.

    `warm_enabled` is whether the warm unit is enabled (None: unknown, not
    judged): with the viewer on in `always`, a warm unit nobody enabled means
    the picture starts only at the first viewer (review round 3 F6).
    Implements: LLR-1583, LLR-1584.
    """
    reasons = preflight(s, lan_network, bridges) + caddy_violations(s, caddy, lan_network, bridges)
    if s.enabled and s.flat_mode == "always" and warm_enabled is False:
        reasons.append("CAMERA_VIEW_DOOR_FLAT_MODE is always but %s is not enabled: the straightened picture "
                       "would start only at the first viewer" % WARM_UNIT)
    try:
        if not stat.S_ISCHR(os.stat(RENDER_NODE).st_mode):
            reasons.append("%s is not a character device" % RENDER_NODE)
    except OSError:
        reasons.append("%s is missing: the straightened picture cannot be encoded" % RENDER_NODE)
    reasons += file_mode_violations()
    forms = [f for relay in s.relays() for f in door_relay.secret_forms(relay)]
    reasons += ["a relay credential is in %s" % hit for hit in credential_in_processes(forms)]
    return {"pass": not reasons, "reasons": reasons, "enabled": s.enabled, "door_flat_mode": s.flat_mode,
            "streams": sorted(view_streams(s)), "door_open_to_the_lan": bool(door_open_words(s.door_open))}


# ── keep-warm: the `always` mode's consumer ──────────────────────────────────
#
# A websocket client of go2rtc's own /api/ws on the viewer's unix socket, the
# path Caddy proxies, asking for door_flat in MSE exactly as the page does. It
# reads and drops; its only job is to be a consumer that never leaves, so the
# straightening FFmpeg never stops. Standard library only (the hub's python3).

WARM_SILENCE_SEC = 30.0              # no video byte this long: reconnect
WARM_BACKOFF_SEC = (2, 5, 10, 30, 60)
WARM_HEALTHY_SEC = 60.0              # a session this long resets the backoff
WARM_MAX_FRAME = 8 * 1024 * 1024     # a larger frame is not go2rtc's: give up the session


def ws_upgrade_request(path: str, key: str) -> bytes:
    """The HTTP/1.1 upgrade for go2rtc's /api/ws. No Origin header: go2rtc's
    same-origin check admits a request without one. Implements: LLR-1587."""
    return ("GET %s HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: %s\r\n\r\n" % (path, key)).encode("ascii")


def ws_client_frame(payload: bytes, mask: bytes, opcode: int = 1) -> bytes:
    """One final, masked client frame (RFC 6455 5.2). Implements: LLR-1587."""
    n = len(payload)
    if n < 126:
        head = bytes([0x80 | opcode, 0x80 | n])
    elif n < 65536:
        head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", n)
    else:
        head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", n)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class FrameReader:
    """The server's side of the conversation, from `recv(n)` (b"" at EOF).

    head() reads the upgrade's status line and headers; frame() one frame as
    (opcode, payload). EOFError at the end; ValueError on a frame no go2rtc
    sends (over WARM_MAX_FRAME). Implements: LLR-1587.
    """

    def __init__(self, recv):
        self.recv = recv
        self.buf = b""

    def _fill(self, n: int) -> None:
        while len(self.buf) < n:
            chunk = self.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk

    def head(self) -> int:
        while b"\r\n\r\n" not in self.buf:
            if len(self.buf) > 16384:
                raise ValueError("the upgrade answer's head is over 16 KiB")
            chunk = self.recv(4096)
            if not chunk:
                raise EOFError
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        parts = head.split(b"\r\n", 1)[0].split()
        return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    def frame(self) -> Tuple[int, bytes]:
        # Nothing is consumed until the whole frame is buffered: a socket
        # timeout part-way through leaves the buffer as it was, so the next
        # call starts at the same header instead of reading payload as one.
        self._fill(2)
        b0, b1 = self.buf[0], self.buf[1]
        n, head = b1 & 0x7F, 2
        if n == 126:
            self._fill(4)
            n, head = struct.unpack(">H", self.buf[2:4])[0], 4
        elif n == 127:
            self._fill(10)
            n, head = struct.unpack(">Q", self.buf[2:10])[0], 10
        if n > WARM_MAX_FRAME:
            raise ValueError("a %d-byte frame" % n)
        masked = 4 if b1 & 0x80 else 0
        self._fill(head + masked + n)
        mask = self.buf[head:head + masked]
        data = self.buf[head + masked:head + masked + n]
        self.buf = self.buf[head + masked + n:]
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return b0 & 0x0F, data


def warm_session(sock, stream: str, clock, on_flowing) -> str:
    """One connection: upgrade, ask for MSE, read until something ends it.

    Returns why it ended: "refused <status>", "no answer", "error <go2rtc's
    text>", "closed", "silent" (no video for WARM_SILENCE_SEC) or "eof". Calls
    on_flowing() once, at the session's first video bytes. Implements: LLR-1587.
    """
    sock.sendall(ws_upgrade_request("/api/ws?src=%s" % stream, base64.b64encode(os.urandom(16)).decode()))
    reader = FrameReader(sock.recv)
    try:
        status = reader.head()
    except EOFError:      # the viewer closed before answering (stopping, or not ready)
        return "eof"
    except socket.timeout:   # accepted but never answered (review round 3, piece d, F1)
        return "no answer"
    if status != 101:
        return "refused %d" % status
    sock.sendall(ws_client_frame(json.dumps({"type": "mse", "value": ""}).encode("utf-8"), os.urandom(4)))
    last_video, flowing = clock(), False
    while True:
        try:
            opcode, data = reader.frame()
        except socket.timeout:
            opcode, data = -1, b""
        except EOFError:
            return "eof"
        if opcode == 2 and data:
            last_video = clock()
            if not flowing:
                flowing = True
                on_flowing()
        elif opcode == 1 and b'"error"' in data:
            return "error " + data.decode("utf-8", "replace")[:300]
        elif opcode == 8:
            return "closed"
        if clock() - last_video > WARM_SILENCE_SEC:
            return "silent"


def unix_connect(path: str):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # pylint: disable=no-member
    sock.settimeout(5.0)
    try:
        sock.connect(path)
    except BaseException:
        sock.close()
        raise
    return sock


def keep_warm(socket_path: str, stream: str, log, redact, connect=unix_connect, sleep=time.sleep,
              clock=time.monotonic, rounds: Optional[int] = None) -> None:
    """Hold `stream` open on the viewer's socket, reconnecting for good (SR-174).

    Backoff WARM_BACKOFF_SEC after each ended session, reset by one that ran
    WARM_HEALTHY_SEC. Logs each end, redacted (go2rtc's error text can carry
    the relay URL with its userinfo already starred). `rounds` bounds the loop
    for tests. Implements: LLR-1587.
    """
    failures = 0
    n = 0
    while rounds is None or n < rounds:
        n += 1
        started = clock()
        try:
            sock = connect(socket_path)
        except OSError as exc:
            why = "no socket (%s)" % type(exc).__name__
        else:
            try:
                why = warm_session(sock, stream, clock,
                                   lambda: log("camera-view warm: %s is flowing" % stream))
            except (OSError, ValueError, EOFError) as exc:
                why = "dropped (%s)" % type(exc).__name__
            finally:
                sock.close()
        failures = 0 if clock() - started >= WARM_HEALTHY_SEC else failures + 1
        wait = WARM_BACKOFF_SEC[min(max(failures - 1, 0), len(WARM_BACKOFF_SEC) - 1)]
        log("camera-view warm: %s ended: %s; again in %d s" % (stream, redact(why), wait))
        sleep(wait)


# ── main ─────────────────────────────────────────────────────────────────────


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--env-file", default=mc.DEFAULT_ENV_FILE)
    sub = p.add_subparsers(dest="verb")
    sub.add_parser("condition")
    sub.add_parser("condition-warm")
    sub.add_parser("keep-warm")
    r = sub.add_parser("render")
    r.add_argument("--out-dir", default=RUN_DIR)
    sub.add_parser("check")
    sub.add_parser("import-login")
    rr = sub.add_parser("redact-run")
    rr.add_argument("cmd", nargs=argparse.REMAINDER)
    args = p.parse_args(argv)
    tag = "camera-view"
    if args.verb == "condition":
        on = mc.read_env_file(args.env_file).get("CAMERA_VIEW_ENABLED", "") == "true"
        mc.log("%s: CAMERA_VIEW_ENABLED is %s" % (tag, "true - starting" if on else "not true - clean skip"))
        return 0 if on else 1
    if args.verb == "import-login":
        try:
            key = import_login(args.env_file, sys.stdin.buffer.read(4096))
        except (Invalid, UnicodeDecodeError, OSError) as exc:
            mc.log("%s: REFUSED - %s" % (tag, exc if isinstance(exc, Invalid) else type(exc).__name__),
                   warning=True)
            return 2
        mc.log("%s: wrote %s into %s (value not shown); recreate caddy to serve it" % (tag, key, args.env_file))
        return 0
    if args.verb not in ("render", "check", "redact-run", "condition-warm", "keep-warm"):
        p.print_help()
        return 2
    try:
        s = ViewSettings(args.env_file)
    except Invalid as exc:
        mc.log("%s: REFUSED - %s" % (tag, exc), warning=True)
        return 2
    if args.verb == "condition-warm":
        on = s.enabled and s.flat_mode == "always"
        mc.log("%s warm: CAMERA_VIEW_ENABLED %s, CAMERA_VIEW_DOOR_FLAT_MODE %s - %s"
               % (tag, "true" if s.enabled else "not true", s.flat_mode, "holding door_flat" if on else "clean skip"))
        return 0 if on else 1
    if args.verb == "keep-warm":
        if not (s.enabled and s.flat_mode == "always"):   # checked here too: a hand start honours the knobs
            mc.log("%s warm: REFUSED - the viewer is off or the mode is not always" % tag, warning=True)
            return 78
        keep_warm(STATE_DIR + "/" + SOCKET_NAME, STREAM_FLAT, mc.log,
                  lambda text: redact_all(text, s.relays()))
        return 0
    if args.verb == "redact-run":
        cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
        if not cmd:
            mc.log("%s: redact-run needs -- CMD" % tag, warning=True)
            return 2
        return door_relay.redact_run(s.relay, cmd, redactor=lambda text: redact_all(text, s.relays()))
    lan_network, bridges = read_host_networks(s.relay.lan_ip)
    if args.verb == "render":
        reasons = preflight(s, lan_network, bridges)
        if reasons:
            for reason in reasons:
                mc.log("%s: REFUSED - %s" % (tag, reason), warning=True)
            return 2
        written = render(s, args.out_dir)
        mc.log("%s: %s written (0600): no listener (api on the unix socket only, allow_paths %s; rtsp, "
               "webrtc, srtp, rtmp off), modules %s, streams %s (the relay's copy) and %s (straightened "
               "%dx%d at %d fps, h264_vaapi; mode %s)%s, each read from its relay while it has a consumer"
               % (tag, " and ".join(written), "/".join(API_PATHS), ",".join(MODULES), STREAM_RAW, STREAM_FLAT,
                  s.flat.width, s.flat.height, FLAT_FPS, s.flat_mode,
                  indoor_note(view_streams(s))))
        return 0
    record = check(s, lan_network, bridges, read_caddy_env(), warm_unit_enabled())
    door_relay.emit(record)
    return 0 if record["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
