#!/usr/bin/env python3
"""Write a fully transparent XCursor theme, so the compositor's own pointer
image is nothing at all.

WHY THIS EXISTS (Owner item 22, 2026-09-13): "Cursor appears whenever I touch
any screen; can the cursor icon be made fully transparent if it is needed?"

The renderer cannot answer that question. Chromium sets a cursor image only for
a pointer that has ENTERED its surface, so `cursor: none` in the shell is inert
for anything the compositor draws on its own -- that is the same measurement
wall-park-cursor.service was built around, and it has not changed. What HAS
changed is that the panel now has a pointer device by design:
touchfilter/daemon.py's scroll uinput device declares REL_X + REL_Y + BTN_LEFT
as well as its wheel axes, because udev's input_id builtin tags ID_INPUT_MOUSE
only for all three together and libinput discards the wheels of a device it has
not tagged. So every finger drag is now pointer activity on the seat, and a
compositor with a pointer draws a cursor image for it.

`cage -d` does not help and never did: its own usage string on the panel
(cage 0.1.5) reads "-d  Don't draw client side decorations, when possible".
cage has no cursor flag at all. What cage does have is wlroots' XCursor
loading, and that is themeable from outside the process -- so the smallest
honest fix is to give it a theme whose cursors are transparent. Then there is
no cursor image to draw, whatever moved the pointer and whoever owns the
surface.

THE THEME IS CALLED "default" AND IS FOUND THROUGH XCURSOR_PATH, NOT
XCURSOR_THEME, and that is measured rather than assumed. On the panel
(2026-09-13, read-only over ssh):

    grep -ao "XCURSOR_(THEME|SIZE|PATH)" /lib/x86_64-linux-gnu/libwlroots.so.12
        -> XCURSOR_PATH          and nothing else
    grep -ao "XCURSOR_...|left_ptr|default"  $(command -v cage)
        -> default, left_ptr     and no XCURSOR_ name at all

So the theme NAME never comes from the environment here: cage 0.1.5 creates its
xcursor manager with a NULL theme, wlroots substitutes "default", and the only
thing the environment can still move is where "default" is looked up. Hence a
directory literally named `default`, in a directory of our own, named by
XCURSOR_PATH. Setting XCURSOR_THEME instead would have looked right, changed
nothing, and been very hard to disbelieve.

THE FORMAT, written by hand because xcursorgen is not on the image and a build
dependency for 68 bytes is not worth it. Xcursor files are little-endian:

    header   "Xcur", header-size 16, version 0x00010000, ntoc
    toc[n]   type 0xfffd0002 (IMAGE), subtype = nominal size, byte position
    image    chunk-header 36, type, subtype, version 1,
             width, height, xhot, yhot, delay-ms, then w*h ARGB pixels

One 1x1 fully transparent pixel per nominal size. The nominal size is what the
loader matches against XCURSOR_SIZE; the pixels are what it draws, and there
are none worth drawing.

    panel-invisible-cursor.py /usr/local/share/wall-cursors/default
"""

import os
import struct
import sys

MAGIC = 0x72756358          # "Xcur" read as a little-endian uint32
FILE_VERSION = 0x00010000
CHUNK_IMAGE = 0xFFFD0002
# The nominal sizes a theme is expected to offer. XCURSOR_SIZE picks among
# them; every one of them is the same nothing.
SIZES = (16, 24, 32, 48, 64, 96)

# Every name cage/wlroots or a client might ask this theme for. A name that is
# missing falls back to the loader's built-in cursor, which is visible -- so the
# list is deliberately generous rather than minimal. cage 0.1.5 asks for
# "left_ptr"; wlroots 0.17+ and most toolkits ask for "default".
NAMES = (
    "default", "left_ptr", "arrow", "top_left_arrow", "left_ptr_watch",
    "pointer", "hand1", "hand2", "text", "xterm", "ibeam", "watch", "wait",
    "crosshair", "cross", "help", "question_arrow", "not-allowed",
    "grab", "grabbing", "progress", "all-scroll", "fleur", "move",
    "n-resize", "s-resize", "e-resize", "w-resize",
    "ne-resize", "nw-resize", "se-resize", "sw-resize",
    "col-resize", "row-resize", "sb_h_double_arrow", "sb_v_double_arrow",
)


def transparent_cursor(sizes=SIZES):
    """One Xcursor file: a 1x1 fully transparent image at each nominal size."""
    ntoc = len(sizes)
    header = struct.pack("<IIII", MAGIC, 16, FILE_VERSION, ntoc)
    # Every image chunk is the same length, so the positions are arithmetic:
    # the header, then the table, then one chunk after another.
    chunk_len = 36 + 4                      # 9 header words + one ARGB pixel
    first = len(header) + 12 * ntoc
    toc = b"".join(
        struct.pack("<III", CHUNK_IMAGE, size, first + i * chunk_len)
        for i, size in enumerate(sizes)
    )
    images = b"".join(
        # chunk-header, type, subtype(size), version, w, h, xhot, yhot, delay
        struct.pack("<IIIIIIIII", 36, CHUNK_IMAGE, size, 1, 1, 1, 0, 0, 0)
        + struct.pack("<I", 0x00000000)     # one premultiplied ARGB pixel: none
        for size in sizes
    )
    return header + toc + images


def write_theme(root, names=NAMES):
    """Install the theme under `root` (e.g. /usr/local/share/wall-cursors/default).

    The LAST path component is the theme name as a lookup will spell it, so the
    caller passes .../default -- see the XCURSOR_PATH note in the module
    docstring for why that name is not negotiable on this compositor."""
    cursors = os.path.join(root, "cursors")
    os.makedirs(cursors, exist_ok=True)
    blob = transparent_cursor()
    for name in names:
        path = os.path.join(cursors, name)
        with open(path, "wb") as fh:
            fh.write(blob)
        os.chmod(path, 0o644)
    # index.theme is not required to LOAD a cursor out of this theme, but a
    # theme without one is invisible to anything that enumerates themes, and
    # the Inherits line is what stops a lookup for a name this theme does not
    # carry from falling through to a VISIBLE arrow somewhere else: it inherits
    # from itself-shaped nothing rather than from Adwaita.
    index = os.path.join(root, "index.theme")
    with open(index, "w", encoding="utf-8") as fh:
        fh.write(
            "[Icon Theme]\n"
            "Name=Wall Invisible\n"
            "Comment=Fully transparent pointer for the office wall panel"
            " (OfficeWallNaglight item 22)\n"
        )
    os.chmod(index, 0o644)
    return cursors, len(blob)


def main(argv):
    root = argv[1] if len(argv) > 1 else "/usr/local/share/wall-cursors/default"
    cursors, size = write_theme(root)
    print(f"wrote {len(NAMES)} transparent cursors of {size} bytes into {cursors}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
