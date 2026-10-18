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
#   every listener present                    -> log, exit 0
#   a listener missing:
#     fence unit not active                   -> alert (high), exit 1, start NOTHING
#     else force-recreate ONLY the service(s) whose listeners are missing,
#     wait, re-check; alert "recovered" (exit 0) or "still down" (high, exit 1)
#
# WHY NOT `systemctl restart homehub-rustdesk.service`. Its ExecStop is
# `compose stop` and its ExecStart `compose up -d`: against a container docker
# wrongly believes is running, stop can hang or no-op and `up -d` sees nothing to
# do. `up --force-recreate --no-deps` replaces the container regardless of what
# docker thinks. The key pair lives in the named volume (stack_rustdesk_data), so
# recreating is safe. The fence invariant is kept by checking the isolation unit
# is active IMMEDIATELY before any recreate; the owner unit stays as it is.
#
# Required listeners: hbbs tcp 21115, tcp 21116, udp 21116; hbbr tcp 21117.
# 21118/21119 (the websocket pair) are not required: the desktop clients use
# neither, and failing a check on them would recreate a healthy relay.
#
# Alerts are best-effort (bounded, never change the exit status) and repeat-
# suppressed by a stamp. Exit: 0 healthy/recovered/not applicable, 1 still down.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="${RUSTDESK_CHECK_STACK_DIR:-$HERE/..}"
ENV_FILE="${RUSTDESK_CHECK_ENV_FILE:-$STACK_DIR/.env}"
STATE_DIR="${RUSTDESK_CHECK_STATE_DIR:-/var/lib/homehub-rustdesk-check}"
OWNER_UNIT="homehub-rustdesk.service"
FENCE_UNIT="homehub-rustdesk-isolation.service"

log() { printf '%s [rustdesk-check] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
_int() { case "$1" in ''|*[!0-9]*) printf '%s' "$2" ;; *) printf '%s' "$1" ;; esac; }
WAIT_SEC="$(_int "${RUSTDESK_CHECK_WAIT_SEC:-}" 30)"
POLL_SEC="$(_int "${RUSTDESK_CHECK_POLL_SEC:-}" 3)"; [ "$POLL_SEC" -ge 1 ] || POLL_SEC=1
ALERT_TIMEOUT="$(_int "${RUSTDESK_CHECK_ALERT_TIMEOUT:-}" 6)"; [ "$ALERT_TIMEOUT" -ge 1 ] || ALERT_TIMEOUT=1
ALERT_REPEAT_SEC="$(_int "${RUSTDESK_CHECK_ALERT_REPEAT_SEC:-}" 1800)"
RECREATE_TIMEOUT="$(_int "${RUSTDESK_CHECK_RECREATE_TIMEOUT:-}" 120)"

# One key from .env, read as text, never sourced. Last assignment wins; strips a
# CR, an unquoted trailing comment and surrounding quotes (as the sibling check).
_env() {
    [ -r "$ENV_FILE" ] || return 0
    awk -v k="$1=" '{ sub(/\r$/, "") } index($0, k) == 1 { v = substr($0, length(k) + 1); f = 1 }
                    END { if (f) print v }' "$ENV_FILE" 2>/dev/null \
        | sed -e 's/[[:space:]]#.*$//' -e 's/[[:space:]]*$//' \
              -e 's/^"\(.*\)"$/\1/' -e "s/^'\\(.*\\)'\$/\\1/"
}

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

# Fills MISSING_LIST with "service:proto/port" entries; returns 0 when none.
# A failing `ss` is NOT read as "nothing listening": that would recreate healthy
# containers because of a tooling fault.
SS_FAILED=0
MISSING_LIST=()
check_listeners() {
    local out want proto port svc
    MISSING_LIST=(); SS_FAILED=0
    out="$(timeout 10 ss -H -ltnu 2>/dev/null)" || { SS_FAILED=1; return 1; }
    for want in hbbs:tcp/21115 hbbs:tcp/21116 hbbs:udp/21116 hbbr:tcp/21117; do
        svc="${want%%:*}"; proto="${want#*:}"; port="${proto#*/}"; proto="${proto%/*}"
        if ! awk -v p="$proto" -v port="$port" '
              $1 == p { n = split($5, a, ":"); if (a[n] == port) f = 1 }
              END { exit f ? 0 : 1 }' <<<"$out"; then
            MISSING_LIST+=("$want")
        fi
    done
    [ "${#MISSING_LIST[@]}" -eq 0 ]
}

main() {
    local enabled s m deadline services=() text
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
        log "ok - hbbs tcp 21115/21116 udp 21116, hbbr tcp 21117 are listening"
        return 0
    fi
    if [ "$SS_FAILED" = 1 ]; then
        log "ss failed or timed out - cannot judge the listeners, taking no action"
        return 1
    fi
    text="${MISSING_LIST[*]}"
    log "MISSING listeners: $text"
    for m in "${MISSING_LIST[@]}"; do
        s="${m%%:*}"
        case " ${services[*]:-} " in *" $s "*) ;; *) services+=("$s") ;; esac
    done

    # The fence invariant: listeners are only ever started with the fence up.
    if ! timeout 20 systemctl is-active --quiet "$FENCE_UNIT"; then
        log "$FENCE_UNIT is NOT active - refusing to start unfenced listeners"
        alert fence-down high "HomeHub: RustDesk listeners down and the fence is not active" \
"Missing on $(hostname 2>/dev/null || echo this host): $text
$FENCE_UNIT is not active, so nothing was restarted (listeners must never run unfenced).
systemctl status $FENCE_UNIT"
        return 1
    fi

    log "recreating: ${services[*]}"
    if ! ( cd "$STACK_DIR" && timeout "$RECREATE_TIMEOUT" docker compose --profile rustdesk \
            up -d --no-build --force-recreate --no-deps "${services[@]}" ); then
        log "docker compose recreate failed or timed out"
    fi

    deadline=$(( $(date +%s) + WAIT_SEC ))
    while :; do
        if check_listeners; then
            log "recovered - all listeners present after recreating ${services[*]}"
            alert recovered default "HomeHub: RustDesk listeners were down and are back" \
"Missing on $(hostname 2>/dev/null || echo this host): $text
Recreated: ${services[*]} (the docker state may have said 'Up' throughout).
journalctl -u homehub-rustdesk-check"
            return 0
        fi
        [ "$(date +%s)" -ge "$deadline" ] && break
        sleep "$POLL_SEC"
    done
    log "STILL DOWN after recreate: ${MISSING_LIST[*]:-(ss failed)}"
    alert down high "HomeHub: RustDesk listeners are STILL down" \
"Missing on $(hostname 2>/dev/null || echo this host): ${MISSING_LIST[*]:-unknown}
Recreating ${services[*]} did not bring them back within ${WAIT_SEC}s.
journalctl -u homehub-rustdesk-check ; docker logs hbbs"
    return 1
}

# One run at a time (timer + a hand run). A busy lock is not a failure.
mkdir -p "$STATE_DIR" 2>/dev/null || true
if command -v flock >/dev/null 2>&1 && exec 9>>"$STATE_DIR/lock" 2>/dev/null; then
    flock -n 9 || { log "another run holds the lock - skipping"; exit 0; }
fi
main
exit $?
