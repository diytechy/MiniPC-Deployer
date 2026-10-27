# Cross-Project Interfaces (IF-###)

Owned by the **System Engineer** hat. Copy to `docs/interfaces.md`. Use this
**only** when a project provides or consumes a contract shared with another
project/repo — skip it for a standalone deliverable.

It keeps interlinked projects honest without heavy multi-repo machinery: each
shared contract gets one stable id, one home, and a link back into the same
`SN→SR→LLR→TC` spine. The registry is `requirements/interfaces.csv`; this page
is the thin, human-readable index over it.

## Index (generated from the registry)

Every row of [requirements/interfaces.csv](requirements/interfaces.csv), generated
by `scripts/gen_if_index.py`. **Never hand-edit between the markers:** edit the
CSV and run `python scripts/gen_if_index.py`. `scripts/check.py` runs it with
`--check` (step `if-index`) and fails when the block is stale. The last column is
only the Contract cell's opening words; the CSV holds the contract.

<!-- BEGIN GENERATED IF INDEX -->
| IF | Direction | Counterpart | Status | Version | SR-Refs | Contract (opening words; full text in the CSV) |
|---|---|---|---|---|---|---|
| **IF-001** | Consumes | NagLight | Implemented | local | SR-006 | Consumes the tracker container image naglight:local — NagLight owns the Dockerfile/build (D1/WI-10.4) … |
| **IF-002** | Consumes | NagLight | Implemented | v1 | SR-003 | oauth2-proxy forwards verified identity headers (X-Forwarded-Email / X-Forwarded-User, X-Auth-Request-*) to … |
| **IF-003** | Consumes | NagLight | Implemented | v1 | SR-013 | POST /api/feed with JSON {check,ok,note}, Bearer NAGLIGHT_TOKEN (+ X-Forwarded-User for multi-user … |
| **IF-004** | Provides | Finance-Auditor | Implemented | v1 | SR-014 | Compose runtime for the finance-audit pipeline (counterpart id: FA IF-003; ids are repo-local — FA numbered … |
| **IF-005** | Provides | OfficeWallNaglight | Implemented | v1 | SR-016, SR-017, SR-004 | BUILT 2026-08-02; secret-boundary implementation completed 2026-09-10. PROVIDED: the wall panel's image … |
| **IF-006** | Consumes | NagLight | Active | v1 | SR-018 | v1 unified file-share/backup state contract (counterpart id: NagLight IF-010; ids are repo-local). POST … |
| **IF-011** | Provides | on-box callers (compose services and hub jobs) | Implemented | v1 | SR-019 | v1 AI CLI service (SR-019, A40 ratified 2026-09-09). POST /v1/ask on {$AI_CLI_BIND}:{$AI_CLI_PORT} - loopback … |
| **IF-012** | Consumes | OfficeWallNaglight (the panel shell) | Draft | v1 | SR-020 | v1 panel presence signal (SR-020, SN-015). The shell WRITES {$WALL_PRESENCE_FILE} (default … |
| **IF-013** | Consumes | NagLight | Implemented | v1 | SR-021 | v1 AI-usage gauge feed (SR-021, SN-016). POST {$AI_USAGE_FEED_URL} with kind="gauge" and the tightened … |
| **IF-014** | Consumes | NagLight | Partial | v1 | SR-022 | v1 weight gauge feed (SR-022, SN-040). POST {$WEIGHT_FEED_URL} with kind="gauge", id="weight" - THE SAME ID … |
| **IF-015** | Provides | OfficeWallNaglight | Implemented | v1 | SR-023, SR-028, SR-048 | v1 panel-local audio socket at {$WALL_AUDIO_SOCKET}; Unix only, peer-UID checked, mode 0660, bounded … |
| **IF-016** | Consumes | OfficeWallNaglight | Implemented | v1 | SR-024 | Private shell artifact capability door-stream-v1 must include doorstream/service.py and doorstream/motion.py. … |
| **IF-017** | Provides | OfficeWallNaglight | Implemented | v1 | SR-026 | v1 amplifier-activation service boundary: OfficeWallNaglight and Bluetooth deliver audio only to the existing … |
| **IF-018** | Provides | OfficeWallNaglight | Implemented | v1 | SR-032 | v1 root-owned local setup AF_UNIX socket accepts newline-delimited {method,params} only from the panel uid. … |
| **IF-019** | Provides | OfficeWallNaglight | Implemented | v1 | SR-036 | v1 HEVC decode capability contract: the wall image installs va-driver-all and vainfo and, at firstboot … |
| **IF-020** | Provides | OfficeWallNaglight | Draft | v1 | SR-041 | v1 optional panel-local ProjectM PCM stream at /run/wall-bus-visualizer/pcm.sock; AF_UNIX SOCK_STREAM only … |
| **IF-021** | Consumes | Dev PC (Windows host) | Draft | v1 | SR-043, SR-044 | v1 OpenAI-compatible local inference endpoint served by the dev PC on its LAN address at the configured … |
| **IF-022** | Consumes | Dev PC (Windows host) | Draft | v1 | SR-044 | v1 out-of-band power actuation for the dev PC. The hub sends a Wake-on-LAN magic packet to the dev PC's … |
| **IF-023** | Provides | Dev PC (Windows host) | Draft | v1 | SR-043, SR-044 | v1 gateway and readiness surface the hub PROVIDES to off-box consumers — coding agents on the dev PC, hub … |
| **IF-024** | Consumes | Mesh VPN coordination service (external) | Draft | v1 | SR-042 | v1 outbound-initiated mesh VPN. The hub's daemon DIALS OUT to a third-party coordination service and accepts … |
| **IF-025** | Consumes | Dev PC (Windows host) | Draft | v1 | SR-044 | v1 interactive-session attachment signal, consumed by the hub's wake service to decide whether sleep is … |
| **IF-026** | Provides | Dev PC (Windows host) | Draft | v1 | SR-044 | v1 sleep-permission verdict the hub PROVIDES and the dev PC polls. The hub owns the POLICY and nothing else … |
| **IF-030** | Consumes | Sure release images | Draft | v1 | SR-060, SR-061 | Four profiled roles at v0.7.5-hotfix.2 e336a285; Postgres:16.15 and Redis:8.10.2 explicit tags with registry … |
| **IF-031** | Provides | Household Sure clients | Draft | v1 | SR-062 | LAN-scoped TLS terminating Caddy route to sure-web:3000. Sure own logins; no oauth2-proxy or trusted identity … |
| **IF-032** | Provides | Finance-Auditor | Draft | v1 | SR-063, SR-066 | DATA_SOURCE actual default; SURE_URL internal sure-web:3000; SURE_API_KEY read-only scope supplied later … |
| **IF-033** | Consumes | HomeHub backup source materialisation | Draft | v1 | SR-065 | postgres:sure-postgres source plus volume:sure_storage rows PROFILE=sure; exact profile activates generated … |
| **IF-034** | Consumes | HomeHub secret materialisation | Draft | v1 | SR-066 | Optional preparation T3 SureSecretKeyBase and SureDbPassword map Sure startup env; FinanceSureApiKey maps … |
| **IF-035** | Consumes | NagLight | Implemented | v1 | — | v1 frame-freshness lane producer (stack/tracker/frame-freshness.sh; timer … |
| **IF-036** | Consumes | NagLight | Implemented | v1 | — | v1 tracker-definitions guard producer (stack/tracker/tracker-defs-guard.sh; the timer runs --report). … |
| **IF-037** | Consumes | NagLight | Draft | v1 | — | v1 Finance-Auditor colour lane. Producer is the finance-auditor service in stack/docker-compose.yml, whose … |
| **IF-038** | Provides | HomeHub | Draft | v1 | SR-076, SR-077, SR-078 | v1 AI CLI provisioning contract (stack/ai-cli). (1) Manifest stack/ai-cli/ai-clis.manifest.tsv … |
| **IF-065** | Provides | HomeHub | Implemented | v1 | SR-086, SR-087, SR-088, SR-089, SR-091 | v1 hub monitoring artefacts (stack/monitoring, SN-048). Status files under /var/lib/homehub-monitor (root … |
| **IF-066** | Consumes | ntfy (stack compose service) | Implemented | v1 | SR-085 | v1 host-side alert post. POST http://&lt;NTFY_BIND_IP or LAN_IP>:&lt;NTFY_PORT>/&lt;HOMEHUB_ALERT_TOPIC, default … |
| **IF-067** | Provides | OfficeWallNaglight | Implemented | v1 | SR-093, SR-094 | v1 power-button warning state (WSN-098). Producer: wall-power-button.service (root). … |
| **IF-068** | Provides | Finance-Auditor | Draft | v1 | SR-098 | v1 accrual-gauge inputs (counterpart FA SR-035/SR-051): a host directory mounted read-only at /config holding … |
| **IF-069** | Consumes | NagLight | Draft | v1 | SR-097 | v1 tracker bearer contract (NagLight SR-030/LLR-020, internal/web/auth.go): with TRACKER_API_TOKEN set every … |
| **IF-070** | Provides | OfficeWallNaglight (the door broker) | Implemented | v1 | SR-105, SR-106 | v1 door relay RTSP endpoint (SR-105). rtsp://&lt;hub LAN_IP>:&lt;DOOR_RELAY_RTSP_PORT, default … |
| **IF-071** | Provides | scratch-volume writers (the door recorder; meetings, plan W6; OtterTube, plan W8; motion clips, SR-131) | Implemented | v1 | SR-103, SR-104 | v1 scratch writer contract (SR-103, SR-104). A writer owns /srv/scratch/&lt;name>/ (door, meetings, clips or … |
| **IF-072** | Consumes | HomeHub (private deploy config) | Implemented | v1 | SR-114, SR-115 | v1 OtterTube curated list (SR-114). One JSON file, HomeHub scripts/deploy/ottertube/list.json, installed as … |
| **IF-073** | Provides | OtterTube (browser build and desktop app) | Implemented | v1 | SR-117 | v1 OtterTube site (SR-117). https://&lt;OTTERTUBE_SUBDOMAIN>.&lt;DOMAIN>/ottertube/ on the LAN only (403 elsewhere … |
| **IF-074** | Provides | HomeHub (the Owner and agents: charts, debugging, later analysis) | Implemented | v1 | SR-122, SR-123, SR-124 | v1 hub metrics log (SR-122..SR-124), all JSONL, one object per line, UTC epochs. … |
| **IF-075** | Provides | OtterTube (desktop app's update check) and HomeHub (the publisher) | Implemented | v1 | SR-125, SR-126, SR-127 | v1 OtterTube update channel (SR-125..SR-127). On the OtterTube site (IF-073; LAN only, the site knob … |
| **IF-076** | Provides | event sources: the hub detector (stack/camera-motion); later an ONVIF adapter and the panel's spool | Implemented | v1 | SR-128, SR-129 | v1 camera motion-event record and store (SR-128). One compact JSON object per line, at most 512 bytes: {v: 1 … |
| **IF-077** | Provides | whoever reads motion clips: the Owner over a share (later), Jellyfin never | Implemented | v1 | SR-129, SR-130, SR-131 | v1 motion clips tree (SR-129..SR-131). &lt;CLIPS_ROOT>/&lt;camera>/&lt;local YYYY-MM-DD>/&lt;camera>_&lt;local … |
<!-- END GENERATED IF INDEX -->

## Notes on selected interfaces (hand-written)

> **2026-10-19:** these notes were the whole index until it was found to name 12
> of the CSV's 31 rows; the generated table above now lists them all. The notes
> are kept for the context they add. Where a note and the CSV disagree, the CSV
> wins.

**This repo ↔ NagLight** (IF-001 to IF-003):

All three are `Consumes` from this repo's side — NagLight (private) owns the
contracts; matching IF-IDs should be recorded there too:

- **IF-001** — the `naglight:local` container image (NagLight owns the build,
  D1/WI-10.4; resolved via `scripts/ensure-local-images.sh`, SR-006).
- **IF-002** — the trusted-header identity contract: oauth2-proxy →
  `X-Forwarded-Email`/`-User` → NagLight multi-user mode (D3/WI-10.5, SR-003).
- **IF-003** — `POST /api/feed` `{check,ok,note}` + Bearer token — the backup
  service's step 6 report. RETIRED: `/api/feed` is no longer posted from
  `stack/backup`. Since 2026-10-23 `feed_naglight` in `stack/backup/common.sh`
  posts `nightlySuccess` to `/api/backup-state` instead (IF-006, SR-121) on a
  verified, promoted nightly run, and posts nothing on a failure (SR-013).

**This repo ↔ Finance-Auditor** (`Provides` — FA numbered its registry first,
so ids are repo-local; each Contract cites the counterpart's id):

- **IF-004** — the compose runtime for the `finance-auditor` service
  (↔ FA IF-003): profile-gated service, the two finance volumes, `FINANCE_*`
  knobs, and the resolver build pinning `ACTUAL_API_VERSION` to
  `ACTUAL_IMAGE_TAG` (FA IF-002's coupling). The v2 Caddy detail-view route
  (FA IF-004) is deliberately not provided yet.

**This repo ↔ OfficeWallNaglight** (`Provides` — the image side of the wall
panel; that repo owns the app):

- **IF-005** — `Implemented` 2026-09-10. The image consumes one stamped
  OfficeWallNaglight build as panel app, hub site and private gateway payloads;
  `/media/*` remains panel-local. HomeHub serves only nonsecret renderer config.
  The legacy feed token, Kuma push URL and optional Subsonic credentials
  originate in root-only panel `wall.env`, are materialized into the panel-owned
  0600 Electron host JSON, and reach only the exact trusted panel document over
  named IPC. Hub preflight rejects those secrets in either access mode. Physical
  image/panel acceptance remains unproven; see `stack/autoinstall/wall/README.md`.
- **IF-017** — `Implemented` 2026-09-13. The app and Bluetooth sink deliver
  audio to the existing ALSA path and know nothing about relay hardware. The
  image-owned detector drives exactly one actuator, the LCUS-2 USB relay; the
  audio-jack tone alternative was retired 2026-09-13 and is refused rather than
  built, so no configuration can claim the jack. An ON/OFF claim requires
  device status readback, and shutdown/suspend own verified OFF (SR-026).
- **IF-020** — `Draft` 2026-09-17 (SR-041). The OPTIONAL panel-local ProjectM
  PCM stream at `/run/wall-bus-visualizer/pcm.sock`. AF_UNIX only, mode 0660
  `root:panel`, authorized by `SO_PEERCRED` uid against a configured allow-list
  **and** that filesystem permission; an empty allow-list denies and no client
  is started by default. Each message is one frozen 32-byte-header `WPCM`
  version-1 frame carrying at most one 1024-frame stereo bus period, refused at
  both ends when malformed, truncated or oversized; backpressure is depth one,
  so an older unsent block is dropped rather than accumulated. A byte-identical
  fixture ships in BOTH repositories (`tests/fixtures/pcm-frame-v1.bin`) and is
  decoded by both suites, which is what stops the two implementations drifting.
  **The endpoint's absence is a supported state**: the image installs before an
  app that can request it, and the app falls back to Frame Media. Raw PCM is
  local and memory-only, exists only while ProjectM is the selected and visible
  fullscreen owner, and is never written to disk, journaled, put in diagnostics
  or telemetry, uploaded, or kept after ProjectM yields — and the exception
  grants no microphone capture, recording, diagnostics or network transmission.

**This repo → on-box callers** (`Provides` — the AI CLI service):

- **IF-011** — `Implemented` 2026-09-09 (SR-019; HomeHub A40). `POST /v1/ask`
  on `{$AI_CLI_BIND}:{$AI_CLI_PORT}` — **loopback or the docker bridge only,
  never the LAN** — takes `{route, schema, messages}` and answers by running one
  headless CLI session on the household subscription. **Model and depth are
  configuration, not request fields:** they come from the registry row a
  `route` names, and a request carrying `model` or `effort` is ignored. `schema`
  is required and becomes the CLI's `--json-schema`. **Not provided:** any path
  that reaches this from the LAN, `--bare` (it ignores the OAuth token and needs
  an API key), and an unconstrained answer. **Latency:** because `--bare` is
  unusable on the subscription, full startup is paid per call — measured
  2026-09-09 at ~1.6–2.2 s of non-model startup and ~3–4 s wall for the cheapest
  call, on the dev PC; the hub is slower. Callers must not put this on a
  sub-second synchronous path. Details in `stack/ai-cli/README.md`.

**This repo → NagLight** (`Consumes` — the AI-usage gauges):

- **IF-013** — `Implemented` 2026-09-09 (SR-021; SN-016). `POST
  {$AI_USAGE_FEED_URL}` with `kind:"gauge"` — six ids
  (`ai-usage-codex`, `ai-usage-claude-session`, `ai-usage-claude-weekly`,
  `ai-usage-claude-weekly-fable` (optional, posted once the vendor reports it),
  `ai-usage-opencode-weekly`, `ai-usage-opencode-monthly`) plus four more only
  when the opt-in source is enabled (SR-067): `ai-usage-claude2-session`,
  `ai-usage-claude2-weekly`, `ai-usage-claude2-weekly-fable` (optional, as
  account 1's) and `ai-usage-gemini-daily` (kind `daily`; its label names the
  model set it is the lowest remaining fraction of), each carrying
  `unit:"%"`, `min:0`, `max:100`, `target:0`, `direction:"down"` and a `window`.
  `value` is percent remaining (100 minus the consumed percent), so the pace line
  falls from a full bar to zero at the reset.
  **Numbers only — never a severity, a `css` or a colour:** NagLight owns
  severity and the panel renders it, and a feeder that computed a hue would make
  that authority ambiguous. `value` and `target` are always present and `min`/
  `max` always sent, because the 2026-09 tightening turned each omission into a
  400 (an omitted `value` used to be stored as a fabricated 0, and `%` has no
  agreed width to infer a range from); `direction` is emitted **if and only if**
  `window` is. **`observed_at` is when the value was TRUE**, which is how
  "unavailable" is said without inventing a number: a source that fails reposts
  its last real reading at its *original* stamp and goes stale on NagLight's
  horizon — except a source whose login is confirmed dead by failed attempts
  spanning ten minutes (SR-068), which posts the stale-on-arrival body for every one of its ids
  and keeps doing so — and one that has never succeeded posts a gauge with no `observed_at`
  at all, stale on arrival. The timer runs every minute and each source is read on
  its own interval, 60 s by default (Codex 120 s), backing off on 429/5xx
  (SR-095); a source that is skipped is not posted, so NagLight keeps its last body. **Not provided:** OpenCode's
  `rolling` bucket, which publishes no window length — a window this repo would
  have had to invent. **Not proven:** the Gemini response shapes against a live
  call (the parser is written from the CLI's source). Details in
  `stack/ai-usage/README.md`.

**This repo → NagLight tracker** (`Consumes` — the check-feed producers that
post to `/api/feed` from this box or the stack; each row cites its code):

- **IF-035** — `Implemented`. The frame-freshness lane,
  `stack/tracker/frame-freshness.sh`: colour `green` or `yellow` only, posted by
  `docker exec` of the tracker container; `--check` posts nothing; exit is 0/2 (green/yellow) by band in both
  modes, and 1 only when `--report` fails to deliver, so the unit shows it.
- **IF-036** — `Implemented`. The tracker-definitions guard,
  `stack/tracker/tracker-defs-guard.sh`: colour `green`, `yellow` or `red`; a
  failed post changes the exit status only through the unknown-check escalation.
- **IF-037** — `Draft`. The Finance-Auditor colour lane, posted by the
  `finance-auditor` service that `stack/docker-compose.yml` runs; the wire contract
  is Finance-Auditor's IF-001. `reason` is 1-80 characters and `color` is from
  Finance-Auditor's colour order; the same URL and headers also carry its separate
  gauge POST. Failure is logged and not turned into a nonzero exit.

---

## Why a separate registry

A cross-project link is a requirement with an *external* counterpart, so it
needs the things ordinary requirements have — an owner, an acceptance contract,
a test — **plus** a version and a stability promise the other side can rely on.
Putting these in one place stops the classic failure of interlinked projects:
each side assumes a slightly different contract and they rot apart silently.

## ID scheme & columns

`IF-###` — Cross-Project Interface. Zero-padded, stable, never reused (its own
namespace, parallel to SN/SR/LLR/TC).

| Column | Meaning |
|---|---|
| `IF-ID` | Stable id for this interface. |
| `Direction` | `Provides` (we expose it) or `Consumes` (we depend on it). |
| `ThisProject` | This repo/project name. |
| `Counterpart` | The other project/repo on the far side of the contract. |
| `Contract` | One testable line naming the surface (REST route, CLI, file schema, event, library symbol) + a link to its spec. |
| `SR-Refs` | The system requirement(s) here that realize or rely on it — ties the interface into the local spine. |
| `Version` | Contract version the other side codes against (e.g. `v1`, a semver, a schema hash). |
| `Stability` | `Experimental` · `Stable` · `Deprecated`. Sets the change-notice bar. |
| `Status` | `Draft` · `Agreed` · `Implemented` · `Verified`. |

## Rules (keep links from rotting)

- **One contract, one home.** The owning side (`Provides`) holds the
  authoritative spec; the consuming side links it by `IF-ID` and never re-states
  it. If both repos describe the shape, they will diverge — link instead.
- **Every interface is backed by an SR and a TC.** A `Provides` interface needs
  a contract test that asserts the published shape; a `Consumes` interface needs
  a test (or recorded fixture/mock pinned to `Version`) proving we read it
  correctly. No interface ships untested.
- **Both sides reference the same `IF-ID`.** Use identical ids across repos so a
  human or agent can grep one string and find both ends. Record the counterpart
  repo + the matching id so the trail is two-way.
- **Stability gates change.** Changing a `Stable` contract requires a notice to
  the counterpart and a version bump; `Experimental` may change freely. Note
  breaking changes in the audit log and bump `Version`.
- **Direction drives ownership.** Only the `Provides` side may close G-Final on
  the contract's correctness; the `Consumes` side verifies against the pinned
  version.

## Worked snippet

```csv
IF-ID,Direction,ThisProject,Counterpart,Contract,SR-Refs,Version,Stability,Status
IF-001,Provides,billing-api,reporting-etl,"GET /v1/invoices returns the documented JSON schema (see docs/openapi.yaml#/Invoice).",SR-014,v1,Stable,Verified
IF-002,Consumes,reporting-etl,billing-api,"Reads GET /v1/invoices; depends on IF-001 v1 schema (pinned fixture in tests/fixtures/invoice_v1.json).",SR-031,v1,Stable,Verified
```

Read together: `billing-api` publishes `IF-001` (with a contract test on the
schema); `reporting-etl` consumes the same contract as `IF-002`, pins `v1`, and
tests against a recorded fixture. Grep `IF-001` across both repos to see the full
link. If `billing-api` needs a breaking change it bumps to `v2`, notifies
`reporting-etl`, and both rows move to the new version deliberately — never by
accident.
