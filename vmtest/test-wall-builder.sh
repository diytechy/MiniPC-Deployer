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
# It is a NEGATIVE-path suite on purpose. That the builder produces an ISO is
# proven every time somebody builds one; what needs proving is that it REFUSES
# the eight inputs that would each produce an image whose failure is invisible
# on the wall.
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
