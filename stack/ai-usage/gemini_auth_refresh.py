#!/usr/bin/env python3
"""homehub-gemini-refresh: make Gemini CLI renew its OWN token, without spending quota.

ONE RESPONSIBILITY: run the CLI just far enough that it refreshes the access
token it keeps in ~/.gemini/oauth_creds.json, and stop. It reads no token, it
writes no file of its own, it posts nothing anywhere. The CLI is the only thing
that ever touches the credential -- the same division of labour as
homehub-claude-refresh, and for the same reason: SR-021 forbids the FEEDER from
minting or refreshing a Google token, and the access token lives about an hour.

WHY THE OBVIOUS COMMANDS ARE NOT USED (read from gemini-cli HEAD 9b6e026,
2026-10-09, because there is no live CLI on the dev PC to measure with):

  * `gemini -p "/stats"` -- the CLI's headless loop (`nonInteractiveCli.ts`)
    runs the slash command, but a command whose action returns nothing
    "falls through to the default prompt handling", so the literal text
    `/stats` is then SENT TO THE MODEL. That is one billed request per run,
    against the very quota the gauge reports. Unlike Claude's `/usage`, it is
    not free.
  * `gemini --version` / `gemini extensions list` -- start no auth at all.

WHAT IS USED. `gemini --acp` speaks the Agent Client Protocol on stdio, and it
renews the token in TWO places, neither of which calls the model:

  * AT STARTUP, before the protocol loop begins (`gemini.tsx`, "Do oauth before
    app renders"): with `security.auth.selectedType` = oauth-personal and the
    browser suppressed (NO_BROWSER=true, set below), `getOauthClient` loads the
    cached credential and renews an expired access token through
    google-auth-library's own 'tokens' hook (`oauth2.ts`), which rewrites
    oauth_creds.json. An `initialize` reply therefore proves startup finished.
  * on `authenticate` (`acp/acpRpcDispatcher.ts`), which runs
    `config.refreshAuth` again. Kept as the second chance for a box where the
    browser is not suppressed.

OBSERVED 2026-10-11 with @google/gemini-cli 0.63.0 (npm) on the dev PC, in an
isolated GEMINI_CLI_HOME holding a FAKE credential: `--acp` is a real flag; and
with a credential that cannot be renewed the CLI does NOT answer ACP at all -- it
prints "Please visit the following URL to authorize the application" on stdout
and reads stdin for a code, BEFORE the protocol loop. That is how a dead login
shows up here, so it is detected (exit 4), and nothing further is written. The
happy path (a real, renewable credential) could not be run without the
Owner's account: it is NOT PROVEN end to end. See README.md ("Gemini") for the
one command that proves it on the hub. If it does not hold, the cost is that the
Gemini ring reads unavailable -- never a wrong number.

TWO REFUSALS BEFORE ANYTHING STARTS, both because `authenticate` can do harm:

  * it CLEARS the cached credentials when the requested method differs from
    `security.auth.selectedType` in settings.json. This script therefore asks
    for `oauth-personal` and refuses to run unless that is what settings say.
  * with no usable cached credential it would begin an INTERACTIVE login. A
    box with no oauth_creds.json is refused instead.

Output is one status line. Nothing the CLI said is printed: its frames can carry
account details, and this unit runs every four minutes forever.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time

ACP_PROTOCOL_VERSION = 1
DEAD_LOGIN = ("the CLI asked for an interactive sign-in, so the stored login "
              "cannot be renewed - sign in again")
AUTH_METHOD = "oauth-personal"          # AuthType.LOGIN_WITH_GOOGLE in gemini-cli
DEFAULT_TIMEOUT_SECONDS = 60      # far under the 4-minute timer interval


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def selected_auth_type(home):
    """`security.auth.selectedType` from ~/.gemini/settings.json, or None."""
    settings = _read_json(os.path.join(home, ".gemini", "settings.json"))
    security = settings.get("security") if settings else None
    auth = security.get("auth") if isinstance(security, dict) else None
    value = auth.get("selectedType") if isinstance(auth, dict) else None
    return value if isinstance(value, str) else None


def credential_expiry(home):
    """The cached token's `expiry_date` (epoch ms), or None. The NUMBER only."""
    creds = _read_json(os.path.join(home, ".gemini", "oauth_creds.json"))
    expiry = creds.get("expiry_date") if creds else None
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        return None
    return expiry


def has_credential(home):
    creds = _read_json(os.path.join(home, ".gemini", "oauth_creds.json"))
    return bool(creds and isinstance(creds.get("access_token"), str)
                and creds.get("access_token"))


LOGIN_PROMPT = ("authorize the application", "authorization code",
                "visit the following url")


def acp_handshake(command, timeout):
    """initialize, then authenticate. Returns (ok, one-line detail, dead_login).

    The child is ALWAYS killed, and nothing it sends is returned except whether
    each request got a result or an error (and an error's numeric code). A
    non-JSON stdout line that is the CLI's interactive sign-in prompt means the
    stored login cannot be renewed: `dead_login` is then True.
    """
    env = dict(os.environ, NO_BROWSER="true")
    try:
        child = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
    except OSError as exc:
        return False, "cannot launch %s (%s)" % (command[0], type(exc).__name__), False

    frames = queue.Queue()

    def pump():
        for line in child.stdout:
            try:
                frame = json.loads(line)
            except ValueError:
                if any(marker in line.lower() for marker in LOGIN_PROMPT):
                    frames.put({"login_prompt": True})
                continue
            if isinstance(frame, dict):
                frames.put(frame)
        frames.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.time() + timeout

    def request(request_id, method, params):
        child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                      "method": method, "params": params}) + "\n")
        child.stdin.flush()
        while True:
            left = deadline - time.time()
            if left <= 0:
                return None, "%s timed out" % method
            try:
                frame = frames.get(timeout=left)
            except queue.Empty:
                return None, "%s timed out" % method
            if frame is None:
                return None, "%s: the CLI exited" % method
            if frame.get("login_prompt"):
                return None, DEAD_LOGIN
            if frame.get("id") != request_id or "method" in frame:
                continue            # a notification or a request TO us: not ours
            return frame, None

    try:
        reply, problem = request(1, "initialize", {
            "protocolVersion": ACP_PROTOCOL_VERSION, "clientCapabilities": {}})
        if problem:
            return False, problem, problem == DEAD_LOGIN
        if "error" in reply:
            return False, "initialize was refused", False
        reply, problem = request(2, "authenticate", {"methodId": AUTH_METHOD})
        if problem:
            return False, problem, problem == DEAD_LOGIN
        if "error" in reply:
            error = reply["error"]
            code = error.get("code") if isinstance(error, dict) else None
            return False, "authenticate was refused (code %s)" % (
                code if isinstance(code, int) and not isinstance(code, bool) else "?"), False
        return True, "authenticate answered", False
    except OSError as exc:
        return False, "the CLI closed its stdin (%s)" % type(exc).__name__, False
    finally:
        try:
            child.kill()
        except OSError:
            pass
        try:
            child.stdin.close()
        except OSError:
            pass


def refresh(command, home, timeout=DEFAULT_TIMEOUT_SECONDS):
    """One refresh. Returns (exit code, one-line message).

    0 = the handshake completed. 1 = it did not. 3 = REFUSED before starting.
    4 = the CLI wanted an interactive sign-in: the login is dead.
    """
    selected = selected_auth_type(home)
    if selected != AUTH_METHOD:
        return 3, ("REFUSED: security.auth.selectedType is not %r, and "
                   "`authenticate` would CLEAR the cached credentials to switch "
                   "method. Sign in with `gemini` (Login with Google) first."
                   % AUTH_METHOD)
    if not has_credential(home):
        return 3, ("REFUSED: no cached Google credential under %s/.gemini. "
                   "Without one `authenticate` starts an interactive login."
                   % home)
    before = credential_expiry(home)
    ok, detail, dead = acp_handshake(command, timeout)
    after = credential_expiry(home)
    if dead:
        return 4, "gemini-refresh: DEAD LOGIN - %s" % detail
    if not ok:
        return 1, "gemini-refresh: FAILED - %s" % detail
    if before is not None and after is not None and after > before:
        return 0, "gemini-refresh: ok - %s; the token's expiry moved forward" % detail
    return 0, ("gemini-refresh: ok - %s; the token's expiry did not change "
               "(still valid, or not renewed)" % detail)


def main(argv=None):
    home = os.environ.get("HOME") or os.path.expanduser("~")
    binary = os.environ.get("GEMINI_BIN") or "gemini"
    flag = os.environ.get("GEMINI_ACP_FLAG") or "--acp"
    try:
        timeout = int(os.environ.get("GEMINI_REFRESH_TIMEOUT_SECONDS")
                      or DEFAULT_TIMEOUT_SECONDS)
    except ValueError:
        timeout = DEFAULT_TIMEOUT_SECONDS
    code, message = refresh([binary, flag], home, timeout)
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
