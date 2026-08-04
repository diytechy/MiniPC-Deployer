#!/usr/bin/env bash
# vmtest/test-hub-seed.sh — do the HUB seed's refusals actually bite?
#
# WHY THIS FILE EXISTS. `test-wall-builder.sh` turned the wall builder's twelve
# hand-run refusals into something anyone can re-run; the hub had no equivalent,
# and the hub is the machine holding the household's real secrets. OI-19 is what
# that cost: `stack/autoinstall/user-data`'s site-staging late-command looked for
# its six real files on `/cdrom` — the BOOT MEDIUM, not the seed — and then
# `exit 0`'d when it found nothing, so a PRODUCTION hub built on the light path
# would install none of them and come up on `.env.example` placeholders while
# believing it was production. Nothing anywhere would have said so.
#
# TWO LAYERS, because the defect spanned both:
#   1. the BUILDER's guards (vmtest/lib/common.sh render_seed_tree) — including
#      the substitution assertions, which are the repo's standing habit: a `sed`
#      that silently no-ops must fail the build, never ship.
#   2. the LATE-COMMAND ITSELF, extracted from the user-data this build actually
#      produced and run against a fake `/target`. That is the only layer where
#      the refusal can be seen to fire, since Subiquity is what normally runs it.
#
# WHAT THIS SUITE CANNOT DO — and the reason OI-19 stays FIXED-BUT-UNVERIFIED:
# it never installs anything. Nothing here proves Subiquity runs these commands
# in this order, that `curtin` leaves /target where they expect it, or that the
# CIDATA-by-label mount in late-command 3 works on a real installer. That needs
# a hub install, which no agent has.
#
# Usage (WSL/Linux, from a MiniPC-Deployer checkout):
#   bash vmtest/test-hub-seed.sh
#
# Needs root for the install-as-root cases (the installer runs as root); they
# are SKIPPED, loudly and counted, otherwise — a suite that quietly shrinks when
# it cannot run is the same false green it exists to prevent.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
USER_DATA="$REPO_ROOT/stack/autoinstall/user-data"
META_DATA="$REPO_ROOT/stack/autoinstall/meta-data"
WORK="${TMPDIR:-/tmp}/hub-seed-test.$$"
# Never the repo's own .out/: a V3 gate ISO may be sitting there, and --clean is
# a recursive delete. Everything this suite writes is disposable.
OUT="$WORK/out-dir"

pass=0; fail=0; skip=0
mkdir -p "$WORK"
trap 'rm -rf "$WORK"; cp -f "$WORK.ud.bak" "$USER_DATA" 2>/dev/null; cp -f "$WORK.md.bak" "$META_DATA" 2>/dev/null; rm -f "$WORK.ud.bak" "$WORK.md.bak"' EXIT
cp "$USER_DATA" "$WORK.ud.bak"
cp "$META_DATA" "$WORK.md.bak"

ok()   { printf 'ok    %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf 'FAIL  %s\n      %s\n' "$1" "$2"; fail=$((fail + 1)); }
skip_case() { printf 'SKIP  %s\n      %s\n' "$1" "$2"; skip=$((skip + 1)); }

# build_seed [env...] — one hub seed build into a FRESH $OUT. Output in $WORK/out.txt.
build_seed() {
    rm -rf "$OUT"
    env "$@" OUT_DIR="$OUT" IMAGES_OUT="$WORK/no-images" \
        bash "$SCRIPT_DIR/build-seed.sh" >"$WORK/out.txt" 2>&1
}

# expect_refusal NAME NEEDLE -- env... : the build must exit non-zero AND say
# why. Both halves matter — a guard that fires with the wrong message costs the
# next person the hour it was supposed to save.
expect_refusal() {
    local name="$1" needle="$2"; shift 2
    [ "${1:-}" = "--" ] && shift
    if build_seed "$@"; then
        bad "$name" "the build SUCCEEDED; it was supposed to refuse"
        return
    fi
    if grep -qF "$needle" "$WORK/out.txt"; then
        ok "$name"
    else
        bad "$name" "refused, but not for the stated reason. Wanted: $needle | Got: $(grep -m1 FATAL "$WORK/out.txt" | cut -c1-160)"
    fi
}

# ── a production SITE_DIR, as Materialize-Deploy.ps1 -Image homehub leaves it ──
# user-data.filled is that script's render of the TRACKED user-data
# (FieldSchema.psd1 Images.homehub), so it is built here the same way.
SITE="$WORK/site"; mkdir -p "$SITE"
make_site_filled() {
    sed -e 's#- "ssh-ed25519 AAAA_REPLACE_WITH_YOUR_PUBLIC_KEY you@host"#- "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItestkey t@t"#' \
        "$WORK.ud.bak" > "$SITE/user-data.filled"
}
make_site_files() {
    printf 'DOMAIN=house.invalid\n'          > "$SITE/.env"
    printf 'BACKUP_SOURCES="x=/srv"\n'       > "$SITE/backup.env"
    printf 'username=share\npassword=x\n'    > "$SITE/cifs.creds"
    printf 'household:x\n'                   > "$SITE/samba-users.creds"
    printf '[library]\n  path = /srv/library\n' > "$SITE/smb.conf.fragment"
    printf 'LABEL=Library /srv/library ext4 nofail 0 2\n' > "$SITE/library-mounts.fstab"
    printf '/srv/library\tSERIAL123\n'       > "$SITE/drive-identity.conf"
}

echo "=== the production seam (builder) ==="
expect_refusal "SITE_DIR with no user-data.filled is refused, not half-applied" \
    "has no user-data.filled" -- "SITE_DIR=$SITE"

make_site_filled
make_site_files
# A materialised tree built BEFORE the OI-19 fix carries the old late-command,
# which exits 0 silently when the payload has no site/. The stick would look
# fine and the box would come up on placeholders.
sed 's/BUILD_PROFILE=production/BUILD_PROFILE=legacy/' "$SITE/user-data.filled" > "$SITE/ud.stale"
mv "$SITE/ud.stale" "$SITE/user-data.filled"
expect_refusal "a user-data.filled predating the OI-19 fix is refused (stale out\\ tree)" \
    "BUILD_PROFILE=production" -- "SITE_DIR=$SITE"
make_site_filled

echo
echo "=== the SIM build ==="
if build_seed; then
    U="$OUT/iso-root/user-data"; M="$OUT/iso-root/meta-data"
    grep -q 'BUILD_PROFILE=sim' "$U" \
        && ! grep -q 'BUILD_PROFILE=production' "$U" \
        && ok "a sim image is marked BUILD_PROFILE=sim (so its install does not demand secrets)" \
        || bad "sim BUILD_PROFILE marker" "$(grep -o 'BUILD_PROFILE=[a-z]*' "$U" | tr '\n' ' ')"
    grep -Fxq 'local-hostname: homehub-vmtest' "$M" \
        && ok "a sim seed's meta-data still says homehub-vmtest" \
        || bad "sim meta-data hostname" "$(grep local-hostname "$M")"
else
    bad "the sim build itself" "$(tail -n 3 "$WORK/out.txt" | tr '\n' ' ' | cut -c1-200)"
fi

echo
echo "=== the PRODUCTION build ==="
if build_seed "SITE_DIR=$SITE"; then
    U="$OUT/iso-root/user-data"
    grep -q 'BUILD_PROFILE=production' "$U" \
        && ok "a production image keeps BUILD_PROFILE=production (its install refuses to lose site/)" \
        || bad "production BUILD_PROFILE marker" "no marker in the baked user-data"
    [ -f "$OUT/iso-root/deploy-payload/site/.env" ] \
        && ok "the real site/ files ride the payload (where late-command 4b now reads them)" \
        || bad "site staging" "no deploy-payload/site/.env"
else
    bad "the production build itself" "$(tail -n 3 "$WORK/out.txt" | tr '\n' ' ' | cut -c1-200)"
fi

echo
echo "=== the substitution assertions (a silent no-op must fail the build) ==="
grep -v 'BUILD_PROFILE=production' "$WORK.ud.bak" > "$USER_DATA"
expect_refusal "a user-data that lost BUILD_PROFILE=production fails the sim build" \
    "BUILD_PROFILE substitution did not apply" --
cp "$WORK.ud.bak" "$USER_DATA"

echo
echo "=== the site-staging late-command itself (OI-19) ==="
# Extracted from the user-data each build PRODUCED, so what runs here is what
# would run on the box — not a paraphrase of it. /target is redirected into the
# work dir; nothing else about the command is altered.
extract_4b() {
    python3 - "$1" <<'PY'
import sys, yaml
for c in yaml.safe_load(open(sys.argv[1]))["autoinstall"]["late-commands"]:
    if isinstance(c, str) and "BUILD_PROFILE" in c:
        print(c)
        break
PY
}

# fake_target KIND -> sets T (a fake /target) and CMD (the late-command to run).
fake_target() {
    local kind="$1" src
    T="$WORK/t-$kind-$RANDOM"
    case "$kind" in
        production) src="$USER_DATA" ;;                 # the tracked template
        sim)        src="$WORK/sim-user-data" ;;        # as the builder rendered it
    esac
    mkdir -p "$T/opt/homehub/stack" "$T/etc/homehub-samba" "$T/etc/homehub-backup"
    CMD="$WORK/cmd.sh"
    extract_4b "$src" | sed -e "s#^bash -c '##" -e "s#'\$##" -e "s#/target#$T#g" > "$CMD"
    [ -s "$CMD" ] || { bad "extracting late-command 4b" "nothing matched in $src"; return 1; }
}

run_4b() { bash "$CMD" >"$WORK/lc.txt" 2>&1; echo $?; }

# The static half of the OI-19 fix: the boot-medium assumption is GONE. Step 3
# is the one place that searches (/cdrom, /media, /media/*, /run/media/*, then
# the CIDATA volume by label) and it copies site/ along with everything else.
if extract_4b "$USER_DATA" | grep -qE '/cdrom|/media'; then
    bad "the site step no longer reads the boot medium" "it still names /cdrom or /media"
else
    ok "the site step no longer reads the boot medium (one discovery path, in step 3)"
fi

build_seed && cp "$OUT/iso-root/user-data" "$WORK/sim-user-data"

if [ "$(id -u)" -ne 0 ]; then
    skip_case "the late-command's five behavioural cases" \
        "not root — 'install -o root -g root' is what the installer does and cannot be faked here"
else
    fake_target production
    rc=$(run_4b)
    if [ "$rc" -ne 0 ] && grep -q "FATAL - this is a PRODUCTION image" "$WORK/lc.txt"; then
        ok "PRODUCTION + no site/ payload REFUSES the install (was: silent exit 0)"
    else
        bad "production refusal" "rc=$rc $(head -n1 "$WORK/lc.txt")"
    fi

    fake_target sim
    rc=$(run_4b)
    if [ "$rc" -eq 0 ] && grep -q "SIM build" "$WORK/lc.txt"; then
        ok "SIM + no site/ payload is a clean, LOUD no-op (a vmtest image has no secrets)"
    else
        bad "sim no-op" "rc=$rc $(head -n1 "$WORK/lc.txt")"
    fi

    # The light path, as late-command 3 leaves it: the payload (site/ included)
    # copied to /target/opt/homehub, and NOTHING mounted at /cdrom. This is the
    # exact case that used to install nothing at all.
    fake_target production
    mkdir -p "$T/opt/homehub/site"
    make_site_files
    for f in .env backup.env cifs.creds samba-users.creds smb.conf.fragment \
             library-mounts.fstab drive-identity.conf; do
        cp "$SITE/$f" "$T/opt/homehub/site/$f"
    done
    rc=$(run_4b)
    missing=""
    for f in "$T/opt/homehub/stack/.env" "$T/etc/homehub-backup/backup.env" \
             "$T/etc/homehub-backup/cifs.creds" "$T/etc/homehub-samba/samba-users.creds" \
             "$T/etc/homehub-samba/smb.conf.fragment" "$T/etc/homehub-samba/library-mounts.fstab" \
             "$T/etc/homehub-samba/drive-identity.conf" "$T/etc/homehub-samba/.site-present"; do
        [ -f "$f" ] || missing="$missing ${f#$T}"
    done
    modes="$(stat -c%a "$T/opt/homehub/stack/.env" 2>/dev/null)"
    if [ "$rc" -eq 0 ] && [ -z "$missing" ] && [ "$modes" = "600" ]; then
        ok "PRODUCTION + a payload-borne site/ installs all seven files 0600 (the light-path case)"
    else
        bad "production install" "rc=$rc missing:$missing mode=$modes"
    fi

    fake_target production
    mkdir -p "$T/opt/homehub/site"
    cp "$SITE"/.env "$SITE/backup.env" "$SITE/smb.conf.fragment" "$SITE/library-mounts.fstab" "$T/opt/homehub/site/"
    rc=$(run_4b)
    if [ "$rc" -eq 0 ] && grep -q "optional site file MISSING - site/cifs.creds" "$WORK/lc.txt"; then
        ok "an absent OPTIONAL site file is named and not fatal (Build-VentoyStick marks three optional)"
    else
        bad "optional file handling" "rc=$rc $(grep -c MISSING "$WORK/lc.txt") missing lines"
    fi

    fake_target production
    mkdir -p "$T/opt/homehub/site"
    cp "$SITE/backup.env" "$SITE/smb.conf.fragment" "$SITE/library-mounts.fstab" "$T/opt/homehub/site/"
    rc=$(run_4b)
    if [ "$rc" -eq 0 ] && grep -q "required site file MISSING - site/.env" "$WORK/lc.txt"; then
        ok "an absent REQUIRED site file is named LOUDLY but does not halt — the documented, UNRATIFIED line (OI-19)"
    else
        bad "required file handling" "rc=$rc $(head -n1 "$WORK/lc.txt")"
    fi
fi

echo
printf '%s\n' "----------------------------------------"
printf 'hub seed guards: %d passed, %d failed, %d skipped\n' "$pass" "$fail" "$skip"
[ "$skip" -eq 0 ] || printf 'NOTE: skipped cases are NOT passes — see the SKIP lines above.\n'
printf 'NOTE: nothing here has installed anything. The late-commands are exercised\n'
printf '      against a fake /target, never under Subiquity — OI-19 stays UNVERIFIED\n'
printf '      until a hub is installed from a built ISO.\n'
[ "$fail" -eq 0 ] || exit 1
