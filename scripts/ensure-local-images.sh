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
#
# OVERRIDABLE, and it has to be (added 2026-08-29 with the crossplay relay).
# vmtest/export-images.sh ALREADY takes `ENV_FILE=stack/.env` as a parameter and
# derives its bake set from that file's COMPOSE_PROFILES **and its
# GAME_RELAY_ENABLED**. This resolver decides which LOCAL IMAGES TO BUILD from
# the same knobs — so if the two read different files they disagree about what
# is on, and the disagreement is silent: the exporter demands an image the
# resolver never built. Same env file in, same answer out. When the relay's
# enable knob moved out of COMPOSE_PROFILES (the lifecycle refresh, same day)
# BOTH SIDES MOVED IN THE SAME COMMIT, for exactly this reason.
ENV_FILE="${ENV_FILE:-$REPO_ROOT/stack/.env}"
[ -f "$ENV_FILE" ] || ENV_FILE="$REPO_ROOT/stack/.env.example"

# env_get KEY : the effective value of KEY in $ENV_FILE.
#
# ONE READER, SHARED WITH vmtest/export-images.sh (round 6, codex gpt-5.6-sol).
# This used to be a local `sed -n "s/^KEY=//p"` that matched only a COLUMN-1
# key, while the exporter's own reader tolerated leading whitespace — so a line
# written `  GAME_RELAY_ENABLED=true` made the exporter bake the relay image and
# made THIS script skip building it. Same file, two answers, and nothing says so
# until a build fails on a missing image. The parser now lives in
# scripts/lib/envfile.sh and both scripts call it; see that file's header for
# the exact semantics and why they match firstboot.sh's `env_value`.
# shellcheck source=lib/envfile.sh
. "$SCRIPT_DIR/lib/envfile.sh"
env_get() { env_file_value "$ENV_FILE" "$1"; }
TRACKER_IMAGE_TAG="$(env_get TRACKER_IMAGE_TAG)"
TRACKER_PUBLIC_IMAGE="${TRACKER_PUBLIC_IMAGE:-$(env_get TRACKER_PUBLIC_IMAGE)}"
FINANCE_AUDITOR_IMAGE_TAG="$(env_get FINANCE_AUDITOR_IMAGE_TAG)"
FINANCE_AUDITOR_PUBLIC_IMAGE="${FINANCE_AUDITOR_PUBLIC_IMAGE:-$(env_get FINANCE_AUDITOR_PUBLIC_IMAGE)}"
ACTUAL_IMAGE_TAG="$(env_get ACTUAL_IMAGE_TAG)"
FILEBACKUP_IMAGE_TAG="$(env_get FILEBACKUP_IMAGE_TAG)"

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
        # SOURCE STAMP (2026-08-01): label the image with the commit it was built
        # from, so vmtest/export-images.sh can refuse to bake a build older than
        # its source. A `*:local` image has no registry and no version in its
        # tag, so without this there is NOTHING that distinguishes a current
        # build from one three weeks old — and the V3 gate booted exactly that.
        # Timestamps do NOT work here: a cache-identical rebuild reuses the
        # existing image record and keeps its original .Created, so an image that
        # was just rebuilt still looks stale. The commit sha is exact.
        # RESOLVE THE REPO ROOT, do not test for `$sibling/.git`. CHANGED
        # 2026-08-29 (adversarial review finding 3): the old test only worked
        # when the build context WAS the repo root, which held for NagLight,
        # Finance-Auditor and FileBackup and does NOT hold for
        # `FinnsGame/server` — the relay's Dockerfile sits in a subdirectory, so
        # `$sibling/.git` is absent and every relay image would have stamped
        # `unknown`. An unstamped image is precisely the vintage-blind case the
        # stamp exists to prevent, and export-images.sh only WARNS on one, so it
        # would have baked stale bytes quietly.
        #
        # `git rev-parse --show-toplevel` answers from any depth inside a
        # checkout, so this is correct for both shapes and needs no special case.
        local rev='unknown' dirty='' gitroot=''
        gitroot="$(git -C "$sibling" rev-parse --show-toplevel 2>/dev/null || true)"
        if [ -n "$gitroot" ]; then
            rev="$(git -C "$gitroot" rev-parse HEAD 2>/dev/null || echo unknown)"
            # --show-toplevel succeeded, so dirtiness is a real answer, not a
            # missing-repo one. Scope it to the BUILD CONTEXT rather than the
            # whole repo: an edit to FinnsGame's game code does not make the
            # relay image stale, and flagging it would train people to ignore
            # the marker.
            # `status --porcelain -- .` not `diff --quiet HEAD -- <abs path>`.
            # With -C already inside the context, the absolute pathspec matched
            # NO TRACKED FILES, so the check always said clean and a modified
            # tree was stamped with a clean HEAD (review finding 4). `-- .` is
            # relative to -C and therefore correct for both shapes, and status
            # also sees UNTRACKED files - which `diff` never does and which are
            # build inputs like any other.
            [ -z "$(git -C "$sibling" status --porcelain --untracked-files=normal -- . 2>/dev/null)" ] || dirty='+dirty'
        fi
        docker build -t "$ref" \
            --label "homehub.source.revision=${rev}${dirty}" \
            "${build_args[@]}" "$sibling" || die "sibling build failed for $ref ($sibling)"
        log "  stamped homehub.source.revision=${rev:0:12}${dirty}"
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

# FileBackup — the LIBRARY backup container (E2 / P1.5). Dockerfile at the repo
# ROOT (unlike Finance-Auditor's), so the plain sibling-build arm fits with no
# extra args. NO PUBLIC FALLBACK: nothing publishes this image anywhere, so an
# absent ../FileBackup checkout is a loud failure naming the clone — which is
# the right answer, because the AWOW can never fetch it either.
#
# BUILD IT WITH --rebuild, ALWAYS (P0.3). Two reasons, and both have bitten:
#   * this resolver SKIPS any ref that already exists, so a pre-existing image
#     short-circuits the build and ships whatever vintage is in the cache;
#   * FileBackup's own .artifacts/*.tar was exported by PODMAN and loads as
#     `localhost/filebackup:local`. Never load it, never hand-tag from it — a
#     hand-tagged image carries no homehub.source.revision, and the freshness
#     check in export-images.sh only WARNS on an unstamped image.
ensure_image "filebackup:${FILEBACKUP_IMAGE_TAG:-local}" "FileBackup" ""

# ── gunmaster3-relay: the family crossplay relay (GAME_RELAY_ENABLED) ────────
# GUARDED ON THE KNOB, and that guard is the whole point of this block.
#
# Every ensure_image above is unconditional because every one of those services
# is always in the stack. This one is not: the relay ships OFF in the public
# repo's .env.example, so building it unconditionally would make a clone WITHOUT
# a ../FinnsGame checkout die on a service that clone does not run. The knob is
# the honest condition — if you are not running the relay, you do not need its
# image.
#
# THE KNOB CHANGED 2026-08-29, from "is `gunmaster3` in COMPOSE_PROFILES" to
# GAME_RELAY_ENABLED, and vmtest/export-images.sh moved in the same step so the
# two still ask the same question of the same file. The relay's lifecycle now
# belongs to homehub-gunmaster3-relay.service rather than to docker (HomeHub's
# CROSSPLAY_HANDOFF.md §7); its compose `profiles:` entry survives only to keep
# it out of firstboot's bulk `up -d`.
#
# WHY IT MATTERS THAT THIS EXISTS AT ALL, since the relay is off by default:
# vmtest/export-images.sh derives the bake set from `docker compose config
# --images`, and it adds `--profile gunmaster3` exactly when GAME_RELAY_ENABLED
# is true. So the moment the knob is on (it is, in the real
# config.homehub.psd1), the exporter expects `gunmaster3-relay:local` to exist —
# and while the relay itself is no longer part of the all-or-nothing bulk `up`,
# an image missing from the ISO still means the relay unit fails on a reflashed
# box with nothing on the stick to fix it from. Without this block the ISO
# builds clean and the relay cannot start.
#
# THE DOCKERFILE IS IN A SUBDIRECTORY of the sibling repo (FinnsGame/server),
# unlike NagLight and FileBackup which have theirs at the repo root. ensure_image
# takes the sibling path verbatim, so naming the subdirectory is all it needs.
#
# NO PUBLIC FALLBACK: nothing publishes this image anywhere, and the hub cannot
# fetch it either — so an absent checkout must be a loud failure naming the
# clone, exactly as it is for FileBackup.
#
# AND `gunmaster3` IN COMPOSE_PROFILES IS A REFUSED BUILD, not a warning. It
# would put the relay into firstboot's bulk `docker compose up -d`, starting a
# public, unauthenticated service outside the unit that fences it off the host —
# the exact failure the lifecycle refresh exists to make unreachable. Dying here
# costs one edit; shipping it costs an unfenced relay on a flashed box.
case ",$(env_get COMPOSE_PROFILES | tr -d '[:space:]')," in
    *,gunmaster3,*)
        die "COMPOSE_PROFILES in ${ENV_FILE} contains 'gunmaster3'. That is a DEFECT, not an enable switch: the relay's lifecycle belongs to homehub-gunmaster3-relay.service, and naming the profile here lets firstboot's bulk 'docker compose up -d' start a public unauthenticated service OUTSIDE the unit that fences it off this host. Remove gunmaster3 from COMPOSE_PROFILES and set GAME_RELAY_ENABLED=true instead."
        ;;
esac
GAME_RELAY_IMAGE_TAG="$(env_get GAME_RELAY_IMAGE_TAG)"
case "$(env_get GAME_RELAY_ENABLED)" in
    true|TRUE|True|yes|1)
        ensure_image "gunmaster3-relay:${GAME_RELAY_IMAGE_TAG:-local}" "FinnsGame/server" ""
        ;;
    *)
        log "skip: gunmaster3-relay (GAME_RELAY_ENABLED is not true in ${ENV_FILE##*/})"
        ;;
esac

# ── caddy-cloudflare: a local image that is NOT an app repo ───────────────────
# The two above resolve from SIBLING repos, because they are our applications.
# This one is built from a Dockerfile inside THIS repo, so ensure_image's
# present → sibling → public chain does not fit: there is no sibling to find and
# no public image to fall back to. Caddy's DNS providers are compiled into the
# binary and no official image ships the Cloudflare one, so building it is the
# only way to have it at all.
#
# WHY THE STACK NEEDS IT: stock Caddy can only answer ACME's http-01/tls-alpn-01
# challenges, both of which need Let's Encrypt to reach this box from the public
# internet. It cannot, so nothing was ever issued and every HTTPS interface —
# including the LAN-only kiosk site the wall panel displays — was down at once.
# The full argument is at the top of stack/caddy/Dockerfile.
#
# NO SOURCE STAMP, and that is not an oversight. The freshness trap
# assert_local_image_fresh guards against is a `*:local` tag whose SOURCE moved
# in a sibling repo nobody rebuilt. This image's inputs are its own Dockerfile
# and two pinned upstream versions, all of them in this repo and all of them
# visible in the tag or the build args — so `--rebuild` after editing the
# Dockerfile is the whole discipline, and the Dockerfile's own `caddy
# list-modules` check refuses to produce a plugin-less binary regardless.
CADDY_IMAGE_TAG="$(env_get CADDY_IMAGE_TAG)"
CADDY_REF="caddy-cloudflare:${CADDY_IMAGE_TAG:-2.11.4-alpine}"
if [ "$REBUILD" -eq 0 ] && docker image inspect "$CADDY_REF" >/dev/null 2>&1; then
    log "present: $CADDY_REF (skip; --rebuild to force)"
elif [ "$DRY_RUN" -eq 1 ]; then
    log "would build: $CADDY_REF  <-  $REPO_ROOT/stack/caddy/Dockerfile"
else
    # The runtime tag carries a variant suffix (2.11.4-alpine) and the Dockerfile
    # needs the bare version for the -builder stage, which has no -alpine form.
    CADDY_VERSION="${CADDY_IMAGE_TAG%%-*}"
    log "local build: $CADDY_REF  <-  stack/caddy/Dockerfile (caddy ${CADDY_VERSION:-2.11.4} + caddy-dns/cloudflare)"
    docker build -t "$CADDY_REF" \
        --build-arg "CADDY_VERSION=${CADDY_VERSION:-2.11.4}" \
        -f "$REPO_ROOT/stack/caddy/Dockerfile" "$REPO_ROOT/stack/caddy" \
        || die "could not build $CADDY_REF — without it Caddy cannot answer a dns-01 challenge, and with no inbound :80 that means NO certificate at all"
fi

log "all local images resolved."
