# The AI CLI service

**Spine:** SN-016, SR-019, LLR-002, TC-002, IF-011. Ratified as HomeHub
`open-items.md` **A40, 2026-09-09**.

A plain hub service — **no container** — that takes messages from the other
containers and from on-box jobs and answers them by running **one headless CLI
session** on the household subscription. Messages in; model and depth as
*configuration*; structured output via `--json-schema`.

## Why there is no container

A40's own reasoning, and it is the reason this directory holds a systemd unit
instead of a Dockerfile: a container was only ever wanted for **containment**,
and it delivers none here. The subscription credential has to come from a home
directory a human logged into interactively, so a container must mount that
home — and the isolation is punctured at exactly the point that mattered.
Running it as a plain service on the hub, where the RDP desktop that performs
those logins already exists, is the honest version of the same posture.

Containment comes instead from four properties, each **asserted by a test**
rather than written into a unit file and checked by eye.

## The four containments, and what asserts each

| # | Property | Enforced by | Asserted by |
|---|---|---|---|
| 1 | A **dedicated unprivileged account**, never `hub` | `assert_service_account`, `setup-ai-cli.sh` refusals, `User=homehub-ai` in the unit | `TestServiceAccount` (8 cases); guards suite A2, A3 |
| 2 | **Read-only tool use**, no `--dangerously-*` flag anywhere | `assert_safe_template`, re-run at every launch | `TestNoDangerousFlags`, `TestReadOnlyToolUse`; guards suite A1, A9, A11 |
| 3 | Bound to **loopback or the docker bridge**, never the LAN | `resolve_bind`, before the socket exists | `TestBindAddress` (18 cases); guards suite A4, A5, A6, A8 |
| 4 | A **scratch working directory per request** | `request_scratch` + `StateDirectory` | `TestRequestScratch` (4 cases); guards suite A10 |

Why 1 matters: `hub` is the box's only account with a real shell **and**
`(ALL) NOPASSWD: ALL` (`stack/remote-ui/homehub-desktop-session.sh`). A service
running as `hub`, driving an agent, taking requests from other containers is
arbitrary code execution as a passwordless-sudo user chosen by the caller. The
account keeps a real shell on purpose — `claude setup-token` is run inside an
RDP session as this user — but it has no sudo, and `setup-ai-cli.sh` re-checks
that on **every boot**, because an account created without sudo can be given it
later.

Why 2 is *read-only* rather than *no tools*: for analysis work the agent should
be able to read the data and reason over it, and unable to write anywhere or
mutate state. Claude rows carry `--permission-mode plan` with an
`--allowedTools` read-only allow-list and an explicit `--disallowedTools` deny
(deny wins); codex rows carry `--sandbox read-only --ask-for-approval never`.

## THE TRAP: do not copy ai-template's flags

`ai-template`'s registry is reused here for its **structure** — the pair-row
registry and its loader, the per-row cooldown, the session-launch decisions
(prompt on stdin when the template carries no `{prompt}` (WI-216); codex's
`--output-last-message` because it echoes the prompt into stdout (WI-217); a
reader thread; a per-session timeout; stdin never left open).

Its **flags are not reused, and must never be.** Every Claude row in
`ai-template/docs/agents.toml` carries `--dangerously-skip-permissions` and
every codex row carries `--dangerously-bypass-approvals-and-sandbox`. That file
says why: *that repo IS the unattended run and consent is explicit.* This
service's work is chosen by a caller, so the consent does not transfer.

Two things enforce that rather than trusting it:

* `assert_safe_template` refuses any token beginning `--dangerously-` — **by
  prefix**, so a dangerous flag a future CLI version invents is caught before
  anyone updates this repo — at startup *and* at every launch, because the
  registry is a tracked file that can also be edited on the box.
* `TestNoDangerousFlags` and guards-suite check **A1** grep the whole committed
  tree for the prefix. A copy-paste from `ai-template` fails the build.

## The latency floor

`--bare` — the fast, predictable startup meant for scripts — **ignores the
OAuth token and requires an API key**, so a service leaning on the subscription
**cannot use it** and pays full startup per invocation: hooks, MCP servers,
skills and `CLAUDE.md` all load every call. It is on the banned-token list for
that reason as well as for safety, so nobody "optimises" the floor away by
quietly moving the service onto a metered key.

**Measured 2026-09-09**, `claude 2.1.201`, trivial prompt
(`-p "Reply with the single word: ok" --output-format json --max-turns 1`),
three runs, from an empty working directory:

| run | wall | `duration_ms` (model) | non-model startup |
|---|---|---|---|
| 1 | 4125 ms | 1897 ms | ~2228 ms |
| 2 | 2914 ms | 1234 ms | ~1680 ms |
| 3 | 3730 ms | 2087 ms | ~1643 ms |

**So the floor is roughly 1.6–2.2 s of startup on top of whatever the model
takes, and ~3–4 s wall for the cheapest possible call.**

Two caveats, stated because they change the number rather than because they are
tidy: this was measured on the **dev PC (Windows)**, not the hub — the AK41 is
a slower box and the figure there will be worse, not better — and the working
directory was empty, so no project `CLAUDE.md` or skills loaded. A per-request
scratch directory (containment 4) keeps the real service close to that
best case, which is a second reason to like it.

Consequences: **do not put this service on a synchronous request path** that
expects sub-second answers, and set `AI_CLI_TIMEOUT_SECONDS` against the
measured floor rather than a guess. The `--output-format json` result also
carries `total_cost_usd` per call, which is worth logging beside the usage
gauges — subscription usage is one pool and a scheduled job competes with the
interactive quota.

## The contract

```
POST http://127.0.0.1:8791/v1/ask
{
  "route":    "ANALYSIS-QUICK",
  "schema":   { "type": "object", "properties": { "category": {"type":"string"} } },
  "messages": [ {"role": "user", "content": "…"} ]
}
-> 200 { "route": …, "result": <constrained by schema>, "raw": <the CLI's json> }
```

**Model and depth are configuration, not request fields.** A caller names a
route id that is in both the registry and the enable-list, and nothing else: a
request carrying `model` or `effort` is ignored, and a test asserts that. The
registry (`agents.registry.csv`) is a **catalog**; `routes-enabled` is the
**consent**, and an absent enable-list is "no routes", never "every route".

`schema` is **required**. The output contract is the point of the service and
an unconstrained answer is not offered.

`GET /healthz` returns the enabled route ids.

### Failure is reported as failure

A CLI that exits 0 having emitted no result object produced **no answer**, and
this service returns 502 for it and cools the route. Assert the artifact, not
the exit code — a green run that yielded nothing is exactly the false evidence
this repo has paid for before.

## Files

| File | What it is |
|---|---|
| `ai_cli_service.py` | the service: registry, the four guards, session launch, the HTTP shell |
| `agents.registry.csv` | the route catalog (ai-template's IF-045 schema, safe flags) |
| `routes-enabled` | the consent half — which routes a caller may name |
| `homehub-ai-cli.service` | the systemd unit (dedicated account, 0700 state tree) |
| `setup-ai-cli.sh` | idempotent account creation + install; refuses `hub`, sudo, a LAN bind |
| `tests/ai-cli-guards.test.sh` | the hermetic guards suite (16 checks), in `run-hermetic-tests.sh` |

Python unit tests live at `tests/test_ai_cli_service.py` (53 cases).

## Deploying

1. Set `AI_CLI_ENABLED=true` (and review the other `AI_CLI_*` knobs) in
   `stack/.env`. All of them are declared in `stack/.env.example` and in
   HomeHub's `FieldSchema.psd1`.
2. Re-run firstboot, or `sudo bash /opt/homehub/stack/ai-cli/setup-ai-cli.sh`.
3. **A human still has to sign the CLIs in.** RDP to the box as
   `AI_CLI_USER` and run `claude setup-token`. It falls back to pasting a code,
   which is documented to work when the link is opened on a different computer,
   so copying the URL to the dev PC is fine and no SSH tunnel is needed — the
   RDP desktop has no browser. The credential lands in
   `~homehub-ai/.claude/.credentials.json` at 0600.
4. It **does not survive a reimage** — the same posture as IceDrive. The
   reimage checklist names the re-login step.

**Adds no apt package.** The service is Python 3 stdlib only and `python3` is
already in `packages.list`; the CLIs are not apt packages at all (SN-016) and
are installed over SSH by the Owner, deliberately outside the offline closure.
So no apt export re-run is owed by this change.
