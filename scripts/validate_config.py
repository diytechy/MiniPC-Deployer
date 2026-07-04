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
  4. Files referenced by autoinstall late-commands under the stack exist.

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
    ):
        check(
            (stack / ref).exists(),
            "autoinstall file present: stack/{}".format(ref),
            "autoinstall file missing: stack/{}".format(ref),
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
