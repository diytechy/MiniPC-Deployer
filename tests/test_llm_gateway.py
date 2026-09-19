"""Tests for the centralised LLM gateway.

Verifies: SR-043 / LLR-963, LLR-964, LLR-965
Cases:    TC-983, TC-984, TC-985, TC-986, TC-987, TC-993, TC-1006
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "stack" / "docker-compose.yml"
GW = ROOT / "stack" / "llm-gateway"
PIN = GW / "gateway-image.pin"
README = GW / "README.md"
ENVEX = GW / "llm-gateway.env.example"
AI_CLI = ROOT / "stack" / "ai-cli" / "setup-ai-cli.sh"
CADDY = ROOT / "stack" / "caddy" / "Caddyfile"
GITIGNORE = ROOT / ".gitignore"

APPROVED = "ghcr.io/tashfeenahmed/freellmapi"


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


# --- TC-983 ---------------------------------------------------------------
def test_gateway_is_profile_gated_and_publishes_no_host_port_sr043(compose):
    svc = compose["services"]["llm-gateway"]
    assert svc["profiles"] == ["llm-gateway"], "must be off by default"
    assert "ports" not in svc, (
        "no host port: this origin fronts every provider credential and is "
        "reached only through Caddy, which carries the address gate"
    )
    assert "llm_gateway_data" in compose["volumes"]


# --- TC-984 ---------------------------------------------------------------
def test_image_is_the_named_upstream_pinned_by_digest_sr043(compose):
    image = compose["services"]["llm-gateway"]["image"]
    assert image.startswith(APPROVED + "@"), (
        "only the named upstream may be referenced - this project has thousands "
        "of forks and search ranks them above the original"
    )
    assert "${LLM_GATEWAY_IMAGE_DIGEST}" in image

    pin = PIN.read_text(encoding="utf-8")
    m = re.search(r"^DIGEST=(sha256:[0-9a-f]{64})$", pin, re.M)
    assert m, "the pin must record a full sha256 digest"
    assert re.search(r"^VERSION=\S+$", pin, re.M), "a digest with no version reads as verified and is not"
    assert re.search(r"^PINNED=\d{4}-\d{2}-\d{2}$", pin, re.M)
    assert re.search(r"^IMAGE=" + re.escape(APPROVED) + r"$", pin, re.M)

    # A floating tag must be impossible to express through this reference.
    assert ":latest" not in image and ":main" not in image


# --- TC-1006 --------------------------------------------------------------
def test_the_upstream_install_script_is_not_invoked_sr043():
    """Upstream's quickstart is a piped curl-to-shell installer: unpinned, runs
    remote code at deploy time, and yields a deployment no commit describes."""
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or "build" in path.parts:
            continue
        if path.suffix not in (".sh", ".yml", ".yaml", ".service", ".py", ".ps1"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if re.search(r"freellmapi\.co/install\.sh", text):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, "the upstream installer is invoked by: %s" % offenders


# --- TC-985 ---------------------------------------------------------------
def test_env_example_declares_only_names_upstream_actually_reads_sr043():
    """The previous version of this test asserted LLM_GATEWAY_API_KEY,
    LLM_GATEWAY_ENCRYPTION_KEY and LLM_GATEWAY_ADMIN_PASSWORD were present in
    our own example file. Upstream reads none of those names. The test read the
    same file the names were invented in, so it could not have failed - the
    same shape of mistake as a check that reads a Tailscale ACL nothing on the
    box consults. It now asserts the names are ABSENT, so the invention cannot
    come back."""
    env = ENVEX.read_text(encoding="utf-8")

    for invented in ("LLM_GATEWAY_API_KEY=", "LLM_GATEWAY_ENCRYPTION_KEY=",
                     "LLM_GATEWAY_ADMIN_EMAIL=", "LLM_GATEWAY_ADMIN_PASSWORD="):
        assert not re.search(r"^%s" % re.escape(invented), env, re.M), (
            "%s is not a name upstream reads; setting it configures nothing "
            "while reading as though it does" % invented)

    # The one key that IS an environment variable, under upstream's own name.
    assert re.search(r"^ENCRYPTION_KEY=", env, re.M)

    # And the file must say why the other two are absent, because an operator
    # who finds no bearer key here will otherwise assume the gateway has none.
    low = env.lower()
    assert "unified api key" in low, "say where the bearer key actually comes from"
    assert "docker logs llm-gateway" in low, "and how to capture it before it is gone"

    # The pinned version's setup-code behaviour must be stated as the PINNED
    # version's, not as upstream's docs describe it. main documents a one-time
    # code gating first-account creation from a non-local device; v0.3.0 does
    # not implement it, verified on the running container. An operator told to
    # look for a code that never prints concludes the deploy is broken.
    assert "no setup code" in low

    # The real file must never be committable. stack/.env is covered by its own
    # rule; this one lives in a subdirectory and needed its own.
    assert "stack/llm-gateway/llm-gateway.env" in GITIGNORE.read_text(encoding="utf-8")


def test_the_data_volume_is_mounted_where_the_image_actually_writes_sr043(compose):
    """This said /app/data and the image declares VOLUME /app/server/data, so
    the wrong path did not fail: docker made an ANONYMOUS volume at the real
    path and the named one sat empty. The backup set would have captured an
    empty volume while every provider credential lived somewhere unnamed."""
    mounts = compose["services"]["llm-gateway"]["volumes"]
    assert "llm_gateway_data:/app/server/data" in mounts
    assert not any(m.endswith(":/app/data") for m in mounts)


def test_one_proxy_hop_is_declared_so_callers_are_not_one_bucket_sr043(compose):
    """Caddy is the only thing that can reach this container, so without
    TRUST_PROXY every request appears to come from the bridge address and the
    whole household shares one per-IP rate-limit bucket. `1` means one hop;
    `true` would trust any hop."""
    env = compose["services"]["llm-gateway"]["environment"]
    assert str(env["TRUST_PROXY"]) == "1"
    assert "HOST_BIND" not in env, (
        "HOST_BIND selects a host publishing interface upstream-side; we publish "
        "no port, so it bound nothing while reading as if it did")


def test_no_healthcheck_override_shadows_the_images_correct_one_sr043(compose):
    """The override here probed /health with wget: not a route this app serves,
    not a binary in this node image. It would have reported the gateway
    permanently unhealthy while the gateway was fine."""
    assert "healthcheck" not in compose["services"]["llm-gateway"]


def test_the_caddy_site_gates_by_address_and_adds_no_second_prompt_sr043():
    caddy = CADDY.read_text(encoding="utf-8")
    assert "{$LLM_GATEWAY_HOST} {" in caddy
    block = caddy.split("{$LLM_GATEWAY_HOST} {", 1)[1].split("\n}", 1)[0]
    live = "\n".join(l for l in block.splitlines() if not l.strip().startswith("#"))
    assert "remote_ip {$LAN_CIDR}" in live
    assert "reverse_proxy llm-gateway:3001" in live
    assert "403" in live, "a WAN caller must be refused before the handler"
    assert "basic_auth" not in live, (
        "the bearer key is the credential; a second prompt would break every "
        "programmatic consumer for no gain"
    )


# --- TC-986 / TC-987 ------------------------------------------------------
def test_ai_cli_bind_is_unchanged_by_this_change_sr043():
    """The risk of a deliberately wider second lane is that the first one gets
    quietly widened to match. Assert it, do not assume it."""
    src = AI_CLI.read_text(encoding="utf-8")
    m = re.search(r'AI_CLI_BIND="\$\{AI_CLI_BIND:-([^}"]+)\}"', src)
    assert m, "ai-cli's bind default must remain declared"
    assert m.group(1) == "127.0.0.1", "ai-cli must stay on loopback"


def test_the_two_lanes_share_no_credential_account_or_volume_sr043(compose):
    gw = compose["services"]["llm-gateway"]
    assert not any("ai-cli" in v or "ai_cli" in v for v in gw["volumes"])
    assert "ai-cli" not in yaml.safe_dump(gw)

    readme = README.read_text(encoding="utf-8")
    assert "departure" in readme.lower(), "the wider reach must be stated, as A40 stated its own"
    assert "never the LAN" in readme


# --- TC-993 ---------------------------------------------------------------
@pytest.mark.skip(reason=(
    "NOT YET IMPLEMENTED, and deliberately not faked. SR-044 requires a request "
    "for a local model on a cold box to be HELD and to fall back rather than "
    "error. The gateway is a third-party binary and cannot host that logic, so "
    "the hold belongs in a small OpenAI-compatible shim in front of the dev PC "
    "which the gateway's custom endpoint points at. That shim is the one "
    "outstanding piece of SR-044 and is tracked as such rather than skipped "
    "quietly."))
def test_a_cold_box_request_is_held_and_falls_back_rather_than_erroring_sr044():
    raise AssertionError("pending the holding shim")
