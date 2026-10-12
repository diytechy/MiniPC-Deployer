#!/usr/bin/env bash
# ensure-ai-account.sh - make sure the dedicated AI service account exists and
# is still unprivileged. Extracted from setup-ai-cli.sh so that every script
# that needs the account (firstboot, setup-ai-cli.sh, setup-ai-usage.sh, the
# running-hub deploy) creates it the SAME way instead of refusing when it is
# absent (plan 3.4, Owner 2026-10-11: the box provisions what its configuration
# needs; only the logins stay human).
#
# THE ACCOUNT: unprivileged, no sudo, not in docker (sudo by another door), a
# real shell (the vendor sign-ins are run as it, in a terminal). Never `hub`,
# never root. Idempotent; the privilege checks run on EVERY call because an
# account created without sudo can be given it later.
#
# Usage:
#   ensure-ai-account.sh <name>
#   ensure-ai-account.sh --effective <ai-cli-enabled> <ai-cli-user> <ai-usage-account>
#       prints the ONE account both consumers run as: <ai-cli-user> when
#       ai-cli-enabled is "true", else <ai-usage-account> (blank -> homehub-ai).
#       Creates nothing; it is the single home of the rule.
#
# Exit 0 = account ready. 1 = failed. 2 = refused (forbidden name / privilege).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_ACCOUNT=homehub-ai

log() { printf 'ensure-ai-account: %s\n' "$*"; }
die() { printf 'ensure-ai-account: FAILED: %s\n' "$*" >&2; exit 1; }
refuse() { printf 'ensure-ai-account: REFUSED: %s\n' "$*" >&2; exit 2; }

# One truthiness rule for the whole stack: strip ONE matched pair of quotes, then
# compare EXACTLY (`true`; `True` is not true). Callers may pass raw .env values.
unquote() {
    local v="${1%$'\r'}"
    case "$v" in
        \"*\") v="${v#\"}"; v="${v%\"}" ;;
        \'*\') v="${v#\'}"; v="${v%\'}" ;;
    esac
    printf '%s' "$v"
}

if [ "${1:-}" = "--effective" ]; then
    if [ "$(unquote "${2:-}")" = "true" ]; then
        name="$(unquote "${3:-}")"
    else
        name="$(unquote "${4:-}")"
    fi
    printf '%s\n' "${name:-$DEFAULT_ACCOUNT}"
    exit 0
fi

NAME="${1:-}"

case "$NAME" in
    hub|root|"")
        refuse "account ${NAME:-<empty>} - this service must not run as an \
account carrying (ALL) NOPASSWD: ALL (A40, 2026-09-09)" ;;
esac
# The name reaches useradd and a path; keep it to what a login name is.
case "$NAME" in
    [!a-z_]*|*[!a-z0-9_-]*) refuse "account name '$NAME' is not a plain login name" ;;
esac

if id -u "$NAME" >/dev/null 2>&1; then
    log "account $NAME already exists"
else
    [ "$(id -u)" = 0 ] || die "must run as root to create the service account $NAME"
    # A real shell and its own home, where the CLI credentials land at 0600.
    # NOT in sudo, NOT in docker, NOT in adm.
    useradd --create-home --shell /bin/bash --comment "HomeHub AI CLI service" \
        "$NAME" || die "useradd $NAME"
    log "created $NAME (no sudo, no docker)"
fi
HOME_DIR="$(getent passwd "$NAME" 2>/dev/null | cut -d: -f6 || true)"
chmod 0700 "${HOME_DIR:-/home/$NAME}" 2>/dev/null || true

groups_now="$(id -nG "$NAME" 2>/dev/null || true)"
for bad in sudo admin wheel root docker adm; do
    case " $groups_now " in
        *" $bad "*) refuse "$NAME is in group '$bad' - a dedicated account that \
was then given sudo (or docker) is not a containment" ;;
    esac
done

# The sudoers search is the service's, not a second grep: it FOLLOWS #include /
# #includedir / @include, so a NOPASSWD grant one file away is not invisible.
AI_CLI_HERE="$HERE" python3 - "$NAME" <<'PY' || refuse "$NAME is named in (or reachable from) a sudoers rule"
import sys, os
sys.path.insert(0, os.environ["AI_CLI_HERE"])
import ai_cli_service as svc
try:
    svc.assert_service_account(sys.argv[1], sudoers_lines=svc.collect_sudoers_lines())
except svc.ConfigError as exc:
    sys.stderr.write("ensure-ai-account: %s\n" % exc)
    sys.exit(1)
PY

log "account $NAME is ready (unprivileged, no sudo, no docker)"
exit 0
