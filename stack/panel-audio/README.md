# Panel audio broker — feasibility-gated image boundary

This directory implements SR-023's narrow local protocol and pure policy. It is
not a working Bluetooth/PipeWire router yet, deliberately.

The real-panel read-only probe on 2026-09-10 found four ALSA playback devices,
one ALSA capture device and a Bluetooth controller with no paired devices.
`wpctl` and `pactl` were unavailable, so it did **not** establish a PipeWire
session, any sink/source/monitor, an A2DP role, or acceptable latency/coexistence.
(The A2DP SINK that landed on 2026-09-12 is bluez-alsa, not PipeWire, and is
described in the wall README. It says nothing about this broker's backend, which
still reports unavailable.)

A codec pin dump on 2026-09-12 settled the two hardware questions that probe
left open. The panel's codec is a Realtek **ALC255**, and **the built-in 3.5 mm
jack is output-only** — there is no analog audio input on this box, mono or
stereo:

- the sole wired external connector is node `0x21`, `[Jack] HP Out at Ext Front`,
  whose widget caps are `Stereo Amp-Out` with no input amp. Retasking it as a
  line-in via `hda-verb` cannot work: the pin has no capture path in silicon;
- every other external pin (`0x18`, `0x19`, `0x1a`, `0x1b`, `0x1e`) carries
  pincfg `0x411111f0` — port connectivity `none`, i.e. no physical connector.
  The pins that do have `Amp-In` are exactly the unwired ones;
- the kernel exposes one analog jack-detect input, `HDA Intel PCH Front
  Headphone`. The other three playback devices are HDMI/DP;
- the single capture device is the internal digital mic, node `0x12`,
  `[Fixed] Mic at Int` / `Conn = Digital` — not the jack.

Beware one trap: `amixer` lists `Headset Mic` and `Headset Mic Boost` controls,
so a mixer-only probe reads as though a combo jack exists. It does not — those
controls hang off node `0x19`, which is declared unconnected. Any future
audio-in feature therefore needs a USB sound card, not a cable.

Consequently:

- `WALL_AUDIO_ENABLED=false` ships by default;
- `audio_router.py` binds only a Unix socket and its shipped backend returns an
  honest `probe-required` status while refusing mutations;
- mutation requests require an injected authorization callback and default to
  deny. The image has intentionally not guessed how panel authentication maps
  onto that callback, so the shipped service cannot mutate devices;
- no WirePlumber profile, automatic capture selection or PipeWire package/session
  assumption is installed. **The Bluetooth FRONT DOOR is a separate thing and it
  does ship** (SR-025, 2026-09-12): the adapter's power/discoverable/pairable
  policy, a window-scoped pairing agent, and a bluez-alsa A2DP sink. None of
  that is reachable from the renderer and none of it grants this broker any
  authority -- the split is deliberate, because "the panel is a speaker a phone
  plays through" and "the renderer may command Bluetooth" are different
  questions with different answers. See `stack/autoinstall/wall/README.md`;
- the unit provisionally runs as `panel`, because an eventual PipeWire graph is
  normally session-owned. A separate broker identity remains preferable for
  Bluetooth least privilege, but choosing it now could make the audio graph
  unreachable. Physical feasibility must settle that boundary.

`routing.py` and `visualizer.py` are pure/testable. The broker serializes
generation checks and successful mutations, validates aliases against a bounded
trusted inventory, rejects common colon/hyphen/underscore/compact hardware-
address forms plus Cisco-dotted forms (including pairing confirmation), and
accepts only method-specific response schemas. Production backend operations
run in bounded spawned child processes. A two-second deadline terminates and
reaps a stuck child before returning its slot, so repeated uncooperative calls
cannot permanently exhaust the broker. The backend object must therefore be
serializable; the deliberately unavailable image backend satisfies that seam.

Production uses `/var/lib/wall-audio-router/state.json`, in systemd's private
`StateDirectory`. The broker atomically records a mutation intent before device
I/O and records the exact result plus new JavaScript-safe generation after a
confirmed success. A lost reply can therefore be retried with the same request
ID and receive the saved result without a second device call. A crash, timeout
backend-unavailable result, other exception, or invalid result after intent is
deliberately sticky `mutation_uncertain` on
restart: routing changes remain refused until an operator reconciles actual
device state and removes that journal. Status and telemetry remain readable.

Every telemetry result is retained under its request epoch and rejected if a
concurrent mutation advances that epoch while capture is in flight; available
derived samples additionally carry that epoch in their result. The mode-0660 socket
shell has bounded concurrent clients, Linux peer-UID
enforcement and read deadlines. `telemetry`
is an IF-015 read method with a positive schema: unavailable, or bounded derived
bands/RMS/peak/activity plus broker-owned generation and monotonic observation time. The
visualizer consumes a bounded normalized sample window, applies bounded silence
hold and emission cadence, emits no raw samples, and retains no samples. The
shipped backend reports telemetry unavailable; the eventual capture adapter
belongs behind the injected backend after the probe and an adversarial authority
review. The service reads only root-owned `/etc/wall-panel/audio-router.env`,
never the broad wall environment containing unrelated credentials. That file's
exact allowlist is `WALL_AUDIO_ENABLED` and `WALL_AUDIO_SOCKET`; the enable bit
is nonsecret installed-state evidence for the verifier without granting access
to `wall.env`.

## The rest of the panel's audio lives next door

This directory is the SR-023 Bluetooth/routing broker, which is still
disabled-by-default and still reports `backend_unavailable`. The panel's working
audio path is not here — it is a set of units and ALSA configuration under
`../autoinstall/wall/`:

| File | Does |
|---|---|
| `asound.conf` + `asound-{trigger,panel}-mode.conf` | the dmix/dsnoop graph and the two output modes |
| `wall-line-in.service` | line input passthrough to the amplifier |
| `wall-kiosk-loop.service` | routes kiosk audio through snd-aloop so it can be measured |
| `wall-amp-trigger.service`, `panel-amp-trigger.py` | detects playing audio and commands the LCUS-2 relay (the headphone-tone actuator is retired, 2026-09-13) |
| `wall-volume-keys.service`, `panel-volume-keys.py` | the side rocker |
| `wall-audio-mode` | switches output modes |
| `90-wall-audio-adapter.rules` | gives the USB adapter a port-independent systemd alias and starts the three units on it |
| `91-wall-usb-hub-reset.rules`, `wall-usb-hub-reset@.service`, `wall-usb-hub-reset.py` | bounded re-enumeration of the amplifier-side hub when it comes back with no downstream ports |
| `wall-alsaloop-guard.py` | runs alsaloop with a bounded recover loop so a wedge becomes a restart |

### Surviving a USB re-enumeration (Owner item 25, 2026-09-13)

The Owner moved the USB hub that carries the adapter and the relay to another
port. Everything below the applications recovered by itself: the kernel
re-enumerated the ICUSBAUDIO7D with the same card id, udev recreated
`/dev/wall-amp-relay`, and `wall-line-in` restarted because its alsaloop died.
What did not recover was the two long-running consumers:

* `wall-kiosk-loop`'s alsaloop kept its handle on the dead device instance and
  printed `unable to prepare slave` forever. It never exits, so `Restart=always`
  never fired and the unit read as perfectly active while no audio moved.
* `wall-amp-trigger` lost its line-in capture and its relay handle.

`systemctl restart wall-kiosk-loop wall-amp-trigger` fixed it by hand. The
durable fix has three parts and no path edits, because card **ids** rather than
indexes were already in use everywhere:

1. `90-wall-audio-adapter.rules` tags the adapter's sound card for systemd and
   gives it `ENV{SYSTEMD_ALIAS}="/dev/wall_audio_adapter"`. The alias is the
   point: the kernel's own device unit name contains the USB port, which is the
   thing that changes. On every non-remove event, so the tag cannot go stale.
2. The three units `BindsTo=` and `After=` `dev-wall_audio_adapter.device`, so a
   remove event stops them; the rule's `SYSTEMD_WANTS` starts them again when the
   adapter comes back. With the adapter absent the start job fails as a
   dependency and the unit stays inactive — no restart loop, and nothing that
   holds up boot or the kiosk, which is a tty autologin loop rather than a unit.
3. Both alsaloops run under `wall-alsaloop-guard.py`, which exits non-zero after
   more than `--max-errors` stream errors inside a `--window` second window and
   hands recovery back to `Restart=`. `panel-amp-trigger.py` additionally waits a
   bounded `WALL_AMP_RELAY_WAIT_SECONDS` (default 6, sized from the ten-second
   budget because this wait runs before the capture threads) for the relay's
   node, reopening a fresh transport each attempt, because the CH340 is on the
   same hub and may still be re-probing when this service restarts. If it still
   cannot reach the relay the detector runs anyway with the safe-state proof
   revoked — which blocks S3 exactly as before — and keeps commanding OFF until
   one verifies: once a second for the first fifteen seconds after the restart,
   on the ordinary five-second actuator beat after that, so a relay that comes
   back at seven seconds is commanded at seven rather than at eleven. That path matters because the
   relay can disappear BEFORE the sound card does, so the stop that `BindsTo`
   triggers may have no CH340 to command and the LCUS-2 latches physically on.

4. **The hub itself can be the thing that fails** (addendum, 21:52 the same
   evening). On the Owner's second move the 4-port Atmel hub answered on the
   bus and none of its downstream ports did —
   `hub 1-2:1.0: hub_ext_port_status failed (err = -71)` — so the adapter and
   the CH340 were both absent, the three units stopped exactly as designed, and
   there was nothing for 1–3 above to recover onto. Everything in this section
   depends on the hub cooperating, and that time it did not. A software
   re-enumeration (`authorized` 0 then 1 on the hub) brought both back and the
   units restarted themselves.

   `91-wall-usb-hub-reset.rules` and `wall-usb-hub-reset@.service` now do that
   automatically, and the bound matters as much as the recovery, because
   toggling `authorized` is a hardware reset of everything below the hub —
   including the LATCHING amplifier relay. So:

   * it is a **oneshot started by a udev add event on the hub**, not a daemon
     and not a timer, so there is no continuous authority to reset anything;
   * it acts only when **both** conditions hold: `dev-wall_audio_adapter.device`
     is still inactive after 15 s, *and* the hub has **no children at all**. A
     hub with the relay attached but no adapter is a working hub and an absent
     adapter — unplugging the adapter on purpose must not reset the relay;
   * it toggles at most **twice per hub**, and a `/run` record caps it at twice
     per ten minutes even if re-authorizing produces more udev events. After
     that it logs a give-up and stops, because a hub that needs a third reset
     needs a person;
   * every decision is journalled under `wall-usb-hub-reset@<bus id>`, including
     the ones where it decided to do nothing;
   * `Restart=no`, and no `[Install]`: it is only ever started by the rule;
   * it **fails closed** on both of its own dependencies. If `systemctl` cannot
     be asked whether the adapter is active, that is not evidence the adapter
     is absent and nothing is reset; if the `/run` record that bounds the
     toggles cannot be written, nothing is reset either, because that file is
     the only bound that survives the process.

   The 15 s wait is sized off the acceptance below — ten seconds is the whole
   budget when the hub cooperates, so intervening sooner would race a recovery
   that was already working.

A panel-mode replug does start `wall-amp-trigger`, because `SYSTEMD_WANTS`
ignores enablement. That is the safe direction and is left alone: the daemon
commands and verifies the relay OFF before it idles.

**Acceptance procedure.** With music playing from the kiosk and the amplifier
on, move the USB hub to a different port on the panel. Within 10 s, with no
manual restart: the amplifier is on and kiosk audio is audible. Then

```sh
journalctl -u wall-kiosk-loop -u wall-line-in -u wall-amp-trigger --since -2min
systemctl status wall-kiosk-loop wall-line-in wall-amp-trigger
```

must show each of the three units stopping once and starting once — one cycle,
not a restart loop — and `wall-amp-trigger` logging `amplifier ON` again. A
`journalctl | grep 'unable to prepare slave'` that keeps growing after the move
is the original defect, not this fix working slowly.

**Acceptance for the hub reset (the addendum).** The fault is not reproducible
on demand — it is a hub that comes back empty — so acceptance is in two parts.

*Whenever it fires for real:*

```sh
journalctl -u 'wall-usb-hub-reset@*' --since -10min
```

must show exactly one `Toggling authorized (attempt 1 of 2)` line naming the bus
id, followed by the three audio units cycling and the amplifier coming on. Two
toggles and a give-up line is the hub failing, not this failing.

*Provable at any time, without breaking anything:*

```sh
# the rule is loaded and matches the hub
udevadm test "/sys/bus/usb/devices/$(lsusb -d 03eb:0902 >/dev/null &&     grep -l 03eb /sys/bus/usb/devices/*/idVendor | head -1 |     xargs dirname | xargs basename)" 2>&1 | grep -i systemd_wants
# the healthy path: the adapter is present, so this exits at once, touching
# nothing, and says so
sudo /usr/local/lib/wall-panel/wall-usb-hub-reset.py <bus id> --wait 2
systemctl cat 'wall-usb-hub-reset@.service' | grep -E 'Type|Restart'
```

The dry run above is safe **because** the adapter is present: the script asks
systemd first and returns before it looks at `authorized` at all. Running it
with the adapter genuinely absent will reset the hub, which is the point.

The measurement record behind all of it, including the trigger circuit that is
still to be built, is `PANEL_AMP_AUTOPOWER.md` in the HomeHub repo.
The LCUS-2 alternative changes only the final actuator. The detector still
watches the same line-in and kiosk/Bluetooth monitor sources. Its CH340 serial
path, selected channel and physical limitations are recorded in the application
repo's `docs/lcus2-amplifier-trigger-plan.md`.

A mute method (`set_mute`) is planned for THIS broker and will be the first
mutation it actually performs; the design and the open journal-class question
are in `PANEL_MUTE_BUTTON_PLAN.md`, also in HomeHub.
