"""B14/SR-024: Door credentials, detector carriage and dark lifecycle."""

from pathlib import Path
import os
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack/autoinstall/wall"
DOOR_CREDENTIAL_FILES = {
    "host", "password", "port", "path", "username", "width", "height",
    "input-fov", "horizontal-fov", "vertical-fov", "yaw", "pitch",
    "stale-seconds", "start-seconds", "motion-enabled", "motion-calibrated",
    "motion-sample-fps",
    "motion-min-area-ratio", "motion-persistence-seconds",
    "motion-dwell-seconds", "motion-stationary-ratio", "motion-trigger-zone",
    "motion-road-zone", "motion-masks", "motion-diagnostics",
}


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
    assert "CPUQuota=80%" in unit and "MemoryMax=384M" in unit
    assert "TasksMax=64" in unit and "TimeoutStopSec=5" in unit
    assert "StandardOutput=null" in unit and "StandardError=null" in unit
    assert "EnvironmentFile=" not in unit


def test_door_broker_is_installed_and_enabled_by_firstboot():
    user_data = (WALL / "user-data").read_text(encoding="utf-8")
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    unit = (WALL / "wall-door-stream.service").read_text(encoding="utf-8")
    assert (
        "wall-door-stream.service /target/etc/systemd/system/wall-door-stream.service"
        in user_data
    )
    assert "useradd --system" in firstboot and "wall-door-stream" in firstboot
    assert "systemctl disable --now wall-door-stream.service" in firstboot
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
    assert "doorstream/motion.py" in firstboot
    assert 'DOORBELL_MOTION_ENABLED:=false' in firstboot
    assert 'DOORBELL_MOTION_CALIBRATED:=false' in firstboot
    assert 'DOORBELL_MOTION_SAMPLE_FPS:=5' in firstboot
    assert 'DOORBELL_MOTION_DIAGNOSTICS:=false' in firstboot
    assert "motion-sample-fps" in unit and "motion-trigger-zone" in unit
    assert "motion-road-zone" in unit and "motion-masks" in unit


def test_door_credential_allowlist_is_exact_and_never_mounts_wall_env():
    unit = (WALL / "wall-door-stream.service").read_text(encoding="utf-8")
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    loaded = {
        line.split("=", 1)[1].split(":", 1)[0]
        for line in unit.splitlines() if line.startswith("LoadCredential=")
    }
    assert loaded == DOOR_CREDENTIAL_FILES
    names_line = next(
        line for line in firstboot.splitlines()
        if line.startswith('_door_credential_names="')
    )
    purged = set(names_line.split('="', 1)[1].rsplit('"', 1)[0].split())
    assert purged == DOOR_CREDENTIAL_FILES
    assert "EnvironmentFile=" not in unit
    assert "/etc/wall-panel/wall.env" not in unit


def test_door_configuration_contract_is_complete_without_committed_credentials():
    env = (WALL / "wall.env.example").read_text(encoding="utf-8")
    assert "DOORBELL_RTSP_HOST=REPLACE_WITH_DOORBELL_RTSP_HOST" in env
    assert "DOORBELL_RTSP_PASSWORD=REPLACE_WITH_DOORBELL_RTSP_PASSWORD" in env
    assert "DOORBELL_RTSP_PORT=554" in env
    assert "DOORBELL_RTSP_PATH=/H.264" in env
    assert "DOORBELL_RTSP_USERNAME=admin" in env
    expected_public = {
        "DOORBELL_MOTION_ENABLED=false",
        "DOORBELL_MOTION_CALIBRATED=false",
        "DOORBELL_MOTION_SAMPLE_FPS=5",
        "DOORBELL_MOTION_MIN_AREA_RATIO=0.002",
        "DOORBELL_MOTION_PERSISTENCE_SECONDS=2",
        "DOORBELL_MOTION_DWELL_SECONDS=3",
        "DOORBELL_MOTION_STATIONARY_RATIO=0.02",
        "DOORBELL_MOTION_TRIGGER_ZONE=0,0,1,1",
        "DOORBELL_MOTION_ROAD_ZONE=0,0,0,0",
        "DOORBELL_MOTION_MASKS=",
        "DOORBELL_MOTION_DIAGNOSTICS=false",
    }
    assert expected_public <= set(env.splitlines())
    assert "rtsp://" not in env


def test_motion_is_fail_closed_and_every_public_knob_is_validated():
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    assert '_door_motion_effective=false' in firstboot
    assert '[ "$DOORBELL_MOTION_ENABLED" = "true" ]' in firstboot
    assert '[ "$DOORBELL_MOTION_CALIBRATED" = "true" ]' in firstboot
    assert 'printf \'%s\' "$_door_motion_effective" > "$_door_cred_dir/motion-enabled"' in firstboot
    for key in (
        "DOORBELL_MOTION_ENABLED", "DOORBELL_MOTION_CALIBRATED",
        "DOORBELL_MOTION_SAMPLE_FPS", "DOORBELL_MOTION_MIN_AREA_RATIO",
        "DOORBELL_MOTION_PERSISTENCE_SECONDS", "DOORBELL_MOTION_DWELL_SECONDS",
        "DOORBELL_MOTION_STATIONARY_RATIO", "DOORBELL_MOTION_TRIGGER_ZONE",
        "DOORBELL_MOTION_ROAD_ZONE", "DOORBELL_MOTION_MASKS",
        "DOORBELL_MOTION_DIAGNOSTICS",
    ):
        assert key in firstboot[firstboot.index("_door_motion_valid=1"):]
    assert "door_motion_zone_valid" in firstboot
    assert "door_motion_masks_valid" in firstboot


@pytest.mark.parametrize(("function", "value", "extra", "accepted"), [
    ("door_motion_bool_valid", "true", "", True),
    ("door_motion_bool_valid", "yes", "", False),
    ("door_motion_number_between", "1", "1 10", True),
    ("door_motion_number_between", "10.1", "1 10", False),
    ("door_motion_number_between", "nan", "0 1", False),
    ("door_motion_zone_valid", "0,0,1,1", "0", True),
    ("door_motion_zone_valid", "0.8,0,0.3,1", "0", False),
    ("door_motion_zone_valid", "0,0,0,0", "0", False),
    ("door_motion_zone_valid", "0,0,0,0", "1", True),
    ("door_motion_masks_valid", "", "", True),
    ("door_motion_masks_valid", "0,0,0.5,1;0.5,0,0.5,1", "", True),
    ("door_motion_masks_valid", "0,0,1.1,1", "", False),
])
def test_motion_config_validators_execute_boundaries(function, value, extra, accepted):
    """Run the shipped validators; a source substring is not range evidence."""
    bash = shutil.which("bash")
    if not bash:
        for candidate in (
            Path("C:/Program Files/Git/bin/bash.exe"),
            Path("C:/Program Files/Git/usr/bin/bash.exe"),
        ):
            if candidate.is_file():
                bash = str(candidate)
                break
    if not bash:
        pytest.skip("bash is required to execute the firstboot validators")
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    helpers = firstboot[
        firstboot.index("door_motion_bool_valid()"):
        firstboot.index("if ! getent group wall-door-stream")
    ]
    command = f'{function} "$VALUE" {extra}'.rstrip()
    result = subprocess.run(
        [bash, "-c", helpers + "\n" + command],
        env={**os.environ, "VALUE": value},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is accepted, result.stderr


def test_incomplete_door_payload_stops_disables_and_purges_runtime():
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    purge = firstboot[firstboot.index("purge_door_runtime()") : firstboot.index("door_motion_bool_valid()")]
    assert "systemctl disable --now wall-door-stream.service" in purge
    assert 'rm -f -- "$_door_cred_dir/$_door_cred_name"' in purge
    assert "rm -f -- /run/wall-door-stream/service.sock" in purge
    incomplete = firstboot[firstboot.index("else\n    purge_door_runtime\n    fail_step \"Door broker is incomplete") :]
    assert incomplete.index("purge_door_runtime") < incomplete.index("fail_step")


def test_door_capability_matches_the_private_application_payload():
    declaration = {
        "app": [
            "index.html", "js/main.js", "js/state-machine.js", "js/views/door.js",
            "css/shell.css", "electron/main.cjs", "electron/preload.cjs",
            "electron/door-bridge.cjs", "doorstream/service.py", "doorstream/motion.py",
        ],
        "site": [],
        "gateway": [],
    }
    assert __import__("scripts.assert_wall_capabilities", fromlist=["REQUIRED"]).REQUIRED[
        "door-stream-v1"
    ] == declaration


def test_door_motion_dependency_and_offline_carriage_contract():
    packages = (WALL / "packages.list").read_text(encoding="utf-8")
    assert "python3-opencv" in {
        line.split("#", 1)[0].strip() for line in packages.splitlines()
    }
    user_data = (WALL / "user-data").read_text(encoding="utf-8")
    assert "packages.list" in user_data and "apt-get" in user_data
    assert "doorstream/motion.py" in __import__(
        "scripts.assert_wall_capabilities", fromlist=["REQUIRED"]
    ).REQUIRED["door-stream-v1"]["app"]
    tracked = set(subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.splitlines())
    assert "stack/autoinstall/wall/packages.list" in tracked


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
    assert "systemctl start wall-door-stream.service" in start
