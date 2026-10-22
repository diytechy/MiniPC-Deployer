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

## Protection survives a reboot (SR-084)

The unit has `StateDirectory=wall-touch-filter` (`/var/lib/wall-touch-filter`,
root, `0700`); nothing else became writable. The daemon (OfficeWallNaglight
`touchfilter/persist.py`) writes its last qualifying fault renewal there as
`protection.json` (device identity, configuration digest, axis and band, the
wall-clock renewal and the expiry the 30-minute rule implies) and restores it
at start only while that expiry is still ahead by wall clock. Restoring never
renews. An orderly stop, a crash or a reboot keeps the file; expiry, OFF (this
script deletes it), a configuration change, a different device and an operator
reset (`sudo python3 -m touchfilter.persist reset`, run from the app directory)
delete it. `status.json` reports `persistence.lastOutcome` and the restored
expiry, never the band.

The unit reaches the panel only through `configure-touch-filter.sh`, which
`wall-firstboot.sh` runs on every boot (user-data does not install it), so a
system-lane paired release carries this change.

A soak can record raw input and decisions from inside the daemon (it grabs the
device once armed, so an outside reader sees nothing): `sudo touch
/run/wall-touch-filter/record.request`; output in
`/run/wall-touch-filter/record/`, bounded, off by default; details in the
application's `touchfilter/README.md`.

## Single path (`TOUCH_FILTER_SINGLE_PATH`, adaptive only, default false)

Adaptive mode hands the physical digitizer back and forth: the compositor reads
it natively until a fault is confirmed, then the daemon grabs it and protects.
A contact that begins on one path and lifts after a switch strands "down" in
Chromium, after which every tap is a second finger and nothing clicks (measured
2026-10-16 after a kiosk restart during a phantom flood: the ripple showed, no
tap acted). With `TOUCH_FILTER_SINGLE_PATH=true` the daemon keeps the grab and
passes touches through its virtual device whenever no fault is active, adopts a
restarted kiosk instead of failing open, and fails open only if no kiosk holds
the virtual device for 15 s. A daemon crash still returns native touch: the
kernel drops the grab and the compositor's own handle resumes. No isolation rule
is involved.

The rendered config carries `"single_path": true` only when the knob is true,
because a daemon that predates the key refuses the whole config. Deliver the
application release that knows it BEFORE turning the knob on. Apply as any
touch-filter change: run `configure-touch-filter.sh`, then restart
`getty@tty1.service`. Status shows `singlePath: true` and `state: PASSTHROUGH`.
Detail and the residual cases: `touchfilter/README.md` in the application repo.

Release builders now require all six capability IDs and their actual files
inside the shell tar. Shell/site/private-gateway payloads must carry the same
full clean source revision. Hub builds stage the gateway separately and firstboot
unpacks it into `stack/panel-access/app/gateway`, outside Caddy's public document
root. Installing those files never enables the private access overlay.
