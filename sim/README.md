# AWOW-sim — local V1 simulation of the whole stack

This directory is the **V1 "AWOW-sim"** (WI-10.14/10.15): the full container
collection runs locally and proves itself against a fictional fixture world,
*before* any hardware. It is a compose **overlay over the real stack**, never a
fork — the sim must exercise `stack/docker-compose.yml`, so a bug there is a bug
the sim catches.

Everything in this directory is **fictional and committable** (Q10.6 public-repo
rule): sim domains, sim users, throwaway bcrypt hashes, sim tokens. Real values
live only in the gitignored `stack/.env`.

## Prerequisites

- A container runtime (WSL2 + docker-ce; WI-10.13).
- The NagLight image built once: `docker build -t naglight:local ../NagLight`.

## Run it

```bash
sim/run-sim.sh          # brings the overlay up + provisions the Technitium zone
sim/validate-sim.sh     # the V1 GATE: 6 checks, PASS/FAIL, nonzero exit on fail
sim/run-sim.sh --down   # tear down (removes volumes)
```

`run-sim.sh` is:

```
docker compose -p awow-sim \
  -f stack/docker-compose.yml -f sim/docker-compose.sim.yml \
  --env-file sim/.env.sim up -d
```

## The V1 gate (`validate-sim.sh`)

1. **service health** — every service with a healthcheck is `healthy`; the two
   distroless services (oauth2-proxy, Dex) are probed functionally (/ping,
   OIDC discovery).
2. **split-horizon DNS** — `dig @technitium` returns the sim LAN IP for the
   tracker/actual/apex names.
3. **Caddy + auth** — through Caddy's **internal CA**: tracker → 302 to Dex (no
   basic_auth); actual → 401 without basic_auth, 200 with it.
4. **oauth2-proxy + Dex** — unauthenticated → 302 to Dex, then a **full headless
   login** (curl cookie-jar dance) lands on the authenticated tracker page.
5. **multi-user isolation** — `X-Forwarded-User` A vs B (direct to `tracker:8787`,
   the documented trust model) see only their own data; a no-identity request is
   403; A's `/api/export` zip is named for A and contains only A's items.
6. **/api/feed round-trip** — a feed POST flips the item in `/api/today`
   (ok=true→done, ok=false→cleared).

## What the overlay changes, and the REAL↔SIM deltas

| Concern | Real deploy | Sim | Why |
|---|---|---|---|
| DNS | Technitium host-net :53 | bridge + alt host API port; `dig @technitium` from the client | systemd-resolved owns loopback :53 on WSL; can't share host :53 |
| OIDC | `provider=google` | `provider=oidc`, issuer = Dex (`sim/dex/`) | no Google client needed to exercise the whole login path |
| TLS | public ACME | Caddy `local_certs` internal CA (`sim/caddy/Caddyfile.sim`) | a local box can't ACME a real public domain |
| basic_auth | `{$VAR}` from `.env` | inlined fictional bcrypt in Caddyfile.sim | avoids `$`-in-env-file escaping; the credential is a sim fixture |
| ddns | Cloudflare updater | disabled (`profile: sim-disabled`) | zero real Cloudflare calls |
| aux (Kuma/Dozzle) | LAN_IP-bound | disabled | they bind a fictional LAN_IP; out of the V1 gate scope |
| tracker data perms | (needs NagLight Dockerfile chown — see status.md) | `init-perms` one-shot chowns the volume to uid 1000 | surfaced a real NagLight bug; the sim reproduces the fixed end-state |

**Deltas the sim genuinely cannot cover** (documented, for the hardware burn-in):
real Google consent, publicly-trusted ACME certs, Technitium binding the host's
real `:53`, and the AWOW hardware itself.

## Fixtures for the follow-on session (WI-10.16)

`mini-serv-sim/` (WI-10.15) exposes Samba fixture shares (a Minecraft-server
tree, a Satisfactory-save tree, an icedrive-sync target). See the mini-serv-sim
section below for how to start them standalone for the MinecraftKeeper
`--execute` validation.
