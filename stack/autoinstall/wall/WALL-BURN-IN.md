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
- [ ] **Confirm which frame the RTC keeps, on the real box** — this decides
      whether the alarm is armed with `-u` or `-l`, and getting it wrong is not
      a failure, it is a wake a whole UTC offset away (a 06:45 alarm firing at
      00:45 or 12:45, i.e. a panel that suspended and did not come back):
      ```bash
      timedatectl show -p LocalRTC --value    # expect: no  (RTC in UTC)
      cat /etc/adjtime 2>/dev/null | sed -n 3p # expect: UTC, or no file at all
      ```
      `wall-sleep.sh` reads exactly those two, in that order, and defaults to
      UTC. If this panel ever reports `yes`, nothing needs editing — but say so
      in the log, because it means the `-l` path is the one in use.
- [ ] **Prove the armed alarm is really programmed, not merely accepted**:
      after `sudo /usr/local/sbin/wall-sleep.sh start`, `cat
      /sys/class/rtc/rtc0/wakealarm` and convert it back with `date -d @<value>`.
      The script now refuses to suspend when that readback disagrees, so a panel
      that stays awake with `the alarm did not verify` in its journal is the
      guard working, not a bug.
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
      **not** randomized (`ip link show wlp1s0` matches the baseline MAC).
- [ ] Confirm Wi-Fi **powersave is off** and the panel stays SSH-able while idle
      for an hour: `iw dev wlp1s0 get power_save`.
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

## 8. The media pull (OI-15 / OI-18) — the half that needs the real shares

The mirror, its guards and the manifest generation were exercised for real against
local fixture libraries (WSL, via the script's per-flow `--bench-source FLOW=DIR`
command-line mode — fixtures under `/var/lib/wall-sync/bench`, and the mode is
refused when systemd is the caller), and the emitted manifest was fed to the
shell's real
`normalizeManifest()`. What that could **not** touch is the cifs half, the Wi-Fi
half, and library-scale data.

**THERE ARE TWO SOURCES ON TWO HOSTS (OI-18 exit (b), ruled 2026-08-03), and they
are not the same shape.** Music comes from HOMEHUB's `Media` share and lives in a
`Music/` subdirectory **under** the mount; frame video comes from Mini-serv's
dedicated `PictureFrameVideos` share and lives at the share **root**. Everything
below has to be done twice, with the right shape each time.

- [ ] **Fill in BOTH UNCs, and the ONE credential** in `/etc/wall-panel/wall.env`:
      `MEDIA_MUSIC_SHARE_UNC`, `MEDIA_FRAME_SHARE_UNC` and
      `MEDIA_FRAME_CIFS_CREDENTIALS`. **There is no music credential** — since
      2026-08-05 (storage-map Q-S7) `//homehub/Media` is an anonymous read-only
      share and the music mount presents nothing. `MEDIA_MUSIC_CIFS_CREDENTIALS`
      is REFUSED by name if a carried-over wall.env still sets it. The frame
      credentials file is root-only `0600` with `username=` / `password=` lines;
      there is no inline fallback any more, and a production image installs it
      from the materialised site payload. Until the UNCs are filled
      `wall-sync.service` **fails on every boot by design** — confirm you see
      exactly that, and that the message names the fix:
      ```bash
      systemctl status wall-sync.service; journalctl -u wall-sync -b
      ```
- [ ] **Prove BOTH mounts** from the panel, by hand, before trusting the units —
      a cifs failure and a credentials failure look the same in a service log:
      **Use the hub's FQDN, not the short name** (storage-map §1 A11(vii),
      2026-08-09). `//homehub/Media` fails here with `could not resolve address`:
      the panel's `search` is `.`, and systemd-resolved does not resolve
      single-label names over unicast DNS. That failure reads as a Samba or
      permission fault and is neither — so typing the short form at this step
      sends you to debug a share that is serving correctly.
      ```bash
      sudo mount -t cifs //homehub.<domain>/Media /mnt -o guest,ro,vers=3.0   # anonymous - no credential
      ls /mnt/Music     # the music must be UNDER the mount, in Music/
      sudo umount /mnt
      sudo mount -t cifs //MINI-SERV/PictureFrameVideos /mnt -o credentials=/etc/wall-panel/cifs-frame.creds,ro,vers=3.0
      ls /mnt           # the videos must be AT THE ROOT — no subdirectory
      sudo umount /mnt
      ```
- [ ] **Prove the frame flow's SILENT SKIP against a really-sleeping Mini-serv.**
      This is the one place the never-silent-green rule bends, so watch it bend
      correctly rather than assume it: with that box asleep, the unit must exit
      **0**, log that it is skipping and NOT waking it, and report the age of the
      cached content. Then wake the box by hand (not by the panel — the panel must
      never wake it) and confirm the next tick mirrors.
      ```bash
      sudo systemctl start wall-sync-frame.service; journalctl -u wall-sync-frame -b
      systemctl list-timers wall-sync-frame.timer
      ```
- [ ] **Prove the frame flow's OTHER half: awake-and-refused is FATAL.** With
      Mini-serv awake, put a wrong password in `cifs-frame.creds` for one run. The
      unit must FAIL and say the box answered on 445 — if this comes out as a
      silent skip, a wrong credential would be invisible forever.
- [ ] **Run the first sync on demand and watch it** — this is the run that copies
      the whole library over 802.11 from a 2016 radio, so it is also the honest
      measurement of how long a re-image costs:
      ```bash
      time sudo systemctl start wall-sync.service; journalctl -u wall-sync -b
      du -sh /var/cache/wall-media/music /var/cache/wall-media/frame
      ```
      Confirm the logged file counts match the share, and that the panel's disk has
      room for the library (256 GB total — a big `FrameVideos/` is the risk).
- [ ] **Confirm the manifests are valid and non-empty**, since everything the shell
      shows depends on them:
      ```bash
      python3 -m json.tool /var/cache/wall-media/music/index.json  | head -20
      python3 -m json.tool /var/cache/wall-media/frame/playlist.json | head
      ```
- [ ] **Confirm the kiosk user can actually READ the cache** — the sync runs as
      root, the shell does not:
      ```bash
      sudo -u panel find /var/cache/wall-media -type f ! -readable | head
      ```
      (Empty output is the pass.)
- [ ] **Prove the mirror-delete on the real share, deliberately, once.** Remove one
      file from the share, sync, and confirm it is gone from the panel and from
      `index.json`. This is the ruling's dangerous half; see it work rather than
      discover it later.
- [ ] **Prove the empty-source refusal on a real share, once.** Point
      `MEDIA_MUSIC_SHARE_UNC` at a share with no `Music/` (or an empty one), sync,
      and confirm the run FAILS and the cache is untouched. If this guard is
      broken, one bad mount erases the panel's library copy silently. Worth doing
      on the frame side too: there the guard is the ONLY thing standing between an
      empty share and a wiped cache, because the frame flow has no missing-subdir
      case at all — its content is the mount root, which always exists.
- [ ] **Then play it**: the shell must show the local library as a station and the
      frame mode must play a video. That is the end-to-end proof that this repo's
      manifests and OfficeWallNaglight's `/media/*` mapping agree — and it is the
      first point at which the *other* half of OI-15 (the Electron host serving
      `/media/*` from the cache) is exercised at all.
- [ ] **Prove the resume hook fires (OI-16a — the Owner chose "wall-sync on
      resume", 2026-07-29).** No harness can suspend real firmware, so this one
      is hardware-only: suspend, wake, and watch the sync fire *after* the wake —
      ```bash
      sudo rtcwake -m mem -l -s 90        # suspend; the RTC wakes it in ~90 s
      # after it wakes:
      journalctl -u wall-sync-resume -b   # the hook ran, at the wake timestamp
      journalctl -u wall-sync -b          # a NEW sync run STARTED after the wake
      ```
      Two things to confirm while you are there: (a) the wake itself was not
      perceptibly delayed — the hook only enqueues (`--no-block`) and must finish
      in milliseconds; (b) the sync run *succeeded* despite Wi-Fi re-association
      (the script waits up to 30 s via `nm-online`). If the sync failed with a
      mount error timed within seconds of the wake, the radio took longer than
      the wait — the honest retry is `sudo systemctl start wall-sync.service`,
      and if it happens routinely on this hardware, say so rather than tuning
      silently.
- [ ] **Prove it fires from the real nightly path too**: run
      `sudo /usr/local/sbin/wall-sleep.sh start` with `SLEEP_END` a few minutes
      out (the same test as §4's RTC check) and confirm the morning-style resume
      also triggered a sync — the D-W4 window is the wake this hook exists for.

## Bluetooth/audio feasibility gate (SR-023)

Do not set `WALL_AUDIO_ENABLED=true` as evidence that audio works. The shipped
backend intentionally reports `probe-required` and refuses every mutation.

- [ ] Record `wpctl status`, `pactl list cards`, `aplay -l`, `arecord -l`,
      Bluetooth adapters/controllers and codec/jack controls without printing
      device addresses or pairing keys into committed evidence.
- [ ] Physically prove whether the built-in jack is output or input. A playback
      control alone is not input evidence.
- [ ] Test desktop A2DP -> panel -> wired/USB output first; only then test the
      two-A2DP topology, a second adapter, USB capture, or LAN fallback in the
      approved order. Measure game latency, 30-minute dropout, reconnect,
      reboot/resume, and BLE-presence coexistence.
- [ ] Prove the chosen output monitor includes Library, Pandora and explicitly
      selected desktop input. Measure animation latency and CPU/memory, and
      verify silence and generation changes clear derived spectral state.
- [ ] Before implementing a backend, decide with evidence whether a dedicated
      `wall-audio-router` identity can reach the PipeWire user session through a
      narrow mechanism. The provisional unit uses `User=panel`; do not add a
      broad D-Bus rule merely to make a separate account convenient.
