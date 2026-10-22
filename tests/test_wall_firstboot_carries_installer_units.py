"""Every unit file the wall autoinstall places is also installed by wall-firstboot.sh.

A unit that only stack/autoinstall/wall/user-data copies is frozen at install
time on a live panel: the paired release lane carries exactly what
wall-firstboot.sh installs from $PAYLOAD (HomeHub
scripts/deploy/panel_system_manifest.py reads those lines), so editing such a
unit deploys "green" and changes nothing. That is how A58's kiosk export missed
the running cage on 2026-10-14, and the audit that followed named eight units
in the same state; five more were hidden by a check that counted a unit as
carried when a later step merely ENABLED it.

TC-1363 reads the real user-data late-commands and the real firstboot, and then
runs firstboot's refresh loop with `install` and `systemctl` stubbed. HomeHub's
InstallerOnlyFilesTests is the other side of the contract: the generated
release manifest holds a hash for each of these destinations.

Verifies: TC-1363 (SR-101, LLR-1212)
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack" / "autoinstall" / "wall"
USER_DATA = WALL / "user-data"
FIRSTBOOT = WALL / "wall-firstboot.sh"


def _joined(text):
    return re.sub(r"\\\n\s*", " ", text)


def _installer_units():
    """Unit files user-data installs into /etc/systemd/system, by name."""
    found = re.findall(
        r"^\s*- (?:cp|install)\b[^\n]*?/target/etc/systemd/system/(\S+)\s*$",
        USER_DATA.read_text(encoding="utf-8"), re.M)
    return sorted(set(found))


def _firstboot_installed_units():
    """Unit names firstboot installs from $PAYLOAD into /etc/systemd/system,
    directly or through a `for _x in ...; do ... install "$PAYLOAD/$_x"` loop."""
    text = _joined(FIRSTBOOT.read_text(encoding="utf-8"))
    lines = text.splitlines()
    names = set()
    loop = None
    for line in lines:
        if line.strip().startswith("#"):
            continue
        m = re.match(r"\s*for\s+(\w+)\s+in\s+(.*?);\s*do\s*$", line)
        if m:
            loop = (m.group(1), m.group(2).split())
            continue
        if re.match(r"\s*done\s*$", line):
            loop = None
        for m in re.finditer(r'install\s+-m\s+0644\s+"\$PAYLOAD/([^"]+)"\s+"?/etc/systemd/system/([^"\s]+)"?', line):
            src, dest = m.group(1), m.group(2)
            if loop and ("$" + loop[0]) in src:
                names.update(item for item in loop[1] if dest == "$" + loop[0])
            elif "$" not in src:
                names.add(dest)
    # The capability helper firstboot runs installs its units from its own
    # directory (the payload): `install -m 0644 "$(dirname "$0")/NAME" ...`.
    for helper in WALL.glob("*.sh"):
        for m in re.finditer(r'install\s+-m\s+0644\s+"\$\(dirname "\$0"\)/([^"]+)"\s+/etc/systemd/system/(\S+)',
                             helper.read_text(encoding="utf-8")):
            if m.group(1) == m.group(2):
                names.add(m.group(2))
    return names


def _refresh_loop_units():
    m = re.search(r"for _u in (.*?); do", _joined(_refresh_block()), re.S)
    return m.group(1).split()


def test_tc1363_the_parse_is_not_blind_llr1212():
    units = _installer_units()
    assert "wall-firstboot.service" in units and "wall-sync-frame.timer" in units, units
    assert "wall-kiosk-loop.service" in _firstboot_installed_units()   # a known loop entry


def test_tc1363_every_installer_unit_is_also_installed_by_firstboot_llr1212():
    missing = sorted(set(_installer_units()) - _firstboot_installed_units())
    assert missing == [], (
        "installed by user-data only, so no release can update them on a live panel: %s" % missing)


def test_tc1363_every_named_unit_exists_in_the_payload_llr1212():
    for name in _installer_units():
        assert (WALL / name).is_file(), name


def _bash():
    git = Path("C:/Program Files/Git/bin/bash.exe")
    if git.exists():
        return str(git)
    exe = shutil.which("bash")
    if not exe:
        pytest.skip("bash not available")
    return exe


def _px(path):
    text = Path(path).as_posix()
    if len(text) > 2 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:]
    return text


def _refresh_block():
    text = FIRSTBOOT.read_text(encoding="utf-8")
    a = text.index("# ── 5c. the unit files")
    b = text.index("# ── 6. D-W4", a)
    return text[a:b]


def test_tc1363_the_refresh_loop_installs_each_unit_and_skips_an_absent_one_llr1212(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    units = _refresh_loop_units()
    assert set(units) <= set(_installer_units()), "the loop names a unit user-data does not install"
    absent = "wall-occupancy.timer"
    for name in units:
        if name != absent:
            (payload / name).write_bytes(b"[Unit]\n")
    calls = tmp_path / "calls.txt"
    calls.write_bytes(b"")
    script = tmp_path / "block.sh"
    preamble = (
        "set -euo pipefail\n"
        'fail_step() { echo "FAIL $*"; }\n'
        'install() { echo "install $*" >> "$CALLS"; }\n'
        'systemctl() { echo "systemctl $*" >> "$CALLS"; }\n'
        # SR-112's drop-in retirement runs inside this block; its behaviour is
        # TC-1421's, so here it is only recorded, in order.
        'retire_obsolete_dropin() { echo "retire $1/$2" >> "$CALLS"; }\n'
        'PAYLOAD="%s"\n' % _px(payload)
    )
    script.write_bytes((preamble + _refresh_block() + "\necho BLOCK-DONE\n").encode("utf-8"))
    proc = subprocess.run([_bash(), _px(script)], env=dict(os.environ, CALLS=_px(calls)),
                          capture_output=True, text=True)
    assert proc.returncode == 0 and "BLOCK-DONE" in proc.stdout, proc.stdout + proc.stderr
    got = calls.read_text(encoding="utf-8").splitlines()
    expected = ['install -m 0644 %s/%s /etc/systemd/system/%s' % (_px(payload), n, n)
                for n in units if n != absent]
    assert sorted(got[:-2]) == sorted(expected), got
    assert got[-2] == "retire /etc/systemd/system/wall-sync-frame.timer.d/10-cadence.conf"
    assert got[-1] == "systemctl daemon-reload"
