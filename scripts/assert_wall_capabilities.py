#!/usr/bin/env python3
"""SR-017: prove capability files inside a release tar and coherent source stamps.

Reads archives without extraction. A declaration alone cannot satisfy a capability.

THE CONTRACT HAS TWO PARTS, AND THEY AGE DIFFERENTLY:

* REQUIRED is a FLOOR. The panel's own ``capabilities.json`` (carried in every
  payload) is the authority for what a capability contains; this script demands
  only that every capability named below is still declared, with at least the
  files named below, and that EVERY file the manifest declares - floor or not -
  really is in the archive. A panel change that adds a capability, or adds a
  file to one, therefore needs NO edit here; it prints a NOTE (not a failure)
  when the manifest has grown past the floor, so the drift is visible. Removing
  or shrinking a floor capability still fails, which is what the floor is for.
* ALLOWED_SITE_FILES is an EXACT inventory and stays exact on purpose: the hub
  extracts the site archive into a public document root, so an extra file is an
  extra HTTP response surface. The manifest cannot stand in for it (it lists
  capability files, not every served file). Each new file under the site
  payload costs one reviewed line there.

Why a floor and not the exact pin it used to be: the exact pin drifted. By
2026-10-11 OfficeWallNaglight ``CameraOpt`` had 7 capabilities and 27 site files
this script had never heard of, and only the image/ISO path (vmtest/) calls it,
so nothing noticed until a reimage would have been refused.
"""

import argparse
import json
from pathlib import PurePosixPath
import re
import tarfile

# The floor: OfficeWallNaglight feat-stacked-life-row 49cddf0 (2026-10-12, based on
# CameraOpt 5008311). Hand-copied from its capabilities.json; see the module docstring for what may and may not grow.
REQUIRED = {
    "tracker-coordination-v1": {"app": ["js/main.js", "js/tracker-coordinator.js", "js/tracker.js"], "site": ["js/main.js", "js/tracker-coordinator.js", "js/tracker.js"], "gateway": []},
    "touch-feedback-v1": {"app": ["index.html", "js/main.js", "js/touch-feedback.js", "css/touch-feedback.css", "vendor/waves/waves.js"], "site": ["index.html", "js/main.js", "js/touch-feedback.js", "css/touch-feedback.css", "vendor/waves/waves.js"], "gateway": []},
    "transport-state-v1": {"app": ["index.html", "js/main.js", "js/views/music.js", "css/shell.css"], "site": ["index.html", "js/main.js", "js/views/music.js", "css/shell.css"], "gateway": []},
    "private-access-v1": {"app": ["index.html", "js/main.js", "js/access.js", "js/access-messages.js", "js/pin-policy.js", "js/views/settings.js", "css/access.css", "electron/access-broker.cjs", "electron/face-unlock-state.cjs", "electron/panel-access.cjs", "electron/host-attention.cjs", "electron/main.cjs", "electron/preload.cjs"], "site": ["index.html", "js/main.js", "js/access.js", "js/access-messages.js", "js/pin-policy.js", "js/views/settings.js", "css/access.css"], "gateway": ["gateway/server.mjs", "gateway/state.mjs"]},
    "sensors-v1": {"app": ["index.html", "js/main.js", "js/views/settings.js", "css/access.css", "electron/host-attention.cjs", "electron/main.cjs", "electron/preload.cjs", "sensors/service.py", "sensors/identity.py", "sensors/face_core.py", "sensors/detection.py", "sensors/config.py"], "site": [], "gateway": []},
    "touch-filter-v1": {"app": ["touchfilter/core.py", "touchfilter/daemon.py", "touchfilter/replay.py"], "site": [], "gateway": []},
    "touch-adaptive-trial-v1": {"app": ["touchfilter/core.py", "touchfilter/daemon.py", "touchfilter/adaptive.py", "touchfilter/adaptive_runtime.py", "touchfilter/adaptive_replay.py", "touchfilter/bridge.py", "electron/touch-bridge.cjs", "electron/main.cjs"], "site": [], "gateway": []},
    "ambient-drill-v1": {"app": ["index.html", "js/main.js", "js/ambient-priority.js", "js/views/nag.js", "js/nag-summary.js", "css/shell.css"], "site": ["index.html", "js/main.js", "js/ambient-priority.js", "js/views/nag.js", "js/nag-summary.js", "css/shell.css"], "gateway": []},
    "virtual-input-v1": {"app": ["index.html", "js/main.js", "js/pin-keypad.js", "js/text-keyboard.js", "js/views/settings.js", "js/views/pandora.js", "css/access.css", "css/shell.css", "electron/pandora-keyboard-bridge.cjs", "electron/main.cjs", "electron/preload.cjs"], "site": ["index.html", "js/main.js", "js/pin-keypad.js", "js/views/settings.js", "css/access.css"], "gateway": []},
    "tracker-corrections-v2": {"app": ["index.html", "js/main.js", "js/access.js", "js/tracker.js", "js/tracker-coordinator.js", "js/views/nag.js", "css/shell.css"], "site": ["index.html", "js/main.js", "js/access.js", "js/tracker.js", "js/tracker-coordinator.js", "js/views/nag.js", "css/shell.css"], "gateway": ["gateway/server.mjs", "gateway/state.mjs"]},
    "panel-tabs-v1": {"app": ["index.html", "js/main.js", "js/state-machine.js", "js/tab-preference.js", "js/views/nag.js", "js/views/music.js", "js/views/pandora.js", "css/shell.css", "css/access.css"], "site": ["index.html", "js/main.js", "js/state-machine.js", "js/tab-preference.js", "js/views/nag.js", "js/views/music.js", "js/views/pandora.js", "css/shell.css", "css/access.css"], "gateway": []},
    "progress-gauges-v1": {"app": ["index.html", "js/main.js", "js/config.js", "js/views/gauges.js", "css/shell.css", "vendor/brand/OAI_OpenAI-Blossom_White.svg", "vendor/brand/opencode-favicon.svg", "vendor/brand/claude-favicon.svg", "vendor/brand/Grok_Logomark_Light.svg", "vendor/brand/gemini-sparkle.svg", "vendor/brand/freellmapi-favicon.svg", "vendor/icons/clock.svg", "vendor/icons/cash.svg", "vendor/icons/weight.svg", "vendor/icons/sleep.svg", "vendor/icons/heart.svg"], "site": ["index.html", "js/main.js", "js/config.js", "js/views/gauges.js", "css/shell.css", "vendor/brand/OAI_OpenAI-Blossom_White.svg", "vendor/brand/opencode-favicon.svg", "vendor/brand/claude-favicon.svg", "vendor/brand/Grok_Logomark_Light.svg", "vendor/brand/gemini-sparkle.svg", "vendor/brand/freellmapi-favicon.svg", "vendor/icons/clock.svg", "vendor/icons/cash.svg", "vendor/icons/weight.svg", "vendor/icons/sleep.svg", "vendor/icons/heart.svg"], "gateway": []},
    "local-visualizer-v1": {"app": ["index.html", "js/main.js", "js/views/settings.js", "js/views/visualizer.js", "js/visualizer-core.js", "js/visualizer-preference.js", "css/access.css", "css/shell.css"], "site": ["index.html", "js/main.js", "js/views/settings.js", "js/views/visualizer.js", "js/visualizer-core.js", "js/visualizer-preference.js", "css/access.css", "css/shell.css"], "gateway": []},
    "door-stream-v1": {"app": ["index.html", "js/main.js", "js/state-machine.js", "js/views/door.js", "css/shell.css", "electron/main.cjs", "electron/preload.cjs", "electron/door-bridge.cjs", "electron/display-lit.cjs", "doorstream/service.py", "doorstream/motion.py"], "site": [], "gateway": []},
    "panel-audio-v1": {"app": ["index.html", "js/main.js", "js/views/bluetooth.js", "js/views/visualizer.js", "js/visualizer-core.js", "css/shell.css", "css/access.css", "electron/main.cjs", "electron/preload.cjs", "electron/audio-bridge.cjs", "electron/audio-ipc.cjs", "electron/bluetooth-sources.cjs"], "site": ["index.html", "js/main.js", "js/views/bluetooth.js", "js/views/visualizer.js", "js/visualizer-core.js", "css/shell.css", "css/access.css"], "gateway": []},
    "local-capabilities-v1": {"app": ["electron/panel-access.cjs", "electron/sensor-observations.cjs", "electron/access-broker.cjs", "electron/face-unlock-state.cjs", "sensors/detection.py", "electron/local-access.cjs", "electron/main.cjs", "electron/display-lit.cjs", "electron/door-bridge.cjs", "sensors/config.py", "sensors/service.py", "sensors/identity.py", "js/views/settings.js", "js/access-messages.js", "js/state-machine.js"], "site": ["js/views/settings.js", "js/access-messages.js", "js/state-machine.js"], "gateway": []},
    "locked-gauges-v1": {"app": ["js/main.js", "js/gauge-snapshot.mjs", "js/views/gauges.js", "electron/local-gauges.cjs", "electron/access-broker.cjs", "css/access.css", "js/locked-gauges.js", "electron/main.cjs"], "site": ["js/main.js", "js/gauge-snapshot.mjs", "js/views/gauges.js", "css/access.css", "js/locked-gauges.js"], "gateway": ["gateway/server.mjs", "js/gauge-snapshot.mjs"]},
    "tracker-event-log-v1": {"app": ["js/tracker.js", "js/tracker-coordinator.js", "js/views/nag.js", "css/shell.css"], "site": ["js/tracker.js", "js/tracker-coordinator.js", "js/views/nag.js", "css/shell.css"], "gateway": []},
    "mutation-identity-v1": {"app": ["js/event-id.js", "js/tracker.js", "js/tracker-coordinator.js"], "site": ["js/event-id.js", "js/tracker.js", "js/tracker-coordinator.js"], "gateway": ["gateway/server.mjs"]},
    "projectm-trial-v1": {"app": ["index.html", "js/main.js", "js/views/settings.js", "js/views/projectm.js", "js/projectm-lifecycle.js", "js/projectm-rotation.js", "js/projectm-worker.js", "js/visualizer-preference.js", "css/shell.css", "vendor/projectm/projectm.mjs", "vendor/projectm/projectm.wasm", "electron/projectm-presets.cjs", "electron/main.cjs", "electron/preload.cjs"], "site": ["index.html", "js/main.js", "js/views/settings.js", "js/views/projectm.js", "js/projectm-lifecycle.js", "js/projectm-rotation.js", "js/projectm-worker.js", "js/visualizer-preference.js", "css/shell.css", "vendor/projectm/projectm.mjs", "vendor/projectm/projectm.wasm"], "gateway": []},
    "bus-pcm-bridge-v1": {"app": ["js/main.js", "js/pcm-source.js", "js/views/projectm.js", "js/projectm-lifecycle.js", "js/projectm-worker.js", "electron/main.cjs", "electron/preload.cjs", "electron/pcm-bridge.cjs", "electron/pcm-frame.cjs"], "site": ["js/main.js", "js/pcm-source.js", "js/views/projectm.js", "js/projectm-lifecycle.js", "js/projectm-worker.js"], "gateway": []},
    "audio-switch-v1": {"app": ["index.html", "js/main.js", "js/audio-switch.js", "js/views/audio-chrome.js", "css/shell.css", "electron/main.cjs", "electron/preload.cjs", "electron/audio-state.cjs", "electron/audio-bridge.cjs", "electron/audio-ipc.cjs"], "site": ["index.html", "js/main.js", "js/audio-switch.js", "js/views/audio-chrome.js", "css/shell.css"], "gateway": []},
}

# Exact regular-file inventory emitted by OfficeWallNaglight's SITE_SOURCES at
# the paired release head. The hub extracts this archive into a public document
# root, so accepting an extra file would accept an extra HTTP response surface.
# Keep this in lockstep with the clean three-artifact build and its source stamp.
# Adding a file to the panel's site payload = add ONE line here (sorted), nothing
# else; the rejection names every file that is missing from this list.
ALLOWED_SITE_FILES = {
    "VERSION",
    "build-info.json",
    "capabilities.json",
    "config.example.json",
    "css/access.css",
    "css/audio-chrome.css",
    "css/shell.css",
    "css/touch-feedback.css",
    "index.html",
    "js/access-messages.js",
    "js/access.js",
    "js/ambient-priority.js",
    "js/attention-exclusions.js",
    "js/attention-lease.js",
    "js/audio-switch.js",
    "js/checklist-watermark.js",
    "js/config.js",
    "js/event-id.js",
    "js/feed-contact.js",
    "js/gauge-brand.js",
    "js/gauge-format.js",
    "js/gauge-order.js",
    "js/gauge-ring-geometry.js",
    "js/gauge-snapshot.mjs",
    "js/heartbeat.js",
    "js/idle.js",
    "js/input-trace.js",
    "js/library-volume-preference.js",
    "js/locked-gauges.js",
    "js/main.js",
    "js/music/local.js",
    "js/music/md5.js",
    "js/music/provider.js",
    "js/music/subsonic.js",
    "js/nag-summary.js",
    "js/paint-heartbeat.js",
    "js/panel-state.js",
    "js/pcm-source.js",
    "js/pin-keypad.js",
    "js/pin-policy.js",
    "js/presentation-report.js",
    "js/presentation.js",
    "js/projectm-lifecycle.js",
    "js/projectm-rotation.js",
    "js/projectm-worker.js",
    "js/state-machine.js",
    "js/tab-preference.js",
    "js/text-keyboard.js",
    "js/touch-feedback.js",
    "js/tracker-coordinator.js",
    "js/tracker-events.js",
    "js/tracker.js",
    "js/views/audio-chrome.js",
    "js/views/bluetooth.js",
    "js/views/door.js",
    "js/views/frame.js",
    "js/views/gauges.js",
    "js/views/music.js",
    "js/views/nag.js",
    "js/views/pandora.js",
    "js/views/projectm.js",
    "js/views/settings.js",
    "js/views/visualizer.js",
    "js/visualizer-core.js",
    "js/visualizer-preference.js",
    "js/wheel-region.js",
    # xAI's Grok logomark for the AI usage ring (OfficeWallNaglight WSN-076,
    # 2026-10-09): a static, script-free SVG, its provenance in PROVENANCE.md.
    "vendor/brand/Grok_Logomark_Light.svg",
    "vendor/brand/OAI_OpenAI-Blossom_White.svg",
    "vendor/brand/PROVENANCE.md",
    "vendor/brand/claude-favicon.svg",
    "vendor/brand/freellmapi-favicon.svg",
    "vendor/brand/gemini-sparkle.svg",
    "vendor/brand/opencode-favicon.svg",
    "vendor/icons/cash.svg",
    "vendor/icons/clock.svg",
    "vendor/icons/heart.svg",
    "vendor/icons/sleep.svg",
    "vendor/icons/weight.svg",
    "vendor/projectm/PROVENANCE.md",
    "vendor/projectm/projectm.mjs",
    "vendor/projectm/projectm.wasm",
    "vendor/waves/LICENSE",
    "vendor/waves/README.md",
    "vendor/waves/waves.css",
    "vendor/waves/waves.js",
}


PAYLOADS = ("app", "gateway", "site")


def _list(names, limit=12):
    """Every offending name, so one run shows the whole job rather than one file."""
    shown = ", ".join(names[:limit])
    return shown + (" (+%d more)" % (len(names) - limit) if len(names) > limit else "")


def _safe_member(file):
    return (
        isinstance(file, str)
        and file != ""
        and not file.startswith("/")
        and "\\" not in file
        and ".." not in file.split("/")
    )


def check_manifest(manifest, notes=None):
    """Validate a payload's capabilities.json against the floor; return its capabilities.

    Raises ValueError("capability declaration mismatch ...") when the document is
    not schema 2, or a floor capability is absent or has lost a floor file, and
    ValueError("malformed payload declaration: ...") when any capability (floor
    or not) is not three duplicate-free lists of safe relative paths. Anything
    the manifest declares beyond the floor is accepted - it is checked for
    presence in the archive by the caller - and described in ``notes``.
    """
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schemaVersion", "capabilities"}
        or manifest["schemaVersion"] != 2
        or not isinstance(manifest["capabilities"], dict)
    ):
        raise ValueError("capability declaration mismatch: not a schema-2 manifest")
    capabilities = manifest["capabilities"]
    for name, declaration in capabilities.items():
        if not name or not isinstance(declaration, dict) or sorted(declaration) != list(PAYLOADS):
            raise ValueError("malformed payload declaration: " + str(name))
        for payload in PAYLOADS:
            files = declaration[payload]
            if (
                not isinstance(files, list)
                or not all(_safe_member(file) for file in files)
                or len(set(files)) != len(files)
            ):
                raise ValueError("malformed payload declaration: %s/%s" % (name, payload))
    absent = sorted(set(REQUIRED) - set(capabilities))
    if absent:
        raise ValueError("capability declaration mismatch: not declared: " + _list(absent))
    for name, floor in REQUIRED.items():
        for payload in PAYLOADS:
            lost = sorted(set(floor[payload]) - set(capabilities[name][payload]))
            if lost:
                raise ValueError(
                    "capability declaration mismatch: %s/%s no longer declares %s"
                    % (name, payload, _list(lost))
                )
    if notes is not None:
        for name in sorted(set(capabilities) - set(REQUIRED)):
            notes.append("capability beyond the floor, accepted: " + name)
        for name in sorted(set(capabilities) & set(REQUIRED)):
            for payload in PAYLOADS:
                grown = sorted(set(capabilities[name][payload]) - set(REQUIRED[name][payload]))
                if grown:
                    notes.append(
                        "%s/%s grew past the floor, accepted: %s" % (name, payload, _list(grown))
                    )
    return capabilities


def inspect(path, kind, notes=None):
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
            outside_root = sorted(
                name for name, member in members.items()
                if member.isfile() and not name.startswith("site/")
            )
            if outside_root:
                raise ValueError("unexpected file outside public site root: " + outside_root[0])
            forbidden = ("site/electron/", "site/gateway/", "site/sensors/", "site/touchfilter/")
            leaked = sorted(
                name for name, member in members.items()
                if member.isfile() and name.startswith(forbidden)
            )
            if leaked:
                raise ValueError("privileged file in public site: " + leaked[0])
        capabilities = check_manifest(document(prefix + "capabilities.json"), notes)
        if stamp.get("capabilities") != sorted(capabilities):
            raise ValueError("build-info capability list mismatch")
        # The MANIFEST's declaration is what must be present, not just the
        # floor: a capability the panel added is held to its own claim.
        for declaration in capabilities.values():
            for file in declaration[payload]:
                require(prefix + file)
        if kind == "site":
            actual_site_files = {
                name[len("site/"):]
                for name, member in members.items()
                if member.isfile() and name.startswith("site/")
            }
            unexpected = sorted(actual_site_files - ALLOWED_SITE_FILES)
            missing_inventory = sorted(ALLOWED_SITE_FILES - actual_site_files)
            if unexpected:
                raise ValueError("unexpected file in public site: " + _list(unexpected))
            if missing_inventory:
                raise ValueError("missing public site inventory file: " + _list(missing_inventory))
        return source["revision"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shell")
    parser.add_argument("--site")
    parser.add_argument("--gateway")
    args = parser.parse_args()
    notes = []
    try:
        revisions = [inspect(path, kind, notes) for kind, path in vars(args).items() if path]
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
    for note in dict.fromkeys(notes):
        print("NOTE " + note)
    print("PASS wall artifact capabilities/layout and source coherence")
