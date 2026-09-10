"""SR-017: renderer secrets originate only in the panel's private host file."""
import importlib.util
import stat
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "stack/autoinstall/wall/render-wall-host-config.py"
SPEC = importlib.util.spec_from_file_location("render_wall_host_config", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_sr017_renderer_secrets_merge_without_replacing_access_registration(tmp_path):
    current = tmp_path / "host.json"
    current.write_text('{"enabled":true,"deviceId":"panel","deviceCredential":"fixture","rendererConfig":{"FEED_TOKEN":"old"}}', encoding="utf-8")
    current.chmod(stat.S_IRUSR | stat.S_IWUSR)
    result = MODULE.render(current, {
        "WALL_SHELL_FEED_TOKEN": "new-token",
        "WALL_SHELL_HEARTBEAT_URL": "https://status.invalid/push/fixture",
        "WALL_SHELL_SUBSONIC_USER": "listener",
        "WALL_SHELL_SUBSONIC_PASSWORD": "music-secret",
    })
    assert result["deviceCredential"] == "fixture"
    assert result["rendererConfig"] == {
        "FEED_TOKEN": "new-token",
        "HEARTBEAT_URL": "https://status.invalid/push/fixture",
        "SUBSONIC": {"user": "listener", "password": "music-secret"},
    }


def test_sr017_empty_wall_values_revoke_old_renderer_secrets(tmp_path):
    current = tmp_path / "host.json"
    current.write_text('{"enabled":false,"rendererConfig":{"FEED_TOKEN":"old"}}', encoding="utf-8")
    current.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert MODULE.render(current, {})["rendererConfig"] == {
        "FEED_TOKEN": "", "HEARTBEAT_URL": "", "SUBSONIC": {"user": "", "password": ""},
    }


def test_sr017_oversized_wall_secret_is_refused_without_echoing_it(tmp_path):
    try:
        MODULE.render(tmp_path / "host.json", {"WALL_SHELL_FEED_TOKEN": "x" * 4097})
    except ValueError as error:
        assert "x" * 64 not in str(error)
    else:
        raise AssertionError("oversized renderer secret was accepted")


def test_sr017_firstboot_passes_only_private_config_path_to_kiosk_env():
    firstboot = (SCRIPT.parent / "wall-firstboot.sh").read_text(encoding="utf-8")
    assert 'echo "WALL_HOST_CONFIG=$WALL_HOST_CONFIG"' in firstboot
    for key in ("WALL_SHELL_FEED_TOKEN", "WALL_SHELL_HEARTBEAT_URL", "WALL_SHELL_SUBSONIC_USER", "WALL_SHELL_SUBSONIC_PASSWORD"):
        assert f'echo "{key}=' not in firstboot
    installer = (SCRIPT.parent / "install-wall-capabilities.sh").read_text(encoding="utf-8")
    assert "incoming.pop('rendererConfig', None)" in installer
