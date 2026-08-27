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
# Usage:  assert-installed.sh [--target hub|wall] [--packages PATH]
#   --packages  the list of package names to assert. Defaults to the one the
#               install itself used, on the payload. Explained at check 3.

set -uo pipefail

TARGET="hub"
PKG_LIST=""
while [ $# -gt 0 ]; do
    case "$1" in
        --target)   TARGET="$2";   shift 2 ;;
        --packages) PKG_LIST="$2"; shift 2 ;;
        # Refused, not ignored — see check 3. A --seed that silently did nothing
        # would turn "every package is installed" into a check of nothing.
        --seed) echo "--seed is gone: the package list moved out of the user-data's 'packages:' (empty since 2026-08-06) and into the baked repo's packages.baked.list. Use --packages <path>." >&2; exit 2 ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown argument '$1'" >&2; exit 2 ;;
    esac
done
case "$TARGET" in hub|wall) ;; *) echo "--target must be hub or wall" >&2; exit 2 ;; esac

# The payload root differs by image, and getting it wrong is not hypothetical:
# this script asked for /opt/homehub on a WALL panel until 2026-08-06 — copied
# from the hub branch, and a path the wall image never creates.
if [ "$TARGET" = wall ]; then PAYLOAD_ROOT=/opt/wall-panel; else PAYLOAD_ROOT=/opt/homehub; fi

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
    REQUIRED="$PAYLOAD_ROOT /usr/sbin/sshd /etc/systemd/system/wall-firstboot.service"
else
    REQUIRED="$PAYLOAD_ROOT /usr/sbin/sshd /usr/bin/docker /etc/systemd/system/homehub-firstboot.service"
fi
for p in $REQUIRED; do
    if [ -e "$p" ]; then pass "present: $p"; else fail "MISSING: $p"; fi
done

# ── 3. every package the image asked for ────────────────────────────────────
# READ OFF THE BOX, NEVER RETYPED. Same discipline as before and the same
# reason — a hand-copied list silently stops testing whatever was added to the
# image last week — but the SOURCE changed on 2026-08-06.
#
# It used to parse `packages:` out of the seed subiquity saved at
# /var/log/installer/autoinstall-user-data. That key is now empty on both
# images: it runs before any late-command, so a baked apt repo cannot serve it,
# and every entry left there is one more thing the install fetches from the
# archive. Parsing it now would find nothing and this check would pass while
# asserting NOTHING — the shape this whole file exists to argue against.
#
# packages.baked.list is the exporter's own record of what it resolved into the
# repo the install used, it rides the payload beside the .debs, and
# stage_apt_into_payload refuses to build an ISO whose copy disagrees with the
# tracked stack/autoinstall[/wall]/packages.list. So it is both what was MEANT
# and what was ASKED FOR, on the machine, with nothing retyped.
[ -n "$PKG_LIST" ] || PKG_LIST="$PAYLOAD_ROOT/apt/packages.baked.list"
if [ -f "$PKG_LIST" ]; then
    # The same three rules the builders and the late-command use: strip from
    # `#`, strip whitespace, drop what is left if empty.
    PKGS="$(sed -e 's/#.*//' -e 's/[[:space:]]//g' "$PKG_LIST" | grep -v '^$')"
    if [ -z "$PKGS" ]; then
        fail "no package names could be parsed out of $PKG_LIST"
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
    # NOT a note. The old version degraded to a note when the seed was missing,
    # because check 1 had already failed and named the cause. This file's absence
    # has a different meaning: the payload — and therefore the apt repo the whole
    # install depends on — never landed, so NOTHING was installed. That is the
    # 2026-08-06 machine, and it must not read as "check skipped".
    fail "no baked package list at $PKG_LIST"
    note "the payload carries it beside the .debs; without it the install had no repo to install from"
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
        if [ -d "$PAYLOAD_ROOT/images" ]; then
            TARS="$(find "$PAYLOAD_ROOT/images" -name '*.tar' 2>/dev/null | wc -l)"
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
    # /opt/wall-panel/app/wall-shell — the path late-command 3b untars it to and
    # the path WALL_APP_CMD names in wall.env, which is the predicate
    # wall-kiosk.sh tests with [ -x ]. This looked for /opt/wall-shell and
    # /opt/homehub/wall-shell until 2026-08-06: two paths the wall image has
    # never created, so the check could only ever fail. Same copy-paste family
    # as the /opt/homehub in check 2 and in the image's own step 7.
    if [ -x /opt/wall-panel/app/wall-shell ]; then
        pass "the panel shell binary is installed (/opt/wall-panel/app/wall-shell)"
    elif [ -e /opt/wall-panel/wall-app ]; then
        fail "the shell tarball reached the panel but was never unpacked — late-command 3b did not run"
    else
        fail "no wall-shell binary — the panel would boot to the NOT INSTALLED screen"
        note "an image built with ALLOW_MISSING_SHELL=1 is expected to fail this"
    fi
    if systemctl is-active --quiet wall-kiosk 2>/dev/null || pgrep -f 'cage|wall-shell' >/dev/null 2>&1; then
        pass "a kiosk session is running"
    else
        fail "no kiosk session (wall-kiosk inactive and no cage/wall-shell process)"
    fi
fi

# ── 5c. the OPTIONAL EXTRAS: was activation actually EXECUTED? ───────────────
# WHY THIS EXISTS, and it is the same argument as every other check in this
# file. On 2026-08-27 the full lab gate PASSED while never once looking at the
# extras. The BUILD log proved carriage — 22 optional packages baked, the
# AppImage staged and placed on the ISO — and then the words `icedrive`, `xrdp`
# and `appimage` did not appear again anywhere in the run. Section 3 asserts the
# CORE package list, and the optional set is deliberately NOT in it, so nothing
# here had an opinion. Carriage was proven twice over; activation never once.
#
# IT IS NOT A THEORETICAL GAP. firstboot step 6c only WARNs when
# setup-remote-ui.sh fails — deliberately, because a box with no graphical
# session is degraded rather than broken. The consequence is that a hub whose
# desktop never installed reports a GREEN firstboot and a GREEN gate, which is
# precisely the shape this file exists to refuse.
#
# READ THE DECLARATION OFF THE BOX, NEVER FROM A FLAG. Same discipline as
# assert-iso-payload.sh: the .env firstboot actually routed on is the only thing
# that can say what SHOULD be here, so this cannot be handed the wrong
# expectation. That file is 0600 root-owned, so without root this is a SKIP —
# never a silent pass.
if [ "$TARGET" = hub ]; then
    ENV_ON_BOX="$PAYLOAD_ROOT/stack/.env"
    _envcat=""
    if [ "$(id -u)" -eq 0 ]; then
        _envcat="$(cat "$ENV_ON_BOX" 2>/dev/null)"
    elif sudo -n true 2>/dev/null; then
        _envcat="$(sudo -n cat "$ENV_ON_BOX" 2>/dev/null)"
    fi

    if [ -z "$_envcat" ]; then
        note "SKIP: the extras — reading $ENV_ON_BOX needs root and this account cannot escalate"
    else
        _v() { printf '%s\n' "$_envcat" | sed -n "s/^$1=//p" | head -1 | tr -d '\r' | sed 's/^"\(.*\)"$/\1/'; }
        RUI="$(_v REMOTE_UI_ENABLED)"
        ICE="$(_v ICEDRIVE_MODE)"
        note "the box declares REMOTE_UI_ENABLED=${RUI:-<absent>}  ICEDRIVE_MODE=${ICE:-<absent>}"

        # An absent knob is not "off" — it is an unanswerable question. Same
        # correction the ISO checker took on 2026-08-27, for the same reason.
        [ -n "$RUI" ] || fail "REMOTE_UI_ENABLED is absent from $ENV_ON_BOX — what SHOULD be installed is unknowable, not off"
        [ -n "$ICE" ] || fail "ICEDRIVE_MODE is absent from $ENV_ON_BOX — what SHOULD be installed is unknowable, not off"

        if [ "$RUI" = true ]; then
            # (a) the packages the feature needs. Read the list off the payload
            # rather than retyping it — the same rule section 3 follows, and the
            # reason eleven Qt/xcb libraries went missing for months is that
            # nobody had a list to check them against.
            OPTL="$PAYLOAD_ROOT/stack/autoinstall/packages.optional.list"
            if [ -s "$OPTL" ]; then
                _missing=0
                _n=0
                for p in $(sed 's/#.*//' "$OPTL" | awk 'NF{print $1}'); do
                    _n=$((_n + 1))
                    if ! dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "ok installed"; then
                        fail "optional package NOT installed: $p"
                        _missing=$((_missing + 1))
                    fi
                done
                if [ "$_missing" -eq 0 ]; then
                    pass "all $_n optional packages are installed (REMOTE_UI_ENABLED=true)"
                fi
            else
                fail "REMOTE_UI_ENABLED=true but $OPTL is not on the box — the optional set cannot be checked"
            fi

            # (b) xrdp: the door itself.
            if systemctl is-enabled --quiet xrdp 2>/dev/null; then
                pass "xrdp is enabled (starts on boot)"
            else
                fail "xrdp is NOT enabled"
            fi
            if systemctl is-active --quiet xrdp 2>/dev/null; then
                pass "xrdp is active"
            else
                fail "xrdp is NOT active"
            fi
            if command -v ss >/dev/null 2>&1; then
                if ss -ltn 2>/dev/null | grep -q ':3389 '; then
                    pass "tcp/3389 is listening"
                else
                    fail "nothing is listening on tcp/3389"
                fi
            fi

            # (c) THE AUTO-LOGIN SESSION — the point of SR-015. A GUI-only vendor
            # app has to run with NOBODY CONNECTED, so the session is created at
            # boot rather than when someone dials in. The unit is oneshot +
            # RemainAfterExit, so is-active means its ExecStart SUCCEEDED.
            if systemctl is-enabled --quiet homehub-desktop-session.service 2>/dev/null; then
                pass "homehub-desktop-session is enabled (the session is created at boot)"
            else
                fail "homehub-desktop-session is NOT enabled — no session unless a human connects"
            fi
            if systemctl is-active --quiet homehub-desktop-session.service 2>/dev/null; then
                pass "homehub-desktop-session succeeded (RemainAfterExit=yes)"
            else
                fail "homehub-desktop-session is NOT active — the boot-time session did not come up"
            fi

            # (d) AND THE SESSION REALLY EXISTS. The unit reporting success is
            # its own claim; an Xorg spawned by sesman is the independent fact.
            # These are different questions, and the gap between them is exactly
            # where a "green but no desktop" box lives.
            if pgrep -x Xorg >/dev/null 2>&1; then
                pass "an Xorg session is running with nobody connected (auto-login worked)"
            else
                fail "NO Xorg process — the unit may be green but no graphical session exists"
                note "this is the state in which a GUI-only app silently never starts"
            fi
        else
            # THE NEGATIVE IS WORTH ASSERTING TOO: a headless box that quietly
            # grew an RDP port is the other half of the same promise.
            if command -v ss >/dev/null 2>&1; then
                if ss -ltn 2>/dev/null | grep -q ':3389 '; then
                    fail "REMOTE_UI_ENABLED is not true but tcp/3389 IS listening"
                else
                    pass "no RDP port on a box that declares no desktop"
                fi
            fi
        fi

        # (e) IceDrive, keyed to the mode the box itself declares.
        ICE_APP=/opt/icedrive/Icedrive.AppImage
        case "$ICE" in
          appimage)
            if [ -x "$ICE_APP" ]; then
                pass "the IceDrive AppImage is installed and executable ($ICE_APP)"
                PIN="$PAYLOAD_ROOT/stack/icedrive/icedrive.pin"
                want="$(sed 's/#.*//' "$PIN" 2>/dev/null | awk -F= '$1 ~ /^[[:space:]]*APPIMAGE_SHA256[[:space:]]*$/ {gsub(/[[:space:]]/,"",$2); print $2; exit}')"
                got="$(sha256sum "$ICE_APP" 2>/dev/null | cut -d' ' -f1)"
                if [ -n "$want" ] && [ "$want" = "$got" ]; then
                    pass "the installed AppImage matches the pin ON THE BOX"
                else
                    fail "installed AppImage sha256 mismatch: box=$got pin=${want:-<unset>}"
                fi
                # The autostart entry is what brings it up inside the session.
                if ls /home/*/.config/autostart/icedrive.desktop >/dev/null 2>&1; then
                    pass "the IceDrive autostart entry is in place"
                else
                    fail "no ~/.config/autostart/icedrive.desktop — the app would never start itself"
                fi
                # AND IS IT ACTUALLY RUNNING. Everything above is about things
                # being PLACED; this is the only line that says the app STARTED —
                # the claim this project made for months while eleven runtime
                # libraries were missing and it could not launch at all.
                if pgrep -f 'Icedrive.AppImage' >/dev/null 2>&1; then
                    pass "IceDrive is RUNNING with nobody connected"
                else
                    fail "IceDrive is NOT running — installed and autostarted, but no process"
                    note "this is the pre-2026-08-27 failure exactly: it aborts on a missing"
                    note "runtime library and names a Qt PLUGIN rather than a package"
                fi
            else
                fail "ICEDRIVE_MODE=appimage but $ICE_APP is absent — ACTIVATION WITHOUT INSTALL"
            fi
            ;;
          cli)
            if [ -x /usr/local/bin/icedrive ] || [ -x /opt/icedrive/IcedriveCLI ]; then
                pass "the IceDrive CLI is installed"
            else
                fail "ICEDRIVE_MODE=cli but no CLI binary is on the box"
            fi
            if [ -e "$ICE_APP" ]; then
                fail "ICEDRIVE_MODE=cli but the AppImage is installed too — two clients fight over one account"
            else
                pass "no AppImage (mode is cli)"
            fi
            ;;
          off)
            if [ -e "$ICE_APP" ]; then
                fail "ICEDRIVE_MODE=off but $ICE_APP is installed anyway"
            else
                pass "no IceDrive client (mode is off)"
            fi
            ;;
        esac
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

# ── 6b. is the door KEY-ONLY, as both images claim? ─────────────────────────
# ADDED 2026-08-08, after a lab hub built from these files answered:
#     passwordauthentication yes
#     permitrootlogin without-password
# with an EMPTY /etc/ssh/sshd_config.d/. Both user-data files set
# `ssh: allow-pw: false` and both carried a comment asserting it survived
# `install-server: false`. It does not — allow-pw is rendered by cloud-init's
# set_passwords during the in-target pass install-server governs, so the offline
# install silently took the hardening with it on 2026-08-06.
#
# WHY IT LOOKED FINE FOR TWO DAYS: `password: "!"` is a locked hash, so there is
# no password to accept and the box behaves key-only. It is key-only by ACCIDENT.
# One `passwd hub` — which this project's own handoff instructs for console
# recovery — converts it into a box that accepts passwords from the whole LAN,
# with nothing anywhere reporting it.
#
# ASSERTED FROM `sshd -T`, NOT FROM A FILE. The effective policy is the resolved
# merge of sshd_config and every drop-in; grepping the drop-in would pass on a
# box where an Include ordering change had overridden it. Needs root, so it is a
# SKIP rather than a silent pass when this account cannot escalate — the same
# rule the rest of this file follows.
SSHD_T=""
if [ "$(id -u)" -eq 0 ]; then
    SSHD_T="$(sshd -T 2>/dev/null)"
elif sudo -n true 2>/dev/null; then
    SSHD_T="$(sudo -n sshd -T 2>/dev/null)"
fi
if [ -z "$SSHD_T" ]; then
    note "SKIP: the effective sshd policy — reading it needs root and this account cannot escalate"
else
    if printf '%s\n' "$SSHD_T" | grep -qix 'passwordauthentication no'; then
        pass "sshd is key-only: password authentication refused"
    else
        fail "PASSWORD AUTHENTICATION IS ENABLED — both images declare ssh.allow-pw: false"
        note "late-command 3c-ter installs the drop-in that enforces it; check it ran"
    fi
    if printf '%s\n' "$SSHD_T" | grep -qix 'permitrootlogin no'; then
        pass "sshd refuses root login outright"
    else
        got="$(printf '%s\n' "$SSHD_T" | awk '/^permitrootlogin/{print $2}')"
        fail "PermitRootLogin is '${got:-unset}' (want no)"
    fi
fi

echo
if [ "$FAILED" -eq 0 ]; then
    echo "== install completeness: ALL CHECKS PASSED =="
    exit 0
fi
echo "== install completeness: $FAILED CHECK(S) FAILED =="
exit 1
