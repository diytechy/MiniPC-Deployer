#!/usr/bin/env python3
"""Carry the panel's Pandora sign-in across a reimage.

Usage: wall-pandora-session [status|save <archive>|restore <archive>] [--replace]

WHAT THIS IS FOR, AND WHAT IT IS NOT
------------------------------------
The Owner, 2026-09-19: "Can Pandora's credentials be retained through a
redeployment of the panel? It seems to be lost occasionally, there must be a
specific service that -- when reinstalled -- causes those credentials to get
lost."

MEASURED FIRST. The sign-in lives in exactly one place --
/home/panel/.config/officewall-shell/Partitions/pandora -- and no service in
this lane touches it. Not firstboot, not install-wall-capabilities.sh, not a
paired release: on the real panel the partition survived every one of the ~67
paired releases between the 2026-09-11 reimage and 2026-09-18, with its session
cookies re-issued on the 18th. A redeployment does NOT lose it, and looking for
the service that does would have been looking for something that is not there.

What does lose it is a REIMAGE, which recreates /home/panel from nothing, and
nothing in the lane carried it forward. This script is that carry. (The other
loss mechanism was a moving client fingerprint, and it is fixed on the app side
-- see OfficeWallNaglight electron/pandora-identity.cjs for the User-Agent and
main.cjs for the pinned `--password-store=basic`.)

WHY A PLAIN ARCHIVE IS ENOUGH, AND THE ONE THING THAT WOULD BREAK IT
--------------------------------------------------------------------
Chromium encrypts the cookie store, and where the KEY lives depends on which
password store it picked. Under `basic` -- which the app now names explicitly
rather than inheriting from whatever is installed -- the key is a constant in
the binary, so a cookie file is decryptable on any box running the same app.
That is what makes carrying one across a reimage possible at all.

So `save` REFUSES when it finds a Secret Service on the box: under
gnome-libsecret or kwallet the key would be in a keyring that is not in this
archive, and the restore would produce a profile full of cookies nobody can
read -- a silent failure that looks exactly like the problem it was meant to
fix. Refusing is the honest answer; the archive is not portable in that
configuration and saying so beats writing one that is not.

WHAT IS CARRIED
---------------
The credential-bearing files only. Caches are excluded deliberately: they are
the bulk of the 109 MB partition, they carry nothing that authenticates, and a
cache restored beside a newer app is a liability rather than a saving.

THE SECURITY POSITION, STATED. This archive is a bearer token for the Owner's
Pandora account. It is written 0600, it belongs beside the other panel secrets
in /opt/wall-panel/site (0700), and it is not a thing to leave on a share.
"""

import argparse
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time

PANEL_USER = "panel"
PROFILE = Path("/home/panel/.config/officewall-shell")
PARTITION = PROFILE / "Partitions" / "pandora"

# Relative to the partition. A file is copied when present and skipped when not;
# a directory is copied whole. `Cookies` is the one that actually holds the
# sign-in, and the two storage areas are what the player keeps its own state in.
CARRIED = ("Cookies", "Cookies-journal", "Local Storage", "IndexedDB")

# The cookie that IS the sign-in, and its companion. Pandora issues `at` with
# about a month of life and `wrt` with about a year; the panel refreshes `at`
# from `wrt` while the player runs.
AUTH_COOKIES = ("at", "wrt")

# Binaries whose presence means Chromium may have chosen a keyring backend.
# This is a PROXY and it is a deliberately loose one: the real selection happens
# inside Chromium at startup from the desktop environment, and this script can
# neither see that decision nor replay it. A false alarm costs one `--replace`
# on a restore and a sentence in the log; a missed one costs a silently useless
# archive, so it errs towards refusing.
KEYRING_BINARIES = ("gnome-keyring-daemon", "kwalletd5", "kwalletd6", "kwalletd")

def fail(message):
    sys.exit(f"wall-pandora-session: {message}")


def keyring_present():
    return [name for name in KEYRING_BINARIES if shutil.which(name)]


def panel_ids():
    # IMPORTED HERE AND NOT AT THE TOP. `pwd` is POSIX-only, and the unit tests
    # for this file run on the development Windows box; a module-level import
    # would make the whole tool unimportable there for the sake of two integers
    # that only the restore path needs.
    import pwd
    try:
        record = pwd.getpwnam(PANEL_USER)
    except KeyError:
        fail(f"there is no {PANEL_USER!r} account on this box")
    return record.pw_uid, record.pw_gid


def profile_locked():
    """Whether a live Chromium holds this profile.

    The SingletonLock symlink's target is `<host>-<pid>`. A stale one is common
    -- the kiosk is killed rather than asked to quit -- so the PID is checked
    rather than the link's mere existence: refusing to restore because of a lock
    left behind by a crash three reboots ago is a refusal nobody can act on.
    """
    link = PROFILE / "SingletonLock"
    try:
        target = os.readlink(link)
    except OSError:
        return None
    _, _, pid = target.rpartition("-")
    if not pid.isdigit():
        return target
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return target
    return target


def auth_cookies():
    """The sign-in cookies present in the live partition, with their expiry.

    Read through a COPY. sqlite3 on a database a live Chromium is writing can
    block or read a torn page, and this is a diagnostic -- it must never be the
    thing that wedges the panel.
    """
    source = PARTITION / "Cookies"
    if not source.is_file():
        return {}
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "Cookies"
        try:
            shutil.copy2(source, copy)
            connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        except (OSError, sqlite3.Error):
            return {}
        try:
            rows = connection.execute(
                "SELECT name, expires_utc FROM cookies WHERE host_key LIKE '%pandora.com'"
            ).fetchall()
        except sqlite3.Error:
            return {}
        finally:
            connection.close()
    # Chromium dates are microseconds since 1601-01-01; 11644473600 is the
    # offset to the Unix epoch.
    return {name: (expires / 1_000_000) - 11_644_473_600
            for name, expires in rows if name in AUTH_COOKIES}


def describe(when):
    if when <= 0:
        return "no expiry"
    days = (when - time.time()) / 86400
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(when))
    return f"{stamp} ({days:+.1f} days)"


def status():
    print(f"partition: {PARTITION}{'' if PARTITION.is_dir() else ' (absent)'}")
    held = profile_locked()
    print(f"profile lock: {held if held else 'none'}")
    found = keyring_present()
    print("password store: basic expected; "
          + (f"KEYRING PRESENT ({', '.join(found)}) — an archive would not be portable"
             if found else "no keyring binary found"))
    cookies = auth_cookies()
    if not cookies:
        print("sign-in: NOT PRESENT — the player will ask for the Pandora login")
        return 1
    for name in AUTH_COOKIES:
        if name in cookies:
            print(f"sign-in: {name} expires {describe(cookies[name])}")
    return 0


def save(archive, replace):
    if not PARTITION.is_dir():
        fail(f"nothing to save: {PARTITION} does not exist")
    found = keyring_present()
    if found and not replace:
        fail(f"a keyring is installed ({', '.join(found)}), so the cookie key may not be "
             "the built-in one and this archive would restore cookies nobody can decrypt. "
             "Re-run with --replace only if you know the app runs --password-store=basic.")
    if not auth_cookies():
        fail("there is no Pandora sign-in in this partition to save. Sign in on the "
             "panel first, then run this again.")
    target = Path(archive)
    if target.exists() and not replace:
        fail(f"{target} exists; pass --replace to overwrite it")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Written to a temporary beside the target and moved into place, so an
    # interrupted save can never leave a half archive that a later restore would
    # happily unpack.
    handle, temporary = tempfile.mkstemp(dir=target.parent, suffix=".partial")
    os.close(handle)
    os.chmod(temporary, 0o600)
    carried = []
    try:
        with tarfile.open(temporary, "w:gz") as bundle:
            for name in CARRIED:
                item = PARTITION / name
                if not item.exists():
                    continue
                # `filter` strips ownership: the archive must restore as whoever
                # the new box calls `panel`, not as the uid this one happened to
                # use. The uid is 1000 on both today; relying on that is how it
                # breaks the one time it is not.
                bundle.add(item, arcname=name, filter=_anonymise)
                carried.append(name)
        os.replace(temporary, target)
    except Exception as error:  # noqa: BLE001 — the message is the deliverable
        Path(temporary).unlink(missing_ok=True)
        fail(f"could not write {target}: {error}")
    print(f"saved {', '.join(carried)} to {target} ({target.stat().st_size} bytes, 0600)")
    print("This archive signs in to the Owner's Pandora account. Keep it with the "
          "other panel secrets; stage it at /opt/wall-panel/site/pandora-session.tar.gz "
          "for firstboot to restore after a reimage.")
    return 0


def _anonymise(info):
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    # Directories traversable, files readable by the owner alone. The partition
    # is 0700 on the panel and the restore re-asserts that; carrying the modes
    # verbatim would also carry any that were wrong.
    info.mode = 0o700 if info.isdir() else 0o600
    return info


def _safe_members(bundle, label):
    """The members of `bundle` that may be unpacked, or nothing at all.

    NOT `lstrip("./")`, which is what this said first and is a trap worth
    naming: `str.lstrip` takes a SET OF CHARACTERS, so it turns
    `../../../etc/shadow` into `etc/shadow` and the `..` check below never sees
    the thing it exists to catch. Only a single leading `./` is removed, and the
    path is then examined as a path.
    """
    for info in bundle.getmembers():
        name = info.name[2:] if info.name.startswith("./") else info.name
        if not name or name == ".":
            continue
        if info.issym() or info.islnk() or info.isdev() or info.isfifo():
            fail(f"refusing {label}: it carries a link or device node ({info.name!r})")
        parts = PurePosixPath(name).parts
        if name.startswith("/") or ".." in parts or ":" in parts[0]:
            fail(f"refusing {label}: it carries an escaping path ({info.name!r})")
        if parts[0] not in CARRIED:
            fail(f"refusing {label}: it carries {info.name!r}, which is not part of "
                 "a Pandora session")
        info.name = name
        yield info


def restore(archive, replace):
    source = Path(archive)
    if not source.is_file():
        fail(f"{source} does not exist")
    uid, gid = panel_ids()
    held = profile_locked()
    if held:
        fail(f"the kiosk is holding this profile ({held}). Stop it first "
             "(`systemctl stop getty@tty1`) — a restore under a live Chromium is "
             "overwritten from memory the moment it exits.")
    existing = auth_cookies()
    if existing and not replace:
        fail("this panel already has a Pandora sign-in "
             f"({', '.join(sorted(existing))}); pass --replace to overwrite it")
    PARTITION.mkdir(parents=True, exist_ok=True)
    # Unpacked to a scratch directory FIRST and moved in afterwards, because a
    # tar that fails halfway through must not leave the partition holding one
    # box's Cookies beside another box's Local Storage.
    with tempfile.TemporaryDirectory(dir=PARTITION.parent) as scratch:
        staged = Path(scratch)
        try:
            with tarfile.open(source, "r:gz") as bundle:
                bundle.extractall(staged, members=_safe_members(bundle, str(source)))
        except tarfile.TarError as error:
            fail(f"could not read {source}: {error}")
        # THE ARCHIVE MUST CARRY THE SIGN-IN IT CLAIMS TO BE (terra, 2026-09-19,
        # finding 8). Without this, an archive holding only `Local Storage`
        # restored cleanly with `--replace`, left the panel's EXISTING Cookies
        # in place, and then reported success because `auth_cookies()` found
        # them -- a mix of one box's credentials and another's player state,
        # announced as a restored session.
        if not (staged / "Cookies").is_file():
            fail(f"{source} carries no Cookies; it is not a Pandora session and "
                 "restoring it would mix this panel's sign-in with another's state")
        restored = []
        for name in CARRIED:
            item = staged / name
            if not item.exists():
                continue
            destination = PARTITION / name
            if destination.is_dir():
                shutil.rmtree(destination)
            elif destination.exists():
                destination.unlink()
            os.replace(item, destination)
            restored.append(name)
    if not restored:
        fail(f"{source} carried nothing to restore")
    # Ownership last, over the whole partition: the files just landed as root
    # and Chromium runs as `panel`. A profile it cannot write is a profile that
    # silently stops persisting anything.
    for path in (PROFILE, PROFILE / "Partitions", PARTITION, *PARTITION.rglob("*")):
        try:
            os.chown(path, uid, gid)
        except OSError as error:
            fail(f"could not give {path} to {PANEL_USER}: {error}")
    for path in (PROFILE, PROFILE / "Partitions", PARTITION):
        os.chmod(path, 0o700)
    print(f"restored {', '.join(restored)} into {PARTITION}")
    cookies = auth_cookies()
    if not cookies:
        print("WARNING: the restored partition still shows no Pandora sign-in. The "
              "archive may have come from a box with a different password store.")
        return 1
    for name in AUTH_COOKIES:
        if name in cookies:
            print(f"sign-in: {name} expires {describe(cookies[name])}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="wall-pandora-session", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("status", "save", "restore"), nargs="?", default="status")
    parser.add_argument("archive", nargs="?")
    parser.add_argument("--replace", action="store_true",
                        help="overwrite an existing archive, or an existing sign-in on restore")
    args = parser.parse_args(argv)
    if args.action == "status":
        return status()
    if not args.archive:
        fail(f"{args.action} needs an archive path")
    if os.geteuid() != 0:
        fail(f"{args.action} needs root: the partition is 0700 {PANEL_USER}")
    return save(args.archive, args.replace) if args.action == "save" else restore(args.archive, args.replace)


if __name__ == "__main__":
    raise SystemExit(main())
