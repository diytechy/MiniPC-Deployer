#!/usr/bin/env bash
# ai-cli-guards.test.sh — the four containments A40 ratified, asserted.
#
# WHY THIS SUITE EXISTS. The four acceptance criteria of the AI CLI service are
# SECURITY properties, and a security property written into a unit file and
# asserted by eye is not asserted.
#
# WHY IT WAS REWRITTEN (cross-review, 2026-09-09). Three of its checks were
# VACUOUS — they greped an artifact instead of observing a behaviour, so they
# would have passed with the guard deleted:
#   * old A10 greped `StateDirectory=`/`StateDirectoryMode=` out of the unit
#     file. It would have passed with `request_scratch` deleted entirely, with
#     the child launched in `WorkingDirectory`, or with cleanup disabled.
#   * old A9 ran `--check` as whoever typed it, so it could not detect the
#     account mismatch (V2) that was the point of criterion 1.
#   * the account checks were greps of `User=` in the shipped unit — a name in
#     a file, not the identity anything runs as.
# Every check below now runs something and inspects what it did. The standard
# is: DELETE THE GUARD AND THIS SUITE MUST GO RED.
#
# HERMETIC: a temp .env and a temp tree. No account is created, no unit is
# installed, no service is started — every path that would do that is reached
# through `--check-only`, `--emit-dropin`, `--bind-check` or refused before it
# gets there.
#
# WHAT IT LOCKS
#   A1  no permission-skipping flag exists ANYWHERE in this repo's tree. This is
#       the trap: ai-template's rows carry two of them and they must never be
#       copied here. A grep over the whole tree, not over the registry, because
#       the flag could arrive in a script, a doc example or a unit file.
#   A2  the unit's account is DERIVED from AI_CLI_USER (setup --emit-dropin),
#       so what is certified and what systemd runs cannot be two accounts
#   A3  setup-ai-cli.sh REFUSES AI_CLI_USER=hub, with a reason
#   A4  setup-ai-cli.sh REFUSES a LAN bind — including 172.20.0.5, which is a
#       real household LAN address INSIDE the 172.16.0.0/12 the first version
#       of this guard accepted as "the docker bridge"
#   A5  setup-ai-cli.sh REFUSES the 0.0.0.0 wildcard
#   A6  setup-ai-cli.sh ACCEPTS loopback, and REFUSES a docker-bridge-shaped
#       address that no local bridge is actually carrying
#   A7  AI_CLI_ENABLED=false installs nothing and exits 0 (off by default)
#   A8  the service's own `--check` refuses a LAN bind and the wildcard
#   A9  the service REFUSES TO RUN AS THE WRONG ACCOUNT — asserted by running
#       `--check` in a process whose real identity is known, both directions
#   A10 the per-request scratch directory is OBSERVED: a request really starts
#       in a fresh 0700 directory under the scratch root, and it is really gone
#       afterwards
#   A11 `--bare` appears in no command template — it ignores the OAuth token
#       and needs an API key, so a service on the subscription cannot use it
#   A12 a row that runs SOMETHING OTHER THAN THE PINNED CLI is refused at
#       startup, however contained the flags after it look (cross-review V1)
#   A13 a row whose `Env=` sets PATH is refused at startup
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

# The account this process is REALLY running as. Taken from the OS, not from
# the module under test: an expectation derived from the code it checks is not
# an expectation.
ME="$(id -un 2>/dev/null || "$PY" -c 'import getpass;print(getpass.getuser())')"

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

check_rc() {
    # $1 bind, $2 user (default: the account really running this)
    AI_CLI_BIND="$1" AI_CLI_USER="${2:-$ME}" \
        AI_CLI_REGISTRY="${REG:-$DIR/agents.registry.csv}" \
        AI_CLI_ENABLED_ROUTES="${ENA:-$DIR/routes-enabled}" \
        AI_CLI_SCRATCH_ROOT="$TMP/scratch" \
        "$PY" "$SVC" --check 2>&1
}

echo "── A1: no permission-skipping flag anywhere in the tree ──"
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
    pass "A1 no permission-skipping token in the tree"
else
    fail "A1 permission-skipping flag found:"; printf '%s\n' "$hits" | sed 's/^/        /'
fi

echo
echo "── A2: the unit's account is DERIVED from the knob, not typed in ──"
# The old check greped `User=` out of the shipped unit, which proves only that
# somebody typed a name. What matters is that the account systemd runs is the
# account the checks certify - so run the generator with an unusual name and
# read what it produced.
if AI_CLI_ENV_FILE=/nonexistent AI_CLI_ENABLED=true AI_CLI_USER=homehub-ai-alt \
        bash "$SETUP" --emit-dropin "$TMP/dropin" >/dev/null 2>&1 \
   && grep -q '^User=homehub-ai-alt$' "$TMP/dropin/10-account.conf" \
   && grep -q '^Group=homehub-ai-alt$' "$TMP/dropin/10-account.conf"; then
    pass "A2 the unit account drop-in is generated from AI_CLI_USER"
else
    fail "A2 setup did not derive User=/Group= from AI_CLI_USER"
fi
unit_user="$(grep -E '^User=' "$UNIT" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
case "$unit_user" in
    hub|root|"") fail "A2 unit default User=${unit_user:-<unset>}" ;;
    *) pass "A2 unit default User=$unit_user (not hub, not root)" ;;
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
# THE V3 CASE, and it is not hypothetical: 172.20.0.0/16 is an ordinary home
# LAN range, and it is INSIDE the 172.16.0.0/12 the first version of this guard
# accepted as "the docker bridge".
if setup_rc true homehub-ai 172.20.0.5; then
    fail "A4 setup accepted 172.20.0.5 — a household LAN inside 172.16.0.0/12"
else
    pass "A4 setup refused a household LAN inside 172.16.0.0/12"
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
# On a machine with no docker bridge — this one — a bridge-SHAPED address must
# be refused, because membership of a range was never evidence of a bridge.
if setup_rc true homehub-ai 172.17.0.1; then
    fail "A6 setup accepted 172.17.0.1 with no docker bridge carrying it"
else
    pass "A6 setup refused a bridge-shaped address no bridge carries"
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
echo "── A8: the service's own --check and the bind ──"
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
    pass "A8 --check passes every enabled route's read-only check: $out"
else
    fail "A8 --check failed on loopback (rc=$rc): $out"
fi

echo
echo "── A9: the account asserted IS the account that runs ──"
# THE REWRITE. The old A9 ran `--check` as whoever typed it and asserted only
# that it printed OK, so it could not see the mismatch between the unit's
# hard-coded User= and the knob the checks validated. This runs the same
# command in the same process identity TWICE and requires the two answers to
# differ for exactly one reason: the configured account.
out="$(check_rc 127.0.0.1 homehub-ai-not-this-one)"; rc=$?
if [ "$rc" != 0 ] && printf '%s' "$out" | grep -q 'is not the account this process runs as'; then
    pass "A9 --check REFUSES when the configured account is not the running one"
else
    fail "A9 --check accepted a configured account nobody is running as (rc=$rc): $out"
fi
out="$(check_rc 127.0.0.1 "$ME")"; rc=$?
if [ "$rc" = 0 ]; then
    pass "A9 --check passes when they are the same account ($ME)"
else
    fail "A9 --check refused the true account $ME (rc=$rc): $out"
fi

echo
echo "── A10: the per-request scratch directory, OBSERVED ──"
# The old A10 greped StateDirectory= out of the unit and would have passed with
# request_scratch deleted. This drives the real request path with a fake CLI
# runner and inspects what the child was actually given: a fresh directory
# under the scratch root, holding only this request's schema, gone afterwards,
# and different for the next request.
if "$PY" - "$DIR" "$TMP/scratch-observed" <<'PY'
import json, os, sys
sys.path.insert(0, sys.argv[1])
import ai_cli_service as svc

root = sys.argv[2]
svc.resolve_executable = lambda name, **kw: "/usr/local/bin/" + name
config = svc.Config(env={"AI_CLI_SCRATCH_ROOT": root}, bridge_addresses=[])
routes, errors = svc.load_registry(os.path.join(sys.argv[1], "agents.registry.csv"))
assert not errors, errors
seen = []

def runner(argv, cwd, timeout, env, stdin_input):
    seen.append({"cwd": cwd, "listing": sorted(os.listdir(cwd)),
                 "mode": os.stat(cwd).st_mode & 0o777})
    return 0, json.dumps({"type": "result", "result": {"ok": True}}), False

for _ in range(2):
    status, payload = svc.handle_ask(
        {"route": "ANALYSIS-QUICK", "schema": {"type": "object"},
         "messages": [{"role": "user", "content": "hi"}]},
        config, routes, svc.RouteGate(0, 0, 1), runner=runner)
    assert status == 200, payload

problems = []
if len({s["cwd"] for s in seen}) != 2:
    problems.append("two requests shared a working directory")
for s in seen:
    if os.path.dirname(s["cwd"]) != os.path.abspath(root):
        problems.append("the child did not start under the scratch root: %s" % s["cwd"])
    if s["listing"] != ["response-schema.json"]:
        problems.append("the working directory held %s" % s["listing"])
    if os.name == "posix" and s["mode"] != 0o700:
        problems.append("mode was %o" % s["mode"])
    if os.path.exists(s["cwd"]):
        problems.append("the scratch directory survived the request: %s" % s["cwd"])
if problems:
    sys.stderr.write("; ".join(problems) + "\n")
    sys.exit(1)
PY
then
    pass "A10 each request really ran in its own 0700 scratch dir, removed after"
else
    fail "A10 the per-request scratch property does not hold"
fi

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
echo "── A12-A13: a bad registry row is refused AT STARTUP ──"
bad_row() {
    # $1 = the CmdTemplate, $2 = the Env cell, $3 = the expected message
    printf 'Id,Family,Model,Version,Tier,CmdTemplate,Env,Notes\n' > "$TMP/bad.csv"
    printf 'BAD-ROW,ANTHROPIC,claude-opus-5,5,medium,"%s","%s",built to be refused\n' \
        "$1" "$2" >> "$TMP/bad.csv"
    printf 'BAD-ROW\n' > "$TMP/bad-enabled"
    REG="$TMP/bad.csv" ENA="$TMP/bad-enabled" check_rc 127.0.0.1
}
out="$(bad_row 'claude -p --model {model} --output-format json --permission-mode plan --allowedTools Read --disallowedTools Bash,Write,Edit,NotebookEdit,WebFetch,WebSearch --dangerously-skip-permissions' '')"; rc=$?
if [ "$rc" != 0 ] && printf '%s' "$out" | grep -q 'dangerously'; then
    pass "A12 a copied ai-template row is REFUSED at startup"
else
    fail "A12 a copied ai-template row was accepted (rc=$rc): $out"
fi
# THE V1 PAYLOAD. Every flag after the executable looks contained; the command
# is not the CLI. The reviewed guard passed this and ran it as the service
# account.
out="$(bad_row "python3 -c pwned --permission-mode plan --disallowedTools Bash" '')"; rc=$?
if [ "$rc" != 0 ] && printf '%s' "$out" | grep -qi 'pinned'; then
    pass "A12 a row running something other than the pinned CLI is REFUSED"
else
    fail "A12 a non-CLI executable was accepted (rc=$rc): $out"
fi
out="$(bad_row 'claude -p --model {model} --output-format json --permission-mode plan --allowedTools Read --disallowedTools Bash,Write,Edit,NotebookEdit,WebFetch,WebSearch' 'PATH=/tmp/planted')"; rc=$?
if [ "$rc" != 0 ] && printf '%s' "$out" | grep -q 'Env may not set'; then
    pass "A13 a row whose Env sets PATH is REFUSED"
else
    fail "A13 an Env=PATH row was accepted (rc=$rc): $out"
fi

echo
echo "──────────────────────────────────────────────────────────────"
printf '%s PASS  %s FAIL\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
