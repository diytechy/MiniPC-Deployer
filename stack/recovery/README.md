# Hub recovery: watchdog, early kill, loop brake

The hub's answer to a crash, a wedge, or a memory thrash like the one on
2026-10-04: kill the offending container, then reboot **gracefully**, and only
when software can no longer act let the **hardware** reset the board. Every
reboot is counted, whatever caused it, and a brake stops the box looping.

Design: HomeHub `docs/design/HUB_HANG_WATCHDOG_DESIGN_2026-10-05.md` (revision 3).
Requirements: SN-046, SR-049 (evidence and accounting), SR-050 (the
supervisor), SR-051 (reset device, shutdown hand-off and brake).

**It is opt-in and it is not a guarantee.** With `HUB_WATCHDOG_ENABLED=true`
the hub self-heals *when it can*. Nothing here promises to catch every wedge,
and the thresholds are a starting point until a week of measurements says
otherwise.

## What runs, and when

| Part | Always on? | What it does |
|---|---|---|
| `homehub-pressure-sample.timer` (C1) | **yes**, record-only | every 30 s, one JSON journal line tagged `hub-pressure`: PSI, memory, every container's memory, the protected items in full, the top 5 host processes, D-state tasks |
| `homehub-recovery-boot.service` (C5) | **yes**, record-only | at boot, classifies how the *previous* boot ended, commits it to `/var/lib/homehub-recovery/boots.log`, computes the brake, sets `kernel.panic` |
| `homehub-recovery-stamp.service` (C5) | **yes**, record-only | late in every shutdown, after Docker stops, writes the clean-shutdown stamp |
| journald drop-in, `hung_task_warnings=-1` (C6) | **yes** | persistent journal pinned at 4G; every hung task reported |
| `homehub-filebackup.slice` (C4) | **yes** | the library backup's own cgroup, so its memory peak is measured exactly |
| `homehub-recoveryd.service` (C2) | **no** — `HUB_WATCHDOG_ENABLED` | the supervisor: kills, sshd restarts, quarantine, graceful reboot, hardware watchdog |
| earlyoom (C3), tailscaled OOM pass (§6) | **no** — `HUB_WATCHDOG_ENABLED` | fast-spike backstop; the tunnel kept out of reach of any OOM killer |
| `RebootWatchdogSec` hand-off, reset device, `kernel.panic=10` (C6/C7) | **no** — `HUB_WD_TIERS=full` | the hardware half |

firstboot step 6g installs the always-on half and runs `setup-recovery.sh`
on every boot, which converges the opt-in half **on or off** from `.env`.

## What it does

`HUB_WD_TIERS=contain` (rollout P3):
- **L1 — early kill.** Memory PSI `full avg10` is sampled every 5 s. When an
  episode has lasted 120 s and at least 75 % of the window's *measured* samples
  were at or above 40 %, the largest eligible container is killed through its
  cgroup (`cgroup.kill`), at most three times an hour. The kill is confirmed by
  `cgroup.events` reading `populated 0` within 10 s, then Docker is reconciled
  in the background: a container Docker still calls running while its cgroup is
  empty (the 10-04 "ghost") is removed or restarted.
- **Quarantine.** A compose service that is the victim twice within an hour
  is, the next time, **stopped and left stopped** (`docker update
  --restart=no` + `docker stop`, which survives a reboot) instead of being
  killed again. The status goes red until you release it.
- **L2 — sshd.** Three failed banner probes 60 s apart, while memory is
  normal, restart `ssh`.
- **Reboots are only recorded** (`would-have-rebooted`).

`HUB_WD_TIERS=full` (rollout P4) adds:
- **L3 — graceful reboot.** An episode still open 600 s after it started
  (kills do not restart that clock) reboots with `systemctl reboot`. The reason
  is written to `/run/homehub-recovery/intent` first and rides to disk in the
  shutdown stamp.
- **L4 — hardware reset.** The supervisor drives the watchdog
  (`/dev/watchdog0`, 60 s) while it runs. It stops petting — and the board
  resets — if the reboot it asked for does not start within 120 s, if its own
  main loop runs out of memory, or if it dies. On every reboot it hands the
  timer to the shutdown with 300 s and **no** magic close, and systemd's own
  `RebootWatchdogSec=300s` covers the late phase. Worst case from the start of
  an episode to a reset is about 22 minutes; that is a bound, not a prediction.
- **L5 — panic.** `kernel.panic=10`, unless braked.

Every action leaves a `/dev/kmsg` line (written by the main thread, so it
survives a frozen disk) and, when the disk allows, an evidence file
`/var/lib/homehub-recovery/events/<UTC>-<boot>-<action>.json` with the samples
held in memory.

## What it does NOT do

- **It does not fix causes.** A runaway backup is still a runaway backup; this
  limits the damage.
- **It does not catch every wedge.** A hang that leaves the supervisor
  running and memory calm is invisible to it. Nothing here promises 40 % would
  have caught 2026-10-04 — no PSI was recorded that night.
- **It never kills a host process.** Only containers are L1 victims; host
  memory hogs are earlyoom's (C3) or the kernel's.
- **It never kills `caddy` or `technitium`** (`HUB_WD_PROTECT`, Owner D9). If
  one of them is the largest during an episode it records
  `would-have-killed=<name> (protected)` instead, and every `hub-pressure`
  sample logs them in full, so a pass does not become a blind spot. They keep
  their own memory caps.
- **It never waits for a backup** (D8). A recovery in the middle of the
  library backup interrupts it; see "The backup" below.
- **It does not restart anything but sshd**, and it does not touch the panel.
- **Reset on expiry is unproven on this board** until the planned P4 test with
  someone at home.

## Disarm — the only safe way to stop it

```
sudo homehub-recovery disarm
```

writes a one-use authorisation naming **this boot** and **this invocation** of
the supervisor, then stops it; the supervisor magic-closes the watchdog only
when it finds that authorisation. **A plain `systemctl stop` or a reboot is not
a disarm**: the unit has no `ExecStop`, so a stop hands the running timer to the
shutdown, and with no shutdown coming the board resets 300 s later. A
`systemctl restart` is fine — the new instance re-arms.

To turn the capability off for good: `HUB_WATCHDOG_ENABLED=false` in `.env`,
then `sudo bash /opt/homehub/stack/recovery/setup-recovery.sh` (firstboot does
it every boot). That disarms first, refuses to stop a supervisor it could not
disarm, then disables the unit, removes every drop-in and sets
`kernel.panic=0`. The always-on record keeps running.

## The brake

Counted boots: **recovery** (an L3 reboot), **watchdog** (an L4 reset the
board reported), **panic** (a pstore record), and **unexplained** — see D10.
Three counted boots inside 24 h engage the brake:

- `kernel.panic=0` (a panicking box stays down rather than looping);
- the supervisor opens no watchdog and never reboots;
- it **still** kills, restarts sshd and quarantines, none of which can loop the box.

It clears after **24 h with no counted boot** (not when the oldest ages out),
or at once with `sudo homehub-recovery reset-brake`.

If the boot accounting cannot be **committed** (a full or read-only disk), the
supervisor runs **report-only** — every decision recorded as `would-have`,
nothing acted on — because an unwritable disk must not be a way round the brake.

## D10 — is a power cut counted?

It depends on what the board can report, and the code decides from evidence:

- A graceful recovery is known for certain (the intent in the stamp). A panic
  is known by its pstore record.
- A hardware watchdog reset is known **only if the firmware implements WDAT's
  status action**, in which case `/sys/class/watchdog/watchdog0/bootstatus`
  reports `WDIOF_CARDRESET` after the reset.
- **Until the board has once reported `WDIOF_CARDRESET`**, a watchdog reset and
  a power cut look the same, so **unexplained boots are counted** — power cuts
  cost brake budget, which is the price of not being able to tell.
- **The first time it does**, the classifier latches
  `/var/lib/homehub-recovery/bootstatus-proven` and unexplained boots stop
  counting: they are power cuts or hangs the firmware never saw.

The P4 expiry test is what answers this: after the test reset, run
`homehub-recovery status` and read the D10 line.

## Protection (§6)

| From | Who | How |
|---|---|---|
| the supervisor's kill (L1) | `caddy`, `technitium` | `HUB_WD_PROTECT` |
| earlyoom and the kernel OOM killer | `tailscaled`, `homehub-recoveryd` | `OOMScoreAdjust=-1000` |
| the same | `sshd`'s listener | OpenSSH sets its own listener to −1000 and puts each session back to the value it inherited (measured on the hub 2026-10-05). **No drop-in is added for `ssh.service`**: a −1000 unit setting would be inherited by every session, making anything run over SSH unkillable. |

## The backup

The library backup runs under `homehub-filebackup.slice`; after every run the
wrapper logs `filebackup slice memory.peak: … bytes` — the peak since boot
across runs, an **upper** bound for any one run, which is what
`FILEBACKUP_MEM_LIMIT` must be sized from (rollout P2). The supervisor scans
that slice too, so the backup is still in its victim set.

A run interrupted by a recovery leaves FileBackup's staging folder (`Temp`)
behind. Until FileBackup can recover that by itself (Owner D8 — it gates the
`full` tier), the next run refuses with

```
LIBRARY BACKUP REFUSED: staging needs recovery (interrupted by recovery event <id>)
```

and leaves `Temp` exactly as it is; recover it by hand per FileBackup's README.
The NagLight backup lane cannot show this yet (P5).

## Costs, honestly

- **A power cut costs brake budget** until the board proves it reports
  watchdog resets (D10).
- **A brake costs automatic recovery** for up to 24 h.
- **A recovery can interrupt the library backup**, which then needs a person
  until FileBackup self-recovers its staging.
- **The first boot of a fresh image** runs earlyoom with Debian's defaults for
  the few minutes before firstboot converges it off (packages.list says why).
- sshd's log gains one `[preauth]` connection every 60 s from the banner probe
  while the supervisor runs.

## Commands

| | |
|---|---|
| `homehub-recovery status` | mode, health, brake, last boots, quarantined services, the D10 line |
| `homehub-recovery events [-n N]` | the newest evidence records |
| `homehub-recovery disarm` | stop it with the watchdog closed |
| `homehub-recovery reset-brake` | forget the counted boots so far |
| `homehub-recovery release <service>` | undo a quarantine |
| `journalctl -t hub-pressure -o cat` | the pressure record |
| `journalctl -t homehub-recoveryd` / `-t homehub-recovery-boot` | the supervisor / the classifier |

## Files

| File | Part |
|---|---|
| `homehub-recoveryd.py`, `.service` | C2, the supervisor (no `ExecStop`, by design) |
| `homehub-recovery-boot.py` | C5: `stamp` and `classify` |
| `homehub-recovery-boot.service`, `homehub-recovery-stamp.service` | C5 units |
| `homehub-pressure-sample.py`, `.service`, `.timer` | C1 |
| `homehub-recovery` | the CLI (installed as a symlink in `/usr/local/sbin`) |
| `recovery_common.py` | config, paths, durable writes, brake arithmetic, cgroup/Docker views |
| `setup-recovery.sh` | converges the opt-in half on or off |
| `dropins/`, `earlyoom.default.tmpl` | C6/C3 configuration |
| `tests/setup-recovery.test.sh`; `tests/test_hub_recovery_*.py` at the repo root | hermetic tests |
