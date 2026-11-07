# clips — the hub's motion detector and motion clips (SN-050)

Implements **SR-128** (the detector, `../camera-motion/`), **SR-129** (the cut),
**SR-130** (the re-encode batch), **SR-131** (storage, guard, retention),
**SR-148** (clips on the media drive) and **SR-149** (when that drive drops).
Interfaces: **IF-076** (the motion-event record) and **IF-077** (the clips tree).

The Owner ruled on 2026-10-26 (HomeHub open-items A78; HomeHub
`docs/MEDIA_DRIVE_AND_CAMERA_CLIPS_PLAN_2026-10-26.md` section 0):

* **the detector runs on the hub** and is the event source for clips, **and the
  panel takes its door events from it** instead of decoding the stream itself,
  so one detector owns door motion. The hub half is the panel feed
  (`../camera-motion/event_feed.py`, IF-079; SR-146), built and OFF
  (`CAMERA_DOOR_MOTION_FEED_PORT=0`); until a panel is switched to
  `DOORBELL_MOTION_SOURCE=hub` it keeps its own detector and nothing changes
  for it (deploy step 9);
* a clip is **3 s before the first event to the panel's own display window**:
  5 s after presence ends, or a 30 s hold for a stopped vehicle (WSN-107);
* clips are **cut copy-only** from the rolling recording, then **re-encoded** on
  the iGPU in a low-priority batch (measured before it is switched on);
* **1 year, capped at 100 GB**, the cap winning;
* clips started on a **small, quota-capped area on the NVMe scratch volume**
  while there was no drive, and go to **the media drive** (the Owner, 2026-11-06):
  `/srv/media/Clips`, with the hidden, admin-only, read-only `Clips` share.
  Jellyfin never reads them.

Nothing here talks to the camera. The relay (`../door/`) holds the camera's only
connection; the detector is one more relay consumer, and the cutter reads the
recorder's files.

```
camera ──1 conn── go2rtc relay ──┬── recorder ── /srv/scratch/door/YYYY-MM-DD/*.ts  (~27 h)
                                 ├── panel broker (unchanged)                          │
                                 └── homehub-camera-motion@door                         │
                                       pinned engine ── IF-076 records                  │
                                       /var/lib/homehub-clips/events/door/ ──► cut ◄────┘
                                                                              │ copy-only
                                       /srv/media/Clips/door/YYYY-MM-DD/*.mp4 + .json (IF-077)
                                                    ▲ encode (VA-API, batch)   ▲ retention (hourly)
```

## The pieces

| Piece | Where | Switch | Runs |
|---|---|---|---|
| detector, one per camera | `../camera-motion/camera_motion.py run`, `homehub-camera-motion@<cam>.service` | `CAMERA_<CAM>_MOTION_ENABLED` (**false**) | always, while on |
| the engine | `../camera-motion/motion_engine.py` = OfficeWallNaglight `doorstream/motion.py`, comments removed, code unchanged (`motion-engine.pin`) | — | inside the detector |
| event store | `clip_events.py` → `/var/lib/homehub-clips/events/<cam>/<source>-<UTC day>.jsonl` | — | — |
| cut | `homehub_clips.py cut`, `homehub-clips-cut.timer` | `CLIPS_ENABLED` (**false**) | every minute after the last run |
| re-encode | `homehub_clips.py encode`, `homehub-clips-encode.timer` | `CLIPS_ENCODE_ENABLED` (**false**) | every 15 min, Nice 19, idle I/O, container `--cpu-shares 2` |
| retention | `homehub_clips.py retention`, `homehub-clips-retention.timer` | `CLIPS_ENABLED` | hourly |
| status | `homehub_clips.py status`, `episodes --camera door` | — | by hand |
| the drive's folder | `homehub_clips.py ensure [--plan]` | `CLIPS_ROOT` | firstboot step 6k, and by hand |
| the switch to the drive | `homehub_clips.py switch-to-media [--plan]` | — | once, by hand, under `systemd-run` |

firstboot step 6k installs every unit, enables one detector instance per name in
`CAMERAS` (inert while its knob is off), installs `python3-opencv` when a
camera's knob is true and it is missing (the image carries it in
`packages.list` from now on), and converges the clip timers to their knobs.

## Listeners

The hub's own listeners that belong to the door and the clips (nothing else here
listens; placeholders, the real addresses are the stack's `.env`):

| Listener | Bind | Port knob | Admits | Carries |
|---|---|---|---|---|
| go2rtc relay, RTSP (`../door/`) | `LAN_IP` | `DOOR_RELAY_RTSP_PORT` (8554) | the relay's own credential | the camera's video |
| go2rtc relay, API (`../door/`) | loopback | `DOOR_RELAY_API_PORT` (1984) | the same credential | stream state |
| detector panel feed (IF-079) | `LAN_IP` | `CAMERA_<CAM>_MOTION_FEED_PORT` (**0 = none**; 8556 planned) | `PANEL_IP` only, two clients | door motion lines, no picture |

## The detector

* **One engine.** `motion_engine.py` is the panel's replay-validated engine at
  the commit in `motion-engine.pin`, with the source's comments removed (they
  record dated observations of a household, and this repository is public;
  the Owner, 2026-10-27) and its code unchanged: TC-1543 compares the syntax
  trees with the panel's file at that commit. The run refuses (exit 78, an
  alert, no restart loop) if the file's sha256 is not the pin's. It is fed exactly what
  the panel feeds it: the RAW fisheye from the relay through
  `fps=5,scale=256:256:flags=area,format=gray` (the test compares the string with
  the panel's `motion_filter`), on the `time.monotonic()` arrival clock, on the
  same runtime (Ubuntu's OpenCV 4.6.0 and NumPy 1.26.4, as on the panel).
* **Private zones.** `CAMERA_DOOR_MOTION_APPROACH_POLYGON` and `_ROAD_POLYGON`
  describe the property, so the real values are T1 in HomeHub
  `scripts/deploy/config.homehub.psd1` (a copy of the panel's, held equal by a
  HomeHub contract test) and never in this public repository; the template's
  whole-image placeholder is refused by the engine's own `eligible`.
  `tests/test_camera_motion.py` TC-1567 fails if any private polygon appears in
  any tracked file here.
* **The panel feed** (IF-079). With `CAMERA_DOOR_MOTION_FEED_PORT` set the
  detector also listens for the panel and tells it each engine signal at once,
  unthrottled and separately from the store's throttled records:
  `{"v":1,"type":"hello"...}` on connect, `{"v":1,"type":"motion","camera":"door","cause":..,"id":..,"utc_ms":..}`
  for every signal, `{"v":1,"type":"beat","utc_ms":..,"frame_age_ms":..}` every
  5 s; each line at most 256 bytes, no backlog, never an image, a zone or a
  credential. Only `PANEL_IP` may connect (any other peer is closed at accept),
  two clients at most, 8 KiB queued per client and a client that falls behind is
  dropped. A feed fault cannot affect detection or the records: a bind failure
  or a port in use is a warning and the detector runs on; an unexpected
  exception turns the feed off for that run. The status file
  (`/var/lib/homehub-clips/motion/<cam>.json`) gains
  `feed {port, clients, sent, dropped}` (`port` is the port actually listening,
  0 for none; `sent` counts lines queued to clients; `dropped` counts clients
  let go for falling behind). `hub-feed up` on the panel means the feed is
  connected, not that the engine has finished its warm-up after a restart.
* **Signals to records.** New events (`approach`, `road-stop`) are written at
  once; renewals (`visitor`, `bridge`, `road-hold`) at most every 3 s, and the
  last renewal of a presence always - so the records end where the panel's
  Door would have ended, and the cutter needs no post-roll of its own.
* **Decode.** `software` (default) is bit for bit the panel's raster. `vaapi`
  decodes and scales on the UHD 600. Measured on the hub 2026-10-27 over 120 s
  of a recorded segment: software decode 10.7 s CPU (8.9 % of one core, offline
  from a file; the live relay decode measured 17.7 % on 2026-10-22); VA-API
  decode + `scale_vaapi` 3.5 s (2.9 %); VA-API decode with the CPU's area scale
  80.5 s (`hwdownload` of every 1536x1536 frame - worse, not offered). The
  `scale_vaapi` raster is NOT the panel's: mean 4 grey levels apart, 5.8 % of
  pixels past the engine's 18-level threshold; on one 4-minute window the events
  still matched (the road stop 0.2 s later). Switch only after `replay` agrees
  (deploy step 5). VA-API through the relay's ffconcat stdin input was checked
  on the hub against a recorded file: identical output to a direct input.
* **Ceilings.** The decoder container is capped at `--cpus 0.4` (about twice
  the measured decode) and weighted low (`--cpu-shares 256`); the unit's
  `CPUQuota=25%` bounds the engine. They stop a runaway; the Owner's budget is
  the cost test's verdict.
* **Night and infrared are not proven.** The engine's evidence is a 9.37 h
  daytime corpus; its lit-only rule was replayed on one night false positive.

## Clips

**Episode** (`build_episodes`): an `event` whose cause is in
`CAMERA_<C>_CLIP_CAUSES` (`approach road-stop`) opens one; every record at or
before its end extends it to that record + `CAMERA_<C>_CLIP_LEASE_SEC` (5 s, the
panel's Door lease); a record after the end is a new episode or an orphan. Past
`CAMERA_<C>_CLIP_MAX_SEC` (600) the episode continues as a `continued` clip.
The window is `CAMERA_<C>_CLIP_PRE_SEC` (3 s) before the first event to its end.

**Cut**: once the window closed 15 s ago and the recording has passed its end
(or 3 minutes went by), copy-only: from the last keyframe at or before the window
start (the camera sends one a second, so up to 1 s more pre-roll) to before the
first frame at or after its end. Frames are timed by the segment's name plus PTS
- or, when the camera sent broken PTS (measured: a 10-minute segment spanning
0.07 s of PTS), by index, spread evenly from the segment's start to where its
frames end: the open `.part`'s last write (its mtime), and for a finished
segment the earlier of its last write and the next segment's start (the camera
slows down in low light and infrared). A rotated segment's mtime lands just
after the next start (the recorder opens the next file first; 0.6-2.6 s measured),
so an mtime up to 5 s past the next start still counts as its last write; the
newest finished segment is bounded at `DOOR_RECORDER_SEGMENT_SEC` + 5 s. Anchored
by its own last write, a slow rate is trusted down to 0.5 fps; anchored only by
the next segment's start (its mtime later than that), a rate under half the
nominal may hide a recorder restart, so the nominal `CAMERA_<C>_FPS` is used then
- as it is with no anchor. An anchor implying a rate above the nominal shrinks the
period to fit, so a segment never overlaps the next. The recorder keeps a
cut-back segment's mtime at its last write. The
segments are listed
again for every episode, and a segment that was listed but can no longer be
opened (rotated, pruned) makes the episode wait for the next pass - never lost,
never partial. The bytes go to
ffmpeg in the relay's pinned image (no network, read-only, no capabilities),
`-c copy`, MP4 `+faststart`, as `.mp4.part`; timestamps are regenerated only for
index timing or a recorder restart inside the window, at the rate the frames
were actually recorded at (frames over their span, not the nominal rate); ffprobe must count exactly
the planned frames before the rename. Measured on the dev PC on five real
segments: clips of 11.5-29.4 s at 10 fps, frame counts as planned, no decode
error, the broken-PTS one included.

**Names and records (IF-077)**:
`<root>/door/2026-10-26/door_2026-10-26_14-51-01-0500_road-stop_29s.mp4` (local
time with its offset, the first event's), the same-stem `.json` (every merged
record, requested and actual windows, segments, timing, coverage, gaps, frames,
bytes, sha256, cutter version, the encode result), one line per clip in
`<root>/door/index.jsonl`.

**Room before every cut**: when the clips project would pass
`CLIPS_QUOTA_HIGH_PCT`, the cutter first removes clips by the retention rules -
past their age or the cap, then the oldest - until the cut fits. On the 5 GiB
scratch area this, not the hourly retention, is the normal cycle; the quota stop
and its alert remain only for a cut that cannot fit even then. The encode makes
room for its copy-sized output the same way.

**No duplicates**: a pass killed after a clip's rename but before its ledger
entry finds the clip by its sidecar's episode next time and records it.

**Loss**: a window the recording no longer covers (pruned) or never did (the
recorder was down) is recorded `lost` and alerted (`clips-lost-<cam>`, at most
daily). A failed cut is retried on the next two runs, then recorded `failed`.

**Re-encode**: h264_vaapi (or hevc_vaapi), VBR `CLIPS_ENCODE_KBPS` (300) with a
peak of twice that; the encoded file replaces the copy only when its frame count
matches and it is at most `CLIPS_ENCODE_KEEP_PCT` (80 %) of the copy; otherwise
the copy stays. A batch looks at the last 7 days of clips and starts no new
encode after 10 minutes (it shares one lock with the cut). The sidecar keeps the
copy's sha256 beside the encoded file's; an encode killed between writing its
record and replacing the file is found by size on the next batch and put back. Measured on the hub 2026-10-27, a 30 s daytime window with
movement: copy 2.31 MB, VBR 300 kb/s 1.09 MB, 450 kb/s 1.68 MB (both judged
visually close to the copy at full size); constant QP 30 was 3.4 MB - larger
than the copy, because the camera's daylight stream is already lean. A night
segment's copy was 4.6 MB/min against 1.3 MB at QP 26. Encode CPU: 1.3-2.1 s per
minute of video.

**Retention**: older than `CLIPS_RETENTION_DAYS` (365), then oldest first over
`CLIPS_CAP_MIB` (95367 MiB = 100 GB), then oldest first while the clips quota
project is past `CLIPS_QUOTA_HIGH_PCT` (90 %), down to 5 points under. On the
5 GiB scratch area the quota rule is the one that acts. Every camera folder
under the root counts, configured or not; stale `.part` files go first. A pass
killed while it was deleting leaves `.index-dirty`; the next pass rebuilds that
camera's `index.jsonl` from the sidecars.

**Switching `CLIPS_ROOT` strands the old area.** Retention, status and the
encode look only at the current root, so the clips already in the old one no
longer age out or count. `switch-to-media` takes them across and then moves the
knob, as one unit. Nothing
needs rewriting, because no record names the root: the sidecar's `clip`, the
`clip` in `index.jsonl` and the ledger's `clip` are all relative to the camera
folder, and the ledger stays in `/var/lib/homehub-clips`, so an episode already
cut stays cut and is not cut again. Per clip it copies the sidecar and then the
clip through a `.part`, syncs, compares, renames, and only then removes the
source, with the guard asked before each. A name already there with the same
bytes is the rest of a killed run; with other bytes it is left alone and reported.
Each destination index is marked dirty on disk before the first clip moves, and
rebuilt from the sidecars at the end, which also merges anything already cut on the
drive; the marker goes only after a good rebuild, a failed rebuild is a failed switch,
and a rerun repairs a marked index even with no clip left to move. It removes no clip
to make room, and refuses if they do not all fit.

**The switch cannot strand the pipeline, by construction.** It stops no timer: it
holds the pipeline lock, so a cut or retention pass just skips ("another clips run
holds the lock"), and the kernel drops the lock however the process ends. And
`CLIPS_ROOT` stays on scratch until the last step: the move works on a copy of the
settings, the counts taken inside the lock are checked (every clip that was on either
side is on the drive, none left, every index whole), and only then is `.env` written,
in place, keeping its inode, after a one-time root-only backup at
`/var/lib/homehub-clips/env.pre-switch-to-media`. A pass re-reads `.env` once it has
the lock, so none can cut to the old root afterwards. A failure, a kill or a power
cut before that write therefore leaves cutting on scratch as before, with what already
moved safe on the drive and invisible to scratch's retention until it is run again.
It is bounded at one hour (`RuntimeMaxSec`), so a hung drive cannot hold the lock for long.

**The write itself can fail.** In place is not atomic, so the switch keeps the exact bytes
it found, and if the write, the truncate, the sync or the read-back fails it puts them back
the same way and reads them back too. It then says which happened, in `"env"` and in `why`:
`restored`, or `NOT restored`, with the backup's path and what to do. A kill in that same
moment can do neither; step 5 says how to tell.

**Where, on the media drive**: `CLIPS_ROOT=/srv/media/Clips`. "Clips on the media
drive" below.

**Where, on scratch**: `CLIPS_ROOT=/srv/scratch/clips` - the scratch volume's `clips`
project 7104, 5 GiB (`HOMEHUB_SCRATCH_QUOTA_CLIPS_MIB`) once the volume is grown
from 18 to 22 GiB (deploy step 2), behind the scratch guard as writer `clips`. It
is sized before ottertube, so ottertube is reduced first: about 4800 MiB granted
on the grown volume (it holds under 1 GiB of a 3 GiB list budget). A quota hit stops cut and encode
(exit 75, `clips-quota` alert, a stop file) until retention has made room.

## Clips on the media drive

**1 year, capped at 100 GB, the cap winning, oldest first** (the Owner, 2026-10-26).
Nothing here is backed up, and nothing leaves the house.

**The cap acts; the quota is a wall behind it.** `CLIPS_CAP_MIB` (95367) is what
retention keeps to, hourly, counting bytes. The quota project (7105, one for the
tree, because the cap is one number over all cameras) has a hard limit `ensure`
derives from it: cap / (`CLIPS_QUOTA_HIGH_PCT` - 5 %), 112197 MiB. So the quota
rule starts at 100977 MiB, 5.6 GiB past the cap, and when it fires it trims to the
cap and no further. That room is what the cap does not count: whole blocks, folders,
an hour of new clips, the encode trial. `make_room` works from the same project, so
a burst between two hourly passes is handled before the cut. Change the cap and run
`ensure` again.

**`ensure`** (root, idempotent, firstboot step 6k when `CLIPS_ROOT` is the drive):

* refuses, exit 4 and nothing touched, unless `/srv/media` is a mount point, ext4,
  `prjquota`, writable, and the device mounted there is the disk
  `/etc/homehub-samba/drive-identity.conf` names, or a partition of it (sysfs, so no
  read reaches the drive). A label is not enough, and a missing row is a refusal.
  The folder is made through a descriptor of the mount, so a drive that goes between
  the check and the `mkdir` cannot put it on `/`;
* makes the `homehub-clips` account, uid and gid 3100, no home, nologin. Fixed, because
  the group is on the drive as a number. A name or id that means something else is refused;
* makes `/srv/media/Clips` `root:homehub-clips 2750` and removes the `/srv/media`
  default ACL it would inherit (group write, other read);
* puts it in project 7105 with inherit and sets the hard limit. A folder that already
  holds files in another project is refused: they would not be counted.

**What clips land with.** The cutter's ffmpeg is uid 0 in a container with every
capability dropped, so it can write only where uid 0 is the owner, and the units run
as root. That is why the folder is root's, not `homehub-clips`'s as the plan had it.
The group does the reading: setgid puts every file and folder in `homehub-clips`
whoever made it, sidecars land 0640 (the units' umask 0027), clips 0644 (docker's
0022), day folders 2750. No default ACL is needed. The `other` bit on a clip reaches
nobody: nothing outside the group gets past the 2750 folder. `homehub-clips` can read
everything and write nothing, so the share is read-only twice.

**The guard** adds, to what `ensure` checks: the process without CAP_SYS_RESOURCE, the
root a real directory on that device, in project 7105 with inherit, and the filesystem
still taking writes (an empty file, made and removed). A full quota is not a refusal, or
retention could never run to clear it. No reason names the disk: they go to alerts.

**The `Clips` share** (HomeHub `storage-map.md` section 3c): `/srv/media/Clips`,
hidden, `admin` only, read-only to `admin` too, `force user = homehub-clips`,
`root preexec` the library guard with `--library /srv/media`, so a missing, stale or
read-only drive refuses the connection instead of serving an empty folder.

**When the drive drops.** Nothing here can reach the door link, the recorder or the
detector: they write only to the scratch volume and `/var/lib/homehub-clips`, and only
the cut, encode and retention units read `CLIPS_ROOT`, under their own CPU and memory
limits. What happens, in order:

| When | What | Alert |
|---|---|---|
| at once | ext4 shuts down. The mount's line stays in mountinfo with its device gone (measured, 6.8 kernel, `TC-1695`), and the share refuses | — |
| within 15 min | `homehub-disk-check`: `/srv/media` is absent, whether the line went or stayed | `disk-absent-srv-media`, high, "/srv/media is not mounted"; once again on recovery. **Not** for a read-only mount: the device is alive, and a USB path is never statfs'd |
| the next ready episode | the cut's guard refuses, exit 76, the unit shows failed, the timer carries on | `clips-guard`, high, at most daily, the reason in the body. A pass that finds no ready episode says nothing |
| hourly | retention refuses, exit 76, a journal warning, nothing deleted | none of its own |
| until it returns | **events are untouched**: they stay in `/var/lib/homehub-clips/events`, no ledger entry is written, and a refused guard is not a loss. Each minute's pass re-reads the oldest episode's segments (about 50 MB each), then stops at the guard | — |
| from 10 min after it returns | the health timer's `--reattach` (`umount -l` if stale, then `systemctl start srv-media.mount`, so fsck runs) puts it back, unless `/run/homehub-no-remount` exists. The next cut pass takes every waiting episode, oldest first | the disk check's recovery line |
| about 27 h after an event | its segments rotate out, it is recorded `lost` | `clips-lost-door`, high, at most daily |

**An I/O error short of a disconnect is a different case.** `errors=remount-ro` does
not do what it says on this kernel: ext4 logs "Remounting filesystem read-only" and
shuts itself down without setting the flag, so mountinfo and statvfs still say `rw`
(measured on 6.8, `TC-1695`). The disk check and the share's guard read those, so
neither sees it: reads through the share simply fail. The clips guard sees it, because it
makes and removes an empty file, and refuses with `clips-guard`. A cut caught mid-way
costs the episode none of its three attempts. Nothing re-attaches a drive whose device is
alive, so this one needs a hand: `sudo umount /srv/media && sudo systemctl start
srv-media.mount`, which is also what makes the unit's fsck run.

The plan's "alert at 6 h of absence" and its `N unhealthy in M hours` escalation are
not built; the "not mounted" alert covers the first, and repeats every
`HOMEHUB_ALERT_REPEAT_SEC` while the state holds.

## Tests

* `tests/test_hub_clips.py` (TC-1541, 1542, 1551-1566, 1568, 1686-1693). TC-1569 runs
  only on Linux with docker (WSL on the dev PC), with
  `HOMEHUB_CLIPS_SEGMENTS=<a door segment tree>` and `-m release`; the DST
  check of TC-1557 needs `time.tzset` (POSIX) and is skipped on Windows.
* `tests/test_camera_motion.py` (TC-1543-1550, 1567): the real engine on
  synthetic frames; the panel's filter and the engine's code against the sibling
  OfficeWallNaglight checkout; no private zone in this repository.
* `../scratch/tests/scratch-fs.root-test.sh` check 7: `clips` stops at its quota.
* `tests/clips-media.root-test.sh` (TC-1695, 18 checks): real ext4, real project
  quota, a real virtio-scsi disk formatted as plan section 3.2 says, deleted under the
  mount and brought back. Only in the throwaway guest, because it uses the hub's real
  paths: `bash stack/scratch/tests/run-root-suite-in-qemu.sh <vmlinuz> <quota modules>
  clips/tests/clips-media.root-test.sh`. WSL2 cannot run it (no `CONFIG_QFMT_V2`).
* `../samba/tests/library-guard-media.test.sh` (TC-1696) and
  `../provision/tests/provision-samba-paths.test.sh` (TC-1697), both in
  `../run-hermetic-tests.sh`.

---

## Deploy steps (the coordinator's)

`<hub>` is the hub's address. Nothing here prints a secret; the zones are T1,
not secret, but they are private: they travel on stdin, never in a command line
or a log.

**1. Deliver.** From the merged integration branch, on the dev PC:
`python scripts/deploy_stack.py --target hub@<hub> --revision <merge commit>`
(dry run; read it), then again with `--apply`. Then on the hub:

```sh
sudo apt-get install -y --no-install-recommends python3-opencv
python3 -c 'import cv2, numpy; print(cv2.__version__, numpy.__version__)'   # 4.6.0 1.26.4
sudo install -m 0644 /opt/homehub/stack/camera-motion/homehub-camera-motion@.service /etc/systemd/system/
sudo install -m 0644 /opt/homehub/stack/clips/homehub-clips-*.service /opt/homehub/stack/clips/homehub-clips-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

**2. Grow the scratch volume and make the clips area** (the Owner, 2026-10-27:
22 GiB, clips 5 GiB). Set `HOMEHUB_SCRATCH_SIZE_MIB=22528`,
`HOMEHUB_SCRATCH_VG_RESERVE_MIB=1200` and `HOMEHUB_SCRATCH_QUOTA_CLIPS_MIB=5120` in
`/opt/homehub/stack/.env` TOGETHER, edited in place (the live file still says
`VG_RESERVE_MIB=4096`, which the VG cannot keep with a 4 GiB grow: the grow would be
refused - with an alert, the volume carrying on at 18 GiB - not done). Then read
the plan - it changes nothing:

```sh
sudo vgs --units m -o vg_free,vg_extent_size,vg_free_count ubuntu-vg     # 5364 MiB / 1341 extents on 2026-10-27
sudo python3 /opt/homehub/stack/scratch/homehub_scratch.py plan
#   would: grow LV /dev/ubuntu-vg/hub-scratch by 1024 extents (18432 -> 22528 MiB), leaving 317 extents (1.24 GiB) free
#   would: grow the ext4 filesystem on /dev/ubuntu-vg/hub-scratch online to fill the LV (resize2fs)
#   would: create /srv/scratch/clips, put it in quota project 7104 (inherit)
#   "grow_refused" in the report (the VG cannot give it and keep the reserve): fix the knobs first
```

Then `sudo python3 .../homehub_scratch.py ensure` (online: the recorder keeps
writing) and `status`: `usable_mib` about 22200, door 8192, meetings 2048, clips
5120, ottertube about 4800 (reduced first, as before). `findmnt /srv/scratch`
and `df -h /srv/scratch` show the new size. A boot runs `ensure` too, so a raised
knob is applied at the next boot if it is not applied by hand.

**3. The knobs, still off.** Append the camera block of `.env.example`
(`CAMERAS` .. `CLIPS_ENCODE_MAX_PER_RUN`) to `/opt/homehub/stack/.env` ONCE, with
`CAMERA_DOOR_MOTION_CALIBRATED=true` and the zones from HomeHub
`config.homehub.psd1` (quoted as they are there), sent over stdin
(`ssh hub@<hub> 'sudo tee -a /opt/homehub/stack/.env >/dev/null' < block.env`).
Pre-flight:

```sh
sudo python3 /opt/homehub/stack/camera-motion/camera_motion.py check --camera door
#   {"eligible": true, "enabled": false, "road_zone": true, "decode": "software", "engine": "b6310d1..."}
```

Every later knob change edits the line in place (`sudo sed -i
's/^CAMERA_DOOR_MOTION_ENABLED=.*/CAMERA_DOOR_MOTION_ENABLED=true/' ...`); every
reader takes the last assignment, but one line per key is what a reader of the
file expects.

**4. The measured cost test** (the Owner's budget: at most 25 % of one core for
the detector, decoder and engine together; PSI cpu within 5 points). Baseline
first, detector off: `sudo python3 .../camera_motion.py cost --camera door --seconds 1800`
(reports `pass: false, the detector is not running` and the PSI mean: that is the
baseline). Then `CAMERA_DOOR_MOTION_ENABLED=true` in `.env`,
`sudo systemctl enable --now homehub-camera-motion@door`, and
`journalctl -t homehub-camera-motion -n 5` shows "reading /door from the relay
... software decode". Two minutes later:

```sh
sudo python3 /opt/homehub/stack/camera-motion/camera_motion.py cost --camera door --seconds 1800
#   pass: true when decoder + detector <= 25; compare psi_some_avg60_mean with the baseline
```

**Stop if** it fails either way: `CAMERA_DOOR_MOTION_ENABLED=false`,
`sudo systemctl stop homehub-camera-motion@door`, and record the numbers. Also
stop if the panel's door view or motion degrades (one more relay consumer).

**5. VA-API, only if step 4 failed on CPU** (or the Owner asks): replay the same
recorded hours both ways and compare the events (a few daylight hours with
visitors; replay costs about a tenth of the video's length in CPU at the lowest
weight):

```sh
sudo python3 .../camera_motion.py replay --camera door --since <UTC> --until <UTC> --decode software > /tmp/sw.jsonl
sudo python3 .../camera_motion.py replay --camera door --since <UTC> --until <UTC> --decode vaapi > /tmp/va.jsonl
```

The same new events within a second: set `CAMERA_DOOR_MOTION_DECODE=vaapi`,
restart the unit, and repeat step 4.

**6. Clips on - not before 27 h after step 1.** A segment the old recorder cut
back after a stall carries the stall's time as its mtime, which would stretch its
night-time frame timing; the recorder delivered in step 1 keeps the last write.
Wait one full rotation of the NVMe segments (about 27 h at the 7.5 GiB cap) so
every segment left was written by it. Then `CLIPS_ENABLED=true`, then
`sudo systemctl enable --now homehub-clips-cut.timer homehub-clips-retention.timer`.
After the next visitor: `sudo python3 /opt/homehub/stack/clips/homehub_clips.py episodes --camera door`
and `status`, and the clip under `/srv/scratch/clips/door/<day>/` plays.

**7. Re-encode, after a day of clips with both daylight and night in it.** First
the trial, which replaces nothing (the Owner, 2026-10-27):

```sh
sudo systemd-run --wait --pipe --quiet -p CapabilityBoundingSet=~CAP_SYS_RESOURCE -p NoNewPrivileges=yes -p Nice=19 -p IOSchedulingClass=idle -p CPUQuota=25% /usr/bin/python3 /opt/homehub/stack/clips/homehub_clips.py encode-trial --camera door --day <YYYY-MM-DD> --max-clips 4
```

Through `systemd-run`, NOT `sudo python3 ...`: a root shell holds CAP_SYS_RESOURCE,
for which the quota is no wall, so the clips guard refuses it (and says this
command). The unit drops the capability, as the clips units do, and runs at Nice
19 with idle I/O; the trial's encoder and SSIM containers are capped at
`--cpus 0.4` with the lowest CPU weight. The same goes for any other verb that
writes the clips area by hand; `status` and `episodes` only read and run as they
are.

It encodes two daylight and two night clips of that day with every combination
of h264 and hevc (HEVC is H.265), native and 1024 square, 300 and 200 kb/s, plus
h264 native 300 kb/s at 5 fps, and prints size (and percent of the copy), encode
CPU and wall seconds and SSIM against the copy per combination, by day and by
night; `trial.json` and `trial.txt` and one sample per combination are in
`/srv/scratch/clips/.trial/door-<day>/` for the Owner to watch side by side
(fetch them read-only). It holds the pipeline lock for at most 30 minutes, runs
at the lowest priority, and refuses to start when the clips area is too full.
From it the Owner chooses `CLIPS_ENCODE_CODEC`, `CLIPS_ENCODE_KBPS`, and, only if
wanted, `CLIPS_ENCODE_SIZE` (0 = native; e.g. 1024) and `CLIPS_ENCODE_FPS`
(0 = unchanged; e.g. 5). Then `sudo systemd-run --wait --pipe --quiet -p CapabilityBoundingSet=~CAP_SYS_RESOURCE -p NoNewPrivileges=yes -p Nice=19 -p IOSchedulingClass=idle -p CPUQuota=25% /usr/bin/python3 /opt/homehub/stack/clips/homehub_clips.py encode-trial --clean`, set
`CLIPS_ENCODE_ENABLED=true`, run one batch
(`sudo systemctl start homehub-clips-encode.service`), read the sidecars' `encode`
(kept, bytes before and after, seconds) and check PSI. Keep it:
`sudo systemctl enable --now homehub-clips-encode.timer`; or set the knob back.
A first look on the hub (2026-10-27, one 30 s daytime window, copy 2.31 MB):
h264 1024 square 200 kb/s 0.75 MB, SSIM 0.927; hevc the same, 0.75 MB, 0.927;
h264 native 300 kb/s at 5 fps 1.12 MB, 0.961.

**8. Make it survive a reimage.** In HomeHub `config.homehub.psd1`, set the knobs
that were switched on (`CAMERA_DOOR_MOTION_ENABLED`, `CAMERA_DOOR_MOTION_DECODE`
if changed, `CLIPS_ENABLED`, `CLIPS_ENCODE_ENABLED`), and commit.

**9. The panel feed, hub first and then the panel** (the Owner, 2026-10-26; the
panel's side is OfficeWallNaglight's `doorstream`, `DOORBELL_MOTION_SOURCE`).
`deploy_stack.py` does NOT write `.env`, on the hub or on the panel: every key
below is a hand edit of the live file (the hub's `/opt/homehub/stack/.env`, and
the panel's `wall.env`, which the deployed HomeHub config files
`config.homehub.psd1` and `config.wall.psd1` render at a reimage). Panel first
would leave Door motion silent until the hub lands, so:

1. Hub: deliver the revision (step 1), then in `.env` set
   `CAMERA_DOOR_MOTION_FEED_PORT=8556` (any port 1024..65535) and restart:
   `sudo systemctl restart homehub-camera-motion@door`. Check the journal says
   "event feed listening", `ss -ltn` shows the port on `LAN_IP` only, and
   `motion/door.json` has `"feed": {"port": 8556, ...}`. A detector restart is a
   gap in Door motion (`RestartSec` 15 s, the decoder, the engine's warm-up).
2. Panel: ship the paired release (the new firstboot and app), then in `wall.env`
   `DOORBELL_MOTION_SOURCE=hub` and `DOORBELL_MOTION_HUB_PORT=8556` (the same
   number), and re-render the credentials (reboot, or the supported firstboot
   path). The kiosk journal shows `[door] hub-feed state=up`; on the hub
   `feed.clients` is 1 and `sent` rises with the beat.
3. Back: `DOORBELL_MOTION_SOURCE=panel` in `wall.env`, then
   `printf panel | sudo tee /run/wall-door-credentials/motion-source` and restart
   `wall-door-stream` (or reboot). The panel decodes for itself again; the hub's
   feed may stay on. Turning the hub's feed off first would leave a `hub` panel
   with no door events: do the panel first when going back.

**10. Clips on the media drive** (the Owner, 2026-11-06). Run in order; stop at any
line that does not read as written. It changes nothing for the detector, the recorder
or the door link, and steps 10.1 to 10.4 change nothing for clips either. `<rev>`
is the merged MiniPC-Deployer revision and `<hub>` the hub's address.

1. Deliver. On the dev PC, from the merged integration branches:
   `python scripts/deploy_stack.py --target hub@<hub> --revision <rev>` (dry run; read
   it), then again with `--apply`. Then in HomeHub:
   `pwsh scripts\deploy\Generate-FromStorageMap.ps1`, and
   `scp scripts\deploy\out\generated\smb.conf.fragment scripts\deploy\out\generated\drive-identity.conf hub@<hub>:/tmp/`.
   On the hub:

   ```sh
   sudo install -m 0755 /opt/homehub/stack/samba/library-guard.sh /usr/local/sbin/homehub-library-guard
   sudo install -m 0644 /opt/homehub/stack/samba/homehub-library-health.service /etc/systemd/system/
   sudo systemctl daemon-reload
   systemctl cat srv-media.mount | sed -n 1,3p       # the unit exists: the reattach starts it
   ```

2. The identity. It is generated, not typed.

   ```sh
   sudo grep -c '^/srv/media' /etc/homehub-samba/drive-identity.conf   # 1. If 0, and only then:
   sudo sh -c 'cat /tmp/drive-identity.conf >/etc/homehub-samba/drive-identity.conf'
   ```

3. The folder, account and quota, with `CLIPS_ROOT` still on scratch.

   ```sh
   sudo python3 /opt/homehub/stack/clips/homehub_clips.py ensure --plan
   #   would: create group, create user, create /srv/media/Clips, ..., set the hard quota to 112197 MiB
   sudo python3 /opt/homehub/stack/clips/homehub_clips.py ensure
   sudo python3 /opt/homehub/stack/clips/homehub_clips.py ensure     # "actions": []
   stat -c '%u %g %a' /srv/media/Clips                               # 0 3100 2750
   getent passwd homehub-clips                                       # homehub-clips:x:3100:3100::...:/usr/sbin/nologin
   ```

   `REFUSED` with exit 4 means `/srv/media` is not the drive: read the reason, fix that,
   go no further.

4. The share. The hub's `smb.conf` is a concatenated copy, so the stanza is appended;
   `[Clips]` is the last one in the fragment.

   ```sh
   sudo diff /etc/homehub-samba/smb.conf.fragment /tmp/smb.conf.fragment   # the header's hash and [Clips], nothing else
   sudo cp -a /etc/samba/smb.conf /etc/samba/smb.conf.pre-clips
   sudo sh -c 'cat /tmp/smb.conf.fragment >/etc/homehub-samba/smb.conf.fragment'   # keeps its owner and mode
   sudo sh -c "sed -n '/^\[Clips\]/,\$p' /etc/homehub-samba/smb.conf.fragment >>/etc/samba/smb.conf"
   sudo testparm -s --section-name=Clips 2>/dev/null   # path, force user, read only, valid users, root preexec
   sudo smbcontrol smbd reload-config
   ```

   Anything else in the diff, or a `testparm` complaint: restore `smb.conf.pre-clips`,
   reload, and stop. `Backup` and the backup itself are not touched: this is a reload.
   Then `rm /tmp/smb.conf.fragment /tmp/drive-identity.conf`.

5. The switch: one command, then checks. Nothing is stopped: while it runs the cut and
   retention timers skip, and events queue as they always do. A lost session does not
   matter, it runs detached: only the wait below is yours, and it can be started again.

   ```sh
   sudo python3 /opt/homehub/stack/clips/homehub_clips.py switch-to-media --plan
   #   "guard": "ok", "root_now": "/srv/scratch/clips", "clips": N (the old area now), "on_drive": 0
   sudo systemd-run --unit=homehub-clips-switch --collect -p RuntimeMaxSec=1h -p CapabilityBoundingSet=~CAP_SYS_RESOURCE -p NoNewPrivileges=yes /usr/bin/python3 /opt/homehub/stack/clips/homehub_clips.py switch-to-media
   while systemctl is-active -q homehub-clips-switch; do sleep 5; done   # returns when it has finished
   sudo cat /var/lib/homehub-clips/switch-to-media.json
   #   "state": "done", "ok": true, "env": "written", "left": 0, "drive_after" = "drive_before" + "source_before",
   #   "root_after": "/srv/media/Clips". Those counts were taken inside the lock: they are the check.
   sudo grep -E '^(CLIPS_ROOT|HOMEHUB_DISK_MOUNTS)=' /opt/homehub/stack/.env
   sudo python3 /opt/homehub/stack/clips/homehub_clips.py status   # "root": "/srv/media/Clips", "clips" = "drive_after" or a little more, "quota": {"limit_mib": 112197, ...}
   sudo find /srv/scratch/clips -type f                            # nothing
   sudo journalctl -u homehub-clips-switch -n 20
   ```

   `"state": "failed"`, by `"env"`:

   * `"not written"`: `.env` was not touched. Read `why`, fix that, run the same command again;
     it repairs and finishes.
   * `"restored"`: the write failed and `.env` is back as it was. The same, once the write's
     cause (`why`) is fixed.
   * `"NOT restored"`: `.env` may be damaged, and `why` gives the backup's path and the two
     commands. Do that before anything else: every reader of `.env` is affected, not only clips.

   `"state": "running"` with the unit gone means it was killed. If
   `/var/lib/homehub-clips/env.pre-switch-to-media` does not exist, the write was never reached
   and `.env` is untouched: run it again. If it exists, the kill may have landed in the write:
   `sudo wc -c` both files and read the two `grep`ped lines above, restore from the backup as
   for `"NOT restored"` if anything is wrong, then run it again.

   Not `sudo python3` for the switch itself: a root shell holds CAP_SYS_RESOURCE and the guard
   refuses it (and says this command).

6. Look at it. `sudo systemctl start homehub-disk-check.service; sudo journalctl -t homehub-disk-check -n 6`
   shows `disk: skipped /srv/media`. From the dev PC, `\\homehub.<domain>\Clips` as `admin`
   lists `door` and a clip plays, and a copy into it is refused. After the next visitor,
   `sudo sh -c "stat -c '%U %G %a %n' /srv/media/Clips/door/<day>/*.mp4"` says `root homehub-clips 644`
   (in `sudo sh -c` because `hub` cannot enter the folder, so its own shell cannot expand the
   `*`), and `sudo python3 /opt/homehub/stack/clips/homehub_clips.py status` shows `last_clip` moving.

**Back:** `CLIPS_ROOT=/srv/scratch/clips`, edited in place, never with `sed -i`, which replaces
the inode: `sudo sh -c "umask 077; sed 's|^CLIPS_ROOT=.*|CLIPS_ROOT=/srv/scratch/clips|' /opt/homehub/stack/.env >/root/env.new && cat /root/env.new >/opt/homehub/stack/.env && rm /root/env.new"`.
No timer needs stopping; the next pass reads it. New clips go to scratch; the ones on the drive
stay readable through the share, and nothing ages them out until the switch comes back.
`HOMEHUB_DISK_MOUNTS` can stay. The share, the account and the folder cost nothing left in place.

**Rollback**: the knobs to false; `sudo systemctl disable --now homehub-camera-motion@door homehub-clips-cut.timer homehub-clips-retention.timer homehub-clips-encode.timer`.
The clips area and its clips stay until removed by hand.
