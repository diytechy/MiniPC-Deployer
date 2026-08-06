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
# THREE LAYERS, because the defect spanned all of them:
#   1. the BUILDER's guards (vmtest/lib/common.sh render_seed_tree) — including
#      the substitution assertions, which are the repo's standing habit: a `sed`
#      that silently no-ops must fail the build, never ship.
#   2. the LATE-COMMANDS THEMSELVES, extracted from the user-data a build
#      actually produced and EXECUTED against a fake `/target` — step 3 (find
#      and copy the payload) followed by step 4b (install site/ out of it), in
#      that order, against a real staged payload and a real loop-mounted CIDATA
#      seed image. That is the only layer where the refusals can be seen to fire,
#      since Subiquity is what normally runs them.
#   3. firstboot.sh steps 3d/3e, carved out and RUN, because the property that
#      matters there (ordering, mode, and a collision notice that is not true on
#      every reboot) cannot be read off the source with grep.
#
# THE RULE THIS SUITE IS HELD TO, after a 2026-08-03 adversarial review found it
# breaking its own rule in four places: A CASE THAT DID NOT RUN IS NOT A PASS.
#   - Extraction that finds no command, or more than one, FAILS. It used to
#     succeed silently, so both negative assertions below passed if the
#     late-command was reverted to a version with no BUILD_PROFILE in it at all.
#   - SKIPPED cases make the whole suite exit non-zero. Root is needed for
#     `install -o root -g root`, loop mounts and a real /media entry; without it
#     the interesting half cannot run, and a green that means "we did not look"
#     is the exact failure this file exists to prevent.
#   - Nothing mutates the repository. The builders run against an ISOLATED COPY
#     of the tracked tree; the deliberately-corrupted templates the substitution
#     assertions need never touch a file git is watching. Before this, a kill at
#     the wrong moment left a broken `user-data` in the checkout — or worse, a
#     concurrent ISO build consumed one.
#
# WHAT THIS SUITE STILL CANNOT DO — and the reason OI-19 stays FIXED-BUT-
# UNVERIFIED: it never installs anything. Nothing here proves Subiquity runs
# these commands in this order or that `curtin` leaves /target where they expect
# it. That needs a hub install, which no agent has.
#
# Usage (WSL/Linux, from a MiniPC-Deployer checkout, AS ROOT):
#   sudo bash vmtest/test-hub-seed.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORK="${TMPDIR:-/tmp}/hub-seed-test.$$"
# Never the repo's own .out/: a V3 gate ISO may be sitting there, and --clean is
# a recursive delete. Everything this suite writes is disposable.
OUT="$WORK/out-dir"
SANDBOX="$WORK/repo"          # the isolated checkout the builders run against
MEDIA_DIR=""                  # a real /media entry, created only while needed
LOOPDEV=""                    # a real loop device, attached only while needed

pass=0; fail=0; skip=0
mkdir -p "$WORK"

cleanup() {
    [ -n "$LOOPDEV" ] && losetup -d "$LOOPDEV" 2>/dev/null
    [ -n "$MEDIA_DIR" ] && rm -rf "$MEDIA_DIR"
    rm -rf "$WORK"
    return 0
}
trap cleanup EXIT

ok()   { printf 'ok    %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf 'FAIL  %s\n      %s\n' "$1" "$2"; fail=$((fail + 1)); }
skip_case() { printf 'SKIP  %s\n      %s\n' "$1" "$2"; skip=$((skip + 1)); }

# ── the isolated tree ────────────────────────────────────────────────────────
# The substitution assertions can only be tested by handing the builder a BROKEN
# user-data/meta-data, and this suite used to do that by overwriting the
# repository's own tracked files and restoring them from an EXIT trap. A trap is
# not a transaction: a SIGKILL, a full disk or a panic leaves the corrupted
# template in the checkout, and a concurrent `build-seed.sh` in another terminal
# would happily bake it onto an ISO. Copy the tracked tree instead and corrupt
# the copy. `git init` + one commit inside it because copy_repo_into_payload
# prefers `git ls-files` and warns loudly on a non-checkout — the copy should
# take the SAME path the real build takes, not the fallback.
build_sandbox() {
    mkdir -p "$SANDBOX"
    if command -v git >/dev/null 2>&1 && git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
        ( cd "$REPO_ROOT" && git ls-files -z | tar -c --null -T - ) | ( cd "$SANDBOX" && tar -x )
        git -C "$SANDBOX" init -q
        # Byte-for-byte: the sandbox must hold exactly what the checkout holds,
        # and on a Windows checkout an autocrlf round-trip would rewrite the very
        # files under test. safecrlf off as well — its warning is about a
        # round-trip this throwaway index will never make, and five lines of it
        # per run is noise across the output that matters.
        git -C "$SANDBOX" config core.autocrlf false
        git -C "$SANDBOX" config core.safecrlf false
        git -C "$SANDBOX" add -A
        git -C "$SANDBOX" -c user.email=vmtest@invalid -c user.name=vmtest \
            commit -q -m "isolated copy under test"
    else
        printf 'NOTE: %s is not a git checkout — copying the whole worktree instead.\n' "$REPO_ROOT"
        ( cd "$REPO_ROOT" && tar -c --exclude=.git --exclude=vmtest/.out --exclude=vmtest/.out-wall . ) \
            | ( cd "$SANDBOX" && tar -x )
    fi
}
build_sandbox
USER_DATA="$SANDBOX/stack/autoinstall/user-data"
META_DATA="$SANDBOX/stack/autoinstall/meta-data"
FB="$SANDBOX/stack/autoinstall/firstboot.sh"
# The pristine originals, read-only, used to restore the sandbox between cases.
PRISTINE_UD="$REPO_ROOT/stack/autoinstall/user-data"
PRISTINE_MD="$REPO_ROOT/stack/autoinstall/meta-data"
for f in "$USER_DATA" "$META_DATA" "$FB"; do
    [ -f "$f" ] || { printf 'FAIL  isolated tree\n      %s did not survive the copy\n' "$f"; exit 1; }
done

# ── extracting the artifacts under test ──────────────────────────────────────
# extract_late_command FILE NEEDLE LABEL OUTFILE — the ONE late-command in FILE
# containing NEEDLE, written to OUTFILE. Non-zero (and a counted FAIL) if there
# is not exactly one.
#
# "Exactly one" is the whole point. The old version broke out of a `for` loop on
# the first hit and printed nothing when there were none — exiting 0 either way.
# So both negative assertions built on it ("no /cdrom left", "no config.json in
# 4b") passed when the command was MISSING, which is precisely the state a
# reverted OI-19 fix leaves behind: an old 4b with no BUILD_PROFILE in it at all
# matched nothing, printed nothing, and every grep over that nothing said "clean".
extract_late_command() {
    local f="$1" needle="$2" label="$3" outfile="$4" err
    err="$(python3 - "$f" "$needle" "$outfile" <<'PY' 2>&1
import sys, yaml
path, needle, outfile = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    doc = yaml.safe_load(open(path))
except Exception as e:                      # noqa: BLE001 - report anything
    sys.exit(f"could not parse {path}: {e}")
cmds = ((doc or {}).get("autoinstall", doc) or {}).get("late-commands") or []
hits = [c for c in cmds if isinstance(c, str) and needle in c]
if len(hits) != 1:
    sys.exit(f"expected exactly ONE late-command containing {needle!r} in {path}, "
             f"found {len(hits)}. Zero means the command under test is GONE "
             f"(reverted, renamed, or never rendered) and every assertion about "
             f"it below would be vacuous; more than one means the wrong body "
             f"could be picked.")
open(outfile, "w").write(hits[0])
PY
)" || { bad "$label" "$err"; return 1; }
    [ -s "$outfile" ] || { bad "$label" "extraction produced an empty command body"; return 1; }
    return 0
}

# runnable_body SRC DEST TARGET_ROOT — turn an extracted `bash -c '…'` entry into
# a script, with /target redirected at a fake root this suite owns.
runnable_body() {
    sed -e "s#^bash -c '##" -e "s#'\$##" -e "s#/target#$3#g" "$1" > "$2"
}

# active_build_profile FILE — the ACTIVE BUILD_PROFILE value(s) in FILE's
# late-commands, one per line, empty if there are none.
#
# Parsed, not grepped, and this test file is the reason the builder is too: a
# `grep -q BUILD_PROFILE=production` over the raw file matches the four lines of
# COMMENT above the marker just as happily as the assignment, so the assertion
# below used to pass on a user-data whose late-command actually runs as `sim`.
# yaml.safe_load throws comments away, so what comes back is only what a shell
# would execute.
active_build_profile() {
    python3 - "$1" <<'PY'
import re, sys, yaml
ai = yaml.safe_load(open(sys.argv[1]))
ai = ai.get("autoinstall", ai)
for c in (ai.get("late-commands") or []):
    if isinstance(c, list):
        c = " ".join(str(x) for x in c)
    if isinstance(c, str):
        for m in re.findall(r"(?<![A-Za-z0-9_])BUILD_PROFILE=([A-Za-z0-9._-]*)", c):
            print(m)
PY
}

# ── a STAND-IN for the baked apt repo ────────────────────────────────────────
# Since 2026-08-06 `packages:` is empty and stage_apt_into_payload REFUSES a
# build with no offline repo, so every case below needs one. Baking the real
# thing costs docker, a network and ~180 MB per run, and none of that is what
# this suite is testing — what it tests is the STAGER and its stamp guard.
#
# So: a directory with the shape the stager judges — a non-empty Packages index,
# at least one *.deb, and a packages.baked.list. The list is DERIVED from the
# tracked packages.list with the same three rules everything else uses, so the
# stamp check is exercised for real rather than sidestepped; fake_apt_repo takes
# a list path precisely so the drift case can hand it a different one.
#
# NOT ALLOW_MISSING_APT=1, deliberately. That would make every case below skip
# the stager entirely and the suite would stop covering the newest thing in the
# build chain — a green meaning "we did not look", which is what this file's
# header argues against.
fake_apt_repo() {
    local dir="$1" list="${2:-$SANDBOX/stack/autoinstall/packages.list}"
    rm -rf "$dir"; mkdir -p "$dir"
    printf 'Package: vmtest-stand-in\nVersion: 0\nArchitecture: all\n\n' > "$dir/Packages"
    : > "$dir/vmtest-stand-in_0_all.deb"
    awk '{ sub(/#.*/, ""); gsub(/[[:space:]]/, ""); if (length($0)) print }' "$list" \
        > "$dir/packages.baked.list"
}
FAKE_APT="$WORK/apt"
fake_apt_repo "$FAKE_APT"

# build_seed [env...] — one hub seed build into a FRESH $OUT, from the SANDBOX.
# Output in $WORK/out.txt.
# APT_OUT comes FIRST so a case can override it (env takes the last assignment
# of a name); OUT_DIR and IMAGES_OUT stay last because no case may redirect
# where this suite deletes.
build_seed() {
    rm -rf "$OUT"
    env APT_OUT="$FAKE_APT" "$@" OUT_DIR="$OUT" IMAGES_OUT="$WORK/no-images" \
        bash "$SANDBOX/vmtest/build-seed.sh" >"$WORK/out.txt" 2>&1
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
        "$PRISTINE_UD" > "$SITE/user-data.filled"
}
make_site_files() {
    printf 'DOMAIN=house.invalid\n'          > "$SITE/.env"
    printf 'BACKUP_SOURCES="x=/srv"\n'       > "$SITE/backup.env"
    printf 'username=share\npassword=x\n'    > "$SITE/cifs.creds"
    printf 'household:x\n'                   > "$SITE/samba-users.creds"
    printf '[library]\n  path = /srv/library\n' > "$SITE/smb.conf.fragment"
    printf 'LABEL=Library /srv/library ext4 nofail 0 2\n' > "$SITE/library-mounts.fstab"
    printf '/srv/library\tSERIAL123\n'       > "$SITE/drive-identity.conf"
    printf '{ "FEED_TOKEN": "sim-not-a-real-token" }\n' > "$SITE/config.json"
}

# THE SEVEN DESTINATIONS late-command 4b installs to, and the posture each must
# land in. Every one is stat'd individually: the old check stat'd `.env` alone
# and called it "all seven files 0600", so a widened drive-identity.conf — the
# one carrying disk serials, and the one nothing had ever installed before
# 2026-08-03 — or any later chmod regression passed unnoticed.
SITE_DESTS="/opt/homehub/stack/.env
/etc/homehub-backup/backup.env
/etc/homehub-backup/cifs.creds
/etc/homehub-samba/samba-users.creds
/etc/homehub-samba/smb.conf.fragment
/etc/homehub-samba/library-mounts.fstab
/etc/homehub-samba/drive-identity.conf"
site_install_problems() {
    local t="$1" d p problems=""
    while IFS= read -r d; do
        [ -n "$d" ] || continue
        if [ ! -f "$t$d" ]; then problems="$problems $d=ABSENT"; continue; fi
        p="$(stat -c '%a:%U:%G' "$t$d" 2>/dev/null)"
        [ "$p" = "600:root:root" ] || problems="$problems $d=$p"
    done <<< "$SITE_DESTS"
    [ -f "$t/etc/homehub-samba/.site-present" ] || problems="$problems /etc/homehub-samba/.site-present=ABSENT"
    printf '%s' "$problems"
}

echo "=== the production seam (builder) ==="
expect_refusal "SITE_DIR with no user-data.filled is refused, not half-applied" \
    "has no user-data.filled" -- "SITE_DIR=$SITE"

make_site_filled
make_site_files
# A materialised tree built BEFORE the OI-19 fix carries the old late-command,
# which exits 0 silently when the payload has no site/. The stick would look
# fine and the box would come up on placeholders.
sed 's/BUILD_PROFILE=production;/BUILD_PROFILE=legacy;/' "$SITE/user-data.filled" > "$SITE/ud.stale"
mv "$SITE/ud.stale" "$SITE/user-data.filled"
expect_refusal "a user-data.filled predating the OI-19 fix is refused (stale out\\ tree)" \
    "expected 'production'" -- "SITE_DIR=$SITE"
make_site_filled

# THE CASE THE OLD grep COULD NOT SEE. Same file, comments untouched — only the
# ACTIVE assignment flipped to sim. `grep -q BUILD_PROFILE=production` matched
# the comment four lines above it and passed the build; the resulting stick then
# installs with 4b in sim mode, which SILENTLY PERMITS a missing site/ on a
# machine whose meta-data, hostname and disk pin all say production.
awk 'NR==1 {print; print "# NOTE (a comment, not a setting): rendered with BUILD_PROFILE=production."; next} {print}' \
    "$SITE/user-data.filled" > "$SITE/ud.commented"
sed -i 's/BUILD_PROFILE=production;/BUILD_PROFILE=sim;/' "$SITE/ud.commented"
mv "$SITE/ud.commented" "$SITE/user-data.filled"
expect_refusal "a user-data.filled whose COMMENTS say production but whose ACTIVE marker says sim is refused" \
    "the ACTIVE BUILD_PROFILE assignment is 'sim'" -- "SITE_DIR=$SITE"
make_site_filled

# THE BUILDER HALF of the defect the late-command half is tested for at the end
# of this file. user-data.filled is MANDATORY (refused above if absent) and used
# to be counted as a staged site file — so a SITE_DIR holding nothing else
# satisfied `staged > 0`, built a clean production ISO, and produced a
# deploy-payload/site/ whose only content was the seed's own user-data. 4b then
# found the DIRECTORY it was looking for and installed nothing.
SITE_MIN="$WORK/site-min"; mkdir -p "$SITE_MIN"
cp "$SITE/user-data.filled" "$SITE_MIN/user-data.filled"
expect_refusal "a SITE_DIR holding ONLY user-data.filled is refused (it used to build a production ISO)" \
    "is missing required site file(s): .env" -- "SITE_DIR=$SITE_MIN"

# config.json is required INDEPENDENTLY of Personal's Build-VentoyStick.ps1
# putting it in the stick's site/ — two repos, two guards, on purpose.
mv "$SITE/config.json" "$SITE/config.json.away"
expect_refusal "a production build with no site/config.json is refused (the panel would run on js/config.js defaults, silently)" \
    "missing required site file(s): config.json" -- "SITE_DIR=$SITE"
mv "$SITE/config.json.away" "$SITE/config.json"

echo
echo "=== the SIM build ==="
if build_seed; then
    U="$OUT/iso-root/user-data"; M="$OUT/iso-root/meta-data"
    cp "$U" "$WORK/sim-user-data"
    [ "$(active_build_profile "$U")" = sim ] \
        && ok "a sim image's ACTIVE marker is BUILD_PROFILE=sim, and there is exactly one (so its install does not demand secrets)" \
        || bad "sim BUILD_PROFILE marker" "active assignments: $(active_build_profile "$U" | tr '\n' ' ')"
    grep -Fxq 'local-hostname: homehub-vmtest' "$M" \
        && ok "a sim seed's meta-data still says homehub-vmtest" \
        || bad "sim meta-data hostname" "$(grep local-hostname "$M")"
else
    bad "the sim build itself" "$(tail -n 3 "$WORK/out.txt" | tr '\n' ' ' | cut -c1-200)"
fi

echo
echo "=== the A19 two-VM lab (sim only) ==="

# THE DEFAULT MUST NOT MOVE. Every V3 gate to date built the `e*` DHCP block,
# and a lab rewrite that fired without being asked would change what the hub
# gate covers without anyone choosing that.
if build_seed; then
    grep -qE '^[[:space:]]*name: "e\*"' "$OUT/iso-root/user-data" \
        && ok "with no SIM_LAB_* set the shipped e* DHCP matcher is untouched (the V3 gate builds the image it always built)" \
        || bad "lab netplan default" "the e* matcher is gone from a build that asked for no lab"
else
    bad "the sim build itself (lab default case)" "$(tail -n 3 "$WORK/out.txt" | tr '\n' ' ' | cut -c1-200)"
fi

LAB_ENV=("SIM_LAB_WAN_MAC=00:15:5D:A1:90:10" "SIM_LAB_MAC=00:15:5D:A1:91:10" "SIM_LAB_ADDR=10.99.7.10/24")
if build_seed "${LAB_ENV[@]}"; then
    U="$OUT/iso-root/user-data"
    probs=""
    grep -q '00:15:5D:A1:90:10' "$U" || probs="$probs no-wan-mac"
    grep -q '00:15:5D:A1:91:10' "$U" || probs="$probs no-lab-mac"
    grep -q '10.99.7.10/24'     "$U" || probs="$probs no-lab-addr"
    grep -qE '^[[:space:]]*name: "e\*"' "$U" && probs="$probs e*-matcher-survived"
    [ -z "$probs" ] \
        && ok "SIM_LAB_* rewrites the network block into two MAC-pinned legs and consumes the e* matcher" \
        || bad "A19 lab netplan" "$probs"
    # It still has to be a file Subiquity accepts — the builder validates the
    # YAML, so a build that got this far already proved it, but the SHAPE is
    # what a hand-edit would break: a lab leg with a default route would send
    # the installer's apt traffic into a switch with no internet.
    grep -qE '^[[:space:]]*(gateway4|via):' "$U" \
        && bad "A19 lab netplan" "the lab leg carries a gateway — the default route belongs to the DHCP leg" \
        || ok "the lab leg carries NO gateway (apt keeps the NAT leg's default route)"
else
    bad "the A19 lab build" "$(tail -n 3 "$WORK/out.txt" | tr '\n' ' ' | cut -c1-200)"
fi

expect_refusal "a lab address with no WAN MAC is refused (one unpinned NIC is an undecidable eth0)" \
    "SIM_LAB_WAN_MAC is not" -- "SIM_LAB_ADDR=10.99.7.10/24" "SIM_LAB_MAC=00:15:5D:A1:91:10"
expect_refusal "a lab address with no prefix length is refused (netplan needs CIDR)" \
    "has no prefix length" -- "SIM_LAB_ADDR=10.99.7.10" "${LAB_ENV[1]}" "${LAB_ENV[0]}"
expect_refusal "an UNSCOPED lab nameserver is refused (apt would resolve through a hub that does not exist yet)" \
    "SIM_LAB_SEARCH is not" -- "${LAB_ENV[@]}" "SIM_LAB_DNS=10.99.7.10"

# THE ONE THAT COST A TWO-HOUR INSTALL. The name was correct everywhere except
# at the stub: systemd-resolved synthesises NXDOMAIN for .invalid without ever
# querying the link's DNS server, so Technitium answered and the panel still
# failed. Every special-use TLD resolved handles itself must be refused, and the
# message has to say WHY or the next person picks another one.
for bad in vmtest.sim.invalid lab.localhost gate.local; do
    expect_refusal "a lab search domain under '.${bad##*.}' is refused (resolved answers it itself and never asks the hub)" \
        "SPECIAL-USE" -- "${LAB_ENV[@]}" "SIM_LAB_DNS=10.99.7.10" "SIM_LAB_SEARCH=$bad"
done
if build_seed "${LAB_ENV[@]}" "SIM_LAB_DNS=10.99.7.10" "SIM_LAB_SEARCH=vmtest.sim"; then
    ok "…and a '.sim' search domain builds (the convention sim/.env.sim already uses)"
else
    bad "a resolvable lab search domain" "$(tail -n 3 "$WORK/out.txt" | tr '\n' ' ' | cut -c1-200)"
fi

echo
echo "=== the A19 gate's drive-lane feed (defect #17) ==="
LAB_FEED=("${LAB_ENV[@]}" "SIM_ENV_OVERRIDES=PANEL_USER_SUB=sim-user-wallpanel-0003")
if build_seed "${LAB_FEED[@]}"; then
    B="$OUT/iso-root/deploy-payload/sim-gate/backup.env"
    if [ -f "$B" ]; then
        probs=""
        grep -q '^NAGLIGHT_FEED_CONTAINER=tracker' "$B" || probs="$probs no-container-route"
        grep -q '^NAGLIGHT_USER=sim-user-wallpanel-0003' "$B" || probs="$probs sub-not-the-panels"
        grep -qE '^BACKUP_TARGET=/' "$B" || probs="$probs no-backup-target"
        [ "$(stat -c '%a' "$B")" = "600" ] || probs="$probs mode=$(stat -c '%a' "$B")"
        [ -z "$probs" ] \
            && ok "a lab build renders sim-gate/backup.env 0600, posting via the tracker container, attributed to the PANEL's sub" \
            || bad "sim gate backup.env" "$probs"
    else
        bad "sim gate backup.env" "no sim-gate/backup.env — both drive lanes would log 'journal only' and A19 could not see red"
    fi
else
    bad "the lab feed build" "$(tail -n 3 "$WORK/out.txt" | tr '\n' ' ' | cut -c1-200)"
fi

# The sub is what makes the report land where the panel reads. Attributed to
# anyone else it is invisible, and invisible is what A19 already looked like.
expect_refusal "a lab build whose sim .env has no PANEL_USER_SUB is refused (the report would go to nobody)" \
    "attributed to nobody" -- "${LAB_ENV[@]}" "SIM_ENV_OVERRIDES=PANEL_USER_SUB="

# An ORDINARY hub gate must be untouched — a reporting path that switches itself
# on would change what the V3 gate covers without anyone choosing it.
if build_seed; then
    [ ! -e "$OUT/iso-root/deploy-payload/sim-gate" ] \
        && ok "a NON-lab hub build stages no sim-gate/ (the V3 gate still covers exactly what it did)" \
        || bad "sim gate scope" "sim-gate/ was staged on a build that asked for no lab"
else
    bad "the plain sim build (scope case)" "$(tail -n 3 "$WORK/out.txt" | tr '\n' ' ' | cut -c1-200)"
fi
make_site_filled
make_site_files
expect_refusal "SIM_LAB_* on a PRODUCTION build is refused rather than silently discarded" \
    "sim-only rewrite" -- "SITE_DIR=$SITE" "${LAB_ENV[@]}"

echo
echo "=== the sim hub's TLS, and the tracker seed seam ==="

# The kiosk site has never served TLS on a gate VM: the sim DOMAIN ends in
# .invalid, which no ACME challenge can validate. A19 is the first gate with a
# client that has to complete a handshake.
if build_seed; then
    CF="$OUT/iso-root/deploy-payload/stack/caddy/Caddyfile"
    grep -qE '^[[:space:]]*local_certs[[:space:]]*$' "$CF" \
        && ok "the SIM payload's Caddyfile issues from Caddy's internal CA (public ACME cannot validate a .invalid name)" \
        || bad "sim TLS delta" "no local_certs in the staged Caddyfile — the panel would get no certificate"
    grep -qE '^[[:space:]]*local_certs[[:space:]]*$' "$REPO_ROOT/stack/caddy/Caddyfile" \
        && bad "sim TLS delta" "the TRACKED Caddyfile declares local_certs — that is the sim's business, and on a real hub it is a browser warning on every household device" \
        || ok "the TRACKED Caddyfile still uses public ACME (the delta lives in the builder, not in the file)"
    E="$OUT/iso-root/deploy-payload/stack/.env"
    grep -qE '^TRACKER_SEED_DIR=' "$E" && grep -qE '^TRACKER_SEED_SRC=' "$E" \
        && ok "the seed-source knobs exist in the staged .env, so SIM_ENV_OVERRIDES can point the gate hub at sim/tracker-seed" \
        || bad "tracker seed knobs" "TRACKER_SEED_DIR/_SRC missing from the staged .env — an override naming them would be refused"
    grep -qE '^TRACKER_SEED_DIR=[^[:space:]]' "$E" \
        && bad "tracker seed default" "the staged .env SEEDS BY DEFAULT — a real household's first user would be given a fixture's definitions" \
        || ok "seeding is OFF by default (blank TRACKER_SEED_DIR = the entrypoint passes no --seed)"
    [ -d "$OUT/iso-root/deploy-payload/stack/tracker-seed" ] && \
    [ ! -d "$OUT/iso-root/deploy-payload/stack/tracker-seed/definitions" ] \
        && ok "the DEFAULT seed source ships with no definitions/, so the bind mount is inert even if the env var is set by mistake" \
        || bad "default seed source" "stack/tracker-seed is absent, or carries a definitions/ dir it must not have"
else
    bad "the sim build itself (TLS/seed case)" "$(tail -n 3 "$WORK/out.txt" | tr '\n' ' ' | cut -c1-200)"
fi

echo
echo "=== the PRODUCTION build (OI-19's companion: the sim identity leaked into it) ==="
PROD_SEED=""
if build_seed "SITE_DIR=$SITE"; then
    U="$OUT/iso-root/user-data"; M="$OUT/iso-root/meta-data"
    grep -Fxq 'local-hostname: homehub' "$M" \
        && ok "a production seed's meta-data says homehub, NOT homehub-vmtest" \
        || bad "production meta-data hostname" "$(grep local-hostname "$M")"
    grep -Eq '^instance-id: homehub-[0-9]+$' "$M" \
        && ok "a production seed's instance-id is homehub-<ts>, not homehub-vmtest-<ts>" \
        || bad "production instance-id" "$(grep instance-id "$M")"
    [ "$(active_build_profile "$U")" = production ] \
        && ok "a production image's ACTIVE marker is BUILD_PROFILE=production (its install refuses to lose site/)" \
        || bad "production BUILD_PROFILE marker" "active assignments: $(active_build_profile "$U" | tr '\n' ' ')"
    [ -f "$OUT/iso-root/deploy-payload/site/.env" ] \
        && ok "the real site/ files ride the payload (where late-command 4b now reads them)" \
        || bad "site staging" "no deploy-payload/site/.env"
    [ -f "$OUT/iso-root/deploy-payload/site/config.json" ] \
        && ok "site/config.json rides the payload too (the kiosk shell's runtime config)" \
        || bad "config.json staging" "no deploy-payload/site/config.json"
    # Keep this seed ISO: the CIDATA case below mounts it for real.
    if [ -f "$OUT/seed.iso" ]; then
        PROD_SEED="$WORK/production-seed.iso"
        cp "$OUT/seed.iso" "$PROD_SEED"
    fi
else
    bad "the production build itself" "$(tail -n 3 "$WORK/out.txt" | tr '\n' ' ' | cut -c1-200)"
fi

echo
echo "=== the payload's MODES (found by BOOTING, 2026-08-04) ==="
# WHY THIS SECTION EXISTS. Every hub ISO ever built on the dev box installed
# /opt/homehub world-writable: `drwxrwxrwx root:root`, `-rwxrwxrwx stack/.env`,
# `-rwxrwxrwx docker-compose.yml`, 230 world-writable paths, and `sudo -u nobody`
# could read every credential in .env and WRITE the compose file root brings up.
# It survived every static test, every sim run and multiple ISO builds for one
# reason: NOTHING IN THIS REPO HAD EVER LOOKED AT A MODE. Every other assertion
# reads text, and a mode is not text. It took an install to find it.
#
# The property is asserted on the STAGED TREE here because this suite stages
# under $TMPDIR (a real Linux filesystem). The builder ALSO asserts the ISO it
# writes, which is the only layer that can speak on the default build host —
# there OUT_DIR is a Windows drive, DrvFs reports 0777 for everything and
# discards chmod outright.
if build_seed "SITE_DIR=$SITE"; then
    P="$OUT/iso-root/deploy-payload"
    nbad=$(find "$P" -perm /022 | wc -l)
    ntotal=$(find "$P" | wc -l)
    if [ "$nbad" -eq 0 ]; then
        ok "no group- or world-writable path anywhere in the staged payload ($ntotal paths)"
    else
        bad "payload modes" "$nbad of $ntotal staged paths are group- or world-writable, e.g. $(find "$P" -perm /022 -printf '%M %P\n' | head -n 3 | tr '\n' ' ')"
    fi

    # THE POLICY, FILE BY FILE — not a count. A single stat'd file was exactly
    # how the site-install check used to claim "all seven are 0600" while only
    # ever looking at .env. Each row is here because it is a DIFFERENT class:
    # a directory, a data file, two scripts git records as 100644 (core.filemode
    # is false on the Windows checkout, so the index cannot be the source of the
    # executable bit), the materialised-config directory and its secrets.
    wrong=""
    check_mode() {
        local got; got="$(stat -c%a "$P/$1" 2>/dev/null)"
        [ "$got" = "$2" ] || wrong="$wrong $1=${got:-ABSENT}(want $2)"
    }
    check_mode stack                                  755
    check_mode stack/docker-compose.yml               644
    check_mode stack/autoinstall/user-data            644
    check_mode stack/provision/provision-samba.sh     755
    check_mode stack/autoinstall/firstboot.sh         755
    check_mode stack/samba/library-guard.sh           755
    check_mode site                                   700
    check_mode site/.env                              600
    check_mode site/cifs.creds                        600
    check_mode site/config.json                       600
    check_mode stack/.env                             600
    if [ -z "$wrong" ]; then
        ok "the mode policy holds per file: dirs 0755, data 0644, #! scripts 0755, site/ 0700 with 0600 secrets, stack/.env 0600"
    else
        bad "the mode policy" "$wrong"
    fi

    # AND IN THE ARTIFACT. The staged tree is not what boots; the ISO is, and
    # `cp -a` reproduces exactly what Rock Ridge recorded.
    if [ -f "$OUT/seed.iso" ] && command -v xorriso >/dev/null 2>&1; then
        isobad=$(xorriso -indev "$OUT/seed.iso" -find /deploy-payload -exec lsdl -- 2>/dev/null \
                 | awk '/^[-dl]/ { m = substr($1,1,10); if (substr(m,6,1)=="w" || substr(m,9,1)=="w") c++ } END { print c+0 }')
        if [ "$isobad" = "0" ]; then
            ok "the SEED ISO records no group- or world-writable path under /deploy-payload (this is what cp -a copies)"
        else
            bad "ISO modes" "$isobad group/world-writable entries under /deploy-payload in seed.iso"
        fi
    else
        skip_case "the seed ISO's recorded modes" "no seed.iso or no xorriso — the artifact half of the mode check could not be read, and that is not a pass"
    fi
else
    bad "the build for the mode cases" "$(tail -n 3 "$WORK/out.txt" | tr '\n' ' ' | cut -c1-200)"
fi

# ── DOES THE GUARD BITE? ─────────────────────────────────────────────────────
# The repo's recent history is full of tests that passed whether or not the code
# was there, so prove this one fails when the normalisation is taken away. The
# STUB goes into the sandbox's copy of common.sh (a later definition wins over
# the earlier one), never into the checkout — and it still sets
# PAYLOAD_MODE_FIX_AT_ISO, so what is being removed is the mode policy alone and
# not the handshake between the two enforcement layers.
cat >> "$SANDBOX/vmtest/lib/common.sh" <<'STUB'

# ── STUB appended by vmtest/test-hub-seed.sh, in the SANDBOX copy only ──
normalize_payload_modes() { PAYLOAD_MODE_FIX_AT_ISO=0; log "STUB: normalisation disabled"; }
STUB
expect_refusal "with normalize_payload_modes STUBBED OUT the build REFUSES (the guard bites)" \
    "group- or world-writable path(s)" -- "SITE_DIR=$SITE"
cp "$REPO_ROOT/vmtest/lib/common.sh" "$SANDBOX/vmtest/lib/common.sh"
if build_seed "SITE_DIR=$SITE"; then
    ok "…and builds green again once it is restored (so the refusal was the stub, not the suite)"
else
    bad "restore after the bite proof" "$(tail -n 3 "$WORK/out.txt" | tr '\n' ' ' | cut -c1-200)"
fi

echo
echo "=== the OFFLINE apt repo (2026-08-06) ==="
# `packages:` is empty, so the baked repo is not an optimisation — it is the only
# thing that installs anything. Both ways it can be wrong end in the same place:
# a box that boots, answers pings and has no sshd. Both must stop the BUILD.
expect_refusal "a build with NO baked apt repo REFUSES (an ISO that installs nothing)" \
    "no baked apt repo" -- APT_OUT="$WORK/apt-absent"

# THE STAMP. The debs are frozen when export-apt.sh runs; the names are read
# from the tracked list when the box installs. Add a package, rebuild the ISO
# without re-baking, and the install asks apt for something the repo has never
# heard of — on a dead network, which is the whole failure being removed.
cp "$SANDBOX/stack/autoinstall/packages.list" "$WORK/list-drifted"
printf '\nvmtest-package-that-was-never-baked\n' >> "$WORK/list-drifted"
fake_apt_repo "$WORK/apt-stale" "$SANDBOX/stack/autoinstall/packages.list"
cp "$WORK/list-drifted" "$SANDBOX/stack/autoinstall/packages.list"
expect_refusal "a baked repo that DISAGREES with packages.list REFUSES" \
    "DISAGREE" -- APT_OUT="$WORK/apt-stale"
cp "$REPO_ROOT/stack/autoinstall/packages.list" "$SANDBOX/stack/autoinstall/packages.list"

# And the artifact itself: the repo has to be ON the ISO, not merely resolved.
if build_seed; then
    if [ -s "$OUT/iso-root/deploy-payload/apt/Packages" ] \
       && [ -s "$OUT/iso-root/deploy-payload/apt/packages.baked.list" ] \
       && [ -n "$(find "$OUT/iso-root/deploy-payload/apt" -name '*.deb' -print -quit)" ]; then
        ok "the baked repo lands at deploy-payload/apt/ (index, list and .deb(s))"
    else
        bad "the baked repo lands at deploy-payload/apt/" \
            "staged: $(ls -1 "$OUT/iso-root/deploy-payload/apt" 2>/dev/null | tr '\n' ' ')"
    fi
    # The payload's own copy of the tracked list is what late-command 3c's
    # sibling checks read; if the repo copy lost it, nothing on the box can say
    # what the install was supposed to contain.
    if [ -s "$OUT/iso-root/deploy-payload/stack/autoinstall/packages.list" ]; then
        ok "the tracked packages.list rides the payload too (assert-installed.sh's cross-check)"
    else
        bad "the tracked packages.list rides the payload" "absent from deploy-payload/stack/autoinstall/"
    fi
else
    bad "the baked repo lands at deploy-payload/apt/" "$(tail -3 "$WORK/out.txt" | tr '\n' ' ')"
fi

echo
echo "=== the substitution assertions (a silent no-op must fail the build) ==="
grep -v 'BUILD_PROFILE=production' "$PRISTINE_UD" > "$USER_DATA"
expect_refusal "a user-data that lost BUILD_PROFILE=production fails the sim build" \
    "BUILD_PROFILE substitution did not apply" --
cp "$PRISTINE_UD" "$USER_DATA"

grep -v '^local-hostname:' "$PRISTINE_MD" > "$META_DATA"
expect_refusal "a meta-data that lost local-hostname: fails the build" \
    "meta-data local-hostname is not" --
cp "$PRISTINE_MD" "$META_DATA"

echo
echo "=== what the late-commands say (structure) ==="
# The static half of the OI-19 fix: the boot-medium assumption is GONE from the
# SITE step. Step 3 is the one place that searches (/cdrom, /media, /media/*,
# /run/media/*, then the CIDATA volume by label) and it copies site/ along with
# everything else. Both of these run through extract_late_command, so a missing
# command is a FAIL rather than a vacuous pass.
if extract_late_command "$USER_DATA" "BUILD_PROFILE" "extracting late-command 4b" "$WORK/4b.raw"; then
    if grep -qE '/cdrom|/media' "$WORK/4b.raw"; then
        bad "the site step no longer reads the boot medium" "it still names /cdrom or /media"
    else
        ok "the site step no longer reads the boot medium (one discovery path, in step 3)"
    fi
    # config.json's destination is a WEB ROOT that firstboot step 3d untars over,
    # so it must NOT be in 4b's table — firstboot step 3e owns it.
    if grep -q 'config.json' "$WORK/4b.raw"; then
        bad "config.json is not in late-command 4b" "it is — and 3d would overwrite it"
    else
        ok "config.json is NOT in late-command 4b's table (its destination is the web root)"
    fi
fi

echo
echo "=== firstboot steps 3d/3e, EXECUTED (not grepped) ==="
# Ordering, mode and the collision notice were all previously asserted by
# grepping firstboot.sh for line numbers and literal strings — which proves the
# source contains some text, not that the code runs, is reachable, or does what
# the text implies. Carve the region out and run it.
H="$WORK/fb3de"
mkdir -p "$H/stack/wall-site" "$H/site" "$H/src/site"
awk '/3d\. unpack the wall kiosk site/{f=1} /4\. bring the stack up/{f=0} f' "$FB" > "$H/body.sh"
if ! grep -q 'tar -xzf "\$WALL_SITE_TARBALL"' "$H/body.sh" || ! grep -q 'WALL_SITE_CONFIG' "$H/body.sh"; then
    bad "carving firstboot steps 3d/3e out" \
        "the region between the '3d. unpack…' and '4. bring the stack up' markers does not contain both the untar and WALL_SITE_CONFIG — the markers moved, so nothing below would be testing what it claims"
else
    {
        printf '%s\n' '#!/usr/bin/env bash' \
                      '# Carved out of stack/autoinstall/firstboot.sh by test-hub-seed.sh.' \
                      'set -euo pipefail' \
                      "STACK_DIR=\"$H/stack\"" \
                      'log() { echo "[firstboot] $*"; }'
        sed -e "s#/opt/homehub/site/config.json#$H/site/config.json#" \
            -e "s#/opt/homehub/wall-site#$H/absent-a#" \
            -e "s#/cdrom/deploy-payload/wall-site#$H/absent-b#" \
            "$H/body.sh"
    } > "$H/run.sh"

    printf '<html>SIM-WALL-SHELL-FIXTURE</html>\n' > "$H/src/site/index.html"
    printf '{"revision":"0123456789ab"}\n'         > "$H/src/site/build-info.json"
    printf '{"FEED_TOKEN":"materialised-not-a-real-token"}\n' > "$H/site/config.json"
    make_tarball() {  # make_tarball WITH_CONFIG(yes|no)
        rm -f "$H/src/site/config.json"
        [ "$1" = yes ] && printf '{"FEED_TOKEN":"PLACEHOLDER-FROM-THE-TARBALL"}\n' > "$H/src/site/config.json"
        rm -f "$H"/stack/wall-site/officewall-site-*.tar.gz
        tar -czf "$H/stack/wall-site/officewall-site-test.tar.gz" -C "$H/src" site
    }
    run_3de() { rm -rf "$H/stack/wall-shell"; bash "$H/run.sh" >"$H/log.txt" 2>&1; echo $?; }
    run_3de_again() { bash "$H/run.sh" >"$H/log.txt" 2>&1; echo $?; }

    # (a) the ordinary production case: the tarball has no config.json of its
    #     own, the materialised one is installed over the unpacked docroot.
    make_tarball no
    rc=$(run_3de)
    cfg="$H/stack/wall-shell/config.json"
    modes="$(stat -c '%a:%U:%G' "$cfg" 2>/dev/null)"
    if [ "$rc" -eq 0 ] && [ -f "$H/stack/wall-shell/index.html" ] && [ "$modes" = "600:root:root" ] \
       && grep -q 'materialised-not-a-real-token' "$cfg"; then
        ok "3d/3e RUN: the site unpacks and the materialised config.json lands on top of it, 600:root:root"
    else
        bad "3d/3e execution" "rc=$rc mode=${modes:-none} index=$([ -f "$H/stack/wall-shell/index.html" ] && echo yes || echo no)"
    fi
    if grep -q 'shipped its own config.json' "$H/log.txt"; then
        bad "collision notice on a clean run" "it claimed the tarball shipped a config.json when the tarball has none"
    else
        ok "3d/3e RUN: no collision notice when the tarball ships no config.json"
    fi

    # (b) THE RERUN. homehub-firstboot.service has no marker guard and no
    #     ConditionPath*, so it runs again after every reboot. The old check
    #     asked `[ -f wall-shell/config.json ]` AFTER the untar — which on run
    #     two finds the file run one installed, and shouted "the tarball shipped
    #     its own config.json" on every hub, forever.
    rc=$(run_3de_again)
    if [ "$rc" -eq 0 ] && ! grep -q 'shipped its own config.json' "$H/log.txt"; then
        ok "3d/3e RERUN (no fresh install): still no collision notice — it is answered from the ARCHIVE, not from its own output"
    else
        bad "collision notice on rerun" "rc=$rc — the notice fired on a second run with the same tarball"
    fi

    # (c) the collision that IS real must still be reported.
    make_tarball yes
    rc=$(run_3de)
    if [ "$rc" -eq 0 ] && grep -q 'shipped its own config.json' "$H/log.txt" \
       && grep -q 'materialised-not-a-real-token' "$H/stack/wall-shell/config.json"; then
        ok "3d/3e RUN: a tarball that DOES ship a config.json is reported, and the materialised one still wins"
    else
        bad "real collision" "rc=$rc notice=$(grep -c 'shipped its own' "$H/log.txt") content=$(head -c 60 "$H/stack/wall-shell/config.json" 2>/dev/null)"
    fi
fi

echo
echo "=== the late-commands, EXECUTED against a fake /target (OI-19) ==="
if [ "$(id -u)" -ne 0 ]; then
    skip_case "every behavioural case (late-commands 3 and 4b)" \
        "not root — 'install -o root -g root', loop mounts and a real /media entry are what the installer does and cannot be faked here. Re-run with sudo."
else
    # fake_target KIND -> sets T (a fake /target) and CMD4B (4b, ready to run).
    fake_target() {
        local kind="$1" src
        T="$WORK/t-$kind-$RANDOM"
        case "$kind" in
            production) src="$USER_DATA" ;;                 # the tracked template
            sim)        src="$WORK/sim-user-data" ;;        # as the builder rendered it
        esac
        mkdir -p "$T/opt/homehub/stack" "$T/etc/homehub-samba" "$T/etc/homehub-backup"
        CMD4B="$WORK/cmd-4b.sh"
        extract_late_command "$src" "BUILD_PROFILE" "extracting late-command 4b from $kind" "$WORK/4b.raw" || return 1
        runnable_body "$WORK/4b.raw" "$CMD4B" "$T"
    }
    run_4b() { bash "$CMD4B" >"$WORK/lc.txt" 2>&1; echo $?; }

    # Step 3 is the payload copy — the one OI-19 was actually about, and the one
    # nothing had ever executed. Extract it once, here.
    CMD3_SRC="$WORK/lc3.raw"
    HAVE_LC3=no
    extract_late_command "$USER_DATA" 'blkid -L CIDATA' "extracting late-command 3 (the payload copy)" "$CMD3_SRC" \
        && HAVE_LC3=yes
    run_lc3() {  # run_lc3 TARGET_ROOT -> rc, output in $WORK/lc3.txt
        runnable_body "$CMD3_SRC" "$WORK/cmd-3.sh" "$1"
        bash "$WORK/cmd-3.sh" >"$WORK/lc3.txt" 2>&1; echo $?
    }
    stage_payload() {  # stage_payload DIR — a deploy-payload/ as the builder makes one
        mkdir -p "$1/deploy-payload/stack" "$1/deploy-payload/site"
        printf 'DOMAIN=example.invalid\n' > "$1/deploy-payload/stack/.env.example"
        local f
        for f in .env backup.env cifs.creds samba-users.creds smb.conf.fragment \
                 library-mounts.fstab drive-identity.conf; do
            cp "$SITE/$f" "$1/deploy-payload/site/$f"
        done
    }

    fake_target production && {
        rc=$(run_4b)
        if [ "$rc" -ne 0 ] && grep -q "FATAL - this is a PRODUCTION image" "$WORK/lc.txt"; then
            ok "PRODUCTION + no site/ payload REFUSES the install (was: silent exit 0)"
        else
            bad "production refusal" "rc=$rc $(head -n1 "$WORK/lc.txt")"
        fi
    }

    fake_target sim && {
        rc=$(run_4b)
        if [ "$rc" -eq 0 ] && grep -q "SIM build" "$WORK/lc.txt"; then
            ok "SIM + no site/ payload is a clean, LOUD no-op (a vmtest image has no secrets)"
        else
            bad "sim no-op" "rc=$rc $(head -n1 "$WORK/lc.txt")"
        fi
    }

    # ── THE LIGHT PATH, END TO END: step 3 then step 4b ──────────────────────
    # This is the case OI-19 was raised about, and until 2026-08-03 the case
    # "testing" it hand-created /target/opt/homehub/site and ran 4b alone — so it
    # passed with late-command 3 reverted, stubbed, or deleted. Nothing here
    # creates that directory: step 3 has to find the payload and copy it.
    if [ "$HAVE_LC3" = no ]; then
        skip_case "the light path (steps 3 -> 4b)" "late-command 3 could not be extracted (see the FAIL above)"
    else
        MEDIA_DIR="/media/hub-seed-test.$$"
        if [ -e "$MEDIA_DIR" ] || ! mkdir -p "$MEDIA_DIR" 2>/dev/null; then
            MEDIA_DIR=""
            skip_case "the light path via a mounted directory (step 3 -> 4b)" \
                "could not create a real /media entry — step 3's directory search cannot be exercised as written"
        else
            stage_payload "$MEDIA_DIR"
            T="$WORK/t-light-$RANDOM"
            mkdir -p "$T/opt/homehub" "$T/etc/homehub-samba" "$T/etc/homehub-backup"
            rc3=$(run_lc3 "$T")
            CMD4B="$WORK/cmd-4b.sh"
            runnable_body "$WORK/4b.raw" "$CMD4B" "$T"
            rc=$(run_4b)
            problems="$(site_install_problems "$T")"
            if [ "$rc3" -eq 0 ] && [ "$rc" -eq 0 ] && [ -z "$problems" ] \
               && grep -q "payload copied from $MEDIA_DIR/deploy-payload" "$WORK/lc3.txt"; then
                ok "LIGHT PATH: step 3 found the payload under /media and copied it, then 4b installed all seven files 600:root:root"
            else
                bad "light path (media)" "step3 rc=$rc3, 4b rc=$rc, problems:${problems:-none}, step3 said: $(head -n1 "$WORK/lc3.txt")"
            fi

            # FORCED cp FAILURE. Both copy branches used to read
            # `cp -a … && echo …; exit 0` — the `exit 0` a separate command, so
            # it ran whether or not the copy worked, and a half-copied payload
            # reported success. A regular file where the directory must be makes
            # `cp -a src/. dest/` fail deterministically.
            T="$WORK/t-cpfail-$RANDOM"
            mkdir -p "$T/opt"
            : > "$T/opt/homehub"
            rc3=$(run_lc3 "$T")
            if [ "$rc3" -ne 0 ] && grep -q "cp -a FAILED partway" "$WORK/lc3.txt"; then
                ok "a FAILED payload copy halts the install instead of reporting success (a partial deploy unit looks installed)"
            else
                bad "cp failure is fatal" "rc=$rc3 $(head -n2 "$WORK/lc3.txt" | tr '\n' ' ')"
            fi

            rm -rf "$MEDIA_DIR"; MEDIA_DIR=""
        fi

        # ── THE CIDATA SEED, FOR REAL ───────────────────────────────────────
        # The branch the light path actually takes on a hub: no /cdrom, no
        # /media entry, so step 3 falls through to `blkid -L CIDATA`, mounts the
        # seed read-only and copies out of it. This mounts THE ISO THIS SUITE
        # JUST BUILT, so the label, the layout and the mount all have to be real.
        if [ -z "$PROD_SEED" ]; then
            skip_case "the CIDATA-seed path (step 3 -> 4b)" "the production build produced no seed.iso to mount"
        elif ! command -v losetup >/dev/null 2>&1 || ! LOOPDEV="$(losetup --find --show "$PROD_SEED" 2>/dev/null)"; then
            LOOPDEV=""
            skip_case "the CIDATA-seed path (step 3 -> 4b)" \
                "no usable loop device — a genuine loopback mount is not possible here, so step 3's blkid+mount branch is UNTESTED. It is not a pass."
        elif [ "$(blkid -L CIDATA 2>/dev/null)" != "$LOOPDEV" ]; then
            skip_case "the CIDATA-seed path (step 3 -> 4b)" \
                "blkid -L CIDATA resolves to '$(blkid -L CIDATA 2>/dev/null)', not our loop device $LOOPDEV — another CIDATA volume is attached and step 3 would read that one"
        else
            T="$WORK/t-cidata-$RANDOM"
            mkdir -p "$T/opt/homehub" "$T/etc/homehub-samba" "$T/etc/homehub-backup"
            rc3=$(run_lc3 "$T")
            CMD4B="$WORK/cmd-4b.sh"
            runnable_body "$WORK/4b.raw" "$CMD4B" "$T"
            rc=$(run_4b)
            problems="$(site_install_problems "$T")"
            if [ "$rc3" -eq 0 ] && [ "$rc" -eq 0 ] && [ -z "$problems" ] \
               && grep -q "payload copied from the CIDATA seed" "$WORK/lc3.txt"; then
                ok "CIDATA SEED: step 3 found the seed by label, mounted it read-only and copied the payload; 4b then installed all seven files 600:root:root"
            else
                bad "light path (CIDATA)" "step3 rc=$rc3, 4b rc=$rc, problems:${problems:-none}, step3 said: $(head -n1 "$WORK/lc3.txt")"
            fi
            losetup -d "$LOOPDEV" 2>/dev/null; LOOPDEV=""
        fi
    fi

    # ── LATE-COMMAND 3b, EXECUTED: the install-time floor under the mode policy ─
    # The builder is the primary fix, but on the default build host the staging
    # filesystem discards chmod, so a payload can still arrive here with the
    # modes the 2026-08-04 boot found. This step is what makes that unable to
    # survive an install — and it is executed, not grepped, against a fake
    # /target deliberately staged the way DrvFs leaves one: 0777 everywhere.
    CMD3B="$WORK/cmd-3b.sh"
    if extract_late_command "$USER_DATA" "normalised - root-owned" "extracting late-command 3b (mode normalisation)" "$WORK/3b.raw"; then
        T="$WORK/t-modes-$RANDOM"
        mkdir -p "$T/opt/homehub/stack" "$T/opt/homehub/site"
        printf 'services:\n' > "$T/opt/homehub/stack/docker-compose.yml"
        printf 'OAUTH2_PROXY_CLIENT_SECRET=x\n' > "$T/opt/homehub/stack/.env"
        printf 'username=share\npassword=x\n' > "$T/opt/homehub/site/cifs.creds"
        chmod -R 0777 "$T/opt/homehub"
        runnable_body "$WORK/3b.raw" "$CMD3B" "$T"
        rc=$(bash "$CMD3B" >"$WORK/lc3b.txt" 2>&1; echo $?)
        left=$(find "$T/opt/homehub" -perm /022 | wc -l)
        m_env="$(stat -c '%a:%U' "$T/opt/homehub/stack/.env")"
        m_site="$(stat -c%a "$T/opt/homehub/site")"
        m_creds="$(stat -c%a "$T/opt/homehub/site/cifs.creds")"
        m_compose="$(stat -c%a "$T/opt/homehub/stack/docker-compose.yml")"
        if [ "$rc" -eq 0 ] && [ "$left" -eq 0 ] && [ "$m_env" = "600:root" ] \
           && [ "$m_site" = "700" ] && [ "$m_creds" = "600" ] && [ "$m_compose" = "755" ]; then
            ok "3b RUN: a 0777 payload is normalised in place — 0 writable paths left, .env 600:root, site/ 0700, *.creds 0600"
        else
            bad "3b execution" "rc=$rc writable-left=$left .env=$m_env site=$m_site creds=$m_creds compose=$m_compose"
        fi

        # AND IT REFUSES rather than reporting a normalisation it did not get.
        # An immutable file is the cheapest way to make chmod fail for real; if
        # chattr is unavailable this is a SKIP, not a pass.
        T="$WORK/t-modes-fail-$RANDOM"
        mkdir -p "$T/opt/homehub/stack"
        : > "$T/opt/homehub/stack/stuck"
        chmod 0666 "$T/opt/homehub/stack/stuck"
        if chattr +i "$T/opt/homehub/stack/stuck" 2>/dev/null; then
            runnable_body "$WORK/3b.raw" "$CMD3B" "$T"
            rc=$(bash "$CMD3B" >"$WORK/lc3b.txt" 2>&1; echo $?)
            chattr -i "$T/opt/homehub/stack/stuck" 2>/dev/null
            if [ "$rc" -ne 0 ]; then
                ok "3b RUN: a path it could NOT tighten halts the install instead of logging success"
            else
                bad "3b refusal" "rc=0 with a file it could not chmod: $(head -n2 "$WORK/lc3b.txt" | tr '\n' ' ')"
            fi
        else
            skip_case "3b's refusal when a path cannot be tightened" \
                "chattr +i is unavailable here, so chmod cannot be made to fail for real — the refusal branch is UNTESTED, which is not a pass"
        fi
    fi

    fake_target production && {
        mkdir -p "$T/opt/homehub/site"
        cp "$SITE"/.env "$SITE/backup.env" "$SITE/smb.conf.fragment" "$SITE/library-mounts.fstab" "$T/opt/homehub/site/"
        rc=$(run_4b)
        if [ "$rc" -eq 0 ] && grep -q "optional site file MISSING - site/cifs.creds" "$WORK/lc.txt"; then
            ok "an absent OPTIONAL site file is named and not fatal (Build-VentoyStick marks three optional)"
        else
            bad "optional file handling" "rc=$rc $(grep -c MISSING "$WORK/lc.txt") missing lines"
        fi
    }

    fake_target production && {
        mkdir -p "$T/opt/homehub/site"
        cp "$SITE/backup.env" "$SITE/smb.conf.fragment" "$SITE/library-mounts.fstab" "$T/opt/homehub/site/"
        rc=$(run_4b)
        if [ "$rc" -ne 0 ] && grep -q "required site file MISSING - site/.env" "$WORK/lc.txt"; then
            ok "an absent REQUIRED site file HALTS the production install (OI-19's second head: a site/ that exists but is empty of what matters)"
        else
            bad "required file handling" "rc=$rc $(head -n1 "$WORK/lc.txt")"
        fi
    }

    # The finding underneath the finding: a site/ holding ONLY the mandatory
    # user-data.filled used to build, pass 4b's directory check, log six MISSING
    # lines and exit 0 — the OI-19 outcome by a different road.
    fake_target production && {
        mkdir -p "$T/opt/homehub/site"
        make_site_filled
        cp "$SITE/user-data.filled" "$T/opt/homehub/site/user-data.filled"
        rc=$(run_4b)
        if [ "$rc" -ne 0 ]; then
            ok "a site/ carrying ONLY user-data.filled REFUSES (the directory existing was never the property worth checking)"
        else
            bad "empty-but-present site/" "rc=$rc — it exited 0 with every required file missing"
        fi
    }
fi

echo
printf '%s\n' "----------------------------------------"
printf 'hub seed guards: %d passed, %d failed, %d skipped\n' "$pass" "$fail" "$skip"
if [ "$skip" -ne 0 ]; then
    printf 'SKIPPED CASES ARE NOT PASSES, and this suite exits NON-ZERO because of them.\n'
    printf 'A green that means "we could not look" is the false green this file exists to prevent.\n'
fi
printf 'NOTE: nothing here has installed anything. The late-commands are exercised\n'
printf '      against a fake /target, never under Subiquity — OI-19 stays UNVERIFIED\n'
printf '      until a hub is installed from a built ISO.\n'
[ "$fail" -eq 0 ] && [ "$skip" -eq 0 ] || exit 1
