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

## mini-serv-sim — Samba fixtures + the bash backup service (WI-10.15)

`mini-serv-sim/` is Mini-serv's stand-in: a Samba server exposing three fixture
shares plus a privileged runner that cifs-mounts them and runs the **real**
`stack/backup` service end to end.

```bash
sim/mini-serv-sim/run-backup-sim.sh        # full 6-step cycle + restore drill
sim/mini-serv-sim/run-drivepower-sim.sh    # WI-10.10 hdparm standby CALL CONTRACT (mock shim)
sim/mini-serv-sim/run-backup-sim.sh --down # tear down
```

`run-drivepower-sim.sh` proves the WI-10.10 **drive power** contract without real
spinning platters: it puts a mock `hdparm` (logs every call) and mock `curl`
(captures each NagLight POST) on PATH in the runner and asserts the sequence —
(a) `-S 0` disable to each device at run start; (b) the configured timeout
re-issued to each device on normal exit; (c) on a FORCED mid-run failure the
restore still fires via the EXIT trap AND the run still posts `ok=false`; (d) no
devices → zero `hdparm` calls, unchanged green cycle. It cleans its shims up after
so a subsequent `run-backup-sim.sh` sees the real tools. Whether a given USB
enclosure actually *honors* `hdparm` standby is a hardware burn-in check.

Requires the awow-sim stack up first (`sim/run-sim.sh`) — the runner feeds the
sim NagLight tracker and shares its network. The run performs steps 1-6 of
HOMELAB_TOPOLOGY.md (cifs pull → tar/zstd → hash+manifest → retention → offsite
push → NagLight feed) and then the **restore drill**: reconstruct minecraft from
the archive+manifest, `diff -r` byte-equality against the live share, delete the
`plugins/` subtree and reconstruct again, and confirm the fixture "secret"
(a fake rcon password) round-trips intact.

The three shares (fictional, committed):
- `//mini-serv/minecraft` — a Paper-server tree: `paper-1.20.4-435.jar`,
  `plugins/` (3 valid jars with parseable `plugin.yml`), `server.properties`
  (fake rcon password), `world/`.
- `//mini-serv/satisfactory` — a save tree (`SaveGames/…/*.sav`).
- `//mini-serv/icedrive` — the empty offsite target (writable).

Regenerate the binary fixtures deterministically with
`sim/mini-serv-sim/fixtures/generate-binaries.sh`.

### Starting the fixture shares standalone (WI-10.16)

MinecraftKeeper's `--execute` validation (WI-10.16) needs only the Samba shares:

```bash
sim/run-sim.sh                                   # once, for the shared network
sim/mini-serv-sim/run-backup-sim.sh --shares-only
# -> //mini-serv/{minecraft,satisfactory,icedrive}  user: awow  pass: simpass
```

The shares are reachable from any container on the `awow-sim_default` network as
`//mini-serv/<share>`; the live tree stays read-only (minecraft/satisfactory
are exported read-only), matching the "live share stays read-only" rule.
