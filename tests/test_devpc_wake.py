"""Tests for the dev-PC wake and readiness service.

Verifies: SR-044 / LLR-966, LLR-967, LLR-968, LLR-969, LLR-974, LLR-978
Cases:    TC-988, TC-989, TC-990, TC-991, TC-992, TC-994, TC-995,
          TC-1002, TC-1003, TC-1010, TC-1013, TC-1014, TC-1016

Every test here is about a distinction that a previous draft of the design got
wrong, and each one is named after the distinction rather than the function, so
a failure says what broke rather than where.
"""
import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "stack" / "devpc-wake" / "devpc_wake.py"


def _load():
    spec = importlib.util.spec_from_file_location("devpc_wake", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["devpc_wake"] = mod
    spec.loader.exec_module(mod)
    return mod


dw = _load()


class _Cfg:
    host = "dev-pc.invalid"
    port = 11434
    probe_timeout = 0.01
    base_url = "http://dev-pc.invalid:11434"
    target_model = "the-model"
    deadline_seconds = 90
    idle_sleep_seconds = 1800
    session_staleness_seconds = 120
    # The residency probe waits out a cold model load, so its timeout is an
    # order of magnitude above probe_timeout in production. Here it is small
    # because every probe is stubbed.
    ready_probe_timeout = 0.01
    ready_ttl_seconds = 120


class _Provider(dw.WakeProvider):
    name = "test"

    def __init__(self, fail=False):
        self.fired = 0
        self._fail = fail

    def fire(self):
        self.fired += 1
        if self._fail:
            raise OSError("actuation failed")


@pytest.fixture
def svc(monkeypatch):
    clock = {"now": 1000.0}
    provider = _Provider()
    s = dw.WakeService(_Cfg(), provider, clock=lambda: clock["now"])
    s._test_clock = clock
    s._test_provider = provider
    return s


def _probes(monkeypatch, reachable, loaded, resident=None):
    """Three layers now, not two.

    `loaded` is the cheap LISTING and can no longer produce `ready` on its own -
    both Ollama and llama-server list a model that is configured and not
    resident. `resident` is the one-token completion that actually proves it.
    """
    monkeypatch.setattr(dw, "reachable", lambda *a, **k: reachable)
    monkeypatch.setattr(dw, "model_loaded", lambda *a, **k: loaded)
    monkeypatch.setattr(dw, "model_resident", lambda *a, **k: resident)


def _deep_now(svc, monkeypatch):
    """Run the background residency probe synchronously, for determinism."""
    monkeypatch.setattr(dw.threading, "Thread",
                        lambda target, daemon=None: type(
                            "T", (), {"start": lambda _self: target()})())
    return svc.state(deep=True)


# --- TC-988 ---------------------------------------------------------------
def test_each_state_has_its_own_determining_condition_sr044(svc, monkeypatch):
    _probes(monkeypatch, False, None)
    assert svc.state()["state"] == "off"
    _probes(monkeypatch, True, False)
    assert svc.state()["state"] == "up"
    # LISTED IS NOT READY. This assertion used to read `== "ready"`, and it was
    # the defect written down as a test: a listing proves the model is
    # CONFIGURED, and the caller still paid a full load on its first request.
    _probes(monkeypatch, True, True, resident=None)
    assert svc.state()["state"] == "up"
    assert "residency not verified" in svc.state()["reason"]

    # A verified completion is what promotes it, and only on a DEEP ask.
    _probes(monkeypatch, True, True, resident=True)
    _deep_now(svc, monkeypatch)
    assert svc.state()["state"] == "ready"
    assert "completion verified" in svc.state()["reason"]


# --- TC-989 ---------------------------------------------------------------
def test_the_two_probe_layers_are_not_conflated_sr044(svc, monkeypatch):
    """A reachability timeout is off. A LISTING timeout is up. Opposite meanings."""
    _probes(monkeypatch, False, None)
    assert svc.state()["state"] == "off", "nothing answered: must not be up"

    # Host answers; the listing itself does not. The box is demonstrably alive.
    _probes(monkeypatch, True, None)
    assert svc.state()["state"] == "up"

    # Host answers, listing answers, model absent. Still not servable.
    _probes(monkeypatch, True, False)
    assert svc.state()["state"] == "up"


# --- TC-990 ---------------------------------------------------------------
def test_waking_expires_to_failed_at_the_deadline_sr044(svc, monkeypatch):
    _probes(monkeypatch, False, None)
    svc.request_wake()
    assert svc.state()["state"] == "waking"

    svc._test_clock["now"] += _Cfg.deadline_seconds - 1
    assert svc.state()["state"] == "waking"

    svc._test_clock["now"] += 2
    assert svc.state()["state"] == "failed"
    # And it does not linger as waking on the next read either.
    assert svc.state()["state"] == "off"


# --- TC-991 ---------------------------------------------------------------
def test_the_wake_target_is_the_configured_physical_mac_sr044():
    p = dw.MagicPacketProvider("2C-F0-5D-3F-8D-26")
    assert p._mac == bytes.fromhex("2CF05D3F8D26")
    # Separators must not change the target.
    assert dw.MagicPacketProvider("2c:f0:5d:3f:8d:26")._mac == p._mac
    for bad in ("", "not-a-mac", "2CF05D3F8D", "2CF05D3F8D2600"):
        with pytest.raises(ValueError):
            dw.MagicPacketProvider(bad)


# --- TC-992 ---------------------------------------------------------------
def test_a_second_provider_swaps_without_touching_the_state_machine_sr044(monkeypatch):
    """The magic-packet path may never work on this NIC. Swapping must be free."""
    calls = []

    class Alt(dw.WakeProvider):
        name = "alternate"

        def fire(self):
            calls.append(1)

    clock = {"now": 500.0}
    s = dw.WakeService(_Cfg(), Alt(), clock=lambda: clock["now"])
    _probes(monkeypatch, False, None)
    s.request_wake()
    assert calls == [1]
    assert s.state()["state"] == "waking"
    with pytest.raises(ValueError):
        dw.CommandProvider([])


# --- TC-994 + TC-1003 -----------------------------------------------------
def test_one_wake_fires_per_flight_not_per_request_sr044(svc):
    a, b, c = svc.request_wake(), svc.request_wake(), svc.request_wake()
    assert a is b is c
    assert svc._test_provider.fired == 1, "a burst must not become a burst of packets"


def test_joiners_inherit_the_deadline_and_failure_clears_the_flight_sr044(svc):
    first = svc.request_wake()
    svc._test_clock["now"] += 30
    later = svc.request_wake()
    assert later is first
    assert later.deadline == first.deadline, "a joiner must not refresh the deadline"

    failing = dw.WakeService(_Cfg(), _Provider(fail=True),
                             clock=lambda: 1000.0)
    flight = failing.request_wake()
    assert flight.outcome == dw.State.FAILED
    assert flight.done.is_set()
    assert failing._flight is None, "a dead flight must not poison later attempts"
    again = failing.request_wake()
    assert again is not flight


# --- TC-1002 --------------------------------------------------------------
def test_state_precedence_when_layers_disagree_sr044(svc, monkeypatch):
    """deadline > reachability > readiness, in that order."""
    # A stale 'ready' must not mask an unreachable host.
    _probes(monkeypatch, False, True)
    assert svc.state()["state"] == "off"

    # An expired deadline outranks a probe that would say ready.
    svc.request_wake()
    svc._test_clock["now"] += _Cfg.deadline_seconds + 1
    _probes(monkeypatch, True, True)
    assert svc.state()["state"] == "failed", "an expired wake must not be masked"


# --- TC-1010 --------------------------------------------------------------
def test_arrivals_interleaved_with_teardown_sr044(svc, monkeypatch):
    """No arrival may see a removed flight beside a stale not-ready state."""
    _probes(monkeypatch, False, None)
    flight = svc.request_wake()
    svc._test_clock["now"] += _Cfg.deadline_seconds + 1

    seen = []

    def reader():
        seen.append(svc.state()["state"])

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive(), "a concurrent read deadlocked"

    # Exactly one observer may resolve the expired flight to failed; the rest
    # see the post-teardown truth. Neither is a lie, and no thread hangs.
    assert set(seen) <= {"failed", "off"}
    assert seen.count("failed") <= 1, "an expired flight must resolve once"
    assert flight.done.is_set()


# --- TC-1013 --------------------------------------------------------------
def test_no_io_or_notification_occurs_under_the_lock_sr044(monkeypatch):
    """Firing under the lock would deadlock a continuation that re-enters."""
    clock = {"now": 10.0}
    holder = {}

    class Reentrant(dw.WakeProvider):
        name = "reentrant"

        def fire(self):
            # A continuation that reads state. If this ran under the lock, the
            # acquire below would never return.
            holder["locked_during_fire"] = svc_ref[0]._lock.locked()

    svc_ref = [None]
    svc_ref[0] = dw.WakeService(_Cfg(), Reentrant(), clock=lambda: clock["now"])
    _probes(monkeypatch, False, None)
    svc_ref[0].request_wake()
    assert holder["locked_during_fire"] is False, "wake I/O ran while holding the lock"


# --- TC-995 + TC-1014 + TC-1016 -------------------------------------------
def test_the_idle_timer_is_inhibited_while_a_session_is_attached_sr044(svc):
    fresh = svc._test_clock["now"]
    # IDLE MUST GENUINELY HAVE ELAPSED, or every assertion below passes for the
    # wrong reason - the idle gate short-circuits before the session is read,
    # and a broken session check would still look green.
    long_ago = fresh - _Cfg.idle_sleep_seconds - 1
    attached = {"attached": True, "observed_at": fresh}
    detached = {"attached": False, "observed_at": fresh}
    assert svc.sleep_verdict(long_ago, attached)["permit"] is False
    assert svc.sleep_verdict(long_ago, detached)["permit"] is True
    # Idle not elapsed beats everything else, including a clean detached reading.
    assert svc.sleep_verdict(fresh, detached)["permit"] is False


@pytest.mark.parametrize("session, why", [
    (None, "absent"),
    ({"attached": False, "observed_at": 0.0}, "stale"),
    ({"observed_at": 1000.0}, "malformed - no attached key"),
    ({"attached": "false", "observed_at": 1000.0}, "malformed - string not bool"),
    ({"attached": True, "observed_at": 1000.0}, "genuinely attached"),
])
def test_an_unavailable_session_signal_is_treated_as_attached_sr044(svc, session, why):
    """Fail closed to ATTACHED. A false detached suspends a machine in use."""
    long_ago = svc._test_clock["now"] - _Cfg.idle_sleep_seconds - 1
    # Idle is deliberately elapsed so the SESSION reading is what decides. With
    # a recent request the idle gate answers first and this would prove nothing.
    assert svc.sleep_verdict(long_ago, session)["permit"] is False, why


def test_the_hub_publishes_a_verdict_and_never_actuates_sr044(svc):
    """IF-026: the hub decides; the dev PC polls and sleeps itself."""
    long_ago = svc._test_clock["now"] - _Cfg.idle_sleep_seconds - 1
    verdict = svc.sleep_verdict(long_ago, {"attached": False, "observed_at": svc._test_clock["now"]})
    assert verdict["permit"] is True, "the permitting path must be exercised, not just the deny path"
    assert set(verdict) == {"permit", "reason", "observed_at"}
    assert not hasattr(svc, "sleep_now"), "the hub must expose no sleep actuation"
    assert not hasattr(svc, "suspend"), "the hub must expose no sleep actuation"


# --- TC-1028 ---------------------------------------------------------------
# The unit-installation half of SR-044, which had no test because nothing
# installed the unit. Until 2026-09-19 firstboot.sh never mentioned
# devpc-wake at all: the code was deployed to /opt/homehub/stack/devpc-wake
# and was unreachable from systemd, so the S3/S5 physical test could not be
# run without hand-writing a unit file on the box.
_FIRSTBOOT = Path(__file__).resolve().parents[1] / "stack" / "autoinstall" / "firstboot.sh"


def _devpc_block():
    """The devpc-wake stanza ONLY.

    Bounded by its own closing `fi` rather than by a character count. A
    fixed-size window overran into the next section of firstboot.sh and made
    these tests read text they do not own - which would have let a neighbouring
    edit turn one of them green or red for no reason.
    """
    text = _FIRSTBOOT.read_text(encoding="utf-8")
    assert "devpc-wake" in text, (
        "firstboot.sh must handle the devpc-wake unit; a payload nothing "
        "installs is a service that cannot even be tested by hand")
    start = text.index("# ── devpc-wake:")
    end = text.index('log "devpc-wake: no payload at', start)
    return text[start:end]


def _devpc_live():
    """The same stanza with comment lines removed.

    Ordering assertions MUST run against this and not the raw block. The first
    draft of the install-order test compared positions in the commented text
    and failed, because the banner above the code says "enables it only on
    DEVPC_WAKE_ENABLED=true" hundreds of characters before the install line.
    It was asserting against prose describing the behaviour instead of against
    the behaviour - the same shape of mistake as a test that reads back the
    file its value was invented in.
    """
    return "\n".join(l for l in _devpc_block().splitlines()
                     if not l.lstrip().startswith("#"))


def test_the_wake_unit_is_installed_unconditionally_sr044():
    """INSTALL ALWAYS, ENABLE CONDITIONALLY. The install must not be gated on
    the knob: the whole point is that the Owner can run the physical wake test
    with one `systemctl start` while the service still stays disabled across
    reboots until the packet is proven to work."""
    block = _devpc_live()
    install_at = block.index("install -m 0644")
    knob_at = block.index("DEVPC_WAKE_ENABLED")
    assert install_at < knob_at, (
        "the unit must be installed BEFORE the knob is consulted, so a box "
        "with the knob false still has a unit that can be started by hand")
    assert "homehub-devpc-wake.service" in block
    assert "systemctl daemon-reload" in block, (
        "a freshly installed unit systemd has not re-read cannot be started")


def test_the_knob_disables_rather_than_merely_declining_to_enable_sr044():
    """firstboot re-runs. A box whose knob was true and is now false would
    otherwise keep the service enabled for ever - the knob would appear to
    turn it off and would not. Same defect the rustdesk listener block fixed."""
    block = _devpc_block()
    assert "systemctl disable homehub-devpc-wake.service" in block
    assert "systemctl stop    homehub-devpc-wake.service" in block, (
        "disable alone leaves a running service up until the next boot, which "
        "is exactly the window somebody flipping this to false is closing")


def test_enabling_is_refused_when_nothing_can_actuate_a_wake_sr044():
    """devpc_wake_service.py already exits when neither knob is set, but a
    unit that enables and then crash-loops on RestartSec is a worse way to
    learn it: the journal fills and uptime-kuma goes red three levels from the
    cause."""
    block = _devpc_block()
    assert "DEVPC_WAKE_MAC" in block and "DEVPC_WAKE_COMMAND" in block
    assert "INSTALLED but left disabled" in block


def test_the_carriage_return_guard_is_present_sr044():
    """A CRLF-saved .env must not smuggle a carriage return into the string
    comparison - the same guard the rustdesk knob needed."""
    block = _devpc_block()
    # A RAW string. Without the r-prefix Python reads \015 as an octal escape
    # for a carriage return, so the assertion searched the shell script for a
    # literal CR and would have passed only by accident.
    assert block.count(r"tr -d '\015'") >= 3, (
        "every value read out of .env here needs the guard, not just the first")


# --- TC-1045: listed is not resident ---------------------------------------
def test_a_listed_model_is_not_ready_until_a_completion_proves_it_sr044(
        svc, monkeypatch):
    """THE DEFECT THIS REPLACED, STATED AS A TEST.

    Both Ollama and llama-server list a model that is configured and NOT
    resident in VRAM. `ready` on a listing meant the first request after a wake
    still paid a full model load - `up` wearing `ready`'s name, which is the one
    confusion this whole service exists to prevent.
    """
    _probes(monkeypatch, True, True, resident=None)
    doc = svc.state(deep=False)
    assert doc["state"] == "up", "a listing must never be enough for ready"
    assert "residency not verified" in doc["reason"]


# --- TC-1046: the poller must not become a keep-alive ----------------------
def test_the_poller_never_fires_a_completion_sr044(svc, monkeypatch):
    """A COMPLETION ON THE POLLER'S BEAT WOULD PIN THE WEIGHTS FOR EVER.

    The service polls every DEVPC_WAKE_POLL_SECONDS to publish telemetry. If
    that beat verified residency, the model could never be evicted and
    OLLAMA_KEEP_ALIVE - whose entire job is to hand the card back when the Owner
    wants to play a game - would be defeated by the thing watching it.

    So the cheap path must fire NOTHING, and this counts.
    """
    calls = []
    monkeypatch.setattr(dw, "reachable", lambda *a, **k: True)
    monkeypatch.setattr(dw, "model_loaded", lambda *a, **k: True)
    monkeypatch.setattr(dw, "model_resident",
                        lambda *a, **k: calls.append(1) or True)

    for _ in range(25):
        svc.state(deep=False)
    assert calls == [], \
        "the poller fired %d completion(s); on a 15s beat that is a permanent " \
        "VRAM lease" % len(calls)


# --- TC-1047: the ask must not block on a cold load ------------------------
def test_a_deep_ask_returns_immediately_and_verifies_behind_it_sr044(
        svc, monkeypatch):
    """BLOCKING /state WOULD BREAK THE CONSUMER IT EXISTS FOR.

    The hold hook polls /state with a SHORT timeout while holding a request. A
    cold load is tens of seconds, so a synchronous probe would time the hook
    out - which it reads as `no-oracle` and REFUSES on, turning a sleeping box
    into a hard failure. The ask returns at once; the verdict lands behind it.
    """
    started = []
    monkeypatch.setattr(dw, "reachable", lambda *a, **k: True)
    monkeypatch.setattr(dw, "model_loaded", lambda *a, **k: True)

    def slow(*a, **k):
        started.append(1)
        raise AssertionError("must not run on the caller's thread")

    monkeypatch.setattr(dw, "model_resident", slow)
    spawned = []
    monkeypatch.setattr(dw.threading, "Thread",
                        lambda target, daemon=None: spawned.append(target) or
                        type("T", (), {"start": lambda _s: None})())

    doc = svc.state(deep=True)
    assert doc["state"] == "up"
    assert "verifying residency" in doc["reason"]
    assert spawned, "the probe must be handed to a background thread"
    assert started == [], "nothing may run on the caller's thread"


# --- TC-1047: single flight ------------------------------------------------
def test_residency_verification_is_single_flight_sr044(svc, monkeypatch):
    """Twenty asks while one probe is in flight must not be twenty loads."""
    monkeypatch.setattr(dw, "reachable", lambda *a, **k: True)
    monkeypatch.setattr(dw, "model_loaded", lambda *a, **k: True)
    monkeypatch.setattr(dw, "model_resident", lambda *a, **k: True)
    spawned = []
    monkeypatch.setattr(dw.threading, "Thread",
                        lambda target, daemon=None: spawned.append(target) or
                        type("T", (), {"start": lambda _s: None})())

    for _ in range(20):
        svc.state(deep=True)
    assert len(spawned) == 1, "expected one in-flight probe, got %d" % len(spawned)


# --- TC-1046: the verdict expires -----------------------------------------
def test_a_verified_residency_expires_so_an_evicted_model_is_noticed_sr044(
        svc, monkeypatch):
    """The keep-alive can evict the model behind our back. A cached `ready`
    that never expired would go on asserting a model that is gone."""
    _probes(monkeypatch, True, True, resident=True)
    _deep_now(svc, monkeypatch)
    assert svc.state()["state"] == "ready"

    svc._test_clock["now"] += 121        # past ready_ttl_seconds
    assert svc.state()["state"] == "up", \
        "a stale verification must not keep claiming ready"
