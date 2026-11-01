# Kids kiosk session — prototype (2026-11-01)

**A prototype, not part of any image.** It is not traced to a requirement and
nothing builds or installs it. It exists to prove the one design question the
planned kids kiosk image (a third target beside the hub and the wall panel)
could not answer on paper: **can a touch kiosk mirror itself to a TV, and lose
that TV at any moment, without the kiosk noticing?**

The Owner's requirement (2026-11-01): the kids kiosk runs OtterTube on a USB
touch monitor; an HDMI TV may mirror it; **unplugging the TV, or switching it
off, must not affect the kiosk.**

## Why sway and not cage

The wall panel runs its app under `cage`. Cage shows exactly one application
and has no way to map a touchscreen to one output or to mirror one output onto
another, so with a TV attached touches would land in the wrong place. Sway can
do both: `input type:touch map_to_output`, and per-output workspaces.

Sway has **no built-in mirroring**. `wl-mirror` (Ubuntu 24.04 universe, 0.16)
draws one output's contents fullscreen on another; `kids-outputs.sh` starts
one per extra output and kills it when that output goes away.

## What is here

| File | Role |
|---|---|
| `sway.config` | the session: OtterTube (`app_id` `ottertube`) fullscreen on workspace `kiosk`; no Xwayland, no borders, hidden cursor |
| `kids-outputs.sh` | the output reconciler: the touch output (`KIDS_TOUCH_OUTPUT`, else the first active output by name) shows `kiosk` and owns touch; every other output gets a `wl-mirror`. Runs on each sway `output` event and every 2 s |
| `kids-app-loop.sh` | restarts OtterTube when it exits |
| `run-proto.sh` | the test: sway's headless backend, then plug a "TV", unplug it (sampled every 100 ms for 3 s), plug a new one; checks OtterTube's output, visibility, fullscreen state and PID after every step |
| `run-in-docker.sh`, `Dockerfile` | runs the test in `ubuntu:24.04` with the noble packages |

```sh
bash vmtest/kids-proto/run-in-docker.sh <OtterTube>/desktop/release/linux-unpacked
```

## Result (2026-11-01, OtterTube `dev` cad7233 built as a Linux AppImage in WSL)

`FAILURES=0`: OtterTube stayed fullscreen, visible and focused on the touch
output, as the same process, through the plug, all 30 unplug samples and the
replug. The mirror appeared on the TV and reappeared on the replugged output.
Screenshots: the touch output at 1920x1080 and the TV at 1280x720, scaled.

## NOT proven

- **Real hardware.** Headless outputs are not HDMI: a TV that is switched off
  may keep its hotplug line asserted (the output stays and the mirror draws to
  nothing, which is harmless) or drop it (the case tested here). Neither has
  been seen on the spare mini-PC.
- **Touch.** The container has no input devices; `map_to_output` was issued
  but no touch was injected.
- **GPU mirroring.** wl-mirror fell back from dmabuf to screencopy because the
  container has no GPU. Its cost on the N3350's HD 500 is unmeasured.
- **OtterTube's own display logic** follows Electron's "primary display", which
  under Wayland moved to the TV whenever one was attached (its log:
  `display: on "Headless output 3" … the primary display`). Sway kept the
  window in place, so nothing visible happened, but the kiosk should configure
  OtterTube's display choice rather than rely on that.
