# WALL-BURN-IN — what only the real panel can settle

The wall image is **config**, and config can be reviewed, validated and (for the
kiosk site) simulated. Everything on this list cannot: it needs the actual Acer
Aspire R5-471T, bolted to (or at least sitting next to) the actual wall.

Work it **on a desk, before drilling**. Every item here is much easier to fix
before the machine is on a wall at head height with the power button behind it.

Tick a box only when you have seen it happen — the honesty rule (SN-008) applies
to this checklist too. Anything still unticked is a remainder, not a pass.

---

## 1. Quirk 1 — the lid switch

- [ ] **Fold the machine into tablet mode and confirm it does NOT suspend.** This
      is the single most likely way to end up with a panel that "looks dead on the
      wall". `wall-firstboot.sh` writes `HandleLidSwitch*=ignore`, so this is a
      verification, not a fix.
      Check: `journalctl -b | grep -i 'lid\|suspend'` shows no suspend, and
      `loginctl show-session -p IdleHint` stays sane.
- [ ] Confirm it survives a **reboot while folded** (the lid is closed the whole
      time the machine boots).

## 2. Quirk 2 — orientation

- [ ] Confirm the display stays **landscape, hinges at the bottom** through a
      fold, a reboot, and a suspend/resume. That is this panel's native
      orientation, so the fix reduces to masking `iio-sensor-proxy` — but verify
      the *touch coordinates* too, not just the image: a rotated touch layer is
      the failure that looks like "the touchscreen is broken".
- [ ] If the panel ends up mounted some other way, orientation must be pinned in
      the compositor instead (`cage` inherits the DRM rotation; the practical
      route is a `WLR_OUTPUT` / kernel `video=` rotation). Not configured here
      because the decided orientation needs nothing.

## 3. Quirk 3 — disabling the internal keyboard + touchpad

`WALL_DISABLE_INPUT` in `/etc/wall-panel/wall.env` is **empty in the shipped
template on purpose**: the udev rule matches device NAMEs, and guessing them
risks disabling the touchscreen — which would leave the panel with no input at
all. Read them off the machine:

```bash
# every input device, with the exact ATTRS{name} udev matches on
sudo libinput list-devices | sed -n 's/^Device:\s*/NAME: /p'
# or, straight from sysfs:
cat /sys/class/input/event*/device/name
```

- [ ] Identify which name is the **internal keyboard**, which is the
      **touchpad**, and — critically — which is the **touchscreen** (an ELAN
      device on this unit).
- [ ] Set `WALL_DISABLE_INPUT="<keyboard name>|<touchpad name>"` (pipe-separated;
      the names contain spaces) and re-run `sudo /usr/local/sbin/wall-firstboot.sh`.
- [ ] Confirm the touchscreen still works and the keyboard/touchpad do not.
- [ ] **Write the re-enable one-liner somewhere you will find it in a panic** —
      it is also printed by firstboot and embedded as a comment in the generated
      rule file:
      ```bash
      sudo rm /etc/udev/rules.d/99-wall-disable-internal-input.rules \
        && sudo udevadm control --reload \
        && sudo udevadm trigger --subsystem-match=input
      ```

## 4. D-W4 — the sleep window, and the wake that must never fail

- [ ] `cat /sys/power/mem_sleep` — if it reads `[s2idle] deep`, apply the kernel
      cmdline change from the wall `user-data` §6 and reboot. s2idle on this
      hardware generation often saves little more than backlight-off, which
      defeats `SLEEP_MODE=suspend` entirely.
- [ ] **Prove a full suspend/resume cycle many times over** — the hwprobe run
      passed once on 2026-07-26, which is not the same thing. Failed resume
      (wakes to a black screen, or does not wake) is the classic failure mode on
      2016 consumer firmware, and it is the reason `SLEEP_MODE=backlight` exists.
      ```bash
      for i in $(seq 1 20); do sudo rtcwake -m mem -l -s 60 && sleep 30; done
      ```
- [ ] **Prove the RTC alarm wakes it from the real path**: run
      `sudo /usr/local/sbin/wall-sleep.sh start` with `SLEEP_END` a few minutes
      out, and confirm it comes back on its own with nobody touching anything.
      This is the primary wake; if it is not reliable, nothing else about the
      sleep window is safe.
- [ ] **Prove the USB mouse wakes it** (the manual/early wake). Then confirm the
      thing everyone gets wrong: **tapping the touchscreen does NOT wake it from
      S3.** That is expected — I2C-HID devices generally cannot be a system wake
      source — and it is exactly why the mouse is required rather than optional.
      Hang the mouse where a person will find it.
- [ ] Confirm `wall-wakeprep` re-armed everything after a reboot:
      `journalctl -u wall-wakeprep -b` and `cat /proc/acpi/wakeup | grep XHC`.
- [ ] Decide and configure the **Uptime-Kuma push monitor's maintenance window**
      to match `SLEEP_START`/`SLEEP_END`. A suspended panel stops pushing, so an
      untaught monitor goes red every single night — and a nightly false alarm
      trains you to ignore the one signal whose job is to tell you the panel
      actually died. This is server-side (Kuma's UI + the Kuma→ntfy notifier),
      not something the image can do.

## 5. The kiosk session

- [ ] Confirm tty1 autologin lands in `cage` with the app fullscreen, and that an
      **SSH login does NOT** start a second compositor (the profile hook is
      tty1-only).
- [ ] Confirm the session **restarts by itself** when the app is killed
      (`pkill -f wall-shell`) — the panel must heal without a visit.
- [ ] If cage cannot open the DRM device (`Cannot open DRM device` / no seat), the
      documented fallback is the installed `seatd`: enable `seatd.service`, add
      `panel` to the `_seatd` group, and run cage under it. Only reach for this if
      the logind-seat path fails; do not pre-emptively switch.
- [ ] Pin the **audio sink** — auto-selection will pick HDMI or the wall-facing
      internal speakers at the worst possible moment. Procedure lives in
      OfficeWallNaglight's `docs/runbooks/audio-bluetooth.md`.
- [ ] Do the **one-time Pandora sign-in** into the persistent session partition
      (that repo's `docs/runbooks/pandora-signin.md`). Note it does not survive a
      reimage.

## 6. Quirk 6 — thermals, and quirk 4b — no UPS

- [ ] `vainfo` — confirm **VA-API decode is actually engaged**, not assumed.
      Software-decoding 1080p continuously is precisely the sustained load that
      cooks a U-class chip behind a wall.
- [ ] Run video for **an hour in the mounted position** and record steady-state
      temperatures (`sensors`, or `/sys/class/thermal/thermal_zone*/temp`). The
      2026-07-26 stress test ran clean but the numbers went unrecorded, so there
      is no baseline to compare against yet — take one.
- [ ] Confirm **vent clearance** in the final mount. Blocked bottom vents is the
      failure mode, and it is invisible until it is thermal throttling.
- [ ] **Pull the plug** while it is running, twice. Confirm it (a) boots clean
      without fsck blocking at a prompt — there is no keyboard to answer one —
      and (b) note that it may simply **stay off** until someone presses the
      power button: this BIOS has no AC-recovery setting (confirmed 2026-07-26),
      and a smart plug cannot press a button.
- [ ] Therefore: **mount with the power button reachable**, and verify the reach
      before drilling.
- [ ] Confirm the battery is out and its terminals are taped (D-W5, done
      2026-07-26 — tick when it has actually gone to recycling).

## 7. Network + the kiosk site (the half the sim could not reach)

- [ ] Give the panel a **DHCP reservation on its hardware MAC** and confirm the
      lease is the address in `PANEL_IP`. The `/32` allow-list is keyed to it.
- [ ] Confirm `WIFI_SSID`/`WIFI_PSK` are live from netplan and that the MAC is
      **not** randomized (`ip link show wlan0` matches the baseline MAC).
- [ ] Confirm Wi-Fi **powersave is off** and the panel stays SSH-able while idle
      for an hour: `iw dev wlan0 get power_save`.
- [ ] From the panel: `curl -sI https://$WALL_HOST:$WALL_PORT/api/today` → 200,
      and the shell loads. From **any other LAN device**: 403.
- [ ] **THE OFF-LAN TEST (the one remainder the V1 sim genuinely cannot cover).**
      From a phone on cellular, off the home network entirely:
      ```bash
      curl -sv https://$WALL_HOST:$WALL_PORT/            # must NOT connect at all
      curl -s  https://$WALL_HOST/                       # :443 — must be the normal 404/403 path
      ```
      The name resolves publicly (the DDNS wildcard), so this is load-bearing: it
      must fail to connect because the router does not forward the port, and the
      `/32` + LAN-bound publish are the second and third layers behind that.
- [ ] Confirm the **certificate** for `WALL_HOST` issued. It is validated over the
      existing `:80`/`:443` listeners (ACME CAs never contact non-standard ports),
      so if it did not issue, look at port 80 reachability — not at `WALL_PORT`.
      `docker exec caddy caddy list-certificates` on the AWOW, or just load the
      site and check for a trusted padlock from the panel.
