"""stack/ai-cli/gauge_login.py - the one table of gauge sign-ins, and the sign-in itself.

Owner 2026-11-10: "a little script ... so they are trivial to refresh". The tool's verdict
is the usage feeder's journal, so most of these tests are about reading that journal
correctly; the rest are the properties that must hold whatever the vendor CLIs do (no
secret on a screen, account 1 untouched by account 2, a terminal required).

The contract is asserted against the OTHER side where there is one: the feeder's real
GAUGE_SPECS and its real print format, the weight feeder's source - never against the
tool's own constants alone.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ai_cli_integration as ti          # noqa: E402  (feeder, REPO)
import test_quiet_pending_states as tq        # noqa: E402  (a real feeder run)

REPO = ti.REPO
feeder = ti.feeder
TOOL = REPO / "stack" / "ai-cli" / "gauge_login.py"
_spec = importlib.util.spec_from_file_location("gauge_login_under_test", str(TOOL))
gl = importlib.util.module_from_spec(_spec)
sys.modules["gauge_login_under_test"] = gl
_spec.loader.exec_module(gl)

NOW = 1_800_000_000
DAY = 86400
CANARY_ACCESS = "sk-ant-oat01-CANARY-ACCESS-0123456789"
CANARY_REFRESH = "sk-ant-ort01-CANARY-REFRESH-0123456789"
CANARY_KEY = "oc-key-CANARY-0123456789abcdef"
CANARY_CODE = "PASTED-CODE-CANARY-777#state"
ALL_CANARIES = (CANARY_ACCESS, CANARY_REFRESH, CANARY_KEY, CANARY_CODE)


# ---------------------------------------------------------------- fixtures

class Sandbox(object):
    def __init__(self, tmp_path, env_lines, verify_seconds="0"):
        self.tmp = tmp_path
        self.home = tmp_path / "home" / "alice"
        self.home.mkdir(parents=True)
        self.codex_home = tmp_path / "codex-home"
        self.env_file = tmp_path / "stack.env"
        self.env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        self.feeder_log = tmp_path / "feeder.log"
        self.weight_log = tmp_path / "weight.log"
        self.gemini_log = tmp_path / "gemini.log"
        for f in (self.feeder_log, self.weight_log, self.gemini_log):
            f.write_text("", encoding="utf-8")
        self.environ = {
            "GAUGE_LOGIN_ENV_FILE": str(self.env_file), "GAUGE_LOGIN_ACCOUNT": "alice",
            "GAUGE_LOGIN_HOME": self.home.as_posix(), "GAUGE_LOGIN_RUNAS": "",
            "GAUGE_LOGIN_NOW": str(NOW), "GAUGE_LOGIN_FEEDER_LOG": str(self.feeder_log),
            "GAUGE_LOGIN_WEIGHT_LOG": str(self.weight_log), "GAUGE_LOGIN_GEMINI_LOG": str(self.gemini_log),
            "GAUGE_LOGIN_CODEX_HOME": self.codex_home.as_posix(),
            "GAUGE_LOGIN_SYSTEMCTL": "no-such-systemctl-for-the-tests",
            "GAUGE_LOGIN_VERIFY_SECONDS": verify_seconds, "GAUGE_LOGIN_POLL_SECONDS": "0",
            "GAUGE_LOGIN_ASSUME_TTY": "1", "GAUGE_LOGIN_PYTHON": sys.executable,
            "GAUGE_LOGIN_MINT": "mint-stub", "PATH": os.environ.get("PATH", ""),
        }

    def ctx(self, **extra):
        env = dict(self.environ)
        env.update(extra)
        return gl.Ctx(environ=env)

    def write(self, rel, text):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def claude_cred(self, rel=".claude/.credentials.json", deadline=NOW + 20 * DAY, blank=False):
        oauth = {"accessToken": "" if blank else CANARY_ACCESS, "refreshToken": "" if blank else CANARY_REFRESH,
                 "expiresAt": 0 if blank else (NOW + 3600) * 1000, "scopes": ["user:inference"]}
        if deadline is not None:
            oauth["refreshTokenExpiresAt"] = int(deadline * 1000)       # epoch MILLISECONDS
        return self.write(rel, json.dumps({"claudeAiOauth": oauth}))

    def usage_journal(self, *runs):
        """Each run: dict(gauges=[(id, state[, 'POST FAILED'])], age=s, complete=bool, id=str)."""
        rows = []
        for n, run in enumerate(runs):
            iid = run.get("id", "run%02d" % n)
            ts = NOW - run.get("age", 60)
            lines = ["ai-usage: %-32s %-12s %s" % (g[0], g[1], g[2] if len(g) > 2 else "posted")
                     for g in run.get("gauges", [])]
            lines += run.get("extra", [])
            if run.get("complete", True):
                lines.append("ai-usage: cycle complete gauges=%d failures=0" % len(run.get("gauges", [])))
            for k, text in enumerate(lines):
                rows.append(json.dumps({"MESSAGE": text, "_SYSTEMD_INVOCATION_ID": iid,
                                        "__REALTIME_TIMESTAMP": str(int((ts - len(lines) + k) * 1e6))}))
        self.feeder_log.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def weight_journal(self, *runs):
        rows = []
        for n, run in enumerate(runs):
            ts = NOW - run.get("age", 60)
            lines = ["weight: %-10s %-12s %s" % (g[0], g[1], g[2] if len(g) > 2 else "posted")
                     for g in run["gauges"]] + run.get("extra", [])
            for k, text in enumerate(lines):
                rows.append(json.dumps({"MESSAGE": text, "_SYSTEMD_INVOCATION_ID": "w%02d" % n,
                                        "__REALTIME_TIMESTAMP": str(int((ts - len(lines) + k) * 1e6))}))
        self.weight_log.write_text("\n".join(rows) + "\n", encoding="utf-8")


USAGE = ["AI_USAGE_ENABLED=true", "AI_USAGE_SOURCES=claude,claude2,codex,gemini,opencode"]


def live(*keys):
    return [(k, "live") for k in keys]


def auth_failed(source):
    """The line the feeder's own auth_line() prints for a failed authentication."""
    entry = {"auth_since": 100, "auth_last": 160, "auth_attempts": 2}
    return "ai-usage: " + feeder.auth_line(source, entry)


def row_of(ctx, name):
    return gl.source_row(ctx, name)


# ---------------------------------------------------------------- the four+ states

def test_live_when_a_gauge_of_the_source_is_live(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    sb.claude_cred()
    sb.usage_journal({"gauges": live("ai-usage-claude-weekly")})
    row = row_of(sb.ctx(), "claude")
    assert row["state"] == "live" and row["flag"] == ""


def test_dead_when_no_gauge_is_live_AND_the_feeder_printed_an_authentication_failure(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    sb.claude_cred()
    sb.usage_journal({"gauges": [("ai-usage-claude-session", "UNAVAILABLE"),
                                 ("ai-usage-claude-weekly", "UNAVAILABLE")],
                      "extra": [auth_failed("claude")]})
    assert row_of(sb.ctx(), "claude")["state"] == "DEAD"


def test_unavailable_without_an_auth_line_is_not_a_dead_login(tmp_path):
    """A throttle, a network failure or a bad payload print UNAVAILABLE exactly like a dead
    login; only the feeder's authentication line tells them apart."""
    sb = Sandbox(tmp_path, USAGE)
    sb.claude_cred()
    throttle = "ai-usage: claude: HTTP 429 from the usage endpoint"
    backoff = "ai-usage: " + feeder.backoff_line("claude", {"backoff": 1, "next_due": 460}, 400, 60)
    for extra in ([], [throttle, backoff], ["ai-usage: claude: network error: timed out"]):
        sb.usage_journal({"gauges": [("ai-usage-claude-session", "UNAVAILABLE"),
                                     ("ai-usage-claude-weekly", "UNAVAILABLE")], "extra": extra})
        row = row_of(sb.ctx(), "claude")
        assert row["state"] == "unavailable", extra
        assert "not a sign-in failure" in row["detail"]
    # ...and another source's auth failure does not convict this one.
    sb.usage_journal({"gauges": [("ai-usage-claude-weekly", "UNAVAILABLE")],
                      "extra": [auth_failed("claude2")]})
    assert row_of(sb.ctx(), "claude")["state"] == "unavailable"


@pytest.mark.parametrize("source", ["claude", "claude2", "codex", "gemini", "opencode"])
def test_every_usage_sources_authentication_line_is_recognised(source):
    run = gl.parse_invocations(json.dumps({"MESSAGE": auth_failed(source), "_SYSTEMD_INVOCATION_ID": "x",
                                           "__REALTIME_TIMESTAMP": "1"}), gl.USAGE_LINE, gl.CYCLE_END)
    assert run[0]["auth"] == {source}


def test_the_no_usable_key_and_real_feeder_failure_lines_are_recognised(tmp_path, monkeypatch, capsys):
    cred = tmp_path / "blank.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": ""}}), encoding="utf-8")
    rc, out, err = tq._main(tmp_path, monkeypatch, capsys, "claude",
                            AI_USAGE_CLAUDE_CREDENTIALS=str(cred))
    lines = [l for l in (out + err).splitlines() if l.startswith("ai-usage: ")]
    assert any("carries no usable key" in l for l in lines), lines
    rows = [json.dumps({"MESSAGE": l, "_SYSTEMD_INVOCATION_ID": "real", "__REALTIME_TIMESTAMP": str(i + 1)})
            for i, l in enumerate(lines)]
    run = gl.parse_invocations(chr(10).join(rows), gl.USAGE_LINE, gl.CYCLE_END)[0]
    assert run["auth"] == {"claude"}, lines
    assert gl.auth_source("ai-usage: claude: credential /x/.credentials.json carries no usable key") == "claude"
    # A codex token_expired line alone is not the marker; the auth_line that follows it is.
    assert gl.auth_source("ai-usage: codex: JSON-RPC error -32603: token_expired") is None


def test_owed_when_there_is_no_usable_credential(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    ctx = sb.ctx()
    for name in ("claude", "claude2", "codex", "gemini", "opencode"):
        assert row_of(ctx, name)["state"] == "owed", name
    sb.write(".claude/.credentials.json", "")                 # an empty file is not a sign-in
    assert row_of(sb.ctx(), "claude")["state"] == "owed"


def test_off_when_not_configured(tmp_path):
    sb = Sandbox(tmp_path, ["AI_USAGE_ENABLED=true", "AI_USAGE_SOURCES=codex,claude,opencode"])
    ctx = sb.ctx()
    assert row_of(ctx, "claude2")["state"] == "off"
    assert row_of(ctx, "gemini")["state"] == "off"
    assert row_of(ctx, "google-health")["state"] == "off"      # WEIGHT_ENABLED unset
    assert row_of(ctx, "myair")["state"] == "off"
    off_entirely = Sandbox(tmp_path / "b", ["AI_USAGE_ENABLED=false", "AI_USAGE_SOURCES=claude"])
    assert row_of(off_entirely.ctx(), "claude")["state"] == "off"
    on = Sandbox(tmp_path / "c", ["WEIGHT_ENABLED=true", "SLEEP_ENABLED=true"])
    assert row_of(on.ctx(), "google-health")["state"] == "owed"
    assert row_of(on.ctx(), "myair")["state"] == "manual"


def test_unknown_when_the_credential_is_present_but_nothing_read_it_recently(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    sb.claude_cred()
    assert row_of(sb.ctx(), "claude")["state"] == "unknown"             # no journal at all
    sb.usage_journal({"gauges": live("ai-usage-claude-weekly"), "age": 3000})
    assert row_of(sb.ctx(), "claude")["state"] == "unknown"             # past the 20 minute cap


def test_a_blanked_claude_credential_is_not_signed_in_even_though_the_file_is_not_empty(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    path = sb.claude_cred(blank=True)
    assert path.stat().st_size > 0
    sb.usage_journal({"gauges": live("ai-usage-claude-weekly")})       # stale-looking evidence must not rescue it
    row = row_of(sb.ctx(), "claude")
    assert row["state"] == "DEAD" and "blanked" in row["detail"]


def test_session_gauge_unavailable_but_weekly_live_reads_live(tmp_path):
    """The vendor has sent five_hour: null since 2026-10-31: the session gauge is
    UNAVAILABLE every cycle on a working login."""
    sb = Sandbox(tmp_path, USAGE)
    sb.claude_cred()
    sb.usage_journal({"gauges": [("ai-usage-claude-session", "UNAVAILABLE"),
                                 ("ai-usage-claude-weekly", "live"),
                                 ("ai-usage-claude-weekly-fable", "UNAVAILABLE")]})
    assert row_of(sb.ctx(), "claude")["state"] == "live"
    sb.usage_journal({"gauges": [("ai-usage-claude-session", "UNAVAILABLE"),
                                 ("ai-usage-claude-weekly", "UNAVAILABLE"),
                                 ("ai-usage-claude-weekly-fable", "live")]})
    assert row_of(sb.ctx(), "claude")["state"] == "live"                # optional gauges count


def test_claude_one_and_two_do_not_borrow_each_others_gauges(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    sb.claude_cred()
    sb.claude_cred(".claude-2/.credentials.json")
    sb.usage_journal({"gauges": live("ai-usage-claude2-weekly") + [("ai-usage-claude-weekly", "UNAVAILABLE")]})
    ctx = sb.ctx()
    assert row_of(ctx, "claude2")["state"] == "live"
    assert row_of(ctx, "claude")["state"] == "unavailable"


def test_live_but_the_post_failed_is_its_own_state(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    sb.claude_cred()
    sb.usage_journal({"gauges": [("ai-usage-claude-weekly", "live", "POST FAILED")]})
    assert row_of(sb.ctx(), "claude")["state"] == "post-failing"


# ---------------------------------------------------------------- the deadline

@pytest.mark.parametrize("seconds_left,flag", [
    (3 * DAY, "DUE SOON"),            # the boundary is inclusive
    (3 * DAY - 1, "DUE SOON"),
    (3 * DAY + 1, ""),
    (20 * DAY, ""),
    (60, "DUE SOON"),
])
def test_deadline_maths_reads_epoch_milliseconds_and_flags_at_three_days(tmp_path, seconds_left, flag):
    sb = Sandbox(tmp_path, USAGE)
    sb.claude_cred(deadline=NOW + seconds_left)
    sb.usage_journal({"gauges": live("ai-usage-claude-weekly")})
    row = row_of(sb.ctx(), "claude")
    assert row["flag"] == flag
    assert row["state"] == "live"
    assert row["days_left"] == pytest.approx(seconds_left / DAY, abs=0.01)   # not off by 1000x
    assert row["deadline_iso"] == __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ",
                                                              __import__("time").gmtime(NOW + seconds_left))


def test_a_passed_deadline_is_dead_even_while_the_old_access_token_still_works(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    sb.claude_cred(deadline=NOW - DAY)
    sb.usage_journal({"gauges": live("ai-usage-claude-weekly")})
    row = row_of(sb.ctx(), "claude")
    assert row["state"] == "DEAD" and "expired" in row["detail"]
    assert row["days_left"] < 0


def test_only_the_deadline_field_ever_leaves_the_credential_file(tmp_path):
    sb = Sandbox(tmp_path, USAGE + ["WEIGHT_ENABLED=true"])
    sb.claude_cred()
    sb.write(".local/share/opencode/auth.json", json.dumps({"opencode": {"type": "api", "key": CANARY_KEY}}))
    sb.usage_journal({"gauges": live("ai-usage-claude-weekly")})
    ctx = sb.ctx()
    text = gl.render_table(gl.build_status(ctx)) + json.dumps(gl.build_status(ctx))
    for canary in ALL_CANARIES:
        assert canary not in text


# ---------------------------------------------------------------- which run decides

def test_a_run_that_did_not_read_the_source_defers_to_the_one_that_did(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    sb.codex_home.mkdir(parents=True)
    (sb.codex_home / "auth.json").write_text('{"x": 1}', encoding="utf-8")
    sb.usage_journal({"gauges": live("ai-usage-codex"), "age": 300, "id": "A"},
                     {"gauges": live("ai-usage-claude-weekly"), "age": 60, "id": "B"})   # B never read codex
    assert row_of(sb.ctx(), "codex")["state"] == "live"
    # ...both ways: the earlier run that read it says it is dead.
    sb.usage_journal({"gauges": [("ai-usage-codex", "UNAVAILABLE")], "extra": [auth_failed("codex")],
                      "age": 300, "id": "A"},
                     {"gauges": live("ai-usage-claude-weekly"), "age": 60, "id": "B"})
    assert row_of(sb.ctx(), "codex")["state"] == "DEAD"
    # ...and not past the age limit.
    sb.usage_journal({"gauges": live("ai-usage-codex"), "age": 1500, "id": "A"},
                     {"gauges": live("ai-usage-claude-weekly"), "age": 60, "id": "B"})
    assert row_of(sb.ctx(), "codex")["state"] == "unknown"


def test_a_run_that_never_printed_cycle_complete_is_not_evidence(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    sb.claude_cred()
    sb.usage_journal({"gauges": live("ai-usage-claude-weekly"), "complete": False})
    assert row_of(sb.ctx(), "claude")["state"] == "unknown"


def test_a_newest_run_that_died_part_way_does_not_borrow_an_older_good_one(tmp_path):
    """A feeder that now crashes partway must not keep showing live for 20 minutes."""
    sb = Sandbox(tmp_path, USAGE)
    sb.claude_cred()
    good = live("ai-usage-claude-weekly")
    sb.usage_journal({"gauges": good, "age": 300, "id": "A"},
                     {"gauges": [], "complete": False, "extra": ["Traceback (most recent call last):"],
                      "age": 60, "id": "B"})
    assert row_of(sb.ctx(), "claude")["state"] == "unknown"
    # A run that died before printing anything (only the manager's lines) counts too.
    sb.usage_journal({"gauges": good, "age": 300, "id": "A"})
    sb.feeder_log.write_text(sb.feeder_log.read_text(encoding="utf-8") + json.dumps(
        {"MESSAGE": "homehub-ai-usage.service: Failed with result 'exit-code'.", "INVOCATION_ID": "C",
         "__REALTIME_TIMESTAMP": str(int((NOW - 10) * 1e6))}) + chr(10), encoding="utf-8")
    assert row_of(sb.ctx(), "claude")["state"] == "unknown"


def test_the_run_going_right_now_leaves_the_previous_completed_run_as_evidence(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    sb.claude_cred()
    good = live("ai-usage-claude-weekly")
    sb.usage_journal({"gauges": good, "age": 300, "id": "A"},
                     {"gauges": [], "extra": ["ai-usage: starting"], "complete": False, "age": 5, "id": "C"})
    now_running = {"GAUGE_LOGIN_SERVICE_STATE": "activating", "GAUGE_LOGIN_INVOCATION_ID": "C"}
    assert row_of(sb.ctx(**now_running), "claude")["state"] == "live"
    # The service is running but it is a DIFFERENT invocation: C died, so unknown.
    assert row_of(sb.ctx(GAUGE_LOGIN_SERVICE_STATE="active", GAUGE_LOGIN_INVOCATION_ID="Z"),
                  "claude")["state"] == "unknown"
    # Not running at all: C died.
    assert row_of(sb.ctx(GAUGE_LOGIN_SERVICE_STATE="inactive", GAUGE_LOGIN_INVOCATION_ID="C"),
                  "claude")["state"] == "unknown"
    # A good, B died, C running and silent: the previous run is B, which did not complete.
    sb.usage_journal({"gauges": good, "age": 900, "id": "A"},
                     {"gauges": [], "extra": ["ai-usage: starting"], "complete": False, "age": 300, "id": "B"},
                     {"gauges": [], "extra": ["ai-usage: starting"], "complete": False, "age": 5, "id": "C"})
    assert row_of(sb.ctx(**now_running), "claude")["state"] == "unknown"


def test_the_newest_run_that_read_the_source_wins_over_older_ones(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    sb.claude_cred()
    sb.usage_journal({"gauges": live("ai-usage-claude-weekly"), "age": 400, "id": "A"},
                     {"gauges": [("ai-usage-claude-weekly", "UNAVAILABLE")], "extra": [auth_failed("claude")],
                      "age": 60, "id": "B"})
    assert row_of(sb.ctx(), "claude")["state"] == "DEAD"


# ---------------------------------------------------------------- the other side of the contract

def test_the_tools_gauge_lists_are_the_feeders_gauge_specs():
    by_source = {}
    for spec in feeder.GAUGE_SPECS:
        by_source.setdefault(spec.source, []).append(spec.key)
    for source, keys in by_source.items():
        assert sorted(gl.GAUGES[source]) == sorted(keys), source
        for key in keys:
            owners = [n for n in gl.ORDER if gl.KIND[n] == "usage" and gl.key_belongs(n, key)]
            assert owners == [source], (key, owners)          # claude vs claude2 never overlap


def test_the_tool_reads_the_real_feeders_own_output(tmp_path, monkeypatch, capsys):
    rc, out, err = tq._main(tmp_path, monkeypatch, capsys, "codex",
                            AI_USAGE_CODEX_BIN=str(tmp_path / "nope"))
    lines = [l for l in out.splitlines() if l.startswith("ai-usage: ")]
    rows = [json.dumps({"MESSAGE": l, "_SYSTEMD_INVOCATION_ID": "real",
                        "__REALTIME_TIMESTAMP": str(int((NOW - 30 + i) * 1e6))}) for i, l in enumerate(lines)]
    runs = gl.parse_invocations("\n".join(rows), gl.USAGE_LINE, gl.CYCLE_END)
    assert len(runs) == 1 and runs[0]["complete"] is not None
    assert runs[0]["gauges"].get("ai-usage-codex") == ("UNAVAILABLE", True), (lines, runs)


def test_the_line_formats_are_the_ones_the_feeders_print():
    source = (REPO / "stack" / "ai-usage" / "ai_usage_feeder.py").read_text(encoding="utf-8")
    assert '"ai-usage: %-32s %-12s %s"' in source and '"live" if fresh else "UNAVAILABLE"' in source
    assert '"posted" if ok else "POST FAILED"' in source
    weight = (REPO / "stack" / "weight" / "weight_feeder.py").read_text(encoding="utf-8")
    assert '"weight: %-10s %-12s %s"' in weight and '"heart: %-10s %-12s %s"' in weight
    assert '"%s: sign-in failure %d of %d%s"' in weight
    assert 'RATIO_GAUGE_ID = "weight-waist"' in weight and 'HEART_GAUGE_ID = "heart"' in weight


def test_google_health_reads_the_weight_services_journal(tmp_path):
    sb = Sandbox(tmp_path, ["WEIGHT_ENABLED=true"])
    token = tmp_path / "token.json"
    token.write_text('{"refresh_token": "%s"}' % CANARY_REFRESH, encoding="utf-8")
    env = sb.env_file.read_text(encoding="utf-8") + "WEIGHT_TOKEN_FILE=%s\n" % token.as_posix()
    sb.env_file.write_text(env, encoding="utf-8")
    sb.weight_journal({"gauges": [("weight", "live"), ("weight-waist", "UNAVAILABLE"), ("heart", "UNAVAILABLE")]})
    assert row_of(sb.ctx(), "google-health")["state"] == "live"
    sb.weight_journal({"gauges": [("weight", "UNAVAILABLE")]})
    assert row_of(sb.ctx(), "google-health")["state"] == "unavailable"
    sb.weight_journal({"gauges": [("weight", "UNAVAILABLE")],
                       "extra": ["weight: sign-in failure 2 of 2 - confirmed, the gauge is grey until the login is renewed"]})
    assert row_of(sb.ctx(), "google-health")["state"] == "DEAD"
    sb.weight_journal({"gauges": [("weight", "live")], "age": 7200})
    assert row_of(sb.ctx(), "google-health")["state"] == "unknown"


# ---------------------------------------------------------------- login: refusals and guards

def run_cli(sb, *args):
    env = dict(os.environ)
    env.update(sb.environ)
    env.pop("GAUGE_LOGIN_ASSUME_TTY")
    return subprocess.run([sys.executable, "-I", str(TOOL)] + list(args), env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)


def test_login_refuses_without_a_terminal_and_runs_nothing(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    done = run_cli(sb, "login", "claude")
    assert done.returncode == 2
    assert "needs a terminal" in done.stderr
    assert not (sb.home / ".claude").exists()
    assert run_cli(sb, "menu").returncode == 2


def test_status_works_without_a_terminal_and_always_exits_zero(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    done = run_cli(sb, "status")
    assert done.returncode == 0 and "claude" in done.stdout
    data = json.loads(run_cli(sb, "status", "--json").stdout)
    assert [r["source"] for r in data] == gl.ORDER
    assert json.loads(run_cli(sb, "--json").stdout)[0]["source"] == "claude"


def test_manual_sources_print_their_step_and_need_no_terminal(tmp_path):
    sb = Sandbox(tmp_path, USAGE + ["DOMAIN=home.test", "TRACKER_SUBDOMAIN=tr"])
    done = run_cli(sb, "login", "tracker-sheet")
    assert done.returncode == 0 and "https://tr.home.test/api/drive/connect" in done.stdout
    assert "drive-setup-url" in done.stdout                       # the trap is named
    done = run_cli(sb, "login", "myair")
    assert done.returncode == 0 and "PrepDeploySecrets.ps1" in done.stdout
    no_domain = Sandbox(tmp_path / "x", USAGE)
    assert "tracker.<your domain>" in run_cli(no_domain, "login", "tracker-sheet").stdout


def test_the_cli_logins_run_as_the_account_from_its_home_with_the_units_environment(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    ctx = sb.ctx(GAUGE_LOGIN_RUNAS="runuser -u alice --")
    argv = gl.cli_login_argv(ctx, "claude")
    assert argv[:4] == ["runuser", "-u", "alice", "--"]
    assert "HOME=" + sb.home.as_posix() in argv and "PATH=/usr/local/bin:/usr/bin:/bin" in argv
    assert argv[-1] == "cd ~ && exec /usr/local/bin/claude auth login --claudeai"
    assert not any(a.startswith("CLAUDE_CONFIG_DIR") for a in argv)
    assert "CLAUDE_CONFIG_DIR=%s/.claude-2" % sb.home.as_posix() in gl.cli_login_argv(ctx, "claude2")
    codex = gl.cli_login_argv(ctx, "codex")
    assert "CODEX_HOME=" + sb.codex_home.as_posix() in codex and codex[-1].endswith("codex login --device-auth")
    assert "NO_BROWSER=true" in gl.cli_login_argv(ctx, "gemini")


def test_claude2_signin_that_changes_account_1_is_reported_loudly(tmp_path, monkeypatch, capsys):
    sb = Sandbox(tmp_path, USAGE)
    sb.claude_cred()
    sb.write(".claude.json", '{"oauthAccount": "account-1"}')
    seen = {}

    def clobbering_signin(argv, timeout=0):
        seen["argv"] = argv
        sb.claude_cred()                                    # the "bad" outcome: account 1's file rewritten
        (sb.home / ".claude.json").write_text('{"oauthAccount": "account-2"}', encoding="utf-8")
        return 0

    monkeypatch.setattr(gl, "spawn_interactive", clobbering_signin)
    rc = gl.login_one(sb.ctx(), "claude2")
    out = capsys.readouterr().out
    assert "ACCOUNT 1" in out and "CHANGED" in out and rc == 1
    assert "CLAUDE_CONFIG_DIR=%s/.claude-2" % sb.home.as_posix() in seen["argv"]


def test_claude2_signin_that_leaves_account_1_alone_raises_no_alarm(tmp_path, monkeypatch, capsys):
    sb = Sandbox(tmp_path, USAGE)
    sb.claude_cred()
    sb.write(".claude.json", '{"oauthAccount": "account-1"}')

    def good_signin(argv, timeout=0):
        sb.claude_cred(".claude-2/.credentials.json")
        return 1                                            # the CLI's exit code is not the verdict

    monkeypatch.setattr(gl, "spawn_interactive", good_signin)
    sb.usage_journal({"gauges": live("ai-usage-claude2-weekly"), "age": -5})
    rc = gl.login_one(sb.ctx(), "claude2")
    out = capsys.readouterr().out
    assert "CHANGED" not in out and "RESULT: PASS" in out and rc == 0
    assert "ai-usage-claude2-weekly live posted" in out
    assert "new deadline" in out


# ---------------------------------------------------------------- verification

def test_verification_takes_the_first_run_after_the_signin_not_an_older_one(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    # A live run BEFORE the sign-in must not pass it; the run after it decides.
    sb.usage_journal({"gauges": live("ai-usage-claude-weekly"), "age": 200, "id": "before"})
    verdict, lines = gl.wait_for_verdict(sb.ctx(), "claude", NOW - 100)
    assert verdict == "fail" and "no completed feeder run" in lines[0]
    sb.usage_journal({"gauges": live("ai-usage-claude-weekly"), "age": 200, "id": "before"},
                     {"gauges": [("ai-usage-claude-session", "UNAVAILABLE"), ("ai-usage-claude-weekly", "live")],
                      "age": 10, "id": "after"})
    verdict, lines = gl.wait_for_verdict(sb.ctx(), "claude", NOW - 100)
    assert verdict == "pass"
    assert "ai-usage-claude-weekly live posted" in lines and "ai-usage-claude-session UNAVAILABLE posted" in lines


def test_verification_fails_on_an_authentication_failure_and_shows_what_it_saw(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    sb.usage_journal({"gauges": [("ai-usage-codex", "UNAVAILABLE")], "extra": [auth_failed("codex")],
                      "age": 10})
    verdict, lines = gl.wait_for_verdict(sb.ctx(), "codex", NOW - 100)
    assert verdict == "fail" and "ai-usage-codex UNAVAILABLE posted" in lines
    assert "authentication failure" in lines[0]


def test_unavailable_without_an_auth_line_after_signin_is_pending_not_fail(tmp_path, monkeypatch, capsys):
    sb = Sandbox(tmp_path, USAGE)
    sb.usage_journal({"gauges": [("ai-usage-codex", "UNAVAILABLE")],
                      "extra": ["ai-usage: codex: HTTP 429 too many requests"], "age": 10})
    verdict, lines = gl.wait_for_verdict(sb.ctx(), "codex", NOW - 100)
    assert verdict == "pending"
    assert "vendor did not answer yet (not an auth failure)" in lines[0]
    assert "re-run status in a few minutes" in lines[0]
    # ...and login reports it as not yet verified, not a bare FAIL (still non-zero).
    sb.codex_home.mkdir(parents=True)
    monkeypatch.setattr(gl, "spawn_interactive", lambda argv, timeout=0: 0)
    sb.usage_journal({"gauges": [("ai-usage-codex", "UNAVAILABLE")], "age": -5})
    rc = gl.login_one(sb.ctx(), "codex")
    out = capsys.readouterr().out
    assert rc == 1 and "NOT YET VERIFIED" in out and "RESULT: FAIL" not in out


def test_a_pending_first_run_does_not_stop_a_later_live_run_passing(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    sb.usage_journal({"gauges": [("ai-usage-codex", "UNAVAILABLE")], "age": 20, "id": "first"},
                     {"gauges": live("ai-usage-codex"), "age": 5, "id": "second"})
    verdict, lines = gl.wait_for_verdict(sb.ctx(), "codex", NOW - 100)
    assert verdict == "pass"


def test_google_health_verification_reads_the_weight_journal(tmp_path):
    sb = Sandbox(tmp_path, ["WEIGHT_ENABLED=true"])
    sb.weight_journal({"gauges": [("weight", "live"), ("weight-waist", "live")], "age": 10})
    verdict, lines = gl.wait_for_verdict(sb.ctx(), "google-health", NOW - 100)
    assert verdict == "pass" and "weight live posted" in lines


# ---------------------------------------------------------------- opencode

def key_files(sb):
    return sb.home / ".local" / "share" / "opencode" / "auth.json"


def test_opencode_writer_preserves_other_providers_and_the_entrys_shape(tmp_path, monkeypatch, capsys):
    sb = Sandbox(tmp_path, USAGE)
    path = sb.write(".local/share/opencode/auth.json", json.dumps(
        {"anthropic": {"type": "oauth", "access": "keep-me"},
         "opencode": {"type": "api", "key": "old-key", "extra": 7}}))
    calls = []
    real_run = subprocess.run

    def spy(argv, *a, **kw):
        calls.append((list(argv), dict(kw.get("env") or {})))
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(gl.subprocess, "run", spy)
    monkeypatch.setattr(gl, "_read_secret", lambda prompt: CANARY_KEY)
    rc = gl.login_one(sb.ctx(), "opencode")
    out = capsys.readouterr()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["opencode"] == {"type": "api", "key": CANARY_KEY, "extra": 7}
    assert data["anthropic"] == {"type": "oauth", "access": "keep-me"}
    assert "opencode-go" not in data                      # the existing spelling is kept, not duplicated
    # The key is in no output, no argv and no child environment.
    assert CANARY_KEY not in out.out + out.err
    for argv, env in calls:
        assert not any(CANARY_KEY in a for a in argv) and CANARY_KEY not in "".join(env.values())
    assert [p.name for p in path.parent.iterdir()] == ["auth.json"]       # no temp file left behind
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600
    assert "Permissions = Read only" in out.out
    assert rc in (0, 1)       # the verdict here is the feeder's; the journal is empty


def test_opencode_writer_creates_the_file_in_the_spelling_the_cli_writes(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    ctx = sb.ctx()
    gl.write_opencode_key(ctx, CANARY_KEY)
    path = key_files(sb)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"opencode": {"type": "api", "key": CANARY_KEY}}
    # ...and the feeder's own reader accepts exactly that.
    assert feeder._opencode_key(data) == CANARY_KEY
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600
        assert (path.parent.stat().st_mode & 0o777) == 0o700


def test_opencode_writer_refuses_a_corrupt_file_without_touching_it(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    path = sb.write(".local/share/opencode/auth.json", "not json {")
    with pytest.raises(gl.LoginError) as exc:
        gl.write_opencode_key(sb.ctx(), CANARY_KEY)
    assert path.read_text(encoding="utf-8") == "not json {"
    assert CANARY_KEY not in str(exc.value)


def test_opencode_writer_refuses_a_key_with_whitespace(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    with pytest.raises(gl.LoginError):
        gl.write_opencode_key(sb.ctx(), "two words")
    assert not key_files(sb).exists()


def test_a_pasted_code_never_reaches_any_output(tmp_path, monkeypatch, capsys):
    """The CLI sign-ins inherit the terminal; this tool never sees the code at all."""
    sb = Sandbox(tmp_path, USAGE)
    monkeypatch.setattr(gl, "spawn_interactive", lambda argv, timeout=0: 1)
    gl.login_one(sb.ctx(), "claude")
    out = capsys.readouterr()
    for canary in ALL_CANARIES:
        assert canary not in out.out + out.err


# ---------------------------------------------------------------- menu and Ctrl+C

def _menu_box(tmp_path):
    sb = Sandbox(tmp_path, USAGE)
    sb.claude_cred(deadline=NOW + DAY)                         # live, DUE SOON
    sb.usage_journal({"gauges": live("ai-usage-claude-weekly") + [("ai-usage-codex", "UNAVAILABLE")],
                      "extra": [auth_failed("codex")]})
    sb.codex_home.mkdir(parents=True)
    (sb.codex_home / "auth.json").write_text("{}", encoding="utf-8")       # codex: DEAD
    return sb                                                  # claude2, gemini, opencode: owed


def test_the_menu_asks_only_about_dead_and_due_soon_and_lists_the_rest(tmp_path, monkeypatch, capsys):
    sb = _menu_box(tmp_path)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "")
    assert gl.cmd_menu(sb.ctx()) == 0
    out = capsys.readouterr().out
    prompts = [p for p in asked if "now (" in p]
    assert [p.split()[2] for p in prompts] == ["claude", "codex"], asked      # DUE SOON, DEAD
    assert not any("claude2" in p or "gemini" in p or "opencode" in p for p in prompts)
    assert "Not asked about (owed or unavailable): claude2, gemini, opencode" in out
    assert "login <source>" in out


def test_the_menu_signs_in_only_on_an_explicit_yes(tmp_path, monkeypatch, capsys):
    sb = _menu_box(tmp_path)
    ran = []
    monkeypatch.setattr(gl, "cmd_login", lambda ctx, source: ran.append(source) or 0)
    answers = iter(["n", "y", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    gl.cmd_menu(sb.ctx())
    assert ran == ["codex"]


def test_ctrl_c_during_a_signin_aborts_cleanly(tmp_path, monkeypatch, capsys):
    sb = Sandbox(tmp_path, USAGE)

    def interrupted(argv, timeout=0):
        raise KeyboardInterrupt

    monkeypatch.setattr(gl, "spawn_interactive", interrupted)
    rc = gl.cmd_login(sb.ctx(), "claude")
    err = capsys.readouterr().err
    assert rc == 130 and "aborted - nothing verified" in err and "Traceback" not in err


def test_ctrl_c_at_the_menu_aborts_cleanly_and_ends_the_menu(tmp_path, monkeypatch, capsys):
    sb = _menu_box(tmp_path)

    def interrupted(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupted)
    assert gl.cmd_menu(sb.ctx()) == 130
    assert "aborted - nothing verified" in capsys.readouterr().err
    # Ctrl+C inside a sign-in the menu started stops the menu too.
    answers = iter(["y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(gl, "cmd_login", lambda ctx, source: 130)
    assert gl.cmd_menu(sb.ctx()) == 130
