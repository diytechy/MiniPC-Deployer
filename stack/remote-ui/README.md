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

The RDP layer restarts on boot, but **the sync only runs while the GUI client
is running inside a session**:

- **After every reboot, sync is DOWN until you open one RDP session.** The app
  autostarts in it; disconnect (don't log off) and it keeps running in the
  disconnected session. This is the **accepted** one-touch deviation from
  SN-001 — it was weighed and ratified with OI-11, not left open.
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
