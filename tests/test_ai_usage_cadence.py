"""TC-1330..TC-1342 -- the AI-usage gauges every minute (W11, 2026-10-22).

The Owner asked for the usage gauges to update every minute. The timer now
fires every minute, and each source -- each Claude ACCOUNT included -- carries
its own interval, backoff and authentication history in the state file, because
the feeder is a one-shot unit and nothing in memory survives a cycle.

Everything here drives the module's public seams (`run_cycle` with injected
readers and an injected clock, `http_json` with the opener replaced, the shipped
unit files, the real refresher gate run as a process) and asserts against the
OTHER side of each contract: the wire bodies NagLight receives, the state file
on disk re-read by a fresh load, the journal lines, and process trees on the
real kernel.

Verifies: TC-1330..TC-1342 (SR-095, SR-096, SR-068; LLR-1054, LLR-1190..LLR-1197)
"""

import calendar
import email.utils
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AI_DIR = REPO / "stack" / "ai-usage"


def load_feeder(name="ai_usage_feeder_cadence"):
    """A FRESH module object each call -- a stand-in for a new process."""
    spec = importlib.util.spec_from_file_location(name, AI_DIR / "ai_usage_feeder.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


feeder = load_feeder()

_gspec = importlib.util.spec_from_file_location("claude_refresh_due", AI_DIR / "claude_refresh_due.py")
gate = importlib.util.module_from_spec(_gspec)
sys.modules["claude_refresh_due"] = gate
_gspec.loader.exec_module(gate)

# 2026-09-09 08:53 UTC: every fixture window below is open (as in the sibling files).
NOW = 1788944000

CODEX_RESULT = {"rateLimits": {"limitId": "codex", "primary": {
    "usedPercent": 34, "windowDurationMins": 10080, "resetsAt": 1789435411}}}
OPENCODE_BODY = {"usage": {
    "weekly": {"status": "ok", "percent": 58, "resetsAt": "2026-09-14T00:00:00.458Z"},
    "monthly": {"status": "ok", "percent": 89, "resetsAt": "2026-09-19T00:11:19.458Z"}}}
CLAUDE_BODY = {"five_hour": {"utilization": 79.0, "resets_at": "2026-09-09T09:10:00.468815+00:00"},
               "seven_day": {"utilization": 87.0, "resets_at": "2026-09-12T09:00:00.468835+00:00"}}


def base_env(tmp_path, **extra):
    env = {
        "AI_USAGE_ENABLED": "true",
        "AI_USAGE_USER": "108000000000000000001",
        "AI_USAGE_STATE_FILE": str(tmp_path / "usage-state.json"),
        "_identity": "108000000000000000001",
        "_feed_url": "http://127.0.0.1:8787/api/feed",
        "STATE_DIRECTORY": str(tmp_path),
    }
    env.update(extra)
    return env


class Recorder(object):
    def __init__(self):
        self.bodies = []

    def __call__(self, body, url, env, timeout):
        self.bodies.append(body)
        return True, "HTTP 200"

    def ids(self):
        return [b["id"] for b in self.bodies]

    def by_id(self):
        return {b["id"]: b for b in self.bodies}


class Counting(object):
    """A reader that counts its calls and answers with whatever `script` says."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, env):
        self.calls.append(env["_now"])
        return self.answer(env)


def codex_ok(e):
    return feeder.parse_codex(CODEX_RESULT, e["_now"])


def opencode_ok(e):
    return feeder.parse_opencode(OPENCODE_BODY, e["_now"])


def cycle(env, readers, now, module=None):
    rec = Recorder()
    env.pop("_notes", None)
    posted, failures = (module or feeder).run_cycle(env, now=now, readers=readers, poster=rec)
    return rec, failures, list(env.get("_notes", []))


def state_of(env):
    with open(env["AI_USAGE_STATE_FILE"], encoding="utf-8") as handle:
        return json.load(handle)


def schedule_of(env, source):
    return state_of(env)[feeder.SCHEDULE_KEY][source]


def throttle(retry_after=None, status=429):
    def reader(e):
        raise feeder.Throttled("vendor: HTTP %d" % status, status=status, retry_after=retry_after)
    return reader


# ═══════════════════════════════════════════════════════════════════════════
# TC-1330 — per-source cadence, and the knob that sets it (LLR-1190)
# ═══════════════════════════════════════════════════════════════════════════

def test_each_source_is_read_on_its_own_cadence_tc1330(tmp_path):
    """The HTTP sources every minute, Codex every second cycle by default."""
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex,opencode")
    codex, opencode = Counting(codex_ok), Counting(opencode_ok)
    for step in range(0, 600, 60):
        cycle(env, {"codex": codex, "opencode": opencode}, NOW + step)
    assert opencode.calls == [NOW + s for s in range(0, 600, 60)]
    assert codex.calls == [NOW + s for s in range(0, 600, 120)]


def test_a_skipped_source_posts_nothing_and_says_why_tc1330(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex,opencode")
    cycle(env, {"codex": codex_ok, "opencode": opencode_ok}, NOW)
    rec, failures, notes = cycle(env, {"codex": codex_ok, "opencode": opencode_ok}, NOW + 60)
    assert rec.ids() == ["ai-usage-opencode-weekly", "ai-usage-opencode-monthly"]
    assert failures == []
    assert notes == ["codex: skipped - not due for 45s (reads every 120s)"]


def test_the_interval_knob_is_per_source_tc1330(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex,opencode",
                   AI_USAGE_CODEX_INTERVAL="60", AI_USAGE_OPENCODE_INTERVAL="300")
    codex, opencode = Counting(codex_ok), Counting(opencode_ok)
    for step in range(0, 600, 60):
        cycle(env, {"codex": codex, "opencode": opencode}, NOW + step)
    assert len(codex.calls) == 10
    assert opencode.calls == [NOW, NOW + 300]


@pytest.mark.parametrize("value", ["59", "601", "abc", "-60", "60.5", "1e3"])
def test_a_malformed_interval_is_refused_by_name_tc1330(tmp_path, value):
    env = base_env(tmp_path, AI_USAGE_SOURCES="claude", AI_USAGE_CLAUDE_INTERVAL=value)
    with pytest.raises(SystemExit) as caught:
        feeder.run_cycle(env, now=NOW, readers={"claude": codex_ok}, poster=Recorder())
    assert "AI_USAGE_CLAUDE_INTERVAL" in str(caught.value)


def test_main_check_refuses_a_malformed_interval_before_any_read_tc1330(monkeypatch, tmp_path, capsys):
    for key, value in base_env(tmp_path, AI_USAGE_FEED_URL="http://127.0.0.1:8787/api/feed",
                               AI_USAGE_SOURCES="codex", AI_USAGE_CODEX_INTERVAL="30").items():
        if not key.startswith("_"):
            monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit) as caught:
        feeder.main(["--check"])
    assert "AI_USAGE_CODEX_INTERVAL" in str(caught.value)


def test_blank_is_the_default_not_a_refusal_tc1330():
    got = feeder.source_intervals({"AI_USAGE_CODEX_INTERVAL": " ", "AI_USAGE_CLAUDE_INTERVAL": ""},
                                  ["codex", "claude", "claude2", "gemini", "opencode"])
    assert got == {"codex": 120, "claude": 60, "claude2": 60, "gemini": 60, "opencode": 60}


def test_the_timers_jitter_never_costs_a_healthy_source_a_cycle_tc1330(tmp_path):
    """Cycles land 59-75 s apart. A 60-s source read at T is due at T+59; the
    slack is what makes that true, and it must not reach T+44."""
    env = base_env(tmp_path, AI_USAGE_SOURCES="opencode")
    opencode = Counting(opencode_ok)
    cycle(env, {"opencode": opencode}, NOW)
    cycle(env, {"opencode": opencode}, NOW + 44)
    assert opencode.calls == [NOW]
    cycle(env, {"opencode": opencode}, NOW + 59)
    assert opencode.calls == [NOW, NOW + 59]


# ═══════════════════════════════════════════════════════════════════════════
# TC-1331 — a skip is its own outcome (LLR-1191)
# ═══════════════════════════════════════════════════════════════════════════

def test_a_skip_leaves_the_schedule_entry_exactly_as_it_was_tc1331(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex")

    def dead(e):
        raise feeder.AuthFailure("codex: HTTP 401", status=401)

    cycle(env, {"codex": dead}, NOW)
    before = schedule_of(env, "codex")
    _rec, failures, notes = cycle(env, {"codex": dead}, NOW + 60)       # not due: skipped
    assert schedule_of(env, "codex") == before
    assert failures == [] and notes and "skipped" in notes[0]


def test_skipped_cycles_between_attempts_add_no_attempts_and_no_span_tc1331(tmp_path):
    """W11 Done-when: skipped cycles between attempts. Codex at its default
    120 s: ten minutes of cycles is five attempts, not ten, and confirmation
    still needs the span between ATTEMPTS."""
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex")
    dead = Counting(lambda e: (_ for _ in ()).throw(feeder.AuthFailure("codex: 401", status=401)))
    for step in range(0, 600, 60):
        rec, failures, _n = cycle(env, {"codex": dead}, NOW + step)
    assert len(dead.calls) == 5
    entry = schedule_of(env, "codex")
    assert (entry["auth_since"], entry["auth_attempts"], entry["last_attempt"]) == (NOW, 5, NOW + 480)
    rec, failures, _n = cycle(env, {"codex": dead}, NOW + 600)
    assert any("attempt 6 over 600s - CONFIRMED" in line for line in failures)


def test_a_pending_source_keeps_its_cadence_and_its_auth_history_tc1331(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="claude")

    def dead(e):
        raise feeder.AuthFailure("claude: HTTP 401", status=401)

    def owed(e):
        raise feeder.PendingState("claude: login owed")

    cycle(env, {"claude": dead}, NOW)
    cycle(env, {"claude": owed}, NOW + 60)
    entry = schedule_of(env, "claude")
    assert entry["last_outcome"] == "pending"
    assert (entry["auth_since"], entry["auth_attempts"]) == (NOW, 1)
    assert entry["next_due"] == NOW + 120


# ═══════════════════════════════════════════════════════════════════════════
# TC-1332 — Retry-After, both forms, and longer than the ceiling (LLR-1192)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("value,expected", [
    ("120", 120), ("0", 0), (" 45 ", 45),
    ("3600", 3600),                                   # longer than the 10-min ceiling: as given
    (str(10 * 86400), feeder.RETRY_AFTER_MAX_SECONDS),  # a year-long park is clamped
    (email.utils.formatdate(NOW + 300, usegmt=True), 300),
    (email.utils.formatdate(NOW + 7200, usegmt=True), 7200),
    (email.utils.formatdate(NOW - 300, usegmt=True), 0),   # already past: come back now
    ("Wed, 09 Sep 2026 09:00:00 +0000", calendar.timegm((2026, 9, 9, 9, 0, 0)) - NOW),
    ("Wed, 09 Sep 2026 04:00:00 -0500", calendar.timegm((2026, 9, 9, 9, 0, 0)) - NOW),
    ("", None), (None, None), ("soon", None), ("-5", None), ("1.5", None),
])
def test_retry_after_seconds_and_http_date_tc1332(value, expected):
    assert feeder.parse_retry_after(value, NOW) == expected


def test_a_numeric_retry_after_is_honoured_to_the_second_tc1332(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="opencode")
    reader = Counting(throttle(retry_after=300))
    cycle(env, {"opencode": reader}, NOW)
    for step in (60, 120, 180, 240, 299):
        cycle(env, {"opencode": reader}, NOW + step)
    assert reader.calls == [NOW], "no slack against the vendor's own number"
    cycle(env, {"opencode": reader}, NOW + 300)
    assert reader.calls == [NOW, NOW + 300]


def test_an_http_date_retry_after_reaches_the_schedule_tc1332(monkeypatch, tmp_path):
    """End to end through http_json: the header as the vendor sends it."""
    env = base_env(tmp_path, AI_USAGE_SOURCES="opencode")
    auth = tmp_path / "auth.json"
    auth.write_text('{"opencode":{"type":"api","key":"K"}}', encoding="utf-8")
    env["AI_USAGE_OPENCODE_AUTH"] = str(auth)
    date = email.utils.formatdate(NOW + 900, usegmt=True)
    monkeypatch.setattr(feeder, "vendor_opener", lambda: _Opener(429, {"Retry-After": date}))
    monkeypatch.setattr(feeder.time, "time", lambda: float(NOW))     # the answer arrives at once
    _rec, failures, _n = cycle(env, {"opencode": feeder.read_opencode}, NOW)
    entry = schedule_of(env, "opencode")
    assert entry["hold"] is True and entry["next_due"] - NOW == 900
    assert any("backing off" in f and "Retry-After" in f for f in failures)


def test_a_retry_delay_longer_than_ten_minutes_is_honoured_as_given_tc1332(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="claude")
    reader = Counting(throttle(retry_after=3600))
    cycle(env, {"claude": reader}, NOW)
    for step in range(60, 3600, 60):
        _rec, _f, notes = cycle(env, {"claude": reader}, NOW + step)
    assert reader.calls == [NOW]
    assert re.match(r"claude: skipped - backing off, \d+s left \(level 1, Retry-After\)", notes[0])
    cycle(env, {"claude": reader}, NOW + 3600)
    assert reader.calls == [NOW, NOW + 3600]


@pytest.mark.parametrize("form", ["seconds", "http-date"])
def test_retry_after_is_anchored_at_the_answer_not_the_cycle_start_tc1332(monkeypatch, tmp_path, form):
    """Sol round 1: an earlier source took 30 s, then this one answered 429 with
    Retry-After 300. The vendor's deadline is receipt + 300, not cycle start +
    300 -- a skip-less cycle at start + 300 would be early."""
    env = base_env(tmp_path, AI_USAGE_SOURCES="opencode")
    auth = tmp_path / "auth.json"
    auth.write_text('{"opencode":{"type":"api","key":"K"}}', encoding="utf-8")
    env["AI_USAGE_OPENCODE_AUTH"] = str(auth)
    received = NOW + 30
    value = "300" if form == "seconds" else email.utils.formatdate(received + 300, usegmt=True)
    monkeypatch.setattr(feeder, "vendor_opener", lambda: _Opener(429, {"Retry-After": value}))
    monkeypatch.setattr(feeder.time, "time", lambda: float(received))
    cycle(env, {"opencode": feeder.read_opencode}, NOW)
    entry = schedule_of(env, "opencode")
    assert (entry["next_due"], entry["hold"]) == (NOW + 330, True)
    reader = Counting(opencode_ok)
    cycle(env, {"opencode": reader}, NOW + 329)
    assert reader.calls == []
    cycle(env, {"opencode": reader}, NOW + 330)
    assert reader.calls == [NOW + 330]


def test_a_retry_after_shorter_than_the_cadence_keeps_the_cadence_tc1332(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="claude")
    cycle(env, {"claude": throttle(retry_after=5)}, NOW)
    entry = schedule_of(env, "claude")
    assert (entry["next_due"], entry["hold"]) == (NOW + 60, False)


# ═══════════════════════════════════════════════════════════════════════════
# TC-1333 — exponential backoff to the ceiling, journalled, headers kept (LLR-1192)
# ═══════════════════════════════════════════════════════════════════════════

def test_no_retry_after_backs_off_exponentially_to_the_ceiling_tc1333(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="gemini")
    reader = Counting(throttle(status=503))
    t, delays = NOW, []
    for _ in range(6):
        _rec, failures, _n = cycle(env, {"gemini": reader}, t)
        entry = schedule_of(env, "gemini")
        delays.append(entry["next_due"] - t)
        assert any(f.startswith("gemini: backing off %ds (level %d" % (delays[-1], entry["backoff"]))
                   for f in failures)
        t = entry["next_due"]
    assert delays == [120, 240, 480, 600, 600, 600]
    assert len(reader.calls) == 6


def test_a_success_ends_the_backoff_tc1333(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="opencode")
    cycle(env, {"opencode": throttle(status=500)}, NOW)
    cycle(env, {"opencode": opencode_ok}, NOW + 120)
    entry = schedule_of(env, "opencode")
    assert (entry["backoff"], entry["next_due"], entry["last_outcome"]) == (0, NOW + 180, "ok")


def test_a_plain_failure_is_not_throttling_and_keeps_the_cadence_tc1333(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="opencode")

    def forbidden(e):
        raise feeder.SourceFailure("opencode: HTTP 403")

    cycle(env, {"opencode": forbidden}, NOW)
    entry = schedule_of(env, "opencode")
    assert (entry["backoff"], entry["next_due"], entry["last_outcome"]) == (0, NOW + 60, "failure")


def test_rate_limit_headers_are_recorded_on_success_and_on_429_tc1333(monkeypatch, tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="opencode")
    auth = tmp_path / "auth.json"
    auth.write_text('{"opencode":{"type":"api","key":"SECRET-KEY-VALUE"}}', encoding="utf-8")
    env["AI_USAGE_OPENCODE_AUTH"] = str(auth)
    ok_headers = {"X-RateLimit-Remaining": "97", "X-RateLimit-Limit": "100",
                  "Set-Cookie": "session=abc", "X-Request-Id": "r1"}
    monkeypatch.setattr(feeder, "vendor_opener", lambda: _Opener(200, ok_headers, OPENCODE_BODY))
    cycle(env, {"opencode": feeder.read_opencode}, NOW)
    assert schedule_of(env, "opencode")["ratelimit"] == {
        "x-ratelimit-remaining": "97", "x-ratelimit-limit": "100"}, "only rate-limit headers"
    monkeypatch.setattr(feeder, "vendor_opener", lambda: _Opener(
        429, {"Retry-After": "120", "X-RateLimit-Remaining": "0"}))
    _rec, failures, _n = cycle(env, {"opencode": feeder.read_opencode}, NOW + 60)
    assert schedule_of(env, "opencode")["ratelimit"] == {
        "retry-after": "120", "x-ratelimit-remaining": "0"}
    assert any("HTTP 429 (rate-limit headers: retry-after=120, x-ratelimit-remaining=0)" in f
               for f in failures)
    text = Path(env["AI_USAGE_STATE_FILE"]).read_text(encoding="utf-8")
    assert "SECRET-KEY-VALUE" not in text and "session=abc" not in text


def test_a_token_shaped_value_under_an_allowed_name_is_not_kept_tc1333():
    got = feeder.sanitize_rate_limit_headers([
        ("anthropic-ratelimit-requests-remaining", "41"),
        ("x-ratelimit-token", "Bearer " + "a" * 70),
        ("retry-after", "30\r\nInjected: yes"),
    ])
    assert got == {"anthropic-ratelimit-requests-remaining": "41"}


# ═══════════════════════════════════════════════════════════════════════════
# TC-1334 — a source in backoff keeps its last reading at its true stamp (LLR-1192)
# ═══════════════════════════════════════════════════════════════════════════

def test_backoff_keeps_the_last_reading_with_its_true_observed_at_tc1334(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="opencode")
    cycle(env, {"opencode": opencode_ok}, NOW)
    rec, _f, _n = cycle(env, {"opencode": throttle(retry_after=600)}, NOW + 60)
    for body in rec.bodies:
        assert body["observed_at"] == feeder.rfc3339_utc(NOW), "the attempt re-posts the truth"
    rec, _f, notes = cycle(env, {"opencode": throttle(retry_after=600)}, NOW + 120)
    assert rec.bodies == [], "a backing-off source is not re-posted at all"
    assert state_of(env)["ai-usage-opencode-weekly"]["observed_at"] == NOW


# ═══════════════════════════════════════════════════════════════════════════
# TC-1335 — what counts as throttling (LLR-1192)
# ═══════════════════════════════════════════════════════════════════════════

class _Response(object):
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = _Headers(headers)
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Headers(object):
    def __init__(self, pairs):
        self.pairs = dict(pairs)

    def items(self):
        return list(self.pairs.items())

    def get(self, name, default=None):
        for k, v in self.pairs.items():
            if k.lower() == name.lower():
                return v
        return default


class _Opener(object):
    def __init__(self, status, headers=None, body=None):
        self.status, self.headers, self.body = status, headers or {}, body or {}

    def open(self, request, timeout=None):
        if self.status != 200:
            raise urllib.error.HTTPError(request.full_url, self.status, "x",
                                         _Headers(self.headers), io.BytesIO(b"SECRET-BODY"))
        return _Response(self.status, self.headers, self.body)


@pytest.mark.parametrize("status,kind", [
    (429, "Throttled"), (500, "Throttled"), (502, "Throttled"), (503, "Throttled"), (599, "Throttled"),
    (401, "AuthFailure"), (403, "SourceFailure"), (404, "SourceFailure"), (400, "SourceFailure"),
])
def test_only_429_and_5xx_are_throttling_tc1335(monkeypatch, status, kind):
    monkeypatch.setattr(feeder, "vendor_opener", lambda: _Opener(status))
    with pytest.raises(feeder.SourceFailure) as caught:
        feeder.http_json("https://example.invalid/x", {}, 5)
    assert type(caught.value).__name__ == kind
    assert "SECRET-BODY" not in str(caught.value)


def test_a_throttled_answer_carries_its_retry_after_tc1335(monkeypatch):
    monkeypatch.setattr(feeder, "vendor_opener", lambda: _Opener(429, {"Retry-After": "90"}))
    with pytest.raises(feeder.Throttled) as caught:
        feeder.http_json("https://example.invalid/x", {}, 5)
    assert (caught.value.status, caught.value.retry_after) == (429, 90)


def test_outside_a_cycle_nothing_collects_the_headers_tc1335(monkeypatch):
    monkeypatch.setattr(feeder, "vendor_opener", lambda: _Opener(200, {"X-RateLimit-Remaining": "1"}, {}))
    assert feeder.http_json("https://example.invalid/x", {}, 5) == {}
    assert feeder._RATE_LIMIT_SINK.get() is None


@pytest.mark.parametrize("message,kind", [
    ("failed to fetch rate limits: 429 Too Many Requests", "Throttled"),
    ('upstream {"status": 429}', "Throttled"),
    ("rate limit window not available", "SourceFailure"),
    ("received 4290 bytes", "SourceFailure"),
])
def test_a_codex_429_frame_is_throttling_and_nothing_else_is_tc1335(message, kind):
    failure = feeder.codex_error_frame({"error": {"code": -32603, "message": message}})
    assert type(failure).__name__ == kind


def test_run_cycle_catches_throttled_before_sourcefailure_tc1335():
    import ast
    tree = ast.parse((AI_DIR / "ai_usage_feeder.py").read_text(encoding="utf-8"))
    run = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_cycle"][0]
    order = [ast.dump(h.type) for h in ast.walk(run) if isinstance(h, ast.ExceptHandler) and h.type is not None]
    first_throttle = next(i for i, d in enumerate(order) if "Throttled" in d)
    first_plain = next(i for i, d in enumerate(order) if "'SourceFailure'" in d)
    assert first_throttle < first_plain


# ═══════════════════════════════════════════════════════════════════════════
# TC-1336 — a process restart between cycles changes nothing (LLR-1190)
# ═══════════════════════════════════════════════════════════════════════════

def test_the_schedule_survives_a_process_restart_tc1336(tmp_path):
    """Each cycle runs in a FRESH module object, so nothing but the state file
    can carry the backoff, the cadence or the authentication history."""
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex,opencode")
    first = load_feeder("ai_usage_feeder_restart_1")
    codex = Counting(codex_ok)
    hold = Counting(lambda e: (_ for _ in ()).throw(first.Throttled("x", status=429, retry_after=900)))
    cycle(env, {"codex": codex, "opencode": hold}, NOW, module=first)
    second = load_feeder("ai_usage_feeder_restart_2")
    assert second is not first
    rec, _f, notes = cycle(env, {"codex": codex, "opencode": hold}, NOW + 60, module=second)
    assert codex.calls == [NOW] and hold.calls == [NOW]
    assert sorted(n.split(":")[0] for n in notes) == ["codex", "opencode"]
    third = load_feeder("ai_usage_feeder_restart_3")
    cycle(env, {"codex": codex, "opencode": hold}, NOW + 120, module=third)
    assert codex.calls == [NOW, NOW + 120] and hold.calls == [NOW]
    cycle(env, {"codex": codex, "opencode": hold}, NOW + 900, module=load_feeder("ai_usage_feeder_restart_4"))
    assert hold.calls == [NOW, NOW + 900]


# ═══════════════════════════════════════════════════════════════════════════
# TC-1337 — clock changes (LLR-1190)
# ═══════════════════════════════════════════════════════════════════════════

def test_a_clock_that_steps_back_reads_the_source_at_once_tc1337(tmp_path):
    """Without the rule, a hub whose clock steps back a day would skip every
    source for a day: next_due is a day in its future."""
    env = base_env(tmp_path, AI_USAGE_SOURCES="claude")
    reader = Counting(throttle(retry_after=3600))
    cycle(env, {"claude": reader}, NOW)
    cycle(env, {"claude": reader}, NOW - 86400)
    assert reader.calls == [NOW, NOW - 86400]


def test_a_small_step_back_inside_the_skew_keeps_the_schedule_tc1337(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="claude")
    reader = Counting(throttle(retry_after=3600))
    cycle(env, {"claude": reader}, NOW)
    cycle(env, {"claude": reader}, NOW - 120)
    assert reader.calls == [NOW]


def test_a_clock_that_steps_forward_reads_the_source_at_once_tc1337(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="claude")
    reader = Counting(throttle(retry_after=3600))
    cycle(env, {"claude": reader}, NOW)
    cycle(env, {"claude": reader}, NOW + 30 * 86400)
    assert reader.calls == [NOW, NOW + 30 * 86400]


def test_a_backwards_step_restarts_the_auth_run_rather_than_confirming_it_tc1337(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="claude")

    def dead(e):
        raise feeder.AuthFailure("claude: HTTP 401", status=401)

    cycle(env, {"claude": dead}, NOW)
    _rec, failures, _n = cycle(env, {"claude": dead}, NOW - 3600)
    entry = schedule_of(env, "claude")
    assert (entry["auth_since"], entry["auth_attempts"]) == (NOW - 3600, 1)
    assert not any("CONFIRMED" in f for f in failures)


# ═══════════════════════════════════════════════════════════════════════════
# TC-1338 — the pure schedule rules, one line each (LLR-1191, LLR-1193)
# ═══════════════════════════════════════════════════════════════════════════

def test_next_schedule_never_mutates_its_input_tc1338():
    entry = {"last_attempt": NOW, "next_due": NOW + 60, "hold": False, "backoff": 2,
             "last_outcome": "throttled", "auth_since": NOW - 60, "auth_last": NOW,
             "auth_attempts": 2}
    frozen = json.dumps(entry, sort_keys=True)
    for outcome in feeder.OUTCOMES:
        feeder.next_schedule(entry, outcome, NOW + 60, 60, retry_at=None)
    assert json.dumps(entry, sort_keys=True) == frozen


def test_an_unknown_outcome_is_a_programming_error_tc1338():
    with pytest.raises(ValueError):
        feeder.next_schedule(None, "skipped", NOW, 60)


@pytest.mark.parametrize("attempts,span,confirmed", [
    (1, 0, False), (1, 900, False), (2, 599, False), (2, 600, True), (11, 600, True), (30, 300, False),
])
def test_confirmation_needs_both_the_attempts_and_the_span_tc1338(attempts, span, confirmed):
    entry = {"last_attempt": NOW, "auth_since": NOW - span, "auth_last": NOW,
             "auth_attempts": attempts}
    assert feeder.auth_failure_confirmed(entry) is confirmed


def test_a_later_non_auth_attempt_cannot_lend_its_stamp_to_the_span_tc1338(tmp_path):
    """Sol round 1: two 401s a minute apart and a timeout ten minutes later are
    not ten minutes of dead login. The span is first-to-LATEST AUTH attempt."""
    env = base_env(tmp_path, AI_USAGE_SOURCES="claude")

    def dead(e):
        raise feeder.AuthFailure("claude: HTTP 401", status=401)

    def network(e):
        raise feeder.SourceFailure("claude: URLError")

    cycle(env, {"claude": lambda e: feeder.parse_claude(CLAUDE_BODY, e["_now"])}, NOW - 60)
    cycle(env, {"claude": dead}, NOW)
    cycle(env, {"claude": dead}, NOW + 60)
    _rec, failures, _n = cycle(env, {"claude": network}, NOW + 600)
    entry = schedule_of(env, "claude")
    assert (entry["auth_since"], entry["auth_last"], entry["auth_attempts"]) == (NOW, NOW + 60, 2)
    assert not feeder.auth_failure_confirmed(entry)
    assert "auth_failed" not in state_of(env)["ai-usage-claude-session"], "never marked"
    _rec, failures, _n = cycle(env, {"claude": dead}, NOW + 660)
    assert any("attempt 3 over 660s - CONFIRMED" in f for f in failures), "a real span still confirms"


# ═══════════════════════════════════════════════════════════════════════════
# TC-1339 — the Codex read never leaves a process behind (LLR-1194)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_the_codex_read_takes_its_whole_process_tree_with_it_tc1339(tmp_path):
    """The hub's app-server spawns helpers (over a hundred `lsb_release` shells
    in one read, measured 2026-10-22). A fake codex here starts a grandchild
    that would outlive it, answers, and waits; after the read returns, no
    process from that tree may still exist."""
    pidfile = tmp_path / "grandchild.pid"
    fake = tmp_path / "codex"
    fake.write_text(
        "#!/bin/sh\n"
        "sleep 300 &\n"
        "echo $! > '%s'\n"
        "while read -r line; do\n"
        "  case \"$line\" in *rateLimits*)\n"
        "    printf '%%s\\n' '{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":%s}' ;;\n"
        "  esac\n"
        "done\n"
        "sleep 300\n" % (pidfile, json.dumps(CODEX_RESULT)), encoding="utf-8")
    fake.chmod(0o755)
    env = {"AI_USAGE_CODEX_BIN": str(fake), "AI_USAGE_TIMEOUT_SECONDS": "10", "_now": NOW,
           "PATH": os.environ.get("PATH", "")}
    readings = feeder.read_codex(env)
    assert readings["ai-usage-codex"].percent == 34
    grandchild = int(pidfile.read_text().strip())
    deadline = time.time() + 5
    while time.time() < deadline and _alive(grandchild):
        time.sleep(0.05)
    assert not _alive(grandchild), "the app-server's own child outlived the read"


def _alive(pid):
    try:
        with open("/proc/%d/stat" % pid) as handle:
            return handle.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def test_end_child_falls_back_to_kill_without_a_process_group_tc1339():
    class Child(object):
        killed = waited = False

        def kill(self):
            Child.killed = True

        def wait(self, timeout=None):
            Child.waited = True

    feeder.end_child(Child())
    assert Child.killed and Child.waited


def test_end_child_never_signals_a_group_the_child_does_not_lead_tc1339(monkeypatch):
    """Belt and braces: a pid whose group is not its own (e.g. this process's)
    must get a plain kill, never a killpg that would reach the feeder itself."""
    sent = []
    monkeypatch.setattr(feeder.os, "killpg", lambda pid, sig: sent.append(pid), raising=False)
    monkeypatch.setattr(feeder.os, "getpgid", lambda pid: 1, raising=False)

    class Child(object):
        pid = 4242
        killed = False

        def kill(self):
            Child.killed = True

    feeder.end_child(Child())
    assert sent == [] and Child.killed


# ═══════════════════════════════════════════════════════════════════════════
# TC-1340 — the minute timer, bounded by the horizons and the target (LLR-1196)
# ═══════════════════════════════════════════════════════════════════════════

def _seconds(text, key):
    value = [l for l in text.splitlines() if l.startswith(key + "=")][0].split("=", 1)[1].strip()
    m = re.fullmatch(r"(\d+)(s|min|h)?", value)
    assert m, value
    return int(m.group(1)) * {None: 1, "s": 1, "min": 60, "h": 3600}[m.group(2)]


FEEDER_TIMER = (AI_DIR / "homehub-ai-usage.timer").read_text(encoding="utf-8")
REFRESH_TIMER = (AI_DIR / "homehub-claude-refresh@.timer").read_text(encoding="utf-8")
REFRESH_SERVICE = (AI_DIR / "homehub-claude-refresh@.service").read_text(encoding="utf-8")
# The panel's own refresh of the gauges while locked (OfficeWallNaglight
# js/config.js POLL_SECONDS default, and the hub-served config.json, both 60,
# measured 2026-10-22). Quoted, not imported: a change on either side is an edit.
PANEL_POLL_SECONDS = 60
FRESHNESS_TARGET_SECONDS = 180


def test_the_feeder_timer_fires_every_minute_with_little_jitter_tc1340():
    assert _seconds(FEEDER_TIMER, "OnUnitActiveSec") == 60
    assert _seconds(FEEDER_TIMER, "AccuracySec") <= 5
    assert _seconds(FEEDER_TIMER, "RandomizedDelaySec") <= 10


def test_the_due_slack_covers_the_timers_jitter_and_no_more_tc1340():
    jitter = _seconds(FEEDER_TIMER, "RandomizedDelaySec") + _seconds(FEEDER_TIMER, "AccuracySec")
    assert feeder.DUE_SLACK_SECONDS >= jitter
    assert feeder.DUE_SLACK_SECONDS * 2 < feeder.SOURCE_INTERVAL_MIN_SECONDS


def test_the_default_cadence_leaves_room_for_the_freshness_target_tc1340():
    """The WORST healthy served age -- a source read the last moment its
    interval allows, every cycle delayed by the timer's full jitter -- must sit
    inside the 3-minute target for every default, and with a locked panel's
    whole poll added for the per-minute sources. (The target is a 95th
    percentile; README "Cadence" models it at about 110 s for the per-minute
    sources and 170 s for Codex. This is the hard ceiling under it.)"""
    jitter = _seconds(FEEDER_TIMER, "RandomizedDelaySec") + _seconds(FEEDER_TIMER, "AccuracySec")
    tick = _seconds(FEEDER_TIMER, "OnUnitActiveSec")
    for source in feeder.SOURCE_READERS:
        interval = feeder.source_intervals({}, [source])[source]
        cycles = -(-(interval - feeder.DUE_SLACK_SECONDS) // tick)      # cycles between reads
        worst = cycles * (tick + jitter)
        assert worst <= FRESHNESS_TARGET_SECONDS, (source, worst)
        if interval == feeder.DEFAULT_SOURCE_INTERVAL_SECONDS:
            assert worst + PANEL_POLL_SECONDS <= FRESHNESS_TARGET_SECONDS, (source, worst)


def test_the_claude_refresher_checks_often_enough_for_the_confirmation_span_tc1340():
    """A renewal that lands late is absorbed by the 10-minute confirmation only
    if the refresher gets at least two chances inside it."""
    period = _seconds(REFRESH_TIMER, "OnUnitActiveSec") + _seconds(REFRESH_TIMER, "RandomizedDelaySec") \
        + _seconds(REFRESH_TIMER, "AccuracySec")
    assert period * 2 < feeder.AUTH_CONFIRM_SECONDS
    assert gate.LEAD_SECONDS >= 3 * period, "several checks before the token lapses"


# ═══════════════════════════════════════════════════════════════════════════
# TC-1341 — the Claude refresher runs only near expiry, and can only add runs (LLR-1195)
# ═══════════════════════════════════════════════════════════════════════════

def _creds(tmp_path, body, name=".credentials.json"):
    path = tmp_path / name
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return path


@pytest.mark.parametrize("expires_in,expected", [
    (8 * 3600, 3), (3 * 3600, 3), (21 * 60, 3),
    (20 * 60, 0), (60, 0), (0, 0), (-3600, 0),
    (30 * 86400, 0),          # implausible: run
])
def test_the_gate_runs_the_cli_only_inside_the_lead_tc1341(tmp_path, capsys, expires_in, expected):
    path = _creds(tmp_path, {"claudeAiOauth": {"accessToken": "TOKEN-VALUE-1234567890",
                                               "expiresAt": (NOW + expires_in) * 1000}})
    assert gate.main([str(path)], env={}, now=NOW) == expected
    out = capsys.readouterr().out
    assert "TOKEN-VALUE" not in out
    assert (out == "") is (expected == gate.NOT_DUE), "quiet when not due, one line when it runs"


@pytest.mark.parametrize("body", ["", "{", "[]", '{"claudeAiOauth":{}}',
                                  '{"claudeAiOauth":{"expiresAt":"soon"}}',
                                  '{"claudeAiOauth":{"expiresAt":true}}'])
def test_every_doubt_runs_the_cli_tc1341(tmp_path, body):
    assert gate.main([str(_creds(tmp_path, body))], env={}, now=NOW) == 0


def test_a_missing_credential_runs_so_the_login_gate_decides_tc1341(tmp_path):
    assert gate.main([str(tmp_path / "absent.json")], env={}, now=NOW) == 0


def test_an_expiry_in_seconds_is_read_as_seconds_tc1341(tmp_path):
    path = _creds(tmp_path, {"claudeAiOauth": {"expiresAt": NOW + 3 * 3600}})
    assert gate.main([str(path)], env={}, now=NOW) == gate.NOT_DUE


def test_the_gate_reads_the_instances_own_credential_tc1341(tmp_path):
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    (tmp_path / "c2").mkdir()
    _creds(tmp_path / "home" / ".claude", {"claudeAiOauth": {"expiresAt": (NOW + 60) * 1000}})
    _creds(tmp_path / "c2", {"claudeAiOauth": {"expiresAt": (NOW + 5 * 3600) * 1000}})
    assert gate.main([], env={"HOME": str(tmp_path / "home")}, now=NOW) == 0
    assert gate.main([], env={"HOME": str(tmp_path / "home"),
                              "CLAUDE_CONFIG_DIR": str(tmp_path / "c2")}, now=NOW) == gate.NOT_DUE


def test_the_gate_never_opens_anything_for_writing_tc1341():
    source = (AI_DIR / "claude_refresh_due.py").read_text(encoding="utf-8")
    assert not re.search(r"open\([^)]*['\"][wax+]", source)
    assert "os.replace" not in source and "os.rename" not in source and ".write(" not in source


GATE_LINE = [l for l in REFRESH_SERVICE.splitlines()
             if l.startswith("ExecCondition=") and "claude_refresh_due.py" in l]


def test_the_unit_maps_only_not_due_to_a_skip_tc1341(tmp_path):
    """Runs the unit's own ExecCondition shell line against helpers that exit
    each way: only 3 may become the skip (1); a crash, a missing helper and a
    run all pass the gate. So the gate can only ever add a refresher run."""
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no POSIX shell")
    assert len(GATE_LINE) == 1, "exactly one expiry gate in the template"
    line = GATE_LINE[0]
    assert not line.startswith("ExecCondition=+"), "runs as User=, not root"
    m = re.match(r"ExecCondition=/bin/sh -c '(.*)' due (\S+)$", line)
    assert m and m.group(2) == "/opt/homehub/stack/ai-usage/claude_refresh_due.py"
    script = m.group(1).replace("$$", "$").replace("/usr/bin/python3", Path(sys.executable).as_posix())
    outcomes = {}
    for code in (0, 1, 2, 3, 4):
        helper = tmp_path / ("helper%d.py" % code)
        helper.write_text("import sys\nsys.exit(%d)\n" % code, encoding="utf-8")
        outcomes[code] = subprocess.run([sh, "-c", script, "due", helper.as_posix()]).returncode
    outcomes["missing"] = subprocess.run([sh, "-c", script, "due",
                                          (tmp_path / "nope.py").as_posix()]).returncode
    assert outcomes == {0: 0, 1: 0, 2: 0, 3: 1, 4: 0, "missing": 0}


def test_the_expiry_gate_comes_before_the_cli_and_after_the_install_gate_tc1341():
    lines = [l for l in REFRESH_SERVICE.splitlines() if l.startswith(("ExecCondition=", "ExecStart="))]
    kinds = ["install" if "/usr/local/bin/claude" in l and l.startswith("ExecCondition") else
             "expiry" if "claude_refresh_due.py" in l else "start" for l in lines]
    assert kinds == ["install", "expiry", "start"]


# ═══════════════════════════════════════════════════════════════════════════
# TC-1342 — the status step reads a skipped source from the run that read it (LLR-1197)
# (the shell-level cases live in tests/test_ai_cli_integration.py)
# ═══════════════════════════════════════════════════════════════════════════

def test_a_skip_line_never_looks_like_a_gauge_line_tc1342(tmp_path):
    status = (REPO / "stack" / "ai-cli" / "ai-cli-status.sh").read_text(encoding="utf-8")
    pattern = re.search(r'GAUGE_LINE = re\.compile\(r"(.*)"\)', status).group(1)
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex")
    cycle(env, {"codex": codex_ok}, NOW)
    _rec, _f, notes = cycle(env, {"codex": codex_ok}, NOW + 60)
    assert notes
    for note in notes:
        assert not re.search(pattern, "ai-usage: INFO " + note)
