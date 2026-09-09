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

from conftest import loopback_server, plain_200, redirect_to

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

# `now` HAD TO BE RE-DERIVED RATHER THAN CHOSEN, and the old value was wrong.
# The parsers now refuse a window that does not CONTAIN `now`, because a reset
# time already in the past is evidence of a replayed or cached body. That makes
# this constant part of the fixture: it must be an instant at which all five of
# the observed windows above were genuinely open. The previous 1789000000 was
# 2026-09-10 00:26 UTC, an hour and a half AFTER Claude's five-hour window had
# already reset - so it was never a moment at which that body could have been
# returned. 2026-09-09 08:53 UTC is: it sits inside every one of the five.
NOW = 1788944000


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
    # The state file must sit inside the service's own StateDirectory; systemd
    # exports that as $STATE_DIRECTORY on the box, and a test that did not set
    # it would be testing the /var/lib default rather than its own tmp_path.
    env.setdefault("STATE_DIRECTORY",
                   os.path.dirname(env["AI_USAGE_STATE_FILE"]))
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
    out = feeder.parse_codex(CODEX_RESULT, NOW)
    assert set(out) == {"ai-usage-codex"}
    reading = out["ai-usage-codex"]
    assert reading.percent == 34.0
    assert reading.window_seconds == 10080 * 60      # the observed weekly window
    assert reading.window_end == 1789435411


def test_parse_codex_ignores_the_per_limit_id_buckets_sr021():
    """Gauges keyed off `rateLimitsByLimitId` would appear and vanish as the
    vendor renames a model, so only the compatible view is read."""
    out = feeder.parse_codex(CODEX_RESULT, NOW)
    assert "codex_bengalfox" not in json.dumps(sorted(out))
    assert len(out) == 1


def test_parse_claude_reads_both_windows_and_discards_severity_sr021():
    out = feeder.parse_claude(CLAUDE_BODY, NOW)
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
    out = feeder.parse_claude(body, NOW)
    assert set(out) == {"ai-usage-claude-weekly"}


def test_parse_opencode_reads_named_windows_and_skips_rolling_sr021():
    out = feeder.parse_opencode(OPENCODE_BODY, NOW)
    assert set(out) == {"ai-usage-opencode-weekly", "ai-usage-opencode-monthly"}
    assert out["ai-usage-opencode-weekly"].percent == 58.0
    assert out["ai-usage-opencode-monthly"].percent == 89.0


def test_parse_opencode_skips_a_bucket_the_vendor_flagged_sr021():
    body = json.loads(json.dumps(OPENCODE_BODY))
    body["usage"]["weekly"]["status"] = "degraded"
    out = feeder.parse_opencode(body, NOW)
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
        feeder.parse_codex(payload, NOW)


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
    ok_reader = {"codex": lambda e: feeder.parse_codex(CODEX_RESULT, NOW)}
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
        readers={"codex": broken, "claude": lambda e: feeder.parse_claude(CLAUDE_BODY, NOW)},
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
        "codex": lambda e: feeder.parse_codex(CODEX_RESULT, NOW),
        "claude": lambda e: feeder.parse_claude(CLAUDE_BODY, NOW),
        "opencode": lambda e: feeder.parse_opencode(OPENCODE_BODY, NOW),
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
    """The ONE body that may have no window is the one that has no stamp.

    `direction` is refused without a `window`, and the never-measured sentinel
    is the only gauge this feeder builds without either.
    """
    spec = feeder.GAUGE_SPECS[0]
    body = feeder.build_gauge(spec, 0.0, None, None, None)
    assert "window" not in body and "direction" not in body
    assert "observed_at" not in body


def test_a_stamped_gauge_may_never_be_windowless_sr021():
    """A malformed-but-200 payload must not become a 7-day-fresh gauge.

    `window.kind` is what selects NagLight's staleness horizon. A gauge that
    carries a real timestamp but no window inherits the STATIC horizon - seven
    days - instead of the 24-48h its vendor window implies, so a source that
    died on Monday is still showing green the following Sunday. `build_gauge`
    refuses to construct that body at all.
    """
    spec = feeder.GAUGE_SPECS[0]
    with pytest.raises(ValueError):
        feeder.build_gauge(spec, 10.0, NOW, None, None)
    with pytest.raises(ValueError):
        feeder.build_gauge(spec, 10.0, NOW, NOW + 600, None)
    with pytest.raises(ValueError):
        feeder.build_gauge(spec, 10.0, NOW, None, 3600)
    ok = feeder.build_gauge(spec, 10.0, NOW, NOW + 600, 3600)
    assert ok["window"]["kind"] == "daily" and ok["direction"] == "up"


@pytest.mark.parametrize("mangle,why", [
    (lambda b: b["five_hour"].pop("resets_at"), "no resets_at at all"),
    (lambda b: b["five_hour"].update(resets_at=None), "a null resets_at"),
    (lambda b: b["five_hour"].update(resets_at="2026-09-08T00:00:00Z"),
     "a resets_at that has already passed"),
])
def test_a_claude_bucket_with_no_usable_window_is_unavailable_not_windowless_sr021(
        mangle, why):
    """200 with a utilization and no usable window is a DEAD source, not a live
    gauge. The bucket drops out; the other one, which is intact, still posts."""
    body = json.loads(json.dumps(CLAUDE_BODY))
    mangle(body)
    out = feeder.parse_claude(body, NOW)
    assert "ai-usage-claude-session" not in out, why
    assert "ai-usage-claude-weekly" in out, "one bad bucket must not blank the other"


def test_an_opencode_bucket_missing_resetsat_is_unavailable_sr021():
    body = json.loads(json.dumps(OPENCODE_BODY))
    del body["usage"]["weekly"]["resetsAt"]
    out = feeder.parse_opencode(body, NOW)
    assert "ai-usage-opencode-weekly" not in out
    assert "ai-usage-opencode-monthly" in out


def test_a_missing_window_is_refused_by_name_and_not_by_accident_sr021():
    """The branch that refuses a windowless reading has to be its OWN branch.

    Found by the mutation run: deleting it left the suite green, because
    `window_kind_for(None)` happens to raise "not a number" a line later. That
    is the right OUTCOME reached by an accident of ordering, and it puts a
    message in the journal that describes a type error rather than the real
    problem — which is that a windowless gauge silently inherits the 7-day
    static horizon. The message is the fix's legibility, so it is asserted.
    """
    with pytest.raises(feeder.SourceFailure) as caught:
        feeder.check_window(None, None, NOW, "test")
    message = str(caught.value)
    assert "no usage window" in message
    assert "7-day static horizon" in message
    with pytest.raises(feeder.SourceFailure) as end_missing:
        feeder.check_window(None, 3600, NOW, "test")
    assert "no usage window" in str(end_missing.value)


def test_a_replayed_vendor_200_is_not_re_stamped_now_sr021():
    """A cached or replayed body describes a window that has already reset.

    Re-stamping it `now` is the fabrication the whole feeder exists to avoid,
    and nothing in the body itself says "this is old" - the reset time is the
    only evidence there is, so it is the evidence that gets used.
    """
    much_later = NOW + 30 * 24 * 3600      # every observed window long gone
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_codex(CODEX_RESULT, much_later)
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_claude(CLAUDE_BODY, much_later)
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_opencode(OPENCODE_BODY, much_later)
    # and a window that has not BEGUN is the same evidence from the other side
    # (a clock error, or milliseconds read as seconds).
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_codex(CODEX_RESULT, NOW - 30 * 24 * 3600)


@pytest.mark.parametrize("minutes", [0, -1, float("nan"), float("inf"), "10080", True, None])
def test_an_invalid_codex_window_fails_the_source_not_the_cycle_sr021(tmp_path, minutes):
    """The defect: `usedPercent: 34` with `windowDurationMins: 0` parsed fine
    and only raised later, in `build_post`, OUTSIDE the per-source catch — so
    the cycle exited before ANY gauge was posted and every other source's
    previously fresh value stayed green on the panel until it expired.

    Two properties, and both are needed: the bad window is refused INSIDE the
    reader, and Claude's two gauges still go out in the same cycle.
    """
    payload = json.loads(json.dumps(CODEX_RESULT))
    payload["rateLimits"]["primary"]["windowDurationMins"] = minutes
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_codex(payload, NOW)

    env = base_env(tmp_path, AI_USAGE_SOURCES="codex,claude")
    rec = Recorder()
    posted, failures = feeder.run_cycle(env, now=NOW, poster=rec, readers={
        "codex": lambda e: feeder.parse_codex(payload, NOW),
        "claude": lambda e: feeder.parse_claude(CLAUDE_BODY, NOW),
    })
    assert {k for k, fresh, _ in posted if fresh} == {
        "ai-usage-claude-session", "ai-usage-claude-weekly"}
    assert len(rec.bodies) == 3, "every gauge must still be posted"
    codex_body = [b for b in rec.bodies if b["id"] == "ai-usage-codex"][0]
    assert not feeder.is_fresh(codex_body, NOW)


def test_a_gauge_that_cannot_be_built_does_not_stop_the_others_sr021(tmp_path):
    """The second half of the same finding, asserted at the loop rather than at
    the parser: a body this repo refuses to BUILD must cost one gauge, not the
    cycle. The refused gauge still posts the unavailable sentinel."""
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex,claude")
    poisoned = feeder.Reading(34.0, None, None)     # a window-free Reading

    rec = Recorder()
    posted, failures = feeder.run_cycle(env, now=NOW, poster=rec, readers={
        "codex": lambda e: {"ai-usage-codex": poisoned},
        "claude": lambda e: feeder.parse_claude(CLAUDE_BODY, NOW),
    })
    assert len(rec.bodies) == 3
    assert any("ai-usage-codex" in f for f in failures)
    codex_body = [b for b in rec.bodies if b["id"] == "ai-usage-codex"][0]
    assert codex_body["value"] == 0 and "observed_at" not in codex_body
    for claude in [b for b in rec.bodies if b["id"].startswith("ai-usage-claude")]:
        assert feeder.is_fresh(claude, NOW)


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
        feeder.open_for_write(str(target), str(target), str(tmp_path))
    assert target.read_text(encoding="utf-8") == '{"real":"token"}'


def test_open_for_write_refuses_everything_outside_the_one_state_path_sr021(tmp_path):
    state = tmp_path / "usage-state.json"
    for other in ("elsewhere.json", "../escape.json", "sub/deep.json"):
        with pytest.raises(PermissionError):
            feeder.open_for_write(str(tmp_path / other), str(state), str(tmp_path))
    # the state file itself and its atomic-rename sibling are the ONLY two.
    feeder.open_for_write(str(state), str(state), str(tmp_path)).close()
    feeder.open_for_write(str(state) + ".tmp", str(state), str(tmp_path)).close()


# ── V1: the symlink that walked through the allow-list ──────────────────────

def test_a_symlinked_temp_file_cannot_truncate_a_vendor_token_sr021(tmp_path):
    """THE CROSS-REVIEW DEFECT, ASSERTED AGAINST THE REAL FILESYSTEM.

    `os.path.abspath` does not resolve symlinks, so pre-creating `<state>.tmp`
    as a link pointing at a vendor token passed the old allow-list unchanged —
    the STRING matched — and `save_state` opened it "w" and truncated the
    household's token. This runs the real `save_state` against a real symlink
    and requires the token to come out byte-identical.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    token = tmp_path / "token-store.json"
    token.write_text('{"refreshToken":"REAL-VENDOR-TOKEN"}', encoding="utf-8")
    state = state_dir / "usage-state.json"
    try:
        os.symlink(str(token), str(state) + ".tmp")
    except (OSError, NotImplementedError):      # pragma: no cover - platform
        pytest.skip("this filesystem/account cannot create symlinks")

    with pytest.raises(PermissionError):
        feeder.save_state({"ai-usage-codex": {"value": 1.0, "observed_at": NOW,
                                              "window_end": NOW + 600,
                                              "window_seconds": 3600}},
                          str(state), str(state_dir))

    assert token.read_text(encoding="utf-8") == '{"refreshToken":"REAL-VENDOR-TOKEN"}', (
        "the feeder followed a symlink and truncated a vendor token")


def test_a_planted_symlink_costs_the_history_and_never_the_token_sr021(tmp_path):
    """Refusing the write is loud, and it must not also be fatal.

    A cycle whose state write is refused has ALREADY posted its gauges; losing
    the history is a named failure in the journal, not a crash and not a
    fabricated reading. This is the operational half of the symlink refusal.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    victim = tmp_path / "vendor-token.json"
    victim.write_text("REAL-VENDOR-TOKEN", encoding="utf-8")
    state = state_dir / "usage-state.json"
    try:
        os.symlink(str(victim), str(state) + ".tmp")
    except (OSError, NotImplementedError):      # pragma: no cover - platform
        pytest.skip("this filesystem/account cannot create symlinks")

    env = base_env(tmp_path, AI_USAGE_SOURCES="codex",
                   AI_USAGE_STATE_FILE=str(state),
                   STATE_DIRECTORY=str(state_dir))
    rec = Recorder()
    posted, failures = feeder.run_cycle(
        env, now=NOW, poster=rec,
        readers={"codex": lambda e: feeder.parse_codex(CODEX_RESULT, NOW)})

    assert rec.bodies and rec.bodies[0]["value"] == 34.0, "the gauge still posts"
    assert any("state" in f for f in failures), "and the refusal is reported"
    assert victim.read_text(encoding="utf-8") == "REAL-VENDOR-TOKEN"


def test_the_write_verdict_resolves_links_rather_than_normalising_strings_sr021(tmp_path):
    """The DECISION, tested without needing a filesystem that grants symlinks.

    The resolver is injected, so this states directly what the old code got
    wrong: given a `.tmp` name that RESOLVES to a token, the verdict must
    refuse. A verdict built on `os.path.abspath` cannot see that at all.
    """
    state_dir = tmp_path / "state"
    state = str(state_dir / "usage-state.json")
    token = str(tmp_path / "vendor" / "auth.json")

    def resolve_like_a_symlink(path):
        return token if path == state + ".tmp" else os.path.abspath(path)

    assert feeder.writable_path_verdict(
        state + ".tmp", state, str(state_dir), resolve=os.path.abspath) is None
    reason = feeder.writable_path_verdict(
        state + ".tmp", state, str(state_dir), resolve=resolve_like_a_symlink)
    assert reason and "auth.json" in reason


def test_the_state_file_must_sit_inside_the_services_own_state_directory_sr021(tmp_path):
    """The allow-list alone only proves the feeder wrote where the .env told it
    to. A knob pointing at a token is refused because the token is not under
    StateDirectory=, whatever the knob says."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    outside = tmp_path / "home" / ".claude" / "usage-state.json"
    outside.parent.mkdir(parents=True)
    reason = feeder.writable_path_verdict(str(outside), str(outside), str(state_dir))
    assert reason and "state directory" in reason
    with pytest.raises(PermissionError):
        feeder.open_for_write(str(outside), str(outside), str(state_dir))
    assert not outside.exists(), "the refused path must not have been created"


def test_the_state_root_comes_from_the_units_state_directory_sr021():
    """systemd exports $STATE_DIRECTORY for `StateDirectory=homehub-ai`, and a
    hand-run cycle falls back to the same path spelled out rather than to no
    bound at all."""
    assert feeder.resolve_state_root({"STATE_DIRECTORY": "/var/lib/homehub-ai"}) \
        == "/var/lib/homehub-ai"
    assert feeder.resolve_state_root({}) == feeder.DEFAULT_STATE_ROOT
    assert feeder.resolve_state_root({"STATE_DIRECTORY": "  "}) == feeder.DEFAULT_STATE_ROOT
    unit = (REPO / "stack" / "ai-usage" / "homehub-ai-usage.service").read_text(
        encoding="utf-8")
    # Matched at the START OF A LINE: the unit explains its choices in comments
    # that quote the directives verbatim, and a substring test passes on the
    # explanation alone. That vacuous shape has now been found twice in this
    # build, so it is not repeated here.
    directives = [l.strip() for l in unit.splitlines()
                  if l and not l.startswith(("#", "[", " "))]
    assert "StateDirectory=homehub-ai" in directives
    assert feeder.DEFAULT_STATE_ROOT == "/var/lib/homehub-ai"


def test_a_temp_file_planted_between_the_check_and_the_open_is_refused_sr021(tmp_path):
    """The check-then-open race, not just the check.

    `open_no_follow` unlinks first — which destroys a planted LINK and never
    the file it points at — and then creates with O_CREAT|O_EXCL, so the kernel
    refuses anything that appeared in the gap rather than trusting the verdict
    we took a moment ago.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    victim = tmp_path / "victim.json"
    victim.write_text("KEEP-ME", encoding="utf-8")
    tmp_file = state_dir / "usage-state.json.tmp"
    try:
        os.symlink(str(victim), str(tmp_file))
    except (OSError, NotImplementedError):      # pragma: no cover - platform
        pytest.skip("this filesystem/account cannot create symlinks")
    with feeder.open_no_follow(str(tmp_file)) as handle:
        handle.write("{}")
    assert victim.read_text(encoding="utf-8") == "KEEP-ME"
    assert not tmp_file.is_symlink()


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
        "codex": lambda e: feeder.parse_codex(CODEX_RESULT, NOW),
        "claude": lambda e: feeder.parse_claude(CLAUDE_BODY, NOW),
        "opencode": lambda e: feeder.parse_opencode(OPENCODE_BODY, NOW),
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
                     readers={"codex": lambda e: feeder.parse_codex(CODEX_RESULT, NOW)})
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

    # `_now` is pinned to the capture instant: the parsers now check each
    # vendor window against the cycle's clock, so a test that let the real
    # clock in would start failing the day the fixtures' windows closed.
    feeder.read_opencode({"AI_USAGE_OPENCODE_AUTH": str(auth), "_now": NOW})
    feeder.read_claude({"AI_USAGE_CLAUDE_CREDENTIALS": str(creds),
                        "AI_USAGE_CLAUDE_VERSION": "2.1.201", "_now": NOW})

    for url, headers in seen.items():
        agent = headers.get("User-Agent", "")
        assert agent and "Python-urllib" not in agent, url
    claude_headers = [h for u, h in seen.items() if "anthropic" in u][0]
    assert claude_headers["User-Agent"] == "claude-code/2.1.201"
    assert claude_headers["anthropic-beta"] == "oauth-2025-04-20"


# ═══════════════════════════════════════════════════════════════════════════
# V2 — the two ways the feed could leave this box
# ═══════════════════════════════════════════════════════════════════════════

def test_the_feed_post_refuses_a_redirect_and_never_re_sends_it_sr021(tmp_path):
    """THE FIRST HALF OF THE EGRESS DEFECT, against real sockets.

    urllib's default opener follows redirects and carries the request's headers
    onto the new request. So the validated loopback endpoint could answer
    `302 Location: <somewhere else>` and urllib would re-send the POST — the
    feed bearer token, the X-Forwarded-User identity and the body — to whatever
    host the response named. Both halves are asserted: the post fails, AND the
    second server records that nothing ever arrived.
    """
    with loopback_server(plain_200) as (elsewhere, arrived):
        with loopback_server(redirect_to(elsewhere + "/api/feed")) as (front, front_seen):
            env = base_env(tmp_path, AI_USAGE_FEED_TOKEN="FEED-TOKEN-SECRET")
            ok, detail = feeder.post_gauge(
                {"kind": "gauge", "id": "ai-usage-codex", "value": 34.0},
                front + "/api/feed", env, 10)

        assert ok is False
        assert "redirect" in detail
        assert arrived == [], "the POST was re-sent to the redirect target"
    assert front_seen and front_seen[0]["path"] == "/api/feed"
    assert "FEED-TOKEN-SECRET" not in detail


def test_the_feed_post_ignores_an_http_proxy_in_the_environment_sr021(
        tmp_path, monkeypatch):
    """THE SECOND HALF. An `http_proxy` exported into the unit's environment
    would otherwise route the POST — token, identity header and body — through
    a LAN proxy that then sees all of it, without the feed URL changing at all.

    Both servers are on loopback so that the peer check passes and this test is
    about the proxy alone.
    """
    with loopback_server(plain_200) as (proxy_url, proxy_seen):
        with loopback_server(plain_200) as (target, target_seen):
            for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
                monkeypatch.setenv(name, proxy_url)
            env = base_env(tmp_path, AI_USAGE_FEED_TOKEN="FEED-TOKEN-SECRET")
            ok, detail = feeder.post_gauge(
                {"kind": "gauge", "id": "ai-usage-codex", "value": 34.0},
                target + "/api/feed", env, 10)

    assert ok is True, detail
    assert proxy_seen == [], "the POST went through the environment's proxy"
    assert target_seen and target_seen[0]["path"] == "/api/feed"
    assert target_seen[0]["headers"]["Authorization"] == "Bearer FEED-TOKEN-SECRET"


def test_the_feed_post_re_validates_the_address_it_actually_reached_sr021(tmp_path):
    """"Re-validate the address actually connected to", asserted where it acts.

    The peer check runs inside `connect()`, after the handshake and BEFORE
    http.client writes the request line — so a connection that lands somewhere
    it should not is dropped with the token and the body still unsent. Here the
    destination judgement is forced to "not on this box" and the server must
    record that no request ever arrived.
    """
    with loopback_server(plain_200) as (target, seen):
        opener = feeder.feed_opener(bridge_addresses=[])
        original = feeder.is_local_destination
        try:
            feeder.is_local_destination = lambda host, bridges=None: False
            request = __import__("urllib.request", fromlist=["request"]).Request(
                target + "/api/feed", data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST")
            with pytest.raises(feeder.EgressRefused):
                opener.open(request, timeout=10)
        finally:
            feeder.is_local_destination = original
    assert seen == [], "bytes reached a peer the guard should have refused"


def test_a_vendor_redirect_is_refused_so_the_bearer_token_does_not_follow_sr021():
    """The VENDOR GETs are a separate decision, and this is the half that is
    the same: urllib does not strip `Authorization` across a cross-host
    redirect, so a 302 from an impersonated vendor endpoint would hand out the
    household's Claude OAuth token. Refused; the source goes unavailable."""
    import urllib.request as urlreq
    with loopback_server(plain_200) as (elsewhere, arrived):
        with loopback_server(redirect_to(elsewhere + "/usage")) as (vendor, _):
            request = urlreq.Request(
                vendor + "/usage",
                headers={"Authorization": "Bearer VENDOR-TOKEN-SECRET"})
            with pytest.raises(feeder.EgressRefused):
                feeder.vendor_opener().open(request, timeout=10)
        assert arrived == [], "the vendor token followed a redirect"


def test_the_vendor_opener_inherits_no_proxy_from_the_environment_sr021(monkeypatch):
    """Deliberate, and different from the feed only in what it CANNOT check.

    These calls go to the internet by design, so there is no peer check — but a
    box-wide `http_proxy` would still route a vendor bearer token through a LAN
    proxy that terminates TLS. There is no knob to opt back in: an egress proxy
    is a requirement change, not a setting.
    """
    import urllib.request as urlreq
    with loopback_server(plain_200) as (proxy_url, proxy_seen):
        with loopback_server(plain_200) as (vendor, vendor_seen):
            for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
                monkeypatch.setenv(name, proxy_url)
            request = urlreq.Request(
                vendor + "/usage",
                headers={"Authorization": "Bearer VENDOR-TOKEN-SECRET"})
            feeder.vendor_opener().open(request, timeout=10).read()
    assert proxy_seen == [], "a vendor bearer token went through a LAN proxy"
    assert vendor_seen and vendor_seen[0]["path"] == "/usage"


def test_a_vendor_url_carrying_a_token_must_be_https_sr021():
    """AI_USAGE_CLAUDE_URL and AI_USAGE_OPENCODE_URL are knobs, and the headers
    `http_json` attaches carry a bearer token. An http one puts that token on
    the wire in clear, so it is refused at the one place the token is attached."""
    with pytest.raises(feeder.SourceFailure) as caught:
        feeder.http_json("http://api.example/usage", {"Authorization": "Bearer X"}, 5)
    assert "https" in str(caught.value)
    assert "Bearer X" not in str(caught.value)


def test_a_remote_error_body_is_never_written_to_the_journal_sr021(tmp_path):
    """`exc.read()[:200]` put whatever a responding server or an interposed
    proxy chose to reflect — a token echoed back in an error body included —
    into a string `main` prints to stderr and systemd persists in the journal.
    The status code is ours to read; the body is the remote's to write."""
    def reflect(handler):
        body = json.dumps({"error": "Authorization: Bearer FEED-TOKEN-SECRET"}).encode()
        handler.send_response(500)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    with loopback_server(reflect) as (target, _):
        env = base_env(tmp_path, AI_USAGE_FEED_TOKEN="FEED-TOKEN-SECRET")
        ok, detail = feeder.post_gauge({"kind": "gauge", "id": "x", "value": 1.0},
                                       target + "/api/feed", env, 10)
    assert ok is False
    assert "500" in detail
    assert "FEED-TOKEN-SECRET" not in detail
    assert "Authorization" not in detail


# ═══════════════════════════════════════════════════════════════════════════
# Loaded state is input, not memory
# ═══════════════════════════════════════════════════════════════════════════

def _sane_entry(**over):
    entry = {"value": 34.0, "observed_at": NOW - 3600,
             "window_end": NOW + 600, "window_seconds": 7 * 24 * 3600}
    entry.update(over)
    return entry


def test_a_sane_stored_reading_is_kept_sr021():
    assert feeder.validate_stored_reading(_sane_entry(), NOW) is not None


@pytest.mark.parametrize("entry,why", [
    (None, "not an object at all"),
    ("34", "a string"),
    (_sane_entry(value=100000), "a percent far outside 0..100"),
    (_sane_entry(value=-500), "a negative reading"),
    (_sane_entry(value=float("nan")), "not a number"),
    (_sane_entry(value="34"), "a numeric string"),
    (_sane_entry(value=True), "a bool, which Python calls an int"),
    (_sane_entry(observed_at=NOW + 90000), "a stamp in the FUTURE"),
    (_sane_entry(observed_at=1), "a stamp before this feeder existed"),
    (_sane_entry(observed_at="yesterday"), "a stamp that is not a number"),
    (_sane_entry(window_seconds=0), "a window of no length"),
    (_sane_entry(window_seconds=-1), "a negative window"),
    (_sane_entry(window_end=None), "no window at all"),
    (_sane_entry(observed_at=NOW - 40 * 24 * 3600), "a stamp outside its own window"),
])
def test_a_nonsensical_stored_reading_is_a_failure_not_history_sr021(entry, why):
    """Corrupt or tampered state used to post a fabricated fresh reading.

    Nothing checked it: `build_gauge` asked only whether the value was finite,
    so `-500` and `100000` percent went out, and a stamp in the future made a
    source that had been dead for days render live. A stored entry that cannot
    be true is not history — it gets the same answer a source that never
    succeeded gets.
    """
    assert feeder.validate_stored_reading(entry, NOW) is None, why


def test_the_state_file_is_filtered_AT_THE_LOAD_sr021(tmp_path):
    """One of the two layers, on its own.

    Found by the mutation run: `load_state` and `build_post` both validate, so
    breaking EITHER left the suite green — the other one caught it. A guard
    that only the other guard proves is a guard nobody has tested, so each
    layer is now asserted where it acts. Dropping a bad entry at the DOOR is
    what stops one corrupted gauge from costing its four neighbours their
    history.
    """
    state = tmp_path / "usage-state.json"
    state.write_text(json.dumps({
        "ai-usage-codex": _sane_entry(value=100000),
        "ai-usage-claude-weekly": _sane_entry(value=87.0),
    }), encoding="utf-8")
    loaded = feeder.load_state(str(state), NOW)
    assert set(loaded) == {"ai-usage-claude-weekly"}
    assert loaded["ai-usage-claude-weekly"]["value"] == 87.0


def test_a_corrupt_stored_reading_is_refused_AT_THE_POINT_OF_USE_sr021():
    """The other layer, on its own, handed the entry directly.

    The load and the use are separated by every source read in the cycle, so
    the guard has to hold at both ends and each has to be tested at its own end.
    """
    spec = feeder.GAUGE_SPECS[0]
    body, fresh = feeder.build_post(spec, None, _sane_entry(value=100000), NOW)
    assert fresh is False
    assert body["value"] == 0 and "observed_at" not in body
    good, fresh = feeder.build_post(spec, None, _sane_entry(value=34.0), NOW)
    assert fresh is False and good["value"] == 34.0


def test_corrupt_state_posts_unavailable_and_never_a_fabricated_reading_sr021(tmp_path):
    """End to end, through the real state file: a tampered entry must not
    resurrect as a gauge, and its neighbours must keep their real history."""
    state = tmp_path / "usage-state.json"
    state.write_text(json.dumps({
        "ai-usage-codex": _sane_entry(value=100000, observed_at=NOW + 99999),
        "ai-usage-claude-session": _sane_entry(value=12.0, observed_at=NOW - 7200,
                                               window_end=NOW + 600,
                                               window_seconds=5 * 3600),
    }), encoding="utf-8")

    env = base_env(tmp_path, AI_USAGE_SOURCES="codex,claude",
                   AI_USAGE_STATE_FILE=str(state))

    def broken(e):
        raise feeder.SourceFailure("HTTP 401")

    rec = Recorder()
    feeder.run_cycle(env, now=NOW, poster=rec,
                     readers={"codex": broken, "claude": broken})
    bodies = {b["id"]: b for b in rec.bodies}
    assert bodies["ai-usage-codex"]["value"] == 0
    assert "observed_at" not in bodies["ai-usage-codex"]
    assert not feeder.is_fresh(bodies["ai-usage-codex"], NOW)
    # the untampered neighbour is still real history, reposted at its own stamp
    assert bodies["ai-usage-claude-session"]["value"] == 12.0
    assert bodies["ai-usage-claude-session"]["observed_at"] == NOW - 7200


def test_the_invariant_holds_across_every_fix_sr021(tmp_path):
    """observed_at == now IF AND ONLY IF this cycle read the source.

    Restated as one test because six separate changes in this round could each
    have broken it from a different direction.
    """
    env = base_env(tmp_path)
    rec = Recorder()
    posted, _ = feeder.run_cycle(env, now=NOW, poster=rec, readers={
        "codex": lambda e: feeder.parse_codex(CODEX_RESULT, NOW),
        "claude": lambda e: feeder.parse_claude(CLAUDE_BODY, NOW),
        "opencode": lambda e: {},          # read, but produced nothing
    })
    fresh = {k for k, f, _ in posted if f}
    for body in rec.bodies:
        assert (body.get("observed_at") == NOW) is (body["id"] in fresh)
        if body["id"] not in fresh:
            assert not feeder.is_fresh(body, NOW)
