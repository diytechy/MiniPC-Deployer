"""The compositor freeze watchdog, taken from the panel into the wall image.

Verifies: SR-090 / LLR-1166
Cases:    TC-1305, TC-1311

The panel's watchdog was hand-installed on 2026-09-15 and lived in no repo. The
payload carries the live copies byte for byte; these tests pin those bytes to
the hashes read off the panel on 2026-10-22 (so a later edit is a deliberate
change of this test, not drift), and run the firstboot block that installs them.
"""

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack" / "autoinstall" / "wall"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")

# Read from the panel 2026-10-22 (sha256sum over the live files):
#   /usr/local/sbin/wall-freeze-watch.sh, /etc/systemd/system/wall-freeze-watch.service,
#   /etc/wall-panel/freeze-watch.env
#
# wall-freeze-watch.sh CHANGED DELIBERATELY on 2026-10-26 (SR-122, LLR-1331): it
# also writes its last verdict to /run/wall-freeze-state/status.json for the hub's
# metrics log. The live copy read 4e4e70803616bb7113c53b3200bb148ded5302e14b9b943653a253698dd0f39f
# before that; this is the payload's new hash, live once the paired release ships it.
LIVE_SHA256 = {
    "wall-freeze-watch.sh": "11e2e1e509c2cebf810a0a55995a67e5eea44eff082fa592bf817fbcb2923e60",
    "wall-freeze-watch.service": "cf070193ebf4735815039b20b27904176a11a1862f8ca8cd6d6379df113746b6",
    "wall-freeze-watch.env": "b2a6ae4c2c5cf822bba292fe25e04541bf764659b3570e367f2603b4e9f243f3",
}


# ── TC-1305: the payload is the live watchdog ───────────────────────────────


@pytest.mark.parametrize("name", sorted(LIVE_SHA256))
def test_payload_files_are_the_panels_live_copies_tc1305(name):
    assert hashlib.sha256((WALL / name).read_bytes()).hexdigest() == LIVE_SHA256[name]


def test_the_image_keeps_recover_on_as_live_tc1305():
    env = (WALL / "wall-freeze-watch.env").read_text(encoding="utf-8")
    assert re.search(r"^RECOVER=1$", env, re.M)
    assert re.search(r"^ENV_FILE=/etc/wall-panel/freeze-watch.env$", (WALL / "wall-freeze-watch.sh").read_text(
        encoding="utf-8"), re.M)  # fmt: skip
    unit = (WALL / "wall-freeze-watch.service").read_text(encoding="utf-8")
    assert (
        "ExecStart=/usr/local/sbin/wall-freeze-watch.sh" in unit
        and "WantedBy=multi-user.target" in unit
    )


def test_the_script_is_executable_in_git_tc1305():
    out = subprocess.run(["git", "-C", str(WALL), "ls-files", "-s", "wall-freeze-watch.sh"],
                         capture_output=True, text=True, check=True).stdout  # fmt: skip
    assert out.startswith("100755"), out


def test_the_knob_is_declared_and_defaults_on_tc1305():
    assert re.search(
        r"^WALL_FREEZE_WATCH_ENABLED=true$",
        (WALL / "wall.env.example").read_text(encoding="utf-8"),
        re.M,
    )


# ── TC-1311: the firstboot block, executed ───────────────────────────────────


def block():
    text = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    found = re.search(r"# BEGIN freeze-watch\n(.*?)# END freeze-watch\n", text, re.S)
    assert found, "wall-firstboot.sh lost its freeze-watch block"
    return found.group(1)


def run_block(tmp_path, knob, same=True):
    calls = tmp_path / "calls"
    script = "\n".join([
        "set -euo pipefail",
        'PAYLOAD=/payload; PROVISION_FAILED=0',
        'log() { echo "LOG $*"; }',
        'fail_step() { PROVISION_FAILED=1; echo "FAIL $*" >>"%s"; }' % calls.as_posix(),
        'cmp() { %s; }' % ("return 0" if same else "return 1"),
        'install() { echo "install $*" >>"%s"; }' % calls.as_posix(),
        'systemctl() { echo "systemctl $*" >>"%s"; }' % calls.as_posix(),
        'enable_unit_now() { shift; echo "enable_unit_now $*" >>"%s"; }' % calls.as_posix(),
        ("WALL_FREEZE_WATCH_ENABLED=%s" % knob) if knob is not None else "unset WALL_FREEZE_WATCH_ENABLED",
        block(),
        'echo "failed=$PROVISION_FAILED"',
    ])  # fmt: skip
    done = subprocess.run(
        [BASH, "-s"], input=script, capture_output=True, text=True, timeout=60
    )
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return done, lines


INSTALLS = [
    "install -m 0755 /payload/wall-freeze-watch.sh /usr/local/sbin/wall-freeze-watch.sh",
    "install -m 0644 /payload/wall-freeze-watch.service /etc/systemd/system/wall-freeze-watch.service",
    "install -m 0644 /payload/wall-freeze-watch.env /etc/wall-panel/freeze-watch.env",
]


@needs_bash
@pytest.mark.parametrize("knob", [None, "true"])
def test_enabled_installs_and_enables_without_a_restart_when_unchanged_tc1311(
    tmp_path, knob
):
    done, lines = run_block(tmp_path, knob)
    assert done.returncode == 0 and "failed=0" in done.stdout, done.stderr
    assert lines[:3] == INSTALLS
    assert "enable_unit_now wall-freeze-watch.service" in lines
    assert not any("restart" in line for line in lines), (
        "an unchanged watchdog keeps its failure count"
    )


@needs_bash
def test_a_changed_definition_restarts_the_watchdog_tc1311(tmp_path):
    done, lines = run_block(tmp_path, "true", same=False)
    assert (
        "systemctl restart wall-freeze-watch.service" in lines
        and "failed=0" in done.stdout
    )


@needs_bash
def test_disabled_keeps_the_files_and_stops_the_unit_tc1311(tmp_path):
    done, lines = run_block(tmp_path, "false")
    assert lines[:3] == INSTALLS, (
        "files stay installed so the release manifest's hashes hold"
    )
    assert "systemctl disable --now wall-freeze-watch.service" in lines
    assert not any(line.startswith("enable_unit_now") for line in lines)


@needs_bash
def test_a_bad_knob_is_a_failed_step_and_the_watchdog_stays_on_tc1311(tmp_path):
    done, lines = run_block(tmp_path, "yes")
    assert "failed=1" in done.stdout and any(
        "WALL_FREEZE_WATCH_ENABLED must be" in line for line in lines
    )
    assert "enable_unit_now wall-freeze-watch.service" in lines


def test_install_lines_have_the_form_the_release_manifest_reads_tc1311():
    """HomeHub's panel_system_manifest.py reads `install -m MODE "$PAYLOAD/x" DEST` and
    skips any line that says fail_step: the guard must be the block's own function."""
    lines = [line for line in block().splitlines() if line.startswith("install -m")]
    assert len(lines) == 3
    for line in lines:
        assert re.match(
            r'install -m 0[67][0-9]{2} "\$PAYLOAD/wall-freeze-watch\.[a-z]+" /\S+ \|\| freeze_failed ',
            line,
        )
        assert "fail_step" not in line
    assert os.path.basename(__file__) == "test_wall_freeze_watch.py"
