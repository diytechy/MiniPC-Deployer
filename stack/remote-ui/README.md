# Remote light UI (RDP) — for GUI-only vendor apps

**Implements: SR-015.** **ON by default since 2026-08-26** (the Owner: ship the
UI and IceDrive by default "now that the form of this Hub is becoming more
concrete"). This AMENDS SN-012, which made the layer an opt-in enabled by hand
over SSH.

`packages.list` carries the nine packages, so late-command 3c installs them from
the baked offline repo; **firstboot step 6c** runs `setup-remote-ui.sh` to wire
the session and install the AppImage if one rode along. The script itself is
unchanged and still works standalone — it is idempotent, and running it by hand
on an already-provisioned box is a no-op plus a re-assert.

**What this did NOT change: SN-001's zero-click core still has an exception, it
is just a narrower one.** It used to be the whole layer; now it is only the
IceDrive *sign-in* — see "What does NOT self-heal" below, which is unchanged and
is the reason defaulting this removes an errand rather than a limitation.

**First (and so far only) case:** the **IceDrive Mount & Sync** client.
IceDrive's current Linux client is GUI-only — no headless daemon, no CLI, and
the WebDAV fallback began sunsetting in April 2026 — so hosting the offsite
sync on the box (which is where it now lives, OI-11) requires a minimal
graphical session to launch and configure it in.

## What IS and IS NOT on the install media (verified 2026-08-09)

Do not assume this arrives with the image, because two thirds of it does not:

| | on the USB / on the box after install |
|---|---|
| `setup-remote-ui.sh` + this README | **yes** — staged to `/opt/homehub/stack/remote-ui/` |
| `xrdp`, `xorgxrdp`, `dbus-x11`, the minimal XFCE, `libfuse2t64` | **yes, and INSTALLED** — in `packages.list` since 2026-08-26, so they come from the baked offline repo like everything else |
| the IceDrive AppImage | **only if pinned** — see `icedrive.pin`. Unset (the shipped default) means no AppImage and a session without it |

**This used to need working internet and no longer does.** The packages were
absent from the baked repo, so `apt-get install` reached for the archive at the
moment you ran the script — meaning on a hub that installed offline the opt-in
*could not be completed at all*. Fixed 2026-08-26 in two halves, both required:
the packages are baked, and late-command **3c-keep** retains `/opt/homehub/apt`
as an apt source so the box can still see them months later. (The repo always
survived the install; nothing had ever pointed apt at it.)

## Shipping the IceDrive AppImage (once per version)

The binary is **not in this repo and is not fetched by the build**. It is
supplied once, verified against a pinned SHA256, and cached — so every build
after that is reproducible and needs no vendor at all.

**Why not fetch it at build time**, which was the first choice: icedrive.net is
behind Cloudflare and answers **403 to anything that is not a browser**
(measured 2026-08-26 — the download page and four candidate asset paths all
refused `curl` with a browser user-agent). A build that curls the vendor would
fail on a machine with perfectly good internet, which is worse than asking for
the file once. **Why not commit it:** ~100 MB of git history per version bump,
forever, for an artifact this project does not own.

```sh
# 1. Download the Linux AppImage from icedrive.net in a browser.
# 2. Get the line to paste:
bash vmtest/export-icedrive.sh --print-hash --from ~/Downloads/Icedrive.AppImage
# 3. Put VERSION= and SHA256= into stack/remote-ui/icedrive.pin
# 4. Verify, cache and stage it:
bash vmtest/export-icedrive.sh --out vmtest/.out/icedrive --from ~/Downloads/Icedrive.AppImage
# 5. Build the ISO as usual — later builds reuse the cache; --from is only
#    needed again when the pin changes.
```

An **unset pin is a valid, shipped default**: the image carries no AppImage and
the hub installs the session without it, saying so in the firstboot log. The
hash travels beside the binary onto the payload and **firstboot re-checks it
before installing** — an ISO can be re-burned and a payload can be edited, so
"we verified it at build time" is not the same claim as "these bytes are pinned".

## Connecting

From the dev PC, `HomeHubDesktop.cmd` (in HomeHub) opens the session and signs
in using the minted `OperatorPassword`. Or point any RDP client at
`<LAN_IP>:3389` as the hub user.

Then, once per reimage: **sign in to IceDrive and re-create the sync pairs.**
That is still manual and cannot be automated from here — see the next section.

Running `setup-remote-ui.sh` by hand is still safe (idempotent) if you need to
re-assert the wiring or install an AppImage onto a running box.

## What does NOT self-heal (read this before relying on it)

The sync only runs while the GUI client is running **inside a session** — but
a session no longer requires a human.

- **~~After every reboot, sync is DOWN until you open one RDP session.~~
  CORRECTED 2026-08-27 — this was never measured, and it is false.** The claim
  was inherited from OI-11 and repeated as fact in three places; open-items E1
  admits the work was blocked because *"Nothing can be built until there is a
  live client to inspect"*, so it was written with nothing to observe.

  What the app needs is a **display**, not a **client**. `homehub-desktop-session.service`
  creates a session at boot by pointing an RDP client at loopback (under a
  throwaway Xvfb, since an RDP client is itself an X app) and then dropping it.
  sesman ships `KillDisconnected=false` / `DisconnectedTimeLimit=0`, so the
  session and everything in it persist indefinitely.

  **Measured on the bench box, including across a real reboot:** every session
  killed → one created headlessly → client dropped entirely → session and its
  app survived; the box rebooted with nobody connected and came up with a live
  session on `:10`; and mstsc from the dev PC then **reconnected to that same
  session** rather than spawning a second one — sesman logged
  `++ reconnected session: username hub, display :10.0`.

  So SN-001's one-touch deviation is now only the **first** sign-in, not every
  reboot.

  > **Geometry matters.** `Policy=Default` keys a session on
  > `<user, bit-depth, screen size>`, so a client arriving at a *different*
  > geometry gets a SECOND session — and IceDrive would autostart there too,
  > leaving two clients syncing the same folders. The unit and
  > `HomeHubDesktop.cmd` both use 1600x900x24 deliberately. `-FullScreen`
  > changes the geometry and will spawn a second session.
- **A crashed/logged-off session stops sync silently on the IceDrive side.**
  IceDrive's cloud upload has no watchdog here, and since the backup service no
  longer has an offsite step (2026-07-29 correction) it cannot notice either —
  the backup can be green while the cloud copy is hours behind.
- **GUI-configured state is not reproducible from this repo.** The IceDrive
  login and sync pairs live in the hub account's home directory; a reimage wipes
  them. Re-setup checklist after a reimage: re-run the script → RDP in →
  sign in → re-create sync pairs → test file round-trip.

## The IceDrive account credential is NOT a deploy secret

**Ruled by the Owner, 2026-08-09.** It is typed into the client's GUI over RDP
and nowhere else, so it is deliberately absent from the DPAPI store and from
`FieldSchema.psd1`.

The reasoning is worth keeping, because "it is a credential, so it belongs in
the vault" is the obvious wrong answer here: **nothing in this repo can act on
it.** There is no CLI and no API to hand it to (see the GUI-only note above), so
storing it would buy no automation — it would only add a plaintext-at-rest
secret, a rotation obligation, and a store key that looks like an unfinished
task. That is exactly the shape `DataRepoDeployKey` had before it was retired:
collectable, and consumed by nothing.

Treat it like any other personal login — password manager, not deploy store.

## Security stance

LAN-only, exactly like Cockpit (SN-005): **never** proxy RDP through Caddy,
**never** port-forward tcp/3389 at the router. Remote use goes through the
future WireGuard path (D5). The script adds no user, no password auth surface
beyond the existing hub account.

## The offsite leg — the client syncs library paths, the backup stages nothing

**The corrected model (Owner, 2026-07-29)**: IceDrive runs here and is pointed
**directly at chosen library paths in its own GUI**. The backup service performs
**no offsite staging at all** — its step 5 is retired
(`OFFSITE_ENABLED=false`), which is also why the backup **ingests** network
shares into the library in the first place (there is one current copy, and the
client syncs it). The Windows box leaves the offsite path entirely.

So the sync pairs you create in step 3 above **are** the offsite configuration.
Choose them from `Personal\deploy\storage-map.md` §4e, and never point one at a
library path holding raw finance data (that data stays on the LAN).

`backup.sh` step 5 still *works* if a box is configured the old way
(`OFFSITE_PATH` local dir, or the older `OFFSITE_UNC` cifs push; exactly one) —
legacy only. See [../backup/README.md](../backup/README.md) "Offsite (step 5) —
retired from the target state".

The one-touch caveat above is the price of this arrangement and does not go
away: after a reboot nothing uploads until a session is opened. With no offsite
step in the pipeline, **the backup's NagLight report cannot see that staleness at
all** — a green backup says nothing about the cloud copy.

## Disable / remove

```sh
sudo systemctl disable --now xrdp
sudo apt-get remove --autoremove xrdp xorgxrdp xfce4-session   # pulls the rest
sudo rm -rf /opt/icedrive ~hub/.config/autostart/icedrive.desktop
```


---

## There IS a Linux CLI, and it changes what this layer is for (2026-08-27)

This document and open-items E1 both asserted the client is **"GUI-only — no
headless daemon, no CLI"**. That is **no longer true**, and it is not clear it
was ever checked rather than inherited. `IcedriveCLI-v3.62` exists and was
tested on the bench box.

**What it is, measured — not from documentation, which barely exists:**

| | |
|---|---|
| form | a **native Linux ELF**, 9.8 MB — not an AppImage (the GUI is a separate 118 MB `IcedriveMounted-v3.62-x86_64.AppImage`) |
| headless | **yes** — `--help` and a no-credential run both work with no X server at all |
| auth | `-login <username> -password <password>`, **non-interactive** |
| session | **persists** — `~/.config/Icedrive/Icedrive.conf` holds `icedrive_sessId`, so the password is needed ONCE, not per run. `-logout` clears it |
| mount | FUSE (links `libfuse.so.2` — already covered by `libfuse2t64`). Defaults to `~/Icedrive`; `-mp <path>` overrides |
| upload | a trailing `<file/folder list>` uploads those paths to the cloud |
| other | `-crypto` / `-lockcrypto` (Encrypted folder), `-clearcache`, `-verbose` |

Its full option list, verbatim:

```
-login <username>   -password <password>   -crypto      -clearcache
-lockcrypto         -logout                -verbose     -mp <path>
<file/folder list>: upload specified files and folders to the cloud
```

**NOT CONFIRMED, and do not build on it until it is:** whether the CLI performs
**continuous two-way sync** unattended. A no-credential run prints a `Sync:`
block with local/remote deletion policies, so sync exists as a concept — but
that shows *settings*, not that the CLI drives them, and nothing testable
without an account. Everything above was reachable without credentials;
everything about sync was not.

### What this changes

1. **The desktop is no longer load-bearing for IceDrive.** A mount needs no X.
   SR-015 remains right for GUI-only vendor apps in general — and the
   boot-session work stands on its own — but IceDrive may not need any of it.
2. **The "not a deploy secret" ruling (2026-08-09) has lost its reasoning.**
   That ruling turned entirely on *"there is no CLI or API to hand it to, so
   storing it buys no automation"*. `-login`/`-password` is exactly that API.
   Because the session persists, the credential would be consumed **once** at
   provisioning and never again — which is the shape every other minted secret
   here already has. **This needs a fresh ruling from the Owner; it is not
   reopened unilaterally.**
3. **A better offsite leg becomes possible.** `backup.sh`'s retired step 5 still
   supports `OFFSITE_PATH` as a local directory. Pointed at an IceDrive mount,
   the offsite copy becomes ordered after the backup, scriptable, and **visible
   to NagLight** — closing the gap this file already admits: *"a green backup
   says nothing about the cloud copy."* The GUI path can never report that.

### What it does NOT change

**Acquisition is still manual.** The CLI is behind the same Cloudflare wall as
the AppImage: `https://icedrive.net/download/linux/cli/install.sh` returns
**HTTP 403** and a `<title>Just a moment...</title>` challenge page to any
non-browser client. So the widely-quoted one-liner

```sh
curl -s https://icedrive.net/download/linux/cli/install.sh | bash    # DO NOT
```

**pipes an HTML challenge page into bash.** Both binaries come down a browser,
once, and are pinned by SHA256 exactly as `icedrive.pin` already does.

### Next step to settle it

Log in on the bench box with a real account and answer, in one sitting: does the
mount survive with no session; can sync pairs be defined without the GUI; does
`backup.sh` write into the mount cleanly; and what does `-logout`/re-login do to
a running mount. That is an hour of testing and it decides whether IceDrive uses
this layer at all.
