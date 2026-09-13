"""The transparent cursor theme is a real XCursor file, not a plausible one.

Owner item 22. The whole fix rests on the compositor accepting these bytes: if
wlroots rejects the file it falls back to its BUILT-IN cursor, which is a
visible arrow, and the panel looks exactly as it did before with nothing in any
log to say why. So the format is asserted field by field here, against the
structure read off a real system theme on the panel itself
(/usr/share/icons/Adwaita/cursors/default: magic 0x72756358, header 16,
version 0x00010000, 5 table entries).
"""

import importlib.util
import struct
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "stack" / "autoinstall" / "wall" / "panel-invisible-cursor.py"

MAGIC = 0x72756358
CHUNK_IMAGE = 0xFFFD0002


def load_module():
    spec = importlib.util.spec_from_file_location("panel_invisible_cursor", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return load_module()


def test_file_header_matches_the_xcursor_format(mod):
    blob = mod.transparent_cursor()
    magic, header, version, ntoc = struct.unpack("<IIII", blob[:16])
    assert magic == MAGIC
    assert header == 16
    assert version == 0x00010000
    assert ntoc == len(mod.SIZES)


def test_every_table_entry_points_at_a_transparent_one_pixel_image(mod):
    blob = mod.transparent_cursor()
    ntoc = struct.unpack("<I", blob[12:16])[0]
    seen = []
    for i in range(ntoc):
        kind, subtype, position = struct.unpack("<III", blob[16 + 12 * i:28 + 12 * i])
        assert kind == CHUNK_IMAGE
        fields = struct.unpack("<IIIIIIIII", blob[position:position + 36])
        chunk_header, chunk_kind, chunk_subtype, chunk_version = fields[:4]
        width, height, xhot, yhot, delay = fields[4:]
        assert chunk_header == 36           # nine little-endian words
        assert chunk_kind == CHUNK_IMAGE
        assert chunk_subtype == subtype     # the nominal size, twice, as the format wants
        assert chunk_version == 1
        assert (width, height) == (1, 1)
        assert (xhot, yhot, delay) == (0, 0, 0)
        # THE POINT OF THE WHOLE FILE: alpha 0, so there is nothing to draw.
        pixel = struct.unpack("<I", blob[position + 36:position + 40])[0]
        assert pixel == 0
        seen.append(subtype)
    assert seen == list(mod.SIZES)
    # No slack and no overlap: the table's arithmetic has to be right, because a
    # position that lands mid-chunk is a file the loader rejects.
    assert len(blob) == 16 + 12 * ntoc + ntoc * 40


def test_the_theme_is_written_under_the_name_the_compositor_asks_for(mod, tmp_path):
    # cage 0.1.5 asks wlroots for a NULL theme, which resolves to "default",
    # and it requests the cursor by the name "left_ptr". Both must be files.
    root = tmp_path / "wall-cursors" / "default"
    cursors, size = mod.write_theme(str(root))
    assert Path(cursors) == root / "cursors"
    assert (root / "index.theme").is_file()
    for name in ("default", "left_ptr"):
        path = root / "cursors" / name
        assert path.is_file(), f"{name} missing: the loader would fall back to a visible arrow"
        assert path.read_bytes() == mod.transparent_cursor()
    assert size == len(mod.transparent_cursor())


def test_no_cursor_name_inherits_a_visible_theme(mod, tmp_path):
    # An Inherits= line would send a name this theme does not carry off to
    # Adwaita, and back comes an arrow. There must not be one.
    root = tmp_path / "default"
    mod.write_theme(str(root))
    assert "Inherits" not in (root / "index.theme").read_text(encoding="utf-8")
