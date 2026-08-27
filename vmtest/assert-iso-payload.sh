#!/usr/bin/env bash
# vmtest/assert-iso-payload.sh — prove what a BUILT ISO actually carries.
#
# WHY THIS EXISTS, and it is not a general-purpose linter. On 2026-08-27 the hub
# build reported success, every step logged "staged", and the ISO carried no
# IceDrive at all: stage_icedrive_into_payload was writing to
# $OUT_DIR/deploy-payload instead of $OUT_DIR/iso-root/deploy-payload, `mkdir -p`
# created the wrong tree without complaint, and `install` succeeded into it.
# Every signal was green and the artifact was wrong.
#
# So this asks the ARTIFACT, not the build log. That is the house rule — verify
# the artifact, never the exit code — applied to the one output that actually
# gets flashed.
#
# THE CENTRAL CHECK IS CARRIAGE-vs-ACTIVATION. HomeHub's Materialize-Deploy
# already refuses to ACTIVATE a feature whose carriage is absent, but it checks
# config on the dev PC. This checks the same invariant on the finished ISO,
# where the two halves have actually been assembled — and it reads the expected
# state out of the payload's own .env rather than being told, so it cannot be
# pointed at the wrong expectation.
#
# Usage:
#   bash vmtest/assert-iso-payload.sh --iso vmtest/.out/repacked.iso
#   bash vmtest/assert-iso-payload.sh --iso <path> --target wall
#
# Exit 0 = every assertion held. Nonzero = the count of failures.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh" 2>/dev/null || true

ISO=""
TARGET="hub"
while [ $# -gt 0 ]; do
    case "$1" in
        --iso)    ISO="$2"; shift 2 ;;
        --target) TARGET="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ -n "$ISO" ] || { echo "need --iso <path>" >&2; exit 2; }
[ -f "$ISO" ] || { echo "not found: $ISO" >&2; exit 2; }
command -v xorriso >/dev/null || { echo "xorriso is required" >&2; exit 2; }

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

PASS=0; FAIL=0
ok()  { printf '  PASS  %s\n' "$*"; PASS=$((PASS+1)); }
bad() { printf '  FAIL  %s\n' "$*"; FAIL=$((FAIL+1)); }

# iso_has PATH — true when the ISO carries that exact path.
iso_has() { xorriso -indev "$ISO" -find "$1" 2>/dev/null | grep -q .; }
# iso_get PATH DEST — extract one file; silent failure leaves DEST absent.
iso_get() { xorriso -osirrox on -indev "$ISO" -extract "$1" "$2" >/dev/null 2>&1; }

echo "ISO:    $ISO ($(du -h "$ISO" | cut -f1))"
echo "target: $TARGET"
echo

# ── the payload root itself ─────────────────────────────────────────────────
echo "== payload root =="
if iso_has /deploy-payload; then ok "/deploy-payload exists on the ISO"
else bad "/deploy-payload is MISSING — nothing below can be true"; echo; echo "PASSED $PASS  FAILED $FAIL"; exit "$FAIL"; fi

# ── the activation knobs, read from the payload's own .env ──────────────────
ENVF="$TMP/env"
REMOTE_UI="unknown"; ICE_MODE="unknown"
if [ "$TARGET" = "hub" ]; then
    echo
    echo "== activation (from the payload's own .env) =="
    iso_get /deploy-payload/stack/.env "$ENVF"
    if [ -s "$ENVF" ]; then
        ok "deploy-payload/stack/.env is on the ISO"
        REMOTE_UI="$(sed -n 's/^REMOTE_UI_ENABLED=//p' "$ENVF" | head -1 | tr -d '\r')"
        ICE_MODE="$(sed -n 's/^ICEDRIVE_MODE=//p'     "$ENVF" | head -1 | tr -d '\r')"
        : "${REMOTE_UI:=false}"; : "${ICE_MODE:=off}"
        echo "        REMOTE_UI_ENABLED=$REMOTE_UI   ICEDRIVE_MODE=$ICE_MODE"
        case "$ICE_MODE" in
            off|appimage|cli) ok "ICEDRIVE_MODE is a known value" ;;
            *) bad "ICEDRIVE_MODE='$ICE_MODE' is not off/appimage/cli" ;;
        esac
        # THE ONE COMBINATION THAT CANNOT WORK, asserted on the artifact as well
        # as in Materialize-Deploy: a GUI app with no display.
        if [ "$ICE_MODE" = "appimage" ] && [ "$REMOTE_UI" != "true" ]; then
            bad "ICEDRIVE_MODE=appimage with REMOTE_UI_ENABLED=$REMOTE_UI — the GUI client would have no display"
        else
            ok "the mode/display pair is coherent"
        fi
        grep -q '^OPERATOR_PASSWORD=.' "$ENVF" && ok "OPERATOR_PASSWORD is set (not the empty knob it was until 2026-08-27)" \
                                                || bad "OPERATOR_PASSWORD is empty or absent"
    else
        bad "could not extract deploy-payload/stack/.env"
    fi
fi

# ── carriage: the IceDrive artifact matches the mode ────────────────────────
if [ "$TARGET" = "hub" ]; then
    echo
    echo "== carriage: IceDrive =="
    PINF="$TMP/pin"; iso_get /deploy-payload/stack/icedrive/icedrive.pin "$PINF"
    pin_sha() { awk -F= -v k="$1" '{ sub(/#.*/,"") } $1 ~ ("^[[:space:]]*" k "[[:space:]]*$") { gsub(/[[:space:]]/,"",$2); print $2; exit }' "$PINF"; }

    APP=/deploy-payload/stack/remote-ui/Icedrive.AppImage
    CLI=/deploy-payload/stack/icedrive/IcedriveCLI
    case "$ICE_MODE" in
      appimage)
        if iso_has "$APP"; then
            ok "the AppImage rides the ISO"
            iso_get "$APP" "$TMP/a"; iso_get "$APP.sha256" "$TMP/a.sha"
            got="$(sha256sum "$TMP/a" | cut -d' ' -f1)"
            want="$(pin_sha APPIMAGE_SHA256)"
            [ -n "$want" ] && [ "$got" = "$want" ] && ok "its sha256 matches the pin ON THE ISO" \
                                                   || bad "sha256 mismatch: iso=$got pin=${want:-<unset>}"
            [ "$(tr -d '[:space:]' < "$TMP/a.sha" 2>/dev/null)" = "$want" ] \
                && ok "the .sha256 travelling beside it agrees (the box re-checks this)" \
                || bad "the travelling .sha256 disagrees with the pin"
        else
            bad "ICEDRIVE_MODE=appimage but no AppImage on the ISO — ACTIVATION WITHOUT CARRIAGE"
        fi
        iso_has "$CLI" && bad "the CLI is carried too — two clients on one account fight" \
                       || ok "the CLI is not carried (one client only)"
        ;;
      cli)
        if iso_has "$CLI"; then
            ok "the CLI rides the ISO"
            iso_get "$CLI" "$TMP/c"
            got="$(sha256sum "$TMP/c" | cut -d' ' -f1)"; want="$(pin_sha CLI_SHA256)"
            [ -n "$want" ] && [ "$got" = "$want" ] && ok "its sha256 matches the pin ON THE ISO" \
                                                   || bad "sha256 mismatch: iso=$got pin=${want:-<unset>}"
        else
            bad "ICEDRIVE_MODE=cli but no CLI on the ISO — ACTIVATION WITHOUT CARRIAGE"
        fi
        iso_has "$APP" && bad "the AppImage is carried too" || ok "the AppImage is not carried"
        ;;
      off)
        iso_has "$APP" && bad "ICEDRIVE_MODE=off but an AppImage rides anyway" || ok "no AppImage (mode is off)"
        iso_has "$CLI" && bad "ICEDRIVE_MODE=off but the CLI rides anyway"     || ok "no CLI (mode is off)"
        ;;
    esac
fi

# ── carriage: the optional packages, baked but NOT install-listed ───────────
echo
echo "== carriage: the offline apt repo =="
BAKED="$TMP/baked"; iso_get /deploy-payload/apt/packages.baked.list "$BAKED"
if [ -s "$BAKED" ]; then
    ok "packages.baked.list is on the ISO ($(grep -c . "$BAKED") names — what late-command 3c installs)"
    # THE DISTINCTION THIS FILE EXISTS FOR: a bake-only package must be in the
    # repo and NOT in the install list. Getting that backwards installs a
    # desktop on a headless box.
    if [ "$TARGET" = "hub" ] && [ "$REMOTE_UI" = "true" ]; then
        for p in xrdp xfce4-session libxcb-icccm4 libfuse2t64; do
            if grep -qx "$p" "$BAKED"; then bad "$p is INSTALL-listed — it should be bake-only"
            elif xorriso -indev "$ISO" -find /deploy-payload/apt -name "${p}_*.deb" 2>/dev/null | grep -q .; then
                ok "$p: in the repo, not install-listed (bake-only, correct)"
            else
                bad "$p: REMOTE_UI_ENABLED=true but its .deb is not in the baked repo"
            fi
        done
    fi
    # -- the systemd lockstep invariant --------------------------------------
    # THE INVARIANT IS REPO MEMBERSHIP, NOT INSTALL-LISTING, and the difference
    # matters. Naming a package in packages.list is one WAY to guarantee it is
    # fetched (the hub's fix on 2026-08-27); arriving transitively through some
    # other package dependency chain is another, and the wall image happens to
    # get both that way through its graphical closure. Asserting the hub chosen
    # mechanism would fail a wall ISO that is perfectly correct.
    #
    # What actually matters: if the baked repo carries a systemd NEWER than the
    # install base, every version-locked sibling must be in the repo too - or
    # apt plan for the missing one is to REMOVE it, and removing libpam-systemd
    # takes snapd, polkitd and ubuntu-server with it.
    PKGIDX="$TMP/Packages"; iso_get /deploy-payload/apt/Packages "$PKGIDX"
    if [ -s "$PKGIDX" ] && grep -qx "Package: systemd" "$PKGIDX"; then
        sysver="$(awk '/^Package: systemd$/{f=1} f&&/^Version: /{print $2; exit}' "$PKGIDX")"
        echo "        repo carries systemd $sysver, so its lockstep siblings must be here too"
        for p in systemd-sysv libpam-systemd libnss-systemd; do
            if grep -qx "Package: $p" "$PKGIDX"; then
                how="transitively"
                grep -qx "$p" "$BAKED" && how="named in packages.list"
                ok "$p is in the baked repo ($how)"
            else
                bad "$p is MISSING from a repo that upgrades systemd - the offline resolve will plan to REMOVE it"
            fi
        done
    else
        ok "the repo carries no systemd upgrade, so the lockstep cascade cannot arise"
    fi
else
    bad "no packages.baked.list on the ISO"
fi

echo
echo "======================================================"
echo "  PASSED $PASS   FAILED $FAIL"
echo "======================================================"
[ "$FAIL" -eq 0 ] || echo "The ISO does not match what the build declared. Do not flash it."
exit "$FAIL"
