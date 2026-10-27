# The hub's backups — two services, one drive

**There are two backup services on this box and neither replaces the other.**
That is the first thing to know before reading anything below, because almost
every question here ("which timer? which check id? which one prunes?") is really
a question about which of the two you are looking at.

| | `homehub-backup` (bash) | `homehub-library-backup` (container) |
|---|---|---|
| **What it protects** | cifs **ingest** from Mini-serv, the five `volume:` sets (Actual, Technitium, Caddy, tracker, finance), the Mini-serv `path:` set | the **nine library `path:` sets** — the ~4 TB tree that had no archive at all |
| **How** | `tar` + `zstd` per set into `/mnt/backup-drive/config-history` — one dated copy per night plus a Fibonacci ladder of older samples (`BACKUP_LAYOUT=fib`, 2026-09-08; `flat` on the library drive before that, and a dated `run_<UTC>` on the archive drive root before that) | **FileBackup** in a container: per-file dedup, a browsable **mirror**, `Snapshot_<date>` history |
| **Storage cost** | 7 dailies + 7 ladder rungs = **14 copies**, ~34 MB in total at the current 2.4 MB per run. Self-contained: the column to the right no longer versions it | one mirror + deltas; growth is bounded by **change rate**, not run count |
| **Runs** | `homehub-backup.timer`, 03:00 box-local | `homehub-library-backup.timer`, also 03:00 box-local, in the same systemd transaction and ordered `After=homehub-backup.service` (so it follows the nightly run; **Q-FB6**, the Owner may move it) |
| **Panel health** | posts only `nightlySuccess` (verified, promoted run) to the one File Backups row; a failure posts nothing, so the age grows | sole writer of verified `lastSuccess` for `file-share-backup-health` |
| **Retention** | `BACKUP_FIB_DAILY_KEEP` (7) rolling dailies, plus a **cascading** Fibonacci ladder: rung F(n) is refilled from F(n-1) every F(n-2) days, up to 233. `BACKUP_KEEP` is not read. | **none — nothing prunes.** See below |
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

Sure adds a `postgres:CONTAINER` logical source (SR-065, LLR-1045) to this
pipeline. It uses the running database image's pg_dump and pg_restore tools;
no raw PGDATA copy. See [Sure's backup contract](../sure/README.md#secrets-and-acceptance)
for LAN-only storage, profile activation, failure handling and unproven restore
acceptance. The timer, mount preflight, archive/hash/manifest and retention are
the existing machinery; no separate cron or backup container is introduced.

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
| 4 | **target** — `BACKUP_LAYOUT=flat`: the archives land in `BACKUP_TARGET` itself, one current copy, no retention (a `--plan` run gets one `plan_latest` slot). `BACKUP_LAYOUT=dated`: a `run_<UTC>` snapshot with `BACKUP_KEEP` retention, and `plan_<UTC>` on its own `BACKUP_PLAN_KEEP` budget. See [Layout](#layout--dated-vs-flat-2026-09-01) | `backup.sh` |
| ~~5~~ | **offsite — DELETED 2026-08-09.** The service stages nothing offsite; the IceDrive client syncs library paths directly. `OFFSITE_PATH` / `OFFSITE_UNC` / `OFFSITE_SETS` are still recognised and now **refuse the run** rather than being ignored | — |
| 0 | **target preflight** — refuse to run unless a real mount *carries* `BACKUP_TARGET` (and is not `ro`); logs the refusal, posts nothing (the `nightlySuccess` age grows) and exits non-zero if not. The target may be the mountpoint itself or a folder on it, so the check walks up; landing on `/` means no data drive is there. Zero disk I/O, so it never wakes a parked drive | `backup.sh` + `common.sh` `enclosing_mountpoint` |
| 6 | **report** — on the `backup ok` path (after verify, promotion, RUN.json and retention) `feed_naglight true` POSTs `nightlySuccess=<UTC now>` to `/api/backup-state` through `post_file_share_backup_state` (SR-121, 2026-10-23). **Every failure path posts nothing** — the ERR trap, `die`, refusals and the fib-ladder failure still exit non-zero — so the age of the last `nightlySuccess` grows and the wall turns the File Backups row yellow/orange/red by itself. A lost post is a WARNING and the run still exits 0 (deliberately unlike `library-backup.sh`: the archives are complete, and the age shows it). `/api/feed` is not used; `lastSuccess`, `shareHealth` and `runState` belong to other producers | `common.sh` `feed_naglight` |

**Rollout and rollback (nightlySuccess).** Deploy order: the tracker that understands `nightlySuccess` first, then this producer. There is no backfill; the File Backups row stays red until the first post, which it already is while FileBackup has no success. **Do not roll the tracker back to a version without the field once this producer is live:** the old writer drops the unknown key when it saves `state/file-share-backup.json`, silently erasing the nightly timestamp. Roll the producer back first if the tracker must go back. No new knob: the nightly run reads the same `FILE_SHARE_BACKUP_*` / `NAGLIGHT_*` settings from `/etc/homehub-backup/backup.env`; the sim gate `backup.env` (`vmtest/lib/common.sh`, installed by `firstboot.sh`) already carries them, so a sim hub posts it too.

## Layout — `dated` vs `flat` (2026-09-01)

`BACKUP_LAYOUT` decides what `BACKUP_TARGET` holds. It is `dated` unless set,
and the hub sets it to `fib`.

| | `dated` (original) | `flat` (2026-09-01 to 09-08) | `fib` (the hub, since 2026-09-08) |
|---|---|---|---|
| Target | `/mnt/backup-drive` — the archive drive's root | `/srv/library/Configs` — a **folder on the library drive** | `/mnt/backup-drive/config-history` — a **folder on the archive drive** |
| Per run | one `run_<UTC>/` directory | the archives sit in the target itself | one `daily/<YYYY-MM-DD>/` directory |
| Plan run | `plan_<UTC>/`, `BACKUP_PLAN_KEEP` of them | one `plan_latest/` slot, replaced | one `plan_latest/` slot, replaced |
| Retention | `BACKUP_KEEP` good runs, pruned after a green night | **none**; `BACKUP_KEEP`/`BACKUP_PLAN_KEEP` are not read, and the run logs that it is ignoring them | `BACKUP_FIB_DAILY_KEEP` dailies + one sample per rung under `fib/<age>/` |
| History | the dated siblings on the drive | the **library backup's** `Snapshot_<date>` series, which already versions `/srv/library` per file | its own, in `fib/` — nothing else versions it |
| Root clutter | one directory per night, forever, on the drive root | none | exactly two: `daily/` and `fib/` |

### The Fibonacci ladder (`fib`, 2026-09-08)

**Why `flat` was replaced.** It put the archives on the library drive so the
FileBackup snapshots would be their history. Measured on the box on 2026-09-08:
all 25 `Configs` files change every night — they are fresh `tar`+`zstd` of live
service state, so identical input still yields a different archive — so **all 33
of their rows were superseded into every `Snapshot_<date>`**. The config archives
were essentially the entire per-night snapshot, and the library backup was paying
a full re-store of the folder nightly to version 2.4 MB that versions better on
its own.

**The shape.** Seven dailies give a working overlap. Past that, one sample is
kept at each Fibonacci age — 13, 21, 34, 55, 89, 144, 233 days, i.e. F(7)..F(13).
Fibonacci's 8 is deliberately absent: it falls inside the 7-day window, which is
why the lowest rung draws from the dailies rather than from a rung below it. A
rung inside the window is **refused at config time**.

**It is a cascade, and it runs oldest first.** Rung F(n) is refilled from rung
F(n-1) — never from the dailies, except at the bottom — every F(n-2) days:

| rung | 233 | 144 | 89 | 55 | 34 | 21 | 13 |
|---|---|---|---|---|---|---|---|
| filled from | 144 | 89 | 55 | 34 | 21 | 13 | oldest daily |
| every | 89d | 55d | 34d | 21d | 13d | 8d | 5d |

**The order is the whole design**, not an implementation detail. The rungs are
walked from oldest to newest, so 233 takes 144's contents *before* 144 is itself
overwritten by 89, and so on down. Walk it the other way and a single night's
daily would propagate the entire length of the ladder in one pass, leaving seven
rungs holding seven copies of the same day and no history at all — the exact
failure the cascade exists to avoid.

**Why these ages are the ones that work.** Content entering rung F(n) is at most
F(n-1) days old, and then sits for at most F(n-2) days before the next refill.
F(n-1) + F(n-2) = F(n) — the Fibonacci identity — so each rung's label is its
ceiling, reached just before a refill. Measured across 300 simulated nights of
uninterrupted running, no rung ever exceeded its label. `backup.sh` checks this
same property against `BACKUP_FIB_SLOTS` at config time and refuses a ladder
whose rungs could not hold their labels.

**The ceiling assumes the service actually runs.** After an outage every sample
on the drive is already older than its label, and the first run back can only
advance each stale source one rung — the ladder re-converges over the following
cycles rather than snapping back. The guarantee is "never older than its label
while the nightly runs", not an unconditional one.

**The cadences are derived, not configured.** For a Fibonacci ladder the gap
between rungs *is* the previous Fibonacci number, so F(n) - F(n-1) = F(n-2) and
the whole schedule falls out of `BACKUP_FIB_SLOTS` itself. There is no second
list to keep in step — a list that could disagree with the first is a list that
eventually will.

**Two markers per rung.** `REFILLED` is the date the rung was last written, and
the cadence is measured from it; in a cascade the contents' age no longer tells
you when the rung was filled. `CAPTURED` is the date of the daily the contents
ultimately came from, carried up the ladder unchanged at every hop — it is the
provenance, and what says a rung really holds 200-day-old state rather than a
copy of last Tuesday.

**A rung whose source is empty is skipped and its clock is not advanced**, so it
refills the moment the rung below it first has something to give. That is how a
new box climbs the ladder from the bottom, one rung per night, instead of
standing empty for eight months — and how it closes the gap after an outage.

**Restoring.** `restore.sh` **requires** `--run`; it does not pick a run for you.
Restore the current copy by naming tonight's daily, or an old sample by naming a
rung:

```
restore.sh --run /mnt/backup-drive/config-history/daily/2026-09-08 --set caddy --target /tmp/r
restore.sh --run /mnt/backup-drive/config-history/fib/34          --set caddy --target /tmp/r
```

What `newest_run_with_set` resolves is restore's *hint* when the run you named
turns out not to hold the set. It searches `daily/` and never `fib/`: silently
offering a months-old rung as "the newest run that holds it" would hand back
stale state to somebody mid-recovery.

**Tested by** `tests/fib-ladder.test.sh`, which drives 300 simulated nights
through the `BACKUP_FIB_TODAY` seam and asserts the cascade directly: no rung
ever older than its label, none fresher than the daily window, ages never
decreasing going up the ladder, and every rung settling on its F(n-2) cadence.

**Why the move.** The service-state sets are small and change slowly, and the
thing that versions `/srv/library` does it far better than a full copy per
night: per-file dedup against a mirror, rather than seven complete copies of the
same seven archives. Dating them under the library would have been a second and
worse history *inside* the thing that already versions them — and worse than
merely redundant, because each night's new directory is a whole new set of paths
for the snapshotter to store again rather than one folder it can diff.

**One copy never means no copy.** A flat run does not write over what is on the
drive. It builds in `$BACKUP_TARGET/.incoming`, and only once the run is
verified and `RUN.json` says `ok` does `promote_flat` move those files up —
removing, at the same moment, any archive belonging to a set that has since left
`BACKUP_SOURCES`, so nothing can sit there looking current. A run that **fails**
never promotes: it is moved aside to `.last-failed` (one slot, as evidence) and
the copy already in the folder is untouched, which `flat-layout.test.sh` F7
asserts byte-for-byte.

**Two runs at once** cannot share the folder. The dated layout's guard is the
exclusive `mkdir` of a per-second directory name; a fixed name has no such
guard, so a flat run takes an advisory `flock` (on `/run/homehub-backup.lock`,
tmpfs, cleared by a reboot) and refuses to start if another run holds it.

**Nothing downstream had to learn about this.** `newest_run_with_set` answers
with the target itself when the target carries the `MANIFEST.tsv`, so
`restore.sh`, `provision/restore-volumes.sh`, firstboot's ACME and IceDrive
restores, and the reimage drill all resolve a flat target unchanged.

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
- **A wake timeout FAILS THE RUN, loudly** — the failure is logged, nothing is
  posted (the `nightlySuccess` age grows) and the exit is
  nonzero. Never-silent-green means a source that failed to *wake* must
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
in the client (authoritative list: HomeHub `scripts/deploy/storage-map.md` §4e), and
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

### The target must be on a real drive (step 0, added 2026-08-01)

`nofail` in the generated fstab is mandatory — a missing USB disk must not hold
up `local-fs.target` and drop a headless box to an emergency shell. Its cost is
that an **unplugged backup drive leaves `BACKUP_TARGET` as an ordinary empty
directory on the system disk**. Before step 0 existed, a run in that state
created its dated folder there, copied into it, verified it (the files really
were present), pruned old runs, and posted **`ok=true`** — a green lane writing
the household's backups to the wrong disk until that disk filled.

Since 2026-09-01 the target can also be a **folder on** a drive rather than the
drive's mountpoint, so the check asks the more general question: *which mount
carries this path?* (`enclosing_mountpoint`, still `/proc/self/mountinfo` only,
still no disk I/O). For `/mnt/backup-drive` the answer is the target itself, as
before; for `/srv/library/Configs` it is `/srv/library`. Walking up to `/` is
what now means "no data drive is here", and it is refused exactly as an
unmounted drive always was — which matters more in the folder case, not less,
because a missing drive leaves a *plausible-looking* path behind either way.

Step 0 refuses instead, logs the refusal, posts nothing (the `nightlySuccess` age grows) and exits non-zero. Deliberately NOT done as
`RequiresMountsFor=` on the unit: systemd would refuse to *start* the service, so
**nothing** would reach NagLight — an unreported non-run, which is worse than a
red one. Escape hatch for a target that is legitimately a plain directory:
`BACKUP_TARGET_REQUIRE_MOUNT=false` (the run then warns loudly every time).

**SN-014/SR-018:** the panel has one `file-share-backup-health` item. Its monitor
checks `/srv/library` plus a representative Samba read and sends only
`shareHealth: red|clear`; FileBackup alone sends `lastSuccess` after artifact
verification. The bash config backup and backup-target identity/preflight checks
remain operational safeguards, never presentation lanes.

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
  still restarts + posts nothing (nonzero exit), cifs-only config makes zero docker calls.

## Recovery MANIFEST / state format

The legacy `*FilesHashTable.csv` files (FileBackup's own hash-tracking state,
INVENTORY.md) are the prior art. Per run — under `BACKUP_TARGET` itself in the
flat layout, under `BACKUP_TARGET/run_<UTC>/` in the dated one:

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
no post, cifs-only zero-docker no-op) with a mock-`docker` shim.
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
library-backup.sh                        the host side (bash, root, one-shot); single layout
library-backup-legacy.sh                 the legacy layout's wrapper, frozen (FILEBACKUP_LAYOUT)
filebackup.json                          the legacy container config — TRACKED, installed
                                         to /etc/homehub-backup/filebackup.json 0644
filebackup-store.json                    the single-store container config, installed beside it
systemd/homehub-library-backup.service   Type=oneshot, TimeoutStartSec=infinity
systemd/homehub-library-backup.timer     03:00 box-local (same transaction as the nightly, After= orders it), Persistent=true
```

The image is `filebackup:${FILEBACKUP_IMAGE_TAG}`, built from a sibling
`../FileBackup` checkout by `scripts/ensure-local-images.sh` (**always with
`--rebuild`** — the resolver skips an image that already exists, and a
hand-tagged one carries no `homehub.source.revision` stamp) and baked into the
ISO payload by `vmtest/export-images.sh`, which enables the `filebackup` profile
unconditionally for the bake.

> **Everything from here on is the `single` layout** of
> [the layout knob](#the-layout-knob--filebackup_layout-legacy-until-the-cutover);
> the default, `legacy`, runs the deployed image unchanged.
>
> **This layout needs a FileBackup image built from FileBackup's bounded-run-loop
> work** (branch `BoundedRebuild`, 2026-10-24 — **not in a released image yet**).
> The sibling checkout must be on that work, or on a later commit that contains
> it, when the image is built. An older image does not fit: it expects a `/state`
> mount and separate `/backup` and `/changes` mounts, and its exit codes mean
> different things. Nothing here checks the image before it runs (it carries no
> contract label). Not verified, but expect an old image to fail for want of a
> writable place for its source-side record, or to return codes this wrapper
> reads differently; an exit 3, which the bounded engine never returns from a
> backup, is reported as "an OLDER image".
>
> The exit meanings below also assume the fixes and rulings of FileBackup's
> 2026-10-26 data-loss review (its plan calls C36–C43 and the commits its
> `docs/reviews/2026-10-26-data-loss/consolidated.md` lists), which are on
> `BoundedRebuild` or being merged into it. An image built before them reads
> mostly the same, but warns and returns 0 on an empty source instead of 4, and
> never sets damaged objects aside.
>
> **And the store layout below needs FileBackup's single-store layout** (its
> plan call C44, rulings L1–L6, `docs/plans/single-store-layout.md`), which is
> **not built yet**. An image without it refuses this stack's `filebackup.json`
> (ConfigVersion 3), and an image with it refuses the previous one. The commits
> that brought this layout to HomeHub are marked as needing it.

### The layout knob — `FILEBACKUP_LAYOUT` (legacy until the cutover)

Two store layouts are installed side by side, and `FILEBACKUP_LAYOUT` in
`stack/.env` (SR-133; HomeHub's `config.homehub.psd1` pins it) chooses:

| | `legacy` — **the default**, and what an unset value means | `single` |
|---|---|---|
| image | the one the hub runs today (kit 12, `filebackup:local` from FileBackup `4b809d1` — inferred from records, confirm with `docker image inspect`) | one built from `BoundedRebuild` **with C44** (not built yet) |
| store | three mounts: `/state` (system disk), `/backup`, `/changes` (`FILEBACKUP_STATE`/`_BACKUP`/`_CHANGES`) | one mount, `/store` (`FILEBACKUP_STORE`), below |
| compose service | `filebackup` | `filebackup-store` |
| container config | `filebackup.json` (ConfigVersion 2) | `filebackup-store.json` (ConfigVersion 3) |
| wrapper | `library-backup-legacy.sh`, which `library-backup.sh` hands the run to with its arguments unchanged | `library-backup.sh` itself (everything below this section) |
| frame-freshness reads | the `/state` record, with its `.meta` row-count check | the system-disk copy the wrapper publishes |

**A stack delivery changes nothing until the knob is switched.** The legacy
wrapper is the integration branch's wrapper as it was before this layout
merged, byte for byte (pinned by `tests/library-backup-layout.test.sh`), and that
branch's own suite runs against it through the default
(`tests/library-backup-legacy.test.sh`). The legacy service and config are
unchanged. `frame-freshness.sh` reads the **newer** of the two system-disk
manifests, so on a legacy hub, where there is no copy, it reads `/state`
exactly as before. Neither service is in `COMPOSE_PROFILES`, and a plain
`docker compose config` (firstboot's bind-source pre-creation) sees neither, so
nothing creates `library-store/` on the backup drive. What a delivery cannot do
is make an old `/etc/homehub-backup/filebackup-store.json` appear: that file
comes from late-command 5c on a reimage, or from the cutover below.

The knob does **not** protect against the image changing under it. A
`filebackup:local` built from `BoundedRebuild` (with or without C44) and run in
the legacy layout fails loudly (the three-mount config and mounts are refused);
that is the build hazard of HomeHub open-items C110, and the reason the old
image is kept under its own tag at step 2 below.

#### The cutover, in this order

1. **FileBackup first.** Its C44 work package (the single-store layout) built,
   independently reviewed and merged on `BoundedRebuild` (its ruling L6); and
   the second half of its call C43 (prune and pool-resolve reading one
   manifest at a time) in the same image, because the verify gate runs by
   itself once the timer is on (see
   [Verify and prune memory](#verify-and-prune-memory-filebackup-c43-not-yet-safe-on-the-hub)).
2. **Build the image** from that commit (`scripts/ensure-local-images.sh
   --rebuild`, the sibling checkout on it). On the hub, before loading it, keep
   the deployed image for the rollback: read its stamp
   (`docker image inspect filebackup:local`) and
   `docker tag filebackup:local filebackup:kit12`. Then load the new one as
   `filebackup:local`. From here the legacy layout no longer runs, so nothing
   may run meanwhile: confirm the timer is off rather than assume it
   (`systemctl is-enabled homehub-library-backup.timer` says `disabled`, and
   `systemctl list-timers --all` does not list it as armed; open-items E13),
   and `systemctl disable --now` it if not.
3. **Deliver this stack and install the config:**
   `install -m0644 -o root -g root /opt/homehub/stack/backup/filebackup-store.json /etc/homehub-backup/filebackup-store.json`.
4. **Set the old store aside** — there is no migration (FileBackup L2, L5):
   [Setting the old store aside](#setting-the-old-store-aside). A store already
   made by hand to be the new baseline must be in the single-store layout at
   `FILEBACKUP_STORE` (`Latest/` and `Snapshot_*` side by side; one in another
   layout is refused by the wrapper's step 2b like any old store), and then the
   set-aside renames must **not** move `library-store`: skip that line.
5. **Switch the knob, with the cap:** `FILEBACKUP_LAYOUT='single'` **and**
   `FILEBACKUP_MEM_LIMIT='4g'` in HomeHub's `config.homehub.psd1`, in the same
   change, so the `.env` that turns the single layout on never runs uncapped
   (compose sets the equal `memswap_limit` itself); materialise, ship the
   `.env`. Then
   `sudo /opt/homehub/stack/backup/library-backup.sh --preflight-only` must
   pass (it creates nothing and starts no container).
6. **The first run, by hand, under that cap** (E13's return conditions; never
   uncapped): check `docker compose --profile filebackup config filebackup-store`
   shows `mem_limit` and `memswap_limit` at 4g, then start it so an SSH drop
   cannot kill it —
   `sudo systemd-run --unit=library-backup-first /opt/homehub/stack/backup/library-backup.sh --verify none`
   — and poll the run container's `memory.peak` every 10 s while it runs (it
   is `--rm`; the slice's peak, which the wrapper logs, is an upper bound).
   Expect days: it is a full first backup.
7. **Size the limit and turn the timer on:** `FILEBACKUP_MEM_LIMIT` at about
   twice that peak, then `systemctl enable --now homehub-library-backup.timer`.

#### Rolling back the cutover

- **Before step 4** (the old store still in its places): set
  `FILEBACKUP_LAYOUT=legacy` again and `docker tag filebackup:kit12
  filebackup:local`. Nothing else changed.
- **Either way**, remove the single layout's manifest copy
  (`/var/lib/homehub-filebackup/latest/MANIFEST.csv`) if a single-store run
  made one: frame-freshness reads the newer of it and the legacy `/state`
  record, and would read the abandoned copy until the next legacy run.
- **After step 4:** the same, plus the set-aside renames reversed (the old
  folders back to `library` and `library-changes`), with the new store moved
  aside in its turn. Not verified; treat it as the Owner's decision.

#### Retiring the legacy layout

Only once the single-store store has a verified history: delete
`library-backup-legacy.sh`, `tests/library-backup-legacy.test.sh`,
`filebackup.json` and the `filebackup` service (giving `filebackup-store` the
name back), drop the hand-off, and retire `FILEBACKUP_STATE`/`_BACKUP`/`_CHANGES`
from HomeHub's config. Until then they are the rollback.

### The single-store layout

The container gets **one writable mount, `/store`**, and it is the library
set's **whole store** (FileBackup's plan call C44):

```
<FILEBACKUP_STORE>/        bound at /store; default /mnt/backup-drive/library-store
├── Latest/                the latest state: MANIFEST.csv, every object in use, the restore kit
├── Snapshot_<date>/       one per superseded state
├── Snapshot_<date>/
├── View/                  the browse view's default place (FileBackup L3; not turned on here)
└── Temp.lock, backup.log, Incoming/, Building_*/, Verify/    run files
```

`filebackup.json` names only `StorePath=/store` (`ConfigVersion: 3`); the old
`BackupPath` and `ChangePath` are gone (L1). From `Latest` or any `Snapshot_*`
the store is the parent folder, so a restore needs no recorded path
(`RECONSTRUCT.paths.json` is gone) and no guess between sibling folders. Why one
mount: the engine moves content between the latest state and the snapshots **by
rename only**, so that an object is in exactly one place before and after every
move, and it refuses (exit 2) a store a rename cannot cross (L4). Linux cannot
rename between two bind mounts **even when both are on one filesystem**.

**The wrapper never creates `Latest/`.** FileBackup creates it on a new store,
and refuses (exit 2) a store that holds snapshots but no `Latest/` — one that
lost its latest state must not be answered with a fresh full backup beside its
history — which it can only see if `Latest/` is really missing.

There is **no `/state` mount.** The latest `MANIFEST.csv` is FileBackup's only
record; `SourceStatePath` is not in `filebackup.json` (FileBackup still accepts
the key but ignores it, with a warning every run). The old record at
`/var/lib/homehub-filebackup/state/MANIFEST.csv` is dead; the wrapper says so
after each verified run, and you delete it by hand (nothing here deletes it).

**`stack/tracker/frame-freshness.sh` used to read that old record** (it must
never wake a drive, so it reads a system-disk CSV). The wrapper publishes a copy
of `Latest/MANIFEST.csv` to `/var/lib/homehub-filebackup/latest/MANIFEST.csv`
(`LIBRARY_MANIFEST_COPY`) after every **completed** run, exit 0 or 1, by rename
so a reader never sees half a file, and frame-freshness reads that. Until the
first completed run the copy does not exist and that lane says yellow ("cannot
read …"); a copy that cannot be made is a `DEGRADED` note, never a failed backup.

A hub with a store in an older layout **sets it aside first** — see
[Setting the old store aside](#setting-the-old-store-aside). Until then the
wrapper refuses (step 2b below).

### Setting the old store aside

There is **no migration** into the single-store layout (FileBackup L2, L5): the
old store is **set aside**, keeps restoring with the kits already inside it, and
a **clean** backup starts. (FileBackup's `-Action Verify -RefreshKits` refuses an
old-layout store rather than give it a kit that cannot read it, so its kits stay
as they are.)

The wrapper refuses (step 2b) while an old-layout store sits where this stack
knows to look: the three-mount folders `FILEBACKUP_BACKUP` and
`FILEBACKUP_CHANGES` (default `/mnt/backup-drive/library` and
`/mnt/backup-drive/library-changes`, what the hub ran before the bounded run
loop); `backup/` or `changes/` inside the store folder (the bounded-run-loop
layout); or a `MANIFEST.csv` at the top of the store folder. Setting aside is
renaming them into a folder of their own **on the same drive** — instant,
nothing copied, nothing deleted:

```bash
sudo systemctl stop homehub-library-backup.timer     # no run may start meanwhile
cd /mnt/backup-drive && findmnt -T .                  # the backup drive itself, mounted
sudo mkdir old-library-store
stat -c '%d %n' . old-library-store library library-changes library-store 2>/dev/null
#   every line must show the SAME device number: then each mv below is a rename.
#   A different number means another filesystem, where mv would copy terabytes - stop.
# One mv per folder that EXISTS; skip a line whose source is absent.
sudo mv -T library         old-library-store/library
sudo mv -T library-changes old-library-store/library-changes
sudo mv -T library-store   old-library-store/library-store
# Then go on with the cutover (the layout knob, steps 5-7): the knob, the
# first run by hand under the 4 GB cap, and only then the timer.
```

- **Space.** The first clean backup is a full one, beside the set-aside store,
  and takes days. The capacity floor (step 3) is checked before the run only; a
  drive that fills during it stops it with **4** (nothing stored is lost), and it
  stays stopped every night until space is made. Where
  the set-aside store ends up — kept on this drive, moved to another, or
  deleted once the new store has history — is the Owner's call
  (docs/status.md), never this stack's.
- **Restoring from the set-aside store** uses the kit inside it
  (`bash/reconstruct.sh` in its latest state or a snapshot). Its folders have
  moved, so pass its roots explicitly as that kit's `--help` describes.
- **`stack/.env`:** keep `FILEBACKUP_STATE`, `FILEBACKUP_BACKUP` and
  `FILEBACKUP_CHANGES` until the legacy layout is retired: they are the legacy
  layout's paths (the rollback). In the single layout `FILEBACKUP_BACKUP` and
  `FILEBACKUP_CHANGES` are read only to find an old store (step 2b refuses
  while either holds data), which after the renames they no longer name;
  `FILEBACKUP_STATE` is never a refusal, only the retired-record note below.
- **The old source-side record**, `/var/lib/homehub-filebackup/state/MANIFEST.csv`,
  belongs to the old store; the wrapper names it after each verified run until
  you delete it.

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
| 1. `flock` | another run holds the lock | the first real run is a **multi-day** job and the nightly timer fires again while it is going. FileBackup's own lock would stop a second container with exit 4, but only after compose and a drive spin-up; this answers first and names the holder. A held lock **posts nothing** — the lane belongs to the run that is still going — but exits non-zero |
| 2. mount identity | `/srv/library` or the backup drive is absent or read-only, or `Latest/` is a separate mount under the store | `nofail` in the fstab means an absent drive leaves an ordinary empty **directory on the system disk**. Reuses `samba/library-guard.sh`, cross-checked against the generated fstab and `drive-identity.conf`; a **stand-in drive is yellow**, carried into the summary, not a refusal. The library-source check answers **first**; behind it, FileBackup itself stops with exit 4 when the source is empty at the start of a run while the latest state holds rows (its call C39), leaving the latest state as it was |
| 2b. store layout | a store in an older layout has not been set aside: the three-mount folders (`FILEBACKUP_BACKUP` / `FILEBACKUP_CHANGES`, default `/mnt/backup-drive/library` and `library-changes`) or `backup/` / `changes/` inside the store still hold anything, or a `MANIFEST.csv` sits at the store's top | there is no migration (FileBackup L2), and starting a clean backup while the old history sits where this stack knows it would mix the two or double the space unannounced. The refusal names [setting it aside](#setting-the-old-store-aside); an **empty** old folder is only a note |
| 3. capacity | free space below `BACKUP_LIBRARY_MIN_FREE_GB` (or the system-disk floor for `/logs`) | there is **no retention** — this floor is the whole space policy |
| 4. pre-create | — | docker **chowns** a bind source it had to create, and that is `EPERM` on the FAT-family data drives, which takes the *entire* compose command down. The config file and the library source are asserted, never created; **`Latest/` is never created** (FileBackup must see it missing) |
| 5. write-probe | uid 65532 cannot write the store, `Latest/` (when it exists) or `/logs` | NTFS/exFAT ownership is synthesized from mount options, so only the fstab's `uid=65532,gid=65532` grants access (**Q-FB5**). A regression must fail in the first seconds, not at hour eight |
| 5b. old staging | *(never refuses)* | an interrupted prune's `Temp` at the top of the store may hold the only copy of superseded content. The bounded engine leaves it alone and runs, so this is a **note** naming the recovery event that interrupted it, carried into the summary; recover `Temp` by hand per FileBackup's README |

Then it holds the drive awake (`hdparm -S 0`, restored on every exit path),
runs the container, applies the verify gate, and posts to NagLight.

`library-backup.sh --preflight-only` runs steps 1–5 and stops without creating
or starting anything.

### Exit codes — three different tables

The **backup** action (FileBackup's bounded-run-loop plan §6). The set log,
`<store>/backup.log`, names every file; the container's global log is
`/var/log/homehub-filebackup/Backup_Global.log`.

| Exit | Meaning | What the wrapper does |
|---|---|---|
| **0** | every file was captured | verify gate; then **lastSuccess** |
| **1** | the run **completed**, but not fully: some files were not captured (each is named in the set log; their earlier rows are kept), and/or a stored object was found **damaged** and set aside (below). Usually a file in use; an unportable name — including paths that differ only in letter case, which a Windows restore would collapse into one (FileBackup C42) — or an unreadable folder repeats every night until fixed | verify gate still runs (a file locked for weeks must not also stop the weekly proof); then **fails the unit, no lastSuccess**. The message counts any `[DamagedObject]` lines |
| **2** | refused **before changing anything**; a person must fix it — the config (a ConfigVersion 2 one, among others), a store that holds snapshots but no `Latest/`, a required tool or dependency that cannot be loaded (7-Zip, the hashing package; before the 2026-10-26 review this escaped as a 1), the source root missing, the one-mount rename test, or a latest manifest that cannot be read (move it aside to force a rebuild; the snapshots are untouched) | fails now: **do not retry** |
| **4** | stopped by a **host** problem — the backup drive full or gone; another FileBackup run holds its lock (`Temp.lock` at the store's top; a manual verify or prune?); or the library source was **empty at the start of the run while the latest state holds rows** (FileBackup C39), which is exactly what an unmounted or failed library drive looks like. The latest state is left as it was | fails now: **retry later**. A stop is no worse than a kill and the next run keeps the progress; the next timer run is the retry. A library that is really meant to be empty is not a supported state for this set (FileBackup's advice is to remove such a set from the configuration) |

**Only 0 is a verified success.** An exit 1 is "partial, no data lost", but
advancing `lastSuccess` for it would let a file that is never captured hide
behind a green lane forever. So the lane's age grows and the unit fails
(`systemctl --failed`, and the hub watch's `library-backup` row) — that *is* the
warning, and it turns red only if nobody acts for days. A backup **never returns
3** any more (the manifest witness is gone); the wrapper reports a 3 as an
**older image**.

#### Damaged objects

Since FileBackup's call C37 a backup run lists the store and treats a stored
object as **damaged** when it is empty while its name records content, or when
it is a raw object (no 7-Zip signature) whose length differs from the one its
name records — what a power cut can leave behind a rename. It **moves** each one
into `<store>/Verify/` (never deletes it, never reuses it; one it cannot
move stays where it is and is still never reused), writes one
`[DamagedObject]` WARN line per object into the set log, stores the content
again only when a source file still hashes to that name, and returns **1**. The
wrapper counts those lines after an exit 1 and says how many; it reads nothing
else out of the log.

Bytes already stored were lost here. If a library file still hashes to that
name, the same run stores it again and every row naming it resolves as before.
If none does (the file has since changed or gone), the rows naming that object —
in the latest state or in a snapshot — can no longer restore that file, and a
verify reports them as `PoolUnresolvable`. `Verify/` is not part of the pool;
look at what it holds before emptying it by hand.

The **verify** action: **0** clean · **1** findings · **2** precondition (no
`Latest/`, no manifest, `-Deep` without 7-Zip, a host problem that stopped
it, or — for a `-RepairStorage` run — the store lock held by a backup or
prune). It never returns 3 or 4 any more; the wrapper reports either as an
older image. See [Verify findings](#verify-findings).

**`prune` and `snapshots`** keep FileBackup's restore-side table — **0** done ·
**1** batch incomplete, nothing lost · **2** usage or precondition, nothing
mutated · **3** an old manifest witness disagrees with its manifest, nothing
mutated · **4** host I/O, retriable; precedence **2 > 3 > 4 > 1**. The wrapper
never runs either; they are the manual relief valve below. Since FileBackup
`3756974` both resolve the store **read-only** and no longer need the library:
with the library drive away they still work (they used to do nothing and exit
1). A store that is not there — no `Latest/` — is refused with **2** and
nothing is created inside it. *Open with the Owner (FileBackup review item B):
whether an unplugged store drive should be 2, as built, or 4 "retry later".*
Note that compose still binds the host folder `FILEBACKUP_STORE`: if the backup
drive is not mounted, docker creates that folder **on the system disk** for the
bind and FileBackup then refuses with 2 — check `findmnt /mnt/backup-drive`
first, and remove such a stray folder by hand.

NagLight sees all of this as **one** thing: whether `lastSuccess` advanced.
IF-006 has no warn/error channel for the library wrapper (`shareHealth` belongs
to the share monitor), so "warn" and "error" differ in the journal and in the
wrapper's message, not on the wall.

### Verify findings

The weekly gate is **report only**; it never passes `FILEBACKUP_REPAIR`. Each
finding is a `[Class]` line in `Backup_Global.log` (and a JSON document on the
container's stdout, in the journal):

| Class | Means | What to do |
|---|---|---|
| `PoolUnresolvable` | a manifest row whose bytes are nowhere in the store | the next backup re-stores it if the source still has the file |
| `DuplicateObject` | one object stored in more than one folder | harmless, costs space. Rare on a clean store (residue of an interrupted move) |
| `DamagedObject` | an object whose **length** its name rules out — empty, or a raw object of another length (any verify, since FileBackup C37; a backup run sets these aside itself, see [Damaged objects](#damaged-objects)) — or, under `-Deep` (monthly), one whose bytes do not hash to its name | the next backup re-stores the content if the source still has it |

Moving duplicates and damaged objects aside is a person's call, never the
timer's. It **moves** them into `<store>/Verify/` — nothing is deleted —
and re-verifies. Preview it first: `-WhatIf` passes straight through to
FileBackup and, since FileBackup `ce068bc`, a repair honours it and moves
nothing:

```bash
cd /opt/homehub/stack
docker compose --profile filebackup run --rm -T -e FILEBACKUP_REPAIR=1 filebackup-store verify -WhatIf  # preview: moves nothing
docker compose --profile filebackup run --rm -T -e FILEBACKUP_REPAIR=1 filebackup-store verify          # duplicates, and objects damaged by length
docker compose --profile filebackup run --rm -T -e FILEBACKUP_REPAIR=1 filebackup-store verify -Deep    # and objects whose bytes do not hash to their name
```

`-e` is required: a variable set on the host shell is **not** passed into the
container (compose forwards only what the service declares). A repair takes the
store's lock (`Temp.lock`), so it is **refused with 2 while a backup or
a prune holds it** — run it outside the 03:00 window, or stop
`homehub-library-backup.timer` for the duration. Empty `Verify/` by hand
once you are sure you do not want what is in it.

#### Verify and prune memory (FileBackup C43): not yet safe on the hub

Until FileBackup's follow-up to its call C43 lands, **a verify (its
pool-resolve check) and a prune still hold every snapshot manifest in memory at
once** — about 420 MB per HomeHub manifest, the mechanism of the 2026-10-04
outage. FileBackup's storage-form audit no longer does; prune and the
pool-resolve check are next.

**Neither should run on the hub's store before that follow-up is in the
deployed image.** The wrapper's verify gate (step 8, weekly, `-Deep` monthly) is
scheduled by the calendar and **nothing here stops it yet** — disabling it is
the Owner's call (docs/status.md). What would gate it: run the unit with
`--verify none` (its `ExecStart`), or a knob the wrapper reads, until such an
image is deployed; a `FILEBACKUP_MEM_LIMIT` would turn a runaway into an OOM
kill of this one container instead of the box. Prune is manual only, so the
same rule is a rule for the person running it. The repair commands above are
verifies too.

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
docker compose --profile filebackup run --rm -T filebackup-store snapshots
# 2. DRY RUN the removal first. `-e` is what makes it a dry run: a variable set
#    on the host shell (FILEBACKUP_DRY_RUN=1 docker compose …) is NOT passed into
#    the container, so that form silently runs the REAL prune.
docker compose --profile filebackup run --rm -T -e FILEBACKUP_DRY_RUN=1 filebackup-store \
    prune -Snapshot Snapshot_2026-08-23
# 3. then for real.
docker compose --profile filebackup run --rm -T filebackup-store prune -Snapshot Snapshot_2026-08-23
```

**Never delete a `Snapshot_*` folder by hand.** It is the only copy of the states
it holds, and the manifests that make the rest reconstructable refer into it.
These actions use the **restore-side** code table above, not the backup one.
Read [Verify and prune memory](#verify-and-prune-memory-filebackup-c43-not-yet-safe-on-the-hub)
before running a prune on the hub.

**A prune that reports 0 can leave a `Pruning_<name>` folder** (FileBackup
C41). The rename to `Pruning_<name>` is the prune's commit point: once it has
happened the snapshot is gone from every reader and its still-needed bytes are
already re-homed. If the delete after it fails, the prune still reports the
removal as done (**0**) with a WARN naming the leftover, and **every later
prune** (of any name) finishes such leftovers **first**: right after it takes
the store lock, before any of its checks, so even a prune that is then refused
(for space, say) clears them and its space counts. A dry run (`-WhatIf`)
finishes nothing. Leave `Pruning_*` folders to FileBackup rather than deleting
them by hand.

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
3. **Reclaim the space by hand.** `rm -rf /mnt/backup-drive/library-store` (the
   `FILEBACKUP_STORE`). The store is self-contained, so deleting it needs no
   tooling and leaves nothing dangling. The retired source-side record at
   `/var/lib/homehub-filebackup/state` and the logs at
   `/var/log/homehub-filebackup` go the same way.

   > **This deletes the only copy of every state it holds.** Since WP9 the backup
   > root is content-addressed, not a browsable mirror (Q-FB1 withdrawn
   > 2026-08-26), so you cannot eyeball what you are about to lose — the file
   > names are opaque hashes. Take an inventory first:
   > `docker compose --profile filebackup run --rm -T filebackup-store snapshots`
   > (`filebackup` in the legacy layout).
4. **The bash service is unaffected.** Its sets, its timer, its `backup` lane and
   its retention are all independent of any of the above. What you lose is the
   library coverage, which is what there was before this shipped.

**Rolling the image back past the single-store layout** is a different thing
and is **not verified**: an image without C44 cannot read this store or
`filebackup-store.json` at all. The way back is the knob's
[rollback](#rolling-back-the-cutover): `FILEBACKUP_LAYOUT=legacy` and the kept kit-12 image. Going back means the older image *and* the older stack
files *and* the set-aside old store renamed back into its places — the
[set-aside renames](#setting-the-old-store-aside), reversed — with the new
store left aside in turn. Treat that as a decision for the Owner, not a routine
rollback.

### The 7-Zip level — `FILEBACKUP_7Z_LEVEL=5`, not the image's 9 (2026-09-01)

FileBackup kit revision 12 (2026-09-01) made the 7-Zip effort level an
environment knob: a single digit `0`–`9`, 7-Zip's own `-mx` range. **The image
ships no default of its own and falls back to 9 (“Ultra”)** — the level every
run before revision 12 used, and the one that re-packs a 4 TB library at maximum
effort for a few percent. **The Owner ruled 5 (“Normal”).** It is set in three
places so no layer can quietly return the library to Ultra: `stack/.env`
(`FILEBACKUP_7Z_LEVEL=5`, pinned by HomeHub's `config.homehub.psd1`), inline in
`docker-compose.yml` (`${FILEBACKUP_7Z_LEVEL:-5}`, for a materialised `.env` that
predates the knob), and the wrapper logs the effective value before every run.
The level never changes *whether* a file is compressed — that is FileBackup's
probe — and every level's archive restores with the same kit, so changing it
later needs no migration.

### The config file

Two configs, one per layout, both **tracked** and installed by autoinstall
late-command 5c at 0644 root — not through the `site/` payload the secrets take:
`filebackup.json` (legacy: ConfigVersion 2, `/source`, `/state`, `/backup`,
`/changes`, unchanged from before the knob) to
`/etc/homehub-backup/filebackup.json`, and `filebackup-store.json` (single) to
`/etc/homehub-backup/filebackup-store.json`. The notes below are the single
layout's. Every path in them (`/source`, `/store`) is a **container-internal mount
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

  **What this changes for recovery:** `Latest/` is a flat directory of
  opaque hash-named objects. `ls` tells you nothing and neither does a file
  manager; the manifest is the index and `reconstruct.sh` is the reader. The
  restore kit still ships **inside** the backup, so a rescue machine still needs
  no tooling installed — see "Restoring without the container" below.
- `StorePath: "/store"` with `ConfigVersion: 3` — the whole store is the one
  mount (see [The single-store layout](#the-single-store-layout)). `BackupPath`
  and `ChangePath` are **gone; do not add them back**: an image with the
  single-store layout refuses a config that names them (L1).
- `SourceStatePath` — **REMOVED. Do not add it back.** The bounded engine keeps
  no source-side record; it still accepts the key but ignores it, with a
  warning on every run.
- `AllowEmptySource` is absent, and FileBackup accepts and **ignores** it
  anyway. Since its call C39 (superseding C2's warning) a source that is empty
  at the start of a run while the latest state holds rows stops the set with
  **4** before the latest state is written; a first backup of an empty source
  still returns 0. The wrapper's step 2 (`/srv/library` must be its own
  mountpoint) answers first, before a container starts.
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
S=/mnt/backup-drive/library-store
bash "$S/Latest/bash/reconstruct.sh" --target-root /var/tmp/restore --from "$S/Snapshot_<date>"   # or --from "$S/Latest"
```

**No root arguments.** In the single-store layout the kit takes the store as
the parent of the `--from` folder and the pool as `Latest` plus every
`Snapshot_*` beside it; there is no `RECONSTRUCT.paths.json` and no guess
between sibling folders. FileBackup keeps one store override for a store read
from somewhere unusual; its name is in FileBackup's README once C44 is built.
The kit-14 restorer reads no witness and **refuses `--require-witness`** as an
unknown argument. A set-aside old store restores with its own, older kit (see
[Setting the old store aside](#setting-the-old-store-aside)).

**Its exit codes** are FileBackup's restore table (its README "Restore exit
codes"): **0** every row restored · **1** content missing — real data loss for
those rows; everything salvageable was restored · **2** nothing attempted (usage,
no or unrecognisable manifest, target problems) · **4** a host problem on this
machine — retriable. Precedence **2 > 4 > 1**. Two meanings are newer than the
first kit-14 build, and the kit revision is still **14**:

- A manifest **torn into NUL bytes** (what NTFS can leave after a power cut)
  restores its complete rows before the NULs, logs `[ManifestDamaged]` naming
  the manifest, and exits **at least 1** — it used to exit 0 with a partial
  tree (FileBackup C38). The rows after the tear are not recoverable from that
  state; an older snapshot may still hold them.
- A **host** problem on a row that carries a `DataPath` (an unreadable pool
  folder, say) is **4**, retriable, not 1 "content missing" (FileBackup review
  A7, `4fb7689`). Since nearly every row carries one, a 1 now really means the
  bytes are gone.
