# camera-relay — a camera's own go2rtc relay (SN-055)

Implements **SR-155** (the relay) and **SR-156** (audio offered on request,
never kept). Interface to MINI-SERV: **IF-082**. The plan is HomeHub
`docs/LIVING_ROOM_PRESENCE_PLAN_2026-11-09.md`.

The door relay ([`../door/README.md`](../door/README.md)) holds the doorbell's
one connection. A second camera gets **its own go2rtc process**, not a stream in
the door relay:

- go2rtc's RTSP listener has **one consumer credential**. The door relay's is
  the doorbell's own. A second camera in that process would be read with the
  doorbell's password, or would need a new shared secret and a change to the
  panel's door broker. Here each relay serves its camera with **that camera's
  own login**, as the door relay does, so no new secret is created.
- Adding a stream to the door relay restarts it, which takes the panel's door
  view down. A separate process never touches it.
- The door relay is video-only by design. This one can pass audio through.

| Piece | Where | Switch |
|---|---|---|
| relay (the door relay's pinned go2rtc image) | compose `camera-relay-<camera>`, run by `homehub-camera-relay@<camera>.service` | `CAMERA_<C>_RELAY_ENABLED` |
| config, redaction, measurement | `camera_relay.py` (`render`, `redact-run`, `streams`, `upstream-count`, `probe`, `audio-check`, `cost`) | — |

Everything that touches the credential is the door relay's code, reused rather
than copied: `redact_run`/`redact` for the unit's output, `relay_consumer` for
every consumer (the URL only in an ffconcat document on stdin), `probe`,
`streams`, `upstream_count` and `cost`.

## The living-room camera

| Item | Value |
|---|---|
| Upstream | the sub stream, `/h264/ch1/sub/av_stream` on 554 (768x432, ~10 fps). User `admin`; the password is the camera's verification code in **upper case** (RTSP is case-sensitive). Host T1 and code T3, from HomeHub's config and deploy store |
| Listeners | RTSP on `LAN_IP:8564`, API on `127.0.0.1:1994`, both behind the camera's login (`local_auth`); WebRTC, SRTP and RTMP off; no Caddy route, no DNS name |
| Stream | `livingroom` (camera names are knob names: `[a-z][a-z0-9]{0,15}`) |
| Audio | `CAMERA_LIVINGROOM_RELAY_AUDIO=true`: the source has no media filter, and `default_query: video`. A consumer gets the AAC track only by asking (`/livingroom?video&audio`). No hub consumer asks, and each also passes `-an` |
| Recording | **none**. The camera is not in `CAMERAS` (motion clips), and no recorder exists for it until the Owner rules on training capture (HomeHub open-items A81) |

## Deploy steps (the coordinator's)

The camera's code must be in the deploy store first (HomeHub
`PrepDeploySecrets.ps1 -Only <the key named by @identity:livingroomcam.PasswordStoreKey>`).
`deploy_stack.py` never writes `.env`, so on the live hub the keys go in by hand,
and the code goes in **through a pipe, never argv**.

```sh
# 1. payload: stack/camera-relay, stack/door (unchanged), the compose file -> /opt/homehub/stack
sudo install -m 0644 /opt/homehub/stack/camera-relay/homehub-camera-relay@.service /etc/systemd/system/
sudo systemctl daemon-reload
# 2. the CAMERA_LIVINGROOM_RELAY_* keys from .env.example into /opt/homehub/stack/.env, UPSTREAM_HOST set;
#    then the code, piped from the dev PC's deploy store (never argv, never printed):
#      <store value> | ssh hub@<hub> 'sudo -n python3 /opt/homehub/stack/camera-relay/camera_relay.py \
#          --camera livingroom import-password'
#    "wrote CAMERA_LIVINGROOM_RELAY_UPSTREAM_PASSWORD into /opt/homehub/stack/.env (value not shown)"
# 3. render and start
sudo python3 /opt/homehub/stack/camera-relay/camera_relay.py --camera livingroom render
sudo systemctl enable --now homehub-camera-relay@livingroom
# 4. prove it
sudo python3 /opt/homehub/stack/camera-relay/camera_relay.py --camera livingroom probe        # h264 768x432
sudo python3 /opt/homehub/stack/camera-relay/camera_relay.py --camera livingroom audio-check  # pass: true
sudo python3 /opt/homehub/stack/camera-relay/camera_relay.py --camera livingroom upstream-count  # 1
journalctl -u homehub-camera-relay@livingroom -n 50   # no code, no camera address
```

## Tests

`tests/test_camera_relay.py` (pytest, TC-1730..TC-1735): settings and refusals,
the rendered config, the audio switch, the probes' argv and stdin, the audio
verdict, redaction, and the unit/compose/template contract. What is not
covered here is the real relay with the real camera: that is step 4 above.
