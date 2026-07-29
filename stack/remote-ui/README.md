# Opt-in remote light UI (RDP) — for GUI-only vendor apps

**Implements: SR-015 (SN-012).** OFF by default. Nothing in the autoinstall or
first-boot path touches this directory — the core "flash → boot → zero clicks"
guarantee (SN-001) is unchanged. This is the sanctioned, *minimized* exception
SN-001 allows: a secondary service that needs a UI, set up entirely over the
LAN, never in person.

**First (and so far only) case:** the **IceDrive Mount & Sync** client.
IceDrive's current Linux client is GUI-only — no headless daemon, no CLI, and
the WebDAV fallback began sunsetting in April 2026 — so hosting the offsite
sync on the box (which is where it now lives, OI-11) requires a minimal
graphical session to launch and configure it in.

## Enable (one-time, over SSH)

```sh
# 1. On your workstation: download the Linux AppImage from icedrive.net, then
scp Icedrive.AppImage operator@<LAN_IP>:~

# 2. On the box:
sudo ICEDRIVE_APPIMAGE=~/Icedrive.AppImage bash /opt/awow-core/stack/remote-ui/setup-remote-ui.sh

# 3. From your workstation: RDP to <LAN_IP>:3389 (mstsc / Remmina) as the
#    operator user; IceDrive autostarts in the session — sign in, set the
#    sync-pair folder(s), verify a test file syncs.
```

Re-running the script is safe (idempotent). Without `ICEDRIVE_APPIMAGE` it
installs just the RDP/XFCE layer and tells you the next step.

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
  login and sync pairs live in the operator's home directory; a reimage wipes
  them. Re-setup checklist after a reimage: re-run the script → RDP in →
  sign in → re-create sync pairs → test file round-trip.

## Security stance

LAN-only, exactly like Cockpit (SN-005): **never** proxy RDP through Caddy,
**never** port-forward tcp/3389 at the router. Remote use goes through the
future WireGuard path (D5). The script adds no user, no password auth surface
beyond the existing operator account.

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
sudo rm -rf /opt/icedrive ~operator/.config/autostart/icedrive.desktop
```
