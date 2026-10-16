# Touch fault filter deployment and recovery

Firstboot runs `configure-touch-filter.sh`. The offline package list includes
`python3-evdev`; application code must arrive inside the stamped shell artifact.
`TOUCH_FILTER_MODE=off` is the image default. Every policy knob is documented in
`wall.env.example`; it is rendered through the application's strict validator.
The root service owns only the selected physical reader and uinput device; it
does not grant the panel user access to all input devices.

On subsequent boots systemd orders filter readiness before `getty@tty1.service`.
The sleep hook stops it before suspend and restarts it after resume. Watchdog,
disconnect and stream-desynchronization failures destroy virtual input and
release the physical grab. There is no startup blackout: `Policy.startup_ms` is 0.

Validation order: replay saved negatives and labeled human touches; run passive
`shadow`; then bench `filter` with `TOUCH_FILTER_ISOLATE=false`; only after
positive touch/drag/cancel/restart/suspend tests consider isolation. The current
negative-only replay does not authorize production isolation.

With isolation false, daemon failure releases raw input back to libinput. This
is reversible fail-open behavior for the bench and may expose phantom input.
With isolation true, the rendered udev rule sets `LIBINPUT_IGNORE_DEVICE=1` on
the physical USB/name match. Only the virtual touchscreen remains visible, even
if the daemon crashes. The helper installs that rule only after fresh daemon
readiness, then reloads/triggers udev. Restart the kiosk session to apply changed
device-discovery policy; firstboot naturally does this before kiosk startup.

## Adaptive mode (the real panel's mode since 2026-10-16)

`TOUCH_FILTER_MODE=adaptive`, `TOUCH_FILTER_ISOLATE=false`. Native touch while the
fault is quiet. When the detector confirms a phantom row it waits for an empty
frame, grabs the physical device, and writes each admitted contact to a uinput
type-B device ("OfficeWall Protected Touchscreen") 40 ms late, holds and
multi-touch intact; touches starting inside the fault row, within ~16 px of the
top edge, or shorter than 40 ms are dropped. It releases the grab 30 minutes
after the last fault. Design and evidence: HomeHub
`docs/TOUCH_PER_CONTACT_PLAN_2026-10-15.md` and OfficeWallNaglight
`touchfilter/README.md`. Needs `/dev/uinput` (loaded above); the panel image does
NOT need `PANEL_TOUCH_ADAPTIVE_TRIAL` or `PANEL_TOUCH_PANDORA_MOUSE_TRIAL`.

Health stays `protecting-fail-open` while ungrabbed (the gate below accepts it);
`status.json` adds `compositor` (`open` once cage holds the virtual device) and a
`protection` block (admitted / rejected by reason). `systemctl stop
wall-touch-filter` gives native touch back at once. **Every stop or restart is a
window of native phantoms until it re-arms (about 2 s once the fault is live).**

SSH or a service keyboard is the recovery route for isolated input. Set
`TOUCH_FILTER_MODE=off` in `/etc/wall-panel/wall.env`, then run:

```
sudo bash /opt/wall-panel/stack/autoinstall/wall/configure-touch-filter.sh
sudo systemctl restart getty@tty1.service
```

OFF disables/stops the service, removes the physical-ignore rule and sleep hook,
then reloads/triggers udev. A daemon restart alone does not remove isolation.
The kernel module may remain loaded; that does not keep a virtual device alive.
Inspect aggregate health at `/run/wall-touch-filter/status.json` and service
state with `systemctl status wall-touch-filter.service`. No live deployment or
hardware proof was performed by this implementation.

Release builders now require all six capability IDs and their actual files
inside the shell tar. Shell/site/private-gateway payloads must carry the same
full clean source revision. Hub builds stage the gateway separately and firstboot
unpacks it into `stack/panel-access/app/gateway`, outside Caddy's public document
root. Installing those files never enables the private access overlay.
