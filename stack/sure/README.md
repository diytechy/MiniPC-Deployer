# Sure preparation (SR-060..SR-065)

The `sure` profile defines web, Sidekiq, Postgres and Redis beside Actual.
Owner direction 2026-10-09 keeps Sure ON by default, and **since 2026-10-22 the
switch is `SURE_ENABLED`, not `COMPOSE_PROFILES`** (SR-100):

- `homehub-sure.service` (this directory, with `sure-unit.sh`) is the only thing
  that passes `--profile sure`. firstboot installs it on every boot, enables it
  and queues its start when `SURE_ENABLED=true`, and disables and stops it when
  the knob is switched off after it had been enabled.
- `sure` must never be in `COMPOSE_PROFILES`: firstboot's every-boot bulk
  `docker compose up -d` is all-or-nothing and would wait on Sure's Postgres and
  Redis before DNS provisioning, Samba and recovery (the 2026-10-18 Sure deploy
  review). `vmtest/export-images.sh` and `scripts/ensure-local-images.sh` refuse
  it and read `SURE_ENABLED` instead; firstboot warns.
- The four services keep `restart: unless-stopped`, so a crash or a
  docker.service restart brings them back without the unit; the unit's stop
  marks them stopped, so after a reboot it is the unit that starts them, and a
  disabled unit keeps them off.
- An `.env` with no `SURE_ENABLED` line (the live hub on 2026-10-22) leaves the
  unit disabled and touches no running Sure container.

Turn it off: set `SURE_ENABLED=false` in `.env`, then `systemctl disable --now
homehub-sure`. Sure owns household logins.
Caddy accepts `LAN_CIDR` only, proxies to `sure-web:3000`, and injects no hub
identity. Technitium provisions `SURE_SUBDOMAIN`. No service publishes a port.

Web and worker share `/rails/storage`. Database/cache are on internal network
`sure`; Rails also joins `default` for bank/price-provider egress. Finance-Auditor
joins both, retains `DATA_SOURCE=actual` and has no Sure startup dependency.
The future read-only API URL is `http://sure-web:3000`.
`FINANCE_TRACKER_FEED_USER` is a separate non-secret Google sub. The header fix
is dispatched in Finance-Auditor `docs/status.md`, "Dispatched from HomeHub";
it is not merged by this preparation. The Actual service is preserved; new FA
env wiring has the merge-order dependency below.

**Merge-order dependency:** this Compose wiring must not reach the hub until
Finance-Auditor's empty-as-absent fix is included in the FA image the hub builds.
Compose exports blank optional `TRACKER_FEED_USER` and `SURE_API_KEY`; FA's
current bridge rejects the former even on the Actual path. Its fix must cover
`TRACKER_FEED_USER`, `SURE_API_KEY`, `SURE_URL` and `DATA_SOURCE`. Static tests
prove mappings and defaults only, not that the current FA accepts them.

## What changes with Sure off (`SURE_ENABLED` not `true`)

Sure containers stay off. The Caddy site and Technitium DNS provisioning remain
active, following the repo's current household-site convention (Caddyfile's
"EACH SITE IS LIVE FROM DAY ONE"). There is no automatic profile-to-site gate;
older commented tier-2 stubs are explicitly an obsolete public shape.
`SURE_HOST` and `APP_DOMAIN` default to `sure.<domain>` when the subdomain knob is
absent. Caddy will request its real certificate even while Sure is off, answer
LAN requests with 502, and deny other sources with 403. Enabling the profile
does not decide outside-LAN access.

**Owner enablement item:** Sure defaults to open LAN registration. The default
is env-settable (`ONBOARDING_STATE` in `app/models/setting.rb:201-207`), but an
existing saved setting takes precedence (`:226-236`). We leave it unset for
first-account bootstrap. Create the first household account, then set hosting
onboarding to **invite-only before allowing other LAN clients to register**.
Leave the invite-only default-family ID blank so invite codes are required;
an existing family invitation token is also accepted. Evidence:
`app/controllers/concerns/invitable.rb:9-17`,
`app/controllers/registrations_controller.rb:61-94,123-127`.
The LAN gate does not itself restrict household account creation.

## Registry evidence and payload

**The Sure image is the Owner's fork, built locally (2026-10-10).** The fork must carry BOTH
`auroleap-bank-stale-capability` and `auroleap-transaction-flags`, so it is built from
the integration branch named by `SURE_SOURCE_REF` (`auroleap-integration`, created in
`../sure` by the coordinator, not by this repo); the exporter refuses an image whose
`homehub.source.ref` label differs. `SURE_IMAGE`
(default `sure-auroleap:local`) replaces the stock `ghcr.io/we-promise/sure`
reference and the retired `SURE_IMAGE_TAG`. It has no registry home, so it travels
exactly like `naglight:local`: `scripts/build-sure-image.sh` builds a NAMED branch
or commit of `../sure` (via `git archive`, so uncommitted edits cannot leak in) and
stamps `homehub.source.revision`, `homehub.source.ref` and `BUILD_COMMIT_SHA`;
`vmtest/export-images.sh` treats it as local-only (never pulled, re-saved every run,
freshness-checked against the ref it was built from); `firstboot.sh` verifies the
loaded image's rootfs hash (sha256 of the diff-id list; `.Id` differs between Docker's classic and
containerd stores so it is display-only) equals the one the manifest recorded. On a
mismatch it removes the tag (so the next boot retries the load rather than skipping),
writes `/var/lib/homehub/image-load.state` (reported FAIL by `healthcheck.sh`) and
lets the boot continue; the Sure services carry `pull_policy: never`, so the service
fails to start instead of pulling from a registry. `docker compose up -d` is
all-or-nothing about its exit status; when `image-load.state` lists failures firstboot
logs an ERROR naming them, defers compose's exit code, finishes provisioning (DNS,
Caddy, Samba, the recovery supervisor) and exits with that code at step 7, so the unit
still ends red. A compose failure with no image failure stays an immediate FATAL. Build it in WSL with real Docker, never the default (Podman) distro:

    wsl -d Ubuntu -- bash /mnt/c/Projects/MiniPC-Deployer/scripts/build-sure-image.sh --ref auroleap-integration

**Postgres and Redis keep their digest pin, enforced at bake time.** The knobs are
now a plain tag (`SURE_POSTGRES_IMAGE_TAG=16.15`, `SURE_REDIS_IMAGE_TAG=8.10.2`,
what compose references) plus `SURE_*_IMAGE_DIGEST=sha256:<index>`. The exporter
runs `docker pull repo@digest` (the daemon verifies content against the digest),
tags the result `repo:tag`, saves that, and records kind `pinned` and the image id
in `images.manifest.tsv`. Reason: `docker save` carries RepoTags only; RepoDigests
are pull metadata that `docker load` does not restore, so a `tag@sha256:` reference
need not resolve on the hub - compose would try to pull (fails offline) and
firstboot's already-present check would never match, reloading every boot. Any
`@` reference left in the resolved image set is a refused bake. Trade-off: a hub
with NO baked payload (online fallback) pulls the plain tag, whose digest is not
re-checked on the box; the digest is only guaranteed for the baked path.

The table below is the 2026-10-09 registry evidence for the digest knobs. The first
row (stock Sure) is historical: that image is no longer deployed.

| Tag | Index SHA-256 | Linux amd64 manifest SHA-256 |
|---|---|---|
| `ghcr.io/we-promise/sure:0.7.5-hotfix.2` | `aa46be2d8e3f8b2182261dc05933a64b4a12543dd5f44ad12f08868caac1f997` | `cd6706f544cd5f9b299a586d719776cf2595b483ea1f1a396137da0d23eafb20` |
| `postgres:16.15` | `ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d` | `75adc2a65806c96bc0e59030230bfe3f1c1b0d6d8f8181a8913e2b8b53bdc166` |
| `redis:8.10.2` | `2c2dff791878316e3b083188be0d578423b24813cdda5de0e80ea4f7b3e6bfd6` | `6e9101032a2126aeeb4b1e862ac77d2fcf26b831ae37e5ff2e69169f82ddd411` |

Upstream's example specifies Redis `latest` and Postgres `16`. Registry config
metadata identifies `8.10.2` and `16.15-1.pgdg13+2`. Independently queried explicit
tags `8.10.2` and `16.15` resolve to the same index digests above; these explicit
tags follow this repo's plain-version convention. The exporter derives its list from Compose and selected
profiles, including `EXTRA_PROFILES`; a future bake must select `sure` even if
runtime activation follows later. No duplicate image list is needed. Diun
watches Docker containers by default; postgres and redis opt in, the local fork opts
out (no registry to watch). Postgres/redis do not belong in the `ensure-local-images.sh`
resolver; the fork does, behind the `sure` profile.

**Not proven:** the lab sequence (load the Sure tar on a lab VM, `docker compose
config --images`, `docker compose up --pull never`) has not run, the hub's Docker
image store (classic vs containerd) is not recorded, and the stub-docker tests prove
the scripts' decisions, not a real daemon's behaviour.

## Reporting and AI audit

Paths below refer to the exact Sure revision above. These are source findings,
not observed traffic.

| Path | Shipped control | Source |
|---|---|---|
| PostHog bundled feedback/daily usage, **on by default in production** | `POSTHOG_FEEDBACK_ENABLED=false`; `POSTHOG_KEY` empty | `config/initializers/posthog.rb:8,12,37`; `app/controllers/concerns/daily_web_usage_tracking.rb:14,28`; `app/helpers/feedback_helper.rb:9`; `app/views/layouts/shared/_head.html.erb:36` |
| Sentry errors/logs/profiling | `SENTRY_DSN` empty | `config/initializers/sentry.rb:1` |
| Logtail log forwarding | Both credential/host keys empty | `config/environments/production.rb:52` |
| Langfuse AI traces | Both credential keys empty | `config/initializers/langfuse.rb:3`; `app/models/provider/openai.rb:753` |
| Skylight performance | `SKYLIGHT_AUTHENTICATION` empty | `config/skylight.yml:1`; `Gemfile:47-50`; `config/application.rb:42-47` |
| GitHub release notes/latest release/Markdown rendering on UI demand | No env switch; web/worker map `api.github.com` to loopback | `app/controllers/pages_controller.rb:90`; `app/controllers/release_highlights_controller.rb:7`; `app/models/provider/github.rb:18,27,66` |

The GitHub override is a judgment call to stop the update path without a fork;
"What's new" cannot fetch. It is a hostname control, not an egress firewall.
No further reporting path was found in targeted config/app/lib searches. Gem
internals and runtime traffic were not audited. Legitimate bank/price calls
retain egress.

No `OPENAI_*` is set. Before future enablement, route `OPENAI_URI_BASE` to the
**litellm finance lane, never llm-gateway**, and amend its owning repo's caller
documentation. AI rule actions stay disabled until that lane answers. Empty env
on a fresh database does not override credentials later stored through Sure UI
(`app/models/provider/registry.rb:94-122`, `app/models/setting.rb:9-17`).

## Secrets and acceptance

The existing `homehub-backup.timer` (03:00 box-local, persistent) collects
`sure-ledger=postgres:sure-postgres` through `backup.sh`. This new source type
uses pg_dump from the database container, then verifies custom-dump signature
and pg_restore table-of-contents parsing before atomic staging promotion.
It reuses archive/compression, per-file and archive hashes, MANIFEST, Fibonacci
retention and failure reporting. A plan invokes neither dump nor Docker.
HomeHub emits the Sure backup rows only when the `sure` profile is selected.
At enablement, regenerate and incorporate those active source rows into the
hand-carried backup configuration; its existing `-Verify` comparison must agree.
Profile selection alone does not update an already installed backup.env.
The existing drive/mount preflight and run lock still apply. A failed dump
fails the run before rotation; the previous good archive remains.
If the configured Postgres container is missing or stopped, the optional set
instead logs `SOURCE MISSING, SET SKIPPED` and **NO fresh ledger backup**. Other
sets and ordinary retention finish. A Docker query failure remains fatal.
This deliberately differs from required path/volume missing-source verdicts.
It does not freeze ledger history: normal retention, including replacement of
the same day's daily, can remove an older ledger archive. Preserve a verified
ledger archive and its encryption key before disabling Sure long term.

The SSOT target is `/mnt/backup-drive/config-history`, on the LAN backup drive,
with `daily/` and `fib/` history. This corrects the older storage-map description
of library `Configs`. Ledger and `sure_storage` attachments are NEVER-OFFSITE;
never select them, their archives or any finance snapshot in IceDrive's GUI.
The backup script refuses offsite configuration; GUI selection remains operator
discipline. Attachments are copied live like existing unquiesced volume sets;
no transaction-aligned dump/attachment snapshot is claimed. Redis is a job/cache
store, and PGDATA is not copied as a substitute for a logical backup.

Recovery is a later Owner acceptance: use the existing archive verifier to
extract the chosen set, then pg_restore into a **new empty** matching Postgres
database, recover attachments, and reconcile accounts before any cutover. This
work neither runs recovery nor automatically replaces a database on reimage.
TOC parsing and tar/hash checks do not establish restorability.

**Restore dependency — NEVER ROTATE `SECRET_KEY_BASE`.** Self-hosted Sure derives
Active Record encryption keys from it
(`config/initializers/active_record_encryption.rb:23-36`). A ledger dump cannot
recover encrypted SimpleFIN access URLs/provider keys without the exact original
key; rotating it makes those columns unreadable. Preserve `SureSecretKeyBase`
in the operator's canonical user/machine-scoped DPAPI `~\DeployProps.xml` and
protected materialised install `.env`/ISO. Before ledger use, preserve it through
HomeHub's existing encrypted-vault export and retain the recovery factors
(`HomeHub/SECRET_HANDOFF.md`, T3 vault amendment). A pre-mint export lacks it.
Recover the original key with the dump; never mint a replacement for recovery.
This work has not created or verified that escrow artifact.

HomeHub T3 optional store keys `SureSecretKeyBase`, `SureDbPassword` and
`FinanceSureApiKey` map to `SURE_SECRET_KEY_BASE`, `SURE_DB_PASSWORD` and
`FINANCE_SURE_API_KEY`. Optional during preparation avoids demanding new
secrets for unrelated Actual materialisation. Optional does **not** prevent
minting: `PrepDeploySecrets.ps1 -NonInteractive` generates absent
`SureSecretKeyBase` (`GeneratedHex`) and `SureDbPassword` (`Generated`) even when
Sure is off (Generated/GeneratedHex branches, lines405-417). Run normal secret
preparation/materialisation before building or deploying the now-enabled default; create the bridge key with read scope in Sure before
the later source cutover. No values were minted here.

SimpleFIN setup token is entered in Sure UI, not env: `app/views/simplefin_items/new.html.erb:16`,
`app/controllers/simplefin_items_controller.rb:64,520`,
`app/models/simplefin_item.rb:8`, `app/models/provider/simplefin.rb:33`.
Database backups contain provider credentials and are NEVER-OFFSITE.

Web probe uses shipped curl and Rails `/up` (`Dockerfile:13`, `config/routes.rb:978`).
Worker uses Ruby/Sidekiq heartbeat (`app/models/sidekiq_health.rb:109-114`).
Database/cache probes match upstream. TLS assumption follows hosting/docker.md:
HTTP API/health traffic remains internal with `RAILS_ASSUME_SSL=true`.

**NOT proven:** startup, image executables, TLS/network/login/invites, provider
sync, bridge key scope/API access, runtime telemetry absence, pg_dump execution
or restore. No deployment, image pull, SSH, lab run, box contact or push.
Offline bake/save/load was not tested: under Docker's classic image store,
`docker save/load` of a `tag@digest` reference drops RepoDigests. If `sure` is
baked, Owner must verify that digest-based references resolve after loading the
payload; this authoring work does not prove that path.
