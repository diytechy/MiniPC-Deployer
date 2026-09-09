#!/usr/bin/env bash
# setup-ai-cli.sh — create the dedicated account and install the AI CLI service.
#
# Idempotent and non-interactive, like every other setup script here. It is
# called from firstboot only when AI_CLI_ENABLED=true; on a box where the knob
# is false it does nothing at all, and nothing it would have installed exists.
#
# WHAT THIS SCRIPT IS FOR, in one line: acceptance criterion 1 of A40 —
# a DEDICATED UNPRIVILEGED ACCOUNT, never `hub`.
#
# `hub` is the box's only account with a real shell and it carries
# (ALL) NOPASSWD: ALL (stack/remote-ui/homehub-desktop-session.sh). A service
# running as `hub`, driving an agent, taking requests from other containers is
# arbitrary code execution as a passwordless-sudo user chosen by the caller.
# So: a new account, no sudo, not in `docker` (which is sudo by another door),
# and — per A40 — THIS is the account the CLIs are signed into over RDP, so the
# subscription credential lives where the service runs and nowhere else.
#
# The account keeps a real shell on purpose. It has to: `claude setup-token`
# is run inside an RDP session as this user, and a nologin shell would make
# that impossible. A shell is not a privilege; sudo is, and this account has
# none. The script REFUSES to proceed if the account has picked one up.
#
# Adds NO apt package: the service is Python 3 stdlib only and python3 is
# already in packages.list. The CLIs themselves are not apt packages either
# (SN-016) and are installed over SSH by the Owner, deliberately outside the
# offline closure.
#
# Usage: setup-ai-cli.sh [--check-only]
# Exit 0 = converged (or disabled). 1 = failed. 2 = refused (privilege found).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${AI_CLI_ENV_FILE:-$HERE/../.env}"
CHECK_ONLY=0
[ "${1:-}" = "--check-only" ] && CHECK_ONLY=1

# Defaults mirror stack/.env.example; the .env wins where it sets a key.
AI_CLI_ENABLED="${AI_CLI_ENABLED:-false}"
AI_CLI_USER="${AI_CLI_USER:-homehub-ai}"
AI_CLI_BIND="${AI_CLI_BIND:-127.0.0.1}"
AI_CLI_SCRATCH_ROOT="${AI_CLI_SCRATCH_ROOT:-/var/lib/homehub-ai/scratch}"

if [ -f "$ENV_FILE" ]; then
    # Read only the keys we own, and never `source` a file that may hold
    # secrets into this shell's environment wholesale.
    for key in AI_CLI_ENABLED AI_CLI_USER AI_CLI_BIND AI_CLI_SCRATCH_ROOT; do
        value="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
        [ -n "$value" ] && printf -v "$key" '%s' "$value"
    done
fi

log() { printf 'setup-ai-cli: %s\n' "$*"; }
die() { printf 'setup-ai-cli: FAILED: %s\n' "$*" >&2; exit 1; }
refuse() { printf 'setup-ai-cli: REFUSED: %s\n' "$*" >&2; exit 2; }

if [ "$AI_CLI_ENABLED" != "true" ]; then
    log "AI_CLI_ENABLED=$AI_CLI_ENABLED — nothing installed, nothing running."
    exit 0
fi

# ── Refusal 1: never `hub`, never root. ────────────────────────────────────
case "$AI_CLI_USER" in
    hub|root|"")
        refuse "AI_CLI_USER=${AI_CLI_USER:-<empty>} — this service must not run \
as an account carrying (ALL) NOPASSWD: ALL (A40, 2026-09-09)" ;;
esac

# ── Refusal 2: the bind address, before anything is installed. ─────────────
python3 - "$AI_CLI_BIND" <<'PY' || refuse "AI_CLI_BIND is not loopback or the docker bridge"
import ipaddress, sys
try:
    addr = ipaddress.ip_address(sys.argv[1].strip())
except ValueError:
    sys.exit(1)
if addr.is_unspecified:
    sys.exit(1)
if addr.is_loopback or addr in ipaddress.ip_network("172.16.0.0/12"):
    sys.exit(0)
sys.exit(1)
PY

if [ "$CHECK_ONLY" = 1 ]; then
    log "check-only: user=$AI_CLI_USER bind=$AI_CLI_BIND — both acceptable."
    exit 0
fi

[ "$(id -u)" = 0 ] || die "must run as root to create the service account"

# ── Create the account, idempotently. ──────────────────────────────────────
if id -u "$AI_CLI_USER" >/dev/null 2>&1; then
    log "account $AI_CLI_USER already exists"
else
    # A real shell (RDP sign-in needs one) and its own home, where the CLI
    # credential will land at 0600 after `claude setup-token`. NOT in sudo,
    # NOT in docker, NOT in adm.
    useradd --create-home --shell /bin/bash --comment "HomeHub AI CLI service" \
        "$AI_CLI_USER" || die "useradd $AI_CLI_USER"
    log "created $AI_CLI_USER (no sudo, no docker)"
fi
chmod 0700 "/home/$AI_CLI_USER" 2>/dev/null || true

# ── Refusal 3: the account must still be unprivileged. Re-checked EVERY run,
# because an account created without sudo can be given it later, and this is
# the thing the block is about.
groups_now="$(id -nG "$AI_CLI_USER" 2>/dev/null || true)"
for bad in sudo admin wheel root docker adm; do
    case " $groups_now " in
        *" $bad "*) refuse "$AI_CLI_USER is in group '$bad' — a dedicated \
account that was then given sudo (or docker) is not a containment" ;;
    esac
done
if grep -RIlqs -E "(^|[^A-Za-z0-9_-])%?${AI_CLI_USER}([^A-Za-z0-9_-]|$)" \
        /etc/sudoers /etc/sudoers.d 2>/dev/null; then
    refuse "$AI_CLI_USER is named in a sudoers rule"
fi

# ── The scratch root: 0700, owned by the account, and empty at install. ────
install -d -m 0700 -o "$AI_CLI_USER" -g "$AI_CLI_USER" "$AI_CLI_SCRATCH_ROOT"
log "scratch root $AI_CLI_SCRATCH_ROOT is 0700 $AI_CLI_USER"

# ── The unit. Installed, enabled, and NOT started here: starting it is a
# deployment step the Owner runs, and this script also runs on every boot.
install -m 0644 "$HERE/homehub-ai-cli.service" /etc/systemd/system/homehub-ai-cli.service
systemctl daemon-reload
systemctl enable homehub-ai-cli.service >/dev/null

# ── The configuration check, as the service itself sees it. Fails loudly
# rather than leaving a unit that will crash-loop at the next boot.
AI_CLI_BIND="$AI_CLI_BIND" AI_CLI_USER="$AI_CLI_USER" \
    AI_CLI_SCRATCH_ROOT="$AI_CLI_SCRATCH_ROOT" \
    python3 "$HERE/ai_cli_service.py" --check || die "ai_cli_service.py --check refused"

log "converged. The Owner still has to sign the CLIs in as $AI_CLI_USER over RDP"
log "(claude setup-token; the credential lands in ~$AI_CLI_USER/.claude at 0600)"
