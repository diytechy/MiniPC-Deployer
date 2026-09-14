"""SR-016/SR-017: protected cutover refuses mismatched or public credentials."""
import json
import subprocess
import sys
from pathlib import Path
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "stack/panel-access/validate-panel-access.py"


def run_validator(tmp_path, enabled, config):
    env = tmp_path / "hub.env"
    env.write_text("PANEL_ACCESS_ENABLED=" + enabled + "\n", encoding="utf-8")
    document = tmp_path / "config.json"
    document.write_text(json.dumps(config), encoding="utf-8")
    return subprocess.run([sys.executable, str(SCRIPT), "--env", str(env), "--config", str(document)], capture_output=True, text=True)


def test_sr016_legacy_and_protected_policy_must_agree(tmp_path):
    assert run_validator(tmp_path, "false", {"ACCESS_ENABLED": False}).returncode == 0
    assert run_validator(tmp_path, "true", {"ACCESS_ENABLED": True}).returncode == 0
    assert run_validator(tmp_path, "false", {"ACCESS_ENABLED": True}).returncode == 1
    assert run_validator(tmp_path, "true", {"ACCESS_ENABLED": False}).returncode == 1


def test_sr016_protected_config_never_exposes_credentials(tmp_path):
    result = run_validator(tmp_path, "true", {"ACCESS_ENABLED": True, "FEED_TOKEN": "never-echo-this-secret"})
    assert result.returncode == 1
    assert "never-echo-this-secret" not in result.stdout + result.stderr
    assert run_validator(tmp_path, "true", {"ACCESS_ENABLED": True, "nested": {"deviceCredential": "hidden"}}).returncode == 1


@pytest.mark.parametrize("enabled", ["false", "true"])
def test_sr017_hub_renderer_never_receives_panel_local_secrets(tmp_path, enabled):
    policy = enabled == "true"
    for secret in [
        {"FEED_TOKEN": "fixture"},
        {"HEARTBEAT_URL": "https://status.invalid/push/fixture"},
        {"nested": {"HEARTBEAT_URL": "https://status.invalid/push/fixture"}},
        {"heartbeat_url": "https://status.invalid/push/fixture"},
        {"SUBSONIC": {"user": "listener"}},
        {"SUBSONIC": {"password": "fixture"}},
        {"nested": {"subsonic": {"USER": "listener"}}},
        {"nested": {"SUBSONIC": [{"Password": "fixture"}]}},
    ]:
        assert run_validator(tmp_path, enabled, {"ACCESS_ENABLED": policy, **secret}).returncode == 1


def test_sr016_invalid_policy_fails_closed(tmp_path):
    assert run_validator(tmp_path, "tru", {"ACCESS_ENABLED": True}).returncode == 1
    assert run_validator(tmp_path, "false", {"ACCESS_ENABLED": "false"}).returncode == 1


def test_sr017_installer_is_offline_and_separates_sensor_state_from_broker():
    root = SCRIPT.parents[1]
    installer = (root / "autoinstall/wall/install-wall-capabilities.sh").read_text()
    assert "--no-index" in installer and "--require-hashes" in installer
    assert 'atomic_install "$merged_config" /etc/wall-panel/host.json root panel 0640' in installer
    assert "data.get('accessMode') not in {'read-protected', 'write-only'}" in installer
    unit = (root / "autoinstall/wall/wall-sensors.service").read_text()
    assert "User=wall-sensors" in unit and "StateDirectoryMode=0700" in unit
    assert "RuntimeDirectoryMode=0750" in unit and "--allowed-uid ${PANEL_SENSOR_UID}" in unit


def test_sr017_legacy_camera_flag_never_blacklists_or_unloads_hardware():
    source = (SCRIPT.parents[1] / "autoinstall/wall/wall-firstboot.sh").read_text(encoding="utf-8")
    camera = source[source.index("CAM_BLACKLIST="):source.index("if [ -f /etc/systemd/system/wall-sync.service")]
    assert 'rm -f "$CAM_BLACKLIST"' in camera
    assert "modprobe uvcvideo" in camera
    assert "blacklist uvcvideo" not in camera
    assert "modprobe -r uvcvideo" not in camera
    assert "WALL_CAMERA_ENABLED" not in camera
