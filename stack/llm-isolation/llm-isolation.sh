#!/usr/bin/env bash
# llm-isolation.sh — stop the private LLM lane reaching anything but the dev PC.
#
# Implements: SR-047, LLR-991
#
# ─────────────────────────────────────────────────────────────────────────────
# WHY THIS FILE EXISTS: THE PROPERTY IT DEFENDS WAS CLAIMED AND WAS NOT TRUE.
# ─────────────────────────────────────────────────────────────────────────────
#
# stack/litellm/ is the lane a finance job uses when it must reach the local
# model or fail. Its claim was: "it holds no cloud credential and names no
# cloud endpoint, so it CANNOT reach a cloud provider". The first half is true.
# The conclusion was false, and an adversarial review (codex gpt-5.6-terra,
# 2026-09-19) found it.
#
# MEASURED against the pinned image sha256:d295634e…, not inferred from docs:
# the proxy registers THIRTY-TWO provider pass-through routes independently of
# `model_list` —
#
#     /vertex_ai/{endpoint}   /gemini/{endpoint}    /anthropic/{endpoint}
#     /bedrock/{endpoint}     /cohere/{endpoint}    /mistral/{endpoint}
#     /openai/{endpoint}      /azure/{endpoint}     /vllm/{endpoint}   …
#
# — and the vertex handler explicitly forwards CALLER-SUPPLIED credentials when
# the proxy has none of its own. Its own log line says so:
#
#     "default_vertex_config not set, forwarding caller-provided headers"
#
# A request was sent to the running container with a caller-supplied
# `x-goog-api-key`, and it tried to open a TLS connection to
# `aiplatform.l.rep.googleapis.com:443`. The one-deployment `model_list`
# constrained none of it. The only reason nothing left the machine was that the
# test host could not resolve the name.
#
# So "holds no credential" is NOT a topology boundary, because the credential
# can arrive in the request. The boundary has to be BELOW the application, at
# the point where packets leave. That is this file.
#
# ─────────────────────────────────────────────────────────────────────────────
# WHY `DOCKER-USER` AND NOT `INPUT` — the OPPOSITE choice to rustdesk-isolation
# and game-isolation next door, and it is worth being explicit because those
# two banners argue at length for INPUT.
#
# They fence traffic addressed to the HOST'S OWN bridge address, which is
# delivered locally and never traverses FORWARD — that is INPUT, and
# DOCKER-USER never sees it. THIS fence is the mirror image: it restricts where
# a container may send packets OUT to, which is forwarded traffic, and
# DOCKER-USER is precisely the chain docker guarantees is consulted for it.
# Putting these rules in INPUT would leave them looking healthy while blocking
# nothing at all.
#
# MATCHED ON A PINNED SOURCE ADDRESS, not a subnet. docker-compose.yml gives
# the container a static `ipv4_address` on a network whose subnet is pinned, so
# these rules can name exactly one source. A subnet match would also cover any
# future neighbour on that network, silently widening or narrowing as the
# compose file changes.
#
# SO THIS SCRIPT PROGRAMS BOTH CHAINS, AND THE SECOND HALF WAS MISSING.
#
# An earlier version of this file wrote the DOCKER-USER half, then said the
# host-directed half was "belt-and-braces documentation rather than the
# control" because "the control for host-directed traffic is
# game-isolation.sh's INPUT fence". THAT WAS WRONG AND A SECOND REVIEW ROUND
# caught it: game-isolation's INPUT rule covers `172.28.90.0/24` — the game
# network — and says nothing about `172.28.91.x`. Measured on the live box, the
# INPUT chain held a REJECT for the game subnet and NOTHING for this one.
#
# That gap mattered more than it looks, for the reason the game fence's own
# banner spells out: `172.x.0.1:5380` is Technitium's admin console, reachable
# straight past both of its guards, and it can rewrite every name the household
# resolves. A private lane that can reprogram DNS is not a private lane. SSH
# and cockpit were equally reachable.
#
# WHAT IS DELIBERATELY STILL ALLOWED, and why each one is not a hole:
#   * ESTABLISHED,RELATED — so the proxy can ANSWER llm-gateway and the finance
#     job. Without it the fence would block the replies to inbound calls and
#     the lane would be dead rather than private.
#   * The dev PC, one address, one TCP port (FORWARD/DOCKER-USER).
#   * The host bridge address on the devpc-wake port ONLY (INPUT), so the hold
#     hook can ask for readiness. Everything else host-directed is rejected.
#
# THE INPUT RULES ARE INSERTED AFTER `ts-input`, NOT APPENDED — the opposite of
# the rustdesk fence, and for a reason measured on the live chain rather than
# assumed. INPUT already holds `-p tcp --dports 21115:21119 -j ACCEPT` from
# ANY source for the remote-desktop relay. An appended REJECT sits below that
# ACCEPT and would never be reached for those ports. Inserting just below
# `ts-input` keeps the tunnel admission the rustdesk banner warns about intact
# while putting this fence above every blanket ACCEPT. The position is FOUND,
# not hard-coded, and the resulting order is verified below.
#
# WHAT IS KNOWINGLY NOT CLOSED: DNS. The container resolves through docker's
# embedded resolver at 127.0.0.11, which forwards from the HOST, so names still
# resolve even though connections are refused. A determined exfiltrator could
# encode data in lookups. That is out of scope for the stated threat model —
# accidental egress, and a future maintainer breaking the property — and is
# recorded here so the next reader knows it was considered rather than missed.
#
# Idempotent by marker comment, like its two siblings: a run removes its own
# previous rules before installing the current set, so a replay after a reboot,
# a firewall reload or a compose recreate converges instead of accumulating.
#
# TWO LIMITS, STATED RATHER THAN PAPERED OVER (both raised in review):
#
#   * If `ts-input` is ABSENT - Tailscale not installed, or its chain not yet
#     programmed - the insertion index falls back to 1. That is safe for this
#     fence (it simply goes first) but it no longer satisfies the "below the
#     tunnel jump" property, because there is no jump to be below. Nothing
#     here can distinguish "Tailscale is gone" from "Tailscale has not started
#     yet", so it does not try.
#   * The read-back proves the chain AT INSTALL TIME. A privileged rule
#     inserted above this fence afterwards bypasses it, and detecting that
#     would need continuous firewall ownership this stack does not have. The
#     mitigation is that this unit re-runs on every boot and converges.
#
# FAILS CLOSED. Non-zero exit here means homehub-litellm.service, which
# `Requires=` the unit that runs this, does not start the container. A private
# lane with no fence must not exist.
set -euo pipefail

MARKER="homehub-llm-isolation"
ENV_FILE="${ENV_FILE:-/opt/homehub/stack/.env}"

log() { echo "llm-isolation: $*"; }
die() { echo "llm-isolation: ERROR: $*" >&2; exit 1; }

_env() {
    # \015 so a CRLF-saved .env cannot smuggle a carriage return into a value
    # that then becomes part of an iptables argument.
    # No `head -1`, for the reason the parser banner below gives at length: a
    # consumer that closes the pipe early can SIGPIPE its producer, and under
    # `set -o pipefail` that becomes the assignment's status and ends the
    # script. .env is small enough that it would not happen today, which is
    # exactly the kind of margin that is invisible when it stops being true.
    # Take the FIRST match and read to the end.
    awk -v k="$1=" 'index($0, k) == 1 && !s { print substr($0, length(k) + 1); s = 1 }' \
        "$ENV_FILE" 2>/dev/null | tr -d '\015' | tr -d '"'
}

[ -r "$ENV_FILE" ] || die "cannot read $ENV_FILE"

LITELLM_IP="$(_env LITELLM_CONTAINER_IP)"
DEVPC_HOST="$(_env DEVPC_HOST)"
DEVPC_PORT="$(_env DEVPC_INFERENCE_PORT)"
WAKE_PORT="$(_env DEVPC_WAKE_PORT)"
: "${WAKE_PORT:=8799}"

# REFUSE TO PROGRAM A PARTIAL FENCE. A missing value must not silently produce
# a rule set that allows more than intended - which is what a bare `-d ` would
# do if it were accepted at all.
[ -n "$LITELLM_IP" ]  || die "LITELLM_CONTAINER_IP is empty - refusing to fence nothing"
[ -n "$DEVPC_HOST" ]  || die "DEVPC_HOST is empty - refusing to program an allow rule with no destination"
[ -n "$DEVPC_PORT" ]  || die "DEVPC_INFERENCE_PORT is empty"

# ADDRESSES ONLY. `iptables -d` takes a name and resolves it ONCE, at program
# time, which bakes whatever DNS said at boot into a rule that then looks
# authoritative for ever. The rustdesk fence learned this as a review finding
# about `netsh remoteip`; the same class of mistake applies here.
case "$DEVPC_HOST" in
    *[!0-9.]*) die "DEVPC_HOST='$DEVPC_HOST' is not a literal IPv4 address. A name is resolved once at program time and the rule then outlives the answer." ;;
esac
case "$LITELLM_IP" in
    *[!0-9.]*) die "LITELLM_CONTAINER_IP='$LITELLM_IP' is not a literal IPv4 address" ;;
esac

command -v iptables >/dev/null 2>&1 || die "iptables not found"

# The chain must exist before we can append to it. Docker creates DOCKER-USER
# itself, but this unit runs BEFORE docker on purpose (see the unit file), so on
# a cold boot it may not be there yet.
if ! iptables -n -L DOCKER-USER >/dev/null 2>&1; then
    iptables -N DOCKER-USER 2>/dev/null || true
fi

# ─────────────────────────────────────────────────────────────────────────────
# WHY NO PARSER BELOW USES `exit` OR `head` — SIGPIPE, AND IT IS NOT THEORETICAL
# ─────────────────────────────────────────────────────────────────────────────
#
# `iptables -n -L INPUT --line-numbers | awk '$2=="ts-input"{print $1; exit}'`
# reads correctly and is a latent, SILENT killer of this script.
#
# When awk exits at the match, it closes the pipe. If iptables has not finished
# writing, it takes SIGPIPE and dies with 141; `set -o pipefail` adopts that as
# the pipeline's status and `set -e` ends the script. Exit 141, no message -
# the same signature as round 6's deploy blocker, and reached on a chain that
# is entirely healthy.
#
# THE TRIGGER IS THIS BOX'S ACTUAL SHAPE. The danger is not a long chain, it is
# a match EARLY with a large tail after it, because that is when awk exits with
# iptables still writing. Measured on the hub 2026-09-19: `ts-input` is INPUT
# position 1 and every other rule is below it. Reproduced here - with the match
# first, the pipeline survives 20 trailing rules and dies with 141 at 2000,
# which is simply where the output passes the 64 KiB pipe buffer.
#
# Today the hub has about twenty INPUT rules, so there is margin. The margin is
# invisible, nobody would think to measure it before adding rules, and the
# failure is a silent non-zero that stops the lane.
#
# `|| true` IS THE WRONG FIX HERE and is the reason this needs a banner rather
# than a one-line change: elsewhere in this file `|| true` guards a `grep` that
# legitimately matches nothing, but on these parsers it would swallow a genuine
# failure to read the chain and let the script proceed on an empty answer.
#
# So every parser CONSUMES ITS WHOLE INPUT and keeps only the first match, via
# a `!seen` flag. Slightly more awk, no early close, no signal.
#
# First matching rule's line number, or empty. `pat` is a literal substring,
# matching what `grep -F` did here before.
_first_pos() {
    iptables -n -L "$1" --line-numbers 2>/dev/null \
        | awk -v pat="$2" 'index($0, pat) && !s {print $1; s=1}'
}

# THE JUMP IS CHECKED UNCONDITIONALLY, NOT ONLY WHEN WE CREATE THE CHAIN.
#
# The first version only added `FORWARD -j DOCKER-USER` inside the branch that
# creates the chain. So if DOCKER-USER already existed but was DETACHED - the
# jump removed by a firewall reload, a `iptables -F FORWARD`, or another tool -
# every rule and order check below would pass against a chain nothing consults,
# and container egress would bypass the fence entirely while this script
# reported success. Found in review; it is the same shape as a bind mount whose
# inode has been replaced.
iptables -C FORWARD -j DOCKER-USER 2>/dev/null \
    || iptables -I FORWARD 1 -j DOCKER-USER

# AND NOTHING MAY MATCH BEFORE IT. A jump that exists but sits below an earlier
# ACCEPT in FORWARD is a jump that is never reached for that traffic - the same
# defect as an appended INPUT rule, one chain over.
_fwd_jump="$(iptables -n -L FORWARD --line-numbers 2>/dev/null \
             | awk '$2=="DOCKER-USER" && !s {print $1; s=1}')"
[ -n "$_fwd_jump" ] || die "FORWARD has no DOCKER-USER jump even after adding one"
# `|| true` for the same reason as the INPUT check further down: finding no
# earlier ACCEPT is the GOOD case, and an unguarded pipeline makes the good
# case fatal under `set -euo pipefail`.
_fwd_before="$(iptables -n -L FORWARD --line-numbers 2>/dev/null \
    | awk -v j="$_fwd_jump" \
          '$1+0>0 && $1+0<j && ($2=="ACCEPT"||$2=="RETURN") && n<3 {print; n++}')"
if [ -n "$_fwd_before" ]; then
    die "an ACCEPT/RETURN precedes the DOCKER-USER jump in FORWARD, so this fence
is not reached for that traffic:
$_fwd_before"
fi

# -- CLOSE THE REAPPLY WINDOW BEFORE TOUCHING ANYTHING ----------------------
#
# A rebuild is "delete the old rules, insert the new ones", and between those
# two the lane is UNFENCED. On the boot path that window is unreachable - this
# unit is Before=docker.service, so there is no container running to use it -
# but a reapply by hand (`systemctl restart homehub-llm-isolation`) happens
# with the lane live, and that is exactly when someone is most likely to run
# it. A request in flight would traverse docker's normal RETURN and leave.
# Found in review.
#
# So a blanket REJECT for this source goes in FIRST, at the head of both
# chains, and comes out LAST, after the read-back has proved the real rules
# are in place. Every intermediate state is therefore MORE restrictive than
# both the old and the new rule set, never less.
#
# It carries its own marker so the rebuild below can skip it, and so a stray
# one left by a run that died mid-way is cleaned up by the next run rather
# than accumulating. Dying with it still installed is the safe direction: the
# lane is fully denied and the unit has failed, so homehub-litellm.service
# does not start.
GUARD="$MARKER reapply-guard"

# Bottom-up, and BEFORE we add this run's guard, so a stray from a previous
# crashed run is removed rather than left underneath.
_drop_guards() {
    local chain
    for chain in DOCKER-USER INPUT; do
        while read -r num; do
            [ -n "$num" ] && iptables -D "$chain" "$num" 2>/dev/null || true
        done < <(iptables -n -L "$chain" --line-numbers 2>/dev/null \
                 | grep -F "$GUARD" | awk '{print $1}' | sort -rn)
    done
}
_drop_guards

TS_POS="$(iptables -n -L INPUT --line-numbers 2>/dev/null | awk '$2=="ts-input" && !s {print $1; s=1}')"
iptables -I DOCKER-USER 1 -s "$LITELLM_IP" -m comment --comment "$GUARD" -j REJECT --reject-with icmp-admin-prohibited
iptables -I INPUT "$(( ${TS_POS:-0} + 1 ))" -s "$LITELLM_IP" -m comment --comment "$GUARD" -j REJECT --reject-with icmp-port-unreachable

# -- remove our own previous rules, by marker ------------------------------
# Iterate by rule number from the bottom so deleting one does not renumber the
# ones still to be examined.
#
# `grep -vF "$GUARD"` so the rebuild does not delete the guard it just
# installed - which would reopen the very window the guard exists to close.
# `|| true` because matching nothing is the ordinary first-run case and an
# unguarded pipeline makes it fatal under `set -euo pipefail`.
while read -r num; do
    [ -n "$num" ] && iptables -D DOCKER-USER "$num" 2>/dev/null || true
done < <(iptables -n -L DOCKER-USER --line-numbers 2>/dev/null \
         | grep -F "$MARKER" | grep -vF "$GUARD" | awk '{print $1}' | sort -rn || true)

# -- install, in order ------------------------------------------------------
# INSERTED AT THE HEAD, NOT APPENDED, and this is the opposite judgement to the
# rustdesk fence. That one appends because an earlier ACCEPT it must not
# override (`ts-input`) sits in INPUT. DOCKER-USER's first rule on this box is
# docker's own RETURN, which sends every packet back to FORWARD - so anything
# appended AFTER it is never reached. A fence that is never reached is the
# failure mode this whole file exists to correct, so it goes at position 1 and
# the verification below proves it is actually there.
iptables -I DOCKER-USER 1 -s "$LITELLM_IP" -m comment --comment "$MARKER deny-all" -j REJECT --reject-with icmp-admin-prohibited
iptables -I DOCKER-USER 1 -s "$LITELLM_IP" -d "$DEVPC_HOST" -p tcp --dport "$DEVPC_PORT" -m comment --comment "$MARKER allow-devpc" -j ACCEPT
iptables -I DOCKER-USER 1 -s "$LITELLM_IP" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$MARKER allow-established" -j ACCEPT

# -- INPUT: host-directed traffic -------------------------------------------
# DOCKER-USER never sees these packets. See the banner: the host bridge address
# is delivered locally, and leaving it open let this container reach SSH, the
# DNS admin console and cockpit.
#
# The bridge gateway is the .1 of the PINNED subnet, which is why pinning it
# was worth doing: the address the container talks to when it says "the host"
# is then a constant a rule can name, rather than whatever docker allocated.
BRIDGE_GW="$(printf '%s' "$LITELLM_IP" | sed 's/\.[0-9]*$/.1/')"

# Remove our own previous INPUT rules first, bottom-up so deletion does not
# renumber what is still to be examined.
# `grep -vF "$GUARD"` for the same reason as the DOCKER-USER loop above: the
# reapply guard must survive the rebuild and is removed only after read-back.
while read -r num; do
    [ -n "$num" ] && iptables -D INPUT "$num" 2>/dev/null || true
done < <(iptables -n -L INPUT --line-numbers 2>/dev/null \
         | grep -F "$MARKER" | grep -vF "$GUARD" | awk '{print $1}' | sort -rn || true)

# FIND the insertion point rather than hard-coding one. `ts-input` is
# Tailscale's jump and must stay first - inserting above it would put this
# fence ahead of the `-i tailscale0 -j ACCEPT` that admits the tunnel at all.
#
# COLUMN 2 IS THE TARGET, AND THAT DEPENDS ON THE ABSENCE OF `-v`.
# `iptables -n -L --line-numbers` prints `num target prot opt source dest`.
# Adding `-v` inserts `pkts bytes` and shifts the target to column 4. A review
# round asserted the target was already column 4 and proposed "fixing" it;
# measured on this box, `$2=="ts-input"` finds it at position 1 and
# `$4=="ACCEPT"` matches NOTHING, so that change would have silently disabled
# both runtime checks below. Kept at $2, and _assert_parse() proves the
# assumption every run rather than trusting this comment.
_ipt_list() { iptables -n -L INPUT --line-numbers 2>/dev/null; }

TS_POS="$(_ipt_list | awk '$2=="ts-input" && !s {print $1; s=1}')"
INS=$(( ${TS_POS:-0} + 1 ))

# Inserted in REVERSE order at the same index, so the ACCEPT ends up above the
# REJECT. Writing them in forward order at a fixed index silently inverts them,
# which is a fence that blocks the one thing it means to allow.
iptables -I INPUT "$INS" -s "$LITELLM_IP" -m comment --comment "$MARKER host-deny" -j REJECT --reject-with icmp-port-unreachable
iptables -I INPUT "$INS" -s "$LITELLM_IP" -d "$BRIDGE_GW" -p tcp --dport "$WAKE_PORT" -m comment --comment "$MARKER host-allow-wake" -j ACCEPT
iptables -I INPUT "$INS" -s "$LITELLM_IP" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$MARKER host-established" -j ACCEPT

in_missing=""
iptables -C INPUT -s "$LITELLM_IP" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$MARKER host-established" -j ACCEPT 2>/dev/null || in_missing="$in_missing host-established"
iptables -C INPUT -s "$LITELLM_IP" -d "$BRIDGE_GW" -p tcp --dport "$WAKE_PORT" -m comment --comment "$MARKER host-allow-wake" -j ACCEPT 2>/dev/null || in_missing="$in_missing host-allow-wake"
iptables -C INPUT -s "$LITELLM_IP" -m comment --comment "$MARKER host-deny" -j REJECT --reject-with icmp-port-unreachable 2>/dev/null || in_missing="$in_missing host-deny"
[ -z "$in_missing" ] || die "INPUT rule(s) not present after programming:$in_missing"

i_est="$(_first_pos INPUT "$MARKER host-established")"
i_allow="$(_first_pos INPUT "$MARKER host-allow-wake")"
i_deny="$(_first_pos INPUT "$MARKER host-deny")"
[ "$i_est" -lt "$i_allow" ] && [ "$i_allow" -lt "$i_deny" ] \
    || die "INPUT rules are out of order (est=$i_est allow=$i_allow deny=$i_deny) - the hold hook would be unable to reach the readiness service"

# PROVE THE COLUMN PARSE BEFORE TRUSTING A CHECK BUILT ON IT.
#
# The danger a review round pointed at is real even though its diagnosis was
# not: if the target column ever moves - a future iptables, a `-v` added here
# by someone tidying up - then `$2=="ACCEPT"` silently matches nothing and the
# "no blanket accept above us" check below becomes permanently, invisibly
# empty. A guard that fails closed beats a comment.
#
# We have just inserted two ACCEPT rules of our own, so the parse MUST find at
# least two. If it does not, the format is not what this script believes.
_parsed_accepts="$(_ipt_list | awk '$2=="ACCEPT"' | grep -cF "$MARKER" || true)"
[ "${_parsed_accepts:-0}" -ge 2 ] || die \
    "the INPUT parser found $_parsed_accepts of our own 2 ACCEPT rules, so the
target column is not \$2 on this iptables. Every order check below would have
passed vacuously. Fix the column, do not delete the check."

# AND THE REJECT MUST OUTRANK EVERY BLANKET ACCEPT. This is the check the first
# version did not have and needed: a rule that is correct but sits below
# `-p tcp --dports 21115:21119 -j ACCEPT` protects nothing for those ports.
#
# `|| true` IS LOAD-BEARING, AND ITS ABSENCE WAS A DEPLOY-BLOCKING BUG.
# Under `set -euo pipefail`, `grep -v` finding NOTHING exits 1, pipefail
# propagates that, and `set -e` kills the script - so this fence died SILENTLY
# (exit 1, no output) at precisely the moment it had just proved everything
# was correct, and the lane would never have started. Every source-text test
# passed. The first test that actually EXECUTED the script found it on its
# first run, which is the whole argument for driving the real thing.
other_accept="$(_ipt_list \
    | awk -v d="$i_deny" -v m="$MARKER" \
          '$1+0>0 && $1+0<d && $2=="ACCEPT" && index($0,m)==0 && n<3 {print; n++}')"
if [ -n "$other_accept" ]; then
    die "an ACCEPT rule precedes this fence in INPUT, so host-directed traffic from $LITELLM_IP can bypass it:
$other_accept"
fi

# -- IPv6 -------------------------------------------------------------------
# THIS SECTION USED TO SAY "deny outright" AND THEN NOT DENY ANYTHING. It
# removed its own old rules, logged, and installed nothing - a heading that
# claimed a control the code did not implement, which is worse than no heading
# (found in review). What follows is what it actually does and why that is
# enough here.
#
# The `llmprivate` network declares no IPv6 subnet and does not enable IPv6, so
# the container is assigned no v6 address and has no v6 route. There is no
# source address a v6 rule could name, and a blanket v6 REJECT on the bridge
# would be a rule matching traffic that cannot exist.
#
# So rather than program a decorative rule, VERIFY THE PREMISE. If the network
# ever gains IPv6, this exits non-zero and the lane does not start - which is
# the fence working, not an obstacle. The rustdesk fence's "the IPv6 half is
# not optional" lesson is honoured by checking, not by pretending.
if command -v ip6tables >/dev/null 2>&1; then
    while read -r num; do
        [ -n "$num" ] && ip6tables -D DOCKER-USER "$num" 2>/dev/null || true
    done < <(ip6tables -n -L DOCKER-USER --line-numbers 2>/dev/null \
             | grep -F "$MARKER" | awk '{print $1}' | sort -rn)
fi

# VERIFIED AGAINST THE COMPOSE FILE, NOT AGAINST THE LIVE NETWORK.
#
# The obvious check - `docker network inspect --format '{{.EnableIPv6}}'` - is
# WORTHLESS HERE and was shipped anyway for one round. This unit is
# deliberately `Before=docker.service`, so on the boot path docker is not
# running when it executes: the inspect fails, the result is "unknown", and the
# branch that was supposed to fail closed logs a shrug instead. It would never
# have caught IPv6 being enabled. Found in review, and it is a good example of
# a check that reads correct and runs vacuously.
#
# The compose file IS available before docker starts, and it is the source of
# truth for whether the network has IPv6 at all. So parse that.
COMPOSE_FILE="${COMPOSE_FILE:-$(dirname "$ENV_FILE")/docker-compose.yml}"
[ -r "$COMPOSE_FILE" ] || die "cannot read $COMPOSE_FILE to verify the lane's address families"

# THE RESOLVED CONFIGURATION, NOT THE RAW TEXT.
#
# The version before this one grepped for the literal lowercase string
# `enable_ipv6: true`, and a third review round showed it FAILS OPEN on every
# other way of writing the same thing:
#
#     enable_ipv6: ${LLM_ENABLE_IPV6}     # env resolves to true
#     enable_ipv6: True                   # YAML boolean, capitalised
#     <<: *some-anchor                    # merged in from elsewhere
#
# In each case docker would give the lane IPv6 while this script logged
# "declares no IPv6" and installed no v6 fence - which is precisely the
# "a future config change silently bypasses the guard" failure the fence
# claims to prevent.
#
# `docker compose config` does the interpolation, anchor merging and YAML
# typing for us. It is CLIENT-SIDE PARSING and does NOT contact the daemon,
# which is what makes it usable from a unit ordered Before=docker.service -
# unlike `docker network inspect`, which is the mistake this replaced.
_compose_dir="$(dirname "$COMPOSE_FILE")"
_v6="$(cd "$_compose_dir" 2>/dev/null && docker compose config --format json 2>/dev/null \
    | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    print("unreadable"); raise SystemExit(0)
net = (doc.get("networks") or {}).get("llmprivate")
if net is None:
    print("absent")
else:
    # Anything that is not an unambiguous false is treated as enabled.
    v = net.get("enable_ipv6", False)
    print("false" if v in (False, "false", "False", None, "") else "true")
' 2>/dev/null)"

case "$_v6" in
    false)
        log "ipv6: llmprivate resolves to enable_ipv6=false (compose config, pre-docker)"
        ;;
    true)
        die "the llmprivate network resolves to enable_ipv6=true, but this fence
programs IPv4 only. Turn IPv6 off on that network or add the v6 half here - do
not start the lane with one family unfenced."
        ;;
    absent)
        die "no llmprivate network in the resolved compose configuration - refusing
to fence a lane whose network this script cannot see."
        ;;
    *)
        # FAIL CLOSED. The previous version logged "premise UNVERIFIED" and
        # carried on, which is the same as not checking. If the resolved
        # configuration cannot be obtained, the lane does not start.
        die "could not resolve the compose configuration in $_compose_dir to check
the lane's address families (got '${_v6:-<nothing>}'). Refusing to program a
fence whose premise is unverified - re-run once 'docker compose config' works."
        ;;
esac

# -- VERIFY, do not trust the exit codes above ------------------------------
# Every rule is read back. `iptables -I` returning 0 means the command parsed,
# not that the rule is where it needs to be.
missing=""
iptables -C DOCKER-USER -s "$LITELLM_IP" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$MARKER allow-established" -j ACCEPT 2>/dev/null || missing="$missing allow-established"
iptables -C DOCKER-USER -s "$LITELLM_IP" -d "$DEVPC_HOST" -p tcp --dport "$DEVPC_PORT" -m comment --comment "$MARKER allow-devpc" -j ACCEPT 2>/dev/null || missing="$missing allow-devpc"
iptables -C DOCKER-USER -s "$LITELLM_IP" -m comment --comment "$MARKER deny-all" -j REJECT --reject-with icmp-admin-prohibited 2>/dev/null || missing="$missing deny-all"
[ -z "$missing" ] || die "rule(s) not present after programming:$missing"

# AND PROVE THE ORDER. Three correct rules in the wrong order are a fence that
# allows everything: if deny-all precedes allow-devpc, the lane is dead; if
# docker's RETURN precedes all three, the lane is wide open. Read the positions.
# `grep -vF "$GUARD"` so the reapply guard, which is still installed at this
# point and sits below all three, does not appear in the reported positions.
order="$(iptables -n -L DOCKER-USER --line-numbers | grep -F "$MARKER" | grep -vF "$GUARD" | awk '{print $1}' | tr '\n' ' ' || true)"
first_est="$(_first_pos DOCKER-USER "$MARKER allow-established")"
first_dev="$(_first_pos DOCKER-USER "$MARKER allow-devpc")"
first_den="$(_first_pos DOCKER-USER "$MARKER deny-all")"
[ -n "$first_est" ] && [ -n "$first_dev" ] && [ -n "$first_den" ] || die "could not read rule positions"
[ "$first_est" -lt "$first_dev" ] || die "established rule must precede the dev-PC rule (got $order)"
[ "$first_dev" -lt "$first_den" ] || die "dev-PC allow must precede the deny (got $order) - the lane would be dead"

# -- THE EFFECTIVE WAKE URL MUST AGREE WITH WHAT WE JUST ALLOWED ------------
#
# A unit test can only check the EXAMPLE env. The real /opt/homehub/stack/.env
# can say DEVPC_WAKE_URL=http://172.17.0.1:8799 - the default bridge, which is
# where `host-gateway` resolves - while every file-level assertion passes. The
# fence would then deny the readiness probe and the hold would report "no
# oracle" for ever: fail-closed for egress, which is right, but a readiness
# path that silently never works, which is not.
#
# So validate the DEPLOYED value here, where both halves are in hand. Empty is
# the deliberate hold-disabled setting and passes.
# A WHITELIST OF ONE SHAPE, NOT A BEST-EFFORT PARSE.
#
# The first version stripped `^[a-z]*://` and defaulted a missing port to 80,
# which accepted `172.28.91.1:8799` with no scheme, accepted
# `ftp://172.28.91.1:8799`, and - the dangerous one - accepted
# `https://172.28.91.1` as port 80 when WAKE_PORT=80, while the hook would
# actually connect to 443 and be rejected by this very fence. Found in review.
#
# The only accepted form is http://<exact IPv4>:<decimal port> with an optional
# trailing slash. Everything else, including https, a bare host:port, userinfo
# and an IPv6 literal, is refused rather than interpreted - the hook speaks
# plain HTTP to a bridge address and there is no second legitimate shape.
WAKE_URL="$(_env DEVPC_WAKE_URL)"
if [ -n "$WAKE_URL" ]; then
    _want="http://${BRIDGE_GW}:${WAKE_PORT}"
    case "$WAKE_URL" in
        "$_want"|"$_want/") : ;;
        *)
            die "DEVPC_WAKE_URL is '$WAKE_URL'. The only accepted value is
'${_want}' (an optional trailing slash is fine), or empty to disable the hold.
Anything else is refused rather than interpreted: a different host or port
would be rejected by this fence's own INPUT rule and reported for ever as an
unreachable oracle, and an https URL with no port would connect to 443 while a
lenient parser read it as 80."
            ;;
    esac
    log "readiness url ${WAKE_URL} agrees with the host-allow rule"
else
    log "DEVPC_WAKE_URL is empty - the hold is disabled (deliberate while the wake path is unverified)"
fi

# -- THE GUARD COMES OUT LAST, AND ONLY HERE --------------------------------
#
# Everything above this line can still `die`, and every one of those exits
# leaves the blanket REJECT in place: the lane stays fully denied and
# homehub-litellm.service, which Requires= this unit, does not start. That is
# the correct direction to fail. Removing the guard is therefore the LAST act
# of a run that has proved every real rule is present and correctly ordered -
# it is the moment the fence stops being "deny everything" and becomes "deny
# everything except the dev PC".
_drop_guards

# And prove that it went. A guard left behind is not dangerous - it denies -
# but it would silently kill the lane while every check above reported
# success, which is its own kind of lie.
_stray="$(iptables -n -L DOCKER-USER --line-numbers 2>/dev/null | grep -cF "$GUARD" || true)"
_stray6="$(iptables -n -L INPUT --line-numbers 2>/dev/null | grep -cF "$GUARD" || true)"
[ "${_stray:-0}" -eq 0 ] && [ "${_stray6:-0}" -eq 0 ] || die \
    "the reapply guard could not be removed (DOCKER-USER=$_stray INPUT=$_stray6).
The lane is fenced but every packet from $LITELLM_IP is denied, including the
dev PC. Remove the '$GUARD' rules by hand."

log "fenced $LITELLM_IP -> only ${DEVPC_HOST}:${DEVPC_PORT} (positions: $order)"
exit 0
