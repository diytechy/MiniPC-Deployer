#!/usr/bin/env bash
# vmtest/assert-installed.sh — did the install actually PRODUCE anything?
#
# Runs ON THE INSTALLED GUEST, over SSH, after it has booted. Copied there and
# executed by HomeHub's Start-VirtualHomeHub.ps1; also runnable by hand on a real box, which
# is the point — the same script that gates a VM can answer "is this hub
# actually finished?" while you are standing in front of one.
#
# WHY THIS EXISTS. On 2026-08-06 a production install reported success and
# produced a bare Ubuntu: no /opt/homehub, no openssh-server, no docker, no
# cockpit, and every subiquity artifact missing from /var/log/installer. The box
# booted, took its hostname and SSH key, and answered pings. Every existing
# check passed, because every existing check runs BEFORE the ISO is written and
# nothing had ever looked at an installed machine.
#
# WHAT IT IS NOT. It does not check that the services WORK — that is
# stack/provision/healthcheck.sh, which the gate runs immediately after this and
# which is a genuinely different question. Container-healthy is not
# service-healthy (WALL_GATE_HANDOFF.md defect #14), and installed is not
# either. Three questions, three checkers, in order of how early they can fail:
#
#   this script          did the install produce its artifacts?
#   healthcheck.sh       do the services answer?
#   (nothing yet)        is the DATA right?  — see open item A24(ii)
#
# Output: one PASS/FAIL line per check, non-zero exit if any FAILed. Same shape
# as healthcheck.sh so a caller can treat them identically.
#
# Usage:  assert-installed.sh [--target hub|wall] [--seed PATH]
#   --seed  the user-data to read the expected package list out of. Defaults to
#           the copy subiquity saved on the target. Explained at check 3.

set -uo pipefail

TARGET="hub"
SEED=""
while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        --seed)   SEED="$2";   shift 2 ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown argument '$1'" >&2; exit 2 ;;
    esac
done
case "$TARGET" in hub|wall) ;; *) echo "--target must be hub or wall" >&2; exit 2 ;; esac

FAILED=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILED=$((FAILED + 1)); }
note() { printf '      %s\n' "$*"; }

echo "== install completeness: $TARGET, $(hostname), $(date -Is) =="

# ── 1. did SUBIQUITY reach its own finish line? ─────────────────────────────
# THE CHECK THAT WOULD HAVE CAUGHT 2026-08-06, and the reason it is first.
# Subiquity copies its own logs to /var/log/installer on the target as one of
# the LAST things it does. A target carrying only the live medium's
# casper-md5check.json and media-info — which is exactly what that box had — is
# an install that stopped partway and rebooted anyway. Nothing else on the
# system says so: it boots, it has a hostname, it answers pings.
for f in autoinstall-user-data curtin-install.log subiquity-server-debug.log; do
    if [ -f "/var/log/installer/$f" ]; then
        pass "subiquity artifact present: $f"
    else
        fail "subiquity artifact MISSING: /var/log/installer/$f"
        note "the installer did not reach its finish line; this install is incomplete"
    fi
done
# And its own evidence-preservation path, which only exists on a failed run.
if [ -d /var/log/installer-failed ]; then
    fail "/var/log/installer-failed exists — a PREVIOUS install failed on this disk"
    note "read it; error-commands saved it there deliberately"
fi

# ── 2. the artifacts the install is supposed to leave ───────────────────────
if [ "$TARGET" = wall ]; then
    REQUIRED="/opt/homehub /usr/sbin/sshd /etc/systemd/system/wall-firstboot.service"
else
    REQUIRED="/opt/homehub /usr/sbin/sshd /usr/bin/docker /etc/systemd/system/homehub-firstboot.service"
fi
for p in $REQUIRED; do
    if [ -e "$p" ]; then pass "present: $p"; else fail "MISSING: $p"; fi
done

# ── 3. every package the image asked for ────────────────────────────────────
# READ OUT OF THE SEED, NEVER RETYPED. The same discipline as
# test-wall-artifact.sh, and for the same reason: a hand-copied list silently
# stops testing whatever was added to the image last week. If the seed is gone
# check 1 has already failed and said why, so this degrades to a note rather
# than piling a second failure onto one cause.
[ -n "$SEED" ] || SEED=/var/log/installer/autoinstall-user-data
if [ -f "$SEED" ]; then
    PKGS="$(sed -n '/^  packages:/,/^  [a-z_-]*:/p' "$SEED" \
            | sed -n 's/^    -[[:space:]]*\([a-zA-Z0-9][a-zA-Z0-9.+-]*\).*/\1/p')"
    if [ -z "$PKGS" ]; then
        fail "no packages: list could be parsed out of $SEED"
    else
        for pkg in $PKGS; do
            if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q '^install ok installed$'; then
                pass "package installed: $pkg"
            else
                fail "package NOT installed: $pkg"
            fi
        done
    fi
else
    note "skipped package check: no seed at $SEED (check 1 already reports why)"
fi

# ── 4. the container images the box is supposed to run offline ─────────────
# Q10.9 B+: every image is baked into the payload so first boot needs no
# registry. "docker is installed" and "the images are loaded" are different
# claims, and only the second means the stack can start on a dead internet.
if [ "$TARGET" = hub ]; then
    if command -v docker >/dev/null 2>&1; then
        COUNT="$(docker image ls -q 2>/dev/null | sort -u | wc -l)"
        if [ "$COUNT" -gt 0 ]; then
            pass "docker holds $COUNT image(s)"
        else
            fail "docker is installed but holds NO images — nothing was loaded from the payload"
        fi
        # Compare against what shipped, when the payload is still there to ask.
        if [ -d /opt/homehub/images ]; then
            TARS="$(find /opt/homehub/images -name '*.tar' 2>/dev/null | wc -l)"
            if [ "$TARS" -gt 0 ] && [ "$COUNT" -lt "$TARS" ]; then
                fail "$TARS image tar(s) shipped but only $COUNT loaded"
            fi
        fi
    else
        fail "docker is not installed — skipping the image check it depends on"
    fi
fi

# ── 5. first boot's own verdict ────────────────────────────────────────────
# firstboot.sh deliberately exits non-zero on a defect so that `systemctl
# status` is RED — its own comment: "on a headless box the unit's state is the
# only surface a defect can appear on that is not a line in a scrolling
# journal". Read that surface.
UNIT="homehub-firstboot.service"
[ "$TARGET" = wall ] && UNIT="wall-firstboot.service"
if systemctl list-unit-files "$UNIT" >/dev/null 2>&1 && [ -e "/etc/systemd/system/$UNIT" ]; then
    STATE="$(systemctl show -p Result --value "$UNIT" 2>/dev/null)"
    ACTIVE="$(systemctl is-active "$UNIT" 2>/dev/null)"
    case "$STATE" in
        success) pass "$UNIT completed (Result=success, is-active=$ACTIVE)" ;;
        "")      fail "$UNIT has no Result yet — it may still be running (is-active=$ACTIVE)" ;;
        *)       fail "$UNIT Result=$STATE (is-active=$ACTIVE)" ;;
    esac
    if journalctl -u "$UNIT" --no-pager 2>/dev/null | grep -q 'FATAL'; then
        fail "$UNIT logged FATAL — read: journalctl -u $UNIT"
    fi
    # The SIM marker must NOT be here. A production box carrying it means a sim
    # artifact reached real media (WALL_GATE_HANDOFF.md's anti-demonstration
    # check, promoted from a manual grep to an assertion).
    if journalctl -u "$UNIT" --no-pager 2>/dev/null | grep -q 'SIM GATE'; then
        fail "$UNIT logged 'SIM GATE' — this is a SIM build, not a production one"
    fi
else
    fail "$UNIT is not installed"
fi

# ── 5b. the panel's own reason to exist ────────────────────────────────────
# A wall panel that installed perfectly and shows nothing is the failure this
# project keeps circling: WALL_GATE_HANDOFF.md defect #15, "no red drive check
# is indistinguishable from the drive check is green on a wall". The kiosk
# session is checked here; whether it PAINTS is still a human looking at it.
if [ "$TARGET" = wall ]; then
    if [ -x /opt/wall-shell/wall-shell ] || [ -x /opt/homehub/wall-shell/wall-shell ]; then
        pass "the panel shell binary is installed"
    else
        fail "no wall-shell binary — the panel would boot to a blank tty"
    fi
    if systemctl is-active --quiet wall-kiosk 2>/dev/null || pgrep -f 'cage|wall-shell' >/dev/null 2>&1; then
        pass "a kiosk session is running"
    else
        fail "no kiosk session (wall-kiosk inactive and no cage/wall-shell process)"
    fi
fi

# ── 6. can anyone get back in? ─────────────────────────────────────────────
# The 2026-08-06 box was unreachable by every path at once: sshd absent AND the
# console account password-locked. Either alone is survivable; together they
# cost a GRUB rescue. Assert that at least one door exists.
SSH_OK=no
systemctl is-enabled ssh >/dev/null 2>&1 && [ -x /usr/sbin/sshd ] && SSH_OK=yes
CONSOLE_OK=no
ACCT="hub"; [ "$TARGET" = wall ] && ACCT="panel"
if [ -r /etc/shadow ]; then
    HASH="$(awk -F: -v u="$ACCT" '$1==u{print $2}' /etc/shadow 2>/dev/null)"
    case "$HASH" in ''|'!'|'*'|'!!') CONSOLE_OK=no ;; *) CONSOLE_OK=yes ;; esac
else
    CONSOLE_OK=unknown
fi
if [ "$SSH_OK" = yes ]; then
    pass "reachable: sshd installed and enabled"
else
    fail "sshd is NOT installed+enabled"
fi
if [ "$SSH_OK" != yes ] && [ "$CONSOLE_OK" != yes ]; then
    fail "NO WAY IN: sshd absent and '$ACCT' has a locked password. Recovery needs GRUB."
fi

echo
if [ "$FAILED" -eq 0 ]; then
    echo "== install completeness: ALL CHECKS PASSED =="
    exit 0
fi
echo "== install completeness: $FAILED CHECK(S) FAILED =="
exit 1
