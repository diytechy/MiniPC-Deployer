#!/usr/bin/env python3
"""Config-level validation for the deploy stack (product-layer check).

No Docker on the dev box (verified), so runtime bring-up can't be exercised here.
This is the honest substitute the WI-4.7 note describes: static coverage checks
that catch the mistakes a `.env`/compose/Caddy edit most often introduces.

Checks (each prints PASS/FAIL; nonzero exit if any FAIL):
  1. Every `${VAR}` referenced in docker-compose.yml has a key in .env.example.
  2. Every `{$VAR}` referenced in caddy/Caddyfile is passed by the caddy
     service's `environment:` block in compose (Caddy only sees what compose
     hands it).
  3. Every host path in a compose bind-mount (`./x:...`) exists in the repo
     (an .example stand-in counts, since the real file is gitignored/seeded).
  4. Files referenced by autoinstall late-commands under the stack exist — for
     BOTH image targets (the AWOW core and the wall panel variant).
  5. Every knob the wall panel's scripts read has a key in wall.env.example.
     The wall variant is configured by a shell-sourced env file rather than by
     compose, so check 1 cannot see it: without this, adding a `$WALL_FOO` read
     to a script would ship a silently-unset knob.

Stdlib only; regex-based (no PyYAML on the dev box). It does not claim to parse
YAML fully — it validates variable/file *coverage*, which is what a config repo
without a runtime can honestly assert. Runtime validation stays PENDING a Docker
host (docs/status.md).

Usage: python scripts/validate_config.py [--stack stack]
"""

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load(p):
    return p.read_text(encoding="utf-8") if p.exists() else ""


def env_keys(env_example_text):
    """KEY names defined in a .env file (KEY=value lines, ignoring comments)."""
    keys = set()
    for line in env_example_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m:
            keys.add(m.group(1))
    return keys


def compose_var_refs(compose_text):
    """Every ${VAR} referenced anywhere in the compose file."""
    return set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", compose_text))


def caddy_var_refs(caddy_text):
    """Every {$VAR} referenced in the Caddyfile (skip commented lines)."""
    refs = set()
    for line in caddy_text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        refs.update(re.findall(r"\{\$([A-Za-z_][A-Za-z0-9_]*)\}", line))
    return refs


def caddy_env_passed(compose_text):
    """The env keys the caddy service passes through (its environment: block).

    We approximate the caddy block as the lines from `  caddy:` to the next
    top-level service key, then read `NAME: ${...}` / `NAME: value` entries."""
    lines = compose_text.splitlines()
    passed = set()
    in_caddy = False
    in_env = False
    for line in lines:
        if re.match(r"^  caddy:\s*$", line):
            in_caddy = True
            continue
        if in_caddy and re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            break  # next service
        if in_caddy and re.match(r"^    environment:\s*$", line):
            in_env = True
            continue
        if in_caddy and in_env:
            if re.match(r"^    [A-Za-z]", line) and not re.match(r"^      ", line):
                in_env = False  # left the environment: block
                continue
            m = re.match(r"^\s+([A-Za-z_][A-Za-z0-9_]*):\s", line)
            if m:
                passed.add(m.group(1))
    return passed


# The wall panel variant is configured by a shell-sourced env file, not by
# compose, so its knobs live in these NAMESPACES by convention. A reference to
# `${WALL_...}`/`${WIFI_...}`/`${SLEEP_...}`/`${MEDIA_...}`/`${NAVIDROME_...}`/
# `${PANDORA_...}` in a wall script is therefore a KNOB and must be declared in
# wall.env.example; anything else (ENV_FILE, MARKER, loop variables) is a local
# and is ignored. The namespace rule is what keeps this check free of false
# positives as the scripts grow — add a namespace here if a genuinely new family
# of knobs appears. `MEDIA_` was added by OI-15 (the panel's media pull) and
# OI-18 doubled its membership: MEDIA_{MUSIC,FRAME}_SHARE_UNC,
# MEDIA_{MUSIC,FRAME}_CIFS_CREDENTIALS and the shared MEDIA_CIFS_VERS.
# The OI-18 review (2026-08-04) then SHRANK it: the free-text MEDIA_CIFS_EXTRA
# became the enum MEDIA_CIFS_VERS, and MEDIA_{MUSIC,FRAME}_SOURCE_OVERRIDE
# stopped being configuration at all (it is `--bench-source FLOW=DIR` now). Both
# retired names are still NAMED in wall-sync.sh — as literals in its refusal
# list, without a `$`, so they are correctly not counted as reads here.
WALL_KNOB_NAMESPACES = (
    "WALL_",
    "WIFI_",
    "SLEEP_",
    "MEDIA_",
    "NAVIDROME_",
    "PANDORA_",
)


def is_wall_knob(name):
    return name.startswith(WALL_KNOB_NAMESPACES)


def wall_env_keys(text):
    """Knob names declared in wall.env.example, INCLUDING commented-out ones.

    A commented `# NAVIDROME_USER=…` is a deliberate declaration of an optional
    knob (un-commenting it makes the household secret tooling demand its store
    key), so it counts as declared — the check is "is this knob documented",
    not "is this knob active"."""
    keys = set()
    for line in text.splitlines():
        m = re.match(r"#?\s*([A-Za-z_][A-Za-z0-9_]*)=", line.strip())
        if m:
            keys.add(m.group(1))
    return keys


def wall_knob_refs(paths):
    """Every namespaced knob a wall script/template/user-data actually reads.

    Covers the three substitution mechanisms the wall variant uses: shell
    `${KNOB}` / `$KNOB` reads, `@@KNOB@@` template placeholders (netplan), and
    `REPLACE_WITH_KNOB` tokens in the autoinstall user-data (which the household
    materialiser substitutes)."""
    refs = set()
    for p in paths:
        text = load(p)
        if not text:
            continue
        found = re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", text)
        found += re.findall(r"\$([A-Z][A-Z0-9_]*)", text)
        found += re.findall(r"@@([A-Za-z_][A-Za-z0-9_]*)@@", text)
        found += re.findall(r"\bREPLACE_WITH_([A-Z][A-Z0-9_]*)", text)
        refs.update(n for n in found if is_wall_knob(n))
    return refs


def image_packages(packages_list_text):
    """Package names from a stack/autoinstall[/wall]/packages.list.

    THE LIST MOVED OUT OF THE user-data ON 2026-08-06. `packages:` runs during
    curtin's install step, before any late-command, so no local apt source can
    serve it — and the whole point of the offline work is that the install
    fetches nothing. Both images now ship `packages: []` and install from a repo
    of frozen .debs on the payload, driven by this file.

    Three rules, the same three vmtest/lib/common.sh read_packages_list, the
    user-data late-command and vmtest/assert-installed.sh implement: strip from
    `#` to end of line, strip all whitespace (a package name contains none),
    drop what is left if empty. Deliberately parseable in awk, sed, bash and
    Python, which is why it is not YAML.
    """
    names = set()
    for line in packages_list_text.splitlines():
        name = re.sub(r"#.*", "", line).strip()
        if name:
            names.add(name)
    return names


def bind_mount_paths(compose_text, stack):
    """Host paths from `./x:...` bind mounts, resolved under the stack dir."""
    paths = []
    for m in re.findall(r"-\s+\./([^:\s]+):", compose_text):
        paths.append(stack / m)
    return paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stack", default="stack")
    args = ap.parse_args()
    stack = REPO / args.stack

    compose = load(stack / "docker-compose.yml")
    env_example = load(stack / ".env.example")
    caddy = load(stack / "caddy" / "Caddyfile")

    fails = 0

    def check(cond, ok_msg, bad_msg):
        nonlocal fails
        if cond:
            print("PASS", ok_msg)
        else:
            print("FAIL", bad_msg)
            fails += 1

    if not compose:
        print("FAIL docker-compose.yml not found under", stack)
        sys.exit(1)

    # 1. compose ${VAR} coverage
    keys = env_keys(env_example)
    missing = sorted(compose_var_refs(compose) - keys)
    check(
        not missing,
        "every ${{VAR}} in compose has an .env.example key ({} vars)".format(
            len(compose_var_refs(compose))
        ),
        "compose vars missing from .env.example: " + ", ".join(missing),
    )

    # 2. Caddy {$VAR} passed by caddy service env
    passed = caddy_env_passed(compose)
    caddy_missing = sorted(caddy_var_refs(caddy) - passed)
    check(
        not caddy_missing,
        "every Caddyfile {{$VAR}} is passed by the caddy service ({} vars)".format(
            len(caddy_var_refs(caddy))
        ),
        "Caddyfile vars not passed by caddy service env: " + ", ".join(caddy_missing),
    )

    # 3. bind-mount host paths exist (an .example stand-in counts)
    for p in bind_mount_paths(compose, stack):
        exists = p.exists() or Path(str(p) + ".example").exists()
        check(
            exists,
            "bind-mount source present: {}".format(p.relative_to(REPO)),
            "bind-mount source missing (no file or .example): {}".format(
                p.relative_to(REPO)
            ),
        )

    # 4. autoinstall referenced files exist (files the late-commands cp/enable).
    # Kept in step with user-data: firstboot, the per-boot powertune unit, and the
    # WI-10.10 backup-drive standby unit + its script (which lives under stack/
    # backup/ and is run in place from the stack dir, like homehub-backup.service).
    for ref in (
        "autoinstall/homehub-firstboot.service",
        "autoinstall/firstboot.sh",
        "autoinstall/powertune.service",
        "autoinstall/powertune.sh",
        "backup/systemd/backup-standby.service",
        "backup/backup-standby.sh",
        # The backup service proper. Added 2026-08-08, when verify-hub.sh found
        # `homehub-backup.timer` NOT-FOUND on a healthy hub: these two shipped in
        # every payload ever built and no late-command ever copied them, so the
        # nightly backup had never fired on any machine. Check 4b below is the
        # generalisation that would have caught it.
        "backup/systemd/homehub-backup.service",
        "backup/systemd/homehub-backup.timer",
        "backup/backup.sh",
        "backup/restore.sh",
        # The WALL panel variant (SR-016/SR-017): a second image target with its
        # own late-commands, so it needs its own coverage. Every file below is
        # cp'd or enabled by autoinstall/wall/user-data.
        "autoinstall/wall/meta-data",
        "autoinstall/wall/wall.env.example",
        "autoinstall/wall/wall-firstboot.service",
        "autoinstall/wall/wall-firstboot.sh",
        "autoinstall/wall/wall-wakeprep.service",
        "autoinstall/wall/wall-wakeprep.sh",
        "autoinstall/wall/wall-sleep.service",
        "autoinstall/wall/wall-wake.service",
        "autoinstall/wall/wall-sleep.sh",
        "autoinstall/wall/wall-kiosk.sh",
        # OI-15 — the media pull. The unit + the script + the manifest generator
        # the script invokes as its post-step (a synced cache with no manifest is
        # a panel that shows no music at all, so the generator is as load-bearing
        # as the sync itself).
        "autoinstall/wall/wall-sync.service",
        "autoinstall/wall/wall-sync.sh",
        "autoinstall/wall/wall-media-manifest.py",
        # OI-16a — the resume hook (WantedBy=suspend.target): without it a panel
        # on SLEEP_MODE=suspend syncs only at boot, which in practice is ~never.
        "autoinstall/wall/wall-sync-resume.service",
        # OI-18 — the frame flow's own cadence. storage-map §4d puts the
        # frame-video share on a ONE-MINUTE accessibility-checked timer while the
        # music pull stays boot/resume/on-demand, so the two flows cannot share a
        # unit. Without the .timer the frame videos refresh only at boot, and
        # nothing on the panel would say so — the flow is designed to be quiet
        # when its source is asleep, which is exactly what "never runs" looks
        # like from the journal.
        "autoinstall/wall/wall-sync-frame.service",
        "autoinstall/wall/wall-sync-frame.timer",
        # Read by wall-firstboot.sh rather than by user-data, but just as fatal
        # if absent — a panel with no netplan has no network at all (no RJ45).
        "autoinstall/wall/netplan-wifi.yaml.template",
        # IF-005 — the soname -> package map the wall builder asserts against
        # (vmtest/lib/common.sh assert_electron_runtime_deps). Without it the
        # builder cannot tell whether the image installs what the shell's
        # Electron runtime loads, and a missing library is a black wall.
        "autoinstall/wall/electron-runtime-deps.tsv",
    ):
        check(
            (stack / ref).exists(),
            "autoinstall file present: stack/{}".format(ref),
            "autoinstall file missing: stack/{}".format(ref),
        )

    # 4b. EVERY SHIPPED SYSTEMD UNIT IS ACTUALLY INSTALLED BY SOMETHING.
    #
    # Check 4 asks "does the file this list names exist?", which is the wrong
    # direction: it can only catch a reference to a missing file, never a file
    # that nothing references. On 2026-08-08 that was the gap — stack/backup/
    # systemd/ held three units, user-data copied one, and homehub-backup.service
    # and .timer rode along in every payload without being installed anywhere
    # systemd would look. `systemctl is-enabled homehub-backup.timer` answered
    # NOT-FOUND on a hub that had been "working" for months. Nothing in this
    # repo could have noticed, because a unit file that exists and is never
    # referenced is indistinguishable from one that is deliberately optional.
    #
    # So: enumerate the units on disk and require each to be named by a
    # late-command or by firstboot. A deliberately-unused unit is fine; it just
    # has to be listed here, which makes leaving it out a decision.
    #
    # MATCHED ON INSTALL LINES ONLY, not on the whole file, and the first draft of
    # this check got that wrong. Both missing units were named in the pre-fix
    # tree — but homehub-backup.service was named in a COMMENT ("runs from the
    # stack dir like homehub-backup.service"), so a plain substring search over
    # the file counted the prose as evidence and reported only the .timer. A
    # check that a comment can satisfy is a check that rewards writing about the
    # work instead of doing it. An installation is a line that puts the unit
    # under /etc/systemd/system or hands its name to `systemctl enable`.
    unit_install_lines = [
        line
        for p in (
            stack / "autoinstall" / "user-data",
            stack / "autoinstall" / "firstboot.sh",
            stack / "autoinstall" / "wall" / "user-data",
            stack / "autoinstall" / "wall" / "wall-firstboot.sh",
        )
        if p.exists()
        for line in load(p).splitlines()
        if "/etc/systemd/system" in line or "systemctl enable" in line
    ]
    unit_installers = "\n".join(unit_install_lines)
    # Units that exist on purpose without being installed by an image. Empty
    # today; an entry here is a claim someone has to defend.
    unit_exemptions = set()
    orphan_units = []
    for unit_dir in (stack / "backup" / "systemd", stack / "samba", stack / "autoinstall"):
        if not unit_dir.is_dir():
            continue
        for unit in sorted(unit_dir.glob("*.service")) + sorted(unit_dir.glob("*.timer")):
            if unit.name in unit_exemptions:
                continue
            if unit.name not in unit_installers:
                orphan_units.append(str(unit.relative_to(stack)))
    check(
        not orphan_units,
        "every shipped systemd unit is referenced by an installer",
        "unit file(s) shipped but installed by NOTHING - they reach the box and "
        "systemd never sees them: " + ", ".join(orphan_units),
    )

    # 5. wall-panel knob coverage (the .env-example equivalent for image 2).
    wall_dir = stack / "autoinstall" / "wall"
    wall_env = wall_dir / "wall.env.example"
    if wall_env.exists():
        declared = wall_env_keys(load(wall_env))
        consumers = [
            wall_dir / "wall-firstboot.sh",
            wall_dir / "wall-sleep.sh",
            wall_dir / "wall-wakeprep.sh",
            wall_dir / "wall-kiosk.sh",
            wall_dir / "wall-sync.sh",
            wall_dir / "netplan-wifi.yaml.template",
            wall_dir / "user-data",
        ]
        used = wall_knob_refs(consumers)
        undeclared = sorted(used - declared)
        check(
            not undeclared,
            "every wall-panel knob is declared in wall.env.example ({} knobs)".format(
                len(used)
            ),
            "wall knobs read but NOT declared in wall.env.example: "
            + ", ".join(undeclared),
        )

    # 6. Electron runtime deps: every soname the shell artifact loads maps to a
    #    package this image installs (IF-005 / PKG-1).
    #
    #    The wall builder makes the STRONGER version of this check — it reads
    #    DT_NEEDED out of the binary about to be baked, so a new dependency
    #    fails the build. That check needs the artifact, which a public checkout
    #    does not have. This one needs only tracked files, so it runs on every
    #    commit and catches the other direction: a row added to the table
    #    without the matching package reaching the image. A missing shared
    #    library is a black wall with no message on it.
    deps_tsv = wall_dir / "electron-runtime-deps.tsv"
    wall_packages = wall_dir / "packages.list"
    if deps_tsv.exists() and wall_packages.exists():
        needed, soname_count = {}, 0
        for line in load(deps_tsv).splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) < 3 or cols[0] == "soname":
                continue
            if cols[2].strip() == "image-packages":
                soname_count += 1
                # Several sonames share one package (libnss3 provides three).
                needed.setdefault(cols[1].strip(), cols[0].strip())
        installed = image_packages(load(wall_packages))
        absent = sorted(p for p in needed if p not in installed)
        check(
            not absent,
            "every Electron runtime package is in the wall image ({} sonames -> {} packages)".format(
                soname_count, len(needed)
            ),
            "electron-runtime-deps.tsv names package(s) missing from "
            "stack/autoinstall/wall/packages.list: "
            + ", ".join("{} (for {})".format(p, needed[p]) for p in absent),
        )

    # 6b. `packages:` must STAY EMPTY on both images (2026-08-06).
    #
    #     The one-line summary of the whole offline change, asserted rather than
    #     documented: that key runs during curtin's install step, before any
    #     late-command, so no local apt source can serve it. A single name put
    #     back reaches for the Ubuntu archive, and reaching for the archive at
    #     all is the failure that produced a bare Ubuntu on the real hub. It is
    #     also an entry NO other check covers — export-apt.sh, the install-time
    #     apt run, assert-installed.sh and stage_apt_into_payload all read
    #     packages.list and none of them would ever see it.
    for rel in ("autoinstall/user-data", "autoinstall/wall/user-data"):
        text = load(stack / rel)
        if not text:
            continue
        # Textual on purpose: the same stdlib-only constraint as everything above
        # this line, and the shape being refused is a LIST under `packages:`.
        entries = []
        in_pkgs = False
        for line in text.splitlines():
            if re.match(r"^  packages:\s*$", line):
                in_pkgs = True
                continue
            if in_pkgs:
                if re.match(r"^  \S", line):
                    break
                m = re.match(r"^\s*-\s+([^\s#]+)", line)
                if m:
                    entries.append(m.group(1))
        check(
            not entries,
            "{}: packages: is empty — the install fetches nothing".format(rel),
            "{}: packages: lists {} entry(s) ({}). That key runs BEFORE any "
            "late-command, so the baked offline repo cannot serve it and the "
            "install would reach for the Ubuntu archive — the 2026-08-06 "
            "failure. Move them to the sibling packages.list.".format(
                rel, len(entries), ", ".join(entries[:5])
            ),
        )

    # 5. YAML parse (optional — needs PyYAML; SKIP cleanly if absent so this stays
    # runnable on a stdlib-only box). cloud-init user-data is valid YAML under its
    # `#cloud-config` first line.
    try:
        import yaml  # noqa: PLC0415

        for rel in (
            "docker-compose.yml",
            "autoinstall/meta-data",
            "autoinstall/user-data",
            # Image target 2 — the wall panel. Same #cloud-config YAML contract.
            "autoinstall/wall/meta-data",
            "autoinstall/wall/user-data",
        ):
            f = stack / rel
            if f.exists():
                try:
                    yaml.safe_load(load(f))
                    check(True, "YAML parses: stack/{}".format(rel), "")
                except yaml.YAMLError as e:
                    check(False, "", "YAML parse error in stack/{}: {}".format(rel, e))
    except ImportError:
        print("SKIP YAML parse (PyYAML not installed on this interpreter)")

    print("----")
    if fails:
        print("VALIDATION FAILED ({} check(s))".format(fails))
        sys.exit(1)
    print("ALL CONFIG CHECKS PASSED")


if __name__ == "__main__":
    main()
