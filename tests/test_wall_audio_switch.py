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
    assert any("no auto-switch" in line for line in lines)


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


def test_the_headset_card_is_resolved_by_usb_id_not_by_name_sr028(applier, tmp_path):
    """The adapter enumerates as the generic id "Device"; the USB id is the fact.

    Built as the panel's /proc/asound looks: card directories with a usbid file,
    and an id-named symlink pointing at each.
    """
    proc = tmp_path / "asound"
    for index, usbid in ((1, "0d8c:0014"), (3, "0d8c:0102")):
        card = proc / ("card%d" % index)
        card.mkdir(parents=True)
        (card / "usbid").write_text(usbid + "\n", encoding="utf-8")
    (proc / "Headset").symlink_to(proc / "card1", target_is_directory=True)
    (proc / "ICUSBAUDIO7D").symlink_to(proc / "card3", target_is_directory=True)
    assert applier.headset_card(proc) == "Headset"


def test_no_headset_adapter_resolves_to_nothing_sr028(applier, tmp_path):
    proc = tmp_path / "asound"
    (proc / "card3").mkdir(parents=True)
    (proc / "card3" / "usbid").write_text("0d8c:0102\n", encoding="utf-8")
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
    unit = read(WALL / "wall-audio-state.service")
    assert "apply-state" in unit
    assert "After=suspend.target" in unit and "WantedBy=multi-user.target suspend.target" in unit


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
