#!/usr/bin/env python3
"""devpc_hold_hook — hold a request while the dev PC wakes. SR-044's last piece.

Implements: SR-044, SR-047, LLR-985, LLR-988, LLR-989
Interfaces: IF-021 (inference), IF-023 (readiness we consume)
Cases:      TC-993, TC-1023, TC-1024, TC-1025

WHAT THIS IS. A LiteLLM pre-call hook. Before a request reaches the dev PC it
asks devpc-wake whether the box is `ready`; if it is not, it fires ONE wake and
waits, up to a hard cap, re-asking until the answer is `ready`. Then it returns
and the request proceeds normally.

WHY IT IS HERE AND NOT IN THE GATEWAY. FreeLLMAPI is a third-party binary and
cannot host this logic. It was specified as "a small OpenAI-compatible shim in
front of the dev PC that the gateway's custom endpoint points at" — which is
exactly what this proxy is, so the shim is a hook inside it rather than a
fourth process.

─────────────────────────────────────────────────────────────────────────────
THE ONE THING TO UNDERSTAND: THIS HOOK NEVER FALLS BACK, AND THAT IS NOT A
DEPARTURE FROM SR-044.
─────────────────────────────────────────────────────────────────────────────

SR-044 reads "held while the box wakes, falling back rather than erroring". At
the time it was written, one component was going to do both. It no longer does,
because the two lanes want opposite things from the same event:

  finance    a cold box MUST produce an error. Substituting a cloud provider
             is the failure the whole arrangement exists to prevent.
  agents     a cold box SHOULD produce an answer from somewhere.

So the hold lives here and the FALLBACK lives one layer out, in llm-gateway,
which has the twelve-platform catalogue and whose job that already is. The
system still satisfies SR-044 for the agents lane — held, then fell back — but
no single component does, and the finance lane gets fail-closed for free
because it never traverses the component that substitutes.

This is a real change to where a requirement is discharged. It is recorded in
docs/design/llm-routing-topology.md (Finance-Auditor) and carried as SR-047
rather than by quietly reinterpreting SR-044.

─────────────────────────────────────────────────────────────────────────────
THE INTEGRATION CONSTRAINT THAT IS EASY TO BREAK AND IMPOSSIBLE TO NOTICE
─────────────────────────────────────────────────────────────────────────────

When the hold expires we raise. For the AGENTS lane to fall back, the gateway
in front of us has to classify that raise as retryable — and it does so by
SUBSTRING-MATCHING OUR ERROR MESSAGE. Read from the running container,
`isRetryableError` in its proxy.js matches, among others:

    '429' 'rate limit' 'quota' 'timeout' 'etimedout' 'aborted'
    'econnrefused' 'econnreset' '503' 'unavailable' '500'

A message outside that list makes the gateway 502 the whole request instead of
trying a cloud tier — the agents lane breaks, and it breaks as "the gateway is
down", not as "the dev PC is asleep". So HOLD_EXPIRED_MESSAGE below deliberately
contains both "503" and "unavailable", and TC-1025 asserts it.

THAT ASSERTION IS AGAINST THE OTHER SIDE OF THE CONTRACT, NOT AGAINST OURSELVES.
A test that checked our message said what we wrote in our own file could never
fail. The matcher list is vendored into the test with the date and the path it
was read from, and it must be re-read when the gateway's image pin rotates.

Python 3 stdlib only apart from LiteLLM's own imports. This file is mounted
read-only into the container; it is not installed with pip.
"""
from __future__ import annotations

import asyncio
import functools
import json
import math
import os
import time
import urllib.error
import urllib.request
from typing import Any, Literal, Optional

try:  # pragma: no cover - exercised only inside the container
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # pragma: no cover - the unit tests run without litellm
    class CustomLogger:  # type: ignore[no-redef]
        """Stand-in so the decision logic below can be unit-tested off-box.

        The tests exercise `decide_hold` and `HoldPolicy`, which touch no
        LiteLLM type at all. Importing the real base class only matters inside
        the container, and pinning a test to a third-party import would make
        the suite unrunnable on a dev machine for no benefit.
        """

try:  # pragma: no cover - exercised only inside the container
    from fastapi import HTTPException
except ImportError:  # pragma: no cover
    class HTTPException(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail


# The message the caller sees when the hold cap expires. See the banner above:
# the substrings are load-bearing, not decorative.
HOLD_EXPIRED_MESSAGE = (
    "503 dev PC unavailable: held {waited:.0f}s for model '{model}' to become "
    "ready and it did not. The box did not wake, or the inference server is not "
    "running as a service, or DEVPC_TARGET_MODEL does not match a model the "
    "server lists."
)

# Returned when devpc-wake itself cannot be reached. Distinct from the above on
# purpose: one says the dev PC is not ready, the other says we cannot tell.
# Both must remain retryable to the gateway.
NO_ORACLE_MESSAGE = (
    "503 dev PC readiness unavailable: devpc-wake did not answer at {url}. "
    "Refusing to guess - a request sent to a box we cannot confirm is awake "
    "would hang for the full request timeout instead of failing in seconds."
)


# The smallest timeout the FIRST readiness probe may use. It exists so that a
# zero-budget cap still gets one honest look instead of reporting "no oracle"
# against an oracle that is fine. HoldPolicy refuses any configured probe
# timeout below it, so the documented bound - cap + probe_timeout - stays true
# rather than quietly becoming cap + max(probe_timeout, this).
FIRST_PROBE_FLOOR = 0.05


def _positive(env, name: str, default: str, allow_zero: bool = False) -> float:
    """Read a timing knob, or refuse to start.

    NaN AND ZERO ARE THE INTERESTING ONES, and both were accepted by the first
    version of this class (found by review). `DEVPC_HOLD_CAP_SECONDS=nan` makes
    every `waited >= cap` comparison permanently False, so the hold loop never
    terminates and the request hangs until the client gives up.
    `DEVPC_HOLD_POLL_SECONDS=0` spins the probe as fast as the executor allows,
    which is a hot loop inside the request path.

    Refusing at startup is the right failure: a typo in an env file should stop
    the container with a legible message, not produce a proxy that hangs.
    """
    raw = env.get(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise SystemExit("%s=%r is not a number" % (name, raw))
    if not math.isfinite(value):
        raise SystemExit(
            "%s=%r is not finite. nan in particular is silent: every "
            "comparison against it is False, so the hold would never expire "
            "and the request would hang instead of failing." % (name, raw))
    if value < 0 or (value == 0 and not allow_zero):
        raise SystemExit(
            "%s=%r must be %s" % (name, raw,
                                  "zero or greater" if allow_zero else "greater than zero"))
    return value


async def _blocking(fn, *args):
    """Run a blocking call off the event loop.

    `run_in_executor`, NOT `asyncio.to_thread`. The container runs Python 3.13
    where either would work, but this repository's test interpreter is 3.8 and
    `to_thread` landed in 3.9 - so the version with `to_thread` could not be
    DRIVEN by the suite at all, only read by it. Code the tests cannot exercise
    is code that is not tested, and the hold loop's timing is the part most
    worth exercising.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args))


def _refusal(message: str) -> Exception:
    """The refusal, as something whose TEXT survives to the wire.

    THIS WAS A BARE RuntimeError AND THAT WAS A REAL DEFECT (found by review).
    The whole handoff to the gateway is a substring match on the response body,
    and the proxy's generic unhandled-exception path answers with
    "Internal server error" - no 503, no 'unavailable'. A refusal that got
    rewritten on the way out would leave the agents lane silently without its
    cloud fallback, presenting as the gateway being broken rather than as the
    dev PC being asleep.

    fastapi.HTTPException carries an explicit status and a detail the proxy's
    handler preserves. TC-1029 asserts this against the REAL container's HTTP
    response bytes, not against this string - because asserting the constant
    here proves only that Python can format a string.
    """
    return HTTPException(status_code=503, detail=message)


class HoldPolicy:
    """Every knob, read once and VALIDATED. Defaults match stack/.env.example."""

    def __init__(self, env=os.environ):
        base = env.get("DEVPC_WAKE_URL", "").rstrip("/")
        self.wake_url = base
        # cap may be 0 - "ask once, do not wait" is a coherent setting.
        self.hold_cap_seconds = _positive(
            env, "DEVPC_HOLD_CAP_SECONDS", "120", allow_zero=True)
        # poll and probe timeout may NOT be 0; see _positive.
        self.poll_seconds = _positive(env, "DEVPC_HOLD_POLL_SECONDS", "3")
        self.probe_timeout = _positive(
            env, "DEVPC_HOLD_PROBE_TIMEOUT_SECONDS", "5")
        # THE FLOOR IS ENFORCED HERE, NOT SILENTLY IN THE LOOP.
        #
        # The first probe is floored to FIRST_PROBE_FLOOR so that a zero-budget
        # cap still gets one honest look rather than reporting "no oracle"
        # against a healthy oracle. A configured probe timeout BELOW that floor
        # therefore makes the documented bound (cap + probe_timeout) false -
        # with cap=0 and probe=0.01 the first probe may take 0.05. Found by a
        # third review round, because the parametrised bound test never tried a
        # timeout below the floor.
        #
        # Rejecting is better than widening the bound: a sub-50ms probe timeout
        # against a service on the other side of a bridge is not a setting
        # anyone wants, and a bound with a hidden floor in it is a bound nobody
        # can reason about.
        if self.probe_timeout < FIRST_PROBE_FLOOR:
            raise SystemExit(
                "DEVPC_HOLD_PROBE_TIMEOUT_SECONDS=%s is below the %ss floor the "
                "first probe uses, which would make the documented bound of "
                "cap + probe_timeout false."
                % (self.probe_timeout, FIRST_PROBE_FLOOR))
        self.model = env.get("DEVPC_TARGET_MODEL", "")
        # A hold that is enabled with no oracle to ask would block every
        # request for the full cap and then fail. Treat the absence of a URL as
        # "no hold configured" and say so at startup rather than at 2am.
        self.enabled = bool(base)


def decide_hold(state_doc: Optional[dict], waited: float, cap: float) -> str:
    """The whole decision, as a pure function. Returns one of:

        'proceed'   the box is ready; let the request through
        'wake'      nothing is coming; fire a wake and keep waiting
        'wait'      something is in progress; do not fire again, keep waiting
        'expired'   the cap is spent; raise
        'no-oracle' devpc-wake did not answer; raise a DIFFERENT error

    Kept pure and separate from the I/O for the same reason devpc_wake.py is
    split from devpc_wake_service.py: this is the part with the interesting
    behaviour, and it can be proved with a table of inputs instead of a box.

    PRECEDENCE IS ORDERED AND THE ORDER MATTERS. `ready` outranks the cap: a
    box that became ready on the very last poll should serve the request, not
    be refused by a clock. Everything else is checked after.
    """
    # NOT `is None` - ANY shape that is not a mapping is "no reading".
    #
    # `_get_state` returns whatever `json.loads` produced, and a JSON body of
    # `[]`, `"ready"` or `5` is perfectly valid JSON that is not a document.
    # The narrower `is None` let those through to `.get`, which raised
    # AttributeError straight out of `async_pre_call_hook` - and that is worse
    # than it sounds, because the whole contract with the gateway in front is
    # that our refusals carry "503"/"unavailable" so the agents lane can fall
    # back. An AttributeError carries neither, so a malformed readiness answer
    # would have turned a sleeping dev PC into "the gateway is down" for BOTH
    # lanes. Widening the existing check costs one line and makes this function
    # total over its declared argument, which its docstring already claimed.
    if not isinstance(state_doc, dict):
        return "no-oracle"

    state = state_doc.get("state")
    if state == "ready":
        return "proceed"

    if waited >= cap:
        return "expired"

    # `waking` means a flight is already in progress and owns a deadline. Firing
    # again would not start a second wake (devpc-wake is single-flight) but it
    # would be a pointless packet and a misleading log line.
    if state == "waking":
        return "wait"

    # off / failed / up / anything unrecognised -> ask for a wake.
    #
    # `up` is included ON PURPOSE and it is the subtle one: the box is awake but
    # the model is not loaded. A wake is harmless there (the packet lands on a
    # running machine and does nothing) and waiting is correct, because what we
    # are waiting for is the weights reaching VRAM, not the power state.
    #
    # An UNRECOGNISED state also asks for a wake rather than proceeding. A new
    # state name from a future devpc-wake must not be read as "fine" by an old
    # hook - the failure of guessing is a request that hangs on a sleeping box.
    return "wake"


class DevPcHoldHook(CustomLogger):
    """Hold the request while the dev PC wakes. Fail closed, never elsewhere."""

    def __init__(self, policy: Optional[HoldPolicy] = None):
        self.policy = policy or HoldPolicy()
        if not self.policy.enabled:
            print(
                "devpc-hold: DEVPC_WAKE_URL is empty - NOT holding. Requests go "
                "straight to the dev PC and a cold box will fail fast rather "
                "than waking. This is a valid configuration (it is what you "
                "want while the wake path is unverified) but it is not the "
                "SR-044 behaviour, so it says so once at startup.",
                flush=True,
            )

    # -- I/O, kept thin and replaceable so the tests never open a socket -----
    def _get_state(self, timeout: Optional[float] = None) -> Optional[dict]:
        if timeout is None:
            timeout = self.policy.probe_timeout
        try:
            req = urllib.request.Request(self.policy.wake_url + "/state")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status != 200:
                    return None
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            # Every failure is the same fact: we have no reading. Distinguishing
            # them here would invite treating one of them as "probably fine".
            return None

    def _fire_wake(self, timeout: Optional[float] = None) -> None:
        # TIMEOUT IS A PARAMETER because this used to take the full configured
        # probe timeout unconditionally. A slow wake POST near expiry then
        # added nearly five seconds on top of a cap the caller had been
        # promised - the same class of overshoot as the probe, missed the
        # first time because the test's fake wake consumed no time at all.
        if timeout is None:
            timeout = self.policy.probe_timeout
        try:
            req = urllib.request.Request(
                self.policy.wake_url + "/wake", data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=timeout):
                pass
        except (urllib.error.URLError, OSError):
            # Best effort. If the wake cannot be requested, the next _get_state
            # will keep saying `off` and the cap will expire with the honest
            # message. Raising here would lose the distinction between "asked
            # and it did not wake" and "could not ask".
            print("devpc-hold: wake request failed", flush=True)

    # -- the hook LiteLLM calls ---------------------------------------------
    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: Literal[
            "completion", "text_completion", "embeddings",
            "image_generation", "moderation", "audio_transcription",
        ],
    ) -> Optional[dict]:
        if not self.policy.enabled:
            return data

        # ONE DEADLINE, COMPUTED ONCE, AND EVERY WAIT BOUNDED BY WHAT REMAINS.
        #
        # The first version sampled `waited` BEFORE the probe and then slept a
        # full poll interval regardless, so a probe that took nearly its whole
        # timeout close to the cap overshot by up to probe_timeout +
        # poll_seconds - roughly eight seconds past a cap documented as hard
        # (found by review). The cap is a promise to the caller; a cap that is
        # "120 seconds, plus however long the last probe took" is not one.
        started = time.monotonic()
        deadline = started + self.policy.hold_cap_seconds
        fired = False
        probed = False
        while True:
            remaining = deadline - time.monotonic()

            # THE DEADLINE IS SPENT: REFUSE WITHOUT WAITING FURTHER.
            #
            # `probed` is what makes a ZERO cap mean "ask once, do not wait"
            # rather than "refuse without looking". Without it, cap=0 refused a
            # box that was sitting there READY - the documented semantics and
            # the behaviour disagreed, and a second review round caught it.
            # The cap bounds WAITING; one readiness check always happens.
            if remaining <= 0 and probed:
                waited = time.monotonic() - started
                raise _refusal(HOLD_EXPIRED_MESSAGE.format(
                    waited=waited, model=self.policy.model))

            # Bound the probe by what is left. The floor applies only to the
            # FIRST probe, where there may be no budget at all and a zero
            # timeout would report "no oracle" against a healthy oracle. After
            # that the clamp is strict, because a floor larger than the
            # remaining budget is itself an overshoot - which is exactly how a
            # 0.01s cap produced a 0.05s stop.
            if probed:
                probe_timeout = min(self.policy.probe_timeout, max(remaining, 0.0))
            else:
                probe_timeout = max(min(self.policy.probe_timeout, remaining),
                                    FIRST_PROBE_FLOOR)
            probed = True
            doc = await _blocking(self._get_state, probe_timeout)

            # Re-read the clock AFTER the probe. Deciding on a pre-probe
            # reading is what produced the overshoot.
            waited = time.monotonic() - started
            action = decide_hold(doc, waited, self.policy.hold_cap_seconds)

            if action == "proceed":
                if fired:
                    print("devpc-hold: ready after %.0fs" % waited, flush=True)
                return data

            if action == "no-oracle":
                raise _refusal(NO_ORACLE_MESSAGE.format(url=self.policy.wake_url))

            if action == "expired":
                raise _refusal(HOLD_EXPIRED_MESSAGE.format(
                    waited=waited, model=self.policy.model))

            if action == "wake" and not fired:
                # ONCE per request. devpc-wake is single-flight so a second call
                # would join rather than duplicate, but firing repeatedly turns
                # a clean journal into noise and hides the one that mattered.
                print("devpc-hold: dev PC is '%s' - requesting wake"
                      % (doc.get("state"),), flush=True)
                # Clamped to what is left, for the same reason the probe is.
                wake_budget = min(self.policy.probe_timeout,
                                  max(deadline - time.monotonic(), 0.0))
                await _blocking(self._fire_wake, wake_budget)
                fired = True

            # Sleep only as long as the deadline allows, and never past it.
            # Without the clamp the loop could sleep through the deadline and
            # only notice on the next iteration.
            nap = min(self.policy.poll_seconds,
                      max(deadline - time.monotonic(), 0.0))
            if nap <= 0:
                # The deadline is spent. Loop once more so the `expired` branch
                # above produces the refusal, rather than duplicating the
                # message here where it could drift out of step with it.
                nap = 0.0
            await asyncio.sleep(nap)


# LiteLLM dispatches INSTANCES, not classes. Naming the class in config.yaml is
# a silent no-op: the proxy starts, the hook never runs, and a cold box is
# discovered by a hanging request rather than by a startup error.
proxy_handler_instance = DevPcHoldHook()
