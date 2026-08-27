#!/usr/bin/env bash
# Provision the IceDrive CLI: install the pinned binary, sign in ONCE, mount.
#
# WHAT REPLACED WHAT. Until 2026-08-27 IceDrive on this box was a GUI AppImage
# autostarted inside an xrdp/XFCE session that a systemd unit created at boot so
# the client would run with nobody connected. All of that is gone. `IcedriveCLI`
# is a native ELF that needs no X server, takes its credentials on the command
# line, and FUSE-mounts the cloud. Measured on 2026-08-27 rather than assumed:
#   * `ldd` lists no X11, xcb, Wayland or Qt GUI library at all;
#   * it runs identically with DISPLAY unset, set to a dead display, or set;
#   * `-login <user> -password <pw>` is fully non-interactive - it reaches the
#     API and returns a real answer with stdin closed.
#
# THE PASSWORD MUST GO ON ARGV, and that is a measured constraint rather than a
# lazy choice. Three ways to avoid it were tried and NONE work: a pipe on stdin
# ("cli: no password given"), a pty via `script` (same message), and pre-seeding
# the config file (the stored credential is encrypted by the app itself, with a
# key we do not have). `-password` is the only non-interactive path it offers.
#
# WHY THAT IS ACCEPTABLE HERE. /proc carries no hidepid, so argv is world-
# readable for the ~2 s of the login. On this box `hub` is the only account with
# a real shell - every other uid >= 1000 is nologin - and `hub` already has
# `(ALL) NOPASSWD: ALL`, so it can simply `sudo cat /opt/homehub/stack/.env` and
# read the same value. The exposure grants nothing that is not already granted.
# That is the Owner standing threat-model ruling (2026-08-09): a credential is
# not a finding for living where the deployment mechanism already puts it.
# THE ONE CONDITION THAT INVALIDATES IT: a second interactive account.
#
# AND IT IS A ONE-TIME EXPOSURE, not a per-boot one. PROVEN on the bench box
# 2026-08-27: after one `-login`/`-password` run, a second run with NO
# credentials at all authenticated and mounted. The login writes two keys into
# ~/.config/Icedrive/Icedrive.conf - `icedrive_login` and `icedrivet` (the
# token) - and those are what a later run reads.
#
# IT IS **NOT** `icedrive_stored_cred`, which is what the first version of this
# script guarded on, from reading the binary rather than running it. That key
# belongs to the GUI "save my password" checkbox and is never written here, so
# the guard reported failure after a login that had provably succeeded - the run
# mounted the drive and read the account's storage stats. Guarding on a key the
# app does not use is indistinguishable from the layer being broken.
#
# The mount unit therefore carries NO password, and that is now a measured
# claim rather than a hope.
#
# SHIPPED OFF. With ICEDRIVE_USER/ICEDRIVE_PASSWORD absent from .env - the
# default - this installs the binary and stops. It does not log in, does not
# mount, and does not enable anything. Nothing new runs on a box until the
# operator puts a credential in the deploy store.
#
# What it does (idempotent, non-interactive, loud):
#   1. Installs the pinned CLI to /opt/icedrive/icedrive, re-checking its
#      sha256 on the box before trusting the bytes.
#   2. Creates the mount point /srv/icedrive, owned by the hub account.
#   3. If ICEDRIVE_USER and ICEDRIVE_PASSWORD are both set: signs in once as the
#      hub account, and on success installs + enables homehub-icedrive.service.
#      On failure it says why and leaves the unit disabled.
#
# Contract:
#   Inputs:  env ENV_FILE (default /opt/homehub/stack/.env) for the credential;
#            the pinned binary beside this script, with its .sha256.
#            Must run as root (sudo).
#   Outputs: /opt/icedrive/icedrive; /srv/icedrive; optionally a signed-in
#            ~hub/.config/Icedrive/Icedrive.conf and an enabled mount unit.
#   Raises:  nonzero exit with a FATAL line when it cannot do what it was asked.
#            A MISSING CREDENTIAL IS NOT A FAILURE - that is the shipped state.
# Implements: the SR-015 replacement. Supersedes the AppImage half of
#             stack/remote-ui/ (open-items E1).
set -euo pipefail

log() { echo "[icedrive] $*"; }
die() { log "FATAL: $*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo (needs to install into /opt and /srv)"

HERE="$(cd "$(dirname "$0")" && pwd)"
STACK_DIR="${STACK_DIR:-/opt/homehub/stack}"
ENV_FILE="${ENV_FILE:-$STACK_DIR/.env}"
# Read from .env like every other knob, so the mount point the box uses and the
# one HomeHub declared cannot disagree. The env var still wins for a hand-run.
MOUNTPOINT="${ICEDRIVE_MOUNTPOINT:-$(sed -n 's/^ICEDRIVE_MOUNTPOINT=//p' "$ENV_FILE" 2>/dev/null | head -1 | tr -d '\r')}"
MOUNTPOINT="${MOUNTPOINT:-/srv/icedrive}"
DEST=/opt/icedrive/icedrive

# The operator account by UID, not by the name `hub` spelled again here. The
# autoinstall names it once and this file should not be a second place that has
# to agree.
HUB_USER="$(getent passwd 1000 | cut -d: -f1)"
[ -n "$HUB_USER" ] || HUB_USER=hub
HUB_HOME="$(getent passwd "$HUB_USER" | cut -d: -f6)"
[ -d "$HUB_HOME" ] || die "home dir for $HUB_USER not found"

# ── 1. the binary ───────────────────────────────────────────────────────────
# RE-CHECK THE HASH ON THE BOX, not just at build time. The sha256 travelled
# beside the binary on the payload; an ISO can be re-burned and a payload can be
# edited, and "we verified it three steps ago" is not the same claim as "the
# bytes about to be installed are the pinned ones".
SRC="$HERE/IcedriveCLI"
if [ -f "$SRC" ]; then
    [ -f "$SRC.sha256" ] || die "$SRC has no .sha256 beside it - refusing to install an unverified vendor binary"
    _want="$(tr -d '[:space:]' < "$SRC.sha256")"
    _got="$(sha256sum "$SRC" | cut -d' ' -f1)"
    [ "$_want" = "$_got" ] || die "$SRC does not match its pin. pinned=$_want actual=$_got - NOT installed"
    install -d -m 0755 /opt/icedrive
    install -m 0755 "$SRC" "$DEST"
    log "installed $DEST (sha256 matches the pin)"
elif [ -x "$DEST" ]; then
    log "no binary on the payload, but $DEST already exists - keeping it"
else
    log "NO ICEDRIVE BINARY on the payload and none installed (icedrive.pin unset"
    log "  at build time). Nothing else here can run. To ship it, see README.md."
    exit 0
fi

# libfuse.so.2 is in packages.list for exactly this; say so plainly if it is
# missing, because the runtime error names a library and not a package.
if ! ldd "$DEST" 2>/dev/null | grep -q "libfuse.so.2 => /"; then
    log "WARNING: libfuse.so.2 does not resolve for $DEST, so the mount will fail."
    log "  It comes from libfuse2t64, which is in packages.list. On a box that"
    log "  predates that:  apt-get install libfuse2t64"
fi

# ── 2. the mount point ──────────────────────────────────────────────────────
# Created here rather than by the service, so a box with no credential still has
# an obvious, empty, correctly-owned directory rather than a surprise.
#
# ASK WHETHER IT IS ALREADY A MOUNT FIRST, because once the service is up this
# path is a live FUSE filesystem that ROOT CANNOT TOUCH - and `install -d` on it
# fails with "cannot change owner and permissions of /srv/icedrive: Permission
# denied", killing a re-run of an otherwise idempotent script. Measured
# 2026-08-27: the script provisioned the box, then could not be run again on the
# box it had just provisioned.
if findmnt -rn "$MOUNTPOINT" >/dev/null 2>&1; then
    log "mount point $MOUNTPOINT is already a live mount - leaving it alone"
else
    install -d -m 0755 -o "$HUB_USER" -g "$(id -gn "$HUB_USER")" "$MOUNTPOINT"
    log "mount point $MOUNTPOINT ready (owned by $HUB_USER)"
fi

# ── 3. the one-time sign-in ─────────────────────────────────────────────────
# Read ONLY the two keys, and never `source` the file: .env holds every secret
# the stack has, and sourcing it would put all of them in this process
# environment for anything it later execs. tr -d CR because a value carrying one
# would authenticate as a different string and the failure would look like a
# wrong password - that exact shape cost an evening on the xrdp login.
if [ -r "$ENV_FILE" ]; then
    ICEDRIVE_USER="$(sed -n 's/^ICEDRIVE_USER=//p' "$ENV_FILE" | head -1 | tr -d '\r')"
    ICEDRIVE_PASSWORD="$(sed -n 's/^ICEDRIVE_PASSWORD=//p' "$ENV_FILE" | head -1 | tr -d '\r')"
else
    ICEDRIVE_USER=""; ICEDRIVE_PASSWORD=""
    log "NOTE: no readable $ENV_FILE - cannot sign in from it"
fi

CONF="$HUB_HOME/.config/Icedrive/Icedrive.conf"
# BOTH KEYS, because either alone is ambiguous. `icedrive_sessId` is NOT one of
# them: it is written even after a login that FAILED, so it proves nothing.
signed_in() {
    grep -q "^icedrivet=."      "$CONF" 2>/dev/null &&
    grep -q "^icedrive_login=." "$CONF" 2>/dev/null
}

if [ -z "$ICEDRIVE_USER" ] || [ -z "$ICEDRIVE_PASSWORD" ]; then
    log "NO CREDENTIAL IN $ENV_FILE - installed the binary and stopped. THIS IS THE"
    log "  SHIPPED DEFAULT, not a fault: nothing signs in, nothing mounts, and no"
    log "  unit is enabled. To turn it on, add IcedriveCredential to the deploy"
    log "  store (PrepDeploySecrets.ps1), re-materialize .env, and re-run this."
    exit 0
fi

if signed_in; then
    # IDEMPOTENCE ASKS THE END STATE, not "did we run before". A stored
    # credential is the thing the login exists to produce, so its presence is
    # what makes a second login unnecessary. (firstboot step 1b was killed by
    # exactly the opposite mistake - a guard that asked about its own output.)
    log "$HUB_USER is already signed in (session token present) - not signing in again"
else
    log "signing in as $ICEDRIVE_USER (once; the password is on argv for ~2s - see the header)"
    # `timeout` because a SUCCESSFUL login continues straight into the mount and
    # runs forever ("Press Ctrl-C to unmount and quit"). What this step wants is
    # the credential stored, not a mount held open by a provisioning script.
    _out="$(runuser -u "$HUB_USER" -- timeout 90 "$DEST" -verbose \
              -login "$ICEDRIVE_USER" -password "$ICEDRIVE_PASSWORD" </dev/null 2>&1 || true)"
    # NEVER echo "$_out" wholesale: -verbose prints the command line back, and
    # the command line contains the password. Match on it; print only verdicts.
    if printf '%s' "$_out" | grep -qi "invalid email or password"; then
        log "REFUSED: IceDrive rejected the credential (invalid email or password)."
        log "  Nothing is enabled. Fix the value in the deploy store and re-run."
        log "  NOTE: the vendor rate-limits repeated failures for 30 minutes."
        exit 1
    fi
    if printf '%s' "$_out" | grep -qiE "2FA|authentication code"; then
        log "REFUSED: this account has TWO-FACTOR AUTHENTICATION enabled, and the CLI"
        log "  cannot complete it unattended - its own words are that the 2FA method"
        log "  is not supported in CLI. Either use an account without 2FA for the"
        log "  hub, or accept that IceDrive here needs a human at every sign-in."
        exit 1
    fi
fi

if signed_in; then
    log "signed in - the session token is stored in $CONF, so later runs need no password"
else
    log "WARNING: the login left no session token behind (no icedrivet/icedrive_login)."
    log "  The mount unit cannot start without a password of its own, and it"
    log "  deliberately does not carry one. Not enabling it. Check the app's own"
    log "  log for the reason:  sudo tail $HUB_HOME/.local/share/Icedrive/logdata.txt"
    log "  Everything else on this box is unaffected."
    exit 1
fi

# ── 4. the mount unit ───────────────────────────────────────────────────────
if [ -f "$HERE/homehub-icedrive.service" ]; then
    # SUBSTITUTE THE OPERATOR ACCOUNT rather than shipping `hub` spelled a second
    # time. See the comment beside User= in the unit. Getting this wrong is not
    # subtle but it is opaque: systemd fails the unit with 217/USER and the
    # journal says only "Failed to determine user credentials: No such process",
    # which names neither the unit's User= line nor the account it wanted.
    sed "s/__HUB_USER__/$HUB_USER/g" "$HERE/homehub-icedrive.service" \
        > /etc/systemd/system/homehub-icedrive.service
    chown root:root /etc/systemd/system/homehub-icedrive.service
    chmod 0644      /etc/systemd/system/homehub-icedrive.service
    grep -q "^User=$HUB_USER$" /etc/systemd/system/homehub-icedrive.service \
        || die "the unit still carries an unsubstituted User= after install"
    systemctl daemon-reload
    systemctl enable --now homehub-icedrive.service || true
    # ACTIVE IS NOT MOUNTED, AND ASKING ONLY systemd IS HOW THIS GOES WRONG.
    # With any sandboxing directive set, systemd gives the service its own mount
    # namespace and a FUSE mount made inside it does not propagate out: the unit
    # reports active, the app has genuinely mounted, and `mount` from anywhere
    # else on the box shows nothing. Measured 2026-08-27. The unit no longer
    # carries those directives - and this asks the END STATE anyway, from
    # outside the service, because that is the only question worth asking.
    for _ in $(seq 1 10); do
        findmnt -rn "$MOUNTPOINT" >/dev/null 2>&1 && break
        sleep 1
    done
    if systemctl is-active --quiet homehub-icedrive.service && findmnt -rn "$MOUNTPOINT" >/dev/null 2>&1; then
        log "homehub-icedrive.service is active AND $MOUNTPOINT is mounted system-wide"
        log "  ($(runuser -u "$HUB_USER" -- ls -1 "$MOUNTPOINT" 2>/dev/null | wc -l) entries visible to $HUB_USER)"
        log "  NOTE: root CANNOT read this mount. FUSE grants the mounting user only,"
        log "    and this binary offers no way to pass -o allow_other. Anything that"
        log "    needs to write the offsite copy must run as $HUB_USER."
    elif systemctl is-active --quiet homehub-icedrive.service; then
        log "WARNING: the unit is ACTIVE but $MOUNTPOINT is NOT mounted system-wide."
        log "  That is the mount-namespace failure described in the unit file: some"
        log "  sandboxing directive has come back. Check the unit for ProtectSystem,"
        log "  ProtectKernelTunables, ProtectControlGroups or NoNewPrivileges."
    else
        log "WARNING: homehub-icedrive.service did not come up. Check:"
        log "    systemctl status homehub-icedrive ; journalctl -u homehub-icedrive"
    fi
else
    log "NOTE: no homehub-icedrive.service beside this script - signed in, but"
    log "  nothing will mount at boot."
fi

log "done (re-running is safe: the install, the sign-in and the unit are all idempotent)"
