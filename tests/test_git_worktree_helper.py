"""Regression checks for Git calls made from WSL into Windows checkouts."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "lib" / "git-worktree.sh"


def test_drvfs_git_matches_windows_line_endings_and_file_modes():
    """A clean Windows checkout must not acquire a false +dirty image stamp."""
    source = HELPER.read_text(encoding="utf-8")

    assert 'case "$requested" in' in source
    assert "compat=(-c core.autocrlf=true -c core.filemode=false)" in source
    assert source.count('git "${compat[@]}"') == 2
