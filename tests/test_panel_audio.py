"""SR-023/LLR-007: bounded policy, broker, telemetry and image carriage."""

import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import socket
import sys
import threading
import time

import pytest
import subprocess


ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "stack/panel-audio"
WALL = ROOT / "stack/autoinstall/wall"
sys.path.insert(0, str(AUDIO))
from routing import Device, PolicyError, choose_restored_route, validate_action
from visualizer import MAX_SAMPLES, VisualizerTelemetry, analyze_samples
from audio_router import (AudioBroker as RealAudioBroker, BoundedUnixServer,
                          BrokerError, UnavailableBackend, JS_SAFE_INTEGER,
                          MAX_REQUEST_BYTES)


def AudioBroker(*args, **kwargs):
    """White-box fakes stay in-process; production/default isolation is tested separately."""
    kwargs.setdefault("isolate_backend", False)
    return RealAudioBroker(*args, **kwargs)


class FakeBackend:
    def __init__(self): self.calls = []
    def call(self, method, params, cancel):
        self.calls.append((method, params)); return {"accepted": True}
    def inventory(self, cancel):
        return [Device("speaker", "output", True), Device("desktop-in", "input", True)]


class ForeverBackend:
    def call(self, method, params, cancel):
        while True:
            time.sleep(1)

    def inventory(self, cancel):
        return []


def request(method="status", params=None, generation=0, request_id="r1"):
    return (json.dumps({"id": request_id, "method": method, "params": params or {},
                        "generation": generation}) + "\n").encode()


def response(broker, raw): return json.loads(broker.handle(raw))


def test_trusted_output_restore_never_uses_untrusted_or_input_sr023():
    devices = [Device("desktop-in", "input", True), Device("speaker", "output", False),
               Device("wired", "output", True)]
    assert choose_restored_route(devices, "speaker", ["desktop-in", "wired"]) == "wired"
    assert choose_restored_route(devices, "speaker", []) is None


def test_input_selection_is_explicit_and_aliases_are_sanitized_sr023():
    with pytest.raises(PolicyError): validate_action("select_input", {"alias": "desktop-in"})
    validate_action("select_input", {"alias": "desktop-in", "explicit": True})
    with pytest.raises(PolicyError): validate_action("connect", {"alias": "AA:BB:CC:DD:EE:FF"})
    for address in ("aabbccddeeff", "aa-bb-cc-dd-ee-ff", "aa_bb_cc_dd_ee_ff", "aabb.ccdd.eeff"):
        with pytest.raises(PolicyError): validate_action("connect", {"alias": address})
        with pytest.raises(PolicyError):
            validate_action("pair", {"alias": "speaker", "confirmation": address})


def test_broker_dispatches_only_exact_bounded_current_generation_requests_sr023():
    backend = FakeBackend(); broker = AudioBroker(backend, generation=4, authorize=lambda _m, _p: True)
    ok = response(broker, request("connect", {"alias": "speaker"}, 4))
    assert ok["ok"] and ok["generation"] == 5
    assert backend.calls == [("connect", {"alias": "speaker"})]
    assert response(broker, request("shell", {}, 4))["error"]["code"] == "policy_refused"
    assert response(broker, request("status", {}, 3))["error"]["code"] == "stale_generation"
    assert response(broker, b"{" + b"x" * MAX_REQUEST_BYTES + b"}\n")["error"]["code"] == "request_too_large"
    assert response(broker, request("connect", {}, 4))["error"]["code"] == "policy_refused"


def test_broker_rejects_backend_identity_and_raw_audio_fields_sr023():
    class UnsafeBackend:
        def call(self, method, params, cancel): return {"address": "device-identity"}
        def inventory(self, cancel): return []
    refused = response(AudioBroker(UnsafeBackend()), request())
    assert refused["error"]["code"] == "unsafe_backend_result"
    class LeakingStatus:
        def call(self, method, params, cancel):
            return {"protocolVersion": 1, "available": True, "reason": "ready",
                    "devices": [{"alias": "speaker", "name": "AA:BB:CC:DD:EE:FF",
                                 "kind": "output", "trusted": True, "connected": True}],
                    "route": None, "visualizer": {"available": False}}
        def inventory(self, cancel): return []
    assert response(AudioBroker(LeakingStatus()), request())["error"]["code"] == "unsafe_backend_result"
    for address in ("aabbccddeeff", "aa-bb-cc-dd-ee-ff", "aa_bb_cc_dd_ee_ff", "aabb.ccdd.eeff"):
        class VariantStatus(LeakingStatus):
            def call(self, method, params, cancel):
                value = super().call(method, params, cancel)
                value["devices"][0]["name"] = address
                return value
        assert response(AudioBroker(VariantStatus()), request())["error"]["code"] == "unsafe_backend_result"


def test_broker_contains_backend_failures_and_rejects_non_json_results_sr023():
    class RaisingBackend:
        def call(self, method, params, cancel): raise RuntimeError("secret device path")
        def inventory(self, cancel): return []
    class TupleBackend:
        def call(self, method, params, cancel): return ("not", "json")
        def inventory(self, cancel): return []
    failed = response(AudioBroker(RaisingBackend()), request())
    assert failed["error"] == {"code": "backend_failure", "message": "audio backend failed"}
    assert "secret" not in json.dumps(failed)
    assert response(AudioBroker(TupleBackend()), request())["error"]["code"] == "unsafe_backend_result"
    assert response(AudioBroker(FakeBackend()), request(generation=False))["error"]["code"] == "bad_request"
    assert response(AudioBroker(FakeBackend()), request(generation=JS_SAFE_INTEGER + 1))["error"]["code"] == "bad_request"
    assert response(AudioBroker(FakeBackend()), request(request_id=-1))["error"]["code"] == "bad_request"


def test_mutations_require_authorization_and_matching_trusted_inventory_sr023():
    backend = FakeBackend()
    assert response(AudioBroker(backend), request("connect", {"alias": "speaker"}))["error"]["code"] == "authorization_required"
    allowed = AudioBroker(backend, authorize=lambda _m, _p: True)
    assert response(allowed, request("connect", {"alias": "missing"}))["error"]["code"] == "policy_refused"
    assert response(allowed, request("select_output", {"alias": "desktop-in"}))["error"]["code"] == "policy_refused"


def test_generation_serializes_concurrent_mutations_sr023():
    broker = AudioBroker(FakeBackend(), generation=7, authorize=lambda _m, _p: True)
    replies = []
    threads = [threading.Thread(target=lambda identifier=identifier: replies.append(response(
        broker, request("connect", {"alias": "speaker"}, 7, identifier))))
        for identifier in ("first", "second")]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sum(reply["ok"] for reply in replies) == 1
    assert {reply.get("error", {}).get("code") for reply in replies} == {None, "stale_generation"}
    assert broker.generation == 8


def test_completed_mutation_is_deduplicated_across_restart_sr023(tmp_path):
    state = tmp_path / "audio-state.json"
    first_backend = FakeBackend()
    first = AudioBroker(first_backend, generation=4, state_path=str(state),
                        authorize=lambda _m, _p: True)
    original = request("connect", {"alias": "speaker"}, 4, "durable-1")
    first_reply = response(first, original)
    assert first_reply["ok"] and first_reply["generation"] == 5

    second_backend = FakeBackend()
    restarted = AudioBroker(second_backend, state_path=str(state),
                            authorize=lambda _m, _p: True)
    assert response(restarted, original) == first_reply
    assert second_backend.calls == []


def test_uncertain_mutation_survives_restart_and_status_is_not_starved_sr023(tmp_path):
    state = tmp_path / "audio-state.json"
    entered = threading.Event(); cancelled = threading.Event()

    class HungBackend(FakeBackend):
        def call(self, method, params, cancel):
            if method == "status":
                return {"protocolVersion": 1, "available": False, "reason": "test",
                        "devices": [], "route": None, "visualizer": {"available": False}}
            entered.set()
            cancel.wait(2); cancelled.set()
            while True: time.sleep(0.1)

    broker = AudioBroker(HungBackend(), state_path=str(state),
                         authorize=lambda _m, _p: True, backend_timeout_seconds=0.1)
    replies = []
    mutation = threading.Thread(target=lambda: replies.append(response(
        broker, request("connect", {"alias": "speaker"}, 0, "uncertain"))))
    mutation.start(); assert entered.wait(1)
    started = time.monotonic()
    status = response(broker, request("status", {}, 0, "status-during-mutation"))
    assert time.monotonic() - started < 0.5
    assert status["ok"] and status["result"]["available"] is False
    mutation.join(timeout=1)
    assert replies[0]["error"]["code"] == "backend_timeout"
    assert cancelled.wait(1)

    restarted = AudioBroker(FakeBackend(), state_path=str(state),
                            authorize=lambda _m, _p: True)
    refused = response(restarted, request("connect", {"alias": "speaker"}, 0, "retry"))
    assert refused["error"]["code"] == "mutation_uncertain"


def test_post_intent_backend_unavailable_remains_uncertain_across_restart_sr023(tmp_path):
    state = tmp_path / "audio-state.json"

    class MutatedThenUnavailable(FakeBackend):
        def call(self, method, params, cancel):
            self.calls.append((method, params))
            raise BrokerError("backend_unavailable", "lost after possible device change")

    first = AudioBroker(MutatedThenUnavailable(), state_path=str(state),
                        authorize=lambda _m, _p: True)
    failed = response(first, request("connect", {"alias": "speaker"}, 0, "lost"))
    assert failed["error"]["code"] == "backend_unavailable"
    restarted = AudioBroker(FakeBackend(), state_path=str(state),
                            authorize=lambda _m, _p: True)
    refused = response(restarted, request("connect", {"alias": "speaker"}, 0, "retry"))
    assert refused["error"]["code"] == "mutation_uncertain"


def test_killable_backend_isolation_recovers_after_more_hangs_than_worker_limit_sr023():
    broker = RealAudioBroker(ForeverBackend(), backend_timeout_seconds=0.5)
    for index in range(6):
        refused = response(broker, request("status", request_id=f"hung-{index}"))
        assert refused["error"]["code"] == "backend_timeout"
    broker.backend = UnavailableBackend()
    recovered = response(broker, request("status", request_id="recovered"))
    assert recovered["ok"] and recovered["result"]["available"] is False
    assert not any(child.name == "panel-audio-backend"
                   for child in multiprocessing.active_children())


def test_if015_telemetry_has_only_positive_bounded_derived_schema_sr023():
    class TelemetryBackend(FakeBackend):
        def call(self, method, params, cancel):
            if method == "telemetry":
                derived = analyze_samples([0.0, 0.5, -0.5], generation=999,
                                          observed_monotonic_ms=40)
                return {"available": True, **derived}
            return super().call(method, params, cancel)

    telemetry = response(AudioBroker(TelemetryBackend()), request("telemetry"))["result"]
    assert telemetry["available"] is True and telemetry["generation"] == 0
    assert "samples" not in telemetry and len(telemetry["bands"]) <= 16
    unavailable = response(AudioBroker(UnavailableBackend()), request("telemetry"))["result"]
    assert unavailable == {"available": False}

    class RawTelemetry(TelemetryBackend):
        def call(self, method, params, cancel):
            value = super().call(method, params, cancel)
            if method == "telemetry": value["samples"] = [0.25]
            return value
    refused = response(AudioBroker(RawTelemetry()), request("telemetry"))
    assert refused["error"]["code"] == "unsafe_backend_result"


def test_shipped_backend_is_observable_but_never_mutates_sr023():
    broker = AudioBroker(UnavailableBackend())
    status = response(broker, request())["result"]
    assert status["available"] is False and status["devices"] == []
    assert response(broker, request("discover"))["error"]["code"] == "authorization_required"
    authorized = AudioBroker(UnavailableBackend(), authorize=lambda _m, _p: True)
    assert response(authorized, request("discover"))["error"]["code"] == "backend_unavailable"


def test_visualizer_is_bounded_finite_and_emits_no_samples_sr023():
    telemetry = analyze_samples([0.0, 0.5, -0.5, 0.0], generation=2,
                                observed_monotonic_ms=99)
    assert telemetry["generation"] == 2 and telemetry["active"] is True
    assert len(telemetry["bands"]) == 8 and "samples" not in telemetry
    assert all(0 <= x <= 1 for x in telemetry["bands"])
    with pytest.raises(ValueError): analyze_samples([float("nan")], generation=0)
    with pytest.raises(ValueError): analyze_samples([0.0] * (MAX_SAMPLES + 1), generation=0)
    with pytest.raises(ValueError): analyze_samples([0.0], generation=0, observed_monotonic_ms=-1)
    for invalid in (True, JS_SAFE_INTEGER + 1):
        with pytest.raises(ValueError): analyze_samples([0.0], generation=invalid)
    with pytest.raises(ValueError): analyze_samples([True], generation=0)
    with pytest.raises(ValueError): analyze_samples([0.0], generation=0, band_count=True)
    with pytest.raises(ValueError): analyze_samples([0.0], generation=0, silence_floor=False)
    assert analyze_samples([0.0], generation=0, silence_floor=0)["active"] is False


def test_visualizer_silence_hold_cadence_and_generation_reset_sr023():
    core = VisualizerTelemetry(silence_hold_ms=100, minimum_interval_ms=50)
    assert core.process([0.5, -0.5], generation=1, observed_monotonic_ms=100)["active"]
    assert core.process([0.0], generation=1, observed_monotonic_ms=120) is None
    assert core.process([0.0], generation=1, observed_monotonic_ms=150)["active"]
    assert not core.process([0.0], generation=1, observed_monotonic_ms=250)["active"]
    assert not core.process([0.0], generation=2, observed_monotonic_ms=300)["active"]
    with pytest.raises(ValueError): core.process([0.0], generation=2, observed_monotonic_ms=299)


def test_visualizer_max_window_has_bounded_cost_and_cadence_sr023():
    core = VisualizerTelemetry(band_count=16, max_samples=MAX_SAMPLES,
                               minimum_interval_ms=50)
    samples = [0.25, -0.25] * (MAX_SAMPLES // 2)
    started = time.perf_counter()
    emitted = [core.process(samples, generation=1, observed_monotonic_ms=i * 10)
               for i in range(100)]
    assert sum(item is not None for item in emitted) == 20
    assert time.perf_counter() - started < 3.0


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Windows Python lacks AF_UNIX")
def test_socket_slow_client_does_not_block_status_and_wire_is_bounded_sr023():
    socket_path = ROOT / ("audio-%s.sock" % os.getpid())
    server = BoundedUnixServer(str(socket_path), AudioBroker(UnavailableBackend()),
                               max_clients=2, client_timeout_seconds=0.2)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    deadline = time.monotonic() + 2
    while not socket_path.exists() and time.monotonic() < deadline: time.sleep(0.01)
    slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); slow.connect(str(socket_path)); slow.sendall(b"{")
    fast = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); fast.settimeout(1); fast.connect(str(socket_path))
    fast.sendall(request()); reply = json.loads(fast.recv(65536)); fast.close()
    assert reply["ok"] and reply["result"]["available"] is False
    oversized = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); oversized.settimeout(1)
    oversized.connect(str(socket_path)); oversized.sendall(b"x" * (MAX_REQUEST_BYTES + 1))
    refused = json.loads(oversized.recv(65536)); oversized.close()
    assert refused["error"]["code"] == "request_too_large"
    slow.close(); server.close(); thread.join(timeout=1)


def test_image_contract_is_disabled_local_and_carries_no_broad_dbus_policy_sr023():
    unit = (WALL / "wall-audio-router.service").read_text(encoding="utf-8")
    env = (WALL / "wall.env.example").read_text(encoding="utf-8")
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    user_data = (WALL / "user-data").read_text(encoding="utf-8")
    assert "User=panel" in unit and "RestrictAddressFamilies=AF_UNIX" in unit
    assert "NoNewPrivileges=true" in unit and "ProtectSystem=strict" in unit
    assert "EnvironmentFile=/etc/wall-panel/audio-router.env" in unit
    assert "EnvironmentFile=/etc/wall-panel/wall.env" not in unit
    assert "StateDirectory=wall-audio-router" in unit
    assert "--state /var/lib/wall-audio-router/state.json" in unit
    assert "WALL_AUDIO_ENABLED=false" in env
    assert "WALL_AUDIO_SOCKET=/run/wall-audio-router/service.sock" in env
    assert "bluetoothctl" not in unit and "wpctl" not in unit and "dbus" not in unit.lower()
    assert not (WALL / "wall-audio-dbus.conf").exists()
    assert "WALL_AUDIO_ENABLED" in firstboot and "wall-audio-router.service" in firstboot
    assert "/opt/wall-panel/stack/panel-audio/audio_router.py" in unit
    assert "wall-audio-router.service /target/etc/systemd/system/wall-audio-router.service" in user_data
    assert 'cp -a "$d/deploy-payload/." /target/opt/wall-panel/' in user_data
    required_payload = {
        "stack/panel-audio/audio_router.py", "stack/panel-audio/routing.py",
        "stack/panel-audio/visualizer.py", "stack/panel-audio/README.md",
        "stack/autoinstall/wall/wall-audio-router.service",
    }
    tracked = set(subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.splitlines())
    assert required_payload <= tracked
    completeness = firstboot.index("for _wall_audio_file in audio_router.py routing.py visualizer.py")
    enabled_branch = firstboot.index('if [ "$WALL_AUDIO_ENABLED" = true ]')
    assert completeness < enabled_branch
    assert 'fail_step "Panel audio payload is incomplete: missing $_wall_audio_file"' in firstboot
    assert '/etc/wall-panel/audio-router.env' in firstboot
    assert "printf 'WALL_AUDIO_SOCKET=/run/wall-audio-router/service.sock\\n'" in firstboot
