"""The hub's weekly trim: skip what cannot discard, and still fail on what can.

WHY THIS RUNS THE SCRIPT INSTEAD OF READING IT. The defect this script exists to
remove was a red `fstrim.service` every week, and the defect it nearly SHIPPED
with was subtler and of exactly the same family: the candidate list was read
straight off a pipe, which put the counting loop in a subshell, so the summary
would have read "0 trimmed" after a clean run and — the part that matters — the
exit status would have said SUCCESS after a genuine failure. No amount of
reading the source proves that either way. Running it does.

The stubs are the three commands it shells out to, on PATH ahead of the real
ones, so this needs no block devices, no root and no mounted filesystem.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "stack" / "autoinstall" / "homehub-fstrim.sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None,
                                reason="POSIX sh is absent (Windows dev box without git-bash)")

# target source, one per line, exactly as `findmnt --raw` emits it.
MOUNTS = """/ /dev/mapper/vg-root
/boot /dev/nvme0n1p2
/srv/library /dev/sdc1
/mnt/backup-drive /dev/sda1
"""

# The two USB spinning disks advertise nothing; the NVMe-backed pair advertise 2T.
DISC_MAX = {"/dev/mapper/vg-root": "2T", "/dev/nvme0n1p2": "2T",
            "/dev/sdc1": "0B", "/dev/sda1": "0B"}


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    # newline="\n" EXPLICITLY, and io.open rather than Path.write_text because
    # this repo's floor is older than that keyword: the dev box is Windows, and
    # a stub written with CRLF endings makes /bin/sh choke on the carriage
    # return with a message that blames the script under test.
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("#!/bin/sh\n" + body)
    path.chmod(0o755)


def run(tmp_path: Path, *, mounts: str = MOUNTS, disc_max=None, fstrim_fails=()):
    """Run the script against stub findmnt/lsblk/fstrim; return CompletedProcess."""
    disc_max = DISC_MAX if disc_max is None else disc_max
    stubs = tmp_path / "bin"
    stubs.mkdir()

    _stub(stubs, "findmnt", f"cat <<'MOUNTS'\n{mounts}MOUNTS\n")
    cases = "\n".join(f'    {device}) echo "{limit}" ;;' for device, limit in disc_max.items())
    # `lsblk --nodeps --noheadings --output DISC-MAX <device>` — the last
    # argument is the device, whatever precedes it.
    _stub(stubs, "lsblk", "for a in \"$@\"; do last=\"$a\"; done\ncase \"$last\" in\n"
          + cases + '\n    *) echo "" ;;\nesac\n')
    refuse = " ".join(fstrim_fails)
    _stub(stubs, "fstrim", 'for a in "$@"; do last="$a"; done\n'
          f'for bad in {refuse or "__none__"}; do\n'
          '    [ "$last" = "$bad" ] && { echo "fstrim: $last: FITRIM ioctl failed" >&2; exit 64; }\n'
          "done\n"
          'echo "$last: 1 GiB (1073741824 bytes) trimmed"\n')

    environment = dict(os.environ, PATH=str(stubs) + os.pathsep + os.environ["PATH"])
    return subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True,
                          env=environment, cwd=str(tmp_path))


def test_a_clean_run_trims_the_discardable_and_skips_the_rest(tmp_path):
    done = run(tmp_path)
    assert done.returncode == 0, done.stderr
    assert "fstrim: 2 trimmed, 2 skipped (no discard support), 0 failed" in done.stdout
    assert "/srv/library" in done.stdout and "/mnt/backup-drive" in done.stdout


def test_a_skip_is_never_silent(tmp_path):
    """A drive that quietly stops advertising discard must be readable, not absent."""
    done = run(tmp_path)
    assert "skip /srv/library (/dev/sdc1): the device advertises no discard support" in done.stdout


def test_a_device_that_advertises_discard_and_then_refuses_is_still_a_failure(tmp_path):
    """The exit status must survive the loop — this is the subshell trap."""
    done = run(tmp_path, fstrim_fails=("/",))
    assert done.returncode != 0, "a real trim failure was laundered into success"
    assert "1 trimmed, 2 skipped (no discard support), 1 failed" in done.stdout
    assert "FAILED to trim /" in done.stdout


def test_one_device_is_asked_once_however_many_times_it_is_mounted(tmp_path):
    """Stock fstrim deduplicates by device when handed the whole list at once."""
    mounts = MOUNTS + "/var/lib/docker /dev/mapper/vg-root\n"
    done = run(tmp_path, mounts=mounts)
    assert "2 trimmed, 2 skipped" in done.stdout, done.stdout


def test_a_device_lsblk_cannot_describe_is_skipped_rather_than_assumed(tmp_path):
    """An unknown answer is not a licence to issue an ioctl and hope."""
    done = run(tmp_path, disc_max={"/dev/mapper/vg-root": "2T"})
    assert done.returncode == 0
    assert "1 trimmed, 3 skipped (no discard support), 0 failed" in done.stdout


def test_nothing_mounted_is_not_a_failure(tmp_path):
    done = run(tmp_path, mounts="")
    assert done.returncode == 0
    assert "0 trimmed, 0 skipped (no discard support), 0 failed" in done.stdout
