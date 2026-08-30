#!/usr/bin/env bash
# scripts/lib/envfile.sh — ONE literal reader for stack/.env, sourced by every
# dev-box script that has to answer a question about it. Not standalone.
#
# WHY THIS FILE EXISTS, and it is a two-file bug of exactly the shape this repo
# keeps paying for. `scripts/ensure-local-images.sh` decides which LOCAL IMAGES
# TO BUILD from the env file; `vmtest/export-images.sh` decides which images to
# BAKE INTO THE ISO from the same file. They had two different readers:
#
#     ensure-local-images:  sed -n "s/^KEY=//p"                  <- column 1 only
#     export-images:        sed -n 's/^[[:space:]]*KEY[[:space:]]*=...'
#
# so a line written `  GAME_RELAY_ENABLED=true` — one leading space, which .env
# permits and compose accepts — made the EXPORTER add `--profile gunmaster3` and
# demand `gunmaster3-relay:local`, while the RESOLVER read nothing, concluded
# "off", and never built it. The build then dies on a missing image, or worse
# bakes an ISO whose relay can never start. Same file in, two answers out, and
# the disagreement is invisible until a build. (codex gpt-5.6-sol, round 6.)
#
# THE INVARIANT THIS FILE OWNS: **every dev-box consumer of stack/.env parses it
# the same way.** If you need a new reader, extend this one; do not write a
# second `sed -n` anywhere.
#
# THE SEMANTICS ARE THE HOUSE ONES, matched deliberately to the two readers that
# already run ON THE BOX — firstboot.sh's `env_value` and verify-hub.sh's
# `load_env_file` — so the dev PC, the first boot and the verifier all agree
# about what a line means:
#
#   * leading whitespace before the key is allowed, and around the `=`;
#   * the LAST assignment wins, which is what compose does;
#   * surrounding single or double quotes are stripped (the quotes are not part
#     of the value);
#   * an UNQUOTED trailing ` # comment` is stripped, as shell sourcing and
#     compose's own .env parser both do — `.env` documents values inline
#     (`LAN_IP=0.0.0.0   # VMTEST: …`). A `#` with NO space before it stays: it
#     may be part of a password;
#   * a trailing CR is stripped, so a file that has been through a Windows
#     editor does not yield values with an invisible \r on the end;
#   * `$$` collapses back to `$`: compose stores a literal dollar that way
#     because it interpolates .env values.
#
# NOTHING IS SOURCED OR EXECUTED. .env is a compose env file, not a shell
# script: every value is literal text, and `source`ing it makes bash expand the
# bcrypt hashes in it (`$2a$14$…`), which under `set -u` aborts the caller and
# with `-u` off silently corrupts the value.
#
# This file defines ONE function and no log/die helpers, on purpose: it is
# sourced by scripts that have their own, with different prefixes, and a shared
# helper file that clobbers its caller's `die` is its own kind of two-file bug.

# env_file_value FILE KEY — print the effective literal value, or nothing.
env_file_value() {
    local __f="$1" __k="$2" __v
    [ -f "$__f" ] || return 0
    __v="$(sed -n "s/^[[:space:]]*${__k}[[:space:]]*=//p" "$__f" | tail -n1)"
    __v="${__v%$'\r'}"
    # Leading space after the `=` is not part of the value either.
    __v="${__v#"${__v%%[![:space:]]*}"}"
    case "$__v" in
        \"*\") __v="${__v#\"}"; __v="${__v%\"}" ;;
        \'*\') __v="${__v#\'}"; __v="${__v%\'}" ;;
        *) __v="${__v%%[[:space:]]#*}"
           __v="${__v%"${__v##*[![:space:]]}"}" ;;
    esac
    __v="${__v//\$\$/\$}"
    printf '%s' "$__v"
}
