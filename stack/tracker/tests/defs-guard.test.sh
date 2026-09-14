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
#   D7  GREEN when the set only GROWS — the baseline refreshes itself and the
#       added ids are logged (Owner ruling F1, 2026-09-13, option 1)
#   D8  RED when the definitions directory is gone entirely
#   D9  RED when the data root cannot be resolved at all — "cannot look" is not
#       the same as "fine", and it used to be the same status
#   D10 the state file is written on every path, and holds the verdict
#   D11 exit codes: 0 green, 2 yellow, 1 red
#   D12 HTTP 400 naming this guard OWN check id escalates to RED - the tracker
#       having never heard of tracker-definitions IS the deletion alarm
#   D13 a stopped tracker does not downgrade a verdict read off the volume
#   D14 EQUAL TOTALS ARE NOT AN EQUAL INVENTORY - a renamed id, or a file moved
#       between users, leaves files/items/users identical. It is a REMOVAL, and
#       red: the old id stopped being tracked
#   D17 add AND remove in one edit is a removal - the removal wins, and only a
#       human --baseline clears it
#   D18 a baseline with no recorded inventory (written before the ruling) does
#       NOT self-accept growth: the guard cannot prove nothing went with it
#   D19 a green run HEALS such a baseline, so the next addition self-accepts
#   D20 growth that cannot refresh the baseline stays YELLOW, never green
#   D21 a sidecar inventory that does not belong to the baseline beside it is
#       IGNORED - diffing against a stale one hides the removal in between
#   D22 and it is VERIFIED, not trusted: a body that does not hash to the
#       baseline's inv_hash is ignored however correct its own header looks
#   D15 a corrupt baseline is yellow, never green
#   D16 a state file that cannot be written escalates to RED - the durable
#       verdict is the whole point, and a stale one reads as current
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
echo "== D7: growth accepts ITSELF, and says which ids it accepted =="
# THE OWNER'S RULING (2026-09-13, F1 option 1). Editing the Sheet is already a
# deliberate, authenticated act; a second confirmation in a root shell on another
# device bought nothing, and a lane that sits yellow for days is learned as
# background noise — which costs the guard its ability to mean anything when a
# row DISAPPEARS. So growth refreshes the baseline itself AND names what it took.
rm -rf "$STATE" "$ROOT"; mkdir -p "$DEFS" "$STATE"
write_defs
guard --baseline >/dev/null
write_gamma() {
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
}
write_gamma
rc="$(guard --check)"
if [ "$rc" = 0 ] && [ "$(state_of band)" = green ]; then pass "D7 green when the set only grew (exit 0)"; else fail "D7 band=$(state_of band) exit=$rc"; cat "$TMP/out.txt"; fi
if grep -q 'INVENTORY GREW — baseline refreshed automatically' "$TMP/out.txt"; then pass "D7 the journal records the automatic acceptance"; else fail "D7 no auto-accept line"; cat "$TMP/out.txt"; fi
if grep -q 'user-one/four' "$TMP/out.txt"; then pass "D7 and NAMES the added id"; else fail "D7 the log does not name what it accepted: $(tail -2 "$TMP/out.txt")"; fi
if state_of added | grep -q 'user-one/four'; then pass "D7 the state file records the added id"; else fail "D7 state added=$(state_of added)"; fi
if [ "$(state_of baseline_items)" = "$(state_of items)" ]; then pass "D7 the state file reports the baseline it was actually measured against"; else fail "D7 baseline_items=$(state_of baseline_items) items=$(state_of items)"; fi
# THE BASELINE REALLY MOVED, which a green band alone does not prove: a guard
# that reported green without writing would go green again for the same reason
# forever and never notice the NEXT change.
rc="$(guard --check)"
if [ "$rc" = 0 ] && [ "$(state_of band)" = green ] && [ -z "$(state_of added)" ]; then pass "D7 the next run is quietly green — the baseline really was refreshed"; else fail "D7 second run band=$(state_of band) added=$(state_of added)"; fi
if [ "$(awk -F= '$1=="items"{print $2}' "$STATE/tracker-defs-baseline")" = 4 ]; then pass "D7 the baseline file carries the grown count"; else fail "D7 baseline items=$(awk -F= '$1=="items"{print $2}' "$STATE/tracker-defs-baseline")"; fi
if grep -q 'growth auto-accepted' "$STATE/tracker-defs-baseline"; then pass "D7 and records that no human typed it"; else fail "D7 the baseline does not say how it was recorded"; fi

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
echo "== D14: equal totals are not the same as an equal inventory =="
# THE HOLE COUNTS ALONE LEAVE, found by adversarial review before it could bite:
# rename an item, or move a file from one user to another, and files/items/users
# are all unchanged. The guard used to say "matches the baseline exactly" — a
# sentence about arithmetic, not about definitions.
rm -rf "$STATE" "$ROOT"; mkdir -p "$DEFS" "$STATE"
write_defs
guard --baseline >/dev/null
rc="$(guard --check)"
if [ "$rc" = 0 ]; then pass "D14 setup: green against its own baseline"; else fail "D14 setup exit=$rc"; fi
# Rename one id. Same file count, same item count, same user count.
sed -i 's/- id: three/- id: three-renamed/' "$DEFS/beta.md"
rc="$(guard --check)"
if [ "$(state_of files)" = 2 ] && [ "$(state_of items)" = 3 ]; then
    pass "D14 the totals really are unchanged (2 files / 3 items)"
else
    fail "D14 totals moved: files=$(state_of files) items=$(state_of items)"
fi
# A RENAME IS A REMOVAL. `three` stopped being tracked; that an id arrived in
# the same edit is not a mitigation, and under the F1 ruling the removal wins.
if [ "$rc" = 1 ] && [ "$(state_of band)" = red ]; then
    pass "D14 red on an inventory change the counts cannot see (exit 1)"
else
    fail "D14 band=$(state_of band) exit=$rc — a renamed item passed as 'matches exactly'"
fi
if state_of verdict | grep -q 'REMOVED'; then pass "D14 and the verdict says which kind of change it is"; else fail "D14 verdict: $(state_of verdict)"; fi
if state_of removed | grep -q 'user-one/three$\|user-one/three '; then pass "D14 the state file names the id that went"; else fail "D14 removed=$(state_of removed)"; fi
if state_of added | grep -q 'user-one/three-renamed'; then pass "D14 and the id that arrived with it"; else fail "D14 added=$(state_of added)"; fi

echo
echo "== D17: add AND remove in one edit is a REMOVAL, and only a human clears it =="
rm -rf "$STATE" "$ROOT"; mkdir -p "$DEFS" "$STATE"
write_defs
guard --baseline >/dev/null
# beta.md (1 item) out, gamma.md (1 item) in: files, items and users all land on
# exactly the numbers the baseline holds. Nothing but the inventory can see this.
rm -f "$DEFS/beta.md"
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
if [ "$(state_of files)" = 2 ] && [ "$(state_of items)" = 3 ]; then pass "D17 the totals really are unchanged (2 files / 3 items)"; else fail "D17 files=$(state_of files) items=$(state_of items)"; fi
if [ "$rc" = 1 ] && [ "$(state_of band)" = red ]; then pass "D17 the removal wins over the addition (exit 1)"; else fail "D17 band=$(state_of band) exit=$rc"; cat "$TMP/out.txt"; fi
if state_of removed | grep -q 'user-one/three'; then pass "D17 and it names the id that disappeared"; else fail "D17 removed=$(state_of removed)"; fi
if ! grep -q 'baseline refreshed automatically' "$TMP/out.txt"; then pass "D17 the baseline was NOT laundered"; else fail "D17 the guard auto-accepted an edit that removed an id"; fi
rc="$(guard --check)"
if [ "$rc" = 1 ]; then pass "D17 still red on the next run — it does not tire of saying so"; else fail "D17 second run exit=$rc"; fi
guard --baseline >/dev/null
rc="$(guard --check)"
if [ "$rc" = 0 ] && [ "$(state_of band)" = green ]; then pass "D17 green once a human accepted it with --baseline"; else fail "D17 post-baseline band=$(state_of band) exit=$rc"; fi

echo
echo "== D18/D19: a baseline written before the ruling has no inventory to diff =="
# The upgrade path. Without a recorded inventory the guard cannot prove that an
# edit ONLY added, so it must not self-accept — and it must say so rather than
# claiming the pre-ruling wording about a re-baseline being merely 'normal'.
rm -rf "$STATE" "$ROOT"; mkdir -p "$DEFS" "$STATE"
write_defs
guard --baseline >/dev/null
rm -f "$STATE/tracker-defs-baseline.inv"
write_gamma
rc="$(guard --check)"
if [ "$rc" = 2 ] && [ "$(state_of band)" = yellow ]; then pass "D18 growth against an inventory-less baseline is yellow, not green (exit 2)"; else fail "D18 band=$(state_of band) exit=$rc"; cat "$TMP/out.txt"; fi
if state_of verdict | grep -q 'no inventory record'; then pass "D18 and it says why it will not self-accept"; else fail "D18 verdict: $(state_of verdict)"; fi
guard --baseline >/dev/null
if [ -s "$STATE/tracker-defs-baseline.inv" ]; then pass "D18 --baseline records the inventory alongside"; else fail "D18 no inventory file after --baseline"; fi

# D19: the same heal, without a human — a GREEN run is a known-good moment, and
# recording the inventory there is safe because it matches the accepted hash.
rm -f "$STATE/tracker-defs-baseline.inv"
rc="$(guard --check)"
if [ "$rc" = 0 ] && [ -s "$STATE/tracker-defs-baseline.inv" ]; then pass "D19 a green run heals the missing inventory record"; else fail "D19 exit=$rc inv=$( [ -f "$STATE/tracker-defs-baseline.inv" ] && echo present || echo absent)"; fi
cat >"$DEFS/delta.md" <<'EOF'
---
category: Delta
color_weight: 1.0
items:
  - id: five
    title: Five
    type: habit
    recur: daily
    horizon: daily
---

# Delta
EOF
rc="$(guard --check)"
if [ "$rc" = 0 ] && [ "$(state_of band)" = green ]; then pass "D19 and the NEXT addition then self-accepts"; else fail "D19 post-heal band=$(state_of band) exit=$rc"; fi

echo
echo "== D20: growth that cannot refresh the baseline is yellow, never green =="
# A green band means "the baseline agrees with the definitions". If the refresh
# did not land there is no such baseline, and reporting green would be a claim
# about a file that was never written.
rm -rf "$STATE" "$ROOT"; mkdir -p "$DEFS" "$STATE"
write_defs
guard --baseline >/dev/null
write_gamma
if [ "$(id -u)" = 0 ]; then
    printf 'SKIP  D20: running as root, which can write into a 0500 directory anyway\n'
else
    # ONLY THE BASELINE IS MADE UNWRITABLE. Making the whole state directory
    # read-only would break write_state too, and D16 escalates THAT to red — so
    # the assertion would pass even if the growth branch wrongly chose green.
    # (Adversarial review, 2026-09-13.) The baseline path is therefore moved to
    # its own read-only directory while the state file stays where it was.
    mkdir -p "$TMP/ro-baseline"
    cp "$STATE/tracker-defs-baseline" "$TMP/ro-baseline/tracker-defs-baseline"
    cp "$STATE/tracker-defs-baseline.inv" "$TMP/ro-baseline/tracker-defs-baseline.inv"
    chmod 500 "$TMP/ro-baseline"
    rc="$(TRACKER_DEFS_ROOT="$ROOT" TRACKER_DEFS_STATE_DIR="$STATE" \
          TRACKER_DEFS_BASELINE_FILE="$TMP/ro-baseline/tracker-defs-baseline" \
          bash "$GUARD" --check >"$TMP/out.txt" 2>&1; echo $?)"
    chmod 700 "$TMP/ro-baseline"
    if [ "$rc" = 2 ] && [ "$(state_of band)" = yellow ]; then pass "D20 growth with an unwritable baseline is YELLOW, not green (exit 2)"; else fail "D20 band=$(state_of band) exit=$rc"; cat "$TMP/out.txt"; fi
    if state_of verdict | grep -q 'could NOT be refreshed'; then pass "D20 and it says the refresh did not land"; else fail "D20 verdict: $(state_of verdict)"; fi
    if [ "$(awk -F= '$1=="items"{print $2}' "$TMP/ro-baseline/tracker-defs-baseline")" = 3 ]; then pass "D20 the baseline really is unchanged"; else fail "D20 the baseline moved anyway"; fi
fi

echo
echo "== D21: a sidecar that belongs to another baseline is not evidence =="
# THE HOLE A PARTIAL REFRESH LEAVES (adversarial review, 2026-09-13). The
# baseline and its inventory are two writes, and the second can fail. Diffing a
# new baseline against an OLD inventory reports long-accepted ids as additions
# — and, worse, an id removed in between never appears in the diff at all, so a
# removal would be auto-accepted as growth. The sidecar therefore names the hash
# it was taken with, and a mismatch means "no inventory", not "this one".
rm -rf "$STATE" "$ROOT"; mkdir -p "$DEFS" "$STATE"
write_defs
guard --baseline >/dev/null
# Stage exactly the aftermath of a failed sidecar write: the baseline moves on,
# the inventory does not.
write_gamma
guard --check >/dev/null                      # auto-accepts {four}; both files move
cp "$STATE/tracker-defs-baseline.inv" "$TMP/stale.inv"
cat >"$DEFS/delta.md" <<'EOF'
---
category: Delta
color_weight: 1.0
items:
  - id: five
    title: Five
    type: habit
    recur: daily
    horizon: daily
---

# Delta
EOF
guard --check >/dev/null                      # auto-accepts {five}
cp "$TMP/stale.inv" "$STATE/tracker-defs-baseline.inv"   # the stale sidecar returns
# Now remove `five` and add `six`. Against the CURRENT baseline that is a
# removal; against the stale inventory it would look like pure growth.
rm -f "$DEFS/delta.md"
cat >"$DEFS/epsilon.md" <<'EOF'
---
category: Epsilon
color_weight: 1.0
items:
  - id: six
    title: Six
    type: habit
    recur: daily
    horizon: daily
---

# Epsilon
EOF
rc="$(guard --check)"
if [ "$rc" != 0 ]; then pass "D21 a stale sidecar did not launder the removal (exit $rc)"; else fail "D21 exit=0 — a removal was auto-accepted as growth"; cat "$TMP/out.txt"; fi
if ! grep -q 'baseline refreshed automatically' "$TMP/out.txt"; then pass "D21 and the baseline was not refreshed"; else fail "D21 the baseline was laundered against an inventory it does not belong to"; fi
if state_of verdict | grep -qE 'INVENTORY does not|no inventory record'; then pass "D21 it reports the mismatch as HAVING no inventory, not as one"; else fail "D21 verdict: $(state_of verdict)"; fi

echo
echo "== D22: the sidecar is verified, not trusted =="
# THE HEADER IS NOT THE EVIDENCE (adversarial review, second round). A sidecar
# truncated, half-restored or hand-edited can carry the CURRENT baseline hash in
# its header over a body that is missing an id — and that id can then be deleted
# with the diff never seeing it, because it was never in the list being diffed.
# So the body is re-hashed on every read.
rm -rf "$STATE" "$ROOT"; mkdir -p "$DEFS" "$STATE"
write_defs
cat >"$DEFS/delta.md" <<'EOF'
---
category: Delta
color_weight: 1.0
items:
  - id: five
    title: Five
    type: habit
    recur: daily
    horizon: daily
---

# Delta
EOF
guard --baseline >/dev/null
rc="$(guard --check)"
if [ "$rc" = 0 ]; then pass "D22 setup: green, with a sidecar this baseline really owns"; else fail "D22 setup exit=$rc"; fi
# Forge it: drop `five` from the BODY, leave the header saying the baseline hash.
grep -v 'user-one/five$' "$STATE/tracker-defs-baseline.inv" >"$TMP/forged.inv"
mv "$TMP/forged.inv" "$STATE/tracker-defs-baseline.inv"
if head -1 "$STATE/tracker-defs-baseline.inv" | grep -q "$(awk -F= '$1=="inv_hash"{print $2}' "$STATE/tracker-defs-baseline")"; then
    pass "D22 the forged sidecar still claims the right baseline"
else
    fail "D22 the forgery did not keep the header, so this proves nothing"
fi
# Now do exactly what the forgery would hide: remove `five`, add `six`.
rm -f "$DEFS/delta.md"
cat >"$DEFS/epsilon.md" <<'EOF'
---
category: Epsilon
color_weight: 1.0
items:
  - id: six
    title: Six
    type: habit
    recur: daily
    horizon: daily
---

# Epsilon
EOF
rc="$(guard --check)"
if [ "$rc" != 0 ]; then pass "D22 a body that does not hash to the baseline is not evidence (exit $rc)"; else fail "D22 exit=0 — a removal was laundered behind a correct-looking header"; cat "$TMP/out.txt"; fi
if ! grep -q 'baseline refreshed automatically' "$TMP/out.txt"; then pass "D22 and the baseline was not refreshed"; else fail "D22 the baseline was laundered"; fi

echo
echo "== D15: a corrupt baseline is yellow, never green =="
# Without validation, a baseline whose items= is non-numeric makes the numeric
# comparisons ERROR, and with no `set -e` the script falls through to the final
# else — which is GREEN. A corrupt baseline is exactly when this must not say
# everything is fine.
write_defs
guard --baseline >/dev/null
sed -i 's/^items=.*/items=not-a-number/' "$STATE/tracker-defs-baseline"
rc="$(guard --check)"
if [ "$rc" = 2 ] && [ "$(state_of band)" = yellow ]; then
    pass "D15 a non-numeric baseline field gives yellow, not green (exit 2)"
else
    fail "D15 band=$(state_of band) exit=$rc"
fi
if state_of verdict | grep -q 'corrupt'; then pass "D15 and it names the field"; else fail "D15 verdict: $(state_of verdict)"; fi

echo
echo "== D16: a verdict nobody can read back is not a verdict =="
# The state file is the durable half of this design — verify-hub.sh reads it and
# never asks the tracker. If it could not be written, the newest thing on disk is
# a STALE answer that reads as current, and the exit code is the only signal
# systemd then throws away.
write_defs
guard --baseline >/dev/null
chmod 500 "$STATE" 2>/dev/null || true
rc="$(guard --check)"
chmod 700 "$STATE" 2>/dev/null || true
if [ "$(id -u)" = 0 ]; then
    printf 'SKIP  D16: running as root, which can write into a 0500 directory anyway\n'
elif [ "$rc" = 1 ]; then
    pass "D16 an unwritable state directory escalates to RED (exit 1)"
    if grep -q 'nothing durable records it' "$TMP/out.txt"; then pass "D16 and it says why"; else fail "D16 no explanation"; fi
else
    fail "D16 exit=$rc with an unwritable state dir, want 1"; cat "$TMP/out.txt"
fi
echo
echo "──────────────────────────────────────────────────────────────"
printf '%s PASS  %s FAIL\n' "$PASS" "$FAIL"
if [ "$FAIL" -eq 0 ]; then exit 0; fi
exit 1
