"""The admin portal: one login, one front door (HomeHub ADMIN_PORTAL_PLAN rev 3).

P1 management UIs on 127.0.0.1 behind *.admin.<domain>; P2 every such site
@lan-gated with a 403 default; P3 loopback publishes for the SSH fallback; the
pinned `admin` network that --trusted-proxy-ip names; Phase B's per-app binds.

WHAT THIS FILE CAN AND CANNOT SAY. The auth MECHANICS — the redirect, the
cookie scope, the rd whitelist, the push route's exactness, HSTS scoping — were
proven by running these exact site blocks and flags (HomeHub's
scripts/verify/admin-portal-harness/, 47 checks; and admin-auth's flags against
the pinned image). What is asserted here is that the SHIPPED files still carry
the shapes those runs proved, because each of them is one plausible edit away
from quietly not doing its job: `{uri}` for `{%uri}`, a push matcher widened to
a prefix, HSTS copied onto the bare domain, a LAN_IP bind put back.
"""
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
COMPOSE = STACK / "docker-compose.yml"
CADDY = STACK / "caddy" / "Caddyfile"
ENVEX = STACK / ".env.example"
HOMEPAGE = STACK / "homepage"
ADMIN_AUTH = STACK / "admin-auth"

ADMIN_SITES = ("admin", "kuma.admin", "logs.admin", "dns.admin", "actual.admin")
PHASE_B = {  # label -> (upstream, compose service, bind knob, port knob)
    "photos": ("immich-server:2283", "immich-server", "IMMICH_BIND_IP", "IMMICH_PORT"),
    "jellyfin": ("jellyfin:8096", "jellyfin", "JELLYFIN_BIND_IP", "JELLYFIN_PORT"),
    "music": ("navidrome:4533", "navidrome", "NAVIDROME_BIND_IP", "NAVIDROME_PORT"),
    "ntfy": ("ntfy:80", "ntfy", "NTFY_BIND_IP", "NTFY_PORT"),
}


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def caddy_live():
    """The Caddyfile with every comment line removed."""
    return "\n".join(l for l in CADDY.read_text(encoding="utf-8").splitlines()
                     if not l.lstrip().startswith("#"))


def site(live, address):
    """One top-level block by its exact address line, `address {` … `}`."""
    m = re.search(r"^" + re.escape(address) + r" \{\n(.*?)^\}", live, re.M | re.S)
    assert m, "no site block for %s" % address
    return m.group(1)


def snippet(live, name):
    return site(live, "(%s)" % name)


def env_example():
    keys = {}
    for line in ENVEX.read_text(encoding="utf-8").splitlines():
        m = re.match(r"([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            keys[m.group(1)] = m.group(2)
    return keys


# ── compose: P1/P3 — loopback publishes, and who sits on the admin network ────

@pytest.mark.parametrize("svc,port_knob,target", [
    ("uptime-kuma", "UPTIMEKUMA_PORT", 3001),
    ("dozzle", "DOZZLE_PORT", 8080),
    ("homepage", "HOMEPAGE_PORT", 3000),
    ("actual", "ACTUAL_PORT", 5006),
])
def test_management_uis_publish_on_loopback_only(compose, svc, port_knob, target):
    """P1/P3. A LAN_IP bind here is the side door the portal exists to close
    (Dozzle's had no login at all); no publish at all would lose the `ssh -L`
    fallback for when Caddy is the thing that is broken."""
    ports = compose["services"][svc]["ports"]
    assert ports == ["127.0.0.1:${%s}:%d" % (port_knob, target)], ports


def test_admin_network_membership(compose):
    s = compose["services"]
    nets = lambda n: set(s[n]["networks"])  # noqa: E731
    assert nets("caddy") == {"default", "game", "admin"}
    # Only on the admin network: nothing on the default bridge (tracker,
    # finance data) has reason to reach them.
    for n in ("admin-auth", "dozzle", "homepage"):
        assert nets(n) == {"admin"}, n
    # Kuma's monitors reach the stack by name; finance-auditor reaches Actual.
    for n in ("uptime-kuma", "actual"):
        assert nets(n) == {"default", "admin"}, n


def test_admin_auth_is_never_published(compose):
    a = compose["services"]["admin-auth"]
    assert "ports" not in a, "Caddy is its only client; a host port is a second door"
    assert a["expose"] == ["4180"]


# ── compose: admin-auth's configuration ──────────────────────────────────────

def test_admin_auth_flags_are_the_proven_set(compose):
    a = compose["services"]["admin-auth"]
    assert a["image"] == "quay.io/oauth2-proxy/oauth2-proxy:${OAUTH2_PROXY_IMAGE_TAG}"
    flags = dict(f[2:].split("=", 1) if "=" in f else (f[2:], None) for f in a["command"])
    # The cookie is scoped to the admin namespace, and the redirect whitelist is
    # the same namespace — a bare-domain cookie reaches the tracker and the
    # public relay (review finding 1; harness T6).
    assert flags["cookie-domain"] == ".admin.${DOMAIN}"
    assert flags["whitelist-domain"] == ".admin.${DOMAIN}"
    assert flags["cookie-name"] == "__Secure-homehub_admin"
    assert flags["cookie-secure"] == "true"
    assert flags["custom-templates-dir"] == "/etc/admin-auth/templates"
    assert flags["htpasswd-file"] == "/etc/admin-auth/htpasswd"
    assert flags["display-htpasswd-form"] == "true"
    assert flags["upstream"] == "static://202"
    assert flags["reverse-proxy"] == "true"
    # `*` makes any allow-list inert (oauth2-proxy issue #73); and the provider
    # button flag skips straight to the dead provider (measured, U1).
    assert "email-domain" not in flags
    assert "skip-provider-button" not in flags


def test_admin_auth_settings_are_flags_and_only_the_secret_is_env(compose):
    """An OAUTH2_PROXY_* variable oauth2-proxy does not know is silently ignored;
    a misspelt flag refuses to start (both measured on the pinned image)."""
    env = compose["services"]["admin-auth"]["environment"]
    assert env == {"OAUTH2_PROXY_COOKIE_SECRET": "${ADMIN_AUTH_COOKIE_SECRET}"}


def test_the_pinned_addresses_agree():
    """Caddy's static address, --trusted-proxy-ip and ADMIN_NET_GATEWAY — the
    same check validate_config.py runs, asserted here so a test run sees it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "validate_config", ROOT / "scripts" / "validate_config.py")
    vc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vc)
    text = COMPOSE.read_text(encoding="utf-8")
    assert vc.admin_network_problems(text) == []
    # And the check can fail: a trusted-proxy value that drifted to the subnet.
    drifted = text.replace("--trusted-proxy-ip=172.28.92.2/32",
                           "--trusted-proxy-ip=172.28.92.0/24")
    assert drifted != text and vc.admin_network_problems(drifted)


def test_admin_network_is_pinned_next_to_the_isolation_networks(compose):
    import ipaddress
    cfg = compose["networks"]["admin"]["ipam"]["config"][0]
    admin = ipaddress.ip_network(cfg["subnet"])
    for other in ("game", "llmprivate"):
        o = ipaddress.ip_network(compose["networks"][other]["ipam"]["config"][0]["subnet"])
        assert not admin.overlaps(o), other


def test_caddy_carries_the_portal_env_and_no_basic_auth(compose):
    env = compose["services"]["caddy"]["environment"]
    assert env["ADMIN_HOST"] == "admin.${DOMAIN}"
    assert env["ADMIN_NET_GATEWAY"] == "172.28.92.1"
    assert not [k for k in env if "BASICAUTH" in k]
    assert set(compose["services"]["caddy"]["depends_on"]) >= {"admin-auth", "oauth2-proxy"}


# ── compose: Phase B ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("label", sorted(PHASE_B))
def test_phase_b_binds_default_to_the_lan(compose, label):
    """Empty knob = today's LAN port. The nested default is what keeps a
    materialised .env that predates the knob working rather than breaking."""
    _, svc, knob, port = PHASE_B[label]
    host = compose["services"][svc]["ports"][0].rsplit(":", 2)[0]
    assert host == "${%s:-${LAN_IP}}" % knob, compose["services"][svc]["ports"]
    assert env_example()[knob] == "", "%s must ship empty (= LAN_IP)" % knob


def test_ntfy_is_behind_the_proxy(compose):
    env = compose["services"]["ntfy"]["environment"]
    assert env["NTFY_BASE_URL"] == "https://ntfy.${DOMAIN}"
    assert env["NTFY_BEHIND_PROXY"] == "true"


# ── Caddyfile: the gate ──────────────────────────────────────────────────────

def test_protect_admin_escapes_the_original_url(caddy_live):
    """`{%uri}` — `{uri}` would let the original query's & and = be read as
    parameters of the sign-in URL (review finding 4; harness U3)."""
    body = snippet(caddy_live, "protect_admin")
    assert "forward_auth admin-auth:4180" in body
    assert "uri /oauth2/auth" in body
    assert "@unauth status 401" in body, "only 401 may become a redirect (fail closed)"
    assert "rd=https://{hostport}{%uri}" in body
    assert "{uri}" not in body


@pytest.mark.parametrize("label", ADMIN_SITES)
def test_every_admin_site_is_gated_and_hsts(caddy_live, label):
    body = site(caddy_live, "%s.{$DOMAIN}" % label)
    assert "import acme_cloudflare" in body
    assert "import hsts" in body
    assert "@lan remote_ip {$LAN_CIDR}" in body
    assert re.search(r"handle \{\n\s+respond \"[^\"]+\" 403\n\s+\}\n\Z", body), \
        "the non-LAN default must be a 403 in its own handle"
    assert "import protect_admin" in body


def test_hsts_never_leaves_the_admin_namespace(caddy_live):
    """includeSubDomains is right only under admin.; on the bare domain it would
    commit every present and future name to HTTPS for a year."""
    assert snippet(caddy_live, "hsts").strip() == \
        'header Strict-Transport-Security "max-age=31536000; includeSubDomains"'
    users = re.findall(r"^(\S+) \{\n(?:(?!^\}).)*?import hsts", caddy_live, re.M | re.S)
    assert sorted(users) == sorted("%s.{$DOMAIN}" % l for l in ADMIN_SITES), users
    assert "Strict-Transport-Security" not in caddy_live.replace(
        snippet(caddy_live, "hsts"), "")


def test_sign_in_route_is_throttled_guarded_and_logged(caddy_live):
    body = site(caddy_live, "admin.{$DOMAIN}")
    route = body.split("handle /oauth2/* {", 1)[1].split("\n        }\n", 1)[0]
    assert "import throttle" in route
    assert '@plainrd expression `{http.request.uri.query.rd}.startsWith("http:")`' in route
    assert 'respond @plainrd "sign-in: https only" 400' in route
    assert "reverse_proxy admin-auth:4180" in route
    assert "import protect_admin" not in route, "the sign-in route IS the gate"
    # The only admin site that can emit a 401 is the only one that logs it.
    assert "import access_log" in body
    for label in ADMIN_SITES[1:]:
        assert "import access_log" not in site(caddy_live, "%s.{$DOMAIN}" % label), label


def test_kuma_push_route_is_get_only_and_exact(caddy_live):
    """GET only (the panel sends GET; Kuma 1.23 refuses POST). One path segment
    and nothing deeper: a prefix served Kuma's SPA with no login (harness)."""
    body = site(caddy_live, "kuma.admin.{$DOMAIN}")
    m = re.search(r"@push \{\n\s+method (.+)\n\s+path_regexp (.+)\n\s+\}", body)
    assert m, "the push matcher must be one method line and one path_regexp"
    assert m.group(1).strip() == "GET"
    assert m.group(2).strip() == "^/api/push/[A-Za-z0-9_-]+$"


def test_dns_console_goes_through_the_admin_gateway(caddy_live):
    body = site(caddy_live, "dns.admin.{$DOMAIN}")
    assert "reverse_proxy {$ADMIN_NET_GATEWAY}:5380" in body
    assert "host.docker.internal" not in caddy_live, \
        "no live site may reach the host via docker0: the fence admits the admin subnet only"


@pytest.mark.parametrize("old,new", [("{$ACTUAL_HOST}", "actual.admin"),
                                     ("dns.{$DOMAIN}", "dns.admin")])
def test_old_names_are_gated_redirects(caddy_live, old, new):
    body = site(caddy_live, old)
    assert "@lan remote_ip {$LAN_CIDR}" in body
    assert "redir https://%s.{$DOMAIN}{uri}" % new in body
    assert "reverse_proxy" not in body


def test_no_basic_auth_is_left(caddy_live):
    assert "basic_auth" not in caddy_live.replace("order rate_limit before basic_auth", "")
    assert "BASICAUTH" not in caddy_live
    assert not re.search(r"^[A-Z_]*BASICAUTH[A-Z_]*=", ENVEX.read_text(encoding="utf-8"), re.M)


@pytest.mark.parametrize("label", sorted(PHASE_B))
def test_phase_b_sites_are_gated_without_the_admin_login(caddy_live, label):
    upstream = PHASE_B[label][0]
    body = site(caddy_live, "%s.{$DOMAIN}" % label)
    assert "import acme_cloudflare" in body
    assert "@lan remote_ip {$LAN_CIDR}" in body
    assert "reverse_proxy %s" % upstream in body
    assert "protect_admin" not in body, "their apps cannot complete a web sign-in"
    assert "hsts" not in body
    if label == "ntfy":
        assert "encode" not in body, "subscribers hold a stream open; compression buffers it"


# ── .env.example ─────────────────────────────────────────────────────────────

def test_env_example_carries_the_portal_knobs():
    env = env_example()
    assert env["ADMIN_AUTH_USER"] == "admin"
    assert env["ADMIN_AUTH_HASH"].startswith("REPLACE_WITH")
    assert env["ADMIN_AUTH_COOKIE_SECRET"].startswith("REPLACE_WITH")
    assert env["ACTUAL_PORT"] == "5006"
    assert env["HOMEHUB_ALERT_TOPIC"] == "homehub-alerts"
    assert env["TAILSCALE_ADVERTISE_EXIT_NODE"] == "false"
    for gone in ("ACTUAL_BASICAUTH_USER", "ACTUAL_BASICAUTH_HASH",
                 "DNS_BASICAUTH_USER", "DNS_BASICAUTH_HASH"):
        assert gone not in env, gone


# ── the admin-auth and homepage directories ──────────────────────────────────

def test_sign_in_template_has_the_form_and_no_provider_button():
    raw = (ADMIN_AUTH / "templates" / "sign_in.html").read_text(encoding="utf-8")
    # The provenance comment names what was removed; judge the template body.
    tpl = re.sub(r"\{\{/\*.*?\*/\}\}", "", raw, flags=re.S)
    assert '{{define "sign_in.html"}}' in tpl
    assert 'type="password"' in tpl
    assert "Sign in with" not in tpl
    assert "/start" not in tpl, "the provider form posted to {{.ProxyPrefix}}/start"


def test_the_materialised_htpasswd_is_never_tracked():
    r = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q",
                        "stack/admin-auth/htpasswd"], capture_output=True)
    if r.returncode == 128:
        pytest.skip("not a git checkout")
    assert r.returncode == 0, "stack/admin-auth/htpasswd must be gitignored"
    assert (ADMIN_AUTH / "htpasswd.example").is_file()


def test_homepage_config_has_every_skeleton_file():
    """The config mount is read-only. At start the pinned Homepage copies a
    skeleton file in for any that is missing; on a read-only mount that copy
    fails and every page is a 500 (measured on v0.10.9, 2026-09-25). This list
    is that image's /app/src/skeleton — re-derive it when the pin moves."""
    skeleton = {"bookmarks.yaml", "custom.css", "custom.js", "docker.yaml",
                "kubernetes.yaml", "services.yaml", "settings.yaml", "widgets.yaml"}
    present = {p.name for p in HOMEPAGE.iterdir() if p.is_file()}
    assert skeleton <= present, sorted(skeleton - present)
    for y in (p for p in HOMEPAGE.glob("*.yaml")):
        yaml.safe_load(y.read_text(encoding="utf-8"))


def test_homepage_links_the_admin_names_and_probes_containers():
    services = yaml.safe_load((HOMEPAGE / "services.yaml").read_text(encoding="utf-8"))
    items = {name: cfg for group in services for entries in group.values()
             for item in entries for name, cfg in item.items()}
    expect = {"Uptime Kuma": ("kuma.admin", "http://uptime-kuma:3001"),
              "Dozzle": ("logs.admin", "http://dozzle:8080"),
              "Technitium": ("dns.admin", "http://172.28.92.1:5380"),
              "Actual": ("actual.admin", "http://actual:5006")}
    for name, (label, probe) in expect.items():
        assert items[name]["href"] == "https://%s.{{HOMEPAGE_VAR_DOMAIN}}/" % label
        assert items[name]["siteMonitor"] == probe
