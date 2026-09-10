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
import os
from pathlib import Path
import re
import socket
import struct
import threading
from typing import Callable, Mapping, Protocol

from routing import ALIAS, Device, MUTATING_METHODS, PolicyError, validate_action, validate_inventory_action


MAX_REQUEST_BYTES = 16_384
MAX_RESPONSE_BYTES = 65_536
MAX_ID_CHARS = 64
JS_SAFE_INTEGER = 9_007_199_254_740_991
DEFAULT_BACKEND_TIMEOUT_SECONDS = 2.0
MAX_BACKEND_WORKERS = 4


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


class DurableMutationState:
    """Atomic one-record mutation journal; pending state is deliberately sticky."""

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

    def completed(self, signature: str) -> dict | None:
        record = self.data["completed"]
        return record["response"] if isinstance(record, dict) and record.get("signature") == signature else None

    def begin(self, signature: str) -> None:
        changed = {**self.data, "pending": {"signature": signature}}
        self._write(changed); self.data = changed

    def finish(self, signature: str, response: dict, *, increment: bool) -> None:
        if increment:
            if self.generation >= JS_SAFE_INTEGER:
                raise BrokerError("generation_exhausted", "generation limit reached")
        changed = {**self.data, "generation": self.generation + (1 if increment else 0),
                   "pending": None, "completed": {"signature": signature, "response": response}}
        self._write(changed); self.data = changed

    def abandon_known_noop(self) -> None:
        changed = {**self.data, "pending": None}
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
    ):
        self.backend = backend
        self.authorize = authorize or (lambda _method, _params: False)
        if (isinstance(backend_timeout_seconds, bool) or
                not isinstance(backend_timeout_seconds, (int, float)) or
                not 0.05 <= backend_timeout_seconds <= 120):
            raise ValueError("backend timeout outside 0.05..120 seconds")
        self.backend_timeout_seconds = float(backend_timeout_seconds)
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
            if set(request) != {"id", "method", "params", "generation"}:
                raise BrokerError("bad_request", "request fields are not exact")
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
            else:
                if request_generation != self.generation:
                    raise BrokerError("stale_generation", "request generation is stale")
                result = self._backend_call("call", method, params)
                self._ensure_safe_result(method, result)
                response = {"id": request_id, "generation": self.generation, "ok": True, "result": result}
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
            cached = self.state.completed(signature)
            if cached is not None:
                if (not isinstance(cached, dict) or set(cached) != {"id", "generation", "ok", "result"} or
                        cached.get("id") != request_id or cached.get("generation") != self.generation or
                        cached.get("ok") is not True):
                    raise BrokerError("unsafe_state", "persisted mutation result is invalid")
                self._ensure_safe_result(method, cached.get("result"))
                return cached
            if self.state.uncertain:
                raise BrokerError("mutation_uncertain", "previous mutation requires operator reconciliation")
            if request_generation != self.generation:
                raise BrokerError("stale_generation", "request generation is stale")
            if self.generation >= JS_SAFE_INTEGER:
                raise BrokerError("generation_exhausted", "generation limit reached")
            if not self.authorize(method, params):
                raise BrokerError("authorization_required", "authorization is required")
            inventory = self._backend_call("inventory")
            if (not isinstance(inventory, list) or len(inventory) > 64 or
                    any(not isinstance(device, Device) for device in inventory)):
                raise BrokerError("unsafe_backend_result", "backend inventory is invalid")
            validate_inventory_action(method, params, inventory)
            self.state.begin(signature)
            try:
                result = self._backend_call("call", method, params)
            except BrokerError as error:
                if error.code == "backend_unavailable":
                    self.state.abandon_known_noop()
                raise
            self._ensure_safe_result(method, result)
            accepted = result["accepted"] is True
            response_generation = self.generation + (1 if accepted else 0)
            response = {"id": request_id, "generation": response_generation,
                        "ok": True, "result": result}
            self.state.finish(signature, response, increment=accepted)
            return response
        finally:
            self._mutation_lock.release()

    def _backend_call(self, operation: str, method: str | None = None,
                      params: Mapping[str, object] | None = None) -> object:
        if not self._backend_slots.acquire(timeout=self.backend_timeout_seconds):
            raise BrokerError("backend_busy", "audio backend capacity is exhausted")
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
            if isinstance(outcome["error"], BrokerError) and outcome["error"].code == "backend_unavailable":
                raise BrokerError("backend_unavailable", "audio routing is not configured")
            raise BrokerError("backend_failure", "audio backend failed")
        return outcome.get("result")

    def _error(self, request_id: object, generation: object, code: str, message: str) -> dict:
        return {"id": request_id, "generation": generation, "ok": False,
                "error": {"code": code, "message": message}}

    def _ensure_safe_result(self, method: str, value: object) -> None:
        """Validate a positive, method-specific response schema."""
        if not isinstance(value, dict):
            raise BrokerError("unsafe_backend_result", "backend result has wrong type")
        if method == "status":
            if set(value) != {"protocolVersion", "available", "reason", "devices", "route", "visualizer"}:
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
        elif method == "telemetry":
            if value == {"available": False}:
                return
            expected = {"available", "generation", "observedMonotonicMs", "active", "rms", "peak", "bands"}
            if set(value) != expected or value["available"] is not True:
                raise BrokerError("unsafe_backend_result", "telemetry fields are not exact")
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
        elif set(value) != {"accepted"} or not isinstance(value["accepted"], bool):
            raise BrokerError("unsafe_backend_result", "action result fields are not exact")

    def _safe_string(self, value: object, limit: int) -> None:
        if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 for char in value):
            raise BrokerError("unsafe_backend_result", "backend string is invalid")
        if re.search(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])|"
                     r"(?<![0-9a-f])(?:[0-9a-f]{2}_){5}[0-9a-f]{2}(?![0-9a-f])|"
                     r"(?<![0-9a-f])[0-9a-f]{12}(?![0-9a-f])|"
                     r"dev_[0-9a-f]{2}(?:_[0-9a-f]{2}){5}|/org/bluez(?:/|$)|data:audio/", value):
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
                 client_timeout_seconds: float = 2.0):
        self.path, self.broker = Path(path), broker
        self.client_timeout_seconds = client_timeout_seconds
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
                if uid != os.getuid(): return
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
    """Serve IF-015 on one filesystem Unix socket; never binds an IP address."""
    server = BoundedUnixServer(socket_path, AudioBroker(backend or UnavailableBackend(), state_path=state_path))
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
