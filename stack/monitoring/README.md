# monitoring — the hub watches itself (SN-048)

Implements **SN-048**, **SR-085** .. **SR-089** and **SR-122** .. **SR-124** (the metrics log; the wall image's freeze watchdog,
**SR-090**, lives in `stack/autoinstall/wall/`).

The hub records memory pressure every 30 s (SN-046, `stack/recovery/`), but until
this directory nothing read its temperatures, nothing alarmed on a filling disk,
and nothing noticed the offsite sync client being down for most of
2026-09-19..10-21. These four timers close that gap (and the metrics log, below, keeps the history). **They record and alert
only: nothing here starts, stops, kills or reboots anything.** The opt-in
recovery supervisor (`HUB_WATCHDOG_ENABLED`) stays the only component on the box
that may act, and nothing here is on its path.

| Timer | Every | Writes | Alerts |
|---|---|---|---|
| `homehub-telemetry.timer` | 30 s | `/var/lib/homehub-telemetry/telemetry-YYYYMMDD.jsonl` | CPU or NVMe temperature over its limit, throttle counters rising, every core under the frequency floor while busy |
| `homehub-disk-check.timer` | 15 min | `/var/lib/homehub-monitor/disk-status.json` | an NVMe filesystem below its floor, or a configured mount point not mounted |
| `homehub-watch.timer` | 5 min | `/var/lib/homehub-monitor/watch-status.json` | a row of [`watch.table`](watch.table) firing |
| `homehub-liveness.timer` | 60 s | `/var/lib/homehub-monitor/liveness-status.json`, one bundle per incident in `/var/lib/homehub-evidence/<ts>/` | DNS, HTTPS or docker not answering for 3 runs |

Each alert is sent once when its condition starts and once when it clears, at
`high` (liveness: `urgent`) and `default`. Every attempt is one journal line,
delivered or not.

## The alert path (SR-085)

[`homehub-alert.sh`](homehub-alert.sh) is the hub's one way to raise an alert,
factored out of `stack/rustdesk/rustdesk-check.sh` (which now calls it):

- ntfy at `http://<NTFY_BIND_IP or LAN_IP>:<NTFY_PORT>/<HOMEHUB_ALERT_TOPIC>`,
  read from the stack `.env` as text, under a hard timeout;
- rate-limited **per key** (`HOMEHUB_ALERT_REPEAT_SEC`, default 1800): only a
  *delivered* alert stamps its key, so an alert ntfy did not take is retried by
  the monitor's next run while the condition lasts;
- a stamp from the future (the clock moved backwards) is ignored;
- one journal line per call: `alert delivered`, `alert suppressed`,
  `ALERT NOT SENT` (no usable config, no curl) or `ALERT NOT DELIVERED` (ntfy
  down). When ntfy is down **the journal line is the record**.

**Record first, then alert, inside the unit's timeout.** Each run writes its
record (the telemetry sample, the status file) and saves its episode state with
every owed alert marked pending *before* it attempts any alert, and saves the
state again after each attempt. All the alerts of one run share one delivery
budget (`ALERT_DEADLINE_SEC` in each script: 15 s telemetry, 45 s disk, 100 s
watch, 160 s liveness, counted from the start of the run), which leaves at least
`SAVE_RESERVE_SEC` (10 s) of the unit's `TimeoutStartSec`. Each attempt's curl
timeout is cut to what is left, and the helper's process group is killed when
the budget ends. An alert whose turn comes with less than 4 s left is journaled
`ALERT DEFERRED` and stays pending for the next run. A slow ntfy costs a run
its alerts, never its record, and never gets the run killed (SR-085, LLR-1168).

Subscribe: `curl -s http://<lan-ip>:<NTFY_PORT>/<topic>/json?poll=1&since=1h`.

## What each one reads, and what it never touches

- **Never wakes a spun-down disk.** Mounts come from `/proc/self/mountinfo`;
  only filesystems whose block device resolves (through sysfs, including LVM)
  to the NVMe are ever `statfs`'d. The USB backup and library drives are
  reported `skipped` and never touched; the backup drive's free space is the
  backup run's own business.
- **Network:** ntfy, plus the liveness probe's two named local targets
  (Technitium on the LAN address, Caddy on 127.0.0.1). Nothing else.
- **No command lines, no environments.** Processes are read by `comm`;
  docker output uses fixed `--format` strings (names, states, usage).
- Bounded: `CPUQuota`, `MemoryMax`, `TasksMax`, `TimeoutStartSec` on every unit.

### Telemetry (SR-086)

`python3 homehub-telemetry.py summary [--hours 24]` for a digest;
`export --since 2026-10-22T00:00:00Z [--until ...]` for JSONL. Each record:
thermal zones, hwmon temperatures, per-core MHz, CPU busy since the previous
sample, the core/package throttle counters, load, PSI, NVMe filesystem usage,
and the alarms active. Retention like the panel's (WSN-036): the newest 7 day
files and at most 50 MiB, oldest first; the file being written is never deleted
and a day file at the cap is not appended to. "N consecutive samples" is
counted on the monotonic clock and boot id, so a clock jump cannot fake one; a
reboot or a gap of more than two minutes restarts every count.

### Disk (SR-087)

`HOMEHUB_DISK_MOUNTS="/ /var/lib/docker /boot"`; an entry may carry its own
floor, `PATH=N%` or `PATH=NG`. Without one: the smaller of 10 % and 5 GB (the
panel's rule, SR-083). Add `/srv/scratch` here when the scratch volume exists.

### Watches (SR-088)

[`watch.table`](watch.table) is the one table: the IceDrive client
(`ICEDRIVE_MODE=appimage`, absent 15 min), the library backup and the IceDrive
profile capture (no success for 26 h), any container `unhealthy` for 10 min,
any container restarted 3 or more times within 30 min (`restart-loops`,
LLR-1211: a crash loop reads `starting`, never `unhealthy`, and `docker ps`
still says Up - open-items C58 ran to 2,327 restarts that way; the count is
taken from the newest sample at least 30 min old, so the whole window is always
measured, and the alert clears only after a whole 30 min with fewer), and the
drill-only synthetic watch. `HOMEHUB_WATCH_DISABLE="row ..."` switches
rows off without editing the table.

Plan W3b added two kinds and three rows, each row on only when its component
is (LLR-1290..LLR-1292):

| Row | Kind | Fires when | Knob for the threshold | On with |
|---|---|---|---|---|
| `door-segments` | `newest-file` | the newest `/srv/scratch/door/*/door-*.ts*` (the open `.ts.part` is the recording in progress and counts) was last written 15 min ago or more, or nothing has matched for 15 min | `HOMEHUB_WATCH_DOOR_FRESH_MIN` | `DOOR_RECORDER_ENABLED=true` |
| `scratch-mount` | `scratch-identity` | `homehub_scratch.py status` has not said `mounted`, `identity_ok` and `prjquota` on two runs in a row (5 min), or cannot run | `HOMEHUB_WATCH_SCRATCH_MIN` | `HOMEHUB_SCRATCH_ENABLED=true` |
| `meeting-pull` | `unit-success` | `homehub-meeting-pull.service` (every 2 min) has not succeeded for 30 min - which includes the panel being unreachable for that long | `HOMEHUB_WATCH_MEETING_PULL_MIN` | `MEETING_PULL_ENABLED=true` |

- **`newest-file` never follows a symlink** - not the root, not a directory,
  not a file - so a link cannot pull a fresh file from elsewhere into the
  judgement. It looks at no more than 10 000 entries (the door tree holds about
  150 at its cap) and gives the walk 10 s; hitting either bound **fires**, because
  a walk that saw part of the tree cannot vouch for its newest file. A missing
  or unreadable tree is "nothing matched", never "unknown". Point it at NVMe
  paths only: a directory walk would wake a spun-down disk.
- **`scratch-identity` re-implements nothing.** It runs the guard's own module
  (`--env-file <stack .env> status`, 10 s) and reads back its three keys, so
  the watch and the writers' mount guard cannot disagree about which volume is
  the right one. A status that crashes, times out or prints no JSON fires with
  the reason; it never crashes the watch.
- A threshold of `0m` fires at the next run on a healthy job: the drill value,
  only ever set in the override file.

`Get-DeploymentState` (HomeHub `scripts/verify/deployment-state.psd1`) already
measures the same two facts from the outside (`scratch-identity`,
`door-segment-fresh`); these rows are the alert, that is the audit.

### Liveness and evidence (SR-089)

After `HOMEHUB_LIVENESS_FAILURES` (3) consecutive failed runs: **one** bundle
for the incident, then the alert. Bundle contents, caps and failure handling are
in the header of [`homehub_evidence.py`](homehub_evidence.py): per-command
timeouts, a 90 s whole-bundle deadline, per-file (512 KiB) and per-bundle
(4 MiB) caps, at most 20 bundles and 64 MiB in all, and a `manifest.json`
recording every item that failed, timed out, was truncated or could not be
written. The journal extract covers a named list of units and tags only, and
every captured line passes the secrets-floor redactor before it is written.
Read a bundle: `sudo ls /var/lib/homehub-evidence/; sudo cat .../manifest.json`.

### The metrics log (SR-122..SR-124)

Ruled by the Owner on 2026-10-26 (HomeHub open-items A79, plan
`docs/HUB_METRICS_LOG_PLAN_2026-10-26.md` in HomeHub): a record for trend charts
and for debugging by the Owner and agents. **Record only: no alert, no action.**

| Timer | When | Writes |
|---|---|---|
| `homehub-metrics-snapshot.timer` | every 5 min, on the clock | `/var/lib/homehub-metrics/snapshot-YYYYMMDD.jsonl` (UTC day) |
| `homehub-metrics-daily.timer` | 10:00 and 14:05 America/Chicago, `Persistent=true` | `/var/lib/homehub-metrics/daily.jsonl` |
| `homehub-metrics-copy.timer` | Sunday 18:30 America/Chicago, `Persistent=true` | a copy in `HOMEHUB_METRICS_COPY_DIR` on the library |

- **The snapshot** (one line, about 3-6 KB): every gauge as the tracker's
  `/api/today` serves it (value, range, target, `observed_at`, the computed
  `stale` and colour), read as `HOMEHUB_METRICS_USER` - the tracker's real user,
  because the tracker mints a new user for ANY `X-Forwarded-User`, so a blank
  knob asks nothing; the newest `homehub-telemetry` sample; memory and uptime;
  every container's state, health and restart count; every `homehub-*.service`'s
  last result and every failed unit; the watch/disk/liveness verdicts and the
  meeting pull's last run; the **wall panel's status** (below); NVMe SMART once
  an hour. Each block that fails is recorded as `{"ok": false, "error": ...}`
  and costs nothing else. One run deadline (100 s) inside `TimeoutStartSec=120`.
  Its own unit, not the 30 s telemetry sampler's (25 s, no network).
- **The panel's status** is pulled over the meeting pull's own key and forced
  command (`homehub_meetings.ssh_argv`, but `StrictHostKeyChecking=yes`: the
  pull pins the host key, this never does) with the verb `status`. The panel
  answers from three world-readable files: `/run/wall-disk/status.json`,
  `/run/wall-freeze-state/status.json` (the freeze watchdog's last verdict) and
  `/run/wall-panel-telemetry/latest.json` (the newest telemetry sample), plus the
  freeze bundles' count. The hub keeps a short summary of each.
- **The daily row** is for the night that ended that morning: every gauge with
  `observed_at` and `stale` (and `missing`, the ones without a believable
  reading), the heart feeder's night summary, the CPAP's usage minutes and
  score, the overnight (22:00-08:00) CPU/NVMe temperature range, the backup
  units' results. `kind` is `initial` (10:00) or `revision` (14:05); both are
  kept, `late` marks a catch-up more than 30 min after its time, a chart takes
  the latest row per night. A run that finds its row written writes nothing;
  nothing is back-filled for an earlier night.
- **Retention:** 365 snapshot day files, gzip'd (deterministically) after 7, at
  most 16 MiB a day and 1 GiB in all; `daily.jsonl` for ever (64 MiB cap).
- **Where it is kept** (the Owner, 2026-10-26): on the hub NOT in the config
  backups, and never under the tracker's data folder (its git mirror is pushed
  to GitHub). Once a week `copy` puts the closed snapshot days (gzip'd),
  `daily.jsonl` and the raw watch samples into `HOMEHUB_METRICS_COPY_DIR`, a
  folder in the Owner's private Docs tree on the library, which the library's own
  backup covers. It skips, journalled, unless `/srv/library` is in the mount
  table and carries `.homehub-library` (root is not stopped by the bare
  mountpoint's mode 000), and unless the folder's parent already exists. It
  never copies today's file and never deletes at the destination; a file whose
  size and mtime already match is skipped. Each run leaves
  `/var/lib/homehub-metrics/copy-status.json` (last run, result, last success,
  last skip and why), and every snapshot carries it as `monitors.copy`, so a
  skipped week shows without the journal.
- **Charts:** `https://metrics.admin.<domain>/` (Caddy, behind the admin login,
  `stack/monitoring/metrics-site/`): nightly rows, recent snapshots, and a night's
  raw heart rate with its sleep sessions shaded. Static files only.

Read it: `python3 homehub-metrics.py summary [--hours 24]`,
`export [--daily] [--since ISO] [--until ISO]`, or `sudo zcat
/var/lib/homehub-metrics/snapshot-YYYYMMDD.jsonl.gz | jq .` for an old day.

## Knobs

All in the stack `.env` (`stack/.env.example`, section "Hub continuous
monitoring"). An unset knob is its default; the four switches default ON:
`HOMEHUB_TELEMETRY_ENABLED`, `HOMEHUB_DISK_CHECK_ENABLED`,
`HOMEHUB_WATCH_ENABLED`, `HOMEHUB_LIVENESS_ENABLED`, and `HOMEHUB_METRICS_ENABLED` for
the metrics log's three timers. Firstboot step 6h installs
the units on every boot and converges each timer to its knob.

**Overrides for drills** go in `/etc/homehub-monitor/override.env`, which the
monitors (not the stack, not compose) read after `.env`. A drill never edits the
stack `.env`.

## Drills (prove the alert path end to end)

Each one ends by deleting the override. None stops IceDrive, DNS, Caddy or
Docker. Subscribe first:
`curl -s "http://<lan-ip>:<NTFY_PORT>/homehub-alerts/json?poll=1&since=2h"`.

**(a) Synthetic watch.** A dummy process the watch expects:

```sh
sudo install -d -m 0755 /etc/homehub-monitor
printf 'HOMEHUB_WATCH_SYNTHETIC_PROCESS=hh-drill\nHOMEHUB_WATCH_SYNTHETIC_ABSENT_MIN=5\n' | sudo tee /etc/homehub-monitor/override.env
cp /bin/sleep /tmp/hh-drill && (/tmp/hh-drill 900 &)      # present
sudo systemctl start homehub-watch.service                  # "synthetic ok running"
pkill -x hh-drill                                           # now absent
# after >= 5 min, the next timer run (or: sudo systemctl start homehub-watch.service):
#   ntfy: "HomeHub: hh-drill is not running (watch synthetic)"
(/tmp/hh-drill 900 &); sudo systemctl start homehub-watch.service
#   ntfy: "HomeHub: hh-drill is running again (watch synthetic)"
pkill -x hh-drill; rm -f /tmp/hh-drill; sudo rm /etc/homehub-monitor/override.env
```

**(b) Disk threshold.** Raise one mount's floor above its free space:

```sh
printf 'HOMEHUB_DISK_MOUNTS="/ /var/lib/docker /boot /srv/scratch=99%%"\n' | sudo tee /etc/homehub-monitor/override.env
sudo systemctl start homehub-disk-check.service   # ntfy: "HomeHub: /srv/scratch is low on space (... free)"
sudo rm /etc/homehub-monitor/override.env
sudo systemctl start homehub-disk-check.service   # ntfy: "HomeHub: /srv/scratch has space again (... free)"
```

(`/boot=99%` works the same on a hub without the scratch volume.)

**(c) Liveness.** Point the HTTPS check at a local port nothing listens on:

```sh
printf 'HOMEHUB_LIVENESS_HTTPS_PORT=1\n' | sudo tee /etc/homehub-monitor/override.env
# three timer runs (about 3 min): journal "liveness: INCIDENT ..."; exactly one new
# bundle in /var/lib/homehub-evidence; ntfy: "HomeHub: liveness failing (https)"
sudo rm /etc/homehub-monitor/override.env
# next run: ntfy "HomeHub: liveness restored"
```

### The W3b drills: (d) door segments, (e) scratch identity, (f) meeting pull

Each one fires a watch on a **healthy** job by overriding only that row's
threshold (or, for (e), the status tool's path), and is restored by deleting the
override. Nothing is stopped: the recorder keeps recording, the pull keeps
pulling, the volume stays mounted.

Two rules before starting:

- **Look first, and never leave a scratch knob in the override.** The scratch
  writers' mount guard (`homehub_scratch.py guard`, run before every door
  segment) reads `/etc/homehub-monitor/override.env` too, so a `HOMEHUB_SCRATCH_*`
  line there would stop the recorder. The drills below write `HOMEHUB_WATCH_*`
  only, which the guard ignores. `sudo cat /etc/homehub-monitor/override.env`
  first; if it exists and is not yours, stop and ask.
- **One fire per row per 30 min.** A delivered alert stamps its key for
  `HOMEHUB_ALERT_REPEAT_SEC` (1800 s); a repeat inside that window is journaled
  `alert suppressed` and stays pending. The `.clear` has its own key.

Subscribe, then run each drill as one block (a timer run landing between the
`tee` and the `rm` is harmless: it sends the same one alert):

```sh
curl -s "http://<lan-ip>:<NTFY_PORT>/homehub-alerts/json?poll=1&since=2h"

# (d) door segments: 0m fires on the segment being written right now
sudo install -d -m 0755 /etc/homehub-monitor
printf 'HOMEHUB_WATCH_DOOR_FRESH_MIN=0\n' | sudo tee /etc/homehub-monitor/override.env
sudo systemctl start homehub-watch.service
journalctl -u homehub-watch -n 20 --no-pager
#   watch: door-segments     FIRING   newest door-<UTC>.ts.part written 2s ago (N matching)
#   alert delivered to http://... [watch-door-segments]: HomeHub: nothing new matching /srv/scratch/door/*/door-*.ts* in 0m (watch door-segments)
#   ntfy: "HomeHub: nothing new matching /srv/scratch/door/*/door-*.ts* in 0m (watch door-segments)"
sudo rm /etc/homehub-monitor/override.env
sudo systemctl start homehub-watch.service
#   watch: door-segments     ok       newest door-<UTC>.ts.part written 1s ago (N matching)
#   ntfy: "HomeHub: /srv/scratch/door/*/door-*.ts* is being written again (watch door-segments)"

# (e) scratch identity: a status tool that does not exist must alert, at 0m
printf 'HOMEHUB_WATCH_SCRATCH_TOOL=/nonexistent/homehub_scratch.py\nHOMEHUB_WATCH_SCRATCH_MIN=0\n' \
    | sudo tee /etc/homehub-monitor/override.env
sudo systemctl start homehub-watch.service
#   watch: scratch-mount     FIRING   scratch status did not run: /nonexistent/homehub_scratch.py status exited 2: ... (bad for 0s)
#   ntfy: "HomeHub: the scratch volume is not mounted as expected (watch scratch-mount)"
sudo rm /etc/homehub-monitor/override.env
sudo systemctl start homehub-watch.service
#   watch: scratch-mount     ok       /srv/scratch mounted from /dev/mapper/...-hub--scratch, identity and prjquota ok
#   ntfy: "HomeHub: the scratch volume is mounted as expected again (watch scratch-mount)"

# (f) meeting pull: 0m fires on any last success older than now
printf 'HOMEHUB_WATCH_MEETING_PULL_MIN=0\n' | sudo tee /etc/homehub-monitor/override.env
sudo systemctl start homehub-watch.service
#   watch: meeting-pull      FIRING   last success Ns ago
#   ntfy: "HomeHub: homehub-meeting-pull.service has not succeeded in 0m (watch meeting-pull)"
sudo rm /etc/homehub-monitor/override.env
sudo systemctl start homehub-watch.service
#   watch: meeting-pull      ok       last success Ns ago     (needs a success within 30 min)
#   ntfy: "HomeHub: homehub-meeting-pull.service succeeded again (watch meeting-pull)"
```

Afterwards `sudo test ! -e /etc/homehub-monitor/override.env && echo restored`,
and `sudo cat /var/lib/homehub-monitor/watch-status.json` shows all three `ok`.

## What is NOT here

- Recovery of any kind (SN-046's opt-in supervisor owns that).
- An alert route from the panel: the panel's freeze watchdog and disk alarm
  write the panel's journal only.
