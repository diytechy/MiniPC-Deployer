#!/bin/bash
# kids-firstboot.sh — first-boot (and re-runnable) configuration of the KIDS
# KIOSK. Implements: SR-138, LLR-1415, LLR-1416.
#
# Reads /etc/kids-kiosk/kids.env (root, 0600) and renders what the unprivileged
# session needs, then hands tty1 to the kiosk user:
#
#   1. /etc/kids-kiosk/kiosk.env       0644, the session's half of kids.env
#   2. OtterTube's config.json         only when absent (a parent's later
#                                      changes are never overwritten)
#   3. Wi-Fi power saving off          a udev rule; iwlwifi's default costs latency
#   4. journald capped                 a 128 GB disk with nothing precious on it
#   5. tty1 autologin + profile hook   the session itself (kids-kiosk.sh)
#   6. OtterTube's shared libraries    ldd, so a missing one is named in the journal
#
# A step that fails is reported and the others still run; the marker
# /var/lib/kids-kiosk/firstboot.done is written only when every step passed.
# Idempotent: re-running it after editing kids.env is the supported way to
# change the kiosk's configuration.
set -u

ENV_FILE=${KIDS_ENV_FILE:-/etc/kids-kiosk/kids.env}
ETC=${KIDS_ETC:-/etc/kids-kiosk}
ROOT=${KIDS_ROOT:-}                 # tests point this at a scratch tree
KIOSK_USER=${KIDS_KIOSK_USER:-kiosk}
MARKER="$ROOT/var/lib/kids-kiosk/firstboot.done"
failed=0

log()  { echo "[kids-firstboot] $*"; }
fail() { log "FAILED: $*"; failed=1; }

# What the running session and journald read. This unit runs on EVERY boot, and
# restarting getty@tty1 kills a kiosk session that is already up (and, before
# the session learned to end with sway, left its children spinning: the
# independent review, 2026-11-01). So each is restarted only when its files
# actually changed.
SESSION_FILES="$ROOT$ETC/kiosk.env $ROOT/etc/systemd/system/getty@tty1.service.d/kids-autologin.conf $ROOT/etc/profile.d/zz-kids-kiosk.sh"
JOURNAL_FILES="$ROOT/etc/systemd/journald.conf.d/90-kids-kiosk.conf"
# shellcheck disable=SC2086  # word-split on purpose: a list of paths without spaces
snapshot() { cat $1 2>/dev/null | sha256sum; }
session_before=$(snapshot "$SESSION_FILES")
journal_before=$(snapshot "$JOURNAL_FILES")

# ── 0. read kids.env (shell-sourced, like the panel's wall.env) ───────────────
KIDS_TOUCH_OUTPUT='' KIDS_APP_CMD=/opt/kids-kiosk/app/ottertube-desktop KIDS_OTTERTUBE_CATALOG=''
if [ -r "$ENV_FILE" ]; then
    # shellcheck source=/dev/null
    . "$ENV_FILE"
else
    fail "$ENV_FILE is missing or unreadable - using the defaults"
fi

# Each value is checked against what its consumer can take. An invalid one is
# refused and replaced by its default, so the kiosk still comes up.
valid_touch_output() { case "$1" in *[!A-Za-z0-9\ ._-]*) return 1 ;; esac; return 0; }
valid_app_cmd()      { case "$1" in /*) case "$1" in *[!A-Za-z0-9/._-]*) return 1 ;; esac; return 0 ;; esac; return 1; }
valid_catalog()      { [ -z "$1" ] && return 0; case "$1" in https://*) case "$1" in *[[:space:]\"\\]*) return 1 ;; esac; return 0 ;; esac; return 1; }

if ! valid_touch_output "$KIDS_TOUCH_OUTPUT"; then
    fail "KIDS_TOUCH_OUTPUT='$KIDS_TOUCH_OUTPUT' has characters a sway output name cannot - using the first output by name"
    KIDS_TOUCH_OUTPUT=''
fi
if ! valid_app_cmd "$KIDS_APP_CMD"; then
    fail "KIDS_APP_CMD='$KIDS_APP_CMD' is not an absolute path of plain characters - using the image's OtterTube"
    KIDS_APP_CMD=/opt/kids-kiosk/app/ottertube-desktop
fi
if ! valid_catalog "$KIDS_OTTERTUBE_CATALOG"; then
    fail "KIDS_OTTERTUBE_CATALOG='$KIDS_OTTERTUBE_CATALOG' is not an https URL (OtterTube refuses http) - leaving the catalogue unset"
    KIDS_OTTERTUBE_CATALOG=''
fi

# ── 1. kiosk.env: what the unprivileged session may read ─────────────────────
render_kiosk_env() {
    install -d -m 0755 "$ROOT$ETC" || return 1
    local tmp; tmp=$(mktemp) || return 1
    {
        echo "# Rendered by kids-firstboot.sh from $ENV_FILE. Edit that file, not this one."
        printf 'KIDS_TOUCH_OUTPUT=%q\n' "$KIDS_TOUCH_OUTPUT"
        printf 'KIDS_APP_CMD=%q\n' "$KIDS_APP_CMD"
    } > "$tmp" && install -m 0644 "$tmp" "$ROOT$ETC/kiosk.env"
    local rc=$?; rm -f "$tmp"; return $rc
}
render_kiosk_env && log "kiosk.env rendered (touch output: ${KIDS_TOUCH_OUTPUT:-first by name})" \
    || fail "could not write $ROOT$ETC/kiosk.env"

# ── 2. OtterTube's config.json, only when absent ─────────────────────────────
seed_ottertube_config() {
    local home dir cfg
    home=$(getent passwd "$KIOSK_USER" | cut -d: -f6)
    [ -n "$home" ] || { fail "no home directory for $KIOSK_USER"; return; }
    dir="$ROOT$home/.config/ottertube"; cfg="$dir/config.json"
    if [ -e "$cfg" ]; then log "OtterTube config.json exists - left as it is"; return; fi
    if [ -z "$KIDS_OTTERTUBE_CATALOG" ]; then
        log "no KIDS_OTTERTUBE_CATALOG - OtterTube creates its own defaults (built-in catalogue only)"
        return
    fi
    install -d -m 0700 -o "$KIOSK_USER" -g "$KIOSK_USER" "$ROOT$home/.config" "$dir" \
        && jq -n --arg c "$KIDS_OTTERTUBE_CATALOG" '{catalog: $c}' > "$cfg" \
        && chown "$KIOSK_USER:$KIOSK_USER" "$cfg" && chmod 0600 "$cfg" \
        && log "OtterTube config.json seeded with the catalogue $KIDS_OTTERTUBE_CATALOG" \
        || fail "could not seed $cfg"
}
seed_ottertube_config

# ── 3. Wi-Fi power saving off ─────────────────────────────────────────────────
install -d -m 0755 "$ROOT/etc/udev/rules.d" && printf '%s\n' \
    '# Rendered by kids-firstboot.sh: iwlwifi power saving delays every packet.' \
    'ACTION=="add", SUBSYSTEM=="net", KERNEL=="wl*", RUN+="/usr/sbin/iw dev $name set power_save off"' \
    > "$ROOT/etc/udev/rules.d/81-kids-wifi-powersave.rules" \
    && log "Wi-Fi power saving: off from the next interface add (udev rule)" \
    || fail "could not write the Wi-Fi power-save rule"

# ── 4. journald capped ────────────────────────────────────────────────────────
install -d -m 0755 "$ROOT/etc/systemd/journald.conf.d" \
    && printf '%s\n' '[Journal]' 'SystemMaxUse=200M' > "$ROOT/etc/systemd/journald.conf.d/90-kids-kiosk.conf" \
    && log "journal capped at 200M" \
    || fail "could not cap the journal"

# ── 5. tty1 autologin and the profile hook ───────────────────────────────────
install -d -m 0755 "$ROOT/etc/systemd/system/getty@tty1.service.d" \
    && printf '%s\n' \
        '# Rendered by kids-firstboot.sh: tty1 is the kids kiosk session.' \
        '[Service]' \
        'ExecStart=' \
        "ExecStart=-/sbin/agetty --autologin $KIOSK_USER --noclear %I \$TERM" \
        > "$ROOT/etc/systemd/system/getty@tty1.service.d/kids-autologin.conf" \
    && install -d -m 0755 "$ROOT/etc/profile.d" \
    && printf '%s\n' \
        '# Rendered by kids-firstboot.sh: the kiosk user on tty1 runs the kids session.' \
        "if [ \"\$(id -un)\" = \"$KIOSK_USER\" ] && [ \"\$(tty)\" = /dev/tty1 ] && [ -z \"\${WAYLAND_DISPLAY:-}\" ]; then" \
        '    exec /usr/local/bin/kids-kiosk.sh' \
        'fi' > "$ROOT/etc/profile.d/zz-kids-kiosk.sh" \
    && log "tty1 autologins $KIOSK_USER into the kiosk session" \
    || fail "could not write the autologin"

# ── 6. OtterTube's shared libraries ──────────────────────────────────────────
if [ -x "$ROOT$KIDS_APP_CMD" ]; then
    missing=$(ldd "$ROOT$KIDS_APP_CMD" 2>&1 | grep 'not found' || true)
    if [ -n "$missing" ]; then
        fail "OtterTube cannot load, shared libraries missing:"; echo "$missing"
    else
        log "OtterTube's shared libraries all resolve"
    fi
else
    fail "$KIDS_APP_CMD is not executable - the session will show NOT INSTALLED"
fi

# ── apply, unless this is a test against a scratch tree ──────────────────────
session_changed=no; [ "$(snapshot "$SESSION_FILES")" = "$session_before" ] || session_changed=yes
journal_changed=no; [ "$(snapshot "$JOURNAL_FILES")" = "$journal_before" ] || journal_changed=yes
log "session config changed: $session_changed; journald config changed: $journal_changed"
if [ -z "$ROOT" ]; then
    systemctl daemon-reload
    udevadm control --reload 2>/dev/null || true
    [ "$journal_changed" = yes ] && { systemctl restart systemd-journald 2>/dev/null || true; }
    # --no-block: firstboot is itself a unit, and getty@tty1 is ordered after
    # the boot transaction this oneshot is part of.
    [ "$session_changed" = yes ] && systemctl restart --no-block getty@tty1.service
fi

if [ "$failed" -eq 0 ]; then
    install -d -m 0755 "$(dirname "$MARKER")" && date -Is > "$MARKER"
    log "done; marker written"
else
    rm -f "$MARKER"
    log "finished WITH FAILURES (above); marker withheld"
    exit 1
fi
