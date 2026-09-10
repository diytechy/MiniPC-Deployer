# Panel audio broker — feasibility-gated image boundary

This directory implements SR-023's narrow local protocol and pure policy. It is
not a working Bluetooth/PipeWire router yet, deliberately.

The real-panel read-only probe on 2026-09-10 found four ALSA playback devices,
one ALSA capture device and a Bluetooth controller with no paired devices.
`wpctl` and `pactl` were unavailable, so it did **not** establish a PipeWire
session, any sink/source/monitor, the built-in jack's direction, the capture
device's physical identity, an A2DP role, or acceptable latency/coexistence.

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
trusted inventory, and accepts only method-specific response schemas. Its
socket shell has bounded concurrent clients and read deadlines. The visualizer
consumes a bounded normalized sample window, applies a bounded silence hold and
emission cadence, emits only derived bands/RMS/peak/activity, and retains no
samples. The eventual capture adapter belongs behind the injected backend,
after the probe and an adversarial authority review.
