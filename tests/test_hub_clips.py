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
T0 = 1793044200000  # 2026-10-26T19:50:00Z


@pytest.fixture(autouse=True)
def no_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))


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
    assert names == ["hub-motion-2026-10-25.jsonl", "hub-motion-2026-10-26.jsonl"]
    with open(tmp_path / "events" / "door" / names[1], "a") as handle:
        handle.write('{"torn": \n')
        handle.write(json.dumps(rec(9, "approach", "event", T0, camera="garage")) + "\n")
    got, rejected = ev.read(state, "door", ["hub-motion"], day1)
    assert [r["id"] for r in got] == ["r.1", "r.2", "r.3"] and rejected == 1
    assert ev.read(state, "door", ["onvif"], day1) == ([], 0)
    assert [r["id"] for r in ev.read(state, "door", ["hub-motion"], T0)[0]] == ["r.2", "r.3"]
    removed = ev.prune_days(state, ["door"], 1, T0 / 1000.0 + 86400)
    assert [os.path.basename(p) for p in removed] == ["hub-motion-2026-10-25.jsonl"]


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
    assert hc.readiness(we + hc.SETTLE_MS, ep, None, T0 / 1000.0 + 60) == "lost: segments pruned"
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
    assert day == "2026-10-26" and stem.startswith("door_2026-10-26_14-51-01") and stem.endswith("_road-stop_29s")
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
    assert result == {"status": "cut", "clip": "2026-10-26/door_2026-10-26_19-50-00+0000_approach_11s.mp4"} or \
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
    monkeypatch.setattr(hc, "guard", lambda settings: (False, "not mounted"))
    with pytest.raises(hc.GuardStop):
        hc.cut_episode(settings, cam, ep, plan, str(tmp_path), runner=pytest.fail, prober=None)


# ── TC-1560 one camera's pass: ledger, overlap, retries, loss ────────────────


def write_segment(seg_dir, start_epoch, frames, part=False):
    day = time.strftime("%Y-%m-%d", time.gmtime(start_epoch))
    name = time.strftime("door-%Y%m%dT%H%M%SZ.ts", time.gmtime(start_epoch)) + (".part" if part else "")
    os.makedirs(os.path.join(seg_dir, day), exist_ok=True)
    pts = 900000 + int((start_epoch - 1793040000) * 90000)
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
    argv = hc.encode_command(s, "door", "/srv/scratch/clips/door/2026-10-26", "a.mp4", "a.enc.mp4.part")
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
    day = tmp_path / "door" / "2026-10-26"
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
    old_day = root / "door" / "2026-10-20"
    new_day = root / "door" / "2026-10-26"
    for day, stem, start, size in ((old_day, "door_old", T0 - 6 * 86400000, 700 * 1024),
                                   (new_day, "door_new", T0, 500 * 1024)):
        day.mkdir(parents=True)
        (day / (stem + ".mp4")).write_bytes(b"x" * size)
        (day / (stem + ".json")).write_text(json.dumps({"v": 1, "actual": {"start_utc_ms": start}}))
    (new_day / "door_crash.mp4.part").write_bytes(b"p")
    os.utime(new_day / "door_crash.mp4.part", (0, 0))
    (root / "door" / "index.jsonl").write_text(
        json.dumps({"clip": "2026-10-20/door_old.mp4"}) + "\n" + json.dumps({"clip": "2026-10-26/door_new.mp4"}) + "\n")
    ev.append(str(state), rec(1, "approach", "event", T0 - 30 * 86400000))
    result = hc.apply_retention(s, str(state), T0)
    assert result["removed"] == 1 and result["stale_parts"] == 1 and result["event_days_removed"] == 1
    assert not old_day.exists() and sorted(os.listdir(new_day)) == ["door_new.json", "door_new.mp4"]
    assert [json.loads(l)["clip"] for l in (root / "door" / "index.jsonl").read_text(encoding="utf-8").splitlines()] == [
        "2026-10-26/door_new.mp4"]


def test_tc1564_a_quota_stop_holds_the_cutter_until_retention_makes_room(tmp_path, monkeypatch):
    state = str(tmp_path / "state")
    env = tmp_path / "env"
    env.write_text("CLIPS_ENABLED=true\nCAMERA_DOOR_SEGMENT_DIR=%s\n" % (tmp_path / "segs"))
    hc.set_quota_stop(state, "Disk quota exceeded", T0)
    monkeypatch.setattr(hc, "cut_camera", lambda *a, **k: pytest.fail("the cutter ran while stopped"))
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
    assert s.quota_mib["clips"] == 1024


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
                       ("CAMERA_MOTION_BUDGET_PCT", "25"), ("HOMEHUB_SCRATCH_QUOTA_CLIPS_MIB", "1024")):
        assert env.get(key) == value, key
    s = hc.ClipsSettings(env)
    assert s.cameras[0].causes == ["approach", "road-stop"]


# ── TC-1568 status ───────────────────────────────────────────────────────────


def test_tc1568_status_reports_events_clips_backlog_and_the_detector(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "clips"
    s = hc.ClipsSettings({"CLIPS_ENABLED": "true"})
    s.root = str(root)
    monkeypatch.setattr(hc, "clips_quota", lambda settings: (10 * MIB, 1024 * MIB))
    ev.append(str(state), rec(1, "approach", "event", T0 - 1000))
    day = root / "door" / "2026-10-26"
    day.mkdir(parents=True)
    (day / "c.mp4").write_bytes(b"x" * 100)
    (day / "c.json").write_text(json.dumps({"v": 1, "actual": {"start_utc_ms": T0 - 60000}, "encode": None}))
    import monitor_common as mc
    mc.save_json_atomic(str(state / "motion" / "door.json"), {"frames": 5})
    out = hc.status(s, str(state), T0)
    door = out["cameras"]["door"]
    assert out["quota"] == {"used_mib": 10, "limit_mib": 1024} and out["clips"] == 1
    assert door["events_24h"] == 1 and door["last_clip"] == "2026-10-26/c.mp4" and door["last_clip_age_min"] == 1.0
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
