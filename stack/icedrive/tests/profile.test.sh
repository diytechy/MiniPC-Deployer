#!/usr/bin/env bash
# profile.test.sh — the IceDrive profile capture/restore, exercised.
#
# HERMETIC. It builds the fixture under a TEMP home (ICEDRIVE_PROFILE_HOME) and
# points the script at the current user, so `getent passwd` resolves and there is
# no IceDrive process to quiesce. It never touches a real home directory. Needs
# bash, tar, gpg.
#
# WHY EVERY ONE OF THESE EXISTS. The design was reviewed adversarially before it
# was written (gpt-5.6-sol, 2026-08-29) and came back APPROVE WITH CHANGES. Each
# change it demanded is one assertion here, so a later "simplification" that
# drops one fails loudly instead of quietly re-opening the finding.
#
#   I1  a capture is ENCRYPTED — the archive is not readable without the key,
#       and the plaintext token does not appear anywhere in the bytes
#   I2  a capture with NO KEY is REFUSED, not silently written in the clear.
#       This is the one that matters most: a fail-open here defeats the whole
#       review, and it is the cheapest failure to write by accident
#   I3  the capture proves it decrypts before replacing the previous archive
#   I4  --verify decrypts, lists, and insists Icedrive.conf is present — an
#       archive that unpacks cleanly but has no config restores a box that
#       still cannot sign in
#   I5  a round trip is byte-exact
#   I6  restore DECLINES a non-empty profile (this is not a fresh install), and
#       --force overrides it
#   I7  a WRONG key fails closed and writes nothing
#   8   a TAMPERED archive is rejected — the cipher is authenticated
#   I9  path traversal / absolute paths / paths outside the profile are refused
#       BEFORE anything is unpacked
#   I11 a command line that merely CONTAINS "Icedrive" is not the client - the
#       capture must not TERM the ssh session that invoked it
#   I12 a SYMLINK inside the archive is refused - names alone are not a check,
#       and this extracts as root into a home directory
#   I13 a DESTINATION root that is a symlink is refused too
#   I14 an archive with no Icedrive.conf is refused by capture AND restore
#   I15 the last good archive survives a capture that fails late
#   I10 "no profile" and "no archive" exit 3, distinct from failure: a box with
#       IceDrive off must never look like a broken recovery
#
# Usage: bash profile.test.sh [--keep-tmp]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUT="$(cd "$HERE/.." && pwd)/icedrive-profile.sh"
[ -f "$SUT" ] || { echo "FATAL: $SUT not found"; exit 2; }
command -v gpg >/dev/null 2>&1 || { echo "FATAL: gpg is required"; exit 2; }
KEEP_TMP=0
[ "${1:-}" = "--keep-tmp" ] && KEEP_TMP=1

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }

TMP="$(mktemp -d)"
cleanup() { if [ "$KEEP_TMP" = 1 ]; then echo "tmp kept: $TMP"; return 0; fi; rm -rf "$TMP"; }
trap cleanup EXIT

ME="$(id -un)"
OUT="$TMP/out"; mkdir -p "$OUT"
ENVF="$TMP/env"
SECRET='icedrivet=THE-LONG-LIVED-API-TOKEN-abc123'
printf 'ICEDRIVE_PROFILE_KEY=correct-horse-battery-staple\nOTHER=x\n' >"$ENVF"
printf 'ICEDRIVE_PROFILE_KEY=a-different-key-entirely\n' >"$TMP/env.wrong"
printf 'OTHER=x\n' >"$TMP/env.nokey"

# AN ISOLATED HOME, NOT THE REAL ONE. The script resolves the profile's home from
# `getent passwd`, so the first version of this file built its fixture in the
# CURRENT USER'S actual home directory, stashing anything already there and
# restoring it at exit. That works right up until the test dies at the wrong
# moment — and it was run that way once, on the production hub, against a live
# IceDrive profile holding the household's cloud sign-in. It survived. Once was
# enough: the script now takes ICEDRIVE_PROFILE_HOME, and this test uses it.
REALHOME="$TMP/home"; mkdir -p "$REALHOME"
export ICEDRIVE_PROFILE_HOME="$REALHOME"

make_profile() {
    rm -rf "$REALHOME/.config/Icedrive" "$REALHOME/.local/share/Icedrive" "$REALHOME/.config/autostart"
    mkdir -p "$REALHOME/.config/Icedrive" "$REALHOME/.local/share/Icedrive/QtWebEngine" "$REALHOME/.config/autostart"
    printf 'icedrive_login=someone@example.invalid\n%s\n' "$SECRET" >"$REALHOME/.config/Icedrive/Icedrive.conf"
    head -c 2048 /dev/urandom >"$REALHOME/.local/share/Icedrive/tempdata.db"
    printf 'cookie-jar\n' >"$REALHOME/.local/share/Icedrive/QtWebEngine/Cookies"
    printf '[Desktop Entry]\nType=Application\nExec=/opt/icedrive/icedrive-gate.sh\n' >"$REALHOME/.config/autostart/icedrive.desktop"
}
clear_profile() { rm -rf "$REALHOME/.config/Icedrive" "$REALHOME/.local/share/Icedrive" "$REALHOME/.config/autostart"; }

sut() { bash "$SUT" --user "$ME" --out "$OUT" --env "$ENVF" "$@" >"$TMP/out.txt" 2>&1; echo $?; }
sut_env() { local e="$1"; shift; bash "$SUT" --user "$ME" --out "$OUT" --env "$e" "$@" >"$TMP/out.txt" 2>&1; echo $?; }

echo "== I2/I10: no key and no profile both fail SAFELY, and differently =="
make_profile
rc="$(sut_env "$TMP/env.nokey" --capture)"
if [ "$rc" = 1 ]; then pass "I2 a capture with no key is REFUSED (exit 1)"; else fail "I2 exit=$rc, want 1"; cat "$TMP/out.txt"; fi
if [ -z "$(ls -A "$OUT" 2>/dev/null)" ]; then pass "I2 and it wrote NOTHING — no plaintext fallback"; else fail "I2 the refused capture left files in $OUT: $(ls "$OUT")"; fi
if grep -q 'in the clear' "$TMP/out.txt"; then pass "I2 and it says why"; else fail "I2 the refusal does not explain itself"; fi
clear_profile
rc="$(sut --capture)"
if [ "$rc" = 3 ]; then pass "I10 no profile at all -> exit 3, not a failure"; else fail "I10 exit=$rc, want 3"; cat "$TMP/out.txt"; fi
rc="$(sut --restore)"
if [ "$rc" = 3 ]; then pass "I10 no archive to restore -> exit 3"; else fail "I10 restore exit=$rc, want 3"; fi

echo
echo "== I1/I3/I4: capture, and prove it is encrypted and readable =="
make_profile
rc="$(sut --capture)"
if [ "$rc" = 0 ]; then pass "I3 capture exits 0"; else fail "I3 capture exit=$rc"; cat "$TMP/out.txt"; fi
ARCH="$OUT/icedrive-profile.tar.gz.gpg"
if [ -f "$ARCH" ]; then pass "I1 the archive exists"; else fail "I1 no archive at $ARCH"; fi
if ! grep -aq 'THE-LONG-LIVED-API-TOKEN' "$ARCH" 2>/dev/null; then
    pass "I1 the API token does NOT appear in the archive bytes"
else
    fail "I1 the token is READABLE in the archive — this is the whole point of the encryption"
fi
if tar -tzf "$ARCH" >/dev/null 2>&1; then
    fail "I1 the archive is a plain readable tar — it was not encrypted at all"
else
    pass "I1 the archive is not a readable tar without the key"
fi
if [ -f "$OUT/capture.info" ] && grep -q '^captured_utc=' "$OUT/capture.info"; then
    pass "I3 a non-secret sidecar records when the capture last succeeded"
else
    fail "I3 no usable capture.info"
fi
if ! grep -q 'PROFILE_KEY\|correct-horse' "$OUT/capture.info" 2>/dev/null; then
    pass "I3 and the sidecar carries no key material"
else
    fail "I3 the sidecar leaks the key"
fi
rc="$(sut --verify)"
if [ "$rc" = 0 ]; then pass "I4 --verify decrypts, lists and finds Icedrive.conf"; else fail "I4 verify exit=$rc"; cat "$TMP/out.txt"; fi

echo
echo "== I7/I8: a wrong key and a tampered archive both fail closed =="
rc="$(sut_env "$TMP/env.wrong" --verify)"
if [ "$rc" = 1 ]; then pass "I7 the wrong key fails (exit 1)"; else fail "I7 exit=$rc, want 1"; cat "$TMP/out.txt"; fi
cp "$ARCH" "$TMP/arch.bak"
# Flip a byte in the middle of the ciphertext.
python3 - "$ARCH" <<'PY' 2>/dev/null || dd if=/dev/urandom of="$ARCH" bs=1 count=1 seek=200 conv=notrunc 2>/dev/null
import sys
p = sys.argv[1]
b = bytearray(open(p, "rb").read())
i = len(b) // 2
b[i] ^= 0xFF
open(p, "wb").write(bytes(b))
PY
rc="$(sut --verify)"
if [ "$rc" = 1 ]; then pass "I8 a tampered archive is rejected — the cipher is authenticated"; else fail "I8 exit=$rc on a flipped byte, want 1"; cat "$TMP/out.txt"; fi
cp "$TMP/arch.bak" "$ARCH"

echo
echo "== I5/I6: a byte-exact round trip, and the clean-profile rule =="
CONF_BEFORE="$(sha256sum "$REALHOME/.config/Icedrive/Icedrive.conf" | cut -d' ' -f1)"
DB_BEFORE="$(sha256sum "$REALHOME/.local/share/Icedrive/tempdata.db" | cut -d' ' -f1)"
rc="$(sut --restore)"
if [ "$rc" = 3 ]; then pass "I6 restore DECLINES a non-empty profile (exit 3)"; else fail "I6 exit=$rc over a populated profile, want 3"; cat "$TMP/out.txt"; fi
clear_profile
rc="$(sut --restore)"
if [ "$rc" = 0 ]; then pass "I5 restore onto a clean profile exits 0"; else fail "I5 restore exit=$rc"; cat "$TMP/out.txt"; fi
CONF_AFTER="$(sha256sum "$REALHOME/.config/Icedrive/Icedrive.conf" 2>/dev/null | cut -d' ' -f1)"
DB_AFTER="$(sha256sum "$REALHOME/.local/share/Icedrive/tempdata.db" 2>/dev/null | cut -d' ' -f1)"
if [ "$CONF_BEFORE" = "$CONF_AFTER" ] && [ "$DB_BEFORE" = "$DB_AFTER" ]; then
    pass "I5 Icedrive.conf and tempdata.db came back byte-identical"
else
    fail "I5 round trip is not byte-exact (conf $CONF_BEFORE -> $CONF_AFTER, db $DB_BEFORE -> $DB_AFTER)"
fi
if [ -f "$REALHOME/.config/autostart/icedrive.desktop" ]; then pass "I5 the autostart entry came back too"; else fail "I5 no autostart entry after restore"; fi
MODE="$(stat -c '%a' "$REALHOME/.config/Icedrive/Icedrive.conf" 2>/dev/null)"
if [ "$MODE" = 600 ]; then pass "I5 the restored config is 0600"; else fail "I5 restored config mode is $MODE, want 600"; fi
rc="$(sut --restore --force)"
if [ "$rc" = 0 ]; then pass "I6 --force restores over a populated profile"; else fail "I6 --force exit=$rc"; cat "$TMP/out.txt"; fi

echo
echo "== I9: an archive that reaches outside the profile is refused BEFORE unpacking =="
EVIL="$TMP/evil"; mkdir -p "$EVIL/.config/Icedrive"
printf 'ok\n' >"$EVIL/.config/Icedrive/Icedrive.conf"
mkdir -p "$EVIL/.ssh"; printf 'ssh-rsa AAAA attacker\n' >"$EVIL/.ssh/authorized_keys"
( cd "$EVIL" && tar -czf "$TMP/evil.tar.gz" .config/Icedrive .ssh )
printf 'correct-horse-battery-staple' | gpg --batch --yes --quiet --pinentry-mode loopback \
    --passphrase-fd 3 --symmetric --cipher-algo AES256 -o "$ARCH" "$TMP/evil.tar.gz" 3<&0 </dev/null
clear_profile
SSH_BEFORE="$(ls -A "$REALHOME/.ssh" 2>/dev/null | wc -l | tr -d ' ')"
rc="$(sut --restore)"
if [ "$rc" = 1 ]; then pass "I9 an archive holding .ssh/ is REFUSED (exit 1)"; else fail "I9 exit=$rc, want 1"; cat "$TMP/out.txt"; fi
if grep -q 'outside the profile' "$TMP/out.txt"; then pass "I9 and it names what it objected to"; else fail "I9 the refusal does not say why"; fi
SSH_AFTER="$(ls -A "$REALHOME/.ssh" 2>/dev/null | wc -l | tr -d ' ')"
if [ "$SSH_BEFORE" = "$SSH_AFTER" ]; then pass "I9 and nothing was written outside the profile"; else fail "I9 ~/.ssh CHANGED ($SSH_BEFORE -> $SSH_AFTER entries)"; fi
if [ ! -e "$REALHOME/.config/Icedrive/Icedrive.conf" ]; then pass "I9 and it refused before unpacking the legitimate half too"; else fail "I9 it unpacked part of the archive before refusing"; fi


echo
echo "== I11: a command line that merely CONTAINS 'Icedrive' is not the client =="
# THE BUG THIS LOCKS, measured on the real hub 2026-08-29. `pgrep -u hub -f
# Icedrive` matched the SSH COMMAND LINE that was running the capture, because
# that command line contained the word. The run reported "quiescing: TERM to
# IceDrive pid(s) 108144" on a box where the client was not running, then
# "client is back (after 1s)" on the same false match. Both lines were wrong;
# both read as success.
make_profile
# A DECOY WHOSE COMMAND LINE CONTAINS THE WORD. `exec -a` is a bashism dash does
# not have, so the name is carried by the SCRIPT PATH instead — which is exactly
# the shape of the real false positive: an `ssh … homehub-icedrive-profile`
# command line, matched because the word appeared in it.
printf '#!/bin/sh\nsleep 30\n' >"$TMP/watch-Icedrive-decoy.sh"
chmod +x "$TMP/watch-Icedrive-decoy.sh"
"$TMP/watch-Icedrive-decoy.sh" &
DECOY=$!
sleep 1
if pgrep -u "$ME" -f Icedrive | grep -q "^$DECOY$"; then
    pass "I11 setup: a naive 'pgrep -f Icedrive' really does match the decoy"
else
    fail "I11 setup: the decoy is not matched by a naive pgrep, so this proves nothing"
fi
rc="$(sut --capture)"
if [ "$rc" = 0 ]; then pass "I11 capture still exits 0 with a decoy 'Icedrive' process about"; else fail "I11 exit=$rc"; cat "$TMP/out.txt"; fi
if grep -q 'the client is not running' "$TMP/out.txt"; then
    pass "I11 and it correctly reported no client running"
else
    fail "I11 it thought the decoy was the client: $(grep -i 'quiesc\|not running' "$TMP/out.txt" | head -2)"
fi
if kill -0 "$DECOY" 2>/dev/null; then pass "I11 and the decoy was not signalled"; else fail "I11 the decoy process was killed"; fi
kill "$DECOY" 2>/dev/null || true

echo
echo "== I12: a SYMLINK inside the archive is refused — names alone are not a check =="
# THE FINDING THAT MATTERED MOST in the second review pass. The first version
# validated tar member NAMES and nothing else, so a member called
# `.config/Icedrive` that is a SYMLINK to /etc passed every name test — and this
# extracts as root into a home directory.
make_profile
rc="$(sut --capture)"
EVIL2="$TMP/evil2"; rm -rf "$EVIL2"; mkdir -p "$EVIL2/.config/Icedrive"
printf 'ok\n' >"$EVIL2/.config/Icedrive/Icedrive.conf"
ln -s /etc "$EVIL2/.config/Icedrive/escape"
( cd "$EVIL2" && tar -czf "$TMP/evil2.tar.gz" .config/Icedrive )
printf 'correct-horse-battery-staple' | gpg --batch --yes --quiet --pinentry-mode loopback \
    --passphrase-fd 3 --symmetric --cipher-algo AES256 -o "$ARCH" "$TMP/evil2.tar.gz" 3<&0 </dev/null
clear_profile
rc="$(sut --restore)"
if [ "$rc" = 1 ]; then pass "I12 an archive containing a symlink is refused (exit 1)"; else fail "I12 exit=$rc, want 1"; cat "$TMP/out.txt"; fi
if grep -q 'links or device nodes' "$TMP/out.txt"; then pass "I12 and it names what it found"; else fail "I12 message: $(tail -1 "$TMP/out.txt")"; fi
if [ ! -e "$REALHOME/.config/Icedrive/escape" ]; then pass "I12 and the link was not planted in the home directory"; else fail "I12 the symlink reached \$HOME"; fi

echo
echo "== I13: a DESTINATION that is a symlink is refused too =="
# The archive can be perfect and the target still wrong: a pre-existing
# `~/.config/Icedrive -> /etc` redirects the copy regardless.
make_profile
rc="$(sut --capture)"
clear_profile
mkdir -p "$REALHOME/.config"
ln -s /tmp "$REALHOME/.config/Icedrive"
rc="$(sut --restore)"
rm -f "$REALHOME/.config/Icedrive"
if [ "$rc" = 1 ]; then pass "I13 a symlinked destination root is refused (exit 1)"; else fail "I13 exit=$rc, want 1"; cat "$TMP/out.txt"; fi
if grep -q 'is a SYMLINK' "$TMP/out.txt"; then pass "I13 and it names the path"; else fail "I13 message: $(tail -1 "$TMP/out.txt")"; fi

echo
echo "== I14: an archive with no Icedrive.conf is refused by capture AND by restore =="
# An archive that unpacks cleanly and holds no credential restores a box that
# still cannot sign in. `--verify` always said so; capture and restore did not.
make_profile
rm -f "$REALHOME/.config/Icedrive/Icedrive.conf"
rc="$(sut --capture)"
if [ "$rc" = 1 ]; then pass "I14 capture refuses when there is no Icedrive.conf to capture (exit 1)"; else fail "I14 capture exit=$rc, want 1"; cat "$TMP/out.txt"; fi
NOCONF="$TMP/noconf"; rm -rf "$NOCONF"; mkdir -p "$NOCONF/.config/Icedrive"
printf 'x\n' >"$NOCONF/.config/Icedrive/something-else.txt"
( cd "$NOCONF" && tar -czf "$TMP/noconf.tar.gz" .config/Icedrive )
printf 'correct-horse-battery-staple' | gpg --batch --yes --quiet --pinentry-mode loopback \
    --passphrase-fd 3 --symmetric --cipher-algo AES256 -o "$ARCH" "$TMP/noconf.tar.gz" 3<&0 </dev/null
clear_profile
rc="$(sut --restore)"
if [ "$rc" = 1 ]; then pass "I14 restore refuses an archive with no Icedrive.conf (exit 1)"; else fail "I14 restore exit=$rc, want 1"; cat "$TMP/out.txt"; fi
if [ ! -e "$REALHOME/.config/Icedrive/something-else.txt" ]; then pass "I14 and nothing from it was written"; else fail "I14 it unpacked anyway"; fi

echo
echo "== I15: the last good archive survives a capture that fails =="
# `install` straight onto the final name can truncate the previous archive and
# then fail, leaving neither. The replacement is now: write beside, verify,
# rename.
make_profile
rc="$(sut --capture)"
GOOD_SHA="$(sha256sum "$ARCH" | cut -d' ' -f1)"
# A capture that will fail late: remove the required member so the pre-install
# check refuses after the archive has been built.
rm -f "$REALHOME/.config/Icedrive/Icedrive.conf"
rc="$(sut --capture)"
if [ "$rc" = 1 ]; then pass "I15 the failing capture exits 1"; else fail "I15 exit=$rc"; fi
if [ "$(sha256sum "$ARCH" | cut -d' ' -f1)" = "$GOOD_SHA" ]; then
    pass "I15 and the previous archive is byte-identical — it was never touched"
else
    fail "I15 the failed capture damaged the last good archive"
fi

# ── I16: a client that STARTS AND THEN DIES must fail the capture (C36) ──────
# THE DEFECT THIS EXISTS TO CATCH, and it is not hypothetical: on 2026-08-29 the
# restart brought the client up, it logged RemoteHostClosedError five seconds
# later and exited, and the old three-second liveness check asserted "still
# running" IN THE SAME SECOND and returned 0. The unit exited 0, the nightly
# timer would have too, and the household's offsite copy was down until
# verify-hub.sh noticed. A check that samples one instant cannot tell "started"
# from "started and about to die".
#
# HOW THE FIXTURE FAKES A CLIENT WITHOUT ONE. Three seams, all already in the
# script: ICEDRIVE_PROFILE_HOME for the profile, SESSION_PID to stand in for the
# desktop session (session_env_load then reads DISPLAY out of that pid's real
# /proc environ), and `exec -a` to give a harmless `sleep` an argv[0] matching
# APP_RE. Nothing is installed and /opt is never touched.
echo "== I16: a client that starts and then dies is a FAILED capture (C36) =="
make_profile

# A stand-in for the desktop session. It has to be DISCOVERABLE the way the real
# one is - session_pid() runs `pgrep -u USER -x xfce4-session` - and exporting
# SESSION_PID does not work, because the script initialises that variable itself
# and clobbers anything inherited. So give the fake the right process NAME:
# `pgrep -x` matches comm, which for a shell script would be "bash", hence a copy
# of a real binary rather than a script. Its environ then supplies the DISPLAY
# that session_env_load is actually looking for.
cp /bin/sleep "$TMP/xfce4-session"
env DISPLAY=:99 "$TMP/xfce4-session" 120 &
FAKE_SESSION=$!
sleep 1
if pgrep -u "$ME" -x xfce4-session >/dev/null 2>&1; then
    pass "I16 setup: a stand-in desktop session is discoverable with a DISPLAY"
else
    fail "I16 setup: no discoverable session - the capture will refuse to stop the client"
fi

cat >"$TMP/dying-client.sh" <<'EOS'
#!/bin/bash
exec -a "/opt/icedrive/Icedrive.AppImage" sleep 4
EOS
chmod +x "$TMP/dying-client.sh"
printf '[Desktop Entry]\nType=Application\nExec=%s\n' "$TMP/dying-client.sh" \
    >"$REALHOME/.config/autostart/icedrive.desktop"

setsid bash -c 'exec -a "/opt/icedrive/Icedrive.AppImage" sleep 120' &
sleep 1
if pgrep -u "$ME" -f '/opt/icedrive/Icedrive\.AppImage' >/dev/null 2>&1; then
    pass "I16 setup: a stand-in client is running and matches the app pattern"
else
    fail "I16 setup: no stand-in client — the restart path would not be exercised"
fi

rc="$(sut --capture)"
if [ "$rc" = 1 ]; then
    pass "I16 the capture FAILS when the restarted client dies (exit 1)"
else
    fail "I16 exit=$rc, want 1 — a dead client reported as success (this IS C36)"
    sed -n '1,40p' "$TMP/out.txt"
fi
if grep -qi 'EXITED' "$TMP/out.txt"; then
    pass "I16 and it says the client EXITED, not merely that something failed"
else
    fail "I16 the failure does not name what happened: $(tail -3 "$TMP/out.txt")"
fi
if [ -f "$OUT/icedrive-profile.tar.gz.gpg" ]; then
    pass "I16 and the archive was still written — the copy is good, the client is not"
else
    fail "I16 the archive is missing; a restart failure must not discard a good capture"
fi
pkill -u "$ME" -f '/opt/icedrive/Icedrive\.AppImage' 2>/dev/null || true
kill "$FAKE_SESSION" 2>/dev/null || true
if [ "$(ls "$OUT" | grep -c '^\.')" = 0 ]; then pass "I15 and no temporary file was left behind"; else fail "I15 leftovers: $(ls -A "$OUT" | tr '\n' ' ')"; fi
echo
echo "──────────────────────────────────────────────────────────────"
printf '%s PASS  %s FAIL\n' "$PASS" "$FAIL"
if [ "$FAIL" -eq 0 ]; then exit 0; fi
exit 1
