#!/usr/bin/env bash
# vmtest/test-wall-builder.sh — do the wall builder's refusals actually bite?
#
# WHY THIS FILE EXISTS. Every guard in build-wall-seed.sh was exercised by hand
# once, and the result was written into docs/status.md. An adversarial review
# made the obvious objection: a transcript in a ledger is not a check, nothing
# in the tree re-runs it, and a refusal nobody has seen fire since is a comment
# with an `if` in front of it. This turns each of those one-off runs into
# something anyone can re-run in about a minute.
#
# It is mostly a NEGATIVE-path suite on purpose. That the builder produces an ISO
# is proven every time somebody builds one; what needs proving is that it REFUSES
# the inputs that would each produce an image whose failure is invisible on the
# wall — plus, since OI-18, that the two media flows really do behave DIFFERENTLY
# where the ruling says they must.
#
# TWO THINGS THE 2026-08-04 REVIEW CHANGED ABOUT THE SUITE ITSELF, and both are
# about the suite lying rather than about the code:
#
#  1. IT MUTATED THE TRACKED TREE. The knob-name cases rewrote
#     `stack/autoinstall/wall/wall.env.example`, and the dependency-gate cases
#     rewrote `electron-runtime-deps.tsv` and `user-data`, restoring them from a
#     backup afterwards — with an EXIT trap that removed only the temp dir. An
#     interrupt at the wrong moment left the repo damaged. Everything now runs
#     against an ISOLATED SANDBOX CHECKOUT (`git ls-files` -> `tar` -> `git
#     init`), the same pattern test-hub-seed.sh already uses, so no case can
#     touch a tracked file.
#
#  2. TWENTY-SIX CASES, AND A SUITE THAT PASSED WITH THE CODE DELETED. All eight
#     wall-sync assertions SKIPPED on a non-root run or an occupied loopback 445,
#     and a skip is not a failure — so `wall-sync.sh` could be deleted, or left
#     unparseable, and this exited zero. There is now a STATIC GATE that always
#     runs and always FAILS: the scripts must exist and parse, the unit files
#     must parse and say what the design says they say, and the autoinstall must
#     actually install and enable them. The root-only cases still skip, but they
#     are no longer the only thing standing between a deleted file and a green
#     suite.
#
# SECTIONS:
#   0.   the STATIC gate — always runs, never skips (scripts parse; units parse
#        and match the design; user-data installs and enables them; firstboot
#        judges the enable instead of claiming it)
#   1-3. the builder's guards (the original suite)
#   4.   the OI-18 production seam — two credential files, staged and reported
#        individually, the payload copies removed, and the retired single one
#        refused
#   5.   cross-repo knob names
#   6.   wall-sync.sh's TWO FLOWS and the knob-containment property, exercised
#        through the bench fixture mode: the frame share's content is at its
#        share ROOT while music sits under a subdir; an unreachable frame source
#        is a SKIP while an unreachable music source is an ALERT; and nothing in
#        wall.env can move the subtree, the destination or the flow set.
#        Section 6 needs root (it takes a lock under /run, calls mount, and
#        stages fixtures under /var/lib/wall-sync/bench); without it the cases
#        SKIP loudly rather than vanishing.
#
# Usage (WSL/Linux, from a MiniPC-Deployer checkout):
#   bash vmtest/test-wall-builder.sh
#   WALL_SHELL_DIST=/path/to/dist bash vmtest/test-wall-builder.sh
#
# Needs the real OfficeWallNaglight artifact for the cases that get past the
# "is it there at all" gate; those are SKIPPED (loudly, and counted) without it,
# because a suite that quietly shrinks when its input is missing is the same
# false green it is here to prevent.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORK="${TMPDIR:-/tmp}/wall-builder-test.$$"
SANDBOX="$WORK/repo"           # the isolated checkout every case runs against
DIST_REAL="${WALL_SHELL_DIST:-$(cd "$REPO_ROOT/.." && pwd)/OfficeWallNaglight/dist}"
# wall-sync.sh's fixture root is a CONSTANT in the script (a debug hook must not
# be aimable at /etc/wall-panel), so the suite has to stage its fixtures there.
BENCH_ROOT="/var/lib/wall-sync/bench"

pass=0; fail=0; skip=0
mkdir -p "$WORK"
cleanup_all() {
    rm -rf "$WORK"
    # Only ever remove the fixtures this suite created, and only the ones it
    # named — never $BENCH_ROOT itself, which an operator may be using.
    rm -rf "$BENCH_ROOT/tc-music" "$BENCH_ROOT/tc-frame" "$BENCH_ROOT/tc-empty" \
           "$BENCH_ROOT/tc-manifest-only" 2>/dev/null || true
}
trap cleanup_all EXIT

# ── the sandbox ──────────────────────────────────────────────────────────────
# `git ls-files` so the copy is exactly the TRACKED tree (no vmtest/.out, no
# .git), then `git init` + one commit inside it because copy_repo_into_payload
# prefers `git ls-files` and warns loudly on a non-checkout.
build_sandbox() {
    mkdir -p "$SANDBOX"
    # `-c safe.directory='*'`: a checkout owned by someone other than the caller
    # makes git refuse to list it, and a SILENTLY EMPTY sandbox would turn every
    # case below into a failure with a misleading reason. The post-condition
    # below is the belt to that braces — this must never fail quietly.
    ( cd "$REPO_ROOT" && git -c safe.directory='*' ls-files -z | tar -c --null -T - ) \
        | ( cd "$SANDBOX" && tar -x )
    git -C "$SANDBOX" init -q
    git -C "$SANDBOX" config core.autocrlf false
    git -C "$SANDBOX" config core.safecrlf false
    git -C "$SANDBOX" add -A
    git -C "$SANDBOX" -c user.email=vmtest@invalid -c user.name=vmtest \
        commit -qm "sandbox" >/dev/null
    if [ ! -f "$SANDBOX/vmtest/build-wall-seed.sh" ] || [ ! -d "$SANDBOX/stack/autoinstall/wall" ]; then
        printf 'FATAL: the sandbox checkout under %s is empty or incomplete.\n' "$SANDBOX" >&2
        printf 'Nothing was tested. Is %s a git checkout, and is it readable by this user?\n' "$REPO_ROOT" >&2
        exit 2
    fi
}
build_sandbox

WALL_DIR="$SANDBOX/stack/autoinstall/wall"
SYNC="$WALL_DIR/wall-sync.sh"
BUILDER="$SANDBOX/vmtest/build-wall-seed.sh"

# ── a STAND-IN for the baked apt repo (2026-08-06) ───────────────────────────
# `packages:` is empty, so stage_apt_into_payload REFUSES a build with no
# offline repo — every case below needs one. Baking the real thing costs docker,
# a network and ~130 MB per run, and none of that is what this suite tests: what
# it tests is the STAGER and its stamp guard. So, the shape the stager judges —
# a non-empty Packages index, a *.deb, and a packages.baked.list DERIVED from
# the tracked packages.list, so the stamp check runs for real. Not
# ALLOW_MISSING_APT=1, which would make every case skip the stager entirely.
fake_apt_repo() {
    local dir="$1" list="${2:-$WALL_DIR/packages.list}"
    rm -rf "$dir"; mkdir -p "$dir"
    printf 'Package: vmtest-stand-in\nVersion: 0\nArchitecture: all\n\n' > "$dir/Packages"
    : > "$dir/vmtest-stand-in_0_all.deb"
    awk '{ sub(/#.*/, ""); gsub(/[[:space:]]/, ""); if (length($0)) print }' "$list" \
        > "$dir/packages.baked.list"
}
FAKE_APT="$WORK/apt"
fake_apt_repo "$FAKE_APT"

# refresh_fake_apt — re-derive the stamp after a case has edited packages.list.
# The dependency-gate cases below REMOVE a package from the list to prove the
# refusal; without this the stager would then refuse first, for drift, and the
# case would report the wrong reason.
refresh_fake_apt() { fake_apt_repo "$FAKE_APT"; }

skip_case() { printf 'SKIP  %s\n      %s\n' "$1" "$2"; skip=$((skip + 1)); }
pass_case() { printf 'ok    %s\n' "$1"; pass=$((pass + 1)); }
fail_case() { printf 'FAIL  %s\n      %s\n' "$1" "$2"; fail=$((fail + 1)); }

# assert_file_matches NAME FILE PATTERN [--absent] : one grep -E, judged.
assert_file_matches() {
    local name="$1" file="$2" pat="$3" mode="${4:-present}"
    if [ ! -f "$file" ]; then
        fail_case "$name" "no such file: $file"
        return
    fi
    if grep -Eq -- "$pat" "$file"; then
        if [ "$mode" = "--absent" ]; then
            fail_case "$name" "the file still matches /$pat/, and it must not"
        else
            pass_case "$name"
        fi
    else
        if [ "$mode" = "--absent" ]; then
            pass_case "$name"
        else
            fail_case "$name" "$file does not match /$pat/"
        fi
    fi
}

# expect_refusal NAME EXPECTED_SUBSTRING -- env... -- : the build must exit
# non-zero AND say why. Both halves matter: a guard that fires with the wrong
# message costs the next person the same hour it was supposed to save.
expect_refusal() {
    local name="$1" needle="$2"; shift 2
    [ "$1" = "--" ] && shift
    local out="$WORK/out.txt"
    # APT_OUT FIRST so a case can override it (env takes the last assignment
    # of a name); OUT_DIR stays last because no case may redirect where this
    # suite deletes.
    if env APT_OUT="$FAKE_APT" "$@" OUT_DIR="$WORK/out-dir" bash "$BUILDER" >"$out" 2>&1; then
        printf 'FAIL  %s\n      the build SUCCEEDED; it was supposed to refuse\n' "$name"
        fail=$((fail + 1)); return
    fi
    if grep -qF "$needle" "$out"; then
        printf 'ok    %s\n' "$name"
        pass=$((pass + 1))
    else
        printf 'FAIL  %s\n      refused, but not for the stated reason. Wanted: %s\n      Got: %s\n' \
            "$name" "$needle" "$(grep -m1 FATAL "$out" | cut -c1-160)"
        fail=$((fail + 1))
    fi
}

expect_success() {
    local name="$1" needle="$2"; shift 2
    [ "$1" = "--" ] && shift
    local out="$WORK/out.txt"
    if env APT_OUT="$FAKE_APT" "$@" OUT_DIR="$WORK/out-dir" bash "$BUILDER" >"$out" 2>&1 \
       && grep -qF "$needle" "$out"; then
        printf 'ok    %s\n' "$name"
        pass=$((pass + 1))
    else
        printf 'FAIL  %s\n      %s\n' "$name" "$(tail -n 3 "$out" | tr '\n' ' ' | cut -c1-200)"
        fail=$((fail + 1))
    fi
}

# expect_absent NAME NEEDLE -- env... : the build must SUCCEED and must NOT say
# NEEDLE. The asymmetric half of expect_success — without it, "names the MUSIC
# source as the casualty" and "names the FRAME source as the casualty" were run
# against the identical both-absent state and a predicate that always fired for
# both would have passed them both.
expect_absent() {
    local name="$1" needle="$2"; shift 2
    [ "$1" = "--" ] && shift
    local out="$WORK/out.txt"
    if ! env APT_OUT="$FAKE_APT" "$@" OUT_DIR="$WORK/out-dir" bash "$BUILDER" >"$out" 2>&1; then
        fail_case "$name" "the build FAILED; it was supposed to succeed. $(grep -m1 FATAL "$out" | cut -c1-160)"
        return
    fi
    if grep -qF "$needle" "$out"; then
        fail_case "$name" "the build still said: $needle"
    else
        pass_case "$name"
    fi
}

echo "=== 0. the static gate (never skips) ==="
# THE POINT OF THIS SECTION: it needs no root, no loopback port and no shell
# artifact, so it runs on every invocation. Before it existed, deleting
# wall-sync.sh left this suite exiting zero.

for f in wall-sync.sh wall-firstboot.sh wall-kiosk.sh wall-sleep.sh; do
    if [ -f "$WALL_DIR/$f" ] && bash -n "$WALL_DIR/$f" 2>/dev/null; then
        pass_case "$f exists and parses (bash -n)"
    else
        fail_case "$f exists and parses (bash -n)" "missing, or bash -n refused it"
    fi
done
if [ -f "$WALL_DIR/wall-media-manifest.py" ] \
   && python3 -c 'import ast,sys; ast.parse(open(sys.argv[1],encoding="utf-8").read())' \
        "$WALL_DIR/wall-media-manifest.py" 2>/dev/null; then
    pass_case "wall-media-manifest.py exists and parses"
else
    fail_case "wall-media-manifest.py exists and parses" "missing, or it does not compile"
fi

# The unit files. `systemd-analyze verify` is the real parser; it is not on every
# box, so it is used when present and the exact-content assertions below stand on
# their own either way. Its exit status is unusable here (it also complains that
# ExecStart's target is absent on a build host), so the FATAL classes are matched
# out of its output instead.
UNITS="wall-sync.service wall-sync-frame.service wall-sync-frame.timer wall-sync-resume.service wall-firstboot.service"
if command -v systemd-analyze >/dev/null 2>&1; then
    UV="$WORK/unitverify.txt"
    ( cd "$WALL_DIR" && systemd-analyze verify $(for u in $UNITS; do printf './%s ' "$u"; done) ) >"$UV" 2>&1 || true
    if grep -Eiq 'Unknown key|Failed to parse|Invalid (section|value|setting)|Unknown section|syntax error' "$UV"; then
        fail_case "systemd-analyze verify accepts every wall unit" \
            "$(grep -Eim2 'Unknown key|Failed to parse|Invalid |Unknown section|syntax error' "$UV" | tr '\n' ' ' | cut -c1-200)"
    else
        pass_case "systemd-analyze verify accepts every wall unit"
    fi
else
    skip_case "systemd-analyze verify accepts every wall unit" \
        "systemd-analyze is not installed on this host; the exact-content assertions below still run"
fi

# The frame flow's cadence, spelled out. Nothing used to read these files at all:
# they could have been EMPTY and every case in this suite still passed.
assert_file_matches "wall-sync-frame.service runs the sync with EXACTLY --only frame" \
    "$WALL_DIR/wall-sync-frame.service" '^ExecStart=/usr/local/sbin/wall-sync\.sh --only frame$'
assert_file_matches "wall-sync-frame.service is bounded (TimeoutStartSec)" \
    "$WALL_DIR/wall-sync-frame.service" '^TimeoutStartSec=[0-9]+$'
assert_file_matches "wall-sync-frame.timer fires a minute after the run FINISHES" \
    "$WALL_DIR/wall-sync-frame.timer" '^OnUnitInactiveSec=1min$'
# OnUnitActiveSec measures from ACTIVATION, so a run longer than the period
# re-fires immediately — and the file used to claim, in a comment, that it meant
# "after the last run finished".
assert_file_matches "wall-sync-frame.timer does NOT use OnUnitActiveSec" \
    "$WALL_DIR/wall-sync-frame.timer" '^OnUnitActiveSec=' --absent
assert_file_matches "wall-sync-frame.timer triggers the frame service" \
    "$WALL_DIR/wall-sync-frame.timer" '^Unit=wall-sync-frame\.service$'
assert_file_matches "wall-sync.service still syncs BOTH flows (no --only)" \
    "$WALL_DIR/wall-sync.service" '^ExecStart=/usr/local/sbin/wall-sync\.sh$'

# The autoinstall half: units that exist in the repo but are never copied or
# enabled are units the panel does not have.
UD="$WALL_DIR/user-data"
assert_file_matches "user-data installs wall-sync-frame.service" "$UD" \
    'cp .*wall/wall-sync-frame\.service /target/etc/systemd/system/wall-sync-frame\.service'
assert_file_matches "user-data installs wall-sync-frame.timer" "$UD" \
    'cp .*wall/wall-sync-frame\.timer /target/etc/systemd/system/wall-sync-frame\.timer'
assert_file_matches "user-data enables the frame TIMER (not the service)" "$UD" \
    'systemctl enable wall-sync-frame\.timer'
assert_file_matches "user-data does NOT enable wall-sync-frame.service itself" "$UD" \
    'systemctl enable wall-sync-frame\.service' --absent
assert_file_matches "user-data installs both credentials 0600 root:root" "$UD" \
    'install -m 0600 -o root -g root'
# The on-box exposure the review found: staged credentials are 0600 but owned by
# the BUILDER's uid, Rock Ridge (`-rock`) preserves it, `cp -a` carries it into
# /opt/wall-panel/site, and the ordinary builder uid and the panel's `panel` user
# are both 1000. A chmod does not fix an owner; deleting the copy does.
assert_file_matches "user-data REMOVES the staged credential copies from the payload" "$UD" \
    'rm -f "\$s"'
assert_file_matches "user-data no longer merely chmods the staged copies" "$UD" \
    '^[^#]*chmod 0600 "\$s"' --absent
# 2026-08-04: the payload's own permissions, normalised at install time as the
# floor under the builder's policy — and ORDERED BEFORE the shell untar, because
# a recursive chmod after it would strip chrome-sandbox's setuid bit and leave a
# permanently dark panel whose `[ -x ]` is still true.
assert_file_matches "user-data normalises the payload's modes at install time" "$UD" \
    'chmod -R go-w "\$R"'
assert_file_matches "…and takes ownership rather than only fixing the mode (the OI-18 uid lesson)" "$UD" \
    'chown -R 0:0 "\$R"'
if awk '/chmod -R go-w/ { norm = NR } /tar -xzf "\$t" -C \/target\/opt\/wall-panel/ { untar = NR }
        END { exit !(norm && untar && norm < untar) }' "$UD"; then
    pass_case "the mode normalisation runs BEFORE the shell untar (chrome-sandbox keeps its 4755)"
else
    fail_case "late-command order" "the recursive chmod is not ordered before the tar -xzf of the shell artifact — it would strip the setuid bit"
fi

# firstboot must JUDGE the enable, not claim it. This is the repo's signature
# bug: `systemctl enable … || warn` followed by an unconditional "enabled" line.
FB="$WALL_DIR/wall-firstboot.sh"
assert_file_matches "wall-firstboot.sh enables the frame timer through the judged helper" \
    "$FB" '^\s*enable_unit "OI-18: wall-sync-frame\.timer enabled'
assert_file_matches "wall-firstboot.sh has no 'systemctl enable … || warn' left" \
    "$FB" 'systemctl enable.*\|\|' --absent
assert_file_matches "wall-firstboot.sh withholds the marker when a step failed" \
    "$FB" 'PROVISION_FAILED.*-ne 0'

# The knob-containment property, asserted statically as well as behaviourally
# (section 6): the allowlist must exist and must not carry the retired knobs.
assert_file_matches "wall-sync.sh has a configuration ALLOWLIST" \
    "$SYNC" '^CONFIG_KEYS='
assert_file_matches "the allowlist does not include the retired MEDIA_CIFS_EXTRA" \
    "$SYNC" '^CONFIG_KEYS=.*MEDIA_CIFS_EXTRA' --absent
assert_file_matches "the allowlist does not include the retired bench overrides" \
    "$SYNC" '^CONFIG_KEYS=.*SOURCE_OVERRIDE' --absent
assert_file_matches "wall.env.example declares MEDIA_CIFS_VERS" \
    "$WALL_DIR/wall.env.example" '^MEDIA_CIFS_VERS='
assert_file_matches "wall.env.example no longer offers MEDIA_CIFS_EXTRA as a knob" \
    "$WALL_DIR/wall.env.example" '^MEDIA_CIFS_EXTRA=' --absent
assert_file_matches "wall.env.example no longer offers the bench overrides as knobs" \
    "$WALL_DIR/wall.env.example" '^# *MEDIA_(MUSIC|FRAME)_SOURCE_OVERRIDE=' --absent

echo
echo "=== 1. the artifact gate ==="
EMPTY="$WORK/empty-dist"; mkdir -p "$EMPTY"
expect_refusal "an absent shell artifact stops the build" \
    "no OfficeWallNaglight shell artifact" -- "WALL_SHELL_DIST=$EMPTY"
expect_success "ALLOW_MISSING_SHELL=1 is the documented, loud way past it" \
    "building a wall ISO with NO shell artifact" -- "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1

if [ -n "$(ls "$DIST_REAL"/officewall-shell-*-linux-x64.tar.gz 2>/dev/null)" ]; then
    REAL="$(ls "$DIST_REAL"/officewall-shell-*-linux-x64.tar.gz | head -n1)"

    D="$WORK/dirty-dist"; mkdir -p "$D"
    cp -l "$REAL" "$D/officewall-shell-0.1.0-gdeadbee-dirty-linux-x64.tar.gz" 2>/dev/null \
        || cp "$REAL" "$D/officewall-shell-0.1.0-gdeadbee-dirty-linux-x64.tar.gz"
    expect_refusal "a -dirty artifact is refused (its commit describes nothing)" \
        "UNCOMMITTED tree" -- "WALL_SHELL_DIST=$D"

    A="$WORK/ambiguous-dist"; mkdir -p "$A"
    cp -l "$REAL" "$A/" 2>/dev/null || cp "$REAL" "$A/"
    cp "$REAL" "$A/officewall-shell-0.2.0-gaaaaaaa-linux-x64.tar.gz"
    expect_refusal "two candidate artifacts are refused (which ships would be a coin flip)" \
        "more than one artifact matches" -- "WALL_SHELL_DIST=$A"

    echo
    echo "=== 2. the runtime-dependency gate ==="
    # Mutating the SANDBOX, never the checkout.
    T="$WALL_DIR/electron-runtime-deps.tsv"
    # THE LIST MOVED (2026-08-06): the packages the gate judges are in
    # packages.list now, not in the user-data's `packages:` (which is empty —
    # it runs before any late-command and so cannot be served by the baked
    # offline repo). The case below removes a name from THAT file.
    PL="$WALL_DIR/packages.list"
    cp "$T" "$WORK/tsv.bak"; cp "$PL" "$WORK/pkglist.bak"

    # WALL_SHELL_DIST IS NOT OPTIONAL HERE (fixed 2026-08-04, out of the mode
    # pass). Both cases used to invoke the builder with no dist at all, so it
    # resolved OfficeWallNaglight as a sibling of the SANDBOX — which is a temp
    # directory with no sibling — and refused for "no shell artifact" instead of
    # for the dependency reason under test. On a machine with the sibling absent
    # the whole section SKIPS and nobody noticed; on one where it is present
    # these two cases FAIL, at HEAD, before this change. The gate they guard —
    # an Electron bump that needs a library this image does not install — was
    # therefore unexercised on both kinds of machine.
    grep -v '^libnspr4.so' "$WORK/tsv.bak" > "$T"
    expect_refusal "a soname the table has never heard of is refused" \
        "this repo has never heard of" -- "WALL_SHELL_DIST=$DIST_REAL"
    cp "$WORK/tsv.bak" "$T"

    # Drop libnspr4 from the LIST, and re-derive the fake repo's stamp from the
    # edited list — otherwise stage_apt_into_payload refuses first, for drift,
    # and the case reports a reason that is not the one under test.
    grep -v '^libnspr4$' "$WORK/pkglist.bak" > "$PL"
    refresh_fake_apt
    expect_refusal "a mapped package the image does not install is refused" \
        "does not install package(s)" -- "WALL_SHELL_DIST=$DIST_REAL"
    cp "$WORK/pkglist.bak" "$PL"
    refresh_fake_apt
else
    skip_case "-dirty / ambiguous / dependency-gate cases" \
        "no shell artifact at $DIST_REAL — build one with 'npm run dist' in OfficeWallNaglight"
fi

echo
echo "=== 3. the production seam ==="
S="$WORK/site"; mkdir -p "$S"
expect_refusal "WALL_SITE_DIR with no user-data.filled is refused, not half-applied" \
    "has no user-data.filled" -- "WALL_SITE_DIR=$S"

# EVERY placeholder the tracked template carries, not the three this fixture was
# written against. The panel became STATICALLY addressed on 2026-08-06 (the
# Owner: hold the address on the panel, keep it outside the DHCP pool), which
# added REPLACE_WITH_PANEL_CIDR, _LAN_GATEWAY and _HUB_IP — and left every case
# in this section refusing for "an ACTIVE setting still holds a REPLACE_WITH"
# instead of for the reason it names. A fixture that stops resembling what
# Materialize-Deploy emits stops testing the guards downstream of it.
sed -e 's/REPLACE_WITH_WIFI_SSID/TestNet/' -e 's/REPLACE_WITH_WIFI_PSK/testpsk123/' \
    -e 's/REPLACE_WITH_PANEL_CIDR/10.0.0.50\/24/' \
    -e 's/REPLACE_WITH_LAN_GATEWAY/10.0.0.1/' \
    -e 's/REPLACE_WITH_HUB_IP/10.0.0.2/' \
    -e 's#- "ssh-ed25519 AAAA_REPLACE_WITH_YOUR_PUBLIC_KEY you@host"#- "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItestkey t@t"#' \
    "$UD" > "$S/user-data.filled"
grep -Eq '^[[:space:]]*[A-Za-z_-]+:.*REPLACE_WITH' "$S/user-data.filled" && \
    fail_case "the production fixture is placeholder-free" \
        "the sed above no longer covers every REPLACE_WITH in the tracked wall user-data; every case in sections 3 and 4 will refuse for that instead of for its own reason"
expect_refusal "a production build with no wall.env is refused (the panel would have no origin)" \
    "no wall.env" -- "WALL_SITE_DIR=$S"

sed 's/^WALL_HOST=.*/WALL_HOST=wall.test.invalid/' \
    "$WALL_DIR/wall.env.example" > "$S/wall.env"
sed -i 's/model: KINGSTON.*/model: Virtual_Disk/' "$S/user-data.filled"
expect_refusal "a production image pinned to the SIM disk is refused" \
    "Virtual_Disk" -- "WALL_SITE_DIR=$S"

sed -i 's/model: Virtual_Disk/model: KINGSTON*RBU-SNS8152S3256GG2/' "$S/user-data.filled"
sed -i 's/allow-pw: false/allow-pw: true/' "$S/user-data.filled"
expect_refusal "a production image with allow-pw: true is refused (the panel is key-only)" \
    "allow-pw: true" -- "WALL_SITE_DIR=$S"

sed -i -e 's/allow-pw: true/allow-pw: false/' -e 's/^    wifis:/    ethernets:/' "$S/user-data.filled"
expect_refusal "a production image with no Wi-Fi is refused (the panel has no RJ45)" \
    "no ethernet port" -- "WALL_SITE_DIR=$S"

echo
echo "=== 4. the production seam: TWO hosts, ONE credential (Q-S7) ==="
# The last case above rewrote `wifis:` away to prove the no-Wi-Fi refusal; put it
# back, or every case below refuses for that reason instead of the one it tests.
sed -i 's/^    ethernets:/    wifis:/' "$S/user-data.filled"

# THE PANEL NOW HOLDS EXACTLY ONE CREDENTIAL (Q-S7, the Owner, 2026-08-05).
# //homehub/Media went anonymous, so the music mount authenticates with nothing
# and cifs-music.creds was retired outright. What used to be a symmetric
# two-credential seam is now asymmetric BY DESIGN, and both halves need proving:
# the frame credential is still reported when missing, and the music one is
# REFUSED when present rather than quietly ignored.
rm -f "$S/cifs-music.creds"
expect_success "a missing cifs-frame.creds names the FRAME source as the casualty" \
    "cannot mount its FRAME VIDEO share" -- "WALL_SITE_DIR=$S" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1

# THE MUSIC SOURCE MUST NEVER BE BLAMED AGAIN. A panel with no HOMEHUB
# credential is fully configured, so any surviving "cannot mount its MUSIC
# share" note would send the operator off to recreate an account that Q-S7
# deleted — the exact loop this change exists to end.
expect_absent "…and never blames the MUSIC source, which needs no credential at all" \
    "cannot mount its MUSIC share" -- "WALL_SITE_DIR=$S" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1

printf 'username=testframe\npassword=notarealpassword\n' > "$S/cifs-frame.creds"
expect_success "the FRAME credential is staged BY NAME" \
    "site/ += cifs-frame.creds" -- "WALL_SITE_DIR=$S" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1
expect_absent "no music credential is staged, because none is emitted any more" \
    "site/ += cifs-music.creds" -- "WALL_SITE_DIR=$S" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1
expect_success "a fully-configured production panel stages exactly wall.env + cifs-frame.creds" \
    "PRODUCTION build: 2 wall config file(s) staged" -- "WALL_SITE_DIR=$S" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1

# A STALE cifs-music.creds IS A REFUSAL, not a skip. It is a HOMEHUB Samba
# password, and the whole point of Q-S7 is that a wall-mounted panel no longer
# carries one; staging it silently would put it back and nothing would say so.
printf 'username=testmusic\npassword=notarealpassword\n' > "$S/cifs-music.creds"
expect_refusal "a stale cifs-music.creds is REFUSED, naming it as a pre-Q-S7 materialisation" \
    "STALE materialisation" -- "WALL_SITE_DIR=$S" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1
rm -f "$S/cifs-music.creds"

# A stale out\wall\ from before OI-18 carries ONE file called cifs.creds. Staged
# silently it would produce a panel with no media at all, and the failure would
# read as "Personal never emitted anything" rather than "you are baking a stale
# directory". One credential cannot authenticate on two hosts, so say so.
printf 'username=stale\npassword=notarealpassword\n' > "$S/cifs.creds"
expect_refusal "a stale PRE-OI-18 single cifs.creds is refused, not silently ignored" \
    "PRE-OI-18 shape" -- "WALL_SITE_DIR=$S" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1
rm -f "$S/cifs.creds"

# THE SIM'S TLS ESCAPE HATCH, ON A PRODUCTION PANEL. The A19 gate hub serves the
# kiosk site from Caddy's internal CA, so the sim WALL_APP_CMD carries
# --ignore-certificate-errors. The only way that reaches a real wall.env is a
# copy-paste from a sim one — and the origin it would stop validating is the one
# that injects this household's identity header.
cp "$S/wall.env" "$WORK/wallenv-prod.bak"
sed -i 's|^WALL_APP_CMD=.*|WALL_APP_CMD=/opt/wall-panel/app/wall-shell --ignore-certificate-errors|' "$S/wall.env"
expect_refusal "a PRODUCTION wall.env carrying the sim's --ignore-certificate-errors is refused" \
    "SIM-ONLY" -- "WALL_SITE_DIR=$S" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1
cp "$WORK/wallenv-prod.bak" "$S/wall.env"

expect_refusal "SIM_LAB_* on a PRODUCTION wall build is refused rather than silently discarded" \
    "sim-only rewrite" -- "WALL_SITE_DIR=$S" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1 \
    "SIM_LAB_WAN_MAC=00:15:5D:A1:90:50" "SIM_LAB_MAC=00:15:5D:A1:91:50" "SIM_LAB_ADDR=10.99.7.50/24"

echo
echo "=== 4c. the A19 two-VM lab, and the panel's TLS delta (sim only) ==="
if env APT_OUT="$FAKE_APT" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1 OUT_DIR="$WORK/out-dir" \
        bash "$BUILDER" >"$WORK/out.txt" 2>&1; then
    assert_file_matches "with no SIM_LAB_* the shipped e* DHCP matcher survives (the wall gate builds the image it always built)" \
        "$WORK/out-dir/iso-root/user-data" '^[[:space:]]*name: "e\*"'
    assert_file_matches "the SIM panel ignores certificate errors, because the gate hub mints its own root and no image can carry it" \
        "$WORK/out-dir/iso-root/deploy-payload/site/wall.env" '^WALL_APP_CMD=.*--ignore-certificate-errors'
    assert_file_matches "…and still disables the GPU (a second flag must not displace the first)" \
        "$WORK/out-dir/iso-root/deploy-payload/site/wall.env" '^WALL_APP_CMD=.*--disable-gpu'
else
    fail_case "the plain sim wall build (4c baseline)" "$(tail -n 3 "$WORK/out.txt" | tr '\n' ' ' | cut -c1-200)"
fi

# WALL_SIM_HOST is not decoration here: a lab build is refused without a
# resolvable name (see the .invalid cases below), which is the whole lesson of
# 2026-08-04. This case predated that guard and asked for the impossible.
if env APT_OUT="$FAKE_APT" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1 OUT_DIR="$WORK/out-dir" \
        WALL_SIM_HOST=wall.vmtest.sim \
        SIM_LAB_WAN_MAC=00:15:5D:A1:90:50 SIM_LAB_MAC=00:15:5D:A1:91:50 \
        SIM_LAB_ADDR=10.99.7.50/24 SIM_LAB_DNS=10.99.7.10 SIM_LAB_SEARCH=vmtest.sim \
        bash "$BUILDER" >"$WORK/out.txt" 2>&1; then
    U="$WORK/out-dir/iso-root/user-data"
    assert_file_matches "the panel's lab leg is pinned by MAC, not by name" "$U" 'macaddress: "00:15:5D:A1:91:50"'
    assert_file_matches "the panel's lab leg is static" "$U" 'addresses:'
    assert_file_matches "the e* matcher is CONSUMED — it would otherwise match both legs and race the pinned stanzas" \
        "$U" '^[[:space:]]*name: "e\*"' --absent
    # The scoped resolver is the whole reason the panel can use the hub's
    # Technitium without sending `archive.ubuntu.com` to a box that does not
    # exist during the install.
    assert_file_matches "the lab nameserver is SCOPED by a search domain (resolved routes only lab names there)" \
        "$U" 'search:'
    assert_file_matches "…and the wall image still declares no Wi-Fi after the second rewrite" \
        "$U" '^[[:space:]]*(wifis|access-points):' --absent
else
    fail_case "the A19 lab wall build" "$(tail -n 3 "$WORK/out.txt" | tr '\n' ' ' | cut -c1-200)"
fi

# THE PANEL'S HALF OF THE .invalid LESSON. WALL_HOST's sim default is
# deliberately unresolvable, which is right for a lone panel and fatal for a lab
# one — measured 2026-08-04, when Technitium answered correctly and the panel
# still ended at ERR_NAME_NOT_RESOLVED because resolved never asked it.
expect_refusal "a LAB wall build keeping the unresolvable .invalid WALL_HOST default is refused" \
    "SPECIAL-USE" -- "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1 \
    "SIM_LAB_WAN_MAC=00:15:5D:A1:90:50" "SIM_LAB_MAC=00:15:5D:A1:91:50" "SIM_LAB_ADDR=10.99.7.50/24"
expect_success "…and the same build with a '.sim' WALL_HOST succeeds" \
    "WALL_HOST=wall.vmtest.sim" -- "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1 \
    "WALL_SIM_HOST=wall.vmtest.sim" \
    "SIM_LAB_WAN_MAC=00:15:5D:A1:90:50" "SIM_LAB_MAC=00:15:5D:A1:91:50" "SIM_LAB_ADDR=10.99.7.50/24"
# A NON-lab wall build must keep the unresolvable default: that is the
# containment property, and only the lab case has a reason to give it up.
expect_success "a NON-lab wall build still gets the deliberately unresolvable .invalid default" \
    "WALL_HOST=wall.vmtest.sim.invalid" -- "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1

echo
echo "=== 4b. the payload's MODES (the hub's 2026-08-04 defect is this image's too) ==="
# Booting the HUB gate VM found /opt/homehub and 230 paths under it 0777, with
# an unprivileged account able to read every credential and write the compose
# file root runs. /opt/wall-panel is populated by the identical `cp -a` out of
# the identical kind of staging tree, so it arrived the identical way — and here
# it COMPOUNDS OI-18, which found the staged credentials readable by `panel`
# through a uid-1000 collision. At 0777 they were readable by everyone.
#
# Nothing in this repo had ever asserted a mode until now; every other check
# reads text. That is why an install found it and three suites did not.
if env APT_OUT="$FAKE_APT" "WALL_SITE_DIR=$S" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1 \
       OUT_DIR="$WORK/out-dir" bash "$BUILDER" >"$WORK/out.txt" 2>&1; then
    P="$WORK/out-dir/iso-root/deploy-payload"
    nbad=$(find "$P" -perm /022 | wc -l)
    ntotal=$(find "$P" | wc -l)
    if [ "$nbad" -eq 0 ]; then
        pass_case "no group- or world-writable path anywhere in the staged wall payload ($ntotal paths)"
    else
        fail_case "wall payload modes" "$nbad of $ntotal paths are group- or world-writable, e.g. $(find "$P" -perm /022 -printf '%M %P\n' | head -n 3 | tr '\n' ' ')"
    fi

    wrong=""
    check_mode() {
        local got; got="$(stat -c%a "$P/$1" 2>/dev/null)"
        [ "$got" = "$2" ] || wrong="$wrong $1=${got:-ABSENT}(want $2)"
    }
    check_mode stack/autoinstall/wall                    755
    check_mode stack/autoinstall/wall/wall.env.example   644
    check_mode stack/autoinstall/wall/wall-sync.sh       755
    check_mode stack/autoinstall/wall/wall-kiosk.sh      755
    check_mode site                                      700
    check_mode site/wall.env                             600
    check_mode site/cifs-frame.creds                     600
    if [ -z "$wrong" ]; then
        pass_case "the mode policy holds per file: dirs 0755, data 0644, #! scripts 0755, site/ 0700 with 0600 credentials"
    else
        fail_case "the wall mode policy" "$wrong"
    fi

    if [ -f "$WORK/out-dir/wall-seed.iso" ] && command -v xorriso >/dev/null 2>&1; then
        isobad=$(xorriso -indev "$WORK/out-dir/wall-seed.iso" -find /deploy-payload -exec lsdl -- 2>/dev/null \
                 | awk '/^[-dl]/ { m = substr($1,1,10); if (substr(m,6,1)=="w" || substr(m,9,1)=="w") c++ } END { print c+0 }')
        if [ "$isobad" = "0" ]; then
            pass_case "the WALL SEED ISO records no group- or world-writable path under /deploy-payload"
        else
            fail_case "wall ISO modes" "$isobad group/world-writable entries under /deploy-payload"
        fi
    else
        skip_case "the wall seed ISO's recorded modes" "no wall-seed.iso or no xorriso — the artifact half could not be read, and that is not a pass"
    fi
else
    fail_case "the build for the wall mode cases" "$(tail -n 3 "$WORK/out.txt" | tr '\n' ' ' | cut -c1-200)"
fi

# DOES IT BITE? Stub the normalisation out in the SANDBOX's common.sh (a later
# definition wins) and the build must refuse. This suite's whole 2026-08-04
# lesson was a section that exited zero with the code it guarded disabled.
cat >> "$SANDBOX/vmtest/lib/common.sh" <<'STUB'

# ── STUB appended by vmtest/test-wall-builder.sh, in the SANDBOX copy only ──
normalize_payload_modes() { PAYLOAD_MODE_FIX_AT_ISO=0; log "STUB: normalisation disabled"; }
STUB
expect_refusal "with normalize_payload_modes STUBBED OUT the wall build REFUSES (the guard bites)" \
    "group- or world-writable path(s)" -- "WALL_SITE_DIR=$S" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1
cp "$REPO_ROOT/vmtest/lib/common.sh" "$SANDBOX/vmtest/lib/common.sh"
expect_success "…and builds green again once it is restored (so the refusal was the stub, not the suite)" \
    "payload modes OK" -- "WALL_SITE_DIR=$S" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1

echo
echo "=== 5. cross-repo knob names: the sim renderer must fill BOTH UNCs ==="
# THE SINGLE BIGGEST RISK IN OI-18 IS A KNOB NAME DRIFTING BETWEEN THE REPOS.
# `set_env_key` already refuses to append a key wall.env.example does not declare
# (a knob the consumer never reads is a silent no-op). These two cases prove that
# guard is actually wired to BOTH of the new names — delete either `set_env_key`
# call from render_sim_wall_env and the corresponding case stops refusing.
WEX="$WALL_DIR/wall.env.example"
cp "$WEX" "$WORK/wallenv.bak"
for knob in MEDIA_MUSIC_SHARE_UNC MEDIA_FRAME_SHARE_UNC; do
    grep -v "^${knob}=" "$WORK/wallenv.bak" > "$WEX"
    expect_refusal "a sim build refuses if wall.env.example loses $knob" \
        "'$knob' is not a key" -- "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1
    cp "$WORK/wallenv.bak" "$WEX"
done

# THE OTHER HALF OF THE CROSS-REPO RISK, which nothing in this repo could see:
# these cases only ever inspected THIS repo, so Personal could rename or delete
# the knobs and both would still pass. If the sibling checkout is present, assert
# the names line up END TO END. If it is not, SKIP loudly — an assertion that
# silently evaporates on a machine without the sibling is the same false green.
PERSONAL="${PERSONAL_REPO:-$(cd "$REPO_ROOT/.." 2>/dev/null && pwd)/Personal}"
if [ -d "$PERSONAL/homelab/deploy" ]; then
    for token in MEDIA_MUSIC_SHARE_UNC MEDIA_FRAME_SHARE_UNC cifs-frame.creds; do
        if grep -rqF -- "$token" "$PERSONAL/homelab/deploy" 2>/dev/null; then
            pass_case "Personal's deploy half still names '$token'"
        else
            fail_case "Personal's deploy half still names '$token'" \
                "nothing under $PERSONAL/homelab/deploy mentions it — the two repos have drifted, and the panel would get an unset knob or an unstaged credential"
        fi
    done
else
    skip_case "cross-repo name agreement with Personal (4 cases)" \
        "no sibling checkout at $PERSONAL/homelab/deploy (set PERSONAL_REPO=… to point at one). This repo's half is still asserted above."
fi

echo
echo "=== 6. OI-18: wall-sync.sh's two flows, and the knob-containment property ==="
# Exercised through the bench fixture mode (--bench-source FLOW=DIR, fixtures
# under $BENCH_ROOT) and, for the policy cases, against 127.0.0.1 and
# 192.0.2.1 (TEST-NET-1) — neither of which is a real host, so no packet reaches
# anything on the LAN.
#
# run_sync NAME MODE NEEDLE ONLY -- items... : run wall-sync.sh, judge it.
#   MODE  ok   = must exit 0      fail = must exit non-zero
#   ONLY  both = no --only flag,  otherwise passed as `--only <flow>`
#   items       `ARG:x` is a command-line ARGUMENT (the prefix is stripped);
#               anything else is a KEY=VALUE for `env`. The explicit prefix is
#               deliberate: the bench fixtures are passed as `--bench-source`
#               plus a `FLOW=DIR` value, and a value containing `=` is
#               indistinguishable from an environment assignment without it.
# Both halves are judged, exactly as expect_refusal does: an exit status with the
# wrong explanation costs the next person the same hour it was meant to save.
run_sync() {
    local name="$1" mode="$2" needle="$3" only="$4"; shift 4
    [ "$1" = "--" ] && shift
    local out="$WORK/sync.txt" rc=0 a
    local -a envs=() args=()
    [ "$only" != "both" ] && args+=(--only "$only")
    for a in "$@"; do
        case "$a" in
            ARG:*) args+=("${a#ARG:}") ;;
            *)     envs+=("$a") ;;
        esac
    done
    env "${envs[@]}" bash "$SYNC" "${args[@]}" >"$out" 2>&1 || rc=$?
    if [ "$mode" = ok ] && [ "$rc" -ne 0 ]; then
        fail_case "$name" "exited $rc; it was supposed to succeed. $(grep -m1 ERROR "$out" | cut -c1-160)"
        return
    fi
    if [ "$mode" = fail ] && [ "$rc" -eq 0 ]; then
        fail_case "$name" "exited 0; it was supposed to fail"
        return
    fi
    if grep -qF "$needle" "$out"; then pass_case "$name"
    else fail_case "$name" "right status, wrong reason. Wanted: $needle | Got: $(tail -n 3 "$out" | tr '\n' ' ' | cut -c1-200)"; fi
}

SYNC_CASES=35
if [ "$(id -u)" -ne 0 ]; then
    skip_case "wall-sync flow + knob-containment cases ($SYNC_CASES)" \
        "need root: wall-sync.sh takes its per-flow lock under /run, calls mount(8), and its bench fixtures live under $BENCH_ROOT. Re-run with sudo."
elif timeout 2 bash -c 'exec 3<>/dev/tcp/127.0.0.1/445' 2>/dev/null; then
    skip_case "wall-sync flow + knob-containment cases ($SYNC_CASES)" \
        "something IS listening on 127.0.0.1:445 here, so the reachability cases cannot be simulated on loopback."
else
    C="$WORK/cache"
    CRED="$WORK/fake.creds"
    printf 'username=test\npassword=notarealpassword\n' > "$CRED"
    chown 0:0 "$CRED"; chmod 600 "$CRED"

    # The fixtures live where the script insists they live: a root-owned
    # directory under $BENCH_ROOT. That constraint IS one of the things under
    # test — the hook used to be a wall.env knob that could name any directory.
    install -d -m 0700 "$BENCH_ROOT"
    MSRC="$BENCH_ROOT/tc-music"
    FSRC="$BENCH_ROOT/tc-frame"
    ESRC="$BENCH_ROOT/tc-empty"
    NSRC="$BENCH_ROOT/tc-manifest-only"
    rm -rf "$MSRC" "$FSRC" "$ESRC" "$NSRC"
    # The music source is shaped like the REAL one: storage-map §3 row 2 reaches
    # `NonDocs\Media\Music` THROUGH the `Media` share, so the mount root holds
    # other media too and only `Music/` may be copied. `decoy.mp3` at the root is
    # the Movies-sized tree this must not drag onto a 256 GB disk.
    install -d -m 0755 "$MSRC/Music/Album" "$FSRC" "$ESRC/Music" "$NSRC/Music"
    : > "$MSRC/decoy.mp3"
    : > "$MSRC/Music/Album/01 Track.mp3"
    # The frame source is shaped like ITS real one: PictureFrameVideos is a
    # DEDICATED share (§3b), so the content is at the share root, no subdir.
    : > "$FSRC/clip.mp4"
    # A source holding ONLY the file rsync then excludes. It is not empty to
    # `find`, and it used to pass the emptiness guard — after which rsync copied
    # nothing and --delete emptied the cache.
    : > "$NSRC/Music/index.json"

    # The needle is the whole SUMMARY line WITH ITS COUNTS, not just "sync
    # complete": a run that silently mirrored only one flow would still say
    # "complete", and one that mirrored the wrong subtree would still say "both".
    run_sync "a full run mirrors both flows" ok "sync complete — music(1 files); frame(1 files)" both -- \
        "WALL_MEDIA_CACHE=$C" "ARG:--bench-source" "ARG:music=$MSRC" "ARG:--bench-source" "ARG:frame=$FSRC"

    if [ -f "$C/music/Album/01 Track.mp3" ] && [ ! -e "$C/music/decoy.mp3" ]; then
        pass_case "music is taken from the Music/ subdir UNDER the mount, not from the share root"
    else
        fail_case "music is taken from the Music/ subdir UNDER the mount, not from the share root" \
            "cache holds: $(find "$C/music" -type f -printf '%P ' 2>/dev/null)"
    fi

    if [ -f "$C/frame/clip.mp4" ] && grep -q 'clip.mp4' "$C/frame/playlist.json" 2>/dev/null; then
        pass_case "frame content is taken from the share ROOT (no subdir) and reaches the playlist"
    else
        fail_case "frame content is taken from the share ROOT (no subdir) and reaches the playlist" \
            "cache holds: $(find "$C/frame" -type f -printf '%P ' 2>/dev/null)"
    fi

    # `--only frame` must not rewrite the music manifest: the frame flow runs
    # every minute and the music flow's first mirror can take an hour, so the two
    # DO overlap, and a frame run regenerating music/index.json from a half-filled
    # music cache is exactly the partial manifest the "written LAST" rule forbids.
    #
    # THE SOURCE IS MUTATED FIRST. This case used to re-run an unchanged fixture,
    # so a mirror that did nothing at all passed it.
    printf 'SENTINEL-NOT-A-MANIFEST\n' > "$C/music/index.json"
    : > "$FSRC/second-clip.mp4"
    run_sync "--only frame actually re-mirrors NEW frame content" ok "frame: manifest" frame -- \
        "WALL_MEDIA_CACHE=$C" "ARG:--bench-source" "ARG:frame=$FSRC"
    if [ -f "$C/frame/second-clip.mp4" ] && grep -q 'second-clip.mp4' "$C/frame/playlist.json" 2>/dev/null; then
        pass_case "the new frame file reached both the cache and the playlist"
    else
        fail_case "the new frame file reached both the cache and the playlist" \
            "cache holds: $(find "$C/frame" -type f -printf '%P ' 2>/dev/null)"
    fi
    if grep -q 'SENTINEL-NOT-A-MANIFEST' "$C/music/index.json"; then
        pass_case "--only frame leaves music/index.json alone"
    else
        fail_case "--only frame leaves music/index.json alone" "the frame run rewrote the music manifest"
    fi

    # THE FAILURE-POLICY FORK. storage-map §4d says Mini-serv may sleep and the
    # AWOW may not — but the probe now says THREE things, not two, and only one
    # of them is "asleep".
    run_sync "a frame source that answers NOTHING is the designed silent skip (exit 0)" ok \
        "The source is ASLEEP or off. Skipping, and NOT waking it" frame -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_FRAME_SHARE_UNC=//192.0.2.1/PictureFrameVideos" \
        "MEDIA_FRAME_CIFS_CREDENTIALS=$CRED"
    # 127.0.0.1 sends an RST: a box that answers is AWAKE, and reporting that as
    # "asleep" is what let a wrong credential hide behind a failing probe forever.
    run_sync "a frame source that REFUSES the connection is INDETERMINATE, not asleep" ok \
        "is INDETERMINATE" frame -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_FRAME_SHARE_UNC=//127.0.0.1/PictureFrameVideos" \
        "MEDIA_FRAME_CIFS_CREDENTIALS=$CRED"
    run_sync "…and it still exits 0 without ever claiming the box is asleep" ok \
        "WITHOUT claiming the source is asleep" frame -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_FRAME_SHARE_UNC=//127.0.0.1/PictureFrameVideos" \
        "MEDIA_FRAME_CIFS_CREDENTIALS=$CRED"
    # NO CREDENTIAL KNOB FOR MUSIC (Q-S7): the mount is anonymous, so this case
    # also proves the flow gets as far as mounting without one.
    run_sync "an unreachable MUSIC source is an ALERT: the unit FAILS" fail \
        "cifs mount REFUSED" music -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_MUSIC_SHARE_UNC=//127.0.0.1/Media"
    run_sync "…and says the mount is ANONYMOUS, so nobody goes hunting for a password" fail \
        "This mount is ANONYMOUS" music -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_MUSIC_SHARE_UNC=//127.0.0.1/Media"

    # THE RETIRED KEY IS REFUSED BY NAME. A wall.env carried over from before
    # 2026-08-05 still sets it, and ignoring it silently is how the panel would
    # look configured while the operator kept a dead HOMEHUB account alive.
    run_sync "a wall.env still setting MEDIA_MUSIC_CIFS_CREDENTIALS is REFUSED, not ignored" fail \
        "NO LONGER READ" music -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_MUSIC_SHARE_UNC=//127.0.0.1/Media" \
        "MEDIA_MUSIC_CIFS_CREDENTIALS=$CRED"

    # THE CASE THE SUITE EXPLICITLY SKIPPED: 445 ANSWERS, AND THE MOUNT IS THEN
    # REFUSED. This is the ONLY path that can ever expose a wrong frame
    # credential, and it was never exercised — the whole section bailed out
    # whenever anything was listening. Listen on purpose instead, for one case.
    LISTENER="$WORK/listen.py"
    cat > "$LISTENER" <<'PYEOF'
import socket, sys, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1])))
s.listen(8)
sys.stderr.write("listening\n"); sys.stderr.flush()
time.sleep(180)
PYEOF
    LPID=""
    start_listener() {   # PORT -> 0 iff something now answers there
        local port="$1" i
        python3 "$LISTENER" "$port" 2>"$WORK/listen.err" &
        LPID=$!
        for i in 1 2 3 4 5 6 7 8 9 10; do
            timeout 1 bash -c 'exec 3<>/dev/tcp/127.0.0.1/"$1"' _ "$port" 2>/dev/null && return 0
            sleep 0.3
        done
        return 1
    }
    stop_listener() {
        [ -n "$LPID" ] && kill "$LPID" 2>/dev/null
        wait "$LPID" 2>/dev/null || true
        LPID=""
    }

    if start_listener 445; then
        run_sync "445 ANSWERS and the frame mount is then refused: that is FATAL, not a skip" fail \
            "WHICH IS AWAKE" frame -- \
            "WALL_MEDIA_CACHE=$C" "MEDIA_FRAME_SHARE_UNC=//127.0.0.1/PictureFrameVideos" \
            "MEDIA_FRAME_CIFS_CREDENTIALS=$CRED"
    else
        fail_case "445 ANSWERS and the frame mount is then refused: that is FATAL, not a skip" \
            "could not bring up the loopback listener this case needs: $(tr '\n' ' ' < "$WORK/listen.err" 2>/dev/null | cut -c1-160)"
    fi
    stop_listener

    # PER-FLOW AGGREGATION. Nothing used to exercise it: the only two-flow case
    # had both flows succeed, and every failure case used --only, so an
    # abort-on-first-error orchestration passed the suite. Music must FAIL, the
    # run must exit non-zero, AND frame must still have been mirrored.
    rm -f "$C/frame/agg.mp4"
    : > "$FSRC/agg.mp4"
    run_sync "one flow's failure does not cancel the other (the run still fails)" fail \
        "sync FINISHED WITH FAILURES" both -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_MUSIC_SHARE_UNC=//127.0.0.1/Media" \
        "ARG:--bench-source" "ARG:frame=$FSRC"
    if [ -f "$C/frame/agg.mp4" ]; then
        pass_case "…and the FRAME flow ran anyway, after the music flow failed"
    else
        fail_case "…and the FRAME flow ran anyway, after the music flow failed" \
            "the frame content added before the run never reached $C/frame"
    fi

    # The transplanted ingest guard, in the two-flow shape: an empty source must
    # not mirror-delete a populated cache.
    run_sync "an EMPTY music source still refuses to mirror-delete a populated cache" fail \
        "REFUSING to mirror-delete" music -- \
        "WALL_MEDIA_CACHE=$C" "ARG:--bench-source" "ARG:music=$ESRC"
    # …and the sharper version: a source holding ONLY the file rsync excludes.
    # `find`-based emptiness said "has data"; rsync then copied nothing and
    # --delete emptied the cache with WALL_SYNC_ALLOW_EMPTY=false.
    run_sync "a source holding ONLY the excluded manifest counts as EMPTY" fail \
        "REFUSING to mirror-delete" music -- \
        "WALL_MEDIA_CACHE=$C" "ARG:--bench-source" "ARG:music=$NSRC"
    if [ -f "$C/music/Album/01 Track.mp3" ]; then
        pass_case "…and the populated music cache survived both refusals"
    else
        fail_case "…and the populated music cache survived both refusals" "the cache was emptied anyway"
    fi

    # THE CACHE COUNT MUST MATCH RSYNC'S EXCLUSIONS EXACTLY. `! -name index.json`
    # hid a same-named file at EVERY depth, so a cache holding only
    # `Album/index.json` — a real mirrored file — counted as ZERO and the
    # "refusing to mirror-delete a populated cache" guard never fired.
    C9="$WORK/cache9"
    install -d -m 0755 "$C9/music/Album"
    : > "$C9/music/Album/index.json"
    run_sync "a cache whose only file is Album/index.json still counts as POPULATED" fail \
        "REFUSING to mirror-delete" music -- \
        "WALL_MEDIA_CACHE=$C9" "ARG:--bench-source" "ARG:music=$ESRC"

    # THE STALENESS LADDER MUST NOT BE THE THING THAT CRASHES. It is the one path
    # whose whole job is to stay calm, and "all digits" was not a safe test for
    # bash arithmetic: `08` is all digits and is an invalid OCTAL literal.
    printf '08\n' > "$C/.wall-sync-frame.stamp"
    run_sync "an octal-looking last-sync stamp still produces a staleness line" ok \
        "content on the wall is" frame -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_FRAME_SHARE_UNC=//192.0.2.1/PictureFrameVideos" \
        "MEDIA_FRAME_CIFS_CREDENTIALS=$CRED"
    # A stamp in the FUTURE used to be clamped to zero, i.e. reported as "0h old"
    # for as long as the clock stayed behind it: a cache that is never stale.
    printf '%s\n' "$(( $(date +%s) + 86400 ))" > "$C/.wall-sync-frame.stamp"
    run_sync "a FUTURE last-sync stamp is reported as UNKNOWN, not as fresh" ok \
        "in the FUTURE" frame -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_FRAME_SHARE_UNC=//192.0.2.1/PictureFrameVideos" \
        "MEDIA_FRAME_CIFS_CREDENTIALS=$CRED"
    rm -f "$C/.wall-sync-frame.stamp"

    # ── THE KNOB-CONTAINMENT PROPERTY ────────────────────────────────────────
    # "Only the UNCs, the credentials files and the SMB version are knobs" is the
    # thesis of this design, and it used to be only a comment: load_env_file
    # exported EVERY valid identifier, over names the script had already set.
    # THE ALLOWLIST ITSELF, DRIVEN THROUGH A REAL wall.env. The script reads a
    # FIXED path (/etc/wall-panel/wall.env — that it is not a knob is part of the
    # property), so the only honest way to test it is to give the process its own
    # mount namespace with a fixture bind-mounted there. `unshare -m` costs
    # nothing and touches nothing outside the child. If /etc/wall-panel already
    # exists this is a REAL PANEL and the cases skip rather than mount over an
    # operator's configuration.
    FAKE_ETC="$WORK/fake-etc"; mkdir -p "$FAKE_ETC"
    run_sync_envfile() {   # NAME MODE NEEDLE ONLY ENVFILE_BODY -- items...
        local name="$1" mode="$2" needle="$3" only="$4" body="$5"; shift 5
        printf '%s\n' "$body" > "$FAKE_ETC/wall.env"
        chmod 600 "$FAKE_ETC/wall.env"
        local out="$WORK/sync.txt" rc=0 a
        local -a envs=() args=()
        [ "$only" != "both" ] && args+=(--only "$only")
        [ "${1:-}" = "--" ] && shift
        for a in "$@"; do
            case "$a" in
                ARG:*) args+=("${a#ARG:}") ;;
                *)     envs+=("$a") ;;
            esac
        done
        unshare -m env "${envs[@]}" bash -c '
            mount --bind "$1" /etc/wall-panel || exit 70
            shift
            exec bash "$@"' _ "$FAKE_ETC" "$SYNC" "${args[@]}" >"$out" 2>&1 || rc=$?
        if [ "$rc" = 70 ]; then
            fail_case "$name" "could not bind-mount the fixture over /etc/wall-panel"
            return
        fi
        if [ "$mode" = ok ] && [ "$rc" -ne 0 ]; then
            fail_case "$name" "exited $rc; it was supposed to succeed. $(grep -m1 ERROR "$out" | cut -c1-160)"
            return
        fi
        if [ "$mode" = fail ] && [ "$rc" -eq 0 ]; then
            fail_case "$name" "exited 0; it was supposed to fail"
            return
        fi
        if grep -qF "$needle" "$out"; then pass_case "$name"
        else fail_case "$name" "right status, wrong reason. Wanted: $needle | Got: $(tail -n 3 "$out" | tr '\n' ' ' | cut -c1-200)"; fi
    }

    if [ -d /etc/wall-panel ] || ! command -v unshare >/dev/null 2>&1; then
        skip_case "wall.env allowlist cases (4)" \
            "/etc/wall-panel already exists (this looks like a real panel) or unshare(1) is absent — refusing to bind-mount over a live configuration."
    else
        mkdir -p /etc/wall-panel
        # FLOWS=music in wall.env used to drop the frame flow out of every
        # boot/resume run: one typo, and the wall goes blank with a green unit.
        run_sync_envfile "wall.env cannot redefine the flow set (FLOWS is not a knob)" ok \
            "sync complete — music(1 files); frame(" both \
            "FLOWS=music" -- \
            "WALL_MEDIA_CACHE=$C" "ARG:--bench-source" "ARG:music=$MSRC" "ARG:--bench-source" "ARG:frame=$FSRC"
        # PROBE_PORT decides which port "is the source awake?" is asked on, so a
        # wall.env key for it could point the question at a port that always
        # answers (making a dead share look awake) or one that never does
        # (switching the every-minute flow off forever, silently). The fixture
        # names a port that DOES answer: if the key were honoured the probe would
        # succeed and the run would go on to a FATAL mount refusal, so "still
        # INDETERMINATE, still exit 0" is only possible if it was ignored.
        if start_listener 9999; then
            run_sync_envfile "wall.env cannot move the reachability probe's port" ok \
                "is INDETERMINATE" frame \
                "PROBE_PORT=9999
MEDIA_FRAME_SHARE_UNC=//127.0.0.1/PictureFrameVideos
MEDIA_FRAME_CIFS_CREDENTIALS=$CRED" -- \
                "WALL_MEDIA_CACHE=$C"
        else
            fail_case "wall.env cannot move the reachability probe's port" \
                "could not bring up the loopback listener on 9999 this case needs"
        fi
        stop_listener
        # RUNDIR=/tmp/… used to move the lock (and the mountpoint) somewhere any
        # user can pre-create.
        run_sync_envfile "wall.env cannot move RUNDIR, the lock/mountpoint root" ok \
            "sync complete — frame(" frame \
            "RUNDIR=$WORK/hijacked-rundir" -- \
            "WALL_MEDIA_CACHE=$C" "ARG:--bench-source" "ARG:frame=$FSRC"
        if [ -e "$WORK/hijacked-rundir" ]; then
            fail_case "…and RUNDIR really did not move" "the run created $WORK/hijacked-rundir"
        else
            pass_case "…and RUNDIR really did not move"
        fi
        rmdir /etc/wall-panel 2>/dev/null || true
    fi

    run_sync "the retired MEDIA_CIFS_EXTRA is REFUSED, not silently ignored" fail \
        "NO LONGER READ" both -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_CIFS_EXTRA=prefixpath=Movies,rw" \
        "ARG:--bench-source" "ARG:music=$MSRC" "ARG:--bench-source" "ARG:frame=$FSRC"
    run_sync "the retired bench-hook KNOB is REFUSED, not silently ignored" fail \
        "NO LONGER READ" both -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_FRAME_SOURCE_OVERRIDE=/etc" \
        "ARG:--bench-source" "ARG:frame=$FSRC"
    run_sync "MEDIA_CIFS_VERS is an ENUM, not a free-text option string" fail \
        "not one of the supported SMB dialects" both -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_CIFS_VERS=3.0,prefixpath=Movies" \
        "ARG:--bench-source" "ARG:music=$MSRC" "ARG:--bench-source" "ARG:frame=$FSRC"
    run_sync "a bench fixture OUTSIDE the fixture root is refused" fail \
        "is not under $BENCH_ROOT" frame -- \
        "WALL_MEDIA_CACHE=$C" "ARG:--bench-source" "ARG:frame=/etc"
    run_sync "the bench mode is refused when systemd is the caller" fail \
        "BENCH-ONLY mode" frame -- \
        "WALL_MEDIA_CACHE=$C" "INVOCATION_ID=deadbeef" "ARG:--bench-source" "ARG:frame=$FSRC"
    # The frame flow consumes the SHARE ROOT, so a typo'd UNC is a way to widen
    # the mirror: //MINI-SERV/NetworkShare passed the old //?*/?* check.
    run_sync "a frame UNC with a PATH TAIL is refused (the subtree is code)" fail \
        "names a PATH INSIDE a share" frame -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_FRAME_SHARE_UNC=//127.0.0.1/Media/PictureFrameVideos" \
        "MEDIA_FRAME_CIFS_CREDENTIALS=$CRED"
    # The two flows now differ in AUTH MODE (Q-S7): music mounts anonymously,
    # frame presents the Mini-serv credential. One host for both would mean one
    # of the two is using the wrong mode for the box it is talking to.
    run_sync "two UNCs on the SAME host are refused (the flows authenticate differently)" fail \
        "both name the host" both -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_MUSIC_SHARE_UNC=//127.0.0.1/Media" \
        "MEDIA_FRAME_SHARE_UNC=//127.0.0.1/PictureFrameVideos" \
        "MEDIA_FRAME_CIFS_CREDENTIALS=$CRED"
    # "root-only 0600" was claimed by the error message and never checked. Runs
    # on FRAME since Q-S7 — it is the only flow with a credentials file left,
    # and the check still happens before the reachability probe, so the frame
    # flow's licence to skip a sleeping box does not swallow the refusal.
    LOOSE="$WORK/loose.creds"
    printf 'username=test\npassword=notarealpassword\n' > "$LOOSE"; chmod 644 "$LOOSE"
    run_sync "a 0644 credentials file is refused (the claim and the check now agree)" fail \
        "must be mode 0600 owned root:root" frame -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_FRAME_SHARE_UNC=//127.0.0.1/PictureFrameVideos" \
        "MEDIA_FRAME_CIFS_CREDENTIALS=$LOOSE"
    # rsync FOLLOWS a symlinked destination, so a symlinked cache leaf redirects
    # both the copy AND the --delete.
    VICTIM="$WORK/victim"; mkdir -p "$VICTIM"; : > "$VICTIM/precious"
    SYMC="$WORK/symcache"; mkdir -p "$SYMC"; ln -sfn "$VICTIM" "$SYMC/music"
    run_sync "a SYMLINKED cache leaf is refused (rsync --delete would follow it)" fail \
        "is a SYMLINK" music -- \
        "WALL_MEDIA_CACHE=$SYMC" "ARG:--bench-source" "ARG:music=$MSRC"
    if [ -f "$VICTIM/precious" ]; then
        pass_case "…and the directory the symlink pointed at was untouched"
    else
        fail_case "…and the directory the symlink pointed at was untouched" "the mirror deleted through the symlink"
    fi

    # THE MANIFEST WRITER MUST NOT FOLLOW A SYMLINK. A crafted source containing
    # `playlist.json.tmp -> <target>` used to make this root process truncate,
    # rewrite and chmod 0644 that target.
    TRAP="$WORK/shadow-decoy"; printf 'DO-NOT-CLOBBER\n' > "$TRAP"
    ln -sfn "$TRAP" "$FSRC/playlist.json.tmp"
    run_sync "a source symlink named like the manifest temp file is not followed" ok \
        "frame: manifest" frame -- \
        "WALL_MEDIA_CACHE=$C" "ARG:--bench-source" "ARG:frame=$FSRC"
    if [ "$(cat "$TRAP")" = "DO-NOT-CLOBBER" ]; then
        pass_case "…and the file it pointed at still holds its own content"
    else
        fail_case "…and the file it pointed at still holds its own content" \
            "the manifest writer wrote through the symlink: $(head -c 80 "$TRAP")"
    fi
    rm -f "$FSRC/playlist.json.tmp"
fi

echo
echo "=== 7. the --clean brake ==="
NOTOURS="$WORK/not-a-build-dir"; mkdir -p "$NOTOURS/precious"
if CLEAN=1 OUT_DIR="$NOTOURS" bash "$BUILDER" >"$WORK/out.txt" 2>&1; then
    printf 'FAIL  --clean wiped a directory it did not create\n'; fail=$((fail + 1))
elif [ -d "$NOTOURS/precious" ] && grep -q "does not look like a vmtest build directory" "$WORK/out.txt"; then
    printf 'ok    --clean refuses a directory this builder did not create\n'; pass=$((pass + 1))
else
    printf 'FAIL  --clean brake: %s\n' "$(grep -m1 FATAL "$WORK/out.txt" | cut -c1-160)"; fail=$((fail + 1))
fi

echo
printf '%s\n' "----------------------------------------"
printf 'wall builder guards: %d passed, %d failed, %d skipped\n' "$pass" "$fail" "$skip"
[ "$skip" -eq 0 ] || printf 'NOTE: skipped cases are NOT passes — see the SKIP lines above.\n'
[ "$fail" -eq 0 ] || exit 1
