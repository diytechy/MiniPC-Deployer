# camera-remote — the camera page: the door camera from a phone's browser (SN-057)

Implements **SR-170** (the viewer), **SR-171** (the page's gate in Caddy),
**SR-172** (the shared login, kept for later), **SR-173** (the source
allow-list) and **SR-174** (the straightened door picture). Interface:
**IF-087**. The plan and the rulings are HomeHub
`docs/CAMERAS_REMOTE_VIEW_AND_RECORDING_PLAN_2026-11-11.md` (sections 6, 8.1,
8.2, 8.9) and open-items A89. **Ships off.** Only the viewer part of plan
section 6 is built: no alerts (8.3), no tailnet fence (8.6), no indoor camera
(8.2).

## What the Owner ruled (2026-11-12)

- **8.1:** a page on the hub at `cameras.<domain>`, LAN and tailnet only.
  Caddy passes **only** go2rtc's player page, its two scripts and its stream
  websocket, in **MSE** mode (repackaged, never re-encoded), and blocks the
  rest of go2rtc's API.
- **The door needs no login** (about 18:15: "a public view of the front of a
  house on a closed network, so anyone on the network should be able to see
  it"). Any source the allow-list admits may watch `door` and `door_flat`.
  Caddy's own username-and-password prompt with **one shared login** stays
  wired for streams other than the door's; **none ships** (indoor cameras wait
  on 8.2).
- **8.9:** the hub makes **one straightened door picture**, the wall panel's
  projection, decoded and straightened on the processor and H.264-encoded on
  the graphics.
- **Its mode is a knob, `always` first** (about 18:25: "an .env configuration
  ... start evaluations with always on since I do notice a delay").
- **Default encoder quality, no rate cap** (about 18:45; measured on the real
  door at evening light: 0.35 Mbit/s straightened, 0.71 Mbit/s for the raw
  copy; HomeHub plan 1.5).

## The pieces

| Piece | Where | Switch |
|---|---|---|
| viewer: a third go2rtc (the relays' pinned image) | compose `camera-view`, run by `homehub-camera-view.service` | `CAMERA_VIEW_ENABLED` |
| keeps `door_flat` running | `homehub-camera-view-warm.service` (`keep-warm`) | `CAMERA_VIEW_DOOR_FLAT_MODE=always` (the default) |
| the page's gate | Caddy's `{$CAMERA_VIEW_HOST}` site (`stack/caddy/Caddyfile`) | caddy's own copy of `CAMERA_VIEW_ENABLED` |
| the name | Technitium A record `cameras.<domain>` -> `LAN_IP` (`provision-technitium.sh`) | `CAMERA_VIEW_ENABLED` |
| config, checks, the warm consumer | `camera_remote.py` (`condition`, `condition-warm`, `render`, `check`, `keep-warm`, `import-login`, `redact-run`) | — |

## The viewer

- **Two streams**, both read from the hub's door relay with the relay's own
  consumer login (the doorbell's; no new camera secret):
  - `door`: the relay's H.264 copied as it is (the raw 1536x1536 circle);
  - `door_flat`: go2rtc's `exec:` source runs FFmpeg in the same image:
    `fps=10,v360=input=fisheye:output=flat:ih_fov=180:iv_fov=180:h_fov=110:v_fov=77.5521:yaw=0:pitch=0:w=960:h=540:interp=linear`
    (the panel's `doorstream/service.py` projection with `wall.env.example`'s
    defaults, which `config.wall.psd1` does not override), then
    `format=nv12,hwupload` and `h264_vaapi` on `/dev/dri/renderD128`, MPEG-TS
    to stdout, which go2rtc reads as a pipe. Rate control is the encoder's
    default (the ruling). Two settings differ from the encoder's defaults, both
    for joining a live stream: a keyframe every 2 s (`-g 20`; the default 120
    frames would make a viewer wait up to 12 s) and no B-frames (`-bf 0`).
    **Neither was part of the hub's measurement**; the shorter keyframe
    interval raises the bitrate somewhat.
- **No listener at all.** The API is a unix socket,
  `/var/lib/homehub-camera-view/api.sock` (the unit's StateDirectory, 0700
  root), which Caddy binds read-only; RTSP, WebRTC, SRTP, RTMP and the API's
  TCP port are off. go2rtc is cut down twice (read from the pinned 1.9.14's
  source: `main.go`'s module table, `internal/api/api.go`): `app.modules`
  loads only `api`, `ws`, `rtsp` (the client), `mp4` (the MSE handler) and
  `exec`; `api.allow_paths` registers only `/` (the static player files) and
  `/api/ws`, so `/api/streams`, `/api/config`, `/api/preload` and the frame
  and mp4 endpoints do not exist. `exec.allow_paths` admits only `ffmpeg`,
  and go2rtc refuses an `exec:` source arriving through the API anyway (it is
  marked insecure). No WebRTC handler is loaded, so the page can only play MSE.
- **The relay URL never reaches an argv.** `door`'s source is the relay URL in
  the rendered config. `door_flat`'s FFmpeg cannot read its input from stdin
  (go2rtc's exec pipe mode owns only stdout), so its input is the door relay's
  own ffconcat document (`door_relay.relay_input_document`, the builder every
  relay consumer uses), in a file beside the config: both 0600, in the unit's
  RuntimeDirectory (RAM, 0700 root, removed when the unit stops), bound
  read-only. The argv names only `/config/door.ffconcat`. That file is the one
  difference from the relays' stdin rule, and it holds nothing the config
  beside it does not already hold.
- **Output** reaches the journal only through `redact-run` (go2rtc logs a
  dropped source with its whole URL); the container has no log driver.

## The two modes of `door_flat`

| `CAMERA_VIEW_DOOR_FLAT_MODE` | What runs | Cost |
|---|---|---|
| `always` (default) | `homehub-camera-view-warm.service` is a consumer of `door_flat` on the viewer's own socket that never leaves and reconnects (backoff 2, 5, 10, 30, 60 s, reset after a minute of video). The relay session and the FFmpeg run continuously; a page gets the picture at its next keyframe (at most 2 s). | about 30 % of one hub core, all the time (plan 1.5, real stream) |
| `on_demand` | go2rtc starts the producer at the first viewer and stops it after the last. | the same while watched, nothing otherwise; the first picture waits for the relay session and FFmpeg |

go2rtc's own `preload` was not used for `always`: a preload whose first dial
fails (the relay still starting at boot, say) is dropped for good
(`internal/streams/preload.go` AddPreload returns the error once).

**Later, not built:** feeding `door_flat` from `homehub-camera-motion@door`'s
own decoder (about 15 % of a core instead of 30 %); HomeHub plan section 6
describes what that merge takes.

## The gate (Caddy)

In order: the switch at request time (off: 404 to everything); `@lan remote_ip
{$LAN_CIDR}` (anything else: 403); a rate limit; then exactly

| Request | Answer |
|---|---|
| `/` | 302 to `/stream.html?src=door_flat&mode=mse` |
| `/raw` | 302 to `/stream.html?src=door&mode=mse` |
| `GET`/`HEAD` `/stream.html`, `/video-stream.js`, `/video-rtc.js` | go2rtc's player |
| `/api/ws` with the raw query **exactly** `src=door` or `src=door_flat` | the stream, no login |
| `/api/ws` with the query exactly `src=<a stream behind the login>` | basic_auth, then the stream (none ships: the list is empty) |
| anything else | 403 |

**Why exactly one canonical `src`:** go2rtc's handler takes `src`, and a
`src` that is not a stream name **creates** a stream from it (an `rtsp://`
URL is accepted), and with `name=` it **replaces** an existing stream's source
(`internal/streams` `GetOrPatch`, `Patch`). Absent, repeated, percent-encoded,
foreign or extra keys are all 403. No method matcher on the websocket: a
browser may open it over HTTP/2 as an extended CONNECT.

The browser's `Authorization` and cookies are stripped before the viewer.
Headers: a CSP that keeps the page to its own origin (the player's icon links
to go2rtc.org are blocked), `nosniff`, no referrer, `no-store`, HSTS.

## The source allow-list (SR-173)

`render` and `check` refuse unless every entry of `LAN_CIDR` (as Caddy reads
it: whitespace-separated, `private_ranges` expanded) lies inside **the hub's
LAN** (the network on the interface holding `LAN_IP`, from `ip -j addr`),
**`100.64.0.0/10`** or a range the Owner **ruled in by name**
(`CAMERA_VIEW_SOURCES_RULED_IN="NAME=CIDR ..."`), and none overlaps a docker
bridge on the hub. `private_ranges`, a broad `10.0.0.0/8` or
`192.168.0.0/16`, a bridge spelled out and **IPv6 are refused as shipped**.
A ruled-in IPv4 range must be private and a /24 or narrower (the WireGuard
range, if the Owner rules it in); a ruled-in IPv6 range must lie inside
Tailscale's `fd7a:115c:a1e0::/48`. Messages name entries by position, never
by value. `check` puts **caddy's own** `LAN_CIDR` (read with `docker
inspect`, every other variable dropped) through the same list.

## The shared login (SR-172), kept for later

Not needed by the door. When a stream other than the door's is added (after
8.2): its name goes in `camera_remote.LOGIN_STREAMS` and in the Caddyfile's
`@cv_login_stream` list as `"src=<name>"`; HomeHub's deploy store holds the
password (`CameraViewPassword`, optional) and derives the bcrypt hash
(`CameraViewHash`); the hash reaches a live hub through a pipe:

```sh
# on the dev PC (the hash only; the password never leaves the store):
#   Show-DeploySecret.ps1 -Key CameraViewHash | ssh hub@<hub> 'sudo -n python3 /opt/homehub/stack/camera-remote/camera_remote.py import-login'
# "wrote CAMERA_VIEW_AUTH_HASH into /opt/homehub/stack/.env (value not shown)"
```

`import-login` doubles each `$` for compose. **Never hand-edit the hash:** an
undoubled `$` breaks compose's interpolation, and a value Caddy cannot decode
stops it loading every site. Unset, compose hands Caddy `bG9ja2Vk` (the base64
of "locked"), which is no bcrypt hash, so every attempt is a 401.

## Deploy steps (the Owner's trial; nothing here has run on the hub)

HomeHub's runbook (`docs/CAMERAS_PAGE_TRIAL_RUNBOOK_2026-11-12.md`) is the
procedure with its checks; in short:

```sh
# 1. payload: stack/camera-remote, stack/door (one function added), stack/caddy/Caddyfile,
#    stack/docker-compose.yml, stack/provision/provision-technitium.sh -> /opt/homehub/stack
sudo install -m 0644 /opt/homehub/stack/camera-remote/homehub-camera-view.service \
                     /opt/homehub/stack/camera-remote/homehub-camera-view-warm.service /etc/systemd/system/
sudo systemctl daemon-reload
# 2. the CAMERA_VIEW_* keys from .env.example into /opt/homehub/stack/.env; read LAN_CIDR through the check:
sudo python3 /opt/homehub/stack/camera-remote/camera_remote.py check     # must name nothing about LAN_CIDR
# 3. switch on: CAMERA_VIEW_ENABLED=true; then caddy (recreated, so it reads its new environment), the
#    name, the viewer and the warm unit:
cd /opt/homehub/stack && sudo docker compose up -d caddy
sudo bash /opt/homehub/stack/provision/provision-technitium.sh
sudo systemctl enable --now homehub-camera-view homehub-camera-view-warm
sudo python3 /opt/homehub/stack/camera-remote/camera_remote.py check     # pass: true
sudo python3 /opt/homehub/stack/door/door_relay.py streams               # consumers: the panel, the recorder, the detector, +1 (always)
journalctl -u homehub-camera-view -u homehub-camera-view-warm -n 50     # no credential, no camera address
```

**Off again:** `CAMERA_VIEW_ENABLED=false`, `systemctl disable --now
homehub-camera-view-warm homehub-camera-view`, `docker compose up -d caddy`
(the site answers 404), and `provision-technitium.sh` (the record is no
longer added; an existing one may need deleting in the console).

Delivering the Caddyfile makes Caddy request a certificate for
`cameras.<domain>` at once, even with the page off (as OtterTube's site does).

## Tests

- `tests/test_camera_remote.py` (pytest, TC-1825..TC-1838): settings and
  refusals; the allow-list (every refused shape named in plan 6.9); the
  config; the FFmpeg argv (no URL, the panel's projection, default rate
  control); the input document and 0600 files; `import-login`; the login only
  while a stream is behind it; `check`'s reading of caddy; the Caddyfile,
  compose, unit and template contract; the warm consumer's websocket client
  against fake sockets.
- `tests/camera-view-lab.sh` (root + docker, WSL; TC-1839..TC-1843): a fake
  doorbell, the real door relay, the viewer run from its own unit lines
  through the real compose file, the hub's own Caddy image serving the real
  site block. It cannot prove the hub's VA-API encode (WSL's render node does
  not take the image's driver): door_flat as shipped is shown to fail cleanly
  there, and a software-encoded build of the same pipeline (a lab-only seam in
  `flat_argv`, reachable by no knob or verb) proves the pipe to MSE.

## Not proven

Anything on the hub: the VA-API encode inside this service (measured on the
hub on 2026-11-12 in the same image, by hand, not through this code), the
CPU of `always`, the bandwidth at `-g 20`, a phone's browser (MSE, the CSP,
basic auth over a websocket for a later login stream), the tailnet source
address Caddy sees, the Technitium record, a reimage (nothing here is in
firstboot: like the relays, the units are installed by hand).
