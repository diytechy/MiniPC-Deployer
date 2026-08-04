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
# THREE SECTIONS:
#   1-3. the builder's guards (the original suite)
#   4.   the OI-18 production seam — two credential files, staged and reported
#        individually, and the retired single one refused
#   5.   wall-sync.sh's TWO FLOWS, exercised through the bench hooks: the frame
#        share's content is at its share ROOT while music sits under a subdir,
#        and an unreachable frame source is a SKIP while an unreachable music
#        source is an ALERT. Section 5 needs root (it takes a lock under /run and
#        calls mount); without it the cases SKIP loudly rather than vanishing.
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
BUILD="bash $SCRIPT_DIR/build-wall-seed.sh"
WORK="${TMPDIR:-/tmp}/wall-builder-test.$$"
DIST_REAL="${WALL_SHELL_DIST:-$(cd "$REPO_ROOT/.." && pwd)/OfficeWallNaglight/dist}"

pass=0; fail=0; skip=0
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

# expect_refusal NAME EXPECTED_SUBSTRING -- env... -- : the build must exit
# non-zero AND say why. Both halves matter: a guard that fires with the wrong
# message costs the next person the same hour it was supposed to save.
expect_refusal() {
    local name="$1" needle="$2"; shift 2
    [ "$1" = "--" ] && shift
    local out="$WORK/out.txt"
    if env "$@" OUT_DIR="$WORK/out-dir" bash "$SCRIPT_DIR/build-wall-seed.sh" >"$out" 2>&1; then
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
    if env "$@" OUT_DIR="$WORK/out-dir" bash "$SCRIPT_DIR/build-wall-seed.sh" >"$out" 2>&1 \
       && grep -qF "$needle" "$out"; then
        printf 'ok    %s\n' "$name"
        pass=$((pass + 1))
    else
        printf 'FAIL  %s\n      %s\n' "$name" "$(tail -n 3 "$out" | tr '\n' ' ' | cut -c1-200)"
        fail=$((fail + 1))
    fi
}

skip_case() { printf 'SKIP  %s\n      %s\n' "$1" "$2"; skip=$((skip + 1)); }
pass_case() { printf 'ok    %s\n' "$1"; pass=$((pass + 1)); }
fail_case() { printf 'FAIL  %s\n      %s\n' "$1" "$2"; fail=$((fail + 1)); }

echo "=== the artifact gate ==="
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
    echo "=== the runtime-dependency gate ==="
    T="$REPO_ROOT/stack/autoinstall/wall/electron-runtime-deps.tsv"
    U="$REPO_ROOT/stack/autoinstall/wall/user-data"
    cp "$T" "$WORK/tsv.bak"; cp "$U" "$WORK/ud.bak"

    grep -v '^libnspr4.so' "$WORK/tsv.bak" > "$T"
    expect_refusal "a soname the table has never heard of is refused" \
        "this repo has never heard of" --
    cp "$WORK/tsv.bak" "$T"

    grep -v '^    - libnspr4$' "$WORK/ud.bak" > "$U"
    expect_refusal "a mapped package the image does not install is refused" \
        "does not install package(s)" --
    cp "$WORK/ud.bak" "$U"
else
    skip_case "-dirty / ambiguous / dependency-gate cases" \
        "no shell artifact at $DIST_REAL — build one with 'npm run dist' in OfficeWallNaglight"
fi

echo
echo "=== the production seam ==="
S="$WORK/site"; mkdir -p "$S"
expect_refusal "WALL_SITE_DIR with no user-data.filled is refused, not half-applied" \
    "has no user-data.filled" -- "WALL_SITE_DIR=$S"

sed -e 's/REPLACE_WITH_WIFI_SSID/TestNet/' -e 's/REPLACE_WITH_WIFI_PSK/testpsk123/' \
    -e 's#- "ssh-ed25519 AAAA_REPLACE_WITH_YOUR_PUBLIC_KEY you@host"#- "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItestkey t@t"#' \
    "$REPO_ROOT/stack/autoinstall/wall/user-data" > "$S/user-data.filled"
expect_refusal "a production build with no wall.env is refused (the panel would have no origin)" \
    "no wall.env" -- "WALL_SITE_DIR=$S"

sed 's/^WALL_HOST=.*/WALL_HOST=wall.test.invalid/' \
    "$REPO_ROOT/stack/autoinstall/wall/wall.env.example" > "$S/wall.env"
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
echo "=== the OI-18 production seam: TWO credentials, on TWO hosts ==="
# The last case above rewrote `wifis:` away to prove the no-Wi-Fi refusal; put it
# back, or every case below refuses for that reason instead of the one it tests.
sed -i 's/^    ethernets:/    wifis:/' "$S/user-data.filled"

# Absent credential files are NOT a build refusal — a bench panel is a legitimate
# thing to build — but they must be reported per SOURCE, because "one of two" is a
# panel with half its media and which half decides what is dead. A single "no
# credentials" line would have been true of neither.
expect_success "a missing cifs-music.creds names the MUSIC source as the casualty" \
    "cannot mount its MUSIC share" -- "WALL_SITE_DIR=$S" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1
expect_success "a missing cifs-frame.creds names the FRAME source as the casualty" \
    "cannot mount its FRAME VIDEO share" -- "WALL_SITE_DIR=$S" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1

# The values are fictional and local to this suite: the point is the STAGING, not
# the secret. Never put a real credential in a test fixture.
printf 'username=testmusic\npassword=notarealpassword\n' > "$S/cifs-music.creds"
printf 'username=testframe\npassword=notarealpassword\n' > "$S/cifs-frame.creds"
expect_success "both credential files are staged when both are present" \
    "PRODUCTION build: 3 wall config file(s) staged" -- "WALL_SITE_DIR=$S" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1

# A stale out\wall\ from before OI-18 carries ONE file called cifs.creds. Staged
# silently it would produce a panel with no media at all, and the failure would
# read as "Personal never emitted anything" rather than "you are baking a stale
# directory". One credential cannot authenticate on two hosts, so say so.
printf 'username=stale\npassword=notarealpassword\n' > "$S/cifs.creds"
expect_refusal "a stale PRE-OI-18 single cifs.creds is refused, not silently ignored" \
    "PRE-OI-18 shape" -- "WALL_SITE_DIR=$S" "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1
rm -f "$S/cifs.creds"

echo
echo "=== cross-repo knob names: the sim renderer must fill BOTH UNCs ==="
# THE SINGLE BIGGEST RISK IN OI-18 IS A KNOB NAME DRIFTING BETWEEN THE REPOS.
# `set_env_key` already refuses to append a key wall.env.example does not declare
# (a knob the consumer never reads is a silent no-op). These two cases prove that
# guard is actually wired to BOTH of the new names — delete either `set_env_key`
# call from render_sim_wall_env and the corresponding case stops refusing.
WEX="$REPO_ROOT/stack/autoinstall/wall/wall.env.example"
cp "$WEX" "$WORK/wallenv.bak"
for knob in MEDIA_MUSIC_SHARE_UNC MEDIA_FRAME_SHARE_UNC; do
    grep -v "^${knob}=" "$WORK/wallenv.bak" > "$WEX"
    expect_refusal "a sim build refuses if wall.env.example loses $knob" \
        "'$knob' is not a key" -- "WALL_SHELL_DIST=$EMPTY" ALLOW_MISSING_SHELL=1
    cp "$WORK/wallenv.bak" "$WEX"
done

echo
echo "=== OI-18: wall-sync.sh's two flows are NOT the same flow ==="
# Exercised through the documented bench hooks (MEDIA_{MUSIC,FRAME}_SOURCE_OVERRIDE)
# and, for the two policy cases, against 127.0.0.1 — which answers nothing on 445,
# so no packet leaves the machine and no real host is involved.
SYNC="$REPO_ROOT/stack/autoinstall/wall/wall-sync.sh"

# run_sync NAME MODE NEEDLE ONLY -- env... : run wall-sync.sh and judge it.
#   MODE  ok   = must exit 0      fail = must exit non-zero
#   ONLY  both = no --only flag,  otherwise passed as `--only <flow>`
# Both halves are judged, exactly as expect_refusal does: an exit status with the
# wrong explanation costs the next person the same hour it was meant to save.
run_sync() {
    local name="$1" mode="$2" needle="$3" only="$4"; shift 4
    [ "$1" = "--" ] && shift
    local out="$WORK/sync.txt" rc=0 onlyflag=""
    [ "$only" != "both" ] && onlyflag="--only $only"
    # $onlyflag is deliberately unquoted — it is either empty or two literal
    # words, both from this file, never from input.
    # shellcheck disable=SC2086
    env "$@" bash "$SYNC" $onlyflag >"$out" 2>&1 || rc=$?
    if [ "$mode" = ok ] && [ "$rc" -ne 0 ]; then
        fail_case "$name" "exited $rc; it was supposed to succeed. $(grep -m1 ERROR "$out" | cut -c1-160)"
        return
    fi
    if [ "$mode" = fail ] && [ "$rc" -eq 0 ]; then
        fail_case "$name" "exited 0; it was supposed to fail"
        return
    fi
    if grep -qF "$needle" "$out"; then pass_case "$name"
    else fail_case "$name" "right status, wrong reason. Wanted: $needle | Got: $(tail -n 2 "$out" | tr '\n' ' ' | cut -c1-160)"; fi
}

if [ "$(id -u)" -ne 0 ]; then
    skip_case "wall-sync two-flow cases (6)" \
        "need root: wall-sync.sh takes its per-flow lock under /run and calls mount(8). Re-run with sudo."
elif timeout 2 bash -c 'exec 3<>/dev/tcp/127.0.0.1/445' 2>/dev/null; then
    skip_case "wall-sync two-flow cases (6)" \
        "something IS listening on 127.0.0.1:445 here, so 'the source is asleep' cannot be simulated on loopback."
else
    C="$WORK/cache"
    MSRC="$WORK/src-music"
    FSRC="$WORK/src-frame"
    CRED="$WORK/fake.creds"
    printf 'username=test\npassword=notarealpassword\n' > "$CRED"; chmod 600 "$CRED"
    # The music source is shaped like the REAL one: storage-map §3 row 2 reaches
    # `NonDocs\Media\Music` THROUGH the `Media` share, so the mount root holds
    # other media too and only `Music/` may be copied. `decoy.mp3` at the root is
    # the Movies-sized tree this must not drag onto a 256 GB disk.
    mkdir -p "$MSRC/Music/Album" "$FSRC"
    : > "$MSRC/decoy.mp3"
    : > "$MSRC/Music/Album/01 Track.mp3"
    # The frame source is shaped like ITS real one: PictureFrameVideos is a
    # DEDICATED share (§3b), so the content is at the share root, no subdir.
    : > "$FSRC/clip.mp4"

    # The needle is the whole SUMMARY line, not just "sync complete": a run that
    # silently mirrored only one flow would still say "complete".
    run_sync "a full run mirrors both flows" ok "sync complete — music(1 files); frame(1 files)" both -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_MUSIC_SOURCE_OVERRIDE=$MSRC" "MEDIA_FRAME_SOURCE_OVERRIDE=$FSRC"

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
    printf 'SENTINEL-NOT-A-MANIFEST\n' > "$C/music/index.json"
    run_sync "--only frame refreshes the frame flow" ok "frame: manifest" frame -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_MUSIC_SOURCE_OVERRIDE=$MSRC" "MEDIA_FRAME_SOURCE_OVERRIDE=$FSRC"
    if grep -q 'SENTINEL-NOT-A-MANIFEST' "$C/music/index.json"; then
        pass_case "--only frame leaves music/index.json alone"
    else
        fail_case "--only frame leaves music/index.json alone" "the frame run rewrote the music manifest"
    fi

    # THE FAILURE-POLICY FORK. Same unreachable address, same credentials file,
    # opposite verdicts — because storage-map §4d says Mini-serv may sleep and the
    # AWOW may not.
    run_sync "an unreachable FRAME source is a silent skip that still exits 0" ok \
        "Skipping, and NOT waking it" frame -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_FRAME_SHARE_UNC=//127.0.0.1/PictureFrameVideos" \
        "MEDIA_FRAME_CIFS_CREDENTIALS=$CRED"
    run_sync "an unreachable MUSIC source is an ALERT: the unit FAILS" fail \
        "cifs mount REFUSED" music -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_MUSIC_SHARE_UNC=//127.0.0.1/Media" \
        "MEDIA_MUSIC_CIFS_CREDENTIALS=$CRED"

    # The transplanted ingest guard, re-proved in the two-flow shape: an empty
    # source must not mirror-delete a populated cache.
    mkdir -p "$WORK/src-empty/Music"
    run_sync "an EMPTY music source still refuses to mirror-delete a populated cache" fail \
        "REFUSING to mirror-delete" music -- \
        "WALL_MEDIA_CACHE=$C" "MEDIA_MUSIC_SOURCE_OVERRIDE=$WORK/src-empty"
fi

echo
echo "=== the --clean brake ==="
NOTOURS="$WORK/not-a-build-dir"; mkdir -p "$NOTOURS/precious"
if CLEAN=1 OUT_DIR="$NOTOURS" bash "$SCRIPT_DIR/build-wall-seed.sh" >"$WORK/out.txt" 2>&1; then
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
