"""Motion clips: the event contract, the cutter, the encode batch, retention (SR-128..SR-131).

What runs here, on the dev PC: the IF-076 record and store; episodes (the
panel's display window), the frame index and its timing (PTS, or index when
the camera's PTS is broken), the cut plan (keyframe snap, end, segments,
gaps), readiness and loss; names and the IF-077 sidecar and index; the cut,
the encode and the probe with ffmpeg replaced by fakes that check the bytes
they are handed; the ledger; retention and the quota stop; the scratch
volume's new `clips` project; the units, firstboot block and template.

What does not: real ffmpeg and VA-API. The integration test at the bottom
(TC-1569, release tier) cuts real recorded segments through the pinned image
when HOMEHUB_CLIPS_SEGMENTS names a directory of them and docker answers;
VA-API is measured on the hub (stack/clips/README.md, deploy steps).
Implements: TC-1541, TC-1542, TC-1551..TC-1566, TC-1568, TC-1569.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
for sub in ("clips", "door", "scratch", "monitoring", "door/tests"):
    sys.path.insert(0, str(STACK / sub))
import clip_events as ev  # noqa: E402
import homehub_clips as hc  # noqa: E402
import homehub_scratch as hs  # noqa: E402
from ts_fixture import body, pat, pes, pmt, stream  # noqa: E402

MIB = 1024 * 1024
T0 = 1792439400000  # 2026-10-19T19:50:00Z


@pytest.fixture(autouse=True)
def no_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))
    # Every test runs on a machine without the clips area; the guard is stubbed open
    # here and tests of the guard itself replace this stub or call its parts directly.
    monkeypatch.setattr(hc, "guard", lambda settings: (True, "ok"))


def rec(i, cause, kind, ms, source="hub-motion", camera="door"):
    return ev.make(camera, source, "r.%d" % i, cause, kind, ms)


# ── TC-1541 / TC-1542 the IF-076 record and store ────────────────────────────


def test_tc1541_a_valid_record_and_every_way_to_break_one():
    good = rec(1, "approach", "event", T0)
    assert ev.invalid(good) is None
    for key, value, words in (("v", 2, "v is"), ("v", True, "v is"), ("camera", "Door", "camera"),
                              ("source", "camera", "source"), ("id", "a b", "id"), ("cause", "", "cause"),
                              ("kind", "start", "kind"), ("utc_ms", 1.5, "utc_ms"), ("utc_ms", 5, "utc_ms"),
                              ("utc_ms", True, "utc_ms")):
        bad = dict(good, **{key: value})
        assert words in ev.invalid(bad)
    assert "unexpected" in ev.invalid(dict(good, image="..."))
    assert ev.invalid([good]) == "not an object"
    with pytest.raises(ValueError):
        ev.make("door", "hub-motion", "x", "approach", "event", 3)


def test_tc1542_the_store_is_per_day_deduped_and_skips_what_is_not_a_record(tmp_path):
    state = str(tmp_path)
    day1 = T0 - 20 * 3600 * 1000  # the previous UTC day
    for r in (rec(1, "approach", "event", day1), rec(2, "visitor", "renewal", T0), rec(3, "road-stop", "event", T0 + 5)):
        assert ev.append(state, r)[0]
    assert ev.append(state, rec(2, "visitor", "renewal", T0))[0]  # the same record twice
    assert not ev.append(state, dict(rec(4, "approach", "event", T0), kind="start"))[0]
    names = sorted(os.listdir(tmp_path / "events" / "door"))
    assert names == ["hub-motion-2026-10-18.jsonl", "hub-motion-2026-10-19.jsonl"]
    with open(tmp_path / "events" / "door" / names[1], "a") as handle:
        handle.write('{"torn": \n')
        handle.write(json.dumps(rec(9, "approach", "event", T0, camera="garage")) + "\n")
    got, rejected = ev.read(state, "door", ["hub-motion"], day1)
    assert [r["id"] for r in got] == ["r.1", "r.2", "r.3"] and rejected == 1
    assert ev.read(state, "door", ["onvif"], day1) == ([], 0)
    assert [r["id"] for r in ev.read(state, "door", ["hub-motion"], T0)[0]] == ["r.2", "r.3"]
    removed = ev.prune_days(state, ["door"], 1, T0 / 1000.0 + 86400)
    assert [os.path.basename(p) for p in removed] == ["hub-motion-2026-10-18.jsonl"]


# ── TC-1551 settings ─────────────────────────────────────────────────────────


def test_tc1551_the_clips_root_is_one_of_two_and_camera_knobs_are_checked():
    s = hc.ClipsSettings({})
    assert (s.root, s.enabled, s.encode, s.retention_days, s.cap_mib) == ("/srv/scratch/clips", False, False, 365, 95367)
    cam = s.cameras[0]
    assert (cam.camera, cam.sources, cam.pre_ms, cam.lease_ms, cam.max_ms, cam.causes, cam.segment_dir) == (
        "door", ["hub-motion"], 3000, 5000, 600000, ["approach", "road-stop"], "/srv/scratch/door")
    assert hc.ClipsSettings({"CLIPS_ROOT": "/srv/media/Clips/"}).root == "/srv/media/Clips"
    for env, key in (({"CLIPS_ROOT": "/srv/library/x"}, "CLIPS_ROOT"), ({"CAMERAS": "Door"}, "CAMERAS"),
                     ({"CAMERA_DOOR_SOURCES": "rtsp"}, "CAMERA_DOOR_SOURCES"),
                     ({"CLIPS_ENCODE_CODEC": "av1"}, "CLIPS_ENCODE_CODEC"),
                     ({"CAMERA_DOOR_SEGMENT_DIR": "srv/x"}, "CAMERA_DOOR_SEGMENT_DIR")):
        with pytest.raises(hc.Invalid, match=key):
            hc.ClipsSettings(env)


# ── TC-1552 / TC-1553 the frame index and its timing ─────────────────────────


def ts_bytes(frames, start_pts=900000, fps=10, gop=10, broken=False, junk=b""):
    pkts = stream(frames, gop=gop, fps=fps, start_pts=start_pts)
    if broken:  # the camera's dawn mode: every PTS 1 tick apart
        n = 0
        out = []
        for p in pkts:
            if p[1] & 0x40 and ((p[1] & 0x1F) << 8 | p[2]) == 0x100:
                out.append(pes(start_pts + n, (n % gop) == 0))
                n += 1
            else:
                out.append(p)
        pkts = out
    return junk + b"".join(pkts)


def test_tc1552_the_index_finds_every_frame_keyframe_and_header():
    data = ts_bytes(30)
    idx = hc.scan_frames(data, part=False)
    assert len(idx.offsets) == 30 and idx.keys == [i % 10 == 0 for i in range(30)]
    assert idx.header == pat() + pmt() and idx.end == len(data)
    assert idx.pts[:2] == [900000, 909000]
    assert all(data[o] == 0x47 for o in idx.offsets)
    # an open .part: the frame being written is dropped, `end` is its start
    part = hc.scan_frames(data[:-188], part=True)
    assert len(part.offsets) == 29 and part.end == idx.offsets[29]
    # a torn start is skipped to the next packet
    torn = hc.scan_frames(b"\x00\x01\x02" + data, part=False)
    assert len(torn.offsets) == 30


def test_tc1553_pts_timing_and_the_index_fallback_for_broken_pts():
    good = hc.scan_frames(ts_bytes(50), False)
    times, mode = hc.frame_times(good, 1000.0, 10)
    assert mode == "pts" and times[0] == 1000.0 and abs(times[49] - 1004.9) < 1e-6
    broken = hc.scan_frames(ts_bytes(50, broken=True), False)
    times, mode = hc.frame_times(broken, 1000.0, 10)
    assert mode == "index" and abs(times[49] - 1004.9) < 1e-6
    wrap = hc.scan_frames(ts_bytes(20, start_pts=(1 << 33) - 45000), False)
    times, mode = hc.frame_times(wrap, 0.0, 10)
    assert mode == "pts" and abs(times[19] - 1.9) < 1e-6
    assert hc.frame_times(hc.FrameIndex(), 0.0, 10) == ([], "none")


# ── TC-1554 episodes: the panel's display window ─────────────────────────────


def test_tc1554_episodes_follow_the_display_window():
    rs = [rec(1, "road-stop", "event", T0)]
    rs += [rec(10 + k, "road-hold", "renewal", T0 + s) for k, s in enumerate((3000, 6100, 9200, 12300, 15400, 18500, 21600, 24700))]
    rs += [rec(2, "approach", "event", T0 + 27000), rec(3, "visitor", "renewal", T0 + 30000)]
    rs += [rec(4, "visitor", "renewal", T0 + 90000)]                 # an orphan: the window had closed
    rs += [rec(5, "approach", "event", T0 + 120000)]
    rs += [rec(6, "doorbell", "event", T0 + 200000)]                 # a cause that does not start a clip
    eps, orphans = hc.build_episodes(rs, ["approach", "road-stop"], 3000, 5000, 600000)
    assert [e.cause for e in eps] == ["road-stop", "approach"] and orphans == 2
    first = eps[0]
    assert first.window == (T0 - 3000, T0 + 35000) and len(first.records) == 11
    assert eps[1].window == (T0 + 117000, T0 + 125000)


def test_tc1554_a_long_episode_continues_as_a_second_clip_without_pre_roll():
    rs = [rec(1, "approach", "event", T0)] + [rec(10 + k, "visitor", "renewal", T0 + 4000 * k) for k in range(1, 40)]
    eps, _ = hc.build_episodes(rs, ["approach"], 3000, 5000, 60000)
    assert [e.cause for e in eps] == ["approach", "continued", "continued"]
    assert eps[0].window == (T0 - 3000, T0 + 60000)
    assert eps[1].window == (T0 + 60000, T0 + 120000) and eps[1].pre_ms == 0
    assert eps[2].window == (T0 + 120000, T0 + 39 * 4000 + 5000)  # to the last renewal + lease


# ── TC-1555 / TC-1556 the plan and readiness ─────────────────────────────────


class Seg:
    def __init__(self, name, start, part=False):
        self.name, self.start, self.part, self.path = name, start, part, name


def loaded(start, frames, part=False, broken=False, start_pts=900000, extra=0):
    data = ts_bytes(frames + extra, start_pts=start_pts, broken=broken)
    return hc.Loaded(Seg("door-%d.ts" % start, start, part), data, 10)


def test_tc1555_the_cut_starts_on_the_last_keyframe_and_ends_before_the_window_end():
    a = loaded(1000.0, 100)
    plan = hc.plan_window([a], 1003.5, 1006.25, 10)
    assert plan.actual == (1003.0, pytest.approx(1006.3))
    assert plan.frames == 33 and plan.start_covered and plan.end_covered and plan.coverage == "full"
    (seg, frm, to, header), = plan.pieces
    assert frm == a.idx.offsets[30] and to == a.idx.offsets[63] and header
    assert not plan.regenerate and plan.timing == "pts"


def test_tc1555_across_segments_with_a_restart_regenerates_and_records_the_gap():
    a = loaded(1000.0, 100)                                          # 1000.0 .. 1010.0
    b = loaded(1013.0, 100, start_pts=5)                             # a recorder restart: new PTS line, 3 s gap
    plan = hc.plan_window([a, b], 1008.2, 1014.05, 10)
    assert [p[0] for p in plan.pieces] == [a, b]
    assert plan.pieces[1][1] == 0 and not plan.pieces[1][3]          # b from its first byte, its own PAT/PMT
    assert plan.regenerate and plan.gaps == [{"after_utc_ms": 1010000, "seconds": 3.0}]
    assert plan.coverage == "partial" and plan.frames == 20 + 11
    contiguous = loaded(1010.0, 100, start_pts=900000 + 100 * 9000)  # continuous PTS: kept
    plan = hc.plan_window([a, contiguous], 1008.2, 1011.0, 10)
    assert not plan.regenerate and not plan.gaps and plan.coverage == "full"


def test_tc1555_broken_pts_and_an_open_part_and_an_uncovered_end():
    dawn = loaded(1000.0, 100, broken=True)
    plan = hc.plan_window([dawn], 1002.0, 1004.0, 10)
    assert plan.timing == "index" and plan.regenerate and plan.frames == 20
    part = loaded(1000.0, 50, part=True, extra=1)                    # 50 whole frames + one being written
    plan = hc.plan_window([part], 1003.0, 1009.0, 10)
    assert not plan.end_covered and plan.pieces[0][2] == part.idx.end and plan.frames == 20
    assert hc.plan_window([part], 1100.0, 1110.0, 10) is None
    late = loaded(1005.0, 50)
    plan = hc.plan_window([late], 1002.0, 1007.0, 10)
    assert not plan.start_covered and plan.actual[0] == 1005.0 and plan.coverage == "partial"


def test_tc1556_readiness_waits_settles_and_names_a_loss():
    ep = hc.build_episodes([rec(1, "approach", "event", T0)], ["approach"], 3000, 5000, 600000)[0][0]
    we = T0 + 5000
    covered = hc.plan_window([loaded(T0 / 1000.0 - 10, 200)], (T0 - 3000) / 1000.0, we / 1000.0, 10)
    assert covered.end_covered
    assert hc.readiness(we + hc.SETTLE_MS - 1, ep, covered, T0 / 1000.0 - 100) == "wait"
    assert hc.readiness(we + hc.SETTLE_MS, ep, covered, T0 / 1000.0 - 100) == "ready"
    short = hc.plan_window([loaded(T0 / 1000.0 - 10, 120)], (T0 - 3000) / 1000.0, we / 1000.0, 10)
    assert not short.end_covered
    assert hc.readiness(we + hc.SETTLE_MS, ep, short, 0) == "wait"
    assert hc.readiness(we + hc.COVER_WAIT_MS, ep, short, 0) == "ready"
    assert hc.readiness(we + hc.SETTLE_MS, ep, None, T0 / 1000.0 + 60) == "lost: the recording no longer covers the window"
    assert hc.readiness(we + hc.SETTLE_MS, ep, None, T0 / 1000.0 - 100) == "wait"
    assert hc.readiness(we + hc.COVER_WAIT_MS, ep, None, T0 / 1000.0 - 100) == "lost: no recording covers the window"


# ── TC-1557 names ────────────────────────────────────────────────────────────


def test_tc1557_names_are_local_time_with_the_offset_across_dst():
    def zone(offset_h, is_dst):
        def lt(epoch):
            t = time.gmtime(epoch + offset_h * 3600)
            return time.struct_time(tuple(t[:8]) + (is_dst,))
        return lt
    # struct_time carries no tm_gmtoff on every platform: format the offset ourselves.
    day, stem = hc.clip_stem("door", T0 + 61000, "road-stop", 29.4, localtime=zone(-5, 1))
    assert day == "2026-10-19" and stem.startswith("door_2026-10-19_14-51-01") and stem.endswith("_road-stop_29s")
    real_day, real_stem = hc.clip_stem("door", T0, "approach", 45)
    assert re.fullmatch(r"door_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}[+-]\d{4}_approach_45s", real_stem), real_stem
    assert real_day == real_stem[5:15]  # the folder is the name's local date


# ── TC-1558 the guard for CLIPS_ROOT ─────────────────────────────────────────


def test_tc1558_the_scratch_root_is_the_clips_writer_with_its_own_project():
    assert hs.PROJECT_ID["clips"] == 7104
    facts = {"writer": "clips", "known_writer": True, "expected_uuid": "u", "uuid_malformed": False,
             "mounted": True, "mount_majmin": "253:2", "uuid_majmin": "253:2", "fstype": "ext4",
             "superopts": "rw,prjquota", "read_only": False, "has_cap_sys_resource": False,
             "dir_kind": "dir", "dir_dev_ok": True, "project_id": 7104, "project_inherit": True, "probe_error": None}
    assert hs.judge_guard(facts) == (True, "ok")
    assert not hs.judge_guard(dict(facts, project_id=7101))[0]
    assert not hs.judge_guard(dict(facts, has_cap_sys_resource=True))[0]


def test_tc1558_the_media_root_needs_a_separate_ext4_prjquota_mount(tmp_path, monkeypatch):
    paths = hs.Paths(str(tmp_path))
    paths.status = str(tmp_path / "status")
    (tmp_path / "status").write_text("CapBnd:\t0000000000000000\n")

    def mounts(line):
        paths.mountinfo = str(tmp_path / "mountinfo")
        (tmp_path / "mountinfo").write_text(line)
    mounts("25 1 8:1 / / rw - ext4 /dev/sda1 rw\n")
    assert hc.media_guard(hc.MEDIA_ROOT, paths) == (False, "/srv/media is not a mount point (the media drive is not mounted)")
    mounts("25 1 8:1 / / rw - ext4 /dev/sda1 rw\n40 25 8:33 / /srv/media rw - ntfs3 /dev/sdc1 rw\n")
    assert "not ext4 with prjquota" in hc.media_guard(hc.MEDIA_ROOT, paths)[1]
    mounts("40 25 8:33 / /srv/media ro - ext4 /dev/sdc1 ro,prjquota\n")
    assert "read-only" in hc.media_guard(hc.MEDIA_ROOT, paths)[1]
    mounts("40 25 8:33 / /srv/media rw - ext4 /dev/sdc1 rw,prjquota\n")
    assert "does not exist" in hc.media_guard(str(tmp_path / "nope"), paths)[1]


# ── TC-1559 the cut (ffmpeg replaced by a fake) ──────────────────────────────


class FakeFfmpeg:
    """Stands in for `docker run ... ffmpeg`: keeps what it is fed, writes the
    output file named last in argv (under the -v bind), exits as told."""

    def __init__(self, rc=0, stderr=b"", write=True):
        self.rc, self.stderr, self.write = rc, stderr, write
        self.argv = None
        self.fed = bytearray()

    def __call__(self, argv, stdin=None, stdout=None, stderr=None):
        self.argv = argv
        fake = self

        class Proc:
            returncode = fake.rc

            class _In:
                def write(self, b):
                    fake.fed.extend(bytes(b))

                def close(self):
                    pass
            stdin = _In()

            def communicate(self, timeout=None):
                if hasattr(stderr, "write"):  # the cutter hands ffmpeg a file, not a pipe
                    stderr.write(fake.stderr)
                if fake.write and fake.rc == 0:
                    bind = argv[argv.index("-v") + 1].split(":/out")[0]
                    with open(os.path.join(bind, argv[-1][len("/out/"):]), "wb") as handle:
                        handle.write(b"mp4:" + bytes(fake.fed[:64]))
                return b"", fake.stderr

            def kill(self):
                pass
        return Proc()


def cut_setup(tmp_path, monkeypatch, frames=200):
    root = tmp_path / "clips"
    root.mkdir()
    monkeypatch.setattr(hc, "SCRATCH_ROOT", str(root))
    monkeypatch.setattr(hc, "guard", lambda settings: (True, "ok"))
    monkeypatch.setattr(hc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    settings = hc.ClipsSettings({"CLIPS_ENABLED": "true"})
    settings.root = str(root)
    cam = settings.cameras[0]
    seg = loaded(T0 / 1000.0 - 10, frames)
    return settings, cam, seg, root


def test_tc1559_the_cut_feeds_exactly_the_planned_bytes_and_publishes_a_verified_clip(tmp_path, monkeypatch):
    settings, cam, seg, root = cut_setup(tmp_path, monkeypatch)
    rs = [rec(1, "approach", "event", T0), rec(2, "visitor", "renewal", T0 + 3000)]
    ep = hc.build_episodes(rs, cam.causes, cam.pre_ms, cam.lease_ms, cam.max_ms)[0][0]
    plan = hc.plan_window([seg], ep.window[0] / 1000.0, ep.window[1] / 1000.0, 10)
    fake = FakeFfmpeg()
    probe = lambda c, d, n: {"frames": plan.frames, "duration": 11.0, "codec": "h264"}  # noqa: E731
    result = hc.cut_episode(settings, cam, ep, plan, str(tmp_path / "state"), runner=fake, prober=probe,
                            localtime=time.gmtime)
    assert result == {"status": "cut", "clip": "2026-10-19/door_2026-10-19_19-50-00+0000_approach_11s.mp4"} or \
        result["status"] == "cut"
    _seg, frm, to, _h = plan.pieces[0]
    assert bytes(fake.fed) == seg.idx.header + seg.data[frm:to]
    argv = fake.argv
    assert argv[:3] == ["docker", "run", "--rm"] and "-i" in argv and "--network" in argv and "none" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL" and "--read-only" in argv
    assert argv[-1].endswith(".mp4.part") and "+faststart" in argv and argv[argv.index("-c") + 1] == "copy"
    assert "setts" not in " ".join(argv)
    day = root / "door" / result["clip"].split("/")[0]
    names = sorted(os.listdir(day))
    assert [n for n in names if n.endswith(".part")] == []
    stem = result["clip"].split("/")[1][:-4]
    side = json.loads((day / (stem + ".json")).read_text(encoding="utf-8"))
    assert side["v"] == 1 and side["camera"] == "door" and side["frames"] == plan.frames
    assert side["requested"] == {"start_utc_ms": T0 - 3000, "end_utc_ms": T0 + 8000}
    assert side["coverage"] == "full" and side["timing"] == "pts" and side["timestamps"] == "source"
    assert side["sha256"] == hc.sha256_file(str(day / (stem + ".mp4"))) and side["encode"] is None
    assert [e["id"] for e in side["events"]] == ["r.1", "r.2"] and side["sources"] == ["hub-motion"]
    index = [json.loads(line) for line in (root / "door" / "index.jsonl").read_text(encoding="utf-8").splitlines()]
    assert index[0]["clip"] == result["clip"] and index[0]["cause"] == "approach"


def test_tc1559_a_short_or_failed_cut_is_removed_and_a_full_quota_stops(tmp_path, monkeypatch):
    settings, cam, seg, root = cut_setup(tmp_path, monkeypatch)
    ep = hc.build_episodes([rec(1, "approach", "event", T0)], cam.causes, 3000, 5000, 600000)[0][0]
    plan = hc.plan_window([seg], ep.window[0] / 1000.0, ep.window[1] / 1000.0, 10)
    short = hc.cut_episode(settings, cam, ep, plan, str(tmp_path), runner=FakeFfmpeg(),
                           prober=lambda c, d, n: {"frames": plan.frames - 1})
    assert short["status"] == "failed" and "not the %d planned" % plan.frames in short["why"]
    failed = hc.cut_episode(settings, cam, ep, plan, str(tmp_path), runner=FakeFfmpeg(rc=1, stderr=b"Invalid data"),
                            prober=lambda c, d, n: pytest.fail("no probe after a failed ffmpeg"))
    assert failed["status"] == "failed" and "Invalid data" in failed["why"]
    with pytest.raises(hc.QuotaStop):
        hc.cut_episode(settings, cam, ep, plan, str(tmp_path),
                       runner=FakeFfmpeg(rc=1, stderr=b"/out/x.mp4.part: Disk quota exceeded"), prober=None)
    leftovers = [p for p in root.rglob("*") if p.is_file()]
    assert leftovers == []


def test_tc1559_untrusted_timestamps_are_regenerated_and_the_guard_comes_first(tmp_path, monkeypatch):
    settings, cam, _seg, root = cut_setup(tmp_path, monkeypatch)
    dawn = loaded(T0 / 1000.0 - 10, 200, broken=True)
    ep = hc.build_episodes([rec(1, "approach", "event", T0)], cam.causes, 3000, 5000, 600000)[0][0]
    plan = hc.plan_window([dawn], ep.window[0] / 1000.0, ep.window[1] / 1000.0, 10)
    fake = FakeFfmpeg()
    hc.cut_episode(settings, cam, ep, plan, str(tmp_path), runner=fake,
                   prober=lambda c, d, n: {"frames": plan.frames})
    assert fake.argv[fake.argv.index("-bsf:v") + 1] == "setts=ts=N/(10*TB)"
    # a camera that slowed to 8 fps in the dark: the clip is retimed at 8, not the nominal 10
    slow = hc.Loaded(Seg("door-slow.ts", T0 / 1000.0 - 10), ts_bytes(200, broken=True), 10, next_start=T0 / 1000.0 + 15)
    plan8 = hc.plan_window([slow], ep.window[0] / 1000.0, ep.window[1] / 1000.0, 10)
    fake8 = FakeFfmpeg()
    hc.cut_episode(settings, cam, ep, plan8, str(tmp_path), runner=fake8,
                   prober=lambda c, d, n: {"frames": plan8.frames, "duration": plan8.frames / 8.0})
    assert fake8.argv[fake8.argv.index("-bsf:v") + 1] == "setts=ts=N/(8*TB)"
    monkeypatch.setattr(hc, "guard", lambda settings: (False, "not mounted"))
    with pytest.raises(hc.GuardStop):
        hc.cut_episode(settings, cam, ep, plan, str(tmp_path), runner=pytest.fail, prober=None)


# ── TC-1560 one camera's pass: ledger, overlap, retries, loss ────────────────


def write_segment(seg_dir, start_epoch, frames, part=False):
    day = time.strftime("%Y-%m-%d", time.gmtime(start_epoch))
    name = time.strftime("door-%Y%m%dT%H%M%SZ.ts", time.gmtime(start_epoch)) + (".part" if part else "")
    os.makedirs(os.path.join(seg_dir, day), exist_ok=True)
    pts = 900000 + int((start_epoch - 1792435200) * 90000)
    with open(os.path.join(seg_dir, day, name), "wb") as handle:
        handle.write(ts_bytes(frames, start_pts=pts))


def test_tc1560_each_episode_is_cut_once_and_a_loss_is_recorded_and_alerted(tmp_path, monkeypatch):
    state, seg_dir = str(tmp_path / "state"), str(tmp_path / "segs")
    root = tmp_path / "clips"
    root.mkdir()
    monkeypatch.setattr(hc, "guard", lambda settings: (True, "ok"))
    monkeypatch.setattr(hc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    settings = hc.ClipsSettings({"CAMERA_DOOR_SEGMENT_DIR": seg_dir})
    settings.root = str(root)
    cam = settings.cameras[0]
    write_segment(seg_dir, T0 / 1000.0 - 60, 1200)                  # 19:49:00 .. 19:51:00
    # two visits 1.4 s apart (the second's pre-roll overlaps the first's end), one in a pruned past
    for r in (rec(1, "road-stop", "event", T0), rec(2, "road-hold", "renewal", T0 + 3000),
              rec(3, "approach", "event", T0 + 9400), rec(4, "approach", "event", T0 - 3600000)):
        ev.append(state, r)
    frames = {}

    def probe(c, d, n):
        return {"frames": frames["want"]}
    real_cut = hc.cut_episode

    def cut(settings_, cam_, ep, plan, state_dir, runner_=None, prober=None, localtime=time.localtime):
        frames["want"] = plan.frames
        return real_cut(settings_, cam_, ep, plan, state_dir, runner=FakeFfmpeg(), prober=probe)
    monkeypatch.setattr(hc, "cut_episode", cut)
    alerts = []
    now = T0 + 60000
    counts = hc.cut_camera(settings, cam, state, now, alert=lambda *a: alerts.append(a))
    assert counts["cut"] == 2 and counts["lost"] == 1
    assert alerts == []  # the lost one ended before this ledger began
    again = hc.cut_camera(settings, cam, state, now + 60000, alert=lambda *a: alerts.append(a))
    assert again["cut"] == 0 and again["lost"] == 0
    ledger = json.load(open(hc.ledger_path(state, "door")))
    assert {e["status"] for e in ledger["episodes"].values()} == {"cut", "lost"}
    # a second source's late record for the first visit - earlier than the hub's,
    # so it would be a new episode id - is not cut twice
    ev.append(state, rec(5, "approach", "event", T0 - 500, source="panel"))
    settings.cameras[0].sources = ["hub-motion", "panel"]
    assert hc.cut_camera(settings, cam, state, now + 120000)["cut"] == 0
    # a new loss inside the ledger's life is alerted once per run
    ev.append(state, rec(6, "approach", "event", T0 + 70000))
    shutil.rmtree(seg_dir)
    hc.cut_camera(settings, cam, state, T0 + 70000 + hc.COVER_WAIT_MS + 6000, alert=lambda *a: alerts.append(a))
    assert len(alerts) == 1 and alerts[0][0] == "clips-lost-door"


def test_tc1560_a_failed_cut_is_retried_twice_then_recorded(tmp_path, monkeypatch):
    state, seg_dir = str(tmp_path / "state"), str(tmp_path / "segs")
    monkeypatch.setattr(hc, "cut_episode", lambda *a, **k: {"status": "failed", "why": "boom"})
    settings = hc.ClipsSettings({"CAMERA_DOOR_SEGMENT_DIR": seg_dir})
    write_segment(seg_dir, T0 / 1000.0 - 60, 1200)
    ev.append(state, rec(1, "approach", "event", T0))
    now = T0 + 60000
    for attempt in (1, 2):
        assert hc.cut_camera(settings, settings.cameras[0], state, now)["failed"] == 1
        assert json.load(open(hc.ledger_path(state, "door")))["retries"] == {"hub-motion:r.1": attempt}
    hc.cut_camera(settings, settings.cameras[0], state, now)
    ledger = json.load(open(hc.ledger_path(state, "door")))
    assert ledger["episodes"]["hub-motion:r.1"]["status"] == "failed" and ledger["retries"] == {}
    assert hc.cut_camera(settings, settings.cameras[0], state, now)["failed"] == 0


# ── TC-1561 / TC-1562 the encode batch ───────────────────────────────────────


def test_tc1561_the_encoder_is_vaapi_at_the_lowest_weight():
    s = hc.ClipsSettings({"CLIPS_ENCODE_KBPS": "250"})
    argv = hc.encode_command(s, "door", "/srv/scratch/clips/door/2026-10-19", "a.mp4", "a.enc.mp4.part")
    joined = " ".join(argv)
    assert argv[argv.index("--cpu-shares") + 1] == "2" and argv[argv.index("--device") + 1] == "/dev/dri/renderD128"
    assert "-hwaccel vaapi" in joined and "-c:v h264_vaapi" in joined and "-b:v 250k -maxrate 500k" in joined
    assert "-rc_mode VBR" in joined and "--network none" in joined and "-i /out/a.mp4" in joined
    assert "-i" not in argv[:argv.index("--entrypoint")]  # no stdin for the encoder
    hevc = hc.encode_command(hc.ClipsSettings({"CLIPS_ENCODE_CODEC": "hevc"}), "door", "/d", "a.mp4", "p")
    assert "hevc_vaapi" in hevc and hevc[hevc.index("-tag:v") + 1] == "hvc1"


def encode_setup(tmp_path, monkeypatch, copy_bytes=1000):
    monkeypatch.setattr(hc, "guard", lambda settings: (True, "ok"))
    monkeypatch.setattr(hc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    day = tmp_path / "door" / "2026-10-19"
    day.mkdir(parents=True)
    (day / "c.mp4").write_bytes(b"c" * copy_bytes)
    (day / "c.json").write_text(json.dumps({"v": 1, "frames": 300, "bytes": copy_bytes, "sha256": "x", "encode": None}))
    s = hc.ClipsSettings({"CLIPS_ENCODE_ENABLED": "true"})
    return s, str(day)


def enc_runner(out_bytes, rc=0, stderr=b""):
    def run(argv, stdout=None, stderr_=None, timeout=None, **kw):
        if rc == 0 and out_bytes:
            bind = argv[argv.index("-v") + 1].split(":/out")[0]
            with open(os.path.join(bind, argv[-1][len("/out/"):]), "wb") as handle:
                handle.write(b"e" * out_bytes)
        return subprocess.CompletedProcess(argv, rc, b"", stderr)
    return run


def test_tc1562_a_verified_smaller_encode_replaces_the_copy(tmp_path, monkeypatch):
    s, day = encode_setup(tmp_path, monkeypatch)
    res = hc.encode_clip(s, "door", day, "c", runner=enc_runner(400), prober=lambda c, d, n: {"frames": 300})
    assert res["kept"] is True and res["bytes_encoded"] == 400
    assert open(os.path.join(day, "c.mp4"), "rb").read() == b"e" * 400
    side = json.load(open(os.path.join(day, "c.json")))
    assert side["bytes"] == 400 and side["sha256"] == hc.sha256_file(os.path.join(day, "c.mp4"))
    assert side["encode"]["kept"] is True and not [n for n in os.listdir(day) if n.endswith(".part")]
    assert hc.encode_candidates(str(tmp_path), "door") == []


@pytest.mark.parametrize("out,frames,rc,word", [(900, 300, 0, None), (400, 280, 0, "frames"), (0, 300, 1, "exited")])
def test_tc1562_otherwise_the_copy_stays_and_the_outcome_is_recorded(tmp_path, monkeypatch, out, frames, rc, word):
    s, day = encode_setup(tmp_path, monkeypatch)
    res = hc.encode_clip(s, "door", day, "c", runner=enc_runner(out, rc), prober=lambda c, d, n: {"frames": frames})
    assert res["kept"] is False and (word is None or word in res["failed"])
    assert open(os.path.join(day, "c.mp4"), "rb").read() == b"c" * 1000
    assert json.load(open(os.path.join(day, "c.json")))["encode"]["kept"] is False
    assert not [n for n in os.listdir(day) if n.endswith(".part")]


def test_tc1562_a_full_quota_during_the_encode_stops_the_batch(tmp_path, monkeypatch):
    s, day = encode_setup(tmp_path, monkeypatch)
    with pytest.raises(hc.QuotaStop):
        hc.encode_clip(s, "door", day, "c", runner=enc_runner(0, 1, b"No space left on device"), prober=None)
    assert open(os.path.join(day, "c.mp4"), "rb").read() == b"c" * 1000


# ── TC-1563 / TC-1564 retention and the quota stop ───────────────────────────


def test_tc1563_age_then_cap_then_quota_oldest_first_the_cap_winning():
    day = 86400 * 1000
    now = T0
    clips = [(now - 400 * day, "d", "old", 10), (now - 30 * day, "d", "a", 40), (now - 20 * day, "d", "b", 40),
             (now - day, "d", "c", 40)]
    assert [c[2] for c in hc.retention_plan(clips, now, 365, 1000)] == ["old"]
    assert [c[2] for c in hc.retention_plan(clips, now, 365, 80)] == ["old", "a"]
    assert [c[2] for c in hc.retention_plan(clips, now, 3650, 80)] == ["old", "a"]  # the cap wins over the age
    # quota: 130 used of 140 is past 90 %; down to 85 % = 119 -> two oldest go
    assert [c[2] for c in hc.retention_plan(clips, now, 3650, 10 ** 9, 130, 140, 90)] == ["old", "a"]
    assert hc.retention_plan(clips, now, 3650, 10 ** 9, 100, 140, 90) == []


def test_tc1563_retention_removes_pairs_rewrites_the_index_and_tidies(tmp_path, monkeypatch):
    root = tmp_path / "clips"
    state = tmp_path / "state"
    s = hc.ClipsSettings({})
    s.root = str(root)
    s.cap_mib = 1
    old_day = root / "door" / "2026-10-13"
    new_day = root / "door" / "2026-10-19"
    for day, stem, start, size in ((old_day, "door_old", T0 - 6 * 86400000, 700 * 1024),
                                   (new_day, "door_new", T0, 500 * 1024)):
        day.mkdir(parents=True)
        (day / (stem + ".mp4")).write_bytes(b"x" * size)
        (day / (stem + ".json")).write_text(json.dumps({"v": 1, "actual": {"start_utc_ms": start}}))
    (new_day / "door_crash.mp4.part").write_bytes(b"p")
    os.utime(new_day / "door_crash.mp4.part", (0, 0))
    (root / "door" / "index.jsonl").write_text(
        json.dumps({"clip": "2026-10-13/door_old.mp4"}) + "\n" + json.dumps({"clip": "2026-10-19/door_new.mp4"}) + "\n")
    ev.append(str(state), rec(1, "approach", "event", T0 - 30 * 86400000))
    result = hc.apply_retention(s, str(state), T0)
    assert result["removed"] == 1 and result["stale_parts"] == 1 and result["event_days_removed"] == 1
    assert not old_day.exists() and sorted(os.listdir(new_day)) == ["door_new.json", "door_new.mp4"]
    assert [json.loads(l)["clip"] for l in (root / "door" / "index.jsonl").read_text(encoding="utf-8").splitlines()] == [
        "2026-10-19/door_new.mp4"]


def test_tc1564_a_quota_stop_holds_the_cutter_until_retention_makes_room(tmp_path, monkeypatch):
    state = str(tmp_path / "state")
    env = tmp_path / "env"
    env.write_text("CLIPS_ENABLED=true\nCAMERA_DOOR_SEGMENT_DIR=%s\n" % (tmp_path / "segs"))
    hc.set_quota_stop(state, "Disk quota exceeded", T0)
    monkeypatch.setattr(hc, "cut_camera", lambda *a, **k: pytest.fail("the cutter ran while stopped"))
    monkeypatch.setattr(hc, "guard", lambda settings: (True, "ok"))
    assert hc.main(["--env-file", str(env), "--state-dir", state, "cut"]) == hc.EXIT_QUOTA
    monkeypatch.setattr(hc, "clips_quota", lambda settings: (95 * MIB, 100 * MIB))
    monkeypatch.setattr(hc, "apply_retention", lambda *a, **k: {"removed": 0})
    hc.main(["--env-file", str(env), "--state-dir", state, "retention"])
    assert hc.quota_stopped(state)  # still past 90 %
    monkeypatch.setattr(hc, "clips_quota", lambda settings: (50 * MIB, 100 * MIB))
    hc.main(["--env-file", str(env), "--state-dir", state, "retention"])
    assert hc.quota_stopped(state) is None


def test_tc1564_a_quota_hit_during_a_cut_sets_the_stop_and_alerts(tmp_path, monkeypatch):
    state = str(tmp_path / "state")
    env = tmp_path / "env"
    env.write_text("CLIPS_ENABLED=true\n")
    sent = []
    monkeypatch.setattr(hc, "make_alert", lambda env_file: lambda *a: sent.append(a))

    def full(*a, **k):
        raise hc.QuotaStop("Disk quota exceeded")
    monkeypatch.setattr(hc, "cut_camera", full)
    assert hc.main(["--env-file", str(env), "--state-dir", state, "cut"]) == hc.EXIT_QUOTA
    assert hc.quota_stopped(state)["why"] == "Disk quota exceeded" and sent[0][0] == "clips-quota"
    env.write_text("CLIPS_ENABLED=false\n")
    assert hc.main(["--env-file", str(env), "--state-dir", state, "cut"]) == 0  # off is a clean no-op


# ── TC-1565 the scratch volume's clips project ───────────────────────────────


def test_tc1565_clips_is_a_scratch_writer_sized_before_ottertube():
    assert hs.WRITER_NAMES == ("door", "meetings", "clips", "ottertube")
    want = {"door": 8192, "meetings": 2048, "clips": 1024, "ottertube": 6144}
    # the live volume: 18170 MiB usable (door 8192 + meetings 2048 + ottertube 5882 + 2048 floor)
    got = hs.size_quotas(18170 * MIB, want, 2048, 512)
    assert got == {"door": 8192, "meetings": 2048, "clips": 1024, "ottertube": 4858}
    tight = hs.size_quotas((8192 + 2048 + 512 + 512 + 2048 + 100) * MIB, want, 2048, 512)
    assert tight == {"door": 8192, "meetings": 2048, "clips": 612, "ottertube": 512}
    s = hs.Settings({})
    assert s.quota_mib["clips"] == 5120   # 5 GiB once the volume is grown to 22 GiB (2026-10-27)
    with pytest.raises(hs.Stop, match="clips"):  # a writer left out is refused, not unlimited
        hs.size_quotas(18170 * MIB, {"door": 8192, "meetings": 2048, "ottertube": 6144}, 2048, 512)


# ── TC-1566 the units, firstboot, the template ───────────────────────────────


def test_tc1566_units_are_writers_with_their_knob_and_low_priority_where_ruled():
    clips = STACK / "clips"
    for name in ("homehub-clips-cut.service", "homehub-clips-encode.service", "homehub-clips-retention.service"):
        unit = (clips / name).read_text(encoding="utf-8")
        assert "CapabilityBoundingSet=~CAP_SYS_RESOURCE" in unit and "NoNewPrivileges=true" in unit
        assert not re.search(r"(?m)^(Requires=.*docker|BindsTo=)", unit)
        assert "StateDirectory=homehub-clips" in unit
    enc = (clips / "homehub-clips-encode.service").read_text(encoding="utf-8")
    assert "Nice=19" in enc and "IOSchedulingClass=idle" in enc
    motion = (STACK / "camera-motion" / "homehub-camera-motion@.service").read_text(encoding="utf-8")
    assert "ExecCondition=/usr/bin/python3 /opt/homehub/stack/camera-motion/camera_motion.py condition --camera %i" in motion
    assert "RestartPreventExitStatus=78" in motion and not re.search(r"(?m)^Requires=.*docker", motion)
    assert "docker rm -f homehub-camera-motion-%i" in motion
    for timer, words in (("homehub-clips-cut.timer", "OnUnitInactiveSec=60s"),
                         ("homehub-clips-retention.timer", "OnUnitActiveSec=1h"),
                         ("homehub-clips-encode.timer", "OnUnitInactiveSec=15min")):
        assert words in (clips / timer).read_text(encoding="utf-8")


def test_tc1566_firstboot_installs_everything_provisions_opencv_first_and_follows_the_knobs():
    fb = (STACK / "autoinstall" / "firstboot.sh").read_text(encoding="utf-8")
    block = fb[fb.index("# BEGIN camera-clips"):fb.index("# END camera-clips")]
    assert 'install -m 0644 "$STACK_DIR/camera-motion/homehub-camera-motion@.service"' in block
    for unit in ("cut", "encode", "retention"):
        assert "homehub-clips-%s.service" % unit in block and "homehub-clips-%s.timer" % unit in block
    assert block.index("apt-get install -y --no-install-recommends python3-opencv") < block.index(
        'systemctl enable "homehub-camera-motion@$__c.service"')
    assert "systemctl disable --now homehub-clips-cut.timer homehub-clips-retention.timer homehub-clips-encode.timer" in block
    assert "start --no-block" in block and fb.index("# BEGIN camera-clips") < fb.index("# ── 7. done")
    pk = (STACK / "autoinstall" / "packages.list").read_text(encoding="utf-8")
    assert re.search(r"(?m)^python3-opencv\s*$", pk)
    wall = (STACK / "autoinstall" / "wall" / "packages.list").read_text(encoding="utf-8")
    assert re.search(r"(?m)^python3-opencv\b", wall)  # the same runtime as the panel's


def test_tc1566_the_template_declares_every_knob_off_with_placeholder_zones():
    import monitor_common as mc
    env = mc.read_env_text((STACK / ".env.example").read_text(encoding="utf-8"))
    for key, value in (("CAMERAS", "door"), ("CLIPS_ENABLED", "false"), ("CLIPS_ENCODE_ENABLED", "false"),
                       ("CAMERA_DOOR_MOTION_ENABLED", "false"), ("CAMERA_DOOR_MOTION_CALIBRATED", "false"),
                       ("CLIPS_ROOT", "/srv/scratch/clips"), ("CLIPS_RETENTION_DAYS", "365"), ("CLIPS_CAP_MIB", "95367"),
                       ("CAMERA_DOOR_CLIP_PRE_SEC", "3"), ("CAMERA_DOOR_CLIP_LEASE_SEC", "5"),
                       ("CAMERA_DOOR_MOTION_APPROACH_POLYGON", "0,0;1,0;1,1;0,1"), ("CAMERA_DOOR_MOTION_ROAD_POLYGON", ""),
                       ("CAMERA_MOTION_BUDGET_PCT", "25"), ("HOMEHUB_SCRATCH_QUOTA_CLIPS_MIB", "5120"),
                       ("HOMEHUB_SCRATCH_SIZE_MIB", "22528"), ("HOMEHUB_SCRATCH_VG_RESERVE_MIB", "1200"),
                       ("CLIPS_ENCODE_SIZE", "0"), ("CLIPS_ENCODE_FPS", "0")):
        assert env.get(key) == value, key
    s = hc.ClipsSettings(env)
    assert s.cameras[0].causes == ["approach", "road-stop"]


# ── TC-1568 status ───────────────────────────────────────────────────────────


def test_tc1568_status_reports_events_clips_backlog_and_the_detector(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "clips"
    s = hc.ClipsSettings({"CLIPS_ENABLED": "true", "CLIPS_ENCODE_ENABLED": "true"})
    s.root = str(root)
    monkeypatch.setattr(hc, "clips_quota", lambda settings: (10 * MIB, 1024 * MIB))
    ev.append(str(state), rec(1, "approach", "event", T0 - 1000))
    day = root / "door" / "2026-10-19"
    day.mkdir(parents=True)
    stem = "door_2026-10-19_14-49-00-0500_approach_10s"   # 19:49:00Z, a minute before T0
    (day / (stem + ".mp4")).write_bytes(b"x" * 100)
    (day / (stem + ".json")).write_text(json.dumps({"v": 1, "encode": None}))
    import monitor_common as mc
    mc.save_json_atomic(str(state / "motion" / "door.json"), {"frames": 5})
    out = hc.status(s, str(state), T0)
    door = out["cameras"]["door"]
    assert out["quota"] == {"used_mib": 10, "limit_mib": 1024} and out["clips"] == 1
    assert door["events_24h"] == 1 and door["last_clip"] == "2026-10-19/%s.mp4" % stem and door["last_clip_age_min"] == 1.0
    assert door["encode_backlog"] == 1 and door["detector"] == {"frames": 5}


# ── TC-1569 integration: real segments through the pinned image ──────────────


SEGMENTS = os.environ.get("HOMEHUB_CLIPS_SEGMENTS", "")


def docker_ok():
    try:
        return subprocess.run(["docker", "version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.release
@pytest.mark.skipif(not (SEGMENTS and os.path.isdir(SEGMENTS) and os.name == "posix" and docker_ok()),
                    reason="set HOMEHUB_CLIPS_SEGMENTS to a door segment tree (YYYY-MM-DD/door-*.ts) on a docker host")
def test_tc1569_real_segments_cut_into_clips_that_decode_with_the_planned_frames(tmp_path, monkeypatch):
    root = tmp_path / "clips"
    root.mkdir()
    state = str(tmp_path / "state")
    monkeypatch.setattr(hc, "guard", lambda settings: (True, "ok"))
    s = hc.ClipsSettings({"CAMERA_DOOR_SEGMENT_DIR": SEGMENTS})
    s.root = str(root)
    segs = [x for x in hc.list_segments(SEGMENTS, "door") if not x.part]
    assert segs, "no segments"
    # one visit 30 s into every segment, and one across the first boundary
    events = [int((seg.start + 30) * 1000) for seg in segs]
    if len(segs) > 1:
        events.append(int((segs[1].start - 1.5) * 1000))
    for i, ms in enumerate(sorted(events)):
        ev.append(state, rec(i, "approach", "event", ms))
        ev.append(state, rec(100 + i, "visitor", "renewal", ms + 3000))
    counts = hc.cut_camera(s, s.cameras[0], state, max(events) + 20 * 60 * 1000)
    assert counts["cut"] == len(events) and counts["failed"] == 0
    for side in root.rglob("*.json"):
        data = json.loads(side.read_text(encoding="utf-8"))
        mp4 = side.with_suffix(".mp4")
        done = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", "%s:/c.mp4:ro" % mp4,
                               "--entrypoint", "ffmpeg", hc.door_relay.image_ref(), "-v", "error", "-i", "/c.mp4",
                               "-f", "null", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        assert done.returncode == 0 and not done.stderr.strip(), done.stderr[-300:]
        assert data["sha256"] == hc.sha256_file(str(mp4))
        assert 10 <= data["frames"] <= 130


@pytest.mark.skipif(not shutil.which("bash"), reason="bash absent")
def test_tc1566_the_firstboot_block_executed_follows_the_last_assignment_and_installs_opencv_first(tmp_path):
    fb = (STACK / "autoinstall" / "firstboot.sh").read_text(encoding="utf-8")
    block = fb[fb.index("# BEGIN camera-clips"):fb.index("# END camera-clips")]
    stack = tmp_path / "stack"
    for sub in ("camera-motion", "clips"):
        (stack / sub).mkdir(parents=True)
    # Re-assigned knobs, as a deploy that appends would leave them: the LAST must win.
    (stack / ".env").write_bytes("CAMERAS=garage\nCAMERAS=door * # the doorbell\nCAMERA_DOOR_MOTION_ENABLED=false\n"
                                "CAMERA_DOOR_MOTION_ENABLED=true\nCLIPS_ENABLED=false\nCLIPS_ENABLED=\"true\"\n"
                                "CLIPS_ENCODE_ENABLED=false\n".encode("utf-8"))
    calls = tmp_path / "calls"
    prelude = ("STACK_DIR=%s\nCALLS=%s\n" % (stack.as_posix(), calls.as_posix()) + r'''
log() { echo "LOG $*" >> "$CALLS"; }
_env_val() { sed -n "s/^$1=//p" "$STACK_DIR/.env" | head -1; }
install() { :; }
systemctl() { echo "systemctl $*" >> "$CALLS"; }
python3() { echo "python3 $*" >> "$CALLS"; return 1; }
apt-get() { echo "apt-get $*" >> "$CALLS"; }
''')
    script = tmp_path / "block.sh"
    script.write_bytes((prelude + block).encode("utf-8"))
    done = subprocess.run(["bash", str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert done.returncode == 0, done.stderr
    lines = calls.read_text(encoding="utf-8").splitlines()
    apt = next(i for i, l in enumerate(lines) if l.startswith("apt-get install -y --no-install-recommends python3-opencv"))
    enable = next(i for i, l in enumerate(lines) if l == "systemctl enable homehub-camera-motion@door.service")
    assert apt < enable
    assert not any("camera-motion@garage" in l or "camera-motion@doorbell" in l for l in lines)
    assert sum(1 for l in lines if l.startswith("systemctl enable homehub-camera-motion@")) == 1  # no glob
    assert "systemctl enable homehub-clips-cut.timer homehub-clips-retention.timer" in lines
    assert "systemctl disable --now homehub-clips-encode.timer" in lines


# ── review round 1 (DeepSeek) regressions ────────────────────────────────────


def test_tc1555_a_hole_at_the_window_end_is_a_gap_not_full_coverage():
    a = loaded(1000.0, 50)                          # 1000.0 .. 1005.0, then the recorder is down
    b = loaded(1030.0, 50, start_pts=5)              # back at 1030
    plan = hc.plan_window([a, b], 1002.0, 1010.0, 10)
    assert plan.end_covered and plan.actual[1] == pytest.approx(1005.0)
    assert plan.gaps == [{"after_utc_ms": 1005000, "seconds": 5.0}] and plan.coverage == "partial"


def test_tc1552_a_name_that_is_not_a_time_is_not_a_segment(tmp_path):
    day = tmp_path / "2026-10-19"
    day.mkdir()
    (day / "door-20261399T999999Z.ts").write_bytes(b"")
    (day / "door-20261019T195000Z.ts").write_bytes(b"")
    assert [s.name for s in hc.list_segments(str(tmp_path), "door")] == ["door-20261019T195000Z.ts"]


def test_tc1559_a_cut_that_plays_in_an_instant_is_refused(tmp_path, monkeypatch):
    settings, cam, _seg, root = cut_setup(tmp_path, monkeypatch)
    dawn = loaded(T0 / 1000.0 - 10, 200, broken=True)
    ep = hc.build_episodes([rec(1, "approach", "event", T0)], cam.causes, 3000, 5000, 600000)[0][0]
    plan = hc.plan_window([dawn], ep.window[0] / 1000.0, ep.window[1] / 1000.0, 10)
    res = hc.cut_episode(settings, cam, ep, plan, str(tmp_path), runner=FakeFfmpeg(),
                         prober=lambda c, d, n: {"frames": plan.frames, "duration": 0.07})
    assert res["status"] == "failed" and "lasts 0.07 s" in res["why"]
    assert [p for p in root.rglob("*") if p.is_file()] == []


def test_tc1559_the_guard_is_asked_again_before_the_record_and_nothing_half_written_stays(tmp_path, monkeypatch):
    settings, cam, seg, root = cut_setup(tmp_path, monkeypatch)
    answers = iter([(True, "ok"), (False, "unmounted between the cut and its record")])
    monkeypatch.setattr(hc, "guard", lambda s: next(answers))
    ep = hc.build_episodes([rec(1, "approach", "event", T0)], cam.causes, 3000, 5000, 600000)[0][0]
    plan = hc.plan_window([seg], ep.window[0] / 1000.0, ep.window[1] / 1000.0, 10)
    with pytest.raises(hc.GuardStop):
        hc.cut_episode(settings, cam, ep, plan, str(tmp_path), runner=FakeFfmpeg(),
                       prober=lambda c, d, n: {"frames": plan.frames})
    assert [p for p in root.rglob("*") if p.is_file()] == []


def test_tc1563_stale_parts_go_before_the_quota_is_read_and_unconfigured_cameras_count(tmp_path):
    root = tmp_path / "clips"
    s = hc.ClipsSettings({"CLIPS_RETENTION_DAYS": "3650"})
    s.root = str(root)
    old_cam = root / "garage" / "2026-10-20"     # a camera no longer in CAMERAS
    old_cam.mkdir(parents=True)
    stem = "garage_2026-10-20_10-00-00+0000_approach_10s"
    (old_cam / (stem + ".mp4")).write_bytes(b"x" * 10)
    part = root / "garage" / "2026-10-20" / "x.mp4.part"
    part.write_bytes(b"p")
    os.utime(part, (0, 0))
    seen = []

    def quota():
        seen.append(part.exists())
        return 95, 100                            # past 90 %: the oldest clip must go
    result = hc.apply_retention(s, str(tmp_path / "state"), T0, quota)
    assert seen == [False] and result["removed"] == 1 and not old_cam.exists()


def test_tc1557_a_names_time_reads_back_to_utc():
    assert hc.stem_utc_ms("door_2026-10-19_14-51-01-0500_road-stop_29s") == T0 + 61000
    assert hc.stem_utc_ms("door_2026-10-19_19-51-01+0000_approach_9s-2") == T0 + 61000
    assert hc.stem_utc_ms("c") is None


# ── review round 2 (DeepSeek) regressions ────────────────────────────────────


def test_tc1553_a_hole_inside_a_segment_keeps_pts_timing():
    pkts = stream(40, start_pts=900000)
    data = b"".join(pkts) + b"".join(stream(40, start_pts=900000 + 30 * 90000 + 40 * 9000))
    idx = hc.scan_frames(data, False)
    times, mode = hc.frame_times(idx, 1000.0, 10)
    assert mode == "pts" and times[40] == pytest.approx(1034.0)


def test_tc1555_a_window_covered_to_its_end_is_full_even_before_a_hole():
    a = loaded(1000.0, 50)                           # 1000.0 .. 1005.0
    b = loaded(1030.0, 50, start_pts=5)
    plan = hc.plan_window([a, b], 1002.0, 1004.95, 10)
    assert plan.end_covered and plan.gaps == [] and plan.coverage == "full"


def test_tc1551_a_lease_as_long_as_the_cap_is_refused():
    with pytest.raises(hc.Invalid, match="CLIP_LEASE_SEC"):
        hc.ClipsSettings({"CAMERA_DOOR_CLIP_LEASE_SEC": "60", "CAMERA_DOOR_CLIP_MAX_SEC": "60"})


def test_tc1557_a_name_with_an_impossible_time_does_not_read_as_one():
    assert hc.stem_utc_ms("door_2026-10-32_10-00-00+0000_approach_9s") is None
    assert hc.stem_utc_ms("door_2026-10-19_10-00-00+9959_approach_9s") is None


def test_tc1563_the_index_loses_lines_of_clips_that_are_already_gone_and_orphan_sidecars_go(tmp_path):
    root = tmp_path / "clips"
    day = root / "door" / "2026-10-19"
    day.mkdir(parents=True)
    keep = "door_2026-10-19_10-00-00+0000_approach_9s"
    (day / (keep + ".mp4")).write_bytes(b"x")
    (day / "door_2026-10-19_09-00-00+0000_approach_9s.json").write_text("{}")   # its clip never landed
    os.utime(day / "door_2026-10-19_09-00-00+0000_approach_9s.json", (0, 0))
    old = root / "door" / "2026-01-01"
    old.mkdir()
    gone = "door_2026-01-01_10-00-00+0000_approach_9s"
    (old / (gone + ".mp4")).write_bytes(b"y")
    index = root / "door" / "index.jsonl"
    index.write_text("".join(json.dumps({"clip": c}) + "\n" for c in
                             ("2025-01-01/door_lost-in-a-crash.mp4", "2026-01-01/%s.mp4" % gone, "2026-10-19/%s.mp4" % keep)))
    s = hc.ClipsSettings({"CLIPS_RETENTION_DAYS": "30"})
    s.root = str(root)
    assert hc.apply_retention(s, str(tmp_path / "state"), T0)["removed"] == 1
    assert [json.loads(l)["clip"] for l in index.read_text().splitlines()] == ["2026-10-19/%s.mp4" % keep]
    assert sorted(os.listdir(day)) == [keep + ".mp4"]


# ── review round 3 (DeepSeek) regressions ────────────────────────────────────


def test_tc1559_a_quota_word_at_the_end_of_a_long_stderr_still_stops(tmp_path, monkeypatch):
    settings, cam, seg, root = cut_setup(tmp_path, monkeypatch)
    ep = hc.build_episodes([rec(1, "approach", "event", T0)], cam.causes, 3000, 5000, 600000)[0][0]
    plan = hc.plan_window([seg], ep.window[0] / 1000.0, ep.window[1] / 1000.0, 10)
    noisy = b"[h264] error while decoding MB\n" * 4000 + b"/out/x.mp4.part: Disk quota exceeded\n"
    with pytest.raises(hc.QuotaStop):
        hc.cut_episode(settings, cam, ep, plan, str(tmp_path), runner=FakeFfmpeg(rc=1, stderr=noisy), prober=None)


def test_tc1562_the_record_is_written_before_the_encoded_file_replaces_the_copy(tmp_path, monkeypatch):
    s, day = encode_setup(tmp_path, monkeypatch)

    def full(path, data):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(hc, "write_json_atomic", full)
    with pytest.raises(hc.QuotaStop):
        hc.encode_clip(s, "door", day, "c", runner=enc_runner(400), prober=lambda c, d, n: {"frames": 300})
    assert open(os.path.join(day, "c.mp4"), "rb").read() == b"c" * 1000      # the copy, untouched
    assert not [n for n in os.listdir(day) if n.endswith(".part")]


def test_tc1562_an_encode_of_the_wrong_length_keeps_the_copy(tmp_path, monkeypatch):
    s, day = encode_setup(tmp_path, monkeypatch)
    side = json.load(open(os.path.join(day, "c.json")))
    side["actual"] = {"start_utc_ms": T0, "end_utc_ms": T0 + 30000}
    open(os.path.join(day, "c.json"), "w").write(json.dumps(side))
    res = hc.encode_clip(s, "door", day, "c", runner=enc_runner(400),
                         prober=lambda c, d, n: {"frames": 300, "duration": 3.0})
    assert res["kept"] is False and "lasts 3.00 s" in res["failed"]
    assert open(os.path.join(day, "c.mp4"), "rb").read() == b"c" * 1000


def test_tc1563_a_clip_that_cannot_be_removed_keeps_its_index_line_and_a_killed_pass_is_repaired(tmp_path, monkeypatch):
    root = tmp_path / "clips"
    day = root / "door" / "2026-01-01"
    day.mkdir(parents=True)
    stem = "door_2026-01-01_10-00-00+0000_approach_9s"
    (day / (stem + ".mp4")).write_bytes(b"x")
    index = root / "door" / "index.jsonl"
    index.write_text(json.dumps({"clip": "2026-01-01/%s.mp4" % stem}) + "\n" + "[]\n" + json.dumps({"clip": []}) + "\n")
    s = hc.ClipsSettings({"CLIPS_RETENTION_DAYS": "30"})
    s.root = str(root)
    real_unlink = os.unlink

    def stuck(path, *a, **k):
        if str(path).endswith(".mp4"):
            raise PermissionError(13, "immutable")
        return real_unlink(path, *a, **k)
    monkeypatch.setattr(hc.os, "unlink", stuck)
    hc.apply_retention(s, str(tmp_path / "state"), T0)
    assert [json.loads(l).get("clip") for l in index.read_text().splitlines()] == ["2026-01-01/%s.mp4" % stem]
    monkeypatch.setattr(hc.os, "unlink", real_unlink)
    # a pass killed after deleting, before its rewrite: the marker makes the next one repair the index
    real_unlink(str(day / (stem + ".mp4")))
    hc.mark_index_dirty(str(root / "door"))
    hc.apply_retention(s, str(tmp_path / "state"), T0)
    assert index.read_text() == "" and not (root / "door" / hc.INDEX_DIRTY).exists()


def test_tc1564_a_guard_refusal_during_retention_alerts_and_exits_76(tmp_path, monkeypatch):
    env = tmp_path / "env"
    env.write_text("CLIPS_ENABLED=true\n")
    sent = []
    monkeypatch.setattr(hc, "make_alert", lambda env_file: lambda *a: sent.append(a))

    def refused(*a, **k):
        raise hc.GuardStop("unmounted mid-pass")
    monkeypatch.setattr(hc, "apply_retention", refused)
    assert hc.main(["--env-file", str(env), "--state-dir", str(tmp_path / "state"), "retention"]) == hc.EXIT_GUARD
    assert sent[0][0] == "clips-guard"


# ── fix round after the Opus review (2026-10-27) ─────────────────────────────


def part_segment(seg_dir, start_epoch, frames):
    """An open segment (.ts.part) as the recorder leaves it mid-rotation."""
    write_segment(seg_dir, start_epoch, frames)
    day = time.strftime("%Y-%m-%d", time.gmtime(start_epoch))
    name = time.strftime("door-%Y%m%dT%H%M%SZ.ts", time.gmtime(start_epoch))
    path = os.path.join(seg_dir, day, name)
    os.rename(path, path + ".part")
    return path


def test_tc1560_a_rotation_during_a_backlogged_pass_is_seen_by_the_next_episode(tmp_path, monkeypatch):
    state, seg_dir = str(tmp_path / "state"), str(tmp_path / "segs")
    root = tmp_path / "clips"
    root.mkdir()
    monkeypatch.setattr(hc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    settings = hc.ClipsSettings({"CAMERA_DOOR_SEGMENT_DIR": seg_dir})
    settings.root = str(root)
    final = part_segment(seg_dir, T0 / 1000.0 - 60, 1201)            # 19:49:00 .. 19:51:00, still open
    for r in (rec(1, "approach", "event", T0), rec(2, "approach", "event", T0 + 30000)):
        ev.append(state, r)
    frames = {}
    real_cut = hc.cut_episode

    def cut(settings_, cam_, ep, plan, state_dir, runner_=None, prober=None, localtime=time.localtime):
        frames["want"] = plan.frames
        result = real_cut(settings_, cam_, ep, plan, state_dir, runner=FakeFfmpeg(),
                          prober=lambda c, d, n: {"frames": frames["want"]})
        if os.path.exists(final + ".part"):
            os.rename(final + ".part", final)                         # the recorder rotates mid-pass
        return result
    monkeypatch.setattr(hc, "cut_episode", cut)
    counts = hc.cut_camera(settings, settings.cameras[0], state, T0 + 300000)  # a backlog: 5 min late
    assert counts["cut"] == 2 and counts["lost"] == 0


def test_tc1560_a_listed_segment_that_cannot_be_opened_waits_never_lost(tmp_path, monkeypatch):
    state, seg_dir = str(tmp_path / "state"), str(tmp_path / "segs")
    settings = hc.ClipsSettings({"CAMERA_DOOR_SEGMENT_DIR": seg_dir})
    write_segment(seg_dir, T0 / 1000.0 - 60, 1200)
    ev.append(state, rec(1, "approach", "event", T0))
    monkeypatch.setattr(hc, "load_segment", lambda *a, **k: None)   # renamed or pruned since listed
    counts = hc.cut_camera(settings, settings.cameras[0], state, T0 + 600000)
    assert counts == {"cut": 0, "lost": 0, "failed": 0, "waiting": 1, "rejected_records": 0}
    assert json.load(open(hc.ledger_path(state, "door")))["episodes"] == {}


def clip_files(root, day, stem, size, start_ms):
    d = root / "door" / day
    d.mkdir(parents=True, exist_ok=True)
    (d / (stem + ".mp4")).write_bytes(b"x" * size)
    (d / (stem + ".json")).write_text(json.dumps({
        "v": 1, "actual": {"start_utc_ms": start_ms, "end_utc_ms": start_ms + 9000}, "camera": "door",
        "cause": "approach", "coverage": "full", "events": [{"utc_ms": start_ms + 3000}], "episode": stem}))


def test_tc1564_room_is_made_before_a_cut_oldest_first_and_only_a_hopeless_case_stops(tmp_path):
    root = tmp_path / "clips"
    s = hc.ClipsSettings({})
    s.root = str(root)
    for i in range(3):
        clip_files(root, "2026-10-2%d" % i, "door_2026-10-2%d_10-00-00+0000_approach_9s" % i, 1000, T0 - (6 - i) * 86400000)
    other = {"v": 0}
    one = 1000 + (root / "door" / "2026-10-20" / "door_2026-10-20_10-00-00+0000_approach_9s.json").stat().st_size

    def quota():
        files = [p for p in root.rglob("*") if p.is_file()]
        return sum(p.stat().st_size for p in files) + other["v"], 10000

    def left():
        return sorted(p.name[:15] for p in root.rglob("*.mp4"))
    other["v"] = 9000 - 3 * one           # exactly at the 90 % ceiling
    assert hc.make_room(s, str(tmp_path), T0, 10, quota) == one      # just enough: the oldest only
    assert left() == ["door_2026-10-21", "door_2026-10-22"]
    assert hc.make_room(s, str(tmp_path), T0, 10, quota) == 0        # it fits now: nothing removed
    other["v"] = 9900
    with pytest.raises(hc.QuotaStop, match="cannot hold"):
        hc.make_room(s, str(tmp_path), T0, 500, quota)              # even with every clip gone it does not


def test_tc1564_the_cutter_makes_room_before_cutting(tmp_path, monkeypatch):
    state, seg_dir = str(tmp_path / "state"), str(tmp_path / "segs")
    settings = hc.ClipsSettings({"CAMERA_DOOR_SEGMENT_DIR": seg_dir})
    write_segment(seg_dir, T0 / 1000.0 - 60, 1200)
    ev.append(state, rec(1, "approach", "event", T0))
    order = []
    monkeypatch.setattr(hc, "make_room", lambda *a, **k: order.append(("room", a[3])))
    monkeypatch.setattr(hc, "cut_episode", lambda *a, **k: order.append(("cut",)) or {"status": "cut", "clip": "x"})
    hc.cut_camera(settings, settings.cameras[0], state, T0 + 60000, quota_fn=lambda: (0, 1))
    assert [o[0] for o in order] == ["room", "cut"] and order[0][1] > 0


def test_tc1553_index_timing_spreads_to_the_next_segment_and_the_cut_is_regenerated_at_that_rate():
    data = ts_bytes(100, broken=True)
    idx = hc.scan_frames(data, False)
    times, mode = hc.frame_times(idx, 1000.0, 10, end=1012.5)          # 8 fps in low light
    assert mode == "index" and times[99] == pytest.approx(1012.375)
    assert hc.frame_times(idx, 1000.0, 10, end=1100.0)[0][99] == pytest.approx(1009.9)  # a restart: nominal
    slow = hc.Loaded(Seg("door-x.ts", 1000.0), data, 10, next_start=1012.5)
    plan = hc.plan_window([slow], 1002.0, 1010.0, 10)      # 8 s at 8 fps: 2 s longer than nominal would say
    assert plan.rate == pytest.approx(8.0) and plan.regenerate
    assert "setts=ts=N/(8*TB)" in hc.cut_command("door", "/d", "p", plan.rate)
    assert hc.duration_plausible(plan.frames / 8.0, plan, 10)
    assert not hc.duration_plausible(plan.frames / 10.0 - 1.1, plan, 10)


def test_tc1562_an_encode_whose_replace_never_happened_is_reconciled_by_size(tmp_path, monkeypatch):
    s, day = encode_setup(tmp_path, monkeypatch)
    side = json.load(open(os.path.join(day, "c.json")))
    side.update(bytes=400, sha256="enc", encode={"kept": True, "bytes_copy": 1000, "bytes_encoded": 400,
                                                  "sha256_copy": "copy"})
    open(os.path.join(day, "c.json"), "w").write(json.dumps(side))
    assert hc.reconcile_encodes(str(tmp_path), "door", T0 / 1000.0) == 1
    side = json.load(open(os.path.join(day, "c.json")))
    assert side["bytes"] == 1000 and side["sha256"] == "copy" and side["encode"]["kept"] is False
    assert hc.reconcile_encodes(str(tmp_path), "door", T0 / 1000.0) == 0


def test_tc1562_a_kept_encode_records_the_hash_of_the_copy(tmp_path, monkeypatch):
    s, day = encode_setup(tmp_path, monkeypatch)
    res = hc.encode_clip(s, "door", day, "c", runner=enc_runner(400), prober=lambda c, d, n: {"frames": 300})
    assert res["kept"] and res["sha256_copy"] == "x"


def test_tc1563_a_dirty_index_is_rebuilt_from_the_sidecars(tmp_path):
    root = tmp_path / "clips"
    stem = "door_2026-10-19_10-00-00+0000_approach_9s"
    clip_files(root, "2026-10-19", stem, 10, T0)
    (root / "door" / "index.jsonl").write_text(json.dumps({"clip": "2026-01-01/gone.mp4"}) + "\n")  # the clip's line lost
    hc.mark_index_dirty(str(root / "door"))
    s = hc.ClipsSettings({})
    s.root = str(root)
    hc.apply_retention(s, str(tmp_path / "state"), T0)
    lines = [json.loads(l) for l in (root / "door" / "index.jsonl").read_text().splitlines()]
    assert [l["clip"] for l in lines] == ["2026-10-19/%s.mp4" % stem] and lines[0]["event_utc_ms"] == T0 + 3000
    assert not (root / "door" / hc.INDEX_DIRTY).exists()


def test_tc1559_a_clip_already_cut_for_the_episode_is_recorded_not_cut_twice(tmp_path, monkeypatch):
    settings, cam, seg, root = cut_setup(tmp_path, monkeypatch)
    ep = hc.build_episodes([rec(1, "approach", "event", T0)], cam.causes, 3000, 5000, 600000)[0][0]
    plan = hc.plan_window([seg], ep.window[0] / 1000.0, ep.window[1] / 1000.0, 10)
    first = hc.cut_episode(settings, cam, ep, plan, str(tmp_path), runner=FakeFfmpeg(),
                           prober=lambda c, d, n: {"frames": plan.frames}, localtime=time.gmtime)
    again = hc.cut_episode(settings, cam, ep, plan, str(tmp_path), runner=pytest.fail, prober=None,
                           localtime=time.gmtime)
    assert again == {"status": "cut", "clip": first["clip"], "recovered": True}
    assert len(list(root.rglob("*.mp4"))) == 1


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset is POSIX only; this runs under Linux/WSL")
def test_tc1557_the_offset_follows_the_zone_across_the_dst_change(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        before = 1793511000000     # 2026-11-01T05:30:00Z = 01:30 EDT
        after = before + 3600000   # 06:30Z = 01:30 EST, after the clocks went back
        a = hc.clip_stem("door", before, "approach", 9)[1]
        b = hc.clip_stem("door", after, "approach", 9)[1]
        assert a == "door_2026-11-01_01-30-00-0400_approach_9s" and b == "door_2026-11-01_01-30-00-0500_approach_9s"
        assert hc.stem_utc_ms(a) == before and hc.stem_utc_ms(b) == after
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


# ── TC-1575 the encode trial (the Owner, 2026-10-27) ─────────────────────────


class FakeTrialFfmpeg:
    """`docker run ... ffmpeg` for the trial: an encode writes a file sized by
    its bitrate and size and prints a bench line; an SSIM run prints a score."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, argv, stdout=None, stderr=None, timeout=None):
        self.calls.append(argv)
        out = argv[-1]
        if out == "-":                                   # the SSIM run
            enc = argv[argv.index("-i") + 1]
            score = 0.98 if "native" in enc else 0.93
            return subprocess.CompletedProcess(argv, 0, b"", ("[Parsed_ssim_2] SSIM Y:0.9 U:0.9 V:0.9 All:%.4f (14.1)\n"
                                                              % score).encode())
        if self.fail and self.fail in out:
            return subprocess.CompletedProcess(argv, 1, b"", b"Error while opening encoder")
        bind = argv[argv.index("-v") + 1].split(":/out")[0]
        kbps = int(argv[argv.index("-b:v") + 1][:-1])
        size = kbps * 10 // (2 if "scale_vaapi" in " ".join(argv) else 1)
        with open(os.path.join(bind, out[len("/out/"):]), "wb") as handle:
            handle.write(b"e" * size)
        return subprocess.CompletedProcess(argv, 0, b"", b"bench: utime=1.250s stime=0.250s rtime=3.000s\n")


def trial_day(tmp_path):
    root = tmp_path / "clips"
    day = root / "door" / "2026-10-19"
    day.mkdir(parents=True)
    stems = ["door_2026-10-19_12-00-00+0000_approach_20s", "door_2026-10-19_13-00-00+0000_road-stop_29s",
             "door_2026-10-19_22-00-00+0000_approach_12s", "door_2026-10-19_02-00-00+0000_approach_31s"]
    for stem in stems:
        (day / (stem + ".mp4")).write_bytes(b"c" * 10000)
        (day / (stem + ".json")).write_text("{}")
    s = hc.ClipsSettings({})
    s.root = str(root)
    return s, root, day, stems


def test_tc1575_the_sample_takes_day_and_night_shortest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(hc.time, "localtime", time.gmtime)
    _s, _root, day, stems = trial_day(tmp_path)
    assert hc.trial_sample(str(day), 2) == [(stems[0], "day"), (stems[2], "night")]
    assert [k for _s2, k in hc.trial_sample(str(day), 4)] == ["day", "day", "night", "night"]


def test_tc1575_every_combination_is_measured_one_sample_each_kept_and_no_clip_touched(tmp_path, monkeypatch):
    monkeypatch.setattr(hc.time, "localtime", time.gmtime)
    monkeypatch.setattr(hc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    s, root, day, stems = trial_day(tmp_path)
    before = {p.name: p.read_bytes() for p in day.iterdir()}
    fake = FakeTrialFfmpeg()
    probe = lambda c, d, n: {"frames": 300, "width": 1536, "height": 1536, "duration": 30.0}  # noqa: E731
    report = hc.encode_trial(s, "door", "2026-10-19", 2, runner=fake, prober=probe)
    assert {p.name: p.read_bytes() for p in day.iterdir()} == before            # nothing replaced or removed
    names = [hc.combo_name(c) for c in hc.TRIAL_COMBOS]
    assert len(names) == 9 and "hevc_1024sq_200k" in names and "h264_native_300k_5fps" in names
    trial = root / hc.TRIAL_DIR / "door-2026-10-19"
    kept = sorted(p.name for p in trial.iterdir() if p.suffix == ".mp4")
    assert kept == sorted("%s_%s.mp4" % (n, stems[0]) for n in names)          # one sample per combination
    assert not [p for p in trial.iterdir() if p.name.endswith(".part")]
    assert (trial / "trial.json").exists() and "hevc_native_300k" in (trial / "trial.txt").read_text()
    rows = report["rows"]
    assert len(rows) == 18 and all("bytes" in r and r["cpu_s"] == 1.5 and r["wall_s"] == 3.0 for r in rows)
    native = [r for r in report["summary"] if r["combo"] == "h264_native_300k" and r["kind"] == "all"][0]
    assert native["pct_of_copy"] == 30.0 and native["ssim_mean"] == 0.98
    encodes = [c for c in fake.calls if c[-1] != "-"]
    assert all(c[c.index("--cpu-shares") + 1] == "2" and "/src:ro" in " ".join(c) for c in encodes)
    five = [c for c in encodes if "5fps" in c[-1]][0]
    assert five[five.index("-vf") + 1] == "fps=5"
    hevc = [c for c in encodes if "hevc_1024sq" in c[-1]][0]
    assert hevc[hevc.index("-vf") + 1] == "scale_vaapi=w=1024:h=1024" and "hevc_vaapi" in hevc
    assert not any("format=gray" in " ".join(c) or "v360" in " ".join(c) for c in fake.calls)  # no grey, no dewarp
    ssim5 = [c for c in fake.calls if c[-1] == "-" and "5fps" in " ".join(c)][0]
    assert "[1:v]fps=5[b]" in ssim5[ssim5.index("-lavfi") + 1]


def test_tc1575_a_failure_a_full_area_a_time_budget_and_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(hc.time, "localtime", time.gmtime)
    monkeypatch.setattr(hc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    s, root, day, stems = trial_day(tmp_path)
    probe = lambda c, d, n: {"width": 1536, "height": 1536}  # noqa: E731
    report = hc.encode_trial(s, "door", "2026-10-19", 1, runner=FakeTrialFfmpeg(fail="hevc"), prober=probe)
    assert sum(1 for r in report["rows"] if "failed" in r) == 4 and "FAILED hevc" in hc.trial_table(report)
    with pytest.raises(hc.QuotaStop, match="no clip is ever removed"):
        hc.encode_trial(s, "door", "2026-10-19", 1, runner=FakeTrialFfmpeg(), prober=probe,
                        quota_fn=lambda: (900, 1000))
    ticks = iter(range(0, 10 ** 6, hc.TRIAL_RUN_SEC // 2 + 1))
    cut = hc.encode_trial(s, "door", "2026-10-19", 1, runner=FakeTrialFfmpeg(), prober=probe, clock=lambda: next(ticks))
    assert cut["stopped_early"] and len(cut["rows"]) < 9
    assert hc.clean_trials(str(root)) > 0 and not (root / hc.TRIAL_DIR).exists()
    assert sorted(p.name for p in day.iterdir()) == sorted([x + ".mp4" for x in stems] + [x + ".json" for x in stems])


def test_tc1575_the_batch_size_and_rate_knobs_default_to_unchanged(tmp_path, monkeypatch):
    plain = hc.encode_command(hc.ClipsSettings({}), "door", "/d", "a.mp4", "p")
    assert "-vf" not in plain
    both = hc.encode_command(hc.ClipsSettings({"CLIPS_ENCODE_SIZE": "1024", "CLIPS_ENCODE_FPS": "5"}), "door", "/d",
                             "a.mp4", "p")
    assert both[both.index("-vf") + 1] == "fps=5,scale_vaapi=w=1024:h=1024"
    with pytest.raises(hc.Invalid, match="CLIPS_ENCODE_SIZE"):
        hc.ClipsSettings({"CLIPS_ENCODE_SIZE": "1023"})
    # a 5 fps encode of a 30 s clip holds about 150 frames, not the copy's 300
    s, day = encode_setup(tmp_path, monkeypatch)
    s.encode_fps = 5
    side = json.load(open(os.path.join(day, "c.json")))
    side["actual"] = {"start_utc_ms": T0, "end_utc_ms": T0 + 30000}
    open(os.path.join(day, "c.json"), "w").write(json.dumps(side))
    res = hc.encode_clip(s, "door", day, "c", runner=enc_runner(400),
                         prober=lambda c, d, n: {"frames": 151, "duration": 30.0})
    assert res["kept"] is True


def test_tc1575_a_5_fps_encode_of_a_clip_with_no_span_is_judged_by_its_frames_existing(tmp_path, monkeypatch):
    s, day = encode_setup(tmp_path, monkeypatch)       # the sidecar has frames 300 and no actual window
    s.encode_fps = 5
    res = hc.encode_clip(s, "door", day, "c", runner=enc_runner(400), prober=lambda c, d, n: {"frames": 1})
    assert res["kept"] is True
    s2, day2 = encode_setup(tmp_path / "b", monkeypatch)
    s2.encode_fps = 5
    res = hc.encode_clip(s2, "door", day2, "c", runner=enc_runner(400), prober=lambda c, d, n: {"frames": 0})
    assert res["kept"] is False


# ── delta review (DeepSeek) regressions ──────────────────────────────────────


def test_tc1562_making_room_for_an_encode_never_removes_the_clip_being_encoded(tmp_path, monkeypatch):
    s, day = encode_setup(tmp_path, monkeypatch)       # the area holds only this clip
    s.root = str(tmp_path)                             # make_room must SEE it, and still not take it
    assert hc.list_clips(s.root)
    with pytest.raises(hc.QuotaStop):
        hc.encode_clip(s, "door", day, "c", runner=enc_runner(400), prober=lambda c, d, n: {"frames": 300},
                       quota_fn=lambda: (10 ** 6, 10 ** 6))
    assert os.path.exists(os.path.join(day, "c.mp4")) and os.path.exists(os.path.join(day, "c.json"))


def test_tc1575_the_trial_refuses_a_camera_that_is_not_a_name(tmp_path):
    s, _root, _day, _stems = trial_day(tmp_path)
    for camera in ("../../x", "door/..", ""):
        with pytest.raises(hc.Invalid, match="camera name"):
            hc.encode_trial(s, camera, "2026-10-19", 1, runner=pytest.fail)


def test_tc1575_the_trial_reserves_its_real_peak_and_keeps_no_broken_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(hc.time, "localtime", time.gmtime)
    monkeypatch.setattr(hc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    s, root, _day, _stems = trial_day(tmp_path)

    class Partial(FakeTrialFfmpeg):
        def __call__(self, argv, stdout=None, stderr=None, timeout=None):
            if argv[-1] != "-" and "hevc" in argv[-1]:       # a failing encoder that leaves half a file
                bind = argv[argv.index("-v") + 1].split(":/out")[0]
                open(os.path.join(bind, argv[-1][len("/out/"):]), "wb").write(b"half")
                return subprocess.CompletedProcess(argv, 1, b"", b"Error")
            return FakeTrialFfmpeg.__call__(self, argv, stdout, stderr, timeout)
    # four 10000-byte clips: the old reserve (9 x the whole sample, 360 kB) would not fit under 90 % of 200 kB
    report = hc.encode_trial(s, "door", "2026-10-19", 4, runner=Partial(), prober=lambda c, d, n: {},
                             quota_fn=lambda: (0, 200000))
    trial = root / hc.TRIAL_DIR / "door-2026-10-19"
    assert not [p for p in trial.iterdir() if p.name.startswith("hevc")]
    assert all("ssim_failed" in r for r in report["rows"] if "bytes" in r)
    assert "SSIM missing for" in hc.trial_table(report)


# ── the Opus confirmation's fixes (2026-10-27) ───────────────────────────────


def test_tc1553_an_index_timed_open_part_is_timed_by_its_own_last_write(tmp_path):
    # 7 fps in the dark, 300 s into the open segment: 2100 whole frames, the file last written at start + 300
    data = ts_bytes(2101, broken=True)
    seg = Seg("door-x.ts.part", 1000.0, part=True)
    slow = hc.Loaded(seg, data, 10, mtime=1300.0)
    assert slow.timing == "index" and slow.times[-1] == pytest.approx(1000.0 + 2099 * 300.0 / 2100, abs=0.01)
    plan = hc.plan_window([slow], 1250.0, 1290.0, 10)
    assert plan.end_covered and plan.frames == pytest.approx(40 * 7, abs=2)        # the right frames, not 400
    assert plan.rate == pytest.approx(7.0, abs=0.05)
    nominal = hc.Loaded(seg, data, 10)                                              # what the cutter did before
    assert hc.plan_window([nominal], 1250.0, 1290.0, 10) is None   # nominal fps put these frames at 1000..1210


def test_tc1553_load_segment_reads_the_last_write_of_a_real_part(tmp_path):
    path = tmp_path / "door-20261019T195000Z.ts.part"
    path.write_bytes(ts_bytes(701, broken=True))
    os.utime(str(path), (1000.0 + 100.0, 1000.0 + 100.0))
    got = hc.load_segment(Seg(str(path), 1000.0, part=True), 10, {})
    assert got.times[-1] == pytest.approx(1000.0 + 699 * 100.0 / 700, abs=0.01)


def test_tc1553_a_finished_segment_is_anchored_to_the_earlier_of_its_last_write_and_the_next_start():
    data = ts_bytes(100, broken=True)
    # the recorder stopped at 1012.5 (last write) and came back at 1020: the outage is not spread
    seg = hc.Loaded(Seg("door-x.ts", 1000.0), data, 10, next_start=1020.0, mtime=1012.5)
    assert seg.times[99] == pytest.approx(1012.375)


def test_tc1575_batch_containers_are_capped_and_the_manual_command_drops_the_capability(tmp_path, monkeypatch, capsys):
    s = hc.ClipsSettings({})
    for argv in (hc.encode_command(s, "door", "/d", "a.mp4", "p"),
                 hc.trial_encode_command(s, "door", "/d", "/o", "x", hc.TRIAL_COMBOS[0], "o.mp4"),
                 hc.trial_ssim_command("door", "/d", "/o", "x", "o.mp4", 1536, 1536, 0)):
        assert argv[argv.index("--cpus") + 1] == "0.4" and argv[argv.index("--cpu-shares") + 1] == "2"
    cut = hc.cut_command("door", "/d", "p", None)
    assert cut[cut.index("--cpus") + 1] == "1"
    assert "-p CapabilityBoundingSet=~CAP_SYS_RESOURCE" in hc.MANUAL_RUN and "-p Nice=19" in hc.MANUAL_RUN
    readme = (STACK / "clips" / "README.md").read_text(encoding="utf-8")
    assert hc.MANUAL_RUN + " encode-trial" in readme and hc.MANUAL_RUN + " encode-trial --clean" in readme
    assert "sudo python3 /opt/homehub/stack/clips/homehub_clips.py encode-trial" not in readme
    monkeypatch.setattr(hc, "guard", lambda settings: (False, "this process holds CAP_SYS_RESOURCE, which ignores"))
    env = tmp_path / "env"
    env.write_text("CLIPS_ENABLED=true\n")
    assert hc.main(["--env-file", str(env), "--state-dir", str(tmp_path / "st"), "encode-trial", "--clean"]) == hc.EXIT_GUARD
    assert hc.MANUAL_RUN in capsys.readouterr().out


def test_tc1564_room_is_never_made_by_halves_and_the_trial_goes_first(tmp_path):
    root = tmp_path / "clips"
    s = hc.ClipsSettings({})
    s.root = str(root)
    for i in range(2):
        clip_files(root, "2026-10-1%d" % i, "door_2026-10-1%d_10-00-00+0000_approach_9s" % i, 1000, T0)
    trial = root / hc.TRIAL_DIR / "door-2026-10-10"
    trial.mkdir(parents=True)
    (trial / "h264_native_300k_x.mp4").write_bytes(b"t" * 3000)

    def size():
        return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    other = {"v": 0}
    quota = lambda: (size() + other["v"], 10000)  # noqa: E731
    other["v"] = 9000 - size()
    assert hc.make_room(s, str(tmp_path), T0, 2000, quota) > 0
    assert not trial.exists() and len(list(root.rglob("*.mp4"))) == 2           # the trial went, no clip did
    other["v"] = 9990 - size()
    with pytest.raises(hc.QuotaStop, match="none was removed"):
        hc.make_room(s, str(tmp_path), T0, 5000, quota)
    assert len(list(root.rglob("*.mp4"))) == 2


def test_tc1563_retention_sweeps_stale_parts_in_the_trial_folder(tmp_path):
    root = tmp_path / "clips"
    s = hc.ClipsSettings({})
    s.root = str(root)
    trial = root / hc.TRIAL_DIR / "door-2026-10-10"
    trial.mkdir(parents=True)
    (trial / ".x.mp4.part").write_bytes(b"p")
    os.utime(str(trial / ".x.mp4.part"), (0, 0))
    (trial / "kept.mp4").write_bytes(b"k")
    assert hc.apply_retention(s, str(tmp_path / "state"), T0)["stale_parts"] == 1
    assert sorted(p.name for p in trial.iterdir()) == ["kept.mp4"]


def test_tc1560_a_segment_that_never_opens_is_lost_and_alerted_after_max_waits(tmp_path, monkeypatch):
    state, seg_dir = str(tmp_path / "state"), str(tmp_path / "segs")
    settings = hc.ClipsSettings({"CAMERA_DOOR_SEGMENT_DIR": seg_dir})
    write_segment(seg_dir, T0 / 1000.0 - 60, 1200)
    ev.append(state, rec(1, "approach", "event", T0))
    monkeypatch.setattr(hc, "load_segment", lambda *a, **k: None)
    alerts = []
    for _ in range(hc.MAX_WAITS - 1):
        assert hc.cut_camera(settings, settings.cameras[0], state, T0 + 600000)["waiting"] == 1
    counts = hc.cut_camera(settings, settings.cameras[0], state, T0 + 600000, alert=lambda *a: alerts.append(a))
    assert counts["lost"] == 1 and alerts and alerts[0][0] == "clips-lost-door"
    ledger = json.load(open(hc.ledger_path(state, "door")))
    assert ledger["episodes"]["hub-motion:r.1"]["status"] == "lost" and ledger["waits"] == {}


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows; runs under Linux/WSL")
def test_tc1563_no_listing_descends_through_a_symlink(tmp_path):
    root = tmp_path / "clips"
    outside = tmp_path / "elsewhere" / "2026-10-10"
    outside.mkdir(parents=True)
    (outside / "door_2026-10-10_10-00-00+0000_approach_9s.mp4").write_bytes(b"x")
    (root / "door").mkdir(parents=True)
    os.symlink(str(outside), str(root / "door" / "2026-10-10"))
    os.symlink(str(tmp_path / "elsewhere"), str(root / "garage"))
    assert hc.camera_dirs(str(root)) == ["door"] and hc._days(str(root / "door")) == []
    assert hc.list_clips(str(root)) == []


@pytest.mark.release
@pytest.mark.skipif(not (SEGMENTS and os.path.isdir(SEGMENTS) and os.name == "posix" and docker_ok()),
                    reason="set HOMEHUB_CLIPS_SEGMENTS to a door segment tree on a Linux docker host (WSL)")
def test_tc1569_an_open_part_cut_from_a_real_segment_has_the_planned_frames(tmp_path, monkeypatch):
    """The usual case for a cut: the window ends in the segment still being
    written. A real segment is cut back to its first half (at a packet edge),
    renamed .part and given the last-write time the recorder would have left
    (its start + its frames' span), and a visit is cut from inside it."""
    monkeypatch.setattr(hc, "guard", lambda settings: (True, "ok"))
    real = [x for x in hc.list_segments(SEGMENTS, "door") if not x.part]
    assert real, "no segments"
    src = real[0]
    data = open(src.path, "rb").read()
    whole = hc.Loaded(src, data, 10)
    half = whole.idx.offsets[len(whole.idx.offsets) // 2]
    seg_dir = tmp_path / "segs"
    day = time.strftime("%Y-%m-%d", time.gmtime(src.start))
    (seg_dir / day).mkdir(parents=True)
    part = seg_dir / day / (src.name + ".part")
    part.write_bytes(data[:half])
    frames = len(whole.idx.offsets) // 2
    span = frames / 10.0 if whole.timing == "index" else whole.times[frames - 1] - whole.times[0] + 0.1
    os.utime(str(part), (src.start + span, src.start + span))
    root = tmp_path / "clips"
    root.mkdir()
    state = str(tmp_path / "state")
    s = hc.ClipsSettings({"CAMERA_DOOR_SEGMENT_DIR": str(seg_dir)})
    s.root = str(root)
    event = int((src.start + span * 0.6) * 1000)
    ev.append(state, rec(1, "approach", "event", event))
    ev.append(state, rec(2, "visitor", "renewal", event + 3000))
    counts = hc.cut_camera(s, s.cameras[0], state, event + 60000)
    assert counts["cut"] == 1 and counts["failed"] == 0, counts
    side_path = next(root.rglob("*.json"))
    side = json.loads(side_path.read_text())
    assert side["coverage"] == "full" and 100 <= side["frames"] <= 130, side
    mp4 = side_path.with_suffix(".mp4")
    done = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", "%s:/c.mp4:ro" % mp4,
                           "--entrypoint", "ffmpeg", hc.door_relay.image_ref(), "-v", "error", "-i", "/c.mp4",
                           "-f", "null", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    assert done.returncode == 0 and not done.stderr.strip(), done.stderr[-300:]


# ── the final Opus check's refinements (2026-10-27) ──────────────────────────


def test_tc1553_a_3_fps_night_part_is_timed_by_its_last_write_not_the_nominal_rate():
    data = ts_bytes(901, broken=True)                       # 900 whole frames in 300 s: 3 fps
    slow = hc.Loaded(Seg("door-x.ts.part", 1000.0, part=True), data, 10, mtime=1300.0)
    assert slow.times[-1] == pytest.approx(1000.0 + 899 * 300.0 / 900, abs=0.01)
    plan = hc.plan_window([slow], 1250.0, 1290.0, 10)
    assert plan.end_covered and plan.frames == pytest.approx(120, abs=2) and plan.rate == pytest.approx(3.0, abs=0.05)
    # far slower than anything plausible is clamped to MIN_INDEX_FPS, not sent back to the nominal rate
    crawl = hc.Loaded(Seg("door-y.ts.part", 1000.0, part=True), data, 10, mtime=1000.0 + 900 * 10)
    assert crawl.period == pytest.approx(1.0 / hc.MIN_INDEX_FPS)
    # anchored by the next start, under half the nominal may hide a restart: the nominal rate, as before
    hidden = hc.Loaded(Seg("door-z.ts", 1000.0), data, 10, next_start=1300.0)
    assert hidden.period == pytest.approx(0.1)


def test_tc1560_waits_are_kept_only_for_episodes_that_still_exist(tmp_path, monkeypatch):
    state, seg_dir = str(tmp_path / "state"), str(tmp_path / "segs")
    settings = hc.ClipsSettings({"CAMERA_DOOR_SEGMENT_DIR": seg_dir})
    write_segment(seg_dir, T0 / 1000.0 - 60, 1200)
    ev.append(state, rec(1, "approach", "event", T0))
    import monitor_common as mc
    mc.save_json_atomic(hc.ledger_path(state, "door"), {"since_ms": T0, "episodes": {},
                                                        "waits": {"hub-motion:gone": 4}})
    monkeypatch.setattr(hc, "load_segment", lambda *a, **k: None)
    hc.cut_camera(settings, settings.cameras[0], state, T0 + 600000)
    assert json.load(open(hc.ledger_path(state, "door")))["waits"] == {"hub-motion:r.1": 1}


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows; runs under Linux/WSL")
def test_tc1563_nothing_under_the_trial_or_a_named_camera_is_followed_through_a_link(tmp_path):
    root = tmp_path / "clips"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "precious.mp4").write_bytes(b"p" * 100)
    (outside / "2026-10-10").mkdir()
    (outside / "2026-10-10" / "door_2026-10-10_10-00-00+0000_x_9s.mp4").write_bytes(b"c" * 100)
    trial = root / hc.TRIAL_DIR
    trial.mkdir(parents=True)
    os.symlink(str(outside), str(trial / "door-2026-10-10"))
    assert hc.trial_bytes(str(root)) == 0
    hc.clean_trials(str(root))
    assert (outside / "precious.mp4").exists() and not os.path.lexists(str(trial / "door-2026-10-10"))
    os.symlink(str(outside), str(root / "door"))
    assert hc.list_clips(str(root), ["door"]) == []
    (root / "garage" / "2026-10-10").mkdir(parents=True)
    os.symlink(str(outside / "precious.mp4"), str(root / "garage" / "2026-10-10" / "garage_2026-10-10_10-00-00+0000_x_9s.mp4"))
    assert hc.list_clips(str(root), ["garage"]) == []


@pytest.mark.release
@pytest.mark.skipif(not (SEGMENTS and os.path.isdir(SEGMENTS) and os.name == "posix" and docker_ok()),
                    reason="set HOMEHUB_CLIPS_SEGMENTS to a door segment tree on a Linux docker host (WSL)")
def test_tc1569_real_segments_with_the_recorders_mtimes_cut_the_planned_frames(tmp_path, monkeypatch):
    """The real segments copied with the mtimes the recorder leaves (a rotated
    segment's last write lands ~0.6 s after the next one's start), so the last-
    write anchor is exercised on real bytes."""
    monkeypatch.setattr(hc, "guard", lambda settings: (True, "ok"))
    real = [x for x in hc.list_segments(SEGMENTS, "door") if not x.part]
    seg_dir = tmp_path / "segs"
    for i, seg in enumerate(real):
        day = time.strftime("%Y-%m-%d", time.gmtime(seg.start))
        (seg_dir / day).mkdir(parents=True, exist_ok=True)
        dst = seg_dir / day / seg.name
        shutil.copyfile(seg.path, str(dst))
        nxt = real[i + 1].start if i + 1 < len(real) and real[i + 1].start - seg.start <= 600 else seg.start + 600
        os.utime(str(dst), (nxt + 0.6, nxt + 0.6))   # what the recorder leaves: just after the next start
    root = tmp_path / "clips"
    root.mkdir()
    state = str(tmp_path / "state")
    s = hc.ClipsSettings({"CAMERA_DOOR_SEGMENT_DIR": str(seg_dir)})
    s.root = str(root)
    events = [int((seg.start + 400) * 1000) for seg in real]
    for i, ms in enumerate(events):
        ev.append(state, rec(i, "approach", "event", ms))
        ev.append(state, rec(100 + i, "visitor", "renewal", ms + 3000))
    counts = hc.cut_camera(s, s.cameras[0], state, max(events) + 20 * 60 * 1000)
    assert counts["cut"] == len(events) and counts["failed"] == 0, counts
    for side in root.rglob("*.json"):
        data = json.loads(side.read_text())
        assert data["coverage"] == "full" and 100 <= data["frames"] <= 130, data


# ── the Opus check of 3ef0ceb (2026-10-27) ───────────────────────────────────


def test_tc1553_a_rotated_3_fps_segment_whose_mtime_lands_just_after_the_next_start_is_timed_by_it():
    data = ts_bytes(901, broken=True)
    # the recorder writes the last bytes, then opens the next file at floor(now): mtime 0.6 s after it
    night = hc.Loaded(Seg("door-x.ts", 1000.0), data, 10, next_start=1300.0, mtime=1300.6)
    n = len(night.times)
    assert night.period == pytest.approx(300.0 / n) and night.times[-1] < 1300.0
    # touched long after the rotation: only the next start is known, and under half the nominal is nominal
    touched = hc.Loaded(Seg("door-x.ts", 1000.0), data, 10, next_start=1300.0, mtime=1400.0)
    assert touched.period == pytest.approx(0.1)


def test_tc1553_the_newest_finished_segment_is_not_stretched_by_a_copied_mtime():
    data = ts_bytes(901, broken=True)
    copied = hc.Loaded(Seg("door-x.ts", 1000.0), data, 10, mtime=1000.0 + 5000, segment_sec=600)
    assert copied.times[-1] < 1000.0 + 600 + hc.ROTATION_SLACK_SEC
    assert hc.ClipsSettings({"DOOR_RECORDER_SEGMENT_SEC": "300"}).segment_sec == 300


def test_tc1553_an_anchor_above_the_nominal_rate_is_a_bound_the_frames_never_pass():
    data = ts_bytes(100, broken=True)
    idx = hc.scan_frames(data, False)
    times, _mode = hc.frame_times(idx, 1000.0, 10, end=1005.0)        # 100 frames before a start 5 s later
    assert times[-1] < 1005.0 and times == sorted(times)
    a = hc.Loaded(Seg("door-a.ts", 1000.0), data, 10, next_start=1005.0)
    b = hc.Loaded(Seg("door-b.ts", 1005.0), data, 10, next_start=1015.0)
    flat = a.times + b.times
    assert all(x < y for x, y in zip(flat, flat[1:]))
    assert hc.frame_times(idx, 1000.0, 10, end=990.0)[0][-1] == pytest.approx(1009.9)   # a clock step back: nominal


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows; runs under Linux/WSL")
def test_tc1563_the_stale_sweep_reads_links_themselves_not_their_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "guard", lambda settings: (True, "ok"))
    root = tmp_path / "clips"
    day = root / "door" / "2026-10-19"
    day.mkdir(parents=True)
    old = tmp_path / "old-target"
    old.write_bytes(b"o")
    os.utime(str(old), (0, 0))
    os.symlink(str(old), str(day / "door_x.mp4.part"))           # a new link to an old file: not stale
    (day / "door_y.json").write_text("{}")
    os.utime(str(day / "door_y.json"), (0, 0))
    os.symlink(str(tmp_path / "gone"), str(day / "door_y.mp4"))   # its clip is a (dangling) link: not an orphan
    s = hc.ClipsSettings({"CLIPS_RETENTION_DAYS": "3650"})
    s.root = str(root)
    hc.apply_retention(s, str(tmp_path / "state"), int(time.time() * 1000))
    assert os.path.lexists(str(day / "door_x.mp4.part")) and (day / "door_y.json").exists() and old.exists()
