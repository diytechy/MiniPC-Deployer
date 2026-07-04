# Project Status — Blackboard

Live coordination for the gated process (see [process.md](process.md)). Keep the
**Current State** header short and current; append the audit log below (newest
last) — it is the record, not required reading for every pass.

---

## Current State

- **Active gate:** G1 — Requirements, UX & constraints. This is a config/infra
  repo delivered against a ratified brief (HOMELAB_RESTRUCTURE_PLAN.md); the
  requirement spine is intentionally **high-level** (proportionality doctrine).
- **Round:** 1
- **Open items:**
  - **Needs Peter** _(ratification / manual steps — dial=HIGH)_:
    - OI-1 — **Google OAuth client** must be created in Google Cloud (needs
      Peter's account); redirect URI to register:
      `https://tracker.<domain>/oauth2/callback` →
      [stack/.env.example](../stack/.env.example)
    - OI-2 — **Reimage-over-LAN ladder** is HIGH-RISK; the memo presents options
      with checkboxes. Nothing destructive is implemented until Peter checks one
      → [REMOTE_MANAGEMENT.md](../REMOTE_MANAGEMENT.md)
    - OI-3 — **Push** each commit (agents lack the SSH key) →
      this repo
    - OI-5 — **Run the V3 gate** (WI-10.18): enable Hyper-V (elevated,
      machine-level, needs a reboot), create the VM, do the one-time GRUB
      edit (light path), and confirm compose-up →
      [vmtest/README.md](../vmtest/README.md). Nobody has booted a VM from
      this yet — scripts delivered + partially smoke-tested, boot itself is
      Peter's step (elevation + Hyper-V).
    - OI-6 — **C: free space is tight (~9GB)** after this session's ISO
      download/repack smoke test — WSL2's `ext4.vhdx` grew and does not
      auto-shrink on file deletion (see vmtest/README.md §2 for the
      reclaim-it steps: `wsl --shutdown` + `Optimize-VHD`/`diskpart compact`,
      elevated). Not urgent, but worth doing before further large downloads.
  - **In flight** _(driver; no approval needed)_:
    - OI-4 — layering WI-10.2/10.11/10.12 onto the migrated base →
      [stack/docker-compose.yml](../stack/docker-compose.yml)
- **Assumptions (unattended):** see the Assumptions log below.
- **Next action:** Peter reviews + pushes; creates the Google OAuth client;
  reacts to the reimage-ladder checkboxes. Runtime bring-up on the first Docker
  host / the AWOW itself.
- **UPDATE 2026-07-03 (WI-10.13):** the "no Docker on the dev machine" constraint
  above is now LIFTED — WSL2 + Ubuntu 24.04 + docker-ce is installed on the dev
  PC (Docker Desktop explicitly NOT installed, per Peter's pick). `naglight:local`
  now builds for real and `docker compose config` resolves this stack's full
  compose file. See audit log entry below for versions/detail. Wave 2 (V1
  AWOW-sim, WI-10.14/10.15) is now unblocked.

## Scope (restated from the brief)

- **Goal:** the deploy repo for the headless AWOW AK41 always-on box — an
  unattended, self-healing Docker stack (DNS, reverse proxy + TLS, the NagLight
  tracker behind Google sign-in, Actual Budget, LAN observability) plus an
  Ubuntu autoinstall image and full LAN remote management. **Config only** — app
  code lives in NagLight / Finance-Auditor / MinecraftKeeper.
- **Stakeholders / end user(s):** Peter (homelab operator); the tracker's hosted
  end-users reach it only through oauth2-proxy.
- **Active hats:** Stakeholder, UX/Docs, System Engineer, Software Engineer, Test
  Engineer, **Network**, **Security/Ops** (the domain hats this infra scope
  needs).
- **Supported platforms:** the deploy target is **Linux** (Ubuntu 24.04 on the
  AWOW box); authored on Windows. Not a launchable product.
- **Constraints:**
  - ~~No Docker on the dev machine~~ **SUPERSEDED 2026-07-03 (WI-10.13):** WSL2 +
    Ubuntu + docker-ce now installed on the dev PC. `naglight:local` build and
    `docker compose config` are now verified for real (see audit log). Full
    runtime bring-up (containers actually running end-to-end) is still PENDING
    the WI-10.14 AWOW-sim harness.
  - **Public-facing repo (Q10.6):** only `*.example` templates tracked; no real
    secret/hash/email/LAN detail/personal name. Local commit identity pinned to
    `diytechy <diytechy@users.noreply.github.com>`.
  - **Kit:** minimum profile, decision dial **HIGH** (secrets-adjacent infra).
  - **No product source in this repo** → the Python `ruff`/`pytest`/arch-map
    steps are dropped (not left passing vacuously, ADOPTING.md §3). The
    product-layer check is `scripts/validate_config.py`.
- **Non-goals:** a container registry / CI publishing (deferred, Q10.2 — local
  builds for now); the NagLight app code and its multi-user engine (NagLight
  repo, WI-10.4/10.5); the secret-handoff script (WI-10.3, Peter ratifies).
- **Definition of done:** the repo's G1 gate is green (`check.py`), `.env.example`
  enumerates every knob, config coverage validates, and the honest validation
  ledger records what remains PENDING a Docker host.

## Honest validation ledger (WI-4.7 note carried forward)

| Check | State |
|---|---|
| `docker compose config` / live bring-up | **PENDING — no Docker on dev box (verified)** |
| curl health endpoints, `dig`, OAuth round-trip, tear-down | **PENDING — Docker host / the AWOW** |
| Q10.9 B+ image payload: `export-images.sh` save + `docker load` all 9 | PASS (WSL; loads idempotent) — first-boot load-at-VM awaits V3 |
| Shell scripts `bash -n` | PASS |
| `docker-compose.yml`, `meta-data`, `user-data` YAML parse | PASS (PyYAML) |
| Every compose `${VAR}` has an `.env.example` key | PASS (`validate_config.py`) |
| Every Caddy `{$VAR}` passed by the caddy service env | PASS |
| Bind-mount sources + autoinstall-referenced files exist | PASS |
| `check.py` (G1: config-validate + registry-integrity + doc-navigability) | PASS |

Runtime validation must be redone on the first Docker host before the burn-in
checklist (stack/README §6) can be signed.

## Gate Sign-offs

Add columns for any active domain hats. Drop the `G-Release` row for a one-off
deliverable.

| Gate | Stakeholder | UX/Docs | System Eng | Test Eng | Human |
|---|---|---|---|---|---|
| G1 — Requirements/UX/Constraints | PENDING | PENDING | PENDING | n/a | PENDING |
| G2 — Decomposition & Test Coverage | n/a | n/a | PENDING | PENDING | PENDING |
| G3 — Implementation | n/a | n/a | PENDING | PENDING | PENDING |
| G-Release — Release readiness | n/a | n/a | n/a | PENDING | PENDING |
| G-Final — Acceptance | PENDING | n/a | n/a | (evidence) | PENDING |

---

## Audit log

<!-- Append verdict blocks here per process.md §5. Newest at the bottom. -->

### DRIVER — G1 — Round 1 — 2026-07-03
Scaffolding created. Starting G1.

### Assumptions log (unattended, dial=HIGH — Peter to confirm/revert)
- A1 — Layout: migrated `life-tracker/deploy/*` under `stack/` at the repo root
  (kept the kit's `docs/`/`scripts/` roots). On-box path renamed `deploy/` →
  `stack/` under `/opt/awow-core/`; the box hostname/opt-dir keep the `awow-core`
  name (faithful to the source; Peter knows it).
- A2 — This repo is treated as gate **G1** (requirements-agreed) — config is the
  deliverable, there is no compiled source to carry to G2/G3. The Python
  product/arch-map steps are dropped, not left vacuous.
- A3 — oauth2-proxy image pinned to `v7.6.0`, Google provider, allow-list via
  `authenticated-emails.txt` (Q10.5). Redirect URI `…/oauth2/callback`.
- A4 — SSH is **key-only** by default in the autoinstall (locked password),
  per WI-10.12 "key-only auth". Cockpit installed but **not** proxied publicly.
- A5 — Kept the `TRACKER_DATA_REMOTE` clone/pull entrypoint behaviour from the
  source; multi-user (D3) sets it blank + `TRACKER_COMMIT=false` (documented).

### DRIVER — G1 — Round 1 — 2026-07-03 (migration + spine)
Migrated the deploy stack, wired the tracker to `naglight:local`, authored the
high-level SN/SR spine (8 SN, 11 SR), and added `scripts/validate_config.py` as
the config-repo product check. `check.py` (G1) green; integrity 0. WI-10.2 (oauth
+ Caddy re-route), WI-10.11 (aux containers), WI-10.12 (remote mgmt) layered in
subsequent commits.

### DRIVER — G1 — Round 1 — 2026-07-03 (WI-10.2 oauth2-proxy + Caddy re-route)
Added the oauth2-proxy service (Google provider, allow-list file, cookie secret,
identity headers to the tracker). Re-routed the Caddyfile: tracker host →
oauth2-proxy (no basic_auth); Actual + dns keep basic_auth (split into
per-service snippets since Caddy resolves {$VAR} once at load). Firstboot now
materializes the allow-list from `OAUTH2_PROXY_ALLOWED_EMAILS`. Documented the
PETER MANUAL STEP (Google OAuth client) in .env.example + stack/README. Redirect
URI to register: `https://tracker.<domain>/oauth2/callback`. config-validate
green (25 compose vars covered, 8 Caddy vars passed).

### DRIVER — G1 — Round 1 — 2026-07-03 (WI-10.11 auxiliary containers)
Added Uptime-Kuma, Dozzle, and optional ntfy (compose `profiles: [ntfy]`), all
LAN-only (published bound to `LAN_IP`, never proxied publicly), all
healthchecked, every knob in `.env.example`. config-validate green (31 compose
vars covered).

### DRIVER — G1 — Round 1 — 2026-07-03 (WI-10.12 LAN remote management)
Implemented now, in the autoinstall: SSH **key-only** (allow-pw false, locked
password, authorized-keys placeholder), **Cockpit** host package (LAN-only :9090,
not proxied), and **unattended-upgrades**. Documented the remote `docker compose`
ops workflow. Wrote `REMOTE_MANAGEMENT.md` — the reimage-over-LAN ladder as a
decision memo with checkboxes (recommended: **B GRUB recovery partition** primary
+ **D smart-plug/USB** fallback). **HIGH-RISK line:** nothing destructive /
reimage-related (no `storage:` recovery-partition change, no GRUB reinstall entry,
no PXE) is implemented until Peter checks a box.

<!-- agent-setup --> Agent setup (2026-07-03): agents=`claude`; skills materialized: downstream-resync, gate-advance, registry-hygiene. AGENTS.md remains the canonical, agent-neutral guide (skills are opt-in accelerators, not a process gate).

### DRIVER — G1 — Round 1 — 2026-07-03 (WI-10.13 dev-PC container runtime)
Peter picked **(a) WSL2 + docker engine inside Ubuntu**, explicitly over Docker
Desktop (not installed; no other tooling touched). This closes Wave 1's
unverified-image-build honesty gap for real.

**Installed on the dev PC (machine-level, Peter-consented):**
- WSL2 itself was already enabled/functional (a pre-existing
  `podman-machine-default` WSL2 distro was running) — no VirtualMachinePlatform
  enable + reboot was needed.
- `Ubuntu` distro registered via the pre-existing `CanonicalGroupLimited.Ubuntu`
  appx package (was installed but never first-run) — ran non-interactively via
  `ubuntu.exe install --root`, avoiding the interactive username/password
  prompt. Result: **Ubuntu 24.04.1 LTS (Noble)**, WSL version 2. Default WSL
  user is `root` (a consequence of the `--root` non-interactive path). A
  secondary non-root user `peter` was also created and added to the `docker` +
  `sudo` groups for future interactive use, but is NOT the WSL default (no
  extra restart was spent switching it — root already has full docker access).
- `/etc/wsl.conf` → `[boot] systemd=true`; confirmed via `wsl --shutdown` +
  relaunch that `systemd` is PID 1.
- **docker-ce from Docker's official apt repo** (not `docker.io`, not Docker
  Desktop): `docker-ce docker-ce-cli containerd.io docker-buildx-plugin
  docker-compose-plugin`. Versions: **Docker 29.6.1** (build 8900f1d),
  **Docker Compose v5.3.0** (plugin). `docker.service` enabled + running under
  systemd.

**Verification, in order (all real, all honest):**
1. `docker run --rm hello-world` → **PASS** (pulled + ran, full expected output).
2. `docker build -t naglight:local /mnt/c/Projects/NagLight` → **PASS**. Built
   clean: Go 1.26-alpine build stage → alpine:3.20 runtime stage, final image
   `naglight:local` (14.1MB content, 47.8MB disk). No errors, no warnings beyond
   Docker's own advisory notices. This closes NagLight's TC-044
   unverified-build gap.
3. `docker compose --env-file <scratchpad>/sim.env config` from `stack/` →
   **PASS** (exit 0). Used a placeholder-but-syntactically-valid `.env`
   (fictional domains/keys/tokens; generated fresh, kept in the agent's
   scratchpad, never written into this repo). All 8 services resolved
   correctly: `actual`, `caddy`, `ddns`, `dozzle`, `oauth2-proxy`, `technitium`,
   `tracker` (confirmed `image: naglight:local` — the just-built image),
   `uptime-kuma`. Volumes/networks/healthchecks all present in the rendered
   config. Minor test-data artifact (not a stack bug): the placeholder bcrypt
   hash values contained unescaped `$` characters, which docker compose's own
   `.env` interpolation partially consumed (`$2a$14$...` truncated to `$2a$14`
   in the rendered output) — a property of how I wrote the throwaway env file,
   not of the compose file itself.

**Tooling note for future sessions:** invoking `wsl.exe` through this agent's
Bash/PowerShell tools silently mangles any `$VAR` in the command string (an
outer shell layer pre-expands it before the real command runs) and Git Bash's
MSYS layer rewrites leading `/mnt/c/...` paths unless `MSYS_NO_PATHCONV=1` is
set. Workaround used throughout: write scripts to files (Write tool, no shell
involved) and execute them via `wsl.exe -d Ubuntu -- bash /mnt/c/...script.sh`
with `MSYS_NO_PATHCONV=1` set on any command touching `/mnt/c/...` paths
directly.

**Remaining for Peter:** none — no reboot, no interactive prompt was needed.
Wave 2's V1 AWOW-sim (WI-10.14/10.15) is now unblocked.

### DRIVER — G1 — Round 1 — 2026-07-04 (WI-10.14 AWOW-sim harness + V1 gate)
Built `sim/` as a compose **overlay over the real `stack/docker-compose.yml`**
(never a fork): `docker-compose.sim.yml` + `.env.sim` (all fictional), a mock
**Dex** OIDC provider (2 static users) swapped in for Google, Caddy on an
**internal CA**, Technitium moved to the bridge with an alt API port, ddns + aux
containers neutralised, a `simclient` probe box, and multi-user fixture seed
data. `sim/run-sim.sh` brings it up and provisions the Technitium split-horizon
zone; `sim/validate-sim.sh` is the V1 gate. **RAN IT FOR REAL** on WSL2/docker
29.6.1 — full gate output:

```
-- (1) service health --   technitium/caddy/tracker/actual healthy; init-perms exit=0;
                           oauth2-proxy /ping=200 & dex discovery=200 (distroless, no HC)
-- (2) split-horizon DNS -- dig @technitium {tracker,actual,apex}.homelab.sim -> 10.99.0.10
-- (3) Caddy vhosts + auth -- tracker (internal CA) -> 302 to Dex; actual -> 401 no-auth, 200 w/ auth
-- (4) oauth2-proxy + Dex -- FULL headless login (curl cookie-jar) -> authenticated tracker 200
-- (5) multi-user isolation -- A/B see only own data; no-identity -> 403; A export = only A's items
-- (6) /api/feed round-trip -- ok=true->done, ok=false->cleared, ok=true->done
== V1 GATE: PASS (all checks green) ==
```

**Real bugs the sim caught in the wave-1 config (all FIXED in the base compose,
never before RUN):**
- **tracker healthcheck** was `/api/today` — returns **403** in multi-user mode
  (the D3 default), so the container could never report healthy. Fixed to
  `/healthz` (identity-free in both modes; matches the NagLight Dockerfile).
- **oauth2-proxy healthcheck** used `wget` but the stock image is **distroless**
  (no shell/wget) — the probe could never run and blocked Caddy. Removed the
  container healthcheck (probe `/ping` from outside) and changed Caddy's
  dependency to `service_started`.
- **technitium + actual healthchecks** used `wget`, absent from both images
  (they ship bash, not wget). Fixed to a tool-independent bash `/dev/tcp` probe.

**FLAGGED FOR PETER (a NagLight repo fix, out of this repo's scope):** the
`naglight:local` image runs as `USER tracker` (uid 1000) but the `tracker_data`
named volume initialises **root-owned**, so multi-user `mkdir /data/<sub>` fails
with EACCES and every request 500s. Correct fix = `mkdir -p /data && chown
tracker:tracker /data` before `VOLUME` in NagLight's Dockerfile. The sim
reproduces that end-state with an `init-perms` one-shot so the tracker still runs
at uid 1000 (faithful to prod) — but the real image should be fixed.

**Sim-vs-real deltas the sim cannot cover (for the hardware burn-in):** real
Google OAuth consent; publicly-trusted ACME/TLS certs; Technitium binding the
host's real `:53` (systemd-resolved owns loopback :53 on WSL, hence the bridge +
alt-port approach — a split-horizon test still runs, `dig @technitium` from the
client); the AWOW hardware. Aux containers (Kuma/Dozzle/ntfy) and ddns are
disabled in the sim (LAN_IP binds / zero external calls) — config-validated in
wave 1, out of the V1 gate scope. Full delta table in `sim/README.md`.

### DRIVER — G1 — Round 1 — 2026-07-04 (WI-10.15 Mini-serv-sim + bash backup service)
Built the **real bash backup service** at `stack/backup/` (ASSUMPTION confirmed
in use: it lives in MiniPC-Deployer as box-plumbing, not its own repo) —
`backup.sh` + `restore.sh` + `common.sh` + `backup.env.example` + systemd
`.service`/`.timer`, implementing HOMELAB_TOPOLOGY.md's six steps in pure bash
(no .bat/.ps1). Recovery MANIFEST format (the `*FilesHashTable.csv` successor):
per run `MANIFEST.tsv` (one row/set: set·source·archive·algo·archive_sha256·
files·bytes·reason) + `<set>.files.tsv` (sha256·size·mtime·relpath per file) +
the `.tar`/`.tar.zst` archive + `RUN.json`. Auto-compression-where-applicable
decides zstd-vs-plain-tar per set by already-compressed byte ratio (FileBackup
exemption, lifted to archive granularity). Hash = **sha256** (coreutils-native)
rather than FileBackup's xxHash128 — documented internal-integrity delta.

Built `sim/mini-serv-sim/`: a `dperson/samba` container (Mini-serv stand-in)
exposing three fictional committed shares — `minecraft` (Paper tree: realistic
`paper-1.20.4-435.jar`, 3 plugin jars with **parseable** `plugin.yml`,
`server.properties` with a fake rcon "secret", a `world/` tree), `satisfactory`
(save tree), and an empty writable `icedrive` offsite target — plus a privileged
`backup-runner` that cifs-mounts them and runs the REAL service.

**RAN THE FULL CYCLE + RESTORE DRILL FOR REAL** (`run-backup-sim.sh`):
```
backup.sh: minecraft -> zstd (.tar.zst, 9% already-compressed < 60% threshold), 13 files
           satisfactory -> plain .tar (98% already-compressed >= threshold), 3 files
           total 16 files / 33429 bytes; retention keep=3
step5 offsite: pushed 5 files into //mini-serv/icedrive/awow-backup/run_<ts>
step6 feed: POST /api/feed ok=true -> HTTP 200; tracker /api/today shows
            backup-files done=true (round-trip confirmed)
RESTORE DRILL: reconstruct minecraft from archive+manifest -> RESTORE OK 13/13
            byte-exact (sha256+size); diff -r vs live share IDENTICAL; delete
            plugins/ subtree, reconstruct again -> IDENTICAL (recovered); fake
            rcon.password round-tripped intact; 3 plugin jars restored.
== BACKUP LEG: PASS (all checks green) ==
```

**Real bug the sim caught + FIXED in the service:** `compression_decision`
`printf`'d without a trailing newline, so the `read` consuming it returned
nonzero and (under `set -o errtrace`) tripped the never-silent-green ERR trap —
every run failed at step 2 and correctly posted ok=false (proving the
never-silent-green path works). Fixed by emitting the trailing newline; the
next run went green end-to-end.

**Fixture shares are UP for the WI-10.16 MinecraftKeeper session.** Start them
standalone with `sim/mini-serv-sim/run-backup-sim.sh --shares-only` (needs
`sim/run-sim.sh` first for the shared `awow-sim_default` network); shares are
`//mini-serv/{minecraft,satisfactory,icedrive}`, user `awow` / `simpass`,
minecraft+satisfactory exported READ-ONLY (live-share-stays-read-only rule).

### DRIVER — G1 — Round 1 — 2026-07-04 (WI-10.18 V3 gate — ISO/VM scripts)
Built `vmtest/` (agent delivers scripts + docs; **the boot itself is Peter's** —
needs an elevated PowerShell session + the Hyper-V Windows feature, both
machine-level, neither touched here):

- **`vmtest/build-seed.sh`** — LIGHT path (default): renders the REAL
  `stack/autoinstall/user-data`+`meta-data` with SIM values (ephemeral
  ed25519 keypair, random SIM console password, unchanged storage/late-
  commands logic), copies the repo into a `deploy-payload/` tree with a SIM
  `.env` (fictional domain/OAuth client, real-shaped throwaway oauth2-proxy
  cookie secret + Technitium password, real Caddy bcrypt basic_auth hashes via
  `docker run caddy hash-password`), and burns a small `CIDATA`-labeled seed
  ISO with `genisoimage`/`xorriso`. No repack of the stock ISO — Ubuntu's
  NoCloud datasource auto-detects any attached CIDATA-labeled media (same
  mechanism `stack/README.md`'s "Second USB" already documents).
- **`vmtest/build-repacked-iso.sh`** — HEAVIER fallback: same SIM rendering,
  but bakes `autoinstall ds=nocloud;s=/cdrom/nocloud/` into a patched copy of
  the stock ISO's `/boot/grub/grub.cfg` (via `xorriso -boot_image any
  replay`, which reuses the ORIGINAL hybrid BIOS+UEFI El Torito boot catalog
  rather than hand-rebuilding one) plus embedded `/nocloud/` +
  `/deploy-payload/` — truly zero-keypress, at the cost of ~3.4GB
  copied/rewritten per build.
- **`vmtest/New-AwowVm.ps1`** / **`Remove-AwowVm.ps1`** — Hyper-V Gen2 VM
  (4 vCPU/8GB static RAM stand-in for the AK41, 64GB dynamic VHDX,
  `MicrosoftUEFICertificateAuthority` Secure Boot template for the Ubuntu
  shim, Default Switch/NAT by default with a documented External-switch
  option for real LAN exposure). Idempotent, `-WhatIf` support, elevation
  asserted at the top. **Never run** (elevation + Hyper-V are Peter's call).
- **`vmtest/README.md`** — the V3 runbook: ISO strategy write-up (why the
  light path needs one manual GRUB keypress and the heavy path doesn't, with
  the exact edit to make), the 24.04.4 download URL + SHA256 (verified for
  real, see below), Hyper-V enable steps, run order, what "success" looks
  like (tracker legitimately can't reach healthy without staging
  `naglight:local` separately — a pre-existing gap, not new; documented which
  `stack/README.md` §6 burn-in items do/don't apply in a VM), and the
  VM-vs-hardware deltas (NAT vs LAN IP, no real ACME/OAuth/DDNS, no USB
  backup drives, no thermal/storage checks).

**RAN FOR REAL (honest ledger):**
- `build-seed.sh` — run twice (fresh + idempotent re-run). Output validated:
  `user-data` parses as YAML (`python3 -c 'yaml.safe_load(...)'`), the SSH
  placeholder/password/hostname substitutions land correctly, `meta-data` gets
  a fresh `instance-id`, the SIM `.env` renders correct values (spot-checked
  `DOMAIN`, `LAN_IP`, `ACME_EMAIL`, real Caddy bcrypt hashes, cookie secret),
  `isoinfo` confirms volume label `CIDATA` and the right files at the ISO
  root. Idempotent re-run reused the SSH key + all SIM secrets unchanged
  (verified byte-identical across runs) — this caught and fixed a real bug
  (below).
- `build-repacked-iso.sh` — downloaded the real
  `ubuntu-24.04.4-live-server-amd64.iso` (SHA256
  `e907d92eeec9df64163a7e454cbc8d7755e8ddc7ed42f99dbc80c40f1a138433`, verified
  byte-for-byte against `releases.ubuntu.com/24.04/SHA256SUMS`) and ran the
  full repack. Confirmed: `/boot/grub/grub.cfg` in the output carries
  `autoinstall ds=nocloud;s=/cdrom/nocloud/` on both boot entries;
  `xorriso -report_el_torito plain` shows BOTH a BIOS and a UEFI boot image
  still present (the "replay" trick preserved the hybrid boot catalog);
  `/nocloud/{user-data,meta-data}` and `/deploy-payload/` present and correct
  inside the ISO.
- `python scripts/check.py` / `validate_config.py` still PASS unchanged (G1
  green) after adding `vmtest/`.

**NOT run (honest gap, by design/scope):** `New-AwowVm.ps1`,
`Remove-AwowVm.ps1` — need elevation + the Hyper-V feature, an agent doesn't
make that call. **No VM has been booted from either ISO.** The GRUB-edit
mechanism (light path) and the El-Torito-preserving repack (heavy path) are
verified at the ISO-structure level only, not by an actual boot.

**Real bug the smoke test caught + FIXED:** the shared secrets file
(`vmtest/.out/secrets/creds.env`) was being re-read via `. "$creds_file"`
(bash `source`) on idempotent re-runs; the SHA-512 password hash it holds
contains `$6$...` crypt syntax, which bash tried to expand as positional
parameters under `set -u`, aborting with `line 8: $6: unbound variable`.
Fixed by extracting values with `grep`/`cut` instead of sourcing. Also folded
what had been a per-call, unbounded-growth `>>` append of the Technitium
password + oauth2-proxy cookie secret into the same once-generated,
idempotently-reused block as the SSH key and console password.

**Side effect flagged for Peter (OI-6 above):** downloading + repacking the
~3.4GB ISO (even under a WSL-native path, not `/mnt/c`) grew WSL2's
`ext4.vhdx` — which itself lives on `C:` — from ~21GB free down to ~9GB, and
deleting the files afterward did **not** give the space back (a known WSL2
quirk: the sparse vhdx doesn't auto-shrink). Reclaim steps are in
`vmtest/README.md` §2.

### DRIVER — G1 — Round 1 — 2026-07-04 (Q10.9 B+ ALL-IMAGES — bake every image into the ISO)

Implemented Peter's locked **Q10.9 B+** decision (HOMELAB_RESTRUCTURE_PLAN.md):
every stack image is `docker save`d into the ISO deploy payload and `docker
load`ed at first boot, so a freshly-imaged AWOW comes up "from infancy" with
**zero registry/internet dependency for container images**, versions pinned to
exactly what the AWOW-sim validated.

**What was built:**
- `vmtest/export-images.sh` — resolves the full image set via `docker compose
  config --images` (from `docker-compose.yml` + the PINNED tags in
  `.env.example`, ntfy profile included), pulls any image not already local at
  its pinned tag, and `docker save`s each into `vmtest/.out/images/*.tar` with an
  `images.manifest.tsv` (ref/id/digest/file/bytes). Fails LOUDLY if any image is
  missing/unpullable; `naglight:local` (no registry home, Q10.2) must be
  pre-built or the script aborts — it is the one image the box can never fetch.
- **Per-image plain `.tar`, no zstd** (measured, justified): `docker save` under
  the containerd/OCI image store already writes compressed layer blobs — a 526MB
  actual-server image saves to a ~106MB tar; a zstd pass buys ~nothing and would
  add an `apt-get install zstd` dependency. Per-image (vs one combined tar) is
  composable + idempotent and gives firstboot per-image load logging + graceful
  per-image degrade. (`--zstd` remains available if ever wanted.)
- `stack/autoinstall/firstboot.sh` — new **step 3**: before `docker compose up`,
  `docker load` every tar found in `/opt/awow-core/images` (with fallbacks
  `$STACK_DIR/images`, `/cdrom/deploy-payload/images`,
  `/media/deploy-payload/images` so it works in either ISO layout). Idempotent;
  per-tar failures warn-and-continue (compose can still pull). **Graceful
  degrade:** no payload present → loud NOTICE + fall back to the pre-Q10.9
  pull-at-compose-up behaviour.
- Payload wired into **BOTH ISO paths** via a shared `stage_images_into_payload`
  in `vmtest/lib/common.sh`: it folds `vmtest/.out/images/*.tar` into
  `deploy-payload/images/` (hardlinked when the fs allows — saves ~470MB of C:,
  OI-6). The **light** path (`build-seed.sh`) burns that into the CIDATA seed ISO
  (~1MB → ~470MB); the **repacked** path (`build-repacked-iso.sh`) maps the same
  `deploy-payload/` dir into the ISO's `/deploy-payload/` (~3.4GB → ~3.9GB).
  Either way the tars land at `/opt/awow-core/images` for firstboot.

**PIN SET (`latest`/floating → concrete, Q10.9 B+).** `latest` was fine for
bring-up; B+ makes what-boots == what-was-validated, so floating tags are now
wrong. Digest = registry index digest as saved (`docker save` under the
containerd store; see `images.manifest.tsv`):

| Service | `.env` var | was | pinned | registry digest |
|---|---|---|---|---|
| technitium | `TECHNITIUM_IMAGE_TAG` | `latest` | `15.2.0` | `sha256:23d3b63d959e997800b095fe93009b3fae271b5258234ff2ade8535cb33682c8` |
| caddy | `CADDY_IMAGE_TAG` | `2-alpine` | `2.11.4-alpine` | `sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648` |
| oauth2-proxy | `OAUTH2_PROXY_IMAGE_TAG` | `v7.6.0` | `v7.6.0` (already pinned) | `sha256:dcb6ff8dd21bf3058f6a22c6fa385fa5b897a9cd3914c88a2cc2bb0a85f8065d` |
| tracker | `TRACKER_IMAGE_TAG` | `local` | `local` (local build, no registry) | id `sha256:9a573d4367032a4d872736718f5dd68872bcf5b1d01d727359ff36413f8f4112` |
| actual | `ACTUAL_IMAGE_TAG` | `latest` | `26.7.0` | `sha256:e18b7fbfec6157a368fad4146563f397502e9da70a120aeaeac63b4977405d1c` |
| ddns | `DDNS_IMAGE_TAG` | `latest` | `v2.10.0` | `sha256:3e2aa558946b5a293def4d73008fa4651c072b2c12932cecd02126fb23979831` |
| uptime-kuma | `UPTIMEKUMA_IMAGE_TAG` | `1` | `1.23.17` | `sha256:3d632903e6af34139a37f18055c4f1bfd9b7205ae1138f1e5e8940ddc1d176f9` |
| dozzle | `DOZZLE_IMAGE_TAG` | `v8` | `v8.14.12` | `sha256:0df89c904da71e94a0c9ed3c89a890f01488321b5f10ac1e0c0bedcead9af6e4` |
| ntfy | `NTFY_IMAGE_TAG` | `latest` | `v2.25.0` | `sha256:cfbbb1bac9196cb711e29ef0ac4adaeb033be6235f1df857705dc39c14384a1d` |

**How the concrete tags were derived (honest):** the 5 **core** images ran in
the V1 AWOW-sim; each concrete pin was verified to be the SAME image V1 ran —
technitium `latest`'s linux/amd64 sub-manifest is byte-identical to `15.2.0`'s
(`sha256:85c2cfd4…`), caddy `2-alpine` and actual `latest` share their exact
index digest with `2.11.4-alpine` / `26.7.0`, oauth2-proxy was already `v7.6.0`,
naglight is the locally-built `naglight:local`. The 4 **aux** images (ddns,
uptime-kuma, dozzle, ntfy) were **disabled in the V1 sim** (LAN_IP binds / zero
external calls), so they have no sim-validated version — they were pinned to
their current newest concrete releases and exported now; **they are first
validated at V3 boot / hardware burn-in.** (ddns note: Docker Hub's `latest`
tag is a differently-built multi-arch index than `v2.10.0`; the named release
`v2.10.0` was chosen for reproducibility.)

**RAN FOR REAL (WSL2 / docker 29.6.1):**
- `export-images.sh` end-to-end → pulled the 4 aux images at their pinned tags,
  re-tagged the 3 core floating→concrete (layers already present, near-instant),
  saved all 9. **Total payload = 469 MB** across 9 tars (well under Peter's
  ~1–2GB estimate). Manifest written.
- `docker load` of all 9 tars → each restored to its exact pinned `repo:tag`
  and re-loading is an idempotent no-op (proves firstboot step 3's guarantee:
  compose finds every image locally, no pull).
- `docker compose config --images` with the pinned `.env.example` → lists
  exactly the 9 concrete pinned refs (what compose asks for == what's baked).
- `build-seed.sh` (light path) with the payload → 471 MB seed ISO, volume id
  `CIDATA`; `isoinfo` confirms all 9 tars + manifest at
  `/deploy-payload/images/`. Hardlink staging worked (link count 2 — no C: bloat).
- Repacked path: the Q10.9 addition (images folded into `/deploy-payload/`) was
  verified via the exact `xorriso -map <dir> /deploy-payload` codepath —
  `xorriso -lsl` confirms all 9 tars land at `/deploy-payload/images/`. The full
  3.4GB stock-ISO repack + El-Torito/GRUB handling was already verified in
  WI-10.18; not re-downloaded (proportionality; C: had 16GB free but the
  mechanism + trivial one-line addition were already covered).
- `scripts/check.py` + `validate_config.py` → **PASS** (G1 green; 35 compose
  vars covered).

**AWAITS V3 (Peter's boot, unchanged):** the actual first-boot `docker load` +
`compose up` run inside a booted VM/hardware. No VM has been booted. Everything
above is the pre-boot smoke test the dev PC can run headlessly.

**OI-6 update:** C: now shows ~16GB free (was ~9GB at the snapshot); the image
tars + seed ISO were written to `vmtest/.out` on `/mnt/c` (real C: via 9p), which
does **not** grow the WSL `ext4.vhdx`. Only the 4 aux-image pulls (~200MB into
the docker store) touched the vhdx.

### DRIVER — G1 — Round 1 — 2026-07-04 (WI-10.10 DRIVE POWER DESIGN — dynamic standby)

Implemented Peter's ratified DRIVE POWER DESIGN in the bash backup service. The
backup drives are the box's biggest electrical lever (5–8 W each spinning ≈ the
whole CPU), so the policy is **dynamic standby**, two pieces:

**What was built:**
- **Boot-time default standby** — a per-boot oneshot
  `stack/backup/backup-standby.service` + `backup-standby.sh` applies a
  conservative `hdparm -S` spin-down timeout to each configured backup drive
  (`hdparm -S` does not persist across power cycles, so it re-applies every boot,
  like `powertune.service`). Shipped/enabled via autoinstall `late-commands`
  exactly like powertune (runs in place from the stack dir so it can source
  `common.sh`, matching `awow-backup.service`). Added `hdparm` to the autoinstall
  packages list (NOT guaranteed on Ubuntu Server).
- **Dynamic hold in the run** — `backup.sh` disables standby (`hdparm -S 0`) on
  its target drive(s) at run start and **restores the configured timeout on any
  exit via an `EXIT` trap** — fires on success, on the ERR-trap's `exit 1`, on a
  `die`, and on interrupt. The EXIT trap fires AFTER the ERR trap, so it never
  disturbs the never-silent-green `ok=false` reporting path; it only re-arms the
  drives. Prevents both wear modes: no start/stop churn during long no-write
  phases (hashing/verify), no aggressive-timeout cycling.
- **Knobs** (`backup.env.example`, placeholders only): `BACKUP_DRIVE_DEVICES`
  (space-separated `/dev/disk/by-id/...` paths — **by-id, never sdX** which
  renumbers) and `BACKUP_DRIVE_STANDBY` (default `241` = 30 min). **Empty device
  list = the whole feature is a clean no-op.** The confusing `hdparm -S` encoding
  (`1..240` = n×5 s so 240 = 20 min; `241..251` = (n−240)×30 min so 241 = 30 min)
  is documented once in `common.sh` and in `backup.env.example`.
- **HARD RULE honored:** power management NEVER fails a backup — missing `hdparm`,
  absent device path, or an enclosure rejecting the command → logged WARNING and
  continue. `drive_standby_set` always returns 0 so it composes with the ERR-trap
  machinery without tripping it. USB-enclosure caveat noted in comments +
  `backup.env.example`; per-drive verification is a burn-in step (`hdparm -C`).

**RAN FOR REAL (WSL2 / docker 29.6.1) — the CALL CONTRACT proven with mock shims**
(platters can't spin in a container, but the sequence of `hdparm` calls can be
asserted). New `sim/mini-serv-sim/run-drivepower-sim.sh` puts a mock `hdparm`
(logs every invocation) and mock `curl` (captures each NagLight POST body) on
PATH in the runner, uses fake by-id device files, and runs REAL backup cycles
against the Samba fixtures. **All four assertions GREEN:**
- **(a)** `-S 0` (standby disabled) issued to **each** configured device at run
  start.
- **(b)** the configured timeout (`-S 241`) re-issued to **each** device on
  normal exit (exactly 4 calls; all disables precede all restores).
- **(c)** on a FORCED mid-run failure (mock `rsync` exits 1) the restore **still
  fires** via the EXIT trap AND the run **still posts `ok=false`** (mock curl
  captured `"ok":false`; `on_err` ran → `BACKUP FAILED` + `RUN.json=failed`;
  nonzero exit).
- **(d)** with **no devices** configured: **zero** `hdparm` calls and an
  unchanged green cycle.

Also validated the boot oneshot directly (`docker run` throwaway, 5 cases):
`-S 241`→"30min" and `-S 240`→"1200s" (=20 min) encoding correct; empty list =
no-op; absent device = WARN + skip + exit 0; missing `hdparm` = WARN + exit 0
(never fails boot).

**No regression:** re-ran `run-backup-sim.sh` end-to-end — full six-step cycle +
restore drill **PASS** (backup.env.sim has `BACKUP_DRIVE_DEVICES=""`, so the
standard cycle logs zero drive-power lines — the clean no-op path). `bash -n`
clean on all scripts; `scripts/check.py` (G1) + `validate_config.py` **PASS**
(the file-presence check now also asserts the powertune + `backup-standby`
unit/script exist, and `user-data` still parses as valid `#cloud-config` YAML).

**HARDWARE-ONLY REMAINDER (honest):** whether the actual USB-SATA backup
enclosure **honors** `hdparm` standby at all — many bridge chips swallow/fake the
command — cannot be known in sim; it is a per-drive **burn-in** check
(`hdparm -C /dev/disk/by-id/...` to read active/idle vs standby; `hdparm -y` to
force a spin-down and confirm). The real electrical/spindle-wear benefit is
likewise a hardware measurement. The sim proves the *call contract and its
failure-path composition*, not the drive's physical response.
