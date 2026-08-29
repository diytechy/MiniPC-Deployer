#!/usr/bin/env bash
# defs-guard.test.sh — the deleted-definitions alarm, exercised.
#
# Hermetic: it points the guard at a temp directory with TRACKER_DEFS_ROOT and
# needs no docker, no tracker and no volume. bash + coreutils + awk.
#
# WHAT IT LOCKS
#   D1  a healthy set reads the right file/item counts and the newest mtime
#   D2  item counting stops at the frontmatter fence (prose examples do not count)
#   D3  --baseline REFUSES an empty set — the state the guard exists to report
#       must never be recordable as normal
#   D4  green when the inventory matches the baseline
#   D5  RED when a whole file is deleted
#   D6  RED when items are deleted but the file survives (the quiet one)
#   D7  YELLOW when the set GROWS, and green again after a deliberate re-baseline
#   D8  RED when the definitions directory is gone entirely
#   D9  RED when the data root cannot be resolved at all — "cannot look" is not
#       the same as "fine", and it used to be the same status
#   D10 the state file is written on every path, and holds the verdict
#   D11 exit codes: 0 green, 2 yellow, 1 red
#   D12 HTTP 400 naming this guard OWN check id escalates to RED - the tracker
#       having never heard of tracker-definitions IS the deletion alarm
#   D13 a stopped tracker does not downgrade a verdict read off the volume
#
# Usage: bash defs-guard.test.sh [--keep-tmp]
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARD="$(cd "$HERE/.." && pwd)/tracker-defs-guard.sh"
[ -f "$GUARD" ] || { echo "FATAL: $GUARD not found"; exit 2; }
KEEP_TMP=0
[ "${1:-}" = "--keep-tmp" ] && KEEP_TMP=1

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }

TMP="$(mktemp -d)"
cleanup() { if [ "$KEEP_TMP" = 1 ]; then echo "tmp kept: $TMP"; return 0; fi; rm -rf "$TMP"; }
trap cleanup EXIT

ROOT="$TMP/data"; STATE="$TMP/state"
DEFS="$ROOT/user-one/definitions"
mkdir -p "$DEFS" "$STATE"

# A definition file whose PROSE also contains a "- id:" line. D2 is the reason:
# every real file in this project ends in prose, and a naive grep over the whole
# file would count the examples in it.
write_defs() {
    cat >"$DEFS/alpha.md" <<'EOF'
---
category: Alpha
color_weight: 1.0
items:
  - id: one
    title: One
    type: habit
    recur: daily
    horizon: daily

  - id: two
    title: Two
    type: habit
    recur: daily
    horizon: daily
---

# Alpha

An item looks like this in the frontmatter:

  - id: not-a-real-item
    title: This is prose, not a definition
EOF
    cat >"$DEFS/beta.md" <<'EOF'
---
category: Beta
color_weight: 1.0
items:
  - id: three
    title: Three
    type: habit
    recur: daily
    horizon: daily
---

# Beta
EOF
}

guard() { # guard MODE... -> echoes exit code; output in $TMP/out.txt
    TRACKER_DEFS_ROOT="$ROOT" TRACKER_DEFS_STATE_DIR="$STATE" \
        bash "$GUARD" "$@" >"$TMP/out.txt" 2>&1
    echo $?
}
state_of() { awk -F= -v k="$1" '$1==k{sub(/^[^=]*=/,"");print}' "$STATE/tracker-defs.state"; }

echo "== D1/D2: the inventory is counted, and prose is not a definition =="
write_defs
rc="$(guard --check)"
if [ "$(state_of files)" = 2 ]; then pass "D1 counted 2 definition files"; else fail "D1 files=$(state_of files), want 2"; fi
if [ "$(state_of items)" = 3 ]; then pass "D2 counted 3 items — the prose '- id:' was NOT counted"; else fail "D2 items=$(state_of items), want 3"; fi
if [ "$(state_of users)" = 1 ]; then pass "D1 counted 1 user directory"; else fail "D1 users=$(state_of users), want 1"; fi
if [ "$(state_of newest_epoch)" -gt 0 ]; then pass "D1 recorded a newest-change timestamp ($(state_of newest_utc))"; else fail "D1 no newest_epoch"; fi
if [ "$rc" = 2 ]; then pass "D11 no baseline yet -> yellow (exit 2)"; else fail "D11 exit=$rc with no baseline, want 2"; fi

echo
echo "== D3: an empty set can never be recorded as normal =="
mv "$DEFS" "$TMP/stash"
rc="$(guard --baseline)"
if [ "$rc" != 0 ]; then pass "D3 --baseline REFUSED an empty definition set (exit $rc)"; else fail "D3 --baseline accepted an empty set"; fi
if grep -q 'refusing to baseline an EMPTY' "$TMP/out.txt"; then pass "D3 and said why"; else fail "D3 refusal did not explain itself"; cat "$TMP/out.txt"; fi
if [ ! -f "$STATE/tracker-defs-baseline" ]; then pass "D3 no baseline file was written"; else fail "D3 a baseline file exists after the refusal"; fi
mv "$TMP/stash" "$DEFS"

echo
echo "== D4: green once the baseline matches =="
rc="$(guard --baseline)"
if [ "$rc" = 0 ] && [ -f "$STATE/tracker-defs-baseline" ]; then pass "D4 baseline recorded"; else fail "D4 --baseline exit=$rc"; cat "$TMP/out.txt"; fi
rc="$(guard --check)"
if [ "$rc" = 0 ] && [ "$(state_of band)" = green ]; then pass "D4 green against its own baseline (exit 0)"; else fail "D4 band=$(state_of band) exit=$rc"; fi

echo
echo "== D5: a whole file deleted is RED =="
rm -f "$DEFS/beta.md"
rc="$(guard --check)"
if [ "$rc" = 1 ] && [ "$(state_of band)" = red ]; then pass "D5 red when a file vanished (exit 1)"; else fail "D5 band=$(state_of band) exit=$rc"; fi
if state_of verdict | grep -q 'SHRANK'; then pass "D5 the verdict says SHRANK and carries both counts"; else fail "D5 verdict: $(state_of verdict)"; fi

echo
echo "== D6: items deleted while the file survives is the quiet one, and is RED =="
write_defs
guard --baseline >/dev/null
python3 - "$DEFS/alpha.md" <<'PY' 2>/dev/null || sed -i '/- id: two/,+5d' "$DEFS/alpha.md"
import sys, re
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
s = s.replace("""  - id: two
    title: Two
    type: habit
    recur: daily
    horizon: daily
""", "")
open(p, "w", encoding="utf-8").write(s)
PY
rc="$(guard --check)"
if [ "$(state_of files)" = 2 ] && [ "$(state_of items)" = 2 ]; then pass "D6 the file count is unchanged and the item count dropped"; else fail "D6 files=$(state_of files) items=$(state_of items), want 2/2"; fi
if [ "$rc" = 1 ] && [ "$(state_of band)" = red ]; then pass "D6 red on an item deletion with no file deletion (exit 1)"; else fail "D6 band=$(state_of band) exit=$rc"; fi

echo
echo "== D7: growth is YELLOW, and re-baselining is the deliberate way back =="
write_defs
guard --baseline >/dev/null
cat >"$DEFS/gamma.md" <<'EOF'
---
category: Gamma
color_weight: 1.0
items:
  - id: four
    title: Four
    type: habit
    recur: daily
    horizon: daily
---

# Gamma
EOF
rc="$(guard --check)"
if [ "$rc" = 2 ] && [ "$(state_of band)" = yellow ]; then pass "D7 yellow when the set grew (exit 2)"; else fail "D7 band=$(state_of band) exit=$rc"; fi
if state_of verdict | grep -q 'GREW'; then pass "D7 the verdict says GREW and names the re-baseline command"; else fail "D7 verdict: $(state_of verdict)"; fi
guard --baseline >/dev/null
rc="$(guard --check)"
if [ "$rc" = 0 ] && [ "$(state_of band)" = green ]; then pass "D7 green again after a deliberate re-baseline"; else fail "D7 post-baseline band=$(state_of band) exit=$rc"; fi

echo
echo "== D8/D9: absent definitions, and an unresolvable root =="
rm -rf "$DEFS"
rc="$(guard --check)"
if [ "$rc" = 1 ] && [ "$(state_of band)" = red ]; then pass "D8 red when the definitions directory is gone"; else fail "D8 band=$(state_of band) exit=$rc"; fi
if state_of verdict | grep -q 'renders GREEN with score 0'; then pass "D8 the verdict names the failure mode it is guarding"; else fail "D8 verdict: $(state_of verdict)"; fi

rc="$(TRACKER_DEFS_STATE_DIR="$STATE" TRACKER_DEFS_VOLUME=definitely_no_such_volume PATH=/nonexistent:/usr/bin:/bin bash "$GUARD" --check >"$TMP/out.txt" 2>&1; echo $?)"
if [ "$rc" = 1 ]; then pass "D9 red when the data volume cannot be resolved at all (exit 1)"; else fail "D9 exit=$rc, want 1"; cat "$TMP/out.txt"; fi
if grep -q 'not the same as them being fine' "$TMP/out.txt"; then pass "D9 and it distinguishes 'cannot look' from 'fine'"; else fail "D9 message: $(tail -1 "$TMP/out.txt")"; fi

echo
echo "== D10: the state file is the half that does not need the tracker =="
mkdir -p "$DEFS"; write_defs
guard --check >/dev/null
for k in checked_utc band users files items newest_utc age_days verdict; do
    if [ -n "$(state_of "$k")" ]; then pass "D10 state file carries $k"; else fail "D10 state file has no $k"; fi
done


echo
echo "== D12: the 400 that IS the alarm, not a feed fault =="
# THE CASE THAT CANNOT BE STAGED ON A LIVE BOX WITHOUT BREAKING IT: every
# definition deleted AND the day rolled over, so the tracker has never heard of
# `tracker-definitions` and answers 400. Reporting that as "the feed failed"
# would be the single most misleading thing this script could do — the 400 is
# the deletion alarm arriving by another route.
#
# A MOCK `docker` ON PATH, the same technique run-drivepower-sim.sh uses for
# hdparm. It answers `inspect` with `true` (the container is up) and makes
# `exec … wget` print the 400 wget itself would print, then exit 8.
MOCKBIN="$TMP/mockbin"; mkdir -p "$MOCKBIN"
cat >"$MOCKBIN/docker" <<'MOCK'
#!/usr/bin/env bash
case "$1" in
  inspect) echo true; exit 0 ;;
  exec)
    printf '  HTTP/1.1 400 Bad Request\n  Content-Type: text/plain; charset=utf-8\n' >&2
    printf 'unknown feeder check id: tracker-definitions\n'
    exit 8 ;;
esac
exit 0
MOCK
chmod +x "$MOCKBIN/docker"
cat >"$TMP/feed.env" <<'ENVF'
NAGLIGHT_FEED_URL=http://127.0.0.1:8787/api/feed
NAGLIGHT_FEED_CONTAINER=tracker
NAGLIGHT_USER=someone
ENVF
mkdir -p "$DEFS"; write_defs
TRACKER_DEFS_ROOT="$ROOT" TRACKER_DEFS_STATE_DIR="$STATE" TRACKER_DEFS_ENV_FILE="$TMP/feed.env" \
    PATH="$MOCKBIN:$PATH" bash "$GUARD" --baseline >/dev/null 2>&1
TRACKER_DEFS_ROOT="$ROOT" TRACKER_DEFS_STATE_DIR="$STATE" TRACKER_DEFS_ENV_FILE="$TMP/feed.env" \
    PATH="$MOCKBIN:$PATH" bash "$GUARD" --report >"$TMP/out.txt" 2>&1
rc=$?
if [ "$rc" = 1 ]; then pass "D12 a 400 naming this check id exits RED (1), not green"; else fail "D12 exit=$rc, want 1"; cat "$TMP/out.txt"; fi
if grep -q 'RED (escalated)' "$TMP/out.txt"; then pass "D12 the escalation is named in the log"; else fail "D12 no escalation line"; cat "$TMP/out.txt"; fi
if grep -q 'This is the deletion alarm, not a feed fault' "$TMP/out.txt"; then pass "D12 and it says which kind of failure this is"; else fail "D12 the escalation does not explain itself"; fi
if [ "$(state_of band)" = red ]; then pass "D12 the state file was corrected to red"; else fail "D12 state band=$(state_of band), want red"; fi
if [ -n "$(state_of feed_escalation)" ]; then pass "D12 the state file records the escalation reason"; else fail "D12 no feed_escalation in the state file"; fi

echo
echo "== D13: a stopped tracker does not downgrade a verdict read off the volume =="
cat >"$MOCKBIN/docker" <<'MOCK2'
#!/usr/bin/env bash
case "$1" in
  inspect) echo false; exit 0 ;;
esac
exit 0
MOCK2
chmod +x "$MOCKBIN/docker"
TRACKER_DEFS_ROOT="$ROOT" TRACKER_DEFS_STATE_DIR="$STATE" TRACKER_DEFS_ENV_FILE="$TMP/feed.env" \
    PATH="$MOCKBIN:$PATH" bash "$GUARD" --report >"$TMP/out.txt" 2>&1
rc=$?
if [ "$rc" = 0 ] && [ "$(state_of band)" = green ]; then pass "D13 green stands with the container down (exit 0)"; else fail "D13 exit=$rc band=$(state_of band)"; cat "$TMP/out.txt"; fi
if grep -q 'read from the volume, not from the tracker' "$TMP/out.txt"; then pass "D13 and it says why the verdict is still trustworthy"; else fail "D13 no explanation for the un-sent feed"; fi
echo
echo "──────────────────────────────────────────────────────────────"
printf '%s PASS  %s FAIL\n' "$PASS" "$FAIL"
if [ "$FAIL" -eq 0 ]; then exit 0; fi
exit 1
