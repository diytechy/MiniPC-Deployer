#!/usr/bin/env python3
"""The hub's AI CLI service: messages in, a schema-constrained answer out.

ONE RESPONSIBILITY: turn an HTTP request carrying messages into ONE headless
CLI session, run under the four containments A40 ratified on 2026-09-09, and
return the structured result. It is a plain hub service — no container. A40's
own reasoning: a container was only ever wanted for containment, and it
delivers none here, because the subscription credential has to come from a home
directory a human logged into interactively, so a container must mount that
home and the isolation is punctured at the only point that mattered.

WHAT IS REUSED, AND WHAT IS DELIBERATELY NOT
    Reused from `ai-template/project-trajectory/scripts` — its *structure*, the
    debugged half A40 says not to rebuild:
      * the pair-row registry and its loader (agent_route.load_registry, its
        IF-045 schema) — one row per (model x route), the row IS the allow
        matrix, the id is a join key and is never parsed;
      * the per-row cooldown, independent by construction
        (agent_route.available / .cool);
      * the session-launch decisions (agent_session, its IF-064): a template
        with no `{prompt}` pipes the prompt to the child's STDIN so a large
        prompt never meets the OS command-line cap (WI-216); codex captures
        `--output-last-message` because it echoes the prompt into stdout
        (WI-217); a reader thread so the child cannot block on a full pipe; a
        per-session timeout; stdin never left open on an interactive wait.
    NOT reused — its FLAGS. Every Claude row in `ai-template/docs/agents.toml`
    carries a `--dangerously-*` permission-skipping flag and every codex row a
    `--dangerously-*` sandbox-bypassing one (README.md names both; it is the
    one file here allowed to write them out, so the tree-wide grep that bans
    them does not fail on its own documentation). That file says why: that repo
    IS the unattended run and consent is explicit. This service's work is
    chosen by a caller, so the consent does not transfer, and copying the flags
    would make the service arbitrary code execution on request.

THE FOUR CONTAINMENTS, each with the symbol that enforces it and the test that
asserts it (tests/test_ai_cli_service.py; the acceptance is the point of the
block, so none of them is left to be asserted by eye in a unit file):
    1. a dedicated unprivileged account, never `hub`  -> `assert_service_account`
    2. read-only tool use, no `--dangerously-*` flag  -> `assert_safe_template`
    3. bound to loopback or the docker bridge, never the LAN -> `resolve_bind`
    4. a scratch working directory per request       -> `request_scratch`

Contract:
  Inputs:  a POST of {"messages":[{"role","content"},...], "route": "<id>",
           "schema": {json-schema}}. `route` names a row in the registry AND
           the enable-list; model and depth are CONFIGURATION (the row), never
           request fields, so a caller can pick an allowed route and nothing
           else.
  Outputs: 200 with the CLI's `--output-format json` result, whose `result` is
           constrained by the supplied `--json-schema`; 4xx/5xx with a
           JSON {"error": ...}. Never a partial turn reported as success.
  Config:  AI_CLI_* keys, see `Config` and stack/.env.example.
  Raises:  UnsafeRouteError, BindRefused, ConfigError — all loudly, never a
           silent degrade to a less contained run.

Stdlib only, Python 3.8+ (the check harness runs 3.8 on the dev PC).
Implements: SR-019, LLR-002
"""

import csv
import ipaddress
import json
import os
import re
import shlex
import shutil
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
#: adds is caught before anyone updates this file.
DANGEROUS_FLAG_PREFIX = "--dangerously-"

#: `--bare` is the fast, predictable startup meant for scripts, and it CANNOT
#: be used here: it ignores the OAuth token and requires an API key (A40), and
#: this service leans on the subscription. Banned so nobody "optimises" the
#: latency floor away by silently moving the service onto a metered key.
BANNED_TOKENS = frozenset({"--bare"})

#: Per family, the token groups that MUST be present for tool use to be
#: read-only. For analysis work the right setting is read-only tool use, NOT no
#: tools: the model can read the data and reason over it, and cannot mutate.
REQUIRED_READONLY_TOKENS = {
    # claude: a settings file resolves deny, then ask, then allow, and deny
    # wins — so the deny list is the load-bearing half and is required too.
    "ANTHROPIC": (("--permission-mode",), ("--disallowedTools",)),
    # codex: read-only is one of three sandbox values; the approval policy has
    # to be `never` or an unattended run wedges waiting for a human.
    "OPENAI": (("--sandbox",), ("--ask-for-approval",)),
}

#: The value each family's read-only token must carry.
REQUIRED_TOKEN_VALUES = {
    ("ANTHROPIC", "--permission-mode"): frozenset({"plan"}),
    ("OPENAI", "--sandbox"): frozenset({"read-only"}),
    ("OPENAI", "--ask-for-approval"): frozenset({"never"}),
}

#: Accounts this service must never run as. `hub` is the box's only account
#: with a real shell that ALSO carries `(ALL) NOPASSWD: ALL`
#: (stack/remote-ui/homehub-desktop-session.sh) — a service running as `hub`,
#: driving an agent, taking requests from other containers is arbitrary code
#: execution as a passwordless-sudo user chosen by the caller.
FORBIDDEN_ACCOUNTS = frozenset({"hub", "root"})

#: Networks the service may bind. Loopback, or the docker bridge — never the
#: LAN. The callers are the other containers and on-box jobs.
DEFAULT_BRIDGE_NETS = ("172.16.0.0/12",)

ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]*$")
MODEL_SLUG_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")
TIERS = ("quick", "medium", "strong")


class ConfigError(Exception):
    """Configuration that cannot be honoured. Always fatal at startup."""


class UnsafeRouteError(Exception):
    """A registry row that would run less contained than A40 ratified."""


class BindRefused(Exception):
    """A bind address outside loopback and the docker bridge."""


# ---------------------------------------------------------------------------
# 1. The registry — ai-template's IF-045 schema, its loader's shape.
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
    than an empty pool — ``{}`` would read as "no routes are enabled", which is
    a consent decision, not a broken file.

    The Model cell is slug-checked because it rides ``{model}`` substitution
    into an argv (ai-template's repo-review 2026-07-21 H-4).

    Contract:
      Inputs:  path: str — a CSV carrying the IF-045 columns.
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
# 2. Containment: the account, the read-only template, the bind.
# ---------------------------------------------------------------------------


def split_cmd(template):
    """Split a command template into argv tokens (ai-template's shell-string
    form, with backslash escaping DISABLED so Windows-style paths survive)."""
    lex = shlex.shlex(template or "", posix=True)
    lex.whitespace_split = True
    lex.escape = ""
    lex.commenters = ""
    return list(lex)


def assert_safe_template(route):
    """Refuse a route whose command template would run less contained than A40
    ratified. Raises ``UnsafeRouteError`` naming the offending token.

    THIS IS A RUNTIME GUARD, NOT A CONFIG LINT. The registry is a tracked file
    a human edits; a lint that only runs in CI is a file that can still be
    edited on the box. Every launch re-checks, so the worst case of a bad edit
    is a refused request rather than an agent running with the sandbox off.

    Three refusals, in the order they matter:
      * any ``--dangerously-*`` token — the ai-template flags, by prefix;
      * ``--bare`` — ignores the OAuth token, needs an API key (A40);
      * a missing or wrong-valued family read-only token — read-only tool use
        is the containment, and an absent flag means the CLI's own default,
        which for neither family is read-only.

    Contract:
      Inputs:  route: Route
      Outputs: list[str] — the checked argv tokens, so a caller builds argv
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
                "route {}: refuses {!r} — ai-template carries that flag because "
                "THAT repo is itself the consented unattended run; this "
                "service's work is chosen by a caller (A40, 2026-09-09)".format(
                    route.id, tok
                )
            )
        if tok in BANNED_TOKENS:
            raise UnsafeRouteError(
                "route {}: refuses {!r} — it ignores the OAuth token and "
                "requires an API key, and this service leans on the "
                "subscription (A40)".format(route.id, tok)
            )
    required = REQUIRED_READONLY_TOKENS.get(route.family)
    if required is None:
        raise UnsafeRouteError(
            "route {}: family {!r} has no declared read-only contract — "
            "refusing rather than guessing one".format(route.id, route.family)
        )
    for group in required:
        present = [t for t in group if t in tokens]
        if not present:
            raise UnsafeRouteError(
                "route {}: missing read-only token {} — an absent flag means "
                "the CLI's own default, which is not read-only".format(
                    route.id, "|".join(group)
                )
            )
        for tok in present:
            allowed = REQUIRED_TOKEN_VALUES.get((route.family, tok))
            if allowed is None:
                continue
            idx = tokens.index(tok)
            value = tokens[idx + 1] if idx + 1 < len(tokens) else ""
            if value not in allowed:
                raise UnsafeRouteError(
                    "route {}: {} is {!r}; expected one of {}".format(
                        route.id, tok, value, "|".join(sorted(allowed))
                    )
                )
    return tokens


def assert_service_account(user, account_groups=(), sudoers_lines=()):
    """Refuse to run as a privileged account. Raises ``ConfigError``.

    ``hub`` is the box's only account with a real shell AND
    ``(ALL) NOPASSWD: ALL``, so a service running as `hub` and driving an agent
    on request is arbitrary code execution as a passwordless-sudo user. `root`
    is refused for the obvious reason. Beyond the name, the account's ACTUAL
    privilege is checked: membership of a sudo-granting group, or any sudoers
    line naming it, is a refusal — because "we made a new account" is worth
    nothing if it was then added to `sudo`.

    Contract:
      Inputs:  user: str — the account the unit declares.
               account_groups: iterable[str] — groups this account belongs to.
               sudoers_lines: iterable[str] — sudoers content to search.
      Outputs: str — the user, unchanged.
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
    privileged = {"sudo", "admin", "wheel", "root", "docker", "adm"}
    bad = sorted(privileged.intersection({str(g).strip() for g in account_groups}))
    if bad:
        raise ConfigError(
            "service account {!r} is in privileged group(s) {} — a dedicated "
            "account that was then given sudo (or docker, which is sudo by "
            "another door) is not a containment".format(name, ", ".join(bad))
        )
    pattern = re.compile(
        r"(?<![A-Za-z0-9_-])%?" + re.escape(name) + r"(?![A-Za-z0-9_-])"
    )
    for line in sudoers_lines:
        text = line.split("#", 1)[0]
        if pattern.search(text):
            raise ConfigError(
                "service account {!r} is named in a sudoers rule: {!r}".format(
                    name, line.strip()
                )
            )
    return name


def resolve_bind(address, bridge_nets=DEFAULT_BRIDGE_NETS):
    """Return the bind address, or raise ``BindRefused``.

    Loopback and the docker bridge are the whole allow-list. A wildcard
    (``0.0.0.0`` / ``::``) is refused BY NAME rather than by "is it in a bad
    network", because a wildcard is precisely the mistake that puts the
    household's interactive credential on the LAN and it does not read as a LAN
    address. Every other literal address is refused too, and a hostname is
    refused because what it resolves to is not this file's to promise.

    Contract:
      Inputs:  address: str — a literal IPv4/IPv6 address.
               bridge_nets: iterable[str] — CIDRs treated as the docker bridge.
      Outputs: str — the address, unchanged.
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
            "bind {!r} is not a literal IP address — a hostname's resolution is "
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
    for cidr in bridge_nets:
        if addr in ipaddress.ip_network(cidr):
            return text
    raise BindRefused(
        "bind {!r} is neither loopback nor inside the docker bridge {} — the "
        "callers are the other containers and on-box jobs, and nothing off-box "
        "has any business reaching this service".format(text, ",".join(bridge_nets))
    )


# ---------------------------------------------------------------------------
# 3. Containment: the per-request scratch working directory.
# ---------------------------------------------------------------------------


class request_scratch(object):
    """Context manager giving ONE request its own 0700 working directory under
    the scratch root, and removing it on every exit path.

    A working directory is not a detail for these CLIs: they are coding agents,
    their sandbox confines the filesystem to the working directory, and
    ``CLAUDE.md`` plus the skills of whatever tree the process starts in load
    per invocation. Starting a caller-chosen job in a real tree would hand it
    that tree; starting every job in the same directory would let one request
    see the last one's leavings. So: a fresh directory per request, inside a
    root that holds nothing real.

    Yields a dict with ``cwd`` and ``schema_path`` — both per-request values,
    so neither belongs in the tracked command template.

    Contract:
      Inputs:  root: str — AI_CLI_SCRATCH_ROOT; created 0700 if absent.
      Outputs: {"cwd": str, "schema_path": str}
      Raises:  OSError if the root cannot be created — loudly, because a
               service that silently falls back to /tmp has lost this
               containment without saying so.
    Implements: SR-019, LLR-002
    """

    def __init__(self, root):
        self.root = root
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
        # Removed on EVERY exit path, including a timeout or a crash: the
        # scratch tree is the one place a caller's content lands on disk.
        if self.path:
            shutil.rmtree(self.path, ignore_errors=True)
            self.path = None
        return False


# ---------------------------------------------------------------------------
# 4. Cooldowns (ai-template's semantics; per row, independent by construction).
# ---------------------------------------------------------------------------


def available(cooldowns, route_id, now):
    """True when ``route_id`` is not cooling down. ``cooldowns`` maps a route id
    to the epoch it is available again."""
    until = (cooldowns or {}).get(route_id)
    return until is None or until <= now


def cool(cooldowns, route_id, now, seconds):
    """Put ``route_id`` on cooldown until ``now + seconds``. In place."""
    cooldowns[route_id] = now + max(0, seconds)


# ---------------------------------------------------------------------------
# 5. Session launch — the argv, the stdin decision, the run, the result.
# ---------------------------------------------------------------------------


def render_messages(messages):
    """Flatten a messages array into the single prompt the CLIs take on stdin.

    The CLIs are one-shot ``-p`` drivers, not chat endpoints: there is no
    multi-turn wire format to hand them, so a conversation is rendered as
    labelled turns. Roles are restricted to user|assistant so a caller cannot
    smuggle a fabricated ``system`` frame past the configured route.

    Raises ValueError on an empty array, a non-object entry, an unknown role or
    empty content — loudly, because a silently dropped turn is a wrong answer
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


def build_argv(route, scratch, tokens=None):
    """Build ``(argv, env_overrides)`` for one session.

    The prompt is NOT here. Every shipped template carries no ``{prompt}``, so
    the prompt goes to the child's STDIN (ai-template WI-216) — immune to the
    OS command-line cap, which a messages array will exceed long before anyone
    notices. The two per-request values are appended here rather than tracked
    in the registry: ``--json-schema`` (the structured-output contract) and,
    for codex, ``--output-last-message``, because codex echoes its banner and
    the whole prompt into stdout so its stdout is not the result (WI-217).

    Contract:
      Inputs:  route: Route; scratch: the dict from `request_scratch`;
               tokens: the already-checked token list, or None to check now.
      Outputs: (argv: list[str], env_overrides: dict[str,str])
      Raises:  UnsafeRouteError via `assert_safe_template`.
    Implements: SR-019, LLR-002
    """
    tokens = assert_safe_template(route) if tokens is None else tokens
    argv = [t.replace("{model}", route.model) for t in tokens]
    if route.family == "ANTHROPIC":
        argv += ["--json-schema", scratch["schema_path"]]
    elif route.family == "OPENAI":
        argv += [
            "--output-last-message",
            os.path.join(scratch["cwd"], "last-message.txt"),
        ]
    env_overrides = {}
    for pair in (route.env or "").split(","):
        if "=" in pair:
            key, _, value = pair.partition("=")
            env_overrides[key.strip()] = value.strip()
    return argv, env_overrides


def run_session(argv, cwd, timeout, env_overrides, stdin_input):
    """Run ONE headless session and return ``(exit_code, output, timed_out)``.

    The thin I/O shell — everything that decides lives above. Reused shape from
    ``agent_session.run_session``: stdin is FED AND CLOSED so the child can
    never wedge on an interactive read; a reader thread drains stdout so the
    child cannot block on a full pipe; the timeout is per session and kills the
    child rather than leaking it.

    Implements: SR-019, LLR-002
    """
    env = dict(os.environ)
    env.update(env_overrides or {})
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
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
        proc.kill()
        proc.wait()
    reader.join(timeout=5)
    return proc.returncode, "".join(chunks), timed_out


def parse_json_result(output):
    """Best-effort parse of a ``--output-format json`` transcript.

    Returns the result object, or ``None`` when the child emitted none — which
    is reported as "no typed outcome", never guessed. A green exit code with no
    result object is exactly the false evidence to guard against: assert the
    artifact, not the exit code.
    Implements: SR-019, LLR-002
    """
    text = (output or "").strip()
    if not text:
        return None
    lines = text.splitlines()
    for candidate in (text, lines[-1] if lines else ""):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


# ---------------------------------------------------------------------------
# 6. Configuration and the HTTP shell.
# ---------------------------------------------------------------------------


class Config(object):
    """The service's configuration, read once at startup from the environment.

    Config:  AI_CLI_BIND (loopback or docker-bridge literal address),
             AI_CLI_PORT (int), AI_CLI_USER (the dedicated account),
             AI_CLI_REGISTRY, AI_CLI_ENABLED_ROUTES (the enable-list file),
             AI_CLI_SCRATCH_ROOT, AI_CLI_TIMEOUT_SECONDS,
             AI_CLI_COOLDOWN_SECONDS. All declared in stack/.env.example.
    Raises:  BindRefused if AI_CLI_BIND is outside the allow-list.
    Implements: SR-019, LLR-002
    """

    def __init__(self, env=None):
        env = os.environ if env is None else env
        here = os.path.dirname(os.path.abspath(__file__))
        self.bind = resolve_bind(env.get("AI_CLI_BIND", "127.0.0.1"))
        self.port = int(env.get("AI_CLI_PORT", "8791"))
        self.user = env.get("AI_CLI_USER", "homehub-ai")
        self.registry_path = env.get("AI_CLI_REGISTRY") or os.path.join(
            here, "agents.registry.csv"
        )
        self.enabled_path = env.get("AI_CLI_ENABLED_ROUTES") or os.path.join(
            here, "routes-enabled"
        )
        self.scratch_root = env.get("AI_CLI_SCRATCH_ROOT", "/var/lib/homehub-ai/scratch")
        self.timeout = int(env.get("AI_CLI_TIMEOUT_SECONDS", "300"))
        self.cooldown = int(env.get("AI_CLI_COOLDOWN_SECONDS", "120"))


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


def handle_ask(body, config, routes, cooldowns, runner=run_session, now=None):
    """The request path: validate, launch one contained session, parse.

    Returns ``(status_code, payload_dict)``. No HTTP in here, so the whole
    contract is unit-testable with a fake runner — which is how this block's
    acceptance criteria are asserted rather than eyeballed.

    Contract:
      Inputs:  body: dict — {"messages": [...], "route": str,
                             "schema": {json-schema object}}
      Outputs: (int, dict). 200 carries {"route","result","raw"}.
      Raises:  nothing — every failure is a status code and an error string.
    Implements: SR-019, LLR-002
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
    if not available(cooldowns, route_id, now):
        return 429, {"error": "route {!r} is cooling down".format(route_id)}
    schema = body.get("schema")
    if not isinstance(schema, dict) or not schema:
        return 400, {
            "error": "schema is required — the output contract is the point of "
            "this service; an unconstrained answer is not offered"
        }
    try:
        prompt = render_messages(body.get("messages"))
    except ValueError as exc:
        return 400, {"error": str(exc)}

    with request_scratch(config.scratch_root) as scratch:
        try:
            argv, env_overrides = build_argv(route, scratch)
        except UnsafeRouteError as exc:
            return 500, {"error": str(exc)}
        with open(scratch["schema_path"], "w", encoding="utf-8") as fh:
            json.dump(schema, fh)
        code, output, timed_out = runner(
            argv, scratch["cwd"], config.timeout, env_overrides, prompt
        )

    if timed_out:
        cool(cooldowns, route_id, now, config.cooldown)
        return 504, {
            "error": "route {!r} timed out after {}s".format(route_id, config.timeout)
        }
    result = parse_json_result(output)
    if code != 0 or result is None:
        cool(cooldowns, route_id, now, config.cooldown)
        # Assert the artifact, not the exit code: a zero exit with no result
        # object is a session with no typed outcome, and is reported as one.
        return 502, {
            "error": "route {!r} produced no typed result (exit {})".format(
                route_id, code
            ),
            "raw": (output or "")[-2000:],
        }
    return 200, {"route": route_id, "result": result.get("result"), "raw": result}


def make_handler(config, routes, cooldowns):
    """Bind the request path into a BaseHTTPRequestHandler. Thin by design."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "homehub-ai-cli"

        def _send(self, status, payload):
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, {"ok": True, "routes": sorted(routes)})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/ask":
                return self._send(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                return self._send(400, {"error": "bad JSON body: {}".format(exc)})
            if not isinstance(body, dict):
                return self._send(400, {"error": "body must be a JSON object"})
            status, payload = handle_ask(body, config, routes, cooldowns)
            self._send(status, payload)

        def log_message(self, fmt, *args):
            # To the journal, and WITHOUT the request body: the body is the
            # caller's content and this service's log is not the place for it.
            sys.stderr.write("ai-cli %s\n" % (fmt % args))

    return Handler


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


def sudoers_lines():
    """Every sudoers rule line on the box, best effort."""
    lines = []
    for path in ["/etc/sudoers"] + _listdir("/etc/sudoers.d"):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines.extend(fh.readlines())
        except OSError:
            continue
    return lines


def main(argv=None):
    """Start the service. Refuses loudly rather than starting less contained.

    ``--check`` validates configuration and exits without binding, so
    provisioning can assert the containments without a running service.

    Implements: SR-019, LLR-002
    """
    argv = sys.argv[1:] if argv is None else argv
    try:
        config = Config()
        assert_service_account(config.user, account_groups(config.user), sudoers_lines())
        routes = selectable_routes(config)
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
            "ai-cli REFUSED TO START: routes-enabled is empty — an absent "
            "enable-list is 'no routes', which is a consent decision\n"
        )
        return 2
    server = ThreadingHTTPServer(
        (config.bind, config.port), make_handler(config, routes, {})
    )
    sys.stderr.write(
        "ai-cli listening on {}:{} as {}\n".format(config.bind, config.port, config.user)
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
