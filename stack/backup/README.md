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
| 1 | **source pulls** — cifs-mount each LAN Samba share, `rsync` into staging | `backup.sh` |
| 2 | **archive + compress** — `tar` per set, `zstd` **where applicable** (already-compressed sets stored as plain `.tar`) | `backup.sh` + `compression_decision` |
| 3 | **hash + verify + manifest** — per-file sha256 table + archive sha256 + integrity test; a recovery MANIFEST | `backup.sh` |
| 4 | **external-drive target** — dated `run_<UTC>` snapshot with retention (`BACKUP_KEEP`) | `backup.sh` |
| 5 | **offsite push** — cifs-mount the IceDrive-synced share, push selected sets | `backup.sh` |
| 6 | **report** — POST NagLight `/api/feed`; **never-silent-green** (failure → `ok=false` + nonzero exit) | `common.sh` `feed_naglight` |

## Files

```
backup.sh            orchestrator (the six steps; --config, --dry-run)
restore.sh           reconstruct + byte-verify a set from a run (the recovery half)
common.sh            shared helpers (logging, cifs mount, compression policy, feed)
backup.env.example   every knob
systemd/awow-backup.{service,timer}   nightly root oneshot + persistent timer
```

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
```

## Try it against the sim fixtures

`sim/mini-serv-sim/run-backup-sim.sh` stands up the Samba fixtures + a privileged
runner, runs a full cycle against them, and does the restore drill. See
`sim/README.md`.
