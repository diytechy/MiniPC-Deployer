#!/usr/bin/env bash
# provision-ai-clis.sh - install exactly the AI CLIs the enabled services need.
#
# SR-076 (what is installed), SR-077 (how: transactionally), SR-078 (failure is
# visible and self-healing). Contract: IF-038. Design: HomeHub
# docs/AI_CLI_PROVISIONING_PLAN_2026-10-11.md sections 3.1-3.5.
#
# The deployers package or pull what the configuration needs; the operator is
# told only which LOGINS remain. Logins stay human. Credentials are never read,
# written or moved by this script.
#
# Usage: provision-ai-clis.sh [--install-units | --schedule | --run | --check | --status [--json] | --rollback KEY]
#   --install-units  Copy the two unit files from this directory into the unit directory
#               (mode 0644), daemon-reload when anything changed; enables and starts nothing.
#               Idempotent: identical files are not rewritten. --schedule does this first.
#   --schedule  No network. Install the units, render the retry interval, enable
#               the retry timer, then start the provisioning unit with
#               --no-block. Never waits on the artifact lock (a retry may hold
#               it for a whole download). With AI_CLI_PROVISION=off, or when no
#               enabled consumer needs a CLI, disables the timer instead.
#   --run       Converge: download, verify, stage, publish, activate. This is
#               what the unit runs. Disables the timer when everything converged.
#   --check     Read-only: exit 0 when converged, 3 when not. No network.
#   --status    Print PROVISIONING: complete | INCOMPLETE - <key>: <reason>...
#               With --json, the machine-readable form the status step reads.
#   --rollback KEY  Swap KEY's link back to what it pointed at before the last
#               managed activation, validated against that previous version.
# Exit: 0 converged, 2 configuration refused, 3 INCOMPLETE (a retry is owed).
#
# Test seams (all default to the real paths): AI_CLIS_PREFIX, AI_CLIS_STATE_DIR,
# AI_CLIS_BIN_DIR, AI_CLIS_LOCK, AI_CLIS_MANIFEST, AI_CLIS_ENV_FILE, AI_CLIS_CURL,
# AI_CLIS_RUNUSER, AI_CLIS_SYSTEMCTL, AI_CLIS_UNIT_DIR, AI_CLIS_VAR_TMP,
# AI_CLIS_CHOWN, AI_CLIS_OWNER, AI_CLIS_RESERVE_MB, AI_CLIS_DEADLINE_SECONDS,
# AI_CLIS_CHECK_TIMEOUT, AI_CLIS_DIE_AT (pending|swap|check|checked: SIGKILL self
# at that point of an activation, to prove recovery).
set -u -o pipefail
umask 022

# Bump on ANY change to wrapper.template: it is part of every install identity,
# so a changed wrapper is a new install and an old directory is never trusted.
WRAPPER_REV=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${AI_CLIS_PREFIX:-/opt/homehub/ai-clis}"
STATE_DIR="${AI_CLIS_STATE_DIR:-/var/lib/homehub-ai-clis}"
BIN_DIR="${AI_CLIS_BIN_DIR:-/usr/local/bin}"
LOCK="${AI_CLIS_LOCK:-/run/lock/homehub-ai-clis.lock}"
MANIFEST="${AI_CLIS_MANIFEST:-$HERE/ai-clis.manifest.tsv}"
ENV_FILE="${AI_CLIS_ENV_FILE:-$HERE/../.env}"
CURL="${AI_CLIS_CURL:-curl}"
RUNUSER="${AI_CLIS_RUNUSER:-runuser}"
SYSTEMCTL="${AI_CLIS_SYSTEMCTL:-systemctl}"
UNIT_DIR="${AI_CLI_UNIT_DIR:-${AI_CLIS_UNIT_DIR:-/etc/systemd/system}}"
VAR_TMP="${AI_CLIS_VAR_TMP:-/var/tmp}"
CHOWN="${AI_CLIS_CHOWN:-chown}"
OWNER="${AI_CLIS_OWNER:-root:root}"
RESERVE_MB="${AI_CLIS_RESERVE_MB:-4096}"
DEADLINE_SECONDS="${AI_CLIS_DEADLINE_SECONDS:-3300}"
CHECK_TIMEOUT="${AI_CLIS_CHECK_TIMEOUT:-20}"

STATE_FILE="$STATE_DIR/state.json"
STAGING="$PREFIX/.staging"
DLDIR="$VAR_TMP/homehub-ai-clis"
SERVICE="homehub-ai-clis-provision.service"
TIMER="homehub-ai-clis-provision.timer"
RUN_START=$SECONDS

say() { printf '%s\n' "$*"; }
refuse() { say "REFUSED: $*"; exit 2; }

# ---------------------------------------------------------------- python helpers
# Standard library only. python3 is already on the box (the feeders need it).
read -r -d '' PYJ <<'PY'
import sys, json
op, a = sys.argv[1], sys.argv[2:]
raw = sys.stdin.read()
try:
    s = json.loads(raw) if raw.strip() else {}
except ValueError:
    s = {}
if not isinstance(s, dict):
    s = {}
s["version"] = 1
if not isinstance(s.get("clis"), dict):
    s["clis"] = {}
def walk(path, make):
    node = s
    parts = path.split(".")
    for p in parts[:-1]:
        if not isinstance(node, dict):
            return None, None
        if p not in node:
            if not make:
                return None, None
            node[p] = {}
        node = node[p]
    return node, parts[-1]
def prune():
    for k in list(s["clis"]):
        v = s["clis"][k]
        if isinstance(v, dict):
            for kk in list(v):
                if v[kk] in ({}, None):
                    del v[kk]
        if v in ({}, None):
            del s["clis"][k]
nul = lambda x: None if x == "-" else x
if op == "get":
    n, k = walk(a[0], False)
    v = n.get(k) if isinstance(n, dict) else None
    if isinstance(v, bool):
        print("true" if v else "false")
    elif isinstance(v, (dict, list)):
        print(json.dumps(v, sort_keys=True))
    elif v is not None:
        print(v)
    sys.exit(0)
if op == "set":
    n, k = walk(a[0], True)
    n[k] = json.loads(a[1])
elif op == "del":
    n, k = walk(a[0], False)
    if isinstance(n, dict):
        n.pop(k, None)
elif op == "act":
    key, st, fr, fv, to, tv = a
    s["clis"].setdefault(key, {})["activation"] = {
        "state": st, "from": nul(fr), "from_version": nul(fv),
        "to": nul(to), "to_version": nul(tv)}
elif op == "pend":
    key, reason, since = a
    c = s["clis"].setdefault(key, {})
    old = c.get("pending") if isinstance(c.get("pending"), dict) else {}
    c["pending"] = {"reason": reason, "since": old.get("since") or since}
elif op == "clearpending":
    for v in s["clis"].values():
        if isinstance(v, dict):
            v.pop("pending", None)
            v.pop("announced", None)
prune()
sys.stdout.write(json.dumps(s, indent=2, sort_keys=True))
PY

read -r -d '' PYARC <<'PY'
import sys, os, re, tarfile, zipfile, posixpath, stat
def bad(n):
    return (not n) or n.startswith("/") or ".." in n.split("/") or "\0" in n
def strip(n, k):
    parts = [p for p in n.split("/") if p not in ("", ".")]
    return "/".join(parts[k:]) if len(parts) > k else None
def check_tar(path, k):
    with tarfile.open(path) as t:
        for m in t:
            n = m.name
            if bad(n.rstrip("/") or n):
                return "member %r is absolute or contains .." % n
            if m.isdev() or m.isfifo():
                return "member %r is a device or fifo" % n
            sn = strip(n, k)
            if sn is None:
                continue
            if m.issym():
                ln = m.linkname
                if ln.startswith("/") or "\0" in ln or not ln:
                    return "symlink %r points outside the tree" % n
                dest = posixpath.normpath(posixpath.join(posixpath.dirname(sn), ln))
                if dest == ".." or dest.startswith("../"):
                    return "symlink %r escapes the tree" % n
            elif m.islnk():
                ln = m.linkname
                if bad(ln):
                    return "hardlink %r points outside the tree" % n
                if strip(ln, k) is None:
                    return "hardlink %r targets outside the stripped tree" % n
    return None
def check_zip(path):
    with zipfile.ZipFile(path) as z:
        for i in z.infolist():
            n = i.filename
            if bad(n.rstrip("/") or n):
                return "member %r is absolute or contains .." % n
            if stat.S_ISLNK(i.external_attr >> 16):
                return "member %r is a symlink" % n
    return None
def extract_zip(path, dest):
    base = os.path.realpath(dest)
    with zipfile.ZipFile(path) as z:
        for i in z.infolist():
            tgt = os.path.realpath(os.path.join(base, i.filename))
            if tgt != base and not tgt.startswith(base + os.sep):
                return "member %r escapes the tree" % i.filename
            if i.filename.endswith("/"):
                os.makedirs(tgt, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(tgt), exist_ok=True)
            with z.open(i) as src, open(tgt, "wb") as out:
                while True:
                    b = src.read(1 << 20)
                    if not b:
                        break
                    out.write(b)
            mode = (i.external_attr >> 16) & 0o777
            os.chmod(tgt, mode if mode else 0o644)
    return None
IMP = re.compile(r"""^[ 	]*(?:import\s*(?:[^"'\n]*?\bfrom\s*)?|export\s*\*\s*from\s*|\}\s*from\s*)["'](\.{1,2}/[^"'\s]+)["']""", re.M)
def inventory(root, entry, items):
    for it in items:
        if not os.path.exists(os.path.join(root, it)):
            return "expected %s is absent from the archive" % it
    if entry.endswith(".js"):
        seen, todo = set(), [entry]
        while todo:
            cur = todo.pop()
            if cur in seen:
                continue
            seen.add(cur)
            p = os.path.join(root, cur)
            if not os.path.isfile(p):
                return "%s imports %s, which is absent" % (os.path.basename(entry), cur)
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            for m in IMP.findall(text):
                nxt = posixpath.normpath(posixpath.join(posixpath.dirname(cur), m))
                if nxt.startswith("..") or not nxt.endswith(".js"):
                    continue
                todo.append(nxt)
    return None
op = sys.argv[1]
if op == "check":
    kind, path, k = sys.argv[2], sys.argv[3], int(sys.argv[4])
    r = check_zip(path) if kind == "zip" else check_tar(path, k)
elif op == "unzip":
    r = extract_zip(sys.argv[2], sys.argv[3])
elif op == "inventory":
    r = inventory(sys.argv[2], sys.argv[3], sys.argv[4:])
else:
    r = "unknown op"
if r:
    print(r)
    sys.exit(1)
PY

read -r -d '' PYREG <<'PY'
import sys, csv, io
reg, enabled = sys.argv[1], sys.argv[2]
def lines(p):
    with open(p, "r", encoding="utf-8-sig") as f:
        return [l for l in f.read().splitlines() if l.strip() and not l.lstrip().startswith("#")]
try:
    ids = [l.split("#")[0].strip() for l in lines(enabled)]
    rows = list(csv.DictReader(io.StringIO("\n".join(lines(reg)))))
except (OSError, csv.Error):
    sys.exit(3)
fam = {r.get("Id", "").strip(): (r.get("Family") or "").strip().upper() for r in rows}
if any(fam.get(i) == "OPENAI" for i in ids if i):
    print("openai")
PY

read -r -d '' PYSTATUS <<'PY'
import sys, json
out = {"provisioning": sys.argv[1], "clis": {}}
for line in sys.stdin.read().splitlines():
    f = line.split("\t")
    f += [""] * (7 - len(f))
    k, inst, ident, ver, link, act, pend = f[:7]
    n = lambda x: x if x else None
    out["clis"][k] = {"installed": inst == "true", "identity": n(ident), "version": n(ver),
                      "link_target": n(link), "activation_state": n(act), "pending_reason": n(pend)}
print(json.dumps(out, sort_keys=True))
PY

pyj() { python3 -I -c "$PYJ" "$@"; }
pyarc() { python3 -I -c "$PYARC" "$@"; }

# ------------------------------------------------------------------- state.json
STATE=""
STATE_ONDISK=""
load_state() {
    STATE_ONDISK=""
    [ -f "$STATE_FILE" ] && STATE_ONDISK="$(cat "$STATE_FILE")"
    STATE="$(printf '%s' "$STATE_ONDISK" | pyj dump)"
}
sget() { printf '%s' "$STATE" | pyj get "$1"; }
sset() { STATE="$(printf '%s' "$STATE" | pyj set "$1" "$2")"; }
sdel() { STATE="$(printf '%s' "$STATE" | pyj del "$1")"; }
# state.json is rewritten only when its bytes would differ, so a converged
# re-run performs no write at all (not even a touch).
save_state() {
    if [ ! -f "$STATE_FILE" ]; then
        # nothing recorded and nothing on disk: do not create an empty file
        [ "$STATE" = "$(printf '{}' | pyj dump)" ] && return 0
    fi
    [ "$STATE" = "$STATE_ONDISK" ] && return 0
    mkdir -p "$STATE_DIR" || return 1
    local tmp="$STATE_DIR/.state.json.$$"
    printf '%s\n' "$STATE" >"$tmp" || return 1
    mv -T "$tmp" "$STATE_FILE"
    STATE_ONDISK="$(cat "$STATE_FILE")"
}
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ------------------------------------------------------------------------ config
envval() { # KEY DEFAULT -> process env, overridden by the .env file (never sourced)
    local key="$1" v="${!1-}" f=""
    [ -n "$v" ] || v="$2"
    if [ -f "$ENV_FILE" ]; then
        f="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' || true)"
        f="${f#"${f%%[![:space:]]*}"}"; f="${f%"${f##*[![:space:]]}"}"
        case "$f" in \"*\") f="${f#\"}"; f="${f%\"}";; \'*\') f="${f#\'}"; f="${f%\'}";; esac
        [ -n "$f" ] && v="$f"
    fi
    printf '%s' "$v"
}
is_true() { [ "$(printf '%s' "$1" | tr 'A-Z' 'a-z')" = "true" ]; }

load_config() {
    CFG_USAGE_ENABLED="$(envval AI_USAGE_ENABLED false)"
    CFG_SOURCES="$(envval AI_USAGE_SOURCES codex,claude,opencode)"
    CFG_CLI_ENABLED="$(envval AI_CLI_ENABLED false)"
    CFG_CLI_USER="$(envval AI_CLI_USER homehub-ai)"
    CFG_USAGE_ACCOUNT="$(envval AI_USAGE_USER_ACCOUNT homehub-ai)"
    CFG_REGISTRY="$(envval AI_CLI_REGISTRY "$HERE/agents.registry.csv")"
    CFG_ROUTES="$(envval AI_CLI_ENABLED_ROUTES "$HERE/routes-enabled")"
    CFG_PROVISION="$(envval AI_CLI_PROVISION auto)"
    CFG_RETRY="$(envval AI_CLI_PROVISION_RETRY_MINUTES 15)"
    # one effective account for both consumers (plan 3.4)
    if is_true "$CFG_CLI_ENABLED"; then ACCOUNT="$CFG_CLI_USER"; else ACCOUNT="$CFG_USAGE_ACCOUNT"; fi
    case "$CFG_RETRY" in ''|*[!0-9]*) CFG_RETRY=15;; esac
    [ "$CFG_RETRY" -lt 5 ] && CFG_RETRY=5
    [ "$CFG_RETRY" -gt 1440 ] && CFG_RETRY=1440
    PROVISION_OFF=0
    [ "$(printf '%s' "$CFG_PROVISION" | tr 'A-Z' 'a-z')" = "off" ] && PROVISION_OFF=1
}

declare -A ENABLED=()
compute_enabled() {
    ENABLED=()
    if is_true "$CFG_USAGE_ENABLED"; then
        local s
        IFS=',' read -r -a srcs <<<"${CFG_SOURCES//[[:space:]]/}"
        for s in "${srcs[@]}"; do [ -n "$s" ] && ENABLED["source:$s"]=1; done
    fi
    if is_true "$CFG_CLI_ENABLED"; then
        ENABLED["service:ai-cli"]=1
        # An OPENAI-family route that is actually enabled needs Codex for the
        # ai-cli service too. Classified against the registry at the path the
        # service itself would resolve (AI_CLI_REGISTRY), not the default.
        if [ "$(python3 -I -c "$PYREG" "$CFG_REGISTRY" "$CFG_ROUTES" 2>/dev/null)" = "openai" ]; then
            ENABLED["service:ai-cli:openai"]=1
        fi
    fi
}

# --------------------------------------------------------------------- manifest
ORDER=()
declare -A M_VERSION M_NEEDED M_URL M_SHA M_SIZE M_KIND M_TREE M_INV M_ENTRY M_VCMD M_VREGEX M_TARGET M_REQ
load_manifest() {
    [ -f "$MANIFEST" ] || refuse "manifest $MANIFEST is not readable"
    local line f key
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        case "$line" in ''|'#'*) continue;; esac
        IFS=$'\t' read -r -a f <<<"$line"
        [ "${#f[@]}" -eq 14 ] || refuse "manifest row '${f[0]:-?}' has ${#f[@]} fields, expected 14"
        key="${f[0]}"
        [[ "$key" =~ ^[a-z][a-z0-9-]*$ ]] || refuse "manifest key '$key' is not a plain name"
        [[ "${f[4]}" =~ ^[0-9a-f]{64}$ ]] || refuse "manifest row $key has no 64-hex sha256"
        [[ "${f[5]}" =~ ^[0-9]+$ ]] || refuse "manifest row $key has a non-numeric size"
        [[ "${f[3]}" == https://* ]] || refuse "manifest row $key does not use https"
        case "${f[6]}" in file|tar|zip|nodetar) ;; *) refuse "manifest row $key has kind '${f[6]}'";; esac
        case "${f[7]}" in whole|member) ;; *) refuse "manifest row $key has tree '${f[7]}'";; esac
        ORDER+=("$key")
        M_VERSION[$key]="${f[1]}"; M_NEEDED[$key]="${f[2]}"; M_URL[$key]="${f[3]}"
        M_SHA[$key]="${f[4]}"; M_SIZE[$key]="${f[5]}"; M_KIND[$key]="${f[6]}"
        M_TREE[$key]="${f[7]}"; M_INV[$key]="${f[8]}"; M_ENTRY[$key]="${f[9]}"
        M_VCMD[$key]="${f[10]}"; M_VREGEX[$key]="${f[11]}"; M_TARGET[$key]="${f[12]}"
        M_REQ[$key]="${f[13]}"
    done <"$MANIFEST"
    [ "${#ORDER[@]}" -gt 0 ] || refuse "manifest $MANIFEST has no rows"
    for key in "${ORDER[@]}"; do
        if [ "${M_REQ[$key]}" != "-" ] && [ -z "${M_VERSION[${M_REQ[$key]}]:-}" ]; then
            refuse "manifest row $key requires '${M_REQ[$key]}', which is not a row"
        fi
    done
}

is_dep_only() { [[ "${M_NEEDED[$1]}" == required-by:* ]]; }

NEEDED=()   # rows an enabled consumer needs directly
compute_needed() {
    NEEDED=()
    local key tok
    for key in "${ORDER[@]}"; do
        is_dep_only "$key" && continue
        IFS=',' read -r -a toks <<<"${M_NEEDED[$key]}"
        for tok in "${toks[@]}"; do
            if [ -n "${ENABLED[$tok]:-}" ]; then NEEDED+=("$key"); break; fi
        done
    done
}

sha12() { printf '%s' "${M_SHA[$1]:0:12}"; }
identity_of() { # KEY
    local key="$1" id
    if is_dep_only "$key"; then
        printf '%s-%s' "${M_VERSION[$key]}" "$(sha12 "$key")"
        return
    fi
    id="${M_VERSION[$key]}-$(sha12 "$key")-w$WRAPPER_REV"
    if [ "${M_REQ[$key]}" != "-" ]; then
        local d="${M_REQ[$key]}"
        id="$id-d${M_VERSION[$d]}-$(sha12 "$d")"
    fi
    printf '%s' "$id"
}
dir_of() { printf '%s/%s/%s' "$PREFIX" "$1" "$(identity_of "$1")"; }
wrapper_of() { printf '%s/wrapper/%s' "$(dir_of "$1")" "$1"; }
is_complete() { # KEY: exact identity match in .complete
    local f; f="$(dir_of "$1")/.complete"
    [ -f "$f" ] && [ "$(cat "$f" 2>/dev/null)" = "$(identity_of "$1")" ]
}

# --------------------------------------------------------------------- the check
OBSERVED=""
CHECK_DETAIL=""
# consumer_observe PATH VCMD REGEX: run the CLI as the service account, in a
# fresh home, with the units' PATH and nothing else. Sets OBSERVED to the first
# version-looking token.
consumer_observe() {
    local path="$1" vcmd="$2" regex="$3" home out rc
    OBSERVED=""; CHECK_DETAIL=""
    home="$(mktemp -d "$VAR_TMP/ai-clis-home.XXXXXX")" || { CHECK_DETAIL="no scratch home"; return 1; }
    chmod 0700 "$home"
    if ! "$CHOWN" "$ACCOUNT" "$home" 2>/dev/null; then
        rm -rf "$home"; CHECK_DETAIL="cannot hand a scratch home to $ACCOUNT"; return 1
    fi
    # shellcheck disable=SC2086
    out="$(timeout "$CHECK_TIMEOUT" "$RUNUSER" -u "$ACCOUNT" -- env -i HOME="$home" \
        PATH=/usr/local/bin:/usr/bin:/bin "$path" $vcmd 2>&1)"; rc=$?
    rm -rf "$home"
    if [ "$rc" -ne 0 ]; then
        CHECK_DETAIL="$ACCOUNT could not run $(basename "$path") (exit $rc)"; return 1
    fi
    OBSERVED="$(printf '%s\n' "$out" | grep -oE -- "$regex" | head -1)"
    [ -n "$OBSERVED" ] || { CHECK_DETAIL="no version token in the output"; return 1; }
    return 0
}
# consumer_check PATH EXPECTED VCMD REGEX: exact token match ('*' accepts any).
consumer_check() { # PATH EXPECTED VCMD REGEX
    consumer_observe "$1" "$3" "$4" || return 1
    if [ "$2" != "*" ] && [ "$OBSERVED" != "$2" ]; then
        CHECK_DETAIL="version $OBSERVED, expected $2"; return 1
    fi
    return 0
}

die_at() { [ "${AI_CLIS_DIE_AT:-}" = "$1" ] && kill -KILL $$; return 0; }

# ---------------------------------------------------------------------- the link
link_now() { readlink "$BIN_DIR/$1" 2>/dev/null || true; }
swap_to() { # NAME TARGET ('' removes the link)
    local link="$BIN_DIR/$1" t="$2" tmp
    if [ -z "$t" ]; then rm -f "$link"; return 0; fi
    tmp="$BIN_DIR/.$1.new.$$"
    rm -f "$tmp"
    ln -s "$t" "$tmp" && mv -T "$tmp" "$link" || { rm -f "$tmp"; return 1; }
}
link_is_plain() { # a non-link at the bin path is somebody else's file
    local l="$BIN_DIR/$1"
    [ ! -e "$l" ] && [ ! -L "$l" ] && return 0
    [ -L "$l" ]
}
version_of_target() { # a managed wrapper path -> its pinned version, or ''
    case "$1" in
        "$PREFIX"/*/*/wrapper/*) local id; id="$(basename "$(dirname "$(dirname "$1")")")"; printf '%s' "${id%%-*}";;
    esac
}

# --------------------------------------------------------------- pending / notes
note_pending() { # KEY REASON
    local key="$1" reason="$2" h
    STATE="$(printf '%s' "$STATE" | pyj pend "$key" "$reason" "$(now)")"
    h="$(printf '%s\0%s' "$key" "$reason" | sha256sum | cut -c1-12)"
    if [ "$(sget "clis.$key.announced")" != "$h" ]; then
        say "PENDING $key: $reason"
        sset "clis.$key.announced" "\"$h\""
    else
        say "still pending since $(sget "clis.$key.pending.since") ($key)"
    fi
}
clear_pending() { # KEY
    local key="$1"
    if [ -n "$(sget "clis.$key.pending.reason")" ]; then say "RESOLVED $key"; fi
    sdel "clis.$key.pending"; sdel "clis.$key.announced"
}

# ----------------------------------------------------------------- disk admission
free_mb() { # PATH (nearest existing ancestor)
    local p="$1"
    while [ ! -e "$p" ] && [ "$p" != "/" ]; do p="$(dirname "$p")"; done
    df -Pm "$p" 2>/dev/null | awk 'NR==2{print $4}'
}
admit() { # KEY -> 0 or sets REASON
    local need=$(( 3 * (${M_SIZE[$1]} + 1048575) / 1048576 + RESERVE_MB )) d f
    for d in "$VAR_TMP" "$PREFIX"; do
        f="$(free_mb "$d")"
        if [ -z "$f" ] || [ "$f" -lt "$need" ]; then
            REASON="low disk: ${f:-?} MB free under $d, need $need MB; nothing was downloaded"
            return 1
        fi
    done
}

# ---------------------------------------------------------------- artifact build
REASON=""
STAGE=""
DL=""
DL_MADE=0
cleanup_work() {
    [ -n "$STAGE" ] && rm -rf "$STAGE"
    [ -n "$DL" ] && rm -f "$DL"
    STAGE=""; DL=""
}
cleanup_exit() {
    cleanup_work
    if [ "$DL_MADE" = 1 ]; then rmdir "$DLDIR" 2>/dev/null || true; fi
}
trap cleanup_exit EXIT
trap 'cleanup_exit; exit 143' TERM INT HUP

fail_stage() { REASON="$1"; cleanup_work; return 1; }

render_wrapper() { # KEY ROOT OUT
    local key="$1" root="$2" out="$3" line node="" id has_exec=0
    id="$(identity_of "$key")"
    if [ "${M_REQ[$key]}" != "-" ]; then
        local d="${M_REQ[$key]}"
        node="$PREFIX/$d/$(identity_of "$d")/${M_ENTRY[$d]}"
    fi
    mkdir -p "$(dirname "$out")" || return 1
    : >"$out"
    while IFS= read -r line || [ -n "$line" ]; do
        if [[ "$line" =~ ^#([a-z0-9-]+)#\ (.*)$ ]]; then
            [ "${BASH_REMATCH[1]}" = "$key" ] || continue
            line="${BASH_REMATCH[2]}"
            case "$line" in exec\ *) has_exec=1;; esac
        fi
        line="${line//@KEY@/$key}"; line="${line//@IDENTITY@/$id}"; line="${line//@ROOT@/$root}"
        line="${line//@ENTRY@/${M_ENTRY[$key]}}"; line="${line//@NODE@/$node}"
        printf '%s\n' "$line" >>"$out"
    done <"$HERE/wrapper.template"
    chmod 0755 "$out"
    [ "$has_exec" = 1 ]
}

# ensure_artifact KEY: the identity directory exists, complete, root-owned.
ensure_artifact() {
    local key="$1" final identity kind k=0 member="" dep
    REASON=""
    final="$(dir_of "$key")"; identity="$(identity_of "$key")"
    if [ "${M_REQ[$key]}" != "-" ]; then
        dep="${M_REQ[$key]}"
        if ! ensure_artifact "$dep"; then REASON="dependency $dep: $REASON"; return 1; fi
    fi
    if is_complete "$key"; then clear_pending_if_artifact "$key"; return 0; fi
    if [ $(( SECONDS - RUN_START )) -ge "$DEADLINE_SECONDS" ]; then
        REASON="the aggregate deadline (${DEADLINE_SECONDS}s) was reached before this was attempted"; return 1
    fi
    # A final-named directory without a matching .complete is never reused or
    # extracted into: it is moved aside and deleted.
    if [ -e "$final" ]; then
        mkdir -p "$STAGING" || { REASON="cannot create $STAGING"; return 1; }
        mv -T "$final" "$STAGING/stale.$key.$$" 2>/dev/null || rm -rf "$final"
        rm -rf "$STAGING/stale.$key.$$"
    fi
    admit "$key" || return 1

    mkdir -p "$STAGING" "$DLDIR" || { REASON="cannot create the staging area"; return 1; }
    chmod 0755 "$PREFIX" "$STAGING" 2>/dev/null
    DL_MADE=1
    DL="$DLDIR/$key.$$.bin"
    STAGE="$STAGING/$key.$$"
    rm -rf "$STAGE"; mkdir -p "$STAGE" || { REASON="cannot create $STAGE"; return 1; }

    say "fetching $key ${M_VERSION[$key]}"
    if ! "$CURL" --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
            --connect-timeout 20 --max-time 900 --output "$DL" "${M_URL[$key]}" 2>/dev/null; then
        fail_stage "download failed"; return 1
    fi
    local sz sum
    sz="$(wc -c <"$DL" | tr -d ' ')"
    if [ "$sz" != "${M_SIZE[$key]}" ]; then fail_stage "size mismatch: got $sz bytes, pinned ${M_SIZE[$key]}"; return 1; fi
    sum="$(sha256sum "$DL" | cut -d' ' -f1)"
    if [ "$sum" != "${M_SHA[$key]}" ]; then fail_stage "checksum mismatch"; return 1; fi

    kind="${M_KIND[$key]}"
    case "$kind" in
        file)
            mkdir -p "$STAGE/$(dirname "${M_ENTRY[$key]}")"
            mv -T "$DL" "$STAGE/${M_ENTRY[$key]}" || { fail_stage "cannot place the binary"; return 1; }
            chmod 0755 "$STAGE/${M_ENTRY[$key]}"   # curl writes 0644; a single binary must be executable
            DL=""
            ;;
        tar|nodetar|zip)
            [ "$kind" = nodetar ] && k=1
            local why
            if ! why="$(pyarc check "$kind" "$DL" "$k")"; then fail_stage "archive refused: $why"; return 1; fi
            if [ "$kind" = zip ]; then
                if ! why="$(pyarc unzip "$DL" "$STAGE")"; then fail_stage "archive refused: $why"; return 1; fi
            else
                [ "${M_TREE[$key]}" = member ] && member="${M_ENTRY[$key]}"
                if ! tar -xf "$DL" -C "$STAGE" --no-same-owner ${k:+--strip-components=$k} ${member:+"$member"} 2>/dev/null; then
                    fail_stage "archive could not be extracted"; return 1
                fi
            fi
            rm -f "$DL"; DL=""
            ;;
    esac

    # Ownership and modes: root-owned, no group/other write, nothing setuid,
    # readable and traversable by the service account.
    "$CHOWN" -R "$OWNER" "$STAGE" 2>/dev/null || { fail_stage "cannot set ownership $OWNER"; return 1; }
    chmod -R a-s,go-w,a+rX "$STAGE" || { fail_stage "cannot set modes"; return 1; }
    chmod 0755 "$STAGE"

    local why inv
    read -r -a inv <<<"${M_INV[$key]}"
    if ! why="$(pyarc inventory "$STAGE" "${M_ENTRY[$key]}" "${inv[@]}")"; then
        fail_stage "incomplete archive: $why"; return 1
    fi

    local probe
    if is_dep_only "$key"; then
        probe="$STAGE/${M_ENTRY[$key]}"
    else
        # the wrapper is rendered against the staging tree for the check, then
        # again against the final path before the rename
        render_wrapper "$key" "$STAGE" "$STAGE/wrapper/$key" || { fail_stage "wrapper template has no launcher for $key"; return 1; }
        probe="$STAGE/wrapper/$key"
    fi
    if ! consumer_check "$probe" "${M_VERSION[$key]}" "${M_VCMD[$key]}" "${M_VREGEX[$key]}"; then
        fail_stage "$CHECK_DETAIL before install"; return 1
    fi
    if ! is_dep_only "$key"; then
        render_wrapper "$key" "$final" "$STAGE/wrapper/$key" || { fail_stage "wrapper could not be rendered"; return 1; }
    fi
    printf '%s' "$identity" >"$STAGE/.complete"
    "$CHOWN" -R "$OWNER" "$STAGE" 2>/dev/null; chmod -R go-w "$STAGE"
    mkdir -p "$(dirname "$final")" || { fail_stage "cannot create $(dirname "$final")"; return 1; }
    chmod 0755 "$(dirname "$final")" 2>/dev/null
    if ! mv -T "$STAGE" "$final" 2>/dev/null; then fail_stage "cannot publish the install"; return 1; fi
    STAGE=""
    say "installed $key $identity"
    clear_pending_if_artifact "$key"
    return 0
}
clear_pending_if_artifact() { is_dep_only "$1" && clear_pending "$1"; return 0; }

# ------------------------------------------------------------------- activation
# activate KEY: the owned transaction. Sets REASON on failure (link restored).
activate() {
    local key="$1" wrapper cur prev from_v tv act_state act_to
    wrapper="$(wrapper_of "$key")"; tv="${M_VERSION[$key]}"
    cur="$(link_now "$key")"
    act_state="$(sget "clis.$key.activation.state")"; act_to="$(sget "clis.$key.activation.to")"
    if [ "$cur" = "$wrapper" ] && [ "$act_state" = committed ] && [ "$act_to" = "$wrapper" ]; then
        return 0
    fi
    if ! link_is_plain "$key"; then
        REASON="$BIN_DIR/$key is not a link; left alone"; return 1
    fi
    if [ "$cur" = "$wrapper" ]; then
        # our wrapper is linked but no committed record says so: verify, then record
        if consumer_check "$BIN_DIR/$key" "$tv" "${M_VCMD[$key]}" "${M_VREGEX[$key]}"; then
            local pf pv
            pf="$(sget "clis.$key.activation.from")"; pv="$(sget "clis.$key.activation.from_version")"
            STATE="$(printf '%s' "$STATE" | pyj act "$key" committed "${pf:--}" "${pv:--}" "$wrapper" "$tv")"
            return 0
        fi
        REASON="$CHECK_DETAIL through $BIN_DIR/$key"; return 1
    fi

    prev="$(sget "clis.$key.activation")"
    from_v=""
    if [ -n "$cur" ]; then
        from_v="$(version_of_target "$cur")"
        if [ -z "$from_v" ]; then
            # a hand-installed predecessor: observe its version once, as the account
            if consumer_observe "$cur" "${M_VCMD[$key]}" "${M_VREGEX[$key]}"; then from_v="$OBSERVED"; fi
        fi
    fi
    STATE="$(printf '%s' "$STATE" | pyj act "$key" pending "${cur:--}" "${from_v:--}" "$wrapper" "$tv")"
    save_state
    die_at pending
    if ! swap_to "$key" "$wrapper"; then
        revert_activation "$key" "$cur" "$prev"; REASON="cannot swap the link"; return 1
    fi
    die_at swap
    if ! consumer_check "$BIN_DIR/$key" "$tv" "${M_VCMD[$key]}" "${M_VREGEX[$key]}"; then
        revert_activation "$key" "$cur" "$prev"
        REASON="$CHECK_DETAIL through $BIN_DIR/$key; the previous link was restored"
        return 1
    fi
    die_at check
    STATE="$(printf '%s' "$STATE" | pyj act "$key" checked "${cur:--}" "${from_v:--}" "$wrapper" "$tv")"
    save_state
    die_at checked
    STATE="$(printf '%s' "$STATE" | pyj act "$key" committed "${cur:--}" "${from_v:--}" "$wrapper" "$tv")"
    save_state
    say "activated $key $tv"
    return 0
}
revert_activation() { # KEY FROM PREV_RECORD
    swap_to "$1" "$2"
    if [ -n "$3" ]; then sset "clis.$1.activation" "$3"; else sdel "clis.$1.activation"; fi
}

# recover: reconcile records left in pending/checked by an interrupted run.
recover() {
    local key st cur to from tv vc vr
    for key in "${ORDER[@]}"; do
        st="$(sget "clis.$key.activation.state")"
        case "$st" in pending|checked) ;; *) continue;; esac
        cur="$(link_now "$key")"; to="$(sget "clis.$key.activation.to")"
        from="$(sget "clis.$key.activation.from")"; tv="$(sget "clis.$key.activation.to_version")"
        vc="${M_VCMD[$key]}"; vr="${M_VREGEX[$key]}"
        if [ "$cur" = "$to" ]; then
            if consumer_check "$BIN_DIR/$key" "$tv" "$vc" "$vr"; then
                STATE="$(printf '%s' "$STATE" | pyj set "clis.$key.activation.state" '"committed"')"
                say "recovered $key: check passed, committed"
            else
                swap_to "$key" "$from"; sdel "clis.$key.activation"
                note_pending "$key" "an interrupted activation failed its check ($CHECK_DETAIL); the previous link was restored"
            fi
        elif [ "$cur" = "$from" ]; then
            sdel "clis.$key.activation"; say "recovered $key: the swap never happened"
        else
            sdel "clis.$key.activation"; say "recovered $key: the link was changed outside the provisioner; left alone"
        fi
        save_state
    done
}

# --------------------------------------------------------------------- timer unit
install_units() { # -> RELOAD=1 when anything changed
    local u d
    for u in "$SERVICE" "$TIMER"; do
        d="$UNIT_DIR/$u"
        [ -f "$HERE/$u" ] || continue
        if ! cmp -s "$HERE/$u" "$d" 2>/dev/null; then
            mkdir -p "$UNIT_DIR" && cp "$HERE/$u" "$d" && chmod 0644 "$d" && RELOAD=1
        fi
    done
}
render_dropin() {
    local dir="$UNIT_DIR/$TIMER.d" f body
    f="$dir/interval.conf"
    body=$'[Timer]\nOnUnitInactiveSec=\nOnUnitInactiveSec='"${CFG_RETRY}min"
    if [ "$(cat "$f" 2>/dev/null)" != "$body" ]; then
        mkdir -p "$dir" && printf '%s\n' "$body" >"$f" && RELOAD=1
    fi
}
RELOAD=0
prepare_units() {
    RELOAD=0
    install_units; render_dropin
    if [ "$RELOAD" = 1 ]; then "$SYSTEMCTL" daemon-reload || return 1; fi
}

# ------------------------------------------------------------------------ modes
lock_or_busy() { exec 9>>"$LOCK" || refuse "cannot open the lock $LOCK"; flock -n 9; }

mode_schedule() {
    if [ "$PROVISION_OFF" = 1 ] || [ "${#NEEDED[@]}" -eq 0 ]; then
        "$SYSTEMCTL" disable --now "$TIMER" || true
        # Clearing pending needs the main lock. Never wait for it: a retry may
        # hold it for a whole download. If it is busy the next --run clears it.
        if exec 9>>"$LOCK" && flock -n 9; then
            load_state; STATE="$(printf '%s' "$STATE" | pyj clearpending)"; save_state
        else
            say "pending-state cleanup deferred: a provisioning run holds the lock"
        fi
        say "provisioning: nothing to do (off or no enabled consumer needs a CLI)"
        return 0
    fi
    prepare_units || { say "cannot install the provisioning units"; return 3; }
    "$SYSTEMCTL" enable --now "$TIMER" || { say "cannot enable $TIMER"; return 3; }
    "$SYSTEMCTL" start --no-block "$SERVICE" || { say "cannot start $SERVICE"; return 3; }
    say "provisioning scheduled: ${NEEDED[*]} (retrying every $CFG_RETRY min until it converges)"
}

mode_run() {
    if [ "$PROVISION_OFF" = 1 ] || [ "${#NEEDED[@]}" -eq 0 ]; then
        "$SYSTEMCTL" disable --now "$TIMER" || true
        if lock_or_busy; then load_state; STATE="$(printf '%s' "$STATE" | pyj clearpending)"; save_state; fi
        say "provisioning: nothing to do (off or no enabled consumer needs a CLI)"
        return 0
    fi
    lock_or_busy || { say "another provisioning run holds the lock"; return 3; }
    prepare_units || say "WARNING: could not refresh the provisioning units"
    # a stale staging tree or download can belong to no live run: we hold the lock
    if [ -d "$STAGING" ]; then find "$STAGING" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null; fi
    if [ -d "$DLDIR" ]; then rm -rf "$DLDIR"; fi
    load_state
    recover
    local key bad=0 held
    for key in "${NEEDED[@]}"; do
        held="$(sget "clis.$key.held_identity")"
        if [ -n "$held" ] && [ "$held" = "$(identity_of "$key")" ]; then
            say "$key is held at the previous version by --rollback; change the pin to release it"
            continue
        fi
        [ -n "$held" ] && sdel "clis.$key.held_identity"
        if ensure_artifact "$key" && activate "$key"; then
            clear_pending "$key"
        else
            note_pending "$key" "$REASON"; bad=1
        fi
        save_state
    done
    save_state
    if [ "$bad" = 0 ]; then
        "$SYSTEMCTL" disable --now "$TIMER" || say "WARNING: could not disable $TIMER"
        say "PROVISIONING: complete"
        return 0
    fi
    "$SYSTEMCTL" enable --now "$TIMER" || true
    say "PROVISIONING: INCOMPLETE; retrying every $CFG_RETRY min"
    return 3
}

row_installed() { # KEY -> 0 when linked at its managed wrapper and committed
    local key="$1" w
    w="$(wrapper_of "$key")"
    [ "$(link_now "$key")" = "$w" ] && is_complete "$key" \
        && [ "$(sget "clis.$key.activation.state")" = committed ] \
        && [ "$(sget "clis.$key.activation.to")" = "$w" ]
}
row_ok() { # installed, or deliberately held by --rollback
    row_installed "$1" && return 0
    local held; held="$(sget "clis.$1.held_identity")"
    [ -n "$held" ] && [ "$held" = "$(identity_of "$1")" ]
}

mode_check() {
    load_state
    local key bad=0
    for key in "${NEEDED[@]}"; do
        if row_ok "$key"; then say "ok $key"; else say "not converged: $key"; bad=1; fi
    done
    return $(( bad * 3 ))
}

mode_status() {
    local json="$1" key prov="complete" bad="" inst link ident ver
    load_state
    local facts=""
    for key in "${ORDER[@]}"; do
        link="$(link_now "$key")"; ident=""; ver=""; inst=false
        if is_dep_only "$key"; then
            is_complete "$key" && inst=true && ident="$(identity_of "$key")" && ver="${M_VERSION[$key]}"
        else
            case "$link" in "$PREFIX/$key/"*/wrapper/*)
                ident="$(basename "$(dirname "$(dirname "$link")")")"; ver="${ident%%-*}";; esac
            row_installed "$key" && inst=true
        fi
        facts+="$key"$'\t'"$inst"$'\t'"$ident"$'\t'"$ver"$'\t'"$link"$'\t'"$(sget "clis.$key.activation.state")"$'\t'"$(sget "clis.$key.pending.reason")"$'\n'
    done
    for key in "${NEEDED[@]}"; do
        if ! row_ok "$key"; then
            local why; why="$(sget "clis.$key.pending.reason")"; [ -n "$why" ] || why="not yet installed"
            bad+="${bad:+; }$key: $why"
        fi
    done
    [ -n "$bad" ] && prov="incomplete"
    if [ "${#NEEDED[@]}" -gt 0 ]; then
        if exec 8>>"$LOCK" 2>/dev/null && ! flock -n 8; then prov="running"; fi
    fi
    [ "$PROVISION_OFF" = 1 ] && prov="off"
    if [ "$json" = 1 ]; then
        printf '%s' "$facts" | python3 -I -c "$PYSTATUS" "$prov"
    elif [ "$prov" = "off" ]; then
        say "PROVISIONING: off"
    elif [ "$prov" = "incomplete" ]; then
        say "PROVISIONING: INCOMPLETE - $bad; retrying every $CFG_RETRY min"
    elif [ "$prov" = "running" ]; then
        say "PROVISIONING: running"
    else
        say "PROVISIONING: complete"
    fi
}

mode_rollback() {
    local key="$1" to from fv tv cur st
    [ -n "${M_VERSION[$key]:-}" ] || refuse "unknown CLI '$key'"
    is_dep_only "$key" && refuse "$key has no link to roll back"
    lock_or_busy || { say "another provisioning run holds the lock"; return 3; }
    load_state
    st="$(sget "clis.$key.activation.state")"
    [ "$st" = committed ] || refuse "no committed activation record for $key; nothing to roll back"
    to="$(sget "clis.$key.activation.to")"; from="$(sget "clis.$key.activation.from")"
    fv="$(sget "clis.$key.activation.from_version")"; tv="$(sget "clis.$key.activation.to_version")"
    cur="$(link_now "$key")"
    [ "$cur" = "$to" ] || refuse "$BIN_DIR/$key no longer points at the recorded install; not touching it"
    local held; held="$(basename "$(dirname "$(dirname "$to")")")"
    if [ -z "$from" ]; then
        swap_to "$key" ""
        sdel "clis.$key.activation"; sset "clis.$key.held_identity" "\"$held\""
        save_state
        say "rolled back $key: the managed link was removed"
        return 0
    fi
    swap_to "$key" "$from"
    if consumer_check "$BIN_DIR/$key" "${fv:-*}" "${M_VCMD[$key]}" "${M_VREGEX[$key]}"; then
        STATE="$(printf '%s' "$STATE" | pyj act "$key" committed "$to" "$tv" "$from" "${fv:--}")"
        sset "clis.$key.held_identity" "\"$held\""
        save_state
        say "rolled back $key to ${fv:-the previous install}"
        return 0
    fi
    swap_to "$key" "$to"
    say "rollback of $key failed its check ($CHECK_DETAIL); the current install was restored"
    return 3
}

# -------------------------------------------------------------------------- main
MODE="${1:-}"
case "$MODE" in
    --install-units|--schedule|--run|--check|--status|--rollback) ;;
    *) refuse "usage: provision-ai-clis.sh [--install-units | --schedule | --run | --check | --status [--json] | --rollback KEY]" ;;
esac
if [ "$MODE" = "--install-units" ]; then    # needs neither the manifest nor the .env
    RELOAD=0; install_units
    if [ "$RELOAD" = 1 ]; then "$SYSTEMCTL" daemon-reload || exit 3; say "units installed in $UNIT_DIR"; else say "units already current in $UNIT_DIR"; fi
    exit 0
fi
load_manifest
load_config
case "$ACCOUNT" in
    root|hub) refuse "the service account may not be '$ACCOUNT'" ;;
esac
[[ "$ACCOUNT" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || refuse "'$ACCOUNT' is not a valid account name"
compute_enabled
compute_needed

case "$MODE" in
    --schedule) mode_schedule; exit $? ;;
    --run) mode_run; exit $? ;;
    --check) mode_check; exit $? ;;
    --status)
        if [ "${2:-}" = "--json" ]; then mode_status 1; else mode_status 0; fi
        exit 0 ;;
    --rollback)
        [ -n "${2:-}" ] || refuse "--rollback needs a CLI key"
        mode_rollback "$2"; exit $? ;;
esac
