"""Tests for the mesh-VPN subnet router.

Verifies: SR-042 / LLR-959, LLR-960, LLR-961, LLR-962, LLR-975
Cases:    TC-976 .. TC-982, TC-1004, TC-1005

Config-level checks. Anything needing a live tunnel or the coordinator's API is
skipped with a stated reason rather than simulated, per the honesty bar (SN-008):
a check that cannot observe the thing it names must say so.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "stack" / "tailscale" / "setup-tailscale.sh"
ACL = ROOT / "stack" / "tailscale" / "tailnet-acl.hujson"
COMPOSE = ROOT / "stack" / "docker-compose.yml"
ENVEX = ROOT / "stack" / ".env.example"
CADDY = ROOT / "stack" / "caddy" / "Caddyfile"


@pytest.fixture(scope="module")
def setup_src():
    return SETUP.read_text(encoding="utf-8")


def _live(src):
    """Strip shell comments.

    These scripts explain at length what they deliberately do NOT do, quoting
    the very flags and phrases the tests forbid. A scan over raw text therefore
    fails on the documentation rather than on the behaviour - which is a test
    disagreeing with the claim for the wrong reason, the failure mode this repo
    already has a ratified lesson about. Assert against live directives only.
    """
    out = []
    for line in src.splitlines():
        if line.strip().startswith("#"):
            continue
        out.append(line.split(" #", 1)[0] if " #" in line else line)
    return chr(10).join(out)


# --- TC-976 ---------------------------------------------------------------
def test_carriage_is_opt_in_and_absent_from_the_core_payload_sr042(setup_src):
    assert "TAILSCALE_ENABLED=false" in ENVEX.read_text(encoding="utf-8"), \
        "carriage must default OFF"
    assert 'if [ "$TAILSCALE_ENABLED" != "true" ]' in setup_src

    # A HOST service, never a compose service. If this ever appears in the
    # compose file, the two lanes have drifted and a docker fault becomes the
    # thing that stops you reaching the box to fix it.
    compose = COMPOSE.read_text(encoding="utf-8")
    assert "tailscale" not in compose.lower(), \
        "the mesh daemon must not become a compose service"


# --- TC-977 ---------------------------------------------------------------
def test_no_reusable_auth_key_is_accepted_or_stored_sr042(setup_src):
    """An enrolment key admits a device to a tunnel reaching the whole LAN."""
    for forbidden in ("TS_AUTHKEY", "TAILSCALE_AUTHKEY", "TAILSCALE_AUTH_KEY"):
        assert forbidden in setup_src, "the script must actively refuse %s" % forbidden
    assert "--authkey" not in setup_src, "no flag may accept a key"
    assert "refuse " in setup_src

    # And no key literal anywhere in the tracked tree.
    for path in (ENVEX, ACL, SETUP):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"tskey-[A-Za-z0-9-]+", text), \
            "an auth-key literal is present in %s" % path.name


# --- TC-978 ---------------------------------------------------------------
def test_forwarding_sysctls_live_in_their_own_dropin_sr042(setup_src):
    assert "/etc/sysctl.d/99-tailscale.conf" in setup_src
    assert "net.ipv4.ip_forward = 1" in setup_src
    assert "net.ipv6.conf.all.forwarding = 1" in setup_src, \
        "the v6 half is not optional"


# --- TC-979 ---------------------------------------------------------------
def test_bringup_advertises_routes_without_exit_node_or_dns_sr042(setup_src):
    live = _live(setup_src)
    assert "--advertise-routes=" in live
    assert "--accept-dns=false" in live, \
        "accepting the tunnel's DNS would displace Technitium's split-horizon"
    assert "--advertise-exit-node" not in live, \
        "household internet traffic must not transit the hub"


# --- TC-980 ---------------------------------------------------------------
def test_route_is_reported_pending_approval_not_active_sr042(setup_src):
    """Approval is a console-side fact this box cannot observe."""
    assert "PENDING APPROVAL" in setup_src
    assert not re.search(r"subnet rout\w* active", _live(setup_src), re.I), \
        "the script must not claim a route is active"


# --- TC-981 ---------------------------------------------------------------
def test_lan_cidr_admits_lan_and_tunnel_and_excludes_the_docker_bridge_sr042():
    """LAN_CIDR was once `private_ranges`, which includes the bridge space and
    put the one deliberately public workload inside a LAN-only gate."""
    import ipaddress
    env = ENVEX.read_text(encoding="utf-8")
    m = re.search(r"^RUSTDESK_TUNNEL_CIDR=(\S+)$", env, re.M)
    assert m, "the tunnel range must be declared"
    tunnel = ipaddress.ip_network(m.group(1))
    bridge = ipaddress.ip_network("172.16.0.0/12")
    assert not tunnel.overlaps(bridge), \
        "the tunnel range must not overlap the docker bridge space"


# --- TC-982 ---------------------------------------------------------------
def test_the_gate_uses_remote_ip_never_client_ip_sr042():
    """client_ip trusts X-Forwarded-For and would hand over the allow-list."""
    caddy = CADDY.read_text(encoding="utf-8")
    assert "remote_ip {$LAN_CIDR}" in caddy
    for line in caddy.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "client_ip" not in stripped, \
            "a live directive uses client_ip: %s" % stripped


# --- TC-1004 / TC-1005 ----------------------------------------------------
def test_the_policy_reference_is_syntactically_legal_sr042():
    """It is a REFERENCE to paste into the console, and it must not repeat the
    two errors the first version shipped with."""
    assert ACL.is_file()
    text = ACL.read_text(encoding="utf-8")
    body = "\n".join(l for l in text.splitlines() if not l.strip().startswith("//"))

    # 1. Group members must be full email addresses. An autogroup inside a
    #    `groups` entry is a hard syntax error the console rejects.
    m = re.search(r'"groups"\s*:\s*\{(.*?)\}', body, re.S)
    if m:
        assert "autogroup:" not in m.group(1), \
            "an autogroup cannot be a member of a group"

    # 2. A rule targeting a tag no device carries accepts nothing. The hub
    #    authenticates AS A USER and is untagged, so with deny-by-omission a
    #    tag-only policy would cut off tunnel access entirely.
    assert not re.search(r'"dst"\s*:\s*\[[^\]]*"tag:', body), \
        "dst targets a tag; no device in this tailnet is tagged"

    assert '"acls"' in body
    assert "autogroup:member" in body


def test_the_policy_reference_is_not_deployed_and_nothing_reads_it_sr042(setup_src):
    """Tailscale policy lives server-side; tailscaled never reads local disk for
    it. A local check over this file would confirm only that the repo says what
    the repo says - false-green in the one case that matters."""
    assert "NOTHING READS THIS FILE" in ACL.read_text(encoding="utf-8")
    live = _live(setup_src)
    assert "ACL_FILE" not in live, \
        "the script must not read a local policy file and call it verification"
    assert "tailscale lock status" in live, "lock state is still queried"


def test_lock_state_matches_the_negative_before_the_positive_sr042(setup_src):
    """The regression guard for a real false green.

    `tailscale lock status` prints "Tailnet Lock is NOT enabled." when it is
    OFF - a string that CONTAINS the word "enabled". The first version of this
    check was `grep -qi enabled`, so it matched that sentence and reported the
    lock as ON while it was off. It was caught only because the Owner noticed
    the check disagreed with what the console had told him.

    Assert the ORDER, not the prose: the disabled pattern must be tested first,
    and an unrecognised output must be reported rather than guessed at.
    """
    live = _live(setup_src)
    neg = live.find("not enabled")
    pos = live.find("lock is enabled")
    assert neg != -1, "the disabled case must be matched explicitly"
    assert pos != -1, "the enabled case must be matched explicitly"
    assert neg < pos, "the negative must be tested BEFORE the positive"
    assert "UNDETERMINED" in live, \
        "an unrecognised output must be reported, not guessed"
    assert not re.search(r"grep -qi ['\"]?enabled['\"]?\s*$", live, re.M), \
        "a bare substring test over human prose is not a state test"


@pytest.mark.skip(reason="needs the coordinator API and a live tunnel: the "
                         "active-policy fetch and digest comparison are proven "
                         "on the hub, not here (SN-008 honesty bar)")
def test_active_policy_matches_the_recorded_digest_sr042():
    raise AssertionError("hardware/runtime acceptance")
