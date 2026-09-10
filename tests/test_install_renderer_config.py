"""SR-016/SR-017: production hub install cannot publish panel secrets."""
import importlib.util
import json
import os
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "stack/panel-access/install-renderer-config.py"
SPEC = importlib.util.spec_from_file_location("install_renderer_config", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def candidate(tmp_path, config):
    env = tmp_path / "hub.env"
    env.write_text("PANEL_ACCESS_ENABLED=false\n", encoding="utf-8")
    source = tmp_path / "config.json"
    source.write_text(json.dumps(config), encoding="utf-8")
    return env, source, tmp_path / "webroot/config.json"


def test_sr017_production_installer_copies_the_exact_validated_public_config(tmp_path):
    env, source, destination = candidate(tmp_path, {"ACCESS_ENABLED": False, "POLL_SECONDS": 60})
    original = source.read_bytes()
    MODULE.install(env, source, destination)
    assert destination.read_bytes() == original
    if os.name != "nt":
        assert destination.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("secret", [
    {"FEED_TOKEN": "fixture"},
    {"HEARTBEAT_URL": "https://status.invalid/push/fixture"},
    {"nested": {"heartbeat_url": "https://status.invalid/push/fixture"}},
    {"SUBSONIC": {"user": "listener", "password": "fixture"}},
    {"nested": {"subsonic": {"USER": "listener"}}},
])
def test_sr017_production_installer_refuses_secret_site_config_without_replacing_destination(tmp_path, secret):
    env, source, destination = candidate(tmp_path, {"ACCESS_ENABLED": False, **secret})
    destination.parent.mkdir(); destination.write_text("known-safe", encoding="utf-8")
    with pytest.raises(ValueError):
        MODULE.install(env, source, destination)
    assert destination.read_text(encoding="utf-8") == "known-safe"
