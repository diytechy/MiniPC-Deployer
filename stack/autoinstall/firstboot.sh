#!/usr/bin/env bash
# First-boot bring-up for the AWOW always-on core. Invoked once by
# awow-firstboot.service after docker + network are up. Idempotent and loud.
#
# Steps ("flash → boot → everything up, zero clicks"):
#   1. Sanity: stack dir + .env exist and .env has been filled (not placeholders).
#   2. Materialize the oauth2-proxy allow-list from OAUTH2_PROXY_ALLOWED_EMAILS.
#   3. `docker compose up -d` — pulls images (tracker uses the locally-built
#      naglight:local tag), starts all services with restart:unless-stopped.
#   4. Wait for Technitium to be healthy, then run provision-technitium.sh
#      (zero-touch DNS: zone, split-horizon records, forwarders, blocklists).
#   5. Point the HOST resolver at the local Technitium so the box itself uses it.
#   6. Stamp .provisioned.
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

# ── 3. bring the stack up ────────────────────────────────────────────────────
# The tracker image (naglight:local) must be built first from a sibling NagLight
# checkout:  docker build -t naglight:local ../NagLight  (or uncomment compose's
# build: fallback). Not built here — this box may not carry the NagLight source.
log "docker compose up -d…"
docker compose up -d

# ── 4. Technitium zero-touch provisioning ────────────────────────────────────
log "waiting for Technitium API on :5380…"
for i in $(seq 1 60); do
    if curl -fsS -o /dev/null http://127.0.0.1:5380/ 2>/dev/null; then break; fi
    sleep 2
done
log "provisioning Technitium (zone + split-horizon + forwarders + blocklists)…"
bash "$STACK_DIR/provision/provision-technitium.sh" --env "$STACK_DIR/.env"

# ── 5. make the host itself use local DNS ────────────────────────────────────
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

# ── 6. done ──────────────────────────────────────────────────────────────────
date > "$MARKER"
log "bring-up complete. Verify with: bash $STACK_DIR/provision/healthcheck.sh"
