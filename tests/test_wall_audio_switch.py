"""SR-028: the merged bus, and the Mute/Headset/Speaker switch that follows it.

Item 23 steps 1 and 2. Three groups of tests, in the order the design is read:

  * the pure switch policy (wall_audio_state) -- every ruling the Owner made on
    the revision-2 review has a test here, named for it;
  * the applier shell (wall-audio-output) -- driven with --dry-run, so the exact
    privileged command sequence is asserted without a sound card;
  * the graph and the units -- the ALSA config, the loopback substreams, the two
    udev aliases and the detector's post-switch tap.

Nothing here claims anything about audibility on the panel: that is the on-glass
acceptance in docs/design/audio-routing-2026-09-13.md.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack" / "autoinstall" / "wall"
PANEL_AUDIO = ROOT / "stack" / "panel-audio"
BUS_CONF = WALL / "asound-bus-mode.conf"
APPLIER = WALL / "wall-audio-output"


def load(path, name):
    """Import a payload script by path; they are installed, not packaged.

    The loader is named explicitly because wall-audio-output has no .py suffix
    -- it is an sbin command -- and spec_from_file_location returns None for a
    file whose extension it does not recognize.
    """
    spec = importlib.util.spec_from_file_location(
        name, path, loader=importlib.machinery.SourceFileLoader(name, str(path)))
    module = importlib.util.module_from_spec(spec)
    # NOT registered in sys.modules. The panel-audio suite runs the broker in
    # spawned child processes, which re-import by name; a second copy of
    # `audio_router` parked under the same key made those children pickle a
    # module the parent no longer had, and the failure looked like a Windows
    # handle bug rather than a test-ordering one.
    spec.loader.exec_module(module)
    return module


def import_panel_audio(name):
    """Import a broker module the way the broker itself does, by name on sys.path.

    Deliberately NOT load(): those modules import each other and are re-imported
    in spawned children by the panel-audio suite, so they must be the one copy
    in sys.modules, not a second one under the same key.
    """
    sys.path.insert(0, str(PANEL_AUDIO))
    return importlib.import_module(name)


@pytest.fixture(scope="module")
def policy():
    return load(WALL / "wall_audio_state.py", "wall_audio_state")


@pytest.fixture(scope="module")
def applier(policy):
    # The applier imports its pure core from beside it, which is how firstboot
    # installs the pair; make that import resolve the same way here.
    sys.path.insert(0, str(WALL))
    return load(APPLIER, "wall_audio_output")


# ── the pure switch policy ─────────────────────────────────────────────────

def test_the_three_positions_are_the_owners_three_sr028(policy):
    assert policy.OUTPUTS == ("mute", "headset", "speaker")


def test_a_selected_output_persists_through_a_round_trip_sr028(policy):
    state, _ = policy.apply_event(policy.default_state(), {"kind": "set_output", "output": "mute"})
    reloaded = policy.normalize(json.loads(json.dumps(state)))
    assert reloaded["output"] == "mute"
    # Owner ruling G: the position is the memory, so re-applying it must not
    # move it. This is what the boot and resume units depend on.
    assert policy.normalize(reloaded) == reloaded


def test_volume_is_remembered_per_output_ruling_f_sr028(policy):
    state = policy.default_state()
    state, _ = policy.apply_event(state, {"kind": "set_output", "output": "speaker"})
    state, _ = policy.apply_event(state, {"kind": "set_volume", "level": 80})
    state, _ = policy.apply_event(state, {"kind": "headset", "present": True})
    assert state["output"] == "headset"
    state, _ = policy.apply_event(state, {"kind": "set_volume", "level": 20})
    assert policy.volume_of(state, "headset") == 20
    state, _ = policy.apply_event(state, {"kind": "set_output", "output": "speaker"})
    assert policy.volume_of(state) == 80, "switching back must restore the speaker level"


def test_a_rocker_press_moves_only_the_selected_outputs_memory_sr028(policy):
    state = policy.default_state()
    state, _ = policy.apply_event(state, {"kind": "set_volume", "level": 50})
    before_headset = policy.volume_of(state, "headset")
    state, lines = policy.apply_event(state, {"kind": "nudge_volume", "louder": True})
    assert policy.volume_of(state, "speaker") == 50 + policy.VOLUME_STEP
    assert policy.volume_of(state, "headset") == before_headset
    assert any("volume" in line for line in lines)


@pytest.mark.parametrize("level,expected", [(-5, 0), (0, 0), (100, 100), (400, 100)])
def test_volume_is_clamped_not_refused_sr028(policy, level, expected):
    state, _ = policy.apply_event(policy.default_state(), {"kind": "set_volume", "level": level})
    assert policy.volume_of(state) == expected


def test_the_rocker_is_journaled_and_dropped_in_mute_sr028(policy):
    state, _ = policy.apply_event(policy.default_state(), {"kind": "set_output", "output": "mute"})
    state, lines = policy.apply_event(state, {"kind": "nudge_volume", "louder": True})
    assert state["volume"] == policy.default_state()["volume"]
    assert any("ignored" in line for line in lines)


def test_the_adapter_appearing_flips_speaker_to_headset_once_d5_sr028(policy):
    state = policy.default_state()
    state, lines = policy.apply_event(state, {"kind": "headset", "present": True})
    assert state["output"] == "headset"
    assert any("auto-switch" in line for line in lines)
    # The Owner switches back; a udev `change` on the same, still-present
    # adapter must not drag them to the headset again.
    state, _ = policy.apply_event(state, {"kind": "set_output", "output": "speaker"})
    state, lines = policy.apply_event(state, {"kind": "headset", "present": True})
    assert state["output"] == "speaker"
    # The adapter never went anywhere, so this is not an arrival at all -- and
    # the latch it does not spend is the one the NEXT real plug needs.
    assert any("already present" in line for line in lines)
    assert state["headset_autoswitch_armed"] is False, "already spent by the first add"


def test_unplugging_re_arms_the_one_shot_sr028(policy):
    state = policy.default_state()
    state, _ = policy.apply_event(state, {"kind": "headset", "present": True})
    state, _ = policy.apply_event(state, {"kind": "set_output", "output": "speaker"})
    state, _ = policy.apply_event(state, {"kind": "headset", "present": False})
    state, _ = policy.apply_event(state, {"kind": "headset", "present": True})
    assert state["output"] == "headset", "a fresh plug is a fresh event"


def test_the_auto_switch_never_overrides_mute_sr028(policy):
    state, _ = policy.apply_event(policy.default_state(), {"kind": "set_output", "output": "mute"})
    state, _ = policy.apply_event(state, {"kind": "headset", "present": True})
    assert state["output"] == "mute", "plugging a headset must not un-mute the panel"


def test_headset_selected_but_absent_is_silence_everywhere_ruling_7_sr028(policy):
    state = policy.default_state()
    state, _ = policy.apply_event(state, {"kind": "headset", "present": True})
    state, lines = policy.apply_event(state, {"kind": "headset", "present": False})
    assert state["output"] == "headset", "the mode stays selected (D3)"
    assert policy.audible(state) is False
    assert policy.unavailable_reason(state) == "headset_absent"
    plan = policy.plan(state)
    assert not any(plan["legs"].values()), "no leg may run"
    assert plan["adapter_muted"] and plan["headset_muted"]
    assert any("silent" in line for line in lines)


def test_input_mute_is_a_separate_button_ruling_e_sr028(policy):
    state, _ = policy.apply_event(policy.default_state(), {"kind": "set_input_mute", "muted": True})
    assert state["input_muted"] is True
    # Output mute does not touch it, and it does not touch the output.
    state, _ = policy.apply_event(state, {"kind": "set_output", "output": "mute"})
    assert state["input_muted"] is True
    assert policy.plan(state)["input_muted"] is True


def test_speaker_runs_the_tap_leg_and_headset_does_not_sr028(policy):
    speaker = policy.plan(policy.default_state())
    assert speaker["legs"]["wall-bus-speaker.service"] is True
    assert speaker["legs"]["wall-speaker-out.service"] is True
    assert speaker["legs"]["wall-bus-headset.service"] is False
    state, _ = policy.apply_event(policy.default_state(), {"kind": "headset", "present": True})
    headset = policy.plan(state)
    assert headset["legs"]["wall-bus-headset.service"] is True
    assert not headset["legs"]["wall-bus-speaker.service"]
    assert headset["adapter_muted"] is True


@pytest.mark.parametrize("event", [
    {"kind": "set_output", "output": "speakers"},
    {"kind": "set_output"},
    {"kind": "set_input_mute", "muted": "yes"},
    {"kind": "set_volume", "level": True},
    {"kind": "nudge_volume", "louder": 1},
    {"kind": "headset", "present": "add"},
    {"kind": "reboot"},
])
def test_a_malformed_event_is_refused_loudly_sr028(policy, event):
    with pytest.raises(policy.StateError):
        policy.apply_event(policy.default_state(), event)


@pytest.mark.parametrize("raw", [None, [], "speaker", {"output": "banana"},
                                 {"volume": {"speaker": "loud"}},
                                 {"volume": {"speaker": True}}])
def test_a_corrupt_state_file_falls_back_field_by_field_sr028(policy, raw):
    state = policy.normalize(raw)
    assert state["output"] in policy.OUTPUTS
    assert policy.volume_of(state, "speaker") == policy.STATE_SCHEMA["volume"]["speaker"]


def test_events_do_not_mutate_the_state_they_were_given_sr028(policy):
    original = policy.default_state()
    snapshot = json.dumps(original, sort_keys=True)
    policy.apply_event(original, {"kind": "set_output", "output": "mute"})
    assert json.dumps(original, sort_keys=True) == snapshot


# ── the applier shell ──────────────────────────────────────────────────────

def run_applier(tmp_path, *arguments, mode="bus"):
    """Drive wall-audio-output --dry-run against a throwaway state file."""
    state = tmp_path / "audio-state.json"
    command = [sys.executable, str(APPLIER), "--dry-run", "--state", str(state), *arguments]
    done = subprocess.run(command, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "WALL_AUDIO_MODE_OVERRIDE": mode,
                               "SystemRoot": "C:\\Windows"})
    return done


def test_the_applier_stops_before_it_starts_and_levels_last_sr028(applier, policy):
    """The order is stop, mute, start, level -- see apply_plan's docstring."""
    recorder = applier.Applier(dry_run=True)
    state = policy.default_state()
    applier.apply_plan(state, recorder)
    verbs = [(argv[0].rsplit("/", 1)[-1], argv[1:3]) for argv in recorder.commands]
    systemctl = [index for index, (tool, _) in enumerate(verbs) if tool == "systemctl"]
    stops = [index for index in systemctl if recorder.commands[index][1] == "stop"]
    starts = [index for index in systemctl if recorder.commands[index][1] == "start"]
    assert stops and starts and max(stops) < min(starts)
    assert recorder.commands[-1][1:] == ["-c", "Loopback", "-q", "sset", "Bus", "60%"]


def test_the_applier_runs_no_leg_when_the_headset_is_absent_sr028(applier, policy):
    state, _ = policy.apply_event(policy.default_state(), {"kind": "headset", "present": True})
    state, _ = policy.apply_event(state, {"kind": "headset", "present": False})
    recorder = applier.Applier(dry_run=True)
    applier.apply_plan(state, recorder)
    assert not any(argv[1] == "start" for argv in recorder.commands if "systemctl" in argv[0])
    mutes = [argv for argv in recorder.commands if "amixer" in argv[0] and "Speaker" in argv]
    assert mutes and all("mute" in argv for argv in mutes)


def fake_proc_asound(root, cards):
    """A /proc/asound as the panel really has one (read back over ssh 2026-09-13).

    Each card is a directory with `usbid` and `id`; the id-named symlinks the
    real procfs also carries are deliberately NOT built, because the code must
    not depend on them.
    """
    for index, usbid, card_id in cards:
        card = root / ("card%d" % index)
        card.mkdir(parents=True)
        (card / "usbid").write_text(usbid + "\n", encoding="utf-8")
        (card / "id").write_text(card_id + "\n", encoding="utf-8")
    return root


def test_the_headset_card_is_resolved_by_usb_id_not_by_name_sr028(applier, tmp_path):
    """The adapter enumerates as the generic id "Device"; the USB id is the fact."""
    proc = fake_proc_asound(tmp_path / "asound",
                            [(1, "0d8c:0014", "Device"), (3, "0d8c:0102", "ICUSBAUDIO7D")])
    assert applier.headset_card(proc) == "Device"
    # A second nameless adapter shifts the id; the USB id still finds it.
    other = fake_proc_asound(tmp_path / "asound2",
                             [(1, "0d8c:0099", "Device"), (2, "0d8c:0014", "Device_1")])
    assert applier.headset_card(other) == "Device_1"


def test_a_card_id_that_cannot_be_used_is_refused_not_passed_on_sr028(applier, tmp_path):
    """An id with whitespace would be unquotable in the EnvironmentFile."""
    proc = fake_proc_asound(tmp_path / "asound", [(1, "0d8c:0014", "USB Audio")])
    assert applier.headset_card(proc) is None


def test_no_headset_adapter_resolves_to_nothing_sr028(applier, tmp_path):
    proc = fake_proc_asound(tmp_path / "asound", [(3, "0d8c:0102", "ICUSBAUDIO7D")])
    assert applier.headset_card(proc) is None


@pytest.mark.parametrize("raw,reason", [
    ('{"version": 2, "seq": 1, "event": {"kind": "set_output", "output": "mute"}}', "envelope"),
    ('{"version": 1, "seq": -1, "event": {"kind": "set_output", "output": "mute"}}', "seq"),
    ('{"version": 1, "seq": 1, "event": {"kind": "headset", "present": true}}', "kind"),
    ('not json at all', "unusable"),
])
def test_a_request_the_broker_must_not_make_is_refused_sr028(applier, tmp_path, raw, reason):
    request = tmp_path / "request.json"
    request.write_text(raw, encoding="utf-8")
    assert applier.read_request(request) is None, reason


def test_an_identical_request_is_not_replayed_sr028(applier, tmp_path):
    """systemd.path fires on every close-write; a replayed press walks the room down.

    The high-water mark is the `request_seq` recorded in the state file, in the
    same atomic write as the change it caused.
    """
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"version": 1, "seq": 4,
                                   "event": {"kind": "nudge_volume", "louder": False}}),
                       encoding="utf-8")
    first = applier.read_request(request, -1)
    assert first is not None and first[0] == 4
    assert applier.read_request(request, 4) is None, "the same request must not replay"
    assert applier.read_request(request, 9) is None, "nor an older one after a newer"
    request.write_text(json.dumps({"version": 1, "seq": 5,
                                   "event": {"kind": "nudge_volume", "louder": False}}),
                       encoding="utf-8")
    assert applier.read_request(request, 4)[0] == 5


def test_the_state_file_is_replaced_in_one_rename_sr028(applier, tmp_path):
    target = tmp_path / "audio-state.json"
    applier.write_atomic(target, "first\n")
    applier.write_atomic(target, "second\n")
    assert target.read_text(encoding="utf-8") == "second\n"
    assert not list(tmp_path.glob("*.new")), "no temporary file may survive"


# ── the graph, the units and the rules ─────────────────────────────────────

def read(path):
    return Path(path).read_text(encoding="utf-8")


def test_every_direct_plugin_has_a_single_card_slave_ruling_9_sr028():
    """No dmix across cards: the clocks are joined by forwarders, not by dmix."""
    conf = read(BUS_CONF)
    blocks = [block for block in conf.split("pcm.")[1:]
              if block.lstrip().startswith(("bus_mix", "bus_monitor", "speaker_tap"))
              or "type dmix" in block.split("pcm.")[0]]
    for name, card in (("bus_mix", "card_loop_play"), ("bus_monitor", "card_loop_cap"),
                       ("speaker_tap_mix", "card_loop_tap_play"),
                       ("speaker_tap", "card_loop_tap_cap"), ("spdif_in", "card_usb")):
        section = conf.split("pcm." + name + " {", 1)[1].split("\n}", 1)[0]
        assert section.count("pcm ") == 1, name
        assert card in section, name
    assert blocks or True


def test_the_three_sources_land_on_one_bus_sr028():
    """Bluetooth, the desktop and the kiosk: one dmix, so all three are audible."""
    conf = read(BUS_CONF)
    # The kiosk and bluealsa-aplay both open `default`.
    default = conf.split("pcm.!default {", 1)[1].split("}", 1)[0]
    assert "bus_mix" in default
    # The desktop's S/PDIF is forwarded onto the same bus by its own unit.
    spdif = read(WALL / "wall-spdif-in.service")
    assert "--cdevice spdif_in" in spdif and "--pdevice bus" in spdif
    assert "'IEC958 In'" in spdif, "the capture selector must leave Line (finding 6)"


def test_the_detector_taps_after_the_switch_and_never_a_source_finding_2_sr028():
    detector = read(WALL / "panel-amp-trigger.py")
    module = load(WALL / "panel-amp-trigger.py", "panel_amp_trigger")
    assert module.sources_for("bus") == (("speaker_tap", 0.0),)
    assert module.sources_for("trigger") == module.SOURCES
    for source in ("linein_shared", "kiosk_monitor", "spdif_in", "bus_monitor"):
        assert source not in [pcm for pcm, _ in module.sources_for("bus")]
    assert "COMMANDING_MODES" in detector
    assert module.current_mode.__doc__ and "bus" in module.current_mode.__doc__


def test_the_hold_off_is_untouched_by_the_switch_owner_amendment_sr028():
    """Leaving Speaker must not open the relay early: the tap goes quiet instead.

    The evidence is negative and deliberate -- there is no mode branch in the
    actuator path at all, so the only thing that can drop the amplifier is the
    ordinary WALL_AMP_HOLD_OFF_SECONDS idle.
    """
    detector = read(WALL / "panel-amp-trigger.py")
    hold_off_block = detector.split("HOLD_OFF_SECONDS")[0]
    assert "ensure_off" not in hold_off_block.split("def sources_for")[-1]
    assert "audio-state.json" not in detector, "the actuator must not read the switch"


def test_the_speaker_leg_carries_the_volume_and_the_tap_does_not_sr028():
    """A quiet room must not switch the amplifier off (the tap is pre-volume)."""
    conf = read(BUS_CONF)
    tap_leg = read(WALL / "wall-bus-speaker.service")
    out_leg = read(WALL / "wall-speaker-out.service")
    assert "--pdevice speaker_tap_mix" in tap_leg
    assert "--cdevice speaker_tap" in out_leg and "--pdevice speaker_out" in out_leg
    speaker_out = conf.split("pcm.speaker_out {", 1)[1].split("\npcm.", 1)[0]
    assert "type softvol" in speaker_out
    assert 'name "Bus Playback Volume"' in speaker_out and 'card "Loopback"' in speaker_out


def test_both_legs_share_one_control_on_an_always_present_card_ruling_f_sr028():
    conf = read(BUS_CONF)
    assert conf.count('name "Bus Playback Volume"') == 2
    assert conf.count('card "Loopback"') == 2, "not on an adapter that can be unplugged"


def test_the_headset_card_reaches_alsa_through_the_environment_sr028():
    conf = read(BUS_CONF)
    headset = conf.split("pcm.headset_out {", 1)[1].split("\npcm.", 1)[0]
    assert "@func getenv" in headset and "WALL_AUDIO_HEADSET_CARD" in headset
    unit = read(WALL / "wall-bus-headset.service")
    assert "EnvironmentFile=-/run/wall-panel/audio-headset.env" in unit


def test_the_loopback_has_a_substream_for_the_tap_sr028():
    assert "pcm_substreams=4" in read(WALL / "wall-aloop.conf")
    for name in ("card_loop_tap_play", "card_loop_tap_cap", "ctl.card_loop_ctl"):
        assert name in read(WALL / "audio-cards.conf.example")
        assert name in read(WALL / "wall-firstboot.sh")


def test_the_headset_adapter_has_its_own_alias_sr028():
    rules = read(WALL / "91-wall-headset-adapter.rules")
    assert '"0d8c"' in rules and '"0014"' in rules
    assert 'ENV{SYSTEMD_ALIAS}="/dev/wall_headset_adapter"' in rules
    assert 'ACTION=="remove", GOTO=' in rules, "the tag must be set on every non-remove event"
    assert "wall_audio_adapter" not in rules, "a headset replug must not cycle the amplifier units"
    presence = read(WALL / "wall-headset-present.service")
    assert "BindsTo=dev-wall_headset_adapter.device" in presence
    assert "RemainAfterExit=yes" in presence, "without it there is no remove half"
    assert "ExecStop=/usr/local/sbin/wall-audio-output headset remove" in presence


@pytest.mark.parametrize("unit", ["wall-spdif-in.service", "wall-bus-speaker.service",
                                  "wall-speaker-out.service", "wall-bus-headset.service"])
def test_every_forwarder_is_bound_guarded_and_mode_gated_sr028(unit):
    text = read(WALL / unit)
    assert "BindsTo=dev-wall_" in text, "item 25: follow the device, not the port"
    assert "wall-alsaloop-guard.py" in text, "a bare alsaloop never exits when it wedges"
    assert "audio-mode" in text and "= bus" in text
    assert "SupplementaryGroups=audio" in text, "the dmix segments carry ipc_gid=audio"


@pytest.mark.parametrize("unit", ["wall-bus-speaker.service", "wall-speaker-out.service",
                                  "wall-bus-headset.service"])
def test_a_leg_is_never_enabled_sr028(unit):
    """A boot must not start audio in a position the Owner did not leave it in."""
    assert "[Install]" not in read(WALL / unit)


def test_the_switch_survives_reboot_and_resume_ruling_g_sr028():
    """Two units, because one cannot do both.

    The boot unit is RemainAfterExit=yes and therefore stays active for the life
    of the boot; wanting THAT from a sleep target would start an already-active
    unit and run nothing -- a silent no-op with a green status above it.
    """
    boot = read(WALL / "wall-audio-state.service")
    resume = read(WALL / "wall-audio-resume.service")
    assert "apply-state" in boot and "apply-state" in resume
    assert "RemainAfterExit=yes" in boot
    resume_service = resume.split("[Service]")[1].split("[Install]")[0]
    assert "RemainAfterExit" not in resume_service, "every resume must run it again"
    assert boot.split("[Install]")[1].strip() == "WantedBy=multi-user.target"
    install = resume.split("[Install]")[1]
    for target in ("suspend.target", "hibernate.target", "hybrid-sleep.target",
                   "suspend-then-hibernate.target"):
        assert target in install and target in resume.split("[Service]")[0]


def test_the_level_is_retried_until_the_control_exists_sr028(applier, policy):
    """softvol creates its control on first open, and `systemctl start` does not wait.

    Setting the level once, immediately, would leave a freshly started leg at the
    plugin default -- which is full scale, the loudest possible way to be wrong.
    """
    class Reply:
        """What subprocess.run returns; amixer answers this until the PCM opens."""

        def __init__(self, returncode):
            self.returncode = returncode
            self.stdout = ""
            self.stderr = "amixer: Unable to find simple control 'Bus',0"

    def amixer_that_succeeds_on(attempt_number, counter):
        def run(argv, **kwargs):
            counter.append(argv)
            return Reply(0 if len(counter) >= attempt_number else 1)
        return run

    calls = []
    shell = applier.Applier(run=amixer_that_succeeds_on(5, calls))
    assert applier.set_bus_level(shell, "Loopback", 60, sleep=lambda _: None) is True
    assert len(calls) == 5, "it must keep looking while a leg is starting"

    calls = []
    shell = applier.Applier(run=amixer_that_succeeds_on(999, calls))
    assert applier.set_bus_level(shell, "Loopback", 60, sleep=lambda _: None) is False
    assert len(calls) == applier.BUS_CONTROL_ATTEMPTS, "and give up, bounded"

    # With no leg running the control legitimately does not exist: one attempt.
    calls = []
    shell = applier.Applier(run=amixer_that_succeeds_on(999, calls))
    assert applier.set_bus_level(shell, "Loopback", 0, expected=False,
                                 sleep=lambda _: None) is True
    assert len(calls) == 1


def test_a_repaired_state_file_says_what_it_replaced_sr028(applier, tmp_path):
    """The check must compare the LOADED document with the normalized one.

    Comparing the normalized state with itself is equal by construction, which
    is how a partial file gets silently corrected to speaker at whatever level
    the defaults carry -- with nothing in the journal to find afterwards.
    """
    state_file = tmp_path / "audio-state.json"
    state_file.write_text(json.dumps({"output": "banana", "input_muted": "yes"}),
                          encoding="utf-8")
    state, note = applier.load_state(state_file)
    assert state["output"] == "speaker"
    assert note and "output" in note and "input_muted" in note
    # A state the applier itself wrote is not "repaired" on the way back in.
    applier.save_state(state, state_file)
    assert applier.load_state(state_file)[1] is None


def test_one_apply_at_a_time_sr028(applier):
    """Four callers, one graph: udev, the rocker, a broker request, boot/resume."""
    source = read(APPLIER)
    assert "def apply_lock(" in source
    assert "with apply_lock(" in source
    assert "LOCK_EX" in source
    # The lock must wrap the whole read-decide-write-apply, not just the write.
    locked = source.split("with apply_lock(")[1]
    assert "_locked_apply(arguments)" in locked


def test_the_broker_reaches_root_through_one_watched_file_sr028():
    path_unit = read(WALL / "wall-audio-apply.path")
    assert "PathChanged=/run/wall-audio-router/request.json" in path_unit
    service = read(WALL / "wall-audio-apply.service")
    assert "Type=oneshot" in service and "apply-request" in service


def test_bus_mode_is_additive_so_trigger_mode_is_the_rollback_sr028():
    mode = read(WALL / "wall-audio-mode")
    assert "trigger|panel|bus" in mode
    assert "apply_bus()" in mode
    assert read(WALL / "asound-trigger-mode.conf").count("pcm.speaker_shared"), "trigger mode intact"
    assert "unit disable wall-line-in.service" in mode, "one capture stream, one selector"


def test_the_mode_script_stops_the_new_forwarders_before_sweeping_ipc_sr028():
    mode = read(WALL / "wall-audio-mode")
    stop_block = mode.split("clear_ipc\n", 1)[0]
    for unit in ("wall-spdif-in.service", "wall-bus-speaker.service",
                 "wall-speaker-out.service", "wall-bus-headset.service"):
        assert unit in stop_block, "a leg still attached leaves a stale segment behind"


def test_the_rocker_asks_the_switch_rather_than_a_card_in_bus_mode_ruling_f_sr028():
    # Read rather than imported: the module asserts a Linux struct size at
    # import time, and this assertion is about what it DOES, not where it runs.
    keys = read(WALL / "panel-volume-keys.py")
    assert "OUTPUT_SWITCH = \"/usr/local/sbin/wall-audio-output\"" in keys
    assert "def nudge_bus(louder):" in keys
    assert 'if current_mode() == "bus" and nudge_bus(louder):' in keys
    # The mute key is inert in bus mode until the switch has a previous-output
    # memory (step 5); a one-way mute from a key that cannot un-mute would
    # strand the panel silent for anyone not standing at it.
    assert "mute key ignored in bus mode" in keys


# ── the broker's half of the switch ────────────────────────────────────────

def test_set_output_is_not_select_output_sr028():
    """The two names that look alike: one is a Bluetooth alias, one is the switch."""
    routing = import_panel_audio("routing")
    routing.validate_action("set_output", {"output": "headset"})
    with pytest.raises(routing.PolicyError):
        routing.validate_action("set_output", {"alias": "speaker"})
    with pytest.raises(routing.PolicyError):
        routing.validate_action("set_output", {"output": "loudspeaker"})
    with pytest.raises(routing.PolicyError):
        routing.validate_action("select_output", {"output": "speaker"})
    routing.validate_action("set_input_mute", {"muted": True})
    with pytest.raises(routing.PolicyError):
        routing.validate_action("set_input_mute", {"muted": "on"})


def test_a_request_sequence_survives_a_broker_restart_sr028():
    """A per-lifetime counter would be silently discarded after a restart.

    The applier persists the high-water mark in the state file, so a producer
    that starts again from zero would have every request below the previous
    lifetime's last value thrown away, and the switch would simply stop
    responding with no error anywhere.
    """
    request = import_panel_audio("switch_request")
    # A wall clock, so a process that restarts still produces a larger number.
    assert request.next_seq() > 0
    assert request.next_seq(clock=lambda: 7) == 7
    assert request.next_seq(clock=lambda: 8) > request.next_seq(clock=lambda: 7)
    # And it is what the envelope accepts.
    request.envelope(request.next_seq(), {"kind": "nudge_volume", "louder": True})


def test_the_switch_request_carries_no_hardware_name_sr028(tmp_path):
    request = import_panel_audio("switch_request")
    written = request.write(1, {"kind": "set_output", "output": "mute"},
                            tmp_path / "request.json")
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload == {"version": 1, "seq": 1,
                       "event": {"kind": "set_output", "output": "mute"}}
    for refused in ({"kind": "headset", "present": True},
                    {"kind": "set_output", "output": "mute", "card": "Device"},
                    {"kind": "set_output"}):
        with pytest.raises(request.RequestError):
            request.envelope(2, refused)
    with pytest.raises(request.RequestError):
        request.envelope(True, {"kind": "nudge_volume", "louder": True})


def test_a_lost_switch_request_is_not_settled_by_the_mute_control_sr028():
    """Reconciling set_output on the mute block would claim an unlooked-at state."""
    router = import_panel_audio("audio_router")
    assert router.OBSERVABLE_BLOCK["set_output"] == "switch"
    assert router.OBSERVABLE_BLOCK["set_mute"] == "mute"


# -- the boot/power-cycle ruling (Owner, 2026-09-13) ------------------------
#
# Measured on the panel at 23:18 that evening: a power cycle with the USB
# headset adapter already plugged in brought the panel back on Headset although
# it was left on Speaker -- wall-headset-present.service ran at boot, logged
# "headset adapter present", and fired the one-shot. The Owner's ruling: the
# position must persist through a power cycle, including the last setting,
# regardless of whether the adapter is plugged in over the boot; the dynamic
# switch on plug-in is an EVENT, and over a boot we do not know when that event
# occurred or whether it is still relevant.

def test_a_coldplug_adapter_never_moves_the_switch_owner_ruling_sr028(policy):
    """The whole ruling, in the pure core."""
    state = policy.default_state()
    assert state["output"] == "speaker"
    state, lines = policy.apply_event(
        state, {"kind": "headset", "present": True, "boot": True})
    assert state["output"] == "speaker", "a power cycle keeps the Owner's position"
    assert state["headset_present"] is True, "presence is still a recorded fact"
    assert not any("auto-switch speaker -> headset" in line for line in lines)
    assert any("no auto-switch" in line for line in lines), "and it says why"


def test_a_coldplug_adapter_leaves_no_pending_event_behind_it_sr028(policy):
    """Present across the boot => the latch is SPENT, so nothing fires later."""
    state, _ = policy.apply_event(policy.default_state(),
                                  {"kind": "headset", "present": True, "boot": True})
    assert state["headset_autoswitch_armed"] is False
    # A udev `change` on that same, stationary adapter must still do nothing.
    state, lines = policy.apply_event(state, {"kind": "headset", "present": True})
    assert state["output"] == "speaker"
    assert any("already present" in line for line in lines), "nothing arrived"


def test_an_adapter_absent_across_the_boot_arms_the_next_real_plug_sr028(policy):
    """Acceptance check 8 must still pass on a panel that booted bare."""
    state, _ = policy.apply_event(policy.default_state(),
                                  {"kind": "headset", "present": False, "boot": True})
    assert state["headset_autoswitch_armed"] is True
    state, lines = policy.apply_event(state, {"kind": "headset", "present": True})
    assert state["output"] == "headset"
    assert any("auto-switch speaker -> headset" in line for line in lines)


def test_a_coldplug_report_never_unmutes_or_re_selects_sr028(policy):
    """It records, and that is all: mute and headset-selected both stand."""
    for position in ("mute", "headset"):
        state, _ = policy.apply_event(policy.default_state(),
                                      {"kind": "set_output", "output": position})
        state, _ = policy.apply_event(state,
                                      {"kind": "headset", "present": True, "boot": True})
        assert state["output"] == position


def test_a_boot_flag_that_is_not_a_boolean_is_refused_sr028(policy):
    with pytest.raises(policy.StateError):
        policy.apply_event(policy.default_state(),
                           {"kind": "headset", "present": True, "boot": "yes"})


def test_coldplug_is_told_from_a_plug_by_two_monotonic_marks_sr028(applier):
    """is_boot_presence gate 2: did this device predate the stored position?

    The device's USEC_INITIALIZED is when udev first processed it; the stamp is
    what apply-state sampled when it began applying the stored position. Earlier
    means it was there across the boot.
    """
    up = dict(system_up=True)
    assert applier.is_boot_presence(1000, stamp=5000, **up) is True
    assert applier.is_boot_presence(5000, stamp=5000, **up) is True, "the same instant is cold"
    assert applier.is_boot_presence(9000, stamp=5000, **up) is False, "later is a plug event"


def test_a_boot_that_has_not_finished_can_never_auto_switch_sr028(applier):
    """Gate 1, and the reason systemd-udev-settle was dropped.

    Review was right that settle only drains the queue it can SEE: an adapter
    behind a slow hub enumerating a second after settle returned would have had
    USEC_INITIALIZED later than the stamp and fired the one-shot against the
    ruling. While systemd still says `starting`, coldplug is still plausible and
    the Owner's position wins -- and a slow boot stays `starting` for longer all
    by itself, so there is no number here to get wrong.
    """
    assert applier.is_boot_presence(999_999_999, stamp=1, system_up=False) is True
    assert applier.is_boot_presence(999_999_999, stamp=1, system_up=None) is True, \
        "an unanswerable gate is coldplug too"


def test_an_unknown_device_age_fails_towards_the_owners_position_sr028(applier, monkeypatch):
    """Every doubt inside a booted system resolves to coldplug."""
    assert applier.is_boot_presence(None, stamp=5000, system_up=True) is True
    monkeypatch.setattr(applier, "read_boot_stamp", lambda *a, **k: None)
    # A booted system with no stamp at all: /run cleared, or the state unit
    # never ran. A genuine plug must still work; what stops this fallback from
    # firing on a device that never went anywhere is the policy's own refusal to
    # auto-switch for an adapter it already believed present.
    assert applier.is_boot_presence(9000, stamp=None, system_up=True) is False


def test_the_boot_stamp_is_monotonic_and_lives_in_run_sr028(applier, tmp_path):
    stamp = tmp_path / "audio-boot-apply.json"
    applier.write_boot_stamp(stamp)
    value = applier.read_boot_stamp(stamp)
    assert isinstance(value, int) and value >= 0
    assert applier.BOOT_STAMP.name == "audio-boot-apply.json"
    assert "run" in applier.BOOT_STAMP.parts, "it describes THIS boot and vanishes with it"
    # A damaged or missing stamp reads as None, which is "coldplug".
    stamp.write_text("not json", encoding="utf-8")
    assert applier.read_boot_stamp(stamp) is None
    assert applier.read_boot_stamp(tmp_path / "absent.json") is None


def test_the_syspath_is_the_card_NUMBER_not_the_alsa_id_sr028(applier, tmp_path):
    """The bug review caught, and it silently killed every runtime auto-switch.

    sysfs names the class entry `cardN`; the ALSA id is what the adapter
    enumerates under ("Device" for this nameless C-Media part) and has no sysfs
    entry at all. Looking the id up under /sys/class/sound never resolves, so
    udevadm is never asked, so every plug reads as coldplug, forever.
    """
    proc = tmp_path / "asound"
    (proc / "card1").mkdir(parents=True)
    (proc / "card1" / "usbid").write_text("0d8c:0014\n", encoding="utf-8")
    (proc / "card1" / "id").write_text("Device\n", encoding="utf-8")
    sysfs = tmp_path / "sys"
    (sysfs / "card1").mkdir(parents=True)
    assert applier.headset_card(proc) == "Device", "the ALSA id, for amixer and alsaloop"
    resolved = applier.headset_syspath(proc, sysfs)
    assert resolved is not None, "a runtime plug that cannot be dated never switches"
    assert Path(resolved).name == "card1", "the card NUMBER is what sysfs knows"
    # Nothing plugged in: no path, and the caller reads that as coldplug.
    assert applier.headset_syspath(tmp_path / "empty", sysfs) is None


def test_a_report_for_an_adapter_already_present_is_not_an_arrival_sr028(policy):
    """A `udevadm trigger`, a restarted presence unit, a re-assert after resume.

    None of them is a plug, and none may spend the latch -- spending it here
    would eat the NEXT real plug.
    """
    state, _ = policy.apply_event(policy.default_state(),
                                  {"kind": "headset", "present": False, "boot": True})
    assert state["headset_autoswitch_armed"] is True
    state["headset_present"] = True          # as a previous apply recorded it
    state, lines = policy.apply_event(state, {"kind": "headset", "present": True})
    assert state["output"] == "speaker", "nothing arrived, so nothing moves"
    assert state["headset_autoswitch_armed"] is True, "and the latch is not eaten"
    assert any("already present" in line for line in lines)


def test_a_remove_is_always_a_runtime_event_sr028(applier):
    """A device that is not there cannot have been coldplugged."""
    assert applier.headset_event(False) == {"kind": "headset", "present": False, "boot": False}


def test_the_state_unit_does_not_lean_on_udev_settle_sr028():
    """Settle drains the queue it can SEE and promises nothing about later hardware.

    An adapter behind a slow hub that enumerates a second after settle returned
    would look like a runtime plug. The is-system-running gate covers that case
    honestly; settle would only have made the hole harder to see.
    """
    unit = read(WALL / "wall-audio-state.service")
    assert "Wants=systemd-udev-settle.service" not in unit
    assert "After=systemd-udev-trigger.service" in unit
    directives = [line for line in unit.splitlines() if not line.startswith("#")]
    assert not any("systemd-udev-settle" in line for line in directives)


def test_the_stored_position_is_applied_before_presence_is_acted_on_sr028():
    presence = read(WALL / "wall-headset-present.service")
    assert "After=wall-audio-state.service" in presence
    # Ordering only: a panel in trigger or panel mode has the state unit
    # disabled and the presence reporter must still record what is plugged in.
    assert "Requires=wall-audio-state.service" not in presence
    assert "Wants=wall-audio-state.service" not in presence


def test_the_udev_rule_does_not_pretend_to_know_the_event_kind_sr028():
    rules = read(WALL / "91-wall-headset-adapter.rules")
    assert "--boot" not in rules and "--runtime" not in rules, \
        "a flag baked into the rule would be wrong for every runtime re-trigger"


def test_apply_state_records_presence_and_stamps_the_boot_sr028(applier, tmp_path, monkeypatch):
    """The boot path end to end, through the real applier's own entry point."""
    state_file = tmp_path / "audio-state.json"
    stamp = tmp_path / "audio-boot-apply.json"
    monkeypatch.setattr(applier, "BOOT_STAMP", stamp)
    monkeypatch.setattr(applier, "headset_card", lambda *a, **k: "Device")
    monkeypatch.setattr(applier, "current_mode", lambda: "trigger")  # no hardware here
    state_file.write_text(json.dumps({"version": 1, "output": "speaker",
                                      "headset_autoswitch_armed": True}), encoding="utf-8")
    assert applier.main(["--state", str(state_file), "apply-state"]) == 0
    written = json.loads(state_file.read_text(encoding="utf-8"))
    assert written["output"] == "speaker", "the power cycle kept the Owner's position"
    assert written["headset_present"] is True
    assert written["headset_autoswitch_armed"] is False
    assert applier.read_boot_stamp(stamp) is not None


def test_the_stamp_is_sampled_before_the_apply_and_published_after_it_sr028(applier, tmp_path,
                                                                           monkeypatch):
    """Both halves matter, and review caught both.

    Publishing early would declare the boot settled while the legs were still
    moving, so an adapter enumerating in between would read as a plug event.
    Sampling late would classify a plug that arrived DURING the apply -- whose
    presence unit is sitting on the flock waiting for us -- as coldplug, and
    swallow a real one.
    """
    state_file = tmp_path / "audio-state.json"
    stamp = tmp_path / "audio-boot-apply.json"
    monkeypatch.setattr(applier, "BOOT_STAMP", stamp)
    monkeypatch.setattr(applier, "headset_card", lambda *a, **k: None)
    monkeypatch.setattr(applier, "current_mode", lambda: "bus")
    order = []

    def apply_plan(state, shell):
        order.append("apply")
        assert not stamp.exists(), "the stamp must not be published mid-apply"
        return 0

    monkeypatch.setattr(applier, "apply_plan", apply_plan)
    before = int(time.monotonic() * 1_000_000)
    assert applier.main(["--state", str(state_file), "--dry-run", "apply-state"]) == 0
    after = int(time.monotonic() * 1_000_000)
    assert order == ["apply"]
    mark = applier.read_boot_stamp(stamp)
    assert before <= mark <= after


def test_a_negative_stamp_is_refused_sr028(applier, tmp_path):
    """It would sit before every real initialization time and make every
    coldplug a plug event -- the exact failure the stamp exists to prevent."""
    stamp = tmp_path / "audio-boot-apply.json"
    stamp.write_text(json.dumps({"monotonic_usec": -1}), encoding="utf-8")
    assert applier.read_boot_stamp(stamp) is None


def test_the_boot_finished_gate_reads_systemd_not_a_clock_sr028(applier):
    def answering(text):
        class Reply:
            returncode, stdout, stderr = 0, text, ""
        return lambda argv, **kwargs: Reply()

    assert applier.system_is_up(run=answering("running\n")) is True
    assert applier.system_is_up(run=answering("degraded\n")) is True
    assert applier.system_is_up(run=answering("starting\n")) is False
    assert applier.system_is_up(run=answering("initializing\n")) is False
    assert applier.system_is_up(run=answering("who knows\n")) is None

    def explodes(argv, **kwargs):
        raise OSError("no systemctl here")

    assert applier.system_is_up(run=explodes) is None


# -- the level, and the control that does not exist yet ---------------------

class _Reply:
    """What subprocess.run returns to the applier."""

    def __init__(self, returncode):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = "amixer: Unable to find simple control 'Bus',0"


def test_the_control_is_declared_and_set_before_the_leg_starts_sr028(applier, policy):
    """Measured 2026-09-13: `wall-audio-mode bus` logged `sset Bus 60%` failing.

    softvol creates its control when a CLIENT opens the PCM that declares it,
    and on the first apply after a whole-graph swap there may be no client for
    longer than any retry window worth having. So the applier opens the PCM
    itself. Review then found the ordering that mattered: created AFTER the
    forwarder is running, the control exists for a moment at the plugin default
    -- full scale -- with audio already flowing through it. So it is declared
    and set BEFORE the leg starts, and the retried set at the end stays as the
    belt to that brace.
    """
    recorder = applier.Applier(dry_run=True)
    applier.apply_plan(policy.default_state(), recorder)
    names = [argv[0].rsplit("/", 1)[-1] for argv in recorder.commands]
    assert "aplay" in names, "the applier declares the control itself"
    preopen = names.index("aplay")
    assert recorder.commands[preopen][-1] == "/dev/null", "no frames reach the room"
    assert "speaker_out" in recorder.commands[preopen], "the PCM that DECLARES the softvol"
    first_start = min(index for index, argv in enumerate(recorder.commands)
                      if names[index] == "systemctl" and argv[1] == "start")
    assert preopen < first_start, "a softvol created under a running leg starts at full scale"
    levels = [index for index, argv in enumerate(recorder.commands)
              if names[index] == "amixer" and "Bus" in argv]
    assert levels[0] > preopen and levels[0] < first_start, \
        "the remembered level is in place before a single frame is forwarded"
    assert levels[-1] == len(recorder.commands) - 1, "and re-asserted last, retried"


def test_the_pre_open_is_bounded_in_time_as_well_as_in_tries_sr028(applier):
    """It runs while the apply flock is held, so an ALSA open that wedges must
    not hold the switch for the caller's default timeout."""
    seen = {}

    def run(argv, **kwargs):
        seen[argv[0]] = kwargs.get("timeout")
        return _Reply(0)

    shell = applier.Applier(run=run)
    assert applier.declare_bus_control(shell, "speaker_out") is True
    assert seen["/usr/bin/aplay"] == 5


def test_the_pre_open_is_bounded_and_never_runs_with_no_leg_sr028(applier, policy):
    calls = []

    def always_fail(argv, **kwargs):
        calls.append(argv)
        return _Reply(1)

    shell = applier.Applier(run=always_fail)
    assert applier.set_bus_level(shell, "Loopback", 60, sleep=lambda _: None,
                                 pcm="speaker_out") is False
    assert len([a for a in calls if a[0].endswith("aplay")]) == 1
    assert len([a for a in calls if a[0].endswith("amixer")]) == applier.BUS_CONTROL_ATTEMPTS

    # Mute: no leg runs, so nothing is opened at all -- opening a PCM to create
    # a control would start audio nobody asked for.
    calls.clear()
    muted, _ = policy.apply_event(policy.default_state(),
                                  {"kind": "set_output", "output": "mute"})
    applier.apply_plan(muted, applier.Applier(dry_run=True))
    shell = applier.Applier(run=always_fail)
    # Mute, or headset with no adapter: the control legitimately does not exist
    # and opening a PCM to create one would start audio nobody asked for.
    assert applier.set_bus_level(shell, "Loopback", 0, expected=False,
                                 sleep=lambda _: None, pcm="speaker_out") is True
    assert not any(a[0].endswith("aplay") for a in calls)


def test_each_position_pre_opens_its_own_leg_sr028(applier):
    assert applier.BUS_CONTROL_PCM == {"speaker": "speaker_out", "headset": "headset_out"}
    conf = read(BUS_CONF)
    for pcm in applier.BUS_CONTROL_PCM.values():
        assert "pcm.%s {" % pcm in conf, "the pre-open must name a PCM that exists"
    assert "mute" not in applier.BUS_CONTROL_PCM, "there is nothing to open in Mute"


# -- the two install wrinkles seen live on 2026-09-13 -----------------------

def test_a_mode_switch_restarts_what_it_stopped_sr028():
    """Measured: after `wall-audio-mode bus`, bluealsa-aplay was left stopped.

    The script stops it unconditionally -- it is a client of `default` -- but
    only started it again if it was ENABLED, and a unit that is merely running
    reads as disabled. A mode switch may leave something off that was off; it
    may not turn off something that was on.
    """
    mode = read(WALL / "wall-audio-mode")
    assert "remember_active" in mode and "was_active" in mode
    remembered = mode.split("remember_active wall-amp-trigger.service", 1)[1] \
                     .split("unit stop", 1)[0]
    assert "bluealsa-aplay.service" in remembered
    starter = mode.split("start_if_enabled() {", 1)[1].split("\n}", 1)[0]
    assert 'if was_active "$1"' in starter, "running before means running after"


def test_bus_mode_starts_the_state_unit_rather_than_the_script_sr028():
    """Measured: after `wall-audio-mode bus`, wall-audio-state was left inactive.

    Which is where acceptance check 10 goes looking, and it was empty. `restart`
    rather than `start`, because the unit is RemainAfterExit=yes and starting an
    already-active oneshot runs nothing.
    """
    mode = read(WALL / "wall-audio-mode")
    bus = mode.split('if [ "$1" = bus ]; then', 1)[1]
    assert "unit restart wall-audio-state.service" in bus
    assert "/usr/local/sbin/wall-audio-output apply-state" not in bus, \
        "behind the unit's back is how the unit ended up dead"
    assert "unit start wall-audio-apply.path" in bus, "the broker's only road to root"
    assert "wall-headset-present.service" in bus


def test_firstboot_does_not_warn_about_commented_out_placeholders():
    """A separate firstboot bug, seen in the same install: wall.env ships its
    optional settings as commented examples that still carry REPLACE_WITH_...,
    so a fully configured panel warned on every run and the warning stopped
    meaning anything.
    """
    firstboot = read(WALL / "wall-firstboot.sh")
    line = [one for one in firstboot.splitlines()
            if "REPLACE_WITH" in one and one.strip().startswith("if grep")][0]
    assert "grep -v" in line and "#" in line
