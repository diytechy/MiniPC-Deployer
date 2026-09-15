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


# The longest accepted request is b"down:100\n" / b"snap:100\n". Read one byte
# more than that so an over-long request is SEEN to be over-long and refused,
# rather than being silently truncated into a valid prefix.
MAX_REQUEST = len(b"down:100\n")


def handle_request(data, apply):
    """Map exact direction bytes to a fixed applier invocation; return status.

    Inputs: bytes; apply(argv) returns an exit code. Unknown requests perform
    no action. Outputs: fixed ASCII success/error line. Implements: LLR-014.

    Three spellings, and NOTHING ELSE reaches the applier:
      b"up\\n" / b"down\\n"          one press of the applier's default step
      b"up:PCT\\n" / b"down:PCT\\n"  one movement of PCT percent, PCT in 1..100
      b"snap:PCT\\n"                round the level to the nearest multiple of
                                   PCT and settle there -- sent once, when the
                                   rocker is released, so the level comes to
                                   rest on a round number

    The magnitude form exists because one apply costs ~520 ms end to end, so a
    ramp made of default-sized presses cannot keep up with the rocker and the
    surplus presses queue -- the level then keeps moving after release. Letting
    the daemon say HOW FAR in a single request is what removes the queue.

    The parse stays deliberately narrow: the direction is matched against a
    literal pair, the size must be ASCII digits within range, and the value
    handed to the applier is REBUILT from the parsed parts rather than passed
    through, so no byte of the request text can reach argv unexamined.
    """
    if data in (b"up\n", b"down\n"):
        return b"ok\n" if apply(["volume", data[:-1].decode("ascii")]) == 0 else b"error\n"
    if not data.endswith(b"\n") or len(data) > MAX_REQUEST:
        return b"error\n"
    direction, separator, size = data[:-1].partition(b":")
    if not separator or direction not in (b"up", b"down", b"snap"):
        return b"error\n"
    # bytes.isdigit() is ASCII-only, so unlike str.isdigit() it cannot admit a
    # unicode digit here. Leading "+"/"-" and whitespace are refused with it.
    if not size.isdigit() or not 1 <= int(size) <= 100:
        return b"error\n"
    argument = "%s:%d" % (direction.decode("ascii"), int(size))
    return b"ok\n" if apply(["volume", argument]) == 0 else b"error\n"


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
            # One byte past the longest legal request, so an over-long one is
            # refused by handle_request rather than truncated into a valid
            # prefix. Was a flat 6 when b"down\n" was the longest form.
            limit = MAX_REQUEST + 1
            while len(data) < limit:
                part = connection.recv(limit - len(data))
                if not part:
                    break
                data.extend(part)
                if data.endswith(b"\n"):
                    break
            answer = handle_request(bytes(data), apply_volume)
            connection.sendall(answer)
            return 0 if answer == b"ok\n" else 1
        except OSError as exc:
            print("volume connection failed: %s" % exc, file=sys.stderr, flush=True)
            return 1


if __name__ == "__main__":
    sys.exit(main())
