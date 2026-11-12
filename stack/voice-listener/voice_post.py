#!/usr/bin/env python3
"""voice_post.py - the hub's side of MINI-SERV's action interface (IF-085).

The living-room PC's scene service (EntertainmentPC src/SceneService, rules in
src/Scenes.Core/Action*.cs; voice-placement hand-off section 5.3) takes a
transcript at POST /v1/utterance and reports itself at GET /v1/state
(bruno.state). This module builds the one document the hub sends, sends it,
and reads the two answers. Nothing else of that interface is used here.

THE TRANSCRIPT is what someone in the house said. It exists in this module only
as the argument of post_utterance and inside the request body on the socket. It
is never logged, never put in an exception or a result, never written, and
never queued: a transcript that cannot be delivered within the deadline is
dropped, because a command that arrives a minute late is worse than none.

THE TOKEN is the interface's bearer token (T3). It is handed in, kept in the
client, sent only in the Authorization header to the one configured address,
and never appears in a message, a result or repr().

A RETRY REUSES THE REQUEST ID. MINI-SERV keeps the last 256 answers for ten
minutes and answers a repeat from them without running anything, so a request
whose answer was lost is safe to send again with the same id, and only so.

Plain HTTP on the LAN, as the interface is built today (its TLS is a later
step on MINI-SERV's side).

Python 3.8-compatible; standard library only.
Implements: SR-167, LLR-1538..LLR-1540.
"""

from __future__ import annotations

import http.client
import io
import json
import re
import socket
import time
import uuid
from typing import Callable, Dict, Optional, Tuple

WIRE_VERSION = 1
MAX_BODY_BYTES = 8192            # ActionWire.MaxBodyBytes
MAX_UTTERANCE_CHARS = 500        # ActionWire.MaxUtteranceChars
TOKEN_MIN, TOKEN_MAX = 32, 256   # ActionToken
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
SOURCE_RE = re.compile(r"[a-z0-9._-]{1,40}")
MAX_ANSWER_BYTES = 16384

# UtteranceOutcomes (ActionWire.cs): what MINI-SERV may say it did with a transcript.
OUTCOMES = ("started", "already_running", "failed", "ambiguous", "unknown", "handed_off", "not_addressed",
            "not_heard", "unavailable")

# What this module adds: the post never got an outcome. Each is a short code
# safe to count and to show; none can hold what was said.
NOT_SENT_TEXT = "not-sent-text"                  # the text broke the interface's rules (too long, a control character)
REJECTED = "rejected-%d"                         # MINI-SERV refused the request: the status says which guard
UNDELIVERED_NOT_READY = "undelivered-not-ready"  # 503 until the deadline
UNDELIVERED_UNREACHABLE = "undelivered-unreachable"
ANSWER_NOT_UNDERSTOOD = "answer-not-understood"

RETRY_STATUSES = (503,)
NO_RETRY_STATUSES = (400, 401, 403, 404, 405, 409, 413, 415)


class Invalid(ValueError):
    """A value cannot be used. The message names the rule, never the value."""


def token_ok(token: str) -> bool:
    """One line of 32 to 256 visible ASCII characters (ActionToken's rule)."""
    return isinstance(token, str) and TOKEN_MIN <= len(token) <= TOKEN_MAX and all(0x21 <= ord(c) <= 0x7E for c in token)


def new_request_id() -> str:
    return str(uuid.uuid4())


def text_problem(text: str) -> Optional[str]:
    """Why the interface would refuse this text, or None (ActionWire.PlainText)."""
    if not isinstance(text, str) or not text.strip():
        return "empty"
    if len(text) > MAX_UTTERANCE_CHARS:
        return "longer than %d characters" % MAX_UTTERANCE_CHARS
    if any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in text):
        return "holds a control character"
    return None


def utterance_document(request_id: str, text: str, wake_checked: bool, source: str = "hub") -> bytes:
    """The body of POST /v1/utterance, exactly the fields version 1 takes.

    Contract:
      Inputs:  request_id: 1..64 of A-Z a-z 0-9 . _ -; text: the transcript,
               1..500 characters, no control character; wake_checked: True
               when the transcript does not include the wake phrase (the hub's
               wake-word engine already heard it), False when it does and
               MINI-SERV's matcher must find it there; source: who is asking.
      Outputs: UTF-8 JSON, at most 8192 bytes.
      Raises:  Invalid naming the rule.
    Implements: LLR-1538.
    """
    if not REQUEST_ID_RE.fullmatch(request_id or ""):
        raise Invalid("request_id must be 1 to 64 of A-Z, a-z, 0-9, '.', '_' and '-'")
    if not SOURCE_RE.fullmatch(source or ""):
        raise Invalid("source must be 1 to 40 of a-z, 0-9, '.', '_' and '-'")
    problem = text_problem(text)
    if problem:
        raise Invalid("text is " + problem)
    if not isinstance(wake_checked, bool):
        raise Invalid("wake_checked must be true or false")
    body = json.dumps({"v": WIRE_VERSION, "request_id": request_id, "text": text, "wake_checked": wake_checked,
                       "source": source}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_BODY_BYTES:
        raise Invalid("the body is larger than %d bytes" % MAX_BODY_BYTES)
    return body


class PostResult:
    """What became of one transcript: a code, never the text."""

    __slots__ = ("outcome", "status", "attempts", "replayed", "action_kind")

    def __init__(self, outcome: str, status: int = 0, attempts: int = 0, replayed: bool = False,
                 action_kind: str = ""):
        self.outcome, self.status, self.attempts, self.replayed = outcome, status, attempts, replayed
        self.action_kind = action_kind   # "category", "menu", ... : the kind only, never the id

    @property
    def delivered(self) -> bool:
        return self.outcome in OUTCOMES

    def __repr__(self) -> str:
        return "PostResult(%s, status=%d, attempts=%d)" % (self.outcome, self.status, self.attempts)


def classify_answer(status: int, body: bytes) -> Tuple[str, bool, str, Optional[float]]:
    """(outcome code, replayed, action kind, retry after seconds or None) for one HTTP answer.

    200 carries an outcome the interface names; anything else it says is
    "answer-not-understood". 503 is the one status worth another try, after the
    answer's retry_after_seconds. Every other refusal is final: it means the
    hub's configuration (token, source address, document) is wrong, and trying
    again cannot fix that. Implements: LLR-1539.
    """
    doc = {}   # type: Dict[str, object]
    try:
        parsed = json.loads(body.decode("utf-8")) if body else {}
        if isinstance(parsed, dict):
            doc = parsed
    except (ValueError, UnicodeDecodeError):
        doc = {}
    if status == 200:
        outcome = doc.get("outcome")
        if doc.get("v") != WIRE_VERSION or outcome not in OUTCOMES:
            return ANSWER_NOT_UNDERSTOOD, False, "", None
        action = doc.get("action")
        kind = action.split(":", 1)[0] if isinstance(action, str) and ":" in action else ""
        return str(outcome), doc.get("replayed") is True, kind if re.fullmatch(r"[a-z]{1,16}", kind) else "", None
    if status in RETRY_STATUSES:
        after = doc.get("retry_after_seconds")
        seconds = float(after) if isinstance(after, (int, float)) and not isinstance(after, bool) else 1.0
        return UNDELIVERED_NOT_READY, False, "", min(5.0, max(0.2, seconds))
    return REJECTED % status, False, "", None


class _DeadlineReader(io.RawIOBase):
    """The answer's bytes, read under one absolute deadline.

    A socket timeout bounds each receive, not the answer: a peer that sends a
    byte every second would keep http.client reading the status line, the
    headers and the body for as long as it liked, and the listener's loop (its
    hold, presence and end-date checks) waits on the post. Before each receive
    the socket's timeout is set to what is left, and nothing is read once the
    deadline has passed (review 2026-11-12). Implements: LLR-1539, LLR-1540.
    """

    def __init__(self, sock, deadline: float, clock: Callable[[], float]):
        super().__init__()
        self._sock, self._deadline, self._clock = sock, deadline, clock
        self._raw = sock.makefile("rb", buffering=0)   # keeps the socket open until the answer is closed

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        left = self._deadline - self._clock()
        if left <= 0:
            raise socket.timeout("the answer was not complete within the deadline")
        self._sock.settimeout(left)
        return self._raw.readinto(buffer)

    def close(self) -> None:
        if not self.closed:
            self._raw.close()
        super().close()


class _DeadlineSocket:
    """What http.client's response is handed in place of the socket: it only
    ever calls makefile("rb"), and gets the deadline reader."""

    def __init__(self, sock, deadline: float, clock: Callable[[], float]):
        self._sock, self._deadline, self._clock = sock, deadline, clock

    def makefile(self, *_args, **_kwargs):
        return io.BufferedReader(_DeadlineReader(self._sock, self._deadline, self._clock))


def _response_under_deadline(deadline: float, clock: Callable[[], float]):
    """A response_class for http.client.HTTPConnection whose every read of the
    answer (status line, headers, body) ends at `deadline` on `clock`."""
    def make(sock, *args, **kwargs):
        return http.client.HTTPResponse(_DeadlineSocket(sock, deadline, clock), *args, **kwargs)
    return make


class ActionClient:
    """One caller of MINI-SERV's action interface.

    Contract:
      Inputs:  host: MINI-SERV's IPv4 address; port; token (token_ok);
               source_ip: the hub's LAN address, which every connection is
               bound to, because MINI-SERV admits that address and no other;
               timeout_sec per attempt and deadline_sec for a whole post: an
               attempt's connect and send are bounded by a socket timeout, and
               reading its entire answer by an absolute deadline of the same
               length on the real clock (a state call is one attempt of
               timeout_sec); clock and sleep drive the retries only.
      Outputs: post_utterance -> PostResult; get_state -> (reachable, state,
               voice).
      Raises:  Invalid for an unusable token or address at construction.
               Never raises from post_utterance or get_state.
    Implements: LLR-1539, LLR-1540.
    """

    def __init__(self, host: str, port: int, token: str, source_ip: str = "", timeout_sec: float = 3.0,
                 deadline_sec: float = 8.0, source: str = "hub",
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 connect: Optional[Callable[..., http.client.HTTPConnection]] = None):
        if not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}", host or ""):
            raise Invalid("the peer address must be an IPv4 address")
        if not 1 <= int(port) <= 65535:
            raise Invalid("the peer port must be 1..65535")
        if not token_ok(token):
            raise Invalid("the token must be one line of %d to %d visible ASCII characters" % (TOKEN_MIN, TOKEN_MAX))
        if not SOURCE_RE.fullmatch(source):
            raise Invalid("source must be 1 to 40 of a-z, 0-9, '.', '_' and '-'")
        self.host, self.port, self.source = host, int(port), source
        self._token = token
        self.source_ip = source_ip
        self.timeout_sec, self.deadline_sec = timeout_sec, deadline_sec
        self._clock, self._sleep = clock, sleep
        self._connect = connect or http.client.HTTPConnection

    def __repr__(self) -> str:
        return "ActionClient(port %d, token not shown)" % self.port

    def _request(self, method: str, path: str, body: Optional[bytes], timeout: float) -> Tuple[int, bytes]:
        """One attempt, over within `timeout` seconds of its start: the connect
        and the send are bounded by the socket's timeout (the request is under
        8 KiB, which the kernel's send buffer takes at once), and the answer by
        an absolute deadline however slowly it arrives.

        The deadline is on the real monotonic clock, never the injected one: it
        bounds a real socket, and a test's frozen clock would stop it from ever
        coming (review 2026-11-12). The answer is closed on every path, so an
        attempt cut off mid-answer releases its socket at once."""
        deadline = time.monotonic() + timeout
        kwargs = {"timeout": timeout}   # type: Dict[str, object]
        if self.source_ip:
            kwargs["source_address"] = (self.source_ip, 0)
        conn = self._connect(self.host, self.port, **kwargs)
        try:
            conn.response_class = _response_under_deadline(deadline, time.monotonic)
            headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json",
                       "Connection": "close"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=body, headers=headers)
            answer = conn.getresponse()
            try:
                return answer.status, answer.read(MAX_ANSWER_BYTES)
            finally:
                answer.close()
        finally:
            conn.close()

    def post_utterance(self, text: str, wake_checked: bool, request_id: Optional[str] = None) -> PostResult:
        """Send one transcript; retry a lost answer or a 503 with the SAME id
        until the deadline; then drop it. The result never holds the text."""
        try:
            body = utterance_document(request_id or new_request_id(), text, wake_checked, self.source)
        except Invalid:
            return PostResult(NOT_SENT_TEXT)
        started = self._clock()
        attempts = 0
        last = PostResult(UNDELIVERED_UNREACHABLE)
        while True:
            left = self.deadline_sec - (self._clock() - started)
            if left <= 0 or attempts >= 8:
                last.attempts = attempts
                return last
            attempts += 1
            try:
                # What is left, never more: the post ends at deadline_sec, not
                # up to half a second after it as a 0.5 s floor here allowed.
                status, answer = self._request("POST", "/v1/utterance", body, min(self.timeout_sec, left))
            except (OSError, http.client.HTTPException, socket.timeout):
                last = PostResult(UNDELIVERED_UNREACHABLE)
                wait = 0.5
            else:
                outcome, replayed, kind, wait = classify_answer(status, answer)
                last = PostResult(outcome, status, attempts, replayed, kind)
                if wait is None:
                    return last
            if self.deadline_sec - (self._clock() - started) <= wait:
                last.attempts = attempts
                return last
            self._sleep(wait)

    def get_state(self) -> Tuple[bool, str, str]:
        """(reachable, state, voice) from GET /v1/state. Unreachable for any
        failure, any status but 200, or a document that is not version 1.
        Implements: LLR-1540."""
        try:
            status, answer = self._request("GET", "/v1/state", None, self.timeout_sec)
            doc = json.loads(answer.decode("utf-8")) if status == 200 else None
        except (OSError, http.client.HTTPException, socket.timeout, ValueError, UnicodeDecodeError):
            return False, "", ""
        if not isinstance(doc, dict) or doc.get("v") != WIRE_VERSION:
            return False, "", ""
        state, voice = doc.get("state"), doc.get("voice")
        clean = lambda v: v if isinstance(v, str) and re.fullmatch(r"[a-z_]{1,16}", v) else ""  # noqa: E731
        return True, clean(state), clean(voice)
