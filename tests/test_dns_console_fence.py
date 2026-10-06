"""The DNS-console fence, the Cockpit drop-in and the firstboot wiring for both.

ADMIN_PORTAL_PLAN_2026-09-22 §4.5 (findings 5, B1), D-A3, D-A5.

WHAT THIS FILE IS FOR. The behaviour of the fence - where its rules land, what
counts as drift, that an alert can never fail it - is asserted by EXECUTING the
script against a fake netfilter (and, where the kernel allows, real iptables in
a throwaway namespace) in stack/dns-console/tests/fence.test.sh, and firstboot's
htpasswd step in stack/autoinstall/tests/admin-htpasswd.test.sh; both run from
test_hermetic_shell_suites.py. This file holds the properties that live in
unit files and in firstboot, where there is nothing to execute off the box:
the systemd dependencies that make the fence re-run after every tailscaled
start, the absence of any Requires= on it (D-A5), and the reset line without
which the Cockpit drop-in only ADDS a listener. Each of these is one edited line
away from silently not working.
"""
import configparser
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
DC = STACK / "dns-console"
FENCE = DC / "dns-console-fence.sh"
BOOT = DC / "homehub-dns-console.service"
HOOK = DC / "homehub-dns-console-tailscaled.service"
CHECK = DC / "homehub-dns-console-check.service"
TIMER = DC / "homehub-dns-console-check.timer"
COCKPIT = STACK / "cockpit" / "listen.conf"
FIRSTBOOT = STACK / "autoinstall" / "firstboot.sh"
RUNNER = STACK / "run-hermetic-tests.sh"

pytestmark = pytest.mark.smoke


def _directives(path):
    """(section, key) -> [values] with comments dropped; systemd allows repeats."""
    out, section = {}, None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, _, value = line.partition("=")
        out.setdefault((section, key.strip()), []).append(value.strip())
    return out


def _words(d, section, key):
    return " ".join(d.get((section, key), [])).split()


def test_every_file_is_lf_only():
    for p in (FENCE, BOOT, HOOK, CHECK, TIMER, COCKPIT, DC / "tests" / "fence.test.sh"):
        assert b"\r" not in p.read_bytes(), f"{p.name} has CR line endings"


def test_boot_unit_runs_before_docker_and_is_required_by_nothing():
    d = _directives(BOOT)
    assert "docker.service" in _words(d, "Unit", "Before")
    assert _words(d, "Unit", "DefaultDependencies") == ["no"]
    assert d[("Service", "Type")] == ["oneshot"]
    assert d[("Service", "RemainAfterExit")] == ["yes"]
    assert d[("Service", "ExecStart")] == ["/opt/homehub/stack/dns-console/dns-console-fence.sh install"]
    assert ("Unit", "Requires") not in d and ("Install", "RequiredBy") not in d
    # Ordering only, never a pull: a sibling fence's failure must not stop this.
    after = _words(d, "Unit", "After")
    assert "homehub-game-isolation.service" in after and "homehub-llm-isolation.service" in after
    assert "homehub-game-isolation.service" not in _words(d, "Unit", "Wants")


def test_nothing_requires_the_fence_d_a5():
    """Technitium is the household resolver. A firewall hiccup must not become
    no DNS for the house, so no unit may Requires=/BindsTo= the fence and the
    compose file may not reference it at all."""
    for unit in STACK.rglob("*.service"):
        d = _directives(unit)
        for key in ("Requires", "BindsTo", "Requisite"):
            assert not any("homehub-dns-console" in w for w in _words(d, "Unit", key)), (
                f"{unit.relative_to(ROOT)} {key}= the DNS-console fence (D-A5)")
    assert "homehub-dns-console" not in (STACK / "docker-compose.yml").read_text(encoding="utf-8")


def test_hook_reruns_on_every_tailscaled_start_or_restart():
    """systemd v255 transaction.c pulls a start job for Wants= units on both
    JOB_START and JOB_RESTART. That only RUNS the unit if it is inactive, so
    RemainAfterExit must stay off here - an active oneshot treats start as a
    no-op, and the hook would fire once per boot and never again."""
    d = _directives(HOOK)
    assert _words(d, "Install", "WantedBy") == ["tailscaled.service"]
    assert "tailscaled.service" in _words(d, "Unit", "After")
    assert "tailscaled.service" in _words(d, "Unit", "PartOf")
    assert d.get(("Service", "RemainAfterExit"), ["no"]) == ["no"]
    assert d[("Service", "ExecStart")] == ["/opt/homehub/stack/dns-console/dns-console-fence.sh after-tailscaled"]
    # The script waits up to 90 s for the jump and settles for 10 s.
    assert int(d[("Service", "TimeoutStartSec")][0]) > 100


def test_check_timer_fires_every_two_minutes():
    t = _directives(TIMER)
    assert t[("Timer", "OnUnitActiveSec")] == ["2min"]
    assert t[("Timer", "OnBootSec")] == ["2min"]
    assert _words(t, "Install", "WantedBy") == ["timers.target"]
    c = _directives(CHECK)
    assert c[("Service", "ExecStart")] == ["/opt/homehub/stack/dns-console/dns-console-fence.sh check"]
    assert c.get(("Service", "RemainAfterExit"), ["no"]) == ["no"], "a timer cannot re-run an active oneshot"


def test_fence_is_one_negated_reject_per_family_on_one_port():
    """One REJECT per family, never an ACCEPT: llm-isolation.sh reads INPUT back
    and dies if a foreign ACCEPT sits above its deny, and this fence sits above
    ts-input, so above the LLM rules. fence.test.sh R7/R8 run the real
    llm-isolation.sh over both designs; this pins the shape at the source."""
    src = FENCE.read_text(encoding="utf-8")
    assert 'MARKER="homehub-dns-console"' in src
    assert "PORT=5380" in src
    assert 'DEFAULT_SUBNET="172.28.92.0/24"' in src, "must agree with the pinned `admin` network"
    specs = dict(re.findall(r"^(R_V[46])=\((.*)\)$", src, re.M))
    assert set(specs) == {"R_V4", "R_V6"}
    for name, spec in specs.items():
        assert '-p tcp -m tcp --dport "$PORT"' in spec, f"{name} must match tcp/5380 only"
        assert spec.endswith("-j REJECT --reject-with tcp-reset"), name
        assert "! -i lo" in spec, f"{name} must exempt loopback"
    assert '! -s "$SUBNET"' in specs["R_V4"]
    assert "-s" not in specs["R_V6"].replace("--", ""), "the admin network has no v6"
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "-j ACCEPT" not in code, "no ACCEPT anywhere in the fence's code"
    assert re.search(r'-I INPUT "\$at"', code) and 'ipt "$bin" -A' not in code, "inserts above the jump, never appends"


def test_cockpit_dropin_resets_then_binds_ipv4_loopback_only():
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    lines = [ln for ln in COCKPIT.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")]
    listens = [ln.split("=", 1)[1].strip() for ln in lines if ln.strip().startswith("ListenStream")]
    assert listens == ["", "127.0.0.1:9090"], (
        "the empty reset must come first, or the packaged ListenStream=9090 stays "
        f"and this only adds a listener: {listens}")
    assert not any("::" in v for v in listens), "a [::1] listener can fail the whole socket on a v6-less kernel"
    parser.read_string("\n".join(lines))
    assert parser.sections() == ["Socket"]


def _block(text, start, end):
    i = text.index(start)
    return text[i:text.index(end, i)]


def test_firstboot_installs_enables_and_restarts_the_fence_without_dying():
    fb = FIRSTBOOT.read_text(encoding="utf-8")
    blk = _block(fb, "4-pre-a3. DNS CONSOLE FENCE", "4-pre-a4. COCKPIT")
    for unit in ("homehub-dns-console.service", "homehub-dns-console-tailscaled.service",
                 "homehub-dns-console-check.service", "homehub-dns-console-check.timer"):
        assert unit in blk
    assert "systemctl restart homehub-dns-console.service" in blk, "start is a no-op on an active unit"
    assert "systemctl enable homehub-dns-console.service homehub-dns-console-tailscaled.service" in blk
    assert "chmod 0755" in blk
    # WARN, never die: every command that can fail carries its own `|| log`.
    code = [ln for ln in blk.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    for i, ln in enumerate(code):
        if re.match(r"\s*(install|systemctl|chmod)\b", ln):
            tail = " ".join(code[i:i + 3])
            assert "|| log" in tail, f"firstboot line can abort under set -e: {ln.strip()}"


def test_firstboot_installs_the_cockpit_dropin_only_when_changed():
    fb = FIRSTBOOT.read_text(encoding="utf-8")
    blk = fb[fb.index("4-pre-a4. COCKPIT"):]
    blk = blk[:blk.index("\n# ── ", 10)] if "\n# ── " in blk[10:] else blk
    assert "systemctl cat cockpit.socket" in blk
    assert "cmp -s" in blk
    assert "/etc/systemd/system/cockpit.socket.d/listen.conf" in blk
    assert "systemctl restart cockpit.socket" in blk


def test_firstboot_writes_the_htpasswd_in_place_and_never_logs_it():
    fb = FIRSTBOOT.read_text(encoding="utf-8")
    blk = _block(fb, "2b. admin sign-in file", "── 3. load the baked image payload")
    assert re.search(r'printf .*> "\$HTPASSWD_FILE"', blk), "must truncate the existing inode"
    assert not re.search(r'(install|mv|cp)\s[^\n]*\$HTPASSWD_FILE', blk), "a new inode is invisible to a single-file bind mount"
    assert "chmod 0640" in blk and "ADMIN_AUTH_UID_GID=65532" in blk
    assert "REPLACE_WITH" in blk, "the .env.example placeholder must be refused"
    for ln in blk.splitlines():
        if "log " in ln and not ln.lstrip().startswith("#"):
            assert "__admin_hash" not in ln and "ADMIN_AUTH_HASH\"" not in ln, f"logs the hash: {ln.strip()}"


def test_the_hermetic_runner_runs_both_suites():
    runner = RUNNER.read_text(encoding="utf-8")
    assert '"dns-console/tests/fence.test.sh"' in runner
    assert '"autoinstall/tests/admin-htpasswd.test.sh"' in runner


def test_firstboot_names_the_exit_node_approval_only_when_advertised():
    fb = FIRSTBOOT.read_text(encoding="utf-8")
    i = fb.index("approve Use as exit node for homehub")
    assert 'TAILSCALE_ADVERTISE_EXIT_NODE' in fb[i - 300:i], "the step is owed only when the knob is true"
    assert "does not survive a reimage" in fb[i:i + 300]
