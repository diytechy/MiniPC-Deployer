# MiniPC-Deployer

The deploy repository for Peter's headless homelab box (the **AWOW AK41
always-on core**): a zero-touch, always-on Docker stack — split-horizon DNS
(Technitium), reverse proxy + TLS (Caddy), the NagLight life-tracker behind
Google sign-in (oauth2-proxy), Actual Budget, and LAN-only observability — with
an unattended Ubuntu autoinstall image and full LAN remote management. This repo
holds **configuration only** (compose, Caddy, autoinstall, provisioning); the
application code lives in its own repos (NagLight, Finance-Auditor, …).

> **Public-facing repo (Q10.6):** only `*.example` templates are tracked. No real
> secret, password, hash, email, or LAN detail is ever committed. Copy each
> `*.example` to its real name and fill it in locally.

## The stack

Everything lives under [`stack/`](stack/) — start with
[stack/README.md](stack/README.md):

| Service | Role | Auth |
|---|---|---|
| Technitium | split-horizon LAN DNS + recursion + blocklists | admin login |
| Caddy | reverse proxy + automatic TLS | — |
| oauth2-proxy | Google sign-in for the tracker | Google OAuth |
| tracker (NagLight) | the multi-user life tracker | via oauth2-proxy |
| Actual Budget | finances | basic_auth |
| Uptime-Kuma · Dozzle · (ntfy) | LAN-only observability | LAN-only |

Remote management of the headless box (SSH, Cockpit, the reimage ladder) is in
[REMOTE_MANAGEMENT.md](REMOTE_MANAGEMENT.md).

## Run it

There is no single "run" command — this repo produces a **deploy image**, not an
app. To bring the stack up on a Docker host, follow
[stack/README.md](stack/README.md) (`docker build -t naglight:local ../NagLight`
→ `docker compose up -d`). The root `run.{cmd,sh,command}` launchers are inert
(this is not a launchable product).

## Validate it

```
python scripts/check.py            # config-coverage + doc + registry gates (G1)
python scripts/validate_config.py  # env/Caddy/file coverage + YAML parse
```

> **No Docker on the dev machine (verified).** Validation here is **config-level
> only**; runtime bring-up is PENDING the first Docker host — see the honest
> ledger in [docs/status.md](docs/status.md).

## Development

This repo follows a gated, requirement-traced process. The working brief is
[AGENTS.md](AGENTS.md); the method is [docs/process.md](docs/process.md). Start
with the code map in [docs/architecture.md](docs/architecture.md) and the
current state in [docs/status.md](docs/status.md).
