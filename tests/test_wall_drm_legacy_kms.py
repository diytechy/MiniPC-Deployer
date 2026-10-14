"""SR-017 / LLR-1134: WALL_DRM_LEGACY_KMS decides whether the kiosk exports
WLR_DRM_NO_ATOMIC.

The kiosk and firstboot pieces are EXECUTED with bash (the marked blocks are cut
out of the real scripts), not read as text. Needs bash.

Implements tests: TC-1238 (kiosk export), TC-1239 (firstboot validation and
kiosk.env). Does not prove the compositor honours the variable on the panel's GPU.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WALL = Path(__file__).resolve().parents[1] / "stack/autoinstall/wall"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")


def block(script: str) -> str:
    text = (WALL / script).read_text(encoding="utf-8")
    found = re.search(r"# BEGIN wlr-legacy-kms\n(.*?)# END wlr-legacy-kms\n", text, re.S)
    assert found, f"{script} lost its wlr-legacy-kms block"
    return found.group(1)


def run(script: str, inherit: str = "") -> str:
    """Run on stdin with WLR_DRM_NO_ATOMIC cleared from the parent's environment
    (or set to `inherit`), so only the script under test can put it there."""
    env = {k: v for k, v in os.environ.items() if k != "WLR_DRM_NO_ATOMIC"}
    if inherit:
        env["WLR_DRM_NO_ATOMIC"] = inherit
    done = subprocess.run([BASH, "-s"], input=script.encode("utf-8"),
                          capture_output=True, timeout=60, env=env)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-800:]
    return done.stdout.decode("utf-8")


def kiosk(setup: str, inherit: str = "") -> str:
    """What a CHILD PROCESS (as cage is) sees of WLR_DRM_NO_ATOMIC after the
    kiosk's block: the exact `WLR_DRM_NO_ATOMIC=<v>` line, or UNSET. A child,
    not a shell variable, so a missing `export` fails."""
    out = run(
        "log() { :; }\n" + setup + "\n" + block("wall-kiosk.sh")
        + "\nenv | grep '^WLR_DRM_NO_ATOMIC=' || echo UNSET\n", inherit)
    return out.strip()


def firstboot(setup: str):
    """(knob after the block, whether fail_step was called)."""
    out = run(
        "fail_step() { echo FAILED; }\n" + setup + "\n" + block("wall-firstboot.sh")
        + '\necho "VALUE:$WALL_DRM_LEGACY_KMS"\n').split()
    return out[-1].split(":", 1)[1], "FAILED" in out


@needs_bash
@pytest.mark.parametrize("setup", [
    "export WALL_DRM_LEGACY_KMS=true",
    "unset WALL_DRM_LEGACY_KMS",
    'export WALL_DRM_LEGACY_KMS=""',
    "export WALL_DRM_LEGACY_KMS=maybe",
])
def test_tc1238_true_unset_empty_or_invalid_child_sees_exactly_one(setup):
    # The knob's default is true: only an explicit false means atomic.
    assert kiosk(setup) == "WLR_DRM_NO_ATOMIC=1"


@needs_bash
def test_tc1238_false_child_does_not_see_it_never_zero():
    assert kiosk("export WALL_DRM_LEGACY_KMS=false") == "UNSET"


@needs_bash
def test_tc1238_false_removes_a_value_inherited_from_the_parent():
    assert kiosk("export WALL_DRM_LEGACY_KMS=false", inherit="1") == "UNSET"


def test_tc1238_block_precedes_the_cage_launch_and_is_the_only_code_using_it():
    text = (WALL / "wall-kiosk.sh").read_text(encoding="utf-8")
    start = text.index("# BEGIN wlr-legacy-kms")
    end = text.index("# END wlr-legacy-kms")
    launch = re.search(r"^\s*cage -d -- ", text, re.M)
    assert launch and end < launch.start()
    outside = text[:start] + text[end:]
    code = [ln for ln in outside.splitlines()
            if "WLR_DRM_NO_ATOMIC" in ln and not ln.lstrip().startswith("#")]
    assert code == []


@needs_bash
@pytest.mark.parametrize("setup,value", [
    ("unset WALL_DRM_LEGACY_KMS", "true"),
    ('WALL_DRM_LEGACY_KMS=""', "true"),
    ("WALL_DRM_LEGACY_KMS=true", "true"),
    ("WALL_DRM_LEGACY_KMS=false", "false"),
])
def test_tc1239_firstboot_accepts_true_false_and_defaults_to_true(setup, value):
    assert firstboot(setup) == (value, False)


@needs_bash
@pytest.mark.parametrize("bad", ["TRUE", "yes", "1", "0", "maybe"])
def test_tc1239_firstboot_refuses_anything_else_loudly(bad):
    assert firstboot(f'WALL_DRM_LEGACY_KMS="{bad}"') == ("true", True)


def test_tc1239_firstboot_block_precedes_kiosk_env_being_written():
    text = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    assert text.index("# END wlr-legacy-kms") < text.index("KIOSK_ENV=/etc/wall-panel/kiosk.env")
    assert text.index("# END wlr-legacy-kms") < text.index('echo "WALL_DRM_LEGACY_KMS=$WALL_DRM_LEGACY_KMS"')


def test_tc1239_firstboot_carries_the_knob_into_kiosk_env_and_the_example_declares_it():
    firstboot_text = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    assert 'echo "WALL_DRM_LEGACY_KMS=$WALL_DRM_LEGACY_KMS"' in firstboot_text
    example = (WALL / "wall.env.example").read_text(encoding="utf-8")
    assert re.search(r"^WALL_DRM_LEGACY_KMS=true$", example, re.M)
