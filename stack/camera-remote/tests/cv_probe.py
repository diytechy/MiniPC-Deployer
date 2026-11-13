#!/usr/bin/env python3
"""cv_probe.py - camera-view-lab.sh's client: one HTTPS request or one websocket.

Standard library only (the lab runs it under WSL's Python 3.12 and nothing is
installed). It connects to ADDRESS:PORT, sends SNI and Host NAME, and prints
one JSON line. The certificate is not verified (the lab's Caddy issues from
its own local CA).

    cv_probe.py http ADDRESS PORT NAME PATH [--auth USER:PASS | --auth-stdin]
        -> {"status": 302, "location": "..."}
    cv_probe.py ws ADDRESS PORT NAME PATH [--auth USER:PASS | --auth-stdin] [--mse SECONDS]
        -> {"status": 101, "text": [...first text frames...], "binary_bytes": N}
           With --mse it sends {"type":"mse"} after the upgrade, as go2rtc's
           player does, and reads frames for SECONDS.

The password arrives in argv here, which is fine for a lab password and is
why check 11's process scan looks for the RELAY's credential, not this one.
On the hub's trial (HomeHub's runbook) the real shared login must not: there,
--auth-stdin reads one line USER:PASS from stdin instead.
"""

import base64
import json
import os
import socket
import ssl
import struct
import sys
import time


def connect(address, port, name):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(["http/1.1"])
    raw = socket.create_connection((address, int(port)), timeout=10)
    return ctx.wrap_socket(raw, server_hostname=name)


def head_lines(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1]) if lines and len(lines[0].split()) > 1 else 0
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return status, headers, rest


def request(sock, name, path, auth, extra=()):
    lines = ["GET %s HTTP/1.1" % path, "Host: %s" % name, "User-Agent: cv-probe"]
    if auth:
        lines.append("Authorization: Basic " + base64.b64encode(auth.encode()).decode())
    lines += list(extra)
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))


def http(address, port, name, path, auth):
    sock = connect(address, port, name)
    request(sock, name, path, auth, ["Connection: close"])
    status, headers, _ = head_lines(sock)
    sock.close()
    return {"status": status, "location": headers.get("location", "")}


def read_exact(sock, n, rest):
    while len(rest) < n:
        chunk = sock.recv(65536)
        if not chunk:
            raise EOFError
        rest += chunk
    return rest[:n], rest[n:]


def ws(address, port, name, path, auth, mse_seconds):
    sock = connect(address, port, name)
    key = base64.b64encode(os.urandom(16)).decode()
    request(sock, name, path, auth, ["Upgrade: websocket", "Connection: Upgrade", "Sec-WebSocket-Version: 13",
                                     "Sec-WebSocket-Key: " + key, "Origin: https://%s" % name])
    status, _, rest = head_lines(sock)
    out = {"status": status, "text": [], "binary_bytes": 0, "first_binary_s": None}
    started = time.monotonic()
    if status != 101 or not mse_seconds:
        sock.close()
        return out
    payload = json.dumps({"type": "mse", "value": ""}).encode()
    mask = os.urandom(4)
    frame = bytes([0x81, 0x80 | len(payload)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(frame)
    deadline = time.monotonic() + float(mse_seconds)
    sock.settimeout(1.0)
    try:
        while time.monotonic() < deadline:
            try:
                hdr, rest = read_exact(sock, 2, rest)
            except socket.timeout:
                continue
            opcode, length = hdr[0] & 0x0F, hdr[1] & 0x7F
            if length == 126:
                ext, rest = read_exact(sock, 2, rest)
                length = struct.unpack(">H", ext)[0]
            elif length == 127:
                ext, rest = read_exact(sock, 8, rest)
                length = struct.unpack(">Q", ext)[0]
            body, rest = read_exact(sock, length, rest)
            if opcode == 1 and len(out["text"]) < 5:
                out["text"].append(body.decode("utf-8", "replace"))
            elif opcode == 2:
                if out["first_binary_s"] is None:
                    out["first_binary_s"] = round(time.monotonic() - started, 2)
                out["binary_bytes"] += length
            elif opcode == 8:
                break
    except (EOFError, OSError):
        pass
    sock.close()
    return out


def main(argv):
    kind, address, port, name, path = argv[1:6]
    auth = argv[argv.index("--auth") + 1] if "--auth" in argv else ""
    if "--auth-stdin" in argv:   # the real login: never in argv (HomeHub's trial)
        auth = sys.stdin.readline().rstrip("\r\n")
    mse = argv[argv.index("--mse") + 1] if "--mse" in argv else ""
    try:
        result = http(address, port, name, path, auth) if kind == "http" else ws(address, port, name, path, auth, mse)
    except OSError as exc:
        result = {"status": -1, "error": type(exc).__name__}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
