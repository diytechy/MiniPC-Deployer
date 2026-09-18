"""The ProjectM preset curation: the lock's format, and the parse that reads it.

WHY THIS FILE EXISTS. `presets.lock` was empty from the day it was written until
the Owner ruled the pack in on 2026-09-18, and the FIRST non-empty lock broke
the script that consumes it -- on every single line. The reproduce path split
each entry with `read -r sha rel _`, which splits on whitespace, and essentially
every Milkdrop filename contains spaces:

    ! Transition/Fast transition to black - levels effect === ... .milk

so `rel` parsed as `!` and all 9795 presets were reported missing. Nothing
caught it because an empty lock exercises no parsing at all. These tests read
the real committed lock, so they cannot pass vacuously again.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / "stack/autoinstall/wall/projectm-presets"
LOCK = PAYLOAD / "presets.lock"
SELECT = PAYLOAD / "presets.select"
SCRIPT = ROOT / "stack/autoinstall/wall/build-projectm-presets.sh"

UPSTREAM = "https://github.com/projectM-visualizer/presets-cream-of-the-crop"
COMMIT = "0180df21f5e0bd39b9060cc5de420ed2f1f9e509"


def _entries(path):
    """Every non-comment, non-blank line, exactly as the shell reads them."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        out.append(line)
    return out


def test_the_lock_and_the_selection_describe_the_same_set():
    lock, select = _entries(LOCK), _entries(SELECT)
    assert len(lock) == len(select), "every selected preset is locked and vice versa"
    assert len(lock) == 9795, "the Owner ruled the whole pack in; a shrink is a real change"
    assert {line[66:] for line in lock} == set(select)


def test_the_lock_parses_positionally_and_survives_spaces():
    """The bug this file exists for, asserted on the real data.

    A sha256 is exactly 64 hex characters and the separator is exactly two
    spaces, so the path starts at offset 66 and runs to end of line. Splitting
    on whitespace instead is what reported every preset missing.
    """
    lines = _entries(LOCK)
    spaced = 0
    for line in lines:
        sha, sep, rel = line[:64], line[64:66], line[66:]
        assert len(sha) == 64 and all(c in "0123456789abcdef" for c in sha), line[:80]
        assert sep == "  ", "exactly two spaces separate the hash from the path"
        assert rel and not rel.startswith(" "), line[:80]
        assert rel.endswith(".milk")
        if " " in rel:
            spaced += 1
            # The naive parse would have produced this, and it is why the split
            # must be positional rather than by field.
            assert rel.split()[0] != rel
    assert spaced > len(lines) * 0.9, (
        "the overwhelming majority of these paths contain spaces -- if this ever "
        "drops, the naive parse would start passing and stop proving anything"
    )


def test_no_preset_is_committed_anywhere_in_this_repo():
    """The basis of the Owner's licence ruling: install, never redistribute."""
    milk = [p for p in ROOT.rglob("*.milk") if ".out-wall" not in p.parts and ".git" not in p.parts]
    assert milk == [], "presets must never be committed; only select + lock are"


def test_the_payload_output_is_gitignored():
    """136 MB of an unlicensed pack must not be one `git add -A` from history."""
    assert ".out-wall/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_the_lock_records_one_upstream_and_no_local_path():
    """The Owner's question: no deployment may depend on anyone's disk."""
    header = LOCK.read_text(encoding="utf-8").splitlines()
    upstream = [line for line in header if line.startswith("# Upstream:")]
    assert upstream == [f"# Upstream: {UPSTREAM}@{COMMIT}"], upstream
    body = LOCK.read_text(encoding="utf-8")
    for forbidden in ("C:\\", "/mnt/c/", "Personal/ProjectM", "\\Projects\\"):
        assert forbidden not in body, f"the lock names a local path: {forbidden}"


@pytest.mark.parametrize("needle", [
    "IFS= read -r line",          # the space-safe lock reader
    "sha=${line:0:64}",           # positional, not field-split
    "rel=${line:66}",
    "IFS= read -r rel",           # the space-safe selection reader
])
def test_the_script_reads_both_files_space_safely(needle):
    assert needle in SCRIPT.read_text(encoding="utf-8"), needle
