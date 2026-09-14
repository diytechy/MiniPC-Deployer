# Panel provisioning: protected access, camera, sensors, door motion

Group C deliverable C1, 2026-09-13. This is the procedure for turning the four
unprovisioned capabilities on, written so that a re-image reproduces it.

**Nothing in this document has been executed.** Every step is for the Owner or
the coordinator to run. Steps marked **[Owner present]** cannot be delegated to
a session at all: they either type a secret or look at the physical world.

Every step is reversible, and the reversal is stated with it.

Read alongside:

* `stack/panel-access/README.md` — the hub-side cutover and the existing
  panel-preparation section this document extends.
* `OfficeWallNaglight/gateway/README.md` — the gateway's own runbook.
* `stack/autoinstall/wall/wall.env.example` — every knob named below.

---

## 0. What is actually true on the panel today

Read live, read-only, 2026-09-13 evening:

| Fact | Evidence |
|---|---|
| `/etc/wall-panel/host.json` is `{"enabled": false, "rendererConfig": {…}}` | keys only: `FEED_TOKEN`, `HEARTBEAT_URL`, `SUBSONIC` |
| `kiosk.env` has `WALL_CAMERA_ENABLED=false` | file is generated; the source is `wall.env` |
| `wall-sensors.service` is **not installed** | absent from `systemctl list-unit-files 'wall-*'` (23 units listed) |
| `wall-door-stream.service` is **active** | `systemctl is-active` |
| The door credentials **do exist** — all 25 of them | `/run/wall-door-credentials/` listing, root-only 0600 |
| Door motion is **off at its own switch**, not missing | `motion-enabled=false`, `motion-calibrated=false`; every other `motion-*` value is the documented default |

**Correction to the audit and to the dispatch brief.** Both say the eight (in
fact eleven) `motion-*` credentials do not exist on the panel and that this is
why door motion never fires. They do exist. `wall-firstboot.sh` writes all of
them unconditionally once `DOORBELL_RTSP_HOST` and `DOORBELL_RTSP_PASSWORD` are
set, which they are. The real reason motion never fires is that
`DOORBELL_MOTION_ENABLED` and `DOORBELL_MOTION_CALIBRATED` are both `false` in
`wall.env`, so firstboot's own gate writes `motion-enabled=false` into the
credential and the broker builds no `MotionEngine`. That is a one-line
`wall.env` change plus a calibration, not a missing materialization path.

---

## 1. Protected access and the PIN

### Where each secret lives

| Secret | Lives in | Who writes it |
|---|---|---|
| The PIN | The hub gateway's encrypted state (`/var/lib/panel-access/state.json.enc`), salted scrypt, N=32768 | **The Owner**, once, through `gateway/setup.mjs` |
| `deviceId` + `deviceCredential` | The panel's `/etc/wall-panel/host.json`, 0600 `panel:panel` | `install-wall-capabilities.sh`, from a file the Owner carries over |
| `gatewayUrl`, `accessMode`, `sensorSocket`, `faceEnabled` | the same `host.json` | the same installer |

The PIN never reaches the panel in any form, and no session ever sees it. The
gateway has **no "no PIN yet" state**: `loadStore()` refuses state without a PIN
record, so an un-bootstrapped gateway does not start. Creating the PIN *is*
creating the gateway.

### 1a. Bootstrap the gateway **[Owner present]**

On the hub, with the `panel-access` container stopped:

```sh
node gateway/setup.mjs \
  --state /var/lib/panel-access/state.json.enc \
  --key   /var/lib/panel-access/state.key \
  --device wall-panel \
  --credential-out /var/lib/panel-access/panel-credential.json \
  --pin-file /run/private/owner-pin
```

* The PIN is 6–12 digits. `setup.mjs` refuses a TTY so the digits are never
  echoed; write them to a root-only file that is deleted immediately after, or
  pipe them on stdin. **A session must not generate, suggest, store or read
  this value.**
* `setup.mjs` refuses to overwrite an existing state, key or credential file.
  That is the safety net, and it means the reversal below is the only way back.
* Output: `panel-credential.json`, containing `{"deviceId","credential"}`.

**Reversible by:** stopping the container and deleting the three files. Every
session is invalidated and the panel falls back to `enabled:false` behaviour as
soon as its `host.json` is reverted too (step 1c).

**Forgotten PIN:** `node gateway/recover.mjs --action pin --pin-file …` with the
gateway stopped. Also Owner-only.

### 1b. Enable the gateway on the hub

In the hub `.env`: `PANEL_ACCESS_ENABLED=true`, `PANEL_ACCESS_MODE=read-protected`.
Bring the stack up with the overlay:

```sh
docker compose -f docker-compose.yml -f panel-access/docker-compose.access.yml up -d
```

Run `stack/panel-access/validate-panel-access.py` as the preflight. The gateway
publishes no host port; Caddy is what reaches it, and per the gateway README the
Caddy route is **a security prerequisite, not a convenience** — the `/api/*` →
`/v1/*` swap is what stops the tracker being reachable unauthenticated.

**Reversible by:** `PANEL_ACCESS_ENABLED=false` and `docker compose up -d`
without the overlay. The panel then fails its `status` request, `AccessView`
applies `{enabled:true, available:false}`, and the protected panes stay covered
with "Unlock service unavailable" — i.e. it fails closed, which is why step 1c
must be reverted in the same maintenance window.

### 1c. Install the registration on the panel **[Owner present]**

Build the private host config (0600, on removable media or a root-only path,
never in a repo):

```json
{
  "enabled": true,
  "accessMode": "read-protected",
  "gatewayUrl": "https://wall.diyt.win:8443/",
  "deviceId": "wall-panel",
  "deviceCredential": "<credential field of panel-credential.json>",
  "sensorSocket": "/run/wall-sensors/service.sock",
  "faceEnabled": false
}
```

`sensorSocket` must be exactly that literal — the installer rejects anything
else. Set `faceEnabled` to `false` until the sensor service and an enrolment
exist; turning it on without them only produces a face button that always
fails. Then, on the panel:

```sh
sudo bash /opt/wall-panel/stack/autoinstall/wall/install-wall-capabilities.sh \
  --host-config /media/owner/host.json \
  --wheelhouse  /media/owner/wall-wheels
```

The installer strips any `rendererConfig` from the incoming file and re-grafts
the panel's own, so the feed token and Subsonic credentials that firstboot
materialized are preserved. It writes `/etc/wall-panel/host.json` as
`panel:panel` 0600.

Finally set `WALL_ACCESS_EXPECTED=true` in `/etc/wall-panel/wall.env` and
re-run `wall-firstboot.sh`, then restart the kiosk session.

**Reversible by:** `python3 -c` rewriting `host.json` to `{"enabled": false}`
plus the existing `rendererConfig` (or simply re-running
`render-wall-host-config.py` against a deleted file), setting
`WALL_ACCESS_EXPECTED=false`, and restarting the kiosk.

### 1d. Why this is not in `wall-firstboot.sh`, and what was added instead

`render-wall-host-config.py` owns `rendererConfig` and nothing else; a re-image
restarts `host.json` at `{"enabled": false}`. Making firstboot able to write the
access half would mean putting `deviceCredential` — a per-device bearer secret —
into `wall.env`, which is the one file that is copied around during imaging.
That is a worse trade than a manual step.

So the re-image reproducibility is delivered as an **expectation plus a loud
warning**, not as an automated write:

* `wall.env.example` gains `WALL_ACCESS_EXPECTED` (default `false`).
* `wall-firstboot.sh`, right after it installs `host.json`, checks `enabled`
  when that knob is true and prints four `warn` lines naming the installer and
  this document when it is not. It never writes a credential and never enables
  anything.

Before this, a re-imaged panel came up with its checklist, Settings and
Bluetooth panes open to anybody and looked identical to a provisioned one.

---

## 1e. Local authentication mode — the panel locks itself **[Owner present]**

*Owner ruling, 2026-09-13.* Sections 1a–1d above describe the **gateway** mode
and remain the arrangement to use when the hub gateway is cut over. Local mode
is the alternative the Owner chose for now: **the hub API and the hub's access
flag stay exactly as they are today**, and the panel owns its own lock.

### What it is, and what it is not

* **Is:** a panel-local PIN, a panel-local session with the same idle/hard
  bounds (120 s / 15 min), the same face unlock through the sensor service, and
  the same masking of the three protected panes (Checklist, Settings,
  Bluetooth). Library, Pandora and Door stay public, as in every mode.
* **Is not:** protection of the data. In local mode the renderer reads the hub
  API **directly**, the way an unprotected panel does today. Anyone on the LAN
  who can reach that API is no more restricted than before. **It is a UI lock,
  and it was accepted as one.** Say so in any summary of the panel's security.

### Where each secret lives, in local mode

| Secret | Lives in | Who writes it |
|---|---|---|
| The PIN | The panel's own encrypted access state, `/var/lib/wall-panel/access-state.json`, scrypt N=32768 r=8 p=1 — the same record the gateway writes | **The Owner**, once, at the wall, in Settings |
| The state key | `/var/lib/wall-panel/access-state.key`, 32 random bytes, AES-256-GCM | The panel, on first run |
| The face wrapping key | inside that state; handed to the sensor service only after a correct PIN | The panel |
| `accessMode: "local"`, `enabled: true` | `/etc/wall-panel/host.json` | `render-wall-host-config.py` (firstboot), from `WALL_ACCESS_MODE=local` |

There is **no device credential and no gateway URL**: local mode has nothing to
register. Both files are written 0600, owned by the account the Electron host
runs as (`panel`), inside a 0700 directory. *Deviation on purpose:* the original
brief said root-owned. The access broker runs as `panel` and must read and write
this state on every unlock, so root ownership would make it unreadable to the
only process that uses it; 0600 `panel:panel` in a 0700 directory is the
equivalent posture and matches `host.json` next to it.

A state file that cannot be read, decrypted or parsed **fails closed**: the
panel reports the unlock service as unavailable and keeps the protected panes
masked. It never degrades to "no PIN set".

### Procedure

1. On the panel, as root, in `/etc/wall-panel/wall.env`:
   `WALL_ACCESS_MODE=local` (and `WALL_ACCESS_EXPECTED=true` if you want the
   re-image reminder; local mode satisfies it without a manual step).
2. Re-run `wall-firstboot.sh`. It writes `accessMode: "local"` and
   `enabled: true` into `host.json` and nothing else; no credential is written,
   and nothing is printed that could carry a PIN. It also creates
   `/var/lib/wall-panel` as `panel:panel` 0700 — `/var/lib` is root-owned
   0755, so the kiosk cannot create it itself, and without it local mode fails
   closed on every boot (masked and unavailable).
3. Restart the kiosk session (`systemctl restart getty@tty1`; `wall-kiosk-loop` is the audio loopback, not the kiosk).
4. **At the wall:** Settings now shows *"No panel PIN set…"* and a **Set panel
   PIN** fieldset. Until the PIN exists the panel masks nothing — that is
   deliberate, so the Owner is never locked out of the view that configures the
   lock. Enter 6–12 digits twice on the keypad and tap **Set panel PIN**.
5. The panel locks immediately. Unlock with that PIN. Nothing echoes it, no log
   line contains it, and it is not recoverable — a forgotten PIN is repaired by
   deleting the two files in step "Reversal" and setting a new one.

### Face unlock in local mode

Face is **optional** and adds two hardware prerequisites to the PIN:

* `WALL_CAMERA_ENABLED=true` in `wall.env` (section 2). This gate is
  **authoritative**: `false` blacklists `uvcvideo`, so no software setting can
  turn the camera on, and face simply never becomes available.
* Both ONNX model files present in the sensor service's `modelDirectory`
  (`det_10g.onnx`, `w600k_r50.onnx`) with their **explicit SHA-256 digests in
  the sensor `modelManifest`**. Every cold model load verifies the bytes; no
  model is downloaded or silently accepted (`sensors/PROVENANCE.md`).

With the camera gate off, or the digests absent, the PIN is the only factor —
a supported configuration, and the one the panel ships in.

### Reversal

* Back to today's unprovisioned/open panel, or to gateway mode: set
  `WALL_ACCESS_MODE=gateway` (or empty) in `wall.env` and re-run firstboot. The
  panel returns to its gateway registration if it still holds one; if it does
  not, `enabled` goes back to `false` and nothing is masked. A registration left
  over from an earlier cutover is preserved and ignored while local mode is on,
  so this is a one-knob flip in both directions.
* To forget the PIN and the face wrapping key entirely (a forgotten PIN, or
  handing the panel on), as root:
  `systemctl stop getty@tty1 && rm -f /var/lib/wall-panel/access-state.json /var/lib/wall-panel/access-state.key && systemctl start getty@tty1` (stop the kiosk session, not the audio loopback).
  The panel comes back unprovisioned and open, ready for a new PIN at the wall.
  Any face enrollment is unreadable afterwards: delete it in Settings, or remove
  the sensor gallery file, once a new PIN exists.

---

## 2. The camera

`WALL_CAMERA_ENABLED=false` in `wall.env` is the single source. Firstboot
renders the canonical boolean into `kiosk.env` **and** enforces it at the
kernel: false blacklists and unloads `uvcvideo` so `/dev/video*` does not exist
and the activity LED cannot light.

**Procedure:** set `WALL_CAMERA_ENABLED=true` in `/etc/wall-panel/wall.env`,
confirm `WALL_CAMERA_DEVICE` names the capture node (a UVC camera publishes a
capture node and a metadata node; `video0` is not guaranteed), re-run
`wall-firstboot.sh`. Firstboot un-blacklists and loads `uvcvideo`, warns if the
device node still does not exist, and restarts `wall-sensors.service` if it is
installed.

**Reversible by:** setting it back to `false` and re-running firstboot, which
stops the sensor service, blacklists and unloads the module.

**[Owner present]** for the acceptance: a camera on an office wall is a
household decision, and the only proof the gate works is watching the activity
LED stay dark.

---

## 3. The sensor service the Settings view expects

### What the renderer is asking for

The chain, end to end:

1. `js/views/settings.js` renders `status.camera.health`, `status.presence.state`,
   `status.attention.state`, `status.bluetooth.state`, `status.enrolled`.
2. That status comes from the `sensors-status` host operation
   (`electron/access-broker.cjs`), which is `sensorRequest(this.config.sensorSocket, 'status')`.
3. `sensorSocket` comes from `host.json` — **not** from `kiosk.env`, which
   carries only the path of the JSON via `PANEL_HOST_CONFIG`.
4. With no `sensorSocket`, `electron/main.cjs`'s once-a-second poll skips the
   whole sensor branch and never emits `panel:sensors`, and the broker answers
   `sensors-status` with `Sensor service is not configured`.

So the Settings block has **two** independent reasons to be empty, and both are
real on this panel: there is no sensor unit, and `host.json` has no
`sensorSocket` because access was never provisioned. Item 14's renderer half is
fixed separately in OfficeWallNaglight; this section is the host half.

### The unit exists. Nothing installs it.

`stack/autoinstall/wall/wall-sensors.service` is tracked, and
`stack/autoinstall/wall/install-wall-capabilities.sh` performs every install
step: the `/opt/wall-sensors/venv`, the offline hash-pinned wheel install and
an import smoke test, the `wall-sensors` system user and group, `panel` added to
that group, `/etc/wall-panel/sensors.env` containing the single variable
`PANEL_SENSOR_UID=$(id -u panel)`, the unit file, the D-Bus policy, and
`systemctl enable --now` followed by a protocol check over the socket as the
`panel` uid.

`wall-firstboot.sh` never calls it. That is deliberate (`stack/panel-access/README.md`):
the image stages the payload at `/opt/wall-panel/stack/…` without adding an
automatically enabled service. So the answer to "what sensor service does the
Settings view expect" is:

> `wall-sensors.service`, which exists in MiniPC-Deployer and is installed by
> `install-wall-capabilities.sh` — the same command as step 1c above. It has
> never been run on this panel. No code is missing.

**Two artefacts were build inputs outside the repo when this was written. As of 2026-09-13 evening the wheelhouse exists (`sensor-wheelhouse/requirements.lock` is committed and `build-sensor-wheelhouse.sh` reproduces it byte-identically; see `sensor-wheelhouse/README.md`) and the installer has been run on the panel. The private host config remains a per-panel secret:**

1. **The offline wheelhouse.** `install-wall-capabilities.sh` requires
   `--wheelhouse DIR` containing a hash-pinned `requirements.lock`, and pip is
   invoked `--no-index --require-hashes --only-binary=:all:`. The dependency
   list is `OfficeWallNaglight/sensors/requirements.txt`: `numpy==1.26.4`,
   `Pillow==10.4.0`, `cryptography==43.0.3`, `onnxruntime==1.20.1`,
   `dbus-next==0.2.3`. The wheelhouse must be built on a matching
   Linux/CPython target and its hashes reviewed. **Specified here as an
   outstanding build input; a session must not fabricate a lock file.**
2. **The private host config** of step 1c.

Also required before it proceeds: `ffmpeg` on the image (present), and
`/opt/wall-panel/app/runtime/resources/app/sensors/service.py`, which
`tools/build-artifact.mjs` includes in the shell artifact.

The service reads exactly one variable of its own, `PANEL_SENSOR_UID`, plus
`WALL_CAMERA_ENABLED` out of `kiosk.env` as the immutable hardware gate — a
persisted config or a Settings checkbox cannot override it. Everything else
(device, fps, ROI, thresholds, model directory, Bluetooth) is persisted JSON
state under `/var/lib/wall-sensors`.

**Reversible by:** `systemctl disable --now wall-sensors.service`, removing the
unit, `/etc/wall-panel/sensors.env`, `/opt/wall-sensors` and
`/var/lib/wall-sensors`, and removing `sensorSocket` from `host.json`. The
Electron host is written to tolerate its absence (`if (broker.config.sensorSocket)`),
and after the item 14 fix the Settings block says "not configured" rather than
"unknown".

---

## 4. Door motion: the eleven `motion-*` credentials and the calibration

### The materialization path already works

`wall-firstboot.sh` writes every `motion-*` credential into
`/run/wall-door-credentials/` on every boot from `DOORBELL_MOTION_*` in
`wall.env`, validates each one before writing (bools exactly `true`/`false`;
sample fps 1–10; min area ratio >0 and ≤1; persistence and dwell 0.1–30 s;
stationary ratio 0–1; zones as four normalized numbers that stay inside the
frame; at most 32 masks and 4096 bytes), and falls back to the documented
defaults if any value is malformed so a latent bad value cannot spring to life
on a later one-line enable.

There are two switches, and **both** must be `true` or firstboot writes
`motion-enabled=false`:

* `DOORBELL_MOTION_ENABLED` — the feature switch.
* `DOORBELL_MOTION_CALIBRATED` — an explicit physical-calibration gate.
  Firstboot logs a `fail_step` if motion is enabled without it. It exists so
  nobody enables motion from a keyboard without having stood at the door.

### What each value means

From `OfficeWallNaglight/doorstream/motion.py`. The policy runs on a
320×180-capped occupancy plane, so every zone is normalized `x,y,w,h` in 0..1
of the **corrected** (de-warped) frame, not the fisheye source.

| Value | Meaning | Firing rule |
|---|---|---|
| `TRIGGER_ZONE` | the approach to the door | a track that **crosses into** it, having existed for the persistence time, fires |
| `ROAD_ZONE` | the street / driveway | a track that **moved and then stopped** inside it for the dwell time fires |
| `MASKS` | up to 32 rectangles that are ignored entirely | a component whose centre is inside any mask is dropped before tracking |
| `MIN_AREA_RATIO` | smallest blob, as a fraction of the plane | below it, not a candidate |
| `PERSISTENCE_SECONDS` | how long a track must survive | converted to consecutive *sampled* frames; a missed sample resets it |
| `DWELL_SECONDS` | how long stationary counts as loitering | road-zone rule only |
| `STATIONARY_RATIO` | per-sample centre movement below which a track counts as still | also what sets `moved` |
| `SAMPLE_FPS` | 1–10; the sampling rate the two times above are measured in | |
| `DIAGNOSTICS` | emits a once-a-second `diagnostic` message on the door socket | |

A whole-frame change (`global_change`) discards the sample and resets the
policy, which is what keeps a light switch or an IR cut-over from firing it.

### Calibration procedure **[Owner present]**

This is physical work; a session can do none of it.

1. Set `DOORBELL_MOTION_DIAGNOSTICS=true`, leave `DOORBELL_MOTION_ENABLED=false`,
   re-run `wall-firstboot.sh`. Diagnostics are cheap and, with motion disabled,
   nothing can take the screen while you work.
2. Capture a corrected frame from the Door tab and mark on it, in normalized
   coordinates: the doormat/approach rectangle (`TRIGGER_ZONE`), the street
   (`ROAD_ZONE`), and everything that moves and is not a person — the road
   itself if it is outside the road zone, a flag, a tree, a reflective window
   (`MASKS`).
3. **Baseline, empty scene, 10 minutes.** Watch `foregroundPermille` and
   `components` in the diagnostic stream. An empty scene should sit near zero
   components. Raise `MIN_AREA_RATIO` until wind-driven foliage stops producing
   components, or mask it.
4. **Walk-ups, five each.** Approach the door at normal pace, slowly, and at an
   angle. Each must produce `decision:1` once. If it does not, lower
   `PERSISTENCE_SECONDS` or `MIN_AREA_RATIO`; if a single approach produces
   several, the persistence is too low.
5. **Pass-bys, five.** Walk past on the street without approaching. None may
   fire. If they do, the trigger zone reaches too far out; shrink it.
6. **Loiter, two.** Stand still in the road zone past the dwell time. Each must
   fire once.
7. **Night.** Repeat 3–5 after dark, with the camera's IR on. The IR cut-over
   itself is a global change and is discarded; what matters is the noise floor
   afterwards.
8. Record the final values, set `DOORBELL_MOTION_ENABLED=true` **and**
   `DOORBELL_MOTION_CALIBRATED=true`, set `DIAGNOSTICS=false`, re-run
   `wall-firstboot.sh`, and confirm `motion-enabled` reads `true`.

**Reversible by:** `DOORBELL_MOTION_ENABLED=false` and re-running firstboot.
The broker is restarted with a fresh credential set and builds no engine.

**Gap found while writing this, not fixed here, and it blocks steps 3–7 as
written.** `electron/door-bridge.cjs` handles `status`, `frame` and `motion`
messages and **ignores `diagnostic` entirely**. Nothing in the Electron host or
the renderer surfaces the numbers those steps depend on.

Reading the socket by hand is **not** a usable substitute as things stand, and
the first draft of this document was wrong to suggest it was. `doorstream/service.py`
calls `server.listen(1)` and serves one client at a time in a single-threaded
accept loop: while the kiosk's bridge owns the connection a second client just
queues behind it and receives nothing. A hand client must also send its own
request line first — `{"mode":"corrected","visible":false}\n` — or the broker
sits waiting for it.

So the diagnostic read is **exclusive**, and the calibration must be run one of
two ways:

* **Exclusive-diagnostic mode (available today).** Stop the kiosk
  (`systemctl --user stop wall-kiosk` or whatever the session unit is), which
  releases the socket, then connect as a member of the `panel` group (socket
  0660 `wall-door-stream:panel` inside a 0751 directory), send the request line,
  and read the `diagnostic` messages. The panel shows nothing during the run,
  which is acceptable for calibration but means steps 4–6 need a second person
  at the door.
* **Bridge-forwarded diagnostics (the right fix).** Have `door-bridge.cjs`
  accept `diagnostic` and log it under a `journalctl -t wall-door-stream` tag,
  or surface it in the Door tab. Then the calibration is one person with the
  panel running. It is a small change, but it touches the door view and is
  therefore out of Group C's scope; it belongs to whichever lane the
  coordinator gives the door to, and it should land **before** the calibration
  is attempted.

---

## 5. Order of operations

1. Gateway bootstrap on the hub (1a) — creates the PIN and the credential.
2. Gateway enabled behind Caddy (1b) — validated before the panel is touched.
3. Build the offline wheelhouse (§3) — done 2026-09-13, reproducible from the committed lock.
4. `install-wall-capabilities.sh` on the panel (1c + §3) — one command does both
   the access registration and the sensor service.
5. `WALL_ACCESS_EXPECTED=true`, camera knob if wanted (§2), firstboot, kiosk
   restart.
6. Door motion calibration (§4), which needs daylight and dark and is therefore
   two sessions.

Item 13 is acceptable after 5. Item 14 needs 4. Item 7's camera half needs 2
and 5. Item 21 needs 6.
