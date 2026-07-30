# `autoinstall/wall/` — the office wall panel image (the second target)

This directory is the **wall panel variant** of this repo's autoinstall image
(OI-12 / D-W0, ratified by the Owner 2026-07-29). It is a *separate* image, not a
profile of the AWOW one: same ISO/payload/secret toolchain, different machine,
different job, **lighter gates**.

The requirement it serves is **SN-013 / SR-016 / SR-017**, not SN-001. SN-001's
zero-click bar is about always-on infrastructure that must self-heal unattended;
a wall panel is a **disposable thin client** — it holds nothing but a cache, and
when it misbehaves the answer is *reimage it*, not repair it
(`REMOTE_MANAGEMENT.md`). That is why this variant installs no Docker, no
Cockpit, and nothing that would make the panel precious.

| | AWOW core (`../user-data`) | Wall panel (`./user-data`) |
|---|---|---|
| Target | headless always-on server | one fullscreen app on a wall |
| Session | none | `cage` (Wayland kiosk), tty1 autologin |
| Containers | the whole compose stack | **none** |
| Network | ethernet DHCP | **Wi-Fi only** (no RJ45 on this hardware) |
| Remote mgmt | SSH + Cockpit + unattended-upgrades | SSH + unattended-upgrades |
| Sleeps? | never | **yes**, a nightly window (D-W4) |
| If it breaks | repair it (it holds state) | **reimage it** (it holds nothing) |
| Gate | SN-001 zero-click | SN-013 — lighter, and honest about it |

## What is here

| File | Role |
|---|---|
| `user-data` / `meta-data` | the graphical autoinstall (Subiquity + NoCloud) |
| `wall.env.example` | every panel knob; seeded to `/etc/wall-panel/wall.env` (0600) |
| `wall-firstboot.{sh,service}` | ONCE: renders the quirk fixes, Wi-Fi, sleep timers, autologin. Re-run it after editing `wall.env` — that is the supported way to change the panel |
| `wall-wakeprep.{sh,service}` | EVERY BOOT: ACPI + USB wake enablement (it does not persist) |
| `wall-sleep.sh` + `wall-sleep.service` + `wall-wake.service` | the D-W4 window; the two `.timer` files are **generated** by firstboot from `SLEEP_START`/`SLEEP_END` (systemd cannot interpolate env into `OnCalendar`) |
| `wall-kiosk.sh` | `cage` + the app, with a restart loop and a visible failure screen |
| `netplan-wifi.yaml.template` | rendered to `/etc/netplan/60-wall-wifi.yaml` (0600) |
| `WALL-BURN-IN.md` | everything only the real hardware can settle — **read it before drilling** |

On the panel the payload lands at `/opt/wall-panel/`, so these files live at
`/opt/wall-panel/stack/autoinstall/wall/`.

## The six hardware quirks, and where each is handled

A convertible laptop mounted flat against a wall is a genuinely hostile
configuration for default Linux power and input behaviour. Each quirk is
**config**, and each is applied by `wall-firstboot.sh` — except where the fix
needs a value only the running machine can supply, in which case firstboot says so
loudly and `WALL-BURN-IN.md` carries the procedure. **Nothing here guesses.**

| # | Quirk | Where |
|---|---|---|
| 1 | Lid reads CLOSED when folded → default logind suspends forever | `logind.conf.d/kiosk.conf` (firstboot §2) |
| 2 | Accelerometer auto-rotates the display *and* touch coordinates | `iio-sensor-proxy` masked (firstboot §3); orientation is native landscape/hinges-down, so nothing else is needed |
| 3 | Keyboard + touchpad face the mount; one held key is an input storm | udev rule generated from `WALL_DISABLE_INPUT` (firstboot §4) — **empty by default**: the device NAMEs are machine-specific and a wrong guess disables the touchscreen. Burn-in §3 |
| 4 / 4b | Battery removed (D-W5) → no UPS, and this BIOS has no AC-recovery | Not config at all: mount with the power button reachable, and the panel-down alert becomes required. Burn-in §6 |
| 5 | Wi-Fi power-save + MAC randomization make the panel unreachable / break its DHCP reservation | `NetworkManager/conf.d/99-wall-wifi.conf` + `macaddress: permanent` in netplan (firstboot §5) |
| 6 | Thermals in a sealed mount; sustained video is the load case | Not config: vent clearance + a measured baseline. Burn-in §6 |

## What IF-005 still owes

The kiosk session runs **one app** — the OfficeWallNaglight shell — and consumes
it as a **built artifact**, exactly as the tracker consumes `naglight:local`
(the IF-001 pattern). `IF-005` is still `Planned`, and this is the gap:

1. **No artifact.** That repo ships source and a `package.json`, not an installer
   or a release tarball. There is no build command to invoke, no versioned
   filename, and nowhere to fetch it from — so `WALL_APP_CMD` is a placeholder
   path and `wall-kiosk.sh` shows an explicit "artifact not installed" screen
   rather than a black wall.
2. **Two halves, one contract.** The *static* build is served by the AWOW's kiosk
   site (`stack/wall-shell/`); the *Electron* half runs here. Whether they ship as
   one artifact or two is undecided.
3. **Who renders `config.json`.** The shell reads `./config.json` from its own
   origin for `HEARTBEAT_URL`, `SUBSONIC`, `LOCAL_LIBRARY` and friends. That file
   is served from the AWOW side, but several of its values are panel-side secrets.
   Nothing renders it today.
4. **`/media/*` has no home** — see the `TODO(OI-15)` in `stack/caddy/Caddyfile`.
   The manifest and frame playlist must be on the shell's own origin, but the
   media itself is on the *panel's* disposable cache, which a document root on the
   AWOW cannot serve. This is an open cross-repo decision, not an omission.

Until those land, this variant is a **complete image with a missing payload** —
which is the intended half-built state at this gate, and is stated as such rather
than papered over.
