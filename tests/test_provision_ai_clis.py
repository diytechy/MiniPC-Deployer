"""TC-1193..TC-1204 - the AI CLI provisioner (SR-076, SR-077, SR-078; LLR-1104..LLR-1112; IF-038).

Everything runs the REAL stack/ai-cli/provision-ai-clis.sh in a throwaway tree through its
documented seams. The fakes stand where the machine stands: `curl` serves fixture archives and
counts calls, `runuser` runs the command and can be told to deny a path (the service account
being unable to read it), `systemctl` records what it was asked, `chown` records what it was
asked. The assertions are made against the OTHER side of each contract - the bytes on disk, the
links in the bin directory, the calls the fakes saw, state.json re-read from disk - never against
a variable the script computed.

The script needs Linux semantics (bash 4, flock, timeout, GNU tar, python3), so the whole module
skips VISIBLY elsewhere; run it under `wsl -d Ubuntu`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CLI_DIR = REPO / "stack" / "ai-cli"
SCRIPT = CLI_DIR / "provision-ai-clis.sh"
REAL_MANIFEST = CLI_DIR / "ai-clis.manifest.tsv"

_NEEDED_TOOLS = ("bash", "flock", "timeout", "tar", "python3", "sha256sum", "df")
_MISSING = [t for t in _NEEDED_TOOLS if shutil.which(t) is None]
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or bool(_MISSING),
    reason="needs Linux with %s (run under: wsl -d Ubuntu)" % ", ".join(_NEEDED_TOOLS),
)

COLUMNS = ("key version needed_by url sha256 size_bytes kind tree inventory entry "
           "version_cmd version_regex target requires").split()
VREGEX = r"[0-9]+\.[0-9]+\.[0-9]+"


# ------------------------------------------------------------------ archive fixtures
def _script(text: str) -> bytes:
    return ("#!/bin/sh\n" + text + "\n").encode()


def _tar_bytes(members, compress="gz") -> bytes:
    """members: name -> bytes | (bytes, mode) | ("symlink", target) | ("hardlink", target)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:" + compress) as t:
        for name, spec in members.items():
            info = tarfile.TarInfo(name)
            if isinstance(spec, tuple) and spec[0] in ("symlink", "hardlink"):
                info.type = tarfile.SYMTYPE if spec[0] == "symlink" else tarfile.LNKTYPE
                info.linkname = spec[1]
                info.mode = 0o777
                t.addfile(info)
                continue
            data, mode = spec if isinstance(spec, tuple) else (spec, 0o755)
            info.size = len(data)
            info.mode = mode
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _zip_bytes(members) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            zi = zipfile.ZipInfo(name)
            zi.external_attr = 0o755 << 16
            z.writestr(zi, data)
    return buf.getvalue()


class Lab:
    """One isolated install tree plus the fakes, with the seams wired."""

    def __init__(self, tmp: Path):
        self.root = tmp
        self.ai = tmp / "ai-cli"
        self.prefix = tmp / "prefix"
        self.state_dir = tmp / "state"
        self.bin = tmp / "bin"
        self.vt = tmp / "vartmp"
        self.units = tmp / "units"
        self.fakes = tmp / "fakes"
        self.work = tmp / "work"
        self.logs = tmp / "logs"
        self.lock = tmp / "lock" / "ai-clis.lock"
        self.env_file = tmp / "dot-env"
        self.manifest = tmp / "manifest.tsv"
        for d in (self.ai, self.bin, self.vt, self.units, self.fakes, self.work, self.logs, self.lock.parent):
            d.mkdir(parents=True, exist_ok=True)
        for name in ("provision-ai-clis.sh", "wrapper.template", "homehub-ai-clis-provision.service",
                     "homehub-ai-clis-provision.timer", "agents.registry.csv", "routes-enabled"):
            shutil.copy(CLI_DIR / name, self.ai / name)
        self.script = self.ai / "provision-ai-clis.sh"
        self.rows: dict[str, dict] = {}
        self.urlmap: dict[str, Path] = {}
        self.extra_env: dict[str, str] = {}
        self.deny: list[str] = []
        self._fakes()
        self.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="claude,codex,opencode,gemini")

    # ---- fakes
    def _fakes(self):
        def w(name, body):
            p = self.fakes / name
            p.write_text("#!/bin/bash\n" + body)
            p.chmod(0o755)
        w("curl", r'''
echo "$*" >> "$FAKE_LOG_DIR/curl.log"
{ echo "== $*"; ls -1 "$AI_CLIS_PREFIX/.staging" "$AI_CLIS_VAR_TMP/homehub-ai-clis" 2>/dev/null; } >> "$FAKE_LOG_DIR/curl-seen.log"
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    --output) out=$2; shift 2;;
    --connect-timeout|--max-time|--proto|--proto-redir) shift 2;;
    -*) shift;;
    *) url=$1; shift;;
  esac
done
[ -n "${FAKE_CURL_SLEEP:-}" ] && sleep "$FAKE_CURL_SLEEP"
src=$(awk -F'\t' -v u="$url" '$1==u{print $2}' "$FAKE_LOG_DIR/map.tsv")
[ -n "$src" ] || exit 22
cp "$src" "$out"
''')
        w("runuser", r'''
acct=$2; shift 2; [ "$1" = "--" ] && shift
echo "$acct" >> "$FAKE_LOG_DIR/runuser-accounts.log"
args=("$@"); i=0
[ "${args[$i]}" = env ] && i=$((i+1))
while [[ "${args[$i]}" == -i || "${args[$i]}" == *=* ]]; do i=$((i+1)); done
path="${args[$i]}"
echo "$acct $path" >> "$FAKE_LOG_DIR/runuser.log"
if [ -f "$FAKE_LOG_DIR/deny.txt" ]; then
  while IFS= read -r d; do
    [ -n "$d" ] || continue
    case "$path" in "$d"*) echo "Permission denied: $path" >&2; exit 126;; esac
  done < "$FAKE_LOG_DIR/deny.txt"
fi
[ -n "${FAKE_RUNUSER_SLEEP:-}" ] && sleep "$FAKE_RUNUSER_SLEEP"
exec "$@"
''')
        w("systemctl", 'echo "$*" >> "$FAKE_LOG_DIR/systemctl.log"\n')
        w("chown", 'echo "$*" >> "$FAKE_LOG_DIR/chown.log"\nexit 0\n')

    # ---- configuration
    def set_env(self, **kv):
        lines = [f"{k}={v}" for k, v in kv.items()]
        self.env_file.write_text("\n".join(lines) + "\n")

    def set_deny(self, *paths):
        (self.logs / "deny.txt").write_text("\n".join(str(p) for p in paths) + "\n")

    # ---- artifacts and manifest
    def artifact(self, key, version, *, kind, data: bytes, needed_by, tree="whole", inventory=None,
                 entry=None, requires="-", url=None):
        fname = f"{key}-{version}.bin"
        path = self.work / fname
        path.write_bytes(data)
        url = url or f"https://dl.test/{key}/{version}/{fname}"
        self.urlmap[url] = path
        self.rows[key] = dict(
            key=key, version=version, needed_by=needed_by, url=url,
            sha256=hashlib.sha256(data).hexdigest(), size_bytes=str(len(data)), kind=kind, tree=tree,
            inventory=inventory or entry or key, entry=entry or key, version_cmd="--version",
            version_regex=VREGEX, target="linux-x86_64-test", requires=requires)
        self.write_manifest()

    def write_manifest(self):
        lines = ["# test manifest"]
        for row in self.rows.values():
            lines.append("\t".join(row[c] for c in COLUMNS))
        self.manifest.write_text("\n".join(lines) + "\n")
        (self.logs / "map.tsv").write_text("".join(f"{u}\t{p}\n" for u, p in self.urlmap.items()))

    def claude(self, version="1.0.0", **kw):
        self.artifact("claude", version, kind="file", needed_by="source:claude,source:claude2,service:ai-cli",
                      data=_script(f'echo "{version} (Claude Code)"'), **kw)

    def codex(self, version="0.1.0", extra=None, mode=None):
        members = {"bin/codex": _script(f'echo "codex-cli {version}"'), "bin/helper": _script("echo helper"),
                   "codex-package.json": (b"{}", 0o644)}
        members.update(extra or {})
        self.artifact("codex", version, kind="tar", needed_by="source:codex,service:ai-cli:openai",
                      data=_tar_bytes(members), inventory="bin/codex bin/helper codex-package.json", entry="bin/codex")

    def opencode(self, version="3.0.0"):
        data = _tar_bytes({"opencode": _script(f'echo "{version}"'), "junk": b"not extracted"})
        self.artifact("opencode", version, kind="tar", tree="member", needed_by="source:opencode",
                      data=data, inventory="opencode", entry="opencode")

    def node(self, version="22.0.0"):
        top = f"node-v{version}-linux-x64"
        body = ('case "$1" in --version) echo "v%s";; *) f="$1"; shift; exec sh "$f" "$@";; esac' % version)
        data = _tar_bytes({f"{top}/bin/node": _script(body), f"{top}/lib/npm-cli.js": (b"x", 0o644),
                           f"{top}/bin/npm": ("symlink", "../lib/npm-cli.js")}, compress="xz")
        self.artifact("node", version, kind="nodetar", needed_by="required-by:gemini", data=data,
                      inventory="bin/node", entry="bin/node")

    def gemini(self, version="0.5.0", requires="node", chunks=("chunk-A.js",)):
        js = ": <<'EOF'\n" + "".join(f'import "./{c}";\n' for c in chunks) + f"EOF\necho \"{version}\"\n"
        members = {"gemini.js": js}
        for c in chunks:
            members[c] = "// chunk\n"
        self.artifact("gemini", version, kind="zip", needed_by="source:gemini", data=_zip_bytes(members),
                      inventory="gemini.js", entry="gemini.js", requires=requires)

    def standard(self):
        self.claude(); self.codex(); self.opencode(); self.node(); self.gemini()

    # ---- running
    def env(self, **extra):
        e = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TERM")}
        e.update(
            HOME=str(self.root), AI_CLIS_PREFIX=str(self.prefix), AI_CLIS_STATE_DIR=str(self.state_dir),
            AI_CLIS_BIN_DIR=str(self.bin), AI_CLIS_LOCK=str(self.lock), AI_CLIS_MANIFEST=str(self.manifest),
            AI_CLIS_ENV_FILE=str(self.env_file), AI_CLIS_CURL=str(self.fakes / "curl"),
            AI_CLIS_RUNUSER=str(self.fakes / "runuser"), AI_CLIS_SYSTEMCTL=str(self.fakes / "systemctl"),
            AI_CLIS_CHOWN=str(self.fakes / "chown"), AI_CLIS_UNIT_DIR=str(self.units),
            AI_CLIS_VAR_TMP=str(self.vt), FAKE_LOG_DIR=str(self.logs))
        e.update(self.extra_env)
        e.update(extra)
        return e

    def run(self, *args, script=None, timeout=120, **env):
        return subprocess.run(["bash", str(script or self.script), *args], env=self.env(**env),
                              capture_output=True, text=True, timeout=timeout)

    def popen(self, *args, **env):
        return subprocess.Popen(["bash", str(self.script), *args], env=self.env(**env), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, start_new_session=True)

    # ---- observation
    def log(self, name) -> list[str]:
        p = self.logs / name
        return p.read_text().splitlines() if p.exists() else []

    def curl_urls(self):
        return [l.split()[-1] for l in self.log("curl.log")]

    def state(self):
        p = self.state_dir / "state.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def link(self, name):
        p = self.bin / name
        return os.readlink(p) if p.is_symlink() else None

    def wrapper(self, key):
        d = sorted((self.prefix / key).iterdir())
        assert len(d) >= 1
        return d

    def snapshot(self, *roots):
        snap = {}
        for r in roots or (self.prefix, self.state_dir, self.bin, self.vt, self.units):
            if not r.exists():
                snap[str(r)] = None
                continue
            for p in [r, *sorted(r.rglob("*"))]:
                st = p.lstat()
                snap[str(p)] = (st.st_ino, st.st_mtime_ns, st.st_size, st.st_mode)
        return snap

    def leftovers(self):
        staging = self.prefix / ".staging"
        left = list(staging.iterdir()) if staging.exists() else []
        dl = self.vt / "homehub-ai-clis"
        left += list(dl.iterdir()) if dl.exists() else []
        left += [p for p in self.vt.iterdir() if p.name.startswith("ai-clis-home")]
        return left


@pytest.fixture
def lab(tmp_path):
    return Lab(tmp_path)


def out(cp):
    return cp.stdout + cp.stderr


# ============================================================ TC-1193 manifest and identity (LLR-1104)
def _real_rows():
    rows = []
    for line in REAL_MANIFEST.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        f = line.split("\t")
        assert len(f) == len(COLUMNS), line[:60]
        rows.append(dict(zip(COLUMNS, f)))
    return rows


def _feeder_sources():
    spec = importlib.util.spec_from_file_location("ai_usage_feeder_prov", REPO / "stack" / "ai-usage" / "ai_usage_feeder.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ai_usage_feeder_prov"] = mod
    spec.loader.exec_module(mod)
    return set(mod.SOURCE_READERS)


def test_real_manifest_rows_are_self_consistent_and_match_the_feeder():
    rows = _real_rows()
    assert [r["key"] for r in rows] == ["claude", "codex", "opencode", "gemini", "node"]
    sources = _feeder_sources()
    text = REAL_MANIFEST.read_text()
    assert "http://" not in text
    for r in rows:
        assert r["url"].startswith("https://")
        assert re.fullmatch(r"[0-9a-f]{64}", r["sha256"]) and r["size_bytes"].isdigit()
        assert r["version"] in r["url"], r["key"]
        assert re.fullmatch(r["version_regex"], r["version"]), r["key"]
        assert r["kind"] in ("file", "tar", "zip", "nodetar") and r["tree"] in ("whole", "member")
        for tok in r["needed_by"].split(","):
            if tok.startswith("source:"):
                assert tok[len("source:"):] in sources, f"{r['key']} names a source the feeder lacks: {tok}"
            else:
                assert tok in ("service:ai-cli", "service:ai-cli:openai") or tok.startswith("required-by:")
    by = {r["key"]: r for r in rows}
    assert by["codex"]["tree"] == "whole" and set(by["codex"]["inventory"].split()) == {
        "bin/codex", "bin/codex-code-mode-host", "codex-package.json", "codex-path/rg",
        "codex-resources/bwrap", "codex-resources/zsh/bin/zsh"}
    assert by["gemini"]["tree"] == "whole" and by["gemini"]["requires"] == "node"
    assert by["gemini"]["entry"] == "gemini.js"
    assert "baseline" in by["opencode"]["url"]
    assert by["node"]["needed_by"] == "required-by:gemini"


def test_node_is_never_linked_into_the_bin_directory(lab):
    lab.standard()
    cp = lab.run("--run")
    assert cp.returncode == 0, out(cp)
    assert sorted(p.name for p in lab.bin.iterdir()) == ["claude", "codex", "gemini", "opencode"]
    for p in lab.bin.iterdir():
        assert "/node/" not in os.readlink(p)
    gem = (lab.wrapper("gemini")[0] / "wrapper" / "gemini").read_text()
    assert str(lab.prefix / "node") in gem and "/opt/node" not in gem
    # the pinned runtime is root-prefix only: nothing under /opt/node is referenced by any wrapper
    for k in ("claude", "codex", "opencode", "gemini"):
        assert "/opt/node" not in (lab.wrapper(k)[0] / "wrapper" / k).read_text()


def test_identity_has_version_sha_wrapper_rev_and_dependency_pin(lab):
    lab.standard()
    assert lab.run("--run").returncode == 0
    claude_id = lab.wrapper("claude")[0].name
    sha = lab.rows["claude"]["sha256"][:12]
    assert claude_id == f"1.0.0-{sha}-w1"
    gem_id = lab.wrapper("gemini")[0].name
    assert gem_id == f"0.5.0-{lab.rows['gemini']['sha256'][:12]}-w1-d22.0.0-{lab.rows['node']['sha256'][:12]}"
    assert (lab.prefix / "claude" / claude_id / ".complete").read_text() == claude_id


def test_changed_node_pin_gives_a_new_gemini_identity_and_a_reinstall(lab):
    lab.standard()
    assert lab.run("--run").returncode == 0
    old = lab.wrapper("gemini")[0].name
    old_link = lab.link("gemini")
    lab.node("22.1.0")
    (lab.logs / "curl.log").unlink()
    cp = lab.run("--run")
    assert cp.returncode == 0, out(cp)
    new = [p.name for p in lab.wrapper("gemini")]
    assert len(new) == 2 and old in new                      # the old directory is left, not reused
    assert lab.link("gemini") != old_link and Path(lab.link("gemini")).parts[-3] != old
    urls = lab.curl_urls()
    assert lab.rows["node"]["url"] in urls and lab.rows["gemini"]["url"] in urls
    assert lab.rows["claude"]["url"] not in urls


def test_changed_wrapper_rev_gives_a_new_identity_and_a_reinstall(lab):
    lab.claude()
    assert lab.run("--run").returncode == 0
    old = lab.wrapper("claude")[0].name
    patched = lab.ai / "provision-bumped.sh"
    patched.write_text(re.sub(r"^WRAPPER_REV=1$", "WRAPPER_REV=2", SCRIPT.read_text(), flags=re.M))
    assert "WRAPPER_REV=2" in patched.read_text()
    cp = lab.run("--run", script=patched)
    assert cp.returncode == 0, out(cp)
    names = sorted(p.name for p in lab.wrapper("claude"))
    assert len(names) == 2 and old in names
    assert Path(lab.link("claude")).parts[-3].endswith("-w2")


def test_an_untrusted_directory_at_the_final_path_is_replaced_not_reused(lab):
    lab.claude()
    assert lab.run("--run").returncode == 0
    ident = lab.wrapper("claude")[0]
    # simulate a half-written or foreign directory at the pinned identity: no matching .complete
    (ident / ".complete").write_text("some-other-identity")
    (ident / "evil").write_text("x")
    os.unlink(lab.bin / "claude")
    (lab.state_dir / "state.json").unlink()
    cp = lab.run("--run")
    assert cp.returncode == 0, out(cp)
    again = lab.wrapper("claude")[0]
    assert not (again / "evil").exists()
    assert (again / ".complete").read_text() == again.name
    assert lab.link("claude")


# ===================================================== TC-1194 no-op re-run (LLR-1112)
def test_converged_rerun_makes_no_network_call_and_no_write(lab):
    lab.standard()
    assert lab.run("--run").returncode == 0
    (lab.logs / "curl.log").unlink()
    before = lab.snapshot()
    time.sleep(0.05)
    cp = lab.run("--run")
    assert cp.returncode == 0, out(cp)
    assert lab.curl_urls() == []
    assert lab.snapshot() == before        # inode, mtime, size and mode of everything, state.json included
    assert "PROVISIONING: complete" in out(cp)


def test_a_pending_gemini_does_not_redownload_completed_clis(lab):
    lab.standard()
    lab.rows["gemini"]["sha256"] = "0" * 64     # gemini will fail its checksum forever
    lab.write_manifest()
    cp = lab.run("--run")
    assert cp.returncode == 3, out(cp)
    (lab.logs / "curl.log").unlink()
    cp = lab.run("--run")
    assert cp.returncode == 3
    urls = lab.curl_urls()
    assert urls == [lab.rows["gemini"]["url"]]      # node is complete, claude/codex/opencode are complete


# ============================================ TC-1195 preservation and cleanup (LLR-1105, LLR-1111)
def _hand_link(lab, name="claude", version="0.9.0"):
    hand = lab.root / "hand" / name
    hand.parent.mkdir(exist_ok=True)
    hand.write_text(_script(f'echo "{version} (Claude Code)"').decode())
    hand.chmod(0o755)
    os.symlink(hand, lab.bin / name)
    return hand


def _break(lab, kind):
    """Make claude/codex fail in one named way. Returns the key under test."""
    if kind == "download":
        lab.urlmap.clear(); lab.write_manifest(); return "claude"
    if kind == "sha":
        lab.rows["claude"]["sha256"] = "1" * 64; lab.write_manifest(); return "claude"
    if kind == "size":
        lab.rows["claude"]["size_bytes"] = "5"; lab.write_manifest(); return "claude"
    if kind == "version":
        lab.rows["claude"]["version"] = "9.9.9"; lab.write_manifest(); return "claude"
    if kind == "inventory":
        lab.rows["codex"]["inventory"] += " bin/not-there"; lab.write_manifest(); return "codex"
    if kind == "low-disk":
        lab.extra_env["AI_CLIS_RESERVE_MB"] = "999999999"; return "claude"
    if kind.startswith("hostile-"):
        evil = {"hostile-absolute": {"/evil-abs": b"x"},
                "hostile-dotdot": {"../evil-dd": b"x"},
                "hostile-symlink-abs": {"bin/x": ("symlink", "/etc/passwd")},
                "hostile-symlink-up": {"bin/y": ("symlink", "../../outside")},
                "hostile-hardlink": {"bin/z": ("hardlink", "/etc/passwd")}}[kind]
        lab.codex(extra=evil)
        return "codex"
    raise AssertionError(kind)


FAILURES = ["download", "sha", "size", "version", "inventory", "low-disk", "hostile-absolute", "hostile-dotdot",
            "hostile-symlink-abs", "hostile-symlink-up", "hostile-hardlink"]


@pytest.mark.parametrize("kind", FAILURES)
def test_every_failure_leaves_the_hand_installed_link_and_no_staging_or_download(lab, kind):
    lab.claude(); lab.codex()
    hands = {"claude": _hand_link(lab, "claude"), "codex": _hand_link(lab, "codex", "0.0.1")}
    key = _break(lab, kind)
    lab.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES=key)       # only the broken CLI is needed
    unrelated = lab.root / "unrelated"
    unrelated.write_text("x")
    os.symlink(unrelated, lab.bin / "unrelated")
    bin_before = sorted((p.name, os.readlink(p)) for p in lab.bin.iterdir())
    cp = lab.run("--run")
    assert cp.returncode == 3, out(cp)
    assert sorted((p.name, os.readlink(p)) for p in lab.bin.iterdir()) == bin_before
    assert lab.link(key) == str(hands[key]) and hands[key].read_text().startswith("#!/bin/sh")
    assert lab.leftovers() == [], lab.leftovers()
    assert not list(lab.root.rglob("evil*"))
    reason = lab.state()["clis"][key]["pending"]["reason"]
    assert reason
    if kind.startswith("hostile-"):
        assert reason.startswith("archive refused:"), reason     # refused by the listing, not by tar failing later
    assert not re.search(r"missing package|not installed|command not found", reason, re.I)
    assert "PROVISIONING: INCOMPLETE" in out(cp)


def test_failed_first_download_changes_nothing_when_nothing_is_installed(lab):
    lab.claude()
    lab.urlmap.clear(); lab.write_manifest()
    cp = lab.run("--run")
    assert cp.returncode == 3
    assert list(lab.bin.iterdir()) == []
    assert lab.leftovers() == []


def test_post_swap_check_failure_restores_the_previous_link(lab):
    lab.claude()
    hand = _hand_link(lab)
    lab.set_deny(lab.bin / "claude")
    cp = lab.run("--run")
    assert cp.returncode == 3, out(cp)
    assert lab.link("claude") == str(hand)
    st = lab.state()["clis"]["claude"]
    assert "activation" not in st or st["activation"].get("state") != "committed"
    assert "previous link was restored" in st["pending"]["reason"]
    assert lab.leftovers() == []


# ============================================ TC-1196 transaction kill points (LLR-1107)
@pytest.mark.parametrize("point", ["pending", "swap", "check", "checked"])
def test_killed_at_every_point_the_next_run_completes_the_check_and_commits(lab, point):
    lab.claude()
    hand = _hand_link(lab)
    cp = lab.run("--run", AI_CLIS_DIE_AT=point)
    assert cp.returncode == -9, out(cp)
    rec = lab.state()["clis"]["claude"]["activation"]
    assert rec["state"] in ("pending", "checked") and rec["from"] == str(hand) and rec["from_version"] == "0.9.0"
    cp = lab.run("--run")
    assert cp.returncode == 0, out(cp)
    rec = lab.state()["clis"]["claude"]["activation"]
    assert rec["state"] == "committed" and rec["to_version"] == "1.0.0"
    assert lab.link("claude") == rec["to"] and "/prefix/claude/" in rec["to"]


@pytest.mark.parametrize("point", ["swap", "check", "checked"])
def test_recovery_restores_from_when_the_account_cannot_run_the_new_link(lab, point):
    lab.claude()
    hand = _hand_link(lab)
    assert lab.run("--run", AI_CLIS_DIE_AT=point).returncode == -9
    assert lab.link("claude") != str(hand)
    lab.set_deny(lab.bin / "claude")
    cp = lab.run("--run")
    assert cp.returncode == 3, out(cp)
    assert lab.link("claude") == str(hand)


def test_an_unrecorded_link_is_never_altered_by_recovery_or_a_run(lab):
    lab.claude(); lab.codex()
    lab.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="claude")      # codex is not needed here
    hand_codex = _hand_link(lab, "codex", "0.0.1")
    other = lab.root / "elsewhere"
    other.write_text("x")
    os.symlink(other, lab.bin / "somethingelse")
    assert lab.run("--run", AI_CLIS_DIE_AT="swap").returncode == -9
    cp = lab.run("--run")
    assert cp.returncode == 0, out(cp)
    assert lab.link("codex") == str(hand_codex) and lab.link("somethingelse") == str(other)


def test_recovery_commits_an_interrupted_activation_even_when_the_cli_is_no_longer_needed(lab):
    lab.claude(); lab.codex()
    lab.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="claude")
    assert lab.run("--run", AI_CLIS_DIE_AT="check").returncode == -9
    assert lab.state()["clis"]["claude"]["activation"]["state"] == "pending"
    lab.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="codex")           # claude is dropped from the config
    cp = lab.run("--run")
    assert cp.returncode == 0, out(cp)
    assert lab.state()["clis"]["claude"]["activation"]["state"] == "committed"
    assert lab.link("claude")                                                 # an installed CLI is never removed


def test_a_failed_artifact_is_deleted_at_once_not_at_the_end_of_the_run(lab):
    lab.claude(); lab.codex()
    lab.rows["claude"]["sha256"] = "5" * 64; lab.write_manifest()
    lab.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="claude,codex")
    cp = lab.run("--run")
    assert cp.returncode == 3, out(cp)
    seen = [c for c in (lab.logs / "curl-seen.log").read_text().split("== ") if c.strip()]
    codex_call = next(c for c in seen if "codex" in c.splitlines()[0])
    assert "claude." not in codex_call, codex_call          # claude's staging tree and download were gone before codex began


def _gemini_zip(lab, js_lines, files):
    nl = chr(10)
    js = ": <<'EOF'" + nl + "".join(l + nl for l in js_lines) + "EOF" + nl + 'echo "0.5.0"' + nl
    members = {"gemini.js": js}
    members.update({f: "//" + nl for f in files})
    lab.artifact("gemini", "0.5.0", kind="zip", needed_by="source:gemini", data=_zip_bytes(members),
                 inventory="gemini.js", entry="gemini.js", requires="node")


def test_a_gemini_whose_entry_imports_an_absent_chunk_is_refused(lab):
    lab.node()
    _gemini_zip(lab, ['import "./chunk-A.js";', 'import "./chunk-GONE.js";'], ["chunk-A.js"])
    lab.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="gemini")
    cp = lab.run("--run")
    assert cp.returncode == 3, out(cp)
    reason = lab.state()["clis"]["gemini"]["pending"]["reason"]
    assert reason.startswith("incomplete archive:") and "chunk-GONE.js" in reason
    # a mention inside a comment is not an import
    _gemini_zip(lab, ['// import "./chunk-NOPE.js"', 'import "./chunk-A.js";'], ["chunk-A.js"])
    cp = lab.run("--run")
    assert cp.returncode == 0, out(cp)


# =============================================== TC-1197 consumer check as the account (LLR-1108)
def test_every_check_runs_as_the_account_in_a_fresh_home_with_the_units_path(lab):
    lab.claude()
    lab.set_env(AI_CLI_ENABLED="true", AI_CLI_USER="alice")
    assert lab.run("--run").returncode == 0
    assert lab.log("runuser-accounts.log") and set(lab.log("runuser-accounts.log")) == {"alice"}
    # both the staging probe and the post-swap probe went through runuser; the second via the bin link
    paths = [l.split(" ", 1)[1] for l in lab.log("runuser.log")]
    assert any("/.staging/" in p for p in paths) and str(lab.bin / "claude") in paths
    assert not list(lab.vt.glob("ai-clis-home*"))


def test_a_wrapper_the_account_cannot_reach_fails_the_staging_check(lab):
    lab.claude()
    lab.set_deny(lab.prefix)       # the account may not enter the prefix at all
    cp = lab.run("--run")
    assert cp.returncode == 3, out(cp)
    assert lab.link("claude") is None and lab.leftovers() == []
    assert not list((lab.prefix / "claude").glob("*")) if (lab.prefix / "claude").exists() else True


def test_a_hung_cli_is_bounded_by_the_check_timeout(lab):
    lab.claude()
    t0 = time.time()
    cp = lab.run("--run", FAKE_RUNUSER_SLEEP="30", AI_CLIS_CHECK_TIMEOUT="1")
    assert cp.returncode == 3, out(cp)
    assert time.time() - t0 < 15
    assert lab.link("claude") is None and lab.leftovers() == []
    assert re.search(r"timeout 20|\$\{?AI_CLIS_CHECK_TIMEOUT:-20\}?", SCRIPT.read_text())


# ================================================== TC-1198 rollback (LLR-1109)
def _install_a_then_b(lab, hand=True):
    lab.claude("1.0.0")
    h = _hand_link(lab) if hand else None
    assert lab.run("--run").returncode == 0
    a_link = lab.link("claude")
    lab.claude("2.0.0")
    assert lab.run("--run").returncode == 0
    b_link = lab.link("claude")
    assert a_link != b_link
    return h, a_link, b_link


def test_rollback_validates_the_previous_version_not_the_current_one(lab):
    _, a_link, b_link = _install_a_then_b(lab, hand=False)
    rec = lab.state()["clis"]["claude"]["activation"]
    assert rec["from"] == a_link and rec["from_version"] == "1.0.0" and rec["to_version"] == "2.0.0"
    # the wrapper of identity A still runs A after B is installed
    assert subprocess.run([a_link], capture_output=True, text=True).stdout.strip() == "1.0.0 (Claude Code)"
    cp = lab.run("--rollback", "claude")
    assert cp.returncode == 0, out(cp)
    assert lab.link("claude") == a_link
    rec = lab.state()["clis"]["claude"]["activation"]
    assert rec["state"] == "committed" and rec["to_version"] == "1.0.0" and rec["from"] == b_link


def test_a_failing_rollback_check_restores_the_current_install(lab):
    _, a_link, b_link = _install_a_then_b(lab, hand=False)
    lab.set_deny(lab.bin / "claude")
    cp = lab.run("--rollback", "claude")
    assert cp.returncode == 3, out(cp)
    assert lab.link("claude") == b_link
    assert lab.state()["clis"]["claude"]["activation"]["to"] == b_link


def test_rollback_of_a_first_install_removes_the_managed_link_successfully(lab):
    lab.claude(); lab.codex()
    assert lab.run("--run").returncode == 0
    codex_link = lab.link("codex")
    rec = lab.state()["clis"]["claude"]["activation"]
    assert rec["from"] is None and rec["from_version"] is None
    cp = lab.run("--rollback", "claude")
    assert cp.returncode == 0, out(cp)
    assert lab.link("claude") is None and lab.link("codex") == codex_link
    assert (lab.bin / "claude").exists() is False


def test_rollback_to_a_hand_installed_predecessor_validates_its_observed_version(lab):
    hand, a_link, _ = _install_a_then_b(lab, hand=True)
    # A was activated over the hand-installed 0.9.0: that version was OBSERVED before the swap
    rec = lab.state()["clis"]["claude"]["activation"]
    assert rec["from"] == a_link and rec["from_version"] == "1.0.0"
    assert lab.run("--rollback", "claude").returncode == 0           # B -> A
    assert lab.run("--rollback", "claude").returncode == 0           # A -> B (roll forward)
    assert lab.link("claude") != a_link


def test_hand_installed_version_is_observed_and_rollback_restores_it(lab):
    lab.claude("1.0.0")
    hand = _hand_link(lab)
    assert lab.run("--run").returncode == 0
    rec = lab.state()["clis"]["claude"]["activation"]
    assert rec["from"] == str(hand) and rec["from_version"] == "0.9.0"
    assert lab.run("--rollback", "claude").returncode == 0
    assert lab.link("claude") == str(hand)
    # a held rollback is not undone by the next converge
    cp = lab.run("--run")
    assert cp.returncode == 0, out(cp)
    assert lab.link("claude") == str(hand)


def test_rollback_without_a_record_or_for_an_unknown_key_is_refused(lab):
    lab.claude()
    assert lab.run("--rollback", "claude").returncode == 2
    assert lab.run("--rollback", "nonesuch").returncode == 2
    assert lab.run("--rollback").returncode == 2


# ======================================================= TC-1199 disk (LLR-1111)
def _tmpfs(path: Path, mb=16):
    path.mkdir(parents=True, exist_ok=True)
    cp = subprocess.run(["mount", "-t", "tmpfs", "-o", f"size={mb}m", "tmpfs", str(path)], capture_output=True)
    if cp.returncode != 0:
        pytest.skip("cannot mount a tmpfs here (needs root): " + cp.stderr.decode()[:80])


def test_repeated_persistent_failures_do_not_consume_space(lab):
    small = lab.root / "small"
    _tmpfs(small)
    try:
        lab.prefix = small / "prefix"
        lab.vt = small / "vartmp"
        lab.vt.mkdir()
        lab.claude(); lab.rows["claude"]["sha256"] = "2" * 64; lab.write_manifest()
        lab.extra_env["AI_CLIS_RESERVE_MB"] = "0"
        lab.run("--run")
        st = os.statvfs(small)
        baseline = st.f_bavail * st.f_frsize
        for _ in range(5):
            assert lab.run("--run").returncode == 3
        st = os.statvfs(small)
        assert st.f_bavail * st.f_frsize == baseline
        assert lab.leftovers() == []
    finally:
        subprocess.run(["umount", str(small)], capture_output=True)


def test_low_free_space_blocks_the_download_entirely(lab):
    lab.claude()
    cp = lab.run("--run", AI_CLIS_RESERVE_MB="999999999")
    assert cp.returncode == 3
    assert lab.curl_urls() == []
    assert "low disk" in lab.state()["clis"]["claude"]["pending"]["reason"]


# ============================================== TC-1200 supervision and timer (LLR-1110)
def test_schedule_makes_no_network_call_and_enables_the_timer_before_starting_the_service(lab):
    lab.standard()
    cp = lab.run("--schedule")
    assert cp.returncode == 0, out(cp)
    assert lab.curl_urls() == []
    calls = lab.log("systemctl.log")
    enable = calls.index("enable --now homehub-ai-clis-provision.timer")
    start = calls.index("start --no-block homehub-ai-clis-provision.service")
    assert enable < start
    assert (lab.units / "homehub-ai-clis-provision.timer").exists() and (lab.units / "homehub-ai-clis-provision.service").exists()
    assert not (lab.prefix / ".staging").exists()


def test_install_units_copies_the_files_reloads_and_enables_nothing(lab):
    cp = lab.run("--install-units", AI_CLI_UNIT_DIR=str(lab.units / "fresh"))
    assert cp.returncode == 0, out(cp)
    for n in ("homehub-ai-clis-provision.service", "homehub-ai-clis-provision.timer"):
        f = lab.units / "fresh" / n
        assert f.read_bytes() == (CLI_DIR / n).read_bytes() and stat.S_IMODE(f.stat().st_mode) == 0o644
    assert lab.log("systemctl.log") == ["daemon-reload"]        # nothing enabled, nothing started
    assert lab.curl_urls() == []


def test_install_units_rerun_writes_nothing(lab):
    assert lab.run("--install-units").returncode == 0
    (lab.logs / "systemctl.log").unlink()
    before = lab.snapshot(lab.units)
    time.sleep(0.05)
    cp = lab.run("--install-units")
    assert cp.returncode == 0 and "already current" in out(cp)
    assert lab.snapshot(lab.units) == before and lab.log("systemctl.log") == []


def test_schedule_on_a_box_without_units_installs_them_then_enables_the_timer(lab):
    lab.claude()
    lab.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="claude")
    assert list(lab.units.iterdir()) == []
    assert lab.run("--schedule").returncode == 0
    assert (lab.units / "homehub-ai-clis-provision.service").exists() and (lab.units / "homehub-ai-clis-provision.timer").exists()
    calls = lab.log("systemctl.log")
    assert calls.index("daemon-reload") < calls.index("enable --now homehub-ai-clis-provision.timer") < calls.index(
        "start --no-block homehub-ai-clis-provision.service")


def test_exit_three_keeps_the_timer_enabled_and_exit_zero_disables_it(lab):
    lab.claude()
    lab.urlmap.clear(); lab.write_manifest()
    cp = lab.run("--run")
    assert cp.returncode == 3
    calls = lab.log("systemctl.log")
    assert not any(c.startswith("disable") for c in calls)
    assert "enable --now homehub-ai-clis-provision.timer" in calls
    (lab.logs / "systemctl.log").unlink()
    lab.claude(); lab.write_manifest()
    cp = lab.run("--run")
    assert cp.returncode == 0, out(cp)
    assert lab.log("systemctl.log")[-1] == "disable --now homehub-ai-clis-provision.timer"


def test_empty_needed_set_or_off_disables_the_timer_and_clears_pending(lab):
    lab.standard()
    pend = {"version": 1, "clis": {"claude": {"pending": {"reason": "old", "since": "t"}, "announced": "abc"}}}
    lab.state_dir.mkdir(parents=True)
    for label, env in (("empty", dict(AI_USAGE_ENABLED="false")),
                       ("off", dict(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="claude", AI_CLI_PROVISION="off"))):
        (lab.state_dir / "state.json").write_text(json.dumps(pend))
        (lab.logs / "systemctl.log").unlink(missing_ok=True)
        lab.set_env(**env)
        cp = lab.run("--schedule")
        assert cp.returncode == 0, label + out(cp)
        assert lab.log("systemctl.log") == ["disable --now homehub-ai-clis-provision.timer"], label
        assert "pending" not in json.dumps(lab.state()), label
        assert lab.curl_urls() == []
    # --run behaves the same way
    lab.set_env(AI_USAGE_ENABLED="false")
    (lab.logs / "systemctl.log").unlink(missing_ok=True)
    assert lab.run("--run").returncode == 0
    assert lab.log("systemctl.log") == ["disable --now homehub-ai-clis-provision.timer"]
    assert lab.curl_urls() == []


@pytest.mark.parametrize("knob,expect", [("1", "5"), ("5", "5"), ("30", "30"), ("1440", "1440"), ("99999", "1440"),
                                         ("abc", "15"), ("", "15")])
def test_retry_interval_is_rendered_into_a_dropin_and_clamped(lab, knob, expect):
    lab.claude()
    lab.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="claude", AI_CLI_PROVISION_RETRY_MINUTES=knob)
    assert lab.run("--schedule").returncode == 0
    conf = (lab.units / "homehub-ai-clis-provision.timer.d" / "interval.conf").read_text()
    values = [l.split("=", 1)[1] for l in conf.splitlines() if l.startswith("OnUnitInactiveSec=")]
    assert values[0] == "" and values[-1] == f"{expect}min"      # the empty line resets any inherited value
    assert "daemon-reload" in lab.log("systemctl.log")


def test_a_persistent_failure_keeps_the_timer_and_prints_the_message_once(lab):
    lab.claude(); lab.rows["claude"]["sha256"] = "3" * 64; lab.write_manifest()
    first = lab.run("--run")
    second = lab.run("--run")
    assert first.returncode == second.returncode == 3
    assert "PENDING claude: checksum mismatch" in out(first)
    assert "PENDING claude" not in out(second) and "still pending since" in out(second)
    assert not any(c.startswith("disable") for c in lab.log("systemctl.log"))
    status = lab.run("--status")
    assert "PROVISIONING: INCOMPLETE - claude: checksum mismatch; retrying every 15 min" in out(status)
    # the version mismatch is persistent in the same way
    lab.claude("1.0.0"); lab.rows["claude"]["version"] = "7.7.7"; lab.write_manifest()
    third = lab.run("--run")
    assert third.returncode == 3 and "version 1.0.0, expected 7.7.7" in lab.state()["clis"]["claude"]["pending"]["reason"]


def test_the_aggregate_deadline_yields_exit_three(lab):
    lab.standard()
    cp = lab.run("--run", AI_CLIS_DEADLINE_SECONDS="0")
    assert cp.returncode == 3, out(cp)
    assert lab.curl_urls() == []
    assert "deadline" in lab.state()["clis"]["claude"]["pending"]["reason"]


def test_every_curl_has_connect_and_total_time_limits_and_https_only(lab):
    lab.standard()
    assert lab.run("--run").returncode == 0
    calls = lab.log("curl.log")
    assert len(calls) == 5
    for c in calls:
        assert "--fail" in c and "--connect-timeout 20" in c and "--max-time 900" in c and "--proto =https" in c


def test_the_unit_files_say_what_the_contract_says():
    svc = (CLI_DIR / "homehub-ai-clis-provision.service").read_text()
    tim = (CLI_DIR / "homehub-ai-clis-provision.timer").read_text()
    assert "Type=oneshot" in svc and "TimeoutStartSec=3600" in svc
    assert "ExecStart=/opt/homehub/stack/ai-cli/provision-ai-clis.sh --run" in svc
    assert "OnBootSec=5min" in tim and "OnUnitInactiveSec" not in re.sub(r"(?m)^#.*$", "", tim)
    assert "WantedBy=timers.target" in tim


# ===================================== TC-1201 schedule never waits on the lock (LLR-1110)
def test_schedule_returns_promptly_while_a_long_run_holds_the_lock(lab):
    lab.claude()
    bg = lab.popen("--run", FAKE_CURL_SLEEP="12")
    try:
        deadline = time.time() + 15
        while not lab.log("curl.log") and time.time() < deadline:
            time.sleep(0.1)
        assert lab.log("curl.log"), "the background run never reached its download"
        t0 = time.time()
        cp = lab.run("--schedule")
        elapsed = time.time() - t0
        assert cp.returncode == 0, out(cp)
        assert elapsed < 4, elapsed
        assert bg.poll() is None                  # the long run really was still holding the lock
        calls = lab.log("systemctl.log")
        assert calls.index("enable --now homehub-ai-clis-provision.timer") < calls.index(
            "start --no-block homehub-ai-clis-provision.service")
    finally:
        os.killpg(bg.pid, 9)
        bg.wait()


def test_a_busy_lock_defers_pending_cleanup(lab):
    lab.claude()
    lab.state_dir.mkdir(parents=True)
    pend = {"version": 1, "clis": {"claude": {"pending": {"reason": "old", "since": "t"}, "announced": "abc"}}}
    (lab.state_dir / "state.json").write_text(json.dumps(pend))
    lab.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="claude", AI_CLI_PROVISION="off")
    holder = subprocess.Popen(["flock", str(lab.lock), "sleep", "30"], start_new_session=True)
    try:
        for _ in range(50):
            if lab.lock.exists():
                break
            time.sleep(0.1)
        time.sleep(0.3)
        t0 = time.time()
        cp = lab.run("--schedule")
        assert time.time() - t0 < 4
        assert cp.returncode == 0, out(cp)
        assert "deferred" in out(cp)
        assert "old" in json.dumps(lab.state())                 # not cleared while the lock is busy
        os.killpg(holder.pid, 9); holder.wait()
        assert lab.run("--schedule").returncode == 0            # lock free: the cleanup now happens
        assert "pending" not in json.dumps(lab.state())
    finally:
        try:
            os.killpg(holder.pid, 9)
        except ProcessLookupError:
            pass


# ================================= TC-1202 archive safety, ownership and modes (LLR-1105, LLR-1106)
def test_a_symlink_that_stays_inside_the_tree_is_accepted(lab):
    lab.standard()
    cp = lab.run("--run")
    assert cp.returncode == 0, out(cp)
    node = next((lab.prefix / "node").iterdir())
    assert os.readlink(node / "bin" / "npm") == "../lib/npm-cli.js"
    # opencode is a member install: only the entry was extracted
    oc = next((lab.prefix / "opencode").iterdir())
    assert (oc / "opencode").is_file() and not (oc / "junk").exists()


def test_installed_trees_are_owned_by_the_configured_owner_and_carry_no_group_other_write_or_setuid(lab):
    lab.codex(extra={"loose": (b"x", 0o666), "suid": (b"#!/bin/sh\n", 0o4777), "d/inner": (b"y", 0o664)})
    lab.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="codex")
    cp = lab.run("--run")
    assert cp.returncode == 0, out(cp)
    for p in [lab.prefix, *lab.prefix.rglob("*")]:
        if p.is_symlink() or p.name == ".staging":
            continue
        mode = p.lstat().st_mode
        assert not mode & (stat.S_IWGRP | stat.S_IWOTH), p
        assert not mode & (stat.S_ISUID | stat.S_ISGID), p
    chown = lab.log("chown.log")
    assert any(c.startswith("-R root:root ") and "/.staging/codex." in c for c in chown), chown
    # the owner is a seam: a different value must reach chown (the default stays root:root)
    shutil.rmtree(lab.prefix); shutil.rmtree(lab.state_dir); (lab.bin / "codex").unlink()
    (lab.logs / "chown.log").unlink()
    assert lab.run("--run", AI_CLIS_OWNER="1234:5678").returncode == 0
    assert any(c.startswith("-R 1234:5678 ") for c in lab.log("chown.log"))
    assert re.search(r'OWNER="\$\{AI_CLIS_OWNER:-root:root\}"', SCRIPT.read_text())


def test_the_pinned_script_is_posix_clean_lf_and_syntax_checked():
    raw = SCRIPT.read_bytes()
    assert b"\r" not in raw and raw.startswith(b"#!/usr/bin/env bash\n")
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0
    assert subprocess.run(["bash", "-n", str(CLI_DIR / "wrapper.template")], capture_output=True).returncode == 0
    for p in (REAL_MANIFEST, CLI_DIR / "wrapper.template", CLI_DIR / "homehub-ai-clis-provision.service",
              CLI_DIR / "homehub-ai-clis-provision.timer"):
        assert b"\r" not in p.read_bytes(), p


def test_updater_controls_live_in_the_wrapper_of_the_right_cli_only(lab):
    lab.standard()
    assert lab.run("--run").returncode == 0
    w = {k: (lab.wrapper(k)[0] / "wrapper" / k).read_text() for k in ("claude", "codex", "opencode", "gemini")}
    assert "export DISABLE_AUTOUPDATER=1" in w["claude"] and "GEMINI" not in w["claude"]
    assert "export GEMINI_CLI_SYSTEM_SETTINGS_PATH=/etc/gemini-cli/settings.json" in w["gemini"]
    assert "export OPENCODE_DISABLE_AUTOUPDATE=true" in w["opencode"]
    assert "export OPENCODE_CONFIG_CONTENT='{\"autoupdate\":false}'" in w["opencode"]
    assert not [l for l in w["codex"].splitlines() if l.startswith("export")]
    for k, text in w.items():
        assert text.count("exec ") == 1 and f"{lab.prefix}/{k}/" in text


def test_a_template_without_a_launcher_for_a_key_fails_the_install(lab):
    lab.claude()
    t = lab.ai / "wrapper.template"
    t.write_text(re.sub(r"(?m)^#claude# exec.*$", "", t.read_text()))
    cp = lab.run("--run")
    assert cp.returncode == 3 and "no launcher" in lab.state()["clis"]["claude"]["pending"]["reason"]
    assert lab.leftovers() == []


# ====================================== TC-1203 status contract (LLR-1112, IF-038)
def test_status_json_has_the_documented_shape(lab):
    lab.standard()
    cp = lab.run("--status", "--json")
    d = json.loads(cp.stdout)
    assert d["provisioning"] == "incomplete" and set(d["clis"]) == {"claude", "codex", "opencode", "gemini", "node"}
    assert set(d["clis"]["claude"]) == {"installed", "identity", "version", "link_target", "activation_state", "pending_reason"}
    assert d["clis"]["claude"]["installed"] is False and d["clis"]["claude"]["identity"] is None
    assert lab.run("--run").returncode == 0
    d = json.loads(lab.run("--status", "--json").stdout)
    assert d["provisioning"] == "complete"
    c = d["clis"]["claude"]
    assert c["installed"] is True and c["version"] == "1.0.0" and c["activation_state"] == "committed"
    assert c["identity"] == lab.wrapper("claude")[0].name and c["link_target"] == lab.link("claude")
    assert d["clis"]["node"]["installed"] is True and d["clis"]["node"]["link_target"] is None
    assert "PROVISIONING: complete" in out(lab.run("--status"))
    assert lab.run("--check").returncode == 0


def test_status_reports_running_while_the_lock_is_held_and_off_when_off(lab):
    lab.claude()
    lab.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="claude")
    holder = subprocess.Popen(["flock", str(lab.lock), "sleep", "20"], start_new_session=True)
    try:
        time.sleep(0.5)
        assert json.loads(lab.run("--status", "--json").stdout)["provisioning"] == "running"
    finally:
        os.killpg(holder.pid, 9); holder.wait()
    lab.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="claude", AI_CLI_PROVISION="off")
    assert json.loads(lab.run("--status", "--json").stdout)["provisioning"] == "off"
    assert "PROVISIONING: off" in out(lab.run("--status"))


def test_state_json_is_minimal_and_has_no_timestamps_except_since(lab):
    lab.claude()
    lab.rows["claude"]["sha256"] = "4" * 64; lab.write_manifest()
    lab.run("--run"); lab.run("--run")
    text = (lab.state_dir / "state.json").read_text()
    assert set(re.findall(r"\d{4}-\d\d-\d\dT", text)) <= {text[text.find("20"):text.find("20") + 11]}
    st = lab.state()["clis"]["claude"]
    assert set(st) == {"pending", "announced"} and set(st["pending"]) == {"reason", "since"}
    first = (lab.state_dir / "state.json").stat()
    lab.run("--run")
    assert (lab.state_dir / "state.json").stat().st_mtime_ns == first.st_mtime_ns       # no rewrite for identical content


# ======================================== TC-1204 effective account and enabled set (LLR-1108, LLR-1104)
def _accounts(lab):
    return set(lab.log("runuser-accounts.log"))


def test_cli_only_deployment_uses_the_cli_account_and_needs_only_claude(lab):
    lab.standard()
    lab.set_env(AI_USAGE_ENABLED="false", AI_USAGE_USER_ACCOUNT="usageacct", AI_CLI_ENABLED="true", AI_CLI_USER="alice")
    assert lab.run("--run").returncode == 0
    assert _accounts(lab) == {"alice"}
    assert sorted(p.name for p in lab.bin.iterdir()) == ["claude"]       # no OPENAI route is enabled by default


def test_usage_only_deployment_uses_the_usage_account(lab):
    lab.standard()
    lab.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="claude,codex", AI_USAGE_USER_ACCOUNT="bob")
    assert lab.run("--run").returncode == 0
    assert _accounts(lab) == {"bob"}
    assert sorted(p.name for p in lab.bin.iterdir()) == ["claude", "codex"]
    assert lab.rows["gemini"]["url"] not in lab.curl_urls() and lab.rows["node"]["url"] not in lab.curl_urls()


def test_both_enabled_with_differing_names_uses_the_cli_account(lab):
    lab.standard()
    lab.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="opencode", AI_USAGE_USER_ACCOUNT="bob",
                AI_CLI_ENABLED="true", AI_CLI_USER="alice")
    assert lab.run("--run").returncode == 0
    assert _accounts(lab) == {"alice"}
    assert sorted(p.name for p in lab.bin.iterdir()) == ["claude", "opencode"]


def test_default_account_is_homehub_ai_and_hub_or_root_is_refused(lab):
    lab.claude()
    lab.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="claude")
    assert lab.run("--run").returncode == 0 and _accounts(lab) == {"homehub-ai"}
    for bad in ("hub", "root", "Bad Name"):
        lab.set_env(AI_USAGE_ENABLED="true", AI_USAGE_SOURCES="claude", AI_USAGE_USER_ACCOUNT=bad)
        assert lab.run("--run").returncode == 2, bad


def test_an_enabled_openai_route_in_a_custom_registry_installs_codex_for_a_cli_only_deployment(lab):
    lab.standard()
    reg = lab.root / "custom.registry.csv"
    reg.write_text("# custom\nId,Family,Model,Version,Tier,CmdTemplate,Env,Notes\n"
                   "R-A,ANTHROPIC,m,1,quick,\"claude -p\",,x\nR-O,OPENAI,m,1,quick,\"codex exec\",,x\n")
    routes = lab.root / "custom.routes"
    routes.write_text("# consent\nR-A\n")
    base = dict(AI_USAGE_ENABLED="false", AI_CLI_ENABLED="true", AI_CLI_USER="alice",
                AI_CLI_REGISTRY=str(reg), AI_CLI_ENABLED_ROUTES=str(routes))
    lab.set_env(**base)
    assert lab.run("--run").returncode == 0
    assert sorted(p.name for p in lab.bin.iterdir()) == ["claude"]          # catalogued but not consented
    routes.write_text("# consent\nR-A\nR-O\n")
    assert lab.run("--run").returncode == 0
    assert sorted(p.name for p in lab.bin.iterdir()) == ["claude", "codex"]
    # the shipped registry has its OPENAI route commented out of routes-enabled: no codex from the defaults
    shutil.rmtree(lab.prefix); shutil.rmtree(lab.state_dir)
    for p in list(lab.bin.iterdir()):
        p.unlink()
    lab.set_env(AI_USAGE_ENABLED="false", AI_CLI_ENABLED="true", AI_CLI_USER="alice")
    assert lab.run("--run").returncode == 0
    assert sorted(p.name for p in lab.bin.iterdir()) == ["claude"]


def test_unknown_arguments_and_a_bad_manifest_are_refused_with_exit_two(lab):
    lab.claude()
    assert lab.run("--bogus").returncode == 2
    assert lab.run().returncode == 2
    bad = lab.root / "bad.tsv"
    bad.write_text("claude\t1.0.0\tonly-three-fields\n")
    assert lab.run("--check", AI_CLIS_MANIFEST=str(bad)).returncode == 2
    lab.rows["claude"]["url"] = "http://dl.test/claude"
    lab.write_manifest()
    cp = lab.run("--check")
    assert cp.returncode == 2 and "https" in out(cp)
