"""The frozen binary PCM frame carried by the panel's local PCM socket.

ONE RESPONSIBILITY: turn one captured block of interleaved PCM16 into exactly
one self-describing frame, and turn bytes back into a block or a refusal. It
opens nothing, holds nothing and logs nothing -- it is the wire format and
nothing else, so that both ends of IF-020 can be tested without a socket.

WHY A FIXED BINARY FRAME AND NOT JSON. The consumer is an Electron host that
hands the payload to a Web Worker as a transferable ArrayBuffer; a JSON array of
2048 numbers would be parsed into a JavaScript array and re-packed on every
block, forty-seven times a second, for no gain. The header is fixed-width so a
reader can size the frame before it has read the payload, which is what makes
"refuse anything oversized" a decision taken BEFORE the allocation rather than
after it.

WHY THE SAME FILE EXISTS TWICE. `electron/pcm-frame.cjs` in OfficeWallNaglight
is the decoder for the other end. The two cannot import each other, so they are
held together by a BYTE-IDENTICAL FIXTURE shipped in both repositories
(`tests/fixtures/pcm-frame-v1.bin`), which each side decodes in its own suite.
A drift in either implementation fails a test in the repository that drifted.

PRIVACY. This module is the one place in the image where raw audio samples are
serialized at all, and that is permitted narrowly: see LLR-954 and IF-020. It
writes to no file and to no log. Raw audio is not retained or emitted unless
explicitly permitted; the ProjectM exception is local, memory-only, lasts only
while ProjectM is the selected and visible fullscreen owner, and is never
written to disk, journaled, put in diagnostics or telemetry, uploaded, or kept
after ProjectM yields.

Implements: SR-041, LLR-953.
"""

from __future__ import annotations

import struct

# "WPCM" -- wall panel PCM. Four bytes so a reader that resynchronises after a
# refusal has something unambiguous to look for.
PCM_MAGIC = b"WPCM"
PCM_VERSION = 1
# Fixed, and CARRIED IN THE FRAME rather than merely agreed: a reader validates
# the value it was sent against this constant and refuses a mismatch, so a
# future version that grows the header cannot be silently misparsed by an old
# reader as a version-1 frame with a corrupt payload.
PCM_HEADER_BYTES = 32
_HEADER = struct.Struct("<4sHHIQIHHI")

# The bus is 48 kHz stereo with a 1024-frame period, so one captured block is
# one frame: 1024 frames x 2 channels x 2 bytes = 4096 bytes of payload. The
# bounds are the period, not an aspiration -- a producer that sent more would be
# sending something other than a block of this bus.
MAX_PCM_FRAME_COUNT = 1024
MAX_PCM_CHANNELS = 2
MAX_PCM_PAYLOAD_BYTES = MAX_PCM_FRAME_COUNT * MAX_PCM_CHANNELS * 2
MAX_PCM_FRAME_BYTES = PCM_HEADER_BYTES + MAX_PCM_PAYLOAD_BYTES
# u32, so it wraps rather than growing without bound. A consumer compares
# sequences for "is this newer than the one I have" over a window of one, which
# a wrap cannot confuse; nothing accumulates across it.
SEQUENCE_MODULUS = 1 << 32
# CLOCK_MONOTONIC milliseconds. Bounded to the JavaScript safe-integer range
# because the consumer is JavaScript and reading a u64 that does not fit is a
# silent precision loss rather than an error.
JS_SAFE_INTEGER = 9_007_199_254_740_991
MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 192_000


class PcmFrameError(ValueError):
    """A frame that may not be encoded or may not be believed.

    One exception type for both directions on purpose: every caller's correct
    response to a bad frame is the same -- drop it, count it, and do not let it
    reach a consumer -- so distinguishing the reasons would only invite a caller
    to handle some of them differently.
    """


def encode_pcm_frame(payload, *, sequence, observed_monotonic_ms, sample_rate,
                     channels, frame_count):
    """Return one complete frame, header first.

    Contract:
      Inputs:  payload: `bytes` of interleaved signed 16-bit little-endian
                        samples, exactly frame_count * channels * 2 long
               sequence: 0 .. 2**32-1, the producer's own counter
               observed_monotonic_ms: CLOCK_MONOTONIC ms when the block was read
               sample_rate/channels/frame_count: the block's declared shape
      Outputs: `bytes`, at most MAX_PCM_FRAME_BYTES
      Raises:  PcmFrameError for any value outside the frozen bounds.
    Implements: SR-041, LLR-953.
    """
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise PcmFrameError("payload must be bytes")
    payload = bytes(payload)
    _check_shape(sample_rate, channels, frame_count)
    if not _bounded_int(sequence, 0, SEQUENCE_MODULUS - 1):
        raise PcmFrameError("sequence outside the u32 range")
    if not _bounded_int(observed_monotonic_ms, 0, JS_SAFE_INTEGER):
        raise PcmFrameError("observation time outside the safe range")
    expected = frame_count * channels * 2
    if len(payload) != expected:
        raise PcmFrameError("payload length does not match the declared shape")
    header = _HEADER.pack(PCM_MAGIC, PCM_VERSION, PCM_HEADER_BYTES, sequence,
                          observed_monotonic_ms, sample_rate, channels,
                          frame_count, expected)
    return header + payload


def decode_pcm_frame(data):
    """Return one frame's fields and payload, or refuse.

    EVERY FIELD IS CHECKED BEFORE THE PAYLOAD IS TOUCHED, which is the point of
    a fixed header: an oversized `payload_bytes` is refused as a number, not
    discovered as a failed allocation.

    Contract:
      Inputs:  data: exactly one frame's bytes
      Outputs: {"sequence", "observedMonotonicMs", "sampleRate", "channels",
                "frameCount", "payload"}
      Raises:  PcmFrameError for a short, long, mislabelled or oversized frame.
    Implements: SR-041, LLR-953.
    """
    fields = decode_pcm_header(data[:PCM_HEADER_BYTES] if len(data) >= PCM_HEADER_BYTES
                               else data)
    total = PCM_HEADER_BYTES + fields["payloadBytes"]
    if len(data) != total:
        raise PcmFrameError("frame length does not match its header")
    payload = bytes(data[PCM_HEADER_BYTES:total])
    return {"sequence": fields["sequence"],
            "observedMonotonicMs": fields["observedMonotonicMs"],
            "sampleRate": fields["sampleRate"], "channels": fields["channels"],
            "frameCount": fields["frameCount"], "payload": payload}


def decode_pcm_header(data):
    """Return the validated header fields, so a reader can size the payload.

    A stream reader has to answer "how many more bytes" before it can have them,
    so the header check is a separate step rather than an inner detail of
    `decode_pcm_frame`. Both go through the same bounds.

    Implements: SR-041, LLR-953.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise PcmFrameError("header must be bytes")
    data = bytes(data)
    if len(data) != PCM_HEADER_BYTES:
        raise PcmFrameError("header must be exactly %d bytes" % PCM_HEADER_BYTES)
    (magic, version, header_bytes, sequence, observed, sample_rate, channels,
     frame_count, payload_bytes) = _HEADER.unpack(data)
    if magic != PCM_MAGIC:
        raise PcmFrameError("not a panel PCM frame")
    if version != PCM_VERSION:
        raise PcmFrameError("unsupported frame version")
    if header_bytes != PCM_HEADER_BYTES:
        raise PcmFrameError("header length does not match this version")
    if observed > JS_SAFE_INTEGER:
        raise PcmFrameError("observation time outside the safe range")
    _check_shape(sample_rate, channels, frame_count)
    if payload_bytes != frame_count * channels * 2:
        raise PcmFrameError("payload length does not match the declared shape")
    if payload_bytes > MAX_PCM_PAYLOAD_BYTES:
        raise PcmFrameError("payload larger than one bus period")
    return {"sequence": sequence, "observedMonotonicMs": observed,
            "sampleRate": sample_rate, "channels": channels,
            "frameCount": frame_count, "payloadBytes": payload_bytes}


def _check_shape(sample_rate, channels, frame_count):
    if not _bounded_int(sample_rate, MIN_SAMPLE_RATE, MAX_SAMPLE_RATE):
        raise PcmFrameError("sample rate outside the supported range")
    if not _bounded_int(channels, 1, MAX_PCM_CHANNELS):
        raise PcmFrameError("channel count outside the supported range")
    if not _bounded_int(frame_count, 1, MAX_PCM_FRAME_COUNT):
        raise PcmFrameError("frame count outside one bus period")


def _bounded_int(value, low, high):
    """True for a real integer in [low, high]. `bool` is an int: refuse it."""
    return (not isinstance(value, bool) and isinstance(value, int)
            and low <= value <= high)
