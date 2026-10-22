"""SR-084: the touch filter's protection survives a reboot (LLR-1142..1144).

The unit gains a private state directory and nothing wider; OFF deletes the
persisted protection; the unit reaches the panel only through firstboot, so a
system-lane paired release carries it. The application half (what is written,
when it is restored) lives in OfficeWallNaglight `touchfilter/persist.py`; the
cross-repo test here holds both sides of the file-name contract.
"""

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack" / "autoinstall" / "wall"
UNIT = WALL / "wall-touch-filter.service"
HELPER = WALL / "configure-touch-filter.sh"


def directives(text):
    """[Service] key=value pairs in order (comments and blanks dropped)."""
    service = text.split("[Service]", 1)[1].split("\n[", 1)[0]
    pairs = []
    for line in service.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            pairs.append((key, value))
    return pairs


def bash():
    return shutil.which("bash") or next(
        (str(p) for p in (Path("C:/Program Files/Git/bin/bash.exe"),) if p.is_file()), None)


def test_tc1265_the_unit_has_a_private_state_directory_and_nothing_wider_sr084():
    pairs = directives(UNIT.read_text(encoding="utf-8"))
    keys = [key for key, _ in pairs]
    values = dict(pairs)
    assert keys.count("StateDirectory") == 1 and values["StateDirectory"] == "wall-touch-filter"
    assert values["StateDirectoryMode"] == "0700"
    # Nothing wider: the sandbox is unchanged and no other path became writable.
    assert values["ProtectSystem"] == "strict" and values["ProtectHome"] == "true"
    assert [value for key, value in pairs if key == "ReadWritePaths"] == ["/run/wall-touch-filter"]
    for widening in ("CacheDirectory", "LogsDirectory", "ConfigurationDirectory", "ReadOnlyPaths",
                     "BindPaths", "PrivateTmp"):
        if widening == "PrivateTmp":
            assert values[widening] == "true"
        else:
            assert widening not in values
    assert values["User"] == "root" and values["RuntimeDirectory"] == "wall-touch-filter"


def test_tc1266_off_deletes_the_persisted_protection_and_only_it_sr084(tmp_path):
    """Run the helper's real OFF branch with systemctl/udevadm stubbed."""
    shell = bash()
    if not shell:
        pytest.skip("bash is required to run configure-touch-filter.sh")
    state = tmp_path / "state"
    state.mkdir()
    (state / "protection.json").write_text("{}")
    (state / "unrelated").write_text("kept")
    env_file = tmp_path / "wall.env"
    env_file.write_text("TOUCH_FILTER_MODE=off\n")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name in ("systemctl", "udevadm"):
        with open(stubs / name, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("#!/bin/sh\necho \"$0 $*\" >> \"$STUB_LOG\"\n")
        (stubs / name).chmod(0o755)
    log = tmp_path / "calls.log"
    environment = {**os.environ, "WALL_ENV_FILE": env_file.as_posix(), "WALL_TOUCH_STATE_DIR": state.as_posix(),
                   "STUB_LOG": log.as_posix(), "PATH": stubs.as_posix() + os.pathsep + os.environ["PATH"]}
    # The OFF branch removes fixed system paths with rm -f; on a test host
    # those do not exist (or are not ours), and -f makes that a no-op.
    result = subprocess.run([shell, HELPER.as_posix()], env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "Touch filter OFF" in result.stdout
    assert not (state / "protection.json").exists()
    assert (state / "unrelated").read_text() == "kept"


def test_tc1266_every_mode_other_than_off_leaves_the_file_to_the_daemon_sr084():
    helper = HELPER.read_text(encoding="utf-8")
    off = helper[helper.index('if [ "$mode" = off ]; then'):helper.index("    exit 0\nfi")]
    assert 'rm -f -- "$state_dir/protection.json"' in off
    assert helper.count("protection.json") == 1
    assert "state_dir=${WALL_TOUCH_STATE_DIR:-/var/lib/wall-touch-filter}" in helper


def test_tc1267_the_unit_reaches_the_panel_only_through_firstboot_sr084():
    """The paired lane's system manifest follows firstboot; user-data installs
    nothing here, so a reimage and a release carry the same unit."""
    firstboot = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    helper = HELPER.read_text(encoding="utf-8")
    assert 'bash "$PAYLOAD/configure-touch-filter.sh"' in firstboot
    assert 'install -m 0644 "$payload/wall-touch-filter.service" /etc/systemd/system/wall-touch-filter.service' in helper
    assert helper.index("install -m 0644 \"$payload/wall-touch-filter.service\"") < helper.index("systemctl daemon-reload")
    assert "wall-touch-filter" not in (WALL / "user-data").read_text(encoding="utf-8")


@pytest.mark.skipif(not os.environ.get("TOUCH_APP_REPO"), reason="the daemon comes from the sibling app repo")
def test_tc1268_the_daemon_and_the_image_agree_on_the_state_file_sr084():
    """Both sides of the contract: the unit's StateDirectory is what the
    daemon reads (systemd sets STATE_DIRECTORY to it), and the file OFF
    deletes is the file the daemon writes."""
    sys.path.insert(0, os.environ["TOUCH_APP_REPO"])
    from touchfilter import persist
    from touchfilter.adaptive_runtime import state_directory

    values = dict(directives(UNIT.read_text(encoding="utf-8")))
    assert persist.DEFAULT_DIRECTORY == "/var/lib/" + values["StateDirectory"]
    helper = HELPER.read_text(encoding="utf-8")
    assert re.search(r'rm -f -- "\$state_dir/%s"' % re.escape(persist.FILENAME), helper)
    os.environ["STATE_DIRECTORY"] = persist.DEFAULT_DIRECTORY
    try:
        assert state_directory() == persist.DEFAULT_DIRECTORY
    finally:
        del os.environ["STATE_DIRECTORY"]
