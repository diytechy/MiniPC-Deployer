#!/usr/bin/env bash
# list-tracker-users.sh — read-only aid for the NAGLIGHT_USER / PANEL_USER_SUB
# knobs (Personal open-items A9e, 2026-07-30).
#
# Multi-user NagLight keys one data dir per user off the Google `sub` claim,
# directly under the tracker volume's root (NagLight web.DataRoot; each real
# user dir contains definitions/). The `sub` IS the dir name — this prints
# them so the owner can copy the right one into config.awow.psd1
# (NAGLIGHT_USER) and config.common.psd1 (PANEL_USER_SUB).
#
# DELIBERATELY NOT auto-discovery for PANEL_USER_SUB: that value is the
# unauthenticated identity the wall kiosk site injects (OFFICEWALL_BOOTSTRAP
# §6, security-critical). "Whoever signed in first" is not an identity
# assertion — a human matches sub -> person and pastes it. Ruled 2026-07-30.
#
# Run on the AWOW as root (the backup service's volume-mountpoint trick —
# no helper image needed). Read-only: nothing is written.
set -euo pipefail

# Resolve the tracker's /data mountpoint from the running container first
# (immune to compose project-name prefixes), falling back to the volume name.
mp="$(docker inspect -f \
    '{{ range .Mounts }}{{ if eq .Destination "/data" }}{{ .Source }}{{ end }}{{ end }}' \
    tracker 2>/dev/null || true)"
if [ -z "$mp" ]; then
    for vol in tracker_data stack_tracker_data; do
        mp="$(docker volume inspect -f '{{ .Mountpoint }}' "$vol" 2>/dev/null)" && break || true
    done
fi
[ -n "${mp:-}" ] || { echo "ERROR: cannot resolve the tracker data volume (is the stack up?)" >&2; exit 1; }

found=0
for dir in "$mp"/*/; do
    [ -d "$dir/definitions" ] || continue   # only real per-user dirs
    sub="$(basename "$dir")"
    last="$(date -r "$dir" '+%Y-%m-%d %H:%M' 2>/dev/null || echo '?')"
    printf '%s   (dir modified %s)\n' "$sub" "$last"
    found=$((found + 1))
done

if [ "$found" -eq 0 ]; then
    echo "No per-user dirs yet — nobody has signed in through oauth2-proxy."
    echo "(A dir appears on a user's first authenticated request, not at consent.)"
else
    echo
    echo "$found user(s). Match sub -> person before pasting: NAGLIGHT_USER +"
    echo "PANEL_USER_SUB take the OWNER's sub, and PANEL_USER_SUB is the identity"
    echo "the wall kiosk injects unauthenticated — never guess from arrival order."
fi
