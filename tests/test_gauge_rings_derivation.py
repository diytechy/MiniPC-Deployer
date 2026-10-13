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

# Sibling resolution honours OWN_REPO so a worktree can point at its own panel
# checkout (precedent: TOUCH_APP_REPO in test_touch_filter_release.py).
OWN_REPO = Path(os.environ.get("OWN_REPO") or ROOT.parent / "OfficeWallNaglight")
GAUGES = OWN_REPO / "js/views/gauges.js"
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
    # Import the renderer under node and read the live export. The order moved
    # to js/gauge-order.js (re-exported by gauges.js), so a regex over
    # gauges.js's text finds nothing.
    if not NODE:
        pytest.skip("node not on PATH: USAGE_PROVIDER_ORDER not imported")
    program = ("import(process.argv[1]).then(m=>console.log(JSON.stringify("
               "m.USAGE_PROVIDER_ORDER)))")
    env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
    done = subprocess.run([NODE, "-e", program, GAUGES.as_uri()],
                          capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 0, done.stderr[-400:]
    order = json.loads(done.stdout)
    assert order and all(isinstance(x, str) for x in order)
    assert list(MODULE.PROVIDER_ORDER) == order


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


def block_of(dest):
    return json.loads(dest.read_text(encoding="utf-8"))["gaugeRings"]


BS = chr(92)  # a backslash, without fighting source-level escaping
ENABLED = "AI_USAGE_ENABLED=true\n"
FAIL_SAFE = {"slots": [], "unlisted": "append"}


def feeder_expected(value):
    """The REAL feeder's answer for a delivered value (None = unset)."""
    spec = importlib.util.spec_from_file_location("feeder_other_side", ROOT / "stack/ai-usage/ai_usage_feeder.py")
    feeder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(feeder)
    return feeder.enabled_sources({} if value is None else {"AI_USAGE_SOURCES": value})


TABLE = [
    ("", ["claude", "codex", "opencode"]),                       # unset
    ("AI_USAGE_SOURCES=\n", ["claude", "codex", "opencode"]),     # empty
    ("AI_USAGE_SOURCES=\"\"\n", ["claude", "codex", "opencode"]),  # empty, quoted
    ("AI_USAGE_SOURCES=   \n", ["claude", "codex", "opencode"]),  # unquoted whitespace: systemd trims
    ("AI_USAGE_SOURCES=\"   \"\n", []),                          # quoted whitespace
    ("AI_USAGE_SOURCES=,\n", []),                                # commas only
    ("AI_USAGE_SOURCES= , ,\n", []),
    ("AI_USAGE_SOURCES=codex,claude\n", ["claude", "codex"]),
    ("AI_USAGE_SOURCES='gemini'\n", ["gemini"]),
]


@pytest.mark.parametrize("line,expected", TABLE)
@pytest.mark.parametrize("start", ["absent", "default", "marked"])
def test_tc1215_every_derivable_block_follows_the_real_feeders_source_list(tmp_path, line, expected, start):
    block = {"absent": "absent", "default": dict(FAIL_SAFE),
             "marked": {"slots": [{"provider": "grok"}], "unlisted": "hide",
                        "derivedFrom": "AI_USAGE_SOURCES"}}[start]
    doc = json.dumps({"ACCESS_ENABLED": False} if block == "absent" else {"ACCESS_ENABLED": False, "gaugeRings": block})
    env, source, dest = make_raw(tmp_path, ENABLED + line, doc)
    MODULE.install(env, source, dest)
    assert providers(dest) == expected
    assert block_of(dest)["unlisted"] == "hide" and block_of(dest)["derivedFrom"] == "AI_USAGE_SOURCES"
    value = None
    if line:
        value = line.split("=", 1)[1].rstrip("\n")
        value = value[1:-1] if value[:1] in "\"'" and value[:1] else value.strip()
    assert sorted(providers(dest)) == sorted(feeder_expected(value)), "renderer disagrees with the feeder"


def test_tc1215_comment_lines_are_not_assignments_and_last_assignment_wins(tmp_path):
    text = ("# AI_USAGE_SOURCES=grok\n; AI_USAGE_SOURCES=grok\n" + ENABLED +
            "AI_USAGE_SOURCES=gemini\nAI_USAGE_SOURCES=\"codex\"\n")
    env, source, dest = make_raw(tmp_path, text)
    MODULE.install(env, source, dest)
    assert providers(dest) == ["codex"]


UNSUPPORTED = [
    ("indent", ENABLED + " AI_USAGE_SOURCES=codex\n"),
    ("space before =", ENABLED + "AI_USAGE_SOURCES =codex\n"),
    ("export", ENABLED + "export AI_USAGE_SOURCES=codex\n"),
    ("backslash", ENABLED + "AI_USAGE_SOURCES=codex,\\\nclaude\n"),
    ("backslash inside", ENABLED + "AI_USAGE_SOURCES=co\\dex\n"),
    ("multiline quote", ENABLED + "AI_USAGE_SOURCES=\"codex,\nclaude\"\n"),
    ("unmatched quote", ENABLED + "AI_USAGE_SOURCES=\"codex\n"),
    ("enabled indent", " AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=codex\n"),
    ("enabled export", "export AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=codex\n"),
    ("enabled space", "AI_USAGE_ENABLED =true\nAI_USAGE_SOURCES=codex\n"),
]


@pytest.mark.parametrize("label,text", UNSUPPORTED, ids=[u[0] for u in UNSUPPORTED])
@pytest.mark.parametrize("start", ["absent", "marked"])
def test_tc1218_unsupported_final_assignment_installs_the_unmarked_append_default(tmp_path, capsys, label, text, start):
    base = {"ACCESS_ENABLED": False}
    if start == "marked":
        base["gaugeRings"] = {"slots": [{"provider": "gemini"}], "unlisted": "hide", "derivedFrom": "AI_USAGE_SOURCES"}
    env, source, dest = make_raw(tmp_path, text, json.dumps(base))
    MODULE.install(env, source, dest)
    assert block_of(dest) == FAIL_SAFE, "stale or marked block retained"
    out = capsys.readouterr().out
    assert len([l for l in out.splitlines() if "WARNING" in l]) == 1
    assert "AI_USAGE_" in out


def test_tc1218_unsupported_form_leaves_an_owner_written_block_alone(tmp_path):
    owner = {"slots": [{"provider": "grok"}], "unlisted": "hide"}
    env, source, dest = make_raw(tmp_path, ENABLED + "export AI_USAGE_SOURCES=codex\n",
                                 json.dumps({"ACCESS_ENABLED": False, "gaugeRings": owner}))
    MODULE.install(env, source, dest)
    assert dest.read_bytes() == source.read_bytes()


def test_tc1218_final_canonical_assignment_wins_over_earlier_canonical_ones(tmp_path):
    stale = {"slots": [{"provider": "gemini"}], "unlisted": "hide", "derivedFrom": "AI_USAGE_SOURCES"}
    text = ENABLED + "AI_USAGE_SOURCES=gemini\nAI_USAGE_SOURCES='claude'\nAI_USAGE_SOURCES=codex\n"
    env, source, dest = make_raw(tmp_path, text, json.dumps({"ACCESS_ENABLED": False, "gaugeRings": stale}))
    MODULE.install(env, source, dest)
    assert providers(dest) == ["codex"] and "derivedFrom" in block_of(dest)


@pytest.mark.parametrize("text", [
    "AI_USAGE_SOURCES=codex\n",                           # key missing
    "AI_USAGE_ENABLED=\nAI_USAGE_SOURCES=codex\n",        # empty value
    "AI_USAGE_ENABLED=false\nAI_USAGE_SOURCES=codex\n",
    "AI_USAGE_ENABLED=True\nAI_USAGE_SOURCES=codex\n",
    "AI_USAGE_ENABLED=true # on\nAI_USAGE_SOURCES=codex\n",
])
def test_tc1218_not_exactly_true_is_not_derived(tmp_path, capsys, text):
    env, source, dest = make_raw(tmp_path, text)
    MODULE.install(env, source, dest)
    assert dest.read_bytes() == source.read_bytes()
    if "True" in text or "# on" in text:
        assert "gaugeRings:" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["codex,claude # note", "codex,mystery", "Codex"])
def test_tc1218_a_list_the_feeder_refuses_installs_the_append_default(tmp_path, capsys, value):
    env, source, dest = make_raw(tmp_path, ENABLED + "AI_USAGE_SOURCES=" + value + "\n")
    with pytest.raises(SystemExit):
        feeder_expected(value)  # the other side really does refuse it
    MODULE.install(env, source, dest)
    assert block_of(dest) == FAIL_SAFE
    assert "WARNING" in capsys.readouterr().out


def test_tc1218_feeder_module_unavailable_installs_the_append_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(MODULE, "FEEDER_PATH", tmp_path / "absent.py")
    env, source, dest = make_raw(tmp_path, ENABLED + "AI_USAGE_SOURCES=codex\n")
    MODULE.install(env, source, dest)
    assert block_of(dest) == FAIL_SAFE
    assert "WARNING" in capsys.readouterr().out


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


# ---- fix round 3: a multiline value makes assignment boundaries ambiguous ----

MULTILINE_ENVS = {
    "other key's quoted value hides a fake assignment":
        'AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=gemini\nOTHER="text SECRETVALUE\nAI_USAGE_SOURCES=codex\n"\n',
    "AI_USAGE_SOURCES itself is multiline with a fake assignment":
        'AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES="gemini\nAI_USAGE_SOURCES=codex"\n',
    "other key continues with a backslash":
        'AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=gemini\nOTHER=a SECRETVALUE\\\nAI_USAGE_SOURCES=codex\n',
}


@pytest.mark.parametrize("label", list(MULTILINE_ENVS))
def test_tc1218_multiline_value_anywhere_installs_the_unmarked_append_default(tmp_path, capsys, label):
    stale = {"slots": [{"provider": "gemini"}], "unlisted": "hide", "derivedFrom": "AI_USAGE_SOURCES"}
    env, source, dest = make_raw(tmp_path, MULTILINE_ENVS[label],
                                 json.dumps({"ACCESS_ENABLED": False, "gaugeRings": stale}))
    MODULE.install(env, source, dest)
    assert block_of(dest) == FAIL_SAFE
    out = capsys.readouterr().out
    warnings = [l for l in out.splitlines() if "WARNING" in l]
    assert len(warnings) == 1 and re.search(r"line \d+", warnings[0])
    assert "SECRETVALUE" not in out, "a value was printed"


def test_tc1218_multiline_value_leaves_an_owner_block_alone(tmp_path):
    owner = {"slots": [{"provider": "grok"}], "unlisted": "hide"}
    env, source, dest = make_raw(tmp_path, MULTILINE_ENVS["other key's quoted value hides a fake assignment"],
                                 json.dumps({"ACCESS_ENABLED": False, "gaugeRings": owner}))
    MODULE.install(env, source, dest)
    assert dest.read_bytes() == source.read_bytes()


def test_tc1218_closed_quotes_on_other_keys_do_not_trigger_the_guard(tmp_path):
    env, source, dest = make_raw(tmp_path, 'OTHER="two words"\nAI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=codex\n')
    MODULE.install(env, source, dest)
    assert providers(dest) == ["codex"]


# ---- fix round 4: ANY backslash in a value makes the file ambiguous ----

def test_tc1218_escaped_quote_cannot_hide_a_fake_assignment(tmp_path, capsys):
    stale = {"slots": [{"provider": "gemini"}], "unlisted": "hide", "derivedFrom": "AI_USAGE_SOURCES"}
    text = ('AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=gemini\n'
            'OTHER="text SECRETVALUE \\"still open\nAI_USAGE_SOURCES=codex\n"\n')
    assert '\\"' in text
    env, source, dest = make_raw(tmp_path, text, json.dumps({"ACCESS_ENABLED": False, "gaugeRings": stale}))
    MODULE.install(env, source, dest)
    assert block_of(dest) == FAIL_SAFE
    out = capsys.readouterr().out
    assert re.search(r"line \d+ \(OTHER\)", out) and "SECRETVALUE" not in out


# ---- fix round 5: a positive whitelist for every line of the .env ----

TEMPLATE = ROOT / "stack/.env.example"


def test_tc1218_the_shipped_env_template_passes_the_whitelist():
    """If this fails the whitelist would disable derivation on every hub."""
    assert MODULE.whitelist_failure(TEMPLATE.read_text(encoding="utf-8")) is None


@pytest.mark.parametrize("text", [
    "", "\n\n", "  \n", "# note\n", "  ; note\n","A=b\n", "A_1=\n", "A=\"two words\"\n", "A='x y'\n",
    "A=has # inline and = signs\n", "A=b\r\nC=d\r\n", "# it's \"quoted\" text\nA=1\n",
])
def test_tc1218_whitelist_passes_blank_comment_and_canonical_lines(text):
    assert MODULE.whitelist_failure(text) is None


@pytest.mark.parametrize("text,key", [
    (" A=b\n", "A"), ("export A=b\n", "A"), ("A =b\n", "A"), ("A=1\nB=\"x\" y\n", "B"),
    ("A=b" + BS + "\n", "A"), ("A=\"b" + BS + "\"c\"\n", "A"), ("A=\"b\n", "A"), ("A=b\"c\n", "A"),
    ("A=b'c\n", "A"), ("A=\"\" \"x\n", "A"), ("A=\"x\"\"y\"\n", "A"), ("A='x\"y\n", "A"),
    ("just words\n", None), ("1A=b\n", None), ("A=\"x\" \n", "A"),
    ("A=\"x" + BS + "y\"\n", "A"), ("A='x" + BS + "y'\n", "A"), ("A='x' y\n", "A"), ("A='x'y'\n", "A"),
    ("A='x''y'\n", "A"), ("A='x\n", "A"),
])
def test_tc1218_whitelist_rejects_everything_else_naming_the_line_and_key(text, key):
    number, found = MODULE.whitelist_failure(text)
    assert found == key
    assert number == (2 if text.startswith("A=1\n") else 1)


SOL_REPROS = {
    "round 3: another key's multiline value": 'OTHER="text\nAI_USAGE_SOURCES=codex\n"\n',
    "round 3: AI_USAGE_SOURCES itself multiline": 'AI_USAGE_SOURCES="gemini\nAI_USAGE_SOURCES=codex"\n',
    "round 4: escaped quote": 'OTHER="text ' + BS + '"still open\nAI_USAGE_SOURCES=codex\n"\n',
    "round 5: closed pair then open quote": 'OTHER="" "still open\nAI_USAGE_SOURCES=codex\n"\n',
    "round 5: stray text after a quoted value": 'OTHER="a" b"\nAI_USAGE_SOURCES=codex\n"\n',
}


@pytest.mark.parametrize("label", list(SOL_REPROS))
def test_tc1218_every_reviewer_repro_fails_safe_with_gemini_active(tmp_path, capsys, label):
    stale = {"slots": [{"provider": "gemini"}], "unlisted": "hide", "derivedFrom": "AI_USAGE_SOURCES"}
    text = "AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=gemini\n" + SOL_REPROS[label]
    env, source, dest = make_raw(tmp_path, text, json.dumps({"ACCESS_ENABLED": False, "gaugeRings": stale}))
    MODULE.install(env, source, dest)
    assert block_of(dest) == FAIL_SAFE
    out = capsys.readouterr().out
    assert len([l for l in out.splitlines() if "WARNING" in l]) == 1 and re.search(r"line \d+ \(", out)


def test_tc1218_non_assignment_line_is_named_as_such(tmp_path, capsys):
    env, source, dest = make_raw(tmp_path, "AI_USAGE_ENABLED=true\nSECRETWORDS here\n")
    MODULE.install(env, source, dest)
    assert block_of(dest) == FAIL_SAFE
    out = capsys.readouterr().out
    assert "line 2 (non-assignment line)" in out and "SECRETWORDS" not in out


def test_tc1218_file_without_ai_usage_is_not_checked(tmp_path):
    env, source, dest = make_raw(tmp_path, "OTHER=\"a\\\"b\nPANEL_ACCESS_ENABLED=false\n")
    MODULE.install(env, source, dest)
    assert dest.read_bytes() == source.read_bytes()


# ---- fix round 6: the keys the derivation reads must be plain ASCII ----

NBSP = chr(0xA0)


@pytest.mark.parametrize("value", [
    NBSP, "codex" + NBSP, chr(0xE9) + "codex", "co" + chr(1) + "dex", "codex" + chr(11), "codex" + chr(0x2003),
    "\"" + NBSP + "\"",
])
@pytest.mark.parametrize("key", ["AI_USAGE_SOURCES", "AI_USAGE_ENABLED"])
def test_tc1218_non_ascii_or_control_in_a_read_key_fails_safe(tmp_path, capsys, key, value):
    stale = {"slots": [{"provider": "gemini"}], "unlisted": "hide", "derivedFrom": "AI_USAGE_SOURCES"}
    lines = {"AI_USAGE_ENABLED": "true", "AI_USAGE_SOURCES": "gemini"}
    lines[key] = value if key == "AI_USAGE_SOURCES" else value
    text = "".join("%s=%s\n" % (k, v) for k, v in lines.items()).replace(chr(13), chr(13))
    env, source, dest = make_raw(tmp_path, text, json.dumps({"ACCESS_ENABLED": False, "gaugeRings": stale}))
    MODULE.install(env, source, dest)
    assert block_of(dest) == FAIL_SAFE
    out = capsys.readouterr().out
    assert len([l for l in out.splitlines() if "WARNING" in l]) == 1 and key in out


def test_tc1218_non_ascii_in_other_keys_is_still_fine(tmp_path):
    text = "NOTE=caf" + chr(0xE9) + " " + NBSP + "\n" + ENABLED + "AI_USAGE_SOURCES=codex\n"
    env, source, dest = make_raw(tmp_path, text)
    MODULE.install(env, source, dest)
    assert providers(dest) == ["codex"]


def test_tc1218_tabs_and_spaces_are_trimmed_from_a_bare_value_like_systemd(tmp_path):
    tab = chr(9)
    text = "AI_USAGE_ENABLED=" + tab + "true " + tab + "\nAI_USAGE_SOURCES=" + tab + " codex," + tab + "claude " + tab + "\n"
    env, source, dest = make_raw(tmp_path, text)
    MODULE.install(env, source, dest)
    assert providers(dest) == ["claude", "codex"]



# ---- fix round 7: blank / comment / indent use systemd's ASCII whitespace only ----

@pytest.mark.parametrize("space", [chr(0xA0), chr(0x2003), chr(0x3000), chr(0x0B), chr(0x0C)],
                         ids=["nbsp", "em space", "ideographic", "vt", "ff"])
@pytest.mark.parametrize("lead", ["#", ";", ""])
def test_tc1218_a_unicode_space_line_is_not_blank_not_a_comment_and_fails_safe(tmp_path, capsys, space, lead):
    stale = {"slots": [{"provider": "gemini"}], "unlisted": "hide", "derivedFrom": "AI_USAGE_SOURCES"}
    # Sol's repro: the first line looks like a comment to Python's str.lstrip() only.
    text = ("AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=gemini\n" + space + lead + "OTHER=\"open\n"
            "AI_USAGE_SOURCES=codex\n#\"\n")
    env, source, dest = make_raw(tmp_path, text, json.dumps({"ACCESS_ENABLED": False, "gaugeRings": stale}))
    MODULE.install(env, source, dest)
    assert block_of(dest) == FAIL_SAFE
    out = capsys.readouterr().out
    assert len([l for l in out.splitlines() if "WARNING" in l]) == 1 and "line 3" in out


def test_tc1218_a_unicode_space_only_line_is_not_blank(tmp_path):
    assert MODULE.whitelist_failure("A=1\n" + chr(0xA0) + "\n") == (2, None)
    assert MODULE.whitelist_failure("A=1\n" + chr(0x2003) + "\n") == (2, None)


def test_tc1218_ascii_blank_and_comment_lines_with_tabs_and_cr_still_pass():
    tab = chr(9)
    text = tab + " " + tab + "\r\n" + " " + tab + "# note\r\n" + tab + "; note\nA=1\r\n"
    assert MODULE.whitelist_failure(text) is None


def test_tc1218_a_carriage_return_inside_blank_space_is_ascii_whitespace():
    assert MODULE.whitelist_failure("A=1\n " + chr(13) + " \n") is None


def test_tc1218_a_backslash_comment_swallows_the_next_line_in_systemd_so_it_fails_safe(tmp_path, capsys):
    stale = {"slots": [{"provider": "gemini"}], "unlisted": "hide", "derivedFrom": "AI_USAGE_SOURCES"}
    text = "AI_USAGE_ENABLED=true\nAI_USAGE_SOURCES=gemini\n# note " + BS + "\nAI_USAGE_SOURCES=codex\n"
    env, source, dest = make_raw(tmp_path, text, json.dumps({"ACCESS_ENABLED": False, "gaugeRings": stale}))
    MODULE.install(env, source, dest)
    assert block_of(dest) == FAIL_SAFE
    out = capsys.readouterr().out
    assert "line 3 (non-assignment line)" in out


@pytest.mark.parametrize("lead", ["#", ";", "  #"])
def test_tc1218_backslash_anywhere_in_a_comment_fails_the_whitelist(lead):
    assert MODULE.whitelist_failure("A=1\n" + lead + " a" + BS + "b c\n") == (2, None)
