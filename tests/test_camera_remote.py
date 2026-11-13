"""The camera page's viewer (SN-057, SR-170..SR-174): stack/camera-remote.

Rulings of 2026-11-12 under test: every camera behind Caddy's shared login;
the door alone open, with no login, to the LAN alone (CAMERA_VIEW_DOOR_OPEN_CIDR);
the living room's relay streams, video only; the projection as knobs.

What runs here, on the dev PC: the settings and their refusals (keys, never
values); the source allow-list over LAN_CIDR (review round 4); the rendered
go2rtc config (no listener, five modules, two API paths, two streams); the
straightened picture's FFmpeg argv (the panel's projection, the graphics
encode at the encoder's default rate, no URL); the shared login (kept for
streams other than the door's; none ships); `check`'s reading of caddy's own
environment; the warm consumer's websocket client against fake sockets; and
the Caddyfile / compose / unit / template contract.

What does not: the real go2rtc and Caddy. That is
stack/camera-remote/tests/camera-view-lab.sh (root + docker; TC-1839..TC-1843),
and the hub itself is the Owner's trial (TC-1844).
Implements: TC-1825..TC-1838.
"""

from __future__ import annotations

import ipaddress
import io
import json
import math
import os
import re
import stat
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
for sub in ("camera-remote", "door", "monitoring"):
    sys.path.insert(0, str(STACK / sub))
import camera_remote as cr  # noqa: E402
import door_relay as dr  # noqa: E402

PASSWORD = "c@m p$ss:1"          # the relay's (the doorbell's) login, as in the door tests
LAN = ipaddress.ip_network("192.0.2.0/24")
BRIDGES = [ipaddress.ip_network("172.17.0.0/16"), ipaddress.ip_network("172.28.92.0/24")]
HASH = "$2b$12$" + "a" * 53


def write_env(tmp_path, **over):
    lines = {
        "LAN_IP": "192.0.2.10",
        "LAN_CIDR": "192.0.2.0/24 100.64.0.0/10",
        "DOOR_RELAY_UPSTREAM_HOST": "192.0.2.20",
        "DOOR_RELAY_UPSTREAM_PASSWORD": '"c@m p$$ss:1"',
        "CAMERA_VIEW_ENABLED": "true",
        "CAMERA_VIEW_AUTH_HASH": HASH.replace("$", "$$"),
    }
    lines.update(over)
    path = tmp_path / "env"
    path.write_text("".join("%s=%s\n" % kv for kv in lines.items() if kv[1] is not None), encoding="utf-8")
    return str(path)


@pytest.fixture(autouse=True)
def no_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))


def settings(tmp_path, **over):
    return cr.ViewSettings(write_env(tmp_path, **over))


# ── TC-1825 settings and refusals ────────────────────────────────────────────


def test_tc1825_defaults_need_no_login_and_mode_is_always(tmp_path):
    s = settings(tmp_path)
    assert (s.enabled, s.user, s.auth_hash, s.flat_mode, s.ruled_in) == (True, "viewer", HASH, "always", {})
    assert (s.door_open, s.livingroom) == ("", None)
    assert s.relay.password == PASSWORD and s.relay.consumer_url().startswith("rtsp://admin:c%40m%20p%24ss%3A1@")


@pytest.mark.parametrize("over, key", [
    ({"CAMERA_VIEW_AUTH_USER": "a b"}, "CAMERA_VIEW_AUTH_USER"),
    ({"CAMERA_VIEW_DOOR_FLAT_MODE": "Always"}, "CAMERA_VIEW_DOOR_FLAT_MODE"),
    ({"CAMERA_VIEW_DOOR_FLAT_MODE": "on-demand"}, "CAMERA_VIEW_DOOR_FLAT_MODE"),
    ({"CAMERA_VIEW_SOURCES_RULED_IN": "10.13.13.0/24"}, "CAMERA_VIEW_SOURCES_RULED_IN"),
    ({"CAMERA_VIEW_SOURCES_RULED_IN": "wg=10.13.13.0/24 wg=10.14.0.0/24"}, "CAMERA_VIEW_SOURCES_RULED_IN"),
    ({"CAMERA_VIEW_SOURCES_RULED_IN": "Wg=10.13.13.0/24"}, "CAMERA_VIEW_SOURCES_RULED_IN"),
    ({"CAMERA_VIEW_SOURCES_RULED_IN": "wg=10.13.13.300/24"}, "CAMERA_VIEW_SOURCES_RULED_IN"),
    ({"DOOR_RELAY_UPSTREAM_PASSWORD": None}, "DOOR_RELAY_UPSTREAM_PASSWORD"),
    ({"LAN_IP": "hub.lan"}, "LAN_IP"),
])
def test_tc1825_refusals_name_the_key_never_the_value(tmp_path, over, key):
    with pytest.raises(cr.Invalid) as exc:
        settings(tmp_path, **over)
    assert key in str(exc.value)
    assert "10.13" not in str(exc.value) and PASSWORD not in str(exc.value)


def test_tc1825_on_demand_and_ruled_in_names(tmp_path):
    s = settings(tmp_path, CAMERA_VIEW_DOOR_FLAT_MODE="on_demand",
                 CAMERA_VIEW_SOURCES_RULED_IN="wireguard=10.13.13.0/24 tailnet6=fd7a:115c:a1e0::/48")
    assert s.flat_mode == "on_demand"
    assert s.ruled_in == {"wireguard": ipaddress.ip_network("10.13.13.0/24"),
                          "tailnet6": ipaddress.ip_network("fd7a:115c:a1e0::/48")}


# ── TC-1826 the source allow-list (review round 4) ───────────────────────────


@pytest.mark.parametrize("lan_cidr", [
    "192.0.2.0/24 100.64.0.0/10",
    "192.0.2.0/24",
    "100.64.0.0/10",
    "192.0.2.0/25 192.0.2.128/25 100.100.0.0/16",
    "192.0.2.10",
])
def test_tc1826_the_lan_and_the_tailnet_pass(lan_cidr):
    assert cr.source_violations(lan_cidr, LAN, {}, BRIDGES) == []


@pytest.mark.parametrize("lan_cidr, fragment", [
    ("private_ranges", "private_ranges"),
    ("192.0.2.0/24 private_ranges", "private_ranges"),
    ("10.0.0.0/8 100.64.0.0/10", "entry 1 of 2 lies outside"),
    ("192.168.0.0/16", "lies outside"),
    ("192.0.2.0/23", "lies outside"),                 # wider than the hub's LAN
    ("172.17.0.0/16 192.0.2.0/24", "lies outside"),   # a docker bridge spelled out
    ("172.28.92.2", "lies outside"),                  # caddy's own bridge address
    ("100.64.0.0/9", "lies outside"),                 # wider than the tailnet
    ("10.13.13.0/24", "lies outside"),                # the WireGuard range, not ruled in
    ("fd7a:115c:a1e0::/48", "IPv6 is out as shipped"),
    ("fd00::/8", "IPv6 is out as shipped"),
    ("0.0.0.0/0", "lies outside"),
    ("192.0.2.0/24,100.64.0.0/10", "not an address or CIDR"),
    ("", "LAN_CIDR is empty"),
])
def test_tc1826_anything_else_is_refused(lan_cidr, fragment):
    reasons = cr.source_violations(lan_cidr, LAN, {}, BRIDGES)
    assert any(fragment in r for r in reasons), reasons


def test_tc1826_a_quoted_list_reads_as_compose_and_caddy_read_it(tmp_path):
    # Materialize-Deploy quotes a value holding whitespace (the hub's LAN_CIDR is
    # three @identity ranges); compose strips the quotes, and so must we.
    s = settings(tmp_path, LAN_CIDR='"192.0.2.0/24 100.64.0.0/10"')
    assert s.lan_cidr == "192.0.2.0/24 100.64.0.0/10"
    assert cr.preflight(s, LAN, BRIDGES) == []


def test_tc1826_a_network_overlapping_a_bridge_is_refused_even_inside_the_lan():
    bridges = [ipaddress.ip_network("192.0.2.128/25")]   # a bridge that collides with the LAN
    reasons = cr.source_violations("192.0.2.0/24", LAN, {}, bridges)
    assert any("overlaps a docker bridge" in r for r in reasons)


def test_tc1826_no_lan_interface_is_a_refusal():
    assert any("no interface" in r for r in cr.source_violations("100.64.0.0/10", None, {}, []))


def test_tc1826_ranges_ruled_in_by_name_are_admitted_and_checked():
    ok = cr.parse_ruled_in("wireguard=10.13.13.0/24 tailnet6=fd7a:115c:a1e0::/48")
    assert cr.source_violations("192.0.2.0/24 100.64.0.0/10 10.13.13.0/24 fd7a:115c:a1e0::/48", LAN, ok, BRIDGES) == []
    for words, fragment in (("ula=fd00::/8", "outside Tailscale's"), ("wide=fd7a::/16", "outside Tailscale's"),
                            ("wg=10.0.0.0/8", "/24 or narrower"), ("wg=10.13.0.0/16", "/24 or narrower"),
                            ("pub=203.0.113.0/24", "private IPv4"), ("dock=172.17.5.0/24", "docker bridge")):
        reasons = cr.source_violations("192.0.2.0/24", LAN, cr.parse_ruled_in(words), BRIDGES)
        assert any(fragment in r for r in reasons), (words, reasons)


def test_tc1826_a_ruled_in_ipv6_range_on_an_ipv6_bridge_is_refused():
    v6_bridge = [ipaddress.ip_network("fd7a:115c:a1e0:ab12::/64")]
    reasons = cr.ruled_in_violations(cr.parse_ruled_in("tailnet6=fd7a:115c:a1e0::/48"), v6_bridge)
    assert reasons == ["ruled-in range tailnet6 overlaps a docker bridge on this hub"]


def test_tc1826_messages_name_positions_not_the_households_addresses():
    reasons = cr.source_violations("198.51.100.0/24 192.0.2.0/24", LAN, {}, BRIDGES)
    assert reasons and not any("198.51" in r or "192.0.2" in r for r in reasons)


# ── TC-1845 the open door: the LAN alone ─────────────────────────────────────


@pytest.mark.parametrize("value", ["", cr.DOOR_OPEN_NOBODY, "192.0.2.0/24", "192.0.2.11", "192.0.2.11 192.0.2.0/25"])
def test_tc1845_the_lan_or_part_of_it_may_take_the_door_without_a_login(value):
    assert cr.door_open_violations(value, LAN, BRIDGES) == []


@pytest.mark.parametrize("value, fragment", [
    ("100.64.0.0/10", "lies outside the hub's LAN"),           # the tailnet always goes through the login
    ("100.101.102.103", "lies outside the hub's LAN"),
    ("192.0.2.0/23", "lies outside the hub's LAN"),
    ("10.0.0.0/8", "lies outside the hub's LAN"),
    ("fd7a:115c:a1e0::/48", "lies outside the hub's LAN"),
    ("private_ranges", "not an IPv4 address or CIDR"),
    ("0.0.0.0/0", "lies outside the hub's LAN"),
])
def test_tc1845_nothing_else_may(value, fragment):
    reasons = cr.door_open_violations(value, LAN, BRIDGES)
    assert any(fragment in r for r in reasons) and not any(value in r for r in reasons if value != "private_ranges")


def test_tc1845_a_lan_that_holds_a_bridge_and_no_lan_at_all():
    assert any("overlaps a docker bridge" in r
               for r in cr.door_open_violations("192.0.2.0/24", LAN, [ipaddress.ip_network("192.0.2.128/25")]))
    assert any("no interface" in r for r in cr.door_open_violations("192.0.2.0/24", None, []))
    s_reasons = cr.preflight(type("S", (), {"user": "viewer", "auth_hash": HASH, "lan_cidr": "192.0.2.0/24",
                                            "ruled_in": {}, "door_open": "100.64.0.0/10"})(), LAN, BRIDGES)
    assert any("CAMERA_VIEW_DOOR_OPEN_CIDR" in r for r in s_reasons)


# ── TC-1846 the projection as knobs ──────────────────────────────────────────


def test_tc1846_the_defaults_are_the_panels():
    assert cr.flat_filter(cr.FlatProjection()) == cr.flat_filter() == (
        "fps=10,v360=input=fisheye:output=flat:ih_fov=180:iv_fov=180:h_fov=110:v_fov=77.5521:"
        "yaw=0:pitch=0:w=960:h=540:interp=linear,format=nv12,hwupload")


def test_tc1846_each_knob_reaches_the_filter_and_v_fov_stays_square(tmp_path):
    s = settings(tmp_path, CAMERA_VIEW_FLAT_INPUT_FOV="190x180", CAMERA_VIEW_FLAT_HORIZONTAL_FOV="104.5",
                 CAMERA_VIEW_FLAT_SIZE="1280x720", CAMERA_VIEW_FLAT_YAW="-3", CAMERA_VIEW_FLAT_PITCH="5.5")
    vf = cr.view_config(s)["streams"]["door_flat"].split(" -vf ")[1].split(" ")[0]
    assert ("ih_fov=190:iv_fov=180:h_fov=104.5:v_fov=%.4f:yaw=-3:pitch=5.5:w=1280:h=720"
            % cr.vertical_fov(1280, 720, 104.5)) in vf


def test_tc1846_cylindrical_output_keeps_square_pixels(tmp_path):
    s = settings(tmp_path, CAMERA_VIEW_FLAT_OUTPUT="cylindrical", CAMERA_VIEW_FLAT_HORIZONTAL_FOV="110")
    vf = cr.view_config(s)["streams"]["door_flat"].split(" -vf ")[1].split(" ")[0]
    v = cr.vertical_fov(960, 540, 110, "cylindrical")
    # square pixels: the width's radians per pixel equal the height's tan units per pixel
    assert abs(math.radians(110) / 960 - 2 * math.tan(math.radians(v) / 2) / 540) < 1e-9
    assert "output=cylindrical:ih_fov=180:iv_fov=180:h_fov=110:v_fov=%.4f:" % v in vf
    assert cr.vertical_fov(960, 540, 110) == cr.vertical_fov(960, 540, 110, "flat") != v


def test_tc1828_the_render_line_names_the_living_room_only_when_served():
    assert cr.living_room_note({"door": "", "door_flat": ""}) == ""
    assert cr.living_room_note({"door": "", "livingroom": ""}) == ", livingroom video only"
    assert (cr.living_room_note({"livingroom-main": "", "livingroom": ""})
            == ", livingroom and livingroom-main video only")


@pytest.mark.parametrize("over, key", [
    ({"CAMERA_VIEW_FLAT_OUTPUT": "pannini"}, "CAMERA_VIEW_FLAT_OUTPUT"),
    ({"CAMERA_VIEW_FLAT_OUTPUT": "Flat"}, "CAMERA_VIEW_FLAT_OUTPUT"),
    ({"CAMERA_VIEW_FLAT_INPUT_FOV": "0"}, "CAMERA_VIEW_FLAT_INPUT_FOV"),
    ({"CAMERA_VIEW_FLAT_INPUT_FOV": "180x"}, "CAMERA_VIEW_FLAT_INPUT_FOV"),
    ({"CAMERA_VIEW_FLAT_INPUT_FOV": "400"}, "CAMERA_VIEW_FLAT_INPUT_FOV"),
    ({"CAMERA_VIEW_FLAT_HORIZONTAL_FOV": "180"}, "CAMERA_VIEW_FLAT_HORIZONTAL_FOV"),
    ({"CAMERA_VIEW_FLAT_HORIZONTAL_FOV": "1e2"}, "CAMERA_VIEW_FLAT_HORIZONTAL_FOV"),
    ({"CAMERA_VIEW_FLAT_SIZE": "961x540"}, "CAMERA_VIEW_FLAT_SIZE"),
    ({"CAMERA_VIEW_FLAT_SIZE": "3840x2160"}, "CAMERA_VIEW_FLAT_SIZE"),
    ({"CAMERA_VIEW_FLAT_YAW": "200"}, "CAMERA_VIEW_FLAT_YAW"),
    ({"CAMERA_VIEW_FLAT_PITCH": "-91"}, "CAMERA_VIEW_FLAT_PITCH"),
    ({"CAMERA_VIEW_FLAT_PITCH": "1:2"}, "CAMERA_VIEW_FLAT_PITCH"),
])
def test_tc1846_out_of_range_or_malformed_is_refused_by_key(tmp_path, over, key):
    with pytest.raises(cr.Invalid) as exc:
        settings(tmp_path, **over)
    assert key in str(exc.value)


# ── TC-1827 the hub's networks from `ip -j addr` ─────────────────────────────


def test_tc1827_host_networks_finds_the_lan_and_every_bridge():
    links = [
        {"ifname": "lo", "addr_info": [{"local": "127.0.0.1", "prefixlen": 8}]},
        {"ifname": "enp1s0", "addr_info": [{"local": "192.0.2.10", "prefixlen": 24}]},
        {"ifname": "docker0", "addr_info": [{"local": "172.17.0.1", "prefixlen": 16}]},
        {"ifname": "br-0a1b2c", "addr_info": [{"local": "172.28.92.1", "prefixlen": 24}]},
        {"ifname": "tailscale0", "addr_info": [{"local": "100.101.102.103", "prefixlen": 32}]},
    ]
    lan, bridges = cr.host_networks(json.dumps(links), "192.0.2.10")
    assert lan == LAN
    assert bridges == [ipaddress.ip_network("172.17.0.0/16"), ipaddress.ip_network("172.28.92.0/24")]
    assert cr.host_networks("not json", "192.0.2.10") == (None, [])
    assert cr.host_networks(json.dumps(links), "192.0.2.99")[0] is None


# ── TC-1828 the rendered go2rtc config ───────────────────────────────────────


def test_tc1828_no_listener_five_modules_two_paths_two_streams(tmp_path):
    cfg = cr.view_config(settings(tmp_path))
    assert cfg["app"] == {"modules": ["api", "ws", "rtsp", "mp4", "exec"]}
    assert cfg["api"] == {"listen": "", "unix_listen": "/run/camera-view/api.sock", "allow_paths": ["/", "/api/ws"]}
    for module in ("rtsp", "webrtc", "srtp", "rtmp"):
        assert cfg[module] == {"listen": ""}
    assert "username" not in cfg["api"] and "tls_listen" not in cfg["api"]
    assert cfg["exec"] == {"allow_paths": ["ffmpeg"]}
    assert set(cfg["streams"]) == set(cr.DOOR_STREAMS) == {"door", "door_flat"}   # the living room's relay off
    assert cfg["streams"]["door"] == "rtsp://admin:c%40m%20p%24ss%3A1@192.0.2.10:8554/door#media=video"
    assert cfg["streams"]["door_flat"].startswith("exec:ffmpeg ")
    assert "preload" not in cfg and "publish" not in cfg   # always is the warm unit, not go2rtc's preload


LIVINGROOM = {"CAMERA_LIVINGROOM_RELAY_ENABLED": "true", "CAMERA_LIVINGROOM_RELAY_RTSP_PORT": "8564",
              "CAMERA_LIVINGROOM_RELAY_API_PORT": "1994", "CAMERA_LIVINGROOM_RELAY_UPSTREAM_HOST": "192.0.2.69",
              "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PATH": "/h264/ch1/sub/av_stream",
              "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PASSWORD": "ABCDEF", "CAMERA_LIVINGROOM_RELAY_AUDIO": "true",
              "CAMERA_LIVINGROOM_RELAY_EXPECT_SIZE": "768x432"}


def test_tc1828_the_living_room_is_video_only_through_its_own_relay(tmp_path):
    cfg = cr.view_config(settings(tmp_path, **LIVINGROOM))
    assert set(cfg["streams"]) == {"door", "door_flat", "livingroom"}
    assert cfg["streams"]["livingroom"] == "rtsp://admin:ABCDEF@192.0.2.10:8564/livingroom#media=video"
    cfg = cr.view_config(settings(tmp_path, CAMERA_LIVINGROOM_RELAY_MAIN_PATH="/h264/ch1/main/av_stream",
                                  **LIVINGROOM))
    assert set(cfg["streams"]) == set(cr.STREAMS)
    assert cfg["streams"]["livingroom-main"] == "rtsp://admin:ABCDEF@192.0.2.10:8564/livingroom-main#media=video"
    for name in ("livingroom", "livingroom-main"):    # never the relay's sound, which it offers on request
        assert "audio" not in cfg["streams"][name] and cfg["streams"][name].endswith("#media=video")


def test_tc1828_both_relays_logins_are_redacted(tmp_path):
    s = settings(tmp_path, **LIVINGROOM)
    line = "rtsp://admin:c%40m%20p%24ss%3A1@192.0.2.10:8554/door and ABCDEF and c@m p$ss:1 at 192.0.2.69:554"
    out = cr.redact_all(line, s.relays())
    assert "ABCDEF" not in out and "c@m" not in out and "c%40m" not in out and "192.0.2.69" not in out


def test_tc1828_redact_run_takes_the_viewers_redactor(tmp_path):
    s = settings(tmp_path, **LIVINGROOM)
    out = io.StringIO()
    rc = dr.redact_run(s.relay, [sys.executable, "-c", "print('ABCDEF c@m p$ss:1')"], out=out,
                       redactor=lambda text: cr.redact_all(text, s.relays()))
    assert rc == 0 and "ABCDEF" not in out.getvalue() and "c@m" not in out.getvalue()


def test_tc1828_the_config_is_the_same_in_both_modes(tmp_path):
    a = cr.view_config(settings(tmp_path, CAMERA_VIEW_DOOR_FLAT_MODE="always"))
    b = cr.view_config(settings(tmp_path, CAMERA_VIEW_DOOR_FLAT_MODE="on_demand"))
    assert a == b


# ── TC-1829 the straightened picture's FFmpeg ────────────────────────────────


def test_tc1829_the_argv_holds_no_url_and_no_credential(tmp_path):
    argv = cr.flat_argv()
    text = " ".join(argv)
    assert "rtsp://" not in text and "://" not in text
    for form in dr.secret_forms(settings(tmp_path).relay):
        assert form not in text
    assert argv[argv.index("-i") + 1] == "/config/door.ffconcat"
    assert not any(re.search(r"[\s'\"]", a) for a in argv)


def test_tc1829_the_panels_projection_decoded_on_the_processor_encoded_on_the_graphics():
    argv = cr.flat_argv()
    vf = argv[argv.index("-vf") + 1]
    assert vf == ("fps=10,v360=input=fisheye:output=flat:ih_fov=180:iv_fov=180:h_fov=110:v_fov=77.5521:"
                  "yaw=0:pitch=0:w=960:h=540:interp=linear,format=nv12,hwupload")
    assert argv[argv.index("-init_hw_device") + 1] == "vaapi=va:/dev/dri/renderD128"
    assert argv[argv.index("-filter_hw_device") + 1] == "va"
    assert argv[argv.index("-c:v") + 1] == "h264_vaapi"
    assert argv[argv.index("-f", argv.index("-c:v")) + 1] == "mpegts" and argv[-1] == "-"
    assert "-an" in argv and argv[argv.index("-map") + 1] == "0:v:0"


def test_tc1829_rate_control_is_the_encoders_default():
    # The Owner, 2026-11-12 18:45: default quality, no rate cap. Only the
    # keyframe interval and B-frames differ from the defaults (joining).
    argv = cr.flat_argv()
    for flag in ("-b:v", "-maxrate", "-bufsize", "-rc_mode", "-qp", "-global_quality", "-crf", "-q:v"):
        assert flag not in argv
    assert argv[argv.index("-g") + 1] == "20" and argv[argv.index("-bf") + 1] == "0"


def test_tc1829_vertical_fov_matches_the_panels_formula():
    assert abs(cr.vertical_fov(960, 540, 110) - 77.5521) < 1e-4
    panel = ROOT.parent / "OfficeWallNaglight" / "doorstream" / "service.py"
    if not panel.exists():
        pytest.skip("OfficeWallNaglight is not beside this repo")
    text = panel.read_text(encoding="utf-8")
    assert "half = math.tan(math.radians(horizontal_fov) / 2) * height / width" in text
    assert "return math.degrees(2 * math.atan(half))" in text
    assert "v360=input=fisheye:output=flat:" in text and "interp=linear" in text


def test_tc1829_the_software_encoder_is_a_lab_seam_only():
    soft = cr.flat_argv("software")
    assert "h264_vaapi" not in soft and "-init_hw_device" not in soft and "hwupload" not in " ".join(soft)
    with pytest.raises(ValueError):
        cr.flat_argv("qsv")
    source = (STACK / "camera-remote" / "camera_remote.py").read_text(encoding="utf-8")
    main = source[source.index("def main("):]
    assert "software" not in main          # no verb or flag reaches it
    assert "encoder" not in (STACK / ".env.example").read_text(encoding="utf-8").split("CAMERA_VIEW_ENABLED")[1][:3000]


# ── TC-1830 render: the two files, and refusals ──────────────────────────────


def test_tc1830_the_input_document_is_the_relays_own(tmp_path):
    s = settings(tmp_path)
    doc = cr.ffconcat_document(s).decode()
    assert doc == dr.relay_input_document(s.relay, 10_000_000).decode()
    assert doc.splitlines()[1] == "file 'rtsp://admin:c%40m%20p%24ss%3A1@192.0.2.10:8554/door'"


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX file modes")
def test_tc1830_render_writes_two_0600_files_in_a_0700_directory(tmp_path):
    out = tmp_path / "run"
    out.mkdir(mode=0o755)          # an existing, looser directory is tightened
    written = cr.render(settings(tmp_path), str(out))
    assert sorted(Path(p).name for p in written) == ["door.ffconcat", "go2rtc.yaml"]
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    for p in written:
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_tc1830_render_refuses_a_broad_gate_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cr, "read_host_networks", lambda ip: (LAN, BRIDGES))
    out = tmp_path / "run"
    env = write_env(tmp_path, LAN_CIDR="private_ranges")
    assert cr.main(["--env-file", env, "render", "--out-dir", str(out)]) == 2
    assert not out.exists()
    assert "private_ranges" in capsys.readouterr().out


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX file modes")
def test_tc1830_render_needs_no_login_for_the_door(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "read_host_networks", lambda ip: (LAN, BRIDGES))
    out = tmp_path / "run"
    assert cr.main(["--env-file", write_env(tmp_path), "render", "--out-dir", str(out)]) == 0
    assert (out / "go2rtc.yaml").exists()


# ── TC-1831 import-login ─────────────────────────────────────────────────────


def stdin(monkeypatch, data):
    monkeypatch.setattr(cr.sys, "stdin", type("S", (), {"buffer": io.BytesIO(data)})())


def test_tc1831_the_hash_goes_in_doubled_and_is_never_printed(tmp_path, monkeypatch, capsys):
    env = write_env(tmp_path, CAMERA_VIEW_AUTH_HASH="", OTHER="kept")
    stdin(monkeypatch, (HASH + "\r\n").encode())
    assert cr.main(["--env-file", env, "import-login"]) == 0
    text = Path(env).read_text(encoding="utf-8")
    assert "CAMERA_VIEW_AUTH_HASH=" + HASH.replace("$", "$$") in text and "OTHER=kept" in text
    assert text.count("CAMERA_VIEW_AUTH_HASH=") == 1
    assert cr.ViewSettings(env).auth_hash == HASH
    out = capsys.readouterr().out
    assert HASH not in out and "aaaa" not in out


@pytest.mark.parametrize("blob", [b"", b"\n", b"hunter2\n", b"bG9ja2Vk\n", (HASH + "\n" + HASH).encode(),
                                  b"$2b$09$" + b"a" * 53, b"$2b$12$" + b"a" * 52, b"$1$" + b"a" * 60,
                                  b"x" * 300, "\u201c$2b$12$".encode("utf-8") + b"a" * 53])
def test_tc1831_anything_but_one_bcrypt_hash_is_refused(tmp_path, monkeypatch, blob):
    env = write_env(tmp_path)
    before = Path(env).read_text(encoding="utf-8")
    stdin(monkeypatch, blob)
    assert cr.main(["--env-file", env, "import-login"]) == 2
    assert Path(env).read_text(encoding="utf-8") == before


# ── TC-1832 the login applies only to streams behind it ──────────────────────


def test_tc1832_every_stream_is_behind_the_login():
    assert cr.LOGIN_STREAMS == cr.STREAMS and set(cr.DOOR_STREAMS) < set(cr.STREAMS)


@pytest.mark.parametrize("value, fragment", [("", "is empty"), ("bG9ja2Vk", "shipped lock"),
                                             ("$2b$12$short", "not a bcrypt hash")])
def test_tc1832_the_page_needs_a_real_hash(tmp_path, value, fragment):
    s = settings(tmp_path, CAMERA_VIEW_AUTH_HASH=value.replace("$", "$$"))
    assert any(fragment in r for r in cr.preflight(s, LAN, BRIDGES))
    assert cr.preflight(settings(tmp_path), LAN, BRIDGES) == []


# ── TC-1833 check: caddy's own environment, the files, the processes ─────────


def inspect(env):
    return json.dumps([{"Config": {"Env": ["%s=%s" % kv for kv in env.items()]}}])


def test_tc1833_only_the_viewers_keys_are_kept_from_caddys_environment():
    got = cr.caddy_env_from_inspect(inspect({"LAN_CIDR": "x", "CLOUDFLARE_API_TOKEN": "secret",
                                             "GAME_WS_SECRET": "s", "CAMERA_VIEW_ENABLED": "true"}))
    assert got == {"LAN_CIDR": "x", "CAMERA_VIEW_ENABLED": "true"}
    assert cr.caddy_env_from_inspect("") == {} and cr.caddy_env_from_inspect("[]") == {}


def caddy_env(**over):
    env = {"LAN_CIDR": "192.0.2.0/24 100.64.0.0/10", "CAMERA_VIEW_ENABLED": "true",
           "CAMERA_VIEW_AUTH_USER": "viewer", "CAMERA_VIEW_AUTH_HASH": HASH,
           "CAMERA_VIEW_DOOR_OPEN_CIDR": cr.DOOR_OPEN_NOBODY}
    env.update(over)
    return env


def test_tc1833_caddy_equal_to_the_env_passes(tmp_path):
    assert cr.caddy_violations(settings(tmp_path), caddy_env(), LAN, BRIDGES) == []


@pytest.mark.parametrize("over, fragment", [
    ({"CAMERA_VIEW_ENABLED": "false"}, "CAMERA_VIEW_ENABLED differs"),
    ({"LAN_CIDR": "10.0.0.0/8 100.64.0.0/10"}, "caddy's own LAN_CIDR entry 1 of 2 lies outside"),
    ({"LAN_CIDR": "private_ranges"}, "caddy's own LAN_CIDR is or holds private_ranges"),
    ({"LAN_CIDR": "192.0.2.0/24"}, "LAN_CIDR differs"),
])
def test_tc1833_caddy_serving_something_else_fails(tmp_path, over, fragment):
    reasons = cr.caddy_violations(settings(tmp_path), caddy_env(**over), LAN, BRIDGES)
    assert any(fragment in r for r in reasons), reasons


def test_tc1833_the_login_and_the_open_door_are_compared_never_printed(tmp_path):
    s = settings(tmp_path)
    reasons = cr.caddy_violations(s, caddy_env(CAMERA_VIEW_AUTH_HASH="bG9ja2Vk"), LAN, BRIDGES)
    assert any("CAMERA_VIEW_AUTH_HASH differs" in r for r in reasons) and not any(HASH in r for r in reasons)
    assert any("shipped lock" in r for r in reasons)
    reasons = cr.caddy_violations(s, caddy_env(CAMERA_VIEW_DOOR_OPEN_CIDR="192.0.2.0/24"), LAN, BRIDGES)
    assert any("CAMERA_VIEW_DOOR_OPEN_CIDR differs" in r for r in reasons)
    s = settings(tmp_path, CAMERA_VIEW_DOOR_OPEN_CIDR="192.0.2.0/24")
    assert cr.caddy_violations(s, caddy_env(CAMERA_VIEW_DOOR_OPEN_CIDR="192.0.2.0/24"), LAN, BRIDGES) == []
    reasons = cr.caddy_violations(settings(tmp_path, CAMERA_VIEW_DOOR_OPEN_CIDR="100.64.0.0/10"),
                                  caddy_env(CAMERA_VIEW_DOOR_OPEN_CIDR="100.64.0.0/10"), LAN, BRIDGES)
    assert any("caddy's own CAMERA_VIEW_DOOR_OPEN_CIDR entry 1 of 1 lies outside" in r for r in reasons)


def test_tc1833_the_order_of_lan_cidr_words_decides_nothing(tmp_path):
    assert cr.caddy_violations(settings(tmp_path), caddy_env(LAN_CIDR="100.64.0.0/10 192.0.2.0/24"), LAN, BRIDGES) == []


def test_tc1833_an_unlistable_proc_is_a_named_reason(tmp_path):
    assert cr.credential_in_processes(["x"], str(tmp_path / "no-proc")) == ["%s could not be listed" % (tmp_path / "no-proc")]


@pytest.mark.parametrize("enabled, mode, warm, flagged", [
    ("true", "always", False, True), ("true", "always", True, False), ("true", "always", None, False),
    ("true", "on_demand", False, False), ("false", "always", False, False)])
def test_tc1833_always_needs_the_warm_unit_enabled(tmp_path, monkeypatch, enabled, mode, warm, flagged):
    monkeypatch.setattr(cr, "credential_in_processes", lambda forms: [])
    s = settings(tmp_path, CAMERA_VIEW_ENABLED=enabled, CAMERA_VIEW_DOOR_FLAT_MODE=mode)
    reasons = cr.check(s, LAN, BRIDGES, caddy_env(CAMERA_VIEW_ENABLED=enabled), warm)["reasons"]
    assert any("homehub-camera-view-warm.service is not enabled" in r for r in reasons) == flagged


def test_tc1833_no_caddy_is_a_failure(tmp_path):
    assert any("could not be read" in r for r in cr.caddy_violations(settings(tmp_path), {}, LAN, BRIDGES))


def test_tc1833_the_process_scan_reports_pids_never_the_match(tmp_path):
    proc = tmp_path / "proc"
    for pid, cmd, environ in (("10", b"go2rtc\0", b"PATH=/bin\0"),
                              ("11", b"ffmpeg\0-i\0rtsp://admin:c%40m%20p%24ss%3A1@x/door\0", b""),
                              ("12", b"sh\0", b"X=c@m p$ss:1\0"), ("self", b"", b"")):
        (proc / pid).mkdir(parents=True)
        (proc / pid / "cmdline").write_bytes(cmd)
        (proc / pid / "environ").write_bytes(environ)
    hits = cr.credential_in_processes(["c@m p$ss:1", "c%40m%20p%24ss%3A1"], str(proc))
    assert hits == ["pid 11 cmdline", "pid 12 environ"]


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX file modes")
def test_tc1833_file_modes(tmp_path):
    run = tmp_path / "run"
    cr.render(settings(tmp_path), str(run))
    assert cr.file_mode_violations(str(run)) == []
    os.chmod(str(run / "door.ffconcat"), 0o644)
    assert cr.file_mode_violations(str(run)) == ["%s is not 0600" % (run / "door.ffconcat")]
    assert cr.file_mode_violations(str(tmp_path / "absent")) == []


# ── TC-1834 the Caddyfile's camera site ──────────────────────────────────────


def site_block():
    text = (STACK / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    start = text.index("\n{$CAMERA_VIEW_HOST} {\n") + 1
    end = text.index("\n}\n", start) + 2
    return text[start:end]


def handle(block, matcher):
    i = block.index("handle %s {" % matcher)
    depth, j = 0, block.index("{", i)
    for k in range(j, len(block)):
        depth += {"{": 1, "}": -1}.get(block[k], 0)
        if depth == 0:
            return block[i:k + 1]
    raise AssertionError(matcher)


def test_tc1834_order_switch_then_lan_then_open_door_then_the_login():
    b = site_block()
    assert b.index("import acme_cloudflare") < b.index("@cv_off") < b.index("@lan remote_ip {$LAN_CIDR}")
    assert '@cv_off expression `{env.CAMERA_VIEW_ENABLED} != "true"`' in b
    assert 'respond "cameras: off" 404' in handle(b, "@cv_off")
    assert b.rstrip().endswith('handle {\n        respond "cameras: LAN only" 403\n    }\n}')
    lan = handle(b, "@lan")
    assert "import throttle" in lan and "encode" not in lan
    assert lan.index("@cv_door_open") < lan.index("@cv_player_open") < lan.index("basic_auth")
    assert lan.rstrip().endswith('handle {\n                respond "cameras: not here" 403\n            }\n        }\n    }')


def test_tc1834_the_door_alone_is_open_and_only_to_the_open_range():
    b = site_block()
    door = re.search(r"@cv_door_open \{\n\s+remote_ip \{\$CAMERA_VIEW_DOOR_OPEN_CIDR\}\n\s+path /api/ws\n"
                     r"\s+expression `(.*)`\n\s+\}", b)
    assert door, "the open door's websocket matcher"
    names = re.findall(r'\{query\} == "src=([a-z_]+)"', door.group(1))
    assert sorted(names) == sorted(cr.DOOR_STREAMS)
    assert re.fullmatch(r'\{query\} == "src=[a-z_]+"( \|\| \{query\} == "src=[a-z_]+")*', door.group(1))
    assert re.search(r"@cv_player_open \{\n\s+remote_ip \{\$CAMERA_VIEW_DOOR_OPEN_CIDR\}\n\s+method GET HEAD\n"
                     r"\s+path /stream.html /video-stream.js /video-rtc.js\n\s+\}", b)
    assert "basic_auth" not in handle(b, "@cv_door_open") and "basic_auth" not in handle(b, "@cv_player_open")


def test_tc1834_everything_else_is_behind_the_login_and_names_every_stream():
    b = site_block()
    login = re.search(r"@cv_stream \{\n\s+path /api/ws\n\s+expression `\{query\} in \[(.*)\]`\n\s+\}", b)
    assert login, "the login streams' matcher"
    assert sorted(re.findall(r'"src=([a-z_-]+)"', login.group(1))) == sorted(cr.LOGIN_STREAMS)
    assert re.fullmatch(r'"src=[a-z_-]+"(, "src=[a-z_-]+")*', login.group(1))
    assert ("handle {\n            basic_auth {\n                {$CAMERA_VIEW_AUTH_USER} {$CAMERA_VIEW_AUTH_HASH}\n"
            "            }\n            @cv_stream {") in b
    assert b.count("basic_auth") == 1


def test_tc1834_only_the_player_and_the_redirects():
    b = site_block()
    assert re.search(r"@cv_player \{\n\s+method GET HEAD\n\s+path /stream.html /video-stream.js /video-rtc.js\n\s+\}", b)
    assert "redir * /stream.html?src=door_flat&src=livingroom&mode=mse 302" in handle(b, "/")
    for path, src in (("/door", "door_flat"), ("/raw", "door"), ("/livingroom", "livingroom"),
                      ("/livingroom-main", "livingroom-main")):
        assert "redir * /stream.html?src=%s&mode=mse 302" % src in handle(b, path)
    assert not re.search(r"redir /", b)        # `redir /x` reads /x as a matcher: every redirect a blank 200
    assert b.count("reverse_proxy unix//srv/camera-view/api.sock {") == 4
    for proxy in re.findall(r"reverse_proxy unix//srv/camera-view/api.sock \{[^}]*\}", b):
        assert "header_up -Authorization" in proxy and "header_up -Cookie" in proxy
    assert not re.search(r"handle /api", b)


def test_tc1834_every_variable_is_passed_by_caddys_compose_environment():
    block = compose_block("caddy")
    for var in ("CAMERA_VIEW_HOST", "CAMERA_VIEW_ENABLED", "CAMERA_VIEW_AUTH_USER", "CAMERA_VIEW_AUTH_HASH",
                "CAMERA_VIEW_DOOR_OPEN_CIDR"):
        assert re.search(r"^      %s: \$\{" % var, block, re.M), var
    assert "CAMERA_VIEW_AUTH_HASH: ${CAMERA_VIEW_AUTH_HASH:-%s}" % cr.LOCKED_HASH in block
    assert "CAMERA_VIEW_AUTH_USER: ${CAMERA_VIEW_AUTH_USER:-%s}" % cr.DEFAULT_USER in block
    assert "CAMERA_VIEW_DOOR_OPEN_CIDR: ${CAMERA_VIEW_DOOR_OPEN_CIDR:-%s}" % cr.DOOR_OPEN_NOBODY in block
    assert "CAMERA_VIEW_ENABLED: ${CAMERA_VIEW_ENABLED:-false}" in block
    assert "- %s:/srv/camera-view:ro" % cr.STATE_DIR in block


# ── TC-1835 compose: the viewer's service ────────────────────────────────────


def compose_block(name):
    text = (STACK / "docker-compose.yml").read_text(encoding="utf-8")
    start = text.index("\n  %s:\n" % name)
    end = re.search(r"\n  [a-z0-9-]+:\n|\n[a-z]+:\n", text[start + 3:]).start() + start + 3
    return text[start:end]


def test_tc1835_the_viewer_has_the_relays_hardening_the_graphics_and_no_port():
    b = compose_block("camera-view")
    assert "image: ${DOOR_RELAY_IMAGE}" in b and "container_name: %s" % cr.CONTAINER in b
    assert 'profiles: ["camera-view"]' in b and 'restart: "no"' in b and "network_mode: host" in b
    assert "ports:" not in b and "environment:" not in b
    assert "read_only: true" in b and "cap_drop:\n      - ALL" in b and "no-new-privileges:true" in b
    assert "driver: none" in b and "diun.enable=false" in b
    assert "- /dev/dri/renderD128:/dev/dri/renderD128" in b and cr.RENDER_NODE == "/dev/dri/renderD128"
    for source, target in (("%s/go2rtc.yaml" % cr.RUN_DIR, cr.CONFIG_IN_CONTAINER),
                           ("%s/door.ffconcat" % cr.RUN_DIR, cr.FFCONCAT_IN_CONTAINER),
                           (cr.STATE_DIR, os.path.dirname(cr.SOCKET_IN_CONTAINER))):
        assert re.search(r"source: %s\n\s+target: %s\n" % (re.escape(source), re.escape(target)), b), source
    live = "\n".join(l for l in b.splitlines() if not l.lstrip().startswith("#"))
    assert live.count("create_host_path: false") == 3
    profiles = re.search(r"^COMPOSE_PROFILES=(.*)$", (STACK / ".env.example").read_text(encoding="utf-8"), re.M)
    assert "camera-view" not in profiles.group(1)


# ── TC-1836 the two units ────────────────────────────────────────────────────


def unit(name):
    return [l for l in (STACK / "camera-remote" / name).read_text(encoding="utf-8").splitlines()
            if l and not l.startswith("#")]


def test_tc1836_the_viewer_unit_is_knob_gated_rendered_redacted_and_never_requires_docker():
    live = unit("homehub-camera-view.service")
    text = "\n".join(live)
    assert "camera_remote.py --env-file /opt/homehub/stack/.env condition" in text
    assert "ExecStartPre=/usr/bin/python3 /opt/homehub/stack/camera-remote/camera_remote.py --env-file /opt/homehub/stack/.env render" in live
    assert ("redact-run -- /usr/bin/docker compose --profile camera-view up --no-deps --no-log-prefix "
            "--exit-code-from camera-view camera-view") in text
    assert {"RuntimeDirectory=homehub-camera-view", "RuntimeDirectoryMode=0700", "StateDirectory=homehub-camera-view",
            "StateDirectoryMode=0700", "Restart=always", "AssertPathExists=/opt/homehub/stack/.env"} <= set(live)
    assert cr.RUN_DIR == "/run/homehub-camera-view" and cr.STATE_DIR == "/var/lib/homehub-camera-view"
    assert not [l for l in live if l.startswith(("Requires=", "PartOf=")) and "docker" in l]


def test_tc1836_the_warm_unit_rides_on_the_viewer_and_touches_only_a_unix_socket():
    live = unit("homehub-camera-view-warm.service")
    assert "BindsTo=homehub-camera-view.service" in live and "WantedBy=homehub-camera-view.service" in live
    assert any(l.startswith("ExecCondition=") and l.endswith(" condition-warm") for l in live)
    assert any(l.startswith("ExecStart=") and l.endswith(" keep-warm") for l in live)
    assert {"Restart=always", "RestrictAddressFamilies=AF_UNIX", "PrivateNetwork=yes", "NoNewPrivileges=yes",
            "ProtectSystem=strict", "CapabilityBoundingSet=", "AssertPathExists=/opt/homehub/stack/.env"} <= set(live)


@pytest.mark.parametrize("enabled, mode, rc", [("true", "always", 0), ("true", "", 0), ("true", "on_demand", 1),
                                               ("false", "always", 1), ("", "always", 1)])
def test_tc1836_the_conditions(tmp_path, enabled, mode, rc):
    env = write_env(tmp_path, CAMERA_VIEW_ENABLED=enabled, CAMERA_VIEW_DOOR_FLAT_MODE=mode)
    assert cr.main(["--env-file", env, "condition-warm"]) == rc
    assert cr.main(["--env-file", env, "condition"]) == (0 if enabled == "true" else 1)


@pytest.mark.parametrize("enabled, mode", [("false", "always"), ("true", "on_demand")])
def test_tc1836_keep_warm_refuses_by_itself_when_it_should_not_run(tmp_path, monkeypatch, enabled, mode):
    monkeypatch.setattr(cr, "keep_warm", lambda *a, **k: pytest.fail("keep_warm ran"))
    env = write_env(tmp_path, CAMERA_VIEW_ENABLED=enabled, CAMERA_VIEW_DOOR_FLAT_MODE=mode)
    assert cr.main(["--env-file", env, "keep-warm"]) == 78


# ── TC-1837 the template and the DNS record ──────────────────────────────────


def env_example(key):
    m = re.search(r"^%s=(.*)$" % key, (STACK / ".env.example").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def test_tc1837_the_template_ships_it_off_always_no_hash_no_open_door_no_ruled_range():
    assert env_example("CAMERA_VIEW_ENABLED") == "false"
    assert env_example("CAMERA_VIEW_DOOR_FLAT_MODE") == "always" == cr.DEFAULT_FLAT_MODE
    assert env_example("CAMERA_VIEW_SUBDOMAIN") == "cameras"
    assert env_example("CAMERA_VIEW_AUTH_USER") == cr.DEFAULT_USER
    assert env_example("CAMERA_VIEW_AUTH_HASH") == ""
    assert env_example("CAMERA_VIEW_SOURCES_RULED_IN") == ""
    assert env_example("CAMERA_VIEW_DOOR_OPEN_CIDR") == ""
    assert (env_example("CAMERA_VIEW_FLAT_INPUT_FOV"), env_example("CAMERA_VIEW_FLAT_HORIZONTAL_FOV"),
            env_example("CAMERA_VIEW_FLAT_SIZE"), env_example("CAMERA_VIEW_FLAT_YAW"),
            env_example("CAMERA_VIEW_FLAT_PITCH"), env_example("CAMERA_VIEW_FLAT_OUTPUT")) == (
        "180", "110", "960x540", "0", "0", "flat")


def test_tc1837_the_dns_record_exists_only_while_the_page_is_on():
    text = (STACK / "provision" / "provision-technitium.sh").read_text(encoding="utf-8")
    m = re.search(r'if \[ "\$\{CAMERA_VIEW_ENABLED:-\}" = true \]; then\n\s+add_a "\$TOKEN" '
                  r'"\$\{CAMERA_VIEW_SUBDOMAIN:-cameras\}\.\$\{DOMAIN\}" "\$LAN_IP"\n\s+fi', text)
    assert m
    assert "CAMERA_VIEW" not in text.replace(m.group(0), "").split("CAMERA_VIEW_ENABLED + CAMERA_VIEW_SUBDOMAIN")[1]


# ── TC-1838 keep-warm: the websocket client against fake sockets ─────────────


def server_frame(opcode, payload):
    n = len(payload)
    if n < 126:
        head = bytes([0x80 | opcode, n])
    elif n < 65536:
        head = bytes([0x80 | opcode, 126]) + struct.pack(">H", n)
    else:
        head = bytes([0x80 | opcode, 127]) + struct.pack(">Q", n)
    return head + payload


class FakeSock:
    """Answers with `script` (bytes, or an exception to raise) one recv at a time."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = b""
        self.closed = False

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if not self.script:
            return b""
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True


UPGRADED = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n"


def test_tc1838_client_frames_are_masked_with_the_right_lengths():
    mask = b"\x01\x02\x03\x04"
    for n, head_len in ((5, 2), (200, 4), (70000, 10)):
        frame = cr.ws_client_frame(b"x" * n, mask)
        assert frame[0] == 0x81 and frame[1] & 0x80
        assert frame[head_len:head_len + 4] == mask
        body = frame[head_len + 4:]
        assert bytes(b ^ mask[i % 4] for i, b in enumerate(body)) == b"x" * n


def test_tc1838_the_reader_takes_split_frames_of_every_length():
    data = UPGRADED + server_frame(1, b'{"type":"mse"}') + server_frame(2, b"v" * 300) + server_frame(2, b"w" * 70000)
    chunks = [data[i:i + 7] for i in range(0, len(data), 7)]
    r = cr.FrameReader(FakeSock(chunks).recv)
    assert r.head() == 101
    assert r.frame() == (1, b'{"type":"mse"}')
    assert r.frame() == (2, b"v" * 300)
    assert r.frame() == (2, b"w" * 70000)
    with pytest.raises(EOFError):
        r.frame()
    huge = cr.FrameReader(FakeSock([UPGRADED + bytes([0x82, 127]) + struct.pack(">Q", cr.WARM_MAX_FRAME + 1)]).recv)
    huge.head()
    with pytest.raises(ValueError):
        huge.frame()


def test_tc1838_a_timeout_part_way_through_a_frame_loses_nothing():
    import socket as socketmod
    frame = server_frame(2, b"v" * 300)
    reader = cr.FrameReader(FakeSock([frame[:3], socketmod.timeout(), frame[3:150], socketmod.timeout(),
                                      frame[150:] + server_frame(8, b"")]).recv)
    for _ in range(2):
        with pytest.raises(socketmod.timeout):
            reader.frame()
    assert reader.frame() == (2, b"v" * 300)
    assert reader.frame() == (8, b"")


def test_tc1838_a_session_asks_for_door_flat_in_mse_and_reports_flowing_once():
    sock = FakeSock([UPGRADED, server_frame(1, b'{"type":"mse","value":"video/mp4"}'),
                     server_frame(2, b"a" * 10), server_frame(2, b"b" * 10), server_frame(8, b"")])
    flowing = []
    assert cr.warm_session(sock, "door_flat", lambda: 0.0, lambda: flowing.append(1)) == "closed"
    assert flowing == [1]
    assert sock.sent.startswith(b"GET /api/ws?src=door_flat HTTP/1.1\r\n")
    assert b"Origin" not in sock.sent and b"Authorization" not in sock.sent
    reader = cr.FrameReader(FakeSock([sock.sent[sock.sent.index(b"\r\n\r\n") + 4:]]).recv)
    opcode, payload = reader.frame()
    assert (opcode, json.loads(payload)) == (1, {"type": "mse", "value": ""})


@pytest.mark.parametrize("script, why", [
    ([b"HTTP/1.1 403 Forbidden\r\n\r\n"], "refused 403"),
    ([UPGRADED, server_frame(1, b'{"type":"error","value":"mse: streams: exec/pipe: EOF"}')], "error "),
    ([UPGRADED], "eof"),
    ([], "eof"),                                   # closed before any answer
    ([b"HTTP/1.1 101 Switching"], "eof"),          # closed part-way through the answer
    ([__import__("socket").timeout()], "no answer"),  # accepted, never answered
])
def test_tc1838_a_session_ends_for_each_reason(script, why):
    assert cr.warm_session(FakeSock(script), "door_flat", lambda: 0.0, lambda: None).startswith(why)


def test_tc1838_a_silent_session_ends_after_the_silence_limit():
    import socket as socketmod
    times = iter([0.0, 10.0, 20.0, 31.0])
    sock = FakeSock([UPGRADED, socketmod.timeout(), socketmod.timeout(), socketmod.timeout()])
    assert cr.warm_session(sock, "door_flat", lambda: next(times), lambda: None) == "silent"


def test_tc1838_keep_warm_backs_off_resets_and_redacts():
    lines, waits = [], []
    clock = {"t": 0.0}

    def connect(path):
        assert path == "/var/lib/homehub-camera-view/api.sock"
        n = len(waits)
        if n in (0, 1, 2):
            raise FileNotFoundError(path)
        if n == 3:      # a healthy session of 61 s, then an error naming the relay
            clock["t"] += 61
            return FakeSock([UPGRADED, server_frame(2, b"v"), server_frame(
                1, b'{"type":"error","value":"mse: rtsp://***@192.0.2.10:8554/door c@m p$ss:1"}')])
        return FakeSock([UPGRADED])

    cr.keep_warm("/var/lib/homehub-camera-view/api.sock", "door_flat", lines.append,
                 lambda text: text.replace("c@m p$ss:1", "***"), connect=connect,
                 sleep=waits.append, clock=lambda: clock["t"], rounds=6)
    assert waits == [2, 5, 10, 2, 2, 5]
    assert "camera-view warm: door_flat is flowing" in lines
    assert not any("c@m" in l for l in lines)
    assert any("ended: no socket (FileNotFoundError)" in l for l in lines)


def test_tc1838_keep_warm_uses_the_viewers_socket_and_the_relay_redaction(tmp_path, monkeypatch):
    seen = {}

    def fake(path, stream, log, redact, **kw):
        seen.update(path=path, stream=stream, red=redact("rtsp://admin:c%40m%20p%24ss%3A1@x/door " + PASSWORD))
    monkeypatch.setattr(cr, "keep_warm", fake)
    assert cr.main(["--env-file", write_env(tmp_path), "keep-warm"]) == 0
    assert seen["path"] == "/var/lib/homehub-camera-view/api.sock" and seen["stream"] == "door_flat"
    assert PASSWORD not in seen["red"] and "c%40m" not in seen["red"]
