"""Delivery of the tracked stack tree to a running hub.

Verifies: SR-047 (the delivery half), LLR-992
Cases:    TC-1042, TC-1043, TC-1044

These tests exercise the PURE parts - what is deliverable, what is protected,
and what the plan says - without touching a hub. The parts that need a box are
named in the scope document as demonstrations, not pretended at here.
"""

import hashlib
import importlib.util
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "deploy_stack.py"


def _load():
    spec = importlib.util.spec_from_file_location("deploy_stack", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ds = _load()


# ── rule 1: the live .env and credentials are never written ─────────────────

@pytest.mark.parametrize("rel", [
    ".env",
    "samba-users.creds",
    "cifs.creds",
    "secrets/anything.txt",
])
def test_live_configuration_and_credentials_are_never_delivered_sr047(rel):
    """THE BOX'S `.env` IS THE ONLY COPY OF ITS SECRETS.

    It is not in the repo, and `.env.example` beside it is a template full of
    placeholders. A sync that treated them as the same file would take the hub
    down and destroy credentials that exist nowhere else - and it would do it
    while reporting a successful delivery.
    """
    why = ds.excluded(rel, include_panel=False)
    assert why is not None, "%s must never be written to the hub" % rel
    assert "protected" in why


def test_the_env_template_IS_delivered_and_is_not_the_env_sr047():
    """`.env.example` is not `.env`, and refusing it would hide the new knobs.

    The template is how an operator learns that a release needs
    LITELLM_CONTAINER_IP. Protecting it "to be safe" would mean every new knob
    arrived undocumented on the one machine that needs to be configured.
    """
    assert ds.excluded(".env.example", include_panel=False) is None


# ── the hub/panel boundary, which the directory names get wrong ─────────────

@pytest.mark.parametrize("rel,hub_side,why", [
    ("autoinstall/wall/wall-firstboot.sh", False, "the panel's own firstboot"),
    ("panel-audio/routed_backend.py",      False, "installed by the PANEL's firstboot; inert on the hub"),
    ("panel-access/install-gateway.py",    True,  "installed by the HUB's firstboot"),
    ("wall-shell/index.html",              True,  "caddy bind-mounts ./wall-shell and serves it FROM the hub"),
    ("litellm/config.yaml",                True,  "bind-mounted into the litellm container"),
    ("llm-isolation/llm-isolation.sh",     True,  "the hub's egress fence"),
])
def test_the_panel_boundary_is_by_usage_not_by_name_sr047(rel, hub_side, why):
    """TWO OF THE FOUR PANEL-SOUNDING DIRECTORIES ARE HUB-SIDE.

    Excluding by name would have dropped `wall-shell/` - which caddy mounts and
    serves from the hub - and kept `panel-audio/`, which nothing on the hub
    reads. Measured from the hub's compose file and the hub's firstboot rather
    than inferred: %s.
    """
    got = ds.excluded(rel, include_panel=False)
    if hub_side:
        assert got is None, "%s is hub-side (%s) and must ship" % (rel, why)
    else:
        assert got is not None and "panel" in got, \
            "%s is panel payload (%s) and must not" % (rel, why)


def test_the_panel_payload_can_be_forced_but_is_off_by_default_sr047():
    for rel in ("autoinstall/wall/x.sh", "panel-audio/y.py"):
        assert ds.excluded(rel, include_panel=False) is not None
        assert ds.excluded(rel, include_panel=True) is None


# ── the plan ────────────────────────────────────────────────────────────────

def test_the_plan_separates_absent_from_stale_from_identical_sr047():
    payload = {
        "a": ("aaa", b"a", False),
        "b": ("bbb", b"b", False),
        "c": ("ccc", b"c", False),
    }
    current = {"b": "different", "c": "ccc", "orphan": "zzz"}
    missing, stale, same = ds.plan(payload, current)
    assert missing == ["a"], "absent on the box"
    assert stale == ["b"], "present but different"
    assert same == ["c"], "already correct"
    # AND THE ORPHAN IS NOT IN ANY OF THEM. Rule 2: a file the repo does not
    # track is left alone, never deleted. `docker-compose.override.yml`, the
    # deployed panel-access/app bundle and llm-gateway.env are all in that
    # category, and removing them would take live configuration with them.
    assert "orphan" not in missing + stale + same


# ── rule 3: the placement must not replace inodes ───────────────────────────

def test_placement_preserves_inodes_rather_than_replacing_files_sr047():
    """COMPOSE BIND-MOUNTS BIND THE INODE, NOT THE PATH.

    `install`, `mv`, `cp` and `tar -x` all unlink and recreate, which detaches
    the mount: the container keeps serving the old bytes and a reload is a
    silent no-op. The litellm service bind-mounts TWO INDIVIDUAL FILES
    (`config.yaml`, `devpc_hold_hook.py`), so this is not hypothetical for this
    lane - it is the second delivery onwards.

    Asserting on the shipped shell text is weak, and it is the strongest thing
    available without a box; the real check is the demonstration named in the
    scope document.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    place = src[src.index("    place = r\"\"\""):src.index("\"\"\" % STACK_REMOTE")]

    # COMMENTS ARE STRIPPED BEFORE ASSERTING, and that is not tidiness.
    # This test passed against the WRONG THING once: the placement was changed
    # from `sh -c "cat '$S/$f' > '$D/$f'"` to positional arguments, and the
    # assertion went on passing because the new code's comment QUOTED the old
    # form verbatim while explaining why it had gone. A source-text test that
    # can be satisfied by a comment is not testing the code.
    code = "\n".join(l for l in place.splitlines()
                     if not l.lstrip().startswith("#"))

    assert 'cat "$1" > "$2"' in code, \
        "placement must truncate in place through positional arguments"
    assert "$S/$f' > '$D/$f" not in code, \
        "paths must not be interpolated into a shell string - a filename " \
        "containing a quote would run as root"
    for bad in (" install -", " mv ", " cp "):
        assert bad not in code, \
            "%s replaces the inode and would detach a bind mount" % bad.strip()
    # tar IS used, but only to unpack into a staging directory, never over the
    # live tree.
    assert 'tar -x -C "$S"' in code
    # AND IT MUST BE POSIX. `read -r -d ''` and `< <(...)` are bash; `ssh host
    # <cmd>` runs the login shell, which is dash on plenty of Ubuntu accounts,
    # so those would have failed to PARSE on an ordinary target. They were
    # introduced while "hardening" a version that had been POSIX already.
    assert "read -r -d" not in code, "bash-only; the remote shell may be dash"
    assert "< <(" not in code, "process substitution is bash-only"


# ── the source of truth is a commit, not the working tree ───────────────────

def test_the_payload_comes_from_a_commit_not_the_working_tree_sr047(tmp_path):
    """A DIRTY TREE IS HOW YOU DEPLOY SOMEBODY ELSE'S HALF-FINISHED CHANGE.

    This repo has already paid for working-tree-as-truth once, losing six
    requirement rows. The payload is `git archive <revision>`, so an
    uncommitted edit cannot reach the hub - and the tool says so out loud when
    the tree is dirty rather than silently ignoring it.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert '"archive"' in src and '"--format=tar"' in src
    assert "status" in src and "porcelain" in src, \
        "a dirty tree must be reported, not silently skipped"

    payload, skipped = ds.build_payload("HEAD", include_panel=False)
    assert payload, "HEAD must produce a deliverable payload"
    assert ".env" not in payload
    assert not any(k.startswith("autoinstall/wall/") for k in payload)

    # Every hash is of the bytes git would deliver - LF, not the CRLF a Windows
    # checkout has on disk. A hash taken from the working copy would mark every
    # text file stale for ever on a Windows clone.
    rel, (h, data, _x) = next(iter(sorted(payload.items())))
    assert hashlib.sha256(data).hexdigest() == h
    assert b"\r\n" not in data or rel.endswith((".bat", ".cmd", ".ps1")), \
        "%s carries CRLF; git archive should have normalised it" % rel


def test_a_report_path_that_exists_is_refused_sr047(tmp_path):
    """Evidence is never overwritten - the panel release lane's rule, reused."""
    existing = tmp_path / "report.json"
    existing.write_text("{}", encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--target", "nobody@nowhere.invalid",
         "--report", str(existing)],
        capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode != 0
    assert "never overwritten" in (r.stdout + r.stderr)


# ── the placement script, EXECUTED ──────────────────────────────────────────
#
# The source-text test above cannot see shell compatibility, symlink handling,
# inode behaviour or a mid-placement failure - a reviewer said so, and was
# right. These run the shipped script against a fake box.

import os
import shutil
import stat
import subprocess as sp
import tarfile
import io

_BASH = shutil.which("bash") or r"C:\Program Files\Git\usr\bin\bash.EXE"
_needs_bash = pytest.mark.skipif(not pathlib.Path(_BASH).exists(),
                                 reason="no bash available")


def _fake_box(tmp_path):
    """A stand-in hub: a stack tree, and a `sudo` that is a no-op passthrough.

    Stubbing `sudo` rather than requiring root is the same technique the
    iptables fence tests use. It keeps the script under test byte-identical to
    the one that runs against the real box.
    """
    box = tmp_path / "stack"
    box.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sudo = bindir / "sudo"
    # Drop the -n and exec the rest; that is all `sudo -n` means here.
    sudo.write_text('#!/usr/bin/env bash\n[ "$1" = "-n" ] && shift\nexec "$@"\n',
                    encoding="utf-8")
    sudo.chmod(sudo.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return box, bindir


def _place_script(box):
    src = SCRIPT.read_text(encoding="utf-8")
    body = src[src.index('    place = r"""') + len('    place = r"""'):
               src.index('""" % STACK_REMOTE')]
    return body % str(box).replace("\\", "/")


def _tar_of(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        manifest = ("\n".join(files) + "\n").encode()
        mi = tarfile.TarInfo(".deploy-manifest"); mi.size = len(manifest)
        tf.addfile(mi, io.BytesIO(manifest))
        for name, data in files.items():
            ti = tarfile.TarInfo(name); ti.size = len(data); ti.mode = 0o644
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _run_place(tmp_path, box, blob):
    """Execute the SHIPPED placement script against a fake tree, unprivileged.

    `DEPLOY_SUDO=` drops the privilege prefix. Stubbing a `sudo` on PATH was
    tried first and does not work here: Windows ships its own `sudo.exe`, which
    wins the lookup and rejects `-n`. Making the escalation a variable in the
    script is both testable and honest about what it does.

    The script is given as a FILE and the tar on STDIN, because the script
    reads the payload from stdin - feeding both down the same pipe made `sh`
    try to execute the tar.
    """
    sfile = tmp_path / "place.sh"
    sfile.write_text(_place_script(box), encoding="utf-8")
    return sp.run([_BASH, "-c", 'DEPLOY_SUDO= exec sh "$1"', "_",
                   str(sfile).replace("\\", "/")],
                  input=blob, capture_output=True)


@_needs_bash
def test_the_placement_script_is_posix_and_runs_under_plain_sh_sr047(tmp_path):
    """IT REQUIRED BASH AND ASKED FOR AN UNSPECIFIED SHELL.

    `ssh host <command>` runs the login shell, which is `dash` on plenty of
    Ubuntu accounts. The loop used `read -r -d ''` and process substitution,
    both bash-only, so the delivery would have failed to PARSE on a perfectly
    ordinary target - and that was introduced while HARDENING a version which
    had been POSIX. `sh -n` is the check that would have caught it.
    """
    box, _ = _fake_box(tmp_path)
    script = _place_script(box)
    r = sp.run([_BASH, "-c", "sh -n"], input=script.encode(), capture_output=True)
    assert r.returncode == 0, \
        "the placement script is not POSIX sh:\n%s" % r.stderr.decode()


@_needs_bash
def test_placement_keeps_the_inode_so_a_bind_mount_survives_sr047(tmp_path):
    """The property the whole design turns on, measured rather than asserted."""
    box, _ = _fake_box(tmp_path)
    target = box / "litellm" / "config.yaml"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old\n")
    before = os.stat(str(target)).st_ino

    r = _run_place(tmp_path, box, _tar_of({"litellm/config.yaml": b"new\n"}))
    assert r.returncode == 0, r.stderr.decode()
    assert target.read_bytes() == b"new\n", "content must be replaced"
    assert os.stat(str(target)).st_ino == before, \
        "THE INODE CHANGED - a compose bind mount on this path would now be " \
        "detached and the container would serve the old bytes for ever"


@_needs_bash
def test_placement_refuses_to_write_through_a_symlink_sr047(tmp_path):
    """A SYMLINK AT THE DESTINATION COULD HAVE TRUNCATED `.env`.

    `find -type f` omits symlinks, so a tracked path that exists on the box AS
    A SYMLINK read as absent, and `cat > dst` then followed it as root. A
    `litellm/config.yaml -> ../.env` would have destroyed the one file this
    tool promises never to write, while reporting a clean delivery.
    """
    box, _ = _fake_box(tmp_path)
    env = box / ".env"
    env.write_bytes(b"SECRET=keepme\n")
    (box / "litellm").mkdir()
    link = box / "litellm" / "config.yaml"
    try:
        os.symlink(str(env), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create symlinks without privilege")

    r = _run_place(tmp_path, box, _tar_of({"litellm/config.yaml": b"attacker\n"}))

    assert r.returncode != 0, "writing through a symlink must be refused"
    assert b"symlink" in r.stderr.lower(), r.stderr.decode()
    assert env.read_bytes() == b"SECRET=keepme\n", \
        "THE PROTECTED .env WAS OVERWRITTEN THROUGH A SYMLINK"
