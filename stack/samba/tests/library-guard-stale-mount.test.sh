#!/usr/bin/env bash
# A live mountinfo entry can outlast its USB block device. Exercise the public
# preexec verdict and timer actions without mounting anything on the host.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
GUARD="${GUARD_OVERRIDE:-$ROOT/stack/samba/library-guard.sh}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/sys-dev-block"
printf '/dev/disk/by-label/Library /srv/library ntfs-3g defaults 0 0\n' >"$TMP/fstab"
printf 'FILE_SHARE_SAMBA_PROBE_SHARE=Shared\n' >"$TMP/env"

cat >"$TMP/bin/mountpoint" <<'EOF'
#!/usr/bin/env bash
grep -q ' /srv/library ' "$MOUNTINFO_FILE"
EOF
cat >"$TMP/bin/umount" <<'EOF'
#!/usr/bin/env bash
printf 'umount %s\n' "$*" >>"$ACTIONS"
: >"$MOUNTINFO_FILE"
EOF
cat >"$TMP/bin/mount" <<'EOF'
#!/usr/bin/env bash
printf 'mount %s\n' "$*" >>"$ACTIONS"
printf '24 22 8:2 / /srv/library rw,relatime - fuseblk /dev/sdd1 rw\n' >"$MOUNTINFO_FILE"
mkdir -p "$SYS_DEV_BLOCK_DIR/8:2"
EOF
cat >"$TMP/bin/smbclient" <<'EOF'
#!/usr/bin/env bash
exit "${SMBCLIENT_EXIT:-0}"
EOF
chmod +x "$TMP/bin/"*

fail() { echo "FAIL: $*" >&2; exit 1; }
setup_case() {
    : >"$TMP/actions"
    : >"$TMP/mountinfo"
    rm -rf "$TMP/sys-dev-block/8:2"
    mkdir -p "$TMP/sys-dev-block/8:2"
    : >"$TMP/no-remount-absent"
}
run_guard() {
    PATH="$TMP/bin:$PATH" LIBRARY_ROOT=/srv/library MOUNTINFO_FILE="$TMP/mountinfo" \
        SYS_DEV_BLOCK_DIR="$TMP/sys-dev-block" FSTAB_FILE="$TMP/fstab" \
        NO_REMOUNT_FILE="${NO_REMOUNT_FILE:-$TMP/no-remount-missing}" \
        ENV_FILE="$TMP/env" IDENTITY_FILE="${IDENTITY_FILE_OVERRIDE:-$TMP/identity-missing}" \
        SMBCLIENT_BIN="$TMP/bin/smbclient" ACTIONS="$TMP/actions" \
        "$GUARD" "$@" >/dev/null 2>&1
}
mounted() {
    printf '24 22 8:2 / /srv/library rw,relatime - fuseblk /dev/sdd1 rw\n' >"$TMP/mountinfo"
}
stale() { mounted; rm -rf "$TMP/sys-dev-block/8:2"; }

healthy_check() {
    setup_case; mounted
    run_guard --check PetersDocs || fail 'healthy mount rejected by --check'
    [ ! -s "$TMP/actions" ] || fail 'healthy --check changed the mount'
    # A present kernel node with no matching by-id name is still an unknown
    # identity, not evidence that the mounted device vanished.
    printf '/srv/library\texpected-drive\tLibrary\n' >"$TMP/identity"
    IDENTITY_FILE_OVERRIDE="$TMP/identity"
    run_guard --check PetersDocs || fail 'present but unnamed device rejected as stale'
    unset IDENTITY_FILE_OVERRIDE
}
absent_check() {
    setup_case
    if run_guard --check PetersDocs; then fail 'absent mount accepted by --check'; fi
}
stale_check() {
    setup_case; stale
    if run_guard --check PetersDocs; then fail 'stale mount accepted by --check'; fi
}
stale_report() {
    setup_case; stale
    run_guard --report || fail 'stale report did not recover after reattachment'
    [ "$(cat "$TMP/actions")" = $'umount -l /srv/library\nmount /srv/library' ] || \
        fail 'stale report did not lazy-unmount before mounting the fstab path'
}
inhibited_report() {
    setup_case; stale
    NO_REMOUNT_FILE="$TMP/no-remount-absent"
    run_guard --report && fail 'maintenance inhibitor allowed stale report to succeed'
    [ ! -s "$TMP/actions" ] || fail 'maintenance inhibitor allowed unmount or mount'
    unset NO_REMOUNT_FILE
}
healthy_report() {
    setup_case; mounted
    run_guard --report || fail 'healthy report failed'
    [ ! -s "$TMP/actions" ] || fail 'healthy mount was unmounted or remounted'
    export SMBCLIENT_EXIT=1
    run_guard --report && fail 'failed Samba probe was ignored'
    [ ! -s "$TMP/actions" ] || fail 'failed Samba probe detached a healthy mount'
    unset SMBCLIENT_EXIT
}

if [ -n "${TEST_CASE:-}" ]; then
    "$TEST_CASE"
else
    healthy_check
    absent_check
    stale_check
    stale_report
    inhibited_report
    healthy_report
fi

echo 'PASS: library guard stale mount, recovery, inhibitor, and healthy controls'
