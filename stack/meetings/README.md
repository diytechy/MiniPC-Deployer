# meetings — the panel's meeting recordings, collected onto the scratch volume

Implements **SR-110** (the pull) and **SR-111** (retention) for OfficeWallNaglight
**WSN-097**, ruled by the Owner on 2026-10-22 (HomeHub open-items A69): the wall
panel records a meeting's audio, both directions, from a control on the panel and
automatically while it carries a Bluetooth call for the dev PC, with an on-screen
indicator whenever capture runs; the files go to the hub's NVMe scratch volume
until the media drive exists. Audio only: nothing here transcribes, analyses or
uploads anything. The panel half (the recorder, SR-108/SR-109) is
`stack/autoinstall/wall/wall-meeting-recorder.py`.

## How a recording reaches the hub

```
panel                                        hub
wall-meeting-recorder ── ready/<name>.opus ─┐
  (+ <name>.opus.sha256, written last)      │   homehub-meeting-pull (every 2 min)
                                            │   1. scratch guard (W4, IF-071)
wall-meeting account                        │   2. ssh … list
  authorized_keys: restrict,command=        │   3. ssh … get NAME > .incoming/NAME.part
  "/usr/local/lib/wall-panel/               │      (hash while streaming; size + sha256 must match)
   wall-meeting-spool" <the hub's key>  ◄───┘   4. rename to meetings/YYYY-MM-DD/NAME
                                                5. ssh … ack NAME SHA256  → the panel deletes
                                                   its copy only if ITS hash is the same
                                                6. ssh … report ok|full
```

Every step is safe to repeat. A transfer that is interrupted or arrives with the
wrong hash leaves the panel's copy and is tried again on the next run; an ack whose
reply was lost is repaired by the next run without a second download.

| File | What |
|---|---|
| `homehub_meetings.py` | `pull`, `retention [--dry-run] [--days N]`, `init-key`, `condition` |
| `homehub-meeting-pull.{service,timer}` | the pull, every 2 minutes |
| `homehub-meeting-retention.{service,timer}` | retention, hourly |

Both services are scratch writers (stack/scratch/README.md): `RequiresMountsFor=`,
`BindsTo=srv-scratch.mount`, the guard before the run and before every new file,
no `CAP_SYS_RESOURCE`. Exit codes: 0 done, 1 the panel could not be reached or a
transfer failed (retried), 2 usage (no panel address), 75 the quota is full, 76 the
guard refused.

## Knobs (stack/.env)

| Knob | Default | |
|---|---|---|
| `MEETING_PULL_ENABLED` | `false` | both timers on/off (and the units' ExecCondition) |
| `MEETING_PANEL_HOST` | empty = `PANEL_IP` | the panel's address |
| `MEETING_PANEL_USER` | `wall-meeting` | the panel's account |
| `MEETING_RETENTION_DAYS` | `14` | **proposed**; the Owner may change it |
| `MEETING_QUOTA_HIGH_PCT` | `90` | then the oldest go while above this share of the quota |

**Why 14 days.** At 48 kbit/s stereo a recording is about 21.6 MB an hour, so the
2 GiB `meetings` quota (W4) holds about 99 hours, about 89 below the high-water:
14 days fits about 6 hours of meetings a day. Past that the high-water rule takes
the oldest early rather than the quota refusing new ones.

## Provisioning (the coordinator's steps)

1. The scratch volume exists (W4a): `/srv/scratch/meetings` is the `meetings`
   project with its quota, and `homehub_scratch.py guard --writer meetings` passes.
2. `MEETING_PULL_ENABLED=true` in the hub's stack `.env` (and the deploy config),
   then firstboot (or by hand: `sudo python3 /opt/homehub/stack/meetings/homehub_meetings.py init-key`)
   makes the key in `/var/lib/homehub-meetings/` (root, 0700) and prints its PUBLIC half.
3. On the panel, `WALL_MEETING_PULL_PUBKEY=<that line>` in `/etc/wall-panel/wall.env`
   (and the deploy config), then firstboot: the `wall-meeting` account gets the one
   restricted key line.
4. First contact pins the panel's host key in `/var/lib/homehub-meetings/known_hosts`
   (`StrictHostKeyChecking=accept-new`); run `sudo systemctl start homehub-meeting-pull`
   once and read `journalctl -t homehub-meetings`.

**A hub reimage makes a new key**: step 3 follows it. A panel reimage keeps
working as long as the deploy config carries `WALL_MEETING_PULL_PUBKEY`.

## Checks

```sh
journalctl -t homehub-meetings --since today        # collected / refused / full
cat /var/lib/homehub-meetings/status.json           # last pull: exit, collected
ls /srv/scratch/meetings/                           # one directory per UTC day
sudo python3 /opt/homehub/stack/meetings/homehub_meetings.py retention --dry-run --days 1
```

Tests: `tests/test_meeting_transfer.py` (TC-1409..TC-1415).
