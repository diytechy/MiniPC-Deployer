"""SR-110/SR-111 (OfficeWallNaglight WSN-097): finished meeting recordings
leave the panel only by the hub pulling them over ssh, are published on the
hub by rename after a hash check, are deleted on the panel only after the hub
confirms that hash, and age out of the hub's 2 GiB quota.

Layers, each against the real artefact:

* TC-1409 -- the panel's forced command (wall-meeting-spool.py): its four
  verbs, and everything else refused.
* TC-1410 -- a pull END TO END through the real Transport class, with a
  stand-in for ssh that does what sshd does with a forced command (it runs the
  real spool script with SSH_ORIGINAL_COMMAND set): collected, published by
  rename under its date, deleted on the panel only after the ack, and a
  second run changes nothing.
* TC-1411 -- an interrupted transfer retried, a hash mismatch (the panel keeps
  its copy), a lost ack repaired without a second download.
* TC-1412 -- the hub unreachable, the scratch guard refusing (before the run
  and before a later file), the quota full (and EDQUOT while writing): the
  panel keeps its copy and is told the hub is full.
* TC-1413 -- retention: the period, the high-water rule, a dry run that
  deletes nothing, stale partial transfers, and the guard.
* TC-1414 -- the hub's units, timers, knobs and firstboot block (executed).
* TC-1415 -- both budgets full: the hub's `report full` reaches the recorder's
  document while the panel spool is full, so the kiosk can say both.

WHAT THIS DOES NOT PROVE. No real sshd, ext4 project quota or scratch mount is
involved: the forced command's `restrict` line is sshd's to enforce, the quota
is W4's (its root suite proves EDQUOT at the wall), and the mount guard is
W4's homehub_scratch.py, stubbed here by its exit code. The live pull from the
real panel onto the real volume is the coordinator's step once W4a exists.

Implements tests: TC-1409, TC-1410, TC-1411, TC-1412, TC-1413, TC-1414, TC-1415.
"""
from __future__ import annotations

import errno
import hashlib
import importlib.util
import io
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
WALL = ROOT / "stack" / "autoinstall" / "wall"
MEETINGS = ROOT / "stack" / "meetings"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


spool = _load("wall_meeting_spool", WALL / "wall-meeting-spool.py")
hm = _load("homehub_meetings", MEETINGS / "homehub_meetings.py")
mr = _load("wall_meeting_recorder_t", WALL / "wall-meeting-recorder.py")

NAME_A = "meeting-20261022T160752Z.opus"
NAME_B = "meeting-20261022T171500Z.opus"


def make_ready(root: Path, name: str, payload: bytes) -> str:
    ready = root / "ready"
    ready.mkdir(parents=True, exist_ok=True)
    (ready / name).write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (ready / (name + ".sha256")).write_text("%s  %s\n" % (digest, name))
    return digest


def run_spool(root: Path, command: str):
    out = io.BytesIO()
    rc = spool.main(environ={"SSH_ORIGINAL_COMMAND": command}, out=out, root=str(root))
    return rc, out.getvalue()


# ── TC-1409: the forced command ──────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "", "ls", "list all", "get", "get ../etc/shadow", "get /var/spool/wall-meeting/ready/" + NAME_A,
    "get " + NAME_A + " extra", "get meeting-x.opus", "ack " + NAME_A, "ack " + NAME_A + " notahash",
    "ack " + NAME_A + " " + "A" * 64, "report", "report maybe", "rm -rf /", "list; id", "x" * 300,
])
def test_tc1409_everything_but_the_four_verbs_is_refused(tmp_path, command, capsys):
    make_ready(tmp_path, NAME_A, b"opus")
    rc, out = run_spool(tmp_path, command)
    assert rc == spool.EXIT_USAGE and out == b""
    assert (tmp_path / "ready" / NAME_A).exists()


def test_tc1409_list_offers_only_finished_files_oldest_first(tmp_path):
    digest_b = make_ready(tmp_path, NAME_B, b"bbbb")
    digest_a = make_ready(tmp_path, NAME_A, b"aa")
    (tmp_path / "ready" / "meeting-20261022T180000Z.opus").write_bytes(b"no sidecar yet")
    (tmp_path / "ready" / "notes.txt").write_bytes(b"x")
    rc, out = run_spool(tmp_path, "list")
    rows = [json.loads(line) for line in out.decode().splitlines()]
    assert rc == 0 and rows == [{"name": NAME_A, "size": 2, "sha256": digest_a},
                                {"name": NAME_B, "size": 4, "sha256": digest_b}]


def test_tc1409_get_streams_the_bytes(tmp_path):
    payload = os.urandom(200_000)
    make_ready(tmp_path, NAME_A, payload)
    rc, out = run_spool(tmp_path, "get " + NAME_A)
    assert rc == 0 and out == payload


def test_tc1409_get_refuses_an_unfinished_file(tmp_path):
    (tmp_path / "ready").mkdir()
    (tmp_path / "ready" / NAME_A).write_bytes(b"x")
    assert run_spool(tmp_path, "get " + NAME_A)[0] == spool.EXIT_USAGE


def test_tc1409_ack_deletes_only_on_the_matching_hash(tmp_path):
    digest = make_ready(tmp_path, NAME_A, b"payload")
    rc, out = run_spool(tmp_path, "ack %s %s" % (NAME_A, "0" * 64))
    assert rc == spool.EXIT_MISMATCH and out == ("mismatch %s\n" % NAME_A).encode()
    assert (tmp_path / "ready" / NAME_A).exists()
    rc, out = run_spool(tmp_path, "ack %s %s" % (NAME_A, digest))
    assert rc == 0 and out == ("deleted %s\n" % NAME_A).encode()
    assert os.listdir(tmp_path / "ready") == []
    rc, out = run_spool(tmp_path, "ack %s %s" % (NAME_A, digest))
    assert rc == 0 and out == ("absent %s\n" % NAME_A).encode(), "a repeated ack is already done"


def test_tc1409_report_records_the_hubs_quota_state(tmp_path):
    assert run_spool(tmp_path, "report full")[0] == 0
    document = json.loads((tmp_path / "hub-state.json").read_text())
    assert document["quota"] == "full" and document["schemaVersion"] == 1
    run_spool(tmp_path, "report ok")
    assert json.loads((tmp_path / "hub-state.json").read_text())["quota"] == "ok"


# ── the rig for TC-1410..TC-1412 ─────────────────────────────────────────────

FAKE_SSH = '''
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("s", {spool!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
# What sshd does with a forced command: the client's words arrive as
# SSH_ORIGINAL_COMMAND and the forced command runs instead.
os.environ["SSH_ORIGINAL_COMMAND"] = " ".join(sys.argv[1:])
mode = os.environ.get("FAKE_SSH_MODE", "")
if mode == "unreachable":
    sys.stderr.write("ssh: connect to host panel port 22: No route to host\\n")
    sys.exit(255)
if mode == "truncate" and sys.argv[1:2] == ["get"]:
    out = __import__("io").BytesIO()
    module.main(out=out, root={root!r})
    data = out.getvalue()
    sys.stdout.buffer.write(data[: len(data) // 2])
    sys.exit(255)
if mode == "short-ok" and sys.argv[1:2] == ["get"]:
    out = __import__("io").BytesIO()
    module.main(out=out, root={root!r})
    data = out.getvalue()
    sys.stdout.buffer.write(data[: len(data) - 100])
    sys.exit(0)
if mode == "corrupt" and sys.argv[1:2] == ["get"]:
    out = __import__("io").BytesIO()
    module.main(out=out, root={root!r})
    data = bytearray(out.getvalue())
    data[len(data) // 2] ^= 0xFF
    sys.stdout.buffer.write(bytes(data))
    sys.exit(0)
rc = module.main(root={root!r})
if mode == "lose-ack" and sys.argv[1:2] == ["ack"]:
    sys.exit(255)
sys.exit(rc)
'''


class PullRig:
    def __init__(self, tmp_path, monkeypatch):
        self.panel = tmp_path / "panel-spool"
        self.hub = tmp_path / "scratch" / "meetings"
        self.hub.mkdir(parents=True)
        fake = tmp_path / "fake_ssh.py"
        fake.write_text(FAKE_SSH.format(spool=str(WALL / "wall-meeting-spool.py"), root=str(self.panel)))
        self.transport = hm.Transport([sys.executable, str(fake)])
        self.monkeypatch = monkeypatch
        self.guard_answers = []
        self.room_bytes = 10 ** 12
        self.alerts = []

    def mode(self, value):
        self.monkeypatch.setenv("FAKE_SSH_MODE", value)

    def guard(self):
        return self.guard_answers.pop(0) if self.guard_answers else (True, "ok")

    def puller(self):
        return hm.Puller(self.transport, root=str(self.hub), guard=self.guard,
                         room=lambda path: self.room_bytes,
                         alert=lambda key, title, body: self.alerts.append(key))

    def final(self, name):
        return Path(hm.final_path(str(self.hub), name))

    def panel_names(self):
        ready = self.panel / "ready"
        return sorted(os.listdir(ready)) if ready.exists() else []

    def hub_state(self):
        path = self.panel / "hub-state.json"
        return json.loads(path.read_text())["quota"] if path.exists() else None


@pytest.fixture
def pull(tmp_path, monkeypatch):
    monkeypatch.delenv("FAKE_SSH_MODE", raising=False)
    return PullRig(tmp_path, monkeypatch)


# ── TC-1410: end to end ──────────────────────────────────────────────────────

def test_tc1410_a_recording_is_collected_published_by_rename_and_deleted_on_the_panel(pull):
    payload = os.urandom(300_000)
    digest = make_ready(pull.panel, NAME_A, payload)
    assert pull.puller().run() == hm.EXIT_OK
    final = pull.final(NAME_A)
    assert final == pull.hub / "2026-10-22" / NAME_A
    assert final.read_bytes() == payload and hashlib.sha256(final.read_bytes()).hexdigest() == digest
    assert os.listdir(pull.hub / ".incoming") == [], "no partial left behind"
    assert pull.panel_names() == [], "deleted on the panel after the ack"
    assert pull.hub_state() == "ok"


def test_tc1410_several_files_oldest_first_and_a_second_run_changes_nothing(pull):
    make_ready(pull.panel, NAME_B, b"b" * 5000)
    make_ready(pull.panel, NAME_A, b"a" * 5000)
    puller = pull.puller()
    assert puller.run() == hm.EXIT_OK and puller.collected == [NAME_A, NAME_B]
    again = pull.puller()
    assert again.run() == hm.EXIT_OK and again.collected == []


def test_tc1410_a_partial_is_never_visible_under_a_final_name(pull, monkeypatch):
    make_ready(pull.panel, NAME_A, b"x" * 100_000)
    seen = []
    real_replace = os.replace

    def watching_replace(src, dst):
        seen.append((os.path.basename(src), os.path.exists(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(hm.os, "replace", watching_replace)
    pull.puller().run()
    assert seen == [(NAME_A + ".part", False)]


# ── TC-1411: interrupted, corrupted, lost ack ────────────────────────────────

def test_tc1411_an_interrupted_transfer_is_discarded_and_retried(pull):
    payload = b"z" * 200_000
    make_ready(pull.panel, NAME_A, payload)
    pull.mode("truncate")
    assert pull.puller().run() == hm.EXIT_TRANSFER
    assert not pull.final(NAME_A).exists() and os.listdir(pull.hub / ".incoming") == []
    assert NAME_A in pull.panel_names(), "the panel keeps its copy"
    pull.mode("")
    assert pull.puller().run() == hm.EXIT_OK
    assert pull.final(NAME_A).read_bytes() == payload and pull.panel_names() == []


def test_tc1411_a_short_transfer_with_a_clean_exit_is_an_interruption_not_a_hash_failure(pull):
    make_ready(pull.panel, NAME_A, b"k" * 50_000)
    pull.mode("short-ok")
    assert pull.puller().run() == hm.EXIT_TRANSFER
    assert not pull.final(NAME_A).exists() and os.listdir(pull.hub / ".incoming") == []
    assert NAME_A in pull.panel_names()
    assert pull.alerts == [], "a short count is caught before the hash, and is retried quietly"


def test_tc1411_a_hash_mismatch_keeps_the_panels_copy_and_alerts(pull):
    make_ready(pull.panel, NAME_A, b"q" * 50_000)
    pull.mode("corrupt")
    assert pull.puller().run() == hm.EXIT_TRANSFER
    assert not pull.final(NAME_A).exists() and os.listdir(pull.hub / ".incoming") == []
    assert NAME_A in pull.panel_names()
    assert pull.alerts == ["meetings-hash"]


def test_tc1411_a_lost_ack_is_repaired_without_a_second_download(pull, monkeypatch):
    make_ready(pull.panel, NAME_A, b"r" * 50_000)
    pull.mode("lose-ack")
    first = pull.puller()
    assert first.run() == hm.EXIT_TRANSFER and first.collected == [NAME_A]
    # The ack WAS applied on the panel; only the reply was lost.
    assert pull.panel_names() == []
    make_ready(pull.panel, NAME_A, b"r" * 50_000)  # say it had not been: a retried ack must still work
    pull.mode("")
    second = pull.puller()
    streamed = []
    real_stream = pull.transport.stream
    monkeypatch.setattr(pull.transport, "stream", lambda *a, **k: streamed.append(a) or real_stream(*a, **k))
    assert second.run() == hm.EXIT_OK and second.collected == [] and streamed == []
    assert pull.panel_names() == []


def test_tc1411_a_file_already_here_with_a_different_hash_is_left_alone(pull):
    make_ready(pull.panel, NAME_A, b"new" * 1000)
    final = pull.final(NAME_A)
    final.parent.mkdir(parents=True)
    final.write_bytes(b"old")
    assert pull.puller().run() == hm.EXIT_TRANSFER
    assert final.read_bytes() == b"old" and NAME_A in pull.panel_names()


def test_tc1411_more_bytes_than_listed_is_discarded(pull, monkeypatch):
    make_ready(pull.panel, NAME_A, b"s" * 1000)
    original = hm.parse_listing
    monkeypatch.setattr(hm, "parse_listing", lambda text: [dict(r, size=r["size"] - 10) for r in original(text)])
    assert pull.puller().run() == hm.EXIT_TRANSFER
    assert not pull.final(NAME_A).exists() and NAME_A in pull.panel_names()


# ── TC-1412: unreachable, guard, quota ───────────────────────────────────────

def test_tc1412_an_unreachable_panel_changes_nothing(pull):
    make_ready(pull.panel, NAME_A, b"u" * 1000)
    pull.mode("unreachable")
    assert pull.puller().run() == hm.EXIT_TRANSFER
    assert list(pull.hub.iterdir()) == [] and NAME_A in pull.panel_names()


def test_tc1412_a_guard_refusal_stops_before_anything_is_written(pull):
    make_ready(pull.panel, NAME_A, b"g" * 1000)
    pull.guard_answers = [(False, "/srv/scratch is not a mount point")]
    assert pull.puller().run() == hm.EXIT_GUARD
    assert list(pull.hub.iterdir()) == [] and NAME_A in pull.panel_names()
    assert pull.alerts == ["meetings-guard"]


def test_tc1412_the_guard_runs_again_before_every_new_file(pull):
    make_ready(pull.panel, NAME_A, b"a" * 1000)
    make_ready(pull.panel, NAME_B, b"b" * 1000)
    pull.guard_answers = [(True, "ok"), (True, "ok"), (False, "wrong device")]
    assert pull.puller().run() == hm.EXIT_GUARD
    assert pull.final(NAME_A).exists() and not pull.final(NAME_B).exists()
    assert pull.panel_names() == [NAME_B, NAME_B + ".sha256"]


def test_tc1412_no_room_in_the_quota_tells_the_panel_and_keeps_its_copy(pull):
    make_ready(pull.panel, NAME_A, b"f" * 100_000)
    pull.room_bytes = 100_000
    assert pull.puller().run() == hm.EXIT_QUOTA
    assert not pull.final(NAME_A).exists() and NAME_A in pull.panel_names()
    assert pull.hub_state() == "full" and pull.alerts == ["meetings-full"]
    pull.room_bytes = 10 ** 12
    assert pull.puller().run() == hm.EXIT_OK and pull.hub_state() == "ok"


def test_tc1412_edquot_while_writing_is_quota_full(pull, monkeypatch):
    make_ready(pull.panel, NAME_A, b"e" * 100_000)
    real_open = open

    class FullFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()
            return False

        def write(self, chunk):
            raise OSError(errno.EDQUOT, os.strerror(errno.EDQUOT))

        def flush(self):
            pass

        def fileno(self):
            return self.handle.fileno()

    def opener(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        return FullFile(handle) if str(path).endswith(".part") and "w" in mode else handle

    monkeypatch.setattr(hm, "open", opener, raising=False)
    assert pull.puller().run() == hm.EXIT_QUOTA
    assert os.listdir(pull.hub / ".incoming") == [] and NAME_A in pull.panel_names()
    assert pull.hub_state() == "full"


def test_tc1412_the_real_guard_command_refuses_when_it_is_missing(tmp_path):
    ok, reason = hm.run_guard([sys.executable, str(tmp_path / "no-such-guard.py")])
    assert ok is False and reason


# ── TC-1413: retention ───────────────────────────────────────────────────────

DAY = 86400


def test_tc1413_retention_plan_period_then_high_water():
    now = 100 * DAY
    files = [("old", 10, now - 20 * DAY), ("mid", 40, now - 5 * DAY), ("new", 40, now - DAY),
             ("newest", 30, now - 10)]
    assert hm.retention_plan(files, now, 14, 0, 90) == ["old"]
    # 110 bytes kept against a 100-byte quota at 90 %: the oldest go until <= 90.
    assert hm.retention_plan(files, now, 14, 100, 90) == ["old", "mid"]
    # Exactly at the cutoff is kept; only older goes.
    assert hm.retention_plan(files, now, 1, 1000, 90) == ["old", "mid"]
    assert hm.retention_plan(files, now + 1, 1, 1000, 90) == ["old", "mid", "new"]


def _tree(root: Path, now: float):
    made = {}
    for name, age_days, size in ((NAME_A.replace("20261022", "20261001"), 21, 1000),
                                 (NAME_A, 0.5, 1000)):
        at = hm.recorded_at(name)
        path = Path(hm.final_path(str(root), name))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        made[name] = path
    incoming = root / ".incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    stale = incoming / (NAME_B + ".part")
    stale.write_bytes(b"p")
    os.utime(stale, (now - 2 * 3600, now - 2 * 3600))
    fresh = incoming / ("meeting-20261022T180000Z.opus.part")
    fresh.write_bytes(b"p")
    os.utime(fresh, (now, now))
    return made, stale, fresh


def test_tc1413_retention_deletes_what_is_past_the_period(tmp_path):
    now = hm.recorded_at(NAME_A) + 12 * 3600
    made, stale, fresh = _tree(tmp_path, now)
    old, new = sorted(made.values())
    assert hm.retention(str(tmp_path), 14, 90, dry_run=False, now=now, limit=0,
                        guard=lambda: (True, "ok")) == hm.EXIT_OK
    assert not old.exists() and new.exists()
    assert not old.parent.exists(), "an emptied day directory goes"
    assert not stale.exists() and fresh.exists()


def test_tc1413_a_short_retention_dry_run_deletes_nothing(tmp_path, capsys):
    now = hm.recorded_at(NAME_A) + 12 * 3600
    made, stale, fresh = _tree(tmp_path, now)
    assert hm.retention(str(tmp_path), 1, 90, dry_run=True, now=now, limit=0,
                        guard=lambda: (True, "ok")) == hm.EXIT_OK
    assert all(p.exists() for p in made.values()) and stale.exists()
    printed = capsys.readouterr()
    assert "would delete" in printed.out + printed.err


def test_tc1413_the_high_water_rule_deletes_the_oldest(tmp_path):
    now = hm.recorded_at(NAME_A) + 60
    made, _stale, _fresh = _tree(tmp_path, now)
    hm.retention(str(tmp_path), 30, 90, dry_run=False, now=now, limit=1500, guard=lambda: (True, "ok"))
    old, new = sorted(made.values())
    assert not old.exists() and new.exists()


def test_tc1413_a_guard_refusal_deletes_nothing(tmp_path):
    now = hm.recorded_at(NAME_A) + 30 * DAY
    made, _stale, _fresh = _tree(tmp_path, now)
    assert hm.retention(str(tmp_path), 1, 90, dry_run=False, now=now, limit=0,
                        guard=lambda: (False, "not mounted")) == hm.EXIT_GUARD
    assert all(p.exists() for p in made.values())


def test_tc1413_the_proposed_period_fits_the_quota():
    # 48 kbit/s stereo is 21.6 MB an hour; 14 days at 6 hours a day stays under
    # the 90 % high-water of 2 GiB (and 7 would not).
    per_hour = 48_000 / 8 * 3600
    assert per_hour * 6 * 14 < 2 * 1024 ** 3 * 0.9 < per_hour * 7 * 14


# ── TC-1414: the hub's units, timers, knobs and firstboot ────────────────────

def _directives(path):
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


@pytest.mark.parametrize("unit", ["homehub-meeting-pull.service", "homehub-meeting-retention.service"])
def test_tc1414_both_services_carry_the_scratch_writer_pattern(unit):
    lines = _directives(MEETINGS / unit)
    for directive in ("RequiresMountsFor=/srv/scratch", "BindsTo=srv-scratch.mount",
                      "CapabilityBoundingSet=~CAP_SYS_RESOURCE", "Type=oneshot",
                      "ExecCondition=/usr/bin/python3 /opt/homehub/stack/meetings/homehub_meetings.py condition",
                      "ExecStartPre=/usr/bin/python3 /opt/homehub/stack/scratch/homehub_scratch.py guard --writer meetings"):
        assert directive in lines, (unit, directive)
    assert any(line.startswith("After=") and "srv-scratch.mount" in line for line in lines)


def test_tc1414_the_timers():
    pull = _directives(MEETINGS / "homehub-meeting-pull.timer")
    keep = _directives(MEETINGS / "homehub-meeting-retention.timer")
    assert "OnUnitActiveSec=2min" in pull and "WantedBy=timers.target" in pull
    assert "OnUnitActiveSec=1h" in keep and "WantedBy=timers.target" in keep


def test_tc1414_the_knobs_are_declared_and_default_off():
    env = (ROOT / "stack" / ".env.example").read_text(encoding="utf-8")
    for line in ("MEETING_PULL_ENABLED=false", "MEETING_PANEL_HOST=", "MEETING_PANEL_USER=wall-meeting",
                 "MEETING_RETENTION_DAYS=14", "MEETING_QUOTA_HIGH_PCT=90"):
        assert re.search("^%s$" % re.escape(line), env, re.M), line
    settings = hm.settings_from({"PANEL_IP": "192.0.2.17"})
    assert settings == {"enabled": False, "host": "192.0.2.17", "user": "wall-meeting", "days": 14,
                        "high_pct": 90}


def test_tc1414_condition_follows_the_knob(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MEETING_PULL_ENABLED=true\n")
    assert hm.main(["--env-file", str(env), "condition"]) == 0
    env.write_text("MEETING_PULL_ENABLED=false\n")
    assert hm.main(["--env-file", str(env), "condition"]) == 1


def test_tc1414_pull_without_a_panel_address_is_a_usage_error(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MEETING_PULL_ENABLED=true\n")
    assert hm.main(["--env-file", str(env), "pull"]) == hm.EXIT_USAGE


def test_tc1414_the_ssh_command_is_batch_pinned_and_key_only():
    argv = hm.ssh_argv({"host": "192.0.2.17", "user": "wall-meeting"}, state_dir="/var/lib/homehub-meetings")
    joined = " ".join(argv)
    for option in ("BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=accept-new",
                   "UserKnownHostsFile=/var/lib/homehub-meetings/known_hosts", "ConnectTimeout=10",
                   "ServerAliveInterval=10"):
        assert option in joined, option
    assert argv[:3] == ["ssh", "-i", "/var/lib/homehub-meetings/id_ed25519"]
    assert argv[-1] == "wall-meeting@192.0.2.17"


def test_tc1414_new_files_are_lf_and_the_script_is_executable_in_git():
    for path in MEETINGS.iterdir():
        if path.is_file():
            assert b"\r" not in path.read_bytes(), path.name
    out = subprocess.run(["git", "-C", str(MEETINGS), "ls-files", "-s", "homehub_meetings.py"],
                         capture_output=True, text=True, check=True).stdout
    assert out.startswith("100755"), out


def hub_block():
    text = (ROOT / "stack" / "autoinstall" / "firstboot.sh").read_text(encoding="utf-8")
    found = re.search(r"# BEGIN meeting-pull\n(.*?)# END meeting-pull\n", text, re.S)
    assert found, "firstboot.sh lost its meeting-pull block"
    return found.group(1)


def run_hub_block(tmp_path, knob):
    calls = tmp_path / "calls"
    stack = tmp_path / "stack"
    (stack / "meetings").mkdir(parents=True, exist_ok=True)
    rec = 'printf "%s\\n" "{kind} $*" >> "' + calls.as_posix() + '"'
    script = "\n".join([
        "set -uo pipefail",
        'STACK_DIR="{}"'.format(stack.as_posix()),
        'log() { echo "[firstboot] $*"; }',
        '_env_val() { [ "$1" = MEETING_PULL_ENABLED ] && printf "%%s\\n" "%s"; }' % (knob or ""),
        'install() { %s; return 0; }' % rec.format(kind="install"),
        'systemctl() { %s; return 0; }' % rec.format(kind="systemctl"),
        'python3() { %s; echo "ssh-ed25519 AAAATEST homehub-meeting-pull"; }' % rec.format(kind="python3"),
        hub_block(),
        "echo BLOCK-DONE",
    ]) + "\n"
    done = subprocess.run([BASH, "-s"], input=script, capture_output=True, timeout=60,
                          text=True, encoding="utf-8", errors="replace")
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return done, lines


@needs_bash
def test_tc1414_firstboot_on_installs_makes_the_key_and_enables_both_timers(tmp_path):
    done, calls = run_hub_block(tmp_path, "true")
    assert "BLOCK-DONE" in done.stdout, done.stderr
    installs = [c for c in calls if c.startswith("install")]
    assert len(installs) == 4 and all("/etc/systemd/system/homehub-meeting-" in c for c in installs)
    assert any(c.startswith("python3 ") and c.endswith(" init-key") for c in calls)
    assert "systemctl enable --now homehub-meeting-pull.timer homehub-meeting-retention.timer" in calls
    assert "meeting pull key (PUBLIC" in done.stdout


@needs_bash
@pytest.mark.parametrize("knob", ["false", ""])
def test_tc1414_firstboot_off_or_unset_disables(tmp_path, knob):
    done, calls = run_hub_block(tmp_path, knob)
    assert "BLOCK-DONE" in done.stdout
    assert "systemctl disable --now homehub-meeting-pull.timer homehub-meeting-retention.timer" in calls
    assert not any(c.startswith("python3") for c in calls)


@needs_bash
def test_tc1414_firstboot_invalid_knob_changes_nothing(tmp_path):
    done, calls = run_hub_block(tmp_path, "yes")
    assert "BLOCK-DONE" in done.stdout and "is not true or false" in done.stdout
    assert not any(c.startswith(("systemctl enable", "systemctl disable")) for c in calls)


# ── TC-1415: both budgets full ───────────────────────────────────────────────

def test_tc1415_a_full_hub_and_a_full_spool_reach_the_kiosk_document(tmp_path, monkeypatch):
    monkeypatch.delenv("FAKE_SSH_MODE", raising=False)
    rig = PullRig(tmp_path, monkeypatch)
    make_ready(rig.panel, NAME_A, b"f" * (13 * 1024 * 1024))
    rig.room_bytes = 0
    assert rig.puller().run() == hm.EXIT_QUOTA
    spool_obj = mr.Spool(rig.panel)
    renderer = tmp_path / "renderer"
    renderer.mkdir()
    (tmp_path / "mode").write_text("bus")
    now = time.time()
    recorder = mr.Recorder(mr.settings_from({"WALL_MEETING_SPOOL_MAX_MB": "16"}), spool=spool_obj,
                           state_file=tmp_path / "state.json", request_file=renderer / "req.json",
                           ack_file=renderer / "ack.json", call_file=tmp_path / "none.json",
                           mode_file=tmp_path / "mode", mic_env=tmp_path / "x", mute_env=tmp_path / "y",
                           clock=lambda: 10.0, wall=lambda: now)
    recorder.prime()
    (renderer / "req.json").write_text(json.dumps({"schemaVersion": 1, "id": 1, "action": "start",
                                                   "requestedAt": int(now * 1000)}))
    doc = recorder.tick()
    assert doc["refusal"]["reason"] == "spool-full" and doc["hub"] == "full" and doc["intent"] == "idle"
