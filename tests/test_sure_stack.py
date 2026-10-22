"""Static contracts for Sure configuration and Owner-selected default enablement; no container execution.

Verifies: SR-060..SR-064, LLR-1040..LLR-1044, TC-1100..TC-1104.
These checks prove authored wiring, never running-image or restore behavior.
"""
from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.full, pytest.mark.smoke]


def stack():
    """Read the committed Compose template without substituting secret values."""
    return yaml.safe_load((ROOT / "stack/docker-compose.yml").read_text(encoding="utf-8"))


def env():
    """Return example knob literals; never read the private .env."""
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", (ROOT / "stack/.env.example").read_text(encoding="utf-8"), re.M))


def test_sure_profile_and_storage_sr060():
    config = stack()
    for name in ("sure-web", "sure-worker", "sure-postgres", "sure-redis"):
        service = config["services"][name]
        assert service["profiles"] == ["sure"]
        assert "ports" not in service
        assert service["healthcheck"]["test"]
        assert service["volumes"]
    # On by default (Owner, 2026-10-09) through its knob and unit, never the
    # profile list (SR-100): tests/test_sure_unit.py covers the unit.
    assert "sure" not in env()["COMPOSE_PROFILES"].split(",")
    assert env()["SURE_ENABLED"] == "true"
    assert config["networks"]["sure"]["internal"] is True
    for name in ("sure-web", "sure-worker"):
        assert config["services"][name]["depends_on"] == {
            "sure-postgres": {"condition": "service_healthy"},
            "sure-redis": {"condition": "service_healthy"},
        }


def test_sure_registry_pins_sr061():
    knobs = env()
    # The fork is a local-only image (naglight:local shape): a plain `name:local` ref,
    # never a registry or digest form that `docker load` could not restore.
    assert knobs["SURE_IMAGE"] == "sure-auroleap:local"
    assert knobs["SURE_SOURCE_REF"] == "auroleap-integration"
    assert "@" not in knobs["SURE_IMAGE"] and "ghcr.io" not in knobs["SURE_IMAGE"]
    assert "SURE_IMAGE_TAG" not in knobs  # retired 2026-10-10
    # Postgres/Redis: plain tag in compose, digest in a separate bake-time-verified knob.
    for tag, digest, prefix in (("SURE_POSTGRES_IMAGE_TAG", "SURE_POSTGRES_IMAGE_DIGEST", "16.15"),
                                ("SURE_REDIS_IMAGE_TAG", "SURE_REDIS_IMAGE_DIGEST", "8.10.2")):
        assert knobs[tag] == prefix
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", knobs[digest])
    services = stack()["services"]
    assert services["sure-web"]["image"] == services["sure-worker"]["image"] == "${SURE_IMAGE:-sure-auroleap:local}"
    assert services["sure-postgres"]["image"] == "postgres:${SURE_POSTGRES_IMAGE_TAG}"
    assert services["sure-redis"]["image"] == "redis:${SURE_REDIS_IMAGE_TAG}"
    # The local fork has no registry to watch, like naglight:local.
    for name in ("sure-web", "sure-worker"):
        assert "diun.enable=false" in services[name]["labels"]
    for name in ("sure-postgres", "sure-redis"):
        assert "diun.enable=true" in services[name]["labels"]
    exporter = (ROOT / "vmtest/export-images.sh").read_text(encoding="utf-8")
    assert "${EXTRA_PROFILES:-}" in exporter and "config --images" in exporter
    assert "sure-auroleap:*" in exporter and "SURE_POSTGRES_IMAGE_DIGEST" in exporter


def test_sure_own_login_lan_tls_sr062():
    caddy = (ROOT / "stack/caddy/Caddyfile").read_text(encoding="utf-8")
    first = next(line.strip() for line in caddy.splitlines() if line.strip() and not line.lstrip().startswith("#"))
    assert first == "{", "Caddy global options must precede all site blocks"
    site = caddy.split("{$SURE_HOST} {")[1].split("\n}", 1)[0]
    assert "remote_ip {$LAN_CIDR}" in site
    assert 'respond "Forbidden" 403' in site
    assert "reverse_proxy sure-web:3000" in site
    assert "import access_log" in site and "encode zstd gzip" in site
    assert "oauth2-proxy" not in site and "X-Forwarded-User" not in site and "basic_auth" not in site
    assert stack()["services"]["caddy"]["environment"]["SURE_HOST"] == "${SURE_SUBDOMAIN:-sure}.${DOMAIN}"
    for name in ("sure-web", "sure-worker"):
        settings = stack()["services"][name]["environment"]
        assert settings["APP_DOMAIN"] == "${SURE_SUBDOMAIN:-sure}.${DOMAIN}"
        assert settings["RAILS_FORCE_SSL"] == settings["RAILS_ASSUME_SSL"] == "true"


def test_finance_defaults_and_separate_user_sr063():
    """Prove wiring only; blanks require FA's empty-as-absent fix in its image."""
    # Compose exports blank optional values. Their runtime acceptance is an FA
    # merge-order dependency, not a claim this static contract can establish.
    service = stack()["services"]["finance-auditor"]
    settings = service["environment"]
    assert settings["DATA_SOURCE"] == "${FINANCE_DATA_SOURCE:-actual}"
    assert env()["FINANCE_DATA_SOURCE"] == "actual"
    assert settings["SURE_URL"] == "${FINANCE_SURE_URL:-http://sure-web:3000}"
    assert settings["SURE_API_KEY"] == "${FINANCE_SURE_API_KEY:-}"
    assert settings["TRACKER_FEED_USER"] == "${FINANCE_TRACKER_FEED_USER:-}"
    assert "sure" in service["networks"] and "default" in service["networks"]
    # Sure may only ever be an OPTIONAL startup dependency (SR-099): a required
    # one invalidates every project with the sure profile off (the hub's case).
    assert service["depends_on"]["sure-web"] == {"condition": "service_healthy", "required": False}
    assert settings["ACTUAL_URL"] == "http://actual:5006"
    # SR-063 must not still promise unchanged Actual dependencies: SR-099 made
    # Actual optional, and no template can satisfy both rows.
    row = next(line for line in (ROOT / "docs/requirements/system-requirements.csv")
               .read_text(encoding="utf-8").splitlines() if line.startswith("SR-063,"))
    assert "dependencies unchanged" not in row
    assert "set by SR-099" in row


def test_sure_reporting_and_ai_off_sr064():
    for name in ("sure-web", "sure-worker"):
        settings = stack()["services"][name]["environment"]
        assert not any(k.startswith("OPENAI_") for k in settings)
        assert settings["SELF_HOSTED"] == "true"
        assert settings["POSTHOG_FEEDBACK_ENABLED"] == "false"
        assert "api.github.com:127.0.0.1" in stack()["services"][name]["extra_hosts"]
        for key in ("POSTHOG_KEY", "SENTRY_DSN", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LOGTAIL_API_KEY", "LOGTAIL_INGESTING_HOST", "SKYLIGHT_AUTHENTICATION"):
            assert settings[key] == ""


def test_sure_scheduled_backup_hook_sr065():
    backup = (ROOT / "stack/backup/backup.sh").read_text(encoding="utf-8")
    common = (ROOT / "stack/backup/common.sh").read_text(encoding="utf-8")
    timer = (ROOT / "stack/backup/systemd/homehub-backup.timer").read_text(encoding="utf-8")
    assert "postgres:*) echo postgres" in common
    assert 'postgres_dump "${src#postgres:}" "$stage/ledger.dump"' in backup
    assert "OnCalendar=" in timer
    assert "OFFSITE_ENABLED=true" in backup and "offsite step was REMOVED" in backup
    assert "sure-ledger=postgres:sure-postgres" in (ROOT / "stack/backup/backup.env.example").read_text(encoding="utf-8")
