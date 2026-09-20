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
#  8e. Item S — enable the read-only DynamicUser thermal/CPU/presentation-mode
#      telemetry collector (WALL_TELEMETRY_ENABLED, default true).
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

# Replace a private file without ever exposing a truncated destination. The
# temporary lives beside the target so rename is atomic; both bytes and the
# directory entry are durable before success is reported.
atomic_install() { # SOURCE TARGET OWNER GROUP MODE
    local source=$1 target=$2 owner=$3 group=$4 mode=$5 temporary
    temporary=$(mktemp "$(dirname "$target")/.$(basename "$target").XXXXXX") || return 1
    if install -o "$owner" -g "$group" -m "$mode" "$source" "$temporary" \
            && python3 - "$temporary" "$target" <<'PY'
import os, sys
temporary, target = sys.argv[1:]
with open(temporary, 'rb') as stream:
    os.fsync(stream.fileno())
os.replace(temporary, target)
directory = os.open(os.path.dirname(target), os.O_RDONLY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
    then
        return 0
    fi
    rm -f "$temporary"
    return 1
}

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

# wall_camera_option_configured — true when THIS panel's wall.env enables any
# camera-related option (Item M, 2026-09-15 Owner ruling). Mirrored BY HAND in
# vmtest/lib/common.sh's build-time copy of this predicate — that one reads a
# staged deploy-payload/site/wall.env before the image exists (no shell
# module reaches both a build-time script and a boot-time one), so it borrows
# THIS file's own load_env_file to parse rather than re-implementing the
# rules; keep the three conditions themselves in sync by hand.
#
# Reads $WALL_ACCESS_MODE/$WALL_CAMERA_ENABLED/$WALL_CAMERA_DEVICE the way
# every other check in this script does: already exported by load_env_file
# (called on $ENV_FILE well above this point), never re-read from the file.
# An earlier version of this function re-parsed $ENV_FILE itself with a raw
# grep for the WALL_CAMERA_DEVICE case (2026-09-15 terra review #4) — a second,
# cruder parser of the same file that could disagree with load_env_file on a
# quoted value. There is now exactly one parser.
#
# Any of the following counts:
#   WALL_ACCESS_MODE=local        local access needs the panel's own sensing
#   WALL_CAMERA_ENABLED=true      (TRUE/yes/1 also count, same as elsewhere)
#   WALL_CAMERA_DEVICE=<anything> a camera node is explicitly configured
wall_camera_option_configured() {
    [ "${WALL_ACCESS_MODE:-gateway}" = "local" ] && return 0
    case "${WALL_CAMERA_ENABLED:-false}" in
        true|TRUE|yes|1) return 0 ;;
    esac
    [ -n "${WALL_CAMERA_DEVICE:-}" ] && return 0
    return 1
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

# enable_unit_optional — same contract, but a failure WARNS instead of failing
# the step, for a unit whose absence degrades the panel rather than breaking it.
#
# It exists so that "this component is optional" and "this component is declared
# in the release manifest" can both be true. A raw `systemctl enable` said the
# first and silently lost the second: panel_system_manifest.extract reads the
# enable HELPERS to learn which units a release must carry, and it refuses to
# generate a manifest it knows is short rather than emit one quietly missing a
# unit. So an optional unit still goes through a helper; only the consequence of
# failure differs.
enable_unit_optional() {
    local ok_line="$1"; shift
    if systemctl enable --now "$@" >/dev/null 2>&1; then
        log "$ok_line"
        return 0
    fi
    warn "systemctl enable --now failed for: $*. The panel keeps working without it. Check: systemctl status $1"
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
# COMMENTED-OUT LINES DO NOT COUNT. wall.env ships its optional settings as
# commented examples that still carry REPLACE_WITH_..., so a fully configured
# panel warned about placeholders on every firstboot and the warning stopped
# meaning anything. Only live assignments are placeholders.
if grep -v '^[[:space:]]*#' "$ENV_FILE" | grep -q "REPLACE_WITH"; then
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

# ── 5b. quirk 5b — the wait-online that waits for nothing ────────────────────
# MEASURED ON THE PANEL 2026-09-16: systemd-networkd-wait-online.service is the
# panel's ONLY failed unit, on every boot, and it has been failing since long
# before the work that found it. It times out after two minutes —
#   "Timeout occurred while waiting for network connectivity"
# — on a panel that is plainly routable the whole time.
#
# THE CAUSE IS NOT A SECOND INTERFACE, which is the usual shape of this failure.
# `networkctl list` on this panel shows exactly two links, `lo` and `wlp1s0`,
# and BOTH read `unmanaged`: quirk 5 above renders the Wi-Fi netplan with
# `renderer: NetworkManager`, so NetworkManager owns the only link that carries
# traffic and systemd-networkd manages nothing at all. A wait-online with no
# managed link to wait for cannot succeed — `--any` would not help, because the
# set it waits over is empty either way. The unit is simply not the right one on
# this machine: NetworkManager-wait-online.service is, it is enabled, and it
# reaches network-online.target in about five seconds.
#
# So the cost is two minutes of every boot plus a permanently red `systemctl
# --failed`, in exchange for nothing. Disable it — and disable it only on the
# EVIDENCE, not on the assumption: if some future panel does have a
# networkd-managed link, this leaves the unit exactly as it found it and says so.
# THE PROBE MUST SUCCEED, AND EVERY LINK MUST POSITIVELY SAY `unmanaged`
# (terra review, 2026-09-16). Two ways the first draft got this wrong, and both
# of them disabled the wait on evidence it did not have:
#
#   * `networkctl list` failing — a transient D-Bus answer is enough — produced
#     an EMPTY list, and an empty list is indistinguishable from "nothing is
#     managed" unless the exit status is checked. `|| true` threw that away.
#   * `pending` was treated as "not managed". It means the opposite of settled:
#     the link's ownership is not established yet. A networkd link still in
#     setup would have read as an absence.
#
# So: the probe must exit 0, and the disable path is taken only when EVERY
# non-loopback link reads exactly `unmanaged`. Anything else — a failed probe,
# a `pending`, a `configured` — leaves the unit alone and says so.
NETWORKD_PROBE_OK=0
NETWORKD_LINKS=""
if command -v networkctl >/dev/null 2>&1; then
    if NETWORKD_LINKS="$(networkctl list --no-legend 2>/dev/null)"; then
        NETWORKD_PROBE_OK=1
    fi
else
    NETWORKD_LINKS=""
fi
NETWORKD_CLAIMED="$(printf '%s\n' "$NETWORKD_LINKS" \
    | awk 'NF && $2 != "lo" && $NF != "unmanaged" { print $2 "(" $NF ")" }')"
if [ "$NETWORKD_PROBE_OK" != "1" ]; then
    warn "quirk 5b: networkctl did not answer, so nothing can be said about"
    warn "systemd-networkd-wait-online.service. Left exactly as it was."
elif [ -n "$NETWORKD_CLAIMED" ]; then
    log "quirk 5b: systemd-networkd still claims $(printf '%s' "$NETWORKD_CLAIMED" | tr '\n' ' ')— wait-online left enabled, it has something to wait for"
elif ! systemctl is-enabled --quiet NetworkManager-wait-online.service 2>/dev/null; then
    # THE REPLACEMENT HAS TO ACTUALLY BE THERE. `NetworkManager.service` being
    # up is not the same claim: the wait unit is what is pulled into
    # network-online.target, and disabling networkd's without NetworkManager's
    # being enabled would leave the target reached IMMEDIATELY and always —
    # a lie rather than a delay, for everything ordered after it, including
    # wall-firstboot.service itself and the media sync units.
    warn "quirk 5b: systemd-networkd manages no link, but"
    warn "NetworkManager-wait-online.service is NOT enabled — so nothing would"
    warn "hold network-online.target honestly. systemd-networkd-wait-online is"
    warn "LEFT ENABLED; enable NetworkManager-wait-online first."
elif ! systemctl is-active --quiet NetworkManager.service; then
    # Nothing is managed by networkd AND NetworkManager is not up. Disabling the
    # only wait-online on a machine with no other one would make
    # network-online.target a lie rather than a delay, so refuse and say why.
    warn "quirk 5b: systemd-networkd manages no link, but NetworkManager is NOT"
    warn "active either — so nothing here would reach network-online.target"
    warn "honestly. systemd-networkd-wait-online.service is LEFT ENABLED; find"
    warn "out what is meant to own this panel's link before changing that."
else
    systemctl disable --now systemd-networkd-wait-online.service >/dev/null 2>&1 \
        || warn "quirk 5b: could not disable systemd-networkd-wait-online.service"
    # It is already `failed` from this boot; without this the red line survives
    # the fix until the next reboot and looks like the fix did not work.
    systemctl reset-failed systemd-networkd-wait-online.service >/dev/null 2>&1 || true
    if systemctl is-enabled --quiet systemd-networkd-wait-online.service 2>/dev/null; then
        warn "quirk 5b: systemd-networkd-wait-online.service is STILL enabled after"
        warn "disable — every boot keeps its two-minute timeout and its red line."
    else
        log "quirk 5b: systemd-networkd-wait-online.service disabled (networkd manages no link here; NetworkManager-wait-online.service owns network-online.target)"
    fi
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
# rerun, then installed root-owned and group-readable by panel at 0640. Only the path crosses
# into kiosk.env; the values never enter the command line or hub-served files.
WALL_HOST_CONFIG=${WALL_HOST_CONFIG:-/etc/wall-panel/host.json}
install -d -m 0755 "$(dirname "$WALL_HOST_CONFIG")"
# Explicit legacy local-mode input is an opt-in migration into the root-owned
# source of truth. Never infer it from a missing camera flag or gateway outage.
LOCAL_CAPABILITY_STATE=/etc/wall-panel/local-capabilities.json
if [ ! -e "$LOCAL_CAPABILITY_STATE" ]; then
    case "${WALL_ACCESS_MODE:-gateway}" in
        local|LOCAL)
            LOCAL_STATE_TMP=$(mktemp /etc/wall-panel/.local-capabilities.XXXXXX)
            printf '%s\n' '{"localAccessEnabled":true,"revision":0,"schemaVersion":1}' > "$LOCAL_STATE_TMP"
            atomic_install "$LOCAL_STATE_TMP" "$LOCAL_CAPABILITY_STATE" root root 0600
            rm -f "$LOCAL_STATE_TMP"
            log "local setup: migrated explicit WALL_ACCESS_MODE=local into root-owned state" ;;
    esac
fi
HOST_CONFIG_TMP=$(mktemp "$(dirname "$WALL_HOST_CONFIG")/.host.json.XXXXXX")
cleanup_host_config_tmp() { rm -f "$HOST_CONFIG_TMP"; }
trap cleanup_host_config_tmp EXIT
if python3 "$PAYLOAD/render-wall-host-config.py" "$WALL_HOST_CONFIG" \
        "$LOCAL_CAPABILITY_STATE" > "$HOST_CONFIG_TMP"; then
    atomic_install "$HOST_CONFIG_TMP" "$WALL_HOST_CONFIG" root panel 0640
    rm -f "$HOST_CONFIG_TMP"
    log "private Electron host config rendered at $WALL_HOST_CONFIG (root:panel 0640; values not logged)"
    # ── the panel-local access state directory ─────────────────────────────
    # /var/lib is root-owned 0755, so the kiosk (which runs as `panel`) cannot
    # create this itself: without this line local mode would fail closed on
    # every boot with an unwritable state path and the panel would sit masked
    # and unavailable. Created empty; the panel writes the key and the
    # encrypted state on first run, and the PIN is set at the wall.
    install -d -o panel -g panel -m 0700 /var/lib/wall-panel

    # ── which lock is in force, stated once in the journal ─────────────────
    # Local mode is the one posture a re-image DOES reproduce: it needs no
    # per-device secret, so WALL_ACCESS_MODE=local is enough and the manual
    # install-wall-capabilities.sh step below does not apply to it. The PIN
    # itself is still set by hand at the wall, and is never printed here.
    case "${WALL_ACCESS_MODE:-gateway}" in
        local|LOCAL)
            log "protected access: LOCAL mode (panel-local PIN; no gateway, no device credential)"
            log "protected access: set the PIN at the wall in Settings; until then the panel masks nothing" ;;
    esac
    # ── the re-image reminder for protected access (Group C, item 13) ───────
    # render-wall-host-config.py owns ONLY rendererConfig. The access half --
    # enabled/gatewayUrl/deviceId/deviceCredential/sensorSocket -- is a per-device
    # registration installed out of band by install-wall-capabilities.sh, and a
    # re-image starts the file again at {"enabled": false}. Nothing used to say
    # so, so a re-imaged panel came up with its checklist, Settings and Bluetooth
    # panes open to anyone and looked exactly like a working one.
    #
    # This only ever WARNS. It cannot enable access, it writes no credential,
    # and it is reversed by setting WALL_ACCESS_EXPECTED back to false.
    case "${WALL_ACCESS_EXPECTED:-false}" in
        true|TRUE|yes|1)
            if [ "${WALL_ACCESS_MODE:-gateway}" = "local" ]; then
                log "protected access: satisfied by WALL_ACCESS_MODE=local; no device credential is owed"
            elif python3 -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("enabled") is True else 1)' "$WALL_HOST_CONFIG" >/dev/null 2>&1; then
                log "protected access: $WALL_HOST_CONFIG is enabled as expected"
            else
                warn "protected access: WALL_ACCESS_EXPECTED=true but $WALL_HOST_CONFIG has enabled=false."
                warn "The panel's protected panes are OPEN. Re-run, as root:"
                warn "  install-wall-capabilities.sh --host-config PRIVATE.json --wheelhouse OFFLINE_WHEELS"
                warn "See stack/panel-access/PROVISIONING.md. No credential is written by firstboot."
            fi ;;
    esac
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
    echo "WALL_CURSOR_TRANSPARENT=${WALL_CURSOR_TRANSPARENT:-true}"
    # Legacy migration input only. Missing/false no longer blocks hardware;
    # the sensor's saved schema-v2 camera consent is the capture authority.
    case "${WALL_CAMERA_ENABLED:-false}" in
        true|TRUE|yes|1) echo "WALL_CAMERA_ENABLED=true" ;;
        *) echo "WALL_CAMERA_ENABLED=false" ;;
    esac
    echo "WALL_CAMERA_DEVICE=${WALL_CAMERA_DEVICE:-/dev/video0}"
    echo "WALL_HOST_CONFIG=$WALL_HOST_CONFIG"
} > "$KIOSK_ENV"
chmod 0644 "$KIOSK_ENV"
log "kiosk: $KIOSK_ENV rendered (0644) — WALL_HOST='${WALL_HOST:-}' WALL_APP_CMD='${WALL_APP_CMD:-}'"

# ── local sensors and root capability helper: image-owned, gateway-independent
SENSOR_WHEELHOUSE=/opt/wall-panel/sensor-wheelhouse
SENSOR_MODELS=/opt/wall-panel/sensor-models
# sync_sensor_models_from_payload — reconcile $SENSOR_MODELS with whatever
# bundle THIS payload's own sensor-models subdirectory carries, every
# firstboot run.
#
# Live release finding, 2026-09-15 (paired-deployment-02.json): a release
# install rebooted into a post-install gate refusal — file-stale on
# $SENSOR_MODELS/manifest.json, expecting the committed canonical digest and
# finding a hand-written copy from earlier that day. Cause: the image build's
# late-command copies the staged bundle straight to $SENSOR_MODELS (a
# sibling of $PAYLOAD, not under it) ONCE, at install time; the release lane
# only ever refreshes $PAYLOAD itself (/opt/wall-panel/stack/autoinstall/wall)
# and never touches that sibling directory at all. A release's own verified
# bundle therefore arrived inside the refreshed payload and sat there
# unused, while $SENSOR_MODELS kept whatever the image carried (or whatever
# a human had staged by hand) — exactly the drift this ONE reconciliation
# step, run on every firstboot invocation regardless of how the payload's
# copy got there, closes.
#
# Re-verifies the payload's own bundle against ITS OWN manifest.json before
# installing anything: that manifest.json is the committed, reviewed one
# (carried into $PAYLOAD by copy_repo_into_payload on an image build, or by
# the release payload archive on a release — both are the SAME git-tracked
# bytes, never a supplier-substituted copy), so this re-proves the .onnx
# bytes were not corrupted or tampered with between being staged and
# reaching this payload, the same discipline the installer's own
# --model-manifest anchor pays again below.
#
# Installed ATOMICALLY (temp dir beside $SENSOR_MODELS, root:root 0644 files
# / 0755 dir, `mv -T`, previous generation removed) — never in place, and
# never only some of the three files. When the payload carries no complete
# bundle at all, an existing $SENSOR_MODELS is left completely untouched:
# absence of a NEW bundle is not licence to remove or degrade one that is
# already installed and working.
sync_sensor_models_from_payload() {
    # Derived via dirname from the one literal, file-shaped payload reference
    # below, not from a bare directory reference:
    # test_firstboot_payload_files_are_all_tracked_and_shipped resolves every
    # literal payload path this script mentions against the git-tracked tree
    # and refuses one that names a directory rather than a file it can find.
    # The manifest.json this derives from IS such a file (always committed,
    # whether or not a bundle sits beside it).
    local payload_bundle
    payload_bundle="$(dirname "$PAYLOAD/sensor-models/manifest.json")"
    [ -f "$payload_bundle/manifest.json" ] || return 0
    [ -f "$payload_bundle/det_10g.onnx" ] || return 0
    [ -f "$payload_bundle/w600k_r50.onnx" ] || return 0
    if ! python3 "$PAYLOAD/check-sensor-models.py" \
            --expect "$payload_bundle/manifest.json" "$payload_bundle" >/dev/null 2>&1; then
        fail_step "sensors: $payload_bundle failed its own manifest check (tampered or corrupted since staging); NOT syncing it to $SENSOR_MODELS. Any existing runtime models are left untouched."
        return 0
    fi
    local new_dir
    new_dir=$(mktemp -d "$(dirname "$SENSOR_MODELS")/.$(basename "$SENSOR_MODELS").XXXXXX") || {
        fail_step "sensors: could not create a staging directory beside $SENSOR_MODELS; $SENSOR_MODELS left untouched"
        return 0
    }
    install -m 0644 -o root -g root "$payload_bundle/det_10g.onnx" "$new_dir/det_10g.onnx"
    install -m 0644 -o root -g root "$payload_bundle/w600k_r50.onnx" "$new_dir/w600k_r50.onnx"
    install -m 0644 -o root -g root "$payload_bundle/manifest.json" "$new_dir/manifest.json"
    chmod 0755 "$new_dir"
    rm -rf "$SENSOR_MODELS.previous"
    [ ! -e "$SENSOR_MODELS" ] || mv -T "$SENSOR_MODELS" "$SENSOR_MODELS.previous"
    mv -T "$new_dir" "$SENSOR_MODELS"
    rm -rf "$SENSOR_MODELS.previous"
    log "sensors: $SENSOR_MODELS synced from $payload_bundle"
}
sync_sensor_models_from_payload

PROJECTM_PRESETS=/opt/wall-panel/projectm-presets
# sync_projectm_presets_from_payload — install the Milkdrop preset pack.
#
# THIS IS SR-031 / LLR-019, which was open from 2026-09-14 until the Owner
# ruled the whole cream-of-the-crop pack in on 2026-09-18. Until then
# `projectm-presets/presets.lock` was empty by design, libprojectM rendered its
# built-in idle output, and the absence of this arm cost nothing --
# panel_system_manifest.py refused outright to generate a manifest for a
# non-empty lock precisely so that the day a preset was chosen, the missing
# installer could not be discovered on the wall.
#
# THE ARCHIVE IS ONE FILE AND THE LOCK IS THE PER-FILE CHECK. The release
# stages `projectm-presets.tar` into the payload (9795 files, ~136 MB, never
# committed -- the same treatment the ONNX bundle gets and for the same reason).
# Its digest is declared once in the system manifest; the integrity of each
# preset inside it comes from `projectm-presets/presets.lock`, which IS
# committed and reviewed. So a substituted archive fails the manifest, and a
# substituted preset inside an otherwise-correct archive fails the lock.
#
# ATOMIC, like the model bundle beside it: extract to a staging directory next
# to the target, verify, then `mv -T`. A panel must never be left with half a
# preset library, and an archive that fails verification leaves the EXISTING
# library untouched -- a bad new pack is not a reason to lose a working one.
sync_projectm_presets_from_payload() {
    # DERIVED FROM THE TRACKED LOCK, not written as a literal $PAYLOAD path.
    # test_firstboot_payload_files_are_all_tracked_and_shipped resolves every
    # literal payload path this script mentions against the git-tracked tree and
    # fails on one it cannot find -- and the archive is deliberately NEVER
    # committed (136 MB of an unlicensed pack). presets.lock IS committed, so it
    # is the one literal reference, exactly as sync_sensor_models_from_payload
    # derives its bundle directory from the tracked manifest.json beside it.
    local archive lock payload_presets
    lock="$PAYLOAD/projectm-presets/presets.lock"
    payload_presets="$(dirname "$lock")"
    archive="$(dirname "$payload_presets")/projectm-presets.tar"
    [ -f "$archive" ] || return 0
    [ -f "$lock" ] || { fail_step "projectm: $archive is staged but $lock is absent; refusing to install presets nothing can verify"; return 0; }
    local new_dir
    new_dir=$(mktemp -d "$(dirname "$PROJECTM_PRESETS")/.$(basename "$PROJECTM_PRESETS").XXXXXX") || {
        fail_step "projectm: could not create a staging directory beside $PROJECTM_PRESETS; it is left untouched"
        return 0
    }
    if ! tar -xf "$archive" -C "$new_dir" 2>/dev/null; then
        rm -rf "$new_dir"
        fail_step "projectm: $archive did not extract; $PROJECTM_PRESETS left untouched"
        return 0
    fi
    # Verify EVERY file against the committed lock. Parsed positionally: a
    # sha256 is 64 characters and the path runs to end of line, because these
    # filenames contain spaces, '#', '!' and '===' and a field split mangles
    # them (the bug that made the lock's own reproduce path report all 9795
    # missing).
    local missing=0 mismatch=0 count=0 sha rel actual
    while IFS= read -r line; do
        case "$line" in ''|'#'*) continue;; esac
        sha=${line:0:64}
        rel=${line:66}
        [ -n "$rel" ] || continue
        count=$((count + 1))
        if [ ! -f "$new_dir/$rel" ]; then missing=$((missing + 1)); continue; fi
        actual=$(sha256sum "$new_dir/$rel" | cut -d' ' -f1)
        [ "$actual" = "$sha" ] || mismatch=$((mismatch + 1))
    done < "$lock"
    if [ "$missing" -ne 0 ] || [ "$mismatch" -ne 0 ]; then
        rm -rf "$new_dir"
        fail_step "projectm: staged preset archive failed its own lock ($missing missing, $mismatch mismatched of $count); $PROJECTM_PRESETS left untouched"
        return 0
    fi
    find "$new_dir" -type d -exec chmod 0755 {} +
    find "$new_dir" -type f -exec chmod 0644 {} +
    chown -R root:root "$new_dir"
    rm -rf "$PROJECTM_PRESETS.previous"
    [ ! -e "$PROJECTM_PRESETS" ] || mv -T "$PROJECTM_PRESETS" "$PROJECTM_PRESETS.previous"
    mv -T "$new_dir" "$PROJECTM_PRESETS"
    rm -rf "$PROJECTM_PRESETS.previous"
    log "projectm: $count preset(s) installed to $PROJECTM_PRESETS, every one verified against presets.lock"
}
sync_projectm_presets_from_payload
# The face model bundle IS "complete" only when both .onnx files are there,
# not merely a manifest.json (2026-09-15 terra review #6: a manifest with no
# model bytes beside it used to read as "present" and get handed to the
# installer, which would then fail for a reason this step never named).
sensor_models_complete() {
    [ -d "$SENSOR_MODELS" ] \
        && [ -f "$SENSOR_MODELS/det_10g.onnx" ] \
        && [ -f "$SENSOR_MODELS/w600k_r50.onnx" ]
}
# Item M, 2026-09-15 Owner ruling: the bundle is a DEPENDENCY of any
# camera-related option, not a separate optional package. Checked and
# fail_step'd HERE, standalone and BEFORE any installer work (terra review
# #5) — not nested inside the SENSOR_WHEELHOUSE branch below, so a
# configured-but-missing bundle gets its own named step regardless of
# whether the wheelhouse itself is also present, and firstboot does not
# spend time on an installer run this panel can never finish correctly.
if wall_camera_option_configured && ! sensor_models_complete; then
    fail_step "sensors: wall.env enables a camera-related option (WALL_ACCESS_MODE=local, WALL_CAMERA_ENABLED=true, or WALL_CAMERA_DEVICE set) but $SENSOR_MODELS is absent or incomplete (needs det_10g.onnx AND w600k_r50.onnx). Face-shape presence and face unlock will never become ready. The image build stages this from WALL_SENSOR_MODELS (stack/autoinstall/wall/sensor-models/README.md); PIN, Bluetooth and motion presence remain available."
fi
if [ -d "$SENSOR_WHEELHOUSE" ]; then
    sensor_args=(--wheelhouse "$SENSOR_WHEELHOUSE")
    if sensor_models_complete; then
        # The ANCHOR is the payload's OWN reviewed, committed manifest
        # (stack/autoinstall/wall/sensor-models/manifest.json, carried by
        # copy_repo_into_payload on every image regardless of whether a
        # bundle was staged), never $SENSOR_MODELS/manifest.json — that copy
        # travelled here alongside the .onnx bytes themselves, so a supplier
        # who substitutes both together with a self-consistent manifest of
        # their own would sail through a check anchored on it (2026-09-15
        # terra review #3). $PAYLOAD is this same image's git-tracked
        # payload, so this is exactly check-wheelhouse-lock.py --expect's
        # pattern one step up: authenticity comes from a copy the supplier
        # of the MODELS never gets to touch.
        sensor_args+=(--models "$SENSOR_MODELS" --model-manifest "$PAYLOAD/sensor-models/manifest.json")
    fi
    if "$PAYLOAD/install-wall-capabilities.sh" "${sensor_args[@]}"; then
        log "sensors: gateway-independent runtime installed and protocol verified"
    else
        fail_step "sensors: offline runtime installation failed; PIN remains available but sensing is unavailable"
    fi
else
    fail_step "sensors: $SENSOR_WHEELHOUSE is missing from the image payload"
fi
# THE /usr/local/sbin POWER AND SYNC SCRIPTS WERE INSTALLED ONLY BY `user-data`,
# WHICH RUNS ONCE, AT IMAGE INSTALL TIME. Nothing refreshed them afterwards: not
# firstboot, and therefore not the release lane either, because the system
# manifest is DERIVED from this file and a component nothing here installs is a
# component no release can carry. Found on 2026-09-16 deploying WSN-057, when the
# `panel-display-off` arm reached /opt/wall-panel/stack and never reached
# /usr/local/sbin -- the socket helper called a verb the installed script did not
# have. The deployed wall-sleep.sh was five days older than the payload beside
# it, and wall-sync.sh had to be installed by hand earlier the same day.
#
# Installing them HERE puts them in the manifest and makes every boot re-assert
# them from the payload, which is what the rest of this file already does for
# everything it owns. `user-data` still installs them for the first boot, before
# this script exists to run; the two agree because both read the same payload.
# The loop header stays on ONE line ending `; do`. Both the payload-completeness
# test and the system-manifest generator resolve a payload path containing a loop
# variable by reading the loop that defines it, and each parses the header form
# `for _v in WORDS; do` with the words on the same line. A line continuation
# leaves the reference unresolvable, which those tools report rather than skip --
# that refusal is the point of them. (Do not write an example of such a path in
# a comment here either: the scanners read comments, and a made-up variable name
# in prose is an unresolvable reference exactly like a real one. Learned twice.)
for _sbin in wall-sleep.sh wall-occupancy.py wall-sensor-power-policy.py wall-sync.sh wall-media-manifest.py wall-wakeprep.sh; do
    if [ -f "$PAYLOAD/$_sbin" ]; then
        install -m 0755 "$PAYLOAD/$_sbin" "/usr/local/sbin/$_sbin"
    else
        fail_step "power/sync scripts: $_sbin is missing from the image payload"
    fi
done
unset _sbin

# ── the Pandora sign-in, carried across a reimage ────────────────────────────
# Owner, 2026-09-19. MEASURED BEFORE BUILT: the sign-in lives only in
# /home/panel/.config/officewall-shell/Partitions/pandora, and nothing in this
# lane deletes it — it survived all ~67 paired releases between the 2026-09-11
# reimage and 2026-09-18. A RELEASE never loses it. A REIMAGE does, because it
# recreates /home/panel, and until now nothing carried it forward.
#
# So this step is a RESTORE and never a save: the archive is made by hand before
# a reimage (`sudo wall-pandora-session save ...`) and staged into the payload's
# secret directory, which is how it survives the wipe. The tool refuses to touch
# a panel that already has a sign-in or whose profile a live Chromium is holding,
# so running it on every firstboot is safe and does nothing on the other 66
# invocations.
if [ -f "$PAYLOAD/wall-pandora-session.py" ]; then
    install -m 0755 "$PAYLOAD/wall-pandora-session.py" /usr/local/sbin/wall-pandora-session
else
    fail_step "pandora session: wall-pandora-session.py is missing from the image payload"
fi
# LOWER CASE, AND THAT IS NOT A STYLE CHOICE. `PANDORA_` is one of the declared
# wall-knob namespaces (scripts/validate_config.py, WALL_KNOB_NAMESPACES), so an
# ordinary local called PANDORA_SEED reads as an undeclared knob to the gate and
# to the next person. The knob is WALL_PANDORA_SESSION_SEED; this is just where
# its value is held for the next four lines.
_pandora_seed="${WALL_PANDORA_SESSION_SEED:-/opt/wall-panel/site/pandora-session.tar.gz}"
if [ ! -f "$_pandora_seed" ]; then
    log "pandora session: no staged archive at $_pandora_seed — the player will ask for the login once"
elif ! id panel >/dev/null 2>&1; then
    warn "pandora session: no 'panel' account yet — leaving $_pandora_seed alone"
else
    # NOT fail_step. A sign-in the Owner can restore with one tap on the panel is
    # not worth failing an image over, and a refusal here is usually the tool
    # working: "this panel already has a sign-in" is the answer on every re-run.
    if /usr/local/sbin/wall-pandora-session restore "$_pandora_seed"; then
        log "pandora session: restored from $_pandora_seed"
    else
        warn "pandora session: $_pandora_seed was not restored (see the line above);"
        warn "the panel still works — sign in to Pandora once on the wall."
    fi
fi
unset _pandora_seed

if [ -f "$PAYLOAD/wall-local-setup.py" ] && [ -f "$PAYLOAD/wall-local-setup.service" ]; then
    install -m 0755 "$PAYLOAD/wall-local-setup.py" /usr/local/lib/wall-panel/wall-local-setup.py
    install -m 0644 "$PAYLOAD/wall-local-setup.service" /etc/systemd/system/wall-local-setup.service
    enable_unit_now "local setup: privileged bootstrap/adjunct and sensor wake helper ready on /run/wall-local-setup/service.sock" \
        wall-local-setup.service
else
    fail_step "local setup: helper payload is incomplete"
fi
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
#
# ...AND IT MUST NOT HAPPEN DURING A RELEASE. A paired release runs
# `stop` (getty@tty1 stopped, tty1 session terminated, nothing left running out
# of /opt/wall-panel/app) and only THEN `system-install`, which is what invokes
# this script. The restart below then brought login -> wall-kiosk.sh -> cage ->
# Electron straight back up, so the `activate` phase two steps later refused to
# swap the app directory underneath a running kiosk — `RuntimeError: Kiosk still
# running` — and rolled the whole release back. Reproduced twice on 2026-09-16;
# it is an ORDERING fault, not a flake, and it failed EVERY no-reboot run that
# carried a system payload. `--system-reboot` hid it because that path keeps
# getty@tty1 masked across the reboot for a different reason.
#
# So the release lane sets PANEL_RELEASE_KIOSK_RESTART=0 for the duration of
# its transaction and OWNS the kiosk lifecycle itself: its `start` phase unmasks,
# re-enables and starts getty@tty1 after the swap, and its rollback path does the
# same. The lane ALSO masks the unit, which is the enforcement — this variable is
# what makes the log say the truth rather than warn about a failure that was
# intended. It is DELIBERATELY NOT in the WALL_ namespace: wall.env knobs are
# the operator's, declared in wall.env.example and asserted by
# scripts/validate_config.py, and an operator who set this one would silently
# lose the kiosk restart for ever. This is a private handshake from the release
# lane, set for one `subprocess.run` and gone. A hand-run `sudo /usr/local/sbin/wall-firstboot.sh` sets neither and
# is unchanged: it still restarts, which is what makes it the documented way to
# apply a wall.env change.
if [ "${PANEL_RELEASE_KIOSK_RESTART:-1}" = "0" ]; then
    log "kiosk: tty1 autologin + profile hook installed; getty@tty1 restart SUPPRESSED (PANEL_RELEASE_KIOSK_RESTART=0 — a release transaction owns the kiosk lifecycle and will start it after the app swap)"
else
    systemctl restart --no-block getty@tty1.service >/dev/null 2>&1 \
        || warn "could not enqueue a getty@tty1 restart — the kiosk starts on the next reboot instead"
    log "kiosk: tty1 autologin + profile hook installed, getty@tty1 restart enqueued (the session starts now, not next boot)"
fi

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

# ── the pointer image itself: fully transparent ─────────────────────────────
# Owner item 22 (2026-09-13). Parking the pointer in a corner answered "the
# arrow is in the MIDDLE of the wall"; it never answered "the arrow appears
# whenever I touch the screen", and nothing in the renderer can: Chromium sets
# a cursor image only for a pointer that has entered its surface, and the touch
# filter's scroll device is deliberately mouse-shaped (udev tags ID_INPUT_MOUSE
# only for REL_X + REL_Y + BTN_LEFT together, and libinput drops the wheels of
# anything it has not tagged), so a finger drag is pointer activity on the seat
# and the compositor draws a cursor for it.
#
# So the cursor IMAGE is made nothing. The theme is found by XCURSOR_PATH and
# must be named `default`: libwlroots.so.12 on the panel carries XCURSOR_PATH
# and no XCURSOR_THEME string, and cage 0.1.5 asks wlroots for a NULL theme,
# which resolves to "default". wall-kiosk.sh exports the path.
if [ -f "$PAYLOAD/panel-invisible-cursor.py" ]; then
    install -m 0755 "$PAYLOAD/panel-invisible-cursor.py" /usr/local/lib/wall-panel/panel-invisible-cursor.py
    case "${WALL_CURSOR_TRANSPARENT:-true}" in
        true|TRUE|yes|1)
            if python3 /usr/local/lib/wall-panel/panel-invisible-cursor.py                    /usr/local/share/wall-cursors/default >/dev/null 2>&1; then
                log "cursor: transparent theme written to /usr/local/share/wall-cursors/default"
            else
                warn "cursor: panel-invisible-cursor.py failed — the compositor's arrow will show on touch (item 22)."
            fi ;;
        *)
            # ONLY the theme this script generates. /usr/local/share/wall-cursors
            # is a search PATH, not our property: anything else dropped in it
            # later must survive this knob being turned off.
            rm -rf /usr/local/share/wall-cursors/default
            rmdir /usr/local/share/wall-cursors 2>/dev/null || true
            log "cursor: WALL_CURSOR_TRANSPARENT is '${WALL_CURSOR_TRANSPARENT}' — normal pointer kept" ;;
    esac
else
    warn "panel-invisible-cursor.py is not on the payload — the compositor's arrow shows on every touch."
fi

# ── panel audio: line input, volume rocker, amplifier trigger ───────────────
# The built-in 3.5 mm jack CANNOT receive audio — its only wired external pin is
# an output with no capture path in silicon — so analog input arrives on a USB
# audio-class adapter. The jack's old role as a trigger-tone control port was
# retired 2026-09-13: the LCUS-2 relay is the only amplifier actuator and the
# jack is a plain, currently unused output held for the headset leg (HomeHub
# item 23). The full measurement record is in stack/panel-audio/README.md.
#
# WALL_AUDIO_MODE picks the output chain, and this script RE-ASSERTS it on
# every boot rather than only seeding it:
#   trigger  audio out the adapter, amplifier commanded over the LCUS-2 relay
#   panel    everything out the panel's own speaker, amplifier not commanded
#   bus      one merged stereo bus behind the Mute/Headset/Speaker switch
install -d -m 0755 /etc/wall-panel

# The card map: the one place ALSA card ids appear. Everything else refers to
# them by role, so swapping the adapter is a knob rather than five edits that
# have to agree. Card IDS, not indexes -- indexes are assignment order and have
# swapped across a reboot on this panel before now.
_adapter="${WALL_AUDIO_ADAPTER_CARD:-ICUSBAUDIO7D}"
_builtin="${WALL_AUDIO_BUILTIN_CARD:-PCH}"
_loopback="${WALL_AUDIO_LOOPBACK_CARD:-Loopback}"
cat > /etc/wall-panel/audio-cards.conf <<EOF
# GENERATED by wall-firstboot.sh. Edit wall.env and re-run, not this file.
pcm.card_usb { type hw
    card "$_adapter"
    device 0
}
pcm.card_builtin { type hw
    card "$_builtin"
    device 0
}
pcm.card_loop_play { type hw
    card "$_loopback"
    device 0
    subdevice 0
}
pcm.card_loop_cap { type hw
    card "$_loopback"
    device 1
    subdevice 0
}
pcm.card_loop_tap_play { type hw
    card "$_loopback"
    device 0
    subdevice 1
}
pcm.card_loop_tap_cap { type hw
    card "$_loopback"
    device 1
    subdevice 1
}
# SUBDEVICE 2 IS THE ECHO CANCELLER'S (item 23 step 6, spike section 6.1).
# Subdevices 0 and 1 are taken by the merged bus and by the amplifier
# detector's tap; the card has 4, so 2 is free and is the one to use. The
# canceller writes the cancelled microphone into card_loop_mic_play and
# \`mic_clean\` snoops it back out -- a dsnoop for the same reason speaker_tap
# and spdif_in are, because more than one consumer will want the microphone
# and a raw open locks the rest out.
pcm.card_loop_mic_play { type hw
    card "$_loopback"
    device 0
    subdevice 2
}
pcm.card_loop_mic_cap { type hw
    card "$_loopback"
    device 1
    subdevice 2
}
ctl.card_loop_ctl { type hw
    card "$_loopback"
}
ctl.card_usb_ctl { type hw
    card "$_adapter"
}
ctl.card_builtin_ctl { type hw
    card "$_builtin"
}
EOF
chmod 0644 /etc/wall-panel/audio-cards.conf
cat > /etc/wall-panel/audio-cards.env <<EOF
# GENERATED by wall-firstboot.sh. Must agree with audio-cards.conf.
WALL_AUDIO_ADAPTER_CARD=$_adapter
WALL_AUDIO_BUILTIN_CARD=$_builtin
WALL_AUDIO_LOOPBACK_CARD=$_loopback
EOF
chmod 0644 /etc/wall-panel/audio-cards.env
log "audio: card map — usb=$_adapter builtin=$_builtin loopback=$_loopback"
if [ ! -d "/proc/asound/$_adapter" ]; then
    warn "audio: card id '$_adapter' is not present (see /proc/asound/cards)."
    warn "audio: the line input and the amplifier feed will not work until it is."
fi

# ── the centre/sub trim (item 23 step 3, D4, review finding 8) ─────────────
# INSTALLED ONLY IF ABSENT, exactly like amp-trigger.env and for the same
# reason: the Owner's tuning session (Owner ruling on finding 8: a dedicated
# effort after everything else is in) moves these six numbers with
# `wall-audio-output trim`, and a firstboot re-run must not throw away numbers
# somebody arrived at by standing in the room and listening. The wall.env knobs
# below are therefore the values a FRESH panel starts at, not a table this
# script re-asserts.
if [ ! -f /etc/wall-panel/audio-trim.env ]; then
    cat > /etc/wall-panel/audio-trim.env <<EOF
# GENERATED by wall-firstboot.sh. Move it with \`wall-audio-output trim\`.
WALL_AUDIO_TRIM_FRONT_L=${WALL_AUDIO_TRIM_FRONT_L:-1.0}
WALL_AUDIO_TRIM_FRONT_R=${WALL_AUDIO_TRIM_FRONT_R:-1.0}
WALL_AUDIO_TRIM_CENTER_L=${WALL_AUDIO_TRIM_CENTER_L:-0.5}
WALL_AUDIO_TRIM_CENTER_R=${WALL_AUDIO_TRIM_CENTER_R:-0.5}
WALL_AUDIO_TRIM_SUB_L=${WALL_AUDIO_TRIM_SUB_L:-0.5}
WALL_AUDIO_TRIM_SUB_R=${WALL_AUDIO_TRIM_SUB_R:-0.5}
EOF
    chmod 0644 /etc/wall-panel/audio-trim.env
    log "audio: centre/sub trim seeded — front 1.0/1.0, centre and sub 0.5 of each input ((L+R)/2)"
fi

# -- the mic knobs (item 23 step 4, spec C, D3, D4) ------------------------
# SEEDED ONLY IF ABSENT, exactly like the trim and amp-trigger.env: the capture
# level is a bench number and a firstboot re-run must not shout over one that
# somebody arrived at by listening to a recording.
#
# THE DEFAULT CAPTURE LEVEL IS A MEASUREMENT. The panel microphone was found
# CLIPPING at the level it had been left at (62 %, +12 dB of boost, peak 32768,
# -15.2 dBFS RMS). 24 % with no boost is what the panel read on 2026-09-14 after
# the AEC measurement run lowered it live, and it is seeded so that the stored
# value and the hardware agree instead of fighting.
#
# REAR_LEVEL is the adapter's own per-channel value for channels 4 and 5 on its
# 0..197 scale; 66 is what the Owner's bench session set the FRONT pair to, so
# it is a level already proven sane out of this adapter's analog section. It is
# a bench number: turn it with `wall-audio-output mic rear_level=N` while
# watching the desktop's input meter.
if [ ! -f /etc/wall-panel/audio-mic.env ]; then
    cat > /etc/wall-panel/audio-mic.env <<EOF
# GENERATED by wall-firstboot.sh. Move it with \`wall-audio-output mic\`.
WALL_AUDIO_MIC_CAPTURE_PERCENT=${WALL_AUDIO_MIC_CAPTURE_PERCENT:-62}
WALL_AUDIO_MIC_BOOST=${WALL_AUDIO_MIC_BOOST:-0}
WALL_AUDIO_MIC_HEADSET_CAPTURE_PERCENT=${WALL_AUDIO_MIC_HEADSET_CAPTURE_PERCENT:-60}
WALL_AUDIO_MIC_REAR_LEVEL=${WALL_AUDIO_MIC_REAR_LEVEL:-66}
WALL_AUDIO_MIC_REAR_GAIN=${WALL_AUDIO_MIC_REAR_GAIN:-1.0}
EOF
    chmod 0644 /etc/wall-panel/audio-mic.env
    log "audio: mic knobs seeded - capture 62% with no boost (the level that hears the room; 24% was the ADC noise floor), rear pair at 66"
fi

# The Bluetooth mic return's own poll interval, in its own file because
# wall-bt-call.service is not root-only and has no business reading wall.env.
if [ ! -f /etc/wall-panel/bt-mic.env ]; then
    printf '# GENERATED by wall-firstboot.sh.\nWALL_BT_MIC_POLL_SECONDS=%s\n' \
        "${WALL_BT_MIC_POLL_SECONDS:-5}" > /etc/wall-panel/bt-mic.env
    chmod 0644 /etc/wall-panel/bt-mic.env
fi

# A3's knob, in its own file for the same reason. REWRITTEN ON EVERY RUN,
# unlike the poll interval above which is seeded only if absent. That
# difference is deliberate: the poll interval is a tuning somebody may have
# arrived at by living with the panel, and this is a POLICY that comes from
# wall.env. A panel whose wall.env says one thing while a generated file says
# another is exactly the defect that left the microphone at the ADC noise
# floor through three rounds of return-path testing.
printf '# GENERATED by wall-firstboot.sh from wall.env. Do not hand-edit.\nWALL_BT_SCO_PLAYBACK=%s\n' \
    "${WALL_BT_SCO_PLAYBACK:-0}" > /etc/wall-panel/bt-call.env
chmod 0644 /etc/wall-panel/bt-call.env

# RETIRE THE UNIT THIS ONE REPLACED. wall-bt-mic.service became
# wall-bt-call.service when it gained the far-end leg, and an upgraded panel
# still has the old unit file on disk and still enabled. Left alone it starts
# a SECOND supervisor against the same D-Bus tree and the same SCO PCM: two
# processes racing to open one microphone, which presents as a microphone that
# works intermittently and is miserable to diagnose. Disabling has to happen
# while systemd can still see the unit, so the order here matters.
if [ -e /etc/systemd/system/wall-bt-mic.service ]; then
    systemctl disable --now wall-bt-mic.service >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/wall-bt-mic.service
    rm -rf /etc/systemd/system/wall-bt-mic.service.d
    rm -f /usr/local/lib/wall-panel/wall-bt-mic.py
    systemctl daemon-reload
    log "audio: wall-bt-mic.service retired; wall-bt-call.service supersedes it"
fi

for _f in asound-trigger-mode.conf asound-panel-mode.conf asound-bus-mode.conf; do
    if [ -f "$PAYLOAD/$_f" ]; then
        install -m 0644 "$PAYLOAD/$_f" "/etc/wall-panel/$_f"
    else
        warn "audio: $_f is not on the payload — that output mode will not work."
    fi
done

# Tunables are installed only if absent, so a re-run never discards thresholds
# somebody arrived at by living with the thing.
if [ -f "$PAYLOAD/amp-trigger.env" ] && [ ! -f /etc/wall-panel/amp-trigger.env ]; then
    install -m 0644 "$PAYLOAD/amp-trigger.env" /etc/wall-panel/amp-trigger.env
fi

if [ -f "$PAYLOAD/asound.conf" ]; then
    install -m 0644 "$PAYLOAD/asound.conf" /etc/asound.conf
    log "audio: /etc/asound.conf installed"
else
    warn "audio: asound.conf is not on the payload — no dmix, so the kiosk and the"
    warn "line-in passthrough cannot share the one playback substream this codec has."
fi

# THE MODULE OPTIONS AND THE INITRAMFS ARE TWO COPIES, AND ONLY ONE OF THEM IS
# READ AT BOOT. snd_usb_audio and snd_hda_intel both load from the initramfs,
# long before this script runs, so installing a changed wall-audio-index.conf
# into /etc/modprobe.d and stopping there changes NOTHING until something else
# happens to rebuild the initramfs. That is not theoretical: `index=1` was
# corrected to `index=1,3` on 2026-09-14 and the built-in codec stayed missing
# until update-initramfs -u was run BY HAND (00:04 and 00:13 on the panel).
#
# Rebuilt only when a file actually changed. firstboot re-runs on every boot,
# and update-initramfs takes tens of seconds.
_modprobe_changed=0
for _f in wall-audio-index.conf wall-aloop.conf; do
    [ -f "$PAYLOAD/$_f" ] || continue
    if ! cmp -s "$PAYLOAD/$_f" "/etc/modprobe.d/$_f"; then
        _modprobe_changed=1
    fi
    install -m 0644 "$PAYLOAD/$_f" "/etc/modprobe.d/$_f"
done
if [ "$_modprobe_changed" = 1 ]; then
    if command -v update-initramfs >/dev/null 2>&1; then
        if update-initramfs -u >/dev/null 2>&1; then
            log "audio: module options changed — initramfs rebuilt; the new ALSA card indexes take effect on the NEXT boot."
        else
            fail_step "audio: /etc/modprobe.d/wall-audio-index.conf changed but update-initramfs -u FAILED. snd_usb_audio and snd_hda_intel load from the initramfs, so the OLD card indexes are what the next boot will use and the built-in codec may fail to probe at all. Run: sudo update-initramfs -u"
        fi
    else
        fail_step "audio: module options changed but update-initramfs is not installed, so the initramfs still carries the OLD ALSA card indexes and the next boot will use them."
    fi
fi
# Options alone do not load a module; this is what does.
[ -f "$PAYLOAD/wall-aloop-load.conf" ] && install -m 0644 "$PAYLOAD/wall-aloop-load.conf" /etc/modules-load.d/wall-aloop-load.conf

# Device activation, because ConditionPathExists on a sound card loses the USB
# enumeration race and a unit skipped for an unmet condition is never retried.
# Since 2026-09-13 the same rule also gives the adapter a port-independent
# systemd alias, which is what the three audio units BindsTo= so that moving the
# USB hub to another port stops and restarts them (Owner item 25).
if [ -f "$PAYLOAD/90-wall-audio-adapter.rules" ]; then
    install -m 0644 "$PAYLOAD/90-wall-audio-adapter.rules" \
        /etc/udev/rules.d/90-wall-audio-adapter.rules
    # The predecessor. Left in place it would keep re-adding its own add-only
    # SYSTEMD_WANTS with no alias, which is harmless, and would keep claiming in
    # a comment that line-in is the only unit involved, which is not.
    rm -f /etc/udev/rules.d/90-wall-line-in.rules
    udevadm control --reload-rules >/dev/null 2>&1 || true
    # A panel being upgraded in place has the adapter already plugged in, so no
    # add event is coming: without this the alias would not exist until the next
    # re-plug or reboot and every BindsTo= would refuse to start.
    udevadm trigger --subsystem-match=sound >/dev/null 2>&1 || true
fi
# The hub's own recovery (item 25 addendum). Separate rule and separate unit
# from the adapter's: this one keys on the HUB's VID:PID, fires on the hub
# appearing rather than the adapter, and its action is a hardware reset of
# everything downstream, so it is deliberately not folded into the rule above.
if [ -f "$PAYLOAD/91-wall-usb-hub-reset.rules" ]; then
    install -m 0644 "$PAYLOAD/91-wall-usb-hub-reset.rules"         /etc/udev/rules.d/91-wall-usb-hub-reset.rules
    udevadm control --reload-rules >/dev/null 2>&1 || true
    # No trigger here, unlike the adapter rule above. That one needed one
    # because a BindsTo= cannot start without its alias; this one only wants to
    # run on a FUTURE add, and triggering it now would start a reset watch
    # against a hub that is working.
fi
# The headset adapter is a SECOND device alias (item 23 step 2): its arrival is
# the one-shot speaker -> headset switch, and its departure is what stops the
# headset leg. Separate from the 5.1 adapter's rule so a headset replug cannot
# cycle the amplifier units.
if [ -f "$PAYLOAD/91-wall-headset-adapter.rules" ]; then
    install -m 0644 "$PAYLOAD/91-wall-headset-adapter.rules"         /etc/udev/rules.d/91-wall-headset-adapter.rules
    udevadm control --reload-rules >/dev/null 2>&1 || true
    udevadm trigger --subsystem-match=sound >/dev/null 2>&1 || true
fi
if [ -f "$PAYLOAD/99-wall-amp-lcus2.rules" ]; then
    install -m 0644 "$PAYLOAD/99-wall-amp-lcus2.rules" /etc/udev/rules.d/99-wall-amp-lcus2.rules
    udevadm control --reload-rules >/dev/null 2>&1 || true
    udevadm trigger --subsystem-match=tty >/dev/null 2>&1 || true
fi

# wall_audio_state.py is the applier's pure core and is imported from beside
# it, so the two must land in the SAME directory or the switch cannot start.
for _f in panel-volume-request.py panel-volume-keys.py panel-amp-trigger.py wall-alsaloop-guard.py wall-usb-hub-reset.py wall_audio_state.py wall-bt-call.py panel-bus-visualizer.py; do
    if [ -f "$PAYLOAD/$_f" ]; then
        install -m 0755 "$PAYLOAD/$_f" "/usr/local/lib/wall-panel/$_f"
    else
        warn "audio: $_f is not on the payload."
    fi
done

# SR-041: the merged-bus visualizer's three imports. `panel-bus-visualizer.py`
# imports these from BESIDE itself, exactly the way the root applier imports
# wall_audio_state, and for the same reason: the daemon lives in
# /usr/local/lib/wall-panel and must not reach into /opt/wall-panel/stack, which
# is a different release lane and goes stale on its own schedule. They come from
# the panel-audio tree because that is the one home each of them has -- the
# broker reads the same `visualizer.py` from /opt, so a fix reaches both.
# A LOOP VARIABLE OF ITS OWN, and that is not cosmetic: the payload-coverage
# test in tests/test_panel_audio.py resolves every payload-relative reference by
# collecting the words a variable is ever assigned ANYWHERE in this file. Reusing
# `_f` here made it cross-multiply the two lists -- so it demanded
# `stack/panel-audio/panel-amp-trigger.py` and `stack/autoinstall/wall/pcm_frame.py`,
# neither of which exists, and the real coverage it was written to prove was lost
# in the noise.
for _pa in bus_source.py pcm_frame.py visualizer.py; do
    if [ -r "$PAYLOAD/../../panel-audio/$_pa" ]; then
        install -m 0644 "$PAYLOAD/../../panel-audio/$_pa" "/usr/local/lib/wall-panel/$_pa"
    else
        warn "audio: $_pa is not on the payload — the merged-bus visualizer will not start."
    fi
done

# Not a warning: both alsaloop units name the guard as their ExecStart, so an
# absent one is not a degraded panel, it is a panel with no audio path at all.
if [ ! -x /usr/local/lib/wall-panel/wall-alsaloop-guard.py ]; then
    fail_step "wall-alsaloop-guard.py is not installed. wall-line-in and wall-kiosk-loop both ExecStart it, so BOTH will fail to start and the panel will have no audio in either direction. Copy it from $PAYLOAD/ to /usr/local/lib/wall-panel/ with mode 0755."
fi

[ -f "$PAYLOAD/wall-audio-mode" ] && install -m 0755 "$PAYLOAD/wall-audio-mode" /usr/local/sbin/wall-audio-mode
# The switch applier. It imports wall_audio_state, which firstboot puts in
# /usr/local/lib/wall-panel, so the script is installed with a symlink there
# rather than copied into sbin on its own.
if [ -f "$PAYLOAD/wall-audio-output" ]; then
    install -m 0755 "$PAYLOAD/wall-audio-output" /usr/local/lib/wall-panel/wall-audio-output
    ln -sfn /usr/local/lib/wall-panel/wall-audio-output /usr/local/sbin/wall-audio-output
else
    warn "audio: wall-audio-output is not on the payload — the Mute/Headset/Speaker switch cannot be applied."
fi

# The ALSA half of the trim, rendered from the values above by the one piece of
# code that knows the adapter's channel order. Done HERE, after the applier is
# installed and before the mode case applies bus mode, because
# /etc/wall-panel/audio-trim.conf is the only definition of `pcm.speaker_multi`
# and the speaker leg's probe falls back to plain stereo without it. A failure
# is a warning, not a fail_step: the fallback is exactly the panel that shipped
# before step 3, with front out working and no centre or sub.
if [ -x /usr/local/sbin/wall-audio-output ]; then
    if /usr/local/sbin/wall-audio-output trim --render >/dev/null 2>&1; then
        log "audio: centre/sub route rendered to /etc/wall-panel/audio-trim.conf"
    else
        warn "audio: could not render the centre/sub trim — the speaker leg will fall back to stereo front out (no centre, no sub)."
    fi    # The mic leg's own generated route, for the same reasons and with the same
    # failure policy: /etc/wall-panel/audio-mic.conf is the only definition of
    # `pcm.mic_rear_route`, and without it the rear mic leg does not run while
    # everything else does. A warning, never a fail_step - a panel with no
    # microphone is a degraded panel, not a broken one.
    if /usr/local/sbin/wall-audio-output mic --render >/dev/null 2>&1; then
        log "audio: mic return route rendered to /etc/wall-panel/audio-mic.conf"
    else
        warn "audio: could not render the mic route - the microphone will not reach the desktop's input or a phone call."
    fi
fi

for _u in wall-volume-request.socket wall-volume-request@.service wall-line-in.service wall-volume-keys.service wall-kiosk-loop.service wall-amp-trigger.service          wall-usb-hub-reset@.service           wall-spdif-in.service wall-bus-speaker.service wall-speaker-out.service           wall-bus-headset.service wall-headset-present.service           wall-audio-state.service wall-audio-resume.service           wall-audio-apply.service wall-audio-apply.path           wall-mic-rear.service wall-bt-call.service           wall-bus-visualizer.service; do
    [ -f "$PAYLOAD/$_u" ] && install -m 0644 "$PAYLOAD/$_u" "/etc/systemd/system/$_u"
done

# SR-041: the merged-bus visualizer. ENABLED, unlike the two switch legs, and
# the difference is deliberate: the legs must not start audio in a position the
# Owner did not leave the switch in, while this only READS. Its ExecCondition
# keeps it out of the failure list in the three ALSA modes that do not declare
# `bus_monitor`, and the daemon re-checks the mode on every block so a live
# `wall-audio-mode` change is answered with `unavailable` rather than a stale
# document.
#
# ITS FAILURE IS A DECORATION'S FAILURE. The amplifier detector, the routing and
# the switch do not depend on it, so every branch here warns and none fails the
# step: a panel with no visualizer is a panel that shows Frame Media.
if [ -f /etc/systemd/system/wall-bus-visualizer.service ] &&
   [ -x /usr/local/lib/wall-panel/panel-bus-visualizer.py ]; then
    # THROUGH THE HELPER, not a raw `systemctl enable`. The failure is still a
    # warning — see the paragraph above — but the release manifest generator
    # reads the enable helpers to learn which units a release must carry, and a
    # raw enable would have left this unit undeclared while every test passed.
    if enable_unit_optional "SR-041: merged-bus visualizer source running; PCM endpoint idle until a local host asks" \
            wall-bus-visualizer.service; then
        # Already running from a previous boot? `enable --now` leaves an active
        # unit alone, so re-assert the new code explicitly. A restart failure is
        # the same decoration failure as the rest of this block.
        systemctl restart wall-bus-visualizer.service >/dev/null 2>&1 ||
            warn "audio: wall-bus-visualizer did not restart — inspect its journal. Routing and the amplifier are unaffected."
    else
        warn "audio: the visualizer will show Frame Media."
    fi
else
    warn "audio: wall-bus-visualizer is not installed — the visualizer will show Frame Media."
fi
# The hub reset is a TEMPLATE started by udev, so it is never enabled and has no
# [Install]. An installed rule with no unit behind it would be a silent no-op,
# which is the one failure mode worth a line of its own.
if [ -f /etc/udev/rules.d/91-wall-usb-hub-reset.rules ] &&
   [ ! -f /etc/systemd/system/wall-usb-hub-reset@.service ]; then
    warn "audio: 91-wall-usb-hub-reset.rules is installed but wall-usb-hub-reset@.service is not. A hub that enumerates with no ports will NOT be reset and the panel's audio will stay dead until someone toggles authorized by hand."
fi

# ── the echo canceller (item 23, step 6; HomeHub docs/AEC_SPIKE_2026-09-14.md)
# BUILT HERE, FROM SOURCE, AND NOT SHIPPED AS A BINARY. The payload carries the
# three C files and a Makefile; the image carries gcc, libasound2-dev and
# libspeexdsp-dev (stack/autoinstall/wall/packages.list). Building on the panel
# is what keeps the daemon matched to the libraries it links against, and this
# script already re-runs at every boot, so a rebuild is cheap and an out-of-date
# binary cannot survive an image update.
#
# A FAILURE HERE IS A WARNING, NEVER A fail_step. A panel with no canceller is
# the panel that shipped before step 6: the microphone works, the echo is not
# removed, and the mic seam below is not moved. That is degraded, not broken.
# THREE SHAPES HERE ARE DICTATED BY THE PAYLOAD INVENTORY TEST
# (tests/test_panel_audio.py), which resolves every payload reference rather
# than pattern-matching it, and fails on one it cannot resolve:
#   * the loop variable is `_aec`, not the `_f` used twice above. That test
#     unions every word any loop binds to a name and crosses it with every
#     reference using that name, so reusing `_f` would have it looking for
#     asound-bus-mode.conf inside aec/.
#   * the loop is ONE LINE. A backslash continuation is invisible to the
#     resolver, which would then call the variable unresolvable.
#   * the guard tests a FILE, not the directory: a directory is not a path
#     `git ls-tree` lists, so it cannot be checked against the archive.
if [ -f "$PAYLOAD/aec/Makefile" ]; then
    install -d -m 0755 /usr/local/src/wall-aec
    for _aec in wall-audio-aec.c wall_aec_policy.c wall_aec_policy.h wall_aec_profile.c wall_aec_profile.h wall_aec_pcm.h wall_aec_status.c wall_aec_status.h Makefile; do
        [ -f "$PAYLOAD/aec/$_aec" ] && install -m 0644 "$PAYLOAD/aec/$_aec" "/usr/local/src/wall-aec/$_aec"
    done
    if ( cd /usr/local/src/wall-aec && make >/tmp/wall-aec-build.log 2>&1 ); then
        install -m 0755 /usr/local/src/wall-aec/wall-audio-aec /usr/local/sbin/wall-audio-aec
        log "audio: echo canceller built and installed to /usr/local/sbin/wall-audio-aec"
    else
        warn "audio: the echo canceller did NOT build (see /tmp/wall-aec-build.log). The microphone still works; the room's own music will not be removed from it."
    fi
    [ -f "$PAYLOAD/wall-audio-aec.service" ] &&
        install -m 0644 "$PAYLOAD/wall-audio-aec.service" /etc/systemd/system/wall-audio-aec.service
    [ -f "$PAYLOAD/wall-audio-aec-restore.service" ] &&
        install -m 0644 "$PAYLOAD/wall-audio-aec-restore.service" /etc/systemd/system/wall-audio-aec-restore.service
fi

# THE MIC SEAM IS MOVED ONLY WHEN THE OWNER ASKS. `mic_selected` resolves
# through WALL_AUDIO_MIC_SOURCE, which wall-audio-output publishes from the
# switch position; with WALL_AUDIO_AEC=1 the applier publishes `mic_clean`
# instead in Speaker, and the mic legs then read the cancelled microphone
# without a single change to their own units.
#
# DEFAULT OFF, AND THE DEFAULT IS THE RULING. The canceller's acceptance needs
# a person in the room -- the double-talk test and the real amplifier-knob move
# -- so until that has been done the seam stays where it was and the daemon is
# installed, buildable and startable without any consumer depending on it.
# ONE PERSISTED SOURCE FOR BOTH HALVES. The unit's enablement below and the
# applier's choice of `mic_clean` must never be able to disagree: an enabled
# daemon whose mic seam was never moved would sit there cancelling a microphone
# no leg reads, and the reverse would point the legs at a PCM nothing feeds.
# Re-asserted on every boot from wall.env, like the audio mode itself.
cat > /etc/wall-panel/audio-aec.env <<EOF
# GENERATED by wall-firstboot.sh. Edit wall.env and re-run, not this file.
WALL_AUDIO_AEC=${WALL_AUDIO_AEC:-0}
EOF
chmod 0644 /etc/wall-panel/audio-aec.env

# The mic units carry static After/PartOf ordering and a start gate which only
# requires AEC while audio-aec.env says the seam is moved.  Older firstboot
# revisions expressed that policy as generated drop-ins; remove them during
# migration so the installed unit behaviour remains entirely represented by
# the release's hashed unit files.
_aec_dropins="/etc/systemd/system/wall-mic-rear.service.d /etc/systemd/system/wall-bt-call.service.d"
for _dir in $_aec_dropins; do
    rm -f "$_dir/10-aec.conf"
    rmdir "$_dir" 2>/dev/null || true
done

if [ "${WALL_AUDIO_AEC:-0}" = "1" ]; then
    if [ -x /usr/local/sbin/wall-audio-aec ]; then
        # `enable_unit SUCCESS_LINE UNIT...` — the helper SHIFTS the first
        # argument away, so calling it with the unit name alone ran
        # `systemctl enable` with no arguments, enabled nothing, and logged the
        # unit name as if it had worked.
        enable_unit "audio: echo canceller ENABLED (WALL_AUDIO_AEC=1); mic_selected will resolve to mic_clean in Speaker" \
            wall-audio-aec.service
    else
        warn "audio: WALL_AUDIO_AEC=1 but the canceller is not installed. The mic seam is NOT moved; the raw microphone is still what the legs read."
    fi
else
    # Installed and not enabled is the intended resting state before
    # acceptance. Said out loud so nobody reads the absence of the unit in
    # `systemctl list-units` as a deployment that went wrong.
    systemctl disable wall-audio-aec.service >/dev/null 2>&1 || true
    log "audio: echo canceller installed but not enabled (WALL_AUDIO_AEC is not 1)"
fi

systemctl daemon-reload >/dev/null 2>&1 || true

# The trigger daemon's on/off and actuator knobs are rendered into its own env file rather
# than read from wall.env: that file is root-only and carries unrelated
# credentials, and this service has no business seeing it.
if [ -f /etc/wall-panel/amp-trigger.env ]; then
    _amp_enabled=true
    case "${WALL_AMP_TRIGGER_ENABLED:-true}" in false|FALSE|no|0) _amp_enabled=false ;; esac
    if grep -q '^WALL_AMP_ENABLED=' /etc/wall-panel/amp-trigger.env 2>/dev/null; then
        sed -i "s/^WALL_AMP_ENABLED=.*/WALL_AMP_ENABLED=$_amp_enabled/" /etc/wall-panel/amp-trigger.env
    else
        printf 'WALL_AMP_ENABLED=%s\n' "$_amp_enabled" >> /etc/wall-panel/amp-trigger.env
    fi

    _amp_activator=${WALL_AMP_ACTIVATOR:-lcus-2}
    case "$_amp_activator" in
        lcus-2) ;;
        audio-jack)
            fail_step "audio: WALL_AMP_ACTIVATOR=audio-jack was RETIRED 2026-09-13 and its code is gone. The built-in jack is an ordinary audio output now, held for the headset leg; nothing here will ever drive a tone into it again. wall-amp-trigger will exit 78 and the amplifier will never switch on. Set WALL_AMP_ACTIVATOR=lcus-2 in wall.env and re-run."
            _amp_activator=invalid ;;
        *)
            fail_step "audio: WALL_AMP_ACTIVATOR must be exactly lcus-2 (got '$_amp_activator'). wall-amp-trigger will exit 78 and the amplifier will never switch on. Set WALL_AMP_ACTIVATOR=lcus-2 in wall.env and re-run."
            _amp_activator=invalid ;;
    esac
    if grep -q '^WALL_AMP_ACTIVATOR=' /etc/wall-panel/amp-trigger.env 2>/dev/null; then
        sed -i "s|^WALL_AMP_ACTIVATOR=.*|WALL_AMP_ACTIVATOR=$_amp_activator|" /etc/wall-panel/amp-trigger.env
    else
        printf 'WALL_AMP_ACTIVATOR=%s\n' "$_amp_activator" >> /etc/wall-panel/amp-trigger.env
    fi

    _amp_lcus2_device=${WALL_AMP_LCUS2_DEVICE:-/dev/wall-amp-relay}
    if [ -n "$_amp_lcus2_device" ] &&
       ! printf '%s' "$_amp_lcus2_device" | grep -Eq '^/dev/[A-Za-z0-9._/-]+$'; then
        warn "audio: WALL_AMP_LCUS2_DEVICE must be a plain absolute /dev path; refusing the supplied value"
        _amp_lcus2_device=
    fi
    if grep -q '^WALL_AMP_LCUS2_DEVICE=' /etc/wall-panel/amp-trigger.env 2>/dev/null; then
        sed -i "s|^WALL_AMP_LCUS2_DEVICE=.*|WALL_AMP_LCUS2_DEVICE=$_amp_lcus2_device|" /etc/wall-panel/amp-trigger.env
    else
        printf 'WALL_AMP_LCUS2_DEVICE=%s\n' "$_amp_lcus2_device" >> /etc/wall-panel/amp-trigger.env
    fi

    _amp_lcus2_channel=${WALL_AMP_LCUS2_CHANNEL:-1}
    case "$_amp_lcus2_channel" in
        1|2) ;;
        *)
            warn "audio: WALL_AMP_LCUS2_CHANNEL must be 1 or 2; the amplifier service will refuse to start"
            _amp_lcus2_channel=invalid ;;
    esac
    if grep -q '^WALL_AMP_LCUS2_CHANNEL=' /etc/wall-panel/amp-trigger.env 2>/dev/null; then
        sed -i "s|^WALL_AMP_LCUS2_CHANNEL=.*|WALL_AMP_LCUS2_CHANNEL=$_amp_lcus2_channel|" /etc/wall-panel/amp-trigger.env
    else
        printf 'WALL_AMP_LCUS2_CHANNEL=%s\n' "$_amp_lcus2_channel" >> /etc/wall-panel/amp-trigger.env
    fi
    if [ "$_amp_activator" = lcus-2 ] && [ -z "$_amp_lcus2_device" ]; then
        warn "audio: lcus-2 actuator selected but WALL_AMP_LCUS2_DEVICE is blank; the amplifier service will refuse to start"
    fi
    [ "$_amp_enabled" = false ] &&
        log "audio: WALL_AMP_TRIGGER_ENABLED is false — no actuator is commanded; the amplifier stays under whatever manual control it had"
fi

# Enablement expresses the line-in and rocker knobs, so that wall-audio-mode can
# switch chains later without quietly turning back on something that was
# deliberately left off.
case "${WALL_LINE_IN_ENABLED:-true}" in
    false|FALSE|no|0)
        systemctl disable --now wall-line-in.service >/dev/null 2>&1 || true
        log "audio: WALL_LINE_IN_ENABLED is false — the desktop feed is dropped; kiosk audio is unaffected" ;;
    *)
        enable_unit "wall-line-in.service enabled — the USB adapter's line input is passed through to the amplifier" wall-line-in.service ;;
esac

case "${WALL_VOLUME_KEYS_ENABLED:-true}" in
    false|FALSE|no|0)
        systemctl disable --now wall-volume-keys.service wall-volume-request.socket >/dev/null 2>&1 || true
        log "audio: WALL_VOLUME_KEYS_ENABLED is false — the side rocker is inert" ;;
    *)
        enable_unit "wall-volume-keys.service enabled — the side rocker drives whichever output the current mode uses" wall-volume-keys.service ;;
esac

# apply_audio_mode MODE SUCCESS_LINE... — apply one output chain and JUDGE it.
#
# A FAILED APPLY IS A RED FIRSTBOOT, NOT A WARNING. This ran as a bare `warn`
# until 2026-09-14 and then printed the affirmative "bus mode" line anyway, so
# a half-applied switch -- mode file and symlink moved, legs not moved, both
# chains on one adapter -- reached the wall behind a GREEN provisioning marker.
# That is the exact failure the bus arm was added to prevent (measured
# 2026-09-14 00:06 as audible distortion), so it cannot be reported as success.
# fail_step does not abort the boot: the kiosk still comes up, the unit goes
# red, and the marker is withheld. THAT IS WHY THIS HELPER ALWAYS RETURNS 0.
# The script runs under `set -euo pipefail` and every arm calls this as a bare
# simple command, so a `return 1` here would kill firstboot on the spot --
# skipping media sync, the door and audio brokers, Bluetooth, the touch filter
# and the final red summary itself -- which is the opposite of recording a
# failure and carrying on.
#
# An absent or non-executable applier is the same failure wearing a different
# hat, and used to be SILENT: the arm's `if [ -x ]` simply fell through while
# the trigger arm logged its success line from outside the guard.
apply_audio_mode() {
    local mode="$1"; shift
    if [ ! -x /usr/local/sbin/wall-audio-mode ]; then
        fail_step "audio: /usr/local/sbin/wall-audio-mode is missing or not executable, so WALL_AUDIO_MODE=$mode was NOT applied. Whatever chain the panel came up in is what it is running, which may not be the one wall.env names. Check $PAYLOAD/wall-audio-mode reached the payload."
        return 0
    fi
    if ! /usr/local/sbin/wall-audio-mode "$mode" >/dev/null 2>&1; then
        fail_step "audio: \`wall-audio-mode $mode\` FAILED. The switch may be HALF applied (mode file and symlink moved, legs not), which can leave two chains driving one adapter. Re-run by hand and read its output: sudo /usr/local/sbin/wall-audio-mode $mode"
        return 0
    fi
    local line
    for line in "$@"; do log "$line"; done
    return 0
}

case "${WALL_AUDIO_MODE:-trigger}" in
    bus|BUS)
        # The merged bus (D-3). Before this arm existed, `bus` fell through to
        # the trigger arm below, so every boot re-asserted trigger over a live
        # bus graph and half-applied it: mode file and symlink said trigger
        # while the bus legs kept running, and both chains drove the adapter
        # at once (measured 2026-09-14 00:06 as audible distortion).
        apply_audio_mode bus             "audio: bus mode — one merged stereo bus, the Mute/Headset/Speaker switch, amp detector on the speaker tap." ;;
    panel|PANEL)
        apply_audio_mode panel             "audio: panel mode — everything out the panel's own speaker. Note this"             "audio: path measured 31 dB noisier than the adapter; it is a fallback." ;;
    *)
        apply_audio_mode trigger             "audio: trigger mode — audio out the USB adapter, amplifier commanded over the LCUS-2 relay."             "audio: the built-in headphone jack carries nothing; the trigger tone was retired 2026-09-13." ;;
esac

# ── camera hardware availability; capture remains sensor-policy owned ───────
# Older images created exactly this blacklist. Remove only that product-owned
# file, never unrelated administrator policy. Loading a driver discovers the
# device but does not open it or illuminate its capture LED.
CAM_BLACKLIST=/etc/modprobe.d/wall-camera-off.conf
CAM_POLICY_BACKUP=/etc/wall-panel/wall-camera-off.pre-local-capabilities.conf
CAM_POLICY_ABSENT=/etc/wall-panel/wall-camera-off.pre-local-capabilities.absent
if [ -e "$CAM_POLICY_BACKUP" ] && [ -e "$CAM_POLICY_ABSENT" ]; then
    fail_step "camera: prior driver-policy record is inconsistent; preserving current policy"
elif [ ! -e "$CAM_POLICY_BACKUP" ] && [ ! -e "$CAM_POLICY_ABSENT" ]; then
    if [ -L "$CAM_BLACKLIST" ]; then
        fail_step "camera: legacy policy path is a symlink; refusing to read or remove it"
    elif [ -f "$CAM_BLACKLIST" ]; then
        install -o root -g root -m 0600 "$CAM_BLACKLIST" "$CAM_POLICY_BACKUP"
        log "camera: privately recorded the prior product policy for exact rollback"
    elif [ -e "$CAM_BLACKLIST" ]; then
        fail_step "camera: legacy policy path is not a regular file; preserving it"
    else
        install -o root -g root -m 0600 /dev/null "$CAM_POLICY_ABSENT"
        log "camera: privately recorded that no prior product policy existed"
    fi
fi
if [ -f "$CAM_BLACKLIST" ] && [ ! -L "$CAM_BLACKLIST" ] \
        && grep -Eq '^[[:space:]]*blacklist[[:space:]]+uvcvideo([[:space:]]|$)' "$CAM_BLACKLIST"; then
    rm -f "$CAM_BLACKLIST"
    log "camera: removed legacy product-owned uvcvideo blacklist"
fi
modprobe uvcvideo >/dev/null 2>&1 || true
if [ -e "${WALL_CAMERA_DEVICE:-/dev/video0}" ]; then
    log "camera: hardware available at ${WALL_CAMERA_DEVICE:-/dev/video0}; capture remains off until saved local opt-in"
else
    warn "camera: ${WALL_CAMERA_DEVICE:-/dev/video0} is absent; PIN, Bluetooth and non-camera functions remain available"
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

# ── 8b-2. IS THE HEVC DECODE PROFILE STILL THERE TO BE USED? ─────────────────
# THE FRAME LIBRARY IS ENTIRELY H.265 (29 files, 43 GB), and the shell plays it
# only through this GPU's hardware decoder. The two halves of that are owned in
# different repos on purpose: OfficeWallNaglight's `packaging/wall-shell` passes
# the Chromium switch that ADMITS the codec, and this image provides the driver
# that DECODES it (va-driver-all + vainfo, both in wall/packages.list).
#
# So this is the image's half of the contract, and it is a PROOF rather than an
# assumption — the same rule quirk 6 already applies to VA-API in
# WALL-BURN-IN.md §6. Skylake GT2 exposes VAProfileHEVCMain:VAEntrypointVLD
# through Intel iHD; a distro upgrade that changed the driver, dropped the
# firmware or renamed the profile would take the frame video away silently,
# because the only symptom on the wall is a black rectangle where a photo
# should be. A loud line here is cheaper than that.
#
# NON-FATAL. A panel with no HEVC decoder is degraded, not broken: NagLight, the
# checklist, music, the door and every other surface are unaffected.
if command -v vainfo >/dev/null 2>&1; then
    VA_PROFILES="$(vainfo 2>/dev/null || true)"
    if printf '%s' "$VA_PROFILES" | grep -q 'VAProfileHEVCMain[[:space:]]*:[[:space:]]*VAEntrypointVLD'; then
        log "video: VAProfileHEVCMain:VAEntrypointVLD is exposed — the frame library can be decoded in hardware"
        log "video:   driver = $(printf '%s' "$VA_PROFILES" | sed -n 's/.*Driver version: //p' | head -1)"
    else
        warn "video: THIS GPU NO LONGER EXPOSES VAProfileHEVCMain:VAEntrypointVLD."
        warn "The frame videos are all H.265 and the shell admits H.265 only when a"
        warn "hardware decoder is present, so the FRAME surface will be a black"
        warn "rectangle while everything else on the panel keeps working."
        warn "Check: vainfo   (expect 'Intel iHD driver'; va-driver-all provides it)"
        warn "A driver that stopped loading usually means a renamed firmware blob or a"
        warn "kernel/mesa upgrade, not a broken GPU."
    fi
else
    warn "video: vainfo is not installed, so the HEVC decode profile was NOT verified."
    warn "It is declared in wall/packages.list; an image missing it was built from a"
    warn "stale package set. The frame video may or may not play — this boot cannot say."
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
    # reset-failed must happen before the runtime mask: systemd rejects a reset
    # request for a unit after that unit has been masked. The unit is static, so
    # there is no enablement-driven resurrection window before the mask follows.
    if ! timeout 5 systemctl reset-failed wall-door-stream.service >/dev/null 2>&1; then
        # `reset-failed UNIT` reports "Unit ... not loaded" for a newly copied,
        # static unit which has never run.  That is the normal first-install
        # state: it has neither a process nor failed state to clear.  Do not
        # confuse it with a failed reset of a live/failed broker.
        _door_reset_state="$(timeout 5 systemctl show --property=ActiveState --property=Result --value wall-door-stream.service 2>/dev/null || true)"
        if printf '%s\n' "$_door_reset_state" | grep -Fxq inactive && \
           printf '%s\n' "$_door_reset_state" | grep -Fxq success; then
            log "Door broker has no failed state to reset (first-install static unit)"
        else
            fail_step "Door broker failed-state reset could not be confirmed; retaining runtime state"
            return 1
        fi
    fi
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
# The shipped backend moves the panel's OWN output switch, microphone button and
# level, through the root applier, and routes no device: BlueZ and PipeWire
# ownership is still probe-gated, so every verb that names a device is refused
# and authorization for them stays deny-by-default (item 23 step 5, SR-023).
: "${WALL_AUDIO_ENABLED:=false}"
: "${WALL_AUDIO_SOCKET:=/run/wall-audio-router/service.sock}"
_wall_audio_complete=1
for _wall_audio_file in audio_router.py routing.py visualizer.py switch_request.py switch_backend.py; do
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

# The Bluetooth front door is configured independently of the broker above: the
# broker governs the panel reaching out, this governs whether anything out there
# can see it and ask to pair. Default is closed at rest with a self-closing
# pairing window; see render-bluetooth.py for why.
if ! WALL_ENV_FILE="$ENV_FILE" bash "$PAYLOAD/configure-bluetooth.sh"; then
    fail_step "Bluetooth adapter policy failed; inspect wall-bluetooth.service."
fi

# Touch fault filter is opt-in; OFF also restores the raw-input recovery path.
if ! WALL_ENV_FILE="$ENV_FILE" bash "$PAYLOAD/configure-touch-filter.sh"; then
    fail_step "Touch filter configuration failed; inspect wall-touch-filter.service."
fi

# ── 8e. Item S — bounded thermal/CPU/presentation-mode telemetry collector ──
# Read-only sensor sampling plus one small writable state dir under
# DynamicUser; never touches input, audio or the renderer. On by default
# (WALL_TELEMETRY_ENABLED, default true) because it is strictly read-only
# evidence-gathering, unlike the audio broker which changes what the panel
# does; set to false to disable entirely.
: "${WALL_TELEMETRY_ENABLED:=true}"
if [ "$WALL_TELEMETRY_ENABLED" != true ] && [ "$WALL_TELEMETRY_ENABLED" != false ]; then
    fail_step "WALL_TELEMETRY_ENABLED must be exactly true or false"
    WALL_TELEMETRY_ENABLED=false
fi
# THE UNIT IS INSTALLED HERE, and it was not installed anywhere at all before
# this line (integration review, 2026-09-14). The audio unit loop further up
# installs only the audio units, so /etc/systemd/system/wall-panel-telemetry.service
# never existed, the completeness check below always failed, and -- because
# WALL_TELEMETRY_ENABLED defaults to true -- EVERY firstboot run took the
# fail_step branch and refused to stamp the provisioning marker. The collector
# is item S's only producer, so this is also the whole of item S not running.
#
# The unit's ExecStart names the payload tree (/opt/wall-panel/stack/...), so
# the two .py files are NOT copied anywhere: they are read from where they
# already are. That is why only the unit is installed here.
[ -f "$PAYLOAD/wall-panel-telemetry.service" ] &&
    install -m 0644 "$PAYLOAD/wall-panel-telemetry.service" /etc/systemd/system/wall-panel-telemetry.service

# ── the one directory the RENDERER writes and the collector reads ───────────
# The presentation snapshot crosses from the Electron host (running as the
# kiosk user `panel`) to the DynamicUser collector through a file. It used to
# be named inside /run/wall-panel, which wall-audio-output creates as root:root
# 0755 -- so the renderer could not create the file there at all, and item S
# would have logged "unknown" for the presentation mode forever while every
# unit looked healthy. Widening /run/wall-panel was rejected: the AEC's
# input-mute env file and the applier's epoch marker live in it, and a renderer
# that can write those can forge them.
#
# A tmpfiles rule rather than an `install -d` here, because /run is a tmpfs and
# the kiosk session can start before this script does; tmpfiles runs in early
# boot, so the directory is there whichever order the rest comes up in.
cat > /etc/tmpfiles.d/wall-panel-renderer.conf <<'EOF'
# GENERATED by wall-firstboot.sh. The kiosk renderer's own run directory: it is
# the ONLY /run path the unprivileged renderer may write, and it holds exactly
# one file, the item S presentation snapshot. World-readable so the telemetry
# collector's DynamicUser can read it without being given a group.
d /run/wall-panel-renderer 0755 panel panel -
EOF
chmod 0644 /etc/tmpfiles.d/wall-panel-renderer.conf
systemd-tmpfiles --create /etc/tmpfiles.d/wall-panel-renderer.conf >/dev/null 2>&1 ||
    warn "SN-S: could not create /run/wall-panel-renderer now; it will exist from the next boot, and until then the presentation mode logs as unknown"

systemctl daemon-reload >/dev/null 2>&1 || true

if [ ! -f "$PAYLOAD/panel-telemetry.py" ] || [ ! -f "$PAYLOAD/panel_telemetry_core.py" ] \
        || [ ! -f /etc/systemd/system/wall-panel-telemetry.service ]; then
    if [ "$WALL_TELEMETRY_ENABLED" = true ]; then
        fail_step "Panel telemetry payload is incomplete: missing panel-telemetry.py, panel_telemetry_core.py or wall-panel-telemetry.service"
    fi
    WALL_TELEMETRY_ENABLED=false
fi
if [ "$WALL_TELEMETRY_ENABLED" = true ]; then
    if enable_unit_now "SN-S: panel telemetry collector enabled (5s presentation poll, 5s full sample)" wall-panel-telemetry.service; then
        :
    fi
else
    systemctl disable --now wall-panel-telemetry.service >/dev/null 2>&1 || true
    log "SN-S: panel telemetry collector disabled"
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
