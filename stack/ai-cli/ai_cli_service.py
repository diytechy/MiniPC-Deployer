#!/usr/bin/env python3
"""The hub's AI CLI service: messages in, a schema-constrained answer out.

ONE RESPONSIBILITY: turn an HTTP request carrying messages into ONE headless
CLI session, run under the four containments A40 ratified on 2026-09-09, and
return the structured result. It is a plain hub service - no container. A40's
own reasoning: a container was only ever wanted for containment, and it
delivers none here, because the subscription credential has to come from a home
directory a human logged into interactively, so a container must mount that
home and the isolation is punctured at the only point that mattered.

WHAT IS REUSED, AND WHAT IS DELIBERATELY NOT
    Reused from `ai-template/project-trajectory/scripts` - its *structure*, the
    debugged half A40 says not to rebuild:
      * the pair-row registry and its loader (agent_route.load_registry, its
        IF-045 schema) - one row per (model x route), the row IS the allow
        matrix, the id is a join key and is never parsed;
      * the per-row cooldown, independent by construction;
      * the session-launch decisions (agent_session, its IF-064): a template
        with no `{prompt}` pipes the prompt to the child's STDIN so a large
        prompt never meets the OS command-line cap (WI-216); codex captures
        `--output-last-message` because it echoes the prompt into stdout
        (WI-217); a reader thread so the child cannot block on a full pipe; a
        per-session timeout; stdin never left open on an interactive wait.
    NOT reused - its FLAGS. Every Claude row in `ai-template/docs/agents.toml`
    carries a permission-skipping flag and every codex row a sandbox-bypassing
    one (README.md names both; it is the one file here allowed to write them
    out, so the tree-wide grep that bans them does not fail on its own
    documentation). That file says why: that repo IS the unattended run and
    consent is explicit. This service's work is chosen by a caller, so the
    consent does not transfer, and copying the flags would make the service
    arbitrary code execution on request.

THE FOUR CONTAINMENTS, each with the symbol that enforces it and the test that
asserts it (tests/test_ai_cli_service.py; the acceptance is the point of the
block, so none of them is left to be asserted by eye in a unit file):
    1. a dedicated unprivileged account, never `hub` -> `assert_effective_account`
       then `assert_service_account`
    2. read-only tool use, no permission-skipping flag -> `assert_safe_template`
    3. bound to loopback or a REAL docker bridge address -> `resolve_bind`
    4. a scratch working directory per request       -> `request_scratch`

WHAT THE 2026-09-09 CROSS-REVIEW CHANGED, and why each change is structural
rather than one more string check. The review's verdict was that these guards
checked *presence* where they had to check *identity*:
    V1 The command template is now validated against a per-family CONTRACT:
       the executable is pinned by name AND resolved to an absolute path on a
       fixed search path, and EVERY token must belong to that executable's
       declared vocabulary with a value this file constrains. Previously the
       guard only asked whether certain tokens were present, so
       `python3 -c '...' --permission-mode plan --disallowedTools Bash` passed
       every check and ran arbitrary code as the service account.
    V2 The account is asserted as the EFFECTIVE identity of this process,
       compared against the configured name, so the account asserted and the
       account that runs are the same thing. Previously the unit hard-coded
       `User=homehub-ai` while the service validated whatever `AI_CLI_USER`
       said, and the two could differ. `setup-ai-cli.sh` now writes the unit's
       `User=`/`Group=` from that same knob into a drop-in, so they cannot.
    V3 "The docker bridge" is no longer the whole of 172.16.0.0/12 - a range
       that contains real household LANs (a home on 172.20.0.0/16 is inside
       it). A non-loopback bind must now be an address that a LOCAL DOCKER
       BRIDGE INTERFACE is actually carrying.

Contract:
  Inputs:  a POST of {"messages":[{"role","content"},...], "route": "<id>",
           "schema": {json-schema}}. `route` names a row in the registry AND
           the enable-list; model and depth are CONFIGURATION (the row), never
           request fields, so a caller can pick an allowed route and nothing
           else.
  Outputs: 200 with the CLI's structured result, constrained by the supplied
           schema; 4xx/5xx with a JSON {"error": ...}. Never a partial turn
           reported as success.
  Config:  AI_CLI_* keys, see `Config` and stack/.env.example.
  Raises:  UnsafeRouteError, BindRefused, ConfigError - all loudly, never a
           silent degrade to a less contained run.

Stdlib only, Python 3.8+ (the check harness runs 3.8 on the dev PC).
Implements: SR-019, LLR-002, LLR-004
"""

import csv
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Refusals. These are the block's acceptance criteria expressed as code, so a
# bad edit to tracked config is a launch failure rather than an exposure.
# ---------------------------------------------------------------------------

#: Any flag beginning with this is refused wherever it appears in a template.
#: ai-template's rows carry two of them; neither may reach this service. The
#: check is by PREFIX, not by name, so a dangerous flag a future CLI version
#: adds is caught before anyone updates this file. It is now a belt over the
#: braces of the per-family vocabulary below - an undeclared flag is refused
#: whatever it is called - kept because its refusal message is the one that
#: explains the trap to whoever tripped it.
DANGEROUS_FLAG_PREFIX = "--dangerously-"

#: `--bare` is the fast, predictable startup meant for scripts, and it CANNOT
#: be used here: it ignores the OAuth token and requires an API key (A40), and
#: this service leans on the subscription. Banned so nobody "optimises" the
#: latency floor away by silently moving the service onto a metered key.
BANNED_TOKENS = frozenset({"--bare"})

#: Accounts this service must never run as. `hub` is the box's only account
#: with a real shell that ALSO carries `(ALL) NOPASSWD: ALL`
#: (stack/remote-ui/homehub-desktop-session.sh) - a service running as `hub`,
#: driving an agent, taking requests from other containers is arbitrary code
#: execution as a passwordless-sudo user chosen by the caller.
FORBIDDEN_ACCOUNTS = frozenset({"hub", "root"})

#: Groups whose membership is a privilege grant. `docker` is sudo by another
#: door: a member can bind-mount `/` into a container it controls.
PRIVILEGED_GROUPS = frozenset({"sudo", "admin", "wheel", "root", "docker", "adm"})

#: The default search path for the pinned CLI binaries. Overridable with
#: AI_CLI_BIN_PATH because the Owner installs the CLIs by hand over SSH
#: (SN-016, deliberately outside the offline closure) and may put them
#: somewhere else - but NEVER from a registry row's `Env=`, which is the
#: injection surface the review found: a template that can set PATH can point
#: the pinned name at a planted binary.
DEFAULT_BIN_PATH = "/usr/local/bin:/usr/bin:/bin"

#: Environment variables a registry row's `Env=` cell may set, and the values
#: each may take. An ALLOW-LIST, not a deny-list: the review's finding was that
#: `Env=` was unguarded, and the dangerous keys are open-ended - anything that
#: selects the executable (PATH), its configuration (HOME, CODEX_HOME,
#: CLAUDE_CONFIG_DIR, XDG_CONFIG_HOME) or the loader (LD_PRELOAD).
ALLOWED_ENV = {
    "CLAUDE_CODE_EFFORT_LEVEL": frozenset({"low", "medium", "high"}),
}

#: Named only to make the refusal message useful; the allow-list above is what
#: actually enforces.
ENV_KEYS_THAT_SELECT_THE_BINARY = (
    "PATH",
    "HOME",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "PYTHONPATH",
    "NODE_OPTIONS",
)

#: The tools an analysis route may be ALLOWED. A40: read-only tool use, not no
#: tools - the model reads the data and reasons over it, and cannot mutate.
READ_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "NotebookRead"})

#: The tools an analysis route must explicitly DENY. Claude resolves deny, then
#: ask, then allow, and deny wins - so the deny list is the load-bearing half.
REQUIRED_DENIED_TOOLS = frozenset(
    {"Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch"}
)

ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]*$")
MODEL_SLUG_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")
CODEX_CONFIG_RE = re.compile(r"^model_reasoning_effort=(minimal|low|medium|high)$")
TIERS = ("quick", "medium", "strong")

#: Sudoers include directives. The review found that the parser treated
#: `#include /etc/other-sudoers` as a comment, so a NOPASSWD grant one file
#: away was invisible. All spellings are followed; an unfollowable one is a
#: refusal to certify rather than a silent pass.
SUDOERS_INCLUDE_RE = re.compile(r"^\s*(?:#|@)(include|includedir)\s+(\S+)\s*$")


class ConfigError(Exception):
    """Configuration that cannot be honoured. Always fatal at startup."""


class UnsafeRouteError(Exception):
    """A registry row that would run less contained than A40 ratified."""


class BindRefused(Exception):
    """A bind address that is neither loopback nor a real docker bridge."""


# ---------------------------------------------------------------------------
# 1. The registry - ai-template's IF-045 schema, its loader's shape.
# ---------------------------------------------------------------------------


class Route(object):
    """One (model x route) pair row. The row IS the allow matrix: a second
    account or a second depth is a second row with its own id and its own
    independent cooldown, never a request field.

    Implements: SR-019, LLR-002
    """

    __slots__ = (
        "id",
        "family",
        "model",
        "version",
        "tier",
        "cmd_template",
        "env",
        "notes",
    )

    def __init__(self, id, family, model, version, tier, cmd_template, env, notes):
        self.id = id
        self.family = family
        self.model = model
        self.version = version
        self.tier = tier
        self.cmd_template = cmd_template
        self.env = env
        self.notes = notes

    def __repr__(self):  # test readability
        return "Route({} {} {})".format(self.id, self.family, self.tier)


def _uncommented(lines):
    """Drop whole-line `#` comments and blanks so the header block above the
    CSV header row is documentation rather than data (DictReader would hand
    back a row of Nones for a blank line)."""
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#") or not stripped.strip():
            continue
        yield line


def load_registry(path):
    """Parse the route registry into ``({id: Route}, [error, ...])``.

    Mirrors ``agent_route.load_registry``: comment rows and ``-000``
    placeholders ship inert, a bad id / tier / model slug is an ERROR rather
    than a silently dropped row, and an unreadable file yields errors rather
    than an empty pool - ``{}`` would read as "no routes are enabled", which is
    a consent decision, not a broken file.

    The Model cell is slug-checked because it rides ``{model}`` substitution
    into an argv (ai-template's repo-review 2026-07-21 H-4).

    Contract:
      Inputs:  path: str - a CSV carrying the IF-045 columns.
      Outputs: (dict id->Route, list of human-readable error strings)
      Raises:  nothing; an unreadable file is reported as an error string.
    Implements: SR-019, LLR-002
    """
    routes, errors = {}, []
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            rows = list(csv.DictReader(_uncommented(fh)))
    except OSError as exc:
        return {}, ["cannot read {}: {}".format(path, exc)]

    def cell(row, name):
        return str(row.get(name) or "").strip()

    for row in rows:
        rid = cell(row, "Id")
        if not rid or rid.endswith("-000"):
            continue
        if not ID_RE.match(rid):
            errors.append("{}: id {!r} is not [A-Z0-9][A-Z0-9.-]*".format(path, rid))
            continue
        if rid in routes:
            errors.append("{}: duplicate id {!r}".format(path, rid))
            continue
        tier = cell(row, "Tier").lower() or "medium"
        if tier == "weak":
            tier = "quick"  # legacy vocabulary, same as ai-template
        if tier not in TIERS:
            errors.append(
                "{}: id {!r} has tier {!r}; expected one of {}".format(
                    path, rid, tier, "|".join(TIERS)
                )
            )
            continue
        model = cell(row, "Model")
        if model and not MODEL_SLUG_RE.match(model):
            errors.append(
                "{}: id {!r} Model {!r} has characters outside [A-Za-z0-9._:/-]; "
                "refusing (shell-unsafe in argv)".format(path, rid, model)
            )
            continue
        routes[rid] = Route(
            id=rid,
            family=cell(row, "Family").upper(),
            model=model,
            version=cell(row, "Version"),
            tier=tier,
            cmd_template=cell(row, "CmdTemplate"),
            env=cell(row, "Env"),
            notes=cell(row, "Notes"),
        )
    return routes, errors


def load_enable_list(path):
    """Ordered route ids a caller may name, one per line, `#` comments.

    An ABSENT enable-list is an empty pool, not "everything": the registry is a
    catalog and the enable-list is the consent. Callers get a 403 naming the
    allowed ids rather than a route nobody turned on.

    Implements: SR-019, LLR-002
    """
    ids = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line and line not in ids:
                    ids.append(line)
    except OSError:
        return []
    return ids


# ---------------------------------------------------------------------------
# 2. Containment 2: the command template, validated against a family CONTRACT.
#
# THE REVIEW'S WORST FINDING LIVES HERE. The first version of this guard asked
# only whether certain tokens were PRESENT, and never asked what the command
# actually was. `python3 -c '...' --permission-mode plan --disallowedTools Bash`
# satisfied every presence check, so a registry edit on the box was arbitrary
# code execution as the service account - and the registry being editable on
# the box is exactly why this is a runtime guard in the first place.
#
# So the contract below pins the EXECUTABLE per family and declares that
# executable's whole permitted vocabulary. A token that is not a declared flag,
# a declared flag's value, or a declared subcommand is refused. Adding a flag
# to a shipped row is therefore a deliberate, reviewed edit HERE, and not a
# registry cell nobody reads.
# ---------------------------------------------------------------------------


def _one_of(allowed):
    """Value validator: membership."""

    def check(value):
        if value not in allowed:
            return "expected one of {}".format("|".join(sorted(allowed)))
        return None

    return check


def _read_only_tool_list(value):
    """Value validator for `--allowedTools`: every named tool must be read-only.

    The review found this value was never inspected at all, so
    `--permission-mode plan --allowedTools Bash --disallowedTools Read` passed.
    """
    items = [v.strip() for v in value.split(",") if v.strip()]
    if not items:
        return "is empty; name the read-only tools explicitly"
    bad = [i for i in items if i not in READ_ONLY_TOOLS]
    if bad:
        return "allows non-read-only tool(s) {} (allowed: {})".format(
            ",".join(bad), ",".join(sorted(READ_ONLY_TOOLS))
        )
    return None


def _denies_the_mutating_tools(value):
    """Value validator for `--disallowedTools`: the deny list must cover every
    mutating tool. Deny wins in Claude's resolution order, so this is the
    load-bearing half of read-only tool use."""
    items = {v.strip() for v in value.split(",") if v.strip()}
    missing = sorted(REQUIRED_DENIED_TOOLS - items)
    if missing:
        return "does not deny {}".format(",".join(missing))
    return None


def _model_value(value):
    """Value validator for the model flag: a slug, or the `{model}` placeholder
    the loader has already slug-checked."""
    if value == "{model}" or MODEL_SLUG_RE.match(value):
        return None
    return "is not a model slug or the {model} placeholder"


def _codex_config_override(value):
    """Value validator for codex's `-c`: only the reasoning-effort knob.

    `-c` is a general config override - `-c sandbox_mode=...` would undo the
    sandbox this guard exists to enforce - so it is constrained to the one key
    the shipped row needs.
    """
    if CODEX_CONFIG_RE.match(value):
        return None
    return "is not an allowed -c override (only model_reasoning_effort=<effort>)"


class FamilyContract(object):
    """Everything a family's command template is allowed to be.

    Attributes:
      executables: the permitted argv[0] names (no path separators - the path
        is this file's to resolve, on AI_CLI_BIN_PATH, so a planted binary
        earlier on someone else's PATH cannot win).
      subcommands: the exact subcommand words that must follow, in order.
      bare_flags: flags taking no value.
      value_flags: flags taking exactly one value.
      validators: flag -> callable(value) -> error string or None.
      required: groups of alternatives; one member of each group must appear.
      service_flags: flags the SERVICE appends per request. Refused inside a
        template, because a per-request value written into tracked config is a
        value that stopped being per-request.
      schema_flag: how this family is handed the caller's JSON schema. A family
        with none cannot honour the output contract and is refused outright -
        the review found codex was written a schema file that was never passed.
      result: "stdout-json" | "last-message-file".
    """

    __slots__ = (
        "executables",
        "subcommands",
        "bare_flags",
        "value_flags",
        "validators",
        "required",
        "service_flags",
        "schema_flag",
        "result",
    )

    def __init__(
        self,
        executables,
        subcommands,
        bare_flags,
        value_flags,
        validators,
        required,
        service_flags,
        schema_flag,
        result,
    ):
        self.executables = frozenset(executables)
        self.subcommands = tuple(subcommands)
        self.bare_flags = frozenset(bare_flags)
        self.value_flags = frozenset(value_flags)
        self.validators = dict(validators)
        self.required = tuple(required)
        self.service_flags = frozenset(service_flags)
        self.schema_flag = schema_flag
        self.result = result


FAMILY_CONTRACTS = {
    # claude: `-p` is the headless one-shot driver; `--permission-mode plan`
    # plus an explicit deny list is read-only tool use.
    "ANTHROPIC": FamilyContract(
        executables=("claude",),
        subcommands=(),
        bare_flags=("-p", "--print"),
        value_flags=(
            "--model",
            "--output-format",
            "--permission-mode",
            "--allowedTools",
            "--disallowedTools",
        ),
        validators={
            "--model": _model_value,
            "--output-format": _one_of(frozenset({"json"})),
            "--permission-mode": _one_of(frozenset({"plan"})),
            "--allowedTools": _read_only_tool_list,
            "--disallowedTools": _denies_the_mutating_tools,
        },
        required=(
            ("-p", "--print"),
            ("--model",),
            ("--output-format",),
            ("--permission-mode",),
            ("--allowedTools",),
            ("--disallowedTools",),
        ),
        service_flags=("--json-schema",),
        schema_flag="--json-schema",
        result="stdout-json",
    ),
    # codex: read-only is one of three sandbox values; the approval policy has
    # to be `never` or an unattended run wedges waiting for a human.
    "OPENAI": FamilyContract(
        executables=("codex",),
        subcommands=("exec",),
        bare_flags=("--json",),
        value_flags=("--sandbox", "--ask-for-approval", "-m", "--model", "-c"),
        validators={
            "--sandbox": _one_of(frozenset({"read-only"})),
            "--ask-for-approval": _one_of(frozenset({"never"})),
            "-m": _model_value,
            "--model": _model_value,
            "-c": _codex_config_override,
        },
        required=(
            ("--json",),
            ("--sandbox",),
            ("--ask-for-approval",),
            ("-m", "--model"),
        ),
        service_flags=("--output-schema", "--output-last-message"),
        schema_flag="--output-schema",
        result="last-message-file",
    ),
}


def split_cmd(template):
    """Split a command template into argv tokens (ai-template's shell-string
    form, with backslash escaping DISABLED so Windows-style paths survive)."""
    lex = shlex.shlex(template or "", posix=True)
    lex.whitespace_split = True
    lex.escape = ""
    lex.commenters = ""
    return list(lex)


def assert_safe_template(route):
    """Refuse a route whose command template would run anything other than the
    pinned CLI, contained. Raises ``UnsafeRouteError`` naming the offending
    token.

    THIS IS A RUNTIME GUARD, NOT A CONFIG LINT. The registry is a tracked file
    a human edits; a lint that only runs in CI is a file that can still be
    edited on the box. Every launch re-checks, so the worst case of a bad edit
    is a refused request rather than an agent running with the sandbox off.

    The refusals, in the order they fire:
      * a permission-skipping or sandbox-bypassing flag by PREFIX, and
        ``--bare`` - both keep their own message because those messages explain
        the trap;
      * argv[0] is not the family's pinned executable, or carries a path
        separator (the path is ours to resolve, not the template's to choose);
      * a missing or misspelled subcommand;
      * ANY token outside the family's declared vocabulary - this is what makes
        the guard about the command's IDENTITY rather than which strings happen
        to appear in it;
      * a duplicate occurrence of a declared flag: the review found
        ``--sandbox read-only ... --sandbox danger-full-access`` passed because
        only ``tokens.index()``, the FIRST occurrence, was inspected. A second
        occurrence is refused outright, so a later value can never override an
        earlier one, and every occurrence's value is checked besides;
      * a flag whose VALUE is not what the containment requires - this is where
        ``--allowedTools Bash`` is caught, because presence was never enough;
      * a service-appended per-request flag written into tracked config;
      * an `Env=` cell setting anything outside the allow-list.

    Contract:
      Inputs:  route: Route
      Outputs: list[str] - the checked argv tokens, so a caller builds argv
               from the form that was actually inspected.
      Raises:  UnsafeRouteError
    Implements: SR-019, LLR-002
    """
    tokens = split_cmd(route.cmd_template)
    if not tokens:
        raise UnsafeRouteError("route {}: empty CmdTemplate".format(route.id))
    for tok in tokens:
        if tok.startswith(DANGEROUS_FLAG_PREFIX):
            raise UnsafeRouteError(
                "route {}: refuses {!r} - ai-template carries that flag because "
                "THAT repo is itself the consented unattended run; this "
                "service's work is chosen by a caller (A40, 2026-09-09)".format(
                    route.id, tok
                )
            )
        if tok in BANNED_TOKENS:
            raise UnsafeRouteError(
                "route {}: refuses {!r} - it ignores the OAuth token and "
                "requires an API key, and this service leans on the "
                "subscription (A40)".format(route.id, tok)
            )

    contract = FAMILY_CONTRACTS.get(route.family)
    if contract is None:
        raise UnsafeRouteError(
            "route {}: family {!r} has no declared read-only contract - "
            "refusing rather than guessing one".format(route.id, route.family)
        )

    _assert_pinned_executable(route, tokens[0], contract)
    index = _assert_subcommands(route, tokens, contract)
    seen = _assert_declared_vocabulary(route, tokens, index, contract)
    _assert_required_flags(route, seen, contract)
    assert_safe_env(route)
    return tokens


def _assert_pinned_executable(route, token, contract):
    """argv[0] must be the family's executable, by bare name.

    A path separator is refused as well as an unknown name: `/tmp/claude` is
    not `claude`, and letting a template choose the path would hand the pin
    straight back to whoever edited the registry.
    """
    if "/" in token or "\\" in token:
        raise UnsafeRouteError(
            "route {}: executable {!r} carries a path - this service resolves "
            "the pinned name on AI_CLI_BIN_PATH, and a template does not get to "
            "choose the binary".format(route.id, token)
        )
    if token not in contract.executables:
        raise UnsafeRouteError(
            "route {}: executable {!r} is not one of the {} family's pinned "
            "binaries ({}) - a template that can name its own executable is "
            "arbitrary code execution as the service account, whatever flags "
            "follow it".format(
                route.id, token, route.family, "|".join(sorted(contract.executables))
            )
        )


def _assert_subcommands(route, tokens, contract):
    """The declared subcommands must follow argv[0], in order. Returns the
    index of the first token after them."""
    index = 1
    for word in contract.subcommands:
        if index >= len(tokens) or tokens[index] != word:
            raise UnsafeRouteError(
                "route {}: expected subcommand {!r} after {!r}".format(
                    route.id, word, tokens[0]
                )
            )
        index += 1
    return index


def _assert_declared_vocabulary(route, tokens, index, contract):
    """Walk the remaining tokens; refuse anything undeclared, duplicated or
    wrong-valued. Returns the set of flags seen."""
    seen = set()
    while index < len(tokens):
        tok = tokens[index]
        if tok in contract.service_flags:
            raise UnsafeRouteError(
                "route {}: {!r} is appended by the service per request and does "
                "not belong in tracked config".format(route.id, tok)
            )
        if tok in contract.bare_flags:
            _note_flag(route, seen, tok)
            index += 1
            continue
        if tok in contract.value_flags:
            _note_flag(route, seen, tok)
            if index + 1 >= len(tokens):
                raise UnsafeRouteError(
                    "route {}: {} is the last token and takes a value".format(
                        route.id, tok
                    )
                )
            value = tokens[index + 1]
            validator = contract.validators.get(tok)
            if validator is not None:
                problem = validator(value)
                if problem:
                    raise UnsafeRouteError(
                        "route {}: {} {!r} {}".format(route.id, tok, value, problem)
                    )
            index += 2
            continue
        raise UnsafeRouteError(
            "route {}: token {!r} is not in the {} family's declared vocabulary "
            "- every flag a row may carry is declared in FAMILY_CONTRACTS, so "
            "adding one is a reviewed edit to the service and not a registry "
            "cell".format(route.id, tok, route.family)
        )
    return seen


def _note_flag(route, seen, tok):
    """Record a flag occurrence, refusing a second one.

    A repeated flag is refused rather than merely re-validated, because "the
    last one wins" is the CLI's rule and not ours: the review's
    ``--sandbox read-only ... --sandbox danger-full-access`` passed a guard
    that looked only at the first occurrence. One occurrence, one meaning.
    """
    if tok in seen:
        raise UnsafeRouteError(
            "route {}: {} appears more than once - the CLI takes the LAST "
            "occurrence, so a duplicate is how a contained value gets "
            "overridden by a later one".format(route.id, tok)
        )
    seen.add(tok)


def _assert_required_flags(route, seen, contract):
    """Every required group must have a member present. An absent flag means
    the CLI's own default, which for neither family is read-only."""
    for group in contract.required:
        if not seen.intersection(group):
            raise UnsafeRouteError(
                "route {}: missing required token {} - an absent flag means "
                "the CLI's own default, which is neither read-only nor "
                "machine-readable".format(
                    route.id, "|".join(group)
                )
            )


def assert_safe_env(route):
    """Refuse a registry row whose ``Env=`` cell sets anything outside the
    allow-list. Returns the parsed overrides.

    The review's finding: `Env=` was applied to the child's environment
    unchecked, so a row could set `PATH=/writable/dir` with a planted `claude`
    on it, or redirect `CODEX_HOME`/`CLAUDE_CONFIG_DIR` at a configuration the
    row also controls. Pinning the executable is worth nothing if the row can
    move the ground it stands on.

    Contract:
      Inputs:  route: Route (its `env` cell: comma-separated `KEY=VALUE`)
      Outputs: dict[str, str]
      Raises:  UnsafeRouteError
    Implements: SR-019, LLR-002
    """
    overrides = {}
    for pair in (route.env or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise UnsafeRouteError(
                "route {}: Env entry {!r} is not KEY=VALUE".format(route.id, pair)
            )
        key, _, value = pair.partition("=")
        key, value = key.strip(), value.strip()
        allowed = ALLOWED_ENV.get(key)
        if allowed is None:
            hint = (
                " - and that one selects the executable or its configuration"
                if key in ENV_KEYS_THAT_SELECT_THE_BINARY
                else ""
            )
            raise UnsafeRouteError(
                "route {}: Env may not set {!r}{}; the allow-list is {}".format(
                    route.id, key, hint, ",".join(sorted(ALLOWED_ENV)) or "(empty)"
                )
            )
        if value not in allowed:
            raise UnsafeRouteError(
                "route {}: Env {}={!r} is not one of {}".format(
                    route.id, key, value, "|".join(sorted(allowed))
                )
            )
        overrides[key] = value
    return overrides


def resolve_executable(name, search_path=None, which=None, stat=None, posix=None):
    """Resolve a pinned executable NAME to an absolute path, or refuse.

    The pin is only half the guard: a name is resolved through a PATH, and a
    PATH is something an attacker with a registry edit (or a stray environment)
    can influence. So the search path is this service's fixed one
    (AI_CLI_BIN_PATH), never the inherited environment's, and the resolved file
    must not be writable by group or other - a binary anyone can rewrite is not
    pinned to anything.

    Off POSIX the name is returned unchanged: this service runs on the hub, and
    the dev PC has neither the CLIs nor meaningful mode bits. The POSIX branch
    is exercised by tests through the injected ``which``/``stat``.

    Contract:
      Inputs:  name: str; search_path: str (os.pathsep-separated) or None for
               the default; which/stat/posix: injection seams for tests.
      Outputs: str - an absolute path on POSIX, the name unchanged elsewhere.
      Raises:  UnsafeRouteError
    Implements: SR-019, LLR-002
    """
    posix = (os.name == "posix") if posix is None else posix
    if not posix:
        return name
    which = shutil.which if which is None else which
    stat = os.stat if stat is None else stat
    path = which(name, path=search_path or DEFAULT_BIN_PATH)
    if not path:
        raise UnsafeRouteError(
            "executable {!r} is not on AI_CLI_BIN_PATH ({}) - refusing to let "
            "the inherited PATH decide which binary runs".format(
                name, search_path or DEFAULT_BIN_PATH
            )
        )
    mode = stat(path).st_mode
    if mode & 0o022:
        raise UnsafeRouteError(
            "executable {} is writable by group or other (mode {:o}) - a binary "
            "anyone can rewrite is not a pin".format(path, mode & 0o777)
        )
    return path


# ---------------------------------------------------------------------------
# 3. Containment 1: the account, asserted as the EFFECTIVE identity.
# ---------------------------------------------------------------------------


def effective_account_name(geteuid=None, getpwuid=None, getuser=None):
    """The account this process is ACTUALLY running as, or None.

    V2's fix. The old check validated `AI_CLI_USER`, a configured string, while
    systemd ran whatever the unit's `User=` said; setting `AI_CLI_USER=alice`
    made the boot checks certify `alice` while `homehub-ai` ran. A configured
    string cannot be the assertion - the running identity has to be.

    Returns None only when no identity can be determined at all, which callers
    treat as a refusal and never as a pass.
    Implements: SR-019, LLR-002
    """
    geteuid = geteuid if geteuid is not None else getattr(os, "geteuid", None)
    if geteuid is not None:
        try:
            lookup = getpwuid
            if lookup is None:
                import pwd

                lookup = pwd.getpwuid
            return lookup(geteuid()).pw_name
        except Exception:  # pragma: no cover - a broken passwd db is not a pass
            return None
    try:  # the dev PC, where there is no euid; still an identity, still checked
        import getpass

        return (getuser or getpass.getuser)()
    except Exception:  # pragma: no cover
        return None


def assert_effective_account(configured, effective):
    """Refuse unless the account asserted is the account that runs.

    Contract:
      Inputs:  configured: str - AI_CLI_USER, what the checks certify.
               effective: str|None - from `effective_account_name()`.
      Outputs: str - the account, which is now both.
      Raises:  ConfigError
    Implements: SR-019, LLR-002
    """
    configured = (configured or "").strip()
    if not configured:
        raise ConfigError("no service account declared; refusing to inherit one")
    if not effective:
        raise ConfigError(
            "cannot determine the account this process is running as; refusing "
            "to certify {!r} on the strength of a configured string".format(
                configured
            )
        )
    if effective != configured:
        raise ConfigError(
            "configured account {!r} is not the account this process runs as "
            "({!r}) - the account that is asserted must be the account that "
            "runs, or the checks certify one identity while systemd starts "
            "another (A40 acceptance 1; cross-review V2)".format(
                configured, effective
            )
        )
    return configured


def assert_service_account(user, account_groups=(), sudoers_lines=()):
    """Refuse to run as a privileged account. Raises ``ConfigError``.

    `hub` is the box's only account with a real shell AND
    `(ALL) NOPASSWD: ALL`, so a service running as `hub` and driving an agent
    on request is arbitrary code execution as a passwordless-sudo user. `root`
    is refused for the obvious reason. Beyond the name, the account's ACTUAL
    privilege is checked: membership of a sudo-granting group, or any sudoers
    line naming it, is a refusal - because "we made a new account" is worth
    nothing if it was then added to `sudo`.

    An UNRESOLVED sudoers include is a refusal to certify. The review found the
    old parser treated `#include /etc/other-sudoers` as a comment, so a
    NOPASSWD grant one file away was invisible; `collect_sudoers_lines` follows
    includes and leaves behind only the ones it could not read, which land here
    as "cannot certify".

    Contract:
      Inputs:  user: str - the account, already confirmed to be the running one.
               account_groups: iterable[str] - groups this account belongs to.
               sudoers_lines: iterable[str] - EXPANDED sudoers content.
      Outputs: str - the user, unchanged.
      Raises:  ConfigError
    Implements: SR-019, LLR-002
    """
    name = (user or "").strip()
    if not name:
        raise ConfigError("no service account declared; refusing to inherit one")
    if name in FORBIDDEN_ACCOUNTS:
        raise ConfigError(
            "refusing to run as {!r}: it carries (ALL) NOPASSWD: ALL, so an "
            "agent driven on request would be arbitrary code execution as a "
            "passwordless-sudo user (A40, 2026-09-09)".format(name)
        )
    bad = sorted(
        PRIVILEGED_GROUPS.intersection({str(g).strip() for g in account_groups})
    )
    if bad:
        raise ConfigError(
            "service account {!r} is in privileged group(s) {} - a dedicated "
            "account that was then given sudo (or docker, which is sudo by "
            "another door) is not a containment".format(name, ", ".join(bad))
        )
    pattern = re.compile(
        r"(?<![A-Za-z0-9_-])%?" + re.escape(name) + r"(?![A-Za-z0-9_-])"
    )
    for line in sudoers_lines:
        include = SUDOERS_INCLUDE_RE.match(line)
        if include:
            raise ConfigError(
                "cannot certify {!r}: sudoers pulls in {!r} and this process "
                "could not read it, so a NOPASSWD grant one file away would be "
                "invisible. Refusing rather than certifying what was never "
                "read".format(name, include.group(2))
            )
        text = line.split("#", 1)[0]
        if pattern.search(text):
            raise ConfigError(
                "service account {!r} is named in a sudoers rule: {!r}".format(
                    name, line.strip()
                )
            )
    return name


def account_groups(user):
    """The groups ``user`` belongs to, best effort. Empty off-Linux, where the
    check degrades to the name-and-sudoers half rather than failing."""
    try:
        import grp
        import pwd

        entry = pwd.getpwnam(user)
        groups = [g.gr_name for g in grp.getgrall() if user in g.gr_mem]
        groups.append(grp.getgrgid(entry.pw_gid).gr_name)
        return groups
    except Exception:
        return []


def _listdir(directory):
    try:
        return [os.path.join(directory, n) for n in sorted(os.listdir(directory))]
    except OSError:
        return []


def collect_sudoers_lines(paths=None, reader=None, lister=None, depth=0, seen=None):
    """Every sudoers rule line reachable from ``paths``, INCLUDES FOLLOWED.

    `#include`, `#includedir`, `@include` and `@includedir` all pull in more
    rules; sudo treats them as directives and not as comments, and the review
    found this parser did the opposite. A directive that CANNOT be followed
    (unreadable file, missing directory, recursion limit) is left in the output
    verbatim, so `assert_service_account` refuses to certify rather than
    passing on content it never saw.

    Contract:
      Inputs:  paths: iterable[str]; reader/lister: injection seams for tests.
      Outputs: list[str] - expanded lines; any surviving include directive is
               one that could not be followed.
    Implements: SR-019, LLR-002
    """
    if paths is None:
        paths = ["/etc/sudoers"] + _listdir("/etc/sudoers.d")
    reader = reader or _read_lines
    lister = lister or _listdir
    seen = set() if seen is None else seen
    lines = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        for line in reader(path) or []:
            include = SUDOERS_INCLUDE_RE.match(line)
            if not include:
                lines.append(line)
                continue
            kind, target = include.group(1), include.group(2).strip('"')
            if depth >= 8:  # a cycle sudo itself would reject
                lines.append(line)
                continue
            targets = lister(target) if kind == "includedir" else [target]
            # An unreadable file (reader -> None) or a missing directory is
            # UNFOLLOWABLE: the directive is kept so assert_service_account
            # refuses to certify. An empty-but-readable file is followed.
            if kind != "includedir" and reader(target) is None:
                targets = []
            if not targets:
                lines.append(line)  # unfollowable -> refuse to certify
                continue
            lines.extend(
                collect_sudoers_lines(targets, reader, lister, depth + 1, seen)
            )
    return lines


def _read_lines(path):
    """The lines of one sudoers file, or None when it cannot be read.

    None and [] mean different things here and the difference is the guard: an
    empty file grants nothing, while a file we could not open might grant
    everything, and only one of those may be certified.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readlines()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 4. Containment 3: the bind. Loopback, or an address a docker bridge CARRIES.
# ---------------------------------------------------------------------------


def docker_bridge_addresses(net_dir="/sys/class/net", iface_addresses=None):
    """The IPv4 addresses local DOCKER BRIDGE interfaces actually carry.

    V3's fix, and the reason the old ``172.16.0.0/12`` allowance had to go: that
    range is 1,048,576 addresses of RFC1918 space and real household LANs live
    inside it (a home on 172.20.0.0/16 is a LAN, and the old guard called it
    "the docker bridge"). Membership of a range is not evidence of anything. An
    address this box's `docker0`/`br-*` interface is holding right now is.

    Contract:
      Inputs:  net_dir: str - sysfs network directory;
               iface_addresses: callable(name) -> list[str], injection seam.
      Outputs: set[str] - literal addresses; EMPTY where there is no docker
               bridge, which makes every non-loopback bind a refusal.
    Implements: SR-019, LLR-002
    """
    iface_addresses = iface_addresses or _iface_ipv4
    found = set()
    for name in sorted(_safe_listdir(net_dir)):
        if name != "docker0" and not name.startswith("br-"):
            continue
        if not os.path.isdir(os.path.join(net_dir, name, "bridge")):
            continue  # named like a bridge but is not one
        for addr in iface_addresses(name):
            try:
                parsed = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if parsed.is_private and not parsed.is_loopback:
                found.add(addr)
    return found


def _safe_listdir(directory):
    try:
        return os.listdir(directory)
    except OSError:
        return []


def _iface_ipv4(name):  # pragma: no cover - Linux ioctl; covered by injection
    """The IPv4 address of one interface, via SIOCGIFADDR. Stdlib only."""
    try:
        import fcntl
        import struct

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            packed = fcntl.ioctl(
                sock.fileno(),
                0x8915,  # SIOCGIFADDR
                struct.pack("256s", name[:15].encode("utf-8")),
            )
        finally:
            sock.close()
        return [socket.inet_ntoa(packed[20:24])]
    except Exception:
        return []


def resolve_bind(address, bridge_addresses=None):
    """Return the bind address, or raise ``BindRefused``.

    Loopback, or an address a local docker bridge interface is actually
    carrying - that is the whole allow-list. A wildcard (``0.0.0.0`` / ``::``)
    is refused BY NAME rather than by "is it in a bad network", because a
    wildcard is precisely the mistake that puts the household's interactive
    credential on the LAN and it does not read as a LAN address. Every other
    literal address is refused, and a hostname is refused because what it
    resolves to is not this file's to promise.

    ``bridge_addresses=None`` means "discover them", so on a box with no docker
    bridge only loopback binds.

    Contract:
      Inputs:  address: str - a literal IPv4/IPv6 address.
               bridge_addresses: iterable[str]|None - the bridge's own
               addresses; None discovers them from sysfs.
      Outputs: str - the address, unchanged.
      Raises:  BindRefused
    Implements: SR-019, LLR-002
    """
    text = (address or "").strip()
    if not text:
        raise BindRefused(
            "no bind address configured; refusing to default to a wildcard"
        )
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        raise BindRefused(
            "bind {!r} is not a literal IP address - a hostname's resolution is "
            "not ours to promise; name the loopback or bridge address".format(text)
        )
    if addr.is_unspecified:
        raise BindRefused(
            "bind {!r} is the wildcard: it would publish the service on the LAN, "
            "and this service holds the household's interactive "
            "credential".format(text)
        )
    if addr.is_loopback:
        return text
    known = (
        docker_bridge_addresses() if bridge_addresses is None else set(bridge_addresses)
    )
    normalised = set()
    for candidate in known:
        try:
            normalised.add(str(ipaddress.ip_address(str(candidate).strip())))
        except ValueError:
            continue
    if str(addr) in normalised:
        return text
    raise BindRefused(
        "bind {!r} is not loopback and is not an address any local docker "
        "bridge is carrying (bridge addresses here: {}). A private RANGE is not "
        "evidence: 172.16.0.0/12 contains real household LANs, so membership of "
        "it was never proof of a bridge (cross-review V3)".format(
            text, ",".join(sorted(normalised)) or "(none)"
        )
    )


def bind_family(address):
    """``socket.AF_INET6`` for an IPv6 literal, ``AF_INET`` otherwise.

    The review found `::1` passed `resolve_bind` and then failed to bind,
    because the server was AF_INET: a guard must not accept what the service
    cannot serve.
    Implements: SR-019, LLR-004
    """
    return (
        socket.AF_INET6
        if ipaddress.ip_address((address or "").strip()).version == 6
        else socket.AF_INET
    )


# ---------------------------------------------------------------------------
# 5. Containment 4: the per-request scratch working directory.
# ---------------------------------------------------------------------------


class request_scratch(object):
    """Context manager giving ONE request its own 0700 working directory under
    the scratch root, and removing it on every exit path - VISIBLY.

    A working directory is not a detail for these CLIs: they are coding agents,
    their sandbox confines the filesystem to the working directory, and
    ``CLAUDE.md`` plus the skills of whatever tree the process starts in load
    per invocation. Starting a caller-chosen job in a real tree would hand it
    that tree; starting every job in the same directory would let one request
    see the last one's leavings. So: a fresh directory per request, inside a
    root that holds nothing real.

    The review's finding on cleanup: it used ``ignore_errors=True``, so "removed
    on every exit path" was unobservable - a cleanup that always failed would
    look exactly like one that always worked. Now a failed removal is written to
    stderr and, when the request itself did not fail, raised: the scratch tree
    is the one place a caller's content lands on disk and leaking it is not a
    detail to swallow.

    Yields a dict with ``cwd`` and ``schema_path`` - both per-request values, so
    neither belongs in the tracked command template.

    Contract:
      Inputs:  root: str - AI_CLI_SCRATCH_ROOT; created 0700 if absent.
               remover: callable(path) - injection seam for the failure test.
      Outputs: {"cwd": str, "schema_path": str}
      Raises:  OSError if the root cannot be created, or if cleanup fails on a
               request that otherwise succeeded - loudly, because a service that
               silently falls back to /tmp, or silently leaves the caller's
               content behind, has lost this containment without saying so.
    Implements: SR-019, LLR-002
    """

    def __init__(self, root, remover=None):
        self.root = root
        self.remover = remover or shutil.rmtree
        self.path = None

    def __enter__(self):
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        self.path = tempfile.mkdtemp(prefix="req-", dir=self.root)
        os.chmod(self.path, 0o700)
        return {
            "cwd": self.path,
            "schema_path": os.path.join(self.path, "response-schema.json"),
        }

    def __exit__(self, exc_type, exc, tb):
        if not self.path:
            return False
        path, self.path = self.path, None
        try:
            self.remover(path)
        except Exception as failure:
            sys.stderr.write(
                "ai-cli SCRATCH NOT REMOVED: {} ({}) - the caller's content is "
                "still on disk\n".format(path, failure)
            )
            if exc_type is None:
                raise
        return False


# ---------------------------------------------------------------------------
# 6. Pacing and concurrency: one decision, under one lock. (LLR-004)
# ---------------------------------------------------------------------------


def available(cooldowns, route_id, now):
    """True when ``route_id`` is not cooling down. ``cooldowns`` maps a route id
    to the epoch it is available again. Pure; the racy part is `RouteGate`."""
    until = (cooldowns or {}).get(route_id)
    return until is None or until <= now


def cool(cooldowns, route_id, now, seconds):
    """Put ``route_id`` on cooldown until ``now + seconds``. In place."""
    cooldowns[route_id] = now + max(0, seconds)


class RouteGate(object):
    """The one place that decides whether a session may launch.

    THE REVIEW'S RACE: every handler thread called `available()` before any
    thread called `cool()`, so a burst of N requests launched N concurrent
    sessions on a small always-on box, each allowed 300 s and a share of the
    subscription quota. And a SUCCESSFUL call never cooled at all, so the
    cooldown only ever paced failures.

    Three bounds, taken together under one lock:
      * a row already IN FLIGHT is unavailable - the check and the claim are
        one atomic step, so a burst cannot slip between them;
      * a row that is cooling is unavailable - failures back off
        (AI_CLI_COOLDOWN_SECONDS) and successes pace
        (AI_CLI_SUCCESS_COOLDOWN_SECONDS, small, so the service stays usable
        while a hot loop still cannot launch back-to-back sessions);
      * at most AI_CLI_MAX_CONCURRENT sessions run at once across ALL rows,
        because the box, not the row, is the scarce thing.

    Implements: SR-019, LLR-004
    """

    def __init__(self, cooldown_seconds, success_cooldown_seconds, max_concurrent):
        self.cooldown_seconds = max(0, int(cooldown_seconds))
        self.success_cooldown_seconds = max(0, int(success_cooldown_seconds))
        self.max_concurrent = max(1, int(max_concurrent))
        self.cooldowns = {}
        self._lock = threading.Lock()
        self._in_flight = set()

    def acquire(self, route_id, now):
        """Claim a launch slot for ``route_id``.

        Returns "ok", "cooling" (this row is backing off, or already running) or
        "busy" (the box is at its concurrency ceiling). The caller MUST call
        `release` for an "ok".
        """
        with self._lock:
            if route_id in self._in_flight or not available(
                self.cooldowns, route_id, now
            ):
                return "cooling"
            if len(self._in_flight) >= self.max_concurrent:
                return "busy"
            self._in_flight.add(route_id)
            return "ok"

    def release(self, route_id, now, ok):
        """Give the slot back and cool the row: briefly after a success, for the
        full backoff after a failure."""
        seconds = self.success_cooldown_seconds if ok else self.cooldown_seconds
        with self._lock:
            self._in_flight.discard(route_id)
            if seconds:
                cool(self.cooldowns, route_id, now, seconds)

    def in_flight(self):
        with self._lock:
            return set(self._in_flight)


# ---------------------------------------------------------------------------
# 7. Session launch - the argv, the stdin decision, the run, the result.
# ---------------------------------------------------------------------------


def render_messages(messages):
    """Flatten a messages array into the single prompt the CLIs take on stdin.

    The CLIs are one-shot ``-p`` drivers, not chat endpoints: there is no
    multi-turn wire format to hand them, so a conversation is rendered as
    labelled turns. Roles are restricted to user|assistant so a caller cannot
    smuggle a fabricated ``system`` frame past the configured route.

    Raises ValueError on an empty array, a non-object entry, an unknown role or
    empty content - loudly, because a silently dropped turn is a wrong answer
    nobody can trace.
    Implements: SR-019, LLR-002
    """
    if not messages:
        raise ValueError("messages: at least one message is required")
    parts = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise ValueError("messages[{}]: expected an object".format(i))
        role = str(msg.get("role") or "").lower()
        if role not in ("user", "assistant"):
            raise ValueError(
                "messages[{}]: role {!r} is not user|assistant".format(i, role)
            )
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(
                "messages[{}]: content must be a non-empty string".format(i)
            )
        parts.append("{}: {}".format(role.upper(), content.strip()))
    return "\n\n".join(parts)


def build_argv(route, scratch, tokens=None, bin_path=None):
    """Build ``(argv, env_overrides)`` for one session.

    argv[0] is replaced with the ABSOLUTE path the pinned name resolves to on
    AI_CLI_BIN_PATH, so neither the template nor the inherited environment
    chooses the binary.

    The prompt is NOT here. Every shipped template carries no ``{prompt}``, so
    the prompt goes to the child's STDIN (ai-template WI-216) - immune to the OS
    command-line cap, which a messages array will exceed long before anyone
    notices. The per-request values are appended here rather than tracked in the
    registry: the family's SCHEMA FLAG (the structured-output contract - the
    review found the schema file was written and then never passed to codex, so
    that family silently ignored the caller's contract) and, for codex,
    ``--output-last-message``, because codex echoes its banner and the whole
    prompt into stdout so its stdout is not the result (WI-217).

    Contract:
      Inputs:  route: Route; scratch: the dict from `request_scratch`;
               tokens: the already-checked token list, or None to check now;
               bin_path: the executable search path (AI_CLI_BIN_PATH).
      Outputs: (argv: list[str], env_overrides: dict[str,str])
      Raises:  UnsafeRouteError, via `assert_safe_template`/`resolve_executable`.
    Implements: SR-019, LLR-002
    """
    tokens = assert_safe_template(route) if tokens is None else tokens
    contract = FAMILY_CONTRACTS[route.family]
    if not contract.schema_flag:
        raise UnsafeRouteError(
            "route {}: family {} declares no way to be handed the caller's "
            "schema, so the output contract could not be enforced".format(
                route.id, route.family
            )
        )
    argv = [t.replace("{model}", route.model) for t in tokens]
    argv[0] = resolve_executable(argv[0], search_path=bin_path)
    argv += [contract.schema_flag, scratch["schema_path"]]
    if contract.result == "last-message-file":
        argv += ["--output-last-message", last_message_path(scratch)]
    return argv, assert_safe_env(route)


def last_message_path(scratch):
    """Where a `last-message-file` family leaves its answer."""
    return os.path.join(scratch["cwd"], "last-message.txt")


def kill_process_group(proc, posix=None, killpg=None, getpgid=None):
    """Kill the child AND everything it started.

    The review's finding: only `proc.kill()` was called and no process group was
    created, so a CLI that had spawned helpers left them running after a timeout
    - on an always-on box, forever. `run_session` starts the child in its own
    session (`start_new_session=True`), which makes it a process-group leader,
    and this kills that group.

    Returns "group" or "process", so a test can tell which happened.
    Implements: SR-019, LLR-004
    """
    posix = (os.name == "posix") if posix is None else posix
    if posix:
        killpg = killpg or getattr(os, "killpg", None)
        getpgid = getpgid or getattr(os, "getpgid", None)
        if killpg and getpgid:
            try:
                import signal

                killpg(getpgid(proc.pid), getattr(signal, "SIGKILL", signal.SIGTERM))
                return "group"
            except Exception:
                pass  # the group is already gone, or we lost the race
    proc.kill()
    return "process"


def run_session(argv, cwd, timeout, env_overrides, stdin_input, bin_path=None):
    """Run ONE headless session and return ``(exit_code, output, timed_out)``.

    The thin I/O shell - everything that decides lives above. Reused shape from
    ``agent_session.run_session``: stdin is FED AND CLOSED so the child can
    never wedge on an interactive read; a reader thread drains stdout so the
    child cannot block on a full pipe; the timeout is per session.

    Two changes from the reviewed version: the child gets its own SESSION, so a
    timeout kills the whole process group rather than orphaning grandchildren;
    and the child's PATH is the service's fixed search path, not whatever this
    process inherited.

    Implements: SR-019, LLR-002, LLR-004
    """
    env = dict(os.environ)
    env["PATH"] = bin_path or DEFAULT_BIN_PATH
    env.update(env_overrides or {})
    kwargs = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True  # its own process group to kill
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        **kwargs
    )
    chunks = []

    def pump():
        for line in proc.stdout:
            chunks.append(line)

    reader = threading.Thread(target=pump)
    reader.daemon = True
    reader.start()
    timed_out = False
    try:
        proc.stdin.write(stdin_input)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_group(proc)
        proc.wait()
    reader.join(timeout=5)
    return proc.returncode, "".join(chunks), timed_out


def _json_objects(text):
    """Every JSON object in a transcript: the whole text if it parses, else each
    line that does. Codex emits JSONL; claude emits one object."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        whole = json.loads(text)
        if isinstance(whole, dict):
            return [whole]
    except ValueError:
        pass
    found = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            found.append(data)
    return found


def extract_result(family, output, scratch, reader=None):
    """The session's TYPED result, or None when it produced none.

    The review's finding: a `{"type":"status", ...}` object with exit 0 was
    reported as HTTP 200 with `"result": null`. A zero exit and a JSON object
    are not an answer - the answer is a RESULT object carrying a result. So:

      * stdout-json families (claude): an object with ``type == "result"``, not
        flagged ``is_error``, whose ``result`` is not None;
      * last-message-file families (codex): the last-message file, non-empty,
        parsing as JSON - which is what the caller's schema constrained.

    Returns the envelope dict (``{"type": "result", "result": ...}``) or None.
    Implements: SR-019, LLR-002, LLR-004
    """
    if family not in FAMILY_CONTRACTS:
        return None
    if FAMILY_CONTRACTS[family].result == "last-message-file":
        reader = reader or _read_text
        text = (reader(last_message_path(scratch)) or "").strip()
        if not text:
            return None
        try:
            value = json.loads(text)
        except ValueError:
            return None
        return {"type": "result", "result": value}
    for data in _json_objects(output):
        if data.get("type") != "result":
            continue
        if data.get("is_error"):
            return None
        if data.get("result") is None:
            return None
        return data
    return None


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 8. Configuration and the HTTP shell.
# ---------------------------------------------------------------------------


class Config(object):
    """The service's configuration, read once at startup from the environment.

    Config:  AI_CLI_BIND (loopback or a real docker-bridge address),
             AI_CLI_PORT (int), AI_CLI_USER (the dedicated account),
             AI_CLI_REGISTRY, AI_CLI_ENABLED_ROUTES (the enable-list file),
             AI_CLI_SCRATCH_ROOT, AI_CLI_BIN_PATH, AI_CLI_TIMEOUT_SECONDS,
             AI_CLI_COOLDOWN_SECONDS, AI_CLI_SUCCESS_COOLDOWN_SECONDS,
             AI_CLI_MAX_CONCURRENT, AI_CLI_MAX_BODY_BYTES,
             AI_CLI_SOCKET_TIMEOUT_SECONDS, AI_CLI_MAX_CONNECTIONS.
             All declared in stack/.env.example.
    Raises:  BindRefused if AI_CLI_BIND is neither loopback nor an address a
             local docker bridge carries.
    Implements: SR-019, LLR-002, LLR-004
    """

    def __init__(self, env=None, bridge_addresses=None):
        env = os.environ if env is None else env
        here = os.path.dirname(os.path.abspath(__file__))
        self.bind = resolve_bind(
            env.get("AI_CLI_BIND", "127.0.0.1"), bridge_addresses=bridge_addresses
        )
        self.port = int(env.get("AI_CLI_PORT", "8791"))
        self.user = env.get("AI_CLI_USER", "homehub-ai")
        self.registry_path = env.get("AI_CLI_REGISTRY") or os.path.join(
            here, "agents.registry.csv"
        )
        self.enabled_path = env.get("AI_CLI_ENABLED_ROUTES") or os.path.join(
            here, "routes-enabled"
        )
        self.scratch_root = env.get("AI_CLI_SCRATCH_ROOT", "/var/lib/homehub-ai/scratch")
        self.bin_path = env.get("AI_CLI_BIN_PATH", DEFAULT_BIN_PATH)
        self.timeout = int(env.get("AI_CLI_TIMEOUT_SECONDS", "300"))
        self.cooldown = int(env.get("AI_CLI_COOLDOWN_SECONDS", "120"))
        self.success_cooldown = int(env.get("AI_CLI_SUCCESS_COOLDOWN_SECONDS", "5"))
        self.max_concurrent = int(env.get("AI_CLI_MAX_CONCURRENT", "1"))
        self.max_body_bytes = int(env.get("AI_CLI_MAX_BODY_BYTES", "262144"))
        self.socket_timeout = int(env.get("AI_CLI_SOCKET_TIMEOUT_SECONDS", "30"))
        self.max_connections = int(env.get("AI_CLI_MAX_CONNECTIONS", "8"))


def selectable_routes(config):
    """Compose the registry against the enable-list and check every survivor.

    Every enabled row runs through ``assert_safe_template`` AT STARTUP as well
    as per launch, so a bad row fails the service loudly instead of waiting for
    the first caller to trip it.
    Implements: SR-019, LLR-002
    """
    registry, errors = load_registry(config.registry_path)
    if errors:
        raise ConfigError("; ".join(errors))
    enabled = load_enable_list(config.enabled_path)
    routes = {}
    for rid in enabled:
        route = registry.get(rid)
        if route is None:
            raise ConfigError(
                "routes-enabled names {!r}, which is not in the registry".format(rid)
            )
        assert_safe_template(route)
        routes[rid] = route
    return routes


def handle_ask(body, config, routes, gate, runner=run_session, now=None):
    """The request path: validate, claim a slot, launch one contained session.

    Returns ``(status_code, payload_dict)``. No HTTP in here, so the whole
    contract is unit-testable with a fake runner - which is how this block's
    acceptance criteria are asserted rather than eyeballed.

    Contract:
      Inputs:  body: dict - {"messages": [...], "route": str,
                             "schema": {json-schema object}}
               gate: RouteGate - the pacing and concurrency decision.
      Outputs: (int, dict). 200 carries {"route","result","raw"}.
      Raises:  nothing - every failure is a status code and an error string.
    Implements: SR-019, LLR-002, LLR-004
    """
    now = time.time() if now is None else now
    route_id = str(body.get("route") or "").strip()
    if not route_id:
        return 400, {"error": "route is required", "allowed": sorted(routes)}
    route = routes.get(route_id)
    if route is None:
        return 403, {
            "error": "route {!r} is not enabled".format(route_id),
            "allowed": sorted(routes),
        }
    schema = body.get("schema")
    if not isinstance(schema, dict) or not schema:
        return 400, {
            "error": "schema is required - the output contract is the point of "
            "this service; an unconstrained answer is not offered"
        }
    try:
        prompt = render_messages(body.get("messages"))
    except ValueError as exc:
        return 400, {"error": str(exc)}

    # The availability check and the claim are ONE atomic step: a burst of
    # requests must not all see "available" before any of them has run.
    claim = gate.acquire(route_id, now)
    if claim == "cooling":
        return 429, {"error": "route {!r} is busy or cooling down".format(route_id)}
    if claim == "busy":
        return 503, {
            "error": "the box is already running {} session(s); this is a small "
            "always-on machine".format(gate.max_concurrent)
        }
    ok = False
    try:
        status, payload, ok = _run_one(config, route, schema, prompt, runner)
        return status, payload
    finally:
        gate.release(route_id, now, ok)


def _run_one(config, route, schema, prompt, runner):
    """One contained session: scratch, argv, run, extract the typed result.

    Returns ``(status, payload, ok)`` - `ok` being whether the session produced
    a genuine result, which is what decides how long the row cools.
    Implements: SR-019, LLR-002
    """
    route_id = route.id
    with request_scratch(config.scratch_root) as scratch:
        try:
            argv, env_overrides = build_argv(route, scratch, bin_path=config.bin_path)
        except UnsafeRouteError as exc:
            return 500, {"error": str(exc)}, False
        with open(scratch["schema_path"], "w", encoding="utf-8") as fh:
            json.dump(schema, fh)
        code, output, timed_out = runner(
            argv, scratch["cwd"], config.timeout, env_overrides, prompt
        )
        if timed_out:
            return (
                504,
                {
                    "error": "route {!r} timed out after {}s".format(
                        route_id, config.timeout
                    )
                },
                False,
            )
        result = extract_result(route.family, output, scratch)

    if code != 0 or result is None:
        # Assert the artifact, not the exit code: a zero exit with no result
        # object is a session with no typed outcome, and is reported as one.
        return (
            502,
            {
                "error": "route {!r} produced no typed result (exit {})".format(
                    route_id, code
                ),
                "raw": (output or "")[-2000:],
            },
            False,
        )
    return 200, {"route": route_id, "result": result.get("result"), "raw": result}, True


def make_handler(config, routes, gate):
    """Bind the request path into a BaseHTTPRequestHandler. Thin by design.

    Bounded by construction: a socket timeout so a slow client cannot hold a
    thread, a Content-Length ceiling checked BEFORE a byte is read, and the
    gate's concurrency ceiling around the launch itself.
    Implements: SR-019, LLR-004
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "homehub-ai-cli"
        #: socketserver applies this to the connection: a client that opens a
        #: socket and dribbles cannot pin a handler thread indefinitely.
        timeout = config.socket_timeout

        def _send(self, status, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(
                    200,
                    {
                        "ok": True,
                        "routes": sorted(routes),
                        "in_flight": sorted(gate.in_flight()),
                    },
                )
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/ask":
                return self._send(404, {"error": "not found"})
            status, payload = self._read_and_handle()
            self._send(status, payload)

        def _read_and_handle(self):
            """Bound the body BEFORE reading it, then hand off. Returns
            ``(status, payload)`` so the size refusals are unit-testable."""
            raw = self.headers.get("Content-Length")
            try:
                length = int(raw)
            except (TypeError, ValueError):
                return 411, {
                    "error": "Content-Length is required and must be an integer"
                }
            if length < 0 or length > config.max_body_bytes:
                # Refused BEFORE the read: a huge or slow body must not be able
                # to consume the box first (cross-review).
                return 413, {
                    "error": "body of {} bytes exceeds AI_CLI_MAX_BODY_BYTES "
                    "({})".format(length, config.max_body_bytes)
                }
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError, OSError) as exc:
                return 400, {"error": "bad JSON body: {}".format(exc)}
            if not isinstance(body, dict):
                return 400, {"error": "body must be a JSON object"}
            return handle_ask(body, config, routes, gate)

        def log_message(self, fmt, *args):
            # To the journal, and WITHOUT the request body: the body is the
            # caller's content and this service's log is not the place for it.
            sys.stderr.write("ai-cli %s\n" % (fmt % args))

    return Handler


def make_server(config, handler):
    """The bound, bounded HTTP server.

    Two things the reviewed version did not do: it serves IPv6 when the bind is
    an IPv6 literal (`::1` was accepted by the guard and then failed to bind),
    and it caps CONNECTIONS, not just requests - `ThreadingHTTPServer` starts a
    thread per connection and nothing was bounding that number.
    Implements: SR-019, LLR-004
    """
    max_connections = max(1, int(config.max_connections))

    class BoundedServer(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        address_family = bind_family(config.bind)
        request_queue_size = max_connections

        def __init__(self, *args, **kwargs):
            self.connection_slots = threading.BoundedSemaphore(max_connections)
            ThreadingHTTPServer.__init__(self, *args, **kwargs)

        def process_request(self, request, client_address):
            if not self.connection_slots.acquire(False):
                sys.stderr.write(
                    "ai-cli refused a connection: already at {} concurrent "
                    "connections\n".format(max_connections)
                )
                self.shutdown_request(request)
                return
            ThreadingHTTPServer.process_request(self, request, client_address)

        def shutdown_request(self, request):
            try:
                ThreadingHTTPServer.shutdown_request(self, request)
            finally:
                try:
                    self.connection_slots.release()
                except ValueError:  # pragma: no cover - released twice
                    pass

    return BoundedServer((config.bind, config.port), handler)


def startup_checks(config, effective=None):
    """Every refusal that must fire before a socket exists, in order.

    The order is deliberate: the account is asserted as the EFFECTIVE identity
    FIRST, because every later check is a statement ABOUT that account, and
    certifying the privileges of an account this process is not running as is
    exactly the defect V2 named.

    Contract:
      Inputs:  config: Config; effective: str|None - the running account,
               discovered when not supplied.
      Outputs: dict[str, Route] - the enabled, checked routes.
      Raises:  ConfigError, UnsafeRouteError
    Implements: SR-019, LLR-002
    """
    effective = effective_account_name() if effective is None else effective
    assert_effective_account(config.user, effective)
    assert_service_account(
        config.user, account_groups(config.user), collect_sudoers_lines()
    )
    return selectable_routes(config)


def main(argv=None):
    """Start the service. Refuses loudly rather than starting less contained.

    ``--bind-check ADDR`` answers only the bind question, for provisioning.
    ``--check`` validates configuration and exits without binding, so
    provisioning can assert the containments without a running service. It runs
    the SAME `startup_checks` the serving path does, the effective account
    included - under systemd `ExecStartPre` runs as the unit's `User=`, so the
    check and the run are the same identity, which is the whole point of V2.

    Implements: SR-019, LLR-002
    """
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--bind-check":
        # setup-ai-cli.sh calls this BEFORE anything is installed, so the shell
        # and the service cannot hold two different opinions about what "the
        # docker bridge" means. One fact, one home - the review found the shell
        # carrying its own copy of the old 172.16.0.0/12 rule.
        try:
            print(resolve_bind(argv[1]))
            return 0
        except (BindRefused, IndexError) as exc:
            sys.stderr.write("ai-cli BIND REFUSED: {}\n".format(exc))
            return 2
    try:
        config = Config()
        routes = startup_checks(config)
    except (ConfigError, BindRefused, UnsafeRouteError, ValueError) as exc:
        sys.stderr.write("ai-cli REFUSED TO START: {}\n".format(exc))
        return 2
    if "--check" in argv:
        print(
            "ai-cli OK: bind={} user={} routes={}".format(
                config.bind, config.user, ",".join(sorted(routes)) or "(none enabled)"
            )
        )
        return 0
    if not routes:
        sys.stderr.write(
            "ai-cli REFUSED TO START: routes-enabled is empty - an absent "
            "enable-list is 'no routes', which is a consent decision\n"
        )
        return 2
    gate = RouteGate(config.cooldown, config.success_cooldown, config.max_concurrent)
    server = make_server(config, make_handler(config, routes, gate))
    sys.stderr.write(
        "ai-cli listening on {}:{} as {} (max {} concurrent session(s))\n".format(
            config.bind, config.port, config.user, gate.max_concurrent
        )
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
