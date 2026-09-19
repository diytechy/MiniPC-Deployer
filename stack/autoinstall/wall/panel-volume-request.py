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


# The longest accepted request is the guarded absolute form,
# b"set:100:trigger:speaker:100:9007199254740991\n". Read one byte more than
# that so an over-long request is SEEN to be over-long and refused, rather
# than being silently truncated into a valid prefix.
MAX_REQUEST = len(b"set:100:trigger:speaker:100:9007199254740991\n")
# The relative forms, which are all this socket carried before the guarded
# target. They keep their own, tighter bound: widening MAX_REQUEST for the
# guarded form must not quietly let `down:<sixteen digits>` through the older
# parse as well.
MAX_RELATIVE_REQUEST = len(b"down:100\n")
GUARDED_MODES = (b"trigger", b"panel", b"bus")
GUARDED_OUTPUTS = (b"mute", b"headset", b"speaker")
MAX_STATE_REVISION = 9007199254740991
# Every reason the applier may answer a guarded target with. The reply is
# rebuilt from this tuple rather than forwarded, so no byte of the applier's
# stdout reaches the client unexamined either.
REFUSAL_REASONS = (b"state-changed", b"apply-failed", b"unavailable",
                   b"ambiguous")
AMBIGUOUS = b"refused:ambiguous\n"


def _digits(field, limit):
    """int(field) if it is plain ASCII digits within 0..limit, else None.

    bytes.isdigit() is ASCII-only, unlike str.isdigit(), so a unicode digit
    cannot get in. Leading "+"/"-" and whitespace are refused with it.
    """
    if not field or not field.isdigit():
        return None
    value = int(field)
    return value if value <= limit else None


def parse_guarded(data):
    """The guarded absolute form as a rebuilt argv string, or None.

    b"set:TARGET:MODE:OUTPUT:EXPECT:REVISION\n" -> "set:75:bus:speaker:70:42"

    Every field is matched against a literal set or parsed as a bounded
    integer, and the value handed to the applier is REBUILT from the parsed
    parts. That is the same rule the relative forms have always followed, and
    it is the whole reason this process exists: it holds the applier's
    privileges and accepts no path, device, unit or command.
    """
    fields = data[:-1].split(b":")
    if len(fields) != 6 or fields[0] != b"set":
        return None
    _, target, mode, output, expected, revision = fields
    if mode not in GUARDED_MODES or output not in GUARDED_OUTPUTS:
        return None
    target = _digits(target, 100)
    expected = _digits(expected, 100)
    revision = _digits(revision, MAX_STATE_REVISION)
    if target is None or expected is None or revision is None:
        return None
    return "set:%d:%s:%s:%d:%d" % (target, mode.decode("ascii"),
                                   output.decode("ascii"), expected, revision)


def guarded_reply(text):
    """The applier's stdout, re-validated and rebuilt, or the ambiguous answer.

    A LOST OR UNREADABLE VERDICT IS `ambiguous`, NOT A FAILURE, and the
    difference is the point: `apply-failed` tells the actor the level did not
    move, while `ambiguous` tells it that it does not know -- so it re-reads
    the confirmed state instead of replaying arithmetic that may already have
    landed. Guessing either way here is how a rocker applies one movement
    twice.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return AMBIGUOUS
    parts = lines[-1].encode("ascii", "replace").split(b":")
    if len(parts) == 2 and parts[0] == b"refused" and parts[1] in REFUSAL_REASONS:
        return b"refused:" + parts[1] + b"\n"
    if len(parts) == 4 and parts[0] == b"ok" and parts[1] in GUARDED_OUTPUTS:
        level = _digits(parts[2], 100)
        revision = _digits(parts[3], MAX_STATE_REVISION)
        if level is not None and revision is not None:
            return b"ok:%s:%d:%d\n" % (parts[1], level, revision)
    return AMBIGUOUS


def handle_request(data, apply, capture=None):
    """Map exact direction bytes to a fixed applier invocation; return status.

    Inputs: bytes; apply(argv) returns an exit code. Unknown requests perform
    no action. Outputs: fixed ASCII success/error line. Implements: LLR-014.

    Four spellings, and NOTHING ELSE reaches the applier:
      b"set:T:MODE:OUT:EXPECT:REV\n"
                                   ONE ABSOLUTE LEVEL, applied only while the
                                   mode, output, level and state revision it
                                   was computed on still hold. This is the
                                   only form the rocker sends now; the three
                                   below remain for `snap` and for a hand-run
                                   test. Answered with `ok:OUT:LEVEL:REV` or
                                   `refused:REASON`, never with `ok`.
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
    # THE GUARDED FORM IS ANSWERED DIFFERENTLY, and it has to be: its whole
    # purpose is to tell the caller what actually landed, so `ok\n` would
    # throw away the three facts the rocker's actor rebases on. `capture` is
    # the applier's stdout; a caller that does not supply one (every existing
    # test of the relative forms) gets the ambiguous answer rather than a
    # crash.
    if data.startswith(b"set:"):
        argument = parse_guarded(data)
        if argument is None:
            return b"error\n"
        if capture is None:
            return AMBIGUOUS
        return guarded_reply(capture(["volume", argument]))
    if len(data) > MAX_RELATIVE_REQUEST:
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


APPLIER = "/usr/local/sbin/wall-audio-output"


def apply_volume(args):
    """Run the fixed applier; failed applies are visible in journal and reply."""
    try:
        return subprocess.run([APPLIER, *args],
                              timeout=20, check=False, stdout=sys.stderr).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        print("volume apply failed: %s" % exc, file=sys.stderr, flush=True)
        return 1


def capture_volume(args):
    """Run the fixed applier and return its stdout verdict line.

    THE EXIT CODE IS DELIBERATELY NOT CONSULTED. The guarded form's whole
    answer is the line it prints -- `refused:state-changed` is a successful
    refusal and `ok:...` carries three facts a bare zero does not -- and
    reading the code as well would give two sources of truth that can
    disagree. An applier that died without printing anything prints nothing,
    which `guarded_reply` reads as `ambiguous`: the one answer that is right
    when we genuinely do not know.

    Its stderr is left attached so the journal still gets the applier's own
    lines; only stdout is captured.
    """
    try:
        done = subprocess.run([APPLIER, *args], timeout=20, check=False,
                              stdout=subprocess.PIPE, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        print("volume apply failed: %s" % exc, file=sys.stderr, flush=True)
        return ""
    return done.stdout or ""


def read_request(connection):
    """Read at most one EOF- or newline-delimited request from `connection`."""
    data = bytearray()
    # One byte past the longest legal request, so an over-long one is refused by
    # handle_request rather than truncated into a valid prefix. Was a flat 6
    # when b"down\n" was the longest form.
    limit = MAX_REQUEST + 1
    while len(data) < limit:
        part = connection.recv(limit - len(data))
        if not part:
            break
        data.extend(part)
        if data.endswith(b"\n"):
            break
    return bytes(data)


def main():
    """Serve the single connection passed as fd 0 by systemd Accept=yes.

    THE EXIT CODE REPORTS THE APPLY, NOT THE DELIVERY, and that distinction is
    what this function exists to get right. `Accept=yes` gives every connection
    its own unit instance, so anything this process calls a failure becomes a
    RED LINE on the panel's failed list -- and a failed list nobody can read is
    a failed list nobody reads.

    Two connections are NOT failures, and both used to be reported as one:

      * A CONNECT-AND-CLOSE. `panel_system_verify.py` opens this socket during
        every panel release to prove it accepts connections, and deliberately
        sends NOTHING -- an `up` there would move the amplifier. That is the
        measured cause of the two `wall-volume-request@` failures seen on every
        boot since 2026-09-14 (the release probe runs inside firstboot's
        window), NOT a stale rocker event, which is what the 2026-09-16 plan
        expected to find. No request arrived, so no apply was attempted and
        nothing went wrong.

      * A CLIENT THAT STOPPED LISTENING. `panel-volume-keys.py` waits 24 s for
        the verdict; past that it gives up and the reply lands on a closed
        peer (EPIPE). The apply has already happened by then. Whether it
        SUCCEEDED is the thing worth a red unit, and that is still reported --
        an undeliverable verdict is journaled and then the apply's own result
        decides the exit code.

    A failure to READ is still a failure: a request that times out half-sent is
    a client this applier could not serve.
    """
    with socket.socket(fileno=0) as connection:
        connection.settimeout(2)
        try:
            data = read_request(connection)
        except OSError as exc:
            print("volume request not received: %s" % exc, file=sys.stderr, flush=True)
            return 1
        if not data:
            print("volume: connection closed without a request — nothing applied",
                  file=sys.stderr, flush=True)
            return 0
        answer = handle_request(data, apply_volume, capture_volume)
        try:
            connection.sendall(answer)
        except OSError as exc:
            print("volume verdict undeliverable (%s) — the client stopped waiting; "
                  "the apply itself decides this unit's result" % exc,
                  file=sys.stderr, flush=True)
        return 0 if _is_success(answer) else 1


def _is_success(answer):
    """Whether this reply should leave a green unit behind.

    `Accept=yes` gives every connection its own unit instance, so whatever
    this calls a failure becomes a RED LINE on the panel's failed list. The
    guarded form makes that judgement finer than `== b"ok\n"`, and getting it
    wrong in either direction is bad in a way the Owner sees:

      * `ok:OUT:LEVEL:REV` is a SUCCESS. Reading the old equality literally
        would have turned every successful rocker step into a failed unit.
      * `refused:state-changed` and `refused:unavailable` are the PROTOCOL
        WORKING. The Owner moved the switch mid-gesture, or the position has
        no level; the applier declined to write a stale target, which is what
        it is for. A red unit for that trains the list to be ignored.
      * `refused:apply-failed` and `refused:ambiguous` are real trouble --
        amixer or a unit did not do as it was told, or the verdict was lost --
        and those still go red.
    """
    if answer == b"ok\n" or answer.startswith(b"ok:"):
        return True
    return answer in (b"refused:state-changed\n", b"refused:unavailable\n")


if __name__ == "__main__":
    sys.exit(main())
