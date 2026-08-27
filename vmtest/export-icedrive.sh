#!/usr/bin/env bash
# vmtest/export-icedrive.sh - put an IceDrive binary on the payload, verified.
#
# TWO ARTIFACTS, ONE SCRIPT. `--artifact appimage` stages the 118 MB GUI client
# for the SR-015 graphical session; `--artifact cli` stages the 9.8 MB headless
# ELF. They are not interchangeable and only one should ever ride an image - two
# clients on one account fight over the same folders. See
# stack/icedrive/README.md.
#
# CARRIAGE IS NOT DECIDED HERE. HomeHub's config.homehub.psd1 `Extras.IceDrive`
# picks off/appimage/cli, and the same declaration sets ICEDRIVE_MODE in .env.
# Called with no --artifact this stages NOTHING, which is this repo's default
# posture: the deployer ships IceDrive off.
#
# WHY IT IS NOT FETCHED FROM THE VENDOR. That was the intended design and it
# does not work: icedrive.net is behind Cloudflare and answers 403 to anything
# that is not a browser - the download page, the asset paths, and the CLI
# installer script alike. A build that curled the vendor would fail on a machine
# with perfectly good internet, which is a worse failure than asking for the
# file once.
#
# SO THE MODEL IS: the operator supplies it ONCE, this verifies it against the
# SHA256 pinned in stack/icedrive/icedrive.pin, and caches it under
# vmtest/.cache/. Every build after that is reproducible, offline, and needs no
# vendor at all. Bumping a version is: edit the pin, re-run with --from.
#
# Usage:
#   bash vmtest/export-icedrive.sh --out .out/icedrive --artifact appimage
#   bash vmtest/export-icedrive.sh --out .out/icedrive --artifact cli --from ~/Downloads/IcedriveCLI-v3.62
#   bash vmtest/export-icedrive.sh --print-hash --from ~/Downloads/<any file>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

REPO_ROOT="$(repo_root)"
PIN="$REPO_ROOT/stack/icedrive/icedrive.pin"
CACHE_DIR="$REPO_ROOT/vmtest/.cache"
OUT=""
FROM=""
ARTIFACT="${ICEDRIVE_MODE:-}"
PRINT_HASH=0

while [ $# -gt 0 ]; do
    case "$1" in
        --out)        OUT="$2"; shift 2 ;;
        --from)       FROM="$2"; shift 2 ;;
        --artifact)   ARTIFACT="$2"; shift 2 ;;
        --pin)        PIN="$2"; shift 2 ;;
        --print-hash) PRINT_HASH=1; shift ;;
        *) die "unknown argument: $1" ;;
    esac
done

# --print-hash is the helper for step 2 of setting a pin. It deliberately works
# with no pin set at all, which is the state you are in when you need it.
if [ "$PRINT_HASH" -eq 1 ]; then
    [ -n "$FROM" ] || die "--print-hash needs --from <path to the downloaded file>"
    [ -f "$FROM" ] || die "not found: $FROM"
    printf 'SHA256=%s\n' "$(sha256sum "$FROM" | cut -d' ' -f1)"
    exit 0
fi

[ -n "$OUT" ] || die "need --out <directory>"
[ -f "$PIN" ]  || die "no pin file at $PIN"

rm -rf "$OUT"
mkdir -p "$OUT"

# NO ARTIFACT IS THE DEFAULT AND IS NOT AN ERROR. This repo ships IceDrive off;
# an image built from it carries no client of either kind.
case "${ARTIFACT:-off}" in
    off|"")
        log "no IceDrive artifact requested - this image carries no client."
        log "  HomeHub's config.homehub.psd1 Extras.IceDrive selects appimage or cli."
        exit 0 ;;
    appimage|cli) : ;;
    *) die "--artifact must be appimage, cli or off (got '$ARTIFACT')" ;;
esac

# ── read the pin (same three rules as packages.list: strip comments, trim) ───
pin_value() {
    awk -F= -v k="$1" '
        { sub(/#.*/, "") }
        $1 ~ ("^[[:space:]]*" k "[[:space:]]*$") {
            v = $2; gsub(/^[[:space:]]+|[[:space:]]+$/, "", v); print v; exit
        }' "$PIN"
}

if [ "$ARTIFACT" = appimage ]; then
    WANT_SHA="$(pin_value APPIMAGE_SHA256)"
    PIN_VER="$(pin_value APPIMAGE_VERSION)"
    PIN_URL="$(pin_value APPIMAGE_URL)"
    OUT_NAME="Icedrive.AppImage"
    CACHED="$CACHE_DIR/Icedrive-$WANT_SHA.AppImage"
    HUMAN="the Linux AppImage (GUI client)"
else
    WANT_SHA="$(pin_value CLI_SHA256)"
    PIN_VER="$(pin_value CLI_VERSION)"
    PIN_URL="$(pin_value CLI_URL)"
    OUT_NAME="IcedriveCLI"
    CACHED="$CACHE_DIR/IcedriveCLI-$WANT_SHA"
    HUMAN="the Linux CLI (headless)"
fi

# AN UNSET PIN FOR AN ARTIFACT SOMEONE ASKED FOR IS A HARD FAILURE, unlike an
# unset pin nobody asked about. Carriage was requested and cannot be provided,
# and the box would otherwise discover that hours later as a missing file.
if [ -z "$WANT_SHA" ]; then
    die "--artifact $ARTIFACT was requested but no SHA256 is pinned for it in ${PIN#"$REPO_ROOT"/}.
  Download $HUMAN from icedrive.net in a browser, then:
      bash vmtest/export-icedrive.sh --print-hash --from <file>
  and put the version and hash in the matching pair in that pin file."
fi

# ── resolve the artifact: cache, then --from, then the optional pinned URL ───
verify() {  # verify FILE - exits nonzero and names both hashes on mismatch
    local f="$1" got
    got="$(sha256sum "$f" | cut -d' ' -f1)"
    [ "$got" = "$WANT_SHA" ] || {
        log "FATAL: $f does not match the pin."
        log "  pinned: $WANT_SHA"
        log "  actual: $got"
        log "  Either you downloaded a different version (bump the pin, deliberately)"
        log "  or the file is damaged. It is NOT staged either way."
        return 1
    }
}

if [ -f "$CACHED" ] && verify "$CACHED" 2>/dev/null; then
    log "using the cached $ARTIFACT (sha256 matches the pin)"
elif [ -n "$FROM" ]; then
    [ -f "$FROM" ] || die "not found: $FROM"
    verify "$FROM" || die "the supplied file does not match the pin (see above)."
    mkdir -p "$CACHE_DIR"
    cp -f "$FROM" "$CACHED"
    log "verified and cached: ${CACHED#"$REPO_ROOT"/}"
elif [ -n "$PIN_URL" ]; then
    log "fetching the pinned URL (no cache, no --from)"
    mkdir -p "$CACHE_DIR"
    curl -fsSL --max-time 600 -o "$CACHED.part" "$PIN_URL" \
        || die "fetch failed: $PIN_URL - icedrive.net is Cloudflared and refuses non-browser clients, which is why the URL fields are normally empty. Download it in a browser and pass --from instead."
    mv "$CACHED.part" "$CACHED"
    verify "$CACHED" || { rm -f "$CACHED"; die "the fetched file does not match the pin (see above)."; }
    log "fetched, verified and cached"
else
    die "the pin names SHA256=$WANT_SHA for $ARTIFACT but the file is not cached and no --from was given.
  Download $HUMAN from icedrive.net in a browser, then:
      bash vmtest/export-icedrive.sh --out $OUT --artifact $ARTIFACT --from <path to it>
  It is verified against the pin and cached, so this is once per version bump."
fi

install -m 0755 "$CACHED" "$OUT/$OUT_NAME"
printf '%s\n' "$WANT_SHA" > "$OUT/$OUT_NAME.sha256"
[ -n "$PIN_VER" ] && printf '%s\n' "$PIN_VER" > "$OUT/$OUT_NAME.version"
# The staging helper needs to know which of the two payload directories this
# belongs in, and guessing from the filename is the kind of coupling that breaks
# quietly when a name changes.
printf '%s\n' "$ARTIFACT" > "$OUT/artifact.kind"

log "OK - staged $OUT/$OUT_NAME ($(du -h "$OUT/$OUT_NAME" | cut -f1)${PIN_VER:+, version $PIN_VER})"
log "     the hash travels beside it, and the box re-checks it before installing"
