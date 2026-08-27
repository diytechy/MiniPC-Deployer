#!/usr/bin/env bash
# OPT-IN remote light UI for GUI-only vendor apps (first case: IceDrive
# Mount & Sync). NOT part of the core zero-click path: nothing in autoinstall
# or first-boot references this script — the Owner runs it over SSH, once, on
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
[ -n "$RDP_USER" ] && [ "$RDP_USER" != "root" ] || die "run via sudo from the hub account (needs a non-root user for the RDP session)"
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

# ── 3. enable + RESTART ──────────────────────────────────────────────────────
# RESTART, NOT `enable --now`, AND THAT IS THE WHOLE POINT OF THIS COMMENT.
# `apt-get install xrdp` starts the service from its postinst — so by the time
# we get here xrdp is ALREADY RUNNING, `enable --now` is a no-op, and the
# `adduser xrdp ssl-cert` above has not reached the running process. A process
# does not gain a group by being added to it; it inherits its groups at exec.
#
# The result is a box where everything reports healthy and TLS is quietly dead:
#
#   [ERROR] Cannot read private key file /etc/xrdp/key.pem: Permission denied
#   [WARN ] Cannot accept TLS connections because certificate or private key
#           file is not readable
#   [INFO ] Security protocol: configured [RDP], requested [RDP], selected [RDP]
#
# AND IT BREAKS THE LOGIN, not just the encryption, which is why this cost an
# evening. With no TLS the client cannot verify the server, so Windows refuses
# to hand over a saved credential — mstsc sends the username with an EMPTY
# password, xrdp passes that to PAM, and the operator gets "Login failed" for a
# password that is provably correct on both sides. Measured on the bench box
# 2026-08-26: /proc/<xrdp>/status showed `Groups: 113` while `id xrdp` showed
# `113(xrdp),112(ssl-cert)`.
#
# A reboot would also fix it, which is exactly why it survives a lab run and
# surfaces on someone's first real connection.
systemctl enable xrdp
systemctl restart xrdp
systemctl is-active --quiet xrdp || die "xrdp failed to start (journalctl -u xrdp)"

# PROVE the group actually took, rather than trusting the restart. This is a
# one-line check for a failure whose only other symptom is a login that refuses
# a correct password.
if ! sudo -u xrdp test -r /etc/ssl/private/ssl-cert-snakeoil.key; then
    log "WARNING: the xrdp user still cannot read the TLS private key, so this"
    log "  server will fall back to unencrypted RDP and Windows clients will NOT"
    log "  send saved credentials (you will get 'Login failed' for a correct"
    log "  password). Check:  id xrdp   and   ls -l /etc/ssl/private/"
fi
log "xrdp active — connect with any RDP client to <LAN_IP>:3389 as $RDP_USER"
log "LAN-ONLY: never proxy this through Caddy or port-forward 3389 (SN-005/SN-012)"

# ── 3b. the account password xrdp authenticates against ──────────────────────
# WITHOUT THIS THE WHOLE LAYER INSTALLS AND THEN REFUSES EVERY LOGIN. `hub` is
# key-only for SSH, so `password: "!"` from the autoinstall has never been
# replaced and `passwd -S` reports `L` (locked). xrdp authenticates through PAM
# against exactly that, so a perfect install lands on a login box that rejects
# every credential the operator owns and says only "Login failed" — with nothing
# in any log connecting the two. Measured on the bench box 2026-08-26.
#
# THE VALUE IS MINTED ON THE DEV PC, not invented here: OperatorPassword
# (GeneratedPassword) in the DPAPI store, emitted to .env as OPERATOR_PASSWORD
# by Materialize-Deploy.ps1. Same shape as FINANCE_ACTUAL_PASSWORD, which
# provision-actual.sh consumes the same way.
#
# IT IS THE SAME VALUE THE INSTALL ALREADY SET. cloud-init's `password:` field
# takes AUTOINSTALL_PASSWORD_HASH, which is sha512-crypt DERIVED from this very
# secret — so on a box installed from a current image this chpasswd is a no-op
# that re-asserts what is already true. It still runs, because it is also the
# repair path for a box installed before that (where `password:` was the locked
# "!" hash) and for a rotation that has not been reflashed.
#
# IT IS NOT AN SSH CREDENTIAL. sshd carries `passwordauthentication no`, so this
# grants the physical console and LAN xrdp and nothing remote.
#
# SILENT-SKIP IS DELIBERATE AND LOUD. If OPERATOR_PASSWORD is absent the script
# does NOT invent one — an unpredictable password nobody has recorded is worse
# than none — it says so and leaves the account as it found it.
ENV_FILE="${ENV_FILE:-/opt/homehub/stack/.env}"
if [ -r "$ENV_FILE" ]; then
    # Read ONLY the one key, and never `source` the file: .env holds every
    # secret the stack has, and sourcing it into this shell would put all of
    # them in this process's environment for anything it later execs.
    # tr -d '\r' because a value that reaches .env with a CR would set a
    # password nobody can type. The emitter writes LF only, so this is defence
    # in depth rather than a known defect - but the HomeHub launcher hit exactly
    # this shape once (PowerShell's pipeline appended CRLF to a chpasswd line,
    # and the account then rejected the real secret from every direction while
    # nothing anywhere reported a fault), so it is worth one cheap guard.
    OPERATOR_PASSWORD="$(sed -n 's/^OPERATOR_PASSWORD=//p' "$ENV_FILE" | head -1 | tr -d '\r')"
else
    OPERATOR_PASSWORD=""
    log "NOTE: no readable $ENV_FILE — cannot set the RDP password from it"
fi

if [ -n "$OPERATOR_PASSWORD" ]; then
    # STDIN, never argv: a command line is world-readable in /proc for the
    # lifetime of the process.
    printf '%s:%s\n' "$RDP_USER" "$OPERATOR_PASSWORD" | chpasswd \
        || die "chpasswd failed for $RDP_USER"
    STATE="$(passwd -S "$RDP_USER" 2>/dev/null | awk '{print $2}')"
    [ "$STATE" = "P" ] || die "password set but passwd -S still reports '$STATE' for $RDP_USER (expected P)"
    log "password set for $RDP_USER from OPERATOR_PASSWORD — xrdp can authenticate it"
    log "  (SSH is unaffected: sshd refuses password auth. This is console + LAN xrdp only.)"
else
    log "WARNING: OPERATOR_PASSWORD is not set in $ENV_FILE, so $RDP_USER still has"
    log "  no usable password and XRDP WILL REFUSE EVERY LOGIN. Nothing here invents"
    log "  one. Fix: add OperatorPassword to the deploy store (PrepDeploySecrets.ps1),"
    log "  re-materialize .env, and re-run this script — or set it by hand:"
    log "      sudo passwd $RDP_USER"
fi

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
