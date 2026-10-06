#!/usr/bin/env bash
# fence.test.sh — the DNS-console fence (tcp/5380), against a fake netfilter
# and, where the box allows it, against the real one.
#
# PART 1 IS HERMETIC. `iptables`, `ip6tables` and `curl` on PATH are fakes
# written below: one INPUT chain per family in a text file, the subset of the
# CLI the fence uses (-S, -C, -I, -A, -D by spec or number, -w), serialised with
# flock the way the kernel serialises real rule edits. The seed is the hub's
# measured chain (2026-09-25, addresses replaced by documentation ranges): the
# game REJECT, Tailscale's jump, the LLM lane's rules, the RustDesk fence.
#
# PART 2 USES REAL iptables INSIDE A THROWAWAY NETWORK NAMESPACE (`unshare
# -rn`: no root, nothing on the host is touched). It proves what the fakes can
# only assume - that iptables accepts the rule, prints it the way the parser
# expects, and that the REAL llm-isolation.sh does not die with this fence above
# ts-input. It SKIPS when the box has no iptables or forbids unprivileged user
# namespaces (Ubuntu 24.04's AppArmor default does); FENCE_TEST_REAL_BIN may name
# a directory holding iptables/ip6tables to use instead of the system's.
#
# WHAT IT LOCKS
#   F1  install with ts-input present lands DIRECTLY above it, both families,
#       below the game REJECT, touching nothing else
#   F2  the rules are exactly one negated REJECT per family, and no ACCEPT
#   F3  install with no ts-input jump lands at the top
#   F4  replay converges: no duplicates, identical chain
#   F5  check on a healthy chain changes nothing and alerts nobody
#   F6  a tailscaled restart (jump deleted, re-inserted at 1) is DRIFT: status
#       says so without touching anything; check repairs it and alerts
#   F7  the after-tailscaled hook repairs the same drift QUIETLY
#   F8  the hook waits for a jump that arrives late, then lands above it
#   F9  the hook gives up within its bound when no jump comes, fence present
#   F10 a subnet change leaves no rule for the old subnet
#   F11 only tcp/5380 is ever matched or edited; no other rule moves
#   F12 the v6 half exempts loopback only
#   F13 remove deletes every marked rule and nothing else
#   F14 a failing or hanging alert cannot fail or stall the fence
#   F15 a failure to re-assert alerts AND fails the unit
#   F16 a broken ip6tables fails loudly; a kernel without IPv6 is not a failure
#   F17 an unusable ADMIN_NET_SUBNET changes nothing
#   F18 stale duplicates, an old subnet, the old ACCEPT/ACCEPT/REJECT design and
#       a stray marked rule all converge to one rule
#   F19 a repeated alert is suppressed inside the repeat window
#   F20 concurrent runs during a tailscaled restart still end with one rule
#   F21 a comment printed quoted by `iptables -S` is still purged
#   L1  llm-isolation.sh's "no foreign ACCEPT above my deny" check, extracted
#       and run over a listing in iptables 1.8.10's real format: silent with
#       this fence, loud with the old ACCEPT design (runs everywhere)
#   R1-R8  part 2, real netfilter: placement, exact `-S` text, drift and repair,
#       replay, subnet change, remove, and the real llm-isolation.sh running
#       clean over the fence - and dying over the old design, as a control
#
# Usage: bash fence.test.sh [--keep-tmp]
#
# `A && pass || fail` is safe here because pass() cannot fail (SC2015),
# cleanup() is reached through the EXIT trap (SC2329), and the single-quoted
# `$` strings are awk programs and child-shell scripts, meant literally (SC2016).
# shellcheck disable=SC2015,SC2016,SC2329
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUT="$(cd "$HERE/.." && pwd)/dns-console-fence.sh"
LLM="$(cd "$HERE/../.." && pwd)/llm-isolation/llm-isolation.sh"
[ -f "$SUT" ] || { echo "FATAL: $SUT not found"; exit 2; }
command -v flock >/dev/null 2>&1 || { echo "FATAL: flock (util-linux) is required"; exit 2; }
KEEP_TMP=0
[ "${1:-}" = "--keep-tmp" ] && KEEP_TMP=1

PASS=0; FAIL=0; SKIP=0
pass() { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }
skip() { SKIP=$((SKIP+1)); printf 'SKIP  %s\n' "$*"; }
check() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1: got [$2], want [$3]"; fi; }

TMP="$(mktemp -d)"
cleanup() { if [ "$KEEP_TMP" = 1 ]; then echo "tmp kept: $TMP"; return 0; fi; rm -rf "$TMP"; }
trap cleanup EXIT

BIN="$TMP/bin"; RBIN="$TMP/rbin"; ST="$TMP/state"; mkdir -p "$BIN" "$RBIN" "$ST"

# ── the fakes ───────────────────────────────────────────────────────────────
cat >"$BIN/iptables" <<'FAKE'
#!/usr/bin/env bash
# One INPUT chain in "$FAKE_IPT_STATE/<family>.rules", one `-A INPUT ...` line per
# rule in evaluation order. Comparisons ignore double quotes, as iptables does
# for a comment it chose to print quoted.
fam="$(basename "$0")"
st="${FAKE_IPT_STATE:?}"
rules="$st/$fam.rules"
exec 8>>"$st/fake.lock"; flock 8
touch "$rules"
printf '%s %s\n' "$fam" "$*" >>"$st/calls.log"
args=()
while [ $# -gt 0 ]; do
    case "$1" in
        -w|--wait) shift; case "${1:-}" in ''|*[!0-9]*) ;; *) shift ;; esac ;;
        *) args+=("$1"); shift ;;
    esac
done
set -- "${args[@]}"
if [ "$fam" = iptables ]; then broken="${FAKE_BROKEN_V4:-0}"; else broken="${FAKE_BROKEN_V6:-0}"; fi
if [ "$broken" = 1 ]; then echo "$fam v1.8.10 (nf_tables): can't initialize $fam table \`filter': Table does not exist" >&2; exit 3; fi
op="${1:-}"; chain="${2:-}"
[ "$chain" = INPUT ] || { echo "fake $fam: only INPUT is modelled (got '$op $chain')" >&2; exit 2; }
shift 2
norm() { printf '%s' "$1" | tr -d '"'; }
case "$op" in
    -S) echo "-P INPUT ACCEPT"; cat "$rules"; exit 0 ;;
    -C) want="$(norm "-A INPUT $*")"
        while IFS= read -r l; do [ "$(norm "$l")" = "$want" ] && exit 0; done <"$rules"
        echo "$fam: Bad rule (does a matching rule exist in that chain?)." >&2; exit 1 ;;
    -I) pos=1
        if [[ "${1:-}" =~ ^[0-9]+$ ]]; then pos="$1"; shift; fi
        [ "${FAKE_FAIL_INSERT:-0}" = 1 ] && { echo "$fam: RULE_INSERT failed (fake)" >&2; exit 4; }
        case " $* " in *" --reject-with tcp-reset "*) case " $* " in *" -p tcp "*) ;; *)
            echo "$fam: REJECT: TCP_RESET invalid for non-tcp" >&2; exit 2 ;; esac ;; esac
        n="$(wc -l <"$rules")"
        [ "$pos" -ge 1 ] && [ "$pos" -le $((n + 1)) ] || { echo "$fam: Index of insertion too big." >&2; exit 1; }
        awk -v p="$pos" -v l="-A INPUT $*" 'NR == p { print l } { print } END { if (p == NR + 1) print l }' \
            "$rules" >"$rules.tmp" && mv "$rules.tmp" "$rules"
        exit 0 ;;
    -A) echo "-A INPUT $*" >>"$rules"; exit 0 ;;
    -D) if [ $# -eq 1 ] && [[ "$1" =~ ^[0-9]+$ ]]; then
            n="$(wc -l <"$rules")"
            [ "$1" -ge 1 ] && [ "$1" -le "$n" ] || { echo "$fam: Index of deletion too big." >&2; exit 1; }
            awk -v p="$1" 'NR != p' "$rules" >"$rules.tmp" && mv "$rules.tmp" "$rules"; exit 0
        fi
        want="$(norm "-A INPUT $*")"; i=0; hit=0
        while IFS= read -r l; do i=$((i + 1)); if [ "$(norm "$l")" = "$want" ]; then hit="$i"; break; fi; done <"$rules"
        [ "$hit" -gt 0 ] || { echo "$fam: Bad rule (does a matching rule exist in that chain?)." >&2; exit 1; }
        awk -v p="$hit" 'NR != p' "$rules" >"$rules.tmp" && mv "$rules.tmp" "$rules"; exit 0 ;;
    *)  echo "fake $fam: unsupported operation $op" >&2; exit 2 ;;
esac
FAKE
cp "$BIN/iptables" "$BIN/ip6tables"
cat >"$BIN/curl" <<'FAKE'
#!/usr/bin/env bash
# ONE line per call: the alert body has newlines, and callers count lines.
printf '%s\n' "$(printf '%s ' "$@" | tr '\n' '|')" >>"${FAKE_IPT_STATE:?}/curl.log"
case "${FAKE_CURL_MODE:-ok}" in
    ok)   exit 0 ;;
    fail) exit 7 ;;
    hang) exec sleep 30 ;;
esac
FAKE
chmod +x "$BIN/iptables" "$BIN/ip6tables" "$BIN/curl"
cp "$BIN/curl" "$RBIN/curl"
# llm-isolation.sh asks `docker compose config` whether its network has IPv6.
cat >"$RBIN/docker" <<'FAKE'
#!/usr/bin/env bash
[ "$*" = "compose config --format json" ] || { echo "fake docker: unexpected '$*'" >&2; exit 2; }
echo '{"networks":{"llmprivate":{"enable_ipv6":false}}}'
FAKE
chmod +x "$RBIN/curl" "$RBIN/docker"

ENVF="$TMP/stack.env"
PROCV6="$TMP/proc-ipv6"; mkdir -p "$PROCV6"
write_env() { printf 'LAN_IP=192.0.2.10\nNTFY_PORT=8090\n%s' "${1:-}" >"$ENVF"; }

RD="-p tcp -m multiport --dports 21115,21116,21117,21118,21119 -m comment --comment homehub-rustdesk-isolation"
seed() { # seed [nots] - the hub's measured chain; `nots` leaves Tailscale's jump out
    local ts="-A INPUT -j ts-input"
    [ "${1:-}" = nots ] && ts=""
    {
        echo "-A INPUT -s 172.28.90.0/24 -m comment --comment homehub-game-isolation -j REJECT --reject-with icmp-port-unreachable"
        [ -n "$ts" ] && echo "$ts"
        echo '-A INPUT -s 172.28.91.10/32 -m conntrack --ctstate RELATED,ESTABLISHED -m comment --comment "homehub-llm-isolation host-established" -j ACCEPT'
        echo '-A INPUT -s 172.28.91.10/32 -d 172.28.91.1/32 -p tcp -m tcp --dport 8799 -m comment --comment "homehub-llm-isolation host-allow-wake" -j ACCEPT'
        echo '-A INPUT -s 172.28.91.10/32 -m comment --comment "homehub-llm-isolation host-deny" -j REJECT --reject-with icmp-port-unreachable'
        echo "-A INPUT -i lo $RD -j ACCEPT"
        echo "-A INPUT -s 192.0.2.0/24 $RD -j ACCEPT"
        echo "-A INPUT -s 100.64.0.0/10 $RD -j ACCEPT"
        echo "-A INPUT $RD -j REJECT --reject-with icmp-port-unreachable"
    } >"$ST/iptables.rules"
    {
        [ -n "$ts" ] && echo "$ts"
        echo "-A INPUT -i lo $RD -j ACCEPT"
        echo "-A INPUT -s 2001:db8::/32 $RD -j ACCEPT"
        echo "-A INPUT $RD -j REJECT --reject-with icmp6-port-unreachable"
    } >"$ST/ip6tables.rules"
    : >"$ST/calls.log"; : >"$ST/curl.log"; rm -rf "$TMP/alertstate"
    write_env
}

# sut MODE [VAR=value ...] -> prints the exit code; output in $TMP/out
sut() {
    local mode="$1"; shift
    env PATH="$BIN:$PATH" FAKE_IPT_STATE="$ST" \
        DNS_CONSOLE_ENV_FILE="$ENVF" DNS_CONSOLE_LOCK="$TMP/lock" \
        DNS_CONSOLE_STATE_DIR="$TMP/alertstate" DNS_CONSOLE_PROC_IPV6="$PROCV6" \
        DNS_CONSOLE_WAIT_SEC=3 DNS_CONSOLE_POLL_SEC=1 DNS_CONSOLE_V6_GRACE_SEC=1 \
        DNS_CONSOLE_SETTLE_SEC=0 DNS_CONSOLE_ALERT_TIMEOUT=2 DNS_CONSOLE_ALERT_REPEAT_SEC=0 \
        "$@" bash "$SUT" "$mode" >"$TMP/out" 2>&1
    echo $?
}

# One token per rule, in order: the shape of the chain at a glance. `dns` is
# exactly the fence rule for that family; any other marked rule is `dns?`.
tokens() {
    awk -v fam="$1" '{ t = "?" }
         /homehub-game-isolation/ { t = "game" }
         /^-A INPUT -j ts-input$/ { t = "ts" }
         /homehub-llm-isolation/ { t = "llm" }
         /homehub-rustdesk-isolation/ { t = "rd" }
         /--comment "?homehub-dns-console"?( |$)/ { t = "dns?"
             if (fam == "iptables" && $0 == "-A INPUT ! -s 172.28.92.0/24 ! -i lo -p tcp -m tcp --dport 5380 -m comment --comment homehub-dns-console -j REJECT --reject-with tcp-reset") t = "dns"
             if (fam == "ip6tables" && $0 == "-A INPUT ! -i lo -p tcp -m tcp --dport 5380 -m comment --comment homehub-dns-console -j REJECT --reject-with tcp-reset") t = "dns" }
         { printf "%s%s", (NR > 1 ? " " : ""), t } END { print "" }' "${2:-$ST/$1.rules}"
}
marked() { grep -c -- 'homehub-dns-console' "$ST/$1.rules"; }
others() { grep -v -- 'homehub-dns-console' "$ST/$1.rules"; }
# The same minus Tailscale's jump, for sequences that include a simulated
# restart - which moves the jump, and that is the point of simulating it.
others_nots() { grep -v -e 'homehub-dns-console' -e '^-A INPUT -j ts-input$' "$ST/$1.rules"; }
mutations() { grep -E -- ' -(I|A|D|F|X|P|R|N|Z) ' "$ST/calls.log" || true; }
restart_tailscaled() { # what tailscaled does on every start: cleanup, then Insert at 1
    local f
    for f in iptables ip6tables; do
        PATH="$BIN:$PATH" FAKE_IPT_STATE="$ST" "$f" -w -D INPUT -j ts-input 2>/dev/null
        PATH="$BIN:$PATH" FAKE_IPT_STATE="$ST" "$f" -w -I INPUT 1 -j ts-input
    done
}

V4="-A INPUT ! -s 172.28.92.0/24 ! -i lo -p tcp -m tcp --dport 5380 -m comment --comment homehub-dns-console -j REJECT --reject-with tcp-reset"
V6="-A INPUT ! -i lo -p tcp -m tcp --dport 5380 -m comment --comment homehub-dns-console -j REJECT --reject-with tcp-reset"
FENCED4="game dns ts llm llm llm rd rd rd rd"
FENCED6="dns ts rd rd rd"
REPAIRED4="dns ts game llm llm llm rd rd rd rd"

echo "── part 1: fake netfilter ──"
# ── F1 / F2 ─────────────────────────────────────────────────────────────────
seed
others4_before="$(others iptables)"; others6_before="$(others ip6tables)"
rc="$(sut install)"
check "F1 install exits 0" "$rc" 0
check "F1 v4: directly above ts-input, below the game REJECT" "$(tokens iptables)" "$FENCED4"
check "F1 v6: directly above ts-input" "$(tokens ip6tables)" "$FENCED6"
check "F1 no other v4 rule changed or moved" "$(others iptables)" "$others4_before"
check "F1 no other v6 rule changed or moved" "$(others ip6tables)" "$others6_before"
check "F2 v4 is exactly one negated REJECT" "$(grep -- homehub-dns-console "$ST/iptables.rules")" "$V4"
check "F2 v6 is exactly one negated REJECT" "$(grep -- homehub-dns-console "$ST/ip6tables.rules")" "$V6"
check "F2 no ACCEPT carries the marker (llm-isolation dies on a foreign ACCEPT above it)" \
    "$(grep -h -- homehub-dns-console "$ST/iptables.rules" "$ST/ip6tables.rules" | grep -c -- '-j ACCEPT' || true)" 0
rc="$(sut status)"
check "F1 status agrees (exit 0)" "$rc" 0
if grep -q 'fence at 2, ts-input at 3' "$TMP/out" && grep -q 'fence at 1, ts-input at 2' "$TMP/out"; then pass "F1 status reports both families in place"
else fail "F1 status output: $(cat "$TMP/out")"; fi

# ── F3 ──────────────────────────────────────────────────────────────────────
seed nots
rc="$(sut install)"
check "F3 install with no jump exits 0" "$rc" 0
check "F3 v4 at the top when there is no jump" "$(tokens iptables)" "dns game llm llm llm rd rd rd rd"
check "F3 v6 at the top when there is no jump" "$(tokens ip6tables)" "dns rd rd rd"

# ── F4 ──────────────────────────────────────────────────────────────────────
seed
sut install >/dev/null; first4="$(cat "$ST/iptables.rules")"; first6="$(cat "$ST/ip6tables.rules")"
sut install >/dev/null; rc="$(sut install)"
check "F4 third replay exits 0" "$rc" 0
check "F4 v4 replay converges byte-for-byte" "$(cat "$ST/iptables.rules")" "$first4"
check "F4 v6 replay converges byte-for-byte" "$(cat "$ST/ip6tables.rules")" "$first6"
check "F4 v4 still exactly one marked rule" "$(marked iptables)" 1
check "F4 v6 still exactly one marked rule" "$(marked ip6tables)" 1

# ── F5 ──────────────────────────────────────────────────────────────────────
: >"$ST/calls.log"
rc="$(sut check)"
check "F5 check on a healthy chain exits 0" "$rc" 0
check "F5 ...and edits nothing" "$(mutations)" ""
check "F5 ...and alerts nobody" "$(cat "$ST/curl.log")" ""

# ── F6 ──────────────────────────────────────────────────────────────────────
restart_tailscaled
check "F6 precondition: a restart puts the jump above the fence" "$(tokens iptables)" "ts game dns llm llm llm rd rd rd rd"
: >"$ST/calls.log"
rc="$(sut status)"
check "F6 status calls it drift (exit 3)" "$rc" 3
grep -q 'BELOW the ts-input jump' "$TMP/out" && pass "F6 status says why" || fail "F6 status output: $(cat "$TMP/out")"
check "F6 status edits nothing" "$(mutations)" ""
rc="$(sut check)"
check "F6 check repairs and exits 0" "$rc" 0
check "F6 v4 back directly above the jump" "$(tokens iptables)" "$REPAIRED4"
check "F6 v6 back directly above the jump" "$(tokens ip6tables)" "$FENCED6"
check "F6 exactly one alert" "$(wc -l <"$ST/curl.log" | tr -d ' ')" 1
if grep -q 'http://192.0.2.10:8090/homehub-alerts' "$ST/curl.log" && grep -q 'Title: HomeHub: DNS console fence drifted' "$ST/curl.log"; then
    pass "F6 the alert goes to LAN_IP:NTFY_PORT/homehub-alerts and says drifted"
else fail "F6 alert call: $(cat "$ST/curl.log")"; fi
write_env "NTFY_BIND_IP=127.0.0.1"$'\n'"HOMEHUB_ALERT_TOPIC=hub-ops"$'\n'; : >"$ST/curl.log"
restart_tailscaled; sut check >/dev/null
grep -q 'http://127.0.0.1:8090/hub-ops' "$ST/curl.log" && pass "F6 NTFY_BIND_IP and HOMEHUB_ALERT_TOPIC override the URL" || fail "F6 override: $(cat "$ST/curl.log")"
write_env

# ── F7 ──────────────────────────────────────────────────────────────────────
restart_tailscaled; : >"$ST/curl.log"
rc="$(sut after-tailscaled)"
check "F7 the hook repairs and exits 0" "$rc" 0
check "F7 v4 back above the jump" "$(tokens iptables)" "$REPAIRED4"
check "F7 v6 back above the jump" "$(tokens ip6tables)" "$FENCED6"
check "F7 ...quietly: no alert on the hook path" "$(cat "$ST/curl.log")" ""

# ── F8 ──────────────────────────────────────────────────────────────────────
seed nots
sut install >/dev/null
( sleep 2; for f in iptables ip6tables; do PATH="$BIN:$PATH" FAKE_IPT_STATE="$ST" "$f" -w -I INPUT 1 -j ts-input; done ) &
bg=$!
t0=$(date +%s)
rc="$(sut after-tailscaled DNS_CONSOLE_WAIT_SEC=15)"
t1=$(date +%s); wait "$bg" 2>/dev/null
check "F8 the hook exits 0 once the late jump appears" "$rc" 0
check "F8 v4 lands above the late jump" "$(tokens iptables)" "$REPAIRED4"
check "F8 v6 lands above the late jump" "$(tokens ip6tables)" "$FENCED6"
[ $((t1 - t0)) -lt 12 ] && pass "F8 it stopped waiting when the jump came ($((t1 - t0))s)" || fail "F8 took $((t1 - t0))s"

# ── F9 ──────────────────────────────────────────────────────────────────────
seed nots
t0=$(date +%s)
rc="$(sut after-tailscaled DNS_CONSOLE_WAIT_SEC=2)"
t1=$(date +%s)
check "F9 no jump ever: the hook still exits 0" "$rc" 0
[ $((t1 - t0)) -le 8 ] && pass "F9 ...within its bound ($((t1 - t0))s)" || fail "F9 took $((t1 - t0))s"
check "F9 ...with the fence present at the top" "$(tokens iptables)" "dns game llm llm llm rd rd rd rd"
grep -q 'no ts-input jump after 2s' "$TMP/out" && pass "F9 ...and says it gave up waiting" || fail "F9 output: $(cat "$TMP/out")"
check "F9 ...and no alert" "$(cat "$ST/curl.log")" ""

# ── F10 ─────────────────────────────────────────────────────────────────────
seed
write_env "ADMIN_NET_SUBNET=172.28.93.0/24"$'\n'
sut install >/dev/null
grep -q -- '^-A INPUT ! -s 172.28.93.0/24 ! -i lo -p tcp' "$ST/iptables.rules" && pass "F10 the override is used" || fail "F10 override not applied: $(cat "$ST/iptables.rules")"
write_env
: >"$ST/curl.log"
rc="$(sut check)"
check "F10 check after the subnet changed exits 0" "$rc" 0
check "F10 no rule for the old subnet remains" "$(grep -c '172.28.93.0/24' "$ST/iptables.rules")" 0
check "F10 exactly one rule, new subnet" "$(tokens iptables)" "$FENCED4"
[ -s "$ST/curl.log" ] && pass "F10 the stale subnet counted as drift and alerted" || fail "F10 no alert for the stale subnet"

# ── F11 ─────────────────────────────────────────────────────────────────────
seed
others4="$(others_nots iptables)"; others6="$(others_nots ip6tables)"
sut install >/dev/null; restart_tailscaled; sut check >/dev/null
restart_tailscaled; sut after-tailscaled >/dev/null; sut remove >/dev/null; sut install >/dev/null
bad="$(mutations | grep -v -- ' -j ts-input' | grep -v -- ' -p tcp -m tcp --dport 5380 -m comment --comment homehub-dns-console -j REJECT --reject-with tcp-reset' || true)"
check "F11 every edit the fence made is the tcp/5380 REJECT carrying the marker" "$bad" ""
check "F11 it never appends" "$(mutations | grep -c -- ' -A ' || true)" 0
check "F11 no other v4 rule changed or moved (bar the jump tailscaled moved)" "$(others_nots iptables)" "$others4"
check "F11 no other v6 rule changed or moved (bar the jump tailscaled moved)" "$(others_nots ip6tables)" "$others6"
check "F11 the v4 jump was never duplicated or dropped" "$(grep -c '^-A INPUT -j ts-input$' "$ST/iptables.rules")" 1

# ── F12 ─────────────────────────────────────────────────────────────────────
check "F12 v6 has exactly one marked rule" "$(marked ip6tables)" 1
check "F12 v6 exempts loopback only (no source match)" "$(grep -- homehub-dns-console "$ST/ip6tables.rules" | grep -c -- ' -s ' || true)" 0
grep -qxF -- "$V6" "$ST/ip6tables.rules" && pass "F12 v6 REJECT is tcp-reset, loopback exempt" || fail "F12 v6: $(cat "$ST/ip6tables.rules")"

# ── F13 ─────────────────────────────────────────────────────────────────────
seed; sut install >/dev/null
rc="$(sut remove)"
check "F13 remove exits 0" "$rc" 0
check "F13 no v4 marked rule remains" "$(marked iptables)" 0
check "F13 no v6 marked rule remains" "$(marked ip6tables)" 0
check "F13 the v4 chain is exactly the seed again" "$(tokens iptables)" "game ts llm llm llm rd rd rd rd"
check "F13 the v6 chain is exactly the seed again" "$(tokens ip6tables)" "ts rd rd rd"
rc="$(sut status)"
check "F13 status after remove reports drift (exit 3)" "$rc" 3
rc="$(sut remove)"
check "F13 remove on a clean chain is a no-op success" "$rc" 0

# ── F14 ─────────────────────────────────────────────────────────────────────
seed; sut install >/dev/null; restart_tailscaled
rc="$(sut check FAKE_CURL_MODE=fail)"
check "F14 a failing POST: check still exits 0" "$rc" 0
check "F14 ...and still repaired" "$(tokens iptables)" "$REPAIRED4"
grep -q 'ALERT NOT DELIVERED' "$TMP/out" && pass "F14 ...and says the alert was not delivered" || fail "F14 output: $(cat "$TMP/out")"
restart_tailscaled
t0=$(date +%s)
rc="$(sut check FAKE_CURL_MODE=hang DNS_CONSOLE_ALERT_TIMEOUT=1)"
t1=$(date +%s)
check "F14 a hanging POST: check still exits 0" "$rc" 0
[ $((t1 - t0)) -le 6 ] && pass "F14 ...bounded ($((t1 - t0))s, curl would hang 30s)" || fail "F14 took $((t1 - t0))s"
check "F14 ...and still repaired" "$(tokens iptables)" "$REPAIRED4"
restart_tailscaled; printf 'NTFY_PORT=\n' >"$ENVF"
rc="$(sut check)"
check "F14 no ntfy configured: check still exits 0" "$rc" 0
grep -q 'ALERT NOT SENT' "$TMP/out" && pass "F14 ...and says the alert was not sent" || fail "F14 output: $(cat "$TMP/out")"

# ── F15 ─────────────────────────────────────────────────────────────────────
seed; sut install >/dev/null; restart_tailscaled; : >"$ST/curl.log"
rc="$(sut check FAKE_FAIL_INSERT=1)"
check "F15 a re-assert that cannot insert exits 1" "$rc" 1
if grep -q 'Title: HomeHub: DNS console fence is NOT in place' "$ST/curl.log" && grep -q 'Priority: high' "$ST/curl.log"; then
    pass "F15 ...and sends a high-priority NOT-in-place alert"
else fail "F15 alert: $(cat "$ST/curl.log")"; fi
rc="$(sut check)"
check "F15 the next healthy run repairs it" "$rc" 0
check "F15 ...to the right place" "$(tokens iptables)" "$REPAIRED4"
rc="$(sut after-tailscaled FAKE_FAIL_INSERT=1)"
check "F15 the hook on a healthy chain inserts nothing, so a broken insert does not matter" "$rc" 0
restart_tailscaled; : >"$ST/curl.log"
rc="$(sut after-tailscaled FAKE_FAIL_INSERT=1)"
check "F15 the hook exits 1 when it cannot re-assert" "$rc" 1
[ -s "$ST/curl.log" ] && pass "F15 ...and alerts, although drift alone would be quiet" || fail "F15 hook failure did not alert"

# ── F16 ─────────────────────────────────────────────────────────────────────
seed; : >"$ST/curl.log"
rc="$(sut install FAKE_BROKEN_V6=1)"
check "F16 a broken ip6tables with IPv6 present fails the install" "$rc" 1
[ -s "$ST/curl.log" ] && pass "F16 ...and alerts" || fail "F16 no alert"
check "F16 ...while the v4 half is still installed" "$(tokens iptables)" "$FENCED4"
seed; rm -rf "$PROCV6"
rc="$(sut install FAKE_BROKEN_V6=1)"
check "F16 a kernel without IPv6 is not a failure" "$rc" 0
check "F16 ...and ip6tables is never called" "$(grep -c '^ip6tables' "$ST/calls.log" || true)" 0
mkdir -p "$PROCV6"

# ── F17 ─────────────────────────────────────────────────────────────────────
for bad in 0.0.0.0/0 10.0.0.0/8 172.28.92.5/24 172.28.92.0 300.1.1.0/24 "172.28.92.0/24 -j ACCEPT"; do
    seed; write_env "ADMIN_NET_SUBNET=$bad"$'\n'
    rc="$(sut install)"
    if [ "$rc" = 1 ] && [ -z "$(mutations)" ]; then pass "F17 ADMIN_NET_SUBNET='$bad' refused, nothing changed"
    else fail "F17 '$bad': exit $rc, edits: $(mutations)"; fi
done

# ── F18 ─────────────────────────────────────────────────────────────────────
seed
OLD="-p tcp -m tcp --dport 5380 -m comment --comment homehub-dns-console"
{ echo "-A INPUT -i lo $OLD -j ACCEPT"; echo "-A INPUT -s 172.28.92.0/24 $OLD -j ACCEPT"
  echo "-A INPUT $OLD -j REJECT --reject-with tcp-reset"; echo "$V4"
  cat "$ST/iptables.rules"; echo "$V4"
  echo "-A INPUT ! -s 172.28.77.0/24 ! -i lo $OLD -j REJECT --reject-with tcp-reset"
  echo "-A INPUT -p udp -m udp --dport 5380 -m comment --comment homehub-dns-console -j DROP"; } >"$ST/v4.tmp"
mv "$ST/v4.tmp" "$ST/iptables.rules"
rc="$(sut status)"
check "F18 status: duplicates, the old design and strays are drift" "$rc" 3
rc="$(sut install)"
check "F18 install over the mess exits 0" "$rc" 0
check "F18 exactly one rule, directly above the jump" "$(tokens iptables)" "$FENCED4"
check "F18 no ACCEPT, old subnet or stray survives" "$(grep -- homehub-dns-console "$ST/iptables.rules" | grep -c -e ACCEPT -e '172.28.77.0' -e ' -p udp ' || true)" 0

# ── F19 ─────────────────────────────────────────────────────────────────────
seed; sut install >/dev/null
restart_tailscaled; sut check DNS_CONSOLE_ALERT_REPEAT_SEC=600 >/dev/null
restart_tailscaled; rc="$(sut check DNS_CONSOLE_ALERT_REPEAT_SEC=600)"
check "F19 second drift inside the window: still repaired" "$rc" 0
check "F19 ...but only one alert was sent" "$(wc -l <"$ST/curl.log" | tr -d ' ')" 1
grep -q 'alert suppressed' "$TMP/out" && pass "F19 ...and the suppression is logged" || fail "F19 output: $(cat "$TMP/out")"
# An UNDELIVERED alert must not open the window, or ntfy being down at the
# moment of the first drift would silence the next half hour.
rm -rf "$TMP/alertstate"; : >"$ST/curl.log"
restart_tailscaled; sut check DNS_CONSOLE_ALERT_REPEAT_SEC=600 FAKE_CURL_MODE=fail >/dev/null
restart_tailscaled; sut check DNS_CONSOLE_ALERT_REPEAT_SEC=600 >/dev/null
check "F19 after an undelivered alert the next drift is still sent" "$(wc -l <"$ST/curl.log" | tr -d ' ')" 2

# ── F20 ─────────────────────────────────────────────────────────────────────
seed
pids=()
for i in 1 2 3 4 5 6; do
    case $((i % 3)) in 0) m=install ;; 1) m=check ;; 2) m=after-tailscaled ;; esac
    ( env PATH="$BIN:$PATH" FAKE_IPT_STATE="$ST" DNS_CONSOLE_ENV_FILE="$ENVF" DNS_CONSOLE_LOCK="$TMP/lock" \
          DNS_CONSOLE_STATE_DIR="$TMP/alertstate" DNS_CONSOLE_PROC_IPV6="$PROCV6" DNS_CONSOLE_WAIT_SEC=3 \
          DNS_CONSOLE_POLL_SEC=1 DNS_CONSOLE_V6_GRACE_SEC=1 DNS_CONSOLE_SETTLE_SEC=1 DNS_CONSOLE_ALERT_TIMEOUT=1 \
          bash "$SUT" "$m" >"$TMP/par$i.out" 2>&1 ) &
    pids+=($!)
    [ "$i" = 3 ] && restart_tailscaled
done
for p in "${pids[@]}"; do wait "$p" 2>/dev/null; done
check "F20 after concurrent runs: v4 exactly one marked rule" "$(marked iptables)" 1
check "F20 after concurrent runs: v6 exactly one marked rule" "$(marked ip6tables)" 1
rc="$(sut status)"
check "F20 ...and in place" "$rc" 0

# ── F21 ─────────────────────────────────────────────────────────────────────
seed
sed -i 's/^-A INPUT -j ts-input$/-A INPUT ! -s 172.28.92.0\/24 ! -i lo -p tcp -m tcp --dport 5380 -m comment --comment "homehub-dns-console" -j REJECT --reject-with tcp-reset\n-A INPUT -j ts-input/' "$ST/iptables.rules"
rc="$(sut install)"
check "F21 install with a quoted-comment rule present exits 0" "$rc" 0
check "F21 the quoted one was purged, one rule remains" "$(tokens iptables)" "$FENCED4"

# ── L1 ──────────────────────────────────────────────────────────────────────
# llm-isolation.sh dies if an ACCEPT without its marker sits above its deny.
# Run ITS awk program - extracted, not copied, so an edit there is tested here -
# over `iptables -n -L INPUT --line-numbers` as iptables 1.8.10 prints it
# (captured from a real netns; `prot` is numeric in that version).
echo "── L: the LLM fence's read-back, against this fence ──"
if [ -f "$LLM" ]; then
    prog="$(grep -F '$2=="ACCEPT" && index($0,m)==0' "$LLM" | sed -n "s/^[^']*'\\(.*\\)')\"\$/\\1/p" | head -1)"
    if [ -z "$prog" ]; then
        fail "L1 could not extract the other_accept program from llm-isolation.sh - its shape changed; re-point this test"
    else
        H='num  target     prot opt source               destination'
        LLMR='3    ACCEPT     0    --  172.28.91.10         0.0.0.0/0            ctstate RELATED,ESTABLISHED /* homehub-llm-isolation host-established */
4    ACCEPT     6    --  172.28.91.10         172.28.91.1          tcp dpt:8799 /* homehub-llm-isolation host-allow-wake */
5    REJECT     0    --  172.28.91.10         0.0.0.0/0            /* homehub-llm-isolation host-deny */ reject-with icmp-port-unreachable'
        now="$(printf 'Chain INPUT (policy ACCEPT)\n%s\n%s\n%s\n%s\n' "$H" \
            '1    REJECT     6    -- !172.28.92.0/24       0.0.0.0/0            tcp dpt:5380 /* homehub-dns-console */ reject-with tcp-reset' \
            '2    ts-input   0    --  0.0.0.0/0            0.0.0.0/0' "$LLMR" \
            | awk -v d=5 -v m=homehub-llm-isolation "$prog")"
        check "L1 llm-isolation's check is silent with this fence above ts-input" "$now" ""
        old="$(printf 'Chain INPUT (policy ACCEPT)\n%s\n%s\n%s\n%s\n' "$H" \
            '1    ACCEPT     6    --  0.0.0.0/0            0.0.0.0/0            tcp dpt:5380 /* homehub-dns-console */' \
            '2    ts-input   0    --  0.0.0.0/0            0.0.0.0/0' "$LLMR" \
            | awk -v d=5 -v m=homehub-llm-isolation "$prog")"
        [ -n "$old" ] && pass "L1 ...and fires on the old ACCEPT design (control: the check is live)" \
                      || fail "L1 the extracted check did not fire on an ACCEPT above the deny - it is not the check we think"
    fi
else
    skip "L1 no llm-isolation.sh beside this stack"
fi

# ── part 2: real netfilter in a throwaway namespace ─────────────────────────
echo "── part 2: real iptables in an unprivileged network namespace ──"
REALPATH="$RBIN:${FENCE_TEST_REAL_BIN:+$FENCE_TEST_REAL_BIN:}$PATH:/usr/sbin:/sbin"
real_ok=0
if command -v unshare >/dev/null 2>&1 \
   && env PATH="$REALPATH" unshare -rn sh -c 'iptables -w 5 -S INPUT && ip6tables -w 5 -S INPUT' >/dev/null 2>&1; then
    real_ok=1
fi
if [ "$real_ok" != 1 ]; then
    skip "R1-R8 need real iptables/ip6tables and unprivileged user namespaces (unshare -rn); not available here"
else
    cat >"$TMP/real-inner.sh" <<'INNER'
# Runs as uid 0 of a fresh user+network namespace. Everything here dies with it.
P() { printf 'PASS  %s\n' "$*"; }
F() { printf 'FAIL  %s\n' "$*"; }
eq() { if [ "$2" = "$3" ]; then P "$1"; else F "$1: got [$2], want [$3]"; fi; }
RD="-p tcp -m multiport --dports 21115,21116,21117,21118,21119 -m comment --comment homehub-rustdesk-isolation"
seed() {
    local f
    for f in iptables ip6tables; do
        $f -w 5 -F INPUT; $f -w 5 -N ts-input 2>/dev/null; $f -w 5 -F ts-input
        $f -w 5 -A ts-input -i tailscale0 -j ACCEPT
    done
    iptables -w 5 -A INPUT -s 172.28.90.0/24 -m comment --comment homehub-game-isolation -j REJECT --reject-with icmp-port-unreachable
    iptables -w 5 -A INPUT -j ts-input
    iptables -w 5 -A INPUT -i lo $RD -j ACCEPT
    iptables -w 5 -A INPUT -s 192.0.2.0/24 $RD -j ACCEPT
    iptables -w 5 -A INPUT $RD -j REJECT --reject-with icmp-port-unreachable
    ip6tables -w 5 -A INPUT -j ts-input
    ip6tables -w 5 -A INPUT -i lo $RD -j ACCEPT
    ip6tables -w 5 -A INPUT $RD -j REJECT --reject-with icmp6-port-unreachable
}
listing() { "$1" -w 5 -S INPUT | grep '^-A'; }
tok() { listing "$1" | awk '{ t = "?" }
    /homehub-game-isolation/ { t = "game" } /^-A INPUT -j ts-input$/ { t = "ts" }
    /homehub-llm-isolation/ { t = "llm" } /homehub-rustdesk-isolation/ { t = "rd" }
    /homehub-dns-console/ { t = "dns" }
    { printf "%s%s", (NR > 1 ? " " : ""), t } END { print "" }'; }
run() { env DNS_CONSOLE_ENV_FILE="$ENVF" DNS_CONSOLE_LOCK="$W/lock" DNS_CONSOLE_STATE_DIR="$W/as" \
        DNS_CONSOLE_PROC_IPV6="$W/v6" DNS_CONSOLE_WAIT_SEC=2 DNS_CONSOLE_POLL_SEC=1 DNS_CONSOLE_SETTLE_SEC=0 \
        DNS_CONSOLE_ALERT_TIMEOUT=1 DNS_CONSOLE_ALERT_REPEAT_SEC=0 "$@" >"$W/out" 2>&1; echo $?; }
restart_ts() { local f; for f in iptables ip6tables; do $f -w 5 -D INPUT -j ts-input; $f -w 5 -I INPUT 1 -j ts-input; done; }
mkdir -p "$W/v6"

seed
eq "R1 install on real netfilter exits 0" "$(run bash "$SUT" install)" 0
eq "R1 v4 lands directly above ts-input, below the game REJECT" "$(tok iptables)" "game dns ts rd rd rd"
eq "R1 v6 lands directly above ts-input" "$(tok ip6tables)" "dns ts rd rd"
eq "R2 real iptables -S prints the v4 rule exactly as the parser expects" "$(listing iptables | grep homehub-dns-console)" "$V4"
eq "R2 real ip6tables -S prints the v6 rule exactly as the parser expects" "$(listing ip6tables | grep homehub-dns-console)" "$V6"
eq "R2 status agrees on real netfilter" "$(run bash "$SUT" status)" 0
restart_ts
eq "R3 a real jump re-insert is drift" "$(run bash "$SUT" status)" 3
eq "R3 check repairs it" "$(run bash "$SUT" check)" 0
eq "R3 ...directly above the jump again" "$(tok iptables)" "dns ts game rd rd rd"
restart_ts
eq "R3 the hook repairs it too" "$(run bash "$SUT" after-tailscaled)" 0
eq "R3 ...v6 as well" "$(tok ip6tables)" "dns ts rd rd"
run bash "$SUT" install >/dev/null
eq "R4 replay on real netfilter: one v4 rule" "$(listing iptables | grep -c homehub-dns-console)" 1
eq "R4 replay on real netfilter: one v6 rule" "$(listing ip6tables | grep -c homehub-dns-console)" 1
printf 'ADMIN_NET_SUBNET=172.28.93.0/24\n' >>"$ENVF"; run bash "$SUT" install >/dev/null
sed -i '/ADMIN_NET_SUBNET/d' "$ENVF"
eq "R5 a subnet change converges on real netfilter" "$(run bash "$SUT" check)" 0
eq "R5 ...leaving no rule for the old subnet" "$(listing iptables | grep -c 172.28.93.0 || true)" 0
eq "R6 remove on real netfilter exits 0" "$(run bash "$SUT" remove)" 0
# (the simulated restarts in R3 left Tailscale's jump first, above the game rule)
eq "R6 ...and leaves every other rule exactly where it was" "$(tok iptables) / $(tok ip6tables)" "ts game rd rd rd / ts rd rd"

# R7/R8: the REAL llm-isolation.sh, with this fence above ts-input.
if [ -f "$LLM" ]; then
    mkdir -p "$W/llm"; : >"$W/llm/docker-compose.yml"
    printf 'LITELLM_CONTAINER_IP=172.28.91.10\nDEVPC_HOST=192.0.2.50\nDEVPC_INFERENCE_PORT=8080\nDEVPC_WAKE_PORT=8799\nDEVPC_WAKE_URL=\n' >"$W/llm/.env"
    seed; run bash "$SUT" install >/dev/null
    rc="$(ENV_FILE="$W/llm/.env" bash "$LLM" >"$W/llm.out" 2>&1; echo $?)"
    eq "R7 the real llm-isolation.sh runs clean with this fence above ts-input" "$rc" 0
    [ "$rc" = 0 ] || sed 's/^/      /' "$W/llm.out"
    rc="$(ENV_FILE="$W/llm/.env" bash "$LLM" >"$W/llm.out" 2>&1; echo $?)"
    eq "R7 ...and on a replay" "$rc" 0
    eq "R7 the fence is still in place after it" "$(run bash "$SUT" status)" 0
    # Control: the plan's original ACCEPT/ACCEPT/REJECT above ts-input.
    seed; run bash "$SUT" remove >/dev/null
    O="-p tcp -m tcp --dport 5380 -m comment --comment homehub-dns-console"
    iptables -w 5 -I INPUT 2 $O -j REJECT --reject-with tcp-reset
    iptables -w 5 -I INPUT 2 -s 172.28.92.0/24 $O -j ACCEPT
    iptables -w 5 -I INPUT 2 -i lo $O -j ACCEPT
    rc="$(ENV_FILE="$W/llm/.env" bash "$LLM" >"$W/llm.out" 2>&1; echo $?)"
    if [ "$rc" != 0 ] && grep -q 'an ACCEPT rule precedes this fence' "$W/llm.out"; then
        P "R8 control: the old ACCEPT design makes the real llm-isolation.sh die, as the review said"
    else
        F "R8 control: expected llm-isolation.sh to die on the old design (exit $rc): $(tail -3 "$W/llm.out")"
    fi
else
    P "R7-R8 skipped: no llm-isolation.sh beside this stack"
fi
INNER
    env PATH="$REALPATH" SUT="$SUT" LLM="$LLM" W="$TMP/real" ENVF="$TMP/real/stack.env" V4="$V4" V6="$V6" \
        FAKE_IPT_STATE="$TMP/real" \
        bash -c 'mkdir -p "$W" && printf "LAN_IP=192.0.2.10\nNTFY_PORT=8090\n" >"$ENVF" && exec unshare -rn bash "$0"' \
        "$TMP/real-inner.sh" >"$TMP/real.out" 2>&1
    rc=$?
    grep -E '^(PASS|FAIL|      )' "$TMP/real.out"
    PASS=$((PASS + $(grep -c '^PASS' "$TMP/real.out"))); FAIL=$((FAIL + $(grep -c '^FAIL' "$TMP/real.out")))
    if [ "$rc" -ne 0 ] || ! grep -q '^PASS  R6' "$TMP/real.out"; then
        fail "R the namespace run did not complete (exit $rc): $(tail -5 "$TMP/real.out")"
    fi
fi

echo
echo "──────────────────────────────────────────────────────────────"
printf '%s PASS  %s FAIL  (%s skipped)\n' "$PASS" "$FAIL" "$SKIP"
if [ "$FAIL" -eq 0 ]; then exit 0; fi
exit 1
