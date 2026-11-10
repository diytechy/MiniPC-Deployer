# Camera training capture (SR-162)

A bounded, copy-only recording of one camera's streams onto the media drive, to
build a training set. First use: the living-room camera, both streams, three
days from 2026-11-10 (the Owner's ruling of HomeHub open-items A81).
`camera_capture.py`'s docstring has the design; this page is how to run it.

| | |
|---|---|
| Units | `homehub-camera-capture@livingroom-main.service`, `@livingroom-sub.service` |
| Source | the camera's relay: `/livingroom` (sub, 768x432) and `/livingroom-main` (1920x1080, needs `CAMERA_LIVINGROOM_RELAY_MAIN_PATH`) |
| Files | `/srv/media/Training/livingroom/<stream>/YYYY-MM-DD/livingroom-<stream>-YYYYmmddTHHMMSSZ.ts`, 10-minute segments, UTC names |
| Access | root only (`root:root 0750`, no ACL, on no share) |
| Budget | quota project 7106, hard limit `CAMERA_LIVINGROOM_CAPTURE_QUOTA_MIB` (400 GiB) for both streams |
| Never | audio (`-an`, and no consumer asks), decoding, anything off the hub |

## Start a capture

```sh
cd /opt/homehub/stack
sudo python3 camera-capture/camera_capture.py --camera livingroom ensure        # tree, project, limit
# in .env: CAMERA_LIVINGROOM_RELAY_MAIN_PATH, CAMERA_LIVINGROOM_CAPTURE_ENABLED=true,
#          CAMERA_LIVINGROOM_CAPTURE_UNTIL=<UTC end>
sudo systemctl restart homehub-camera-relay@livingroom                          # serves -main
sudo install -m 0644 camera-capture/homehub-camera-capture@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now homehub-camera-capture@livingroom-main homehub-camera-capture@livingroom-sub
```

## Watch it

```sh
sudo python3 camera-capture/camera_capture.py --camera livingroom status
journalctl -u 'homehub-camera-capture@*' --no-pager | tail -20
sudo python3 door/door_recorder.py verify /srv/media/Training/livingroom/main/<day>/<file>.ts
```

`status` gives each stream's segment count, bytes, first and last segment, the
open `.part`, and the project's use against its limit.

## When it ends

At `CAPTURE_UNTIL` each unit closes its segment and exits 0; a reboot after that
starts nothing. Then: `systemctl disable` both units and set
`CAMERA_LIVINGROOM_CAPTURE_ENABLED=false`. The recordings stay until someone
deletes them: `sudo rm -r /srv/media/Training/livingroom/<stream>/<day>`.

## Not in the image

No firstboot step installs the unit or creates the tree, on purpose: a capture
is a decision with an end date, not a standing service. A reimage brings back
the code (it is in `stack/`) and the knobs default to off.
