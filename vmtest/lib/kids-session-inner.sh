#!/bin/bash
# vmtest/lib/kids-session-inner.sh — runs INSIDE test-kids-session.sh's
# container, as root. Mounted: /kids (stack/autoinstall/kids), /artifact.tar.gz,
# /out (screenshots). Prints one line per check and FAILURES=<n> last; exits
# non-zero when any check failed.
set -u
FAIL=0
ok()  { printf 'PASS %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAIL=$((FAIL + 1)); }

# ── TC-1621: the tarball unpacks as late-command 3b unpacks it ───────────────
mkdir -p /opt/kids-kiosk
tar -xzf /artifact.tar.gz -C /opt/kids-kiosk
[ -x /opt/kids-kiosk/app/ottertube-desktop ] && ok "TC-1621 app/ottertube-desktop executable" || bad "TC-1621 app/ottertube-desktop not executable"
[ -u /opt/kids-kiosk/app/chrome-sandbox ] && [ "$(stat -c %u /opt/kids-kiosk/app/chrome-sandbox)" = 0 ] \
    && ok "TC-1621 chrome-sandbox setuid root" || bad "TC-1621 chrome-sandbox lost setuid root"

# ── TC-1622: packages.list covers every shared library ───────────────────────
missing=$(ldd /opt/kids-kiosk/app/ottertube-desktop | grep 'not found' || true)
[ -z "$missing" ] && ok "TC-1622 every shared library resolves from packages.list" || bad "TC-1622 missing: $missing"

# ── late-command 5, as the user-data does it ─────────────────────────────────
install -D -m 0644 /kids/sway.config /etc/kids-kiosk/sway.config
install -m 0755 /kids/kids-firstboot.sh /usr/local/sbin/kids-firstboot.sh
for f in kids-kiosk.sh kids-outputs.sh kids-app-loop.sh; do install -m 0755 "/kids/$f" "/usr/local/bin/$f"; done

# ── TC-1623: firstboot renders the session config from kids.env ──────────────
CATALOG=https://catalog.example.test/ottertube/catalog.json
printf '%s\n' "KIDS_TOUCH_OUTPUT=HEADLESS-1" "KIDS_APP_CMD=/opt/kids-kiosk/app/ottertube-desktop" \
    "KIDS_OTTERTUBE_CATALOG=$CATALOG" > /etc/kids-kiosk/kids.env
chmod 0600 /etc/kids-kiosk/kids.env
if KIDS_ROOT=/ /usr/local/sbin/kids-firstboot.sh > /tmp/firstboot.log 2>&1; then ok "TC-1623 firstboot exit 0"; else bad "TC-1623 firstboot failed:"; cat /tmp/firstboot.log; fi
[ -f /var/lib/kids-kiosk/firstboot.done ] && ok "TC-1623 marker written" || bad "TC-1623 no marker"
grep -qx 'KIDS_TOUCH_OUTPUT=HEADLESS-1' /etc/kids-kiosk/kiosk.env && [ "$(stat -c %a /etc/kids-kiosk/kiosk.env)" = 644 ] \
    && ok "TC-1623 kiosk.env 0644 with the touch output" || bad "TC-1623 kiosk.env wrong: $(cat /etc/kids-kiosk/kiosk.env)"
cfg=/home/kiosk/.config/ottertube/config.json
[ "$(jq -r .catalog "$cfg" 2>/dev/null)" = "$CATALOG" ] && [ "$(stat -c '%U %a' "$cfg")" = "kiosk 600" ] \
    && ok "TC-1623 OtterTube config.json seeded, kiosk 0600" || bad "TC-1623 config.json wrong: $(stat -c '%U %a' "$cfg" 2>&1) $(cat "$cfg" 2>&1)"
grep -q -- '--autologin kiosk' /etc/systemd/system/getty@tty1.service.d/kids-autologin.conf \
    && ok "TC-1623 tty1 autologin drop-in" || bad "TC-1623 no autologin drop-in"
echo '{"catalog":"https://parent.example.test/x.json"}' > "$cfg"
KIDS_ROOT=/ /usr/local/sbin/kids-firstboot.sh > /dev/null 2>&1
[ "$(jq -r .catalog "$cfg")" = "https://parent.example.test/x.json" ] \
    && ok "TC-1623 a re-run leaves an existing config.json alone" || bad "TC-1623 a re-run overwrote config.json"
rm -f "$cfg"
jq -n --arg c "$CATALOG" '{catalog: $c}' > "$cfg"; chown kiosk:kiosk "$cfg"; chmod 600 "$cfg"

# ── TC-1627: the touch-output choice (hermetic, but jq is certain here) ──────
if bash /kids/tests/kids-outputs.test.sh > /tmp/unit.log 2>&1; then ok "TC-1627 kids-outputs.test.sh ($(grep -c '^PASS' /tmp/unit.log) checks)"; else bad "TC-1627 kids-outputs.test.sh:"; grep -v '^PASS' /tmp/unit.log; fi

# ── TC-1624..1626: the session, as the kiosk user ─────────────────────────────
runuser -u kiosk -- bash /lib-test/kids-session-scenario.sh main
FAIL=$((FAIL + $?))
runuser -u kiosk -- bash /lib-test/kids-session-scenario.sh tv-at-start
FAIL=$((FAIL + $?))
runuser -u kiosk -- bash /lib-test/kids-session-scenario.sh launcher-killed
FAIL=$((FAIL + $?))
echo "FAILURES=$FAIL"
[ "$FAIL" -eq 0 ]
