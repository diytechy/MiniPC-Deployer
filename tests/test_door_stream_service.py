"""B14/WSN-019: the panel image keeps Door credentials behind a local broker."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack/autoinstall/wall"


def test_door_broker_unit_is_local_unprivileged_and_credential_isolated():
    unit = (WALL / "wall-door-stream.service").read_text(encoding="utf-8")
    assert "User=wall-door-stream" in unit
    assert "SupplementaryGroups=panel" in unit
    assert "wall.env:/etc/wall-panel/wall.env" not in unit
    assert "LoadCredential=password:/run/wall-door-credentials/password" in unit
    assert "--credential-directory %d" in unit
    assert "--socket /run/wall-door-stream/service.sock" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "StandardOutput=null" in unit and "StandardError=null" in unit
    assert "EnvironmentFile=" not in unit


def test_door_broker_is_installed_and_enabled_by_firstboot():
    user_data = (WALL / "user-data").read_text(encoding="utf-8")
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    assert (
        "wall-door-stream.service /target/etc/systemd/system/wall-door-stream.service"
        in user_data
    )
    assert "useradd --system" in firstboot and "wall-door-stream" in firstboot
    assert "systemctl disable wall-door-stream.service" in firstboot
    assert "wall-door-stream.service" in firstboot
    assert "systemctl restart wall-door-stream.service" in firstboot
    assert "install -d -m 0700 -o root -g root \"$_door_cred_dir\"" in firstboot
    assert "printf '%s' \"$DOORBELL_RTSP_PASSWORD\" > \"$_door_cred_dir/password\"" in firstboot
    purge = firstboot.index('rm -f -- "$_door_cred_dir/$_door_cred_name"')
    missing = firstboot.index('if [ -z "${DOORBELL_RTSP_HOST:-}" ]')
    assert purge < missing
    assert "After=network-online.target wall-firstboot.service" not in (
        WALL / "wall-door-stream.service"
    ).read_text(encoding="utf-8")


def test_door_configuration_contract_is_complete_without_committed_credentials():
    env = (WALL / "wall.env.example").read_text(encoding="utf-8")
    assert "DOORBELL_RTSP_HOST=REPLACE_WITH_DOORBELL_RTSP_HOST" in env
    assert "DOORBELL_RTSP_PASSWORD=REPLACE_WITH_DOORBELL_RTSP_PASSWORD" in env
    assert "DOORBELL_RTSP_PORT=554" in env
    assert "DOORBELL_RTSP_PATH=/H.264" in env
    assert "DOORBELL_RTSP_USERNAME=admin" in env
    assert "rtsp://" not in env


def test_door_capability_matches_the_private_application_payload():
    declaration = {
        "app": [
            "index.html", "js/main.js", "js/state-machine.js", "js/views/door.js",
            "css/shell.css", "electron/main.cjs", "electron/preload.cjs",
            "electron/door-bridge.cjs", "doorstream/service.py",
        ],
        "site": [],
        "gateway": [],
    }
    assert __import__("scripts.assert_wall_capabilities", fromlist=["REQUIRED"]).REQUIRED[
        "door-stream-v1"
    ] == declaration


def test_every_display_off_and_suspend_path_stops_the_door_source_first():
    sleep = (WALL / "wall-sleep.sh").read_text(encoding="utf-8")
    backlight = sleep[sleep.index("backlight_set()") : sleep.index("# ── which frame")]
    off = backlight[backlight.index('if [ "$1" = "off" ]') : backlight.index("else")]
    assert off.index("stop_door_stream") < off.index("want=0")
    suspend = sleep[sleep.index("suspend_now()") : sleep.index("# write_absent_since")]
    assert suspend.index("stop_door_stream") < suspend.index("systemctl suspend")


def test_display_on_readies_the_idle_broker_without_starting_a_camera():
    sleep = (WALL / "wall-sleep.sh").read_text(encoding="utf-8")
    backlight = sleep[sleep.index("backlight_set()") : sleep.index("# ── which frame")]
    on = backlight[backlight.index("else") : backlight.index("return 0")]
    assert "start_door_broker" in on
    start = sleep[sleep.index("start_door_broker()") : sleep.index("backlight_set()")]
    assert "[ -r /run/wall-door-credentials/host ] || return 0" in start
    assert "[ -r /run/wall-door-credentials/password ] || return 0" in start
    executable = "\n".join(
        line for line in sleep.splitlines() if not line.lstrip().startswith("#")
    ).lower()
    assert "ffmpeg" not in executable and "rtsp://" not in executable
