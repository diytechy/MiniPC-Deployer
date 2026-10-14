"""SR-020 / LLR-1135: the wall image masks Ubuntu's backlight save/restore, so
wall-sleep.sh is the only thing that sets the panel's brightness at boot.

The firstboot block is EXECUTED with bash (cut out of the real script between its
markers) against a fake `systemctl` that records its arguments, not read as
text. Needs bash.

Implements tests: TC-1256. Does not prove that systemd treats a masked template
as masking its instances; that was checked on the live panel when the fix
shipped (`systemctl show -p LoadState systemd-backlight@backlight:intel_backlight`
reads `masked`).
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parents[1] / "stack/autoinstall/wall"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")


def block() -> str:
    text = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    found = re.search(r"# BEGIN backlight-restore-mask\n(.*?)# END backlight-restore-mask\n", text, re.S)
    assert found, "wall-firstboot.sh lost its backlight-restore-mask block"
    return found.group(1)


def run(tmp_path: Path, systemctl_status: int):
    """Run the block with `systemctl` replaced by a shell function (a function
    outranks any binary, and a PATH entry would not survive Git Bash splitting
    `C:/...` on its colon). Returns (exit code, stdout, stderr, the argument
    lines systemctl received)."""
    calls = tmp_path / "systemctl.calls"
    script = ('set -euo pipefail\n'
              'log() { echo "[wall-firstboot] $*"; }\n'
              'warn() { echo "[wall-firstboot] WARNING: $*" >&2; }\n'
              f'systemctl() {{ printf "%s\\n" "$*" >> "{calls.as_posix()}"; return {systemctl_status}; }}\n'
              + block() + 'echo AFTER-BLOCK\n')
    done = subprocess.run([BASH, "-s"], input=script.encode("utf-8"), capture_output=True, timeout=60,
                          env=dict(os.environ))
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return (done.returncode, done.stdout.decode("utf-8", "replace"),
            done.stderr.decode("utf-8", "replace"), lines)


@needs_bash
def test_tc1256_firstboot_masks_the_backlight_restore_template(tmp_path):
    code, out, err, calls = run(tmp_path, 0)
    assert code == 0, err
    # The TEMPLATE, so the mask holds whatever the hardware names its backlight;
    # an instance name here would be the hardware dependence this fix exists to avoid.
    assert calls == ["mask systemd-backlight@.service"]
    assert "quirk 7: systemd-backlight@ masked" in out
    assert "AFTER-BLOCK" in out


@needs_bash
def test_tc1256_a_failed_mask_warns_and_firstboot_carries_on(tmp_path):
    code, out, err, calls = run(tmp_path, 1)
    assert code == 0, err
    assert calls == ["mask systemd-backlight@.service"]
    assert "WARNING: quirk 7: could not mask systemd-backlight@.service" in err
    assert "AFTER-BLOCK" in out


def test_tc1256_the_block_names_no_backlight_instance():
    assert not re.search(r"systemd-backlight@[^.\s]", block())
