"""TC-1172..TC-1175 and the egress/logging rows - the sleep feeder's unit, timer,
setup script, knobs, destinations and journal.

Asserted against the other side wherever there is one:

  * the unit's hardening is compared with the WEIGHT feeder's unit, which it was
    told to copy, directive by directive (TC-1172);
  * the setup script is RUN (`--emit-dropin`, `--check-only`, a refusal) and what
    it wrote is read back, never grepped out of the script (TC-1173);
  * the `.env.example` block, and the README's honest statements, are checked as
    text because they are text (TC-1174);
  * the feed POST is made against real loopback sockets: a redirect is refused and
    the second host receives nothing, an exported proxy is ignored, an off-box
    destination is refused (TC-1175);
  * the journal never carries a score or a credential (TC-1175).

Verifies: TC-1172, TC-1173, TC-1174, TC-1175 (SR-070, SR-072, LLR-1073..LLR-1076,
          LLR-1087)
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import loopback_server, plain_200, redirect_to
from test_sleep_support import CENTRAL, REPO, Harness, feeder, local

SLEEP = REPO / "stack" / "sleep"
UNIT = SLEEP / "homehub-sleep.service"
TIMER = SLEEP / "homehub-sleep.timer"
SETUP = SLEEP / "setup-sleep.sh"
README = SLEEP / "README.md"
ENV_EXAMPLE = REPO / "stack" / ".env.example"
WEIGHT_UNIT = REPO / "stack" / "weight" / "homehub-weight.service"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def directives(path):
    """{(section, key): [values]} with comments and blank lines dropped."""
    out, section = {}, None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, _, value = line.partition("=")
        out.setdefault((section, key.strip()), []).append(value.strip())
    return out


# ── TC-1172 : unit and timer ─────────────────────────────────────────────────

def test_the_unit_runs_as_its_own_account_with_a_private_state_directory():
    u = directives(UNIT)
    assert u[("Service", "Type")] == ["oneshot"]
    assert u[("Service", "User")] == ["homehub-sleep"] and u[("Service", "Group")] == ["homehub-sleep"]
    assert u[("Service", "StateDirectory")] == ["homehub-sleep"]
    assert u[("Service", "StateDirectoryMode")] == ["0700"]
    assert u[("Service", "ReadWritePaths")] == ["/var/lib/homehub-sleep"]
    assert u[("Service", "WorkingDirectory")] == ["/var/lib/homehub-sleep"]
    assert u[("Service", "EnvironmentFile")] == ["/opt/homehub/stack/.env"]
    assert u[("Service", "ExecStart")] == ["/usr/bin/python3 /opt/homehub/stack/sleep/sleep_feeder.py"]
    assert u[("Service", "ExecStartPre")] == [
        "/usr/bin/python3 /opt/homehub/stack/sleep/sleep_feeder.py --check"]
    assert u[("Service", "ProtectHome")] == ["yes"]
    assert ("Service", "Restart") not in u            # a rejected password counts toward a lockout
    assert int(u[("Service", "TimeoutStartSec")][0]) >= 5 * feeder.DEFAULT_TIMEOUT_SECONDS


def test_the_unit_copies_the_weight_feeders_hardening_directive_for_directive():
    sleep, weight = directives(UNIT), directives(WEIGHT_UNIT)
    hardening = ("NoNewPrivileges", "ProtectSystem", "PrivateTmp", "PrivateDevices",
                 "ProtectKernelTunables", "ProtectKernelModules", "ProtectControlGroups",
                 "RestrictSUIDSGID", "RestrictNamespaces", "LockPersonality",
                 "CapabilityBoundingSet", "AmbientCapabilities", "SystemCallFilter",
                 "SystemCallErrorNumber", "UMask")
    for key in hardening:
        assert ("Service", key) in weight, "the weight unit no longer has %s" % key
        assert sleep.get(("Service", key)) == weight[("Service", key)], key


def test_every_path_the_feeder_may_write_is_the_same_directory():
    """The unit, the setup script, the example knob and the feeder's own default."""
    assert feeder.DEFAULT_STATE_ROOT == "/var/lib/homehub-sleep"
    assert directives(UNIT)[("Service", "StateDirectory")] == ["homehub-sleep"]
    assert "SLEEP_STATE_DIR=/var/lib/homehub-sleep\n" in SETUP.read_text(encoding="utf-8")
    assert re.search(r"^SLEEP_STATE_FILE=/var/lib/homehub-sleep/sleep-state\.json$",
                     ENV_EXAMPLE.read_text(encoding="utf-8"), re.M)
    assert feeder.DEFAULT_STATE_NAME == "sleep-state.json"


def test_the_timer_runs_every_thirty_minutes_inside_the_daily_horizon():
    t = directives(TIMER)
    assert t[("Timer", "OnUnitActiveSec")] == ["30min"]
    assert t[("Timer", "Persistent")] == ["false"]
    minutes = int(re.match(r"^(\d+)min$", t[("Timer", "OnUnitActiveSec")][0]).group(1))
    # NagLight's daily horizon is 36 h; the cadence must stay far inside it.
    assert minutes * 60 <= feeder.DAILY_STALE_SECONDS // 4
    assert t[("Install", "WantedBy")] == ["timers.target"]


def test_the_daily_horizon_the_cadence_is_judged_against_is_naglights_own():
    gauge_go = REPO.parent / "NagLight" / "internal" / "gauge" / "gauge.go"
    if not gauge_go.exists():
        pytest.skip("no sibling NagLight checkout")
    source = gauge_go.read_text(encoding="utf-8")
    assert re.search(r'case\s+"daily"\s*:\s*\n?\s*return\s+36\s*\*\s*time\.Hour', source) or \
        "36 * time.Hour" in source or "36*time.Hour" in source


# ── TC-1173 : the setup script, run ──────────────────────────────────────────

def run_setup(tmp_path, args, env_text=None, **extra):
    env_file = tmp_path / ".env"
    env_file.write_bytes((env_text if env_text is not None else "").encode("utf-8"))
    env = dict(os.environ, SLEEP_ENV_FILE=str(env_file).replace("\\", "/"),
               SLEEP_UNIT_DIR=str(tmp_path / "units").replace("\\", "/"))
    for key in ("SLEEP_ENABLED", "SLEEP_USER", "SLEEP_USER_ACCOUNT", "SLEEP_FEED_URL"):
        env.pop(key, None)
    env.update(extra)
    return subprocess.run(["bash", str(SETUP).replace("\\", "/")] + args, env=env,
                          capture_output=True, text=True)


GOOD_ENV = ("SLEEP_ENABLED=true\nSLEEP_USER=household-member-1\n"
            "SLEEP_FEED_URL=http://127.0.0.1:8787/api/feed\n"
            "MYAIR_EMAIL=sleeper@example.invalid\n"  # privacy-ok: placeholder
            "MYAIR_PASSWORD=PASSWORD-SENTINEL-6120\n")


@needs_bash
def test_the_setup_script_parses():
    proc = subprocess.run(["bash", "-n", str(SETUP).replace("\\", "/")], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@needs_bash
def test_the_drop_in_names_the_account_from_the_knob_and_nothing_else(tmp_path):
    out = tmp_path / "dropin"
    proc = run_setup(tmp_path, ["--emit-dropin", str(out).replace("\\", "/")], GOOD_ENV,
                     SLEEP_USER_ACCOUNT="acct-from-the-knob")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    conf = (out / "10-account.conf").read_text(encoding="utf-8")
    assert "User=acct-from-the-knob" in conf and "Group=acct-from-the-knob" in conf
    assert "PASSWORD-SENTINEL" not in conf and "BindReadOnlyPaths" not in conf


@needs_bash
def test_check_only_passes_with_a_full_configuration_and_never_echoes_the_sign_in(tmp_path):
    proc = run_setup(tmp_path, ["--check-only"], GOOD_ENV)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASSWORD-SENTINEL" not in proc.stdout + proc.stderr
    assert "sleeper@example" not in proc.stdout + proc.stderr


@needs_bash
@pytest.mark.parametrize("drop,needle", [
    ("SLEEP_USER", "SLEEP_USER"), ("SLEEP_FEED_URL", "SLEEP_FEED_URL"),
    ("MYAIR_EMAIL", "MYAIR_EMAIL"), ("MYAIR_PASSWORD", "MYAIR_PASSWORD")])
def test_the_setup_script_refuses_when_something_it_needs_is_blank(tmp_path, drop, needle):
    text = "".join(line + "\n" for line in GOOD_ENV.splitlines() if not line.startswith(drop + "="))
    proc = run_setup(tmp_path, ["--check-only"], text)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "REFUSED" in proc.stdout and needle in proc.stdout
    assert "PASSWORD-SENTINEL" not in proc.stdout + proc.stderr


def stub_timedatectl(tmp_path, answer):
    stub = tmp_path / "timedatectl-stub"
    stub.write_bytes(("#!/bin/sh\necho %s\n" % answer).encode("ascii"))
    os.chmod(str(stub), 0o755)
    return str(stub).replace("\\", "/")


def sync_lines(proc):
    return [line for line in proc.stdout.splitlines() if "clock sync" in line]


@needs_bash
def test_install_time_probe_reports_the_timesyncd_flag_file(tmp_path):
    flag = tmp_path / "synchronized"
    flag.write_bytes(b"")
    proc = run_setup(tmp_path, ["--check-only"], GOOD_ENV, SLEEP_SYNC_FLAG=str(flag).replace("\\", "/"),
                     SLEEP_TIMEDATECTL="/nonexistent/timedatectl")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = sync_lines(proc)
    assert len(lines) == 1 and "is present" in lines[0] and "WARNING" not in lines[0]


@needs_bash
@pytest.mark.parametrize("answer,needle", [("yes", "NTPSynchronized=yes"), ("no", "NTPSynchronized=no")])
def test_install_time_probe_falls_back_to_timedatectl(tmp_path, answer, needle):
    proc = run_setup(tmp_path, ["--check-only"], GOOD_ENV,
                     SLEEP_SYNC_FLAG=str(tmp_path / "absent").replace("\\", "/"),
                     SLEEP_TIMEDATECTL=stub_timedatectl(tmp_path, answer))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = sync_lines(proc)
    assert len(lines) == 1 and needle in lines[0] and "WARNING" not in lines[0]
    if answer == "no":
        assert "grey" in lines[0] and "systemd-timesyncd" in lines[0]


@needs_bash
def test_install_time_probe_warns_when_there_is_no_signal_at_all_and_is_not_fatal(tmp_path):
    proc = run_setup(tmp_path, ["--check-only"], GOOD_ENV,
                     SLEEP_SYNC_FLAG=str(tmp_path / "absent").replace("\\", "/"),
                     SLEEP_TIMEDATECTL="/nonexistent/timedatectl")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = sync_lines(proc)
    assert len(lines) == 1 and lines[0].startswith("WARNING clock sync:")
    assert "grey" in lines[0] and "systemd-timesyncd" in lines[0]


def test_the_install_path_probes_before_it_installs_anything():
    script = SETUP.read_text(encoding="utf-8")
    assert "\nprobe_clock_sync\n\nif ! id -u" in script           # before the account, the units, anything
    assert script.count("probe_clock_sync") >= 3                    # defined, check-only, install


@needs_bash
def test_a_blank_quoted_password_is_blank(tmp_path):
    text = GOOD_ENV.replace("MYAIR_PASSWORD=PASSWORD-SENTINEL-6120", 'MYAIR_PASSWORD=""')
    assert run_setup(tmp_path, ["--check-only"], text).returncode == 2


@needs_bash
@pytest.mark.parametrize("account", ["hub", "root"])
def test_the_setup_script_refuses_a_privileged_account(tmp_path, account):
    proc = run_setup(tmp_path, ["--check-only"], GOOD_ENV, SLEEP_USER_ACCOUNT=account)
    assert proc.returncode == 2 and "REFUSED" in proc.stdout


@needs_bash
def test_disabled_installs_nothing_and_says_so(tmp_path):
    proc = run_setup(tmp_path, [], GOOD_ENV.replace("SLEEP_ENABLED=true", "SLEEP_ENABLED=false"))
    assert proc.returncode == 0 and "not installed" in proc.stdout
    assert not (tmp_path / "units").exists()


def test_the_install_path_enables_the_timer_and_never_the_oneshot_service():
    script = SETUP.read_text(encoding="utf-8")
    assert "systemctl enable --now homehub-sleep.timer" in script
    assert "enable --now homehub-sleep.service" not in script
    assert "useradd --system --no-create-home --shell /usr/sbin/nologin" in script
    assert 'install -d -m 0700 -o "$SLEEP_USER_ACCOUNT"' in script


def test_the_setup_script_never_holds_or_prints_the_myair_values():
    """MYAIR_* may be asked about (`env_present`) and never fetched into a variable."""
    script = SETUP.read_text(encoding="utf-8")
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    assert 'env_get MYAIR' not in code
    assert re.search(r'for key in SLEEP_ENABLED SLEEP_USER SLEEP_USER_ACCOUNT SLEEP_FEED_URL;', code)
    assert 'echo "$MYAIR' not in code and 'say "$MYAIR' not in code


def test_line_endings_are_lf():
    for path in (SETUP, UNIT, TIMER, SLEEP / "sleep_feeder.py", README):
        assert b"\r" not in path.read_bytes(), path.name
    assert SETUP.read_bytes().startswith(b"#!/usr/bin/env bash\n")
    assert (SLEEP / "sleep_feeder.py").read_bytes().startswith(b"#!/usr/bin/env python3\n")


# ── TC-1174 : knobs and README ───────────────────────────────────────────────

KNOBS = {"SLEEP_ENABLED": "false", "SLEEP_USER": "", "SLEEP_USER_ACCOUNT": "homehub-sleep",
         "SLEEP_FEED_URL": "http://127.0.0.1:8787/api/feed", "SLEEP_FEED_TOKEN": "",
         "SLEEP_PUBLISH": "1", "MYAIR_EMAIL": "", "MYAIR_PASSWORD": "",
         "SLEEP_STATE_FILE": "/var/lib/homehub-sleep/sleep-state.json",
         "SLEEP_TIMEOUT_SECONDS": "30"}


def test_the_env_example_block_carries_every_knob_with_its_safe_default():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", text, re.M))
    for knob, default in KNOBS.items():
        assert knob in values, knob
        assert values[knob] == default, (knob, values[knob])
    # Exactly one line per knob: a duplicate would silently win in a .env.
    for knob in KNOBS:
        assert len(re.findall(r"^%s=" % knob, text, re.M)) == 1, knob
    # The two sign-in values are BLANK in a tracked file, always.
    assert values["MYAIR_EMAIL"] == "" and values["MYAIR_PASSWORD"] == ""


def test_the_example_publishes_by_default_and_documents_the_off_switch():
    assert feeder.resolve_publish({}) is True
    for off in ("0", "false", "FALSE", "no", "off", " 0 "):
        assert feeder.resolve_publish({"SLEEP_PUBLISH": off}) is False, off
    for on in ("1", "", "true", "yes", "2", "anything"):
        assert feeder.resolve_publish({"SLEEP_PUBLISH": on}) is True, on


def test_the_readme_states_what_is_not_proven_and_the_operator_path():
    text = README.read_text(encoding="utf-8")
    for must in ("startDate", "+ 1", "Unused nights", "UNKNOWN", "no refresh token",
                 "No email MFA", "MFA_REQUIRED", "refresh-gauge-logins", "MyAirPassword",
                 "household.MyAirEmail", "status only", "e2c52a30", "MIT License",
                 "frozen", "13:59", "14:00", "published night", "never a score"):
        assert must.lower() in text.lower(), must


# ── The destination and the identity (TC-1175) ────────────────────────────────

@pytest.mark.parametrize("url", [
    "", "ftp://127.0.0.1/api/feed", "http://192.0.2.5:8787/api/feed", "https://example.com/api/feed",
    "http://localhost:8787/api/feed", "http:///api/feed", "http://tracker.lan/api/feed"])
def test_a_feed_destination_off_this_box_is_refused(url):
    with pytest.raises(SystemExit) as caught:
        feeder.resolve_feed_url({"SLEEP_FEED_URL": url})
    assert "REFUSED" in str(caught.value)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8787/api/feed", "http://[::1]:8787/api/feed",
                                 "https://127.0.0.1/api/feed"])
def test_loopback_is_accepted(url):
    assert feeder.resolve_feed_url({"SLEEP_FEED_URL": url}) == url


def test_a_docker_bridge_address_is_accepted_only_when_the_box_carries_it():
    import ipaddress
    bridge = [ipaddress.ip_network("172.30.0.0/24")]
    assert feeder.is_local_destination("172.30.0.7", bridge) is True
    assert feeder.is_local_destination("172.31.0.7", bridge) is False
    assert feeder.is_local_destination("172.30.0.7", []) is False
    assert feeder.is_local_destination("tracker.internal", bridge) is False


@pytest.mark.parametrize("env", [{}, {"SLEEP_USER": ""}, {"SLEEP_USER": "   "}])
def test_the_identity_is_never_guessed(env):
    with pytest.raises(SystemExit) as caught:
        feeder.resolve_identity(env)
    assert "REFUSED" in str(caught.value) and "SLEEP_USER" in str(caught.value)
    assert feeder.resolve_identity({"SLEEP_USER": " abc "}) == "abc"


def test_a_feed_redirect_is_refused_and_the_second_host_receives_nothing():
    with loopback_server(plain_200) as (elsewhere, elsewhere_seen):
        with loopback_server(redirect_to(elsewhere + "/stolen")) as (feed, feed_seen):
            ok, detail = feeder.post_gauge(
                {"id": "sleep"}, feed + "/api/feed",
                {"_identity": "household-member-1", "SLEEP_FEED_TOKEN": "tracker-token-1"}, 5)
        assert ok is False and "redirect" in detail
        assert elsewhere_seen == []
        assert len(feed_seen) == 1


def test_an_exported_proxy_is_ignored(monkeypatch):
    with loopback_server(plain_200) as (proxy, proxy_seen):
        with loopback_server(plain_200) as (feed, feed_seen):
            monkeypatch.setenv("http_proxy", proxy)
            monkeypatch.setenv("HTTP_PROXY", proxy)
            ok, _detail = feeder.post_gauge({"id": "sleep"}, feed + "/api/feed",
                                            {"_identity": "household-member-1"}, 5)
    assert ok is True and len(feed_seen) == 1 and proxy_seen == []


def test_the_post_carries_the_identity_and_the_token_and_the_remotes_words_stay_out():
    def teapot(handler):
        handler.send_response(500)
        body = b"REFLECTED Bearer tracker-token-1 household-member-1"
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    with loopback_server(teapot) as (feed, seen):
        ok, detail = feeder.post_gauge({"id": "sleep"}, feed + "/api/feed",
                                       {"_identity": "household-member-1",
                                        "SLEEP_FEED_TOKEN": "tracker-token-1"}, 5)
    assert ok is False and "REFLECTED" not in detail and "tracker-token" not in detail
    assert seen[0]["headers"]["X-Forwarded-User"] == "household-member-1"
    assert seen[0]["headers"]["Authorization"] == "Bearer tracker-token-1"
    assert seen[0]["headers"]["Content-Type"] == "application/json"


def test_a_blank_token_sends_no_authorization_header():
    with loopback_server(plain_200) as (feed, seen):
        feeder.post_gauge({"id": "sleep"}, feed + "/api/feed",
                          {"_identity": "household-member-1", "SLEEP_FEED_TOKEN": ""}, 5)
    assert "Authorization" not in seen[0]["headers"]


# ── Gating, the off switch, TIMEZONE ─────────────────────────────────────────

def test_only_a_literal_true_enables_it():
    assert feeder.resolve_enabled({"SLEEP_ENABLED": "true"}) is True
    for off in (None, "", "false", "True", "TRUE", "yes", "1", " false"):
        assert feeder.resolve_enabled({} if off is None else {"SLEEP_ENABLED": off}) is False, off


def main_env(monkeypatch, tmp_path, **overrides):
    values = {"SLEEP_ENABLED": "true", "SLEEP_USER": "household-member-1",
              "SLEEP_FEED_URL": "http://127.0.0.1:8787/api/feed", "TIMEZONE": "Etc/UTC",
              "MYAIR_EMAIL": "sleeper@example.invalid", "MYAIR_PASSWORD": "PASSWORD-SENTINEL-6120",  # privacy-ok: placeholder
              "STATE_DIRECTORY": str(tmp_path), "SLEEP_PUBLISH": "1"}
    values.update(overrides)
    for key in list(os.environ):
        if key.startswith(("SLEEP_", "MYAIR_")) or key in ("TIMEZONE", "STATE_DIRECTORY"):
            monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        if value is not None:
            monkeypatch.setenv(key, value)


def test_the_off_switch_reads_nothing_and_posts_nothing(monkeypatch, tmp_path, capsys):
    main_env(monkeypatch, tmp_path, SLEEP_PUBLISH="0")

    def boom(*args, **kwargs):
        raise AssertionError("SLEEP_PUBLISH=0 must not read or post")

    monkeypatch.setattr(feeder, "read_myair", boom)
    monkeypatch.setattr(feeder, "post_gauge", boom)
    monkeypatch.setattr(feeder, "run_cycle", boom)
    assert feeder.main([]) == 0
    assert "SLEEP_PUBLISH is off" in capsys.readouterr().out
    assert list(Path(str(tmp_path)).iterdir()) == []             # not even a state file


def test_disabled_does_nothing(monkeypatch, tmp_path, capsys):
    main_env(monkeypatch, tmp_path, SLEEP_ENABLED="false")
    monkeypatch.setattr(feeder, "run_cycle", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    assert feeder.main([]) == 0
    assert "not true" in capsys.readouterr().out


def test_check_accepts_a_full_configuration_without_reading_or_posting(monkeypatch, tmp_path, capsys):
    main_env(monkeypatch, tmp_path)
    monkeypatch.setattr(feeder, "run_cycle", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    assert feeder.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "accepted" in out and "PASSWORD-SENTINEL" not in out and "sleeper@" not in out


@pytest.mark.parametrize("override", [
    {"SLEEP_USER": ""}, {"SLEEP_FEED_URL": "http://203.0.113.9/api/feed"},
    {"TIMEZONE": ""}, {"TIMEZONE": "Not/AZone"}, {"MYAIR_EMAIL": ""}, {"MYAIR_PASSWORD": ""}])
def test_check_refuses_a_configuration_it_cannot_run(monkeypatch, tmp_path, override):
    main_env(monkeypatch, tmp_path, **override)
    with pytest.raises(SystemExit) as caught:
        feeder.main(["--check"])
    assert "REFUSED" in str(caught.value)
    assert "PASSWORD-SENTINEL" not in str(caught.value)


def test_the_timezone_knob_resolves_utc_everywhere_and_others_by_zoneinfo():
    assert feeder.resolve_timezone({"TIMEZONE": "Etc/UTC"}) is feeder.dt.timezone.utc
    assert feeder.resolve_timezone({"TIMEZONE": "UTC"}) is feeder.dt.timezone.utc

    class FakeZoneInfo(object):
        @staticmethod
        def ZoneInfo(name):
            if name != "America/Chicago":
                raise KeyError(name)
            return CENTRAL

    assert feeder.resolve_timezone({"TIMEZONE": "America/Chicago"}, FakeZoneInfo) is CENTRAL
    with pytest.raises(SystemExit) as caught:
        feeder.resolve_timezone({"TIMEZONE": "Mars/Olympus"}, FakeZoneInfo)
    assert "REFUSED" in str(caught.value)


def test_the_zone_the_feeder_resolves_is_the_one_the_night_keys_use(monkeypatch, tmp_path):
    """TIMEZONE is not decoration: the same instant is a different expected night
    in a different zone."""
    instant = local(CENTRAL, 2026, 10, 12, 21, 0)                # 21:00 CDT = 02:00Z next day
    assert feeder.expected_night(instant, CENTRAL).isoformat() == "2026-10-12"
    assert feeder.expected_night(instant, feeder.dt.timezone.utc).isoformat() == "2026-10-13"


# ── TC-1175 : the journal ────────────────────────────────────────────────────

def test_a_full_cycle_prints_no_score_and_no_credential(monkeypatch, tmp_path, capsys):
    """Distinctive numbers go in; none may come out of stdout or stderr."""
    main_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TIMEZONE", "Etc/UTC")
    now = int(local(feeder.dt.timezone.utc, 2026, 10, 12, 8, 0))
    monkeypatch.setattr(feeder.time, "time", lambda: now)
    monkeypatch.setattr(feeder, "clock_synchronized", lambda: (True, "test"))
    monkeypatch.setattr(feeder, "post_gauge", lambda body, url, env, timeout: (True, "HTTP 200"))
    monkeypatch.setattr(
        feeder, "read_myair",
        lambda env, n, tz: ([feeder.Record(feeder.parse_night_date("2026-10-11"), 67)], 0))
    assert feeder.main([]) == 0
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "published night 2026-10-12" in text
    assert not re.search(r"\b67\b", text), text
    assert "PASSWORD-SENTINEL" not in text and "sleeper@" not in text
    state_text = (Path(str(tmp_path)) / "sleep-state.json").read_text(encoding="utf-8")
    assert "PASSWORD-SENTINEL" not in state_text and "sleeper@" not in state_text


def test_every_journal_line_the_cycle_can_build_is_free_of_health_values(tmp_path):
    h = Harness(tmp_path)
    lines = []
    scores = (67, 0, 100, 53.5)
    for index, score in enumerate(scores):
        _p, notes, failures = h.cycle(local(CENTRAL, 2026, 10, 12, 7 + index, 0),
                                      [("2026-10-11", score)])
        lines += notes + failures
    for score in (67, 53.5, 100):
        assert not any(re.search(r"(?<![\d-])%s(?![\d-])" % re.escape(str(score)), line)
                       for line in lines), score
    assert any(line.startswith("published night 2026-10-12") for line in lines)
