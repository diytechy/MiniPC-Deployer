#!/usr/bin/env bash
# reap-orphan-sesman.test.sh — the reaper's two shapes, against a FAKE process table.
#
# HERMETIC. systemctl, pgrep, ps, kill and sleep are stubs on PATH that read a
# fixture of "pid ppid etimes comm" lines; nothing real is signalled.
#
#   R1 a healthy session (Xorg among its descendants) is left alone
#   R2 a HEADLESS session (per-session sesman, no Xorg, old) is reaped, whole tree
#   R3 a headless session younger than the grace is left alone (X still starting)
#   R4 an ORPHAN (PPID 1, not MainPID) is still reaped, as before
#   R5 sesman not active -> nothing is reaped
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SUT="$HERE/../reap-orphan-sesman.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }

BIN="$TMP/bin"; mkdir -p "$BIN"
cat >"$BIN/systemctl" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  is-active) echo "${FAKE_STATE:-active}" ;;
  show) echo "${FAKE_MAIN:-100}" ;;
esac
EOF
cat >"$BIN/pgrep" <<'EOF'
#!/usr/bin/env bash
P=""; X=""
while [ $# -gt 0 ]; do
  case "$1" in -P) P="$2"; shift 2 ;; -x) X="$2"; shift 2 ;; *) X="$1"; shift ;; esac
done
awk -v P="$P" -v X="$X" '(P=="" || $2==P) && (X=="" || $4==X) { print $1; n++ } END { exit n?0:1 }' "$FAKE_PS"
EOF
cat >"$BIN/ps" <<'EOF'
#!/usr/bin/env bash
F=""; PID=""
while [ $# -gt 0 ]; do
  case "$1" in -o) F="${2%=}"; shift 2 ;; -p) PID="$2"; shift 2 ;; *) shift ;; esac
done
col=4; case "$F" in ppid) col=2 ;; etimes) col=3 ;; comm) col=4 ;; esac
awk -v p="$PID" -v c="$col" '$1==p { print $c }' "$FAKE_PS"
EOF
cat >"$BIN/kill" <<'EOF'
#!/usr/bin/env bash
shift; echo "$*" >>"$FAKE_KILLS"
EOF
printf '#!/bin/sh\nexit 0\n' >"$BIN/sleep"
chmod +x "$BIN"/*

# `kill` is a bash builtin, so the stub needs the builtin out of the way.
run() {
    : >"$TMP/kills"
    FAKE_PS="$TMP/ps" FAKE_KILLS="$TMP/kills" PATH="$BIN:$PATH" \
        bash -c 'enable -n kill; . "$0"' "$SUT" >"$TMP/out.txt" 2>&1
}
killed() { tr ' ' '\n' <"$TMP/kills" | grep -qx "$1"; }

echo "== R1: a healthy session is left alone =="
cat >"$TMP/ps" <<'EOF'
100 1 9000 xrdp-sesman
200 100 9000 xrdp-sesman
201 200 9000 Xorg
202 200 9000 bash
203 202 9000 xfce4-session
EOF
run
if [ ! -s "$TMP/kills" ]; then pass "R1 nothing signalled"; else fail "R1 signalled: $(cat "$TMP/kills")"; cat "$TMP/out.txt"; fi

echo "== R2: a headless session is reaped, tree and all =="
cat >"$TMP/ps" <<'EOF'
100 1 9000 xrdp-sesman
200 100 9000 xrdp-sesman
204 200 9000 xrdp-chansrv
EOF
run
if killed 200 && killed 204; then pass "R2 sesman 200 and its chansrv 204 were signalled"; else fail "R2 kills='$(cat "$TMP/kills")'"; cat "$TMP/out.txt"; fi
if killed 100; then fail "R2 the LIVE sesman 100 was signalled"; else pass "R2 the live sesman was spared"; fi
if grep -q HEADLESS "$TMP/out.txt"; then pass "R2 the log names the shape"; else fail "R2 no HEADLESS line in the log"; fi

echo "== R3: a young headless session is spared (its X server is still starting) =="
cat >"$TMP/ps" <<'EOF'
100 1 9000 xrdp-sesman
200 100 5 xrdp-sesman
201 200 5 xrdp-sesman
EOF
run
if [ ! -s "$TMP/kills" ]; then pass "R3 nothing signalled inside the grace"; else fail "R3 signalled: $(cat "$TMP/kills")"; fi

echo "== R4: an orphan is still reaped =="
cat >"$TMP/ps" <<'EOF'
100 1 9000 xrdp-sesman
300 1 9000 xrdp-sesman
301 300 9000 Xorg
EOF
run
if killed 300 && killed 301; then pass "R4 orphan 300 and its Xorg were signalled"; else fail "R4 kills='$(cat "$TMP/kills")'"; cat "$TMP/out.txt"; fi

echo "== R5: sesman not active -> nothing =="
cat >"$TMP/ps" <<'EOF'
100 1 9000 xrdp-sesman
200 100 9000 xrdp-sesman
EOF
FAKE_STATE=inactive run
if [ ! -s "$TMP/kills" ]; then pass "R5 nothing signalled"; else fail "R5 signalled: $(cat "$TMP/kills")"; fi

echo
echo "$PASS PASS  $FAIL FAIL"
[ "$FAIL" -eq 0 ]
