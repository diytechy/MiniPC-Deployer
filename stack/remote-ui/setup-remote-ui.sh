#!/usr/bin/env bash
# Remote light UI for GUI-only vendor apps (first case: IceDrive Mount & Sync).
#
# ACTIVATED BY .env, NOT BY THIS REPO (the Owner, 2026-08-27): MiniPC-Deployer
# defaults the remote desktop OFF and HomeHub turns it on. firstboot step 6c
# calls this ONLY when REMOTE_UI_ENABLED=true, which comes from HomeHub's
# config.homehub.psd1. On a hub built straight from this repo, nothing here runs
# and tcp/3389 never listens.
#
# ITS PACKAGES ARE CARRIED THE SAME WAY. The 22 names this needs - the RDP/XFCE
# set, the eleven Qt/xcb libraries the AppImage links against, and libfuse2t64 -
# are in packages.optional.list, which export-apt.sh bakes only when the build
# passes BAKE_OPTIONAL=1. Same declaration, same source of truth, so the apt-get
# below resolves offline when the feature is on and would need the archive when
# it is not.
#
# ELEVEN OF THOSE LIBRARIES WERE MISSING FOR A MONTH, and that is why the two
# halves are now tied together at source. The project told operators to "RDP in
# and sign in to IceDrive" while the AppImage could not start on any hub this
# repo had ever built: it aborts on libwebpmux/libwebpdemux/libXss, then dies
# with "Could not load the Qt platform plugin xcb ... even though it was found"
# and a core dump. The message names a plugin rather than a package, so it reads
# like a corrupt download. Nothing noticed because nothing had ever launched it.
#
# THIS SCRIPT IS ALSO THE MANUAL DOOR, unchanged and idempotent: HomeHub's
# HomeHubDesktop.cmd runs it over SSH, and running it by hand on a provisioned
# box is a no-op plus a re-assert.
#
# What it does (idempotent, non-interactive, loud):
#   1. apt-installs xrdp + a MINIMAL XFCE session (no full desktop meta-package)
#      + the AppImage's runtime libraries.
#   2. Points the invoking user's RDP session at XFCE (~/.xsession) and lets
#      xrdp read the TLS snakeoil key (ssl-cert group).
#   3. Enables + RESTARTS xrdp, and sets the account password from
#      OPERATOR_PASSWORD so PAM has something to authenticate.
#   4. If ICEDRIVE_APPIMAGE is set (firstboot passes it after re-checking the
#      pinned sha256), installs it to /opt/icedrive/ and writes an XFCE autostart
#      entry so the client launches whenever the session starts.
#   5. Installs the boot-time session unit, so that autostarted client runs with
#      nobody connected.
#
# What it deliberately does NOT do:
#   - download the AppImage. icedrive.net is behind Cloudflare and answers 403
#     to anything that is not a browser, so nothing here or in the build can
#     fetch it. It arrives pinned into the image (stack/icedrive/icedrive.pin)
#     or by hand: scp it over and pass ICEDRIVE_APPIMAGE=/path/to/it;
#   - configure the IceDrive account or its sync pairs. That is GUI-only and is
#     the one remaining hands-on step after a reimage;
#   - expose anything off-LAN (like Cockpit: never proxy through Caddy, never
#     port-forward tcp/3389 at the router).
#
# Contract:
#   Inputs:  env ICEDRIVE_APPIMAGE (optional): path to a verified AppImage.
#            Must run as root (sudo); the RDP user is $SUDO_USER.
#   Outputs: xrdp enabled+running; ~/.xsession for the RDP user; that account's
#            UNIX password set from OPERATOR_PASSWORD; optionally
#            /opt/icedrive/Icedrive.AppImage + autostart + the session unit.
#   Raises:  nonzero exit with a FATAL line on any failed step (fail loudly).
# Implements: SR-015 (activation moved to HomeHub 2026-08-27; SN-005 LAN-only)
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
# READ THE LIST, NEVER RETYPE IT - and this file is why that rule exists.
# Until 2026-08-27 the names were HARDCODED here, and on that date eleven
# Qt/xcb runtime libraries plus xvfb and freerdp2-x11 were added to
# packages.optional.list so the image would CARRY them. This line was never
# updated. Measured on a real box the same day:
#
#   * all 22 were baked into the offline repo and asserted present on the ISO;
#   * exactly the 9 named here were installed;
#   * the AppImage still died on "libwebpmux.so.3: cannot open shared object
#     file" - the identical defect the eleven were added to fix; and
#   * Xvfb and xfreerdp were absent, so the boot-time session could not run.
#
# The feature was carried, verified, and non-functional. Nothing reported it,
# because carriage and installation were TWO lists and only one was checked.
# So there is now ONE list, and it is the one the bake already used.
log "installing the optional feature set (apt, non-interactive, from the baked repo)"
export DEBIAN_FRONTEND=noninteractive
OPT_LIST="${OPT_LIST:-/opt/homehub/stack/autoinstall/packages.optional.list}"
[ -f "$OPT_LIST" ] || OPT_LIST="$(dirname "$0")/../autoinstall/packages.optional.list"
# NO SILENT FALLBACK TO A BUILT-IN LIST. A hardcoded default is precisely what
# drifted, invisibly, for the whole life of the feature.
[ -f "$OPT_LIST" ] || die "packages.optional.list not found (looked in /opt/homehub/stack/autoinstall/ and beside this script). It is the list export-apt.sh baked; without it there is no way to know what this feature needs."
OPT_PKGS="$(sed 's/#.*//' "$OPT_LIST" | awk 'NF{print $1}' | tr '
' ' ')"
[ -n "$OPT_PKGS" ] || die "$OPT_LIST parses to no package names - refusing to install nothing and call it success."
log "  $(printf '%s' "$OPT_PKGS" | wc -w) package(s) from $(basename "$OPT_LIST")"
apt-get update -q
# shellcheck disable=SC2086
apt-get install -y -q --no-install-recommends $OPT_PKGS

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
# RESTART ONLY WHEN THE GROUP IS ACTUALLY MISSING (2026-08-27). Everything
# above stays true, but an UNCONDITIONAL restart here has a second effect that
# cost the same evening back again: firstboot runs step 6c on EVERY boot, so
# this restarted xrdp roughly 35 s after homehub-desktop-session.service had
# just created the boot-time session. Restarting xrdp destroys sesman's
# in-memory session list, which ORPHANS that session: the Xorg keeps running,
# sesman no longer knows about it, and every later RDP connection therefore
# starts a SECOND session whose xfce4-session collides with the orphan and
# exits after one second. Measured on the box: session created 21:25:12,
# sesman restarted 21:25:47, and every connection after that died in 1 s with
# 'Window manager exited quickly'.
#
# So ask whether the restart is NEEDED rather than doing it every time. The
# condition is the exact thing the restart exists to fix: does the RUNNING
# xrdp process carry the ssl-cert gid?
systemctl enable xrdp
SSLCERT_GID="$(getent group ssl-cert | cut -d: -f3)"
XRDP_PID="$(systemctl show -p MainPID --value xrdp 2>/dev/null)"
_need_restart=1
if [ -n "$SSLCERT_GID" ] && [ -n "$XRDP_PID" ] && [ "$XRDP_PID" != 0 ] \n   && awk -v g="$SSLCERT_GID" '/^Groups:/{for(i=2;i<=NF;i++) if($i==g) f=1} END{exit !f}' \n        "/proc/$XRDP_PID/status" 2>/dev/null; then
    _need_restart=0
fi
if [ "$_need_restart" -eq 1 ]; then
    log "restarting xrdp so the ssl-cert group takes effect"
    systemctl restart xrdp
else
    log "xrdp already runs with the ssl-cert group - NOT restarting"
    log "  (a restart here would orphan a boot-time session that already exists)"
fi
systemctl is-active --quiet xrdp || die "xrdp failed to start (journalctl -u xrdp)"

# ── STOP needrestart RESTARTING xrdp BEHIND OUR BACKS (2026-09-01, C59) ──────
# The conditional restart above guards THIS script's restart. It does nothing
# about anyone else's, and the most frequent "anyone else" is automatic:
# needrestart, invoked by the DAILY apt-daily-upgrade.timer, restarts any
# daemon linked against an upgraded library. On 2026-09-01 that was libbz2-1.0
# — xrdp itself was NOT upgraded (zero xrdp entries in apt history) — and it
# orphaned a desktop session created the night before. Every RDP connection
# after that died in one second, blaming the window manager.
#
# Without this file a reimaged box is one library patch away from the same
# wedge, with nobody having touched it. The file carries the full reasoning and
# the stated trade-off (xrdp keeps the old library mapped until reboot;
# acceptable because 3389 is LAN-only, SN-005).
if [ -d /etc/needrestart ]; then
    install -d -m0755 -o root -g root /etc/needrestart/conf.d
    install -m0644 -o root -g root "$(dirname "$0")/90-homehub-xrdp.conf"         /etc/needrestart/conf.d/90-homehub-xrdp.conf
    # PROVE it parses. A malformed file here is read by needrestart on every
    # apt run, and a Perl syntax error there is someone else's confusing
    # failure at 06:54 rather than ours now.
    if command -v perl >/dev/null 2>&1        && ! perl -c /etc/needrestart/conf.d/90-homehub-xrdp.conf >/dev/null 2>&1; then
        die "90-homehub-xrdp.conf failed perl -c after install"
    fi
    log "needrestart: xrdp/xrdp-sesman excluded from automatic restart (C59)"
else
    log "needrestart not installed - skipping the xrdp restart exclusion"
    log "  (if it is installed later, re-run this script or copy"
    log "   stack/remote-ui/90-homehub-xrdp.conf into /etc/needrestart/conf.d/)"
fi

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
log "LAN-ONLY: never proxy this through Caddy or port-forward 3389 (SN-005)"

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

# ── 3c. the boot-time session, so the GUI app runs with nobody connected ─────
# THIS IS WHAT MAKES ICEDRIVE UNATTENDED. Installing the unit is all that is
# needed; homehub-desktop-session.sh explains the mechanism and what was
# measured to establish it. Enabled but NOT started here - firstboot is still
# provisioning at this point, and a session created now would be torn down by
# the reboot that usually follows anyway.
if [ -f "$(dirname "$0")/homehub-desktop-session.service" ]; then
    install -m0755 -o root -g root "$(dirname "$0")/homehub-desktop-session.sh"         /opt/homehub/stack/remote-ui/homehub-desktop-session.sh 2>/dev/null || true
    install -m0755 -o root -g root "$(dirname "$0")/reap-orphan-sesman.sh" /opt/homehub/stack/remote-ui/reap-orphan-sesman.sh 2>/dev/null || true
    install -m0644 -o root -g root "$(dirname "$0")/homehub-desktop-session.service"         /etc/systemd/system/homehub-desktop-session.service
    systemctl daemon-reload
    systemctl enable homehub-desktop-session.service >/dev/null 2>&1
    # ENABLE IS NOT START, and on a FIRST INSTALL that difference is the whole
    # feature. This unit is WantedBy=multi-user.target, which the box passed
    # long before firstboot reached here, so enabling it schedules a session for
    # the NEXT boot and creates none now. An earlier comment justified that with
    # "the reboot that usually follows anyway" — there is no such reboot (that
    # claim was corrected in f783ff3), so the measured result on 2026-08-27 was
    # a unit sitting "enabled, inactive (dead)" with NO journal entries at all,
    # and a freshly installed box with no desktop and no running GUI app.
    # --no-block IS LOAD-BEARING, and leaving it out DEADLOCKS THE BOOT.
    # This script runs INSIDE homehub-firstboot.service, and the unit it is
    # starting is ordered `After=homehub-firstboot.service`. A blocking
    # `systemctl start` therefore waits for a unit that cannot begin until this
    # script's own service finishes - and it never will, because it is waiting
    # here. Measured 2026-08-27 on a clean install: firstboot sat in
    # `activating` for 18 minutes with `systemctl start` blocked, the session
    # unit inactive, and `multi-user.target start waiting` in the job queue.
    # The whole boot was wedged by two fixes that were each correct alone.
    #
    # --no-block ENQUEUES the job and returns. systemd runs it as soon as the
    # ordering allows, i.e. the moment firstboot completes. So the ordering
    # guarantee is kept and the deadlock is not possible.
    if systemctl start --no-block homehub-desktop-session.service >/dev/null 2>&1; then
        log "boot-time session unit enabled, and its start QUEUED (--no-block)"
        log "  systemd runs it the moment firstboot finishes - it cannot run"
        log "  sooner, because the unit is ordered After=homehub-firstboot"
        log "  check it afterwards with:"
        log "    systemctl is-active homehub-desktop-session.service"
    else
        log "  WARNING: could not queue the session unit start."
        log "    This box will have no desktop until it reboots. Diagnose with:"
        log "      systemctl status homehub-desktop-session.service"
        log "      journalctl -u homehub-desktop-session.service"
    fi
else
    log "NOTE: no homehub-desktop-session.service beside this script — the session"
    log "  will exist only while someone is connected (the pre-2026-08-27 behaviour)"
fi

# ── 4. optional: install the IceDrive AppImage + autostart ───────────────────
if [ -n "${ICEDRIVE_APPIMAGE:-}" ]; then
    [ -f "$ICEDRIVE_APPIMAGE" ] || die "ICEDRIVE_APPIMAGE=$ICEDRIVE_APPIMAGE not found"
    install -d /opt/icedrive
    install -m 0755 "$ICEDRIVE_APPIMAGE" /opt/icedrive/Icedrive.AppImage
    AUTOSTART_DIR="$RDP_HOME/.config/autostart"
    # OWN EVERY LEVEL, NOT JUST THE LEAF. `install -d -o user a/b/c` creates the
    # parents as ROOT and applies -o/-g only to the final component, so this
    # line used to leave ~/.config owned by root:root with ~/.config/autostart
    # owned by the operator. That one wrong owner broke the entire desktop:
    #
    #   xfconfd (running as the operator) could not create its config dir, so
    #   it started NO backends; xfce4-session could then not read
    #   FailsafeSessionName and died with "Unable to load a failsafe session";
    #   and because no session ever came up, the autostart entry written two
    #   lines below NEVER RAN - which is precisely why IceDrive was installed,
    #   pinned, autostart-registered and still not running.
    #
    # Measured on a real box 2026-08-27: `chown hub:hub ~/.config` alone took
    # xfconf-query from "No backends could be started" to listing the session
    # channel. Unconditional, so a box provisioned by the old code is repaired
    # the next time this runs.
    RDP_GROUP="$(id -gn "$RDP_USER")"
    for _d in "$RDP_HOME/.config" "$AUTOSTART_DIR"; do
        install -d -o "$RDP_USER" -g "$RDP_GROUP" "$_d"
        chown "$RDP_USER:$RDP_GROUP" "$_d"
    done
    # THE AUTOSTART ENTRY POINTS AT THE GATE, NOT AT THE APP. IceDrive's sync
    # pairs are SERVER-SIDE: it signs in, asks the API and starts a sync thread
    # against an absolute `path_local` without ever asking whether that disk is
    # mounted. `/srv/library` is a directory on the eMMC that the real disk
    # mounts over, and the fstab entries carry `nofail`, so the session can
    # legitimately start before a slow USB enclosure has enumerated. See
    # icedrive-gate.sh for why this is a predicate and not a delay.
    # `$(dirname "$0")` is this file's idiom for its own directory (see the
    # session-unit install above); there is no $HERE here, and under `set -u`
    # referencing one would abort the whole opt-in.
    if [ -f "$(dirname "$0")/icedrive-gate.sh" ]; then
        install -m 0755 "$(dirname "$0")/icedrive-gate.sh" /opt/icedrive/icedrive-gate.sh
        ICEDRIVE_EXEC=/opt/icedrive/icedrive-gate.sh
    else
        # A payload that predates the gate must still produce a WORKING
        # autostart entry rather than one pointing at a file that is not
        # there - which would leave the client silently never starting.
        log "WARNING: icedrive-gate.sh is not beside this script - autostarting the app directly."
        log "  The client will then start before the library disk is verified mounted."
        ICEDRIVE_EXEC=/opt/icedrive/Icedrive.AppImage
    fi
    cat > "$AUTOSTART_DIR/icedrive.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Icedrive
Exec=__ICEDRIVE_EXEC__
X-GNOME-Autostart-enabled=true
EOF
    sed -i "s|__ICEDRIVE_EXEC__|$ICEDRIVE_EXEC|" "$AUTOSTART_DIR/icedrive.desktop"
    chown "$RDP_USER:" "$AUTOSTART_DIR/icedrive.desktop"
    log "IceDrive installed to /opt/icedrive/ + autostart entry written (via icedrive-gate.sh)"
    log "  the gate waits for \${ICEDRIVE_GATE_PATHS:-/srv/library} to be really mounted"
    log "  and REFUSES to start the client if it never appears - see its header"
    log "NEXT (GUI, over RDP): sign in + configure sync pairs — see README.md,"
    log "including the post-reboot one-RDP-touch limitation."
else
    log "ICEDRIVE_APPIMAGE not set — skipped app install. Download the Linux"
    log "AppImage from icedrive.net on your workstation, scp it to the box, then:"
    log "  sudo ICEDRIVE_APPIMAGE=/home/$RDP_USER/Icedrive.AppImage bash $0"
fi

log "done (re-running is safe: apt/systemctl/file writes are all idempotent)"
