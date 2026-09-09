"""The two feeders duplicate their guards — this is what keeps them in step.

WHY THERE ARE TWO COPIES AT ALL. `ai_usage_feeder.py` and `weight_feeder.py`
ship as STANDALONE SCRIPTS: separate systemd units, separate unprivileged
accounts, separate `StateDirectory=` roots, each run as
`/usr/bin/python3 /opt/homehub/stack/<service>/<file>.py` under
`ProtectSystem=strict`. There is no importable module between them and putting
one there would be a carriage change (a third install path, a `sys.path` the
units have to agree on, and a shared failure surface for two services that are
required to fail independently). B11 duplicated B7's shell for that reason and
this round did not reverse the decision.

THE COST OF THAT DECISION IS THIS FILE. A mirrored fix is only a fix while both
copies still have it, and "we remembered to mirror it" is not a property a
reviewer can check. So the guards that were WRONG IN BOTH FEEDERS are asserted
here against BOTH modules, by behaviour rather than by comparing source text —
a text comparison would pass on two identically broken copies, which is exactly
the state the cross-review found.

Verifies: TC-005, TC-006 (SR-021, SR-022)
"""

import ast
import importlib.util
import io
import os
import sys
import tokenize
import urllib.request
from pathlib import Path

import pytest

from conftest import loopback_server, plain_200, redirect_to

REPO = Path(__file__).resolve().parents[1]

MODULES = {
    "ai-usage": REPO / "stack" / "ai-usage" / "ai_usage_feeder.py",
    "weight": REPO / "stack" / "weight" / "weight_feeder.py",
}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_parity", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FEEDERS = {name: _load(name, path) for name, path in MODULES.items()}
NAMES = sorted(FEEDERS)


def code_lines(path):
    """The module's CODE, with comments and string literals blanked out.

    The docstrings in these two feeders QUOTE the very patterns being banned —
    that is what a comment explaining a defect looks like — so a plain
    substring scan would go red on the explanation and green on the offence.
    This is the vacuous-substring trap this build has now found several times,
    met the same way it was met in the unit-file checks: match the code, not
    the prose.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    with io.open(str(path), "rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type not in (tokenize.COMMENT, tokenize.STRING):
                continue
            # Blank the token's own COLUMNS, not its whole lines: a line like
            # `failures.append("%s: %s" % (name, type(exc).__name__))` is half
            # string and half code, and erasing the line would hide the code.
            for number in range(token.start[0], token.end[0] + 1):
                line = lines[number - 1]
                first = token.start[1] if number == token.start[0] else 0
                last = token.end[1] if number == token.end[0] else len(line)
                lines[number - 1] = line[:first] + " " * (last - first) + line[last:]
    return lines


@pytest.mark.parametrize("name", NAMES)
def test_no_feeder_reaches_for_the_default_opener(name):
    """`urllib.request.urlopen` IS the defect, in source form.

    Every one of the four call sites the cross-review found — two readers and
    two posters — was a bare `urlopen(Request(...))`, and the default opener
    both follows redirects and honours `http_proxy`. A structural check is
    worth having here on top of the behavioural ones because the weight
    feeder's vendor half HAS NOT BEEN WRITTEN YET: this is what fails if the
    session that finally writes the Google Health reader reaches for the same
    convenient function.
    """
    offenders = [number for number, line in enumerate(code_lines(MODULES[name]), 1)
                 if "urlopen(" in line]
    assert offenders == [], (
        "%s calls urlopen at line(s) %s. Use feed_opener() for the POST and "
        "vendor_opener() for an outbound call: the default opener follows "
        "redirects and honours http_proxy." % (name, offenders))


@pytest.mark.parametrize("name", NAMES)
def test_every_feeder_refuses_a_redirect_on_the_feed_post(name):
    feeder = FEEDERS[name]
    with loopback_server(plain_200) as (elsewhere, arrived):
        with loopback_server(redirect_to(elsewhere + "/api/feed")) as (front, _):
            request = urllib.request.Request(
                front + "/api/feed", data=b"{}",
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer PARITY-TOKEN"},
                method="POST")
            with pytest.raises(feeder.EgressRefused):
                feeder.feed_opener().open(request, timeout=10)
        assert arrived == [], "%s re-sent the POST to the redirect target" % name


@pytest.mark.parametrize("name", NAMES)
def test_every_feeder_installs_no_proxy_on_either_opener(name, monkeypatch):
    """Both openers, because they were both built from the same default."""
    feeder = FEEDERS[name]
    with loopback_server(plain_200) as (proxy_url, proxy_seen):
        with loopback_server(plain_200) as (target, target_seen):
            for variable in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
                monkeypatch.setenv(variable, proxy_url)
            for opener in (feeder.feed_opener(), feeder.vendor_opener()):
                request = urllib.request.Request(
                    target + "/api/feed",
                    headers={"Authorization": "Bearer PARITY-TOKEN"})
                opener.open(request, timeout=10).read()
    assert proxy_seen == [], "%s sent a bearer token through a LAN proxy" % name
    assert len(target_seen) == 2


@pytest.mark.parametrize("name", NAMES)
def test_every_feeder_resolves_symlinks_in_its_write_guard(name, tmp_path):
    """The V1 defect, stated once against both copies.

    The verdict is handed a resolver that models a symlink, so this asserts the
    DECISION rather than the filesystem: a `.tmp` name that RESOLVES to a
    credential must be refused, and a guard built on `os.path.abspath` cannot
    see that at all.
    """
    feeder = FEEDERS[name]
    state_dir = tmp_path / name / "state"
    state = str(state_dir / "feeder-state.json")
    token = str(tmp_path / name / "vendor" / "credentials.json")

    def resolve_like_a_symlink(path):
        return token if path == state + ".tmp" else os.path.abspath(path)

    assert feeder.writable_path_verdict(
        state + ".tmp", state, str(state_dir), resolve=os.path.abspath) is None
    assert feeder.writable_path_verdict(
        state + ".tmp", state, str(state_dir), resolve=resolve_like_a_symlink)


@pytest.mark.parametrize("name", NAMES)
def test_every_feeder_binds_its_writes_to_its_own_state_directory(name, tmp_path):
    feeder = FEEDERS[name]
    state_dir = tmp_path / name / "state"
    state_dir.mkdir(parents=True)
    outside = tmp_path / name / "elsewhere" / "state.json"
    outside.parent.mkdir(parents=True)
    assert feeder.writable_path_verdict(str(outside), str(outside), str(state_dir))
    assert feeder.resolve_state_root({}) == feeder.DEFAULT_STATE_ROOT
    assert feeder.DEFAULT_STATE_ROOT.startswith("/var/lib/homehub-")


@pytest.mark.parametrize("name", NAMES)
def test_no_feeder_logs_a_remote_body(name):
    """The journal sink, stated structurally against both copies: reading an
    error body is the only way one can get there, and neither feeder needs to."""
    offenders = [number for number, line in enumerate(code_lines(MODULES[name]), 1)
                 if "exc.read()" in line]
    assert offenders == [], (
        "%s reads a remote error body at line(s) %s; `main` prints that to "
        "stderr and systemd writes it to the journal." % (name, offenders))


@pytest.mark.parametrize("name", NAMES)
def test_no_feeder_prints_an_exception_message_for_an_unexpected_error(name):
    """B7 logged only the exception TYPE; B11's broad catch printed `str(exc)`.

    Once the real OAuth reader exists, an exception raised inside urllib
    carries the request object, so its message can contain an Authorization
    header. Both feeders now name the type and nothing else, and this reads the
    catch itself rather than trusting a comment about it.
    """
    tree = ast.parse(MODULES[name].read_text(encoding="utf-8"))
    handlers = [node for node in ast.walk(tree)
                if isinstance(node, ast.ExceptHandler)
                and isinstance(node.type, ast.Name)
                and node.type.id == "Exception"]
    assert handlers, "%s has no broad catch to check" % name

    for handler in handlers:
        # THE CHECK IS ON THE AST, NOT ON A WINDOW OF LINES. A line-range
        # heuristic reads whatever happens to follow the handler and calls it
        # evidence; this reads the handler's OWN body, so the answer does not
        # change when an unrelated statement moves.
        blessed = set()
        for node in ast.walk(handler):
            if isinstance(node, ast.Attribute) and node.attr == "__name__" \
                    and isinstance(node.value, ast.Call) \
                    and isinstance(node.value.func, ast.Name) \
                    and node.value.func.id == "type":
                blessed.update(id(arg) for arg in node.value.args)
        leaked = [node for node in ast.walk(handler)
                  if isinstance(node, ast.Name) and node.id == handler.name
                  and id(node) not in blessed]
        assert leaked == [], (
            "%s: the broad catch at line %d uses the exception itself at "
            "line(s) %s. Only `type(exc).__name__` may be reported: once the "
            "real OAuth reader exists, an exception raised inside urllib "
            "carries the request object and its message can contain an "
            "Authorization header."
            % (name, handler.lineno, [node.lineno for node in leaked]))
