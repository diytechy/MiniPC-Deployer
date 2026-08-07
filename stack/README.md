# AWOW AK41 always-on core — deploy stack

Zero-manual-config, always-on box image for the **AWOW AK41** (Celeron J4125,
8 GB, x86). Flash a USB → boot the hub → the whole stack comes up on its own.
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
  default; see §9. **What `COMPOSE_PROFILES` enables IS baked** into the ISO
  payload (corrected 2026-08-07) — the boundary is the profile switch, not the
  bake.

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
    homehub-firstboot.service    systemd oneshot that runs the bring-up once
    firstboot.sh              compose up + provisioning + point host resolver at local DNS
    powertune.{service,sh}    per-boot low-power auto-tune (powertop) + USB-storage guard
    wall/                     IMAGE TARGET 2 — the office wall panel (§10)
      user-data / meta-data     graphical autoinstall: cage kiosk, Wi-Fi, no Docker
      wall.env.example          the panel's knobs (seeded to /etc/wall-panel/wall.env)
      wall-firstboot.{service,sh}  hardware-quirk fixes, netplan, sleep timers, autologin
      wall-wakeprep.{service,sh}   per-boot ACPI/USB wake enablement (does not persist)
      wall-sleep.sh + wall-{sleep,wake}.service   the D-W4 nightly window
      wall-kiosk.sh             cage + the shell app, with a visible failure screen
      netplan-wifi.yaml.template   rendered to /etc/netplan/60-wall-wifi.yaml (0600)
      WALL-BURN-IN.md           what only the real panel can settle — read before drilling
  wall-shell/                 document root the kiosk site serves (the shell build; IF-005)
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

### Image delivery to the hub — Q10.9 B+ (ALL-IMAGES, baked into the ISO)

The hub does **not** pull any image from a registry at first boot. Per the Owner's
locked **Q10.9 B+** decision, EVERY stack image — the locally-built
`naglight:local` **and** every public image (technitium, caddy, oauth2-proxy,
actual, ddns, uptime-kuma, dozzle, ntfy) — is `docker save`d into the ISO deploy
payload and `docker load`ed at first boot. So a freshly-imaged box comes up "from
infancy": **zero registry/internet dependency for container images**, versions
pinned (in `.env.example`, see the "Image tags" block) to exactly what the
homehub-sim validated. What boots == what was validated; no drift from moving
`latest` tags.

Mechanism (see `../vmtest/`):

```
vmtest/export-images.sh   ->  docker save each pinned image -> deploy-payload/images/*.tar
autoinstall late-commands ->  copy deploy-payload/ -> /opt/homehub/ (images and all)
autoinstall/firstboot.sh  ->  docker load /opt/homehub/images/*.tar  BEFORE  compose up
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
| `LAN_IP` | the hub's LAN IP — **give it a DHCP reservation** at this address |
| `ACME_EMAIL` | email for Let's Encrypt |
| `ACTUAL_BASICAUTH_HASH` / `DNS_BASICAUTH_HASH` | `docker run --rm caddy:2-alpine caddy hash-password --plaintext 'yourpass'` |
| `TECHNITIUM_ADMIN_PASSWORD` | strong password (set on Technitium's first start) |
| `OAUTH2_PROXY_CLIENT_ID` / `_SECRET` | from your Google OAuth client (**Owner manual step**, below) |
| `OAUTH2_PROXY_COOKIE_SECRET` | `openssl rand -base64 32 \| tr -- '+/' '-_'` |
| `OAUTH2_PROXY_ALLOWED_EMAILS` | comma-separated Google accounts permitted into the tracker |
| `TRACKER_DATA_REMOTE` | git remote of your private **data** repo (single-user); blank for multi-user |
| `CLOUDFLARE_ZONE_ID` / `CLOUDFLARE_API_TOKEN` | dynamic DNS (`ddns` service) — a **new** scoped token (Zone→DNS→Edit, this zone only); never reuse the old plaintext token that sat on the legacy Windows box's setup share |
| `BACKUP_DRIVE_DEVICES` / `BACKUP_DRIVE_STANDBY` | backup-drive spin-down (WI-10.10) — space-separated `/dev/disk/by-id/...` paths (never `sdX`, it renumbers); empty = no-op |

> **This table lists the knobs that need YOUR values filled in.** For the full
> set including ones with sensible defaults, read `.env.example` top to bottom
> — it's the source of truth and every entry is commented. Mind its **QUOTING
> RULE** header (values with spaces must be double-quoted — the file is both a
> compose env-file and shell-sourced by firstboot/provisioning). Tier-2 opt-in
> knobs (`COMPOSE_PROFILES`, `MEDIA_ROOT`, `EXTRA_SUBDOMAINS`, per-service
> pins/ports/passwords) live in the same file — see §9.

**Filling these in today is a manual step.** The automated secret-handoff
design in the sibling `Personal` repo's `SECRET_HANDOFF.md` was **ratified
2026-07-25** but is **not built yet**, so hand-fill `.env` until it exists.
Once built it extends the Owner's DPAPI credential pattern to materialize this
file; path-shaped values (`MEDIA_ROOT`, the backup source/target table) come
from `Personal\deploy\storage-map.md`, which is the pathing SSOT.

### OWNER MANUAL STEP — Google OAuth client (required for the tracker)

The tracker subdomain is gated by Google sign-in. Before first bring-up, create
the OAuth client (needs the Owner's Google account — an agent cannot):

1. console.cloud.google.com → **APIs & Services → Credentials**.
2. Configure the **OAuth consent screen** (External). While unverified, add each
   allowed Google account as a **Test user** (or publish the app). Scopes:
   `openid`, `email`, `profile` (the slacker-tracker pattern the Owner trusts uses
   `email profile`).
3. **Create Credentials → OAuth client ID → Web application**.
4. Register **Authorized redirect URI** exactly:
   `https://tracker.<your-domain>/oauth2/callback`
   (e.g. `https://tracker.example.tld/oauth2/callback`). No JavaScript origin
   needed — this is a server-side flow.
5. Paste the Client ID + secret into `.env`.

### OWNER MANUAL STEP — bank sync in Actual (SimpleFIN, one-time, in-app)

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

The hub installs **Ubuntu Server 24.04 LTS** unattended, then first-boot brings
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
     `/opt/homehub`, so the stack lands at `/opt/homehub/stack` and the
     images at `/opt/homehub/images` where first-boot `docker load`s them.
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

4. **Boot the hub from USB #1.** It partitions, installs Ubuntu + Docker
   unattended, copies the repo, seeds `.env`, enables `homehub-firstboot.service`,
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
dig @<HOMEHUB_LAN_IP> tracker.<domain> +short          # expect the hub's LAN IP
curl -s "http://<HOMEHUB_LAN_IP>:5380/api/dashboard/stats/get?token=<TOKEN>" | head
bash provision/healthcheck.sh --env .env            # all-in-one from the box
```

---

## 6. Burn-in checklist ("prove the hub is stable")

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
- [ ] **Then pick the secondary/failover DNS** so a hub outage degrades
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
`NTFY_PORT`); each has a healthcheck that has been **run against its pinned
image** (2026-08-01). Do not "simplify" the Uptime-Kuma or Dozzle probes back
into a `wget`/`CMD-SHELL` one-liner: kuma ships no `wget` and Dozzle is
distroless (no shell at all), which is exactly why both sat permanently red
until 2026-08-01. Each now runs the probe binary its own image provides — see
`docs/status.md`, entry "2026-08-01". The WI-10.14 lesson generalises: check
what a probe tool actually exists in the image before writing a healthcheck.

---

## 9. Opt-in tier-2 catalog (SR-012)

A second tier of self-hosted services rides in the same compose file behind
**profiles** (the ntfy pattern) — **everything OFF by default**. The core stack,
its validation, and the baked ISO payload are untouched until a profile is
explicitly enabled.

| Profile | Service | Access (default) | Purpose |
|---|---|---|---|
| `immich` (+`immich-ml`) | Immich (+DB+Redis) | `http://<LAN_IP>:2283` | photo backup, Google-Photos-style; ML is a separate heavy profile — leave off on the J4125, or cap it with `IMMICH_ML_MEM_LIMIT` |
| `photoprism` | PhotoPrism (+MariaDB) | `http://<LAN_IP>:2342` | photo library indexed in place (run at most ONE photo stack) |
| `jellyfin` | Jellyfin | `http://<LAN_IP>:8096` | movies/TV; library path via `JELLYFIN_LIBRARY_DIR`; QSV `/dev/dri` wired up by firstboot when an iGPU is present |
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
4. **`vmtest/export-images.sh`** — bakes core+ntfy **plus every profile
   `COMPOSE_PROFILES` names in the `.env` being bundled**. Nothing extra is
   needed: enable a profile and its image is baked on the next build, or the
   build is refused because the tag cannot be resolved. `EXTRA_PROFILES="…"`
   still adds profiles the `.env` does not enable.

   > **Corrected 2026-08-07, and it mattered.** This used to bake core+ntfy and
   > nothing else, whatever the `.env` said. A hub configured with
   > `COMPOSE_PROFILES=ntfy,immich,immich-ml,jellyfin,finance-auditor` therefore
   > shipped with 9 images and needed 15, so first boot went to
   > `registry-1.docker.io` for the rest — the offline-install promise broken one
   > step past the install. Worse, `docker compose up -d` is all-or-nothing: the
   > failed pull of one **optional** image took the whole command down,
   > `homehub-firstboot.service` exited 1, and DNS, Caddy, the tracker and Actual
   > never started. Tier-2 pins are still best-effort rather than sim-validated,
   > so verify a tag when you enable its profile — but an unresolvable one is now
   > a refused build, where someone can act on it.

Then build as in §3: the USB payload carries your filled `.env` (autoinstall
seeds from `.env.example` **only if you didn't pre-fill one**), the stack lands
in `/opt/homehub/stack`, and first boot brings up core + enabled profiles.

### Enabling a service on a RUNNING box (no reflash)

```sh
ssh hub@<LAN_IP>
cd /opt/homehub/stack
$EDITOR .env                       # add the profile to COMPOSE_PROFILES (+ its REPLACE_WITH knobs)
docker compose up -d               # pulls the tier-2 image(s), starts them
```

`restart: unless-stopped` keeps them running across reboots from then on.
Disable = remove the profile from `COMPOSE_PROFILES`, then
`docker compose up -d --remove-orphans`.

### Ground rules / caveats (read before enabling)

- **RAM budget (8 GB):** core ≈ 1.5 GB. HA + Jellyfin + the small services fit;
  run at most ONE photo stack; `immich-ml` is the heavy piece — leave it off, or
  set `IMMICH_ML_MEM_LIMIT` (e.g. `2g`) so an OOM kills only that container and
  the core stack stays up. `immich` + `immich-ml` + `jellyfin` together land
  around 4–5 GB on top of the core.
- **Media storage:** `MEDIA_ROOT` must point at real always-on storage — NOT
  the WI-10.10 backup drives (streaming would defeat their spin-down policy).
- **A `MEDIA_ROOT` on a mounted drive must be mounted BEFORE compose starts.**
  Docker creates a missing bind-mount source itself, on whatever filesystem is
  there at container-start time — so a late mount shadows the directory the
  container is already writing to, and the data lands on the system disk where
  nothing can see it. `firstboot.sh` step 3b mounts the storage-map drives ahead
  of `docker compose up -d` for exactly this reason; keep that order.
- **Subdirectory casing is exact.** The documented subdirs are lowercase
  (`music/`, `photos/`, …) and both ntfs3 and ext4 are case-sensitive: pointing
  at a library whose folders are `Music`/`Movies` with a lowercase bind path
  gets you a silently-created empty directory, not an error.
- **Hardware passthrough is not in `docker-compose.yml`.** Jellyfin's Quick Sync
  needs `/dev/dri`, and a `devices:` entry for an absent node fails container
  creation — which fails the entire `docker compose up -d`, not just that
  service. `provision-compose-overrides.sh` (firstboot step 3c) generates
  `docker-compose.override.yml` with the device on boxes that have an iGPU and
  removes it on boxes that do not. Compose auto-loads that file, so a later
  manual `docker compose up -d` over SSH keeps the same behaviour.
- **`JELLYFIN_LIBRARY_DIR`** narrows what Jellyfin sees to a subtree of the media
  root (empty = the whole root). Inside the container the path is always
  `/media` — that is what you type when adding the library in Jellyfin's UI.
- **Tier-2 pins are NOT sim-validated** and carry no custom healthchecks yet
  (probe tooling per image is unverified — the WI-10.14 lesson). Verify the
  tag + behavior at enable time; the `diun` profile watches for updates after.
- **Back up what you enable:** stack volumes join the backup by adding
  `name=volume:VOL[@container]` lines to the `BACKUP_SOURCES` table
  (SR-013 — see [backup/README.md](backup/README.md) "Docker-volume sources");
  commented starter lines ship in `backup.env.example`.

## 10. The office wall panel — image target 2 (SR-016/SR-017, OI-12)

Ratified by the Owner on **2026-07-29** (the belt-and-braces "LAN port + `/32`"
variant). Two pieces land on the hub side, and a whole second image lands in
[`autoinstall/wall/`](autoinstall/wall/README.md) — read that README for the
panel itself and `WALL-BURN-IN.md` before mounting anything.

### What the hub box gains

A Caddy site, `{$WALL_HOST}:{$WALL_PORT}`, that gives a keyboard-less panel the
Owner's NagLight view **with no sign-in step**. It deliberately bypasses
oauth2-proxy — a wall panel cannot complete an interactive Google consent, so
every session expiry would otherwise leave a login screen on the office wall.

That makes it the **second** injector of the tracker's trusted identity headers,
which is why it needed ratification rather than a commit. Four guards, all
load-bearing:

| Guard | Where | If you remove it |
|---|---|---|
| The injected `X-Forwarded-User` **replaces** any client-supplied one | Caddyfile `header_up` | anyone who can reach the site is anyone they like |
| `remote_ip {$PANEL_IP}/32` — one address, not the LAN CIDR | Caddyfile matcher | guest Wi-Fi, IoT gear and an inbound-facing Minecraft server all become the Owner |
| The port is published bound to `{$LAN_IP}` **and the router never forwards it** | `docker-compose.yml` `ports:` + your router | the site is on the internet |
| `respond 403` default | Caddyfile | a miss becomes a fall-through instead of a refusal |

**Do NOT add a router forward for `WALL_PORT`.** The hostname resolves publicly
the day it exists (the DDNS updater maintains a wildcard record), so structural
unreachability is doing real work here.

Enabling it: fill `WALL_HOST`, `WALL_PORT`, `PANEL_IP`, `PANEL_USER_SUB` in
`.env`, add the host's bare label to `EXTRA_SUBDOMAINS` (so provisioning creates
the split-horizon record and the panel reaches the box directly rather than
hairpinning), give the panel a **DHCP reservation on its hardware MAC**, and
`docker compose up -d`.

### One gotcha worth knowing before you edit that block

Caddy applies request-header operations in a **fixed order — add, set, delete,
replace — regardless of the order you write them**. So the intuitive
"strip then inject" pair (`header_up -X-Forwarded-User` followed by a set of the
same field) deletes the value it just injected: the tracker sees no identity and
every panel request 403s. This was found for real in the V1 sim on the first run,
and the Caddyfile carries a DO-NOT-ADD banner so it does not come back. The strip
is not lost — `header_up <Name> <value>` is a *set*, which replaces every existing
value of that field — and the sim asserts exactly that with a forged header rather
than trusting it.

### The certificate for `{$WALL_HOST}` — verified, not assumed

**It issues normally, over the existing `:80`/`:443` listeners. The alternate port
is never involved in validation.** Checked against Caddy's docs and source
(2026-07-29) rather than hoped for:

- **Automatic HTTPS activates on the hostname, not the port.** The documented list
  of things that disable it does not include a non-standard port — only an
  `http://` prefix, listening exclusively on the HTTP port, no hostnames at all,
  manually-loaded certs, or an explicit opt-out. So Caddy manages a
  publicly-trusted cert for `{$WALL_HOST}` even though the site is served on
  `{$WALL_PORT}`.
- **ACME CAs never contact non-standard ports.** HTTP-01 is *always* validated on
  80 and TLS-ALPN-01 *always* on 443. There is therefore nothing to forward for
  `{$WALL_PORT}`, and no way for the challenge to arrive there.
- **The existing port-80 listener answers for this name even though no site block
  serves it on 80.** Caddy's ACME challenge handler runs in every HTTP server
  ahead of route matching and dispatches on the requested hostname
  process-wide — not per site block, not per port. *Honesty note: this last point
  is clear in Caddy's source but is **not stated in the documentation**, so treat
  it as verified-by-code, and confirm the cert actually issued on the real box
  (`WALL-BURN-IN.md` §7) rather than assuming.*
- **The dependency this creates:** inbound TCP/80 must stay forwarded to Caddy. If
  port 80 ever closes, renewal for this name breaks — and the symptom will look
  like a wall-panel problem. TLS-ALPN-01 on 443 is a weak backstop (it needs a
  connection policy matching this SNI). If you ever want the panel independent of
  WAN-reachable 80/443, the clean answer is a DNS-01 challenge, which needs no
  open ports at all — you already run a Cloudflare API token for DDNS.
- **One cosmetic consequence:** automatic HTTPS also creates a port-80 route for
  this name that 301s to `https://{$WALL_HOST}:{$WALL_PORT}`. From the LAN that is
  correct; from the WAN it is a redirect to a port nobody can reach, which
  advertises the port number without exposing it. Harmless, and left alone rather
  than "fixed" with an extra site block on a security-critical host.

## Local validation status (honest)

- **`docker compose config` / live bring-up:** the old "no Docker on the build
  machine" constraint was **LIFTED 2026-07-03 (WI-10.13)** — the dev PC runs
  WSL2 + Ubuntu + docker-ce (Docker Desktop deliberately not installed).
  `docker compose config` resolves the full stack (core + every tier-2 profile)
  and the whole stack **runs GREEN in the V1 sim** (`sim/run-sim.sh` +
  `sim/validate-sim.sh`) against fictional stand-ins — Dex for Google, an
  internal CA for ACME, Samba fixtures for the LAN shares. What the sim still
  **cannot** prove is the hardware-and-real-world layer: real Google consent,
  publicly-trusted ACME certs, Technitium on the host's real `:53`, drive
  spin-down physics, thermals. Those wait for the V3 VM boot and the box itself.
- **The wall panel (§10):** the kiosk site is **GREEN in the V1 sim** — the
  forged-header identity swap and the 403-for-everyone-else default are both
  asserted end to end (`validate-sim.sh` checks 7-8). The **image** has only
  been exercised as config: a throwaway container run of `wall-firstboot.sh`
  against stub `systemctl`/`udevadm`/`netplan` proved what it writes. Everything
  physical — the graphical session, Wi-Fi association, suspend/resume, every
  hardware quirk's real effect, and the **off-LAN 403** — is a hardware
  remainder, enumerated in
  [`autoinstall/wall/WALL-BURN-IN.md`](autoinstall/wall/WALL-BURN-IN.md).
- **What is validated statically:** all shell scripts pass `bash -n`; `meta-data`
  and `docker-compose.yml` parse; both `user-data` files (core + wall) are valid
  `#cloud-config` YAML; every wall knob a panel script reads is declared in
  `wall.env.example`;
  every `${VAR}` in compose has a matching key in `.env.example`
  (`scripts/validate_config.py`); every `{$VAR}` in the Caddyfile is passed by
  the caddy service's environment; all files referenced by compose/autoinstall
  exist. See `../docs/status.md` for the full validation ledger.
