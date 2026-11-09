# camera-presence — room presence from a relayed camera (SN-055)

Implements **SR-157..SR-160**. Interface to MINI-SERV: **IF-081**. The plan is
HomeHub `docs/LIVING_ROOM_PRESENCE_PLAN_2026-11-09.md`, section 4.

`homehub-camera-presence@<camera>` reads **one frame a second** from the
camera's own relay ([`../camera-relay/README.md`](../camera-relay/README.md)),
**video only**. It decides whether someone is in the room **outside the excluded
zones** (the treadmill, and the TV, whose picture moves), and publishes **state
changes only**. It never writes a frame, stores no identity, and its feed,
status and journal never carry a picture, a box or a zone.

## How it decides

| Step | What |
|---|---|
| Crop | the room polygon's bounding box, padded 4 %, from a 640x360 decode |
| Face | `faceyolo` (single-class YOLOv8n), confidence 0.15: the panel study's operating point |
| Body | `yolov8n` person, confidence 0.40, only when no face counted (`cascade`; or `always`, `off`) |
| Counts | a detection whose anchor (face: centre; body: bottom centre) is in the room and in no exclude polygon |
| On | 2 positive seconds of the last 3 |
| Off | `HOLD_SEC` (8) with no positive. Any positive refreshes the hold; room motion refreshes it while present and never turns presence on |
| Engage | anchors in the engage polygon (the couch) for `ENGAGE_SEC` (20) |

**Draw the exclude polygon around where a person ON the treadmill appears**,
head to feet. Both the face centre and the body's feet must fall inside it.

Measured on the hub, 2026-11-09 (onnxruntime 1.20.1, one thread, random input):
faceyolo 320 = 204 ms, 192 = 74 ms; yolov8n 256 = 143 ms, 320 = 221 ms. Face
320 plus body 256 in an empty room is about 35 % of one core, which is over the
30 % budget. **The cost run decides the sizes**: `cascade` spends less while
someone faces the camera, and 192 may be enough once the crop is known.

## Output

The feed (IF-081): JSON lines on `LAN_IP:CAMERA_<C>_PRESENCE_FEED_PORT` (0 =
off), only from `CAMERA_<C>_PRESENCE_FEED_PEERS` (default `MAIN_BOX_IP`):
`hello`, then the current state, then each change, plus a beat every 5 s:

```
{"v":1,"type":"state","event":"living_room.presence","present":true,"zone":"room","since":"2026-11-09T20:14:03Z","confidence":0.62}
```

The status file is `/var/lib/homehub-presence/<camera>/status.json`. Journal:
`journalctl -t homehub-camera-presence`, one line per change.

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
# 3. the CAMERA_LIVINGROOM_PRESENCE_* keys into .env (polygons from HomeHub config.homehub.psd1)
sudo /opt/homehub-presence/venv/bin/python /opt/homehub/stack/camera-presence/camera_presence.py --camera livingroom check
# 4. switch on, then the declared cost test (30 min, the room in normal use)
sudo install -m 0644 /opt/homehub/stack/camera-presence/homehub-camera-presence@.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now homehub-camera-presence@livingroom
sudo /opt/homehub-presence/venv/bin/python /opt/homehub/stack/camera-presence/camera_presence.py --camera livingroom cost --seconds 1800
# 5. the live proof with the operator (plan section 6): entry; 3+ min still on the couch; leaving; the treadmill
journalctl -t homehub-camera-presence -f
```

## Tests

`tests/test_camera_presence.py` (pytest, TC-1736..TC-1747): zones, settings,
the model pin, the YOLO decode and letterbox, anchors, the state machine
against the plan's targets, engage, the cascade, the feed, the decoder's argv,
the unit and template, and the refusal before any decode. Not covered:
onnxruntime, the models and the camera, which are steps 1 to 5.
