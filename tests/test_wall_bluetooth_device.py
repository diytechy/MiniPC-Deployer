"""WSN-024: the routed-device backend and the root applier behind it.

The Owner asked for three things on 2026-09-19 -- a way to trust a device, a way
to deal with a PIN, and a way to turn discoverability on -- and the answer was
the whole routed-device backend that `switch_backend` had been documenting as
deliberately absent. These pin the properties that make it safe to have:

  * the alias rule is stable and address-free, in both directions;
  * an unreadable or stale document is "do not know" and never "no devices";
  * a replayed request is not performed twice;
  * a device the panel does not trust can still be trusted.
"""

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PANEL_AUDIO = ROOT / "stack" / "panel-audio"
APPLIER = ROOT / "stack" / "autoinstall" / "wall" / "wall-bluetooth-device.py"

sys.path.insert(0, str(PANEL_AUDIO))

import bluetooth_request  # noqa: E402
import bluetooth_state  # noqa: E402
import routed_backend  # noqa: E402
import routing  # noqa: E402


# --------------------------------------------------------------- the alias rule


def test_the_alias_rule_has_one_spelling():
    """bluetooth_state and routing must agree, or the applier cannot be asked.

    The applier does not import `routing` -- it is the privileged side and takes
    as little from the broker's tree as it can -- so the length lives in both.
    An alias longer than routing.ALIAS allows is refused at the broker with a
    message about an invalid alias, for a device whose only fault was a long
    name.
    """
    longest = "a" * bluetooth_state.ALIAS_MAX
    assert routing.ALIAS.fullmatch(longest)
    assert not routing.ALIAS.fullmatch(longest + "a")


@pytest.mark.parametrize("name, expected", [
    ("Peter's Pixel 9", "peters-pixel-9"),
    ("JBL Flip 6", "jbl-flip-6"),
    # Unicode is FOLDED, not dropped: dropping it leaves "s-iphone", which is
    # both unrecognisable and a different device every time the fold changes.
    ("Pétur’s iPhone", "peturs-iphone"),
    # Must begin with a letter (routing.ALIAS), so a leading number is trimmed
    # rather than producing an alias the far end refuses.
    ("5.1 Speakers", "speakers"),
    ("   ", ""),
    ("", ""),
    (None, ""),
])
def test_a_device_name_becomes_a_recognisable_alias(name, expected):
    assert bluetooth_state.slug(name) == expected


def test_a_device_named_after_its_own_address_never_gets_an_address_shaped_alias():
    """The one case routing.ALIAS alone would wave through.

    BlueZ calls an unresolved device by its address, and `a0b1c2d3e4f5` passes
    ALIAS perfectly: it starts with a letter and is all lowercase alphanumerics.
    The HARDWARE_ADDRESS guard beside it is what actually catches it, and a slug
    that produced one would fail the whole status at the broker.
    """
    for name in ("A0:B1:C2:D3:E4:F5", "a0b1c2d3e4f5", "A0-B1-C2-D3-E4-F5"):
        assert bluetooth_state.slug(name) == "", name
    assigned = bluetooth_state.assign_aliases([("A0:B1:C2:D3:E4:F5", "A0:B1:C2:D3:E4:F5")])
    alias = assigned["A0:B1:C2:D3:E4:F5"]
    assert routing.ALIAS.fullmatch(alias)
    assert not routing.HARDWARE_ADDRESS.search(alias)


def test_aliases_do_not_depend_on_the_order_bluez_lists_devices_in():
    devices = [("AA:BB:CC:DD:EE:01", "Pixel"), ("AA:BB:CC:DD:EE:02", "Pixel")]
    first = bluetooth_state.assign_aliases(devices)
    assert bluetooth_state.assign_aliases(list(reversed(devices))) == first
    assert len(set(first.values())) == 2
    assert first["AA:BB:CC:DD:EE:01"] == "pixel"
    assert first["AA:BB:CC:DD:EE:02"] == "pixel-2"


# THE DEFECT THIS PREVENTS, and it is the one address ordering could not: a
# device that is ALREADY ON THE GLASS must not be renamed because another device
# appeared. Ordering by address is stable for a fixed set and not otherwise -- a
# second "Pixel" with a LOWER address took the bare alias and pushed the first to
# `pixel-2`, so a tap on a row painted a moment earlier acted on the other phone.
def test_a_device_already_on_the_glass_keeps_its_alias_when_another_appears():
    alone = bluetooth_state.assign_aliases([("AA:BB:CC:DD:EE:02", "Pixel")])
    assert alone["AA:BB:CC:DD:EE:02"] == "pixel"
    both = bluetooth_state.assign_aliases(
        [("AA:BB:CC:DD:EE:01", "Pixel"), ("AA:BB:CC:DD:EE:02", "Pixel")], alone)
    assert both["AA:BB:CC:DD:EE:02"] == "pixel", "the device on the glass kept its name"
    assert both["AA:BB:CC:DD:EE:01"] == "pixel-2"
    # ...and it keeps it across every later poll, including one where the other
    # device has gone away again.
    assert bluetooth_state.assign_aliases([("AA:BB:CC:DD:EE:02", "Pixel")], both)["AA:BB:CC:DD:EE:02"] == "pixel"


def test_a_renamed_device_gets_the_new_name_rather_than_keeping_the_old_alias():
    before = bluetooth_state.assign_aliases([("AA:BB:CC:DD:EE:01", "Pixel")])
    after = bluetooth_state.assign_aliases([("AA:BB:CC:DD:EE:01", "Studio Speaker")], before)
    assert after["AA:BB:CC:DD:EE:01"] == "studio-speaker"


def test_a_previous_map_naming_a_device_that_is_gone_does_not_reserve_its_alias():
    stale = {"AA:BB:CC:DD:EE:09": "pixel"}
    assigned = bluetooth_state.assign_aliases([("AA:BB:CC:DD:EE:01", "Pixel")], stale)
    assert assigned == {"AA:BB:CC:DD:EE:01": "pixel"}


def test_a_device_literally_named_like_a_suffix_does_not_steal_it():
    devices = [("AA:BB:CC:DD:EE:01", "Pixel"), ("AA:BB:CC:DD:EE:02", "Pixel"),
               ("AA:BB:CC:DD:EE:03", "Pixel 2")]
    assigned = bluetooth_state.assign_aliases(devices)
    assert len(set(assigned.values())) == 3


@pytest.mark.parametrize("uuids, kind", [
    # A phone SOURCES audio, so it is an input to the panel's bus.
    (["0000110a-0000-1000-8000-00805f9b34fb"], "input"),
    # A speaker SINKS it, so it is an output.
    (["0000110b-0000-1000-8000-00805f9b34fb"], "output"),
    # A headset does both; it is an output, because "send the music there" is
    # the choice somebody makes about it.
    (["0000110a-0000-1000-8000-00805f9b34fb",
      "0000110b-0000-1000-8000-00805f9b34fb"], "output"),
    # HFP's two UUIDs are OPPOSITE ROLES and were treated as one. 111e is the
    # Handsfree UNIT -- a headset, so an output. 111f is the Audio GATEWAY -- a
    # phone, so an input. Lumping them made every phone an output, and
    # `select_input` refuses a device whose kind is not input, so the panel could
    # not choose the phone whose microphone wall-bt-mic exists to carry.
    (["0000111e-0000-1000-8000-00805f9b34fb"], "output"),
    (["0000111f-0000-1000-8000-00805f9b34fb"], "input"),
    # A real phone: A2DP source plus the HFP gateway.
    (["0000110a-0000-1000-8000-00805f9b34fb",
      "0000111f-0000-1000-8000-00805f9b34fb"], "input"),
    ([], "input"),
    (None, "input"),
])
def test_a_device_kind_is_read_from_its_services(uuids, kind):
    assert bluetooth_state.classify(uuids) == kind


# ------------------------------------------------------------- the request file


def test_every_verb_the_policy_admits_can_be_written_as_a_request():
    """The two contracts must cover the same verbs.

    `routing.DEVICE_METHODS` is what the broker will accept and
    `bluetooth_request.REQUEST_KINDS` is what the applier will perform. A verb
    in one and not the other is either a request nothing reads or an accepted
    call that cannot be written -- and both fail as a mutation that goes
    sticky-pending rather than as an error anybody sees.
    """
    assert set(routing.DEVICE_METHODS) == set(bluetooth_request.REQUEST_KINDS)


@pytest.mark.parametrize("event", [
    {"kind": "discover", "timeoutSeconds": 30},
    {"kind": "cancel"},
    {"kind": "pair", "alias": "pixel"},
    {"kind": "pair", "alias": "pixel", "confirmation": "012345"},
    {"kind": "trust", "alias": "pixel", "trusted": True},
    {"kind": "trust", "alias": "pixel", "trusted": False},
    {"kind": "connect", "alias": "pixel"},
    {"kind": "forget", "alias": "pixel"},
    {"kind": "select_output", "alias": "flip"},
    {"kind": "set_discoverable", "enabled": True},
])
def test_a_well_formed_event_is_written(event):
    built = bluetooth_request.envelope(7, event)
    assert built["seq"] == 7 and built["event"] == event and built["version"] == 1
    assert "generation" not in built


@pytest.mark.parametrize("event, why", [
    ({"kind": "nonsense"}, "unknown"),
    ({"kind": "pair"}, "no alias"),
    ({"kind": "pair", "alias": ""}, "empty alias"),
    ({"kind": "pair", "alias": "pixel", "stray": 1}, "stray key"),
    ({"kind": "trust", "alias": "pixel"}, "no direction"),
    ({"kind": "trust", "alias": "pixel", "trusted": "yes"}, "direction is not a boolean"),
    ({"kind": "discover", "timeoutSeconds": 0}, "below the range"),
    ({"kind": "discover", "timeoutSeconds": 121}, "above the range"),
    ({"kind": "discover", "timeoutSeconds": True}, "a bool is not a timeout"),
    ({"kind": "set_discoverable", "enabled": 1}, "1 is not a boolean"),
    ({"kind": "pair", "alias": "pixel", "confirmation": "no"}, "not digits"),
    ({"kind": "pair", "alias": "pixel", "confirmation": "1" * 17}, "too long"),
])
def test_a_malformed_event_is_never_written(event, why):
    with pytest.raises(bluetooth_request.RequestError):
        bluetooth_request.envelope(1, event)


def test_the_epoch_is_omitted_rather_than_guessed():
    assert "generation" not in bluetooth_request.envelope(1, {"kind": "cancel"})
    assert bluetooth_request.envelope(1, {"kind": "cancel"}, 4)["generation"] == 4
    with pytest.raises(bluetooth_request.RequestError):
        bluetooth_request.envelope(1, {"kind": "cancel"}, -1)


# ------------------------------------------------------------------ the backend


class FakeSwitch:
    """The switch backend, as far as the wrapper is concerned."""

    LOCAL_ONLY_METHODS = frozenset({"status", "telemetry"})

    def __init__(self):
        self.calls = []

    def inventory(self, cancel):
        return []

    def call(self, method, params, cancel):
        self.calls.append((method, dict(params)))
        if method == "status":
            return {"protocolVersion": 1, "available": False, "reason": "routing-disabled",
                    "devices": [], "route": None, "visualizer": {"available": False},
                    "mute": {"supported": True, "muted": False},
                    "switch": {"supported": True, "output": "speaker"}}
        if method == "request_landed":
            return {"accepted": True, "from": "switch"}
        return {"accepted": True, "seq": 1}


def document(**overrides):
    base = {
        "version": 1,
        "publishedAt": int(time.time()),
        "reason": "ready",
        "devices": [
            {"alias": "pixel", "name": "Pixel", "kind": "input", "trusted": True,
             "connected": True, "battery": 80},
            {"alias": "flip", "name": "JBL Flip", "kind": "output", "trusted": False,
             "connected": False},
        ],
        "route": {"input": "pixel", "output": None},
        "pairing": {"active": False, "discoverable": False, "passkey": None,
                    "device": None, "endsInSeconds": None},
        "generation": 3,
        "requestSeq": 100,
    }
    base.update(overrides)
    return base


@pytest.fixture()
def backend(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps(document()), encoding="utf-8")
    applier = tmp_path / "wall-bluetooth-device"
    applier.write_text("#!/bin/sh\n", encoding="utf-8")
    switch = FakeSwitch()
    made = routed_backend.RoutedDeviceBackend(
        switch, state_path=state, request_path=tmp_path / "spool", applier_path=applier)
    made.switch_fake = switch
    made.state_file = state
    made.spool = tmp_path / "spool"
    return made


def spooled(backend):
    """Every request the backend has written, oldest first."""
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in bluetooth_request.pending(backend.spool)]


def _cancel():
    import threading
    return threading.Event()


def test_the_inventory_is_what_the_document_says(backend):
    devices = backend.inventory(_cancel())
    assert {d.alias for d in devices} == {"pixel", "flip"}
    assert next(d for d in devices if d.alias == "flip").trusted is False


@pytest.mark.parametrize("damage", [
    {"version": 2},
    {"reason": "made-up"},
    {"publishedAt": time.time() - 3600},
    # From the future is as untrustworthy as stale, and sharper: it would never
    # expire.
    {"publishedAt": time.time() + 600},
    {"devices": "not a list"},
    {"devices": [{"alias": "AA:BB:CC:DD:EE:FF", "name": "x", "kind": "input",
                  "trusted": True, "connected": True}]},
    {"devices": [{"alias": "pixel", "name": "x", "kind": "input", "trusted": True, "connected": True},
                 {"alias": "pixel", "name": "y", "kind": "output", "trusted": True, "connected": True}]},
])
def test_a_document_that_cannot_be_trusted_reads_as_no_devices_and_unavailable(backend, damage):
    backend.state_file.write_text(json.dumps(document(**damage)), encoding="utf-8")
    assert backend.inventory(_cancel()) == []
    status = backend.call("status", {}, _cancel())
    assert status["available"] is False
    assert status["reason"] == "unavailable"
    assert status["devices"] == []


def test_a_missing_document_is_do_not_know_rather_than_no_devices(backend):
    backend.state_file.unlink()
    assert backend.inventory(_cancel()) == []
    assert backend.call("status", {}, _cancel())["reason"] == "unavailable"


def test_the_status_keeps_the_switch_backends_own_blocks_verbatim(backend):
    status = backend.call("status", {}, _cancel())
    assert status["switch"] == {"supported": True, "output": "speaker"}
    assert status["mute"] == {"supported": True, "muted": False}
    # ...and replaces exactly the four fields WSN-024 owns, plus the block it adds.
    assert status["available"] is True
    assert status["reason"] == "ready"
    assert [d["alias"] for d in status["devices"]] == ["pixel", "flip"]
    assert status["route"] == {"input": "pixel", "output": None}
    assert status["pairing"]["active"] is False


def test_a_route_naming_a_device_that_is_gone_is_dropped(backend):
    backend.state_file.write_text(
        json.dumps(document(route={"input": "vanished", "output": "flip"})), encoding="utf-8")
    assert backend.call("status", {}, _cancel())["route"] == {"input": None, "output": "flip"}


def test_the_passkey_survives_its_leading_zero(backend):
    backend.state_file.write_text(json.dumps(document(pairing={
        "active": True, "discoverable": True, "passkey": "012345",
        "device": "pixel", "endsInSeconds": 45})), encoding="utf-8")
    pairing = backend.call("status", {}, _cancel())["pairing"]
    assert pairing["passkey"] == "012345"
    assert pairing["device"] == "pixel" and pairing["endsInSeconds"] == 45


@pytest.mark.parametrize("passkey", [12345, "12a", "", "1" * 17, None])
def test_a_passkey_that_is_not_digits_is_dropped_rather_than_shown(backend, passkey):
    backend.state_file.write_text(json.dumps(document(pairing={
        "active": True, "discoverable": True, "passkey": passkey,
        "device": "pixel", "endsInSeconds": 45})), encoding="utf-8")
    assert backend.call("status", {}, _cancel())["pairing"]["passkey"] is None


def test_a_verb_becomes_a_request_file_scoped_to_the_documents_epoch(backend):
    result = backend.call("trust", {"alias": "flip", "trusted": True}, _cancel())
    assert result["accepted"] is True
    written = spooled(backend)
    assert len(written) == 1
    assert written[0]["event"] == {"kind": "trust", "alias": "flip", "trusted": True}
    assert written[0]["generation"] == 3
    assert written[0]["seq"] == result["seq"]


# THE DEFECT THIS PREVENTS. With one mailbox file the second write destroyed the
# first, the applier advanced its high-water mark past both sequences, and
# `request_landed` then reported the destroyed request as landed -- so a
# revocation the person asked for and was told had happened simply did not.
def test_two_requests_in_a_burst_both_survive_to_the_applier(backend):
    first = backend.call("forget", {"alias": "pixel"}, _cancel())
    second = backend.call("trust", {"alias": "flip", "trusted": True}, _cancel())
    written = spooled(backend)
    assert [item["seq"] for item in written] == [first["seq"], second["seq"]]
    assert [item["event"]["kind"] for item in written] == ["forget", "trust"]


def test_select_inputs_explicit_gate_does_not_travel(backend):
    """`explicit` is an IF-015 GATE, not a fact about the device.

    routing.validate_action has already refused a request that lacked it, so
    carrying it into the applier would be a second copy of a rule with no second
    reader -- and bluetooth_request would refuse the stray key anyway.
    """
    backend.call("select_input", {"alias": "pixel", "explicit": True}, _cancel())
    assert spooled(backend)[0]["event"] == {"kind": "select_input", "alias": "pixel"}


def test_an_unreadable_document_sends_the_request_unscoped(backend):
    backend.state_file.unlink()
    backend.call("cancel", {}, _cancel())
    assert "generation" not in spooled(backend)[0]


def test_two_requests_inside_one_microsecond_still_get_increasing_sequences(backend):
    backend.clock = lambda: 1_000_000_000
    first = backend.call("cancel", {}, _cancel())["seq"]
    second = backend.call("cancel", {}, _cancel())["seq"]
    assert second > first


# THE ONE THAT ACTUALLY BITES. A broker restart resets `_last_seq` to -1; a wall
# clock stepped backwards in between -- NTP correcting a dead RTC is how that
# happens here -- then mints a sequence BELOW the applier's persisted mark, and
# every request is discarded as old while `_submit` goes on returning accepted.
def test_a_restarted_broker_with_a_backwards_clock_still_outranks_the_applier(backend):
    # The document's mark is 100; a clock this far back mints a sequence of 1.
    backend.clock = lambda: 1_000_000_000
    fresh = backend.call("cancel", {}, _cancel())
    assert fresh["seq"] > 100
    assert spooled(backend)[0]["seq"] > 100


def test_a_missing_applier_refuses_the_verb_rather_than_writing_a_request(backend):
    backend.applier_path.unlink()
    from audio_router import BrokerError
    with pytest.raises(BrokerError) as raised:
        backend.call("pair", {"alias": "flip"}, _cancel())
    assert raised.value.code == "backend_unavailable"
    assert spooled(backend) == []


def test_switch_verbs_are_delegated_untouched(backend):
    backend.call("set_output", {"output": "headset"}, _cancel())
    assert ("set_output", {"output": "headset"}) in backend.switch_fake.calls
    assert spooled(backend) == []


# THE BUG THIS PREVENTS: two appliers, two high-water marks, one microsecond
# clock. Asking the wrong one is a coin toss between replaying a `forget` that
# already happened and dropping a `pair` that never did.
def test_request_landed_asks_the_applier_that_minted_the_sequence(backend):
    device = backend.call("request_landed", {"seq": 50, "method": "pair"}, _cancel())
    assert device == {"accepted": True}   # 50 <= the document's mark of 100
    assert backend.call("request_landed", {"seq": 500, "method": "pair"}, _cancel()) == {"accepted": False}
    switch = backend.call("request_landed", {"seq": 500, "method": "set_output"}, _cancel())
    assert switch.get("from") == "switch"
    # An older broker that sends no method at all is answered by the switch,
    # which is where every sequence came from before this backend existed.
    assert backend.call("request_landed", {"seq": 500}, _cancel()).get("from") == "switch"


def test_a_device_request_whose_outcome_cannot_be_seen_reports_landed(backend):
    """Between doing a destructive thing twice and possibly not doing it once,
    not doing it is the recoverable failure: the person taps again."""
    backend.state_file.unlink()
    assert backend.call("request_landed", {"seq": 5, "method": "forget"}, _cancel()) == {"accepted": True}


# ------------------------------------------------------------------- the policy


def test_trust_may_name_a_device_the_panel_does_not_trust_yet():
    """Otherwise the verb could only ever be used where it has nothing to do."""
    devices = [routing.Device(alias="flip", kind="output", trusted=False)]
    routing.validate_inventory_action("trust", {"alias": "flip"}, devices)
    routing.validate_inventory_action("pair", {"alias": "flip"}, devices)
    for method in ("connect", "disconnect", "forget", "select_output"):
        with pytest.raises(routing.PolicyError):
            routing.validate_inventory_action(method, {"alias": "flip"}, devices)


def test_the_new_verbs_are_validated_and_the_switch_gate_still_refuses_them():
    routing.validate_action("trust", {"alias": "flip", "trusted": True})
    routing.validate_action("set_discoverable", {"enabled": True})
    for params in ({"alias": "flip"}, {"alias": "flip", "trusted": "yes"}):
        with pytest.raises(routing.PolicyError):
            routing.validate_action("trust", params)
    with pytest.raises(routing.PolicyError):
        routing.validate_action("set_discoverable", {"enabled": 1})
    # A panel without the applier keeps the old gate, so a pairing request is
    # refused before any backend can observe it.
    assert routing.switch_only_authorization("trust", {}) is False
    assert routing.switch_only_authorization("set_discoverable", {}) is False
    assert routing.device_authorization("trust", {}) is True
    assert routing.device_authorization("set_output", {}) is True
    assert routing.device_authorization("nonsense", {}) is False


# ------------------------------------------------------------------ the applier


@pytest.fixture()
def applier(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("wall_bluetooth_device", APPLIER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setattr(module, "STATE_DIR", run)
    for name in ("STATE_PATH", "ROUTE_PATH", "PAIRING_PATH", "PASSKEY_PATH",
                 "MARK_PATH", "DISCOVERY_PID_PATH"):
        monkeypatch.setattr(module, name, run / (name.lower().replace("_path", "") + ".json"))
    monkeypatch.setattr(module, "adapter", lambda: ("ready", True, False, False))
    module.devices = [
        {"address": "AA:BB:CC:DD:EE:01", "name": "Pixel", "paired": True, "trusted": True,
         "connected": True, "uuids": ["0000110a-0000-1000-8000-00805f9b34fb"], "battery": 80},
    ]
    # KEPT BEFORE IT IS REPLACED. Most tests want the fake -- they are about the
    # drain, not about bluetoothctl -- but the deadline tests are about the real
    # loop, and asserting against the fake would prove nothing at all.
    module.real_inventory = module.inventory
    monkeypatch.setattr(module, "inventory", lambda deadline=None: list(module.devices))
    module.performed = []
    monkeypatch.setattr(module, "perform", lambda event, aliases: (
        module.performed.append(event) or True))
    module.spool = tmp_path / "spool"
    module.spool.mkdir()
    return module


def test_the_published_document_carries_no_hardware_address(applier):
    rows, aliases = applier.publish()
    text = applier.STATE_PATH.read_text(encoding="utf-8")
    # `aliasesByAddress` is deliberately in this file, so the addresses ARE
    # present -- see publish(). What must never appear is an address inside the
    # fields the broker reads.
    document = json.loads(text)
    assert "AA:BB:CC:DD:EE:01" not in json.dumps(document["devices"])
    assert "AA:BB:CC:DD:EE:01" not in json.dumps(document["route"])
    assert "AA:BB:CC:DD:EE:01" not in json.dumps(document["pairing"])
    assert rows[0]["alias"] == "pixel" and rows[0]["kind"] == "input"
    assert aliases["AA:BB:CC:DD:EE:01"] == "pixel"


# BLUEZ NAMES AN UNRESOLVED DEVICE AFTER ITS OWN ADDRESS, and `name` goes into
# the document beside the alias we were so careful about. The broker's validator
# then rejects the WHOLE document, so one unresolved device made every other
# device's row vanish.
def test_a_device_named_after_its_address_publishes_no_address_and_costs_nobody_their_row(applier):
    applier.devices = [
        {"address": "AA:BB:CC:DD:EE:01", "name": "Pixel", "paired": True, "trusted": True,
         "connected": True, "uuids": [], "battery": None},
        {"address": "AA:BB:CC:DD:EE:0F", "name": "AA:BB:CC:DD:EE:0F", "paired": False,
         "trusted": False, "connected": False, "uuids": [], "battery": None},
    ]
    rows, _aliases = applier.publish()
    assert len(rows) == 2, "the unresolved device must not cost the other one its row"
    for row in rows:
        assert not routing.HARDWARE_ADDRESS.search(row["name"]), row
        assert not routing.HARDWARE_ADDRESS.search(row["alias"]), row
    # ...and the document the broker reads survives its own validator.
    devices = routed_backend.RoutedDeviceBackend._devices(json.loads(
        applier.STATE_PATH.read_text(encoding="utf-8"))["devices"])
    assert devices is not None and len(devices) == 2


# A publish that cannot finish inside its budget says "do not know" rather than
# presenting the devices it managed to read as the whole list.
def test_a_publish_that_runs_out_of_budget_reports_do_not_know(applier, monkeypatch):
    monkeypatch.setattr(applier, "inventory", lambda deadline=None: None)
    rows, _aliases = applier.publish()
    assert rows == []
    assert json.loads(applier.STATE_PATH.read_text(encoding="utf-8"))["reason"] == "unavailable"


def test_a_replayed_request_is_performed_once(applier):
    bluetooth_request.write(500, {"kind": "cancel"}, applier.spool, generation=0)
    assert applier.apply_request(applier.spool) == 0
    assert len(applier.performed) == 1
    # The drain CONSUMES the file, so an ordinary second fire finds nothing.
    assert applier.apply_request(applier.spool) == 0
    assert len(applier.performed) == 1
    # ...and a request the broker re-spools under a sequence it has already used
    # -- the belt to the consumed file's braces -- is still refused.
    bluetooth_request.write(500, {"kind": "cancel"}, applier.spool, generation=0)
    assert applier.apply_request(applier.spool) == 0
    assert len(applier.performed) == 1, "a replayed sequence must not act twice"
    # ...and a NEWER one still gets through.
    bluetooth_request.write(501, {"kind": "cancel"}, applier.spool, generation=0)
    assert applier.apply_request(applier.spool) == 0
    assert len(applier.performed) == 2


# THE WHOLE POINT OF THE SPOOL. Two taps before the path unit fires used to leave
# the first request overwritten and reported as landed.
def test_a_burst_is_drained_in_order_and_nothing_is_lost(applier):
    for seq, kind in ((10, "cancel"), (20, "cancel"), (30, "cancel")):
        bluetooth_request.write(seq, {"kind": kind}, applier.spool, generation=0)
    assert applier.apply_request(applier.spool) == 0
    assert len(applier.performed) == 3
    assert bluetooth_request.pending(applier.spool) == []
    _generation, mark = applier.mark()
    assert mark == 30


def test_a_drain_is_bounded(applier):
    for seq in range(1, bluetooth_request.MAX_DRAIN + 5):
        bluetooth_request.write(seq, {"kind": "cancel"}, applier.spool, generation=0)
    applier.apply_request(applier.spool)
    assert len(applier.performed) == bluetooth_request.MAX_DRAIN
    # The remainder is still spooled, and the next fire takes it: a bound is not
    # a discard.
    assert len(bluetooth_request.pending(applier.spool)) == 4


def test_a_request_from_a_previous_life_of_the_applier_is_ignored(applier):
    bluetooth_request.write(9, {"kind": "cancel"}, applier.spool, generation=7)
    applier.apply_request(applier.spool)
    assert applier.performed == [], "the applier is at epoch 0, not 7"
    assert bluetooth_request.pending(applier.spool) == [], "and it is consumed, not left to retry"


def test_an_unscoped_request_is_still_performed(applier):
    """What an older broker sends, and what a broker that could not read the
    document sends. Refusing it would take device management down over a file."""
    bluetooth_request.write(9, {"kind": "cancel"}, applier.spool)
    applier.apply_request(applier.spool)
    assert applier.performed == [{"kind": "cancel"}]


@pytest.mark.parametrize("payload", [
    {"version": 2, "seq": 1, "event": {"kind": "cancel"}},
    {"version": 1, "seq": -1, "event": {"kind": "cancel"}},
    {"version": 1, "seq": True, "event": {"kind": "cancel"}},
    {"version": 1, "seq": 1},
    {"version": 1, "seq": 1, "event": "cancel"},
])
def test_a_request_the_applier_cannot_read_is_ignored_and_never_fatal(applier, payload):
    (applier.spool / bluetooth_request.request_name(7)).write_text(
        json.dumps(payload), encoding="utf-8")
    assert applier.apply_request(applier.spool) == 0
    assert applier.performed == []
    # CONSUMED, not left to be re-read forever: a request nothing can understand
    # would otherwise keep the spool non-empty, and DirectoryNotEmpty would
    # restart the applier against it on every settle.
    assert bluetooth_request.pending(applier.spool) == []
    # It still republished, because the document going stale is what tells the
    # panel something is wrong -- and nothing is.
    assert applier.STATE_PATH.exists()


def test_the_mark_moves_before_the_action(applier, monkeypatch):
    """A process that dies mid-pairing must not have its request replayed.

    A half-finished bond re-attempted is the sticky case wall-bluetooth-pairing
    documents; the person at the glass can simply tap again.
    """
    def explode(event, aliases):
        raise RuntimeError("the radio went away")
    monkeypatch.setattr(applier, "perform", explode)
    bluetooth_request.write(42, {"kind": "pair", "alias": "pixel"}, applier.spool, generation=0)
    assert applier.apply_request(applier.spool) == 0
    _generation, seq = applier.mark()
    assert seq == 42
    assert bluetooth_request.pending(applier.spool) == [], "and the file is gone too"


# ---------------------------------------------- terra round 2, the three fixes


# R2/1. Stopping at MAX_DRAIN leaves the directory non-empty, and whether
# DirectoryNotEmpty= fires again for a directory that was ALREADY non-empty when
# the unit started is a systemd detail a revocation should not be bet on. The
# observe timer drains too, so ten seconds is the worst case either way.
def test_the_observe_verb_drains_a_spool_the_bound_left_behind(applier):
    for seq in range(1, bluetooth_request.MAX_DRAIN + 3):
        bluetooth_request.write(seq, {"kind": "cancel"}, applier.spool, generation=0)
    applier.main(["observe", str(applier.spool)])
    assert len(applier.performed) == bluetooth_request.MAX_DRAIN
    applier.main(["observe", str(applier.spool)])
    assert len(applier.performed) == bluetooth_request.MAX_DRAIN + 2
    assert bluetooth_request.pending(applier.spool) == []


def test_observe_and_apply_request_are_the_same_drain(applier):
    bluetooth_request.write(5, {"kind": "cancel"}, applier.spool, generation=0)
    applier.main(["observe", str(applier.spool)])
    assert applier.performed == [{"kind": "cancel"}]
    assert bluetooth_request.pending(applier.spool) == []


# R2/2. Rename A while a NEW device with A's old name appears: without the
# reservation, A's alias falls free in the same pass and B takes it, so a row
# painted before the rename resolves to B.
def test_an_alias_freed_by_a_rename_is_not_handed_straight_to_another_device():
    before = bluetooth_state.assign_aliases([("AA:BB:CC:DD:EE:01", "Pixel")])
    assert before["AA:BB:CC:DD:EE:01"] == "pixel"
    after = bluetooth_state.assign_aliases(
        [("AA:BB:CC:DD:EE:01", "Other"), ("AA:BB:CC:DD:EE:02", "Pixel")], before)
    assert after["AA:BB:CC:DD:EE:01"] == "other"
    assert after["AA:BB:CC:DD:EE:02"] != "pixel", "the renamed device's alias was reserved"
    assert len(set(after.values())) == 2


# ...but only while the device is still here. Reserving a departed device's
# alias would make the map grow for the life of the boot for no benefit: nothing
# can be painted for a device the document no longer carries.
def test_a_departed_devices_alias_is_reusable():
    before = {"AA:BB:CC:DD:EE:09": "pixel"}
    after = bluetooth_state.assign_aliases([("AA:BB:CC:DD:EE:01", "Pixel")], before)
    assert after["AA:BB:CC:DD:EE:01"] == "pixel"


# R2/3. The budget has to cover the LISTING and the LAST PROBE, not just the gaps
# between probes: outside it, the worst case was the control timeout plus the
# whole budget plus one probe, past the thirty seconds the broker gives the
# document before it stops believing it.
def test_a_listing_that_runs_long_is_do_not_know_rather_than_an_empty_room(applier, monkeypatch):
    monkeypatch.setattr(applier, "known_addresses", lambda deadline=None: None)
    assert applier.real_inventory(applier.time.monotonic() + 5) is None


def test_the_deadline_is_checked_after_the_last_probe_too(applier, monkeypatch):
    # One device, and the probe itself consumes the whole budget. Before the
    # post-loop check this returned a complete-looking list with that device
    # silently missing from it.
    monkeypatch.setattr(applier, "known_addresses", lambda deadline=None: ["AA:BB:CC:DD:EE:01"])
    monkeypatch.setattr(applier, "device_info", lambda address: None)
    deadline = applier.time.monotonic() - 1
    assert applier.real_inventory(deadline) is None


# terra round 3. Mapping every OSError to contention meant a /run that could not
# be written looked exactly like a drain already in progress: the caller exited
# 0, the spool was never processed, and the unit reported success forever.
def test_a_lock_that_cannot_be_opened_fails_loudly_instead_of_looking_contended(applier, monkeypatch):
    bluetooth_request.write(5, {"kind": "cancel"}, applier.spool, generation=0)
    monkeypatch.setattr(applier, "drain_lock", lambda: (_ for _ in ()).throw(OSError("read-only")))
    assert applier.apply_request(applier.spool) == 1
    assert applier.performed == []
    # UNTOUCHED, so the next observe tick still has the work.
    assert len(bluetooth_request.pending(applier.spool)) == 1


def test_a_contended_lock_leaves_the_work_to_the_holder_and_is_not_a_failure(applier, monkeypatch):
    bluetooth_request.write(5, {"kind": "cancel"}, applier.spool, generation=0)
    monkeypatch.setattr(applier, "drain_lock", lambda: False)
    assert applier.apply_request(applier.spool) == 0
    assert applier.performed == []
    assert len(bluetooth_request.pending(applier.spool)) == 1
