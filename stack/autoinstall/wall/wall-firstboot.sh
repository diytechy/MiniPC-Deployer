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
#   8. OI-15 — the media cache + the pull unit: create the cache dir, make sure
#      wall-sync.service is enabled, and REPORT whether the share is configured.
#      Plus OI-16a: wall-sync-resume.service enabled, so every wake from the
#      nightly suspend re-triggers the sync (a resume is not a boot).
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
    sed -e "s|@@WIFI_SSID@@|${WIFI_SSID}|g" -e "s|@@WIFI_PSK@@|${WIFI_PSK:-}|g" \
        "$PAYLOAD/netplan-wifi.yaml.template" > "$NETPLAN"
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
systemctl enable --now wall-sleep.timer wall-wake.timer >/dev/null 2>&1 || \
    warn "could not enable the sleep-window timers — check: systemctl status wall-sleep.timer"
log "D-W4: sleep window ${SLEEP_START}-${SLEEP_END}, SLEEP_MODE=$SLEEP_MODE, RTC wake=$SLEEP_RTC_WAKE"

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
install -d -m 0755 /etc/profile.d
cat > /etc/profile.d/zz-wall-kiosk.sh <<'EOF'
# WALL PANEL — start the kiosk session on tty1 only, and only once. An SSH login
# (which is how the panel is administered) must NOT launch a compositor.
if [ "$(tty)" = "/dev/tty1" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    exec /usr/local/bin/wall-kiosk.sh
fi
EOF
systemctl daemon-reload
log "kiosk: tty1 autologin + profile hook installed (session starts on next boot)"

# ── 8. OI-15 — the media cache and the pull unit ─────────────────────────────
# The panel PULLS its media (the Owner's ruling, 2026-07-29): wall-sync.service
# mirrors the share's Music/ + FrameVideos/ into WALL_MEDIA_CACHE at boot and on
# demand. Firstboot's job here is only to make the destination exist and the unit
# be enabled — the sync itself is NOT run from here, because a first sync can be
# the whole library over Wi-Fi and firstboot must not block on it.
: "${WALL_MEDIA_CACHE:=/var/cache/wall-media}"
install -d -m 0755 "$WALL_MEDIA_CACHE" "$WALL_MEDIA_CACHE/music" "$WALL_MEDIA_CACHE/frame"
log "OI-15: media cache ready at $WALL_MEDIA_CACHE (music/ + frame/)"
if [ -f /etc/systemd/system/wall-sync.service ]; then
    systemctl enable wall-sync.service >/dev/null 2>&1 \
        || warn "could not enable wall-sync.service — check: systemctl status wall-sync.service"
else
    warn "wall-sync.service is not installed (the autoinstall late-commands place it)."
    warn "Without it the panel will never pull media. Re-image, or copy it from"
    warn "$PAYLOAD/wall-sync.service by hand."
fi
# OI-16a (the Owner, 2026-07-29): the resume hook. Enabling is what plants the
# suspend.target wants-symlink — an installed-but-disabled hook never fires, and
# a panel on SLEEP_MODE=suspend would then sync only at boot, i.e. ~never.
if [ -f /etc/systemd/system/wall-sync-resume.service ]; then
    systemctl enable wall-sync-resume.service >/dev/null 2>&1 \
        || warn "could not enable wall-sync-resume.service — check: systemctl status wall-sync-resume.service"
    log "OI-16a: wall-sync-resume.service enabled — every resume from suspend re-triggers the media sync"
else
    warn "wall-sync-resume.service is not installed (the autoinstall late-commands place it)."
    warn "Without it a resume does NOT refresh the media cache — with SLEEP_MODE=suspend"
    warn "the panel can then run for weeks on a stale cache. Copy it from"
    warn "$PAYLOAD/wall-sync-resume.service and: systemctl enable wall-sync-resume.service"
fi
case "${MEDIA_SHARE_UNC:-}" in
    ''|*REPLACE_WITH*)
        warn "OI-15: MEDIA_SHARE_UNC is unset/placeholder — the panel has NO media source."
        warn "wall-sync.service will FAIL loudly at boot until it is filled in (that is"
        warn "deliberate: a wall with no music and a green unit would be a lie)."
        ;;
    *)
        log "OI-15: media source is $MEDIA_SHARE_UNC (mirror: Music/ + FrameVideos/, --delete)"
        log "OI-15: sync now, or any time, with: sudo systemctl start wall-sync.service"
        ;;
esac

# ── 9. done ──────────────────────────────────────────────────────────────────
install -d -m 0755 "$(dirname "$MARKER")"
date > "$MARKER"
log "panel configuration complete. Remaining checks are hardware-only:"
log "  $PAYLOAD/WALL-BURN-IN.md"
