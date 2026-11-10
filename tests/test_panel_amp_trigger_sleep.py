"""SR-140: the speakers sleep with the screen (panel-amp-trigger.py).

Every test drives the real state machine and the real FadeStage with FAKES THAT
HAVE STATE: a fake clock, a fake sysfs tree, a fake mixer that records the exact
amixer argv it was handed, and a fake relay that records the commands it was
sent -- all appending to ONE event log, so the ORDER between the two (the fade
completes before the relay opens; the relay closes before the fade returns) is
asserted against what was actually sent, not against the machine's own phase
names. Nothing here claims anything about audibility on the panel: that is the
on-glass acceptance in HomeHub docs/SPEAKER_SLEEPS_WITH_SCREEN_PLAN_2026-11-01.md.
"""

import importlib.util
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "stack" / "autoinstall" / "wall" / "panel-amp-trigger.py"
FADE_ARGV_HEAD = ["/usr/bin/amixer", "-c", "Loopback", "-q", "cset",
                  "name=Speaker Fade Playback Volume"]
STEP = 0.05


def load_module():
    spec = importlib.util.spec_from_file_location("panel_amp_trigger_sleep", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── fakes with state ─────────────────────────────────────────────────────────

class World:
    """The clock and the one event log both fakes append to."""

    def __init__(self):
        self.now = 100.0
        self.events = []          # (time, kind, detail)

    def log(self, kind, detail=None):
        self.events.append((self.now, kind, detail))

    def of(self, kind):
        return [(t, d) for t, k, d in self.events if k == kind]

    def index_of(self, kind, detail=None, last=False):
        hits = [i for i, (_, k, d) in enumerate(self.events)
                if k == kind and (detail is None or d == detail)]
        return (hits[-1] if last else hits[0]) if hits else None


class FakeMixer:
    """Stands in for subprocess.run: records argv, can be told to fail."""

    def __init__(self, world, fail=False, value=60):
        self.world = world
        self.fail = fail
        self.value = value            # the control's real state
        self.fail_values = set()      # writes of these values fail
        self.argvs = []

    def __call__(self, argv, **kwargs):
        self.argvs.append(list(argv))
        if "cget" in argv:
            self.world.log("cget")
            if self.fail:
                return SimpleNamespace(returncode=1, stdout="", stderr="no control")
            return SimpleNamespace(
                returncode=0, stderr="",
                stdout="numid=1,iface=MIXER,name='x'\n  : values=%d,%d\n"
                       % (self.value, self.value))
        written = int(argv[-1])
        self.world.log("amixer", written)
        if self.fail or written in self.fail_values:
            return SimpleNamespace(returncode=1, stdout="", stderr="no such control")
        self.value = written
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class Rig:
    """A SpeakerSleep wired to a real FadeStage over the fake mixer and relay."""

    def __init__(self, fail_fade=False, relay_on_results=None):
        self.module = load_module()
        self.world = World()
        self.mixer = FakeMixer(self.world, fail=fail_fade)
        self.relay_on_results = list(relay_on_results or [])
        # The loop's own record of the relay, as main() keeps it.
        self.relay_is_on = True
        self.relay_changed_at = -1e9
        self.messages = []
        fade = self.module.FadeStage(run=self.mixer, card_reader=lambda: "Loopback")
        self.sleeper = self.module.SpeakerSleep(
            fade, self._on, self._off, emit=self.messages.append)

    def _on(self, now):
        self.world.log("relay", "ON")
        ok = self.relay_on_results.pop(0) if self.relay_on_results else True
        if ok:
            self.relay_is_on, self.relay_changed_at = True, now
        return ok

    def _off(self, now):
        self.world.log("relay", "OFF")
        self.relay_is_on, self.relay_changed_at = False, now
        return True

    def run(self, seconds, enabled=True, lit=True, call=False, audio=True, beat=5.0):
        """Step the machine every 50 ms for `seconds` of fake time."""
        for _ in range(int(round(seconds / STEP))):
            self.sleeper.step(self.world.now, enabled, lit, call, audio, beat,
                              relay_is_on=self.relay_is_on,
                              relay_changed_at=self.relay_changed_at)
            self.world.now += STEP

    def fade_values(self):
        return [d for _, d in self.world.of("amixer")]


def settled_lit(rig, audio=True):
    """A rig in the steady lit state with the stage confirmed at unity."""
    rig.sleeper.reset_unity(rig.world.now)
    rig.world.events.clear()
    rig.mixer.argvs.clear()
    rig.run(1.0, lit=True, audio=audio)
    assert rig.sleeper.phase == rig.module.SpeakerSleep.LIT
    assert rig.world.events == []


def go_dark(rig, audio=True, seconds=3.0, dwell=True):
    """Settled lit, then dark; by default long enough that the minimum off-time
    has also passed, so a wake is not held back (the dwell has its own tests)."""
    settled_lit(rig, audio)
    rig.run(seconds, lit=False, audio=audio)
    assert rig.sleeper.phase == rig.module.SpeakerSleep.DARK
    if dwell:
        rig.run(rig.module.MIN_OFF_SECONDS + 1.0, lit=False, audio=audio)


# ── lit -> dark ──────────────────────────────────────────────────────────────

def test_lit_to_dark_ramps_the_fade_dblinear_then_opens_the_relay_llr1436():
    rig = Rig()
    settled_lit(rig)
    started = rig.world.now
    rig.run(3.0, lit=False)

    values = rig.fade_values()
    assert values[-1] == 0, "the ramp ends at the floor"
    assert values == sorted(values, reverse=True), "monotonic: never back up"
    assert len(values) == len(set(values)), "a write only when the step changes"
    # dB-linear over ~2 s in 50 ms steps = 1.5 dB per step (60 dB / 40 steps):
    # every written step is one or two dB, never a jump.
    gaps = [a - b for a, b in zip([60] + values, values)]
    assert all(1 <= gap <= 2 for gap in gaps), gaps
    assert 30 <= len(values) <= 40
    # Exact argv: the fade control by name, on the Loopback card, nothing else.
    for argv in rig.mixer.argvs:
        assert argv[:-1] == FADE_ARGV_HEAD
        assert argv[-1].isdigit()
    # The midpoint is half way down in dB (linear), within a step.
    midpoint = next(t for t, d in rig.world.of("amixer") if d <= 30)
    assert abs((midpoint - started) - 1.0) <= 0.1


def test_the_fade_completes_before_the_relay_opens_and_only_then_llr1436():
    rig = Rig()
    settled_lit(rig)
    started = rig.world.now
    rig.run(3.0, lit=False)
    last_fade = rig.world.index_of("amixer", 0)
    relay_off = rig.world.index_of("relay", "OFF")
    assert last_fade is not None and relay_off is not None
    assert last_fade < relay_off, "silence first, then the relay"
    assert rig.world.of("relay") == [(rig.world.events[relay_off][0], "OFF")], \
        "exactly one relay command"
    # Not early: the relay waits out the ~2 s ramp.
    assert rig.world.events[relay_off][0] - started >= 1.9
    # Every fade write precedes it.
    assert all(i < relay_off for i, e in enumerate(rig.world.events) if e[1] == "amixer")


def test_while_dark_the_relay_stays_off_even_if_audio_plays_llr1436():
    rig = Rig()
    go_dark(rig, audio=True)
    before = list(rig.world.events)
    rig.run(600.0, lit=False, audio=True)          # ten minutes of music
    assert rig.world.events == before, "no relay command, no fade write, while dark"
    assert rig.sleeper.governing


def test_audio_attack_cannot_close_the_relay_while_the_machine_governs_llr1436():
    rig = Rig()
    go_dark(rig)
    assert rig.sleeper.governing is True    # the main loop skips the attack rule


# ── gating: setting off, other positions, calls ──────────────────────────────

def test_with_the_setting_off_nothing_fades_and_nothing_is_commanded_llr1436():
    rig = Rig()
    settled_lit(rig)
    rig.run(30.0, enabled=False, lit=False, audio=True)
    assert rig.world.events == []
    assert rig.sleeper.phase == rig.module.SpeakerSleep.LIT
    assert rig.sleeper.governing is False


@pytest.mark.parametrize("output", ["headset", "builtin", "mute"])
@pytest.mark.parametrize("sleeps", [True, False])
def test_other_positions_behave_exactly_as_today_llr1434(tmp_path, output, sleeps):
    rig = Rig()
    state = tmp_path / "audio-state.json"
    state.write_text(json.dumps({"output": output, "speaker_sleeps_with_screen": sleeps}),
                     encoding="utf-8")
    position, preference = rig.module.read_sleep_policy(str(state))
    assert position is False
    settled_lit(rig)
    rig.run(30.0, enabled=position and preference, lit=False, audio=True)
    assert rig.world.events == []


def test_the_speaker_position_with_the_preference_on_enables_it_llr1434(tmp_path):
    module = load_module()
    state = tmp_path / "audio-state.json"
    state.write_text(json.dumps({"output": "speaker", "speaker_sleeps_with_screen": True}),
                     encoding="utf-8")
    assert module.read_sleep_policy(str(state)) == (True, True)
    state.write_text(json.dumps({"output": "speaker", "speaker_sleeps_with_screen": False}),
                     encoding="utf-8")
    assert module.read_sleep_policy(str(state)) == (True, False)


@pytest.mark.parametrize("text", [None, "", "not json", "[]", "7",
                                  '{"output": "banana"}', '{"output": 7}'])
def test_an_unreadable_state_means_do_nothing_llr1434(tmp_path, text):
    module = load_module()
    path = tmp_path / "audio-state.json"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    assert module.read_sleep_policy(str(path))[0] is False


@pytest.mark.parametrize("value,expected", [(None, True), ("off", True), (0, True),
                                            (False, False), (True, True)])
def test_a_missing_or_malformed_preference_reads_true_llr1434(tmp_path, value, expected):
    module = load_module()
    document = {"output": "speaker"}
    if value is not None:
        document["speaker_sleeps_with_screen"] = value
    path = tmp_path / "audio-state.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert module.read_sleep_policy(str(path)) == (True, expected)


def test_a_call_active_when_the_screen_goes_dark_keeps_the_speakers_on_llr1436():
    rig = Rig()
    settled_lit(rig)
    rig.run(60.0, lit=False, call=True, audio=True)
    assert rig.world.events == []
    assert rig.sleeper.phase == rig.module.SpeakerSleep.LIT


def test_a_call_becoming_active_while_dark_turns_the_relay_on_at_unity_llr1436():
    rig = Rig()
    go_dark(rig, audio=False)
    rig.world.events.clear()
    # No audio, screen still dark: the CALL alone wakes the room.
    rig.run(10.5, lit=False, call=True, audio=False)
    assert rig.world.index_of("relay", "ON") is not None
    assert rig.fade_values()[-1] == 60, "the stage is back at unity for the call"
    assert rig.sleeper.phase == rig.module.SpeakerSleep.LIT
    # ...and it stays on, undisturbed, for the length of the call.
    rig.world.events.clear()
    rig.run(120.0, lit=False, call=True, audio=False)
    assert rig.world.events == []
    # The call ends while the screen is still dark: the room sleeps again.
    rig.run(4.0, lit=False, call=False, audio=True)
    assert rig.world.index_of("relay", "OFF") is not None


def test_a_call_arriving_mid_fade_fades_back_in_without_touching_the_relay_llr1436():
    rig = Rig()
    settled_lit(rig)
    rig.run(1.0, lit=False)                         # half way down
    assert rig.sleeper.phase == rig.module.SpeakerSleep.FADING_OUT
    rig.run(3.0, lit=False, call=True)
    assert rig.world.of("relay") == []
    assert rig.fade_values()[-1] == 60


# ── dark -> lit ──────────────────────────────────────────────────────────────

def test_dark_to_lit_with_audio_closes_the_relay_waits_then_fades_in_llr1436():
    rig = Rig()
    go_dark(rig, audio=True)
    rig.world.events.clear()
    lit_at = rig.world.now
    rig.run(10.5, lit=True, audio=True)

    relay_on = rig.world.index_of("relay", "ON")
    first_write = rig.world.index_of("amixer")
    assert relay_on is not None and first_write is not None
    assert relay_on < first_write, "relay ON (verified) before any fade-in write"
    on_time = rig.world.events[relay_on][0]
    first_time = rig.world.events[first_write][0]
    assert on_time - lit_at < 0.2, "the relay closes at once"
    assert first_time - on_time >= 8.5, "then 8.5 seconds for the boards to start"
    values = rig.fade_values()
    assert values == sorted(values) and values[-1] == 60
    assert values[0] <= 4, "the ramp starts from the floor"
    ramp_seconds = rig.world.of("amixer")[-1][0] - first_time
    assert 0.85 <= ramp_seconds <= 1.15, "about one second of fade-in"
    assert rig.sleeper.phase == rig.module.SpeakerSleep.LIT
    assert rig.world.of("relay") == [(on_time, "ON")]


def test_dark_to_lit_with_no_audio_returns_to_unity_and_leaves_the_relay_alone_llr1436():
    rig = Rig()
    go_dark(rig, audio=False)
    rig.world.events.clear()
    rig.run(5.0, lit=True, audio=False)
    assert rig.fade_values() == [60], "one write: the stage to unity"
    assert rig.world.of("relay") == [], "the existing attack rule closes it later"
    assert rig.sleeper.phase == rig.module.SpeakerSleep.LIT
    assert rig.sleeper.governing is False


def test_relight_mid_fade_goes_back_up_without_a_relay_cycle_llr1436():
    rig = Rig()
    settled_lit(rig)
    rig.run(0.8, lit=False)
    low = rig.fade_values()[-1]
    rig.run(3.0, lit=True)
    assert rig.world.of("relay") == []
    assert rig.fade_values()[-1] == 60 and low < 60
    assert rig.sleeper.phase == rig.module.SpeakerSleep.LIT


def test_dark_again_while_settling_sleeps_again_llr1436():
    rig = Rig()
    go_dark(rig)
    rig.world.events.clear()
    rig.run(0.5, lit=True)                       # relay ON, still settling
    assert rig.sleeper.phase == rig.module.SpeakerSleep.SETTLING
    rig.run(rig.module.MIN_ON_SECONDS + 4.0, lit=False)
    kinds = [d for _, k, d in rig.world.events if k == "relay"]
    assert kinds == ["ON", "OFF"]
    assert rig.sleeper.phase == rig.module.SpeakerSleep.DARK


def test_a_relay_that_will_not_close_is_retried_on_the_beat_llr1436():
    rig = Rig(relay_on_results=[False, False, True])
    go_dark(rig)
    rig.world.events.clear()
    rig.run(20.0, lit=True, audio=True, beat=5.0)
    times = [t for t, _ in rig.world.of("relay")]
    assert len(times) == 3
    assert times[1] - times[0] == pytest.approx(5.0, abs=0.1)
    assert rig.sleeper.phase == rig.module.SpeakerSleep.LIT


def test_turning_the_setting_off_while_dark_snaps_to_unity_llr1436():
    rig = Rig()
    go_dark(rig)
    rig.world.events.clear()
    rig.run(1.0, enabled=False, lit=False, audio=True)
    assert rig.fade_values() == [60]
    assert rig.world.of("relay") == [], "the relay is the existing rule's again"
    assert rig.sleeper.governing is False


# ── failure modes ────────────────────────────────────────────────────────────

def test_a_fade_that_cannot_be_written_still_turns_the_relay_off_llr1436():
    rig = Rig(fail_fade=True)
    rig.sleeper.reset_unity(rig.world.now)          # fails, logged, not raised
    rig.world.events.clear()
    rig.mixer.argvs.clear()
    started = rig.world.now
    rig.run(3.0, lit=False)
    off = rig.world.of("relay")
    assert off and off[0][1] == "OFF"
    writes = [a for a in rig.mixer.argvs if "cset" in a]
    assert len(writes) <= rig.module.FADE_FAIL_LIMIT, "gave up, did not hammer"
    assert off[0][0] - started <= 2.1, "the relay was not held back by the failure"
    assert any("not writable" in m or "cutting" in m for m in rig.messages)


def test_a_fade_failure_halfway_cuts_the_ramp_short_but_never_the_relay_llr1436():
    rig = Rig()
    settled_lit(rig)
    rig.run(0.5, lit=False)
    rig.mixer.fail = True
    rig.run(3.0, lit=False)
    assert rig.world.index_of("relay", "OFF") is not None
    assert rig.sleeper.phase == rig.module.SpeakerSleep.DARK


def test_startup_and_shutdown_put_the_stage_at_unity_llr1436():
    rig = Rig()
    assert rig.sleeper.reset_unity(rig.world.now) is True
    assert rig.mixer.argvs == [FADE_ARGV_HEAD + ["60"]]
    assert rig.sleeper.unity_known is True
    # After a crash mid-fade, the next start restores it even from "faded".
    rig.sleeper.level = 7
    rig.sleeper.reset_unity(rig.world.now)
    assert rig.mixer.argvs[-1] == FADE_ARGV_HEAD + ["60"]
    assert rig.sleeper.level == 60


def test_an_unconfirmed_unity_is_retried_quietly_while_lit_llr1436():
    rig = Rig(fail_fade=True)
    rig.sleeper.reset_unity(rig.world.now)
    rig.mixer.argvs.clear()
    rig.run(1.0, lit=True)
    assert rig.mixer.argvs == [], "not hammered every tick"
    rig.mixer.fail = False
    rig.run(6.0, lit=True)               # one 5 s beat, not 30 s
    assert rig.mixer.argvs == [FADE_ARGV_HEAD + ["60"]]
    assert rig.sleeper.unity_known is True
    rig.mixer.argvs.clear()
    rig.run(60.0, lit=True)
    assert rig.mixer.argvs == [], "and then left alone"


def test_the_fade_stage_can_only_ever_name_its_own_control_llr1435():
    """The main volume is unreachable from the fade: whatever value, whatever card."""
    module = load_module()
    seen = []
    stage = module.FadeStage(run=lambda argv, **kw: (seen.append(argv),
                                                     SimpleNamespace(returncode=0, stdout="", stderr=""))[1],
                             card_reader=lambda: "Loopback")
    for value in (0, 17, 60, "60", 59.6):
        stage.write(value)
    for argv in seen:
        assert argv[:6] == FADE_ARGV_HEAD
        assert "Bus Playback Volume" not in " ".join(argv)
        assert "sset" not in argv and "Bus" not in argv
    assert [a[-1] for a in seen] == ["0", "17", "60", "60", "59"]


def test_a_timed_out_or_missing_amixer_is_a_false_not_a_crash_llr1435():
    module = load_module()

    def boom(argv, **kw):
        raise OSError("amixer: not found")

    stage = module.FadeStage(run=boom, card_reader=lambda: "Loopback")
    assert stage.write(60) is False

    import subprocess

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    assert module.FadeStage(run=slow, card_reader=lambda: "Loopback").write(0) is False


def test_the_card_id_is_read_from_the_cards_file_the_way_the_applier_does_llr1435(tmp_path):
    module = load_module()
    cards = tmp_path / "audio-cards.env"
    assert module.loopback_card(str(cards)) == "Loopback"
    cards.write_text("WALL_AUDIO_ADAPTER_CARD=X\nWALL_AUDIO_LOOPBACK_CARD=Loopback_1\n",
                     encoding="utf-8")
    assert module.loopback_card(str(cards)) == "Loopback_1"
    cards.write_text("WALL_AUDIO_LOOPBACK_CARD=a b; rm\n", encoding="utf-8")
    assert module.loopback_card(str(cards)) == "Loopback", "a hostile id is not used"


# ── the lit fact ─────────────────────────────────────────────────────────────

def backlight(root, name, brightness, maximum):
    device = root / name
    device.mkdir(parents=True)
    if brightness is not None:
        (device / "brightness").write_text(str(brightness) + "\n", encoding="ascii")
    if maximum is not None:
        (device / "max_brightness").write_text(str(maximum) + "\n", encoding="ascii")


def test_one_lit_backlight_is_lit_and_a_zero_one_is_dark_llr1433(tmp_path):
    module = load_module()
    backlight(tmp_path, "intel_backlight", 400, 937)
    assert module.screen_lit(str(tmp_path)) is True
    (tmp_path / "intel_backlight" / "brightness").write_text("0\n", encoding="ascii")
    assert module.screen_lit(str(tmp_path)) is False


def test_any_lit_device_among_several_is_lit_llr1433(tmp_path):
    module = load_module()
    backlight(tmp_path, "a", 0, 100)
    backlight(tmp_path, "b", 5, 100)
    assert module.screen_lit(str(tmp_path)) is True


def test_missing_backlight_data_reads_lit_never_dark_llr1433(tmp_path):
    """Never fade on missing data -- the deliberate difference from the kiosk."""
    module = load_module()
    assert module.screen_lit(str(tmp_path / "does-not-exist")) is True
    assert module.screen_lit(str(tmp_path)) is True            # empty class dir
    backlight(tmp_path, "nomax", 0, None)
    backlight(tmp_path, "nobrightness", None, 100)
    assert module.screen_lit(str(tmp_path)) is True


@pytest.mark.parametrize("brightness,maximum", [
    ("abc", "100"), ("-1", "100"), ("0", "0"), ("5", "0"), ("200", "100"),
    ("", "100"), ("1.5", "100"),
])
def test_an_invalid_device_is_not_counted_so_it_cannot_make_the_room_dark_llr1433(
        tmp_path, brightness, maximum):
    module = load_module()
    device = tmp_path / "bad"
    device.mkdir()
    (device / "brightness").write_text(brightness, encoding="ascii")
    (device / "max_brightness").write_text(maximum, encoding="ascii")
    assert module.screen_lit(str(tmp_path)) is True
    # ...and beside a VALID dark device, the invalid one does not light it.
    backlight(tmp_path, "good", 0, 100)
    assert module.screen_lit(str(tmp_path)) is False


def test_the_lit_rule_matches_the_kiosks_valid_device_filter_llr1433():
    """Both sides read the same predicate: unsigned ints, max>0, brightness<=max."""
    kiosk = Path("C:/Projects/OfficeWallNaglight/electron/display-lit.cjs")
    if not kiosk.exists():
        pytest.skip("the kiosk repo is not beside this one")
    text = kiosk.read_text(encoding="utf-8")
    assert "/^[0-9]+$/" in text and "maximum>0" in text and "brightness<=maximum" in text
    assert "/sys/class/backlight" in text
    assert load_module().BACKLIGHT_ROOT == "/sys/class/backlight"


# ── the call fact ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("document,expected", [
    ({"state": "call"}, True), ({"state": "bridge"}, True),
    ({"state": "idle"}, False), ({"state": "ringing"}, False),
    ({"state": None}, False), ([], False), ("call", False),
])
def test_only_a_call_or_a_bridge_counts_as_a_call_llr1434(tmp_path, document, expected):
    module = load_module()
    path = tmp_path / "call-state.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert module.read_call_active(str(path), wall_clock=time.time) is expected


def test_a_missing_garbled_or_stale_call_document_is_no_call_llr1434(tmp_path):
    module = load_module()
    path = tmp_path / "call-state.json"
    assert module.read_call_active(str(path)) is False
    path.write_text("{not json", encoding="utf-8")
    assert module.read_call_active(str(path)) is False
    path.write_text('{"state": "call"}', encoding="utf-8")
    assert module.read_call_active(str(path)) is True
    later = time.time() + module.CALL_MAX_AGE_SECONDS + 5
    assert module.read_call_active(str(path), wall_clock=lambda: later) is False


# ── the daemon loop, end to end ──────────────────────────────────────────────

class LoopActuator:
    """A relay with state: records every command in the shared world."""

    device = "/dev/wall-amp-relay"

    def __init__(self, world, stop_failures=0):
        self.world = world
        self.physical = False
        self.running_flag = False
        self.stop_failures = stop_failures

    @property
    def running(self):
        return self.running_flag

    def ensure_off(self):
        self.world.log("relay", "ensure_off")
        self.physical = False
        return True

    def start(self):
        self.world.log("relay", "ON")
        self.physical = True
        self.running_flag = True
        return True

    def maintain(self, now=None):
        if self.running_flag:
            self.world.log("heartbeat")
        return self.running_flag

    def stop(self):
        if self.stop_failures:
            self.stop_failures -= 1
            self.world.log("relay", "OFF-FAILED")
            self.running_flag = False
            return False
        self.world.log("relay", "OFF")
        self.physical = False
        self.running_flag = False
        return True


class LoopLevel:
    loud = True

    def __init__(self, pcm, offset):
        self.pcm = pcm

    def start(self):
        pass

    def stop(self):
        pass

    def above(self, db):
        return LoopLevel.loud

    def report(self):
        return self.pcm


def wait_until(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def daemon(monkeypatch):
    """The real main() loop with fake relay, level, mixer and facts, real time."""
    module = load_module()
    world = World()
    # Real time, compressed: every constant that paces the machine is shrunk.
    for name, value in (("FADE_OUT_SECONDS", 0.4), ("FADE_IN_SECONDS", 0.3),
                        ("WAKE_SETTLE_SECONDS", 0.3), ("SLEEP_SAMPLE_SECONDS", 0.02),
                        ("ATTACK_SECONDS", 0.0), ("MIN_OFF_SECONDS", 0.0),
                        ("MIN_ON_SECONDS", 0.0), ("ACTUATOR_FAST_RETRY_SECONDS", 0.05),
                        ("ACTUATOR_RETRY_SECONDS", 0.05), ("BLOCK_SECONDS", 0.02),
                        ("FADE_STEP_SECONDS", 0.02)):
        monkeypatch.setattr(module, name, value)

    real_log = world.log
    start = time.monotonic()

    def timed(kind, detail=None):
        world.now = time.monotonic() - start
        real_log(kind, detail)

    world.log = timed
    facts = {"output": "speaker", "sleeps": True, "lit": True, "call": False}
    actuator = LoopActuator(world)
    mixer = FakeMixer(world)
    handlers = []
    monkeypatch.setattr(module, "build_actuator", lambda *a, **k: actuator)
    monkeypatch.setattr(module, "required_tools", lambda method: ())
    monkeypatch.setattr(module, "Level", LoopLevel)
    monkeypatch.setattr(module, "current_mode", lambda: "bus")
    monkeypatch.setattr(module, "record_safe_state", lambda verified: None)
    monkeypatch.setattr(module, "publish_bus_telemetry", lambda *a, **k: None)
    monkeypatch.setattr(module, "read_sleep_policy",
                        lambda *a: (facts["output"] == "speaker", facts["sleeps"]))
    monkeypatch.setattr(module, "screen_lit", lambda *a: facts["lit"])
    monkeypatch.setattr(module, "read_call_active", lambda *a: facts["call"])
    real_stage = module.FadeStage
    monkeypatch.setattr(module, "FadeStage",
                        lambda: real_stage(run=mixer, card_reader=lambda: "Loopback"))
    monkeypatch.setattr(module.signal, "signal", lambda sig, handler: handlers.append(handler))
    monkeypatch.setenv("WALL_AMP_ENABLED", "true")
    LoopLevel.loud = True
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.setdefault("status", module.main()),
                              daemon=True)

    def stop():
        for handler in handlers[:1]:
            handler(15, None)
        thread.join(5.0)
        assert not thread.is_alive(), "the daemon did not stop"

    rig = SimpleNamespace(module=module, world=world, facts=facts, actuator=actuator,
                          mixer=mixer, thread=thread, stop=stop, outcome=outcome)
    yield rig
    if thread.is_alive():
        stop()


def test_the_whole_loop_sleeps_wakes_and_restores_unity_llr1436(daemon):
    rig = daemon
    module = rig.module
    rig.thread.start()

    # Lit, loud: the existing attack rule closes the relay.
    assert wait_until(lambda: rig.world.index_of("relay", "ON") is not None)
    startup = rig.world.events[0:3]
    assert ("relay", "ensure_off") == startup[0][1:], "safe OFF first"
    assert startup[1][1:] == ("amixer", 60), "then the stage to unity, at start-up"
    # That close takes the settle and the fade (SR-143) before the room is lit.
    assert wait_until(lambda: rig.mixer.value == 60)
    rig.world.events.append((0, "mark", "dark"))

    # Screen dark: fade down, THEN the relay opens; audio keeps playing.
    rig.facts["lit"] = False
    assert wait_until(lambda: rig.world.index_of("relay", "OFF") is not None)
    mark = rig.world.index_of("mark", "dark")
    off = rig.world.index_of("relay", "OFF")
    fades = [i for i, e in enumerate(rig.world.events)
             if e[1] == "amixer" and i > mark]
    assert fades and fades[-1] < off
    assert rig.world.events[fades[-1]][2] == 0
    # Dark with loud audio: the heartbeat and the attack rule do not undo it.
    quiet_from = len(rig.world.events)
    time.sleep(0.5)
    assert [e for e in rig.world.events[quiet_from:] if e[1] in ("relay", "heartbeat")] == []
    assert rig.actuator.physical is False

    # Screen lit again: relay ON before the fade-in, which ends at unity.
    rig.world.events.append((0, "mark", "lit"))
    rig.facts["lit"] = True
    def faded_back_up():
        lit_at = rig.world.index_of("mark", "lit")
        ups = [i for i, e in enumerate(rig.world.events)
               if e[1] == "amixer" and i > lit_at]
        return bool(ups) and rig.world.events[ups[-1]][2] == 60

    assert wait_until(faded_back_up)
    lit_mark = rig.world.index_of("mark", "lit")
    on_again = rig.world.index_of("relay", "ON", last=True)
    first_up = next(i for i, e in enumerate(rig.world.events)
                    if e[1] == "amixer" and i > lit_mark)
    assert on_again < first_up

    # Shutdown: relay opened, THEN unity written (so it cannot be heard).
    rig.stop()
    events = rig.world.events
    last_off = rig.world.index_of("relay", "OFF", last=True)
    last_fade = rig.world.index_of("amixer", last=True)
    assert last_off < last_fade and events[last_fade][2] == 60
    assert rig.outcome["status"] == 0
    # The main volume was never addressed by anything.
    assert all(argv[:6] == FADE_ARGV_HEAD for argv in rig.mixer.argvs)


def test_every_relay_on_path_revokes_the_off_since_ledger_llr1482(daemon, tmp_path, monkeypatch):
    """Both ways the loop energizes the relay -- the attack rule and the sleep
    machine's wake -- revoke the off-since ledger before the ON (LLR-1482).

    A ledger left standing across an ON would let the NEXT process believe
    the relay had been open since before it was closed, and skip MIN_OFF.
    """
    rig = daemon
    module = rig.module
    real = load_module()
    real.SAFE_STATE_FILE = str(tmp_path / "off-verified")
    monkeypatch.setattr(module, "SAFE_STATE_FILE", real.SAFE_STATE_FILE)
    monkeypatch.setattr(module, "record_safe_state", real.record_safe_state)
    ledger = tmp_path / module.RELAY_OFF_SINCE_NAME
    present_at_on = []
    closes = rig.actuator.start

    def start():
        present_at_on.append(ledger.exists())
        return closes()

    rig.actuator.start = start
    rig.thread.start()

    # The attack rule closes it: the start-up OFF had written the ledger.
    assert wait_until(lambda: present_at_on)
    assert present_at_on == [False]
    assert wait_until(lambda: rig.mixer.value == 60)
    # Dark: the sleep machine opens it, which is a verified OFF.
    rig.facts["lit"] = False
    assert wait_until(lambda: rig.world.index_of("relay", "OFF") is not None)
    assert wait_until(ledger.exists)
    # Lit again: the sleep machine's wake closes it, and revokes first.
    rig.facts["lit"] = True
    assert wait_until(lambda: len(present_at_on) == 2)
    assert present_at_on == [False, False]
    rig.stop()
    assert ledger.exists(), "the shutdown OFF is verified too"


def test_the_loop_with_the_setting_off_is_todays_behaviour_llr1436(daemon):
    rig = daemon
    rig.facts["sleeps"] = False
    rig.thread.start()
    assert wait_until(lambda: rig.world.index_of("relay", "ON") is not None)
    # The close itself settles even with the setting off (SR-143): that is about
    # the amplifier starting. It is the SCREEN that has no say from here on.
    assert wait_until(lambda: rig.mixer.value == 60)
    time.sleep(0.05)
    mark = len(rig.world.events)
    rig.facts["lit"] = False
    time.sleep(0.6)                      # dark for 30 fade-out lengths
    rig.facts["lit"] = True
    time.sleep(0.2)
    before_stop = len(rig.world.events)
    rig.stop()
    dark_window = rig.world.events[mark:before_stop]
    # Merely dark, audio still loud, hold-off far away: the relay was not touched
    # and the stage was not written -- exactly the loop of yesterday.
    assert [e for e in dark_window if e[1] in ("relay", "amixer")] == []
    # The only fade writes are the start-up unity, the close's hold and ramp,
    # and the shutdown unity.
    values = [int(argv[-1]) for argv in rig.mixer.argvs]
    assert values[:2] == [60, 0] and values[-2:] == [60, 60]
    assert values[1:-1] == sorted(values[1:-1])


def test_the_loop_retries_an_unverified_off_while_dark_llr1436(daemon):
    rig = daemon
    rig.thread.start()
    assert wait_until(lambda: rig.world.index_of("relay", "ON") is not None)
    rig.actuator.stop_failures = 2
    rig.facts["lit"] = False
    assert wait_until(lambda: rig.actuator.physical is False and
                      rig.world.index_of("relay", "OFF") is not None)
    failed = [e for e in rig.world.events if e[1:] == ("relay", "OFF-FAILED")]
    assert len(failed) == 2
    rig.stop()


# ── minimum dwell on the relay (fix round 1, item 1) ─────────────────────────

def test_a_wake_waits_out_the_minimum_off_with_the_fade_at_the_floor_llr1436():
    rig = Rig()
    settled_lit(rig)
    rig.run(3.0, lit=False)                    # fade out, relay OFF
    off_at = rig.world.of("relay")[0][0]
    assert rig.sleeper.phase == rig.module.SpeakerSleep.DARK
    rig.world.events.clear()
    rig.run(rig.module.MIN_OFF_SECONDS - 2.0, lit=True, audio=True)
    assert rig.world.events == [], "no relay ON, no fade write, before the dwell"
    assert rig.sleeper.level == rig.module.FADE_FLOOR
    rig.run(4.0, lit=True, audio=True)
    ons = rig.world.of("relay")
    assert ons and ons[0][1] == "ON"
    assert ons[0][0] - off_at >= rig.module.MIN_OFF_SECONDS - 0.01


def test_a_flapping_backlight_cannot_cycle_the_relay_faster_than_the_dwell_llr1436():
    rig = Rig()
    settled_lit(rig)
    for _ in range(40):                        # 4 s dark, 1 s lit, repeated
        rig.run(4.0, lit=False, audio=True)
        rig.run(1.0, lit=True, audio=True)
    commands = [(t, d) for t, k, d in rig.world.events if k == "relay"]
    assert commands, "it did cycle at least once"
    for (t0, d0), (t1, d1) in zip(commands, commands[1:]):
        assert d0 != d1, commands
        floor = rig.module.MIN_OFF_SECONDS if d0 == "OFF" else rig.module.MIN_ON_SECONDS
        assert t1 - t0 >= floor - 0.01, (d0, d1, t1 - t0)


def test_the_relay_stays_on_for_the_minimum_even_when_the_fade_is_done_llr1436():
    rig = Rig()
    go_dark(rig)
    rig.run(0.5, lit=True)                     # relay ON now
    on_at = rig.world.of("relay")[-1][0]
    rig.run(8.0, lit=False)                    # dark again: silent, but OFF must wait
    assert rig.sleeper.level == rig.module.FADE_FLOOR
    assert rig.world.of("relay")[-1][1] == "ON"
    rig.run(rig.module.MIN_ON_SECONDS, lit=False)
    off = rig.world.of("relay")[-1]
    assert off[1] == "OFF" and off[0] - on_at >= rig.module.MIN_ON_SECONDS - 0.01


def test_a_wake_during_the_off_wait_comes_back_without_a_relay_command_llr1436():
    rig = Rig()
    go_dark(rig)
    rig.run(0.5, lit=True)
    rig.run(8.0, lit=False)
    rig.world.events.clear()
    rig.run(3.0, lit=True)
    assert rig.world.of("relay") == []
    assert rig.fade_values()[-1] == 60


def test_the_dwell_values_are_the_existing_contact_protection_llr1436():
    module = load_module()
    assert module.MIN_ON_SECONDS == 30.0 and module.MIN_OFF_SECONDS == 10.0
    assert module.MIN_OFF_SECONDS >= 5.0, "long enough for the boards to power down"


# ── retry and unknown level (items 2 and 3) ──────────────────────────────────

def test_a_partial_fade_in_failure_is_retried_on_the_beat_not_every_30s_llr1436():
    rig = Rig()
    go_dark(rig)
    rig.mixer.fail_values = {60}               # the top step never lands
    rig.run(10.5, lit=True)
    assert rig.sleeper.phase == rig.module.SpeakerSleep.LIT
    assert rig.sleeper.unity_known is False
    rig.mixer.fail_values = set()
    rig.world.events.clear()
    rig.run(6.0, lit=True)                     # one 5 s beat
    assert rig.fade_values() == [60]
    assert rig.mixer.value == 60 and rig.sleeper.unity_known is True


def test_a_failed_write_leaves_the_level_unknown_and_the_next_ramp_reads_it_llr1436():
    rig = Rig()
    settled_lit(rig)
    rig.mixer.fail = True
    rig.sleeper.reset_unity(rig.world.now)     # fails
    assert rig.sleeper.level is None
    rig.mixer.fail = False
    rig.mixer.value = 42                       # where the control really is
    rig.world.events.clear()
    rig.run(3.0, lit=False)
    assert rig.world.of("cget"), "the start was read, not assumed"
    values = rig.fade_values()
    assert values[0] <= 42 and values[-1] == 0


def test_an_unreadable_level_falls_back_to_the_far_end_ramp_llr1436():
    rig = Rig()
    settled_lit(rig)
    rig.sleeper.level = None
    rig.mixer.fail = False
    rig.mixer.value = 60
    rig.sleeper.fade.read = lambda: None
    rig.run(3.0, lit=False)
    assert rig.fade_values()[-1] == 0 and rig.sleeper.level == 0


# ── start-up and idle paths (items 4 and 5) ──────────────────────────────────

def test_the_loop_writes_unity_at_start_up_over_a_stage_left_low_llr1437(daemon):
    rig = daemon
    rig.mixer.value = 7                         # SIGKILLed mid-fade last time
    rig.facts["sleeps"] = False                 # nothing else may touch it
    rig.thread.start()
    assert wait_until(lambda: rig.world.index_of("relay", "ON") is not None)
    first_write = rig.world.index_of("amixer")
    assert rig.world.events[first_write][2] == 60, "restored before anything else"
    assert first_write < rig.world.index_of("relay", "ON")
    rig.stop()


@pytest.mark.parametrize("idle", ["disabled", "panel"])
def test_the_idle_paths_restore_unity_too_llr1437(daemon, monkeypatch, idle):
    rig = daemon
    rig.mixer.value = 7
    if idle == "disabled":
        monkeypatch.setenv("WALL_AMP_ENABLED", "false")
    else:
        monkeypatch.setattr(rig.module, "current_mode", lambda: "panel")
    rig.thread.start()
    assert wait_until(lambda: rig.mixer.value == 60)
    assert rig.world.of("relay")[0][1] == "ensure_off", "safe OFF first"
    assert rig.world.index_of("relay", "ON") is None, "idle: the relay is not commanded"
    rig.stop()


def test_a_failed_unity_write_on_an_idle_path_is_only_logged_llr1437(daemon, monkeypatch):
    rig = daemon
    rig.mixer.fail = True
    monkeypatch.setenv("WALL_AMP_ENABLED", "false")
    rig.thread.start()
    assert wait_until(lambda: rig.mixer.argvs)
    rig.stop()
    assert rig.outcome["status"] == 0


def test_the_explicit_startup_reset_does_not_depend_on_the_machine_stepping_llr1437(
        daemon, monkeypatch):
    """The machine would also heal an unconfirmed unity on its first tick; this
    pins the START-UP write itself, with stepping disabled."""
    rig = daemon
    monkeypatch.setattr(rig.module.SpeakerSleep, "step", lambda self, *a, **k: None)
    LoopLevel.loud = False      # no relay close: its hold would write the floor
    rig.mixer.value = 7
    rig.thread.start()
    assert wait_until(lambda: rig.mixer.value == 60)
    rig.stop()


def test_the_real_loop_honours_the_dwell_through_a_backlight_flap_llr1437(
        daemon, monkeypatch):
    """The call site in main() passes the loop's own relay record to step();
    a flapping backlight must not cycle the relay faster than the dwells."""
    rig = daemon
    monkeypatch.setattr(rig.module, "MIN_ON_SECONDS", 0.6)
    monkeypatch.setattr(rig.module, "MIN_OFF_SECONDS", 0.5)
    rig.thread.start()
    assert wait_until(lambda: rig.world.index_of("relay", "ON") is not None)
    deadline = time.monotonic() + 5.0
    lit = True
    while time.monotonic() < deadline:
        lit = not lit
        rig.facts["lit"] = lit
        # 0.8 s dark leaves 0.4 s beyond the 0.4 s fade-out, so a loaded
        # runner (-n 12) still completes a sleep inside the window.
        time.sleep(0.8 if not lit else 0.3)
    rig.facts["lit"] = True
    rig.stop()
    commands = [(t, d) for t, k, d in rig.world.events
                if k == "relay" and d in ("ON", "OFF")]
    # Drop the shutdown OFF, which is not a sleep transition.
    commands = commands[:-1]
    assert len([c for c in commands if c[1] == "OFF"]) >= 1, "it did sleep"
    for (t0, d0), (t1, d1) in zip(commands, commands[1:]):
        floor = 0.5 if d0 == "OFF" else 0.6
        # Times are stamped at the logger, slightly after the loop's own
        # `now`; 0.15 s absorbs that lag without hiding a real dwell skip.
        assert t1 - t0 >= floor - 0.15, (d0, d1, t1 - t0, commands)


# ── SR-143: the settle follows the relay, not the screen ─────────────────────
# Owner, 2026-11-05: "When in speaker mode, wait 5 seconds after relay turns on
# before fading in audio, otherwise can fade in as soon as presence is detected."
# Owner 2026-11-09: 8.5 s settle; the one-second fade is unchanged.

def close_by_attack_rule(rig):
    """What main()'s attack branch does in the Speaker position."""
    rig.sleeper.hold_for_relay()
    rig.world.log("relay", "ON")
    rig.relay_is_on, rig.relay_changed_at = True, rig.world.now
    rig.sleeper.relay_closed(rig.world.now)


def test_the_settle_is_8s5_by_default_llr1440(monkeypatch):
    monkeypatch.delenv("WALL_AMP_SLEEP_WAKE_SETTLE_SECONDS", raising=False)
    assert load_module().WAKE_SETTLE_SECONDS == 8.5


def test_real_default_holds_until_8s5_then_starts_the_one_second_ramp_llr1440(monkeypatch):
    """Owner 2026-11-09: 8.5 s at the real boundary, no timing override."""
    monkeypatch.delenv("WALL_AMP_SLEEP_WAKE_SETTLE_SECONDS", raising=False)
    rig = Rig()
    settled_lit(rig)
    rig.relay_is_on = False
    close_by_attack_rule(rig)
    on_at = rig.world.now

    def tick(elapsed):
        rig.world.now = on_at + elapsed
        rig.sleeper.step(rig.world.now, False, True, False, True,
                         relay_is_on=True, relay_changed_at=on_at)

    for elapsed in (0.0, 5.0, 8.0, 8.499):
        tick(elapsed)
        assert rig.sleeper.phase == rig.module.SpeakerSleep.SETTLING
        assert rig.mixer.value == rig.module.FADE_FLOOR
    assert rig.fade_values() == [0], "nothing rises before 8.5 seconds"
    tick(8.5)
    assert rig.sleeper.phase == rig.module.SpeakerSleep.FADING_IN
    assert rig.mixer.value == 0, "the ramp starts at the floor at 8.5 seconds"
    tick(8.55)
    assert 0 < rig.mixer.value < 60
    tick(9.0)
    assert rig.mixer.value == 30, "halfway through the unchanged one-second ramp"
    tick(9.5)
    assert rig.mixer.value == 60
    assert rig.sleeper.phase == rig.module.SpeakerSleep.LIT


@pytest.mark.parametrize("enabled", [True, False], ids=["sleep-on", "sleep-off"])
def test_the_attack_rules_close_silences_settles_and_fades_llr1440(enabled):
    """Floor before the relay, nothing for 8.5 seconds, then the ramp to unity.
    The sleep preference has no say: this is the amplifier starting."""
    rig = Rig()
    settled_lit(rig)
    rig.relay_is_on = False
    close_by_attack_rule(rig)
    on_at = rig.world.now
    assert rig.sleeper.governing, "the hold-off rule must not act mid-settle"
    rig.run(10.5, enabled=enabled, lit=True)

    floor = rig.world.index_of("amixer", 0)
    relay_on = rig.world.index_of("relay", "ON")
    assert floor is not None and floor < relay_on, "silent before the relay closes"
    ups = [(t, d) for t, d in rig.world.of("amixer") if d > 0]
    assert ups and ups[0][0] - on_at >= 8.5, ups[:1]
    assert ups[-1][1] == 60 and rig.sleeper.phase == rig.module.SpeakerSleep.LIT
    assert not rig.sleeper.governing, "the relay is the existing rule's again"


def test_a_dark_screen_does_not_stop_the_hold_with_the_preference_off_llr1440():
    rig = Rig()
    settled_lit(rig)
    rig.relay_is_on = False
    close_by_attack_rule(rig)
    rig.run(10.5, enabled=False, lit=False)
    assert rig.fade_values()[-1] == 60
    assert rig.world.of("relay") == [(rig.world.of("relay")[0][0], "ON")], "no OFF"
    assert rig.sleeper.phase == rig.module.SpeakerSleep.LIT


def test_a_dark_screen_during_the_hold_sleeps_with_the_preference_on_llr1440():
    rig = Rig()
    settled_lit(rig)
    rig.relay_is_on = False
    close_by_attack_rule(rig)
    rig.run(rig.module.MIN_ON_SECONDS + 4.0, enabled=True, lit=False)
    assert rig.sleeper.phase == rig.module.SpeakerSleep.DARK
    assert [d for _, d in rig.world.of("relay")] == ["ON", "OFF"]


def test_a_relay_already_closed_fades_in_at_once_llr1440():
    """The "otherwise": the screen lights mid-fade, the relay never opened."""
    rig = Rig()
    settled_lit(rig)
    rig.run(1.0, lit=False)
    assert rig.sleeper.phase == rig.module.SpeakerSleep.FADING_OUT
    rig.world.events.clear()
    lit_at = rig.world.now
    rig.run(2.0, lit=True)
    assert rig.world.of("relay") == []
    assert rig.world.of("amixer")[0][0] - lit_at < 0.2, "no settle to wait for"
    assert rig.fade_values()[-1] == 60


def test_a_relay_still_closed_after_the_fade_out_fades_in_at_once_llr1440():
    """Silent, waiting out MIN_ON_SECONDS: the relay is closed, so no settle."""
    rig = Rig()
    settled_lit(rig)
    rig.relay_changed_at = rig.world.now
    rig.run(4.0, lit=False)
    assert rig.sleeper.phase == rig.module.SpeakerSleep.FADING_OUT
    assert rig.relay_is_on and rig.fade_values()[-1] == 0
    rig.world.events.clear()
    lit_at = rig.world.now
    rig.run(2.0, lit=True)
    assert rig.world.of("relay") == []
    assert rig.world.of("amixer")[0][0] - lit_at < 0.2
    assert rig.fade_values()[-1] == 60


def test_a_screen_bounce_cannot_shorten_the_settle_llr1440():
    """Dark then lit again inside the 8.5 seconds: the boards still get them."""
    rig = Rig()
    go_dark(rig, audio=True)
    rig.world.events.clear()
    rig.run(1.0, lit=True)                       # relay ON, settling
    on_at = rig.world.of("relay")[0][0]
    rig.run(1.0, lit=False)                      # dark again, relay still held ON
    rig.run(10.5, lit=True)
    ups = [(t, d) for t, d in rig.world.of("amixer") if d > 0]
    assert ups and ups[0][0] - on_at >= 8.5, ups[:1]
    assert rig.fade_values()[-1] == 60
    assert [d for _, d in rig.world.of("relay")] == ["ON"]


def test_the_loop_holds_the_stage_down_across_the_close_in_speaker_llr1440(daemon):
    rig = daemon
    rig.thread.start()
    assert wait_until(lambda: rig.world.index_of("relay", "ON") is not None)
    assert wait_until(lambda: rig.mixer.value == 60)
    rig.stop()
    relay_on = rig.world.index_of("relay", "ON")
    writes = [(i, e[0], e[2]) for i, e in enumerate(rig.world.events) if e[1] == "amixer"]
    before = [w for w in writes if w[0] < relay_on]
    after = [w for w in writes if w[0] > relay_on]
    assert before[-1][2] == 0, "the floor is the last write before the relay closes"
    assert after[0][1] - rig.world.events[relay_on][0] >= 0.25, "then the settle"
    assert after[-1][2] == 60


def test_the_loop_leaves_the_stage_alone_outside_speaker_llr1440(daemon):
    rig = daemon
    rig.facts["output"] = "headset"
    rig.thread.start()
    assert wait_until(lambda: rig.world.index_of("relay", "ON") is not None)
    time.sleep(0.6)
    rig.stop()
    assert [argv[-1] for argv in rig.mixer.argvs] == ["60", "60"], "start-up and shutdown only"


def test_a_failed_close_does_not_leave_the_stage_down_llr1440(daemon):
    """The close failed, the relay is open: nothing is heard, and unity returns."""
    rig = daemon
    real_start = rig.actuator.start
    attempts = []

    def refuse_twice():
        attempts.append(1)
        return False if len(attempts) <= 2 else real_start()

    rig.actuator.start = refuse_twice
    rig.thread.start()
    assert wait_until(lambda: len(attempts) >= 3)
    assert wait_until(lambda: rig.world.index_of("relay", "ON") is not None)
    assert wait_until(lambda: rig.mixer.value == 60)
    rig.stop()


def test_the_preference_going_off_cannot_cut_a_wake_settle_short_llr1440():
    rig = Rig()
    go_dark(rig, audio=True)
    rig.world.events.clear()
    rig.run(1.0, lit=True)                       # relay ON, settling
    on_at = rig.world.of("relay")[0][0]
    rig.run(10.5, enabled=False, lit=True)
    ups = [(t, d) for t, d in rig.world.of("amixer") if d > 0]
    assert ups and ups[0][0] - on_at >= 8.5, ups[:1]
    assert rig.fade_values()[-1] == 60
    assert rig.sleeper.phase == rig.module.SpeakerSleep.LIT


def test_a_relay_restarted_after_it_stopped_settles_too_llr1440(daemon):
    """Restarting proves OFF and closes again, so it is a close like any other."""
    rig = daemon
    rig.thread.start()
    assert wait_until(lambda: rig.world.index_of("relay", "ON") is not None)
    assert wait_until(lambda: rig.mixer.value == 60)
    time.sleep(0.05)
    mark = len(rig.world.events)
    rig.actuator.running_flag = False
    assert wait_until(lambda: any(e[1:] == ("relay", "ON") for e in rig.world.events[mark:]))
    assert wait_until(lambda: rig.mixer.value == 60)
    rig.stop()
    tail = rig.world.events[mark:]
    closed = next(i for i, e in enumerate(tail) if e[1:] == ("relay", "ON"))
    writes = [e[2] for e in tail[:closed] if e[1] == "amixer"]
    assert writes and writes[-1] == 0, "silent before the relay closes again"
    rising = [e[0] for e in tail[closed:] if e[1] == "amixer" and e[2] > 0]
    assert rising and rising[0] - tail[closed][0] >= 0.25, "and then the settle"


def test_a_failed_restart_mid_fade_in_does_not_stop_the_machine_llr1440():
    """hold_for_relay() takes the ramp, and a failed start leaves the phase alone."""
    rig = Rig()
    settled_lit(rig)
    rig.relay_is_on = False
    close_by_attack_rule(rig)
    rig.run(8.8, lit=True)
    assert rig.sleeper.phase == rig.module.SpeakerSleep.FADING_IN
    rig.sleeper.hold_for_relay()                 # main() then finds start() failed
    rig.run(1.0, lit=True)
    assert rig.sleeper.phase == rig.module.SpeakerSleep.LIT
    rig.run(6.0, lit=True)
    assert rig.fade_values()[-1] == 60, "the beat puts unity back"


def test_a_restarted_relay_keeps_its_minimum_on_time_llr1440(daemon, monkeypatch):
    """The restart is a real close, so the dwell runs from it."""
    rig = daemon
    monkeypatch.setattr(rig.module, "MIN_ON_SECONDS", 0.8)
    rig.thread.start()
    assert wait_until(lambda: rig.mixer.value == 60)
    time.sleep(0.8)                              # past the first close's dwell
    mark = len(rig.world.events)
    rig.actuator.running_flag = False
    assert wait_until(lambda: any(e[1:] == ("relay", "ON") for e in rig.world.events[mark:]))
    rig.facts["lit"] = False
    assert wait_until(lambda: any(e[1:] == ("relay", "OFF") for e in rig.world.events[mark:]))
    rig.stop()
    tail = rig.world.events[mark:]
    closed = next(e[0] for e in tail if e[1:] == ("relay", "ON"))
    opened = next(e[0] for e in tail if e[1:] == ("relay", "OFF"))
    assert opened - closed >= 0.65, (closed, opened)
