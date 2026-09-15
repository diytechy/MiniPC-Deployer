#!/usr/bin/env bash
# wall-touch-wakeup-report.sh — READ-ONLY. Records whether the panel's
# touchscreen is a hardware wake source, so the S3 limitation in WALL-BURN-IN.md
# is a measured fact with a date rather than an assumption.
#
# Implements: LLR-914 (verified by TC-914).
#
# It reads and prints. It writes nothing, suspends nothing, and must never be
# given a device to enable: the Owner ruling of 2026-09-15 keeps suspend at the
# lowest power, so a touchscreen that turns out to be wake-capable is new
# evidence for the coordinator, not licence to change the ruling.
#
# Usage (on the panel, as root for the ACPI node):
#   sudo bash /opt/wall-panel/stack/autoinstall/wall/wall-touch-wakeup-report.sh
# Paste the whole output into WALL-BURN-IN.md beside the existing S3 claim.
set -uo pipefail

CONFIG="${TOUCH_FILTER_CONFIG:-/etc/wall-panel/touch-filter.json}"

say() { printf '%s\n' "$*"; }
rule() { printf -- '--- %s ---\n' "$*"; }

say "wall touch wakeup report"
say "date: $(date '+%F %H:%M %Z')"
say "host kernel: $(uname -r)"
say ""

rule "1. /proc/acpi/wakeup"
if [ -r /proc/acpi/wakeup ]; then
    cat /proc/acpi/wakeup
else
    say "(unreadable — re-run as root)"
fi
say ""

rule "2. touchscreen input node, resolved BY IDENTITY"
# eventN is allocation-order dependent; the identity in the filter config is not.
if [ -r "$CONFIG" ]; then
    python3 - "$CONFIG" <<'PY'
import json, sys, glob, os

config = json.load(open(sys.argv[1], encoding="utf-8"))
want = (config.get("name"), config.get("vendor"), config.get("product"))
print("config identity: name=%r vendor=%#x product=%#x mode=%s"
      % (want[0], want[1], want[2], config.get("mode")))


def attr(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None


matches = []
for base in sorted(glob.glob("/sys/class/input/input*")):
    name = attr(os.path.join(base, "name"))
    vendor, product = attr(os.path.join(base, "id/vendor")), attr(os.path.join(base, "id/product"))
    if name != want[0] or vendor is None or product is None:
        continue
    if int(vendor, 16) != want[1] or int(product, 16) != want[2]:
        continue
    matches.append(base)

if not matches:
    print("no input device matches that identity (is the touchscreen attached?)")
for base in matches:
    print("input node: %s" % base)
    # For an I2C-HID part the wakeup flag lives on an ancestor, not on input*.
    walk = os.path.realpath(os.path.join(base, "device"))
    for _ in range(8):
        flag = os.path.join(walk, "power", "wakeup")
        if os.path.exists(flag):
            print("  %s = %s" % (flag, attr(flag)))
            break
        parent = os.path.dirname(walk)
        if parent == walk or parent == "/sys":
            print("  no power/wakeup file on any ancestor of %s" % base)
            break
        walk = parent
PY
else
    say "(no $CONFIG — cannot resolve the touchscreen by identity)"
fi
say ""

rule "3. sleep states"
say "mem_sleep: $(cat /sys/power/mem_sleep 2>/dev/null || echo unreadable)"
say "state:     $(cat /sys/power/state 2>/dev/null || echo unreadable)"
say ""

rule "4. the answer to record"
say "Is the touchscreen a hardware wake source on this hardware?"
say "Answer from section 2 above (enabled / disabled / file absent), with today's date."
exit 0
