# camera-motion — the hub's motion detector (SR-128)

The event source for motion clips: one `homehub-camera-motion@<camera>` per
name in `CAMERAS`, each running the panel's door motion engine on a decode of
that camera's relay stream and writing IF-076 records. It ships off.

| File | What |
|---|---|
| `camera_motion.py` | `condition`, `check`, `run`, `replay`, `cost` |
| `motion_engine.py` | OfficeWallNaglight `doorstream/motion.py`, comments removed (this repository is public), code unchanged - never edit it here |
| `motion-engine.pin` | the commit it was taken at and the scrubbed file's sha256; the run refuses any other bytes, and TC-1543 compares its code with that commit's |
| `homehub-camera-motion@.service` | the instance unit |

The design, the measurements, the tests and the deploy steps (including the
cost test that comes before switching it on) are in
[`../clips/README.md`](../clips/README.md).
