# shellcheck shell=bash
# vmtest/lib/kids.sh — the KIDS KIOSK image's half of the build (SR-138).
# Sourced after lib/common.sh by build-repacked-iso.sh; uses its log/die,
# ensure_sim_ssh_key, sim_banner, validate_autoinstall_yaml, assert_storage_pin
# and copy_repo_into_payload.
#
# Kept out of common.sh on purpose: the hub and wall halves there are 2,800
# lines of their own history, and the kids image shares the mechanisms (called
# from here), not the special cases.

# render_kids_seed_tree REPO_ROOT OUT_DIR CALLER — user-data, meta-data and the
# deploy payload for the kids kiosk. Implements: LLR-1419.
#
# PRODUCTION when KIDS_SITE_DIR is set: its user-data.filled (HomeHub's
# Materialize-Deploy.ps1 -Image kids) is taken verbatim and guarded, and its
# kids.env, if present, is staged. SIM otherwise: a throwaway SSH key and
# console password, the hostname marked -vmtest, the disk pin swapped for
# Hyper-V's Virtual_Disk and the Wi-Fi block for a DHCP ethernet.
render_kids_seed_tree() {
    local repo_root="$1" out_dir="$2" caller="$3"
    local src="$repo_root/stack/autoinstall/kids"

    [ -f "$src/user-data" ] || die "not found: $src/user-data (run from a MiniPC-Deployer checkout)"
    [ -f "$src/meta-data" ] || die "not found: $src/meta-data"
    [ -f "$src/kids.env.example" ] || die "not found: $src/kids.env.example"
    require_cmd openssl "Install with: sudo apt-get install -y openssl"

    KIDS_BUILD_KIND=sim   # exported for the caller's summary
    if [ -n "${KIDS_SITE_DIR:-}" ]; then
        [ -d "$KIDS_SITE_DIR" ] || die "KIDS_SITE_DIR=$KIDS_SITE_DIR is not a directory"
        [ -f "$KIDS_SITE_DIR/user-data.filled" ] || \
            die "KIDS_SITE_DIR=$KIDS_SITE_DIR has no user-data.filled - refusing to stage a production" \
                "kids.env beside a SIM-substituted user-data. Run: Materialize-Deploy.ps1 -Image kids (HomeHub)"
        KIDS_BUILD_KIND=production
    fi

    [ "${CLEAN:-0}" -eq 1 ] && wipe_build_dir "$out_dir"
    mark_build_dir "$out_dir"
    mkdir -p "$out_dir/ssh" "$out_dir/iso-root/deploy-payload" "$out_dir/secrets"
    chmod 700 "$out_dir/ssh" "$out_dir/secrets"
    local user_data_out="$out_dir/iso-root/user-data"

    if [ "$KIDS_BUILD_KIND" = production ]; then
        log "PRODUCTION: using $KIDS_SITE_DIR/user-data.filled (real key, Wi-Fi and disk pin; no sim substitution)"
        SSH_KEY=""; CREDS_FILE=""
        cp "$KIDS_SITE_DIR/user-data.filled" "$user_data_out"
        # Anchored to ACTIVE settings: the tracked comments quote placeholders.
        grep -Eq '^[[:space:]]*[A-Za-z_"-][^#]*REPLACE_WITH' "$user_data_out" && \
            die "user-data.filled has an ACTIVE setting still holding a REPLACE_WITH placeholder - re-run Materialize-Deploy.ps1 -Image kids."
        # PARSED, not grepped: YAML reads True, yes and on as true too, and a
        # grep for one spelling is a guard with three holes (independent review).
        python3 - "$user_data_out" <<'PY' || die "user-data.filled does not set ssh.allow-pw to false - the kiosk is SSH-key-only. Refusing to bake it."
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
ssh = (doc.get("autoinstall", doc) or {}).get("ssh") or {}
sys.exit(0 if ssh.get("allow-pw") is False else 1)
PY
        grep -Eq '^[[:space:]]+(path|serial|model|wwn):' "$user_data_out" || \
            die "user-data.filled has no storage.layout match: - the unattended wipe would pick a disk by heuristic."
        grep -Eq '^[[:space:]]+model:[[:space:]]*Virtual_Disk' "$user_data_out" && \
            die "user-data.filled pins model: Virtual_Disk - the SIM pin, which matches nothing on the spare mini-PC."
        grep -Eq '^[[:space:]]*wifis:' "$user_data_out" || \
            die "user-data.filled declares no wifis: - the kiosk sits on Wi-Fi and would come up with NO network."
    else
        ensure_sim_ssh_key "$out_dir"
        local ssh_pubkey; ssh_pubkey="$(cat "$SSH_KEY.pub")"
        local creds_file="$out_dir/secrets/creds.env" sim_password sim_password_hash
        # shellcheck disable=SC2034  # read by the caller (build-repacked-iso.sh)
        CREDS_FILE="$creds_file"
        if [ ! -f "$creds_file" ]; then
            sim_password="kidsvm-$(openssl rand -hex 6)"
            sim_password_hash="$(openssl passwd -6 "$sim_password")"
            { sim_banner "$caller"
              echo "SIM_CONSOLE_PASSWORD=$sim_password"
              echo "SIM_CONSOLE_PASSWORD_HASH=$sim_password_hash"; } > "$creds_file"
            chmod 600 "$creds_file"
        else
            log "reusing existing SIM secrets at $creds_file (pass --clean to regenerate)"
            sim_password_hash="$(grep '^SIM_CONSOLE_PASSWORD_HASH=' "$creds_file" | cut -d= -f2-)"
        fi

        sed \
            -e "s#- \"ssh-ed25519 AAAA_REPLACE_WITH_YOUR_PUBLIC_KEY you@host\"#- \"$ssh_pubkey\"#" \
            -e 's/allow-pw: false/allow-pw: true   # VMTEST ONLY - the real kiosk is key-only/' \
            -e "s|password: \"!\"|password: \"$sim_password_hash\"   # VMTEST ONLY sim password, see the builder's secrets/creds.env|" \
            -e 's/hostname: kids-kiosk$/hostname: kids-kiosk-vmtest/' \
            -e 's/realname: "Kids Kiosk"/realname: "Kids Kiosk VM Test"/' \
            -e 's/^\([[:space:]]*\)timezone: REPLACE_WITH_TIMEZONE/\1timezone: Etc\/UTC/' \
            -e 's|^\([[:space:]]*\)model: FPT310M8SSD128G.*|\1model: Virtual_Disk|' \
            "$src/user-data" > "$user_data_out"

        # Wi-Fi -> a DHCP ethernet: Hyper-V cannot emulate a radio. A sim run
        # therefore proves nothing about the Wi-Fi path (stated in the README).
        awk '
            /^    wifis:/ {
                print "    ethernets:"
                print "      any-eth:"
                print "        match:"
                print "          name: \"e*\""
                print "        dhcp4: true"
                skip = 1; next
            }
            skip && /^  [^ ]/ { skip = 0 }
            !skip
        ' "$user_data_out" > "$user_data_out.tmp" && mv "$user_data_out.tmp" "$user_data_out"

        # Every substitution above is a silent no-op if its source string moved.
        grep -q "REPLACE_WITH_YOUR_PUBLIC_KEY" "$user_data_out" && die "SSH placeholder substitution failed"
        grep -Eq '^[[:space:]]*hostname: kids-kiosk-vmtest$' "$user_data_out" || \
            die "hostname substitution did not apply - stack/autoinstall/kids/user-data no longer says 'hostname: kids-kiosk'. Update the sed above."
        grep -Eq '^[[:space:]]+model: Virtual_Disk$' "$user_data_out" || \
            die "vmtest storage pin was NOT applied - kids/user-data's storage match is no longer 'model: FPT310M8SSD128G'. Refusing to build a sim ISO whose disk pin is unreviewed."
        grep -Eq '^[[:space:]]*[A-Za-z_.-]+:[^#]*FPT310M8SSD128G' "$user_data_out" && \
            die "the sim user-data still names the spare mini-PC's real disk model in an active setting. Refusing to build."
        grep -Eq '^[[:space:]]+(path|serial|wwn):' "$user_data_out" && \
            die "the sim user-data carries a real-hardware disk match (path/serial/wwn). Refusing to build."
        grep -Eq '^[[:space:]]*ethernets:$' "$user_data_out" || \
            die "the wifis: -> ethernets: rewrite did not apply - kids/user-data's network block changed shape. Update the awk above."
        grep -qE '^[[:space:]]*(wifis|access-points):|REPLACE_WITH_(WIFI|HUB_IP)' "$user_data_out" && \
            die "the sim user-data still declares Wi-Fi or a hub placeholder. The awk above did not consume the whole network block."
        grep -Eq '^[[:space:]]*[A-Za-z_"-][^#]*REPLACE_WITH' "$user_data_out" && \
            die "the sim user-data still has an ACTIVE REPLACE_WITH placeholder - a substitution above is missing."
        apply_sim_lab_netplan "$user_data_out" kids
    fi

    if [ "$KIDS_BUILD_KIND" = production ] && [ -n "${SIM_LAB_ADDR:-}" ]; then
        die "SIM_LAB_* is set on a PRODUCTION kids build (KIDS_SITE_DIR=$KIDS_SITE_DIR); production" \
            "user-data.filled is taken verbatim, so these knobs would be silently discarded."
    fi

    validate_autoinstall_yaml "$user_data_out"
    assert_storage_pin "$user_data_out" "$KIDS_BUILD_KIND"

    # A FRESH instance-id per build, so cloud-init never skips first-boot work.
    local stamp iid hostname_sed='' expect_host='kids-kiosk'
    stamp="$(date +%Y%m%d%H%M%S)"
    iid="kids-kiosk-$stamp"
    if [ "$KIDS_BUILD_KIND" = sim ]; then
        iid="kids-kiosk-vmtest-$stamp"
        hostname_sed='s/local-hostname: kids-kiosk$/local-hostname: kids-kiosk-vmtest/'
        expect_host='kids-kiosk-vmtest'
    fi
    sed -e "s/instance-id: kids-kiosk-001/instance-id: $iid/" \
        ${hostname_sed:+-e "$hostname_sed"} \
        "$src/meta-data" > "$out_dir/iso-root/meta-data"
    grep -Eq "^local-hostname: ${expect_host}\$" "$out_dir/iso-root/meta-data" || \
        die "meta-data local-hostname is not '$expect_host' - stack/autoinstall/kids/meta-data changed shape."
    grep -Eq '^instance-id: kids-kiosk-(vmtest-)?[0-9]+$' "$out_dir/iso-root/meta-data" || \
        die "meta-data instance-id substitution did not apply."

    copy_repo_into_payload "$repo_root" "$out_dir/iso-root/deploy-payload"

    if [ "$KIDS_BUILD_KIND" = production ] && [ -f "$KIDS_SITE_DIR/kids.env" ]; then
        mkdir -p "$out_dir/iso-root/deploy-payload/site"
        install -m 0600 "$KIDS_SITE_DIR/kids.env" "$out_dir/iso-root/deploy-payload/site/kids.env"
        log "PRODUCTION: staged site/kids.env (late-command 4a installs it 0600 and deletes the payload copy)"
        # A warning, not a refusal: the connector name (or the monitor's make,
        # model and serial) is read off the booted box, so the FIRST stick
        # cannot know it. Unset, a TV that is on at boot in a lower-sorting
        # socket takes the kiosk until it is unplugged (independent review).
        if ! grep -Eq '^KIDS_TOUCH_OUTPUT=..*' "$KIDS_SITE_DIR/kids.env"; then
            log "WARNING: kids.env sets no KIDS_TOUCH_OUTPUT. With a TV already on at boot the kiosk may"
            log "  open on the TV. After the first boot, read the touch monitor's name with"
            log "  'swaymsg -t get_outputs' (as kiosk) and set it in the config, then rebuild or edit kids.env."
        fi
    else
        log "no site kids.env - the kiosk seeds /etc/kids-kiosk/kids.env from kids.env.example"
    fi
}

# stage_kids_app_into_payload OUT_DIR APP_OUT — put export-ottertube.sh's
# tarball where late-command 3b looks for it. Implements: LLR-1414.
#
# EXACTLY ONE tarball, or the build stops: none would be a kiosk with nothing
# to show (late-command 3b refuses it anyway, three hours later and on the
# floor next to the machine), and two would leave which one ships to `ls`.
# ALLOW_MISSING_APP=1 builds without it, loudly, to exercise the seed alone.
stage_kids_app_into_payload() {
    local out_dir="$1" app_out="$2"
    local dest="$out_dir/iso-root/deploy-payload/kids-app" found=()
    shopt -s nullglob
    found=("$app_out"/ottertube-*-linux-x64.tar.gz)
    shopt -u nullglob
    if [ "${#found[@]}" -eq 0 ]; then
        if [ "${ALLOW_MISSING_APP:-0}" -eq 1 ]; then
            log "WARNING: ALLOW_MISSING_APP=1 - building a kids ISO with NO OtterTube. Its install will REFUSE to finish (late-command 3b)."
            return 0
        fi
        die "no OtterTube tarball in $app_out. Build it first:" \
            "  bash vmtest/export-ottertube.sh --out $app_out" \
            "(or point KIDS_APP_OUT at the directory that holds it)."
    fi
    [ "${#found[@]}" -eq 1 ] || die "${#found[@]} OtterTube tarballs in $app_out - exactly one may ship: ${found[*]}"
    case "${found[0]}" in *-dirty-*) die "refusing a -dirty OtterTube build: ${found[0]}" ;; esac
    # NO LIVE PIPE INTO grep -q: the callers run under pipefail, and grep -q
    # exiting at its match SIGPIPEs whatever is still writing - tar listing a
    # 230 MB archive (a good tarball refused, 2026-11-01), or a printf of a
    # listing longer than the pipe buffer. Capture, then a here-string.
    local listing
    listing="$(tar -tvzf "${found[0]}")" || die "${found[0]} is not a readable gzip tarball"
    grep -Eq '^-rwsr-xr-x 0/0 .* app/chrome-sandbox$' <<< "$listing" \
        || die "${found[0]}: app/chrome-sandbox is not setuid root - Electron would refuse to start on the kiosk. Rebuild it with export-ottertube.sh."
    mkdir -p "$dest"
    cp "${found[0]}" "$dest/"
    log "OtterTube staged: $(basename "${found[0]}") ($(du -h "${found[0]}" | cut -f1))"
}
