#!/usr/bin/env bash
# First-boot configuration for the OFFICE WALL PANEL. Invoked once by
# wall-firstboot.service; idempotent and loud, so re-running after editing
# /etc/wall-panel/wall.env is the supported way to change anything.
#
# This script is CONFIG ONLY — it writes files and enables units. It installs no
# product code and downloads nothing.
#
# Steps (each one is a §3 quirk from the design brief, or the D-W4 sleep window):
#   1. Sanity: wall.env exists and is filled.
#   2. Quirk 1 — logind must IGNORE the lid switch. Folded into tablet mode the
#      lid reads CLOSED, and default logind then suspends the machine forever:
#      the panel just looks dead on the wall.
#   3. Quirk 2 — mask iio-sensor-proxy and pin the orientation. A convertible
#      will rotate its own display (and its touch coordinates with it).
#   4. Quirk 3 — disable the internal keyboard/touchpad, which face the mount.
#      Needs THIS machine's device names; skipped loudly if not configured.
#   5. Quirk 5 — Wi-Fi powersave OFF, MAC randomization OFF, netplan rendered.
#      An idle panel that drops off the LAN is unreachable exactly when it needs
#      fixing, and a random MAC silently breaks the DHCP reservation the kiosk
#      site's /32 depends on.
#   6. D-W4 — render the sleep-window timers from SLEEP_START/SLEEP_END and
#      report what /sys/power/mem_sleep actually says (the mem_sleep_default=deep
#      decision — see user-data §6).
#  6b. SN-015 — enable or disable the occupancy tick (wall-occupancy.timer) from
#      WALL_ABSENCE_ENABLED, and log which of the two power behaviours the panel
#      actually has. Off is the default and leaves step 6 untouched.
#   7. Autologin the kiosk user on tty1 so cage gets a real logind SEAT.
#   8. OI-15/OI-18 — the media cache + the pull units: create the cache dir, make
#      sure wall-sync.service is enabled, and REPORT whether BOTH shares are
#      configured (there are two sources on two hosts since OI-18 exit (b)).
#      Plus OI-16a: wall-sync-resume.service enabled, so every wake from the
#      nightly suspend re-triggers the sync (a resume is not a boot); and
#      wall-sync-frame.timer enabled, the frame flow's every-minute cadence.
#  8d. WSN-019 — create and start the unprivileged Door stream broker. Extract
#      only its allowlisted values into root-only /run files; systemd copies
#      those into the broker's RAM-backed credential mount. No second persistent
#      secret file exists and the broker never receives unrelated wall secrets.
#   9. Stamp the marker.
#
# What this script deliberately does NOT do: guess. Where a fix needs a value only
# the running hardware can supply (input device names, the backlight interface,
# the mounted orientation), it says so and points at WALL-BURN-IN.md instead of
# writing a plausible-looking rule that disables the touchscreen.
set -euo pipefail

ENV_FILE="/etc/wall-panel/wall.env"
PAYLOAD="/opt/wall-panel/stack/autoinstall/wall"
MARKER="/opt/wall-panel/.provisioned"
log() { echo "[wall-firstboot] $*"; }
warn() { echo "[wall-firstboot] WARNING: $*" >&2; }

# ── did anything we were ASKED to do actually fail? ──────────────────────────
# THIS SCRIPT'S SIGNATURE BUG, found twice now: a `systemctl enable` whose
# failure was swallowed by `|| warn`, followed by an UNCONDITIONAL "…enabled" log
# line and, at the end, the provisioning marker. A panel whose frame timer failed
# to enable therefore reported a GREEN first boot that explicitly claimed a
# cadence which did not exist — "the frame share is re-checked every minute",
# except it never is, and nothing anywhere says so. That is precisely the
# silent-green this repo forbids, in the one place a human is watching.
#
# So enabling goes through ONE helper: the success line is printed only on
# success, a failure is an ERROR (not a warning), and any failure makes the unit
# RED and WITHHOLDS the marker, because a partially provisioned panel is not
# provisioned.
PROVISION_FAILED=0
fail_step() {   # MESSAGE...
    PROVISION_FAILED=1
    echo "[wall-firstboot] ERROR: $*" >&2
}

# enable_unit SUCCESS_LINE UNIT... — enable units and JUDGE the result.
# The success line is the caller's, because only the caller knows what the
# enablement means on the wall; it is printed if and only if enable succeeded.
enable_unit() {
    local ok_line="$1"; shift
    if systemctl enable "$@" >/dev/null 2>&1; then
        log "$ok_line"
        return 0
    fi
    fail_step "systemctl enable FAILED for: $*. The panel will boot WITHOUT it, so do not read the absence of a complaint later as the feature working. Check: systemctl status $1 ; then: systemctl enable $*"
    return 1
}

# enable_unit_now — same, but starts the units as well (`--now`).
enable_unit_now() {
    local ok_line="$1"; shift
    if systemctl enable --now "$@" >/dev/null 2>&1; then
        log "$ok_line"
        return 0
    fi
    fail_step "systemctl enable --now FAILED for: $*. The panel will boot WITHOUT it. Check: systemctl status $1 ; then: systemctl enable --now $*"
    return 1
}

# ── 1. sanity ────────────────────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
    log "FATAL: $ENV_FILE missing (autoinstall should have seeded it from wall.env.example)"
    exit 1
fi
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
load_env_file "$ENV_FILE"
if grep -q "REPLACE_WITH" "$ENV_FILE"; then
    warn "$ENV_FILE still contains REPLACE_WITH placeholders."
    warn "The panel will boot, but Wi-Fi and/or the kiosk URL will be wrong until"
    warn "you edit it and re-run: sudo /usr/local/sbin/wall-firstboot.sh"
fi

: "${SLEEP_MODE:=suspend}"
: "${SLEEP_START:=22:00}"
: "${SLEEP_RTC_WAKE:=true}"
: "${WALL_ABSENCE_ENABLED:=false}"
# SLEEP_END is ONE value doing three jobs (the RTC wake, the wake timer, and the
# start of the on-period), with ONE default, 06:45, whichever power path is
# live. It briefly defaulted per path (06:45 on / 06:30 off); the Owner
# collapsed that on 2026-09-09 — see wall-sleep.sh's block and docs/status.md.
# Keep this line identical to wall-sleep.sh's — a panel whose wall.env predates
# the knob must get the same answer from both scripts, and this one renders the
# timers.
: "${SLEEP_END:=06:45}"
: "${WALL_ABSENCE_TIMEOUT_MIN:=60}"
: "${WALL_PRESENCE_FILE:=/run/wall-presence/state.json}"
: "${WALL_PORT:=8443}"

# ── 2. quirk 1 — logind ignores the lid ──────────────────────────────────────
install -d -m 0755 /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/kiosk.conf <<'EOF'
# WALL PANEL — quirk 1. Folded into tablet mode (screen back-to-back with the
# keyboard) the lid switch reads CLOSED. Default logind suspends on a closed lid,
# which on a wall means the panel is simply dark and looks broken. All three
# variants are set because the panel is on permanent AC with the lid "shut", and
# the docked/external-power cases behave differently by default.
[Login]
HandleLidSwitch=ignore
HandleLidSwitchDocked=ignore
HandleLidSwitchExternalPower=ignore
EOF
log "quirk 1: logind lid handling set to ignore (verify by folding it BEFORE mounting)"

# ── 3. quirk 2 — no auto-rotate ──────────────────────────────────────────────
# The mounted orientation is landscape, hinges at the BOTTOM (decided 2026-07-26)
# — which is this panel's native orientation, so the whole quirk reduces to
# stopping the accelerometer from rotating away from it. Masking the sensor daemon
# is the durable half; a compositor-level rotation is only needed if the panel is
# ever mounted some other way (see WALL-BURN-IN.md §2).
if systemctl list-unit-files | grep -q '^iio-sensor-proxy'; then
    systemctl mask iio-sensor-proxy.service >/dev/null 2>&1 || true
    log "quirk 2: iio-sensor-proxy masked (orientation pinned to native landscape, hinges-down)"
else
    log "quirk 2: iio-sensor-proxy not installed — nothing to mask (orientation is native landscape)"
fi

# ── 4. quirk 3 — internal keyboard + touchpad disabled ───────────────────────
# Folded flat, the keys press against the mount: one held key is an input storm,
# and a held power/volume key is worse. The TOUCHSCREEN must survive — it is the
# panel's only input. udev NAMEs are machine-specific, so this is configured, not
# guessed.
RULES=/etc/udev/rules.d/99-wall-disable-internal-input.rules
# Create the target dirs rather than assuming them: on a minimal install either
# can be absent, and a redirect into a missing directory aborts the whole script
# (found running this against a bare ubuntu:24.04 root, 2026-07-29).
install -d -m 0755 /etc/udev/rules.d /etc/netplan
if [ -n "${WALL_DISABLE_INPUT:-}" ]; then
    {
        echo "# GENERATED by wall-firstboot.sh from WALL_DISABLE_INPUT in $ENV_FILE."
        echo "# Quirk 3: the internal keyboard/touchpad face the wall mount."
        echo "# RE-ENABLE ON SITE (one line, survives until the next reboot):"
        echo "#   sudo rm $RULES && sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=input"
        while IFS= read -r name; do
            [ -n "$name" ] || continue
            printf 'SUBSYSTEM=="input", ATTRS{name}=="%s", ENV{LIBINPUT_IGNORE_DEVICE}="1"\n' "$name"
        # printf '%s\n' — WITH the trailing newline. Without it the final field is
        # an unterminated line, `read` returns nonzero for it, and the loop body
        # never runs: the LAST device silently keeps working. (Found by the wall
        # firstboot harness, 2026-07-29 — it had dropped the touchpad.)
        done < <(printf '%s\n' "$WALL_DISABLE_INPUT" | tr '|' '\n')
    } > "$RULES"
    udevadm control --reload >/dev/null 2>&1 || true
    udevadm trigger --subsystem-match=input >/dev/null 2>&1 || true
    log "quirk 3: internal input disabled for: $WALL_DISABLE_INPUT"
    log "quirk 3: re-enable on site with: sudo rm $RULES && sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=input"
else
    warn "quirk 3 NOT APPLIED — WALL_DISABLE_INPUT is empty."
    warn "The internal keyboard/touchpad are still live and will face the mount."
    warn "Read the device names off THIS machine and set the knob; the exact"
    warn "command is in $PAYLOAD/WALL-BURN-IN.md (§3). Guessing here risks"
    warn "disabling the touchscreen, which would leave the panel with no input."
fi

# ── 5. quirk 5 — Wi-Fi that stays reachable ──────────────────────────────────
install -d -m 0755 /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/99-wall-wifi.conf <<'EOF'
# WALL PANEL — quirk 5. Two settings, both about being findable:
#   powersave=2  -> DISABLE Wi-Fi power-save. An idle panel with powersave on is
#                   intermittently unreachable, which is exactly when SSH matters.
#                   (NetworkManager: 2 = disable, 3 = enable. Counter-intuitive.)
#   cloned-mac-address=permanent -> never a random MAC. The live boot showed
#                   randomization ON by default, and a DHCP reservation keyed to
#                   the hardware MAC silently stops working — which then breaks
#                   the kiosk site's /32 allow-list, not just the address.
[connection]
wifi.powersave=2
wifi.cloned-mac-address=permanent
EOF
log "quirk 5: Wi-Fi powersave disabled, MAC randomization pinned off"

# netplan: rendered from wall.env so the PSK lives in exactly one place.
if [ -n "${WIFI_SSID:-}" ] && [ "${WIFI_SSID}" != "REPLACE_WITH_WIFI_SSID" ]; then
    NETPLAN=/etc/netplan/60-wall-wifi.yaml
    # BASH SUBSTITUTION, NOT sed — and this is a correctness fix, not a style
    # one. These two values are a real SSID and a real Wi-Fi PSK, i.e. arbitrary
    # user text, and they used to be pasted straight into a `s|…|…|` expression:
    #   PSK containing '|'  -> sed errors, and under `set -e` firstboot dies
    #                          HERE, before installing the tty1 autologin, so the
    #                          panel never enters the kiosk at all;
    #   SSID containing '&' -> sed expands it to the whole match and the netplan
    #                          silently names the WRONG network;
    #   backslashes         -> same class, silently.
    # On a machine whose only link is Wi-Fi and whose console account is locked,
    # either outcome is a panel nobody can reach. `${var//pat/repl}` treats the
    # replacement as literal text, so nothing needs escaping and nothing can be
    # re-interpreted.
    NETPLAN_BODY="$(cat "$PAYLOAD/netplan-wifi.yaml.template")"
    NETPLAN_BODY="${NETPLAN_BODY//@@WIFI_SSID@@/$WIFI_SSID}"
    NETPLAN_BODY="${NETPLAN_BODY//@@WIFI_PSK@@/${WIFI_PSK:-}}"
    printf '%s\n' "$NETPLAN_BODY" > "$NETPLAN"
    grep -q '@@WIFI_' "$NETPLAN" && \
        warn "netplan still holds an @@WIFI_*@@ placeholder — the template gained a knob wall-firstboot.sh does not fill."
    # 0600 or netplan refuses to read it (and it holds the PSK).
    chmod 0600 "$NETPLAN"
    netplan generate >/dev/null 2>&1 || warn "netplan generate failed — check $NETPLAN"
    log "quirk 5: $NETPLAN rendered (0600). Apply with: netplan apply"
else
    warn "netplan NOT rendered — WIFI_SSID is unset/placeholder. The panel keeps"
    warn "whatever the installer configured; it has no ethernet to fall back on."
fi

# ── 6. D-W4 — the sleep window ───────────────────────────────────────────────
# systemd cannot interpolate an env var into OnCalendar, so the two timers are
# GENERATED here from the one schedule both sleep modes share.
render_timer() {
    local unit="$1" when="$2" desc="$3"
    cat > "/etc/systemd/system/${unit}.timer" <<EOF
# GENERATED by wall-firstboot.sh from $ENV_FILE (SLEEP_START/SLEEP_END).
# Edit wall.env and re-run wall-firstboot.sh; hand edits here are overwritten.
[Unit]
Description=$desc

[Timer]
OnCalendar=*-*-* ${when}:00
# The panel is asleep for part of the day and loses power without warning
# (quirk 4b: no battery = no UPS), so a missed boundary must still fire.
Persistent=true

[Install]
WantedBy=timers.target
EOF
}
render_timer wall-sleep "$SLEEP_START" "Enter the wall panel's sleep window (SLEEP_MODE=$SLEEP_MODE)"
render_timer wall-wake  "$SLEEP_END"   "Leave the wall panel's sleep window"
systemctl daemon-reload

# SAY WHICH CLOCK THE WINDOW IS IN. Both boundaries are wall-clock times read in
# LOCAL time - systemd resolves OnCalendar locally, and wall-sleep.sh arms the
# RTC from `date -d "today $SLEEP_END"` - so the timezone is part of the setting,
# not context for it. The autoinstall sets it (user-data `timezone:`), but an
# image built before that key existed, or a hand-installed panel, lands on
# subiquity's UTC default and the window silently slides by the UTC offset.
# Measured on the real panel 2026-09-05: 22:00-06:30 on a UTC box meant asleep at
# 17:00 local and awake from 01:30. Logging the zone is what makes that visible
# in `journalctl -u wall-firstboot` instead of only on the wall in December.
PANEL_TZ="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo unknown)"
case "$PANEL_TZ" in
    UTC|Etc/UTC|unknown)
        warn "the sleep window ${SLEEP_START}-${SLEEP_END} will be read in '$PANEL_TZ'."
        warn "These are LOCAL wall-clock times. If this site is not actually on UTC the"
        warn "panel sleeps and wakes at the wrong hours - and nothing else reports it."
        warn "Fix: timedatectl set-timezone <Area/City>, then re-run this script."
        ;;
    *)  log "D-W4: sleep window is read in $PANEL_TZ (local time)" ;;
esac

enable_unit_now "D-W4: sleep window ${SLEEP_START}-${SLEEP_END} $PANEL_TZ, SLEEP_MODE=$SLEEP_MODE, RTC wake=$SLEEP_RTC_WAKE" \
    wall-sleep.timer wall-wake.timer

# ── 6b. SN-015 — occupancy power ─────────────────────────────────────────────
# The timer is TRACKED rather than rendered (fixed cadence, nothing to
# interpolate), so all that happens here is enable/disable plus saying, in
# words, which of the two behaviours this panel actually has. The knob itself is
# read by wall-sleep.sh at every tick, so this is about the tick existing at all.
if [ "$WALL_ABSENCE_ENABLED" = "true" ]; then
    if [ ! -f /etc/systemd/system/wall-occupancy.timer ]; then
        fail_step "WALL_ABSENCE_ENABLED=true but wall-occupancy.timer is NOT installed (the autoinstall late-commands place it). Occupancy would be silently inert: the backlight would never follow presence and the absence timeout would never fire, while wall.env claims the feature is on. Copy it and wall-occupancy.service from $PAYLOAD/ and: systemctl enable --now wall-occupancy.timer"
    else
        # The decider is a separate file from the unit, and a missing decider is
        # the FAIL-SAFE (wall-sleep.sh then does nothing at all) — which is
        # exactly the silent-green shape this script exists to refuse, so it is
        # an error here even though the panel is perfectly usable.
        if [ ! -f /usr/local/sbin/wall-occupancy.py ]; then
            fail_step "WALL_ABSENCE_ENABLED=true but /usr/local/sbin/wall-occupancy.py is missing. Every tick would decide NOTHING (fail-safe: never suspend, never dim), so the panel would merely look 'always on' with no error anywhere. Copy it from $PAYLOAD/wall-occupancy.py and chmod +x."
        fi
        enable_unit_now "SN-015: occupancy power ON - backlight follows presence; ${WALL_ABSENCE_TIMEOUT_MIN} min of absence OUTSIDE the on-period ${SLEEP_END}-${SLEEP_START} suspends with the RTC wake at ${SLEEP_END}; presence inside the on-period never suspends. Presence is read from $WALL_PRESENCE_FILE and absence is believed ONLY when positively and freshly asserted."             wall-occupancy.timer
    fi
else
    # NOT an error: off is the default, and the whole point of it is that the
    # panel then behaves exactly as it did before SN-015. Disabling rather than
    # merely not-enabling is what makes flipping the knob back off stop the tick.
    if [ -f /etc/systemd/system/wall-occupancy.timer ]; then
        systemctl disable --now wall-occupancy.timer >/dev/null 2>&1 || true
    fi
    log "SN-015: occupancy power OFF (WALL_ABSENCE_ENABLED=false) - the ${SLEEP_START} SLEEP_START schedule stands unchanged and no presence is consulted."
fi

# The mem_sleep_default decision (user-data §6): report, never silently rewrite
# the kernel cmdline on a keyboard-less wall-mounted machine.
if [ -r /sys/power/mem_sleep ]; then
    MS="$(cat /sys/power/mem_sleep)"
    log "D-W4: /sys/power/mem_sleep = $MS"
    case "$MS" in
        *"[deep]"*) log "D-W4: S3 'deep' is the running default — nothing to do." ;;
        *deep*)     warn "S3 'deep' is SUPPORTED but NOT the default (s2idle is)."
                    warn "s2idle on this hardware generation often saves little more than"
                    warn "backlight-off, which defeats SLEEP_MODE=suspend. Add"
                    warn "mem_sleep_default=deep to the kernel cmdline (the exact sed is in"
                    warn "the wall user-data §6), then re-run the suspend/resume test." ;;
        *)          warn "S3 'deep' is NOT offered by this firmware at all. SLEEP_MODE=suspend"
                    warn "will only reach s2idle; consider SLEEP_MODE=backlight instead." ;;
    esac
else
    warn "/sys/power/mem_sleep unreadable — cannot tell which suspend state is default."
fi

# ── 7. kiosk autologin on tty1 ───────────────────────────────────────────────
# cage needs a logind SEAT, and a plain system service has none. An autologin
# getty on tty1 produces a real session with seat0, and the shell profile then
# execs the kiosk. (The seatd package is installed as the documented fallback if
# this path ever misbehaves — see WALL-BURN-IN.md §5.)
install -d -m 0755 /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf <<'EOF'
# WALL PANEL — autologin the kiosk user on tty1 so cage gets a logind seat.
# The account has a LOCKED password (SSH key-only), so this grants nothing that
# physical access to an unlocked wall panel does not already grant.
[Service]
ExecStart=
ExecStart=-/sbin/agetty --noissue --autologin panel %I $TERM
Type=idle
EOF
# ── the Electron host's PRIVATE renderer config ─────────────────────────────
# Secret shell values originate on this panel, in root-only wall.env. They are
# merged into the existing private host JSON so access registration survives a
# rerun, then installed for the panel account at 0600. Only the path crosses
# into kiosk.env; the values never enter the command line or hub-served files.
WALL_HOST_CONFIG=${WALL_HOST_CONFIG:-/etc/wall-panel/host.json}
install -d -m 0755 "$(dirname "$WALL_HOST_CONFIG")"
HOST_CONFIG_TMP=$(mktemp "$(dirname "$WALL_HOST_CONFIG")/.host.json.XXXXXX")
cleanup_host_config_tmp() { rm -f "$HOST_CONFIG_TMP"; }
trap cleanup_host_config_tmp EXIT
if python3 "$PAYLOAD/render-wall-host-config.py" "$WALL_HOST_CONFIG" > "$HOST_CONFIG_TMP"; then
    install -o panel -g panel -m 0600 "$HOST_CONFIG_TMP" "$WALL_HOST_CONFIG"
    rm -f "$HOST_CONFIG_TMP"
    log "private Electron host config rendered at $WALL_HOST_CONFIG (0600; values not logged)"
else
    rm -f "$HOST_CONFIG_TMP"
    fail_step "private Electron host config could not be rendered"
fi
trap - EXIT

# ── the kiosk's OWN config file, because it cannot read wall.env ─────────────
# THE KIOSK RUNS AS `panel`. /etc/wall-panel/wall.env is 0600 root:root — it
# holds the Wi-Fi PSK and, optionally, music passwords — so wall-kiosk.sh's
# load_env_file read NOTHING from it and every value fell back to its default.
# Measured on the first real boot (2026-08-03): the kiosk logged
# `PANEL_URL=https://:8443/` — an EMPTY WALL_HOST, i.e. a URL that cannot
# resolve — and silently dropped the flags configured in WALL_APP_CMD. On real
# hardware that is a panel that can never reach its hub, with nothing in the
# journal to say why.
#
# The fix is NOT to loosen wall.env. Nothing the kiosk needs is secret, so
# render exactly those four knobs into a world-readable file and leave the PSK
# where it was. Regenerated on every firstboot run, which is already the
# supported way to apply a wall.env change.
KIOSK_ENV=/etc/wall-panel/kiosk.env
{
    echo "# GENERATED by wall-firstboot.sh from $ENV_FILE. Do not edit."
    echo "# The NON-SECRET subset wall-kiosk.sh needs, readable by the kiosk user."
    echo "# Everything secret stays in wall.env, which is 0600 root:root."
    echo "WALL_HOST=${WALL_HOST:-}"
    echo "WALL_PORT=${WALL_PORT:-8443}"
    echo "WALL_APP_CMD=${WALL_APP_CMD:-/opt/wall-panel/app/wall-shell}"
    echo "WALL_MEDIA_CACHE=${WALL_MEDIA_CACHE:-/var/cache/wall-media}"
    # The cursor and camera knobs (2026-09-06). Non-secret by construction, and
    # read by wall-park-cursor.service and the panel's camera diagnostics.
    echo "WALL_CURSOR_PARK=${WALL_CURSOR_PARK:-true}"
    echo "WALL_CURSOR_PARK_CORNER=${WALL_CURSOR_PARK_CORNER:-bottom-right}"
    echo "WALL_CURSOR_PARK_DELAY=${WALL_CURSOR_PARK_DELAY:-10}"
    # Publish the same canonical boolean the kernel gate accepts. The sensor
    # process reads this file once at startup, so alternate input spellings
    # must not leave the driver and camera owner disagreeing.
    case "${WALL_CAMERA_ENABLED:-false}" in
        true|TRUE|yes|1) echo "WALL_CAMERA_ENABLED=true" ;;
        *) echo "WALL_CAMERA_ENABLED=false" ;;
    esac
    echo "WALL_CAMERA_DEVICE=${WALL_CAMERA_DEVICE:-/dev/video0}"
    echo "WALL_HOST_CONFIG=$WALL_HOST_CONFIG"
} > "$KIOSK_ENV"
chmod 0644 "$KIOSK_ENV"
log "kiosk: $KIOSK_ENV rendered (0644) — WALL_HOST='${WALL_HOST:-}' WALL_APP_CMD='${WALL_APP_CMD:-}'"
case "${WALL_HOST:-}" in
    ''|*REPLACE_WITH*)
        warn "WALL_HOST is unset/placeholder — PANEL_URL will not resolve and the shell"
        warn "will render nothing. Set it in $ENV_FILE and re-run this script." ;;
esac

install -d -m 0755 /etc/profile.d
cat > /etc/profile.d/zz-wall-kiosk.sh <<'EOF'
# WALL PANEL — start the kiosk session on tty1 only, and only once. An SSH login
# (which is how the panel is administered) must NOT launch a compositor.
if [ "$(tty)" = "/dev/tty1" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    exec /usr/local/bin/wall-kiosk.sh
fi
EOF
systemctl daemon-reload
# RESTART getty@tty1, or the panel does not enter the kiosk until a SECOND
# reboot. wall-firstboot.service runs After=network-online.target, by which time
# getty@tty1 is already up with its ORIGINAL configuration — and
# `daemon-reload` does not restart a running unit. So the first boot of a
# freshly imaged panel showed a plain login prompt on a machine whose account
# has a LOCKED password: no kiosk, and no way to log in at the console either.
# It looked exactly like a failed image. Restarting is also what makes
# `sudo /usr/local/sbin/wall-firstboot.sh` the documented way to apply a
# wall.env change: the kiosk comes back with the new values.
#
# `--no-block` because the drop-in sets `Type=idle`, which waits for the run
# queue to drain — and this script IS a job in that queue. A blocking restart
# would sit there until systemd's 5 s idle timeout gave up. Enqueue and return;
# the kiosk comes up a moment later either way.
systemctl restart --no-block getty@tty1.service >/dev/null 2>&1 \
    || warn "could not enqueue a getty@tty1 restart — the kiosk starts on the next reboot instead"
log "kiosk: tty1 autologin + profile hook installed, getty@tty1 restart enqueued (the session starts now, not next boot)"

# ── 8. OI-15/OI-18 — the media cache and the pull units ──────────────────────
# The panel PULLS its media (the Owner's ruling, 2026-07-29), from TWO hosts
# since OI-18 exit (b) (2026-08-03): wall-sync.service mirrors BOTH flows at boot
# and on resume, and wall-sync-frame.timer re-mirrors the frame flow every minute
# (storage-map §4d). Firstboot's job here is only to make the destinations exist
# and the units be enabled — the sync itself is NOT run from here, because a
# first sync can be the whole music library over Wi-Fi and firstboot must not
# block on it.
: "${WALL_MEDIA_CACHE:=/var/cache/wall-media}"

# GIVE THE MEDIA CACHE ITS OWN LV, BEFORE ANYTHING SYNCS INTO IT — 2026-08-09.
#
# THE FAILURE THIS PREVENTS WAS MEASURED, not imagined. On 2026-08-09 the frame
# sync mirrored 7.7 GB of a 43 GB share into this cache and took / to 100% —
# `/dev/mapper/ubuntu--vg-ubuntu--lv 15G 14G 0 100% /`. wall-sync failed, and it
# failed in the worst available way: a full ROOT filesystem, on a box whose only
# door is sshd, which then cannot write. The cache is the one path here that is
# sized by SOMEONE ELSE'S share — the frame source can grow at any time and this
# panel has no say in it.
#
# On its own LV, the same overfill fills the CACHE and stops. wall-sync reports a
# disk error, the shell keeps serving whatever it already mirrored, and the box
# stays reachable. That is the difference between a failed sync and a failed
# panel.
#
# The installer leaves about half the volume group unallocated (guided LVM, no
# sizing-policy), which is where this comes from — nothing had ever claimed it.
# `-l 60%FREE` rather than a literal size, so one line is right on the 238.5 GB
# production SSD and on a small lab VHDX; a fixed -L would fail outright on the
# smaller disk, at firstboot, on a box mounted on a wall. The remaining 40% is
# left unallocated deliberately: ext4 grows online in seconds and shrinks only
# unmounted, so an over-committed LV is expensive and a reserve is not.
setup_media_cache_lv() {
    command -v lvs >/dev/null 2>&1 || { log "media cache: no LVM tooling — leaving it on root"; return 0; }

    local rootdev vg
    rootdev="$(findmnt -no SOURCE / 2>/dev/null)"
    vg="$(lvs --noheadings -o vg_name "$rootdev" 2>/dev/null | awk '{$1=$1;print}')"
    [ -n "$vg" ] || { log "media cache: / is not on LVM — leaving it on root"; return 0; }

    # THE END-STATE QUESTION. Already mounted from our LV = the work is done, on
    # this boot and every later one.
    #
    # THIS GUARD USED TO ASK `lvs "$vg/wall-cache"` — THE LV THIS FUNCTION
    # CREATES TWENTY LINES BELOW — under a comment asserting it asked an input.
    # It is the same defect that cost run 3 on the hub (firstboot ordered its own
    # death at step 1b, then could never repair because its guard tested its own
    # output), fixed there by 527cebe and 15ea81d and never ported here. The
    # wrong comment made it worse than an unnoticed instance: anyone auditing for
    # the class read it and moved on.
    #
    # Five steps run AFTER the lvcreate — mkfs, carrying an existing cache
    # forward, the fstab entry, the mount. A death anywhere in there left the LV
    # present and nothing else done, and every later boot then said "already
    # exists, nothing to do". "Re-run the script", the documented repair for
    # every other step, was the one thing that could not work.
    if [ "$(findmnt -no SOURCE "$WALL_MEDIA_CACHE" 2>/dev/null)" = "/dev/mapper/${vg//-/--}-wall--cache" ]; then
        log "media cache: $WALL_MEDIA_CACHE is already on $vg/wall-cache — nothing to do"
        return 0
    fi

    if lvs "$vg/wall-cache" >/dev/null 2>&1; then
        # The LV is there but the mount is not, so a previous attempt died
        # part-way. RESUME rather than return: skip the carve, keep every step
        # after it. mkfs is re-run only if the volume has no filesystem, which is
        # the one case where the previous attempt stopped even earlier.
        log "media cache: $vg/wall-cache exists but $WALL_MEDIA_CACHE is not mounted from it — finishing the move"
        if ! blkid -s TYPE -o value "/dev/$vg/wall-cache" >/dev/null 2>&1; then
            log "media cache: the volume has no filesystem — making one"
            mkfs.ext4 -q -L wall-cache "/dev/$vg/wall-cache" || { warn "mkfs failed — leaving the media cache on root"; return 0; }
        fi
    else
        local freeext
        freeext="$(vgs --noheadings -o vg_free_count "$vg" 2>/dev/null | tr -d ' ')"
        if [ -z "$freeext" ] || [ "$freeext" -lt 256 ]; then
            log "media cache: $vg has no meaningful free space (${freeext:-0} extents) — leaving the cache on root"
            return 0
        fi

        log "media cache: carving $WALL_MEDIA_CACHE onto its own LV from $vg (${freeext} free extents)"
        lvcreate -y -l 60%FREE -n wall-cache "$vg" || { warn "lvcreate failed — leaving the media cache on root"; return 0; }
        mkfs.ext4 -q -L wall-cache "/dev/$vg/wall-cache"  || { warn "mkfs failed — leaving the media cache on root"; return 0; }
    fi

    # Preserve anything already mirrored. A re-image starts empty, but a box that
    # reached this line with a populated cache must not silently lose it behind a
    # new mount.
    install -d -m 0755 "$WALL_MEDIA_CACHE"
    local tmp; tmp="$(mktemp -d)"
    mount "/dev/$vg/wall-cache" "$tmp"
    if [ -n "$(ls -A "$WALL_MEDIA_CACHE" 2>/dev/null)" ]; then
        cp -a "$WALL_MEDIA_CACHE"/. "$tmp"/ 2>/dev/null || warn "could not copy the existing cache forward — it will be re-synced"
        rm -rf "${WALL_MEDIA_CACHE:?}"/* 2>/dev/null || true
    fi
    umount "$tmp"; rmdir "$tmp"

    # By UUID: fstab is read before anything could repair a stale device path.
    local uuid; uuid="$(blkid -s UUID -o value "/dev/$vg/wall-cache")"
    if ! grep -q "$uuid" /etc/fstab 2>/dev/null; then
        printf 'UUID=%s %s ext4 defaults,nofail 0 2\n' "$uuid" "$WALL_MEDIA_CACHE" >> /etc/fstab
    fi
    mount "$WALL_MEDIA_CACHE" 2>/dev/null || warn "could not mount $WALL_MEDIA_CACHE — the cache stays on root this boot"
    log "media cache: $WALL_MEDIA_CACHE is now $(findmnt -no SIZE "$WALL_MEDIA_CACHE" 2>/dev/null), separate from root"
}
setup_media_cache_lv

install -d -m 0755 "$WALL_MEDIA_CACHE" "$WALL_MEDIA_CACHE/music" "$WALL_MEDIA_CACHE/frame"
log "OI-15: media cache ready at $WALL_MEDIA_CACHE (music/ + frame/)"
# ════════════════════════════════════════════════════════════════════════════
# THE POINTER, AND THE CAMERA (2026-09-06)
# ════════════════════════════════════════════════════════════════════════════

# ── the arrow in the middle of the wall ─────────────────────────────────────
# `cage` parks its own default cursor at the centre of the output and never
# moves it, and no client can: Wayland has no pointer-warp. One synthetic
# relative motion drives it into a corner AND hands the pointer to Chromium,
# after which the shell's own `cursor: none` finally applies. Measured with
# `grim -c`; see wall.env.example's WALL_CURSOR_PARK block for the whole story.
install -d -m 0755 /usr/local/lib/wall-panel
if [ -f "$PAYLOAD/panel-poke.py" ]; then
    install -m 0755 "$PAYLOAD/panel-poke.py" /usr/local/lib/wall-panel/panel-poke.py
    log "cursor: /usr/local/lib/wall-panel/panel-poke.py installed"
else
    warn "panel-poke.py is not on the payload — the cursor cannot be parked, and"
    warn "verify-panel.sh loses its SPLIT screenshot (it drives the same tool)."
fi

if [ -f "$PAYLOAD/wall-park-cursor.service" ]; then
    install -m 0644 "$PAYLOAD/wall-park-cursor.service" /etc/systemd/system/wall-park-cursor.service
    systemctl daemon-reload >/dev/null 2>&1 || true
    case "${WALL_CURSOR_PARK:-true}" in
        true|TRUE|yes|1)
            enable_unit "wall-park-cursor.service enabled — the pointer is driven to the ${WALL_CURSOR_PARK_CORNER:-bottom-right} corner ${WALL_CURSOR_PARK_DELAY:-10}s after boot, which is also what lets the shell hide it"                 wall-park-cursor.service ;;
        *)
            systemctl disable --now wall-park-cursor.service >/dev/null 2>&1 || true
            log "cursor: WALL_CURSOR_PARK is '${WALL_CURSOR_PARK}' — unit disabled; expect cage's arrow in the CENTRE of the wall, which the shell cannot remove" ;;
    esac
else
    warn "wall-park-cursor.service is not on the payload — cage's arrow stays in the middle of the wall."
fi

# ── the camera: OFF AT THE KERNEL unless the knob says otherwise ────────────
# WALL_CAMERA_ENABLED=false does not mean "nothing opens it". It blacklists
# uvcvideo, so /dev/video* does not exist, nothing CAN open it, and the
# hardware activity LED cannot come on at all. A mechanism, not a policy —
# which is the point, because the LED is wired to the sensor's power rail and
# is the one claim about this camera that software cannot forge.
CAM_BLACKLIST=/etc/modprobe.d/wall-camera-off.conf
sensor_was_active=0
if systemctl is-active --quiet wall-sensors.service 2>/dev/null; then
    sensor_was_active=1
fi
case "${WALL_CAMERA_ENABLED:-false}" in
    true|TRUE|yes|1)
        if [ -f "$CAM_BLACKLIST" ]; then
            rm -f "$CAM_BLACKLIST"
            modprobe uvcvideo >/dev/null 2>&1 || true
            log "camera: WALL_CAMERA_ENABLED=true — uvcvideo un-blacklisted and loaded"
        else
            log "camera: WALL_CAMERA_ENABLED=true — uvcvideo available at ${WALL_CAMERA_DEVICE:-/dev/video0}"
        fi
        if [ ! -e "${WALL_CAMERA_DEVICE:-/dev/video0}" ]; then
            warn "camera: ${WALL_CAMERA_DEVICE:-/dev/video0} does not exist even though the camera is enabled."
            warn "  A UVC device publishes two nodes; check which is the CAPTURE node with:"
            warn "  python3 $PAYLOAD/panel-camera.py probe --device /dev/video1"
        fi ;;
    *)
        printf '# Wall panel: WALL_CAMERA_ENABLED=false in wall.env.
# Removing this file does NOT re-enable the camera across a firstboot run;
# set the knob instead, or the next run puts it back.
blacklist uvcvideo
' > "$CAM_BLACKLIST"
        chmod 0644 "$CAM_BLACKLIST"
        # Unload only if nothing holds it; a busy module means something is
        # using the camera RIGHT NOW, which is worth a loud line rather than a
        # forced removal.
        # Release our single camera owner before unloading. It restarts with the
        # newly rendered hardware gate false; Bluetooth can remain available.
        if [ "$sensor_was_active" = 1 ]; then
            systemctl stop wall-sensors.service || fail_step "camera: could not stop wall-sensors.service"
        fi
        if lsmod 2>/dev/null | grep -q '^uvcvideo'; then
            if modprobe -r uvcvideo >/dev/null 2>&1; then
                log "camera: WALL_CAMERA_ENABLED=false — uvcvideo blacklisted and unloaded; /dev/video* is gone and the activity LED cannot light"
            else
                fail_step "camera: requested off but NOT verified off; uvcvideo remains in use. Stop the camera consumer or reboot."
                warn "  Something on this panel is holding the camera open. Find it with: fuser -v /dev/video*"
            fi
        else
            log "camera: WALL_CAMERA_ENABLED=false — uvcvideo blacklisted; the camera cannot be opened"
        fi
        ;;
esac
# Both directions need a fresh process: its hardware gate is fixed at startup.
# A false-to-true change must not leave a permanently disabled camera daemon.
if [ "$sensor_was_active" = 1 ]; then
    systemctl restart wall-sensors.service || fail_step "camera: could not restart sensors with the updated hardware gate"
fi

if [ -f /etc/systemd/system/wall-sync.service ]; then
    enable_unit "OI-15: wall-sync.service enabled — both media flows are mirrored once after boot" \
        wall-sync.service
else
    fail_step "wall-sync.service is not installed (the autoinstall late-commands place it). Without it the panel will NEVER pull media — no music and no frame video, on a unit nothing will ever mark red. Re-image, or copy it from $PAYLOAD/wall-sync.service by hand and enable it."
fi
# OI-16a (the Owner, 2026-07-29): the resume hook. Enabling is what plants the
# suspend.target wants-symlink — an installed-but-disabled hook never fires, and
# a panel on SLEEP_MODE=suspend would then sync only at boot, i.e. ~never.
if [ -f /etc/systemd/system/wall-sync-resume.service ]; then
    enable_unit "OI-16a: wall-sync-resume.service enabled — every resume from suspend re-triggers the media sync" \
        wall-sync-resume.service
else
    fail_step "wall-sync-resume.service is not installed (the autoinstall late-commands place it). Without it a resume does NOT refresh the media cache — with SLEEP_MODE=suspend the panel can then run for weeks on a stale cache. Copy it from $PAYLOAD/wall-sync-resume.service and: systemctl enable wall-sync-resume.service"
fi
# D1 (2026-09-05): the pre-sleep teardown. Same enable-or-it-never-fires rule as
# the resume hook above, and the same class of silent failure — except this one
# does not show up as a stale cache, it shows up as a panel that stays lit all
# night because the freezer could not stop an rsync in time.
if [ -f /etc/systemd/system/wall-sync-suspend.service ]; then
    enable_unit "D1: wall-sync-suspend.service enabled — the media sync is stopped before the freezer runs, so a mirror in flight cannot block the sleep window"         wall-sync-suspend.service
else
    fail_step "wall-sync-suspend.service is not installed (the autoinstall late-commands place it). Without it a media sync running at SLEEP_START blocks the freeze for 40 s (deep, then s2idle) and the panel does NOT sleep — measured on the real panel 2026-09-05. Copy it from $PAYLOAD/wall-sync-suspend.service and: systemctl enable wall-sync-suspend.service"
fi
# OI-18: the frame flow has its OWN cadence — every minute, per storage-map §4d
# — so it has its own unit and timer. Enabling the TIMER is what matters; the
# service it triggers is deliberately not enabled on its own (nothing should
# start it at boot; wall-sync.service already covers the boot pass).
if [ -f /etc/systemd/system/wall-sync-frame.timer ]; then
    enable_unit "OI-18: wall-sync-frame.timer enabled — the frame share is re-checked every minute (skipped while Mini-serv sleeps)" \
        wall-sync-frame.timer
else
    fail_step "wall-sync-frame.timer is not installed (the autoinstall late-commands place it). Without it the frame videos refresh only at boot/resume, not every minute — and the frame flow's whole design is that its silence is normal, so nothing would ever look wrong. Copy it from $PAYLOAD/ and: systemctl enable --now wall-sync-frame.timer"
fi

# Report BOTH sources. Reported separately and named individually because they
# are two different machines with two different failure policies: an unset music
# UNC is a dead music player, an unset frame UNC is a blank wall, and a message
# naming only "the media source" would have been true of neither.
report_media_source() {   # LABEL UNC EXTRA
    case "${2:-}" in
        ''|*REPLACE_WITH*)
            warn "OI-18: $1 is unset/placeholder — the panel has NO $1 source."
            warn "wall-sync will FAIL loudly until it is filled in (that is deliberate:"
            warn "a wall with no media and a green unit would be a lie)."
            ;;
        *) log "OI-18: $1 source is $2 ($3)" ;;
    esac
}
report_media_source MEDIA_MUSIC_SHARE_UNC "${MEDIA_MUSIC_SHARE_UNC:-}" \
    "HOMEHUB; mirrors the Music/ subdir UNDER the mount, --delete"
report_media_source MEDIA_FRAME_SHARE_UNC "${MEDIA_FRAME_SHARE_UNC:-}" \
    "Mini-serv; mirrors the share ROOT, --delete; skipped silently while that box sleeps"
log "OI-15: sync now, or any time, with: sudo systemctl start wall-sync.service"

# ── 8b. IF-005 — is the shell artifact actually there, and can it load? ──────
# The autoinstall unpacks the artifact (user-data late-command 3b); this reports
# whether it worked, on the ONE boot where somebody is watching the journal.
#
# Two failures, and only one of them is visible from across the office:
#   - NOT INSTALLED — `[ -x ]` is false, wall-kiosk.sh paints the explicit
#     refusal screen. Loud by design.
#   - INSTALLED BUT DYING — `[ -x ]` is true, so no refusal screen; Electron
#     exits at load time and cage restarts it every 3 s behind a black
#     rectangle. The overwhelmingly likely cause is a missing shared library,
#     and `ldd` is the only thing that says so. Run it here, once, while the
#     answer is still in the journal next to everything else.
# `:-` first: wall.env may not declare WALL_APP_CMD at all and this script runs
# under `set -u`. `%% *` then drops any flags (the sim appends --disable-gpu),
# exactly as wall-kiosk.sh's own `[ -x "${WALL_APP_CMD%% *}" ]` does.
APP_BIN="${WALL_APP_CMD:-}"
APP_BIN="${APP_BIN%% *}"
: "${APP_BIN:=/opt/wall-panel/app/wall-shell}"
if [ -x "$APP_BIN" ]; then
    log "IF-005: shell artifact present at $APP_BIN"
    [ -r "$(dirname "$APP_BIN")/VERSION" ] && \
        log "IF-005: build = $(cat "$(dirname "$APP_BIN")/VERSION")"
    ELECTRON_BIN="$(dirname "$APP_BIN")/runtime/electron"
    if [ -x "$ELECTRON_BIN" ] && command -v ldd >/dev/null 2>&1; then
        MISSING="$(ldd "$ELECTRON_BIN" 2>/dev/null | awk '/not found/ {print $1}' | sort -u | tr '\n' ' ')"
        if [ -n "$MISSING" ]; then
            warn "IF-005: the Electron runtime is MISSING shared libraries: $MISSING"
            warn "The panel will show a BLACK screen, not the NOT INSTALLED screen — cage"
            warn "restarts a client that dies at load time, forever, with no message."
            warn "Fix: apt-get install the packages naming those sonames, then reboot."
            warn "The image's declared set is in"
            warn "  $PAYLOAD/electron-runtime-deps.tsv"
            warn "and every name there should already be in the image; a NEW one means the"
            warn "artifact was rebuilt against a newer Electron than this image was wired for."
        else
            log "IF-005: ldd resolves every library the Electron runtime needs"
        fi
    else
        warn "IF-005: could not run ldd against $ELECTRON_BIN — the runtime layout is not"
        warn "what packaging.md §4 describes, or ldd is absent. The library check did NOT run."
    fi
else
    warn "IF-005: NO shell artifact at $APP_BIN."
    warn "The panel will show the explicit 'NOT INSTALLED' screen — which is correct"
    warn "behaviour, not a crash. The image was built without the OfficeWallNaglight"
    warn "payload (nothing at /opt/wall-panel/wall-app/), or the unpack failed."
    warn "Rebuild the image with the artifact staged: see vmtest/build-wall-seed.sh."
fi

# ── 8c. CAN THIS PANEL RESOLVE A NAME AT ALL? ────────────────────────────────
# ADDED 2026-08-08, after a panel that had installed perfectly came up unable to
# resolve anything. `systemctl is-active systemd-resolved` said `not-found` — the
# package was never in wall/packages.list — so /etc/resolv.conf was a DANGLING
# symlink into a directory that does not exist, and glibc had nowhere to ask.
# networkd had the nameserver the whole time and nothing to write it into.
#
# THE SYMPTOM WAS A DARK WALL AND NOTHING ELSE. Electron failed the kiosk URL
# with ERR_NAME_NOT_RESOLVED, wall-sync could not resolve the hub for the music
# mount or Mini-serv for the frame pull, and both manifests were therefore
# absent. Four different-looking faults, one cause, and the only thing visible
# from the room was a blank screen.
#
# ASSERTED AS A RESOLUTION, NOT AS A PACKAGE OR A FILE. `dpkg -l` would go green
# on a box whose resolv.conf still pointed nowhere, and `[ -e /etc/resolv.conf ]`
# is TRUE for a dangling symlink — it was true on the broken panel. The only
# check that cannot be satisfied by the broken state is actually resolving
# something, so that is the check. WALL_HOST specifically: it is the name this
# machine exists to fetch, and the one whose absence blanks the wall.
#
# FAILS THE UNIT. The panel's whole failure mode is silence (SN-013), and this
# is the difference between "reimage it" and a week of looking at a dark screen.
if command -v getent >/dev/null 2>&1; then
    _resolve_target="${WALL_HOST:-}"
    if [ -z "$_resolve_target" ]; then
        warn "WALL_HOST is unset, so name resolution could not be checked against the name that matters."
    elif getent hosts "$_resolve_target" >/dev/null 2>&1; then
        log "name resolution works ($_resolve_target resolves)"
    else
        fail_step "THIS PANEL CANNOT RESOLVE NAMES. '$_resolve_target' does not resolve."
        warn "  Everything this box does over the network is by name, so the visible"
        warn "  symptom is a BLANK WALL and nothing else: the kiosk URL fails with"
        warn "  ERR_NAME_NOT_RESOLVED, the music mount cannot find the hub, and the"
        warn "  frame pull cannot find Mini-serv."
        warn "  Check, in this order:"
        warn "    systemctl is-active systemd-resolved   (not-found = the package is missing)"
        warn "    ls -l /etc/resolv.conf                 (a DANGLING symlink still passes -e)"
        warn "    networkctl status                      (networkd may know the DNS already)"
    fi
fi

# ── 8d. WSN-019 — credential-isolated Door stream broker ────────────────────
# The account owns no files and has no login. The root firstboot process writes
# exactly the Door allowlist into volatile /run files; PID 1 alone can traverse
# their 0700 directory and copies them into the unit's credential mount. The
# broker then publishes a 0660 socket to the panel group. Starting the unit opens
# NO RTSP connection by itself. While the panel is present and lit, the
# application may declare the FULL sampler eligible and may separately request
# public visible Door frames.
_door_cred_dir=/run/wall-door-credentials
_door_credential_names="host password port path username width height input-fov horizontal-fov vertical-fov yaw pitch stale-seconds start-seconds motion-enabled motion-calibrated motion-sample-fps motion-min-area-ratio motion-persistence-seconds motion-dwell-seconds motion-stationary-ratio motion-trigger-zone motion-road-zone motion-masks motion-diagnostics"

# Stop before removing the volatile inputs. A rerun with an incomplete payload
# must not leave the previous process or socket alive on stale credentials.
purge_door_runtime() {
    # Never unlink a live process's credential source and call that teardown.
    # systemctl itself can wedge on a stuck child, so every control operation is
    # bounded and the dedicated account is the final process-level authority.
    if ! timeout 5 systemctl mask --runtime wall-door-stream.service >/dev/null 2>&1; then
        fail_step "Door broker could not be runtime-masked against restart; retaining runtime state"
        return 1
    fi
    if ! timeout 7 systemctl stop wall-door-stream.service >/dev/null 2>&1; then
        warn "Door broker did not stop within seven seconds; applying bounded kill fallback"
    fi
    if pgrep -u wall-door-stream >/dev/null 2>&1; then
        timeout 2 pkill -KILL -u wall-door-stream >/dev/null 2>&1 || true
    fi
    if ! timeout 5 systemctl reset-failed wall-door-stream.service >/dev/null 2>&1; then
        fail_step "Door broker failed-state reset could not be confirmed; retaining runtime state"
        return 1
    fi
    _door_wait=0
    _door_stable=0
    while [ "$_door_wait" -lt 10 ]; do
        _door_unit_inactive=0
        if timeout 1 systemctl is-active --quiet wall-door-stream.service >/dev/null 2>&1; then
            _door_unit_inactive=0
        elif [ "$?" -eq 3 ]; then
            _door_unit_inactive=1
        fi
        if [ "$_door_unit_inactive" -eq 1 ] && ! pgrep -u wall-door-stream >/dev/null 2>&1; then
            _door_stable=$((_door_stable + 1))
        else
            _door_stable=0
        fi
        sleep 0.1
        _door_wait=$((_door_wait + 1))
    done
    if [ "$_door_stable" -ne 10 ]; then
        fail_step "Door broker stable inactivity could not be proven; retaining runtime credentials and socket"
        return 1
    fi
    if ! timeout 5 systemctl disable wall-door-stream.service >/dev/null 2>&1; then
        fail_step "Door broker disable operation failed after verified process teardown"
    fi
    for _door_cred_name in $_door_credential_names; do
        rm -f -- "$_door_cred_dir/$_door_cred_name"
    done
    rm -f -- /run/wall-door-stream/service.sock
}

door_motion_bool_valid() {
    [ "$1" = "true" ] || [ "$1" = "false" ]
}

door_motion_number_between() { # VALUE MIN MAX
    case "$1" in *'
'*) return 1 ;; esac
    printf '%s\n' "$1" | LC_ALL=C awk -v low="$2" -v high="$3" 'NR == 1 {
        value = $0
        if (value !~ /^([0-9]+([.][0-9]+)?|[.][0-9]+)$/) exit 1
        number = value + 0
        exit !(number >= low && number <= high)
    } NR != 1 { exit 1 }'
}

door_motion_zone_valid() { # X,Y,WIDTH,HEIGHT ALLOW_DISABLED
    case "$1" in *'
'*) return 1 ;; esac
    printf '%s\n' "$1" | LC_ALL=C awk -v allow_disabled="$2" 'NR == 1 {
        raw = $0
        count = split(raw, field, ",")
        if (count != 4) exit 1
        for (i = 1; i <= 4; i++) {
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", field[i])
            if (field[i] !~ /^([0-9]+([.][0-9]+)?|[.][0-9]+)$/) exit 1
            value[i] = field[i] + 0
            if (value[i] < 0 || value[i] > 1) exit 1
        }
        if (allow_disabled && value[3] == 0 && value[4] == 0) exit 0
        if (value[3] <= 0 || value[4] <= 0) exit 1
        exit !((value[1] + value[3] <= 1) && (value[2] + value[4] <= 1))
    } NR != 1 { exit 1 }'
}

door_motion_masks_valid() {
    [ -z "$1" ] && return 0
    case "$1" in *'
'*) return 1 ;; esac
    printf '%s\n' "$1" | LC_ALL=C awk 'NR == 1 {
        raw = $0
        if (length(raw) > 4096) exit 1
        zones = split(raw, zone, ";")
        if (zones > 32) exit 1
        for (z = 1; z <= zones; z++) {
            count = split(zone[z], field, ",")
            if (count != 4) exit 1
            for (i = 1; i <= 4; i++) {
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", field[i])
                if (field[i] !~ /^([0-9]+([.][0-9]+)?|[.][0-9]+)$/) exit 1
                value[i] = field[i] + 0
                if (value[i] < 0 || value[i] > 1) exit 1
            }
            if (value[3] <= 0 || value[4] <= 0 ||
                value[1] + value[3] > 1 || value[2] + value[4] > 1) exit 1
        }
        exit 0
    } NR != 1 { exit 1 }'
}

if ! getent group wall-door-stream >/dev/null 2>&1; then
    groupadd --system wall-door-stream
fi
if ! id -u wall-door-stream >/dev/null 2>&1; then
    useradd --system --gid wall-door-stream --home-dir /nonexistent \
        --shell /usr/sbin/nologin wall-door-stream
fi
if [ -f /etc/systemd/system/wall-door-stream.service ] && \
   [ -r /opt/wall-panel/app/runtime/resources/app/doorstream/service.py ] && \
   [ -r /opt/wall-panel/app/runtime/resources/app/doorstream/motion.py ]; then
    : "${DOORBELL_RTSP_PORT:=554}"
    : "${DOORBELL_RTSP_PATH:=/H.264}"
    : "${DOORBELL_RTSP_USERNAME:=admin}"
    : "${DOORBELL_OUTPUT_WIDTH:=960}"
    : "${DOORBELL_OUTPUT_HEIGHT:=540}"
    : "${DOORBELL_INPUT_FOV:=180}"
    : "${DOORBELL_HORIZONTAL_FOV:=110}"
    : "${DOORBELL_VERTICAL_FOV:=75}"
    : "${DOORBELL_YAW:=0}"
    : "${DOORBELL_PITCH:=0}"
    : "${DOORBELL_STALE_SECONDS:=4}"
    : "${DOORBELL_START_SECONDS:=12}"
    : "${DOORBELL_MOTION_ENABLED:=false}"
    : "${DOORBELL_MOTION_CALIBRATED:=false}"
    : "${DOORBELL_MOTION_SAMPLE_FPS:=5}"
    : "${DOORBELL_MOTION_MIN_AREA_RATIO:=0.002}"
    : "${DOORBELL_MOTION_PERSISTENCE_SECONDS:=2}"
    : "${DOORBELL_MOTION_DWELL_SECONDS:=3}"
    : "${DOORBELL_MOTION_STATIONARY_RATIO:=0.02}"
    : "${DOORBELL_MOTION_TRIGGER_ZONE:=0,0,1,1}"
    : "${DOORBELL_MOTION_ROAD_ZONE:=0,0,0,0}"
    : "${DOORBELL_MOTION_MASKS:=}"
    : "${DOORBELL_MOTION_DIAGNOSTICS:=false}"
    _door_credentials_ready=0
    install -d -m 0700 -o root -g root "$_door_cred_dir"
    purge_door_runtime

    # Validate even while disabled: a latent malformed value must not spring to
    # life after a later one-line enable toggle. Only explicit enabled AND
    # calibrated state may become the effective value handed to the service.
    _door_motion_valid=1
    for _door_bool_name in DOORBELL_MOTION_ENABLED DOORBELL_MOTION_CALIBRATED DOORBELL_MOTION_DIAGNOSTICS; do
        if ! door_motion_bool_valid "${!_door_bool_name}"; then
            fail_step "Door motion $_door_bool_name must be exactly true or false; motion remains disabled"
            _door_motion_valid=0
        fi
    done
    door_motion_number_between "$DOORBELL_MOTION_SAMPLE_FPS" 1 10 || { fail_step "Door motion sample FPS must be between 1 and 10"; _door_motion_valid=0; }
    door_motion_number_between "$DOORBELL_MOTION_MIN_AREA_RATIO" 0.000001 1 || { fail_step "Door motion minimum area ratio must be greater than 0 and at most 1"; _door_motion_valid=0; }
    door_motion_number_between "$DOORBELL_MOTION_PERSISTENCE_SECONDS" 0.1 30 || { fail_step "Door motion persistence must be between 0.1 and 30 seconds"; _door_motion_valid=0; }
    door_motion_number_between "$DOORBELL_MOTION_DWELL_SECONDS" 0.1 30 || { fail_step "Door motion dwell must be between 0.1 and 30 seconds"; _door_motion_valid=0; }
    door_motion_number_between "$DOORBELL_MOTION_STATIONARY_RATIO" 0 1 || { fail_step "Door motion stationary ratio must be between 0 and 1"; _door_motion_valid=0; }
    door_motion_zone_valid "$DOORBELL_MOTION_TRIGGER_ZONE" 0 || { fail_step "Door motion trigger zone must be one nonempty normalized rectangle"; _door_motion_valid=0; }
    door_motion_zone_valid "$DOORBELL_MOTION_ROAD_ZONE" 1 || { fail_step "Door motion road zone must be disabled or one normalized rectangle"; _door_motion_valid=0; }
    door_motion_masks_valid "$DOORBELL_MOTION_MASKS" || { fail_step "Door motion masks must be empty or normalized rectangles separated by semicolons"; _door_motion_valid=0; }

    _door_motion_effective=false
    if [ "$_door_motion_valid" -eq 1 ] && [ "$DOORBELL_MOTION_ENABLED" = "true" ]; then
        if [ "$DOORBELL_MOTION_CALIBRATED" = "true" ]; then
            _door_motion_effective=true
        else
            fail_step "Door motion was enabled without the explicit physical-calibration gate; motion remains disabled"
        fi
    fi
    if [ "$_door_motion_valid" -eq 0 ]; then
        # Do not hand malformed latent values to a future service version.
        DOORBELL_MOTION_SAMPLE_FPS=5
        DOORBELL_MOTION_MIN_AREA_RATIO=0.002
        DOORBELL_MOTION_PERSISTENCE_SECONDS=2
        DOORBELL_MOTION_DWELL_SECONDS=3
        DOORBELL_MOTION_STATIONARY_RATIO=0.02
        DOORBELL_MOTION_TRIGGER_ZONE=0,0,1,1
        DOORBELL_MOTION_ROAD_ZONE=0,0,0,0
        DOORBELL_MOTION_MASKS=
        DOORBELL_MOTION_DIAGNOSTICS=false
        DOORBELL_MOTION_CALIBRATED=false
    fi
    if [ -z "${DOORBELL_RTSP_HOST:-}" ] || [ -z "${DOORBELL_RTSP_PASSWORD:-}" ]; then
        fail_step "Door broker needs DOORBELL_RTSP_HOST and DOORBELL_RTSP_PASSWORD in wall.env"
    else
        printf '%s' "$DOORBELL_RTSP_HOST" > "$_door_cred_dir/host"
        printf '%s' "$DOORBELL_RTSP_PASSWORD" > "$_door_cred_dir/password"
        printf '%s' "$DOORBELL_RTSP_PORT" > "$_door_cred_dir/port"
        printf '%s' "$DOORBELL_RTSP_PATH" > "$_door_cred_dir/path"
        printf '%s' "$DOORBELL_RTSP_USERNAME" > "$_door_cred_dir/username"
        printf '%s' "$DOORBELL_OUTPUT_WIDTH" > "$_door_cred_dir/width"
        printf '%s' "$DOORBELL_OUTPUT_HEIGHT" > "$_door_cred_dir/height"
        printf '%s' "$DOORBELL_INPUT_FOV" > "$_door_cred_dir/input-fov"
        printf '%s' "$DOORBELL_HORIZONTAL_FOV" > "$_door_cred_dir/horizontal-fov"
        printf '%s' "$DOORBELL_VERTICAL_FOV" > "$_door_cred_dir/vertical-fov"
        printf '%s' "$DOORBELL_YAW" > "$_door_cred_dir/yaw"
        printf '%s' "$DOORBELL_PITCH" > "$_door_cred_dir/pitch"
        printf '%s' "$DOORBELL_STALE_SECONDS" > "$_door_cred_dir/stale-seconds"
        printf '%s' "$DOORBELL_START_SECONDS" > "$_door_cred_dir/start-seconds"
        printf '%s' "$_door_motion_effective" > "$_door_cred_dir/motion-enabled"
        printf '%s' "$DOORBELL_MOTION_CALIBRATED" > "$_door_cred_dir/motion-calibrated"
        printf '%s' "$DOORBELL_MOTION_SAMPLE_FPS" > "$_door_cred_dir/motion-sample-fps"
        printf '%s' "$DOORBELL_MOTION_MIN_AREA_RATIO" > "$_door_cred_dir/motion-min-area-ratio"
        printf '%s' "$DOORBELL_MOTION_PERSISTENCE_SECONDS" > "$_door_cred_dir/motion-persistence-seconds"
        printf '%s' "$DOORBELL_MOTION_DWELL_SECONDS" > "$_door_cred_dir/motion-dwell-seconds"
        printf '%s' "$DOORBELL_MOTION_STATIONARY_RATIO" > "$_door_cred_dir/motion-stationary-ratio"
        printf '%s' "$DOORBELL_MOTION_TRIGGER_ZONE" > "$_door_cred_dir/motion-trigger-zone"
        printf '%s' "$DOORBELL_MOTION_ROAD_ZONE" > "$_door_cred_dir/motion-road-zone"
        printf '%s' "$DOORBELL_MOTION_MASKS" > "$_door_cred_dir/motion-masks"
        printf '%s' "$DOORBELL_MOTION_DIAGNOSTICS" > "$_door_cred_dir/motion-diagnostics"
        chmod 0600 "$_door_cred_dir"/*
        _door_credentials_ready=1
    fi
    # The source is volatile, so boot ordering belongs to firstboot rather than
    # multi-user.target. Disable any old enablement before starting it here.
    if [ "$_door_credentials_ready" -eq 0 ]; then
        purge_door_runtime
    elif ! timeout 5 systemctl unmask --runtime wall-door-stream.service >/dev/null 2>&1; then
        fail_step "Door broker runtime mask could not be removed for a complete installation"
    elif ! timeout 7 systemctl restart wall-door-stream.service; then
        purge_door_runtime
        fail_step "Door broker could not restart with its Door-only credentials; inspect systemctl status wall-door-stream.service"
    else
        log "SR-024: Door broker ready — idle until present/lit sampler or visible-frame eligibility"
    fi
else
    purge_door_runtime
    fail_step "Door broker is incomplete: the unit or packaged doorstream/service.py + motion.py is missing. The Door tab and motion sampler will remain disabled."
fi

# ── 8e. SR-023 — feasibility-gated panel-local audio broker ────────────────
# The current backend is deliberately read-only/unavailable. It gives Electron
# a bounded status contract while refusing every mutation until a physical
# probe establishes the least-privilege BlueZ and PipeWire ownership boundary.
: "${WALL_AUDIO_ENABLED:=false}"
: "${WALL_AUDIO_SOCKET:=/run/wall-audio-router/service.sock}"
_wall_audio_complete=1
for _wall_audio_file in audio_router.py routing.py visualizer.py; do
    if [ ! -r "$PAYLOAD/../../panel-audio/$_wall_audio_file" ]; then
        fail_step "Panel audio payload is incomplete: missing $_wall_audio_file"
        _wall_audio_complete=0
    fi
done

if [ ! -f /etc/systemd/system/wall-audio-router.service ]; then
    fail_step "Panel audio payload is incomplete: missing wall-audio-router.service"
    _wall_audio_complete=0
fi
if [ "$WALL_AUDIO_SOCKET" != /run/wall-audio-router/service.sock ]; then
    fail_step "WALL_AUDIO_SOCKET must remain the panel-local broker socket"
    WALL_AUDIO_ENABLED=false
fi
if [ "$WALL_AUDIO_ENABLED" != true ] && [ "$WALL_AUDIO_ENABLED" != false ]; then
    fail_step "WALL_AUDIO_ENABLED must be exactly true or false"
    WALL_AUDIO_ENABLED=false
fi
# PID 1 reads this root-only, audio-only environment file before changing to
# User=panel. Never expose the broad wall.env (which also holds unrelated
# credentials) to the broker process.
install -d -m 0755 -o root -g root /etc/wall-panel
_wall_audio_env=/etc/wall-panel/audio-router.env
_wall_audio_env_new="${_wall_audio_env}.new"
printf 'WALL_AUDIO_ENABLED=%s\nWALL_AUDIO_SOCKET=/run/wall-audio-router/service.sock\n' \
    "$WALL_AUDIO_ENABLED" > "$_wall_audio_env_new"
chown root:root "$_wall_audio_env_new"
chmod 0600 "$_wall_audio_env_new"
mv -f "$_wall_audio_env_new" "$_wall_audio_env"
if [ "$WALL_AUDIO_ENABLED" = true ]; then
    if [ "$_wall_audio_complete" -ne 1 ]; then
        fail_step "Panel audio broker files are incomplete; leaving it stopped"
        systemctl stop wall-audio-router.service >/dev/null 2>&1 || true
    else
        systemctl disable wall-audio-router.service >/dev/null 2>&1 || true
        if ! systemctl restart wall-audio-router.service; then
            fail_step "Panel audio broker did not start; inspect its journal"
        else
            log "SR-023: bounded audio status broker enabled; device backend remains probe-gated"
        fi
    fi
elif [ "$WALL_AUDIO_ENABLED" = false ]; then
    systemctl disable wall-audio-router.service >/dev/null 2>&1 || true
    systemctl stop wall-audio-router.service >/dev/null 2>&1 || true
    log "SR-023: panel audio broker disabled (default)"
fi

# Touch fault filter is opt-in; OFF also restores the raw-input recovery path.
if ! WALL_ENV_FILE="$ENV_FILE" bash "$PAYLOAD/configure-touch-filter.sh"; then
    fail_step "Touch filter configuration failed; inspect wall-touch-filter.service."
fi

# ── 9. done — but only if it IS done ─────────────────────────────────────────
# The marker means "this panel is provisioned", and units, scripts and humans all
# read it that way. It is therefore written ONLY when every step that could fail
# did not: a panel with, say, no frame timer is not provisioned, and stamping it
# as such is how the missing cadence stays invisible for a month.
if [ "$PROVISION_FAILED" -ne 0 ]; then
    warn "panel configuration FAILED — see the ERROR line(s) above."
    warn "The provisioning marker $MARKER was NOT written and this unit is RED on"
    warn "purpose: something the panel needs is not in place, and a green firstboot"
    warn "claiming otherwise is the failure this check exists to prevent."
    warn "Fix the cause, then re-run: sudo /usr/local/sbin/wall-firstboot.sh"
    exit 1
fi
install -d -m 0755 "$(dirname "$MARKER")"
date > "$MARKER"
log "panel configuration complete. Remaining checks are hardware-only:"
log "  $PAYLOAD/WALL-BURN-IN.md"
