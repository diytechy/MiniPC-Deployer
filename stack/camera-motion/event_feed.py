#!/usr/bin/env python3
"""event_feed.py - the hub detector's door event feed (IF-079, SR-146).

The Owner ruled on 2026-10-26 (HomeHub open-items A78) that the panel takes its
door events from the hub instead of decoding the stream itself, so one detector
owns door motion. This is the hub's half: a small TCP listener inside the
detector's own select loop (camera_motion.py run) that tells the panel what the
engine just saw. The panel's half subscribes (OfficeWallNaglight
doorstream/service.py, DOORBELL_MOTION_SOURCE=hub).

WIRE. JSON lines, UTF-8, each at most 256 bytes including its newline:

    {"v":1,"type":"hello","camera":"door"}                   once, on connect
    {"v":1,"type":"motion","camera":"door","cause":"approach","id":17,"utc_ms":N}
    {"v":1,"type":"beat","utc_ms":N,"frame_age_ms":180}       every 5 s

`cause` is one of approach road-stop visitor bridge road-hold. There is never an
image, a zone or a credential on the wire, and a connection never sees anything
that happened before it: no backlog. A beat tells a subscriber the feed is alive
while the door is quiet, and how old the detector's newest frame is.

A FEED FAULT NEVER TOUCHES DETECTION. Everything here is non-blocking, bounded
and caught: the listener binds the hub's LAN address only, accepts only PANEL_IP
(any other peer is closed at accept), allows two clients, keeps at most 8 KiB
queued per client and drops THAT client when it falls behind, and reads client
input only to notice EOF - it is discarded, never parsed. A bind failure is a
warning and the detector runs on; an unexpected exception disables the feed for
the rest of the run, with one warning, and nothing else.

Python 3.8-compatible (the dev PC's test runner imports this module).
Implements: SR-146, LLR-1448..LLR-1453.
"""

from __future__ import annotations

import json
import re
import socket
import time
from typing import Callable, Dict, List, Optional, Set

PROTOCOL = 1
CAUSES = ("approach", "road-stop", "visitor", "bridge", "road-hold")
MAX_LINE = 256            # bytes, newline included
MAX_CLIENTS = 2
CLIENT_BUFFER = 8192      # bytes queued for one client before it is dropped
BEAT_SEC = 5.0
PORT_LOW, PORT_HIGH = 1024, 65535
ACCEPTS_PER_PASS = 8      # a flood of connects cannot hold the detector's loop
IPV4_RE = re.compile(r"(25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})(\.(25[0-5]|2[0-4][0-9]|1?[0-9]{1,2})){3}")
REFUSAL_LOG_SEC = 60.0


def _noop_log(message: str, warning: bool = False) -> None:
    return None


def encode(obj: Dict[str, object]) -> bytes:
    """One wire line; ValueError when it would exceed MAX_LINE."""
    line = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
    if len(line) > MAX_LINE:
        raise ValueError("a feed line is at most %d bytes" % MAX_LINE)
    return line


class _Client:
    __slots__ = ("sock", "out")

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.out = bytearray()


class EventFeed:
    """The listener. `port` 0 builds nothing: no socket is ever created."""

    def __init__(self, camera: str, host: str, port: int, peer: str,
                 log: Callable[..., None] = _noop_log,
                 mono: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        self.camera, self.host, self.port, self.peer = camera, host, int(port), peer
        self.log, self.mono, self.wall = log, mono, wall
        self.listener = None  # type: Optional[socket.socket]
        self.clients = []  # type: List[_Client]
        self.sent = 0       # lines queued to a client
        self.dropped = 0    # clients dropped for falling behind (overflow)
        self.next_id = 0
        self.disabled = False
        self.last_beat = 0.0
        self.last_refusal_log = float("-inf")

    # ── lifecycle ───────────────────────────────────────────────────────────

    def open(self) -> bool:
        """Bind and listen. False (and a warning, for a requested port) when there
        is no feed; never raises."""
        if self.port == 0:
            return False
        if not (PORT_LOW <= self.port <= PORT_HIGH):
            self.log("camera motion [%s]: event feed off - the port is outside %d..%d"
                     % (self.camera, PORT_LOW, PORT_HIGH), warning=True)
            return False
        if not IPV4_RE.fullmatch(self.host or "") or not IPV4_RE.fullmatch(self.peer or ""):
            self.log("camera motion [%s]: event feed off - it needs LAN_IP and PANEL_IP, both IPv4 addresses"
                     % self.camera, warning=True)
            return False
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.listen(MAX_CLIENTS)
            sock.setblocking(False)
        except (OSError, OverflowError) as exc:
            if sock is not None:
                self._close_quietly(sock)
            self.log("camera motion [%s]: event feed off - could not listen on its port (%s); the detector "
                     "runs on" % (self.camera, type(exc).__name__), warning=True)
            return False
        self.listener = sock
        self.last_beat = self.mono()
        self.log("camera motion [%s]: event feed listening on the LAN address, port %d"
                 % (self.camera, self.port))
        return True

    @property
    def active(self) -> bool:
        return self.listener is not None and not self.disabled

    def close(self) -> None:
        for client in self.clients:
            self._close_quietly(client.sock)
        self.clients = []
        if self.listener is not None:
            self._close_quietly(self.listener)
            self.listener = None

    @staticmethod
    def _close_quietly(sock: socket.socket) -> None:
        try:
            sock.close()
        except Exception:  # noqa: BLE001 - closing must not be able to fail
            pass

    def _fault(self, where: str, exc: BaseException) -> None:
        """An unexpected exception: the feed ends for this run, the detector does not."""
        self.disabled = True
        self.close()
        try:
            self.log("camera motion [%s]: event feed disabled after %s in %s; detection is unaffected"
                     % (self.camera, type(exc).__name__, where), warning=True)
        except Exception:  # noqa: BLE001
            pass

    # ── what the detector's select asks ─────────────────────────────────────

    def read_fds(self) -> List[socket.socket]:
        """Sockets to wait on for reading: the listener, and each client (EOF)."""
        if not self.active:
            return []
        return [self.listener] + [c.sock for c in self.clients]  # type: ignore[list-item]

    def write_fds(self) -> List[socket.socket]:
        """Sockets with bytes still queued."""
        if not self.active:
            return []
        return [c.sock for c in self.clients if c.out]

    def service(self, readable: Set[object], writable: Set[object]) -> None:
        """Handle what select reported. Never raises."""
        if not self.active:
            return
        try:
            if self.listener in readable:
                self._accept()
            for client in list(self.clients):
                if client.sock in readable:
                    self._drain(client)
                if client in self.clients and client.sock in writable:
                    self._flush(client)
        except Exception as exc:  # noqa: BLE001
            self._fault("service", exc)

    # ── publishing ──────────────────────────────────────────────────────────

    def publish(self, cause: str, utc_ms: int) -> None:
        """Every engine signal, at once, unthrottled. Never raises, never blocks."""
        if not self.active or cause not in CAUSES:
            return
        try:
            self.next_id += 1
            self._broadcast(encode({"v": PROTOCOL, "type": "motion", "camera": self.camera, "cause": cause,
                                    "id": self.next_id, "utc_ms": int(utc_ms)}))
        except Exception as exc:  # noqa: BLE001
            self._fault("publish", exc)

    def tick(self, mono: float, frame_age_ms: int) -> None:
        """The 5 s beat. Never raises."""
        if not self.active or mono - self.last_beat < BEAT_SEC:
            return
        try:
            self.last_beat = mono
            self._broadcast(encode({"v": PROTOCOL, "type": "beat", "utc_ms": int(self.wall() * 1000),
                                    "frame_age_ms": max(0, int(frame_age_ms))}))
        except Exception as exc:  # noqa: BLE001
            self._fault("tick", exc)

    def status(self) -> Dict[str, int]:
        """The status JSON's feed block. `port` is the port actually listening (0: none)."""
        return {"port": self.port if self.active else 0, "clients": len(self.clients),
                "sent": self.sent, "dropped": self.dropped}

    # ── internals ───────────────────────────────────────────────────────────

    def _broadcast(self, line: bytes) -> None:
        for client in list(self.clients):
            self._queue(client, line)

    def _queue(self, client: _Client, line: bytes) -> None:
        if len(client.out) + len(line) > CLIENT_BUFFER:
            self.dropped += 1
            self._remove(client)
            return
        client.out.extend(line)
        self.sent += 1
        self._flush(client)

    def _flush(self, client: _Client) -> None:
        while client.out:
            try:
                count = client.sock.send(bytes(client.out))
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self._remove(client)
                return
            if count <= 0:
                return
            del client.out[:count]

    def _drain(self, client: _Client) -> None:
        """Read and discard. EOF or a hard error ends the client."""
        try:
            data = client.sock.recv(1024)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._remove(client)
            return
        if not data:
            self._remove(client)

    def _remove(self, client: _Client) -> None:
        if client in self.clients:
            self.clients.remove(client)
        self._close_quietly(client.sock)

    def _accept(self) -> None:
        for _ in range(ACCEPTS_PER_PASS):
            try:
                conn, addr = self.listener.accept()  # type: ignore[union-attr]
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if addr[0] != self.peer:
                self._close_quietly(conn)
                now = self.mono()
                if now - self.last_refusal_log >= REFUSAL_LOG_SEC:
                    self.last_refusal_log = now
                    self.log("camera motion [%s]: event feed closed a connection from a peer that is not "
                             "PANEL_IP" % self.camera, warning=True)
                continue
            if len(self.clients) >= MAX_CLIENTS:
                self._close_quietly(conn)
                continue
            try:
                conn.setblocking(False)
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                # A small send buffer keeps a client that never reads from hiding
                # behind the kernel's: it falls behind our 8 KiB bound promptly.
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, CLIENT_BUFFER)
            except OSError:
                pass
            client = _Client(conn)
            self.clients.append(client)
            self._queue(client, encode({"v": PROTOCOL, "type": "hello", "camera": self.camera}))
