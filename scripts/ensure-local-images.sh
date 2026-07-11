#!/usr/bin/env bash
# ensure-local-images.sh — resolve every LOCALLY-BUILT image the stack consumes
# (Q10.2: no registry home yet). Implements: SR-006.
#
# The stack's own app images (NagLight today; Finance-Auditor when its service
# lands) live in PRIVATE sibling repos, which breaks "anyone can check this repo
# out and run the sim". This resolver makes the dev-package build permissive
# without changing what compose consumes — everything downstream (compose, the
# sim overlay, vmtest/export-images.sh) keeps using the same local ref
# (e.g. naglight:local); only HOW that ref gets onto the machine varies:
#
#   1. PRESENT   — the ref already exists in the docker store: no-op.
#   2. SIBLING   — a checkout exists at $SIBLING_ROOT/<Repo> (default: this
#                  repo's parent dir, the documented layout): docker build it.
#   3. PUBLIC    — a public image is DECLARED (e.g. TRACKER_PUBLIC_IMAGE in
#                  stack/.env — empty by default): docker pull + retag to the
#                  local ref. The knob stays empty until the app repo publishes.
#   4. otherwise — FAIL LOUDLY naming all three fixes (clone the sibling, set
#                  the public knob, or build/tag the image yourself).
#
# Usage:
#   bash scripts/ensure-local-images.sh              # ensure (skip refs that exist)
#   bash scripts/ensure-local-images.sh --rebuild    # force sibling rebuilds
#   bash scripts/ensure-local-images.sh --dry-run    # print decisions, change nothing
#   SIBLING_ROOT=/elsewhere bash scripts/ensure-local-images.sh
#
# Called automatically by sim/run-sim.sh; export-images.sh points here when the
# tracker image is missing. Needs the docker CLI (run inside WSL on the dev box).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
SIBLING_ROOT="${SIBLING_ROOT:-$(dirname "$REPO_ROOT")}"

REBUILD=0; DRY_RUN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --rebuild) REBUILD=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1 (try --help)" >&2; exit 2 ;;
    esac
done

log() { printf '[ensure-images] %s\n' "$*"; }
die() { printf '[ensure-images] ERROR: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker CLI not found (on the dev box, run inside WSL — see docs/status.md WI-10.13)"

# Public-fallback knobs live with every other knob: stack/.env, else the example
# (where the *_PUBLIC_IMAGE defaults are empty = "not defined"). Extract ONLY the
# keys we need instead of shell-sourcing the file: .env is a compose env file,
# not a shell script — an unquoted space in an unrelated value must not be able
# to break this resolver (the 2026-07-10 TRACKER_GIT_NAME finding).
ENV_FILE="$REPO_ROOT/stack/.env"
[ -f "$ENV_FILE" ] || ENV_FILE="$REPO_ROOT/stack/.env.example"

# env_get KEY : the last KEY= value in $ENV_FILE, surrounding quotes stripped.
env_get() {
    sed -n "s/^${1}=//p" "$ENV_FILE" | tail -n1 | sed 's/^"\(.*\)"$/\1/'
}
TRACKER_IMAGE_TAG="$(env_get TRACKER_IMAGE_TAG)"
TRACKER_PUBLIC_IMAGE="${TRACKER_PUBLIC_IMAGE:-$(env_get TRACKER_PUBLIC_IMAGE)}"
FINANCE_AUDITOR_IMAGE_TAG="$(env_get FINANCE_AUDITOR_IMAGE_TAG)"
FINANCE_AUDITOR_PUBLIC_IMAGE="${FINANCE_AUDITOR_PUBLIC_IMAGE:-$(env_get FINANCE_AUDITOR_PUBLIC_IMAGE)}"
ACTUAL_IMAGE_TAG="$(env_get ACTUAL_IMAGE_TAG)"

# ensure_image REF SIBLING_DIR PUBLIC_REF [docker-build args...] : resolve one
# local image via the present → sibling-build → declared-public chain; die
# loudly if none applies. Extra args (e.g. --build-arg) go to the sibling build.
ensure_image() {
    local ref="$1" sibling="$SIBLING_ROOT/$2" public="${3:-}"
    shift 3 || shift $#
    local build_args=("$@")

    if [ "$REBUILD" -eq 0 ] && docker image inspect "$ref" >/dev/null 2>&1; then
        log "present: $ref (skip; --rebuild to force)"
        return 0
    fi

    if [ -d "$sibling" ]; then
        log "sibling build: $ref  <-  $sibling ${build_args[*]:+(${build_args[*]})}"
        [ "$DRY_RUN" -eq 1 ] && { log "  (dry-run: would docker build)"; return 0; }
        docker build -t "$ref" "${build_args[@]}" "$sibling" || die "sibling build failed for $ref ($sibling)"
        return 0
    fi

    if [ -n "$public" ]; then
        log "public fallback: $ref  <-  $public (sibling $sibling not found)"
        [ "$DRY_RUN" -eq 1 ] && { log "  (dry-run: would docker pull + tag)"; return 0; }
        docker pull "$public" || die "public pull failed for $public"
        docker tag "$public" "$ref"
        return 0
    fi

    die "cannot resolve $ref — no sibling checkout at $sibling and no public image declared.
  Fix ONE of:
    - clone the app repo next to this one:        git clone <url> $sibling
    - declare a public image in stack/.env:       (e.g.) TRACKER_PUBLIC_IMAGE=ghcr.io/<owner>/<app>:<tag>
    - build/tag it yourself:                      docker build -t $ref <checkout>"
}

# ── the locally-built image set (one line per app repo) ───────────────────────
# NagLight — the tracker (SR-006). Tag follows TRACKER_IMAGE_TAG (default local).
ensure_image "naglight:${TRACKER_IMAGE_TAG:-local}" "NagLight" "${TRACKER_PUBLIC_IMAGE:-}"

# Finance-Auditor — the daily finance audit pipeline (SR-014, IF-004 ↔ FA
# IF-003). The IF-002 coupling: @actual-app/api inside the image MUST match the
# deployed actual-server, so the build arg is pinned to ACTUAL_IMAGE_TAG — when
# that pin bumps, re-run with --rebuild to rebuild this image against it.
ensure_image "finance-auditor:${FINANCE_AUDITOR_IMAGE_TAG:-local}" "Finance-Auditor" "${FINANCE_AUDITOR_PUBLIC_IMAGE:-}" \
    --build-arg "ACTUAL_API_VERSION=${ACTUAL_IMAGE_TAG:?ACTUAL_IMAGE_TAG missing from env file}"

log "all local images resolved."
