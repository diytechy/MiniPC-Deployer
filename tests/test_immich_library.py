"""Immich reads one photo tree, read-only, as an External Library (LLR-1570).

HomeHub ruled (A17(i), 2026-08-30) that Immich reads the household's photo
library read-only and is not a photo store, and its open item C49 asks the
Owner to register that tree in Immich's UI. Until this mount existed the
container held only Immich's own upload store, so C49's first step could not
pass. What is asserted here is that the SHIPPED compose and template still
carry that shape, because each part is one plausible edit away from harm:

- `:ro` dropped, and Immich's trash or "delete from disk" reaches originals;
- the default pointed at ${MEDIA_ROOT}/immich, and an empty knob offers Immich
  its own thumbnails and encoded videos as a library to import;
- the knob missing from .env.example, and the deploy template can never set it
  (HomeHub's Materialize-Deploy fills only keys the template carries);
- the upload store made read-only by a careless copy, and Immich stops.
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
COMPOSE = STACK / "docker-compose.yml"
ENVEX = STACK / ".env.example"

pytestmark = pytest.mark.smoke

KNOB = "IMMICH_EXTERNAL_LIBRARY_DIR"
LIBRARY_TARGET = "/external/library"


@pytest.fixture(scope="module")
def immich_volumes():
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return compose["services"]["immich-server"]["volumes"]


def _split_volume(volume):
    """Short-syntax `source:target[:mode]` -> parts, ignoring colons inside ${...}.

    A plain split breaks on `${KNOB:-default}`, whose `:-` is interpolation.
    """
    parts, current, depth = [], "", 0
    for char in volume:
        if char == "{" and current.endswith("$"):
            depth += 1
        elif char == "}" and depth:
            depth -= 1
        if char == ":" and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    return parts


def _volume_for(volumes, target):
    """The (source, target, mode) of the one short-syntax mount at `target`."""
    matches = [_split_volume(v) for v in volumes if _split_volume(v)[1] == target]
    assert len(matches) == 1, f"expected one mount at {target}, got {matches}"
    source, _, *mode = matches[0]
    return source, target, (mode[0] if mode else "rw")


def test_library_is_mounted_read_only_from_the_knob_tc1820(immich_volumes):
    source, _, mode = _volume_for(immich_volumes, LIBRARY_TARGET)
    assert mode == "ro"
    assert source == "${IMMICH_EXTERNAL_LIBRARY_DIR:-${MEDIA_ROOT}/photos}"


def test_default_is_never_immichs_own_store_tc1820(immich_volumes):
    source, _, _ = _volume_for(immich_volumes, LIBRARY_TARGET)
    default = re.fullmatch(r"\$\{" + KNOB + r":-(.+)\}", source).group(1)
    store, _, _ = _volume_for(immich_volumes, "/data")
    assert store == "${MEDIA_ROOT}/immich"
    assert default != store and not default.startswith(store + "/")
    # ...nor a tree that CONTAINS the store: Jellyfin's ${MEDIA_ROOT} default
    # would hand Immich its own thumbnails one directory down.
    assert not store.startswith(default.rstrip("/") + "/")


def test_upload_store_stays_writable_tc1820(immich_volumes):
    _, _, mode = _volume_for(immich_volumes, "/data")
    assert mode == "rw"


def test_knob_is_in_the_template_exactly_once_and_empty_tc1820():
    template = ENVEX.read_text(encoding="utf-8")
    assert re.findall(rf"(?m)^{KNOB}=(.*)$", template) == [""]
