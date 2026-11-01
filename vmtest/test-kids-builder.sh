#!/usr/bin/env bash
# vmtest/test-kids-builder.sh — the kids kiosk builder's seams. Verifies: TC-1628, TC-1629.
#
# render_kids_seed_tree and stage_kids_app_into_payload decide what a stick
# installs and on which disk. Each guard below is exercised by building the
# thing it exists to refuse, and the refusal is asserted by name:
#   TC-1628 the SIM seed (VM-only identity, Virtual_Disk pin, ethernet, no Wi-Fi,
#           no placeholder) and the PRODUCTION seam (allow-pw true, the SIM pin,
#           no wifis:, an active placeholder, no storage match - each refused;
#           a good filled seed and its kids.env staged)
#   TC-1629 the OtterTube tarball: none, two, -dirty, or chrome-sandbox not
#           setuid root - each refused; exactly one good one staged
#
# Needs: bash, python3 + PyYAML (validate_autoinstall_yaml), openssl,
# ssh-keygen, tar. Builds nothing big; a few seconds.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
FAIL=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; FAIL=$((FAIL + 1)); }

# Run a builder function in a subshell (die exits); print its combined output.
build() {  # out-dir [VAR=value ...] -- function args...
    ( set -e
      # shellcheck source=lib/common.sh
      . "$SCRIPT_DIR/lib/common.sh"
      # shellcheck source=lib/kids.sh
      . "$SCRIPT_DIR/lib/kids.sh"
      "$@" ) 2>&1
}
expect_refusal() {  # name pattern -- command...
    local name=$1 pat=$2; shift 3
    local out; out=$("$@"); local rc=$?
    if [ $rc -ne 0 ] && printf '%s' "$out" | grep -q -- "$pat"; then pass "$name"
    else fail "$name (rc=$rc, wanted a refusal matching '$pat'): $(printf '%s' "$out" | tail -n 2)"; fi
}

# ── TC-1628: SIM ─────────────────────────────────────────────────────────────
sim="$WORK/sim"
if out=$(build render_kids_seed_tree "$REPO_ROOT" "$sim" test); then
    ud="$sim/iso-root/user-data"
    grep -q '^    hostname: kids-kiosk-vmtest$' "$ud" && grep -q '^        model: Virtual_Disk$' "$ud" \
        && grep -q '^    ethernets:$' "$ud" && ! grep -qE 'wifis:|REPLACE_WITH_(WIFI|HUB_IP|TIMEZONE)' "$ud" \
        && grep -q '^local-hostname: kids-kiosk-vmtest$' "$sim/iso-root/meta-data" \
        && [ -f "$sim/iso-root/deploy-payload/stack/autoinstall/kids/kids-outputs.sh" ] \
        && pass "TC-1628 sim seed: vmtest identity, Virtual_Disk, ethernet, payload carries the kids files" \
        || fail "TC-1628 sim seed wrong: $(grep -nE 'hostname|model:|ethernets|wifis|REPLACE' "$ud" | head -5)"
else
    fail "TC-1628 sim build refused: $(printf '%s' "$out" | tail -n 3)"
fi

# ── TC-1628: PRODUCTION seam ─────────────────────────────────────────────────
site="$WORK/site"; mkdir -p "$site"
# A filled seed as Materialize-Deploy.ps1 -Image kids would write it.
sed -e 's/REPLACE_WITH_TIMEZONE/Etc\/UTC/' -e 's/REPLACE_WITH_HUB_IP/192.0.2.10/' \
    -e 's/REPLACE_WITH_WIFI_SSID/test-ssid/' -e 's/REPLACE_WITH_WIFI_PSK/test-psk-1234/' \
    -e 's#ssh-ed25519 AAAA_REPLACE_WITH_YOUR_PUBLIC_KEY you@host#ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleOnlyKeyForTheBuilderTest0000000000 test#' \
    -e 's/^\(        \)model: FPT310M8SSD128G/\1serial: FPT310M8SSD128G_TESTSERIAL/' \
    "$REPO_ROOT/stack/autoinstall/kids/user-data" > "$site/user-data.filled.good"
printf 'KIDS_TOUCH_OUTPUT=HDMI-A-1\n' > "$site/kids.env"
variant() {  # name sed-expression
    mkdir -p "$WORK/$1"; cp "$site/kids.env" "$WORK/$1/"
    sed -e "$2" "$site/user-data.filled.good" > "$WORK/$1/user-data.filled"
}
variant good 's/^$//'
if out=$(KIDS_SITE_DIR="$WORK/good" build render_kids_seed_tree "$REPO_ROOT" "$WORK/prod-good" test); then
    [ -f "$WORK/prod-good/iso-root/deploy-payload/site/kids.env" ] \
        && cmp -s "$WORK/good/user-data.filled" "$WORK/prod-good/iso-root/user-data" \
        && pass "TC-1628 production: user-data.filled verbatim, site/kids.env staged" \
        || fail "TC-1628 production seed not taken verbatim or kids.env not staged"
else
    fail "TC-1628 a good production seed was refused: $(printf '%s' "$out" | tail -n 3)"
fi
build_prod() { KIDS_SITE_DIR="$1" build render_kids_seed_tree "$REPO_ROOT" "$2" test; }
variant allowpw 's/allow-pw: false/allow-pw: true/'
expect_refusal "TC-1628 production refuses allow-pw: true" "allow-pw: true" -- build_prod "$WORK/allowpw" "$WORK/p1"
variant simpin 's/serial: FPT310M8SSD128G_TESTSERIAL/model: Virtual_Disk/'
expect_refusal "TC-1628 production refuses the SIM disk pin" "Virtual_Disk" -- build_prod "$WORK/simpin" "$WORK/p2"
variant nowifi 's/^    wifis:$/    ethernets:/'
expect_refusal "TC-1628 production refuses a seed without wifis:" "no wifis:" -- build_prod "$WORK/nowifi" "$WORK/p3"
variant placeholder 's/test-ssid/REPLACE_WITH_WIFI_SSID/'
expect_refusal "TC-1628 production refuses an active placeholder" "REPLACE_WITH placeholder" -- build_prod "$WORK/placeholder" "$WORK/p4"
variant nomatch '/serial: FPT310M8SSD128G_TESTSERIAL/d'
expect_refusal "TC-1628 production refuses a seed with no storage match" "no storage.layout match" -- build_prod "$WORK/nomatch" "$WORK/p5"
mkdir -p "$WORK/nofilled"
expect_refusal "TC-1628 production refuses a site dir with no user-data.filled" "no user-data.filled" -- build_prod "$WORK/nofilled" "$WORK/p6"

# ── TC-1629: the OtterTube tarball ───────────────────────────────────────────
mk_tar() {  # dir name sandbox-mode
    local s; s=$(mktemp -d); mkdir -p "$s/app"
    printf '#!/bin/sh\n' > "$s/app/ottertube-desktop"; chmod 755 "$s/app/ottertube-desktop"
    : > "$s/app/chrome-sandbox"; chmod "$3" "$s/app/chrome-sandbox"
    mkdir -p "$1"; tar --owner=0 --group=0 --numeric-owner -czf "$1/$2" -C "$s" app; rm -rf "$s"
}
stage() { build stage_kids_app_into_payload "$1" "$2"; }
mkdir -p "$WORK/none"
expect_refusal "TC-1629 no tarball is refused" "no OtterTube tarball" -- stage "$WORK/s1" "$WORK/none"
mk_tar "$WORK/two" ottertube-aaaaaaa-linux-x64.tar.gz 4755; mk_tar "$WORK/two" ottertube-bbbbbbb-linux-x64.tar.gz 4755
expect_refusal "TC-1629 two tarballs are refused" "exactly one may ship" -- stage "$WORK/s2" "$WORK/two"
mk_tar "$WORK/dirty" ottertube-aaaaaaa-dirty-linux-x64.tar.gz 4755
expect_refusal "TC-1629 a -dirty build is refused" "dirty" -- stage "$WORK/s3" "$WORK/dirty"
mk_tar "$WORK/nosuid" ottertube-aaaaaaa-linux-x64.tar.gz 755
expect_refusal "TC-1629 chrome-sandbox without setuid is refused" "not setuid root" -- stage "$WORK/s4" "$WORK/nosuid"
mk_tar "$WORK/one" ottertube-aaaaaaa-linux-x64.tar.gz 4755
if out=$(stage "$WORK/s5" "$WORK/one") && [ -f "$WORK/s5/iso-root/deploy-payload/kids-app/ottertube-aaaaaaa-linux-x64.tar.gz" ]; then
    pass "TC-1629 exactly one good tarball is staged under kids-app/"
else
    fail "TC-1629 a good tarball was not staged: $(printf '%s' "${out:-}" | tail -n 2)"
fi

echo "FAILURES=$FAIL"
[ "$FAIL" -eq 0 ]
