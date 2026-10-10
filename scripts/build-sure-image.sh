#!/usr/bin/env bash
# build-sure-image.sh - build the Owner's FORK of Sure into the local image the
# stack consumes (SURE_IMAGE, default sure-auroleap:local).
#
# The fork lives in the sibling checkout ../sure (C:\Projects\sure). This builds a
# NAMED branch or commit from it - not whatever the working tree happens to hold -
# by streaming `git archive <ref>` into `docker build`. The checkout is never
# touched (it is often on a detached HEAD or a feature branch), and uncommitted
# edits can never leak into an image that gets baked into an ISO.
#
# RUN IT IN WSL WITH REAL DOCKER, never against the default distro:
#     wsl -d Ubuntu -- bash /mnt/c/Projects/MiniPC-Deployer/scripts/build-sure-image.sh
# (the default WSL distro is a Podman alias that mis-tags saved tars.)
#
# Usage:
#   bash scripts/build-sure-image.sh                          # the ref in SURE_SOURCE_REF
#   bash scripts/build-sure-image.sh --ref auroleap-transaction-flags
#   bash scripts/build-sure-image.sh --ref <sha> --tag sure-auroleap:local
#   bash scripts/build-sure-image.sh --dry-run                # print the plan, build nothing
#
# Stamps (read by vmtest/export-images.sh's freshness check):
#   homehub.source.revision  the full commit sha built (what the NagLight image carries)
#   homehub.source.ref       the ref name asked for, so the check compares against THAT
#                            ref's current tip rather than the checkout's HEAD
#   BUILD_COMMIT_SHA         Sure's own Dockerfile build arg (shown by the app)
#
# THE REF: --ref, else the SURE_SOURCE_REF environment variable, else the
# SURE_SOURCE_REF knob in the env file (ENV_FILE, default stack/.env then
# stack/.env.example - auroleap-integration, the branch merging both fork branches).
# There is deliberately no hard-coded fallback: a build from the wrong branch is
# the failure the exporter's ref check exists to catch.
#
# Existing images and tags are never removed; the target tag is simply re-pointed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
SIBLING_ROOT="${SIBLING_ROOT:-$(dirname "$REPO_ROOT")}"
# shellcheck source=lib/git-worktree.sh
. "$SCRIPT_DIR/lib/git-worktree.sh"
# shellcheck source=lib/envfile.sh
. "$SCRIPT_DIR/lib/envfile.sh"

log() { printf '[build-sure] %s\n' "$*"; }
die() { printf '[build-sure] ERROR: %s\n' "$*" >&2; exit 1; }

ENV_FILE="${ENV_FILE:-$REPO_ROOT/stack/.env}"
[ -f "$ENV_FILE" ] || ENV_FILE="$REPO_ROOT/stack/.env.example"
REF="${SURE_SOURCE_REF:-}"
[ -n "$REF" ] || REF="$(env_file_value "$ENV_FILE" SURE_SOURCE_REF | tr -d '[:space:]')"
TAG="${SURE_IMAGE:-}"
[ -n "$TAG" ] || TAG="$(env_file_value "$ENV_FILE" SURE_IMAGE | tr -d '[:space:]')"
TAG="${TAG:-sure-auroleap:local}"
DRY_RUN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --ref) REF="${2:?--ref needs a branch or commit}"; shift 2 ;;
        --tag) TAG="${2:?--tag needs an image ref}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
        *) die "unknown argument '$1' (try --help)" ;;
    esac
done

[ -n "$REF" ] || die "no source ref: pass --ref, or set SURE_SOURCE_REF (env or ${ENV_FILE##*/})"
case "$TAG" in
    *@*) die "--tag '$TAG' contains '@': a digest form cannot be restored by docker load." ;;
esac

SURE_DIR="$SIBLING_ROOT/sure"
repo_git "$SURE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || die "no Sure checkout at $SURE_DIR (clone the fork next to this repo)"
SHA="$(repo_git "$SURE_DIR" rev-parse --verify --quiet "$REF^{commit}")" \
    || die "ref '$REF' does not resolve to a commit in $SURE_DIR"
SUBJECT="$(repo_git "$SURE_DIR" log -1 --format='%h %cs %s' "$SHA" 2>/dev/null || true)"

log "ref:    $REF"
log "commit: $SHA  ($SUBJECT)"
log "tag:    $TAG"
if [ "$DRY_RUN" -eq 1 ]; then
    log "(dry-run: would git archive $SHA | docker build -t $TAG -)"
    exit 0
fi

command -v docker >/dev/null 2>&1 || die "docker CLI not found (run inside WSL: wsl -d Ubuntu -- bash $0)"
docker info >/dev/null 2>&1 || die "docker daemon not reachable"
# REAL PODMAN GUARD. The default WSL distro's `docker` is a Podman alias that
# mis-tags saved tars; refuse it on what the daemon and CLI SAY they are.
_platform="$(docker version --format '{{.Server.Platform.Name}}' 2>/dev/null || true)"
_cli="$(docker --version 2>&1 || true)"
case "$(printf '%s %s' "$_platform" "$_cli" | tr '[:upper:]' '[:lower:]')" in
    *podman*) die "this docker is Podman ('$_platform' / '$_cli'). Use real Docker: wsl -d Ubuntu -- bash $0" ;;
esac

# autocrlf off for the archive: the image must contain the committed bytes, not
# whatever line-ending policy the Windows checkout applies.
repo_git "$SURE_DIR" -c core.autocrlf=false archive --format=tar "$SHA" \
    | docker build -t "$TAG" \
        --label "homehub.source.revision=$SHA" \
        --label "homehub.source.ref=$REF" \
        --build-arg "BUILD_COMMIT_SHA=$SHA" \
        - \
    || die "docker build failed for $TAG"

ID="$(docker image inspect "$TAG" --format '{{.Id}}')"
log "built $TAG = $ID"
log "stamped homehub.source.revision=${SHA:0:12} homehub.source.ref=$REF"
log "next: bash vmtest/export-images.sh   (re-saves it; the manifest records this id)"
