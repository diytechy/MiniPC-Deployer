# litellm — the pinned private route to the dev PC

Implements **SR-047** and the last outstanding piece of **SR-044** (the hold
shim). Interfaces **IF-021**, **IF-023**.

One OpenAI-compatible proxy with exactly one deployment, no cloud credential,
and no database. It is the lane a request uses when it **must** reach the
local model or fail.

---

## The property — corrected, after the first version of it was false

> A request that reaches this container can reach the dev PC or it can fail.
> There is no third outcome.

**The first version of this README said that was true because the container
holds no cloud credential and names no cloud endpoint. That conclusion was
wrong**, and an adversarial review (codex `gpt-5.6-terra`, 2026-09-19) found
it. Measured against the pinned digest, not inferred from documentation:

- the proxy serves **618 routes**, of which **32 are provider pass-throughs**
  registered entirely independently of `model_list` — `/vertex_ai/{endpoint}`,
  `/gemini/`, `/anthropic/`, `/bedrock/`, `/cohere/`, `/mistral/`, `/openai/`,
  `/azure/`, `/vllm/` and more;
- the Vertex handler **forwards caller-supplied credentials** when the proxy
  holds none of its own. Its own log line says so: *"default_vertex_config not
  set, forwarding caller-provided headers"*;
- a probe request carrying an `x-goog-api-key` header made the container open a
  connection to `aiplatform.l.rep.googleapis.com:443`. The one-deployment
  `model_list` constrained none of it.

**The credential can arrive in the request.** Its absence from the config was
therefore never a boundary. Two controls now stand where one imaginary one did:

| | What it is | Status |
|---|---|---|
| **`stack/llm-isolation/`** — DOCKER-USER rules for egress **and INPUT rules for the host bridge**, allowing this container's pinned address to reach the dev PC's inference port and the readiness service, and REJECTing everything else | **the guarantee**, below the application | this is the one that is true |
| **`allowed_routes`** in `config.yaml` — a whitelist that removes the pass-through routes | **defence in depth**, in-process policy | verified working; not authoritative |

If the two ever disagree, **the fence is the one that is true.** `allowed_routes`
is evaluated by the very component being constrained and upstream may change
how it is enforced on any version bump.

### The fence needs BOTH chains, and the first version had only one

A second review round found that the DOCKER-USER half alone left the **host
bridge wide open**. Packets addressed to the host's own bridge address are
delivered locally — that is INPUT, and DOCKER-USER never sees them. The first
version asserted this was covered by `game-isolation.sh`; that fence covers
`172.28.90.0/24`, and this lane is `172.28.91.x`. Measured on the live box:
INPUT held a REJECT for the game subnet and **nothing** for this one.

What that reached is the point. As the game fence's own banner records, the
host bridge exposes SSH, cockpit, and **Technitium's admin console on :5380**,
past both of its guards — a console that can rewrite every name the household
resolves. *A private lane that can reprogram DNS is not a private lane.*

The INPUT half now allows exactly the bridge gateway on the readiness port and
rejects the rest. It is inserted **below `ts-input`** (inserting above it would
break the tunnel admission) and **above every blanket ACCEPT** (appending would
put it below the remote-desktop fence's `--dports 21115:21119 -j ACCEPT`, which
matches any source). Both constraints were measured on the live chain, the
insertion point is found rather than hard-coded, and the resulting order is
read back and verified.

### Both were verified against the running container, 2026-09-19

```
/vertex_ai/v1/x        -> 403      /bedrock/x            -> 403
/gemini/v1/x           -> 403      /openai/v1/responses  -> 403
/anthropic/v1/messages -> 403
```

rejected in `user_api_key_auth`, *before* any handler and before any outbound
attempt — the container log shows the route-not-allowed rejection and no
connection attempt at all.

The per-key-allowlist alternative was rejected for the same reason the
credential-absence claim failed: it puts the decision inside a process that
still has the credentials. [BerriAI/litellm#8616](https://github.com/BerriAI/litellm/issues/8616)
is that bug in its pure form. Under this design virtual keys are not
load-bearing, so that issue is moot here.

Adding a second model to `config.yaml` still does not add a fallback — it
deletes the reason this container exists, and `tests/test_litellm_pin_route.py`
fails the build. But understand that the config file is the *weaker* of the two
controls.

## Why not just do this in the gateway

Because the gateway cannot express it, and this was read from the running
container on 2026-09-19 rather than inferred.

- An explicit `model` field there is a **preference, not a pin**. Its router
  moves the named model to the front of the chain (`splice`/`unshift`) and its
  proxy then retries down the rest of the chain for up to **20 attempts**.
- `ECONNREFUSED`, `timeout` and `503` are all on its retryable list — which are
  exactly the states of a sleeping dev PC. So "name the local model" fails over
  to a free cloud tier with a **200** in the normal case.
- Its chain is **global**: `SELECT ... FROM fallback_config fc WHERE fc.enabled
  = 1`, with no user, key or caller column. "Finance may not fall back" and
  "agents may" are not both sayable there at any setting.

The gateway's own source comment above that block reads *"Explicit `model`
field pins routing."* It does not. That comment is worth knowing about before
anyone re-derives this conclusion from it.

## How the two lanes compose

```
finance ─────────────────────────► litellm ──► dev PC
                                      ▲         (no fallbacks, no cloud key)
agents / phone ──► llm-gateway ───────┘
                        └───────────► 12 free cloud tiers
```

The gateway's **custom endpoint points at this container**, not at the dev PC
directly. So this is the sole path to the dev PC, and the wake-and-hold logic
is written once and serves both lanes.

When the box is cold:

| | behaviour | why it is right |
|---|---|---|
| `litellm` | **refuses** | substituting a cloud provider is the failure being prevented |
| `llm-gateway` | reads that refusal as retryable and walks to a free tier | the agents lane wants an answer from somewhere |

Each component makes the decision that is its job, and **finance never
traverses the component that substitutes**.

### The handoff is a substring match, and it is fragile

The gateway classifies retryability by substring-matching the error text. So
`devpc_hold_hook.py`'s refusal messages contain **"503"** and **"unavailable"**
on purpose, and deliberately avoid `429`/`quota`/`rate limit` — those would
also cause a fallback but would bench the dev PC on a rate-limit cooldown for
being asleep. Fall back, yes; lie about why, no.

TC-1025 asserts this against the gateway's **vendored matcher list**, dated and
with the path it was read from, because a test that checked our own message
against our own file could never fail. **Re-read that list when
`stack/llm-gateway/gateway-image.pin` rotates.**

## SR-044: this hook never falls back, and that is not a departure

SR-044 reads *"held while the box wakes, falling back rather than erroring."*
When it was written, one component was going to do both. It no longer does,
because the two lanes want opposite things from the same event.

The hold lives here; the **fallback lives one layer out**, in the gateway,
whose job that already is. The system still satisfies SR-044 for the agents
lane — held, then fell back — but **no single component does**. The finance
lane gets fail-closed for free, because it never reaches the substituting
component.

This is a real change to *where* a requirement is discharged, so it is carried
as **SR-047** and written up in Finance-Auditor's
`docs/design/llm-routing-topology.md` rather than absorbed quietly into an
implementation.

## What this container does not have, and why each absence is deliberate

| Absent | Because |
|---|---|
| A host port | Its only ingress is the compose bridge |
| A Caddy site | No dashboard worth reaching; its entire config is in git. Do not add one "for debugging" |
| A database | `store_model_in_db: false` makes `config.yaml` authoritative — with a DB, a model added through the admin UI would override this file and git would not record it |
| A volume | Follows from the above: nothing to persist |
| An entry in any backup set | Follows again. Contrast `llm_gateway_data`, which holds credentials and is not reproducible from this repo |
| An `.env` file of its own | Everything comes from `stack/.env` through compose. One fewer secret-bearing file on the box |
| A provider credential | The whole point |

The `api_key` in `config.yaml` is a **declared placeholder**, written in the
clear in a committed file on purpose. The dev PC's inference server has no
authentication of its own — the Windows firewall rule scoping it to the hub's
address is the control — but the client still requires an `Authorization`
header. A placeholder that *looks* like a real key would be worse than none, so
the test asserts the exact string.

## Operating notes

- **Enable with `LITELLM_ENABLED=true`, never by adding `litellm` to
  `COMPOSE_PROFILES`.** This bullet said the opposite until 2026-09-19. The
  profile route starts the container through firstboot's bulk
  `docker compose up -d`, which runs before the egress fence has been applied
  in that boot — so following the old instruction produced exactly the
  unfenced proxy this design exists to prevent. `homehub-litellm.service`
  passes `--profile litellm` itself, which is what makes naming it in the
  environment both unnecessary and unsafe.
- Two **file** mounts, not a directory mount. Replacing a bind-mounted
  directory's inode detaches the mount, and the container then keeps serving
  the old contents while the deploy reports success.
- `DEVPC_WAKE_URL` **empty disables the hold**, and that is the correct setting
  until the wake path is physically verified from S3 and S5. With no hold a
  cold box fails in seconds; with a hold and a wake that does not work, every
  cold request waits out `DEVPC_HOLD_CAP_SECONDS` first. The hook logs which
  mode it is in once at startup.
- The healthcheck probes `/health/liveliness`, which does **not** touch the dev
  PC. A probe that did would mark this container unhealthy every night the box
  is asleep — the normal case, and the same mistake `devpc-wake`'s `/health`
  deliberately avoids.
- `DEVPC_TARGET_MODEL` is the single source of truth for the model id: this
  proxy names it and `devpc-wake` looks for it in the dev PC's `/v1/models`
  listing to decide `ready`. The `openai/` prefix is applied in
  `docker-compose.yml`, because `openai/os.environ/X` inside the YAML does not
  substitute — LiteLLM triggers only on values that *begin* with `os.environ/`.

## Known gaps

- **Nothing consumes this lane yet.** Finance-Auditor has no AI code — its AI
  audit is still a design memo with no build and no registry row. This is the
  plumbing, deliberately built before the consumer so the consumer cannot be
  written against a lane that substitutes.
- **The hold has never held a real request**, because no model has been pulled
  to the dev PC and `DEVPC_TARGET_MODEL` is empty. The decision logic is
  proved by a truth table; the integration is not.
- **`/health/liveliness` is verified on the pinned image** (200,
  unauthenticated), not taken from upstream's docs.
- **The refusal's wire contract is verified**, not assumed. Against the running
  container with an unreachable readiness oracle, a held request returns
  **HTTP 503** with the message intact — containing `503` and `unavailable`,
  and none of `429`/`quota`/`rate limit`. This was review finding 2: the unit
  test formats a Python string and would have passed whether or not the text
  ever reached the wire. The unit test remains, and this is the evidence
  behind it. **Re-run it on any image pin rotation** — reproduce with an
  unroutable `DEVPC_WAKE_URL` (TEST-NET-3, `203.0.113.0/24`) and a short
  `DEVPC_HOLD_CAP_SECONDS`.
- **The fence has not been exercised against a real blocked destination.**
  The rules are programmed, read back and order-verified by
  `llm-isolation.sh`, but no test has confirmed *from inside the container*
  that a cloud connection is refused, or that the host bridge is closed except
  for the readiness port. That is a deploy-time acceptance step and no unit
  test claims it.
- **The hold's honest bound is `cap + one probe timeout`, not the cap.** One
  readiness check always happens, so a zero cap still serves a box that is
  already ready; everything after that first check is inside the cap. Stated
  because two review rounds were spent discovering that "hard cap" was not
  quite true — first by five seconds, then by the probe floor and an unclamped
  wake POST.
