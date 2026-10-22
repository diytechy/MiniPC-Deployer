"""Synthetic MPEG-TS for the recorder's tests: PAT, PMT, video PES with PTS and
the random-access flag on keyframes, and body packets. No dependency beyond the
standard library, so the pytest suite and the root filesystem suite (which runs
in a bare initramfs) share one definition of "a stream". Not a valid H.264
bitstream - the recorder never decodes; the lab suite covers real video.
"""

PMT_PID, VIDEO_PID = 0x1000, 0x100


def packet(pid, pusi, payload, rai=False):
    """One 188-byte TS packet; an adaptation field carries the RAI flag."""
    head = bytes([0x47, (0x40 if pusi else 0) | (pid >> 8), pid & 0xFF])
    if rai:
        head += bytes([0x30])
        body = bytes([1, 0x40]) + payload
    else:
        head += bytes([0x10])
        body = payload
    return (head + body + b"\xff" * 188)[:188]


def pat():
    sec = bytes([0x00, 0xB0, 13, 0, 1, 0xC1, 0, 0, 0, 1, 0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF]) + b"\0" * 4
    return packet(0, True, b"\x00" + sec)


def pmt(stream_type=0x1B):
    sec = bytes([0x02, 0xB0, 18, 0, 1, 0xC1, 0, 0, 0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0,
                 stream_type, 0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0]) + b"\0" * 4
    return packet(PMT_PID, True, b"\x00" + sec)


def pes(pts, key):
    """The first packet of a video PES carrying a 33-bit PTS."""
    p = pts
    stamp = bytes([0x21 | ((p >> 29) & 0x0E), (p >> 22) & 0xFF, 0x01 | ((p >> 14) & 0xFE),
                   (p >> 7) & 0xFF, 0x01 | ((p << 1) & 0xFE)])
    header = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + stamp
    return packet(VIDEO_PID, True, header + b"\x00\x00\x00\x01\x65", rai=key)


def body():
    return packet(VIDEO_PID, False, b"\x00" * 184)


def stream(frames, gop=10, fps=10, start_pts=0, bodies=2):
    """PAT/PMT before every GOP, then frames of one PES packet + `bodies` body packets."""
    out = []
    for i in range(frames):
        if i % gop == 0:
            out += [pat(), pmt()]
        out += [pes(start_pts + i * 90000 // fps, i % gop == 0)] + [body()] * bodies
    return out
