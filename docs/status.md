> **2026-09-13 panel audit:** [current findings](../../HomeHub/docs/PANEL_CURRENT_2026-09-13.md) take precedence over
> older deployment claims below. No gate or requirement was changed.
> Detailed coordination awaits Owner feedback.

# Project Status — Blackboard

Live coordination for the gated process (see [process.md](process.md)). Keep the
**Current State** header short and current; append the audit log below (newest
last) — it is the record, not required reading for every pass.

---

## Current State

**2026-10-27 — FileBackup's 2026-10-26 data-loss review, HomeHub's side (`filebackup-review-contract`: the six `filebackup-bounded-contract` commits and this work, replayed 2026-10-27 onto `IceDrive-DesktopDirection` 6318772 with the ids it introduced renumbered past the 2026-10-26 reservations: SR-122 -> SR-128, LLR-1320..1324 -> LLR-1356..1360, TC-1490..1496 -> TC-1541..1547; that branch, 35f5faf, stays as the record; not merged, not deployed, nothing sent to the hub).**
Sources of truth (FileBackup, read-only): `docs/plans/bounded-run-loop.md` §13 calls C36–C44, `docs/plans/single-store-layout.md` L1–L6,
`docs/reviews/2026-10-26-data-loss/consolidated.md` Outcome table. FileBackup's own `docs/interfaces.md` was not treated as final.
- **Part 1 — FileBackup behaviour that is built or ruled (commits `971d4c2`, `2d67564` and the trace commit; pullable on its own):**
  the wrapper's exit messages and README carry: exit 4 also = a source empty at the start while the latest state holds rows (C39;
  FileBackup branch commit `83d0822`, unmerged); exit 1 also = damaged objects set aside into `changes/Verify/` with `[DamagedObject]`
  lines in `backup.log` (C37; `428d53a`, unmerged), which the wrapper now **counts** after an exit 1, and case-only names (C42, being
  built); exit 2 also = an unloadable dependency, and prune/snapshots resolve the store read-only without the library, a missing store
  being 2 (`3756974`, on `BoundedRebuild`); prune `Pruning_<name>` leftovers finished by the next prune (C41; `7f47428`, unmerged);
  `-RepairStorage` holds the lock and honours `-WhatIf` (`ce068bc`); plain verify reports `DamagedObject` (C37); restore: NUL-torn
  manifest ≥ 1 (C38; `ba737eb`, unmerged), host problem on a row with a DataPath = 4 (A7, `4fb7689`); kit stays 14. C36 (flush
  before rename/delete) changes no interface. NagLight mapping unchanged: only backup 0 + passing verify advances `lastSuccess`.
  Traced: LLR-1356 and TC-1541 amended, **TC-1546** new (damaged-object count).
- **Before anything here is deployed:** (1) the FileBackup image rebuilt from `BoundedRebuild` **after** the review's fix batches
  (C36–C42) merge there; (2) **C43's follow-up in that image before any Verify or prune touches the hub's store** — the wrapper's
  step-8 verify gate runs **automatically** (weekly, `-Deep` monthly) and nothing on this branch stops it (not disabled silently, by
  instruction). What would gate it: `--verify none` in the unit's `ExecStart`, or a knob step 8 reads, until that image is deployed;
  `FILEBACKUP_MEM_LIMIT` would turn a runaway into an OOM kill of the container instead of the box. Prune is manual only.
- **Open questions for the Owner:**
  (a) prune/snapshots with the **store drive unplugged**: exit 2 as built, or 4 "retry later"? (FileBackup review item B, open there.)
  (b) **C43 gating**: gate step 8 now (which way), or deploy only once the follow-up is in the image?
  (c) **C36 / power safety:** both hub drives are USB NTFS through ntfs-3g (FUSE), which journals none of its own writes, so a power
  cut can damage the filesystem itself, not just the last files; FileBackup's flushes cannot fix that. For the clean start, is the
  backup drive to be **ext4** (more power-safe), given it would then not be readable on Windows? (Found 2026-10-27;
  `homehub-backup.timer` is enabled, 03:00 nightly.)
  (d) HomeHub registers **no `Consumes` IF row for FileBackup's IF-001**; the contract lives in SR-018/SR-128 and the README. Add one
  (repo-local id, counterpart IF-001)?
  (e) **Found, not fixed (outside this list):** the bounded-contract wrapper's step 4 pre-creates `<store>/backup/`, so FileBackup's
  C26 refusal (a *missing* BackupPath beside snapshots, never a fresh full backup) can never fire on the hub. Matters only if Part 1
  ships without Part 2.

**2026-10-24 — the library backup on FileBackup's bounded-run-loop contract (`filebackup-bounded-contract`, not merged, not deployed).**
**Requires a FileBackup image built from FileBackup's `BoundedRebuild` work, which is not in a released image yet.**
- **SR-128 / LLR-1357..1360:** one writable `/store` bind holding `backup/` and `changes/` replaces `/state`,
  `/backup` and `/changes`; `filebackup.json` drops `SourceStatePath`. The wrapper refuses while backup/ or changes/
  is a separate mount, or while the old three-mount store still holds data (step 2b). `library-store-migrate.sh`
  is the explicit migration: two `rename(2)` calls, never a copy or a delete, refusing other filesystems or
  mounts, two stores, symlinks, the root filesystem, a held lock or a running container; idempotent. After every
  completed run the wrapper publishes a system-disk copy of the latest manifest, which `frame-freshness.sh` (IF-035)
  now reads instead of the retired `/state` record. TC-1542..1545.
- **SR-018 amended / LLR-1356:** backup exits 0/1/2/4 and verify 0/1/2. **Only backup exit 0 plus a passing
  verify advances `lastSuccess`;** exit 1 ("completed, files not captured") still runs the verify gate, then fails
  the unit. A 3 is reported as an older image. TC-1541.
- **SR-049 amended / LLR-1004, TC-1065:** an old engine's `Temp` is a note naming the recovery event, not a
  refusal (the bounded engine never stages in Temp; refusing would block every backup).
- **Fixed in passing:** the documented prune dry run (`FILEBACKUP_DRY_RUN=1 docker compose run …`) ran the real
  prune — compose does not pass a host variable into the container (measured). It is `-e` now (TC-1542 guard).
- **Assumptions to confirm (decided unattended):** (a) branch cut from `IceDrive-DesktopDirection`, the
  integration branch, not `main` (803 commits behind, no FileBackup integration); (b) exit 1 is not a verified
  success, and the verify gate still runs after it; (c) exit 4 is retried by the next timer run, no extra
  retry; (d) the weekly verify stays report-only, so `DuplicateObject` findings left by the old engine fail
  verify nights until someone runs the one-time `-e FILEBACKUP_REPAIR=1 … verify` (README "Verify findings");
  (e) store default `/mnt/backup-drive/library-store`; (f) SR-128 is traced to SN-014 for want of an SN for the
  library backup itself; (g) IF-006 cannot show warn vs error for the library lane, so 1, 2 and 4 all show as the
  age growing; (h) comment-only drift in `autoinstall/user-data` (5c, "/state, /backup, /changes") and
  `firstboot.sh` (C61, "stale Temp refuses later backups") left alone, per the 2026-10-19 no-drift precedent.
- **Not proven:** the image, a migration on the hub, frame-freshness on the hub. Cross-repo: HomeHub's
  materialiser/FieldSchema should emit `FILEBACKUP_STORE` and drop the three retired keys (the defaults match).

**2026-10-23 — nightly backup into the unified lane (`nightly-backup-feed`).** `sim/mini-serv-sim/run-ingest-sim.sh`, `run-volume-sim.sh` and `run-drivepower-sim.sh` were retargeted from the retired `/api/feed` `ok` bodies to `nightlySuccess` (success: a post; failure: none; each has a positive success scenario) but were never executed, only `bash -n`-checked. The hermetic `stack/backup/tests/nightly-success-feed.test.sh` is what proves the behaviour.

**2026-10-22 — HomeHub plan W3b, on `w3b-recorder-scratch-watches` (not merged, not deployed).**
- **SR-088 amended / LLR-1290..1292:** `homehub-watch` gains two kinds. `newest-file`: the newest file
  matching an absolute glob is THRESHOLD old, or nothing has matched for THRESHOLD; never follows a
  symlink, at most 10 000 entries and 10 s, and a bound hit fires. `scratch-identity`: runs
  `homehub_scratch.py status` (the guard's own module, never re-implemented) and fires when it has not
  said mounted + identity_ok + prjquota on two runs in a row, or cannot run. Three rows, each gated by its
  component's switch: `door-segments` (15 min, the open `.ts.part` counts), `scratch-mount` (5 min),
  `meeting-pull` (`unit-success`, 30 min). Thresholds take `0m` from the override file for live drills
  that stop nothing (README "The W3b drills"). TC-1460..1466.
- Not proven: any of it on the hub (deploy and drills are the coordinator's; steps in HomeHub FOLLOWUPS).

**2026-10-22 — HomeHub plan W10, lane W10-timer, on `w10-frame-timer-cadence` (not merged, not deployed).**
- **SR-112 / LLR-1255:** `wall-sync-frame.timer` carries the Owner's 30-minute cadence itself
  (`OnUnitInactiveSec=30min`) plus `OnActiveSec=3min`, which arms from the timer's own start. The panel's
  drop-in `10-cadence.conf` (2026-09-15) reset the whole trigger list with its empty `OnUnitInactiveSec=`,
  deleting `OnBootSec=3min`; the inactive trigger alone never arms for a service that has never run, so the
  timer sat `elapsed`, `NextElapseUSecMonotonic=infinity`, and never fired (C47). Reproduced on systemd 255.
  TC-1420, TC-1423.
- **LLR-1256:** firstboot 5c retires that drop-in only by its sha256 (to `/var/backups/wall-panel/...`),
  leaves and names anything else in the directory, then reloads. TC-1421 (TC-1363 updated).
  **Depends on a HomeHub change to ship safely:** the paired release's rollback set must hold each
  declared unit's `.d` directory (HomeHub `panel_release_remote.system_targets`), or a release that fails
  after firstboot restores the old one-minute unit without the drop-in (review finding F1, reproduced on
  systemd 255 with HomeHub's real backup and rollback phases). Merge that HomeHub change before releasing this.
- **LLR-1257:** firstboot 8 restarts the timer when it is elapsed (a daemon-reload does not re-arm one,
  measured) and starts it when inactive. TC-1422.
- Not proven: any of it on the panel (needs the paired release; coordinator's steps in HomeHub FOLLOWUPS).

**2026-10-22 — HomeHub plan W10 shortlist, lane W10-sA, on `w10a-shortlist` (not merged, not deployed).**
- **SR-100 / LLR-1210:** Sure has its own owner, `stack/sure/homehub-sure.service` + `sure-unit.sh`,
  switched by `SURE_ENABLED` (exactly `true`); firstboot 4-pre-a2b installs it, enables it and queues its
  start; `sure` is refused in `COMPOSE_PROFILES` by the bake and the resolver, which now select the fork by
  the knob (they keyed on the profile, so every bake since 2026-10-18 had silently dropped it). TC-1360..1362.
- **SR-101 / LLR-1212:** wall-firstboot step 5c installs the twelve unit files only user-data placed, so
  the paired release lane can update them. TC-1363.
- **LLR-1213:** the AI CLI hands claude its schema inline (`--json-schema` parses JSON; it was handed a
  file path and every request died 502). TC-1364, and TC-1365 runs the real CLI with no credential.
- **LLR-1214:** panel-telemetry drains the host's transition ring and judges freshness on CLOCK_BOOTTIME.
  TC-1366..1367.
- **LLR-1211:** homehub-watch gains a `docker-restarts` kind; the shipped `restart-loops` row alerts a crash
  loop (C58's shape) that never reads unhealthy. TC-1368..1369.
- Not proven: any of it on a real box (deploys, a lab reboot with Sure, a signed-in `POST /v1/ask`, a panel
  suspend). The coordinator's steps are in HomeHub FOLLOWUPS.

**2026-10-22 — SR-093/SR-094 (SN-049, OfficeWallNaglight WSN-098): the panel's power button asks before it
shuts down. Branch `w5-power-button`, NOT merged, NOT deployed.**
- `wall-power-button.service` runs `wall-power-button.py` inside `systemd-inhibit --what=handle-power-key
  --mode=block`; one press publishes `armed` in `/run/wall-power-button/state.json` (IF-067) for the kiosk and
  asks the new `wall-sleep.sh power-button-wake` arm to light a dark display; a second press within 5 s runs
  `systemctl poweroff`; presses are de-duplicated across the two `Power Button` devices only. No kiosk
  acknowledgement within 2 s raises a journal ALERT and, off the press path, posts it to the hub's ntfy through
  `/usr/local/lib/wall-panel/panel-alert` and a byte-identical copy of W3a's `homehub-alert.sh` (SR-094,
  LLR-1188/1189; the hub's address from the new wall.env knob `HUB_IP` or, for a wall.env that predates it,
  read-only from the single IPv4 nameserver in the installed netplan; journal-only with neither).
  The window runs on CLOCK_BOOTTIME, so a suspend ends it; a device that will not open stops the guard.
  firstboot step 8h, knob `WALL_POWER_BUTTON_GUARD` (default true), checks the inhibitor in
  `systemd-inhibit --list`.
- `scripts/assert_wall_capabilities.py` names the panel's new site file `js/power-warning.js`, so this branch
  and OfficeWallNaglight `w5-power-warning` must merge together (TC-1232 compares the two).
- Not proven: nothing ran on the panel; that logind honours a sessionless service's inhibitor, the warning on a
  dark screen and a real press are the installed checks and the Owner's acceptance.

**2026-10-22 — SN-048 specified and built on `w3a-hub-monitoring` (HomeHub plan W3a); NOT deployed.**
SR-085..SR-91: the shared alert helper (`stack/monitoring/homehub-alert.sh`, now also rustdesk-check's
transport), the 30 s telemetry record with thermal/throttle/frequency alarms, the 15 min NVMe-only
low-disk alarm, the 5 min watch table, the 60 s liveness probe with one evidence bundle per incident,
firstboot step 6h with one knob per component (unset = on), and the panel's freeze watchdog taken into
the wall image byte-identical (wall-firstboot 8g, SR-090). Record and alert only. Deploy, the live drills
and the panel system release are the coordinator's (HomeHub FOLLOWUPS).

**2026-10-22 — SR-084 built on `w2-touch-filter-statedir` (not merged, not deployed).**
- `wall-touch-filter.service` gains `StateDirectory=wall-touch-filter` (`StateDirectoryMode=0700`) and nothing
  wider (LLR-1142); `configure-touch-filter.sh` OFF deletes `protection.json` from it (LLR-1143); the unit still
  reaches the panel only through firstboot, so the system lane carries it (LLR-1144). TC-1265..1268 in
  `tests/test_touch_filter_persistence.py`. The application half is OfficeWallNaglight WSN-099
  (`touchfilter/persist.py`); both ship in one paired release (HomeHub plan 2026-10-22, W2).
- Not proven: anything on the panel (installed unit, restored protection after a reboot).

**2026-10-19 — Correction (records only; nothing deployed by this entry).**
- **Sure is live.** The 2026-10-18 entry below says "Sure is NOT started". That was true when written and is
  superseded: Sure went live on the hub later on 2026-10-18, started by HomeHub's run-sheet script
  (`docs/deploy-runs/sure/sure-start.sh`), not through `COMPOSE_PROFILES`. Evidence: HomeHub `open-items.md`,
  the "Recounted mechanically 2026-10-18 (night)" note ("Sure went live (A49 stays open for the bridge
  decisions)"); HomeHub `FOLLOWUPS.md`, "2026-10-18 (late night)" ("Sure: Running") and "2026-10-19 —
  Carried by the documentation clean-up" (Sure has no owning unit yet; a `homehub-sure.service` with a
  `SURE_ENABLED` switch is owed, and reboot behaviour without the profile is unproven).
- **Pathing pointers.** `Personal\deploy\storage-map.md` (OI-11 and OI-14 below) and the
  `Personal\homelab\…` paths in the open-items register below no longer exist. The pathing SSOT is
  HomeHub `scripts/deploy/storage-map.md`; the secret-handoff design and tooling are HomeHub
  `SECRET_HANDOFF.md` and `scripts/deploy/` (`FieldSchema.psd1` included); `OFFICEWALL_BOOTSTRAP.md` is
  archived at HomeHub `docs/archive/2026-09-18/`. Live docs were repointed on branch
  `docs-cleanup-2026-10-19b`; the register below is left as written.
- **Docs clean-up on `docs-cleanup-2026-10-19b`** (not yet merged): AGENTS.md refresh, the stale stack
  READMEs, a generated IF index in `docs/interfaces.md` (`scripts/gen_if_index.py`, harness step
  `if-index`), dated status lines on the gateway runbook, the audio-routing design and `architecture.md`.

**2026-10-18 — DEPLOYED to the live hub: `6d11843` (this branch) via `deploy_stack.py --apply --run-firstboot`.**
The Owner approved the full lane. Its run-sheet, pre-deploy script and rollback script are in HomeHub
`docs/deploy-runs/2026-10-18/`, reviewed by Codex gpt-6.1-sol to GO. This deploy ships:
- the admin portal (admin-auth + homepage);
- the DNS-console fence;
- the always-on, record-only half of hub recovery. That half is now deployed. The supervisor stays OFF
  (`HUB_WATCHDOG_ENABLED` unset), so the HELD status below still covers it, as does the lab rehearsal;
- the RustDesk listener check (merged `785e153`, Sol APPROVE);
- the weight-setup retry and the AI CLI start on its install boot (merged `e4f09d7`, Sol APPROVE);
- the FileBackup slice and memory limit (the library-backup timer stays disabled).

The exit node is advertised and waiting for the Owner's console approval. Sure is NOT started. Its run-sheet
(HomeHub `docs/deploy-runs/sure/`, Sol GO) starts it explicitly and never through `COMPOSE_PROFILES`. A
profiled Sure would make firstboot's every-boot bulk `up -d` wait on its databases.

Verified after the deploy:
- all 79 files were verified by hash;
- `healthcheck.sh` passed every check;
- `admin-portal-live.sh` gave 65/0;
- the DNS-console fence re-asserted after a `tailscaled` restart.

The dev PC's remote start is proven, by the Owner's statement 2026-10-18. `DEVPC_WAKE_URL` is still unset.
Details are in HomeHub FOLLOWUPS 2026-10-18.

**2026-10-10 — Owner accepted the nine hub-recovery deviations; branch `hub-recovery-watchdog` merged into the working line; NOT deployed.**
Owner ruling 2026-10-10: deviations 1-3 and 5-8 as built, 4 and 9 in their corrected form, plus the
quarantine-survives-reboot fix (commits 81046b3, b8e57f5). Deployment is HELD. NOT RUN: the lab rehearsal
(scripted kill-and-recover test in the virtual lab) and the on-hub window. Deviation 2's basis (the SSH
out-of-memory setting measured on the hub 2026-10-05) is to be re-measured before the phase that relies on it.

**2026-10-10 — sim oauth2-proxy pin v7.6.0 -> v7.15.2; `SIM_ADMIN_AUTH_IMAGE_TAG` retired (FOLLOWUPS A7 bullet 3), branch `fix-sim-oauth2-proxy-pin`.**
SIM ONLY: `sim/.env.sim` `OAUTH2_PROXY_IMAGE_TAG` now v7.15.2 (tag, not digest: the repo pins tags) so
tracker oauth2-proxy and admin-auth share one image, as in `stack/`; the admin-auth image override and
the knob are gone (compose, env, README). Production pin (`stack/.env.example`) unchanged. Exercised
with the real `sim/validate-sim.sh` on WSL Ubuntu Docker, before and after: 25 PASS / 5 FAIL both
times, the identical set; check 4 (full headless Dex login to the tracker) and the admin-portal
checks pass on v7.15.2. The 5 FAILs are pre-existing and not oauth2-proxy (check 5 isolation x2,
check 6 unified state, check 8 non-panel 403 x2); left alone. v7.15.2 logs one new startup warning
on the tracker proxy: `--reverse-proxy` without `--trusted-proxy-ip` trusts all X-Forwarded-*
(stack/docker-compose.yml, base file, not changed here). NOT PROVEN: real Google provider, a
re-export of the offline image bundle.

**2026-10-10 — `wall-line-in` skips in BUS mode only (FOLLOWUPS B3), branch `fix-b3-line-in-bus-mode`.**
A second `ExecCondition=` (`!= bus`, the visualizer's mode file and `trigger` default) makes a udev
start in bus mode a skip, not an `Unknown PCM speaker_shared` failure. Trigger and panel modes keep
line-in (panel is the documented fallback). `wall-audio-mode` already disables the unit in bus mode
(`apply_bus`), so no capability is removed: analog line-in is retired there by design (S/PDIF,
`asound-bus-mode.conf`). `start_if_enabled` would restart a unit active before the switch even
after `apply_bus` disabled it, but the enablement ExecCondition already makes that start a skip,
so no script change was made. By reasoning only, a skipped condition still counts as a start
attempt toward StartLimitBurst, as the existing enablement condition does; not measured.
Evidence: `tests/test_wall_audio_reenumeration.py` asserts both conditions and runs the guard's
sh body for bus/trigger/panel/absent. BUILT, NOT DEPLOYED: reaches the panel only by a system
release. NOT PROVEN: never run on the panel; FOLLOWUPS B3's "enabled" claim is unverified against
the live panel (`systemctl is-enabled wall-line-in` is the next read).

**2026-10-05 — Draft, built and hermetically tested, NOTHING deployed or enabled: the hub recovery capability (SN-046, SR-049..SR-51; LLR-994..1018; TC-1048..1086), branch `hub-recovery-watchdog`. Owner D11 (quarantine) ACCEPTED. Updated 2026-10-10 for a second review round (three MAJORs, below) and merged up to `IceDrive-DesktopDirection`; still NOT merged into it (Owner decision 4 gates that).** Implements HomeHub `docs/design/HUB_HANG_WATCHDOG_DESIGN_2026-10-05.md` rev 3 in [stack/recovery/](../stack/recovery/README.md). Branched after merging GitHub's admin-portal and exit-node work into the base (7fc16ca).

* **Always on, record-only:** the 30 s `hub-pressure` sample (C1), the late-shutdown stamp and the five-class boot classifier with atomic commits and the brake (C5), a persistent journal pinned at 4G and `hung_task_warnings=-1` (C6), and the library backup in its own `homehub-filebackup.slice` with its `memory.peak` logged after every run (C4). firstboot step 6g installs them every boot.
* **Opt-in, OFF:** `HUB_WATCHDOG_ENABLED=false`. `HUB_WD_TIERS=contain` (P3) gives the measured-pressure early kill, Docker reconciliation, quarantine and the sshd restart; `full` (P4) adds the graceful reboot, the hardware watchdog with its 300 s shutdown hand-off and `kernel.panic=10`. `setup-recovery.sh` converges on or off; any change to a running supervisor (off, full -> contain, a different device) goes through the VERIFIED disarm first, and a disarm that cannot be proven changes nothing.
* **Deviations from the design, each deliberate and recorded in the code beside it:**
  1. **The stamp unit is `Before=docker.service`, not `After=`.** systemd stops units in reverse start order, so the design's `After=docker.service` would stop the stamp BEFORE Docker - the opposite of its stated goal "stops after Docker", and a hung Docker stop would hide behind a written stamp.
  2. **No `OOMScoreAdjust=-1000` drop-in for `ssh.service`.** Measured on the hub: OpenSSH already runs its listener at -1000 and restores each session to the value it inherited (0). A unit-level -1000 would be inherited by every session, making anything run over SSH unkillable. tailscaled does get its drop-in. *2026-10-10 review: judged UNVERIFIABLE from the repo - it rests entirely on the 2026-10-05 live-hub measurement of OpenSSH's own scores. Unchanged; re-measure on the hub (or the lab VM) before P3 relies on it.*
  3. **The victim scan covers `homehub.slice` as well as `system.slice`**, because C4 moves the backup - the 10-04 culprit - into `homehub.slice/homehub-filebackup.slice`, out of reach of a `system.slice`-only scan.
  4. **D10 switches on evidence, not a knob - and the evidence is now per device.** `bootstatus` reads 0 both when WDAT's status action is unsupported and when there was no reset, so support cannot be read from sysfs. *2026-10-10 review: the original form was judged UNSOUND* - one global `bootstatus-proven` latch, so after WDAT once reported CARDRESET a later fall-back to softdog (the knob, or wdat_wdt failing to load) had every softdog reset excluded as "unexplained" for ever and the brake could never engage. **Changed (81046b3):** the latch records the kernel identity (`/sys/class/watchdog/<dev>/identity`) of each device that reported CARDRESET; unexplained boots stop counting only while the device `recovery.env` selects has one of those identities. A different device, no device (contain, none, off), an unreadable identity, or the old unbound latch all count them again, retroactively within the 24 h window; the classifier and the supervisor's 10-min re-evaluation decide through one function (LLR-1017, TC-1085). **New state:** sound by test; the real identity strings (`wdat_wdt`, `Software Watchdog`) are from kernel source, not read on the hub, where no watchdog module was loaded.
  5. **Two knobs the design's §5 table lacks:** `HUB_WD_TIERS` (contain|full, default contain) so P3 can ship without P4, and `HUB_WD_RESET_DEVICE` (auto|wdat_wdt|iTCO_wdt|softdog|none).
  6. **The previous boot id comes from the classifier's own `last-boot` record**, with `journalctl --list-boots` only as a fallback: the journal is what a hard reset most likely leaves dirty. The capability's first boot is class `first`, uncounted.
  7. **The classifier and the sampler are Python, not shell**, so they share one cgroup scan, one Docker-metadata reader and one atomic write with the supervisor.
  8. **Filled-in details:** a kill window needs at least half the samples it could hold (else two pressured samples around a long gap read as 100 %); clear needs 60 s and >= 3 calm samples; a container Docker's metadata cannot identify is skipped (it could be a recreated protected one); quarantine still writes `cgroup.kill` so relief does not wait on Docker, and spends no budget; `Restart=no` on the supervisor so a crash reaches L4 and is counted; `braked` still kills (design §4) where the brief said report-only; report-only is reserved for an unaccounted boot or a setup failure.
  9. **earlyoom is in `packages.list`**, configured and enabled only by the knob and disabled every other boot. *2026-10-10 review: the accepted window was judged UNSOUND* - the postinst enabled the unit, so a fresh image ran earlyoom with Debian's defaults through firstboot's image load and Docker pulls, before any opt-in and with the knob false. **Changed (81046b3):** user-data late-command 3c-oom disables the unit right after the baked install (guarded for an image without the package; a failed disable warns and 6g still converges it off), and setup-recovery.sh `converge_on` stays the only enabler. `validate_config.py` check 6c fails if the disable is missing or not after the install (LLR-1018, TC-1086). **New state:** no window; earlyoom first runs when the knob converges ON. The baked apt repo still needs a re-export.
* **Verified on the hub (read-only, 2026-10-05):** kernel 6.8.0-139, systemd 255.4, Python 3.12.3; Docker cgroup driver systemd on cgroup v2, 20 `system.slice/docker-<id>.scope` with `cgroup.kill`, `cgroup.events`, `memory.peak`; `DefaultMemoryAccounting=yes`; container metadata in `/var/lib/docker/containers/<id>/{config.v2,hostconfig}.json`; ACPI `WDAT` table present, `wdat_wdt`/`iTCO_wdt`/`softdog` modules present but none loaded and no `/dev/watchdog*`; `kernel.panic=0`; ssh is socket-activated; sshd listener at -1000, sessions and tailscaled at 0; pstore mounted and empty; journal persistent, 1.4G; earlyoom 1.7-2 available from noble/universe. The ioctl numbers match `linux/watchdog.h` (compiled check). All six units pass `systemd-analyze verify` on systemd 255.
* **NOT proven, and owed before each phase:** lab TC-1071 (L-1..L-13, L-EO - none run); the on-hub window TC-1072 (WDAT reset on expiry, `bootstatus`, L-6, a graceful round trip); that Docker accepts the custom `cgroup_parent` slice and the slice outlives `--rm` containers (L-13, gates P1's peak capture); mlockall under `MemoryMax=128M`, real SCHED_RR and `LimitRTTIME` behaviour; the CPython vfork/posix_spawn audit (§3.0); whether PID 1 with `WatchdogDevice=` set and `RuntimeWatchdogSec=0` touches the device on a daemon-reexec while the supervisor holds it; firstboot 6g has never executed on a real boot; the apt re-export with earlyoom. Added 2026-10-10: the 3c-oom late-command has never run in a real install (V3 gate); `enforce-quarantine` and the label lookup have never run against a real Docker (lab L-2b should end with a reboot); the watchdog identity strings on this board.

* **Code review (gpt-6.1-sol, high), 9 findings, all accepted and fixed - each with a test and mutation checks:**
  * **C1 (blocker)** full -> contain / device -> none / a report-only replacement left the old instance's 300 s hand-off running on a healthy box. Setup now decides first, runs the verified disarm before writing anything, and STARTS the replacement; an unchanged re-run leaves the supervisor alone. Closing the rest of the class: a supervisor that will not arm takes over and stops a predecessor's still-active timer at start, and `reset-brake` no longer try-restarts.
  * **C2** disarm is now SIGUSR1 + a boot/invocation-bound authorisation; the supervisor disables the card, magic-closes and acknowledges ok only when sysfs reads inactive; the CLI stops the unit only after the ack AND its own sysfs read, else exits 1 with the supervisor still petting. An active device with no supervisor is disarmed directly (nowayout 0, verified). **SIGTERM never disarms any more** - a change from the design's "magic close on SIGTERM if authorised", made because a stop cannot be retracted once the disarm turns out to have failed.
  * **C3** both timeouts are checked against the device's sysfs min/max before opening; a failure after open disarms and verifies before going report-only, and a device that will not stop is held and petted, red.
  * **C4** boots.log is read strictly; only an absent file is an empty history; EIO or a corrupt line fails closed (uncommitted, braked, kernel.panic=0, report-only), same-boot re-runs included.
  * **C5** a container whose metadata is missing, corrupt or unreadable has no identity and is never chosen.
  * **C6** atomic writes use pid+thread+counter O_EXCL temp names; quarantine snapshots are numbered and written under a telemetry-only lock, newest wins.
  * **C7** the running tailscaled's oom_score_adj is set and read back before earlyoom may start; if not, earlyoom stays off.
  * **C8** an unmeasured sample or a gap breaks the calm streak.
  * **C9** the running supervisor re-reads the accounting every 10 min: an expired brake re-arms (through C3's validation), an engaged or unreadable one releases the device and sets kernel.panic=0.
  * Also found and fixed while doing C3: full tier with `HUB_WD_RESET_DEVICE=none` had become report-only; it is graceful reboots without a device again.
  * New limit, recorded: if both telemetry threads are blocked, the 10-minute brake re-evaluation stops and the current mode holds.

* **Second review (Sol, medium), 2026-10-10: three MAJORs, each verified against the code and valid; fixed in 81046b3, each with a test and mutation checks:**
  * **Quarantine did not survive a reboot.** `docker update --restart=no` holds against dockerd, but firstboot's `docker compose up -d` starts every stopped service whatever its policy, so a quarantined hog came back on every boot while `status` still called it quarantined (the supervisor only reloads its dict, and `key not in self.quarantined` blocked re-quarantine). The next command after that `up` is now `homehub-recovery enforce-quarantine`: it reads only `quarantine.json` (so it holds with the supervisor off) and stops each entry's CURRENT container, found by its compose service label with the recorded id as fallback. The Owner's way out is unchanged - `release` removes the entry, and now resolves the container the same way, so a service the boot recreated under a new id is the one released. `status` shows quarantine when the supervisor is off. Known limit: the service runs for the seconds between the `up` and the enforcement. LLR-1016, TC-1084.
  * **D10's proof** - deviation 4 above.
  * **earlyoom before opt-in** - deviation 9 above.

* **Third review (Sol), 2026-10-10, of the fixes above.** Confirmed: the device-bound proof fails toward counting in every case (missing/empty identity, stale device, different identity, legacy latch) and survives renumbering; the earlyoom guard is correct; f1565aa kept both parents' rows. Triaged findings, each verified against the code:
  * **ACCEPTED - enforcement ran only if Compose succeeded.** Under `set -euo pipefail` a partial `docker compose up -d` failure (one service's pull or health check) exited firstboot before `enforce-quarantine`, leaving a hog it had already started running. The up's status is now captured, the enforcement runs either way, and the up's failure is then propagated as `set -e` would have. Quarantined services are deliberately NOT left out of the `up`: the stack starts as a bare `up -d` whose service set comes from the active profiles, so an explicit list would have to re-derive it and stay in step with every future profile, and a `depends_on` would still start the service; stopping it straight after has no such coupling. Test: the firstboot block is RUN under bash with compose exiting 0 and 1.
  * **ACCEPTED - the lookup could match an unrelated container.** The service label was the only filter, and any match skipped the recorded-id fallback. The supervisor now records the victim's compose project and service with its full id; a compose victim is looked up by project AND service (recorded id as fallback), a non-compose victim is its recorded id only, and `release` uses the same lookup. Test: another project's same-named service is neither stopped nor released.
  * **REJECTED, considered and out of scope - a concurrent `release` racing the boot's enforcement.** It needs the Owner to run `release` in the seconds firstboot's enforcement is running; that is the Owner's own deliberate command, and the worst outcome is a stopped, no-longer-quarantined service the Owner simply starts again. No lock added.
  * **REJECTED, considered and out of scope - migrating "legacy" quarantine records with no `project`/`service` fields** (round-4 review). That format existed only on this unmerged branch, and the watchdog has never been deployed, so no such record exists on any box. A migration would guard a state that cannot occur. If a pre-release build is ever deployed for testing, clear `quarantine.json` before upgrading.

Evidence after the third review round (2026-10-10): 133 recovery pytest cases pass on Windows Python 3.8 (1 POSIX-only skipped) and 134 on WSL Python 3.12; four mutations each turned a test red (the up's status left to `set -e`: the run test and the order test; the propagation disabled; a service-only filter; the project not recorded). `validate_config.py` PASS (90 checks, 0 FAIL); `python scripts/check.py` (tier all) PASS: unit-tests 2640 passed, 31 skipped, 0 failed.

Evidence after the second review round (2026-10-10, on the merged tree): 129 recovery pytest cases pass on Windows Python 3.8 (1 POSIX-only skipped) and 130 on WSL Python 3.12; `setup-recovery` 18/0 as a non-root user (as root under WSL TC-1082's refused-write case fails because root's write is not refused - the same at the pre-fix HEAD, an environment artifact); six mutations each turned their test red (quarantine targets ignoring the label: 2 tests; the firstboot call removed; an unbound proof; the old file-exists test in `evaluate_brake`; the earlyoom disable removed: check 6c FAIL). `validate_config.py` PASS (90 checks); `python scripts/check.py` (tier all) PASS: unit-tests 2636 passed, 31 skipped, 0 failed (the 9 tailscale failures recorded below are gone on the merged line); registry-integrity orphans=25, integrity=0.

Evidence after the review round: 122 recovery pytest cases pass on Windows Python 3.8 (1 POSIX-only concurrency case skipped) and 123 on WSL Python 3.12; `setup-recovery` 18/0 and `library-backup-state` 18/0; 39 further mutations (C1-C9, the orphan path and reset-brake) each turned their test red; `WDIOC_SETOPTIONS` 0x80045704 / `WDIOS_DISABLECARD` 1 checked against `linux/watchdog.h`. `check.py`: unit-tests 2584 passed, the same 9 pre-existing failures; the other three steps PASS.

Evidence at the first build: 82 recovery pytest cases pass on Windows (Python 3.8) and in WSL (Python 3.12, the hub's version); the hermetic runner reports `library-backup-state` 18/0 and `setup-recovery` 10/0; 40 mutations (30 core, 5 wrapper, 5 setup) and both new validate_config checks were each shown to turn their test red. `python scripts/check.py`: config-validate, registry-integrity (orphans=25, none new; integrity=0) and doc-navigability PASS; unit-tests 2544 passed and **9 failed, all pre-existing** in `tests/test_tailscale_subnet_router.py` from the merged exit-node work (`Path.write_text(newline=)` needs Python 3.10; the dev box's gate interpreter is 3.8). Also pre-existing and not investigated: 46 hermetic failures in four unrelated suites under WSL (icedrive profile, ai-cli guards, wall occupancy, audio-switch). `library-backup-state` had 7 failures at the base (its fixture devices did not exist under the new stale-device guard); fixed in 560b852.

(Superseded by the 2026-10-10 acceptance entry above.) Next action: Owner review of the deviations above (D11 is accepted; #4 and #9 changed after review, #2 still rests on the hub measurement), and Owner decision 4 before this branch merges; then lab L-13 before P1 ships; the lab must also exercise the verified disarm on softdog (L-11, L-12, and full -> contain on a running VM). Active gate remains G1.

**2026-10-09 — boot copies of firstboot now refresh themselves (FOLLOWUPS A7 bullet 1).**
`firstboot.sh` step 1a installs `homehub-firstboot.sh` and `powertune.sh` from the
`/opt` tree on every run; `deploy_stack.py` reports (read-only, never fails the run)
whether each boot copy is `current`, `stale`, `stale-line-endings` or `missing`,
using an exact sha256. Design: `docs/design/firstboot-boot-copy-refresh-2026-10-09.md`
(revised after review). Evidence: `tests/test_deploy_stack.py` 24 passed, mutations
on the ordering, gate and CRLF checks each turned their test red. Under LLR-992;
no new TC id added.
NOT PROVEN: not run on the hub. The first `--run-firstboot` after delivery is what
converges the boot copies; the hub's current sbin copies are unmeasured until then.
The `/etc` unit file refresh is deferred (needs daemon-reload).

**2026-10-09 — Record: the private LLM lane IS deployed and running (supersedes the "Not deployed" line in the 2026-09-19 entry below).** Established by a read-only SSH session to the hub on 2026-10-09 ~21:30 UTC (`docker ps`/`docker inspect`/`systemctl`/`sha256sum`; no secret, env value or key was printed, env names only). Gate unchanged (G1); nothing was changed on the hub or deployed by this entry.

* **litellm** container running, healthy, 0 restarts, started 2026-09-19 17:58 UTC. Image `ghcr.io/berriai/litellm@sha256:d295634e…b044b` (pin file says `v1.101.0`, pinned 2026-09-19; the digest matches `stack/litellm/litellm-image.pin`). Attached to the `stack_llmprivate` network only.
* **llm-gateway** (the separate FreeLLMAPI gateway) running, healthy, 0 restarts, started 2026-09-19 17:58 UTC. Image `ghcr.io/tashfeenahmed/freellmapi@sha256:04ce170a…870fe`; networks `stack_default` and `stack_llmprivate`.
* **homehub-litellm.service** and **homehub-llm-isolation.service**: both enabled and `active (exited)` (RemainAfterExit oneshots, as designed).
* **Deployed files match the repo byte for byte** (sha256 equal on the hub and in this tree): `stack/litellm/config.yaml`, `devpc_hold_hook.py`, `litellm-image.pin`, `stack/llm-isolation/llm-isolation.sh`.
* **The dev-PC hold is OFF as designed**: `DEVPC_WAKE_URL` is present in the container but empty (`DEVPC_HOLD_CAP_SECONDS` is set). So a cold dev PC fails fast rather than waiting.
* **Spine unchanged:** SR-047 is still `Draft`; deployment is not ratification. The "Next action" in the 2026-09-19 entry (Owner ratification of the SR-047 rows) has not been shown to be done by anything here.

NOT established: whether the isolation fence rules are *correct* (six iptables lines mention the lane; their content was not audited, nor was the `TC-1023..1041` set re-run against the live box); whether a request actually round-trips to the dev PC model; the value of `LITELLM_ENABLED` in `/opt/homehub/stack/.env` (that file is unreadable to the `hub` account without printing secrets, so the running state, not the knob, is the evidence); `llm-gateway`'s configuration, which is in a directory the `hub` account cannot list; when and by whom the lane was deployed beyond the container start time.

**2026-10-09 — Wall capability contract: the Grok mark (branch
`feat-ring-gauge-rail-capabilities`); gate G1 unchanged; nothing deployed.**
OfficeWallNaglight's ring gauge rail (its WSN-076) bundles xAI's
`vendor/brand/Grok_Logomark_Light.svg` and declares it under
`progress-gauges-v1`, so `scripts/assert_wall_capabilities.py` now declares it
in that capability (app and site) and admits it to the public-site inventory.
**RESOLVED 2026-10-11 by `94f5a12`** (merged as `0997367`; Sonnet medium review:
APPROVE). The contract now treats the panel's own manifest as authoritative
above a floor of 22 CameraOpt capabilities, and keeps the public-site inventory
exact. Not proven: the vmtest image path, which needs the lab. The finding as
written on 2026-10-09:
**FINDING, pre-existing and NOT fixed here:** this contract was already out of
step with OfficeWallNaglight `CameraOpt` before this change — seven
capabilities absent (`audio-switch-v1`, `bus-pcm-bridge-v1`,
`local-capabilities-v1`, `locked-gauges-v1`, `mutation-identity-v1`,
`projectm-trial-v1`, `tracker-event-log-v1`), four declared differently
(`panel-audio-v1`, `panel-tabs-v1`, `private-access-v1`, `sensors-v1`) and 27
site files outside `ALLOWED_SITE_FILES` — so the image path that calls it
(`vmtest/lib/common.sh` `assert_wall_artifact_contract` /
`assert_coherent_wall_release`) refuses any current wall artifact with or
without this change. Re-syncing it widens the public-site allow-list and is an
Owner/reviewer decision, not a side effect of this one. HomeHub's paired panel
release (`panel_release.py`) does not call this script. Tests: the four
capability/release suites 355 passed, 3 skipped; `check.py --tier smoke` PASS.

**2026-10-09 — Owner authorized Sure integration and default profile enablement.**
Merged `sure-stack` into `IceDrive-DesktopDirection`; `sure` is selected by
default in the deployer template and HomeHub materialization configuration.
Actual remains running and Finance-Auditor retains `DATA_SOURCE=actual`.
Gate remains G1; this is local configuration, not deployment or runtime acceptance.
Normal Sure secret preparation and the Finance-Auditor empty-as-absent image fix
must precede delivery. Validation results follow in the session audit entry.


**2026-10-09 — Sure phase-1 review round 1; active gate G1 unchanged.**
Right-base merge `91cd3a2` retains the admin portal and Tailscale exit-node
changes. Review fixes add loud optional-container skips, explicit version pins,
consistent default hostnames/Caddy logging, and corrected cross-repo and recovery
contracts. [Sure enablement and source evidence](../stack/sure/README.md) is the
review artifact. No gate advanced; nothing deployed or pushed. Next: Claude
re-review, then Owner activation and restore/key-escrow acceptance; FA image dependency blocks rollout.

**Merge-order blocker:** these FA Compose env mappings must not reach the hub
until FA's empty-as-absent fix for TRACKER_FEED_USER, SURE_API_KEY, SURE_URL and
DATA_SOURCE is in the FA image the hub builds. Current FA rejects the blank
feed user even on Actual. Static wiring/default checks do not prove runtime
compatibility. No Finance-Auditor file was edited.

Review commits: MiniPC merge `91cd3a2`, optional backup skip `faa6f34`, final
wiring/pins/contracts `9dad6e8`;
HomeHub `48f6e66` corrects minting/restore dependencies and case-sensitive profile
handling. The working-copy ending fix produced no content change: requested
HomeHub files match committed LF/BOM conventions. ExpectedOptionalAbsent keeps
the generated keys listed because Prep has not run since schema addition;
reasons now explicitly require removal once Prep has minted them. Optional does
not suppress that unattended minting. No Prep/mint/export was performed here.

Final checks: config validation ALL CONFIG CHECKS PASSED; full Python3.12 pytest
2487 passed / 30 skipped / 8 warnings in210.05s; smoke RESULT: PASS, 229 passed /
4 skipped / 2284 deselected in7.94s. Targeted Sure pytest 16 passed; mock-Docker
full backup pipeline 8 PASS 0 FAIL. HomeHub before:
188 discovered / 178 passed / 9 failed / 1 skipped in107.59s; after identical
counts in108.44s; final committed-stack after-check again 178/9/1 in108.3s.
The earlier two extra failures were caused by the old local
MiniPC base, not by a wrong brief: corrected by this merge. Same nine known
failures (AdminAuthCookieSecret; LitellmMasterKey; two LabRunner; five switch
matrix). ScriptAnalyzer unchanged 1 warning + 2 informational; edited PowerShell
parses and privacy gates pass. All three touched shells pass bash -n; edited
shell/Python files are LF. Trace:25 pre-existing orphans / integrity0; docs:
0 broken links with6 existing orphan-doc warnings. Python3.12 emits8 existing
Pandora tar extraction deprecation warnings; no test failures.

Owner acceptance items (one decision each; source paths are at pinned Sure):

- Accept `api.github.com` loopback: only the What's new/release-highlight UI loses fetching; no env off switch (pages_controller.rb:90; release_highlights_controller.rb:7; provider/github.rb:18,27,66).
- Bootstrap first household account then enable invite-only, leaving default-family ID blank to require codes; open is the env default and saved settings win (app/models/setting.rb:201-207,226-236; app/controllers/concerns/invitable.rb:9-17).
- NEVER ROTATE SECRET_KEY_BASE; preserve exact key through canonical DPAPI store, protected install .env/ISO and existing encrypted-vault export before ledger use; encrypted-column restores require it (config/initializers/active_record_encryption.rb:23-36; HomeHub SECRET_HANDOFF.md).
- Accept live attachment copies without alignment to the SQL dump; no quiesce is configured (stack/docker-compose.yml sure_storage; existing backup volume source; HomeHub storage-map.md).

Judgments/limits: current household Caddy sites are live from day one; no
profile-to-site gate exists. Sure's site requests a real certificate while its
profile is off and returns LAN 502; WAN remains 403. Optional missing/stopped
Postgres skips are explicitly nonfatal, unlike required path/volume missing
sources: other backups and retention finish. No fresh ledger is claimed. This
also means history is not frozen; same-day daily replacement/ordinary retention
can remove prior ledger archives. Preserve a verified archive/key before long
term disablement. Running dump or Docker query failures remain fatal.

Spine stays Draft: SR-060..66, LLR-1040..1046, TC-1100..1117, IF-030..034;
review adds TC-1115..1117 for missing/stopped/query-failure boundaries. CSV
prose corrected line by line; TC-1114 placed in order and edited rows re-read.
Original full WSL hermetic attempts were NOT green (unrelated unchanged suites; corrected attempt hit240s). That
aggregate was not proven; the Sure mock pipeline ran independently in Ubuntu.
Python3.8 cannot run imported upstream Tailscale tests using Path.write_text's
newline parameter. Python3.12 with existing Python3.8 site-packages runs those
checks (20 passed / 1 skipped); no dependency install or source workaround.

**NOT proven:** hub/panel startup, any actual image executable/probe, TLS/Caddy
adaptation, own login/invites, bank sync, FA blank handling/API/read scope,
runtime telemetry absence, real pg_dump or restore, SQL/attachment consistency,
key escrow/export, or full HomeHub materialisation. Nothing deployed, pulled,
run in Docker, contacted on a box, pushed, or secret-minted/printed. Classic
Docker image-store save/load of tag@digest drops RepoDigests; if sure is baked,
payload loading and digest-reference resolution still require Owner acceptance.

**2026-10-09 - `/sleep-verdict` binding fixed in source (SR-044 / LLR-978).**
On `fix-sleep-verdict-binding`, cut from `IceDrive-DesktopDirection` as requested;
`sure-stack` is unchanged. `main()` now calls `bind_session_reader(cfg)`, which
wraps the configured zero-argument session probe in `staticmethod`. The prior
explanatory comment is retained with an added descriptor-binding explanation.
Real Handler loopback GET tests cover a fresh detached reading (200 JSON permit)
and None (200 JSON deny, fail-closed to attached), using the production helper.

Evidence (actual output):

```text
python -m pytest -q tests/test_devpc_wake.py
28 passed in 1.09s
python scripts/validate_config.py
ALL CONFIG CHECKS PASSED
mutation: restore the plain lambda assignment, run the two HTTP cases
2 failed, 26 deselected in 1.21s
MUTATION CHECK PASS: both HTTP cases reject the original binding TypeError
python scripts/check.py
2413 passed, 30 skipped in 218.48s (0:03:38)
Traceability: SN=33 SR=47 LLR=94 TC=148 orphans=25 integrity=0
check_docs: OK - 20 doc(s), 66 intra-repo link(s), 0 broken (6 orphan warning(s)).
RESULT: PASS
```

The fixed source was restored after mutation. Both changed Python files are LF;
`git diff --check` passed. README Known defects now records the source fix dated
2026-10-09. HomeHub is unchanged: FOLLOWUPS A6's Phase 0 TypeError item should
be marked source-fixed / live verification pending, and A2's earlier wiring
note should acknowledge the binding correction. No Owner policy decision was
made. No push or deployment. Active gate remains G1; no gate advanced.
Next action awaiting Owner authorization: deploy this source fix and verify the
live endpoint. NOT PROVEN: live hub response, real session-agent integration,
and physical dev-PC sleep/wake behavior.

**2026-09-19 — Draft, nine review rounds complete, Owner ratification pending: two LLM lanes, one of which cannot reach a cloud provider (SN-043/SN-044/SR-047 and SR-044's last piece; LLR-985..991; TC-1023..1041).** One OpenAI-compatible proxy with exactly one deployment, no cloud credential and no database, plus a hold hook that waits while the dev PC wakes. Nine rounds with codex gpt-5.6-terra (medium) produced 31 findings; 30 were accepted and one was rejected with the rejection confirmed in a later round.

* **The property this lane exists for was CLAIMED AND WAS FALSE, and that is the finding the whole design turns on.** The first version argued the container could not reach a cloud provider because it holds no cloud credential and names no cloud endpoint. Measured against the pinned digest rather than inferred: the proxy serves 618 routes of which 32 are provider pass-throughs registered independently of `model_list`, and the Vertex handler FORWARDS CALLER-SUPPLIED CREDENTIALS when the proxy has none of its own — its own log line says so. A probe carrying an `x-goog-api-key` made the container open a connection to `aiplatform.l.rep.googleapis.com:443`. **The credential can arrive in the request**, so its absence from the config was never a boundary. The boundary is now `stack/llm-isolation/`, below the application, and `allowed_routes` is documented as defence-in-depth rather than the control.
* **The fence needed BOTH chains and the first version had one.** DOCKER-USER alone left the host bridge wide open: packets to the host's own bridge address are delivered locally, which is INPUT, and DOCKER-USER never sees them. The earlier text asserted `game-isolation.sh` covered it; that fence covers `172.28.90.0/24` and this lane is `172.28.91.x`. Measured on the live box, INPUT held a REJECT for the game subnet and **nothing** for this one — reaching SSH, cockpit and Technitium's admin console on :5380, which can rewrite every name the household resolves. A private lane that can reprogram DNS is not a private lane.
* **Two findings were deploy-blockers that only an EXECUTED test could reach.** (a) Under `set -euo pipefail` a `grep -v` that matches nothing exits 1, so the fence died silently at exactly the moment it had proved everything correct — on a correct box the unit would have failed and the lane never started. Every source-text test passed; the first test that actually ran the script found it immediately. (b) Every position parser used `awk '{print; exit}'` or `head -1`, which closes the pipe and can SIGPIPE iptables into 141, adopted by `pipefail` and fatal under `set -e`. The trigger is not a long chain but a match EARLY with a large tail — which is this box's measured shape, `ts-input` at INPUT position 1 with everything below it. Reproduced: survives 20 trailing rules, dies at 2000. The hub has about twenty today, and that margin is invisible.
* **Where the guards fire is now tested, not just where they are silent.** Both precedence checks, the reapply guard and the parser class had only ever been exercised in their passing state. They are now driven in the state where they must fail, and every one is mutation-checked: removing the guard turns exactly the matching test red and leaves the others green.
* **A reapply window, a docker restart and a lane that was enabled but never started** were the round-7 findings. Rebuilding the rules left the lane unfenced between the delete and the insert; a blanket deny now goes in first and comes out last, so every intermediate state is more restrictive than both rule sets. `systemctl restart docker` stopped the container while systemd went on believing a RemainAfterExit oneshot was active, stranding the lane until a human noticed (`PartOf=docker.service`). And firstboot ran `enable` without starting anything, so a provisioned box with `LITELLM_ENABLED=true` had no lane at all while the log said otherwise — started with `--no-block`, because a blocking start from inside firstboot is the 18-minute boot deadlock this project already paid for.
* **SR-044 is discharged in two places now, deliberately, and that is recorded rather than reinterpreted.** The requirement reads "held while the box wakes, falling back rather than erroring". The finance lane must ERROR on a cold box — substituting a cloud provider is the failure the arrangement exists to prevent — and the agents lane should answer from somewhere. So the hold lives in this proxy and the fallback stays in llm-gateway, which already has the catalogue. The system satisfies SR-044 for the agents lane; no single component does. The hook's refusal deliberately contains "503" and "unavailable" because the gateway in front classifies retryability by substring-matching the message, and a test asserts that against the gateway's shipped text rather than against our own file.

**Verified against the real hardware before shipping, not only against stubs.** Read from the hub 2026-09-19: INPUT has `ts-input` at position 1 and the remote-desktop blanket ACCEPT at 3, so this fence lands at 2–4 and above it; FORWARD has `ts-forward` at 1 and the DOCKER-USER jump at 2, neither an ACCEPT nor a RETURN; DOCKER-USER is empty, so this lane is its first user. Both precedence checks therefore pass on the real chain rather than only on the fixture.

**Not deployed.** `LITELLM_ENABLED` is false and `DEVPC_WAKE_URL` is empty, so the hold is off: a wake path that cannot wake makes every cold call wait out the cap before failing, which is worse than not holding.

Evidence: `python scripts/check.py` reports **PASS** at gate G1 — 2291 passed, 30 skipped; config-validate, registry-integrity (orphans=25, the clean-tree baseline; integrity=0) and doc-navigability all green.

Next action: Owner ratification of the SR-047 spine rows, then the deploy per `stack/litellm/README.md`. Active gate remains G1. **LLR-979..984 need nothing from the Owner.** They were destroyed by this session's `git checkout` and rebuilt from the shipped code under a [RECONSTRUCTED] marker that declared them unrecoverable; the authoring session then restored the originals verbatim from its own scratchpad (fd4dab4), so the marker and the ratification request are both withdrawn. Verified here against fragments captured before the loss: five of the six match byte-for-byte, including two details the reconstruction had got wrong, and all 34 CodeSymbols resolve in the shipped modules.

**2026-09-17 (later) — Draft, reviewer round complete, Owner ratification pending: the merged bus becomes the visualizer source, and one narrow raw-PCM exception (SN-041/SR-041/LLR-950..958/IF-020; TC-960..975).** Work package G of the panel kiosk state build. One new least-privileged unit, `wall-bus-visualizer.service`, opens `bus_monitor` — the merged, PRE-switch ALSA bus — and fans each captured block into a bounded schema-2 telemetry document and an OPTIONAL bounded local PCM stream for ProjectM. `switch_backend` reads that document instead of the `speaker_tap`-derived one. codex gpt-5.6-terra (medium) reviewed and returned two findings; one was accepted, one was accepted in part with the rejected half written down.

* **Accepted, major: capture teardown could hang, and the capture outlived a mode change.** `close()` drained the child's stderr BEFORE terminating it — the amplifier detector's pattern, copied out of the one context where it is safe. The detector only ever drains after its read loop has broken, so the child is gone and the pipe is at EOF; here `close()` also runs on service stop and on a mode change, with `arecord` alive and quiet, and `read(2048)` on a live silent pipe blocks until it has 2048 bytes. systemd would have killed the control group on EVERY stop. Separately, when `_pump` returned on a mode change the loop sat in the not-in-bus branch holding the `bus_monitor` dsnoop reader open for as long as the panel stayed out of bus mode — contention beside the very legs a mode switch is trying to rearrange. Terminate-then-drain, and release before the wait. The reviewer was also right that the existing tests would have passed the defect: `FakeCapture` has no pipes. A real subprocess test now holds both pipes open and writes nothing.
* **Accepted IN PART, and the rejected half is the more interesting one (finding 1).** The reviewer asked for a host-owned "ProjectM is visible" lease carried to the producer, gating admission and broadcast. **Rejected:** visibility is not observable from the producer and never will be, and the consumer already signals it in the only way a producer can hear — by closing the socket. The connection IS the lease. A heartbeat protocol would be more mechanism, with its own expiry to get wrong, guarding a case the consumer already covers; every teardown path on the app side was audited instead, and the ones that were missing were added there. **Accepted:** the part that is a RULING rather than an invention. Mute yields Frame Media, so Mute means the permission does not hold, and the producer now drops every stream when the confirmed switch position is Mute, checked on the publish beat. An unreadable state file answers *audible* here — the opposite of the broker's choice, and right for the opposite reason: this is a backstop, and refusing PCM because an unrelated file is damaged would turn a file problem into "ProjectM never works".
* **Found by the FULL pytest tier, which the gate-scoped smoke run deselects — and the smoke tier was green over both the whole time.** (a) `test_every_bound_unit_is_started_again_when_the_device_returns` derives its coverage rather than listing it, precisely so a new `BindsTo=` leg cannot go stale, and it caught this one: a hub move would have stopped the visualizer with the adapter and never started it again, leaving the wall with no visualizer until the next mode switch or reboot. It is named directly in `90-wall-audio-adapter.rules` rather than left to `wall-audio-resume`, which does not contradict that rule's warning against a second switch plan: it is not a switch leg, it reads `bus_monitor` wherever the switch points, so `ALL_LEGS` does not and must not know about it. (b) A shebang file at 100644 in the index reaches the panel through `git archive` at 0664 and firstboot cannot exec it. The unit invokes the daemon through `python3` so it would have worked anyway — which is exactly why that is checked at the transport boundary rather than inferred from the unit file.
* **Found by sweeping, not by a test.** `wall.env.example` still promised "raw samples never leave that future backend". The durable rule and ProjectM's one narrow exception replace it.

**The three visible changes on the wall, which are requirements and not side effects.** HEADSET VISUALIZES — it shows nothing at all today, because `speaker_tap` is post-switch. MUTE YIELDS FRAME MEDIA — `bus_monitor` is upstream of the switch and carries samples while muted, so the broker ANDs the confirmed output selection into `active` and into nothing else: the levels are still reported and the `bus` block still says `live`, because the capture is fine and saying otherwise would be a lie about the panel's health. THE MICROPHONE MAY NEVER STAND IN FOR THE BUS — the backend used to answer `available: true` with a fabricated silent bus whenever the microphone had telemetry and the bus had none, promoting a microphone reading into the bus's place in the reply. That substitution is gone and an absent bus reports absent whatever the microphone is doing. **The block itself stays**, and the correction is recorded below.

**Not disturbed, and proved rather than asserted.** `panel-amp-trigger.py` still opens `speaker_tap` and only `speaker_tap` and takes the same relay decision; a test reads the shipped text of both files and each failure of the new service is injected. `bus_monitor` is a `dsnoop`, so a second reader is what the plugin is for — the conf comment calling it "The single reader of the bus" is amended in the same commit that made it false.

**Spine rows were drafted by codex gpt-5.6-terra (medium) against the ruling list and the existing rows' style, then integrated and corrected here**: the drafts gated Headset on `headset_present`, which this implementation does not check and no ruling asks for, and named test functions that did not exist. SN-041 leaves a gap after SN-027 because the ids were allocated across two lanes before either started.

Evidence: `python -m pytest -q` reports **1919 passed, 26 skipped, 0 failed** in 196s (the FULL tier; the gate-scoped smoke run is 201 passed, 4 skipped, 1736 deselected); `python scripts/check.py --tier smoke` reports the SAME two pre-existing failures as the branch point (registry-integrity at 25 orphans, and doc-navigability's seven `../HomeHub` links, which do not resolve from a worktree) and no new one; `python scripts/check_flows.py --no-placeholders` reports only the pre-existing diagram-1 failure.

**Correction, same day, before anything shipped.** This lane first read the microphone ruling as "the block leaves the document entirely" and deleted `_microphone_block`. The coordinator caught it: the Owner's words are *"the microphone does not go to the speakers or headset, so it is not part of visualization"*, which is a ruling about what the VISUALIZER may draw. The `microphone` block is not a visualizer feed — it is the level ring on the microphone button in the audio chrome, a different consumer with a different question — and killing it would have been a user-visible regression nobody asked for, on the wall tonight. The block is restored; only the substitution stays deleted, which is the part that was genuinely wrong. Worth keeping as a pattern rather than an anecdote: the over-broad reading was in the BRIEF I was given, not in the ruling, and the way it was caught was somebody going back to the Owner's own sentence.

Next action: Owner ratification of the spine rows, then the paired deploy. **Install order: the MiniPC producer lands FIRST.** The endpoint's absence is a supported state — an app that lands first simply shows Frame Media until the producer arrives. Active gate remains G1. Physical acceptance on the wall (each source, Headset, Mute, ProjectM teardown, thermals) is open.

**2026-09-17 — RATIFIED after adversarial review: removable-audio deployment deferral and clean amp dependency stop (SN-027/SR-039/LLR-939..941; TC-951..954).** codex gpt-5.6-terra (medium) reviewed the draft and returned five findings; three were accepted and fixed, one was refuted on the hardware, and one is an Owner policy question rather than a defect.

* **Refuted (its blocker).** The reviewer held that `/proc/asound/$ADAPTER` cannot resolve an ALSA card id, so a present adapter would be classified absent and every mode switch would exit zero having changed nothing. Measured on the panel instead of argued: ALSA creates an id-named symlink beside the numeric directories — `ICUSBAUDIO7D -> card1`, `PCH -> card0` — so `[ -d ]` resolves. Its empty-variable sub-case is unreachable because `${WALL_AUDIO_ADAPTER_CARD:-$ADAPTER}` substitutes the default for empty as well as unset. **The probe is unchanged, and this entry is the reason it was not "fixed".**
* **Accepted, major: every caller ignored `set_mode_selection`'s status.** A failed write logged "selection recorded" and exited zero, so firstboot would go green with no selection on disk — and the whole contract with the udev re-add is that it will find a recorded mode to reassert. New `require_mode_selection` wrapper; all three `apply_*` arms and the deferred branch go through it. A failed rollback now returns a distinct code and says INCONSISTENT, because a symlink and mode file naming different modes is the one state no later reader can reconcile. `_had_link` separates "there was no link" from "readlink failed", which an empty string had conflated. Both new `if` forms avoid the `a && b` statement trap this script has already been bitten by twice under `set -e`.
* **Accepted, major: `requested_shutdown` was too broad.** It returned zero for *every* unverified stop, not only the absent relay it was written for, so a relay that was plugged in and refused its OFF reported success while the amplifier may still have been energized. `relay_absent()` now makes the device node the evidence; only absence is excused. `main()`'s final `return 0` also discarded the result and now carries it.
* **Accepted, minor: the tests were source-string checks.** Fair, and it mattered — every existing SR-039 test passed against the version whose callers ignored the status. `tests/test_wall_audio_mode_selection.py` now EXECUTES the two helpers with `sh` against a temporary tree and forces `mv`/`ln` to fail. Mutation-checked: restoring the ignored status fails four tests, collapsing the distinct rollback code fails one. It skips where the platform `sh` cannot create symlinks (Git Bash), probing that tool rather than the platform name, because Python can make a symlink on Windows where Git Bash cannot.
* **OPEN, for the Owner, not a defect here (finding 5).** `activateAuthorizedEnrollment` accepts any live session, including one issued by `issueSession('face')` — so a face unlock can authorize replacing the face gallery. That is self-perpetuating. It is left alone deliberately: nothing anywhere in the codebase gates on session method, `change-pin` and `delete-gallery` have exactly the same property, and WSN-052 ratified that the session which unlocked the Settings tab IS the authorization for the whole surface. Singling out `enroll` would be inconsistent with its siblings and would break the case this change exists to fix. **A ruling is wanted: should any Settings operation require a PIN-method session?**

**Round two found three more, all accepted.** (a) The distinct inconsistent-state code was advertised in the requirement and then flattened to 1 by the wrapper, so it existed only in a log line and no caller could act on it; it is now returned, and every call site propagates `$?` rather than a literal. (b) If `$LINK` existed but was NOT a readable symlink — a regular file the Owner had put there — `ln -sfn` would replace it and the rollback could only `rm` it, destroying it while reporting "nothing was changed"; there is nothing to restore it from, so the helper now refuses before the first mutation, with a `-L` arm so an ordinary dangling symlink is not caught by it. (c) is in OfficeWallNaglight and is recorded there. Round two also confirmed the finding-1 rejection and the finding-5 reasoning in terms: "Enrollment is not materially different from changing the PIN in authorization terms."

Next action: panel demonstration TC-953 (deploy with the adapter absent, then re-add). Active gate remains G1.

**2026-09-16 — Draft, reviewer-ratification pending: removable-audio deployment deferral and clean amp dependency stop (SN-027/SR-039/LLR-939..941; TC-951..953).** `wall-audio-mode` records a consistent requested selection and exits zero before touching legs only when its configured adapter is absent; a present-device counted failure remains fatal. `panel-amp-trigger.py` returns cleanly after SIGTERM/SIGINT despite an absent relay, while unsafe startup remains fatal. Other firstboot fail_step paths were inspected and none are the same removable-device case. Next action: reviewer ratification and panel demonstrations TC-953; active gate remains G1.

**2026-09-16 — Draft, reviewer-ratification pending: panel USB audio/relay re-enumeration recovery (SN-026/SR-038/LLR-936..938; TC-947..950).** Adapter re-add now directly starts only legacy/always-on consumers and invokes the existing non-sticky reassert for ALL_LEGS policy; absent adapter is a logged zero-exit deferral. The CH340 now has a systemd alias and independent add activation; the actuator wants/orders after it but does not bind to it. Next action: reviewer ratification, then panel acceptance TC-950; active gate remains G1.

**2026-09-16 — second spine pass corrects and extends accepted trace rows;
active gate remains G1.** SN-022/SR-034/LLR-931 and TC-935..939 remain accepted,
with SR-034's rendered parser literal corrected. SN-023/SR-035/LLR-932 and
TC-940..942 now name six implemented fstrim tests, including unparseable
capability as a logged skip and an empty mount list as success. SN-024/SR-036/
LLR-933 and TC-943..945 remain Draft firstboot demonstrations: next panel-boot
journal evidence is required, because no hermetic firstboot harness exists.
SN-025/SR-037/LLR-934..935 and TC-946 define the desktop S/PDIF leg's bounded
geometric backoff without widening the requirement to internal-source sibling
audio legs. `python scripts/trace.py --strict` reports SN=26 SR=37 LLR=42
TC=54, orphans=25 (the clean-tree baseline), integrity=0; its exit remains
non-zero solely for those baseline orphans. IF-019 remains the HEVC counterpart
obligation. Next action: obtain
the HEVC firstboot journal on a panel boot and submit these G1 spine changes for
human review; no gate advance is requested.

**2026-09-14 - NI_A1 feeder rounding and the NI_A2 waist/height sister gauge
are implemented on `ni-a2-weight-feeder`; not deployed, not merged.** The
weight feeder now rounds the posted and stored weight to 0.1 lb (nothing ever
removed rounding - this path never had any; the panel's half of NI_A1 is a
separate worker's). It also posts an optional second gauge `weight-waist`
(unit `ratio`, explicit min/max, favourable low), reading the height from the
SAME Google Health route and scope with the data type segment swapped - no
consent change - and the waist from NagLight's own GET /api/today over the
already-verified local destination, so nothing new is mounted. Both halves are
cached in the existing state file and re-posted at their original stamps.
Ships OFF behind `WEIGHT_WAIST_ENABLED` and needs one sheet row the Owner must
add (documented in `stack/weight/README.md`). Active gate remains G1.

**Assumptions to confirm at the next gate.** (1) `heightMeters` is taken from
the Google Health body CAPTURED 2026-09-14 (HTTP 200, 1493 bytes, two data
points) - the one-real-call gate is now MET for height, and the capture
overturned the earlier discovery-document reading: the field is
`heightMillimeters` as a decimal string, not `heightMeters` as a number.
The captured body also confirmed the weight parser's nesting, so it needed
no change. RESOLVED - `weight_oauth.py capture --data-type height` is the
command that settled it. Any other field spelling, and any value that is not
a plain decimal string, is still refused rather than converted, and the cost
of a refusal is the ratio gauge only.

(2) The plan's `target from the waist-in item's target` is implemented as
a waist goal IN INCHES divided by the height rather than a literal 0.5 in the
target column, because `unit: in` means inches to every other reader of that
sheet and because NagLight marks a quantified row satisfied once progress
reaches its target - a 0.5 there would complete the item on every entry,
forever. The documented default when no usable target is declared is still 0.5.
**2026-09-14 — group A, panel audio: intent epoch, coupled mute, echo canceller
and real telemetry (branch `p3-a`, LOCAL ONLY, nothing installed on the panel).**
Four source changes, each with tests, against the coordinator plan's section A.

* **Contract.** The root applier now stamps a backend EPOCH (`generation`) into
  `/etc/wall-panel/audio-state.json`, advanced once per boot by `apply-state`
  finding no marker in `/run/wall-panel/audio-epoch.json`. A broker request may
  name the epoch it was minted against and one naming a dead epoch is refused
  inside the apply lock, leaving the state byte-identical. The reply now echoes
  `seq`, `generation` and the applied state (`effective`), all behind the
  existing `echoSeq` opt-in so an un-upgraded renderer is unaffected. Field
  names of record: OfficeWallNaglight
  `docs/design/audio-intent-contract-2026-09-14.md`.
* **Item J** (supersedes item-23 ruling E). Selecting output Mute now also mutes
  the microphone, as a LATCH on the stored flag rather than a mask, because the
  Owner's second half — the mute is retained after leaving Mute until an
  explicit unmute — is only consistent with a latch. `normalize` applies it, so
  it holds for boot, resume, udev, the rocker and the CLI, not only for broker
  requests. An independent unmute while Mute is selected is refused with a
  reason. `mic_legs_running` is the applier's OBSERVATION after its pass, and
  the broker reports `inputMutedConfirmed: false` whenever a leg may still be
  transmitting.
* **Step 6.** `stack/autoinstall/wall/aec/` is a SpeexDSP echo canceller built
  from the AEC spike's section 6: a pure-C policy core (108 assertions, no ALSA
  and no Speex) plus an I/O shell that owns both PCMs in one process. Built on
  the panel by firstboot, INSTALLED BUT NOT ENABLED — the mic seam moves to
  `mic_clean` only with `WALL_AUDIO_AEC=1`, because acceptance needs a person in
  the room.
* **Item L.** `telemetry` no longer answers `{"available": false}`
  unconditionally. That was structural — the broker cannot open a sound device —
  so two services that already hold their captures publish what they measured:
  `wall-amp-trigger` (already reading `speaker_tap` for the relay) and
  `wall-audio-aec`. No second capture and no routing change.

**Independent review: two Terra-medium passes, thirteen real findings, all
fixed with regression tests.** Pass one: a rolled-back state file re-opened
every request the applier had already refused (the run marker now carries the
sequence mark as well as the epoch); clearing /run under a running panel fenced
nothing (a missing marker is now an epoch break for scoped requests, and the
rocker is deliberately unaffected); the canceller never learned the input mute,
so it claimed a live microphone through an item J coupled mute; a tap that
failed to open at start was never retried; the divergence test used a lifetime
mean, making detection latency a function of uptime; the AEC enable knob lived
in two files and nothing wrote the second; bus freshness was stamped at read
time; an absent bus was passed off as a measured quiet room; and the decimation
had no anti-alias at all. Pass two: the epoch marker was advanced BEFORE the
state, so a crash between them lost an accepted request permanently; a
failed-over canceller logged "passed through" and went on cancelling; the
reference ring desynchronised as soon as a non-unity resampler ratio was in
force; the reply's `effective` field was renamed `observedBefore` after two
readers took it for a claim about the outcome; the boxcar did not actually
suppress the alias it named (decimation is now 2x, so nothing audible folds
into a displayed band); and a mic leg could open `mic_clean` with no writer.

Two claims were reviewed and REJECTED with evidence, not silently: that valid
telemetry is refused for a missing `generation` (the router stamps it before
validating, confirmed end to end through the real broker), and that this work
installs the canceller on the panel (it adds the firstboot lines the plan asked
for; nothing was installed, and the unit ships disabled).

**Assumptions recorded for the next gate.** (a) Item J's latch means a person
who was live on Speaker, taps Mute, then taps Speaker again comes back MUTED;
that is the Owner's ruling read literally and is flagged for review. (b) The
canceller's bands and ERLE are proven only against a synthetic fixture: the
spike's retained evidence is PNGs and JSON summaries, so no replay against real
captures was possible. (c) Everything hardware-dependent — the rocker chain,
coupled mic silence on both returns, AEC double talk, the amplifier-knob move —
is explicitly unproven and listed in the group's report.

**Installed baseline, read-only inspection 2026-09-14.** The panel's
`wall_audio_state.py` and `switch_backend.py` are one commit behind `c4136e6`;
`panel-volume-request.py` and `wall-volume-request.socket` are absent, as the
plan already records. The state file carries neither `generation` nor
`mic_legs_running`, which is the compatibility case this work handles.


**2026-09-14 — local panel capability provisioning is implemented and
independently reviewed in the isolated checkout; not deployed.** The image now
stages and installs the verified sensor runtime without gateway registration,
publishes a digest-verified model manifest path, removes only its retired camera
blacklist without opening capture, and provides an atomic root local-setup/wake
helper. Saved camera/Bluetooth wake forces display-off instead of S3. Existing
gateway fields and panel PIN/enrollment state are preserved. Active gate remains
G1; independent adversarial review is closed and the next action is coordinator
integration.

**2026-09-14 — panel follow-up D/F source continuation (not deployed).**
The volume rocker uses a narrow up/down-only socket helper to reach the root
applier under its existing DynamicUser restrictions. Successful presses carry
`volume_event_seq`, including at a volume limit. Firstboot carries/enables the
helper payload. Independent terra medium review found normalization drift for
invalid sequence values: canonical and backend normalization now both mark
repair and force input mute. Cross-implementation regression cases cover
negative, boolean and unsafe-integer counters. WSL targeted audio suite:
366 passed. Active gate remains G1; no live deployment or gate advancement.
The coordinator's final validation and hardware limitations are recorded in
HomeHub `docs/archive/2026-10-19/PANEL_FOLLOWUP_2026-09-14.md`.

**2026-09-13 — the AI-usage feeder is DEPLOYED.** The gauge-rail redesign's
feeder half (`stack/ai-usage/ai_usage_feeder.py`, plus its README and timer)
went to the hub during the tracker event-log cutover, in the required order:
NagLight, then the feeder, then the panel. Deployed and repo now read the same
sha; the feeder posted all six gauges on its first run — codex, Claude session,
Claude weekly, **Claude weekly Fable** (the scoped sub-column), OpenCode weekly
and monthly. `pace` is a live number on the wire (36.9 / 80.3 / 98.3) where every
gauge used to serve 0, and the rail draws on the wall.

Two things learned doing it, both worth more than the deploy. The deployed copy
was **behind** the repo by 194 lines and a project note said the two were in
sync — that note had by then been wrong in both directions on successive days, so
the habit, not the note, is the answer: compare `sha256sum` on both sides before
believing anything about what is deployed. And `/etc/systemd/system/` holds a
**copy** of the timer unit, not a symlink to `stack/`, so updating the stack copy
alone changes nothing that runs.

**2026-09-13 — LCUS-2 amplifier actuation is PROVEN ON THE PANEL, except the
amplifier itself.** The actuator defaults to the measured CH340 LCUS-2 board
through a stable `/dev/wall-amp-relay` udev link, `audio-jack` is retired and
refused (2026-09-13),
and SN-020/SR-026/LLR-010/TC-010/IF-017 are implemented. On the panel the board
enumerates, the shipped transport drives it, and a full audio-to-relay cycle
runs to the exact configured timings. **The panel now carries hand-installed
copies of four artifacts and is AHEAD of the image** — no image has been rebuilt,
and nothing was pushed. Still unproven: COM/NO amplifier wiring, amplifier
audibility, S3 on hardware, USB-disconnect recovery and long-run behaviour.

**2026-09-11 — Finance-Auditor snapshot recovery is BUILT and LOCAL-ONLY (not
pushed, deployed, or included on existing media).** A reimage now restores the
local-only `finance_snapshots` volume through the existing byte-verifying
`restore-volumes.sh` path. Its profile is named for `docker compose create`
only, so restoration creates the volume without starting the profile-gated
Finance-Auditor service or a bank-sync attempt. `finance_actual_data` remains
excluded because it is a re-creatable Actual API cache. The hermetic
`restore-volumes.test.sh` suite was run on the HomeHub from a temporary source
copy: 38 PASS / 0 FAIL, including a byte-for-byte finance snapshot restore and
the profile-only create assertion. The temporary copy was removed. This joins
the next ISO/USB rebuild; the live hub retains its older manual-finance restore
behavior until a full reimage or a separately approved SSH deployment.

**2026-09-09 — B11's automated check-off is BUILT and LOCAL-ONLY (not pushed,
not deployed).** The Owner changed the `weigh-in` item to `type: automated` with
`check: weight`, and it has synced, so the feeder now posts **two** bodies per
cycle to the same `/api/feed`: the existing `kind: gauge` body for the bar, and
`{"check": "weight", "ok": true}` on the **legacy lane** to tick the item. The
two posts fail independently and the **gauge goes first** — the gauge is what
SN-040 promises, the tick is the extra. The tick fires only on a genuinely fresh
read whose `sampleTime` is strictly newer than the one already ticked **and**
whose civil date is today in the reading's own local frame; the source-failure
re-post path can never tick, which is the whole point. There is **no
`WEIGHT_CHECK_ENABLED` knob** and none is wanted: `type:`/`check:` in the
person's own definitions are the declaration, so nothing is owed to
`stack/.env.example` or to HomeHub's `FieldSchema.psd1`. **The DAY question is
now DECIDED and applied** — see the 2026-09-10 entry below and the next
paragraph. Gate remains G1.

**2026-09-10 — THE TRACKER NOW RUNS IN THE HOUSEHOLD'S TIMEZONE
(Owner-approved). This moves the day boundary for EVERY item in the tracker, not
just weight.** `stack/docker-compose.yml`'s `tracker:` service gained
`TZ: ${TIMEZONE}` (`TIMEZONE=America/Chicago` is already in the hub's `.env`).
NagLight's `time.Now()` — which is what `s.Now()`, the nightly materialize and
the legacy `ok` check-off lane all resolve "today" through — was running in
**UTC**, because that one service was the only one that cares and never got a
`TZ:` while eight others already had one. So **habits, todos, rollovers,
streaks and catch-ups all rolled over at midnight UTC = 19:00 local**; anything
the Owner ticked between 19:00 and midnight was landing on the **next** day's
log. From now on they roll at **local midnight**. Nothing historical is
rewritten — the boundary moves forward only, so the evening of the restart is
the seam. **NOT YET DEPLOYED and it needs a container restart**: Go reads
`TZ` once, when it builds `time.Local` at process start, so an already-running
tracker keeps its UTC clock until it is recreated
(`docker compose up -d tracker` on the hub, which recreates it because the
environment changed). Local commit only, never pushed.

**2026-09-09 — B11 steps 3 and 4 are BUILT on a SIDE BRANCH (`b11-weight-token`),
CROSS-REVIEW FIXES APPLIED, pending merge into `IceDrive-DesktopDirection`, and
neither has ever spoken to Google.** A cross-review by another model family
returned REJECT (10 confirmed + 1 suspected); all of it is fixed here — the
frontmatter reader is now **depth-aware** (four ways a nested block could source
a *wrong body weight* were one defect), the OAuth `state` check is **mandatory**
and duplicated parameters are **refused rather than ranked**, the containment
claim is now true at **every path component** and the token allow-list compares
against the **configured** `WEIGHT_TOKEN_FILE` instead of against itself. Detail
in the audit entry at the bottom of this file. The Owner has cleared steps 1 and 2 (health.googleapis.com enabled on
the project that owns the shared OAuth client; the health-metrics scope on its
consent screen; the Owner a Test user). `stack/weight/weight_oauth.py` now
carries the two Owner-run commands that follow: `mint` walks the Owner through
browser consent once and writes a refresh token into `WEIGHT_TOKEN_FILE`, and
`capture` makes exactly ONE `dataPoints.list` call and saves the raw body to a
file. The chosen consent flow is a registered loopback redirect with **nothing
listening** — the browser is on the PC and the process is on the hub, OOB was
shut down by Google in 2022, and a hub-side listener would need an SSH tunnel
raised before consent or the single-use code is lost. **The Owner must add
`http://localhost:8117/` to the OAuth client's redirect URIs, alongside — never
replacing — the existing `/oauth2/callback` and `/api/drive/callback`.**
**Step 5 is now DONE - see the 2026-09-09 audit entry at the bottom of this
file.** The Owner ran `weight_oauth.py capture` against the live API and got
HTTP 200 with one data point, so the gate B7 set - "one real call, verified,
*before* the parser is written" - was **met, not waived**, and
`parse_weight_datapoint` is written against that observed body. The six "no
parser exists" absence assertions in `tests/test_weight_feeder.py` have been
**replaced by correctness assertions**, deliberately and on the record, rather
than quietly dropped; the equivalent assertion in `tests/test_weight_oauth.py`
**remains**, because the capture tool still must not interpret a body. The
captured body, the Owner's Google user id and their real weight are **not** in
this repo: what is recorded is the body's shape, with a placeholder id and a
made-up weight. Gate remains G1.

**2026-09-09 — B14 Door image integration is implemented and independently
reviewed, not deployed.** The
wall image now installs a hardened `wall-door-stream.service`, creates its
non-login service account, and starts an idle local broker only after the
packaged application exists. Root firstboot extracts only the Door allowlist
from `wall.env` into root-only `/run` files; systemd exposes those to the account
through a RAM-backed credential mount. The broker receives no unrelated panel
secrets, there is no second persistent password file or credential-bearing
process argument, and Electron can reach only `/run/wall-door-stream/service.sock`.
Every image-owned display-off and suspend path stops that broker first; display
on readies only its idle socket, never a camera connection.
The wall template declares the reserved camera address and T3 password
placeholders plus public RTSP/geometry knobs; the SIM uses an unreachable
`.invalid` fixture. The application capability contract now includes
`door-stream-v1`. Application lifecycle/synthetic coverage is complete. Four
independent review rounds closed the image/application boundary findings; the
later task-surface review also accepted the full-height Door geometry and fresh
selection requirement after idle. No panel deployment has occurred. Owner-run
load, latency, reconnect, filter comparison, long-run/concurrent behavior and
Pandora-coexistence checks remain. Gate remains G1.

**2026-09-09 — two Owner rulings applied (see the audit entry at the foot of
this file).** (1) `SLEEP_END` now has **one** default, **06:45**, on both power
paths; the per-path default is gone and SN-015's "the disabled path is
completely unchanged" line is **knowingly relaxed on this one value** — the
schedule-only morning wake moves 06:30 -> 06:45. (2) The ai-cli route cooldown
is accepted at 5 s **on the condition that a refusal says which refusal it is**:
a cooling route now answers 429 with `status`/`reason`
(`success-pacing` vs `failure-backoff`), `retry_after_seconds`/`retry_at` and a
`Retry-After` header; an in-flight row is `status:"running"`; the box-wide
ceiling stays 503 `at-capacity`. None of B12's four security criteria moved.
Next action awaiting approval: the gate is unchanged (G1).

**2026-09-09 cross-review fix round on B9, the wall panel's occupancy power —
verdict REJECT, 10 confirmed findings, all applied.** This is the
highest-consequence block in the build: the panel is wall-mounted with no
battery and no UPS, so a wrong suspend, or a suspend with no working wake,
costs the panel *and* LAN reach to it until somebody physically walks over and
touches it. Every fix below therefore leans one way — **when in doubt, stay
awake with the backlight off.**

**V1 — the panel would not have woken.** `rtcwake -m no -l -t` asserted that
the RTC keeps LOCAL time. It does not: nothing in this image runs `timedatectl
set-local-rtc 1` and nothing writes `/etc/adjtime`, so a stock Ubuntu
autoinstall leaves the RTC in **UTC** — and `-l` therefore programmed the alarm
a whole UTC offset away (a 06:45 alarm firing at 00:45 or 12:45 in Chicago).
The frame is now **established, in order of authority**: `timedatectl show -p
LocalRTC`, then `/etc/adjtime`'s third line, then UTC as the default — and the
alarm is armed `-u` or `-l` to match. The armed epoch is logged in local
wall-clock terms, which is what the requirement is written in.

**V2 — a fixed 86400-second day.** Across a DST boundary that lands at 05:45 or
07:45, not 06:45. The target is now the **next local calendar occurrence** of
the wake time (`date -d "<today> <wake> tomorrow"`), and A18 asserts it by
running the real script at a fixed instant on the night the clocks go back.

**V3 — `SLEEP_END`'s shipped default. Flagged for the Owner, and the Owner
ruled: ONE default, 06:45.** The B9 fix round made the shipped default
path-dependent — 06:45 with `WALL_ABSENCE_ENABLED=true`, the pre-existing 06:30
with it false — so that SN-015's ratified 06:45 occupancy wake and its "the
disabled path is completely unchanged" line could both hold from one knob. It
was flagged rather than silently picked, and on **2026-09-09 the Owner ruled it
collapsed to a single 06:45 default on both paths**: two shipped defaults for
one knob is a rule nobody will remember in a year. See the ruling entry below.

**The block's central rule was broken, and is now the decider's to enforce.**
The absence clock was the shell's own bookkeeping ("absent, so start counting"),
which counted straight *through* the on-period: absent from 12:00 and still away
at 22:00 arrived at the boundary already carrying 600 minutes and suspended on
the very first tick outside — the required hour **outside** the on-period never
observed at all. `decide()` now returns `absence_clock` alongside the backlight
and the power action, and it is CLEARED for every minute that is present or
inside the on-period.

**The other accepted findings, each with a test that fails without it.**

* **A truncated absence clock read as decades of absence.** A `1` left by an
  interrupted write is 1970, i.e. an immediate suspend. It is now written
  **atomically** (temp file + rename) and **range-checked** on read
  (`MIN_PLAUSIBLE_EPOCH`, plus a future-stamp bound); an unusable clock restarts
  the timer rather than being believed or left in place to be rejected forever.
* **`json.loads` accepts `NaN`** — and every comparison against NaN is false, so
  a presence file stamped `"observedAt": NaN` passed the staleness check, passed
  the skew check, and was believed when it said "absent". That inverts the whole
  fail-safe direction. The parse now refuses the three non-finite constants, and
  an in-range literal that overflows to infinity (`1e999`) is refused as well.
* **A zero exit from `rtcwake` was taken as proof of an alarm.** Firmware or a
  wrapper can return success and program nothing. The alarm is now **read back**
  from `/sys/class/rtc/rtc0/wakealarm` (with the UTC-frame shift accounted for
  when the RTC is local) and an unverifiable alarm degrades to backlight-off on
  BOTH paths. The suite's fake could not previously express this failure — it
  only recorded arguments — so the old "armed" assertion passed in exactly the
  mode it was meant to catch; the fake now programs a fake RTC, and can lie.
* **A missing or read-only backlight did not stop the suspend.** The decision
  being applied is "nobody is here: go dark, THEN sleep", and `backlight_set`
  now **reads the brightness back**. If the dark half did not happen the suspend
  is refused: lit AND unreachable is the one outcome worth avoiding above all,
  and a lit awake panel is something somebody can SSH into. The SCHEDULED path
  is deliberately untouched by this — it never claimed to dim anything.

**Both test-quality findings rewritten to assert the property, not the
spelling.** A11 greped the literals `class/backlight` and `systemctl suspend`;
it now scans **every** file in the wall tree (units included, comment lines
stripped) for **any** mechanism that could dim a screen or sleep a machine, and
counts the decider invocations at RUNTIME. A12 greped `wall.env.example` for
alternative knob names; it now **moves** `SLEEP_END` to an unusual value and
requires the same value to appear in both jobs in the same run, and separately
requires that exactly two variables in the wall scripts hold a wall-clock time.
Both mutants the review described — `loginctl suspend` elsewhere, a
`WALL_ON_START` defaulted from `SLEEP_END` — now go red.

**Tests.** `python scripts/check.py` **525 passed / 5 skipped** (baseline
517/5); `scripts/trace.py --strict-integrity` **0, orphans 24** (= baseline);
`check_flows.py --no-placeholders` **OK, 4 diagrams**;
`occupancy-power.test.sh` **73 PASS / 0 FAIL** run standalone (was 42/0).
`stack/run-hermetic-tests.sh` was **NOT run** — it refuses on this dev PC for
want of `zstd` and `rsync`, reported as UNRUN, never as passing.
**11 deliberate defects, 11 killed, no first-pass survivors** — and M1 (the RTC
frame forced back to `-l`) was killed by the readback guard as well as by its
own case, which is the cross-check the last round's lesson asked for.

**Nothing live changed.** No panel or hub state touched, no unit installed, no
service started. No apt package name added, so no apt export is owed.

---

**2026-09-09 cross-review fix round on BOTH feeders (B7 SR-021 + B11 SR-022) —
verdict REJECT, all findings applied.** A different model family reviewed the
usage feeder and the weight feeder together; the coordinator verified the two
worst in source and accepted the rest. These are the most serious defects this
build has produced, because they act on the household's **real vendor
credentials** rather than on fixtures.

**V1 — the credential guard was bypassable by symlink.** `open_for_write`
compared `os.path.abspath`, which is string arithmetic and does **not** resolve
symlinks. Pre-creating `<state>.tmp` as a link pointing at a vendor token
therefore passed the allow-list unchanged — the *string* matched — and
`save_state` opened it `"w"` and truncated the token. The allow-list SHAPE was
right; the resolution was wrong. Now:

* `writable_path_verdict` resolves with `os.path.realpath`, and it is a **pure
  decision with an injected resolver**, so the rule can be asserted on a
  filesystem that will not grant symlinks as well as on one that will;
* the resolved path must sit **inside the service's own `StateDirectory=`**
  (`$STATE_DIRECTORY`, falling back to the literal `/var/lib/homehub-{ai,weight}`
  for a hand-run). The allow-list alone is derived from a path the *config*
  names, so it can only ever say "the feeder wrote where it was told"; this
  bound is the one the `.env` cannot move;
* `open_no_follow` does not trust the check that preceded it. The file is
  unlinked first — which destroys a planted *link* and never the file it points
  at, and clears a `.tmp` left by a killed cycle — then opened
  `O_CREAT|O_EXCL|O_NOFOLLOW`, so a symlink planted **between** the check and
  the open is refused by the kernel. `O_NOFOLLOW` is absent on Windows and
  resolves to 0 there; the hub is Linux, so the deployed guard is whole and the
  dev PC still exercises the unlink, `O_EXCL` and the resolving verdict.

A refused write costs the **history**, never the gauge: the cycle has already
posted, and the refusal is a named failure in the journal.

**One redundancy was found and removed rather than reported as depth.** The
first cut bounded both `state_path` and the path being opened. The mutation run
killed neither — each hid the other's absence — because the allow-list pins the
opened path to the state file or its `.tmp` anyway. It is now **one** check, on
the path actually about to be opened, and deleting it goes red.

**V2 — the feed could leave the box two ways, and did not need the URL to
change.** All four call sites (two readers, two posters) used a bare
`urllib.request.urlopen(Request(...))`, and urllib's **default opener** follows
redirects *and* honours `http_proxy`/`https_proxy`. So the validated loopback
endpoint could answer `302 Location: http://attacker.example/feed` and urllib
would re-send the POST — feed bearer token, `X-Forwarded-User` identity, and a
body that for the weight feeder is **the Owner's body weight** — to a host the
*response* chose; and an exported proxy variable routed the same POST through a
LAN proxy that saw all of it. urllib does **not** strip `Authorization` across a
cross-host redirect, so the vendor GETs leaked in the same way.

Two openers now, and the difference between them is a decision rather than an
oversight:

| | feed POST (`feed_opener`) | vendor GET (`vendor_opener`) |
|---|---|---|
| redirects | refused | refused |
| proxies | `ProxyHandler({})` — none installed | none installed, **and no knob to opt back in** |
| peer address | re-checked on the socket **before the request is written** | not checked — outbound by design |

**The vendor decision, stated because it was asked for.** Those calls are
*supposed* to leave the box, so a peer check would be nonsense. Redirects are
still refused, because a 302 from an impersonated endpoint hands out the
household's Claude OAuth access token, the OpenCode workspace key, or (once the
weight half is unblocked) a Google Health token that grants blood glucose, body
fat, oxygen saturation, core temperature and heart-rate metrics as well as
weight — there is no weight-only scope. All three endpoints answered **200
directly** during the 2026-09-09 verification calls, so refusing costs nothing
that has ever been observed, and the price if a vendor starts redirecting is an
"unavailable" gauge — the outcome this feeder exists to produce. Proxies are
**not** made opt-in-able: a proxy knob nobody needs is a second way for a token
to leave, and an egress proxy would be a requirement change rather than a
setting. A vendor URL must also be `https`, because the URL is a knob and the
headers carry a bearer token.

The peer check runs inside `connect()`, after the handshake and **before**
`http.client` writes the request line, so a connection that lands somewhere it
should not is dropped with the token and the body unsent. That is what
"re-validate the address actually connected to" means here; the feed host is
already required to be an IP literal, so the URL check and the socket check
agree by construction and the socket check is what survives a future change.

**The other accepted findings, each fixed and each with a test that fails
without it.**

* **A malformed-but-200 payload became a 7-day-fresh gauge.** `window.kind`
  *selects* NagLight's staleness horizon, so a Claude bucket with `utilization`
  and no `resets_at`, or an OpenCode bucket missing `resetsAt`, produced a
  windowless gauge that inherited the **static** horizon — seven days — instead
  of the 24–48 h its real window implies. A source that died on Monday was
  still green the following Sunday. `check_window` now refuses a window it
  cannot determine, so that bucket becomes **unavailable**; `build_gauge`
  refuses to construct a body that carries `observed_at` without a window at
  all. The one legitimately windowless body is the never-measured sentinel, and
  it is exactly the one with no stamp.
* **A replayed or cached vendor 200 was re-stamped `now`.** The vendors do not
  say when the value was true, but they do say when the window resets, and that
  is the evidence there is: the window must **contain** `now`. A reset already
  in the past means the body describes a finished window and is a replay or a
  cache; a window that has not begun is the same evidence from the other side
  (a clock error, or milliseconds read as seconds). `MAX_CLOCK_SKEW_SECONDS`
  (300) is the only slack. The weight feeder already used the source's own
  sample time rather than `now` — the fix there is `check_observed_at`, which
  **refuses** a future stamp rather than clamping it, because clamping would
  invent the stamp the invariant forbids.
* **An invalid Codex window aborted the whole cycle.** `usedPercent: 34` with
  `windowDurationMins` of `0`, `-1`, `NaN` or infinity parsed cleanly and only
  raised later in `build_post`, **outside** the per-source catch — so the cycle
  exited before any gauge was posted and every other source's previously fresh
  value stayed green until it expired. Two fixes, and both were needed: the
  window is validated **inside the reader**, and each gauge is now built and
  posted in **its own try**, falling back to the unavailable sentinel so a
  refusable body costs one gauge rather than the cycle.
* **Loaded state was never validated.** Types, ranges and timestamps were all
  trusted; `build_gauge` asked only whether the number was finite. Corrupted or
  tampered state posted fabricated fresh readings — `-500 lb`, `100000` percent,
  or a stamp in the **future** that made a dead source render live.
  `validate_stored_reading` now runs **at the load and at the point of use**,
  which are separated by the whole source read. A nonsensical stored reading is
  not history, it is a failure, and it gets the answer a source that never
  succeeded gets. Weight also gained a plausibility band (40–1000 lb, the same
  numbers the goal is held to, now with one home and two names) applied to any
  body that carries a stamp — the sentinel `0` is the only unmeasured number
  either file may emit and it only ever appears with no stamp.
* **Error bodies landed in the journal.** Both posters returned
  `exc.read()[:200]` and `main` prints it to stderr, so anything a responding
  server or an interposed proxy chose to reflect — a token echoed back in an
  error body included — was persisted by systemd. The status code is ours to
  read; the body is the remote's to write, and it does not get a journal. The
  same reasoning removed the one remaining message interpolation: a
  `SystemExit` echoing an unparseable `AI_USAGE_FEED_URL` could have printed
  `http://user:token@host/`.
* **B11's broad `except Exception` recorded `str(exc)`.** B7 logged only the
  exception TYPE and that pattern is now copied. This is the branch the real
  OAuth reader will fall into, and an exception raised inside urllib carries the
  request object — `str(exc)` on one of those prints an `Authorization` header.

**THE INVARIANT WAS NOT BROKEN, and it is asserted whole in both suites.**
`observed_at == now` **iff** this cycle actually read the source. A failure with
history reposts the last real value at its ORIGINAL stamp; no history posts
value 0 with **no** `observed_at`. Every change above tightens what counts as
"read the source" and none of them creates a new way to stamp a reading this
cycle did not take. The wire contract is unchanged: `value`/`target` always
present, `direction` required with a `window` and refused without, `min`/`max`
together or both omitted (weight still omits them so NagLight infers its 50 lb
range), numbers only and never a colour.

**B11 IS STILL BLOCKED BY CONSTRUCTION.** No parser exists, `read_google_health`
still refuses by name, and the test asserting no `parse_google_health` /
`parse_weight_datapoint` symbol still passes — with a new test that re-asserts
both *after* this round, because a fix round is exactly when a blocked path
quietly acquires a way through. `vendor_opener` was written for that blocked
half deliberately: the egress defect happened because every call site reached
for the convenient function, and the Google Health reader is the one call site
still unwritten.

**SHARED VS MIRRORED: mirrored, deliberately, with a parity test as the price.**
The two feeders ship as standalone scripts under separate units, separate
unprivileged accounts and separate `StateDirectory=` roots, run as
`/usr/bin/python3 /opt/homehub/stack/<service>/<file>.py` under
`ProtectSystem=strict`; there is no importable module between them and adding
one is a carriage change (a third install path, a `sys.path` two units must
agree on, and a shared failure surface for two services required to fail
independently). So every guard was applied twice — and the cost of that decision
is `tests/test_feeder_egress_parity.py`, which asserts the shared properties
against **both** modules by behaviour rather than by comparing source text (a
text comparison passes on two identically broken copies, which is the state the
review found). It also refuses a bare `urlopen` anywhere in either file, which
is what will fail if the session that finally writes the Google Health reader
reaches for the default opener. Only the genuinely divergent parts differ:
`check_window` is B7-only (weight has no window by design) and the plausible-lb
band is B11-only.

**Evidence.** `python scripts/check.py` — **PASS** at gate G1, all four steps:
config-validate, unit-tests **517 passed / 5 skipped** (baseline 423/5),
registry-integrity SN=16 SR=22 LLR=6 TC=6 **integrity=0**, doc-navigability OK.
`python scripts/trace.py --strict-integrity` — **exit 0**, orphans 24 =
baseline. `python scripts/check_flows.py --no-placeholders` — **OK, 4 diagrams**,
10 ids, all known (both feeder flows were updated to show the new refusals).
`stack/run-hermetic-tests.sh` — **UNRUN**: it REFUSES on this dev PC for want of
`zstd` and `rsync`, which is the pre-existing gap and is reported as UNRUN, not
as passing.

**Mutation: 28 deliberate defects, 28 killed** — after two passes that found
real gaps rather than confirming the first set. The first pass had **seven**
survivors and each one was a defect in the *tests*:

| Survivor | What it exposed | Fix |
|---|---|---|
| state-root bound (×2) | the two containment checks were the same check twice | collapsed to one; deleting it now goes red |
| `open_no_follow` → plain `open` | the mutant left the `unlink` in place, so it removed nothing | mutant restated to remove unlink + `O_EXCL`; weight lacked the planted-symlink test entirely |
| missing-window branch | `window_kind_for(None)` raised a *type* error one line later — the right outcome by accident of ordering, with a journal message describing the wrong problem | the refusal message is now asserted |
| state validation (×3) | `load_state` and `build_post` each hid the other's absence — a guard only the other guard proves is a guard nobody has tested | each layer is now asserted where it acts |

The final table, every mutant taking the suite red on the check meant to notice
and green again on restore: `realpath`→`abspath` (both feeders) · state-root
containment dropped (both) · `open_no_follow` → plain truncating open · feed
opener follows redirects (both) · feed opener honours `http_proxy` (both) · peer
check removed (both) · vendor opener back to the default (both) · a stamped
gauge may be windowless · a missing vendor window accepted · an already-passed
reset accepted · the codex window no longer validated in the reader · per-gauge
build/post isolation removed (both) · loaded state trusted (both) · stored
reading trusted at the point of use · remote error body journalled (both) · the
broad catch journals the message (weight) · a future stamp accepted · the
plausible-weight band removed · a vendor URL may be `http`.

**One test constant had to be re-derived rather than copied — the B11 lesson
applied to B7.** `NOW` in `test_ai_usage_feeder.py` was `1789000000`, which is
2026-09-10 00:26 UTC — an hour and a half **after** Claude's five-hour window had
already reset. It was never an instant at which the recorded fixture body could
have been returned, and it only stopped being harmless when the parsers began
checking that a window contains `now`. It is now `1788944000`, an instant that
sits inside all five observed windows, and the reason is written beside it.

**Nothing live changed.** No hub or panel state was touched, no service started,
no account created. **No knob was added or removed**, so HomeHub's
`FieldSchema.psd1` is untouched and no apt export is owed; `.env.example` gained
only a comment on each `*_STATE_FILE` saying the path must stay inside the
service's `StateDirectory=`, which the shipped defaults already do.

---

**2026-09-09 the goal moved from a file-level key to the weigh-in item's
`target` (B11 correction, SR-022/LLR-006/TC-006/IF-014). READ THIS BEFORE
"RESTORING" `weight_goal_lb`.** The entry below describes the first version,
which put the goal in a top-level frontmatter key `weight_goal_lb` and
**deliberately refused an item-level goal** — mutation **M10**, "item-level
goal", was killed by a test that enforced the refusal. The Owner has ruled that
design out and it is now reversed. It did not survive this household's
configuration, and neither of the two reasons is a preference:

* **Drive SHEET mode erases top-level keys, and this household runs sheet mode.**
  `TRACKER_DRIVE_FOLDER_ID` is **empty** and `TRACKER_DRIVE_SHEET_ID` is **set**
  on the live hub. In folder mode (`drive.applyFolder`) the `.md` bytes are
  staged verbatim and any key rides along; in sheet mode NagLight's
  `internal/defsheet` **regenerates** each `.md` from a fixed `columns` list
  which is the **item** field set, and a column it does not recognise is
  collected into `unknown` and **dropped**. So `weight_goal_lb` would be deleted
  by the first sync after anyone edited the sheet — silently, leaving the panel
  dark with nothing saying why. The old entry called this a "known gap needing a
  NagLight change". It was not a gap; it was the design being wrong for the
  deployment that exists.
* **There is no vendor fallback.** Google Health v4 has **no goal or target
  concept at all**: `DataPoint` has 43 members and none is a goal, `Profile` and
  `Settings` carry none, and the only two occurrences of "goal" in the 292 KB
  discovery document (revision 20260908) are a UI settings enum. Nothing could
  have supplied a goal if the definitions lost it.

`target` and `unit` are **already** sheet columns that round-trip today, so the
goal now lives where the sync actually carries it. The Owner has added the row
and it has already synced to the hub:

```yaml
category: Health
color_weight: 1.5
  - id: weigh-in
    title: Step on the scale
    type: habit
    recur: weekly
    horizon: long
    target: 170
    unit: lb
```

**WHICH ITEM IS CONFIGURATION, NOT A HARDCODED STRING.** `WEIGHT_ITEM_CATEGORY`
(default `Health`) and `WEIGHT_ITEM_ID` (default `weigh-in`) name the item's
**location**. Both match case-insensitively and trimmed, because both are typed
by hand into a spreadsheet cell and `Health` against `health` must not be the
difference between a goal and a dark panel.

**THE "NO GOAL KNOB ON THE HUB" PROPERTY IS INTACT AND STILL MEANS SOMETHING.**
The two new knobs name a **place** and can never carry a number, so the
household's intent still lives only in the person's own definitions and still
syncs with them. They are deliberately **not** called `WEIGHT_GOAL_*`, so the
existing file scan — no `WEIGHT_GOAL` / `WEIGHT_TARGET` / `GOAL_WEIGHT` /
`TARGET_WEIGHT` name may be **declared** in `.env.example` or `FieldSchema.psd1`
— is not quietly satisfied by a rename. And the runtime negative now sets the
two location knobs to `170` as well as every goal-shaped knob, and still gets no
goal.

**THE UNIT IS CHECKED, NEVER ASSUMED, AND THAT IS THE SHARPEST EDGE IN THE
BLOCK.** A missing `unit`, or any unit but `lb`, is a **refusal**; nothing is
converted. `target: 77` with `unit: kg` is 170 lb, and **77 sits inside the
40..1000 lb plausibility band**, so the band *cannot* catch it — the panel would
show "77 lb" against a real 191 lb reading and paint it full red. The unit is
therefore checked **before the number is even parsed**, so a kilogram target
fails on the unit rather than misleadingly on the band. This is the same failure
class as a vendor parser written from a schema, which this block still refuses to
write: confident, plausible, and wrong about a person's body, with nothing on the
wall able to tell anyone.

**BOTH PRESENT IS A REFUSAL, NOT A PRECEDENCE.** If a file still carries the
superseded `weight_goal_lb` **and** the item carries a `target`, the feeder
refuses rather than picking one. Silent precedence is the trap: whichever way it
fell, the person would be looking at a bar drawn around one number while a
different number sat in their file looking equally authoritative — and in sheet
mode the top-level one is about to be deleted underneath them, so "the newest
edit wins" is not even stable. It is the same rule this module already applies to
two files declaring a goal. A file carrying **only** the legacy key is also
refused, with a message that names the key, says it is **no longer read**, and
says where the number goes — not "no goal declared", which would leave a person
upgrading staring at a `weight_goal_lb: 180` line while the journal said nothing
was declared. An item that exists but carries no target *beside* a lingering
legacy key gets the migration message rather than the both-present one, because
that person is mid-migration and "delete one" would leave them with no goal at
all.

**A blank, absent, non-numeric, non-finite or out-of-band `target` is no goal,
and nothing is posted.** Whether it surfaces as `GoalMissing` (nothing was
declared) or `ValueError` (something was declared that cannot be used), the
outcome is the same and is the one that matters — the poster is never called and
the journal names the file. The line between the two exception types is
deliberate: *nothing declared* is missing; *something declared we cannot use* is
a refusal that names the file, because a typo'd `1700` for `170` must not decay
into "no goal declared".

**The two refusals that must not collapse into one are preserved.** No **source**
still posts an unavailable gauge; no **goal** still posts **nothing**. Their
tests are unchanged in intent and both still assert the poster was never called.

**FIXTURE: THE OWNER'S REAL FILE, NOT ONE SHAPED TO SUIT THE PARSER.**
`HEALTH_MD` in `tests/test_weight_feeder.py` mirrors what actually synced —
`category: Health`, `color_weight: 1.5`, `horizon: long`, `recur: weekly`, and a
sibling item on **either side** of `weigh-in`, one of which carries a `target`
and `unit` of its own (a step count). Finding the goal by shape rather than by id
would post a step goal as a body weight, and a test asserts it does not.

**THE FieldSchema HALF IS OWED TO HomeHub.** `WEIGHT_ITEM_CATEGORY` /
`WEIGHT_ITEM_ID` are in `stack/.env.example` with their defaults but are **not**
in `scripts/deploy/FieldSchema.psd1`, which lives in the HomeHub repo — a repo
this worktree may not edit. That is safe rather than broken: an undeclared knob
is simply absent from the emitted `.env`, a blank knob takes the default, and the
default *is* the shape the Owner's sheet already syncs. What it costs is that
**moving** the item currently needs an `.env` edit on the hub rather than a
deploy-config change. A test records this explicitly so it is not "fixed" by
adding the knobs to the list that would turn the FieldSchema assertion red.

**STILL NO PARSER, AND ALL SIX ABSENCE ASSERTIONS ARE STILL GREEN.** Nothing in
this change touched the blocked vendor half: `read_google_health` still refuses
by name, and no `parse_google_health` / `parse_weight_datapoint` / `parse_weight`
/ `parse_datapoints` symbol exists. `grams_to_pounds` is untouched and is **not**
reachable from the goal path — it converts a vendor **reading**, and no unit
conversion exists anywhere on the goal path by construction.

**The redundancy that carried M25 is gone, and that is recorded rather than
silent.** The old top-level scanner had three overlapping guards against reading
an indented line as the household's goal, so removing any one of them left the
suite green (M25) and only removing all three was killed (M27). The frontmatter
is now parsed once, into top-level keys and items, so the indent rule exists in
exactly one place and is asserted directly. The "top-level keys stop at `items:`"
rule likewise survives as one line with its own test — the test that the earlier
mutation run had to be written to add.

**THE OWNER'S OPEN QUESTION — AUTO-CHECK-OFF — IS ASSESSED, NOT BUILT.** See
"Auto-check-off: what it would take" below.

**Tests:** 599 passed, 6 skipped (baseline before this change: 578/6);
`trace.py --strict-integrity` clean, `check_flows.py --no-placeholders` clean
(5 diagrams). **UNRUN:** `stack/run-hermetic-tests.sh` still refuses on this dev
PC (`missing tool(s): zstd rsync`).

**Mutation run: 18 deliberate defects (M28–M45), all 18 killed — but TWO
SURVIVED THE FIRST PASS AND BOTH WERE TEST DEFECTS.** That is the third round
running in this worktree where the first-pass survivor was a bad assertion
rather than a missing guard, so it is worth naming the shape: both survivors
were tests that asserted the *outcome the person sees* ("nothing was posted",
"it refused") when a **different guard downstream** was already producing that
outcome, so the line under test was carrying nothing.

* **M37 — the prose body read as frontmatter.** Survived. The test put a bare
  `- id:` block after the closing fence, which the parser skips anyway because a
  column-zero line ends the item sequence, so the fence check was never what
  refused it. Rewritten to re-open `items:` at column zero in the prose — the
  only shape that actually reaches the item reader — and it now dies.
* **M45 — a blank target silently becomes `0`.** Survived. The refusal was being
  carried by the **40..1000 lb plausibility band**, which catches the zero, so
  "nothing was posted" stayed true and the parametrised test could not tell. A
  new test asserts the exception TYPE through the real loader: a blank target
  must be `GoalMissing`, not a band violation, or the journal tells the person
  their goal is out of range when what they have done is not set one.

| # | deliberate defect | killed by |
|---|---|---|
| M28 | unit check removed — any unit accepted | `test_a_plausible_kilogram_target_is_refused_by_the_unit_not_the_band_sr022` (+ all 7 `..._is_refused_and_never_converted_sr022` params) |
| M29 | a missing `unit` assumed to be `lb` | `test_a_target_with_no_unit_at_all_is_refused_sr022` |
| M30 | item found by shape (first item with a `target`) instead of by id | `test_the_siblings_targets_are_not_the_weight_goal_sr022` |
| M31 | both keys present → the item silently wins (precedence, not refusal) | `test_both_a_legacy_key_and_an_item_target_are_refused_not_ranked_sr022` |
| M32 | a legacy-only key silently ignored, falls through to "no goal" | `test_the_superseded_top_level_key_is_refused_not_silently_ignored_sr022` |
| M33 | the 40..1000 lb plausibility band removed | `test_an_unusable_target_posts_nothing_at_all_sr022` (4 params) |
| M34 | the location knobs ignored — `Health`/`weigh-in` hardcoded | `test_the_location_knobs_reach_the_loader_through_a_whole_cycle_sr022` |
| M35 | two files holding the item → take the first | `test_two_files_holding_the_item_are_refused_not_ordered_sr022` |
| M36 | a field declared twice → last one wins | `test_a_target_declared_twice_on_the_item_is_refused_sr022` |
| M37 | the prose body read as frontmatter | `test_a_target_in_the_prose_body_is_not_the_goal_sr022` **(survived pass 1 — test defect, rewritten)** |
| M38 | top-level keys do not stop at `items:` | `test_a_top_level_key_after_the_items_sequence_is_not_a_declaration_sr022` |
| M39 | indented lines treated as top-level keys | `test_an_indented_legacy_key_is_an_item_field_not_a_top_level_one_sr022` (+29 others) |
| M40 | `WEIGHT_GOAL_LB=180` **declared** in `.env.example` | `test_no_goal_shaped_knob_is_declared_anywhere_deploy_reads_sr022` |
| M41 | no goal → invent 180 and post anyway | `test_a_missing_goal_posts_nothing_at_all_sr022` (+ `test_main_reports_the_refusal_and_exits_nonzero_without_posting_sr022`) |
| M42 | category/id matched case-sensitively | `test_the_category_and_id_match_the_way_a_person_types_them_sr022` |
| M43 | the `.env.example` default drifts from the code default | `test_the_item_location_knobs_are_declared_in_env_example_sr022` |
| M44 | duplicate item ids in one file → take the first | `test_one_file_holding_the_item_twice_is_refused_sr022` |
| M45 | a blank target becomes `0` rather than "no goal" | `test_a_blank_target_is_MISSING_not_a_zero_sr022` **(survived pass 1 — test defect, new test added)** |

**One check IS carried by another, and it is named rather than left implicit.**
M29 (a missing `unit` assumed to be `lb`) and M28 (the unit compared at all) are
two lines guarding one property, and each has its own test, so neither is
carried. But the **indent** rule (M39) and the **stop-at-`items:`** rule (M38)
are now single lines in one shared parser rather than the three overlapping
guards the old scanner had — the redundancy that let M25 survive last round is
gone, and each is asserted directly. Nothing else in this block is defended by
more than one mechanism.

### Auto-check-off: what it would take, and why it is not built

**SUPERSEDED 2026-09-09 — the check-off IS built; see the audit entry at the
end of this file. Keep reading for the hazards it names, but note that the
route guessed at here (`POST /api/check {id, done, expectedDate}`) is NOT the
one used: the legacy `/api/feed` lane carries no `expectedDate` and cannot
back-date at all.**

The Owner asked whether the feeder could tick the `weigh-in` habit off when
Google reports a new weight sample, via `POST /api/check {id, done,
expectedDate}`. Three hazards, and the first is the one that decides it:

* **It must fire on a new `sampleTime`, not on a successful read.** The feeder
  reads every 15 minutes and reposts the last known reading when the source
  fails; a check-off keyed to "the cycle worked" would tick the habit off
  hundreds of times against a weigh-in that happened days ago. The trigger is a
  `sampleTime` strictly newer than the one in the state file — which means the
  state file grows a second responsibility, and it is the file this block spent
  a mutation round proving is only ever written on a successful read.
* **`expectedDate` must be the sample's own local date, not today's.** A Sunday
  evening weigh-in read on Monday morning would otherwise tick off Monday and
  leave Sunday's box empty — the feeder would be recording a fact about the
  wrong day. That needs the household's timezone applied to the sample's
  instant, which this feeder does not currently carry.
* **Idempotency is not established.** This repo has never exercised the
  `{id, done, expectedDate}` shape at all: the only check-off it drives is
  `sim/validate-sim.sh`'s `POST /api/check {"id": "..."}` with
  `X-Forwarded-User`, which flips **today's** box and returns 200. Whether a
  repeat POST is a no-op or a toggle is a NagLight-side fact that has not been
  read, and a toggle would make a retry *un*-check a habit the person already
  did. That has to be verified with one real call before anything is written —
  the same gate that is keeping the vendor parser unwritten.

**A separate interaction worth naming: `recur: weekly` against a windowless
gauge's static 7-day staleness horizon.** The gauge sends no `window`, so
NagLight renders it stale after **7 days** — and the item's declared cadence is
**weekly**. Those are the same number, so a person weighing in exactly on
schedule has **zero margin**: the gauge goes stale in the hours before each
weigh-in, and any slip to day eight shows "unavailable" for a reading that is
perfectly current by the household's own rule. The 15-minute timer does not help
— it governs how fast an *absent source* becomes visible, not the horizon. This
is not caused by the change above and is not fixed by it; the honest options are
to accept the stale window before each weigh-in, or to raise it with NagLight as
a horizon that should follow the item's cadence. Sending a `window` to buy a
different horizon is **not** an option: `weekly` maps to a **24-hour** horizon,
which is far worse, and a `window` would drag `direction` in with it.

---

**2026-09-09 (later still) B11's deployment blocker is FIXED IN SOURCE, NOT YET
DEPLOYED.** On the hub the feeder could not read the goal: the drop-in bound the
definitions DIRECTORY at its own path, and that directory is `drwx------ hub hub`
under root-only ancestors. `setup-weight.sh` now resolves WHICH `.md` declares
`WEIGHT_ITEM_CATEGORY` by reading its frontmatter (never by guessing a filename)
and binds THAT ONE FILE read-only into the account's `StateDirectory`, with the
service's `WEIGHT_DEFINITIONS_DIR` overridden by a generated `EnvironmentFile=` -
`Environment=` does NOT win over the unit's `EnvironmentFile=`, measured on
systemd 255. Scope is the reason as much as the mechanism: the volume holds every
household member's tracker data. See the audit entry at the end of this file for
the deploy order the coordinator owes.

---

**2026-09-09 the weight feeder (B11, SR-022/LLR-006/TC-006/IF-014) — PARTIAL,
and the blocked half is a finding, not a gap.** The hub gains a third plain
service — no container, on a 15-minute timer — that posts one body-weight gauge
to NagLight under the same id B5's manual `feeders/weight-manual.ps1` uses, so
it REPLACES that feed by upsert rather than standing a second bar beside it. It
ships **OFF** (`WEIGHT_ENABLED=false`) and declares no credential.

**THE API IS REAL, AND THE ONE REAL CALL WAS MADE BEFORE ANY PARSER — SO NO
PARSER EXISTS YET.** The build plan named "Google Health API v4" and told this
block to verify that before building on it. It verified:

* `GET https://www.googleapis.com/discovery/v1/apis?preferred=false` → **200**,
  531 APIs, including `health:v4` and `health:v4beta`. It is **not** Google Fit
  (`fitness:v1`, closed to new sign-ups) and **not** Health Connect
  (Android-device-local, no server REST path). It is the successor to the
  **Fitbit Web API**, fed from Fitbit, Pixel Watch and partner apps.
* `GET https://health.googleapis.com/$discovery/rest?version=v4` → **200**,
  292 545 bytes, `revision 20260907`. The Weight type is real and exact:
  `Weight {sampleTime: ObservationSampleTime (required), weightGrams: double
  (required), notes: string}`, reached as the `weight` member of the `DataPoint`
  union.
* `GET https://health.googleapis.com/v4/users/me/dataTypes/weight/dataPoints`
  unauthenticated → **401 UNAUTHENTICATED**, whose `ErrorInfo` names
  `service health.googleapis.com` and
  `method google.devicesandservices.health.v4.DataPointsService.ListDataPoints`.
  **Routing happens before authentication**, so a 401 naming the backend RPC is
  proof the route resolves and that auth is the only gate left.

**THE PLAN'S SCOPE ASSUMPTION IS WRONG AND THAT MATTERS TO THE OWNER.** The
plan said Weight has "its own OAuth scope". It does not — there is no
weight-specific scope in the 21 the discovery document lists. The only scope
that admits the Weight data type is
`googlehealth.health_metrics_and_measurements.readonly`, which **also grants
blood glucose, body fat, oxygen saturation, core body temperature and
heart-rate metrics**. Consenting to a weight bar consents to all of those.

**The prose docs are also wrong about the path.** `developers.google.com`
renders the read as `/v4/users/me/dataPoints/weight`; the discovery document —
which is what a client is routed by — says
`v4/users/{usersId}/dataTypes/{dataTypesId}/dataPoints`. Exactly the class of
error the verify-first gate exists to catch.

**BLOCKED ON THE OWNER, AND THEREFORE NO PARSER WAS WRITTEN.** No authenticated
call has ever been made, because that needs (1) `health.googleapis.com` enabled
on the Cloud project that owns the existing OAuth client, (2) the scope added to
that client's consent screen plus the Owner on its Test users list, and (3) the
Owner at a browser minting a refresh token. B7 made "one real call before the
parser" this build's standard, and **body weight is the worst place to break
it**: a usage parser one field off posts a silly percentage; a weight parser
that reads kilograms as pounds posts a confident, plausible, wrong body weight
and nothing on the wall could tell anyone. So `read_google_health` refuses with
a named blocker, that flows into the ordinary unavailable path, and the panel
says "unavailable" — the truth. A test asserts no `parse_google_health` /
`parse_weight_datapoint` symbol exists (B7's `parse_gemini` precedent), and a
deliberate defect that stubs a plausible fake reading is killed by it.

**SUPERSEDED 2026-09-09 — the goal is now the weigh-in item's `target`; see
the entry above. The paragraph below describes the design that was replaced and
is kept so the reasoning is not lost.**

**"THE GOAL LIVES IN THE USER'S DEFINITIONS" IS BUILT, AND IT UNCOVERED A REAL
NagLight DEPENDENCY.** B5 recorded this half as explicitly not built. It is now:
`WEIGHT_DEFINITIONS_DIR` names the **directory**, never the number, and the goal
is a top-level `weight_goal_lb:` in a definitions file's frontmatter.
`internal/defs/yaml.go` ignores unknown top-level keys (`default:` branch, "//
ignore unknown top-level keys (forward-compatible)"), so **no NagLight change is
needed to store it there** and it rides the existing Drive sync. There is
deliberately **no `WEIGHT_GOAL` knob**, and a test asserts the negative twice
over: a cycle with every plausible goal-shaped knob set and an empty definitions
tree refuses, and no goal-shaped knob may be **declared** in `.env.example` or
`FieldSchema.psd1` at all.

**THE GAP, AND IT NEEDS NagLight — SUPERSEDED: it was not a gap, it was the
design being wrong for the deployment that exists, and the goal moved onto an
item's `target` instead. See the entry above.** Definitions reach the hub two ways. Folder
mode (`drive.applyFolder`) stages the `.md` bytes verbatim and the key rides
along. **Sheet mode regenerates the `.md` from CSV through `internal/defsheet`,
whose `columns` list is the item field set, and drops unknown columns.** This
household runs sheet mode (`TRACKER_DRIVE_SHEET_ID` set, `FOLDER_ID` blanked
2026-09-08), so on the deployed box the goal would be **erased by the first sync
after someone edits the sheet**. Smallest fix: let `defsheet` carry file-level,
non-item keys through the round trip. That is a NagLight change, a repo this
block may not edit, so it is reported rather than worked around — writing the
goal to a hub knob "for now" would have made the acceptance criterion false
while looking like it passed.

**TWO REFUSALS THAT MUST NOT COLLAPSE INTO ONE.** No **source** posts an
unavailable gauge, because the panel must say "we do not know what you weigh"
rather than leave a hole where a bar belongs. No **goal** posts **nothing**,
because the target line *is* the goal: NagLight refuses a gauge without a
target, and satisfying it would mean inventing one and colouring a real body
weight against a number nobody chose.

**"STALE, NEVER GREEN" IS ONE INVARIANT.** `build_post` says the body carries a
stamp from this cycle **if and only if** this cycle read the source. A failed
source reposts its last real reading at its **original** stamp and goes stale on
NagLight's `static` 7-day horizon; a source that never succeeded posts value 0
with **no `observed_at` at all**, stale on arrival, so the panel says
"unavailable" rather than showing nothing. A failed cycle does **not** write
state, so that stamp cannot decay into permanent freshness. And a **real but
old** reading is still stale and must be — re-stamping it to `now` to "fix" an
unavailable gauge is the easiest mistake here and has its own test.

**THE WIRE SHAPE DIFFERS FROM IF-013 IN THREE WAYS, EACH A 400 OR A SILENT
LIE.** No `min`/`max`: `lb` is the ONE unit NagLight infers a range for
(`unitRangeSpan`, a 50 lb bar around the goal), so the range rule stays in the
tracker — **decided deliberately**, and computing `goal±25` here would put a
second range authority on the producer side. No `window` and therefore no
`direction`, which NagLight refuses without one. And `observed_at` is an RFC3339
**string**, not IF-013's integer — getting that wrong renders as a permanently
stale gauge rather than failing loudly.

**THE CADENCE EXPOSED A VACUOUS ASSERTION, WHICH IS THE FIFTH IN THIS BUILD AND
THE SECOND FOUND HERE.** The usage feeder's timer test asserts the interval is
within a quarter of the tightest horizon; copied across, that is a quarter of
**seven days** — forty-two hours — so it passed on a three-hourly timer and was
enforcing almost nothing. The number had to be re-derived, not the shape copied:
the timer is 15 minutes and the test now also bounds it at one hour, because
what matters is how long the wall can keep showing a number after the source
went away. The second vacuous assertion was the same shape as B7's: the unit
test read `"ProtectHome=read-only" in unit`, which passes on the **comment that
explains the choice**, so setting the real directive to `ProtectHome=no` left the
suite green. Both now match directives at the start of a line.

**Mutation runs: 27 deliberate defects, 26 killed.** The one survivor (M25,
removing the indent check in the goal parser) survives **by construction** —
three redundant guards defend that property and any one alone suffices; M27
removes all three at once and is killed, so the property is covered. Recorded
in the code rather than left as a mystery.

**UNRUN:** `stack/run-hermetic-tests.sh` still refuses on this dev PC
(`missing tool(s): zstd rsync`), so the shell-side suites were not exercised.
This block adds no hermetic suite and no apt package (python3 stdlib only,
already in `packages.list`), so no apt export is owed.

**2026-09-09 the AI-usage feeder (B7, SR-021/LLR-005/TC-005/IF-013):** the hub
gains a second plain service - no container, on a 10-minute timer - that reads
how much of each AI subscription has been consumed and posts it to NagLight as
five gauges. It ships **OFF** (`AI_USAGE_ENABLED=false`) and declares no
credential.

**All three sources were verified with one real call BEFORE any parser existed**,
which the build plan made a hard gate rather than a preference, because a parser
written from documentation posts fiction and fiction looks exactly like a healthy
subscription. None of the three was blocked:

* **Codex** - `codex app-server --stdio`, JSON-RPC `initialize` then
  `account/rateLimits/read`. Returned `rateLimits.primary.usedPercent 34`,
  `windowDurationMins 10080` (so the codex gauge is WEEKLY), `resetsAt`
  1789435411, plus a `rateLimitsByLimitId` map. **No credential passes through
  the feeder for this source at all** - the child reads its own
  `~/.codex/auth.json` and we read stdout. Only the compatible `rateLimits` view
  is parsed: the per-limit-id buckets (`codex_bengalfox`,
  `base_model_inference`) are vendor implementation detail and gauges keyed off
  them would appear and vanish as a model is renamed.
* **Claude** - `GET https://api.anthropic.com/api/oauth/usage` with
  `anthropic-beta: oauth-2025-04-20` and a `claude-code/<version>` UA. **200**,
  carrying `five_hour.utilization 79.0` and `seven_day.utilization 87.0` as
  FLOAT percents, plus a `limits[]` array. That array carries the vendor's own
  `severity` words and **the feeder discards them** - NagLight owns severity and
  colour, and a feeder that forwarded "warning" would make the panel's authority
  ambiguous. A test greps a built body for `severity`/`css`/`colour`.
* **OpenCode Go** - `GET https://opencode.ai/zen/go/v1/usage` with the workspace
  bearer key from `auth.json`. **200**, `{"usage":{"rolling":…,"weekly":
  {"percent":58},"monthly":{"percent":89}}}`. **`rolling` is deliberately NOT
  posted**: it names no window length and the endpoint publishes none, and since
  `direction` is refused without a `window` the alternatives were to invent a
  window length or to ship a gauge with the 7-day horizon that stays green for a
  week after the feeder dies. Both are the fabricated reading the feed contract
  was tightened to stop.

**Gemini is out** - deferred as E11, and a test asserts no `parse_gemini` exists,
because a speculative parser is exactly the failure this block was written to
prevent.

**"UNAVAILABLE, NEVER A GREEN GAUGE" IS ONE INVARIANT, NOT A SCATTER OF
CHECKS.** `build_post` says: `observed_at == now` **if and only if** this cycle
actually read the source. Everything else falls out of NagLight's own rule that
`observed_at` is when the value was TRUE - a failed source reposts its last real
reading at its **original** stamp and goes stale on its horizon, and a source
that has never succeeded posts value 0 with **no `observed_at` at all**, which is
stale on arrival so the panel says "unavailable" rather than showing nothing.
Six failure classes (transport error, timeout, 401, garbage body, well-formed
body with no usable bucket, and a bug in one of our own parsers) collapse to that
single path, and each is asserted against NagLight's horizon table rather than
against our intent. **The cadence follows from the horizons:** two gauges are
`weekly`, whose horizon is 24 h, so the timer is 10 minutes and a test reads the
shipped timer file and fails if that is ever loosened past a quarter of it.

**"Never writes a vendor credential file" is an ALLOW-LIST, not a deny-list.**
`open_for_write` is the only writing door in the module and admits exactly one
path plus its `.tmp` sibling, because a list of known credential filenames
cannot survive the next CLI version putting its token somewhere new. Backed by
three independent lines: `read_secret_file` has no write mode (a test greps it),
the unit mounts that home `ProtectHome=read-only` (not `yes` - it must still
READ it), and a test runs a whole cycle inside a throwaway HOME holding all
three real-shaped credential files and fails if **any byte under it changed**,
if more than one file was written, or if the written file contains a token.

**It refuses to guess, in the manner of `TRACKER_DRIVE_USER`.** `AI_USAGE_USER`
ships blank and blank is a **refusal** - `setup-ai-usage.sh` will not install and
`resolve_identity` will not run, both saying why. No "the only user" fallback: a
single-user box is a two-user box after one oauth2-proxy login, and a usage gauge
on the wrong person's board is a lie about their own subscription.
`AI_USAGE_FEED_URL` refuses in the same spirit - loopback or an address a local
docker bridge is **actually carrying**, never a LAN or public host and never the
https route through oauth2-proxy, which would overwrite `X-Forwarded-User`. That
is `resolve_bind`'s SR-019 lesson reused: `172.16.0.0/12` contains real household
LANs, so membership of the range proves nothing.

**AND THE END-TO-END SMOKE FOUND ANOTHER, WHICH IS WHY IT WAS RUN.** With all
three parsers green against captured bodies, one cycle was run against the three
LIVE sources with the POST recorded rather than sent (no hub state touched).
OpenCode answered **403** where the verification call had answered 200: the
verification sent a named `User-Agent` and the first cut of `read_opencode` sent
none, so urllib's default `Python-urllib/3.x` went out. **The feeder behaved
exactly as specified while broken** - two "unavailable" gauges and a named
failure line, never a green one - which is the only reason the fault was visible
rather than silent. Fixed, and three more mutations (M26-M28: drop the agent,
replace the `claude-code/<version>` agent, drop the `anthropic-beta` header) all
turn the suite red. The re-run posts **all five gauges live**: codex 34%
(weekly), Claude session and weekly 87% (daily/weekly), OpenCode 58% weekly and
89% monthly, each with `min:0 max:100 target:0 direction:"up"` and no colour
field anywhere.

**MUTATION-TESTED, AND IT FOUND ONE.** 25 deliberate defects were run against
`tests/test_ai_usage_feeder.py`; 24 turned it red immediately and **M15 did
not**. M15 made a failing source discard every reading collected so far, and the
test passed only because it read the failing source FIRST - there was nothing
collected yet to discard. The test now runs BOTH orders and M15 is red. That is
the fourth block in this build to find a vacuous assertion, and the first cut of
this one would have shipped it.

**Assumption recorded for the next gate (AGENTS.md "Ask, don't assume").** The
plan says "profile-gated". There is no compose profile to join, because this is
not a container - so `AI_USAGE_ENABLED` **is** the gate, the same shape as
`AI_CLI_ENABLED` and `REMOTE_UI_ENABLED`, and only a literal `true` enables it.

**Not done, and named rather than buried:** `stack/run-hermetic-tests.sh`
REFUSES on this dev PC (`missing tool(s): zstd rsync`), so the shell carriage
(`setup-ai-usage.sh`, the firstboot hook) has been syntax-checked with `bash -n`
and reviewed, but never executed. No live hub or panel state changed. No apt
package added - python3 stdlib only, and python3 is already in `packages.list` -
so no apt export is owed.

**2026-09-09 occupancy power in the wall image (B9, SR-020/LLR-003/TC-003/IF-012):**
the panel's backlight and its suspend/RTC path now come out of **one** evaluation
of **one** knob set (`SLEEP_MODE`, `SLEEP_START`, `SLEEP_END`,
`WALL_ABSENCE_ENABLED`, `WALL_ABSENCE_TIMEOUT_MIN`, `WALL_PRESENCE_FILE`).
`wall-occupancy.py decide()` is a pure function returning both halves in one
record; `wall-sleep.sh occupancy` applies that record and re-derives neither, so
the two behaviours cannot drift into a dark screen on an awake machine or a
suspend with somebody at the panel. **`SLEEP_END` is now 06:45** (ratified
2026-09-08, replacing 06:30; one default on both power paths by Owner ruling
2026-09-09) and is one value doing three jobs: the RTC wake target, the morning
wake timer, and the start of the on-period.
**Off by default** — with `WALL_ABSENCE_ENABLED=false` nothing is read and
nothing is written, and the 22:00 `SLEEP_START` schedule stands unchanged. On:
backlight off whenever nobody is present; suspend only after an hour of absence
**outside** the on-period, arming 06:45; **never** a suspend inside the
on-period, so a walk-in is a backlight write and not a resume. With detection on,
22:00 stops being a suspend and hands over to the absence timer instead of
sleeping on somebody standing there. Presence arrives as a file (IF-012) written
by the panel shell, not from the sensor socket — that socket checks SO_PEERCRED
against the panel uid and refuses root by design, so the app senses and the image
powers. **The reader fails safe in one direction only:** missing, unreadable,
malformed, wrong-version, stale or future-stamped all read as PRESENT, so an
image whose shell does not yet write the file is inert rather than dangerous.
**SN-013 still holds and is asserted, not assumed:** an `rtcwake` that cannot arm
refuses the suspend and degrades to backlight-off on *both* paths (one guard,
`suspend_now`), and a mains blip loses the tmpfs absence clock so the panel comes
back lit, awake and reachable and must serve a full timeout again. The tests
assert the **artifacts** rather than a green timer — the brightness file really
containing 0, the epoch actually handed to `rtcwake` converting back to 06:45,
a recorded `systemctl suspend`. No live panel or hub state changed; no apt
package added, so no apt export is owed.

**2026-09-09 the AI CLI service, after a security cross-review REJECTED it
(B12 fix round, SR-019/LLR-002/LLR-004/TC-002/TC-004/IF-011):** the hub gains a
plain service - no container - that answers `POST /v1/ask` from on-box callers
by running one headless CLI session on the household subscription: messages in,
model and depth from a registry row rather than the request, the answer
constrained by a supplied schema. It ships **OFF** (`AI_CLI_ENABLED=false`) and
declares no credential.

**The block first reported all four of A40's containments as MET. They were
not.** A cross-review by a different model family returned 16 confirmed
findings and the coordinator verified the worst three in source. The four
criteria are unchanged; what changed is that each guard now checks an
**identity** where it used to check that a **string was present**:

* **A dedicated unprivileged account, never `hub`.** The unit hard-coded
  `User=homehub-ai` while every check validated the `AI_CLI_USER` knob, so
  `AI_CLI_USER=alice` had setup create and certify `alice` while systemd still
  started `homehub-ai`. Now `setup-ai-cli.sh` **generates** the unit's
  `User=`/`Group=` drop-in from that same knob, and the service asserts its own
  **effective identity** at startup and refuses a mismatch. The sudoers search
  also follows `#include`/`#includedir`/`@include` and refuses to certify an
  include it could not read - the old parser treated `#include` as a comment,
  so a NOPASSWD grant one file away was invisible.
* **Read-only tool use, no permission-skipping flag.** The old guard never
  pinned the executable, so a registry row of `python3 -c '...'` followed by
  contained-looking flags passed everything and ran arbitrary code as the
  service account. The registry is editable on the box, which is the whole
  reason this is a runtime guard, so this was the ballgame. Now argv[0] is
  **pinned per family**, resolved only on `AI_CLI_BIN_PATH` (and refused if the
  binary is group/other-writable), and **every token must be in that
  executable's declared vocabulary** with a value this repo constrains -
  duplicates refused (the old check read only the first occurrence, and the CLI
  takes the last), `--allowedTools` values actually inspected, and the `Env=`
  cell an allow-list so a row cannot set `PATH`/`HOME`/`CODEX_HOME` and move
  the ground the pin stands on.
* **Loopback or the docker bridge, never the LAN.** "The docker bridge" was the
  whole of `172.16.0.0/12` - which contains real household LANs, so a home on
  `172.20.0.0/16` was accepted as a bridge. Now a non-loopback bind must be an
  address a local `docker0`/`br-*` interface **is actually carrying**; on a box
  with no bridge, only loopback binds. `setup-ai-cli.sh` no longer carries its
  own copy of the rule - it calls the service's `--bind-check`.
* **A scratch working directory per request.** Unchanged in behaviour, but a
  failed cleanup is now reported and raised instead of swallowed by
  `ignore_errors=True`, so "removed on every exit path" is observable.

**Also bounded, because this is a small always-on box** (new LLR-004/TC-004):
one lock now takes the availability decision, the launch claim and the cooldown
together, so a burst cannot launch N sessions in the gap the old code left
between `available()` and `cool()`; successful calls pace the row too (they
never cooled at all before); at most `AI_CLI_MAX_CONCURRENT` sessions run at
once; a body with no `Content-Length` or above `AI_CLI_MAX_BODY_BYTES` is
refused before it is read; connections are capped and idle ones time out; a
timed-out session is killed as a **process group** so no grandchild survives;
`::1` is now actually servable (it was accepted and then failed to bind); codex
is actually handed the caller's schema (the file was written and never passed);
and a zero exit with a non-result frame is a 502, never a 200 carrying `null`.

**The test standard is the other half of the fix.** Three shipped checks were
vacuous - shell A10 greped `StateDirectory=` out of the unit and would have
passed with `request_scratch` deleted; A9 never ran under the unit's `User=`;
the account checks were artifact greps. They are rewritten to observe
behaviour, and **every guard was mutation-checked**: broken deliberately,
confirmed red, restored, confirmed green (24 of 24 bite; the evidence is in the
audit entry below). No live hub state changed; no apt package added, so no apt
export is owed.

**2026-09-07 unified file-share/backup feed requirement:** Owner retired the
separate visible library-drive, backup-drive and backup-run model. New
SN-014/SR-018 and repo-local IF-006 consume NagLight IF-010: a missing library
mount or failed representative Samba read is an immediate red override;
otherwise one item ages from the last artifact-verified FileBackup success.
Health recovery cannot advance that success time. The producer, fresh
provisioning, sim fixture, and registered TC-001 hermetic producer/full-wrapper
tests now use the new contract; the
legacy backup-drive unit and fixture definitions are removed. A live deployment
also runs an idempotent stop/remove migration for an already-installed legacy
timer, and the new guard refuses its old backup-target invocation even during a
race. It still needs the explicit tracker-definition inventory inspection/rebaseline
documented in `stack/tracker-seed/README.md`; no deployment or process gate
changed.

**2026-09-06 panel capabilities implementation:** Owner-approved app plan has
opt-in deploy configuration, protected Caddy routing, offline sensor installation,
private host config and camera-off verification changes. Defaults remain disabled;
no live deploy or ISO build occurred. Active gate stays G1. Nine targeted tests,
config validation, Bash parsing and G1 smoke pass; hardware/Linux/Caddy runtime
proof remains open. [Implementation and operator handoff](archive/2026-09-13/docs/panel-capabilities-implementation-2026-09-06.md).

The older cold-session block below is historical; HomeHub's current operating
brief supersedes its dated live-box state.

> **RESUMING FROM A COLD SESSION? THIS BLOCK IS THE WHOLE HANDOVER (2026-08-29, evening).**
>
> ### 0. THE ONE THING THAT NEEDS A HUMAN
>
> **IceDrive is STOPPED and will not start on its own, deliberately.** The
> FileBackup permutation drill clears `/srv/library` (its own header says "do not
> point it at anything you care about"), and it took two things with it: the
> local half of IceDrive sync pair 63605 — `/srv/library/permtest`, which is in no
> backup set, so the CLOUD SIDE IS THE ONLY COPY — and `/srv/library/.homehub-library`,
> the marker the gate now requires.
>
> The gate refusing is the guard working. A two-way pair whose local path does
> not exist may propagate a mass delete, and which way IceDrive jumps is
> unmeasured. Decide about the pair, then:
>
>     sudo touch /srv/library/.homehub-library   # bless the volume
>     sudo systemctl restart homehub-desktop-session
>
> Everything else on the box is up. The library tree was recreated after the
> drill; `verify-hub.sh` reports **16 of 16 backup sources reachable**. The only
> other red is **C21** (`finance-auditor` restart-looping on an unset
> `ACTUAL_SYNC_ID`), which is unchanged and needs one browser visit.
>
> ### 1. WHAT LANDED, AND WHY IT IS SAFE TO BUILD ON
>
> Four Owner rulings, built and exercised:
>
> | | |
> |---|---|
> | **Q1** | A `--plan` run writes `plan_<ts>/`, not `run_<ts>/`, with its own retention budget (`BACKUP_PLAN_KEEP`) paid by PLAN runs — hanging it off the nightly would bound the litter by the nightly's health |
> | **Q2** | `uptimekuma_data` is a backup set, quiesced like `actual` |
> | **Q3** | `--dry-run` is gone. The `dry_run=1` LOG token in `restore.sh` stays: that is not the flag, it is what is written on the drive |
> | **Q4** | The changed-host-key path has nine Pester cases, lifted out of the shipped launcher by AST |
>
> **The reimage restore covers five sets, not one** — `caddy`, `tracker`,
> `actual`, `uptimekuma` and the encrypted IceDrive profile. The loop lives in
> `provision/restore-volumes.sh` rather than inline in firstboot, because inline
> it could only be tested by reimaging a box, and a restore path that has never
> executed is exactly how C22 happened.
>
> **A third guard watches the tracker's definitions from OUTSIDE the tracker.**
> `/api/today` was measured returning `{"items":null}` while the panel rendered
> GREEN with score 0. The tracker cannot raise that alarm — the item that would
> carry it is deleted by the same `rm` — so the guard reads the files off the
> volume and writes a verdict to a state file `verify-hub.sh` asserts on.
>
> ### 2. THE TESTS, WHICH ARE THE POINT
>
>     158 hermetic assertions   stack/run-hermetic-tests.sh   green in WSL AND on the hub
>     160 Pester tests          scripts/lab/tests/            green
>      58 PASS / 0 FAIL / 2 SKIP   FileBackup permutation drill, production wrapper
>      19 PASS / 0 FAIL         reimage-recovery-drill.sh — the archives really do reconstruct
>
> The hermetic suites need bash, tar, gzip, zstd, rsync, sha256sum, gpg and awk —
> **no docker, no VM, no drive**. That is deliberate: every other suite here is
> expensive, which is why the cheap properties went unasserted for months.
> `tests/test_hermetic_shell_suites.py` drives them from pytest so CI gates on
> them, and `scripts/check.py` now has a `unit-tests` step — **it never ran pytest
> at all before**, so `tests/` had been in the tree unexecuted by any gate.
>
> ### 3. WHAT THE ADVERSARIAL REVIEW FOUND
>
> Four passes through `codex exec -m gpt-5.6-sol` (medium), files inlined rather
> than explored. **Twenty-six findings, twenty-four real.** The three worst were
> a tar member-NAME validation that ignored link targets in a restore running as
> root into a home directory; `restore.sh` verifying counts rather than
> membership; and `retention_prune` classifying runs by an unanchored grep over a
> file whose `note` is free text. Writing the tests for those fixes found four
> more, including `exec 9>FILE 2>/dev/null` silencing every later warn and die.
>
> Two findings were REJECTED and are written up in HomeHub's
> `DECISIONS_FOR_REVIEW_2026-08-29.md`: making the drive-identity guard fatal
> would stop IceDrive on the stand-in drives, and closing the gate's unmount race
> needs a systemd mount binding rather than another check.
>
> ### 4. THE BOX IS AHEAD OF ITS IMAGE
>
> Everything above is committed here, so the next image carries it — but it was
> installed by hand on the hub to be tested against reality. The running box and
> the last-built ISO disagree until you rebuild. See HomeHub's
> `REIMAGE_PERSISTENCE_PLAN.md` for the list.
>
> ### 5. THE HABIT, AGAIN
>
> Every defect above was found by RUNNING something. The `PLAN_FLAG_USED`
> reference that killed every plan run, the `DISPLAY=:0` restart that left the
> cloud sync down while logging success, the `pgrep` that matched its own ssh
> command line, the state write that failed on every ordinary run: none was
> visible to a reader, and all four were visible in the first execution.

<details><summary>The previous handover (2026-08-28, afternoon) — kept because C21 and C27 are still open. Its C25 row is corrected in place; everything else in it should be read as "true that afternoon".</summary>

> **RESUMING FROM A COLD SESSION? THIS BLOCK IS THE WHOLE HANDOVER (2026-08-28, afternoon).**
>
> ### 0. THE HUB IS UP, GREEN, AND NOT YET MOVED
>
> `systemctl is-system-running` = **running**, 0 failed units, the stack up. It
> was shut down for relocation earlier in the day and then **booted again** so the
> FileBackup drills could run against the stand-in drives — so the "powered off,
> being moved" note that stood here this morning is spent.
>
> **The two USB stand-ins are attached but DELIBERATELY UNMOUNTED**, cleanly, so
> the Owner can read them on a PC. The drive-health timers are running again, so
> they will report **red** until the drives are remounted or pulled — that is the
> guard doing its job, not a fault. To bring them back:
>
>     sudo mount /srv/library && sudo mount /mnt/backup-drive
>
> ⚠️ **Before it is powered on at its new location, confirm a WIRED ETHERNET
> DROP.** The hub is `eno1`-only, no wifi in netplan. See HomeHub **C2**.
>
> ### 1. THE ONE THING BLOCKING THE ETHERNET DEPLOY (C28)
>
> There is now a fifth entry point that automates the whole Option E path:
>
>     DeployHomeHubImageOverEthernet.cmd          build -> publish -> preflight
>                                                 -> consent -> reboot -> jump
>     DeployHomeHubImageOverEthernet.cmd -PublishOnly    stops before the box
>
> **The ISO is already built (14/14 assertions) and the share already published
> and verified** — 1540 files, four witness hashes matching. What is missing is
> one elevated command the Owner must run once, because it sets a Windows local
> account password:
>
>     $p = & 'C:\Projects\HomeHub\scripts\deploy\Show-DeploySecret.ps1' HubIsoSharePassword
>     Set-LocalUser -Name hubread -Password (ConvertTo-SecureString $p -AsPlainText -Force)
>
> That reads the minted secret and sets the account without displaying it. After
> it, the deploy is unattended and the Ethernet install is one command.
>
> ### 2. WHAT THE TEST WORK FOUND, AND WHAT IS STILL OPEN
>
> The suite went **103 checks -> 167 defined / 149 cited**, with three new areas
> (**R** boot integrity, **S** the graphical layer + IceDrive, **T** the tier-2
> catalogue) and new cases D11, J05, L06, M17, B02. Eight open items came out of
> it, and **none is a regression** — every one is something that had always been
> true and nothing had ever asked:
>
> | Item | What |
> |---|---|
> | **C21** | `finance-auditor` restart-loops — `ACTUAL_SYNC_ID` unset, 0 budget files in Actual. One browser visit |
> | **C22** | Three of five vhosts hold no certificate — Let's Encrypt 429. Clears itself; re-run the suite after **2026-08-29 04:12 UTC** and 11 failures become 3 |
> | **C25** | `backup.sh --dry-run` was **not dry**: it writes a `run_<ts>/` directory to the BACKUP DRIVE, mounts CIFS and issues `hdparm -S`. **Ruled 2026-08-29: the behaviour stays, the NAME goes.** The mode is `--plan`, it names what it wrote at both ends of the run, and `--dry-run` still works. **Both halves are now CLOSED (2026-08-29 evening):** the directory is `plan_<ts>`, with its own `BACKUP_PLAN_KEEP` budget pruned by plan runs, and `--dry-run` was removed once a sweep of both repos found no caller |
> | **C26** | **The household Samba accounts have never existed on any hub.** One regex in `Materialize-Deploy.ps1:890` filters on schema key NAMES, and three of the four are `@identity:` tokens that do not match. Five `Private` shares are served to nobody |
> | **C29** | The drive-health guard could not report: `/api/feed` answered **400** because the tracker declared no item for the check ids. Not a transport fault — `docker exec tracker wget …/healthz` returned `ok`. Four item definitions added on the box; both lanes now report. The guard prints the server's own explanation instead of guessing |
> | **C32** | The `caddy_data` ACME restore had **never fired**: `provision-mounts` writes the backup drive's fstab line seven minutes after the step that reads it. It now mounts the drive itself, READ-ONLY, from the generated fragment. Proven — 21 files, ACME keys and certs |
> | **C27** | The **Library** stand-in dropped off the bus 5 times on port `1-8`; moving it to `1-3` gave a clean 8 MB drill run with zero disconnects. Evidence, not yet a verdict |
> | **C28** | The share credential — now in the schema and minted; the elevated step above is what remains |
>
> ### 3. WHAT CHANGED THAT ONLY A REIMAGE WILL DELIVER
>
> `packages.list` gained three, and **the running hub does not have them yet**:
>
> - **`wakeonlan`** — measured, load-bearing. bash `/dev/udp` broadcast is refused
>   by this kernel (EACCES), and with `wakeonlan`/`etherwake` both absent
>   `wol_send` puts **nothing on the wire**. The Mini-serv ingest could never have
>   worked while Mini-serv was asleep.
> - **`exfatprogs`** — the backup drive asks for a fsck the box could not run.
> - **`smartmontools`** — nothing could read a drive's health.
>
> `library-guard.sh --report` also now **reattaches a dropped drive** (fstab entry
> only, never in `--check`, re-assesses afterwards, inhibited by
> `/run/homehub-no-remount`). All of it lands on the next install.
>
> ### 4. FILEBACKUP IS SOUND — ALL THREE DRILLS PASSED
>
>     roundtrip     49 PASS  0 FAIL  exit 0   10 states reconstructed byte-exactly
>     permutation   59 PASS  0 FAIL  exit 0   15 mutation cycles, production wrapper
>     defect regr    6 passed, 2 refused-for-preconditions (never-silent-green working)
>
> Two things were needed to get there and both are written down in
> `BENCH_BRINGUP_HANDOFF.md`: `backup.bench.env` must be recreated after a reimage
> (it is residue). **Superseded 2026-09-08:** the combined-state contract no
> longer uses `LIBRARY_BACKUP_FEED_CHECK`; `NAGLIGHT_FEED_URL` is retained only
> for the independent tracker-definition guard, while FileBackup uses
> `FILE_SHARE_BACKUP_STATE_URL`. A feed-less backup drill must override the new
> state URL without disabling the definitions guard's transport.
>
> ### 5. OPTION E IS NOT A RESCUE PATH
>
> Its own precondition is *"the OS must boot and be reachable"*, so it cannot
> recover the case a reimage is usually wanted for. Two standing caveats: the
> kernel is still `6.8.0-138` (`kexec_file_load` faults cumulatively — **reboot,
> then jump once**, which the new launcher does for you), and the CIFS share is
> hand-made. A verified USB stick exists as the fallback; **it has not been
> booted**, which is the one thing neither path establishes.
>
> ### 6. STATE OF THE MACHINES
>
> - **The real HOMEHUB: UP and green.** Stand-in drives attached, unmounted.
> - **HomeHub-Lab / WallPanel-Lab: absent.**
> - Branch on both repos: **`IceDrive-DesktopDirection`**. Everything is committed
>   locally; **the Owner pushes** (OI-3).
>
> ### 7. THE HABIT THIS WEEK KEEPS REWARDING
>
> **Six documentation-vs-reality drifts in one session, every one found by RUNNING
> something rather than reading it** — the tier-2 catalogue described as off while
> five profiles ran; Samba K04/K05 called "written only" while passing; a ratified
> decision describing a relabel that stopped happening in August; a "never writes"
> contract that wrote to the backup drive on every run; household Samba accounts
> that never existed; and `FieldSchema.psd1`, which has **never** parsed under
> Windows PowerShell 5.1 because it carried non-ASCII with no BOM. If you find
> yourself reasoning about whether something works instead of executing it, that
> is the habit this project keeps punishing.


</details>

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
    - OI-2 — **Reimage-over-LAN ladder — RESOLVED 2026-08-28, ticked here
      2026-08-30.** This entry said "nothing destructive is implemented until the
      Owner checks one". That is no longer true: Option E (the SSH rung) is BUILT
      AND PROVEN, `remote-reimage/` holds the initrd builder and the kexec script,
      `kexec-tools` is baked into the image — and it has been *used*: the box
      reimaged itself over Ethernet from an SSH command, jump 23:06:46, green
      23:24:07, no stick written. PXE stays off the table.
      → [REMOTE_MANAGEMENT.md](../REMOTE_MANAGEMENT.md) Option E; HomeHub
      open-items §E6
    - OI-3 — **Push** each commit (agents lack the SSH key) →
      this repo
    - OI-5 — **Run the V3 gate** (WI-10.18): enable Hyper-V (elevated,
      machine-level, needs a reboot), create the VM, do the one-time GRUB
      edit (light path), and confirm compose-up →
      [vmtest/README.md](../vmtest/README.md). **The HUB half is DONE** — the
      gate ran 2026-07-31 and again 2026-08-01 (see the audit entries; the
      first one PASSED while running a three-week-stale tracker image).
    - OI-17 — ~~**Boot the WALL ISO**~~ **CLOSED 2026-08-05.** The A19 gate
      booted it and **the shell painted** — the narrow point this item stayed
      open on. See the 2026-08-05 audit entry. Original text follows.

      OI-17 (as written 2026-08-02; ATTEMPTED 2026-08-03):
      the ISO was booted for the first time and got as far as `late-command_9`
      before dying on the light path's missing payload (see that day's entry —
      it was a real defect in BOTH images, now fixed). **Subiquity, the disk
      pin and all 40 packages including the 23 Electron libraries are now
      PROVEN on a real install.** What is still unproven is everything after
      the late-commands: `wall-firstboot`, `getty@tty1`, `cage`, and whether
      the shell paints. A repacked (zero-keypress) wall ISO is built at
      `D:\vmtest-out-wall\wall-repacked.iso` and `Wall-VMTest` exists; the
      re-run needs **one elevated approval** →
      [vmtest/README.md §11](../vmtest/README.md).
    - OI-19 — **The hub's site-staging late-command had the same `/cdrom`
      bug — FIXED 2026-08-03, and NEVER INSTALLED FROM.** The defect:
      `stack/autoinstall/user-data`'s site step read
      `/cdrom/deploy-payload/site`, fell back to `/media/...`, and then
      `[ -d "$S" ] || exit 0` — a **silent success**. On the LIGHT path neither
      exists (that is the defect fixed for the payload copy the same day), so a
      PRODUCTION hub would install **none** of its six real files: `.env`,
      `backup.env`, `cifs.creds`, `samba-users.creds`, `smb.conf.fragment`,
      `library-mounts.fstab`. Severity, precisely: `.env` is the only one with a
      downstream check, and `firstboot.sh` step 1 only **WARNs** on
      `REPLACE_WITH` before bringing the stack up anyway — on a headless box
      that warning is in the journal and nowhere else. The other five have no
      check at all, so the drives would not mount and Samba/backup would be
      unconfigured; the box would come up looking exactly like a SIM build
      while believing it is production. **Exposure, stated exactly:**
      `Build-VentoyStick.ps1` — the only wired production path — calls
      `build-repacked-iso.sh`, where `/cdrom` IS the combined ISO and does carry
      `/deploy-payload`, so no stick ever built has been bitten. What was one
      command away from it is the equally supported
      `SITE_DIR=… bash vmtest/build-seed.sh`.
      **The fix (see the audit entry):** the step no longer searches at all — it
      reads `/target/opt/homehub/site`, the payload late-command 3 has already
      copied, so there stays exactly ONE discovery mechanism (the `/cdrom`,
      `/media`, `/media/*`, `/run/media/*`, then CIDATA-by-label search) and it
      is the one that was fixed and exercised. Same shape as the wall image's
      late-command 4a. It also **refuses** now: the tracked user-data carries
      `BUILD_PROFILE=production` and `render_seed_tree` rewrites it to `sim` for
      a vmtest image, so a production image whose payload lost `site/` fails the
      install loudly instead of exiting 0.
      **A SECOND HEAD, found by adversarial review 2026-08-03 and fixed the
      same day:** the refusal only ever asked whether `site/` was a DIRECTORY.
      A payload whose `site/` existed but held nothing that mattered — the
      builder counted the mandatory `user-data.filled` as a staged site file,
      so a `SITE_DIR` containing only that built a clean production ISO —
      walked past it, logged six MISSING lines nobody reads on a headless box,
      touched `.site-present` so `firstboot.sh`'s own "broken production stick"
      branch stayed quiet, and exited 0. Byte for byte the outcome above, by a
      different road. Measured, not reasoned: the build succeeded and 4b
      returned 0. Now the FILES are checked, in both places — the builder
      refuses to stage, and a missing `required` file makes 4b exit 1.
      **PROVEN — behavioural, still never installed:** `vmtest/test-hub-seed.sh`
      (**28 checks, 0 failed, 0 skipped**, as root in WSL2) builds real sim and
      production seeds and then RUNS the late-commands extracted from the
      user-data each build produced — step 3 (find and copy the payload)
      followed by 4b, against a real payload under a real `/media` entry, a
      real loop-mounted CIDATA seed ISO, and a forced `cp` failure. Until
      2026-08-03 the case advertised as "the light path" hand-created
      `/target/opt/homehub/site` and never ran step 3 at all.
      **NOT PROVEN: nothing has been installed from any of it.** Subiquity has
      never run these late-commands, `curtin` has never placed `/target` for
      them, and no ISO was built or booted here. OI-19 is FIXED-BUT-UNVERIFIED,
      not closed; it closes on a hub install.
      **NEEDS THE OWNER (one ruling now, one decision to ratify):**
      (a) **DECIDED IN THE REFUSE DIRECTION, 2026-08-03, pending ratification:**
      a production build whose `site/` arrives but is missing a `required` file
      now HALTS the install (it was LOUD-BUT-NOT-FATAL). Same call the
      missing-directory branch and the wall image already make. The lever to
      reverse it is moving a file from `required` to `optional` in 4b's own
      table. (b) `drive-identity.conf` is now installed with the other six: the
      builder and `Build-VentoyStick.ps1` both stage it and **nothing ever
      installed it**, so the drive-identity file has never reached a box. Both
      are recorded as **A10** in the Assumptions log below.
    - OI-20 — **`config.json` now has a path onto the hub — and its MODE needs
      a ruling** (2026-08-03, new): Personal's `Materialize-Deploy.ps1` emits
      `out\homehub\config.json` (the kiosk shell's runtime config: `FEED_TOKEN`,
      a Kuma push token, the Subsonic password). It is a **hub** artifact — the
      shell fetches `./config.json` relative to its own origin, and that origin
      is Caddy's kiosk site, document root `/opt/homehub/stack/wall-shell/`.
      This repo now stages it (`render_seed_tree`) and installs it
      (`firstboot.sh` step 3e, **after** step 3d's untar). **The ruling owed:**
      it is installed **0600 root:root**, the most restrictive mode that
      actually serves — measured, not assumed: the pinned `caddy:2.11.4-alpine`
      declares no `USER`, `docker-compose.yml` sets no `user:`, so the container
      is uid 0 and reads the read-only bind mount as root. It was NOT widened to
      0644. What the Owner should rule on is the **posture**, not the digits: a
      file carrying live credentials is now **served over HTTP** — behind the
      kiosk site's `remote_ip {$PANEL_IP}/32` matcher, so not open to the LAN,
      but that is an allow-list, not a secret store.
      **The silent-degradation half is now closed (2026-08-03, adversarial
      review):** nothing verified that caddy could actually READ the file. Its
      healthcheck probes the admin API, which is up whenever the process is, so
      a `user:`, a `USER` in a newer image or daemon userns-remap would leave
      caddy `healthy` while the site 403/404s and `loadConfig` — which never
      throws — falls back to defaults. (`:ro`/`read_only:` are NOT the risk:
      they restrict writes, and this is a read.) `firstboot.sh` **step 4b** now
      asserts the read **as the container** (`docker exec` inherits the
      service's user) whenever the production source exists, and a failure makes
      the whole unit exit non-zero so `systemctl status homehub-firstboot` is
      RED. **UNPROVEN: step 4b has never run** — no hub, no compose bring-up.
      **The stick half is now guarded from this side too:** a production build
      with no `site/config.json` is REFUSED by `render_seed_tree`. Personal's
      `Build-VentoyStick.ps1` still assembles `out\site\` from a hardcoded
      `$wanted` list; the two halves are deliberately independent — supplying
      the file and refusing to build without it are different failures with
      different owners. **Consequence to expect:** until Personal's half lands,
      a `Build-VentoyStick.ps1` run whose `out\` tree has no `config.json` now
      FAILS instead of quietly producing a stick without one.
    - OI-21 — ~~**`/opt/homehub` installed WORLD-WRITABLE**~~ **CLOSED
      2026-08-05:** measured on two freshly installed boxes — 0 group- or
      world-writable paths under `/opt/homehub` (was 230) and `/opt/wall-panel`
      (was 215), `stack/.env` `0600 root:root`. Original text follows.

      OI-21 (as written) — **`/opt/homehub` installed WORLD-WRITABLE — every hub ISO ever
      built here. FIXED 2026-08-04, NOT YET REBUILT.** Found by BOOTING the hub
      gate VM, not by review: `drwxrwxrwx root:root /opt/homehub` (and `stack/`,
      `images/`), `-rwxrwxrwx stack/.env`, `-rwxrwxrwx docker-compose.yml`, 230
      world-writable paths, with `sudo -u nobody` able to READ every credential
      in `.env` and WRITE the compose file root brings up on the next boot — a
      straightforward local privilege escalation. The chain was three honest
      links: DrvFs reports 0777 for everything on an NTFS mount, `-rock` records
      that faithfully, `cp -a` copies it faithfully; **nobody ever decided the
      mode.** Now decided in the builder (`normalize_payload_modes` +
      `assert_payload_modes`, which FAILS the build), imposed by the ISO writer
      when the staging filesystem cannot hold it, and asserted again at install
      time (hub late-command 3b, wall 3a). **What the Owner owes:** nothing to
      rule — but **no ISO has been rebuilt**, so the fix is proven on staged
      trees, on a freshly-written seed ISO and on executed late-commands, and
      **not** on an installed box. The next hub/wall install is what closes it.
      Same family as OI-19 and the gitignored-payload leak: a latent defect in
      every artifact, invisible to every static check, surfaced by an install.
      **2026-08-04 (later): BOTH ISOs HAVE NOW BEEN REBUILT** from the fixed
      builder, into `D:\vmtest-out-hub-a19` and `D:\vmtest-out-wall-a19`, and
      `assert_iso_payload_modes` passed on both artifacts. Still not an
      installed box — that is the A19 run (OI-22).
    - OI-22 — ~~**RUN THE A19 TWO-VM GATE**~~ **CLOSED 2026-08-05 — PASSED,
      UNAIDED.** Every §3 "Done when" row met with no hand-patching: the panel
      painted red with both drive lanes named, the per-ITEM colours agreed, and
      all three ratified guards held (403 to a non-panel, a forged
      `X-Forwarded-User` replaced — including when sent twice — and `WALL_PORT`
      bound to the LAN leg only). Capture:
      `D:\vmtest-out-wall-a19\panel-a19-GATE.png`. **It closes OI-17 and OI-21
      as well — but NOT OI-19, OI-20 or the hostname fix, which need a
      PRODUCTION build and are therefore blocked on C17.** See the 2026-08-05
      audit entry, which corrects an earlier claim that one install would close
      all five. The original text follows.

      OI-22 (as written 2026-08-04). Everything it needs is
      now built and no decision is outstanding; it needs elevation and about
      three hours of wall-clock, which is why it is the Owner's. Three
      commands, in order, from an elevated shell in the MiniPC-Deployer
      checkout:
      `.\vmtest\Start-A19Gate.ps1 -Stage Lab`, then `-Stage Hub -Force -Watch`,
      then — only once the hub answers — `-Stage Panel -Force -Watch`. The two
      installs must not overlap (two VMs on one host and one disk turned a
      45-60 minute wall install into ~2h10m on 2026-08-04). See
      [vmtest/README.md](../vmtest/README.md) §12 for the addressing plan and
      the three sim deltas it introduces, and Personal's
      `WALL_PANEL_BRINGUP_PLAN.md` §3 for what to assert once both are up.
      **This single run is what closes OI-17 (the shell has never painted),
      OI-19, OI-20, OI-21 and the hostname fix — all five are code-complete and
      INSTALL-UNVERIFIED, and an install is the only thing that can speak to
      any of them.**
    - OI-18 — **RULED (b) 2026-08-03, BUILT THE SAME DAY, and HARDENED 2026-08-04
      after an adversarial review refuted its headline claim;
      INSTALL-UNVERIFIED — NOTHING HAS EVER BEEN MOUNTED.** The panel has **two**
      media sources on **two** hosts (storage-map §3 rows 1-2, §3b, §4d) and one
      credential cannot authenticate on both, so no set of values made a
      production panel work. The Owner ruled **exit (b) — two UNC/credential
      pairs** (exit (a), consolidating behind one host, would have re-opened
      HOMELAB_TOPOLOGY.md decision 2). What now exists:
      - `wall-sync.sh` carries a **flow map** (`flow_spec()`) with two flows:
        music from HOMEHUB via `MEDIA_MUSIC_SHARE_UNC` + the `Music` subdir
        **under** the mount, frame video from Mini-serv via
        `MEDIA_FRAME_SHARE_UNC` at the **share root**.
      - **THE KNOB-CONTAINMENT PROPERTY IS NOW A MECHANISM, NOT A COMMENT.** The
        2026-08-04 review found FIVE ways round it: `wall.env` exported every
        identifier it contained (so `FLOWS=music` dropped the frame flow),
        `MEDIA_CIFS_EXTRA` could re-point the subtree/host/credential, a
        symlinked cache leaf redirected `rsync --delete`, the bench override
        could copy both credential files into the kiosk-readable cache, and a
        frame UNC with a path tail could widen the mirror. There is now an
        eight-key **allowlist** with the internals `readonly` after the load, a
        validated SMB-dialect **enum**, symlink refusal on the destination,
        `//host/share`-only UNCs, and the bench hook as a **command-line mode**
        (`--bench-source`, refused under systemd, fixtures under
        `/var/lib/wall-sync/bench`).
      - **Two failure policies, and a probe with THREE answers.** Music: a
        refused mount FAILS the unit. Frame: 445 is probed first, and the script
        now distinguishes **OFFLINE** (nothing answered at all, or no route —
        the designed "asleep" state, skipped calmly) from **INDETERMINATE** (name
        will not resolve, this panel has no default route, or the box answered
        with a REFUSAL — skipped too, but reported as unknown, never as asleep).
        A box that *answers* and then refuses the mount is **fatal**. No magic
        packet is ever sent for this flow.
      - **Two cadences, two units.** `wall-sync.service` keeps boot + resume +
        on-demand and syncs BOTH flows, with a **per-flow time budget** so a
        wedged music mount can no longer eat the unit's timeout and leave the
        frame flow unrun; `wall-sync-frame.{service,timer}` runs `--only frame`
        **every minute** (`OnUnitInactiveSec`, so a slow run cannot re-fire on
        top of itself). Per-flow `flock`, held through the manifest and the
        stamp.
      - **Two credential files**: `/etc/wall-panel/cifs-{music,frame}.creds`,
        `0600 root:root` — now **enforced**, not merely claimed — installed by
        wall late-command 4a, which also **deletes the payload copies** (they
        were builder-owned, and the ordinary builder uid is the panel user's).
        The inline `MEDIA_CIFS_USER`/`_PASS` fallback is **retired**.
      - **THE `share` USERNAME COLLISION IS GUARDED, NOT JUST OBEYED.** RULED
        2026-08-04: the HOMEHUB music account is ALSO called `share` — two
        accounts, two hosts, two passwords. `wall-sync.sh` refuses two UNCs on
        one host and two flows sharing one credentials file, and no wording
        anywhere says "the `share` account" without naming its host.
      - A **staleness ladder** that measures and reports (a stamp at the cache
        root, `WALL_FRAME_STALE_WARN_HOURS`, default 24) but deliberately
        **never escalates a skip into a failed unit** (ruled 2026-08-04:
        reporting-only, and never to the tracker).
      - Personal's half: store key `PanelMusicCifsCredential`, two UNCs in
        `config.wall.psd1`, and `Materialize-Deploy.ps1 -Image wall` emitting
        **four files for the first time in the project's life**.
      - **Coverage: `vmtest/test-wall-builder.sh` is 26 cases -> 85**, runs from
        an isolated sandbox checkout (it used to rewrite tracked files), and has
        a static gate that never skips. Proven: with `wall-sync.sh` present but
        unparseable, the previous suite exited **0**; this one exits 1. All 22
        stubs of the new guards turned exactly their intended case(s) red.
      **STILL FOR THE OWNER:** (a) the HOMEHUB music account is ruled to be
      `share`, but its §2 status — a real §2 identity needing a §2 row and a
      `Samba<Name>Password` key, or a panel-only account made by hand at the box
      — is **unruled**; (b) **is Mini-serv's NIC set to wake on a magic packet
      only, or on any traffic pattern?** The code sends no magic packet, but a
      wake-on-any-pattern adapter could be woken by the every-minute probe, which
      §4d does not want; (c) ratify the new same-host refusal (it is stricter
      than OI-18 said in words). **Nothing here has been mounted** — "emits
      successfully" is not "mounts successfully", and the reachability tests use
      `127.0.0.1`/`192.0.2.1`, which prove the decision, not the mount.
    - OI-7 — **Tier-2 catalog ratifications (2026-07-10):** ~~(a) confirm the
      tier-2-NOT-baked ISO boundary (profiles excluded from the payload unless
      `EXTRA_PROFILES` at export) as the standing Q10.9 B+ interpretation;~~
      **(a) is CLOSED, and answered the other way, 2026-08-07.** Running it
      settled it: that interpretation is incompatible with Q10.9 B+ itself. A
      hub whose `.env` enabled five profiles shipped with 9 of the 15 images it
      needed, so first boot went to the registry — and because `compose up -d`
      is all-or-nothing, one unreachable **optional** image took the whole core
      stack down with it. `export-images.sh` now derives the bake set from
      `COMPOSE_PROFILES` in the `.env` it is bundling: what is enabled is baked,
      and an unresolvable tag is a refused build. The boundary is the profile
      switch, not the bake. (b) decide where `MEDIA_ROOT` physically lives (must
      NOT be the WI-10.10 backup drives); (c) the oauth2-proxy **security pin
      bump** v7.6.0→v7.15.2 needs a V1 sim re-run + re-export before any real
      flash → [stack/README.md §9](../stack/README.md)
    - OI-10 — **Disk encryption decision (2026-07-11):** the AWOW's disk is
      UNENCRYPTED — physical theft/disposal exposes `.env` secrets + the
      finance volumes. Proposed: autoinstall LUKS (Subiquity supports
      `storage: layout: {name: lvm, password: …}`) + unattended unlock via
      TPM2 enroll at first boot. **THE BIOS QUESTION IS ANSWERED — 2026-07-26:
      Intel PTT is PRESENT AND ENABLED** (vendor INTC, firmware 402.0), so
      LUKS+TPM2 is on the table and this entry should stop asking. Still
      unimplemented: the disk is plain LVM, no `crypt` device anywhere. Ratified
      in principle; tracked as HomeHub open-items §E5. The fallback if PTT had
      been absent was dropbear-initramfs (SSH unlock over LAN — fits
      headless); it is not needed. Touches the autoinstall `storage:` layout = the
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

[Earlier session log — archived, may be out of date](archive/2026-09-13/docs/status.md)

2026-09-13: documentation audit archived dated implementation narratives
and historical session logs. Operating rules, gate records, active
requirements and runbooks were retained. See the shared current findings
for deployment mismatch, provisioning gaps and all 17 Owner items.

2026-09-14 (D-BACKEND, item 23 step 5): the broker's switch backend. The shell
gained its upper-left microphone button and Mute/Headset/Speaker switch on the
renderer side, but the shipped broker backend was still `UnavailableBackend`, so
every tap on the glass was refused honestly and did nothing. `switch_backend.py`
is now the shipped backend: it turns a validated `set_output`, `set_input_mute`,
`set_volume` or the legacy `set_mute` into one sequenced request file that the
root applier picks up, and it routes no device at all. `routing.validate_action`
gained `set_volume` with a closed 0..100 percentage and an optional output guard,
and the broker returns the sequence it minted so a client can correlate its own
request exactly rather than by value.

Four decisions worth reviewing, all recorded at the code. (1) The `seq` in the
reply is OPT-IN, through an optional `echoSeq` request field, because the
shipped renderer validates the action result as an exact key set and an
unconditional extra key would have broken it in whichever order the two repos
were deployed. (2) The minted sequence moved from nanoseconds to MICROSECONDS: a
nanosecond epoch is past JavaScript's safe-integer range, and the renderer
compares it against the applier's mark with `Number.isSafeInteger`, so the
comparison it depends on would have been skipped in silence. It is also floored
above the applier's recorded mark and any unconsumed request, so it increases
across a restart, across two taps in one tick and across a clock stepped
backwards. (3) Authorization ships allowing the switch verbs alone; every verb
that names a device keeps its deny-by-default, so the routed-device backend
stays shut. (4) A level applies to whichever output is selected when the applier
runs, and the request wire names no output, so a caller that means a particular
one sends `output` and the request is refused with `switch_moved` if the switch
has already left it. The residual race, between that read and the applier's run,
is stated at the code rather than papered over.


2026-09-14 (D-BACKEND, terra rounds 1-5): eight findings, seven fixed and two
rejected with their reasons recorded at the code and in tests. Fixed: an
unreadable state file read as an empty one, so three mutation decisions were
taken from `{}` and a backward clock could mint below the applier's mark while
the backend answered accepted; `switch_moved` preserved across the backend seam
by its token alone, so a backend could answer `set_output` or `status` with a
sentence only ever true of a guarded `set_volume`; a sparse state file reported
as an unsupported switch rather than normalized as the applier normalizes it,
which drew an unknown switch and refused reconciliation on a panel whose switch
works; LLR-015 naming `AudioRouter.*` for a class called `AudioBroker`; the
completed-mutation journal outliving the volatile request file it acknowledged,
so a restart in the moment before the applier ran left a journal saying the
switch had moved and told the retry it had already succeeded; and a redo being
refused by the generation ceiling it deliberately does not advance.

REJECTED, with the reasons at the code. (1) That `_state_strict` accepts a
valid but partial object: the applier reads the same file through a field-wise
fallback, so an absent or damaged `request_seq` is -1 on both sides, and
validating a stricter schema here than the applier validates would refuse
requests on a panel whose switch works. A test now asserts the two readers
agree rather than leaving it a coincidence. (2) That a second tap overwriting an
unconsumed first request lets the landing check settle the first one: the
request file is a one-slot mailbox holding the latest intent, and that is the
design. The residue is real and is stated rather than papered over: replaying
the superseded request's completed reply reports success for an intent that was
overtaken. Both alternatives are worse (an exact match makes the redo rewrite
the older command over the newer one; refusing while a request is unconsumed
fails the second tap of a double-tap). Closing it properly needs a per-sequence
queue in the APPLIER's protocol, which this work does not own. Owed follow-up.

2026-09-14 panel follow-up validation: source commit c4136e6 is local; no push
or deployment. Config validation and firstboot Bash syntax pass. Targeted audio
tests pass (366), and the native Linux hermetic audio-switch suite reports
103 PASS. A disposable unprivileged Linux clone ran 1,414 passing tests with
10 skipped before the five-failure stop. Remaining failures involve the wider
hermetic suites, reset-versus-EOF socket expectations, /run permissions/systemd
host verification and the absent weight service account. Its doc check also
cannot resolve HomeHub sibling links inside the disposable clone; the original
checkout doc-navigability check passes. Registry integrity reports zero integrity
errors and 23 existing orphans; no gate advancement is claimed. Full logs are
in HomeHub/build/panel-followup-minipc-native-check-20260914.txt. No broad-suite
green or hardware acceptance is claimed.


2026-09-15 group D, Door broker lifecycle refusals (LLR-920). `wall-sleep.sh`'s
two Door helpers no longer refuse silently. `start_door_broker` keeps every
guard it had - an absent unit and unreadable `/run/wall-door-credentials/host`
or `password` still start nothing, because display-on must never resurrect an
older source - but each refusal now writes a warning naming the condition and
the remedy, a successful start and a failed `systemctl start` are logged, and
`stop_door_stream` logs a completed stop and warns on one that does not finish
inside its seven-second ceiling. Credential FILE NAMES are logged; credential
VALUES are not read anywhere in either helper. This closes the half of the
2026-09-14 Door incident that lived in this repo: a credential purge followed
by a wake left the broker down with nothing in the journal connecting the two,
and the panel could observe only an absent listener. New row LLR-920 traced to
SR-024 and TC-008; new smoke case
`test_door_broker_refusals_are_logged_not_silent_llr920` in
`tests/test_wall_occupancy.py`. `python scripts/check.py --tier smoke` in the
worktree: config-validate PASS, unit-tests 159 passed / 3 skipped,
registry-integrity 0 integrity errors and 24 orphans (unchanged from the
baseline). doc-navigability FAILS in the worktree only, because the sibling
`../HomeHub/...` links cannot resolve from `build/wt-d`; the same check passes
in the original checkout. Branch `d-door`, local commit only, no push, no
deployment, active gate unchanged. Group W rebases on this file.

Terra review follow-up, same day: `start_door_broker`'s failed `systemctl start`
was returning 0 after the new warning, which would have hidden the condition
twice over - `backlight_set` has its own caller-side warning for exactly that
case. It returns 1 again; only the two policy guards (no unit, no readable
credentials) still return success, because in those cases there is deliberately
nothing to start. `test_door_broker_refusals_are_logged_not_silent_llr920` now
asserts that seam directly. Smoke re-run: unit-tests 159 passed / 3 skipped,
registry-integrity 24 orphans / 0 integrity errors, doc-navigability still the
worktree-only sibling-link failure.
2026-09-15 group W, touch wakes the dark panel (branch `w-touch-wake`, local
commits only). LLR-910..914 and TC-910..914 landed against SR-017/SR-020, with
the accepted design memo `HomeHub/docs/design/WAKE_TOUCH_DESIGN_2026-09-15.md`
as its record. A root-owned evdev witness thread now runs inside
`wall-local-setup.py`, reading the touch filter's virtual uinput node by
identity while the daemon holds `EVIOCGRAB` and the physical node only when no
grab exists; `validate_wake` gained a keyword-only `witness=` seam that a
socket peer cannot reach; `wall-sleep.sh` gained a `touch-wake` arm that
mirrors `sensor-wake` and adds no re-sleep timer. `python scripts/check.py
--tier smoke` in the main checkout is the bar; in the worktree the
doc-navigability step cannot resolve the `../HomeHub` sibling links and fails
for that reason alone. Unit tests: 173 smoke passing (158 before), 34 in
tests/test_wall_local_capabilities.py; the hermetic
`tests/occupancy-power.test.sh` reports 98 PASS 0 FAIL under WSL Ubuntu (94
before). Assumptions to confirm: the 2 s rate limit is sized to one deliberate
tap and is NOT measured (LLR-911 owns the constant); the S3 hardware-wake claim
in WALL-BURN-IN.md is still unmeasured and `wall-touch-wakeup-report.sh`
exists to settle it in the hardware window (LLR-914). No physical acceptance,
no deployment and no gate advancement is claimed.

2026-09-15 group W, Terra review round (same branch). Six findings fixed. The
critical one was a real race, not a style point: `run_occupancy` decides, writes
the backlight, and only then suspends, so a tap landing in that window cleared
the absence clock and lit the screen while the occupancy process went on to
suspend a lit panel. The decide-and-act window and the whole `touch-wake` arm
are now serialized on one `flock` file, a tap that cannot get the lock is
declined with exit 75 rather than served on a stale view, and `suspend_now`
takes a last look under the lock — a backlight lit again, or an absence clock
that no longer matches the stamp taken after the decision, aborts the suspend.
The scheduled 22:00 path passes no guard and is unchanged. Also: `adaptive`
creates no uinput device (only `filter` does), so it now targets the physical
node and reports a held grab once on a long backoff instead of retrying a
device that is never created; the live grab is PROBED rather than inferred from
the persisted mode, so the witness falls back to the raw node while the daemon
is restarting and returns to the virtual node afterwards; `select_input_device`
closes every handle on every path; the node-loss test now runs the real loop
end to end; and the A11 one-writer admission is a Python AST audit plus a
shell-redirection audit rather than a line regex, with planted writes proving
it bites. Counts after the round: `check.py --tier smoke` PASS, 176 passed / 3
skipped; `occupancy-power.test.sh` 113 PASS 0 FAIL.

2026-09-15 group W, integrated-review fix (branched from the integrated head
3ce8aaf). One MEDIUM: the `touch-wake` arm called `backlight_set on` and then
released the lock unconditionally. This script runs `set -u` and NOT `set -e`,
so the write's status was discarded and the arm exited 0 on a backlight that
never came on — the witness would have read that as a completed wake, armed its
2 s rate limit against a panel that is still dark, and the journal would have
reported a successful touch wake. The arm now captures the status, releases the
lock (the next tick must not queue behind a tap that achieved nothing), then
journals what failed and exits with that status; the witness arms the rate
limit only on exit 0, telling 75 (a power decision is in flight) from any other
non-zero (the backlight did not come on) in the journal but treating both the
same way. Counts: `check.py --tier smoke` PASS, 179 passed / 3 skipped;
`occupancy-power.test.sh` 120 PASS 0 FAIL.

## 2026-09-17 evening — the broker was spending a sixth of the box starting interpreters

DEPLOYED with the panel-system payload at `9c01195`.

**Asked to investigate an anomaly, not a fault.** `wall-audio-router` measured
64% of one core continuously. Nothing was failing, no test was red, and nothing
in either repository measured it — it was visible only because the face work
made me profile the whole unit set.

`strace -c` on the live broker: **32 `execve` of
`/usr/bin/python3 --multiprocessing-fork` in eight seconds**, with 4201 `openat`
and 2334 `mmap` behind them, all module imports. `_backend_call` ran EVERY call
in a freshly spawned process (added 2026-09-10, `616e005`) because a backend
that talks to a device can hang and Python cannot kill a stuck thread. The
renderer polls `telemetry` at 4 Hz whenever the display is lit
(`setInterval(pollAudioTelemetry, 250)` in `js/main.js`), so the panel was
starting a Python interpreter four times a second to read two JSON files
out of `/run`.

**The shipped backend cannot hang on hardware, and says so itself** — it runs as
`panel` under `ProtectSystem=strict` with AF_UNIX as its only address family, so
it can neither run `amixer` nor talk to systemd.

The exemption is DECLARED BY THE BACKEND (`LOCAL_ONLY_METHODS`) and defaults to
off, so the routed-device backend WSN-024 holds out inherits isolation by saying
nothing. A blanket "telemetry is cheap" rule inside the broker would have gone
silently wrong the day that backend answers `status` from BlueZ.
`request_landed` is deliberately not declared: it reads the same file today, but
its job is to watch a mutation settle.

**Also in this deploy, the Owner's second report:** the audio-playing signal cut
off early and sent the wall to Frame Media mid-album. The mechanism already
existed; both numbers were wrong. `silence_floor` 0.01 -> 0.004 (about -40 to
-48 dBFS) because the old one called ordinary quiet passages silence, and
`silence_hold_ms` 1500 -> 5000, the Owner's figure, because even a correct floor
is crossed between tracks. Neither alone is sufficient. `panel-bus-visualizer.py`
passes NEITHER, so these defaults are the wall's behaviour and a test pins that.

**Measured on the panel, before and after:**

| | before | after |
|---|---|---|
| `wall-audio-router` | 64% of one core | **1%** |
| `wall-sensors` | 153% | 57% (also the NMS fix) |
| package temp | 78 C | **50 C** |
| interpreter spawns | 32 / 8 s | **0 / 6 s** |

**Terra found two real defects in the first draft**, both fixed and both
mutation-checked. A slot leaked whenever `Thread.start()` raised — latent while
only tests reached that seam, live the moment production did. And the
declaration was membership-tested without being type-checked, so a plain string
would have opted in by SUBSTRING, exempting "tele" and "etry" along with
"telemetry". Its third point is accepted as a STATED COST rather than fixed: a
declared-local call that never returns keeps its slot where a spawned process
could be killed. That is in the function's docstring, because it is the reason
the declaration must stay narrow.

**Still owed:** whether -48 dBFS sits above this capture path's real idle noise
floor is not answerable from the code and was not measured. If a silent room
ever animates the visualizer, that is the number to check first.

## 2026-09-18 — fresh post-AEC level publication

`wall-audio-aec` now publishes its microphone status at 4 Hz. Filesystem I/O
runs on a worker fed through a non-blocking latest-value handoff, so the audio
thread never waits behind status publication. The broker carries the source
observation timestamp and expires six missed publications at 1.5 seconds while
accepting the prior block shape for staggered upgrades. The AEC check target
now tests contention, snapshot delivery, and shutdown drain; 120 policy, 25
PCM, and 5 publisher checks pass, and both live and replay binaries compile.
**2026-09-18 — rocker pulse diagnosis only; no implementation or deployment.**
The Owner paused the responsiveness change for planning. A fresh evdev capture
and a temporary lower-layer `serio_interrupt` kprobe proved that a held rocker
arrives from the i8042 controller as repeated raw `E0 2E` make / `E0 AE` break
sequences about every 106–107 ms; evdev and `atkbd` are not synthesizing the
pairs. The probe was removed and tracing left off. Draft behavior is now one 5%
step per real break pulse, with display feedback per pulse. Adversarial review
showed that audible in-hold batching is not a scalar accumulator: the blocking
daemon needs a separate bounded actor, boundary reversals need an absolute
virtual target, and output/generation guards need a protocol extension. The
simple alternative is one final apply after the quiet gap. The assertion plan is
HomeHub `docs/PANEL_VOLUME_ROCKER_RESPONSIVENESS_PLAN_2026-09-18.md`. Product
code and tests remain unchanged. The Owner subsequently selected the in-hold
audible strategy: the non-blocking guarded actor, absolute virtual target and
causal preview reconciliation are now the asserted target. The 250 ms lifecycle
gap remains the documented default unless the Owner objects.

**2026-09-19 — the rocker plan implemented, and a preview path that had never
run.** Strategy B of `PANEL_VOLUME_ROCKER_RESPONSIVENESS_PLAN_2026-09-18.md` is
built. `RampPlanner` and its 500 ms / 25%-per-second model are withdrawn:
`PulsePlanner` charges exactly one 5% step per firmware break pulse, the quiet
gap ends the gesture and adds nothing, and `VirtualTarget` applies pulses to one
clamped absolute level in event order (98 → up → down = 95). The quiet-gap
`snap` is gone from the rocker — it could move the level with no pulse behind
it. Applies moved off the input loop onto `ApplyActor`, bounded to one request
in flight plus one replaceable target; ten pulses during one ~0.5 s apply now
produce two applies rather than ten. `wall_audio_state.py` gained
`state_revision`, which advances on every accepted change to output, input
mute, a levelled output's level or adapter presence and on nothing else, and
`set_volume_guarded` applies an absolute target as a compare-and-set against
the mode, output, level and revision it was computed on. The applier answers
`ok:OUTPUT:LEVEL:REVISION` or `refused:REASON` on stdout; the broker rebuilds
both the argv and the verdict from parsed parts, and a refused guard is not a
failed unit.

**The preview file had never once been written on the real panel.** The journal
of a unit running since 16:22 on 2026-09-18 carried
`volume gesture stamp: [Errno 13] Permission denied:
'/run/wall-panel/volume-gesture.json'` for every press since WSN-063 shipped:
/run/wall-panel is created by root units at 0755 and this unit is `DynamicUser`
under `ProtectSystem=strict`. It now has its own `RuntimeDirectory` and
publishes the schema-2 preview to `/run/wall-volume-gesture`. Pointing
`RuntimeDirectory=` at `wall-panel` would have been the wrong fix: systemd
chowns it to the unit's user on every start.

**Two AEC defects found while reviewing the microphone fixes.** Failover to
passthrough was written as a health state with a recovery branch that could
never run — the bypass copies the microphone to the output, so the residual IS
the microphone, so ERLE is exactly 0 dB on every qualifying frame after it, and
0 is below every legal floor. It was a one-way latch for the life of the daemon,
and the panel had entered it at 17:10:18 on 2026-09-18 with no measured profile
and a guessed 40 ms pre-delay. Separately, `aec_policy_rearm` left `failed_over`
set while writing `state = CONVERGING`, so after each of the session's 13 xruns
the published status contradicted what the shell did. Both fixed:
`aec_policy_rearm` ends the bypass with the filter it judged, and
`AEC_FAILOVER_RETRY_MS` gives a bypassed canceller a fresh cold start every ten
minutes. Still outstanding and needing hardware: the panel has no
`aec-profile.json` at all, so the corrected pre-delay direction is applied to a
guessed 40 ms geometry.

Three adversarial Terra rounds (gpt-5.6-terra, medium) found ten defects across
the new code; all ten were fixed and pinned by tests — adjacent gestures
baselining on a stale confirmation instead of the actor's intent, a stranded
preview after a daemon kill, terminals published and unlinked in the same
breath, an unbounded future-dated stamp, a transient read failure read as a
cancellation, expiry measured from the read rather than the document stamp, and
a SIGTERM that could not wake an idle `poll(None)`. Verified: 2087 pytest
passed / 28 skipped, `validate_config.py` all checks passed, 129 AEC policy +
31 PCM + 5 publisher C checks passed. Not deployed; the Owner deploys
separately.

## 2026-09-19 — WSN-024's routed-device backend, and carrying the Pandora login

**Pairing, trust and discoverability now reach the glass.** `switch_backend` has
been saying at length that it "routes nothing" and that WSN-024 keeps the
routed-device backend disabled until the BlueZ feasibility gate settles; the
Owner settled it by asking for the whole thing. The privilege boundary is the
one the switch already uses — the broker runs as `panel` with AF_UNIX and
ProtectSystem=strict and cannot drive BlueZ, so it writes one request file and
`wall-bluetooth-device` answers it as root, beside `wall-audio-output`.

`bluetooth_state` is the ONE alias rule and both sides import it, because a
second spelling is how the renderer names a device the applier cannot find.
Aliases are slugs of the device's own BlueZ name, ordered BY ADDRESS so the same
two devices get the same two aliases on every poll — the renderer holds an alias
between painting a row and the finger landing on it. **No hardware address is
ever in the broker's process:** its document carries aliases only, the request
carries the alias, and the applier resolves it. That is architecture rather than
a promise somebody has to keep remembering.

**The applier's presence is what ARMS the backend.** `serve()` checks for it at
startup and keeps `switch_only_authorization` when it is absent, so a panel
without it refuses `pair` at the gate rather than accepting it into a request
file nothing will read — which would leave the broker's journal sticky-pending
on a mutation that never had anywhere to go.

`select_output` is a leg BESIDE the Mute/Headset/Speaker switch and deliberately
not a fourth position of it: that enum is validated in six places and is the
control the person at the glass is holding, and widening it would put the room's
music inside a radio's failure modes. `bus_monitor` is a dsnoop and takes as
many readers as ask. `select_input` is a preference handed to `wall-bt-mic`,
which already supervises the HFP SCO leg; it can move a leg but never conjure
one, so a working microphone stays working.

**Two bugs the tests found and the code would not have.** `request_landed` was
never routed through `call`, so the broker's capability probe succeeded and
every DEVICE sequence was then checked against the SWITCH applier's high-water
mark — a coin toss between replaying a `forget` and dropping a `pair`; the
method now travels with the sequence. And `lstrip("./")` takes a SET OF
CHARACTERS, so the Pandora archive's path guard turned `../../../etc/shadow`
into `etc/shadow` and never saw the `..` it exists to catch.

**The Pandora sign-in now survives a reimage.** Measured before built: no
service reinstall loses it, and it survived all ~67 paired releases since the
2026-09-11 reimage. A REIMAGE loses it, by recreating /home/panel, and nothing
carried it forward. `wall-pandora-session save|restore|status` is that carry;
firstboot restores a staged archive once and declines on every later boot
because a sign-in is already there. It refuses when a keyring is installed,
because the cookie key would then live somewhere the archive does not.

**Three adversarial Terra rounds (gpt-5.6-terra, medium) found and closed
thirteen defects.** The ones worth carrying:

* **One mailbox file for non-idempotent verbs.** The switch's design was reused
  verbatim and it is only correct for idempotent verbs: two taps before the path
  unit fired left the first request overwritten, the applier's high-water mark
  advanced past BOTH sequences, and `request_landed` reported the destroyed
  request as landed. A `forget` the person asked for and was told had happened
  did not happen. It is a spool directory now, drained in sequence order.
* **`request_landed` was never routed through `call`.** The broker probes
  `hasattr(backend, "request_landed")` and then asks through `call` like
  everything else, so the method existed, the probe succeeded, and every DEVICE
  sequence was checked against the SWITCH applier's mark.
* **`lstrip("./")` takes a SET OF CHARACTERS**, so the Pandora archive's path
  guard turned `../../../etc/shadow` into `etc/shadow` and never saw the `..`.
* **BlueZ names an unresolved device after its own address**, and `name` sat in
  the document beside the alias we were so careful about -- so an address
  crossed the boundary, and the broker's validator then rejected the WHOLE
  document, making one unresolved device cost every other device its row.
* **Address-ordered aliases are stable for a FIXED set and not otherwise.** A
  second phone also called "Pixel", with a lower address, took the bare alias
  and pushed the first to `pixel-2` -- so a tap on a row painted a moment
  earlier acted on the other phone. Aliases now carry forward.
* **HFP's two UUIDs are opposite roles.** `111e` is the handsfree UNIT (a
  headset) and `111f` is the audio GATEWAY (a phone); treating them as one made
  every phone an output, and `select_input` refuses a non-input -- so the panel
  could not select the phone whose microphone `wall-bt-mic` exists to carry.
* **A bounded drain that strands work.** Whether `DirectoryNotEmpty=` fires again
  for a directory that was already non-empty is a systemd detail a revocation
  should not be bet on, so the observe timer drains too and ten seconds is the
  worst case either way -- with a non-blocking lock, because the two are
  different units and would otherwise both drain at once.
* **A unit timeout that was right until the unit's job changed.** `observe` was
  bounded at 25 s to sit under the broker's staleness bound; the moment it
  gained the drain, that killed a `pair` at 25 s when a pair is deliberately
  allowed 40, leaving BlueZ half way with the request already consumed.
* **Every `OSError` read as lock contention**, so a `/run` that could not be
  written looked exactly like a drain already in progress: exit 0, spool
  untouched, unit green, forever.

**One honest testing gap.** `wall-bluetooth-agent.py` imports `dbus` and `gi` at
module scope, so its passkey publishing and its new SIGTERM handling have no
unit test here -- the development box has neither binding. The fixes it carries
(a FRESH document at window open rather than a merge, and a signal handler so
the `finally` that retires it actually runs) are reasoned in the file and
verified by reading; they are not proven by a test, and writing one that read
the source back would prove nothing.

`pytest tests/`: 2291 passed, 30 skipped, 0 failed (re-measured 2026-09-19 after
other sessions' work landed; it read 2198 when this entry was first written).
`scripts/check.py`: **all four steps PASS** — config-validate, unit-tests,
registry-integrity and doc-navigability. Two of those four were failing before
this work: `WALL_PANDORA_SESSION_SEED` was read by firstboot and declared
nowhere, and AGENTS.md pointed at a handoff the 2026-09-18 cleanup archived.

**LLR-979..984 were destroyed and restored.** They were authored here, left
unstaged, and taken by a concurrent session's `git checkout`; that session
rebuilt them from the shipped code and recorded them as unrecoverable. They were
recoverable, and `fd4dab4` restores the author's Title and Detail verbatim while
keeping the union of both symbol lists — the reconstruction had found six real
symbols the originals missed.

**Not deployed, and the Bluetooth half has never met a radio** *(when written;
it deployed the next afternoon and the dual role followed on 2026-09-20 -- see
the entry below).* The acceptance plan was HomeHub
`docs/PANEL_ACCEPTANCE_WSN024_AND_PANDORA_2026-09-19.md`, now archived to
`docs/archive/2026-09-20/`; its §0 — save the
Pandora sign-in off the panel BEFORE deploying — is not optional, because that
sign-in is the one thing in scope that cannot be rebuilt from a repository.
Spine: **SR-023** (amended), **SR-046**, **IF-015** (amended), LLR-979..984,
TC-1017..1022.

---

2026-09-20 (B3–B7, the dual Bluetooth mode, deployed): the Headset switch
position became one that RESOLVES — a connected Bluetooth headset first, the USB
adapter second, silence and the red icon when neither — per the Owner's ruling
Q5 quoted verbatim in the spine. `wall_audio_state.headset_via` is the single
spelling of that priority, and the output leg, the microphone source, the
hardware mutes and the published state all read it; two spellings would put the
room's music in one headset and somebody's voice in the other. Both of the
position's legs are named in every plan, the loser explicitly false, because the
applier stops what the plan says is false and an omitted leg keeps running.

The Bluetooth leg is a unit of its own rather than the WSN-024 route leg the plan
proposed: sharing it would give one unit two owners and one environment file two
writers. It plays through a new `bt_headset_out` softvol carrying the same "Bus
Playback Volume" control, so the rocker still works in that position.

`mic_bt` is a third microphone source and is a LOOPBACK, not a BlueALSA PCM: an
SCO capture cannot be wrapped in `plug` (`Poll FD initialization failed`,
measured) and exists only while the link is up, named after the headset's
address, so nothing downstream could open it by name.

`wall-bt-call` grew from two legs to four and from one state to three — idle,
call, bridge. A2DP and SCO are exclusive on one peer, so the bridge takes the
headset's A2DP leg down behind a marker file the unit tests; a marker and not a
match against the published JSON, because neither systemd's parser nor sh's can
be trusted to keep a quoted fragment intact and a condition that silently matched
nothing would start that leg under a live SCO link.

**Four defects worth recording.** Two adversarial Terra rounds found that the
bridge opened the headset's microphone before consulting the mute; that the
supervisor restarted the A2DP leg whenever a call ended, and "the call ended"
says nothing about where the switch is; and that a call which merely CONTINUED
aged out of the broker's staleness window with every leg still running. The
panel's own paired list found the fourth: the office desktop advertises
`Handsfree`, so "connected + trusted + advertises `hf`" would have made it the
panel's own Bluetooth headset the moment it was trusted, routing the room's music
into the PC under the desk. A UUID says what a device CAN do; the class of device
says what it IS, and the rule now requires the Audio/Video major class.

**And one component was retired the day it was built.** The "silence pump" fed
`mic_bt` zeros on the belief that a loopback capture with no writer blocks.
Measured three ways on the panel and it does not — snd-aloop's capture side
free-runs silent. A component whose stated reason is false is worse than no
component; a test now pins its absence and names what to measure before bringing
it back.

`pytest tests/`: **2411 passed, 30 skipped**. `trace.py` integrity 0, orphans
unchanged at 25. Spine: **SR-025**, **SR-028**, **SR-029** amended and **SR-048**
new; **LLR-013**, **LLR-014**, **LLR-017** amended and **LLR-020**, **LLR-021**
new; **IF-015** amended; **TC-013**, **TC-014** extended.

**Deployed** 2026-09-20 as `f9bbabc` with OfficeWallNaglight `adb4d34`, paired,
`installed-verified-awaiting-visual-acceptance`, 95 files / 42 units / 66
packages. Largely UNTESTED against real hardware — everything needing a Bluetooth
headset in the room waits on HomeHub open-items **C75**. Standing document:
HomeHub `docs/FOLLOWUP_BLUETOOTH_ACCEPTANCE_2026-09-19d.md`.

## 2026-10-09 — Sure merge and default enablement verification

Owner authorized merging sure-stack and selecting its profile by default.
The merge was conflict-free. The default remains reversible by removing sure
from COMPOSE_PROFILES in both the template and HomeHub materialization config.
Finance-Auditor retains actual; no runtime cutover or deployment occurred.

Validation: revised default-profile test failed before the config edit, then
all 16 focused Sure stack/backup pytest cases passed using Git Bash.
python scripts/validate_config.py passed; python scripts/check.py --tier smoke
passed at G1 (229 passed, 4 skipped, 2284 deselected; registry integrity=0,
existing orphan baseline=25; no broken doc links, 6 orphan doc warnings).
All five merged shell files passed bash -n. The standalone postgres-source shell
suite could not run in Git Bash because rsync is missing; no pass is claimed.
HomeHub Pester: 183 passed, 4 failed, 1 skipped. Sure cross-repo contracts pass;
remaining failures are the absent LitellmMasterKey omission record and three
launcher checks blocked by stale panel build artifacts. ScriptAnalyzer retains
one warning and two informational findings in unchanged lab code.

Not proven: running Sure startup, database migrations, household accounts,
provider sync, real ledger dump/restore, offline image digest loading, or
Finance-Auditor runtime compatibility. Normal secret preparation, protected
SureSecretKeyBase escrow, and the FA empty-as-absent image fix precede deployment.
No push, live machine contact, secret generation, or gate advancement.

## 2026-10-10 - Sure fork image knob and bake-verified digest pins

Branch `sure-local-image-and-pins` (not pushed, nothing deployed). Sure now runs
the Owner's fork, built locally: compose references `${SURE_IMAGE:-sure-auroleap:local}`
(`SURE_IMAGE_TAG` retired), `scripts/build-sure-image.sh` builds a named ref of
`../sure` in WSL stamping `homehub.source.revision/ref`, `export-images.sh` handles
it as local-only (and `ensure-local-images.sh` builds it behind the `sure` profile).
Review item 5.12: `tag@sha256` knobs for postgres/redis were replaced by a plain tag
plus `*_IMAGE_DIGEST` verified at bake time and retagged, because `docker load`
does not restore RepoDigests. `images.manifest.tsv` gained a `kind` column and
`firstboot.sh` fails if a loaded local or pinned image's id differs from the manifest
(naglight and every other local-only image gained this check too).

Validation: tests/test_sure_image_bake.py drives the real exporter against a stub
docker (7 cases); test_sure_stack updated. check.py --tier smoke: 229 -> 236 passed (4 skipped); validate_config passes.

Not proven: the lab run (load tar, `docker compose config --images`, `up --pull
never`), a real image build of the fork, the hub's Docker image store, and that
`docker save`/`load` preserves the image id on the hub's store (the firstboot check
assumes it; a false FATAL would show on the first lab boot).

### 2026-10-10 fix round (review: REQUEST CHANGES)

firstboot compares a store-independent rootfs hash (new manifest column) instead of
`.Id`, and on a mismatch removes the tag, writes `/var/lib/homehub/image-load.state`
(healthcheck FAIL) and continues rather than aborting; `SURE_SOURCE_REF`
(`auroleap-integration`) names the build ref and the exporter refuses a wrong-ref
image; build-sure-image.sh refuses Podman and archives through `repo_git`;
validate_config.py checks the Sure knobs; tests are behavioural (21 cases).
Not proven: compose behaviour with a removed image (`pull_policy: never`) is
stub-reasoned, not run - in particular whether `docker compose up -d` still creates the
OTHER services when one image is missing, or aborts before creating any (then 4a-post
warns about many services and Caddy/Technitium may be down for the later steps); the lab
sequence. (`auroleap-integration` now exists in `../sure` at 87b606048: the coordinator
merged both fork branches; the merged tree passes Sure's full suite, 10397 runs, 0
failures, 1 known environment error.) Reviewed: Sonnet medium, REQUEST CHANGES, then
APPROVE after two fix rounds (c0cf2cc, db44672).

## 2026-10-13 — refactor programme: WI-06 to WI-10 and WI-24 (deploy held)

Refactor programme steps 2 and 6, on the MiniPC-Deployer side. Nothing here is deployed; the deploy is held until the Owner's go-ahead.

- Shared fixtures, byte-identical with the panel repository (OfficeWallNaglight): presentation snapshot (LLR-1126, TC-1220/1221), camera hierarchy (LLR-1127, TC-1222/1223), runtime paths (LLR-1128, TC-1224/1225), audio vocabulary (LLR-1129, TC-1226/1227) and Door configuration (LLR-1130, TC-1228/1229).
- Door configuration: the image's defaults, firstboot shell validators and credential list are checked against one fixture, including boundary values run in bash.
- WI-24, the public-site inventory: `ALLOWED_SITE_FILES` equals the panel's real site file set (85 files, LLR-1132, TC-1232), and the `REQUIRED` floor is a subset of the panel's capabilities manifest (LLR-1133, TC-1233).
- Full suite: 4209 passed. Two failures remain, both pre-existing tc1084 failures that this work did not touch.
- Not proven: nothing ran on the panel, and the image was not rebuilt or booted for these changes.
- Two tests named in the plan were not found in `tests/` and are not in the registry: TC-1234 (provider order) and TC-1235. They are listed as open in the report, not claimed here.

## 2026-10-13 — refactor programme WI-25 (final records)

Records only, MiniPC-Deployer side. No code or test logic changed; nothing here is deployed.

- Test-case rows added to `docs/test/test-cases.csv` for the seven LLRs that had tests but no row, so the trace can see them: TC-1220 and TC-1221 (LLR-1126), TC-1222 and TC-1223 (LLR-1127), TC-1224 and TC-1225 (LLR-1128), TC-1226 and TC-1227 (LLR-1129), TC-1228 and TC-1229 (LLR-1130), TC-1232 (LLR-1132) and TC-1233 (LLR-1133). TC-1230 and TC-1231 stay unassigned (held item); TC-1234 and TC-1235 have no rows.
- `scripts/trace.py`: orphans back to 25 (from 32), integrity 0. `scripts/check.py --tier smoke`: PASS.
- The Q5 power-policy change landed (merge `c7b83b7`): face unlock alone no longer keeps the panel's CPU awake, matching the settings hierarchy. LLR-1127 text is updated to match.

## 2026-10-13 — Session 9 merges: WI-14 touch defaults, A58 legacy KMS, wake fix (deploy held)

Entry written on 2026-10-14 from the merge messages; these three merges had no status entry. Nothing here is deployed.

- WI-14 touch defaults (merge `ff1e3e6`, Owner Q1 2026-10-13: "Agreed retain current panel functionality, update settings to match."): `render-touch-filter.py` takes every default from `touchfilter.core.Policy`, treats the four exact legacy values (60/1500/10000/2048) as unset exactly as the daemon's migration does, and emits only non-default keys. `wall.env.example` drops the four legacy lines. The panel's effective policy stays 40/0/60000/8192. The old renderer (`68443af`) is frozen under `tests/fixtures` and compared output for output (device name, udev rules, every non-policy field) in a 12-cell matrix; review round 1 caught a reused loop variable that would have renamed the touchscreen device and broken touch. LLR-1131, TC-1230/1231. Full suite at the merge: 4234 passed, 218 skipped, 2 failed (the known tc1084 pair).
- A58 `WALL_DRM_LEGACY_KMS` (merge `b4265a8`, Owner 2026-10-13: "As long as it's noted somewhere, I'd be curious about going back at some point."): the panel's temporary `profile.d` legacy-KMS override becomes a documented image knob, default true (legacy KMS, as the live panel runs). Missing, empty or invalid also mean legacy; only an explicit false selects atomic, so going back is one line. firstboot validates it and writes it to `kiosk.env`; `wall-kiosk.sh` exports `WLR_DRM_NO_ATOMIC=1` into cage's environment. Tests check a child process's environment and the block's position before the cage launch. LLR-1134, TC-1238/1239. Deploy step: once the image carries the knob, remove `/etc/profile.d/zy-wlr-atomic-test.sh` and confirm cage still has `WLR_DRM_NO_ATOMIC=1`.
- Wake fix (merge `4275f28`): a live defect found during the Sleep B design. `wall-local-setup.py` `validate_wake` still looked for the protocol-v3 `cameraFace` reading, so a dark screen could be woken by camera motion only; a still face, which the panel host does claim from the v4 `faceCount` reading, was refused. The validator now accepts a fresh positive `faceCount` (an int above 0) under presenceFace and consent, with every freshness rule unchanged; motion and Bluetooth behave as before. TC-1255, including a cross-repo test that drives OfficeWallNaglight's real status projection and forwards the claim as the host does.
- Reviews: each merge was Sonnet-built, then Sonnet medium and Codex Sol low over two rounds (APPROVE, APPROVE, APPROVE; A58 with nits).
- NOT PROVEN: nothing ran on the panel and the image was not rebuilt or booted for any of these. The A58 deploy step above has not been done.

## 2026-10-14 — panel site inventory for WI-05Q v2 / WI-05L

Records and inventory only; nothing is deployed and nothing ran on the panel.

- `1c5a437`: `ALLOWED_SITE_FILES` gains `js/published-state.js`, the new public site file from OfficeWallNaglight WI-05Q v2 (merged `9363338`). The inventory is exact, so without the line the image path refuses the site archive as carrying an unexpected file.
- `7b541d8`: the inventory follows WI-05L (OfficeWallNaglight `c149439`): `js/contact-verdict.js` and `js/paint-batch.js` added, `js/attention-lease.js` removed; the manifest gains the `attention-lease-v1` capability, accepted beyond the `REQUIRED` floor, which is untouched. Because a site file was REMOVED, the contract fixture had to be re-baselined as well, or it would still name the deleted file: `capabilities.json` byte for byte from `c149439`, `site-files.txt` by the `SOURCE.txt` recipe (87 files), `REVISION`, and the pinned count in `test_wall_capability_contract.py`.
- Tests (contract, sibling inventory TC-1232/1233, sensor-power-policy, local-capability): 204 passed, 1 skipped by design (the "not in the floor" case for the new capability).
- LLR-1132 no longer pins a literal file count; it says the inventory equals the panel's real set (87 at `c149439`).
- Deploy window: the site files and `ALLOWED_SITE_FILES` move with the OfficeWallNaglight app and site as one pair (HomeHub `scripts/deploy/PANEL_RELEASE.md`).

## 2026-10-22 — W6: meeting recording, both directions (OfficeWallNaglight WSN-097; SR-108..SR-111)

Branch `w6-meeting-recording`, NOT merged, NOT deployed. Owner ruling A69 (2026-10-22): a panel control and a
Bluetooth call for the dev PC both start a recording; an on-screen indicator whenever capture runs; audio only;
the hub's NVMe scratch volume holds the files.

- Feasibility measured on the panel with test tones (every capture deleted, the mixer restored, the Bluetooth link
  returned to its at-rest state): `mic_selected` and `bus_monitor` read together as two more dsnoop consumers,
  during a live HFP call from the dev PC too, at 4.9-5.5 % of one core.
- Panel: `wall-meeting-recorder.{py,service}` (own account `wall-meeting`, audio group, AF_UNIX only), one stereo
  Opus file per recording (left near, right far), capture only while the kiosk's indicator beats are under 1.0 s
  old (stop within 2 s of the indicator leaving the glass), spool cap and disk floor, firstboot step 8i, knob
  `WALL_MEETING_RECORDING` (default false). Forced command `wall-meeting-spool` for the hub's one key.
- Hub: `stack/meetings/` pull (publish by rename after a hash check, ack by hash) and retention (14 days proposed,
  then the 90 % high-water), scratch writers per W4's IF-071, firstboot step 6g-meetings, knob
  `MEETING_PULL_ENABLED` (default false).
- `scripts/assert_wall_capabilities.py` names `js/meeting-recording.js`: this branch and OfficeWallNaglight
  `w6-meeting-recording` merge together.
- Review: Codex gpt-6.1-sol medium, two rounds, APPROVE. Round 1: one BLOCKER (a beat for the previous recording
  could start the next) and four SHOULD-FIX, all fixed and mutation-checked; its quota finding was withdrawn by
  the reviewer in round 2 (the fix stays, defensive).
- No IF row: none was reserved for W6; the kiosk contract is stated in SR-108/LLR-1245 and an IF id is for the
  coordinator to assign.
- Lane review round 1 (two findings, both fixed):
  - F1 (BLOCKER): the 1.9 s budget ran from the kiosk's last beat, but the kiosk's beat handler trusted a display
    value up to 500 ms old, so the stop could land about 2.4 s after the screen went dark. The kiosk now samples
    the display for every beat (OfficeWallNaglight `indicatorBeatHandler`); SR-108/LLR-1242 state the bound from
    the indicator leaving the glass. New TC-1405 rig test sweeps 81 dark/poll phases with a capture that ignores
    SIGINT (worst 1.909 s) and shows that a modelled 500 ms display lag overruns 2 s.
  - F2 (SHOULD-FIX): a lost indicator kept a call's intent, so the capture resumed into a new file, and a
    restarted recorder took the call still up for a new automatic trigger; plan W6 task 3 says a recorder or kiosk
    crash finalises the partial and returns to idle. `indicator_lost` is now `capture_failed` (idle, that call
    suppressed until it ends), and the recorder remembers the call it was recording or had suppressed in
    `/var/spool/wall-meeting/call-memory.json` (0600, keyed to the boot id; not the RuntimeDirectory, which is
    removed on every stop so a stopped recorder shows the kiosk nothing). The resume test is replaced by a kiosk
    outage mid-call (one file, idle, no second capture, a manual start still records) and four restart tests (a
    new Recorder on the same files: the partial finalised, idle for the same `since` across two restarts, the
    memory cleared at the call's end, a new `since` records; a call that changed during the restart records; a
    reboot forgets; a stopped call stays stopped).
- NOT PROVEN: a real call's far end heard in a recording; the indicator on the real panel; the hub half on the real
  scratch volume (W4a); a real `systemctl restart` of the recorder mid-call on the panel (the rig builds a new
  Recorder on the same files, which is what a restart does, but no unit was restarted).
