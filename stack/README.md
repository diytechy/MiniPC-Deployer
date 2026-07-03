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
- **Uptime-Kuma · Dozzle · (ntfy)** — auxiliary LAN-only observability
  (WI-10.11); see §8.

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
```

---

## 1. Build the NagLight image (local, Q10.2)

There is no registry yet, so every host builds the tracker image locally from a
sibling NagLight checkout:

```sh
docker build -t naglight:local ../../NagLight   # adjust path to your checkout
```

(or uncomment the `build:` fallback in `docker-compose.yml`).

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
     whole repo to `deploy-payload/` on that stick (the autoinstall
     `late-commands` copy `deploy-payload/` into `/opt/awow-core`, so the stack
     lands at `/opt/awow-core/stack`).
   - **One USB (remaster):** unpack the ISO, add `/nocloud/` with
     `user-data` + `meta-data`, add kernel arg
     `autoinstall ds=nocloud;s=/cdrom/nocloud/`, repack with `xorriso`.

   > Before writing, **replace the placeholders** in `user-data`: the SHA-512
   > password hash and, ideally, an SSH key (then set `allow-pw: false`). Do
   > **not** commit a filled-in `user-data`.

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
  (`technitium_config`, `actual_data`, `caddy_data`) for faster recovery. The
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
