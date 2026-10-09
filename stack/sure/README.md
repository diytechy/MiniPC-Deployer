# Sure preparation (SR-060..SR-065)

The `sure` profile defines web, Sidekiq, Postgres and Redis beside Actual.
It is absent from shipped `COMPOSE_PROFILES`. Sure owns household logins.
Caddy accepts `LAN_CIDR` only, proxies to `sure-web:3000`, and injects no hub
identity. Technitium provisions `SURE_SUBDOMAIN`. No service publishes a port.

Web and worker share `/rails/storage`. Database/cache are on internal network
`sure`; Rails also joins `default` for bank/price-provider egress. Finance-Auditor
joins both, retains `DATA_SOURCE=actual` and has no Sure startup dependency.
The future read-only API URL is `http://sure-web:3000`.
`FINANCE_TRACKER_FEED_USER` is a separate non-secret Google sub. The header fix
is dispatched in Finance-Auditor `docs/status.md`, "Dispatched from HomeHub";
it is not merged by this preparation. Actual remains unchanged.

## Registry evidence and payload

Pins use `tag@sha256:index`. On 2026-10-09, anonymous HTTPS registry manifest
responses were independently SHA-256 hashed and matched `Docker-Content-Digest`.
No image layers were fetched. Sure's amd64 config OCI revision matches source
`e336a285d96582253debd57396b33e18b4e2c66c`.

| Tag | Index SHA-256 | Linux amd64 manifest SHA-256 |
|---|---|---|
| `ghcr.io/we-promise/sure:0.7.5-hotfix.2` | `aa46be2d8e3f8b2182261dc05933a64b4a12543dd5f44ad12f08868caac1f997` | `cd6706f544cd5f9b299a586d719776cf2595b483ea1f1a396137da0d23eafb20` |
| `postgres:16` | `ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d` | `75adc2a65806c96bc0e59030230bfe3f1c1b0d6d8f8181a8913e2b8b53bdc166` |
| `redis:latest` | `2c2dff791878316e3b083188be0d578423b24813cdda5de0e80ea4f7b3e6bfd6` | `6e9101032a2126aeeb4b1e862ac77d2fcf26b831ae37e5ff2e69169f82ddd411` |

Upstream's example specifies Redis `latest`; config metadata identifies it as
`8.10.2`. The immutable digest preserves the upstream tag selection; no numeric
Redis version was named by upstream. Postgres metadata identifies
`16.15-1.pgdg13+2`. The exporter derives its list from Compose and selected
profiles, including `EXTRA_PROFILES`; a future bake must select `sure` even if
runtime activation follows later. No duplicate image list is needed. Diun
watches Docker containers by default; all four explicitly opt in. These public
images do not belong in the private-app `ensure-local-images.sh` resolver.

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

HomeHub T3 optional store keys `SureSecretKeyBase`, `SureDbPassword` and
`FinanceSureApiKey` map to `SURE_SECRET_KEY_BASE`, `SURE_DB_PASSWORD` and
`FINANCE_SURE_API_KEY`. Optional during preparation avoids demanding new
secrets for unrelated Actual materialisation. Supply both Sure startup secrets
before selecting `sure`; create the bridge key with read scope in Sure before
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
