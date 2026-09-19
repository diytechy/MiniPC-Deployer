# llm-gateway — the centralised LLM gateway

Implements **SN-043**, **SR-043**, **LLR-963**, **LLR-964**, **LLR-965**, **IF-023**.

One endpoint every LLM consumer points at — hub services, the Owner over the
tunnel, and coding agents on the dev PC — so provider keys are held in one place
and a provider can be swapped without touching a consumer.

Upstream: **FreeLLMAPI**, by Neu Software LLC — <https://freellmapi.co/>,
[github.com/tashfeenahmed/freellmapi](https://github.com/tashfeenahmed/freellmapi).

---

## The departure, stated plainly

The AI CLI block (A40, SR-019) is bound to loopback or a docker bridge address
and **never the LAN**. That posture survived a cross-review which rejected the
block with sixteen findings, and it is not relaxed here.

**This service is a different lane and is deliberately reachable off-box.**

|  | `ai-cli` | this gateway |
|---|---|---|
| Backs onto | the household subscription CLIs | free provider tiers + the local model |
| Callers | hub containers and on-box jobs | those, plus the Owner over the tunnel, plus coding agents on the dev PC |
| Bind | loopback / docker bridge only | bridge, fronted by Caddy |

The wider reach is for **this service only**. The two share no credential, no
account and no volume, no gateway route names an `ai-cli` endpoint, and a test
asserts `ai-cli`'s bind is unchanged by this addition — because the real risk of
adding a deliberately wider second lane is that somebody quietly widens the
first one to match.

## Why there is no host port

`vaultwarden` in the same compose file also publishes no LAN port, for a
different reason. Here the reason is that this origin fronts **every provider
credential the household holds**, so it stays on the docker bridge and is
reached only through the existing Caddy, which already terminates TLS and
already carries the `LAN_CIDR` `remote_ip` gate admitting the LAN and the tunnel
and returning 403 to everything else *before any handler runs*.

It also carries **its own bearer key on every request, including from the LAN**.
That is a departure from how the rest of this stack gates LAN services, and it
is deliberate: ambient LAN trust is not sufficient for this one origin. The key
is distinct from every provider credential, so revoking it costs no provider
access.

Upstream itself states the project is local-first and single-user and advises
against exposing it publicly. That is consistent with the above. **Do not read
the Caddy site as an invitation to widen it.**

## The image is pinned by digest

See [`gateway-image.pin`](gateway-image.pin). A credential concentrator that
auto-updates is exactly what the pin prevents.

At pinning time `latest` resolved to a **different digest than `v0.3.0`** —
`latest` follows `main`, not the newest release. Both are recorded in the pin
file so that is checkable rather than assertable.

**The upstream install script is not used.** Upstream's quickstart is a piped
curl-to-shell installer: unpinned, executes remote code at deploy time, and
produces a deployment no commit describes. This compose file is the install.

## The fork hazard — read this before touching the image reference

This project has **thousands of forks**, and a search for it returns forks
**ranked above the original**. An earlier pass of this very work misread a
fork's zero-star counters as the project's, and misread its correct pointer to
the author's own `ghcr.io` namespace as an unrelated third party — and on that
basis wrongly rejected the upstream the Owner had chosen.

Assume any search result for this project is a fork until you have checked the
author. **The pointer of record is the project site**, which names the canonical
repository directly. Not a search ranking.

## Operating notes

- Profile-gated and **off by default**: `COMPOSE_PROFILES=…,llm-gateway`.
- The dashboard wants an email + password on a server install; that is the
  gateway's own admin login and is not the bearer key.
- Provider keys are encrypted at rest with a key generated on first setup. The
  volume `llm_gateway_data` is therefore **not reproducible from this repo** and
  joins the **local** backup set — never the offsite set, because it holds
  credentials.
- Point consumers at **this gateway's URL, never a provider's**. A consumer
  found holding a provider URL is a defect in the consumer: it is what makes a
  provider swap invisible, and it is the whole reason this service exists.

## Known caution

This is a **0.x project** (`v0.3.0` at pinning). It is widely used and actively
developed, but it is young, and it is holding every provider credential. That is
an accepted trade for the free-tier quota tracking it provides, which was the
capability actually wanted. The alternative router (LiteLLM) speaks the same
API, so the swap stays cheap — provided consumers keep pointing at this URL.
