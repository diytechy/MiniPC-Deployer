"""The two library-backup compose services, one per store layout (SR-133).

`filebackup` is the three-mount layout the deployed kit-12 image needs and the
default of FILEBACKUP_LAYOUT; `filebackup-store` is FileBackup's single-store
layout (its plan call C44). They differ in their mounts and in nothing else: the
hardening, the cgroup, the memory ceiling and the environment are one contract
written twice, because compose cannot drop a volume by variable. This file is
what stops a hardening change from reaching one of them only (TC-1588; the
shell half, stack/backup/tests/library-backup-layout.test.sh, pins the mounts).

The timer's knob (SR-142) sits here too: an image enables the timer, so the
service's own ExecCondition is what keeps a reimage from starting a backup. The
line is taken from the unit and run for real, against a stand-in .env.

Implements: SR-133, LLR-1387, SR-142, LLR-1438.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "stack" / "docker-compose.yml"
SERVICE = ROOT / "stack" / "backup" / "systemd" / "homehub-library-backup.service"
ENV_EXAMPLE = ROOT / "stack" / ".env.example"
USER_DATA = ROOT / "stack" / "autoinstall" / "user-data"
ENV_PATH = "/opt/homehub/stack/.env"


@pytest.fixture(scope="module")
def services():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


@pytest.mark.smoke
def test_the_two_layouts_differ_only_in_their_mounts(services):
    legacy = dict(services["filebackup"])
    store = dict(services["filebackup-store"])
    legacy.pop("volumes")
    store.pop("volumes")
    assert store == legacy


@pytest.mark.smoke
def test_neither_layout_can_start_on_up(services):
    for name in ("filebackup", "filebackup-store"):
        assert services[name]["profiles"] == ["filebackup"], name
        assert "restart" not in services[name], name
        assert "container_name" not in services[name], name


@pytest.mark.smoke
def test_each_layout_binds_its_own_config(services):
    def config_bind(name):
        return [v for v in services[name]["volumes"] if v.endswith(":/config/FileBackup.json:ro")]

    assert config_bind("filebackup") == ["/etc/homehub-backup/filebackup.json:/config/FileBackup.json:ro"]
    assert config_bind("filebackup-store") == ["/etc/homehub-backup/filebackup-store.json:/config/FileBackup.json:ro"]


def _condition():
    lines = [l for l in SERVICE.read_text(encoding="utf-8").splitlines() if l.startswith("ExecCondition=")]
    assert len(lines) == 1, lines
    m = re.fullmatch(r"ExecCondition=/bin/sh -c '(.*)'", lines[0])
    assert m, lines[0]
    return m.group(1)


@pytest.mark.smoke
def test_the_condition_comes_before_the_run_and_reads_the_stack_env():
    text = SERVICE.read_text(encoding="utf-8")
    assert text.index("ExecCondition=") < text.index("ExecStart=")
    assert ENV_PATH in _condition()


@pytest.mark.smoke
def test_the_tracked_default_is_off_and_the_image_still_enables_the_timer():
    assert re.search(r"^FILEBACKUP_TIMER_ENABLED=false$", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M)
    # The knob stands in for this line: if an image ever stopped enabling the
    # timer, the 5d assertion and TC-H-M13 would have to change with it.
    assert "systemctl enable homehub-library-backup.timer" in USER_DATA.read_text(encoding="utf-8")


@pytest.mark.smoke
@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.parametrize(
    "env, runs",
    [
        ("FILEBACKUP_TIMER_ENABLED=true\n", True),
        ("A=1\nFILEBACKUP_TIMER_ENABLED=true\nB=2\n", True),
        ("FILEBACKUP_TIMER_ENABLED=true", True),
        ("", False),
        ("OTHER=1\n", False),
        ("FILEBACKUP_TIMER_ENABLED=false\n", False),
        ("FILEBACKUP_TIMER_ENABLED=\n", False),
        ("FILEBACKUP_TIMER_ENABLED=TRUE\n", False),
        ("FILEBACKUP_TIMER_ENABLED=true-ish\n", False),
        ("#FILEBACKUP_TIMER_ENABLED=true\n", False),
        ("NOT_FILEBACKUP_TIMER_ENABLED=true\n", False),
        (None, False),
    ],
)
def test_only_exactly_true_lets_a_timer_run_through(tmp_path, env, runs):
    target = tmp_path / ".env"
    if env is not None:
        target.write_bytes(env.encode())
    done = subprocess.run(
        ["sh", "-c", _condition().replace(ENV_PATH, target.as_posix())],
        capture_output=True, timeout=30,
    )
    if runs:
        assert done.returncode == 0
    else:
        # 1..254 is systemd's clean skip; 255 would turn the unit red.
        assert 1 <= done.returncode <= 254
