"""LLR-1128 / TC-1224, TC-1225: the runtime paths both repos name.

tests/fixtures/runtime-paths-v1.json is BYTE-IDENTICAL to the copy in
OfficeWallNaglight. Neither repo imports the other, so a socket or state-file path
stated on both sides can drift silently: a rename on one side passes its own
tests and fails only on the panel. This file asserts THIS repo's units and
scripts against the fixture; the panel repo's tests/runtime-paths-contract.test.mjs
asserts its sources against the same file.

Each listed file must contain the path, or each 'contains' part where a file
composes the path (a directory constant plus a filename, a RuntimeDirectory=).
"""

import hashlib
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OWN_REPO = Path(os.environ.get("OWN_REPO") or ROOT.parent / "OfficeWallNaglight")
FIXTURE = ROOT / "tests/fixtures/runtime-paths-v1.json"
ROWS = json.loads(FIXTURE.read_text(encoding="utf-8"))["paths"]


def test_tc1224_every_row_names_both_sides():
    assert len(ROWS) >= 12
    roles = [r["role"] for r in ROWS]
    assert len(roles) == len(set(roles))
    for row in ROWS:
        assert row["path"].startswith(("/run/", "/etc/", "/var/", "/opt/")), row["role"]
        assert row["own"], f"{row['role']}: no panel-side file"
        assert row["mpd"], f"{row['role']}: no MiniPC-side file"


@pytest.mark.parametrize(
    "row,entry",
    [pytest.param(r, e, id=f"{r['role']}::{e['file']}") for r in ROWS for e in r["mpd"]],
)
def test_tc1224_every_mpd_file_contains_its_path(row, entry):
    file = ROOT / entry["file"]
    assert file.is_file(), f"{row['role']}: {entry['file']} does not exist"
    text = file.read_text(encoding="utf-8")
    for literal in entry.get("contains", [row["path"]]):
        assert literal in text, f"{row['role']}: {entry['file']} does not contain {literal}"


def test_tc1225_fixture_is_byte_identical_to_the_panel_copy():
    sibling = OWN_REPO / "tests/fixtures/runtime-paths-v1.json"
    if not sibling.is_file():
        pytest.skip("OfficeWallNaglight checkout (or its copy of the fixture) is absent; set OWN_REPO")
    assert hashlib.sha256(sibling.read_bytes()).hexdigest() == hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), \
        "runtime-paths-v1.json differs between the repos"
