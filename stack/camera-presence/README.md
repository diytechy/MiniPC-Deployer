# camera-presence — room presence from a relayed camera (SN-055)

Implements **SR-157..SR-160** (LLR-1496..1511, LLR-1524..1528). Interface to MINI-SERV: **IF-081**. The plan is
HomeHub `docs/LIVING_ROOM_PRESENCE_PLAN_2026-11-09.md`, section 4.

`homehub-camera-presence@<camera>` reads **one frame every two seconds** from
the camera's own relay ([`../camera-relay/README.md`](../camera-relay/README.md)),
**video only**. It decides whether someone is in the room **outside the excluded
zones** (the treadmill, and the TV, whose picture moves), and publishes **state
changes only**. It never writes a frame, stores no identity, and its feed,
status and journal never carry a picture, a box or a zone.

The defaults are the settings the Owner adopted on 2026-11-12 (HomeHub
`DECISIONS_RATIFIED.md`, "Ratified 2026-11-12", A-2: hold 30 s plus the couch
check), measured offline over two recorded days by HomeHub `scripts/livingroom/`.
`holds.py` is that build's file, moved unchanged.

## How it decides

| Step | What |
|---|---|
| Sample | `FPS` (0.5): one 640x360 frame every 2 s (the Owner's 2026-11-11 ruling: at most every 2 s) |
| Dark | a frame whose mean grey is below `DARK_LEVEL` (8) is skipped, not read as "nobody" (the camera's black frames while it switches to infrared) |
| Crop | the room polygon's bounding box, padded 4 % |
| Face | `faceyolo_320` (single-class YOLOv8n), confidence 0.15: the panel study's operating point |
| Body | `yolov8n_320` person, confidence 0.25, only when no face counted (`cascade`; or `always`, `off`) |
| Counts | a detection whose anchor (face: centre; body: bottom centre) is in the room and in no exclude polygon |
| On | 2 positive samples of the last 3 |
| Off | `HOLD_SEC` (30) with no positive. Any positive refreshes the hold; room motion refreshes it while present, for at most `MOTION_MAX_SEC` (60) past the last real detection, and never turns presence on. A frame whose whole picture changed by `WHOLE_MOTION_MAX` (0.5) or more (a light switched) carries no motion |
| Couch check | while present and nothing has counted for `COUCH_AFTER_SEC` (6): the newest keyframe of the relay's `/<stream>-main` (video only, keyframes only, about one every 4 s), cropped to `COUCH_CROP` and scaled to 960 wide, read by `COUCH_MODEL` (`yolov8n_640`) at `COUCH_CONF` (0.25), once per keyframe. A body that counts in the room zones refreshes the hold; one whose box reaches `COUCH_MAX_BOTTOM` (0.97, cut off next to the lens) is ignored. No cap (`COUCH_MAX_SEC` 0). It never turns presence on. `COUCH_CROP` is private like the zones; empty switches the check off |
| Engage | anchors in the engage polygon (the couch) for `ENGAGE_SEC` (20) |

**Draw the exclude polygon around where a person ON the treadmill appears**,
head to feet. Both the face centre and the body's feet must fall inside it.

Measured on the hub (Celeron J4115), 2026-11-13 (onnxruntime 1.20.1, one thread,
random input; per read): faceyolo 320 = 208 ms, 192 = 74 ms; yolov8n 256 =
145 ms, 320 = 223 ms, 448 = 447 ms, 640 = 934 ms. At 0.5 fps an empty room costs
face + body every 2 s (about 22 % of one core before the decoders); a person
facing the camera, face only (about 10 %); a lying spell, the room's 22 % plus
one 640 read per keyframe (about 23 %).

**Measured live, 2026-11-13** (`cost`, the room empty, so face and body every
sample and the couch check never asked): detector 22.5 %, room decoder 2.3 %,
couch decoder 2.4 %: **27.2 % of one core** against the 30 % budget, the hub's
PSI within 0.3 of its baseline. The unit's quota period is 1 s
(`CPUQuotaPeriodSec`): at systemd's 100 ms every inference was throttled to
half speed and the unit's own throttling raised the hub's PSI by 7 points. A
lying spell (the couch check running) costs about 45-50 % while it lasts; not
measured live. Details: HomeHub `docs/LIVING_ROOM_PRESENCE_LIVE_2026-11-13.md`.

## Output

The feed (IF-081): JSON lines on `LAN_IP:CAMERA_<C>_PRESENCE_FEED_PORT` (0 =
off), only from `CAMERA_<C>_PRESENCE_FEED_PEERS` (default `MAIN_BOX_IP`):
`hello`, then the current state (only once three frames have been judged), then each change, plus a beat every 5 s. A beat whose `frame_age_ms` exceeds 10000 means the state is unknown:

```
{"v":1,"type":"state","event":"living_room.presence","present":true,"zone":"room","since":"2026-11-09T20:14:03Z","confidence":0.62}
```

The status file is `/var/lib/homehub-presence/<camera>/status.json` (with the
couch check's counts: samples that wanted it, hits, model reads, keyframes
missing, decoder restarts; counts only). Journal: `journalctl -t
homehub-camera-presence`, one line per change.

**State, not action.** Nothing here wakes, switches or tells anything to act;
MINI-SERV decides what presence means (HomeHub open-items A88 Part 2, the wake
rules, is not ruled). No face identity is computed at all.

## The runtime

OpenCV 4.6 (apt) cannot load these graphs, so the detector runs on
`/opt/homehub-presence/venv`. `install-presence-runtime.sh` builds it from the
panel's reviewed, hash-pinned sensor wheelhouse
(`../autoinstall/wall/build-sensor-wheelhouse.sh`), with `--system-site-packages`
for the apt `python3-opencv`. The models are **not committed**: `models.manifest.json`
pins each one's sha256, checked at install and at every start.

## Deploy steps (the coordinator's)

```sh
# 0. the relay is up (../camera-relay/README.md)
sudo apt-get install --no-install-recommends python3.12-venv python3-opencv
# 1. the wheelhouse (built in WSL, REPRODUCE mode) and the model bundle onto the hub's /tmp
sudo bash /opt/homehub/stack/camera-presence/install-presence-runtime.sh --wheelhouse /tmp/wh --models /tmp/models
# 2. one still to draw the zones on WITH THE OWNER; delete it afterwards
sudo python3 /opt/homehub/stack/camera-presence/camera_presence.py --camera livingroom still --out /root/lr-still.jpg
# 3. the CAMERA_LIVINGROOM_PRESENCE_* keys into .env (polygons and COUCH_CROP from HomeHub
#    config.homehub.psd1); the couch check needs CAMERA_LIVINGROOM_RELAY_MAIN_PATH (the relay's -main)
sudo /opt/homehub-presence/venv/bin/python /opt/homehub/stack/camera-presence/camera_presence.py --camera livingroom check
# 4. switch on, then the declared cost test (30 min, the room in normal use)
sudo install -m 0644 /opt/homehub/stack/camera-presence/homehub-camera-presence@.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now homehub-camera-presence@livingroom
sudo /opt/homehub-presence/venv/bin/python /opt/homehub/stack/camera-presence/camera_presence.py --camera livingroom cost --seconds 1800
# 5. the live proof with the operator (plan section 6): entry; 3+ min still on the couch; leaving; the treadmill
journalctl -t homehub-camera-presence -f
```

## Tests

`tests/test_camera_presence.py` (pytest, TC-1736..TC-1747, TC-1764..TC-1772):
zones, settings, the model pin, the YOLO decode and letterbox, anchors, the
state machine against the plan's targets, engage, the cascade, the feed, the
decoders' argv, the unit and template, the refusal before any decode; the
2026-11-12 settings: the couch crop, dark frames, whole-frame motion, the
couch check (held lying, never turning presence on, the bottom edge, one read
per keyframe, the reader's restart), and the cost verdict with the couch
decoder. Not covered: onnxruntime, the models and the camera, which are steps 1
to 5.

**The replay of the recorded days** (HomeHub `scripts/livingroom/port_check.py`)
drives this module's own `RoomJudge`, `Evaluator` and `CouchCheck` over the
offline build's cached detections and compares every presence change with the
offline replay's: identical on both recorded days (2026-11-13, 94 and 43
changes). It does not exercise the models, the live decoders, or the dark-frame
and whole-frame gates (neither changed a verdict on those days).
