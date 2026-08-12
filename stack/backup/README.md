# AWOW bash backup service (WI-10.10)

The homelab's main backup, running on the hub box as **pure bash + systemd**.
This service has no `.bat`/`.ps1` in it and is not going to grow any — but note
the rule it was written under **changed on 2026-08-09**: HOMELAB_TOPOLOGY.md
item 3 said "no `.bat`/`.ps1` anywhere in the pipeline" and now says they are
**avoided where possible**, because the absolute form also forbade a
*containerised* runner where nothing on the host is PowerShell. The host still
grows no PowerShell dependency and bash is still the default; the change is that
a container is no longer excluded on the letter of it.

That matters here because the direction of travel is toward one. The rewritten
**FileBackup** repo has been the behavioral *spec* this reproduces (hash
tracking, auto-compression-where-applicable, recovery/reconstruct) rather than
code to port — and the Owner has now directed that it be brought back and folded
in, because **this service copied its hash tracking and its restore contract but
not the storage model that made it fit on a disk**: it writes a full copy of
every set every night with no deduplication. See
`FileBackup/docs/homehub-integration.md` and item 0 of
`HomeHub/LOOP_TESTING_HANDOFF.md`.

It was built and validated end-to-end against the `sim/mini-serv-sim` Samba
fixtures in WI-10.15 (see `docs/status.md`), and the `run-backup-cycle-sim.sh`
leg added 2026-08-09 covers what only exists across runs.

## The pipeline — the six steps (HOMELAB_TOPOLOGY.md), 1 in three parts

| # | Step | Where |
|---|---|---|
| 1a | **wake** (optional) — a **Wake-on-LAN pre-step** for a source box that is allowed to sleep (`BACKUP_WAKE_MAC`; wait for tcp/445, LOUD failure on timeout) | `backup.sh` + `wake_and_wait` |
| 1b | **ingest** (ratified 2026-07-29) — mirror each `INGEST_SOURCES` network share **into the library tree** (`name=//host/share -> /abs/library/dest`: cifs-mount ro → `rsync -a --delete` → unmount), so the library holds the current copy and the flows below cover it like any other folder. **Mirror: source deletions propagate** | `backup.sh` + `ingest_parse` |
| 1c | **source pulls** — ONE `BACKUP_SOURCES` table, three source kinds (SR-013): `path:/dir` (local rsync — including the library folders just ingested, the intended pattern), `//host/share` (cifs-mount + `rsync`, the original direct form), `volume:VOL[@CONTAINER]` (rsync from the docker volume's mountpoint, optional stop→copy→restart quiesce) | `backup.sh` + `source_kind` |
| 2 | **archive + compress** — `tar` per set, `zstd` **where applicable** (already-compressed sets stored as plain `.tar`), minus the **excluded** patterns (`BACKUP_EXCLUDE` + per-set `name.exclude=`), which are logged, listed in `<set>.excluded.log` and recorded in the MANIFEST | `backup.sh` + `compression_decision` + `rsync_pull` |
| 3 | **hash + verify + manifest** — per-file sha256 table + archive sha256 + integrity test; a recovery MANIFEST | `backup.sh` |
| 4 | **external-drive target** — dated `run_<UTC>` snapshot with retention (`BACKUP_KEEP`) | `backup.sh` |
| ~~5~~ | **offsite — DELETED 2026-08-09.** The service stages nothing offsite; the IceDrive client syncs library paths directly. `OFFSITE_PATH` / `OFFSITE_UNC` / `OFFSITE_SETS` are still recognised and now **refuse the run** rather than being ignored | — |
| 0 | **target preflight** — refuse to run unless `BACKUP_TARGET` is a real mountpoint (and not `ro`); posts `ok=false` and exits 1 if not. Zero disk I/O, so it never wakes a parked drive | `backup.sh` + `common.sh` `mount_options_for` |
| 6 | **report** — POST NagLight `/api/feed`; **never-silent-green** (failure → `ok=false` + nonzero exit) — the ERR trap **and** every `die` path (OI-9) | `common.sh` `feed_naglight` |

## Files

```
backup.sh            orchestrator (the six steps; --config, --dry-run)
restore.sh           reconstruct + byte-verify a set from a run (the recovery half)
common.sh            shared helpers (logging + die/report hook, wake-on-LAN, cifs mount,
                     compression policy, feed, drive power)
backup-standby.sh    boot-time DEFAULT STANDBY oneshot (WI-10.10 drive power)
backup.env.example   every knob
systemd/homehub-backup.{service,timer}   nightly root oneshot + persistent timer
systemd/backup-standby.service        per-boot backup-drive spin-down default
```

## Waking a source box that sleeps (step 1 pre-step)

The Windows game box exposes **one** share and is allowed to **sleep**, so the
run wakes it before pulling: send a Wake-on-LAN magic packet, then block until
the host answers **tcp/445** (the SMB port the pull actually needs), re-sending
the packet every 15 s.

- **The packet is best-effort; the wait is the truth.** The run proceeds only
  once the port answers. If the box was never asleep the probe short-circuits
  and no packet is sent.
- **A wake timeout FAILS THE RUN, loudly** — `ok=false` to the feed and a
  nonzero exit. Never-silent-green means a source that failed to *wake* must
  never be mistaken for a source with nothing new to copy.
- **No new dependency:** the packet goes out over bash's own `/dev/udp`.
  Bash cannot set `SO_BROADCAST` on that socket, so some kernels refuse a
  broadcast destination — `wakeonlan` (or `etherwake`) is then used
  automatically if installed, and the log says plainly when neither worked.
- Knobs: `BACKUP_WAKE_MAC` (**empty = the feature is off**), `BACKUP_WAKE_HOST`,
  `BACKUP_WAKE_TIMEOUT` (default 120 s), plus optional `BACKUP_WAKE_BROADCAST` /
  `BACKUP_WAKE_IFACE`. The **Windows side** must also have "Wake on Magic
  Packet" enabled on the wired adapter and **Fast Startup off** — hybrid
  shutdown leaves the NIC unable to wake.

## Ingest (step 1b) — the library holds the current copy

**Ratified 2026-07-29.** A source on another box is not pulled straight into a
tarball any more: it is first **mirrored into the library tree**, and the library
folder is then backed up as an ordinary `path:` set. One line per source:

```
INGEST_SOURCES="gamebox=//mini-serv/mini-pc-share -> /srv/library/NonDocs/MiniServ"
BACKUP_SOURCES="gamebox=path:/srv/library/NonDocs/MiniServ"
```

- **Why:** the library becomes the single current copy of that data — usable,
  browsable and syncable (the IceDrive client points at library paths, step 5) —
  while the archive flow stays exactly one flow for every folder.
- **Mirror semantics (`rsync -a --delete`):** the library copy is made to
  **match** the share, so **a file deleted on the share disappears from the
  library** on the next run. History is the dated run snapshots under
  `BACKUP_TARGET` (`BACKUP_KEEP` of them), **not** the library.
- **Mirror safety:** if a share *mounts* but holds **no files** while the library
  copy does (wrong share name, a host that booted with its data drive unmounted),
  the run **refuses and fails loudly** rather than let `--delete` erase a good
  copy. `INGEST_ALLOW_EMPTY=true` is the explicit override.
- **Destination discipline:** the leaf directory is created on first ingest, but a
  **missing parent is fatal** — that is a typo or an unmounted library
  filesystem, and mirroring into a stray directory while the `path:` set keeps
  archiving the stale folder would be green and wrong.
- The **wake** pre-step (1a) runs first, so a sleeping source box is awake before
  the first mount; ingest reuses it rather than owning a second wake.
- Pulling a share **directly** (`name=//host/share` in `BACKUP_SOURCES`) is still
  fully supported — it just leaves no current copy in the library.
- `--dry-run` passes `--dry-run` to the mirror too: a dry run never writes to the
  library.

## Exclusions (step 2) — and why nothing is excluded silently

Two knobs, both space-separated glob lists with rsync's semantics (a pattern with
no `/` matches that name at **any** depth):

- **`BACKUP_EXCLUDE`** in `backup.env` — global, applies to every set
  (`BACKUP_EXCLUDE="*.bak"`).
- **`name.exclude=PATTERN …`** — extra lines *inside* the `BACKUP_SOURCES` table,
  adding to the global list for that one set. This is where a **named very-large
  folder** belongs. A `name.exclude=` line naming a set that does not exist
  **fails the run** (a filter that silently excludes nothing is worse than none).

The patterns go to `rsync` at pull time (so the copy is never made — that is what
saves the time and space) **and** to `tar` (so the archive cannot contain them).
Visibility is the requirement, so every run:

- logs the **effective pattern list per set** (`[docs] exclude patterns: *.bak Downloads`);
- logs each path the patterns actually hid — the first few inline, **all** of them
  in **`<set>.excluded.log`** next to the archive (rsync's own `--debug=FILTER`
  decisions: `hiding file sub/deep.bak because of pattern *.bak`);
- records the patterns in the MANIFEST's **`excludes`** column;
- makes `restore.sh` state plainly that such a set is a **FILTERED copy** of its
  source, so a missing file after a restore is explained, never a mystery.

## Offsite — retired 2026-07-29, deleted 2026-08-09

**The backup service performs no offsite staging at all**, and as of 2026-08-09
there is no code that could.** The IceDrive client (the SR-015 opt-in desktop
session) is pointed **directly at chosen library paths in its own GUI** and syncs
them itself — which is also why step 1b puts the current copy *in the library* in
the first place. The target state is therefore **`OFFSITE_ENABLED=false`**, which is
now the only legal value.

The step was carried as "legacy, still functional" for six weeks after the design
changed; the Owner ruled to delete it. **The knobs remain recognised on purpose:**
setting `OFFSITE_PATH`, `OFFSITE_UNC` or `OFFSITE_SETS` fails the run at start
with a message naming the knob. A box whose config asks for an offsite copy must
not silently receive a backup that has none — a config quietly ignored is the
same family of fault as a green run that wrote nothing.

Two consequences to keep in mind: *which* folders reach the cloud is configured
in the client (authoritative list: `Personal\deploy\storage-map.md` §4e), and the
client is a GUI app — **sync is down after every reboot until one desktop session
is opened** ([../remote-ui/README.md](../remote-ui/README.md)) — a staleness this
service's NagLight report cannot see.

**And it cannot come back by accident.** There is no offsite code path left to
re-enable — `OFFSITE_ENABLED=true` is itself refused. If an offsite step is ever
wanted again it has to be written deliberately, which is the correct cost for
re-adding a second copy of the household's data.

## Drive power / spin-down (WI-10.10 DRIVE POWER DESIGN)

The backup drive(s) are the box's biggest electrical lever (5–8 W each spinning ≈
the whole CPU), so the service manages their standby with a **dynamic** policy:

- **At boot** — `backup-standby.service` (a per-boot oneshot; `hdparm -S` does not
  persist across power cycles) sets a conservative default spin-down timeout on
  each `BACKUP_DRIVE_DEVICES` entry using `BACKUP_DRIVE_STANDBY`.
- **During a run** — `backup.sh` **disables** standby (`hdparm -S 0`) on its
  target drive(s) at the start and **restores** the configured timeout on exit
  via a shell trap that fires on **success, failure, or interrupt**. This avoids
  both start/stop churn during long no-write phases (source hashing, verify) and
  aggressive-timeout cycling.

Knobs (`backup.env.example`): `BACKUP_DRIVE_DEVICES` (space-separated
`/dev/disk/by-id/...` paths — **by-id, never `sdX`**, which the kernel renumbers)
and `BACKUP_DRIVE_STANDBY` (default `241`). The `hdparm -S` encoding is
notoriously confusing — `1..240` = value × 5 s (so `240` = 20 min) and
`241..251` = (value − 240) × 30 min (so `241` = 30 min) — documented in
`common.sh` and `backup.env.example`.

### The target must be a mountpoint (step 0, added 2026-08-01)

`nofail` in the generated fstab is mandatory — a missing USB disk must not hold
up `local-fs.target` and drop a headless box to an emergency shell. Its cost is
that an **unplugged backup drive leaves `BACKUP_TARGET` as an ordinary empty
directory on the system disk**. Before step 0 existed, a run in that state
created its dated folder there, copied into it, verified it (the files really
were present), pruned old runs, and posted **`ok=true`** — a green lane writing
the household's backups to the wrong disk until that disk filled.

Step 0 refuses instead, and reports `ok=false`. Deliberately NOT done as
`RequiresMountsFor=` on the unit: systemd would refuse to *start* the service, so
**nothing** would reach NagLight — an unreported non-run, which is worse than a
red one. Escape hatch for a target that is legitimately a plain directory:
`BACKUP_TARGET_REQUIRE_MOUNT=false` (the run then warns loudly every time).

The presence of the drive is *also* now watched independently of the run, by
`homehub-backup-drive-health.timer` every 10 minutes — check id
**`backup-drive-mounted`**, the backup-drive twin of `library-mounted`. Both use
`samba/library-guard.sh`, whose only probe is `/proc/self/mountinfo`: the answer
comes from the kernel's mount table and **no request ever reaches the device**,
so a 10-minute cadence cannot fight the spin-down policy below. Before this, an
absent backup drive was invisible until 03:30 the next morning.

**Power management NEVER fails a backup:** a missing `hdparm`, an absent device
path, or an enclosure that rejects the command is logged as a WARNING and skipped.
An **empty `BACKUP_DRIVE_DEVICES` is a clean no-op** (no boot standby, no run-time
hold). **Caveat:** many USB-SATA enclosures ignore `hdparm` APM/standby entirely
(the bridge chip swallows the command) — verify each drive at hardware burn-in
with `hdparm -C /dev/disk/by-id/...`. The call contract + failure-path
composition are proven in `sim/mini-serv-sim/run-drivepower-sim.sh` (mock-`hdparm`
shim); the drive's physical response is a burn-in-only check.

## Docker-volume sources (SR-013 / OI-8)

The box's OWN service state — `actual_data` (the finances), `technitium_config`,
`caddy_data`, `tracker_data`, any tier-2 volume — rides the same table and
pipeline as the LAN shares: add `name=volume:VOL` lines to `BACKUP_SOURCES`
(commented examples in `backup.env.example`) and, for the important ones, the
set names to `OFFSITE_SETS`.

- **Quiesce (`volume:VOL@CONTAINER`)** — stops the container, copies, restarts
  it immediately (downtime = the copy, seconds). Use it for volumes holding live
  databases (actual, technitium); a live copy can catch a mid-write state. The
  EXIT trap restarts any still-stopped container on **every** exit path — a
  failed backup never leaves a service down — and a restart failure is a LOUD
  warning naming the manual fix.
- **Restoring a volume set:** `restore.sh` reconstructs to a directory as usual;
  putting it back into a (stopped) volume is
  `docker run --rm -v VOL:/dst -v <restored>:/src:ro alpine sh -c 'cp -a /src/. /dst/'`
  — deliberate manual step, like all restores here.
- Call contract + failure paths proven in `sim/mini-serv-sim/run-volume-sim.sh`
  (mock-`docker` shim, same pattern as the drive-power leg): volume set archives
  + restores byte-equal, stop-before-copy/start-after ordering, mid-copy failure
  still restarts + posts `ok=false`, cifs-only config makes zero docker calls.

## Recovery MANIFEST / state format

The legacy `*FilesHashTable.csv` files (FileBackup's own hash-tracking state,
INVENTORY.md) are the prior art. Per run, under `BACKUP_TARGET/run_<UTC>/`:

- **`MANIFEST.tsv`** — one row per set:
  `set · source · archive · algo · archive_sha256 · files · bytes · reason · excludes`
  (`excludes` is `-` when nothing was filtered; runs written before exclusions
  existed simply have no such column and restore fine).
- **`<set>.files.tsv`** — the per-file hash table (the `*FilesHashTable.csv`
  successor): `sha256 · size · mtime_epoch · relpath` for every file in the set.
- **`<set>.tar` / `<set>.tar.zst`** — the archive (algo per step 2).
- **`<set>.excluded.log`** — only when the set had exclude patterns: one line per
  path the patterns hid, with the pattern that hid it.
- **`RUN.json`** — run summary (status, ingest, totals, per-set sizes +
  per-set `excludes`).
- **`backup.log`** — the run log.

`restore.sh --run <run_dir> --set <name> --target <dir>` verifies the archive's
`archive_sha256`, extracts it, then checks **every** restored file against
`<set>.files.tsv` (sha256 + size). It **fails loudly**: restores everything
recoverable, then exits nonzero naming the count it could not verify; a clean
restore exits 0.

## Hash-algorithm delta vs FileBackup

FileBackup uses **xxHash128** (speed). This service uses **sha256** — coreutils-
native, so the hub needs no extra hashing dependency. The hash is an internal
integrity/dedup choice for a self-contained backup+restore leg; both give
byte-exact verification. (The FileBackup *restore* format — `bash/reconstruct.sh`
+ `MANIFEST.csv` — is a different, content-addressed layout; this service is the
tar-archive pipeline the topology's six steps describe, not that layout.)

## Run on the hub

```bash
sudo cp backup.env.example /etc/homehub-backup/backup.env   # then edit
sudo install -m600 /dev/stdin /etc/homehub-backup/cifs.creds <<< $'username=awow\npassword=…'
sudo cp systemd/homehub-backup.{service,timer} /etc/systemd/system/
sudo systemctl enable --now homehub-backup.timer
sudo systemctl start homehub-backup.service     # run once now
journalctl -u homehub-backup.service -f
# Boot-time drive standby (WI-10.10) — the autoinstall enables this for you; to
# do it by hand (runs in place from this dir so it can source common.sh):
sudo cp systemd/backup-standby.service /etc/systemd/system/
sudo systemctl enable --now backup-standby.service
```

## Try it against the sim fixtures

`sim/mini-serv-sim/run-backup-sim.sh` stands up the Samba fixtures + a privileged
runner, runs a full cycle against them, and does the restore drill.
`sim/mini-serv-sim/run-drivepower-sim.sh` proves the WI-10.10 `hdparm` standby
call contract (disable-at-start, restore-on-exit including the failure path, and
the empty-device no-op) with a mock-`hdparm` shim.
`sim/mini-serv-sim/run-volume-sim.sh` proves the SR-013 volume-source contract
(archive/restore byte-equality, quiesce ordering, failure-path restart +
`ok=false`, cifs-only zero-docker no-op) with a mock-`docker` shim.
`sim/mini-serv-sim/run-ingest-sim.sh` proves the **ingest** and **exclusion**
steps against the real Samba fixtures (mirror byte-equality + restore,
deletion propagation, global/per-set exclusions and their visibility, the
empty-share refusal + its override, and three loud config failures).
See `sim/README.md`.

The **cycle leg** (`run-backup-cycle-sim.sh`) covers what only exists across runs:
a second run after files are added/deleted/modified (and the previous run still
holding the deletion), retention pruning the oldest, a missing `path:` and a
missing `volume:` source each skipping only their own set, and the two volume
failures that must stay fatal — an ambiguous compose-label match and an
unreachable daemon.

**Honest gap:** the wake pre-step has no sim leg; see `docs/status.md`.
