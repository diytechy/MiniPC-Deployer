#!/usr/bin/env python3
"""panel-camera.py - open the wall panel's camera, with no dependencies and no
installs, and leave nothing behind.

WHY IT IS WRITTEN THIS WAY. The panel image carries no ffmpeg, no gstreamer, no
fswebcam, no v4l-utils and no python3-opencv, and it is reimage-not-repair by
ratification - so the honest way to answer "does the camera work, and does its
LED come on" is not to apt-install four packages onto it. V4L2 is an ioctl
interface; ctypes can drive it directly, exactly as panel-poke.py drives
/dev/uinput. Nothing is installed, nothing is left running.

    panel-camera.py probe                 what the camera is and what it offers
    panel-camera.py stream --seconds 20   stream for 20 s (this is what lights
                                          the activity LED), then stop
    panel-camera.py stream --seconds 20 --stats
                                          + measured resolution and frame rate

FRAMES NEVER TOUCH THE DISK. There is deliberately no --save, --out or --frames
option, and adding one would be a decision rather than a convenience: the
mmap'd buffers are read for their length and dropped. The privacy posture of
anything built on this camera starts here, in the one tool that can see through
it, and it starts as "cannot write an image at all".

`stream` is also the only reliable way to test the LED: opening /dev/video0
powers nothing. UVC sensors - and the activity LED wired to them - come on at
VIDIOC_STREAMON and go off at VIDIOC_STREAMOFF, which is the pair this cycles.
"""

import argparse
import fcntl
import mmap
import os
import struct
import sys
import time

# ── ioctl encoding (asm-generic/ioctl.h) ────────────────────────────────────
_IOC_NRBITS, _IOC_TYPEBITS, _IOC_SIZEBITS = 8, 8, 14
_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2


def _IOC(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


def _IOR(typ, nr, size):
    return _IOC(_IOC_READ, typ, nr, size)


def _IOW(typ, nr, size):
    return _IOC(_IOC_WRITE, typ, nr, size)


def _IOWR(typ, nr, size):
    return _IOC(_IOC_READ | _IOC_WRITE, typ, nr, size)


# ── struct sizes (videodev2.h, x86-64) ──────────────────────────────────────
# Each is the sizeof() the kernel encodes into the ioctl number, so a wrong one
# is an ENOTTY that looks like "the driver does not support this call".
SZ_CAPABILITY = 104        # driver[16] card[32] bus_info[32] + 3 u32 + u32[3]
SZ_FMTDESC = 64
SZ_FORMAT = 208            # u32 type + 200-byte union, 4 bytes of padding
SZ_REQUESTBUFFERS = 20
SZ_BUFFER = 88             # see the offset map in _buffer() below

VIDIOC_QUERYCAP = _IOR("V", 0, SZ_CAPABILITY)
VIDIOC_ENUM_FMT = _IOWR("V", 2, SZ_FMTDESC)
VIDIOC_S_FMT = _IOWR("V", 5, SZ_FORMAT)
VIDIOC_REQBUFS = _IOWR("V", 8, SZ_REQUESTBUFFERS)
VIDIOC_QUERYBUF = _IOWR("V", 9, SZ_BUFFER)
VIDIOC_QBUF = _IOWR("V", 15, SZ_BUFFER)
VIDIOC_DQBUF = _IOWR("V", 17, SZ_BUFFER)
VIDIOC_STREAMON = _IOW("V", 18, 4)
VIDIOC_STREAMOFF = _IOW("V", 19, 4)

BUF_TYPE_VIDEO_CAPTURE = 1
MEMORY_MMAP = 1

# MJPG first: a UVC camera that offers it hands over a compressed frame, which
# on a 2-core Skylake is the difference between a background presence check and
# a busy panel. YUYV is the universal fallback.
PIXFMT = {
    "MJPG": 0x47504A4D,
    "YUYV": 0x56595559,
    "NV12": 0x3231564E,
}


def fourcc_name(value):
    for name, code in PIXFMT.items():
        if code == value:
            return name
    return "".join(chr((value >> s) & 0xFF) for s in (0, 8, 16, 24))


def _buffer(index=0):
    """A zeroed struct v4l2_buffer with type/memory/index set.

    Offsets that matter on x86-64, because getting one wrong is a silent
    EINVAL: index 0, type 4, bytesused 8, flags 12, field 16, [pad 20],
    timestamp 24, timecode 40, sequence 56, memory 60, m.offset 64,
    length 72, reserved2 76, request_fd 80, [pad to 88].
    """
    buf = bytearray(SZ_BUFFER)
    struct.pack_into("<II", buf, 0, index, BUF_TYPE_VIDEO_CAPTURE)
    struct.pack_into("<I", buf, 60, MEMORY_MMAP)
    return buf


def probe(path):
    fd = os.open(path, os.O_RDWR)
    try:
        cap = bytearray(SZ_CAPABILITY)
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, cap, True)
        driver = cap[0:16].split(b"\0")[0].decode(errors="replace")
        card = cap[16:48].split(b"\0")[0].decode(errors="replace")
        bus = cap[48:80].split(b"\0")[0].decode(errors="replace")
        version, caps, device_caps = struct.unpack_from("<III", cap, 80)
        print(f"device      {path}")
        print(f"driver      {driver}")
        print(f"card        {card}")
        print(f"bus         {bus}")
        print(f"version     {version >> 16}.{(version >> 8) & 0xFF}.{version & 0xFF}")
        print(f"caps        0x{caps:08x}  device_caps 0x{device_caps:08x}")
        print(f"            video-capture={bool(device_caps & 0x00000001)} "
              f"streaming={bool(device_caps & 0x04000000)} "
              f"readwrite={bool(device_caps & 0x01000000)}")

        formats = []
        for i in range(32):
            desc = bytearray(SZ_FMTDESC)
            struct.pack_into("<II", desc, 0, i, BUF_TYPE_VIDEO_CAPTURE)
            try:
                fcntl.ioctl(fd, VIDIOC_ENUM_FMT, desc, True)
            except OSError:
                break
            name = desc[12:44].split(b"\0")[0].decode(errors="replace")
            pixfmt = struct.unpack_from("<I", desc, 44)[0]
            formats.append(fourcc_name(pixfmt))
            print(f"format[{i}]   {fourcc_name(pixfmt)}  {name}")
        if not formats:
            print("format      (none enumerated - unusual for a UVC device)")
        return 0
    finally:
        os.close(fd)


def stream(path, seconds, width, height, pixfmt, stats, quiet):
    fd = os.open(path, os.O_RDWR)
    maps = []
    streaming = False
    try:
        # ── negotiate a format. The driver rewrites the struct with what it
        # will actually give us, which is why it is read back rather than
        # assumed.
        fmt = bytearray(SZ_FORMAT)
        struct.pack_into("<I", fmt, 0, BUF_TYPE_VIDEO_CAPTURE)
        struct.pack_into("<IIII", fmt, 8, width, height, PIXFMT[pixfmt], 1)  # field=NONE
        try:
            fcntl.ioctl(fd, VIDIOC_S_FMT, fmt, True)
        except OSError as err:
            print(f"panel-camera: the driver refused {pixfmt} {width}x{height} ({err})",
                  file=sys.stderr)
            return 1
        got_w, got_h, got_fmt = struct.unpack_from("<III", fmt, 8)
        size_image = struct.unpack_from("<I", fmt, 28)[0]
        if not quiet:
            print(f"negotiated  {fourcc_name(got_fmt)} {got_w}x{got_h}  "
                  f"({size_image} bytes/frame)")

        # ── ask for buffers and map them
        req = bytearray(SZ_REQUESTBUFFERS)
        struct.pack_into("<III", req, 0, 4, BUF_TYPE_VIDEO_CAPTURE, MEMORY_MMAP)
        fcntl.ioctl(fd, VIDIOC_REQBUFS, req, True)
        count = struct.unpack_from("<I", req, 0)[0]
        if count < 1:
            print("panel-camera: the driver granted no buffers", file=sys.stderr)
            return 1

        for i in range(count):
            buf = _buffer(i)
            fcntl.ioctl(fd, VIDIOC_QUERYBUF, buf, True)
            offset = struct.unpack_from("<I", buf, 64)[0]
            length = struct.unpack_from("<I", buf, 72)[0]
            maps.append(mmap.mmap(fd, length, mmap.MAP_SHARED,
                                  mmap.PROT_READ | mmap.PROT_WRITE, offset=offset))
            fcntl.ioctl(fd, VIDIOC_QBUF, _buffer(i), True)

        # ── THE LED COMES ON HERE, and not a moment before.
        fcntl.ioctl(fd, VIDIOC_STREAMON, struct.pack("<i", BUF_TYPE_VIDEO_CAPTURE))
        streaming = True
        if not quiet:
            print(f"STREAMING   the camera is live for {seconds}s - watch for the LED")
            sys.stdout.flush()

        frames = 0
        started = time.monotonic()
        deadline = started + seconds
        while time.monotonic() < deadline:
            buf = _buffer()
            try:
                fcntl.ioctl(fd, VIDIOC_DQBUF, buf, True)
            except OSError:
                time.sleep(0.01)
                continue
            # The frame is deliberately NOT read into Python, saved, hashed or
            # forwarded. Its length is the only thing taken from it.
            bytesused = struct.unpack_from("<I", buf, 8)[0]
            index = struct.unpack_from("<I", buf, 0)[0]
            frames += 1
            if stats and frames == 1:
                print(f"first frame {bytesused} bytes")
            fcntl.ioctl(fd, VIDIOC_QBUF, _buffer(index), True)

        elapsed = time.monotonic() - started
        if stats:
            print(f"measured    {frames} frames in {elapsed:.1f}s = {frames / elapsed:.1f} fps "
                  f"at {got_w}x{got_h} {fourcc_name(got_fmt)}")
        return 0
    finally:
        if streaming:
            try:
                fcntl.ioctl(fd, VIDIOC_STREAMOFF, struct.pack("<i", BUF_TYPE_VIDEO_CAPTURE))
            except OSError:
                pass
            if not quiet:
                print("STOPPED     the camera is off (the LED should be out)")
        for m in maps:
            m.close()
        os.close(fd)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["probe", "stream"])
    # WALL_CAMERA_DEVICE is the knob (wall.env -> kiosk.env), so the answer to
    # "which node is the capture node" lives in one place rather than in every
    # caller's argv. A UVC camera publishes two nodes and the capture one is not
    # guaranteed to be video0 once a second camera exists.
    ap.add_argument("--device",
                    default=os.environ.get("WALL_CAMERA_DEVICE", "/dev/video0"))
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--pixfmt", choices=sorted(PIXFMT), default="MJPG")
    ap.add_argument("--stats", action="store_true",
                    help="report the frame rate the camera actually delivered")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        if args.action == "probe":
            return probe(args.device)
        return stream(args.device, args.seconds, args.width, args.height,
                      args.pixfmt, args.stats, args.quiet)
    except PermissionError:
        print(f"panel-camera: no access to {args.device} - the user must be in group 'video'",
              file=sys.stderr)
        return 77
    except FileNotFoundError:
        print(f"panel-camera: {args.device} does not exist.", file=sys.stderr)
        print("  If WALL_CAMERA_ENABLED=false in wall.env this is CORRECT and deliberate:",
              file=sys.stderr)
        print("  firstboot blacklists uvcvideo so the camera cannot be opened at all.",
              file=sys.stderr)
        return 69


if __name__ == "__main__":
    sys.exit(main())
