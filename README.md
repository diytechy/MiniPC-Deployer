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
| tier-2 opt-in catalog | Immich/PhotoPrism · Jellyfin · Navidrome · Audiobookshelf · Vaultwarden · Home Assistant/Mosquitto · Syncthing · FreshRSS/Mealie/Homepage · diun — compose profiles, **off by default** ([stack/README.md](stack/README.md) §9) | per-service |

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

Three progressively-more-real gates precede flashing the real AWOW:

| Gate | What | Status |
|---|---|---|
| V1 — [`sim/`](sim/) | full stack on WSL2/Docker vs. a mock OIDC provider (Dex stands in for Google); split-horizon DNS, multi-user isolation, and the bash backup service + restore drill all exercised for real | **GREEN** — all checks pass |
| V2 — launcher (Personal repo, `MINI_PC_Setup/`) | the Mini-serv rebuild launcher validated in Windows Sandbox (real task-import/reg/share rungs) | **GREEN** |
| V3 — [`vmtest/`](vmtest/) | the REAL autoinstall booted in a local Hyper-V VM — every container baked into the ISO payload (Q10.9 B+), zero registry pulls at first boot | scripted + smoke-tested; **the boot itself is Peter's step** (needs elevation + the Hyper-V feature) |

Dev-box container runtime (WSL2 + docker-ce) is installed and verified — see
[docs/status.md](docs/status.md) for the full ledger, including exactly which
images are pinned and their digests.

## Configuring the REAL box (not the sim)

`vmtest/`'s scripts (`build-seed.sh`, etc.) are **VM-test-only** — they
substitute throwaway SIM secrets and an ephemeral SSH key on purpose, so
never flash `vmtest/.out/*.iso` onto real hardware. To build the actual
deploy image:

1. Fill in a real `stack/.env` from `.env.example` (every knob is documented
   there and in [stack/README.md](stack/README.md) §2) and put your real SSH
   public key into `stack/autoinstall/user-data` (§"Build the USB").
2. Filling `.env` today is a **manual** step — the automated secret-handoff
   script proposed in `Personal\SECRET_HANDOFF_PROPOSAL.md` (a sibling repo;
   extends Peter's existing DPAPI credential pattern) is still an unratified
   proposal, not implemented. Once ratified it would materialize `.env` from
   Personal's credential store instead of hand-editing.
3. Run `vmtest/export-images.sh` (it reads the real pinned tags in
   `.env.example`, not sim values) so the real USB also carries every
   container image baked in (Q10.9 B+) — see stack/README.md §3.

## Development

This repo follows a gated, requirement-traced process. The working brief is
[AGENTS.md](AGENTS.md); the method is [docs/process.md](docs/process.md). Start
with the code map in [docs/architecture.md](docs/architecture.md) and the
current state in [docs/status.md](docs/status.md).
