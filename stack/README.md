# AWOW AK41 always-on core — deploy stack

Zero-manual-config, always-on box image for the **AWOW AK41** (Celeron J4125,
8 GB, x86). Flash a USB → boot the AWOW → the whole stack comes up on its own.
Migrated from `life-tracker/deploy/` (WI-10.1); NagLight is now the source of the
tracker image.

Services (all health-checked, all `restart: unless-stopped`):

- **Technitium DNS** — split-horizon LAN DNS + recursion + ad-blocklists; its
  HTTP API is the health/config surface.
- **Caddy** — reverse proxy + automatic TLS (valid on the LAN via split-horizon).
  `basic_auth` guards the single-user services (Actual, the DNS console) only.
- **oauth2-proxy** — Google sign-in in front of the tracker subdomain (D1/D2);
  forwards verified identity headers to the tracker container.
- **tracker** — the **NagLight** web container (`naglight:local`), multi-user via
  trusted headers (D3).
- **Actual Budget** — finances on its own subdomain, behind `basic_auth`.
- **ddns** — Cloudflare dynamic-DNS updater (qmcgaw/ddns-updater): keeps the
  apex + wildcard A records pointed at the home IP, checked every
  `DDNS_PERIOD` (default 5m). Replaces the legacy DDNS-Cloudflare-PowerShell
  scripts on Mini-serv (WI-9 Q4). Needs a NEW scoped Cloudflare token — the
  old share token is rotation-flagged (see `.env.example`).
- **Uptime-Kuma · Dozzle · (ntfy)** — auxiliary LAN-only observability
  (WI-10.11); see §8.
- **Finance-Auditor** — the daily finance audit pipeline (SR-014/IF-004):
  triggers Actual's bank sync, runs the audit rules, posts a de-identified
  status to the tracker. **Profile-gated** (`finance-auditor` in
  `COMPOSE_PROFILES`) until it passes its own G-Release/G-Final, then promoted
  to core. Image from the private sibling repo via the resolver, with
  `@actual-app/api` pinned to `ACTUAL_IMAGE_TAG` (rebuild on Actual pin bumps).
  Its snapshots volume is raw finance data: LOCAL backup only, **never**
  `OFFSITE_SETS`.
- **Tier-2 opt-in catalog (SR-012)** — Immich / PhotoPrism, Jellyfin, Navidrome,
  Audiobookshelf, Vaultwarden, Home Assistant + Mosquitto, Syncthing,
  FreshRSS / Mealie / Homepage, diun — all behind compose **profiles**, OFF by
  default and excluded from the baked ISO payload; see §9.

This directory is the **image pipeline**. It is self-contained and committed with
placeholders only — no secrets. Copy `.env.example` → `.env` and fill it in;
`.env`, the Technitium API token, and the first-boot marker are gitignored.

---

## What's in here

```
stack/
  docker-compose.yml          the services, health-checked, restart:unless-stopped
  .env.example                every knob (domains, LAN IP, tokens, data-repo remote, image tags)
  caddy/Caddyfile             reverse proxy; oauth2-proxy for tracker, basic_auth for actual/dns
  oauth2-proxy/
    authenticated-emails.txt.example   allow-list template (real file gitignored)
  mosquitto/
    mosquitto.conf              MQTT broker config (tier-2 homeassistant/mosquitto profiles, §9)
  tracker/
    Dockerfile.deprecated     REFERENCE ONLY — NagLight owns the canonical build (WI-10.4)
    entrypoint.sh.deprecated  REFERENCE ONLY
    README.md                 why these are deprecated + the local build step
  provision/
    provision-technitium.sh   ZERO-TOUCH DNS: token + zone + split-horizon records + forwarders + blocklists (idempotent)
    healthcheck.sh            one-shot PASS/FAIL box check (Technitium API, dig, tracker, actual)
  autoinstall/
    user-data                 Ubuntu autoinstall: partition, user, docker, drop stack, first-boot unit
    meta-data                 NoCloud datasource companion
    awow-firstboot.service    systemd oneshot that runs the bring-up once
    firstboot.sh              compose up + provisioning + point host resolver at local DNS
    powertune.{service,sh}    per-boot low-power auto-tune (powertop) + USB-storage guard
```

> **Low-power units (per boot).** `user-data`'s `late-commands` also install two
> oneshots that re-apply non-persistent power tuning on every boot: `powertune`
> (powertop auto-tune, above) and — for the backup drive(s) — `backup-standby`
> (a conservative `hdparm -S` spin-down default; the unit + script live under
> [`stack/backup/`](backup/), WI-10.10 drive power). The package list adds
> `hdparm`, which is **not** guaranteed on Ubuntu Server. `backup-standby` is a
> clean no-op until `BACKUP_DRIVE_DEVICES` is set in the backup config.

---

## 1. Build the NagLight image (local, Q10.2)

There is no registry yet, so every host builds the tracker image locally from a
sibling NagLight checkout:

```sh
docker build -t naglight:local ../../NagLight   # adjust path to your checkout
```

(or uncomment the `build:` fallback in `docker-compose.yml`).

**Dev boxes: use the resolver instead.** NagLight (and later Finance-Auditor)
are private repos, so `scripts/ensure-local-images.sh` resolves each
locally-built image permissively — already present → sibling-checkout build →
a declared public image (`TRACKER_PUBLIC_IMAGE` in `.env`, empty until the app
repo publishes) — and fails loudly naming all three fixes otherwise.
`sim/run-sim.sh` calls it automatically; compose and export-images.sh keep
consuming the same `naglight:local` ref regardless of which path supplied it.

### Image delivery to the AWOW — Q10.9 B+ (ALL-IMAGES, baked into the ISO)

The AWOW does **not** pull any image from a registry at first boot. Per Peter's
locked **Q10.9 B+** decision, EVERY stack image — the locally-built
`naglight:local` **and** every public image (technitium, caddy, oauth2-proxy,
actual, ddns, uptime-kuma, dozzle, ntfy) — is `docker save`d into the ISO deploy
payload and `docker load`ed at first boot. So a freshly-imaged box comes up "from
infancy": **zero registry/internet dependency for container images**, versions
pinned (in `.env.example`, see the "Image tags" block) to exactly what the
AWOW-sim validated. What boots == what was validated; no drift from moving
`latest` tags.

Mechanism (see `../vmtest/`):

```
vmtest/export-images.sh   ->  docker save each pinned image -> deploy-payload/images/*.tar
autoinstall late-commands ->  copy deploy-payload/ -> /opt/awow-core/ (images and all)
autoinstall/firstboot.sh  ->  docker load /opt/awow-core/images/*.tar  BEFORE  compose up
```

The full pin set + registry digests are recorded in `../docs/status.md`. Bump a
pin only deliberately, then re-run `export-images.sh` and rebuild the ISO.

**GHCR stays additive-later** (Q10.9 option A): if day-2 update pain ever
appears, a private registry can be layered on for `compose pull && up -d` over
SSH — it does not replace the baked payload, which remains the first-boot path.
`firstboot.sh` also **degrades gracefully**: if a seed is built without the image
payload, it logs loudly and falls back to pulling public images at compose up
(and `naglight:local` must then be staged some other way).

## 2. Prepare `.env`

```sh
cp .env.example .env
$EDITOR .env
```

Fill in at minimum:

| Key | What |
|---|---|
| `DOMAIN` | your public apex (e.g. `example.tld`) |
| `LAN_IP` | the AWOW's LAN IP — **give it a DHCP reservation** at this address |
| `ACME_EMAIL` | email for Let's Encrypt |
| `ACTUAL_BASICAUTH_HASH` / `DNS_BASICAUTH_HASH` | `docker run --rm caddy:2-alpine caddy hash-password --plaintext 'yourpass'` |
| `TECHNITIUM_ADMIN_PASSWORD` | strong password (set on Technitium's first start) |
| `OAUTH2_PROXY_CLIENT_ID` / `_SECRET` | from your Google OAuth client (**Peter manual step**, below) |
| `OAUTH2_PROXY_COOKIE_SECRET` | `openssl rand -base64 32 \| tr -- '+/' '-_'` |
| `OAUTH2_PROXY_ALLOWED_EMAILS` | comma-separated Google accounts permitted into the tracker |
| `TRACKER_DATA_REMOTE` | git remote of your private **data** repo (single-user); blank for multi-user |
| `CLOUDFLARE_ZONE_ID` / `CLOUDFLARE_API_TOKEN` | dynamic DNS (`ddns` service) — a **new** scoped token (Zone→DNS→Edit, this zone only); never reuse the old plaintext token found on `\\Mini-serv\setup` |
| `BACKUP_DRIVE_DEVICES` / `BACKUP_DRIVE_STANDBY` | backup-drive spin-down (WI-10.10) — space-separated `/dev/disk/by-id/...` paths (never `sdX`, it renumbers); empty = no-op |

> **This table lists the knobs that need YOUR values filled in.** For the full
> set including ones with sensible defaults, read `.env.example` top to bottom
> — it's the source of truth and every entry is commented. Mind its **QUOTING
> RULE** header (values with spaces must be double-quoted — the file is both a
> compose env-file and shell-sourced by firstboot/provisioning). Tier-2 opt-in
> knobs (`COMPOSE_PROFILES`, `MEDIA_ROOT`, `EXTRA_SUBDOMAINS`, per-service
> pins/ports/passwords) live in the same file — see §9.

**Filling these in today is a manual step.** An automated secret-handoff
script is proposed (not yet built) in the sibling `Personal` repo's
`SECRET_HANDOFF_PROPOSAL.md` — it would extend Peter's existing DPAPI
credential pattern to materialize this `.env` instead of hand-editing. Still
awaiting Peter's ratification checkboxes; hand-fill `.env` until then.

### PETER MANUAL STEP — Google OAuth client (required for the tracker)

The tracker subdomain is gated by Google sign-in. Before first bring-up, create
the OAuth client (needs Peter's Google account — an agent cannot):

1. console.cloud.google.com → **APIs & Services → Credentials**.
2. Configure the **OAuth consent screen** (External). While unverified, add each
   allowed Google account as a **Test user** (or publish the app). Scopes:
   `openid`, `email`, `profile` (the slacker-tracker pattern Peter trusts uses
   `email profile`).
3. **Create Credentials → OAuth client ID → Web application**.
4. Register **Authorized redirect URI** exactly:
   `https://tracker.<your-domain>/oauth2/callback`
   (e.g. `https://tracker.example.tld/oauth2/callback`). No JavaScript origin
   needed — this is a server-side flow.
5. Paste the Client ID + secret into `.env`.

### PETER MANUAL STEP — bank sync in Actual (SimpleFIN, one-time, in-app)

Actual **owns the SimpleFIN relationship** (Finance-Auditor, when it lands,
only *triggers* Actual's sync — it never talks to banks). After first
bring-up: open `https://actual.<domain>`, set Actual's server password, then
link SimpleFIN under its bank-sync settings. The credential is stored
**server-side in the `actual_data` volume** — never in `.env`, never in git.
It survives every container update and comes back from the SR-013 volume
backup after a reimage (see REMOTE_MANAGEMENT.md "State & credentials").
Not doable from the sim — the sim is hermetic, and a real bank credential
must never enter a throwaway fictional volume; use the real box or the
real-secrets rehearsal VM.

---

## 3. Build the USB (autoinstall)

The AWOW installs **Ubuntu Server 24.04 LTS** unattended, then first-boot brings
the stack up.

1. **Download** the Ubuntu Server 24.04 LTS live ISO from ubuntu.com.
2. **Write the ISO to USB** (`dd`, Rufus, or balenaEtcher). This is USB #1.
3. **Attach the autoinstall + payload.** The installer looks for a NoCloud
   datasource:
   - **Second USB (simplest):** label a second stick `CIDATA`; put
     `autoinstall/user-data` and `autoinstall/meta-data` at its root. Copy the
     whole repo to `deploy-payload/` on that stick, **plus the baked image tars
     to `deploy-payload/images/`** (Q10.9 B+ — run `vmtest/export-images.sh`
     first). The autoinstall `late-commands` copy `deploy-payload/` into
     `/opt/awow-core`, so the stack lands at `/opt/awow-core/stack` and the
     images at `/opt/awow-core/images` where first-boot `docker load`s them.
   - **One USB (remaster):** unpack the ISO, add `/nocloud/` with
     `user-data` + `meta-data`, add kernel arg
     `autoinstall ds=nocloud;s=/cdrom/nocloud/`, add `/deploy-payload/` (repo +
     `images/` tars), repack with `xorriso`. `vmtest/build-repacked-iso.sh`
     automates exactly this.

   > Before writing, **replace the placeholders** in `user-data`: the SHA-512
   > password hash and, ideally, an SSH key (then set `allow-pw: false`). Do
   > **not** commit a filled-in `user-data`.

   > **The stick carries your secrets in PLAINTEXT** (the filled `.env`, the
   > password hash). After a successful install, either **wipe it properly**
   > (`diskpart clean all` / `dd if=/dev/zero` — a quick format does NOT scrub
   > data) or lock it away as the reimage medium — a reimage re-seeds `.env`
   > from this stick, so keeping one is useful, but treat it like the secrets
   > it holds. The reimage-ladder Option D ("USB left in the box") inherits
   > this trade-off. On-box protection today is SSH-key-only + LAN-only
   > management; the DISK IS NOT ENCRYPTED — see OI-10 (docs/status.md) for
   > the LUKS+TPM decision.

4. **Boot the AWOW from USB #1.** It partitions, installs Ubuntu + Docker
   unattended, copies the repo, seeds `.env`, enables `awow-firstboot.service`,
   and reboots.
5. **First real boot** runs `firstboot.sh`: materialize the oauth2-proxy
   allow-list, `docker compose up -d`, wait for Technitium, run
   `provision-technitium.sh`, and point the host resolver at local DNS.

### Manual bring-up (a box that already runs Docker)

```sh
docker build -t naglight:local ../../NagLight
cp .env.example .env && $EDITOR .env
docker compose up -d
bash provision/provision-technitium.sh --env .env
bash provision/healthcheck.sh --env .env
```

---

## 4. What happens automatically (Technitium provisioning)

`provision-technitium.sh` drives the Technitium HTTP API and is **idempotent**:

1. **Auth + token.** Logs in with `TECHNITIUM_ADMIN_PASSWORD`, mints a
   non-expiring API token → `provision/.token` (gitignored).
2. **Local zone + split-horizon records** for `$DOMAIN`: `tracker`, `actual`,
   `dns`, apex, and (if `MAIN_BOX_IP` set) `map` → `$LAN_IP`. Delete-then-add, so
   re-runs don't duplicate.
3. **Forwarders** (Quad9 DoH by default).
4. **Blocklists** enabled + refreshed.

> **Point the LAN's DHCP DNS server at this box** so devices use Technitium —
> otherwise split-horizon never engages.

---

## 5. Verify from another machine

```sh
dig @<AWOW_LAN_IP> tracker.<domain> +short          # expect the AWOW's LAN IP
curl -s "http://<AWOW_LAN_IP>:5380/api/dashboard/stats/get?token=<TOKEN>" | head
bash provision/healthcheck.sh --env .env            # all-in-one from the box
```

---

## 6. Burn-in checklist ("prove the AWOW is stable")

"Unused ≠ reliable." Before depending on this box, burn it in **under load** for
a day or two, then pick the secondary DNS. Track:

- [ ] **48h uptime** with the full stack up — `uptime`, `docker compose ps` all
      `healthy`, no container restarts (`docker inspect -f '{{.RestartCount}}'`).
- [ ] **DNS under load** — hammer the resolver (`dnsperf`/a loop of `dig`) while
      watching CPU/temp; verify no SERVFAILs and split-horizon records stay
      correct. J4125 idles ~8 W; confirm it stays cool under sustained queries.
- [ ] **Thermals / throttling** — `sensors` or `/sys/class/thermal` over the
      burn-in; the fanless-ish AK41 must not thermal-throttle serving DNS+proxy.
- [ ] **Storage health** — it's eMMC/SSD; check `smartctl`/`dmesg` for I/O errors.
- [ ] **Reboot resilience** — power-cycle; confirm all services return
      automatically (restart:unless-stopped) and DNS resolves within ~1 min.
- [ ] **TLS on LAN** — from a LAN device using the box as DNS, open
      `https://tracker.<domain>` with **no cert warning**.
- [ ] **OAuth round-trip** — sign in at `https://tracker.<domain>` with an
      allowed Google account (success) and a non-allowed one (rejected).
- [ ] **Then pick the secondary/failover DNS** so an AWOW outage degrades
      gracefully instead of killing LAN name resolution.

---

## 7. Rollback

- **Config only:** edit `.env`, then `docker compose up -d` (or re-run
  `firstboot.sh`) — provisioning is idempotent, so it re-converges DNS.
- **A bad image tag:** pin the previous `*_IMAGE_TAG` in `.env` and
  `docker compose up -d <service>`.
- **Whole box:** the image pipeline is the recovery path — **re-flash the USB**
  and boot; data lives in named Docker volumes and, for the tracker, in the
  **remote data repo** (`TRACKER_DATA_REMOTE`). Back up the Docker volumes
  (`technitium_config`, `actual_data`, `caddy_data`) — the backup service does
  this once the `volume:` lines in `backup.env` are uncommented (SR-013,
  [backup/README.md](backup/README.md)). The
  LAN reimage ladder (do it without visiting the box) is in
  [../REMOTE_MANAGEMENT.md](../REMOTE_MANAGEMENT.md) (WI-10.12).
- **Fall back:** keep any previous DNS/proxy setup untouched until this box
  passes burn-in.

---

## 8. Auxiliary observability (LAN-only, WI-10.11)

Three optional management UIs, each **published bound to `LAN_IP` only** — they
are reachable from the LAN but are **not** proxied through Caddy and **not**
forwarded by the router. Do not add public Caddy sites for them.

| Service | Default URL | Purpose |
|---|---|---|
| Uptime-Kuma | `http://<LAN_IP>:3001` | generic up-checks (can POST to NagLight `/api/feed`) |
| Dozzle | `http://<LAN_IP>:8081` | live container log viewer (docker socket, read-only) |
| ntfy | `http://<LAN_IP>:8090` | **optional** self-hosted push — opt-in |

ntfy is behind a compose **profile**, so it starts only when asked:

```sh
docker compose --profile ntfy up -d
```

All ports are configurable in `.env` (`UPTIMEKUMA_PORT`, `DOZZLE_PORT`,
`NTFY_PORT`); each has a healthcheck.

---

## 9. Opt-in tier-2 catalog (SR-012)

A second tier of self-hosted services rides in the same compose file behind
**profiles** (the ntfy pattern) — **everything OFF by default**. The core stack,
its validation, and the baked ISO payload are untouched until a profile is
explicitly enabled.

| Profile | Service | Access (default) | Purpose |
|---|---|---|---|
| `immich` (+`immich-ml`) | Immich (+DB+Redis) | `http://<LAN_IP>:2283` | photo backup, Google-Photos-style; ML is a separate heavy profile — leave off on the J4125 |
| `photoprism` | PhotoPrism (+MariaDB) | `http://<LAN_IP>:2342` | photo library indexed in place (run at most ONE photo stack) |
| `jellyfin` | Jellyfin | `http://<LAN_IP>:8096` | movies/TV; QSV transcode via `/dev/dri` |
| `navidrome` | Navidrome | `http://<LAN_IP>:4533` | music, Subsonic API |
| `audiobookshelf` | Audiobookshelf | `http://<LAN_IP>:13378` | podcasts + audiobooks |
| `vaultwarden` | Vaultwarden | **Caddy site only** (HTTPS required) | Bitwarden-compatible passwords |
| `homeassistant` | Home Assistant | `http://<LAN_IP>:8123` (host net) | home automation (container flavor — no add-on store) |
| `mosquitto` | Mosquitto | `<LAN_IP>:1883` | MQTT bus for HA; config in `mosquitto/mosquitto.conf` |
| `syncthing` | Syncthing | `http://<LAN_IP>:8384` | p2p file sync — feeds `MEDIA_ROOT` from phones/PCs |
| `freshrss` / `mealie` / `homepage` | FreshRSS / Mealie / Homepage | LAN ports in `.env` | RSS · recipes · LAN dashboard |
| `diun` | diun | logs / ntfy topic | update **notifier** for the pinned images (never auto-updates) |

### Where everything is configured (the pre-build chain)

All configuration happens in **this directory before the USB/ISO is built**, in
four places — then the build scripts carry it onto the box:

1. **`stack/.env`** (copy of `.env.example`) — THE knob file: secrets, ports,
   pins, `MEDIA_ROOT`, and the enable switch **`COMPOSE_PROFILES`**. Compose
   reads `COMPOSE_PROFILES` from `.env`, so firstboot's plain
   `docker compose up -d` brings enabled profiles up at first boot.
2. **`stack/caddy/Caddyfile`** — uncomment a tier-2 site block to publish that
   service (each carries its own login; no basic_auth on them).
3. **`stack/.env` → `EXTRA_SUBDOMAINS`** — one label per uncommented site;
   provisioning adds the split-horizon A records.
4. **`vmtest/export-images.sh`** — bakes core+ntfy images only. Tier-2 images
   are **not baked** (pins are best-effort, not sim-validated); they pull at
   enable time. To bake an enabled set into the ISO anyway:
   `EXTRA_PROFILES="navidrome vaultwarden" bash vmtest/export-images.sh`.

Then build as in §3: the USB payload carries your filled `.env` (autoinstall
seeds from `.env.example` **only if you didn't pre-fill one**), the stack lands
in `/opt/awow-core/stack`, and first boot brings up core + enabled profiles.

### Enabling a service on a RUNNING box (no reflash)

```sh
ssh operator@<LAN_IP>
cd /opt/awow-core/stack
$EDITOR .env                       # add the profile to COMPOSE_PROFILES (+ its REPLACE_WITH knobs)
docker compose up -d               # pulls the tier-2 image(s), starts them
```

`restart: unless-stopped` keeps them running across reboots from then on.
Disable = remove the profile from `COMPOSE_PROFILES`, then
`docker compose up -d --remove-orphans`.

### Ground rules / caveats (read before enabling)

- **RAM budget (8 GB):** core ≈ 1.5 GB. HA + Jellyfin + the small services fit;
  run at most ONE photo stack; `immich-ml` is the heavy piece — leave it off or
  give it a `mem_limit`.
- **Media storage:** `MEDIA_ROOT` must point at real always-on storage — NOT
  the WI-10.10 backup drives (streaming would defeat their spin-down policy).
- **Tier-2 pins are NOT sim-validated** and carry no custom healthchecks yet
  (probe tooling per image is unverified — the WI-10.14 lesson). Verify the
  tag + behavior at enable time; the `diun` profile watches for updates after.
- **Back up what you enable:** stack volumes join the backup by adding
  `name=volume:VOL[@container]` lines to the `BACKUP_SOURCES` table
  (SR-013 — see [backup/README.md](backup/README.md) "Docker-volume sources");
  commented starter lines ship in `backup.env.example`.

## Local validation status (honest)

- **`docker compose config` / live bring-up:** **PENDING — no Docker on the
  build machine** (Windows dev box; `docker`/`docker compose` not installed,
  verified). The runtime path (compose up, curl the health endpoints, `dig`,
  tear down) **could not be exercised here** and must be run on a Docker host /
  the AWOW itself.
- **What WAS validated locally:** all shell scripts pass `bash -n`; `meta-data`
  and `docker-compose.yml` parse; `user-data` is valid `#cloud-config` YAML;
  every `${VAR}` in compose has a matching key in `.env.example`
  (`scripts/validate_config.py`); every `{$VAR}` in the Caddyfile is passed by
  the caddy service's environment; all files referenced by compose/autoinstall
  exist. See `../docs/status.md` for the full validation ledger.
