# Remote light UI (RDP) — shipped OFF here, activated by HomeHub

**Implements: SR-015.** **This repo defaults it OFF** (the Owner, 2026-08-27:
*"Deployer I'm okay with as long as it defaults IceDrive and remote desktop to
off from its side, and gets configured to active from the HomeHub"*). A hub
built straight from MiniPC-Deployer has no X, no XFCE and nothing on tcp/3389.

HomeHub turns it on with `REMOTE_UI_ENABLED = 'true'` in
`scripts/deploy/config.homehub.psd1`, and **that same declaration decides what
the USB carries.** See "The two halves" below — it is the part of this design
worth understanding before changing anything.

## What this layer is for

The **IceDrive Mount & Sync** GUI client. IceDrive does also ship a headless CLI
— it works, and it is a selectable mode — but it is a *mount* client rather than
a *sync* client and the vendor documents it almost not at all, so the Owner
chose the supported client: *"if there is no documentation of this, it's likely
safest just to drop back to the desktop."* Full account of what the CLI is and
is not: [../icedrive/README.md](../icedrive/README.md).

The session also exists **at boot, with nobody connected**
(`homehub-desktop-session.service`), which is what lets a GUI-only app run
unattended. That mechanism is explained in `homehub-desktop-session.sh` and was
measured across a real reboot.

## The two halves: carriage and activation

An optional feature has two decisions that happen at different times, in
different repos:

| | **Carriage** (build time, dev PC) | **Activation** (first boot, on the box) |
|---|---|---|
| desktop | the 22 names in `packages.optional.list`, baked only when `BAKE_OPTIONAL=1` | `REMOTE_UI_ENABLED=true` in `.env` → firstboot step 6c runs `setup-remote-ui.sh` |
| IceDrive | `icedrive.pin` + `export-icedrive.sh --artifact appimage` | `ICEDRIVE_MODE=appimage` → the AppImage is installed and autostarted **via `icedrive-gate.sh`** (below) |

Both are derived from **one declaration** in HomeHub, and
`Materialize-Deploy.ps1` **refuses to emit an activation whose carriage is
absent.**

> **Why that gate exists, in one paragraph.** The two halves used to be
> independent, and they disagreed for a month with nothing noticing: this
> project told operators to *"RDP in and sign in to IceDrive"* while **eleven of
> the AppImage's runtime libraries were in no image at all.** The app aborts on
> `libwebpmux`/`libwebpdemux`/`libXss`, then dies with `Could not load the Qt
> platform plugin "xcb" ... even though it was found` and a core dump — a
> message that names a *plugin* rather than a *package*, so it reads like a
> corrupt download. Only `libfuse2t64` was ever listed. **The instruction could
> not have been carried out on any hub this repo had ever built**, and nothing
> reported it because nothing had ever launched the app. All eleven are now in
> `packages.optional.list`, carried with the feature.

## What arrives on the media, and when

| | on the USB / on the box after install |
|---|---|
| `setup-remote-ui.sh`, `homehub-desktop-session.{sh,service}`, this README | **always** — staged to `/opt/homehub/stack/remote-ui/` |
| the 22 desktop + AppImage-runtime packages | **only if `REMOTE_UI_ENABLED='true'`** — baked into `/opt/homehub/apt`, installed by late-command 3c only when activated |
| the IceDrive AppImage | **only if `ICEDRIVE_MODE='appimage'`** and `APPIMAGE_SHA256` is pinned |
| an installed, running desktop | **only if activated.** Otherwise nothing here runs |

Late-command **3c-keep** registers `/opt/homehub/apt` as a `[trusted=yes]` apt
source on the target, so a box whose image carried the packages can still
install them months later with no internet.

## Connecting

From the dev PC, HomeHub's `HomeHubDesktop.cmd` opens the session and signs in
with the minted `OperatorPassword`. Or point any RDP client at `<LAN_IP>:3389`
as the hub user.

**It reconnects to the session that is already running** rather than making a
second one — measured: `++ reconnected session: username hub, display :10.0`.

> **Geometry matters.** sesman's `Policy=Default` keys a session on
> `<user, bit-depth, screen size>`, so a client arriving at a *different*
> geometry gets a SECOND session — and IceDrive would autostart there too,
> leaving two clients syncing the same folders. The unit and
> `HomeHubDesktop.cmd` both use 1600x900x24 deliberately. **`-FullScreen`
> changes the geometry and will spawn a second session.**

## What this session is, and is not

It is deliberately minimal: a window manager, a panel, a terminal and Thunar.
**`xfdesktop` is not installed**, so there is no desktop surface at all — no
wallpaper, no desktop icons, no right-click desktop menu. `~/Desktop` exists and
nothing renders it. There is no Applications-menu entry for IceDrive either; the
app reaches you through the **panel's system tray**, where it registers itself.

That is a deliberate trade (SR-015 says *light* UI), not an oversight. Adding a
launcher is a `.desktop` file in `/usr/share/applications`; adding a real
desktop means putting `xfdesktop4` into the carriage set.

## What does NOT self-heal

- **A crashed or logged-off session stops the client silently.** IceDrive's
  cloud upload has no watchdog here, and since the backup service has no offsite
  step (2026-07-29) it cannot notice either — the backup can be green while the
  cloud copy is hours behind.
- **GUI-configured state lives in the hub account's home directory.** A reimage
  wipes it. **But probably less than was long assumed:** sync pairs live in the
  *account*, not on the box (`sync-list-pairs` carries `path_local`,
  `path_remote`, `folder_id`), so they should return with the login rather than
  needing to be re-created. The long-standing "re-create sync pairs after every
  reimage" checklist is very likely wrong about that, and it has not been
  re-tested since the finding.
- **The sign-in may not be needed at all after a CLI login.** Both clients share
  `~/.config/Icedrive/Icedrive.conf`, and the GUI came up **already
  authenticated and mounted** on a box where the CLI had signed in earlier
  (observed 2026-08-27). The GUI also writes `icedrive_stored_cred`, which the
  CLI never does. On a genuinely fresh box the one-touch sign-in still applies.

## Security stance

LAN-only, exactly like Cockpit (SN-005): **never** proxy RDP through Caddy,
**never** port-forward tcp/3389 at the router. Remote use goes through the
future WireGuard path (D5). The script adds no user and no password auth surface
beyond the existing hub account. On a hub with the feature off, tcp/3389 does
not listen at all — a smaller surface than any amount of documented discipline.

## The offsite leg

IceDrive runs here and is pointed at chosen library paths in its own GUI; the
backup service performs **no offsite staging at all** (`OFFSITE_ENABLED=false`,
and `true` is refused). That is also why the backup *ingests* network shares
into the library: there is one current copy, and the client syncs it. Choose
those paths from `Personal\deploy\storage-map.md` §4e, and never point one at a
library path holding raw finance data.

The price is unchanged and is written down in
[../backup/README.md](../backup/README.md): **a green backup says nothing about
the cloud copy.** The only route that could ever close that gap is the CLI's
FUSE mount plus `OFFSITE_PATH` — and it has its own blocker (root cannot read a
`hub`-owned FUSE mount). See [../icedrive/README.md](../icedrive/README.md).

## The client's on-disk state — `~/.config/Icedrive/Icedrive.conf`

**Observed on the real hub 2026-09-01, by diffing the file across GUI actions.**
The client has no documented config format; everything here was established by
changing one thing in the GUI and seeing which key moved. Recorded because the
GUI is the only other way to answer any of it, and that needs a desktop session.

| key | meaning |
|---|---|
| `icedrive_folder_pick` | the local folder of the sync pair (`/srv/library/permtest` on this box) |
| `icedrive_sync_paused<pairId>` | `true`/`false` — the pause toggle, per pair |
| `icedrive_isync_time-<pairId>` | epoch stamp; **only moves when a sync actually runs** |
| `icedrive_local_deletion_policy` | GUI "Deletion policy (Local)" |
| `icedrive_remote_deletion_policy` | GUI "Deletion policy (Remote)" |
| `icedrive_fuse_installed` | whether the client believes FUSE support is present |
| `icedrive_exe_location` | path inside the AppImage's self-mount — changes every launch |
| `icedrive_login`, `icedrive_sessId`, `icedrive_stored_cred`, `icedrivet` | account/session material. **Do not paste these anywhere**; `icedrivet` in particular is a long token that is easy to miss when redacting. |

**THE DELETION-POLICY ENUM IS NOT SHARED BETWEEN THE TWO SIDES.** With **both**
dropdowns set to **"Delete"**, the file reads:

    icedrive_local_deletion_policy=2
    icedrive_remote_deletion_policy=1

Same label, different number. **Do not assume a value means the same thing on
the other key**, and do not set these by hand from one observation — 2026-09-01
establishes only what "Delete/Delete" looks like, not the rest of either enum.

Both keys are **absent** until the policy is set in the GUI at least once, so
their absence is "never configured", not "set to the default".

**SYNC AND MOUNT ARE INDEPENDENT FEATURES.** Un-pausing a pair does **not**
establish the FUSE mount: measured 2026-09-01, `sync_paused` went `true` ->
`false` and no cloud mount appeared, with `icedrive_fuse_installed=true`
throughout. The only IceDrive entry in `mount` at that point was
`/tmp/.mount_Icedri*`, which is the **AppImage unpacking itself** and is not a
cloud mount. If you are checking whether the drive is mounted, exclude that path
or you will get a false positive.

**A MISSING SYNC FOLDER FAILS LOUDLY, WHICH IS THE GOOD CASE.** With
`folder_pick` pointing at a path that did not exist, resume produced a GUI
error — *"Sync folder doesn't exist: /srv/library/permtest"* — and changed
nothing. Worth knowing because the folder is an ordinary directory on the
library disk: **anything that tidies up the library can delete the sync pair's
target**, and the pair is server-side state that survives it. That is the same
hazard `icedrive-gate.sh` covers for an unmounted disk, arriving by a different
route — see that section below, and note the gate does **not** check that
`folder_pick` itself still exists.

**How to tell a sync actually ran**, rather than trusting the GUI: watch
`icedrive_isync_time-<pairId>`. Pausing, resuming, and editing policy all leave
it untouched; only a real sync moves it.

## Disable / remove (on a box that had it installed)

```sh
sudo systemctl disable --now homehub-desktop-session xrdp
sudo apt-get remove --autoremove xrdp xorgxrdp xfce4-session   # pulls the rest
sudo rm -rf /opt/icedrive ~hub/.config/autostart/icedrive.desktop ~hub/.xsession
```

Then set `REMOTE_UI_ENABLED = 'false'` in HomeHub's `config.homehub.psd1` so the
next image does not put it back.


## `icedrive-gate.sh` — why the client is not started directly

The autostart entry runs `icedrive-gate.sh`, not the AppImage. It waits for the
disk the sync pairs point at to be **really mounted**, and refuses to start the
client if it never appears.

**Why it is needed.** IceDrive's sync pairs are *server-side* state. Measured on
the box 2026-08-29, across a reboot:

```
the response is: {"count":1,"pairs":[{"id":63605,"path_local":"/srv/library/permtest", …}]}
run sync threads (1); starting sync thread for syncId 63605
start watch on "/srv/library/permtest" id: 63605
```

The client signs in, asks the API, and starts scanning an **absolute path** — it
never asks whether that disk is mounted. `/srv/library` is an ordinary directory
on the eMMC that the real disk mounts over, and the generated fstab carries
`nofail`, so systemd does not wait for these disks and neither does the desktop
session. A two-way pair scanning an unmounted mountpoint sees an empty tree, and
*empty* and *deleted* are the same observation.

**Why a predicate and not a delay.** A timed delay is a guess about enumeration
speed, and open-item C27 has these enclosures dropping off the bus six times
inside two minutes — a drive present at T+60 s can be gone at T+120 s. A delay
moves the race; a check removes it.

**Why not `RequiresMountsFor=` on the session unit.** That gates the whole
graphical layer, so a missing USB disk would also cost you the remote desktop —
the one tool you would want in order to go and look.

**Behaviour**

| situation | result |
|---|---|
| every path mounted | `exec`s the client immediately |
| path absent | polls, then **refuses** at the timeout — logs to the journal via `logger`, exits 1 |
| path exists but is not a mount | same refusal — this is the dangerous case a `-d` test would miss |
| appears mid-wait | proceeds as soon as it does |
| a STAND-IN drive | **starts** — the guard's identity verdict is logged, never enforced |

Knobs: `ICEDRIVE_GATE_PATHS` (default `/srv/library`), `ICEDRIVE_GATE_TIMEOUT`
(default 600 s), `ICEDRIVE_GATE_INTERVAL` (default 5 s), `ICEDRIVE_APP`.

It reads `/proc/self/mountinfo` directly rather than calling `mountpoint`,
`findmnt` or the guard: this decides whether the household's cloud sync starts,
and it must not be able to answer "no" because a tool is missing. That is also a
pure read of a virtual file, so it can never wake a spun-down disk.

**Which disk.** The pairs live under `/srv/library`, so the LIBRARY drive is what
matters — not the backup drive, which IceDrive never touches. Waiting on
`/mnt/backup-drive` would block the cloud copy for a fault that has nothing to do
with it. Add it to `ICEDRIVE_GATE_PATHS` only if a pair is ever pointed at it.
