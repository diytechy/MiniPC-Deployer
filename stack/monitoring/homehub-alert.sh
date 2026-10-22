#!/usr/bin/env bash
# homehub-alert.sh - the hub's one way to raise an alert: ntfy, rate-limited, journaled.
#
# Implements: SR-085 / LLR-1147 (SN-048). Factored out of rustdesk-check.sh
# (SR-045), which now calls it; every SN-048 monitor calls it too, so there is one
# transport, one rate limit and one journal format for every host-side alert.
#
# WHAT IT DOES, once per call:
#   * reads NTFY_BIND_IP (else LAN_IP), NTFY_PORT and HOMEHUB_ALERT_TOPIC (default
#     homehub-alerts) from the stack .env AS TEXT - never sourced, so a value with
#     shell in it is data;
#   * suppresses the alert when the same KEY was DELIVERED less than --repeat-sec
#     ago (a stamp file per key in --state-dir). Only a delivery stamps: an alert
#     that could not be delivered is retried by the caller's next run;
#   * posts it to http://<host>:<port>/<topic> with curl under a hard timeout;
#   * writes ONE journal line for every call, whatever happened, so the journal is
#     the record when ntfy is down: "alert delivered", "alert suppressed",
#     "ALERT NOT SENT" (no usable config, no curl) or "ALERT NOT DELIVERED".
#
# A STAMP FROM THE FUTURE IS IGNORED. A clock that jumped backwards would
# otherwise suppress the key until the wall clock caught up again, which after a
# bad RTC read can be years. More than a minute ahead of now is treated as no stamp.
#
# Usage:
#   homehub-alert.sh --key KEY --priority PRIO --title TITLE [--body TEXT]
#                    [--env-file F] [--state-dir D] [--repeat-sec N]
#                    [--timeout N] [--tags T] [--log-tag NAME]
#   The body is --body, else stdin (read only when stdin is not a terminal).
#   KEY: [A-Za-z0-9_.-]{1,64}. PRIO: min|low|default|high|urgent (ntfy's names).
#
# Exit: 0 delivered; 3 suppressed by the rate limit; 4 not delivered (ntfy down,
# no usable config, no curl); 2 usage. The caller decides what a non-delivery
# means; it never changes a check's own verdict (rustdesk-check ignores it).
set -u

ENV_FILE="/opt/homehub/stack/.env"
STATE_DIR="/var/lib/homehub-monitor/alerts"
REPEAT_SEC=1800
TIMEOUT=6
TAGS="warning"
LOG_TAG="homehub-alert"
KEY="" PRIO="" TITLE="" BODY="" BODY_SET=0

log() { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LOG_TAG" "$*"; }
_int() { case "$1" in ''|*[!0-9]*) printf '%s' "$2" ;; *) printf '%s' "$1" ;; esac; }
usage() { printf 'homehub-alert.sh: %s\n' "$*" >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --key) KEY="${2:-}"; shift 2 ;;
        --priority) PRIO="${2:-}"; shift 2 ;;
        --title) TITLE="${2:-}"; shift 2 ;;
        --body) BODY="${2:-}"; BODY_SET=1; shift 2 ;;
        --env-file) ENV_FILE="${2:-}"; shift 2 ;;
        --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
        --repeat-sec) REPEAT_SEC="$(_int "${2:-}" 1800)"; shift 2 ;;
        --timeout) TIMEOUT="$(_int "${2:-}" 6)"; shift 2 ;;
        --tags) TAGS="${2:-}"; shift 2 ;;
        --log-tag) LOG_TAG="${2:-homehub-alert}"; shift 2 ;;
        *) usage "unknown argument: $1" ;;
    esac
done
[[ "$KEY" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || usage "--key must match [A-Za-z0-9_.-]{1,64}"
case "$PRIO" in min|low|default|high|urgent) ;; *) usage "--priority must be min|low|default|high|urgent" ;; esac
[[ "$TAGS" =~ ^[A-Za-z0-9_,-]{1,64}$ ]] || TAGS="warning"
[ "$TIMEOUT" -ge 1 ] || TIMEOUT=1
# A title is an HTTP header: no CR/LF may reach curl's -H.
TITLE="$(printf '%s' "$TITLE" | tr '\r\n' '  ')"
[ -n "$TITLE" ] || usage "--title is required"
if [ "$BODY_SET" = 0 ] && [ ! -t 0 ]; then BODY="$(head -c 4096)"; fi

# One key from .env, read as text, never sourced. Last assignment wins; strips a
# CR, an unquoted trailing comment and surrounding quotes (rustdesk-check's
# reader, moved here with the transport).
_env() {
    [ -r "$ENV_FILE" ] || return 0
    awk -v k="$1=" '{ sub(/\r$/, "") } index($0, k) == 1 { v = substr($0, length(k) + 1); f = 1 }
                    END { if (f) print v }' "$ENV_FILE" 2>/dev/null \
        | sed -e 's/[[:space:]]#.*$//' -e 's/[[:space:]]*$//' \
              -e 's/^"\(.*\)"$/\1/' -e "s/^'\\(.*\\)'\$/\\1/"
}

host="$(_env NTFY_BIND_IP)"; [ -n "$host" ] || host="$(_env LAN_IP)"
port="$(_env NTFY_PORT)"
topic="$(_env HOMEHUB_ALERT_TOPIC)"; topic="${topic:-homehub-alerts}"
if ! [[ "$host" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || ! [[ "$port" =~ ^[0-9]{1,5}$ ]] \
   || ! [[ "$topic" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
    log "ALERT NOT SENT (no usable NTFY_BIND_IP/LAN_IP, NTFY_PORT or topic in $ENV_FILE) [$KEY]: $TITLE"
    exit 4
fi
url="http://$host:$port/$topic"
stamp="$STATE_DIR/alerted-$KEY"
now="$(date +%s)"
last=0
if [ -r "$stamp" ]; then last="$(_int "$(cat "$stamp" 2>/dev/null)" 0)"; fi
if [ "$last" -gt $((now + 60)) ]; then
    log "ignoring a '$KEY' stamp $((last - now))s in the future (the clock moved backwards)"
    last=0
fi
if [ $((now - last)) -lt "$REPEAT_SEC" ]; then
    log "alert suppressed (a '$KEY' alert was delivered $((now - last))s ago): $TITLE"
    exit 3
fi
if ! command -v curl >/dev/null 2>&1; then
    log "ALERT NOT SENT (no curl) [$KEY]: $TITLE"
    exit 4
fi
if timeout "$((TIMEOUT + 2))" curl -fsS -o /dev/null --connect-timeout 3 --max-time "$TIMEOUT" \
        -H "Title: $TITLE" -H "Priority: $PRIO" -H "Tags: $TAGS" \
        --data-binary "$BODY" "$url" >/dev/null 2>&1; then
    log "alert delivered to $url [$KEY]: $TITLE"
    { mkdir -p "$STATE_DIR" && printf '%s\n' "$now" >"$stamp"; } 2>/dev/null \
        || log "WARN: could not write the rate-limit stamp $stamp; the next call will not be suppressed"
    exit 0
fi
log "ALERT NOT DELIVERED to $url (ntfy down or unreachable; this journal line is the record) [$KEY]: $TITLE"
exit 4
