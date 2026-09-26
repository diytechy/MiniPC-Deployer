#!/usr/bin/env bash
# dns-console-fence.sh — keep Technitium's admin console (tcp/5380) to loopback
# and the pinned admin bridge, in both address families, ABOVE Tailscale's jump.
#
# Implements: ADMIN_PORTAL_PLAN_2026-09-22 §4.5 (findings 5, B1) and D-A5.
#
# WHAT IT FENCES, AND NOTHING ELSE. Technitium is host-networked, so :5380
# answers on every interface: the LAN, every docker bridge, and the tailnet.
# ONE rule per family, marker `homehub-dns-console`:
#
#   v4  ! -s 172.28.92.0/24 ! -i lo -p tcp --dport 5380 -j REJECT tcp-reset
#   v6            ! -i lo -p tcp --dport 5380 -j REJECT tcp-reset
#
# Loopback (provisioning, healthcheck, verify-hub, `ssh -L`) and the pinned
# `admin` network (Caddy is 172.28.92.2) are not matched and fall through the
# rest of INPUT exactly as before; everyone else - tailnet, LLM lane, relay,
# LAN - is reset. The admin network has no v6, so v6 admits loopback only.
#
# ONE NEGATED REJECT, NOT THE PLAN'S ACCEPT/ACCEPT/REJECT, and the reason is
# the fence next door. llm-isolation.sh reads INPUT back and DIES if any
# ACCEPT without its own marker sits above its host-deny rule. This fence sits
# above ts-input, so above the LLM rules; ACCEPTs here would stop the private
# LLM lane from starting whenever its unit ran after this one. A REJECT trips
# nothing. The outcome is the same today; the one difference: if INPUT's policy
# ever became DROP (ufw enabled), loopback and the admin network would no
# longer be accepted HERE - ufw is inactive and the policy is ACCEPT.
#
# WHY ABOVE `ts-input`, when the RustDesk fence argues for below it. ts-input
# holds `-i tailscale0 -j ACCEPT`, so a 5380 rule below it never sees tailnet
# traffic (round-2 finding B1). The RustDesk banner calls moving above it "not
# obviously safe" - for a fence matching whole port ranges. This rule matches
# one TCP port, and only rejects, so nothing ts-input admits is otherwise
# touched.
#
# WHAT tailscaled DOES TO THIS CHAIN - precondition 4, read from the v1.102.4
# source (what the hub runs), not measured:
#   * Its jump goes in at POSITION 1, and only if absent. AddHooks:
#     `ipt.Insert(table, chain, 1, "-j", "ts-input")`
#     https://github.com/tailscale/tailscale/blob/v1.102.4/util/linuxfw/iptables_runner.go#L137-L175
#     (nftables mode inserts at the head too: nftables_runner.go addHookRule.)
#   * EVERY START deletes it first. tailscaled runs its cleanup (DelHooks,
#     DelChains) at startup "even if we're going to run the server", and again
#     as `ExecStopPost=tailscaled --cleanup`:
#     https://github.com/tailscale/tailscale/blob/v1.102.4/cmd/tailscaled/tailscaled.go#L514-L522
#     https://github.com/tailscale/tailscale/blob/v1.102.4/cmd/tailscaled/tailscaled.service
#     So after ANY restart - an auto-update (the .deb postinst runs
#     `deb-systemd-invoke restart tailscaled`), a crash, a reboot - the jump comes
#     back ABOVE this fence. Drift after a restart is certain, not possible.
#   * It comes back LATE. systemd sees READY when the LocalAPI starts listening
#     (ipn/ipnserver/server.go#L529), but the hooks go in from the first router
#     Set() with NetfilterOn, which needs a netmap from control. A unit ordered
#     After=tailscaled.service therefore runs before the jump exists - hence the
#     bounded wait in `after-tailscaled`.
#   * Three things re-add it WITHOUT a restart, and only the timer sees them:
#     `tailscale down`/`up` (and key expiry + re-login), because a stopped
#     backend reconfigures with an empty router.Config whose NetfilterMode is Off
#     (ipn/ipnlocal/local.go#L6786-L6792); a --netfilter-mode change; and a
#     firewall kind pushed by control (router_linux.go Set, #L436-L461).
#   * Nothing re-adds it on a timer. AddHooks' only callers are the mode
#     transitions in setNetfilterModeLocked (router_linux.go#L745-L870).
#   * iptables vs nftables: util/linuxfw/detector.go - TS_DEBUG_FIREWALL_MODE,
#     else a control-pushed hint, else iptables. The hub runs iptables mode.
#
# SO THERE ARE THREE ENTRY POINTS (the units beside this file):
#   install           boot, Before=docker.service; converges unconditionally
#   after-tailscaled  every tailscaled start or restart; waits for the jump,
#                     re-asserts QUIETLY, because drift is expected there
#   check             timer, every 2 min; drift -> re-assert AND an ntfy alert
# In every mode a failure to re-assert alerts and exits 1. `remove` is the
# rollback (plan §8 step 5); `status` is read-only (exit 3 = drift).
#
# IT DOES NOT FAIL CLOSED - decision D-A5. RustDesk's listeners `Requires=`
# their fence; Technitium is the household resolver and must not. A failed
# fence leaves the console reachable behind Technitium's own login, and says so
# through ntfy. Alerts are best-effort by construction: bounded by a timeout,
# and never able to change this script's exit status.
#
# The rules are NOT removed on stop, like the three sibling fences.
#
# Exit: 0 fence in place (after any repair). 1 it is not. 2 usage.
#       3 (status only) drift found, nothing changed.
set -uo pipefail

MARKER="homehub-dns-console"
PORT=5380
DEFAULT_SUBNET="172.28.92.0/24"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${DNS_CONSOLE_ENV_FILE:-$HERE/../.env}"
LOCK_FILE="${DNS_CONSOLE_LOCK:-/run/homehub-dns-console.lock}"
STATE_DIR="${DNS_CONSOLE_STATE_DIR:-/run/homehub-dns-console}"
PROC_IPV6="${DNS_CONSOLE_PROC_IPV6:-/proc/sys/net/ipv6}"

log() { printf '%s [dns-console] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Tunables, for the tests. A non-number falls back rather than becoming a
# zero-second poll loop.
_int() { case "$1" in ''|*[!0-9]*) printf '%s' "$2" ;; *) printf '%s' "$1" ;; esac; }
WAIT_SEC="$(_int "${DNS_CONSOLE_WAIT_SEC:-}" 90)"
POLL_SEC="$(_int "${DNS_CONSOLE_POLL_SEC:-}" 2)"; [ "$POLL_SEC" -ge 1 ] || POLL_SEC=1
V6_GRACE_SEC="$(_int "${DNS_CONSOLE_V6_GRACE_SEC:-}" 10)"
SETTLE_SEC="$(_int "${DNS_CONSOLE_SETTLE_SEC:-}" 10)"
ALERT_TIMEOUT="$(_int "${DNS_CONSOLE_ALERT_TIMEOUT:-}" 6)"; [ "$ALERT_TIMEOUT" -ge 1 ] || ALERT_TIMEOUT=1
ALERT_REPEAT_SEC="$(_int "${DNS_CONSOLE_ALERT_REPEAT_SEC:-}" 1800)"

# One key from the stack .env, read as text and never sourced (bcrypt hashes
# there start with `$2a$`; see firstboot.sh step 2). Last assignment wins, as in
# compose. Strips a CR, an unquoted ` # comment`, and surrounding quotes. awk
# reads the whole file, so nothing upstream can take SIGPIPE under pipefail.
_env() {
    [ -r "$ENV_FILE" ] || return 0
    awk -v k="$1=" '{ sub(/\r$/, "") } index($0, k) == 1 { v = substr($0, length(k) + 1); f = 1 }
                    END { if (f) print v }' "$ENV_FILE" 2>/dev/null \
        | sed -e 's/[[:space:]]#.*$//' -e 's/[[:space:]]*$//' \
              -e 's/^"\(.*\)"$/\1/' -e "s/^'\\(.*\\)'\$/\\1/"
}

# ---------------------------------------------------------------------------
# The admin subnet. Pinned in docker-compose.yml; ADMIN_NET_SUBNET overrides it
# only if someone moves the network. Validated hard: exempting a typo'd /0 or a
# /8 would exempt far more than the one bridge it is for, and a CIDR with host
# bits set is printed back normalised by `iptables -S`, which would read as
# drift for ever.
# ---------------------------------------------------------------------------
valid_v4_net() {
    local ip="${1%/*}" len="${1##*/}" a b c d o num mask
    case "$1" in */*) ;; *) return 1 ;; esac
    [[ "$len" =~ ^[0-9]{1,2}$ ]] && [ "$len" -ge 16 ] && [ "$len" -le 32 ] || return 1
    [[ "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || return 1
    IFS=. read -r a b c d <<<"$ip"
    for o in "$a" "$b" "$c" "$d"; do [ "$((10#$o))" -le 255 ] || return 1; done
    num=$(( (10#$a << 24) | (10#$b << 16) | (10#$c << 8) | 10#$d ))
    mask=$(( (0xFFFFFFFF << (32 - len)) & 0xFFFFFFFF ))
    [ $(( num & mask )) -eq "$num" ]
}

SUBNET="$(_env ADMIN_NET_SUBNET)"
SUBNET="${SUBNET:-$DEFAULT_SUBNET}"

# Written in the order `iptables -S` prints them (source, interface, protocol,
# matches, target), measured with iptables 1.8.10 (nf_tables), so the listing
# reads back exactly as written.
R_V4=(! -s "$SUBNET" ! -i lo -p tcp -m tcp --dport "$PORT" -m comment --comment "$MARKER" -j REJECT --reject-with tcp-reset)
R_V6=(! -i lo -p tcp -m tcp --dport "$PORT" -m comment --comment "$MARKER" -j REJECT --reject-with tcp-reset)
rule_for() { if [ "$1" = iptables ]; then RULE=("${R_V4[@]}"); else RULE=("${R_V6[@]}"); fi; }
fam_net()  { if [ "$1" = iptables ]; then printf '%s' "$SUBNET"; fi; }

# `-w`: wait for the xtables lock rather than fail when tailscaled or docker is
# mid-edit. A no-op under the nft backend.
ipt() { local bin="$1"; shift; "$bin" -w 5 "$@"; }

# ---------------------------------------------------------------------------
# Parse `-S INPUT`, captured whole into a variable first. Never pipe iptables
# into an early-exiting reader: under pipefail the producer's SIGPIPE becomes the
# status (llm-isolation.sh's banner has the measurements). Rule N is the Nth
# `-A` line. Prints three lines: the first ts-input jump's position (0 = none),
# the positions of rules carrying the marker, and what each one is.
#
# Classification is by feature, not by exact text, so a difference in how some
# iptables build orders its output cannot read as permanent drift; `-C` then
# confirms the rule semantically. `fence` is exactly our rule for this family
# (v6 is recognised by having no admin subnet to exempt). Anything else carrying
# the marker - a stale subnet, a duplicate's sibling from an older design, a
# stray - is `other`, and `other` is drift.
# ---------------------------------------------------------------------------
analyse() {
    printf '%s\n' "$1" | awk -v m="$MARKER" -v p="$PORT" -v net="$2" '
        function negations(s,   c, i) { c = 0; for (i = 1; i + 2 <= length(s); i++) if (substr(s, i, 3) == " ! ") c++; return c }
        /^-A INPUT / {
            n++
            if (!ts && $0 ~ /^-A INPUT -j ts-input[ \t]*$/) ts = n
            l = " " $0 " "
            if (index(l, " --comment " m " ") || index(l, " --comment \"" m "\" ")) {
                k = "other"
                if (index(l, " -p tcp ") && index(l, " --dport " p " ") && index(l, " -j REJECT --reject-with tcp-reset ") \
                    && index(l, " ! -i lo ") && !index(l, " -d ")) {
                    if (net != "" && index(l, " ! -s " net " ") && negations(l) == 2) k = "fence"
                    else if (net == "" && !index(l, " -s ") && negations(l) == 1) k = "fence"
                }
                pos = pos (pos == "" ? "" : " ") n
                kinds = kinds (kinds == "" ? "" : " ") k
            }
        }
        END { print ts + 0; print pos; print kinds }'
}

# check_family BIN -> 0 in place, 1 drift, 2 cannot read. Sets CHECK_DESC /
# DRIFT_REASON. IN PLACE MEANS: exactly one rule carrying the marker, it is
# ours, and it is ABOVE the ts-input jump if there is one. Directly above is
# where `install` puts it, but it is not required here: nothing that lands
# between it and the jump can admit 5380 past it, and demanding adjacency would
# start a fight with any future tool that inserts just above the jump.
check_family() {
    local bin="$1" listing ts pos kinds out
    CHECK_DESC=""; DRIFT_REASON=""
    if ! listing="$(ipt "$bin" -S INPUT 2>&1)"; then
        DRIFT_REASON="$bin -S INPUT failed: ${listing##*$'\n'}"
        return 2
    fi
    out="$(analyse "$listing" "$(fam_net "$bin")")"
    { read -r ts; read -r pos; read -r kinds; } <<<"$out"
    if [ "$kinds" != fence ]; then
        DRIFT_REASON="$bin: marked rules [${kinds:-none}] at [${pos:-none}], want exactly one fence rule"
        return 1
    fi
    if [ "$ts" -gt 0 ] && [ "$pos" -gt "$ts" ]; then
        DRIFT_REASON="$bin: fence at $pos is BELOW the ts-input jump at $ts, so tailnet traffic to $PORT is accepted before it"
        return 1
    fi
    rule_for "$bin"
    ipt "$bin" -C INPUT "${RULE[@]}" 2>/dev/null || { DRIFT_REASON="$bin: the fence rule fails -C"; return 1; }
    if [ "$ts" -gt 0 ]; then CHECK_DESC="$bin: fence at $pos, ts-input at $ts"
    else CHECK_DESC="$bin: fence at $pos, no ts-input jump"; fi
    return 0
}

# Delete every rule carrying the marker, BY SPEC, re-reading after each delete.
# By spec rather than by number because tailscaled may be inserting at position
# 1 at the same moment, and a delete by a stale number removes someone else's
# rule. The `-S` line keeps its chain once `-A ` becomes `-D ` (the RustDesk
# fence's 2026-09-18 bug was naming the chain twice). A failed delete stops the
# run: rebuilding on top of a stale rule is how the old RustDesk fence
# accumulated. Bounded, so a delete that "succeeds" without effect cannot spin.
purge_family() {
    local bin="$1" listing line spec n=0
    local -a argv
    while :; do
        listing="$(ipt "$bin" -S INPUT 2>/dev/null)" || { log "$bin: cannot list INPUT"; return 1; }
        line="$(printf '%s\n' "$listing" | awk -v m="$MARKER" '
            /^-A INPUT / && !s && (index($0 " ", " --comment " m " ") || index($0 " ", " --comment \"" m "\" ")) { print; s = 1 }')"
        [ -n "$line" ] || break
        if [ "$n" -ge 50 ]; then log "$bin: still finding marked rules after 50 deletes - stopping"; return 1; fi
        spec="-D ${line#-A }"
        spec="${spec//\"$MARKER\"/$MARKER}"
        read -r -a argv <<<"$spec"
        if ! ipt "$bin" "${argv[@]}"; then log "$bin: could not delete [$line]"; return 1; fi
        n=$((n + 1))
    done
    [ "$n" -eq 0 ] || log "$bin: removed $n marked rule(s)"
    return 0
}

# Purge, then insert directly above the jump (or at the top when there is no
# jump yet - the later tailscaled hook moves it). The window with no fence is
# the milliseconds between the two; the REJECT matches every 5380 packet, not
# only NEW ones, so a connection opened in that window is reset on its next
# packet. Verified after every attempt; three attempts because tailscaled can
# move the jump mid-way.
assert_family() {
    local bin="$1" attempt listing out ts at
    rule_for "$bin"
    for attempt in 1 2 3; do
        purge_family "$bin" || return 1
        listing="$(ipt "$bin" -S INPUT 2>/dev/null)" || { log "$bin: cannot list INPUT"; return 1; }
        out="$(analyse "$listing" "")"
        read -r ts <<<"$out"
        at=1; [ "$ts" -gt 0 ] && at="$ts"
        ipt "$bin" -I INPUT "$at" "${RULE[@]}" || log "$bin: insert at $at failed"
        if check_family "$bin"; then log "asserted - $CHECK_DESC"; return 0; fi
        log "$bin: attempt $attempt did not verify: $DRIFT_REASON"
    done
    return 1
}

# ---------------------------------------------------------------------------
# Alerting. Best-effort, bounded, and unable to change the exit status: every
# path returns 0. The URL is the host's own ntfy (contract: NTFY_BIND_IP, else
# LAN_IP). The same kind of alert is not repeated within ALERT_REPEAT_SEC once
# one has been DELIVERED, so a fault that persists costs one message per half
# hour rather than one per timer tick; an undelivered one is retried next run.
# ---------------------------------------------------------------------------
alert() {
    local kind="$1" prio="$2" title="$3" body="$4" host port topic url now last=0 stamp
    host="$(_env NTFY_BIND_IP)"; [ -n "$host" ] || host="$(_env LAN_IP)"
    port="$(_env NTFY_PORT)"
    topic="$(_env HOMEHUB_ALERT_TOPIC)"; topic="${topic:-homehub-alerts}"
    if ! [[ "$host" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || ! [[ "$port" =~ ^[0-9]{1,5}$ ]] \
       || ! [[ "$topic" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
        log "ALERT NOT SENT (no usable NTFY_BIND_IP/LAN_IP, NTFY_PORT or topic in $ENV_FILE): $title"
        return 0
    fi
    url="http://$host:$port/$topic"
    stamp="$STATE_DIR/alerted-$kind"
    now="$(date +%s)"
    if [ -r "$stamp" ]; then last="$(_int "$(cat "$stamp" 2>/dev/null)" 0)"; fi
    if [ $((now - last)) -lt "$ALERT_REPEAT_SEC" ]; then
        log "alert suppressed (a '$kind' alert was delivered $((now - last))s ago): $title"
        return 0
    fi
    if ! command -v curl >/dev/null 2>&1; then log "ALERT NOT SENT (no curl): $title"; return 0; fi
    if timeout "$((ALERT_TIMEOUT + 2))" curl -fsS -o /dev/null --connect-timeout 3 --max-time "$ALERT_TIMEOUT" \
            -H "Title: $title" -H "Priority: $prio" -H "Tags: shield" \
            --data-binary "$body" "$url" >/dev/null 2>&1; then
        log "alert delivered to $url: $title"
        { mkdir -p "$STATE_DIR" && printf '%s\n' "$now" >"$stamp"; } 2>/dev/null || true
    else
        log "ALERT NOT DELIVERED to $url (ntfy down or unreachable; this journal line is the record): $title"
    fi
    return 0
}

alert_failed() {
    alert fail high "HomeHub: DNS console fence is NOT in place" \
"The tcp/5380 fence could not be asserted on $(hostname 2>/dev/null || echo this host).
$1
Technitium keeps running (D-A5): its console is reachable behind its own login, including from the tailnet, until this is fixed.
journalctl -u 'homehub-dns-console*' ; $HERE/dns-console-fence.sh status"
}

# ---------------------------------------------------------------------------
# Locking: the boot unit, the tailscaled hook and the timer can overlap, and two
# interleaved purge/insert runs are how duplicates are made. The lock covers the
# chain edits only - never the wait for the jump, and never the alert.
# SIGTERM inside it (PartOf= propagates a tailscaled stop) is deferred until the
# edit is finished, so a stop cannot leave the fence half-rebuilt.
# ---------------------------------------------------------------------------
TERMINATED=0
take_lock() {
    trap 'TERMINATED=1' TERM
    command -v flock >/dev/null 2>&1 || { log "WARN: flock not found - running unlocked"; return 0; }
    if ! exec 9>>"$LOCK_FILE"; then log "WARN: cannot open $LOCK_FILE - running unlocked"; return 0; fi
    flock -w 60 9 || { log "could not take $LOCK_FILE within 60 s"; return 1; }
}
drop_lock() {
    exec 9>&-
    trap - TERM
    if [ "$TERMINATED" = 1 ]; then log "SIGTERM received during the edit; the edit finished, exiting"; exit 143; fi
}

# ---------------------------------------------------------------------------
MODE="${1:-}"
case "$MODE" in
    install|check|after-tailscaled|remove|status) ;;
    *) echo "usage: $0 install|check|after-tailscaled|remove|status" >&2; exit 2 ;;
esac

FAMS=(iptables)
if [ -d "$PROC_IPV6" ]; then
    FAMS+=(ip6tables)
else
    log "IPv6 is disabled in this kernel ($PROC_IPV6 absent): the v6 half has nothing to fence"
fi

if [ "$MODE" != remove ] && ! valid_v4_net "$SUBNET"; then
    log "FATAL: ADMIN_NET_SUBNET='$SUBNET' is not an IPv4 network address of /16 to /32 - refusing to guess; nothing changed"
    [ "$MODE" = status ] || alert_failed "ADMIN_NET_SUBNET='$SUBNET' in $ENV_FILE is not a valid network; nothing was changed."
    exit 1
fi

missing=""
for bin in "${FAMS[@]}"; do command -v "$bin" >/dev/null 2>&1 || missing="$missing $bin"; done
if [ -n "$missing" ]; then
    log "FATAL: not found:$missing - a fence with one family unprogrammed is not a fence"
    case "$MODE" in status|remove) ;; *) alert_failed "Not installed:$missing" ;; esac
    exit 1
fi

# converge QUIET -> sets FAILED / DRIFTED. Checks first and edits only a family
# that is out of place, so a healthy chain is never touched.
converge() {
    local bin rc
    FAILED=""; DRIFTED=""
    take_lock || { FAILED="could not take the lock"; return; }
    for bin in "${FAMS[@]}"; do
        check_family "$bin"; rc=$?
        case "$rc" in
            0) [ "$1" = quiet ] || log "in place - $CHECK_DESC" ;;
            1) log "DRIFT - $DRIFT_REASON"
               DRIFTED="${DRIFTED}${DRIFTED:+; }$DRIFT_REASON"
               assert_family "$bin" || FAILED="${FAILED}${FAILED:+; }$bin: re-assert failed ($DRIFT_REASON)" ;;
            *) log "ERROR - $DRIFT_REASON"
               FAILED="${FAILED}${FAILED:+; }$DRIFT_REASON" ;;
        esac
    done
    drop_lock
}

# Rule numbers and the jump, per family - the "show order" an operator wants.
show() {
    local bin listing
    for bin in "${FAMS[@]}"; do
        listing="$(ipt "$bin" -S INPUT 2>&1)" || { log "$bin: cannot list INPUT: $listing"; continue; }
        printf '%s\n' "$listing" | awk -v b="$bin" -v m="$MARKER" \
            '/^-A INPUT / { n++; if (index($0, m) || $0 ~ /-j ts-input/) printf "  %-9s %3d  %s\n", b, n, $0 }'
    done
}

jump_at() {
    local listing out ts
    listing="$(ipt "$1" -S INPUT 2>/dev/null)" || { echo 0; return; }
    out="$(analyse "$listing" "")"
    read -r ts <<<"$out"
    echo "${ts:-0}"
}

# Wait for tailscaled to re-add its jump (see the header: READY comes first).
# v4 is the signal; v6 gets a short grace after it, because tailscaled programs
# no v6 hooks at all on a box whose ip6tables filter table it cannot use.
wait_for_jump() {
    local waited=0 v4_since=-1 t4 t6
    while :; do
        t4="$(jump_at iptables)"
        t6=1; [ "${#FAMS[@]}" -gt 1 ] && t6="$(jump_at ip6tables)"
        if [ "$t4" -gt 0 ]; then
            if [ "$t6" -gt 0 ]; then log "ts-input jump present after ${waited}s"; return 0; fi
            [ "$v4_since" -ge 0 ] || v4_since="$waited"
            if [ $((waited - v4_since)) -ge "$V6_GRACE_SEC" ]; then
                log "v4 jump present, no v6 jump after ${V6_GRACE_SEC}s - proceeding"
                return 0
            fi
        fi
        if [ "$waited" -ge "$WAIT_SEC" ]; then
            log "no ts-input jump after ${WAIT_SEC}s (tailscaled logged out, or control unreachable); asserting presence only - the check timer re-positions the fence if the jump arrives later"
            return 1
        fi
        sleep "$POLL_SEC"
        waited=$((waited + POLL_SEC))
    done
}

case "$MODE" in
    status)
        rc=0
        for bin in "${FAMS[@]}"; do
            check_family "$bin"
            case "$?" in
                0) log "in place - $CHECK_DESC" ;;
                1) log "DRIFT - $DRIFT_REASON"; [ "$rc" -ne 0 ] || rc=3 ;;
                *) log "ERROR - $DRIFT_REASON"; rc=1 ;;
            esac
        done
        show
        exit "$rc"
        ;;

    remove)
        take_lock || exit 1
        rc=0
        for bin in "${FAMS[@]}"; do
            purge_family "$bin" || rc=1
            listing="$(ipt "$bin" -S INPUT 2>/dev/null)" || { rc=1; continue; }
            out="$(analyse "$listing" "")"
            { read -r _; read -r pos; } <<<"$out"
            if [ -n "$pos" ]; then log "$bin: marked rules still present at [$pos]"; rc=1; fi
        done
        drop_lock
        if [ "$rc" -eq 0 ]; then log "removed: no $MARKER rules remain; the console answers on every interface again"
        else log "FAILED to remove every $MARKER rule"; fi
        show
        exit "$rc"
        ;;

    install)
        take_lock || { alert_failed "Could not take $LOCK_FILE."; exit 1; }
        FAILED=""
        for bin in "${FAMS[@]}"; do
            assert_family "$bin" || FAILED="${FAILED}${FAILED:+; }$bin: ${DRIFT_REASON:-could not program INPUT}"
        done
        drop_lock
        if [ -n "$FAILED" ]; then log "FAILED - $FAILED"; alert_failed "$FAILED"; exit 1; fi
        log "fence up: tcp/$PORT rejected except from lo and $SUBNET, above ts-input, families: ${FAMS[*]}"
        exit 0
        ;;

    check)
        converge loud
        if [ -n "$FAILED" ]; then alert_failed "$FAILED"; exit 1; fi
        if [ -n "$DRIFTED" ]; then
            alert drift default "HomeHub: DNS console fence drifted and was re-asserted" \
"The tcp/5380 fence on $(hostname 2>/dev/null || echo this host) was out of place and has been put back.
Found: $DRIFTED
Expected after 'tailscale down/up', a re-login or a control-pushed firewall change; after a tailscaled restart the hook unit should have fixed it first."
        fi
        exit 0
        ;;

    after-tailscaled)
        wait_for_jump || true
        converge quiet
        any_drift="$DRIFTED"
        if [ -z "$FAILED" ] && [ "$SETTLE_SEC" -gt 0 ]; then
            # One more look once tailscaled has settled: its v6 hook, or a second
            # Set() from a netmap that changed the firewall kind, can follow the
            # first within seconds.
            sleep "$SETTLE_SEC"
            converge quiet
            any_drift="$any_drift$DRIFTED"
        fi
        if [ -n "$FAILED" ]; then alert_failed "$FAILED"; exit 1; fi
        if [ -n "$any_drift" ]; then log "re-asserted after tailscaled (expected there; no alert)"
        else log "already in place after tailscaled"; fi
        exit 0
        ;;
esac
