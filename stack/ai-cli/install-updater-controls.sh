#!/usr/bin/env bash
# install-updater-controls.sh - write the vendor CLIs' system-level "do not
# self-update" settings (plan 3.5). The provisioned wrappers also export the
# matching environment variables; these files are the second control, read by
# the CLI itself wherever it is launched from.
#
#   /etc/claude-code/managed-settings.json  {"env":{"DISABLE_AUTOUPDATER":"1"}}
#   /etc/gemini-cli/settings.json           {"general":{"enableAutoUpdate":false}}   (--gemini only)
#
# Existing content is MERGED, never replaced: another key in either file is the
# Owner's and survives, and so do the file's mode and owner. Unchanged files are
# not rewritten; writes are temp file + fsync + rename. A file that is not JSON,
# or whose `env`/`general` is not an object, is left untouched and reported.
#
# Usage: install-updater-controls.sh [--gemini]
# Seam: AI_CLI_ETC_DIR (default /etc), so a test runs unprivileged.
set -euo pipefail

ETC="${AI_CLI_ETC_DIR:-/etc}"
WITH_GEMINI=0
[ "${1:-}" = "--gemini" ] && WITH_GEMINI=1

merge() {
    # merge <path> <dotted key path> <value-json>
    python3 - "$1" "$2" "$3" <<'PY'
import json, os, sys, tempfile
path, dotted, value = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
data = {}
existing = os.path.exists(path)
if existing:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except ValueError:
        print("install-updater-controls: %s is not JSON; left as it is" % path)
        sys.exit(0)
    if not isinstance(data, dict):
        print("install-updater-controls: %s is not a JSON object; left as it is" % path)
        sys.exit(0)
node = data
keys = dotted.split(".")
for key in keys[:-1]:
    if key in node and not isinstance(node[key], dict):
        # An incompatible branch is the Owner's: never replace it.
        print("install-updater-controls: %s has a non-object %r; left as it is" % (path, key))
        sys.exit(0)
    node = node.setdefault(key, {})
if existing and node.get(keys[-1]) == value:
    sys.exit(0)
node[keys[-1]] = value
directory = os.path.dirname(path)
os.makedirs(directory, mode=0o755, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=directory, prefix=".updater-")
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    if existing:
        before = os.stat(path)               # keep the mode and owner it had
        os.chmod(tmp, before.st_mode & 0o7777)
        try:
            os.chown(tmp, before.st_uid, before.st_gid)
        except (AttributeError, OSError):
            pass
    else:
        os.chmod(tmp, 0o644)
    os.replace(tmp, path)
except BaseException:
    if os.path.exists(tmp):
        os.unlink(tmp)
    raise
print("install-updater-controls: wrote %s" % path)
PY
}

merge "$ETC/claude-code/managed-settings.json" env.DISABLE_AUTOUPDATER '"1"'
if [ "$WITH_GEMINI" = 1 ]; then
    merge "$ETC/gemini-cli/settings.json" general.enableAutoUpdate 'false'
fi
