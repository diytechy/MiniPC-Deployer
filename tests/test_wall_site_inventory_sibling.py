"""WI-24: the deployer's public-site inventory equals the panel's real site.

Implements tests: TC-1232 (inventory equals sibling), TC-1233 (floor is a
subset of the sibling's manifest). Covers LLR-1132..LLR-1133.

Both sides are read from OfficeWallNaglight itself: the site set is
`git ls-files` over the SITE_SOURCES that tools/build-artifact.mjs stages
(parsed from that file, not retyped here), and the floor is compared with the
sibling's capabilities.json. Skips when the sibling is absent.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Sibling resolution honours OWN_REPO (precedent: TOUCH_APP_REPO).
OWN_REPO = Path(os.environ.get("OWN_REPO") or ROOT.parent / "OfficeWallNaglight")
BUILD = OWN_REPO / "tools/build-artifact.mjs"
STAMPS = {"VERSION", "build-info.json"}

spec = importlib.util.spec_from_file_location(
    "wall_site_inventory", ROOT / "scripts/assert_wall_capabilities.py")
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


def need_sibling():
    if not BUILD.is_file() or not (OWN_REPO / "capabilities.json").is_file():
        pytest.skip("sibling OfficeWallNaglight absent: contract side not checked")


def site_sources():
    text = BUILD.read_text(encoding="utf-8")
    match = re.search(r"const SITE_SOURCES = \[(.*?)\];", text, re.S)
    assert match, "SITE_SOURCES not found in tools/build-artifact.mjs"
    names = re.findall(r'"([^"]+)"', match.group(1))
    assert names
    return names


def sibling_site_files():
    done = subprocess.run(["git", "-C", str(OWN_REPO), "ls-files", "--", *site_sources()],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-400:]
    files = set(done.stdout.split())
    assert files
    return files


def test_tc1232_inventory_equals_the_panels_site_files():
    need_sibling()
    actual = sibling_site_files()
    inventory = set(contract.ALLOWED_SITE_FILES) - STAMPS
    assert STAMPS <= set(contract.ALLOWED_SITE_FILES)
    assert sorted(actual - inventory) == [], "panel ships files the inventory does not name"
    assert sorted(inventory - actual) == [], "inventory names files the panel does not ship"


def test_tc1233_required_floor_is_a_subset_of_the_panels_manifest():
    need_sibling()
    manifest = json.loads((OWN_REPO / "capabilities.json").read_text(encoding="utf-8"))["capabilities"]
    for name, payloads in contract.REQUIRED.items():
        assert name in manifest, "floor capability missing from the panel: " + name
        for kind, files in payloads.items():
            assert set(files) <= set(manifest[name][kind]), (name, kind)
