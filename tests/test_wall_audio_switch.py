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
import os
from pathlib import Path
import shutil
import subprocess
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


@pytest.fixture(autouse=True)
def _never_touch_the_real_run_dir(applier, tmp_path, monkeypatch):
    """Every /run path the applier publishes goes to a temp tree, in EVERY test.

    Not a convenience. Tests that drive `apply_plan` for real (rather than with
    --dry-run) write these files, and on a dev box `/run/wall-panel` resolves to
    a real directory that SURVIVES the test run -- so the next run read back a
    published speaker chain and a whole unrelated test started failing. It
    happened twice while step 4 was being written, which is why the guard is
    autouse and module-wide rather than a line in each test that remembers.
    """
    # BOOT_STAMP and LOCK_FILE are deliberately NOT in this list: their own
    # tests assert their real names and paths, and both are already given temp
    # locations by the cases that write them.
    for name in ("SPEAKER_CHAIN_ENV", "MIC_SOURCE_ENV", "HEADSET_ENV"):
        monkeypatch.setattr(applier, name, tmp_path / ("run-" + name.lower()))
    for name in ("SPEAKER_CHAIN_VAR", "MIC_SOURCE_VAR"):
        monkeypatch.delenv(getattr(applier, name), raising=False)


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
    # STEP 4 CHANGED THIS AND THE CHANGE IS DELIBERATE. The adapter's mute is
    # ONE boolean over all eight channels (numid 7, measured 2026-09-14), and in
    # Headset position item 23 D3 still wants the headset's microphone on the
    # adapter's REAR pair, which is the desktop's input. Muting the card would
    # silence that too, and the card cannot say "front off, rear on". So it is
    # muted only when nothing at all is meant to leave it, and the separation is
    # the two generated route tables' explicit zeros (asserted further down).
    assert headset["adapter_muted"] is False
    assert headset["mic_live"] is True
    # ... and it IS muted the moment no mic leg wants it either.
    silent, _ = policy.apply_event(state, {"kind": "set_input_mute", "muted": True})
    assert policy.plan(silent)["adapter_muted"] is True


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
    assert blocks, "no direct plugin was inspected at all"


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
    # The LAST one: `trim` also takes the lock now (review, step 3), and it is
    # written first in main(). The apply's own is the one this test is about.
    locked = source.split("with apply_lock(")[-1]
    assert "_locked_apply(arguments)" in locked
    # And trim's, which review added after finding that a trim could restart a
    # leg an apply had just stopped for Mute.
    assert "with apply_lock(" in source.split('command == "trim"', 1)[1][:200]


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
    assert 'VOLUME_SOCKET = "/run/wall-volume-request.sock"' in keys
    # Both carry a percent since 2026-09-15: one apply costs ~520 ms, so a ramp
    # built from default-sized presses queues instead of keeping up.
    assert "def nudge_bus(louder, percent):" in keys
    assert 'if current_mode() == "bus":\n        nudge_bus(louder, percent)\n        return' in keys
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
    # The clock is read in nanoseconds and the sequence is MICROSECONDS: a
    # nanosecond epoch is past JavaScript's safe-integer range, and the shell
    # compares this number against the applier's mark with Number.isSafeInteger,
    # so it would have silently skipped the comparison it depends on.
    assert request.next_seq(clock=lambda: 7_000) == 7
    assert request.next_seq(clock=lambda: 8_000) > request.next_seq(clock=lambda: 7_000)
    assert request.next_seq() < 2 ** 53 - 1
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


def test_a_boot_that_has_not_finished_can_never_auto_switch_sr028(applier, monkeypatch):
    """Gate 1, and the reason systemd-udev-settle was dropped.

    Review was right that settle only drains the queue it can SEE: an adapter
    behind a slow hub enumerating a second after settle returned would have had
    USEC_INITIALIZED later than the stamp and fired the one-shot against the
    ruling. While systemd still says `starting`, coldplug is still plausible and
    the Owner's position wins -- and a slow boot stays `starting` for longer all
    by itself, so there is no number here to get wrong.
    """
    assert applier.is_boot_presence(999_999_999, stamp=1, system_up=False) is True
    # `system_up=None` is the "not supplied" sentinel, NOT "unanswerable": the
    # function then goes and asks system_is_up() itself. Passing None here
    # therefore asserted nothing on any machine that HAS systemd -- including
    # the panel, which is the only machine that matters -- and passed on the dev
    # box only because `systemctl` is missing there. Found running this suite
    # under `wsl -d Ubuntu` for the step-4 ALSA proof, 2026-09-14. The claim is
    # about an unanswerable gate 1, so the unanswerable thing is what is stubbed.
    monkeypatch.setattr(applier, "system_is_up", lambda *a, **k: None)
    assert applier.is_boot_presence(999_999_999, stamp=1) is True, \
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

    def __init__(self, returncode, stdout=None, stderr=None):
        self.returncode = returncode
        self.stdout = "" if stdout is None else stdout
        self.stderr = ("amixer: Unable to find simple control 'Bus',0"
                       if stderr is None else stderr)


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
    # The aplay that DECLARES the control, not the one that probes the speaker
    # chain: step 3 added a second no-frames open ahead of this one, and
    # index("aplay") would now find the probe.
    preopen = min(index for index, argv in enumerate(recorder.commands)
                  if names[index] == "aplay" and "speaker_out" in argv)
    assert recorder.commands[preopen][-1] == "/dev/zero" and recorder.commands[preopen][-3:-1] == ["-s", "1"], "one raw zero frame, nothing audible"
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


# ── step 3: the centre and sub leg (D4, review finding 8) ──────────────────
# Item 23 step 3. The speaker leg opens the adapter's 8-channel altsetting,
# front stays stereo and an (L+R)/2 mono sum feeds BOTH the centre channel and
# the LFE channel, through a trim table the Owner's tuning session can move
# without touching ALSA syntax.

TRIM_EXAMPLE = WALL / "audio-trim.conf.example"


def test_the_speaker_leg_opens_eight_channels_d4_sr028():
    """Measured on the panel: altset 1 is 8 ch S16_LE, map FL FR FC LFE RL RR SL SR.

    `channels 8` on the slave is the whole of how that altsetting is selected --
    the map is fixed by the device and is already the order the ttable is
    written in.
    """
    conf = read(BUS_CONF)
    # STEP 4 MOVED THE OPEN ITSELF ONE LEVEL DOWN, into a dmix, because the
    # adapter has ONE playback stream and the mic return needs the rear pair of
    # it at the same time as the speaker leg has the front. speaker_hw8 is now
    # the plug that reaches that dmix; `channels 8` -- which is the whole of how
    # altset 1 is selected -- lives in the dmix's slave.
    hw8 = conf.split("pcm.speaker_hw8 {", 1)[1].split("\n}", 1)[0]
    assert "usb_out_mix" in hw8
    shared = conf.split("pcm.usb_out_mix {", 1)[1].split("\n}", 1)[0]
    assert "type dmix" in shared
    assert "channels 8" in shared
    assert '"card_usb"' in shared
    assert 'pcm "card_usb"' in shared, "the card id still comes from the generated map"
    assert "format S16_LE" in shared and "rate 48000" in shared


def test_the_mono_sum_reaches_both_centre_and_sub_d4_sr028(applier):
    """D4: the SAME (L+R)/2 signal to FC and to LFE, and front left stereo.

    Asserted on the numbers the renderer produces, not on the example file, so
    that a change to the defaults has to be made on purpose.
    """
    rendered = applier.render_trim_conf(applier.TRIM_DEFAULTS)
    # 0 FL, 1 FR, 2 FC, 3 LFE -- the hardware's fixed order.
    assert "ttable.0.2 0.5000" in rendered and "ttable.1.2 0.5000" in rendered
    assert "ttable.0.3 0.5000" in rendered and "ttable.1.3 0.5000" in rendered
    assert "ttable.0.0 1.0000" in rendered and "ttable.1.1 1.0000" in rendered
    # Front stays STEREO: neither input crosses into the other front channel.
    assert "ttable.1.0 0.0000" in rendered and "ttable.0.1 0.0000" in rendered
    # FC and LFE are fed identically, which is what "the same mono signal on
    # both" means and is what the Owner's tone test will show.
    for source in (0, 1):
        centre = rendered.split("ttable.%d.2 " % source, 1)[1].split()[0]
        sub = rendered.split("ttable.%d.3 " % source, 1)[1].split()[0]
        assert centre == sub


def test_the_rear_and_side_pairs_are_explicit_zeros_sr028(applier):
    """Rear is wired to the desktop's INPUT: the room's music must never reach it.

    Written out rather than left implicit, so "silent on purpose" and
    "forgotten" do not look the same in a generated file.
    """
    rendered = applier.render_trim_conf(applier.TRIM_DEFAULTS)
    for channel in (4, 5, 6, 7):
        assert "ttable.0.%d 0.0000" % channel in rendered
        assert "ttable.1.%d 0.0000" % channel in rendered


def test_the_trim_defaults_are_the_owners_day_one_table_sr028(applier):
    """Front 1.0/1.0, centre 0.5/0.5 per input, sub 0.5/0.5 -- finding 8."""
    assert applier.TRIM_DEFAULTS == {"front_l": 1.0, "front_r": 1.0,
                                     "center_l": 0.5, "center_r": 0.5,
                                     "sub_l": 0.5, "sub_r": 0.5}
    env = read(WALL / "wall.env.example")
    for key, value in applier.TRIM_DEFAULTS.items():
        assert "WALL_AUDIO_TRIM_%s=%s" % (key.upper(), value) in env
    assert "WALL_AUDIO_TRIM_FRONT_L" in read(WALL / "wall-firstboot.sh")


def test_a_tuning_session_moves_the_table_without_alsa_syntax_finding_8_sr028(applier):
    """`wall-audio-output trim center=0.35 sub=0.6` is the Owner ruling's mechanism.

    A group name moves both halves of the mono sum at once and keeps it a sum;
    a per-channel name is there for the room that turns out to be asymmetric.
    """
    values = dict(applier.TRIM_DEFAULTS)
    moved = applier.parse_trim_assignments(["center=0.35", "sub=0.6"], values)
    assert moved["center_l"] == moved["center_r"] == 0.35
    assert moved["sub_l"] == moved["sub_r"] == 0.6
    assert moved["front_l"] == 1.0, "the others are untouched"
    assert values == applier.TRIM_DEFAULTS, "the input table is never mutated"
    finer = applier.parse_trim_assignments(["front_l=0.95"], values)
    assert finer["front_l"] == 0.95 and finer["front_r"] == 1.0


@pytest.mark.parametrize("assignment", [
    "center=-0.5",   # a phase inversion is not a trim
    "center=99",     # above the ceiling is a wiring problem
    "center=loud",   # not a number
    "rear=0.5",      # there is no knob for a pair wired to the desktop's input
    "center",        # not KEY=VALUE
])
def test_a_refused_trim_moves_nothing_at_all_sr028(applier, assignment):
    """Whole table or none of it: a session that fat-fingers the fourth change
    must not be left with the first three applied and no idea which."""
    with pytest.raises(ValueError):
        applier.parse_trim_assignments(["center=0.4", assignment],
                                       dict(applier.TRIM_DEFAULTS))


def test_the_trim_file_falls_back_field_by_field_sr028(applier, tmp_path):
    """One bad number must not cost the other five, exactly as the state file."""
    env = tmp_path / "audio-trim.env"
    env.write_text("WALL_AUDIO_TRIM_CENTER_L=0.25\n"
                   "WALL_AUDIO_TRIM_SUB_L=banana\n"
                   "WALL_AUDIO_TRIM_REAR_L=1.0\n", encoding="utf-8")
    values, notes = applier.load_trim(env)
    assert values["center_l"] == 0.25, "the good one is kept"
    assert values["sub_l"] == applier.TRIM_DEFAULTS["sub_l"], "the bad one defaults"
    assert values["front_l"] == 1.0, "an absent one defaults"
    assert any("sub_l" in note for note in notes), "and it is journaled, not silent"
    assert any("rear_l" in note for note in notes)


def test_the_trim_round_trips_through_its_own_env_file_sr028(applier, tmp_path):
    """What `trim` writes is what the next run reads: one number, not two."""
    env = tmp_path / "audio-trim.env"
    wanted = applier.parse_trim_assignments(["sub=0.6", "center_r=0.4"],
                                            dict(applier.TRIM_DEFAULTS))
    applier.write_atomic(env, applier.render_trim_env(wanted))
    back, notes = applier.load_trim(env)
    assert notes == []
    assert back == wanted


def test_a_missing_trim_file_may_not_take_the_whole_graph_down_sr028():
    """`errors false`, and it is load-bearing.

    A plain `</...>` include of an absent file aborts the WHOLE configuration --
    the bus, the headset leg and `default` with it, i.e. silence everywhere
    because a generated file went missing. The hook leaves only `speaker_multi`
    undefined, and the applier's probe then falls back to stereo.
    """
    conf = read(BUS_CONF)
    assert "</etc/wall-panel/audio-trim.conf>" not in conf, "a hard include would"
    hook = conf.split("@hooks", 1)[1].split("\n]", 1)[0]
    assert "func load" in hook
    assert '"/etc/wall-panel/audio-trim.conf"' in hook
    assert "errors false" in hook


def test_one_probe_answers_both_ways_the_multi_chain_can_be_missing_sr028(applier):
    """An adapter that refuses 8 channels and a missing trim file are one question.

    Asking ALSA to open the chain answers both, about the graph as it is now
    rather than about a file's existence. The fallback is exactly what shipped
    before step 3.
    """

    class Refusing:
        dry_run = False

        def __init__(self):
            self.commands = []

        def command(self, argv, required=True, quiet=False, timeout=15):
            self.commands.append(list(argv))
            return 1

    refusing = Refusing()
    assert applier.probe_speaker_chain(refusing) == applier.SPEAKER_CHAIN_STEREO
    assert refusing.commands == [["/usr/bin/aplay", "-q", "-D", "speaker_multi",
                                  "-t", "raw", "-f", "S16_LE", "-r", "48000",
                                  "-c", "2", "-s", "1", "/dev/zero"]], "opened with one raw zero frame, nothing audible"
    accepting = applier.Applier(dry_run=True)
    assert applier.probe_speaker_chain(accepting) == applier.SPEAKER_CHAIN_MULTI


def test_the_chain_reaches_both_the_leg_and_our_own_pre_open_sr028(applier, monkeypatch,
                                                                  tmp_path):
    """`@func getenv` reads the environment of the process that OPENS the PCM.

    The unit gets the answer through EnvironmentFile=; the applier's own
    pre-open is its own child and gets it through the environment. Without the
    second half the pre-open would declare the softvol on a chain the leg was
    not going to use.
    """
    monkeypatch.setattr(applier, "SPEAKER_CHAIN_ENV", tmp_path / "audio-speaker.env")
    monkeypatch.delenv(applier.SPEAKER_CHAIN_VAR, raising=False)

    def refuse(argv, **kwargs):
        raise OSError("no aplay here")

    recorder = applier.Applier(dry_run=False, run=refuse)
    chain = applier.select_speaker_chain(recorder)
    assert chain == applier.SPEAKER_CHAIN_STEREO
    written = (tmp_path / "audio-speaker.env").read_text(encoding="utf-8")
    assert written.strip() == "%s=%s" % (applier.SPEAKER_CHAIN_VAR, chain)
    assert os.environ[applier.SPEAKER_CHAIN_VAR] == chain
    unit = read(WALL / "wall-speaker-out.service")
    assert "EnvironmentFile=-/run/wall-panel/audio-speaker.env" in unit
    conf = read(BUS_CONF)
    speaker_out = conf.split("pcm.speaker_out {", 1)[1].split("\n}", 1)[0]
    assert "@func getenv" in speaker_out
    assert applier.SPEAKER_CHAIN_VAR in speaker_out
    assert '"speaker_multi"' in speaker_out, "the 8-channel chain is the default"


def test_the_chain_is_chosen_before_anything_opens_the_leg_sr028(applier, policy):
    """Probe, publish, then declare the control -- in that order or not at all."""
    recorder = applier.Applier(dry_run=True)
    applier.apply_plan(policy.default_state(), recorder)
    flat = [" ".join(one) for one in recorder.commands]
    probe = [i for i, one in enumerate(flat) if "-D speaker_multi" in one]
    declare = [i for i, one in enumerate(flat) if "-D speaker_out" in one]
    start = [i for i, one in enumerate(flat) if "systemctl start wall-speaker-out" in one]
    assert probe and declare and start
    assert probe[0] < declare[0] < start[0]


def test_no_chain_is_probed_when_the_speaker_leg_is_not_wanted_sr028(applier, policy):
    """In Mute, and in Headset, opening the adapter is audio nobody asked for."""
    for output in ("mute", "headset"):
        state = policy.default_state()
        state["output"] = output
        recorder = applier.Applier(dry_run=True)
        applier.apply_plan(state, recorder)
        assert not [one for one in recorder.commands
                    if "speaker_multi" in " ".join(one)], output


def test_the_boot_minute_fix_is_not_an_ordering_deadlock_sr028():
    """58 underruns on 2026-09-14, 00:14:18-00:15:13, then none.

    `After=wall-firstboot.service` was the first answer and review caught it as a
    FIRST-BOOT DEADLOCK: nothing here is started by a boot target, so firstboot
    itself starts these units -- through wall-audio-mode and wall-audio-state --
    and waits for the job. Ordering that job after firstboot parks it behind the
    job that is waiting for it. The chain is asserted here so that nobody
    re-adds the line without meeting it.
    """
    firstboot = read(WALL / "wall-firstboot.sh")
    mode = read(WALL / "wall-audio-mode")
    # The literal invocation lives in firstboot's apply_audio_mode helper,
    # which judges the result; the bus arm calls it with the mode name.
    assert '/usr/local/sbin/wall-audio-mode "$mode"' in firstboot,         "firstboot is what applies a mode"
    assert "apply_audio_mode bus" in firstboot,         "firstboot is what applies bus mode"
    assert "unit restart wall-audio-state.service" in mode,         "and that is what runs the applier that starts these legs"
    for name in ("wall-speaker-out.service", "wall-bus-speaker.service"):
        # Directives only: the units EXPLAIN the withdrawn ordering at length,
        # and the explanation must not read as the thing it warns about.
        directives = [one.strip() for one in read(WALL / name).splitlines()
                      if one.strip() and not one.lstrip().startswith("#")]
        assert "After=wall-firstboot.service" not in directives, name
        assert "After=wall-kiosk-loop.service" not in directives, name


def test_the_boot_minute_is_answered_by_a_bigger_ring_on_one_half_sr028():
    """The buffer, on the half that actually underran, and only that half.

    A larger ring tolerates a longer scheduling gap, which is what the boot
    minute is. It costs about 20 ms end to end, still under the kiosk leg's own
    100 ms; the tap keeps 30 ms because buying the tolerance twice would pay for
    the boot minute twice.
    """
    assert "--tlatency 50000" in read(WALL / "wall-speaker-out.service")
    assert "--tlatency 30000" in read(WALL / "wall-bus-speaker.service")
    for name in ("wall-spdif-in.service", "wall-bus-headset.service"):
        assert "--tlatency 30000" in read(WALL / name),             "%s is not on the speaker path and was not part of this" % name


def test_the_trim_takes_the_same_lock_every_other_writer_takes_sr028():
    """Review: trim saw the leg running, an apply for Mute stopped it, and
    trim's restart brought it back -- the graph in a position nobody chose."""
    source = read(APPLIER)
    entry = source.split('if arguments.command == "trim":', 1)[1][:300]
    assert "with apply_lock(" in entry
    assert "return trim_command(arguments)" in entry


def test_the_trim_restarts_nothing_the_owner_did_not_select_sr028(applier, tmp_path,
                                                                 monkeypatch):
    """`is-active` is a fact about a moment; the stored position is the choice.

    Only when both agree is a restart something the Owner would recognize --
    review's interleaving was trim seeing the leg active, an apply for Mute
    stopping it, and trim starting it again.
    """
    (tmp_path / "audio-mode").write_text("bus" + chr(10), encoding="utf-8")
    monkeypatch.setattr(applier, "MODE_FILE", tmp_path / "audio-mode")
    state_file = tmp_path / "audio-state.json"
    recorded = []

    real = applier.Applier

    class Recorder(real):
        def __init__(self, dry_run=False, run=None):
            real.__init__(self, dry_run=True)
            recorded.append(self)

    monkeypatch.setattr(applier, "Applier", Recorder)

    class Arguments:
        dry_run = True
        render = True
        value = []
        trim_env = tmp_path / "audio-trim.env"
        trim_conf = tmp_path / "audio-trim.conf"
        state = str(state_file)

    for output, expect_restart in (("speaker", True), ("headset", False),
                                   ("mute", False)):
        recorded[:] = []
        state_file.write_text(json.dumps({"version": 1, "output": output}),
                              encoding="utf-8")
        assert applier.trim_command(Arguments()) == 0
        issued = [" ".join(one) for one in recorded[0].commands]
        restarted = any("systemctl restart" in one for one in issued)
        assert restarted is expect_restart, "%s: %s" % (output, issued)


def test_the_trim_is_installed_only_if_absent_so_tuning_survives_a_rerun_sr028():
    """The Owner's tuning session is numbers arrived at by listening. A firstboot
    re-run must not throw them away -- the same rule amp-trigger.env has."""
    firstboot = read(WALL / "wall-firstboot.sh")
    assert "if [ ! -f /etc/wall-panel/audio-trim.env ]; then" in firstboot
    assert "wall-audio-output trim --render" in firstboot
    before = firstboot.split("wall-audio-output trim --render", 1)[0]
    assert "/usr/local/lib/wall-panel/wall-audio-output" in before, \
        "the renderer has to be installed before firstboot calls it"


def test_the_trim_never_starts_audio_that_was_not_running_sr028(applier):
    """A command that only moves a number must never put audio in the room.

    So the running check counts a missing or erroring systemctl as NOT running:
    with required=False an exception would answer 0, i.e. "active", and the leg
    would be started by a tuning command.
    """

    def refuse(argv, **kwargs):
        raise OSError("no systemctl here")

    recorder = applier.Applier(dry_run=False, run=refuse)
    assert recorder.command(["/usr/bin/systemctl", "is-active", "--quiet",
                             "wall-speaker-out.service"], quiet=True) == 1


def test_the_generated_alsa_config_parses_sr028(tmp_path):
    """The one test that asks ALSA itself, rather than reading text.

    Skipped where alsa-lib is not installed (the dev box); run under
    `wsl -d Ubuntu`, which is where it was proven. The include paths are
    rewritten into a temp tree because the real ones are absolute and under
    /etc -- so what is asserted is the SYNTAX and the name resolution, not the
    panel's own file locations, which the firstboot tests cover.
    """
    aplay = shutil.which("aplay")
    if not aplay:
        pytest.skip("alsa-lib is not installed here; run under wsl -d Ubuntu")
    alsa_conf = Path("/usr/share/alsa/alsa.conf")
    if not alsa_conf.exists():
        pytest.skip("alsa-lib's own configuration is not present")

    trim = tmp_path / "audio-trim.conf"
    mic = tmp_path / "audio-mic.conf"
    cards = tmp_path / "audio-cards.conf"
    out = tmp_path / "audio-out.conf"
    root = tmp_path / "asound.conf"
    trim.write_text(read(TRIM_EXAMPLE), encoding="utf-8")
    mic.write_text(read(MIC_EXAMPLE), encoding="utf-8")
    cards.write_text(read(WALL / "audio-cards.conf.example"), encoding="utf-8")
    out.write_text(read(BUS_CONF)
                   .replace("/etc/wall-panel/audio-trim.conf", str(trim))
                   .replace("/etc/wall-panel/audio-mic.conf", str(mic)),
                   encoding="utf-8")
    root.write_text(read(WALL / "asound.conf")
                    .replace("/etc/wall-panel/audio-cards.conf", str(cards))
                    .replace("/etc/wall-panel/audio-out.conf", str(out)),
                    encoding="utf-8")

    def names(config):
        done = subprocess.run([aplay, "-L"], capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin",
                                   "ALSA_CONFIG_PATH": "%s:%s" % (alsa_conf, config)})
        return {line for line in done.stdout.splitlines() if not line.startswith(" ")}

    resolved = names(root)
    for name in ("bus", "bus_monitor", "speaker_tap", "speaker_out", "speaker_multi",
                 "speaker_stereo", "speaker_hw8", "spdif_in",
                 # step 4: the shared eight-channel dmix, both capture sources,
                 # the one name the AEC step will replace, and the rear route.
                 "usb_out_mix", "mic_panel", "mic_headset", "mic_selected",
                 "mic_rear", "mic_rear_route"):
        assert name in resolved, "%s did not resolve: %s" % (name, sorted(resolved))

    # And the containment the two hooks buy, one generated file at a time.
    # Without the trim file the graph is still there, only the multi chain is
    # gone -- and the MIC leg survives, because the two files are independent.
    trim.unlink()
    survived = names(root)
    assert "speaker_multi" not in survived
    for name in ("bus", "speaker_out", "speaker_stereo", "mic_rear_route",
                 "mic_selected"):
        assert name in survived, "a missing trim file took %s down with it" % name

    # And the other way round: a missing mic file costs the rear route and
    # NOTHING else. This is the one that matters at the glass -- a panel that
    # went silent because a microphone's generated file was not there would be
    # the worst possible trade.
    trim.write_text(read(TRIM_EXAMPLE), encoding="utf-8")
    mic.unlink()
    survived = names(root)
    assert "mic_rear_route" not in survived
    for name in ("bus", "speaker_out", "speaker_multi", "mic_selected",
                 "mic_panel", "usb_out_mix", "default"):
        assert name in survived, "a missing mic file took %s down with it" % name
    # `headset_out` and `mic_headset_raw` never appear in `aplay -L` at all,
    # here or above: alsa-lib's name hints skip a definition whose card comes
    # from `@func getenv`. That is a property of the LISTING, not of the graph --
    # they open fine on the panel -- and it is written down so that a future
    # reader does not add them to these lists and then "fix" the config.


# ── step 4: the mic legs (item 23 C, D3, D4; Owner ruling E) ───────────────
#
# Read in this order: which microphone the switch selects, what the input mute
# actually does, the two generated route tables that are the whole of the
# separation between the microphone and the speakers, and the ways the leg is
# allowed to fail.

MIC_EXAMPLE = WALL / "audio-mic.conf.example"
MIC_REAR_UNIT_FILE = WALL / "wall-mic-rear.service"
BT_MIC_UNIT_FILE = WALL / "wall-bt-mic.service"
BT_MIC = WALL / "wall-bt-mic.py"


@pytest.fixture(scope="module")
def btmic():
    return load(BT_MIC, "wall_bt_mic")


def test_the_mic_follows_the_output_switch_d3_d4_sr029(policy):
    """D4 Speaker -> the panel's own mic; D3 Headset -> the adapter's.

    There is no fourth button: the selection is a consequence of the output
    position, which is what item 23 asks for and all it asks for.
    """
    speaker = policy.default_state()
    assert policy.mic_source(speaker) == policy.MIC_SOURCE_PANEL
    assert policy.plan(speaker)["mic_source"] == "mic_panel"

    headset, _ = policy.apply_event(speaker, {"kind": "headset", "present": True})
    assert headset["output"] == "headset"
    assert policy.mic_source(headset) == policy.MIC_SOURCE_HEADSET
    assert policy.plan(headset)["mic_source"] == "mic_headset"


def test_output_mute_also_mutes_the_microphone_item_j_sr029(policy):
    """ITEM J, which SUPERSEDES Owner ruling E (coordinator plan, 2026-09-14).

    Ruling E said the Mute position silences the room and not the microphone --
    "the mic has its own mute". The Owner reversed that on 2026-09-14: selecting
    output Mute now also stops microphone transmission. The mic button still
    exists and is still a separate control; what changed is the coupling.

    This is the test that used to assert the opposite. It is rewritten rather
    than deleted so the supersession is visible in one place.
    """
    muted, lines = policy.apply_event(policy.default_state(),
                                      {"kind": "set_output", "output": "mute"})
    assert policy.audible(muted) is False, "the room is silent"
    assert policy.mic_live(muted) is False, "and so is the microphone"
    # The coupling is a LATCH on the stored flag, not a mask over it: see
    # _set_output for why the Owner's second half (the mute is retained after
    # leaving the Mute position) is only consistent with a latch.
    assert muted["input_muted"] is True
    assert any("item J" in line for line in lines), "and it is journalled"
    plan = policy.plan(muted)
    assert all(plan["legs"][unit] is False for unit in policy.ALL_LEGS)
    assert plan["input_muted_effective"] is True
    assert plan["input_mute_held"] is True
    # And in Mute there is no headset to take the mic from, so the name
    # published for the legs that are NOT running is the panel's.
    assert plan["mic_source"] == "mic_panel"


def test_leaving_mute_retains_the_input_mute_until_an_explicit_unmute_item_j(policy):
    """The Owner's second half, and the cost it carries.

    "No inferred requirement to unmute the microphone when leaving output Mute:
    default to retaining its muted state until an explicit microphone-unmute
    action." So a person who was live on Speaker, taps Mute, then taps Speaker
    again comes back MUTED and must press the mic button.
    """
    live = policy.default_state()
    assert live["input_muted"] is False and policy.mic_live(live) is True
    muted, _ = policy.apply_event(live, {"kind": "set_output", "output": "mute"})
    back, lines = policy.apply_event(muted, {"kind": "set_output", "output": "speaker"})
    assert back["input_muted"] is True, "not silently cleared"
    assert policy.mic_live(back) is False
    assert any("explicit unmute" in line for line in lines)
    assert policy.plan(back)["input_mute_held"] is False, "but no longer HELD"
    # The explicit unmute is what brings it back, and only that.
    unmuted, _ = policy.apply_event(back, {"kind": "set_input_mute", "muted": False})
    assert policy.mic_live(unmuted) is True


def test_an_independent_unmute_is_refused_while_mute_is_selected_item_j(policy):
    """Refused out loud, never silently dropped.

    A request that changed nothing while answering "accepted" would leave the
    chrome painting a live microphone over a dead one -- which is worse than
    the refusal, because it is a claim.
    """
    muted, _ = policy.apply_event(policy.default_state(),
                                  {"kind": "set_output", "output": "mute"})
    with pytest.raises(policy.StateError):
        policy.apply_event(muted, {"kind": "set_input_mute", "muted": False})
    # Muting again is not an unmute and is not refused.
    same, _ = policy.apply_event(muted, {"kind": "set_input_mute", "muted": True})
    assert same["input_muted"] is True


def test_boot_recovery_couples_the_mute_with_no_request_at_all_item_j(policy):
    """The paths that are not requests: boot, resume, udev, a rolled-back file.

    Every caller normalizes before it decides anything, so the coupling holds
    for all of them. Without this a panel could come up in Mute with a live
    microphone and nothing would ever ask a question that noticed.
    """
    recovered = policy.normalize({"output": "mute", "input_muted": False,
                                  "volume": {"headset": 60, "speaker": 60},
                                  "headset_present": False,
                                  "headset_autoswitch_armed": True,
                                  "request_seq": 5, "version": 1})
    assert recovered["input_muted"] is True
    assert policy.mic_live(recovered) is False


def test_the_input_mute_is_a_real_mute_not_a_ui_flag_ruling_e_sr029(policy):
    """It STOPS both mic legs, so no sample leaves the panel at all.

    This is the test that would fail if the input mute were ever reduced to
    something the chrome draws over a running microphone. A cosmetic mute is
    indistinguishable from a working one to whoever is on the call.
    """
    state, _ = policy.apply_event(policy.default_state(),
                                  {"kind": "set_input_mute", "muted": True})
    plan = policy.plan(state)
    assert plan["input_muted"] is True
    assert plan["mic_live"] is False
    for unit in policy.MIC_LEGS:
        assert plan["legs"][unit] is False, "%s must be STOPPED, not flagged" % unit
    # The output is untouched: the two buttons are independent (ruling E).
    assert plan["legs"]["wall-speaker-out.service"] is True


def test_headset_absent_tunnels_no_microphone_ruling_7_sr029(policy):
    """Ruling 7: "silence on every output, NO MIC TUNNELLED".

    Falling back to the panel microphone here would put a live microphone in a
    room whose owner believes the panel is switched away from it, and nobody
    could hear that happening. It is the one failure in this design with no
    audible symptom, which is exactly why it has a test of its own.
    """
    state, _ = policy.apply_event(policy.default_state(), {"kind": "headset", "present": True})
    state, _ = policy.apply_event(state, {"kind": "headset", "present": False})
    assert state["output"] == "headset"
    assert policy.mic_live(state) is False
    plan = policy.plan(state)
    assert not any(plan["legs"].values()), "no leg at all, output or mic"


def test_no_mic_leg_is_ever_enabled_sr029():
    """A boot must not start a microphone the Owner did not leave running."""
    for unit in (MIC_REAR_UNIT_FILE, BT_MIC_UNIT_FILE):
        text = read(unit)
        assert "[Install]" not in text, "%s must not be enableable" % unit.name
        assert "WantedBy" not in text
        # The mode gate every leg carries: `wall-audio-mode trigger` is the
        # Owner's one-command rollback and must take the mic legs with it.
        assert "audio-mode" in text and "= bus" in text


def test_the_mic_route_writes_explicit_zeros_everywhere_else_sr029(applier):
    """The mic on channels 4 and 5, and NOTHING anywhere else. Both halves.

    This table is the whole of what keeps the microphone out of the speakers:
    the adapter's mute is one boolean over all eight channels, so hardware
    cannot express "front off, rear on" while the mic leg is running.
    """
    rendered = applier.render_mic_conf(dict(applier.MIC_DEFAULTS))
    assert 'pcm "usb_out_mix"' in rendered, "it must share the one playback stream"
    assert "channels 8" in rendered
    for channel in (4, 5):
        assert "ttable.0.%d 1.0000" % channel in rendered
    for channel in (0, 1, 2, 3, 6, 7):
        assert "ttable.0.%d 0.0000" % channel in rendered, (
            "channel %d must be an EXPLICIT zero: silent on purpose and "
            "forgotten must not look the same" % channel)
    # The microphone reaches the two rear channels IDENTICALLY: a desktop line
    # input is stereo and both sides must carry the same capsule.
    assert rendered.count("ttable.0.4 1.0000") == rendered.count("ttable.0.5 1.0000")


def test_the_two_route_tables_do_not_overlap_sr029(applier):
    """The other direction, and the pair is the whole bargain.

    The speaker route owns 0-3 and zeroes 4-7; the mic route owns 4-5 and zeroes
    the rest. dmix sums per channel, so music cannot reach the desktop's input
    and the microphone cannot reach the amplifier, and neither statement depends
    on a mixer control.
    """
    speaker = applier.render_trim_conf(dict(applier.TRIM_DEFAULTS))
    mic = applier.render_mic_conf(dict(applier.MIC_DEFAULTS))
    for channel in applier.MIC_REAR_CHANNELS:
        assert channel in applier.TRIM_SILENT_CHANNELS, (
            "channel %d carries the mic, so the speaker route must zero it"
            % channel)
        assert "ttable.0.%d 0.0000" % channel in speaker
        assert "ttable.1.%d 0.0000" % channel in speaker
    for channel in applier.MIC_SILENT_CHANNELS:
        assert "ttable.0.%d 0.0000" % channel in mic
    assert set(applier.MIC_REAR_CHANNELS) & set(applier.MIC_SILENT_CHANNELS) == set()
    assert set(applier.MIC_REAR_CHANNELS) | set(applier.MIC_SILENT_CHANNELS) == set(range(8))


def test_the_capture_gain_default_is_the_measured_one_sr029(applier):
    """It was CLIPPING, so this number is a measurement and not a preference.

    62 % with +12 dB of boost gave peak 32768 and -15.2 dBFS RMS. 24 % (-6.00 dB)
    with boost 0 is what the panel read on 2026-09-14 after the AEC run lowered
    it live, and firstboot must seed the same value or the stored state and the
    hardware disagree from the first apply.
    """
    assert applier.MIC_DEFAULTS["capture_percent"] == 24
    assert applier.MIC_DEFAULTS["boost"] == 0
    seeded = read(WALL / "wall-firstboot.sh")
    assert "WALL_AUDIO_MIC_CAPTURE_PERCENT:-24" in seeded
    assert "WALL_AUDIO_MIC_BOOST:-0" in seeded
    example = read(WALL / "wall.env.example")
    assert "WALL_AUDIO_MIC_CAPTURE_PERCENT=24" in example
    assert "WALL_AUDIO_MIC_BOOST=0" in example


@pytest.mark.parametrize("assignment,why", [
    ("capture_percent=101", "above the codec's range"),
    ("capture_percent=-1", "below it"),
    ("capture_percent=loud", "not a number"),
    ("boost=4", "the codec has four steps, 0..3"),
    ("rear_level=198", "the adapter's scale stops at 197"),
    ("rear_gain=-0.5", "a phase inversion is not a gain"),
    ("rear_gain=8", "that is a wiring problem"),
    ("treble=1", "not a knob this panel has"),
    ("capture_percent", "not KEY=VALUE"),
])
def test_a_refused_mic_value_moves_nothing_sr029(applier, assignment, why):
    """The trim command's rule: the whole table moves or none of it does."""
    values = dict(applier.MIC_DEFAULTS)
    with pytest.raises(ValueError):
        applier.parse_mic_assignments([assignment], values)
    assert values == applier.MIC_DEFAULTS, why


def test_a_good_and_a_bad_value_together_move_nothing_sr029(applier):
    with pytest.raises(ValueError):
        applier.parse_mic_assignments(["capture_percent=30", "boost=9"],
                                      dict(applier.MIC_DEFAULTS))


def test_a_damaged_mic_file_falls_back_field_by_field_sr029(applier, tmp_path):
    """One typo must not throw the other four numbers away."""
    path = tmp_path / "audio-mic.env"
    path.write_text("\n".join([
        "WALL_AUDIO_MIC_CAPTURE_PERCENT=30",
        "WALL_AUDIO_MIC_BOOST=nine",
        "WALL_AUDIO_MIC_REAR_LEVEL=900",
        "WALL_AUDIO_MIC_TREBLE=1",
        "not an assignment",
    ]) + "\n", encoding="utf-8")
    values, notes = applier.load_mic(path)
    assert values["capture_percent"] == 30, "the good one survives"
    assert values["boost"] == applier.MIC_DEFAULTS["boost"]
    assert values["rear_level"] == applier.MIC_DEFAULTS["rear_level"]
    assert values["rear_gain"] == applier.MIC_DEFAULTS["rear_gain"]
    assert len(notes) == 3, notes
    assert any("boost" in note for note in notes)
    assert any("treble" in note for note in notes)


def test_the_mic_env_round_trips_sr029(applier, tmp_path):
    wanted = applier.parse_mic_assignments(["capture_percent=30", "rear_gain=0.75"],
                                           dict(applier.MIC_DEFAULTS))
    path = tmp_path / "audio-mic.env"
    path.write_text(applier.render_mic_env(wanted), encoding="utf-8")
    back, notes = applier.load_mic(path)
    assert notes == []
    assert back == wanted


def test_only_the_rear_pair_of_the_adapter_moves_sr029(applier):
    """The other six values are the Owner's bench session, verbatim.

    Read off the panel 2026-09-14: 66,66,24,24,0,0,24,24. Everything except
    channels 4 and 5 belongs to somebody who stood at an amplifier.
    """
    bench = [66, 66, 24, 24, 0, 0, 24, 24]
    assert applier.rear_levels(bench, 66) == [66, 66, 24, 24, 66, 66, 24, 24]
    assert applier.rear_levels(bench, 0) == bench
    assert bench == [66, 66, 24, 24, 0, 0, 24, 24], "the input must not be mutated"


@pytest.mark.parametrize("text,expected", [
    ("  : values=66,66,24,24,0,0,24,24\n", [66, 66, 24, 24, 0, 0, 24, 24]),
    ("numid=8\n  ; type=INTEGER\n  : values=1,2,3,4,5,6,7,8\n", [1, 2, 3, 4, 5, 6, 7, 8]),
    ("  : values=66,66\n", None),
    ("  : values=a,b,c,d,e,f,g,h\n", None),
    ("nothing useful", None),
    ("", None),
    (None, None),
])
def test_an_unreadable_adapter_level_moves_nothing_sr029(applier, text, expected):
    """None means "leave the hardware alone", and every caller must honour it."""
    assert applier.parse_adapter_levels(text) == expected


def test_the_rear_pair_is_only_raised_while_a_mic_leg_runs_sr029(applier, policy):
    """Otherwise it goes back to the 0 the tone era left it at.

    Two independent things then have to be wrong at once before the room's music
    reaches the desktop's input: this level, and the speaker route's zeros.
    """
    bench = "  : values=66,66,24,24,0,0,24,24\n"

    def run_amixer(argv, **kwargs):
        return _Reply(0, bench)

    live = applier.Applier(run=run_amixer)
    assert applier.set_rear_level(live, "ICUSBAUDIO7D", 66) is True
    csets = [argv for argv in live.commands if "cset" in argv]
    assert csets and csets[-1][-1] == "66,66,24,24,66,66,24,24"

    quiet = applier.Applier(run=run_amixer)
    applier.set_rear_level(quiet, "ICUSBAUDIO7D", 0)
    # Already 0 in the bench values, so nothing is written at all.
    assert not [argv for argv in quiet.commands if "cset" in argv]


def test_a_changed_microphone_restarts_the_leg_rather_than_starting_it_sr029(
        applier, policy, tmp_path, monkeypatch):
    """ALSA resolves @func getenv when the PCM is OPENED, not per sample.

    A running forwarder holds the microphone it started with for its whole life,
    and `systemctl start` on an active unit does nothing. Without the restart,
    moving Speaker -> Headset leaves the far end of a call listening to the
    PANEL's microphone: audio flows, everything looks right, and it is the wrong
    room.
    """
    monkeypatch.setattr(applier, "MIC_SOURCE_ENV", tmp_path / "audio-mic-source.env")
    monkeypatch.delenv(applier.MIC_SOURCE_VAR, raising=False)

    first = applier.Applier()
    assert applier.select_mic_source(first, "mic_panel") is False, "nothing to change from"
    second = applier.Applier()
    assert applier.select_mic_source(second, "mic_headset") is True
    third = applier.Applier()
    assert applier.select_mic_source(third, "mic_headset") is False, "no change, no restart"


def test_a_mic_leg_never_fails_the_apply_that_carries_the_music_sr029(
        applier, policy, tmp_path, monkeypatch):
    """apply-state runs under a unit firstboot waits on.

    A microphone that will not start is a degraded panel; a non-zero return here
    is a red firstboot and, at the glass, a panel that looks broken. The output
    legs keep counting, because those ARE the audio.
    """
    # apply_plan is run for real here (not --dry-run), so every /run path it
    # publishes has to land in the temp tree. Without this the test writes
    # audio-speaker.env into the real filesystem and the NEXT run of the suite
    # reads it back as a published chain -- which is how it was found.
    for name in ("SPEAKER_CHAIN_ENV", "MIC_SOURCE_ENV", "HEADSET_ENV"):
        monkeypatch.setattr(applier, name, tmp_path / name.lower())
    monkeypatch.delenv(applier.SPEAKER_CHAIN_VAR, raising=False)
    monkeypatch.delenv(applier.MIC_SOURCE_VAR, raising=False)

    def fail_mic_legs(argv, **kwargs):
        if argv[0].endswith("systemctl") and len(argv) > 2 and argv[2] in policy.MIC_LEGS:
            return _Reply(1, "", "Job failed")
        if argv[0].endswith("amixer") and "cget" in argv:
            return _Reply(0, "  : values=66,66,24,24,0,0,24,24\n")
        return _Reply(0)

    shell = applier.Applier(run=fail_mic_legs)
    assert applier.apply_plan(policy.default_state(), shell) == 0

    # ... and the same failure on an OUTPUT leg does count.
    def fail_output_leg(argv, **kwargs):
        if argv[0].endswith("systemctl") and len(argv) > 2 \
                and argv[2] == "wall-speaker-out.service":
            return _Reply(1, "", "Job failed")
        if argv[0].endswith("amixer") and "cget" in argv:
            return _Reply(0, "  : values=66,66,24,24,0,0,24,24\n")
        return _Reply(0)

    assert applier.apply_plan(policy.default_state(),
                             applier.Applier(run=fail_output_leg)) > 0


def test_the_stereo_fallback_and_the_rear_leg_cannot_both_run_sr029(applier):
    """The one place step 3's fallback and step 4's leg are exclusive.

    `speaker_stereo` is deliberately still a RAW open of the adapter -- the point
    of a fallback is that it is the simplest thing that can work -- so while it
    is running the adapter is held exclusively and the rear leg cannot open it.
    Refused with a journal line that names the fix, rather than left to fail in
    a restart loop.
    """
    shell = applier.Applier(dry_run=True)
    assert applier.rear_mic_possible(shell, chain=applier.SPEAKER_CHAIN_STEREO,
                                     speaker_wanted=True) is False
    # In Mute and in Headset the speaker leg is stopped, so the adapter is free.
    assert applier.rear_mic_possible(shell, chain=applier.SPEAKER_CHAIN_STEREO,
                                     speaker_wanted=False) is True
    assert applier.rear_mic_possible(shell, chain=applier.SPEAKER_CHAIN_MULTI,
                                     speaker_wanted=True) is True


def test_mic_selected_is_one_name_so_aec_is_one_line_sr029():
    """The later AEC step inserts a cancelled PCM by redefining ONE name.

    Nothing downstream may open `mic_panel` or `mic_headset` directly, or that
    insertion stops being one line and becomes an edit to every leg.
    """
    conf = read(BUS_CONF)
    assert "pcm.mic_selected {" in conf
    assert "WALL_AUDIO_MIC_SOURCE" in conf
    # The UNITS, and both of them: a `or True` here meant this could never fail
    # and would not have caught a direct source name in either one (terra,
    # 2026-09-14).
    for unit in (MIC_REAR_UNIT_FILE, BT_MIC_UNIT_FILE):
        execstart = read(unit).split("ExecStart=", 1)[1]
        assert "mic_selected" in execstart or unit is BT_MIC_UNIT_FILE
        assert "mic_panel" not in execstart, unit.name
        assert "mic_headset" not in execstart, unit.name
    # And the supervisor addresses the same one name in code.
    assert 'MIC_PCM = "mic_selected"' in read(BT_MIC)
    assert '"mic_panel"' not in read(BT_MIC).split("MIC_PCM")[-1]


def test_the_mic_capture_pcms_are_dsnoop_on_one_card_each_sr029():
    """dsnoop, so the AEC measurement loop can read the same mic as the leg."""
    conf = read(BUS_CONF)
    for name in ("mic_panel_raw", "mic_headset_raw"):
        block = conf.split("pcm.%s {" % name, 1)[1].split("\n}", 1)[0]
        assert "type dsnoop" in block
        assert "ipc_key" in block
    # The panel mic is mixed to mono EXPLICITLY rather than by plug's own
    # channel conversion: this signal ends up on somebody's telephone call.
    mono = conf.split("pcm.mic_panel {", 1)[1].split("\n}", 1)[0]
    assert "type route" in mono
    assert "ttable.0.0 0.5" in mono and "ttable.1.0 0.5" in mono


def test_every_ipc_key_in_bus_mode_is_unique_sr029():
    """A dmix and a dsnoop that collide look convincingly like a signal."""
    conf = read(BUS_CONF)
    keys = [int(line.split()[1]) for line in conf.splitlines()
            if line.strip().startswith("ipc_key ")]
    assert len(keys) == len(set(keys)), sorted(keys)
    assert 7715 in keys and 8825 in keys and 8826 in keys


def test_bluealsa_gains_hfp_hf_and_keeps_a2dp_sr029():
    """The panel is the phone's HANDS-FREE unit, not its gateway."""
    override = read(WALL / "wall-bluealsa-override.conf")
    # The COMMAND, not the prose above it: the comment quotes the stock unit's
    # own flags, and a test that grepped the whole file would pass on the
    # explanation rather than on what the panel runs.
    command = [line for line in override.splitlines()
               if line.startswith("ExecStart=") and line.strip() != "ExecStart="]
    assert len(command) == 1, command
    command = command[0]
    assert "-p hfp-hf" in command, "hfp-hf: the panel is the phone's headset"
    assert "-p hfp-ag" not in command, "hfp-ag would make the panel the telephone"
    assert "-p a2dp-sink" in command, "music must still work"
    assert "-p a2dp-source" not in command, "SR-025: no pulling audio off the panel"


@pytest.mark.parametrize("tree,expected", [
    ("", []),
    ("/org/bluealsa\n", []),
    ("/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/sco/sink\n", ["AA:BB:CC:DD:EE:FF"]),
    # a2dp endpoints are not a call and must not start a microphone
    ("/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dp/sink\n", []),
    # the far end's voice is the OTHER direction and is not this leg
    ("/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/sco/source\n", []),
    ("/org/bluealsa/hci0/dev_11_22_33_44_55_66/sco/sink\n"
     "/org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/sco/sink\n",
     ["11:22:33:44:55:66", "AA:BB:CC:DD:EE:FF"]),
])
def test_only_an_sco_sink_is_a_call_sr029(btmic, tree, expected):
    assert btmic.parse_sco_sinks(tree) == expected


def test_a_call_in_progress_is_never_moved_to_another_phone_sr029(btmic):
    both = ["11:22:33:44:55:66", "AA:BB:CC:DD:EE:FF"]
    assert btmic.decide(both, None) == "11:22:33:44:55:66", "deterministic, not arbitrary"
    assert btmic.decide(both, "AA:BB:CC:DD:EE:FF") == "AA:BB:CC:DD:EE:FF"
    assert btmic.decide([], "AA:BB:CC:DD:EE:FF") is None


def test_the_bt_mic_leg_reads_the_selected_mic_and_writes_sco_sr029(btmic):
    argv = btmic.loop_argv("AA:BB:CC:DD:EE:FF")
    assert "mic_selected" in argv
    assert "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=sco" in argv
    assert argv[0].endswith("wall-alsaloop-guard.py"), "item 25's lesson applies here too"
    # HFP is mono at 8 or 16 kHz (review finding 4, accepted): asking for stereo
    # would make every open a conversion nobody asked for.
    assert argv[argv.index("--channels") + 1] == "1"


def test_bluealsa_unreachable_means_no_call_not_an_open_microphone_sr029(btmic):
    """The failure direction that matters is a mic that stays open."""
    def explode(*args, **kwargs):
        raise OSError("no busctl here")

    assert btmic.read_sinks(run=explode) == []

    def refuse(*args, **kwargs):
        return _Reply(1, "", "no such service")

    assert btmic.read_sinks(run=refuse) == []


@pytest.mark.parametrize("raw,expected", [
    (None, 5.0), ("", 5.0), ("nonsense", 5.0), ("nan", 5.0),
    ("0.1", 1.0), ("600", 60.0), ("12", 12.0),
])
def test_the_bt_poll_is_clamped_not_refused_sr029(btmic, raw, expected):
    """No operator is standing in front of this daemon."""
    assert btmic.poll_seconds(raw) == expected


def test_the_bt_mic_leg_stops_when_bus_mode_is_rolled_back_sr029(btmic, tmp_path):
    """`wall-audio-mode trigger` is the Owner's rollback and must roll this back too."""
    mode = tmp_path / "audio-mode"
    assert btmic.bus_mode_active(mode) is False, "a missing file is not bus mode"
    mode.write_text("trigger\n", encoding="utf-8")
    assert btmic.bus_mode_active(mode) is False
    mode.write_text("bus\n", encoding="utf-8")
    assert btmic.bus_mode_active(mode) is True


def test_firstboot_seeds_the_mic_knobs_only_if_absent_sr029():
    """The amp-trigger.env rule: a re-run must not discard a bench number."""
    text = read(WALL / "wall-firstboot.sh")
    assert "if [ ! -f /etc/wall-panel/audio-mic.env ]; then" in text
    assert "wall-audio-output mic --render" in text
    assert "wall-bt-mic.py" in text, "the supervisor must be installed"
    for unit in ("wall-mic-rear.service", "wall-bt-mic.service"):
        assert unit in text, "%s must be installed" % unit


def test_the_mode_switch_takes_the_mic_legs_with_it_sr029():
    """Item 25's lesson: a forwarder left holding a dmix leaves a stale segment."""
    text = read(WALL / "wall-audio-mode")
    stop = [line for line in text.splitlines() if "unit stop" in line]
    assert any("wall-mic-rear.service" in line and "wall-bt-mic.service" in line
               for line in stop), stop
    assert "wall-mic-rear wall-bt-mic" in text, "and the IPC sweep must know them"


# ── what review found, and the asymmetry it forced ────────────────────────
# Both of these are regression tests for a FAIL-OPEN on the one control in this
# design whose failure nobody in the room can hear (terra, 2026-09-14).

def test_a_mic_leg_that_will_not_stop_fails_the_apply_sr029(applier, policy, monkeypatch):
    """Failing to START a microphone is degradation. Failing to STOP one is not.

    Before this, both directions were `required=False`: a transient systemctl
    error would have persisted `input_muted: true`, returned success, and left
    the microphone transmitting with the panel believing it was muted.
    """
    muted, _ = policy.apply_event(policy.default_state(),
                                  {"kind": "set_input_mute", "muted": True})

    def refuse_to_stop(argv, **kwargs):
        if argv[0].endswith("systemctl") and len(argv) > 2 and argv[1] == "stop" \
                and argv[2] in policy.MIC_LEGS:
            return _Reply(1, "", "Job for %s failed" % argv[2])
        if argv[0].endswith("amixer") and "cget" in argv:
            return _Reply(0, "  : values=66,66,24,24,0,0,24,24\n")
        return _Reply(0)

    shell = applier.Applier(run=refuse_to_stop)
    assert applier.apply_plan(muted, shell) > 0, \
        "a microphone that would not stop must NOT report success"


def test_a_capture_switch_that_will_not_close_fails_the_apply_sr029(
        applier, policy, monkeypatch):
    """The hardware half of the mute fails in the same direction as the legs."""
    monkeypatch.setattr(applier, "headset_card", lambda *a, **k: None)
    muted, _ = policy.apply_event(policy.default_state(),
                                  {"kind": "set_input_mute", "muted": True})

    def refuse_nocap(argv, **kwargs):
        if argv[0].endswith("amixer") and "nocap" in argv:
            return _Reply(1, "", "Unable to find simple control 'Capture',0")
        if argv[0].endswith("amixer") and "cget" in argv:
            return _Reply(0, "  : values=66,66,24,24,0,0,24,24\n")
        return _Reply(0)

    assert applier.apply_plan(muted, applier.Applier(run=refuse_nocap)) > 0

    # ... and the UN-mute direction does not, because a capture switch that will
    # not open is a microphone that does not work, which the leg already said.
    live = policy.default_state()

    def refuse_cap(argv, **kwargs):
        if argv[0].endswith("amixer") and "cap" in argv and "nocap" not in argv:
            return _Reply(1, "", "Unable to find simple control 'Capture',0")
        if argv[0].endswith("amixer") and "cget" in argv:
            return _Reply(0, "  : values=66,66,24,24,0,0,24,24\n")
        return _Reply(0)

    assert applier.apply_plan(live, applier.Applier(run=refuse_cap)) == 0


def test_the_bt_supervisor_re_checks_the_mute_every_poll_sr029(btmic, policy, tmp_path):
    """It is Restart=always, so "it was stopped once" is not a property.

    An operator restart, a daemon-reload workflow, a crash, or a stop that failed
    during an apply would each have reopened the microphone into a live call.
    The supervisor therefore reads the state itself rather than trusting that a
    command reached it.
    """
    state = tmp_path / "audio-state.json"

    def store(value):
        state.write_text(json.dumps(value), encoding="utf-8")

    store(policy.default_state())
    assert btmic.mic_allowed(state) is True, "speaker, unmuted: a mic may run"

    muted, _ = policy.apply_event(policy.default_state(),
                                  {"kind": "set_input_mute", "muted": True})
    store(muted)
    assert btmic.mic_allowed(state) is False, "ruling E's mute reaches the supervisor"

    absent, _ = policy.apply_event(policy.default_state(),
                                   {"kind": "headset", "present": True})
    absent, _ = policy.apply_event(absent, {"kind": "headset", "present": False})
    store(absent)
    assert btmic.mic_allowed(state) is False, "ruling 7 reaches it too"


@pytest.mark.parametrize("content", [None, "", "not json", "{oops"])
def test_an_unparseable_state_never_opens_a_microphone_sr029(btmic, tmp_path, content):
    """A document that cannot be READ answers False. That gate fails CLOSED.

    A leg that does not run is a degraded panel; a leg that runs against a mute
    is the failure nobody in the room can see, so an absent or unparseable state
    file is not consent.
    """
    state = tmp_path / "audio-state.json"
    if content is not None:
        state.write_text(content, encoding="utf-8")
    assert btmic.mic_allowed(state) is False


@pytest.mark.parametrize("content", ["[]", '{"output": 12}', '{"input_muted": "yes"}',
                                     '{"volume": {"speaker": "loud"}}'])
def test_a_damaged_state_comes_back_MUTED_sr029(btmic, tmp_path, content):
    """A damaged file must not turn a privacy control from muted to live.

    This test asserted the opposite in the first round, on the argument that the
    applier and the supervisor normalizing the same document the same way made
    it safe. Review (terra, round 2) refused that, correctly: agreeing to open a
    microphone nobody asked for is not safety. `normalize` now returns
    `input_muted: True` for any document that needed repair, so BOTH halves see
    the safe answer and they still agree.
    """
    state = tmp_path / "audio-state.json"
    state.write_text(content, encoding="utf-8")
    assert btmic.mic_allowed(state) is False


def test_a_repair_mutes_the_input_but_costs_nothing_else_sr029(policy):
    """Field by field is kept: a typo must not cost the panel its audio policy.

    Only the microphone fails closed, because only the microphone's default is
    the unsafe direction.
    """
    repaired = policy.normalize({"output": "speakers", "volume": {"speaker": 80}})
    assert repaired["input_muted"] is True, "damage mutes the input"
    assert repaired["output"] == "speaker", "and the rest still falls back sanely"
    assert repaired["volume"]["speaker"] == 80, "a good field survives a bad one"

    # AN ABSENT KEY IS NOT CONSENT EITHER, and the earlier version of this test
    # blessed the opposite (terra, round 4). A truncated write is
    # indistinguishable from a file that never had the field, so absence is
    # muted; only an explicit boolean `false` un-mutes.
    partial = policy.normalize({"output": "headset"})
    assert partial["input_muted"] is True
    assert policy.normalize({})["input_muted"] is True
    # And a whole, valid document is untouched in both directions.
    assert policy.normalize(policy.default_state()) == policy.default_state()


def test_the_supervisor_and_the_applier_share_one_definition_of_mic_live_sr029(btmic):
    """The policy is not duplicated: firstboot installs both beside each other."""
    source = read(BT_MIC)
    assert "policy.mic_live" in source, "the single definition, not a second copy"
    assert "import wall_audio_state" in source
    installed = read(WALL / "wall-firstboot.sh")
    assert "wall_audio_state.py" in installed and "wall-bt-mic.py" in installed
    # Both into the SAME directory, or the import above cannot resolve.
    block = installed.split("wall_audio_state.py wall-bt-mic.py", 1)[1][:400]
    assert "/usr/local/lib/wall-panel/" in block


# ── review round 2: the restart paths, and telling the truth about hardware ─

def test_both_mic_units_re_check_the_mute_on_every_start_sr029():
    """Restart=always means "it was stopped once" is not a property.

    A guard crash, an operator restart, a daemon-reload workflow, or a stop that
    FAILED during an apply would each bring a mic leg back with the state still
    saying muted. Found once per leg by review (terra, rounds 1 and 2).
    """
    for unit in (MIC_REAR_UNIT_FILE, BT_MIC_UNIT_FILE):
        text = read(unit)
        assert "Restart=always" in text, "the premise of the gate"
        conditions = [line for line in text.splitlines()
                      if line.startswith("ExecCondition=")]
        assert any("mic-allowed" in line for line in conditions), \
            "%s can be restarted behind a mute" % unit.name
        assert any("audio-mode" in line for line in conditions), \
            "%s must still carry the bus-mode gate" % unit.name


def test_the_mic_allowed_gate_takes_no_lock_sr029(applier):
    """It is asked by an ExecCondition while the apply that starts the leg holds it.

    A gate that blocked on the apply lock would deadlock the very apply it is
    gating, which is the same shape as the withdrawn firstboot ordering.
    """
    source = read(APPLIER)
    dispatch = source.split('if arguments.command == "mic-allowed":', 1)[1][:600]
    assert "apply_lock" not in dispatch
    # and it is dispatched BEFORE the block that takes the lock
    assert source.index('"mic-allowed"') < source.index("with apply_lock(")


@pytest.mark.parametrize("stored,allowed", [
    ({"output": "speaker", "input_muted": False}, 0),
    ({"output": "speaker", "input_muted": True}, 1),
    # Item J: output Mute couples the microphone, so the gate refuses.
    ({"output": "mute", "input_muted": False}, 1),
    ({"output": "mute", "input_muted": True}, 1),
    # ruling 7: headset selected, adapter absent -> nothing tunnelled
    ({"output": "headset", "input_muted": False, "headset_present": False}, 1),
    ({"output": "headset", "input_muted": False, "headset_present": True}, 0),
])
def test_mic_allowed_answers_the_policy_sr029(applier, tmp_path, stored, allowed):
    state = tmp_path / "audio-state.json"
    state.write_text(json.dumps(stored), encoding="utf-8")
    assert applier.main(["--state", str(state), "mic-allowed"]) == allowed


def test_a_missing_state_file_is_not_consent_sr029(applier, tmp_path):
    """And the two gates must answer identically, or they can disagree about a mic.

    Not a hardship: the applier SAVES the state before it applies the plan, so by
    the time it runs `systemctl start` on a mic leg the file is there.
    """
    missing = tmp_path / "nothing.json"
    assert applier.main(["--state", str(missing), "mic-allowed"]) == 1
    btmic_module = load(BT_MIC, "wall_bt_mic_gate")
    assert btmic_module.mic_allowed(missing) is False
    source = read(APPLIER)
    assert "save_state" in source.split("def _locked_apply", 1)[1].split("apply_plan(", 1)[0]


def test_a_rear_level_that_would_not_lower_fails_the_apply_sr029(applier, policy):
    """It reported success whether or not the write landed (terra, round 2).

    Failing to RAISE the rear pair is a quiet mic return. Failing to LOWER it
    leaves the desktop's input wired to live channels after the leg has gone,
    which is the independent hardware barrier this is supposed to be.
    """
    raised = "  : values=66,66,24,24,66,66,24,24\n"

    def refuse_cset(argv, **kwargs):
        if argv[0].endswith("amixer") and "cset" in argv:
            return _Reply(1, "", "Unable to find control")
        if argv[0].endswith("amixer") and "cget" in argv:
            return _Reply(0, raised)
        return _Reply(0)

    # Lowering (no mic leg wanted): a refused write is reported and counted.
    muted, _ = policy.apply_event(policy.default_state(),
                                  {"kind": "set_input_mute", "muted": True})
    assert applier.apply_plan(muted, applier.Applier(run=refuse_cset)) > 0

    # Raising: a refused write is a quiet microphone, not a failed apply.
    lowered = "  : values=66,66,24,24,0,0,24,24\n"

    def refuse_raise(argv, **kwargs):
        if argv[0].endswith("amixer") and "cset" in argv:
            return _Reply(1, "", "Unable to find control")
        if argv[0].endswith("amixer") and "cget" in argv:
            return _Reply(0, lowered)
        return _Reply(0)

    assert applier.apply_plan(policy.default_state(),
                             applier.Applier(run=refuse_raise)) == 0


def test_set_rear_level_reports_what_actually_happened_sr029(applier):
    """The unconditional `return True` is what made the above invisible."""
    raised = "  : values=66,66,24,24,66,66,24,24\n"

    def refuse(argv, **kwargs):
        if "cset" in argv:
            return _Reply(1, "", "nope")
        return _Reply(0, raised)

    assert applier.set_rear_level(applier.Applier(run=refuse), "ICUSBAUDIO7D", 0) is False

    def accept(argv, **kwargs):
        if "cset" in argv:
            return _Reply(0)
        return _Reply(0, raised)

    assert applier.set_rear_level(applier.Applier(run=accept), "ICUSBAUDIO7D", 0) is True


# ── review round 3: the gate's own fail-open, and two boundaries ───────────

@pytest.mark.parametrize("content", ["", "not json", "{oops", '{"input_muted": "yes"}',
                                     "[]", '{"output": 12}'])
def test_the_applier_gate_refuses_a_damaged_state_too_sr029(applier, tmp_path, content):
    """It used load_state, which exists to keep AUDIO working, not a microphone.

    load_state turns an unreadable or unparseable file into `default_state()` --
    Speaker, UNMUTED -- so the gate exited 0 and started a real forwarder off a
    corrupt file while claiming "every failure answers 1". It also disagreed with
    the Bluetooth supervisor, which parses the document itself (terra, round 3).
    """
    state = tmp_path / "audio-state.json"
    state.write_text(content, encoding="utf-8")
    assert applier.main(["--state", str(state), "mic-allowed"]) == 1


def test_both_gates_answer_the_same_for_every_state_sr029(applier, tmp_path):
    """Two gates on one privacy control must not be able to disagree."""
    btmic_module = load(BT_MIC, "wall_bt_mic_agree")
    state = tmp_path / "audio-state.json"
    cases = [
        None,                                             # missing
        "", "not json", "{oops",                          # unparseable
        "[]", '{"output": 12}', '{"input_muted": "yes"}',  # damaged
        '{"volume": {"speaker": 101}}',                   # clamped
        '{"output": "speaker", "input_muted": false}',    # clean, allowed
        '{"output": "speaker", "input_muted": true}',     # clean, muted
        '{"output": "headset", "input_muted": false, "headset_present": false}',
    ]
    for content in cases:
        if content is None:
            state.unlink(missing_ok=True)
        else:
            state.write_text(content, encoding="utf-8")
        gate = applier.main(["--state", str(state), "mic-allowed"]) == 0
        supervisor = btmic_module.mic_allowed(state)
        assert gate is supervisor, "the two gates disagree about %r" % content


@pytest.mark.parametrize("stored,expected_level,muted", [
    (101, 100, True),
    (-1, 0, True),
    (100, 100, False),
    (0, 0, False),
    (60, 60, False),
])
def test_a_clamped_volume_counts_as_a_repair_sr029(policy, stored, expected_level, muted):
    """The rule failed on its own boundary (terra, round 3).

    `{"speaker": 101}` came back as 100 and stayed UNMUTED, while the applier
    journaled the same document as repaired -- so the two halves already
    disagreed in the log about whether anything had been replaced.
    """
    state = policy.normalize({"output": "speaker", "input_muted": False,
                              "volume": {"speaker": stored, "headset": 60}})
    assert state["volume"]["speaker"] == expected_level
    assert state["input_muted"] is muted


def test_a_mode_switch_refuses_while_a_microphone_is_still_running_sr029():
    """Every other leg surviving a stop is a wrong noise. This one is a live mic.

    A mode switch changes the ALSA chain, but an already-open client keeps the
    graph it opened with -- so a surviving wall-mic-rear goes on sending the room
    to the desktop's input in a mode whose whole point is that the audio work was
    rolled back.
    """
    text = read(WALL / "wall-audio-mode")
    assert "stop_mic_legs_or_refuse()" in text
    # It runs BEFORE anything else in the mode switch moves.
    body = text.split("  trigger|panel|bus)", 1)[1]
    assert body.index("stop_mic_legs_or_refuse") < body.index("remember_active")
    # It escalates rather than noting a failure and carrying on, and it aborts.
    helper = text.split("stop_mic_legs_or_refuse() {", 1)[1].split("\n}", 1)[0]
    assert "is-active" in helper
    assert "kill" in helper
    assert "return 1" in helper
    assert "REFUSING" in helper
    assert "stop_mic_legs_or_refuse || exit 1" in text


# ── review round 4: the one rule, and the stop that has to bite ───────────

@pytest.mark.parametrize("raw,muted", [
    ({"output": "speaker", "input_muted": False}, False),
    ({"output": "speaker", "input_muted": True}, True),
    ({}, True),
    ({"output": "speaker"}, True),
    ({"input_muted": "false"}, True),
    ({"input_muted": 0}, True),
    ({"input_muted": None}, True),
    ([], True),
    ("nonsense", True),
    (None, True),
])
def test_the_microphone_is_muted_unless_the_document_says_false_sr029(policy, raw, muted):
    """The one rule, after four rounds found four ways round the last one.

    Not "unless it says true", and not "unless a field was repaired": both of
    those left a hole, because a TRUNCATED write and an unreadable file are
    indistinguishable from a fresh one if absence counts as consent.
    """
    assert policy.normalize(raw)["input_muted"] is muted


def test_an_unreadable_state_file_is_not_rewritten_into_consent_sr029(applier, tmp_path):
    """The applier PERSISTS what it loaded before it applies it.

    So an unreadable file returning the unmuted default was rewritten as a
    valid, unmuted document, and the units' own ExecCondition then saw consent
    nobody gave. A file that cannot be read comes back MUTED.
    """
    damaged = tmp_path / "audio-state.json"
    damaged.write_text("{truncated", encoding="utf-8")
    state, note = applier.load_state(damaged)
    assert state["input_muted"] is True
    assert "MUTED" in note

    # A panel that has NEVER been configured is different in kind: there is no
    # prior mute to lose, and a fresh image whose microphone needs a button
    # press nobody has a button for yet would be the worse failure.
    fresh, note = applier.load_state(tmp_path / "never-written.json")
    assert fresh["input_muted"] is False
    assert fresh == applier.policy.default_state()


def test_muting_escalates_to_a_kill_rather_than_counting_and_moving_on_sr029(
        applier, policy):
    """A stop that failed left an already-open alsaloop transmitting.

    apply_plan counted the failure and carried on; wall-mic-rear has no runtime
    state recheck, so its open PCM went on sending the room to the desktop's
    input while the stored state said muted. The mode switch already escalated;
    `input-mute` did not (terra, round 4).
    """
    muted, _ = policy.apply_event(policy.default_state(),
                                  {"kind": "set_input_mute", "muted": True})
    seen = []

    def stubborn(argv, **kwargs):
        seen.append(list(argv))
        if argv[0].endswith("systemctl") and argv[1] == "stop" \
                and len(argv) > 2 and argv[2] in policy.MIC_LEGS:
            return _Reply(1, "", "Job failed")
        if argv[0].endswith("systemctl") and argv[1] == "show" \
                and argv[-1] in policy.MIC_LEGS:
            return _Reply(0, "active\n")   # still running, and it SAYS so
        if argv[0].endswith("amixer") and "cget" in argv:
            return _Reply(0, "  : values=66,66,24,24,0,0,24,24\n")
        return _Reply(0)

    assert applier.apply_plan(muted, applier.Applier(run=stubborn)) > 0
    kills = [argv for argv in seen
             if argv[0].endswith("systemctl") and argv[1] == "kill"
             and argv[-1] in policy.MIC_LEGS]
    assert kills, "a mic leg that will not stop must be KILLED, not just counted"


def test_a_mic_leg_that_stops_normally_is_never_killed_sr029(applier, policy):
    """Escalation only where it is needed: a clean stop stays a clean stop."""
    muted, _ = policy.apply_event(policy.default_state(),
                                  {"kind": "set_input_mute", "muted": True})
    seen = []

    def obedient(argv, **kwargs):
        seen.append(list(argv))
        if argv[0].endswith("systemctl") and argv[1] == "show":
            return _Reply(0, "inactive\n")   # the stop worked, confirmed
        if argv[0].endswith("amixer") and "cget" in argv:
            return _Reply(0, "  : values=66,66,24,24,0,0,24,24\n")
        return _Reply(0)

    assert applier.apply_plan(muted, applier.Applier(run=obedient)) == 0
    assert not [argv for argv in seen
                if argv[0].endswith("systemctl") and argv[1] == "kill"]


# ── review round 5: "gone" and "I could not ask" are different answers ─────

@pytest.mark.parametrize("stdout,code,expected", [
    ("inactive\n", 0, False),
    ("failed\n", 0, False),
    ("active\n", 0, True),
    ("activating\n", 0, True),
    # NOT gone: it is still shutting down, and a microphone that is still
    # shutting down is still open.
    ("deactivating\n", 0, True),
    ("reloading\n", 0, True),
    # Unobservable, all of these. None, and every caller must treat it as live.
    ("", 0, None),
    ("\n", 0, None),
    (None, 1, None),
])
def test_unit_active_never_guesses_sr029(applier, stdout, code, expected):
    """`systemctl is-active --quiet` could not express this and that was the bug.

    It exits non-zero for an inactive unit AND for a D-Bus error, a timeout or a
    missing systemctl, and Applier.command collapses all of them to 1 -- so "the
    microphone is gone" and "I could not ask whether the microphone is gone"
    were the same answer (terra, round 5).
    """
    def reply(argv, **kwargs):
        return _Reply(code, stdout if stdout is not None else "")

    assert applier.unit_active(applier.Applier(run=reply), "x.service") is expected


def test_an_unqueryable_mic_leg_fails_the_apply_sr029(applier, policy):
    """Unknown must fail the apply, not pass it."""
    def unqueryable(argv, **kwargs):
        if argv[0].endswith("systemctl") and argv[1] == "show":
            return _Reply(1, "", "Failed to connect to bus")
        if argv[0].endswith("amixer") and "cget" in argv:
            return _Reply(0, "  : values=66,66,24,24,0,0,24,24\n")
        return _Reply(0)

    muted, _ = policy.apply_event(policy.default_state(),
                                  {"kind": "set_input_mute", "muted": True})
    assert applier.apply_plan(muted, applier.Applier(run=unqueryable)) > 0


def test_the_mode_script_also_refuses_an_unqueryable_mic_leg_sr029():
    """The same distinction, in the shell half."""
    text = read(WALL / "wall-audio-mode")
    assert "mic_leg_gone()" in text
    helper = text.split("mic_leg_gone() {", 1)[1].split("\n}", 1)[0]
    assert "ActiveState" in helper, "is-active cannot express 'I could not ask'"
    assert "inactive|failed) return 0" in helper
    assert "*) return 1" in helper, "anything else, including empty, is STILL THERE"
    # Scoped to the helper's own body: `remember_active` legitimately uses
    # `is-active --quiet` elsewhere, where "I could not ask" is not a safety
    # question.
    stopper = text.split("stop_mic_legs_or_refuse() {", 1)[1].split("\n}", 1)[0]
    decisions = [line for line in stopper.splitlines()
                 if not line.strip().startswith(("echo", "#"))]
    assert not any("is-active" in line for line in decisions), decisions


def test_a_replugged_adapter_brings_the_rear_mic_leg_back_sr029():
    """It BindsTo the adapter, so an unplug stops it. Nothing restarted it.

    wall-audio-state.service is RemainAfterExit, so it does not re-assert the
    stored position either, and one hub move -- which this appliance is known to
    do, item 25 -- lost the mic return to the desktop permanently (terra, round
    5). Safe to start from udev because the unit gates itself twice on every
    start attempt.
    """
    rule = read(WALL / "90-wall-audio-adapter.rules")
    assert 'SYSTEMD_WANTS}+="wall-mic-rear.service"' in rule
    unit = read(WALL / "wall-mic-rear.service")
    assert "BindsTo=dev-wall_audio_adapter.device" in unit
    conditions = [line for line in unit.splitlines()
                  if line.startswith("ExecCondition=")]
    assert any("mic-allowed" in line for line in conditions), \
        "starting it from udev is only safe because it gates itself"
    # The Bluetooth leg is NOT wanted here: it has nothing to do with this
    # adapter, and its own poll brings it back.
    assert "wall-bt-mic.service" not in rule


def test_a_dry_run_mic_command_writes_nothing_sr029(applier, tmp_path, monkeypatch):
    """`--dry-run` dropped through the mic path and it overwrote /etc for real."""
    env = tmp_path / "audio-mic.env"
    conf = tmp_path / "audio-mic.conf"
    monkeypatch.setattr(applier, "MIC_ENV", env)
    monkeypatch.setattr(applier, "MIC_CONF", conf)
    monkeypatch.setattr(applier, "LOCK_FILE", tmp_path / "lock")
    assert applier.main(["--dry-run", "--state", str(tmp_path / "s.json"),
                         "mic", "capture_percent=42"]) == 0
    assert not env.exists(), "a dry run must not write the knobs"
    assert not conf.exists(), "or the generated ALSA file"

    # ... and without --dry-run it does.
    assert applier.main(["--state", str(tmp_path / "s.json"),
                         "mic", "capture_percent=42"]) == 0
    assert "WALL_AUDIO_MIC_CAPTURE_PERCENT=42" in env.read_text(encoding="utf-8")
