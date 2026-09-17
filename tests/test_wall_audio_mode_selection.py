"""`set_mode_selection` / `require_mode_selection`, EXECUTED rather than read.

WHY THIS FILE EXISTS. The absent-adapter deferral (SR-039) was reviewed by
codex gpt-5.6-terra on 2026-09-17, and its fourth finding was fair: every test
covering it in test_wall_audio_switch.py is a source-string check. Those tests
passed against a version whose callers ignored `set_mode_selection`'s status
entirely, so a failed write logged "selection recorded" and exited zero. Text
placement is not behaviour, and the defect lived in the behaviour.

So this suite runs the two functions for real, with `sh`, against a temp
directory, and forces the failure modes by making the paths unwritable or the
commands fail. It does NOT run the whole script: the mode file, the symlink and
the conf dir are hardcoded absolute paths under /etc, and the script's tail
switches live audio. The functions are extracted from the source and evaluated,
so the thing under test is the shipped text, and the extraction fails loudly if
the markers move rather than silently testing nothing.

Skipped where there is no POSIX `sh` (a plain Windows dev box); CI is Linux.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "stack" / "autoinstall" / "wall" / "wall-audio-mode"

pytestmark = pytest.mark.smoke

SH = shutil.which("sh") or shutil.which("bash")


def _symlinks_work() -> bool:
    """Probe the capability rather than the platform name.

    Git Bash on Windows HAS `sh` and `ln`, and its `ln -s` fails with a
    misleading "No such file or directory" because the OS refuses the symlink,
    not because the path is wrong. These functions are entirely about a symlink
    and a file agreeing, so without real symlinks there is nothing to test. A
    skip here is honest; the panel and CI are Linux, and `wsl -d Ubuntu` runs
    them on this dev box (all 9 pass there, 2026-09-17).
    """
    import tempfile

    if SH is None:
        return False
    # Probe with THE SAME `sh` and `ln` the tests use, not with Python's
    # os.symlink: on a Windows box with Developer Mode on, Python can create a
    # symlink while Git Bash's `ln -s` still fails. Probing the wrong tool gave
    # a suite that skipped nowhere and failed everywhere.
    with tempfile.TemporaryDirectory() as box:
        probe = Path(box).as_posix()
        done = subprocess.run(
            [SH, "-c", 'ln -sfn "%s/target" "%s/link"' % (probe, probe)],
            capture_output=True, text=True)
        return done.returncode == 0


if SH is None:  # pragma: no cover - platform dependent
    pytest.skip("no POSIX sh on this platform", allow_module_level=True)
if not _symlinks_work():  # pragma: no cover - platform dependent
    pytest.skip("this platform cannot create symlinks; run under WSL or CI",
                allow_module_level=True)


def _extract(name: str) -> str:
    """Pull one shell function out of the shipped script, by its header."""
    text = SOURCE.read_text(encoding="utf-8")
    start = text.index("\n%s() {\n" % name) + 1
    end = text.index("\n}\n", start) + 3
    body = text[start:end]
    assert body.startswith("%s() {" % name), body[:80]
    return body


def _harness(tmp_path: Path, extra: str = "") -> str:
    """The two functions, with the script's real constants pointed at tmp."""
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    return "\n".join([
        "set -eu",
        'CONF_DIR="%s"' % conf.as_posix(),
        'MODE_FILE="%s"' % (tmp_path / "audio-mode").as_posix(),
        'LINK="%s"' % (conf / "audio-out.conf").as_posix(),
        _extract("set_mode_selection"),
        _extract("require_mode_selection"),
        extra,
    ])


def _run(tmp_path: Path, extra: str):
    return subprocess.run([SH, "-c", _harness(tmp_path, extra)],
                          capture_output=True, text=True)


def _state(tmp_path: Path):
    mode_file = tmp_path / "audio-mode"
    link = tmp_path / "conf" / "audio-out.conf"
    mode = mode_file.read_text(encoding="utf-8").strip() if mode_file.exists() else None
    target = Path(link.readlink()).name if link.is_symlink() else None
    return mode, target


def test_a_clean_selection_agrees_across_both_files_sr039(tmp_path):
    """The mode file and the chain symlink name the same mode."""
    done = _run(tmp_path, "require_mode_selection bus")
    assert done.returncode == 0, done.stderr
    assert _state(tmp_path) == ("bus", "asound-bus-mode.conf")


@pytest.mark.parametrize("mode,conf", [
    ("trigger", "asound-trigger-mode.conf"),
    ("panel", "asound-panel-mode.conf"),
    ("bus", "asound-bus-mode.conf"),
])
def test_every_mode_selects_its_own_chain_sr039(tmp_path, mode, conf):
    assert _run(tmp_path, "require_mode_selection %s" % mode).returncode == 0
    assert _state(tmp_path) == (mode, conf)


def test_an_unknown_mode_is_refused_without_touching_anything_sr039(tmp_path):
    """An unrecognised mode has its own code (2) and never reaches the disk."""
    done = _run(tmp_path, "require_mode_selection wireless || echo RC=$?")
    assert "RC=2" in done.stdout, done.stdout + done.stderr
    assert _state(tmp_path) == (None, None)


def test_a_failed_mode_file_write_is_not_reported_as_recorded_sr039(tmp_path):
    """THE TERRA FINDING. A failed publish used to exit zero and say 'recorded'.

    `mv` is made to fail. The old code ignored the status, so firstboot went
    green with no selection on disk; the re-add would then reassert the mode
    the Owner had just replaced.
    """
    done = _run(tmp_path, "\n".join([
        "mv() { return 1; }",
        "require_mode_selection bus || echo RC=$?",
    ]))
    assert "RC=1" in done.stdout, done.stdout + done.stderr
    assert "FATAL" in done.stderr
    assert "could not record the requested mode 'bus'" in done.stderr
    # And, crucially, the link did not keep the new value on its own.
    mode, target = _state(tmp_path)
    assert mode is None and target is None, (mode, target)


def test_a_failed_publish_rolls_the_link_back_to_the_previous_mode_sr039(tmp_path):
    """A selection that cannot be published leaves the OLD mode intact."""
    assert _run(tmp_path, "require_mode_selection trigger").returncode == 0
    done = _run(tmp_path, "\n".join([
        "mv() { return 1; }",
        "require_mode_selection bus || echo RC=$?",
    ]))
    assert "RC=1" in done.stdout
    # Both halves still name trigger: no half-applied selection survives.
    assert _state(tmp_path) == ("trigger", "asound-trigger-mode.conf")


def test_a_failed_rollback_is_louder_than_a_clean_failure_sr039(tmp_path):
    """Link and mode file disagreeing is the one state nobody can reason about.

    It must not return the same code as a clean rollback, because a caller that
    treats them alike would report 'nothing was changed' about a panel whose
    chain and policy record now name different modes.
    """
    assert _run(tmp_path, "require_mode_selection trigger").returncode == 0
    done = _run(tmp_path, "\n".join([
        "mv() { return 1; }",
        "ln() { case \"$*\" in *asound-bus-mode*) command ln \"$@\" ;; *) return 1 ;; esac; }",
        "require_mode_selection bus || echo RC=$?",
    ]))
    assert "RC=3" in done.stdout, done.stdout + done.stderr
    assert "INCONSISTENT" in done.stderr, done.stderr
    assert "rollback failed" in done.stderr


def test_the_temp_file_never_survives_a_failure_sr039(tmp_path):
    """A leftover .tmp.<pid> beside the mode file would be read by nothing."""
    _run(tmp_path, "mv() { return 1; }\nrequire_mode_selection bus || true")
    leftovers = list(tmp_path.glob("audio-mode.tmp.*"))
    assert leftovers == [], leftovers


# ── Terra round 2: the distinction must ESCAPE, and a non-symlink is refused ──

def test_the_inconsistent_state_has_its_own_exit_code_sr039(tmp_path):
    """A caller must be able to TELL a failed rollback from a clean refusal.

    The first version of this fix printed "INCONSISTENT" and then returned 1,
    so the distinction the requirement advertises existed only in a log line.
    A caller that treats 3 like 1 is making a choice; one that cannot tell them
    apart is not.
    """
    assert _run(tmp_path, "require_mode_selection trigger").returncode == 0
    done = _run(tmp_path, "\n".join([
        "mv() { return 1; }",
        "ln() { case \"$*\" in *asound-bus-mode*) command ln \"$@\" ;; *) return 1 ;; esac; }",
        "require_mode_selection bus || echo RC=$?",
    ]))
    assert "RC=3" in done.stdout, done.stdout + done.stderr
    assert "INCONSISTENT" in done.stderr


def test_a_clean_refusal_keeps_its_own_distinct_code_sr039(tmp_path):
    """...and the ordinary publish failure is still 1, not 3."""
    done = _run(tmp_path, "mv() { return 1; }\nrequire_mode_selection bus || echo RC=$?")
    assert "RC=1" in done.stdout, done.stdout + done.stderr


def test_a_link_that_is_a_real_file_is_refused_before_anything_moves_sr039(tmp_path):
    """TERRA ROUND 2. `ln -sfn` would replace it and the rollback could only rm it.

    That destroys a file the Owner put there while reporting "nothing was
    changed". There is nothing to restore it from, so the only honest move is
    to decline before the first mutation.
    """
    link = tmp_path / "conf" / "audio-out.conf"
    link.parent.mkdir(exist_ok=True)
    link.write_text("the Owner's own configuration\n", encoding="utf-8")

    done = _run(tmp_path, "require_mode_selection bus || echo RC=$?")
    assert "RC=4" in done.stdout, done.stdout + done.stderr
    assert "not a readable symlink" in done.stderr
    # Untouched, and no mode file was written either.
    assert link.read_text(encoding="utf-8") == "the Owner's own configuration\n"
    assert not (tmp_path / "audio-mode").exists()


def test_a_dangling_symlink_is_still_a_symlink_and_is_replaced_sr039(tmp_path):
    """The refusal above must not catch a link whose target simply went away.

    `[ -e ]` is false for a dangling symlink, so a naive existence check would
    refuse the ordinary case of a conf file not yet rendered.
    """
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    (conf / "audio-out.conf").symlink_to(conf / "asound-nothing-here.conf")

    done = _run(tmp_path, "require_mode_selection bus")
    assert done.returncode == 0, done.stderr
    assert _state(tmp_path) == ("bus", "asound-bus-mode.conf")
