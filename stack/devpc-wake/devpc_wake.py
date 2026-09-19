#!/usr/bin/env python3
"""devpc-wake — measured readiness for the dev PC, and the wake that gets it there.

Implements: SR-044, LLR-966, LLR-967, LLR-968, LLR-969, LLR-970, LLR-974, LLR-978
Interfaces: IF-021 (inference), IF-022 (wake actuation), IF-023 (state we serve),
            IF-025 (session signal we consume), IF-026 (sleep verdict we publish)

THE ONE IDEA THIS FILE EXISTS TO PROTECT: `up` is not `ready`.

A woken machine answers ICMP tens of seconds before it can answer a prompt,
because the weights still have to reach VRAM. A caller told `ready` when the
truth is `up` will block on a completion and then blame the model. So the state
is decided by TWO layers that are never collapsed into one truthiness check:

    reachability   ICMP or TCP to the inference port   ->  off / up
    readiness      a model listing naming the model    ->  up  / ready

A reachability TIMEOUT is `off`, never `up` - a powered-off box times out, and
calling that `up` suppresses the very wake this service exists to fire. A
READINESS timeout against a host that IS answering leaves the state at `up`,
because the box is demonstrably alive and merely not servable yet. Those two
timeouts look identical in a log and mean opposite things.

Python 3 stdlib only, like ai_cli_service.py next door. No new apt package.
"""
from __future__ import annotations

import enum
import json
import os
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request


class State(str, enum.Enum):
    OFF = "off"
    WAKING = "waking"
    UP = "up"
    READY = "ready"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Wake actuation, behind a seam.
# ---------------------------------------------------------------------------
# IF-022. The magic-packet provider is UNVERIFIED on this hardware and may never
# work: the dev PC's physical NIC is bound to a Hyper-V external vSwitch, which
# has historically consumed the magic packet before the NIC's wake filter sees
# it - and the driver reports every wake filter armed either way, so the arming
# evidence is not evidence. The seam exists so that a failed physical test costs
# one provider rather than the whole service.


class WakeProvider:
    """One way to make the box come back. Fire-and-forget: no acknowledgement."""

    name = "abstract"

    def fire(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class MagicPacketProvider(WakeProvider):
    name = "magic-packet"

    def __init__(self, mac: str, broadcast: str = "255.255.255.255", port: int = 9):
        cleaned = mac.replace("-", "").replace(":", "").replace(".", "").strip()
        if len(cleaned) != 12:
            raise ValueError(
                "DEVPC_WAKE_MAC must be a 12-hex-digit MAC of the PHYSICAL adapter. "
                "Do NOT resolve it from the host address: on this box the LAN "
                "address lives on a vEthernet adapter, not on the NIC, so an "
                "ARP-derived MAC targets the virtual switch and wakes nothing."
            )
        self._mac = bytes.fromhex(cleaned)
        self._broadcast = broadcast
        self._port = port

    def fire(self) -> None:
        frame = b"\xff" * 6 + self._mac * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(frame, (self._broadcast, self._port))


class CommandProvider(WakeProvider):
    """The escape hatch: a smart plug, a BMC, anything with a command line.

    Exists because the magic-packet path through the vSwitch may simply not
    work. Swapping to this must not require touching the state machine.
    """

    name = "command"

    def __init__(self, argv: list):
        if not argv:
            raise ValueError("DEVPC_WAKE_COMMAND is empty")
        self._argv = argv

    def fire(self) -> None:
        subprocess.run(self._argv, check=True, timeout=30)


# ---------------------------------------------------------------------------
# Probing.
# ---------------------------------------------------------------------------


def reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """Layer one. True iff something answers. A timeout is NOT reachable."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        # Refused, timed out, unroutable - all mean 'nothing is answering here'.
        # They are distinguished in the journal, never in the return value,
        # because every one of them is `off` for state purposes.
        return False


def model_loaded(base_url: str, target_model: str, timeout: float = 5.0):
    """Layer two. Returns True/False, or None when the listing did not answer.

    None and False are DIFFERENT and callers must not merge them:
      False -> the server answered and the model is not loaded  -> up
      None  -> the listing itself failed or timed out           -> up
    Both are `up`, but only because layer one already said the host answers.
    Neither is ever `ready`, and neither is ever `off`.
    """
    url = base_url.rstrip("/") + "/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    names = {m.get("id") for m in body.get("data", []) if isinstance(m, dict)}
    if not target_model:
        # No target configured: a 200 listing is the most we can honestly claim,
        # and claiming `ready` on it would be inventing a guarantee.
        return False
    return target_model in names


# ---------------------------------------------------------------------------
# The flight: single-flight wake, one linearization point, no I/O under the lock.
# ---------------------------------------------------------------------------


class WakeFlight:
    """One in-flight wake for one target. LLR-974.

    THE LOCK DISCIPLINE IS THE WHOLE POINT AND IS EASY TO BREAK LATER:

      Under the lock  -- read state, join or start, publish terminal state,
                         detach the flight, SNAPSHOT the waiters.
      Outside it      -- fire the wake, probe, publish to the broker, serve
                         HTTP, notify waiters.

    Two failures this prevents. (a) Re-entrancy: notifying a waiter while
    holding the lock runs its continuation synchronously, and a continuation
    that reads state or asks for another wake re-enters and DEADLOCKS. (b)
    Unbounded hold: firing a packet and probing are network I/O, and doing them
    under the lock turns a bounded critical section into a wait as long as the
    slowest operation - stalling every joiner and every state read, including
    the health endpoint that would explain why.
    """

    def __init__(self, deadline_epoch: float):
        self.deadline = deadline_epoch
        self.done = threading.Event()
        self.outcome = None

    def expired(self, now: float) -> bool:
        return now >= self.deadline


class WakeService:
    def __init__(self, cfg, provider: WakeProvider, clock=time.time):
        self.cfg = cfg
        self.provider = provider
        self._clock = clock
        self._lock = threading.Lock()
        self._flight = None
        self._last_wake_ok = None

    # -- state ------------------------------------------------------------
    def state(self) -> dict:
        """Determine the state. Precedence is FIXED and ordered (LLR-966).

        1. an expired wake deadline -> failed, outranking every probe, because a
           wake that did not happen must not be masked by a stale reading;
        2. reachability, because an unreachable host cannot be ready whatever a
           cached listing once said;
        3. readiness.
        """
        now = self._clock()

        with self._lock:
            flight = self._flight
            if flight is not None and flight.expired(now):
                # Terminal publication and removal happen TOGETHER, so no
                # arrival can interleave between them and observe a removed
                # flight beside a not-yet-updated state.
                self._flight = None
                flight.outcome = State.FAILED
                waiters = [flight]
            else:
                waiters = []
        for f in waiters:
            f.done.set()  # outside the lock, always

        if waiters:
            return self._doc(State.FAILED, now, "wake deadline expired")

        with self._lock:
            waking = self._flight is not None

        if not reachable(self.cfg.host, self.cfg.port, self.cfg.probe_timeout):
            return self._doc(State.WAKING if waking else State.OFF, now,
                             "wake in flight" if waking else "no probe answered")

        loaded = model_loaded(self.cfg.base_url, self.cfg.target_model,
                              self.cfg.probe_timeout)
        if loaded is True:
            return self._doc(State.READY, now, "target model loaded")
        reason = ("listing did not answer" if loaded is None
                  else "answering, target model not loaded")
        return self._doc(State.UP, now, reason)

    def _doc(self, state: State, now: float, reason: str) -> dict:
        # Every document carries the instant it was determined, so a stale
        # document is distinguishable from a current one rather than read as
        # fresh (LLR-970).
        return {"state": state.value, "reason": reason, "observed_at": now,
                "target": self.cfg.host, "model": self.cfg.target_model}

    # -- wake -------------------------------------------------------------
    def request_wake(self):
        """Join the in-flight wake, or start exactly one. Returns the flight."""
        now = self._clock()
        start = False
        with self._lock:
            if self._flight is not None and not self._flight.expired(now):
                return self._flight  # joiner: inherits the deadline, cannot refresh it
            self._flight = WakeFlight(now + self.cfg.deadline_seconds)
            flight = self._flight
            start = True

        if start:
            # I/O strictly outside the lock.
            try:
                self.provider.fire()
                self._last_wake_ok = True
            except Exception:
                self._last_wake_ok = False
                with self._lock:
                    if self._flight is flight:
                        self._flight = None
                    flight.outcome = State.FAILED
                flight.done.set()
        return flight

    # -- sleep verdict (IF-026): we decide, we NEVER actuate ---------------
    def sleep_verdict(self, last_request_epoch: float, session) -> dict:
        """Publish permit/deny. The dev PC polls this and sleeps itself.

        `session` is the IF-025 reading, or None when it is absent/unreadable.
        FAIL CLOSED MEANS ATTACHED (LLR-978): absent, stale, malformed and
        unreachable all inhibit sleep. A false `attached` costs idle watts until
        the next poll; a false `detached` suspends a machine somebody is using
        and loses their work.
        """
        now = self._clock()
        idle_for = now - last_request_epoch
        if idle_for < self.cfg.idle_sleep_seconds:
            return self._verdict(False, now, "idle period not elapsed")

        if session is None:
            return self._verdict(False, now, "session signal unavailable - treated as attached")
        age = now - session.get("observed_at", 0)
        if age > self.cfg.session_staleness_seconds:
            return self._verdict(False, now, "session signal stale - treated as attached")
        if session.get("attached") is not False:
            # Anything that is not an explicit False - True, missing, a string,
            # a malformed body - is attached. Only an explicit, fresh, well-formed
            # `attached: false` permits sleep.
            return self._verdict(False, now, "session attached")
        return self._verdict(True, now, "idle elapsed and no session attached")

    def _verdict(self, permit: bool, now: float, reason: str) -> dict:
        return {"permit": permit, "reason": reason, "observed_at": now}
