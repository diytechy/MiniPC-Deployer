#!/usr/bin/env bash
# The guard for a share on the media drive (SR-148, SR-149; TC-1696): `--check
# Clips --library /srv/media` as Samba's root preexec runs it, and `--reattach`
# as the health unit runs it. Nothing is mounted: fake mountinfo, sysfs, fstab,
# and systemctl/umount stubs that record what was asked of them.
# clips-media.root-test.sh does the same on a real disk that really leaves.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
GUARD="${GUARD_OVERRIDE:-$ROOT/stack/samba/library-guard.sh}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/sys"
printf 'LABEL=MediaDrive /srv/media ext4 nofail,noatime,prjquota,errors=remount-ro 0 2\n' >"$TMP/fstab"

cat >"$TMP/bin/mountpoint" <<'EOF'
#!/usr/bin/env bash
grep -q ' /srv/media ' "$MOUNTINFO_FILE"
EOF
cat >"$TMP/bin/umount" <<'EOF'
#!/usr/bin/env bash
printf 'umount %s\n' "$*" >>"$ACTIONS"
: >"$MOUNTINFO_FILE"
EOF
cat >"$TMP/bin/mount" <<'EOF'
#!/usr/bin/env bash
printf 'BARE mount %s\n' "$*" >>"$ACTIONS"
EOF
cat >"$TMP/bin/systemd-escape" <<'EOF'
#!/usr/bin/env bash
printf 'systemd-escape %s\n' "$*" >>"$ACTIONS"
printf 'srv-media.mount\n'
EOF
cat >"$TMP/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >>"$ACTIONS"
[ "${SYSTEMCTL_EXIT:-0}" = 0 ] || exit "$SYSTEMCTL_EXIT"
printf '40 25 8:33 / /srv/media rw,noatime - ext4 /dev/sdc1 rw,prjquota\n' >"$MOUNTINFO_FILE"
mkdir -p "$SYS_DEV_BLOCK_DIR/8:33"
EOF
cat >"$TMP/bin/smbclient" <<'EOF'
#!/usr/bin/env bash
printf 'smbclient %s\n' "$*" >>"$ACTIONS"
EOF
chmod +x "$TMP/bin/"*

fail() { echo "FAIL: $*" >&2; exit 1; }
run_guard() {
    PATH="$TMP/bin:$PATH" MOUNTINFO_FILE="$TMP/mountinfo" SYS_DEV_BLOCK_DIR="$TMP/sys" FSTAB_FILE="$TMP/fstab" \
        NO_REMOUNT_FILE="${NO_REMOUNT_FILE:-$TMP/no-remount-missing}" ENV_FILE="$TMP/env-missing" \
        IDENTITY_FILE="$TMP/identity-missing" ACTIONS="$TMP/actions" "$GUARD" "$@" >"$TMP/out" 2>&1
}
fresh() { : >"$TMP/actions"; : >"$TMP/mountinfo"; rm -rf "$TMP/sys/8:33"; mkdir -p "$TMP/sys/8:33"; }
mounted() { printf '40 25 8:33 / /srv/media %s,noatime - ext4 /dev/sdc1 %s,prjquota,errors=remount-ro\n' "${1:-rw}" "${1:-rw}" >"$TMP/mountinfo"; }
stale() { mounted; rm -rf "$TMP/sys/8:33"; }

# --check: the option after the positional share name must reach the parser
fresh; mounted
run_guard --check Clips --library /srv/media || fail 'a healthy /srv/media refused'
fresh
if run_guard --check Clips --library /srv/media; then fail 'an unmounted /srv/media served'; fi
grep -q "REFUSING share 'Clips'" "$TMP/out" || fail 'the refusal does not name the share'
fresh; printf '24 22 8:2 / /srv/library rw - ext4 /dev/sdd1 rw\n' >"$TMP/mountinfo"; mkdir -p "$TMP/sys/8:2"
if run_guard --check Clips --library /srv/media; then fail 'a healthy LIBRARY passed for the media share'; fi
fresh; stale
if run_guard --check Clips --library /srv/media; then fail 'a stale /srv/media served'; fi
grep -q 'STALE mount' "$TMP/out" || fail 'a stale mount is not called stale'
fresh; mounted ro
if run_guard --check Clips --library /srv/media; then fail 'a read-only /srv/media served'; fi
grep -q 'READ-ONLY' "$TMP/out" || fail 'the read-only refusal does not say so'
[ ! -s "$TMP/actions" ] || fail '--check acted on a mount'

# --reattach: through the unit, after a lazy unmount of a stale mount
fresh; stale
run_guard --reattach --library /srv/media --label media || fail "stale: --reattach did not recover: $(cat "$TMP/out")"
[ "$(cat "$TMP/actions")" = $'umount -l /srv/media\nsystemd-escape -p --suffix=mount /srv/media\nsystemctl start srv-media.mount' ] \
    || fail "stale: unexpected actions: $(cat "$TMP/actions")"
fresh
run_guard --reattach --library /srv/media --label media || fail 'absent: --reattach did not recover'
[ "$(cat "$TMP/actions")" = $'systemd-escape -p --suffix=mount /srv/media\nsystemctl start srv-media.mount' ] \
    || fail "absent: unexpected actions: $(cat "$TMP/actions")"
fresh; mounted
run_guard --reattach --library /srv/media --label media || fail 'healthy: --reattach failed'
[ ! -s "$TMP/actions" ] || fail 'healthy: --reattach touched a good mount'
fresh; export SYSTEMCTL_EXIT=1
if run_guard --reattach --library /srv/media --label media; then fail 'a failed start reported success'; fi
unset SYSTEMCTL_EXIT
fresh; : >"$TMP/no-remount"; stale; NO_REMOUNT_FILE="$TMP/no-remount"
if run_guard --reattach --library /srv/media --label media; then fail 'the inhibitor did not hold'; fi
[ ! -s "$TMP/actions" ] || fail 'the inhibitor let a mount through'
unset NO_REMOUNT_FILE
fresh; : >"$TMP/fstab.empty"; cp "$TMP/fstab" "$TMP/fstab.keep"; : >"$TMP/fstab"
if run_guard --reattach --library /srv/media --label media; then fail 'no fstab row reported success'; fi
[ ! -s "$TMP/actions" ] || fail 'it mounted with no fstab row: this never guesses a device'
cp "$TMP/fstab.keep" "$TMP/fstab"

# --reattach serves /srv/media and nothing else, whatever is asked of it
for target in /srv/library /mnt/backup-drive /mnt/backup-old /srv/media/ /srv/media/Clips /srv/media/../library /srv; do
    fresh
    printf '24 22 8:2 / %s rw - ext4 /dev/sdd1 rw\n' "$target" >"$TMP/mountinfo"   # stale: no 8:2 in sysfs
    printf 'LABEL=X %s ext4 nofail 0 2\n' "$target" >"$TMP/fstab"
    set +e
    run_guard --reattach --library "$target"; rc=$?
    set -e
    [ "$rc" = 2 ] || fail "--reattach on $target exited $rc, not 2"
    grep -q 'REFUSED --reattach' "$TMP/out" || fail "--reattach on $target did not say it refused"
    [ ! -s "$TMP/actions" ] || fail "--reattach on $target acted: $(cat "$TMP/actions")"
done
cp "$TMP/fstab.keep" "$TMP/fstab"
fresh
printf '24 22 8:2 / /srv/library rw - ext4 /dev/sdd1 rw\n' >"$TMP/mountinfo"
printf 'LABEL=Library /srv/library ext4 nofail 0 2\n' >"$TMP/fstab"
set +e
run_guard --reattach; rc=$?                                   # the default is the library
LIBRARY_ROOT=/srv/library run_guard --reattach; rc_env=$?     # and so is the environment's
set -e
[ "$rc" = 2 ] && [ "$rc_env" = 2 ] || fail "--reattach with no --library exited $rc and $rc_env, not 2"
[ ! -s "$TMP/actions" ] || fail "--reattach with no --library acted: $(cat "$TMP/actions")"
cp "$TMP/fstab.keep" "$TMP/fstab"

# --report stays the library's alone
fresh; mounted
set +e
run_guard --report --library /srv/media; rc=$?
set -e
[ "$rc" = 2 ] || fail "--report on /srv/media exited $rc, not the 2 of its refusal"
[ ! -s "$TMP/actions" ] || fail '--report on /srv/media acted'

echo 'PASS: library guard on the media drive: check, reattach through the unit, report refused'
