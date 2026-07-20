#!/usr/bin/env bash
# OPT-IN remote light UI for GUI-only vendor apps (first case: IceDrive
# Mount & Sync). NOT part of the core zero-click path: nothing in autoinstall
# or first-boot references this script — Peter runs it over SSH, once, on
# purpose (SN-001 scopes zero-click to the core; SN-012 is this deviation,
# minimized and documented).
#
# What it does (idempotent, non-interactive, loud):
#   1. apt-installs xrdp + a MINIMAL XFCE session (no full desktop meta-package)
#      + libfuse2t64 (the AppImage FUSE shim the IceDrive client still needs
#      on Ubuntu 24.04).
#   2. Points the invoking user's RDP session at XFCE (~/.xsession) and lets
#      xrdp read the TLS snakeoil key (ssl-cert group).
#   3. Enables + starts xrdp.
#   4. If ICEDRIVE_APPIMAGE (a path to an already-downloaded AppImage) is set,
#      installs it to /opt/icedrive/ and writes an XFCE autostart entry so the
#      client launches whenever the RDP session starts.
#
# What it deliberately does NOT do:
#   - download the AppImage (vendor URLs churn; fetch it from icedrive.net on
#     your workstation, scp it over, pass ICEDRIVE_APPIMAGE=/path/to/it);
#   - configure the IceDrive account/sync pairs (GUI-only, done over RDP —
#     see README.md, including what does NOT self-heal);
#   - expose anything off-LAN (like Cockpit: never proxy through Caddy, never
#     port-forward tcp/3389 at the router).
#
# Contract:
#   Inputs:  env ICEDRIVE_APPIMAGE (optional): path to the downloaded AppImage.
#            Must run as root (sudo); the RDP user is $SUDO_USER.
#   Outputs: xrdp enabled+running; ~/.xsession for the RDP user; optionally
#            /opt/icedrive/Icedrive.AppImage + the user's autostart entry.
#   Raises:  nonzero exit with a FATAL line on any failed step (fail loudly).
# Implements: SR-015 (SN-012; SN-001 opt-in exception; SN-005 LAN-only)
set -euo pipefail

log() { echo "[remote-ui] $*"; }
die() { log "FATAL: $*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo (needs apt + systemctl)"
RDP_USER="${SUDO_USER:-}"
[ -n "$RDP_USER" ] && [ "$RDP_USER" != "root" ] || die "run via sudo from the operator account (needs a non-root user for the RDP session)"
RDP_HOME="$(getent passwd "$RDP_USER" | cut -d: -f6)"
[ -d "$RDP_HOME" ] || die "home dir for $RDP_USER not found"

# ── 1. packages: xrdp + minimal XFCE + AppImage FUSE shim ────────────────────
# --no-install-recommends keeps this a LIGHT UI (no office suite, no full
# xubuntu set) — the session only exists to run a vendor GUI.
log "installing xrdp + minimal XFCE session (apt, non-interactive)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
    xrdp xorgxrdp dbus-x11 \
    xfce4-session xfwm4 xfce4-panel xfce4-terminal thunar \
    libfuse2t64

# ── 2. session wiring for the RDP user ───────────────────────────────────────
# xrdp starts whatever ~/.xsession says; without it the login lands in a black
# screen on a server install (no default session exists).
XSESSION="$RDP_HOME/.xsession"
if [ "$(cat "$XSESSION" 2>/dev/null)" != "startxfce4" ]; then
    printf 'startxfce4\n' > "$XSESSION"
    chown "$RDP_USER:" "$XSESSION"
    log "wrote $XSESSION (startxfce4)"
else
    log "$XSESSION already set"
fi
# xrdp's TLS uses the snakeoil key readable only by group ssl-cert.
adduser --quiet xrdp ssl-cert || true

# ── 3. enable + start ────────────────────────────────────────────────────────
systemctl enable --now xrdp
systemctl is-active --quiet xrdp || die "xrdp failed to start (journalctl -u xrdp)"
log "xrdp active — connect with any RDP client to <LAN_IP>:3389 as $RDP_USER"
log "LAN-ONLY: never proxy this through Caddy or port-forward 3389 (SN-005/SN-012)"

# ── 4. optional: install the IceDrive AppImage + autostart ───────────────────
if [ -n "${ICEDRIVE_APPIMAGE:-}" ]; then
    [ -f "$ICEDRIVE_APPIMAGE" ] || die "ICEDRIVE_APPIMAGE=$ICEDRIVE_APPIMAGE not found"
    install -d /opt/icedrive
    install -m 0755 "$ICEDRIVE_APPIMAGE" /opt/icedrive/Icedrive.AppImage
    AUTOSTART_DIR="$RDP_HOME/.config/autostart"
    install -d -o "$RDP_USER" -g "$(id -gn "$RDP_USER")" "$AUTOSTART_DIR"
    cat > "$AUTOSTART_DIR/icedrive.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Icedrive
Exec=/opt/icedrive/Icedrive.AppImage
X-GNOME-Autostart-enabled=true
EOF
    chown "$RDP_USER:" "$AUTOSTART_DIR/icedrive.desktop"
    log "IceDrive installed to /opt/icedrive/ + autostart entry written"
    log "NEXT (GUI, over RDP): sign in + configure sync pairs — see README.md,"
    log "including the post-reboot one-RDP-touch limitation."
else
    log "ICEDRIVE_APPIMAGE not set — skipped app install. Download the Linux"
    log "AppImage from icedrive.net on your workstation, scp it to the box, then:"
    log "  sudo ICEDRIVE_APPIMAGE=/home/$RDP_USER/Icedrive.AppImage bash $0"
fi

log "done (re-running is safe: apt/systemctl/file writes are all idempotent)"
