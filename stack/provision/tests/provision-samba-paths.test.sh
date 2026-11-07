#!/usr/bin/env bash
# provision-samba.sh creates a missing share path only on the library (SR-148;
# TC-1697). Its loop over the fragment's paths is lifted out of the script and run
# here with a temp library: the rest of the script wants a real smbd and /etc.
#
# THE DEFECT THIS PINS. `mkdir -p` on every missing path was safe while each
# path sat on the library it had just proved mounted. A share on another drive is
# a different matter: with that drive away, the same line puts /srv/media/Clips on
# the system disk, under a mount point that then hides it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRIPT="$ROOT/stack/provision/provision-samba.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

mkdir -p "$TMP/library" "$TMP/backup-drive"
cat >"$TMP/fragment" <<EOF
[Media]
   path = $TMP/library/NonDocs/Media
[Backup]
   path = $TMP/backup-drive
[Clips]
   path = $TMP/media/Clips
EOF
LOOP="$(sed -n '/^while IFS= read -r p; do$/,/^done < <(awk/p' "$SCRIPT")"
[ -n "$LOOP" ] || fail 'the path loop was not found in provision-samba.sh'
LIBRARY_ROOT="$TMP/library" FRAGMENT="$TMP/fragment" bash -c "set -euo pipefail
log() { echo \"\$*\"; }
$LOOP" >"$TMP/out" 2>&1 || fail "the loop failed: $(cat "$TMP/out")"

[ -d "$TMP/library/NonDocs/Media" ] || fail 'a missing path on the library was not created'
[ ! -e "$TMP/media" ] || fail 'a share path off the library was created'
grep -q "share path $TMP/media/Clips does not exist and is not on the library" "$TMP/out" \
    || fail 'the missing path was not reported'
echo 'PASS: provision-samba creates share paths on the library only'
