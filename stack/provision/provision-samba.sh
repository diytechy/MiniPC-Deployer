#!/usr/bin/env bash
# Samba server bring-up (Personal open-items A14, 2026-07-30).
#
# Assembles /etc/samba/smb.conf from two pieces and starts the daemon:
#   [global]  stack/samba/smb.conf.global   — tracked, T0, LAN-only enforcement
#   [shares]  smb.conf.fragment             — GENERATED from storage-map §3 by
#                                             Generate-FromStorageMap.ps1
# Keeping them separate is the point: share definitions have exactly one
# source of truth (the storage map), and this repo never restates a path.
#
# WHY THIS EXISTS: until 2026-07-30 the map defined eleven shares, the
# generator faithfully emitted stanzas for them, and NOTHING SERVED ANY OF IT —
# no samba package, no smb.conf, no daemon. The §2 identities and their
# per-account passwords (A13) were specified but had no server to exist in.
#
# ORDER MATTERS: run this BEFORE provision-samba-users.sh. smbpasswd needs the
# Samba databases, which only exist once the package is configured.
#
# IDEMPOTENT: rewrites the config from source every run and reloads. Safe to
# re-run after regenerating the fragment.
#
# Exit codes: 0 = serving (or deliberately skipped); nonzero = samba missing,
#             fragment missing, config invalid, or the daemon refused to start.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
GLOBAL_CONF="$STACK_DIR/samba/smb.conf.global"
# The generated fragment is site data: it arrives on the USB payload at
# site/smb.conf.fragment and is installed here by the autoinstall late-command.
FRAGMENT="/etc/awow-samba/smb.conf.fragment"
LIBRARY_ROOT="/srv/library"

while [ $# -gt 0 ]; do
    case "$1" in
        --fragment) FRAGMENT="$2"; shift 2 ;;
        --global)   GLOBAL_CONF="$2"; shift 2 ;;
        --library)  LIBRARY_ROOT="$2"; shift 2 ;;
        *) echo "usage: provision-samba.sh [--fragment PATH] [--global PATH] [--library PATH]" >&2; exit 2 ;;
    esac
done
log() { echo "[provision-samba] $*"; }

if ! command -v smbd >/dev/null 2>&1; then
    log "FATAL: smbd not installed. The autoinstall package list should carry 'samba'."
    exit 1
fi
[ -f "$GLOBAL_CONF" ] || { log "FATAL: missing $GLOBAL_CONF (tracked file — is the payload complete?)"; exit 1; }

if [ ! -f "$FRAGMENT" ]; then
    log "FATAL: no share fragment at $FRAGMENT."
    log "  It is GENERATED on the dev PC by Generate-FromStorageMap.ps1 and carried"
    log "  on the USB payload as site/smb.conf.fragment. Without it this box would"
    log "  serve a [global] section and zero shares — refusing to start a pointless"
    log "  daemon rather than look healthy while serving nothing."
    exit 1
fi

# ── the library must be mounted before we export paths inside it ─────────────
# Exporting a path that is NOT the real mount is the dangerous failure: Samba
# would happily serve the empty mountpoint on the eMMC, clients would see an
# empty share, and a write would land on the system disk instead of the 4 TB
# drive. Refuse instead.
if ! mountpoint -q "$LIBRARY_ROOT"; then
    log "FATAL: $LIBRARY_ROOT is not a mountpoint — the library drive is not mounted."
    log "  Serving shares from an unmounted path would export an empty directory on"
    log "  the system disk and silently accept writes there. Fix the mount first"
    log "  (site/library-mounts.fstab -> /etc/fstab, then 'systemctl daemon-reload"
    log "  && mount -a'), then re-run."
    exit 1
fi

# ── assemble ─────────────────────────────────────────────────────────────────
install -d -m 0755 /etc/samba
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
{
    echo "# ASSEMBLED by provision-samba.sh — DO NOT EDIT."
    echo "#   [global] from $GLOBAL_CONF (tracked)"
    echo "#   shares   from $FRAGMENT (generated from storage-map.md §3)"
    echo "# Edit the storage map and re-generate; hand edits are lost on re-run."
    echo
    cat "$GLOBAL_CONF"
    echo
    cat "$FRAGMENT"
} > "$tmp"

# ── validate BEFORE installing: a broken smb.conf takes the daemon down ──────
if ! testparm -s "$tmp" >/dev/null 2>&1; then
    log "FATAL: assembled smb.conf failed testparm. Not installing it. Diagnosis:"
    testparm -s "$tmp" 2>&1 | sed 's/^/    /' || true
    exit 1
fi

install -m 0644 "$tmp" /etc/samba/smb.conf
log "installed /etc/samba/smb.conf (global + $(grep -c '^\[' "$FRAGMENT") share stanza(s))"

# ── share directories must exist, or Samba reports them as unavailable ───────
# Paths come from the fragment itself, so this cannot drift from the map.
while IFS= read -r p; do
    [ -n "$p" ] || continue
    if [ ! -d "$p" ]; then
        log "creating missing share path $p"
        install -d -m 0775 "$p"
    fi
done < <(awk -F'=' '/^[[:space:]]*path[[:space:]]*=/ { gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2); print $2 }' "$FRAGMENT")

systemctl enable smbd >/dev/null 2>&1 || true
if systemctl is-active --quiet smbd; then
    systemctl reload smbd || systemctl restart smbd
    log "smbd reloaded"
else
    systemctl start smbd
    log "smbd started"
fi

# nmbd (NetBIOS name service) is optional; SMB2+ clients find the box by DNS or
# IP. Left disabled deliberately: it broadcasts, and Technitium owns naming.
systemctl disable --now nmbd >/dev/null 2>&1 || true

log "serving $(grep -c '^\[' "$FRAGMENT") share(s) from $LIBRARY_ROOT — LAN-only per the [global] hosts allow line."
exit 0
