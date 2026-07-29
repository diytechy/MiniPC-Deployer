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
  The backup pipeline's own offsite step still fails loudly if its target is
  missing (never-silent-green), but IceDrive's cloud upload has no watchdog
  here.
- **GUI-configured state is not reproducible from this repo.** The IceDrive
  login and sync pairs live in the operator's home directory; a reimage wipes
  them. Re-setup checklist after a reimage: re-run the script → RDP in →
  sign in → re-create sync pairs → test file round-trip.

## Security stance

LAN-only, exactly like Cockpit (SN-005): **never** proxy RDP through Caddy,
**never** port-forward tcp/3389 at the router. Remote use goes through the
future WireGuard path (D5). The script adds no user, no password auth surface
beyond the existing operator account.

## The backup offsite leg — on-box is the target state

**The switch is decided** (OI-11, ratified 2026-07-25): the offsite leg runs
here. IceDrive runs on this box in the RDP session and syncs a **local** folder
straight to the cloud; the Windows box leaves the offsite path entirely.

`backup.sh` step 5 supports that target: set `OFFSITE_PATH=/abs/dir` to the
folder IceDrive syncs and the run lands the selected sets there (the upload is
the client's job). The old `OFFSITE_UNC` cifs push to a remote synced share
still works — it is the **legacy** form for a box not yet migrated — but only
one of the two may be set. See
[../backup/README.md](../backup/README.md) "Offsite target".

The one-touch caveat above is the price of this arrangement and does not go
away: after a reboot nothing uploads until an RDP session is opened, so the
backup's own step 5 can be green (files staged locally) while the cloud copy is
hours behind.

## Disable / remove

```sh
sudo systemctl disable --now xrdp
sudo apt-get remove --autoremove xrdp xorgxrdp xfce4-session   # pulls the rest
sudo rm -rf /opt/icedrive ~operator/.config/autostart/icedrive.desktop
```
