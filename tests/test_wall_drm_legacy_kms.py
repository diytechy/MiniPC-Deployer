"""SR-017 / LLR-1134: WALL_DRM_LEGACY_KMS decides whether the kiosk exports
WLR_DRM_NO_ATOMIC.

The kiosk and firstboot pieces are EXECUTED with bash (the marked blocks are cut
out of the real scripts), not read as text. Needs bash.

Implements tests: TC-1238 (kiosk export), TC-1239 (firstboot validation and
kiosk.env). Does not prove the compositor honours the variable on the panel's GPU.
"""
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


def run(script: str) -> str:
    done = subprocess.run([BASH, "-s"], input=script.encode("utf-8"),
                          capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-800:]
    return done.stdout.decode("utf-8")


def kiosk(setup: str) -> str:
    """What the kiosk's block leaves in WLR_DRM_NO_ATOMIC, or the word UNSET."""
    return run(
        "log() { :; }\n" + setup + "\n" + block("wall-kiosk.sh")
        + '\nif [ "${WLR_DRM_NO_ATOMIC+set}" = set ]; then'
        ' echo "SET:$WLR_DRM_NO_ATOMIC"; else echo UNSET; fi\n').strip()


def firstboot(setup: str):
    """(knob after the block, whether fail_step was called)."""
    out = run(
        "fail_step() { echo FAILED; }\n" + setup + "\n" + block("wall-firstboot.sh")
        + '\necho "VALUE:$WALL_DRM_LEGACY_KMS"\n').split()
    return out[-1].split(":", 1)[1], "FAILED" in out


@needs_bash
def test_tc1238_true_exports_the_variable_as_one():
    assert kiosk("export WALL_DRM_LEGACY_KMS=true") == "SET:1"


@needs_bash
@pytest.mark.parametrize("setup", [
    "export WALL_DRM_LEGACY_KMS=false",
    "unset WALL_DRM_LEGACY_KMS",
    'export WALL_DRM_LEGACY_KMS=""',
    "export WALL_DRM_LEGACY_KMS=maybe",
])
def test_tc1238_false_unset_or_invalid_leaves_it_unset_never_zero(setup):
    assert kiosk(setup) == "UNSET"


@needs_bash
def test_tc1238_false_removes_a_value_inherited_from_the_shell():
    assert kiosk("export WLR_DRM_NO_ATOMIC=1 WALL_DRM_LEGACY_KMS=false") == "UNSET"


@needs_bash
@pytest.mark.parametrize("setup,value", [
    ("unset WALL_DRM_LEGACY_KMS", "true"),
    ("WALL_DRM_LEGACY_KMS=true", "true"),
    ("WALL_DRM_LEGACY_KMS=false", "false"),
])
def test_tc1239_firstboot_accepts_true_false_and_defaults_to_true(setup, value):
    assert firstboot(setup) == (value, False)


@needs_bash
@pytest.mark.parametrize("bad", ["TRUE", "yes", "1", "0", "maybe", ""])
def test_tc1239_firstboot_refuses_anything_else_loudly(bad):
    value, failed = firstboot(f'WALL_DRM_LEGACY_KMS="{bad}"')
    # An empty value takes the default; every other non-boolean is refused.
    assert (value, failed) == (("true", False) if bad == "" else ("true", True))


def test_tc1239_firstboot_carries_the_knob_into_kiosk_env_and_the_example_declares_it():
    firstboot_text = (WALL / "wall-firstboot.sh").read_text(encoding="utf-8")
    assert 'echo "WALL_DRM_LEGACY_KMS=$WALL_DRM_LEGACY_KMS"' in firstboot_text
    example = (WALL / "wall.env.example").read_text(encoding="utf-8")
    assert re.search(r"^WALL_DRM_LEGACY_KMS=true$", example, re.M)
