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
# Owner's and survives. Unchanged files are not rewritten. A file that is not
# JSON is left alone and reported (a diagnostic, not a package error).
#
# Usage: install-updater-controls.sh [--gemini]
# Seam: AI_CLI_ETC_DIR (default /etc), so a test runs unprivileged.
set -euo pipefail

ETC="${AI_CLI_ETC_DIR:-/etc}"
WITH_GEMINI=0
[ "${1:-}" = "--gemini" ] && WITH_GEMINI=1

merge() {
    # merge <path> <json-path as dotted> <value-json>
    python3 - "$1" "$2" "$3" <<'PY'
import json, os, sys
path, dotted, value = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
data = {}
if os.path.exists(path):
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
    child = node.get(key)
    if not isinstance(child, dict):
        child = {}
        node[key] = child
    node = child
if node.get(keys[-1]) == value and os.path.exists(path):
    sys.exit(0)
node[keys[-1]] = value
os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
    json.dump(data, handle, separators=(",", ":"))
    handle.write("\n")
os.chmod(tmp, 0o644)
os.replace(tmp, path)
print("install-updater-controls: wrote %s" % path)
PY
}

merge "$ETC/claude-code/managed-settings.json" env.DISABLE_AUTOUPDATER '"1"'
if [ "$WITH_GEMINI" = 1 ]; then
    merge "$ETC/gemini-cli/settings.json" general.enableAutoUpdate 'false'
fi
