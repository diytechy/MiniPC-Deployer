#!/usr/bin/env bash
# reap-orphan-sesman.sh — make a wedged RDP layer self-heal.
#
# THE FAILURE THIS EXISTS TO CLEAR (HomeHub open-items C59). Restarting
# xrdp-sesman destroys its in-memory session table. If a desktop session is live
# at that moment the OLD sesman does not exit — it still has a child — so it
# detaches to PPID 1 and keeps running, while systemd starts a NEW sesman beside
# it. The new one cannot see the old session, so every later RDP connection
# starts a SECOND session whose xfce4-session collides with the orphan on the
# shared user bus and exits after one second. The operator sees "the session
# almost starts, then closes"; the log blames the window manager.
#
# THE ORPHAN NEVER EXPIRES. There is no timeout and no reaper in xrdp, so the
# wedge lasts until a human finds it — which on 2026-08-31 took ~18 hours and two
# missed scheduled windows, and the visible error pointed two days away from the
# actual cause. This script is that missing reaper.
#
# WHAT COUNTS AS AN ORPHAN, and the test is exact rather than heuristic:
#   * process name xrdp-sesman, AND
#   * PPID == 1  (it has been reparented to init), AND
#   * PID != the MainPID systemd currently tracks for xrdp-sesman.service
#
# A per-session sesman forked by the LIVE sesman has that live sesman as its
# parent, not init, so it is never matched. Measured on the real hub: main
# sesman 1845521 (PPID 1, == MainPID) with per-session child 1950618 (PPID
# 1845521). Only the first shape survives a restart to become an orphan.
#
# WHY IT IS SAFE TO KILL: an orphaned sesman OWNS A SESSION NOTHING CAN REACH.
# Reconnect is a lookup in the live sesman's table, and the orphan is not in it,
# so that desktop can never be attached to again by any client. Killing it costs
# only whatever was left running inside it — and that is precisely why this runs
# as ExecStartPre of the session service rather than on a timer: it fires when a
# session is about to be created, not at an arbitrary moment.
#
# REFUSES TO ACT WHEN IT CANNOT TELL. If xrdp-sesman.service is not active, every
# top-level sesman would look like an orphan — but that is also what a deliberate
# `systemctl stop` looks like. No MainPID to compare against means no reaping.
#
# Exits 0 ALWAYS. It is a best-effort cleanup in front of the session service;
# a failure here must never be the reason the desktop does not come up.
set -uo pipefail

log() { echo "[reap-sesman] $*"; }

state="$(systemctl is-active xrdp-sesman.service 2>/dev/null || true)"
if [ "$state" != "active" ]; then
    log "xrdp-sesman is '${state:-unknown}', not active — refusing to reap (cannot tell an orphan from a deliberate stop)"
    exit 0
fi

main="$(systemctl show xrdp-sesman.service -p MainPID --value 2>/dev/null || echo 0)"
if [ -z "$main" ] || [ "$main" = "0" ]; then
    log "xrdp-sesman has no MainPID — refusing to reap (nothing to compare against)"
    exit 0
fi

# Every descendant of $1, deepest first, so children die before their parents.
descendants() {
    local p="$1" k
    for k in $(pgrep -P "$p" 2>/dev/null || true); do
        descendants "$k"
        echo "$k"
    done
}

found=0
for p in $(pgrep -x xrdp-sesman 2>/dev/null || true); do
    [ "$p" != "$main" ] || continue
    ppid="$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')"
    [ "$ppid" = "1" ] || continue

    found=$((found + 1))
    log "ORPHAN xrdp-sesman $p (PPID 1, and not MainPID $main) — reaping its unreachable session"
    tree="$(descendants "$p" | tr '\n' ' ')$p"
    log "  tree: $tree"
    # shellcheck disable=SC2086
    kill -TERM $tree 2>/dev/null || true
    sleep 3
    # shellcheck disable=SC2086
    kill -KILL $tree 2>/dev/null || true
done

if [ "$found" -eq 0 ]; then
    log "no orphaned sesman — nothing to do"
else
    log "reaped $found orphaned sesman process(es); RDP will mint a clean session on the next connect"
fi
exit 0
