#!/usr/bin/env bash
# Create the operator's graphical session AT BOOT, with nobody connected.
#
# WHY THIS EXISTS. IceDrive's Linux client is GUI-only: it syncs only while it is
# running, and it can only run with an X display. The project believed that meant
# "after every reboot the cloud copy is stale until someone opens an RDP session"
# — recorded in the remote-ui README, in open-items E1, and repeated as fact.
#
# IT IS NOT TRUE, and it was never measured. Measured on the bench box
# 2026-08-27: what the app needs is a DISPLAY, not a CLIENT. An xrdp session
# survives its client leaving (sesman.ini ships KillDisconnected=false,
# DisconnectedTimeLimit=0), and a session can be created with no human at all by
# pointing an RDP client at loopback and then dropping it. The session — and
# everything running in it — persists indefinitely.
#
# Proven end to end before this was written:
#   * every session killed (count 0), one created headlessly, client dropped
#     entirely: the session and its app survived;
#   * mstsc from the dev PC then RECONNECTED to that same session rather than
#     spawning a second one. sesman logged it in those words:
#         ++ reconnected session: username hub, display :10.0
#
# So IceDrive runs unattended from boot, and the operator attaches to the
# RUNNING instance whenever they want to check or reconfigure it.
#
# THE CLIENT IS xfreerdp UNDER Xvfb, which reads oddly and is not a hack: an RDP
# client is itself an X application and needs somewhere to draw. Xvfb is that
# somewhere, for the ~15 seconds it takes to establish the session. Both are then
# thrown away; the session they created is what remains.
#
# Implements: SR-015. Amends the "one RDP touch per reboot" claim in
# stack/remote-ui/README.md and open-items E1.

set -uo pipefail

STACK_DIR="${STACK_DIR:-/opt/homehub/stack}"
ENV_FILE="${ENV_FILE:-$STACK_DIR/.env}"
# MUST MATCH WHAT THE OPERATOR CONNECTS WITH. sesman's Policy=Default keys a
# session on <user, bit-depth, screen size>, so a client arriving at a different
# geometry gets a SECOND session — and IceDrive would autostart in that one too,
# leaving two clients syncing the same folders. HomeHubDesktop.cmd's default is
# 1600x900; these are deliberately the same numbers.
GEOM_W="${GEOM_W:-1600}"
GEOM_H="${GEOM_H:-900}"
GEOM_BPP="${GEOM_BPP:-24}"
SETTLE="${SETTLE:-15}"

log() { echo "[desktop-session] $*"; }

RDP_USER="$(getent passwd 1000 | cut -d: -f1)"
[ -n "$RDP_USER" ] || RDP_USER=hub

have_session() { pgrep -u "$RDP_USER" -f "Xorg.*:1[0-9]" >/dev/null 2>&1; }

# ── idempotent: never build a second session ────────────────────────────────
# This runs at boot, but it is also safe to run by hand. A second session is the
# specific harm to avoid (two IceDrive instances), so the check comes first.
if have_session; then
    log "a session already exists for $RDP_USER — nothing to do"
    exit 0
fi

for dep in Xvfb xfreerdp; do
    command -v "$dep" >/dev/null || { log "FATAL: $dep not installed"; exit 1; }
done
[ -r "$ENV_FILE" ] || { log "FATAL: cannot read $ENV_FILE (need OPERATOR_PASSWORD)"; exit 1; }

# Read ONLY the one key; never source .env (it holds every secret the stack has).
# tr -d '\r' because a CR here would authenticate as a different password and the
# failure would look like a wrong credential — that exact bug cost an evening.
PW="$(sed -n 's/^OPERATOR_PASSWORD=//p' "$ENV_FILE" | head -1 | tr -d '\r')"
if [ -z "$PW" ]; then
    log "FATAL: OPERATOR_PASSWORD is not set in $ENV_FILE."
    log "  Without it nothing can authenticate to xrdp, so no session can be made."
    exit 1
fi

# ── create it ───────────────────────────────────────────────────────────────
# THE PASSWORD IS ON THE COMMAND LINE, AND THAT IS A DELIBERATE DECISION rather
# than the lazy default. /proc carries no hidepid, so argv is world-readable for
# the ~15s this runs — normally unacceptable, and the same exposure that was
# deliberately designed out of the chpasswd push in HomeHub's launcher.
#
# It is acceptable HERE, and this was measured on the box rather than assumed:
#   * `hub` is the ONLY account with a real shell — every other uid >= 1000 is
#     nologin — so it is the only thing that could read the argv at all; and
#   * `hub` already carries `(ALL) NOPASSWD: ALL`, so it can simply
#     `sudo cat /opt/homehub/stack/.env` and read the very same value.
# The exposure therefore grants nothing that is not already granted. That is the
# Owner's standing threat-model ruling (2026-08-09): a credential is not a
# finding for living where the deployment mechanism already puts it.
#
# /from-stdin WAS TRIED FIRST and cannot work from a service: it wants a TTY to
# prompt at and hangs forever when fed on a pipe. Measured twice — password
# alone, and username+password — and both times the client sat in poll() until
# it was killed, with no session created. If FreeRDP ever grows a file- or
# fd-based password option, switch to it; that is the only obstacle.
#
# THE ONE CONDITION THAT INVALIDATES THIS: a second interactive account on the
# box. If one is ever added, this decision has to be revisited.
log "creating a session for $RDP_USER (${GEOM_W}x${GEOM_H}x${GEOM_BPP}) with no client attached…"

# Xvfb IS MANAGED EXPLICITLY rather than through `xvfb-run`, because xvfb-run
# only cleans up its Xvfb via an EXIT trap — and this script has to kill the
# client rather than wait for it (the client would sit connected forever). A
# SIGTERM to the wrapper does not run that trap, so the first version leaked one
# `Xvfb :99` per invocation, reparented to init and invisible until something
# counted processes. Owning the PID means it cannot be orphaned.
SCRATCH_DISPLAY=99
while [ -e "/tmp/.X11-unix/X$SCRATCH_DISPLAY" ] && [ "$SCRATCH_DISPLAY" -lt 120 ]; do
    SCRATCH_DISPLAY=$((SCRATCH_DISPLAY + 1))
done
XVFB_PID=""
cleanup() {
    [ -n "${CLIENT_PID:-}" ] && kill "$CLIENT_PID" 2>/dev/null
    [ -n "$XVFB_PID" ]       && kill "$XVFB_PID"   2>/dev/null
    wait "$XVFB_PID" 2>/dev/null
}
# Covers every exit path, including the failure returns below and any signal
# systemd sends on timeout — the scratch display never outlives this script.
trap cleanup EXIT INT TERM

Xvfb ":$SCRATCH_DISPLAY" -screen 0 "${GEOM_W}x${GEOM_H}x${GEOM_BPP}" -nolisten tcp \
    >>/var/log/homehub-desktop-session.log 2>&1 &
XVFB_PID=$!
sleep 2

DISPLAY=":$SCRATCH_DISPLAY" timeout 60 xfreerdp \
    /v:127.0.0.1:3389 /u:"$RDP_USER" /p:"$PW" \
    /cert-ignore /w:"$GEOM_W" /h:"$GEOM_H" /bpp:"$GEOM_BPP" \
    >>/var/log/homehub-desktop-session.log 2>&1 &
CLIENT_PID=$!
unset PW

# Wait for sesman to actually start the Xorg, rather than sleeping a fixed
# guess and hoping.
for _ in $(seq 1 "$SETTLE"); do
    have_session && break
    sleep 1
done

# ── drop the client; keep the session ───────────────────────────────────────
# The trap does the actual killing, so there is one cleanup path rather than two
# that can disagree. This just waits long enough for the session to be visibly
# standing on its own afterwards.
cleanup
sleep 3

if have_session; then
    DISP="$(pgrep -u "$RDP_USER" -af 'Xorg.*:1[0-9]' | grep -oE ':1[0-9]' | head -1)"
    log "session ready on display ${DISP:-?} and will persist until reboot"
    log "  anything autostarted in it (IceDrive) is now running with nobody connected"
    log "  connect any time to attach to THIS session — you will see the running app"
    exit 0
fi

log "FATAL: no session exists after ${SETTLE}s. See /var/log/homehub-desktop-session.log"
log "  Common causes: OPERATOR_PASSWORD does not match the account (try it at the"
log "  greeter by hand), or xrdp is not listening. Check: systemctl status xrdp"
exit 1
