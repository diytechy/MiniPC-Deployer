"""tests/git_retry.py: a transient `fatal:` is retried, a lasting one still fails.

The helper exists because three repo-query tests failed in one hour on a shared
.git with "fatal: Invalid path '/mnt'" (see its docstring). These assert both
halves, so the helper can neither stop retrying nor start hiding a real fault.
"""

import subprocess

import pytest

import git_retry


class _FakeRun:
    """A stateful subprocess.run: answers from a script of (rc, stderr) pairs."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, argv, check=False, **kwargs):
        rc, err = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return subprocess.CompletedProcess(argv, rc, "a" * 40 if rc == 0 else "", err)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(git_retry, "PAUSE_SEC", 0)

    def install(script):
        run = _FakeRun(script)
        monkeypatch.setattr(git_retry.subprocess, "run", run)
        return run
    return install


MNT = "fatal: Invalid path '/mnt': No such file or directory\n"


def test_a_transient_fatal_is_retried_and_the_answer_returned(fake):
    run = fake([(128, MNT), (0, "")])
    done = git_retry.run_git(["git", "-C", ".", "rev-parse", "HEAD"])
    assert done.returncode == 0 and done.stdout == "a" * 40
    assert run.calls == 2


def test_a_lasting_fatal_still_fails_with_gits_own_message(fake):
    run = fake([(128, MNT)])
    with pytest.raises(subprocess.CalledProcessError) as caught:
        git_retry.run_git(["git", "-C", ".", "ls-files"])
    assert run.calls == git_retry.ATTEMPTS == 3
    assert "Invalid path" in caught.value.stderr


def test_a_failure_that_is_not_fatal_is_not_retried(fake):
    run = fake([(1, "")])
    done = git_retry.run_git(["git", "diff", "--quiet"], check=False)
    assert done.returncode == 1 and run.calls == 1


def test_the_real_shape_of_the_fault_surfaces_through_the_helper(tmp_path, monkeypatch):
    """Real git, real repo: a configured path that does not exist is the
    message the baseline saw. It must reach the test, not be swallowed."""
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git not installed")
    monkeypatch.setattr(git_retry, "PAUSE_SEC", 0)
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "core.worktree", "/no-such-root-w10/x"], check=True)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        git_retry.run_git(["git", "-C", str(repo), "ls-files"])
    assert "Invalid path" in caught.value.stderr, caught.value.stderr
