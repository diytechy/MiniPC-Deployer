"""The bounded training capture of a camera's streams (SR-162, LLR-1519..1523).

What runs here, on the dev PC: the capture's settings and window, the relay's
optional main stream, the names and the per-stream prune, the guard's verdicts
with the drive facts faked, the recorder's segment opening and its relabelled
log, the copy-only consumer argv (no credential, no audio), `ensure` with the
filesystem calls faked, and the unit contract.

What does not: the real streams onto the real drive. That is the deploy on the
hub (`camera_capture.py ensure`, the units, segments decoded with
`door_recorder.py verify`), recorded in HomeHub FOLLOWUPS.
Implements: TC-1757..TC-1763.
"""

from __future__ import annotations

import calendar
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
for sub in ("camera-capture", "camera-relay", "door", "clips", "scratch", "monitoring"):
    sys.path.insert(0, str(STACK / sub))
import camera_capture as cc  # noqa: E402
import camera_relay as cr  # noqa: E402
import door_recorder as drec  # noqa: E402

PASSWORD = "ABCDEF"
UNTIL = "2026-11-13T07:00:00Z"
UNTIL_EPOCH = float(calendar.timegm((2026, 11, 13, 7, 0, 0)))


def env(**over):
    base = {"CAMERA_LIVINGROOM_CAPTURE_ENABLED": "true", "CAMERA_LIVINGROOM_CAPTURE_UNTIL": UNTIL}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def write_env(tmp_path, **over):
    lines = {
        "LAN_IP": "192.0.2.10",
        "CAMERA_LIVINGROOM_RELAY_RTSP_PORT": "8564",
        "CAMERA_LIVINGROOM_RELAY_API_PORT": "1994",
        "CAMERA_LIVINGROOM_RELAY_UPSTREAM_HOST": "192.0.2.69",
        "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PATH": "/h264/ch1/sub/av_stream",
        "CAMERA_LIVINGROOM_RELAY_MAIN_PATH": "/h264/ch1/main/av_stream",
        "CAMERA_LIVINGROOM_RELAY_UPSTREAM_PASSWORD": PASSWORD,
        "CAMERA_LIVINGROOM_RELAY_AUDIO": "true",
        "CAMERA_LIVINGROOM_RELAY_EXPECT_SIZE": "768x432",
        "CAMERA_LIVINGROOM_CAPTURE_ENABLED": "true",
        "CAMERA_LIVINGROOM_CAPTURE_UNTIL": UNTIL,
    }
    lines.update(over)
    path = tmp_path / "env"
    path.write_text("".join("%s=%s\n" % kv for kv in lines.items() if kv[1] is not None))
    return str(path)


@pytest.fixture(autouse=True)
def no_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))


# ── TC-1757 settings, instance names and the window ─────────────────────────


def test_tc1757_settings_window_and_instances():
    s = cc.CaptureSettings(env(), "livingroom", "main")
    assert (s.until, s.streams, s.segment_sec) == (UNTIL_EPOCH, ["main", "sub"], 600)
    assert s.root == "/srv/media/Training/livingroom/main" and s.prefix == "livingroom-main"
    assert cc.window_state(s, UNTIL_EPOCH - 3600)[0] == "run"
    assert cc.window_state(s, UNTIL_EPOCH)[0] == "skip", "the end instant itself starts nothing"
    assert cc.window_state(cc.CaptureSettings(env(CAMERA_LIVINGROOM_CAPTURE_ENABLED="false"),
                                              "livingroom", "sub"), 0)[0] == "skip"
    assert cc.window_state(cc.CaptureSettings(env(CAMERA_LIVINGROOM_CAPTURE_STREAMS="sub"),
                                              "livingroom", "main"), 0)[0] == "skip"
    no_end = cc.CaptureSettings(env(CAMERA_LIVINGROOM_CAPTURE_UNTIL=None), "livingroom", "sub")
    assert cc.window_state(no_end, 0)[0] == "refuse", "an enabled capture must have an end"
    for bad in ({"CAMERA_LIVINGROOM_CAPTURE_UNTIL": "2026-11-13 07:00"},
                {"CAMERA_LIVINGROOM_CAPTURE_STREAMS": "main,hd"}):
        with pytest.raises(cc.Invalid):
            cc.CaptureSettings(env(**bad), "livingroom", "main")
    assert cc.split_instance("livingroom-main") == ("livingroom", "main")
    for bad in ("livingroom", "livingroom-hd", "door-main", "-main"):
        with pytest.raises(cc.Invalid):
            cc.split_instance(bad)


# ── TC-1758 the relay's optional main stream ─────────────────────────────────


def test_tc1758_the_relay_serves_the_main_stream_video_only(tmp_path):
    s = cr.CameraRelaySettings(write_env(tmp_path), "livingroom")
    cfg = cr.relay_config(s)
    assert set(cfg["streams"]) == {"livingroom", "livingroom-main"}
    main = cfg["streams"]["livingroom-main"]
    assert main.endswith("/h264/ch1/main/av_stream#media=video"), "never audio on the main stream"
    assert cfg["streams"]["livingroom"].endswith("/h264/ch1/sub/av_stream"), "the sub stream unchanged"
    m = s.for_main()
    assert m.stream == "livingroom-main" and s.stream == "livingroom", "a copy, not a mutation"
    assert m.consumer_url().endswith(":8564/livingroom-main")
    unset = cr.CameraRelaySettings(write_env(tmp_path, CAMERA_LIVINGROOM_RELAY_MAIN_PATH=None), "livingroom")
    assert set(cr.relay_config(unset)["streams"]) == {"livingroom"}, "unset: the relay is as before"
    with pytest.raises(cr.Invalid):
        unset.for_main()
    for bad in ("h264/main", "/main stream", "/h264/ch1/sub/av_stream"):
        with pytest.raises(cr.Invalid):
            cr.CameraRelaySettings(write_env(tmp_path, CAMERA_LIVINGROOM_RELAY_MAIN_PATH=bad), "livingroom")


def test_tc1758_the_capture_consumer_is_copy_only_video_and_holds_no_credential(tmp_path):
    envf = write_env(tmp_path)
    for stream, path in (("main", "/livingroom-main"), ("sub", "/livingroom")):
        s = cc.CaptureSettings(cc.mc.read_knobs(envf), "livingroom", stream)
        relay = cc.relay_for(envf, s)
        argv, payload = cc.ffmpeg_command(relay, cc.container(s))
        joined = " ".join(argv)
        assert PASSWORD not in joined and "192.0.2.69" not in joined and "rtsp://" not in joined
        assert argv[argv.index("-map") + 1] == "0:v:0" and "-an" in argv
        assert argv[argv.index("-c") + 1] == "copy"
        assert payload.decode().count(path + "'") == 1 and PASSWORD in payload.decode(), "the URL on stdin only"
        assert cc.container(s) == "homehub-camera-capture-livingroom-" + stream


# ── TC-1759 names and the per-stream prune ───────────────────────────────────


def test_tc1759_names_and_prune_touch_only_this_stream(tmp_path):
    now = float(calendar.timegm((2026, 11, 10, 6, 30, 0)))
    day, name = cc.segment_name(now, "livingroom-main")
    assert (day, name) == ("2026-11-10", "livingroom-main-20261110T063000Z.ts")
    assert cc.segment_start(name, "livingroom-main") == now
    assert cc.segment_start(name + ".part", "livingroom-main") == now
    assert cc.segment_start("door-20261110T063000Z.ts", "livingroom-main") is None
    root = tmp_path / "main"
    (root / "2026-11-10").mkdir(parents=True)
    for stamp, size in (("060000", 10), ("061000", 10), ("062000", 10)):
        (root / "2026-11-10" / ("livingroom-main-20261110T%sZ.ts" % stamp)).write_bytes(b"x" * size)
    foreign = root / "2026-11-10" / "door-20261110T050000Z.ts"
    foreign.write_bytes(b"x" * 100)
    assert [os.path.basename(p) for p, _ in cc.list_segments(str(root), "livingroom-main")] == [
        "livingroom-main-20261110T060000Z.ts", "livingroom-main-20261110T061000Z.ts",
        "livingroom-main-20261110T062000Z.ts"]
    removed = cc.prune(str(root), "livingroom-main", cap_bytes=25, headroom=0)
    assert [os.path.basename(p) for p in removed] == ["livingroom-main-20261110T060000Z.ts"]
    assert foreign.exists(), "nothing but this stream's own segments is ever pruned"


# ── TC-1760 the guard ────────────────────────────────────────────────────────


@pytest.mark.skipif(not hasattr(os, "makedev"), reason="the guard compares device numbers (Linux)")
def test_tc1760_the_guard_refuses_each_unsafe_fact(tmp_path, monkeypatch):  # pragma: no cover - Linux
    root = tmp_path / "main"
    root.mkdir()
    dev = os.stat(str(root)).st_dev
    entry = {"majmin": "%d:%d" % (os.major(dev), os.minor(dev)), "source": "/dev/sdx1"}
    facts = {"mount": (True, "ok", entry), "cap": False, "project": (cc.TRAINING_PROJECT, True), "write": None}
    monkeypatch.setattr(cc.clips, "media_mount_state", lambda _p: facts["mount"])
    monkeypatch.setattr(cc.scratch, "holds_cap_sys_resource", lambda _s: facts["cap"])
    monkeypatch.setattr(cc.scratch, "get_project", lambda _p: facts["project"])
    monkeypatch.setattr(cc.clips, "media_write_error", lambda _r: facts["write"])
    paths = cc.clips.MediaPaths()
    assert cc.training_guard(str(root), paths) == (True, "ok")
    for key, value, words in (("mount", (False, "/srv/media is not a mount point", None), "not a mount point"),
                              ("cap", True, "CAP_SYS_RESOURCE"),
                              ("project", (7105, True), "not 7106"),
                              ("project", (cc.TRAINING_PROJECT, False), "inherit False"),
                              ("write", "EROFS", "not writable")):
        saved = facts[key]
        facts[key] = value
        ok, reason = cc.training_guard(str(root), paths)
        assert not ok and words in reason, (key, reason)
        facts[key] = saved
    assert not cc.training_guard(str(tmp_path / "absent"), paths)[0]


# ── TC-1761 the recorder: names, guard first, relabelled log, alerts ─────────


def test_tc1761_the_recorder_opens_this_streams_segments_after_the_guard(tmp_path, monkeypatch):
    s = cc.CaptureSettings(env(), "livingroom", "sub")
    s.root = str(tmp_path / "sub")
    os.makedirs(s.root)
    verdicts = [(True, "ok")]
    alerts = []
    monkeypatch.setattr(cc, "training_guard", lambda _r, _p=None: verdicts[-1])
    rec = cc.CaptureRecorder("/nonexistent/env", s, alert=lambda *a: alerts.append(a))
    now = float(calendar.timegm((2026, 11, 10, 6, 30, 0)))
    rec.open_segment(now)
    assert rec.part.endswith(os.path.join("2026-11-10", "livingroom-sub-20261110T063000Z.ts.part"))
    rec.write(b"\x47" * 188)
    rec.finalise(at_keyframe=True)
    assert os.path.exists(os.path.join(s.root, "2026-11-10", "livingroom-sub-20261110T063000Z.ts"))
    rec.open_segment(now)  # a fast restart in the same second never overwrites
    assert rec.part.endswith("livingroom-sub-20261110T063000Z-1.ts.part")
    rec.finalise(at_keyframe=True)
    verdicts.append((False, "/srv/media is not a mount point"))
    with pytest.raises(SystemExit) as stop:
        rec.open_segment(now + 600)
    assert stop.value.code == drec.EXIT_GUARD and alerts and alerts[-1][0] == "guard"
    assert not os.path.exists(os.path.join(s.root, "2026-11-10", "livingroom-sub-20261110T064000Z.ts.part"))


def test_tc1761_log_lines_and_alerts_say_capture_not_door(monkeypatch):
    lines = []

    class Base:
        DEFAULT_STATE_DIR = "/x"

        @staticmethod
        def log(message, warning=False):
            lines.append(message)
    relabelled = cc._Relabelled(Base, "livingroom-main capture")
    relabelled.log("door recorder: closed /srv/media/Training/livingroom/main/a.ts (1.0 MiB)")
    relabelled.log("unrelated line")
    assert lines == ["livingroom-main capture: closed /srv/media/Training/livingroom/main/a.ts (1.0 MiB)",
                     "unrelated line"]
    assert relabelled.DEFAULT_STATE_DIR == "/x"
    sent = []
    monkeypatch.setattr(cc.mc, "send_alert", lambda key, sev, title, body, *a, **k: sent.append((key, title, body)))
    s = cc.CaptureSettings(env(), "livingroom", "main")
    rec = cc.CaptureRecorder("/nonexistent/env", s)
    rec.alert("door-recorder-quota", "HomeHub: door recorder stopped (EDQUOT)",
              "door/ on the scratch volume is full; the recorder will not restart by itself.")
    key, title, body = sent[-1]
    assert key == "camera-capture-livingroom-main-quota"
    assert "door" not in title + body and "/srv/media/Training/livingroom" in body
    assert drec.mc is not relabelled, "nothing is relabelled at import: only run_service installs it"


# ── TC-1762 the window ends a run cleanly; a closed window starts nothing ────


def test_tc1762_a_start_after_the_window_touches_nothing(tmp_path, monkeypatch):
    envf = write_env(tmp_path)
    called = []
    monkeypatch.setattr(cc, "training_guard", lambda *a: called.append("guard") or (True, "ok"))
    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: called.append("docker"))
    assert cc.run_service(envf, "livingroom", "main", clock=lambda: UNTIL_EPOCH + 1) == 0
    assert called == [], "after the window: no guard, no docker, no file"
    envf2 = write_env(tmp_path, CAMERA_LIVINGROOM_CAPTURE_UNTIL=None)
    assert cc.run_service(envf2, "livingroom", "main", clock=lambda: 0.0) == cc.EXIT_REFUSED
    assert called == []


# ── TC-1763 ensure and the unit ──────────────────────────────────────────────


@pytest.mark.skipif(not hasattr(os, "getxattr"), reason="ensure's ACL handling needs Linux xattrs")
def test_tc1763_ensure_makes_a_private_tree_in_its_project(tmp_path, monkeypatch):  # pragma: no cover - Linux
    mount = tmp_path / "media"
    mount.mkdir()
    dev = os.stat(str(mount)).st_dev
    entry = {"majmin": "%d:%d" % (os.major(dev), os.minor(dev)), "source": "/dev/sdx1"}
    monkeypatch.setattr(cc.clips, "MEDIA_MOUNT", str(mount))
    monkeypatch.setattr(cc, "TRAINING_ROOT", str(mount / "Training"))
    monkeypatch.setattr(cc.clips, "media_mount_state", lambda _p: (True, "ok", entry))
    projects, quotas = {}, {}
    monkeypatch.setattr(cc.scratch, "get_project", lambda p: projects.get(p, (0, False)))
    monkeypatch.setattr(cc.scratch, "set_project", lambda p, i: projects.__setitem__(p, (i, True)))
    monkeypatch.setattr(cc.scratch, "get_project_quota", lambda d, i: (0, quotas.get(i, 0)))
    monkeypatch.setattr(cc.scratch, "set_project_quota", lambda d, i, m: quotas.__setitem__(i, m * cc.MIB))
    monkeypatch.setattr(os, "chown", lambda *a: None)
    envf = write_env(tmp_path)
    first = cc.ensure(envf, "livingroom", act_log=lambda t: None)
    cam = str(mount / "Training" / "livingroom")
    assert projects[cam] == (cc.TRAINING_PROJECT, True) and quotas[cc.TRAINING_PROJECT] == 204800 * cc.MIB
    for d in (mount / "Training", mount / "Training" / "livingroom", mount / "Training" / "livingroom" / "main"):
        assert oct(os.stat(str(d)).st_mode & 0o7777) == oct(0o750)
    assert first["actions"]
    assert cc.ensure(envf, "livingroom", act_log=lambda t: None)["actions"] == [], "idempotent"


def test_tc1763_the_unit_drops_cap_sys_resource_and_binds_to_the_drive():
    unit = (STACK / "camera-capture" / "homehub-camera-capture@.service").read_text(encoding="utf-8")
    for line in ("CapabilityBoundingSet=~CAP_SYS_RESOURCE", "BindsTo=srv-media.mount",
                 "RequiresMountsFor=/srv/media", "RestartPreventExitStatus=2 75 76", "Restart=on-failure",
                 "--instance %i condition", "--instance %i run",
                 "ExecStopPost=-/usr/bin/docker rm -f homehub-camera-capture-%i",
                 "WantedBy=multi-user.target srv-media.mount"):
        assert line in unit, line
    assert cc.TRAINING_PROJECT != cc.clips.MEDIA_PROJECT
