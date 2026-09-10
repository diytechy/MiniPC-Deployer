"""TC-006 — the two Owner-run tools that clear the weight feeder's vendor half.

`stack/weight/weight_oauth.py` mints the Google Health refresh token and
captures ONE real response body. It handles a credential that grants blood
glucose, body fat, oxygen saturation, core body temperature and heart-rate
metrics as well as weight, so the two properties that matter are asserted
first and by behaviour rather than by inspection:

  A. NOTHING SECRET IS PRINTED. The whole flow is run against a fake Google
     whose every field is a distinct sentinel string, and the captured output
     is grepped for each one - on the success path AND on the failure paths,
     because "the error message leaked it" is how this normally happens.
  B. THE TOKEN WRITE IS THE FEEDER'S GUARD, sign-reversed: one allow-listed
     path, resolved with `realpath`, contained in the service's own
     StateDirectory, opened unlink-then-O_CREAT|O_EXCL|O_NOFOLLOW at 0600. A
     symlink planted at the token path must not truncate what it points at.

Plus the standard this build runs on, restated for this file: NO PARSER MAY
EXIST FOR A VENDOR CALL THAT HAS NEVER BEEN MADE. The feeder's test asserts
that against `weight_feeder`; a parser could just as easily be smuggled in
here, so the same assertion is made against this module.

WHAT IS NOT ASSERTED HERE, AND CANNOT BE. No test in this file has ever spoken
to Google. Every "Google" below is a loopback HTTP server this file starts. The
consent screen, the real authorization code, the real token exchange, the real
`dataPoints.list` response and the real 401/403 shapes are UNEXERCISED, and the
capture tool exists precisely because they must be seen by a human before
anybody writes a parser.

Verifies: TC-006 (SR-022, LLR-006)
"""

import ast
import importlib.util
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from conftest import loopback_server
from test_feeder_egress_parity import code_lines

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "stack" / "weight" / "weight_oauth.py"

_spec = importlib.util.spec_from_file_location("weight_oauth", MODULE_PATH)
oauth = importlib.util.module_from_spec(_spec)
sys.modules["weight_oauth"] = oauth
_spec.loader.exec_module(oauth)

NOW = 1789000000

# Every credential-shaped value the flow touches gets its own sentinel, so a
# leak names WHICH secret leaked rather than just "something did".
CLIENT_ID = "sentinel-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "CLIENT-SECRET-SENTINEL"
AUTH_CODE = "AUTH-CODE-SENTINEL"
REFRESH_TOKEN = "REFRESH-TOKEN-SENTINEL"
ACCESS_TOKEN = "ACCESS-TOKEN-SENTINEL"
BODY_SENTINEL = "BODY-WEIGHT-SENTINEL"

SECRETS = (CLIENT_SECRET, AUTH_CODE, REFRESH_TOKEN, ACCESS_TOKEN)


def write_env(tmp_path, client=True, **overrides):
    """A `.env` in the shape stack/.env really has: our keys among others.

    `client=False` writes a file with NEITHER client pair in it, which is what
    a box that was never provisioned for this looks like.
    """
    values = {}
    if client:
        values.update({"OAUTH2_PROXY_CLIENT_ID": CLIENT_ID,
                       "OAUTH2_PROXY_CLIENT_SECRET": CLIENT_SECRET})
    values.update(overrides)
    lines = ["# a comment", "TECHNITIUM_ADMIN_PASSWORD=NOT-OURS-DO-NOT-READ"]
    lines += ["%s=%s" % (key, value) for key, value in values.items()]
    path = tmp_path / "dot.env"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def json_responder(status, payload):
    """A loopback responder that answers with JSON (or raw bytes) and a status."""
    def respond(handler):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    return respond


def mint_args(env_file, token_file, state_root, extra=()):
    return oauth.build_parser().parse_args(
        ["mint", "--env-file", env_file, "--token-file", token_file,
         "--state-root", state_root] + list(extra))


def capture_args(env_file, token_file, state_root, out, extra=()):
    return oauth.build_parser().parse_args(
        ["capture", "--env-file", env_file, "--token-file", token_file,
         "--state-root", state_root, "--out", out] + list(extra))


def paste_of(code, state):
    """What the Owner really pastes: the whole address-bar URL of a dead page."""
    return "http://localhost:8117/?state=%s&code=%s&scope=x" % (state, code)


# ── A. Nothing secret is printed ────────────────────────────────────────────

def test_a_whole_mint_prints_no_secret_no_code_and_no_token_sr022(tmp_path):
    """THE PROPERTY THAT MATTERS, end to end against a fake Google.

    The client secret, the authorization code, the access token and the
    refresh token each get a sentinel, and every byte this tool wrote to the
    terminal is searched for all four. This is behavioural on purpose: a
    structural "no print(token)" check passes on a print of an f-string that
    interpolates it.
    """
    env_file = write_env(tmp_path)
    state_root = str(tmp_path / "state")
    token_file = str(tmp_path / "state" / "tokens" / "google-health-token.json")
    out = io.StringIO()
    seen_state = {}

    def prompt(_text):
        # The state is generated inside mint(); read it back off the URL it
        # printed, exactly as the Owner reads it out of the browser.
        printed = out.getvalue()
        seen_state["state"] = printed.split("state=")[1].split("&")[0]
        return paste_of(AUTH_CODE, seen_state["state"])

    with loopback_server(json_responder(200, {"refresh_token": REFRESH_TOKEN,
                                              "access_token": ACCESS_TOKEN,
                                              "scope": oauth.SCOPE,
                                              "token_type": "Bearer"})) as (token_url, seen):
        rc = oauth.mint(mint_args(env_file, token_file, state_root), out=out,
                        prompt=prompt, token_endpoint=token_url + "/token",
                        now=NOW)

    assert rc == 0
    printed = out.getvalue()
    for secret in SECRETS:
        assert secret not in printed, "mint printed %s" % secret
    # ...and the exchange really happened, so the absence above is not the
    # absence of a flow.
    assert len(seen) == 1
    sent = seen[0]["body"].decode("ascii")
    assert "code=" + AUTH_CODE in sent and "code_verifier=" in sent
    assert "grant_type=authorization_code" in sent

    stored = json.loads(Path(token_file).read_text(encoding="utf-8"))
    assert stored["refresh_token"] == REFRESH_TOKEN
    assert CLIENT_SECRET not in json.dumps(stored)
    assert ACCESS_TOKEN not in json.dumps(stored)


def test_a_failed_exchange_reports_the_status_and_leaks_nothing_sr022(tmp_path):
    """The failure path, which is where a leak normally happens.

    Google's token endpoint is made to answer 400 with a body that quotes the
    code and the client secret back - a real hazard, since `error_description`
    has been observed quoting the request. The refusal must carry the status
    and the short `error` enum, and neither sentinel.
    """
    env_file = write_env(tmp_path)
    state_root = str(tmp_path / "state")
    token_file = str(tmp_path / "state" / "tokens" / "google-health-token.json")
    hostile = {"error": "invalid_grant",
               "error_description": "code %s for secret %s is expired"
                                    % (AUTH_CODE, CLIENT_SECRET)}
    with loopback_server(json_responder(400, hostile)) as (token_url, _seen):
        with pytest.raises(oauth.Refused) as err:
            oauth.mint(mint_args(env_file, token_file, state_root),
                       out=io.StringIO(),
                       prompt=lambda _t: AUTH_CODE,
                       token_endpoint=token_url + "/token", now=NOW)
    message = str(err.value)
    assert "HTTP 400" in message and "invalid_grant" in message
    for secret in SECRETS:
        assert secret not in message
    assert not os.path.exists(token_file), "a failed exchange wrote a token file"


def test_only_an_enum_shaped_error_is_ever_repeated_sr022():
    """`error` is repeated through an ALLOW-LIST, not a sanitiser.

    The input is a remote's chosen bytes, so the question is what we are
    willing to repeat. A body whose `error` field is a sentence - or a token -
    is not repeated at all.
    """
    assert oauth.error_code_in(json.dumps({"error": "invalid_grant"})) == "invalid_grant"
    assert oauth.error_code_in(json.dumps({"error": {"status": "PERMISSION_DENIED"}})) \
        == "PERMISSION_DENIED"
    assert oauth.error_code_in(json.dumps({"error": "ya29.%s" % ACCESS_TOKEN})) is None
    assert oauth.error_code_in(json.dumps({"error": "the code %s expired" % AUTH_CODE})) is None
    assert oauth.error_code_in("<html>not json</html>") is None
    assert oauth.error_code_in(json.dumps({"error_description": "invalid_grant"})) is None


def test_no_refusal_message_ever_quotes_the_paste_sr022():
    """A paste may BE a live authorization code, and a message gets printed."""
    for paste in ("http://localhost:8117/?state=other&code=" + AUTH_CODE,
                  AUTH_CODE + " trailing words",
                  "http://localhost:8117/?error=access_denied&code=" + AUTH_CODE):
        with pytest.raises(oauth.Refused) as err:
            oauth.code_from_paste(paste, "expected-state")
        assert AUTH_CODE not in str(err.value)


def test_the_broad_catches_name_only_the_exception_type_sr022():
    """B7's rule, asserted on this module's own AST.

    An exception raised inside urllib carries the request object, so its
    message can contain an `Authorization` header. The parity test makes this
    assertion about the two feeders; this file is the third module that talks
    to a vendor and it is not in that test's list.
    """
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    handlers = [node for node in ast.walk(tree)
                if isinstance(node, ast.ExceptHandler)
                and isinstance(node.type, ast.Name)
                and node.type.id == "Exception"
                and node.name]
    assert handlers, "weight_oauth has no named broad catch to check"
    for handler in handlers:
        blessed = set()
        for node in ast.walk(handler):
            if isinstance(node, ast.Attribute) and node.attr == "__name__" \
                    and isinstance(node.value, ast.Call) \
                    and isinstance(node.value.func, ast.Name) \
                    and node.value.func.id == "type":
                blessed.update(id(arg) for arg in node.value.args)
        leaked = [node.lineno for node in ast.walk(handler)
                  if isinstance(node, ast.Name) and node.id == handler.name
                  and id(node) not in blessed]
        assert leaked == [], (
            "the broad catch at line %d uses the exception itself at line(s) %s; "
            "only type(exc).__name__ may be reported." % (handler.lineno, leaked))


def test_the_minting_tools_do_not_reach_for_the_default_opener_sr022():
    """The egress defect, in source form, for the module the parity test does
    not cover. urllib's default opener follows redirects (carrying
    `Authorization` across hosts) and honours `http_proxy`."""
    offenders = [number for number, line in enumerate(code_lines(MODULE_PATH), 1)
                 if "urlopen(" in line]
    assert offenders == [], (
        "weight_oauth calls urlopen at line(s) %s; use "
        "weight_feeder.vendor_opener()." % offenders)


def test_the_outbound_calls_refuse_a_redirect_and_a_proxy_sr022(monkeypatch):
    """The behavioural half: a 302 must not re-send the exchange elsewhere, and
    an exported proxy must not see it."""
    def redirect(target):
        def respond(handler):
            handler.send_response(302)
            handler.send_header("Location", target)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
        return respond

    with loopback_server(json_responder(200, {"refresh_token": REFRESH_TOKEN})) \
            as (elsewhere, arrived):
        with loopback_server(redirect(elsewhere + "/token")) as (front, _seen):
            for variable in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
                monkeypatch.setenv(variable, elsewhere)
            with pytest.raises(oauth.Refused) as err:
                oauth.post_form(front + "/token",
                                {"client_secret": CLIENT_SECRET}, 10,
                                "the token exchange")
        assert arrived == [], "the exchange was re-sent to the redirect target"
    assert CLIENT_SECRET not in str(err.value)


# ── B. The token write is the feeder's guard, sign-reversed ─────────────────

def test_the_token_write_is_bound_to_the_services_state_directory_sr022(tmp_path):
    """The allow-list, the containment bound, and the state-file collision.

    A knob is a knob: WEIGHT_TOKEN_FILE naming somebody's `~/.ssh/id_ed25519`
    must be refused by the guard, not by the person editing the file.
    """
    root = tmp_path / "state"
    (root / "tokens").mkdir(parents=True)
    token = str(root / "tokens" / "google-health-token.json")
    state = str(root / "weight-state.json")

    assert oauth.token_write_verdict(token, token, state, str(root)) is None

    outside = str(tmp_path / "home" / ".ssh" / "id_ed25519")
    os.makedirs(os.path.dirname(outside))
    reason = oauth.token_write_verdict(outside, outside, state, str(root))
    assert reason and "state directory" in reason

    other = str(root / "tokens" / "somewhere-else.json")
    assert oauth.token_write_verdict(other, token, state, str(root))
    assert oauth.token_write_verdict(state, state, state, str(root))


def test_the_token_verdict_resolves_symlinks_rather_than_strings_sr022(tmp_path):
    """`abspath` is string arithmetic; a link at the token path pointing at
    another credential would pass a string comparison unchanged."""
    root = tmp_path / "state"
    token = str(root / "tokens" / "google-health-token.json")
    state = str(root / "weight-state.json")
    victim = str(tmp_path / "drive-token.json")

    def resolve_like_a_symlink(path):
        return victim if path == token else os.path.abspath(path)

    assert oauth.token_write_verdict(token, token, state, str(root),
                                     resolve=os.path.abspath) is None
    assert oauth.token_write_verdict(token, token, state, str(root),
                                     resolve=resolve_like_a_symlink)


def test_a_symlink_at_the_token_path_pointing_out_of_the_root_is_refused_sr022(tmp_path):
    """A link planted at the token path, aimed at another credential.

    The verdict resolves before it decides, so this never reaches an open at
    all: the resolved target is outside the service's state directory and the
    write is refused with the link - and its target - untouched.
    """
    root = tmp_path / "state"
    (root / "tokens").mkdir(parents=True)
    token = root / "tokens" / "google-health-token.json"
    victim = tmp_path / "tracker-drive-token.json"
    victim.write_text('{"refresh_token":"KEEP-ME"}', encoding="utf-8")
    try:
        os.symlink(str(victim), str(token))
    except (OSError, NotImplementedError):      # pragma: no cover - platform
        pytest.skip("this filesystem/account cannot create symlinks")

    env_file = write_env(tmp_path)
    with pytest.raises(oauth.Refused) as err:
        oauth.mint(mint_args(env_file, str(token), str(root), extra=["--force"]),
                   out=io.StringIO(), prompt=lambda _t: AUTH_CODE, now=NOW)
    assert "state directory" in str(err.value)
    assert victim.read_text(encoding="utf-8") == '{"refresh_token":"KEEP-ME"}'


def test_a_symlink_inside_the_root_is_destroyed_not_followed_sr022(tmp_path):
    """The check-then-open race, against the real filesystem.

    A link whose target is itself inside the state root passes the verdict -
    both sides resolve to the same file - so this is the case where the OPEN
    has to be the guard. `open_no_follow` unlinks first, which destroys a
    planted LINK and never the file it points at, then creates
    O_CREAT|O_EXCL|O_NOFOLLOW.
    """
    root = tmp_path / "state"
    (root / "tokens").mkdir(parents=True)
    token = root / "tokens" / "google-health-token.json"
    victim = root / "tokens" / "another-token.json"
    victim.write_text('{"refresh_token":"KEEP-ME"}', encoding="utf-8")
    try:
        os.symlink(str(victim), str(token))
    except (OSError, NotImplementedError):      # pragma: no cover - platform
        pytest.skip("this filesystem/account cannot create symlinks")

    env_file = write_env(tmp_path)
    with loopback_server(json_responder(200, {"refresh_token": REFRESH_TOKEN}))             as (token_url, _seen):
        oauth.mint(mint_args(env_file, str(token), str(root), extra=["--force"]),
                   out=io.StringIO(), prompt=lambda _t: AUTH_CODE,
                   token_endpoint=token_url + "/token", now=NOW)

    assert victim.read_text(encoding="utf-8") == '{"refresh_token":"KEEP-ME"}'
    assert not token.is_symlink()
    assert json.loads(token.read_text(encoding="utf-8"))["refresh_token"] == REFRESH_TOKEN


def test_mint_refuses_to_overwrite_an_existing_token_without_force_sr022(tmp_path):
    """And it refuses BEFORE consent: an authorization code is single-use, so
    discovering this afterwards costs a second trip through a browser."""
    env_file = write_env(tmp_path)
    root = tmp_path / "state"
    (root / "tokens").mkdir(parents=True)
    token = root / "tokens" / "google-health-token.json"
    token.write_text('{"refresh_token":"ALREADY-HERE"}', encoding="utf-8")

    def must_not_be_called(_text):      # pragma: no cover - the point is it is not
        raise AssertionError("mint sent the Owner to a browser before refusing")

    with pytest.raises(oauth.Refused) as err:
        oauth.mint(mint_args(env_file, str(token), str(root)), out=io.StringIO(),
                   prompt=must_not_be_called, now=NOW)
    assert "--force" in str(err.value)
    assert token.read_text(encoding="utf-8") == '{"refresh_token":"ALREADY-HERE"}'


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_the_minted_token_file_is_0600_sr022(tmp_path):     # pragma: no cover - posix
    env_file = write_env(tmp_path)
    root = tmp_path / "state"
    token = str(root / "tokens" / "google-health-token.json")
    with loopback_server(json_responder(200, {"refresh_token": REFRESH_TOKEN})) \
            as (token_url, _seen):
        oauth.mint(mint_args(env_file, token, str(root)), out=io.StringIO(),
                   prompt=lambda _t: AUTH_CODE,
                   token_endpoint=token_url + "/token", now=NOW)
    assert stat.S_IMODE(os.stat(token).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(token)).st_mode) == 0o700


def test_the_token_document_holds_the_token_and_nothing_else_secret_sr022():
    """A second copy of the client secret is a second thing to rotate - the
    lesson the tracker's Drive sync already paid for. The access token is
    excluded for a different reason: it is worthless in an hour."""
    document = oauth.token_document(
        {"refresh_token": REFRESH_TOKEN, "access_token": ACCESS_TOKEN,
         "scope": oauth.SCOPE, "expires_in": 3599}, NOW)
    text = json.dumps(document)
    assert REFRESH_TOKEN in text
    assert ACCESS_TOKEN not in text
    assert CLIENT_SECRET not in text
    assert "client_secret" not in document
    assert document["scope"] == oauth.SCOPE
    assert document["minted_at"] == "2026-09-10T00:26:40Z"


def test_a_reply_with_no_refresh_token_writes_nothing_sr022(tmp_path):
    """Google omits the refresh token on a re-consent it treats as already
    granted. A file holding only an access token looks minted, works for an
    hour and then fails mysteriously."""
    with pytest.raises(oauth.Refused) as err:
        oauth.token_document({"access_token": ACCESS_TOKEN}, NOW)
    assert "refresh token" in str(err.value)
    assert ACCESS_TOKEN not in str(err.value)


# ── The capture: one call, body saved, body never printed ───────────────────

def prepared_token(tmp_path):
    root = tmp_path / "state"
    (root / "tokens").mkdir(parents=True)
    token = root / "tokens" / "google-health-token.json"
    token.write_text(json.dumps({"refresh_token": REFRESH_TOKEN}), encoding="utf-8")
    return root, token


def test_capture_makes_exactly_one_list_call_and_never_prints_the_body_sr022(tmp_path):
    """The body is a real body weight with a timestamp. What reaches the
    terminal is the status, the byte count and a digest."""
    env_file = write_env(tmp_path)
    root, token = prepared_token(tmp_path)
    out_file = str(tmp_path / "captured-body.json")
    body = json.dumps({"dataPoint": [{"weight": {"weightGrams": 86800.0,
                                                 "notes": BODY_SENTINEL}}]}).encode()

    with loopback_server(json_responder(200, {"access_token": ACCESS_TOKEN})) \
            as (token_url, token_seen):
        with loopback_server(json_responder(200, body)) as (list_url, list_seen):
            out = io.StringIO()
            rc = oauth.capture(capture_args(env_file, str(token), str(root), out_file),
                               out=out, list_url=list_url + "/v4/list",
                               token_endpoint=token_url + "/token")

    assert rc == 0
    assert len(list_seen) == 1, "capture made %d list calls" % len(list_seen)
    assert len(token_seen) == 1
    assert list_seen[0]["headers"]["Authorization"] == "Bearer " + ACCESS_TOKEN
    assert Path(out_file).read_bytes() == body

    printed = out.getvalue()
    assert "HTTP 200" in printed and str(len(body)) in printed
    assert BODY_SENTINEL not in printed, "capture printed the health data"
    for secret in SECRETS:
        assert secret not in printed


def test_capture_reports_a_failure_without_the_body_or_the_token_sr022(tmp_path):
    """A 403 is the likely first answer (API not enabled, or scope not on the
    consent screen), and the Owner needs the status, not the body."""
    env_file = write_env(tmp_path)
    root, token = prepared_token(tmp_path)
    out_file = str(tmp_path / "captured-body.json")
    hostile = {"error": {"status": "PERMISSION_DENIED",
                         "message": "token %s lacks scope" % ACCESS_TOKEN}}

    with loopback_server(json_responder(200, {"access_token": ACCESS_TOKEN})) \
            as (token_url, _seen):
        with loopback_server(json_responder(403, hostile)) as (list_url, _also):
            with pytest.raises(oauth.Refused) as err:
                oauth.capture(capture_args(env_file, str(token), str(root), out_file),
                              out=io.StringIO(), list_url=list_url + "/v4/list",
                              token_endpoint=token_url + "/token")
    message = str(err.value)
    assert "HTTP 403" in message and "PERMISSION_DENIED" in message
    for secret in SECRETS:
        assert secret not in message
    assert not os.path.exists(out_file), "a failed capture created an output file"


def test_capture_refuses_to_overwrite_and_refuses_a_credential_name_sr022(tmp_path):
    """The Owner names this path, so it cannot be an allow-list - but it can
    refuse the two shapes that would cost a credential."""
    for name in ("google-health-token.json", ".netrc", "credentials.json"):
        assert oauth.capture_output_verdict(str(tmp_path / name))
        # ...and the DOOR refuses it, not only the verdict. A name that does
        # not exist yet is the case O_EXCL cannot catch, which is exactly where
        # a verdict that nobody consulted would survive unnoticed.
        with pytest.raises(oauth.Refused):
            oauth.open_capture_output(str(tmp_path / name))
        assert not (tmp_path / name).exists()
    ordinary = tmp_path / "body.json"
    assert oauth.capture_output_verdict(str(ordinary)) is None
    with oauth.open_capture_output(str(ordinary)) as handle:
        handle.write(b"{}")
    with pytest.raises(oauth.Refused):
        oauth.open_capture_output(str(ordinary))
    assert ordinary.read_bytes() == b"{}"


def test_the_capture_uses_the_route_the_discovery_document_gave_sr022():
    """The prose docs render the read as /v4/users/me/dataPoints/weight and are
    WRONG; the discovery document routes v4/users/{id}/dataTypes/{type}/
    dataPoints. This is the exact class of error the verify-first gate exists
    to catch, so the route is pinned rather than trusted."""
    assert oauth.LIST_URL == (
        "https://health.googleapis.com/v4/users/me/dataTypes/weight/dataPoints")
    assert oauth.SCOPE == (
        "https://www.googleapis.com/auth/googlehealth."
        "health_metrics_and_measurements.readonly")


# ── The flow's pure parts ───────────────────────────────────────────────────

def test_the_authorization_url_asks_for_offline_access_and_the_one_scope_sr022():
    """`access_type=offline` WITH `prompt=consent` is what returns a refresh
    token: Google omits it on a re-consent for a client the account has already
    granted, which is exactly this client (oauth2-proxy signs in with it)."""
    import urllib.parse
    verifier = oauth.new_verifier()
    url = oauth.authorization_url(CLIENT_ID, "http://localhost:8117/", "st8",
                                  oauth.pkce_challenge(verifier))
    fields = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert fields["scope"] == [oauth.SCOPE]
    assert fields["access_type"] == ["offline"]
    assert fields["prompt"] == ["consent"]
    assert fields["response_type"] == ["code"]
    assert fields["redirect_uri"] == ["http://localhost:8117/"]
    assert fields["code_challenge_method"] == ["S256"]
    assert fields["code_challenge"] == [oauth.pkce_challenge(verifier)]
    assert verifier not in url, "the PKCE verifier must never leave the process"


def test_pkce_is_s256_of_the_verifier_sr022():
    """Pinned against the RFC 7636 appendix-B vector, so a base64 or a hashing
    mistake fails here rather than at Google."""
    assert oauth.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") \
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_the_paste_may_be_the_whole_url_or_the_bare_code_sr022():
    """The Owner pastes an address bar. Telling a person to select part of a
    string is how the wrong part gets selected."""
    assert oauth.code_from_paste(paste_of("4/0Aabc", "st8"), "st8") == "4/0Aabc"
    assert oauth.code_from_paste("  4/0Aabc  ", "st8") == "4/0Aabc"
    assert oauth.code_from_paste("?state=st8&code=4/0Aabc", "st8") == "4/0Aabc"


def test_a_state_mismatch_and_a_denial_are_both_refusals_sr022():
    with pytest.raises(oauth.Refused) as stale:
        oauth.code_from_paste(paste_of("4/0Aabc", "somebody-elses"), "st8")
    assert "state" in str(stale.value)
    with pytest.raises(oauth.Refused) as denied:
        oauth.code_from_paste("http://localhost:8117/?error=access_denied", "st8")
    assert "access_denied" in str(denied.value)
    with pytest.raises(oauth.Refused):
        oauth.code_from_paste("", "st8")
    with pytest.raises(oauth.Refused):
        oauth.code_from_paste("http://localhost:8117/?state=st8", "st8")


def test_the_env_file_is_read_key_by_key_and_last_wins_sr022(tmp_path):
    """`stack/.env` also holds TECHNITIUM_ADMIN_PASSWORD, CLOUDFLARE_API_TOKEN
    and the finance credentials. A tool that needs two keys holds two keys -
    and `source`-ing that file into this process is what is being avoided.
    Last-wins matches setup-weight.sh's `grep ... | tail -1`."""
    text = ("# comment\nOAUTH2_PROXY_CLIENT_ID=first\n"
            "TECHNITIUM_ADMIN_PASSWORD=NOT-OURS\n"
            "export OAUTH2_PROXY_CLIENT_SECRET=\"quoted-secret\"\n"
            "OAUTH2_PROXY_CLIENT_ID=second\nnot-an-assignment\n")
    values = oauth.env_values(text)
    assert values == {"OAUTH2_PROXY_CLIENT_ID": "second",
                      "OAUTH2_PROXY_CLIENT_SECRET": "quoted-secret"}


def test_the_exchange_sends_the_verifier_and_the_same_redirect_uri_sr022():
    """`redirect_uri_mismatch` is the most common failure of this flow, and it
    is caused by the two requests disagreeing by a byte."""
    fields = oauth.authorization_code_fields(CLIENT_ID, CLIENT_SECRET, AUTH_CODE,
                                             "verifier", "http://localhost:8117/")
    assert fields["grant_type"] == "authorization_code"
    assert fields["code_verifier"] == "verifier"
    assert fields["redirect_uri"] == "http://localhost:8117/"
    assert oauth.refresh_fields(CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN) == {
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN, "grant_type": "refresh_token"}


def test_a_missing_client_is_a_refusal_that_names_the_key_not_the_value_sr022(tmp_path):
    env_file = write_env(tmp_path, OAUTH2_PROXY_CLIENT_SECRET="")
    root = str(tmp_path / "state")
    token = str(tmp_path / "state" / "tokens" / "google-health-token.json")
    with pytest.raises(oauth.Refused) as err:
        oauth.mint(mint_args(env_file, token, root), out=io.StringIO(),
                   prompt=lambda _t: AUTH_CODE, now=NOW)
    assert "OAUTH2_PROXY_CLIENT_SECRET" in str(err.value)


def test_main_refuses_legibly_and_prints_no_traceback_sr022(tmp_path, capsys):
    """Every failure ends at `main`, which prints the refusal and nothing else:
    a traceback of a urllib failure carries the request object and its headers.
    """
    rc = oauth.main(["mint", "--env-file", str(tmp_path / "absent.env"),
                     "--token-file", str(tmp_path / "t.json"),
                     "--state-root", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 2
    assert "could not read" in captured.err
    assert "Traceback" not in captured.err and "Traceback" not in captured.out


# ── The client comes from the variables the hub REALLY has ─────────────────

def looked_for_clause(message):
    """The fenced list of variables the refusal says it SEARCHED FOR.

    The rest of the paragraph names the oauth2-proxy pair for an unrelated
    reason ("that is the one to reuse"), so searching the whole message would
    pass on a build that had stopped looking for it.
    """
    assert oauth.LOOKED_FOR_PREFIX in message, message
    tail = message.split(oauth.LOOKED_FOR_PREFIX, 1)[1]
    assert oauth.LOOKED_FOR_SUFFIX in tail, message
    return tail.split(oauth.LOOKED_FOR_SUFFIX, 1)[0]


def mint_against_a_fake_google(env_file, token_file, state_root):
    """Run a whole successful `mint` against the loopback fake. Returns the
    stored token document. The state is read back off the printed URL exactly
    as the Owner reads it out of the browser's address bar."""
    out = io.StringIO()

    def prompt(_text):
        state = out.getvalue().split("state=")[1].split("&")[0]
        return paste_of(AUTH_CODE, state)

    with loopback_server(json_responder(200, {"refresh_token": REFRESH_TOKEN,
                                              "access_token": ACCESS_TOKEN,
                                              "scope": oauth.SCOPE,
                                              "token_type": "Bearer"})) as (url, _seen):
        rc = oauth.mint(mint_args(env_file, token_file, state_root), out=out,
                        prompt=prompt, token_endpoint=url + "/token", now=NOW)
    assert rc == 0
    assert CLIENT_SECRET not in out.getvalue()
    return json.loads(Path(token_file).read_text(encoding="utf-8"))


def test_the_oauth2_proxy_pair_alone_is_enough_to_mint_sr022(tmp_path):
    """THE DEFECT THIS TEST EXISTS FOR, observed against the deployed hub on
    2026-09-09: `/opt/homehub/stack/.env` holds `OAUTH2_PROXY_CLIENT_ID` and
    `OAUTH2_PROXY_CLIENT_SECRET` and has NO `TRACKER_DRIVE_CLIENT_ID` and no
    `TRACKER_DRIVE_CLIENT_SECRET` at all. A tool that looked only for the
    tracker names would fail at its very first step, before the Owner ever
    reached a browser. This fixture carries ONLY the oauth2-proxy pair - the
    real hub's shape - and mint must go all the way through.
    """
    env_file = write_env(tmp_path)
    assert "TRACKER_DRIVE_CLIENT" not in Path(env_file).read_text(encoding="utf-8")
    root = str(tmp_path / "state")
    token = str(tmp_path / "state" / "tokens" / "google-health-token.json")
    stored = mint_against_a_fake_google(env_file, token, root)
    assert stored["refresh_token"] == REFRESH_TOKEN
    assert "OAUTH2_PROXY_CLIENT_ID" in stored["client"]


def test_the_resolution_order_is_oauth2_proxy_then_tracker_sr022():
    """The order is a decision, so it is asserted as one rather than inferred
    from whichever pair a fixture happens to carry."""
    both = {"OAUTH2_PROXY_CLIENT_ID": "proxy-id",
            "OAUTH2_PROXY_CLIENT_SECRET": "proxy-secret",
            "TRACKER_DRIVE_CLIENT_ID": "tracker-id",
            "TRACKER_DRIVE_CLIENT_SECRET": "tracker-secret"}
    assert oauth.resolve_client(both) == (
        "OAUTH2_PROXY_CLIENT_ID", "proxy-id", "proxy-secret")

    tracker_only = {"TRACKER_DRIVE_CLIENT_ID": "tracker-id",
                    "TRACKER_DRIVE_CLIENT_SECRET": "tracker-secret"}
    assert oauth.resolve_client(tracker_only) == (
        "TRACKER_DRIVE_CLIENT_ID", "tracker-id", "tracker-secret")

    proxy_only = {"OAUTH2_PROXY_CLIENT_ID": "proxy-id",
                  "OAUTH2_PROXY_CLIENT_SECRET": "proxy-secret"}
    assert oauth.resolve_client(proxy_only) == (
        "OAUTH2_PROXY_CLIENT_ID", "proxy-id", "proxy-secret")

    # HALF A PAIR IS NOT A PAIR. Sliding from a set id to the other pair's
    # secret would send Google a mismatched client and read as its problem.
    half = {"OAUTH2_PROXY_CLIENT_ID": "proxy-id",
            "TRACKER_DRIVE_CLIENT_SECRET": "tracker-secret"}
    with pytest.raises(oauth.Refused):
        oauth.resolve_client(half)


def test_a_box_with_neither_pair_is_refused_naming_all_four_sr022(tmp_path):
    """The message that named only the two variables that do not exist is what
    made this hard to diagnose. It must now name every variable it looked for,
    through the real `mint` entry point and not just the verdict function.

    IT ASSERTS AGAINST THE LOOKED-FOR CLAUSE, NOT THE WHOLE PARAGRAPH. A first
    pass of this test searched the entire message, and passed against a
    deliberately broken build whose search list had been cut back to the
    tracker pair - because a later sentence names the oauth2-proxy pair for a
    different reason. Cutting the fenced clause out is what makes this test
    the thing that catches the defect rather than a bystander.
    """
    env_file = write_env(tmp_path, client=False)
    root = str(tmp_path / "state")
    token = str(tmp_path / "state" / "tokens" / "google-health-token.json")
    with pytest.raises(oauth.Refused) as err:
        oauth.mint(mint_args(env_file, token, root), out=io.StringIO(),
                   prompt=lambda _t: AUTH_CODE, now=NOW)
    looked_for = looked_for_clause(str(err.value))
    for name in ("OAUTH2_PROXY_CLIENT_ID", "OAUTH2_PROXY_CLIENT_SECRET",
                 "TRACKER_DRIVE_CLIENT_ID", "TRACKER_DRIVE_CLIENT_SECRET"):
        assert name in looked_for, (
            "%s is consulted but is not named in the looked-for clause %r"
            % (name, looked_for))
    assert not os.path.exists(token)

    # `capture` reads the same variables, so it refuses the same way.
    with pytest.raises(oauth.Refused) as err2:
        oauth.capture(capture_args(env_file, token, root, str(tmp_path / "body.json")),
                      out=io.StringIO())
    capture_clause = looked_for_clause(str(err2.value))
    for name in ("OAUTH2_PROXY_CLIENT_ID", "OAUTH2_PROXY_CLIENT_SECRET",
                 "TRACKER_DRIVE_CLIENT_ID", "TRACKER_DRIVE_CLIENT_SECRET"):
        assert name in capture_clause


def test_a_fallback_mint_records_the_variable_it_actually_used_sr022(tmp_path):
    """A box that really does carry the tracker pair must not write a token
    file claiming it used oauth2-proxy's client."""
    env_file = write_env(tmp_path, client=False,
                         TRACKER_DRIVE_CLIENT_ID=CLIENT_ID,
                         TRACKER_DRIVE_CLIENT_SECRET=CLIENT_SECRET)
    root = str(tmp_path / "state")
    token = str(tmp_path / "state" / "tokens" / "google-health-token.json")
    stored = mint_against_a_fake_google(env_file, token, root)
    assert "TRACKER_DRIVE_CLIENT_ID" in stored["client"]
    assert CLIENT_SECRET not in json.dumps(stored)


# ── Step 5 is still forbidden, and it is forbidden HERE too ─────────────────

def test_no_google_health_parser_lives_in_the_minting_tools_either_sr022():
    """The feeder's test asserts this against `weight_feeder`. A parser could
    just as easily be smuggled into the module that holds the token, so it is
    asserted here as well - by name, and by the one field name that could only
    come from a parser.

    The reason is the README's: a usage parser that is one field off posts an
    obviously silly percentage; a weight parser that reads kilograms as pounds
    posts a confident, plausible, WRONG body weight, and nothing on the wall
    could tell anyone.
    """
    for name in ("parse_google_health", "parse_weight_datapoint", "parse_weight",
                 "parse_datapoints", "grams_to_pounds"):
        assert not hasattr(oauth, name), (
            "%s exists in weight_oauth, but no authenticated Google Health call "
            "has been made. Capture a body, paste it into the feeder's module "
            "docstring, THEN write the parser." % name)
    offenders = [number for number, line in enumerate(code_lines(MODULE_PATH), 1)
                 if "weightGrams" in line or "sampleTime" in line]
    assert offenders == [], (
        "weight_oauth reads a Weight field at line(s) %s. This tool captures a "
        "body; it does not interpret one." % offenders)
