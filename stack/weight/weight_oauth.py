#!/usr/bin/env python3
"""Two Owner-run tools that clear the weight feeder's blocked vendor half.

ONE RESPONSIBILITY EACH, AND NEITHER OF THEM IS A PARSER:

  * `mint`    - take the Owner through Google's consent screen ONCE and write a
                refresh token into WEIGHT_TOKEN_FILE.
  * `capture` - make EXACTLY ONE real `dataPoints.list` call and save the raw
                response body to a file the Owner can hand back.

THIS FILE DELIBERATELY CONTAINS NO GOOGLE HEALTH PARSER, and adding one here
would evade the test that asserts `weight_feeder` has none. The rule is B7's
and `stack/weight/README.md` states the reason in the words that matter: a
usage parser that is one field off posts an obviously silly percentage, while a
weight parser that reads kilograms as pounds posts a confident, plausible,
WRONG body weight and nothing on the wall could tell anyone. `capture` exists
precisely so the parser can be written against a body somebody has actually
seen. `tests/test_weight_oauth.py` asserts the absence by name for THIS module
too.

WHY THESE ARE NOT IN `weight_feeder.py`. The feeder is a oneshot the timer runs
unattended as an unprivileged account under `ProtectSystem=strict`, and its
central invariant is that it NEVER WRITES A CREDENTIAL - a whole allow-list,
a systemd mount option and three tests stand behind that sentence. These two
tools are the opposite shape: run by a human, at a browser, once, and one of
them writes exactly the credential the feeder may not. Putting them in the same
module would put a credential-writing door inside the module whose guarantee is
that it has none.

WHAT THEY REUSE RATHER THAN REINVENT, and why each one matters:

  * `weight_feeder.vendor_opener()` for every outbound call. urllib's DEFAULT
    opener follows redirects - carrying `Authorization` across a cross-host
    302 - and honours `http_proxy`. The token these calls carry grants blood
    glucose, body fat, oxygen saturation, core body temperature and heart-rate
    metrics as well as weight, because there is no weight-only scope.
  * `weight_feeder.open_no_follow()` for the token write: unlink first (which
    destroys a planted LINK, never the file it points at), then
    `O_CREAT|O_EXCL|O_NOFOLLOW` at 0600, so a symlink planted between the
    check and the open is refused by the kernel rather than by our confidence.
  * `weight_feeder.CREDENTIAL_BASENAMES` for the capture output, so
    `--out ~/.netrc` is refused by name.
  * `weight_feeder.GOOGLE_HEALTH_LIST_URL` / `GOOGLE_HEALTH_SCOPE` - the route
    and scope VERIFIED against the live discovery document, not the prose docs
    (which render the read as `/v4/users/me/dataPoints/weight` and are wrong).

NOTHING SECRET IS EVER PRINTED. Not the client secret, not the authorization
code, not the access token, not the refresh token - not on success, not in an
error, not in a traceback, not into the file the capture writes its status
line to. A remote's error body is never echoed either: at most a single short
`error` enum is lifted out of it, through a strict allow-list pattern, because
"invalid_grant" is what the Owner needs and the rest of the body is the
remote's to write. `tests/test_weight_oauth.py` asserts this by running the
whole flow against a fake Google whose every field is a sentinel string and
grepping the captured output for each one.

THE ONE OAUTH CLIENT IS OAUTH2-PROXY'S, AND IT IS REUSED, NOT COPIED. The
client id and secret are read from `stack/.env`'s `OAUTH2_PROXY_CLIENT_ID` /
`OAUTH2_PROXY_CLIENT_SECRET`. OBSERVED, NOT ASSUMED: the deployed hub's
`/opt/homehub/stack/.env` was listed by key name on 2026-09-09 and holds
`OAUTH2_PROXY_CLIENT_ID`, `OAUTH2_PROXY_CLIENT_SECRET`, `TRACKER_DRIVE_USER`,
`TRACKER_DRIVE_SHEET_ID` and an empty `TRACKER_DRIVE_FOLDER_ID` - and NO
`TRACKER_DRIVE_CLIENT_ID`, NO `TRACKER_DRIVE_CLIENT_SECRET` and no
`GOOGLE_CLIENT_*` of any kind. An earlier draft of this docstring said the
tracker's Drive sync had its own client-id variable that referenced the same
value; it does not, and the tracker's compose reaches straight for the
`OAUTH2_PROXY_*` pair. The household has EXACTLY ONE Google OAuth client,
which is the good outcome: one secret to rotate, nothing to drift.

`TRACKER_DRIVE_CLIENT_ID` / `TRACKER_DRIVE_CLIENT_SECRET` are still accepted as
a FALLBACK, after the `OAUTH2_PROXY_*` pair and only if that pair is absent, so
a differently-provisioned box that really does carry them still mints. See
`resolve_client` for the exact order and for the refusal, which names ALL FOUR
variables - the message that named only the two that do not exist is what made
this hard to diagnose.

THE MINTED TOKEN FILE HOLDS NO CLIENT SECRET; the feeder gets the pair from its
`EnvironmentFile=` at run time, so the secret stays in exactly one place.

Implements: SR-022, LLR-006
"""

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# The feeder is the neighbouring file; both are installed into
# /opt/homehub/stack/weight/ and run as `/usr/bin/python3 <that path>`, so the
# directory this file lives in is the only import root either of them needs.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import weight_feeder                                    # noqa: E402


# ── Constants: the endpoints, the knobs, and the one flow decision ──────────

# Google's OAuth 2.0 endpoints. Stated as constants so a test can point the
# flow at a loopback server without the production values ever being in doubt.
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# The route and scope the feeder VERIFIED against Google on 2026-09-09. Taken
# from the feeder rather than restated, so the capture cannot drift from the
# constants the feeder's tests pin.
LIST_URL = weight_feeder.GOOGLE_HEALTH_LIST_URL
SCOPE = weight_feeder.GOOGLE_HEALTH_SCOPE

# THE CONSENT FLOW: a registered loopback redirect, and NO LISTENER. See the
# module docstring's companion section in README.md for the Owner's steps.
#
# The Owner runs `mint` ON THE HUB, over SSH, on a box with no browser, so the
# browser that shows the consent screen is on a DIFFERENT MACHINE from the
# process waiting for the code. That rules the three candidate flows as
# follows:
#
#   * `urn:ietf:wg:oauth:2.0:oob` (the old copy-the-code page) - Google SHUT
#     THIS DOWN in October 2022. New use answers `invalid_request`. It is not
#     an option however convenient it would be.
#   * A loopback LISTENER on the hub - would give the Owner a tidy "you may
#     close this window" page, but only if `ssh -L 8117:127.0.0.1:8117` is
#     already up BEFORE consent, because the browser resolves `localhost` on
#     the PC. Forget the tunnel and the code is spent and lost, and consent has
#     to be repeated. It also puts a live authorization code on a hub socket
#     that any other local account may connect to.
#   * A registered loopback redirect with NOTHING LISTENING - the browser lands
#     on "this site can't be reached", the code sits in the address bar, and
#     the Owner pastes it back into the SSH session. No tunnel, no port, no
#     race, and the code goes from the Owner's clipboard to this process's
#     stdin without touching a socket.
#
# The third is chosen. Its one cost is that the browser shows a failure page
# that looks like the flow broke, so README.md says in the step itself that the
# error page IS the success case.
#
# PKCE IS SENT ANYWAY (S256), even though a web client also authenticates with
# its secret: the code travels through an address bar, a clipboard and a
# terminal, and the verifier - which never leaves this process - is what makes
# an intercepted code useless.
DEFAULT_REDIRECT_URI = "http://localhost:8117/"

# Where the client id/secret live. Read with the key-by-key reader below, never
# by `source`-ing a file that also holds TECHNITIUM_ADMIN_PASSWORD and
# CLOUDFLARE_API_TOKEN into this process's environment.
DEFAULT_ENV_FILE = "/opt/homehub/stack/.env"
CLIENT_ID_KEY = "OAUTH2_PROXY_CLIENT_ID"
CLIENT_SECRET_KEY = "OAUTH2_PROXY_CLIENT_SECRET"

# The fallback pair, tried ONLY when the pair above is absent. It is not on the
# hub - the live `.env` was listed by key name on 2026-09-09 and has neither of
# these - but a differently-provisioned box may carry them, and accepting them
# costs one tuple entry while refusing them would cost that box a mint it
# cannot diagnose.
FALLBACK_CLIENT_ID_KEY = "TRACKER_DRIVE_CLIENT_ID"
FALLBACK_CLIENT_SECRET_KEY = "TRACKER_DRIVE_CLIENT_SECRET"

# THE RESOLUTION ORDER, in one place, in order, so it is readable and testable.
# First complete pair wins; a pair is complete only when BOTH halves are set.
CLIENT_KEY_PAIRS = (
    (CLIENT_ID_KEY, CLIENT_SECRET_KEY),
    (FALLBACK_CLIENT_ID_KEY, FALLBACK_CLIENT_SECRET_KEY),
)

# The refusal's looked-for list is fenced by these two markers so a test can
# cut it out and assert that every consulted variable is named INSIDE it. The
# rest of the message may mention a variable for other reasons; only what lies
# between these two markers is the record of what was actually searched for.
LOOKED_FOR_PREFIX = "Looked for, in order: "
LOOKED_FOR_SUFFIX = "."

TOKEN_FILE_KEY = "WEIGHT_TOKEN_FILE"
STATE_FILE_KEY = "WEIGHT_STATE_FILE"
ACCOUNT_KEY = "WEIGHT_USER_ACCOUNT"
TIMEOUT_KEY = "WEIGHT_TIMEOUT_SECONDS"

# Every key this tool reads out of `.env`, and NOTHING ELSE reaches this
# process: the same file holds TECHNITIUM_ADMIN_PASSWORD, CLOUDFLARE_API_TOKEN
# and the finance credentials.
ENV_KEYS = (CLIENT_ID_KEY, CLIENT_SECRET_KEY,
            FALLBACK_CLIENT_ID_KEY, FALLBACK_CLIENT_SECRET_KEY,
            TOKEN_FILE_KEY, STATE_FILE_KEY, ACCOUNT_KEY, TIMEOUT_KEY)

DEFAULT_TIMEOUT_SECONDS = 30


class Refused(Exception):
    """Something the Owner must fix before this can proceed.

    ONE exception type for every refusal - a missing knob, a path outside the
    state directory, an existing token file, a pasted blank, a non-200 from
    Google - because `main` treats them identically: print the message, exit
    non-zero, print nothing else. A second type would be a second chance for a
    message carrying credential material to reach a different printer.

    ITS MESSAGE IS ALWAYS OURS. Nothing built from a token, a code, a secret or
    a remote's response body is ever put in one; `tests/test_weight_oauth.py`
    proves that by sentinel.
    """


# ── Pure core: no I/O below this line until the SHELL banner ────────────────

# An OAuth error identifier is a short bare word (`invalid_grant`,
# `access_denied`, `PERMISSION_DENIED`). ANYTHING ELSE IS NOT PRINTED. This is
# an allow-list rather than a sanitiser because the input is a remote's chosen
# bytes: the question is not "what should we strip" but "what are we willing to
# repeat", and the answer is one short enum from a fixed alphabet.
ERROR_ENUM_RE = re.compile(r"^[A-Za-z_]{1,40}$")


def printable_error_code(value):
    """Return `value` if it looks like an OAuth/Google error enum, else None.

    Contract:
      Inputs:  value - a string lifted from a remote's error body, or anything.
      Outputs: the same string when it matches ERROR_ENUM_RE, else None.
      Raises:  nothing.

    Implements: SR-022
    """
    if not isinstance(value, str):
        return None
    return value if ERROR_ENUM_RE.match(value) else None


def error_code_in(body_text):
    """The one short enum worth repeating out of an OAuth/Google error body.

    Contract:
      Inputs:  body_text - the remote's response body, as text.
      Outputs: `invalid_grant`-shaped string, or None when the body is not
               JSON, has no error, or names one that is not enum-shaped.
      Raises:  nothing.

    WHY NOT `error_description`. That field is prose the remote composes, and
    Google's token endpoint has been observed to quote the offending request
    back inside it. The `error` enum answers the Owner's actual question
    ("expired code" vs "wrong redirect_uri" vs "scope not granted") and cannot
    carry a credential through the pattern above.

    Implements: SR-022
    """
    try:
        data = json.loads(body_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    direct = printable_error_code(data.get("error"))
    if direct:
        return direct
    nested = data.get("error")
    if isinstance(nested, dict):
        return (printable_error_code(nested.get("status"))
                or printable_error_code(nested.get("code")))
    return None


def env_values(text, keys=ENV_KEYS):
    """Read ONLY the named keys out of a `.env` file's text.

    Contract:
      Inputs:  text - the whole file; keys - the names we own.
      Outputs: {key: value} for the keys present, LAST assignment winning.
      Raises:  nothing.

    LAST WINS, matching `setup-weight.sh`'s `grep ... | tail -1`, so a file
    where somebody appended an override behaves the same way in both places.
    Surrounding quotes are stripped because compose-style `.env` files carry
    both forms and `docker compose` strips them too.

    Nothing else in the file is read into this process at all: `stack/.env`
    also holds TECHNITIUM_ADMIN_PASSWORD, CLOUDFLARE_API_TOKEN and the finance
    credentials, and a tool that needs two keys should hold two keys.

    Implements: SR-022
    """
    found = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in keys:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found[key] = value
    return found


def resolve_client(values, path=DEFAULT_ENV_FILE, pairs=CLIENT_KEY_PAIRS):
    """Settle WHICH pair of `.env` keys carries the Google OAuth client.

    Contract:
      Inputs:  values - what `env_values` read; path - only for the message;
               pairs - the order, injected by tests.
      Outputs: (id_key, client_id, client_secret) - WHICH variable the id
               came from, then the two non-blank values. The key name is
               returned so the minted token file can record its provenance
               without a second search.
      Raises:  Refused when no pair is complete - naming EVERY variable that
               was looked for, and never a value.

    THE ORDER IS `OAUTH2_PROXY_*` FIRST, `TRACKER_DRIVE_CLIENT_*` SECOND, and
    the first COMPLETE pair wins. A pair is complete only when both halves are
    set: half a pair is a mis-provisioned box, and silently sliding to the next
    pair would mint with a client id from one place and a secret from another,
    which fails at Google as `invalid_client` and looks like a Google problem.

    WHY THE MESSAGE NAMES ALL FOUR. On the deployed hub this tool's first
    version looked only for `TRACKER_DRIVE_CLIENT_ID` /
    `TRACKER_DRIVE_CLIENT_SECRET`, which DO NOT EXIST there, and said so - so
    the Owner went looking for a variable nobody had ever set instead of seeing
    that the two that ARE set were the answer. A refusal that lists everything
    it looked for turns that into one glance at the file.

    Implements: SR-022, LLR-006
    """
    for id_key, secret_key in pairs:
        client_id = values.get(id_key)
        client_secret = values.get(secret_key)
        if client_id and client_secret:
            return id_key, client_id, client_secret

    # The looked-for list is its own delimited clause, between the two markers
    # below, so a test can assert that EVERY variable this function consulted
    # is named IN IT - not merely somewhere in a paragraph that happens to
    # mention two of them for other reasons.
    halves = ["%s (%s)" % (key, "set" if values.get(key) else "unset or blank")
              for pair in pairs for key in pair]
    raise Refused(
        "no complete Google OAuth client pair in %s. %s%s%s A pair counts only "
        "when BOTH halves are set, and the pairs are tried in that order. "
        "There is exactly one Google OAuth client in this household - "
        "oauth2-proxy's - and it is the one to reuse; do not mint a second. "
        "stack/.env is mode 0600 root:root, so run this with sudo."
        % (path, LOOKED_FOR_PREFIX, "; ".join(halves), LOOKED_FOR_SUFFIX))


def pkce_challenge(verifier):
    """The S256 challenge for `verifier`: base64url(sha256(verifier)), unpadded.

    Implements: SR-022
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def new_verifier():
    """A fresh PKCE verifier. It NEVER leaves this process - not printed, not
    stored, not passed on a command line where `ps` could read it."""
    return base64.urlsafe_b64encode(os.urandom(48)).decode("ascii").rstrip("=")


def authorization_url(client_id, redirect_uri, state, code_challenge,
                      scope=SCOPE, endpoint=AUTH_ENDPOINT):
    """The URL the Owner opens in a browser on their own PC.

    Contract:
      Outputs: an absolute https URL carrying the client, the ONE scope, the
               registered redirect, a CSRF `state` and the PKCE challenge.

    `access_type=offline` with `prompt=consent` is what makes Google return a
    REFRESH token rather than an access token alone - and `prompt=consent`
    specifically, because Google omits the refresh token on a re-consent when
    the account has already granted this client, which is exactly the case here
    (the account has signed in through oauth2-proxy many times). Without it the
    flow succeeds and mints nothing usable.

    Implements: SR-022, LLR-006
    """
    fields = [
        ("client_id", client_id),
        ("redirect_uri", redirect_uri),
        ("response_type", "code"),
        ("scope", scope),
        ("access_type", "offline"),
        ("prompt", "consent"),
        ("state", state),
        ("code_challenge", code_challenge),
        ("code_challenge_method", "S256"),
    ]
    return endpoint + "?" + urllib.parse.urlencode(fields)


def code_from_paste(raw, expected_state):
    """The authorization code out of whatever the Owner pasted back.

    Contract:
      Inputs:  raw - either the whole redirected URL from the address bar, or
                     the bare `code=` value;
               expected_state - the CSRF state this process generated.
      Outputs: the authorization code.
      Raises:  Refused - blank paste, a consent denial, a state mismatch, or a
               paste that is not one of the two accepted shapes.

    THE WHOLE URL IS THE EXPECTED PASTE. The browser lands on a "site can't be
    reached" page whose address bar holds
    `http://localhost:8117/?state=...&code=4/0A...&scope=...`, and telling a
    person to select part of that string is how the wrong part gets selected.
    The bare code is accepted too because somebody will paste just the code.

    NO REFUSAL MESSAGE HERE EVER QUOTES `raw`. It may be a live authorization
    code, and a message is a thing that gets printed.

    Implements: SR-022
    """
    raw = (raw or "").strip()
    if not raw:
        raise Refused(
            "nothing was pasted. Paste the WHOLE address-bar URL from the "
            "browser (it starts with the redirect URI and contains `code=`).")
    looks_like_url = "://" in raw or raw.startswith("?") or raw.startswith("/?")
    if not looks_like_url:
        if any(character.isspace() for character in raw) or "&" in raw:
            raise Refused(
                "that paste is neither a URL nor a bare code (it contains "
                "whitespace or `&`). Paste the whole address-bar URL.")
        return raw
    parts = urllib.parse.urlsplit(raw)
    fields = urllib.parse.parse_qs(parts.query or raw.lstrip("?"))
    denial = printable_error_code((fields.get("error") or [None])[0])
    if fields.get("error"):
        raise Refused(
            "Google returned an error instead of a code%s. If it is "
            "`access_denied` the consent screen was declined or the account is "
            "not on the project's Test users list."
            % (": " + denial if denial else ""))
    given_state = (fields.get("state") or [None])[0]
    if given_state is not None and given_state != expected_state:
        raise Refused(
            "the `state` in that URL is not the one this run generated. That "
            "is either a stale browser tab from an earlier attempt or a "
            "response to a request nobody here made. Start `mint` again.")
    code = (fields.get("code") or [None])[0]
    if not code:
        raise Refused("that URL carries no `code=` parameter.")
    return code


def authorization_code_fields(client_id, client_secret, code, verifier,
                              redirect_uri):
    """The form body of the authorization-code exchange.

    Split out as a pure function so a test can assert what is SENT - notably
    that the PKCE verifier goes to Google and that `redirect_uri` is byte-equal
    to the one in the authorization URL, which is the single most common cause
    of `redirect_uri_mismatch`.

    Implements: SR-022
    """
    return {"client_id": client_id, "client_secret": client_secret,
            "code": code, "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"}


def refresh_fields(client_id, client_secret, refresh_token):
    """The form body of a refresh-token grant.

    Implements: SR-022
    """
    return {"client_id": client_id, "client_secret": client_secret,
            "refresh_token": refresh_token, "grant_type": "refresh_token"}


def token_document(payload, minted_at, scope=SCOPE, client_key=CLIENT_ID_KEY):
    """What actually gets written to WEIGHT_TOKEN_FILE.

    Contract:
      Inputs:  payload - Google's parsed token response; minted_at - epoch s.
      Outputs: a dict holding the refresh token and provenance.
      Raises:  Refused when the response carries no refresh token.

    WHAT IS DELIBERATELY NOT IN IT:

      * THE CLIENT SECRET. The feeder receives the pair through its unit's
        `EnvironmentFile=/opt/homehub/stack/.env`, so writing it here would put
        a second copy of the household's Google client secret on disk, in a
        second place, to be rotated separately. The household has exactly ONE
        Google OAuth client - oauth2-proxy's - and everything that needs it
        reaches for `OAUTH2_PROXY_CLIENT_SECRET` itself rather than keeping a
        copy; this file is not going to be the first copy.
      * THE ACCESS TOKEN. It expires in an hour, so it is a credential with no
        durable value; `capture` refreshes for a fresh one each time.

    A MISSING REFRESH TOKEN IS A REFUSAL, NOT A FILE. Google omits it on a
    re-consent that it treats as already granted, and a token file holding only
    an access token would look minted, work for an hour and then fail
    mysteriously.

    Implements: SR-022, LLR-006
    """
    refresh_token = payload.get("refresh_token") if isinstance(payload, dict) else None
    if not isinstance(refresh_token, str) or not refresh_token:
        raise Refused(
            "Google's reply carried no refresh token, so nothing was written. "
            "That happens when the consent was not a fresh one; this tool "
            "always sends prompt=consent, so if it recurs, remove this app "
            "under https://myaccount.google.com/permissions and run mint "
            "again.")
    granted = payload.get("scope")
    return {
        "refresh_token": refresh_token,
        "scope": granted if isinstance(granted, str) else scope,
        "token_type": "Bearer",
        "minted_at": weight_feeder.iso8601_utc(int(minted_at)),
        "minted_by": "stack/weight/weight_oauth.py mint",
        # WHICH env variable the client came from, so a box that fell back
        # to the TRACKER_DRIVE_CLIENT_* pair says so in its own token file
        # rather than claiming a client it did not use.
        "client": "the shared %s; the client secret is deliberately NOT stored "
                  "here - the service reads it from stack/.env through its "
                  "unit's EnvironmentFile=" % client_key,
    }


def token_write_verdict(path, token_path, state_path, state_root, resolve=None):
    """Decide whether the minted token may be written to `path`. Reason, or None.

    Contract:
      Inputs:  path - the file about to be opened;
               token_path - WEIGHT_TOKEN_FILE, the ONE path this tool may write;
               state_path - WEIGHT_STATE_FILE, which it may never write;
               state_root - the service's own StateDirectory root;
               resolve - the resolver, `os.path.realpath` in production and
                 injected only so a test can state what a symlink resolves to
                 on a filesystem that will not let it create one.
      Outputs: None if allowed, else the reason it is refused.
      Raises:  nothing. It DECIDES; `open_token_for_write` refuses.

    THIS IS THE FEEDER'S GUARD WITH ITS SIGN REVERSED, AND THAT IS WHY IT IS A
    SEPARATE FUNCTION. `weight_feeder.writable_path_verdict` refuses every
    credential basename outright - "this feeder reads credentials and never
    writes one" - and reusing it here is impossible, because writing
    `google-health-token.json` is this tool's entire job. What is kept is the
    SHAPE, every part of which was a real defect somewhere:

      * `realpath`, NOT `abspath`. String arithmetic does not resolve symlinks,
        and a link at the token path pointing at, say, the tracker's Drive
        token would otherwise pass a string comparison and be truncated.
      * A ONE-PATH ALLOW-LIST. Not "not a credential", which cannot survive the
        next tool putting its token somewhere new.
      * CONTAINMENT IN THE SERVICE'S OWN StateDirectory, which the `.env`
        cannot move: a knob naming somebody's `~/.ssh/id_ed25519` still names
        something outside /var/lib/homehub-weight.
      * AND NEVER THE STATE FILE, because the two knobs are edited by the same
        hand and a token written over the state file would be read back as a
        reading, then overwritten by the next cycle.

    Implements: SR-022, LLR-006
    """
    resolve = resolve or os.path.realpath
    root = resolve(state_root)
    inside = os.path.join(root, "")
    resolved = resolve(path)
    if not resolved.startswith(inside):
        return ("%s resolves to %s, which is outside the service's own state "
                "directory %s. The refresh token must live under the state "
                "directory this service owns, so that no knob can aim this "
                "write at a credential somewhere else."
                % (path, resolved, root))
    if resolved != resolve(token_path):
        return ("%s resolves to %s, which is not the one path this tool may "
                "write (%s, from %s)."
                % (path, resolved, resolve(token_path), TOKEN_FILE_KEY))
    if state_path and resolved == resolve(state_path):
        return ("%s resolves to the feeder's own state file (%s). %s and %s "
                "must name different files."
                % (path, resolve(state_path), TOKEN_FILE_KEY, STATE_FILE_KEY))
    return None


def capture_output_verdict(path, resolve=None):
    """Decide whether a captured response body may be written to `path`.

    Contract:
      Outputs: None if allowed, else the reason.

    THE OWNER NAMES THIS PATH, so it cannot be an allow-list - the whole point
    is to put the body somewhere they can `scp` it from. What it can do is
    refuse the two shapes that would cost a credential: a path whose name (or
    whose symlink target's name) is a known credential file, and any path that
    already exists (`open_capture_output` creates with O_EXCL, so an existing
    file or a planted link is refused by the kernel too).

    The body itself is HEALTH DATA - a real body weight, with a timestamp - so
    the file is created 0600 and its content is never printed.

    Implements: SR-022
    """
    resolve = resolve or os.path.realpath
    for named in (path, resolve(path)):
        if os.path.basename(named) in weight_feeder.CREDENTIAL_BASENAMES:
            return ("%s (which resolves to %s) is a credential file. The "
                    "captured body is health data and does not go there."
                    % (path, named))
    return None


# ── SHELL: everything below touches the world ───────────────────────────────

def read_env_file(path):
    """Load the keys this tool owns out of a `.env` file.

    Raises Refused when the file cannot be read. WHICH keys must be present is
    not decided here: `resolve_client` owns that, because the answer is a
    resolution ORDER across two pairs and not a flat required-list.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise Refused(
            "could not read %s (%s). stack/.env is mode 0600 root:root by "
            "design, so these tools are run with sudo; see README.md."
            % (path, type(exc).__name__))
    return env_values(text)


def post_form(url, fields, timeout, what):
    """POST a form to Google and return the parsed JSON. Never leaks material.

    Contract:
      Inputs:  url; fields - the form body; timeout - seconds; what - a short
               noun for the message ("the token exchange").
      Outputs: the parsed JSON object.
      Raises:  Refused, with the HTTP status and at most one error enum.

    IT GOES THROUGH `vendor_opener()`. A 302 from anything pretending to be
    Google's token endpoint would otherwise be followed WITH the client secret
    and the authorization code attached, and an exported `http_proxy` would
    route the whole exchange through a LAN proxy that terminates TLS in front
    of a health-data credential.

    Implements: SR-022, LLR-006
    """
    data = urllib.parse.urlencode(fields).encode("ascii")
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
        method="POST")
    try:
        with weight_feeder.vendor_opener().open(request, timeout=timeout) as response:
            body = response.read()
    except weight_feeder.EgressRefused as exc:
        raise Refused("%s was refused: %s" % (what, exc))   # our words, not theirs
    except urllib.error.HTTPError as exc:
        code = error_code_in(_error_text(exc))
        raise Refused("%s failed: HTTP %s%s (body not printed)."
                      % (what, exc.code, ", " + code if code else ""))
    except Exception as exc:
        raise Refused("%s failed: %s." % (what, type(exc).__name__))
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise Refused("%s returned something that is not JSON (%s)."
                      % (what, type(exc).__name__))


def _error_text(http_error):
    """The error body as text, for `error_code_in` ONLY - never for printing.

    It is read here rather than at the call site so there is exactly one place
    a remote's bytes exist, and the only thing that leaves this function's
    caller is a value that matched ERROR_ENUM_RE.
    """
    try:
        return http_error.read().decode("utf-8", "replace")
    except Exception:
        return ""


def access_token_from_refresh(client_id, client_secret, refresh_token, timeout,
                              endpoint=TOKEN_ENDPOINT):
    """Exchange the stored refresh token for a short-lived access token.

    This is NOT the `dataPoints.list` call - it is the ordinary grant that
    precedes it, and it is why `capture` still makes exactly one call to the
    health API.

    Implements: SR-022
    """
    payload = post_form(endpoint,
                        refresh_fields(client_id, client_secret, refresh_token),
                        timeout, "the token refresh")
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise Refused(
            "the token refresh returned no access token. If the refresh token "
            "has been revoked (Google expires unused ones, and removing the "
            "app under myaccount.google.com/permissions revokes them all), "
            "run `mint --force` again.")
    return token


def ensure_directory(path, state_root):
    """Create the token file's directory, 0700, INSIDE the state root only.

    `StateDirectory=homehub-weight` creates /var/lib/homehub-weight the first
    time the unit runs, but the Owner mints BEFORE the first successful run, so
    the directory may legitimately not exist yet. Creating it here is bounded
    the same way the write is: a directory whose real path is not under the
    state root is refused rather than created, so this cannot be turned into a
    `mkdir -p` anywhere on the box.
    """
    root = os.path.realpath(state_root)
    target = os.path.realpath(path)
    if not (target == root or target.startswith(os.path.join(root, ""))):
        raise Refused("REFUSED: %s is outside the service's state directory %s."
                      % (path, root))
    if not os.path.isdir(path):
        os.makedirs(path, 0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass            # Windows has no mode to set; the hub is Linux.


def open_token_for_write(path, token_path, state_path, state_root):
    """The ONLY way this tool creates the token file.

    Raises Refused for anything `token_write_verdict` refuses, and OSError from
    the kernel if the final component is a symlink or something appeared
    between the verdict and the open.

    The open itself is the feeder's `open_no_follow`: unlink first (destroying
    a planted LINK, never its target), then `O_CREAT|O_EXCL|O_NOFOLLOW` at
    0600. Reusing it rather than restating it is deliberate - it is the
    function the feeder's own race test exercises.

    Implements: SR-022, LLR-006
    """
    reason = token_write_verdict(path, token_path, state_path, state_root)
    if reason:
        raise Refused("REFUSED: " + reason)
    return weight_feeder.open_no_follow(path)


def open_capture_output(path):
    """Create the capture's output file, 0600, refusing to overwrite anything.

    No unlink here, unlike the token write: this path is the OWNER'S choice, so
    "something is already there" is a question for them, not a thing to clear.
    O_EXCL makes an existing file - or a symlink planted at that name - an
    error from the kernel.
    """
    reason = capture_output_verdict(path)
    if reason:
        raise Refused("REFUSED: " + reason)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        return os.fdopen(os.open(path, flags, 0o600), "wb")
    except OSError as exc:
        raise Refused(
            "could not create %s (%s). It refuses to overwrite an existing "
            "file or follow a symlink; choose a new name." % (path, type(exc).__name__))


def give_to_account(path, account):
    """Hand `path` to the UNIX account the feeder runs as, if there is one.

    These tools are run with sudo (stack/.env is 0600 root:root), so the file
    root creates is root's. The feeder runs as WEIGHT_USER_ACCOUNT under
    `ProtectHome=read-only` and must be able to READ it. Silent on platforms
    with no such thing - the dev PC runs the tests, the hub runs the tool.
    """
    if os.name != "posix" or not account:
        return None
    import pwd                                          # posix only
    try:
        entry = pwd.getpwnam(account)
    except KeyError:
        raise Refused(
            "the account %s does not exist, so the token cannot be handed to "
            "the feeder. setup-weight.sh creates it; run it first, or pass "
            "--account." % account)
    os.chown(path, entry.pw_uid, entry.pw_gid)
    return account


def give_back_to_invoker(path):
    """Hand a sudo-created file back to the human who typed sudo.

    Without this, `capture --out ~/body.json` leaves a root-owned 0600 file in
    the Owner's home that they then cannot read without sudo - and the whole
    point of the file is that they hand it back.
    """
    if os.name != "posix":
        return
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if not (uid and gid):
        return
    try:
        os.chown(path, int(uid), int(gid))
    except (OSError, ValueError):
        pass            # ownership is a convenience here, not the guarantee.


def resolve_paths(values, args):
    """Settle (token_file, state_file, state_root) from the .env and the flags.

    A flag wins over the file, which wins over the shipped default - the same
    order every other tool in this stack uses - and the state root falls back to
    the feeder's own DEFAULT_STATE_ROOT so a hand-run is bound exactly as the
    unit's StateDirectory= binds a timed run.
    """
    token_file = args.token_file or values.get(TOKEN_FILE_KEY) or ""
    if not token_file:
        raise Refused(
            "%s is not set in the .env and --token-file was not given. There "
            "is no default: the token's home is a decision, not a guess."
            % TOKEN_FILE_KEY)
    state_file = values.get(STATE_FILE_KEY) or ""
    state_root = (args.state_root
                  or weight_feeder.resolve_state_root(dict(os.environ)))
    return os.path.expanduser(token_file), os.path.expanduser(state_file), state_root


def resolve_timeout(values, args):
    """Seconds for every outbound call. The .env's knob, or the default."""
    if args.timeout:
        return args.timeout
    raw = values.get(TIMEOUT_KEY) or ""
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


# ── The two commands ────────────────────────────────────────────────────────

def read_pasted_code(prompt_text):
    """Read the pasted redirect URL from stdin. The ONLY interactive step."""
    sys.stdout.write(prompt_text)
    sys.stdout.flush()
    return sys.stdin.readline()


def mint(args, out=None, prompt=None, auth_endpoint=AUTH_ENDPOINT,
         token_endpoint=TOKEN_ENDPOINT, now=None):
    """Take the Owner through consent once and write the refresh token.

    Contract:
      Inputs:  args - the parsed `mint` arguments; out - where the human-facing
               lines go; prompt - the reader for the pasted URL; the two
               endpoints and `now`, injected only by tests.
      Outputs: 0.
      Raises:  Refused for every "the Owner must fix something" case.

    IT ORCHESTRATES AND DOES NOT COMPUTE: settle configuration, refuse early,
    print a URL, read a paste, exchange it, write it, hand it over.

    THE EARLY REFUSAL IS NOT COSMETIC. Every check that can be made before the
    Owner is sent to a browser IS made before that, because an authorization
    code is single-use and short-lived: discovering afterwards that the token
    file already exists costs a whole second trip through consent.

    Implements: SR-022, LLR-006
    """
    out = out or sys.stdout
    prompt = prompt or read_pasted_code
    values = read_env_file(args.env_file)
    client_key, client_id, client_secret = resolve_client(values, args.env_file)
    token_file, state_file, state_root = resolve_paths(values, args)
    timeout = resolve_timeout(values, args)
    account = args.account or values.get(ACCOUNT_KEY) or "homehub-weight"

    ensure_directory(os.path.dirname(token_file) or ".", state_root)
    reason = token_write_verdict(token_file, token_file, state_file, state_root)
    if reason:
        raise Refused("REFUSED: " + reason)
    if os.path.lexists(token_file) and not args.force:
        raise Refused(
            "%s already exists and --force was not given. Overwriting a "
            "working refresh token silently is how a feeder starts saying "
            "'unavailable' for a reason nobody can see; if you mean to replace "
            "it, pass --force." % token_file)

    verifier = new_verifier()
    state = secrets.token_urlsafe(16)
    url = authorization_url(client_id, args.redirect_uri, state,
                            pkce_challenge(verifier), endpoint=auth_endpoint)
    out.write(
        "\nOpen this URL in a browser ON YOUR PC (it is one line):\n\n%s\n\n"
        "Sign in as the household account, and grant the health-metrics scope.\n"
        "THE BROWSER WILL THEN SHOW A 'site can't be reached' PAGE AT %s -\n"
        "THAT IS THE SUCCESS CASE. Nothing is listening there on purpose.\n"
        "Copy the WHOLE address-bar URL of that failed page and paste it below.\n"
        % (url, args.redirect_uri))
    code = code_from_paste(prompt("Paste the address-bar URL here: "), state)
    payload = post_form(
        token_endpoint,
        authorization_code_fields(client_id, client_secret,
                                  code, verifier, args.redirect_uri),
        timeout, "the token exchange")
    document = token_document(payload, now if now is not None else time.time(),
                              client_key=client_key)
    with open_token_for_write(token_file, token_file, state_file, state_root) as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    handed = give_to_account(token_file, account)
    out.write("\nweight: refresh token written to %s (mode 0600%s).\n"
              % (token_file, ", owner %s" % handed if handed else ""))
    out.write("weight: the token itself was not printed, and the client secret "
              "was not copied into it.\n")
    out.write("weight: next, capture ONE response body - see stack/weight/README.md.\n")
    return 0


def capture(args, out=None, list_url=None, token_endpoint=TOKEN_ENDPOINT):
    """Make EXACTLY ONE `dataPoints.list` call and save the body verbatim.

    Contract:
      Inputs:  args - the parsed `capture` arguments; out; the two endpoints,
               injected only by tests.
      Outputs: 0 on a 200 whose body was saved.
      Raises:  Refused for a bad configuration, a refused write, or a non-200.

    THE BODY IS NEVER PRINTED. It is a real body weight with a timestamp -
    health data about a specific person - so what reaches the terminal is the
    HTTP status, the byte count and a SHA-256 of the file, which is enough to
    confirm that the file handed back is the file that was captured.

    ONE CALL. Not a loop over pages, not a retry: this exists so a human can
    look at one real response before anybody writes a parser, and a tool that
    quietly made four calls would be four consents' worth of health data moving
    for a step that needs one.

    Implements: SR-022, LLR-006
    """
    out = out or sys.stdout
    list_url = list_url or LIST_URL
    values = read_env_file(args.env_file)
    _client_key, client_id, client_secret = resolve_client(values, args.env_file)
    token_file, _state_file, _root = resolve_paths(values, args)
    timeout = resolve_timeout(values, args)

    try:
        with open(token_file, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except OSError as exc:
        raise Refused("could not read the token file %s (%s). Run `mint` first."
                      % (token_file, type(exc).__name__))
    except ValueError:
        raise Refused("the token file %s is not JSON. Run `mint --force`."
                      % token_file)
    refresh_token = stored.get("refresh_token") if isinstance(stored, dict) else None
    if not isinstance(refresh_token, str) or not refresh_token:
        raise Refused("the token file %s carries no refresh_token. Run "
                      "`mint --force`." % token_file)

    access_token = access_token_from_refresh(
        client_id, client_secret, refresh_token, timeout,
        endpoint=token_endpoint)

    url = list_url
    if args.page_size:
        url = url + "?" + urllib.parse.urlencode({"pageSize": args.page_size})
    request = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + access_token,
                      "Accept": "application/json"}, method="GET")
    try:
        with weight_feeder.vendor_opener().open(request, timeout=timeout) as response:
            status, body = response.status, response.read()
    except weight_feeder.EgressRefused as exc:
        raise Refused("the list call was refused: %s" % exc)
    except urllib.error.HTTPError as exc:
        code = error_code_in(_error_text(exc))
        raise Refused(
            "the list call failed: HTTP %s%s (body not printed). 401 means the "
            "token is not valid for this API; 403 usually means "
            "health.googleapis.com is not enabled on the project, or the scope "
            "is not on the consent screen; 404 means the route is wrong - it "
            "is %s." % (exc.code, ", " + code if code else "", LIST_URL))
    except Exception as exc:
        raise Refused("the list call failed: %s." % type(exc).__name__)

    with open_capture_output(args.out) as handle:
        handle.write(body)
    give_back_to_invoker(args.out)
    out.write("weight: HTTP %s from %s\n" % (status, list_url))
    out.write("weight: %d bytes saved to %s (mode 0600; the body is NOT printed - "
              "it is health data).\n" % (len(body), args.out))
    out.write("weight: sha256 %s\n" % hashlib.sha256(body).hexdigest())
    out.write("weight: hand that file back. The parser is written against it, "
              "not before it.\n")
    return 0


# ── Entry point: it orchestrates, it does not compute ───────────────────────

def build_parser():
    """The command line. Two subcommands, no destructive default."""
    parser = argparse.ArgumentParser(
        prog="weight_oauth.py",
        description="Mint the weight feeder's Google Health refresh token, "
                    "then capture ONE real response body. Neither command "
                    "parses a weight.")
    subcommands = parser.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                        help="where the shared OAuth client lives "
                             "(default: %s)" % DEFAULT_ENV_FILE)
    common.add_argument("--token-file", default=None,
                        help="override WEIGHT_TOKEN_FILE from the .env")
    common.add_argument("--state-root", default=None,
                        help="override the service's state directory root")
    common.add_argument("--timeout", type=int, default=None,
                        help="seconds for each outbound call")

    minter = subcommands.add_parser(
        "mint", parents=[common],
        help="browser consent once, then write the refresh token")
    minter.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI,
                        help="MUST be registered on the OAuth client, byte for "
                             "byte (default: %s)" % DEFAULT_REDIRECT_URI)
    minter.add_argument("--account", default=None,
                        help="the UNIX account to hand the token to "
                             "(default: WEIGHT_USER_ACCOUNT, else homehub-weight)")
    minter.add_argument("--force", action="store_true",
                        help="replace an existing token file")

    capturer = subcommands.add_parser(
        "capture", parents=[common],
        help="ONE dataPoints.list call, body saved verbatim")
    capturer.add_argument("--out", required=True,
                          help="where to save the raw body; must not exist")
    capturer.add_argument("--page-size", type=int, default=None,
                          help="optional pageSize query parameter")
    return parser


def main(argv=None):
    """Entry point: parse, dispatch, refuse legibly.

    Every failure path ends here, and it prints `Refused`'s message and nothing
    else - no traceback, because a traceback of a urllib failure carries the
    request object and its headers.

    Implements: SR-022
    """
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if not args.command:
        build_parser().print_help()
        return 2
    try:
        return mint(args) if args.command == "mint" else capture(args)
    except Refused as exc:
        print("weight: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("weight: interrupted; nothing was written.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
