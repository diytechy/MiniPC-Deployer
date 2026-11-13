# camera-remote — the camera page: the hub's cameras from a phone's browser (SN-057)

Implements **SR-170** (the viewer), **SR-171** (the page's gate in Caddy),
**SR-172** (the shared login every camera is behind), **SR-173** (the source
allow-list) and **SR-174** (the straightened door picture). Interface:
**IF-087**. The plan and the rulings are HomeHub
`docs/CAMERAS_REMOTE_VIEW_AND_RECORDING_PLAN_2026-11-11.md` (sections 6, 8.1,
8.2, 8.9) and open-items A89. **Ships off.** Only the viewer part of plan
section 6 is built: no alerts (8.3), no tailnet fence for the cameras
themselves (8.6: written up in HomeHub, not built), no corner camera (it is on
no relay).

## What the Owner ruled (2026-11-12)

- **8.1:** a page on the hub at `cameras.<domain>`, LAN and tailnet only.
  Caddy passes **only** go2rtc's player page, its two scripts and its stream
  websocket, in **MSE** mode (repackaged, never re-encoded), and blocks the
  rest of go2rtc's API.
- **Every camera behind one shared login** (about 19:00: the page sits behind
  Caddy's shared login "as it contains all cameras"; 8.2 answered): the door,
  raw and straightened, and the living room's two relay streams, video only.
- **The door alone stays open on the LAN, without the login**, for the wall
  panel (the earlier ruling, about 18:15: "a public view of the front of a
  house on a closed network"): the door's two streams and the player, to
  `CAMERA_VIEW_DOOR_OPEN_CIDR`, the hub's LAN alone. A tailnet source always
  goes through the login.
- **The projection is knobs** (A65 Part 1, the straightened picture looks
  "stretched a little far horizontally"): `CAMERA_VIEW_FLAT_*`, the panel's
  values by default, tuned on the hub against a shape of known form.
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

- **Up to four streams**, each read from one of the hub's relays with that
  relay's own consumer login (no new camera secret):
  - `livingroom` and `livingroom-main`: the living-room relay's sub and main
    streams, **video only** (`#media=video`: its camera's sound is never asked
    for), served only while `CAMERA_LIVINGROOM_RELAY_ENABLED` is `true` (the
    main stream only while that relay serves one, `_MAIN_PATH`);
  - `corner` and `corner-main`: the corner camera's relay
    (`homehub-camera-relay@corner`, 2026-11-13), the same way, while
    `CAMERA_CORNER_RELAY_ENABLED` is `true`;
  - `door`: the relay's H.264 copied as it is (the raw 1536x1536 circle);
  - `door_flat`: go2rtc's `exec:` source runs FFmpeg in the same image:
    `fps=10,v360=input=fisheye:output=flat:ih_fov=180:iv_fov=180:h_fov=110:v_fov=77.5521:yaw=0:pitch=0:w=960:h=540:interp=linear`
    (the panel's `doorstream/service.py` projection with `wall.env.example`'s
    defaults, which `config.wall.psd1` does not override; these are the
    defaults of the projection knobs below), then
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
  dropped source with its whole URL), which removes every relay's login and
  camera address; the container has no log driver.

## The projection (A65 Part 1)

| Knob | Default (the panel's) | Range |
|---|---|---|
| `CAMERA_VIEW_FLAT_INPUT_FOV` | `180` | the fisheye's field, one value or `HxV` (such as `190x180`), each 1..360 |
| `CAMERA_VIEW_FLAT_HORIZONTAL_FOV` | `110` | the flat view's field across, 1..179; the vertical field is always derived for square pixels |
| `CAMERA_VIEW_FLAT_SIZE` | `960x540` | even, 160..1920 by 90..1080 |
| `CAMERA_VIEW_FLAT_YAW` | `0` | -180..180 |
| `CAMERA_VIEW_FLAT_PITCH` | `0` | -90..90 |
| `CAMERA_VIEW_FLAT_OUTPUT` | `flat` | `flat` or `cylindrical` (the angle across linear) |

The numbers take up to three decimals; anything else is refused by name. Why
the picture can look "stretched a little far horizontally": a flat view 110
degrees across stretches things toward its left and right edges, as any very
wide photo does, and the lens's 180 degrees is nominal. Tuned on the hub, one
knob at a time: a narrower `HORIZONTAL_FOV` (about 90) or `OUTPUT=cylindrical`
for the edges, then `INPUT_FOV` for the lens; after each change restart the
viewer (`systemctl restart homehub-camera-view`; the warm unit follows) and
compare one live frame on the LAN against a shape of known form (HomeHub's
trial runbook). Not `pannini`: FFmpeg's pannini output reads `h_fov` as the
Pannini parameter, with a fixed field and no square-pixel control.

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
{$LAN_CIDR}` (anything else: 403); a rate limit; then **the open door**, with
no login, only for a source inside `CAMERA_VIEW_DOOR_OPEN_CIDR` (empty: compose
hands `0.0.0.0/32`, which no packet comes from):

| Request (source in `CAMERA_VIEW_DOOR_OPEN_CIDR`) | Answer |
|---|---|
| `GET`/`HEAD` `/stream.html`, `/video-stream.js`, `/video-rtc.js` | go2rtc's player |
| `/api/ws` with the raw query **exactly** `src=door` or `src=door_flat` | the stream |

and **everything else behind the shared login** (`basic_auth`; none or a wrong
one: 401), then exactly

| Request | Answer |
|---|---|
| `GET`/`HEAD` `/` | the landing page (`www/index.html`, below) |
| `GET`/`HEAD` `/video-stream.js`, `/video-rtc.js` | go2rtc's player scripts |
| `/api/ws` with the raw query **exactly** `src=` one of `door`, `door_flat`, `livingroom`, `livingroom-main`, `corner`, `corner-main` | the stream |
| anything else (`/stream.html`, the earlier `/door`, `/raw` redirects included) | 403 |

A listed name the viewer does not serve (the living room while its relay is
off) creates nothing in go2rtc (it has no scheme): the player shows that tile's
error and the door still plays. go2rtc's `stream.html` applies one `mode=` to
every `src` it is given.

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

## The landing page (`www/index.html`)

The operator, 2026-11-13: all four views on one page, each shown full screen
on a tap, viewable remotely (over the tailnet, behind the login). One file,
bound read-only into caddy at `/srv/camera-page`, served at `/` behind the
login; no other file and no external URL. Four tiles, each go2rtc's own
player (`video-stream.js`, MSE, video only) on a camera's **small** stream:
the door's raw circle (`door`), the door straightened (`door_flat`), the
living room (`livingroom`), the corner (`corner`). A tap shows that camera
alone on its **main** stream where one exists (`livingroom-main`,
`corner-main`; the door's two have one each), in the browser's full screen
where it allows it and filling the window where it does not (an iPhone's
Safari, for one). The grid is taken out of the page meanwhile, so its four
streams close (the player disconnects 5 s after it leaves the page): one
stream at a time crosses mobile data in that view. The close button, Escape,
Back or leaving full screen returns to the grid. A camera whose relay is off
shows its tile's error ("stream not found") and the others play.

## The open door (`CAMERA_VIEW_DOOR_OPEN_CIDR`)

The sources that may take the door's two streams and the player **without**
the login: the LAN alone (the hub's own LAN network, or narrower, such as the
panel's address). `render` and `check` refuse an entry that is not IPv4, lies
outside the hub's LAN (the tailnet, `100.64.0.0/10`, `private_ranges`, a
broad range, IPv6) or overlaps a docker bridge, by position, never by value;
`check` puts **caddy's own** value through the same rule and compares it with
the `.env`. HomeHub's config sets it to the LAN identity value. Empty: the
door needs the login everywhere.

## The shared login (SR-172)

**Required:** every stream is behind it, so `render` refuses without a real
bcrypt hash (empty, the shipped lock, or anything else). To add a camera: its
relay's settings in `ViewSettings`, its stream in `view_streams`, its name in
`camera_remote.STREAMS` and in the Caddyfile's `@cv_stream` list as
`"src=<name>"` (the tests hold the two lists equal). HomeHub's deploy store
holds the password (`CameraViewPassword`, set or minted) and derives the
bcrypt hash (`CameraViewHash`); the hash reaches a live hub through a pipe:

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
# 1. payload: stack/camera-remote, stack/door (redact_run's redactor), stack/caddy/Caddyfile,
#    stack/docker-compose.yml, stack/provision/provision-technitium.sh -> /opt/homehub/stack
#    (stack/camera-relay must be there too: the viewer reads the living room's settings through it)
sudo install -m 0644 /opt/homehub/stack/camera-remote/homehub-camera-view.service \
                     /opt/homehub/stack/camera-remote/homehub-camera-view-warm.service /etc/systemd/system/
sudo systemctl daemon-reload
# 2. the CAMERA_VIEW_* keys from .env.example into /opt/homehub/stack/.env; CAMERA_VIEW_DOOR_OPEN_CIDR
#    set to the LAN; the login's hash through import-login (above); read LAN_CIDR through the check:
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

- `tests/test_camera_remote.py` (pytest, TC-1825..TC-1838, TC-1845,
  TC-1846): settings and refusals; the allow-list (every refused shape named
  in plan 6.9); the open door (the LAN alone); the config (the living room's
  streams video only, both relays' logins redacted); the FFmpeg argv (no URL,
  the projection knobs with the panel's values by default, default rate
  control); the input document and 0600 files; `import-login`; the login
  required; `check`'s reading of caddy; the Caddyfile, compose, unit and
  template contract; the warm consumer's websocket client against fake
  sockets.
- `tests/camera-view-lab.sh` (root + docker, WSL; TC-1839..TC-1843): a fake
  doorbell, the real door relay, the viewer run from its own unit lines
  through the real compose file, the hub's own Caddy image serving the real
  site block: the open door from the lab's LAN, everything else behind the
  login, and the door behind it too once the open range is emptied. It has
  no living-room relay (its streams are proven by pytest only). It cannot prove the hub's VA-API encode (WSL's render node does
  not take the image's driver): door_flat as shipped is shown to fail cleanly
  there, and a software-encoded build of the same pipeline (a lab-only seam in
  `flat_argv`, reachable by no knob or verb) proves the pipe to MSE.

## Not proven

Anything on the hub: the VA-API encode inside this service (measured on the
hub on 2026-11-12 in the same image, by hand, not through this code), the
CPU of `always`, the bandwidth at `-g 20`, a phone's browser (MSE, the CSP,
the browser's basic-auth prompt and its credentials sent on the websocket),
the living room's streams through a real relay (and whether its main stream
plays over MSE on the phone), the projection knobs on the real door, the
tailnet source address Caddy sees, the Technitium record, systemd on the hub (the two unit files ran
under WSL's systemd 255 in lab check 13: the warm unit pulled in and back
after a viewer restart, its sandbox reaching the socket; `systemctl stop`
leaves the viewer `failed` with compose's exit 130), a reimage (nothing here is in
firstboot: like the relays, the units are installed by hand).
