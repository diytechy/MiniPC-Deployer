#!/usr/bin/env bash
# First-boot bring-up for the AWOW always-on core. Invoked once by
# homehub-firstboot.service after docker + network are up. Idempotent and loud.
#
# Steps ("flash → boot → everything up, zero clicks"):
#   1. Sanity: stack dir + .env exist and .env has been filled (not placeholders).
#   2. Materialize the oauth2-proxy allow-list from OAUTH2_PROXY_ALLOWED_EMAILS.
#   3. Q10.9 B+ ALL-IMAGES: docker-load EVERY stack image from the baked deploy
#      payload (deploy-payload/images/*.tar) so the box comes up "from infancy"
#      with zero registry dependency, versions pinned to the sim-validated set.
#      docker load is idempotent. If no payload is present, fall back LOUDLY to
#      the old pull-at-compose-up behaviour.
#   4. `docker compose up -d` — starts all services (images already loaded in step
#      3; any not baked are pulled here) with restart:unless-stopped.
#   5. Wait for Technitium to be healthy, then run provision-technitium.sh
#      (zero-touch DNS: zone, split-horizon records, forwarders, blocklists);
#      then provision-actual.sh (A9d: set the minted Actual server password
#      on the un-bootstrapped server — idempotent no-op afterwards).
#   6. Point the HOST resolver at the local Technitium so the box itself uses it.
#   7. Stamp .provisioned.
#
# Re-running is safe: compose is declarative, provisioning is idempotent.
set -euo pipefail

STACK_DIR="/opt/homehub/stack"
MARKER="/opt/homehub/.provisioned"
log() { echo "[firstboot] $*"; }

cd "$STACK_DIR" || { log "FATAL: $STACK_DIR missing"; exit 1; }

# ── 1. sanity ────────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    log "FATAL: $STACK_DIR/.env missing (autoinstall should have seeded it)"; exit 1
fi
if grep -q "REPLACE_WITH" .env; then
    log "WARNING: .env still contains REPLACE_WITH placeholders."
    log "The stack will start but TLS/auth/DNS may be wrong until you edit .env"
    log "and re-run: sudo /usr/local/sbin/homehub-firstboot.sh"
fi

# ── 2. oauth2-proxy allow-list (Q10.5) ───────────────────────────────────────
# Materialize authenticated-emails.txt (one account per line) from the
# comma/space-separated OAUTH2_PROXY_ALLOWED_EMAILS in .env. Gitignored output.
# DO NOT `source` .env. This used to be `set -a; . ./.env; set +a` and it is
# how the first successful install died, one second into first boot:
#
#     ./.env: line 43: $2: unbound variable
#
# .env is a docker-compose env file, NOT a shell script — every value is
# literal text. Sourcing it makes bash expand that text, and EVERY basic_auth
# hash here is bcrypt, i.e. starts with `$2a$14$`. Under `set -u` (line 23)
# the unbound `$2` aborts the script outright.
#
# Turning off `set -u` would be the WRONG fix and strictly more dangerous:
# `$2` and `$1` would expand to nothing, the hash would be SILENTLY corrupted,
# Caddy would reject every login, and nothing anywhere would say why. The hard
# failure was the good outcome.
#
# Compose reads .env itself, without shell expansion, so nothing else in this
# script needs it — OAUTH2_PROXY_ALLOWED_EMAILS is the only value used here.
# Read that one literally. Bonus: no longer exports every secret in the file
# into this process's environment as a side effect.
env_value() {
    local __v
    __v=$(sed -n "s/^[[:space:]]*$1[[:space:]]*=//p" .env | tail -n1)
    case "$__v" in
        \"*\") __v=${__v#\"}; __v=${__v%\"} ;;
        \'*\') __v=${__v#\'}; __v=${__v%\'} ;;
        # UNQUOTED: strip a trailing ` # comment`, as shell sourcing and
        # compose's own .env parser both do. `.env` documents values inline
        # (LAN_IP=0.0.0.0   # VMTEST: …), and keeping the comment made the
        # value unusable. A `#` with no space before it stays — it may be
        # part of a password.
        *) __v=${__v%%[[:space:]]#*}
           __v=${__v%"${__v##*[![:space:]]}"} ;;
    esac
    # Compose stores a literal '$' as '$$' because it interpolates .env values.
    # Collapse it back so this script sees what the containers receive.
    __v=${__v//\$\$/\$}
    printf '%s' "$__v"
}

ALLOWED_EMAILS="$(env_value OAUTH2_PROXY_ALLOWED_EMAILS)"
EMAILS_FILE="$STACK_DIR/oauth2-proxy/authenticated-emails.txt"
if [ -n "$ALLOWED_EMAILS" ]; then
    mkdir -p "$STACK_DIR/oauth2-proxy"
    printf '%s' "$ALLOWED_EMAILS" | tr ', ' '\n\n' | sed '/^$/d' > "$EMAILS_FILE"
    log "oauth2-proxy allow-list written ($(wc -l < "$EMAILS_FILE") account(s))"
elif [ ! -f "$EMAILS_FILE" ]; then
    log "WARNING: no OAUTH2_PROXY_ALLOWED_EMAILS set and no allow-list file — the"
    log "tracker will reject every sign-in until you populate it."
    : > "$EMAILS_FILE"   # empty file so the bind-mount is a file, not a dir
fi

# ── 3. load the baked image payload (Q10.9 B+ ALL-IMAGES) ─────────────────────
# A freshly-imaged AWOW carries every stack image as a docker-save tar in the
# deploy payload (built by vmtest/export-images.sh, versions pinned to the
# sim-validated set), so first boot needs ZERO registry/internet access for
# container images — including naglight:local, which has no registry home at all
# (Q10.2). `docker load` is idempotent: loading an image that already exists is a
# no-op. The tars ride inside deploy-payload/, so they land wherever the payload
# lands; check every layout the light + repacked ISO paths can produce.
# GRACEFUL DEGRADE: if NO payload is present (e.g. a seed built without images),
# log loudly and fall through to the pre-Q10.9 pull-at-compose-up behaviour.
shopt -s nullglob
IMAGES_DIR=""
for cand in \
    /opt/homehub/images \
    "$STACK_DIR/images" \
    /cdrom/deploy-payload/images \
    /media/deploy-payload/images; do
    tars=("$cand"/*.tar "$cand"/*.tar.zst)
    if [ -d "$cand" ] && [ "${#tars[@]}" -gt 0 ]; then
        IMAGES_DIR="$cand"; break
    fi
done

if [ -n "$IMAGES_DIR" ]; then
    tars=("$IMAGES_DIR"/*.tar "$IMAGES_DIR"/*.tar.zst)
    log "loading ${#tars[@]} baked image tar(s) from $IMAGES_DIR (Q10.9 B+ — zero-registry first boot)"
    loaded=0
    for tar in "${tars[@]}"; do
        name="$(basename "$tar")"
        case "$tar" in
            *.tar.zst)
                if command -v zstd >/dev/null 2>&1; then
                    if zstd -dc "$tar" | docker load 2>&1 | sed 's/^/[firstboot]   /'; then
                        loaded=$((loaded + 1))
                    else
                        log "WARNING: docker load failed for $name — compose will try to PULL this image instead"
                    fi
                else
                    log "WARNING: $name is zstd-compressed but zstd is not installed — skipping; compose will PULL this image"
                fi
                ;;
            *)
                if docker load -i "$tar" 2>&1 | sed 's/^/[firstboot]   /'; then
                    loaded=$((loaded + 1))
                else
                    log "WARNING: docker load failed for $name — compose will try to PULL this image instead"
                fi
                ;;
        esac
    done
    log "image payload: $loaded of ${#tars[@]} tar(s) loaded from $IMAGES_DIR"
else
    log "NOTICE: no baked image payload found (looked in /opt/homehub/images,"
    log "  $STACK_DIR/images, /cdrom/deploy-payload/images, /media/deploy-payload/images)."
    log "  Falling back to PULL-AT-COMPOSE-UP — 'docker compose up -d' fetches each image"
    log "  from its registry (needs internet). Pre-Q10.9-B+ behaviour; expected ONLY for a"
    log "  seed built WITHOUT vmtest/export-images.sh. naglight:local has no registry home,"
    log "  so the tracker will fail to start unless that image was staged some other way."
fi
shopt -u nullglob

# ── 3b. mount the storage-map data drives BEFORE anything bind-mounts into them ─
# ORDER IS LOAD-BEARING — this used to sit in step 5c, AFTER `docker compose
# up -d`, and that was a latent data-loss bug the moment any container bind-
# mounted a path under /srv/library:
#   docker CREATES a missing bind-mount source directory, on whatever filesystem
#   is there at container-start time. With the library unmounted that is the
#   SYSTEM disk; ntfs3 then mounts over /srv/library and SHADOWS it. The
#   container keeps writing to the now-invisible directory on the 119 GB system
#   disk, and every read through the share sees an empty library. Silent, and it
#   only surfaces when the system disk fills.
# It was harmless while MEDIA_ROOT defaulted to /srv/media and nothing mounted
# inside the library; enabling the media profiles (immich writes ${MEDIA_ROOT}/
# immich, jellyfin reads the video library) makes it live. Same reasoning the
# Samba block below already states for `root preexec` — it just has to happen
# before compose, not after.
#
# Not fatal when a drive is absent: `nofail` in the generated fstab options, and
# provision-mounts.sh exits 0 with a loud log on a sim/vmtest build that carries
# no fragment at all.
log "mounting storage-map data drives (before compose — bind sources must be real)…"
bash "$STACK_DIR/provision/provision-mounts.sh" || \
    log "WARN: drive mounting reported a problem — see above"

# ── 3c. hardware-conditional compose overrides ───────────────────────────────
# Jellyfin's Quick Sync passthrough needs /dev/dri, and a `devices:` entry for a
# node that does not exist makes container CREATION fail outright — which would
# take the whole `docker compose up -d` down with it on any box without an iGPU.
# Generated here instead of being hardcoded in docker-compose.yml, so one stack
# file works on every box.
#   Note (measured 2026-08-01): a Hyper-V VM is NOT such a box — hyperv_drm
#   gives it /dev/dri with a card node but no renderD*, so the override is
#   written there too and simply has no VA-API to offer. Do not read "the gate
#   VM passed" as proof the no-device branch works; that has its own test.
bash "$STACK_DIR/provision/provision-compose-overrides.sh" || \
    log "WARN: compose override generation failed — Jellyfin (if enabled) runs without QSV"

# ── 4. bring the stack up ────────────────────────────────────────────────────
# Images were loaded from the payload in step 3 (Q10.9 B+). compose finds each
# pinned tag locally and starts it without a pull; anything NOT baked (or a
# no-payload fallback) is pulled here — which for naglight:local (no registry,
# Q10.2) means it must have been built/staged first.
log "docker compose up -d…"
docker compose up -d

# ── 5. Technitium zero-touch provisioning ────────────────────────────────────
log "waiting for Technitium API on :5380…"
for i in $(seq 1 60); do
    if curl -fsS -o /dev/null http://127.0.0.1:5380/ 2>/dev/null; then break; fi
    sleep 2
done
log "provisioning Technitium (zone + split-horizon + forwarders + blocklists)…"
bash "$STACK_DIR/provision/provision-technitium.sh" --env "$STACK_DIR/.env"

# ── 5b. Actual zero-touch bootstrap (A9d) ────────────────────────────────────
# Sets the minted server password on the un-bootstrapped Actual so no UI step
# stands between first boot and a working Finance-Auditor. Idempotent: an
# already-bootstrapped server is a logged no-op.
log "bootstrapping Actual (server password from FINANCE_ACTUAL_PASSWORD)…"
bash "$STACK_DIR/provision/provision-actual.sh" --env "$STACK_DIR/.env"

# ── 5c. The Samba file server (A14 + A13) ────────────────────────────────────
# ORDER IS LOAD-BEARING:
#   mounts  -> the library must be a real mountpoint before anything exports a
#              path inside it, or Samba serves an empty dir on the eMMC and
#              silently accepts writes to the system disk. MOVED UP to step 3b:
#              docker bind mounts need the same guarantee and compose runs in
#              step 4, so mounting here was already too late for them.
#   samba   -> assembles smb.conf (tracked [global] + generated share stanzas)
#              and starts smbd. Creates the Samba databases smbpasswd needs.
#   users   -> one identity per storage-map §2 entry, each with its OWN
#              password, which is what makes §3's per-share ACLs real.
# All three are no-ops when their site files were not shipped (sim/vmtest).
# The mount guard must exist BEFORE the shares reference it: every share
# stanza names it as `root preexec`, and a missing guard would make Samba
# refuse every connection (preexec close = yes treats "cannot run" as failure).
install -m 0755 "$STACK_DIR/samba/library-guard.sh" /usr/local/sbin/homehub-library-guard
install -m 0644 "$STACK_DIR/samba/homehub-library-health.service" /etc/systemd/system/homehub-library-health.service
install -m 0644 "$STACK_DIR/samba/homehub-library-health.timer"   /etc/systemd/system/homehub-library-health.timer
# Same guard, second drive (A21): the backup target had no presence check at all
# — it was looked at once a night by the backup run, which (fstab `nofail`) could
# not tell an absent drive from an empty directory on the system disk.
install -m 0644 "$STACK_DIR/samba/homehub-backup-drive-health.service" /etc/systemd/system/homehub-backup-drive-health.service
install -m 0644 "$STACK_DIR/samba/homehub-backup-drive-health.timer"   /etc/systemd/system/homehub-backup-drive-health.timer
systemctl daemon-reload

if [ -f /etc/homehub-samba/smb.conf.fragment ]; then
    log "bringing up the Samba file server…"
    if bash "$STACK_DIR/provision/provision-samba.sh"; then
        log "provisioning Samba household accounts…"
        bash "$STACK_DIR/provision/provision-samba-users.sh" || \
            log "WARN: Samba account provisioning failed — private shares will be unreachable"
    else
        log "WARN: Samba server did not come up — every §3 share is unreachable. Accounts skipped."
    fi
elif [ -f /etc/homehub-samba/.site-present ]; then
    # The USB DID carry a site/ payload, but the fragment did not survive the
    # install. That is a broken production stick, not a sim build - and the
    # reassuring "expected on sim builds" message below would be a lie.
    log "FATAL: site payload was present but /etc/homehub-samba/smb.conf.fragment is missing."
    log "  Every §3 share is unreachable. The late-command copy failed or the"
    log "  payload was incomplete. Rebuild with Build-VentoyStick.ps1 and re-image."
else
    log "no /etc/homehub-samba/smb.conf.fragment and no site payload marker —"
    log "  skipping the file server (A14). Expected ONLY on a sim/vmtest build."
fi

# Enable the health timer AFTER mounting, not before: OnBootSec=3min has long
# elapsed by the time firstboot runs, so `enable --now` fires immediately and
# would post a spurious library-mounted ok=false against an unmounted library.
systemctl enable --now homehub-library-health.timer >/dev/null 2>&1 ||     log "WARN: could not enable homehub-library-health.timer — a vanished drive would go unreported"
systemctl enable --now homehub-backup-drive-health.timer >/dev/null 2>&1 || \
    log "WARN: could not enable homehub-backup-drive-health.timer — an absent backup drive would go unreported until the nightly run"

# ── 6. make the host itself use local DNS ────────────────────────────────────
# systemd-resolved: point it at 127.0.0.1 so the box resolves its own zone.
if systemctl is-active --quiet systemd-resolved; then
    mkdir -p /etc/systemd/resolved.conf.d
    cat > /etc/systemd/resolved.conf.d/homehub.conf <<'EOF'
[Resolve]
DNS=127.0.0.1
Domains=~.
EOF
    systemctl restart systemd-resolved || true
    log "host resolver pointed at local Technitium"
fi

# ── 7. done ──────────────────────────────────────────────────────────────────
date > "$MARKER"
log "bring-up complete. Verify with: bash $STACK_DIR/provision/healthcheck.sh"
