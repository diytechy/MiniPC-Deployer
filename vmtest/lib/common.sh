#!/usr/bin/env bash
# vmtest/lib/common.sh — shared helpers for the V3 gate scripts.
# Sourced by build-seed.sh, build-repacked-iso.sh and build-wall-seed.sh.
# Not standalone.
#
# TWO IMAGE TARGETS, ONE SET OF HELPERS. This repo builds the hub image
# (stack/autoinstall/) and the wall-panel image (stack/autoinstall/wall/,
# SR-017). They share the NoCloud/CIDATA mechanism, the ephemeral SSH key, the
# repo-into-payload copy and the "assert every substitution applied" discipline
# — so those live here ONCE and both renderers call them. What differs (which
# user-data, which sim hostname, which disk pin, which env file) is the whole
# content of render_seed_tree vs. render_wall_seed_tree; nothing else is forked.

log()  { echo "[vmtest] $*" >&2; }
die()  { echo "[vmtest] FATAL: $*" >&2; exit 1; }

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "required command '$1' not found. $2"
}

# validate_autoinstall_yaml FILE — parse the rendered user-data the way
# Subiquity will and refuse to bake a file it would reject.
#
# WHY THIS EXISTS: on the first real boot of the V3 gate the install died at
# "Malformed autoinstall in 'late-commands' section". The cause was one entry
# whose JSON contained ": " (colon-space) — inside a PLAIN YAML scalar that
# parses as a mapping, so the list item became a dict instead of a string.
# Every structural check we had passed, because the text was all PRESENT and
# the file was valid YAML; it was the SHAPE Subiquity wanted that was wrong.
# A whole boot cycle to learn one character of punctuation. Never again:
# parse it here, where it costs a second.
validate_autoinstall_yaml() {
    local f="$1"
    require_cmd python3 "Install it with: sudo apt-get install -y python3 python3-yaml"
    python3 - "$f" <<'PY' || die "autoinstall validation FAILED for $f (see above) — refusing to bake an ISO Subiquity would reject."
import sys, yaml

path = sys.argv[1]
try:
    doc = yaml.safe_load(open(path))
except ImportError:
    sys.exit("PyYAML missing. Install it with: sudo apt-get install -y python3-yaml")
except yaml.YAMLError as e:
    sys.exit(f"user-data is not valid YAML: {e}")

ai = doc.get("autoinstall", doc)
if not isinstance(ai, dict):
    sys.exit("no autoinstall mapping found")

rc = 0

# identity.username must not collide with a user OR GROUP the base system
# already ships. Subiquity runs a bare `useradd <name>`, which creates a
# primary group of the same name; if that group exists useradd exits 9 and the
# install dies in postinstall, AFTER partitioning and installing - i.e. you
# find out late, on real hardware. `operator` cost us exactly that: it is a
# system group (GID 37) on every Debian/Ubuntu box.
SHIPPED = set("""root daemon bin sys adm tty disk lp mail news uucp man proxy
kmem dialout fax voice cdrom floppy tape sudo audio dip www-data backup
operator list irc src shadow utmp video sasl plugdev staff games users
nogroup""".split())
# Prefer the authoritative files when this is a Debian-ish build host.
for f, idx in (("/usr/share/base-passwd/group.master", 0),
               ("/usr/share/base-passwd/passwd.master", 0)):
    try:
        with open(f) as fh:
            SHIPPED |= {ln.split(":")[idx] for ln in fh if ":" in ln}
    except OSError:
        pass

user = (ai.get("identity") or {}).get("username")
if user in SHIPPED:
    print(f"identity.username {user!r} is a user/group the base system already "
          f"ships - `useradd {user}` will exit 9 and the install will die in "
          f"postinstall. Choose a name that is not one of: "
          f"{' '.join(sorted(SHIPPED))}", file=sys.stderr)
    rc = 1
elif not user:
    print("identity.username is missing", file=sys.stderr)
    rc = 1
# Subiquity wants each command to be a string, or a list of strings (argv
# form). A dict here is the colon-space bug and kills the whole install.
for sec in ("early-commands", "late-commands", "error-commands"):
    items = ai.get(sec)
    if not items:
        continue
    for i, item in enumerate(items):
        if isinstance(item, str):
            continue
        if isinstance(item, list) and all(isinstance(x, str) for x in item):
            continue
        rc = 1
        print(f"{sec}[{i}] is a {type(item).__name__}, not a string.", file=sys.stderr)
        if isinstance(item, dict):
            for k in item:
                print(f"    YAML split it at a ': ' here -> {k!r}", file=sys.stderr)
            print("    Fix: make that entry a block scalar (- >-) or quote it.",
                  file=sys.stderr)

if ai.get("version") != 1:
    print(f"autoinstall version is {ai.get('version')!r}, expected 1", file=sys.stderr)
    rc = 1

sys.exit(rc)
PY
    log "autoinstall YAML validated: command sections are well-formed"
}

# assert_storage_pin FILE sim|production — check the disk match STRUCTURALLY.
#
# The grep guards elsewhere in this file read LINES. That was enough while the
# only question was "did the sed bite", and it is not enough for the question
# that actually matters: *what disk will this unattended install wipe?* A
# `model: Virtual_Disk` anywhere in the document — a comment's example, a
# future second storage stanza, a `network:` key that happens to be indented the
# same — satisfies a line-based positive assertion while the REAL pin says
# something else entirely. An adversarial review made exactly that point.
#
# So parse it, walk to autoinstall.storage.layout.match, and judge THAT mapping:
#   sim         -> must be exactly {model: Virtual_Disk}. Only a Hyper-V
#                  synthetic disk reports ID_MODEL=Virtual_Disk, so on real
#                  hardware this matches nothing and the install HALTS. A match
#                  that matches nothing is fail-safe; an ABSENT match is not —
#                  Subiquity then picks by heuristic, and every attached disk,
#                  including a 4 TB library drive, is a candidate.
#   production  -> must have a match, and must NOT be the sim's. A production
#                  stick pinned to Virtual_Disk halts on the real panel with
#                  nothing on screen saying why.
assert_storage_pin() {
    local f="$1" mode="$2"
    require_cmd python3 "Install it with: sudo apt-get install -y python3 python3-yaml"
    python3 - "$f" "$mode" <<'PY' || die "storage pin check FAILED for $f (see above) — refusing to bake an unattended installer whose disk selection is not what this build intends."
import sys, yaml

path, mode = sys.argv[1], sys.argv[2]
ai = yaml.safe_load(open(path))
ai = ai.get("autoinstall", ai)

layout = ((ai.get("storage") or {}).get("layout") or {})
match = layout.get("match")

if not isinstance(match, dict) or not match:
    sys.exit(
        "storage.layout has no `match:` mapping. Subiquity would pick the install\n"
        "disk by heuristic and wipe it, unattended — every attached disk is a\n"
        "candidate, including the library and backup drives.")

if mode == "sim":
    if match != {"model": "Virtual_Disk"}:
        sys.exit(
            f"the SIM storage match is {match!r}, expected exactly "
            "{'model': 'Virtual_Disk'}.\n"
            "A sim ISO must be able to select NOTHING BUT a virtual disk: it may end\n"
            "up on a USB stick, and the real panel is one boot away from it.")
elif mode == "production":
    if match.get("model") == "Virtual_Disk":
        sys.exit(
            "a PRODUCTION image is pinned to model: Virtual_Disk — the SIM pin.\n"
            "On the real panel it matches nothing and every install halts with no\n"
            "explanation. This artifact was materialised from a sim source.")
else:
    sys.exit(f"unknown mode {mode!r}")

print(f"storage pin OK ({mode}): storage.layout.match = {match}")
PY
    log "storage pin validated structurally ($mode) — not by grep"
}

# compose_escape VALUE — encode VALUE for a compose `.env`, printed on stdout.
#
# Docker Compose interpolates `$VAR` inside .env VALUES, not just in the compose
# file. A bcrypt hash is `$2a$14$<salt><digest>`: `$2a` and `$14` start with
# digits so compose leaves them, but if the salt starts with a LETTER then
# `$<salt><digest>` is a valid identifier and compose replaces the whole thing
# with an empty string. Caddy then gets `$2a$14` and rejects every login, with
# nothing but a "variable is not set" warning to explain it.
#
# Bcrypt salts are base64 `./A-Za-z0-9`, so 52 of 64 possible first characters
# are letters: about 81% of generated hashes break, and WHICH ones changes every
# time they are regenerated. The V3 gate passed once purely because both salts
# happened to start with digits.
#
# Doubling `$` is the fix. Compose collapses `$$` back to one `$`, verified
# end-to-end against `caddy hash-password` output. Single-quoting also works
# UNTIL a value contains a single quote: compose's dotenv parser rejects the
# POSIX `'\''` escape and then fails to read the whole file. `$$` has no such
# hole, so it is used for every value. Also tested and REJECTED: `env_file:`
# (interpolates too) and double quotes (interpolate too).
# NOTE: done with sed, not `${1//$/$$}`. In a bash parameter-expansion
# REPLACEMENT, `$$` expands to the shell's PID — that first draft turned
# `$2a$14$x` into `16892a1689141689x`. In a sed replacement `$` is literal.
compose_escape() {
    printf '%s' "$1" | sed 's/[$]/$$/g'
}

# assert_env_interpolation_safe FILE — refuse to ship a .env compose would eat.
# Text-only, so it needs no docker and runs anywhere: flags any value with a
# `$` that is followed by a letter or underscore and is NOT part of a `$$`.
assert_env_interpolation_safe() {
    local f="$1" bad
    bad=$(grep -nE '^[A-Za-z_][A-Za-z0-9_]*=' "$f" \
          | sed 's/\$\$//g' \
          | grep -E '\$[A-Za-z_]' || true)
    if [ -n "$bad" ]; then
        die "generated .env has value(s) docker compose will silently EAT — a '\$' followed by a letter is read as a variable reference and replaced with nothing:" \
            "$(printf '%s' "$bad" | cut -d= -f1 | tr '\n' ' ')" \
            "Escape them with compose_escape (doubles every \$)."
    fi
    log ".env interpolation-safe: no unescaped \$VAR in any value"
}

# require_writable_output FILE — fail FAST if FILE exists but cannot be
# rewritten.
#
# WHY: on Windows a RUNNING Hyper-V VM holds its attached ISO open, and WSL
# then cannot unlink it. Without this check the build stages the entire
# ~500 MB payload first and only dies four minutes later at `rm -f`, one line
# that scrolls past under xorriso output — so the run looks finished, the ISO
# on disk is silently the OLD one, and the next boot "inexplicably" reproduces
# a bug you just fixed. That happened; hence this.
#
# Opening for append is the discriminator: verified DENIED on a VM-held ISO
# and OK on a free one. It does not truncate, so an aborted build leaves the
# previous ISO intact.
require_writable_output() {
    local f="$1"
    [ -e "$f" ] || return 0
    ( exec 3>>"$f" ) 2>/dev/null && return 0
    die "cannot rewrite $f — another process holds it open." \
        "On Windows this is almost always a RUNNING Hyper-V VM with this ISO still attached." \
        "Turn it off first (elevated):  Stop-VM -Name HomeHub-VMTest -TurnOff -Force" \
        "or build somewhere else:  OUT_DIR=/mnt/d/somewhere-else bash vmtest/build-repacked-iso.sh ..."
}

# require_free_gb DIR GB — abort if the filesystem holding DIR has less than
# GB gigabytes free. Creates DIR first (mkdir -p) so a not-yet-existing output
# dir can still be statted.
require_free_gb() {
    local dir="$1" need_gb="$2" avail_kb avail_gb
    mkdir -p "$dir"
    avail_kb=$(df -Pk "$dir" | awk 'NR==2 {print $4}')
    avail_gb=$(( avail_kb / 1024 / 1024 ))
    if [ "$avail_gb" -lt "$need_gb" ]; then
        die "only ${avail_gb}GB free at '$dir' (need >= ${need_gb}GB). Point" \
            "OUT_DIR/ISO_CACHE_DIR at a roomier volume, e.g.:" \
            "  OUT_DIR=/mnt/d/vmtest-out bash vmtest/build-seed.sh"
    fi
    log "disk check OK: ${avail_gb}GB free at '$dir' (need >= ${need_gb}GB)"
}

# repo_root — resolve the MiniPC-Deployer repo root from this file's location
# (vmtest/lib/common.sh -> two dirs up), regardless of caller's cwd.
repo_root() {
    local here
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    (cd "$here/../.." && pwd)
}

# sim_banner NAME — printed at the top of every generated SIM artifact so
# nobody mistakes it for a real credential.
sim_banner() {
    cat <<EOF
# ============================================================================
# SIM / PLACEHOLDER VALUE — generated by vmtest/$1 for the local Hyper-V V3
# gate ONLY. Not a real secret, not reused anywhere, safe to discard with the
# VM. Real materialization for the physical AWOW happens later via
# SECRET_HANDOFF (WI-10.3, RATIFIED 2026-07-25; tooling not yet written).
# ============================================================================
EOF
}

# require_iso_tool — pick the ISO writer and export ISO_TOOL. genisoimage first
# (it is what the V3 gate was verified with); xorriso's genisoimage emulation is
# the accepted stand-in on hosts that only ship xorriso.
require_iso_tool() {
    if command -v genisoimage >/dev/null 2>&1; then
        ISO_TOOL="genisoimage"
    elif command -v xorriso >/dev/null 2>&1; then
        ISO_TOOL="xorriso"
    else
        die "need genisoimage or xorriso. Install with: sudo apt-get install -y genisoimage xorriso"
    fi
}

# write_seed_iso SRC_DIR OUT_ISO — burn SRC_DIR as a NoCloud seed ISO.
#
# The volume label MUST be CIDATA: that label is the entire reason the light
# path works without repacking the stock Ubuntu ISO (cloud-init's NoCloud
# datasource auto-detects any attached filesystem labelled CIDATA and reads
# user-data/meta-data from its root). Both image targets use it, which is also
# why each builder writes a DIFFERENTLY-NAMED file — attaching the wrong seed to
# a VM would install the other machine's image, and the label cannot tell them
# apart.
write_seed_iso() {
    local src_dir="$1" out_iso="$2"
    require_iso_tool
    log "building $out_iso with $ISO_TOOL (volume label CIDATA)"
    case "$ISO_TOOL" in
        genisoimage)
            genisoimage -output "$out_iso" -volid CIDATA -joliet -rock "$src_dir" >/dev/null
            ;;
        xorriso)
            xorriso -as genisoimage -output "$out_iso" -volid CIDATA -joliet -rock "$src_dir" >/dev/null
            ;;
    esac
}

# ensure_sim_ssh_key OUT_DIR — generate (or reuse) the disposable VM-test
# keypair under OUT_DIR/ssh and export SSH_KEY. Reused across re-runs so a
# rebuilt seed still matches a VM installed from the previous one; CLEAN=1 (the
# callers wipe OUT_DIR) forces a fresh pair.
ensure_sim_ssh_key() {
    local out_dir="$1" key="$1/ssh/homehub-vmtest-ed25519"
    require_cmd ssh-keygen "Install with: sudo apt-get install -y openssh-client"
    mkdir -p "$out_dir/ssh"
    chmod 700 "$out_dir/ssh"
    if [ ! -f "$key" ]; then
        log "generating ephemeral SSH keypair for the test VM (not reused anywhere else)"
        ssh-keygen -q -t ed25519 -N "" -C "homehub-vmtest (disposable, V3 gate)" -f "$key"
    else
        log "reusing existing ephemeral SSH keypair at $key (pass --clean to regenerate)"
    fi
    SSH_KEY="$key"   # exported for the caller
}

# copy_repo_into_payload REPO_ROOT PAYLOAD_DIR — materialize the deploy payload.
#
# Both images' late-commands expect the repo tree at the payload root
# (deploy-payload/stack/...).
#
# TRACKED FILES ONLY, VIA `git ls-files`. This used to be `tar --exclude=.git
# --exclude=vmtest/.out .`, i.e. the whole worktree — which quietly baked every
# GITIGNORED file into an image that is then written to a USB stick. That is not
# theoretical: a review found `stack/provision/.token` (64 bytes, documented as
# non-expiring) inside a freshly built payload, and a dev box that has ever run
# the real stack also has a real `stack/.env` sitting right there. Both would
# have shipped. Ignored means "not part of the deploy unit", and the payload is
# a deploy unit.
#
# Nothing the images need is untracked: the SIM `.env`, the materialised `site/`
# files, the container image tars and the OfficeWallNaglight tarballs are all
# staged into the payload AFTER this call, deliberately and one at a time.
#
# The whole-worktree copy stays as a fallback for a non-git export (a source
# tarball), and says loudly what it cannot promise.
copy_repo_into_payload() {
    local repo_root="$1" payload_dir="$2"
    rm -rf "$payload_dir"
    mkdir -p "$payload_dir"

    if command -v git >/dev/null 2>&1 && git -C "$repo_root" rev-parse --git-dir >/dev/null 2>&1; then
        local n
        n=$(git -C "$repo_root" ls-files | wc -l)
        [ "$n" -gt 0 ] || die "git ls-files returned nothing in $repo_root — refusing to build an empty payload."
        log "copying $n TRACKED file(s) into deploy-payload/ (gitignored files are NOT baked)"
        # -z + --null: paths with spaces are ordinary here (docs/, stack/samba/).
        ( cd "$repo_root" && git ls-files -z | tar -c --null -T - ) | ( cd "$payload_dir" && tar -x )
    else
        log "WARNING: $repo_root is not a git checkout (or git is absent) — falling back to a"
        log "  WHOLE-WORKTREE copy. Anything gitignored and present will be BAKED INTO THE"
        log "  IMAGE, including stack/.env and stack/provision/.token if they exist here."
        ( cd "$repo_root" && tar -c --exclude=.git --exclude=vmtest/.out --exclude=vmtest/.out-wall . ) \
            | ( cd "$payload_dir" && tar -x )
    fi
}

# set_env_key FILE KEY VALUE — rewrite an EXISTING KEY= line, then PROVE it.
#
# The whole point is the proof. Every `sed` in this file is a silent no-op if
# the source string moves, and the build still succeeds — which is how a sim
# image comes up claiming to be the production box (see the hostname/storage
# assertions below, each of which exists because that nearly happened). This
# helper refuses to ADD a key (a typo'd knob would otherwise append a line the
# consumer never reads) and re-reads the file afterwards to confirm the value
# landed byte-for-byte.

# wipe_build_dir DIR — what `--clean` actually does, with a brake on it.
#
# `CLEAN=1` means `rm -rf $OUT_DIR`, and OUT_DIR is an environment variable the
# README tells you to set (`OUT_DIR=/mnt/d/vmtest-out …`). One typo —
# `OUT_DIR=/mnt/d bash vmtest/build-seed.sh --clean` — and the recursive delete
# is aimed at a whole drive. Nothing about the rest of the build needs that
# power, so take it away: only ever wipe a directory THIS BUILDER made, proven
# by a marker it drops itself. Anything else is refused with the manual command,
# so the operator deletes it deliberately or not at all.
wipe_build_dir() {
    local dir="$1"
    [ -d "$dir" ] || return 0
    case "$dir" in
        ''|/|/root|/home|/tmp|/mnt|/mnt/*/|"$HOME") die "refusing to wipe '$dir'" ;;
    esac
    if [ -e "$dir/.vmtest-build-dir" ] || [ -e "$dir/iso-root" ]; then
        log "CLEAN=1: wiping previous $dir"
        rm -rf "$dir"
        return 0
    fi
    die "refusing to 'rm -rf $dir' — it does not look like a vmtest build directory" \
        "(no .vmtest-build-dir marker and no iso-root/ inside it)." \
        "OUT_DIR is an env var and --clean is a recursive delete; a typo there is a" \
        "very bad afternoon. If you really meant that path, empty it yourself first."
}

# mark_build_dir DIR — drop the marker wipe_build_dir looks for.
mark_build_dir() {
    mkdir -p "$1"
    printf '%s\n' \
        "# Written by vmtest/lib/common.sh. Its presence is what allows --clean to" \
        "# 'rm -rf' this directory. Everything here is generated; nothing is precious." \
        > "$1/.vmtest-build-dir"
}

# sed_delim TEXT — print a sed delimiter character TEXT does not contain.
#
# '|' is this file's house delimiter because so many values carry '/' and '#'.
# WALL_DISABLE_INPUT broke that assumption: its values are PIPE-separated by
# design (the device names themselves contain spaces), so a '|' delimiter would
# make sed parse the replacement as extra commands. Choose per value instead of
# assuming.
sed_delim() {
    local c
    for c in '|' '#' '%' '^' ',' '@' '!'; do
        case "$1" in *"$c"*) continue ;; esac
        printf '%s' "$c"; return 0
    done
    die "no usable sed delimiter for: $1"
}

set_env_key() {
    local f="$1" key="$2" val="$3" delim esc_val actual
    grep -qE "^${key}=" "$f" || \
        die "'$key' is not a key in $(basename "$f") — refusing to append it." \
            "A knob the consumer never reads is a silent no-op: the sim would" \
            "boot with the DEFAULT value while this build reported success." \
            "Check the spelling, or declare the knob in wall.env.example first."
    delim="$(sed_delim "$val$key")"
    # In a sed REPLACEMENT, '&' means "the whole match" and '\' escapes; both
    # would silently corrupt the value.
    esc_val="$(printf '%s' "$val" | sed -e 's/[\\&]/\\&/g')"
    sed -i -e "s${delim}^${key}=.*${delim}${key}=${esc_val}${delim}" "$f"
    actual="$(sed -n "s|^${key}=||p" "$f" | tail -n1)"
    [ "$actual" = "$val" ] || \
        die "substitution for '$key' did not apply as written: expected '$val', file now has '$actual'"
}

# render_seed_tree REPO_ROOT OUT_DIR CALLER_NAME
#
# Shared by build-seed.sh and build-repacked-iso.sh: materializes
# $OUT_DIR/iso-root/{user-data,meta-data,deploy-payload/} from the REAL
# stack/autoinstall/ + stack/.env.example with SIM values substituted, plus an
# ephemeral SSH keypair and a SIM console password under $OUT_DIR/{ssh,secrets}.
# Idempotent: reuses an existing SSH key / SIM password across calls (pass
# CLEAN=1 in the environment to force fresh ones). Never touches the stock
# Ubuntu ISO — that only happens in build-repacked-iso.sh, after this returns.
#
# TWO MODES, decided once and exported as HUB_BUILD_KIND (sim|production):
# SITE_DIR pointing at a materialised Personal\homelab\deploy\out\site makes
# this a REAL build — user-data.filled verbatim, the real site/ files staged,
# every sim substitution skipped, and the meta-data identity taken from the
# production user-data rather than stamped `homehub-vmtest`.
render_seed_tree() {
    local repo_root="$1" out_dir="$2" caller="$3"
    local autoinstall_src="$repo_root/stack/autoinstall"
    local stack_env_example="$repo_root/stack/.env.example"

    [ -f "$autoinstall_src/user-data" ] || die "not found: $autoinstall_src/user-data (run from a MiniPC-Deployer checkout)"
    [ -f "$stack_env_example" ] || die "not found: $stack_env_example"
    require_cmd openssl "Install with: sudo apt-get install -y openssl"
    require_cmd ssh-keygen "Install with: sudo apt-get install -y openssh-client"

    # WHICH KIND OF IMAGE IS THIS? Decided ONCE, here, and read by everything
    # below — the user-data render, the meta-data identity and the site/ staging.
    # It used to be re-derived at each of those points from `[ -n "$SITE_DIR" ]
    # && [ -f "$SITE_DIR/user-data.filled" ]`, which is how the meta-data step
    # came to apply the SIM hostname to a PRODUCTION build (OI-19's companion,
    # fixed 2026-08-03): a condition spelled out three times is a condition that
    # will eventually disagree with itself. Same shape as render_wall_seed_tree's
    # WALL_BUILD_KIND.
    #
    # SITE_DIR set but no user-data.filled is a SILENT DOWNGRADE and is refused
    # HERE, before anything is staged: the site/ files (real secrets) would
    # otherwise be baked onto a SIM-substituted user-data, producing a
    # "production" stick with allow-pw: true and a known sim password hash.
    HUB_BUILD_KIND=sim   # exported for the caller's summary
    if [ -n "${SITE_DIR:-}" ]; then
        [ -d "$SITE_DIR" ] || die "SITE_DIR=$SITE_DIR is not a directory"
        [ -f "$SITE_DIR/user-data.filled" ] || \
            die "SITE_DIR=$SITE_DIR is set but has no user-data.filled — refusing to stage real secrets onto a SIM-substituted user-data (allow-pw would be true). Run Materialize-Deploy.ps1 -Image homehub."
        HUB_BUILD_KIND=production
    fi

    [ "${CLEAN:-0}" -eq 1 ] && wipe_build_dir "$out_dir"
    mark_build_dir "$out_dir"

    mkdir -p "$out_dir/ssh" "$out_dir/iso-root/deploy-payload" "$out_dir/secrets"
    chmod 700 "$out_dir/ssh" "$out_dir/secrets"

    # ── ephemeral SSH keypair (disposable, VM-test only) ─────────────────────
    ensure_sim_ssh_key "$out_dir"
    local ssh_pubkey; ssh_pubkey="$(cat "$SSH_KEY.pub")"

    # ── SIM secrets: console password + Technitium admin password + oauth2-proxy
    #    cookie secret, all generated ONCE and reused across re-runs (pass
    #    CLEAN=1 to force fresh ones) so build-seed.sh / build-repacked-iso.sh
    #    can be re-run idempotently without silently drifting secrets between
    #    the seed and a previously-built ISO. ─────────────────────────────────
    local creds_file="$out_dir/secrets/creds.env"
    CREDS_FILE="$creds_file"   # exported for the caller
    local sim_password sim_password_hash cookie_secret technitium_pw
    if [ ! -f "$creds_file" ]; then
        sim_password="vmtest-$(openssl rand -hex 6)"
        sim_password_hash="$(openssl passwd -6 "$sim_password")"
        cookie_secret="$(openssl rand -base64 32 | tr -- '+/' '-_')"
        technitium_pw="$(openssl rand -base64 18)"
        {
            sim_banner "$caller"
            echo "SIM_CONSOLE_PASSWORD=$sim_password"
            echo "SIM_CONSOLE_PASSWORD_HASH=$sim_password_hash"
            echo "TECHNITIUM_ADMIN_PASSWORD=$technitium_pw"
            echo "OAUTH2_PROXY_COOKIE_SECRET=$cookie_secret"
        } > "$creds_file"
        chmod 600 "$creds_file"
    else
        log "reusing existing SIM secrets at $creds_file (pass --clean to regenerate)"
        # Extract by grep, NOT by sourcing: the password hash contains
        # '$N$...' crypt syntax which bash would try to expand as positional
        # parameters if this file were sourced under `set -u`.
        sim_password="$(grep '^SIM_CONSOLE_PASSWORD=' "$creds_file" | cut -d= -f2-)"
        sim_password_hash="$(grep '^SIM_CONSOLE_PASSWORD_HASH=' "$creds_file" | cut -d= -f2-)"
        technitium_pw="$(grep '^TECHNITIUM_ADMIN_PASSWORD=' "$creds_file" | cut -d= -f2-)"
        cookie_secret="$(grep '^OAUTH2_PROXY_COOKIE_SECRET=' "$creds_file" | cut -d= -f2-)"
    fi

    # ── render user-data: SSH placeholder + password + hostname ──────────────
    # hub user: SSH key-only in production (WI-10.12); for this disposable
    # VM we ALSO allow password login at the console for convenience — never
    # do this on the real AWOW (stack/README.md's runbook keeps allow-pw:false).
    # PRODUCTION SEAM (A14, 2026-07-30): when SITE_DIR points at a materialized
    # Personal\homelab\deploy\out\awow directory, this is a REAL build — take
    # its user-data.filled verbatim and skip every sim substitution below. That
    # file already has the operator's real SSH key and login hash patched in by
    # Materialize-Deploy.ps1, and it keeps `allow-pw: false` (key-only), which
    # is precisely what the sim path overrides. Mixing the two would be the
    # dangerous outcome: a production stick that quietly accepts a password
    # login with a known sim hash.
    local user_data_out="$out_dir/iso-root/user-data"
    if [ "$HUB_BUILD_KIND" = "production" ]; then
        log "PRODUCTION: using $SITE_DIR/user-data.filled (real key + hash; no sim substitution)"
        cp "$SITE_DIR/user-data.filled" "$user_data_out"
        # Both guards are ANCHORED to an active YAML setting, not a substring.
        # The stock user-data carries instructional COMMENTS mentioning both
        # "allow-pw: true" and "REPLACE with your real public key(s)", so a
        # naive grep refuses a perfectly good production build (caught
        # 2026-07-30 the first time a real user-data.filled was produced).
        grep -Eq '^[[:space:]]*[A-Za-z_-]+:.*REPLACE_WITH' "$user_data_out" && \
            die "user-data.filled has an active setting still holding a REPLACE_WITH placeholder — re-run Materialize-Deploy.ps1 and fix what it names."
        grep -Eq '^[[:space:]]*allow-pw:[[:space:]]*true' "$user_data_out" && \
            die "user-data.filled sets allow-pw: true — production is SSH-key-only (WI-10.12). Refusing to bake it."
        # THE MOST DESTRUCTIVE LINE IN THE PROJECT WAS THE ONLY UNGUARDED ONE.
        # `storage: layout:` wipes whatever disk Subiquity selects, unattended.
        # Without a `match:` it picks by heuristic and EVERY attached disk is a
        # candidate — including the 4 TB library and 8 TB backup drives, both an
        # order of magnitude larger than the 128 GB internal NVMe. A `match:`
        # that matches nothing HALTS the install (fail-safe); an absent one does
        # not. Caught by review 2026-07-30 against a real staged payload that
        # predated the pinning fix by 46 minutes.
        grep -Eq '^[[:space:]]+(path|serial|model|wwn):' "$user_data_out" || \
            die "user-data.filled has no storage.layout match: — the unattended wipe would pick a disk by heuristic and the library/backup drives are candidates. Re-run Materialize-Deploy.ps1 (its out\\ tree is stale)."
        # OI-19: late-command 4b refuses to install a PRODUCTION box whose
        # payload lost its site/ directory, and it decides that from the
        # BUILD_PROFILE=production marker carried in this very file. Materialize
        # -Deploy.ps1 renders user-data.filled FROM the tracked
        # stack/autoinstall/user-data (FieldSchema.psd1 Images.homehub), so the
        # marker arrives for free — unless the out\ tree predates the fix, in
        # which case this stick would carry the OLD silent-exit-0 late-command
        # and nothing downstream would ever say so.
        grep -q 'BUILD_PROFILE=production' "$user_data_out" || \
            die "user-data.filled carries no 'BUILD_PROFILE=production' marker — it was materialised from a stack/autoinstall/user-data older than the OI-19 fix, so its site-staging late-command still exits 0 SILENTLY when the payload has no site/. A production hub built from it can come up on .env.example placeholders believing it is production. Re-run Materialize-Deploy.ps1 -Image homehub (its out\\ tree is stale)."
    else
    sed \
        -e "s#- \"ssh-ed25519 AAAA_REPLACE_WITH_YOUR_PUBLIC_KEY you@host\"#- \"$ssh_pubkey\"#" \
        -e 's/allow-pw: false/allow-pw: true   # VMTEST ONLY - production keeps this false (key-only)/' \
        -e "s|password: \"!\"|password: \"$sim_password_hash\"   # VMTEST ONLY sim password, see vmtest/.out/secrets/creds.env|" \
        -e 's/hostname: homehub/hostname: homehub-vmtest/' \
        -e 's/realname: "Home Hub Operator"/realname: "Home Hub VM Test"/' \
        -e 's|^\([[:space:]]*\)path: /dev/nvme0n1|\1model: Virtual_Disk|' \
        -e 's|BUILD_PROFILE=production|BUILD_PROFILE=sim|' \
        "$autoinstall_src/user-data" > "$user_data_out"
    grep -q "REPLACE_WITH_YOUR_PUBLIC_KEY" "$user_data_out" && die "SSH placeholder substitution failed"

    # OI-19: the site-staging late-command REFUSES the install when a production
    # image arrives with no site/ payload. `production` is the tracked default
    # (safe by default — a real stick materialised from this file inherits it),
    # so the SIM path is the one that must opt out, and a no-op here would make
    # every vmtest image halt at late-command 4b for want of secrets it is not
    # supposed to have. Assert BOTH directions: the marker says sim, and no
    # `production` marker survived anywhere in the file.
    grep -q 'BUILD_PROFILE=sim' "$user_data_out" || \
        die "the BUILD_PROFILE substitution did not apply — stack/autoinstall/user-data no longer contains 'BUILD_PROFILE=production'. A SIM image would then REFUSE to install (late-command 4b halts a production build that has no site/ payload, and this build has none). Update the sed above."
    grep -q 'BUILD_PROFILE=production' "$user_data_out" && \
        die "a 'BUILD_PROFILE=production' marker survived into the SIM user-data — there is now more than one, and the sed above only rewrote the first. Every occurrence must be rewritten or a sim install halts at late-command 4b."

    # Every sed above is a SILENT no-op if the source string moves, and the
    # result still builds — that is how the sim VM would quietly come up
    # claiming to be the production box. Assert the two that identify it.
    grep -Eq '^[[:space:]]*hostname: homehub-vmtest$' "$user_data_out" || \
        die "hostname substitution did not apply — stack/autoinstall/user-data no longer says 'hostname: homehub'. The sim ISO would boot claiming the PRODUCTION hostname. Update the sed above."
    grep -Eq '^[[:space:]]*realname: "Home Hub VM Test"$' "$user_data_out" || \
        die "realname substitution did not apply — stack/autoinstall/user-data no longer says 'realname: \"Home Hub Operator\"'. Update the sed above."

    # ── VMTEST storage pin: CONTAINMENT, not convenience ─────────────────────
    # Production pins `path: /dev/nvme0n1` (the AK41's internal NVMe). Hyper-V
    # Gen2 has no NVMe controller — the VHDX hangs off the synthetic SCSI
    # controller and enumerates as /dev/sda — so the production pin matches
    # NOTHING in a VM and the install halts. That halt is the fail-safe working,
    # but it also means the V3 gate can never complete.
    #
    # The naive fix (`path: /dev/sda`) is the WRONG one and must never be made:
    # /dev/sda is a REAL disk on real hardware, so a sim ISO that ever met a
    # physical machine — or a USB stick someone wrote it to — would wipe it
    # unattended. The whole point of the pin is that it cannot do that.
    #
    # So pin to something ONLY a virtual disk can satisfy. Hyper-V synthetic
    # disks report udev ID_MODEL=Virtual_Disk / ID_VENDOR=Msft (verified on this
    # dev box: WSL2 is itself a Hyper-V guest and its disks report exactly
    # that). Subiquity matches `model:` against udev's ID_MODEL via probert's
    # StorageInfo, which reads ID_MODEL — NOT ID_MODEL_ENC — so the value is the
    # UNDERSCORE form `Virtual_Disk`; `lsblk`'s prettified "Virtual Disk" would
    # match nothing. On real hardware the disk reports its true model
    # (FORESEE P900F128GBH, etc.), so this pin matches nothing and the install
    # fail-safe halts. Containment holds even if the sim ISO escapes the VM.
    grep -Eq '^[[:space:]]+model: Virtual_Disk$' "$user_data_out" || \
        die "vmtest storage pin was NOT applied — stack/autoinstall/user-data's storage match is no longer 'path: /dev/nvme0n1', so the sed above silently did nothing. Refusing to build a sim ISO whose disk pin is unreviewed: re-check the storage stanza and update this substitution."
    grep -Eq '^[[:space:]]+(path|serial|wwn):' "$user_data_out" && \
        die "the sim user-data still carries a real-hardware disk match (path/serial/wwn) — a sim ISO must only ever be able to select a virtual disk. Refusing to build."
    fi

    # ── Does Subiquity actually ACCEPT this file? ────────────────────────────
    # Applies to BOTH branches: production user-data.filled and the sim render.
    validate_autoinstall_yaml "$user_data_out"

    # ── meta-data: fresh instance-id per build, vmtest hostname ──────────────
    sed \
        -e "s/instance-id: homehub-001/instance-id: homehub-vmtest-$(date +%Y%m%d%H%M%S)/" \
        -e 's/local-hostname: homehub/local-hostname: homehub-vmtest/' \
        "$autoinstall_src/meta-data" > "$out_dir/iso-root/meta-data"
    # Same silent-no-op hazard as the user-data seds. A stale instance-id is
    # worse than cosmetic: cloud-init uses it to decide whether this is a FRESH
    # instance, so a repeated one can make it skip first-boot work entirely.
    grep -Eq '^local-hostname: homehub-vmtest$' "$out_dir/iso-root/meta-data" || \
        die "meta-data local-hostname substitution did not apply — stack/autoinstall/meta-data no longer says 'local-hostname: homehub'. Update the sed above."
    grep -Eq '^instance-id: homehub-vmtest-[0-9]+$' "$out_dir/iso-root/meta-data" || \
        die "meta-data instance-id substitution did not apply — stack/autoinstall/meta-data no longer says 'instance-id: homehub-001'. cloud-init could treat this as a repeat instance and skip first-boot work. Update the sed above."

    # ── deploy-payload/ = a copy of the whole repo (late-commands expects
    #    deploy-payload/stack/... at its root) ────────────────────────────────
    local payload_dir="$out_dir/iso-root/deploy-payload"
    copy_repo_into_payload "$repo_root" "$payload_dir"

    # ── SIM .env — syntactically-valid, internally-consistent placeholders so
    #    `docker compose up -d` can actually run. See vmtest/README.md "what
    #    success looks like" for which services this does/doesn't get to
    #    healthy (the tracker image is a KNOWN gap, documented there). ────────
    # PRODUCTION SEAM (A14): a real build stages the materialized site files
    # into deploy-payload/site/ and returns before the sim .env block. The
    # autoinstall late-command 4b then installs them into /etc/homehub-samba,
    # /etc/homehub-backup and stack/.env on the target — reading them out of the
    # payload copy at /target/opt/homehub/site, which is why they must ride the
    # payload rather than be looked for on the boot medium (OI-19).
    # The SITE_DIR-without-user-data.filled downgrade is refused at the top of
    # this function, before anything is staged.
    if [ "$HUB_BUILD_KIND" = "production" ]; then
        local site_out="$payload_dir/site"
        mkdir -p "$site_out"
        local staged=0
        for f in .env backup.env cifs.creds samba-users.creds user-data.filled \
                 smb.conf.fragment library-mounts.fstab drive-identity.conf; do
            if [ -f "$SITE_DIR/$f" ]; then
                install -m 600 "$SITE_DIR/$f" "$site_out/$f"
                log "  site/ += $f"
                staged=$((staged + 1))
            fi
        done
        [ "$staged" -gt 0 ] || die "SITE_DIR=$SITE_DIR contained none of the expected files — nothing to bake."
        # stack/.env inside the payload is what late-command 4 would otherwise
        # seed from .env.example; overwrite it with the real one.
        [ -f "$SITE_DIR/.env" ] && install -m 600 "$SITE_DIR/.env" "$payload_dir/stack/.env"
        log "PRODUCTION build: $staged site file(s) staged; sim .env generation SKIPPED"
        return 0
    fi

    local sim_env="$payload_dir/stack/.env"
    local basicauth_hash_actual="REPLACE_WITH_caddy_hash-password_OUTPUT"
    local basicauth_hash_dns="REPLACE_WITH_caddy_hash-password_OUTPUT"
    if command -v docker >/dev/null 2>&1; then
        log "generating real Caddy bcrypt basic_auth hashes via docker (SIM passwords, not secrets)"
        basicauth_hash_actual="$(docker run --rm caddy:2-alpine caddy hash-password --plaintext "sim-actual-$(openssl rand -hex 4)" 2>/dev/null || echo "$basicauth_hash_actual")"
        basicauth_hash_dns="$(docker run --rm caddy:2-alpine caddy hash-password --plaintext "sim-dns-$(openssl rand -hex 4)" 2>/dev/null || echo "$basicauth_hash_dns")"
    else
        log "WARNING: docker not found in WSL - basic_auth hashes left as REPLACE_WITH placeholders." \
            "Caddy will still start, but basic_auth logins won't work until you run:" \
            "docker run --rm caddy:2-alpine caddy hash-password --plaintext 'yourpass'"
    fi

    cp "$stack_env_example" "$sim_env"
    # NOTE: the SIM .env pins the SAME image tags as .env.example (this is a copy
    # of it), so the images baked by export-images.sh match what compose asks for.
    # NOTE: delimiter is '|' throughout — several replacements carry an inline
    # NOTE: delimiter is '|' throughout — several replacements carry an inline
    # '#' YAML/dotenv comment, which would otherwise prematurely terminate a
    # '#'-delimited sed expression.
    sed -i \
        -e "s|^DOMAIN=.*|DOMAIN=vmtest.sim.invalid|" \
        -e "s|^LAN_IP=.*|LAN_IP=0.0.0.0   # VMTEST: unknown until Hyper-V NAT assigns a DHCP lease; 0.0.0.0 = bind all, harmless in a NAT'd test VM. Production MUST set the real LAN reservation IP.|" \
        -e "s|^MAIN_BOX_IP=.*|MAIN_BOX_IP=|" \
        -e "s|^DNS_HOSTNAME=.*|DNS_HOSTNAME=dns.vmtest.sim.invalid|" \
        -e "s|^ACME_EMAIL=.*|ACME_EMAIL=vmtest@example.invalid   # no real ACME in a VM, see vmtest/README.md|" \
        -e "s|^ACTUAL_BASICAUTH_HASH=.*|ACTUAL_BASICAUTH_HASH=$(compose_escape "$basicauth_hash_actual")|" \
        -e "s|^DNS_BASICAUTH_HASH=.*|DNS_BASICAUTH_HASH=$(compose_escape "$basicauth_hash_dns")|" \
        -e "s|^OAUTH2_PROXY_CLIENT_ID=.*|OAUTH2_PROXY_CLIENT_ID=sim-client-id.apps.googleusercontent.com|" \
        -e "s|^OAUTH2_PROXY_CLIENT_SECRET=.*|OAUTH2_PROXY_CLIENT_SECRET=sim-client-secret-not-real|" \
        -e "s|^OAUTH2_PROXY_COOKIE_SECRET=.*|OAUTH2_PROXY_COOKIE_SECRET=$cookie_secret|" \
        -e "s|^OAUTH2_PROXY_ALLOWED_EMAILS=.*|OAUTH2_PROXY_ALLOWED_EMAILS=simuser@example.invalid|" \
        -e "s|^TECHNITIUM_ADMIN_PASSWORD=.*|TECHNITIUM_ADMIN_PASSWORD=$technitium_pw|" \
        -e "s|^CLOUDFLARE_ZONE_ID=.*|CLOUDFLARE_ZONE_ID=sim-zone-id-not-real|" \
        -e "s|^CLOUDFLARE_API_TOKEN=.*|CLOUDFLARE_API_TOKEN=sim-token-not-real   # ddns WILL fail auth in the VM - expected, no healthcheck gates it|" \
        "$sim_env"

    apply_sim_env_overrides "$sim_env"
    assert_env_interpolation_safe "$sim_env"
}

# apply_sim_env_overrides FILE [OVERRIDES] [VAR_NAME] — fold KEY=VALUE pairs in.
#
# OVERRIDES defaults to $SIM_ENV_OVERRIDES (the hub's compose .env); the wall
# builder passes $WALL_ENV_OVERRIDES and its own VAR_NAME so the error messages
# name the knob the caller actually set. `$` is doubled on the way in
# (compose_escape) and collapsed back by the consumer — docker compose for the
# hub's .env, load_env_file() for the panel's wall.env — so the round-trip is
# the same on both sides.
#
# WHY: the sim .env is a copy of stack/.env.example, i.e. the GENERIC defaults —
# tier-2 profiles off, MEDIA_ROOT=/srv/media. That is the right default for the
# gate, but it means the gate cannot exercise the profile set a REAL image will
# boot with (the AWOW's lives in Personal's config.homehub.psd1, which never
# comes near this repo). Baking those values into .env.example instead would
# change the public default for everyone — wrong knob.
#
# So: an explicit, opt-in list of KEY=VALUE pairs, newline- or semicolon-
# separated, applied after every sed above. Example — boot the gate with the
# homehub image's opt-in set:
#
#   SIM_ENV_OVERRIDES='COMPOSE_PROFILES=ntfy,immich,immich-ml,jellyfin
#   IMMICH_ML_ENABLED=true
#   IMMICH_ML_MEM_LIMIT=2g' bash vmtest/build-seed.sh
#
# FAILS LOUDLY on a key that is not already in the file: a typo'd knob would
# otherwise append a line compose ignores, and the gate would quietly test the
# default set while reporting success — the exact silent-no-op failure mode the
# hostname/storage-pin assertions elsewhere in this file exist to prevent.
apply_sim_env_overrides() {
    local f="$1" overrides="${2-${SIM_ENV_OVERRIDES:-}}" var="${3:-SIM_ENV_OVERRIDES}"
    [ -n "$overrides" ] || return 0

    local pair key val
    # Split on newlines and semicolons; ignore blanks and comments.
    while IFS= read -r pair; do
        pair="${pair#"${pair%%[![:space:]]*}"}"     # ltrim
        pair="${pair%"${pair##*[![:space:]]}"}"     # rtrim
        case "$pair" in ''|\#*) continue ;; esac
        case "$pair" in *=*) : ;; *) die "$var entry '$pair' is not KEY=VALUE" ;; esac
        key="${pair%%=*}"
        val="${pair#*=}"
        grep -qE "^$key=" "$f" \
            || die "$var names '$key', which is not a knob in $(basename "$f")." \
                   "Appending it would be a silent no-op — the consumer reads only knobs it asks for." \
                   "Check the spelling, or add the knob to the example file first."
        # compose_escape + a delimiter the value cannot contain (WALL_DISABLE_INPUT
        # is pipe-separated, so '|' is not always safe here).
        local d esc
        d="$(sed_delim "$key$val")"
        esc="$(compose_escape "$val" | sed -e 's/[\\&]/\\&/g')"
        sed -i -e "s${d}^$key=.*${d}$key=$esc   # VMTEST override ($var)${d}" "$f"
        log "  sim env override: $key=$val"
    done < <(printf '%s\n' "$overrides" | tr ';' '\n')
}

# stage_images_into_payload OUT_DIR IMAGES_OUT
#
# Q10.9 B+ ALL-IMAGES: copy the docker-save image tars produced by
# vmtest/export-images.sh into the deploy-payload's images/ subdir, so they ride
# onto BOTH ISO paths and land at /opt/homehub/images on the target, where
# firstboot.sh docker-loads them before `docker compose up -d`. The tars live in
# vmtest/.out/images by default — which render_seed_tree's repo copy EXCLUDES
# (via --exclude=vmtest/.out) — so this is the one place they enter the payload.
#
# GRACEFUL: if the staging dir is empty/missing, warn LOUDLY and still build the
# seed — the box will fall back to pulling at first boot (firstboot.sh handles
# that path), except naglight:local which has no registry and would then fail.
stage_images_into_payload() {
    local out_dir="$1" images_out="$2"
    local dest="$out_dir/iso-root/deploy-payload/images"
    mkdir -p "$dest"

    local had_nullglob=0
    shopt -q nullglob && had_nullglob=1
    shopt -s nullglob
    local tars=("$images_out"/*.tar "$images_out"/*.tar.zst)

    if [ "${#tars[@]}" -eq 0 ]; then
        [ "$had_nullglob" -eq 1 ] || shopt -u nullglob
        log "WARNING: no image tars found in $images_out — the payload will carry NO"
        log "  container images. Run  bash vmtest/export-images.sh  first to bake the"
        log "  Q10.9 B+ all-images payload, then rebuild. Without it the VM falls back"
        log "  to pulling at first boot (needs internet; naglight:local has no registry"
        log "  home and will fail to start)."
        return 0
    fi

    log "staging ${#tars[@]} image tar(s) into deploy-payload/images/ (Q10.9 B+ ALL-IMAGES)"
    local total=0 t
    for t in "${tars[@]}"; do
        # hardlink when the filesystem allows (saves ~1GB of C: — OI-6); else copy.
        cp -l "$t" "$dest/" 2>/dev/null || cp -f "$t" "$dest/"
        total=$(( total + $(stat -c%s "$t") ))
    done
    [ -f "$images_out/images.manifest.tsv" ] && cp -f "$images_out/images.manifest.tsv" "$dest/"
    [ "$had_nullglob" -eq 1 ] || shopt -u nullglob

    log "deploy-payload/images/ = $(( total / 1024 / 1024 )) MB across ${#tars[@]} tar(s)"
    log "  -> lands at /opt/homehub/images on the target; firstboot.sh docker-loads it."
}

# ═══════════════════════════════════════════════════════════════════════════
#  IF-005 — the OfficeWallNaglight shell artifact (PKG-1)
#
#  That repo's `npm run dist` emits ONE build as TWO payloads, both stamped
#  with the same source commit (OfficeWallNaglight docs/design/packaging.md §4):
#
#    officewall-shell-<ver>-g<sha7>-linux-x64.tar.gz  -> the PANEL: unpacks to
#        app/, and app/wall-shell is what WALL_APP_CMD names.
#    officewall-site-<ver>-g<sha7>.tar.gz             -> the HUB: unpacks to
#        site/, which is the kiosk site's document root (stack/wall-shell/).
#
#  Two payloads because the same-origin contract forces it: NagLight sends no
#  CORS headers, so the renderer must be served by the origin that proxies
#  /api/* (the hub), while the Electron container is a process on the panel. A
#  hub and a panel whose stamps differ are a mismatched deploy — which is why
#  both stagers below LOG the commit they staged.
# ═══════════════════════════════════════════════════════════════════════════

# wall_dist_dir REPO_ROOT — where OfficeWallNaglight's `npm run dist` writes.
# Same sibling-checkout convention as scripts/ensure-local-images.sh (SIBLING_ROOT).
wall_dist_dir() {
    local sibling_root="${SIBLING_ROOT:-$(dirname "$1")}"
    printf '%s' "${WALL_SHELL_DIST:-$sibling_root/OfficeWallNaglight/dist}"
}

# find_wall_artifact DIST_DIR GLOB — echo the ONE matching artifact, or nothing.
#
# Refuses ambiguity and refuses a `-dirty` build. The dirty rule is the
# homehub.source.revision habit (a *:local artifact with no version is exactly
# what silently shipped three weeks stale on 2026-08-01): a build from an
# uncommitted tree cannot be reproduced from its own stamp, so an image must
# never carry one.
find_wall_artifact() {
    local dist_dir="$1" glob="$2"
    local had_nullglob=0
    shopt -q nullglob && had_nullglob=1
    shopt -s nullglob
    local matches=("$dist_dir"/$glob)
    [ "$had_nullglob" -eq 1 ] || shopt -u nullglob

    [ "${#matches[@]}" -eq 0 ] && return 0
    if [ "${#matches[@]}" -gt 1 ]; then
        die "more than one artifact matches $glob in $dist_dir:" \
            "$(printf '%s ' "${matches[@]##*/}")" \
            "Which one would ship is a coin flip. Clear the stale ones (npm run dist --clean) and rebuild."
    fi
    # BASENAME, not the whole path: `WALL_SHELL_DIST=/builds/pr-dirty-fix/dist`
    # is a directory name, not a claim about the artifact.
    case "${matches[0]##*/}" in
        *-dirty*)
            die "refusing to bake ${matches[0]##*/} — its filename says it was built from an" \
                "UNCOMMITTED tree, so the commit it names does not describe what is inside it" \
                "and the image could never be reproduced from its own stamp." \
                "Commit in OfficeWallNaglight, then re-run: npm run dist"
            ;;
    esac
    printf '%s' "${matches[0]}"
}

# assert_wall_artifact_contract REPO_ROOT TARBALL PROBE_DIR
#
# Cross-repo assertion: does this tarball still match the contract this image
# was WIRED against (packaging.md §4)? Three things are checked because each
# one, wrong, produces a black wall with no message on the screen:
#
#   1. app/wall-shell exists and is executable — `wall-kiosk.sh` keys everything
#      off `[ -x "${WALL_APP_CMD%% *}" ]`. If it is false the panel shows the
#      NOT INSTALLED screen, which is loud but wrong for a gate.
#   2. app/runtime/chrome-sandbox is 4755 root:root INSIDE the archive. Ubuntu
#      24.04 sets kernel.apparmor_restrict_unprivileged_userns=1, which closes
#      the namespace sandbox, so the SUID helper is the only route left and
#      Electron refuses to start without it. NTFS cannot express a setuid bit,
#      so this is the one property most likely to be silently lost.
#   3. every shared library the binary actually needs is a package this image
#      installs — see assert_electron_runtime_deps.
#
# 1 and 2 are read out of the ARCHIVE, not out of an extracted tree, because the
# extraction is exactly where the bits get lost.

# artifact_sha7 PATH — the g<sha7> stamp out of an artifact's filename.
artifact_sha7() {
    printf '%s' "$1" | sed -n 's/.*-g\([0-9a-f]\{7,\}\)\(-dirty\)\?\(-linux-x64\)\?\.tar\.gz$/\1/p'
}

# assert_payload_stamps_agree DIST_DIR KIND SHA7 — the mismatch nobody can see.
#
# PKG-1's whole argument for two payloads is that ONE build emits both with ONE
# source stamp, so a hub and a panel that disagree are visible rather than
# invisible. That argument is worth nothing if the builders never look. A review
# pointed out that each stager staged its own artifact and neither compared —
# so a shell tarball from one commit and a site tarball from another would both
# stage happily, and the "visible mismatch" would be two files nobody read.
#
# Checked here, where both are on the same disk, because it is the LAST moment
# they are: after this they ride different ISOs onto different machines.
assert_payload_stamps_agree() {
    local dist_dir="$1" kind="$2" sha7="$3" other other_sha7 other_glob
    [ -n "$sha7" ] || return 0
    case "$kind" in
        shell) other_glob='officewall-site-*.tar.gz' ;;
        site)  other_glob='officewall-shell-*-linux-x64.tar.gz' ;;
    esac
    local had_nullglob=0
    shopt -q nullglob && had_nullglob=1
    shopt -s nullglob
    local others=("$dist_dir"/$other_glob)
    [ "$had_nullglob" -eq 1 ] || shopt -u nullglob
    [ "${#others[@]}" -eq 1 ] || return 0     # absent or ambiguous: not this check's business

    other="${others[0]}"
    other_sha7="$(artifact_sha7 "$other")"
    [ -n "$other_sha7" ] || return 0
    [ "$sha7" = "$other_sha7" ] && return 0
    die "the two payloads of 'one build' come from DIFFERENT commits:" \
        "  staged here: g$sha7   other half: g$other_sha7 ($(basename "$other"))" \
        "The panel's container and the hub's renderer have to agree about /api/*," \
        "/media/* and the config shape, and a shared source stamp is the only thing" \
        "that proves they do. Rebuild both together: npm run dist  (it emits both)."
}

assert_wall_artifact_contract() {
    local repo_root="$1" tarball="$2" probe="$3" listing sandbox_line
    require_cmd tar "Install with: sudo apt-get install -y tar"

    listing="$probe/tar-listing.txt"
    mkdir -p "$probe"
    tar -tzvf "$tarball" > "$listing" || die "cannot read $tarball — is it a complete gzip?"

    grep -qE '^-rwxr-xr-x .* app/wall-shell$' "$listing" || \
        die "$(basename "$tarball") has no executable app/wall-shell." \
            "WALL_APP_CMD=/opt/wall-panel/app/wall-shell is the contract wall-kiosk.sh tests with [ -x ]." \
            "Listing says: $(grep -E ' app/wall-shell$' "$listing" || echo '<absent>')"

    # The wrapper is only the front door. `[ -x wall-shell ]` can be true while
    # the two things it execs are broken — and BOTH of those failures are
    # invisible: the panel restarts every 3 s with nothing on screen, because
    # `[ -x ]` never becomes false. Assert what the wrapper itself requires.
    grep -qE '^-rwx.* app/runtime/electron$' "$listing" || \
        die "$(basename "$tarball")'s app/runtime/electron is not executable." \
            "The wrapper execs it; a non-executable runtime is a permanently dark panel." \
            "Listing says: $(grep -E ' app/runtime/electron$' "$listing" || echo '<absent>')"
    grep -qE ' app/runtime/resources/app/index\.html$' "$listing" || \
        die "$(basename "$tarball") has no app/runtime/resources/app/index.html — the application" \
            "bundle is missing, so Electron starts and has nothing to load. Rebuild in" \
            "OfficeWallNaglight (npm run dist); do not repack by hand."

    sandbox_line="$(grep -E ' app/runtime/chrome-sandbox$' "$listing" || true)"
    [ -n "$sandbox_line" ] || die "$(basename "$tarball") has no app/runtime/chrome-sandbox — Electron cannot start without it."
    case "$sandbox_line" in
        -rwsr-xr-x*root/root*) : ;;
        *)  die "app/runtime/chrome-sandbox is not 4755 root:root inside the archive:" \
                "$sandbox_line" \
                "Ubuntu 24.04 disables the unprivileged-userns sandbox, so the SUID helper is the" \
                "only route left and Electron exits rather than run unsandboxed. The panel would" \
                "crash-loop showing NOTHING — [ -x ] stays true, so the NOT INSTALLED screen never" \
                "fires. Rebuild in OfficeWallNaglight (npm run dist); do not repack by hand."
            ;;
    esac
    log "  artifact contract OK: app/wall-shell is -rwxr-xr-x, chrome-sandbox is 4755 root:root"
}

# assert_electron_runtime_deps REPO_ROOT TARBALL PROBE_DIR
#
# THE `ldd` CHECK, MOVED TO WHERE IT CAN STILL BE ACTED ON. On the panel the
# diagnosis is `ldd /opt/wall-panel/app/runtime/electron | grep 'not found'` —
# true, but by then the wall is black and someone has to drive to it. The build
# host cannot run the panel's loader, so it asserts the same thing statically
# and one step earlier:
#
#     every DT_NEEDED soname in the shipped binary
#       -> is mapped in stack/autoinstall/wall/electron-runtime-deps.tsv
#       -> whose package is in the wall user-data's `packages:` list
#
# That closes the loop the doc alone leaves open: a Electron bump that adds a
# dependency now fails THIS build with the soname named, instead of producing a
# bootable image whose only symptom is a dark panel. (Verified 2026-08-02: with
# a correct install and none of these packages, Electron stops at
# "libnspr4.so: cannot open shared object file" — the list is load-bearing.)
assert_electron_runtime_deps() {
    local repo_root="$1" tarball="$2" probe="$3"
    local table="$repo_root/stack/autoinstall/wall/electron-runtime-deps.tsv"
    local user_data="$repo_root/stack/autoinstall/wall/user-data"
    [ -f "$table" ] || die "not found: $table (the soname -> package map this check reads)"
    [ -f "$probe/tar-listing.txt" ] || die "internal: assert_wall_artifact_contract must run first (it writes the listing this check reads)"
    require_cmd readelf "Install with: sudo apt-get install -y binutils"
    require_cmd python3 "Install it with: sudo apt-get install -y python3"

    mkdir -p "$probe"
    log "  reading the shipped binary's DT_NEEDED list (decompresses the archive once)"
    tar -xzf "$tarball" -C "$probe" app/runtime/electron app/build-info.json app/VERSION \
        || die "$(basename "$tarball") is missing one of app/runtime/electron, app/build-info.json, app/VERSION" \
               "— that is not the packaging.md §4 layout this image is wired against."

    # THE STAMP, READ FROM THE ARCHIVE. The filename check in find_wall_artifact
    # is a convention; this is the fact. A review pointed out that the refusal
    # message claimed to have read build-info.json when nothing had opened it —
    # a clean-looking filename around a dirty build would have sailed through.
    python3 - "$probe/app/build-info.json" <<'PY' || die "the artifact's own build-info.json says it was built from an UNCOMMITTED tree, whatever its filename says. Commit in OfficeWallNaglight and re-run: npm run dist"
import json, sys
info = json.load(open(sys.argv[1]))
sys.exit(1 if (info.get("source") or {}).get("dirty") else 0)
PY

    # The packages: list of the wall user-data, one name per line, comments off.
    # Written to a FILE, then grepped — never `printf … | grep -q`. Under
    # pipefail an early-exiting grep SIGPIPEs the writer and the pipeline
    # reports failure, i.e. the check reports "package missing" precisely when
    # it FOUND the package. The same shape cost a good site tarball a refusal
    # ten lines below; once is enough.
    local declared declared_file="$probe/declared-packages.txt"
    declared="$(awk '
        /^  packages:/            { inpkgs = 1; next }
        inpkgs && /^  [^ ]/       { inpkgs = 0 }
        inpkgs && /^[[:space:]]*-[[:space:]]/ {
            sub(/^[[:space:]]*-[[:space:]]*/, ""); sub(/[[:space:]]*#.*/, ""); print
        }' "$user_data")"
    [ -n "$declared" ] || die "could not read a packages: list out of $user_data"
    printf '%s\n' "$declared" > "$declared_file"

    # READELF INTO A FILE, AND CHECK IT WORKED. This used to be a here-document
    # wrapping a command substitution: if `readelf` failed — wrong binutils, a
    # truncated extraction, an architecture it does not know — the substitution
    # produced an EMPTY dependency list, the loop below ran zero times, and the
    # function logged "runtime deps OK". A check that passes hardest when it
    # cannot see anything is worse than no check, and a review found this one.
    local needed_list="$probe/dt-needed.txt"
    readelf -d "$probe/app/runtime/electron" > "$probe/readelf.txt" 2>&1 || \
        die "readelf could not read app/runtime/electron out of $(basename "$tarball"):" \
            "$(tail -n 3 "$probe/readelf.txt")" \
            "Without its DT_NEEDED list this build cannot tell whether the image installs" \
            "what the shell needs, and a missing library is a black wall. Refusing to guess."
    sed -n 's/.*NEEDED.*\[\(.*\)\]/\1/p' "$probe/readelf.txt" | sort -u > "$needed_list"
    [ -s "$needed_list" ] || \
        die "app/runtime/electron declares NO shared library dependencies at all." \
            "A dynamically-linked Electron binary has ~34 of them, so this is not a lucky" \
            "static build — it is a readelf that parsed something else, or a truncated" \
            "extraction. Refusing to record a pass this check did not earn."

    local soname pkg where missing_map="" missing_pkg=""
    while IFS= read -r soname; do
        [ -n "$soname" ] || continue
        # Column 1 = soname, 2 = package, 3 = where it comes from.
        local row
        row="$(awk -F'\t' -v s="$soname" '$1 == s { print $2 "\t" $3; exit }' "$table")"
        if [ -z "$row" ]; then
            missing_map="$missing_map $soname"
            continue
        fi
        pkg="${row%%$'\t'*}"
        where="${row#*$'\t'}"
        # `bundled` means "the artifact carries it" — so CHECK that it does,
        # rather than taking the table's word for it. A row marked bundled is
        # otherwise a hole straight through this whole check.
        if [ "$where" = "bundled" ]; then
            grep -qE " app/runtime/$soname\$" "$probe/tar-listing.txt" || \
                die "$table marks $soname as bundled with the artifact, but the artifact does" \
                    "not contain app/runtime/$soname. Either the packaging changed or the" \
                    "table is lying; both end as a black wall."
            continue
        fi
        [ "$where" = "image-packages" ] || continue
        grep -qxF "$pkg" "$declared_file" || missing_pkg="$missing_pkg $pkg($soname)"
    done < "$needed_list"

    [ -z "$missing_map" ] || \
        die "the shell artifact needs shared library(s) this repo has never heard of:$missing_map" \
            "A missing library is a BLACK WALL whose only diagnosis is ldd on the panel." \
            "Add each soname to $table with the Ubuntu package that provides it" \
            "(dpkg -S /usr/lib/x86_64-linux-gnu/<soname>), then add that package to the" \
            "packages: list in $user_data."
    [ -z "$missing_pkg" ] || \
        die "the wall image does not install package(s) the shell artifact needs:$missing_pkg" \
            "Add them to the packages: list in $user_data. Without them Electron exits at" \
            "load time and the panel crash-loops with nothing on screen — [ -x ] is still" \
            "true, so the NOT INSTALLED screen never fires either."
    log "  runtime deps OK: every DT_NEEDED soname maps to a package the wall image installs"
}

# stage_wall_shell_into_payload OUT_DIR REPO_ROOT — the PANEL half.
#
# Folds the shell tarball into deploy-payload/wall-app/, so it rides the seed
# ISO, lands at /opt/wall-panel/wall-app/ (late-command 3) and is untarred to
# /opt/wall-panel/app/ (late-command 3b) — AS ROOT, WITH tar, because a copy or
# an unzip drops the setuid bit.
#
# FAILS LOUDLY when the artifact is absent. That is deliberate and it is the
# opposite of stage_images_into_payload's graceful degrade: an image built
# without it boots to the NOT INSTALLED screen, which is the RIGHT behaviour on
# real hardware and the WRONG outcome for a gate — the gate would "run" and
# prove nothing. ALLOW_MISSING_SHELL=1 opts out, loudly, for exercising the
# image layer alone.
stage_wall_shell_into_payload() {
    local out_dir="$1" repo_root="$2"
    local dist_dir; dist_dir="$(wall_dist_dir "$repo_root")"
    local dest="$out_dir/iso-root/deploy-payload/wall-app"
    local probe="$out_dir/.artifact-probe"
    local tarball; tarball="$(find_wall_artifact "$dist_dir" 'officewall-shell-*-linux-x64.tar.gz')"

    if [ -z "$tarball" ]; then
        if [ "${ALLOW_MISSING_SHELL:-0}" -eq 1 ]; then
            log "WARNING: ALLOW_MISSING_SHELL=1 — building a wall ISO with NO shell artifact."
            log "  The panel WILL boot to the 'NOT INSTALLED' screen. This image can test the"
            log "  image layer (Wi-Fi, quirks, sleep, sync) and NOTHING about the kiosk."
            return 0
        fi
        die "no OfficeWallNaglight shell artifact in $dist_dir" \
            "An image built without it boots to the NOT INSTALLED screen — correct on real" \
            "hardware, useless as a gate, so this build refuses rather than hand you one." \
            "Build it:  cd $(dirname "$dist_dir") && npm install && npm run dist" \
            "Elsewhere: WALL_SHELL_DIST=/path/to/dist bash vmtest/build-wall-seed.sh" \
            "Deliberately without it: ALLOW_MISSING_SHELL=1 bash vmtest/build-wall-seed.sh"
    fi

    rm -rf "$probe"
    assert_wall_artifact_contract "$repo_root" "$tarball" "$probe"
    assert_electron_runtime_deps "$repo_root" "$tarball" "$probe"
    assert_payload_stamps_agree "$dist_dir" shell "$(artifact_sha7 "$tarball")"

    mkdir -p "$dest"
    # hardlink when the filesystem allows (saves ~110MB of C: — OI-6); else copy.
    cp -l "$tarball" "$dest/" 2>/dev/null || cp -f "$tarball" "$dest/"
    cp -f "$probe/app/build-info.json" "$dest/build-info.json"

    local rev size
    rev="$(sed -n 's/.*"revision"[^"]*"\([0-9a-f]*\)".*/\1/p' "$probe/app/build-info.json" | head -n1)"
    size=$(( $(stat -c%s "$tarball") / 1024 / 1024 ))
    log "deploy-payload/wall-app/ = ${size} MB — $(basename "$tarball")"
    log "  source commit ${rev:0:12}, shell version $(cat "$probe/app/VERSION" 2>/dev/null || echo '?')"
    log "  -> lands at /opt/wall-panel/wall-app/; user-data late-command 3b untars it to /opt/wall-panel/app/."
}

# stage_wall_site_into_payload OUT_DIR REPO_ROOT — the HUB half.
#
# The kiosk site's document root (stack/wall-shell/, bind-mounted read-only into
# caddy as /srv/wall-shell) is empty in this repo BY DESIGN — the "no product
# source" constraint. The renderer therefore has to arrive as a payload, the way
# naglight:local does, and firstboot.sh unpacks it there before compose up.
#
# GRACEFUL, unlike the panel half: MiniPC-Deployer is public and
# OfficeWallNaglight is not, so a checkout without the sibling must still build
# a hub ISO. Without it the kiosk site serves 404 at / while /api/* works —
# the documented half-built state, not a bug. It IS a bug for the A19 gate,
# which is why the warning says so.
stage_wall_site_into_payload() {
    local out_dir="$1" repo_root="$2"
    local dist_dir; dist_dir="$(wall_dist_dir "$repo_root")"
    local dest="$out_dir/iso-root/deploy-payload/wall-site"
    local tarball; tarball="$(find_wall_artifact "$dist_dir" 'officewall-site-*.tar.gz')"

    if [ -z "$tarball" ]; then
        log "WARNING: no OfficeWallNaglight SITE artifact in $dist_dir — the hub's kiosk"
        log "  site will serve 404 at / (its /api/* proxy still works). That is the"
        log "  documented half-built state on a checkout without the private sibling,"
        log "  but it means a PANEL booted against this hub renders nothing. Build it with"
        log "  'npm run dist' in OfficeWallNaglight, or set WALL_SHELL_DIST."
        return 0
    fi

    # OPEN IT. The panel half is checked member-by-member; this one used to be
    # checked by FILENAME ALONE, so a truncated download or a tar with the wrong
    # internal layout sailed through the build, and firstboot's unpack failure
    # is only a WARNING (correct — a hub with no kiosk site is still a working
    # hub) which then gets stamped `.provisioned` and never retried. The symptom
    # would surface a session later, as a panel showing nothing.
    # Listing to a FILE first, not `tar … | grep -q`. Under `set -o pipefail`
    # (which both hub builders set) grep -q exits at the first match, tar takes
    # SIGPIPE, and the pipeline reports 141 — so the check fails hardest when
    # the file is CORRECT. Caught by exactly that: a good site tarball was
    # refused for having no site/index.html, which it did have.
    local site_listing="$out_dir/.site-listing.txt"
    tar -tzf "$tarball" > "$site_listing" 2>/dev/null || \
        die "$(basename "$tarball") is not a readable gzip — truncated download or a partial write." \
            "Rebuild it in OfficeWallNaglight (npm run dist)."
    grep -qx 'site/index.html' "$site_listing" || \
        die "$(basename "$tarball") has no site/index.html at its root." \
            "firstboot unpacks it with --strip-components=1 into the kiosk site's document" \
            "root (packaging.md §4), so the wrong layout means the site 404s at / with" \
            "nothing failing loudly anywhere. Rebuild it; do not repack by hand."

    assert_payload_stamps_agree "$dist_dir" site "$(artifact_sha7 "$tarball")"

    mkdir -p "$dest"
    cp -f "$tarball" "$dest/"
    [ -f "$dist_dir/build-info.json" ] && cp -f "$dist_dir/build-info.json" "$dest/build-info.json"
    log "deploy-payload/wall-site/ = $(basename "$tarball") ($(( $(stat -c%s "$tarball") / 1024 )) KB, site/index.html present)"
    log "  -> lands at /opt/homehub/wall-site/; firstboot.sh unpacks it into stack/wall-shell/."
}

# render_wall_seed_tree REPO_ROOT OUT_DIR CALLER_NAME
#
# The wall panel's equivalent of render_seed_tree: materializes
# $OUT_DIR/iso-root/{user-data,meta-data,deploy-payload/} from the REAL
# stack/autoinstall/wall/ files with SIM values substituted, plus a filled SIM
# wall.env in deploy-payload/site/.
#
# TWO PATHS, AND MIXING THEM IS THE DANGEROUS OUTCOME. Same seam as the hub's
# (A14): with WALL_SITE_DIR pointing at Personal's materialised
# `homelab\deploy\out\wall`, this is a REAL build — take `user-data.filled`
# verbatim, stage the real `wall.env`, and skip every sim substitution. Without
# it, everything is a throwaway placeholder for a local VM.
#
# Containment for the sim path is the reason a sim ISO is safe to have lying
# around: the disk pin is rewritten to `model: Virtual_Disk`, which ONLY a
# Hyper-V synthetic disk reports. On real hardware it matches nothing and the
# unattended install HALTS (a match: that matches nothing is fail-safe; an
# absent one is not). The panel's own pin — `model: KINGSTON*RBU-SNS8152S3256GG2`
# — must not survive into a sim image, because a sim ISO written to a USB stick
# would then be able to wipe the real panel. The production path has the mirror
# containment: it refuses a `Virtual_Disk` pin, which on real hardware would
# halt every install with nothing explaining why.
render_wall_seed_tree() {
    local repo_root="$1" out_dir="$2" caller="$3"
    local src="$repo_root/stack/autoinstall/wall"

    [ -f "$src/user-data" ] || die "not found: $src/user-data (run from a MiniPC-Deployer checkout)"
    [ -f "$src/meta-data" ] || die "not found: $src/meta-data"
    [ -f "$src/wall.env.example" ] || die "not found: $src/wall.env.example"
    require_cmd openssl "Install with: sudo apt-get install -y openssl"

    # WALL_SITE_DIR set but no user-data.filled would be a SILENT DOWNGRADE, the
    # hub's exact lesson: the real wall.env (with the Wi-Fi PSK) would still be
    # staged while user-data fell through to the sim seds below — producing a
    # "production" stick with allow-pw: true, a known sim password hash, and a
    # disk pin that matches nothing on the panel. Refuse instead of mixing.
    WALL_BUILD_KIND=sim   # exported for the caller's summary
    if [ -n "${WALL_SITE_DIR:-}" ]; then
        [ -d "$WALL_SITE_DIR" ] || die "WALL_SITE_DIR=$WALL_SITE_DIR is not a directory"
        [ -f "$WALL_SITE_DIR/user-data.filled" ] || \
            die "WALL_SITE_DIR=$WALL_SITE_DIR has no user-data.filled — refusing to stage the real" \
                "wall.env (it carries the Wi-Fi PSK) onto a SIM-substituted user-data." \
                "Run:  pwsh Materialize-Deploy.ps1 -Image wall   (Personal\\homelab\\deploy)"
        WALL_BUILD_KIND=production
    fi

    [ "${CLEAN:-0}" -eq 1 ] && wipe_build_dir "$out_dir"
    mark_build_dir "$out_dir"

    mkdir -p "$out_dir/ssh" "$out_dir/iso-root/deploy-payload" "$out_dir/secrets"
    chmod 700 "$out_dir/ssh" "$out_dir/secrets"

    local user_data_out="$out_dir/iso-root/user-data"

    if [ "$WALL_BUILD_KIND" = "production" ]; then
        log "PRODUCTION: using $WALL_SITE_DIR/user-data.filled (real key + real disk pin; no sim substitution)"
        SSH_KEY=""; CREDS_FILE=""
        cp "$WALL_SITE_DIR/user-data.filled" "$user_data_out"

        # Every guard is ANCHORED TO AN ACTIVE SETTING, never to a substring:
        # this file is the tracked user-data with placeholders filled, and the
        # tracked user-data explains each of these things in a COMMENT that
        # quotes the placeholder. A naive grep refuses a perfectly good
        # production build — which is exactly what happened to the hub's
        # equivalent guards on 2026-07-30.
        grep -Eq '^[[:space:]]*[A-Za-z_-]+:.*REPLACE_WITH' "$user_data_out" && \
            die "user-data.filled has an ACTIVE setting still holding a REPLACE_WITH placeholder — re-run Materialize-Deploy.ps1 -Image wall and fix what it names."
        grep -Eq '^[[:space:]]*allow-pw:[[:space:]]*true' "$user_data_out" && \
            die "user-data.filled sets allow-pw: true — the panel is SSH-key-only, same posture as the hub. Refusing to bake it."
        # THE MOST DESTRUCTIVE LINE IN THE PROJECT IS THE UNGUARDED ONE.
        # `storage: layout:` wipes whatever Subiquity selects, unattended, and
        # without a `match:` it picks by heuristic.
        grep -Eq '^[[:space:]]+(path|serial|model|wwn):' "$user_data_out" || \
            die "user-data.filled has no storage.layout match: — the unattended wipe would pick a disk by heuristic. Re-run Materialize-Deploy.ps1 -Image wall (its out\\ tree is stale)."
        grep -Eq '^[[:space:]]+model:[[:space:]]*Virtual_Disk' "$user_data_out" && \
            die "user-data.filled pins model: Virtual_Disk — that is the SIM pin, and only a Hyper-V synthetic disk reports it. On the real panel it would match nothing and every install would halt with nothing on screen explaining why. This stick was built from a sim artifact; re-run Materialize-Deploy.ps1 -Image wall."
        # The panel has NO RJ45 (hardware baseline 2026-07-26). A production
        # image whose network block lost its `wifis:` stanza has no network at
        # all — and no way to tell you, because telling you needs the network.
        grep -Eq '^[[:space:]]*wifis:' "$user_data_out" || \
            die "user-data.filled declares no wifis: — the panel has no ethernet port, so this image would come up with NO network and no way to reach it. Refusing to bake it."
    else

    ensure_sim_ssh_key "$out_dir"
    local ssh_pubkey; ssh_pubkey="$(cat "$SSH_KEY.pub")"

    # ── SIM console password ─────────────────────────────────────────────────
    # Production is SSH-key-only with a LOCKED password, and tty1 autologins
    # into cage — so on a real panel there is no console shell at all. In a VM
    # that would mean a black kiosk and no way in if the network misbehaves,
    # which is the state you most need a way in for. The sim therefore unlocks
    # a password so Ctrl+Alt+F2 gets a login. NEVER on the real panel.
    local creds_file="$out_dir/secrets/creds.env" sim_password sim_password_hash
    CREDS_FILE="$creds_file"   # exported for the caller
    if [ ! -f "$creds_file" ]; then
        sim_password="wallvm-$(openssl rand -hex 6)"
        sim_password_hash="$(openssl passwd -6 "$sim_password")"
        {
            sim_banner "$caller"
            echo "SIM_CONSOLE_PASSWORD=$sim_password"
            echo "SIM_CONSOLE_PASSWORD_HASH=$sim_password_hash"
        } > "$creds_file"
        chmod 600 "$creds_file"
    else
        log "reusing existing SIM secrets at $creds_file (pass --clean to regenerate)"
        # By grep, not by sourcing: the hash contains '$N$...' crypt syntax that
        # bash would try to expand as positional parameters under `set -u`.
        sim_password="$(grep '^SIM_CONSOLE_PASSWORD=' "$creds_file" | cut -d= -f2-)"
        sim_password_hash="$(grep '^SIM_CONSOLE_PASSWORD_HASH=' "$creds_file" | cut -d= -f2-)"
    fi

    # ── user-data: identity + console + disk pin ─────────────────────────────
    sed \
        -e "s#- \"ssh-ed25519 AAAA_REPLACE_WITH_YOUR_PUBLIC_KEY you@host\"#- \"$ssh_pubkey\"#" \
        -e 's/allow-pw: false/allow-pw: true   # VMTEST ONLY - the real panel is key-only/' \
        -e "s|password: \"!\"|password: \"$sim_password_hash\"   # VMTEST ONLY sim password, see the builder's secrets/creds.env|" \
        -e 's/hostname: wall-panel/hostname: wall-panel-vmtest/' \
        -e 's/realname: "Office Wall Panel"/realname: "Office Wall Panel VM Test"/' \
        -e 's|^\([[:space:]]*\)model: KINGSTON.*|\1model: Virtual_Disk|' \
        "$src/user-data" > "$user_data_out"

    # ── Wi-Fi -> ethernet, because Hyper-V cannot emulate a radio ────────────
    # The panel has no RJ45 on real hardware, so the shipped user-data declares
    # `wifis:` — and the INSTALLER needs the network for apt. A VM given that
    # block configures nothing, installs nothing, and dies fetching `cage` with
    # apt exit 100 (the same failure the hub image hit in 81eb3a4, three steps
    # downstream of the real cause). Swap the whole block for the hub's ethernet
    # matcher, `e*` not `en*`: Hyper-V's VMBus NIC comes up as `eth0`, which
    # predictable naming never touches.
    #
    # THE CONSEQUENCE IS THE POINT, AND §3 MUST STATE IT: a gate run this way
    # proves the kiosk/identity/render path and proves NOTHING about the Wi-Fi
    # path — not `macaddress: permanent`, not powersave-off, not the DHCP
    # reservation the /32 allow-list is keyed to. Those stay hardware-only (C7).
    awk '
        /^    wifis:/ {
            print "    ethernets:"
            print "      any-eth:"
            print "        match:"
            print "          name: \"e*\""
            print "        dhcp4: true"
            skip = 1
            next
        }
        skip && /^  [^ ]/ { skip = 0 }
        !skip
    ' "$user_data_out" > "$user_data_out.tmp" && mv "$user_data_out.tmp" "$user_data_out"

    # ── every substitution above is a silent no-op if its source string moved ─
    grep -q "REPLACE_WITH_YOUR_PUBLIC_KEY" "$user_data_out" && die "SSH placeholder substitution failed"
    grep -Eq '^[[:space:]]*hostname: wall-panel-vmtest$' "$user_data_out" || \
        die "hostname substitution did not apply — stack/autoinstall/wall/user-data no longer says 'hostname: wall-panel'. The sim ISO would boot claiming the PRODUCTION hostname, and the hub's /32 allow-list is keyed to a specific panel. Update the sed above."
    grep -Eq '^[[:space:]]*realname: "Office Wall Panel VM Test"$' "$user_data_out" || \
        die "realname substitution did not apply — stack/autoinstall/wall/user-data no longer says 'realname: \"Office Wall Panel\"'. Update the sed above."
    grep -Eq '^[[:space:]]+model: Virtual_Disk$' "$user_data_out" || \
        die "vmtest storage pin was NOT applied — stack/autoinstall/wall/user-data's storage match is no longer 'model: KINGSTON...', so the sed above silently did nothing. Refusing to build a sim ISO whose disk pin is unreviewed."
    # ANCHORED TO AN ACTIVE SETTING, not to the substring. The shipped user-data
    # explains the pin in a COMMENT that names the disk ("KINGSTON
    # RBU-SNS8152S3256GG2, 238.5 GB — 2026-07-26 baseline"), so a naive grep
    # refuses a perfectly good sim build. Exactly the lesson the hub's
    # user-data.filled guards learned on 2026-07-30, re-learned here.
    grep -Eq '^[[:space:]]*[A-Za-z_.-]+:[^#]*KINGSTON' "$user_data_out" && \
        die "the sim user-data still names the PANEL'S REAL DISK MODEL in an active setting — a sim ISO must only ever be able to select a virtual disk. Refusing to build."
    grep -Eq '^[[:space:]]+(path|serial|wwn):' "$user_data_out" && \
        die "the sim user-data carries a real-hardware disk match (path/serial/wwn). Refusing to build."
    grep -Eq '^[[:space:]]*ethernets:$' "$user_data_out" || \
        die "the wifis: -> ethernets: rewrite did not apply — stack/autoinstall/wall/user-data's network block changed shape. A VM would install with NO network and die fetching packages. Update the awk above."
    grep -qE '^[[:space:]]*(wifis|access-points):' "$user_data_out" && \
        die "the sim user-data still declares Wi-Fi — Hyper-V cannot emulate a radio and the installer would have no network. The awk above did not consume the whole block."
    grep -q 'REPLACE_WITH_WIFI' "$user_data_out" && \
        die "the sim user-data still carries Wi-Fi placeholders. The awk above did not consume the whole block."
    fi

    # Does Subiquity actually ACCEPT this file, and WHICH DISK will it wipe?
    # Both apply to BOTH branches — the production one most of all, since nobody
    # re-reads a generated file.
    validate_autoinstall_yaml "$user_data_out"
    assert_storage_pin "$user_data_out" "$WALL_BUILD_KIND"

    # ── meta-data: a FRESH instance-id per build, always ─────────────────────
    # cloud-init uses instance-id to decide whether this is a new instance, so a
    # repeated one can make it skip first-boot work entirely. The hostname is
    # marked only on the sim path — a production panel must answer to
    # `wall-panel`, which is what the kiosk site's records point at.
    local iid="wall-panel-$(date +%Y%m%d%H%M%S)" hostname_sed='' expect_host='wall-panel'
    if [ "$WALL_BUILD_KIND" = "sim" ]; then
        iid="wall-panel-vmtest-$(date +%Y%m%d%H%M%S)"
        hostname_sed='s/local-hostname: wall-panel/local-hostname: wall-panel-vmtest/'
        expect_host='wall-panel-vmtest'
    fi
    sed -e "s/instance-id: wall-panel-001/instance-id: $iid/" \
        ${hostname_sed:+-e "$hostname_sed"} \
        "$src/meta-data" > "$out_dir/iso-root/meta-data"
    grep -Eq "^local-hostname: ${expect_host}\$" "$out_dir/iso-root/meta-data" || \
        die "meta-data local-hostname is not '$expect_host' — stack/autoinstall/wall/meta-data changed shape. Update the sed above."
    grep -Eq '^instance-id: wall-panel-(vmtest-)?[0-9]+$' "$out_dir/iso-root/meta-data" || \
        die "meta-data instance-id substitution did not apply — cloud-init could treat this as a repeat instance and skip first-boot work. Update the sed above."

    copy_repo_into_payload "$repo_root" "$out_dir/iso-root/deploy-payload"

    if [ "$WALL_BUILD_KIND" = "production" ]; then
        stage_wall_site_files "$WALL_SITE_DIR" "$out_dir/iso-root/deploy-payload"
    else
        render_sim_wall_env "$repo_root" "$out_dir/iso-root/deploy-payload" "$caller"
    fi
}

# stage_wall_site_files SITE_DIR PAYLOAD_DIR — the PRODUCTION config payload.
#
# Personal's `Materialize-Deploy.ps1 -Image wall` writes `wall.env` and
# `user-data.filled` into `homelab\deploy\out\wall`. The first lands here (and
# then at /etc/wall-panel/wall.env, 0600, via late-command 4a); the second was
# already consumed above.
#
# `cifs.creds` is staged IF PRESENT and is deliberately not required: the
# materialiser emits it for the hub only today, even though the ruling of
# 2026-08-01 gave BOTH machines the same read-only `share` account and the
# panel's MEDIA_CIFS_CREDENTIALS points straight at /etc/wall-panel/cifs.creds.
# Until that is fixed on the Personal side, a production panel mounts nothing
# and `wall-sync.service` fails loudly — which is the honest state, but it is a
# gap, so say so here rather than let a silent absence read as "not needed".
stage_wall_site_files() {
    local site_dir="$1" payload_dir="$2"
    local site_out="$payload_dir/site" staged=0 f
    mkdir -p "$site_out"
    for f in wall.env cifs.creds; do
        if [ -f "$site_dir/$f" ]; then
            install -m 600 "$site_dir/$f" "$site_out/$f"
            log "  site/ += $f"
            staged=$((staged + 1))
        fi
    done
    [ -f "$site_out/wall.env" ] || \
        die "$site_dir has user-data.filled but no wall.env — the panel would fall back to" \
            "wall.env.example, i.e. REPLACE_WITH placeholders for the Wi-Fi SSID/PSK and the" \
            "kiosk host, and come up with no network and no origin. Re-run" \
            "Materialize-Deploy.ps1 -Image wall."
    [ -f "$site_out/cifs.creds" ] || \
        log "NOTE: no cifs.creds in $site_dir — the panel will not be able to mount" \
            "MEDIA_SHARE_UNC, so wall-sync.service fails and there is no music or frame" \
            "video. Materialize-Deploy.ps1 emits cifs.creds for the hub image only; the" \
            "2026-08-01 ruling gave both machines the same read-only 'share' account."
    log "PRODUCTION build: $staged wall config file(s) staged; sim wall.env generation SKIPPED"
}

# render_sim_wall_env REPO_ROOT PAYLOAD_DIR CALLER — the SIM /etc/wall-panel/wall.env.
#
# Written to deploy-payload/site/wall.env; late-command 4a installs it 0600 and
# late-command 4's "seed from the example" then no-ops. `site/` is the same
# name the hub image uses for materialised config, so the panel gains the same
# seam the day something fills it.
#
# Every value is a DELIBERATE sim choice, and four of them are choices a reader
# will otherwise think are mistakes:
render_sim_wall_env() {
    local repo_root="$1" payload_dir="$2" caller="$3"
    local src="$repo_root/stack/autoinstall/wall/wall.env.example"
    local site_out="$payload_dir/site"
    local env_out="$site_out/wall.env"

    mkdir -p "$site_out"
    cp "$src" "$env_out"

    # The origin the shell loads. Must resolve, from the panel, to the hub —
    # §3 sets it to a name the hub's Technitium answers for.
    set_env_key "$env_out" WALL_HOST "${WALL_SIM_HOST:-wall.vmtest.sim.invalid}"
    set_env_key "$env_out" WALL_PORT "${WALL_SIM_PORT:-8443}"

    # Wi-Fi: FILLED, and never used. The sim user-data installs an ethernet
    # netplan (see render_wall_seed_tree), so wall-firstboot.sh would render a
    # SECOND, wireless netplan from these values against an interface that does
    # not exist. They are non-placeholder so firstboot does not warn about
    # REPLACE_WITH, and obviously fake so nobody mistakes them for the house.
    set_env_key "$env_out" WIFI_SSID "vmtest-no-radio-in-hyperv"
    set_env_key "$env_out" WIFI_PSK "vmtest-not-a-real-psk"

    # SLEEP: `backlight`, not the production `suspend`. A gate VM that suspends
    # itself at 22:00 is indistinguishable from a gate VM that died, and its
    # RTC wake is the host's clock, not the panel's firmware. backlight mode is
    # additionally INERT here — a Hyper-V guest has no /sys/class/backlight, so
    # wall-sleep.sh logs that it cannot dim and leaves the screen up, which is
    # what a screen-capture gate needs. D-W4 is hardware-only either way.
    set_env_key "$env_out" SLEEP_MODE "backlight"
    set_env_key "$env_out" SLEEP_RTC_WAKE "false"

    # MEDIA: sim builds skip Samba (no smb.conf.fragment), so there is nothing
    # to mount. Left obviously-invalid rather than plausible: wall-sync.service
    # will FAIL loudly at boot, which is the honest state — a green sync unit
    # with no media would be a lie (and is exactly what the panel's own docs
    # promise). Frame video and the local music library are OUT OF SCOPE for
    # the A19 gate; that is a stated delta, not a failure.
    set_env_key "$env_out" MEDIA_SHARE_UNC "//vmtest-no-samba.invalid/library"

    # THE KIOSK COMMAND. `--disable-gpu` is the sim delta: Hyper-V's hyperv_drm
    # exposes /dev/dri/card1 with NO renderD* node, so hardware GL has nothing
    # to bind to. wall-kiosk.sh word-splits WALL_APP_CMD and tests only its
    # FIRST word with [ -x ], so flags ride along without a second knob and
    # without repackaging (packaging.md §2.3).
    set_env_key "$env_out" WALL_APP_CMD "/opt/wall-panel/app/wall-shell ${WALL_SIM_APP_FLAGS:---disable-gpu}"

    # WALL_DISABLE_INPUT: EMPTY, deliberately. Quirk 3 disables the internal
    # keyboard/touchpad because they face the wall mount. A VM's synthetic
    # keyboard and mouse are the only way to reach the console for the one-time
    # GRUB edit and for Ctrl+Alt+F2 — disabling them would lock the operator out
    # of the machine this gate exists to watch. It is already empty in the
    # example; asserted here so a future change to that default is caught.
    set_env_key "$env_out" WALL_DISABLE_INPUT ""

    apply_sim_env_overrides "$env_out" "${WALL_ENV_OVERRIDES:-}" WALL_ENV_OVERRIDES

    # Banner LAST so it survives the substitutions above and is the first thing
    # anyone reading /etc/wall-panel/wall.env on the VM sees.
    { sim_banner "$caller"; cat "$env_out"; } > "$env_out.tmp" && mv "$env_out.tmp" "$env_out"
    chmod 600 "$env_out"
    log "SIM wall.env rendered -> deploy-payload/site/wall.env (0600)"
    log "  WALL_HOST=$(sed -n 's/^WALL_HOST=//p' "$env_out" | tail -n1)  WALL_APP_CMD=$(sed -n 's/^WALL_APP_CMD=//p' "$env_out" | tail -n1)"
}
