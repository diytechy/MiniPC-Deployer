"""OtterTube's update channel on the hub and the site/library knob split (SR-125..SR-127, IF-075).

What runs here, on the dev PC: the compose mount and the knob's resolution, the
Caddy block's exact update handles (read from the file), the DNS record's knob
reading (the real lines of provision-technitium.sh run under bash with a stub
add_a), the template's defaults, the library knob's readers left alone, the
folder's git rules, and the names agreeing with OtterTube's own client and
manifest maker (the sibling checkout, when present).

What does not: Caddy actually serving the routes - the real block in the hub's
Caddy image, stack/ottertube/tests/ottertube-updates-lab.sh (TC-1531..TC-1536).
Implements: TC-1521..TC-1530.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
OT_DIR = STACK / "ottertube"
OTTERTUBE = Path(os.environ.get("OTTERTUBE_CHECKOUT") or ROOT.parent / "OtterTube")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def caddy_block() -> str:
    text = read(STACK / "caddy" / "Caddyfile")
    block = text[text.index("\n{$OTTERTUBE_HOST} {\n") + 1:]
    return block[:block.index("\n}\n") + 3]


def handle_body(block: str, matcher: str) -> str:
    """The body of `handle <matcher> { ... }` (one level of braces)."""
    start = block.index("handle %s {" % matcher)
    depth, i = 0, block.index("{", start)
    for j in range(i, len(block)):
        if block[j] == "{":
            depth += 1
        elif block[j] == "}":
            depth -= 1
            if depth == 0:
                return block[i + 1:j]
    raise AssertionError("unbalanced handle " + matcher)


def compose():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(read(STACK / "docker-compose.yml"))


def env_example() -> list[tuple[str, str]]:
    out = []
    for line in read(STACK / ".env.example").splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


# ── TC-1521 the folder, mounted read-only into caddy and nowhere else ────────


def test_tc1521_caddy_mounts_the_updates_folder_read_only_and_nothing_else_does():
    doc = compose()
    caddy = doc["services"]["caddy"]
    assert "./ottertube/updates:/srv/ottertube/updates:ro" in caddy["volumes"]
    # the web build keeps its own mount: the channel is not inside it
    assert "./ottertube/web:/srv/ottertube/web:ro" in caddy["volumes"]
    for name, svc in doc["services"].items():
        if name != "caddy":
            assert "ottertube/updates" not in json.dumps(svc.get("volumes", [])), name
    assert (OT_DIR / "updates" / "README.md").is_file()


# ── TC-1522 the exact update handles, no encode, revalidated manifest ────────


def test_tc1522_the_update_handles_are_exact_unencoded_and_ahead_of_the_web_build():
    block = caddy_block()
    assert "@ot_update path /ottertube/update.json /ottertube/update.json.sig\n" in block
    assert "@ot_update_exe path_regexp ^/ottertube/OtterTube-[0-9a-f]{7}\\.exe$\n" in block
    for matcher in ("@ot_update", "@ot_update_os", "@ot_update_exe"):
        body = handle_body(block, matcher)
        assert "root * /srv/ottertube/updates" in body, matcher
        assert "uri strip_prefix /ottertube" in body, matcher
        assert "file_server" in body and "browse" not in body, matcher
        assert "encode" not in body, matcher
        assert "reverse_proxy" not in body, matcher
    assert 'header >Cache-Control "no-cache"' in handle_body(block, "@ot_update")
    assert 'header >Cache-Control "no-cache"' in handle_body(block, "@ot_update_os")
    # Every update handle precedes the web build's catch-all in the text, and the
    # catch-all is a handle_path (which Caddy orders after every handle).
    web = block.index("handle_path /ottertube/* {")
    for matcher in ("@ot_update", "@ot_update_os", "@ot_update_exe"):
        assert block.index("handle %s {" % matcher) < web, matcher
    # nothing else in the block roots at the folder, and the folder is served nowhere else
    assert block.count("root * /srv/ottertube/updates") == 3
    assert "/srv/ottertube/updates" not in read(STACK / "caddy" / "Caddyfile").replace(block, "")


# ── TC-1523 the reserved per-platform names ──────────────────────────────────


def test_tc1523_later_platforms_have_reserved_names_and_update_json_stays_windows():
    block = caddy_block()
    m = re.search(r"@ot_update_os path_regexp (\S+)\n", block)
    assert m
    rx = re.compile(m.group(1))
    for ok in ("/ottertube/update-linux-x64.json", "/ottertube/update-linux-arm64.json.sig",
               "/ottertube/update-darwin-arm64.json", "/ottertube/update-windows-arm64.json"):
        assert rx.match(ok), ok
    for no in ("/ottertube/update.json", "/ottertube/update-linux-x64.json.tmp", "/ottertube/update-linux.json",
               "/ottertube/update-Linux-x64.json", "/ottertube/update-linux-x64.exe", "/ottertube/x/update-linux-x64.json"):
        assert not rx.match(no), no
    exe = re.compile(re.search(r"@ot_update_exe path_regexp (\S+)\n", block).group(1))
    assert exe.match("/ottertube/OtterTube-abcdef0.exe")
    for no in ("/ottertube/OtterTube-ABCDEF0.exe", "/ottertube/OtterTube-abcdef.exe", "/ottertube/OtterTube-abcdef01.exe",
               "/ottertube/OtterTube-abcdef0.exe.tmp", "/ottertube/.OtterTube-abcdef0.exe.tmp", "/ottertube/OtterTube-abcdef0.EXE"):
        assert not exe.match(no), no
    readme = read(OT_DIR / "updates" / "README.md")
    assert "update-<os>-<arch>.json" in readme and "Windows schema-1" in readme


# ── TC-1524 the site knob gates the whole block, first ───────────────────────


def test_tc1524_the_site_knob_turns_the_block_into_404_before_anything_else():
    block = caddy_block()
    gate = '@ot_site_off expression `"{$OTTERTUBE_SITE_ENABLED}" != "true"`\n'
    assert gate in block
    assert handle_body(block, "@ot_site_off").strip() == 'respond "ottertube: site off" 404'
    # first among the block's handles (both are named, non-path matchers, which
    # Caddy keeps in written order)
    first_handle = re.search(r"^    handle\b.*$", block, re.M).group(0)
    assert first_handle == "    handle @ot_site_off {"
    assert block.index("handle @ot_site_off {") < block.index("handle @lan {")
    header = read(STACK / "caddy" / "Caddyfile").split("{$OTTERTUBE_HOST} {")[0]
    assert "{$OTTERTUBE_SITE_ENABLED}" in header, "the env list at the top names it"


# ── TC-1525 compose resolves the site knob, falling back to the library's ────


def test_tc1525_compose_hands_caddy_the_site_knob_or_else_the_library_knob():
    env = compose()["services"]["caddy"]["environment"]
    assert env["OTTERTUBE_SITE_ENABLED"] == "${OTTERTUBE_SITE_ENABLED:-${OTTERTUBE_ENABLED:-false}}"
    assert env["OTTERTUBE_HOST"] == "${OTTERTUBE_SUBDOMAIN:-ottertube}.${DOMAIN}"


# ── TC-1526 the DNS record follows the site knob (the real lines, under bash) ──


def dns_lines() -> str:
    text = read(STACK / "provision" / "provision-technitium.sh")
    start = text.index("    local __ot_site=")
    end = text.index("    fi\n", start) + len("    fi\n")
    return text[start:end]


@pytest.mark.parametrize("library, site, record", [
    ("true", None, True),       # every .env before the split: the live hub
    ("true", "", True),         # the new template line, left empty
    ("true", "false", False),   # the site off, the library on
    ("false", "true", True),    # the library retired, the site kept
    ("false", None, False),
    (None, None, False),
    ("True", None, False),      # exactly `true`, as the Caddyfile reads it
    ("false", "yes", False),
])
def test_tc1526_the_record_exists_exactly_while_the_site_knob_is_on(library, site, record):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("needs bash")
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTTERTUBE_")}
    env.update({"DOMAIN": "home.example", "LAN_IP": "192.0.2.5", "TOKEN": "t"})
    if library is not None:
        env["OTTERTUBE_ENABLED"] = library
    if site is not None:
        env["OTTERTUBE_SITE_ENABLED"] = site
    script = "set -euo pipefail\nadd_a() { echo \"A $2 $3\"; }\nf() {\n%s}\nf\n" % dns_lines()
    out = subprocess.run([bash, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert (out.stdout.strip() == "A ottertube.home.example 192.0.2.5") is record, out.stdout
    assert "*." not in dns_lines()


# ── TC-1527 the template: the site knob empty (follows), the library off ─────


def test_tc1527_the_template_declares_the_site_knob_empty_and_the_library_off():
    pairs = env_example()
    keys = [k for k, _ in pairs]
    for k in ("OTTERTUBE_ENABLED", "OTTERTUBE_SITE_ENABLED", "OTTERTUBE_SUBDOMAIN"):
        assert keys.count(k) == 1, k
    vals = dict(pairs)
    assert vals["OTTERTUBE_ENABLED"] == "false"
    assert vals["OTTERTUBE_SITE_ENABLED"] == ""
    assert keys.index("OTTERTUBE_ENABLED") < keys.index("OTTERTUBE_SITE_ENABLED") < keys.index("OTTERTUBE_SUBDOMAIN")


# ── TC-1528 the library knob's readers do not read the site knob ─────────────


def test_tc1528_the_library_stays_on_its_own_knob():
    sys.path.insert(0, str(OT_DIR))
    sys.path.insert(0, str(STACK / "monitoring"))
    import ottertube as ot  # noqa: E402
    assert ot.Settings({"OTTERTUBE_ENABLED": "false", "OTTERTUBE_SITE_ENABLED": "true"}).enabled is False
    assert ot.Settings({"OTTERTUBE_ENABLED": "true", "OTTERTUBE_SITE_ENABLED": "false"}).enabled is True
    for path in (OT_DIR / "ottertube.py", STACK / "autoinstall" / "firstboot.sh", ROOT / "vmtest" / "export-images.sh",
                 OT_DIR / "homehub-ottertube-download.service", OT_DIR / "homehub-ottertube-media.service",
                 OT_DIR / "homehub-ottertube-download.timer"):
        assert "OTTERTUBE_SITE_ENABLED" not in read(path), path
    # and the site's readers never read the library knob except as the fallback
    tech = read(STACK / "provision" / "provision-technitium.sh")
    code = "\n".join(l for l in tech.splitlines() if not l.lstrip().startswith("#"))
    assert re.findall(r"\$\{OTTERTUBE_ENABLED[^}]*\}", code) == ["${OTTERTUBE_ENABLED:-}"]


# ── TC-1529 the folder's git rules: README only, binary should it ever be tracked ──


def test_tc1529_only_the_readme_is_tracked_and_the_manifest_is_never_normalised():
    git = shutil.which("git")
    if not git or not (ROOT / ".git").exists():
        pytest.skip("needs git and a checkout")

    def ignored(rel):
        return subprocess.run([git, "check-ignore", "-q", rel], cwd=ROOT).returncode == 0
    for rel in ("stack/ottertube/updates/update.json", "stack/ottertube/updates/update.json.sig",
                "stack/ottertube/updates/OtterTube-abcdef0.exe", "stack/ottertube/updates/update-linux-x64.json"):
        assert ignored(rel), rel
    assert not ignored("stack/ottertube/updates/README.md")
    for rel in ("stack/ottertube/updates/update.json", "stack/ottertube/updates/update.json.sig",
                "stack/ottertube/updates/update-linux-x64.json"):
        attrs = subprocess.run([git, "check-attr", "text", "diff", "--", rel], cwd=ROOT, capture_output=True, text=True).stdout
        assert "text: unset" in attrs and "diff: unset" in attrs, (rel, attrs)


# ── TC-1530 the names agree with OtterTube's own client and manifest maker ───


@pytest.mark.skipif(not (OTTERTUBE / "desktop" / "src" / "main" / "update.ts").exists(),
                    reason="needs the sibling OtterTube checkout (OTTERTUBE_CHECKOUT overrides)")
def test_tc1530_the_routes_are_the_names_the_app_asks_for():
    update_ts = read(OTTERTUBE / "desktop" / "src" / "main" / "update.ts")
    maker = read(OTTERTUBE / "desktop" / "scripts" / "make-update.mjs")
    assert "UPDATE_MANIFEST_PATH = '/ottertube/update.json'" in update_ts
    assert "UPDATE_SIG_SUFFIX = '.sig'" in update_ts
    assert "const file = `OtterTube-${info.commit.slice(0, 7)}.exe`;" in maker
    # the maker names exactly what the exe route admits: 7 lowercase hex of a 40-hex commit
    assert "/^[0-9a-f]{40}$/" in maker
    assert r"/^OtterTube-[0-9a-f]{7}\.exe$/" in maker
    # the app checks Content-Length against the manifest's size: why no route may encode
    assert "res.headers.get('content-length')" in update_ts
    # the order the publisher follows is the one the app's README asks for
    readme = read(OTTERTUBE / "desktop" / "README.md")
    assert "the exe first, then `update.json.sig` and `update.json`" in readme
    assert "the exe first, then `update.json.sig`, then\n`update.json`" in read(OT_DIR / "updates" / "README.md")
