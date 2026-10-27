"""The hub's camera motion detector (SR-128): the pinned engine, its knobs, its
decoder, which signals reach the IF-076 store, replay, cost, and the public
repository carrying no private zone.

What runs here, on the dev PC: everything but a live relay. The engine is the
REAL pinned engine (numpy + OpenCV; skipped where they are absent) fed
synthetic frames, so the spooler is tested against what the engine actually
emits, not against a double. The decoder's filter is compared with the panel's
own (OfficeWallNaglight doorstream/service.py motion_filter) when the sibling
checkout is present.

What does not: the live relay, VA-API, and the CPU cost - those are the deploy
steps on the hub (stack/clips/README.md), TC-1571..TC-1573.
Implements: TC-1543..TC-1550, TC-1567.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
for sub in ("camera-motion", "clips", "door", "scratch", "monitoring"):
    sys.path.insert(0, str(STACK / sub))
import camera_motion as cm  # noqa: E402
import clip_events as ev  # noqa: E402
import door_relay as dr  # noqa: E402

OWN_REPO = Path(os.environ.get("OWN_REPO") or ROOT.parent / "OfficeWallNaglight")
HOMEHUB_REPO = Path(os.environ.get("HOMEHUB_REPO") or ROOT.parent / "HomeHub")
PASSWORD = "c@m p$ss:1 #x"
ZONE = '"0.1,0.1;0.9,0.1;0.9,0.9;0.1,0.9"'  # a test zone: not a property's

try:
    import cv2  # noqa: F401
    import numpy as np
    HAVE_CV = True
except ImportError:  # pragma: no cover
    HAVE_CV = False
needs_cv = pytest.mark.skipif(not HAVE_CV, reason="numpy/OpenCV absent: the engine cannot run here")


@pytest.fixture(autouse=True)
def no_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))


def write_env(tmp_path, **over):
    lines = {"LAN_IP": "192.0.2.10", "DOOR_RELAY_UPSTREAM_HOST": "192.0.2.20",
             "DOOR_RELAY_UPSTREAM_PASSWORD": '"c@m p$$ss:1 #x"', "CAMERAS": "door",
             "CAMERA_DOOR_MOTION_ENABLED": "true", "CAMERA_DOOR_MOTION_CALIBRATED": "true",
             "CAMERA_DOOR_MOTION_APPROACH_POLYGON": ZONE}
    lines.update(over)
    path = tmp_path / "env"
    path.write_text("".join("%s=%s\n" % kv for kv in lines.items() if kv[1] is not None))
    return str(path)


# ── TC-1543 the pinned engine ────────────────────────────────────────────────


def test_tc1543_the_engine_is_the_pinned_bytes_and_an_edited_copy_is_refused(tmp_path):
    pin = cm.read_pin()
    assert pin["SHA256"] == cm.engine_sha256()
    assert pin["SOURCE_REPO"] == "OfficeWallNaglight" and pin["PATH"] == "doorstream/motion.py"
    assert re.fullmatch(r"[0-9a-f]{40}", pin["COMMIT"]) and re.fullmatch(r"[0-9a-f]{40}", pin["LAST_CHANGE"])
    edited = tmp_path / "motion_engine.py"
    data = (STACK / "camera-motion" / "motion_engine.py").read_bytes()
    edited.write_bytes(data.replace(b"DIFFERENCE_THRESHOLD = 18", b"DIFFERENCE_THRESHOLD = 17"))
    with pytest.raises(cm.Invalid, match="not the pinned engine"):
        cm.load_engine(str(edited), cm.PIN_FILE)
    assert b"\r\n" not in data
    # The public copy carries no comment and no date (the Owner's ruling, 2026-10-27).
    text = data.decode("utf-8")
    import io
    import tokenize
    assert not [t for t in tokenize.generate_tokens(io.StringIO(text).readline) if t.type == tokenize.COMMENT]
    # dates and clock times in the TEXT the file carries (its strings): code such as a slice is not a time
    strings = " ".join(t.string for t in tokenize.generate_tokens(io.StringIO(text).readline)
                       if t.type == tokenize.STRING)
    assert not re.search(r"20\d\d-\d\d-\d\d|\b\d{1,2}:\d\d(:\d\d)?\b|\b\d{1,2}/\d{1,2}/20\d\d\b", strings)


def code_only(source):
    """The program without its comments and docstrings: ast.dump of the tree
    with every module/class/function docstring removed (comments never reach
    the tree), and the token stream with COMMENT/NL tokens and docstring
    strings dropped. Two files with equal results run the same code."""
    import ast
    import io
    import tokenize
    tree = ast.parse(source)
    doc_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                    and isinstance(first.value.value, str):
                doc_lines.add(first.lineno)
                node.body = node.body[1:] or [ast.Pass()]
    tokens = [(t.type, t.string) for t in tokenize.generate_tokens(io.StringIO(source).readline)
              if t.type not in (tokenize.COMMENT, tokenize.NL)
              and not (t.type == tokenize.STRING and t.start[0] in doc_lines)]
    # A removed docstring leaves its NEWLINE behind; drop NEWLINEs that follow a NEWLINE or INDENT.
    collapsed = []
    for tok in tokens:
        # (a NEWLINE first in the file is what a removed module docstring leaves)
        if tok[0] == tokenize.NEWLINE and (not collapsed or collapsed[-1][0] in (tokenize.NEWLINE, tokenize.INDENT)):
            continue
        collapsed.append(tok)
    return ast.dump(tree), collapsed


@pytest.mark.skipif(not (OWN_REPO / ".git").exists() or not shutil.which("git"),
                    reason="sibling OfficeWallNaglight checkout absent; set OWN_REPO")
def test_tc1543_the_scrubbed_engine_is_the_pinned_commits_code():
    pin = cm.read_pin()
    shown = subprocess.run(["git", "-C", str(OWN_REPO), "show", "%s:%s" % (pin["COMMIT"], pin["PATH"])],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert shown.returncode == 0, shown.stderr[-300:]
    panel = shown.stdout.decode("utf-8")
    hub = (STACK / "camera-motion" / "motion_engine.py").read_text(encoding="utf-8")
    assert code_only(hub) == code_only(panel), "the hub's engine is not the panel's code"
    assert hub != panel  # the scrubbed copy, not the original
    last = subprocess.run(["git", "-C", str(OWN_REPO), "log", "-1", "--format=%H", pin["COMMIT"], "--", pin["PATH"]],
                          stdout=subprocess.PIPE, universal_newlines=True, timeout=60).stdout.strip()
    assert last == pin["LAST_CHANGE"]


# ── TC-1544 the knobs, through the engine's own parser ───────────────────────


@needs_cv
def test_tc1544_the_camera_knobs_reach_the_engine_and_placeholder_zones_refuse(tmp_path):
    settings, _engine, config = cm.prepare(write_env(tmp_path, CAMERA_DOOR_MOTION_SAMPLE_FPS="4"), "door")
    assert config.eligible and config.sample_fps == 4.0 and config.road_polygon is None and not config.diagnostics
    assert settings.decode == "software" and settings.stream == "door"
    for over in ({"CAMERA_DOOR_MOTION_APPROACH_POLYGON": '"0,0;1,0;1,1;0,1"'}, {"CAMERA_DOOR_MOTION_CALIBRATED": None},
                 {"CAMERA_DOOR_MOTION_APPROACH_POLYGON": None}):
        with pytest.raises(cm.Invalid, match="placeholder"):
            cm.prepare(write_env(tmp_path, **over), "door")
    with pytest.raises(cm.Invalid, match="CAMERA_DOOR_MOTION_.*approach polygon") as err:
        cm.prepare(write_env(tmp_path, CAMERA_DOOR_MOTION_APPROACH_POLYGON='"0.1,0.1;0.2,0.2"'), "door")
    assert "0.1" not in str(err.value)
    for over, key in (({"CAMERA_DOOR_MOTION_DECODE": "cuda"}, "MOTION_DECODE"),
                      ({"CAMERA_DOOR_MOTION_RELAY_STREAM": "Door Cam"}, "RELAY_STREAM"),
                      ({"CAMERA_VAAPI_DEVICE": "/dev/sda"}, "CAMERA_VAAPI_DEVICE")):
        with pytest.raises(cm.Invalid, match=key):
            cm.MotionSettings(cm.mc.read_knobs(write_env(tmp_path, **over)), "door")
    with pytest.raises(cm.Invalid):
        cm.MotionSettings({}, "Door")


@needs_cv
def test_tc1544_check_prints_no_polygon(tmp_path, capsys):
    assert cm.main(["--env-file", write_env(tmp_path, CAMERA_DOOR_MOTION_ROAD_POLYGON='"0.2,0.5;0.8,0.5;0.8,0.55"'),
                    "check", "--camera", "door"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["eligible"] and out["road_zone"] and out["engine"] == cm.read_pin()["COMMIT"]
    assert "0.1" not in json.dumps(out) and "0.55" not in json.dumps(out)
    # the pre-flight works with the run knob still off; the run itself still refuses
    assert cm.main(["--env-file", write_env(tmp_path, CAMERA_DOOR_MOTION_ENABLED="false"), "check", "--camera", "door"]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is False
    with pytest.raises(cm.Invalid, match="placeholder"):
        cm.prepare(write_env(tmp_path, CAMERA_DOOR_MOTION_ENABLED="false"), "door")


# ── TC-1545 the decoder: the panel's raster, from the relay, credential on stdin ──


def test_tc1545_the_decoder_is_a_relay_consumer_with_the_panels_filter(tmp_path):
    env = write_env(tmp_path)
    relay = dr.RelaySettings(env)
    settings = cm.MotionSettings(cm.mc.read_knobs(env), "door")
    argv, payload = cm.decoder_command(relay, settings, 5.0)
    joined = " ".join(argv)
    assert PASSWORD not in joined and "c%40m" not in joined and "192.0.2.20" not in joined
    assert b"rtsp://admin:c%40m%20p%24ss%3A1%20%23x@192.0.2.10:8554/door" in payload
    assert argv[argv.index("-vf") + 1] == "fps=5,scale=256:256:flags=area,format=gray"
    assert argv[argv.index("--name") + 1] == "homehub-camera-motion-door"
    assert argv[argv.index("--cpu-shares") + 1] == "256" and "--device" not in argv and "-hwaccel" not in argv
    assert argv[-5:] == ["-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    settings.decode = "vaapi"
    argv, _ = cm.decoder_command(relay, settings, 5.0)
    assert argv[argv.index("--device") + 1] == "/dev/dri/renderD128"
    assert argv.index("-hwaccel") < argv.index("pipe:0")  # an input option of the relay input
    assert "scale_vaapi=w=256:h=256" in argv[argv.index("-vf") + 1]


@pytest.mark.skipif(not (OWN_REPO / "doorstream" / "service.py").is_file(),
                    reason="sibling OfficeWallNaglight checkout absent; set OWN_REPO")
def test_tc1545_the_software_filter_is_the_panels_character_for_character():
    sys.path.insert(0, str(OWN_REPO))
    try:
        from doorstream import service
    finally:
        sys.path.remove(str(OWN_REPO))

    class Motion:
        sample_fps = 5.0

    class Config:
        motion = Motion()
    for fps in (5.0, 2.5, 10.0):
        Motion.sample_fps = fps
        assert cm.software_filter(fps) == service.motion_filter(Config())


# ── TC-1546 / TC-1547 which signals reach the store ──────────────────────────


def test_tc1546_events_at_once_renewals_throttled_and_the_last_one_kept():
    written = []
    spool = cm.SignalSpooler(lambda cause, kind, utc: written.append((cause, kind, utc)))
    spool.signal("road-stop", 0.0, 1000)
    for t in range(1, 26):                       # road-hold renewals every second for 25 s
        spool.signal("road-hold", float(t), 1000 + t * 1000)
        spool.tick(float(t) + 0.5)
    for t in range(26, 40):
        spool.tick(float(t))
    times = [w[2] for w in written]
    assert written[0] == ("road-stop", "event", 1000)
    assert times[-1] == 26000 and written[-1][1] == "renewal"   # the last renewal, flushed after the lease
    assert max(b - a for a, b in zip(times, times[1:])) < 5000  # never a hole the cutter would split on
    assert len(written) < 12                                     # and not every beat
    spool.signal("approach", 50.0, 60000)
    spool.signal("visitor", 51.0, 61000)
    spool.flush()
    assert written[-2:] == [("approach", "event", 60000), ("visitor", "renewal", 61000)]
    with pytest.raises(ValueError):
        cm.SignalSpooler(lambda *a: None, lease=5.0, throttle=4.0)


@needs_cv
def test_tc1547_the_real_engine_through_the_spooler_writes_one_unbroken_window(tmp_path):
    engine = cm.load_engine()
    config = engine.motion_config({"DOORBELL_MOTION_ENABLED": "true", "DOORBELL_MOTION_CALIBRATED": "true",
                                   "DOORBELL_MOTION_APPROACH_POLYGON": ZONE.strip('"')})
    motion = engine.MotionEngine(config)
    state = str(tmp_path)
    writer = cm.EventWriter(state, "door", clock=lambda: 1793044200.0)
    spool = cm.SignalSpooler(writer.emit)
    rng = np.random.default_rng(1)
    background = (120 + rng.integers(0, 6, (256, 256))).astype(np.uint8)
    base_ms = 1793044200000
    for i in range(120):
        frame = background.copy()
        if 20 <= i < 45:                          # a dark figure crosses the zone for 5 s
            x = 60 + (i - 20) * 3
            frame[100:170, x:x + 25] = 30
        now = i * 0.2
        fired, _ = motion.process(frame.tobytes(), now)
        if fired and motion.cause:
            spool.signal(motion.cause, now, base_ms + int(now * 1000))
        spool.tick(now)
    records, rejected = ev.read(state, "door", ["hub-motion"], base_ms - 1000)
    assert rejected == 0 and records[0]["cause"] == "approach" and records[0]["kind"] == "event"
    assert all(r["kind"] == "renewal" and r["cause"] in ("visitor", "bridge") for r in records[1:])
    assert len({r["id"] for r in records}) == len(records)
    sys.path.insert(0, str(STACK / "clips"))
    import homehub_clips as hc
    episodes, orphans = hc.build_episodes(records, ["approach", "road-stop"], 3000, 5000, 600000)
    assert len(episodes) == 1 and orphans == 0
    last_signal = max(int(r["utc_ms"]) for r in records)
    assert episodes[0].window == (int(records[0]["utc_ms"]) - 3000, last_signal + 5000)
    assert writer.counts["event"] == 1 and writer.counts["failed"] == 0


# ── TC-1548 replay ───────────────────────────────────────────────────────────


def test_tc1548_replay_decodes_by_index_without_retiming_read_only_and_offline():
    class S:
        decode, stream_fps, device = "software", 10, "/dev/dri/renderD128"
    argv = cm.replay_command("/srv/scratch/door/2026-10-26/door-x.ts", S, 5.0, "img@sha256:x")
    assert argv[argv.index("-vf") + 1] == "select=not(mod(n\\,2)),scale=256:256:flags=area,format=gray"
    assert argv[argv.index("-fps_mode") + 1] == "passthrough" and argv.index("-fps_mode") > argv.index("-vf")
    assert "/srv/scratch/door/2026-10-26/door-x.ts:/seg.ts:ro" in argv and argv[argv.index("--network") + 1] == "none"
    S.decode = "vaapi"
    argv = cm.replay_command("/x.ts", S, 5.0, "img")
    assert "--device" in argv and "scale_vaapi" in argv[argv.index("-vf") + 1]
    seen = []

    class Engine:
        cause = "approach"

        def process(self, frame, now):
            seen.append(now)
            return len(seen) == 2, None
    signals = []
    last = cm.replay_frames(Engine(), [b"a", b"b", b"c"], 100.0, 0.2, 100.1, lambda t, c: signals.append((t, c)))
    assert seen == [100.101, 100.2, pytest.approx(100.4)] and last == pytest.approx(100.4)
    assert signals == [(100.2, "approach")]


# ── TC-1549 cost ─────────────────────────────────────────────────────────────


def test_tc1549_the_cost_verdict_sums_decoder_and_engine_against_the_budget():
    ok = cm.cost_verdict({"decoder": {"cpu_pct_one_core": 17.7}, "detector": {"cpu_pct_one_core": 4.1}}, 25)
    assert ok == {"total_cpu_pct_one_core": 21.8, "budget_pct": 25, "pass": True}
    over = cm.cost_verdict({"decoder": {"cpu_pct_one_core": 20.0}, "detector": {"cpu_pct_one_core": 5.5}}, 25)
    assert not over["pass"]
    gone = cm.cost_verdict({"decoder": {"missing": "/x"}, "detector": {"cpu_pct_one_core": 1.0}}, 25)
    assert gone["pass"] is False and "not running" in gone["reason"]


# ── TC-1550 the knob, and refusals that do not loop ──────────────────────────


def test_tc1550_condition_needs_the_knob_and_the_camera_listed(tmp_path):
    assert cm.main(["--env-file", write_env(tmp_path), "condition", "--camera", "door"]) == 0
    assert cm.main(["--env-file", write_env(tmp_path, CAMERA_DOOR_MOTION_ENABLED="false"), "condition", "--camera", "door"]) == 1
    assert cm.main(["--env-file", write_env(tmp_path, CAMERAS="garage"), "condition", "--camera", "door"]) == 1
    assert cm.main(["--env-file", write_env(tmp_path, CAMERA_DOOR_MOTION_ENABLED="yes"), "condition", "--camera", "door"]) == 1


def test_tc1550_run_refuses_with_an_alert_and_starts_no_decoder(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(cm.mc, "send_alert", lambda key, *a, **k: sent.append(key))
    monkeypatch.setattr(cm.door_relay, "start_consumer", lambda *a, **k: pytest.fail("a decoder was started"))
    env = write_env(tmp_path, CAMERA_DOOR_MOTION_APPROACH_POLYGON='"0,0;1,0;1,1;0,1"')
    if HAVE_CV:
        assert cm.run_service(env, "door", str(tmp_path / "state")) == cm.EXIT_REFUSED
        assert sent == ["camera-motion-door"]
    engine_copy = tmp_path / "e.py"
    engine_copy.write_bytes(b"# not the engine\n")
    monkeypatch.setattr(cm, "ENGINE_FILE", str(engine_copy))
    sent.clear()
    assert cm.run_service(write_env(tmp_path), "door", str(tmp_path / "state")) == cm.EXIT_REFUSED
    assert sent == ["camera-motion-door"]
    unit = (STACK / "camera-motion" / "homehub-camera-motion@.service").read_text(encoding="utf-8")
    assert "RestartPreventExitStatus=%d" % cm.EXIT_REFUSED in unit


# ── TC-1567 the public repository carries no private zone ────────────────────


def private_polygons():
    found = set()
    for name in ("config.homehub.psd1", "config.wall.psd1", "config.common.psd1"):
        path = HOMEHUB_REPO / "scripts" / "deploy" / name
        if path.is_file():
            text = path.read_text(encoding="utf-8-sig")
            for value in re.findall(r"(?m)^\s*[A-Z_]*POLYGONS?\s*=\s*'\"?([^'\"]*)\"?'", text):
                for part in value.split("|"):
                    if part.strip() and part.strip() != "0,0;1,0;1,1;0,1":
                        found.add(part.strip())
    return found


@pytest.mark.skipif(not (HOMEHUB_REPO / "scripts" / "deploy" / "config.wall.psd1").is_file() or not shutil.which("git"),
                    reason="sibling HomeHub checkout absent; set HOMEHUB_REPO")
def test_tc1567_no_private_zone_is_anywhere_in_this_public_repository():
    polygons = private_polygons()
    assert polygons, "the private configuration names no zone - the check would prove nothing"
    for polygon in polygons:
        for probe in {polygon, polygon.split(";")[0] + ";" + polygon.split(";")[1]}:
            hit = subprocess.run(["git", "-C", str(ROOT), "grep", "-l", "-F", "-e", probe, "--", "."],
                                 stdout=subprocess.PIPE, universal_newlines=True, timeout=60)
            assert hit.stdout.strip() == "", "a private zone is in the public tree (file names only): " + hit.stdout


# ── review round 1 (DeepSeek) regressions ────────────────────────────────────


def test_tc1550_a_bad_other_knob_reaches_the_run_and_its_alert_not_a_silent_skip(tmp_path):
    env = write_env(tmp_path, CAMERA_DOOR_MOTION_DECODE="vaap", CAMERA_MOTION_BUDGET_PCT="25%")
    assert cm.main(["--env-file", env, "condition", "--camera", "door"]) == 0
    with pytest.raises(cm.Invalid, match="MOTION_DECODE"):
        cm.prepare(env, "door")


def test_tc1543_a_missing_engine_is_a_refusal_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "ENGINE_FILE", str(tmp_path / "gone.py"))
    with pytest.raises(cm.Invalid, match="cannot be loaded"):
        cm.prepare(write_env(tmp_path), "door")


@needs_cv
def test_tc1548_a_failed_decode_fails_the_replay_and_a_non_integer_rate_is_refused(tmp_path):
    seg_dir = tmp_path / "segs" / "2026-10-26"
    seg_dir.mkdir(parents=True)
    (seg_dir / "door-20261026T195000Z.ts").write_bytes(b"")
    env = write_env(tmp_path, CAMERA_DOOR_SEGMENT_DIR=str(tmp_path / "segs").replace("\\", "/"))

    class Proc:
        def __init__(self, argv, stdout=None, stderr=None):
            self.stdout = __import__("io").BytesIO(b"\x00" * (256 * 256 * 3))
            stderr.write(b"/seg.ts: Invalid data found when processing input")
            self.returncode = None

        def kill(self):
            pass

        def wait(self):
            self.returncode = 183
    lines = []
    rc = cm.replay(env, "door", 1793044200.0, 1793044800.0, None, False, runner=Proc, out=lines.append)
    summary = json.loads(lines[-1])
    assert rc == 1 and summary["frames"] == 3 and "Invalid data" in summary["failed_segments"][0]
    with pytest.raises(cm.Invalid, match="whole multiple"):
        cm.replay(write_env(tmp_path, CAMERA_DOOR_MOTION_SAMPLE_FPS="4"), "door", 0, 1, None, False, runner=Proc,
                  out=lines.append)


def test_tc1548_replay_warms_up_on_earlier_segments_but_reports_only_its_window(tmp_path, monkeypatch):
    since = 1793044200.0                              # 19:50:00Z
    seg_dir = tmp_path / "segs" / "2026-10-26"
    seg_dir.mkdir(parents=True)
    for name in ("door-20261026T194000Z.ts", "door-20261026T195000Z.ts"):
        (seg_dir / name).write_bytes(b"")
    env = write_env(tmp_path, CAMERA_DOOR_SEGMENT_DIR=str(tmp_path / "segs").replace("\\", "/"))

    class Config:
        eligible, sample_fps = True, 5.0

    class Engine:
        def __init__(self, config):
            self.cause = "approach"

        def process(self, frame, now):
            return True, None

    class Module:
        motion_config = staticmethod(lambda values: Config())
        MotionEngine = Engine
    monkeypatch.setattr(cm, "load_engine", lambda *a: Module)

    class Proc:
        def __init__(self, argv, stdout=None, stderr=None):
            self.stdout = __import__("io").BytesIO(b"\x00" * (256 * 256 * 10))
            self.returncode = 0

        def kill(self):
            pass

        def wait(self):
            pass
    lines = []
    assert cm.replay(env, "door", since, since + 1.0, None, True, runner=Proc, out=lines.append) == 0
    summary = json.loads(lines[-1])
    assert summary["signals"] == {"approach": 5} and len(lines) == 6   # 5 in-window lines + the summary


def test_tc1543_the_code_comparison_sets_docstrings_and_comments_aside_and_nothing_else():
    doc = chr(34) * 3
    assert code_only(doc + "doc" + doc + "\nx = 1  # c\n") == code_only("x = 1\n")
    assert code_only("def f():\n    " + doc + "d" + doc + "\n    return 1\n") == code_only("def f():\n    return 1\n")
    assert code_only("x = 1\n") != code_only("x = 2\n")
    assert code_only("y = a[0:16]\n") != code_only("y = a[0:15]\n")
