#!/usr/bin/env bash
# The kiosk session: ONE app, fullscreen, forever. Exec'd from the tty1 autologin
# session (/etc/profile.d/zz-wall-kiosk.sh) so that cage inherits a real logind
# SEAT — a plain system service has none, and cage cannot open the DRM/input
# devices without one.
#
# Why cage and nothing else: `cage` is a single-surface Wayland compositor. It
# shows exactly one client, fullscreen, with no decorations, no desktop, no
# switcher and nothing to accidentally reveal on a wall in an office. Per
# OfficeWallNaglight's design (P8, shrunk by OWN-D1), the image's whole job is to
# run THE app fullscreen — not a browser pointed at a URL — so there is no second
# surface, no switching affordance, and no second idle/audio behaviour to manage.
#
# IF-005 PLACEHOLDER. WALL_APP_CMD points at the built OfficeWallNaglight
# artifact, and how that artifact is PACKAGED is precisely the part of IF-005 that
# is not decided yet (that repo ships source + a package.json, not an installer).
# Until it is, this script fails VISIBLY rather than leaving a black rectangle on
# the wall: a dead panel must be a visible event, not silence.
set -u

ENV_FILE="/etc/wall-panel/wall.env"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }
: "${WALL_APP_CMD:=/opt/wall-panel/app/wall-shell}"
: "${WALL_HOST:=}"
: "${WALL_PORT:=8443}"

log() { echo "[wall-kiosk] $*"; }

# The origin that serves the shell AND proxies /api/* — the same-origin contract
# (NagLight sends no CORS headers). The app reads this as PANEL_URL.
export PANEL_URL="https://${WALL_HOST}:${WALL_PORT}/"
export PANEL_KIOSK=1
log "PANEL_URL=$PANEL_URL"

# Restart loop: a crashed shell must come back by itself. `cage` exits when its
# client exits, so the loop covers both. The 3 s pause keeps a hard-failing
# artifact from spinning the CPU behind a wall (quirk 6 is a thermal concern).
while true; do
    if [ -x "${WALL_APP_CMD%% *}" ]; then
        log "starting: cage -- $WALL_APP_CMD"
        # -d: don't draw a cursor for a touch-only panel.
        cage -d -- ${WALL_APP_CMD} || log "kiosk client exited ($?) — restarting"
    else
        # LOUD, ON THE SCREEN. `cage` needs a client, so use the one thing every
        # install has: a shell printing the reason, held open so it stays readable
        # from across the office.
        log "FATAL: WALL_APP_CMD='$WALL_APP_CMD' is not executable (IF-005 artifact not installed)"
        cage -- /bin/sh -c '
            printf "\n\n  OFFICE WALL PANEL\n\n";
            printf "  The shell application is NOT INSTALLED.\n\n";
            printf "  WALL_APP_CMD in /etc/wall-panel/wall.env points at a path that\n";
            printf "  does not exist or is not executable. The panel image is fine;\n";
            printf "  the OfficeWallNaglight artifact was never baked into the\n";
            printf "  payload (IF-005 — its packaging contract is still open).\n\n";
            printf "  Everything else works: ssh in and check\n";
            printf "    journalctl -t wall-kiosk\n\n";
            sleep 3600' || true
    fi
    sleep 3
done
