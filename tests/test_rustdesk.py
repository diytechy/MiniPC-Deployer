"""Tests for the self-hosted remote-desktop relay.

Verifies: SR-045 / LLR-971, LLR-972, LLR-973, LLR-976, LLR-977
Cases:    TC-997 .. TC-1001, TC-1008, TC-1009, TC-1011, TC-1012
"""
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "stack" / "docker-compose.yml"
RD = ROOT / "stack" / "rustdesk"
ISO = RD / "rustdesk-isolation.sh"
ISO_UNIT = RD / "homehub-rustdesk-isolation.service"
OWNER_UNIT = RD / "homehub-rustdesk.service"
README = RD / "README.md"
ENVEX = ROOT / "stack" / ".env.example"


def _live(src):
    """Live directives only. These files document at length what they do NOT do,
    quoting the very chains and flags the tests forbid."""
    out = []
    for line in src.splitlines():
        if line.strip().startswith("#"):
            continue
        out.append(line)
    return chr(10).join(out)


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


# --- TC-997 ---------------------------------------------------------------
def test_both_services_share_exactly_one_named_volume_sr045(compose):
    """Two volumes silently produce two key pairs and a client that reaches the
    rendezvous but never the relay - an intermittent connection, not an obvious
    misconfiguration."""
    hbbs, hbbr = compose["services"]["hbbs"], compose["services"]["hbbr"]
    assert hbbs["volumes"] == hbbr["volumes"] == ["rustdesk_data:/root"]
    assert "rustdesk_data" in compose["volumes"]
    assert COMPOSE.read_text(encoding="utf-8").count("\n  rustdesk_data:") == 1


# --- TC-1011 --------------------------------------------------------------
def test_containers_carry_no_restart_policy_and_are_systemd_owned_sr045(compose):
    for name in ("hbbs", "hbbr"):
        svc = compose["services"][name]
        assert svc["restart"] == "no", (
            "%s must never be started by the container runtime: the INPUT fence "
            "is installed by a systemd unit that must run first" % name
        )
        assert svc["network_mode"] == "host"
        assert svc["profiles"] == ["rustdesk"]

    owner = OWNER_UNIT.read_text(encoding="utf-8")
    assert "Requires=homehub-rustdesk-isolation.service" in owner, (
        "After= alone would still start the listeners when the fence had FAILED"
    )
    assert "After=homehub-rustdesk-isolation.service" in owner


# --- TC-1000 --------------------------------------------------------------
def test_the_rule_lands_in_input_not_docker_user_sr045():
    """Host networking means host sockets, which INPUT filters and DOCKER-USER
    never sees. A DOCKER-USER rule here would be present and inert."""
    live = _live(ISO.read_text(encoding="utf-8"))
    assert "DOCKER-USER" not in live, "a DOCKER-USER rule here filters nothing"
    assert "-A INPUT" in live
    assert "iptables" in live and "ip6tables" in live, "the v6 half is not optional"


def test_both_address_families_are_programmed_sr045():
    live = _live(ISO.read_text(encoding="utf-8"))
    assert "install_family iptables" in live
    assert "install_family ip6tables" in live
    # With no v6 admitted source the REJECT must still be installed, so the
    # listeners are closed over v6 rather than open by omission.
    assert 'install_family ip6tables "$TUNNEL_CIDR_V6"' in live


# --- TC-1008 --------------------------------------------------------------
def test_rules_are_replayed_idempotently_by_marker_sr045():
    """The previous version of this test asserted the literal string "-D INPUT"
    was present, and it PASSED for weeks while the delete could not work: the
    script stripped only "-A " from `-A INPUT -s ...`, leaving the chain at the
    head of the spec, and then named the chain AGAIN in `-D INPUT $spec`.
    iptables answered "Invalid rule number `INPUT'" every time and a `|| break`
    swallowed it, so each run appended a fresh set on top of the last.

    Substring presence is not behaviour. This exercises the transformation on a
    real `iptables -S` line instead."""
    src = ISO.read_text(encoding="utf-8")
    live = _live(src)
    assert "purge iptables" in live and "purge ip6tables" in live
    assert "--comment $COMMENT" in live, "rules must be identified by marker"

    # The exact shape `iptables -S INPUT` prints for one of our own rules.
    sample = ("-A INPUT -s 192.168.117.0/24 -p tcp -m multiport "
              "--dports 21115,21116 -m comment --comment "
              "homehub-rustdesk-isolation -j ACCEPT")

    sed_expr = re.search(r"sed '([^']+)'", live)
    assert sed_expr, "the purge must still derive the spec with sed"
    spec = subprocess.run(["sed", sed_expr.group(1)], input=sample,
                          capture_output=True, text=True, check=True).stdout.strip()

    delete = re.search(r"\$bin -D (\S+)", live)
    assert delete, "the purge must still issue a delete"
    argv = ("iptables -D " + delete.group(1).replace("$spec", spec)).split()

    # The chain must appear EXACTLY once in the resulting command line.
    assert argv.count("INPUT") == 1, (
        "the chain is named %d times in %r - naming it twice is the bug that "
        "made every run accumulate rules" % (argv.count("INPUT"), " ".join(argv)))
    assert argv[:3] == ["iptables", "-D", "INPUT"], " ".join(argv)
    assert "--comment" in argv and "homehub-rustdesk-isolation" in argv

    # And a failed delete must now be fatal, not swallowed: a stale REJECT left
    # ahead of a freshly appended ACCEPT silently defeats the new rule.
    assert "|| break" not in live, "a failed delete must not be swallowed"
    assert re.search(r"if ! \$bin -D", live), "a failed delete must stop the run"


def test_loopback_is_admitted_or_hbbs_kills_itself_sr045():
    """hbbs runs a startup self-test against its own 0.0.0.0:21116. That
    connection arrives over loopback, so a fence admitting only the LAN and the
    tunnel rejects it, the test times out ("Timeout of test_hbbs") and hbbs
    EXITS 1 - and with `restart: "no"` it stays dead while the relay beside it
    keeps running. Found by deploying it, 2026-09-18.

    The loopback ACCEPT must also be installed BEFORE the REJECT, or it is
    unreachable."""
    live = _live(ISO.read_text(encoding="utf-8"))
    assert "-i lo" in live, "hbbs cannot complete its own health check without this"

    accept_lo = live.index("-i lo")
    reject = live.index("-j REJECT")
    assert accept_lo < reject, (
        "the loopback ACCEPT must be appended before the REJECT; INPUT is "
        "evaluated in order and a rule after the REJECT is never reached")


# --- TC-1001 --------------------------------------------------------------
def test_no_router_forward_is_documented_for_these_ports_sr045():
    """The remote path is the tunnel; that is what makes no-forward affordable."""
    for path in (README, ISO, ISO_UNIT, OWNER_UNIT):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"port[- ]forward\w*\s+(21\d{3})", text, re.I)
    readme = README.read_text(encoding="utf-8")
    assert "No router forward" in readme or "never" in readme.lower()
    # LLR-973: service mode is a requirement, and the README must say so.
    assert "AS A SERVICE" in readme
    assert "not your Windows password" in readme
    assert "logged-out" in readme, "a locked machine proves nothing about this"


# --- TC-998 ---------------------------------------------------------------
def test_the_key_volume_is_declared_for_local_backup_not_offsite_sr045():
    """The key pair is not reproducible from the repo and regenerating it
    invalidates every installed client."""
    text = COMPOSE.read_text(encoding="utf-8")
    block = text.split("  rustdesk_data:", 1)[0].rsplit("volumes:", 1)[-1]
    assert "LOCAL backup" in block or "local" in block.lower()
    assert "Never offsite" in block or "never offsite" in block.lower()


def test_the_env_declares_the_ports_and_the_tunnel_ranges_sr045():
    env = ENVEX.read_text(encoding="utf-8")
    assert re.search(r"^RUSTDESK_TCP_PORTS=21115,21116,21117,21118,21119$", env, re.M)
    assert re.search(r"^RUSTDESK_UDP_PORTS=21116$", env, re.M)
    assert re.search(r"^RUSTDESK_TUNNEL_CIDR6=\S+$", env, re.M), "the v6 range must exist"
    assert re.search(r"^RUSTDESK_IMAGE_TAG=\d+\.\d+\.\d+$", env, re.M), "pin a concrete tag"


# --- TC-999 / TC-1009 -----------------------------------------------------
@pytest.mark.skip(reason=(
    "needs a real netfilter table and a non-LAN source. Proving a connection is "
    "REFUSED - rather than that a file contains a rule - is hardware acceptance "
    "on the hub, and the honesty bar (SN-008) forbids claiming it here."))
def test_ports_are_admitted_from_lan_and_tunnel_only_sr045():
    raise AssertionError("hardware acceptance")


@pytest.mark.skip(reason=(
    "needs a running host: a flush while the containers are up, then a real "
    "connection attempt. Hardware acceptance on the hub."))
def test_listeners_are_refused_from_non_lan_sources_after_reload_sr045():
    raise AssertionError("hardware acceptance")
