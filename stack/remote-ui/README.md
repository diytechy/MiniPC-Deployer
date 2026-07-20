# Opt-in remote light UI (RDP) — for GUI-only vendor apps

**Implements: SR-015 (SN-012).** OFF by default. Nothing in the autoinstall or
first-boot path touches this directory — the core "flash → boot → zero clicks"
guarantee (SN-001) is unchanged. This is the sanctioned, *minimized* exception
SN-001 allows: a secondary service that needs a UI, set up entirely over the
LAN, never in person.

**First (and so far only) case:** the **IceDrive Mount & Sync** client.
IceDrive's current Linux client is GUI-only — no headless daemon, no CLI, and
the WebDAV fallback began sunsetting in April 2026 — so hosting the offsite
sync on the box (instead of via Mini-serv's synced Samba share) requires a
minimal graphical session to launch and configure it in.

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
  disconnected session. This is the accepted one-touch deviation from SN-001 —
  if that's not acceptable, keep the Mini-serv arrangement.
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

## Scope boundary — the backup offsite leg

Today `backup.sh` step 5 pushes offsite by cifs-mounting `OFFSITE_UNC`
(Mini-serv's IceDrive-synced share). Running IceDrive on-box makes the synced
folder **local**, which step 5 cannot target yet (cifs-only). Pointing the
offsite leg at a local synced dir is a **separate, deliberately-out-of-scope
change** (a `path:`-style offsite target + sim legs + ratification) — tracked
in `docs/status.md`. Until then, on-box IceDrive and the Mini-serv share can
coexist; nothing forces a switch.

## Disable / remove

```sh
sudo systemctl disable --now xrdp
sudo apt-get remove --autoremove xrdp xorgxrdp xfce4-session   # pulls the rest
sudo rm -rf /opt/icedrive ~operator/.config/autostart/icedrive.desktop
```
