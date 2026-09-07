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
# IF-005 IS SETTLED (PKG-1, 2026-08-02). WALL_APP_CMD points at the built
# OfficeWallNaglight artifact, which the image now unpacks to
# /opt/wall-panel/app/ (wall user-data late-command 3b). This script's job is
# what it always was: fail VISIBLY rather than leave a black rectangle on the
# wall. There are two ways to be dead and both are handled below — not installed
# (the `[ -x ]` branch) and installed-but-exiting (the crash-loop counter).
set -u

# THE KIOSK RUNS AS AN UNPRIVILEGED USER AND CANNOT READ wall.env. That file is
# 0600 root:root because it holds the Wi-Fi PSK; this session is the `panel`
# autologin. Reading it here silently produced NOTHING — measured on the first
# real boot (2026-08-03): `PANEL_URL=https://:8443/`, an empty WALL_HOST, and
# WALL_APP_CMD's configured flags dropped, with no error anywhere because
# load_env_file treats an unreadable file exactly like an absent one.
#
# wall-firstboot.sh now renders the non-secret subset this script needs into
# kiosk.env (0644). wall.env stays as a fallback for a root-run debug, and for
# the moment before firstboot has ever executed.
ENV_FILE="/etc/wall-panel/kiosk.env"
FALLBACK_ENV_FILE="/etc/wall-panel/wall.env"
# shellcheck disable=SC1090
# load_env_file FILE — export every KEY=VALUE in FILE **literally**.
#
# NEVER `source` a compose .env. Its values are literal text, and every
# basic_auth hash in this project is bcrypt — `$2a$14$…`. Sourcing makes bash
# expand them: under `set -u` it aborts on the unbound `$2` (which is exactly
# how first boot died), and WITHOUT `set -u` it is worse — `$2`/`$1` expand to
# nothing, the hash is silently corrupted, and auth then fails with nothing
# anywhere explaining why.
load_env_file() {
    local __f="$1" __line __k __v
    [ -f "$__f" ] || return 0
    while IFS= read -r __line || [ -n "$__line" ]; do
        case "$__line" in ''|'#'*) continue ;; esac
        case "$__line" in *=*) ;; *) continue ;; esac
        __k=${__line%%=*}
        __v=${__line#*=}
        __k=${__k#"${__k%%[![:space:]]*}"}
        __k=${__k%"${__k##*[![:space:]]}"}
        case "$__k" in ''|*[!A-Za-z0-9_]*) continue ;; esac
        case "$__v" in
            \"*\") __v=${__v#\"}; __v=${__v%\"} ;;
            \'*\') __v=${__v#\'}; __v=${__v%\'} ;;
            # UNQUOTED: strip a trailing ` # comment`, exactly as shell
            # sourcing and docker compose's own .env parser both do. Without
            # this, LAN_IP=0.0.0.0 followed by an explanatory comment reached
            # Technitium's API as part of the address and curl rejected the
            # URL. A `#` with no space before it is kept — it may be part of a
            # password.
            *) __v=${__v%%[[:space:]]#*}
               __v=${__v%"${__v##*[![:space:]]}"} ;;
        esac
        # Compose stores a literal '$' as '$$' (see compose_escape) because it
        # interpolates .env values. Collapse it back, so a script reading this
        # file sees exactly what the containers receive.
        __v=${__v//\$\$/\$}
        printf -v "$__k" '%s' "$__v" 2>/dev/null && export "$__k"
    done < "$__f"
}
# Loud about which one it got, and louder about neither: a kiosk running on
# pure defaults looks identical to a configured one until you notice the URL.
if [ -r "$ENV_FILE" ]; then
    load_env_file "$ENV_FILE"
    KIOSK_ENV_SOURCE="$ENV_FILE"
elif [ -r "$FALLBACK_ENV_FILE" ]; then
    load_env_file "$FALLBACK_ENV_FILE"
    KIOSK_ENV_SOURCE="$FALLBACK_ENV_FILE (fallback)"
else
    KIOSK_ENV_SOURCE="NOTHING — running on built-in defaults"
fi
: "${WALL_APP_CMD:=/opt/wall-panel/app/wall-shell}"
: "${WALL_HOST:=}"
: "${WALL_PORT:=8443}"

# log MESSAGE — to the journal under the tag everything else documents, AND to
# stdout.
#
# THE `logger` HALF IS NOT DECORATION. This script is exec'd from
# /etc/profile.d/zz-wall-kiosk.sh inside an agetty AUTOLOGIN session on tty1, so
# its stdout is the TTY DEVICE — systemd does not capture it. Every runbook in
# this repo, the wall image's own NOT INSTALLED screen, and the A19 gate's pass
# criterion all say `journalctl -t wall-kiosk`, and without this line that
# command returns NOTHING: not an error, not a hint, just no output, on the one
# machine whose whole failure mode is having nothing to show. Writing to both
# keeps the tty copy for a panel with no network.
HAVE_LOGGER=0
command -v logger >/dev/null 2>&1 && HAVE_LOGGER=1
log() {
    echo "[wall-kiosk] $*"
    [ "$HAVE_LOGGER" -eq 1 ] && logger -t wall-kiosk -- "$*"
    return 0
}

# panel_screen — put the text on stdin ONTO THE WALL, and do it WITHOUT cage.
#
# THE FAILURE SCREENS NEVER RENDERED. Both of them used to be
# `cage -- /bin/sh -c 'printf ...'`, and `cage` displays exactly one WAYLAND
# CLIENT — a shell running printf is not one. It writes to stdout, cage shows an
# empty surface, and because cage does the KMS modeset it also HIDES the text
# console underneath. Measured 2026-08-03 with grim inside the session and with
# Hyper-V's thumbnail: a uniformly black screen while the message sat in the
# journal. So the whole premise — "a dead panel must be a visible event, not
# silence" — was false for the entire life of this script, in both directions.
#
# This is why it was never caught by testing: with a stand-in `cage` that simply
# execs its client, the text appears on stdout and everything looks right.
#
# The fix needs no extra package. This script IS the tty1 session leader, so its
# stdout is the console; with no compositor running, the console is what the
# panel scans out. Clear the screen, write, and leave cage out of it.
panel_screen() {
    local out=/dev/tty1
    [ -w "$out" ] || out=/dev/stdout
    { printf '\033[2J\033[H'; cat; } > "$out" 2>/dev/null || cat
}
# journal_sink — stdin to the journal under the same tag, or nowhere.
# One place decides what happens to the CLIENT's output, so the cage invocation
# below stays a single line whether or not logger(1) exists.
journal_sink() {
    if [ "$HAVE_LOGGER" -eq 1 ]; then logger -t wall-kiosk; else cat >/dev/null; fi
}

# The origin that serves the shell AND proxies /api/* — the same-origin contract
# (NagLight sends no CORS headers). The app reads this as PANEL_URL.
export PANEL_URL="https://${WALL_HOST}:${WALL_PORT}/"
export PANEL_KIOSK=1
# Only the path is public. Electron reads the separate 0600 private config.
if [ -n "${WALL_HOST_CONFIG:-}" ]; then
    export PANEL_HOST_CONFIG="$WALL_HOST_CONFIG"
else
    unset PANEL_HOST_CONFIG
fi
log "config from: $KIOSK_ENV_SOURCE"
log "PANEL_URL=$PANEL_URL"
case "${WALL_HOST:-}" in
    ''|*REPLACE_WITH*)
        log "WARNING: WALL_HOST is empty or a placeholder, so PANEL_URL cannot resolve."
        log "WARNING: cage and the shell will start and paint NOTHING. Fix WALL_HOST in"
        log "WARNING: /etc/wall-panel/wall.env and re-run: sudo /usr/local/sbin/wall-firstboot.sh" ;;
esac

# ── software rendering, but ONLY where there is genuinely no GPU ─────────────
# wlroots (and therefore cage) REFUSES to start on a software renderer unless
# this is set — measured 2026-08-03 in Hyper-V:
#   [render/egl.c:320] Software rendering detected, please use the
#   WLR_RENDERER_ALLOW_SOFTWARE environment variable to proceed
# Hyper-V's hyperv_drm gives /dev/dri/card1 and NO renderD* node, so EGL lands
# on llvmpipe and cage exits before the shell ever draws.
#
# CONDITIONAL, and that is the point. Setting it unconditionally would mean a
# panel whose iGPU regressed silently fell back to CPU rendering — on a machine
# in a sealed wall mount where sustained video is the load case and thermals are
# quirk 6. Absent a render node there is nothing to lose; present, a failure
# must stay a failure.
if ls /dev/dri/renderD* >/dev/null 2>&1; then
    log "GPU: render node present ($(ls /dev/dri/renderD* 2>/dev/null | tr '\n' ' ')) — hardware rendering expected"
else
    export WLR_RENDERER_ALLOW_SOFTWARE=1
    log "GPU: NO /dev/dri/renderD* — allowing wlroots' software renderer (WLR_RENDERER_ALLOW_SOFTWARE=1)."
    log "GPU: expected in a VM; on the PANEL it means the iGPU is not bound and video will be CPU-decoded."
fi
[ "$HAVE_LOGGER" -eq 1 ] || echo "[wall-kiosk] WARNING: no logger(1) — journalctl -t wall-kiosk will be EMPTY"

# ── the crash-loop counter ───────────────────────────────────────────────────
# A DEAD PANEL MUST BE A VISIBLE EVENT, and until now only half of that was
# true. `[ -x ]` false paints the NOT INSTALLED screen below — loud. But an
# artifact that IS installed and dies at startup shows NOTHING: cage exits when
# its client does, this loop restarts it 3 s later, and the wall stays black
# forever with the reason only in the journal. That state became reachable the
# moment the image started installing an artifact, so it needs the same
# treatment: after three exits inside 15 s, stop restarting silently and put the
# client's own last words ON THE SCREEN.
#
# The 15 s bar distinguishes "failed at startup" from "ran and then crashed";
# Electron reaches a window well inside that. Three, not one, so a single
# transient (a compositor losing DRM on resume) still self-heals unseen.
FAST_EXIT_SECS=15
FAST_EXIT_LIMIT=3
fast_exits=0
# tmpfs, and the kiosk user owns it. `tail -c` writes only at EOF and caps the
# file at 4 KB, so a healthy shell running for weeks cannot fill /run.
CRASH_LOG="${XDG_RUNTIME_DIR:-/tmp}/wall-kiosk.last"

# Restart loop: a crashed shell must come back by itself. `cage` exits when its
# client exits, so the loop covers both. The 3 s pause keeps a hard-failing
# artifact from spinning the CPU behind a wall (quirk 6 is a thermal concern).
while true; do
    if [ -x "${WALL_APP_CMD%% *}" ]; then
        log "starting: cage -- $WALL_APP_CMD"
        started=$(date +%s)
        # -d: don't draw a cursor for a touch-only panel.
        # The client's own output goes to the journal live (the wrapper's
        # chmod-4755 diagnosis and Electron's "cannot open shared object file"
        # both arrive here) and its tail is kept for the screen below.
        cage -d -- ${WALL_APP_CMD} 2>&1 | tee >(journal_sink) | tail -c 4096 > "$CRASH_LOG"
        rc=${PIPESTATUS[0]}      # cage's status, not tail's
        ran=$(( $(date +%s) - started ))
        [ "$rc" -eq 0 ] || log "kiosk client exited ($rc) after ${ran}s — restarting"

        if [ "$ran" -lt "$FAST_EXIT_SECS" ]; then
            fast_exits=$(( fast_exits + 1 ))
        else
            fast_exits=0
        fi

        if [ "$fast_exits" -ge "$FAST_EXIT_LIMIT" ]; then
            log "FATAL: the shell exited within ${FAST_EXIT_SECS}s, $fast_exits times running — showing the crash on screen"
            fast_exits=0
            # Held long enough to read from across the office, but not forever —
            # a transient that clears should recover on its own rather than
            # needing someone to walk over.
            {
                printf '\n\n  OFFICE WALL PANEL\n\n'
                printf '  The shell application is INSTALLED but KEEPS EXITING.\n\n'
                printf '  Its last output follows. A missing shared library or a\n'
                printf '  cleared setuid bit on chrome-sandbox are the usual causes.\n'
                printf '  Full history:  journalctl -t wall-kiosk\n\n'
                printf '  ----------------------------------------------------------\n'
                if [ -s "$CRASH_LOG" ]; then
                    tail -n 14 "$CRASH_LOG"
                else
                    printf '  The client produced NO OUTPUT AT ALL before exiting.\n'
                    printf '  That points at cage or the seat, not at the shell.\n'
                fi
                printf '  ----------------------------------------------------------\n\n'
                printf '  Retrying in 5 minutes.\n'
            } | panel_screen
            sleep 300
        fi
    else
        # LOUD, ON THE SCREEN — via the console, not via cage. See panel_screen.
        log "FATAL: WALL_APP_CMD='$WALL_APP_CMD' is not executable (IF-005 artifact not installed)"
        {
            printf '\n\n  OFFICE WALL PANEL\n\n'
            printf '  The shell application is NOT INSTALLED.\n\n'
            printf '  WALL_APP_CMD in /etc/wall-panel/wall.env points at a path that\n'
            printf '  does not exist or is not executable. The panel image is fine;\n'
            printf '  the OfficeWallNaglight artifact was never baked into the\n'
            printf '  payload, or its unpack failed. Check /opt/wall-panel/wall-app/\n'
            printf '  for the tarball, and rebuild the image with it staged.\n\n'
            printf '  Everything else works: ssh in and check\n'
            printf '    journalctl -t wall-kiosk\n\n'
        } | panel_screen
        sleep 3600
    fi
    sleep 3
done
