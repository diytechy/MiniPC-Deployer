"""WI-10. The image's Door configuration agrees with the panel app's.

`tests/fixtures/door-config-v1.json` is BYTE-IDENTICAL to the copy in
OfficeWallNaglight. There it is generated from the broker's own parsers
(`config_from_values`, `motion_config`) by `tools/gen-door-config.py`, and the
app's tests hold the parsers to it. Here the image's four restatements are held
to the same file:

  * `wall.env.example` defaults,
  * the `: "${X:=default}"` defaults in `wall-firstboot.sh`,
  * the firstboot shell range validators, and the defaults they fall back to,
  * the credential file list, in firstboot's `printf` block, its purge list and
    the systemd unit's `LoadCredential=` lines.

The firstboot pieces are EXECUTED with bash, not read as text. Only the
validator functions and the validation block are lifted out of the script (the
rest of firstboot installs packages and starts services), then run on every
boundary value the fixture lists. A grep would pass a validator whose awk had
been changed to `>` from `>=`; running it cannot.

Needs bash and awk (skipped otherwise). The sibling panel repo is found at
`OWN_REPO`, default `../OfficeWallNaglight`; the checks that read it skip if it
is absent. Implements: LLR-1130; TC-1228, TC-1229.

Not covered: firstboot validates only the motion keys. The port, size, FOV, yaw,
pitch and deadline keys have no shell validator, so their bounds are held by the
application alone; `shellValidated` in the fixture says which keys the shell
checks, and this test fails if the flag is wrong in either direction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALL = ROOT / "stack" / "autoinstall" / "wall"
FIRSTBOOT = WALL / "wall-firstboot.sh"
ENV_EXAMPLE = WALL / "wall.env.example"
UNIT = WALL / "wall-door-stream.service"
FIXTURE = ROOT / "tests" / "fixtures" / "door-config-v1.json"
OWN_REPO = Path(os.environ.get("OWN_REPO") or ROOT.parent / "OfficeWallNaglight")
OWN_FIXTURE = OWN_REPO / "tests" / "fixtures" / "door-config-v1.json"

DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
KEYS = DATA["keys"]
BY_ENV = {entry["env"]: entry for entry in KEYS}

needs_own = pytest.mark.skipif(
    not (OWN_REPO / "doorstream" / "service.py").is_file(),
    reason="panel repo not found; set OWN_REPO",
)


def _bash_works() -> bool:
    bash = shutil.which("bash")
    if not bash:
        return False
    try:
        done = subprocess.run([bash, "-c", "command -v awk >/dev/null && echo ok"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.stdout.strip() == "ok"


needs_bash = pytest.mark.skipif(not _bash_works(), reason="needs bash and awk")


def bash(script: str) -> str:
    """Run a script on stdin, so no host path ever reaches the shell."""
    done = subprocess.run([shutil.which("bash"), "-s"], input=script.encode("utf-8"),
                          capture_output=True, timeout=300)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-800:]
    return done.stdout.decode("utf-8")


def from_text(kind: str, text: str):
    """An env-file value, read the way the panel app's parsers read it."""
    if kind == "bool":
        return {"true": True, "false": False}[text]
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    if kind == "polygon":
        return [[float(n) for n in point.split(",")] for point in text.split(";")] if text else []
    if kind == "polygons":
        return [[[float(n) for n in point.split(",")] for point in polygon.split(";")]
                for polygon in text.split("|")] if text else []
    return text


def to_text(entry: dict, value) -> str:
    if entry["kind"] == "bool":
        return "true" if value else "false"
    if entry["kind"] == "polygon":
        return ";".join(",".join(repr(n) for n in point) for point in value)
    if entry["kind"] == "polygons":
        return "|".join(";".join(",".join(repr(n) for n in point) for point in polygon)
                        for polygon in value)
    return str(value) if entry["kind"] in ("int", "string") else repr(value)


FIRSTBOOT_TEXT = FIRSTBOOT.read_text(encoding="utf-8")
FUNCTIONS = ("door_motion_bool_valid", "door_motion_number_between",
             "door_motion_polygon_valid", "door_motion_polygons_valid")


def lift_functions() -> str:
    """The validator definitions and the awk helper they share, verbatim, and
    nothing else from firstboot."""
    helper = re.search(r"^_door_polygon_awk='.*?^\}'$", FIRSTBOOT_TEXT, re.M | re.S)
    assert helper, "firstboot no longer defines _door_polygon_awk"
    out = [helper.group(0)]
    for name in FUNCTIONS:
        found = re.search(r"^%s\(\) \{.*?^\}$" % name, FIRSTBOOT_TEXT, re.M | re.S)
        assert found, "firstboot no longer defines %s" % name
        out.append(found.group(0))
    return "\n".join(out) + "\n"


def lift_validation_block() -> str:
    """From `_door_motion_valid=1` through the fallback block that follows the
    validators, exactly as firstboot runs it."""
    start = FIRSTBOOT_TEXT.index("    _door_motion_valid=1\n")
    marker = FIRSTBOOT_TEXT.index('if [ "$_door_motion_valid" -eq 0 ]; then', start)
    end = FIRSTBOOT_TEXT.index("\n    fi\n", marker) + len("\n    fi\n")
    return FIRSTBOOT_TEXT[start:end]


def lift_default_lines() -> str:
    lines = re.findall(r'^\s*: "\$\{DOORBELL_[A-Z0-9_]+:=[^}]*\}"$', FIRSTBOOT_TEXT, re.M)
    assert lines, "no DOORBELL defaults found in firstboot"
    return "\n".join(lines) + "\n"


def lift_printf_lines() -> str:
    lines = re.findall(r"^\s*printf '%s' \"\$\w+\" > \"\$_door_cred_dir/[\w-]+\"$", FIRSTBOOT_TEXT, re.M)
    assert lines, "no credential printf lines found in firstboot"
    return "\n".join(lines) + "\n"


def env_example_values() -> dict:
    values = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        found = re.match(r"^(DOORBELL_[A-Z0-9_]+)=(.*)$", line)
        if found:
            assert found.group(1) not in values, "duplicate %s" % found.group(1)
            text = found.group(2)
            # As firstboot's load_env_file reads it: one matching outer quote
            # pair is syntax (polygons MUST be quoted for the sourcing helpers).
            if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
                text = text[1:-1]
            values[found.group(1)] = text
    return values


def import_own_service():
    """The panel's real CREDENTIAL_FILES, imported from the sibling."""
    sys.path.insert(0, str(OWN_REPO))
    try:
        from doorstream.service import CREDENTIAL_FILES  # type: ignore
        return dict(CREDENTIAL_FILES)
    finally:
        sys.path.remove(str(OWN_REPO))
        for name in [m for m in sys.modules if m == "doorstream" or m.startswith("doorstream.")]:
            del sys.modules[name]


# --- the fixture itself -----------------------------------------------------

@pytest.mark.skipif(not OWN_FIXTURE.is_file(),
                    reason="panel repo has no door-config-v1.json yet (or OWN_REPO unset)")
def test_tc1228_fixture_is_byte_identical_to_the_panel_repos_copy():
    own = OWN_FIXTURE.read_bytes()
    assert hashlib.sha256(own).hexdigest() == hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), (
        "door-config-v1.json differs from the panel repo's; regenerate there with "
        "tools/gen-door-config.py and copy it here")


def test_tc1228_fixture_is_lf():
    raw = FIXTURE.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")


@needs_own
def test_tc1228_fixture_credential_map_equals_the_apps_credential_files():
    assert {e["credential"]: e["env"] for e in KEYS} == import_own_service()


# --- wall.env.example -------------------------------------------------------

def test_tc1228_env_example_has_exactly_the_fixture_keys_with_the_fixture_defaults():
    values = env_example_values()
    assert set(values) == set(BY_ENV), "key set differs: %s" % sorted(set(values) ^ set(BY_ENV))
    for env, text in values.items():
        entry = BY_ENV[env]
        if entry["required"]:
            assert text.startswith("REPLACE_WITH_"), "%s must stay a placeholder in the tracked file" % env
        else:
            assert from_text(entry["kind"], text) == entry["default"], env


# --- firstboot defaults, executed ------------------------------------------

@needs_bash
def test_tc1228_firstboot_default_assignments_equal_the_fixture_defaults():
    names = [e["env"] for e in KEYS if not e["required"]]
    script = "".join("unset %s\n" % n for n in names) + lift_default_lines() + "".join(
        'printf "%%s=%%s\\n" %s "${%s}"\n' % (n, n) for n in names)
    got = dict(line.split("=", 1) for line in bash(script).splitlines())
    assert set(got) == set(names)
    for env in names:
        entry = BY_ENV[env]
        assert from_text(entry["kind"], got[env]) == entry["default"], env


# --- credential file list, executed ----------------------------------------

@needs_bash
def test_tc1228_firstboot_printf_block_writes_each_credential_from_its_own_key():
    """Run the printf block with every key set to a distinct value: each file
    must hold the value of the key the fixture (and the app) maps it to."""
    exports = "".join("export %s=%s\n" % (e["env"], shlex.quote("v:" + e["env"])) for e in KEYS)
    script = (exports + "_door_motion_effective=v:DOORBELL_MOTION_ENABLED\n"
              '_door_cred_dir="$(mktemp -d)"\n' + lift_printf_lines() +
              'for f in "$_door_cred_dir"/*; do printf "%s=%s\\n" "$(basename "$f")" "$(cat "$f")"; done\n'
              'rm -rf "$_door_cred_dir"\n')
    written = dict(line.split("=", 1) for line in bash(script).splitlines())
    assert written == {e["credential"]: "v:" + e["env"] for e in KEYS}


def test_tc1228_purge_list_and_systemd_unit_name_the_fixture_credentials():
    names = [e["credential"] for e in KEYS]
    listed = re.search(r'^_door_credential_names="([^"]*)"$', FIRSTBOOT_TEXT, re.M)
    assert listed and listed.group(1).split() == names
    loaded = re.findall(r"^LoadCredential=([\w-]+):", UNIT.read_text(encoding="utf-8"), re.M)
    assert loaded == names


@needs_own
def test_tc1228_printf_credential_list_equals_credential_files():
    """Same as above but against the app's real CREDENTIAL_FILES, not the fixture."""
    files = import_own_service()
    printed = dict((m.group(2), m.group(1)) for m in re.finditer(
        r"printf '%s' \"\$(\w+)\" > \"\$_door_cred_dir/([\w-]+)\"", lift_printf_lines()))
    # `motion-enabled` is written from the EFFECTIVE value, not from its own env key.
    printed["motion-enabled"] = "DOORBELL_MOTION_ENABLED"
    assert printed == files


# --- firstboot range validators, executed ----------------------------------

def run_cases(env: str, values: list[str]) -> list[tuple[int, list[str]]]:
    """Run firstboot's real validation block once per value, with all other keys
    at their fixture defaults. Returns (valid flag, failure messages) per value."""
    baseline = "".join("%s=%s\n" % (e["env"], shlex.quote(to_text(e, e["default"])))
                       for e in KEYS if not e["required"])
    script = ["fail_step() { echo \"FAIL $*\"; }", lift_functions(), baseline,
              "check_block() {", lift_validation_block(), "}"]
    for index, value in enumerate(values):
        script.append("( %s=%s; echo \"CASE %d\"; check_block; echo \"VALID=$_door_motion_valid\" )"
                      % (env, shlex.quote(value), index))
    cases, current = [], None
    for line in bash("\n".join(script) + "\n").splitlines():
        if line.startswith("CASE "):
            current = (0, [])
            cases.append(current)
        elif line.startswith("VALID="):
            cases[-1] = (int(line[6:]), cases[-1][1])
        elif line.startswith("FAIL "):
            cases[-1][1].append(line[5:])
    assert len(cases) == len(values)
    return cases


@needs_bash
@pytest.mark.parametrize("env", [e["env"] for e in KEYS], ids=str)
def test_tc1229_shell_validators_accept_and_reject_the_fixtures_boundary_values(env):
    entry = BY_ENV[env]
    accept, reject = entry["accept"], entry["reject"]
    if not entry["shellValidated"]:
        # Each probe forks the whole validation block; a key the shell does not
        # check needs only enough to pin that fact, not every boundary.
        accept, reject = accept[:1], reject[:2]
    results = run_cases(env, accept + reject)
    for value, (valid, messages) in zip(accept, results):
        assert valid == 1, "shell rejected %r which the app accepts: %s" % (value[:40], messages)
    for value, (valid, messages) in zip(reject, results[len(accept):]):
        if entry["shellValidated"]:
            assert valid == 0, "shell accepted %r which the app rejects" % value[:40]
            assert len(messages) == 1, messages
        else:
            # No shell validator exists for this key; the flag in the fixture
            # must say so, and flips the day one is added.
            assert valid == 1, ("shell now rejects %r: set shellValidated true in the "
                                "generator and regenerate" % value[:40])


@needs_bash
def test_tc1229_shell_fallback_defaults_equal_the_fixture_defaults():
    """An invalid motion value makes firstboot reset the motion keys; those
    fallback literals are a third copy of the defaults."""
    names = ["DOORBELL_MOTION_SAMPLE_FPS", "DOORBELL_MOTION_MIN_AREA_RATIO",
             "DOORBELL_MOTION_PERSISTENCE_SECONDS", "DOORBELL_MOTION_DWELL_SECONDS",
             "DOORBELL_MOTION_STATIONARY_RATIO", "DOORBELL_MOTION_APPROACH_POLYGON",
             "DOORBELL_MOTION_ROAD_POLYGON", "DOORBELL_MOTION_IGNORE_POLYGONS",
             "DOORBELL_MOTION_DIAGNOSTICS", "DOORBELL_MOTION_CALIBRATED"]
    baseline = "".join("%s=%s\n" % (e["env"], shlex.quote(to_text(e, e["default"])))
                       for e in KEYS if not e["required"])
    script = ("fail_step() { :; }\n" + lift_functions() + baseline +
              "DOORBELL_MOTION_SAMPLE_FPS=999\nDOORBELL_MOTION_DIAGNOSTICS=true\n"
              "DOORBELL_MOTION_IGNORE_POLYGONS='0,0;0.5,0;0.5,0.5'\nDOORBELL_MOTION_CALIBRATED=true\n" +
              lift_validation_block() +
              "".join('printf "%%s=%%s\\n" %s "${%s}"\n' % (n, n) for n in names) +
              'echo "VALID=$_door_motion_valid"\n')
    got = dict(line.split("=", 1) for line in bash(script).splitlines())
    assert got.pop("VALID") == "0", "the poisoned value did not trip the validator"
    for env in names:
        entry = BY_ENV[env]
        assert from_text(entry["kind"], got[env]) == entry["default"], env
