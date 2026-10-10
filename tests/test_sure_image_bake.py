"""Behavioural contracts for how the Sure fork and the digest-pinned images are baked.

Verifies: SR-061, LLR-1041 (image knob, bake-time digest verification, manifest ids).

These drive the REAL vmtest/export-images.sh against a stub `docker` on PATH, so they
prove the script's decisions (what it refuses, what it retags, what it records) - NOT
that a real daemon, registry or `docker load` behaves as the stub does. That lab step
is listed in stack/sure/README.md and has not run.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "vmtest" / "export-images.sh"
FIRSTBOOT = ROOT / "stack" / "autoinstall" / "firstboot.sh"
pytestmark = [pytest.mark.full, pytest.mark.smoke]

PG_DIG = "sha256:" + "a" * 64
RD_DIG = "sha256:" + "b" * 64
ENV_TEXT = f"""COMPOSE_PROFILES=sure
SURE_IMAGE=sure-auroleap:local
SURE_POSTGRES_IMAGE_TAG=16.15
SURE_POSTGRES_IMAGE_DIGEST={PG_DIG}
SURE_REDIS_IMAGE_TAG=8.10.2
SURE_REDIS_IMAGE_DIGEST={RD_DIG}
GAME_RELAY_ENABLED=false
"""

# A minimal docker: state lives in $STUB as "ref<TAB>id" lines in `present`.
STUB = r'''#!/usr/bin/env bash
S="$STUB"
present() { awk -F'\t' -v r="$1" '$1==r{id=$2; f=1} END{if(f)print id; exit !f}' "$S/present"; }
case "$1" in
  compose) cat "$S/images.txt" ;;
  image)   # image inspect REF --format F
    ref="$3"; fmt="$5"
    id="$(present "$ref")" || exit 1
    case "$fmt" in
      *.Id*) echo "$id" ;;
      *) echo "" ;;
    esac ;;
  pull)
    echo "$2" >> "$S/pulls.log"
    grep -qxF "$2" "$S/pull-fails" 2>/dev/null && exit 1
    case "$2" in *@sha256:*) printf '%s\t%s\n' "$2" "sha256:pulled-$(echo "$2" | tr -c 'a-z0-9\n' _)" >> "$S/present" ;; esac
    exit 0 ;;
  tag)
    id="$(present "$2")" || exit 1
    printf '%s\t%s\n' "$3" "$id" >> "$S/present" ;;
  save)   # save REF -o OUT
    echo "tar-of-$2" > "$4" ;;
  *) exit 0 ;;
esac
'''


def _w(path, text):
    """Write LF bytes (older Path.write_text has no newline=)."""
    path.write_bytes(text.encode("utf-8"))


def _px(path):
    """A path the bash on PATH understands (Git Bash on Windows wants /c/...)."""
    text = Path(path).as_posix()
    if len(text) > 2 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:]
    return text


def _bash():
    exe = shutil.which("bash")
    if not exe:
        pytest.skip("bash not available")
    return exe


def _run(tmp_path, images, present, pull_fails=(), env_text=ENV_TEXT):
    bash = _bash()
    stub = tmp_path / "stub"
    stub.mkdir()
    _w(stub / "images.txt", "\n".join(images) + "\n")
    _w(stub / "present", "".join(f"{r}\t{i}\n" for r, i in present))
    _w(stub / "pull-fails", "\n".join(pull_fails) + "\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    _w(docker, STUB)
    docker.chmod(0o755)
    envfile = tmp_path / "env"
    _w(envfile, env_text)
    out = tmp_path / "out"
    env = dict(os.environ, PATH=f"{_px(bindir)}:{os.environ['PATH']}", STUB=_px(stub),
               IMAGES_OUT=_px(out), ALLOW_STALE_LOCAL="1")
    proc = subprocess.run([bash, _px(EXPORT), "--env-file", _px(envfile)], env=env,
                          capture_output=True, text=True)
    return proc, out, stub


def _manifest(out):
    rows = [l.split("\t") for l in (out / "images.manifest.tsv").read_text().splitlines()]
    return {r[0]: r for r in rows[1:]}


def test_pinned_digest_is_verified_retagged_and_recorded(tmp_path):
    proc, out, stub = _run(
        tmp_path,
        images=["sure-auroleap:local", "postgres:16.15", "redis:8.10.2"],
        # a STALE local postgres:16.15 must be overwritten, never trusted
        present=[("sure-auroleap:local", "sha256:fork"), ("postgres:16.15", "sha256:stale")],
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    pulls = (stub / "pulls.log").read_text().split()
    assert f"postgres@{PG_DIG}" in pulls and f"redis@{RD_DIG}" in pulls
    assert "postgres:16.15" not in pulls and "redis:8.10.2" not in pulls  # never pulled by mutable tag
    m = _manifest(out)
    assert m["postgres:16.15"][5] == "pinned" and m["postgres:16.15"][1] != "sha256:stale"
    assert m["redis:8.10.2"][5] == "pinned"
    assert m["sure-auroleap:local"][5] == "local" and m["sure-auroleap:local"][1] == "sha256:fork"
    assert (out / "sure-auroleap_local.tar").exists()  # saved under the plain tag


def test_unverifiable_pin_refuses_the_bake(tmp_path):
    proc, out, _ = _run(
        tmp_path,
        images=["postgres:16.15"],
        present=[("postgres:16.15", "sha256:stale")],
        pull_fails=[f"postgres@{PG_DIG}"],
    )
    assert proc.returncode != 0
    assert "unverified" in (proc.stderr + proc.stdout).lower()
    assert not (out / "postgres_16.15.tar").exists()


def test_malformed_digest_knob_refuses(tmp_path):
    proc, _, _ = _run(tmp_path, images=["redis:8.10.2"], present=[],
                      env_text=ENV_TEXT.replace(RD_DIG, "sha256:short"))
    assert proc.returncode != 0 and "not a sha256" in (proc.stderr + proc.stdout)


def test_tag_at_digest_ref_in_the_resolved_set_is_refused(tmp_path):
    proc, _, _ = _run(tmp_path, images=[f"postgres:16.15@{PG_DIG}"], present=[])
    assert proc.returncode != 0 and "tag@digest" in (proc.stderr + proc.stdout)


def test_missing_sure_fork_is_a_refused_build_with_the_recipe(tmp_path):
    proc, _, _ = _run(tmp_path, images=["sure-auroleap:local"], present=[])
    text = proc.stderr + proc.stdout
    assert proc.returncode != 0
    assert "build-sure-image.sh" in text and "docker pull" not in text


def test_firstboot_verifies_loaded_ids_for_local_and_pinned_only():
    text = FIRSTBOOT.read_text(encoding="utf-8")
    assert "verify_loaded_image()" in text
    body = text.split("verify_loaded_image()", 1)[1].split("\n}\n", 1)[0]
    assert "local|pinned" in body and "exit 1" in body and "FATAL" in body
    # called after BOTH load paths (plain and zstd), never on the skip path
    assert text.count('verify_loaded_image "$name"') == 2


def test_build_recipe_is_wsl_real_docker_and_stamps_source():
    text = (ROOT / "scripts" / "build-sure-image.sh").read_text(encoding="utf-8")
    assert "wsl -d Ubuntu" in text
    for needle in ("homehub.source.revision", "homehub.source.ref", "BUILD_COMMIT_SHA", "git -C", "archive"):
        assert needle in text
    assert "\r" not in text
