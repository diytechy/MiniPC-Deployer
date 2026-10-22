"""TC-1365: the argv the AI CLI service builds is accepted by the REAL claude CLI.

Every unit test of `build_argv` checks the argv against this repo's own idea of
the CLI, which is how the service shipped a schema FILE PATH to
`claude --json-schema` for six weeks: the tests agreed with the code, and the
CLI refused every request with "--json-schema is not valid JSON" before a model
was called. The only enabled route had never answered (HomeHub FOLLOWUPS,
2026-10-21 deploy notes).

So this runs the real binary, through the service's own `build_argv` and
`run_session`, with the shipped ANALYSIS-QUICK row. It is made safe and free by
taking every credential away: HOME, USERPROFILE and CLAUDE_CONFIG_DIR point at an
empty directory, the auth variables are removed, and ANTHROPIC_BASE_URL points
at a closed local port. A CLI that ACCEPTED its argv then reaches the sign-in
check and returns a `type: result` envelope saying it is not logged in; one that
refused its argv prints the parse error and no envelope. That distinction is the
whole test. It costs no API call.

SKIPPED where no `claude` is on PATH (the hub does not run tests; the dev PC
has the CLI). What it cannot show: a signed-in answer, or the structured_output
field of one - that is the live `POST /v1/ask` check after deploy.

Verifies: TC-1365 (SR-019, LLR-1213)
"""
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AI_CLI = REPO / "stack" / "ai-cli"
REGISTRY = AI_CLI / "agents.registry.csv"
sys.path.insert(0, str(AI_CLI))
import ai_cli_service as svc  # noqa: E402

CLAUDE = shutil.which("claude")
needs_claude = pytest.mark.skipif(CLAUDE is None, reason="no claude CLI on PATH")

AUTH_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "AWS_BEARER_TOKEN_BEDROCK",
)
PARSE_REFUSAL = "is not valid JSON"


def _isolate(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "cfg").mkdir(parents=True)
    for var in AUTH_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "cfg"))
    # Belt to the missing credential: even a credential found somewhere this
    # test did not think of has nowhere to go.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")


def _envelopes(output):
    found = []
    for line in (output or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                found.append(json.loads(line))
            except ValueError:
                pass
    return found


def _run_real(tmp_path, argv_mutator=None):
    routes, errors = svc.load_registry(str(REGISTRY))
    assert errors == [], errors
    route = routes["ANALYSIS-QUICK"]
    schema = {"type": "object", "properties": {"category": {"type": "string"}},
              "required": ["category"]}
    bin_dir = os.path.dirname(CLAUDE)
    with svc.request_scratch(str(tmp_path / "scratch")) as scratch:
        argv, env_overrides = svc.build_argv(route, scratch, bin_path=bin_dir, schema=schema)
        with open(scratch["schema_path"], "w", encoding="utf-8") as fh:
            json.dump(schema, fh)
        if argv_mutator:
            argv = argv_mutator(argv, scratch)
        code, output, timed_out = svc.run_session(
            argv, scratch["cwd"], 120, env_overrides, "USER: categorise this\n", bin_path=bin_dir
        )
    return argv, code, output, timed_out


@needs_claude
def test_tc1365_the_real_cli_accepts_the_built_argv_llr1213(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    argv, code, output, timed_out = _run_real(tmp_path)
    assert not timed_out
    assert PARSE_REFUSAL not in output, output[-2000:]
    results = [e for e in _envelopes(output) if e.get("type") == "result"]
    assert results, "the CLI produced no result envelope - it did not get past its argv: " + output[-2000:]
    # No credential, so the envelope is an error - which proves the argv parsed
    # and the run reached the sign-in check, and that nothing was billed.
    assert results[-1].get("is_error") is True
    assert results[-1].get("total_cost_usd", 0) == 0


@needs_claude
def test_tc1365_the_old_file_path_argv_is_what_the_cli_refused_llr1213(tmp_path, monkeypatch):
    """The control: the pre-2026-10-22 argv (the schema FILE path after
    --json-schema) is refused by the same CLI with the error the hub logged.
    Without this, the test above could pass for a reason unrelated to the fix."""
    _isolate(monkeypatch, tmp_path)

    def old_argv(argv, scratch):
        i = argv.index("--json-schema")
        return argv[:i + 1] + [scratch["schema_path"]] + argv[i + 2:]

    _, _, output, _ = _run_real(tmp_path, old_argv)
    assert PARSE_REFUSAL in output, output[-2000:]
    assert not [e for e in _envelopes(output) if e.get("type") == "result"]
