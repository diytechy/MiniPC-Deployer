# Project Status — Blackboard

Live coordination for the gated process (see [process.md](process.md)). Keep the
**Current State** header short and current; append the audit log below (newest
last) — it is the record, not required reading for every pass.

---

## Current State

- **Active gate:** G1 — Requirements, UX & constraints. This is a config/infra
  repo delivered against a ratified brief (HOMELAB_RESTRUCTURE_PLAN.md); the
  requirement spine is intentionally **high-level** (proportionality doctrine).
- **Round:** 1
- **Open items:**
  - **Needs Peter** _(ratification / manual steps — dial=HIGH)_:
    - OI-1 — **Google OAuth client** must be created in Google Cloud (needs
      Peter's account); redirect URI to register:
      `https://tracker.<domain>/oauth2/callback` →
      [stack/.env.example](../stack/.env.example)
    - OI-2 — **Reimage-over-LAN ladder** is HIGH-RISK; the memo presents options
      with checkboxes. Nothing destructive is implemented until Peter checks one
      → `REMOTE_MANAGEMENT.md` (added in WI-10.12)
    - OI-3 — **Push** each commit (agents lack the SSH key) →
      this repo
  - **In flight** _(driver; no approval needed)_:
    - OI-4 — layering WI-10.2/10.11/10.12 onto the migrated base →
      [stack/docker-compose.yml](../stack/docker-compose.yml)
- **Assumptions (unattended):** see the Assumptions log below.
- **Next action:** Peter reviews + pushes; creates the Google OAuth client;
  reacts to the reimage-ladder checkboxes. Runtime bring-up on the first Docker
  host / the AWOW itself.

## Scope (restated from the brief)

- **Goal:** the deploy repo for the headless AWOW AK41 always-on box — an
  unattended, self-healing Docker stack (DNS, reverse proxy + TLS, the NagLight
  tracker behind Google sign-in, Actual Budget, LAN observability) plus an
  Ubuntu autoinstall image and full LAN remote management. **Config only** — app
  code lives in NagLight / Finance-Auditor / MinecraftKeeper.
- **Stakeholders / end user(s):** Peter (homelab operator); the tracker's hosted
  end-users reach it only through oauth2-proxy.
- **Active hats:** Stakeholder, UX/Docs, System Engineer, Software Engineer, Test
  Engineer, **Network**, **Security/Ops** (the domain hats this infra scope
  needs).
- **Supported platforms:** the deploy target is **Linux** (Ubuntu 24.04 on the
  AWOW box); authored on Windows. Not a launchable product.
- **Constraints:**
  - **No Docker on the dev machine (verified).** Validation is config-level only;
    runtime bring-up is PENDING a Docker host (see the ledger below).
  - **Public-facing repo (Q10.6):** only `*.example` templates tracked; no real
    secret/hash/email/LAN detail/personal name. Local commit identity pinned to
    `diytechy <diytechy@users.noreply.github.com>`.
  - **Kit:** minimum profile, decision dial **HIGH** (secrets-adjacent infra).
  - **No product source in this repo** → the Python `ruff`/`pytest`/arch-map
    steps are dropped (not left passing vacuously, ADOPTING.md §3). The
    product-layer check is `scripts/validate_config.py`.
- **Non-goals:** a container registry / CI publishing (deferred, Q10.2 — local
  builds for now); the NagLight app code and its multi-user engine (NagLight
  repo, WI-10.4/10.5); the secret-handoff script (WI-10.3, Peter ratifies).
- **Definition of done:** the repo's G1 gate is green (`check.py`), `.env.example`
  enumerates every knob, config coverage validates, and the honest validation
  ledger records what remains PENDING a Docker host.

## Honest validation ledger (WI-4.7 note carried forward)

| Check | State |
|---|---|
| `docker compose config` / live bring-up | **PENDING — no Docker on dev box (verified)** |
| curl health endpoints, `dig`, OAuth round-trip, tear-down | **PENDING — Docker host / the AWOW** |
| Shell scripts `bash -n` | PASS |
| `docker-compose.yml`, `meta-data`, `user-data` YAML parse | PASS (PyYAML) |
| Every compose `${VAR}` has an `.env.example` key | PASS (`validate_config.py`) |
| Every Caddy `{$VAR}` passed by the caddy service env | PASS |
| Bind-mount sources + autoinstall-referenced files exist | PASS |
| `check.py` (G1: config-validate + registry-integrity + doc-navigability) | PASS |

Runtime validation must be redone on the first Docker host before the burn-in
checklist (stack/README §6) can be signed.

## Gate Sign-offs

Add columns for any active domain hats. Drop the `G-Release` row for a one-off
deliverable.

| Gate | Stakeholder | UX/Docs | System Eng | Test Eng | Human |
|---|---|---|---|---|---|
| G1 — Requirements/UX/Constraints | PENDING | PENDING | PENDING | n/a | PENDING |
| G2 — Decomposition & Test Coverage | n/a | n/a | PENDING | PENDING | PENDING |
| G3 — Implementation | n/a | n/a | PENDING | PENDING | PENDING |
| G-Release — Release readiness | n/a | n/a | n/a | PENDING | PENDING |
| G-Final — Acceptance | PENDING | n/a | n/a | (evidence) | PENDING |

---

## Audit log

<!-- Append verdict blocks here per process.md §5. Newest at the bottom. -->

### DRIVER — G1 — Round 1 — 2026-07-03
Scaffolding created. Starting G1.

### Assumptions log (unattended, dial=HIGH — Peter to confirm/revert)
- A1 — Layout: migrated `life-tracker/deploy/*` under `stack/` at the repo root
  (kept the kit's `docs/`/`scripts/` roots). On-box path renamed `deploy/` →
  `stack/` under `/opt/awow-core/`; the box hostname/opt-dir keep the `awow-core`
  name (faithful to the source; Peter knows it).
- A2 — This repo is treated as gate **G1** (requirements-agreed) — config is the
  deliverable, there is no compiled source to carry to G2/G3. The Python
  product/arch-map steps are dropped, not left vacuous.
- A3 — oauth2-proxy image pinned to `v7.6.0`, Google provider, allow-list via
  `authenticated-emails.txt` (Q10.5). Redirect URI `…/oauth2/callback`.
- A4 — SSH is **key-only** by default in the autoinstall (locked password),
  per WI-10.12 "key-only auth". Cockpit installed but **not** proxied publicly.
- A5 — Kept the `TRACKER_DATA_REMOTE` clone/pull entrypoint behaviour from the
  source; multi-user (D3) sets it blank + `TRACKER_COMMIT=false` (documented).

### DRIVER — G1 — Round 1 — 2026-07-03 (migration + spine)
Migrated the deploy stack, wired the tracker to `naglight:local`, authored the
high-level SN/SR spine (8 SN, 11 SR), and added `scripts/validate_config.py` as
the config-repo product check. `check.py` (G1) green; integrity 0. WI-10.2 (oauth
+ Caddy re-route), WI-10.11 (aux containers), WI-10.12 (remote mgmt) layered in
subsequent commits.

### DRIVER — G1 — Round 1 — 2026-07-03 (WI-10.2 oauth2-proxy + Caddy re-route)
Added the oauth2-proxy service (Google provider, allow-list file, cookie secret,
identity headers to the tracker). Re-routed the Caddyfile: tracker host →
oauth2-proxy (no basic_auth); Actual + dns keep basic_auth (split into
per-service snippets since Caddy resolves {$VAR} once at load). Firstboot now
materializes the allow-list from `OAUTH2_PROXY_ALLOWED_EMAILS`. Documented the
PETER MANUAL STEP (Google OAuth client) in .env.example + stack/README. Redirect
URI to register: `https://tracker.<domain>/oauth2/callback`. config-validate
green (25 compose vars covered, 8 Caddy vars passed).

<!-- agent-setup --> Agent setup (2026-07-03): agents=`claude`; skills materialized: downstream-resync, gate-advance, registry-hygiene. AGENTS.md remains the canonical, agent-neutral guide (skills are opt-in accelerators, not a process gate).
