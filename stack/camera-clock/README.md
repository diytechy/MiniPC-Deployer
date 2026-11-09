# camera-clock — set a cloud-less camera's clock from the hub (SN-055)

Implements **SR-161**. The plan is HomeHub `docs/LIVING_ROOM_PRESENCE_PLAN_2026-11-09.md`.

The living-room camera (an EZVIZ, Hikvision family) takes its time only from
its vendor's cloud. The router blocks the camera's internet, so after every
power cut its clock counts from **1970**, and that date is burned into every
frame. Measured 2026-11-09: after power-up it asks DNS for its cloud login
server and nothing else, and sends no NTP, so the hub cannot serve it time by
the usual route.

It does accept a login on its **device SDK port (8000)** with its own username
and verification code, and it keeps a time set there until the next power cut.
`homehub-camera-clock@<camera>.timer` therefore runs `camera_clock.py sync`
two minutes after boot and every ten minutes after that. Each pass reads the
camera's clock and sets it to the hub's local time when the year is before 2024
or the camera is off by more than `MAX_DRIFT_SEC`. **Nothing depends on the
vendor's cloud.**

| Piece | What |
|---|---|
| `camera_clock.py` | `condition`, `check`, `sync`, `read` |
| `homehub-camera-clock@.service` / `.timer` | one pass, every 10 min |
| `sdk.manifest.json` | the HCNetSDK release: its URL, the zip's sha256, every library file's sha256 |
| `install-camera-clock-sdk.sh` | zip -> `/opt/homehub-camera-clock/sdk/lib`, verified, swapped in whole |

**The SDK is Hikvision's Device Network SDK for Linux 64 (HCNetSDK)**, which is
proprietary and is **not in this repository**. The run refuses (78) any bytes but
the pinned ones, and any unpinned file beside them. Hikvision's CDN answers 403
unless the download page is sent as the `Referer`; the manifest records both.

**The vendor binary is confined.** It runs as root only so it can read the
root-only `.env`, and it has:

- no capabilities;
- a read-only system with one state directory;
- no devices;
- IP access to loopback and private (RFC 1918) addresses only, so it cannot
  reach the internet;
- its own file log closed, so nothing of the camera's is written.

The credential is the camera relay's. It is handed to the library in memory
and never appears in argv, the environment or a log.

**Facts measured 2026-11-09 with the real SDK and camera:**

- The login works in the private mode.
- A fresh power-up reads `1970-01-01 00:20:06` (the clock counts from zero).
- A set reads back within a second, and the overlay follows it.
- A date the camera considers out of range (2000-01-01) is refused with SDK
  error 11, "transferred data has error".
- A clock knocked an hour off was put back by one `sync`.

## Deploy steps (the coordinator's)

```sh
# 1. the zip (from the manifest's URL, with its Referer) onto the hub's /tmp, then:
sudo bash /opt/homehub/stack/camera-clock/install-camera-clock-sdk.sh --zip /tmp/EN-HCNetSDKV6.1.11.40_build20260922_linux64.zip
# 2. the CAMERA_LIVINGROOM_CLOCK_* keys into .env (the relay's credential is already there)
sudo python3 /opt/homehub/stack/camera-clock/camera_clock.py --camera livingroom check
sudo python3 /opt/homehub/stack/camera-clock/camera_clock.py --camera livingroom read
# 3. units, knob true, timer on
sudo install -m 0644 /opt/homehub/stack/camera-clock/homehub-camera-clock@.service /opt/homehub/stack/camera-clock/homehub-camera-clock@.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now homehub-camera-clock@livingroom.timer
sudo systemctl start homehub-camera-clock@livingroom.service; journalctl -t homehub-camera-clock -n 5
cat /var/lib/homehub-camera-clock/livingroom.json
```

## Tests

`tests/test_camera_clock.py` (pytest, TC-1748..TC-1756) uses a fake SDK. The
real library and camera are covered by the deploy steps above.
