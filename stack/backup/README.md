# The hub's backups — two services, one drive

**There are two backup services on this box and neither replaces the other.**
That is the first thing to know before reading anything below, because almost
every question here ("which timer? which check id? which one prunes?") is really
a question about which of the two you are looking at.

| | `homehub-backup` (bash) | `homehub-library-backup` (container) |
|---|---|---|
| **What it protects** | cifs **ingest** from Mini-serv, the five `volume:` sets (Actual, Technitium, Caddy, tracker, finance), the Mini-serv `path:` set | the **nine library `path:` sets** — the ~4 TB tree that had no archive at all |
| **How** | `tar` + `zstd` per set into a dated `run_<UTC>` folder | **FileBackup** in a container: per-file dedup, a browsable **mirror**, `Snapshot_<date>` history |
| **Storage cost** | a **full copy per run**, `BACKUP_KEEP` of them | one mirror + deltas; growth is bounded by **change rate**, not run count |
| **Runs** | `homehub-backup.timer`, 03:30 | `homehub-library-backup.timer`, 21:30 (**Q-FB6**, the Owner may move it) |
| **Feed lane** | `backup` | `library-backup` |
| **Retention** | `BACKUP_KEEP`, pruned after a good run | **none — nothing prunes.** See below |
| **Entry point** | `backup.sh` | `library-backup.sh` (host) → one `docker compose run` |
| **Wake-on-LAN, drive power, ingest** | yes | reuses the same `common.sh` drive-power helpers; no WoL, no ingest |

A **merge, not a swap** (the Owner, `HomeHub/LOOP_TESTING_HANDOFF.md`). The bash
service's coverage is unchanged by the container's arrival. What the container
**dissolves** is the NVMe staging bomb for the library sets specifically:
FileBackup reads the source in place, so those nine sets stage nothing. Ingest
and the `volume:` sets still stage.

The rest of this document is the bash service; the container's half is
[the library backup](#the-library-backup-filebackup-in-a-container) near the end.

---

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
| 4 | **external-drive target** — dated `run_<UTC>` snapshot with retention (`BACKUP_KEEP`). A `--plan` run writes `plan_<UTC>` instead, on its own `BACKUP_PLAN_KEEP` budget | `backup.sh` |
| ~~5~~ | **offsite — DELETED 2026-08-09.** The service stages nothing offsite; the IceDrive client syncs library paths directly. `OFFSITE_PATH` / `OFFSITE_UNC` / `OFFSITE_SETS` are still recognised and now **refuse the run** rather than being ignored | — |
| 0 | **target preflight** — refuse to run unless `BACKUP_TARGET` is a real mountpoint (and not `ro`); posts `ok=false` and exits 1 if not. Zero disk I/O, so it never wakes a parked drive | `backup.sh` + `common.sh` `mount_options_for` |
| 6 | **report** — POST NagLight `/api/feed`; **never-silent-green** (failure → `ok=false` + nonzero exit) — the ERR trap **and** every `die` path (OI-9) | `common.sh` `feed_naglight` |

## Files

```
backup.sh            orchestrator (the six steps; --config, --plan)
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
- `--plan` passes rsync its own `--dry-run` for the mirror: a plan run never writes to the
  library. (rsync's `--dry-run` genuinely writes nothing, which is exactly the
  promise this script's own mode could not keep — see `--plan` below.)

### `--plan` — what it does and does not do (C25, Q1, Q3)

`--plan` plans the run, reports on it, and writes **no archives**. It was called
`--dry-run` until 2026-08-29 and that name was retired, because it was not true:
the mode was found writing to the backup drive on every invocation, and it was
found the only way it could be — the Owner pulled the USB stick and looked. The
Owner then ruled: **the behaviour is acceptable, the word was not.**

**`--dry-run` no longer exists.** It survived the morning of 2026-08-29 as an
accepted alias and was **removed the same evening** (Q3), once a sweep of both
repos found nothing calling it: `verify-hub.sh` asks the *deployed* `backup.sh`
which name it knows and picks accordingly, so it works against a box on either
payload without the alias existing here. Passing it now **fails the run** with a
message naming `--plan` — deliberately, rather than a bare `unknown arg`, because
the only caller left is a human hand and a backup that refuses to run should say
what to type instead.

What a plan run still does, in full:

| | |
|---|---|
| writes `$BACKUP_TARGET/plan_<ts>/` | `backup.log`, a header-only `MANIFEST.tsv`, one `<set>.excluded.log` per set — ~16 files on the **backup drive** |
| mounts every cifs source | read-only, and unmounts again |
| issues `hdparm -S 0` | on `BACKUP_DRIVE_DEVICES`, restoring the timeout on exit — **on a box with the real archive drive attached, a plan run spins it up** |
| writes no archive, no hash table, no `RUN.json` | so nothing it leaves behind can ever be mistaken for a restorable run |
| prunes older `plan_*` directories | to `BACKUP_PLAN_KEEP` (default `BACKUP_KEEP`) — see below |

It is therefore safe to point at production, and it is **not read-only**. Both
halves of that sentence matter. The run says all of this in its own log, at the
start and again at the end, naming the file count and the path — so the next
person to find one of these directories on the drive can read why it is there.

#### The directory is `plan_<ts>`, not `run_<ts>` (Q1)

Until 2026-08-29 a plan run wrote `run_<ts>/`, shaped exactly like a real archive
run. **That is how C25 stayed invisible**: twelve of them on the stick looked
like twelve backups, and only opening one and reading `dry_run=1` said otherwise.
The prefix now carries the answer, with no log to read.

Renaming it needed retention to grow a budget for the new prefix, and **who**
pays that budget is the part worth knowing:

- **A plan run prunes plan directories itself**, to `BACKUP_PLAN_KEEP`. The
  obvious alternative — let the nightly's `retention_prune` do it — is wrong,
  because retention only executes after a **green** full run (fixed 2026-08-09,
  and correct: a red night must not rotate away good nights). Under that design
  plan litter would be bounded by the *nightly's health*, so a box whose backup
  is failing would accumulate log directories on exactly the drive whose free
  space the failure may be about.
- **A green nightly prunes them too**, so a box that is verified once and never
  again does not keep that one directory forever.
- The pruner **refuses** any `plan_*` directory that holds a `RUN.json` or an
  archive, and says so loudly. It cannot touch `run_*` at all.
- `BACKUP_PLAN_KEEP=0` is legal (a plan directory holds only logs, so keeping
  none destroys nothing). `BACKUP_KEEP=0` is still refused.

`newest_run_with_set` — the lookup behind `restore.sh` and firstboot's ACME
restore — globs `run_*`, so it can no longer even *offer* a plan directory as a
candidate.

`restore.sh` recognises a plan run and exits **3** ("this run holds no copy of
this set, and that is recorded, not damage") rather than reporting damage. It
reads three spellings: the `plan_` **prefix** (which needs no log at all, so a
plan run that died before writing one is still identified), `mode=plan` in the
log, and the legacy `dry_run=1`. The last two are about **data already on the
drive**, not about the removed flag — twelve such directories are on the
household's stick right now.

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
in the client (authoritative list: `Personal\deploy\storage-map.md` §4e), and
this service's NagLight report cannot see that copy's staleness at all.

> **The second half of this used to say the client is a GUI app and "sync is
> down after every reboot until one desktop session is opened". Both halves were
> wrong** and are corrected as of 2026-08-27: there is a headless CLI
> ([../icedrive/README.md](../icedrive/README.md)), and the reboot claim was
> never measured. The *reporting* gap above is real and unchanged — but it now
> has a plausible fix, because a CLI FUSE mount at `OFFSITE_PATH` would put the
> offsite copy back inside this pipeline where NagLight can see it. Not wired up
> yet; it depends on questions the icedrive README lists.

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

---

## The library backup — FileBackup in a container

The second service. `library-backup.sh` is a **host** wrapper around exactly one
`docker compose run`; everything else in it is proof that the run should happen.

```
library-backup.sh                        the host side (bash, root, one-shot)
filebackup.json                          the container's config — TRACKED, installed
                                         to /etc/homehub-backup/filebackup.json 0644
systemd/homehub-library-backup.service   Type=oneshot, TimeoutStartSec=infinity
systemd/homehub-library-backup.timer     21:30 (Q-FB6 default), Persistent=true
```

The image is `filebackup:${FILEBACKUP_IMAGE_TAG}`, built from a sibling
`../FileBackup` checkout by `scripts/ensure-local-images.sh` (**always with
`--rebuild`** — the resolver skips an image that already exists, and a
hand-tagged one carries no `homehub.source.revision` stamp) and baked into the
ISO payload by `vmtest/export-images.sh`, which enables the `filebackup` profile
unconditionally for the bake.

### The profile is deliberately not in `COMPOSE_PROFILES`

`filebackup` must never be added to `COMPOSE_PROFILES` in any `.env`. Firstboot
runs a plain `docker compose up -d`, and a profiled-in backup service would
launch a **multi-day library backup on a box that was imaged ten minutes ago**.
`compose run` enables the profile implicitly, which is why the wrapper does not
need it enabled anywhere. The image still reaches the payload because the bake
asks for the profile itself.

### What the wrapper does before it starts anything

| Step | Refuses when | Why it exists |
|---|---|---|
| 1. `flock` | another run holds the lock | the first real run is a **multi-day** job; the nightly timer would otherwise stack a second container into one `/state` and one `/backup`. A held lock **posts nothing** — the lane belongs to the run that is still going — but exits non-zero |
| 2. mount identity | `/srv/library` or the backup drive is absent or read-only | `nofail` in the fstab means an absent drive leaves an ordinary empty **directory on the system disk**. Reuses `samba/library-guard.sh`, cross-checked against the generated fstab and `drive-identity.conf`; a **stand-in drive is yellow**, carried into the summary, not a refusal |
| 3. capacity | free space below `BACKUP_LIBRARY_MIN_FREE_GB` (or the state/logs floor) | there is **no retention** — this floor is the whole space policy |
| 4. pre-create | — | docker **chowns** a bind source it had to create, and that is `EPERM` on the FAT-family data drives, which takes the *entire* compose command down. The config file and the library source are asserted, never created |
| 5. write-probe | uid 65532 cannot write `/backup` or `/changes` | NTFS/exFAT ownership is synthesized from mount options, so only the fstab's `uid=65532,gid=65532` grants access (**Q-FB5**). A regression must fail in the first seconds, not at hour eight |

Then it holds the drive awake (`hdparm -S 0`, restored on every exit path),
runs the container, applies the verify gate, and posts to NagLight.

`library-backup.sh --preflight-only` runs steps 1–5 and stops without creating
or starting anything.

### Exit codes — two different tables

The **backup** action (`SR-043`), which has no code 3:

| | |
|---|---|
| **0** | complete |
| **1** | a set failed — a data/IO problem; read `Backup_Global.log` |
| **2** | the configuration could not be loaded, or violates the contract. **Retrying will not help** |

The **verify** action uses the restore-side table (`SR-040`), shared with
`reconstruct.sh` — **0** complete · **1** incomplete (content) · **2** usage or
precondition · **3** manifest-witness mismatch · **4** incomplete (host,
retriable); precedence **2 > 3 > 4 > 1**. Each maps to its own message, because
one generic "backup failed" puts a human in the container log every time.

### Retention: none, by ruling — and the manual prune

**Q-FB2 (the Owner, 2026-08-23): retention is NONE for now.** Snapshots are kept
indefinitely and **the wrapper prunes nothing**. Growth is bounded by *change
rate*, not run count — a run that supersedes nothing creates no snapshot at all —
which is why unbounded-by-policy is tolerable here and would not be for the eight
full copies this replaces.

The burden therefore falls entirely on **loud space surfacing**: the capacity
floor is a hard refusal posted **red**, and every successful run's summary
carries the drive's **fill percentage**, so the trend is visible long before the
floor is near.

When the Owner does want space back, the procedure is manual and in this order:

```bash
cd /opt/homehub/stack
# 1. list what exists. The output is line-framed JSON: take exactly the [ .. ]
#    block by line — never a greedy regex. Empty is `[]`.
docker compose --profile filebackup run --rm -T filebackup snapshots
# 2. DRY RUN the removal first.
FILEBACKUP_DRY_RUN=1 docker compose --profile filebackup run --rm -T filebackup \
    prune -Snapshot Snapshot_2026-08-23
# 3. then for real.
docker compose --profile filebackup run --rm -T filebackup prune -Snapshot Snapshot_2026-08-23
```

**Never delete a `Snapshot_*` folder by hand.** It is the only copy of the states
it holds, and the manifests that make the rest reconstructable refer into it.
These actions use the **restore-side** code table above, not the backup one.

### Rollback

The container can be taken out of service without touching the bash service or
the data:

1. **Stop it running.** `systemctl disable --now homehub-library-backup.timer`,
   and `systemctl mask homehub-library-backup.timer` if it must not come back
   through a later `enable`.
2. **The image stays and never runs.** It is behind the `filebackup` profile,
   which is in no `.env`, so `docker compose up -d` cannot start it — leaving the
   image in place costs a few hundred MB and keeps the rollback reversible. To
   remove it from future ISOs as well, drop `--profile filebackup` from
   `PROFILE_ARGS` in `vmtest/export-images.sh`.
3. **Reclaim the space by hand.** `rm -rf /mnt/backup-drive/library
   /mnt/backup-drive/library-changes`. Both trees are self-contained, so deleting
   them needs no tooling and leaves nothing dangling. The manifest cache at
   `/var/lib/homehub-filebackup/state` and the logs at
   `/var/log/homehub-filebackup` go the same way.

   > **This deletes the only copy of every state it holds.** Since WP9 the backup
   > root is content-addressed, not a browsable mirror (Q-FB1 withdrawn
   > 2026-08-26), so you cannot eyeball what you are about to lose — the file
   > names are opaque hashes. Take an inventory first:
   > `docker compose --profile filebackup run --rm -T filebackup snapshots`.
4. **The bash service is unaffected.** Its sets, its timer, its `backup` lane and
   its retention are all independent of any of the above. What you lose is the
   library coverage, which is what there was before this shipped.

### The config file

`filebackup.json` is **tracked** and installed to
`/etc/homehub-backup/filebackup.json` at 0644 root by autoinstall late-command
5c — not through the `site/` payload the secrets take. Every path in it
(`/source`, `/state`, `/backup`, `/changes`) is a **container-internal mount
target** fixed by `docker-compose.yml`; it derives from the storage map not at
all and carries no secret, no serial and nothing site-specific. The host paths it
pairs with are the `FILEBACKUP_*` knobs in `stack/.env`.

JSON carries no comments and the schema is **closed** (`additionalProperties:
false` — an extra key is a contract violation, exit 2), so the settled choices
are recorded here instead:

- `HashRecalcFreq: "W"` — production. The reconstruction drill overrides it to
  `"A"` in a drill-local copy, never this file: under `"W"` a same-length
  in-place edit whose mtime lands in the same coarse exFAT tick is silently
  skipped, and that state would be unreconstructable.
- `CompressEnabled: true` (**Q-FB4**). FileBackup's own
  `NonCompressibleExtensions` list already contains the union of both services'
  exemptions, so already-compressed content is skipped for us.
- `PreserveFolderTree` — **REMOVED. Do not add it back.** It selected *Mirror*
  mode (**Q-FB1**, ruled 2026-08-23), which made the backup root a browsable
  mirror of the library. FileBackup's WP9 deleted Mirror outright: storage is
  always content-addressed now, and a config still carrying this key is
  **refused by name** before anything runs. Q-FB1 was withdrawn by the Owner on
  2026-08-26 — there is no longer a mode to choose.

  **What this changes for recovery:** the backup root is a flat directory of
  opaque hash-named objects. `ls` tells you nothing and neither does a file
  manager; the manifest is the index and `reconstruct.sh` is the reader. The
  restore kit still ships **inside** the backup, so a rescue machine still needs
  no tooling installed — see "Restoring without the container" below.
- `AllowEmptySource` is **absent**, and that absence is load-bearing: the default
  is `false`, which refuses to empty a populated backup when the source comes up
  empty — the safety net for a library drive that failed to mount.
- No `Tools` block: the image sets `FILEBACKUP_7ZIP_PATH=/usr/bin/7z` itself.
- No `Secrets` block: containerized runs are `-NoMail`, and JSON cannot carry a
  `PSCredential` anyway.

Validate any edit against `FileBackup/container/FileBackup.schema.json`.

### Restoring without the container

The three packages `xxhash`, `gawk` and `p7zip-full` are in
`autoinstall/packages.list` for the **restore** side, not the backup: every
snapshot ships `bash/reconstruct.sh`, which runs on coreutils plus those. `gawk`
is named separately because Ubuntu Server ships **mawk**, and `reconstruct.sh`
parses the manifest with `FPAT` — a GNU extension that mawk does not merely lack
but mis-splits silently.

```bash
bash reconstruct.sh --target-root /var/tmp/restore --from <snapshot-or-backup-root> \
    --backup-root /mnt/backup-drive/library --change-root /mnt/backup-drive/library-changes \
    --require-witness
```

**`--require-witness` on every restore** — without it a missing manifest sidecar
only warns. The explicit root flags exist for exactly this split-mount shape; on
a rescue machine, `cd` onto the drive and pass `--from` alone and it auto-detects.
