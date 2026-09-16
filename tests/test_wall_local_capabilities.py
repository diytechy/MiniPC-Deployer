"""SR-017/SR-020: local bootstrap, authority preservation and wake policy."""
import importlib.util
import json
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


WALL = Path(__file__).resolve().parents[1] / "stack/autoinstall/wall"


def load(filename, name):
    spec = importlib.util.spec_from_file_location(name, WALL / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SETUP = load("wall-local-setup.py", "wall_local_setup")
POWER = load("wall-sensor-power-policy.py", "wall_sensor_power_policy")


def test_unprovisioned_status_allows_only_local_bootstrap(tmp_path):
    host = {"enabled": False}
    state = {"schemaVersion": 1, "revision": 0, "localAccessEnabled": False}
    result = SETUP.current_status(host, state, (tmp_path / "state", tmp_path / "key"))
    assert result == {"revision": 0, "bootstrapAllowed": True,
                      "localConfigured": False, "remoteConfigured": False}


def test_existing_local_auth_blocks_anonymous_bootstrap(tmp_path):
    existing = tmp_path / "access-state.json"
    existing.write_text("protected", encoding="utf-8")
    result = SETUP.current_status({"enabled": False},
        {"schemaVersion": 1, "revision": 0, "localAccessEnabled": False}, (existing,))
    assert result["bootstrapAllowed"] is False


def test_local_merge_preserves_gateway_owner_and_secrets():
    host = {"enabled": True, "accessMode": "read-protected", "gatewayUrl": "https://wall.invalid",
            "deviceId": "panel", "deviceCredential": "secret", "rendererConfig": {"FEED_TOKEN": "also-secret"}}
    merged = SETUP.merged_host(host)
    assert all(merged[key] == value for key, value in host.items())
    assert merged["localAccessEnabled"] is True
    assert merged["localCapabilitiesVersion"] == merged["localFacePolicyVersion"] == 1
    assert merged["sensorSocket"] == "/run/wall-sensors/service.sock"
    assert merged["localSetupSocket"] == "/run/wall-local-setup/service.sock"


def test_local_merge_selects_local_authority_only_without_gateway():
    merged = SETUP.merged_host({"enabled": False, "rendererConfig": {}})
    assert merged["enabled"] is True and merged["accessMode"] == "local"


def test_dormant_gateway_is_reported_but_does_not_block_offline_local_bootstrap(tmp_path):
    host = {"enabled": False, "accessMode": "read-protected", "gatewayUrl": "https://wall.invalid",
            "deviceId": "panel", "deviceCredential": "secret"}
    state = {"schemaVersion": 1, "revision": 0, "localAccessEnabled": False}
    result = SETUP.current_status(host, state, (tmp_path / "state",))
    assert result["remoteConfigured"] is True and result["bootstrapAllowed"] is True
    merged = SETUP.merged_host(host)
    assert merged["accessMode"] == "local" and merged["enabled"] is True
    assert merged["deviceCredential"] == "secret"


def test_enabled_incomplete_gateway_fails_closed_without_downgrade(tmp_path):
    host = {"enabled": True, "accessMode": "read-protected",
            "gatewayUrl": "https://wall.invalid", "deviceId": "panel"}
    state = {"schemaVersion": 1, "revision": 0, "localAccessEnabled": False}
    result = SETUP.current_status(host, state, (tmp_path / "auth",))
    assert result["bootstrapAllowed"] is False and result["remoteConfigured"] is False
    with pytest.raises(SETUP.Refused, match="gateway-config-invalid"):
        SETUP.verify_gateway(host, "session", "1234")


def test_invalid_enabled_type_is_rejected():
    with pytest.raises(SETUP.Refused, match="host-state-invalid"):
        SETUP._validate_host({"enabled": "true"})


def test_atomic_json_replaces_complete_private_file(tmp_path):
    target = tmp_path / "state.json"
    SETUP.atomic_json(target, {"schemaVersion": 1, "revision": 1, "localAccessEnabled": True},
                      mode=0o600, uid=-1, gid=-1)
    assert json.loads(target.read_text(encoding="utf-8"))["revision"] == 1
    if __import__("os").name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_helper_requires_exact_private_modes_and_panel_gid():
    source = (WALL / "wall-local-setup.py").read_text(encoding="utf-8")
    assert "allowed_modes = {0o600, 0o640}" in source
    assert "info.st_gid != group_read_gid" in source
    assert "group_read_gid=panel.pw_gid" in source


def test_durable_state_replays_host_after_interrupted_rename(tmp_path, monkeypatch):
    host_path = tmp_path / "host.json"
    state_path = tmp_path / "local.json"
    backup_path = tmp_path / "backup.json"
    SETUP.atomic_json(host_path, {"enabled": False}, mode=0o600, uid=-1, gid=-1)
    helper = SETUP.Helper(host_path, state_path, (tmp_path / "auth",), backup_path,
                          SimpleNamespace(pw_uid=1000, pw_gid=-1))
    real_atomic = SETUP.atomic_json
    def windows_private(path, *, absent=None, group_read_gid=None):
        del group_read_gid
        if not Path(path).exists():
            return dict(absent) if absent is not None else SETUP.Refused("required-state-missing")
        return json.loads(Path(path).read_text(encoding="utf-8"))
    monkeypatch.setattr(SETUP, "private_object", windows_private)
    failed = False

    def fail_host_once(path, value, **kwargs):
        nonlocal failed
        if Path(path) == host_path and not failed:
            failed = True
            raise OSError("injected host rename failure")
        return real_atomic(path, value, **kwargs)

    monkeypatch.setattr(SETUP, "atomic_json", fail_host_once)
    with pytest.raises(OSError, match="injected"):
        helper.request({"method": "configure", "params": {
            "expectedRevision": 0, "accessMode": "local"}})
    assert json.loads(state_path.read_text(encoding="utf-8"))["localAccessEnabled"] is True
    result = helper.request({"method": "status", "params": {}})
    repaired = json.loads(host_path.read_text(encoding="utf-8"))
    assert result["localConfigured"] is True
    assert repaired["accessMode"] == "local"
    assert repaired["localSetupSocket"] == SETUP.SETUP_SOCKET


@pytest.mark.parametrize("patch", [
    {"cameraEnabled": True, "cameraConsentVersion": 1, "presenceMotion": True},
    {"cameraEnabled": True, "cameraConsentVersion": 1, "presenceFace": True},
    {"cameraEnabled": True, "cameraConsentVersion": 1, "faceLoginEnabled": True},
    {"bluetoothEnabled": True},
])
def test_saved_software_wake_policy_keeps_cpu_awake(patch):
    config = {"schemaVersion": 2, "cameraConsentVersion": 1, "cameraEnabled": False,
              "presenceFace": False, "presenceMotion": False, "faceLoginEnabled": False,
              "bluetoothEnabled": False, **patch}
    assert POWER.keep_awake(config) is True


def test_camera_without_fresh_consent_does_not_override_suspend():
    config = {"schemaVersion": 2, "cameraConsentVersion": 0, "cameraEnabled": True,
              "presenceFace": True, "presenceMotion": False, "faceLoginEnabled": False,
              "bluetoothEnabled": False}
    assert POWER.keep_awake(config) is False


def test_wake_must_match_fresh_sensor_status():
    now = 2_000_000
    status = {"protocolVersion": 2,
        "config": {"schemaVersion": 2, "cameraConsentVersion": 1, "cameraEnabled": True,
                   "presenceFace": False, "presenceMotion": True, "bluetoothEnabled": False},
        "motion": {"state": "positive", "observedAt": now - 10, "ttlMs": 3000}}
    SETUP.validate_wake({"source": "camera", "observedAt": now - 10, "ttlMs": 3000}, status, now)
    with pytest.raises(SETUP.Refused, match="wake-not-observed"):
        SETUP.validate_wake({"source": "bluetooth", "observedAt": now - 10, "ttlMs": 3000}, status, now)
    with pytest.raises(SETUP.Refused, match="wake-observation-stale"):
        SETUP.validate_wake({"source": "camera", "observedAt": now - 4000, "ttlMs": 3000},
                            {**status, "motion": {"state": "positive", "observedAt": now - 4000, "ttlMs": 3000}}, now)


def test_wake_accepts_a_newer_independently_fresh_observation():
    now = 2_000_000
    status = {"protocolVersion": 2,
        "config": {"schemaVersion": 2, "cameraConsentVersion": 1, "cameraEnabled": True,
                   "presenceFace": False, "presenceMotion": True, "bluetoothEnabled": False},
        "motion": {"state": "positive", "observedAt": now - 5, "ttlMs": 1000}}
    SETUP.validate_wake({"source": "camera", "observedAt": now - 200, "ttlMs": 1000}, status, now)
    status["motion"] = {"state": "positive", "observedAt": now - 1200, "ttlMs": 1000}
    with pytest.raises(SETUP.Refused, match="wake-not-observed"):
        SETUP.validate_wake({"source": "camera", "observedAt": now - 200, "ttlMs": 1000}, status, now)


def test_image_contract_installs_helper_and_sensor_without_host_flag():
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    installer = (WALL / "install-wall-capabilities.sh").read_text(encoding="utf-8")
    assert 'sensor_args=(--wheelhouse "$SENSOR_WHEELHOUSE")' in firstboot
    assert 'enable_unit_now "local setup:' in firstboot
    assert 'WALL_ACCESS_MODE=local into root-owned state' in firstboot
    assert '[ -d "$wheelhouse" ] || fail "The offline wheelhouse is required"' in installer
    assert '[ -f "$host_config" ] && [ -d "$wheelhouse" ]' not in installer
    assert "protocolVersion') == 2" in installer
    assert "local-capabilities-v1" in installer
    unit = (WALL / "wall-local-setup.service").read_text(encoding="utf-8")
    assert "User=root\nGroup=panel" in unit
    assert "RuntimeDirectoryMode=0750" in unit
    assert "-/run/wall-occupancy -/run/wall-backlight.prev" in unit
    assert "connection.settimeout(5)" in (WALL / "wall-local-setup.py").read_text(encoding="utf-8")
    assert "wall-camera-off.pre-local-capabilities.conf" in firstboot
    assert "wall-camera-off.pre-local-capabilities.absent" in firstboot
    assert 'atomic_install "$HOST_CONFIG_TMP" "$WALL_HOST_CONFIG" root panel 0640' in firstboot
    assert 'atomic_install "$merged_config" /etc/wall-panel/host.json root panel 0640' in installer
    assert "wall-camera-off.pre-local-capabilities.conf" in firstboot
    assert "wall-camera-off.pre-local-capabilities.absent" in firstboot


def test_model_manifest_path_and_exact_names_are_one_contract():
    renderer = (WALL / "render-wall-host-config.py").read_text(encoding="utf-8")
    installer = (WALL / "install-wall-capabilities.sh").read_text(encoding="utf-8")
    assert '"sensorModelManifest": "/opt/wall-sensors/models/manifest.json"' in renderer
    assert "names = {'det_10g.onnx', 'w600k_r50.onnx'}" in installer
    assert "for block in iter(lambda: model.read(1024 * 1024), b'')" in installer


# ── TC-910..912, TC-914: the touch witness (LLR-910..912, LLR-914) ───────────
#
# The witness is a thread of the root helper, so everything decidable about it
# is a pure function with an injected seam: the evdev module, the backlight
# tree, the monotonic clock and the Helper that owns the power command. None of
# these tests needs python-evdev, a device node or a panel.

TOUCH_CONFIG = {"mode": "filter", "name": "ELAN Touchscreen", "vendor": 0x04F3,
                "product": 0x222A, "isolate": False}


class FakeInfo:
    def __init__(self, vendor, product):
        self.vendor, self.product = vendor, product


class FakeDevice:
    """Enough evdev.InputDevice for identity selection, the grab probe and a read.

    `held` models somebody else's EVIOCGRAB: grab() then raises, exactly as the
    kernel refuses an exclusive grab that another file description holds.
    `events` is replayed by read_loop(), which then raises `ends_with` so a node
    that disappears mid-read is expressible.
    """

    def __init__(self, path, name, vendor, product, *, held=False, events=(),
                 ends_with=None):
        self.path, self.name, self.info = path, name, FakeInfo(vendor, product)
        self.closed = False
        self.held = held
        self.events = list(events)
        self.ends_with = ends_with
        self.grabs = 0
        self.ungrabs = 0

    def grab(self):
        self.grabs += 1
        if self.held:
            raise OSError(16, "Device or resource busy")

    def ungrab(self):
        self.ungrabs += 1

    def read_loop(self):
        for event in self.events:
            yield event
        raise self.ends_with if self.ends_with is not None else OSError(19, "No such device")

    def close(self):
        self.closed = True


class FakeEvdev:
    """A device table that can also lose a node between listing and opening."""

    def __init__(self, devices, vanish=()):
        self._devices = {device.path: device for device in devices}
        self._vanish = set(vanish)

    def list_devices(self):
        return sorted(self._devices)

    def InputDevice(self, path):  # noqa: N802 - mirrors the real evdev API
        if path in self._vanish:
            raise OSError(19, "No such device")
        return self._devices[path]

    def opened(self):
        return list(self._devices.values())


def physical_device(path="/dev/input/event5", **kwargs):
    return FakeDevice(path, TOUCH_CONFIG["name"], TOUCH_CONFIG["vendor"],
                      TOUCH_CONFIG["product"], **kwargs)


def virtual_device(path="/dev/input/event9", **kwargs):
    return FakeDevice(path, SETUP.TOUCH_VIRTUAL_NAME, SETUP.TOUCH_VIRTUAL_VENDOR,
                      SETUP.TOUCH_VIRTUAL_PRODUCT, **kwargs)


def backlight(tmp_path, brightness, maximum=100, name="intel_backlight"):
    """Build a fake /sys/class/backlight tree and return its root."""
    root = tmp_path / "backlight"
    device = root / name
    device.mkdir(parents=True, exist_ok=True)
    (device / "brightness").write_text(str(brightness), encoding="utf-8")
    (device / "max_brightness").write_text(str(maximum), encoding="utf-8")
    return root


class SpyHelper:
    """A Helper stand-in that records the wake calls the witness makes."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def wake(self, params, *, witness=False):
        self.calls.append((params, witness))
        if self.error is not None:
            raise self.error
        return {"woke": True}


def witness(tmp_path, *, brightness=0, config=TOUCH_CONFIG, devices=(), clock=None,
            helper=None, vanish=(), evdev=None, log=None, sleep=None):
    path = tmp_path / "touch-filter.json"
    if config is not None:
        path.write_text(json.dumps(config), encoding="utf-8")
    return SETUP.TouchWitness(
        helper if helper is not None else SpyHelper(),
        config_path=path,
        backlight_root=backlight(tmp_path, brightness),
        clock=clock if clock is not None else (lambda: 0.0),
        evdev_module=evdev if evdev is not None else FakeEvdev(list(devices), vanish),
        log=log if log is not None else (lambda message: None),
        retry_s=0,
        blocked_retry_s=0,
        sleep=sleep if sleep is not None else (lambda seconds: None),
    )


@pytest.mark.smoke
def test_witness_selects_the_virtual_node_in_filter_mode_tc910(tmp_path):
    # `filter` is the ONLY mode that creates a uinput device, and it is also
    # the mode that grabs the raw node, so the virtual node is both the only
    # readable source and the preferred one.
    assert SETUP.touch_targets({**TOUCH_CONFIG, "mode": "filter"})[0] == {
        "virtual": True, "name": SETUP.TOUCH_VIRTUAL_NAME, "vendor": 0, "product": 1}
    watcher = witness(tmp_path, config={**TOUCH_CONFIG, "mode": "filter"},
                      devices=[physical_device(held=True), virtual_device()])
    assert watcher.resolve().name == SETUP.TOUCH_VIRTUAL_NAME


@pytest.mark.smoke
def test_adaptive_creates_no_virtual_node_so_the_witness_targets_the_physical_one_tc910(tmp_path):
    # configure-touch-filter.sh modprobes uinput for `filter` ALONE: adaptive's
    # accepted taps leave over its AF_UNIX bridge, not through a device. A
    # witness that hunted for a virtual node here would retry for ever.
    assert SETUP.touch_targets({**TOUCH_CONFIG, "mode": "adaptive"}) == [
        {"virtual": False, "name": TOUCH_CONFIG["name"],
         "vendor": TOUCH_CONFIG["vendor"], "product": TOUCH_CONFIG["product"]}]
    # With the daemon holding the raw node there is nothing to witness, and
    # that is reported as a steady state on the long backoff, not as a fault.
    watcher = witness(tmp_path, config={**TOUCH_CONFIG, "mode": "adaptive"},
                      devices=[physical_device(held=True)])
    with pytest.raises(SETUP.Refused, match="touch-node-grabbed"):
        watcher.resolve()
    slept, said = [], []
    watcher = witness(tmp_path, config={**TOUCH_CONFIG, "mode": "adaptive"},
                      devices=[physical_device(held=True)],
                      sleep=slept.append, log=said.append)
    watcher.blocked_retry_s = SETUP.TOUCH_BLOCKED_RETRY_S
    watcher.run(stop=lambda: len(slept) >= 3)
    assert slept == [SETUP.TOUCH_BLOCKED_RETRY_S] * 3
    # Said once over three cycles, not once per retry.
    assert sum("grabbed by the touch filter" in line for line in said) == 1
    # Once the daemon lets go, the same physical node becomes the witness.
    watcher = witness(tmp_path, config={**TOUCH_CONFIG, "mode": "adaptive"},
                      devices=[physical_device(held=False)])
    assert watcher.resolve().name == TOUCH_CONFIG["name"]


@pytest.mark.smoke
def test_witness_selects_the_physical_node_without_a_grab_tc910(tmp_path):
    for mode in ("off", "shadow"):
        assert SETUP.touch_targets({**TOUCH_CONFIG, "mode": mode}) == [
            {"virtual": False, "name": TOUCH_CONFIG["name"],
             "vendor": TOUCH_CONFIG["vendor"], "product": TOUCH_CONFIG["product"]}]
    watcher = witness(tmp_path, config={**TOUCH_CONFIG, "mode": "shadow"},
                      devices=[physical_device(), virtual_device()])
    assert watcher.resolve().name == TOUCH_CONFIG["name"]


@pytest.mark.smoke
def test_the_live_grab_is_probed_not_inferred_from_the_mode_tc910(tmp_path):
    # The persisted mode says what the daemon INTENDS. While it is down or
    # restarting nobody holds the grab, and the raw node is then a perfectly
    # good witness — so filter mode falls back to it and returns to the virtual
    # node the moment the daemon is back.
    raw = physical_device(held=False)
    watcher = witness(tmp_path, config={**TOUCH_CONFIG, "mode": "filter"}, devices=[raw])
    assert watcher.resolve().name == TOUCH_CONFIG["name"]
    assert (raw.grabs, raw.ungrabs) == (1, 1)  # probed, and given straight back
    # Daemon back up: the virtual node wins again and is never grab-probed,
    # because nobody can hold a grab on it by construction.
    virtual = virtual_device()
    watcher = witness(tmp_path, config={**TOUCH_CONFIG, "mode": "filter"},
                      devices=[physical_device(held=True), virtual])
    assert watcher.resolve() is virtual
    assert virtual.grabs == 0
    # Filter mode with the raw node grabbed and no virtual node yet (the
    # restart window in the other direction) refuses rather than reading a node
    # that would deliver nothing.
    watcher = witness(tmp_path, config={**TOUCH_CONFIG, "mode": "filter"},
                      devices=[physical_device(held=True)])
    with pytest.raises(SETUP.Refused, match="touch-node-grabbed"):
        watcher.resolve()


@pytest.mark.smoke
def test_no_handle_is_left_open_when_a_node_vanishes_mid_enumeration_tc910(tmp_path):
    # The filter unit is Restart=always, so a node really can disappear between
    # list_devices() and InputDevice(). Leaking the handles opened before that
    # point ends in EMFILE on a loop that re-resolves every second.
    survivor = virtual_device("/dev/input/event9")
    doomed = physical_device("/dev/input/event5")
    evdev = FakeEvdev([doomed, survivor], vanish={"/dev/input/event9"})
    with pytest.raises(OSError):
        SETUP.select_input_device(evdev, SETUP.touch_targets(TOUCH_CONFIG)[0])
    assert doomed.closed is True
    # And on the ordinary refusals too: everything opened, nothing kept.
    devices = [physical_device("/dev/input/event5"), physical_device("/dev/input/event6")]
    with pytest.raises(SETUP.Refused, match="touch-node-ambiguous"):
        SETUP.select_input_device(FakeEvdev(devices), SETUP.touch_targets(
            {**TOUCH_CONFIG, "mode": "off"})[0])
    assert [device.closed for device in devices] == [True, True]
    # The one that IS returned stays open.
    keeper = physical_device()
    assert SETUP.select_input_device(FakeEvdev([keeper]), SETUP.touch_targets(
        {**TOUCH_CONFIG, "mode": "off"})[0]) is keeper
    assert keeper.closed is False


@pytest.mark.smoke
def test_witness_refuses_zero_or_ambiguous_matches_tc910(tmp_path):
    off = {**TOUCH_CONFIG, "mode": "off"}
    watcher = witness(tmp_path, config=off, devices=[virtual_device()])
    with pytest.raises(SETUP.Refused, match="touch-node-missing"):
        watcher.resolve()
    watcher = witness(tmp_path, config=off, devices=[physical_device("/dev/input/event5"),
                                                    physical_device("/dev/input/event6")])
    with pytest.raises(SETUP.Refused, match="touch-node-ambiguous"):
        watcher.resolve()
    # Ambiguity on the preferred target is not silently papered over by the
    # fallback either: filter mode with two virtual nodes refuses.
    watcher = witness(tmp_path, devices=[virtual_device("/dev/input/event9"),
                                         virtual_device("/dev/input/event10")])
    with pytest.raises(SETUP.Refused):
        watcher.resolve()


@pytest.mark.smoke
def test_witness_never_opens_an_event_node_by_number_tc910(tmp_path):
    # eventN is allocation-order dependent and changes on every daemon restart
    # and USB re-enumeration, so no literal node path may appear in the source.
    source = (WALL / "wall-local-setup.py").read_text(encoding="utf-8")
    assert "/dev/input/event" not in source
    # A node at a different number is still found, because identity selects it.
    watcher = witness(tmp_path, devices=[virtual_device("/dev/input/event31")])
    assert watcher.resolve().path == "/dev/input/event31"
    # An absent or malformed config refuses rather than guessing a device.
    for bad in (None, {}, {"mode": "off", "name": "ELAN Touchscreen", "vendor": "04f3"}):
        with pytest.raises(SETUP.Refused, match="touch-config-invalid"):
            SETUP.touch_targets(bad)


@pytest.mark.smoke
def test_brightness_zero_is_the_wake_precondition_tc911(tmp_path):
    assert SETUP.backlight_dark(backlight(tmp_path / "dark", 0)) is True
    assert SETUP.backlight_dark(backlight(tmp_path / "lit", 1)) is False
    # No readable backlight is not evidence of darkness.
    assert SETUP.backlight_dark(tmp_path / "absent") is False
    mixed = backlight(tmp_path / "mixed", 0)
    second = mixed / "edp_backlight"
    second.mkdir()
    (second / "brightness").write_text("40", encoding="utf-8")
    (second / "max_brightness").write_text("100", encoding="utf-8")
    assert SETUP.backlight_dark(mixed) is False


@pytest.mark.smoke
def test_a_lit_panel_runs_no_power_command_tc911(tmp_path):
    helper = SpyHelper()
    watcher = witness(tmp_path, brightness=100, helper=helper)
    assert watcher.contact() is False
    assert helper.calls == []


@pytest.mark.smoke
def test_one_contact_wakes_once_and_the_rate_limit_holds_tc911(tmp_path):
    helper = SpyHelper()
    now = [100.0]
    watcher = witness(tmp_path, brightness=0, helper=helper, clock=lambda: now[0])
    assert watcher.contact() is True
    assert helper.calls == [({"source": "touch"}, True)]
    now[0] += SETUP.TOUCH_WAKE_MIN_INTERVAL_S - 0.01
    assert watcher.contact() is False
    assert len(helper.calls) == 1
    now[0] += 0.02
    assert watcher.contact() is True
    assert len(helper.calls) == 2
    assert SETUP.TOUCH_WAKE_MIN_INTERVAL_S == 2.0


@pytest.mark.smoke
def test_only_a_contact_start_is_a_wake_tc911():
    down = SimpleNamespace(type=SETUP.EV_KEY, code=SETUP.BTN_TOUCH, value=1)
    assert SETUP.is_contact_start(down) is True
    for other in (SimpleNamespace(type=SETUP.EV_KEY, code=SETUP.BTN_TOUCH, value=0),
                  SimpleNamespace(type=3, code=SETUP.BTN_TOUCH, value=1),
                  SimpleNamespace(type=SETUP.EV_KEY, code=1, value=1)):
        assert SETUP.is_contact_start(other) is False


@pytest.mark.smoke
def test_a_touch_source_over_the_socket_is_refused_tc912():
    status = {"protocolVersion": 2,
              "config": {"schemaVersion": 2, "cameraConsentVersion": 1, "cameraEnabled": True,
                         "presenceFace": False, "presenceMotion": True, "bluetoothEnabled": False},
              "motion": {"state": "positive", "observedAt": 1_999_990, "ttlMs": 3000}}
    for params in ({"source": "touch"},
                   {"source": "touch", "observedAt": 1_999_990, "ttlMs": 3000}):
        with pytest.raises(SETUP.Refused, match="wake-request-invalid"):
            SETUP.validate_wake(params, status, 2_000_000)


@pytest.mark.smoke
def test_the_witness_seam_accepts_exactly_one_shape_tc912():
    SETUP.validate_wake({"source": "touch"}, {}, witness=True)
    for params in ({"source": "camera"}, {"source": "touch", "ttlMs": 1},
                   {}, {"source": "touch", "observedAt": 1}):
        with pytest.raises(SETUP.Refused, match="wake-request-invalid"):
            SETUP.validate_wake(params, {}, witness=True)


@pytest.mark.smoke
def test_the_socket_path_never_passes_witness_true_tc912(tmp_path, monkeypatch):
    # Structural: the seam is keyword-only and Helper.request cannot reach it.
    source = (WALL / "wall-local-setup.py").read_text(encoding="utf-8")
    assert "def validate_wake(params: dict, status: dict, now_ms: int | None = None, *,\n" in source
    assert source.count("witness=True") == 3  # the seam call, Helper.wake, the witness
    seen = {}
    helper = SETUP.Helper()

    def spy(params, status, now_ms=None, *, witness=False):
        seen["witness"] = witness
        raise SETUP.Refused("wake-not-observed")

    monkeypatch.setattr(SETUP, "validate_wake", spy)
    monkeypatch.setattr(SETUP, "sensor_status", lambda *args, **kwargs: {})
    monkeypatch.setattr(SETUP.Helper, "reconcile",
                        lambda self: (None, {"enabled": False}, {"revision": 0}))
    with pytest.raises(SETUP.Refused):
        helper.request({"method": "wake", "params": {"source": "touch"}})
    assert seen == {"witness": False}


@pytest.mark.smoke
def test_one_place_runs_the_power_command_tc912(monkeypatch):
    ran = []
    monkeypatch.setattr(SETUP.subprocess, "run", lambda command, **kwargs: ran.append(command))
    monkeypatch.setattr(SETUP, "sensor_status", lambda *args, **kwargs: {})
    monkeypatch.setattr(SETUP, "validate_wake", lambda *args, **kwargs: None)
    helper = SETUP.Helper()
    assert helper.wake({"source": "touch"}, witness=True) == {"woke": True}
    assert helper.wake({"source": "camera", "observedAt": 1, "ttlMs": 2}) == {"woke": True}
    assert ran == [SETUP.TOUCH_POWER_COMMAND, SETUP.POWER_COMMAND]
    assert SETUP.TOUCH_POWER_COMMAND == ("/usr/local/sbin/wall-sleep.sh", "touch-wake")


@pytest.mark.smoke
def test_panel_display_off_accepts_only_its_empty_conclusion_shape_tc915(monkeypatch):
    # display-off is a renderer conclusion, not a sensor observation. In
    # particular, accepting wake's evidence tuple here would accidentally make
    # the image re-adjudicate the panel's 30-second state machine.
    for params in ({},):
        SETUP.validate_display_off(params)
    for params in ({"source": "camera", "observedAt": 1, "ttlMs": 1},
                   {"source": "touch"}, {"observedAt": 1}, {"reason": "absence"},
                   {"source": "bluetooth", "ttlMs": 1}):
        with pytest.raises(SETUP.Refused, match="display-off-request-invalid"):
            SETUP.validate_display_off(params)

    ran = []
    monkeypatch.setattr(SETUP.subprocess, "run", lambda command, **kwargs: ran.append(command))
    helper = SETUP.Helper()
    assert helper.display_off({}) == {"displayOff": True}
    assert ran == [SETUP.DISPLAY_OFF_COMMAND]
    assert SETUP.DISPLAY_OFF_COMMAND == ("/usr/local/sbin/wall-sleep.sh", "panel-display-off")


@pytest.mark.smoke
def test_socket_keeps_wake_and_display_off_validation_separate_tc916(monkeypatch):
    helper = SETUP.Helper()
    monkeypatch.setattr(SETUP.Helper, "reconcile",
                        lambda self: (None, {"enabled": False}, {"revision": 0}))
    monkeypatch.setattr(SETUP.subprocess, "run", lambda *args, **kwargs: None)
    assert helper.request({"method": "display-off", "params": {}}) == {"displayOff": True}
    with pytest.raises(SETUP.Refused, match="display-off-request-invalid"):
        helper.request({"method": "display-off", "params": {"source": "camera", "observedAt": 1, "ttlMs": 1}})
    with pytest.raises(SETUP.Refused, match="wake-request-invalid"):
        SETUP.validate_wake({}, {})


@pytest.mark.smoke
def test_a_node_that_disappears_is_re_resolved_and_reads_again_tc913(tmp_path):
    """Resolve, wake, lose the node, re-resolve at a NEW minor, wake again.

    wall-touch-filter is Restart=always and `wall-touch-filter-sleep pre` stops
    it before every suspend, so the uinput node is destroyed and recreated with
    a different minor routinely. The whole journey runs through the real
    TouchWitness.run(), not a stubbed resolve.
    """
    down = SimpleNamespace(type=SETUP.EV_KEY, code=SETUP.BTN_TOUCH, value=1)
    before = virtual_device("/dev/input/event9", events=[down])
    after = virtual_device("/dev/input/event12", events=[down])
    helper = SpyHelper()
    now = [0.0]

    class Restarting:
        """One table whose virtual node is replaced between resolutions."""

        def __init__(self):
            self.stage = 0
            self.tables = [FakeEvdev([physical_device(held=True), before]),
                           FakeEvdev([physical_device(held=True)]),
                           FakeEvdev([physical_device(held=True), after])]

        def table(self):
            return self.tables[min(self.stage, len(self.tables) - 1)]

        def list_devices(self):
            return self.table().list_devices()

        def InputDevice(self, path):  # noqa: N802
            return self.table().InputDevice(path)

    devices = Restarting()

    def tick(_seconds):
        # Each backoff advances the world: the node goes away, then returns.
        devices.stage += 1
        now[0] += 10  # well past the rate limit, so the second tap is its own

    watcher = witness(tmp_path, brightness=0, helper=helper, evdev=devices,
                      clock=lambda: now[0], sleep=tick)
    watcher.run(stop=lambda: devices.stage >= 3)

    # Two real resolutions, two real reads, two real wakes — at two different
    # node numbers, because identity and not eventN is what found them.
    assert before.closed is True and after.closed is True
    assert helper.calls == [({"source": "touch"}, True), ({"source": "touch"}, True)]
    assert devices.stage == 3


@pytest.mark.smoke
def test_a_refused_wake_does_not_arm_the_rate_limit_tc911(tmp_path):
    helper = SpyHelper(error=SETUP.Refused("wake-request-invalid"))
    watcher = witness(tmp_path, brightness=0, helper=helper, clock=lambda: 5.0)
    assert watcher.contact() is False
    assert watcher.last_wake is None


@pytest.mark.smoke
@pytest.mark.parametrize("status,expected_note", [
    (SETUP.TOUCH_WAKE_DECLINED, "a power decision is in flight"),
    (1, "the backlight did not come on"),
    (2, "the backlight did not come on"),
])
def test_only_a_completed_wake_arms_the_rate_limit_tc911(tmp_path, status, expected_note):
    """A non-zero touch-wake leaves the panel DARK, so the clock must not start.

    wall-sleep.sh runs `set -u` and not `set -e`, so a discarded backlight
    status would have made the arm exit 0 on a panel that never lit; the arm now
    exits with backlight_set's own status. On this side the rule is the mirror
    image: arm only on exit 0. Arming on a failed attempt would make the panel
    ignore the next two seconds of tapping while still dark -- exactly the
    symptom the Owner reported in the first place.
    """
    said = []
    helper = SpyHelper(error=subprocess.CalledProcessError(status, SETUP.TOUCH_POWER_COMMAND))
    watcher = witness(tmp_path, brightness=0, helper=helper, clock=lambda: 5.0, log=said.append)
    assert watcher.contact() is False
    assert watcher.last_wake is None, "a failed wake armed the rate limit"
    assert any("not arming the rate limit" in line for line in said)
    assert any(expected_note in line for line in said)
    assert any("exit %s" % status in line for line in said)
    # The next contact is therefore tried immediately, not swallowed by a clock
    # that a failure started.
    helper.error = None
    assert watcher.contact() is True
    assert watcher.last_wake == 5.0


@pytest.mark.smoke
def test_the_hardware_wake_measurement_is_read_only_and_recorded_tc914():
    report = (WALL / "wall-touch-wakeup-report.sh").read_text(encoding="utf-8")
    assert "/proc/acpi/wakeup" in report
    assert "power/wakeup" in report
    assert "/sys/power/mem_sleep" in report and "/sys/power/state" in report
    # Read-only: it never writes a wakeup flag and never suspends.
    for forbidden in ("> /sys/", ">/sys/", "rtcwake", "systemctl suspend", "echo enabled"):
        assert forbidden not in report
    burn_in = (WALL / "WALL-BURN-IN.md").read_text(encoding="utf-8")
    assert "wall-touch-wakeup-report.sh" in burn_in
    assert "### Measured hardware-wake facts" in burn_in
