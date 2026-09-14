#!/bin/bash
# Create the gateway's private state directory and install the ONE secret this
# repo is allowed to place there: the tracker feed token, delivered as a
# root-created 0600 file instead of renderer config.
#
# Implements: SR-016, SR-017, LLR-012.
#
# THE OTHER TWO FILES ARE NOT OURS. `state.key` and `state.json.enc` are created
# by the Owner running OfficeWallNaglight `gateway/setup.mjs`, which refuses to
# overwrite an existing output. This script therefore never writes them, never
# reads them, and refuses to run if it would disturb them.
#
# SAME SHAPE AS backup.env: a real value lives in a materialized private file
# outside the repo, and nothing tracked here ever carries one. The source file
# is an argument; a session must not invent it.
#
#   sudo bash install-gateway-state.sh --feed-token-file /run/private/feed-token
#
# Reversal: `rm -rf /var/lib/panel-access` destroys the PIN, the device registry
# and every enrolment with it. That is an Owner decision, not a cleanup step.
set -euo pipefail

STATE_DIR=/var/lib/panel-access
OWNER_UID=1000
OWNER_GID=1000
token_source=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --feed-token-file) token_source="${2:-}"; shift 2 ;;
        --state-dir)       STATE_DIR="${2:-}";    shift 2 ;;
        --uid)             OWNER_UID="${2:-}";    shift 2 ;;
        --gid)             OWNER_GID="${2:-}";    shift 2 ;;
        *) echo "FAIL unknown argument" >&2; exit 2 ;;
    esac
done

# THE PATH GUARD RUNS FIRST, BEFORE THE ROOT CHECK, so it is testable without
# root and so a mistyped --state-dir is refused rather than half-applied.
# A secret must never be written inside a checkout: --state-dir is an operator
# convenience for a lab VM, not a licence to put a feed token next to tracked
# files where the next `git add -A` sweeps it up.
case "$STATE_DIR" in
    /*) : ;;
    *) echo "FAIL --state-dir must be an absolute path" >&2; exit 1 ;;
esac
case "$STATE_DIR" in
    *..*) echo "FAIL --state-dir must not contain .." >&2; exit 1 ;;
esac
guard="$STATE_DIR"
while [ -n "$guard" ] && [ "$guard" != "/" ]; do
    if [ -e "$guard/.git" ]; then
        echo "FAIL --state-dir is inside a git checkout; a secret must not be written there" >&2
        exit 1
    fi
    guard="$(dirname "$guard")"
done

[ "$(id -u)" = "0" ] || { echo "FAIL run as root" >&2; exit 1; }
case "$OWNER_UID$OWNER_GID" in *[!0-9]*) echo "FAIL uid/gid must be numeric" >&2; exit 1 ;; esac

# The container runs as 1000:1000 and the gateway's requirePrivate() rejects any
# file with group or other bits. Ownership is what makes 0600 readable AT ALL,
# so it is asserted here rather than assumed from the image's defaults.
install -d -m 0700 -o "$OWNER_UID" -g "$OWNER_GID" "$STATE_DIR"
chmod 0700 "$STATE_DIR"
chown "$OWNER_UID:$OWNER_GID" "$STATE_DIR"

if [ -n "$token_source" ]; then
    [ -f "$token_source" ] || { echo "FAIL feed token source is not a regular file" >&2; exit 1; }
    [ -s "$token_source" ] || { echo "FAIL feed token source is empty" >&2; exit 1; }
    # Written through a temporary in the SAME directory so a reader never sees a
    # partial token, and never through a shell redirect that would put the value
    # on a command line or in history.
    temporary="$STATE_DIR/.feed-token.$$"
    trap 'rm -f -- "$temporary"' EXIT
    install -m 0600 -o "$OWNER_UID" -g "$OWNER_GID" "$token_source" "$temporary"
    # Trailing whitespace is stripped by the gateway; a trailing newline is fine.
    mv -f -- "$temporary" "$STATE_DIR/feed-token"
    trap - EXIT
    echo "PASS feed token installed 0600 ${OWNER_UID}:${OWNER_GID}"
else
    echo "NOTE no --feed-token-file given; directory prepared only"
fi

for name in state.key state.json.enc; do
    if [ -e "$STATE_DIR/$name" ]; then
        echo "NOTE $name already present; left untouched (Owner-owned)"
    else
        echo "NOTE $name absent; the Owner creates it with gateway/setup.mjs"
    fi
done

echo "PASS gateway state directory ready at $STATE_DIR (no PIN, key or credential written)"
