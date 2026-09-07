#!/usr/bin/env python3
"""SR-017: unpack a matching private gateway artifact without enabling access."""

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import tarfile


def install(archive_path, site, destination):
    site_source = json.loads(Path(site).read_text())["source"]
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        names = set()
        for member in members:
            parts = PurePosixPath(member.name).parts
            if (
                member.name in names
                or not parts
                or parts[0] != "access"
                or ".." in parts
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("unsafe gateway member")
            names.add(member.name)
        for name in (
            "access/gateway/server.mjs",
            "access/gateway/state.mjs",
            "access/build-info.json",
        ):
            member = archive.getmember(name)
            if not member.isfile() or not member.size:
                raise ValueError("incomplete gateway")
        if archive.getmember("access/build-info.json").size > 131072:
            raise ValueError("oversized stamp")
        source = json.load(archive.extractfile("access/build-info.json"))["source"]
        if (
            source != site_source
            or source.get("dirty") is not False
            or not re.fullmatch("[0-9a-f]{40}", source.get("revision", ""))
        ):
            raise ValueError("gateway/site source mismatch")
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        # Only validated regular files; never archive ownership, links or modes.
        for member in members:
            relative = PurePosixPath(member.name).parts[1:]
            target = destination.joinpath(*relative)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or any(
                    parent.is_symlink() for parent in target.parents
                ):
                    raise ValueError("symlink in destination")
                with archive.extractfile(member) as src, target.open("wb") as dst:
                    import shutil

                    shutil.copyfileobj(src, dst)
                target.chmod(0o644)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("site_stamp")
    parser.add_argument("destination")
    args = parser.parse_args()
    install(args.archive, args.site_stamp, args.destination)
    print("PASS private gateway installed; access remains explicitly opt-in")
