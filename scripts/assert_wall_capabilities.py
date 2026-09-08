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
    "tracker-coordination-v1": {"app": ["js/main.js", "js/tracker-coordinator.js", "js/tracker.js"], "site": ["js/main.js", "js/tracker-coordinator.js", "js/tracker.js"], "gateway": []},
    "touch-feedback-v1": {"app": ["index.html", "js/main.js", "js/touch-feedback.js", "css/touch-feedback.css", "vendor/waves/waves.js"], "site": ["index.html", "js/main.js", "js/touch-feedback.js", "css/touch-feedback.css", "vendor/waves/waves.js"], "gateway": []},
    "transport-state-v1": {"app": ["index.html", "js/main.js", "js/views/music.js", "css/shell.css"], "site": ["index.html", "js/main.js", "js/views/music.js", "css/shell.css"], "gateway": []},
    "private-access-v1": {"app": ["index.html", "js/main.js", "js/access.js", "js/views/settings.js", "css/access.css", "electron/access-broker.cjs", "electron/main.cjs", "electron/preload.cjs"], "site": ["index.html", "js/main.js", "js/access.js", "js/views/settings.js", "css/access.css"], "gateway": ["gateway/server.mjs", "gateway/state.mjs"]},
    "sensors-v1": {"app": ["index.html", "js/main.js", "js/views/settings.js", "css/access.css", "electron/main.cjs", "electron/preload.cjs", "sensors/service.py", "sensors/identity.py", "sensors/face_core.py"], "site": [], "gateway": []},
    "touch-filter-v1": {"app": ["touchfilter/core.py", "touchfilter/daemon.py", "touchfilter/replay.py"], "site": [], "gateway": []},
    "ambient-drill-v1": {"app": ["index.html", "js/main.js", "js/views/nag.js", "js/nag-summary.js", "css/shell.css"], "site": ["index.html", "js/main.js", "js/views/nag.js", "js/nag-summary.js", "css/shell.css"], "gateway": []},
    "virtual-input-v1": {"app": ["index.html", "js/main.js", "js/pin-keypad.js", "js/text-keyboard.js", "js/views/settings.js", "js/views/pandora.js", "css/access.css", "css/shell.css", "electron/pandora-keyboard-bridge.cjs", "electron/main.cjs", "electron/preload.cjs"], "site": ["index.html", "js/main.js", "js/pin-keypad.js", "js/views/settings.js", "css/access.css"], "gateway": []},
    "tracker-corrections-v2": {"app": ["index.html", "js/main.js", "js/access.js", "js/tracker.js", "js/tracker-coordinator.js", "js/views/nag.js", "css/shell.css"], "site": ["index.html", "js/main.js", "js/access.js", "js/tracker.js", "js/tracker-coordinator.js", "js/views/nag.js", "css/shell.css"], "gateway": ["gateway/server.mjs", "gateway/state.mjs"]},
    "local-visualizer-v1": {"app": ["index.html", "js/main.js", "js/views/settings.js", "js/views/visualizer.js", "js/visualizer-core.js", "js/visualizer-preference.js", "css/access.css", "css/shell.css"], "site": ["index.html", "js/main.js", "js/views/settings.js", "js/views/visualizer.js", "js/visualizer-core.js", "js/visualizer-preference.js", "css/access.css", "css/shell.css"], "gateway": []},
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
        payload = {"shell": "app", "site": "site", "gateway": "gateway"}[kind]
        prefix = {
            "shell": "app/runtime/resources/app/",
            "site": "site/",
            "gateway": "access/",
        }[kind]
        if kind == "site":
            forbidden = ("site/electron/", "site/gateway/", "site/sensors/", "site/touchfilter/")
            leaked = sorted(
                name for name, member in members.items()
                if member.isfile() and name.startswith(forbidden)
            )
            if leaked:
                raise ValueError("privileged file in public site: " + leaked[0])
        manifest = document(prefix + "capabilities.json")
        if manifest != {"schemaVersion": 2, "capabilities": REQUIRED}:
            raise ValueError("capability declaration mismatch")
        if stamp.get("capabilities") != sorted(REQUIRED):
            raise ValueError("build-info capability list mismatch")
        for capability, declaration in REQUIRED.items():
            files = declaration[payload]
            if not isinstance(files, list):
                raise ValueError("malformed payload declaration: " + capability)
            for file in files:
                require(prefix + file)
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
