#!/usr/bin/env bash
# vmtest/lib/common.sh — shared helpers for the WI-10.18 V3 gate scripts.
# Sourced by build-seed.sh and build-repacked-iso.sh. Not standalone.

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
    local ssh_key="$out_dir/ssh/homehub-vmtest-ed25519"
    if [ ! -f "$ssh_key" ]; then
        log "generating ephemeral SSH keypair for the test VM (not reused anywhere else)"
        ssh-keygen -q -t ed25519 -N "" -C "homehub-vmtest (disposable, WI-10.18 V3 gate)" -f "$ssh_key"
    else
        log "reusing existing ephemeral SSH keypair at $ssh_key (pass --clean to regenerate)"
    fi
    SSH_KEY="$ssh_key"   # exported for the caller
    local ssh_pubkey; ssh_pubkey="$(cat "$ssh_key.pub")"

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
    rm -rf "$payload_dir"
    mkdir -p "$payload_dir"
    log "copying repo into deploy-payload/ (excludes .git, vmtest/.out)"
    ( cd "$repo_root" && tar -c --exclude=.git --exclude=vmtest/.out . ) | ( cd "$payload_dir" && tar -x )

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
                 smb.conf.fragment library-mounts.fstab; do
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

    assert_env_interpolation_safe "$sim_env"
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
