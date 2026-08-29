"""The shell suites under stack/, driven from pytest so CI runs them too.

WHY THIS FILE IS A WRAPPER AND NOT A REWRITE. The suites are bash because the
things they test are bash — a plan run's directory naming, a retention delete, a
tar extracted as root into a home directory. Reimplementing their assertions in
Python would create a second definition of "passing" that drifts from the one an
operator runs on the box, and this repo has already paid for that shape twice
(the traceability count, the LabVerify count). One implementation, two callers:
`bash stack/run-hermetic-tests.sh` on the hub, and this in CI.

WHAT THEY NEED, and why it is a SKIP rather than a failure when absent: bash,
tar, gzip, zstd, rsync, sha256sum, gpg, awk, find. A Windows dev box has none of
the last few, and failing there would train people to ignore the suite. CI is
Linux and has them all, so CI really does run them — the skip cannot hide a
regression on the platform that matters.

Tiering: the run-them-all test is "full" (about a minute together, right for a
pre-merge gate and wrong for every iteration); the existence check is also
"smoke", both because it is instant and because a smoke tier that collects ZERO
tests makes pytest exit 5, which reads as a broken harness rather than an empty
one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "stack" / "run-hermetic-tests.sh"

REQUIRED_TOOLS = ("bash", "tar", "gzip", "zstd", "rsync", "sha256sum", "gpg", "awk", "find")


def _missing_tools() -> list[str]:
    return [t for t in REQUIRED_TOOLS if shutil.which(t) is None]


def _suite_names() -> list[str]:
    """Ask the runner what it runs, rather than restating the list here.

    Two copies of this list is how one of them goes quietly stale — the same
    reasoning that keeps the restore set list inside restore-volumes.sh.
    """
    out = subprocess.run(
        ["bash", str(RUNNER), "--list"], capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


# The slow suite is "full" (about a minute); the cheap existence check below is
# also marked "smoke" so the every-push tier collects something.
pytestmark = pytest.mark.full

_missing = _missing_tools()
_skip_reason = ""
if sys.platform.startswith("win"):
    _skip_reason = "POSIX shell suites; run them in WSL or on the hub"
elif os.geteuid() == 0:
    # profile.test.sh used to stash paths under $HOME and the runner still
    # refuses root for that reason; a root CI runner would otherwise report a
    # precondition failure as a test failure.
    _skip_reason = "the runner refuses root (one suite works under $HOME)"
elif _missing:
    _skip_reason = f"missing tool(s): {', '.join(_missing)}"


# SMOKE, and it is the one cheap thing worth asserting on every push: that every
# suite the runner claims to run is actually present. Deleting a suite file is a
# one-line change that would otherwise show up as a smaller green number nobody
# reads. It also keeps the smoke tier from collecting ZERO tests, which pytest
# reports as exit 5 — an error that looks like a broken harness rather than an
# empty one.
@pytest.mark.smoke
@pytest.mark.skipif(bool(_skip_reason), reason=_skip_reason or "n/a")
def test_runner_exists_and_lists_its_suites():
    assert RUNNER.is_file(), f"{RUNNER} is missing"
    names = _suite_names()
    assert names, "the runner lists no suites"
    for n in names:
        assert (REPO / "stack" / n).is_file(), f"{n} is listed but not present"


@pytest.mark.skipif(bool(_skip_reason), reason=_skip_reason or "n/a")
def test_every_hermetic_suite_passes():
    """Run all of them in one process and surface the whole output on failure.

    The runner already prints only failures plus a per-suite tally, so a green
    run is short and a red one carries everything needed to act.
    """
    proc = subprocess.run(
        ["bash", str(RUNNER)], capture_output=True, text=True, cwd=str(REPO)
    )
    if proc.returncode != 0:
        pytest.fail(
            "hermetic shell suites failed (exit "
            f"{proc.returncode})\n\n--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )
    # A green run must also have actually asserted something. A runner that
    # silently ran zero suites would exit 0, and "0 PASS 0 FAIL" is the shape
    # this repo keeps finding at the bottom of a count nobody checked.
    tally = [ln for ln in proc.stdout.splitlines() if "PASS" in ln and "across" in ln]
    assert tally, f"no summary line in the runner's output:\n{proc.stdout}"
    passed = int(tally[-1].split()[0])
    assert passed > 100, f"only {passed} assertions ran; expected well over 100\n{proc.stdout}"
