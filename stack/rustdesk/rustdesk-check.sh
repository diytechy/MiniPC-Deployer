#!/usr/bin/env bash
# rustdesk-check.sh - verify the RustDesk LISTENERS exist; recreate what is dead.
#
# Implements: SR-045 / LLR-1137. Driven by homehub-rustdesk-check.timer (5 min).
#
# WHY THIS ASKS THE KERNEL AND NOT DOCKER. hbbs runs a periodic self-test against
# its own 0.0.0.0:21116 and EXITS on a single failed test. On 2026-09-28 21:55
# a memory-pressure episode (containerd/dockerd "context deadline exceeded")
# made that test time out, hbbs exited, and docker LOST the exit event: `docker
# ps` said "Up 4 weeks", `docker top hbbs` listed no process, and nothing
# listened on tcp 21115/21116 or udp 21116 for three weeks. The containers carry
# `restart: "no"` on purpose (only homehub-rustdesk.service, ordered after the
# fence, may start them), so nothing brought it back. Docker state was the thing
# that lied; a socket in the listening table cannot.
#
# WHAT IT DOES
#   RUSTDESK_ENABLED != true                  -> log, exit 0
#   homehub-rustdesk.service not active       -> log, exit 0 (stopped on purpose)
#   every listener present                    -> log, exit 0 (and the retry count resets)
#   a listener missing -> take the OWNER LOCK, then under it:
#     owner unit no longer active             -> exit 0, touch nothing
#     the fence rules are not really there    -> alert (high), exit 1, start NOTHING
#     too many recent attempts without a stable recovery -> alert (high), exit 1
#     else force-recreate ONLY the service(s) whose listeners are missing, then
#     require the listeners to stay up through a settle window:
#       stable -> alert "recovered", exit 0;  not stable -> "still down", exit 1
#
# THE FENCE IS VERIFIED BY ITS RULES, not by the isolation unit being "active":
# that unit is a oneshot with RemainAfterExit, so it stays active after a
# firewall flush or reload removed either family's rules. Per family and per
# protocol the marked rules must read, in order: loopback ACCEPT, an ACCEPT for
# each configured admitted CIDR, then a REJECT (rustdesk-isolation.sh).
#
# THE OWNER LOCK. homehub-rustdesk.service wraps its ExecStart/ExecStop in
# flock on the same file, and this script holds it for the whole recovery. An
# operator stop therefore waits for a recovery in flight, or runs first and is
# seen when the owner is re-validated under the lock; recovery cannot undo it.
#
# WHY NOT `systemctl restart homehub-rustdesk.service`. Its ExecStop is
# `compose stop` and its ExecStart `compose up -d`: against a container docker
# wrongly believes is running, stop can hang or no-op and `up -d` sees nothing to
# do. `up --force-recreate --no-deps` replaces the container regardless of what
# docker thinks. The key pair lives in the named volume (stack_rustdesk_data), so
# recreating is safe.
#
# WHY A SETTLE WINDOW. hbbs listens briefly and THEN runs its startup self-test
# and may exit (rustdesk-isolation.sh records "listened for twenty seconds and
# vanished"). One good snapshot is not recovery; the listeners must be present on
# every snapshot for SETTLE_SEC. And a fault that keeps coming back must not
# loop silently: after MAX_ATTEMPTS recreates without a stable recovery it stops
# recreating (one more attempt per BACKOFF_SEC) and keeps alerting high.
#
# Required listeners: hbbs tcp 21115, tcp 21116, udp 21116; hbbr tcp 21117.
# 21118/21119 (the websocket pair) are not required: the desktop clients use
# neither, and failing a check on them would recreate a healthy relay.
#
# Alerts are best-effort (bounded, never change the exit status) and repeat-
# suppressed by a stamp, both through the shared hub alert helper
# (stack/monitoring/homehub-alert.sh, SR-085). Exit: 0 healthy/recovered/not
# applicable, 1 still down.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="${RUSTDESK_CHECK_STACK_DIR:-$HERE/..}"
ENV_FILE="${RUSTDESK_CHECK_ENV_FILE:-$STACK_DIR/.env}"
# Resolved from THIS script's own location, not STACK_DIR: the helper ships in the
# same tree as this script, whatever stack the check is pointed at.
ALERT_HELPER="${RUSTDESK_CHECK_ALERT_HELPER:-$HERE/../monitoring/homehub-alert.sh}"
STATE_DIR="${RUSTDESK_CHECK_STATE_DIR:-/var/lib/homehub-rustdesk-check}"
OWNER_LOCK="${RUSTDESK_CHECK_OWNER_LOCK:-/run/homehub-rustdesk.lock}"
OWNER_UNIT="homehub-rustdesk.service"
FENCE_UNIT="homehub-rustdesk-isolation.service"
FENCE_MARK="homehub-rustdesk-isolation"
# All FIVE tcp ports: recreating hbbs/hbbr also opens the websocket pair
# 21118/21119, so the fence must cover them even though the listener health
# check above does not require them to be listening.
FENCE_TCP_PORTS="21115,21116,21117,21118,21119"
FENCE_UDP_PORTS="21116"
# The most this script can hold the owner lock, in seconds: owner and fence checks
# (~60) + recreate (RECREATE_TIMEOUT 90) + appear and settle windows (30 + 45)
# + a snapshot of overshoot and the alert, with headroom. homehub-rustdesk.service
# sets TimeoutStartSec/TimeoutStopSec to this plus a bounded compose stop; a test
# asserts the relationship. In practice recovery lets go far sooner: it aborts as
# soon as the owner is seen stopping.
LOCK_HOLD_BUDGET_SEC=270

log() { printf '%s [rustdesk-check] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
_int() { case "$1" in ''|*[!0-9]*) printf '%s' "$2" ;; *) printf '%s' "$1" ;; esac; }
WAIT_SEC="$(_int "${RUSTDESK_CHECK_WAIT_SEC:-}" 30)"
SETTLE_SEC="$(_int "${RUSTDESK_CHECK_SETTLE_SEC:-}" 45)"
POLL_SEC="$(_int "${RUSTDESK_CHECK_POLL_SEC:-}" 3)"; [ "$POLL_SEC" -ge 1 ] || POLL_SEC=1
ALERT_TIMEOUT="$(_int "${RUSTDESK_CHECK_ALERT_TIMEOUT:-}" 6)"; [ "$ALERT_TIMEOUT" -ge 1 ] || ALERT_TIMEOUT=1
ALERT_REPEAT_SEC="$(_int "${RUSTDESK_CHECK_ALERT_REPEAT_SEC:-}" 1800)"
RECREATE_TIMEOUT="$(_int "${RUSTDESK_CHECK_RECREATE_TIMEOUT:-}" 90)"
LOCK_WAIT_SEC="$(_int "${RUSTDESK_CHECK_LOCK_WAIT_SEC:-}" 30)"
MAX_ATTEMPTS="$(_int "${RUSTDESK_CHECK_MAX_ATTEMPTS:-}" 3)"
BACKOFF_SEC="$(_int "${RUSTDESK_CHECK_BACKOFF_SEC:-}" 3600)"
ATTEMPTS_FILE="$STATE_DIR/recreate-attempts"

# One key from .env, read as text, never sourced. Last assignment wins; strips a
# CR, an unquoted trailing comment and surrounding quotes (as the sibling check).
_env() {
    [ -r "$ENV_FILE" ] || return 0
    awk -v k="$1=" '{ sub(/\r$/, "") } index($0, k) == 1 { v = substr($0, length(k) + 1); f = 1 }
                    END { if (f) print v }' "$ENV_FILE" 2>/dev/null \
        | sed -e 's/[[:space:]]#.*$//' -e 's/[[:space:]]*$//' \
              -e 's/^"\(.*\)"$/\1/' -e "s/^'\\(.*\\)'\$/\\1/"
}

# The transport, the rate limit and the journal line live in the shared hub
# alert helper (SR-085, stack/monitoring/homehub-alert.sh), which every SN-048
# monitor uses too. Its stamps stay in THIS script's state dir under the same
# names (alerted-<kind>), so a deploy does not reset the repeat window. Best
# effort: whatever the helper reports, the check's own verdict is unchanged. It
# is bounded twice: its own curl timeout, and this outer one.
alert() {
    local kind="$1" prio="$2" title="$3" body="$4"
    if [ ! -r "$ALERT_HELPER" ]; then
        log "ALERT NOT SENT (the alert helper $ALERT_HELPER is missing): $title"
        return 0
    fi
    timeout "$((ALERT_TIMEOUT + 10))" bash "$ALERT_HELPER" --key "$kind" --priority "$prio" \
        --title "$title" --body "$body" --env-file "$ENV_FILE" --state-dir "$STATE_DIR" \
        --repeat-sec "$ALERT_REPEAT_SEC" --timeout "$ALERT_TIMEOUT" --tags shield \
        --log-tag rustdesk-check </dev/null || true
    return 0
}

# Fills MISSING_LIST with "service:proto/port" entries; returns 0 when none.
# A failing `ss` is NOT read as "nothing listening": that would recreate healthy
# containers because of a tooling fault.
SS_FAILED=0
MISSING_LIST=()
check_listeners() {
    local out want proto port
    MISSING_LIST=(); SS_FAILED=0
    out="$(timeout 10 ss -H -ltnu 2>/dev/null)" || { SS_FAILED=1; return 1; }
    for want in hbbs:tcp/21115 hbbs:tcp/21116 hbbs:udp/21116 hbbr:tcp/21117; do
        proto="${want#*:}"; port="${proto#*/}"; proto="${proto%/*}"
        if ! awk -v p="$proto" -v port="$port" '
              $1 == p { n = split($5, a, ":"); if (a[n] == port) f = 1 }
              END { exit f ? 0 : 1 }' <<<"$out"; then
            MISSING_LIST+=("$want")
        fi
    done
    [ "${#MISSING_LIST[@]}" -eq 0 ]
}

# Does one family's INPUT chain carry the fence, in order, for tcp AND udp?
# $1 = iptables|ip6tables, rest = admitted CIDRs that must be present. A failing
# listing is "not verified". Sets FENCE_WHY.
FENCE_WHY=""
fence_family_ok() {
    local bin="$1" proto ports rules; shift
    rules="$(timeout 10 "$bin" -S INPUT 2>/dev/null)" || { FENCE_WHY="$bin -S INPUT failed"; return 1; }
    for proto in tcp udp; do
        # The ports the checker is about to (re)start listeners on. A marked
        # rule that does not cover ALL of them leaves some listener unfenced.
        if [ "$proto" = tcp ]; then ports="$FENCE_TCP_PORTS"; else ports="$FENCE_UDP_PORTS"; fi
        # Rules are matched on their SPEC, not just their marker: the marker may
        # print quoted or bare; the dports must cover the ports above; and the
        # closing REJECT must carry no -s/-d/-i/-o/! restriction, else it is not
        # the catch-all and a source-restricted one would pass for it.
        if ! printf '%s\n' "$rules" | awk -v mark="$FENCE_MARK" -v p="$proto" -v cidrs="$*" -v ports="$ports" '
              function covers(spec,   k, m, parts, r, lohi, hit) {
                  m = split(spec, parts, ",")
                  for (r = 1; r <= np; r++) {
                      hit = 0
                      for (k = 1; k <= m; k++) {
                          if (index(parts[k], ":")) {
                              split(parts[k], lohi, ":")
                              if (need[r] + 0 >= lohi[1] + 0 && need[r] + 0 <= lohi[2] + 0) hit = 1
                          } else if (parts[k] + 0 == need[r] + 0) hit = 1
                      }
                      if (!hit) return 0
                  }
                  return 1
              }
              BEGIN { n = split(cidrs, want, " "); np = split(ports, need, ","); re = "--comment \"?" mark "\"?( |$)" }
              $0 ~ re && index($0, "-p " p " ") {
                  spec = ""
                  for (i = 1; i < NF; i++) if ($i == "--dports" || $i == "--dport") spec = $(i + 1)
                  if (spec == "" || !covers(spec)) next
                  if ($0 ~ /-j REJECT/) {
                      if ($0 ~ / (-s|-d|-i|-o) / || $0 ~ / ! /) next
                      if (!done) { ok = lo; for (i = 1; i <= n; i++) if (!seen[i]) ok = 0; done = 1 }
                  } else if ($0 ~ /-j ACCEPT/ && !done) {
                      if (index($0, "-i lo ")) lo = 1
                      for (i = 1; i <= n; i++) if (index($0, "-s " want[i] " ")) seen[i] = 1
                  }
              }
              END { exit (done && ok) ? 0 : 1 }'; then
            FENCE_WHY="$bin: $proto fence (lo ACCEPT, admitted ACCEPTs, unrestricted REJECT, all of dports $ports) not in place"
            return 1
        fi
    done
    return 0
}

fence_ok() {
    local lan tun tun6
    lan="$(_env RUSTDESK_LAN_CIDR)"; tun="$(_env RUSTDESK_TUNNEL_CIDR)"; tun6="$(_env RUSTDESK_TUNNEL_CIDR6)"
    if [ -z "$lan" ]; then FENCE_WHY="RUSTDESK_LAN_CIDR is empty in $ENV_FILE - cannot verify the fence"; return 1; fi
    fence_family_ok iptables "$lan" $tun || return 1
    fence_family_ok ip6tables $tun6 || return 1
    return 0
}

# Consecutive recreate attempts without a stable recovery: "count epoch".
attempts_count() { local c; c="$(cut -d' ' -f1 "$ATTEMPTS_FILE" 2>/dev/null)"; _int "$c" 0; }
attempts_last() { local c; c="$(cut -d' ' -f2 "$ATTEMPTS_FILE" 2>/dev/null)"; _int "$c" 0; }
attempts_note() { { mkdir -p "$STATE_DIR" && printf '%s %s\n' "$(( $(attempts_count) + 1 ))" "$(date +%s)" >"$ATTEMPTS_FILE"; } 2>/dev/null || true; }
attempts_reset() { rm -f "$ATTEMPTS_FILE" 2>/dev/null || true; }

recover() {
    local s m services=() text now stable_since deadline t

    # Everything below runs under the owner lock (taken by main).
    if ! timeout 20 systemctl is-active --quiet "$OWNER_UNIT"; then
        log "$OWNER_UNIT is no longer active (stopped or stopping) - leaving it alone"
        return 0
    fi
    if check_listeners; then log "listeners are back on their own (owner start in flight) - nothing to do"; return 0; fi
    if [ "$SS_FAILED" = 1 ]; then log "ss failed - taking no action"; return 1; fi
    text="${MISSING_LIST[*]}"
    # Derived from the listing that is true NOW, under the lock - not the one
    # taken before the lock was won.
    for m in "${MISSING_LIST[@]}"; do
        s="${m%%:*}"
        case " ${services[*]:-} " in *" $s "*) ;; *) services+=("$s") ;; esac
    done

    if ! fence_ok; then
        log "FENCE NOT VERIFIED ($FENCE_WHY) - refusing to start unfenced listeners"
        alert fence-down high "HomeHub: RustDesk listeners down and the fence is not in place" \
"Missing on $(hostname 2>/dev/null || echo this host): $text
$FENCE_WHY
Nothing was restarted (listeners must never run unfenced). Re-run: systemctl restart $FENCE_UNIT"
        return 1
    fi

    now="$(date +%s)"
    if [ "$(attempts_count)" -ge "$MAX_ATTEMPTS" ] && [ $((now - $(attempts_last))) -lt "$BACKOFF_SEC" ]; then
        log "GIVING UP for now: $(attempts_count) recreates without a stable recovery (next attempt in $((BACKOFF_SEC - now + $(attempts_last)))s)"
        alert gaveup high "HomeHub: RustDesk keeps dying; automatic recreate paused" \
"Missing on $(hostname 2>/dev/null || echo this host): $text
$(attempts_count) recreates did not produce a stable recovery. Look at: docker logs hbbs ; journalctl -u homehub-rustdesk-check"
        return 1
    fi

    attempts_note
    log "recreating: ${services[*]} (attempt $(attempts_count))"
    if ! ( cd "$STACK_DIR" && timeout "$RECREATE_TIMEOUT" docker compose --profile rustdesk \
            up -d --no-build --force-recreate --no-deps "${services[@]}" ); then
        log "docker compose recreate failed or timed out"
    fi

    # Grace to appear (WAIT_SEC), then present on EVERY snapshot for SETTLE_SEC.
    t="$(date +%s)"; deadline=$(( t + WAIT_SEC + SETTLE_SEC )); stable_since=""
    while :; do
        # An operator stop is queued behind our lock: let go at once rather than
        # hold it (and make the stop time out) while the containers stay up.
        if ! timeout 20 systemctl is-active --quiet "$OWNER_UNIT"; then
            log "$OWNER_UNIT is stopping - aborting recovery and releasing the owner lock"
            return 0
        fi
        if check_listeners; then
            [ -n "$stable_since" ] || stable_since="$(date +%s)"
            if [ $(( $(date +%s) - stable_since )) -ge "$SETTLE_SEC" ]; then
                log "recovered - listeners stable for ${SETTLE_SEC}s after recreating ${services[*]}"
                attempts_reset
                alert recovered default "HomeHub: RustDesk listeners were down and are back" \
"Missing on $(hostname 2>/dev/null || echo this host): $text
Recreated: ${services[*]} (the docker state may have said 'Up' throughout); stable for ${SETTLE_SEC}s.
journalctl -u homehub-rustdesk-check"
                return 0
            fi
        else
            stable_since=""
        fi
        [ "$(date +%s)" -ge "$deadline" ] && break
        sleep "$POLL_SEC"
    done
    log "STILL DOWN after recreate (not stable for ${SETTLE_SEC}s): ${MISSING_LIST[*]:-(ss failed)}"
    alert down high "HomeHub: RustDesk listeners are STILL down" \
"Missing on $(hostname 2>/dev/null || echo this host): ${MISSING_LIST[*]:-unstable}
Recreating ${services[*]} did not give ${SETTLE_SEC}s of stable listeners (hbbs may be dying in its startup self-test).
journalctl -u homehub-rustdesk-check ; docker logs hbbs"
    return 1
}

main() {
    local enabled
    enabled="$(_env RUSTDESK_ENABLED)"
    if [ "$enabled" != "true" ]; then
        log "RUSTDESK_ENABLED='$enabled' - nothing to check"
        return 0
    fi
    if ! timeout 20 systemctl is-active --quiet "$OWNER_UNIT"; then
        log "$OWNER_UNIT is not active - stopped on purpose, nothing to check"
        return 0
    fi
    if check_listeners; then
        attempts_reset
        log "ok - hbbs tcp 21115/21116 udp 21116, hbbr tcp 21117 are listening"
        return 0
    fi
    if [ "$SS_FAILED" = 1 ]; then
        log "ss failed or timed out - cannot judge the listeners, taking no action"
        return 1
    fi
    log "MISSING listeners: ${MISSING_LIST[*]}"

    # The owner lock (fd 8), held until this process exits.
    if ! command -v flock >/dev/null 2>&1; then
        log "flock not found - cannot serialise with $OWNER_UNIT, taking no action"
        return 1
    fi
    if ! exec 8>>"$OWNER_LOCK" 2>/dev/null; then
        log "cannot open $OWNER_LOCK - taking no action"
        return 1
    fi
    if ! flock -w "$LOCK_WAIT_SEC" 8; then
        log "$OWNER_UNIT is mid-start/stop (owner lock busy ${LOCK_WAIT_SEC}s) - will retry next run"
        return 0
    fi
    recover
}

# One run at a time (timer + a hand run). A busy lock is not a failure.
mkdir -p "$STATE_DIR" 2>/dev/null || true
if command -v flock >/dev/null 2>&1 && exec 9>>"$STATE_DIR/lock" 2>/dev/null; then
    flock -n 9 || { log "another run holds the lock - skipping"; exit 0; }
fi
main
exit $?
