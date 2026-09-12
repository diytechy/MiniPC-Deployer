# Panel audio broker — feasibility-gated image boundary

This directory implements SR-023's narrow local protocol and pure policy. It is
not a working Bluetooth/PipeWire router yet, deliberately.

The real-panel read-only probe on 2026-09-10 found four ALSA playback devices,
one ALSA capture device and a Bluetooth controller with no paired devices.
`wpctl` and `pactl` were unavailable, so it did **not** establish a PipeWire
session, any sink/source/monitor, an A2DP role, or acceptable latency/coexistence.

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
- no BlueZ D-Bus policy, WirePlumber profile, automatic capture selection or
  PipeWire package/session assumption is installed;
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
