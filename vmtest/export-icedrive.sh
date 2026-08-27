#!/usr/bin/env bash
# vmtest/export-icedrive.sh — put the IceDrive AppImage on the payload, verified.
#
# WHY THIS EXISTS. IceDrive's Linux client is GUI-only and is the whole reason
# the SR-015 graphical session exists. Until 2026-08-26 the binary was never
# shipped: the operator downloaded it, scp'd it to the box, and passed
# ICEDRIVE_APPIMAGE= to setup-remote-ui.sh by hand. With the UI no longer
# optional, the app that justifies it should not still be a manual errand.
#
# WHY IT IS NOT FETCHED FROM THE VENDOR. That was the intended design and it
# does not work: icedrive.net is behind Cloudflare and answers 403 to anything
# that is not a browser. Measured 2026-08-26 — the download page and four
# candidate asset paths all returned 403 to curl with a browser user-agent. A
# build that curls the vendor would fail on a machine with perfectly good
# internet, which is a worse failure than asking for the file once.
#
# SO THE MODEL IS: the operator supplies it ONCE, this verifies it against the
# SHA256 pinned in stack/remote-ui/icedrive.pin, and caches it under
# vmtest/.cache/. Every build after that is reproducible, offline, and needs no
# vendor at all. Bumping the version is: edit the pin, re-run with --from.
#
# UNSET PIN IS NOT AN ERROR. With no SHA256 pinned, the ISO carries no AppImage
# and the hub installs the session without it — the pre-2026-08-26 behaviour.
# This says so and exits 0, because a half-configured opt-in should not block
# someone building an image for unrelated reasons.
#
# Usage:
#   bash vmtest/export-icedrive.sh --out .out/icedrive
#   bash vmtest/export-icedrive.sh --out .out/icedrive --from ~/Downloads/Icedrive.AppImage
#   bash vmtest/export-icedrive.sh --print-hash --from ~/Downloads/Icedrive.AppImage

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

REPO_ROOT="$(repo_root)"
PIN="$REPO_ROOT/stack/remote-ui/icedrive.pin"
CACHE_DIR="$REPO_ROOT/vmtest/.cache"
OUT=""
FROM=""
PRINT_HASH=0

while [ $# -gt 0 ]; do
    case "$1" in
        --out)        OUT="$2"; shift 2 ;;
        --from)       FROM="$2"; shift 2 ;;
        --pin)        PIN="$2"; shift 2 ;;
        --print-hash) PRINT_HASH=1; shift ;;
        *) die "unknown argument: $1" ;;
    esac
done

# --print-hash is the helper for step 2 of setting the pin. It deliberately
# works with no pin set at all, which is the state you are in when you need it.
if [ "$PRINT_HASH" -eq 1 ]; then
    [ -n "$FROM" ] || die "--print-hash needs --from <path to the AppImage>"
    [ -f "$FROM" ] || die "not found: $FROM"
    printf 'SHA256=%s\n' "$(sha256sum "$FROM" | cut -d' ' -f1)"
    exit 0
fi

[ -n "$OUT" ] || die "need --out <directory>"
[ -f "$PIN" ]  || die "no pin file at $PIN"

# ── read the pin (same three rules as packages.list: strip comments, trim) ───
pin_value() {
    awk -F= -v k="$1" '
        { sub(/#.*/, "") }
        $1 ~ ("^[[:space:]]*" k "[[:space:]]*$") {
            v = $2; gsub(/^[[:space:]]+|[[:space:]]+$/, "", v); print v; exit
        }' "$PIN"
}
WANT_SHA="$(pin_value SHA256)"
PIN_URL="$(pin_value URL)"
PIN_VER="$(pin_value VERSION)"

rm -rf "$OUT"
mkdir -p "$OUT"

if [ -z "$WANT_SHA" ]; then
    log "no SHA256 pinned in ${PIN#"$REPO_ROOT"/} — this image will carry NO IceDrive AppImage."
    log "  The hub still installs the RDP/XFCE session; IceDrive stays a manual step."
    log "  To ship it: download the AppImage, then"
    log "    bash vmtest/export-icedrive.sh --print-hash --from <file>   # gives you the line"
    log "    (put VERSION= and SHA256= in the pin, then re-run with --from)"
    exit 0
fi

CACHED="$CACHE_DIR/Icedrive-$WANT_SHA.AppImage"

# ── resolve the artifact: cache, then --from, then the optional pinned URL ───
verify() {  # verify FILE — exits nonzero and names both hashes on mismatch
    local f="$1" got
    got="$(sha256sum "$f" | cut -d' ' -f1)"
    [ "$got" = "$WANT_SHA" ] || {
        log "FATAL: $f does not match the pin."
        log "  pinned: $WANT_SHA"
        log "  actual: $got"
        log "  Either you downloaded a different version (bump SHA256 in the pin, deliberately)"
        log "  or the file is damaged. It is NOT staged either way."
        return 1
    }
}

if [ -f "$CACHED" ] && verify "$CACHED" 2>/dev/null; then
    log "using the cached AppImage (sha256 matches the pin)"
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
        || die "fetch failed: $PIN_URL — icedrive.net is Cloudflared and refuses non-browser clients, which is why URL= is normally empty. Download it in a browser and pass --from instead."
    mv "$CACHE_DIR/$(basename "$CACHED").part" "$CACHED" 2>/dev/null || mv "$CACHED.part" "$CACHED"
    verify "$CACHED" || { rm -f "$CACHED"; die "the fetched file does not match the pin (see above)."; }
    log "fetched, verified and cached"
else
    die "the pin names SHA256=$WANT_SHA but the file is not cached and no --from was given.
  Download the Linux AppImage from icedrive.net in a browser, then:
      bash vmtest/export-icedrive.sh --out $OUT --from <path to it>
  It is verified against the pin and cached, so this is once per version bump."
fi

install -m 0755 "$CACHED" "$OUT/Icedrive.AppImage"
printf '%s\n' "$WANT_SHA" > "$OUT/Icedrive.AppImage.sha256"
[ -n "$PIN_VER" ] && printf '%s\n' "$PIN_VER" > "$OUT/Icedrive.AppImage.version"

log "OK - staged $OUT/Icedrive.AppImage ($(du -h "$OUT/Icedrive.AppImage" | cut -f1)${PIN_VER:+, version $PIN_VER})"
log "     the hash travels beside it, and firstboot re-checks it before installing"
