# Remote light UI (RDP) — an opt-in that nothing turns on

**Implements: SR-015.** **REMOVED FROM THE IMAGE AND FROM THE BOX 2026-08-27**
(the Owner: *"GUI can be removed… from the image and from the box, along with
the auto-desktop startup"*). This reverts the 2026-08-26 ruling that shipped the
layer on by default, and restores SN-012's original shape — an opt-in a human
runs deliberately.

**SN-001's zero-click core now has no exception at all.** It used to be the
whole layer; then it was narrowed to the IceDrive sign-in; now that sign-in is a
non-interactive CLI call at provisioning and there is nothing left to except.

## Why the layer lost its only tenant

It existed for exactly one app: the **IceDrive Mount & Sync** client, believed
to be GUI-only. **It is not.** `IcedriveCLI` is a 9.8 MB headless native ELF —
no X server, non-interactive `-login`/`-password`, FUSE mount. Measured
2026-08-27; the whole account is in **[../icedrive/README.md](../icedrive/README.md)**,
which is where IceDrive lives now.

With that tenant gone, this layer was an always-listening tcp/3389, an XFCE
session, and a systemd unit that logged a desktop in at boot — none of it in
service of anything.

**Three claims that were on this page turned out to be wrong**, and they are
listed here rather than quietly deleted, because the pattern is the point: all
three were inherited and repeated rather than measured.

| the claim | what is actually true |
|---|---|
| "the client is GUI-only — no headless daemon, no CLI" | there is a CLI, and there was one the whole time |
| "after every reboot sync is DOWN until you open one RDP session" | corrected 2026-08-27 — the app needs a *display*, not a *client* — and now moot |
| "`icedrive_sessId` holds the persisted session, so the password is needed once" | that key is written after a login that **failed**. The one that matters is `icedrive_stored_cred` |

## What IS and IS NOT on the install media (2026-08-27)

| | on the USB / on the box after install |
|---|---|
| `setup-remote-ui.sh` + this README | **yes** — staged to `/opt/homehub/stack/remote-ui/` |
| `xrdp`, `xorgxrdp`, `dbus-x11`, the minimal XFCE, `xvfb`, `freerdp2-x11` | **baked, NOT installed** — moved to `packages.optional.list`. They ride in `/opt/homehub/apt` and install offline whenever someone opts in |
| `libfuse2t64` | **installed** — it did not move. The IceDrive **CLI** needs `libfuse.so.2` for its mount |
| `homehub-desktop-session.service` | **deleted.** It existed to keep a GUI app running with nobody connected |
| the IceDrive AppImage | **never again.** The pin now names the CLI: `../icedrive/icedrive.pin` |

**The offline opt-in is deliberately preserved.** Packages that are not baked
cannot be installed on a hub with no internet — `apt-get install` would reach for
the archive at the moment you run it. That was fixed on 2026-08-26 (open-items
E1(ii)) and removing the GUI does not undo it. It costs ~150–250 MB of image and
buys the ability to change your mind without a reflash.

## Turning it on, if some future vendor app needs a desktop

```sh
sudo bash /opt/homehub/stack/remote-ui/setup-remote-ui.sh
```

Idempotent, non-interactive, and safe to re-run. It installs the packages from
the baked repo, points `~/.xsession` at XFCE, joins `xrdp` to `ssl-cert`,
restarts (**not** `enable --now` — see the long comment in the script; that
distinction once cost an evening), and sets the account's UNIX password from
`OPERATOR_PASSWORD` so PAM has something to authenticate.

From the dev PC, HomeHub's `HomeHubDesktop.cmd` does the same thing and then
opens the session. **It is no longer required for anything** and its header says
so.

**A session now exists only while someone is connected** — the pre-2026-08-27
behaviour. If you ever need one at boot again, the mechanism is recorded in
git history (`homehub-desktop-session.sh`, deleted 2026-08-27): an `xfreerdp`
pointed at loopback under a throwaway `Xvfb`, with the client then dropped.
`xvfb` and `freerdp2-x11` stay in the bake for exactly that possibility.

> **Geometry still matters if you do.** sesman's `Policy=Default` keys a session
> on `<user, bit-depth, screen size>`, so a client arriving at a *different*
> geometry gets a SECOND session. `HomeHubDesktop.cmd` uses 1600x900x24
> deliberately; `-FullScreen` changes the geometry.

## Security stance

LAN-only, exactly like Cockpit (SN-005): **never** proxy RDP through Caddy,
**never** port-forward tcp/3389 at the router. Remote use goes through the
future WireGuard path (D5). The script adds no user and no password auth surface
beyond the existing hub account.

Now that nothing installs it, tcp/3389 does not listen on a stock hub at all —
which is a smaller surface than any amount of documented discipline.

## The offsite leg has moved

The old arrangement — IceDrive's GUI pointed at library paths from inside an RDP
session, with `backup.sh` doing no offsite staging at all (`OFFSITE_ENABLED=false`)
— is superseded. See [../icedrive/README.md](../icedrive/README.md), including
what a single login session still has to settle before the mount can carry the
offsite copy.

## Disable / remove (on a box that had it installed)

```sh
sudo systemctl disable --now xrdp
sudo apt-get remove --autoremove xrdp xorgxrdp xfce4-session   # pulls the rest
sudo rm -f ~hub/.xsession
```
