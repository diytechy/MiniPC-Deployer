"""TC-1136..TC-1145 - the weight feeder greys at once when its Google login dies.

Background (plan 3.6 G2, 2026-10-11): the weight bar was dead from 2026-09-19
because the Google refresh token expires after 7 days while the OAuth app is in
"Testing". The feeder kept re-posting the last weight at its original stamp, so
the bar stayed coloured for a week, and its hint said "401 means the refresh
token is no longer valid" when Google actually answers HTTP 400 `invalid_grant`.

What is asserted here, each against the OTHER side of its contract (the wire
body NagLight would receive, the bytes in the state file, the text an operator
would read) and not against a value invented in the same file:

  * the token refresh classifies HTTP 401 and HTTP 400 `invalid_grant` as a
    typed AuthFailure, and nothing else (TC-1136);
  * the operator hint is the right one (TC-1137);
  * only an allow-listed `error` word ever leaves the remote's body, and a
    non-allow-listed one is never reproduced (TC-1138);
  * the consecutive-failure count lives in the state file, survives a restart
    and is validated like the input it is (TC-1139);
  * one failed cycle does not grey, two do (TC-1140);
  * the grey sticks across a later failure of another kind (TC-1141);
  * a failure of another kind alone changes nothing about today's behaviour
    (TC-1142);
  * a success or a failure of another kind resets the count (TC-1143);
  * recovery returns a live gauge (TC-1144);
  * the whole path, with the real reader against a fake Google (TC-1145).

Verifies: TC-1136, TC-1137, TC-1138, TC-1139, TC-1140, TC-1141, TC-1142,
          TC-1143, TC-1144, TC-1145 (SR-022, LLR-1063..LLR-1068)
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from conftest import loopback_server

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "stack" / "weight" / "weight_feeder.py"

_spec = importlib.util.spec_from_file_location("weight_feeder_auth", MODULE_PATH)
feeder = importlib.util.module_from_spec(_spec)
sys.modules["weight_feeder_auth"] = feeder
_spec.loader.exec_module(feeder)

NOW = 1789000000
GOAL = 180.0
OLD_STAMP = NOW - 3 * 3600          # a real weigh-in three hours old
STORED = {"value": 191.4, "observed_at": OLD_STAMP}
SENTINEL = "REMOTE-CHOSE-THESE-BYTES-8821"
STEP = 900                          # the 15-minute timer


# ── helpers ─────────────────────────────────────────────────────────────────

def make_env(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    return {"_identity": "u", "_feed_url": "http://127.0.0.1:1/api/feed",
            "WEIGHT_STATE_FILE": str(state_dir / "weight-state.json"),
            "STATE_DIRECTORY": str(state_dir)}


def state_file(env):
    return Path(env["WEIGHT_STATE_FILE"])


def seed(env, data):
    state_file(env).write_text(json.dumps(data), encoding="utf-8")


def read_state(env):
    return json.loads(state_file(env).read_text(encoding="utf-8"))


def auth_dead(_env):
    raise feeder.AuthFailure("google-health: the token refresh failed: HTTP 400 "
                             "invalid_grant", code="invalid_grant")


def net_dead(_env):
    raise feeder.SourceFailure("google-health: the token refresh failed: "
                               "URLError")


def alive(_env):
    return (176.0, NOW)


def cycle(env, reader, step=0):
    """One run_cycle with an injected reader; returns (posted, failures, bodies)."""
    bodies = []

    def poster(body, url, e, t):
        bodies.append(body)
        return True, "HTTP 200"

    posted, failures, _ = feeder.run_cycle(
        env, now=NOW + step, readers={"google-health": reader}, poster=poster,
        goal_loader=lambda d, *a: (GOAL, "health.md"),
        check_loader=lambda *a: None)
    return posted, failures, bodies


def is_stale_on_arrival(body):
    """The never-measured body: value 0 and NO observed_at (NagLight: stale)."""
    return body["value"] == 0.0 and "observed_at" not in body


# ── TC-1136: the typed failure ──────────────────────────────────────────────

def refresh_against(status, body, tmp_path_unused=None):
    """Run the REAL google_access_token against a fake token endpoint."""
    raw = json.dumps(body).encode() if not isinstance(body, bytes) else body

    def respond(handler):
        handler.send_response(status)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    with loopback_server(respond) as (base, _seen):
        return feeder.google_access_token(
            "client-id", "client-secret", "REFRESH-TOKEN-ABC", 10,
            endpoint=base + "/token")


def test_authfailure_is_a_sourcefailure_so_every_old_handler_still_works():
    assert issubclass(feeder.AuthFailure, feeder.SourceFailure)
    exc = feeder.AuthFailure("x", code=401)
    assert exc.code == 401


def test_a_400_invalid_grant_on_the_refresh_is_an_authfailure_with_that_code():
    with pytest.raises(feeder.AuthFailure) as err:
        refresh_against(400, {"error": "invalid_grant",
                              "error_description": "Token has been expired"})
    assert err.value.code == "invalid_grant"
    assert "HTTP 400" in str(err.value) and "invalid_grant" in str(err.value)


def test_a_401_on_the_refresh_is_an_authfailure_with_the_status():
    with pytest.raises(feeder.AuthFailure) as err:
        refresh_against(401, {"error": "invalid_client"})
    assert err.value.code == 401
    assert "HTTP 401" in str(err.value)


@pytest.mark.parametrize("status, body", [
    (400, {"error": "invalid_request"}),           # our request is malformed
    (400, {"error": "invalid_client"}),            # the OAuth CLIENT is wrong
    (400, {"error": "unauthorized_client"}),
    (400, {"error": {"message": "invalid_grant"}}),  # the other Google shape
    (400, {"error": ["invalid_grant"]}),
    (400, {"error": "INVALID_GRANT"}),             # exact match only
    (400, {"error": "invalid_grant "}),
    (400, {"unrelated": "invalid_grant"}),
    (400, [1, 2, 3]),
    (400, b"<html>not json invalid_grant</html>"),
    (400, b""),
    (403, {"error": "invalid_grant"}),             # wrong status: not ours
    (500, {"error": "invalid_grant"}),
    (503, {"error": "backend_error"}),
])
def test_anything_else_on_the_refresh_stays_an_ordinary_failure(status, body):
    with pytest.raises(feeder.SourceFailure) as err:
        refresh_against(status, body)
    assert not isinstance(err.value, feeder.AuthFailure), (status, body)


def test_a_401_on_the_list_call_is_not_a_signin_failure():
    """The classification is for the TOKEN REFRESH only. A 401 from the list
    call arrives with a freshly minted access token, so it does not mean the
    refresh token is dead, and counting it would grey a healthy login."""
    def respond(handler):
        if handler.path.startswith("/token"):
            raw = json.dumps({"access_token": "ACCESS"}).encode()
            handler.send_response(200)
        else:
            raw = b"{}"
            handler.send_response(401)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tok = Path(tmp) / "t.json"
        tok.write_text(json.dumps({"refresh_token": "R"}), encoding="utf-8")
        env = {"WEIGHT_TOKEN_FILE": str(tok), "_now": NOW,
               "OAUTH2_PROXY_CLIENT_ID": "i", "OAUTH2_PROXY_CLIENT_SECRET": "s",
               "WEIGHT_TIMEOUT_SECONDS": "10"}
        with loopback_server(respond) as (base, _seen):
            with pytest.raises(feeder.SourceFailure) as err:
                feeder.read_google_health(env, list_url=base + "/points",
                                          token_endpoint=base + "/token")
    assert not isinstance(err.value, feeder.AuthFailure)
    assert "HTTP 401" in str(err.value)


# ── TC-1137: the operator hint ──────────────────────────────────────────────

def test_the_hint_names_400_invalid_grant_the_remedy_and_the_7_day_cliff():
    with pytest.raises(feeder.AuthFailure) as err:
        refresh_against(400, {"error": "invalid_grant"})
    text = str(err.value)
    assert "no longer valid" in text
    assert "weight_oauth.py mint --force" in text
    assert "7 days" in text and "Testing" in text and "In production" in text


def test_the_401_hint_is_the_same_remedy():
    with pytest.raises(feeder.AuthFailure) as err:
        refresh_against(401, {})
    assert "weight_oauth.py mint --force" in str(err.value)
    assert "7 days" in str(err.value)


def test_the_old_wrong_claim_is_gone_and_the_403_hint_is_kept():
    """"401 means the refresh token is no longer valid" was the wrong sentence
    (Google answers 400 invalid_grant). The 403 / scope hint must survive."""
    with pytest.raises(feeder.SourceFailure) as err:
        refresh_against(403, {})
    text = str(err.value)
    assert "(the body is not logged). 401 means" not in text
    assert "400 invalid_grant or 401" in text
    assert "scope is no longer on the consent screen" in text
    assert "body is not logged" in text


# ── TC-1138: the narrow amendment of the logging invariant ──────────────────

def test_only_the_allow_listed_word_reaches_the_message():
    """The body quotes a header and carries free text; neither is reproduced,
    but the one allow-listed word is."""
    body = {"error": "invalid_grant",
            "error_description": "Authorization: Bearer %s" % SENTINEL,
            "request": {"headers": {"Authorization": "Bearer " + SENTINEL}}}
    with pytest.raises(feeder.AuthFailure) as err:
        refresh_against(400, body)
    text = str(err.value)
    assert SENTINEL not in text
    assert "Authorization" not in text
    assert "Bearer" not in text
    assert "invalid_grant" in text
    assert repr(err.value).count(SENTINEL) == 0


@pytest.mark.parametrize("shape", [
    {"error": SENTINEL},
    {"error": SENTINEL + " invalid_grant"},
    {"error": "invalid_grant " + SENTINEL},
    {"error": {"message": SENTINEL}},
    {"error": "Authorization: Bearer " + SENTINEL},
])
def test_a_non_allow_listed_error_word_is_never_reproduced(shape):
    """THE MUTATION TARGET. If the allow-list gate is removed, the remote's own
    string is returned and ends up in the AuthFailure message."""
    with pytest.raises(feeder.SourceFailure) as err:
        refresh_against(400, shape)
    assert SENTINEL not in str(err.value)
    assert SENTINEL not in repr(err.value)
    assert not isinstance(err.value, feeder.AuthFailure)


def test_the_helper_returns_our_constant_and_nothing_longer(monkeypatch):
    """What it returns is a member of the allow-list, never the remote string:
    proven by widening the allow-list and watching only exact members pass."""
    class FakeError:
        def __init__(self, payload):
            self.payload = payload

        def read(self, limit):
            return self.payload[:limit]

    assert feeder.allow_listed_oauth_error(
        FakeError(b'{"error": "invalid_grant"}')) == "invalid_grant"
    assert feeder.allow_listed_oauth_error(
        FakeError(b'{"error": "invalid_grant", "x": "' + SENTINEL.encode() + b'"}')
    ) == "invalid_grant"
    assert feeder.allow_listed_oauth_error(FakeError(b"not json")) is None
    assert feeder.allow_listed_oauth_error(FakeError(b'{"error": 7}')) is None
    # An oversized body is cut at the limit, so it cannot parse: None, no leak.
    big = b'{"error": "invalid_grant", "pad": "' + b"x" * 10000 + b'"}'
    assert feeder.allow_listed_oauth_error(FakeError(big)) is None
    # A reader that raises is an answer of None, never an exception.
    class Raising:
        def read(self, limit):
            raise OSError("boom")
    assert feeder.allow_listed_oauth_error(Raising()) is None
    monkeypatch.setattr(feeder, "AUTH_ERROR_ALLOWLIST",
                        frozenset({"invalid_grant", "other_word"}))
    assert feeder.allow_listed_oauth_error(
        FakeError(b'{"error": "other_word"}')) == "other_word"
    assert feeder.allow_listed_oauth_error(
        FakeError(b'{"error": "other_word2"}')) is None


def test_the_allow_list_is_exactly_invalid_grant():
    """Deliberately one entry: invalid_client / unauthorized_client mean the
    OAuth CLIENT is wrong, which re-signing in does not cure."""
    assert feeder.AUTH_ERROR_ALLOWLIST == frozenset({"invalid_grant"})


def test_the_whole_cycle_journal_never_carries_the_remotes_bytes(tmp_path):
    """Through run_cycle, so the line `main` prints is what is searched."""
    env = make_env(tmp_path)
    tok = tmp_path / "t.json"
    tok.write_text(json.dumps({"refresh_token": "R"}), encoding="utf-8")
    env.update({"WEIGHT_TOKEN_FILE": str(tok),
                "OAUTH2_PROXY_CLIENT_ID": "i", "OAUTH2_PROXY_CLIENT_SECRET": "s",
                "WEIGHT_TIMEOUT_SECONDS": "10"})
    raw = json.dumps({"error": "invalid_grant",
                      "error_description": SENTINEL}).encode()

    def respond(handler):
        handler.send_response(400)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    with loopback_server(respond) as (base, _seen):
        reader = lambda e: feeder.read_google_health(  # noqa: E731
            e, token_endpoint=base + "/token")
        _posted, failures, _bodies = cycle(env, reader)
    joined = "\n".join(failures)
    assert SENTINEL not in joined
    assert "invalid_grant" in joined
    assert "REFRESH" not in joined and "client-secret" not in joined


# ── TC-1139: the counter lives in the state file ────────────────────────────

def test_the_counter_survives_save_load_increment_to_two(tmp_path):
    """A restart is a fresh load_state; nothing is carried in memory."""
    env = make_env(tmp_path)
    seed(env, {"weight": STORED})
    cycle(env, auth_dead, 0)
    assert read_state(env)[feeder.AUTH_STATE_KEY] == {"google-health": 1}
    # "Restart": the loader reads the file from scratch.
    assert feeder.load_state(env["WEIGHT_STATE_FILE"], NOW)[
        feeder.AUTH_STATE_KEY] == {"google-health": 1}
    cycle(env, auth_dead, STEP)
    assert read_state(env)[feeder.AUTH_STATE_KEY] == {"google-health": 2}


def test_save_state_carries_the_counter_through_load_state(tmp_path):
    env = make_env(tmp_path)
    feeder.save_state({feeder.AUTH_STATE_KEY: {"google-health": 1},
                       "weight": STORED},
                      env["WEIGHT_STATE_FILE"], env["STATE_DIRECTORY"])
    loaded = feeder.load_state(env["WEIGHT_STATE_FILE"], NOW)
    assert loaded[feeder.AUTH_STATE_KEY] == {"google-health": 1}
    assert loaded["weight"] == STORED


@pytest.mark.parametrize("entry, expected", [
    ({"google-health": 1}, {"google-health": 1}),
    ({"google-health": 2}, {"google-health": 2}),
    ({"google-health": 10 ** 9}, {"google-health": 2}),   # clamped
    ({"google-health": 0}, {}),
    ({"google-health": -1}, {}),
    ({"google-health": True}, {}),                         # bool is not a count
    ({"google-health": 1.0}, {}),
    ({"google-health": "2"}, {}),
    ({"google-health": None}, {}),
    ({"no-such-source": 1}, {}),                           # unknown name
    ({"": 1}, {}),
    ({"google-health": 1, "no-such-source": 2}, {"google-health": 1}),
    ([1, 2], {}),
    ("google-health", {}),
    (7, {}),
    (None, {}),
])
def test_the_counter_validator_drops_what_is_not_a_small_integer(
        tmp_path, entry, expected):
    env = make_env(tmp_path)
    seed(env, {feeder.AUTH_STATE_KEY: entry})
    loaded = feeder.load_state(env["WEIGHT_STATE_FILE"], NOW)
    assert loaded.get(feeder.AUTH_STATE_KEY, {}) == expected


def test_the_generic_loader_would_have_dropped_the_counter(tmp_path):
    """Why a dedicated validator exists: a dict of integers is not a reading.
    If the special case in load_state is removed, this key vanishes."""
    assert feeder.validate_stored_reading({"google-health": 1}, NOW) is None
    env = make_env(tmp_path)
    seed(env, {feeder.AUTH_STATE_KEY: {"google-health": 1}})
    assert feeder.AUTH_STATE_KEY in feeder.load_state(env["WEIGHT_STATE_FILE"], NOW)


def test_a_corrupt_counter_cannot_grey_a_gauge_by_itself(tmp_path):
    """A count alone never greys anything: it takes a failure in the same cycle
    to act on it, and a success resets it."""
    env = make_env(tmp_path)
    seed(env, {"weight": STORED, feeder.AUTH_STATE_KEY: {"google-health": 2}})
    posted, _failures, bodies = cycle(env, alive)
    assert posted == [("weight", True, True)]
    assert bodies[0]["value"] != 0.0 and "observed_at" in bodies[0]
    assert feeder.AUTH_STATE_KEY not in read_state(env)


def test_the_state_file_holds_counts_only_never_a_credential(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"weight": STORED})
    cycle(env, auth_dead, 0)
    cycle(env, auth_dead, STEP)
    raw = state_file(env).read_text(encoding="utf-8")
    for needle in ("REFRESH", "refresh_token", "client-secret", "Bearer",
                   "invalid_grant", "access_token"):
        assert needle not in raw, needle
    assert read_state(env)[feeder.AUTH_STATE_KEY] == {"google-health": 2}


# ── TC-1140: one failure does not grey, two do ──────────────────────────────

def test_one_auth_failure_with_a_stored_reading_still_reposts_it(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"weight": STORED})
    posted, failures, bodies = cycle(env, auth_dead)
    assert bodies[0]["value"] == 191.4
    assert bodies[0]["observed_at"] == feeder.iso8601_utc(OLD_STAMP)
    assert posted == [("weight", False, True)]
    assert read_state(env)["weight"] == STORED
    assert any("sign-in failure 1 of 2" in line and "not yet confirmed" in line
               for line in failures)


def test_two_consecutive_auth_failures_post_stale_on_arrival_and_clear_it(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"weight": STORED})
    cycle(env, auth_dead, 0)
    posted, failures, bodies = cycle(env, auth_dead, STEP)
    assert is_stale_on_arrival(bodies[0]), bodies[0]
    assert posted == [("weight", False, True)]
    stored = read_state(env)
    assert "weight" not in stored, "the stored reading must stop being usable"
    assert stored[feeder.AUTH_STATE_KEY] == {"google-health": 2}
    assert any("sign-in failure 2 of 2" in line and "confirmed" in line
               and "not yet" not in line for line in failures)
    # And it is NagLight-stale by its own rule, not only by our intent.
    assert feeder.is_fresh(bodies[0], NOW + STEP) is False


def test_the_confirmed_body_keeps_the_goal_and_the_wire_shape(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"weight": STORED})
    cycle(env, auth_dead, 0)
    _posted, _failures, bodies = cycle(env, auth_dead, STEP)
    expected = feeder.gauge_body(0.0, GOAL, None)
    assert bodies[0] == expected
    assert bodies[0]["target"] == GOAL


def test_a_third_and_later_auth_failure_stays_grey(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"weight": STORED})
    for step in range(5):
        _p, _f, bodies = cycle(env, auth_dead, step * STEP)
    assert is_stale_on_arrival(bodies[0])
    assert read_state(env)[feeder.AUTH_STATE_KEY] == {"google-health": 2}


def test_the_first_ever_cycle_failing_auth_posts_stale_on_arrival(tmp_path):
    """No history: nothing to keep, nothing to clear, still never a number."""
    env = make_env(tmp_path)
    _p, _f, bodies = cycle(env, auth_dead)
    assert is_stale_on_arrival(bodies[0])


# ── TC-1141: sticky ─────────────────────────────────────────────────────────

def test_auth_failures_then_a_network_failure_stay_stale_on_arrival(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"weight": STORED})
    cycle(env, auth_dead, 0)
    cycle(env, auth_dead, STEP)
    for step in (2, 3, 4):
        _p, _f, bodies = cycle(env, net_dead, step * STEP)
        assert is_stale_on_arrival(bodies[0]), \
            "the old colour must not come back after a network blip"
    assert "weight" not in read_state(env)


def test_build_post_with_the_flag_skips_a_perfectly_good_stored_reading():
    body, fresh = feeder.build_post(None, dict(STORED), GOAL, NOW,
                                    auth_failed=True)
    assert is_stale_on_arrival(body) and fresh is False
    # Same input, flag off: today's behaviour, unchanged.
    body, fresh = feeder.build_post(None, dict(STORED), GOAL, NOW)
    assert body["value"] == 191.4 and "observed_at" in body and fresh is False


def test_run_cycle_threads_the_confirmation_into_build_post(tmp_path, monkeypatch):
    """The class must reach build_post, not only the text. Clearing the stored
    reading makes the flag redundant on the happy path, so the flag is checked
    where it is handed over: it is False until the confirming cycle."""
    seen = []
    real = feeder.build_post

    def spy(reading, last, goal_lb, now, auth_failed=False):
        seen.append(auth_failed)
        return real(reading, last, goal_lb, now, auth_failed=auth_failed)

    monkeypatch.setattr(feeder, "build_post", spy)
    env = make_env(tmp_path)
    seed(env, {"weight": STORED})
    cycle(env, auth_dead, 0)
    cycle(env, auth_dead, STEP)
    cycle(env, net_dead, 2 * STEP)
    cycle(env, alive, 3 * STEP)
    assert seen == [False, True, False, False]


def test_the_flag_never_overrides_a_reading_read_this_cycle():
    body, fresh = feeder.build_post((176.0, NOW), dict(STORED), GOAL, NOW,
                                    auth_failed=True)
    assert fresh is True and body["value"] == 176.0 and "observed_at" in body


# ── TC-1142: a network failure alone keeps today's behaviour ────────────────

def test_a_network_failure_alone_reposts_at_the_original_stamp(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"weight": STORED})
    before = state_file(env).read_bytes()
    for step in range(4):
        _p, failures, bodies = cycle(env, net_dead, step * STEP)
        assert bodies[0]["value"] == 191.4
        assert bodies[0]["observed_at"] == feeder.iso8601_utc(OLD_STAMP)
        assert not any("sign-in failure" in line for line in failures)
    assert state_file(env).read_bytes() == before, \
        "a failed read must not rewrite history it did not improve"


# ── TC-1143: any other outcome resets the count ─────────────────────────────

def test_a_success_resets_the_count(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"weight": STORED})
    cycle(env, auth_dead, 0)
    cycle(env, alive, STEP)
    assert feeder.AUTH_STATE_KEY not in read_state(env)
    _p, _f, bodies = cycle(env, auth_dead, 2 * STEP)
    assert "observed_at" in bodies[0], "one failure after a success is not two"
    assert read_state(env)[feeder.AUTH_STATE_KEY] == {"google-health": 1}


def test_a_network_failure_between_two_auth_failures_resets_the_count(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"weight": STORED})
    cycle(env, auth_dead, 0)
    cycle(env, net_dead, STEP)
    assert feeder.AUTH_STATE_KEY not in read_state(env)
    _p, _f, bodies = cycle(env, auth_dead, 2 * STEP)
    assert bodies[0]["value"] == 191.4, "not consecutive, so not confirmed"
    assert read_state(env)[feeder.AUTH_STATE_KEY] == {"google-health": 1}


def test_an_unexpected_exception_also_resets_the_count(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"weight": STORED})
    cycle(env, auth_dead, 0)

    def bug(_env):
        raise RuntimeError("our own bug")

    cycle(env, bug, STEP)
    assert feeder.AUTH_STATE_KEY not in read_state(env)


# ── TC-1144: recovery ───────────────────────────────────────────────────────

def test_a_renewed_login_returns_a_live_gauge_and_a_clean_state(tmp_path):
    env = make_env(tmp_path)
    seed(env, {"weight": STORED})
    cycle(env, auth_dead, 0)
    cycle(env, auth_dead, STEP)
    posted, _f, bodies = cycle(env, alive, 2 * STEP)
    assert posted == [("weight", True, True)]
    assert bodies[0]["value"] == 176.0
    assert bodies[0]["observed_at"] == feeder.iso8601_utc(NOW)
    stored = read_state(env)
    assert stored["weight"]["value"] == 176.0
    assert feeder.AUTH_STATE_KEY not in stored


# ── TC-1145: the real reader against a fake Google ──────────────────────────

def test_the_real_reader_two_cycles_against_a_dead_refresh_token(tmp_path):
    env = make_env(tmp_path)
    tok = tmp_path / "t.json"
    tok.write_text(json.dumps({"refresh_token": "REFRESH-TOKEN-ABC"}),
                   encoding="utf-8")
    env.update({"WEIGHT_TOKEN_FILE": str(tok),
                "OAUTH2_PROXY_CLIENT_ID": "i", "OAUTH2_PROXY_CLIENT_SECRET": "s",
                "WEIGHT_TIMEOUT_SECONDS": "10"})
    seed(env, {"weight": STORED})
    raw = json.dumps({"error": "invalid_grant",
                      "error_description": "Token has been expired or revoked."
                      }).encode()

    def respond(handler):
        handler.send_response(400)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    with loopback_server(respond) as (base, _seen):
        reader = lambda e: feeder.read_google_health(  # noqa: E731
            e, token_endpoint=base + "/token")
        _p, _f, first = cycle(env, reader, 0)
        _p, _f, second = cycle(env, reader, STEP)
        _p, _f, third = cycle(env, net_dead, 2 * STEP)
    assert first[0]["value"] == 191.4 and "observed_at" in first[0]
    assert is_stale_on_arrival(second[0])
    assert is_stale_on_arrival(third[0])


def test_the_module_docs_and_readme_state_the_400_and_the_7_day_cliff():
    readme = (REPO / "stack" / "weight" / "README.md").read_text(encoding="utf-8")
    assert "invalid_grant" in readme
    assert "7 days" in readme or "7-day" in readme
    assert "In production" in readme
    assert "weight_oauth.py mint --force" in readme or "mint --force" in readme
    # The known limit is written down, not silently inherited.
    assert "weight-waist" in readme and "known limit" in readme.lower()
