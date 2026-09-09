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
| 1 | A **dedicated unprivileged account**, never `hub` | `assert_effective_account` (the process's *own* identity), `assert_service_account` + `collect_sudoers_lines`, the `User=` drop-in `setup-ai-cli.sh` generates from `AI_CLI_USER` | `TestServiceAccount` (17 cases); guards suite A2, A3, **A9** |
| 2 | **Read-only tool use**, the pinned CLI and nothing else | `assert_safe_template` against `FAMILY_CONTRACTS`, `assert_safe_env`, `resolve_executable` — re-run at every launch | `TestPinnedCommand`, `TestEnvIsAnAllowList`, `TestNoDangerousFlags`, `TestReadOnlyToolUse`; guards suite A1, A11, **A12, A13** |
| 3 | Bound to **loopback or a real docker bridge address**, never the LAN | `resolve_bind` + `docker_bridge_addresses`, before the socket exists | `TestBindAddress`, `TestTheServiceCanServeWhatItAccepts`; guards suite A4, A5, A6, A8 |
| 4 | A **scratch working directory per request** | `request_scratch` (+ `StateDirectory` as a floor) | `TestRequestScratch` (6 cases); guards suite **A10** |

### What the 2026-09-09 cross-review changed

A second model family reviewed this block and **rejected it with 16 confirmed
findings**; the coordinator verified the worst three in source. All four
criteria are unchanged — they simply were not met. The common shape of the
defects is worth keeping in mind before editing any guard here:

> **every guard checked that a string was PRESENT where the property was about
> IDENTITY.**

* **V1 — the template guard did not pin the executable.** It asked whether
  `--permission-mode` and `--disallowedTools` appeared, never what the command
  *was*, so a row reading `python3 -c '…' --permission-mode plan
  --disallowedTools Bash` passed every check and ran arbitrary code as the
  service account. Now argv[0] is pinned per family, resolved only on
  `AI_CLI_BIN_PATH`, and **every token must be in that executable's declared
  vocabulary** (`FAMILY_CONTRACTS`) with a value this repo constrains. Adding a
  flag is a reviewed edit to `ai_cli_service.py`, not a registry cell.
* **V2 — the account asserted was not the account that ran.** The unit
  hard-coded `User=homehub-ai` while everything else validated `AI_CLI_USER`.
  Now `setup-ai-cli.sh` writes the unit's `User=`/`Group=` from that same knob,
  and the service asserts its **effective identity** at startup.
* **V3 — "the docker bridge" was the whole of `172.16.0.0/12`.** A household
  LAN on `172.20.0.0/16` is inside it, so a LAN bind was accepted. Now a
  non-loopback bind must be an address a local `docker0`/`br-*` interface is
  **actually carrying**.

Also fixed, each with its own test: duplicate/late flags (only the *first*
occurrence was inspected), the never-inspected `--allowedTools` value, `Env=`
as an unguarded `PATH`/`CODEX_HOME` injection surface, sudoers `#include` read
as a comment, `::1` accepted then unbindable, unbounded concurrency and body
size, the cooldown race (every thread read `available()` before any thread
wrote), a timeout that killed only the direct child, a zero-exit non-result
reported as success, a schema written for codex and never passed to it, and a
scratch cleanup whose failure was swallowed.

**And the test standard the review actually failed us on:** three of the shell
checks were *vacuous* — they greped an artifact rather than observing a
behaviour, so they would have passed with the guard deleted. Every guard here
is now **mutation-checked**: break it, watch the test go red, restore it, watch
it go green. If you add a guard, do that run and record it. An assertion that
cannot fail is worse than no assertion, because it produces false evidence.

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
this repo has paid for before. "A result" means a `type: "result"` frame that
is not flagged `is_error` and whose `result` is not null (or, for a
last-message family like codex, a non-empty last-message file that parses):
the cross-review found a `{"type":"status"}` frame with exit 0 being returned
as **200 with `"result": null`**, which is the same false evidence one level
down.

### A refusal says which refusal it is

Every **completed** call cools its route — successes included, so a hot loop
cannot launch back-to-back sessions on the household subscription. The Owner's
condition when ratifying that (2026-09-09) is this section: **a caller must be
able to tell a cooling route from a broken one, and learn when it is next
available.** So a refusal is machine-readable, never prose to be parsed:

| Condition | Status | `status` | `reason` | Retry hint |
|---|---|---|---|---|
| paced after a **completed** call | 429 | `cooling` | `success-pacing` | `retry_after_seconds` + `retry_at`, and a `Retry-After` header |
| backing off after a **failed** call | 429 | `cooling` | `failure-backoff` | same fields — but seconds, not "shortly" |
| a session already running on this row | 429 | `running` | `in-flight` | `retry_after_seconds` only, as a **floor** |
| the box is at `AI_CLI_MAX_CONCURRENT` | 503 | `busy` | `at-capacity` | none |

Read `reason`, not the sentence. The two cooldowns are one code but **not one
condition**: `success-pacing` is the short, expected gap (`AI_CLI_SUCCESS_
COOLDOWN_SECONDS`, default 5 s) and nothing went wrong, while `failure-backoff`
is `AI_CLI_COOLDOWN_SECONDS` (default 120 s) after a session that produced no
result. A caller told "try again shortly" and then made to wait two minutes
stops believing the hint, which is why they are labelled apart.

`retry_after_seconds` is an integer, **rounded up**, and the `Retry-After`
header carries the same number — mirrored from the body, so the two cannot name
different times. It is a *minimum* wait in both places, never a promise: for an
`in-flight` row nobody can know when the running session ends (it may run to
`AI_CLI_TIMEOUT_SECONDS`), so the floor handed back is the pacing gap that must
follow it, and there is deliberately **no `retry_at`**. `at-capacity` gets no
hint at all rather than a guessed one — and it is a different code on purpose,
because it is the *box* that is full: retrying a different route will not help,
which is the opposite of the advice for a cooldown.

A genuine failure carries none of this vocabulary: no `status: cooling`, no
retry fields. "Cooling" and "broken" are distinguishable by a field test.

### And a bounded box

This is a small always-on machine. `AI_CLI_MAX_CONCURRENT` sessions run at
once **across all routes** (default 1) and a further request is a 503, not a
queue; a route already in flight is a 429 taken under the same lock as the
cooldown — the check, the claim and the retry hint describe the same instant —
so a burst cannot launch N sessions in the gap between the check and
the claim; a body without a `Content-Length`, or above
`AI_CLI_MAX_BODY_BYTES`, is refused **before it is read**; connections are
capped at `AI_CLI_MAX_CONNECTIONS` and idle ones time out after
`AI_CLI_SOCKET_TIMEOUT_SECONDS`; and a timed-out session is killed as a
**process group**, so nothing it spawned outlives it.

## Files

| File | What it is |
|---|---|
| `ai_cli_service.py` | the service: registry, the four guards, session launch, the HTTP shell |
| `agents.registry.csv` | the route catalog (ai-template's IF-045 schema, safe flags) |
| `routes-enabled` | the consent half — which routes a caller may name |
| `homehub-ai-cli.service` | the systemd unit (dedicated account, 0700 state tree) |
| `setup-ai-cli.sh` | idempotent account creation + install; refuses `hub`, sudo, a LAN bind |
| `tests/ai-cli-guards.test.sh` | the hermetic guards suite (21 checks), in `run-hermetic-tests.sh` |

Python unit tests live at `tests/test_ai_cli_service.py` (120 cases).

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
