"""SR-081: the panel's AI rings are derived from AI_USAGE_SOURCES.

Implements tests: TC-1215 (order/badges vs the real renderer), TC-1216
(ownership), TC-1217 (idempotence, source untouched, atomic, inode),
TC-1218 (disabled / dropped source), TC-1219 (secrets).
Covers LLR-1121..LLR-1125. Every contract is asserted against the OTHER side:
the real normalizeGaugeRings and USAGE_PROVIDER_ORDER in OfficeWallNaglight.
No FreeLLMAPI behaviour is asserted here (a later phase).
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "stack/panel-access/install-renderer-config.py"
SPEC = importlib.util.spec_from_file_location("install_renderer_config_w3", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

GAUGES = ROOT.parent / "OfficeWallNaglight/js/views/gauges.js"
NODE = shutil.which("node")
DEFAULT_BLOCK = {"slots": [], "unlisted": "append"}


def need_gauges():
    if not GAUGES.is_file():
        pytest.skip("sibling repo OfficeWallNaglight/js/views/gauges.js absent: contract side not checked")


def make(tmp_path, sources, enabled="true", block="absent", extra=None, indent=2):
    env = tmp_path / "hub.env"
    env.write_text(
        "PANEL_ACCESS_ENABLED=false\nAI_USAGE_ENABLED=%s\nAI_USAGE_SOURCES=%s\n" % (enabled, sources),
        encoding="utf-8")
    doc = {"ACCESS_ENABLED": False, "POLL_SECONDS": 60, "zeta": 1, "alpha": {"b": 2, "a": 1}}
    doc.update(extra or {})
    if block != "absent":
        doc["gaugeRings"] = block
    source = tmp_path / "site" / "config.json"
    source.parent.mkdir(exist_ok=True)
    source.write_text(json.dumps(doc, indent=indent) + "\n", encoding="utf-8")
    dest = tmp_path / "wall-shell" / "config.json"
    dest.parent.mkdir(exist_ok=True)
    return env, source, dest


def run(env, source, dest):
    MODULE.install(env, source, dest)
    return json.loads(dest.read_text(encoding="utf-8"))


def derived(tmp_path, sources, **kw):
    return run(*make(tmp_path, sources, **kw))["gaugeRings"]


def normalise(block):
    """The REAL normalizeGaugeRings, under node."""
    need_gauges()
    if not NODE:
        pytest.skip("node not on PATH: normalizeGaugeRings not exercised")
    program = (
        "import(process.argv[1]).then(m=>{const w=[];"
        "const out=m.normalizeGaugeRings(JSON.parse(process.argv[2]),x=>w.push(x));"
        "console.log(JSON.stringify({out,w}));})"
    )
    env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
    done = subprocess.run([NODE, "-e", program, GAUGES.as_uri(), json.dumps(block)],
                          capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr[-400:]
    return json.loads(done.stdout)


def slots(block):
    return [(s["provider"], s.get("mark"), s.get("badge")) for s in block["slots"]]


CASES = [
    ("codex,claude,opencode",
     [("claude", "claude", None), ("codex", None, None), ("opencode", None, None)]),
    ("claude,claude2,codex,gemini,opencode",
     [("claude", "claude", "1"), ("claude2", "claude", "2"), ("codex", None, None),
      ("gemini", None, None), ("opencode", None, None)]),
    ("claude2", [("claude2", "claude", None)]),
    ("opencode, Mystery ,codex,mystery",
     [("codex", None, None), ("opencode", None, None), ("mystery", None, None)]),
    ("codex,bad_name,x y,,opencode", [("codex", None, None), ("opencode", None, None)]),
]


@pytest.mark.parametrize("sources,expected", CASES)
def test_tc1215_order_marks_badges_and_non_lossy_round_trip(tmp_path, sources, expected):
    block = derived(tmp_path, sources)
    assert slots(block) == expected
    assert block["unlisted"] == "hide" and block["derivedFrom"] == "AI_USAGE_SOURCES"
    result = normalise(block)
    assert result["out"]["slots"] == block["slots"], "normalizeGaugeRings dropped or changed a slot"
    assert result["out"]["unlisted"] == "hide"
    assert result["w"] == [], "renderer warned about the derived block"


def test_tc1215_order_table_matches_the_renderer_source():
    need_gauges()
    text = GAUGES.read_text(encoding="utf-8")
    body = re.search(r"USAGE_PROVIDER_ORDER\s*=\s*\[(.*?)\]", text, re.S).group(1)
    assert list(MODULE.PROVIDER_ORDER) == re.findall(r'"([a-z0-9]+)"', body)


def test_tc1216_absent_marker_and_shipped_default_are_derived(tmp_path):
    blocks = ["absent",
              {"slots": [{"provider": "grok"}], "unlisted": "hide", "derivedFrom": "AI_USAGE_SOURCES"},
              dict(DEFAULT_BLOCK),
              {"unlisted": "append", "slots": []}]
    for n, block in enumerate(blocks):
        d = tmp_path / str(n)
        d.mkdir()
        assert slots(derived(d, "codex,claude", block=block)) == [
            ("claude", "claude", None), ("codex", None, None)]


@pytest.mark.parametrize("block", [
    {"slots": [], "unlisted": "hide"},
    {"slots": [{"provider": "grok"}], "unlisted": "append"},
    {"slots": [], "unlisted": "append", "extra": 1},
    {"unlisted": "hide"},
])
def test_tc1216_owner_written_block_untouched_with_one_log_line(tmp_path, capsys, block):
    env, source, dest = make(tmp_path, "codex,claude", block=block)
    assert run(env, source, dest)["gaugeRings"] == block
    assert dest.read_bytes() == source.read_bytes()
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("gaugeRings:")]
    assert len(lines) == 1


def test_tc1217_idempotent_source_untouched_inode_stable_no_partials(tmp_path):
    env, source, dest = make(tmp_path, "claude,claude2,codex")
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    run(env, source, dest)
    first = dest.read_bytes()
    inode = dest.parent.stat().st_ino
    run(env, source, dest)
    assert dest.read_bytes() == first
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    assert b"gaugeRings" not in source.read_bytes()
    assert dest.parent.stat().st_ino == inode
    assert [p.name for p in dest.parent.iterdir()] == ["config.json"], "temp file left behind"


def test_tc1217_replace_is_atomic_one_rename_into_the_same_directory(tmp_path, monkeypatch):
    env, source, dest = make(tmp_path, "codex")
    dest.write_text("old", encoding="utf-8")
    calls = []
    real = os.replace

    def spy(src, dst):
        calls.append((Path(src).parent, Path(dst), Path(src).read_bytes()))
        real(src, dst)

    monkeypatch.setattr(MODULE.os, "replace", spy)
    MODULE.install(env, source, dest)
    assert len(calls) == 1 and calls[0][0] == dest.parent and calls[0][1] == dest
    assert json.loads(calls[0][2])["gaugeRings"]["slots"][0]["provider"] == "codex"


def test_tc1217_other_keys_and_their_order_and_indent_preserved(tmp_path):
    env, source, dest = make(tmp_path, "codex", indent=4)
    run(env, source, dest)
    got = json.loads(dest.read_text(encoding="utf-8"))
    src = json.loads(source.read_text(encoding="utf-8"))
    assert list(got)[:-1] == list(src) and list(got["alpha"]) == ["b", "a"]
    assert {k: v for k, v in got.items() if k != "gaugeRings"} == src
    assert '\n    "zeta"' in dest.read_text(encoding="utf-8")
    assert dest.read_bytes().endswith(b"\n")


@pytest.mark.parametrize("enabled", ["false", "", "yes"])
def test_tc1218_not_enabled_is_byte_identical(tmp_path, enabled):
    env, source, dest = make(tmp_path, "codex,claude", enabled=enabled)
    run(env, source, dest)
    assert dest.read_bytes() == source.read_bytes()


def test_tc1218_dropped_source_leaves_the_slots(tmp_path):
    env, source, dest = make(tmp_path, "codex,gemini")
    assert "gemini" in [s["provider"] for s in run(env, source, dest)["gaugeRings"]["slots"]]
    env.write_text(env.read_text().replace("codex,gemini", "codex"), encoding="utf-8")
    assert [s["provider"] for s in run(env, source, dest)["gaugeRings"]["slots"]] == ["codex"]


CANARY = "canary-secret-7f3a"


def test_tc1219_non_empty_secret_rejected_without_printing_it(tmp_path):
    env, source, dest = make(tmp_path, "codex", extra={"FEED_TOKEN": CANARY})
    done = subprocess.run([sys.executable, str(SCRIPT), "--env", str(env), "--source", str(source),
                           "--destination", str(dest)], capture_output=True, text=True)
    assert done.returncode != 0
    assert CANARY not in done.stdout + done.stderr
    assert not dest.exists()


def test_tc1219_empty_secret_keys_derive_and_are_preserved(tmp_path):
    env, source, dest = make(tmp_path, "codex", extra={"FEED_TOKEN": "", "HEARTBEAT_URL": "", "SUBSONIC": {}})
    got = run(env, source, dest)
    assert got["FEED_TOKEN"] == "" and got["HEARTBEAT_URL"] == "" and got["SUBSONIC"] == {}
    assert got["gaugeRings"]["slots"] == [{"provider": "codex"}]


# ---- fix round 1: the renderer must see the environment the feeder sees ----

FEEDER = ROOT / "stack/ai-usage/ai_usage_feeder.py"
DEFAULT_PROVIDERS = ["claude", "codex", "opencode"]


def make_raw(tmp_path, env_text, document='{"ACCESS_ENABLED": false, "POLL_SECONDS": 60}\n'):
    env = tmp_path / "hub.env"
    env.write_bytes(env_text.encode("utf-8"))
    source = tmp_path / "site-config.json"
    source.write_bytes(document.encode("utf-8") if isinstance(document, str) else document)
    dest = tmp_path / "wall-shell"
    dest.mkdir(exist_ok=True)
    return env, source, dest / "config.json"


def providers(dest):
    return [s["provider"] for s in json.loads(dest.read_text(encoding="utf-8"))["gaugeRings"]["slots"]]


def test_tc1215_default_sources_match_the_feeder_source():
    text = FEEDER.read_text(encoding="utf-8")
    found = re.search(r'env\.get\("AI_USAGE_SOURCES"\)\s+or\s+"([^"]+)"', text)
    assert found, "feeder default no longer has the shape this test reads"
    assert MODULE.DEFAULT_SOURCES == found.group(1)


@pytest.mark.parametrize("line", ["", "AI_USAGE_SOURCES=\n", "AI_USAGE_SOURCES=  \n",
                                  "AI_USAGE_SOURCES=,\n", "AI_USAGE_SOURCES= , ,\n",
                                  "AI_USAGE_SOURCES=\"\"\n"])
def test_tc1215_unset_or_empty_sources_use_the_feeders_default(tmp_path, line):
    env, source, dest = make_raw(tmp_path, "AI_USAGE_ENABLED=true\n" + line)
    MODULE.install(env, source, dest)
    assert providers(dest) == DEFAULT_PROVIDERS


def test_tc1216_marked_block_with_no_usable_source_is_regenerated_empty(tmp_path):
    stale = {"slots": [{"provider": "gemini"}], "unlisted": "hide", "derivedFrom": "AI_USAGE_SOURCES"}
    doc = json.dumps({"ACCESS_ENABLED": False, "gaugeRings": stale})
    env, source, dest = make_raw(tmp_path, "AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=bad_name\n", doc)
    MODULE.install(env, source, dest)
    block = json.loads(dest.read_text(encoding="utf-8"))["gaugeRings"]
    assert block["slots"] == [] and block["derivedFrom"] == "AI_USAGE_SOURCES"


def test_tc1215_continuation_line_is_joined_like_systemd(tmp_path):
    env, source, dest = make_raw(tmp_path, "AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=codex,\\\nclaude\n")
    MODULE.install(env, source, dest)
    assert providers(dest) == ["claude", "codex"]


def test_tc1215_comment_lines_are_not_assignments_and_last_assignment_wins(tmp_path):
    text = ("# AI_USAGE_SOURCES=grok\n; AI_USAGE_SOURCES=grok\nAI_USAGE_ENABLED=true\n"
            "AI_USAGE_SOURCES=gemini\nAI_USAGE_SOURCES=\"codex\"\n")
    env, source, dest = make_raw(tmp_path, text)
    MODULE.install(env, source, dest)
    assert providers(dest) == ["codex"]


def test_tc1215_inline_hash_is_part_of_the_value_as_systemd_reads_it(tmp_path):
    env, source, dest = make_raw(tmp_path, "AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=codex,claude # note\n")
    MODULE.install(env, source, dest)
    assert providers(dest) == ["codex"], "'claude # note' is not a source name; only codex survives"


@pytest.mark.parametrize("text", [
    "AI_USAGE_SOURCES=codex\n",                           # key missing
    "AI_USAGE_ENABLED=\nAI_USAGE_SOURCES=codex\n",        # empty value
    "AI_USAGE_ENABLED=true # on\nAI_USAGE_SOURCES=codex\n",  # inline hash makes it not 'true'
    "export AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=codex\n",
    "AI_USAGE_ENABLED=True\nAI_USAGE_SOURCES=codex\n",
    "  AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=codex\n",
    "AI_USAGE_ENABLED=true\nexport AI_USAGE_SOURCES=codex\n",
])
def test_tc1218_unreadable_or_non_true_enable_fails_closed(tmp_path, capsys, text):
    env, source, dest = make_raw(tmp_path, text)
    MODULE.install(env, source, dest)
    assert dest.read_bytes() == source.read_bytes()
    out = capsys.readouterr().out
    if "ENABLED=\n" not in text and "AI_USAGE_ENABLED" in text:
        assert "gaugeRings:" in out, "a fail-closed refusal must say so"


def test_tc1217_unpaired_surrogate_in_source_does_not_abort(tmp_path):
    doc = '{"ACCESS_ENABLED": false, "note": "\\ud800"}\n'
    env, source, dest = make_raw(tmp_path, "AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=codex\n", doc)
    MODULE.install(env, source, dest)
    out = dest.read_bytes()
    out.decode("ascii")
    assert providers(dest) == ["codex"]
    assert json.loads(out.decode("ascii"))["note"] == "\ud800"


def test_tc1217_any_derivation_failure_installs_the_source_unchanged(tmp_path, monkeypatch, capsys):
    env, source, dest = make_raw(tmp_path, "AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=codex\n")

    def boom(*args):
        raise UnicodeEncodeError("ascii", "x", 0, 1, "boom")

    monkeypatch.setattr(MODULE, "serialise", boom)
    MODULE.install(env, source, dest)
    assert dest.read_bytes() == source.read_bytes()
    assert "WARNING" in capsys.readouterr().out


def test_tc1217_output_is_validated_and_non_finite_numbers_fall_back(tmp_path, capsys):
    doc = '{"ACCESS_ENABLED": false, "n": Infinity}\n'
    env, source, dest = make_raw(tmp_path, "AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=codex\n", doc)
    MODULE.install(env, source, dest)
    assert dest.read_bytes() == source.read_bytes()
    assert "WARNING" in capsys.readouterr().out


def test_tc1217_output_is_policy_validated(tmp_path, monkeypatch, capsys):
    env, source, dest = make_raw(tmp_path, "AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=codex\n")
    real = MODULE.MODULE.validate
    seen = []

    def spy(env_text, document):
        seen.append(document)
        if "gaugeRings" in document:
            raise ValueError("rejected")
        return real(env_text, document)

    monkeypatch.setattr(MODULE.MODULE, "validate", spy)
    MODULE.install(env, source, dest)
    assert any("gaugeRings" in d for d in seen), "the derived output was never validated"
    assert dest.read_bytes() == source.read_bytes()


def test_tc1217_crlf_bom_and_tab_indent_are_normalised_value_faithfully(tmp_path):
    body = '{\r\n\t"ACCESS_ENABLED": false,\r\n\t"POLL_SECONDS": 60\r\n}\r\n'
    env, source, dest = make_raw(tmp_path, "AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=codex\n",
                                 b"\xef\xbb\xbf" + body.encode("utf-8"))
    MODULE.install(env, source, dest)
    out = dest.read_bytes()
    assert not out.startswith(b"\xef\xbb\xbf") and b"\r" not in out
    assert b'\n\t"POLL_SECONDS": 60' in out and out.endswith(b"}\n")
    got = json.loads(out.decode("ascii"))
    assert got["POLL_SECONDS"] == 60 and providers(dest) == ["codex"]


def test_tc1217_unicode_is_written_as_ascii_escapes_with_the_same_value(tmp_path):
    doc = '{"ACCESS_ENABLED": false, "label": "caf\u00e9 \u2603"}\n'
    env, source, dest = make_raw(tmp_path, "AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=codex\n", doc)
    MODULE.install(env, source, dest)
    out = dest.read_bytes()
    out.decode("ascii")
    assert b"\\u00e9" in out
    assert json.loads(out.decode("ascii"))["label"] == "caf\u00e9 \u2603"
