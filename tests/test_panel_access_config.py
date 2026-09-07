"""SR-016/SR-017: protected cutover refuses mismatched or public credentials."""
import json
import subprocess
import sys
import shutil
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
    assert run_validator(tmp_path, "false", {"ACCESS_ENABLED": False, "FEED_TOKEN": "fixture-secret"}).returncode == 0
    assert run_validator(tmp_path, "true", {"ACCESS_ENABLED": True}).returncode == 0
    assert run_validator(tmp_path, "false", {"ACCESS_ENABLED": True}).returncode == 1
    assert run_validator(tmp_path, "true", {"ACCESS_ENABLED": False}).returncode == 1


def test_sr016_protected_config_never_exposes_credentials(tmp_path):
    result = run_validator(tmp_path, "true", {"ACCESS_ENABLED": True, "FEED_TOKEN": "never-echo-this-secret"})
    assert result.returncode == 1
    assert "never-echo-this-secret" not in result.stdout + result.stderr
    assert run_validator(tmp_path, "true", {"ACCESS_ENABLED": True, "nested": {"deviceCredential": "hidden"}}).returncode == 1


def test_sr016_invalid_policy_fails_closed(tmp_path):
    assert run_validator(tmp_path, "tru", {"ACCESS_ENABLED": True}).returncode == 1
    assert run_validator(tmp_path, "false", {"ACCESS_ENABLED": "false"}).returncode == 1


def test_sr017_installer_is_offline_and_separates_sensor_state_from_broker():
    root = SCRIPT.parents[1]
    installer = (root / "autoinstall/wall/install-wall-capabilities.sh").read_text()
    assert "--no-index" in installer and "--require-hashes" in installer
    assert "-o panel -g panel -m 0600" in installer
    unit = (root / "autoinstall/wall/wall-sensors.service").read_text()
    assert "User=wall-sensors" in unit and "StateDirectoryMode=0700" in unit
    assert "RuntimeDirectoryMode=0750" in unit and "--allowed-uid ${PANEL_SENSOR_UID}" in unit


@pytest.mark.parametrize("setting", ["true", "TRUE", "yes", "1", "false"])
def test_sr017_camera_gate_refreshes_running_service_in_both_directions(setting):
    bash = shutil.which("bash")
    if not bash:
        candidate = Path("C:/Program Files/Git/bin/bash.exe")
        if candidate.exists():
            bash = str(candidate)
    if not bash:
        pytest.skip("Bash is required for the isolated camera lifecycle test")
    source = (SCRIPT.parents[1] / "autoinstall/wall/wall-firstboot.sh").read_text(encoding="utf-8")
    start = source.index("sensor_was_active=0")
    end = source.index('\nif [ -f /etc/systemd/system/wall-sync.service', start)
    fragment = source[start:end]
    fixture = r'''
set -eu
fixture_dir=$(mktemp -d)
trap 'rm -f "$fixture_dir/blacklist"; rmdir "$fixture_dir"' EXIT
CAM_BLACKLIST="$fixture_dir/blacklist"
touch "$CAM_BLACKLIST"
PAYLOAD=/fixture
WALL_CAMERA_DEVICE=/dev/not-a-real-camera
systemctl() { echo "systemctl $*"; return 0; }
lsmod() { echo 'uvcvideo fixture'; }
modprobe() { echo "modprobe $*" >&2; return 0; }
log() { :; }
warn() { :; }
fail_step() { echo "FAIL $*" >&2; exit 1; }
'''
    result = subprocess.run([bash, "-c", fixture + '\nWALL_CAMERA_ENABLED="' + setting + '"\n' + fragment], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "systemctl restart wall-sensors.service" in result.stdout
    assert ("systemctl stop wall-sensors.service" in result.stdout) == (setting == "false")
    # Execute the actual renderer-publication case too, so synonyms cannot drift.
    normalization_start = source.index('    case "${WALL_CAMERA_ENABLED:-false}" in')
    normalization_end = source.index("    esac", normalization_start) + len("    esac")
    normalized = subprocess.run([bash, "-c", 'WALL_CAMERA_ENABLED="' + setting + '"\n' + source[normalization_start:normalization_end]], capture_output=True, text=True)
    assert normalized.stdout.strip() == "WALL_CAMERA_ENABLED=" + ("false" if setting == "false" else "true")
