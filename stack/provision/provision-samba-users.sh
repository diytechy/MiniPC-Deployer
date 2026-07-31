#!/usr/bin/env bash
# Samba household accounts, zero-touch (Personal open-items A13, 2026-07-30).
#
# Creates one Unix account per storage-map §2 identity and sets its Samba
# password from the materialized credentials file, so the per-share ACLs in
# smb.conf actually mean something. Before this, bring-up meant running
# `useradd` + `sudo smbpasswd -a <id>` by hand for every household member and
# typing each password from Show-DeploySecret.ps1.
#
# WHY EACH ACCOUNT HAS ITS OWN PASSWORD (the thing this protects):
# storage-map §3 gives PetersDocs/FellasDocs/FellaPeterShared to `Zafella` and
# each *Docs tree to its owner. Until 2026-07-30 the tooling told you to give
# all four identities the SAME password, which made those ACLs decorative —
# anyone holding it could authenticate as any identity, including `admin`
# (implicit full read/write everywhere). Peter's ruling: one password each.
#
# INPUT  /etc/homehub-samba/samba-users.creds  — `account:password` per line,
#        emitted by Materialize-Deploy.ps1, root-only (0600). Colon-delimited
#        and NOT shell-sourced on purpose: these passwords are human-chosen and
#        may contain $ ` " and spaces, which `. file` would execute or mangle.
#
# Accounts are created with NO Unix login (`-M` no home, nologin shell): they
# exist so Samba has something to map to, and must never become SSH surface.
# The box is SSH-key-only (WI-10.12) and these accounts get no key.
#
# IDEMPOTENT: an existing user is not recreated; its Samba password is re-set
# to the file's value every run, so this is also the "I changed a password on
# the dev PC" apply path — re-run it after shipping a new .env/creds file.
#
# Exit codes: 0 = all accounts provisioned (or nothing to do);
#             nonzero = samba missing, creds unreadable, or a user failed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREDS="/etc/homehub-samba/samba-users.creds"
while [ $# -gt 0 ]; do
    case "$1" in
        --creds) CREDS="$2"; shift 2 ;;
        *) echo "usage: provision-samba-users.sh [--creds PATH]" >&2; exit 2 ;;
    esac
done
log() { echo "[provision-samba-users] $*"; }

if [ ! -f "$CREDS" ]; then
    log "no credentials file at $CREDS — skipping."
    log "  (Materialize-Deploy.ps1 emits out/homehub/samba-users.creds; it is carried"
    log "   on the USB payload and installed here. Without it there are no"
    log "   household accounts and every §3 private share is unreachable.)"
    exit 0
fi

# smbpasswd comes from samba-common-bin; pdbedit/smbd from samba. If Samba is
# not installed there is nothing to add users TO — fail loudly rather than
# create bare Unix accounts that look like success.
if ! command -v smbpasswd >/dev/null 2>&1; then
    log "FATAL: smbpasswd not found — the Samba server lane is not installed on this box."
    log "  Install it (apt-get install -y samba) and deploy an smb.conf built from"
    log "  storage-map §3 before re-running. See Personal open-items A14."
    exit 1
fi

# Root-only: the file holds every household password in plaintext.
perms="$(stat -c '%a' "$CREDS")"
if [ "$perms" != "600" ] && [ "$perms" != "400" ]; then
    log "WARN: $CREDS is mode $perms — tightening to 0600 (it holds plaintext passwords)"
    chmod 0600 "$CREDS"
fi

# ── the household group (A15) ────────────────────────────────────────────────
# The library is NTFS, and ntfs3 synthesizes ownership from the MOUNT options
# rather than storing it per file. Every file on that volume therefore reports
# gid=HOUSEHOLD_GID, and a household account can only WRITE if it is in that
# group. This is not the access-control boundary — Samba's per-share
# `valid users` is, and it gates the connection before any file is touched.
# This group only makes the write mechanically possible.
#
# GID is fixed and numeric to match the generated fstab: ntfs3's options are
# parsed in the kernel, which cannot resolve a group name.
HOUSEHOLD_GROUP="household"
HOUSEHOLD_GID=3000
if getent group "$HOUSEHOLD_GID" >/dev/null 2>&1; then
    existing="$(getent group "$HOUSEHOLD_GID" | cut -d: -f1)"
    if [ "$existing" != "$HOUSEHOLD_GROUP" ]; then
        log "FATAL: gid $HOUSEHOLD_GID is already group '$existing', not '$HOUSEHOLD_GROUP'."
        log "  The generated fstab pins gid=$HOUSEHOLD_GID for the library mount, so this"
        log "  collision would silently give '$existing' write access to the whole library."
        exit 1
    fi
    log "group $HOUSEHOLD_GROUP (gid $HOUSEHOLD_GID) exists"
elif groupadd -g "$HOUSEHOLD_GID" "$HOUSEHOLD_GROUP"; then
    log "group $HOUSEHOLD_GROUP created (gid $HOUSEHOLD_GID)"
else
    log "FATAL: could not create group $HOUSEHOLD_GROUP — household accounts would be unable to write to the NTFS library."
    exit 1
fi

rc=0
created=0; updated=0
while IFS= read -r line || [ -n "$line" ]; do
    # Skip blanks and comments.
    case "$line" in ''|\#*) continue ;; esac
    account="${line%%:*}"
    password="${line#*:}"
    if [ -z "$account" ] || [ "$account" = "$line" ]; then
        log "WARN: malformed line (expected account:password) — skipped"
        rc=1; continue
    fi

    if id -u "$account" >/dev/null 2>&1; then
        log "user '$account' exists"
    else
        # -M no home dir, nologin shell: Samba-only identity, never SSH surface.
        if useradd -M -s /usr/sbin/nologin "$account"; then
            log "user '$account' created (no home, nologin)"
            created=$((created + 1))
        else
            log "ERROR: useradd failed for '$account'"; rc=1; continue
        fi
    fi

    # Membership in the household group is what lets this account WRITE to the
    # NTFS library (see the group block above). Idempotent.
    if id -nG "$account" 2>/dev/null | tr ' ' '\n' | grep -qxF "$HOUSEHOLD_GROUP"; then
        :
    elif usermod -aG "$HOUSEHOLD_GROUP" "$account"; then
        log "  '$account' added to $HOUSEHOLD_GROUP"
    else
        log "  ERROR: could not add '$account' to $HOUSEHOLD_GROUP — its writes to the library will fail"
        rc=1
    fi

    # -s reads two newline-separated copies from stdin; the password never
    # appears in argv (visible in /proc) or in this script's logs.
    if printf '%s\n%s\n' "$password" "$password" | smbpasswd -s -a "$account" >/dev/null 2>&1; then
        log "samba password set for '$account'"
        updated=$((updated + 1))
    else
        log "ERROR: smbpasswd failed for '$account'"; rc=1; continue
    fi

    # An account with no password set is disabled by smbpasswd -a; make sure it
    # is enabled (a previously disabled account stays disabled otherwise).
    smbpasswd -e "$account" >/dev/null 2>&1 || true
done < "$CREDS"

log "done: $created account(s) created, $updated password(s) set."
if [ "$rc" -ne 0 ]; then
    log "FATAL: at least one account failed — the shares those identities own are unreachable."
fi
exit "$rc"
