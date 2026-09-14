"""SR-017/SR-020: local bootstrap, authority preservation and wake policy."""
import importlib.util
import json
import stat
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
