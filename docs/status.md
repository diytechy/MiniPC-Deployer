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
  - **Needs the Owner** _(ratification / manual steps — dial=HIGH)_:
    - OI-1 — **Google OAuth client** must be created in Google Cloud (needs
      the Owner's account); redirect URI to register:
      `https://tracker.<domain>/oauth2/callback` →
      [stack/.env.example](../stack/.env.example)
    - OI-2 — **Reimage-over-LAN ladder** is HIGH-RISK; the memo presents options
      with checkboxes. Nothing destructive is implemented until the Owner checks one
      → [REMOTE_MANAGEMENT.md](../REMOTE_MANAGEMENT.md)
    - OI-3 — **Push** each commit (agents lack the SSH key) →
      this repo
    - OI-5 — **Run the V3 gate** (WI-10.18): enable Hyper-V (elevated,
      machine-level, needs a reboot), create the VM, do the one-time GRUB
      edit (light path), and confirm compose-up →
      [vmtest/README.md](../vmtest/README.md). Nobody has booted a VM from
      this yet — scripts delivered + partially smoke-tested, boot itself is
      the Owner's step (elevation + Hyper-V).
    - OI-6 — **C: free space is tight (~9GB)** after this session's ISO
      download/repack smoke test — WSL2's `ext4.vhdx` grew and does not
      auto-shrink on file deletion (see vmtest/README.md §2 for the
      reclaim-it steps: `wsl --shutdown` + `Optimize-VHD`/`diskpart compact`,
      elevated). Not urgent, but worth doing before further large downloads.
    - OI-7 — **Tier-2 catalog ratifications (2026-07-10):** (a) confirm the
      tier-2-NOT-baked ISO boundary (profiles excluded from the payload unless
      `EXTRA_PROFILES` at export) as the standing Q10.9 B+ interpretation;
      (b) decide where `MEDIA_ROOT` physically lives (must NOT be the WI-10.10
      backup drives); (c) the oauth2-proxy **security pin bump** v7.6.0→v7.15.2
      needs a V1 sim re-run + re-export before any real flash →
      [stack/README.md §9](../stack/README.md)
    - OI-10 — **Disk encryption decision (2026-07-11):** the AWOW's disk is
      UNENCRYPTED — physical theft/disposal exposes `.env` secrets + the
      finance volumes. Proposed: autoinstall LUKS (Subiquity supports
      `storage: layout: {name: lvm, password: …}`) + unattended unlock via
      TPM2 enroll at first boot **if the AK41 BIOS exposes Intel PTT (Owner:
      check BIOS)**, else dropbear-initramfs (SSH unlock over LAN — fits
      headless). Touches the autoinstall `storage:` layout = the
      REMOTE_MANAGEMENT **hard line**: not implemented until the Owner ratifies.
      Companion decision: LUKS on the backup drives too (finance snapshots
      land there in plaintext otherwise).
    - OI-8 — **Backup gap — RESOLVED in-repo 2026-07-10 (SN-010/SR-013,
      Owner-ratified single-table design):** `BACKUP_SOURCES` now accepts
      `volume:VOL[@CONTAINER]` and `path:/dir` specs; sim-proven
      (`run-volume-sim.sh` a–d GREEN + full `run-backup-sim.sh` regression
      GREEN). **Remaining for the Owner:** uncomment the volume lines in the real
      `/etc/homehub-backup/backup.env` (+ add `actual tracker` to `OFFSITE_SETS`)
      when configuring the box — they ship commented in `backup.env.example`.
    - OI-11 — **Offsite leg — the Owner CORRECTED the model on 2026-07-29: the
      backup service has NO offsite step in the target state.** The IceDrive
      client (SN-012/SR-015 opt-in desktop session) is pointed **directly at
      chosen library paths in its own GUI** and syncs them itself; the service
      stages, copies and prunes nothing for it. `OFFSITE_ENABLED=false` is now
      the documented target state (`backup.env.example`), the step-5 code is
      kept working as legacy, and the pipeline instead **ingests** network
      shares into the library so there is one current copy for the client to
      sync (see the 2026-07-29 ingest entry). **Mini-serv leaves the offsite
      path entirely** (unchanged from the 2026-07-25 ratification).
      **What this makes MOOT:** the "local-path target" build half done earlier
      on 2026-07-29 (`OFFSITE_PATH`) — it stays in the tree as harmless legacy,
      needs no sim leg, and there is no `OFFSITE_PATH` for the Owner to set;
      likewise the old OI-11(a) "a local-target sim leg is owed".
      **Still outstanding for the Owner:** stand the IceDrive client up on-box
      per `stack/remote-ui/README.md` and create its sync pairs against the
      library paths named in `Personal\deploy\storage-map.md` §4e — never one
      holding raw finance data. **Two consequences to accept:** the client is a
      GUI app, so **sync is down after every reboot until a session is opened**
      (SR-015), and with no offsite step the backup's NagLight report **cannot
      see** a stale cloud copy at all — a green backup says nothing about
      IceDrive.
    - OI-14 — **Ingest + exclusions need the Owner's real values
      (2026-07-29, OWNER):** the ratified INGEST step is inert until
      `INGEST_SOURCES` in the real `/etc/homehub-backup/backup.env` names the real
      share and the real library destination, and the `BACKUP_SOURCES` `path:`
      entry points at that library folder — the repo ships the fictional
      `mini-serv` / `/srv/library/NonDocs/MiniServ` placeholders only (SN-007).
      The **authoritative** library paths come from
      `Personal\deploy\storage-map.md`, not from the example. Same for
      exclusions: `BACKUP_EXCLUDE="*.bak"` ships as the Owner's stated default,
      but the **named very-large folders** he wants skipped are per-set
      (`name.exclude=` lines) and only he knows their names. Note the mirror
      contract before filling it in: `rsync -a --delete` means a file deleted on
      the share is deleted from the library copy on the next run (history lives
      in the dated run snapshots, `BACKUP_KEEP`).
    - OI-13 — **Wake-on-LAN needs the real values + the Windows-side
      settings (2026-07-29, OWNER):** the backup now wakes the sleeping game box
      before pulling and fails the run loudly on a wake timeout, but it is OFF
      until `BACKUP_WAKE_MAC` is filled in (with `BACKUP_WAKE_HOST` /
      `BACKUP_WAKE_TIMEOUT`) in the real `/etc/homehub-backup/backup.env` — the
      repo ships placeholders only (SN-007). On the **Windows** side: enable
      "Wake on Magic Packet" + "Allow this device to wake the computer" on the
      WIRED adapter and turn **Fast Startup OFF** (hybrid shutdown leaves the
      NIC unable to wake). Worth verifying at the same time whether the AWOW's
      `/dev/udp` broadcast is accepted or whether `apt-get install wakeonlan` is
      needed for the fallback — bash cannot set `SO_BROADCAST`, and the WSL
      kernel used for this session's testing REFUSED it.
    - OI-12 — **RATIFIED 2026-07-29 (the Owner) — the wall-panel image lane is
      GO**, in the belt-and-braces variant he chose: the kiosk auth site on a
      **LAN-bound alternate port the router never forwards, PLUS the `/32`
      allow-list** (not either one alone). Proposed 2026-07-25; brief:
      `Personal\homelab\OFFICEWALL_BOOTSTRAP.md`. **BUILT 2026-07-29** — see the
      audit entry below. The D-W0 split holds: the *image* is now a second target
      in this repo (`stack/autoinstall/wall/`), the panel's *shell app* stays in
      `OfficeWallNaglight` and is consumed as a built artifact (IF-005), so the
      "no product source" constraint is intact. What landed, against the three
      things this item said would:
      (a) the kiosk site — DONE (SR-016), and the forged-header strip + the 403
      default are **sim-proven** (`validate-sim.sh` checks 7-8). The **off-LAN**
      403 from a real WAN vantage remains the documented hardware test;
      (b) the graphical autoinstall variant + its own spine rows — DONE
      (SR-017, SN-013 per OI-12b: lighter gates, the panel is disposable);
      (c) `navidrome` is **no longer a gate** — D-W8's rider has the panel playing
      from its local synced copy, so streaming is optional, and `MEDIA_ROOT`'s
      ratification (2026-07-29) closed OI-7(b) anyway. The Caddyfile ships the
      `/music` proxy commented.
      **Still needing the Owner:** the values (`WALL_HOST`, `PANEL_IP`,
      `PANEL_USER_SUB`, `WIFI_*` — all T1/T3, placeholders only in this repo per
      SN-007), the panel's **DHCP reservation on its hardware MAC**, the
      `EXTRA_SUBDOMAINS` label, registering the wall templates in
      `Personal\homelab\deploy\FieldSchema.psd1` (see the audit entry for the
      exact snippet and two cross-repo consequences), and the whole of
      `stack/autoinstall/wall/WALL-BURN-IN.md`.
    - OI-15 — **RESOLVED 2026-07-29 (the Owner), and BUILT the same evening.**
      The ruling: *panel media is an AWOW network share; the **panel PULLS** —
      once after boot and on demand via a dedicated SSH-invocable command — with
      **MIRROR semantics** (`--delete`: content removed from the LAN source
      disappears from the panel cache); `/media/*` is then served **panel-locally**
      by the shell's Electron host.* So the question the item asked ("which origin
      serves `/media/*`?") is answered *panel-side*, and this repo owes the pull,
      not a route: the `TODO(OI-15)` stub is gone from `stack/caddy/Caddyfile`,
      replaced by a one-line statement that the kiosk site serves no `/media`
      route. Built here: `stack/autoinstall/wall/wall-sync.{sh,service}` (cifs
      mount → `rsync -a --delete` of ONLY `Music/` + `FrameVideos/` →unmount) plus
      `wall-media-manifest.py`, which emits the shell's two contracts
      (`music/index.json`, `frame/playlist.json`) into the cache as the sync's
      post-step. The dedicated command is `sudo systemctl start
      wall-sync.service`; there is deliberately **no timer**. See the audit entry
      below for what ran for real. **Still owed by the OTHER side** (not this
      repo): the Electron host actually mapping `/media/*` onto the cache dir —
      OfficeWallNaglight's half of the same ruling.
    - OI-16 — **RESOLVED 2026-07-29 (evening, the Owner): option (a) — the
      wall-sync also fires on RESUME from suspend.** The panel suspends nightly
      (`SLEEP_MODE=suspend`) and resumes without booting, so boot-only sync went
      stale by design; now every wake behaves like a boot for freshness. Built
      the same evening: `stack/autoinstall/wall/wall-sync-resume.service` — the
      standard systemd resume hook (`After=suspend.target` +
      `WantedBy=suspend.target`, so it starts when the suspend transaction
      completes, i.e. at WAKE) running `systemctl start --no-block
      wall-sync.service`. `--no-block` is what keeps the wake imperceptible: the
      hook only enqueues and exits, and the sync runs detached with its own
      journal/timeout/failure status. Wi-Fi wrinkle handled in `wall-sync.sh`:
      `network-online.target` is not re-evaluated on resume, so the script does
      a bounded non-fatal `nm-online` wait (30 s) before mounting; a still-down
      network fails the mount loudly and the retry is the on-demand command.
      Zero new knobs. See the audit entry below; the real suspend→wake firing is
      hardware-only (WALL-BURN-IN.md §8).
  - **In flight** _(driver; no approval needed)_:
    - OI-4 — layering WI-10.2/10.11/10.12 onto the migrated base →
      [stack/docker-compose.yml](../stack/docker-compose.yml)
    - OI-9 — **RESOLVED 2026-07-29.** (Found 2026-07-10 during the SR-013 red
      run: a `die` — e.g. a cifs mount failure — exited 1 WITHOUT posting
      `ok=false`, so only ERR-trap failures fed the tracker.) `die` now calls a
      registered reporter (`DIE_REPORTER`) before exiting; `backup.sh` registers
      `report_failure`, the single idempotent failure path shared with the ERR
      trap. A failing report is swallowed into a WARNING so it can never mask
      the original error, and an unset `DIE_REPORTER` (restore.sh,
      backup-standby.sh) is a clean no-op. Verified for real on four die paths
      — see the 2026-07-29 audit entry. Still owed: an assertion inside the
      committed sim legs (they exercise ERR-trap failures, not `die`).
- **Assumptions (unattended):** see the Assumptions log below.
- **Next action:** the Owner reviews + pushes; **for the newly-built wall lane
  (OI-12): fills in `WALL_HOST`/`PANEL_IP`/`PANEL_USER_SUB`/`WIFI_*`, gives the
  panel a DHCP reservation on its hardware MAC, confirms the router forwards
  :80/:443 and NOTHING else, registers the two wall templates in
  `Personal\homelab\deploy\FieldSchema.psd1`, and works
  `stack/autoinstall/wall/WALL-BURN-IN.md` on a desk before the panel is
  mounted** — which now includes filling in `MEDIA_SHARE_UNC` + the share
  credentials and working `WALL-BURN-IN.md` §8, since an unconfigured panel fails
  `wall-sync.service` on every boot by design — §8 now also carries the OI-16a
  hardware proof (suspend, wake, watch `journalctl -u wall-sync-resume -b` show
  the sync fired); fills in the wake values + the
  Windows-side WoL settings (OI-13) and the real ingest/exclusion values
  (OI-14); points the on-box IceDrive client at the chosen library paths
  (OI-11); creates the Google OAuth client
  (OI-1); runs the V3 boot (OI-5); ratifies the tier-2 decisions (OI-7) —
  then the "V3.5 dress rehearsal" (real secrets in the VM: External vSwitch,
  TLS decision, backup VHDX) discussed 2026-07-10 turns the sim-GREENs into
  real evidence.
- **UPDATE 2026-07-03 (WI-10.13):** the "no Docker on the dev machine" constraint
  above is now LIFTED — WSL2 + Ubuntu 24.04 + docker-ce is installed on the dev
  PC (Docker Desktop explicitly NOT installed, per the Owner's pick). `naglight:local`
  now builds for real and `docker compose config` resolves this stack's full
  compose file. See audit log entry below for versions/detail. Wave 2 (V1
  homehub-sim, WI-10.14/10.15) is now unblocked.

## Scope (restated from the brief)

- **Goal:** the deploy repo for the headless AWOW AK41 always-on box — an
  unattended, self-healing Docker stack (DNS, reverse proxy + TLS, the NagLight
  tracker behind Google sign-in, Actual Budget, LAN observability) plus an
  Ubuntu autoinstall image and full LAN remote management. **Config only** — app
  code lives in NagLight / Finance-Auditor / MinecraftKeeper.
- **Stakeholders / end user(s):** the Owner (homelab operator); the tracker's hosted
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
    the WI-10.14 homehub-sim harness.
  - **Public-facing repo (Q10.6):** only `*.example` templates tracked; no real
    secret/hash/email/LAN detail/personal name. Local commit identity pinned to
    `diytechy <diytechy@users.noreply.github.com>`.
  - **Kit:** minimum profile, decision dial **HIGH** (secrets-adjacent infra).
  - **No product source in this repo** → the Python `ruff`/`pytest`/arch-map
    steps are dropped (not left passing vacuously, ADOPTING.md §3). The
    product-layer check is `scripts/validate_config.py`.
- **Non-goals:** a container registry / CI publishing (deferred, Q10.2 — local
  builds for now); the NagLight app code and its multi-user engine (NagLight
  repo, WI-10.4/10.5); the secret-handoff script (WI-10.3, the Owner ratifies).
- **Definition of done:** the repo's G1 gate is green (`check.py`), `.env.example`
  enumerates every knob, config coverage validates, and the honest validation
  ledger records what remains PENDING a Docker host.

## Honest validation ledger (WI-4.7 note carried forward; updated 2026-07-11)

| Check | State |
|---|---|
| `docker compose config` (core + all tier-2 profiles) | PASS (WSL docker-ce; core = the original 8 services with no profiles) |
| Live bring-up + curl health + `dig` + OAuth round-trip + tear-down | **GREEN in the V1 sim** (vs Dex/internal-CA/fixtures — `validate-sim.sh` 6 checks); **real-Google/real-TLS/host-:53 PENDING V3 boot + hardware** |
| Backup pipeline (cifs + offsite + feed + restore drill) | GREEN (`run-backup-sim.sh`, re-run 2026-07-29 after the ingest/exclusion change); drive-power + volume-source call contracts GREEN (mock-shim legs) — drive spin-down physics + real-docker volume copy are V3/burn-in |
| **INGEST step (library mirror) + EXCLUSIONS** | **GREEN — committed sim leg `run-ingest-sim.sh` (2026-07-29), 28 checks over the REAL cifs path:** mirror byte-identical to the live share + restore byte-equal, `--delete` deletion propagation asserted, global+per-set patterns kept out of archive AND `files.tsv` while every excluded path is named in the log/`<set>.excluded.log`/MANIFEST, empty-share refusal + its override, 3 loud config failures each posting `ok=false`. Untested by anything: a real Windows share as the ingest source, and real library-scale volumes (V3/burn-in) |
| Wake-on-LAN pre-step + OI-9 `die` reporting | **Exercised for real on WSL2 (2026-07-29)** — magic-packet bytes, probe, timeout-dies-loudly, `die` paths posting `ok=false` — the wake half from a THROWAWAY harness, **not a committed sim leg**; the `die`-reports-`ok=false` contract is now asserted for real in `run-ingest-sim.sh` (e1–e3, plus the empty-share refusal). A real magic packet has never woken a real box (V3/hardware). `OFFSITE_PATH` needs no leg — the offsite step is retired (OI-11) |
| **Wall kiosk site (SR-016) — the identity swap + the 403 default** | **GREEN — committed sim legs, `validate-sim.sh` checks 7-8 (2026-07-29):** a FORGED `X-Forwarded-User` arriving from the panel's `/32` reaches the tracker as `PANEL_USER_SUB` (read back off `/api/export`'s filename, so the identity the tracker actually saw is asserted, not inspected); the panel's `/api/today` is 200 and `/` serves the shell build without swallowing `/api/*`; the same two requests from a NON-panel source address get 403 with **Caddy's own body**, proving refusal at the edge rather than at the tracker. Untested by anything below hardware: the **off-LAN** 403 from a real WAN vantage, and whether Docker's port publish preserves the panel's source IP on the real box (it fails CLOSED if not) |
| **Wall panel image (SR-017)** | **CONFIG-LEVEL ONLY, and that is all it claims.** A throwaway container harness ran `wall-firstboot.sh` twice against a bare `ubuntu:24.04` root with stub `systemctl`/`udevadm`/`netplan` — 27 assertions PASS on what it writes (lid conf, iio mask, the udev rule from both piped names + its re-enable hint, NM powersave/MAC pinning, netplan rendered 0600 with no `@@TOKEN@@` left, both timers re-rendered from a changed schedule, tty1-only autologin, mem_sleep reporting) — **not a committed sim leg.** NOTHING physical has run: no graphical session, no `cage`, no Wi-Fi association, no suspend/resume, no quirk verified against the actual panel. `WALL-BURN-IN.md` is the list |
| **Wall media pull + manifests (OI-15)** | **Exercised FOR REAL on WSL2 Ubuntu (2026-07-29) — 67 assertions PASS from a THROWAWAY harness, not a committed sim leg.** The real `wall-sync.sh` ran via its supported `MEDIA_SOURCE_OVERRIDE` bench hook (a local fixture library instead of a cifs mount; everything after the mount is the production path): first sync, idempotent re-run, `--delete` propagation, the empty-subtree and missing-subtree REFUSALS with the cache proven untouched, the `WALL_SYNC_ALLOW_EMPTY` override clearing the cache, a non-UTF-8 filename skipped-and-counted, and 5 loud config guards. Both manifests pass `python3 -m json.tool`, and the emitted `index.json` was fed to **OfficeWallNaglight's real `normalizeManifest()` under node** (20 more assertions: stations, once-only URL encoding, quotes/`&`/`#`/non-ASCII round-tripping). **NOT tested by anything:** the cifs mount itself, Wi-Fi, library-scale volumes, the kiosk user reading the cache on a real box, and the *other* half of the ruling (the Electron host serving `/media/*`) — `WALL-BURN-IN.md` §8 is the list |
| Q10.9 B+ image payload: `export-images.sh` save + `docker load` all 9 | PASS (WSL; loads idempotent) — first-boot load-at-VM awaits V3; **oauth2-proxy v7.15.2 pin bump needs a re-export + sim re-run (OI-7c)** |
| Tier-2 pins exist on their registries (`docker manifest inspect`) | PASS — but tier-2 services have never been STARTED anywhere (enable-time validation, stack/README §9) |
| Shell scripts `bash -n` | PASS |
| `docker-compose.yml`, `meta-data`, `user-data` YAML parse | PASS (PyYAML) |
| Every compose `${VAR}` has an `.env.example` key | PASS (`validate_config.py`) |
| Every Caddy `{$VAR}` passed by the caddy service env | PASS |
| Bind-mount sources + autoinstall-referenced files exist | PASS |
| `check.py` (G1: config-validate + registry-integrity + doc-navigability) | PASS |

What only V3/hardware can still prove: real Google consent, publicly-trusted
ACME certs, Technitium on the host's real `:53`, the `extra_hosts` dns.<domain>
fix, drive spin-down physics, thermals — then the burn-in checklist
(stack/README §6) signs the box off. For the **panel**, add: the off-LAN 403, the
graphical session, Wi-Fi, S3 suspend/resume, and all six hardware quirks
(`stack/autoinstall/wall/WALL-BURN-IN.md`).

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

### Assumptions log (unattended, dial=HIGH — the Owner to confirm/revert)
- A1 — Layout: migrated `life-tracker/deploy/*` under `stack/` at the repo root
  (kept the kit's `docs/`/`scripts/` roots). On-box path renamed `deploy/` →
  `stack/` under `/opt/homehub/`; the box hostname/opt-dir keep the `homehub`
  name (faithful to the source; the Owner knows it).
- A2 — This repo is treated as gate **G1** (requirements-agreed) — config is the
  deliverable, there is no compiled source to carry to G2/G3. The Python
  product/arch-map steps are dropped, not left vacuous.
- A3 — oauth2-proxy image pinned to `v7.6.0`, Google provider, allow-list via
  `authenticated-emails.txt` (Q10.5). Redirect URI `…/oauth2/callback`.
- A4 — SSH is **key-only** by default in the autoinstall (locked password),
  per WI-10.12 "key-only auth". Cockpit installed but **not** proxied publicly.
- A5 — Kept the `TRACKER_DATA_REMOTE` clone/pull entrypoint behaviour from the
  source; multi-user (D3) sets it blank + `TRACKER_COMMIT=false` (documented).
- A6 — SN-012/SR-015 shape (2026-07-20, the Owner asked for "opt-in light UI +
  SN-001 rescope"; details decided unattended): xrdp + **minimal** XFCE
  (`--no-install-recommends`, no desktop meta-package) rather than a full DE
  or VNC; the script never downloads the AppImage (vendor URL churn on a
  public repo — the Owner scp's it, `ICEDRIVE_APPIMAGE=` path knob); sync-resume
  is the documented post-reboot one-RDP-touch (NO autologin/virtual-display
  hack — that would fake self-healing the vendor app can't honestly offer);
  priority C, Verification=Inspection, no sim leg (a host-level GUI can't be
  exercised in the compose sim; the V3.5 rehearsal VM is where it could be
  tried for real). Revert any of these at the next gate if wrong.
- A7 — Wake/offsite shape (2026-07-29; the decisions themselves were ratified,
  these mechanics were not): the wake probe targets **tcp/445** because SMB is
  the port the pull actually needs — "awake" is defined as "can serve the
  share", not "answers ping"; `BACKUP_WAKE_TIMEOUT` defaults to **120 s** and
  the packet is re-sent every 15 s while waiting (a sleeping NIC can miss one);
  the timeout is a **budget, not a deadline** — a run can overshoot it by up to
  one probe+sleep cycle (~5 s); two OPTIONAL knobs beyond the three asked for
  (`BACKUP_WAKE_BROADCAST`, `BACKUP_WAKE_IFACE`) exist only because the
  no-dependency `/dev/udp` send cannot set `SO_BROADCAST` and the fallbacks
  need somewhere to read a subnet/interface from; `OFFSITE_PATH` must **already
  exist** (the run fails rather than creating it — a typo'd path would
  otherwise report green with the files where nothing syncs). Revert any of
  these at the next gate if wrong.
- A8 — Ingest/exclusion shape (2026-07-29; the two steps themselves were
  ratified, these mechanics were not): the ingest table is its own knob
  (`INGEST_SOURCES`) rather than a fourth `BACKUP_SOURCES` spec kind, because an
  ingest entry needs a DESTINATION and is not a backup set; the grammar is
  `name=//host/share -> /abs/dest` with the arrow, and both a non-UNC source and
  a relative destination are rejected; the destination **leaf** is created on
  first ingest but a **missing parent is fatal** (that is a typo or an unmounted
  library filesystem); a share that mounts but holds **no files** while the
  library copy does **refuses to mirror** — new knob `INGEST_ALLOW_EMPTY=false`
  is the explicit override (this is the one guard that exists purely because
  `--delete` is irreversible); `--dry-run` passes `--dry-run` to the mirror too;
  ingest reuses the existing wake pre-step rather than owning a second one, and
  exclusions deliberately do **not** apply to ingest (they filter the BACKUP,
  not the library). Per-set exclusions are `name.exclude=` lines **inside the
  one `BACKUP_SOURCES` table** (one table, one place to look — the SR-013
  doctrine), which costs one rule: a set name may not end in `.exclude`; a
  `name.exclude=` line naming a set that does not exist **fails the run**;
  patterns go to rsync (saves the copy) AND tar (the archive-level promise), and
  visibility is implemented with rsync's own `--debug=FILTER` decisions
  (`<set>.excluded.log`) plus a new `excludes` MANIFEST column — the column is
  additive, so runs written before it restore unchanged. Revert any of these at
  the next gate if wrong.

- A9 — Wall-lane shape (2026-07-29; OI-12 and the LAN-port+/32 variant were
  ratified, these mechanics were not): `WALL_PORT` defaults to **8443** and is a
  **T0** knob (no FieldSchema entry — the default is the public value); the site
  address carries no `bind` directive because `LAN_IP` is not an address the caddy
  container owns, so the LAN-binding is the compose **publish** instead; the 403
  body is the distinct string `wall: panel only` **specifically so the sim can tell
  an edge refusal from the tracker's own no-identity 403**; the shell's document
  root lives at `stack/wall-shell/` (i.e. `/opt/homehub/stack/wall-shell` on the
  box) rather than a sibling of the stack dir, so the existing bind-mount coverage
  check applies to it; `/api/*` is the only proxied prefix (`/drill` is left to the
  shell, which renders its own from `items[]`); the panel's payload lands at
  **`/opt/wall-panel/`** and its env file at `/etc/wall-panel/wall.env` (0600),
  mirroring the AWOW's layout without sharing it; `PANEL_USER_SUB` is deliberately
  **absent** from `wall.env.example` (the panel never learns its own identity — the
  site injects it) and `NAVIDROME_*`/`PANDORA_*` ship **commented**, because
  un-commenting them makes the household emitter demand store keys for an optional
  feature; the kiosk session is reached by a **tty1 autologin + profile hook**
  rather than a system unit, because `cage` needs a logind seat (with `seatd`
  installed as the documented fallback); `SLEEP_RTC_WAKE` is a new knob beyond the
  ratified two, and if the RTC alarm cannot be armed the panel **refuses to
  suspend** and degrades to backlight-off for that window (a reachable panel beats
  a dark one); `mem_sleep_default=deep` is **reported, never silently written** to
  the kernel cmdline; the quirk-3 udev rule is generated from a **pipe-separated**
  knob and is not written at all when that knob is empty. Revert any of these at
  the next gate if wrong.

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
OWNER MANUAL STEP (Google OAuth client) in .env.example + stack/README. Redirect
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
no PXE) is implemented until the Owner checks a box.

<!-- agent-setup --> Agent setup (2026-07-03): agents=`claude`; skills materialized: downstream-resync, gate-advance, registry-hygiene. AGENTS.md remains the canonical, agent-neutral guide (skills are opt-in accelerators, not a process gate).

### DRIVER — G1 — Round 1 — 2026-07-03 (WI-10.13 dev-PC container runtime)
The Owner picked **(a) WSL2 + docker engine inside Ubuntu**, explicitly over Docker
Desktop (not installed; no other tooling touched). This closes Wave 1's
unverified-image-build honesty gap for real.

**Installed on the dev PC (machine-level, Owner-consented):**
- WSL2 itself was already enabled/functional (a pre-existing
  `podman-machine-default` WSL2 distro was running) — no VirtualMachinePlatform
  enable + reboot was needed.
- `Ubuntu` distro registered via the pre-existing `CanonicalGroupLimited.Ubuntu`
  appx package (was installed but never first-run) — ran non-interactively via
  `ubuntu.exe install --root`, avoiding the interactive username/password
  prompt. Result: **Ubuntu 24.04.1 LTS (Noble)**, WSL version 2. Default WSL
  user is `root` (a consequence of the `--root` non-interactive path). A
  secondary non-root user `<owner>` (placeholder — the real login name is
  redacted per SR-010) was also created and added to the `docker` +
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

**Remaining for the Owner:** none — no reboot, no interactive prompt was needed.
Wave 2's V1 homehub-sim (WI-10.14/10.15) is now unblocked.

### DRIVER — G1 — Round 1 — 2026-07-04 (WI-10.14 homehub-sim harness + V1 gate)
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

**FLAGGED FOR OWNER (a NagLight repo fix, out of this repo's scope):** the
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
step5 offsite: pushed 5 files into //mini-serv/icedrive/homehub-backup/run_<ts>
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
`sim/run-sim.sh` first for the shared `homehub-sim_default` network); shares are
`//mini-serv/{minecraft,satisfactory,icedrive}`, user `awow` / `simpass`,
minecraft+satisfactory exported READ-ONLY (live-share-stays-read-only rule).

### DRIVER — G1 — Round 1 — 2026-07-04 (WI-10.18 V3 gate — ISO/VM scripts)
Built `vmtest/` (agent delivers scripts + docs; **the boot itself is the Owner's** —
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
- **`vmtest/New-HomeHubVm.ps1`** / **`Remove-HomeHubVm.ps1`** — Hyper-V Gen2 VM
  (4 vCPU/8GB static RAM stand-in for the AK41, 64GB dynamic VHDX,
  `MicrosoftUEFICertificateAuthority` Secure Boot template for the Ubuntu
  shim, Default Switch/NAT by default with a documented External-switch
  option for real LAN exposure). Idempotent, `-WhatIf` support, elevation
  asserted at the top. **Never run** (elevation + Hyper-V are the Owner's call).
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

**NOT run (honest gap, by design/scope):** `New-HomeHubVm.ps1`,
`Remove-HomeHubVm.ps1` — need elevation + the Hyper-V feature, an agent doesn't
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

**Side effect flagged for the Owner (OI-6 above):** downloading + repacking the
~3.4GB ISO (even under a WSL-native path, not `/mnt/c`) grew WSL2's
`ext4.vhdx` — which itself lives on `C:` — from ~21GB free down to ~9GB, and
deleting the files afterward did **not** give the space back (a known WSL2
quirk: the sparse vhdx doesn't auto-shrink). Reclaim steps are in
`vmtest/README.md` §2.

### DRIVER — G1 — Round 1 — 2026-07-04 (Q10.9 B+ ALL-IMAGES — bake every image into the ISO)

Implemented the Owner's locked **Q10.9 B+** decision (HOMELAB_RESTRUCTURE_PLAN.md):
every stack image is `docker save`d into the ISO deploy payload and `docker
load`ed at first boot, so a freshly-imaged AWOW comes up "from infancy" with
**zero registry/internet dependency for container images**, versions pinned to
exactly what the homehub-sim validated.

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
  `docker load` every tar found in `/opt/homehub/images` (with fallbacks
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
  Either way the tars land at `/opt/homehub/images` for firstboot.

**PIN SET (`latest`/floating → concrete, Q10.9 B+).** `latest` was fine for
bring-up; B+ makes what-boots == what-was-validated, so floating tags are now
wrong. Digest = registry index digest as saved (`docker save` under the
containerd store; see `images.manifest.tsv`):

| Service | `.env` var | was | pinned | registry digest |
|---|---|---|---|---|
| technitium | `TECHNITIUM_IMAGE_TAG` | `latest` | `15.2.0` | `sha256:23d3b63d959e997800b095fe93009b3fae271b5258234ff2ade8535cb33682c8` |
| caddy | `CADDY_IMAGE_TAG` | `2-alpine` | `2.11.4-alpine` | `sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648` |
| oauth2-proxy | `OAUTH2_PROXY_IMAGE_TAG` | `v7.6.0` | **`v7.15.2`** (SECURITY bump 2026-07-10 — auth-bypass CVEs fixed since v7.6.0; NOT yet sim-validated/exported — re-run V1 sim + export-images.sh; digest recorded at that export) | *(pending re-export; v7.6.0 was `sha256:dcb6ff8d…`)* |
| tracker | `TRACKER_IMAGE_TAG` | `local` | `local` (local build, no registry) | id `sha256:9a573d4367032a4d872736718f5dd68872bcf5b1d01d727359ff36413f8f4112` |
| actual | `ACTUAL_IMAGE_TAG` | `latest` | `26.7.0` | `sha256:e18b7fbfec6157a368fad4146563f397502e9da70a120aeaeac63b4977405d1c` |
| ddns | `DDNS_IMAGE_TAG` | `latest` | `v2.10.0` | `sha256:3e2aa558946b5a293def4d73008fa4651c072b2c12932cecd02126fb23979831` |
| uptime-kuma | `UPTIMEKUMA_IMAGE_TAG` | `1` | `1.23.17` | `sha256:3d632903e6af34139a37f18055c4f1bfd9b7205ae1138f1e5e8940ddc1d176f9` |
| dozzle | `DOZZLE_IMAGE_TAG` | `v8` | `v8.14.12` | `sha256:0df89c904da71e94a0c9ed3c89a890f01488321b5f10ac1e0c0bedcead9af6e4` |
| ntfy | `NTFY_IMAGE_TAG` | `latest` | `v2.25.0` | `sha256:cfbbb1bac9196cb711e29ef0ac4adaeb033be6235f1df857705dc39c14384a1d` |

**How the concrete tags were derived (honest):** the 5 **core** images ran in
the V1 homehub-sim; each concrete pin was verified to be the SAME image V1 ran —
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
  saved all 9. **Total payload = 469 MB** across 9 tars (well under the Owner's
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

**AWAITS V3 (the Owner's boot, unchanged):** the actual first-boot `docker load` +
`compose up` run inside a booted VM/hardware. No VM has been booted. Everything
above is the pre-boot smoke test the dev PC can run headlessly.

**OI-6 update:** C: now shows ~16GB free (was ~9GB at the snapshot); the image
tars + seed ISO were written to `vmtest/.out` on `/mnt/c` (real C: via 9p), which
does **not** grow the WSL `ext4.vhdx`. Only the 4 aux-image pulls (~200MB into
the docker store) touched the vhdx.

### DRIVER — G1 — Round 1 — 2026-07-04 (WI-10.10 DRIVE POWER DESIGN — dynamic standby)

Implemented the Owner's ratified DRIVE POWER DESIGN in the bash backup service. The
backup drives are the box's biggest electrical lever (5–8 W each spinning ≈ the
whole CPU), so the policy is **dynamic standby**, two pieces:

**What was built:**
- **Boot-time default standby** — a per-boot oneshot
  `stack/backup/backup-standby.service` + `backup-standby.sh` applies a
  conservative `hdparm -S` spin-down timeout to each configured backup drive
  (`hdparm -S` does not persist across power cycles, so it re-applies every boot,
  like `powertune.service`). Shipped/enabled via autoinstall `late-commands`
  exactly like powertune (runs in place from the stack dir so it can source
  `common.sh`, matching `homehub-backup.service`). Added `hdparm` to the autoinstall
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

### DRIVER — G1 — Round 1 — 2026-07-10 (STACK REVIEW + TIER-2 OPT-IN CATALOG — SN-009/SR-012)

The Owner asked for (a) a review of the deployer stack and (b) an opt-in catalog of
additional self-hosted services (photos ×2, media, music, podcasts, home
automation, passwords, etc.). Both delivered this session.

**Review findings → fixes applied (config-only):**
- **BUG (real-box): `dns.<domain>` would 502.** The Caddyfile proxies
  `host.docker.internal:5380`, a Docker-DESKTOP-only name — plain docker-ce
  never resolves it. Fixed with `extra_hosts: host.docker.internal:host-gateway`
  on the caddy service. The V1 sim could not catch this: `Caddyfile.sim`
  deliberately proxies the bridge container name instead. **Needs V3/hardware
  verification.**
- **SECURITY: oauth2-proxy pin bumped v7.6.0 → v7.15.2** (multiple 2026
  auth-bypass advisories fixed in between: CVE-2026-34457, CVE-2026-40575,
  GHSA-7x63-xv5r-3p2x, GHSA-pxq7-h93f-9jrg, GHSA-c5c4-8r6x-56w3 email-validation
  — the last one weakens exactly our allow-list gate). NOT sim-validated at the
  new tag yet — OI-7(c); pin-ledger row updated with digest PENDING re-export.
- **Docker log rotation** was unconfigured (unbounded json-file on a small
  eMMC) — autoinstall now writes `/etc/docker/daemon.json` with the `local`
  driver (rotates by default).
- **Backup gap recorded as OI-8** (stack volumes — incl. `actual_data` — backed
  up by nothing); deliberately NOT fixed inline: `backup.sh` is
  WI-10.15-validated behavior and a change there needs its own sim assertions.
- Minor (recorded, not acted on): Dozzle has no auth (LAN-bind is the only
  gate); ntfy topics are LAN-open by default; Technitium :5380 is cleartext
  HTTP on the LAN; no mem_limits yet (matters as tier-2 services get enabled).

**Tier-2 catalog added (SN-009/SR-012, all OFF by default, ntfy-profile
pattern):** immich(+ml)/photoprism (photos — at most ONE), jellyfin (QSV via
/dev/dri), navidrome (music), audiobookshelf (podcasts/audiobooks), vaultwarden
(Caddy-site-only ingress by design), homeassistant+mosquitto (host-net HA;
committed no-secret mosquitto.conf), syncthing, freshrss/mealie/homepage, diun
(update NOTIFIER — closes the stale-pin gap within the pins-never-drift
philosophy; `diun.enable=false` label on the registry-less naglight:local).
Wiring: `COMPOSE_PROFILES` in `.env` is the enable switch (firstboot's plain
`compose up -d` honors it); commented Caddyfile sites + `EXTRA_SUBDOMAINS`
(provision script loop) for public exposure; `export-images.sh` gained
`EXTRA_PROFILES` and its default behavior — tier-2 excluded from the baked
payload — falls out of profiles being off (OI-7(a) to ratify). Docs:
stack/README §9 (incl. the pre-build configuration chain + RAM/storage ground
rules); root README table row.

**Versions (honest):** Immich layout verified against the v3.0.2 release
compose (server/ml `v3.0.2`, `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-
pgvectors0.2.0`, valkey `9`); PhotoPrism upstream publishes `latest` as its
stable tag (documented exception). Remaining tier-2 pins are best-effort
known-good tags, marked VERIFY-at-enable in `.env.example`; none are
sim-validated and none carry custom healthchecks yet (WI-10.14 lesson — probe
tooling per image is checked empirically at enable time).

**Assumptions (unattended, to confirm at next gate):** tier-2-not-baked is the
right Q10.9 B+ boundary (OI-7a); `MEDIA_ROOT=/srv/media` default pending the
storage decision (OI-7b); FreshRSS chosen over Miniflux (single container +
SQLite); Zigbee2MQTT deferred until a coordinator stick exists; subdomain
labels fixed as vault/photos/prism/jellyfin/music/audio/home.

**RAN FOR REAL (this session, dev box):** `validate_config.py` (72 compose vars
covered, all PASS) + `check.py` G1 (config-validate, registry-integrity with
SN=9/SR=12, doc-navigability) → **PASS**; `bash -n` clean on the two changed
shell scripts; WSL-Ubuntu `docker compose config` — with **no profiles** it
resolves exactly the original 8 core services (tier-2 exclusion proven for the
export path), with **all 15 profiles** the config is VALID and lists the full
26-image set; `docker manifest inspect` confirmed **every** tier-2 pin AND
oauth2-proxy `v7.15.2` exists on its registry. **NOT run:** any container —
tier-2 services have never been started anywhere (enable-time validation per
README §9), and the core changes (extra_hosts, log rotation, oauth2-proxy tag)
await the V1-sim re-run / V3 boot (OI-7c).

### DRIVER — G1 — Round 1 — 2026-07-10 (OI-8 → SN-010/SR-013: docker-volume sources in the ONE backup table)

The Owner ratified the single-table design ("multiple sources/destinations
configured for a single backup execution sequence"): extend the existing
`BACKUP_SOURCES` grammar rather than bolt on a second mechanism. Destinations
stay as-is (one drive target + the `OFFSITE_SETS` subset selector).

**What was built (TDD — sim first, red, then green):**
- **Grammar (SR-013):** `name=SPEC` where SPEC is `//host/share` (cifs,
  unchanged), `volume:VOL` (live copy from the volume's mountpoint via
  `docker volume inspect` — no helper image needed, the service runs as root),
  `volume:VOL@CONTAINER` (quiesce: stop → copy → restart immediately), or
  `path:/abs/dir`. Comment lines in the table are skipped, so
  `backup.env.example` ships ready-to-uncomment lines for `actual_data`,
  `technitium_config`, `caddy_data`, `tracker_data`.
- **Quiesce safety (the OI-8 hard case):** `QUIESCED` tracking + a
  `quiesce_restore` EXIT trap (runs BEFORE drive-power restore) — no failure
  path can leave a service stopped; an EXIT-trap restart failure warns LOUDLY
  naming the manual command. Inline restart failure fails the run (a stopped
  service is worse than a missed backup). Steps 2–6 untouched — volume sets
  ride the same archive/hash/manifest/retention/offsite/report pipeline.
- **New sim leg `sim/mini-serv-sim/run-volume-sim.sh`** (mock-`docker` +
  mock-`curl` shims, the run-drivepower-sim.sh pattern; feed mocked so the leg
  runs without the homehub-sim tracker).

**RAN FOR REAL (WSL2 / docker, this session):**
- **RED first:** pre-implementation run — scenarios (a)–(c) failed exactly as
  expected (volume: spec treated as a cifs UNC), (d) green. Then GREEN:
  **all 15 checks PASS** — (a) volume set archived+hashed+in-manifest, restore
  drill BYTE-EQUAL vs the fixture volume, zero stop/start without @container,
  comment line skipped, ok=true; (b) exactly one stop + one start, stop
  precedes start; (c) forced mid-copy failure (mock rsync) → container STILL
  restarted via the EXIT trap, ok=false posted, nonzero exit, on_err logged;
  (d) cifs-only table → ZERO docker calls.
- **No-regression:** full `run-backup-sim.sh` re-run against the live homehub-sim
  tracker — **BACKUP LEG: PASS** (cycle, offsite push, real NagLight feed
  round-trip, minecraft restore drill incl. post-loss reconstruct).
- `bash -n` clean; `check.py` G1 re-run after the registry additions (SN=10,
  SR=13) — see below.

**FINDING logged as OI-9 (not fixed here — stay in lane):** the RED run exposed
that `die` paths (e.g. cifs mount failure) exit 1 WITHOUT posting ok=false —
only ERR-trap failures feed NagLight. Pre-existing; small targeted fix + its
own sim assertion later.

**HARDWARE-ONLY REMAINDER (honest):** real-docker semantics (an actual
`docker stop` latency, a volume mountpoint under /var/lib/docker) are proven
only by the call contract here; first real exercise happens on a Docker host /
the AWOW with the volume lines uncommented. The sim shim answers `volume
inspect` with a fixture dir by design.

### DRIVER — G1 — Round 1 — 2026-07-10 (SR-006 resolver chain + .env QUOTING BUG fix)

**The Owner's ask:** NagLight and Finance-Auditor are private — the dev package
should take each locally-built container from a sister folder of the same name,
else grab a declared public one.

**Built:** `scripts/ensure-local-images.sh` — resolves each locally-built image
via **present → sibling build (`../NagLight`) → declared public image
(`TRACKER_PUBLIC_IMAGE` in `.env`, empty until the app repo publishes) → loud
failure naming all three fixes**. Downstream consumers are untouched: compose,
the sim overlay, and export-images.sh keep consuming the same `naglight:local`
ref regardless of which path supplied it (a public image is pulled + retagged).
`sim/run-sim.sh` now calls the resolver before bring-up; export-images.sh's
missing-tracker error points at it. Finance-Auditor has a ready-to-uncomment
entry (no compose service consumes it yet — Actual Budget is the running
finance app). `--rebuild` forces a sibling rebuild; `--dry-run` prints
decisions; `SIBLING_ROOT` overrides the parent-dir convention (CI-friendly).
SR-006 requirement/acceptance updated to cover the chain.

**BUG FOUND AND FIXED while testing (real-box first-boot breaker):**
`.env.example` carried `TRACKER_GIT_NAME=naglight bot` — an UNQUOTED space.
The file is BOTH a compose env-file AND shell-sourced by `firstboot.sh` (step
2) and `provision-technitium.sh`; sourcing that line executes `bot` as a
command → command-not-found → `set -e` kills first boot before compose up.
Never caught because the sim/seed substitute space-free values — the REAL
hand-filled-.env path was the one that would break. Fixed: quoted
`TRACKER_GIT_NAME`, quoted the space-separated knobs (`TECHNITIUM_FORWARDERS`,
`TECHNITIUM_BLOCKLISTS`), added the QUOTING RULE to the file header (compose
strips double quotes, so quoting is safe for both readers). The resolver
itself now greps only the keys it needs instead of shell-sourcing the file
(defense in depth).

**RAN FOR REAL (WSL2 / docker):** all four resolver paths — (1) image present
→ no-op; (2) `--rebuild --dry-run` → sibling build from `/mnt/c/Projects/
NagLight`; (3) no sibling + knob set → public pull+retag decision; (4) neither
→ loud failure, exit 1. Firstboot-style `set -a; . .env.example` now sources
clean (was: `bot: command not found`, exit 127); `docker compose config`
confirms quote-stripping (`TRACKER_GIT_NAME: naglight bot`). `bash -n` clean on
all touched scripts; `check.py` G1 PASS.

### DRIVER — G1 — Round 1 — 2026-07-11 (DOC CURRENCY PASS — the Owner's ask)

The Owner asked for all docs to be brought current (the tier-2 catalog and other
recent work were missing from the root README and elsewhere). Swept every doc
against the repo's actual state:

- **Root README:** intro now names the backup service + the tier-2 opt-in
  catalog; "Run it" points at the SR-006 resolver and the zero-secrets sim
  path; V1 gate row lists the drive-power + volume sim legs; "Configuring the
  REAL box" adds the QUOTING RULE, `COMPOSE_PROFILES`, the backup `volume:`
  lines, and the tier-2/EXTRA_PROFILES bake boundary.
- **AGENTS.md:** the Project section (never filled since scaffolding) now
  carries the one-line purpose, users, layout, run/check commands, non-goals,
  and sibling repos.
- **docs/architecture.md:** topology diagram gains ddns, the backup service,
  and the tier-2 subgraph; new bullets for DDNS/backup/tier-2/resolver; layout
  table adds stack/backup, stack/mosquitto, sim/, vmtest/, the resolver; the
  STALE "no Docker on the dev machine" validation model replaced with the
  three-tier static → V1 sim → V3/hardware model.
- **docs/status.md:** the honest-validation ledger no longer claims
  "PENDING — no Docker" (superseded 2026-07-03); rows now record what the V1
  sim proved vs what V3/hardware still must; Next action refreshed.
- **docs/requirements/interfaces.csv + docs/interfaces.md:** the EXAMPLE row
  replaced with the three real NagLight contracts — IF-001 (naglight:local
  image), IF-002 (trusted identity headers), IF-003 (/api/feed) — and a
  human-readable index; matching IF-IDs still need recording on the NagLight
  side. Clears the pre-existing check_docs orphan warning.
- **vmtest/README:** Q10.9 "bake EVERY container" qualified with the SR-012
  tier-2 boundary + EXTRA_PROFILES. **REMOTE_MANAGEMENT.md:** tier-2
  enable/disable added to the day-2 compose workflow. **sim/README:**
  run-volume-sim.sh listed. **stack/README §2:** QUOTING RULE + tier-2 knob
  pointer. **stack/tracker/README:** resolver pointer.

`check.py` G1 PASS (config-validate, registry-integrity, doc-navigability —
now 0 warnings). Docs-only change; no config/script behavior touched.

### DRIVER — G1 — Round 1 — 2026-07-11 (STATE & CREDENTIALS clarity — the Owner's ask)

The Owner asked where credentials live across updates/reimages and how the budget
app's bank feed wires in. Docs now say it in one place:

- **REMOTE_MANAGEMENT.md "State & credentials"** — the survives-what table:
  NO credential lives in a container/image; state = named volumes (Actual
  server pw + SimpleFIN credential + budgets, Technitium config, Caddy certs,
  tracker data, Vaultwarden) + host files (`.env`, allow-list, `.token`,
  `/etc/homehub-backup/*`). Container updates (`compose pull && up -d` / pin
  bumps) are credential-safe by construction; a reimage wipes volumes → they
  return via the SR-013 volume backups, `.env` re-seeds from the USB payload.
  Plus "What runs where": on the AWOW everything is a container except the
  backup service, powertune/backup-standby oneshots, Cockpit, sshd,
  unattended-upgrades; Mini-serv runs nothing from this stack.
- **stack/README §2** — new OWNER MANUAL STEP: SimpleFIN bank sync is a
  one-time, in-app Actual setup (Actual OWNS the SimpleFIN relationship;
  Finance-Auditor will only trigger its sync). Stored server-side in
  `actual_data`; not doable from the hermetic sim (a real bank credential must
  never enter a throwaway fictional volume) — real box or the real-secrets
  rehearsal VM, whose `actual_data` volume can be carried to hardware via the
  backup/restore path ("authenticate once" literally once).
- **Root README** — real-box step 4 points at both.

`check.py` G1 PASS. Docs only.

### DRIVER — G1 — Round 1 — 2026-07-11 (SN-011/SR-014: Finance-Auditor ingested, profile-gated)

The Owner confirmed FA's deploy artifacts landed (its commits bbea348/59d9f27:
Dockerfile + deploy/compose.service.example.yml + the ACTUAL_API_VERSION
build-arg). Verified ready and ingested per its IF-003 spec:

- **Compose service `finance-auditor`** (profile `finance-auditor` — OPT-IN
  until FA passes G-Release/G-Final, then promote to core + baked payload):
  bridge-internal to actual:5006 + tracker:8787, finance_snapshots +
  finance_actual_data volumes, FINANCE_* knobs in .env.example, TZ-honored
  RUN_AT, no ports/healthcheck (FA's documented daemon design; the tracker's
  automated-lane aging is the backstop).
- **Resolver:** ensure_image grew optional docker-build args; the
  finance-auditor entry is ACTIVE and pins
  `--build-arg ACTUAL_API_VERSION=${ACTUAL_IMAGE_TAG}` — the IF-002 coupling
  is now mechanical (bump the Actual pin → `--rebuild` rebuilds FA against it).
- **Privacy (FA §3 firewall):** backup.env.example gained
  `finance=volume:finance_snapshots@finance-auditor` (commented) + a LOUD
  never-OFFSITE_SETS warning — raw finance data rides the LOCAL drive only,
  never the IceDrive-synced share.
- **Registries:** SN-011 + SR-014; IF-004 (↔ FA IF-003; ids are repo-local —
  FA numbered first — each contract cites the counterpart id). Docs: stack +
  root READMEs, architecture topology, interfaces.md index.

**RAN FOR REAL (WSL2/docker):** validate_config.py + compose config PASS (core
still exactly 8 services without the profile; finance-auditor appears with it);
`ensure-local-images.sh` BUILT `finance-auditor:local` end-to-end (two-stage
build, @actual-app/api@26.7.0 installed in the throwaway stage); `docker run`
of the image boots under Node 24 native type-stripping and exits with the
documented config-invalid fatal naming the missing key (trackerFeedUrl) —
FA's startup contract observed. NOT run: a live pipeline cycle (that is FA's
TC-033 G-Release Demonstration — the homehub-sim's actual+tracker are a
ready-made environment for it; sim runs Actual `latest`, so pin the sim to
26.7.0 for a faithful rehearsal per FA's spec note).

**NagLight cleanup (its repo, recorded not fixed):** /api/feed accepts only
{check,ok,note} — FA posts {check,color,reason,at}; the color-lane extension
FA's IF-001 calls "pending" is still unbuilt tracker-side (and a
finance-audit item must exist in definitions or the post 400s). Also
NagLight's IF registry still claims the deploy/caddy/technitium interfaces
that migrated HERE (WI-10.1) — needs re-homing + counterpart ids; its G1
human sign-off is still pending (process debt). FA multi-user note: its feed
client sends bearer only, no X-Forwarded-User — fine single-user, a gap if
the tracker runs multi-user (D3).

### DRIVER — G1 — Round 1 — 2026-07-11 (NagLight color lane VERIFIED end-to-end + sim interpolation fix)

The Owner said NagLight was updated; verified in its working tree (NOTE: that work
is **UNCOMMITTED in NagLight** — the Owner to commit there): /api/feed now takes
three lanes ({check,ok,note} boolean; {check,color,reason,at} severity;
{check,rgb,...}), field names pinned to FA's contract (NagLight IF-006 ↔ FA
IF-001); its IF registry is re-homed with counterpart ids matching ours
(their IF-004↔our IF-003, IF-005↔our IF-002); G1 human sign-off still pending
(the Owner's).

**RAN FOR REAL (sim):** rebuilt `naglight:local` from the updated tree,
recreated the sim tracker, added the fictional `finances.md` severity-item
fixture to `sim/tracker-seed/` (mirrors NagLight's example-data), probed as a
fresh sim user with FA's exact payload shape:
`POST /api/feed {check:finance-audit, color:yellow, reason, at}` → **HTTP 200**,
`/api/today` shows `reportColor/reportReason/reportAt`, and the day's
aggregate flipped yellow with the de-identified reason in the overlay. The
FA↔NagLight seam is proven in the sim. (Also learned: a color post to a
boolean-lane item 400s by design — lanes are typed per item definition.)

**REGRESSION found+fixed en route:** `docker compose up` with
`--env-file sim/.env.sim` REFUSED to parse after the tier-2/finance additions
— unset `${MEDIA_ROOT}` renders a volume spec `:/media` (compose interpolates
the whole file before profile filtering; `config -q` tolerated it, `up` did
not). Fixed: `sim/.env.sim` gained a fictional interpolation-defaults block
for every tier-2/finance knob. Sim bring-up works again.

**Follow-ons recorded:** `naglight:local` image id changed → the baked payload
needs a re-export before any flash (folds into the OI-7c re-export); OI-10
added (disk-encryption decision); USB-wipe note added to stack/README §3.

### DRIVER — G1 — Round 1 — 2026-07-20 (SN-012/SR-015: opt-in remote light UI; SN-001 rescoped — the Owner's ask)

The Owner asked (following the IceDrive-headless discussion — the current client is
GUI-only, no daemon/CLI, WebDAV sunsetting since 2026-04) to make an on-box
light UI an **opt-in option** and to rescope SN-001: zero-click is guaranteed
for **core** services; opt-in secondary services may need UI/manual config —
always remote, restricted/minimized.

- **SN-001 rescoped** (need + acceptance intent now say "core"; opt-ins that
  can't meet the bar must be OFF by default, document their manual steps and
  what doesn't self-heal, and be fully set-up-able over the LAN).
- **SN-012 + SR-015 added** (+3 SN-012 edge rows): opt-in, off-by-default
  xrdp + minimal-XFCE layer solely for GUI-only vendor apps; first case
  IceDrive Mount & Sync. Priority C, Verification=Inspection.
- **`stack/remote-ui/`**: `setup-remote-ui.sh` (idempotent, non-interactive,
  root-checked; installs xrdp+minimal XFCE+libfuse2t64, wires `~/.xsession`,
  optional `ICEDRIVE_APPIMAGE=` install + autostart; referenced by NOTHING in
  autoinstall/first-boot) + README (LAN-only rule, what does NOT self-heal:
  post-reboot one-RDP-touch, GUI state lost on reimage + re-setup checklist,
  uninstall steps). REMOTE_MANAGEMENT.md gained the opt-in section;
  architecture layout table row added.
- **Scope line held:** backup step 5 offsite stays cifs-only — pointing it at
  an on-box synced folder is OI-11 (needs the Owner; small backup.sh extension +
  sim legs). A6 records the unattended shape decisions (minimal XFCE not full
  DE; no AppImage auto-download; no autologin sync-resume hack).

**RAN FOR REAL:** `check.py` G1 **PASS** (config-validate 77 vars,
registry-integrity `SN=12 SR=15 orphans=21 integrity=0` — the +1 orphan line
(20→21) is SR-015's standard pre-G2 "no TC" state, same as every SR
(Inspection exempts it from the no-LLR finding);
doc-navigability 48 links 0 broken); `bash -n` on the new script OK. NOT run:
the script itself (needs the real box / rehearsal VM — host-level GUI is
outside the compose sim; recorded in A6).

### DRIVER — G1 — Round 1 — 2026-07-29 (WoL wake pre-step, local-path offsite (OI-11 build), OI-9 closed + staleness sweep)

Absorbed three ratified decisions this repo had not caught up with, plus the
driver-side OI-9 fix. No new decisions were taken; two things that would need
one are named at the bottom.

**What was built**

- **Wake-on-LAN pre-step (step 1).** The Windows game box now exposes ONE share
  and is allowed to SLEEP, so `backup.sh` wakes it and waits for **tcp/445**
  before the first cifs mount. `BACKUP_WAKE_MAC` (empty = feature off),
  `BACKUP_WAKE_HOST`, `BACKUP_WAKE_TIMEOUT` (+ optional `BACKUP_WAKE_BROADCAST`
  / `BACKUP_WAKE_IFACE`, A7). The packet is best-effort; the **wait is the
  truth**, and a timeout is a LOUD failure — never-silent-green means a source
  that failed to wake must never look like a source with nothing new. No new
  dependency: the magic packet goes out over bash's `/dev/udp`, with
  `wakeonlan`/`etherwake` as the documented fallback.
- **OI-11 build half — `OFFSITE_PATH`.** Step 5 takes a LOCAL directory (the
  folder the on-box IceDrive client syncs) as the primary offsite target;
  `OFFSITE_UNC` survives as the legacy cifs push. Exactly one may be set —
  both is a config error caught at run start, not a precedence puzzle — and one
  staging routine serves both, so "which files go offsite" stays one fact.
- **OI-9 closed.** `die` now invokes a registered `DIE_REPORTER` before exiting.
  `backup.sh` registers `report_failure`: the single, idempotent failure path
  shared with the ERR trap, so whichever fires first owns the verdict. A failing
  report degrades to a WARNING (it must never mask the original error) and an
  unset reporter is a clean no-op, so `restore.sh` / `backup-standby.sh` are
  untouched.
- **Docs currency:** backup README (wake contract, offsite target choice, step
  table); `backup.env.example` (single-share example, wake section incl. the
  Windows-side settings, OFFSITE_PATH-primary offsite section);
  REMOTE_MANAGEMENT (one share, sleeps, no IceDrive role); remote-ui README
  ("nothing forces a switch" → the switch IS ratified); architecture (backup
  bullet + topology diagram: wake-then-pull, local offsite folder → on-box
  IceDrive → cloud); stack/README "Local validation status" and SN-008/SR-011
  (the "no Docker on the build machine" premise died 2026-07-03, WI-10.13);
  the two committed `\<box>\setup` references reworded so the token-rotation
  warning survives without the literal UNC path.

**RAN FOR REAL (WSL2 Ubuntu + docker-ce, Windows dev PC)**

- `python scripts/check.py` — **G1 PASS** (config-validate 77 vars;
  registry-integrity `SN=12 SR=15 orphans=21 integrity=0`; doc-navigability
  48 links 0 broken). `bash -n` clean on all four backup scripts.
- **`sim/mini-serv-sim/run-backup-sim.sh` — PASS, all checks green**, run
  against the changed service (the runner bind-mounts `stack/backup` live):
  full six-step cycle over the Samba fixtures, offsite push landed 20 files,
  NagLight feed round-trip visible in `/api/today`, restore drill byte-equal
  including the post-loss reconstruct. This is the regression net for the
  **legacy** `OFFSITE_UNC` leg — it still works.
- **`run-drivepower-sim.sh` — PASS (a–d)** and **`run-volume-sim.sh` — PASS
  (a–d)**, i.e. the forced-mid-run-failure scenarios still post `ok=false`,
  still restore drive standby, and still restart a quiesced container after the
  `report_failure` refactor.
- **A throwaway Linux harness** (scratch, not committed) drove the new paths
  with `path:` sources and a stub feed endpoint: happy run with `OFFSITE_PATH`
  → files staged under `<OFFSITE_PATH>/homehub-backup/run_<ts>` + `ok=true` +
  restore byte-identical; `OFFSITE_ENABLED=false` still a clean skip; legacy
  `OFFSITE_UNC` branch still selected and failing loudly when the share is
  unreachable; `--dry-run` unchanged. **Four `die` paths each POSTED
  `ok=false`** before exit 1 (both offsite forms set; enabled-with-no-target;
  wake timeout; unreachable cifs share) — the OI-9 contract, observed rather
  than asserted. Wake helpers checked byte-level: magic packet 102 bytes,
  `ff*6` + MAC ×16, NULs intact for MACs containing `00`; MAC parser accepts
  `:`/`-`/bare and rejects short/non-hex; probe true/false/timeout correct;
  already-awake fast path skips the packet; timeout path dies loudly.

**NOT run (honest gap)**

- **No real box was woken.** Every wake test used a loopback listener or a
  blackhole address. Whether a magic packet actually resumes the game box —
  and whether its adapter/Fast-Startup settings allow it — is V3/hardware
  (OI-13).
- **The `/dev/udp` broadcast send was REFUSED by the WSL kernel** (bash cannot
  set `SO_BROADCAST`), so only the fallback *decision path* was observed, never
  a successful send. `wakeonlan`/`etherwake` are not installed here either.
  Which method the AWOW ends up using is unknown until the box is tried.
- **No sim leg was added** for either new path. `OFFSITE_PATH` and the wake
  step were exercised from a throwaway harness that is not in the repo; the
  committed sim still drives `OFFSITE_UNC`. Recorded as OI-11(a).
- **shellcheck is not installed** on this machine (Windows) or in the WSL
  Ubuntu — not run, not claimed. `bash -n` is all the static shell checking
  that happened.
- The V1 stack sim was already up from a previous session and was **reused**,
  not rebuilt from scratch; `sim/run-sim.sh` itself was not re-run.

**For the Owner / next gate**

- **OI-13** (new): the wake feature is inert until the real MAC lands in
  `/etc/homehub-backup/backup.env`, and it needs two Windows-side settings.
- **OI-11(b)**: set `OFFSITE_PATH` once IceDrive is running on-box; the run
  fails if that directory is missing, deliberately.
- **A7** records the mechanics decided unattended (probe port, default timeout,
  re-send interval, must-already-exist rule, the two optional knobs).
- Left alone on purpose: OI-12 (wall panel) — nothing built, no wall site or
  image lane created.

---

### DRIVER — G1 — Round 1 — 2026-07-29 (SR-010 enforcement — personal name swept out of every tracked file)

The Owner sanctioned (2026-07-29) replacing the personal first name in tracked
files with the generic role term, closing the last gap against **SR-010** /
**SN-007** ("no ... personal-name in any tracked file"). Name tokens only —
no decision IDs, dates, semantics, or surrounding wording were changed, and no
git history was rewritten.

- **21 tracked files** swept (docs, requirements CSVs, READMEs, `AGENTS.md`,
  `REMOTE_MANAGEMENT.md`, compose/sim YAML, `Caddyfile`, `.env.example`,
  autoinstall + backup + remote-ui shell, `vmtest/*`) — plus this log, which the
  Owner explicitly sanctioned despite its append-only rule.
- Mapping used: possessive → "the Owner's"; sentence subject → "the Owner";
  table-cell/label/attribution forms → "Owner"/"Owner:"; the all-caps banner
  became **`OWNER MANUAL STEP`** (same 5-character name token, so the
  `.env.example` ASCII box border needed no re-drawing); hyphenated compounds
  → `Owner-gated`/`Owner-ratified`/`Owner-consented`.
- **Two conflicts, handled deliberately:**
  - The `.env.example` OAuth box had one comment line that no longer fit the
    76-column border once the name expanded; that sentence was re-wrapped across
    its existing two lines. Box width and content are otherwise identical.
  - The 2026-07-03 WI-10.13 WSL2 entry recorded a real *Linux login name* (a
    literal system identifier, not prose). Renaming it to a role phrase would
    have falsified the record, so it is now the placeholder `` `<owner>` ``
    with an inline note that the real login name is redacted per SR-010. The
    account itself is unchanged on the dev PC.
- The pseudonym `diytechy`, the `AuroLeap` org, hostnames (`mini-serv`,
  `homehub`), and all `Co-Authored-By` trailers were left untouched.

**RAN FOR REAL**

- A case-insensitive `git grep` for the name over tracked files: **102 hits
  across 21 files → 0 hits**.
- CSV structure re-parsed with Python `csv` before/after: `interfaces.csv`
  6 rows and `system-requirements.csv` 16 rows, **every row's column count
  identical** — the edits touched cell text only, never a delimiter or quote.
- `python scripts/check.py` — **G1 PASS** (config-validate: 77 compose vars vs
  `.env.example`, 8 Caddyfile vars, all bind-mounts/autoinstall files present,
  YAML parses; registry-integrity `SN=12 SR=15 orphans=21 integrity=0`;
  doc-navigability 11 docs / 48 links / 0 broken). Identical to the previous
  round's numbers, i.e. the rename moved no requirement and broke no link.

**NOT run (honest gap)**

- No sim, no container, no box. This pass is text-only; nothing executable
  changed behavior, so only the static gate was exercised.
- Commit metadata was **not** rewritten. Author identity was already `diytechy`
  (verified via `git config user.name`), and history rewriting was out of scope.

---

### DRIVER — G1 — Round 1 — 2026-07-29 (three Owner rulings: INGEST step, archive EXCLUSIONS, OFFSITE retired)

Implemented the Owner's three rulings of 2026-07-29 in `stack/backup/`. Two are
new pipeline capability; the third **removes** a design half-built earlier the
same day. No new decisions were taken — the mechanics decided unattended are
listed as **A8** and the things only the Owner can supply are **OI-14**.

**What was built**

- **INGEST (step 1b) — ratified.** A new `INGEST_SOURCES` table
  (`name=//host/share -> /abs/library/dest`, one per line, same hand-edited style
  as `BACKUP_SOURCES`) mirror-syncs each network source **into the library tree**
  BEFORE any archiving: wake (the existing `wake_and_wait`, unchanged) → cifs
  mount ro → `rsync -a --delete` → unmount. The library folder is then covered by
  an ordinary `path:` `BACKUP_SOURCES` entry, so one archive flow serves ingested
  and native folders identically. **Mirror semantics are documented loudly in
  three places** (`.example`, README, the run log itself): source deletions
  PROPAGATE, and history lives in the dated run snapshots, not the library.
  Failures are loud per OI-9 — a refused mount, an rsync error, a malformed line
  and a missing library parent each post `ok=false` and exit nonzero. One guard
  exists purely because `--delete` is irreversible: a share that **mounts but
  holds no files** while the library copy does **refuses to mirror**
  (`INGEST_ALLOW_EMPTY=true` overrides). The old pattern (a `//host/share`
  straight in `BACKUP_SOURCES`) still works; the `.example` now presents the
  ingest pair as the intended one.
- **EXCLUSIONS (step 2) — new requirement.** `BACKUP_EXCLUDE` (global,
  space-separated globs, ships as the Owner's `"*.bak"`) plus per-set
  `name.exclude=PATTERN …` lines **inside the one `BACKUP_SOURCES` table** — one
  table, one place to look. Patterns go to `rsync` at pull time (so the copy is
  never made) and to `tar` (so the archive cannot contain them). **Nothing is
  excluded silently:** the run logs the effective pattern list per set, logs each
  path the patterns actually hid (first five inline, all of them in a new
  `<set>.excluded.log` next to the archive — rsync's own `--debug=FILTER`
  decisions), records the patterns in a new MANIFEST `excludes` column and in
  `RUN.json`, and `restore.sh` states plainly that such a set is a **FILTERED
  copy** of its source. A `name.exclude=` line naming a set that does not exist
  **fails the run** rather than quietly filtering nothing.
- **OFFSITE (step 5) — RETIRED from the target state.** The Owner's corrected
  model: the IceDrive client is pointed **directly at library paths in its own
  GUI**, the service stages nothing. `backup.env.example` now documents
  `OFFSITE_ENABLED=false` as the target state with the corrected model spelled
  out and both target knobs demoted to commented legacy; step 5's code is
  unchanged and still works. Docs reworded: backup README (step-5 table row plus
  a rewritten "Offsite — retired from the target state" section), architecture
  (topology diagram now shows *backup → library → IceDrive client → cloud*, and
  the backup bullet says there is no offsite step), REMOTE_MANAGEMENT (Mini-serv
  is ingested, not staged-for), remote-ui README (the sync pairs the Owner
  creates ARE the offsite configuration).
- **New sim leg `sim/mini-serv-sim/run-ingest-sim.sh`** — the ingest/exclusion
  regression net, over the REAL cifs path (only the NagLight feed is mocked, so
  it needs no homehub-sim stack). The compose file gains one fixture: the
  intentionally always-empty share `//mini-serv/empty`, needed to prove an empty
  share cannot mirror-delete a good library copy.

**RAN FOR REAL (WSL2 Ubuntu + docker-ce, Windows dev PC)**

- `python scripts/check.py` — **G1 PASS** (config-validate 77 compose vars + 8
  Caddyfile vars, bind-mounts/autoinstall files present, YAML parses;
  registry-integrity `SN=12 SR=15 orphans=21 integrity=0`; doc-navigability
  11 docs / 48 links / **0 broken**). Same numbers as the previous round.
- `bash -n` clean on all four `stack/backup` scripts **and** all four sim legs
  (bash 5.2.21, rsync 3.2.7, GNU tar 1.35).
- **`sim/mini-serv-sim/run-ingest-sim.sh` — INGEST LEG: PASS, 28/28 checks**,
  first run of the new leg against the real service: (a) the mirror of
  `//mini-serv/minecraft` into `/srv/library/NonDocs/MiniServ` created the leaf,
  came out **byte-identical to the live share** (`diff -r`), was archived by the
  ordinary `path:` flow and **restored byte-equal**, and is recorded in
  `RUN.json`; (b) a planted library-only file **and** folder were **deleted** by
  the next mirror — the `--delete` contract asserted, not trusted; (c) `*.bak`
  globally + `docs.exclude=Downloads` kept 3 paths out of both the archive and
  `docs.files.tsv` while the keepers stayed, the **source still holds** the
  excluded files, both pattern lists appear in the log, every excluded path is
  named in the log and in `docs.excluded.log`, the MANIFEST `excludes` column
  carries them, a set with no per-set line still got the global pattern, and
  `restore.sh` flagged the FILTERED copy; (d) the empty-share refusal fired
  (library survived at 13 files, `ok=false` posted) and `INGEST_ALLOW_EMPTY=true`
  then cleared it as instructed; (e) three loud config failures (malformed
  ingest line, exclude line for an unknown set, missing library parent) each
  died nonzero **and posted `ok=false`**.
- **Regression, all re-run against the changed service:**
  `run-backup-sim.sh` — **PASS** (full cycle over the Samba fixtures, legacy
  `OFFSITE_UNC` push landed 5 files, NagLight round-trip visible in
  `/api/today`, restore drill byte-equal including the post-loss reconstruct);
  `run-volume-sim.sh` — **PASS (a–d)**; `run-drivepower-sim.sh` — **PASS (a–d)**.
  So the loop restructure (the sources table is now parsed in a pre-pass) and the
  new MANIFEST column broke nothing.
- **A throwaway WSL harness** (scratch, not committed) drove the paths the sim
  cannot reach cheaply: **glob safety** — a decoy `*.bak` in the working
  directory does NOT hijack the pattern list (patterns are split with `read -a`,
  never pathname-expanded); `--dry-run` still writes no archive and no library
  change; the `MANIFEST` / `RUN.json` / `restore.sh` fields verified by eye on a
  plain `.tar` set; the exclude-unknown-set and malformed-ingest `die`s each
  posted `ok=false` through a mock `curl`.
- **Checked the bash semantics the new code leans on** rather than assuming them:
  with `errtrace`, the ERR trap does **not** fire for a failing command
  substitution whose status is tested (`x="$(f)" || die`) but **does** for a bare
  assignment — which is why `ingest_parse` may return 1 while
  `exclude_line_name` never does. `rsync --debug=FILTER`'s exact output
  (`[sender] hiding file X because of pattern Y`) was probed before being made
  the visibility mechanism.

**NOT run (honest gap)**

- **No real box, no real share.** Every ingest test used the sim's Samba
  container as the network source; the sleeping Windows box was never woken and
  its share was never mirrored. Real-share behaviour (SMB quirks, permissions,
  file names Linux dislikes) is V3/hardware.
- **Library-scale data was never involved.** Fixtures are kilobytes, so mirror
  duration, the effect of excluding a genuinely very-large folder, and the
  interaction with the drive spin-down policy at that size are burn-in checks.
- **`zstd` is not installed on the WSL host**, so the throwaway harness ran the
  plain-`.tar` path only; the `.tar.zst` path was exercised inside the sim runner
  container (which has zstd) by the ingest + backup legs.
- **shellcheck is still not installed** on this machine or in the WSL Ubuntu —
  not run, not claimed. `bash -n` is the only static shell checking done.
- The V1 stack sim was **already up from a previous session and reused**;
  `sim/run-sim.sh` was not re-run.
- **The offsite target state is documented, not demonstrated.** Nobody pointed an
  IceDrive client at a library path — that is Owner-side GUI work this repo
  cannot exercise (SR-015).
- **No requirement-registry rows were added or edited** (`SN=12 SR=15`
  unchanged), following the precedent of this morning's wake/offsite change.
  See the two stale wordings flagged below.

**For the Owner / next gate**

- **OI-14** (new): ingest + exclusions are inert until the real share, the real
  library destination and the real "very large folder" names land in
  `/etc/homehub-backup/backup.env`; the authoritative paths are in
  `Personal\deploy\storage-map.md`, and the mirror deletes whatever the share
  deletes.
- **OI-11 rewritten** for the corrected model; the `OFFSITE_PATH` build half is
  recorded as **moot** (harmless legacy, no sim leg owed).
- **A8** records the mechanics decided unattended (the arrow grammar, the
  leaf-created/parent-fatal rule, `INGEST_ALLOW_EMPTY`, exclusions living in the
  sources table and the `.exclude` name restriction, the additive MANIFEST
  column).
- **Two now-stale requirement wordings, left for the Owner** because editing
  ratified rows is his call: SR-013's text still says sets flow "through
  archive/hash/manifest/retention/**offsite**/report", and SR-015's acceptance
  criteria still contains the parenthetical "backup.sh offsite is cifs-only
  today - a local-path offsite target is a separate ratified change". Both
  describe a step that is now retired.
- **A monitoring hole worth a decision:** with no offsite step, the backup's
  never-silent-green report **cannot** see a stale cloud copy — after a reboot
  IceDrive is down until someone opens a session, and the backup will still post
  `ok=true`. If that should be watched it needs its own check (something that
  looks at the client/cloud and feeds NagLight); that is a new decision, not
  built here.
- **A privacy-posture change worth noticing:** the Finance-Auditor §3 firewall
  used to be enforceable by config review (`finance` must not appear in
  `OFFSITE_SETS` — a line in a file). In the corrected model the cloud selection
  lives in the IceDrive **GUI**, so nothing in this repo can prove raw finance
  data is not being synced. The `.example`, the backup README and the remote-ui
  README now say so in words, which is all a config repo can do.
- Left alone on purpose: OI-12 (wall panel) — nothing built.

### DRIVER — G1 — Round 1 — 2026-07-29 (SR-013/SR-015 wording currency — the Owner's sanction)

The two stale ratified wordings flagged above are now current per the Owner's 2026-07-29 sanction (SR-013 flow drops offsite → README step 5 legacy; SR-015's deferred offsite question marked settled; SN-010/SN-012 sentences matched) — wording only, nothing ran; `scripts/check.py` PASS.

### DRIVER — G1 — Round 1 — 2026-07-29 (OI-12 RATIFIED: the wall-panel image lane, built)

The Owner ratified OI-12 with a specific, stronger variant than either option on
the table: the kiosk auth site sits on a **LAN-bound alternate port the router
never forwards, PLUS the `/32` allow-list** — belt and braces, not either alone.
Built here. New decisions taken unattended are recorded as **A9**; the things only
the Owner can supply are folded into OI-12 above, and one genuine cross-repo
decision surfaced and was deliberately NOT taken (**OI-15**).

**What was built**

- **The kiosk site (SR-016)** — `{$WALL_HOST}:{$WALL_PORT}` in
  `stack/caddy/Caddyfile`. Four guards, each independent and each documented as
  load-bearing in the block's own banner: the injected `X-Forwarded-User`
  **replaces** any client-supplied identity; `remote_ip {$PANEL_IP}/32` (one
  address, never the LAN CIDR — guest Wi-Fi, IoT gear and an inbound-facing
  Minecraft server share that network); the port published bound to `{$LAN_IP}`
  with the router forwarding only :80/:443; and `respond 403` for everything else.
  Inside, it serves the shell's static build from `stack/wall-shell/` **and**
  proxies `/api/*` to `tracker:8787` — the same origin, because NagLight sends no
  CORS headers (OfficeWallNaglight needs doc §3.2). `/music` ships as a commented
  Navidrome stub. **The Caddyfile deliberately carries no `bind` directive:**
  `LAN_IP` is not an address the container owns, so `bind` there would fail to
  listen at all — the LAN-binding is the compose publish.
- **The wall autoinstall variant (SR-017)** — `stack/autoinstall/wall/`: graphical
  target (`cage` + tty1 autologin, no display manager), Wi-Fi-only netplan
  rendered from placeholders, **no Docker and no Cockpit**, and every §3 quirk
  expressed as config: logind lid ignore (1), `iio-sensor-proxy` masked (2), a
  udev rule generated from `WALL_DISABLE_INPUT` (3), NM powersave-off +
  `cloned-mac-address=permanent` (5). D-W4 is `SLEEP_MODE=suspend|backlight`
  sharing ONE schedule, with the RTC alarm armed **before** suspending and a
  per-boot unit re-enabling the ACPI `XHC` + USB device wakeup flags that do not
  persist. Quirks 4b and 6 are not config at all (mount geometry, vent clearance,
  a measured thermal baseline) and say so.
- **The spine** — SN-013 + 8 edge rows, SR-016 (Demonstration) and SR-017
  (Inspection), IF-005 `Planned` → **`Partial`** with the four remaining gaps
  named. `validate_config.py` gained check 5: every namespaced knob a wall script
  reads must be declared in `wall.env.example` (compose cannot see an
  env-file-configured image), plus the wall variant's files and YAML.
- **Docs** — architecture ("two images from one pipeline", the third auth model),
  `stack/README` §10 (enable steps, the guards as a table, the cert analysis),
  `REMOTE_MANAGEMENT` (the panel is reimage-not-repair), and
  `WALL-BURN-IN.md` for everything only the hardware can settle.

**RAN FOR REAL (WSL2 Ubuntu + docker-ce, Windows dev PC)**

- **`sim/validate-sim.sh` — V1 GATE PASS, all 8 checks**, including the two new
  wall legs. The stack was brought up fresh (`sim/run-sim.sh`) because the overlay
  now adds a network. The wall legs reach the site from two source addresses out
  of ONE container: `simclient` sits on both the default network and a new
  sim-only `simlan` (fixed subnet, static leases), so dialling Caddy's `simlan`
  address arrives as `PANEL_IP` and dialling its default-network address does not.
  Asserted: `forged X-Forwarded-User=sim-user-attacker-9999 from the panel /32
  reached the tracker as sim-user-wallpanel-0003`; panel `/api/today` → 200;
  panel `/` → the shell fixture and `/config.json` → 200; and both `/` and
  `/api/today` from the non-panel route → **403 with Caddy's own body** (which is
  what distinguishes an edge refusal from the tracker's own no-identity 403).
- **THE SIM CAUGHT A REAL BUG ON ITS FIRST RUN, and it was the important one.**
  The design brief's literal "strip then inject" — `header_up -X-Forwarded-User`
  followed by a set of the same field — **does not work in Caddy**: header ops are
  applied in a fixed order (add → set → **delete** → replace) regardless of the
  order written, so the delete erased the injected identity, the tracker saw no
  identity, and every panel request 403'd. Verified by reading the adapted JSON
  (`caddy adapt` showed both a `delete` and a `set` of the same field) rather than
  guessed. The site failed **closed**, which is the right direction — but it would
  never have worked, and on hardware this would have looked like a panel fault.
  Fixed by relying on set-replaces-all (asserted with the forged header, not
  trusted) and a DO-NOT-ADD banner so it cannot come back.
- **`caddy validate`** on the real (not sim) Caddyfile with placeholder env:
  **Valid configuration** — and it confirmed automatic HTTPS treats the
  alternate-port site as a normal HTTPS server (`srv1`, redirects enabled).
- **A throwaway container harness** (scratch, not committed) ran
  `wall-firstboot.sh` **twice** against a bare `ubuntu:24.04` root with stub
  `systemctl`/`udevadm`/`netplan` on PATH — the mock-shim pattern this repo
  already uses for `hdparm`/`docker`. **27 assertions PASS**, and it caught two
  real bugs, both fixed: `printf '%s'` without a trailing newline made `read` skip
  the **last** piped device name (the touchpad would have kept working), and a
  redirect into a missing `/etc/udev/rules.d` / `/etc/netplan` aborted the whole
  script on a minimal root. It also proves the honest-degradation paths: the
  shipped placeholder template exits 0 while announcing that quirk 3 and netplan
  were skipped, and writes no udev rule from nothing.
- **The new config-validate check was negative-tested**: an undeclared
  `${WALL_BOGUS_KNOB}` added to a wall script makes the run FAIL, so check 5 is
  not vacuous.
- **`python scripts/check.py` — G1 PASS**: config-validate (81 compose vars, 12
  Caddyfile vars, 10 wall knobs, both `user-data` files parse), registry-integrity
  `SN=13 SR=17 orphans=24 integrity=0`, doc-navigability 11 docs / 48 links /
  **0 broken**.
- **The certificate claim was verified against Caddy's docs AND source, not
  assumed** (the ask was explicit about honesty here): automatic HTTPS activates on
  the *hostname*, not the port, so a `:8443` site still gets a publicly-trusted
  cert; ACME CAs **never** contact non-standard ports (HTTP-01 is always :80,
  TLS-ALPN-01 always :443); and Caddy's ACME challenge handler runs in every HTTP
  server ahead of route matching, dispatching on the requested hostname
  process-wide — so the existing :80 listener answers for a name that has no
  port-80 site block. **That last point is clear in Caddy's source but is NOT
  stated in its documentation**, and `stack/README` §10 says exactly that rather
  than presenting it as documented. The dependency it creates (inbound :80 must
  stay forwarded, or renewal for this name breaks) is written down, with DNS-01
  named as the fallback since a Cloudflare token already exists for DDNS.

**NOT run (honest gap)**

- **No panel, no hardware, nothing physical.** `cage` has never been started, no
  Wi-Fi has been associated, no suspend or resume has happened, no lid has been
  folded, no udev rule has been applied to a real input device, no backlight has
  been dimmed and no RTC alarm has woken anything. Every §3 quirk fix is
  *asserted as written config*, never as observed behaviour.
- **The off-LAN 403 is still an assumption.** Every wall-site probe came from
  inside a docker network. Nobody has curled the panel's hostname from cellular,
  and nobody has confirmed the router's forward list. This is the single test the
  design brief itself called out as load-bearing, and it remains owed.
- **Docker's source-IP preservation is unproven on the real box.** The `/32` match
  relies on the panel's real address reaching Caddy through the port publish. In
  the sim the probes are on the same bridge, so this is untested; if it fails, the
  site 403s the panel (fails closed, not open).
- **The wall image harness is a throwaway, not a committed sim leg.** A graphical
  kiosk session cannot be exercised in a compose sim, so unlike the wall *site*
  there is no permanent regression net for the wall *image* — the same honesty
  position as SR-015's opt-in RDP layer.
- **IF-005 has no artifact**, so `WALL_APP_CMD` points at a placeholder and the
  kiosk shows an explicit "not installed" screen. Nothing has ever run under
  `cage`, including a stand-in.
- **The payload-bake path was not extended to the wall image.** The wall variant is
  not yet wired into `vmtest/export-images.sh` or the ISO builders; the panel needs
  no container images, but which files a wall USB carries has not been implemented
  or tested.
- **shellcheck is still not installed** on this machine or in the WSL Ubuntu — not
  run, not claimed. `bash -n` (clean on all four wall scripts) is the only static
  shell checking done.

**For the Owner / next gate**

- **OI-12 rewritten as RATIFIED + BUILT**, with what still needs him listed there:
  the four T1/T3 values, the panel's DHCP reservation on its hardware MAC, the
  `EXTRA_SUBDOMAINS` label, the FieldSchema registration, and the burn-in.
- **OI-15 (new) — `/media/*` has no origin.** A genuine decision, left untaken:
  the shell's manifest paths must be same-origin, but the media is on the panel's
  cache while the origin is a site on the AWOW. Marked `TODO(OI-15)` in the
  Caddyfile with a commented stub. The likely answer is panel-side (Electron
  intercepting `/media/*`), which belongs to OfficeWallNaglight.
- **Two cross-repo consequences of registering the wall templates** in
  `Personal\homelab\deploy\FieldSchema.psd1` (Personal is not edited from here —
  reported instead): (i) `WALL_HOST`, `PANEL_IP` and `PANEL_USER_SUB` now appear in
  the **AWOW's** `.env.example` too, because the kiosk site runs on the AWOW — so
  they must move from `config.wall.psd1` to `config.common.psd1`, or the `awow`
  image's completeness rule will refuse to emit; (ii) the wall `user-data` needs
  the SSID and PSK substituted, but the emitter's `userdata` format substitutes
  only the password hash and the SSH key — the placeholders were named
  `REPLACE_WITH_WIFI_SSID` / `REPLACE_WITH_WIFI_PSK` so a generic
  `REPLACE_WITH_<KNOB>` pass is a small change rather than a new format.
- **A note on the panel-down alert:** it is required, not optional (quirk 4b), and
  it is **not** in this repo — it is an Uptime-Kuma push monitor plus a Kuma→ntfy
  notifier, configured in Kuma's UI, and its maintenance window must be taught the
  sleep window or it will cry wolf every single night.
- **`WALL_PORT` is a new T0 knob** (public default, `8443`) and needs no
  FieldSchema entry; unlisted knobs are T0 by that schema's own rule.
- Left alone on purpose: the ISO/payload wiring for the wall image, and OI-15.

### DRIVER — G1 — Round 1 — 2026-07-29 (OI-15 RESOLVED by the Owner: the panel PULLS its media — built)

The Owner ruled OI-15 the same evening it was opened, and the ruling is narrower
and better than the "which origin serves `/media/*`" framing it answered:

> Panel media is an AWOW network share; **the panel PULLS** — once after boot and
> on demand via a dedicated SSH-invocable command — with **MIRROR semantics**
> (`--delete`: content removed from the LAN source disappears from the panel
> cache). `/media/*` is then served panel-locally by the shell's Electron host.

So there is no `/media` route to design on the AWOW at all, and this repo owes the
**pull**. Built here; the shell-side half (Electron mapping `/media/*` onto the
cache) stays OfficeWallNaglight's.

**What was built**

- **`stack/autoinstall/wall/wall-sync.sh`** — mount `MEDIA_SHARE_UNC` read-only
  over cifs (same option shape and credentials-file-first precedence as
  `stack/backup/common.sh`'s `mount_cifs`), `rsync -a --delete` **only** the
  share's `Music/` and `FrameVideos/` subtrees into
  `WALL_MEDIA_CACHE/{music,frame}` (default `/var/cache/wall-media`), unmount via
  an EXIT trap so no failure path leaves a mount behind. The subtree map is a
  constant, not a knob: widening what the panel pulls is a decision, and a knob
  would let a typo widen it silently onto a 256 GB disk.
- **The guards are the ingest step's lessons, transplanted** (that code learned
  them the expensive way): an **empty** source subtree, and separately an
  **absent** one, does not get to mirror-delete a populated cache — the run
  refuses, names the override, and leaves the cache untouched;
  `WALL_SYNC_ALLOW_EMPTY=true` is the deliberate escape hatch and mirrors the
  emptiness *through the same rsync* (an empty temp dir as the source) rather than
  through a second deletion mechanism. Every failure is fatal and nonzero: the
  panel has no NagLight feed of its own, so "loud" means a failed unit plus
  journal lines.
- **`wall-media-manifest.py`** — the sync's post-step, and the reason the
  generator is Python rather than a bash JSON writer: the one thing that must not
  be got wrong is string escaping, and a real library is full of quotes,
  ampersands, `#` and non-ASCII. It emits `music/index.json` in
  `LocalLibraryProvider`'s documented shape (albums from folders, `Artist/Album`
  giving artist + album, `cover.jpg` as art, a leading track number parsed off the
  title, root-level files as the documented **flat** `tracks` form) and
  `frame/playlist.json` as `[{url,title}]`.
- **THE ASYMMETRY THAT WOULD HAVE BITTEN**, found by reading both consumers rather
  than assuming they matched: music `path` values must be **RAW** (`local.js`'s
  `joinUrl()` percent-encodes every segment itself, so encoding here would
  double-encode every space), while frame `url` values must be **ALREADY ENCODED**
  (`frame.js` assigns them straight to `video.src`). Both are asserted, in both
  directions.
- `ensure_ascii=True` on both manifests, so a non-ASCII filename ships as `\uXXXX`
  and cannot depend on the Electron host guessing a charset; a filename whose
  bytes are not valid UTF-8 (a Windows library will produce one eventually) is
  **skipped and counted** rather than emitted as a lone surrogate that would break
  the whole manifest for one bad name.
- **`wall-sync.service`** — `Type=oneshot`, `After=network-online.target` (the
  panel is Wi-Fi-only, so that is load-bearing, not decorative) and
  `After=wall-firstboot.service`, `WantedBy=multi-user.target`,
  `TimeoutStartSec=3600` for the first full copy over 802.11,
  `IOSchedulingClass=idle` so a sync cannot make the wall stutter. **No `.timer`**
  — the ruling is boot + on demand, and re-`start`ing a oneshot IS the on-demand
  path, so `sudo systemctl start wall-sync.service` is the whole documented
  interface.
- **The Caddyfile `TODO(OI-15)` stub is gone**, replaced by the one line the ruling
  makes true: `/media/*` is panel-local, this site serves no `/media` route.
- **Knobs + coverage**: `MEDIA_SHARE_UNC`, `WALL_MEDIA_CACHE`,
  `WALL_SYNC_ALLOW_EMPTY`, `MEDIA_CIFS_CREDENTIALS`/`_USER`/`_PASS`/`_EXTRA` and
  the commented `MEDIA_SOURCE_OVERRIDE` bench hook in `wall.env.example` (10 → 18
  declared wall knobs); `validate_config.py` gained the `MEDIA_` namespace, the
  three new files in its autoinstall-file list, and `wall-sync.sh` as a knob
  consumer. `wall-firstboot.sh` gained a step 8 that creates the cache dirs,
  enables the unit, and reports whether the share is configured; the wall
  `user-data` grew `cifs-utils`, `rsync` and (explicitly) `python3`, plus the
  late-commands that install the three files.

**RAN FOR REAL (WSL2 Ubuntu + node on the Windows host, 2026-07-29)**

- **A THROWAWAY harness — 67 assertions PASS, and it caught two real bugs.** The
  harness drives the REAL `wall-sync.sh` through its `MEDIA_SOURCE_OVERRIDE` bench
  hook against a fixture library with deliberately nasty filenames (apostrophe,
  double quotes, `&`, `#`, non-ASCII, spaces), under `env -i` so nothing ambient
  props it up. Covered: first sync into an empty cache; **`--delete` propagation**
  (a file removed from the source disappears from the cache AND from
  `index.json`); an idempotent no-op re-run; the **empty-subtree refusal** and the
  **missing-subtree refusal**, each with the cache asserted file-count-unchanged
  afterwards; `WALL_SYNC_ALLOW_EMPTY=true` clearing the cache and still emitting
  valid JSON (`[]`); the non-UTF-8 filename skipped-and-counted; five loud config
  guards (unset share, the shipped placeholder, a non-UNC share, a relative cache
  path, a bogus override); and the generator-not-found path failing instead of
  leaving synced media unlisted.
- **THE HARNESS'S FIRST RUN FOUND THE BUG THAT MATTERED**, and it was a
  self-inflicted one: the manifests live *inside* the directories the mirror
  refreshes, so `--delete` removed them on every run and the file counts included
  them — a no-op re-run therefore logged "1 file(s) were DELETED" and the
  cache-clearing message was off by one. Both were fixed properly rather than by
  adjusting the message: the mirror now `--exclude`s the top-level manifest
  (anchored, so a same-named file inside an album is still mirrored) and every
  logged count is a MEDIA count. The side benefit is real — a run that dies before
  the post-step now leaves the last complete manifest in place instead of nothing.
- **The emitted manifest was validated against the REAL CONSUMER, not against my
  reading of it**: `node` importing `normalizeManifest`/`joinUrl` straight out of
  `OfficeWallNaglight/js/music/local.js` (read-only; nothing in that repo was
  touched) — **20 assertions PASS**: 4 tracks / 2 albums / 3 stations with the
  endless shuffle first, `artUrl` and every track URL encoded exactly once
  (`%2520` asserted absent), quotes/`&`/`#`/`Å` round-tripping into playable URLs,
  album+artist inherited by tracks, and the flat form resolving. The frame playlist
  was checked segment-by-segment and by `new URL(...)` resolution back to the real
  filenames.
- **`python3 -m json.tool` on both manifests**, in three states: populated, empty
  (`[]`), and after the non-UTF-8 skip. All valid.
- **`wall-firstboot.sh` was re-checked for real after gaining step 8** — a second
  throwaway container harness (bare `ubuntu:24.04`, stub
  `systemctl`/`udevadm`/`netplan`), **12 assertions PASS**: the shipped placeholder
  template still exits 0, the cache dirs are created, the unit is enabled, the
  placeholder share is warned about *with its consequence stated*, a filled share
  is reported instead, and a missing `wall-sync.service` is called out rather than
  silently skipped.
- **`caddy validate` on the real Caddyfile after removing the `TODO(OI-15)` stub**
  (pinned `caddy:2.11.4-alpine`, placeholder env + a valid bcrypt so provisioning
  gets that far): **Valid configuration**, both servers still adapting.
- **`bash -n` clean** on all five wall scripts; `py_compile` clean on the
  generator (it is written to run on Python 3.8+, though the panel has 3.12).
- **The new config-validate coverage was negative-tested**: an undeclared
  `${MEDIA_BOGUS_KNOB}` in `wall-sync.sh` makes the run FAIL with exactly that
  name, so the `MEDIA_` namespace is not vacuous.
- **`python scripts/check.py` — G1 PASS**: config-validate (18 wall knobs, both
  `user-data` files parse), registry-integrity `SN=13 SR=17 orphans=24
  integrity=0`, doc-navigability 11 docs / 48 links / **0 broken**.

**NOT run (honest gap)**

- **No cifs mount was performed by any of this.** Every real run used the bench
  hook, so `mount -t cifs`, the credentials file, `vers=3.0`, and the behaviour of
  a share that vanishes mid-rsync are all untested here. The backup service's
  ingest leg exercises the same `mount_cifs` shape against a real Samba container,
  which is evidence for the *pattern* but not for this script.
- **No panel, no Wi-Fi, no library-scale data.** Fixtures are kilobytes on ext4;
  the first-sync duration over 802.11, the 256 GB disk budget with a real
  `FrameVideos/`, and the interaction with the D-W4 sleep window are hardware.
- **Nothing has ever read these manifests in the shell.** The consumer check ran
  `normalizeManifest` in node, not the Electron host — and the *other half* of the
  ruling (mapping `/media/*` onto the cache) does not exist yet in
  OfficeWallNaglight, so end-to-end playback is unproven by construction.
- **The kiosk user has never read the cache.** `--chmod=D755,F644` is asserted as
  written config, not as an observed `sudo -u panel` read (burn-in §8 has the
  check).
- **Throwaway, not a committed sim leg** — same position as the wall image
  harness: a cifs + Wi-Fi + graphical path cannot be exercised in the compose sim,
  and a leg that only re-ran the bench hook would test the hook, not the pull.
- **shellcheck is still not installed** on this machine or in the WSL Ubuntu — not
  run, not claimed.
- **No requirement-registry rows were added or edited** (`SN=13 SR=17` unchanged),
  following this lane's precedent: the pull is the mechanism of an already-ratified
  need (SR-017 / D-W8 rider / OWN-D5), not a new one. If the Owner wants the media
  pull to carry its own SR row with its own verification method, that is a spine
  edit for him.

**For the Owner / next gate**

- **OI-15 is closed on this side and half-open on the other.** The panel pulls; the
  Electron host must serve `/media/*` from `WALL_MEDIA_CACHE`. That is one line in
  `IF-005`'s contract and it is now the *only* `/media` question left.
- **OI-16 (new) — does a RESUME also sync?** Implemented exactly as ruled (boot +
  on demand), but `SLEEP_MODE=suspend` means the panel may not boot for weeks, so
  in practice the cache is as fresh as the last SSH command. Three options are
  written up in the item; none was taken unattended.
- **A freshly imaged panel FAILS `wall-sync.service` on every boot** until
  `MEDIA_SHARE_UNC` and the credentials are filled in. That is deliberate (a green
  unit on a music-less wall would be a lie), but it is a thing to expect rather
  than to debug — and it means `MEDIA_SHARE_UNC` + a root-only
  `/etc/wall-panel/cifs.creds` join the panel's fill-in list, with the share's
  identity coming from `Personal\deploy\storage-map.md`, not from a guess here.
- **The share must have `Music/` and `FrameVideos/` AT ITS ROOT.** If the household
  library's layout puts them somewhere else, that is a one-line change (a subpath
  knob, or a different share) — say which, rather than letting the sync refuse.
- **FieldSchema consequence:** `MEDIA_SHARE_UNC` is a T1-ish value and the share
  password is T3. If the wall templates are registered as planned, the password
  belongs in the secret store, not in `wall.env` — the script prefers a credentials
  FILE for exactly that reason and warns when it falls back to inline.
- **A disk-space question nobody has answered:** mirroring the whole `Music/` +
  `FrameVideos/` tree onto a 256 GB panel disk is fine for music and possibly not
  fine for video. If `FrameVideos/` is large, the honest fix is a curated subfolder
  on the share rather than a filter here — that is the Owner's call about what the
  wall should show.

### DRIVER — G1 — Round 1 — 2026-07-29 (OI-16 RESOLVED by the Owner: wall-sync also fires on RESUME — built)

The Owner decided OI-16 the same evening it was opened: **option (a)** — the
media sync also fires on resume from suspend. The reasoning the item stated
holds: with `SLEEP_MODE=suspend` the panel suspends nightly and resumes without
booting, so "once after boot" was, in practice, "approximately never". Now every
wake behaves like a boot for freshness. Zero new knobs, one new file.

**What was built**

- **`stack/autoinstall/wall/wall-sync-resume.service`** — the standard systemd
  resume hook: `WantedBy=suspend.target` pulls it into the suspend transaction,
  `After=suspend.target` orders it after that target is *reached* — which only
  happens when `systemd-suspend.service` completes, i.e. **at wake**. So it
  starts on resume, never at suspend time; `Type=oneshot` with no
  `RemainAfterExit` drops it back to inactive so every later cycle fires again.
- **`ExecStart=/usr/bin/systemctl start --no-block wall-sync.service`** — and
  `--no-block` is load-bearing, not a flourish: the hook runs *inside* the
  resume transaction, a plain `start` waits for the started job, and a first
  full mirror is allowed up to an hour (`TimeoutStartSec=3600`). Blocking would
  hold the resume transaction open for the whole sync; `--no-block` only
  enqueues the job and returns, so the hook finishes in milliseconds, the wake
  is never perceptibly delayed, and the sync runs detached as its own job with
  its own journal, timeout and pass/fail (`journalctl -u wall-sync`). Same
  unit, same code path as boot and on-demand — no second entry point to drift.
- **The Wi-Fi race, handled where ordering cannot reach:**
  `network-online.target` was reached at boot and is NOT re-evaluated on
  resume, while re-association takes a few seconds after wake — so no unit
  ordering can cover it. `wall-sync.sh` therefore gained a short **bounded**
  wait before the real mount: `nm-online -q --timeout=30` (network-manager is
  already in the wall image), **non-fatal in every branch** — success proceeds
  silently, timeout warns and proceeds, a missing `nm-online` warns and
  proceeds. The cifs mount remains the loud arbiter, and the documented retry
  is the on-demand command. Boot/on-demand runs lose nothing (`nm-online`
  returns immediately when the network is already up); the bench-hook path
  skips the wait entirely (it touches no network).
- **Only `suspend.target`** — the panel's one sleep path is `systemctl suspend`
  from `wall-sleep.sh`; no code path can reach hibernate/hybrid-sleep, so those
  targets are deliberately not listed rather than cargo-culted in.
- **Wiring, consistent with wall-sync's own install:** the wall `user-data`
  late-commands cp the unit and `systemctl enable` it (enabling is what plants
  the `suspend.target.wants` symlink — an installed-but-disabled hook never
  fires); `wall-firstboot.sh` step 8 re-enables it idempotently and warns, with
  the consequence stated, if the file is missing. `validate_config.py`'s
  autoinstall-file list gained the one new file; no knobs were added so the
  knob coverage is unchanged (18 declared, still all accounted for).
- **Docs:** `wall/README.md` (table row + the "no timer" bullet rewritten to
  "a resume DOES sync", with the `--no-block` and Wi-Fi reasoning);
  `WALL-BURN-IN.md` §8's "decide OI-16" checkbox replaced by the two hardware
  proofs (an `rtcwake` suspend→wake with `journalctl -u wall-sync-resume -b`
  showing the sync fired after the wake, and the same via the real
  `wall-sleep.sh start` nightly path); the OI-16 item above → RESOLVED.

**RAN FOR REAL (this machine + WSL, 2026-07-29)**

- **`systemd-analyze verify` on the new unit** (systemd 255 in WSL): exit 0.
  (The one warning — "marked executable" — is an artifact of the drvfs copy,
  not of the unit.)
- **`bash -n`** clean on both edited scripts (`wall-sync.sh`,
  `wall-firstboot.sh`).
- **The edited `wall-sync.sh` re-run end-to-end in WSL Ubuntu** through the
  `MEDIA_SOURCE_OVERRIDE` bench hook under `env -i`: mirror + both manifests
  regenerated, `python3 -m json.tool` valid on both, exit 0.
- **All three nm-online branches exercised for real** with a stub on `PATH`:
  called with exactly `-q --timeout=30`; exit-0 proceeds silently; exit-1
  prints the "network still not up after 30s" warning and proceeds; absent
  binary prints the skip warning. In each case the run still died LOUDLY at the
  refused cifs mount (exit 1, cache untouched) — the arbiter is unchanged.
- **`python scripts/check.py` — G1 PASS**: config-validate (the new file
  present, 18 wall knobs, both `user-data` files still parse as YAML),
  registry-integrity `SN=13 SR=17 orphans=24 integrity=0`, doc-navigability
  11 docs / 48 links / 0 broken.

**NOT run (honest gap)**

- **No real suspend/resume fired this hook.** WSL cannot suspend, so
  `WantedBy=suspend.target` actually triggering at wake — the entire point —
  is asserted from the documented systemd semantics, not observed. Burn-in §8
  now carries the two-step hardware proof.
- **No real Wi-Fi re-association raced the sync.** The 30 s `nm-online` cap is
  a judgment, not a measurement; if this radio routinely takes longer, burn-in
  says to report it rather than tune silently.
- **`--no-block`'s "wake never delayed" claim** is by construction (enqueue and
  return), verified only as unit semantics — the perceptibility check is on the
  burn-in list, on the panel.
- **shellcheck is still not installed** on this machine or in the WSL Ubuntu —
  not run, not claimed.

**For the Owner / next gate**

- OI-16 is **closed**: boot + resume + on demand, still no timer. The only
  remainders are hardware (burn-in §8): see the hook fire after a real
  suspend→wake, and once from the real nightly `wall-sleep.sh` path.
- Freshness statement, updated and honest: the cache is now as fresh as the
  **last wake or boot**, whichever is later — plus whatever `sudo systemctl
  start wall-sync.service` was run in between. Music added during the panel's
  awake hours still needs the on-demand command (or waits for tomorrow's
  resume); that is the shape of option (a), stated rather than hidden.

---

### 2026-07-30 — Owner-directed automation sweep (Personal open-items A9) + feed-transport defect fix

Four changes landed this pass, driven by the Owner's ruling that box/LAN-local
configuration should self-configure wherever the trust model allows:

- **`FINANCE_ACTUAL_SYNC_ID` is now optional** (`stack/.env.example`): empty =
  finance-auditor auto-discovers the sole budget file at startup (its own repo
  change, `getBudgets()`/`cloudFileId` verified against the pinned
  `@actual-app/api` 26.7.0 types); several files = fatal naming them.
- **Backup feeder transport fixed** (`stack/backup/common.sh feed_naglight` +
  `backup.env.example`): the shipped example URL pointed at the public
  tracker route, which oauth2-proxy would bounce and whose `X-Forwarded-User`
  it would overwrite — and the tracker is deliberately bridge-only (D2), so a
  host-side curl cannot reach it at all. New `NAGLIGHT_FEED_CONTAINER` knob
  (default `tracker`): the POST runs inside the tracker container via
  `docker exec` + its busybox wget (the same binary its healthcheck proves)
  against its own loopback. Port stays closed. **Sim-untested**: `bash -n`
  clean; the homehub-sim backup lane is the gate.
- **`provision/provision-actual.sh` (NEW) + firstboot step 5b**: sets the
  dev-PC-minted Actual server password on the un-bootstrapped server
  (`POST /account/bootstrap`, checked via `GET /account/needs-bootstrap`
  first; idempotent — an already-bootstrapped server is a logged no-op, with
  mismatch guidance). Runs inside the `actual` container via
  `docker exec node -e` + fetch (image ships no curl/wget, WI-10.14; password
  via exec env, never argv). **HONEST STATE: the endpoint shape is from
  actual-server source reading, NOT yet exercised against the pinned
  `ACTUAL_IMAGE_TAG` in the sim — that sim run is the acceptance gate for
  this script.** `bash -n` clean.
- **`provision/list-tracker-users.sh` (NEW, read-only)**: prints the per-user
  dir names (= Google `sub` values) under the tracker volume so the Owner can
  fill `NAGLIGHT_USER`/`PANEL_USER_SUB` without spelunking. Deliberately NOT
  auto-discovery for `PANEL_USER_SUB` — that is the unauthenticated identity
  the kiosk injects (security-critical per the Caddyfile banner); the ruling
  is the human matches sub → person. `bash -n` clean.

Companion changes in `Personal\homelab\deploy\` (same pass): basic-auth
plaintexts + Technitium/Actual server passwords are now machine-minted
(`GeneratedPassword`), the two Caddy bcrypt hashes derive automatically at
prep time (WSL python3-bcrypt — the `docker run caddy hash-password`
instruction printed here was unrunnable on the dev PC, no docker), and a new
`Show-DeploySecret.ps1` reads one key back for browser prompts.

---

### 2026-08-01 — dozzle + uptime-kuma healthchecks fixed (V3_GATE_HANDOFF §5 item 1)

The last two never-passable healthchecks — the same class of bug the homehub-sim
caught for tracker/oauth2-proxy/technitium/actual in WI-10.14, but in the aux
containers the V1 sim never ran (LAN_IP binds), so nothing had ever executed
them. Both sat permanently red. **Verified against the PINNED images, not
assumed** — `amir20/dozzle:v8.14.12` and `louislam/uptime-kuma:1.23.17`:

- **uptime-kuma** ships **no wget** (it has curl + bash), so
  `CMD-SHELL wget …:3001/` could never run. The image already declares its own
  `HEALTHCHECK CMD-SHELL extra/healthcheck` — a 6.8 MB compiled Go binary at
  `/app/extra/healthcheck` (source `extra/healthcheck.go` sits beside it) that
  GETs `http://127.0.0.1:3001` and exits 0 on 200. Now
  `test: ["CMD", "/app/extra/healthcheck"]` — the image's own probe, absolute
  path so it does not depend on WorkingDir, keeping this stack's 30s cadence
  rather than the image's slower 60s/180s defaults.
- **dozzle** is **distroless** — no `/bin/sh`, so *any* `CMD-SHELL` form is
  unrunnable. Unlike kuma it declares **no HEALTHCHECK of its own**, so one had
  to be supplied: the binary ships a `healthcheck` subcommand ("checks if the
  server is running") that requests its own `/healthcheck` endpoint. Now
  `test: ["CMD", "/dozzle", "healthcheck"]` — exec form is mandatory here.
  Bonus: the subcommand reads the same `DOZZLE_ADDR` config as the server, so it
  follows the listen port instead of hardcoding 8080.

**RAN FOR REAL (WSL, podman 5.3.1 — this dev PC no longer has the Docker Engine
of WI-10.13):** pulled both pinned images; confirmed the missing/​present tooling
above by exec; ran each probe as a **container-runtime healthcheck**
(`podman healthcheck run`) → **both report healthy**, and unhealthy when the
service is down (dozzle's negative case checked with no server running).
`scripts/validate_config.py` → ALL CONFIG CHECKS PASSED. Compose YAML re-parsed
in a throwaway python container (the validator's own YAML step SKIPs here — no
PyYAML on this interpreter): 27 services, both `test:` arrays as intended.

Two test artifacts worth recording so the next person does not re-chase them:
**dozzle exits 1 without a docker socket**, so a socket-less test container is
dead, not unhealthy — the passing run mounts the socket like production does;
and podman's `--health-cmd` does **not** parse a JSON-array string (it stored
`[["CMD",…]]` and tried to exec that literally) — its `CMD …` prefix form is the
CLI equivalent of compose's `test:` list. Neither affects the compose file.

**Also checked, and NOT broken:** `ntfy` v2.25.0 — its `CMD-SHELL wget …
/v1/health` probe is fine (image has wget + sh; ran it, `{"healthy":true}`).
Worth confirming because the 2026-07-29 telemetry ruling made ntfy load-bearing
for the Kuma→ntfy notifier. `caddy` was already proven healthy in the V1 gate.
`ddns` defines no healthcheck by design. That closes the audit of every
healthcheck in the stack.

Still hardware-gated: these two aux images remain otherwise first-validated at
V3 boot / burn-in (image *behaviour* under the real LAN_IP binds is unchanged by
this fix).

---

### DRIVER — G1 — Round 1 — 2026-08-01 (TIER-2 PROMOTED TO THE HOMEHUB DEFAULT SET — SR-012)

Owner's call: `immich` (+`immich-ml`), `jellyfin` and `finance-auditor` become
the **default** opt-in set for the AWOW image, opt-out-able. Nothing is promoted
to core here — `docker-compose.yml` still ships every tier-2 service profiled
OFF and `.env.example` still enables none. The image's set lives in Personal's
`config.homehub.psd1`, so the public default is untouched and disabling one is
still a word removed from `COMPOSE_PROFILES` + `docker compose up -d
--remove-orphans`.

**Three defects the change surfaced — all latent, none introduced by it:**

1. **`MEDIA_ROOT` was ratified and never wired.** D-W8/OI-7b settled it on
   2026-07-29 (`/srv/library/NonDocs/Media`); the generator emitted
   `media-root.txt` saying "nothing consumes this file until then", and the
   materialised `.env` kept the template's `/srv/media` — a directory on the
   119 GB system disk that no fstab line mounts. Every media profile would have
   served an empty library and written photos to the wrong drive. Wired now
   (Personal side); this closes the pathing half of **OI-7(b)**.

2. **Drives mounted AFTER `docker compose up -d`** (firstboot step 5c vs step 4).
   Docker creates a missing bind-mount source itself, on whatever filesystem is
   present at container-start time; the ntfs3 mount then lands on top and
   **shadows** it. The container keeps writing to an invisible directory on the
   system disk while every read through the share sees an empty library — silent
   until the system disk fills. Harmless while nothing bind-mounted inside
   `/srv/library`; live the moment a media profile is on. Mounting moved to a new
   **step 3b**, ahead of compose. The comment block at 5c already argued exactly
   this ordering for Samba — it just had not been applied to containers.

3. **`devices: /dev/dri` cannot live in a shared compose file.** A device entry
   for an absent node fails container *creation*, and compose reports that as a
   failed `up` for the whole run — so the file as written took the entire stack
   down on any box without an iGPU, the V3 gate VM included. New
   `provision-compose-overrides.sh` (**step 3c**) generates
   `docker-compose.override.yml` with the device when `/dev/dri` exists and
   removes it when it does not; compose auto-loads that filename, so a later
   manual `docker compose up -d` over SSH behaves the same. It refuses to touch
   an override lacking its generated header.

**Also in this pass:**
- `JELLYFIN_LIBRARY_DIR` — hand Jellyfin a subtree instead of the whole media
  root (empty = whole root, the generic default). The AWOW gets
  `NonDocs/Media/Movies` per storage-map §3 row 3; music stays Navidrome's and
  the panel's. In-container path is always `/media`.
- `IMMICH_ML_MEM_LIMIT` — the tier-2 banner's "or cap memory", made real. On
  7.6 GiB usable the ML container is the one that can OOM the box; capped, the
  kernel kills it rather than picking a victim from the core stack, and compose
  restarts it. AWOW starts at `2g`.
- `SIM_ENV_OVERRIDES` (`vmtest/lib/common.sh`) — lets the V3 gate boot a real
  image's profile set without changing `.env.example` or staging real secrets.
  Fails the build on a key that is not already a knob, so a typo cannot make the
  gate silently test the default set and report success.
- Docs: tier-2 caveats gained the mount-ordering rule, the case-sensitivity trap
  (`Music` ≠ `music` on ntfs3/ext4 — docker creates an empty sibling, no error),
  and the hardware-passthrough rule.

**Verified (WSL, Docker 29.6.1 — the Engine is back on this dev PC):** all five
new pinned tags resolve on the registry (immich-server/ML `v3.0.2`, immich
postgres `14-vectorchord0.4.3-pgvectors0.2.0`, `valkey:9`, `jellyfin:10.10.7`);
`docker compose config` against the real materialised homehub `.env` resolves 15
services with the bind at `/srv/library/NonDocs/Media/Movies -> /media` read-only
and **no** `devices:` key; the same file against `.env.example` still falls back
to `/srv/media` with no `mem_limit`, i.e. the public default is byte-for-byte
unchanged in behaviour. `scripts/check.sh` → PASS (config-validate,
registry-integrity, doc-navigability).

**Owner-carried risk, recorded not resolved** (Personal `open-items.md` **A17**):
Finance-Auditor is enabled *before* its own G-Release/G-Final, and its tracker
feed cannot authenticate on this box — D3's multi-user tracker wants an
`X-Forwarded-User` its feed client does not send, so the daily status post is
lost while the audit runs fine. FA carries no healthcheck *because* that post is
its liveness signal, so this fails silently and looks healthy. Immich's photo
store also has no `arch-` row in storage-map §4b, where absence-of-row is the
documented way to say "not backed up".

### DRIVER — G1 — Round 1 — 2026-08-01 (V3 GATE RE-RUN — booted, and it found three more)

Ran the full gate against the tier-2 default set: repacked ISO (5.59 GB, 15
baked images / 2473 MB payload), zero-keypress boot, hands-off install, first
boot, verified over SSH. **`homehub-firstboot` → SUCCESS.**

| Service | Result |
|---|---|
| technitium / caddy / actual / tracker | **healthy** (the four gate criteria) |
| jellyfin | **healthy**, `/health` → 200 |
| immich-server | **healthy**, `/api/server/ping` → 200 — but see (1) |
| immich-db / immich-machine-learning | **healthy** |
| ntfy / dozzle / uptime-kuma | healthy |
| oauth2-proxy / immich-redis | Up, no healthcheck by design |
| ddns | unhealthy — expected, SIM Cloudflare token |
| finance-auditor | **restart loop** — see (2) and (3) |

Bind path resolved to `/srv/library/NonDocs/Media/Movies -> /media` read-only;
`IMMICH_ML_MEM_LIMIT` landed as exactly 2147483648 bytes; step 3b mounted before
compose; step 3c wrote the override. **Memory with all 15 up: 2.4 GiB used of
7.8, 5.3 GiB available** — the 8 GB budget holds with room, though the AWOW will
also be serving Samba and running backups.

**Three findings, none of them the thing the run was aimed at:**

1. **`/dev/dri` EXISTS in Hyper-V** — `hyperv_drm` publishes `card1` (no
   `renderD*`). This repo's compose comment asserted the opposite ("container
   creation FAILS ... e.g. a Hyper-V test VM"). Corrected in three places. The
   override is still right — it just means the gate VM does **not** exercise the
   no-device branch, which is covered by direct tests instead.

2. **A stale locally-built image was baked into the ISO and nothing noticed.**
   `finance-auditor:local` in the payload was built 2026-07-11 — **19 days older
   than its repo HEAD**, from before the A8 auto-discovery work — and crash-
   looped on `ACTUAL_SYNC_ID is required`, a knob the current source does not
   require. `naglight:local` was stale too (missing 75b3e3a, the `/api/feed`
   severity colour lane), which means **the 2026-07-31 gate that PASSED was also
   running a stale tracker.** Root cause: a `*:local` image has no registry and
   no version in its tag, and every resolver treats "present" as "done" —
   `ensure-local-images.sh` skips it, `export-images.sh` saves it.
   **Fixed, and the fix took two passes.** `ensure-local-images.sh` now stamps
   `homehub.source.revision` at build time and `export-images.sh` refuses to bake
   an image whose stamp ≠ sibling HEAD (unstamped → loud warning, dirty tree →
   note, `ALLOW_STALE_LOCAL=1` to override). The **first** cut compared
   `.Created` against the sibling's HEAD date and was WRONG: a cache-identical
   rebuild reuses the image record and keeps its original `.Created`, so a
   just-rebuilt image still reported stale. Commit shas are exact; timestamps
   are not. A second layer of the same bug: `export-images.sh` skipped re-saving
   a tar that already existed — fine when the tag pins content, useless for
   `*:local`, so those are now re-saved every run.

3. **finance-auditor cannot start on a fresh box, by design, and will restart
   forever.** With the current image the error becomes the intended one:
   `ACTUAL_SYNC_ID is unset and the server has 0 budget files — cannot
   auto-pick`. Auto-discovery works; there is simply nothing to discover until
   somebody creates a budget in Actual's UI. `provision-actual.sh` sets the
   server password but no budget. So on the real AWOW this profile will sit in
   `restart: unless-stopped` backoff from first boot until that manual step
   happens — visible in `compose ps` and Uptime-Kuma as a broken service.
   Recorded, not fixed: the durable answer is FA treating "no budget yet" as
   wait-and-retry rather than fatal.

**Method note for the next session:** `Get-VMNetworkAdapter | IPAddresses` never
reported an address for this guest — Ubuntu Server does not run the Hyper-V KVP
daemon by default — so an automated runner must read the IP from the console
thumbnail or scan the Default Switch subnet, not from the Hyper-V integration
data. Verification was done over SSH from the host (WSL2 cannot route to the
Default Switch subnet; use Windows-side `ssh`/`scp`). The `hub` account is in the
`docker` group, so none of the verification needs `sudo`.

### DRIVER — G1 — Round 1 — 2026-08-01 (BACKUP DRIVE: FALSE-GREEN CLOSED — Owner question)

The Owner asked whether a disconnected **backup** drive produced a NagLight
report, or whether the only check was "did the backup run". Answer: neither, and
the gap was worse than unreported.

There were exactly two check ids in the system — `library-mounted`
(`samba/library-guard.sh`, `/srv/library`, on a 10-minute timer AND as `root
preexec` on every Samba connect) and `backup` (posted by a run). The backup
drive had **no presence check at all**: the only thing that ever looked at it was
the 03:30 run.

**And the run could not tell an absent drive from an empty directory.** The
generated fstab uses `nofail` (mandatory — a missing USB disk must not hold up
`local-fs.target` on a headless box), so with the drive unplugged
`BACKUP_TARGET=/mnt/backup-drive` is an ordinary empty directory on the system
disk. `mkdir -p "$RUN_DIR"` succeeded there, rsync copied into it, verification
passed (the files genuinely were present), retention pruned, and step 6 posted
**`ok=true`** — a green backup lane writing the household's backups to the 119 GB
system disk until it filled. Grep confirmed no `mountpoint`/`findmnt`/mountinfo
check against `BACKUP_TARGET` and no `RequiresMountsFor=` on the unit.

That is the exact silent-green shape `library-guard.sh`'s own header forbids for
the library. The guard had simply never been pointed at the second drive, while
`Generate-FromStorageMap.ps1` emits fstab lines for both.

**Fixed, in two halves:**
- **backup.sh step 0 — target preflight.** Refuses to run unless `BACKUP_TARGET`
  is a real mountpoint, and refuses on a `ro` mount (ntfs3's dirty-bit
  fallback). Posts `ok=false` first, exits 1. Placed BEFORE the `mkdir`, which is
  the whole point. NOT implemented as `RequiresMountsFor=`: systemd would refuse
  to start the unit, so nothing would reach NagLight at all — an unreported
  non-run is worse than a red one. Escape hatch `BACKUP_TARGET_REQUIRE_MOUNT=false`
  for a target that is deliberately a plain directory, which then warns loudly
  every run.
- **`homehub-backup-drive-health.timer`** — check id `backup-drive-mounted`, the
  twin of `library-mounted`, every 10 minutes. Reuses `library-guard.sh` via its
  existing `--library` plus a new `--label` (so a red check names the right
  drive); the library's own wording and check id are byte-identical to before.
  The unit reads `BACKUP_TARGET` out of `backup.env` rather than hardcoding a
  site path, and no-ops cleanly on an unprovisioned/sim box.

**Why a 10-minute cadence is safe on a parked drive:** the only probe is
`/proc/self/mountinfo`, a kernel pseudo-file. The answer comes from the VFS mount
table and **no request reaches the device**, so the check cannot wake a
spun-down disk — `df`/`stat`/`ls`/touch-tests all can, and none are used. That is
what makes this compatible with WI-10.10's `hdparm -S` policy. Factored into
`common.sh mount_options_for` so backup.sh and the guard share one implementation.

**Verified on the running V3 VM**, not just locally: units installed and enabled,
timer registered and firing; report **UNHEALTHY** with the path unmounted →
**healthy** after mounting a tmpfs there → UNHEALTHY again after unmount, with
`library-mounted` unaffected throughout; `backup.sh` preflight passes on the
mounted path, and on the unmounted one exits **1** with nothing created under the
target. Also unit-tested in WSL: the three preflight branches, the guard's
label/check-id wiring, and the unit's `BACKUP_TARGET` parsing (inline comment,
quoted value, missing file, unset key).

*Method note:* the VM's Default Switch lease moved mid-session (WSL recreated its
virtual network on a new range), so the gate VM is now at a different address —
find it by scanning the current `vEthernet (Default Switch)` subnet for port 22
rather than trusting a recorded IP.
