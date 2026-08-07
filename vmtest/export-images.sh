#!/usr/bin/env bash
# vmtest/export-images.sh — Q10.9 B+ (ALL-IMAGES).
#
# docker-save EVERY stack image into the gitignored deploy-payload staging area
# so a freshly-imaged AWOW comes up with every container "from infancy": zero
# registry/internet dependency for container images at first boot, versions
# pinned to exactly what the homehub-sim validated (Q10.9 B+ ALL-IMAGES, LOCKED IN
# by the Owner 2026-07-04, see HOMELAB_RESTRUCTURE_PLAN.md).
#
# WHAT IT DOES
#   1. Resolves the full image set from stack/docker-compose.yml + the PINNED
#      tags in the env file (default stack/.env.example — the committed pin set)
#      via `docker compose config --images` (includes the ntfy profile).
#   2. For each image: uses the local copy if present at the pinned tag, else
#      `docker pull`s it at that PINNED tag. Fails LOUDLY if any image is
#      missing/unpullable (never a silent partial payload).
#   3. `docker save`s each into $IMAGES_OUT as one plain .tar per image, and
#      writes an images.manifest.tsv (ref / id / digest / file / bytes) that
#      doubles as the audit record. Reports the total payload size.
#
# naglight:local is SPECIAL — the locally-built tracker image (no registry home,
# Q10.2). It must already exist locally (build it first:
#   docker build -t naglight:local ../NagLight ). This script will NOT try to
# pull it and FAILS LOUDLY if it is absent — that is the one image the AWOW can
# never fetch from anywhere, so it MUST be in the payload.
#
# COMPRESSION — plain .tar, no zstd (measured, justified): `docker save` under
# Docker's containerd/OCI image store already writes COMPRESSED layer blobs into
# the tar (e.g. actual-server: a 526MB image saves to a ~105MB tar). A second
# zstd pass over already-compressed blobs buys almost nothing and would add a
# machine-level `apt-get install zstd` dependency. If you ever want it anyway and
# zstd is on PATH, pass --zstd (firstboot.sh transparently loads *.tar.zst too).
#
# WHY PER-IMAGE (not one combined tar): composable + idempotent (only re-save a
# tag whose tar is missing), per-image load logging + graceful per-image degrade
# in firstboot, and you can stage/copy a subset. `docker load` is a no-op for an
# image that already exists, so re-runs are cheap and safe.
#
# Usage:
#   bash vmtest/export-images.sh                 # -> vmtest/.out/images/*.tar
#   ENV_FILE=stack/.env bash vmtest/export-images.sh   # pin from the real .env
#   IMAGES_OUT=/mnt/d/homehub-images bash vmtest/export-images.sh
#   bash vmtest/export-images.sh --force         # re-save even if a tar exists
#   bash vmtest/export-images.sh --zstd          # compress (only if zstd present)
#
# Output (gitignored, see ../.gitignore vmtest/.out/):
#   $IMAGES_OUT/<sanitized-ref>.tar          one docker-save tar per image
#   $IMAGES_OUT/images.manifest.tsv          ref / id / repo-digest / file / bytes
#
# NOTE ON PATHS: the default $IMAGES_OUT lives under vmtest/.out, i.e. on the
# Windows C: drive via the WSL 9p mount — writing there does NOT grow WSL2's
# ext4.vhdx (the OI-6 gotcha). Point IMAGES_OUT at /mnt/d/... if C: is tight.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

REPO_ROOT="$(repo_root)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/stack/.env.example}"
COMPOSE_FILE="$REPO_ROOT/stack/docker-compose.yml"
IMAGES_OUT="${IMAGES_OUT:-$REPO_ROOT/vmtest/.out/images}"
FORCE=0
USE_ZSTD=0

while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        --zstd)  USE_ZSTD=1; shift ;;
        --env-file) ENV_FILE="$2"; shift 2 ;;
        -h|--help) sed -n '2,60p' "$0"; exit 0 ;;
        *) die "unknown argument '$1' (try --help)" ;;
    esac
done

# Resolve a possibly-relative ENV_FILE against the repo root for a friendlier msg.
case "$ENV_FILE" in /*) : ;; *) ENV_FILE="$REPO_ROOT/$ENV_FILE" ;; esac

require_cmd docker "Install Docker Engine in WSL (see docs/status.md WI-10.13)."
[ -f "$COMPOSE_FILE" ]  || die "not found: $COMPOSE_FILE (run from a MiniPC-Deployer checkout)"
[ -f "$ENV_FILE" ]      || die "not found: $ENV_FILE"
if [ "$USE_ZSTD" -eq 1 ] && ! command -v zstd >/dev/null 2>&1; then
    die "--zstd requested but zstd is not installed (sudo apt-get install -y zstd), or drop --zstd for plain tars"
fi

# The saved tars are small per-image but sum to ~1GB; ask for a little headroom.
require_free_gb "$IMAGES_OUT" 3

# ── 1. resolve the full pinned image set (compose is the source of truth) ─────
# THE PROFILES ARE READ FROM THE .env BEING BAKED. They used to be hardcoded as
# `(--profile ntfy)`, and that one line defeated the whole offline install.
#
# WHAT HAPPENED, 2026-08-07. The materialised hub .env carries
#     COMPOSE_PROFILES=ntfy,immich,immich-ml,jellyfin,finance-auditor
# so the box starts five profiles' worth of services. This script asked compose
# "which images do I need with ONLY the ntfy profile on?", got 9, baked 9, and
# shipped. On first boot `docker compose up -d` therefore needed six images that
# were never on the stick — immich-server, immich-ml, immich-db, valkey,
# jellyfin, finance-auditor — and went to registry-1.docker.io for them.
#
# The 2026-08-06 work exists to make an install need NO NETWORK. This made the
# BRING-UP need one, which is the same promise broken one step later. And it was
# not a graceful degrade: `compose up -d` is all-or-nothing, so the failed
# jellyfin pull took the whole command down, homehub-firstboot.service exited 1,
# and DNS, Caddy, the tracker and Actual never started at all. One unreachable
# OPTIONAL image stopped the entire core stack.
#
# NOTE WHAT WAS NOT WRONG. The refusal machinery below is sound and always was —
# a missing local-only image and an unpullable tag are both `die`, not warnings.
# It was being asked the wrong question. Deriving the answer from the same file
# the box will use means drift is now a REFUSED BUILD, where someone can act on
# it, rather than a failed first boot on a machine that may be on a wall.
#
# EXTRA_PROFILES still adds to the set, for baking something the .env does not
# yet enable. ntfy stays in unconditionally: it is the historical default and
# omitting it would silently shrink an ISO whose .env predates this change.
PROFILE_ARGS=(--profile ntfy)
ENV_PROFILES="$(sed -n 's/^[[:space:]]*COMPOSE_PROFILES[[:space:]]*=[[:space:]]*//p' "$ENV_FILE" \
                | tail -n1 | tr -d '"'\''' | tr ',' ' ')"
for p in $ENV_PROFILES ${EXTRA_PROFILES:-}; do
    case " ${PROFILE_ARGS[*]} " in *" $p "*) continue ;; esac
    PROFILE_ARGS+=(--profile "$p")
done
if [ -n "$ENV_PROFILES" ]; then
    log "COMPOSE_PROFILES in ${ENV_FILE##*/} enables: $ENV_PROFILES"
    log "  every one of them is baked; a profile whose image cannot be resolved is a REFUSED BUILD"
else
    log "COMPOSE_PROFILES is empty in ${ENV_FILE##*/} — baking the core set plus ntfy only"
fi

log "resolving image set from $(basename "$COMPOSE_FILE") with pins from ${ENV_FILE#$REPO_ROOT/}"
mapfile -t IMAGES < <(
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "${PROFILE_ARGS[@]}" config --images \
        | sed '/^\s*$/d' | sort -u
)
[ "${#IMAGES[@]}" -gt 0 ] || die "docker compose config --images returned nothing — is the env file complete?"

log "the AWOW will ship these ${#IMAGES[@]} images from infancy (Q10.9 B+):"
for ref in "${IMAGES[@]}"; do log "    - $ref"; done

mkdir -p "$IMAGES_OUT"
MANIFEST="$IMAGES_OUT/images.manifest.tsv"
printf 'repo_tag\timage_id\trepo_digest\tsaved_file\tbytes\n' > "$MANIFEST"

# sanitize an image ref into a filename: registry/repo:tag -> registry_repo_tag
sanitize() { echo "$1" | tr '/:@' '___'; }

total_bytes=0
saved_count=0
pulled_count=0

# ── 2. ensure each image is present locally (pull at the PINNED tag if not) ────
# sibling_repo_for REF — the app repo a locally-built image is built from, or ''.
# Same pairs scripts/ensure-local-images.sh declares; keep them in step.
sibling_repo_for() {
    case "${1%%:*}" in
        naglight)        echo "NagLight" ;;
        finance-auditor) echo "Finance-Auditor" ;;
        *)               echo "" ;;
    esac
}

# assert_local_image_fresh REF — refuse to bake an app image older than its source.
#
# THE TRAP THIS CLOSES (found by the 2026-08-01 V3 gate): a `*:local` image has no
# registry and no version in its tag, and every resolver in this repo treats
# "present" as "done" — ensure-local-images.sh skips it, this script saves it. So
# whatever vintage happens to sit in the dev PC's docker cache is what gets baked
# into an ISO you then flash and keep for months. That gate booted a
# finance-auditor image 19 days older than its repo HEAD, from BEFORE the A8
# auto-discovery work, and it crash-looped on a knob the current source does not
# even require. Nothing anywhere said the image was stale.
#
# COMPARES COMMIT SHAs, NOT TIMESTAMPS. The first cut of this check compared
# .Created against the sibling's HEAD date and was wrong: a cache-identical
# rebuild reuses the existing image record and keeps its ORIGINAL .Created, so a
# freshly rebuilt image still reported as stale. ensure-local-images.sh now
# stamps homehub.source.revision at build time; that is exact.
#
# Skipped (with a note) when the sibling checkout is absent — a public-image or
# no-checkout run is legitimate. An UNSTAMPED image (hand-built with plain
# `docker build`) warns loudly rather than failing: it may well be current, but
# nothing can prove it, and silence would be the very failure being fixed.
assert_local_image_fresh() {
    local ref="$1"
    local repo; repo="$(sibling_repo_for "$ref")"
    [ -n "$repo" ] || return 0
    local dir="$REPO_ROOT/../$repo"
    if [ ! -d "$dir/.git" ]; then
        log "  (no $repo checkout beside this repo — cannot verify $ref against its source)"
        return 0
    fi

    local stamped head
    stamped="$(docker image inspect "$ref" --format '{{index .Config.Labels "homehub.source.revision"}}' 2>/dev/null)"
    head="$(git -C "$dir" rev-parse HEAD 2>/dev/null || echo '')"

    if [ -z "$stamped" ] || [ "$stamped" = "<no value>" ]; then
        log "  WARNING: $ref carries no homehub.source.revision label — cannot prove it matches $repo."
        log "           It was built by hand rather than through scripts/ensure-local-images.sh."
        log "           Rebuild through the resolver to make this verifiable:"
        log "             bash scripts/ensure-local-images.sh --rebuild"
        return 0
    fi
    case "$stamped" in
        *'+dirty')
            log "  NOTE: $ref was built from a DIRTY $repo tree (${stamped%+dirty} + uncommitted changes)."
            log "        Baking it is fine for a gate run; commit before a real flash so the box is reproducible."
            return 0 ;;
    esac
    if [ -n "$head" ] && [ "$stamped" != "$head" ]; then
        die "STALE local image '$ref' — built from ${stamped:0:12}, but $repo HEAD is ${head:0:12}." \
            "  HEAD: $(git -C "$dir" log -1 --format='%h %cs %s' 2>/dev/null)" \
            "This image has no registry and no version in its tag, so nothing else will ever notice." \
            "Baking it means flashing a box with an app build older than its source." \
            "Rebuild first:  bash scripts/ensure-local-images.sh --rebuild" \
            "(Deliberately shipping an older build? Export with ALLOW_STALE_LOCAL=1.)"
    fi
    log "  source-check OK: $ref built from $repo ${stamped:0:12} (= HEAD)"
}

for ref in "${IMAGES[@]}"; do
    if docker image inspect "$ref" >/dev/null 2>&1; then
        log "present locally: $ref"
        [ "${ALLOW_STALE_LOCAL:-0}" = "1" ] || assert_local_image_fresh "$ref"
    else
        case "$ref" in
            naglight:*|*:local)
                die "MISSING local-only image '$ref' — it has no registry home (Q10.2)." \
                    "Resolve it first:  bash scripts/ensure-local-images.sh" \
                    "(present -> sibling build ../NagLight -> declared TRACKER_PUBLIC_IMAGE)" \
                    "then re-run this script. The AWOW cannot fetch this image anywhere," \
                    "so it MUST be baked into the payload."
                ;;
        esac
        log "pulling (not local yet) at the pinned tag: $ref"
        docker pull "$ref" >/dev/null 2>&1 \
            || die "docker pull FAILED for '$ref' — unpullable/missing tag." \
                   "Fix the pin in $ENV_FILE or your network, then re-run." \
                   "Refusing to build a partial payload."
        pulled_count=$((pulled_count + 1))
    fi
done

# ── 3. docker save each image into the staging dir ────────────────────────────
for ref in "${IMAGES[@]}"; do
    base="$(sanitize "$ref")"
    if [ "$USE_ZSTD" -eq 1 ]; then
        out="$IMAGES_OUT/$base.tar.zst"
    else
        out="$IMAGES_OUT/$base.tar"
    fi

    # A tar is reusable only when its filename pins the content. For a registry
    # image the tag does that. For a `*:local` build it does NOT — the tag is
    # constant across every rebuild, so "already saved" would keep shipping the
    # tar from whichever build happened first, even after the source-check above
    # confirms the IMAGE is current. Same 2026-08-01 stale-payload trap, one
    # layer down: re-save these every run (they are the two smallest images).
    local_only=0
    case "$ref" in *:local|naglight:*) local_only=1 ;; esac

    if [ -f "$out" ] && [ "$FORCE" -eq 0 ] && [ "$local_only" -eq 0 ]; then
        log "skip (already saved, --force to redo): $(basename "$out")"
    else
        [ "$local_only" -eq 1 ] && [ -f "$out" ] && log "re-saving locally-built $ref (its tag pins no content)"
        log "docker save $ref -> $(basename "$out")"
        if [ "$USE_ZSTD" -eq 1 ]; then
            docker save "$ref" | zstd -q -3 -f -o "$out"
        else
            # save to a .part then rename, so an interrupted save never leaves a
            # truncated tar that firstboot would try to load.
            docker save "$ref" -o "$out.part"
            mv -f "$out.part" "$out"
        fi
        saved_count=$((saved_count + 1))
    fi

    bytes=$(stat -c%s "$out")
    total_bytes=$((total_bytes + bytes))
    img_id="$(docker image inspect "$ref" --format '{{.Id}}' 2>/dev/null || echo '?')"
    repo_digest="$(docker image inspect "$ref" --format '{{range .RepoDigests}}{{.}} {{end}}' 2>/dev/null | awk '{print $1}')"
    [ -n "$repo_digest" ] || repo_digest='(local-build, no registry digest)'
    printf '%s\t%s\t%s\t%s\t%s\n' "$ref" "$img_id" "$repo_digest" "$(basename "$out")" "$bytes" >> "$MANIFEST"
done

# ── 4. report ─────────────────────────────────────────────────────────────────
total_mb=$((total_bytes / 1024 / 1024))
log "───────────────────────────────────────────────────────────────────"
log "DONE — ${#IMAGES[@]} images in the payload (${pulled_count} pulled, ${saved_count} saved this run)"
log "total payload size: ${total_mb} MB (${total_bytes} bytes) in $IMAGES_OUT"
log "manifest (ref/id/digest/file/bytes): $MANIFEST"
log "Next: build-seed.sh / build-repacked-iso.sh copy these into deploy-payload/images/;"
log "      firstboot.sh docker-loads them before 'docker compose up -d'."
