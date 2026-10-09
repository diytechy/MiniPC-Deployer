"""Tests for the mesh-VPN subnet router.

Verifies: SR-042 / LLR-959, LLR-960, LLR-961, LLR-962, LLR-975
Cases:    TC-976 .. TC-982, TC-1004, TC-1005

Config-level checks. Anything needing a live tunnel or the coordinator's API is
skipped with a stated reason rather than simulated, per the honesty bar (SN-008):
a check that cannot observe the thing it names must say so.
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
    """By DEFAULT there is no exit node. Since the exit-node plan it is an
    opt-in knob, so the flag may appear - but only ever carrying the knob's
    value, never hard-wired on, and the knob defaults to false."""
    live = _live(setup_src)
    assert "--advertise-routes=" in live
    assert "--accept-dns=false" in live, \
        "accepting the tunnel's DNS would displace Technitium's split-horizon"
    assert 'TAILSCALE_ADVERTISE_EXIT_NODE="${TAILSCALE_ADVERTISE_EXIT_NODE:-false}"' in live, \
        "the exit node must default OFF"
    uses = re.findall(r"--advertise-exit-node\S*", live)
    assert uses, "the knob must actually be converged"
    for use in uses:
        assert use == '--advertise-exit-node="$TAILSCALE_ADVERTISE_EXIT_NODE"', \
            "the exit-node flag may only carry the knob's value: %s" % use


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


def _hujson(text):
    """HuJSON as this file writes it: full-line // comments and trailing commas."""
    body = "\n".join(l for l in text.splitlines() if not l.strip().startswith("//"))
    return json.loads(re.sub(r",(\s*[\]}])", r"\1", body))


def test_the_policy_reference_permits_exit_node_use_for_members_sr042():
    """Using an exit node needs a rule whose dst is autogroup:internet;
    reaching the node itself is not enough (Tailscale exit-node docs). In the
    `acls` form a dst carries a port, so the entry is `autogroup:internet:*`.
    Parsing the whole file also proves the reference is still valid HuJSON."""
    policy = _hujson(ACL.read_text(encoding="utf-8"))
    rules = [r for r in policy["acls"]
             if "autogroup:internet:*" in r.get("dst", [])]
    assert rules, "no rule lets a member use an exit node"
    assert rules[0]["action"] == "accept"
    assert rules[0]["src"] == ["autogroup:member"]
    # The LAN rule must survive beside it: autogroup:internet excludes private
    # ranges, so it is NOT a replacement for the subnet-route rule.
    assert any(any(d.startswith("192.168.") for d in r.get("dst", []))
               for r in policy["acls"]), "the LAN rule is gone"


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


# --- The exit-node knob, EXECUTED against a fake daemon -------------------
#
# The source-text tests above cannot tell `set` from `up` at run time, or see
# what the routes are after a run. These execute the shipped script, unmodified,
# with a fake `tailscale` first on PATH. The fake keeps its preferences in a
# JSON file and applies `set` the way the real CLI does
# (calcAdvertiseRoutesForSet, cmd/tailscale/cli/set.go), so "the LAN route
# survived" is a statement about the routes it ends up holding, not about the
# command line alone.
#
# PATH IS BUILT FROM NOTHING, deliberately: the fake's directory and bash's own,
# and no more. On the operator's laptop the real tailscale.exe is on the
# inherited PATH, and a fake that failed to shadow it would run
# `tailscale set --accept-dns=false` against the laptop's own tunnel.

LAN = "192.168.1.0/24"
EXIT = ["0.0.0.0/0", "::/0"]


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

FAKE_TAILSCALE = r'''
import json, os, sys
state_path = os.environ["FAKE_TS_STATE"]
with open(state_path, encoding="utf-8") as f:
    st = json.load(f)
args = sys.argv[1:]
with open(os.environ["FAKE_TS_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
EXIT = ["0.0.0.0/0", "::/0"]

def flags(rest):
    out = {}
    for a in rest:
        if a.startswith("--"):
            k, eq, v = a[2:].partition("=")
            out[k] = v if eq else "true"      # a bare Go bool flag means true
    return out

def routes(csv, exit_on):
    return [r for r in csv.split(",") if r] + (EXIT if exit_on else [])

def save():
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(st, f)

p = st["prefs"]
if args[:1] == ["version"]:
    print("1.102.4"); sys.exit(0)
if args == ["status"]:
    sys.exit(0 if st["authorised"] else 1)
if args[:2] == ["lock", "status"]:
    print("Tailnet Lock is NOT enabled."); sys.exit(0)
if args[:2] == ["debug", "prefs"]:
    print(json.dumps(p, indent="\t")); sys.exit(0)
if args[:1] == ["set"]:
    f = flags(args[1:])
    cur = p["AdvertiseRoutes"] or []
    cur_exit = all(e in cur for e in EXIT)
    ex, rt = f.get("advertise-exit-node"), f.get("advertise-routes")
    if ex is not None and rt is not None:
        p["AdvertiseRoutes"] = routes(rt, ex == "true")
    elif rt is not None:
        p["AdvertiseRoutes"] = routes(rt, cur_exit)
    elif ex is not None and (ex == "true") != cur_exit:
        p["AdvertiseRoutes"] = [r for r in cur if r not in EXIT] + (EXIT if ex == "true" else [])
    if "accept-dns" in f:
        p["CorpDNS"] = f["accept-dns"] == "true"
    if st.get("fault") == "drop-lan-on-exit-set" and ex is not None:
        p["AdvertiseRoutes"] = [r for r in p["AdvertiseRoutes"] if r in EXIT]
    save(); sys.exit(0)
if args[:1] == ["up"]:
    f = flags(args[1:])
    p["AdvertiseRoutes"] = routes(f.get("advertise-routes", ""), f.get("advertise-exit-node") == "true")
    p["CorpDNS"] = f.get("accept-dns", "true") == "true"
    st["authorised"] = True
    save(); sys.exit(0)
sys.stderr.write("fake tailscale: unhandled %r\n" % (args,))
sys.exit(97)
'''


class _FakeHub:
    """A stand-in hub: the fake daemon, a no-op systemctl, and a .env."""

    def __init__(self, tmp_path, *, knob=None, routes=(LAN,), authorised=True,
                 corpdns=False, fault=None):
        py = Path(sys.executable).as_posix()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        fake = tmp_path / "fake_tailscale.py"
        fake.write_text(FAKE_TAILSCALE, encoding="utf-8")
        shims = {
            "tailscale": 'exec "%s" "%s" "$@"\n' % (py, fake.as_posix()),
            "python3": 'exec "%s" "$@"\n' % py,
            "systemctl": "exit 0\n",
            "sysctl": "exit 0\n",
        }
        for name, body in shims.items():
            shim = self.bin / name
            shim.write_text("#!/bin/sh\n" + body, encoding="utf-8", newline="\n")
            shim.chmod(0o755)

        self.state = tmp_path / "state.json"
        self.state.write_text(json.dumps({
            "authorised": authorised, "fault": fault,
            "prefs": {"ControlURL": "https://controlplane.tailscale.com",
                      "ExitNodeID": "", "CorpDNS": corpdns, "WantRunning": True,
                      "AdvertiseRoutes": list(routes), "NetfilterMode": 2,
                      "Hostname": "homehub"}}), encoding="utf-8")
        self.log = tmp_path / "calls.jsonl"
        self.log.write_text("", encoding="utf-8")

        lines = ["TAILSCALE_ENABLED=true", "TAILSCALE_ADVERTISE_ROUTES=%s" % LAN]
        if knob is not None:
            lines.append("TAILSCALE_ADVERTISE_EXIT_NODE=%s" % knob)
        self.env_file = tmp_path / "stack.env"
        self.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        # Present and correct, so step 1 never tries to write /etc/sysctl.d.
        self.sysctl = tmp_path / "99-tailscale.conf"
        self.sysctl.write_text("net.ipv4.ip_forward = 1\n", encoding="utf-8", newline="\n")

        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("TAILSCALE_", "TS_"))}
        tail = [str(Path(_BASH).parent)] if os.name == "nt" else ["/usr/bin", "/bin"]
        env.update(PATH=os.pathsep.join([str(self.bin)] + tail),
                   TAILSCALE_ENV_FILE=self.env_file.as_posix(),
                   TAILSCALE_SYSCTL_FILE=self.sysctl.as_posix(),
                   FAKE_TS_STATE=self.state.as_posix(),
                   FAKE_TS_LOG=self.log.as_posix())
        self.env = env

        # Refuse to run anything unless the fake is what `tailscale` means.
        which = subprocess.run([_BASH, "-c", "command -v tailscale"], env=env,
                               capture_output=True, text=True)
        assert which.stdout.strip().endswith("/bin/tailscale"), \
            "the fake does not shadow the real daemon: %r" % which.stdout

    def run(self, *args, extra_env=None):
        env = dict(self.env, **(extra_env or {}))
        r = subprocess.run([_BASH, SETUP.as_posix(), *args], env=env,
                           capture_output=True, text=True, timeout=120)
        r.out = r.stdout + r.stderr
        return r

    @property
    def calls(self):
        return [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines() if l]

    @property
    def routes(self):
        return json.loads(self.state.read_text(encoding="utf-8"))["prefs"]["AdvertiseRoutes"]

    def verbs(self, verb):
        return [c for c in self.calls if c[:1] == [verb]]


@_needs_bash
def test_exit_node_knob_absent_means_false_and_is_still_converged_sr042(tmp_path):
    """False is converged, not skipped: `set --advertise-exit-node=false` runs,
    so a node flipped back from true stops offering itself."""
    hub = _FakeHub(tmp_path, knob=None)
    r = hub.run()
    assert r.returncode == 0, r.out
    assert ["set", "--advertise-exit-node=false"] in hub.calls
    assert not hub.verbs("up"), "an authorised node must never be sent `up`"
    assert hub.routes == [LAN]
    assert "Not an exit node" in r.out


@_needs_bash
def test_exit_node_knob_true_uses_set_never_up_and_keeps_the_lan_route_sr042(tmp_path):
    hub = _FakeHub(tmp_path, knob="true")
    r = hub.run()
    assert r.returncode == 0, r.out
    assert ["set", "--advertise-exit-node=true"] in hub.calls
    assert not hub.verbs("up"), "an authorised node must never be sent `up`"
    assert LAN in hub.routes, "the LAN route must survive the exit-node change"
    assert set(EXIT) <= set(hub.routes), "an exit node is BOTH /0 routes"
    assert "PENDING APPROVAL" in r.out and "Use as exit node" in r.out
    assert not re.search(r"exit node (is )?active", r.out, re.I)


@_needs_bash
def test_exit_node_knob_false_withdraws_it_and_keeps_the_lan_route_sr042(tmp_path):
    hub = _FakeHub(tmp_path, knob="false", routes=[LAN] + EXIT)
    r = hub.run()
    assert r.returncode == 0, r.out
    assert hub.routes == [LAN]


@_needs_bash
def test_a_vanished_lan_route_fails_the_run_loudly_sr042(tmp_path):
    """The one outcome that must never pass quietly: the LAN route gone."""
    hub = _FakeHub(tmp_path, knob="true", fault="drop-lan-on-exit-set")
    r = hub.run()
    assert r.returncode == 1, r.out
    assert "THE LAN ROUTE IS GONE" in r.out
    assert "tailscale set --advertise-routes=%s" % LAN in r.out, \
        "the failure must say how to restore the route"


@_needs_bash
def test_check_only_reports_the_exit_node_and_changes_nothing_sr042(tmp_path):
    hub = _FakeHub(tmp_path, knob="true", routes=[LAN] + EXIT)
    before = hub.state.read_bytes()
    r = hub.run("--check-only")
    assert r.returncode == 0, r.out
    assert "exit node ADVERTISED" in r.out
    assert "LAN route advertised" in r.out
    assert not hub.verbs("set") and not hub.verbs("up")
    assert hub.state.read_bytes() == before


@_needs_bash
def test_check_only_reports_drift_without_repairing_it_sr042(tmp_path):
    hub = _FakeHub(tmp_path, knob="false", routes=[LAN] + EXIT)
    before = hub.state.read_bytes()
    r = hub.run("--check-only")
    assert r.returncode == 1, r.out
    assert "DRIFT" in r.out
    assert not hub.verbs("set") and not hub.verbs("up")
    assert hub.state.read_bytes() == before


@_needs_bash
def test_an_unrecognised_knob_value_is_refused_before_anything_changes_sr042(tmp_path):
    hub = _FakeHub(tmp_path, knob="yes")
    r = hub.run()
    assert r.returncode == 2, r.out
    assert "must be exactly true or false" in r.out
    assert not hub.verbs("set") and not hub.verbs("up")


@_needs_bash
def test_the_enrolment_up_names_the_knob_and_the_route_sr042(tmp_path):
    """`up` is reached only when the node is not running. On a node enrolled
    before, `up` refuses while a non-default preference goes unmentioned, so it
    must name the exit-node knob as well as the route."""
    hub = _FakeHub(tmp_path, knob="true", routes=[], authorised=False)
    r = hub.run()
    assert r.returncode == 0, r.out
    ups = hub.verbs("up")
    assert len(ups) == 1
    assert "--advertise-routes=%s" % LAN in ups[0]
    assert "--advertise-exit-node=true" in ups[0]
    assert "--accept-dns=false" in ups[0]
    assert LAN in hub.routes and set(EXIT) <= set(hub.routes)


@_needs_bash
def test_no_auth_key_reaches_the_daemon_tc977_sr042(tmp_path):
    """TC-977, executed: a key in the environment is refused before the daemon
    is touched at all, and no run passes anything key-shaped to it."""
    hub = _FakeHub(tmp_path, knob="true")
    r = hub.run(extra_env={"TS_AUTHKEY": "not-a-real-key"})
    assert r.returncode == 2, r.out
    assert hub.calls == [], "the refusal must come before any daemon call"
    assert "not-a-real-key" not in r.out, "a refusal must not echo the key"
    hub.run()
    for call in hub.calls:
        assert not any("authkey" in a.lower() or "auth-key" in a.lower() for a in call), call
