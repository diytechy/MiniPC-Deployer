"""Tests for the pinned private route to the dev PC.

Verifies: SR-047 / LLR-985..LLR-991, and SR-044's last piece, the hold shim
Cases:    TC-1023, TC-1024, TC-1025, TC-1026, TC-1027, TC-1028, TC-1029,
          TC-1030, TC-1031

THE GUARANTEE THESE TESTS DEFEND, AND IT IS NOT THE ONE THIS FILE FIRST
CLAIMED. The first version of this docstring said the container "holds no
cloud credential and names no cloud endpoint", and concluded it therefore
cannot reach a cloud provider. THAT CONCLUSION WAS FALSE and an adversarial
review (codex gpt-5.6-terra, 2026-09-19) found it. Measured against the pinned
digest: the proxy registers 32 provider pass-through routes independently of
`model_list`, and the vertex handler forwards CALLER-SUPPLIED credentials when
it holds none of its own. A probe request with an `x-goog-api-key` header
opened a connection to aiplatform.l.rep.googleapis.com:443.

The credential can arrive IN THE REQUEST, so its absence from the config was
never a boundary. The real one is below the application:

    the container has ONE pinned address on a pinned subnet, and
    stack/llm-isolation/ programs DOCKER-USER so that address may reach the
    dev PC's inference port and nothing else.

The config-level checks below are still worth having as defence in depth, but
they are NOT the guarantee, and this file must not imply that they are.

THREE PROPERTIES OF THIS FILE ARE DELIBERATE AND SHOULD SURVIVE EDITING.

1. CHECKS ARE WHITELISTS, NOT BLACKLISTS - the permitted destination, the
   permitted routes, the permitted mounts. A blacklist of provider names would
   pass for every provider nobody thought of, and the providers nobody thought
   of are the whole problem.

2. THE ERROR-MESSAGE CHECK ASSERTS AGAINST THE OTHER SIDE OF THE CONTRACT. The
   gateway decides whether to fall back by substring-matching our error text,
   so its matcher list is vendored below with the date and path it was read
   from. A test reading our message out of our own file could never fail.

3. THE TIMING TESTS DRIVE THE REAL COROUTINE against a fake clock rather than
   grepping the source. A source-string assertion passes for any refactor that
   keeps the words and loses the behaviour - and the version of this file that
   did grep the source missed a five-second overshoot that the driven test
   caught on its first run.
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
LL = ROOT / "stack" / "litellm"
CONFIG = LL / "config.yaml"
HOOK_SRC = LL / "devpc_hold_hook.py"
PIN = LL / "litellm-image.pin"
README = LL / "README.md"
COMPOSE = ROOT / "stack" / "docker-compose.yml"
CADDY = ROOT / "stack" / "caddy" / "Caddyfile"
ENVEX = ROOT / "stack" / ".env.example"

APPROVED_IMAGE = "ghcr.io/berriai/litellm"

# The ONE destination this proxy may have, as written in config.yaml. Any other
# URL-shaped string in that file is a finding.
PERMITTED_API_BASE = "os.environ/DEVPC_OPENAI_BASE_URL"

# The placeholder that stands where a credential would go. Asserted exactly, so
# that a real key cannot be substituted without this test noticing.
EXPECTED_PLACEHOLDER_KEY = "not-a-credential-the-dev-pc-has-no-auth"


def _load_hook():
    spec = importlib.util.spec_from_file_location("devpc_hold_hook", HOOK_SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["devpc_hold_hook"] = mod
    spec.loader.exec_module(mod)
    return mod


hook = _load_hook()


@pytest.fixture(scope="module")
def cfg():
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


# --- TC-1023: one deployment, and it is the dev PC ------------------------
def test_exactly_one_deployment_and_it_is_the_dev_pc_sr046(cfg):
    """The guarantee is arithmetic: one entry cannot route to two places."""
    models = cfg["model_list"]
    assert len(models) == 1, (
        "this proxy must have exactly ONE deployment - a second one is not a "
        "fallback, it is the deletion of the reason this container exists. "
        "Found: %r" % [m.get("model_name") for m in models])

    params = models[0]["litellm_params"]
    assert params["api_base"] == PERMITTED_API_BASE, (
        "the only permitted destination is the dev PC, via %s" % PERMITTED_API_BASE)


def test_no_url_other_than_the_dev_pc_appears_anywhere_in_the_config_sr046():
    """A WHITELIST. Every URL-shaped string in the file must be the permitted
    one. Blacklisting provider hostnames would pass for every provider nobody
    thought of - and the point of this container is the providers nobody
    thought of."""
    text = CONFIG.read_text(encoding="utf-8")
    live = "\n".join(l for l in text.splitlines()
                     if not l.lstrip().startswith("#"))

    urls = re.findall(r"https?://[^\s'\"]+", live)
    assert not urls, (
        "no literal URL may appear in the live config - the one destination is "
        "supplied by the environment as %s. Found: %r"
        % (PERMITTED_API_BASE, urls))

    # And no os.environ/ reference other than the four this design uses. A new
    # one is not automatically wrong, but it is a routing-surface change and
    # must be read by a human rather than slipped in.
    refs = set(re.findall(r"os\.environ/([A-Z0-9_]+)", live))
    allowed = {"DEVPC_OPENAI_BASE_URL", "DEVPC_LITELLM_MODEL", "LITELLM_MASTER_KEY"}
    assert refs <= allowed, (
        "unexpected environment reference(s) in the routing config: %r. Each "
        "one is a potential second destination." % sorted(refs - allowed))


def test_the_api_key_slot_holds_a_declared_placeholder_not_a_credential_sr046(cfg):
    """The dev PC's inference server has no authentication (the Windows
    firewall rule scoping it to the hub is the control). The client still
    requires an Authorization header, so a placeholder goes here - in the
    clear, in a committed file, on purpose. Asserting it EXACTLY is what stops
    a real key being parked here later 'temporarily'."""
    key = cfg["model_list"][0]["litellm_params"]["api_key"]
    assert key == EXPECTED_PLACEHOLDER_KEY
    assert not key.startswith("sk-"), (
        "a placeholder that looks like a real key is worse than no placeholder")


# --- TC-1024: nothing falls anywhere --------------------------------------
FALLBACK_KEYS = (
    "fallbacks", "default_fallbacks",
    "context_window_fallbacks", "content_policy_fallbacks",
)


def test_no_fallback_of_any_kind_is_configured_sr046(cfg):
    """LiteLLM's fallbacks are OPT-IN: absent means nothing falls anywhere.
    That default is the inverse of the gateway's, where a named model is only a
    preference and the router walks past it for up to 20 attempts. Here,
    absence IS the mechanism - which is exactly why it needs a test, because an
    absent key is the kind of thing somebody adds back at 1am."""
    def walk(node, path="config"):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in FALLBACK_KEYS:
                    assert v in (None, [], {}), (
                        "%s.%s is set to %r - any fallback here can send a "
                        "finance request to a provider this container was "
                        "built to be unable to reach" % (path, k, v))
                walk(v, "%s.%s" % (path, k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, "%s[%d]" % (path, i))

    walk(cfg)


def test_the_database_is_off_so_the_config_file_is_authoritative_sr046(cfg):
    """The second half of the guarantee. With store_model_in_db ON, a
    deployment added through the admin UI or /config/update overrides this file
    and nothing in git records it - the file would still say one model while
    the process served two."""
    assert cfg["general_settings"]["store_model_in_db"] is False


def test_the_model_id_has_exactly_one_source_of_truth_sr046(cfg, compose):
    """DEVPC_TARGET_MODEL is what devpc-wake looks for in the dev PC's
    /v1/models listing to decide `ready`. If litellm names a different model,
    every wake runs its full deadline and reports `failed` - which reads as a
    broken NIC rather than as a typo. One name, one fact."""
    assert cfg["model_list"][0]["litellm_params"]["model"] == \
        "os.environ/DEVPC_LITELLM_MODEL"

    env = compose["services"]["litellm"]["environment"]
    assert env["DEVPC_LITELLM_MODEL"] == "openai/${DEVPC_TARGET_MODEL}", (
        "the openai/ prefix must be composed HERE from the single source of "
        "truth. `openai/os.environ/X` inside the YAML does not substitute - "
        "LiteLLM triggers only on a value that BEGINS with os.environ/ - so it "
        "would be sent as a literal model name and fail at call time")


def test_the_hold_hook_is_wired_as_an_instance_not_a_class_sr044(cfg):
    """LiteLLM dispatches CustomLogger INSTANCES. Naming the class is a silent
    no-op: the proxy starts, the hook never runs, and a cold box is discovered
    by a request that hangs rather than by a startup error."""
    assert cfg["litellm_settings"]["callbacks"] == \
        "devpc_hold_hook.proxy_handler_instance"
    assert hasattr(hook, "proxy_handler_instance")
    assert isinstance(hook.proxy_handler_instance, hook.DevPcHoldHook)


# --- TC-1025: the handoff to the gateway is a string match ----------------
#
# Vendored from the RUNNING gateway container on 2026-09-19:
#   docker exec llm-gateway grep -n "function isRetryableError" -A 30 \
#       /app/server/dist/routes/proxy.js
# It substring-matches the lowercased error message. Anything outside this list
# makes the gateway 502 the whole request instead of trying a cloud tier - so
# the AGENTS lane silently loses its fallback, and it looks like the gateway is
# down rather than like the dev PC is asleep.
#
# RE-READ THIS LIST WHEN stack/llm-gateway/gateway-image.pin ROTATES.
GATEWAY_RETRYABLE_SUBSTRINGS_2026_09_19 = (
    "429", "rate limit", "too many requests", "quota", "resource_exhausted",
    "aborted", "timeout", "etimedout", "econnrefused", "econnreset",
    "503", "unavailable", "500", "internal server error",
)


@pytest.mark.parametrize("template,fields", [
    (hook.HOLD_EXPIRED_MESSAGE, {"waited": 120.0, "model": "some-model"}),
    (hook.NO_ORACLE_MESSAGE, {"url": "http://example.invalid:8799"}),
])
def test_every_refusal_is_retryable_to_the_gateway_in_front_sr044(template, fields):
    """Asserted against the OTHER side of the contract. Checking that our own
    message says what our own file says is the shape of test that cannot fail."""
    msg = template.format(**fields).lower()
    hits = [s for s in GATEWAY_RETRYABLE_SUBSTRINGS_2026_09_19 if s in msg]
    assert hits, (
        "no substring of the gateway's retryable list appears in %r, so the "
        "gateway would 502 instead of falling back and the agents lane would "
        "lose its cloud tier silently" % msg)


def test_the_refusal_does_not_accidentally_claim_a_rate_limit_sr044():
    """'429' and 'quota' would also make the gateway fall back, but they would
    put the model on a rate-limit COOLDOWN - benching the dev PC for a window
    because it was asleep. Fall back, yes; lie about why, no."""
    msg = hook.HOLD_EXPIRED_MESSAGE.format(waited=120.0, model="m").lower()
    for wrong in ("429", "rate limit", "quota", "too many requests"):
        assert wrong not in msg


# --- the decision, as a table --------------------------------------------
@pytest.mark.parametrize("state,waited,expected", [
    ("ready",   0.0,   "proceed"),
    # `ready` outranks the cap: a box that became ready on the last poll should
    # serve the request, not be refused by a clock.
    ("ready",   999.0, "proceed"),
    ("off",     0.0,   "wake"),
    ("failed",  0.0,   "wake"),
    # `up` is the subtle one: awake, weights not in VRAM yet. Keep waiting.
    ("up",      0.0,   "wake"),
    # a flight already owns a deadline - joining is right, re-firing is noise
    ("waking",  0.0,   "wait"),
    ("waking",  999.0, "expired"),
    ("off",     999.0, "expired"),
    # an unrecognised state from a future devpc-wake must NOT read as fine
    ("quiesced", 0.0,  "wake"),
])
def test_the_hold_decision_table_sr044(state, waited, expected):
    assert hook.decide_hold({"state": state}, waited, 120.0) == expected


def test_no_reading_is_not_the_same_as_a_bad_reading_sr044():
    """devpc-wake not answering is a DIFFERENT fact from the dev PC not being
    ready, and merging them would let 'we cannot tell' be read as 'go ahead' -
    which costs the caller the full request timeout against a sleeping box."""
    assert hook.decide_hold(None, 0.0, 120.0) == "no-oracle"
    assert hook.decide_hold(None, 999.0, 120.0) == "no-oracle"


def test_an_empty_wake_url_disables_the_hold_and_says_so_sr044(monkeypatch):
    """Valid configuration, and the correct one while the wake path is
    unverified: a cold box then fails in seconds instead of waiting out the
    full cap. It must be explicit, not silent."""
    monkeypatch.delenv("DEVPC_WAKE_URL", raising=False)
    policy = hook.HoldPolicy(env={})
    assert policy.enabled is False

    policy2 = hook.HoldPolicy(env={"DEVPC_WAKE_URL": "http://h:8799/"})
    assert policy2.enabled is True
    assert policy2.wake_url == "http://h:8799", "trailing slash must be trimmed"


# --- TC-1027..TC-1027: the container's posture ----------------------------
def test_the_service_is_profile_gated_bridge_only_and_stateless_sr046(compose):
    svc = compose["services"]["litellm"]
    assert svc["profiles"] == ["litellm"], "must be off by default"
    assert "ports" not in svc, (
        "no host port: the only ingress is the compose bridge. This is a "
        "STRONGER posture than the gateway's, which needs one because a human "
        "uses its dashboard; this has no human operand at all")

    # No named volume anywhere, which is what makes it reproducible from the
    # repo and absent from every backup set.
    #
    # THIS USED TO CONTAIN `assert ... or True`, WHICH CANNOT FAIL, plus a
    # check that rejected only the exact name `litellm_data` and would have
    # passed for any other named volume (found by review). Both are replaced by
    # an exact whitelist of the two permitted mounts: anything else - a named
    # volume under any name, a writable mount, a directory mount, an
    # interpolated path - fails, without anyone having to have predicted it.
    assert sorted(svc["volumes"]) == sorted([
        "./litellm/config.yaml:/app/litellm/config.yaml:ro",
        "./litellm/devpc_hold_hook.py:/app/litellm/devpc_hold_hook.py:ro",
    ]), "exactly the two read-only config bind mounts, and nothing else"


def test_two_file_mounts_not_a_directory_mount_sr046(compose):
    """Replacing a bind-mounted DIRECTORY's inode detaches the mount: the
    container keeps serving the old contents while the deploy reports success.
    Two explicit files make the update surface obvious."""
    mounts = compose["services"]["litellm"]["volumes"]
    assert sorted(mounts) == sorted([
        "./litellm/config.yaml:/app/litellm/config.yaml:ro",
        "./litellm/devpc_hold_hook.py:/app/litellm/devpc_hold_hook.py:ro",
    ])


def test_the_hook_is_importable_from_the_working_directory_sr044(compose):
    """`callbacks: devpc_hold_hook.proxy_handler_instance` resolves relative to
    the process's import path. Without this the proxy starts cleanly and the
    hook never loads."""
    svc = compose["services"]["litellm"]
    assert svc["working_dir"] == "/app/litellm"
    assert "/app/litellm/config.yaml" in svc["command"]


def test_the_image_is_the_named_upstream_pinned_by_digest_sr046(compose):
    image = compose["services"]["litellm"]["image"]
    assert image.startswith(APPROVED_IMAGE + "@")
    assert "${LITELLM_IMAGE_DIGEST}" in image
    assert ":latest" not in image and ":main" not in image

    pin = PIN.read_text(encoding="utf-8")
    assert re.search(r"^DIGEST=(sha256:[0-9a-f]{64})$", pin, re.M)
    assert re.search(r"^VERSION=\S+$", pin, re.M)
    assert re.search(r"^PINNED=\d{4}-\d{2}-\d{2}$", pin, re.M)
    assert re.search(r"^IMAGE=" + re.escape(APPROVED_IMAGE) + r"$", pin, re.M)

    # The database variant would silently reintroduce Postgres, a volume and a
    # backup-set entry - all three of which this design does without.
    assert "litellm-database" not in image


def test_the_env_digest_matches_the_pin_sr046():
    """A pin nobody deploys is a document, not a control."""
    pin = PIN.read_text(encoding="utf-8")
    digest = re.search(r"^DIGEST=(sha256:[0-9a-f]{64})$", pin, re.M).group(1)
    env = ENVEX.read_text(encoding="utf-8")
    m = re.search(r"^LITELLM_IMAGE_DIGEST=(sha256:[0-9a-f]{64})$", env, re.M)
    assert m, "stack/.env.example must carry the digest the compose file reads"
    assert m.group(1) == digest, (
        "the example env and the pin disagree: %s vs %s" % (m.group(1), digest))


def test_the_healthcheck_does_not_go_red_every_night_sr046(compose):
    """A probe that reaches the dev PC would mark this container unhealthy
    whenever the box is asleep - which is the NORMAL case, and is exactly the
    mistake devpc-wake's /health deliberately avoids."""
    test = " ".join(compose["services"]["litellm"]["healthcheck"]["test"])
    assert "/health/liveliness" in test
    assert "127.0.0.1" in test or "localhost" in test
    assert "DEVPC" not in test and "11434" not in test


def test_no_caddy_site_exposes_this_proxy_sr046():
    """It has no dashboard worth reaching and its whole configuration is in
    git. An ingress would be a second way in with none of the reasoning that
    justified the gateway's."""
    caddy = CADDY.read_text(encoding="utf-8")
    live = "\n".join(l for l in caddy.splitlines()
                     if not l.strip().startswith("#"))
    assert "litellm" not in live, (
        "the litellm proxy must not be reverse-proxied; its only ingress is "
        "the compose bridge")


def test_the_two_lanes_do_not_share_a_bearer_key_sr046(compose):
    """One key across both lanes would mean a request misdirected between them
    succeeds against the wrong lane instead of 401-ing - which is the failure
    the separate key exists to make loud."""
    ll = compose["services"]["litellm"]["environment"]
    assert ll["LITELLM_MASTER_KEY"] == "${LITELLM_MASTER_KEY}"

    gw = compose["services"]["llm-gateway"]
    assert "LITELLM_MASTER_KEY" not in yaml.safe_dump(gw)
    assert "LLM_GATEWAY" not in yaml.safe_dump(
        compose["services"]["litellm"]["environment"])


def test_the_readme_states_the_departure_from_sr044_sr046():
    """The hold no longer falls back; the fallback moved one layer out. That is
    a change to WHERE a requirement is discharged, and it must be written down
    rather than absorbed silently into an implementation."""
    readme = README.read_text(encoding="utf-8").lower()
    assert "sr-044" in readme
    assert "never falls back" in readme or "does not fall back" in readme
    assert "freellmapi" in readme or "llm-gateway" in readme


# ===========================================================================
# TC-1029..TC-1031 - the findings from the 2026-09-19 adversarial review.
#
# Finding 1 was that the headline claim of this whole design was FALSE: the
# pinned image registers 32 provider pass-through routes independently of
# model_list, and the vertex handler forwards caller-supplied credentials. So
# "holds no credential" was never a boundary, because the credential can
# arrive in the request. These tests defend the boundary that actually exists.
# ===========================================================================

ISO = ROOT / "stack" / "llm-isolation"
ISO_SH = ISO / "llm-isolation.sh"
FENCE_UNIT = ISO / "homehub-llm-isolation.service"
LANE_UNIT = ISO / "homehub-litellm.service"


def _live(path):
    return "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                     if not l.lstrip().startswith("#"))


# --- TC-1029: the egress fence is the boundary ----------------------------
def test_the_lane_has_a_pinned_address_a_fence_names_sr047(compose):
    """A firewall rule needs an address it can name. A dynamically-allocated
    one moves, and a rule naming last week's address silently protects
    nothing."""
    svc = compose["services"]["litellm"]
    nets = svc["networks"]
    assert list(nets) == ["llmprivate"], (
        "the fenced container must sit on exactly one network, so there is no "
        "second interface its egress could leave by: %r" % list(nets))
    assert nets["llmprivate"]["ipv4_address"] == "${LITELLM_CONTAINER_IP}"

    subnet = compose["networks"]["llmprivate"]["ipam"]["config"][0]["subnet"]
    assert subnet == "172.28.91.0/24", "the subnet must be pinned, not allocated"

    env = ENVEX.read_text(encoding="utf-8")
    m = re.search(r"^LITELLM_CONTAINER_IP=(\d+\.\d+\.\d+\.\d+)$", env, re.M)
    assert m, "the example env must carry the address the fence will name"
    ip = m.group(1)
    assert ip.startswith("172.28.91."), "the pinned address must lie in the pinned subnet"
    assert not ip.endswith(".1"), "must not collide with the bridge gateway"


def test_the_fence_allows_the_dev_pc_and_rejects_everything_else_sr047():
    """The rule set, read as text. The ORDER is what makes it a fence: three
    correct rules in the wrong order either kill the lane or open it."""
    live = _live(ISO_SH)
    assert "DOCKER-USER" in live
    assert "ESTABLISHED,RELATED" in live, (
        "without it the proxy cannot answer its own callers and the lane is "
        "dead rather than private")
    assert "-j REJECT" in live
    assert "--dport" in live and "DEVPC_INFERENCE_PORT" in live

    # It must PROVE the order rather than trust three exit codes.
    assert "--line-numbers" in live
    assert "first_dev" in live and "first_den" in live


def test_the_fence_refuses_to_program_a_partial_rule_set_sr047():
    """A missing value must not silently yield a rule that allows more than
    intended. And a hostname must be refused outright: iptables resolves a name
    ONCE at program time, so the rule outlives the answer."""
    live = _live(ISO_SH)
    assert "refusing to fence nothing" in live
    assert "is not a literal IPv4 address" in live
    assert live.count("die ") >= 5


def test_the_container_cannot_start_outside_the_fence_sr047(compose):
    """restart:no plus a unit that Requires= the fence. Ordering alone would
    start the proxy when the fence had FAILED - the lesson both sibling fences
    in this stack record after getting it wrong."""
    svc = compose["services"]["litellm"]
    assert svc["restart"] == "no", (
        "a restart policy lets the runtime start this at boot, after a daemon "
        "restart or after a crash, with the fence not yet programmed")

    lane = LANE_UNIT.read_text(encoding="utf-8")
    assert "Requires=homehub-llm-isolation.service" in lane
    assert "After=homehub-llm-isolation.service" in lane
    assert "--profile litellm" in lane

    # LIVE LINES ONLY. The first draft matched the unit's own comment, which
    # says "Deliberately NO Requires=docker.service" - so it asserted against
    # prose describing the behaviour instead of the behaviour.
    fence = _live(FENCE_UNIT)
    assert "Before=docker.service" in fence, (
        "After=docker would leave a window on EVERY boot in which the proxy is "
        "up and unfenced")
    assert "Requires=docker.service" not in fence


def test_the_profile_is_never_named_in_compose_profiles_sr047():
    """firstboot runs a bulk `docker compose up -d`. A profiled-in service
    would start outside the fence on every boot - the gunmaster3 defect."""
    env = ENVEX.read_text(encoding="utf-8")
    m = re.search(r"^COMPOSE_PROFILES=(.*)$", env, re.M)
    if m:
        assert "litellm" not in m.group(1), (
            "the private lane must be started by its unit, never by a profile")
    assert "LITELLM_ENABLED=false" in env, "off by default"


# --- TC-1030: defence in depth, declared as such --------------------------
def test_the_route_allowlist_is_a_whitelist_and_excludes_pass_through_sr047(cfg):
    """MEASURED against the pinned digest: 618 routes, 32 of them provider
    pass-throughs registered independently of model_list. This allowlist
    removes them. It is IN-PROCESS POLICY and the config says so - the
    guarantee is the fence."""
    allowed = cfg["general_settings"]["allowed_routes"]
    assert allowed, "an empty allowlist means every route is served"
    for path in allowed:
        assert path.startswith("/"), path
    for forbidden in ("/vertex_ai", "/gemini", "/anthropic", "/bedrock",
                      "/cohere", "/mistral", "/azure", "/vllm",
                      "/openai_passthrough"):
        assert not any(p.startswith(forbidden) for p in allowed), forbidden

    text = CONFIG.read_text(encoding="utf-8").lower()
    assert "defence in depth" in text or "defense in depth" in text
    assert "the guarantee is the egress fence" in text, (
        "the config must say which of the two controls is authoritative, or "
        "the next reader will trust the weaker one")


# --- TC-1031: the hold's timing cannot hang or overshoot ------------------
def test_non_finite_and_zero_timings_are_refused_at_startup_sr044():
    """nan is the silent one: every comparison against it is False, so the
    hold would never expire and the request would hang instead of failing.
    Zero poll spins a hot loop inside the request path."""
    for bad in ("nan", "inf", "-1", "not-a-number"):
        with pytest.raises(SystemExit):
            hook.HoldPolicy(env={"DEVPC_WAKE_URL": "http://h:1",
                                 "DEVPC_HOLD_CAP_SECONDS": bad})
    for bad in ("0", "nan", "-2"):
        with pytest.raises(SystemExit):
            hook.HoldPolicy(env={"DEVPC_WAKE_URL": "http://h:1",
                                 "DEVPC_HOLD_POLL_SECONDS": bad})
    # A zero CAP is legitimate - "ask once, do not wait".
    p = hook.HoldPolicy(env={"DEVPC_WAKE_URL": "http://h:1",
                             "DEVPC_HOLD_CAP_SECONDS": "0"})
    assert p.hold_cap_seconds == 0


def test_the_hold_returns_the_moment_the_box_is_ready_sr044(monkeypatch):
    """The other side of the same coroutine: it must not keep waiting once the
    answer arrives, and it must fire exactly ONE wake however many polls it
    takes (devpc-wake is single-flight; repeated packets are noise that hides
    the one that mattered)."""
    import asyncio as _aio

    now = {"t": 0.0}
    monkeypatch.setattr(hook.time, "monotonic", lambda: now["t"])

    async def fake_sleep(sec):
        now["t"] += sec

    monkeypatch.setattr(_aio, "sleep", fake_sleep)

    h = hook.DevPcHoldHook(hook.HoldPolicy(env={
        "DEVPC_WAKE_URL": "http://oracle.invalid:8799",
        "DEVPC_HOLD_CAP_SECONDS": "120",
        "DEVPC_HOLD_POLL_SECONDS": "3",
        "DEVPC_HOLD_PROBE_TIMEOUT_SECONDS": "5",
        "DEVPC_TARGET_MODEL": "some-model",
    }))

    calls = {"probe": 0, "wake": 0}
    states = ["off", "waking", "waking", "up", "ready"]

    def probe(timeout=None):
        i = min(calls["probe"], len(states) - 1)
        calls["probe"] += 1
        now["t"] += 0.2
        return {"state": states[i]}

    def wake(timeout=None):
        calls["wake"] += 1

    monkeypatch.setattr(h, "_get_state", probe)
    monkeypatch.setattr(h, "_fire_wake", wake)

    sentinel = {"model": "local-devpc"}
    loop = _aio.new_event_loop()
    try:
        out = loop.run_until_complete(
            h.async_pre_call_hook(None, None, sentinel, "completion"))
    finally:
        loop.close()

    assert out is sentinel, "the hook must pass the request through untouched"
    assert calls["wake"] == 1, (
        "exactly one wake per request; got %d" % calls["wake"])
    assert calls["probe"] == len(states), (
        "it must stop polling AT ready, not keep going")
    assert now["t"] < 120.0, "returned well inside the cap"


def test_the_refusal_is_an_http_exception_with_a_503_status_sr044():
    """A bare RuntimeError goes through the proxy's generic handler, whose body
    is 'Internal server error' - no 503, no 'unavailable' - and the agents lane
    would lose its fallback silently. Asserted on the object here, and on the
    WIRE by the integration check the README records as still owed."""
    exc = hook._refusal("503 dev PC unavailable: test")
    assert getattr(exc, "status_code", None) == 503
    assert "unavailable" in getattr(exc, "detail", "")


# ===========================================================================
# TC-1032..TC-1033 - round 2 of the adversarial review.
#
# Round 2 found four more. The two that matter most here: the fence left the
# HOST BRIDGE entirely open (DOCKER-USER never sees host-directed packets, and
# the sibling INPUT fence covers a DIFFERENT subnet), and the "hard" cap could
# still overshoot because the probe FLOOR and the unclamped wake POST were
# both outside the deadline arithmetic.
#
# The cap test below is written so it WOULD have caught that: no tolerance
# slop, and a fake wake that actually consumes time - the previous version's
# wake was free, which is precisely why it passed.
# ===========================================================================


# --- TC-1032: the host-directed half of the fence -------------------------
def test_the_fence_also_closes_the_host_bridge_sr047():
    """DOCKER-USER does not see packets addressed to the host's own bridge
    address - they are delivered locally, which is INPUT. The first version of
    this fence wrote only the DOCKER-USER half and asserted the host half was
    covered by game-isolation.sh; that fence covers 172.28.90.0/24 and says
    nothing about this lane. Measured on the live box: INPUT held a REJECT for
    the game subnet and NOTHING for 172.28.91.x, so this container could reach
    SSH, cockpit, and the DNS admin console that can rewrite every name the
    household resolves."""
    live = _live(ISO_SH)
    assert "-L INPUT" in live or "iptables -I INPUT" in live, (
        "the host-directed half must exist, not be delegated to a fence that "
        "covers a different subnet")
    assert "host-allow-wake" in live and "host-deny" in live
    assert "BRIDGE_GW" in live, (
        "the allow rule must name the bridge gateway specifically, not the "
        "whole host")


def test_the_input_rules_go_below_ts_input_and_above_every_blanket_accept_sr047():
    """Two opposite constraints, both measured on the live chain. Inserting at
    the head would put this fence above Tailscale's `ts-input` jump, which is
    what admits the tunnel at all. Appending would put it BELOW
    `-p tcp --dports 21115:21119 -j ACCEPT`, which the remote-desktop fence
    installs for ANY source - so those ports would bypass it entirely."""
    live = _live(ISO_SH)
    assert "ts-input" in live, "the insertion point must be found, not hard-coded"
    assert "TS_POS" in live and "INS=" in live
    # And the script must PROVE no ACCEPT precedes its REJECT.
    assert "other_accept" in live, (
        "a rule that is correct but sits below a blanket ACCEPT protects "
        "nothing, and nothing but a read-back can tell you that")
    assert "an ACCEPT rule precedes this fence" in live


def test_the_input_rules_are_inserted_in_reverse_so_the_order_comes_out_right_sr047():
    """Three `-I` at the same index apply in reverse. Writing them in forward
    order silently inverts them, and an inverted fence blocks the one thing it
    means to allow - the readiness probe - while looking correctly installed."""
    live = _live(ISO_SH)
    i_deny = live.index('"$MARKER host-deny"')
    i_allow = live.index('"$MARKER host-allow-wake"')
    i_est = live.index('"$MARKER host-established"')
    # In the SOURCE the deny is written first, so that after three inserts at
    # the same index the established rule ends up on top.
    assert i_deny < i_allow < i_est, (
        "insertion order must be reversed relative to the intended rule order")
    assert "INPUT rules are out of order" in live, "and it must be verified"


# --- TC-1033: the knobs actually reach the container ----------------------
def test_every_hold_knob_declared_is_actually_passed_to_the_container_sr044(compose):
    """DEVPC_HOLD_POLL_SECONDS and DEVPC_HOLD_PROBE_TIMEOUT_SECONDS were
    declared in .env.example and read by HoldPolicy but never passed in, so
    the container silently used the Python defaults and turning either knob
    changed nothing while reading as though it did. Found by review, not by
    anything failing - which is why this test compares the two SIDES rather
    than either one against itself."""
    env = compose["services"]["litellm"]["environment"]
    src = HOOK_SRC.read_text(encoding="utf-8")
    read_by_hook = set(re.findall(r'env\.get\("(DEVPC_[A-Z_]+)"', src))
    assert read_by_hook, "expected the hook to read some DEVPC_ knobs"
    missing = sorted(k for k in read_by_hook if k not in env)
    assert not missing, (
        "the hook reads %s but compose does not pass them, so the container "
        "uses hard-coded defaults while stack/.env appears to control them"
        % missing)


def test_the_readiness_url_agrees_with_the_subnet_and_the_fence_sr044(compose):
    """TC-1034. THREE ARTIFACTS MUST NAME THE SAME ADDRESS, and this is a
    cross-artifact check rather than a restatement of any one of them.

    Two wrong answers preceded this. `host.docker.internal` is not provided by
    Docker Engine on Ubuntu, so the probe failed name resolution. The fix -
    `extra_hosts: host.docker.internal:host-gateway` - was ALSO wrong, because
    host-gateway resolves to the DEFAULT bridge's gateway (docker0, measured
    as 172.17.0.1 on this hub), not to this network's .1. The probe would then
    have been rejected by our OWN INPUT fence, which permits 172.28.91.1
    alone. Both halves individually correct; the pair wrong - which is exactly
    what a test of either half alone cannot see.
    """
    svc = compose["services"]["litellm"]
    assert "extra_hosts" not in svc, (
        "host-gateway points at the DEFAULT bridge, not this network's "
        "gateway, so the mapping sends the probe somewhere the fence rejects")

    subnet = compose["networks"]["llmprivate"]["ipam"]["config"][0]["subnet"]
    expected_gw = subnet.rsplit(".", 1)[0] + ".1"

    env = ENVEX.read_text(encoding="utf-8")
    m = re.search(r"^#\s+DEVPC_WAKE_URL=http://([0-9.]+):(\d+)\s*$", env, re.M)
    assert m, "the example env must recommend a literal readiness address"
    assert m.group(1) == expected_gw, (
        "the recommended readiness host %s is not the .1 of the pinned subnet "
        "%s - the fence permits only the latter" % (m.group(1), subnet))
    assert "host.docker.internal:host-gateway" not in env or "wrong" in env.lower()

    # And the fence must derive the same address from the same place.
    iso = ISO_SH.read_text(encoding="utf-8")
    assert "BRIDGE_GW=" in iso
    assert r"sed 's/\.[0-9]*$/.1/'" in iso, (
        "the fence must derive the gateway as the .1 of the container's own "
        "pinned subnet, so it cannot drift from the address above")


def test_a_probe_timeout_below_the_first_probe_floor_is_refused_sr044():
    """The first probe is floored so a zero-budget cap still gets one honest
    look. A configured timeout BELOW that floor therefore makes the documented
    bound (cap + probe_timeout) false - with cap=0 and probe=0.01 the first
    probe may take 0.05. Rejecting beats widening the bound: a sub-50ms probe
    across a bridge is not a setting anyone wants, and a bound with a hidden
    floor in it is one nobody can reason about."""
    with pytest.raises(SystemExit):
        hook.HoldPolicy(env={"DEVPC_WAKE_URL": "http://h:1",
                             "DEVPC_HOLD_PROBE_TIMEOUT_SECONDS": "0.01"})
    # At the floor exactly is fine.
    p = hook.HoldPolicy(env={"DEVPC_WAKE_URL": "http://h:1",
                             "DEVPC_HOLD_PROBE_TIMEOUT_SECONDS":
                                 str(hook.FIRST_PROBE_FLOOR)})
    assert p.probe_timeout == hook.FIRST_PROBE_FLOOR


def test_the_hold_cap_of_zero_asks_once_rather_than_refusing_blind_sr044(monkeypatch):
    """A zero cap is documented as 'ask once, do not wait'. It refused without
    probing at all, so a dev PC that was sitting there READY was turned away -
    the documented semantics and the behaviour disagreed."""
    import asyncio as _aio
    now = {"t": 0.0}
    monkeypatch.setattr(hook.time, "monotonic", lambda: now["t"])

    async def fake_sleep(sec):
        now["t"] += sec
    monkeypatch.setattr(_aio, "sleep", fake_sleep)

    h = hook.DevPcHoldHook(hook.HoldPolicy(env={
        "DEVPC_WAKE_URL": "http://oracle.invalid:8799",
        "DEVPC_HOLD_CAP_SECONDS": "0",
        "DEVPC_TARGET_MODEL": "m",
    }))
    probes = {"n": 0}

    def probe(timeout=None):
        probes["n"] += 1
        return {"state": "ready"}
    monkeypatch.setattr(h, "_get_state", probe)
    monkeypatch.setattr(h, "_fire_wake", lambda timeout=None: None)

    sentinel = {"model": "local-devpc"}
    loop = _aio.new_event_loop()
    try:
        out = loop.run_until_complete(
            h.async_pre_call_hook(None, None, sentinel, "completion"))
    finally:
        loop.close()
    assert out is sentinel
    assert probes["n"] == 1, "exactly one probe, and it must happen"


@pytest.mark.parametrize("cap,poll,probe_to", [
    (120.0, 3.0, 5.0),
    (0.01, 3.0, 5.0),    # the case that exposed the probe FLOOR
    (7.0, 2.0, 9.0),     # probe timeout larger than the cap
    (1.0, 0.25, 0.5),
    (0.0, 1.0, 0.05),   # probe timeout exactly AT the floor
])
def test_the_hold_never_exceeds_cap_plus_one_probe_sr044(monkeypatch, cap, poll, probe_to):
    """THE HONEST BOUND, ASSERTED WITHOUT SLOP.

    One readiness check always happens (so a zero cap still serves a ready
    box), and everything after it is inside the cap. The bound is therefore
    `cap + one probe timeout` and nothing looser. The previous version of this
    test allowed a flat 0.5s tolerance and used a fake wake that consumed NO
    time, which is exactly why it passed while a slow wake POST near expiry
    could add nearly a full probe timeout on top.
    """
    import asyncio as _aio
    now = {"t": 0.0}
    monkeypatch.setattr(hook.time, "monotonic", lambda: now["t"])

    async def fake_sleep(sec):
        assert sec >= 0, "a negative sleep is a sign the clamp went wrong"
        now["t"] += sec
    monkeypatch.setattr(_aio, "sleep", fake_sleep)

    h = hook.DevPcHoldHook(hook.HoldPolicy(env={
        "DEVPC_WAKE_URL": "http://oracle.invalid:8799",
        "DEVPC_HOLD_CAP_SECONDS": str(cap),
        "DEVPC_HOLD_POLL_SECONDS": str(poll),
        "DEVPC_HOLD_PROBE_TIMEOUT_SECONDS": str(probe_to),
        "DEVPC_TARGET_MODEL": "m",
    }))

    def slow_probe(timeout=None):
        now["t"] += (timeout or 0.0)          # burns its WHOLE allowance
        return {"state": "off"}               # never ready

    def slow_wake(timeout=None):
        now["t"] += (timeout or 0.0)          # so does the wake POST
    monkeypatch.setattr(h, "_get_state", slow_probe)
    monkeypatch.setattr(h, "_fire_wake", slow_wake)

    loop = _aio.new_event_loop()
    try:
        with pytest.raises(Exception) as exc:
            loop.run_until_complete(h.async_pre_call_hook(
                None, None, {"model": "local-devpc"}, "completion"))
    finally:
        loop.close()

    detail = getattr(exc.value, "detail", str(exc.value))
    assert "503" in detail

    bound = cap + probe_to
    assert now["t"] <= bound, (
        "cap=%s probe=%s: stopped at %.4fs, past the honest bound of %.4fs"
        % (cap, probe_to, now["t"], bound))


# ===========================================================================
# TC-1035 - round 4. The reviewer's diagnosis here was WRONG (it claimed the
# target column is $4; measured on the hub with `-n -L --line-numbers` it is
# $2, and $4=="ACCEPT" matches nothing, so its proposed fix would have
# silently disabled both runtime checks). But the WORRY was right: every
# assertion about the fence's ordering logic was a source-text grep, so a
# parser that matched nothing would have left those checks vacuous and the
# suite green.
#
# These tests DRIVE the script's awk expressions against captured iptables
# output instead of reading the script. They run on the fixture, not on the
# host's firewall, so they need no privileges and no hub.
# ===========================================================================

import shutil
import subprocess

# Captured verbatim from the hub, 2026-09-19, `iptables -n -L INPUT
# --line-numbers`. The trailing whitespace is part of iptables' real output
# and is kept deliberately - stripping it would make the fixture kinder than
# the thing it stands for.
_IPTABLES_FIXTURE = (
    "Chain INPUT (policy ACCEPT)\n"
    "num  target     prot opt source               destination         \n"
    "1    ts-input   0    --  0.0.0.0/0            0.0.0.0/0           \n"
    "2    REJECT     0    --  172.28.90.0/24       0.0.0.0/0            "
    "/* homehub-game-isolation */ reject-with icmp-port-unreachable\n"
    "3    ACCEPT     6    --  0.0.0.0/0            0.0.0.0/0            "
    "multiport dports 21115,21116,21117,21118,21119 /* homehub-rustdesk-isolation */\n"
    "4    ACCEPT     6    --  192.168.117.0/24     0.0.0.0/0            "
    "multiport dports 21115,21116,21117,21118,21119 /* homehub-rustdesk-isolation */\n"
    "5    REJECT     6    --  0.0.0.0/0            0.0.0.0/0            "
    "multiport dports 21115,21116,21117,21118,21119 /* homehub-rustdesk-isolation */ "
    "reject-with icmp-port-unreachable\n"
)

# The same chain as it would look with `-v`, where the target REALLY is $4.
# Present so the guard below is tested against the format that would break it.
_IPTABLES_FIXTURE_V = (
    "Chain INPUT (policy ACCEPT 5973K packets, 7545M bytes)\n"
    "num   pkts bytes target     prot opt in     out     source               destination\n"
    "1     212K  393M ts-input   0    --  *      *       0.0.0.0/0            0.0.0.0/0\n"
    "2     1234  567K ACCEPT     6    --  *      *       0.0.0.0/0            0.0.0.0/0\n"
)

_BASH = shutil.which("bash")
_needs_bash = pytest.mark.skipif(_BASH is None, reason="no bash on this host")


def _awk(fixture, expr):
    """Run one of the script's awk expressions against captured output."""
    return subprocess.run([_BASH, "-c", "awk %r" % expr],
                          input=fixture, capture_output=True, text=True).stdout


@_needs_bash
def test_the_target_column_assumption_holds_for_the_real_format_sr047():
    """`iptables -n -L --line-numbers` prints `num target prot opt src dst`, so
    the target is $2. A review round asserted it was $4 and proposed changing
    it; against the real output $4 matches NOTHING, so that change would have
    made the insertion point and the blanket-ACCEPT check both vacuous while
    every source-text test stayed green."""
    assert _awk(_IPTABLES_FIXTURE, '$2=="ts-input"{print $1; exit}').strip() == "1"
    assert _awk(_IPTABLES_FIXTURE, '$4=="ts-input"{print $1; exit}').strip() == ""
    assert len(_awk(_IPTABLES_FIXTURE, '$2=="ACCEPT"').splitlines()) == 2
    assert _awk(_IPTABLES_FIXTURE, '$4=="ACCEPT"').strip() == ""


@_needs_bash
def test_the_parse_guard_would_catch_a_shifted_target_column_sr047():
    """The reviewer's diagnosis was wrong; the hazard it named was not. If the
    format ever gains `-v`, $2 stops being the target and every ordering check
    silently matches nothing. The script therefore counts its OWN just-inserted
    ACCEPT rules through the same parse and dies if it cannot find them."""
    marked = _IPTABLES_FIXTURE + (
        "6    ACCEPT     6    --  172.28.91.10         172.28.91.1          "
        "tcp dpt:8799 /* homehub-llm-isolation host-allow-wake */\n"
        "7    ACCEPT     0    --  172.28.91.10         0.0.0.0/0            "
        "ctstate RELATED,ESTABLISHED /* homehub-llm-isolation host-established */\n")
    found = [l for l in _awk(marked, '$2=="ACCEPT"').splitlines()
             if "homehub-llm-isolation" in l]
    assert len(found) == 2, "the guard must see both of our ACCEPT rules"

    # With -v the same parse finds none of ours, which is what the guard trips on.
    found_v = [l for l in _awk(_IPTABLES_FIXTURE_V, '$2=="ACCEPT"').splitlines()
               if "homehub-llm-isolation" in l]
    assert len(found_v) == 0

    live = _live(ISO_SH)
    assert "_parsed_accepts" in live and "-ge 2" in live, (
        "the script must count its own rules through the same parse rather "
        "than trusting the column index")


@_needs_bash
def test_the_blanket_accept_check_finds_the_rustdesk_rule_sr047():
    """The concrete case the check exists for: appending would put our REJECT
    below `-p tcp --dports 21115:21119 -j ACCEPT`, which matches ANY source."""
    # Pretend our deny landed at position 6, i.e. appended after everything.
    out = _awk(_IPTABLES_FIXTURE, '$1+0>0 && $1+0<6 && $2=="ACCEPT"')
    assert "21115" in out, (
        "the check must see the blanket rustdesk ACCEPT when our rule is "
        "below it - that is the whole point of the check")
    # And when we are inserted at position 2, nothing precedes us.
    out2 = _awk(_IPTABLES_FIXTURE, '$1+0>0 && $1+0<2 && $2=="ACCEPT"')
    assert out2.strip() == ""


def test_the_fence_validates_the_deployed_wake_url_not_just_the_example_sr047():
    """TC-1034 can only see .env.example. A real stack/.env can name the
    DEFAULT bridge (172.17.0.1, where host-gateway resolves) while every
    file-level assertion passes; the fence would then deny the readiness probe
    and the hold would report 'no oracle' for ever. So the deployed value is
    checked where both halves are in hand."""
    live = _live(ISO_SH)
    assert "WAKE_URL=" in live and "_env DEVPC_WAKE_URL" in live
    assert "The only accepted value is" in live, (
        "it must refuse by whitelist, not warn and not interpret")
    assert "the hold is disabled" in live, (
        "and empty must remain the deliberate disabled case, not an error")


def test_the_ipv6_section_does_what_its_heading_says_sr047():
    """It used to be headed 'deny outright' and then install nothing - a
    heading claiming a control the code did not implement. It now verifies the
    premise (the network has no IPv6) instead of programming a rule for
    traffic that cannot exist, and fails closed if that premise breaks."""
    live = _live(ISO_SH)
    assert "enable_ipv6" in live, "the premise must be verified, not assumed"
    assert "programs IPv4 only" in live and "die" in live
    # HEADING LINES ONLY. The first version of this assertion scanned the
    # whole file and matched the sentence that EXPLAINS the old heading - a
    # test failing on its own changelog. The claim under test is about what
    # the section is titled, so look only at section titles.
    headings = [l for l in ISO_SH.read_text(encoding="utf-8").splitlines()
                if l.startswith("# -- ")]
    assert not any("deny outright" in h for h in headings), (
        "the old heading promised what the code did not do")


# ===========================================================================
# TC-1036 - round 5. Four more, all runtime rather than textual:
#   * the FORWARD -> DOCKER-USER jump was only ensured when we CREATED the
#     chain, so a detached-but-existing chain passed every check;
#   * the IPv6 "verification" ran `docker network inspect` from a unit that is
#     Before=docker.service, so it could only ever return unknown;
#   * the wake-URL parser accepted no-scheme, ftp://, and https-without-port;
#   * .env.example and the README still told operators to enable the lane via
#     COMPOSE_PROFILES, which is the one route that starts it unfenced.
# ===========================================================================


def test_the_forward_jump_is_verified_even_when_the_chain_already_exists_sr047():
    """A DOCKER-USER chain that exists but is DETACHED from FORWARD passes
    every rule and order check while consulting nothing. The jump check must
    therefore sit OUTSIDE the branch that creates the chain."""
    live = _live(ISO_SH)
    create_at = live.index("iptables -N DOCKER-USER")
    jump_at = live.index("iptables -C FORWARD -j DOCKER-USER")
    # The jump check must not be inside the `if ! ... DOCKER-USER` block that
    # precedes it; the `fi` between them is the proof.
    between = live[create_at:jump_at]
    assert "\nfi\n" in between, (
        "the FORWARD jump check must run unconditionally, not only when this "
        "script is the thing that created the chain")
    assert "_fwd_before" in live, (
        "and a jump below an earlier ACCEPT is a jump that is never reached")


@_needs_bash
def test_the_forward_precedence_check_finds_an_earlier_accept_sr047():
    """Driven, not grepped: the same awk the script uses, against a FORWARD
    chain whose jump sits below a blanket ACCEPT."""
    fwd = (
        "Chain FORWARD (policy DROP)\n"
        "num  target       prot opt source               destination\n"
        "1    ACCEPT       0    --  0.0.0.0/0            0.0.0.0/0\n"
        "2    DOCKER-USER  0    --  0.0.0.0/0            0.0.0.0/0\n")
    jump = _awk(fwd, '$2=="DOCKER-USER"{print $1; exit}').strip()
    assert jump == "2"
    bad = _awk(fwd, '$1+0>0 && $1+0<2 && ($2=="ACCEPT"||$2=="RETURN")')
    assert bad.strip(), "an ACCEPT above the jump must be detected"

    good = (
        "Chain FORWARD (policy DROP)\n"
        "num  target       prot opt source               destination\n"
        "1    DOCKER-USER  0    --  0.0.0.0/0            0.0.0.0/0\n"
        "2    ACCEPT       0    --  0.0.0.0/0            0.0.0.0/0\n")
    assert _awk(good, '$1+0>0 && $1+0<1 && ($2=="ACCEPT"||$2=="RETURN")').strip() == ""


def test_the_ipv6_premise_is_checked_somewhere_it_can_actually_run_sr047():
    """`docker network inspect` cannot work from a unit ordered
    Before=docker.service: it returns unknown, and the branch meant to fail
    closed logs a shrug. The compose FILE is readable pre-docker and is the
    source of truth, so that is what is parsed."""
    live = _live(ISO_SH)
    assert "docker network inspect" not in live, (
        "this unit runs before docker; inspecting a live network here can only "
        "ever return unknown, which is a check that reads correct and runs "
        "vacuously")
    assert "COMPOSE_FILE" in live and "enable_ipv6" in live
    assert "programs IPv4 only" in live, "and it must die, not warn"


@_needs_bash
@pytest.mark.parametrize("url,accepted", [
    ("http://172.28.91.1:8799",       True),
    ("http://172.28.91.1:8799/",      True),
    ("172.28.91.1:8799",              False),   # no scheme
    ("ftp://172.28.91.1:8799",        False),   # wrong scheme
    ("https://172.28.91.1",           False),   # would connect to 443, read as 80
    ("http://172.17.0.1:8799",        False),   # the DEFAULT bridge - the host-gateway trap
    ("http://user@172.28.91.1:8799",  False),   # userinfo
    ("http://[::1]:8799",             False),   # v6 literal
    ("http://172.28.91.1:8798",       False),   # right host, wrong port
])
def test_the_wake_url_whitelist_accepts_exactly_one_shape_sr047(url, accepted):
    """A WHITELIST OF ONE SHAPE. The first parser stripped any `[a-z]*://` and
    defaulted a missing port to 80, so it accepted a bare host:port, accepted
    ftp://, and read `https://host` as port 80 while the hook would connect to
    443 and be rejected by this very fence."""
    script = (
        'BRIDGE_GW=172.28.91.1; WAKE_PORT=8799; WAKE_URL="$1"\n'
        '_want="http://${BRIDGE_GW}:${WAKE_PORT}"\n'
        'case "$WAKE_URL" in "$_want"|"$_want/") echo ok;; *) echo refuse;; esac\n')
    out = subprocess.run([_BASH, "-c", script, "_", url],
                         capture_output=True, text=True).stdout.strip()
    assert (out == "ok") is accepted, "%s -> %s" % (url, out)


def test_no_document_tells_an_operator_to_enable_the_lane_by_profile_sr047():
    """THE CONTRADICTION THAT WAS LIVE FOR SEVERAL REVISIONS. The compose
    banner and the unit both say the profile route is forbidden, while
    .env.example and the README still said to use it - and that route starts
    the container through firstboot's bulk `up -d`, before the fence has
    applied in that boot. An operator following the documentation would have
    produced exactly the unfenced proxy this design exists to prevent."""
    import itertools
    for path in (ENVEX, README, CONFIG, ISO_SH):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "COMPOSE_PROFILES" not in line or "litellm" not in line:
                continue
            # A mention is fine only if it is telling you NOT to.
            assert any(w in line.upper() for w in ("NEVER", "NOT ", "FORBID",
                                                   "MUST NOT", "ABSENT")), (
                "%s appears to instruct enabling the lane by profile: %r"
                % (path.name, line.strip()))


def test_the_fence_is_started_not_merely_enabled_at_firstboot_sr047():
    """`enable` arranges the NEXT boot. The bulk `docker compose up -d` happens
    later in the SAME boot, so a merely-enabled fence has programmed nothing
    when anything could start the lane."""
    fb = (ROOT / "stack" / "autoinstall" / "firstboot.sh").read_text(encoding="utf-8")
    # ANCHOR ON THE CALL, NOT THE FIRST MENTION. The first occurrence of the
    # unit name is in the explanatory banner above the code, so a window around
    # it contained only prose - the same mistake the devpc-wake ordering test
    # made and had to be corrected for.
    assert "systemctl enable --now homehub-llm-isolation.service" in fb, (
        "the fence must be started in this boot, not just enabled for the "
        "next: firstboot's bulk `docker compose up -d` runs later in the SAME "
        "boot, and a merely-enabled fence has programmed nothing yet")
    assert "systemctl enable homehub-llm-isolation.service" not in fb, (
        "no bare `enable` for the fence may survive alongside it")


# ===========================================================================
# TC-1037 - round 6. THE REAL SCRIPT, DRIVEN.
#
# Round 6 made two points and both were right:
#
#   * the IPv6 guard grepped for the literal lowercase `enable_ipv6: true`, so
#     it FAILED OPEN on `${VAR}` interpolation, on `True`, and on an anchor
#     merge - every one of which gives the lane IPv6 while the script logs
#     "declares no IPv6";
#   * the test I called "driven" ran a DUPLICATE of the awk program and only
#     grepped the script for keywords, so it could not catch the production
#     parser diverging from the copy it tested.
#
# These execute stack/llm-isolation/llm-isolation.sh itself, with iptables,
# ip6tables and docker replaced by stubs on PATH. No privileges, no daemon,
# no hub. The whole point is that the thing under test is the shipped file.
# ===========================================================================

import json
import os
import stat


STUB = ROOT / "tests" / "fixtures" / "iptables_stub.py"


def _shim(path, lines):
    path.write_text("#!/usr/bin/env bash\n" + lines, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _posix(p):
    r"""Windows form -> POSIX form (``C:\x`` becomes ``/c/x``).

    Git Bash cannot search a Windows-form PATH entry, and a harness that sets
    one silently fails to find its own stubs - which presents as the script
    being broken rather than the test being broken."""
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _harness(tmp_path, compose_json, env_extra="", env_base=None, preseed=None):
    """Lay out a fake box: stub binaries, an .env, and a resolved compose doc.

    The iptables stub is a real mini-implementation (tests/fixtures/) rather
    than an echo: it honours INSERT POSITIONS, so the script's own ordering
    checks can actually fail. A stub that appended would have listed the rules
    in exactly the wrong order and "proved" the opposite of the property.
    """
    # exist_ok: a test that calls _run twice (two env shapes in one case)
    # reuses the same tmp_path, and a bare mkdir() raises on the second call.
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    py = sys.executable.replace("\\", "/")
    stub = str(STUB).replace("\\", "/")

    # The two families get SEPARATE state. Pointing ip6tables at the IPv4
    # store made the script's IPv6 cleanup loop delete the IPv4 rules it had
    # just installed - a harness bug that presented as a script bug and cost
    # two rounds of debugging. They are different tables in reality.
    _shim(bindir / "iptables", 'exec "%s" "%s" "$@"\n' % (py, stub))
    _shim(bindir / "ip6tables", 'exec "%s" "%s" --v6 "$@"\n' % (py, stub))
    _shim(bindir / "python3", 'exec "%s" "$@"\n' % py)
    _shim(bindir / "docker",
          'if [ "$1" = compose ] && [ "$2" = config ]; then\n'
          '  cat "$COMPOSE_JSON"; exit 0\n'
          'fi\n'
          'exit 0\n')

    # `_env` in the script takes the FIRST match, so an override must REPLACE
    # the base line rather than follow it. A test that appended a second
    # `DEVPC_HOST=` silently exercised the original value and passed for the
    # wrong reason.
    base = env_base if env_base is not None else {
        "LITELLM_CONTAINER_IP": "172.28.91.10",
        "DEVPC_HOST": "192.168.1.20",
        "DEVPC_INFERENCE_PORT": "11434",
        "DEVPC_WAKE_PORT": "8799",
    }
    env = tmp_path / ".env"
    env.write_text("".join("%s=%s\n" % kv for kv in base.items()) + env_extra,
                   encoding="utf-8")

    compose = tmp_path / "docker-compose.yml"
    compose.write_text("# the stub docker returns compose.json\n", encoding="utf-8")
    (tmp_path / "compose.json").write_text(compose_json, encoding="utf-8")

    envmap = dict(os.environ)
    envmap["STUB_BIN"] = _posix(bindir)
    envmap["ENV_FILE"] = str(env)
    envmap["COMPOSE_FILE"] = str(compose)
    envmap["IPT_STATE"] = str(tmp_path / "rules.json")
    envmap["IPT_STATE6"] = str(tmp_path / "rules6.json")
    envmap["IPT_PRESEED6"] = json.dumps({"DOCKER-USER": []})
    envmap["COMPOSE_JSON"] = str(tmp_path / "compose.json")
    # The normal state of a running box: the chain exists, the FORWARD jump is
    # present, and Tailscale's jump is first in INPUT.
    envmap["IPT_PRESEED"] = preseed if preseed is not None else json.dumps({
        "INPUT": [{"target": "ts-input", "comment": "", "body": "-j ts-input"}],
        "FORWARD": [{"target": "DOCKER-USER", "comment": "", "body": "-j DOCKER-USER"}],
        "DOCKER-USER": [],
    })
    return envmap


def _run(tmp_path, **kw):
    envmap = _harness(tmp_path, **kw)
    # PATH is assembled INSIDE bash, where $PATH is already in POSIX form.
    # Building it in Python produced `C:\...;C:\...`, which Git Bash cannot
    # search, so the script reported "iptables not found".
    return subprocess.run(
        [_BASH, "-c", 'export PATH="$STUB_BIN:$PATH"; exec bash "$1"',
         "_", _posix(ISO_SH)],
        env=envmap, capture_output=True, text=True)


_V6_OFF = '{"networks": {"llmprivate": {"ipam": {"config": [{"subnet": "172.28.91.0/24"}]}}}}'
_V6_ON = '{"networks": {"llmprivate": {"enable_ipv6": true}}}'
_V6_ON_STR = '{"networks": {"llmprivate": {"enable_ipv6": "True"}}}'
_V6_ABSENT = '{"networks": {"game": {}}}'


@_needs_bash
@pytest.mark.parametrize("cfg_json,should_die,why", [
    (_V6_OFF,    False, "no IPv6 - the normal case"),
    (_V6_ON,     True,  "a real YAML boolean true"),
    (_V6_ON_STR, True,  "the string 'True' - capitalised, which the old grep missed"),
    (_V6_ABSENT, True,  "the network is not in the resolved config at all"),
])
def test_the_real_script_fails_closed_on_every_ipv6_shape_sr047(
        tmp_path, cfg_json, should_die, why):
    """DRIVES THE SHIPPED SCRIPT. The previous test ran a duplicate of the awk
    program, so the production parser could have diverged from it freely.

    `docker compose config` resolves interpolation, anchors and YAML typing
    before we see it, which is why `${VAR}` and `True` are covered here
    without this test having to know how they were written."""
    r = _run(tmp_path, compose_json=cfg_json)
    if should_die:
        assert r.returncode != 0, (
            "expected the fence to refuse (%s) but it exited 0:\n%s\n%s"
            % (why, r.stdout, r.stderr))
    else:
        assert r.returncode == 0, (
            "expected the fence to program cleanly (%s):\n%s\n%s"
            % (why, r.stdout, r.stderr))
        assert "enable_ipv6=false" in r.stdout


@_needs_bash
def test_the_real_script_refuses_when_the_resolved_config_is_unobtainable_sr047(tmp_path):
    """FAIL CLOSED. The version before this one logged 'premise UNVERIFIED'
    and carried on, which is the same as not checking at all."""
    r = _run(tmp_path, compose_json="this is not json")
    assert r.returncode != 0
    assert "unverified" in (r.stdout + r.stderr).lower()


@_needs_bash
@pytest.mark.parametrize("url,ok", [
    ("http://172.28.91.1:8799",  True),
    ("http://172.28.91.1:8799/", True),
    ("172.28.91.1:8799",         False),
    ("https://172.28.91.1",      False),
    ("http://172.17.0.1:8799",   False),
])
def test_the_real_script_validates_the_deployed_wake_url_sr047(tmp_path, url, ok):
    """Same cases as TC-1035, but through the shipped script rather than a
    re-implementation of its `case` statement."""
    r = _run(tmp_path, compose_json=_V6_OFF,
             env_extra="DEVPC_WAKE_URL=%s\n" % url)
    if ok:
        assert r.returncode == 0, r.stdout + r.stderr
        assert "agrees with the host-allow rule" in r.stdout
    else:
        assert r.returncode != 0, "should have refused %s" % url
        assert "only accepted value" in (r.stdout + r.stderr).lower()


@_needs_bash
def test_the_real_script_refuses_a_hostname_and_an_empty_address_sr047(tmp_path):
    # REPLACE the base value, do not append one. `_env` takes the first match,
    # so an appended override is read by nothing and the test passes against
    # the original address - which is how this assertion first "passed".
    r = _run(tmp_path, compose_json=_V6_OFF, env_base={
        "LITELLM_CONTAINER_IP": "172.28.91.10",
        "DEVPC_HOST": "devpc.local",
        "DEVPC_INFERENCE_PORT": "11434",
        "DEVPC_WAKE_PORT": "8799",
    })
    assert r.returncode != 0, "a hostname must be refused: iptables resolves " \
        "it once at program time and the rule then outlives the answer"
    assert "literal IPv4" in (r.stdout + r.stderr)

    # And an empty container address must refuse rather than fence nothing.
    r2 = _run(tmp_path, compose_json=_V6_OFF, env_base={
        "LITELLM_CONTAINER_IP": "",
        "DEVPC_HOST": "192.168.1.20",
        "DEVPC_INFERENCE_PORT": "11434",
        "DEVPC_WAKE_PORT": "8799",
    })
    assert r2.returncode != 0
    assert "refusing to fence nothing" in (r2.stdout + r2.stderr)


# ── the two precedence checks, DRIVEN in the state where they must fire ──────
#
# Both of these guards existed and were only ever exercised in their SILENT
# state - the state where nothing precedes the fence and the check passes.
# That is precisely how round 6's deploy-blocking bug hid: the INPUT guard's
# `grep -v` pipeline exits 1 when it matches nothing, so the "everything is
# correct" path was the one that killed the script, and no test noticed because
# no test had ever run the script at all. A guard whose FIRING branch has never
# executed is a guard nobody has tested; these two run it.


@_needs_bash
def test_the_real_script_refuses_when_a_blanket_accept_outranks_the_input_fence_sr047(tmp_path):
    """An ACCEPT at or above `ts-input` sits above everything we insert.

    This is not hypothetical: the remote-desktop fence one directory over adds
    `-p tcp --dports 21115:21119 -j ACCEPT` matching ANY source, and the whole
    reason this fence is inserted rather than appended is to stay above rules
    of that shape. If one is placed above the insertion point instead, the
    fence protects nothing for those ports and must refuse to claim otherwise.
    """
    r = _run(tmp_path, compose_json=_V6_OFF, preseed=json.dumps({
        # Position 1 - ABOVE ts-input, so above the TS_POS+1 insertion point.
        "INPUT": [
            {"target": "ACCEPT", "comment": "",
             "body": "-p tcp -m multiport --dports 21115:21119 -j ACCEPT"},
            {"target": "ts-input", "comment": "", "body": "-j ts-input"},
        ],
        "FORWARD": [{"target": "DOCKER-USER", "comment": "", "body": "-j DOCKER-USER"}],
        "DOCKER-USER": [],
    }))
    out = r.stdout + r.stderr
    assert r.returncode != 0, \
        "an ACCEPT above the fence must be fatal, not logged: got\n%s" % out
    assert "precedes this fence in INPUT" in out, out


@_needs_bash
def test_the_real_script_refuses_when_an_accept_precedes_the_forward_jump_sr047(tmp_path):
    """A DOCKER-USER jump that is never reached is a fence that filters nothing.

    The jump can exist and still be bypassed if an ACCEPT or RETURN sits above
    it in FORWARD. Every read-back below that point would pass against a chain
    the packet never enters - the same shape as a detached bind mount.
    """
    r = _run(tmp_path, compose_json=_V6_OFF, preseed=json.dumps({
        "INPUT": [{"target": "ts-input", "comment": "", "body": "-j ts-input"}],
        "FORWARD": [
            {"target": "ACCEPT", "comment": "", "body": "-j ACCEPT"},
            {"target": "DOCKER-USER", "comment": "", "body": "-j DOCKER-USER"},
        ],
        "DOCKER-USER": [],
    }))
    out = r.stdout + r.stderr
    assert r.returncode != 0, \
        "an ACCEPT above the DOCKER-USER jump must be fatal: got\n%s" % out
    assert "precedes the DOCKER-USER jump" in out, out


# ── the reapply window ───────────────────────────────────────────────────────


def _rules(tmp_path, chain):
    """The stub's final IPv4 state for one chain, in order, as comment strings."""
    state = json.loads((tmp_path / "rules.json").read_text(encoding="utf-8"))
    return [r["comment"] for r in state.get(chain, [])]


@_needs_bash
def test_a_successful_run_leaves_no_reapply_guard_behind_sr047(tmp_path):
    """The blanket deny is scaffolding, and scaffolding that stays is a dead lane.

    It denies everything from this source INCLUDING the dev PC, so a run that
    reported success while leaving it installed would look healthy and serve
    nothing.
    """
    r = _run(tmp_path, compose_json=_V6_OFF)
    assert r.returncode == 0, r.stdout + r.stderr

    for chain in ("DOCKER-USER", "INPUT"):
        comments = _rules(tmp_path, chain)
        assert not [c for c in comments if "reapply-guard" in c], \
            "%s still holds the guard: %r" % (chain, comments)

    # And the real rules ARE there, in the order the fence depends on.
    du = [c for c in _rules(tmp_path, "DOCKER-USER") if "homehub-llm-isolation" in c]
    assert du == ["homehub-llm-isolation allow-established",
                  "homehub-llm-isolation allow-devpc",
                  "homehub-llm-isolation deny-all"], du


@_needs_bash
def test_a_refusal_after_the_rebuild_still_leaves_a_blanket_deny_sr047(tmp_path):
    """FAIL CLOSED IS A STATE, NOT JUST AN EXIT CODE.

    Several checks can still refuse AFTER the rules have been rebuilt. When
    they do, the chain must not be left holding allow rules with nothing
    denying the rest. A bad DEVPC_WAKE_URL is a real refusal in that position.
    """
    r = _run(tmp_path, compose_json=_V6_OFF, env_base={
        "LITELLM_CONTAINER_IP": "172.28.91.10",
        "DEVPC_HOST": "192.168.1.20",
        "DEVPC_INFERENCE_PORT": "11434",
        "DEVPC_WAKE_PORT": "8799",
        "DEVPC_WAKE_URL": "http://172.17.0.1:8799",   # the default bridge, not ours
    })
    assert r.returncode != 0, "a wrong wake URL must refuse"

    for chain in ("DOCKER-USER", "INPUT"):
        comments = _rules(tmp_path, chain)
        denies = [i for i, c in enumerate(comments)
                  if "deny" in c or "reapply-guard" in c]
        allows = [i for i, c in enumerate(comments) if "allow" in c]
        assert denies, "%s has no blanket deny after a refusal: %r" % (chain, comments)
        # Every allow is bounded by a deny below it; nothing falls through.
        assert max(denies) > max(allows or [-1]),             "%s leaves an allow below the last deny: %r" % (chain, comments)


@_needs_bash
def test_a_failure_MID_REBUILD_leaves_the_lane_denied_not_open_sr047(tmp_path):
    """THE CASE THE GUARD EXISTS FOR, and the only one that can prove it.

    A rebuild removes the old rules before inserting the new ones. If a rule
    cannot be inserted - a missing kernel module, a conntrack match the kernel
    will not take - the script exits there, with the old fence already gone.
    WITHOUT the blanket guard installed first, DOCKER-USER is then EMPTY of any
    rule for this source: the unit reports failure while the container, if it
    is running, has completely unrestricted egress. That is the worst possible
    combination, and it is invisible from the exit code alone.

    `IPT_FAIL` fails the FIRST insert of the rebuild, `deny-all`. That choice
    is the point: the three rules go in as deny, allow-devpc, allow-established
    (each at position 1, so they come out in the opposite order), which means
    failing any LATER insert still leaves `deny-all` behind and the lane merely
    dead. Only failing the first one produces the genuinely open chain, and
    picking a later one would have made this test pass without the guard.
    Removing the guard from the script turns this test red.
    """
    envmap = _harness(tmp_path, compose_json=_V6_OFF)
    envmap["IPT_FAIL"] = "deny-all"
    r = subprocess.run(
        [_BASH, "-c", 'export PATH="$STUB_BIN:$PATH"; exec bash "$1"',
         "_", _posix(ISO_SH)],
        env=envmap, capture_output=True, text=True)

    assert r.returncode != 0, "an insert that fails must not exit zero"

    for chain in ("DOCKER-USER", "INPUT"):
        comments = _rules(tmp_path, chain)
        mine = [c for c in comments if "homehub-llm-isolation" in c]
        assert any("reapply-guard" in c for c in mine), (
            "%s holds NO blanket deny after a failed rebuild - the lane is "
            "unfenced at the moment the unit reports failure: %r" % (chain, mine))
        guard_at = next(i for i, c in enumerate(comments) if "reapply-guard" in c)
        allows = [i for i, c in enumerate(comments) if "allow" in c]
        assert all(guard_at < i for i in allows), (
            "%s guard at %d sits below an allow, so it denies nothing that "
            "matters: %r" % (chain, guard_at, comments))
