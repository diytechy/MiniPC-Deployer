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
# AI_CLIS_CHECK_TIMEOUT, AI_CLIS_ID, AI_CLIS_DIE_AT (pending|swap|check|checked: SIGKILL
# self at that point of a link transaction, to prove recovery), AI_CLIS_FAIL_SAVE_AT
# (pending|checked|committed: that state write fails), AI_CLIS_FAIL_RESTORE=<target>
# (restoring the link to that target fails), AI_CLIS_FAIL_REMOVE=1 (removing a link fails),
# (ALL of the following hooks need AI_CLIS_TEST=1 and are otherwise ignored.)
# AI_CLIS_HOLD_AFTER_ARM=<seconds> (sleep between the arm and the pending write), AI_CLIS_FAIL_FSYNC_BIN_AT=<n>
# (only the n-th bin-directory fsync fails), AI_CLIS_HOLD_IN_LOCK=<seconds> (sleep inside the timer lock, to test the interleaving),
# AI_CLIS_FSYNC_LOG=<file> (append every path that is fsynced).
set -u -o pipefail
umask 022

# The fault-injection hooks are for the test suite only. They are ignored (removed from the
# environment) unless AI_CLIS_TEST=1, which no unit ever sets, so a stray variable cannot
# break a removal or restore, or kill a run.
if [ "${AI_CLIS_TEST:-}" != 1 ]; then
    unset AI_CLIS_DIE_AT AI_CLIS_FAIL_SAVE_AT AI_CLIS_FAIL_RESTORE AI_CLIS_FAIL_REMOVE AI_CLIS_HOLD_IN_LOCK AI_CLIS_HOLD_AFTER_ARM AI_CLIS_FAIL_FSYNC_BIN_AT AI_CLIS_FSYNC_LOG
fi

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
# 2400 s + one 900 s download + staging/check margin stays inside the unit's TimeoutStartSec=3600.
DEADLINE_SECONDS="${AI_CLIS_DEADLINE_SECONDS:-2400}"
CHECK_TIMEOUT="${AI_CLIS_CHECK_TIMEOUT:-20}"
IDCMD="${AI_CLIS_ID:-id}"

STATE_FILE="$STATE_DIR/state.json"
STAGING="$PREFIX/.staging"
DLDIR="$VAR_TMP/homehub-ai-clis"
SERVICE="homehub-ai-clis-provision.service"
TIMER="homehub-ai-clis-provision.timer"
GEN_FILE="$STATE_DIR/schedule-request"
# A SHORT second lock guards the request stamp and the timer state (never held across a download).
LOCK2="${LOCK}.timer"
PREFIX_N="$(realpath -m -- "$PREFIX" 2>/dev/null || printf '%s' "$PREFIX")"
# The scheduling generation this process started with, read before anything else
# so a --schedule that lands while we run is never mistaken for the one we saw.
GEN_AT_START="$(cat "$GEN_FILE" 2>/dev/null || true)"
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
if op == "histget":        # key target field
    h = (s["clis"].get(a[0]) or {}).get("history") or {}
    v = (h.get(a[1]) or {}).get(a[2])
    if v is not None:
        print(v)
    sys.exit(0)
if op == "histlist":       # every predecessor target recorded for a key
    h = (s["clis"].get(a[0]) or {}).get("history") or {}
    for v in h.values():
        print(v.get("target", ""))
    sys.exit(0)
if op == "set":
    n, k = walk(a[0], True)
    n[k] = json.loads(a[1])
elif op == "del":
    n, k = walk(a[0], False)
    if isinstance(n, dict):
        n.pop(k, None)
elif op == "act":
    key, st, fr, fv, to, tv = a[:6]
    s["clis"].setdefault(key, {})["activation"] = {
        "state": st, "from": nul(fr), "from_version": nul(fv),
        "to": nul(to), "to_version": nul(tv), "kind": a[6] if len(a) > 6 else "activate"}
elif op == "baseline":
    c = s["clis"].setdefault(a[0], {})
    if not c.get("baseline"):                  # permanent: written once, never replaced
        c["baseline"] = {"target": a[1], "version": nul(a[2])}
elif op == "histset":      # history[<target>] = the predecessor of THAT target
    c = s["clis"].setdefault(a[0], {})
    c.setdefault("history", {})[a[1]] = {"target": a[2], "version": nul(a[3])}
elif op == "health":
    s["clis"].setdefault(a[0], {})["health"] = {"target": a[1], "ok": a[2] == "true"}
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

read -r -d '' PYWRITE <<'PY'
import sys, os
path = sys.argv[1]
data = sys.stdin.buffer.read()
d = os.path.dirname(path) or "."
tmp = os.path.join(d, ".%s.%d.tmp" % (os.path.basename(path), os.getpid()))
try:
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    if os.environ.get("AI_CLIS_FSYNC_LOG"):
        with open(os.environ["AI_CLIS_FSYNC_LOG"], "a") as f:
            f.write("state " + path + chr(10))
except OSError:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    sys.exit(1)
PY

read -r -d '' PYSYNC <<'PY'
import sys, os
log = os.environ.get("AI_CLIS_FSYNC_LOG")
def sync(p):
    fd = os.open(p, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    if log:
        with open(log, "a") as f:
            f.write(p + "\n")
mode = sys.argv[1]
try:
    for p in sys.argv[2:]:
        if mode == "tree" and os.path.isdir(p):
            for dp, dns, fns in os.walk(p):
                for n in fns:
                    fp = os.path.join(dp, n)
                    if not os.path.islink(fp):
                        sync(fp)
                sync(dp)
        else:
            sync(p)
except OSError:
    sys.exit(1)
PY

read -r -d '' PYARC <<'PY'
import sys, os, re, tarfile, zipfile, posixpath, stat
def bad(n):
    return (not n) or n.startswith("/") or ".." in n.split("/") or "\0" in n
def strip(n, k):
    parts = [p for p in n.split("/") if p not in ("", ".")]
    return "/".join(parts[k:]) if len(parts) > k else None
def resolve(path, links, depth=0):
    """Normalise PATH inside the tree, following archive symlinks met on the way.
    Returns None when it would leave the tree (or loops)."""
    if depth > 40:
        return None
    parts = path.split("/")
    stack = []
    for i, p in enumerate(parts):
        if p in ("", "."):
            continue
        if p == "..":
            if not stack:
                return None
            stack.pop()
            continue
        stack.append(p)
        cur = "/".join(stack)
        rest = [q for q in parts[i + 1:] if q not in ("", ".")]
        if cur in links and rest:
            tgt = links[cur]
            if tgt.startswith("/"):
                return None
            return resolve("/".join(stack[:-1] + [tgt] + rest), links, depth + 1)
    return "/".join(stack)
def check_tar(path, k):
    links, members = {}, []
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
            members.append((sn, m))
            if m.issym():
                if m.linkname.startswith("/") or "\0" in m.linkname or not m.linkname:
                    return "symlink %r points outside the tree" % n
                links[sn] = m.linkname
            elif m.islnk():
                if bad(m.linkname) or strip(m.linkname, k) is None:
                    return "hardlink %r points outside the tree" % n
    for sn, m in members:
        if m.issym():
            if resolve(posixpath.join(posixpath.dirname(sn), m.linkname), links) is None:
                return "symlink %r escapes the tree" % m.name
        elif m.islnk():
            if resolve(strip(m.linkname, k), links) is None:
                return "hardlink %r escapes the tree" % m.name
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
    base = os.path.realpath(root)
    for dp, dns, fns in os.walk(root):
        for nme in dns + fns:
            fp = os.path.join(dp, nme)
            if os.path.islink(fp):
                rp = os.path.realpath(fp)
                if rp != base and not rp.startswith(base + os.sep):
                    return "link %s resolves outside the tree" % os.path.relpath(fp, root)
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
FS_BIN_N=0
fsync_paths() {
    if [ -n "${AI_CLIS_FAIL_FSYNC_BIN_AT:-}" ] && [ "$1" = "$BIN_DIR" ]; then
        FS_BIN_N=$((FS_BIN_N + 1))
        [ "$FS_BIN_N" -eq "$AI_CLIS_FAIL_FSYNC_BIN_AT" ] && return 1
    fi
    python3 -I -c "$PYSYNC" paths "$@" 2>/dev/null
}
fsync_tree() { python3 -I -c "$PYSYNC" tree "$@" 2>/dev/null; }
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
    if [ -n "${AI_CLIS_FAIL_SAVE_AT:-}" ] && printf '%s' "$STATE" | grep -q "\"state\": \"$AI_CLIS_FAIL_SAVE_AT\""; then
        return 1
    fi
    mkdir -p "$STATE_DIR" 2>/dev/null || return 1
    # temp file, fsync, rename, fsync the directory: a state write is either
    # durably there or it failed (and the caller must not go on to swap a link)
    printf '%s\n' "$STATE" | python3 -I -c "$PYWRITE" "$STATE_FILE" 2>/dev/null || return 1
    STATE_ONDISK="$STATE"
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
# One truthiness rule for the whole stack: envval already stripped one matched pair
# of quotes; the value must then be EXACTLY `true` (`True` is not true anywhere).
is_true() { [ "$1" = "true" ]; }

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
    if ! "$IDCMD" -u "$ACCOUNT" >/dev/null 2>&1; then
        CHECK_DETAIL="the service account '$ACCOUNT' does not exist"; return 1
    fi
    home="$(mktemp -d "$VAR_TMP/ai-clis-home.XXXXXX")" || { CHECK_DETAIL="no scratch home"; return 1; }
    chmod 0700 "$home"
    if ! "$CHOWN" "$ACCOUNT" "$home" 2>/dev/null; then
        rm -rf "$home"; CHECK_DETAIL="cannot hand a scratch home to $ACCOUNT"; return 1
    fi
    # shellcheck disable=SC2086
    out="$(timeout --kill-after=5 "$CHECK_TIMEOUT" "$RUNUSER" -u "$ACCOUNT" -- env -i HOME="$home" \
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
norm_path() { # TARGET -> an absolute, normalised path (a relative link target is relative to the bin directory)
    local t="$1"
    case "$t" in /*) ;; *) t="$BIN_DIR/$t";; esac
    realpath -m -- "$t" 2>/dev/null || printf '%s' "$t"
}
is_managed() { # is this link target one of our wrappers?
    local n; n="$(norm_path "$1")"
    case "$n" in "$PREFIX_N"/*/*/wrapper/*) return 0;; esac
    return 1
}
swap_to() { # NAME TARGET ('' removes the link); the bin directory is fsynced after the change
    local link="$BIN_DIR/$1" t="$2" tmp
    if [ -z "$t" ]; then
        [ -n "${AI_CLIS_FAIL_REMOVE:-}" ] && return 1
        rm -f "$link" || return 1
        fsync_paths "$BIN_DIR"; return $?
    fi
    [ -n "${AI_CLIS_FAIL_RESTORE:-}" ] && [ "$t" = "$AI_CLIS_FAIL_RESTORE" ] && return 1
    tmp="$BIN_DIR/.$1.new.$$"
    rm -f "$tmp"
    ln -s "$t" "$tmp" && mv -T "$tmp" "$link" || { rm -f "$tmp"; return 1; }
    fsync_paths "$BIN_DIR"
}
link_is() { # NAME TARGET ('' = there is no link and no file at all)
    local l="$BIN_DIR/$1"
    if [ -z "$2" ]; then [ ! -e "$l" ] && [ ! -L "$l" ]; else [ "$(readlink "$l" 2>/dev/null)" = "$2" ]; fi
}
link_is_plain() { # a non-link at the bin path is somebody else's file
    local l="$BIN_DIR/$1"
    [ ! -e "$l" ] && [ ! -L "$l" ] && return 0
    [ -L "$l" ]
}
abs_of() { # a link's target as the kernel resolves it: relative to the bin directory
    case "$1" in /*) printf '%s' "$1";; *) printf '%s/%s' "$BIN_DIR" "$1";; esac
}
version_of_target() { # a managed wrapper path -> its pinned version, or ''
    local n; n="$(norm_path "$1")"
    case "$n" in
        "$PREFIX_N"/*/*/wrapper/*) local id; id="$(basename "$(dirname "$(dirname "$n")")")"; printf '%s' "${id%%-*}";;
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
            say "free space under $d: ${f:-?} MB"
            REASON="low disk: a download here needs $need MB free (3 x the artifact + ${RESERVE_MB} MB reserve); nothing was downloaded"
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

# referenced_dir DIR: is anything live pointing into DIR? A link, a recorded transaction, or
# another install's wrapper (Node is reached only through the Gemini wrapper).
referenced_dir() {
    local d dn k t f w
    d="$1"; dn="$(realpath -m -- "$d" 2>/dev/null || printf '%s' "$d")"
    inside() { local n; n="$(norm_path "$1")"; case "$n" in "$dn"|"$dn"/*) return 0;; esac; return 1; }
    for k in "${ORDER[@]}"; do
        t="$(link_now "$k")"
        [ -n "$t" ] && inside "$t" && return 0
        for f in from to; do
            t="$(sget "clis.$k.activation.$f")"
            [ -n "$t" ] && inside "$t" && return 0
        done
        t="$(sget "clis.$k.baseline.target")"
        [ -n "$t" ] && inside "$t" && return 0
        while IFS= read -r t; do
            [ -n "$t" ] && inside "$t" && return 0
        done < <(printf '%s' "$STATE" | pyj histlist "$k")
    done
    for w in "$PREFIX"/*/*/wrapper/*; do
        [ -f "$w" ] || continue
        case "$w" in "$d"/*|"$dn"/*) continue;; esac
        grep -qsF -- "$dn/" "$w" && return 0
        grep -qsF -- "$d/" "$w" && return 0
    done
    return 1
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
        if referenced_dir "$final"; then
            REASON="the installed $key directory has no valid completion marker but is in use; it was left in place"
            return 1
        fi
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
    local cerr
    if ! cerr="$("$CURL" --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
            --connect-timeout 20 --max-time 900 --output "$DL" "${M_URL[$key]}" 2>&1)"; then
        cerr="$(printf '%s' "$cerr" | tr '\r\n' '  ' | sed -E 's/[0-9]{4,}/N/g' | cut -c1-160)"
        fail_stage "download failed${cerr:+: $cerr}"; return 1
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
        fail_stage "$CHECK_DETAIL (checked before install)"; return 1
    fi
    if ! is_dep_only "$key"; then
        render_wrapper "$key" "$final" "$STAGE/wrapper/$key" || { fail_stage "wrapper could not be rendered"; return 1; }
    fi
    printf '%s' "$identity" >"$STAGE/.complete"
    "$CHOWN" -R "$OWNER" "$STAGE" 2>/dev/null; chmod -R go-w "$STAGE"
    mkdir -p "$(dirname "$final")" || { fail_stage "cannot create $(dirname "$final")"; return 1; }
    chmod 0755 "$(dirname "$final")" 2>/dev/null
    if ! fsync_tree "$STAGE"; then fail_stage "cannot make the staged install durable"; return 1; fi
    if ! mv -T "$STAGE" "$final" 2>/dev/null; then fail_stage "cannot publish the install"; return 1; fi
    STAGE=""
    if ! fsync_paths "$(dirname "$final")" "$PREFIX"; then REASON="the published install could not be made durable"; return 1; fi
    say "installed $key $identity"
    clear_pending_if_artifact "$key"
    return 0
}
clear_pending_if_artifact() { is_dep_only "$1" && clear_pending "$1"; return 0; }

# ------------------------------------------------------------ retries, locks, history
any_open_txn() { # is ANY CLI's transaction pending or checked (needed or not)?
    local k st
    for k in "${ORDER[@]}"; do
        st="$(sget "clis.$k.activation.state")"
        case "$st" in pending|checked) return 0;; esac
    done
    return 1
}
arm_retries() { "$SYSTEMCTL" enable --now "$TIMER"; }
timer_lock() { { exec 7>>"$LOCK2"; } 2>/dev/null && flock -w 30 7; }
timer_unlock() { { exec 7>&-; } 2>/dev/null; return 0; }
# history_from KEY TARGET -> HF HFV: the predecessor recorded for exactly that target, else the baseline;
# never the target itself (a self-predecessor would turn a rollback into a no-op)
HF=""; HFV=""
history_from() {
    local key="$1" target="$2"
    HF="$(printf '%s' "$STATE" | pyj histget "$key" "$target" target)"
    HFV="$(printf '%s' "$STATE" | pyj histget "$key" "$target" version)"
    if [ -z "$HF" ] || [ "$(norm_path "$HF")" = "$(norm_path "$target")" ]; then
        HF="$(sget "clis.$key.baseline.target")"; HFV="$(sget "clis.$key.baseline.version")"
    fi
    if [ -n "$HF" ] && [ "$(norm_path "$HF")" = "$(norm_path "$target")" ]; then HF=""; HFV=""; fi
}
# note_history KEY KIND FROM FROM_VERSION TO: an ACTIVATION makes FROM the predecessor of TO
# (a rollback does not rewrite anyone's history)
note_history() {
    [ "$2" = activate ] || return 0
    [ -n "$3" ] && [ -n "$5" ] || return 0
    [ "$(norm_path "$3")" != "$(norm_path "$5")" ] || return 0
    STATE="$(printf '%s' "$STATE" | pyj histset "$1" "$5" "$3" "${4:--}")"
}

# -------------------------------------------------------------- link transactions
# ONE primitive for activation, rollback and recovery: record pending (durably), swap,
# run the consumer check, record checked, record committed. If a state write fails the
# operation aborts BEFORE the swap (or, after it, puts the link back); the previous link
# is never given up without a record that names it.
rec_set() { # KEY STATE FROM FROM_VERSION TO TO_VERSION KIND ; 1 and STATE unchanged when the write fails
    local before="$STATE"
    STATE="$(printf '%s' "$STATE" | pyj act "$@")"
    save_state || { STATE="$before"; return 1; }
}
txn_revert() { # KEY FROM PREV_RECORD -> 1 reverted cleanly, 2 restoring failed (the record is KEPT)
    local key="$1" from="$2" prev="$3"
    if ! swap_to "$key" "$from" || ! link_is "$key" "$from"; then
        REASON="$REASON; restoring the previous link FAILED, so the transaction record is kept and the next run retries"
        return 2
    fi
    if [ -n "$prev" ]; then sset "clis.$key.activation" "$prev"; else sdel "clis.$key.activation"; fi
    save_state || true   # if this write fails the on-disk pending/checked record plus link==from is recovered next run
    return 1
}
# link_txn KEY KIND FROM FROM_VERSION TO TO_VERSION EXPECT PREV_RECORD
# FROM is the link as it is now, TO what it will become ('' removes it). 0 committed, 1 reverted, 2 revert failed.
link_txn() {
    local key="$1" kind="$2" from="$3" fv="$4" to="$5" tv="$6" expect="$7" prev="$8" rc keep
    # 1. retries exist BEFORE anything can displace the link, and no shutdown can slip in between
    #    the arm and the durable pending record: the (short) timer lock covers both
    if ! timer_lock; then
        REASON="the timer lock was busy; the link was not touched"; return 1
    fi
    if ! arm_retries; then
        timer_unlock; REASON="the retry timer could not be armed; the link was not touched"; return 1
    fi
    if [ -n "${AI_CLIS_HOLD_AFTER_ARM:-}" ]; then : >"$LOCK2.armed"; sleep "$AI_CLIS_HOLD_AFTER_ARM"; fi
    # 2. the first time an UNOWNED link is displaced its target and observed version become a
    #    permanent baseline, written in the same durable write as the pending record
    keep="$STATE"
    if [ -n "$from" ] && ! is_managed "$from"; then
        STATE="$(printf '%s' "$STATE" | pyj baseline "$key" "$from" "${fv:--}")"
    fi
    if ! rec_set "$key" pending "${from:--}" "${fv:--}" "${to:--}" "${tv:--}" "$kind"; then
        STATE="$keep"
        timer_unlock
        REASON="the transaction could not be recorded durably in $STATE_DIR; the link was not touched"
        return 1
    fi
    timer_unlock
    die_at pending
    if ! swap_to "$key" "$to" || ! link_is "$key" "$to"; then
        REASON="cannot swap the link"
        txn_revert "$key" "$from" "$prev"; return $?
    fi
    die_at swap
    if [ -n "$to" ] && ! consumer_check "$BIN_DIR/$key" "$expect" "${M_VCMD[$key]}" "${M_VREGEX[$key]}"; then
        REASON="$CHECK_DETAIL through $BIN_DIR/$key; the previous link was restored"
        txn_revert "$key" "$from" "$prev"; return $?
    fi
    die_at check
    if ! rec_set "$key" checked "${from:--}" "${fv:--}" "${to:--}" "${tv:--}" "$kind"; then
        REASON="the check could not be recorded; the previous link was restored"
        txn_revert "$key" "$from" "$prev"; return $?
    fi
    die_at checked
    # everything the commit depends on is on disk before the commit is
    if [ -n "$to" ] && ! fsync_paths "$(norm_path "$to")" "$(dirname "$(norm_path "$to")")"; then
        REASON="the activated install could not be made durable; the previous link was restored"
        txn_revert "$key" "$from" "$prev"; return $?
    fi
    note_history "$key" "$kind" "$from" "$fv" "$to"
    if ! rec_set "$key" committed "${from:--}" "${fv:--}" "${to:--}" "${tv:--}" "$kind"; then
        REASON="the commit could not be recorded; the previous link was restored"
        txn_revert "$key" "$from" "$prev"; return $?
    fi
    return 0
}

# activate KEY: bring the link to the pinned install. Sets REASON on failure.
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
            if [ -n "$act_state" ]; then
                pf="$(sget "clis.$key.activation.from")"; pv="$(sget "clis.$key.activation.from_version")"
            else
                history_from "$key" "$wrapper"; pf="$HF"; pv="$HFV"          # the predecessor of this very target, else the baseline
            fi
            rec_set "$key" committed "${pf:--}" "${pv:--}" "$wrapper" "$tv" activate \
                || { REASON="the verified state could not be recorded"; return 1; }
            return 0
        fi
        REASON="$CHECK_DETAIL through $BIN_DIR/$key"; return 1
    fi

    prev="$(sget "clis.$key.activation")"
    from_v=""
    if [ -n "$cur" ]; then
        from_v="$(version_of_target "$cur")"
        if [ -z "$from_v" ]; then
            # a hand-installed predecessor: observe its version once, as the account.
            # A relative link target resolves against the bin directory, not our cwd.
            if consumer_observe "$(abs_of "$cur")" "${M_VCMD[$key]}" "${M_VREGEX[$key]}"; then
                from_v="$OBSERVED"
            else
                from_v="unobserved"
            fi
        fi
    fi
    link_txn "$key" activate "$cur" "$from_v" "$wrapper" "$tv" "$tv" "$prev" || return $?
    say "activated $key $tv"
    return 0
}

# expect_of VERSION: the token a check must see; "unobserved"/empty means any (only reachable via --force-unverified)
expect_of() { if [ -z "$1" ] || [ "$1" = unobserved ]; then printf '*'; else printf '%s' "$1"; fi; }

UNRESOLVED=()
# recover: reconcile records left in pending/checked by an interrupted run. Never throws a
# record away unless the link verifiably is where the record says it should be.
recover() {
    local key st cur to from tv fv kind
    UNRESOLVED=()
    for key in "${ORDER[@]}"; do
        st="$(sget "clis.$key.activation.state")"
        case "$st" in pending|checked) ;; *) continue;; esac
        cur="$(link_now "$key")"; to="$(sget "clis.$key.activation.to")"
        from="$(sget "clis.$key.activation.from")"; tv="$(sget "clis.$key.activation.to_version")"
        fv="$(sget "clis.$key.activation.from_version")"; kind="$(sget "clis.$key.activation.kind")"
        [ -n "$kind" ] || kind=activate
        if [ "$cur" = "$to" ]; then
            if [ -n "$to" ] && ! consumer_check "$BIN_DIR/$key" "$(expect_of "$tv")" "${M_VCMD[$key]}" "${M_VREGEX[$key]}"; then
                REASON="an interrupted change failed its check ($CHECK_DETAIL)"
                arm_retries || say "WARNING: could not arm the retry timer before restoring $key"
                if swap_to "$key" "$from" && link_is "$key" "$from"; then
                    sdel "clis.$key.activation"
                    note_pending "$key" "$REASON; the previous link was restored"
                else
                    note_pending "$key" "$REASON; restoring the previous link FAILED, the record is kept and the next run retries"
                    UNRESOLVED+=("$key")
                fi
            else
                local before="$STATE"
                note_history "$key" "$kind" "$from" "$fv" "$to"
                STATE="$(printf '%s' "$STATE" | pyj act "$key" committed "${from:--}" "${fv:--}" "${to:--}" "${tv:--}" "$kind")"
                if save_state; then say "recovered $key: committed"; else
                    STATE="$before"; UNRESOLVED+=("$key")
                    note_pending "$key" "an interrupted change passed its check but the commit could not be recorded"
                fi
            fi
        elif [ "$cur" = "$from" ]; then
            # the link is visibly back where it was; make that durable before the record is closed
            if fsync_paths "$BIN_DIR"; then
                sdel "clis.$key.activation"; say "recovered $key: the swap never happened"
            else
                UNRESOLVED+=("$key")
                note_pending "$key" "the previous link is back but could not be made durable; the record is kept and the next run retries"
            fi
        else
            sdel "clis.$key.activation"; say "recovered $key: the link was changed outside the provisioner; left alone"
        fi
        save_state || UNRESOLVED+=("$key")
    done
}
is_unresolved() { local k; for k in "${UNRESOLVED[@]}"; do [ "$k" = "$1" ] && return 0; done; return 1; }

# --------------------------------------------------------------------- timer unit
atomic_install() { # SRC DEST MODE: temp file in the destination directory, then rename
    local tmp
    tmp="$(dirname "$2")/.$(basename "$2").$$"
    cp "$1" "$tmp" && chmod "$3" "$tmp" && mv -T "$tmp" "$2" || { rm -f "$tmp"; return 1; }
}
install_units() { # -> RELOAD=1 when anything changed; non-zero when a unit could not be installed
    local u d rc=0
    for u in "$SERVICE" "$TIMER"; do
        d="$UNIT_DIR/$u"
        if [ ! -f "$HERE/$u" ]; then say "unit source $HERE/$u is absent"; rc=1; continue; fi
        if ! cmp -s "$HERE/$u" "$d" 2>/dev/null; then
            if mkdir -p "$UNIT_DIR" 2>/dev/null && atomic_install "$HERE/$u" "$d" 0644; then RELOAD=1
            else say "cannot install $u into $UNIT_DIR"; rc=1; fi
        fi
    done
    return $rc
}
render_dropin() {
    local dir="$UNIT_DIR/$TIMER.d" f body tmp
    f="$dir/interval.conf"
    body=$'[Timer]\nOnUnitInactiveSec=\nOnUnitInactiveSec='"${CFG_RETRY}min"
    if [ "$(cat "$f" 2>/dev/null)" != "$body" ]; then
        tmp="$dir/.interval.conf.$$"
        if mkdir -p "$dir" 2>/dev/null && printf '%s\n' "$body" >"$tmp" && chmod 0644 "$tmp" && mv -T "$tmp" "$f"; then
            RELOAD=1
        else
            rm -f "$tmp"; say "cannot write the retry interval into $dir"; return 1
        fi
    fi
}
RELOAD=0
prepare_units() {
    local rc=0
    RELOAD=0
    install_units || rc=1
    render_dropin || rc=1
    if [ "$RELOAD" = 1 ]; then "$SYSTEMCTL" daemon-reload || rc=1; fi
    return $rc
}

# ------------------------------------------------------------------------ modes
lock_or_busy() { exec 9>>"$LOCK" || refuse "cannot open the lock $LOCK"; flock -n 9; }

bump_request() { # a durable, unique stamp: a run that started before it knows it missed a request
    mkdir -p "$STATE_DIR" 2>/dev/null && printf '%s\n' "$(date +%s%N)-$$" | python3 -I -c "$PYWRITE" "$GEN_FILE" 2>/dev/null
}

# timer_finish [quiet]: the ONLY place a run may disable the timer. Under the short timer lock it
# compares the request stamp and checks that no transaction anywhere is open; both must be clean.
timer_finish() {
    local quiet="${1:-}"
    if ! timer_lock; then
        arm_retries || true
        [ -n "$quiet" ] || say "PROVISIONING: INCOMPLETE; the timer lock was busy, so the retry timer stays enabled"
        return 3
    fi
    local newer=0
    [ "$(cat "$GEN_FILE" 2>/dev/null || true)" != "$GEN_AT_START" ] && newer=1
    if [ "$newer" = 1 ] || any_open_txn; then
        arm_retries || true
        timer_unlock
        if [ -z "$quiet" ]; then
            if [ "$newer" = 1 ]; then say "PROVISIONING: INCOMPLETE; a newer request arrived during this run, so the retry timer stays enabled"
            else say "PROVISIONING: INCOMPLETE; a link change is still open, so the retry timer stays enabled"; fi
        fi
        return 3
    fi
    if [ -n "${AI_CLIS_HOLD_IN_LOCK:-}" ]; then : >"$LOCK2.held"; sleep "$AI_CLIS_HOLD_IN_LOCK"; fi
    "$SYSTEMCTL" disable --now "$TIMER" || say "WARNING: could not disable $TIMER"
    timer_unlock
    return 0
}

mode_schedule() {
    if [ "$PROVISION_OFF" = 1 ] || [ "${#NEEDED[@]}" -eq 0 ]; then
        timer_lock || { say "cannot take the timer lock"; return 3; }
        # Never disable while somebody else holds the main lock: a run or rollback may be between
        # its arm and its pending record. Probe it without waiting; if busy, DEFER (exit 0) and
        # leave the timer alone - that run decides at its own end whether it can be disabled.
        if ! { exec 9>>"$LOCK"; } 2>/dev/null || ! flock -n 9; then
            timer_unlock
            say "provisioning: shutdown deferred - a provisioning run holds the lock, so the retry timer was left alone"
            return 0
        fi
        load_state
        if any_open_txn; then
            # an interrupted link change must be reconciled by a run: never disable under it
            { exec 9>&-; } 2>/dev/null; timer_unlock
            prepare_units || say "WARNING: could not install the provisioning units"
            "$SYSTEMCTL" start --no-block "$SERVICE" || { say "cannot start $SERVICE"; return 3; }
            say "provisioning: an interrupted link change is recorded; recovery was started and the retry timer was left alone"
            return 3
        fi
        "$SYSTEMCTL" disable --now "$TIMER" || true
        STATE="$(printf '%s' "$STATE" | pyj clearpending)"; save_state
        { exec 9>&-; } 2>/dev/null; timer_unlock
        say "provisioning: nothing to do (off or no enabled consumer needs a CLI)"
        return 0
    fi
    # unit files first, so they exist before the timer is enabled; the main lock is never taken
    prepare_units || { say "cannot install the provisioning units"; return 3; }
    timer_lock || { say "cannot take the timer lock"; return 3; }
    if ! bump_request; then
        timer_unlock; say "cannot record the scheduling request in $STATE_DIR"; return 3
    fi
    if ! "$SYSTEMCTL" enable --now "$TIMER"; then timer_unlock; say "cannot enable $TIMER"; return 3; fi
    timer_unlock
    "$SYSTEMCTL" start --no-block "$SERVICE" || { say "cannot start $SERVICE"; return 3; }
    say "provisioning scheduled: ${NEEDED[*]} (retrying every $CFG_RETRY min until it converges)"
}

mode_run() {
    local off=0 key bad=0 tv
    if [ "$PROVISION_OFF" = 1 ] || [ "${#NEEDED[@]}" -eq 0 ]; then off=1; fi
    if ! lock_or_busy; then
        if [ "$off" = 1 ]; then
            say "another provisioning run holds the lock; it will finish the shutdown"
            return 0
        fi
        say "another provisioning run holds the lock"; return 3
    fi
    if [ "$off" = 0 ]; then prepare_units || say "WARNING: could not refresh the provisioning units"; fi
    # a stale staging tree or download can belong to no live run: we hold the lock
    if [ -d "$STAGING" ]; then find "$STAGING" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null; fi
    if [ -d "$DLDIR" ]; then rm -rf "$DLDIR"; fi
    load_state
    # an interrupted link change is reconciled FIRST, even when provisioning is off or nothing is needed
    recover
    if [ "$off" = 1 ]; then
        if any_open_txn; then
            arm_retries || true
            say "PROVISIONING: INCOMPLETE; an interrupted link change is not yet reconciled; retrying every $CFG_RETRY min"
            return 3
        fi
        timer_finish || return 3
        STATE="$(printf '%s' "$STATE" | pyj clearpending)"; save_state
        say "provisioning: nothing to do (off or no enabled consumer needs a CLI)"
        return 0
    fi
    for key in "${NEEDED[@]}"; do
        if is_unresolved "$key"; then bad=1; continue; fi
        if held_of "$key"; then
            tv="$(sget "clis.$key.activation.to_version")"
            if held_ok_static "$key" && { [ -z "$(sget "clis.$key.activation.to")" ] \
                    || consumer_check "$BIN_DIR/$key" "$(expect_of "$tv")" "${M_VCMD[$key]}" "${M_VREGEX[$key]}"; }; then
                say "$key is held at the previous version by --rollback; change the pin to release it"
                STATE="$(printf '%s' "$STATE" | pyj health "$key" "$(sget "clis.$key.activation.to")" true)"
                clear_pending "$key"
            else
                STATE="$(printf '%s' "$STATE" | pyj health "$key" "$(sget "clis.$key.activation.to")" false)"
                note_pending "$key" "the rolled-back install is no longer usable; it is held by --rollback and was not replaced"; bad=1
            fi
            save_state; continue
        fi
        if ensure_artifact "$key" && activate "$key"; then
            clear_pending "$key"
        else
            note_pending "$key" "$REASON"; bad=1
        fi
        save_state
    done
    save_state || bad=1
    # a transaction for ANY CLI that is still open (needed or not) keeps the run incomplete
    if any_open_txn; then bad=1; fi
    if [ "$bad" = 0 ]; then
        if timer_finish; then
            say "PROVISIONING: complete"
            return 0
        fi
        return 3
    fi
    arm_retries || true
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
# An explicit --rollback holds a key away from the pinned install: the committed record is a
# rollback and what it rolled away from is the install the manifest pins now.
held_of() {
    [ "$(sget "clis.$1.activation.state")" = committed ] && [ "$(sget "clis.$1.activation.kind")" = rollback ] \
        && [ "$(sget "clis.$1.activation.from")" = "$(wrapper_of "$1")" ]
}
held_ok_static() { # the link is still where the rollback put it, and what it points at exists and is executable
    local key="$1" to
    to="$(sget "clis.$key.activation.to")"
    link_is "$key" "$to" || return 1
    [ -z "$to" ] || [ -x "$BIN_DIR/$key" ]
}
# cheap_run KEY: the binary exists and its version runs (as root, in a scratch home)
cheap_run() {
    local key="$1" tv home out rc tok
    tv="$(sget "clis.$key.activation.to_version")"
    [ -x "$BIN_DIR/$key" ] || return 1
    home="$(mktemp -d "$VAR_TMP/ai-clis-chk.XXXXXX")" || return 1
    # shellcheck disable=SC2086
    out="$(env -i HOME="$home" PATH=/usr/local/bin:/usr/bin:/bin timeout --kill-after=5 "$CHECK_TIMEOUT" "$BIN_DIR/$key" ${M_VCMD[$key]} 2>&1)"; rc=$?
    rm -rf "$home"
    [ "$rc" -eq 0 ] || return 1
    tok="$(printf '%s\n' "$out" | grep -oE -- "${M_VREGEX[$key]}" | head -1)"
    [ -n "$tok" ] || return 1
    [ "$(expect_of "$tv")" = "*" ] || [ "$tok" = "$tv" ]
}
held_verified() { # the most recent --run checked THIS link target and it passed, and it still runs
    local key="$1" to
    to="$(sget "clis.$key.activation.to")"
    [ -z "$to" ] && return 0
    [ "$(sget "clis.$key.health.target")" = "$to" ] && [ "$(sget "clis.$key.health.ok")" = true ] || return 1
    cheap_run "$key"
}
row_ok() { # installed, or held by --rollback AND verified healthy
    row_installed "$1" && return 0
    held_of "$1" && held_ok_static "$1" && held_verified "$1"
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
            if held_of "$key"; then [ -n "$(sget "clis.$key.pending.reason")" ] || why="the held install is unverified (no passing --run check of the current link)"; fi
            bad+="${bad:+; }$key: $why"
        fi
    done
    [ -n "$bad" ] && prov="incomplete"
    # status never writes: the lock is opened read-only, and only if it already exists
    if [ "${#NEEDED[@]}" -gt 0 ] && [ -e "$LOCK" ] && { exec 8<"$LOCK"; } 2>/dev/null && ! flock -n 8; then prov="running"; fi
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

# --rollback KEY [--force-unverified]: the same transaction as an activation, aimed at what the
# link pointed at before. A previous version that was never observed needs the explicit flag.
mode_rollback() {
    local key="$1" force="${2:-}" to from fv tv cur st prev rc
    [ -n "${M_VERSION[$key]:-}" ] || refuse "unknown CLI '$key'"
    is_dep_only "$key" && refuse "$key has no link to roll back"
    case "$force" in ''|--force-unverified) ;; *) refuse "unknown option '$force'";; esac
    lock_or_busy || { say "another provisioning run holds the lock"; return 3; }
    load_state
    recover
    is_unresolved "$key" && { say "an interrupted link change for $key is not yet reconciled; try again after the next run"; return 3; }
    st="$(sget "clis.$key.activation.state")"
    [ "$st" = committed ] || refuse "no committed activation record for $key; nothing to roll back"
    to="$(sget "clis.$key.activation.to")"; from="$(sget "clis.$key.activation.from")"
    fv="$(sget "clis.$key.activation.from_version")"; tv="$(sget "clis.$key.activation.to_version")"
    cur="$(link_now "$key")"
    [ "$cur" = "$to" ] || refuse "$BIN_DIR/$key no longer points at the recorded install; not touching it"
    if [ -n "$from" ] && { [ -z "$fv" ] || [ "$fv" = unobserved ]; } && [ "$force" != --force-unverified ]; then
        refuse "the previous install of $key was never observed, so a rollback cannot verify it; rerun with --force-unverified to accept that"
    fi
    if [ -z "$from" ]; then   # durable history: never remove a CLI that had an unowned predecessor
        history_from "$key" "$cur"
        if [ -n "$HF" ]; then from="$HF"; fv="$HFV"; fi
    fi
    if [ -n "$from" ] && { [ -z "$fv" ] || [ "$fv" = unobserved ]; } && [ "$force" != --force-unverified ]; then
        refuse "the previous install of $key was never observed, so a rollback cannot verify it; rerun with --force-unverified to accept that"
    fi
    prev="$(sget "clis.$key.activation")"
    link_txn "$key" rollback "$cur" "$tv" "$from" "${fv:--}" "$(expect_of "$fv")" "$prev"; rc=$?
    if [ "$rc" = 0 ]; then
        if [ -z "$from" ]; then say "rolled back $key: the managed link was removed"; else say "rolled back $key to ${fv:-the previous install}"; fi
        if [ -n "$from" ]; then
            STATE="$(printf '%s' "$STATE" | pyj health "$key" "$from" true)"; save_state || true   # the rollback just checked it
        fi
        timer_finish quiet || true
        return 0
    fi
    say "rollback of $key did not complete: $REASON"
    return 3
}

# -------------------------------------------------------------------------- main
MODE="${1:-}"
case "$MODE" in
    --install-units|--schedule|--run|--check|--status|--rollback) ;;
    *) refuse "usage: provision-ai-clis.sh [--install-units | --schedule | --run | --check | --status [--json] | --rollback KEY [--force-unverified]]" ;;
esac
if [ "$MODE" = "--install-units" ]; then    # needs neither the manifest nor the .env
    RELOAD=0; rc=0; install_units || rc=1
    if [ "$RELOAD" = 1 ]; then "$SYSTEMCTL" daemon-reload || rc=1; fi
    [ "$rc" = 0 ] || { say "the provisioning units could not be installed in $UNIT_DIR"; exit 3; }
    if [ "$RELOAD" = 1 ]; then say "units installed in $UNIT_DIR"; else say "units already current in $UNIT_DIR"; fi
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
        mode_rollback "$2" "${3:-}"; exit $? ;;
esac
