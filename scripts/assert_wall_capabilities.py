#!/usr/bin/env python3
"""SR-017: prove capability files inside a release tar and coherent source stamps.

Reads archives without extraction. A declaration alone cannot satisfy a capability.
"""

import argparse
import json
from pathlib import PurePosixPath
import re
import tarfile

REQUIRED = {
    "tracker-coordination-v1": ["js/tracker-coordinator.js", "js/tracker.js"],
    "touch-feedback-v1": [
        "js/touch-feedback.js",
        "css/touch-feedback.css",
        "vendor/waves/waves.js",
    ],
    "transport-state-v1": ["js/views/music.js"],
    "private-access-v1": [
        "js/access.js",
        "js/views/settings.js",
        "electron/access-broker.cjs",
    ],
    "sensors-v1": ["sensors/service.py", "sensors/identity.py", "sensors/face_core.py"],
    "touch-filter-v1": [
        "touchfilter/core.py",
        "touchfilter/daemon.py",
        "touchfilter/replay.py",
    ],
}


def inspect(path, kind):
    with tarfile.open(path, "r:gz") as archive:
        members = {}
        for member in archive.getmembers():
            name = member.name.rstrip("/")
            if (
                name in members
                or name.startswith("/")
                or ".." in PurePosixPath(name).parts
            ):
                raise ValueError("unsafe or duplicate archive member")
            if not member.isfile() and not member.isdir():
                raise ValueError("archive links/devices are forbidden")
            members[name] = member

        def require(name):
            member = members.get(name)
            if not member or not member.isfile() or member.size == 0:
                raise ValueError("missing capability file: " + name)
            return member

        def document(name):
            member = require(name)
            if member.size > 131072:
                raise ValueError("oversized manifest")
            return json.load(archive.extractfile(member))

        root = {"shell": "app", "site": "site", "gateway": "access"}[kind]
        stamp = document(root + "/build-info.json")
        source = stamp["source"]
        if (
            not re.fullmatch("[0-9a-f]{40}", source["revision"])
            or source["dirty"] is not False
        ):
            raise ValueError("release source must be clean and fully stamped")
        match = re.search(r"-g([0-9a-f]{7,40})(?:-linux-x64)?\.tar\.gz$", str(path))
        if not match or not source["revision"].startswith(match[1]):
            raise ValueError("filename/source stamp mismatch")
        if kind == "shell":
            prefix = "app/runtime/resources/app/"
            manifest = document(prefix + "capabilities.json")
            if manifest.get("schemaVersion") != 1:
                raise ValueError("unknown capability schema")
            for capability, files in REQUIRED.items():
                if capability not in stamp.get("capabilities", []):
                    raise ValueError("build-info missing " + capability)
                declared = manifest.get("capabilities", {}).get(capability, [])
                for file in files:
                    if file not in declared:
                        raise ValueError("manifest missing " + file)
                    require(prefix + file)
        elif kind == "site":
            require("site/index.html")
        else:
            for file in ("gateway/server.mjs", "gateway/state.mjs"):
                require("access/" + file)
        return source["revision"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shell")
    parser.add_argument("--site")
    parser.add_argument("--gateway")
    args = parser.parse_args()
    try:
        revisions = [inspect(path, kind) for kind, path in vars(args).items() if path]
        if not revisions or len(set(revisions)) != 1:
            raise ValueError("payload revision mismatch")
    except (
        ValueError,
        KeyError,
        TypeError,
        tarfile.TarError,
        OSError,
        json.JSONDecodeError,
    ) as error:
        raise SystemExit("Wall artifact rejected: " + str(error))
    print("PASS wall artifact capabilities/layout and source coherence")
