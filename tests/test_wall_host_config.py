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


# ── Group C3: local authentication mode (Owner ruling 2026-09-13) ───────────
# The renderer half above is unchanged. What follows is the ACCESS half, which
# this script did not own before: two fields, no credential, and a reversal.


def test_local_mode_writes_only_access_mode_and_enabled_no_credential(tmp_path):
    current = tmp_path / "host.json"
    current.write_text('{"enabled":false}', encoding="utf-8")
    current.chmod(stat.S_IRUSR | stat.S_IWUSR)
    result = MODULE.render(current, {"WALL_ACCESS_MODE": "local"})
    assert result["accessMode"] == "local"
    assert result["enabled"] is True
    # A local panel has NO device credential and NO gateway. If either ever
    # appears here it came from somewhere this script must not invent.
    assert "deviceCredential" not in result
    assert "gatewayUrl" not in result


def test_local_mode_preserves_a_previous_registration_without_using_it(tmp_path):
    current = tmp_path / "host.json"
    current.write_text('{"enabled":true,"accessMode":"read-protected","gatewayUrl":"https://wall.invalid",'
                       '"deviceId":"panel","deviceCredential":"fixture"}', encoding="utf-8")
    current.chmod(stat.S_IRUSR | stat.S_IWUSR)
    result = MODULE.render(current, {"WALL_ACCESS_MODE": "local"})
    assert result["accessMode"] == "local"
    assert result["deviceCredential"] == "fixture", "reversal stays a one-knob edit"
    assert result["gatewayUrl"] == "https://wall.invalid"


def test_reversal_restores_gateway_mode_only_when_a_registration_is_still_there(tmp_path):
    registered = tmp_path / "registered.json"
    registered.write_text('{"enabled":true,"accessMode":"local","gatewayUrl":"https://wall.invalid",'
                          '"deviceId":"panel","deviceCredential":"fixture"}', encoding="utf-8")
    registered.chmod(stat.S_IRUSR | stat.S_IWUSR)
    back = MODULE.render(registered, {"WALL_ACCESS_MODE": "gateway"})
    assert "accessMode" not in back
    assert back["enabled"] is True

    bare = tmp_path / "bare.json"
    bare.write_text('{"enabled":true,"accessMode":"local"}', encoding="utf-8")
    bare.chmod(stat.S_IRUSR | stat.S_IWUSR)
    reverted = MODULE.render(bare, {"WALL_ACCESS_MODE": ""})
    assert "accessMode" not in reverted
    assert reverted["enabled"] is False, "no registration behind it: back to the open/unprovisioned posture"


def test_absent_knob_leaves_todays_access_half_untouched(tmp_path):
    current = tmp_path / "host.json"
    current.write_text('{"enabled":true,"accessMode":"read-protected","gatewayUrl":"https://wall.invalid",'
                       '"deviceId":"panel","deviceCredential":"fixture"}', encoding="utf-8")
    current.chmod(stat.S_IRUSR | stat.S_IWUSR)
    result = MODULE.render(current, {})
    assert result["accessMode"] == "read-protected"
    assert result["enabled"] is True


def test_an_unknown_access_mode_is_refused_rather_than_guessed(tmp_path):
    try:
        MODULE.render(tmp_path / "host.json", {"WALL_ACCESS_MODE": "localish"})
    except ValueError as error:
        assert "WALL_ACCESS_MODE" in str(error)
    else:
        raise AssertionError("an unknown access mode was accepted")


def test_wall_env_documents_the_knob_and_firstboot_reports_the_mode():
    env = (SCRIPT.parent / "wall.env.example").read_text(encoding="utf-8")
    assert "WALL_ACCESS_MODE=gateway" in env
    assert "WALL_CAMERA_ENABLED=true" in env, "face on the panel still needs the hardware gate"
    firstboot = (SCRIPT.parent / "wall-firstboot.sh").read_text(encoding="utf-8")
    assert "WALL_ACCESS_MODE" in firstboot
    # The PIN is never handled by firstboot, so it can never be echoed by it.
    assert "WALL_ACCESS_PIN" not in firstboot and "WALL_PANEL_PIN" not in firstboot


def test_reversal_needs_the_whole_registration_not_two_thirds_of_it(tmp_path):
    # Terra round 1: a gatewayUrl and a credential with no deviceId is a config
    # the panel's readConfig REFUSES, so enabling it would produce a panel that
    # is locked and unavailable while firstboot reports access as enabled.
    partial = tmp_path / "partial.json"
    partial.write_text('{"enabled":true,"accessMode":"local","gatewayUrl":"https://wall.invalid",'
                       '"deviceCredential":"fixture"}', encoding="utf-8")
    partial.chmod(stat.S_IRUSR | stat.S_IWUSR)
    result = MODULE.render(partial, {"WALL_ACCESS_MODE": "gateway"})
    assert "accessMode" not in result
    assert result["enabled"] is False
