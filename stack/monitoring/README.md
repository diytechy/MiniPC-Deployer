# monitoring — the hub watches itself (SN-048)

Implements **SN-048**, **SR-085** .. **SR-089** (the wall image's freeze watchdog,
**SR-090**, lives in `stack/autoinstall/wall/`).

The hub records memory pressure every 30 s (SN-046, `stack/recovery/`), but until
this directory nothing read its temperatures, nothing alarmed on a filling disk,
and nothing noticed the offsite sync client being down for most of
2026-09-19..10-21. These four timers close that gap. **They record and alert
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
and the drill-only synthetic watch. `HOMEHUB_WATCH_DISABLE="row ..."` switches
rows off without editing the table.

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

## Knobs

All in the stack `.env` (`stack/.env.example`, section "Hub continuous
monitoring"). An unset knob is its default; the four switches default ON:
`HOMEHUB_TELEMETRY_ENABLED`, `HOMEHUB_DISK_CHECK_ENABLED`,
`HOMEHUB_WATCH_ENABLED`, `HOMEHUB_LIVENESS_ENABLED`. Firstboot step 6h installs
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

## What is NOT here

- Recovery of any kind (SN-046's opt-in supervisor owns that).
- The door recorder and scratch-volume identity watches (plan W3b, after W4).
- An alert route from the panel: the panel's freeze watchdog and disk alarm
  write the panel's journal only.
