#!/usr/bin/env bash
# First-boot bring-up for the AWOW always-on core. Invoked once by
# awow-firstboot.service after docker + network are up. Idempotent and loud.
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
#      (zero-touch DNS: zone, split-horizon records, forwarders, blocklists).
#   6. Point the HOST resolver at the local Technitium so the box itself uses it.
#   7. Stamp .provisioned.
#
# Re-running is safe: compose is declarative, provisioning is idempotent.
set -euo pipefail

STACK_DIR="/opt/awow-core/stack"
MARKER="/opt/awow-core/.provisioned"
log() { echo "[firstboot] $*"; }

cd "$STACK_DIR" || { log "FATAL: $STACK_DIR missing"; exit 1; }

# ── 1. sanity ────────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    log "FATAL: $STACK_DIR/.env missing (autoinstall should have seeded it)"; exit 1
fi
if grep -q "REPLACE_WITH" .env; then
    log "WARNING: .env still contains REPLACE_WITH placeholders."
    log "The stack will start but TLS/auth/DNS may be wrong until you edit .env"
    log "and re-run: sudo /usr/local/sbin/awow-firstboot.sh"
fi

# ── 2. oauth2-proxy allow-list (Q10.5) ───────────────────────────────────────
# Materialize authenticated-emails.txt (one account per line) from the
# comma/space-separated OAUTH2_PROXY_ALLOWED_EMAILS in .env. Gitignored output.
# shellcheck disable=SC1091
set -a; . ./.env; set +a
EMAILS_FILE="$STACK_DIR/oauth2-proxy/authenticated-emails.txt"
if [ -n "${OAUTH2_PROXY_ALLOWED_EMAILS:-}" ]; then
    mkdir -p "$STACK_DIR/oauth2-proxy"
    printf '%s' "$OAUTH2_PROXY_ALLOWED_EMAILS" | tr ', ' '\n\n' | sed '/^$/d' > "$EMAILS_FILE"
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
    /opt/awow-core/images \
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
    log "NOTICE: no baked image payload found (looked in /opt/awow-core/images,"
    log "  $STACK_DIR/images, /cdrom/deploy-payload/images, /media/deploy-payload/images)."
    log "  Falling back to PULL-AT-COMPOSE-UP — 'docker compose up -d' fetches each image"
    log "  from its registry (needs internet). Pre-Q10.9-B+ behaviour; expected ONLY for a"
    log "  seed built WITHOUT vmtest/export-images.sh. naglight:local has no registry home,"
    log "  so the tracker will fail to start unless that image was staged some other way."
fi
shopt -u nullglob

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

# ── 6. make the host itself use local DNS ────────────────────────────────────
# systemd-resolved: point it at 127.0.0.1 so the box resolves its own zone.
if systemctl is-active --quiet systemd-resolved; then
    mkdir -p /etc/systemd/resolved.conf.d
    cat > /etc/systemd/resolved.conf.d/awow.conf <<'EOF'
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
