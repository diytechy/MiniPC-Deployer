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
# `${WALL_...}`/`${WIFI_...}`/`${SLEEP_...}`/`${NAVIDROME_...}`/`${PANDORA_...}`
# in a wall script is therefore a KNOB and must be declared in wall.env.example;
# anything else (ENV_FILE, MARKER, loop variables) is a local and is ignored. The
# namespace rule is what keeps this check free of false positives as the scripts
# grow — add a namespace here if a genuinely new family of knobs appears.
WALL_KNOB_NAMESPACES = ("WALL_", "WIFI_", "SLEEP_", "NAVIDROME_", "PANDORA_")


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
    # backup/ and is run in place from the stack dir, like awow-backup.service).
    for ref in (
        "autoinstall/awow-firstboot.service",
        "autoinstall/firstboot.sh",
        "autoinstall/powertune.service",
        "autoinstall/powertune.sh",
        "backup/systemd/backup-standby.service",
        "backup/backup-standby.sh",
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
        # Read by wall-firstboot.sh rather than by user-data, but just as fatal
        # if absent — a panel with no netplan has no network at all (no RJ45).
        "autoinstall/wall/netplan-wifi.yaml.template",
    ):
        check(
            (stack / ref).exists(),
            "autoinstall file present: stack/{}".format(ref),
            "autoinstall file missing: stack/{}".format(ref),
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
