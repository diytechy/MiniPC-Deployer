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
#   7. Autologin the kiosk user on tty1 so cage gets a real logind SEAT.
#   8. OI-15/OI-18 — the media cache + the pull units: create the cache dir, make
#      sure wall-sync.service is enabled, and REPORT whether BOTH shares are
#      configured (there are two sources on two hosts since OI-18 exit (b)).
#      Plus OI-16a: wall-sync-resume.service enabled, so every wake from the
#      nightly suspend re-triggers the sync (a resume is not a boot); and
#      wall-sync-frame.timer enabled, the frame flow's every-minute cadence.
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
: "${SLEEP_END:=06:30}"
: "${SLEEP_RTC_WAKE:=true}"
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
enable_unit_now "D-W4: sleep window ${SLEEP_START}-${SLEEP_END}, SLEEP_MODE=$SLEEP_MODE, RTC wake=$SLEEP_RTC_WAKE" \
    wall-sleep.timer wall-wake.timer

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
install -d -m 0755 "$WALL_MEDIA_CACHE" "$WALL_MEDIA_CACHE/music" "$WALL_MEDIA_CACHE/frame"
log "OI-15: media cache ready at $WALL_MEDIA_CACHE (music/ + frame/)"
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
