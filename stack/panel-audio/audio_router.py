#!/usr/bin/env python3
"""Bounded Unix-socket shell for panel audio control.

The shipped backend is deliberately unavailable: physical evidence must settle
BlueZ authorization and PipeWire user-session ownership before any device I/O
is implemented. Tests inject a backend through the same narrow interface.
Implements: SR-023, LLR-007.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import socket
import struct
import threading
from typing import Callable, Mapping, Protocol

import switch_backend
from routing import (ALIAS, HARDWARE_ADDRESS, Device, MUTATING_METHODS,
                     SELF_RECONCILING_METHODS, SWITCH_OUTPUTS, PolicyError,
                     switch_only_authorization, validate_action,
                     validate_inventory_action)


MAX_REQUEST_BYTES = 16_384
MAX_RESPONSE_BYTES = 65_536
MAX_ID_CHARS = 64
JS_SAFE_INTEGER = 9_007_199_254_740_991
DEFAULT_BACKEND_TIMEOUT_SECONDS = 2.0
MAX_BACKEND_WORKERS = 4
# The status block that settles each self-reconciling method (see _reconcile).
OBSERVABLE_BLOCK = {"set_mute": "mute", "set_output": "switch",
                    "set_input_mute": "switch", "set_volume": "switch"}
# The one optional field an action result may carry beyond {"accepted"}: the
# sequence number the backend minted for this request. See _ensure_safe_result
# and ECHO_SEQ_FIELD.
# `seq`, `generation` and `effective` all ride behind the same opt-in flag, for
# the same reason `seq` alone did: the shipped renderer validates the action
# result as an EXACT key set, so a wider result reaches an old panel as an
# error. They are one group because a client that wants exact correlation wants
# all three -- the sequence identifies the request, the generation says which
# life of the applier it belongs to, and `effective` is the applied state the
# reply echoes (contract 2026-09-14, section 1.4).
ACTION_RESULT_OPTIONAL = {"seq", "generation", "effective"}
# The exact shape of `effective`. Every field is nullable, because the backend
# reads them off a file that may be sparse, and a guessed number here would be
# reconciled against.
EFFECTIVE_FIELDS = {"output", "inputMuted", "volume", "requestSeq", "generation"}
# OPT-IN, and that is the whole point. A client that does not ask gets exactly
# the historic {"accepted": bool} -- the shipped renderer validates that result
# as an EXACT key set and turns anything wider into an error, so echoing `seq`
# unconditionally would break every panel whose shell had not been updated
# first, in whichever order the two repos are deployed. Asking for it is one
# optional top-level request field, which an old broker ignores and an old
# client never sends.
ECHO_SEQ_FIELD = "echoSeq"
# BACKEND ERROR CODES THAT SURVIVE THE SEAM, and their public messages.
#
# Everything else a backend raises becomes `backend_failure` with a fixed
# message, because backend exception text can carry a device path or an address
# and the broker must never reflect one. These two are different in kind: they
# are enum-ish tokens this file defines, they name nothing about the hardware,
# and the shell has to tell them apart to say anything useful. The message is
# re-attached HERE rather than carried across the process boundary, which is why
# it cannot be influenced by whatever the child actually raised.
PRESERVED_BACKEND_CODES = {
    "backend_unavailable": "audio routing is not configured",
    # The selected output moved between the level being chosen and the request
    # being written; applying it would have set the other output's level.
    "switch_moved": "the selected output changed before the level could be applied",
}
# AND WHICH METHOD EACH CODE IS ALLOWED TO EXPLAIN (terra 2.2). A code is a
# PUBLIC DIAGNOSIS, so preserving one by its token alone lets a backend answer
# `set_output` or `status` with "the selected output changed before the level
# could be applied" -- a sentence that is only ever true of a guarded
# `set_volume`. A wrong explanation is a worse failure than a generic one,
# because an operator acts on it. None means the code may explain any method.
PRESERVED_BACKEND_METHODS = {"switch_moved": frozenset({"set_volume"})}


def _preserved_code(code: object, method: str | None) -> str | None:
    """The public code this backend failure may be reported as, or None."""
    if code not in PRESERVED_BACKEND_CODES:
        return None
    allowed = PRESERVED_BACKEND_METHODS.get(code)
    if allowed is not None and method not in allowed:
        return None
    return str(code)


class Backend(Protocol):
    """The only device-I/O seam; implementations receive validated actions."""

    def call(self, method: str, params: Mapping[str, object], cancel: threading.Event) -> object: ...

    def inventory(self, cancel: threading.Event) -> list[Device]: ...


class UnavailableBackend:
    """Safe image default until the real-panel feasibility gate is complete."""

    def call(self, method: str, params: Mapping[str, object], cancel: threading.Event) -> object:
        if method == "status":
            return {
                "protocolVersion": 1, "available": False, "reason": "probe-required",
                "devices": [], "route": None, "visualizer": {"available": False},
            }
        if method == "telemetry":
            return {"available": False}
        raise BrokerError("backend_unavailable", "audio routing is not configured")

    def inventory(self, cancel: threading.Event) -> list[Device]:
        return []


class BrokerError(Exception):
    """A stable IF-015 error with a non-sensitive public message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _isolated_backend_entry(connection, backend: Backend, operation: str,
                            method: str | None, params: Mapping[str, object]) -> None:
    """Run one trusted backend operation in a process the broker can reap."""
    cancel = threading.Event()
    try:
        result = (backend.inventory(cancel) if operation == "inventory"
                  else backend.call(str(method), params, cancel))
        connection.send(("result", result))
    except BaseException as error:
        code = (error.code if isinstance(error, BrokerError) and
                error.code in PRESERVED_BACKEND_CODES else "backend_failure")
        connection.send(("error", code))
    finally:
        connection.close()


class DurableMutationState:
    """Atomic one-record mutation journal; pending state is deliberately sticky.

    `pending` also records the METHOD that was in flight. It is not used to
    decide whether the device changed -- nothing in the journal can know that --
    but to decide who is entitled to answer the question. For a pairing, only an
    operator can; for a mute, the mixer control itself can, and the broker asks
    it. See routing.SELF_RECONCILING_METHODS and AudioRouter._reconcile.
    """

    def __init__(self, path: str | None = None, generation: int = 0):
        if not _safe_integer(generation):
            raise ValueError("generation must be a JavaScript-safe non-negative integer")
        self.path = Path(path) if path else None
        self.data = {"version": 1, "generation": generation, "pending": None, "completed": None}
        if self.path and self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(loaded, dict) or set(loaded) != set(self.data) or
                    loaded.get("version") != 1 or not _safe_integer(loaded.get("generation")) or
                    loaded.get("pending") is not None and not isinstance(loaded.get("pending"), dict) or
                    loaded.get("completed") is not None and not isinstance(loaded.get("completed"), dict)):
                raise ValueError("audio mutation state is invalid")
            self.data = loaded

    @property
    def generation(self) -> int:
        return self.data["generation"]

    @property
    def uncertain(self) -> bool:
        return self.data["pending"] is not None

    @property
    def pending_method(self) -> str | None:
        """The in-flight method, or None. Absent in journals written before the
        method was recorded, which therefore reconcile the old way: by hand."""
        record = self.data["pending"]
        return record.get("method") if isinstance(record, dict) else None

    def completed(self, signature: str) -> dict | None:
        record = self.data["completed"]
        return record["response"] if isinstance(record, dict) and record.get("signature") == signature else None

    def begin(self, signature: str, method: str) -> None:
        changed = {**self.data, "pending": {"signature": signature, "method": method}}
        self._write(changed); self.data = changed

    def resolve(self) -> None:
        """Clear a pending record that has been settled by observing the device.

        Deliberately does NOT touch `generation` or `completed`. The mutation was
        never confirmed, so it must not be reported as having landed: the client
        keeps the generation it holds, and the truth about the device is whatever
        the next `status` reads off it.
        """
        changed = {**self.data, "pending": None}
        self._write(changed); self.data = changed

    def finish(self, signature: str, response: dict, *, increment: bool) -> None:
        if increment:
            if self.generation >= JS_SAFE_INTEGER:
                raise BrokerError("generation_exhausted", "generation limit reached")
        changed = {**self.data, "generation": self.generation + (1 if increment else 0),
                   "pending": None, "completed": {"signature": signature, "response": response}}
        self._write(changed); self.data = changed

    def _write(self, data: dict) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".new")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, separators=(",", ":"), sort_keys=True, allow_nan=False)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)


def _safe_integer(value: object) -> bool:
    return (not isinstance(value, bool) and isinstance(value, int) and
            0 <= value <= JS_SAFE_INTEGER)


class AudioBroker:
    """Decode, validate and dispatch exactly one bounded IF-015 request."""

    def __init__(
        self, backend: Backend, generation: int = 0,
        authorize: Callable[[str, Mapping[str, object]], bool] | None = None,
        *, state_path: str | None = None,
        backend_timeout_seconds: float = DEFAULT_BACKEND_TIMEOUT_SECONDS,
        isolate_backend: bool = True,
    ):
        self.backend = backend
        self.authorize = authorize or (lambda _method, _params: False)
        if (isinstance(backend_timeout_seconds, bool) or
                not isinstance(backend_timeout_seconds, (int, float)) or
                not 0.05 <= backend_timeout_seconds <= 120):
            raise ValueError("backend timeout outside 0.05..120 seconds")
        self.backend_timeout_seconds = float(backend_timeout_seconds)
        self.isolate_backend = isolate_backend is True
        self.state = DurableMutationState(state_path, generation)
        self._mutation_lock = threading.Lock()
        self._backend_slots = threading.BoundedSemaphore(MAX_BACKEND_WORKERS)

    @property
    def generation(self) -> int:
        return self.state.generation

    def handle(self, raw: bytes) -> bytes:
        request_id: object = None
        request_generation: object = None
        try:
            if len(raw) > MAX_REQUEST_BYTES:
                raise BrokerError("request_too_large", "request exceeds limit")
            if not raw.endswith(b"\n"):
                raise BrokerError("bad_request", "request must end with newline")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise BrokerError("bad_request", "request must be an object")
            if (not {"id", "method", "params", "generation"} <= set(request) or
                    set(request) - {"id", "method", "params", "generation", ECHO_SEQ_FIELD}):
                raise BrokerError("bad_request", "request fields are not exact")
            echo_seq = request.get(ECHO_SEQ_FIELD, False)
            if not isinstance(echo_seq, bool):
                raise BrokerError("bad_request", "echoSeq must be boolean")
            request_id = request["id"]
            request_generation = request["generation"]
            if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
                raise BrokerError("bad_request", "invalid request id")
            if len(str(request_id)) > MAX_ID_CHARS:
                raise BrokerError("bad_request", "request id too long")
            if not _safe_integer(request_generation):
                raise BrokerError("bad_request", "invalid generation")
            if isinstance(request_id, int) and not _safe_integer(request_id):
                raise BrokerError("bad_request", "invalid request id")
            method, params = request["method"], request["params"]
            if not isinstance(method, str) or not isinstance(params, dict):
                raise BrokerError("bad_request", "method and params have wrong type")
            validate_action(method, params)
            signature = hashlib.sha256(json.dumps(
                {"id": request_id, "method": method, "params": params, "generation": request_generation},
                separators=(",", ":"), sort_keys=True, allow_nan=False,
            ).encode()).hexdigest()
            if method in MUTATING_METHODS:
                response = self._mutation(request_id, request_generation, method, params, signature)
                # Stripped AFTER the journal, never before it: the seq is
                # persisted with the completed response, so a retry of the same
                # request replays the same number rather than minting a second
                # one for a change that already happened.
                if not echo_seq:
                    response = self._without_seq(response)
            else:
                if request_generation != self.generation:
                    raise BrokerError("stale_generation", "request generation is stale")
                result = self._backend_call("call", method, params)
                if method == "telemetry":
                    if not self._mutation_lock.acquire(timeout=self.backend_timeout_seconds):
                        raise BrokerError("broker_busy", "another mutation is still running")
                    try:
                        if self.generation != request_generation:
                            raise BrokerError("stale_generation", "telemetry generation changed during capture")
                        if isinstance(result, dict) and result.get("available") is True:
                            result = {**result, "generation": request_generation}
                        self._ensure_safe_result(method, result)
                        response = {"id": request_id, "generation": request_generation,
                                    "ok": True, "result": result}
                    finally:
                        self._mutation_lock.release()
                else:
                    self._ensure_safe_result(method, result)
                    response = {"id": request_id, "generation": self.generation,
                                "ok": True, "result": result}
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            response = self._error(request_id, self.generation, "bad_json", "invalid JSON")
        except PolicyError as exc:
            response = self._error(request_id, self.generation, "policy_refused", str(exc))
        except BrokerError as exc:
            response = self._error(request_id, self.generation, exc.code, str(exc))
        except Exception:
            # Backend exception text can contain a device path/address. Preserve
            # observability through a stable code without reflecting it.
            response = self._error(request_id, self.generation,
                                   "backend_failure", "audio backend failed")
        encoded = (json.dumps(response, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(encoded) > MAX_RESPONSE_BYTES:
            encoded = (json.dumps(self._error(request_id, self.generation,
                       "response_too_large", "backend response exceeds limit"),
                       separators=(",", ":")) + "\n").encode()
        return encoded

    def _mutation(self, request_id: object, request_generation: int, method: str,
                  params: Mapping[str, object], signature: str) -> dict:
        if not self._mutation_lock.acquire(timeout=self.backend_timeout_seconds):
            raise BrokerError("broker_busy", "another mutation is still running")
        try:
            replaying = False
            cached = self.state.completed(signature)
            if cached is not None:
                if (not isinstance(cached, dict) or set(cached) != {"id", "generation", "ok", "result"} or
                        cached.get("id") != request_id or cached.get("generation") != self.generation or
                        cached.get("ok") is not True):
                    raise BrokerError("unsafe_state", "persisted mutation result is invalid")
                self._ensure_safe_result(method, cached.get("result"))
                if self._effect_survived(cached.get("result")):
                    return cached
                # THE JOURNAL OUTLIVED THE EFFECT (terra 4.1). This broker's
                # completed record is in StateDirectory and the request it
                # acknowledged is one file in RuntimeDirectory, which systemd
                # removes when the unit stops. A restart between the write and
                # the applier consuming it leaves a journal saying the switch
                # moved and nothing anywhere that will move it. Replaying that
                # acknowledgement is the one case where deduplication lies, so
                # the request is dispatched again instead -- which is safe
                # precisely for these verbs, because every one of them is
                # idempotent: asking for the position, the microphone state or
                # the level that was already asked for changes nothing twice.
                replaying = True
            if self.state.uncertain:
                self._reconcile()
            # A replay carries the generation it was first sent with, which the
            # first completion has already advanced past. It is the SAME logical
            # request, so it is neither stale nor allowed to advance it again.
            if not replaying and request_generation != self.generation:
                raise BrokerError("stale_generation", "request generation is stale")
            # A REDO DOES NOT ADVANCE THE GENERATION, so the exhaustion ceiling
            # must not refuse it (terra 5.2): a request whose own success took
            # the counter to the limit would otherwise become undeliverable
            # exactly when its effect had been lost.
            if not replaying and self.generation >= JS_SAFE_INTEGER:
                raise BrokerError("generation_exhausted", "generation limit reached")
            if not self.authorize(method, params):
                raise BrokerError("authorization_required", "authorization is required")
            inventory = self._backend_call("inventory")
            if (not isinstance(inventory, list) or len(inventory) > 64 or
                    any(not isinstance(device, Device) for device in inventory)):
                raise BrokerError("unsafe_backend_result", "backend inventory is invalid")
            validate_inventory_action(method, params, inventory)
            self.state.begin(signature, method)
            # Once intent is durable, no backend exception proves that the
            # device remained unchanged. Leave pending sticky for reconciliation.
            result = self._backend_call("call", method, params)
            self._ensure_safe_result(method, result)
            accepted = result["accepted"] is True
            increment = accepted and not replaying
            response = {"id": request_id, "generation": self.generation + (1 if increment else 0),
                        "ok": True, "result": result}
            self.state.finish(signature, response, increment=increment)
            return response
        finally:
            self._mutation_lock.release()

    def _effect_survived(self, result: object) -> bool:
        """Whether the effect a completed reply acknowledged is still in force.

        Only a result carrying a `seq` makes a checkable claim, and only a
        backend that offers the internal `request_landed` observation can answer
        it; anything else is taken at its word, exactly as before. The
        observation is dispatched through the ordinary backend seam, like
        `_reconcile`'s `status`, so a backend that hangs answering it is still
        bounded and killable. It is NOT an IF-015 method: `routing.METHODS` does
        not contain it, so no client can ask for it.
        """
        if not isinstance(result, dict) or "seq" not in result:
            return True
        if not hasattr(self.backend, "request_landed"):
            return True
        try:
            observed = self._backend_call("call", "request_landed", {"seq": result["seq"]})
        except BrokerError:
            # Failing to look is not the same as having looked, and the safe
            # answer here is the one that re-does an idempotent request.
            return False
        return isinstance(observed, dict) and observed.get("accepted") is True

    def _reconcile(self) -> None:
        """Settle a pending journal entry, or refuse until an operator does.

        Called with the mutation lock held. Raises mutation_uncertain unless the
        in-flight method was one whose true state can simply be read back, in
        which case observing the device IS the reconciliation and the entry is
        cleared. An observation that itself fails leaves the entry pending --
        failing to look is not the same as having looked.
        """
        method = self.state.pending_method
        if method not in SELF_RECONCILING_METHODS:
            raise BrokerError("mutation_uncertain", "previous mutation requires operator reconciliation")
        # WHICH BLOCK COUNTS AS HAVING LOOKED depends on what was in flight. A
        # lost `set_output` is not settled by reading the mute control: the
        # switch has three positions and the mute block cannot express two of
        # them. Reconciling on the wrong block would be the sticky-pending
        # failure dressed up as an observation.
        observable = OBSERVABLE_BLOCK[method]
        observed = self._backend_call("call", "status", {})
        self._ensure_safe_result("status", observed)
        # A VALID STATUS IS NOT AN OBSERVATION OF THE THING WE LOST. `mute` is
        # optional in the status schema, so a backend that predates it, or one
        # whose output has no switch to read, returns a perfectly valid status
        # carrying no answer at all -- and clearing the journal on that would be
        # the sticky-pending failure dressed up as reconciliation: the broker
        # would resume mutations claiming to know a state it never looked at.
        # Self-reconciling means the control could be READ, not merely that
        # something replied.
        block = observed.get(observable) if isinstance(observed, dict) else None
        if not isinstance(block, dict) or block.get("supported") is not True:
            raise BrokerError("mutation_uncertain",
                              "%s state could not be observed; reconciliation requires an operator"
                              % observable)
        self.state.resolve()

    def _backend_call(self, operation: str, method: str | None = None,
                      params: Mapping[str, object] | None = None) -> object:
        if not self._backend_slots.acquire(timeout=self.backend_timeout_seconds):
            raise BrokerError("backend_busy", "audio backend capacity is exhausted")
        if self.isolate_backend:
            try:
                return self._isolated_backend_call(operation, method, params or {})
            finally:
                self._backend_slots.release()
        # White-box tests may opt into this in-process seam to inspect their
        # fake. Production serve() never does: Python cannot kill a stuck thread.
        cancel = threading.Event(); done = threading.Event(); outcome: dict[str, object] = {}

        def invoke() -> None:
            try:
                outcome["result"] = (self.backend.inventory(cancel) if operation == "inventory"
                                     else self.backend.call(str(method), params or {}, cancel))
            except BaseException as error:  # contained and never reflected to the peer
                outcome["error"] = error
            finally:
                done.set(); self._backend_slots.release()

        threading.Thread(target=invoke, daemon=True, name="panel-audio-backend").start()
        if not done.wait(self.backend_timeout_seconds):
            cancel.set()
            raise BrokerError("backend_timeout", "audio backend exceeded its deadline")
        if "error" in outcome:
            failure = outcome["error"]
            preserved = (_preserved_code(failure.code, method)
                         if isinstance(failure, BrokerError) else None)
            if preserved is not None:
                raise BrokerError(preserved, PRESERVED_BACKEND_CODES[preserved])
            raise BrokerError("backend_failure", "audio backend failed")
        return outcome.get("result")

    def _isolated_backend_call(self, operation: str, method: str | None,
                               params: Mapping[str, object]) -> object:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=_isolated_backend_entry,
            args=(child, self.backend, operation, method, params),
            name="panel-audio-backend",
            daemon=True,
        )
        try:
            process.start(); child.close()
            if not parent.poll(self.backend_timeout_seconds):
                process.terminate(); process.join(0.5)
                if process.is_alive():
                    process.kill(); process.join(0.5)
                if process.is_alive():
                    raise BrokerError("backend_busy", "audio backend could not be reaped")
                raise BrokerError("backend_timeout", "audio backend exceeded its deadline")
            try:
                kind, value = parent.recv()
            except EOFError:
                raise BrokerError("backend_failure", "audio backend failed")
            process.join(0.5)
            if process.is_alive():
                process.terminate(); process.join(0.5)
            if process.is_alive():
                process.kill(); process.join(0.5)
            if process.is_alive():
                raise BrokerError("backend_busy", "audio backend could not be reaped")
            if kind == "error":
                # The child sends only a token; the PARENT decides whether that
                # token may explain the method it dispatched.
                preserved = _preserved_code(value, method)
                if preserved is not None:
                    raise BrokerError(preserved, PRESERVED_BACKEND_CODES[preserved])
                raise BrokerError("backend_failure", "audio backend failed")
            if kind != "result":
                raise BrokerError("backend_failure", "audio backend failed")
            return value
        finally:
            parent.close()
            child.close()
            if process.is_alive():
                process.kill(); process.join(0.5)

    @staticmethod
    def _without_seq(response: dict) -> dict:
        """The historic reply shape, for a client that did not ask for `seq`.

        Strips the whole correlation group, not just `seq`: an old renderer
        validates the action result as an exact key set, so leaving `generation`
        or `effective` behind would reach it as an error (contract 2026-09-14,
        section 1.4).
        """
        result = response.get("result")
        if not isinstance(result, dict) or not (set(result) & ACTION_RESULT_OPTIONAL):
            return response
        return {**response, "result": {key: value for key, value in result.items()
                                       if key not in ACTION_RESULT_OPTIONAL}}

    def _error(self, request_id: object, generation: object, code: str, message: str) -> dict:
        return {"id": request_id, "generation": generation, "ok": False,
                "error": {"code": code, "message": message}}

    # The status block that describes the panel's own Mute/Headset/Speaker
    # switch (item 23 step 2). Optional for exactly the reason `mute` is: a
    # panel with no switch, or a backend that predates one, must stay valid.
    #
    # `available` is what the red icon renders: false with output "headset"
    # means the adapter is absent and every output is silent (Owner ruling 7),
    # which the shell must show differently from "playing quietly". `reason` is
    # a short enum-ish token, never a device name or a card id.
    #
    # `generation` and `requestSeq` were added by the intent contract
    # (2026-09-14, section 1.3) and are OPTIONAL for exactly the reason the
    # whole `switch` block is: a backend that predates them must stay valid.
    SWITCH_FIELDS = {"supported", "output", "inputMuted", "available", "reason", "volume"}
    # `inputMuteHeld` and `inputMutedConfirmed` were added by item J and are
    # optional on the same rule.
    SWITCH_OPTIONAL_FIELDS = {"generation", "requestSeq",
                              "inputMuteHeld", "inputMutedConfirmed"}

    def _safe_switch(self, value: object) -> None:
        """Validate the switch status block. Implements: SR-028, LLR-015."""
        if (not isinstance(value, dict) or not self.SWITCH_FIELDS <= set(value) or
                set(value) - self.SWITCH_FIELDS - self.SWITCH_OPTIONAL_FIELDS):
            raise BrokerError("unsafe_backend_result", "switch status is not exact")
        # Null is legal and means "this broker could not read it"; a number must
        # be one a JavaScript client can hold exactly, or the renderer's
        # Number.isSafeInteger guard would skip the comparison it depends on.
        if value.get("generation") is not None and not _safe_integer(value["generation"]):
            raise BrokerError("unsafe_backend_result", "switch generation is invalid")
        mark = value.get("requestSeq")
        if mark is not None and mark != -1 and not _safe_integer(mark):
            raise BrokerError("unsafe_backend_result", "switch request mark is invalid")
        if value["output"] not in SWITCH_OUTPUTS:
            raise BrokerError("unsafe_backend_result", "switch output is invalid")
        for field in ("supported", "inputMuted", "available"):
            if not isinstance(value[field], bool):
                raise BrokerError("unsafe_backend_result", "switch state is invalid")
        for field in ("inputMuteHeld", "inputMutedConfirmed"):
            if field in value and not isinstance(value[field], bool):
                raise BrokerError("unsafe_backend_result", "switch mute coupling is invalid")
        if value["reason"] is not None:
            self._safe_string(value["reason"], 32)
        volume = value["volume"]
        if isinstance(volume, bool) or not isinstance(volume, int) or not 0 <= volume <= 100:
            raise BrokerError("unsafe_backend_result", "switch volume is invalid")

    def _ensure_safe_result(self, method: str, value: object) -> None:
        """Validate a positive, method-specific response schema."""
        if not isinstance(value, dict):
            raise BrokerError("unsafe_backend_result", "backend result has wrong type")
        if method == "status":
            # `mute` is OPTIONAL where every other field is exact. A backend that
            # predates mute, or one on a panel whose output has no switch to
            # throw, omits it and stays valid; absent means "this panel cannot
            # tell you", which the shell must render differently from "not
            # muted". Making it required would have invalidated every existing
            # backend for a field most of them cannot answer.
            required = {"protocolVersion", "available", "reason", "devices", "route", "visualizer"}
            if not required <= set(value) or set(value) - required - {"mute", "switch"}:
                raise BrokerError("unsafe_backend_result", "status fields are not exact")
            if value["protocolVersion"] != 1 or not isinstance(value["available"], bool):
                raise BrokerError("unsafe_backend_result", "status version or availability is invalid")
            self._safe_string(value["reason"], 64)
            if not isinstance(value["devices"], list) or len(value["devices"]) > 64:
                raise BrokerError("unsafe_backend_result", "device list is invalid")
            for device in value["devices"]:
                self._safe_device(device)
            if value["route"] is not None:
                self._safe_route(value["route"])
            visualizer = value["visualizer"]
            if not isinstance(visualizer, dict) or set(visualizer) - {"available", "active"}:
                raise BrokerError("unsafe_backend_result", "visualizer status is invalid")
            if not isinstance(visualizer.get("available"), bool):
                raise BrokerError("unsafe_backend_result", "visualizer availability is invalid")
            if "active" in visualizer and not isinstance(visualizer["active"], bool):
                raise BrokerError("unsafe_backend_result", "visualizer activity is invalid")
            if "mute" in value:
                mute = value["mute"]
                if not isinstance(mute, dict) or set(mute) != {"supported", "muted"}:
                    raise BrokerError("unsafe_backend_result", "mute status is invalid")
                if not isinstance(mute["supported"], bool) or not isinstance(mute["muted"], bool):
                    raise BrokerError("unsafe_backend_result", "mute state is invalid")
            if "switch" in value:
                self._safe_switch(value["switch"])
        elif method == "telemetry":
            if value == {"available": False}:
                return
            expected = {"available", "generation", "observedMonotonicMs", "active", "rms", "peak", "bands"}
            # `microphone` is OPTIONAL, for exactly the reason the status block's
            # `mute` and `switch` are: a backend that predates item L, or a panel
            # with no echo canceller installed, must stay valid. Absent means
            # "this panel cannot tell you", which the shell must render
            # differently from a microphone at zero.
            if not expected <= set(value) or set(value) - expected - {"microphone"}:
                raise BrokerError("unsafe_backend_result", "telemetry fields are not exact")
            if value["available"] is not True:
                raise BrokerError("unsafe_backend_result", "telemetry fields are not exact")
            if "microphone" in value:
                self._safe_microphone(value["microphone"])
            if not _safe_integer(value["generation"]) or not _safe_integer(value["observedMonotonicMs"]):
                raise BrokerError("unsafe_backend_result", "telemetry counters are invalid")
            if not isinstance(value["active"], bool):
                raise BrokerError("unsafe_backend_result", "telemetry activity is invalid")
            for field in ("rms", "peak"):
                if (isinstance(value[field], bool) or not isinstance(value[field], (int, float)) or
                        not 0 <= value[field] <= 1):
                    raise BrokerError("unsafe_backend_result", "telemetry level is invalid")
            bands = value["bands"]
            if (not isinstance(bands, list) or not 1 <= len(bands) <= 16 or
                    any(isinstance(item, bool) or not isinstance(item, (int, float)) or
                        not 0 <= item <= 1 for item in bands)):
                raise BrokerError("unsafe_backend_result", "telemetry bands are invalid")
        elif ("accepted" not in value or set(value) - {"accepted"} - ACTION_RESULT_OPTIONAL or
                not isinstance(value["accepted"], bool)):
            raise BrokerError("unsafe_backend_result", "action result fields are not exact")
        else:
            # A seq a client cannot hold exactly is worse than none: the shell
            # compares it against the applier's mark with Number.isSafeInteger,
            # so anything outside that range would silently never settle. Null
            # is legal -- the legacy `set_mute` no-op mints no request, so there
            # is honestly nothing to correlate against.
            if value.get("seq") is not None and not _safe_integer(value["seq"]):
                raise BrokerError("unsafe_backend_result", "action result sequence is invalid")
            if value.get("generation") is not None and not _safe_integer(value["generation"]):
                raise BrokerError("unsafe_backend_result", "action result generation is invalid")
            if value.get("effective") is not None:
                self._safe_effective(value["effective"])

    def _safe_effective(self, value: object) -> None:
        """Validate the echoed applied state. Implements: SR-028, LLR-015."""
        if not isinstance(value, dict) or set(value) != EFFECTIVE_FIELDS:
            raise BrokerError("unsafe_backend_result", "effective state is not exact")
        if value["output"] not in SWITCH_OUTPUTS:
            raise BrokerError("unsafe_backend_result", "effective output is invalid")
        if value["inputMuted"] is not None and not isinstance(value["inputMuted"], bool):
            raise BrokerError("unsafe_backend_result", "effective input mute is invalid")
        volume = value["volume"]
        if volume is not None and (isinstance(volume, bool) or not isinstance(volume, int)
                                   or not 0 <= volume <= 100):
            raise BrokerError("unsafe_backend_result", "effective volume is invalid")
        mark = value["requestSeq"]
        if mark is not None and mark != -1 and not _safe_integer(mark):
            raise BrokerError("unsafe_backend_result", "effective request mark is invalid")
        if value["generation"] is not None and not _safe_integer(value["generation"]):
            raise BrokerError("unsafe_backend_result", "effective generation is invalid")

    # Item L. Every field is checked, and the two that carry a CLAIM -- `source`
    # and `state` -- are enums rather than strings, because the whole point of
    # this block is that a raw or stale level can never be presented as a live
    # post-filter one.
    MICROPHONE_FIELDS = {"level", "source", "state", "ageMs", "valid", "referenceDbfs"}
    MICROPHONE_SOURCES = {"aec_post_filter", "raw_capture", "none"}
    MICROPHONE_STATES = {"live", "muted", "stale", "unavailable"}

    def _safe_microphone(self, value: object) -> None:
        """Validate the post-filter microphone block. Implements: SR-028, LLR-015."""
        if not isinstance(value, dict) or set(value) != self.MICROPHONE_FIELDS:
            raise BrokerError("unsafe_backend_result", "microphone telemetry is not exact")
        level = value["level"]
        if (isinstance(level, bool) or not isinstance(level, (int, float))
                or not 0 <= level <= 1):
            raise BrokerError("unsafe_backend_result", "microphone level is invalid")
        if value["source"] not in self.MICROPHONE_SOURCES:
            raise BrokerError("unsafe_backend_result", "microphone source is invalid")
        if value["state"] not in self.MICROPHONE_STATES:
            raise BrokerError("unsafe_backend_result", "microphone state is invalid")
        if not _safe_integer(value["ageMs"]):
            raise BrokerError("unsafe_backend_result", "microphone freshness is invalid")
        if not isinstance(value["valid"], bool):
            raise BrokerError("unsafe_backend_result", "microphone validity is invalid")
        reference = value["referenceDbfs"]
        if (isinstance(reference, bool) or not isinstance(reference, (int, float))
                or not -120 <= reference <= 0):
            raise BrokerError("unsafe_backend_result", "microphone reference is invalid")
        # A BLOCK THAT CLAIMS VALIDITY MUST ALSO CLAIM A LIVE STATE. The two are
        # separate fields so the shell can say WHY a ring is not drawn, and a
        # backend that let them disagree would be handing the renderer a
        # contradiction to resolve on the glass.
        if value["valid"] and value["state"] != "live":
            raise BrokerError("unsafe_backend_result", "microphone validity contradicts its state")

    def _safe_string(self, value: object, limit: int) -> None:
        if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 for char in value):
            raise BrokerError("unsafe_backend_result", "backend string is invalid")
        if (HARDWARE_ADDRESS.search(value) or
                re.search(r"(?i)dev_[0-9a-f]{2}(?:_[0-9a-f]{2}){5}|/org/bluez(?:/|$)|data:audio/", value)):
            raise BrokerError("unsafe_backend_result", "backend string exposes forbidden identity or audio")

    def _safe_device(self, value: object) -> None:
        if not isinstance(value, dict) or set(value) - {"alias", "name", "kind", "trusted", "connected", "battery"}:
            raise BrokerError("unsafe_backend_result", "device fields are invalid")
        required = {"alias", "name", "kind", "trusted", "connected"}
        if not required <= set(value):
            raise BrokerError("unsafe_backend_result", "device fields are incomplete")
        self._safe_string(value["alias"], 48); self._safe_string(value["name"], 96)
        if not ALIAS.fullmatch(value["alias"]):
            raise BrokerError("unsafe_backend_result", "device alias is invalid")
        if value["kind"] not in {"input", "output"}:
            raise BrokerError("unsafe_backend_result", "device kind is invalid")
        if not isinstance(value["trusted"], bool) or not isinstance(value["connected"], bool):
            raise BrokerError("unsafe_backend_result", "device state is invalid")
        battery = value.get("battery")
        if battery is not None and (isinstance(battery, bool) or not isinstance(battery, int) or not 0 <= battery <= 100):
            raise BrokerError("unsafe_backend_result", "device battery is invalid")

    def _safe_route(self, value: object) -> None:
        if not isinstance(value, dict) or set(value) != {"input", "output"}:
            raise BrokerError("unsafe_backend_result", "route fields are invalid")
        for alias in value.values():
            if alias is not None:
                self._safe_string(alias, 48)
                if not ALIAS.fullmatch(alias):
                    raise BrokerError("unsafe_backend_result", "route alias is invalid")


class BoundedUnixServer:
    """Small bounded concurrent Unix server with per-client read deadlines."""

    def __init__(self, path: str, broker: AudioBroker, *, max_clients: int = 4,
                 client_timeout_seconds: float = 2.0, allowed_uid: int | None = None):
        self.path, self.broker = Path(path), broker
        self.client_timeout_seconds = client_timeout_seconds
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self._slots = threading.BoundedSemaphore(max_clients)
        self._stop = threading.Event()
        self._socket: socket.socket | None = None

    def serve_forever(self) -> None:
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("Unix-domain sockets are required")
        if self.path.exists(): self.path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket = listener
        listener.bind(str(self.path)); os.chmod(self.path, 0o660); listener.listen(8)
        listener.settimeout(0.2)
        while not self._stop.is_set():
            try: connection, _ = listener.accept()
            except socket.timeout: continue
            except OSError:
                if self._stop.is_set(): break
                raise
            if not self._slots.acquire(blocking=False):
                connection.close(); continue
            threading.Thread(target=self._client, args=(connection,), daemon=True).start()

    def _client(self, connection: socket.socket) -> None:
        try:
            connection.settimeout(self.client_timeout_seconds)
            if hasattr(socket, "SO_PEERCRED"):
                _pid, uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != self.allowed_uid: return
            data = bytearray()
            while not data.endswith(b"\n") and len(data) <= MAX_REQUEST_BYTES:
                part = connection.recv(min(4096, MAX_REQUEST_BYTES + 1 - len(data)))
                if not part: break
                data.extend(part)
            connection.sendall(self.broker.handle(bytes(data)))
        except (socket.timeout, BrokenPipeError, ConnectionResetError):
            pass
        finally:
            connection.close(); self._slots.release()

    def close(self) -> None:
        self._stop.set()
        if self._socket is not None: self._socket.close()
        try: self.path.unlink()
        except FileNotFoundError: pass


def serve(socket_path: str, backend: Backend | None = None, *, state_path: str | None = None) -> None:
    """Serve IF-015 on one filesystem Unix socket; never binds an IP address.

    The shipped backend is the SWITCH applier backend (item 23 step 5): it moves
    the panel's own Mute/Headset/Speaker switch, the microphone button and the
    level, through the root applier, and routes no device at all. WSN-024's
    routed-device backend stays unimplemented -- `inventory` is empty, so every
    pair/connect/select verb is still refused. On a panel where the applier is
    not installed the backend refuses with backend_unavailable, which is what
    UnavailableBackend used to say for every method.
    """
    server = BoundedUnixServer(socket_path, AudioBroker(
        backend or switch_backend.backend_from_environment(),
        # Switch verbs only. Device routing keeps the deny-by-default it has
        # always had; see routing.switch_only_authorization for why the two
        # questions get different answers.
        authorize=switch_only_authorization, state_path=state_path))
    try: server.serve_forever()
    finally: server.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--state", required=True)
    args = parser.parse_args()
    serve(args.socket, state_path=args.state)


if __name__ == "__main__":
    main()
