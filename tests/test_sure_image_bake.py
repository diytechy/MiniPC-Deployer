"""Behavioural contracts for how the Sure fork and the digest-pinned images are baked.

Verifies: SR-061, LLR-1041 (image knobs, bake-time digest verification, source-ref
check, store-independent verification of loaded images, build recipe); TC-1362
(SR-100, LLR-1210: the bake and the resolver select Sure by SURE_ENABLED, never by
a `sure` entry in COMPOSE_PROFILES, which both refuse).

Everything here drives the REAL scripts (vmtest/export-images.sh,
scripts/build-sure-image.sh, and verify_loaded_image extracted verbatim from
stack/autoinstall/firstboot.sh) against a stub `docker` on PATH. That proves the
scripts' decisions - what they refuse, retag, remove and record - NOT that a real
daemon, registry or `docker load` behaves as the stub does. The lab step that does
is listed in stack/sure/README.md and has not run.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "vmtest" / "export-images.sh"
FIRSTBOOT = ROOT / "stack" / "autoinstall" / "firstboot.sh"
BUILD = ROOT / "scripts" / "build-sure-image.sh"
pytestmark = [pytest.mark.full, pytest.mark.smoke]

PG_DIG = "sha256:" + "a" * 64
RD_DIG = "sha256:" + "b" * 64
REF = "auroleap-integration"
ENV_TEXT = f"""COMPOSE_PROFILES=
SURE_ENABLED=true
SURE_IMAGE=sure-auroleap:local
SURE_SOURCE_REF={REF}
SURE_POSTGRES_IMAGE_TAG=16.15
SURE_POSTGRES_IMAGE_DIGEST={PG_DIG}
SURE_REDIS_IMAGE_TAG=8.10.2
SURE_REDIS_IMAGE_DIGEST={RD_DIG}
GAME_RELAY_ENABLED=false
"""

# A minimal docker. State is files under $STUB:
#   present      "ref<TAB>id" lines (last match wins)
#   labels       "ref<TAB>homehub.source.ref value"
#   rootfs       "ref<TAB>json" overriding the default layer list
#   pull-fails   refs whose pull fails
#   tag-skew     if it exists, `docker tag` gives the new tag a DIFFERENT id
#   podman       if it exists, the CLI and server say they are Podman
#   calls.log    every invocation
STUB = r'''#!/usr/bin/env bash
S="$STUB"
printf '%s\n' "$*" >> "$S/calls.log"
last() { [ -f "$S/$1" ] && awk -F'\t' -v r="$2" '$1==r{v=$2; f=1} END{if(f)print v; exit !f}' "$S/$1"; }
case "$1" in
  --version) if [ -f "$S/podman" ]; then echo "podman version 5.0"; else echo "Docker version 27.0.1, build x"; fi ;;
  version)   if [ -f "$S/podman" ]; then echo "Podman Engine"; else echo "Docker Engine - Community"; fi ;;
  info) exit 0 ;;
  compose) cat "$S/images.txt" ;;
  image)   # image inspect REF --format F
    ref="$3"; fmt="$5"
    id="$(last present "$ref")" || exit 1
    case "$fmt" in
      *.Id*) echo "$id" ;;
      *homehub.source.ref*) last labels "$ref" || echo "" ;;
      *RootFS.Layers*) last rootfs "$ref" || echo "[\"layer-of-$id\"]" ;;
      *) echo "" ;;
    esac ;;
  pull)
    grep -qxF "$2" "$S/pull-fails" 2>/dev/null && exit 1
    case "$2" in *@sha256:*) printf '%s\t%s\n' "$2" "sha256:pulled-$(echo "$2" | tr -c 'a-z0-9\n' _)" >> "$S/present" ;; esac
    exit 0 ;;
  tag)
    id="$(last present "$2")" || exit 1
    [ -f "$S/tag-skew" ] && id="sha256:skewed"
    printf '%s\t%s\n' "$3" "$id" >> "$S/present" ;;
  rmi) grep -v "^$2	" "$S/present" > "$S/present.new"; mv "$S/present.new" "$S/present" ;;
  save)   # save REF -o OUT
    echo "tar-of-$2" > "$4" ;;
  build)  # build -t TAG ... -   (consume the context tar)
    cat > /dev/null
    while [ $# -gt 0 ]; do [ "$1" = "-t" ] && printf '%s\t%s\n' "$2" "sha256:built" >> "$S/present"; shift; done ;;
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


def _stub(tmp_path, present=(), labels=(), rootfs=(), pull_fails=(), images=(), flags=()):
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    _w(stub / "images.txt", "\n".join(images) + "\n")
    _w(stub / "present", "".join(f"{r}\t{i}\n" for r, i in present))
    _w(stub / "labels", "".join(f"{r}\t{v}\n" for r, v in labels))
    _w(stub / "rootfs", "".join(f"{r}\t{v}\n" for r, v in rootfs))
    _w(stub / "pull-fails", "\n".join(pull_fails) + "\n")
    _w(stub / "calls.log", "")
    for flag in flags:
        _w(stub / flag, "1\n")
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    _w(docker, STUB)
    docker.chmod(0o755)
    env = dict(os.environ, PATH=f"{_px(bindir)}:{os.environ['PATH']}", STUB=_px(stub))
    return stub, env


def _run(tmp_path, images, present, pull_fails=(), env_text=ENV_TEXT, labels=None, flags=()):
    bash = _bash()
    if labels is None:
        labels = [("sure-auroleap:local", REF)]
    stub, env = _stub(tmp_path, present=present, labels=labels, pull_fails=pull_fails,
                      images=images, flags=flags)
    envfile = tmp_path / "env"
    _w(envfile, env_text)
    out = tmp_path / "out"
    env.update(IMAGES_OUT=_px(out), ALLOW_STALE_LOCAL="1")
    proc = subprocess.run([bash, _px(EXPORT), "--env-file", _px(envfile)], env=env,
                          capture_output=True, text=True)
    return proc, out, stub


def _manifest(out):
    rows = [l.split("\t") for l in (out / "images.manifest.tsv").read_text().splitlines()]
    return {r[0]: r for r in rows[1:]}


def _text(proc):
    return proc.stderr + proc.stdout


# ── exporter ─────────────────────────────────────────────────────────────────

def test_pinned_digest_is_verified_retagged_and_recorded(tmp_path):
    proc, out, stub = _run(
        tmp_path,
        images=["sure-auroleap:local", "postgres:16.15", "redis:8.10.2"],
        # a STALE local postgres:16.15 must be overwritten, never trusted
        present=[("sure-auroleap:local", "sha256:fork"), ("postgres:16.15", "sha256:stale")],
    )
    assert proc.returncode == 0, _text(proc)
    pulls = [l.split(" ", 1)[1] for l in (stub / "calls.log").read_text().splitlines() if l.startswith("pull ")]
    assert f"postgres@{PG_DIG}" in pulls and f"redis@{RD_DIG}" in pulls
    assert "postgres:16.15" not in pulls and "redis:8.10.2" not in pulls  # never pulled by mutable tag
    m = _manifest(out)
    assert m["postgres:16.15"][5] == "pinned" and m["postgres:16.15"][1] != "sha256:stale"
    assert m["redis:8.10.2"][5] == "pinned"
    assert m["sure-auroleap:local"][5] == "local" and m["sure-auroleap:local"][1] == "sha256:fork"
    # the store-independent identity is the sha256 of the diff-id list, not the Id
    layers = '["layer-of-sha256:fork"]\n'
    assert m["sure-auroleap:local"][6] == hashlib.sha256(layers.encode()).hexdigest()
    assert (out / "sure-auroleap_local.tar").exists()


def test_unverifiable_pin_refuses_the_bake(tmp_path):
    proc, out, _ = _run(tmp_path, images=["postgres:16.15"], present=[("postgres:16.15", "sha256:stale")],
                        pull_fails=[f"postgres@{PG_DIG}"])
    assert proc.returncode != 0 and "unverified" in _text(proc).lower()
    assert not (out / "postgres_16.15.tar").exists()


def test_id_mismatch_after_tag_refuses_the_bake(tmp_path):
    """The tag must end up on the image pulled by digest; a skewed tag is fatal."""
    proc, out, _ = _run(tmp_path, images=["postgres:16.15"], present=[], flags=["tag-skew"])
    assert proc.returncode != 0 and "is not the image pulled by digest" in _text(proc)
    assert not (out / "postgres_16.15.tar").exists()


def test_malformed_digest_knob_refuses(tmp_path):
    proc, _, _ = _run(tmp_path, images=["redis:8.10.2"], present=[],
                      env_text=ENV_TEXT.replace(RD_DIG, "sha256:short"))
    assert proc.returncode != 0 and "not a sha256" in _text(proc)


def test_empty_tag_knob_with_a_digest_refuses(tmp_path):
    proc, _, _ = _run(tmp_path, images=["redis:8.10.2"], present=[],
                      env_text=ENV_TEXT.replace("SURE_REDIS_IMAGE_TAG=8.10.2", "SURE_REDIS_IMAGE_TAG="))
    assert proc.returncode != 0 and "SURE_REDIS_IMAGE_TAG is empty" in _text(proc)


def test_missing_tag_knob_with_a_digest_refuses(tmp_path):
    proc, _, _ = _run(tmp_path, images=["postgres:16.15"], present=[],
                      env_text=ENV_TEXT.replace("SURE_POSTGRES_IMAGE_TAG=16.15\n", ""))
    assert proc.returncode != 0 and "SURE_POSTGRES_IMAGE_TAG is empty" in _text(proc)


def test_tag_at_digest_ref_in_the_resolved_set_is_refused(tmp_path):
    proc, _, _ = _run(tmp_path, images=[f"postgres:16.15@{PG_DIG}"], present=[])
    assert proc.returncode != 0 and "tag@digest" in _text(proc)


def test_missing_sure_fork_is_a_refused_build_with_the_recipe(tmp_path):
    proc, _, stub = _run(tmp_path, images=["sure-auroleap:local"], present=[])
    assert proc.returncode != 0 and "build-sure-image.sh" in _text(proc)
    assert "pull " not in (stub / "calls.log").read_text()  # never tried a registry


def test_image_built_from_the_wrong_ref_is_refused_even_with_allow_stale(tmp_path):
    proc, out, _ = _run(tmp_path, images=["sure-auroleap:local"],
                        present=[("sure-auroleap:local", "sha256:fork")],
                        labels=[("sure-auroleap:local", "auroleap-bank-stale-capability")])
    assert proc.returncode != 0 and "WRONG-BRANCH" in _text(proc)
    assert "auroleap-bank-stale-capability" in _text(proc) and REF in _text(proc)
    assert not (out / "sure-auroleap_local.tar").exists()


def test_image_with_no_ref_label_is_refused(tmp_path):
    proc, _, _ = _run(tmp_path, images=["sure-auroleap:local"],
                      present=[("sure-auroleap:local", "sha256:fork")], labels=[])
    assert proc.returncode != 0 and "<none>" in _text(proc)


def test_empty_source_ref_knob_refuses(tmp_path):
    proc, _, _ = _run(tmp_path, images=["sure-auroleap:local"],
                      present=[("sure-auroleap:local", "sha256:fork")],
                      env_text=ENV_TEXT.replace(f"SURE_SOURCE_REF={REF}\n", "SURE_SOURCE_REF=\n"))
    assert proc.returncode != 0 and "SURE_SOURCE_REF is empty" in _text(proc)


# ── TC-1362: the knob, not the profile, selects Sure for the bake ───────────
# Until 2026-10-22 both scripts asked "is `sure` in COMPOSE_PROFILES"; the hub's
# config dropped the profile on 2026-10-18 (firstboot's bulk `up -d` would wait on
# Sure's database), so every bake since would have left out sure-auroleap:local,
# which has no registry home: a reimaged hub could never start Sure again.

def _compose_calls(stub):
    return [l for l in (stub / "calls.log").read_text().splitlines() if l.startswith("compose ")]


def test_tc1362_sure_enabled_bakes_the_sure_profile_sr100(tmp_path):
    proc, out, stub = _run(tmp_path, images=["sure-auroleap:local"],
                           present=[("sure-auroleap:local", "sha256:fork")])
    assert proc.returncode == 0, _text(proc)
    calls = _compose_calls(stub)
    assert calls and all("--profile sure" in c for c in calls), calls
    assert "SURE_ENABLED=true" in _text(proc)


@pytest.mark.parametrize("value", ["false", "True", "", "yes"])
def test_tc1362_anything_but_exactly_true_bakes_no_sure_sr100(tmp_path, value):
    proc, _, stub = _run(tmp_path, images=["postgres:16.15"], present=[],
                         env_text=ENV_TEXT.replace("SURE_ENABLED=true\n", "SURE_ENABLED=%s\n" % value))
    calls = _compose_calls(stub)
    assert calls and not any("--profile sure" in c for c in calls), calls
    assert "the Sure images are NOT baked" in _text(proc)


@pytest.mark.parametrize("profiles", ["sure", "ntfy,sure", "sure,immich"])
def test_tc1362_a_sure_profile_is_a_refused_bake_sr100(tmp_path, profiles):
    proc, _, stub = _run(tmp_path, images=["sure-auroleap:local"],
                         present=[("sure-auroleap:local", "sha256:fork")],
                         env_text=ENV_TEXT.replace("COMPOSE_PROFILES=\n", "COMPOSE_PROFILES=%s\n" % profiles))
    assert proc.returncode != 0 and "SURE_ENABLED=true instead" in _text(proc)
    assert _compose_calls(stub) == [], "refused before resolving anything"


RESOLVER = ROOT / "scripts" / "ensure-local-images.sh"
RESOLVER_DOCKER = r"""#!/usr/bin/env bash
# `image inspect`: everything is present except the Sure fork.
if [ "$1" = image ] && [ "$2" = inspect ]; then
  case "$3" in sure-auroleap*) exit 1 ;; *) exit 0 ;; esac
fi
exit 0
"""


def _resolve(tmp_path, env_text):
    bash = _bash()
    bindir = tmp_path / "rbin"
    bindir.mkdir(exist_ok=True)
    _w(bindir / "docker", RESOLVER_DOCKER)
    (bindir / "docker").chmod(0o755)
    envfile = tmp_path / "renv"
    _w(envfile, env_text + "ACTUAL_IMAGE_TAG=26.1.0\n")
    env = dict(os.environ, PATH=f"{_px(bindir)}:{os.environ['PATH']}", ENV_FILE=_px(envfile))
    return subprocess.run([bash, _px(RESOLVER), "--dry-run"], env=env, capture_output=True, text=True)


def test_tc1362_the_resolver_builds_the_fork_only_when_sure_enabled_sr100(tmp_path):
    proc = _resolve(tmp_path, ENV_TEXT)
    assert proc.returncode == 0, _text(proc)
    assert "would build: sure-auroleap:local" in _text(proc)
    proc = _resolve(tmp_path, ENV_TEXT.replace("SURE_ENABLED=true\n", "SURE_ENABLED=false\n"))
    assert proc.returncode == 0, _text(proc)
    assert "would build: sure-auroleap" not in _text(proc)
    assert "SURE_ENABLED is not true" in _text(proc)


def test_tc1362_the_resolver_refuses_a_sure_profile_sr100(tmp_path):
    proc = _resolve(tmp_path, ENV_TEXT.replace("COMPOSE_PROFILES=\n", "COMPOSE_PROFILES=ntfy,sure\n"))
    assert proc.returncode != 0 and "set SURE_ENABLED=true" in _text(proc)


# ── firstboot: verify_loaded_image, extracted verbatim ───────────────────────

def _verify_fn():
    text = FIRSTBOOT.read_text(encoding="utf-8")
    start = text.index('IMAGE_LOAD_STATE="${IMAGE_LOAD_STATE:-')
    end = text.index("\n}\n", text.index("verify_loaded_image() {")) + 3
    return text[start:end]


def _rootfs_hash(image_id):
    return hashlib.sha256(f'["layer-of-{image_id}"]\n'.encode()).hexdigest()


def _verify(tmp_path, rows, present):
    bash = _bash()
    stub, env = _stub(tmp_path, present=present)
    images = tmp_path / "images"
    images.mkdir()
    _w(images / "images.manifest.tsv", "".join("\t".join(r) + "\n" for r in rows))
    state = tmp_path / "image-load.state"
    harness = tmp_path / "harness.sh"
    _w(harness, "log() { echo \"[fb] $*\"; }\n"
                f"IMAGES_DIR={_px(images)}\nIMAGE_LOAD_STATE={_px(state)}\n"
                + _verify_fn() + "verify_loaded_image sure.tar\necho BOOT-CONTINUED\n")
    proc = subprocess.run([bash, _px(harness)], env=env, capture_output=True, text=True)
    return proc, state, stub


def _row(ref, kind, rootfs, tar="sure.tar"):
    return [ref, "sha256:baked-id-differs-by-store", "(local)", tar, "1", kind] + ([rootfs] if rootfs else [])


def test_firstboot_verify_match_keeps_the_image(tmp_path):
    proc, state, stub = _verify(tmp_path, [_row("sure-auroleap:local", "local", _rootfs_hash("sha256:loaded"))],
                                [("sure-auroleap:local", "sha256:loaded")])
    assert proc.returncode == 0 and "BOOT-CONTINUED" in proc.stdout and "verified sure-auroleap:local" in proc.stdout
    assert not state.exists() and "rmi" not in (stub / "calls.log").read_text()


def test_firstboot_verify_mismatch_removes_tag_records_and_continues(tmp_path):
    proc, state, stub = _verify(tmp_path, [_row("sure-auroleap:local", "local", _rootfs_hash("sha256:other"))],
                                [("sure-auroleap:local", "sha256:loaded")])
    assert proc.returncode == 0 and "BOOT-CONTINUED" in proc.stdout  # the boot is NOT aborted
    assert "rmi sure-auroleap:local" in (stub / "calls.log").read_text()  # next boot retries the load
    assert "sure-auroleap:local\t" not in (stub / "present").read_text()
    line = state.read_text()
    assert line.startswith("FAILED ref=sure-auroleap:local") and "tar=sure.tar" in line


def test_firstboot_verify_absent_image_is_recorded(tmp_path):
    proc, state, _ = _verify(tmp_path, [_row("postgres:16.15", "pinned", _rootfs_hash("sha256:x"))], [])
    assert proc.returncode == 0 and "BOOT-CONTINUED" in proc.stdout
    assert "FAILED ref=postgres:16.15" in state.read_text() and "got_rootfs=absent" in state.read_text()


def test_firstboot_verify_ignores_registry_rows_and_old_manifests(tmp_path):
    rows = [_row("caddy:2", "registry", _rootfs_hash("sha256:nope")),
            _row("naglight:local", "local", "")]  # baked before the rootfs column existed
    proc, state, stub = _verify(tmp_path, rows, [("caddy:2", "sha256:loaded"), ("naglight:local", "sha256:loaded")])
    assert proc.returncode == 0 and "BOOT-CONTINUED" in proc.stdout
    assert "no rootfs hash recorded for naglight:local" in proc.stdout
    assert not state.exists() and "rmi" not in (stub / "calls.log").read_text()


def test_healthcheck_reports_the_firstboot_failure_marker():
    text = (ROOT / "stack" / "provision" / "healthcheck.sh").read_text(encoding="utf-8")
    assert "image-load.state" in text and 'bad "baked image failed verification' in text
    assert "pull_policy: never" in (ROOT / "stack" / "docker-compose.yml").read_text(encoding="utf-8")


# ── build recipe ─────────────────────────────────────────────────────────────

def _sure_checkout(tmp_path):
    """A throwaway ../sure with two branches, so ref selection is observable."""
    base = tmp_path / "sib"
    repo = base / "sure"
    repo.mkdir(parents=True)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(repo)]
    subprocess.run(git + ["init", "-q"], check=True)
    _w(repo / "Dockerfile", "FROM scratch\n")
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "init"], check=True)
    subprocess.run(git + ["branch", REF], check=True)
    subprocess.run(git + ["branch", "other"], check=True)
    sha = subprocess.run(git + ["rev-parse", REF], check=True, capture_output=True, text=True).stdout.strip()
    return base, sha


def _build(tmp_path, args=(), env_text=f"SURE_SOURCE_REF={REF}\nSURE_IMAGE=sure-auroleap:local\n", flags=(), extra_env=None):
    bash = _bash()
    base, sha = _sure_checkout(tmp_path)
    stub, env = _stub(tmp_path, flags=flags)
    envfile = tmp_path / "build.env"
    _w(envfile, env_text)
    env.update(SIBLING_ROOT=_px(base), ENV_FILE=_px(envfile))
    env.pop("SURE_SOURCE_REF", None)
    env.update(extra_env or {})
    proc = subprocess.run([bash, _px(BUILD), *args], env=env, capture_output=True, text=True)
    return proc, stub, sha


def test_build_ref_comes_from_the_knob_and_flag_overrides_it(tmp_path):
    proc, _, sha = _build(tmp_path, ["--dry-run"])
    assert proc.returncode == 0 and f"ref:    {REF}" in _text(proc) and sha in _text(proc)
    other = tmp_path / "second"
    other.mkdir()
    proc, _, _ = _build(other, ["--dry-run", "--ref", "other"])
    assert proc.returncode == 0 and "ref:    other" in _text(proc)


def test_build_refuses_without_a_ref_or_with_an_unknown_one(tmp_path):
    proc, _, _ = _build(tmp_path, ["--dry-run"], env_text="SURE_IMAGE=sure-auroleap:local\n")
    assert proc.returncode != 0 and "no source ref" in _text(proc)
    other = tmp_path / "second"
    other.mkdir()
    proc, _, _ = _build(other, ["--dry-run", "--ref", "nope"])
    assert proc.returncode != 0 and "does not resolve" in _text(proc)


def test_build_refuses_podman_and_never_builds(tmp_path):
    proc, stub, _ = _build(tmp_path, flags=["podman"])
    assert proc.returncode != 0 and "Podman" in _text(proc)
    assert not any(l.startswith("build") for l in (stub / "calls.log").read_text().splitlines())


def test_build_stamps_the_ref_and_commit(tmp_path):
    proc, stub, sha = _build(tmp_path)
    assert proc.returncode == 0, _text(proc)
    build = next(l for l in (stub / "calls.log").read_text().splitlines() if l.startswith("build"))
    assert f"homehub.source.revision={sha}" in build and f"homehub.source.ref={REF}" in build
    assert f"BUILD_COMMIT_SHA={sha}" in build and "-t sure-auroleap:local" in build


def test_build_script_has_no_hardcoded_fallback_branch():
    text = BUILD.read_text(encoding="utf-8")
    assert "auroleap-bank-stale-capability" not in text and "\r" not in text
    assert re.search(r"wsl -d Ubuntu", text)


# ── firstboot: compose failure after an image-verification failure ───────────

def _compose_harness(tmp_path, compose_rc, state_text):
    text = FIRSTBOOT.read_text(encoding="utf-8")
    a = text.index("_deferred_rc=0\nif [ \"$_compose_rc\" -ne 0 ]; then")
    marker = '        exit "$_compose_rc"\n    fi\nfi\n'
    step4a = text[a:text.index(marker, a) + len(marker)]
    b = text.index('if [ "$_deferred_rc" -ne 0 ]; then')
    end_marker = 'exit "$_deferred_rc"\nfi\n'
    step7 = text[b:text.index(end_marker, b) + len(end_marker)]
    state = tmp_path / "image-load.state"
    if state_text is not None:
        _w(state, state_text)
    harness = tmp_path / "fb.sh"
    _w(harness, "log() { echo \"[fb] $*\"; }\n"
                f"IMAGE_LOAD_STATE={_px(state)}\n_compose_rc={compose_rc}\n"
                + step4a + "echo LATER-STEPS-REACHED\n" + step7 + "echo CLEAN-END\n")
    return subprocess.run([_bash(), _px(harness)], capture_output=True, text=True)


def test_compose_failure_after_image_failure_defers_and_still_ends_red(tmp_path):
    proc = _compose_harness(tmp_path, 3, "FAILED ref=sure-auroleap:local tar=s.tar want_rootfs=a got_rootfs=b\n")
    assert "ERROR" in proc.stdout and "sure-auroleap:local" in proc.stdout  # names the image
    assert "LATER-STEPS-REACHED" in proc.stdout  # DNS/Samba/recovery steps still run
    assert proc.returncode == 3 and "CLEAN-END" not in proc.stdout  # and the unit still ends red


def test_compose_failure_without_image_failure_is_still_immediately_fatal(tmp_path):
    for state in (None, ""):
        sub = tmp_path / f"s{state is None}"
        sub.mkdir()
        proc = _compose_harness(sub, 3, state)
        assert proc.returncode == 3 and "FATAL: 'docker compose up -d' exited 3" in proc.stdout
        assert "LATER-STEPS-REACHED" not in proc.stdout


def test_compose_success_with_a_stale_state_file_is_clean(tmp_path):
    proc = _compose_harness(tmp_path, 0, "FAILED ref=x\n")
    assert proc.returncode == 0 and "CLEAN-END" in proc.stdout


# ── exporter: never record the empty-string rootfs hash ──────────────────────

def test_unreadable_layer_list_refuses_instead_of_recording_the_empty_hash(tmp_path):
    bash = _bash()
    stub, env = _stub(tmp_path, present=[("sure-auroleap:local", "sha256:fork")],
                      labels=[("sure-auroleap:local", REF)], rootfs=[("sure-auroleap:local", "")],
                      images=["sure-auroleap:local"])
    envfile = tmp_path / "env"
    _w(envfile, ENV_TEXT)
    env.update(IMAGES_OUT=_px(tmp_path / "out"), ALLOW_STALE_LOCAL="1")
    proc = subprocess.run([bash, _px(EXPORT), "--env-file", _px(envfile)], env=env, capture_output=True, text=True)
    assert proc.returncode != 0 and "empty rootfs hash" in _text(proc)
    assert "e3b0c44298fc1c14" not in (tmp_path / "out" / "images.manifest.tsv").read_text()
