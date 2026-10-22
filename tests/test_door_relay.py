"""The door relay's host side and the recorder's pure core (SN-050, SR-105..SR-107).

What runs here, on the dev PC: the relay config (every listener named, auth on
both, video only, the credential encoded), the field validation that names keys
and never values, the panel-to-hub credential import, the pin agreeing with the
compose image, the unit/compose/firstboot contract, and the recorder's MPEG-TS
cutter, pruning, guard-before-every-segment, quota stop and continuity rule -
on synthetic TS packets built below.

What does not: the real relay, ffmpeg and a camera-like source. That is
stack/door/tests/door-relay-lab.sh (root + docker; TC-1385..TC-1392).
Implements: TC-1377..TC-1384, TC-1393..TC-1398.
"""

from __future__ import annotations

import errno
import io
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
for sub in ("door", "scratch", "monitoring"):
    sys.path.insert(0, str(STACK / sub))
import door_relay as dr  # noqa: E402
import door_recorder as rec  # noqa: E402

PASSWORD = 'c@m p$ss:1 #x'


def write_env(tmp_path, **over):
    lines = {
        "LAN_IP": "192.0.2.10",
        "DOOR_RELAY_UPSTREAM_HOST": "192.0.2.20",
        "DOOR_RELAY_UPSTREAM_USERNAME": "admin",
        # As Materialize-Deploy writes it: $ doubled, whitespace -> one quote pair.
        "DOOR_RELAY_UPSTREAM_PASSWORD": '"c@m p$$ss:1 #x"',
    }
    lines.update(over)
    path = tmp_path / "env"
    path.write_text("".join("%s=%s\n" % kv for kv in lines.items()))
    return str(path)


@pytest.fixture(autouse=True)
def no_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))


# ── TC-1380 the relay config ─────────────────────────────────────────────────


def test_tc1380_every_listener_is_named_bound_and_authenticated(tmp_path):
    s = dr.RelaySettings(write_env(tmp_path))
    cfg = dr.relay_config(s)
    assert cfg["rtsp"]["listen"] == "192.0.2.10:8554"
    assert cfg["api"]["listen"] == "127.0.0.1:1984"
    assert cfg["api"]["local_auth"] is True  # loopback is NOT exempt from the API credential
    for mod in ("rtsp", "api"):
        assert cfg[mod]["username"] == "admin" and cfg[mod]["password"] == PASSWORD
    for mod in ("webrtc", "srtp", "rtmp"):
        assert cfg[mod] == {"listen": ""}
    assert set(cfg) == {"api", "rtsp", "webrtc", "srtp", "rtmp", "log", "streams"}


def test_tc1380_video_only_and_the_credential_percent_encoded(tmp_path):
    s = dr.RelaySettings(write_env(tmp_path))
    src = dr.relay_config(s)["streams"]["door"]
    assert src.endswith("#media=video")
    assert src.startswith("rtsp://admin:c%40m%20p%24ss%3A1%20%23x@192.0.2.20:554/H.264")
    assert dr.relay_config(s)["rtsp"]["default_query"] == "video"
    assert s.consumer_url() == "rtsp://admin:c%40m%20p%24ss%3A1%20%23x@192.0.2.10:8554/door"


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX file modes")
def test_tc1380_render_writes_0600(tmp_path):
    out = tmp_path / "run" / "go2rtc.yaml"
    assert dr.main(["--env-file", write_env(tmp_path), "render", "--out", str(out)]) == 0
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert json.loads(out.read_text())["rtsp"]["password"] == PASSWORD


# ── TC-1381 validation names the key, never the value ────────────────────────


@pytest.mark.parametrize("over, key", [
    ({"LAN_IP": "hub.lan"}, "LAN_IP"),
    ({"DOOR_RELAY_UPSTREAM_HOST": ""}, "DOOR_RELAY_UPSTREAM_HOST"),
    ({"DOOR_RELAY_UPSTREAM_HOST": "192.0.2.10"}, "own address"),
    ({"DOOR_RELAY_UPSTREAM_PORT": "70000"}, "DOOR_RELAY_UPSTREAM_PORT"),
    ({"DOOR_RELAY_UPSTREAM_PATH": "H.264"}, "DOOR_RELAY_UPSTREAM_PATH"),
    ({"DOOR_RELAY_UPSTREAM_PASSWORD": ""}, "DOOR_RELAY_UPSTREAM_PASSWORD"),
    ({"DOOR_RELAY_STREAM": "Door Cam"}, "DOOR_RELAY_STREAM"),
])
def test_tc1381_a_bad_field_is_refused_by_name(tmp_path, over, key):
    with pytest.raises(dr.Invalid) as err:
        dr.RelaySettings(write_env(tmp_path, **over))
    assert key in str(err.value)
    assert "p$ss" not in str(err.value) and "c@m" not in str(err.value)


def test_tc1381_the_secret_reader_undoes_exactly_the_materialisers_encoding():
    assert dr.env_secret_value('"a b$$c"') == "a b$c"
    assert dr.env_secret_value("a$$b#c") == "a$b#c"   # a ' #' is NOT a comment in a secret
    assert dr.env_secret_value(dr.env_encode("x y $z")) == "x y $z"


# ── TC-1382 import-upstream through a pipe ───────────────────────────────────


def records(**over):
    base = {"host": "192.0.2.20", "port": "554", "path": "/H.264", "username": "admin", "password": PASSWORD}
    base.update(over)
    return b"".join(("%s=%s" % kv).encode() + b"\0" for kv in base.items())


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX file modes")
def test_tc1382_import_writes_the_five_keys_encoded_and_keeps_the_rest(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LAN_IP=192.0.2.10\nDOOR_RELAY_UPSTREAM_HOST=\nOTHER=1\n")
    keys = dr.import_upstream(str(env), records())
    assert keys == sorted(dr.UPSTREAM_KNOB.values())
    text = env.read_text()
    assert "OTHER=1\n" in text and text.count("DOOR_RELAY_UPSTREAM_HOST=") == 1
    assert 'DOOR_RELAY_UPSTREAM_PASSWORD="c@m p$$ss:1 #x"\n' in text
    s = dr.RelaySettings(str(env))
    assert s.password == PASSWORD and s.host == "192.0.2.20"


@pytest.mark.parametrize("blob, words", [
    (records().replace(b"username=admin\0", b""), "missing"),
    (records() + b"host=192.0.2.21\0", "duplicate"),
    (records() + b"shell=1\0", "unexpected"),
    (records(password="a\nb"), "control"),
    (records(host="cam.lan"), "IPv4"),
])
def test_tc1382_bad_records_are_refused_and_the_file_is_untouched(tmp_path, blob, words):
    env = tmp_path / ".env"
    env.write_text("LAN_IP=192.0.2.10\n")
    with pytest.raises((dr.Invalid, UnicodeDecodeError)) as err:
        dr.import_upstream(str(env), blob)
    assert words in str(err.value)
    assert env.read_text() == "LAN_IP=192.0.2.10\n"
    assert PASSWORD not in str(err.value)


# ── TC-1383 the pin, the compose image and the knob agree ────────────────────


def pin():
    out = {}
    for line in (STACK / "door" / "door-relay-image.pin").read_text().splitlines():
        m = re.match(r"([A-Z0-9_]+)=(\S+)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def env_example(key):
    m = re.search(r"^%s=(.*)$" % key, (STACK / ".env.example").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def test_tc1383_pin_has_version_and_index_digest_and_the_knob_is_built_from_it():
    p = pin()
    assert p["IMAGE"] == "alexxit/go2rtc" and re.fullmatch(r"\d+\.\d+\.\d+", p["VERSION"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", p["DIGEST"])
    assert env_example("DOOR_RELAY_IMAGE") == "%s:%s@%s" % (p["IMAGE"], p["VERSION"], p["DIGEST"])
    assert dr.image_ref() == env_example("DOOR_RELAY_IMAGE")


# ── TC-1377 / TC-1378 units, compose and firstboot ───────────────────────────


def compose_block(name):
    text = (STACK / "docker-compose.yml").read_text(encoding="utf-8")
    start = text.index("\n  %s:\n" % name)
    end = re.search(r"\n  [a-z0-9-]+:\n|\n[a-z]+:\n", text[start + 3:]).start() + start + 3
    return text[start:end]


def test_tc1378_the_relay_service_is_systemd_owned_pinned_and_hostnet():
    block = compose_block("door-relay")
    assert 'image: ${DOOR_RELAY_IMAGE}' in block
    assert 'profiles: ["door-relay"]' in block and 'restart: "no"' in block
    assert "network_mode: host" in block and "ports:" not in block
    assert "create_host_path: false" in block and "read_only: true" in block
    assert "cap_drop:\n      - ALL" in block
    profiles = env_example("COMPOSE_PROFILES") or ""
    assert "door-relay" not in profiles.split(",")


def unit(name):
    return (STACK / "door" / name).read_text(encoding="utf-8")


def test_tc1377_the_recorder_cannot_start_ahead_of_the_mount_or_outlive_it():
    u = unit("homehub-door-recorder.service")
    for line in ("RequiresMountsFor=/srv/scratch", "BindsTo=srv-scratch.mount",
                 "CapabilityBoundingSet=~CAP_SYS_RESOURCE", "RestartPreventExitStatus=75 76",
                 "ExecStartPre=/usr/bin/python3 /opt/homehub/stack/scratch/homehub_scratch.py guard --writer door",
                 "WantedBy=multi-user.target srv-scratch.mount"):
        assert line in u.splitlines(), line
    assert "After=srv-scratch.mount" in u
    # The recorder's container has no restart policy of its own and mounts nothing.
    argv = rec.ffmpeg_argv("/run/x.env")
    assert "--restart" not in argv and "-v" not in argv and "--volume" not in argv
    assert "--rm" in argv and "--read-only" in argv and "ALL" in argv
    assert not any("rtsp://" in a for a in argv)  # the URL is in the env file, not argv


def test_tc1377_neither_unit_requires_docker_and_both_have_their_knob_condition():
    for name, knob in (("homehub-door-relay.service", "DOOR_RELAY_ENABLED"),
                       ("homehub-door-recorder.service", "DOOR_RECORDER_ENABLED")):
        live = [l for l in unit(name).splitlines() if not l.startswith("#")]
        assert not [l for l in live if l.startswith(("Requires=", "PartOf=")) and "docker" in l]
        u = "\n".join(live)
        assert "condition %s" % knob in u


def test_tc1379_firstboot_makes_scratch_after_the_docker_lv_and_installs_the_units():
    fb = (STACK / "autoinstall" / "firstboot.sh").read_text(encoding="utf-8")
    assert fb.index("\nsetup_docker_lv\n") < fb.index("homehub_scratch.py\" --env-file") < fb.index("# ── 1c.")
    assert 'if [ "$__scratch_on" = true ]' in fb
    assert "systemctl start --no-block homehub-door-relay.service homehub-door-recorder.service" in fb
    for name in ("homehub-door-relay.service", "homehub-door-recorder.service"):
        assert "systemctl enable %s" % name in fb


def test_tc1397_the_image_bake_follows_the_relay_knob():
    text = (ROOT / "vmtest" / "export-images.sh").read_text(encoding="utf-8")
    assert "DOOR_RELAY_ENABLED" in text and "--profile door-relay" in text


# ── TC-1384 measurement helpers carry no secret ──────────────────────────────


def test_tc1384_redact_and_the_upstream_count():
    assert dr.redact("x rtsp://admin:p%40ss@192.0.2.20:554/H.264 y") == "x rtsp://***@192.0.2.20:554/H.264 y"
    ss = ("0 0 192.0.2.10:40000 192.0.2.20:554\n"
          "0 0 192.0.2.10:40002 192.0.2.30:554\n"
          "0 0 192.0.2.10:8554 192.0.2.17:51000\n")
    assert dr.count_established(ss, "192.0.2.20", 554) == 1
    assert dr.count_established(ss, "192.0.2.20", 8554) == 0


# ── the recorder: synthetic MPEG-TS ──────────────────────────────────────────

sys.path.insert(0, str(STACK / "door" / "tests"))
from ts_fixture import PMT_PID, VIDEO_PID, body, pat, pes, pmt, stream  # noqa: E402


def test_tc1393_packet_fields_pat_pmt_and_pts():
    assert rec.parse_pat(pat()) == PMT_PID
    assert rec.parse_pmt(pmt()) == VIDEO_PID
    assert rec.parse_pmt(pmt(stream_type=0x0F)) is None  # AAC: not video
    pid, pusi, rai = rec.packet_fields(pes(0, True))
    assert (pid, pusi, rai) == (VIDEO_PID, True, True)
    assert rec.packet_fields(pes(0, False))[2] is False
    assert rec.pes_pts(pes(123456, True)) == 123456


def test_tc1393_the_packetiser_realigns_after_junk_and_torn_reads():
    data = b"\x00junk" + b"".join(stream(3))
    pk = rec.Packetiser()
    got = []
    for i in range(0, len(data), 77):  # arbitrary pipe read sizes
        got += list(pk.feed(data[i:i + 77]))
    assert got == stream(3) and pk.dropped == 5


def test_tc1393_segments_start_on_a_keyframe_with_pat_pmt_and_cut_on_the_clock():
    # 7 s, so the boundary (packet 70) falls mid-GOP, two packets after a
    # NON-key PES: a cut on any PES would land there, a correct one waits.
    split = rec.Splitter(segment_sec=7)
    actions = []
    t = 994.0  # a multiple of 7: boundaries are on the clock, so the next is t + 7
    pkts = [body(), body()] + stream(40)  # junk before the first PAT is dropped
    for n, pkt in enumerate(pkts):
        now = t + n * 0.1
        action, header = split.feed(pkt, now)
        actions.append(action)
        if action == "start":
            assert header == pat() + pmt()
            assert rec.packet_fields(pkt)[2]  # a keyframe
    starts = [i for i, a in enumerate(actions) if a == "start"]
    assert actions[0] == actions[1] == "drop"
    assert len(starts) >= 2
    # the second cut is the first keyframe at/after the t + 7 boundary: GOP 3
    assert t + starts[1] * 0.1 >= t + 7
    assert starts[1] == 2 + 3 * 32 + 2  # 2 junk, three 32-packet GOPs, then PAT+PMT


def test_tc1394_pruning_removes_whole_oldest_segments_only(tmp_path):
    for day, names in (("2026-10-21", ["door-20261021T230000Z.ts", "door-20261021T231000Z.ts"]),
                       ("2026-10-22", ["door-20261022T000000Z.ts"])):
        (tmp_path / day).mkdir()
        for n in names:
            (tmp_path / day / n).write_bytes(b"x" * 100)
    (tmp_path / "2026-10-22" / "door-20261022T001000Z.ts.part").write_bytes(b"x" * 100)
    removed = rec.prune(str(tmp_path), cap_bytes=200, headroom=50, current=0)
    assert [os.path.basename(p) for p in removed] == ["door-20261021T230000Z.ts", "door-20261021T231000Z.ts"]
    assert not (tmp_path / "2026-10-21").exists()
    assert (tmp_path / "2026-10-22" / "door-20261022T001000Z.ts.part").exists()
    assert rec.prune(str(tmp_path), cap_bytes=10 ** 6, headroom=0) == []


class FakeStream:
    """A readable fd over bytes for Recorder.consume. A regular file, not a pipe:
    select() reports it ready, os.read() ends with b"" (the stream ended), and
    nothing blocks on a pipe buffer the way a 100 KB os.write() into a pipe would."""

    def __init__(self, data):
        import tempfile
        self.file = tempfile.TemporaryFile()
        self.file.write(data)
        self.file.seek(0)

    def fileno(self):
        return self.file.fileno()


needs_select_on_pipes = pytest.mark.skipif(sys.platform.startswith("win"), reason="select() on pipes is POSIX")


def make_recorder(tmp_path, guard_results, alerts, **knobs):
    settings = rec.RecorderSettings(dict({"DOOR_RECORDER_SEGMENT_SEC": "10"}, **knobs))
    r = rec.Recorder("/nonexistent", str(tmp_path / "door"), settings, None,
                     alert=lambda *a: alerts.append(a), clock=iter([]).__next__)
    results = iter(guard_results)
    r.guard = lambda: next(results)
    return r


@needs_select_on_pipes
def test_tc1395_the_guard_runs_before_every_segment_and_a_refusal_stops_before_opening(tmp_path):
    alerts = []
    r = make_recorder(tmp_path, [(True, "ok"), (False, "/srv/scratch is not a mount point")], alerts)
    clock = iter([1000.0 + i * 20.0 for i in range(10000)])  # 20 s per 64 KiB read
    r.clock = lambda: next(clock)
    data = b"".join(stream(200))  # 20 s of frames -> a second segment is due
    with pytest.raises(SystemExit) as code:
        r.consume(FakeStream(data))
    assert code.value.code == rec.EXIT_GUARD
    files = sorted(p.name for p in (tmp_path / "door").rglob("door-*"))
    assert len(files) == 1 and files[0].endswith(".ts")  # the first closed; no second opened
    assert alerts and alerts[0][0] == "door-recorder-guard"


class QuotaFull:
    """A segment file whose next write hits EDQUOT; everything else is real."""

    def __init__(self, real):
        self.real = real

    def write(self, data):
        raise OSError(errno.EDQUOT, "Disk quota exceeded")

    def __getattr__(self, name):
        return getattr(self.real, name)


@pytest.mark.skipif(not hasattr(errno, "EDQUOT"), reason="EDQUOT is POSIX")
def test_tc1395_a_full_quota_stops_cleanly_writes_nothing_else_and_alerts(tmp_path):
    alerts = []
    r = make_recorder(tmp_path, [(True, "ok")], alerts)
    r.open_segment(1000.0)
    r.handle = QuotaFull(r.handle)
    with pytest.raises(SystemExit) as code:
        r.write(b"G" * 188)
    assert code.value.code == rec.EXIT_QUOTA
    assert [a[0] for a in alerts] == ["door-recorder-quota"]
    # The segment holding no whole frame is dropped; nothing was written anywhere else.
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []
    assert r.handle is None


@needs_select_on_pipes
def test_tc1395_an_interrupted_segment_is_cut_back_to_its_last_whole_frame(tmp_path):
    alerts = []
    r = make_recorder(tmp_path, [(True, "ok")] * 5, alerts)
    r.clock = lambda: 1000.0
    data = b"".join(stream(15)) + b"".join([pes(99 * 9000, False), body()])  # a frame cut short
    assert r.consume(FakeStream(data)) == 1  # EOF: the stream ended
    r.finalise()
    (seg,) = list((tmp_path / "door").rglob("door-*.ts"))
    raw = seg.read_bytes()
    assert len(raw) % 188 == 0
    last_pes = max(i for i in range(0, len(raw), 188)
                   if rec.packet_fields(raw[i:i + 188])[1] and rec.packet_fields(raw[i:i + 188])[0] == VIDEO_PID)
    # the file ends with the body of the last COMPLETE frame (2 body packets after its PES)
    assert len(raw) - last_pes == 3 * 188


def test_tc1396_continuity_counts_gaps_over_tolerance():
    seg = [(0.0, 600.0), (600.5, 600.0), (1230.0, 570.0), (1800.0, 600.0)]
    c = rec.continuity(seg)
    assert c["gaps"] == 1 and c["downtime_seconds"] == 29.5 and c["pass"]
    many = [(i * 700.0, 600.0) for i in range(6)]  # five 100 s gaps
    c = rec.continuity(many)
    assert c["gaps"] == 5 and not c["pass"]
    long_gap = [(0.0, 600.0), (1000.0, 600.0)]
    assert not rec.continuity(long_gap)["pass"]  # 400 s > 5 min


def test_tc1396_segment_names_are_utc_and_parse_back():
    day, name = rec.segment_name(1792667254.0)
    assert day == "2026-10-22" and name == "door-20261022T110734Z.ts"
    assert rec.segment_start(name) == 1792667254.0
    assert rec.segment_start("door-x.ts") is None


def test_tc1396_ts_duration_from_pts(tmp_path):
    f = tmp_path / "s.ts"
    f.write_bytes(b"".join(stream(101)))  # 10 fps, frames 0..100 -> 10 s
    assert rec.ts_duration(str(f)) == pytest.approx(10.0)
