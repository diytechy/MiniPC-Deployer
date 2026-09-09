"""TC-005 — the AI-usage feeder's four acceptance properties, asserted.

Every test here targets a criterion the build plan wrote down, and each one is
written so that BREAKING THE GUARD TURNS IT RED (the mutation runs are recorded
in docs/status.md). The three that carried the block:

  A. the feeder NEVER writes a vendor credential file  — and this is asserted
     against the real filesystem, by running a whole cycle inside a throwaway
     HOME and proving that the only file that appeared is the state file.
  B. a failing source posts "unavailable" and NEVER a green gauge — asserted
     for every failure class separately (transport, timeout, 401, garbage body,
     well-formed-but-empty body, out-of-range percent, non-finite percent),
     against NagLight's own freshness rule rather than against our intent.
  C. one explicit identity, refusing to guess — the refusal is an error, not a
     default.
  D. off by default.

Plus the wire contract itself (IF-013 / NagLight IF-012), because the 2026-09
tightening turned an omitted `value`, a lone `min`, and a `direction` without a
`window` into 400s, and a feeder that 400s posts nothing at all.

Verifies: TC-005 (SR-021, LLR-005)
"""

import importlib.util
import json
import math
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "stack" / "ai-usage" / "ai_usage_feeder.py"

_spec = importlib.util.spec_from_file_location("ai_usage_feeder", MODULE_PATH)
feeder = importlib.util.module_from_spec(_spec)
sys.modules["ai_usage_feeder"] = feeder
_spec.loader.exec_module(feeder)


# ── Fixtures taken VERBATIM from the three real calls of 2026-09-09 ──────────
# These are not invented shapes. Each is the body the vendor actually returned
# during the verification the build plan required before any parser was
# written; keeping them literal is what makes a vendor change show up as a red
# test instead of a silently wrong gauge.

CODEX_RESULT = json.loads("""
{"rateLimits":{"limitId":"codex","limitName":null,
  "primary":{"usedPercent":34,"windowDurationMins":10080,"resetsAt":1789435411},
  "secondary":null,
  "credits":{"hasCredits":false,"unlimited":false,"balance":"0"},
  "individualLimit":null,"planType":"prolite","rateLimitReachedType":null},
 "rateLimitsByLimitId":{"codex":{"limitId":"codex","primary":{"usedPercent":34,
   "windowDurationMins":10080,"resetsAt":1789435411}},
  "codex_bengalfox":{"limitId":"codex_bengalfox","limitName":"GPT-5.3-Codex-Spark",
   "primary":{"usedPercent":0,"windowDurationMins":300,"resetsAt":1788959668}}},
 "rateLimitResetCredits":{"availableCount":0,"credits":[]}}
""")

CLAUDE_BODY = json.loads("""
{"five_hour":{"utilization":79.0,"resets_at":"2026-09-09T09:10:00.468815+00:00",
  "limit_dollars":null,"locked_reason":null},
 "seven_day":{"utilization":87.0,"resets_at":"2026-09-12T09:00:00.468835+00:00",
  "limit_dollars":null,"locked_reason":null},
 "seven_day_opus":null,
 "limits":[{"kind":"session","group":"session","percent":79,"severity":"warning",
   "resets_at":"2026-09-09T09:10:00.468815+00:00","is_active":false},
  {"kind":"weekly_all","group":"weekly","percent":87,"severity":"warning",
   "resets_at":"2026-09-12T09:00:00.468835+00:00","is_active":true}],
 "spend":{"percent":0,"severity":"normal","enabled":false},
 "member_dashboard_available":false}
""")

OPENCODE_BODY = json.loads("""
{"usage":{"rolling":{"status":"ok","percent":0,"resetsAt":"2026-09-09T13:15:25.458Z"},
 "weekly":{"status":"ok","percent":58,"resetsAt":"2026-09-14T00:00:00.458Z"},
 "monthly":{"status":"ok","percent":89,"resetsAt":"2026-09-19T00:11:19.458Z"}}}
""")

NOW = 1789000000


def base_env(tmp_path, **extra):
    env = {
        "AI_USAGE_ENABLED": "true",
        "AI_USAGE_USER": "108000000000000000001",
        "AI_USAGE_FEED_URL": "http://127.0.0.1:8787/api/feed",
        "AI_USAGE_STATE_FILE": str(tmp_path / "usage-state.json"),
        "_identity": "108000000000000000001",
        "_feed_url": "http://127.0.0.1:8787/api/feed",
    }
    env.update(extra)
    return env


class Recorder(object):
    """A poster that records bodies instead of sending them."""

    def __init__(self, ok=True):
        self.bodies = []
        self.ok = ok

    def __call__(self, body, url, env, timeout):
        self.bodies.append(body)
        return self.ok, "HTTP 200" if self.ok else "HTTP 500"


# ── The parsers, against the observed bodies (the "real call" gate) ─────────

def test_parse_codex_reads_the_observed_shape_sr021():
    out = feeder.parse_codex(CODEX_RESULT)
    assert set(out) == {"ai-usage-codex"}
    reading = out["ai-usage-codex"]
    assert reading.percent == 34.0
    assert reading.window_seconds == 10080 * 60      # the observed weekly window
    assert reading.window_end == 1789435411


def test_parse_codex_ignores_the_per_limit_id_buckets_sr021():
    """Gauges keyed off `rateLimitsByLimitId` would appear and vanish as the
    vendor renames a model, so only the compatible view is read."""
    out = feeder.parse_codex(CODEX_RESULT)
    assert "codex_bengalfox" not in json.dumps(sorted(out))
    assert len(out) == 1


def test_parse_claude_reads_both_windows_and_discards_severity_sr021():
    out = feeder.parse_claude(CLAUDE_BODY)
    assert out["ai-usage-claude-session"].percent == 79.0
    assert out["ai-usage-claude-weekly"].percent == 87.0
    assert out["ai-usage-claude-session"].window_seconds == 5 * 3600
    assert out["ai-usage-claude-weekly"].window_seconds == 7 * 24 * 3600
    # The vendor's own severity words are in the fixture and must not survive.
    spec = feeder.GaugeSpec("ai-usage-claude-weekly", "Claude weekly", "x", "claude")
    body = feeder.build_gauge(spec, out["ai-usage-claude-weekly"].percent, NOW,
                              out["ai-usage-claude-weekly"].window_end,
                              out["ai-usage-claude-weekly"].window_seconds)
    blob = json.dumps(body)
    for forbidden in ("severity", "warning", "css", "colour", "color"):
        assert forbidden not in blob


def test_parse_claude_one_null_bucket_is_not_a_source_failure_sr021():
    body = dict(CLAUDE_BODY, five_hour=None)
    out = feeder.parse_claude(body)
    assert set(out) == {"ai-usage-claude-weekly"}


def test_parse_opencode_reads_named_windows_and_skips_rolling_sr021():
    out = feeder.parse_opencode(OPENCODE_BODY)
    assert set(out) == {"ai-usage-opencode-weekly", "ai-usage-opencode-monthly"}
    assert out["ai-usage-opencode-weekly"].percent == 58.0
    assert out["ai-usage-opencode-monthly"].percent == 89.0


def test_parse_opencode_skips_a_bucket_the_vendor_flagged_sr021():
    body = json.loads(json.dumps(OPENCODE_BODY))
    body["usage"]["weekly"]["status"] = "degraded"
    out = feeder.parse_opencode(body)
    assert "ai-usage-opencode-weekly" not in out


@pytest.mark.parametrize("payload", [
    None, [], {}, {"rateLimits": None}, {"rateLimits": {"primary": None}},
    {"rateLimits": {"primary": {"usedPercent": None}}},
    {"rateLimits": {"primary": {"usedPercent": "34"}}},
    {"rateLimits": {"primary": {"usedPercent": 101}}},
    {"rateLimits": {"primary": {"usedPercent": -1}}},
    {"rateLimits": {"primary": {"usedPercent": True}}},
    {"rateLimits": {"primary": {"usedPercent": float("nan")}}},
    {"rateLimits": {"primary": {"usedPercent": float("inf")}}},
])
def test_codex_garbage_is_a_source_failure_never_a_reading_sr021(payload):
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_codex(payload)


def test_percent_rejects_bool_because_python_says_true_is_one_sr021():
    with pytest.raises(feeder.SourceFailure):
        feeder.check_percent(True, "test")


# ── Criterion B: a failing source posts "unavailable", never a green gauge ──

FAILURE_CLASSES = {
    "transport": ConnectionRefusedError("no route"),
    "timeout": TimeoutError("timed out"),
    "unauthorised": feeder.SourceFailure("HTTP 401"),
    "garbage": feeder.SourceFailure("body is not JSON"),
    "empty": feeder.SourceFailure("no usable bucket"),
    "reader_bug": KeyError("a bug in our own parser"),
}


@pytest.mark.parametrize("name", sorted(FAILURE_CLASSES))
def test_every_failure_class_posts_a_stale_gauge_and_never_a_fresh_one_sr021(tmp_path, name):
    """The acceptance criterion, asserted against NagLight's OWN freshness rule.

    A source that errors, times out, returns garbage or returns 401 must never
    produce a gauge NagLight would render live — because a live gauge with a
    plausible number is indistinguishable from a healthy subscription.
    """
    exc = FAILURE_CLASSES[name]

    def broken(env):
        raise exc

    env = base_env(tmp_path, AI_USAGE_SOURCES="codex")
    rec = Recorder()
    posted, failures = feeder.run_cycle(env, now=NOW, readers={"codex": broken},
                                        poster=rec)
    assert failures, "a failing source must be reported, not swallowed"
    assert [k for k, fresh, ok in posted if fresh] == [], "nothing may be fresh"
    assert rec.bodies, "the gauge must still be POSTED — 'unavailable', not absent"
    for body in rec.bodies:
        assert not feeder.is_fresh(body, NOW)


def test_a_failed_source_reposts_the_last_true_reading_at_its_own_stamp_sr021(tmp_path):
    """Not a fabricated number and not a fresh one: the last value that WAS
    true, stamped when it was true, so it goes stale on its own horizon."""
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex")
    ok_reader = {"codex": lambda e: feeder.parse_codex(CODEX_RESULT)}
    rec = Recorder()
    feeder.run_cycle(env, now=NOW, readers=ok_reader, poster=rec)
    assert rec.bodies[0]["value"] == 34.0
    assert rec.bodies[0]["observed_at"] == NOW
    assert feeder.is_fresh(rec.bodies[0], NOW)

    def broken(e):
        raise feeder.SourceFailure("HTTP 401")

    later = NOW + 25 * 3600          # past the 24h weekly horizon
    rec2 = Recorder()
    feeder.run_cycle(env, now=later, readers={"codex": broken}, poster=rec2)
    assert rec2.bodies[0]["value"] == 34.0, "the value must be the one that was true"
    assert rec2.bodies[0]["observed_at"] == NOW, "stamped when it was true, not now"
    assert not feeder.is_fresh(rec2.bodies[0], later)


def test_first_ever_cycle_failure_posts_a_gauge_with_no_observed_at_sr021(tmp_path):
    """With no previous reading there is nothing true to repost, so the gauge
    carries no timestamp at all — stale on arrival by NagLight's rule, which is
    how the panel gets to say 'unavailable' instead of showing nothing."""
    env = base_env(tmp_path, AI_USAGE_SOURCES="opencode")

    def broken(e):
        raise feeder.SourceFailure("HTTP 401")

    rec = Recorder()
    feeder.run_cycle(env, now=NOW, readers={"opencode": broken}, poster=rec)
    assert len(rec.bodies) == 2          # weekly + monthly, both present
    for body in rec.bodies:
        assert "observed_at" not in body
        assert not feeder.is_fresh(body, NOW)


@pytest.mark.parametrize("order", ["codex,claude", "claude,codex"])
def test_one_source_failing_does_not_blank_the_others_sr021(tmp_path, order):
    """BOTH ORDERS, and the second one is why this test exists.

    The first cut read the failing source FIRST, so a mutant that discarded
    every reading collected so far on a failure still passed — there was
    nothing collected yet to discard. A guard whose test only exercises the
    ordering that cannot expose it is a vacuous assertion; the mutation run of
    2026-09-09 (M15) found exactly that here, and this parametrisation is the
    fix.
    """
    env = base_env(tmp_path, AI_USAGE_SOURCES=order)

    def broken(e):
        raise feeder.SourceFailure("HTTP 401")

    rec = Recorder()
    posted, failures = feeder.run_cycle(
        env, now=NOW,
        readers={"codex": broken, "claude": lambda e: feeder.parse_claude(CLAUDE_BODY)},
        poster=rec)
    fresh = {k for k, f, _ in posted if f}
    assert fresh == {"ai-usage-claude-session", "ai-usage-claude-weekly"}
    assert any("codex" in f for f in failures)
    for body in rec.bodies:
        if body["id"].startswith("ai-usage-claude"):
            assert feeder.is_fresh(body, NOW)
        else:
            assert not feeder.is_fresh(body, NOW)


def test_is_fresh_refuses_a_future_stamp_sr021():
    body = {"observed_at": NOW + 600, "window": {"kind": "weekly"}}
    assert not feeder.is_fresh(body, NOW)


# ── The wire contract (IF-013 / NagLight IF-012) ────────────────────────────

def test_every_posted_gauge_matches_the_tightened_wire_shape_sr021(tmp_path):
    env = base_env(tmp_path)
    rec = Recorder()
    feeder.run_cycle(env, now=NOW, poster=rec, readers={
        "codex": lambda e: feeder.parse_codex(CODEX_RESULT),
        "claude": lambda e: feeder.parse_claude(CLAUDE_BODY),
        "opencode": lambda e: feeder.parse_opencode(OPENCODE_BODY),
    })
    assert len(rec.bodies) == 5
    for body in rec.bodies:
        assert body["kind"] == "gauge"
        # value and target must be PRESENT — omitted is a 400, and used to be
        # stored as a fabricated zero.
        assert "value" in body and "target" in body
        assert math.isfinite(body["value"]) and body["target"] == 0
        # min/max go together, and `%` has no agreed width to infer from.
        assert body["min"] == 0 and body["max"] == 100
        assert math.isfinite(body["max"] - body["min"])
        # direction is required WITH a window and refused WITHOUT one.
        assert ("window" in body) == ("direction" in body)
        if "window" in body:
            assert body["direction"] == "up"     # usage counts UP toward a cap
            assert body["window"]["start"] < body["window"]["end"]
            assert body["window"]["kind"] in ("daily", "weekly", "monthly", "static")
        for field in ("id", "label", "icon", "unit"):
            assert 0 < len(body[field]) <= 120
        assert "css" not in body and "severity" not in body and "colour" not in body


def test_direction_is_refused_without_a_window_sr021():
    spec = feeder.GAUGE_SPECS[0]
    body = feeder.build_gauge(spec, 10.0, NOW, None, None)
    assert "window" not in body and "direction" not in body


def test_window_kind_buckets_the_observed_durations_sr021():
    assert feeder.window_kind_for(5 * 3600) == "daily"          # Claude session
    assert feeder.window_kind_for(10080 * 60) == "weekly"       # codex primary
    assert feeder.window_kind_for(30 * 24 * 3600) == "monthly"  # opencode
    assert feeder.window_kind_for(400 * 24 * 3600) == "static"
    for bad in (0, -1, float("nan"), float("inf"), None, "week", True):
        with pytest.raises(feeder.SourceFailure):
            feeder.window_kind_for(bad)


def test_the_weekly_horizon_is_why_the_cadence_must_beat_24h_sr021():
    """A weekly gauge goes stale after 24h. If the shipped timer interval were
    ever loosened past that the panel would flap into 'unavailable' on a
    healthy box, so the relationship is asserted rather than commented."""
    assert feeder.STALE_HORIZON_SECONDS["weekly"] == 24 * 3600
    unit = (REPO / "stack" / "ai-usage" / "homehub-ai-usage.timer").read_text(encoding="utf-8")
    line = [l for l in unit.splitlines() if l.startswith("OnUnitActiveSec=")][0]
    minutes = int(line.split("=", 1)[1].rstrip("min"))
    assert minutes * 60 < feeder.STALE_HORIZON_SECONDS["weekly"] / 4


# ── Criterion A: never writes a vendor credential file ──────────────────────

@pytest.mark.parametrize("name", sorted(feeder.CREDENTIAL_BASENAMES))
def test_open_for_write_refuses_a_vendor_credential_file_sr021(tmp_path, name):
    target = tmp_path / name
    target.write_text('{"real":"token"}', encoding="utf-8")
    with pytest.raises(PermissionError):
        feeder.open_for_write(str(target), str(target))
    assert target.read_text(encoding="utf-8") == '{"real":"token"}'


def test_open_for_write_refuses_everything_outside_the_one_state_path_sr021(tmp_path):
    state = tmp_path / "usage-state.json"
    for other in ("elsewhere.json", "../escape.json", "sub/deep.json"):
        with pytest.raises(PermissionError):
            feeder.open_for_write(str(tmp_path / other), str(state))
    # the state file itself and its atomic-rename sibling are the ONLY two.
    feeder.open_for_write(str(state), str(state)).close()
    feeder.open_for_write(str(state) + ".tmp", str(state)).close()


def test_a_whole_cycle_writes_exactly_one_file_and_no_credential_sr021(tmp_path):
    """The property asserted against the real filesystem, not against intent.

    A fake HOME is populated with all three vendor credential files; a full
    cycle runs; then every file under that HOME is checked for an unchanged
    mtime+content, and the whole tree is checked for any file that was created.
    """
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir(parents=True)
    (home / ".local" / "share" / "opencode").mkdir(parents=True)
    creds = {
        home / ".claude" / ".credentials.json": '{"claudeAiOauth":{"accessToken":"CLAUDE-SECRET"}}',
        home / ".codex" / "auth.json": '{"tokens":{"access_token":"CODEX-SECRET"}}',
        home / ".local" / "share" / "opencode" / "auth.json":
            '{"opencode-go":{"type":"api","key":"OPENCODE-SECRET"}}',
    }
    for path, text in creds.items():
        path.write_text(text, encoding="utf-8")
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}

    state = tmp_path / "state" / "usage-state.json"
    state.parent.mkdir()
    env = base_env(tmp_path, AI_USAGE_STATE_FILE=str(state))
    env["AI_USAGE_CLAUDE_CREDENTIALS"] = str(home / ".claude" / ".credentials.json")
    env["AI_USAGE_OPENCODE_AUTH"] = str(home / ".local" / "share" / "opencode" / "auth.json")
    rec = Recorder()
    feeder.run_cycle(env, now=NOW, poster=rec, readers={
        "codex": lambda e: feeder.parse_codex(CODEX_RESULT),
        "claude": lambda e: feeder.parse_claude(CLAUDE_BODY),
        "opencode": lambda e: feeder.parse_opencode(OPENCODE_BODY),
    })

    after = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    assert after == before, "the feeder touched something under HOME"
    assert sorted(p.name for p in state.parent.iterdir()) == ["usage-state.json"]

    # And the one file it DID write must not have absorbed a token.
    written = state.read_text(encoding="utf-8")
    for secret in ("CLAUDE-SECRET", "CODEX-SECRET", "OPENCODE-SECRET", "Bearer"):
        assert secret not in written
    stored = json.loads(written)
    for entry in stored.values():
        assert set(entry) <= {"value", "observed_at", "window_end", "window_seconds"}


def test_read_secret_file_never_opens_a_credential_for_writing_sr021():
    """A structural check: the module has exactly one writing door, and the
    credential reader is not behind it."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    body = source.split("def read_secret_file", 1)[1].split("\ndef ", 1)[0]
    assert 'open(os.path.expanduser(path), encoding="utf-8")' in body
    assert '"w"' not in body and "'w'" not in body


# ── Criterion C: one explicit identity, refusing to guess ───────────────────

@pytest.mark.parametrize("value", [None, "", "   "])
def test_identity_refuses_to_guess_and_errors_out_sr021(value):
    env = {} if value is None else {"AI_USAGE_USER": value}
    with pytest.raises(SystemExit) as caught:
        feeder.resolve_identity(env)
    assert "AI_USAGE_USER" in str(caught.value)


def test_identity_is_carried_on_every_post_sr021(tmp_path):
    captured = {}

    def poster(body, url, env, timeout):
        captured["identity"] = env["_identity"]
        return True, "HTTP 200"

    env = base_env(tmp_path, AI_USAGE_SOURCES="codex")
    feeder.run_cycle(env, now=NOW, poster=poster,
                     readers={"codex": lambda e: feeder.parse_codex(CODEX_RESULT)})
    assert captured["identity"] == "108000000000000000001"


def test_the_destination_must_stay_on_this_box_sr021():
    ok = {"AI_USAGE_FEED_URL": "http://127.0.0.1:8787/api/feed"}
    assert feeder.resolve_feed_url(ok).endswith("/api/feed")
    for bad in ("", "https://tracker.example.com/api/feed",
                "http://192.168.1.50:8787/api/feed",
                "http://8.8.8.8/api/feed", "ftp://127.0.0.1/api/feed",
                "http://localhost:8787/api/feed"):
        with pytest.raises(SystemExit):
            feeder.resolve_feed_url({"AI_USAGE_FEED_URL": bad})


def test_a_lan_shaped_bridge_range_is_not_a_bridge_sr021():
    """The lesson ai_cli_service.resolve_bind learned: 172.16.0.0/12 contains
    real household LANs, so membership of the range proves nothing. Only an
    address a bridge is ACTUALLY carrying counts."""
    import ipaddress
    bridges = [ipaddress.ip_network("172.17.0.0/16")]
    assert feeder.is_local_destination("172.17.0.2", bridges)
    assert not feeder.is_local_destination("172.20.0.5", bridges)
    assert not feeder.is_local_destination("172.17.0.2", [])
    assert feeder.is_local_destination("127.0.0.1", [])
    assert not feeder.is_local_destination("tracker", [])


# ── Criterion D: off by default ─────────────────────────────────────────────

@pytest.mark.parametrize("value", [None, "", "false", "False", "TRUE", "yes", "1", " true"])
def test_off_by_default_and_for_every_near_miss_sr021(value):
    env = {} if value is None else {"AI_USAGE_ENABLED": value}
    assert feeder.resolve_enabled(env) is (value == " true")


def test_the_shipped_env_example_ships_the_feeder_off_sr021():
    text = (REPO / "stack" / ".env.example").read_text(encoding="utf-8")
    assert "\nAI_USAGE_ENABLED=false\n" in text
    assert "\nAI_USAGE_USER=\n" in text, "the identity must ship BLANK, not guessed"


def test_main_does_nothing_at_all_when_the_knob_is_off_sr021(monkeypatch, capsys):
    for key in list(os.environ):
        if key.startswith("AI_USAGE_"):
            monkeypatch.delenv(key, raising=False)
    assert feeder.main([]) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_unknown_source_names_are_refused_not_ignored_sr021():
    with pytest.raises(SystemExit):
        feeder.enabled_sources({"AI_USAGE_SOURCES": "codex,gemini"})
    assert feeder.enabled_sources({}) == ["codex", "claude", "opencode"]


def test_gemini_is_absent_from_the_feeder_sr021():
    """Deferred as E11. A source nobody could verify with a real call must not
    appear as a parser — that is the failure mode the gate exists to prevent."""
    source = MODULE_PATH.read_text(encoding="utf-8").lower()
    assert "def parse_gemini" not in source
    assert "generativelanguage" not in source


def test_every_http_source_sends_a_named_user_agent_sr021(monkeypatch, tmp_path):
    """Found by RUNNING it, not by reading anything.

    The first cut of read_opencode set no User-Agent, so urllib's default
    `Python-urllib/3.x` went out and opencode.ai answered 403 — where the
    verification call of 2026-09-09, which sent a named agent, got 200. The
    feeder behaved correctly (two 'unavailable' gauges and a named failure,
    never a green one), which is how the defect surfaced at all. This test
    stops it coming back, and covers the Claude endpoint's required
    `claude-code/<version>` agent in the same breath.
    """
    seen = {}

    def fake_http_json(url, headers, timeout):
        seen[url] = headers
        return OPENCODE_BODY if "opencode" in url else CLAUDE_BODY

    monkeypatch.setattr(feeder, "http_json", fake_http_json)
    creds = tmp_path / ".credentials.json"
    creds.write_text('{"claudeAiOauth":{"accessToken":"T"}}', encoding="utf-8")
    auth = tmp_path / "auth.json"
    auth.write_text('{"opencode-go":{"type":"api","key":"K"}}', encoding="utf-8")

    feeder.read_opencode({"AI_USAGE_OPENCODE_AUTH": str(auth)})
    feeder.read_claude({"AI_USAGE_CLAUDE_CREDENTIALS": str(creds),
                        "AI_USAGE_CLAUDE_VERSION": "2.1.201"})

    for url, headers in seen.items():
        agent = headers.get("User-Agent", "")
        assert agent and "Python-urllib" not in agent, url
    claude_headers = [h for u, h in seen.items() if "anthropic" in u][0]
    assert claude_headers["User-Agent"] == "claude-code/2.1.201"
    assert claude_headers["anthropic-beta"] == "oauth-2025-04-20"
