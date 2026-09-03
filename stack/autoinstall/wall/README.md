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
| `wall-sync.{sh,service}` | **the media pull (OI-15; TWO sources since OI-18)** — mirror the HOMEHUB music share and the Mini-serv frame-video share into the panel's cache. Boot-once + every resume + on-demand; **no timer on this unit** |
| `wall-sync-resume.service` | **the resume hook (OI-16a)** — `WantedBy=suspend.target`, fires on every wake from the nightly suspend and `--no-block`-starts `wall-sync.service`, detached, so the wake is never delayed |
| `wall-sync-frame.{service,timer}` | **the frame flow's own cadence (OI-18)** — `wall-sync.sh --only frame`, every minute, per storage-map §4d. Same script, same guards; a different address, credential, shape and failure policy |
| `wall-media-manifest.py` | the sync's post-step: emits the shell's `music/index.json` and `frame/playlist.json` into that cache (`--only <flow>` so one flow cannot rewrite the other's manifest) |
| `netplan-wifi.yaml.template` | rendered to `/etc/netplan/60-wall-wifi.yaml` (0600) |
| `wall-efi-fallback-sync.sh` + `wall-efi-fallback.service` | **the Insyde firmware workaround (2026-09-03)** — this panel's firmware discards the NVRAM boot entry `grub-install` creates, so it boots the spec's removable-media path, where shim has no `grubx64.efi` beside it. Installed by a **late-command**, not by firstboot, because firstboot waits on `network-online.target` and a panel that cannot boot has no network. Re-asserted from an apt hook after every upgrade and from the unit at every boot: `grub-install` restores `fbx64.efi` (the reset-loop trigger) on each grub/shim update and never maintains the fallback GRUB, so a one-shot repair would leave an unpatchable GRUB on the boot path |
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

## The media pull (OI-15, ruled by the Owner 2026-07-29; TWO sources since OI-18, ruled 2026-08-03)

> The panel's media lives on network shares; the **panel pulls** — once after
> boot, on every resume, and on demand — with **mirror semantics**; `/media/*` is
> then served panel-locally by the shell's Electron host.
>
> **OI-18 exit (b):** there are **two** sources, on **two** hosts, and the panel
> mounts each with its **own** credential. Consolidating them behind one host was
> rejected — it would re-open `HOMELAB_TOPOLOGY.md` decision 2, "the panel pulls
> frame videos from Mini-serv directly".

| | music | frame videos |
|---|---|---|
| flow (storage-map §4d) | `sync-music` | `sync-frame-videos` |
| host | **HOMEHUB** (the AWOW) | **Mini-serv** |
| UNC knob | `MEDIA_MUSIC_SHARE_UNC` | `MEDIA_FRAME_SHARE_UNC` |
| shape | the `Media` share, mirroring the **`Music/` subdir under the mount** (§3 rows 1-2) | a **dedicated** share, mirroring the **share root** — no subdir (§3b) |
| cache leaf | `music` | `frame` |
| manifest | `index.json` | `playlist.json` |
| credential file | **none** — anonymous `guest` mount (storage-map Q-S7, 2026-08-05) | `/etc/wall-panel/cifs-frame.creds` |
| cadence | boot / resume / on demand | **every minute** (`wall-sync-frame.timer`) |
| may the source sleep? | **no** — always-on | **yes**, by design |
| mount refused | **fails the unit** — a real alert | 445 probed first: **no answer = silent skip**; answered-and-refused = **fails** |

`wall-sync.sh` mounts each UNC read-only over cifs with that flow's credentials,
`rsync -a --delete`s that flow's source into `WALL_MEDIA_CACHE/{music,frame}`
(default `/var/cache/wall-media`), unmounts, and regenerates **that flow's**
contract inside the cache — `music/index.json` (`LocalLibraryProvider`'s
manifest) or `frame/playlist.json`.

**Playlists ride the mirror, not a server.** `index.json` carries a `playlists`
array built from the `.m3u`/`.m3u8` files already in the music tree: the hub's
library is the source of truth for them exactly as it is for tracks, so they
arrive on the same `rsync` and need no Navidrome, no credential and no second
sync path. Entries resolve relative to the playlist's own directory and survive
only if they name a track the same run emitted; anything escaping the cache is
refused rather than clamped. **Added 2026-08-28** — `js/music/local.js` had
consumed the key since it was written and nothing had ever produced it, which
made "the panel has no playlists" look like an argument for streaming.

**The dedicated on-demand command — unchanged, and it still means "everything":**

```bash
sudo systemctl start wall-sync.service      # BOTH flows; journalctl -u wall-sync
journalctl -u wall-sync-frame               # the every-minute frame flow
```

Things worth knowing before trusting it:

- **The two mounts are not the same shape.** Music is reached *through* the
  `Media` share and needs the `Music` subdirectory under the mount; the frame
  share's content is at its **root**. Treat them uniformly and the frame mirror
  looks one directory too deep. That map is fixed in `flow_spec()` and is
  **deliberately not a knob** — a knob there would let a typo silently widen the
  mirror onto a 2016 laptop's 256 GB disk. The *addresses* and *credentials* are
  knobs; the subtree/leaf/manifest/policy map is not.
- **The two sources have different FAILURE POLICIES**, and that is a ruling
  (storage-map §4d, A0/A10), not a setting. HOMEHUB is always-on, so a refused
  music mount is an alert. Mini-serv may sleep and is never woken, so an
  unreachable frame share is skipped. **The skip is gated on a reachability
  probe, not on the mount's return code** — `mount.cifs` returns the same rc for
  "asleep" and "wrong password", so a box that *answers* on 445 and then refuses
  the mount is a wrong share or a wrong credential and it is **fatal**.
- **A silent skip still reports the consequence.** Every successful flow stamps
  its finish time at the cache root; every skip logs how old the cached content
  is, at WARNING level past `WALL_FRAME_STALE_WARN_HOURS` (default 24). It never
  escalates to a failed unit however old the content gets — "Mini-serv has been
  off for a week" is an allowed state, and at what age it stops being allowed is
  a decision nobody has made.
- **Configuration defects are fatal for both flows.** An unset or placeholder
  UNC, or a missing/unreadable credentials file, is not a sleeping box; the frame
  flow's licence to be quiet does not extend to "nobody filled this in".
- **Mirror semantics are the ruling.** Content removed from the LAN source
  disappears from the panel on the next sync. `--delete` is irreversible from the
  panel's side — which is fine, because the cache is disposable and the library is
  the copy that matters.
- **It refuses to mirror an empty source over a populated cache** (the backup
  service's ingest step learned this the hard way): an empty *or absent* source
  fails the run loudly rather than erasing the cache.
  `WALL_SYNC_ALLOW_EMPTY=true` is the deliberate override.
- **A freshly imaged panel shows `wall-sync.service` FAILED**, because both UNCs
  ship as placeholders. That is intentional: an empty wall with a green unit
  would be a lie.
- **One flow's failure does not cancel the other.** Each is attempted, each gets
  its own verdict, and the exit status is non-zero if any failed — so a HOMEHUB
  outage cannot also stop the frame videos refreshing in the same run.
- **No timer on `wall-sync.service`, and a resume DOES sync (OI-16a, the Owner, 2026-07-29).** The
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
