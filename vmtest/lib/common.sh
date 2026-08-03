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
# (deploy-payload/stack/...). vmtest/.out is excluded because it holds the
# build's own multi-hundred-MB output — including, since the wall target
# landed, the OfficeWallNaglight tarballs, which are staged into the payload
# deliberately by stage_wall_*_into_payload rather than swept in by this copy.
copy_repo_into_payload() {
    local repo_root="$1" payload_dir="$2"
    rm -rf "$payload_dir"
    mkdir -p "$payload_dir"
    log "copying repo into deploy-payload/ (excludes .git, vmtest/.out)"
    ( cd "$repo_root" && tar -c --exclude=.git --exclude=vmtest/.out . ) | ( cd "$payload_dir" && tar -x )
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
render_seed_tree() {
    local repo_root="$1" out_dir="$2" caller="$3"
    local autoinstall_src="$repo_root/stack/autoinstall"
    local stack_env_example="$repo_root/stack/.env.example"

    [ -f "$autoinstall_src/user-data" ] || die "not found: $autoinstall_src/user-data (run from a MiniPC-Deployer checkout)"
    [ -f "$stack_env_example" ] || die "not found: $stack_env_example"
    require_cmd openssl "Install with: sudo apt-get install -y openssl"
    require_cmd ssh-keygen "Install with: sudo apt-get install -y openssh-client"

    if [ "${CLEAN:-0}" -eq 1 ] && [ -d "$out_dir" ]; then
        log "CLEAN=1: wiping previous $out_dir"
        rm -rf "$out_dir"
    fi

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
    if [ -n "${SITE_DIR:-}" ] && [ -f "$SITE_DIR/user-data.filled" ]; then
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
    else
    sed \
        -e "s#- \"ssh-ed25519 AAAA_REPLACE_WITH_YOUR_PUBLIC_KEY you@host\"#- \"$ssh_pubkey\"#" \
        -e 's/allow-pw: false/allow-pw: true   # VMTEST ONLY - production keeps this false (key-only)/' \
        -e "s|password: \"!\"|password: \"$sim_password_hash\"   # VMTEST ONLY sim password, see vmtest/.out/secrets/creds.env|" \
        -e 's/hostname: homehub/hostname: homehub-vmtest/' \
        -e 's/realname: "Home Hub Operator"/realname: "Home Hub VM Test"/' \
        -e 's|^\([[:space:]]*\)path: /dev/nvme0n1|\1model: Virtual_Disk|' \
        "$autoinstall_src/user-data" > "$user_data_out"
    grep -q "REPLACE_WITH_YOUR_PUBLIC_KEY" "$user_data_out" && die "SSH placeholder substitution failed"

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
    # autoinstall late-command 4b installs them into /etc/homehub-samba,
    # /etc/homehub-backup and stack/.env on the target.
    # SITE_DIR set but no user-data.filled would be a SILENT DOWNGRADE: the
    # site/ files (real secrets) would still be staged while user-data fell
    # through to the sim sed above — producing a "production" stick with
    # allow-pw: true and a known sim password hash. Refuse instead of mixing.
    if [ -n "${SITE_DIR:-}" ] && [ ! -f "$SITE_DIR/user-data.filled" ]; then
        die "SITE_DIR=$SITE_DIR is set but has no user-data.filled — refusing to stage real secrets onto a SIM-substituted user-data (allow-pw would be true). Run Materialize-Deploy.ps1 -Image homehub."
    fi
    if [ -n "${SITE_DIR:-}" ] && [ -d "$SITE_DIR" ]; then
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
    case "${matches[0]}" in
        *-dirty*)
            die "refusing to bake ${matches[0]##*/} — it was built from an UNCOMMITTED tree." \
                "Its build-info.json says \"dirty\": true, so the commit it names does not describe" \
                "what is inside it and the image could never be reproduced from its own stamp." \
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
    require_cmd readelf "Install with: sudo apt-get install -y binutils"

    mkdir -p "$probe"
    log "  reading the shipped binary's DT_NEEDED list (decompresses the archive once)"
    tar -xzf "$tarball" -C "$probe" app/runtime/electron app/build-info.json app/VERSION \
        || die "$(basename "$tarball") is missing one of app/runtime/electron, app/build-info.json, app/VERSION" \
               "— that is not the packaging.md §4 layout this image is wired against."

    # The packages: list of the wall user-data, one name per line, comments off.
    local declared
    declared="$(awk '
        /^  packages:/            { inpkgs = 1; next }
        inpkgs && /^  [^ ]/       { inpkgs = 0 }
        inpkgs && /^[[:space:]]*-[[:space:]]/ {
            sub(/^[[:space:]]*-[[:space:]]*/, ""); sub(/[[:space:]]*#.*/, ""); print
        }' "$user_data")"
    [ -n "$declared" ] || die "could not read a packages: list out of $user_data"

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
        [ "$where" = "image-packages" ] || continue
        printf '%s\n' "$declared" | grep -qxF "$pkg" || missing_pkg="$missing_pkg $pkg($soname)"
    done <<EOF
$(readelf -d "$probe/app/runtime/electron" | sed -n 's/.*NEEDED.*\[\(.*\)\]/\1/p' | sort -u)
EOF

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

    mkdir -p "$dest"
    cp -f "$tarball" "$dest/"
    [ -f "$dist_dir/build-info.json" ] && cp -f "$dist_dir/build-info.json" "$dest/build-info.json"
    log "deploy-payload/wall-site/ = $(basename "$tarball") ($(( $(stat -c%s "$tarball") / 1024 )) KB)"
    log "  -> lands at /opt/homehub/wall-site/; firstboot.sh unpacks it into stack/wall-shell/."
}

# render_wall_seed_tree REPO_ROOT OUT_DIR CALLER_NAME
#
# The wall panel's equivalent of render_seed_tree: materializes
# $OUT_DIR/iso-root/{user-data,meta-data,deploy-payload/} from the REAL
# stack/autoinstall/wall/ files with SIM values substituted, plus a filled SIM
# wall.env in deploy-payload/site/.
#
# SIM-ONLY, ON PURPOSE. The hub renderer carries a production seam (SITE_DIR +
# user-data.filled from Personal's materialiser, A14). The wall has no
# materialiser output yet, so rather than build a seam nothing feeds — and risk
# the exact silent downgrade the hub's guards exist to prevent — this refuses a
# production build outright and says what would have to exist first.
#
# Containment is identical to the hub's and is the reason a sim ISO is safe to
# have lying around: the disk pin is rewritten to `model: Virtual_Disk`, which
# ONLY a Hyper-V synthetic disk reports. On real hardware it matches nothing and
# the unattended install HALTS (a match: that matches nothing is fail-safe; an
# absent one is not). The panel's own pin — `model: KINGSTON*RBU-SNS8152S3256GG2`
# — must not survive into a sim image, because a sim ISO written to a USB stick
# would then be able to wipe the real panel.
render_wall_seed_tree() {
    local repo_root="$1" out_dir="$2" caller="$3"
    local src="$repo_root/stack/autoinstall/wall"

    [ -f "$src/user-data" ] || die "not found: $src/user-data (run from a MiniPC-Deployer checkout)"
    [ -f "$src/meta-data" ] || die "not found: $src/meta-data"
    [ -f "$src/wall.env.example" ] || die "not found: $src/wall.env.example"
    require_cmd openssl "Install with: sudo apt-get install -y openssl"

    if [ -n "${WALL_SITE_DIR:-}" ]; then
        die "WALL_SITE_DIR is set, but there is no production path for the wall image yet." \
            "This builder emits SIM images ONLY. A production panel image needs (a) a" \
            "materialised user-data.filled carrying the real SSH key and the panel's REAL" \
            "disk pin, and (b) a real wall.env with the Wi-Fi PSK — neither of which any" \
            "tool emits today (Personal's materialiser covers the hub only). Building one" \
            "half of that would mix real secrets into a sim-substituted user-data, which is" \
            "precisely the silent downgrade the hub's SITE_DIR guards exist to prevent."
    fi

    if [ "${CLEAN:-0}" -eq 1 ] && [ -d "$out_dir" ]; then
        log "CLEAN=1: wiping previous $out_dir"
        rm -rf "$out_dir"
    fi

    mkdir -p "$out_dir/ssh" "$out_dir/iso-root/deploy-payload" "$out_dir/secrets"
    chmod 700 "$out_dir/ssh" "$out_dir/secrets"

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
    local user_data_out="$out_dir/iso-root/user-data"
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

    # Does Subiquity actually ACCEPT this file? (Same parse, same reasons.)
    validate_autoinstall_yaml "$user_data_out"

    # ── meta-data: fresh instance-id per build, vmtest hostname ──────────────
    sed \
        -e "s/instance-id: wall-panel-001/instance-id: wall-panel-vmtest-$(date +%Y%m%d%H%M%S)/" \
        -e 's/local-hostname: wall-panel/local-hostname: wall-panel-vmtest/' \
        "$src/meta-data" > "$out_dir/iso-root/meta-data"
    grep -Eq '^local-hostname: wall-panel-vmtest$' "$out_dir/iso-root/meta-data" || \
        die "meta-data local-hostname substitution did not apply — update the sed above."
    grep -Eq '^instance-id: wall-panel-vmtest-[0-9]+$' "$out_dir/iso-root/meta-data" || \
        die "meta-data instance-id substitution did not apply. cloud-init could treat this as a repeat instance and skip first-boot work. Update the sed above."

    copy_repo_into_payload "$repo_root" "$out_dir/iso-root/deploy-payload"

    render_sim_wall_env "$repo_root" "$out_dir/iso-root/deploy-payload" "$caller"
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
