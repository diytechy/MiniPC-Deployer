"""Shared fixtures for the sleep-feeder tests (tests/test_sleep_*.py). No tests here.

It is named test_sleep_*.py only so it lives with the tests it serves; pytest
collects it and finds nothing to run.

THREE THINGS LIVE HERE, each chosen so the tests assert against the OTHER side
of a contract and not against a value invented in the same file:

  * `UsCentral`, a hand-written US Central time zone. The dev PC's Python 3.8
    has no zoneinfo, and a test that built its expected instants with the same
    zone object the feeder uses would prove nothing about DST. Every DST
    expectation in the tests is therefore written as a literal UTC time
    (`utc(...)`), and `UsCentral` is itself checked against `zoneinfo` where
    that exists.
  * `Harness`, one feeder cycle at a time against a temp state directory with a
    fake reader and a fake poster - for the data-contract branches.
  * `FakeMyAir`, a loopback stand-in for Okta and the AppSync GraphQL endpoint,
    built from the shapes the S0b probe observed on 2026-10-11 (plan 4.4). It
    checks the PKCE challenge against the verifier, the cookie the first call
    hands out, and the bearer token the third call returns - so a sign-in that
    is wrong fails HERE, not on the hub.
"""

import base64
import calendar
import datetime as dt
import hashlib
import importlib.util
import json
import sys
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "stack" / "sleep" / "sleep_feeder.py"


def load_feeder(name="sleep_feeder_under_test"):
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


feeder = load_feeder()


# ── Time ─────────────────────────────────────────────────────────────────────

def utc(year, month, day, hour=0, minute=0, second=0):
    """A literal UTC instant as epoch seconds. The independent side of every DST test."""
    return calendar.timegm((year, month, day, hour, minute, second))


class UsCentral(dt.tzinfo):
    """US Central time, 2007+ rules: CST = UTC-6, CDT = UTC-5.

    DST runs from 02:00 local standard on the second Sunday of March (08:00Z) to
    02:00 local daylight on the first Sunday of November (07:00Z). Only the
    unambiguous parts matter to the feeder (noon, 14:00, the local date).
    """

    _STD = dt.timedelta(hours=-6)
    _HOUR = dt.timedelta(hours=1)

    @staticmethod
    def _sunday_on_or_after(year, month, day):
        d = dt.datetime(year, month, day)
        return d + dt.timedelta(days=(6 - d.weekday()) % 7)

    def _range_naive(self, year):
        return (self._sunday_on_or_after(year, 3, 8) + dt.timedelta(hours=2),
                self._sunday_on_or_after(year, 11, 1) + dt.timedelta(hours=2))

    def dst(self, d):
        start, end = self._range_naive(d.year)
        return self._HOUR if start <= d.replace(tzinfo=None) < end else dt.timedelta(0)

    def utcoffset(self, d):
        return self._STD + self.dst(d)

    def tzname(self, d):
        return "CDT" if self.dst(d) else "CST"

    def fromutc(self, d):
        naive = d.replace(tzinfo=None)
        start, end = self._range_naive(naive.year)
        start_utc = start - self._STD                       # 02:00 CST
        end_utc = end - (self._STD + self._HOUR)            # 02:00 CDT
        offset = self._STD + (self._HOUR if start_utc <= naive < end_utc else dt.timedelta(0))
        return (naive + offset).replace(tzinfo=self)


CENTRAL = UsCentral()


def timezones():
    """The zones every time-dependent test runs under: the hand-written one, plus
    the real tz database's when this Python has it (3.9+ with tzdata)."""
    zones = [("UsCentral", CENTRAL)]
    try:
        import zoneinfo
        zones.append(("zoneinfo", zoneinfo.ZoneInfo("America/Chicago")))
    except Exception:
        pass
    return zones


def local(tz, year, month, day, hour=0, minute=0):
    """Epoch seconds of a LOCAL wall-clock time that is not in a DST gap or fold."""
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=tz).timestamp())


# ── One feeder cycle at a time ───────────────────────────────────────────────

class Harness(object):
    """A temp state directory, a fake poster and a scripted reader."""

    def __init__(self, tmp_path, tz=CENTRAL, module=None):
        self.f = module or feeder
        self.tz = tz
        state_dir = Path(str(tmp_path)) / "state"
        state_dir.mkdir(exist_ok=True)
        self.state_dir = state_dir
        self.state_path = state_dir / "sleep-state.json"
        self.env = {
            "_identity": "household-member-1",
            "_feed_url": "http://127.0.0.1:1/api/feed",
            "SLEEP_STATE_FILE": str(self.state_path),
            "STATE_DIRECTORY": str(state_dir),
        }
        self.posts = []
        self.calls = 0

    def poster(self, body, url, env, timeout):
        self.posts.append(body)
        return True, "HTTP 200"

    def failing_poster(self, body, url, env, timeout):
        self.posts.append(body)
        return False, "HTTP 500 (body not logged)"

    def cycle(self, now, records=None, error=None, poster=None, gated=False, clock=None,
              retry_now=False):
        """Run one cycle. `records` is [(startDate, sleepScore)], `error` an exception.

        By default the fetch windows are IGNORED (`force_fetch`), so the data-contract
        tests can run a cycle at any hour; `gated=True` runs the real schedule and
        back-off. `self.calls` counts how many times myAir was actually asked.
        `clock` is the injected "time after the fetch" (default: `now`).
        """
        f = self.f

        def reader(env, now_, tz_):
            self.calls += 1
            if error is not None:
                raise error
            return [f.Record(f.parse_night_date(start), score) for start, score in records], 0

        return f.run_cycle(dict(self.env), now=now, tz=self.tz, reader=reader,
                           poster=poster or self.poster, clock=clock,
                           force_fetch=not gated, retry_now=retry_now)

    def last(self):
        return self.posts[-1]

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def restart(self):
        """A fresh process: a fresh module object, so nothing survives in memory."""
        self.f = load_feeder("sleep_feeder_restarted")


# ── A loopback Okta + AppSync ────────────────────────────────────────────────

UNSET = object()
ACCESS_TOKEN = "ACCESS-TOKEN-SENTINEL-4471"
SESSION_TOKEN = "SESSION-TOKEN-SENTINEL-2290"
AUTH_CODE = "AUTHCODE-SENTINEL-7715"
DEVICE_COOKIE = "DT-COOKIE-SENTINEL-5538"
ID_TOKEN_SECRET = "ID-TOKEN-SIGNATURE-SENTINEL-6602"


def b64url(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def fake_id_token(country="US"):
    header = b64url(json.dumps({"alg": "none"}).encode())
    claims = b64url(json.dumps({"myAirCountryId": country, "sub": "x"}).encode())
    return "%s.%s.%s" % (header, claims, ID_TOKEN_SECRET)


class FakeMyAir(object):
    """Okta + AppSync on one loopback port, driven by plain attributes.

    Attributes a test may set (defaults give a clean NA sign-in):
      email, password         what authn accepts;
      authn_http, authn_status the authn reply (HTTP code / body `status`);
      authorize_http          302 for the real answer;
      token_http, graphql_http, graphql_errors, records (list of GraphQL items).
    """

    def __init__(self, email="sleeper@example.invalid", password="pass-word-1"):  # privacy-ok: placeholder
        self.email = email
        self.password = password
        self.authn_http = 200
        self.authn_status = "SUCCESS"
        self.authorize_http = 302
        self.token_http = 200
        self.graphql_http = 200
        self.graphql_errors = None
        self.records = []
        self.items_override = UNSET
        self.country = "US"
        self.problems = []              # contract violations the fake saw
        self.challenge = None
        self.requests = []              # (method, path) in arrival order
        self.redirect_uri = "https://myair.example.invalid"
        self.set_cookie_on_prime = True

    def endpoints(self, base):
        return feeder.Endpoints(okta_base=base, auth_server_id="aus_test", client_id="client-test",
                                redirect_uri=self.redirect_uri, graphql_url=base + "/graphql",
                                api_key="api-key-test", product="myAir", https_only=False)

    # -- reply helpers --
    @staticmethod
    def _send(handler, code, body=None, headers=None):
        raw = b"" if body is None else json.dumps(body).encode("utf-8")
        handler.send_response(code)
        for key, value in (headers or []):
            handler.send_header(key, value)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    def __call__(self, handler):
        parts = urllib.parse.urlsplit(handler.path)
        path, query = parts.path, urllib.parse.parse_qs(parts.query)
        # The base server already read the body into `seen`; `serve_myair` hands it over.
        body = self._last_body
        self.requests.append((handler.command, path))
        if path == "/oauth2/aus_test/v1/authorize" and "sessionToken" not in query:
            headers = []
            if self.set_cookie_on_prime:
                headers = [("Set-Cookie", "DT=%s; Path=/; Secure; HttpOnly" % DEVICE_COOKIE),
                           ("Set-Cookie", "JSESSIONID=ignored; Path=/")]
            return self._send(handler, 400, {"error": "invalid_request"}, headers)
        if path == "/api/v1/authn":
            return self._authn(handler, body)
        if path == "/oauth2/aus_test/v1/authorize":
            return self._authorize(handler, query)
        if path == "/oauth2/aus_test/v1/token":
            return self._token(handler, body)
        if path == "/graphql":
            return self._graphql(handler, body)
        self.problems.append("unexpected request %s" % path)
        return self._send(handler, 404, {})

    _last_body = b""

    def _authn(self, handler, body):
        if DEVICE_COOKIE not in (handler.headers.get("Cookie") or "") and self.set_cookie_on_prime:
            self.problems.append("authn did not carry the cookie the first call handed out")
        try:
            creds = json.loads(body.decode("utf-8"))
        except ValueError:
            creds = {}
        if self.authn_http != 200:
            return self._send(handler, self.authn_http,
                              {"errorCode": "E0000004", "errorSummary": "REMOTE-WORDS-MUST-NOT-APPEAR"})
        if creds.get("username") != self.email or creds.get("password") != self.password:
            return self._send(handler, 401, {"errorCode": "E0000004",
                                             "errorSummary": "REMOTE-WORDS-MUST-NOT-APPEAR"})
        if self.authn_status != "SUCCESS":
            return self._send(handler, 200, {"status": self.authn_status, "stateToken": "state-sentinel"})
        return self._send(handler, 200, {"status": "SUCCESS", "sessionToken": SESSION_TOKEN})

    def _authorize(self, handler, query):
        if query.get("sessionToken", [""])[0] != SESSION_TOKEN:
            self.problems.append("authorize did not carry the session token")
        if query.get("code_challenge_method", [""])[0] != "S256":
            self.problems.append("authorize did not ask for S256")
        if query.get("response_mode", [""])[0] != "fragment":
            self.problems.append("authorize did not ask for a fragment")
        self.challenge = query.get("code_challenge", [""])[0]
        if self.authorize_http != 302:
            return self._send(handler, self.authorize_http, {})
        location = "%s#code=%s&state=%s" % (self.redirect_uri, AUTH_CODE,
                                            query.get("state", [""])[0])
        return self._send(handler, 302, None, [("Location", location)])

    def _token(self, handler, body):
        form = urllib.parse.parse_qs(body.decode("ascii"))
        if form.get("code", [""])[0] != AUTH_CODE:
            self.problems.append("token did not carry the code")
        verifier = form.get("code_verifier", [""])[0]
        expected = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        if not verifier or expected != self.challenge:
            self.problems.append("PKCE verifier does not match the challenge")
        if form.get("grant_type", [""])[0] != "authorization_code":
            self.problems.append("token grant_type wrong")
        if self.token_http != 200:
            return self._send(handler, self.token_http, {"error": "REMOTE-WORDS-MUST-NOT-APPEAR"})
        return self._send(handler, 200, {"access_token": ACCESS_TOKEN,
                                         "id_token": fake_id_token(self.country),
                                         "token_type": "Bearer", "expires_in": 3600})

    def _graphql(self, handler, body):
        headers = handler.headers
        if headers.get("Authorization") != "Bearer " + ACCESS_TOKEN:
            self.problems.append("graphql did not carry the bearer token")
        if headers.get("x-api-key") != "api-key-test":
            self.problems.append("graphql did not carry the api key")
        if headers.get("rmdcountry") != self.country:
            self.problems.append("graphql did not carry the country from the id token")
        payload = json.loads(body.decode("utf-8"))
        self.graphql_query = payload.get("query", "")
        if self.graphql_http != 200:
            return self._send(handler, self.graphql_http, {"errors": [{"message": "REMOTE-WORDS-MUST-NOT-APPEAR"}]})
        if self.graphql_errors is not None:
            return self._send(handler, 200, {"data": None, "errors": self.graphql_errors})
        items = self.records if self.items_override is UNSET else self.items_override
        return self._send(handler, 200, {"data": {"getPatientWrapper": {
            "sleepRecords": {"items": items, "__typename": "SleepRecords"}}}})


def serve_myair(fake):
    """loopback_server(...) around a FakeMyAir, with the request body captured.

    `conftest.loopback_server` reads the body itself and puts it in `seen`; the
    wrapper hands the fake the latest one before it answers.
    """
    from conftest import loopback_server

    holder = {}

    def respond(handler):
        fake._last_body = holder["seen"][-1]["body"]
        fake(handler)

    manager = loopback_server(respond)

    class Wrapper(object):
        def __enter__(self_inner):
            base, seen = manager.__enter__()
            holder["seen"] = seen
            return base, seen

        def __exit__(self_inner, *exc):
            return manager.__exit__(*exc)

    return Wrapper()


def sub(tmp_path, name, tz=CENTRAL):
    """A second, independent Harness in a sub-directory of `tmp_path`."""
    path = Path(str(tmp_path)) / name
    path.mkdir()
    return Harness(path, tz)
