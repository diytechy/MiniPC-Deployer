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
