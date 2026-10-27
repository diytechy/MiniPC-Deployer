# clips — the hub's motion detector and motion clips (SN-050)

Implements **SR-128** (the detector, `../camera-motion/`), **SR-129** (the cut),
**SR-130** (the re-encode batch) and **SR-131** (storage, guard, retention).
Interfaces: **IF-076** (the motion-event record) and **IF-077** (the clips tree).

The Owner ruled on 2026-10-26 (HomeHub open-items A78; HomeHub
`docs/MEDIA_DRIVE_AND_CAMERA_CLIPS_PLAN_2026-10-26.md` section 0):

* **the detector runs on the hub** and is the event source for clips. The panel
  keeps its own detector for now; taking its door events from the hub is a later
  step, not built here;
* a clip is **3 s before the first event to the panel's own display window**:
  5 s after presence ends, or a 30 s hold for a stopped vehicle (WSN-107);
* clips are **cut copy-only** from the rolling recording, then **re-encoded** on
  the iGPU in a low-priority batch (measured before it is switched on);
* **1 year, capped at 100 GB**, the cap winning;
* the media drive is not bought yet (C111), so clips go to a **small,
  quota-capped area on the NVMe scratch volume**, switched to the drive later by
  one knob. The `Clips` share, the drive, Jellyfin and the drive's storage-map
  rows are not part of this.

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
                                       /srv/scratch/clips/door/YYYY-MM-DD/*.mp4 + .json (IF-077)
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

firstboot step 6k installs every unit, enables one detector instance per name in
`CAMERAS` (inert while its knob is off), installs `python3-opencv` when a
camera's knob is true and it is missing (the image carries it in
`packages.list` from now on), and converges the clip timers to their knobs.

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
0.07 s of PTS), by index, spread evenly from the segment's start to the NEXT
segment's start (the camera slows down in low light and infrared; the nominal
`CAMERA_<C>_FPS` is used only for the open `.part`). The segments are listed
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
longer age out or count. At the switch, move them into the new root's camera
folders (keeping the day folders and sidecars together), or delete them.

**Where**: `CLIPS_ROOT=/srv/scratch/clips` - the scratch volume's `clips`
project 7104, 5 GiB (`HOMEHUB_SCRATCH_QUOTA_CLIPS_MIB`) once the volume is grown
from 18 to 22 GiB (deploy step 2), behind the scratch guard as writer `clips`. It
is sized before ottertube, so ottertube is reduced first: about 4800 MiB granted
on the grown volume (it holds under 1 GiB of a 3 GiB list budget). `/srv/media/Clips` is the only other accepted value; its
guard checks a separate ext4 mount with project quotas, writable - the drive's
identity check comes with the drive (C111). A quota hit stops cut and encode
(exit 75, `clips-quota` alert, a stop file) until retention has made room.

## Tests

* `tests/test_hub_clips.py` (TC-1541, 1542, 1551-1566, 1568). TC-1569 runs
  only on Linux with docker (WSL on the dev PC), with
  `HOMEHUB_CLIPS_SEGMENTS=<a door segment tree>` and `-m release`; the DST
  check of TC-1557 needs `time.tzset` (POSIX) and is skipped on Windows.
* `tests/test_camera_motion.py` (TC-1543-1550, 1567): the real engine on
  synthetic frames; the panel's filter and the engine's code against the sibling
  OfficeWallNaglight checkout; no private zone in this repository.
* `../scratch/tests/scratch-fs.root-test.sh` check 7: `clips` stops at its quota.

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
`/opt/homehub/stack/.env` (edit in place), then read the plan - it changes nothing:

```sh
sudo vgs --units m -o vg_free,vg_extent_size,vg_free_count ubuntu-vg     # 5364 MiB / 1341 extents on 2026-10-27
sudo python3 /opt/homehub/stack/scratch/homehub_scratch.py plan
#   would: grow LV /dev/ubuntu-vg/hub-scratch by 1024 extents (18432 -> 22528 MiB), leaving 317 extents (1.24 GiB) free
#   would: grow the ext4 filesystem on /dev/ubuntu-vg/hub-scratch online to fill the LV (resize2fs)
#   would: create /srv/scratch/clips, put it in quota project 7104 (inherit)
#   exit 3 = STOP (the VG cannot give it and keep the reserve): record the numbers, change nothing
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

**6. Clips on.** `CLIPS_ENABLED=true`, then
`sudo systemctl enable --now homehub-clips-cut.timer homehub-clips-retention.timer`.
After the next visitor: `sudo python3 /opt/homehub/stack/clips/homehub_clips.py episodes --camera door`
and `status`, and the clip under `/srv/scratch/clips/door/<day>/` plays.

**7. Re-encode, after a day of clips with both daylight and night in it.** First
the trial, which replaces nothing (the Owner, 2026-10-27):

```sh
sudo python3 /opt/homehub/stack/clips/homehub_clips.py encode-trial --camera door --day <YYYY-MM-DD> --max-clips 4
```

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
(0 = unchanged; e.g. 5). Then `homehub_clips.py encode-trial --clean`, set
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

**Rollback**: the knobs to false; `sudo systemctl disable --now homehub-camera-motion@door homehub-clips-cut.timer homehub-clips-retention.timer homehub-clips-encode.timer`.
The clips area and its clips stay until removed by hand.
