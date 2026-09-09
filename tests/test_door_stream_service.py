"""B14/WSN-019: the panel image keeps Door credentials behind a local broker."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack/autoinstall/wall"


def test_door_broker_unit_is_local_unprivileged_and_credential_isolated():
    unit = (WALL / "wall-door-stream.service").read_text(encoding="utf-8")
    assert "User=wall-door-stream" in unit
    assert "SupplementaryGroups=panel" in unit
    assert "LoadCredential=wall.env:/etc/wall-panel/wall.env" in unit
    assert "--config %d/wall.env" in unit
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
    assert "enable_unit_now" in firstboot
    assert "wall-door-stream.service" in firstboot
    assert "systemctl restart wall-door-stream.service" in firstboot
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
