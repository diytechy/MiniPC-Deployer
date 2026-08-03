# `autoinstall/wall/` — the office wall panel image (the second target)

This directory is the **wall panel variant** of this repo's autoinstall image
(OI-12 / D-W0, ratified by the Owner 2026-07-29). It is a *separate* image, not a
profile of the hub one: same ISO/payload/secret toolchain, different machine,
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
| `wall-sync.{sh,service}` | **the media pull (OI-15)** — mirror the library share's `Music/` + `FrameVideos/` into the panel's cache. Boot-once + every resume + on-demand; **no timer** |
| `wall-sync-resume.service` | **the resume hook (OI-16a)** — `WantedBy=suspend.target`, fires on every wake from the nightly suspend and `--no-block`-starts `wall-sync.service`, detached, so the wake is never delayed |
| `wall-media-manifest.py` | the sync's post-step: emits the shell's `music/index.json` and `frame/playlist.json` into that cache |
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

## IF-005 — what landed, and the one thing still owed

The kiosk session runs **one app** — the OfficeWallNaglight shell — and consumes
it as a **built artifact**, exactly as the tracker consumes `naglight:local`
(the IF-001 pattern).

1. ~~No artifact~~ — **RESOLVED 2026-08-02 (PKG-1**, `OfficeWallNaglight
   docs/design/packaging.md`**).** `npm run dist` in that repo emits **one build
   as two payloads**, both stamped with the same source commit:
   `officewall-shell-<ver>-g<sha7>-linux-x64.tar.gz` (this machine) and
   `officewall-site-<ver>-g<sha7>.tar.gz` (the hub's kiosk site).
2. ~~Two halves, one contract~~ — **answered by the same decision.** Two
   payloads, because NagLight sends no CORS headers and the renderer must
   therefore be served by the origin that proxies `/api/*` (the hub), while the
   Electron container is a process on the panel. One build and one stamp is what
   makes a mismatch visible with `cat` instead of invisible.
3. **Who renders `config.json` — STILL OPEN, and it is now the only gap.** The
   shell reads `./config.json` from its own origin for `HEARTBEAT_URL`,
   `SUBSONIC`, `LOCAL_LIBRARY` and friends. That file is served from the hub
   side, but several of its values are panel-side secrets. The build ships
   `config.example.json` and deliberately refuses to pack a file named
   `config.json` at all. Nothing renders it today; it is deploy-time work.
4. ~~`/media/*` has no home~~ — **RESOLVED by the Owner 2026-07-29 (OI-15)**, and
   built: see "The media pull" below. `/media/*` is served **panel-locally** by
   the shell's Electron host; the kiosk site on the hub serves no `/media` route
   at all. What is still owed here is the *other* side of that ruling — the
   Electron host mapping `/media/*` onto the cache directory — which is
   OfficeWallNaglight's half, not this repo's.

Until those land, this variant is a **complete image with a missing payload** —
which is the intended half-built state at this gate, and is stated as such rather
than papered over.

## The media pull (OI-15, ruled by the Owner 2026-07-29)

> The panel's media is a network share on the hub; the **panel pulls** — once
> after boot and on demand — with **mirror semantics**; `/media/*` is then served
> panel-locally by the shell's Electron host.

`wall-sync.sh` mounts `MEDIA_SHARE_UNC` read-only over cifs, `rsync -a --delete`s
**only** the share's `Music/` and `FrameVideos/` subtrees into
`WALL_MEDIA_CACHE/{music,frame}` (default `/var/cache/wall-media`), unmounts, and
then regenerates the shell's two contracts inside that cache —
`music/index.json` (`LocalLibraryProvider`'s manifest) and `frame/playlist.json`.

**The dedicated on-demand command — this is the whole interface:**

```bash
sudo systemctl start wall-sync.service      # sync now; journalctl -u wall-sync for the log
```

Things worth knowing before trusting it:

- **Mirror semantics are the ruling.** Content removed from the LAN source
  disappears from the panel on the next sync. `--delete` is irreversible from the
  panel's side — which is fine, because the cache is disposable and the library is
  the copy that matters.
- **It refuses to mirror an empty source over a populated cache** (the backup
  service's ingest step learned this the hard way): an empty *or absent* `Music/`
  or `FrameVideos/` fails the run loudly rather than erasing the cache.
  `WALL_SYNC_ALLOW_EMPTY=true` is the deliberate override.
- **A freshly imaged panel shows `wall-sync.service` FAILED**, because
  `MEDIA_SHARE_UNC` ships as a placeholder. That is intentional: an empty wall
  with a green unit would be a lie.
- **No timer, and a resume DOES sync (OI-16a, the Owner, 2026-07-29).** The
  ruling's "once after boot" went stale in practice because a resume from the
  D-W4 window is *not* a boot — with `SLEEP_MODE=suspend` the panel can run for
  weeks without booting. The Owner chose option (a): `wall-sync-resume.service`
  (`After=suspend.target` + `WantedBy=suspend.target`, the standard systemd
  resume hook) fires on every wake and runs `systemctl start --no-block
  wall-sync.service`. `--no-block` is load-bearing: the hook runs *inside* the
  resume transaction, and a blocking start could hold it open for up to an hour
  (a first full mirror's timeout) — enqueue-and-return keeps the wake
  imperceptible and lets the sync run detached with its own journal and
  pass/fail. Wi-Fi wrinkle: `network-online.target` is not re-evaluated on
  resume, so `wall-sync.sh` does its own bounded `nm-online` wait (30 s cap,
  non-fatal) before mounting; if the radio still is not up, the mount fails
  loudly and the retry is the on-demand command.
- **The manifests are the contract, and this repo is the producer.** Their shapes
  are documented in `OfficeWallNaglight/js/music/local.js` (music) and its
  `docs/config-reference.md` (frame). One asymmetry matters: music `path` values
  are **raw** (the shell percent-encodes them itself), frame `url` values are
  **already encoded** (the shell assigns them straight to `video.src`).
