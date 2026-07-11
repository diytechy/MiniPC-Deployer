# AWOW bash backup service (WI-10.10)

The homelab's main backup, running on the AWOW box as **pure bash + systemd** —
zero `.bat`/`.ps1` anywhere in the pipeline (Peter's rule, HOMELAB_TOPOLOGY.md).
The rewritten **FileBackup** repo is the behavioral *spec* this reproduces (hash
tracking, auto-compression-where-applicable, recovery/reconstruct), not code to
port. It was built and validated end-to-end against the `sim/mini-serv-sim`
Samba fixtures in WI-10.15 (see `docs/status.md`).

## The six steps (HOMELAB_TOPOLOGY.md)

| # | Step | Where |
|---|---|---|
| 1 | **source pulls** — ONE `BACKUP_SOURCES` table, three source kinds (SR-013): `//host/share` (cifs-mount + `rsync`), `volume:VOL[@CONTAINER]` (rsync from the docker volume's mountpoint, optional stop→copy→restart quiesce), `path:/dir` (local rsync) | `backup.sh` + `source_kind` |
| 2 | **archive + compress** — `tar` per set, `zstd` **where applicable** (already-compressed sets stored as plain `.tar`) | `backup.sh` + `compression_decision` |
| 3 | **hash + verify + manifest** — per-file sha256 table + archive sha256 + integrity test; a recovery MANIFEST | `backup.sh` |
| 4 | **external-drive target** — dated `run_<UTC>` snapshot with retention (`BACKUP_KEEP`) | `backup.sh` |
| 5 | **offsite push** — cifs-mount the IceDrive-synced share, push selected sets | `backup.sh` |
| 6 | **report** — POST NagLight `/api/feed`; **never-silent-green** (failure → `ok=false` + nonzero exit) | `common.sh` `feed_naglight` |

## Files

```
backup.sh            orchestrator (the six steps; --config, --dry-run)
restore.sh           reconstruct + byte-verify a set from a run (the recovery half)
common.sh            shared helpers (logging, cifs mount, compression policy, feed, drive power)
backup-standby.sh    boot-time DEFAULT STANDBY oneshot (WI-10.10 drive power)
backup.env.example   every knob
systemd/awow-backup.{service,timer}   nightly root oneshot + persistent timer
systemd/backup-standby.service        per-boot backup-drive spin-down default
```

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
  `set · source · archive · algo · archive_sha256 · files · bytes · reason`
- **`<set>.files.tsv`** — the per-file hash table (the `*FilesHashTable.csv`
  successor): `sha256 · size · mtime_epoch · relpath` for every file in the set.
- **`<set>.tar` / `<set>.tar.zst`** — the archive (algo per step 2).
- **`RUN.json`** — run summary (status, totals, offsite, per-set sizes).
- **`backup.log`** — the run log.

`restore.sh --run <run_dir> --set <name> --target <dir>` verifies the archive's
`archive_sha256`, extracts it, then checks **every** restored file against
`<set>.files.tsv` (sha256 + size). It **fails loudly**: restores everything
recoverable, then exits nonzero naming the count it could not verify; a clean
restore exits 0.

## Hash-algorithm delta vs FileBackup

FileBackup uses **xxHash128** (speed). This service uses **sha256** — coreutils-
native, so the AWOW needs no extra hashing dependency. The hash is an internal
integrity/dedup choice for a self-contained backup+restore leg; both give
byte-exact verification. (The FileBackup *restore* format — `bash/reconstruct.sh`
+ `MANIFEST.csv` — is a different, content-addressed layout; this service is the
tar-archive pipeline the topology's six steps describe, not that layout.)

## Run on the AWOW

```bash
sudo cp backup.env.example /etc/awow-backup/backup.env   # then edit
sudo install -m600 /dev/stdin /etc/awow-backup/cifs.creds <<< $'username=awow\npassword=…'
sudo cp systemd/awow-backup.{service,timer} /etc/systemd/system/
sudo systemctl enable --now awow-backup.timer
sudo systemctl start awow-backup.service     # run once now
journalctl -u awow-backup.service -f
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
See `sim/README.md`.
