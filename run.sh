#!/bin/sh
# Product launcher (POSIX) — run this project with no commands to remember.
# Every launchable project ships run.cmd / run.sh / run.command (process.md §7,
# "the evaluator's rungs"). Read it first; it only runs the one command below.
# macOS: run.command is the double-clickable Finder wrapper around this file.
#
# Not applicable (a pure library)? Delete the run.* launchers and describe
# usage in README.md instead.

# --- EDIT FOR YOUR PROJECT ----------------------------------------------------
# The command that starts the product, run from the repo root. Examples:
#   RUN_CMD="python -m yourapp"
#   RUN_CMD="go run ./cmd/yourapp serve"
#   RUN_CMD="npm start"
# Keep run.cmd's RUN_CMD in sync — it is the Windows twin; the command lives
# exactly twice: here and there.
RUN_CMD=""
# ------------------------------------------------------------------------------

cd "$(dirname "$0")" || exit 1
if [ -z "$RUN_CMD" ]; then
  echo "run.sh: no launch command wired yet." >&2
  echo "Edit RUN_CMD in this file and in run.cmd; the README 'Run it' section" >&2
  echo "documents the underlying command." >&2
  exit 1
fi
echo "Running: $RUN_CMD $*"
# Word-splitting of RUN_CMD is intentional: it is a command plus its arguments.
exec $RUN_CMD "$@"
