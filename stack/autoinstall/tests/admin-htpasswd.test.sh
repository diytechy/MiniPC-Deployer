#!/usr/bin/env bash
# admin-htpasswd.test.sh — firstboot.sh step 2b, the admin sign-in file.
#
# RUNS THE REAL STEP, NOT A COPY. Steps 2 and 2b are cut out of firstboot.sh by
# their headings and executed under the script's own `set -euo pipefail`, with
# STACK_DIR pointed at a temp tree, so an edit to firstboot is what is tested.
#
# WHAT IT LOCKS
#   H1  a placeholder hash writes nothing, but leaves a FILE (docker would
#       otherwise create a directory at the bind source)
#   H2  the hash as Materialize writes it - every `$` doubled - is written with
#       single `$`s (written raw, every sign-in 401s like a wrong password)
#   H3  a hand-written single-`$` hash is written unchanged
#   H4  written IN PLACE (same inode) and not rewritten when unchanged
#   H5  quoted .env values; a user change rewrites the same inode
#   H6  a user an htpasswd line cannot carry, and a non-bcrypt hash, are refused
#       and the existing file kept
#   H7  the empty directory docker leaves behind is replaced by the file
#   H8  the hash is never printed
#   H9  mode 0640 (SKIPPED on a filesystem that ignores chmod, e.g. DrvFS)
#   H10 errexit never fires across steps 2 + 2b
#
# Usage: bash admin-htpasswd.test.sh [--keep-tmp]
#
# `A && pass || fail` is safe because pass() cannot fail (SC2015); single-quoted
# `$` is a literal .env value or awk program (SC2016); cleanup() runs from the
# EXIT trap (SC2329).
# shellcheck disable=SC2015,SC2016,SC2329
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FB="$(cd "$HERE/.." && pwd)/firstboot.sh"
[ -f "$FB" ] || { echo "FATAL: $FB not found"; exit 2; }
KEEP_TMP=0
[ "${1:-}" = "--keep-tmp" ] && KEEP_TMP=1

PASS=0; FAIL=0; SKIP=0
pass() { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }
skip() { SKIP=$((SKIP+1)); printf 'SKIP  %s\n' "$*"; }

TMP="$(mktemp -d)"
cleanup() { if [ "$KEEP_TMP" = 1 ]; then echo "tmp kept: $TMP"; return 0; fi; rm -rf "$TMP"; }
trap cleanup EXIT

S="$TMP/stack"; mkdir -p "$S/oauth2-proxy"
awk '/^# ── 2\. oauth2-proxy allow-list/ { on = 1 } /^# ── 3\. load the baked image payload/ { on = 0 } on' "$FB" >"$TMP/steps.sh"
grep -q 'HTPASSWD_FILE=' "$TMP/steps.sh" || { echo "FATAL: could not cut step 2b out of firstboot.sh - its headings changed"; exit 2; }
cat >"$TMP/wrap.sh" <<WRAP
set -euo pipefail
STACK_DIR="$S"; cd "\$STACK_DIR"
log() { echo "[firstboot] \$*"; }
. "$TMP/steps.sh"
echo "[firstboot] harness: reached the end of step 2b"
WRAP
run() { bash "$TMP/wrap.sh" 2>&1; }

F="$S/admin-auth/htpasswd"
# Shape only: 53 characters of the bcrypt alphabet after the cost. Not a hash
# of anything.
SALT='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0'
HASH="\$2b\$12\$$SALT"
DOUBLED="${HASH//\$/\$\$}"
env_is() { printf '%s\n' "$@" >"$S/.env"; }

# ── H1 ──
env_is 'ADMIN_AUTH_USER=admin' 'ADMIN_AUTH_HASH=REPLACE_WITH_caddy_hash-password_OUTPUT'
out="$(run)"
[ -f "$F" ] && [ ! -s "$F" ] && pass "H1 placeholder: an empty FILE, not a directory" || fail "H1 placeholder left: $(ls -la "$S/admin-auth" 2>&1)"
echo "$out" | grep -q 'ADMIN_AUTH_HASH is empty or a placeholder' && pass "H1 ...and a warning" || fail "H1 output: $out"

# ── H2 ──
env_is 'ADMIN_AUTH_USER=admin' "ADMIN_AUTH_HASH=$DOUBLED"
ino="$(stat -c %i "$F")"
run >/dev/null
[ "$(cat "$F")" = "admin:$HASH" ] && pass "H2 a \$\$-doubled hash (as Materialize writes it) is written with single \$" \
    || fail "H2 content: $(cat "$F")"
grep -q '\$\$' "$F" && fail "H2 a doubled \$ reached the file" || pass "H2 no doubled \$ reached the file"

# ── H3 ──
env_is 'ADMIN_AUTH_USER=admin' "ADMIN_AUTH_HASH=\$2a\$14\$$SALT"
run >/dev/null
[ "$(cat "$F")" = "admin:\$2a\$14\$$SALT" ] && pass "H3 a single-\$ hash is written unchanged" || fail "H3 content: $(cat "$F")"

# ── H4 ──
[ "$(stat -c %i "$F")" = "$ino" ] && pass "H4 written in place (same inode as the placeholder file)" || fail "H4 inode changed"
before="$(stat -c %Y "$F")"; sleep 1
out="$(run)"
echo "$out" | grep -q 'admin sign-in file unchanged' && [ "$(stat -c %Y "$F")" = "$before" ] \
    && pass "H4 an unchanged value is not rewritten" || fail "H4 rewritten: $out"

# ── H5 ──
env_is 'ADMIN_AUTH_USER="ops"' "ADMIN_AUTH_HASH=\"$DOUBLED\""
run >/dev/null
[ "$(cat "$F")" = "ops:$HASH" ] && [ "$(stat -c %i "$F")" = "$ino" ] \
    && pass "H5 quoted values and a new user: rewritten, same inode" || fail "H5 content: $(cat "$F")"

# ── H6 ──
for bad in 'ADMIN_AUTH_USER=a:b' 'ADMIN_AUTH_USER=' 'ADMIN_AUTH_USER=two words'; do
    env_is "$bad" "ADMIN_AUTH_HASH=$DOUBLED"
    out="$(run)"
    echo "$out" | grep -q 'ADMIN_AUTH_USER is empty or has characters' && [ "$(cat "$F")" = "ops:$HASH" ] \
        && pass "H6 '$bad' refused, file kept" || fail "H6 '$bad': $out / $(cat "$F")"
done
for bad in '{SHA}abc=' "\$2b\$12\$short" "\$\$\$2b\$12\$$SALT"; do
    env_is 'ADMIN_AUTH_USER=admin' "ADMIN_AUTH_HASH=$bad"
    out="$(run)"
    echo "$out" | grep -q 'not a bcrypt hash' && [ "$(cat "$F")" = "ops:$HASH" ] \
        && pass "H6 hash '$bad' refused, file kept" || fail "H6 hash '$bad': $out"
done

# ── H7 ──
rm -f "$F"; mkdir "$F"
env_is 'ADMIN_AUTH_USER=admin' "ADMIN_AUTH_HASH=$DOUBLED"
run >/dev/null
[ -f "$F" ] && [ "$(cat "$F")" = "admin:$HASH" ] && pass "H7 docker's empty-directory artefact is replaced by the file" || fail "H7: $(ls -la "$S/admin-auth")"

# ── H8 ──
rm -f "$F"; env_is 'ADMIN_AUTH_USER=zed' "ADMIN_AUTH_HASH=$DOUBLED"
out="$(run)"
echo "$out" | grep -qF "$SALT" && fail "H8 the hash appears in firstboot's output" || pass "H8 the hash is never printed"

# ── H9 ──
probe="$TMP/probe"; : >"$probe"; chmod 0640 "$probe" 2>/dev/null
if [ "$(stat -c %a "$probe")" = 640 ]; then
    [ "$(stat -c %a "$F")" = 640 ] && pass "H9 mode 0640" || fail "H9 mode $(stat -c %a "$F")"
else
    skip "H9 this filesystem ignores chmod - mode not checkable here"
fi

# ── H10 ──
echo "$out" | grep -q 'reached the end of step 2b' && pass "H10 errexit never fired across steps 2 + 2b" || fail "H10 died early: $out"

echo
echo "──────────────────────────────────────────────────────────────"
printf '%s PASS  %s FAIL  (%s skipped)\n' "$PASS" "$FAIL" "$SKIP"
if [ "$FAIL" -eq 0 ]; then exit 0; fi
exit 1
