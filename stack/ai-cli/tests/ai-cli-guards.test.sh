#!/usr/bin/env bash
# ai-cli-guards.test.sh — the four containments A40 ratified, asserted.
#
# WHY THIS SUITE EXISTS. The four acceptance criteria of the AI CLI service are
# SECURITY properties, and a security property written into a unit file and
# asserted by eye is not asserted. Each check below observes the behaviour of a
# script or the content of the tree, not a sentence in a comment.
#
# HERMETIC: a temp .env and a temp tree. No account is created, no unit is
# installed, no service is started — every path that would do that is reached
# through `--check-only` or refused before it gets there.
#
# WHAT IT LOCKS
#   A1  no `--dangerously-*` flag exists ANYWHERE in this repo's tree. This is
#       the trap: ai-template's rows carry two of them and they must never be
#       copied here. A grep over the whole tree, not over the registry, because
#       the flag could arrive in a script, a doc example or a unit file.
#   A2  the shipped unit runs as a dedicated account and NOT `hub` or `root`
#   A3  setup-ai-cli.sh REFUSES AI_CLI_USER=hub, with a reason
#   A4  setup-ai-cli.sh REFUSES a LAN bind address
#   A5  setup-ai-cli.sh REFUSES the 0.0.0.0 wildcard
#   A6  setup-ai-cli.sh ACCEPTS loopback and the docker bridge
#   A7  AI_CLI_ENABLED=false installs nothing and exits 0 (off by default)
#   A8  the service's own `--check` refuses a LAN bind
#   A9  every enabled route passes the read-only template check, via `--check`
#   A10 the unit declares the per-request scratch state directory at 0700
#   A11 `--bare` appears in no command template — it ignores the OAuth token
#       and needs an API key, so a service on the subscription cannot use it
#
# Usage: bash ai-cli-guards.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
SETUP="$DIR/setup-ai-cli.sh"
UNIT="$DIR/homehub-ai-cli.service"
SVC="$DIR/ai_cli_service.py"
for f in "$SETUP" "$UNIT" "$SVC"; do
    [ -f "$f" ] || { echo "FATAL: $f not found"; exit 2; }
done
PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || { echo "FATAL: no python3"; exit 2; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); printf '  PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$*"; }

# `setup` runs the script with a synthesised environment and returns its exit
# code, capturing output. --check-only stops before anything is created.
setup_rc() {
    local out
    out="$(AI_CLI_ENV_FILE=/nonexistent AI_CLI_ENABLED="$1" AI_CLI_USER="$2" \
        AI_CLI_BIND="$3" bash "$SETUP" --check-only 2>&1)"
    local rc=$?
    LAST_OUT="$out"
    return $rc
}

echo "── A1: no --dangerously-* flag anywhere in the tree ──"
# `git ls-files` so the check covers exactly what is committed and cannot be
# fooled by an untracked scratch file. Fall back to find where git is absent.
# The pattern is `--dangerously-[a-z]`, NOT the bare prefix: prose that forbids
# the flag writes the glob `--dangerously-*`, which can never be an argv token,
# while a real flag always continues into a name. Three files are excluded
# because each must write a real flag name to do its job - this suite and the
# pytest file build a bad row to prove it is refused, and the README quotes both
# ai-template flags to say which ones must never be copied. Nothing in those
# three is ever executed as a command.
EXCLUDE='ai-cli-guards.test.sh|tests/test_ai_cli_service.py|stack/ai-cli/README.md'
if command -v git >/dev/null 2>&1 && git -C "$REPO" rev-parse >/dev/null 2>&1; then
    hits="$(git -C "$REPO" grep -nE -e '--dangerously-[a-z]' -- . 2>/dev/null | grep -Ev "$EXCLUDE" || true)"
else
    hits="$(grep -rnE -e '--dangerously-[a-z]' "$REPO" --exclude-dir=.git 2>/dev/null | grep -Ev "$EXCLUDE" || true)"
fi
if [ -z "$hits" ]; then
    pass "A1 no --dangerously-* token in the tree"
else
    fail "A1 --dangerously-* found:"; printf '%s\n' "$hits" | sed 's/^/        /'
fi

echo
echo "── A2: the unit's account ──"
unit_user="$(grep -E '^User=' "$UNIT" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
case "$unit_user" in
    hub|root|"") fail "A2 unit User=${unit_user:-<unset>}" ;;
    *) pass "A2 unit User=$unit_user (not hub, not root)" ;;
esac
if grep -qE '^NoNewPrivileges=yes' "$UNIT"; then
    pass "A2 unit sets NoNewPrivileges=yes"
else
    fail "A2 unit does not set NoNewPrivileges=yes"
fi

echo
echo "── A3-A6: setup-ai-cli.sh refusals ──"
if setup_rc true hub 127.0.0.1; then
    fail "A3 setup accepted AI_CLI_USER=hub"
else
    if printf '%s' "$LAST_OUT" | grep -q 'NOPASSWD'; then
        pass "A3 setup refused AI_CLI_USER=hub, naming the sudo grant"
    else
        fail "A3 setup refused hub but without the reason: $LAST_OUT"
    fi
fi
# 192.0.2.0/24 is TEST-NET-1 — a stand-in LAN address, never a real one.
if setup_rc true homehub-ai 192.0.2.10; then
    fail "A4 setup accepted a LAN bind"
else
    pass "A4 setup refused a LAN bind (192.0.2.10)"
fi
if setup_rc true homehub-ai 0.0.0.0; then
    fail "A5 setup accepted the 0.0.0.0 wildcard"
else
    pass "A5 setup refused the 0.0.0.0 wildcard"
fi
if setup_rc true homehub-ai 127.0.0.1; then
    pass "A6 setup accepted loopback"
else
    fail "A6 setup refused loopback: $LAST_OUT"
fi
if setup_rc true homehub-ai 172.17.0.1; then
    pass "A6 setup accepted the docker bridge"
else
    fail "A6 setup refused the docker bridge: $LAST_OUT"
fi

echo
echo "── A7: off by default ──"
if setup_rc false homehub-ai 127.0.0.1; then
    if printf '%s' "$LAST_OUT" | grep -q 'nothing installed'; then
        pass "A7 AI_CLI_ENABLED=false installs nothing and exits 0"
    else
        fail "A7 disabled run exited 0 but said: $LAST_OUT"
    fi
else
    fail "A7 disabled run exited nonzero: $LAST_OUT"
fi

echo
echo "── A8-A9: the service's own --check ──"
check_rc() {
    AI_CLI_BIND="$1" AI_CLI_USER="${2:-homehub-ai}" \
        AI_CLI_REGISTRY="$DIR/agents.registry.csv" \
        AI_CLI_ENABLED_ROUTES="$DIR/routes-enabled" \
        AI_CLI_SCRATCH_ROOT="$TMP/scratch" \
        "$PY" "$SVC" --check 2>&1
}
out="$(check_rc 192.0.2.10)"; rc=$?
if [ "$rc" != 0 ] && printf '%s' "$out" | grep -q 'REFUSED TO START'; then
    pass "A8 --check refused a LAN bind"
else
    fail "A8 --check accepted a LAN bind (rc=$rc): $out"
fi
out="$(check_rc 0.0.0.0)"; rc=$?
if [ "$rc" != 0 ]; then
    pass "A8 --check refused the wildcard"
else
    fail "A8 --check accepted the wildcard: $out"
fi
out="$(check_rc 127.0.0.1)"; rc=$?
if [ "$rc" = 0 ] && printf '%s' "$out" | grep -q 'ai-cli OK'; then
    pass "A9 --check passes every enabled route's read-only check: $out"
else
    fail "A9 --check failed on loopback (rc=$rc): $out"
fi
# And a route that carries the ai-template flag must be refused, not merely
# absent from the shipped file. Build a bad registry and prove the refusal.
cat > "$TMP/bad.csv" <<'EOF'
Id,Family,Model,Version,Tier,CmdTemplate,Env,Notes
BAD-ROW,ANTHROPIC,claude-opus-5,5,medium,"claude -p --model {model} --permission-mode plan --disallowedTools Bash --dangerously-skip-permissions",,copied from ai-template
EOF
printf 'BAD-ROW\n' > "$TMP/bad-enabled"
out="$(AI_CLI_BIND=127.0.0.1 AI_CLI_REGISTRY="$TMP/bad.csv" \
    AI_CLI_ENABLED_ROUTES="$TMP/bad-enabled" AI_CLI_SCRATCH_ROOT="$TMP/scratch" \
    "$PY" "$SVC" --check 2>&1)"; rc=$?
if [ "$rc" != 0 ] && printf '%s' "$out" | grep -q 'dangerously'; then
    pass "A9 a copied --dangerously-* row is REFUSED at startup"
else
    fail "A9 a copied --dangerously-* row was accepted (rc=$rc): $out"
fi

echo
echo "── A10: the per-request scratch directory ──"
if grep -qE '^StateDirectory=homehub-ai' "$UNIT" \
   && grep -qE '^StateDirectoryMode=0700' "$UNIT"; then
    pass "A10 unit declares StateDirectory homehub-ai at 0700"
else
    fail "A10 unit does not declare a 0700 state directory"
fi
wd="$(grep -E '^WorkingDirectory=' "$UNIT" | tail -1 | cut -d= -f2-)"
case "$wd" in
    /var/lib/homehub-ai*) pass "A10 unit WorkingDirectory=$wd is the state tree" ;;
    *) fail "A10 unit WorkingDirectory=$wd is not under the state tree" ;;
esac

echo
echo "── A11: --bare is absent ──"
# Data rows only: the file's header block EXPLAINS why --bare is unusable, and
# a grep that cannot tell a comment from a command template would fail on its
# own documentation.
if grep -v '^[[:space:]]*#' "$DIR/agents.registry.csv" | grep -q -- '--bare'; then
    fail "A11 --bare is in a command template; it needs an API key (A40)"
else
    pass "A11 no --bare in any command template"
fi

echo
echo "──────────────────────────────────────────────────────────────"
printf '%s PASS  %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
