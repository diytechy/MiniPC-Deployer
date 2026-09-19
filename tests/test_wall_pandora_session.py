"""wall-pandora-session: carrying the Pandora sign-in across a reimage.

The Owner asked whether the sign-in can survive a redeployment (2026-09-19).
The measurement said a redeployment never loses it and a REIMAGE always does,
so the tool is a save/restore pair and these are the properties that make it
safe to run from firstboot on every boot: it refuses a live profile, it refuses
to clobber a sign-in, and it refuses an archive that is not one.
"""

import importlib.util
import io
import sqlite3
import sys
import tarfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "stack" / "autoinstall" / "wall" / "wall-pandora-session.py"

# Chromium stores cookie expiry as microseconds since 1601-01-01.
EPOCH_OFFSET = 11_644_473_600


@pytest.fixture()
def tool(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("wall_pandora_session", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    profile = tmp_path / "home" / "panel" / ".config" / "officewall-shell"
    partition = profile / "Partitions" / "pandora"
    partition.mkdir(parents=True)
    monkeypatch.setattr(module, "PROFILE", profile)
    monkeypatch.setattr(module, "PARTITION", partition)
    # The real tool needs root to read a 0700 partition and to chown the result.
    # Neither is true of a tmp_path, and neither is what these tests are about.
    # `raising=False` throughout: os.geteuid/getuid/chown do not exist on the
    # development Windows box, and these tests are about the tool's decisions,
    # not about POSIX.
    monkeypatch.setattr(module.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(module, "panel_ids", lambda: (0, 0))
    monkeypatch.setattr(module.os, "chown", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(module, "keyring_present", lambda: [])
    return module


def write_cookies(partition, names=("at", "wrt"), days=30):
    expires = int((time.time() + days * 86400 + EPOCH_OFFSET) * 1_000_000)
    connection = sqlite3.connect(partition / "Cookies")
    connection.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, expires_utc INTEGER)")
    for name in names:
        connection.execute("INSERT INTO cookies VALUES (?, ?, ?)", (".pandora.com", name, expires))
    connection.commit()
    connection.close()


def test_a_saved_archive_restores_the_sign_in_onto_a_fresh_profile(tool, tmp_path):
    write_cookies(tool.PARTITION)
    (tool.PARTITION / "Local Storage").mkdir()
    (tool.PARTITION / "Local Storage" / "leveldb.log").write_bytes(b"state")
    archive = tmp_path / "pandora-session.tar.gz"
    assert tool.save(str(archive), False) == 0
    if sys.platform != "win32":
        assert archive.stat().st_mode & 0o777 == 0o600

    # A reimage: the partition is gone entirely.
    for item in list(tool.PARTITION.iterdir()):
        if item.is_dir():
            __import__("shutil").rmtree(item)
        else:
            item.unlink()
    assert tool.auth_cookies() == {}

    assert tool.restore(str(archive), False) == 0
    assert set(tool.auth_cookies()) == {"at", "wrt"}
    assert (tool.PARTITION / "Local Storage" / "leveldb.log").read_bytes() == b"state"


def test_saving_a_partition_with_no_sign_in_refuses_rather_than_writing_an_empty_archive(tool, tmp_path):
    archive = tmp_path / "empty.tar.gz"
    with pytest.raises(SystemExit) as raised:
        tool.save(str(archive), False)
    assert "no Pandora sign-in" in str(raised.value)
    assert not archive.exists()


# The one configuration in which the archive is not portable. Chromium's `basic`
# password store keeps the cookie key in the binary; a keyring keeps it in a
# keyring, which is not in the tar.
def test_a_keyring_on_the_box_refuses_the_save(tool, tmp_path, monkeypatch):
    write_cookies(tool.PARTITION)
    monkeypatch.setattr(tool, "keyring_present", lambda: ["gnome-keyring-daemon"])
    with pytest.raises(SystemExit) as raised:
        tool.save(str(tmp_path / "a.tar.gz"), False)
    assert "gnome-keyring-daemon" in str(raised.value)
    # ...and the operator can still override it knowingly.
    assert tool.save(str(tmp_path / "b.tar.gz"), True) == 0


def test_a_live_kiosk_holding_the_profile_refuses_the_restore(tool, tmp_path, monkeypatch):
    write_cookies(tool.PARTITION)
    archive = tmp_path / "a.tar.gz"
    tool.save(str(archive), False)
    monkeypatch.setattr(tool, "profile_locked", lambda: "panel-4242")
    with pytest.raises(SystemExit) as raised:
        tool.restore(str(archive), True)
    assert "holding this profile" in str(raised.value)


# Firstboot runs `restore` unconditionally on EVERY boot. This is the property
# that makes that safe: on the other 66 invocations there is already a sign-in
# and the tool declines rather than replacing a live session with a stale one.
def test_an_existing_sign_in_is_never_silently_replaced(tool, tmp_path):
    write_cookies(tool.PARTITION)
    archive = tmp_path / "a.tar.gz"
    tool.save(str(archive), False)
    with pytest.raises(SystemExit) as raised:
        tool.restore(str(archive), False)
    assert "already has a Pandora sign-in" in str(raised.value)
    assert tool.restore(str(archive), True) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symlinks and os.kill(pid, 0)")
def test_a_stale_lock_from_a_dead_process_does_not_block_a_restore(tool):
    # 2**22 is above the usual pid_max; nothing is running there.
    (tool.PROFILE / "SingletonLock").symlink_to("panel-4194304")
    assert tool.profile_locked() is None


def hostile_archive(path, member):
    """A tar carrying one good member and one `member`, written BY HAND.

    `TarFile.add(arcname=...)` normalises a leading `/` and collapses `..` on the
    way in, so an archive built through it can never carry the escaping path this
    is testing for. The threat is a tar somebody else wrote, so the TarInfo is
    written directly.
    """
    with tarfile.open(path, "w:gz") as bundle:
        for name in ("Cookies", member):
            info = tarfile.TarInfo(name)
            info.size = 1
            bundle.addfile(info, io.BytesIO(b"x"))


@pytest.mark.parametrize("member, expected", [
    ("../../../etc/shadow", "escaping path"),
    ("/etc/shadow", "escaping path"),
    ("Cookies/../../../etc/shadow", "escaping path"),
    ("Preferences", "not part of"),
    ("Local State", "not part of"),
])
def test_an_archive_that_is_not_a_pandora_session_is_refused_whole(tool, tmp_path, member, expected):
    archive = tmp_path / "hostile.tar.gz"
    hostile_archive(archive, member)
    with pytest.raises(SystemExit) as raised:
        tool.restore(str(archive), True)
    assert expected in str(raised.value)
    # Refused WHOLE: the legitimate member must not have landed either.
    assert not (tool.PARTITION / "Cookies").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symlinks")
def test_a_symlink_member_is_refused(tool, tmp_path):
    archive = tmp_path / "link.tar.gz"
    link = tmp_path / "Cookies"
    link.symlink_to("/etc/shadow")
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(link, arcname="Cookies")
    with pytest.raises(SystemExit) as raised:
        tool.restore(str(archive), True)
    assert "link or device node" in str(raised.value)


def test_status_reports_absence_as_a_nonzero_exit(tool, capsys):
    assert tool.status() == 1
    assert "NOT PRESENT" in capsys.readouterr().out
    write_cookies(tool.PARTITION)
    assert tool.status() == 0
    assert "expires" in capsys.readouterr().out


# terra, 2026-09-19, finding 8. With `--replace`, an archive holding only
# `Local Storage` used to restore cleanly, leave this panel's EXISTING Cookies
# in place, and then report success because `auth_cookies()` found them -- one
# box's credentials mixed with another's player state, announced as a restored
# session.
def test_an_archive_carrying_no_cookies_is_refused_before_anything_is_replaced(tool, tmp_path):
    write_cookies(tool.PARTITION)
    (tool.PARTITION / "Local Storage").mkdir()
    (tool.PARTITION / "Local Storage" / "mine.log").write_bytes(b"this panel")

    archive = tmp_path / "storage-only.tar.gz"
    other = tmp_path / "other"
    other.mkdir()
    (other / "leveldb.log").write_bytes(b"another panel")
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(other, arcname="Local Storage")

    with pytest.raises(SystemExit) as raised:
        tool.restore(str(archive), True)
    assert "carries no Cookies" in str(raised.value)
    # Nothing was replaced: this panel's own storage is untouched.
    assert (tool.PARTITION / "Local Storage" / "mine.log").read_bytes() == b"this panel"
    assert set(tool.auth_cookies()) == {"at", "wrt"}
