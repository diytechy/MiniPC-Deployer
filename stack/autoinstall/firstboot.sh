#!/usr/bin/env bash
# First-boot bring-up for the AWOW always-on core. Invoked once by
# homehub-firstboot.service after docker + network are up. Idempotent and loud.
#
# Steps ("flash → boot → everything up, zero clicks"):
#   1. Sanity: stack dir + .env exist and .env has been filled (not placeholders).
#   2. Materialize the oauth2-proxy allow-list from OAUTH2_PROXY_ALLOWED_EMAILS.
#   3. Q10.9 B+ ALL-IMAGES: docker-load EVERY stack image from the baked deploy
#      payload (deploy-payload/images/*.tar) so the box comes up "from infancy"
#      with zero registry dependency, versions pinned to the sim-validated set.
#      docker load is idempotent. If no payload is present, fall back LOUDLY to
#      the old pull-at-compose-up behaviour.
#   4. `docker compose up -d` — starts all services (images already loaded in step
#      3; any not baked are pulled here) with restart:unless-stopped, then
#      (4b) assert caddy can actually READ the kiosk config it is about to serve.
#   5. Wait for Technitium to be healthy, then run provision-technitium.sh
#      (zero-touch DNS: zone, split-horizon records, forwarders, blocklists);
#      then provision-actual.sh (A9d: set the minted Actual server password
#      on the un-bootstrapped server — idempotent no-op afterwards).
#   6. Point the HOST resolver at the local Technitium so the box itself uses it.
#   7. Stamp .provisioned.
#
# Re-running is safe: compose is declarative, provisioning is idempotent.
# AND IT DOES RE-RUN. `RemainAfterExit=yes` only stops a second start within ONE
# boot; the unit is WantedBy=multi-user.target, so it runs again after every
# reboot, and the runbook tells the operator to re-run it by hand. /opt/homehub/
# .provisioned is a STAMP, not a guard — nothing reads it. Any step that asks
# "was this already here?" must therefore ask it of an input, never of its own
# output, or the answer is yes from the second boot onward (step 3e, 2026-08-03).
set -euo pipefail

STACK_DIR="/opt/homehub/stack"
MARKER="/opt/homehub/.provisioned"
log() { echo "[firstboot] $*"; }

cd "$STACK_DIR" || { log "FATAL: $STACK_DIR missing"; exit 1; }

# ── 1. sanity ────────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    log "FATAL: $STACK_DIR/.env missing (autoinstall should have seeded it)"; exit 1
fi
if grep -q "REPLACE_WITH" .env; then
    log "WARNING: .env still contains REPLACE_WITH placeholders."
    log "The stack will start but TLS/auth/DNS may be wrong until you edit .env"
    log "and re-run: sudo /usr/local/sbin/homehub-firstboot.sh"
fi

# ── 2. oauth2-proxy allow-list (Q10.5) ───────────────────────────────────────
# Materialize authenticated-emails.txt (one account per line) from the
# comma/space-separated OAUTH2_PROXY_ALLOWED_EMAILS in .env. Gitignored output.
# DO NOT `source` .env. This used to be `set -a; . ./.env; set +a` and it is
# how the first successful install died, one second into first boot:
#
#     ./.env: line 43: $2: unbound variable
#
# .env is a docker-compose env file, NOT a shell script — every value is
# literal text. Sourcing it makes bash expand that text, and EVERY basic_auth
# hash here is bcrypt, i.e. starts with `$2a$14$`. Under `set -u` (line 23)
# the unbound `$2` aborts the script outright.
#
# Turning off `set -u` would be the WRONG fix and strictly more dangerous:
# `$2` and `$1` would expand to nothing, the hash would be SILENTLY corrupted,
# Caddy would reject every login, and nothing anywhere would say why. The hard
# failure was the good outcome.
#
# Compose reads .env itself, without shell expansion, so nothing else in this
# script needs it — OAUTH2_PROXY_ALLOWED_EMAILS is the only value used here.
# Read that one literally. Bonus: no longer exports every secret in the file
# into this process's environment as a side effect.
env_value() {
    local __v
    __v=$(sed -n "s/^[[:space:]]*$1[[:space:]]*=//p" .env | tail -n1)
    case "$__v" in
        \"*\") __v=${__v#\"}; __v=${__v%\"} ;;
        \'*\') __v=${__v#\'}; __v=${__v%\'} ;;
        # UNQUOTED: strip a trailing ` # comment`, as shell sourcing and
        # compose's own .env parser both do. `.env` documents values inline
        # (LAN_IP=0.0.0.0   # VMTEST: …), and keeping the comment made the
        # value unusable. A `#` with no space before it stays — it may be
        # part of a password.
        *) __v=${__v%%[[:space:]]#*}
           __v=${__v%"${__v##*[![:space:]]}"} ;;
    esac
    # Compose stores a literal '$' as '$$' because it interpolates .env values.
    # Collapse it back so this script sees what the containers receive.
    __v=${__v//\$\$/\$}
    printf '%s' "$__v"
}

ALLOWED_EMAILS="$(env_value OAUTH2_PROXY_ALLOWED_EMAILS)"
EMAILS_FILE="$STACK_DIR/oauth2-proxy/authenticated-emails.txt"
if [ -n "$ALLOWED_EMAILS" ]; then
    mkdir -p "$STACK_DIR/oauth2-proxy"
    printf '%s' "$ALLOWED_EMAILS" | tr ', ' '\n\n' | sed '/^$/d' > "$EMAILS_FILE"
    log "oauth2-proxy allow-list written ($(wc -l < "$EMAILS_FILE") account(s))"
elif [ ! -f "$EMAILS_FILE" ]; then
    log "WARNING: no OAUTH2_PROXY_ALLOWED_EMAILS set and no allow-list file — the"
    log "tracker will reject every sign-in until you populate it."
    : > "$EMAILS_FILE"   # empty file so the bind-mount is a file, not a dir
fi

# ── 3. load the baked image payload (Q10.9 B+ ALL-IMAGES) ─────────────────────
# A freshly-imaged AWOW carries every stack image as a docker-save tar in the
# deploy payload (built by vmtest/export-images.sh, versions pinned to the
# sim-validated set), so first boot needs ZERO registry/internet access for
# container images — including naglight:local, which has no registry home at all
# (Q10.2). `docker load` is idempotent: loading an image that already exists is a
# no-op. The tars ride inside deploy-payload/, so they land wherever the payload
# lands; check every layout the light + repacked ISO paths can produce.
# GRACEFUL DEGRADE: if NO payload is present (e.g. a seed built without images),
# log loudly and fall through to the pre-Q10.9 pull-at-compose-up behaviour.
shopt -s nullglob
IMAGES_DIR=""
for cand in \
    /opt/homehub/images \
    "$STACK_DIR/images" \
    /cdrom/deploy-payload/images \
    /media/deploy-payload/images; do
    tars=("$cand"/*.tar "$cand"/*.tar.zst)
    if [ -d "$cand" ] && [ "${#tars[@]}" -gt 0 ]; then
        IMAGES_DIR="$cand"; break
    fi
done

if [ -n "$IMAGES_DIR" ]; then
    tars=("$IMAGES_DIR"/*.tar "$IMAGES_DIR"/*.tar.zst)
    log "loading ${#tars[@]} baked image tar(s) from $IMAGES_DIR (Q10.9 B+ — zero-registry first boot)"
    loaded=0
    for tar in "${tars[@]}"; do
        name="$(basename "$tar")"
        case "$tar" in
            *.tar.zst)
                if command -v zstd >/dev/null 2>&1; then
                    if zstd -dc "$tar" | docker load 2>&1 | sed 's/^/[firstboot]   /'; then
                        loaded=$((loaded + 1))
                    else
                        log "WARNING: docker load failed for $name — compose will try to PULL this image instead"
                    fi
                else
                    log "WARNING: $name is zstd-compressed but zstd is not installed — skipping; compose will PULL this image"
                fi
                ;;
            *)
                if docker load -i "$tar" 2>&1 | sed 's/^/[firstboot]   /'; then
                    loaded=$((loaded + 1))
                else
                    log "WARNING: docker load failed for $name — compose will try to PULL this image instead"
                fi
                ;;
        esac
    done
    log "image payload: $loaded of ${#tars[@]} tar(s) loaded from $IMAGES_DIR"
else
    log "NOTICE: no baked image payload found (looked in /opt/homehub/images,"
    log "  $STACK_DIR/images, /cdrom/deploy-payload/images, /media/deploy-payload/images)."
    log "  Falling back to PULL-AT-COMPOSE-UP — 'docker compose up -d' fetches each image"
    log "  from its registry (needs internet). Pre-Q10.9-B+ behaviour; expected ONLY for a"
    log "  seed built WITHOUT vmtest/export-images.sh. naglight:local has no registry home,"
    log "  so the tracker will fail to start unless that image was staged some other way."
fi
shopt -u nullglob

# ── 3b. mount the storage-map data drives BEFORE anything bind-mounts into them ─
# ORDER IS LOAD-BEARING — this used to sit in step 5c, AFTER `docker compose
# up -d`, and that was a latent data-loss bug the moment any container bind-
# mounted a path under /srv/library:
#   docker CREATES a missing bind-mount source directory, on whatever filesystem
#   is there at container-start time. With the library unmounted that is the
#   SYSTEM disk; ntfs3 then mounts over /srv/library and SHADOWS it. The
#   container keeps writing to the now-invisible directory on the 119 GB system
#   disk, and every read through the share sees an empty library. Silent, and it
#   only surfaces when the system disk fills.
# It was harmless while MEDIA_ROOT defaulted to /srv/media and nothing mounted
# inside the library; enabling the media profiles (immich writes ${MEDIA_ROOT}/
# immich, jellyfin reads the video library) makes it live. Same reasoning the
# Samba block below already states for `root preexec` — it just has to happen
# before compose, not after.
#
# Not fatal when a drive is absent: `nofail` in the generated fstab options, and
# provision-mounts.sh exits 0 with a loud log on a sim/vmtest build that carries
# no fragment at all.
log "mounting storage-map data drives (before compose — bind sources must be real)…"
bash "$STACK_DIR/provision/provision-mounts.sh" || \
    log "WARN: drive mounting reported a problem — see above"

# ── 3c. hardware-conditional compose overrides ───────────────────────────────
# Jellyfin's Quick Sync passthrough needs /dev/dri, and a `devices:` entry for a
# node that does not exist makes container CREATION fail outright — which would
# take the whole `docker compose up -d` down with it on any box without an iGPU.
# Generated here instead of being hardcoded in docker-compose.yml, so one stack
# file works on every box.
#   Note (measured 2026-08-01): a Hyper-V VM is NOT such a box — hyperv_drm
#   gives it /dev/dri with a card node but no renderD*, so the override is
#   written there too and simply has no VA-API to offer. Do not read "the gate
#   VM passed" as proof the no-device branch works; that has its own test.
bash "$STACK_DIR/provision/provision-compose-overrides.sh" || \
    log "WARN: compose override generation failed — Jellyfin (if enabled) runs without QSV"

# ── 3d. unpack the wall kiosk site's document root (IF-005 / PKG-1) ──────────
# `stack/wall-shell/` is empty in the repo by design — the "no product source"
# constraint — but caddy bind-mounts it read-only as /srv/wall-shell and serves
# it as the {$WALL_HOST}:{$WALL_PORT} site's document root (SR-016). The panel's
# renderer therefore has to ARRIVE, the way naglight:local does: as a built
# artifact in the payload.
#
# BEFORE compose up, deliberately. The mount is read-only from caddy's side, so
# unpacking afterwards would work — but a caddy that starts against an empty
# docroot serves 404 at / for however long that takes, and "the panel showed
# nothing" is a symptom with too many possible causes to want an extra one.
#
# Absent is FINE and expected on a public checkout (OfficeWallNaglight is
# private): the site then serves 404 at / while /api/* works, which is the
# documented half-built state. It is NOT fine for the A19 panel gate, so say so.
WALL_SITE_TARBALL=""
# Does the ARCHIVE carry a config.json of its own? Answered from the archive's
# LISTING, before anything is extracted — see step 3e, which is where the answer
# is used and where the reason it must be asked here is written out.
WALL_TARBALL_SHIPS_CONFIG=no
shopt -s nullglob
for cand in /opt/homehub/wall-site "$STACK_DIR/wall-site" /cdrom/deploy-payload/wall-site; do
    if [ -z "$WALL_SITE_TARBALL" ]; then
        for t in "$cand"/officewall-site-*.tar.gz; do WALL_SITE_TARBALL="$t"; break; done
    fi
done
shopt -u nullglob
if [ -n "$WALL_SITE_TARBALL" ]; then
    # NOT `tar -tzf … | grep -q`. This script runs under `set -o pipefail`
    # (line 23): `grep -q` exits at the FIRST match, `tar` then dies of SIGPIPE
    # (141), and the pipeline's status is that failure — so the pipeline reports
    # FAILURE exactly when the answer is YES, and `set -e` kills the script on
    # the good path. This repo has been bitten by that shape before. Write the
    # listing to a file, then grep the file: one command, no pipe, no ambiguity.
    # The member we care about is `<top>/config.json`, because the untar below
    # passes --strip-components=1 and only that depth lands on wall-shell/.
    _wall_listing="$(mktemp)"
    if tar -tzf "$WALL_SITE_TARBALL" > "$_wall_listing" 2>/dev/null; then
        if grep -Eq '^(\./)?[^/]+/config\.json$' "$_wall_listing"; then
            WALL_TARBALL_SHIPS_CONFIG=yes
        fi
    else
        log "WARN: could not list $WALL_SITE_TARBALL — cannot tell whether it ships a config.json of its own"
    fi
    rm -f "$_wall_listing"
    # --strip-components=1 drops the archive's leading site/ (packaging.md §4).
    log "unpacking the wall kiosk site: $(basename "$WALL_SITE_TARBALL") -> $STACK_DIR/wall-shell/"
    install -d -m 0755 "$STACK_DIR/wall-shell"
    if tar -xzf "$WALL_SITE_TARBALL" -C "$STACK_DIR/wall-shell" --strip-components=1; then
        [ -f "$STACK_DIR/wall-shell/index.html" ] || \
            log "WARN: the site payload unpacked but has no index.html — the kiosk site will 404 at /"
        if [ -f "$STACK_DIR/wall-shell/build-info.json" ]; then
            # The stamp both halves share: a hub and a panel whose revisions
            # differ are a mismatched deploy (packaging.md §2.1), and this is
            # the line that makes that visible instead of invisible.
            log "  wall site build: $(sed -n 's/.*"revision"[^"]*"\([0-9a-f]\{12\}\).*/\1/p' "$STACK_DIR/wall-shell/build-info.json" | head -n1)"
        fi
    else
        log "WARN: could not unpack $WALL_SITE_TARBALL — the kiosk site keeps serving 404 at /"
    fi
else
    log "NOTICE: no wall kiosk site payload found (looked in /opt/homehub/wall-site,"
    log "  $STACK_DIR/wall-site, /cdrom/deploy-payload/wall-site). The {\$WALL_HOST} site"
    log "  will serve 404 at / while its /api/* proxy works — the documented half-built"
    log "  state on a checkout without OfficeWallNaglight. A wall PANEL pointed at this"
    log "  hub would render nothing, so the A19 gate needs an image built WITH it."
fi

# ── 3e. the kiosk site's RUNTIME CONFIG (config.json) — AFTER the untar ───────
# The shell fetches `./config.json` RELATIVE TO ITS OWN ORIGIN (js/config.js
# loadConfig), and that origin is this hub: Caddy's {$WALL_HOST}:{$WALL_PORT}
# site serves `root * /srv/wall-shell`, bind-mounted from the directory step 3d
# just filled. The panel's Electron host intercepts /media/* and nothing else,
# so a copy on the panel's disk would never be read. It is a HUB artifact.
#
# AFTER step 3d, DELIBERATELY, and this is the whole reason it is not a
# late-command like the other seven site files. `tar -xzf … -C wall-shell/`
# above writes over that directory; today's site tarball ships no config.json,
# so the ordering is benign — but "benign today" is a promise about a private
# sibling repo's future releases, and one that starts shipping a
# config.example.json-shaped config.json would silently overwrite the real
# credentials with placeholders. Copying afterwards makes the ordering correct
# by construction rather than by agreement, and the collision (if it ever
# happens) is reported rather than assumed away.
#
# WHY LOSING IT IS SILENT, which is why this block is loud: loadConfig NEVER
# THROWS. A 404 or a parse error yields the js/config.js DEFAULTS plus a
# console.warn nobody on a wall can see — so the panel comes up looking like it
# works, with no FEED_TOKEN (its feed posts are unattributed), no heartbeat, and
# no music credentials.
#
# MODE: 0600 root:root — the same posture as the other site files, and it works
# because the caddy container runs as uid 0 (measured on the pinned
# caddy:2.11.4-alpine: no USER in the image, no 'user:' in docker-compose.yml,
# no userns-remap), so the read-only bind mount reaches it as root. It is the
# most restrictive mode that serves. THE MODE IS OWED A RULING (docs/status.md
# OI-20): this file carries FEED_TOKEN, a Kuma push token and the Subsonic
# password onto an HTTP surface — one guarded by the kiosk site's
# `remote_ip {$PANEL_IP}/32` matcher, so it is not open to the LAN, but "a
# secret is served over HTTP behind an IP allow-list" is a posture, not a
# detail. If caddy ever gains a `user:` the file becomes unreadable and the
# panel degrades to defaults SILENTLY — which is exactly the failure this
# comment exists to make findable.
#
# THE COLLISION IS DETECTED FROM THE ARCHIVE, NOT FROM THE DESTINATION. This
# block used to ask `[ -f "$STACK_DIR/wall-shell/config.json" ]` AFTER the
# untar, which answers a different question than the one it claimed to: this
# service has no marker guard and no ConditionPath* (homehub-firstboot.service
# is a RemainAfterExit oneshot, which only stops it re-running within ONE boot —
# the unit is WantedBy=multi-user.target and starts again after every reboot,
# and the script is documented as safe to re-run by hand). On the second run the
# config.json sitting there is the one THIS STEP installed on the first run, so
# the test was true every time and the NOTE below cried "the tarball shipped its
# own config.json" on every reboot of every hub, forever, whether or not it ever
# had. A warning that is always on is a warning nobody reads — which is how the
# real collision, the one this exists to catch, would have gone past unnoticed.
# Step 3d now records the answer from `tar -tzf` before extracting.
WALL_SITE_CONFIG="/opt/homehub/site/config.json"
if [ -f "$WALL_SITE_CONFIG" ]; then
    if [ "$WALL_TARBALL_SHIPS_CONFIG" = yes ]; then
        log "NOTE: the site tarball shipped its own config.json — replacing it with the"
        log "  materialised one. If that was a real file rather than a placeholder, the"
        log "  two are now competing: check OfficeWallNaglight's release contents."
    fi
    install -d -m 0755 "$STACK_DIR/wall-shell"
    install -m 0600 -o root -g root "$WALL_SITE_CONFIG" "$STACK_DIR/wall-shell/config.json"
    log "kiosk site config installed: $STACK_DIR/wall-shell/config.json (0600 root:root,"
    log "  served over HTTP to the panel only — carries FEED_TOKEN; mode owed a ruling)"
elif [ -n "$WALL_SITE_TARBALL" ]; then
    log "NOTICE: the kiosk site is installed but there is no site/config.json in the"
    log "  payload, so the site will 404 on it and the panel runs on js/config.js"
    log "  DEFAULTS — no FEED_TOKEN, no heartbeat, no music credentials, and nothing"
    log "  on the wall saying so. Expected on a SIM build. On a PRODUCTION hub it means"
    log "  Materialize-Deploy.ps1 -Image homehub has not been re-run since config.json"
    log "  was added, or Build-VentoyStick.ps1's site\\ list does not carry it yet."
fi

# ── 4. bring the stack up ────────────────────────────────────────────────────
# Images were loaded from the payload in step 3 (Q10.9 B+). compose finds each
# pinned tag locally and starts it without a pull; anything NOT baked (or a
# no-payload fallback) is pulled here — which for naglight:local (no registry,
# Q10.2) means it must have been built/staged first.
# ── 4a-pre. THE ONE .env VALUE THAT NOW STOPS CADDY FROM STARTING AT ALL ──────
# Since the switch to dns-01 (2026-08-08), the Caddyfile's global block carries
#     acme_dns cloudflare {$CLOUDFLARE_API_TOKEN}
# and Caddy REFUSES TO ADAPT ITS CONFIG when that value is empty:
#     Error: parsing caddyfile tokens for 'acme_dns': missing API token
# Measured, not guessed — run against the built image before this shipped.
#
# That is a worse failure than the one dns-01 fixes if it is allowed to surface
# as "caddy keeps restarting": no tracker, no Actual, no DNS console, no kiosk
# site, and a container log nobody reads on a headless box. The materialiser
# cannot produce this state (CLOUDFLARE_API_TOKEN is a T3 store key and a missing
# one is a hard build failure), so the only route here is a hand-edited .env —
# which is exactly the case that deserves a sentence rather than a crash loop.
#
# NOT FATAL. The rest of the stack is fine without Caddy and the box stays
# reachable; saying so precisely beats refusing to boot.
if ! grep -qE '^CLOUDFLARE_API_TOKEN=.+' "$STACK_DIR/.env" 2>/dev/null; then
    log "WARN: CLOUDFLARE_API_TOKEN is empty or absent in .env."
    log "  Caddy will FAIL TO START — the Caddyfile uses the dns-01 ACME challenge and"
    log "  the provider needs that token. Every HTTPS site goes with it: the tracker,"
    log "  Actual, the DNS console, the apex and the kiosk site the wall panel displays."
    log "  Fix: put the scoped Zone->DNS->Edit token in .env, then re-run this script."
fi

# ── 4a-pre-2. CREATE THE BIND-MOUNT SOURCES ON THE DATA DRIVES ───────────────
# FOUND 2026-08-08, and it took the whole stack down on a box that had installed
# perfectly:
#
#   Error response from daemon: error while creating mount source path
#   '/srv/library/NonDocs/Media/Movies': chown ...: operation not permitted
#
# Docker creates a missing bind-mount source directory and then CHOWNS it. The
# data drives are exfat (A23 chose LABEL mounts so the first days of service can
# run on plain flash drives, and those are FAT-family filesystems with no
# ownership at all), so the chown returns EPERM and `docker compose up -d`
# ABORTS — leaving eight containers in `created`, Caddy among them, and firstboot
# dead before it provisioned DNS, Samba or anything else. Every downstream
# failure that run traced back to this one line.
#
# THE FIX IS ORDERING, NOT PERMISSIONS. Docker only chowns a path it had to
# create; a directory that already exists is left exactly alone. Proven on the
# live box: pre-create, re-run `up`, and all fifteen containers start.
#
# NOT A LAB ARTIFACT. The fstab options for these mounts are uid/gid/umask —
# which only exist on filesystems without native ownership — so the real drives
# are the same family. A first install onto an empty drive hits this identically;
# the lab's stand-in just gets there first because it has no directories at all.
#
# SOURCED FROM COMPOSE ITSELF rather than a list retyped here. `docker compose
# config` resolves every variable and prints bind mounts in long form, so this
# cannot drift from the file it is protecting — the rule packages.list follows.
# Restricted to the data mounts on purpose: creating an arbitrary missing bind
# source would paper over a genuinely absent Caddyfile or allow-list.
log "ensuring bind-mount sources exist on the data drives (docker cannot chown them into being)…"
_created=0
while read -r _src; do
    case "$_src" in
        /srv/library/*|/mnt/backup-drive/*)
            if [ ! -d "$_src" ]; then
                mkdir -p "$_src" && _created=$((_created + 1))
                log "  created $_src"
            fi ;;
    esac
done <<EOF
$(docker compose config 2>/dev/null | awk '/^ *source: \//{print $2}' | sort -u)
EOF
log "  bind-mount sources checked; $_created created"

# ── 4a-pre-3. DOES THE KIOSK SITE HAVE AN IDENTITY TO INJECT? ────────────────
# PANEL_USER_SUB empty is a DELIBERATE fail-closed state, not a bug:
# Materialize-Deploy.ps1 blanks the REPLACE_WITH_… placeholder on purpose,
# because NagLight rejects only an EMPTY trusted-identity header — a non-empty
# placeholder would be accepted as a perfectly valid user id and mint a phantom
# identity on an unauthenticated trust path. That reasoning is right.
#
# WHAT IS WRONG IS THE SILENCE. Measured on a live panel 2026-08-08: the kiosk
# site serves `/` and `/config.json` with a 200, the shell loads and paints, and
# then EVERY `/api/*` call returns 403 because Caddy injected an empty
# X-Forwarded-User. The wall shows its frame with no data in it, forever, and
# nothing on the hub, the panel or the wall says why. It also makes the
# highest-value security test in the plan (TC-H-G05, the forged-header strip)
# unreachable — there is no identity to compare a forged one against.
#
# So: fail closed, and SAY SO. Not fatal — a hub with no panel is a legitimate
# configuration, and this is the wrong place to refuse a boot over it.
if grep -qE '^WALL_HOST=.+' "$STACK_DIR/.env" 2>/dev/null \
   && ! grep -qE '^PANEL_USER_SUB=.+' "$STACK_DIR/.env" 2>/dev/null; then
    log "WARN: WALL_HOST is configured but PANEL_USER_SUB is EMPTY."
    log "  The kiosk site will serve the shell and then 403 every /api/* request:"
    log "  Caddy injects X-Forwarded-User from this value, and the tracker refuses"
    log "  an empty identity (correctly — that is what makes 'optional' fail closed)."
    log "  The wall will paint, and show no data, and nothing else will report it."
    log "  Fix: put the Owner's Google 'sub' in config.common.psd1's PANEL_USER_SUB"
    log "  and re-materialise. It cannot be invented here; it is an Owner value."
fi

# ── 4a-pre-4. IS THE ADDRESS COMPOSE PUBLISHES TO ACTUALLY ON THIS BOX YET? ──
# EVERY PUBLISHED PORT IN docker-compose.yml IS BOUND TO ${LAN_IP}, NOT TO
# 0.0.0.0 — that is deliberate (D5: the tier-2 surfaces must be structurally
# unreachable from the WAN, not merely firewalled). The cost of that choice is
# that `docker compose up -d` CANNOT START AT ALL until the address exists:
#
#     failed to bind host port <lan ip>:8096/tcp: cannot assign requested address
#
# and because `up -d` is all-or-nothing, ONE unbindable port leaves the whole
# stack in `created`. Measured 2026-08-08: eight containers stranded, Caddy
# among them, so no DNS, no certificates, no kiosk site — from one missing
# address.
#
# THIS IS NOT A LAB ARTIFACT, though the lab is where it was found. The lab
# installs with the adapter disconnected and reconnects afterwards, so firstboot
# began with no carrier at all. On real hardware the same window exists whenever
# the box finishes booting before DHCP hands out its lease — a slow switch, a
# port coming out of STP learning, an AP that has not finished associating. The
# unit already orders itself After=network-online.target and that is NOT enough:
# systemd-networkd-wait-online TIMES OUT after its deadline and boot proceeds,
# so network-online.target is "reached" with no address on the interface. Seen
# in the same journal: "Timeout occurred while waiting for network connectivity."
#
# So wait for the thing we actually depend on — the ADDRESS, not the target —
# and if it never arrives, say which address was missing. A named failure here
# is worth far more than the opaque docker bind error it replaces, because that
# error names a container (jellyfin) that has nothing to do with the cause.
#
# LAN_IP=0.0.0.0 is a legitimate configuration (the sim/vmtest overlay sets it
# to publish on every interface); there is nothing to wait for in that case.
# \042 is a double quote and \047 a single one, given in octal so this line
# needs no nested quoting of its own — the obvious spelling of it does not
# survive being written inside a command substitution.
__lan_ip="$(sed -n 's/^LAN_IP=//p' "$STACK_DIR/.env" 2>/dev/null | tail -n1 \
            | sed 's/[[:space:]]*#.*$//' | tr -d '\042\047[:space:]')"
if [ -n "$__lan_ip" ] && [ "$__lan_ip" != "0.0.0.0" ]; then
    __waited=0
    while ! ip -4 -o addr show 2>/dev/null | grep -qwF "$__lan_ip"; do
        if [ "$__waited" -ge 180 ]; then
            log "FATAL: $__lan_ip is not held by any interface after ${__waited}s."
            log "  Every published port in docker-compose.yml binds to that address, and"
            log "  'docker compose up -d' is all-or-nothing — it would fail with"
            log "  'cannot assign requested address' naming an arbitrary container."
            log "  The box has no LAN address, so this is a DHCP/link problem, not a"
            log "  stack problem. Check:  ip -4 addr ; networkctl status ; the cable."
            exit 1
        fi
        [ "$__waited" -eq 0 ] && log "waiting for $__lan_ip to appear on an interface (compose publishes to it)…"
        sleep 5
        __waited=$((__waited + 5))
    done
    [ "$__waited" -gt 0 ] && log "  $__lan_ip appeared after ${__waited}s"
fi

log "docker compose up -d…"
docker compose up -d

# ── 4a-post. DID THEY ACTUALLY STAY UP? ──────────────────────────────────────
# `docker compose up -d` returns 0 once containers are STARTED. A container that
# starts and then dies on its own configuration exits 0 here and restart-loops
# quietly afterwards — which is exactly what a bad ACME provider token did on
# 2026-08-08: Caddy refused its own config, `up` reported success, and the box
# came up with every web surface dead and nothing in firstboot's log about it.
#
# So: give them a moment to fall over, then name any that did. This catches the
# whole class — a bad token, a missing bind source, an image that will not run on
# this kernel — rather than the one instance that prompted it.
#
# NOT FATAL, deliberately. The box is reachable, the data is mounted, and the
# rest of provisioning is still worth doing; a named WARN in the journal and a
# non-zero unit at the end serve better than refusing to finish.
sleep 15
_notrunning=""
for _c in $(docker compose ps --services 2>/dev/null); do
    _st="$(docker inspect -f '{{.State.Status}}' "$_c" 2>/dev/null || echo missing)"
    case "$_st" in
        running|exited) ;;   # exited is correct for one-shot services
        *) _notrunning="$_notrunning $_c($_st)" ;;
    esac
done
if [ -n "$_notrunning" ]; then
    log "WARN: container(s) did not reach a running state:$_notrunning"
    log "  'docker compose up -d' returned success — it reports STARTED, not STAYED UP."
    log "  A container that rejects its own configuration looks exactly like this."
    log "  Read:  docker logs <name> --tail 30"
else
    log "every compose service reached a running state"
fi

# ── 4b. CAN CADDY ACTUALLY READ THE KIOSK CONFIG? (OI-20) ────────────────────
# Installing a 0600 root-owned file into a bind mount is not the same as the
# server being able to open it, and NOTHING here checked the difference. The
# caddy healthcheck probes the admin API on :2019, which is up whenever the
# process is — so an unreadable config.json leaves caddy `healthy`, the site
# answers 403/404 for /config.json, and `loadConfig` NEVER THROWS: the panel
# paints on js/config.js DEFAULTS with no FEED_TOKEN, no heartbeat and no music
# credentials, and nothing anywhere says so. Exactly the failure the 0600 ruling
# (OI-20) is owed a decision about, and the one step 3e's comment predicts.
#
# It reads today because the pinned caddy:2.11.4-alpine declares no USER,
# docker-compose.yml sets no `user:` for caddy, and the daemon has no
# userns-remap — so the container is uid 0. Any ONE of those changing breaks it
# silently. `:ro` and `read_only:` would NOT: both restrict WRITES, and this is
# a read. So assert the read itself, and do it AS THE CONTAINER: `docker exec`
# inherits the service's user, so this fails precisely when caddy would fail.
#
# Only asserted when the PRODUCTION source exists. A sim/vmtest hub legitimately
# has no site/config.json; step 3e's NOTICE already covers that case.
KIOSK_CONFIG_UNREADABLE=0
if [ -f "$WALL_SITE_CONFIG" ]; then
    for _i in $(seq 1 30); do
        [ "$(docker inspect -f '{{.State.Running}}' caddy 2>/dev/null || true)" = "true" ] && break
        sleep 2
    done
    if docker exec caddy sh -c 'head -c 1 /srv/wall-shell/config.json > /dev/null 2>&1'; then
        log "kiosk config readable INSIDE the caddy container ($(docker exec caddy id -u 2>/dev/null || echo '?') = the uid it serves as)"
    else
        KIOSK_CONFIG_UNREADABLE=1
        log "ERROR: caddy CANNOT read /srv/wall-shell/config.json."
        log "  The file is installed 0600 root:root and the container is running as"
        log "  uid $(docker exec caddy id -u 2>/dev/null || echo '?') — a 'user:' in docker-compose.yml, a USER in a newer caddy"
        log "  image, or daemon userns-remap will each do this. The kiosk site will"
        log "  serve 403/404 for /config.json, loadConfig will NOT throw, and the panel"
        log "  will paint on js/config.js DEFAULTS: no FEED_TOKEN (its feed posts are"
        log "  unattributed), no heartbeat, no music credentials. Fix the ownership/mode"
        log "  to match the uid above, or give caddy back uid 0. See OI-20."
    fi
fi

# ── 5. Technitium zero-touch provisioning ────────────────────────────────────
log "waiting for Technitium API on :5380…"
for i in $(seq 1 60); do
    if curl -fsS -o /dev/null http://127.0.0.1:5380/ 2>/dev/null; then break; fi
    sleep 2
done
log "provisioning Technitium (zone + split-horizon + forwarders + blocklists)…"
bash "$STACK_DIR/provision/provision-technitium.sh" --env "$STACK_DIR/.env"

# ── 5b. Actual zero-touch bootstrap (A9d) ────────────────────────────────────
# Sets the minted server password on the un-bootstrapped Actual so no UI step
# stands between first boot and a working Finance-Auditor. Idempotent: an
# already-bootstrapped server is a logged no-op.
log "bootstrapping Actual (server password from FINANCE_ACTUAL_PASSWORD)…"
bash "$STACK_DIR/provision/provision-actual.sh" --env "$STACK_DIR/.env"

# ── 5c. The Samba file server (A14 + A13) ────────────────────────────────────
# ORDER IS LOAD-BEARING:
#   mounts  -> the library must be a real mountpoint before anything exports a
#              path inside it, or Samba serves an empty dir on the eMMC and
#              silently accepts writes to the system disk. MOVED UP to step 3b:
#              docker bind mounts need the same guarantee and compose runs in
#              step 4, so mounting here was already too late for them.
#   samba   -> assembles smb.conf (tracked [global] + generated share stanzas)
#              and starts smbd. Creates the Samba databases smbpasswd needs.
#   users   -> one identity per storage-map §2 entry, each with its OWN
#              password, which is what makes §3's per-share ACLs real.
# All three are no-ops when their site files were not shipped (sim/vmtest).
# The mount guard must exist BEFORE the shares reference it: every share
# stanza names it as `root preexec`, and a missing guard would make Samba
# refuse every connection (preexec close = yes treats "cannot run" as failure).
install -m 0755 "$STACK_DIR/samba/library-guard.sh" /usr/local/sbin/homehub-library-guard
install -m 0644 "$STACK_DIR/samba/homehub-library-health.service" /etc/systemd/system/homehub-library-health.service
install -m 0644 "$STACK_DIR/samba/homehub-library-health.timer"   /etc/systemd/system/homehub-library-health.timer
# Same guard, second drive (A21): the backup target had no presence check at all
# — it was looked at once a night by the backup run, which (fstab `nofail`) could
# not tell an absent drive from an empty directory on the system disk.
# A23: the serials the guard asserts. Carried on the USB as site/, copied to
# /etc/homehub-samba/ beside the fstab fragment it pairs with. Absent = the
# guard still reports, it just cannot distinguish a stand-in from the real disk
# and names that gap in the check note.
if [ -f /etc/homehub-samba/drive-identity.conf ]; then
    log "drive identity assertions present — health checks will flag stand-in drives"
else
    log "no /etc/homehub-samba/drive-identity.conf — health checks report presence only (a stand-in drive will read as healthy)"
fi
install -m 0644 "$STACK_DIR/samba/homehub-backup-drive-health.service" /etc/systemd/system/homehub-backup-drive-health.service
install -m 0644 "$STACK_DIR/samba/homehub-backup-drive-health.timer"   /etc/systemd/system/homehub-backup-drive-health.timer

# ── the A19 GATE's feed configuration, and only ever a gate's ────────────────
# Both drive lanes take their feed settings from backup.env. A production hub
# gets that file from the household materialiser (late-command 4b); a SIM hub
# gets none, so on 2026-08-04's gate the library lane logged "NAGLIGHT_FEED_URL
# unset — journal only" and the backup lane exited before it checked. Correct
# for an unprovisioned box, and it made A19's whole assertion unreachable.
#
# `sim-gate/` is GENERATED by vmtest's lab builder and is not a tracked path, so
# a production payload cannot contain one. It is still refused explicitly if a
# real backup.env is already installed — a sim fixture must never overwrite a
# household's own configuration, whatever route put it here.
SIM_GATE_BACKUP_ENV="$(dirname "$STACK_DIR")/sim-gate/backup.env"
if [ -f "$SIM_GATE_BACKUP_ENV" ]; then
    if [ -f /etc/homehub-backup/backup.env ]; then
        log "ERROR: this payload carries a SIM GATE backup.env AND this box already has a real"
        log "  one at /etc/homehub-backup/backup.env. Refusing to overwrite it. A gate fixture"
        log "  on a provisioned hub would repoint its drive reports at a test identity."
    else
        install -d -m 0755 /etc/homehub-backup
        install -m 0600 -o root -g root "$SIM_GATE_BACKUP_ENV" /etc/homehub-backup/backup.env
        log "SIM GATE: installed a test backup.env so the two drive-presence lanes REPORT"
        log "  instead of logging 'journal only'. THIS IS A GATE FIXTURE, not household config."
    fi
fi

systemctl daemon-reload

if [ -f /etc/homehub-samba/smb.conf.fragment ]; then
    log "bringing up the Samba file server…"
    if bash "$STACK_DIR/provision/provision-samba.sh"; then
        log "provisioning Samba household accounts…"
        bash "$STACK_DIR/provision/provision-samba-users.sh" || \
            log "WARN: Samba account provisioning failed — private shares will be unreachable"
    else
        log "WARN: Samba server did not come up — every §3 share is unreachable. Accounts skipped."
    fi
elif [ -f /etc/homehub-samba/.site-present ]; then
    # The USB DID carry a site/ payload, but the fragment did not survive the
    # install. That is a broken production stick, not a sim build - and the
    # reassuring "expected on sim builds" message below would be a lie.
    log "FATAL: site payload was present but /etc/homehub-samba/smb.conf.fragment is missing."
    log "  Every §3 share is unreachable. The late-command copy failed or the"
    log "  payload was incomplete. Rebuild with Build-VentoyStick.ps1 and re-image."
else
    log "no /etc/homehub-samba/smb.conf.fragment and no site payload marker —"
    log "  skipping the file server (A14). Expected ONLY on a sim/vmtest build."
fi

# Enable the health timer AFTER mounting, not before: OnBootSec=3min has long
# elapsed by the time firstboot runs, so `enable --now` fires immediately and
# would post a spurious library-mounted ok=false against an unmounted library.
systemctl enable --now homehub-library-health.timer >/dev/null 2>&1 ||     log "WARN: could not enable homehub-library-health.timer — a vanished drive would go unreported"
systemctl enable --now homehub-backup-drive-health.timer >/dev/null 2>&1 || \
    log "WARN: could not enable homehub-backup-drive-health.timer — an absent backup drive would go unreported until the nightly run"

# ── 6. make the host itself use local DNS, and give Technitium the whole port ─
#
# DNSStubListener=no IS THE LOAD-BEARING LINE, and it was missing until
# 2026-08-08. Without it systemd-resolved keeps its stub on 127.0.0.53:53 —
# TCP AND UDP — and it is already running when docker starts Technitium. A TCP
# bind of 0.0.0.0:53 conflicts with any specific-address bind on the same port,
# so Technitium's IPv4 TCP listener FAILED. Its UDP sockets carry SO_REUSEADDR
# and bound fine, and it logged nothing at all about the half that did not.
#
# WHAT THAT LOOKED LIKE, on a box everyone would have called healthy:
#     dig  @<lan ip> tracker.<domain>          -> answers
#     dig +tcp @<lan ip> tracker.<domain>      -> connection refused
# i.e. every response over 512 bytes fails, plus every deliberate TCP query,
# on a resolver that passes every casual test. MEASURED, then fixed and
# re-measured on the same running box: with the stub off and Technitium
# restarted, 0.0.0.0:53/tcp appeared and dig +tcp answered immediately.
# verify-hub.sh's TC-H-C01 is the assertion that keeps it honest.
#
# THE STUB IS WHAT /etc/resolv.conf POINTS AT, so turning it off has to be paid
# for: resolved regenerates stub-resolv.conf listing the configured servers
# instead, which is 127.0.0.1 — Technitium itself, which is what this step
# claims to be arranging anyway. Verified on the box: /etc/resolv.conf came back
# as `nameserver 127.0.0.1` and getent kept resolving.
if systemctl is-active --quiet systemd-resolved; then
    mkdir -p /etc/systemd/resolved.conf.d
    cat > /etc/systemd/resolved.conf.d/homehub.conf <<'EOF'
[Resolve]
DNS=127.0.0.1
Domains=~.
# Technitium owns :53 on this box. Leaving the stub up costs the IPv4 TCP
# listener, silently — see firstboot.sh step 6.
DNSStubListener=no
EOF
    systemctl restart systemd-resolved || true
    log "host resolver pointed at local Technitium (resolved stub off — :53 is Technitium's)"

    # THE STUB WAS UP WHILE TECHNITIUM STARTED, so on THIS boot the container is
    # already missing its IPv4 TCP socket; freeing the port does not retroactively
    # bind it. Every later boot is fine (resolved reads the drop-in before docker
    # starts), which is exactly the shape of bug that hides — the box is correct
    # the second time anyone looks. So: check, remediate once, and say which.
    if command -v docker >/dev/null 2>&1 && docker inspect technitium >/dev/null 2>&1; then
        tcp53_ok() { (exec 3<>/dev/tcp/127.0.0.1/53) 2>/dev/null; }
        if tcp53_ok; then
            log "DNS answers on :53/tcp"
        else
            log "restarting Technitium so it can claim :53/tcp (the resolved stub held it until just now)"
            docker restart technitium >/dev/null 2>&1 || true
            for _ in 1 2 3 4 5 6 7 8 9 10; do tcp53_ok && break; sleep 2; done
            if tcp53_ok; then
                log "DNS answers on :53/tcp"
            else
                log "WARN: :53/tcp still refuses after restarting Technitium."
                log "  UDP works, so this box LOOKS like a healthy resolver — but every"
                log "  response over 512 bytes and every TCP query will fail. Check what"
                log "  else holds the port:  ss -ltnp '( sport = :53 )'"
            fi
        fi
    fi
fi

# ── 7. done ──────────────────────────────────────────────────────────────────
date > "$MARKER"
# A defect found in step 4b is reported HERE, at the end, and as a NON-ZERO
# EXIT: everything else still ran (the stack is up, DNS is provisioned, the
# shares are mounted), but `systemctl status homehub-firstboot` must be RED and
# `systemctl is-failed` must say so. On a headless box the unit's state is the
# only surface a defect can appear on that is not a line in a scrolling journal.
if [ "$KIOSK_CONFIG_UNREADABLE" -ne 0 ]; then
    log "FATAL: bring-up finished, but the wall panel WILL run on js/config.js defaults"
    log "  (see the step 4b ERROR above). Exiting non-zero so this unit reports FAILED"
    log "  rather than letting a silently-degraded panel look like a clean first boot."
    exit 1
fi
log "bring-up complete. Verify with: bash $STACK_DIR/provision/healthcheck.sh"
