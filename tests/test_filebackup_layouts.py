"""The two library-backup compose services, one per store layout (SR-133).

`filebackup` is the three-mount layout the deployed kit-12 image needs and the
default of FILEBACKUP_LAYOUT; `filebackup-store` is FileBackup's single-store
layout (its plan call C44). They differ in their mounts and in nothing else: the
hardening, the cgroup, the memory ceiling and the environment are one contract
written twice, because compose cannot drop a volume by variable. This file is
what stops a hardening change from reaching one of them only (TC-1588; the
shell half, stack/backup/tests/library-backup-layout.test.sh, pins the mounts).

Implements: SR-133, LLR-1387.
"""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "stack" / "docker-compose.yml"


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
