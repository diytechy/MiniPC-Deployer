#!/bin/sh
# Product launcher (macOS) — the double-clickable Finder wrapper. The POSIX
# launch command lives once, in run.sh (edit RUN_CMD there); this file only
# hops to its own directory so double-click works from anywhere.
cd "$(dirname "$0")" || exit 1
exec ./run.sh "$@"
