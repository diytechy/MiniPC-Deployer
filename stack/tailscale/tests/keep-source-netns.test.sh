#!/usr/bin/env bash
# keep-source-netns.test.sh - TC-1860: the keep-source rule against the REAL
# kernel, with tailscaled's and Docker's rules imitated around it.
#
# Hermetic: everything happens inside throwaway namespaces (`unshare -rn` for an
# ordinary user, `unshare -n` for root) and nothing touches the host's own
# firewall or interfaces. It SKIPS where the box forbids unprivileged user
# namespaces or lacks the netfilter pieces (a machine that cannot answer, not a
# failed rule). Run it alone, as an ordinary user, in WSL Ubuntu:
#     bash stack/tailscale/tests/keep-source-netns.test.sh
#
# THE LAB, all in one outer namespace standing in for the hub:
#   client ns   100.101.102.103 on c0  <->  tailscale0 on the hub
#   container   172.31.0.2 on ct0      <->  br-lab0 (172.31.0.1) on the hub
#   lan ns      192.0.2.2 on lan0      <->  eno1 (192.0.2.1) on the hub
# The hub forwards, DNATs 192.0.2.1:8443 to the container (Docker's published
# port), and carries tailscaled's rules as v1.104.5 writes them: ts-forward
# marks everything from tailscale0 with 0x40000/0xff0000, ts-postrouting
# (first in nat POSTROUTING) masquerades every marked packet. The lan ns has no
# route back to the tailnet, like a real LAN device whose gateway is the router.
#
# What it proves:
#   N1 without the rule the container sees the bridge gateway (tonight's fault);
#   N2 the coordinator's hand-made TEST rule fixes it (the live finding, again);
#   N3 apply replaces the TEST rule with exactly one rule of its own, and the
#      container sees the client's own tailnet address;
#   N4 the exit-node / subnet-route path (client -> lan ns) is STILL masqueraded:
#      the lan host sees the hub's LAN address, and the connection completes
#      only because of that;
#   N5 a second apply changes nothing (mangle table byte-identical);
#   N6 status agrees (exit 0, ON as configured);
#   N7 knob false -> apply withdraws it and the container sees the gateway again;
#   N8 remove leaves no rule carrying either comment.
# What it does NOT prove: anything about the hub itself, IPv6 (not covered by
# design; see the script's header), or tailscaled's and Docker's own behaviour
# on restart (read from their source; the script's header cites it).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../tailnet-keep-source.sh"

PASS=0; FAIL=0; SKIP=0
pass() { PASS=$((PASS+1)); printf 'PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL  %s\n' "$*"; }
skip() { SKIP=$((SKIP+1)); printf 'SKIP  %s\n' "$*"; }
check() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1: got [$2], want [$3]"; fi; }
finish() { printf '\n%d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"; [ "$FAIL" -eq 0 ]; exit $?; }

if [ "${1:-}" != --inside ]; then
    for t in unshare nsenter ip iptables python3; do
        command -v "$t" >/dev/null 2>&1 || { skip "TC-1860 needs $t"; finish; }
    done
    if [ "$(id -u)" = 0 ]; then ns=(unshare -n); else ns=(unshare -rn); fi
    if ! "${ns[@]}" sh -c 'iptables -w 5 -t mangle -S POSTROUTING && iptables -w 5 -t nat -S POSTROUTING' >/dev/null 2>&1; then
        skip "TC-1860 needs real iptables in a throwaway network namespace (${ns[*]}); not available here"
        finish
    fi
    exec "${ns[@]}" bash "$0" --inside
fi

# ----------------------------------------------------------------------------
# Inside the outer namespace: we are root here, and only here.
# ----------------------------------------------------------------------------
W="$(mktemp -d /tmp/keep-source-netns.XXXXXX)"
PIDS=()
cleanup() { local p; for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; rm -rf "$W"; }
trap cleanup EXIT

# A fake systemctl so `status` can judge the rule (the unit is not under test).
mkdir -p "$W/bin"
printf '#!/bin/sh\nexit 0\n' >"$W/bin/systemctl"; chmod 0755 "$W/bin/systemctl"
export PATH="$W/bin:$PATH"
export TAILNET_KEEP_SOURCE_ENV_FILE="$W/stack.env" TAILNET_KEEP_SOURCE_LOCK="$W/ks.lock"
printf 'TAILSCALE_ENABLED=true\n' >"$TAILNET_KEEP_SOURCE_ENV_FILE"

# A child network namespace held open by a sleeper; its pid goes in NS. Never
# called through $(...): the sleeper would hold the substitution's pipe open,
# and the caller would wait for it to exit.
newns() { unshare -n sleep 600 </dev/null >/dev/null 2>&1 & PIDS+=("$!"); NS="$!"; }
in_ns() { local p="$1"; shift; nsenter -t "$p" -n "$@"; }

lab() {
    newns; CLIENT="$NS"; newns; CT="$NS"; newns; LAN="$NS"
    sleep 0.3
    ip link set lo up
    echo 1 >/proc/sys/net/ipv4/ip_forward || return 1

    ip link add tailscale0 type veth peer name c0 netns "$CLIENT" || return 1
    ip addr add 100.64.0.1/32 dev tailscale0 && ip link set tailscale0 up
    ip route add 100.101.102.103/32 dev tailscale0
    in_ns "$CLIENT" ip addr add 100.101.102.103/32 dev c0
    in_ns "$CLIENT" ip link set c0 up
    in_ns "$CLIENT" ip route add 100.64.0.1/32 dev c0
    in_ns "$CLIENT" ip route add default via 100.64.0.1 dev c0 onlink

    ip link add br-lab0 type bridge || return 1
    ip addr add 172.31.0.1/24 dev br-lab0 && ip link set br-lab0 up
    ip link add vb0 type veth peer name ct0 netns "$CT"
    ip link set vb0 master br-lab0 && ip link set vb0 up
    in_ns "$CT" ip addr add 172.31.0.2/24 dev ct0
    in_ns "$CT" ip link set ct0 up
    in_ns "$CT" ip route add default via 172.31.0.1

    ip link add eno1 type veth peer name lan0 netns "$LAN"
    ip addr add 192.0.2.1/24 dev eno1 && ip link set eno1 up
    in_ns "$LAN" ip addr add 192.0.2.2/24 dev lan0
    in_ns "$LAN" ip link set lan0 up

    # Docker: the published port and its own masquerade for the container's
    # outbound traffic.
    iptables -w 5 -t nat -A PREROUTING -d 192.0.2.1/32 -p tcp --dport 8443 \
        -j DNAT --to-destination 172.31.0.2:8443 || return 1
    iptables -w 5 -t nat -A POSTROUTING -s 172.31.0.0/24 ! -o br-lab0 -j MASQUERADE || return 1
    # tailscaled, as v1.104.5 writes it (addBase4, AddSNATRule, AddHooks).
    iptables -w 5 -N ts-forward && iptables -w 5 -I FORWARD 1 -j ts-forward || return 1
    iptables -w 5 -A ts-forward -i tailscale0 -j MARK --set-xmark 0x40000/0xff0000 || return 1
    iptables -w 5 -A ts-forward -m mark --mark 0x40000/0xff0000 -j ACCEPT || return 1
    iptables -w 5 -t nat -N ts-postrouting && iptables -w 5 -t nat -I POSTROUTING 1 -j ts-postrouting || return 1
    iptables -w 5 -t nat -A ts-postrouting -m mark --mark 0x40000/0xff0000 -j MASQUERADE || return 1

    # A server that answers each connection with the peer address it saw.
    local srv='import socket,sys
s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", int(sys.argv[1]))); s.listen(8)
while True:
    c, a = s.accept(); c.sendall(a[0].encode()); c.close()'
    in_ns "$CT" python3 -c "$srv" 8443 </dev/null >/dev/null 2>&1 & PIDS+=("$!")
    in_ns "$LAN" python3 -c "$srv" 9000 </dev/null >/dev/null 2>&1 & PIDS+=("$!")
    sleep 0.5
}

# seen HOST PORT -> the address the server saw for a connection from the client.
seen() {
    in_ns "$CLIENT" timeout 5 python3 -c 'import socket,sys
c=socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3); print(c.recv(64).decode())' "$1" "$2" 2>/dev/null \
        || echo "no-connection"
}

if ! lab; then
    skip "TC-1860 could not build the lab here (bridge, veth, forwarding or a netfilter target unavailable in this namespace)"
    finish
fi

count() { iptables -w 5 -t mangle -S POSTROUTING | grep -c -- "--comment $1" || true; }
TEST_RULE=(-s 100.64.0.0/10 -o br-+ -m comment --comment tailnet-to-docker-keep-source-TEST-20261112 -j MARK --set-xmark 0x0/0x40000)

check "N1 no rule: the container sees the bridge gateway (the 2026-11-12 fault)" "$(seen 192.0.2.1 8443)" "172.31.0.1"

iptables -w 5 -t mangle -A POSTROUTING "${TEST_RULE[@]}"
check "N2 the hand-made TEST rule: the container sees the client" "$(seen 192.0.2.1 8443)" "100.101.102.103"

out="$(bash "$SCRIPT" apply 2>&1)"; rc=$?
check "N3a apply exits 0 [$out]" "$rc" "0"
check "N3b the TEST rule is gone" "$(count tailnet-to-docker-keep-source-TEST)" "0"
check "N3c exactly one rule of its own" "$(count homehub-tailnet-keep-source)" "1"
check "N3d the container sees the client's own tailnet address" "$(seen 192.0.2.1 8443)" "100.101.102.103"

check "N4 the exit-node / subnet-route path is still masqueraded" "$(seen 192.0.2.2 9000)" "192.0.2.1"

before="$(iptables -w 5 -t mangle -S)"
bash "$SCRIPT" apply >/dev/null 2>&1
check "N5 a second apply changes nothing" "$(iptables -w 5 -t mangle -S)" "$before"

out="$(bash "$SCRIPT" status 2>&1)"; rc=$?
check "N6a status exits 0 [$out]" "$rc" "0"
case "$out" in *"ON as configured"*) pass "N6b status says ON as configured" ;; *) fail "N6b status: $out" ;; esac

printf 'TAILSCALE_ENABLED=true\nTAILSCALE_KEEP_SOURCE_TO_DOCKER=false\n' >"$TAILNET_KEEP_SOURCE_ENV_FILE"
bash "$SCRIPT" apply >/dev/null 2>&1
check "N7a knob false: no rule of its own" "$(count homehub-tailnet-keep-source)" "0"
check "N7b knob false: the container sees the bridge gateway again" "$(seen 192.0.2.1 8443)" "172.31.0.1"

printf 'TAILSCALE_ENABLED=true\n' >"$TAILNET_KEEP_SOURCE_ENV_FILE"
bash "$SCRIPT" apply >/dev/null 2>&1
iptables -w 5 -t mangle -A POSTROUTING "${TEST_RULE[@]}"
out="$(bash "$SCRIPT" remove 2>&1)"; rc=$?
check "N8a remove exits 0 [$out]" "$rc" "0"
check "N8b remove: no rule carrying either comment" \
    "$(count homehub-tailnet-keep-source)/$(count tailnet-to-docker-keep-source-TEST)" "0/0"

finish
