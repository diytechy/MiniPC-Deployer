#!/usr/bin/env python3
"""Accept only rocker up/down over a systemd-owned local socket.

The evdev reader retains DynamicUser and no writable root state. This process
has the applier's privileges but accepts no paths, output choices or commands.
One EOF-delimited request, at most five bytes, has a two-second read deadline.
Implements: SR-028, LLR-014.
"""
import socket
import subprocess
import sys


def handle_request(data, apply):
    """Map exact direction bytes to a fixed applier invocation; return status.

    Inputs: bytes; apply(argv) returns an exit code. Unknown requests perform
    no action. Outputs: fixed ASCII success/error line. Implements: LLR-014.
    """
    if data not in (b"up\n", b"down\n"):
        return b"error\n"
    return b"ok\n" if apply(["volume", data[:-1].decode("ascii")]) == 0 else b"error\n"


def apply_volume(args):
    """Run the fixed applier; failed applies are visible in journal and reply."""
    try:
        return subprocess.run(["/usr/local/sbin/wall-audio-output", *args],
                              timeout=20, check=False, stdout=sys.stderr).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        print("volume apply failed: %s" % exc, file=sys.stderr, flush=True)
        return 1


def main():
    """Serve the single connection passed as fd 0 by systemd Accept=yes."""
    with socket.socket(fileno=0) as connection:
        connection.settimeout(2)
        data = bytearray()
        try:
            while len(data) <= 5:
                part = connection.recv(6 - len(data))
                if not part:
                    break
                data.extend(part)
            answer = handle_request(bytes(data), apply_volume)
            connection.sendall(answer)
            return 0 if answer == b"ok\n" else 1
        except OSError as exc:
            print("volume connection failed: %s" % exc, file=sys.stderr, flush=True)
            return 1


if __name__ == "__main__":
    sys.exit(main())
