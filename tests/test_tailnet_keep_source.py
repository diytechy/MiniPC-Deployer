"""Tests for the tailnet keep-source rule.

Verifies: SR-175 / LLR-1600, LLR-1601, LLR-1602, LLR-1603
Cases:    TC-1850 .. TC-1859 (TC-1860 is the netns suite,
          stack/tailscale/tests/keep-source-netns.test.sh, run by
          stack/run-hermetic-tests.sh)

These EXECUTE the shipped script, unmodified, with a fake `iptables` and a fake
`systemctl` first on a PATH built from nothing (the same rule as the exit-node
tests beside them: a fake that failed to shadow the real tool would edit the
dev box's own firewall). The fake keeps one store for -S/-C/-A/-D, so "the rule
is in place" is a statement about the chain it ends up holding.

What these cannot show, and the netns suite does: that the kernel then hands
the container the client's own address, and that the exit-node path is still
masqueraded. What neither shows: the hub (the coordinator's deploy check).
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
SCRIPT = STACK / "tailscale" / "tailnet-keep-source.sh"
UNIT = STACK / "tailscale" / "homehub-tailnet-keep-source.service"
SETUP = STACK / "tailscale" / "setup-tailscale.sh"
ENVEX = STACK / ".env.example"
STUB = ROOT / "tests" / "fixtures" / "iptables_mangle_stub.py"

OURS = ["-s", "100.64.0.0/10", "-o", "br-+", "-m", "comment", "--comment",
        "homehub-tailnet-keep-source", "-j", "MARK", "--set-xmark", "0x0/0x40000"]
TEST_RULE = ["-s", "100.64.0.0/10", "-o", "br-+", "-m", "comment", "--comment",
             "tailnet-to-docker-keep-source-TEST-20261112", "-j", "MARK",
             "--set-xmark", "0x0/0x40000"]
# Somebody else's rule in the same chain: must survive every mode.
OTHER = ["-o", "eno1", "-p", "udp", "-m", "udp", "--dport", "68", "-j", "CHECKSUM",
         "--checksum-fill"]


def _find_bash():
    # On Windows a bare "bash" can resolve to the WSL launcher, so name Git's.
    if os.name == "nt":
        for c in (r"C:\Program Files\Git\usr\bin\bash.exe",
                  r"C:\Program Files\Git\bin\bash.exe"):
            if Path(c).is_file():
                return c
        return None
    return shutil.which("bash")


_BASH = _find_bash()
_needs_bash = pytest.mark.skipif(_BASH is None, reason="no POSIX bash to execute the script with")

FAKE_SYSTEMCTL = r'''#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
case "$1" in
    is-enabled) exit "${FAKE_UNIT_ENABLED_RC:-0}" ;;
    restart) [ "${FAKE_SYSTEMCTL_APPLY:-1}" = 1 ] && exec bash "$KS_SCRIPT" apply ;;
esac
exit 0
'''


class _Box:
    """A stand-in hub: a mangle table, a systemd and a stack .env."""

    def __init__(self, tmp_path, *, env_lines=("TAILSCALE_ENABLED=true",), rules=()):
        py = Path(sys.executable).as_posix()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        shims = {
            "iptables": '#!/bin/sh\nexec "%s" "%s" "$@"\n' % (py, STUB.as_posix()),
            "python3": '#!/bin/sh\nexec "%s" "$@"\n' % py,
            "systemctl": FAKE_SYSTEMCTL,
        }
        for name, body in shims.items():
            with open(self.bin / name, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(body)
            (self.bin / name).chmod(0o755)
        self.state = tmp_path / "ipt.json"
        self.state.write_text(json.dumps({"mangle/POSTROUTING": [list(r) for r in rules]}),
                              encoding="utf-8")
        self.ipt_log = tmp_path / "ipt.jsonl"
        self.ipt_log.write_text("", encoding="utf-8")
        self.sc_log = tmp_path / "systemctl.log"
        self.sc_log.write_text("", encoding="utf-8")
        self.units = tmp_path / "units"
        self.units.mkdir()
        self.env_file = tmp_path / "stack.env"
        with open(self.env_file, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(env_lines) + "\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TAILSCALE_", "TS_"))}
        tail = [str(Path(_BASH).parent)] if os.name == "nt" else ["/usr/bin", "/bin"]
        env.update(PATH=os.pathsep.join([str(self.bin)] + tail),
                   IPT_STATE=self.state.as_posix(), IPT_LOG=self.ipt_log.as_posix(),
                   FAKE_SYSTEMCTL_LOG=self.sc_log.as_posix(), KS_SCRIPT=SCRIPT.as_posix(),
                   TAILNET_KEEP_SOURCE_ENV_FILE=self.env_file.as_posix(),
                   TAILNET_KEEP_SOURCE_UNIT_DIR=self.units.as_posix(),
                   TAILNET_KEEP_SOURCE_LOCK=(tmp_path / "ks.lock").as_posix())
        self.env = env
        which = subprocess.run([_BASH, "-c", "command -v iptables"], env=env,
                               capture_output=True, text=True)
        assert which.stdout.strip().endswith("/bin/iptables"), \
            "the fake does not shadow the real iptables: %r" % which.stdout

    def run(self, *args, extra_env=None, script=SCRIPT):
        env = dict(self.env, **(extra_env or {}))
        r = subprocess.run([_BASH, script.as_posix(), *args], env=env,
                           capture_output=True, text=True, timeout=120)
        r.out = r.stdout + r.stderr
        return r

    @property
    def rules(self):
        return json.loads(self.state.read_text(encoding="utf-8"))["mangle/POSTROUTING"]

    @property
    def edits(self):
        calls = [json.loads(l) for l in self.ipt_log.read_text(encoding="utf-8").splitlines() if l]
        return [c for c in calls if "-A" in c or "-D" in c or "-I" in c]

    @property
    def systemctl(self):
        return self.sc_log.read_text(encoding="utf-8").splitlines()


def _live(src):
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


# --- TC-1850 --------------------------------------------------------------
@_needs_bash
def test_the_rule_clears_only_the_tailscale_bit_on_the_docker_bridges_sr175(tmp_path):
    """LLR-1600: exactly this rule, in mangle POSTROUTING (before nat). Its only
    output match is the docker bridges, so traffic leaving by the LAN interface
    - the exit node and the subnet route to other LAN devices - keeps the mark
    and is masqueraded as before."""
    box = _Box(tmp_path)
    r = box.run("apply")
    assert r.returncode == 0, r.out
    assert box.rules == [OURS]
    assert all("-t" in c and c[c.index("-t") + 1] == "mangle" for c in box.edits), \
        "every edit is in the mangle table"
    live = _live(SCRIPT.read_text(encoding="utf-8"))
    outs = re.findall(r'(?<![\w-])-o\s+(\S+)', live)
    assert outs == ['"$BRIDGES"'], "the bridges are the only output match: %r" % outs
    assert 'BRIDGES="br-+"' in live
    assert "MASQUERADE" not in live and "SNAT" not in live, \
        "the script must never add or remove a NAT rule"


# --- TC-1851 --------------------------------------------------------------
@_needs_bash
def test_apply_defaults_on_replaces_the_test_rule_and_is_idempotent_sr175(tmp_path):
    box = _Box(tmp_path, rules=[OTHER, TEST_RULE])
    r = box.run("apply")
    assert r.returncode == 0, r.out
    assert box.rules == [OTHER, OURS], "the TEST rule goes, the other rule stays"
    first = len(box.edits)
    r = box.run("apply")
    assert r.returncode == 0, r.out
    assert len(box.edits) == first, "a second apply must change nothing"
    assert box.rules == [OTHER, OURS]


# --- TC-1852 --------------------------------------------------------------
@_needs_bash
def test_duplicates_and_a_stale_spec_collapse_to_exactly_one_rule_sr175(tmp_path):
    stale = [a.replace("100.64.0.0/10", "100.64.0.0/16") for a in OURS]
    box = _Box(tmp_path, rules=[OURS, OTHER, OURS, stale])
    r = box.run("apply")
    assert r.returncode == 0, r.out
    assert box.rules == [OTHER, OURS]


# --- TC-1853 --------------------------------------------------------------
@_needs_bash
@pytest.mark.parametrize("env_lines", [
    ("TAILSCALE_ENABLED=true", "TAILSCALE_KEEP_SOURCE_TO_DOCKER=false"),
    ("TAILSCALE_ENABLED=false",),
    (),
])
def test_the_knob_off_or_no_tailnet_withdraws_the_rule_sr175(tmp_path, env_lines):
    box = _Box(tmp_path, env_lines=env_lines, rules=[OURS, OTHER, TEST_RULE])
    r = box.run("apply")
    assert r.returncode == 0, r.out
    assert box.rules == [OTHER]
    s = box.run("status")
    assert s.returncode == 0, s.out
    assert "OFF as configured" in s.out


@_needs_bash
@pytest.mark.parametrize("value", ["yes", "1", "True", "on"])
def test_a_knob_that_is_not_exactly_true_or_false_is_refused_unchanged_sr175(tmp_path, value):
    box = _Box(tmp_path, env_lines=("TAILSCALE_ENABLED=true",
                                    "TAILSCALE_KEEP_SOURCE_TO_DOCKER=%s" % value),
               rules=[TEST_RULE])
    r = box.run("apply")
    assert r.returncode == 2, r.out
    assert "must be exactly true or false" in r.out
    assert box.edits == [] and box.rules == [TEST_RULE]


# --- TC-1854 --------------------------------------------------------------
@_needs_bash
def test_apply_fails_loudly_when_the_rule_does_not_land_sr175(tmp_path):
    """Verify the artifact: an append that reports success and adds nothing
    must fail the unit, not log "applied"."""
    box = _Box(tmp_path)
    r = box.run("apply", extra_env={"IPT_IGNORE_APPEND": "1"})
    assert r.returncode == 1, r.out
    assert "FAILED" in r.out and "applied" not in r.out


@_needs_bash
@pytest.mark.parametrize("mode", ["apply", "status", "remove"])
def test_an_unreadable_chain_is_a_failure_in_every_mode_sr175(tmp_path, mode):
    box = _Box(tmp_path)
    r = box.run(mode, extra_env={"IPT_FAIL_VERB": "-S"})
    assert r.returncode == 1, r.out
    assert box.edits == []


# --- TC-1855 --------------------------------------------------------------
def test_the_unit_runs_at_boot_after_both_daemons_and_binds_to_neither_sr175():
    """LLR-1602: ordering only. A Requires=/PartOf=/BindsTo= on docker would let
    firstboot's `systemctl stop docker` reach this unit (validate_config 5b)."""
    text = UNIT.read_text(encoding="utf-8")
    after = " ".join(re.findall(r"^After=(.*)$", text, re.M)).split()
    assert "docker.service" in after and "tailscaled.service" in after
    for kw in ("Requires=", "PartOf=", "BindsTo=", "Wants="):
        assert not re.search(r"^%s" % kw, text, re.M), kw
    assert re.search(r"^Type=oneshot$", text, re.M)
    assert re.search(r"^RemainAfterExit=yes$", text, re.M)
    assert re.search(r"^WantedBy=multi-user\.target$", text, re.M)
    assert re.search(r"^ExecStart=/bin/bash /opt/homehub/stack/tailscale/tailnet-keep-source\.sh apply$",
                     text, re.M)


@_needs_bash
def test_install_puts_the_unit_in_place_enables_and_runs_it_sr175(tmp_path):
    box = _Box(tmp_path, rules=[TEST_RULE])
    r = box.run("install")
    assert r.returncode == 0, r.out
    assert (box.units / UNIT.name).read_bytes() == UNIT.read_bytes()
    sc = box.systemctl
    assert "daemon-reload" in sc
    assert "enable %s" % UNIT.name in sc
    assert "restart %s" % UNIT.name in sc
    assert box.rules == [OURS]
    assert "ON as configured" in r.out


@_needs_bash
def test_converging_the_tunnel_installs_and_applies_the_rule_sr175(tmp_path):
    from test_tailscale_subnet_router import _FakeHub
    hub = _FakeHub(tmp_path, knob="false", ipt_rules=[TEST_RULE])
    r = hub.run()
    assert r.returncode == 0, r.out
    assert (hub.units / UNIT.name).is_file()
    assert hub.ipt_rules == [OURS]
    assert "WARNING: the keep-source rule" not in r.out


def test_setup_tailscale_installs_it_when_converging_and_reports_it_in_check_sr175():
    live = _live(SETUP.read_text(encoding="utf-8"))
    assert 'tailnet-keep-source.sh" install' in live
    assert 'tailnet-keep-source.sh" status' in live
    assert "TAILSCALE_KEEP_SOURCE_TO_DOCKER=true" in ENVEX.read_text(encoding="utf-8"), \
        "the template must carry the knob, ON"


# --- TC-1856 --------------------------------------------------------------
@_needs_bash
@pytest.mark.parametrize("rules,unit_rc,expect", [
    ([OURS], "0", "ON as configured"),
    ([], "0", "found 0 rule(s)"),
    ([OURS, OURS], "0", "found 2 rule(s)"),
    ([OURS, TEST_RULE], "0", "hand-made"),
    ([OURS], "1", "not enabled"),
])
def test_status_reads_only_and_names_the_drift_sr175(tmp_path, rules, unit_rc, expect):
    box = _Box(tmp_path, rules=rules)
    r = box.run("status", extra_env={"FAKE_UNIT_ENABLED_RC": unit_rc})
    want_rc = 0 if expect == "ON as configured" else 3
    assert r.returncode == want_rc, r.out
    assert expect in r.out
    assert box.edits == [], "status must never edit the chain"
    assert box.rules == rules


@_needs_bash
def test_status_calls_a_rule_left_on_while_the_knob_is_off_drift_sr175(tmp_path):
    box = _Box(tmp_path, env_lines=("TAILSCALE_ENABLED=true",
                                    "TAILSCALE_KEEP_SOURCE_TO_DOCKER=false"), rules=[OURS])
    r = box.run("status")
    assert r.returncode == 3, r.out
    assert "want the rule OFF" in r.out


# --- TC-1857 --------------------------------------------------------------
@_needs_bash
@pytest.mark.parametrize("rules,rc", [([OURS], 0), ([TEST_RULE], 1)])
def test_check_only_folds_in_the_rule_state_sr175(tmp_path, rules, rc):
    """setup-tailscale.sh --check-only, against the exit-node tests' fake
    daemon, reports the rule and fails on its drift."""
    from test_tailscale_subnet_router import _FakeHub
    hub = _FakeHub(tmp_path, knob="false", ipt_rules=rules)
    r = hub.run("--check-only")
    assert r.returncode == rc, r.out
    assert "check: tailnet-keep-source:" in r.out
    assert not hub.verbs("set") and not hub.verbs("up")


# --- TC-1858 --------------------------------------------------------------
@_needs_bash
def test_remove_deletes_ours_and_the_test_rule_and_nothing_else_sr175(tmp_path):
    box = _Box(tmp_path, rules=[OURS, OTHER, TEST_RULE])
    r = box.run("remove")
    assert r.returncode == 0, r.out
    assert box.rules == [OTHER]
    assert box.systemctl == [], "remove does not touch the unit"


# --- TC-1859 --------------------------------------------------------------
def test_every_compose_network_is_a_br_bridge_and_v4_only_sr175():
    """`br-+` covers a network only if Docker names its bridge br-<id>, which a
    custom bridge name or the default bridge would not; and the rule is v4
    only, which is complete only while no network enables IPv6."""
    for path in sorted(STACK.rglob("docker-compose*.yml")):
        text = _live(path.read_text(encoding="utf-8"))
        assert "com.docker.network.bridge.name" not in text, path
        assert not re.search(r"network_mode:\s*['\"]?bridge\b", text), path
        assert not re.search(r"enable_ipv6:\s*true", text), path
