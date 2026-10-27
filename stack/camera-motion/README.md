# camera-motion — the hub's motion detector (SR-128)

The event source for motion clips: one `homehub-camera-motion@<camera>` per
name in `CAMERAS`, each running the panel's door motion engine on a decode of
that camera's relay stream and writing IF-076 records. It ships off.

| File | What |
|---|---|
| `camera_motion.py` | `condition`, `check`, `run`, `replay`, `cost` |
| `motion_engine.py` | OfficeWallNaglight `doorstream/motion.py`, byte for byte - never edit it here |
| `motion-engine.pin` | the commit, last change and sha256 it was taken at; the run refuses any other bytes |
| `homehub-camera-motion@.service` | the instance unit |

The design, the measurements, the tests and the deploy steps (including the
cost test that comes before switching it on) are in
[`../clips/README.md`](../clips/README.md).
