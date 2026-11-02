# The kids kiosk image (SN-053, SR-138, SR-139)

One USB stick turns the **spare mini-PC** (Celeron N3350, 8 GB, Intel HD 500,
two HDMI outputs, Intel 7265 Wi-Fi, a 128 GB SATA SSD) into a kids' touch
kiosk: Ubuntu 24.04 and OtterTube installed unattended, booting straight into
OtterTube full screen on a USB touch monitor. A TV on the other HDMI socket may
show the same picture; **attaching, unplugging or switching off the TV never
disturbs the kiosk** (the Owner, 2026-11-01).

It is the third image beside the hub (`../user-data`) and the wall panel
(`../wall/`). Its installer mechanisms are the panel's, step for step; what
differs is below.

## The session

```
tty1 autologin (kiosk) -> /etc/profile.d/zz-kids-kiosk.sh -> kids-kiosk.sh
   -> sway -c /etc/kids-kiosk/sway.config        restarted if it exits
        -> kids-outputs.sh                       the touch output + one mirror per other output
        -> kids-app-loop.sh                      OtterTube, restarted if it exits
```

**Sway, not cage.** Cage shows one client and can neither map a touchscreen to
one output nor mirror, so with a TV attached the touches would land on the
wrong screen. Sway maps touch with `input type:touch map_to_output`, and keeps
OtterTube on its own workspace (`kiosk`) on the touch output. Sway has no
built-in mirroring: `kids-outputs.sh` starts one `wl-mirror` per extra output
and kills it when that output goes away, before it touches anything else.

**The touch output** is `KIDS_TOUCH_OUTPUT` in `/etc/kids-kiosk/kids.env`
when that output is present: a connector such as `HDMI-A-1`, or better its
"make model serial" as `swaymsg -t get_outputs` prints them, which follows the
monitor to whichever socket it is in. Unset, the session keeps the output it
chose first (STICKY, so a TV plugged in later never takes the kiosk because
its connector sorts first), and chooses the first active output by name only
when it has none. **Set it on the real box**: with nothing configured and the
TV already on at boot, the first-by-name rule can still pick the TV.

**It ends with sway.** Sway does not kill what it exec'd. `kids-outputs.sh`
and `kids-app-loop.sh` exit when sway's IPC goes away, their output never
touches sway's stdout, and `kids-kiosk.sh` kills any survivor before starting
sway again. Without that a dead session left a script spinning a core and held
the launcher's pipe open: a black screen until reboot (the independent review,
2026-11-01).

**No key bindings, no Xwayland, no swaynag.** There is nothing to exit to.
Alt+F4 still closes OtterTube (its own escape hatch); the app loop brings it
back.

## Two accounts

`kiosk` runs the session and nothing else: no sudo, a locked password, and
`DenyUsers kiosk` in sshd, because OtterTube is a browser that loads remote
sites. `kidsops` (the installer's identity user) holds the SSH key and
NOPASSWD sudo, and nothing runs as it. SSH in as `kidsops`.

## Configuration

`/etc/kids-kiosk/kids.env` (root, 0600; see `kids.env.example`). After editing
it: `sudo /usr/local/sbin/kids-firstboot.sh && sudo systemctl restart getty@tty1`.
Firstboot writes OtterTube's `config.json` only when it does not exist, so a
parent's settings made in OtterTube are never overwritten.

## Building

```sh
bash vmtest/export-ottertube.sh --out vmtest/.out-kids/kids-app      # Node >= 24, network; a few minutes
bash vmtest/export-apt.sh --target kids --out vmtest/.out-kids/apt --src-iso <ubuntu-24.04.x-live-server-amd64.iso>
bash vmtest/build-repacked-iso.sh --target kids --src-iso <same iso>  # SIM; KIDS_SITE_DIR=<materialised dir> for production
```

Production seeds come from HomeHub (`Materialize-Deploy.ps1 -Image kids`:
SSH key, Wi-Fi, hub address, timezone, the disk serial). Run the builders on
WSL's own filesystem, not `/mnt/c`.

## Tests

| Command | Proves |
|---|---|
| `bash vmtest/test-kids-session.sh --app <tarball>` | TC-1621..1627 in `ubuntu:24.04` with exactly `packages.list`: the tarball unpacks with chrome-sandbox setuid root; every library resolves; firstboot renders correctly and leaves a parent's config alone; then, as `kiosk` with **the setuid sandbox on**, headless sway with this `sway.config`: OtterTube fullscreen on the touch output, a TV plugged (mirror shown), unplugged (30 samples over 3 s, nothing moves, the mirror is removed), replugged; same OtterTube process throughout |
| `bash vmtest/test-kids-builder.sh` | TC-1628..1629: the sim seed; each production refusal; each tarball refusal |
| `bash stack/autoinstall/kids/tests/kids-outputs.test.sh` | TC-1627: the touch-output choice (skips without `jq`) |

Each session check was mutation-tested on 2026-11-01: dropping the kiosk
workspace rule, reversing the mirror, not killing a gone TV's mirror,
overwriting `config.json`, ignoring `KIDS_TOUCH_OUTPUT`, and removing both
the outputs script's exit-with-sway and the launcher's backstop each turned a
named check red. Removing the `>/dev/null` on sway's `exec` lines (with the
backstop) did NOT: the scripts already exit with sway and release the pipe, so
that redirect is a second layer no test isolates. The builder's tarball check
was found by the first real ISO build (a live pipe into `grep -q` under
pipefail); its test now reproduces that and fails without the fix.

## NOT proven

- **Real hardware**: an install on the spare mini-PC; real HDMI hotplug (a TV
  switched off may keep its hotplug line up, so the mirror keeps drawing to it,
  which is harmless, or drop it, which is the tested case); real touch
  (the container has no input devices); Wi-Fi (Hyper-V has no radio, so the
  lab installs on a wire).
- **GPU cost**: wl-mirror fell back from dmabuf to screencopy without a GPU;
  its cost on the HD 500, and OtterTube's frame rates and video decode on the
  N3350, are unmeasured. `intel-media-va-driver` is installed so hardware decode
  can be tried without a reimage.
- **Audio**: pipewire is installed; which sink (touch monitor or TV) is used is
  wireplumber's default and has not been chosen.
- **Updates**: OtterTube's update channel is Windows-only. This image updates
  OtterTube by reimaging until a Linux channel exists (HomeHub
  `OTTERTUBE_UPDATE_CHANNEL_PLAN_2026-10-26.md` 5.6).
- **OtterTube's own display choice** follows Electron's "primary display",
  which moved to the TV whenever one appeared (its log). Sway kept the window in
  place in every test, so nothing visible happened.
- **The firmware**: the panel's Insyde boot-entry workaround is not carried; the
  spare mini-PC's firmware has not been seen to drop its boot entry.
