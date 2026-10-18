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


# ===========================================================================
# LLR-1137 / TC-1258: the listener check. hbbs can exit (its own self-test
# fails under memory pressure) while docker keeps reporting it "Up"; nothing
# restarts it because the containers are restart: "no".
# ===========================================================================
import os
import shutil
import stat

CHECK = RD / "rustdesk-check.sh"
CHECK_UNIT = RD / "homehub-rustdesk-check.service"
CHECK_TIMER = RD / "homehub-rustdesk-check.timer"
FIRSTBOOT = ROOT / "stack" / "autoinstall" / "firstboot.sh"

SS_ALL = """tcp LISTEN 0 1024 0.0.0.0:21115 0.0.0.0:*
tcp LISTEN 0 1024 0.0.0.0:21116 0.0.0.0:*
udp UNCONN 0 0 0.0.0.0:21116 0.0.0.0:*
tcp LISTEN 0 1024 0.0.0.0:21117 0.0.0.0:*
tcp LISTEN 0 128 [::]:22 [::]:*
"""
# hbbs is dead, hbbr alive - the 2026-09-28 shape.
SS_NO_HBBS = "tcp LISTEN 0 1024 0.0.0.0:21117 0.0.0.0:*\ntcp LISTEN 0 1024 0.0.0.0:21119 0.0.0.0:*\n"

_bash = shutil.which("bash")
needs_bash = pytest.mark.skipif(_bash is None, reason="needs bash")


def _w(path, text):
    path.write_bytes(text.encode("utf-8"))


FENCE_V4 = """-P INPUT ACCEPT
-A INPUT -i lo -p tcp -m multiport --dports 21115,21116,21117,21118,21119 -m comment --comment homehub-rustdesk-isolation -j ACCEPT
-A INPUT -s 192.168.1.0/24 -p tcp -m multiport --dports 21115,21116,21117,21118,21119 -m comment --comment homehub-rustdesk-isolation -j ACCEPT
-A INPUT -s 100.64.0.0/10 -p tcp -m multiport --dports 21115,21116,21117,21118,21119 -m comment --comment homehub-rustdesk-isolation -j ACCEPT
-A INPUT -p tcp -m multiport --dports 21115,21116,21117,21118,21119 -m comment --comment homehub-rustdesk-isolation -j REJECT --reject-with icmp-port-unreachable
-A INPUT -i lo -p udp -m multiport --dports 21116 -m comment --comment homehub-rustdesk-isolation -j ACCEPT
-A INPUT -s 192.168.1.0/24 -p udp -m multiport --dports 21116 -m comment --comment homehub-rustdesk-isolation -j ACCEPT
-A INPUT -s 100.64.0.0/10 -p udp -m multiport --dports 21116 -m comment --comment homehub-rustdesk-isolation -j ACCEPT
-A INPUT -p udp -m multiport --dports 21116 -m comment --comment homehub-rustdesk-isolation -j REJECT --reject-with icmp-port-unreachable
"""
FENCE_V6 = """-P INPUT ACCEPT
-A INPUT -i lo -p tcp -m multiport --dports 21115,21116,21117,21118,21119 -m comment --comment homehub-rustdesk-isolation -j ACCEPT
-A INPUT -s fd7a:115c:a1e0::/48 -p tcp -m multiport --dports 21115,21116,21117,21118,21119 -m comment --comment homehub-rustdesk-isolation -j ACCEPT
-A INPUT -p tcp -m multiport --dports 21115,21116,21117,21118,21119 -m comment --comment homehub-rustdesk-isolation -j REJECT --reject-with icmp6-port-unreachable
-A INPUT -i lo -p udp -m multiport --dports 21116 -m comment --comment homehub-rustdesk-isolation -j ACCEPT
-A INPUT -s fd7a:115c:a1e0::/48 -p udp -m multiport --dports 21116 -m comment --comment homehub-rustdesk-isolation -j ACCEPT
-A INPUT -p udp -m multiport --dports 21116 -m comment --comment homehub-rustdesk-isolation -j REJECT --reject-with icmp6-port-unreachable
"""


class _Rig:
    """Fake ss/systemctl/docker/curl/flock/iptables on PATH, each recording its argv."""

    def __init__(self, tmp_path):
        self.d = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.state = tmp_path / "state"
        self.env_file = tmp_path / "env"
        self.ss_now = tmp_path / "ss.now"
        self.ss_after = tmp_path / "ss.after"
        self.calls = tmp_path / "calls.log"
        _w(self.env_file, "RUSTDESK_ENABLED=true\nLAN_IP=192.0.2.10\nNTFY_PORT=8081\n"
                          "RUSTDESK_LAN_CIDR=192.168.1.0/24\nRUSTDESK_TUNNEL_CIDR=100.64.0.0/10\n"
                          "RUSTDESK_TUNNEL_CIDR6=fd7a:115c:a1e0::/48\n")
        _w(self.ss_now, SS_ALL)
        _w(self.ss_after, SS_ALL)
        _w(tmp_path / "ipt4", FENCE_V4)
        _w(tmp_path / "ipt6", FENCE_V6)
        self.active = {"homehub-rustdesk.service": True, "homehub-rustdesk-isolation.service": True}
        self.ss_fails = False
        # ss: after a recreate, `ss.flap` = N means the Nth following call onward sees `ss.dead`.
        self._stub("ss", 'echo "ss $*" >>"$CALLS"\n'
                         '[ -e "$RIG/ss.fail" ] && exit 1\n'
                         'n=$(cat "$RIG/ss.n" 2>/dev/null || echo 0); echo $((n+1)) >"$RIG/ss.n"\n'
                         'if [ -e "$RIG/ss.call$n" ] && [ ! -e "$RIG/recreated" ]; then cat "$RIG/ss.call$n"; exit 0; fi\n'
                         'if [ -e "$RIG/ss.flap" ] && [ -e "$RIG/recreated" ] && [ "$n" -ge "$(cat "$RIG/ss.flap")" ]; then cat "$RIG/ss.dead"; exit 0; fi\n'
                         'cat "$RIG/ss.now"\n')
        # systemctl: `owner.flip` = N means the owner reads active for N queries, then inactive.
        self._stub("systemctl", 'echo "systemctl $*" >>"$CALLS"\n'
                                'if [ "$1" = is-active ]; then\n'
                                '  if [ "${@: -1}" = homehub-rustdesk.service ] && [ -e "$RIG/owner.flip" ]; then\n'
                                '    q=$(cat "$RIG/owner.q" 2>/dev/null || echo 0); echo $((q+1)) >"$RIG/owner.q"\n'
                                '    [ "$q" -lt "$(cat "$RIG/owner.flip")" ]; exit $?\n'
                                '  fi\n'
                                '  [ -e "$RIG/active.${@: -1}" ]; exit $?\n'
                                'fi\nexit 0\n')
        self._stub("docker", 'echo "docker $*" >>"$CALLS"\n'
                             'case "$*" in *force-recreate*) cp "$RIG/ss.after" "$RIG/ss.now"; echo 0 >"$RIG/ss.n"; : >"$RIG/recreated";; esac\nexit 0\n')
        self._stub("curl", 'echo "curl $*" | tr "\\n" " " >>"$CALLS"; echo >>"$CALLS"\n[ -e "$RIG/curl.fail" ] && exit 22\nexit 0\n')
        self._stub("flock", 'echo "flock $*" >>"$CALLS"\n[ -e "$RIG/flock.fail8" ] && [ "${@: -1}" = 8 ] && exit 1\nexit 0\n')
        self._stub("iptables", 'echo "iptables $*" >>"$CALLS"\ncat "$RIG/ipt4"\n')
        self._stub("ip6tables", 'echo "ip6tables $*" >>"$CALLS"\ncat "$RIG/ipt6"\n')

    def _stub(self, name, body):
        p = self.bin / name
        _w(p, "#!/usr/bin/env bash\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)

    def sync(self):
        for unit, on in self.active.items():
            f = self.d / ("active." + unit)
            if on:
                f.write_text("", encoding="utf-8")
            elif f.exists():
                f.unlink()
        if self.ss_fails:
            (self.d / "ss.fail").write_text("", encoding="utf-8")

    def run(self, **extra):
        self.sync()
        env = dict(os.environ)
        env.update({
            "PATH": str(self.bin) + os.pathsep + env["PATH"],
            "RIG": str(self.d), "CALLS": str(self.calls),
            "RUSTDESK_CHECK_ENV_FILE": str(self.env_file),
            "RUSTDESK_CHECK_STACK_DIR": str(self.d),
            "RUSTDESK_CHECK_STATE_DIR": str(self.state),
            "RUSTDESK_CHECK_WAIT_SEC": "1", "RUSTDESK_CHECK_POLL_SEC": "1",
            "RUSTDESK_CHECK_SETTLE_SEC": "2",
            "RUSTDESK_CHECK_OWNER_LOCK": str(self.d / "owner.lock"),
        })
        env.update(extra)
        self.calls.write_text("", encoding="utf-8")
        r = subprocess.run([_bash, str(CHECK)], env=env, capture_output=True, text=True, timeout=60)
        return r, self.calls.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def rig(tmp_path):
    return _Rig(tmp_path)


def _has(calls, prefix):
    return [c for c in calls if c.startswith(prefix)]


@needs_bash
@pytest.mark.parametrize("value", ["false", "", "TRUE", "1"])
def test_check_does_nothing_unless_enabled_is_exactly_true_sr045(rig, value):
    _w(rig.env_file, "RUSTDESK_ENABLED=%s\n" % value)
    _w(rig.ss_now, SS_NO_HBBS)
    r, calls = rig.run()
    assert r.returncode == 0, r.stderr
    assert not _has(calls, "docker") and not _has(calls, "curl")
    assert not _has(calls, "ss"), "the gate must come before any probing"


@needs_bash
def test_check_does_nothing_when_the_owner_unit_is_inactive_sr045(rig):
    rig.active["homehub-rustdesk.service"] = False
    _w(rig.ss_now, SS_NO_HBBS)
    r, calls = rig.run()
    assert r.returncode == 0, r.stderr
    assert not _has(calls, "docker") and not _has(calls, "curl")


@needs_bash
def test_check_is_quiet_when_every_listener_is_present_sr045(rig):
    r, calls = rig.run()
    assert r.returncode == 0, r.stderr
    assert _has(calls, "ss -H -ltnu")
    assert not _has(calls, "docker") and not _has(calls, "curl")


@needs_bash
@pytest.mark.parametrize("drop,svc", [
    ("udp UNCONN 0 0 0.0.0.0:21116 0.0.0.0:*\n", "hbbs"),
    ("tcp LISTEN 0 1024 0.0.0.0:21115 0.0.0.0:*\n", "hbbs"),
    ("tcp LISTEN 0 1024 0.0.0.0:21116 0.0.0.0:*\n", "hbbs"),
    ("tcp LISTEN 0 1024 0.0.0.0:21117 0.0.0.0:*\n", "hbbr"),
])
def test_check_detects_each_required_listener_sr045(rig, drop, svc):
    """The UDP half of 21116 is its own case: a tcp-only probe passes while
    clients cannot reach the rendezvous."""
    _w(rig.ss_now, SS_ALL.replace(drop, ""))
    r, calls = rig.run()
    other = "hbbr" if svc == "hbbs" else "hbbs"
    recreate = _has(calls, "docker")
    assert len(recreate) == 1 and "force-recreate" in recreate[0], calls
    assert recreate[0].split()[-1] == svc and other not in recreate[0].split()


@needs_bash
def test_check_recreates_only_the_dead_service_and_reports_recovery_sr045(rig):
    _w(rig.ss_now, SS_NO_HBBS)
    r, calls = rig.run()
    assert r.returncode == 0, r.stdout + r.stderr
    docker = _has(calls, "docker")
    assert docker == ["docker compose --profile rustdesk up -d --no-build --force-recreate --no-deps hbbs"]
    curl = _has(calls, "curl")
    assert len(curl) == 1 and "http://192.0.2.10:8081/homehub-alerts" in curl[0]
    assert "Priority: default" in curl[0] and "back" in curl[0]


@needs_bash
def test_check_still_down_alerts_high_and_fails_sr045(rig):
    _w(rig.ss_now, SS_NO_HBBS)
    _w(rig.ss_after, SS_NO_HBBS)
    r, calls = rig.run()
    assert r.returncode == 1, "must exit non-zero so `systemctl --failed` shows it"
    curl = _has(calls, "curl")
    assert len(curl) == 1 and "Priority: high" in curl[0] and "STILL down" in curl[0]


def _fence_down_case(rig, v4, v6):
    _w(rig.ss_now, SS_NO_HBBS)
    _w(rig.d / "ipt4", v4)
    _w(rig.d / "ipt6", v6)
    r, calls = rig.run()
    assert r.returncode == 1, r.stdout
    assert not _has(calls, "docker"), "started a listener with the fence not in place"
    assert not _has(calls, "systemctl restart") and not _has(calls, "systemctl start")
    assert "Priority: high" in " ".join(_has(calls, "curl"))
    return r


@needs_bash
def test_check_refuses_when_both_families_have_no_fence_rules_sr045(rig):
    """The isolation unit is a oneshot RemainAfterExit: it stays 'active' after a
    flush removed the rules, so 'unit active' proves nothing."""
    assert rig.active["homehub-rustdesk-isolation.service"] is True
    _fence_down_case(rig, "-P INPUT ACCEPT\n", "-P INPUT ACCEPT\n")


@needs_bash
def test_check_refuses_when_only_the_v6_fence_is_missing_sr045(rig):
    r = _fence_down_case(rig, FENCE_V4, "-P INPUT ACCEPT\n")
    assert "ip6tables" in r.stdout


@needs_bash
def test_check_refuses_when_only_the_v4_fence_is_missing_sr045(rig):
    r = _fence_down_case(rig, "-P INPUT ACCEPT\n", FENCE_V6)
    assert "iptables" in r.stdout


@needs_bash
def test_check_refuses_when_one_protocol_or_the_reject_is_missing_sr045(rig):
    only_tcp = "\n".join(l for l in FENCE_V4.splitlines() if "-p udp" not in l) + "\n"
    _fence_down_case(rig, only_tcp, FENCE_V6)


@needs_bash
def test_check_refuses_a_misordered_fence_sr045(rig):
    """A REJECT ahead of the loopback ACCEPT kills hbbs's own self-test."""
    lines = FENCE_V4.splitlines()
    rej = [i for i, l in enumerate(lines) if "tcp" in l and "REJECT" in l][0]
    lo = [i for i, l in enumerate(lines) if "tcp" in l and "-i lo" in l][0]
    lines[rej], lines[lo] = lines[lo], lines[rej]
    _fence_down_case(rig, "\n".join(lines) + "\n", FENCE_V6)


@needs_bash
def test_check_refuses_a_fence_without_the_loopback_accept_sr045(rig):
    """Without it hbbs's own startup self-test is rejected and it exits."""
    no_lo = "\n".join(l for l in FENCE_V6.splitlines() if "-i lo" not in l) + "\n"
    _fence_down_case(rig, FENCE_V4, no_lo)


@needs_bash
def test_check_refuses_a_fence_missing_the_configured_lan_accept_sr045(rig):
    no_lan = "\n".join(l for l in FENCE_V4.splitlines() if "-s 192.168.1.0/24" not in l) + "\n"
    _fence_down_case(rig, no_lan, FENCE_V6)


@needs_bash
def test_check_reads_the_rules_of_both_families_before_recreating_sr045(rig):
    _w(rig.ss_now, SS_NO_HBBS)
    r, calls = rig.run()
    assert r.returncode == 0, r.stdout
    kinds = [c.split()[0] for c in calls if c.split()[0] in ("iptables", "ip6tables", "docker")]
    assert kinds.index("docker") > kinds.index("iptables") and kinds.index("docker") > kinds.index("ip6tables")


@needs_bash
def test_check_takes_the_owner_lock_and_revalidates_the_owner_under_it_sr045(rig):
    _w(rig.ss_now, SS_NO_HBBS)
    # This test is about lock ORDER, not settling: a zero settle window keeps
    # wall-clock load from turning a correct run into "still down".
    r, calls = rig.run(RUSTDESK_CHECK_SETTLE_SEC="0")
    assert r.returncode == 0, r.stdout
    flock_i = next(i for i, c in enumerate(calls) if c.startswith("flock -w") and c.endswith(" 8"))
    docker_i = next(i for i, c in enumerate(calls) if c.startswith("docker"))
    owner_after = [i for i, c in enumerate(calls) if c == "systemctl is-active --quiet homehub-rustdesk.service" and i > flock_i]
    assert flock_i < docker_i and owner_after and owner_after[0] < docker_i, calls


@needs_bash
def test_check_does_not_recreate_when_the_owner_is_stopped_during_the_check_sr045(rig):
    """Deterministic stop-during-check: the owner reads active for the first
    query (before the lock) and inactive when re-validated under the lock - an
    operator stop that landed in between. Recovery must not undo it."""
    _w(rig.ss_now, SS_NO_HBBS)
    _w(rig.d / "owner.flip", "1")
    r, calls = rig.run()
    assert r.returncode == 0, r.stdout
    assert not _has(calls, "docker"), "recreated listeners with their owner stopped"
    assert not _has(calls, "curl")
    assert not _has(calls, "iptables"), "re-validation must precede any work"


@needs_bash
def test_check_does_nothing_when_it_cannot_take_the_owner_lock_sr045(rig):
    _w(rig.ss_now, SS_NO_HBBS)
    _w(rig.d / "flock.fail8", "")  # only the owner-lock (fd 8) acquisition fails
    r, calls = rig.run()
    assert _has(calls, "flock -n 9"), "the run lock must have been taken first"
    assert [c for c in calls if c.startswith("flock -w") and c.endswith(" 8")], "the owner lock was never attempted"
    assert not _has(calls, "docker") and not _has(calls, "iptables")
    assert r.returncode == 0


@needs_bash
def test_check_requires_listeners_to_stay_up_not_just_appear_sr045(rig):
    """hbbs listens, then its startup self-test times out and it exits. One good
    snapshot is not recovery."""
    _w(rig.ss_now, SS_NO_HBBS)
    _w(rig.d / "ss.dead", SS_NO_HBBS)
    _w(rig.d / "ss.flap", "1")
    r, calls = rig.run()
    assert r.returncode == 1, r.stdout
    curl = " ".join(_has(calls, "curl"))
    assert "STILL down" in curl and "Priority: high" in curl and "back" not in curl


@needs_bash
def test_check_bounds_repeated_recreates_sr045(rig):
    _w(rig.ss_now, SS_NO_HBBS)
    rig.state.mkdir()
    import time as _t
    _w(rig.state / "recreate-attempts", "3 %d\n" % int(_t.time()))
    r, calls = rig.run()
    assert r.returncode == 1
    assert not _has(calls, "docker"), "a persistent fault must not loop"
    assert "Priority: high" in " ".join(_has(calls, "curl"))


@needs_bash
def test_check_retries_after_the_backoff_and_resets_on_stable_recovery_sr045(rig):
    _w(rig.ss_now, SS_NO_HBBS)
    rig.state.mkdir()
    _w(rig.state / "recreate-attempts", "3 1000\n")  # long ago
    r, calls = rig.run()
    assert r.returncode == 0, r.stdout
    assert len(_has(calls, "docker")) == 1
    assert not (rig.state / "recreate-attempts").exists()


@needs_bash
def test_check_counts_each_unstable_attempt_sr045(rig):
    _w(rig.ss_now, SS_NO_HBBS)
    _w(rig.d / "ss.dead", SS_NO_HBBS)
    _w(rig.d / "ss.flap", "1")
    rig.run()
    assert (rig.state / "recreate-attempts").read_text().split()[0] == "1"


@needs_bash
def test_check_does_not_mistake_a_failing_ss_for_dead_listeners_sr045(rig):
    rig.ss_fails = True
    r, calls = rig.run()
    assert r.returncode == 1
    assert not _has(calls, "docker"), "recreated healthy containers over a tooling fault"


@needs_bash
def test_check_alerts_are_best_effort_and_repeat_suppressed_sr045(rig):
    _w(rig.ss_now, SS_NO_HBBS)
    _w(rig.ss_after, SS_NO_HBBS)
    (rig.d / "curl.fail").write_text("", encoding="utf-8")
    r, calls = rig.run()
    assert r.returncode == 1 and len(_has(calls, "curl")) == 1, "an undelivered alert must not change the verdict"
    (rig.d / "curl.fail").unlink()
    _w(rig.ss_now, SS_NO_HBBS)
    r, calls = rig.run()
    assert len(_has(calls, "curl")) == 1, "the failed delivery must be retried"
    _w(rig.ss_now, SS_NO_HBBS)
    r, calls = rig.run()
    assert not _has(calls, "curl"), "a delivered alert must be suppressed on the next run"


def test_check_files_are_lf_and_the_units_are_wired_sr045():
    for p in (CHECK, CHECK_UNIT, CHECK_TIMER):
        assert b"\r" not in p.read_bytes(), "%s has CR line endings" % p.name
    assert CHECK.read_bytes().startswith(b"#!/usr/bin/env bash\n")
    unit, timer = CHECK_UNIT.read_text(encoding="utf-8"), CHECK_TIMER.read_text(encoding="utf-8")
    live_u, live_t = _live(unit), _live(timer)
    assert "Type=oneshot" in live_u
    assert "ExecStart=/opt/homehub/stack/rustdesk/rustdesk-check.sh" in live_u
    assert "homehub-rustdesk-isolation.service" in live_u and "homehub-rustdesk.service" in live_u
    assert re.search(r"^TimeoutStartSec=\d+$", live_u, re.M)
    assert "OnBootSec=" in live_t and "OnUnitActiveSec=5min" in live_t
    assert "WantedBy=timers.target" in live_t
    # The check must never become a second owner of the listeners.
    assert "Requires=homehub-rustdesk" not in live_u and "WantedBy=multi-user" not in live_u


def test_check_script_bounds_its_calls_and_checks_the_fence_sr045():
    live = _live(CHECK.read_text(encoding="utf-8"))
    assert "set -u" in live
    for call in ("ss -H -ltnu", "systemctl is-active", "docker compose", "curl "):
        assert re.search(r"timeout[^\n]*" + re.escape(call), live), "%s is not bounded" % call
    assert "homehub-rustdesk-isolation.service" in live
    assert "iptables" in live and "ip6tables" in live and "flock" in live


def test_firstboot_installs_the_check_and_enables_the_timer_only_when_enabled_sr045():
    src = FIRSTBOOT.read_text(encoding="utf-8")
    assert 'chmod 0755 "$STACK_DIR/rustdesk/rustdesk-check.sh"' in src
    assert "homehub-rustdesk-check.service" in src and "homehub-rustdesk-check.timer" in src
    start = src.index('if [ "$_rustdesk_enabled" = "true" ]; then')
    els = src.index("    else\n", start)
    end = src.index('\nelse\n    log "rustdesk: no payload', els)
    enabled_branch, disabled_branch = src[start:els], src[els:end]
    assert "enable --now homehub-rustdesk-check.timer" in enabled_branch
    assert "disable --now homehub-rustdesk-check.timer" in disabled_branch
    assert "enable --now homehub-rustdesk-check.timer" not in disabled_branch
    assert src.count("enable --now homehub-rustdesk-check.timer") == 1


def test_readme_documents_the_listener_check_sr045():
    text = README.read_text(encoding="utf-8")
    assert "rustdesk-check.sh" in text and "2026-09-28" in text


def test_owner_unit_and_checker_share_one_lock_file_sr045():
    owner = _live(OWNER_UNIT.read_text(encoding="utf-8"))
    lock = re.search(r"RUSTDESK_CHECK_OWNER_LOCK:-([^}]+)\}", CHECK.read_text(encoding="utf-8")).group(1)
    for verb in ("ExecStart", "ExecStop"):
        line = [l for l in owner.splitlines() if l.startswith(verb + "=")]
        assert len(line) == 1 and line[0].startswith(verb + "=/usr/bin/flock " + lock + " /usr/bin/docker compose"), line


def _no_lines(text, *needles):
    return "\n".join(l for l in text.splitlines() if not all(n in l for n in needles)) + "\n"


@needs_bash
def test_check_refuses_a_fence_that_does_not_cover_the_relay_port_sr045(rig):
    """RUSTDESK_TCP_PORTS can be narrowed: a block over 21115,21116 leaves hbbr's
    tcp 21117 unfenced even though every rule is present and ordered."""
    narrow = FENCE_V4.replace("21115,21116,21117,21118,21119", "21115,21116")
    _fence_down_case(rig, narrow, FENCE_V6)


@needs_bash
@pytest.mark.parametrize("family", ["v4", "v6"])
@pytest.mark.parametrize("omit", ["21118", "21119"])
def test_check_refuses_a_fence_missing_a_websocket_port_sr045(rig, family, omit):
    """Recreating hbbs/hbbr also opens tcp 21118/21119: the fence must cover all
    five tcp ports in both families, though they are not required to LISTEN."""
    ports = [x for x in "21115,21116,21117,21118,21119".split(",") if x != omit]
    narrow = lambda t: t.replace("21115,21116,21117,21118,21119", ",".join(ports))
    if family == "v4":
        _fence_down_case(rig, narrow(FENCE_V4), FENCE_V6)
    else:
        _fence_down_case(rig, FENCE_V4, narrow(FENCE_V6))


@needs_bash
def test_check_does_not_require_the_websocket_ports_to_listen_sr045(rig):
    _w(rig.ss_now, SS_ALL)  # SS_ALL has no 21118/21119 listener
    r, calls = rig.run()
    assert r.returncode == 0 and not _has(calls, "docker")


@needs_bash
def test_check_refuses_a_fence_with_the_wrong_udp_port_sr045(rig):
    _fence_down_case(rig, FENCE_V4.replace("--dports 21116 ", "--dports 21117 "), FENCE_V6)


@needs_bash
def test_check_refuses_a_source_restricted_reject_sr045(rig):
    """A REJECT for one source is not the catch-all."""
    restricted = FENCE_V6.replace("-A INPUT -p tcp -m multiport", "-A INPUT -s 2001:db8::/32 -p tcp -m multiport")
    assert restricted != FENCE_V6
    _fence_down_case(rig, FENCE_V4, restricted)


@needs_bash
def test_check_refuses_an_interface_restricted_reject_sr045(rig):
    iface = FENCE_V4.replace("-A INPUT -p udp -m multiport", "-A INPUT -i eth0 -p udp -m multiport")
    assert iface != FENCE_V4
    _fence_down_case(rig, iface, FENCE_V6)


@needs_bash
def test_check_accepts_a_port_range_and_both_marker_spellings_sr045(rig):
    """`iptables -S` prints the comment bare or quoted, and a range as a:b. A
    legitimate listing must not be refused or recovery never happens."""
    quoted = FENCE_V4.replace("--comment homehub-rustdesk-isolation", '--comment "homehub-rustdesk-isolation"')
    ranged = FENCE_V6.replace("21115,21116,21117,21118,21119", "21115:21119")
    assert quoted != FENCE_V4 and ranged != FENCE_V6
    _w(rig.ss_now, SS_NO_HBBS)
    _w(rig.d / "ipt4", quoted)
    _w(rig.d / "ipt6", ranged)
    r, calls = rig.run()
    assert r.returncode == 0, r.stdout
    assert len(_has(calls, "docker")) == 1


@needs_bash
def test_check_accepts_a_fully_quoted_listing_in_both_families_sr045(rig):
    q = lambda t: t.replace("--comment homehub-rustdesk-isolation", '--comment "homehub-rustdesk-isolation"')
    _w(rig.ss_now, SS_NO_HBBS)
    _w(rig.d / "ipt4", q(FENCE_V4))
    _w(rig.d / "ipt6", q(FENCE_V6))
    r, calls = rig.run()
    assert r.returncode == 0 and len(_has(calls, "docker")) == 1


@needs_bash
def test_check_recreates_only_what_is_missing_under_the_lock_sr045(rig):
    """hbbr came back between the first listing and the one taken under the
    lock: only hbbs is recreated."""
    _w(rig.ss_now, "")
    _w(rig.d / "ss.call1", SS_ALL.replace("tcp LISTEN 0 1024 0.0.0.0:21115 0.0.0.0:*\n", "")
                                  .replace("tcp LISTEN 0 1024 0.0.0.0:21116 0.0.0.0:*\n", "")
                                  .replace("udp UNCONN 0 0 0.0.0.0:21116 0.0.0.0:*\n", ""))
    r, calls = rig.run()
    docker = _has(calls, "docker")
    assert len(docker) == 1 and docker[0].split()[-1] == "hbbs", docker


@needs_bash
def test_check_releases_the_lock_when_the_owner_starts_stopping_during_settle_sr045(rig):
    """An operator stop queued behind our lock must not wait out the settle
    window: the checker sees the owner stopping and lets go, no alert."""
    _w(rig.ss_now, SS_NO_HBBS)
    _w(rig.d / "owner.flip", "2")  # main, recover: active; first settle iteration: stopping
    r, calls = rig.run()
    assert r.returncode == 0, r.stdout
    assert "aborting recovery" in r.stdout
    assert len(_has(calls, "docker")) == 1 and not _has(calls, "curl")


def test_owner_unit_timeouts_cover_the_checkers_lock_hold_sr045():
    """The owner's ExecStop waits on the lock the checker holds. Default
    TimeoutStopSec (90 s) would kill that wait and leave the containers up."""
    script = CHECK.read_text(encoding="utf-8")
    budget = int(re.search(r"^LOCK_HOLD_BUDGET_SEC=(\d+)$", script, re.M).group(1))
    recreate = int(re.search(r'RECREATE_TIMEOUT="\$\(_int "\$\{RUSTDESK_CHECK_RECREATE_TIMEOUT:-\}" (\d+)\)"', script).group(1))
    wait = int(re.search(r'WAIT_SEC="\$\(_int "\$\{RUSTDESK_CHECK_WAIT_SEC:-\}" (\d+)\)"', script).group(1))
    settle = int(re.search(r'SETTLE_SEC="\$\(_int "\$\{RUSTDESK_CHECK_SETTLE_SEC:-\}" (\d+)\)"', script).group(1))
    assert budget >= recreate + wait + settle + 60 + 30, "budget must cover recreate + appear + settle + checks"
    owner = _live(OWNER_UNIT.read_text(encoding="utf-8"))
    stop = int(re.search(r"^TimeoutStopSec=(\d+)$", owner, re.M).group(1))
    start = int(re.search(r"^TimeoutStartSec=(\d+)$", owner, re.M).group(1))
    assert stop >= budget + 90 and start >= budget + 90, (stop, start, budget)
    chk = int(re.search(r"^TimeoutStartSec=(\d+)$", _live(CHECK_UNIT.read_text(encoding="utf-8")), re.M).group(1))
    lock_wait = int(re.search(r'LOCK_WAIT_SEC="\$\(_int "\$\{RUSTDESK_CHECK_LOCK_WAIT_SEC:-\}" (\d+)\)"', script).group(1))
    assert chk >= lock_wait + budget, "the checker's own timeout must outlast its lock wait + hold"
