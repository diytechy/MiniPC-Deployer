"""TC-1162..TC-1171 - the stdlib myAir sign-in, and a dead login going grey (plan 3.6 G2).

The loopback fakes in test_sleep_support.py stand in for Okta and AppSync and are
built from the shapes the S0b probe observed on 2026-10-11. They CHECK the
contract from the server's side: the PKCE verifier against the challenge, the
cookie the first call handed out, the session token and the bearer token each
later call must carry. A sign-in that is wrong fails here and not on the hub.

  * the flow is the four Okta calls then one GraphQL call (TC-1162);
  * the password is un-doubled and goes in exactly one body; it never reaches an
    exception, a journal line or the state file (TC-1163, TC-1166);
  * only an Okta 401, a non-SUCCESS authn status, or a GraphQL 401/unauthorized
    is an AuthFailure; a 500, a 429, a timeout and a malformed body are not
    (TC-1164);
  * one auth failure keeps the last score, two grey it (TC-1165);
  * the count survives a restart and a non-auth outcome resets it (TC-1167);
  * auth, auth, network stays grey; a success clears it (TC-1168);
  * MFA_REQUIRED says so, and nothing in the feeder can read a mailbox (TC-1169);
  * the remote's own words never reach the journal (TC-1170);
  * a real feeder run against the loopback fakes posts the right gauge (TC-1171).

Verifies: TC-1162, TC-1163, TC-1164, TC-1165, TC-1166, TC-1167, TC-1168,
          TC-1169, TC-1170, TC-1171 (SR-070, SR-072, LLR-1086..LLR-1090)
"""

import ast
import json
import re
import socket
import threading
from pathlib import Path

import pytest

from conftest import loopback_server
from test_sleep_support import (
    ACCESS_TOKEN, SYNCED, CENTRAL, DEVICE_COOKIE, FakeMyAir, Harness, feeder, local, serve_myair, utc)

TZ = CENTRAL
NOW = local(TZ, 2026, 10, 12, 10, 30)
RECORDS = [{"startDate": "2026-10-11", "sleepScore": 82, "__typename": "SleepRecord"},
           {"startDate": "2026-10-10", "sleepScore": 79, "__typename": "SleepRecord"}]
SENTINEL = "REMOTE-WORDS-MUST-NOT-APPEAR"


def read_through(fake, base, password="pass-word-1", email="sleeper@example.invalid"):  # privacy-ok: placeholder
    env = {"MYAIR_EMAIL": email, "MYAIR_PASSWORD": password}
    return feeder.read_myair(env, NOW, TZ, endpoints=fake.endpoints(base))


# ── TC-1162 ──────────────────────────────────────────────────────────────────

def test_the_flow_is_four_okta_calls_then_one_graphql_call():
    fake = FakeMyAir()
    fake.records = RECORDS
    with serve_myair(fake) as (base, seen):
        records, unreadable = read_through(fake, base)
    assert fake.problems == []
    assert fake.requests == [
        ("GET", "/oauth2/aus_test/v1/authorize"),      # 400, hands out the device cookie
        ("POST", "/api/v1/authn"),
        ("GET", "/oauth2/aus_test/v1/authorize"),      # 302, the code in the fragment
        ("POST", "/oauth2/aus_test/v1/token"),
        ("POST", "/graphql")]
    assert unreadable == 0
    assert [(r.start_date.isoformat(), r.score) for r in records] == \
        [("2026-10-11", 82), ("2026-10-10", 79)]


def test_the_query_asks_for_thirty_days_and_only_the_two_fields_it_uses():
    fake = FakeMyAir()
    fake.records = RECORDS
    with serve_myair(fake) as (base, _seen):
        read_through(fake, base)
    query = fake.graphql_query
    assert 'startMonth: "2026-09-12"' in query and 'endMonth: "2026-10-12"' in query
    assert "startDate" in query and "sleepScore" in query
    for unused in ("totalUsage", "ahi", "leakPercentile", "maskPairCount", "usageScore",
                   "firstName", "fgDevices", "serialNumber"):
        assert unused not in query, unused


def test_the_authorization_code_is_read_from_the_redirect_fragment_not_followed():
    """The 302's Location is the redirect URI: a followed redirect would call it."""
    fake = FakeMyAir()
    fake.records = RECORDS
    with serve_myair(fake) as (base, seen):
        read_through(fake, base)
    assert all(request["path"].split("?")[0] in (
        "/oauth2/aus_test/v1/authorize", "/api/v1/authn", "/oauth2/aus_test/v1/token", "/graphql")
        for request in seen)


def test_the_pkce_pair_is_s256():
    verifier, challenge = feeder.pkce_pair()
    import base64
    import hashlib
    assert re.match(r"^[A-Za-z0-9]{43,128}$", verifier)
    assert challenge == base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert feeder.pkce_pair()[0] != verifier


def test_the_production_endpoints_are_https_and_refuse_plain_http():
    ep = feeder.NA_ENDPOINTS
    assert ep.https_only is True
    for url in (ep.okta_base, ep.redirect_uri, ep.graphql_url):
        assert url.startswith("https://"), url
    with pytest.raises(feeder.SourceFailure):
        feeder._require_https("http://resmed-ext-1.okta.com/api/v1/authn", ep)
    feeder._require_https("https://resmed-ext-1.okta.com/api/v1/authn", ep)


def test_a_vendor_redirect_is_never_followed_with_the_password():
    """If /api/v1/authn redirected, a followed 307 would re-POST the password elsewhere."""
    elsewhere_hits = []

    with loopback_server(lambda h: (elsewhere_hits.append(h.path), h.send_response(200),
                                    h.send_header("Content-Length", "2"), h.end_headers(),
                                    h.wfile.write(b"{}"))) as (other, other_seen):
        fake = FakeMyAir()

        def respond(handler):
            if handler.path.startswith("/api/v1/authn"):
                handler.send_response(307)
                handler.send_header("Location", other + "/stolen")
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return
            fake._last_body = b""
            fake(handler)

        with loopback_server(respond) as (base, _seen):
            with pytest.raises(feeder.SourceFailure) as caught:
                read_through(fake, base)
        assert not isinstance(caught.value, feeder.AuthFailure)
        assert other_seen == []                    # the second host received nothing at all


# ── TC-1163 / TC-1166 ────────────────────────────────────────────────────────

def test_the_password_goes_in_exactly_one_body_and_nowhere_else():
    fake = FakeMyAir(password="pass-word-SENTINEL-3381")
    fake.records = RECORDS
    with serve_myair(fake) as (base, seen):
        read_through(fake, base, password="pass-word-SENTINEL-3381")
    carrying = [r["path"] for r in seen
                if b"pass-word-SENTINEL-3381" in r["body"]
                or "pass-word-SENTINEL-3381" in json.dumps(r["headers"])
                or "pass-word-SENTINEL-3381" in r["path"]]
    assert carrying == ["/api/v1/authn"]


@pytest.mark.parametrize("stored,real", [
    ("pa$$word", "pa$word"),               # the materialiser doubled the dollar
    ("$$$$", "$$"),                         # a genuine double dollar arrives as four
    ("pa$word", "pa$word"),                 # a hand-written single dollar is left alone
    ("plain", "plain")])
def test_the_doubled_dollar_the_materialiser_writes_is_collapsed(stored, real):
    assert feeder.undouble_dollars(stored) == real
    email, password = feeder.resolve_credentials({"MYAIR_EMAIL": " me@example.invalid ",  # privacy-ok: placeholder
                                                  "MYAIR_PASSWORD": stored})
    assert password == real and email == "me@example.invalid"  # privacy-ok: placeholder


def test_a_password_with_a_dollar_signs_in_with_the_collapsed_value():
    fake = FakeMyAir(password="pa$word")
    fake.records = RECORDS
    with serve_myair(fake) as (base, _seen):
        records, _ = read_through(fake, base, password="pa$$word")     # as it sits in .env
    assert len(records) == 2


@pytest.mark.parametrize("env", [
    {}, {"MYAIR_EMAIL": "x@example.invalid"},  # privacy-ok: placeholder
    {"MYAIR_PASSWORD": "x"}, {"MYAIR_EMAIL": " ", "MYAIR_PASSWORD": "x"},
    {"MYAIR_EMAIL": "x@example.invalid", "MYAIR_PASSWORD": ""}])  # privacy-ok: placeholder
def test_a_blank_sign_in_is_refused_by_name_without_echoing_anything(env):
    with pytest.raises(SystemExit) as caught:
        feeder.resolve_credentials(env)
    message = str(caught.value)
    assert "REFUSED" in message and "MYAIR_" in message
    for value in env.values():
        assert value.strip() == "" or value not in message


# ── TC-1164 ──────────────────────────────────────────────────────────────────

def run_expecting(fake, **kw):
    with serve_myair(fake) as (base, _seen):
        try:
            read_through(fake, base, **kw)
        except feeder.SourceFailure as exc:
            return exc
    raise AssertionError("expected a SourceFailure")


def test_an_okta_401_is_an_authentication_failure():
    fake = FakeMyAir()
    exc = run_expecting(fake, password="the-wrong-password")
    assert type(exc) is feeder.AuthFailure and exc.status == 401
    assert SENTINEL not in str(exc)


@pytest.mark.parametrize("status,word", [
    ("LOCKED_OUT", "LOCKED_OUT"), ("PASSWORD_EXPIRED", "PASSWORD_EXPIRED"),
    ("PASSWORD_WARN", "PASSWORD_WARN"), ("A_STATUS_NOBODY_HAS_SEEN", "UNRECOGNISED")])
def test_a_non_success_authn_status_is_an_authentication_failure_by_allow_listed_word(status, word):
    fake = FakeMyAir()
    fake.authn_status = status
    exc = run_expecting(fake)
    assert type(exc) is feeder.AuthFailure and exc.code == word
    assert word in str(exc) and "A_STATUS_NOBODY_HAS_SEEN" not in str(exc)


def test_mfa_required_is_its_own_authentication_failure():
    fake = FakeMyAir()
    fake.authn_status = "MFA_REQUIRED"
    exc = run_expecting(fake)
    assert isinstance(exc, feeder.MfaRequired) and isinstance(exc, feeder.AuthFailure)


def test_a_401_from_the_token_endpoint_is_an_authentication_failure():
    fake = FakeMyAir()
    fake.token_http = 401
    exc = run_expecting(fake)
    assert type(exc) is feeder.AuthFailure and exc.status == 401


def test_graphql_401_and_unauthorized_after_a_good_sign_in_are_authentication_failures():
    fake = FakeMyAir()
    fake.graphql_http = 401
    exc = run_expecting(fake)
    assert type(exc) is feeder.AuthFailure and exc.status == 401

    fake2 = FakeMyAir()
    fake2.graphql_errors = [{"message": SENTINEL, "errorInfo": {"errorType": "unauthorized",
                                                                  "errorCode": "x"}}]
    exc = run_expecting(fake2)
    assert type(exc) is feeder.AuthFailure and exc.code == "unauthorized"
    assert SENTINEL not in str(exc)


@pytest.mark.parametrize("knob,value", [
    ("authn_http", 500), ("authn_http", 503), ("authn_http", 429), ("authn_http", 403),
    ("token_http", 500), ("token_http", 400), ("graphql_http", 500), ("graphql_http", 403),
    ("authorize_http", 200), ("authorize_http", 500)])
def test_other_failures_are_plain_source_failures_never_authentication(knob, value):
    fake = FakeMyAir()
    setattr(fake, knob, value)
    exc = run_expecting(fake)
    assert not isinstance(exc, feeder.AuthFailure), (knob, value)
    assert SENTINEL not in str(exc)


def test_graphql_errors_that_are_not_unauthorized_are_plain_failures_without_the_servers_words():
    fake = FakeMyAir()
    fake.graphql_errors = [{"message": SENTINEL, "errorInfo": {"errorType": "badRequest",
                                                                 "errorCode": "equipmentNotAssigned"}}]
    exc = run_expecting(fake)
    assert not isinstance(exc, feeder.AuthFailure) and SENTINEL not in str(exc)


@pytest.mark.parametrize("items", [None, "x", {"a": 1}, [1], ["x"]])
def test_a_response_that_is_not_the_observed_shape_fails_closed(items):
    fake = FakeMyAir()
    fake.items_override = items
    exc = run_expecting(fake)
    assert not isinstance(exc, feeder.AuthFailure)


def test_an_empty_record_list_is_none_not_a_failure():
    fake = FakeMyAir()
    fake.items_override = []
    with serve_myair(fake) as (base, _seen):
        records, unreadable = read_through(fake, base)
    assert records == [] and unreadable == 0


def test_records_with_an_unreadable_start_date_are_counted_and_skipped():
    fake = FakeMyAir()
    fake.items_override = [{"startDate": "2026-10-11T00:00:00", "sleepScore": 82},
                           {"startDate": None, "sleepScore": 80},
                           {"startDate": "2026-10-10", "sleepScore": 79}]
    with serve_myair(fake) as (base, _seen):
        records, unreadable = read_through(fake, base)
    assert unreadable == 2 and [r.score for r in records] == [79]


def test_a_missing_country_in_the_id_token_is_a_plain_failure():
    fake = FakeMyAir()
    fake.country = "not a valid country!"
    exc = run_expecting(fake)
    assert not isinstance(exc, feeder.AuthFailure)


def test_a_dead_port_is_a_plain_source_failure_naming_only_the_exception_type():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()                                  # nothing listens here now
    fake = FakeMyAir()
    with pytest.raises(feeder.SourceFailure) as caught:
        feeder.read_myair({"MYAIR_EMAIL": "a@example.invalid", "MYAIR_PASSWORD": "PASSWORD-SENTINEL"},  # privacy-ok: placeholder
                          NOW, TZ, endpoints=fake.endpoints("http://127.0.0.1:%d" % port))
    assert not isinstance(caught.value, feeder.AuthFailure)
    assert "PASSWORD-SENTINEL" not in str(caught.value) and "a@example" not in str(caught.value)


def test_a_slow_server_times_out_as_a_plain_failure():
    release = threading.Event()
    fake = FakeMyAir()

    def respond(handler):
        release.wait(5)

    with loopback_server(respond) as (base, _seen):
        env = {"MYAIR_EMAIL": "a@example.invalid", "MYAIR_PASSWORD": "x",  # privacy-ok: placeholder
               "SLEEP_TIMEOUT_SECONDS": "1"}
        try:
            with pytest.raises(feeder.SourceFailure) as caught:
                feeder.read_myair(env, NOW, TZ, endpoints=fake.endpoints(base))
        finally:
            release.set()
    assert not isinstance(caught.value, feeder.AuthFailure)


# ── TC-1165 / TC-1167 / TC-1168 : the confirmation, through the cycle ────────

AUTH = feeder.AuthFailure("cpap-myair: okta-authn: HTTP 401", status=401)
NET = feeder.SourceFailure("cpap-myair: okta-authn: URLError")


def published_morning(h, hour=7):
    """A good night on the wall, then 30-minute cycles from 13:30 onward are the tests' business."""
    h.cycle(local(TZ, 2026, 10, 12, hour, 0), [("2026-10-11", 82)])
    return h.last()["observed_at"]


def after(minutes, hour=13, minute=0):
    return local(TZ, 2026, 10, 12, hour, minute) + minutes * 60


def test_one_authentication_failure_does_not_grey_the_gauge():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(Path(tmp))
        stamp = published_morning(h)
        h.cycle(after(0), error=h.f.AuthFailure("cpap-myair: okta-authn: HTTP 401", status=401))
        assert h.last()["value"] == 82 and h.last()["observed_at"] == stamp
        assert h.state()["__auth_failures__"] == {"cpap-myair": 1}
        assert "__auth_failed__" not in h.state()


def test_two_consecutive_authentication_failures_grey_it_with_no_stamp():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(Path(tmp))
        published_morning(h)
        err = h.f.AuthFailure("cpap-myair: okta-authn: HTTP 401", status=401)
        h.cycle(after(0), error=err)
        _posted, _notes, failures = h.cycle(after(30), error=err)
        assert "observed_at" not in h.last()
        assert h.state()["__auth_failures__"] == {"cpap-myair": 2}
        assert h.state()["__auth_failed__"]["auth_failed"] is True
        assert any("CONFIRMED" in line for line in failures)
        # The night's score is still on disk for the recovery to reuse; the marker, not
        # a deletion, is what greys the gauge.
        assert "2026-10-12" in h.state()["nights"]


def test_the_failure_count_survives_a_restart():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(Path(tmp))
        published_morning(h)
        err = h.f.AuthFailure("cpap-myair: okta-authn: HTTP 401", status=401)
        h.cycle(after(0), error=err)                       # count 1, written to the state file
        h.restart()                                        # a new process, nothing in memory
        h.cycle(after(30), error=h.f.AuthFailure("again", status=401))   # reaches 2
        assert "observed_at" not in h.last()
        assert h.state()["__auth_failures__"] == {"cpap-myair": 2}


@pytest.mark.parametrize("between", ["network", "success"])
def test_a_non_authentication_outcome_resets_the_count(between):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(Path(tmp))
        stamp = published_morning(h)
        h.cycle(after(0), error=h.f.AuthFailure("a", status=401))
        if between == "network":
            h.cycle(after(30), error=h.f.SourceFailure("net"))
        else:
            h.cycle(after(30), [("2026-10-11", 82)])
        assert h.state()["__auth_failures__"] == {}
        h.cycle(after(60), error=h.f.AuthFailure("b", status=401))   # 1 again, not 2
        assert h.last()["value"] == 82 and h.last()["observed_at"] == stamp


def test_auth_auth_then_a_network_failure_stays_grey_and_a_success_recovers():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(Path(tmp))
        published_morning(h)
        err = h.f.AuthFailure("a", status=401)
        h.cycle(after(0), error=err)
        h.cycle(after(30), error=err)
        assert "observed_at" not in h.last()
        for step in (60, 90, 120):                           # network blips: STILL grey
            h.cycle(after(step), error=h.f.SourceFailure("net"))
            assert "observed_at" not in h.last(), step
        assert h.state()["__auth_failed__"]["auth_failed"] is True
        assert h.calls == 3                                  # the morning, then two refused sign-ins; the blips never reached myAir: back-off
        h.cycle(after(400), [("2026-10-11", 82)])            # the back-off is over; the login works again
        assert h.last()["value"] == 82 and "observed_at" in h.last()
        assert "__auth_failed__" not in h.state()
        assert h.state()["__auth_failures__"] == {}
        # ...and the stamp is the frozen one: recovery does not rejuvenate the night.
        assert h.state()["nights"]["2026-10-12"]["observed_at"] == local(TZ, 2026, 10, 12, 7, 0)


def test_the_marker_and_the_count_have_their_own_validators():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(Path(tmp))
        now = after(0)
        for counts, expected in (({"cpap-myair": 1}, {"cpap-myair": 1}), ({"cpap-myair": 0}, {}),
                                 ({"cpap-myair": True}, {}), ({"cpap-myair": 1.0}, {}),
                                 ({"cpap-myair": "2"}, {}), ({"cpap-myair": 101}, {}),
                                 ({"other": 1}, {}), ("junk", {}), (None, {})):
            h.state_path.write_text(json.dumps({"__auth_failures__": counts}), encoding="utf-8")
            assert h.f.load_state(str(h.state_path), now, TZ)["__auth_failures__"] == expected
        for marker, ok in (({"auth_failed": True, "at": now}, True), ({"auth_failed": False, "at": now}, False),
                           ({"auth_failed": True}, False), ({"auth_failed": True, "at": 5}, False),
                           ({"auth_failed": True, "at": now + 99999}, False), ("x", False)):
            h.state_path.write_text(json.dumps({"__auth_failed__": marker}), encoding="utf-8")
            loaded = h.f.load_state(str(h.state_path), now, TZ)["__auth_failed__"]
            assert (loaded is not None) is ok, marker


def test_the_state_file_holds_counts_and_dates_never_a_credential_or_a_body():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(Path(tmp))
        published_morning(h)
        h.cycle(after(0), error=h.f.AuthFailure("PASSWORD-SENTINEL " + SENTINEL, status=401))
        text = h.state_path.read_text(encoding="utf-8")
        assert "SENTINEL" not in text and SENTINEL not in text


# ── TC-1169 : MFA, and no mailbox ────────────────────────────────────────────

def test_mfa_required_logs_the_person_must_sign_in_line_and_still_posts():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        h = Harness(Path(tmp))
        published_morning(h)
        mfa = h.f.MfaRequired("okta-authn: MFA_REQUIRED", code="MFA_REQUIRED")
        _p, _n, failures = h.cycle(after(0), error=mfa)
        assert any("myAir now asks for an emailed code - a person must sign in "
                   "(see refresh-gauge-logins)" in line for line in failures)
        assert h.last()["value"] == 82                       # first cycle: kept until confirmed
        h.cycle(after(30), error=mfa)
        assert "observed_at" not in h.last()                 # confirmed: unavailable


def test_mfa_required_never_triggers_a_challenge_or_any_other_call():
    """Against the fake: after MFA_REQUIRED the feeder made NO factor-verify call and
    contacted no host but Okta."""
    fake = FakeMyAir()
    fake.authn_status = "MFA_REQUIRED"
    with serve_myair(fake) as (base, seen):
        with pytest.raises(feeder.MfaRequired):
            read_through(fake, base)
    assert [p for _m, p in fake.requests] == ["/oauth2/aus_test/v1/authorize", "/api/v1/authn"]
    assert not any("/factors/" in p or "verify" in p for _m, p in fake.requests)


def test_the_feeder_contains_no_mail_reading_code_at_all():
    """No IMAP/POP/SMTP import, and no call to any mail library, in code (not prose)."""
    tree = ast.parse(Path(feeder.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    for banned in ("imaplib", "poplib", "smtplib", "email", "mailbox", "nntplib"):
        assert banned not in imported, banned
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for banned in ("IMAP4", "IMAP4_SSL", "POP3", "POP3_SSL", "SMTP", "SMTP_SSL"):
        assert banned not in names, banned


# ── TC-1170 : the journal ────────────────────────────────────────────────────

def test_the_remotes_own_words_never_reach_the_journal(capsys):
    """Run the real reader against a fake that shouts SENTINEL in every error body."""
    import tempfile
    for knob, value in (("authn_http", 401), ("authn_http", 500), ("token_http", 401),
                        ("token_http", 500), ("graphql_http", 500)):
        fake = FakeMyAir()
        setattr(fake, knob, value)
        with tempfile.TemporaryDirectory() as tmp, serve_myair(fake) as (base, _seen):
            h = Harness(Path(tmp))
            env = dict(h.env, MYAIR_EMAIL="a@example.invalid", MYAIR_PASSWORD="PASSWORD-SENTINEL")  # privacy-ok: placeholder
            _p, notes, failures = h.f.run_cycle(
                env, now=NOW, tz=TZ, poster=h.poster, sync_check=SYNCED,
                reader=lambda e, n, t: h.f.read_myair(e, n, t, endpoints=fake.endpoints(base)))
        text = "\n".join(notes + failures)
        assert SENTINEL not in text and "PASSWORD-SENTINEL" not in text
        assert "a@example" not in text


# ── TC-1171 : the whole path ─────────────────────────────────────────────────

def test_a_real_run_against_loopback_okta_and_a_loopback_tracker_posts_the_right_gauge(tmp_path, capsys):
    """The real reader, the real poster, the real state file; only the hosts are fakes."""
    fake = FakeMyAir()
    fake.records = RECORDS

    def tracker(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "2")
        handler.end_headers()
        handler.wfile.write(b"{}")

    state = tmp_path / "state"
    state.mkdir()
    with serve_myair(fake) as (base, _seen), loopback_server(tracker) as (feed, feed_seen):
        env = {"SLEEP_ENABLED": "true", "SLEEP_USER": "household-member-1", "SLEEP_FEED_URL": feed + "/api/feed",
               "SLEEP_FEED_TOKEN": "tracker-token-1", "TIMEZONE": "Etc/UTC",
               "MYAIR_EMAIL": "sleeper@example.invalid", "MYAIR_PASSWORD": "pass-word-1",  # privacy-ok: placeholder
               "STATE_DIRECTORY": str(state), "_identity": "household-member-1", "_feed_url": feed + "/api/feed",
               "_tz": feeder.dt.timezone.utc}
        posted, notes, failures = feeder.run_cycle(
            env, now=utc(2026, 10, 12, 10, 30), tz=env["_tz"], sync_check=SYNCED,
            reader=lambda e, n, t: feeder.read_myair(e, n, t, endpoints=fake.endpoints(base)))
    assert failures == [] and fake.problems == []
    assert posted == [("2026-10-12", True)]
    assert len(feed_seen) == 1
    sent = feed_seen[0]
    assert sent["path"] == "/api/feed"
    assert sent["headers"]["X-Forwarded-User"] == "household-member-1"
    assert sent["headers"]["Authorization"] == "Bearer tracker-token-1"
    body = json.loads(sent["body"].decode("utf-8"))
    assert body["value"] == 82 and body["id"] == "sleep" and body["unit"] == "pts"
    assert body["observed_at"] == "2026-10-12T10:30:00+00:00"
    assert ACCESS_TOKEN not in sent["body"].decode("utf-8")
    assert DEVICE_COOKIE not in json.dumps(sent["headers"])
    assert "published night 2026-10-12" in notes


def test_a_transport_exceptions_own_text_never_reaches_a_failure_message():
    """An exception raised under urllib can carry a URL (with the session token in
    its query) or a body. Only the exception TYPE may reach the message."""
    class LeakyOpener(object):
        def open(self, request, timeout=None):
            raise RuntimeError("LEAK " + request.full_url + " " + "PASSWORD-SENTINEL-0042")

    endpoints = feeder.NA_ENDPOINTS
    with pytest.raises(feeder.SourceFailure) as caught:
        feeder._request(LeakyOpener(), endpoints, "okta-authorize",
                        "https://resmed-ext-1.okta.com/x?sessionToken=SESSION-SENTINEL", 5)
    message = str(caught.value)
    assert "RuntimeError" in message
    assert "LEAK" not in message and "PASSWORD-SENTINEL" not in message
    assert "SESSION-SENTINEL" not in message and "sessionToken" not in message
