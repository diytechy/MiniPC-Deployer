# Panel settings inventory, lifecycle acceptance, and the motion takeover plan

Group C deliverables for Owner items **16** (inventory + sleep/power-cycle
acceptance) and **21** (door motion takeover test plan), 2026-09-13.

Both are procedures. **Nothing here has been executed**, and item 21's plan
cannot be run at all until door motion is enabled and calibrated
(`stack/panel-access/PROVISIONING.md` §4).

---

## Item 16 — every setting store, and who owns what

Six stores. The column that matters is the last one: when two stores appear to
hold "the same" setting, exactly one of them is authoritative and the other is
a cache or a hardware gate.

### 1. Renderer `localStorage` (Chromium profile on the panel, `panel` account)

| Key | Setting | Owner |
|---|---|---|
| `officewall.media-mode` | Major-screen media: Frame media vs local audio visualizer | **This store.** `MediaPreference` in `js/visualizer-preference.js` is the only writer; the Settings radio buttons write through it. |

That is the **entire** renderer-persisted surface. A repo-wide grep for
`localStorage` / `sessionStorage` / `indexedDB` across `js/` and `electron/`
finds this one key and nothing else. Electron uses the default `userData` path
and no partition override for the shell window, so the value survives a kiosk
restart, a suspend/resume and a reboot, and is lost only if the profile
directory is deleted.

### 2. `/etc/wall-panel/host.json` (0600 `panel:panel`)

| Field | Setting | Owner |
|---|---|---|
| `enabled`, `accessMode`, `gatewayUrl`, `deviceId`, `deviceCredential` | Whether protected access exists on this panel at all, and its registration | **This store**, written only by `install-wall-capabilities.sh` |
| `faceEnabled` | Whether the host may attempt face unlock, and whether `AccessView` offers a face button | **This store.** Note the Settings checkbox `faceLoginEnabled` is a *different* setting (see store 4) — this one is the host-side permission and it wins. |
| `sensorSocket` | Whether the host polls a sensor service at all | **This store** |
| `rendererConfig.{FEED_TOKEN,HEARTBEAT_URL,SUBSONIC}` | Renderer secrets | **`wall.env`** via `render-wall-host-config.py`; `host.json` is the materialized copy, replaced on every firstboot so a removal revokes |

### 3. `/etc/wall-panel/wall.env` (0600 `root:root`) — the image's source of truth

Owns, and re-materializes on every firstboot run: `WALL_CAMERA_ENABLED` and
`WALL_CAMERA_DEVICE`; every `DOORBELL_*` value including the eleven `motion-*`;
`SLEEP_MODE`, `SLEEP_START`, `SLEEP_END`, `SLEEP_RTC_WAKE`; the cursor-park
knobs; `TOUCH_*`; `WALL_AUDIO_*`; `WALL_BLUETOOTH_*`; `WALL_ABSENCE_*`; the
renderer secrets above; and (new) `WALL_ACCESS_EXPECTED`.

Two derived stores are **caches, never authorities** — both are regenerated
from `wall.env` and hand-editing either is overwritten on the next firstboot:

* `/etc/wall-panel/kiosk.env` (0644) — the non-secret subset the kiosk can read,
  including the canonical `WALL_CAMERA_ENABLED` boolean.
* `/run/wall-door-credentials/*` (0600 root, volatile) — the 25 door values,
  rewritten every boot.

### 4. Sensing settings — `/var/lib/wall-sensors/` (0700 `wall-sensors`)

Owns everything the Settings "Sensing" fieldset shows except the camera gate:
`presenceFace`, `presenceMotion`, `attentionEnabled`, `bluetoothEnabled`,
`faceLoginEnabled`, plus thresholds, ROI, model directory and the face
enrolment. Written only through the sensor service's `configure` RPC, which is
optimistic-locked on a `revision` the view carries in its draft.

**The camera checkbox is the trap.** `cameraEnabled` exists in this store *and*
`WALL_CAMERA_ENABLED` exists in `wall.env`. The env value is an immutable
hardware gate — false blacklists and unloads `uvcvideo`, so `/dev/video*` does
not exist — and `sensors/service.py` reads it once at startup as
`camera_allowed`. **`wall.env` wins; the checkbox can only turn the camera off,
never on.** This store does not exist on the panel today (no sensor service).

### 5. Tracker-side state — NagLight, on the hub

Owns every check-off, count, undo record and band colour. The panel holds none
of it; `tracker.invalidate()` drops the cached copy synchronously the moment
authority is lost. Nothing in item 16 depends on panel persistence for these —
they survive a power cycle because they were never on the panel.

Session state (whether you are unlocked) is owned by the **gateway**, not the
panel: deadlines are absolute, and the host locks on `suspend` and
`lock-screen` via `powerMonitor`. A sleep/wake therefore *should* come back
locked, and that is correct behaviour rather than a lost setting.

### 6. Audio mode file — **does not exist yet**

Group D's item 23 step 2 introduces a host-side `mute|headset|speaker` file
owned by the audio broker, with per-output volume memory. Owner ruling G says
the mode persists through reboot, and item 16 extends that to sleep. When it
lands it is a seventh store and this table needs a row; the acceptance below
already has a placeholder step for it.

### What is NOT stored anywhere — and item 16 asks for it

**The active tab.** `DisplayMachine` sets `this.tab = CHECKLIST` in its
constructor and nothing persists `tab` or `lastSelectedTab`. The tab survives a
FULL collapse (and, after the item 7 fix, is returned to correctly), but a
kiosk restart or a power cycle always lands on Checklist.

A suspend/resume does **not** restart the kiosk, so the tab survives sleep. A
power cycle does not. Item 16's "every setting and the active tab survive" is
therefore **half unmet by construction**, and closing it means persisting the
tab — a small change, but one that touches `js/main.js` and `js/state-machine.js`,
which Groups A and B are also editing. It is recorded here rather than done
inside this group's diff; the coordinator should schedule it after the A→B→C
merge.

---

## Item 16 — the acceptance procedure

Run **last**, after A, B and C are merged and one release is deployed, per the
coordinator plan's final-integration line. The panel's sleep window is
`SLEEP_MODE=suspend`, `SLEEP_START=22:00`, `SLEEP_END=06:45`, `SLEEP_RTC_WAKE=true`.

### Preparation — set a distinguishable state, not a default one

Every value below must differ from its default, or the test proves nothing.

1. Settings → Major-screen media → **Local audio visualizer** (default is Frame).
2. Settings → Sensing → flip **two** checkboxes and Save with the PIN; note the
   `revision` the view reports. *(Skipped if the sensor service is not installed;
   say so in the record rather than passing the step vacuously.)*
3. Select the **Library** tab (default is Checklist) and leave it selected.
4. Unlock with the PIN, so the session state is non-default too.
5. *(When Group D's mode file exists)* set the output switch to **Headset**.
6. Record `VERSION`, `journalctl -t wall-kiosk -n 5`, and a `grim -c` screenshot.

### A. Sleep and wake — the real timer, not a manual suspend

The timers are the acceptance, because a hand-run `systemctl suspend` skips
`wall-sleep.sh` and therefore skips the RTC alarm and the backlight path.

7. `systemctl list-timers wall-sleep.timer wall-wake.timer` — confirm the next
   elapse times are tonight's 22:00 and tomorrow's 06:45.
8. Let the window run. Do not touch the panel.
9. After 06:45, before touching the screen: `journalctl -u wall-sleep -u wall-wake
   -u wall-sync-suspend -u wall-sync-resume --since yesterday`. Expected:
   `wall-sync-suspend` stops `wall-sync-frame.timer`/`.service`/`wall-sync.service`
   before the freeze; the kernel logs a clean `PM: suspend entry (deep)` with no
   "Freezing user space processes failed"; `wall-sync-resume` runs *after* the
   wake and re-starts both the sync and `wall-sync-frame.timer`.
10. `systemctl is-active wall-sync-frame.timer` — **must be active.** A stopped
    frame timer here is the documented self-perpetuating failure (the frame flow
    silently drops to boot-and-resume cadence after the first night).
11. Screenshot before any touch. Then one touch, and check:
    * media mode is still **visualizer**;
    * the tab is still **Library**;
    * the panel is **locked** (expected: `powerMonitor` `suspend` locks it) and
      the PIN pad is up by itself — that is the item 7 fix under the item 16
      procedure;
    * sensing checkboxes still read as saved.

### B. Full power cycle

12. Note everything again, then **[Owner present]** pull power at the wall — not
    `systemctl reboot`. Quirk 4b: there is no battery and no UPS, so an
    unexpected mains loss is the failure this is standing in for.
13. Restore power. Do not touch the screen until the shell has painted.
14. `journalctl -b -u wall-firstboot` — confirm it re-materialized `kiosk.env`,
    `host.json` and the door credentials without a `fail_step`, and that the
    protected-access warning is **absent** (i.e. `WALL_ACCESS_EXPECTED=true`
    and `enabled` is true).
15. Check the same list as step 11, and record honestly:
    * media mode **must** survive (localStorage, Chromium profile on disk);
    * sensing settings **must** survive (`/var/lib/wall-sensors`);
    * camera and door-motion state **must** survive (`wall.env` → firstboot);
    * the session **must not** survive — a reboot that left the panel unlocked
      is a defect, not a passed persistence test;
    * the tab **will not** survive. Expected: Checklist. Record it as the known
      gap above, do not fail the run on it, and do not close item 16 on it
      either.
16. `sha256sum` the deployed site payload and `VERSION` — a power cycle must not
    change what is installed.

### C. What counts as done

Item 16 closes when steps 11 and 15 agree with the table above for every store
that exists at the time, with a screenshot per check and the journal excerpts
from 9, 10 and 14 pasted into the row. The tab gap and any store that does not
yet exist are named explicitly in the closing note.

---

## Item 21 — door motion takeover test plan

**Blocked on** `stack/panel-access/PROVISIONING.md` §4: `motion-enabled=true` in
`/run/wall-door-credentials/motion-enabled`, which requires
`DOORBELL_MOTION_ENABLED=true` **and** `DOORBELL_MOTION_CALIBRATED=true`.

### How the takeover actually works, so the tests match the mechanism

* The broker emits `{"type":"motion","sequence":N}` on its Unix socket.
* `electron/door-bridge.cjs` accepts it only if the sequence is a safe
  non-negative integer **strictly greater** than the last one, and re-emits it
  with the connection's `generation`.
* `js/main.js` puts it into `ambientFacts.doorMotion` and calls `apply()`.
* `AmbientPriority.update()` (`js/ambient-priority.js`) takes a **5 second**
  lease (`DOOR_LEASE_MS`), and door is the **first** rule — it outranks red,
  orange, yellow, the visualizer and the frame.
* `apply()` maps the `DOOR` owner to `major = DOOR` and calls `door.present(...)`,
  so the door pane is composited over the FULL screen.
* Ambient arbitration runs **only in FULL**: `apply()` computes
  `owner = to.display === FULL ? ambient.update(...) : AMBIENT_NAG`.

Two consequences the tests must pin: the takeover lasts 5 s from the last
accepted motion message and renews while motion continues, and **it cannot
happen while somebody is using the panel**, because SPLIT never consults
`AmbientPriority` at all.

### Tests

| # | Test | Method | Expected |
|---|---|---|---|
| 21.1 | Motion takes FULL | Leave the panel idle until it collapses to FULL. Walk into the trigger zone. | Within one sample period the door pane covers the screen. `shell.dataset.owner === 'DOOR'`. Screenshot via `grim -c`. |
| 21.2 | It releases | Leave the trigger zone and stand still out of frame. | The door pane goes at 5 s (±1 sample) and FULL returns to whatever the colour rule says — NAG on a non-green, frame or visualizer on a green. |
| 21.3 | It renews, it does not stack | Stand in the trigger zone for 30 s. | One continuous takeover, no flicker, and it still ends ~5 s after you leave. |
| 21.4 | It loses to a human | Touch the panel (SPLIT), then have a second person trigger motion. | **No takeover.** SPLIT does not consult ambient priority. The Door *tab* is still selectable by hand. |
| 21.5 | Replay is refused | Restart `wall-door-stream` mid-test. | The `generation` increments; an old generation can never regain authority, and a repeated or lower `sequence` inside a generation is dropped. Journal + no visible takeover from the stale stream. |
| 21.6 | Dark screen | Trigger motion inside the sleep window. | Nothing lights up. `applyDisplayLit(false)` clears `doorMotion` to `null`, so a motion observed while the display is off cannot be waiting to fire on wake. |

### The negative test the brief asks for: the checklist attention state

**It must not fire from the checklist attention state**, and the reason is
worth stating precisely, because "attention" appears in two places and only one
of them is relevant.

* `AttentionLease` / `renewAttention()` is a *camera gaze* lease. It only exists
  while `display === SPLIT` (`renewAttention` returns false otherwise) and its
  only effect is to postpone the collapse to FULL. It is not an ambient owner
  and `AmbientPriority` has never heard of it.
* The thing that *can* own FULL and is easy to confuse with it is
  `AMBIENT_NAG` raised by a **yellow** band, which holds a 20-minute lease
  (`YELLOW_LEASE_MS`) — the checklist demanding attention.

So the negative test is: **a checklist that is demanding attention must not be
able to produce a door takeover, and a door takeover must not be creditable to
the checklist.** Concretely:

| # | Test | Method | Expected |
|---|---|---|---|
| 21.7 | Gaze-held checklist never takes over | With the camera enabled, stand in front of the panel reading the checklist in SPLIT for 60 s, with **nobody at the door**. | `display` stays SPLIT the whole time (the gaze lease is doing its job), `shell.dataset.owner` is never `DOOR`, and no `motion` message appears on the door socket. If a `motion` message *does* appear with nobody at the door, motion is mis-calibrated: the panel's own viewer is being seen by the door camera, or a mask is missing. Go back to §4 step 3. |
| 21.8 | A yellow checklist FULL is not a door takeover | Drive the tracker to yellow so `AMBIENT_NAG` owns FULL on its 20-minute lease. Do not go near the door. | `owner` stays `NAG` for the whole lease. Nothing promotes it to `DOOR`; the only thing that can is an accepted `motion` message. |
| 21.9 | And the door still beats it | With 21.8's yellow still owning FULL, walk into the trigger zone. | `DOOR` wins immediately — door is checked before every band — and hands FULL *back* to `NAG` after 5 s, with the yellow lease undisturbed. |

### Instrumentation gap to close first

`electron/door-bridge.cjs` handles `status`, `frame` and `motion` and **drops
`diagnostic` messages on the floor**. With `DOORBELL_MOTION_DIAGNOSTICS=true`
the broker emits per-second component/track/foreground numbers that tests 21.1,
21.7 and the whole of §4's calibration need, and nothing on the panel surfaces
them. Until that is closed, read them directly from
`/run/wall-door-stream/service.sock` as a member of the `panel` group. This is
named in `PROVISIONING.md` §4 as well; it is one small change to the door
bridge and belongs to whichever lane owns the door view.
