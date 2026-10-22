"""Ask git about this checkout without failing on a transient shared-.git fault.

WHY THIS EXISTS. In the 2026-10-22 baseline, three tests that only ask git
what is committed (test_sleep_wiring, test_wall_disk_and_log_cap tc1263,
test_wall_payload_executables) failed in the same hour with

    fatal: Invalid path '/mnt': No such file or directory

from a plain `git -C <repo> ls-files`. Each passed alone. HomeHub's lab Pester
hit the identical message from `git rev-parse HEAD` at the same time. The
repository's .git is shared by every linked worktree, and several sessions
were creating and driving worktrees on it (some from WSL) while the gate ran.
Windows git prints exactly that message when a configuration file it reads
names a /mnt/... path (reproduced with `core.worktree=/mnt/...`); nothing in
this repo writes one, and the writer was not identified.

So a git query gets three tries, one second apart, and only a `fatal:` exit is
retried. A fault that outlasts them still fails the test, with git's own
stderr in the message. What each test asserts is unchanged: this bounds the
environment, it does not relax the contract.
"""

import subprocess
import time

ATTEMPTS = 3
PAUSE_SEC = 1.0


def run_git(argv, *, check=True, **kwargs):
    """subprocess.run(argv) for a git query, retried on a transient `fatal:`.

    `argv` starts with "git". Output is captured (text unless the caller passes
    text=False). With check=True a final failure raises CalledProcessError
    carrying git's stderr, exactly as subprocess.run(check=True) would.
    """
    assert argv and argv[0] == "git", argv
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", 120)
    for attempt in range(1, ATTEMPTS + 1):
        done = subprocess.run(argv, check=False, **kwargs)
        err = done.stderr if isinstance(done.stderr, str) else (done.stderr or b"").decode("utf-8", "replace")
        if done.returncode == 0 or not err.lstrip().startswith("fatal:") or attempt == ATTEMPTS:
            break
        time.sleep(PAUSE_SEC)
    if check and done.returncode != 0:
        raise subprocess.CalledProcessError(done.returncode, argv, done.stdout, done.stderr)
    return done
