#!/usr/bin/env bash
# First-boot bring-up for the AWOW always-on core. Invoked once by
# homehub-firstboot.service after docker + network are up. Idempotent and loud.
#
# Steps ("flash → boot → everything up, zero clicks"):
#   1. Sanity: stack dir + .env exist and .env has been filled (not placeholders).
#   2. Materialize the oauth2-proxy allow-list from OAUTH2_PROXY_ALLOWED_EMAILS.
#   3. Q10.9 B+ ALL-IMAGES: docker-load every stack image from the baked deploy
#      payload (deploy-payload/images/*.tar) THAT IS NOT ALREADY PRESENT, so the
#      box comes up "from infancy" with zero registry dependency, versions pinned
#      to the sim-validated set. `docker load` is NOT idempotent in the way this
#      line used to claim — it RE-POINTS an existing tag — so the step asks the
#      tar whether its repo:tag is already here. If no payload is present, fall
#      back LOUDLY to the old pull-at-compose-up behaviour.
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

# ── 1b. give /var/lib/docker its own LV, out of the space the installer left ──
#
# WHY THIS EXISTS. Subiquity's guided LVM ("layout: name: lvm", no
# sizing-policy) gives the root LV about HALF the volume group and leaves the
# rest unallocated — 62 GB of a 125 GB disk on this box, measured 2026-08-09.
# Nothing had ever claimed it, so half the SSD sat idle while everything shared
# one filesystem.
#
# WHAT IT BUYS, and it is not the space. Docker is the one path here that grows
# without bound: images, layers, volumes, and the tier-2 profiles (immich,
# immich-ml, jellyfin) are exactly the things that grow. On one filesystem, a
# runaway pull fills ROOT — and a full root on the box that serves this LAN's
# DNS does not degrade, it fails: journald stops, sshd cannot write, and
# recovery needs the console. Measured on the panel the same day: its media sync
# took / to 100% and the box stayed up only because nothing else needed to write.
# On its own LV, docker fills ITS filesystem, compose reports a disk error, and
# the rest of the box keeps running.
#
# PROPORTIONAL, NEVER ABSOLUTE. `-l 60%FREE` is 60% of what is unallocated, so
# the same line is correct on the 128 GB production NVMe and on a small lab
# VHDX. A literal `-L 60G` would simply fail on the smaller disk, and would do
# it here, at firstboot, on a box with no console.
#
# THE REMAINING 40% IS LEFT UNALLOCATED ON PURPOSE. ext4 grows online in seconds
# (`lvextend -r`); it shrinks only unmounted, which for / means rescue media. So
# space committed to the wrong LV is expensive and space left in the VG is free
# to direct later. This split is a guess, and the reserve is what makes a wrong
# guess cheap.
#
# IDEMPOTENT, and the question it asks is "IS /var/lib/docker ALREADY ON ITS OWN
# LV" — the END STATE — not "does the LV exist".
#
# IT USED TO ASK THE SECOND, AND THAT WAS THE BUG (found 2026-08-09, run
# 20260809-145118). The LV is this step's OWN OUTPUT, and the unit header is
# explicit that a step must never ask "has this already happened?" of its own
# output. It cost exactly what that warning predicts: the run was SIGTERMed
# between `mkfs` and `mount` (see homehub-firstboot.service — a self-inflicted
# stop, now fixed), leaving a formatted LV that nothing mounted. On the next
# boot `lvs` found it, said "already exists — nothing to do", and returned. So
# the box could never finish the job, and "re-run the script" — the DOCUMENTED
# repair for every other step here — was the one thing that could not work.
#
# A half-done storage change that cannot self-repair is worse than one that
# never started, so the guard now checks the mount and resumes from wherever the
# previous attempt stopped.
setup_docker_lv() {
    command -v lvs >/dev/null 2>&1 || { log "1b: no LVM tooling — skipping the docker LV"; return 0; }

    # ASKED OF THE ROOT DEVICE, not of "the first VG on the box". A box with a
    # second volume group — a data disk, a leftover — must not have its docker
    # LV carved out of the wrong one. If / is not an LV at all, lvs fails, $vg is
    # empty, and this returns without touching anything.
    local rootdev vg
    rootdev="$(findmnt -no SOURCE / 2>/dev/null)"
    vg="$(lvs --noheadings -o vg_name "$rootdev" 2>/dev/null | awk '{$1=$1;print}')"
    [ -n "$vg" ] || { log "1b: / is not on LVM — skipping the docker LV (nothing to carve)"; return 0; }

    # THE END-STATE QUESTION. Already mounted from our LV = the work is done,
    # on this boot and every later one.
    if [ "$(findmnt -no SOURCE /var/lib/docker 2>/dev/null)" = "/dev/mapper/${vg//-/--}-docker--data" ]; then
        log "1b: /var/lib/docker is already on $vg/docker-data — nothing to do"
        return 0
    fi

    if lvs "$vg/docker-data" >/dev/null 2>&1; then
        # The LV is there but the mount is not, so a previous attempt died
        # part-way. Resume rather than return: skip the carve, keep every step
        # after it. mkfs is re-run only if the volume has no filesystem, which
        # is the one case where the previous attempt stopped even earlier.
        log "1b: $vg/docker-data exists but /var/lib/docker is not mounted from it — finishing the move"
        if ! blkid -s TYPE -o value "/dev/$vg/docker-data" >/dev/null 2>&1; then
            log "1b: the volume has no filesystem — making one"
            mkfs.ext4 -q -L docker-data "/dev/$vg/docker-data" || { log "1b: WARNING mkfs failed — leaving docker on root"; return 0; }
        fi
    else
        # Free extents, not free bytes: 0 means the installer already used the
        # whole VG (sizing-policy: all, or a hand-built layout). That is a
        # legitimate configuration and must not be an error.
        local freeext
        freeext="$(vgs --noheadings -o vg_free_count "$vg" 2>/dev/null | tr -d ' ')"
        if [ -z "$freeext" ] || [ "$freeext" -lt 256 ]; then
            log "1b: $vg has no meaningful free space (${freeext:-0} extents) — leaving /var/lib/docker on root"
            return 0
        fi

        log "1b: carving /var/lib/docker onto its own LV from $vg (${freeext} free extents)"
        lvcreate -y -l 60%FREE -n docker-data "$vg" || { log "1b: WARNING lvcreate failed — leaving docker on root"; return 0; }
        mkfs.ext4 -q -L docker-data "/dev/$vg/docker-data" || { log "1b: WARNING mkfs failed — leaving docker on root"; return 0; }
    fi

    # STOP DOCKER BEFORE MOVING ITS STATE. On a fresh install this directory is
    # nearly empty, but "nearly" is not "empty" — the daemon creates its storage
    # driver tree at first boot, and mounting over live state would hide it from
    # a running daemon rather than move it.
    #
    # THIS LINE IS ONLY SURVIVABLE BECAUSE homehub-firstboot.service SAYS
    # `Wants=docker.service`, NOT `Requires=`. Under Requires=, systemd
    # propagates the stop back to this very unit and SIGTERMs the script here,
    # mid-move — which is exactly what happened on 2026-08-09. If you ever
    # restore Requires=, this step will kill itself again; check_docker_lv_stop
    # in scripts/validate_config.py fails the build if you do.
    systemctl stop docker.socket docker 2>/dev/null || true

    local tmp; tmp="$(mktemp -d)"
    mount "/dev/$vg/docker-data" "$tmp"
    if [ -d /var/lib/docker ] && [ -n "$(ls -A /var/lib/docker 2>/dev/null)" ]; then
        cp -a /var/lib/docker/. "$tmp"/ || { log "1b: WARNING could not copy existing docker state — leaving it on root"; umount "$tmp"; rmdir "$tmp"; systemctl start docker 2>/dev/null || true; return 0; }
        rm -rf /var/lib/docker/* /var/lib/docker/.[!.]* 2>/dev/null || true
    fi
    umount "$tmp"; rmdir "$tmp"
    mkdir -p /var/lib/docker

    # BY UUID, not by /dev/<vg>/<lv>. A device-mapper path depends on the VG name
    # surviving; a UUID does not, and fstab is read before anything can fix it.
    #
    # nofail, because the failure modes are not symmetric. If this mount is ever
    # unsatisfiable, `nofail` means the box boots with docker back on root — the
    # behaviour it had before this step existed. Without it, systemd drops to
    # emergency.target on a headless machine, which is the 2026-08-06 signature
    # this project has already paid for once.
    local uuid; uuid="$(blkid -s UUID -o value "/dev/$vg/docker-data")"
    if ! grep -q "$uuid" /etc/fstab 2>/dev/null; then
        printf 'UUID=%s /var/lib/docker ext4 defaults,nofail 0 2\n' "$uuid" >> /etc/fstab
    fi
    mount /var/lib/docker
    systemctl start docker 2>/dev/null || true
    log "1b: /var/lib/docker is now $(findmnt -no SIZE /var/lib/docker 2>/dev/null), separate from root"
}
setup_docker_lv

# ── 1c. THE BOX'S TIMEZONE, WHICH NOTHING USED TO SET ────────────────────────
#
# Found 2026-08-30 (open-items C39). The installer left the OS on UTC because
# nothing here ever set it, and `TIMEZONE` in .env was an untouched template
# default of `Etc/UTC`. That is not cosmetic: **systemd's OnCalendar is read in
# the BOX's local time**, so both backup timers were firing five hours off the
# clock times they were written to mean — the nightly's `03:30` landed at 22:30
# for a UTC-5 household, and nobody noticed for weeks because a log line in UTC
# looks correct to a reader who assumes UTC.
#
# ONE SOURCE, BOTH LAYERS. `TIMEZONE` already reaches eight containers as
# `TZ: ${TIMEZONE}` (finance-auditor reads RUN_AT as box-local off it). This
# step points the OS at the same value, so the timers and the containers cannot
# disagree, and a reimage cannot silently revert to UTC.
#
# FAIL-OPEN and idempotent, like every other optional step in this file: no
# .env, an unset value, or a name zoneinfo does not know leaves the box exactly
# as it was. An unknown name is worth a loud line rather than a silent UTC.
__tz="$(sed -n 's/^TIMEZONE=//p' "$STACK_DIR/.env" 2>/dev/null | tail -n1 \
        | tr -d "\"'" | tr -d '[:space:]')"
if [ -n "${__tz:-}" ]; then
    if [ "$(timedatectl show -p Timezone --value 2>/dev/null)" = "$__tz" ]; then
        log "1c: timezone already $__tz"
    elif [ -e "/usr/share/zoneinfo/$__tz" ]; then
        timedatectl set-timezone "$__tz" 2>/dev/null \
            && log "1c: timezone set to $__tz (was $(date -u +%Z); OnCalendar is read in THIS zone)" \
            || log "WARN: 1c: timedatectl refused '$__tz' — timers stay on $(timedatectl show -p Timezone --value 2>/dev/null)"
    else
        log "WARN: 1c: TIMEZONE='$__tz' is not a zoneinfo name — leaving the box on $(timedatectl show -p Timezone --value 2>/dev/null); backup timers will fire in THAT zone"
    fi
else
    log "1c: no TIMEZONE in .env — leaving the box on $(timedatectl show -p Timezone --value 2>/dev/null)"
fi

# ── 1d. WHEN UNATTENDED UPGRADES MAY RESTART THINGS (C59 / C61) ─────────────
# Ubuntu ships apt-daily-upgrade.timer as `*-*-* 6:00` with
# RandomizedDelaySec=60m. It runs unattended-upgrades, and `needrestart` then
# restarts every daemon linked against an upgraded library. That is NOT
# hypothetical here, twice over:
#
#   C59 (measured 2026-09-01): libbz2-1.0 was the ONLY package upgraded, xrdp
#   was never upgraded at all, and needrestart restarted it anyway because it
#   links against the library. That destroyed sesman's session table, orphaned
#   a live desktop, and every RDP login afterwards died in one second blaming
#   the window manager. Permanent until a human killed the orphan.
#
#   C61 (still open): dockerd is not on needrestart's exclusion list either. A
#   restart bounces every container AND kills any `docker compose run` in
#   flight - which is exactly how homehub-library-backup.service runs the
#   FileBackup container. A killed container leaves a stale Temp that then
#   refuses every LATER backup too. One unattended patch, two outages, the
#   second one silent for days.
#
# THE FIX IS SCHEDULING, NOT DISABLING. Patching is worth more than the
# restarts cost, so upgrades still happen - once a week, at a known time, in
# the quiet half hour before the 03:00 backup rather than at a random moment
# inside the working day. A restart that lands 30 minutes BEFORE the backup is
# harmless; the same restart at 06:54 is what cost 2026-09-01.
#
# RandomizedDelaySec MUST be zeroed. The shipped 60m would smear a nominal
# 02:30 across 02:30-03:30 - i.e. straight into the backup this window exists
# to precede. The empty OnCalendar= clears the shipped 6:00 first; without it
# BOTH schedules stay active and the daily one still fires.
#
# apt-daily.timer is deliberately untouched: it only refreshes package lists,
# installs nothing, and therefore restarts nothing.
#
# Persistent=true is kept on purpose: a run missed because the box was off
# happens at the next boot. Late patching beats skipped patching, and a
# boot-time restart is harmless because no session or backup exists yet.
#
# This does NOT close C61. It narrows the window; it does not stop a restart
# landing on a backup that is STILL RUNNING from a previous week, and it does
# not help if apt overruns 03:00. Excluding dockerd from needrestart is the
# belt to this braces, and is the Owner's call.
mkdir -p /etc/systemd/system/apt-daily-upgrade.timer.d
cat > /etc/systemd/system/apt-daily-upgrade.timer.d/override.conf <<'EOF'
# Installed by firstboot 1d. See HomeHub open-items.md C59 / C61.
# Weekly, exact, 30 minutes before the 03:00 backup - NOT daily at a random time.
[Timer]
OnCalendar=
OnCalendar=Sun *-*-* 02:30:00
RandomizedDelaySec=0
Persistent=true
EOF
systemctl daemon-reload
systemctl restart apt-daily-upgrade.timer 2>/dev/null || true
log "1d: unattended upgrades -> Sun 02:30 exactly (was daily 06:00 +/-60m); needrestart can only bounce services in that window"

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
# (Q10.2). The tars ride inside deploy-payload/, so they land wherever the payload
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

# ── WHAT A TAR WOULD INSTALL, ASKED OF THE TAR ───────────────────────────────
# Prints the repo:tag(s) a `docker load` of this archive would (re)point, one per
# line, or nothing when they cannot be determined — and "nothing" must always
# mean LOAD IT, because refusing to load on a parse failure would silently strand
# a box with no images at all.
#
# Two sources, cheapest first. images.manifest.tsv is written beside the tars by
# vmtest/export-images.sh (ref / id / digest / file / bytes) and is exact, so a
# normal payload never has to open an archive. A tar with no row — a payload
# built before the manifest existed, or one staged by hand — is asked directly:
# `docker save` puts a manifest.json at the archive root whose RepoTags carry the
# same answer. No jq: it is not in packages.list and this must work on a box with
# nothing but the base system.
tar_repo_tags() {
    local tar="$1" base tags="" json=""
    base="$(basename "$tar")"
    if [ -f "$IMAGES_DIR/images.manifest.tsv" ]; then
        tags="$(awk -F'\t' -v f="$base" '$4 == f { print $1 }' "$IMAGES_DIR/images.manifest.tsv" 2>/dev/null || true)"
    fi
    if [ -z "$tags" ]; then
        case "$tar" in
            *.tar.zst)
                command -v zstd >/dev/null 2>&1 && \
                    json="$(zstd -dc "$tar" 2>/dev/null | tar -xO manifest.json 2>/dev/null || true)" ;;
            *)  json="$(tar -xOf "$tar" manifest.json 2>/dev/null || true)" ;;
        esac
        # One archive can carry several images, each with several tags, so pull
        # every "RepoTags":[…] block and then every string inside it. A null
        # RepoTags (an untagged save) matches nothing and correctly yields "".
        tags="$(printf '%s' "$json" \
                | grep -o '"RepoTags":\[[^]]*\]' 2>/dev/null \
                | sed 's/"RepoTags":\[//; s/\]//' | tr ',' '\n' | tr -d '"' || true)"
    fi
    printf '%s\n' "$tags" | sed '/^$/d'
}

if [ -n "$IMAGES_DIR" ]; then
    tars=("$IMAGES_DIR"/*.tar "$IMAGES_DIR"/*.tar.zst)
    log "loading ${#tars[@]} baked image tar(s) from $IMAGES_DIR (Q10.9 B+ — zero-registry first boot)"
    loaded=0
    skipped=0
    for tar in "${tars[@]}"; do
        name="$(basename "$tar")"

        # ── SKIP A TAR WHOSE repo:tag IS ALREADY HERE (found 2026-08-24) ──────
        # `docker load` IS NOT A NO-OP FOR AN EXISTING TAG. It is a no-op for an
        # existing IMAGE — identical content re-loads to the same id and nothing
        # changes — and the two were conflated in this step's comment since it was
        # written. What load actually does is (re)bind the tag in the archive to
        # the archive's image, whatever the tag pointed at a moment earlier.
        #
        # MEASURED ON THE LAB HUB, drill 2026-08-24, filebackup:local:
        #     baked                    sha256:932791f9…
        #     after a remote update    sha256:d26a9df6…   (the operator's build)
        #     after `docker load` of the baked tar
        #                              sha256:932791f9…   ← silently back
        # and the operator's image was left dangling with no tag on it at all.
        #
        # WHY THAT IS A REAL BUG AND NOT A LAB CURIOSITY. This script re-runs on
        # EVERY boot (see the header: RemainAfterExit only stops a second start
        # within one boot). The documented way to ship a fix to a running hub is
        # build → scp → `docker load` → `compose up -d`, and for the four images
        # with no registry home — naglight, finance-auditor, filebackup,
        # caddy-cloudflare — that is the ONLY way. So every such update was living
        # on borrowed time: it worked, it was verified, and the next reboot threw
        # it away and put the ISO's vintage back, with nothing in any log saying a
        # rollback had happened. A hub reverting a security fix on a power cut is
        # the failure this closes.
        #
        # THE QUESTION IS ASKED OF THE INPUTS, per the header's rule. The tar says
        # which repo:tag it would bind; docker says what that tag holds now. It is
        # the step 1b shape ("is the END STATE already true?"), not the 3e shape
        # ("did I already run?") — nothing here consults a marker this step wrote.
        # A fresh reimage has an empty docker state, so nothing matches, and every
        # tar loads exactly as before: the zero-registry first boot is untouched.
        #
        # THE COST, stated because it is real: a payload whose TAR was replaced
        # in place under a tag the box already has will now be skipped. That is
        # the same lever inverted, and it stays available — a deliberate rollback
        # or re-flash of one image is `docker load -i <tar>` by hand, which still
        # re-points the tag exactly as it always did. Automatic and irreversible
        # was the wrong default; manual and explicit is the right one.
        _tags=()
        mapfile -t _tags < <(tar_repo_tags "$tar")
        if [ "${#_tags[@]}" -gt 0 ]; then
            _missing=""
            for _t in "${_tags[@]}"; do
                docker image inspect "$_t" >/dev/null 2>&1 || _missing="$_missing $_t"
            done
            if [ -z "$_missing" ]; then
                # One line per skipped tag, naming the id that is being KEPT, so
                # a rollback that does not happen is as visible in the journal as
                # one that does.
                for _t in "${_tags[@]}"; do
                    log "  SKIP $name: $_t is already present as $(docker image inspect "$_t" --format '{{.Id}}' 2>/dev/null || echo '?') — keeping it"
                done
                log "    (loading would re-point that tag at the baked image and discard any"
                log "     update made since the flash. Deliberate rollback: docker load -i $tar)"
                skipped=$((skipped + 1))
                continue
            fi
        fi

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
    log "image payload: $loaded of ${#tars[@]} tar(s) loaded from $IMAGES_DIR ($skipped already present, left alone)"
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
# ── 3a½. name the backup principal (Q-FB5, 2026-08-23) ───────────────────────
# The backup drive now mounts uid=65532 so the FileBackup container can write
# it without running as root. The fstab option is numeric, so the mount works
# either way — this only gives the uid a name (`filebackup`) for `ls -l` and
# the library-backup wrapper. FATAL inside the script (uid already taken =
# someone else owns the archives) is a real finding; it must not stop bring-up
# here, so it is a WARN at this boundary like the mounts below.
log "naming the backup principal (filebackup, uid 65532)…"
bash "$STACK_DIR/provision/provision-backup-principal.sh" || \
    log "WARN: backup principal provisioning reported a problem — see above"

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

# Stage the coherent private gateway outside Caddy's public document root.
# Installing files does not enable PANEL_ACCESS_ENABLED or its compose overlay.
_gateway_candidates=()
shopt -s nullglob
for cand in /opt/homehub/wall-gateway "$STACK_DIR/wall-gateway" /cdrom/deploy-payload/wall-gateway; do
    for gateway in "$cand"/officewall-gateway-*.tar.gz; do
        _gateway_candidates+=("$gateway")
    done
done
shopt -u nullglob
if [ "${#_gateway_candidates[@]}" -eq 0 ]; then
    rm -rf "$STACK_DIR/panel-access/app"
    log "NOTICE: no private gateway payload found; any stale private gateway was removed"
elif ! bash "$STACK_DIR/panel-access/stage-gateway.sh" \
    "$(command -v python3)" "$STACK_DIR/panel-access/install-gateway.py" \
    "$STACK_DIR/wall-shell/build-info.json" "$STACK_DIR/panel-access/app" \
    "${_gateway_candidates[@]}"; then
    rm -rf "$STACK_DIR/panel-access/app"
    log "WARN: private gateway staging failed unexpectedly and was disabled; core startup continues"
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

# ── 4a-pre-2b. AND THE BACKUP'S OWN PATH SOURCES, WHICH NO BIND MOUNT NAMES ──
# The step above is sourced from `docker compose config`, so it only ever creates
# directories some CONTAINER binds. That is the right rule for its own purpose
# and it leaves a hole: the library tree this box is responsible for is defined
# by BACKUP_SOURCES, not by the compose file, and the two do not agree.
#
# MEASURED 2026-08-09, and it is the whole reason this block exists. Eight of the
# nine path sources existed — Media/Movies (jellyfin binds it), Media/immich
# (immich does), every Private/* tree, Shared, Snapshots. `Media/Music` did NOT,
# because NOTHING CONTAINERISED SERVES MUSIC: the hub exposes it through the
# Samba `Media` share and the wall panel pulls it over CIFS, so no bind mount
# ever names it. One directory, absent on every hub with a fresh library drive,
# and it took the ENTIRE nightly backup down with it (backup.sh has since been
# changed to skip the set rather than abort — but a set that is skipped is a set
# that is not protected, so creating the directory is still the actual fix).
#
# GATED ON THE LIBRARY ACTUALLY BEING MOUNTED. /srv/library is a `nofail` mount:
# if the drive is absent the path still exists as an empty directory on the root
# filesystem, and a `mkdir -p` there would manufacture a plausible-looking
# library on the system disk — masking a missing drive AND giving the backup
# somewhere harmless-looking to write. Refusing to create is the honest answer;
# the unified file-share monitor already reports the missing library mount.
if mountpoint -q /srv/library 2>/dev/null; then
    _made=0
    while read -r _p; do
        [ -n "$_p" ] || continue
        case "$_p" in
            /srv/library/*|/mnt/backup-drive/*)
                if [ ! -d "$_p" ]; then
                    mkdir -p "$_p" && _made=$((_made + 1))
                    log "  created backup path source $_p"
                fi ;;
            *) log "  NOT creating $_p — outside the data drives, so an absent one is a real fault" ;;
        esac
    done <<EOF
$(
    # READ IT THE WAY backup.sh DOES — by sourcing. BACKUP_SOURCES is a
    # multi-line QUOTED value, so every line-oriented tool (grep, sed -n 's///p')
    # sees only its first entry and silently agrees that everything else is fine.
    # That is not hypothetical: the first cut of this block used `grep -oP` and
    # "checked" exactly one of the nine sources. Sourcing hands the parsing to
    # the shell, which is the thing that defined the format.
    # Subshell + a `set -a`-free scope so nothing here leaks into firstboot's
    # own environment.
    if [ -r /etc/homehub-backup/backup.env ]; then
        . /etc/homehub-backup/backup.env 2>/dev/null || true
        printf '%s\n' "${BACKUP_SOURCES:-}" | while IFS= read -r _line; do
            case "$_line" in
                *=path:*) printf '%s\n' "${_line#*=path:}" ;;
            esac
        done
    fi
)
EOF
    log "  backup path sources checked; $_made created"
else
    log "  SKIPPED the backup path sources: /srv/library is NOT a mountpoint."
    log "    Creating them now would build a fake library on the system disk and hide"
    log "    the missing drive. Fix the mount, then re-run this script."
fi

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
# HOW LONG TO WAIT, MEASURED RATHER THAN GUESSED. The first cut of this waited
# 180s and still failed, on a box that was perfectly healthy. From that run's
# own journal:
#
#     21:43:38  boot
#     21:47:12  firstboot starts waiting for the address
#     21:50:13  gives up after 180s
#     21:52:20  eth0: Gained carrier / DHCPv4 address … acquired   (+308s)
#
# DHCP answered in the SAME SECOND as carrier — the wait was never about DHCP
# being slow, it was about the link being physically absent until +308s. In the
# lab that is the offline-install mechanism: the adapter stays disconnected for
# the whole install and is reconnected only once the runner's "VHDX quiet for
# 4 minutes" heuristic fires, which can land several minutes after firstboot has
# already started. 600s covers the observed 308s with room, and is still a sane
# bound on real hardware: a box with no address ten minutes after boot has a
# problem a longer timeout would only hide.
__addr_wait_max=600
if [ -n "$__lan_ip" ] && [ "$__lan_ip" != "0.0.0.0" ]; then
    __waited=0
    while ! ip -4 -o addr show 2>/dev/null | grep -qwF "$__lan_ip"; do
        if [ "$__waited" -ge "$__addr_wait_max" ]; then
            log "FATAL: $__lan_ip is not held by any interface after ${__waited}s."
            log "  Every published port in docker-compose.yml binds to that address, and"
            log "  'docker compose up -d' is all-or-nothing — it would fail with"
            log "  'cannot assign requested address' naming an arbitrary container."
            log "  The box has no LAN address, so this is a DHCP/link problem, not a"
            log "  stack problem. Check:  ip -4 addr ; networkctl status ; the cable."
            exit 1
        fi
        [ "$__waited" -eq 0 ] && log "waiting for $__lan_ip to appear on an interface (compose publishes to it)…"
        # A ten-minute silent wait is indistinguishable from a hang to anyone
        # watching the console, which is the only vantage a box in this state has.
        [ "$__waited" -gt 0 ] && [ $((__waited % 60)) -eq 0 ] \
            && log "  still waiting for $__lan_ip … ${__waited}s of ${__addr_wait_max}s"
        sleep 5
        __waited=$((__waited + 5))
    done
    [ "$__waited" -gt 0 ] && log "  $__lan_ip appeared after ${__waited}s"
fi

# ── 4a-pre-5. GIVE CADDY ITS CERTIFICATES BACK BEFORE IT ASKS FOR NEW ONES ───
# A reimage wipes the named volumes, so caddy starts against an empty caddy_data
# and immediately asks Let's Encrypt for every hostname. LE's duplicate-
# certificate limit is FIVE per exact set of identifiers per 168h, so two or
# three reinstalls in a week exhaust it. MEASURED ON THIS BOX 2026-08-28: one
# reimage spent the week's allowance for all five vhosts, logged 122 rate-limit
# refusals, and the apex certificate did not land until 2h40m after boot.
#
# Restoring the volume FIRST means caddy comes up already holding valid material
# and never calls ACME — the same "before compose up" reasoning as the wall site
# in step 3e, for the same reason: doing it afterwards works, but only after a
# window of visibly broken service.
#
# IT FAILS OPEN, ALWAYS, AND THAT IS THE WHOLE CONTRACT. No drive, no run, no
# archived set, a failed verify, no docker, a volume that already has content —
# every one of them leaves things exactly as they were and lets caddy issue
# normally. This step can only ever SAVE issuances; it must never be able to
# withhold a boot, so it runs in a subshell, reports through a flag file, and
# nothing in it is fatal.
# ONE LINE PER SET, written from inside the fail-open subshell. A subshell cannot
# hand a variable back to its parent, and this step now restores four volumes
# rather than one - so the parent needs a per-set RECORD, not the single boolean
# flag file (/run/homehub-acme-restored) this used to carry. That flag is gone
# with the single-set shape; nothing else ever read it.
__restore_log=/run/homehub-volume-restore.log
# `|| true` ON BOTH rm CALLS. These sit OUTSIDE the fail-open subshell and run
# under `set -e`, so if that path ever exists as a DIRECTORY - a stale mount, a
# name collision - `rm -f` returns nonzero and aborts firstboot before
# `docker compose up -d`. A step whose entire contract is "can only save
# issuances, never withhold a boot" must not have a cleanup that can withhold a
# boot. Adversarial review, 2026-08-28.
rm -f "$__restore_log" || true
# AND IF IT IS STILL THERE, IT IS NOT A FILE. A stale mount or a name collision
# leaves a DIRECTORY at that path; `rm -f` cannot remove one and returns nonzero,
# which the `|| true` above already stops from aborting the boot. What it does
# not stop is every later read and write of $__restore_log doing something other
# than what this step expects. Fall back to a private name rather than reason
# about which of those are harmless. (Adversarial review, 2026-08-29.)
if [ -e "$__restore_log" ] && [ ! -f "$__restore_log" ]; then
    log "  WARNING: $__restore_log exists and is not a regular file - using a temporary one instead"
    __restore_log="$(mktemp /run/homehub-volume-restore.XXXXXX 2>/dev/null)" || __restore_log=""
fi
if [ -z "$__restore_log" ]; then
    log "  no writable result log - SKIPPING the volume restore entirely rather than running it blind"
else
(
    . /etc/homehub-backup/backup.env 2>/dev/null || exit 0
    . "$STACK_DIR/backup/common.sh"  2>/dev/null || exit 0
    [ -n "${BACKUP_TARGET:-}" ] || exit 0
    # THE FSTAB ENTRY CARRIES `nofail` BY DESIGN - a missing USB disk must not
    # hold up local-fs.target - so systemd does not wait for this drive and
    # neither does this unit (After=network-online.target docker.service). A
    # drive that is PRESENT BUT SLOW TO ENUMERATE would therefore read as "no
    # backup drive" and cost the exact issuances this step exists to save.
    # Bounded wait, and only when fstab says this box is meant to have one, so a
    # box with no backup drive is not delayed at all. Same shape as the LAN-IP
    # wait in 4a-pre-4.
    # awk ON FIELD 2, not grep on a pattern: BACKUP_TARGET went into a BRE
    # unescaped, so a path holding `.` could match a DIFFERENT mount (and impose
    # a pointless 60s boot delay), while one holding `[...]` would be read as a
    # character class and match nothing - skipping the wait on exactly the box
    # that needed it. A mount point is a field, so compare it as one.
    # Adversarial review, 2026-08-28.
    # THE TARGET IS NOT ALWAYS A MOUNTPOINT ANY MORE (2026-09-01): it moved to
    # /srv/library/Configs, a FOLDER on the library drive. So the fstab question
    # is asked about the deepest ancestor fstab actually names - the target
    # itself when it is its own mountpoint, as /mnt/backup-drive was, and the
    # drive under it otherwise. Same field-not-regex comparison as before, just
    # applied to every ancestor (fstab_mount_for, backup/common.sh).
    __bmp="$(fstab_mount_for "$BACKUP_TARGET" /etc/fstab)" || __bmp=""
    if [ -n "$__bmp" ]; then
        __bw=0
        while [ "$__bw" -lt 60 ] && ! mountpoint -q "$__bmp"; do
            [ "$__bw" -eq 0 ] && log "  waiting up to 60s for $__bmp (fstab lists it, and nofail means nothing else waits)…"
            sleep 2; __bw=$((__bw + 2))
        done
        if mountpoint -q "$__bmp" && [ "$__bw" -gt 0 ]; then
            log "  $__bmp appeared after ${__bw}s"
        fi
    fi
    # ── C32: MOUNT IT OURSELVES, BECAUSE ON A FRESH INSTALL NOBODY ELSE HAS ──
    #
    # THIS STEP COULD NEVER FIRE ON THE INSTALL IT WAS WRITTEN FOR, and that was
    # measured on 2026-08-29 rather than reasoned about. The fstab entry for the
    # backup drive is written by provision-mounts, which runs LATER IN THIS SAME
    # SCRIPT:
    #
    #     00:03:24  /etc/homehub-samba/library-mounts.fstab installed (late-command)
    #     00:04:52  firstboot starts
    #     ~00:05    THIS STEP runs -> no fstab entry, so the wait above is
    #               skipped entirely and the mountpoint check fails -> exit 0
    #     00:12:46  [provision-mounts] fstab += /mnt/backup-drive
    #     00:12:46  [provision-mounts] mounted /mnt/backup-drive
    #
    # The absence of any "waiting up to 60s" line in that boot's journal is the
    # proof: the wait is gated on fstab naming the mountpoint, and at 00:05 it
    # does not. So a step whose entire purpose is the FRESH install only ever
    # worked on a box that had already been installed once, and C22 - five
    # certificates re-issued, 122 rate-limit refusals, the apex cert landing
    # 2h40m after boot - was never prevented, only survived.
    #
    # THE FIX IS SELF-CONTAINED, and deliberately not a reordering. Moving
    # provision-mounts earlier would drag the library tree creation and the
    # Samba provisioning with it, for one consumer. Instead: read the mount SPEC
    # from the same generated fragment provision-mounts will use - it has been on
    # disk since before this script started - and mount it ourselves, READ-ONLY,
    # for the duration of the restore.
    #
    # READ-ONLY IS NOT DECORATION. This step only ever READS the backup drive,
    # and it runs before anything else on the box has looked at that disk. A
    # bug here that wrote to it would damage the one copy the restore exists to
    # use, so the kernel is told the write cannot happen at all.
    #
    # Everything below stays FAIL-OPEN: any failure leaves things exactly as
    # they were and lets caddy issue normally.
    __src="$BACKUP_TARGET"
    __tmpmnt=""
    # Set BEFORE the first mount attempt so that every later `exit 0` - and
    # there are many - unmounts. A subshell gets its own EXIT trap.
    trap 'if [ -n "${__tmpmnt:-}" ]; then umount "$__tmpmnt" 2>/dev/null || log "  WARN: could not unmount $__tmpmnt"; rmdir "$__tmpmnt" 2>/dev/null || true; fi' EXIT
    if ! backup_target_ready "$BACKUP_TARGET"; then
        __frag=/etc/homehub-samba/library-mounts.fstab
        # WHICH LINE TO MOUNT is the ancestor question again: a folder target
        # needs the DRIVE UNDER IT mounted, not a line naming the folder - there
        # is none, and looking for one is how this step would silently become a
        # no-op on the box it was written for. __fmp is that mountpoint and
        # __frel the rest of the path, appended to the temporary mount below.
        __fmp="$(fstab_mount_for "$BACKUP_TARGET" "$__frag")" || __fmp=""
        __frel="${BACKUP_TARGET#"${__fmp:-$BACKUP_TARGET}"}"
        # Field 1 is the spec (LABEL=PriBackup), field 3 the fstype. `mount`
        # resolves LABEL=/UUID= itself, so nothing here has to know how.
        __spec="$(awk -v t="${__fmp:-}" '$0 !~ /^[[:space:]]*#/ && $2 == t { print $1; exit }' "$__frag" 2>/dev/null || true)"
        __fstype="$(awk -v t="${__fmp:-}" '$0 !~ /^[[:space:]]*#/ && $2 == t { print $3; exit }' "$__frag" 2>/dev/null || true)"
        # AND ONLY WHEN THE DRIVE IS NOT ALREADY MOUNTED. With a FOLDER target
        # the "not ready" branch is also reached on a box whose drive is mounted
        # and simply has no such folder yet - no backup has run since the
        # reimage - and mounting an in-use NTFS volume a second time either
        # fails or spends the whole 60s retry budget doing so, on every boot.
        if [ -n "$__spec" ] && ! mountpoint -q "${__fmp:-/nonexistent}"; then
            __tmpmnt="$(mktemp -d /run/homehub-acme-src.XXXXXX 2>/dev/null)" || __tmpmnt=""
            if [ -n "$__tmpmnt" ]; then
                # RETRY, because this is exactly where a slow USB enclosure
                # bites: on a fresh install nothing has waited for this disk,
                # and `nofail` means nothing ever will. Same 60s budget as the
                # fstab path above, for the same reason.
                __mw=0
                until mount -t "${__fstype:-auto}" -o ro "$__spec" "$__tmpmnt" 2>/dev/null; do
                    if [ "$__mw" -ge 60 ]; then break; fi
                    [ "$__mw" -eq 0 ] && log "  $BACKUP_TARGET is not mounted and fstab does not list it yet (C32) - waiting up to 60s for $__spec…"
                    sleep 2; __mw=$((__mw + 2))
                done
                if mountpoint -q "$__tmpmnt"; then
                    log "  mounted $__spec READ-ONLY at $__tmpmnt for the restore${__mw:+ (after ${__mw}s)}"
                    __src="$__tmpmnt$__frel"
                else
                    rmdir "$__tmpmnt" 2>/dev/null || true
                    __tmpmnt=""
                fi
            fi
        fi
    fi
    # NOT a bare `-d`: the directory exists whether or not the drive is on it, and
    # an unmounted empty dir would read as "no runs archived". backup_target_ready
    # asks both halves - the path exists AND a real data mount carries it rather
    # than the root filesystem - which is the guarantee `mountpoint -q` gave while
    # the target was itself a mountpoint.
    backup_target_ready "$__src" || exit 0

    # ── GENERALISED 2026-08-29: four volumes, not one ────────────────────────
    #
    # This step was written for caddy_data alone, and REIMAGE_PERSISTENCE_PLAN.md
    # said plainly why nothing else was hung off it: "Generalising a step that has
    # never once run would be building on an unproven foundation. Fix it, watch it
    # fire, then generalise it." C32 fixed it and the bench run proved it — 21
    # files, ACME account key and certificates included — so this is the "then".
    #
    # THE LOOP LIVES IN ITS OWN FILE, and that is the whole point. Inline, it
    # could only be tested by reimaging a box; as provision/restore-volumes.sh it
    # takes a mock `docker` on PATH and a temp directory, which is what
    # stack/provision/tests/restore-volumes.test.sh does. A restore path that has
    # never been executed is precisely how C22 happened.
    #
    # ORDERING IS ALREADY RIGHT AND IS WORTH SAYING OUT LOUD: this runs before
    # `docker compose up -d`, which is before provision-technitium and
    # provision-actual. So a restored actual_data is in place BEFORE the bootstrap
    # step looks at it — and that step is idempotent, so it finds an
    # already-bootstrapped server and no-ops (C21 stops recurring per reimage).
    # Moving this step later would silently invert that.
    STACK_DIR="$STACK_DIR" bash "$STACK_DIR/provision/restore-volumes.sh" "$__src" "$__restore_log" || true
    # The last command in the subshell must not decide its exit status by
    # accident; the parent reads the LOG, and the subshell is followed by || true.
    true
) || true
fi
if [ -n "${__restore_log:-}" ] && [ -s "$__restore_log" ]; then
    while IFS= read -r __rl; do
        case "$__rl" in
            ok\ caddy*) log "restored caddy_data from the backup drive — caddy starts holding its certificates and will not call ACME" ;;
            ok\ *)      log "restored ${__rl#ok }" ;;
            FAIL\ *)    log "WARN: volume restore FAILED — ${__rl#FAIL }" ;;
            skip\ *)    log "  no restore: ${__rl#skip }" ;;
        esac
    done <"$__restore_log"
else
    log "no volume restore at all (no drive, or the backup drive could not be read)"
fi
if ! grep -q '^ok caddy' "${__restore_log:-/nonexistent}" 2>/dev/null; then
    log "  caddy_data was NOT restored, so caddy will obtain certificates normally."
    log "  since C32 this step also MOUNTS the backup drive itself when fstab does not"
    log "  list it yet, so 'no drive' now means the disk was genuinely not there."
    log "  Let's Encrypt allows 5 per exact identifier set per 168h and a reimage"
    log "  spends one of each, so expect the last hostname to take a while if this"
    log "  box has been reinstalled recently."
fi
rm -f "${__restore_log:-}" 2>/dev/null || true

# ── 4-pre. THE CROSSPLAY RELAY'S TWO UNITS, INSTALLED AND ENABLED ───────────
# REWRITTEN 2026-08-29 (CROSSPLAY_HANDOFF.md §7). What stood here was ~40 lines
# that detected a profile, validated a secret, installed a unit, verified an
# iptables rule and degraded gracefully — security-critical branching in shell,
# under `set -euo pipefail`, in the last place that runs before the household's
# DNS, Caddy, Actual and tracker come up. Four adversarial review rounds each
# found a defect in the previous round's fix, and TWICE that defect would have
# flashed a box with no stack at all. The block whose entire purpose was "never
# take the household's DNS down over a game relay" took the household's DNS down
# over a game relay, twice, in two different ways.
#
# The fix was not a fifth patch. It was moving WHO OWNS THE RELAY'S LIFECYCLE:
#   * docker-compose.yml gives the relay `restart: "no"`, so Docker never starts
#     it, and keeps `profiles: ["gunmaster3"]`, so the bulk `up -d` below never
#     starts it either;
#   * homehub-gunmaster3-relay.service is the only thing that ever passes
#     `--profile gunmaster3`, and it `Requires=` the fence unit — so systemd,
#     not shell branching, both orders the fence first AND propagates its
#     failure;
#   * the enable knob is GAME_RELAY_ENABLED in .env, read by that unit's
#     ExecCondition, where a parse disagreement can only mean "did not start"
#     or "started behind a guaranteed fence" — never "started unfenced".
#
# WHAT IS LEFT HERE IS FILE COPYING, AND IT HAS NO BRANCHES. Every line ends in
# `|| log "WARN: …"`, so NOTHING in this section can take firstboot down before
# `docker compose up -d`. The measured fact that made all of this necessary —
# `internal: true` does NOT stop a container reaching the HOST — lives in
# game-isolation.sh's header, next to the port-by-port measurements.
#
# NOT A COPY ONTO ITSELF: the payload is $STACK_DIR/game-isolation/, the target
# is /etc/systemd/system/. That is the round-2 bug (`install <path> <same path>`
# exits 1, "are the same file") checked rather than assumed.
#
# IDEMPOTENT ON PURPOSE. autoinstall/user-data installs and enables these same
# two units in its late-commands, so the fence exists from the very FIRST boot,
# before dockerd has ever started — which is exactly what the fence unit's
# `Before=docker.service` is for. THE ISO IS AUTHORITATIVE for a flashed box;
# this section is the re-apply that covers a stack payload updated in place, a
# box deployed by copying /opt/homehub/stack without a reflash, and any unit
# file that changed since the ISO was built.
chmod 0755 "$STACK_DIR/game-isolation/game-isolation.sh" 2>/dev/null || log "WARN: could not chmod game-isolation.sh"
chmod 0755 "$STACK_DIR/game-isolation/gunmaster3-relay.sh" 2>/dev/null || log "WARN: could not chmod gunmaster3-relay.sh"
install -m 0644 "$STACK_DIR/game-isolation/homehub-game-isolation.service"   /etc/systemd/system/ 2>/dev/null || log "WARN: could not install homehub-game-isolation.service"
install -m 0644 "$STACK_DIR/game-isolation/homehub-gunmaster3-relay.service" /etc/systemd/system/ 2>/dev/null || log "WARN: could not install homehub-gunmaster3-relay.service"
systemctl daemon-reload 2>/dev/null || log "WARN: systemctl daemon-reload failed"
systemctl enable homehub-game-isolation.service   >/dev/null 2>&1 || log "WARN: could not enable homehub-game-isolation.service - the relay subnet would not be fenced off this host after a reboot"
systemctl enable homehub-gunmaster3-relay.service >/dev/null 2>&1 || log "WARN: could not enable homehub-gunmaster3-relay.service - the relay would not come back after a reboot"
log "game-isolation + relay units installed and enabled"

# ── 4-pre-b. DISARM A LEGACY RELAY CONTAINER, AND VERIFY THE DISARM ─────────
# THE ONE THING THE LIFECYCLE REFRESH DOES NOT FIX BY ITSELF (found by codex
# gpt-5.6-sol, round 6). A box that ran the OLD shape has a `gunmaster3-relay`
# container created with `restart: unless-stopped`, and NOTHING below touches
# it: the bulk `docker compose up -d` skips the relay because its profile is
# inactive, so compose neither recreates nor removes the container it does not
# consider part of this invocation. The container just sits there, armed.
#
# WHY THAT IS A SECURITY PROBLEM AND NOT UNTIDINESS. If the relay unit then
# skips cleanly (GAME_RELAY_ENABLED=false) or fails its precheck, systemd has
# correctly decided the relay must not run — and DOCKER restarts it anyway at
# the next boot, because `unless-stopped` is a property of the container, not of
# the compose file. The result is a public, unauthenticated relay started
# outside homehub-gunmaster3-relay.service: outside the `Requires=` that
# guarantees the fence, and outside the ExecStartPost that verifies it. Exactly
# the state the refresh exists to make unreachable, reached from the past.
#
# `docker update --restart=no` rewrites the policy on the EXISTING container -
# no recreate, no image, no compose - and is the only way to disarm one without
# removing it. The stop is separate because `update` changes what happens at the
# NEXT boot and nothing about now.
#
# VERIFY THE ARTIFACT, NOT THE EXIT CODE (house rule). Both commands can return
# 0 against a container that is then still armed or still running, and the whole
# point of this block is a claim about the next boot.
#
# STILL NON-FATAL, like everything else in this section. A failure here is loud,
# names the exact two commands to run by hand, and does not stop the household's
# DNS, Caddy, Actual and tracker from starting ten lines below.
__gm3_rp="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' gunmaster3-relay 2>/dev/null || true)"
if [ -n "$__gm3_rp" ] && [ "$__gm3_rp" != no ]; then
    log "MIGRATION: a legacy gunmaster3-relay container is armed with restart='$__gm3_rp'"
    log "  docker - not systemd - would start it at the next boot, outside the fence check"
    docker update --restart=no gunmaster3-relay >/dev/null 2>&1 || log "  WARN: 'docker update --restart=no gunmaster3-relay' failed"
    docker stop gunmaster3-relay >/dev/null 2>&1 || log "  WARN: 'docker stop gunmaster3-relay' failed"
    __gm3_rp2="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' gunmaster3-relay 2>/dev/null || true)"
    __gm3_run2="$(docker inspect -f '{{.State.Running}}' gunmaster3-relay 2>/dev/null || true)"
    if [ "$__gm3_rp2" = no ] && [ "$__gm3_run2" != true ]; then
        log "  disarmed and stopped - the relay unit owns this container's lifecycle now"
    else
        log "  WARN: COULD NOT DISARM the legacy relay container (restart='${__gm3_rp2:-?}', running='${__gm3_run2:-?}')."
        log "  Docker may start a PUBLIC, UNAUTHENTICATED relay at the next boot, outside"
        log "  homehub-gunmaster3-relay.service and therefore outside its fence check. Run:"
        log "    docker update --restart=no gunmaster3-relay && docker stop gunmaster3-relay"
        log "  then confirm:  docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' gunmaster3-relay"
    fi
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

# ── 4a-relay. START THE CROSSPLAY RELAY, WHICH THE LOOP ABOVE DID NOT JUDGE ──
# WHY THE LOOP ABOVE SAYS NOTHING ABOUT IT, stated precisely because the first
# version of this comment got it wrong. `docker compose ps --services` DOES list
# a profiled service once its container is running (and with `-a`, once the
# container merely exists) — measured on Compose v5.3. What is true here is
# narrower and is about TIMING, not profiles: at this point in firstboot nothing
# has started the relay yet, because the only thing that ever does is its unit,
# three lines below. So there is no container for `ps` to have found, the loop
# correctly reported on everything that existed, and the relay needs its own
# line. Its state is a systemd question now, not a docker one.
#
# --no-block IS LOAD-BEARING, AND LEAVING IT OUT DEADLOCKS THE BOOT. This script
# runs INSIDE homehub-firstboot.service, and the unit it is starting is ordered
# `After=homehub-firstboot.service` (so that on boot 1 the relay does not race
# the image load and the caddy it needs). A BLOCKING `systemctl start` therefore
# waits for a unit that cannot begin until this script's own service finishes —
# and it never will, because it is waiting here. This project has already paid
# for that exact deadlock once: 2026-08-27, setup-remote-ui.sh, firstboot stuck
# `activating` for 18 MINUTES with `multi-user.target start waiting`, no session,
# no IceDrive. Two fixes that were each correct alone wedged the whole boot.
# --no-block ENQUEUES the job and returns; systemd runs it the moment firstboot
# completes, so the ordering guarantee is kept and the deadlock cannot happen.
#
# WHICH MEANS THIS CANNOT REPORT THE RELAY'S FINAL STATE, and must not pretend
# to: the job has not run yet. Log the fence (known now) and name the command
# that answers the relay question afterwards.
#
# NON-FATAL, and this is the whole blast-radius change. The relay is the one
# unsafe component on this box; every other thing in this file has already come
# up by now. A relay that will not start leaves DNS, Caddy, Actual and the
# tracker exactly as they are, says so in the journal, and waits for someone.
# GAME_RELAY_ENABLED=false makes this a clean no-op: the unit's ExecCondition
# reports "condition not met", which systemd records as a skip, not a failure.
if systemctl start --no-block homehub-gunmaster3-relay.service >/dev/null 2>&1; then
    log "crossplay relay: start QUEUED (--no-block); systemd runs it the moment firstboot finishes"
    log "  it cannot run sooner - the unit is ordered After=homehub-firstboot.service"
    log "  check it afterwards with: systemctl is-active homehub-gunmaster3-relay.service"
    log "  (GAME_RELAY_ENABLED=false makes that 'inactive' with a condition-not-met skip, which is correct)"
else
    log "WARN: could not queue the relay unit start - systemctl status homehub-gunmaster3-relay"
    log "  the rest of the stack is unaffected; this box simply has no crossplay relay"
fi
# `systemctl is-active` PRINTS ITS ANSWER AND EXITS NON-ZERO for anything but
# active, so `|| echo unknown` appended a SECOND word on a second line to every
# inactive answer. `|| true` keeps the printed answer and swallows the status.
log "crossplay fence unit: $(systemctl is-active homehub-game-isolation.service 2>/dev/null || true)"

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
install -m 0755 "$STACK_DIR/samba/retire-legacy-backup-health.sh" /usr/local/sbin/homehub-retire-legacy-backup-health
# SR-018 upgrade migration: deleting a tracked unit does not remove its already
# installed copy. Stop and remove it before the shared guard changes meaning.
/usr/local/sbin/homehub-retire-legacy-backup-health || \
    log "WARN: could not fully retire the legacy backup-drive health units"
install -m 0644 "$STACK_DIR/samba/homehub-library-health.service" /etc/systemd/system/homehub-library-health.service
install -m 0644 "$STACK_DIR/samba/homehub-library-health.timer"   /etc/systemd/system/homehub-library-health.timer
# A23: the serials the guard asserts. Carried on the USB as site/, copied to
# /etc/homehub-samba/ beside the fstab fragment it pairs with. Absent = the
# guard still reports, it just cannot distinguish a stand-in from the real disk
# and names that gap in the check note.
if [ -f /etc/homehub-samba/drive-identity.conf ]; then
    log "drive identity assertions present — health checks will flag stand-in drives"
else
    log "no /etc/homehub-samba/drive-identity.conf — health checks report presence only (a stand-in drive will read as healthy)"
fi

# ── the tracker's own definitions, watched from OUTSIDE the tracker ──────────
# The third guard, added 2026-08-29 after /api/today was measured returning
# {"items":null} while the panel rendered GREEN with score 0. A tracker with no
# items is green because there is nothing to be late for, and on a wall that is
# indistinguishable from a tracker where everything is fine.
#
# It cannot be an item in the tracker, because the alarm would be deleted by the
# same `rm` as everything else. So it reads the definition FILES off the docker
# volume — which also means a stopped tracker is still assessable — and leaves a
# verdict in /var/lib/homehub/tracker-defs.state for verify-hub.sh.
install -d -m 0755 /var/lib/homehub
install -m 0755 "$STACK_DIR/tracker/tracker-defs-guard.sh" /usr/local/sbin/homehub-tracker-defs-guard
install -m 0644 "$STACK_DIR/tracker/homehub-tracker-defs-health.service" /etc/systemd/system/homehub-tracker-defs-health.service
install -m 0644 "$STACK_DIR/tracker/homehub-tracker-defs-health.timer"   /etc/systemd/system/homehub-tracker-defs-health.timer
install -m 0755 "$STACK_DIR/tracker/frame-freshness.sh" /usr/local/sbin/homehub-frame-freshness
install -m 0644 "$STACK_DIR/tracker/homehub-frame-freshness.service" /etc/systemd/system/homehub-frame-freshness.service
install -m 0644 "$STACK_DIR/tracker/homehub-frame-freshness.timer"   /etc/systemd/system/homehub-frame-freshness.timer

# -- fail2ban for the two password-guarded Caddy sites (the Owner, 2026-08-29) --
#
# actual.<domain> and dns.<domain> are the only surfaces on this box whose gate
# is a password rather than Google sign-in, and the router forwards :80/:443 to
# Caddy. Three layers now sit in front of them, and this is the third:
#
#   1. the Caddyfile remote_ip gate  - takes them off the internet entirely
#   2. the caddy-ratelimit plugin    - 120 requests/min/IP, blunts speed
#   3. this                          - bans persistence, at the FIREWALL
#
# FAIL-OPEN, like every other optional step in this file: no fail2ban binary, no
# config directory, or a failed reload leaves the box exactly as it would have
# been. The two layers above do not depend on it.
#
# THE @LAN_IP@ SUBSTITUTION is why this is not a plain `install`. The jail must
# never ban the box itself, and MiniPC-Deployer carries no site addresses - so
# the template ships a token and the address comes from .env at install time.
if command -v fail2ban-server >/dev/null 2>&1 && [ -d "$STACK_DIR/caddy/fail2ban" ]; then
    install -d -m 0755 /var/log/caddy
    install -m 0644 "$STACK_DIR/caddy/fail2ban/caddy-guarded.filter.conf" \
        /etc/fail2ban/filter.d/caddy-guarded.conf
    # Raises dbpurgeage from the shipped 1d. Without it bantime.increment resets
    # its count every day and the escalating ban is a flat one-hour ban wearing a
    # feature's name.
    install -m 0644 "$STACK_DIR/caddy/fail2ban/fail2ban.local" /etc/fail2ban/fail2ban.local
    _f2b_lan="$(sed -n 's/^LAN_IP=//p' "$STACK_DIR/.env" | tail -n1 | tr -d "\"'")"
    if [ -n "$_f2b_lan" ]; then
        sed "s|@LAN_IP@|$_f2b_lan|" "$STACK_DIR/caddy/fail2ban/caddy-guarded.jail.local" \
            > /etc/fail2ban/jail.d/caddy-guarded.local
        chmod 0644 /etc/fail2ban/jail.d/caddy-guarded.local
        systemctl enable --now fail2ban >/dev/null 2>&1 || true
        if systemctl reload-or-restart fail2ban >/dev/null 2>&1; then
            log "  fail2ban: caddy-guarded jail installed (bans in DOCKER-USER, not INPUT)"
        else
            log "  WARNING: fail2ban would not reload - the caddy-guarded jail is NOT active"
        fi
    else
        log "  WARNING: LAN_IP is empty in .env - the caddy-guarded jail was NOT installed,"
        log "    because a jail that cannot exempt this box could ban it out of its own stack."
    fi
else
    log "  fail2ban not installed - the caddy-guarded jail is skipped (the remote_ip gate"
    log "    and the rate limiter are unaffected; both live in the Caddyfile)."
fi

# ── the A19 GATE's feed configuration, and only ever a gate's ────────────────
# Both drive lanes take their feed settings from backup.env. A production hub
# gets that file from the household materialiser (late-command 4b); a SIM hub
# gets none, so on 2026-08-04's gate the file-share monitor logged "journal only"
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
        log "SIM GATE: installed a test backup.env so the unified file-share health item REPORTS"
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
# would post a spurious combined share-health red while the mount is still
# coming up.  There is deliberately no backup-drive health timer: physical
# backup-target checks remain internal FileBackup preflight safeguards.
systemctl enable --now homehub-library-health.timer >/dev/null 2>&1 || \
    log "WARN: could not enable homehub-library-health.timer — file-share failure would go unreported"
# The definition-inventory guard (2026-08-29). Its FIRST run on a fresh box will
# be YELLOW and should be: there is no baseline yet, and recording one
# automatically is exactly what would make a later deletion invisible. Taking the
# baseline is a deliberate act, once the definitions are what the household
# wants:
#     sudo homehub-tracker-defs-guard --baseline
systemctl enable --now homehub-tracker-defs-health.timer >/dev/null 2>&1 || \
    log "WARN: could not enable homehub-tracker-defs-health.timer — a tracker whose definitions were deleted would render green with score 0 and nothing would say so"

# The frame-freshness lane (VIDEO_FRAME_GEN_CHECK_PLAN.md). A failure to enable
# is a WARN rather than fatal, but it is not harmless: the item is `automated`,
# so a lane that stops being reported goes stale and NagLight escalates it to
# RED after three days. The feeder exists precisely to keep that from happening
# silently, and an un-enabled timer is the one way it can.
systemctl enable --now homehub-frame-freshness.timer >/dev/null 2>&1 || \
    log "WARN: could not enable homehub-frame-freshness.timer — the picture-frame videos could fall behind their source media with nothing saying so"

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

# ── 6b. ARE THE BACKUP TIMERS ACTUALLY ARMED? (defect 22, asked properly) ────
# The install already refuses if a unit file did not land or `enable` did not
# create its symlink (late-command 5d). That check runs in a curtin chroot,
# where there is no running systemd to ask, so it can only see the FILES.
#
# THIS is where the question has a real answer: a booted box, with the manager
# up, where `systemctl list-timers` can say whether the timer is loaded AND when
# it will next fire. Defect 22 was `is-enabled` answering NOT-FOUND on a hub
# everyone called healthy for months — the two halves of that (the file, and the
# armed timer) fail independently, so they are checked independently.
#
# A WARNING, NOT A FATAL. By this point the stack is up, DNS is provisioned and
# the shares are mounted; refusing the whole first boot over a timer would trade
# a working box for a broken one. The line is loud, it names the fix, and
# verify-hub.sh / LAB_TEST_PLAN TC-H-M13 assert it afterwards from outside.
for _t in homehub-backup.timer homehub-library-backup.timer; do
    # -F (fixed string): the unit names carry a `.` and neither is a substring
    # of the other, so a literal match is both sufficient and unambiguous.
    if systemctl list-timers --all --no-pager --no-legend 2>/dev/null | grep -qF "$_t"; then
        log "timer armed: $_t (next: $(systemctl show -p NextElapseUSecRealtime --value "$_t" 2>/dev/null))"
    else
        log "WARN: $_t is NOT in \`systemctl list-timers\` — it will never fire."
        log "  This is the defect-22 shape: the unit can be present and enabled and still"
        log "  not be armed (a bad OnCalendar, or a failed daemon-reload). Nothing else on"
        log "  this box will notice a backup that simply never runs."
        log "  Check:  systemctl status $_t ; systemctl list-timers --all | grep ${_t%.timer}"
    fi
done

# ── 6c. the two OPTIONAL FEATURES: remote desktop, and IceDrive ─────────────
# ACTIVATED BY .env, CARRIED BY THE BUILD, AND NEITHER DECIDED HERE. This repo
# ships both OFF (the Owner, 2026-08-27); HomeHub's config.homehub.psd1 `Extras`
# block turns them on, and the SAME declaration decides what the image carries.
#
# WHY THE TWO HALVES ARE TIED TOGETHER AT SOURCE. They used to be independent,
# and they silently disagreed for a month: every version of this project told
# operators to "RDP in and sign in to IceDrive" while ELEVEN of the AppImage's
# runtime libraries were in no image at all - so the instruction could not have
# been followed on any hub this repo ever built. Nothing noticed, because
# nothing had ever launched the thing. Materialize-Deploy now refuses to emit an
# activation whose carriage is absent; this step's job is only to obey.
#
# NON-FATAL BY DESIGN. A hub with no desktop and no offsite client is degraded,
# not broken: the stack, the shares, the local backups and SSH are unaffected.
# So this logs loudly and carries on rather than failing the unit, which is
# reserved for things that make the box wrong rather than incomplete.
_env_val() { sed -n "s/^$1=//p" "$STACK_DIR/.env" 2>/dev/null | head -1 | tr -d '
'; }
REMOTE_UI_ENABLED="$(_env_val REMOTE_UI_ENABLED)"
ICEDRIVE_MODE="$(_env_val ICEDRIVE_MODE)"
: "${ICEDRIVE_MODE:=off}"

# THE ONE COMBINATION THAT CANNOT WORK, refused here as well as in
# Materialize-Deploy: the AppImage is a GUI app and has no display without the
# session. Belt and braces on purpose - this file is also reached by a payload
# edited by hand, which no dev-PC gate can see.
if [ "$ICEDRIVE_MODE" = "appimage" ] && [ "$REMOTE_UI_ENABLED" != "true" ]; then
    log "WARNING: ICEDRIVE_MODE=appimage needs REMOTE_UI_ENABLED=true - the GUI client"
    log "  cannot run without a display. Treating IceDrive as OFF for this boot."
    ICEDRIVE_MODE=off
fi


# ── 6c-pre. GIVE ICEDRIVE ITS SIGN-IN BACK, BEFORE THE SESSION STARTS IT ─────
#
# The sync PAIRS come back from the API on their own — measured across a real
# reboot 2026-08-29, the client asked and got them with no local state involved.
# What does NOT come back is the ability to sign in: the token, the saved
# credential and the web-session cookies live in the `hub` account's home
# directory, and the account has 2FA, so a reflashed box cannot resume the
# offsite copy until a human sits down at it.
#
# ORDER IS LOAD-BEARING AND THIS IS WHY IT SITS ABOVE setup-remote-ui.sh: that
# script starts the graphical session, the session autostarts IceDrive, and
# IceDrive begins signing in within seconds. A profile restored afterwards is a
# profile the client has already replaced with a fresh, signed-out one.
#
# THE ARCHIVE IS ENCRYPTED and the two halves travel on different media: the
# ciphertext rides the BACKUP DRIVE inside the `icedrive` backup set, and the key
# (ICEDRIVE_PROFILE_KEY) rides the INSTALL STICK in .env. Either alone is
# useless. That split is the change an adversarial review required before this
# was allowed to exist at all — see the header of icedrive-profile.sh.
#
# FAIL-OPEN, like every other restore in this file. No drive, no archived set, no
# key, a failed decrypt, a profile that is already populated: each one leaves the
# box exactly as it would have been, and the operator signs in once by hand.
if [ "$ICEDRIVE_MODE" != "off" ] && [ -f "$STACK_DIR/icedrive/icedrive-profile.sh" ]; then
    install -d -m 0755 /var/lib/homehub
    install -m 0755 "$STACK_DIR/icedrive/icedrive-profile.sh" /usr/local/sbin/homehub-icedrive-profile
    if [ -f "$STACK_DIR/icedrive/homehub-icedrive-profile.service" ]; then
        install -m 0644 "$STACK_DIR/icedrive/homehub-icedrive-profile.service" /etc/systemd/system/
        install -m 0644 "$STACK_DIR/icedrive/homehub-icedrive-profile.timer"   /etc/systemd/system/
        systemctl daemon-reload
        systemctl enable --now homehub-icedrive-profile.timer >/dev/null 2>&1 || \
            log "  WARNING: could not enable homehub-icedrive-profile.timer - the sign-in would not be captured for the NEXT reimage"
    fi
    _ICE_USER="$(getent passwd 1000 | cut -d: -f1)"; [ -n "$_ICE_USER" ] || _ICE_USER=hub
    _ICE_DIR=/var/lib/homehub/icedrive
    (
        # Bring the encrypted archive back off the backup drive first. It is an
        # ordinary `path:` backup set, so restore.sh is the tool - the same one
        # the volume restores use, for the same reason: one implementation of
        # "reconstruct this set and verify every byte".
        if [ ! -f "$_ICE_DIR/icedrive-profile.tar.gz.gpg" ]; then
            . /etc/homehub-backup/backup.env 2>/dev/null || exit 0
            . "$STACK_DIR/backup/common.sh" 2>/dev/null || exit 0
            [ -n "${BACKUP_TARGET:-}" ] || exit 0
            backup_target_ready "$BACKUP_TARGET" || exit 0
            _run="$(newest_run_with_set "$BACKUP_TARGET" icedrive)" || exit 0
            [ -n "$_run" ] || exit 0
            install -d -m 0700 "$_ICE_DIR"
            bash "$STACK_DIR/backup/restore.sh" --run "$_run" --set icedrive --target "$_ICE_DIR" >/dev/null 2>&1 || exit 0
        fi
    ) || true
    if [ -f "$_ICE_DIR/icedrive-profile.tar.gz.gpg" ]; then
        # Exit 3 is "declined, nothing to do" (a populated profile, no archive) and
        # is NOT a warning; exit 1 is a real failure and is.
        #
        # `|| __ice_rc=$?` IS LOad-BEARING, and its absence made the case below
        # DEAD CODE — found 2026-08-30 on the real hub. This file runs under
        # `set -euo pipefail`: an unguarded pipeline that exits non-zero kills the
        # script on the spot, so exit 3 aborted firstboot before the branch that
        # exists precisely to call exit 3 benign could run. Every boot of a hub
        # with an already-populated IceDrive profile — i.e. every boot after the
        # first sign-in — reported homehub-firstboot.service as FAILED, with the
        # last log line being a decline that the code was written to tolerate.
        #
        # Capturing into a variable rather than appending `|| true` to the
        # pipeline is deliberate: `|| true` is a following simple command, which
        # resets PIPESTATUS, so the case would then read 0 for every outcome and
        # a genuine exit 1 would report as a successful restore.
        __ice_rc=0
        bash "$STACK_DIR/icedrive/icedrive-profile.sh" --restore --user "$_ICE_USER" --env "$STACK_DIR/.env" 2>&1 | sed 's/^/  /' || __ice_rc=$?
        case "$__ice_rc" in
            0) log "  restored the IceDrive profile - the client should sign in without the 2FA prompt" ;;
            3) log "  no IceDrive profile restore (the profile is already populated, or there is no archive)" ;;
            *) log "  WARNING: the IceDrive profile restore FAILED. The client will start signed out;"
               log "    sign in once over RDP, then: sudo homehub-icedrive-profile --capture" ;;
        esac
    else
        log "  no encrypted IceDrive profile on the backup drive yet - first sign-in is by hand."
        log "    After signing in: sudo homehub-icedrive-profile --capture"
    fi
fi
if [ "$REMOTE_UI_ENABLED" = "true" ]; then
    log "provisioning the graphical session (SR-015, activated by .env)…"
    HUB_USER="$(getent passwd 1000 | cut -d: -f1)"
    [ -n "$HUB_USER" ] || HUB_USER="hub"
    _rui_env=""
    if [ "$ICEDRIVE_MODE" = "appimage" ]; then
        ICEDRIVE_SRC="$STACK_DIR/remote-ui/Icedrive.AppImage"
        if [ -f "$ICEDRIVE_SRC" ] && [ -f "$ICEDRIVE_SRC.sha256" ]; then
            # RE-CHECK THE HASH ON THE BOX. An ISO can be re-burned and a payload
            # can be edited; "we verified it at build time" is not the same claim
            # as "these bytes are pinned".
            _want="$(tr -d '[:space:]' < "$ICEDRIVE_SRC.sha256")"
            _got="$(sha256sum "$ICEDRIVE_SRC" | cut -d' ' -f1)"
            if [ "$_want" = "$_got" ]; then
                _rui_env="ICEDRIVE_APPIMAGE=$ICEDRIVE_SRC"
                log "  IceDrive AppImage present and matches its pinned sha256"
            else
                log "  WARNING: the AppImage does NOT match its pin - session only."
                log "    pinned=$_want actual=$_got"
            fi
        else
            log "  WARNING: ICEDRIVE_MODE=appimage but no verified AppImage rode the"
            log "    payload. Installing the session without it. This is the carriage/"
            log "    activation split that Materialize-Deploy exists to prevent."
        fi
    fi
    if runuser -u "$HUB_USER" -- sudo -n env $_rui_env bash "$STACK_DIR/remote-ui/setup-remote-ui.sh" 2>&1 | sed 's/^/  /'; then
        log "  graphical session ready - RDP to this box on :3389 as $HUB_USER"
        [ -n "$_rui_env" ] && log "  IceDrive autostarts in that session; sign in once over RDP."
    else
        log "  WARNING: setup-remote-ui.sh failed - the box has NO graphical session."
        log "    Everything else is unaffected. Re-run by hand:"
        log "      sudo bash $STACK_DIR/remote-ui/setup-remote-ui.sh"
    fi
else
    log "REMOTE_UI_ENABLED is not true - no desktop, no xrdp, nothing on tcp/3389."
fi

if [ "$ICEDRIVE_MODE" = "cli" ]; then
    log "provisioning IceDrive (headless CLI, activated by .env)…"
    if [ -f "$STACK_DIR/icedrive/setup-icedrive.sh" ]; then
        if bash "$STACK_DIR/icedrive/setup-icedrive.sh" 2>&1 | sed 's/^/  /'; then
            log "  IceDrive CLI step complete (see the lines above for what it did)"
        else
            log "  WARNING: setup-icedrive.sh failed - this box has NO offsite client."
            log "    Everything else is unaffected. Re-run by hand once fixed:"
            log "      sudo bash $STACK_DIR/icedrive/setup-icedrive.sh"
        fi
    else
        log "  WARNING: ICEDRIVE_MODE=cli but no $STACK_DIR/icedrive/setup-icedrive.sh"
        log "    on the payload - carriage missing for an activated feature."
    fi
elif [ "$ICEDRIVE_MODE" = "off" ]; then
    log "ICEDRIVE_MODE=off - no IceDrive client of either kind on this box."
fi

# ── 6d. the AI CLI service (SR-019, A40 ratified 2026-09-09) ─────────────────
# A plain service, no container. Gated exactly like REMOTE_UI_ENABLED, and OFF
# by default: with the knob false the account is not created, the unit is not
# installed, and nothing listens.
#
# THE SCRIPT DOES THE REFUSING, NOT THIS BLOCK. setup-ai-cli.sh refuses
# AI_CLI_USER=hub or root, refuses an account that has since acquired sudo or
# docker, and refuses a bind address outside loopback and the docker bridge —
# and it re-checks all of that on EVERY boot, because an account created
# without sudo can be given it later. A refusal is exit 2 and is reported here
# as a WARNING with the reason, never swallowed.
if [ "${AI_CLI_ENABLED:-false}" = "true" ]; then
    log "provisioning the AI CLI service (activated by .env)…"
    if [ -f "$STACK_DIR/ai-cli/setup-ai-cli.sh" ]; then
        if bash "$STACK_DIR/ai-cli/setup-ai-cli.sh" 2>&1 | sed 's/^/  /'; then
            log "  AI CLI service installed and enabled on ${AI_CLI_BIND:-127.0.0.1}:${AI_CLI_PORT:-8791}"
            log "  STILL OWED BY A HUMAN: sign the CLIs in as ${AI_CLI_USER:-homehub-ai}"
            log "    over RDP (claude setup-token). The credential does not survive a"
            log "    reimage - the same posture as IceDrive."
        else
            log "  WARNING: setup-ai-cli.sh refused or failed - this box has NO AI CLI"
            log "    service. Everything else is unaffected. The reason is in the lines"
            log "    above; re-run by hand once fixed:"
            log "      sudo bash $STACK_DIR/ai-cli/setup-ai-cli.sh"
        fi
    else
        log "  WARNING: AI_CLI_ENABLED=true but no $STACK_DIR/ai-cli/setup-ai-cli.sh"
        log "    on the payload - carriage missing for an activated feature."
    fi
else
    log "AI_CLI_ENABLED is not true - no AI CLI service, no dedicated account."
fi


# ── 6e. the AI-usage feeder (SR-021, SN-016) ───────────────────────
# A plain service on a timer, no container, OFF by default. It runs as the
# account setup-ai-cli.sh created, because the vendor sign-ins it READS live in
# that account's home - so this block does nothing useful on a box where the AI
# CLI layer was never provisioned, and setup-ai-usage.sh refuses rather than
# creating a second account nobody certified.
#
# THE SCRIPT DOES THE REFUSING, NOT THIS BLOCK: a blank AI_USAGE_USER is a
# refusal and not a default, because guessing which household member's board
# these gauges land on puts one person's usage on another person's panel.
if [ "${AI_USAGE_ENABLED:-false}" = "true" ]; then
    log "provisioning the AI-usage feeder (activated by .env)…"
    if [ -f "$STACK_DIR/ai-usage/setup-ai-usage.sh" ]; then
        if bash "$STACK_DIR/ai-usage/setup-ai-usage.sh" 2>&1 | sed 's/^/  /'; then
            log "  AI-usage feeder installed; timer runs every 10 minutes"
            log "  STILL OWED BY A HUMAN: the vendor sign-ins live in"
            log "    ${AI_USAGE_USER_ACCOUNT:-homehub-ai}'s home and do not survive a"
            log "    reimage. A source with no credential posts 'unavailable', which"
            log "    is correct behaviour and not a fault to chase."
        else
            log "  WARNING: setup-ai-usage.sh refused or failed - this box posts NO"
            log "    usage gauges. Everything else is unaffected. The reason is in the"
            log "    lines above; re-run by hand once fixed:"
            log "      sudo bash $STACK_DIR/ai-usage/setup-ai-usage.sh"
        fi
    else
        log "  WARNING: AI_USAGE_ENABLED=true but no $STACK_DIR/ai-usage/setup-ai-usage.sh"
        log "    on the payload - carriage missing for an activated feature."
    fi
fi

# ── 7. done ──────────────────────────────────────────────────────────────────
date > "$MARKER"
# A defect found in step 4b is reported HERE, at the end, and as a NON-ZERO
# EXIT: everything else still ran (the stack is up, DNS is provisioned, the
# shares are mounted), but `systemctl status homehub-firstboot` must be RED and
# `systemctl is-failed` must say so. On a headless box the unit's state is the
# only surface a defect can appear on that is not a line in a scrolling journal.
#
# THE RELAY NO LONGER REPORTS HERE, and that is the design refresh rather than a
# regression (CROSSPLAY_HANDOFF.md §7). It used to: a `__gm3_failed` flag set in
# step 4-pre made THIS unit exit non-zero because the crossplay relay had been
# switched off. The relay now owns its own verdict —
# `systemctl is-failed homehub-gunmaster3-relay` — which is strictly better on
# every axis: it is visible without reading firstboot's log, it survives every
# subsequent boot instead of being a one-time exit code, it distinguishes "off
# by the knob" (condition not met, a clean skip) from "tried and could not be
# fenced" (failed), and it stops one optional service's problem from colouring
# the whole first boot red. Step 4a-relay logs its state so the journal still
# names it in one line.
if [ "$KIOSK_CONFIG_UNREADABLE" -ne 0 ]; then
    log "FATAL: bring-up finished, but the wall panel WILL run on js/config.js defaults"
    log "  (see the step 4b ERROR above). Exiting non-zero so this unit reports FAILED"
    log "  rather than letting a silently-degraded panel look like a clean first boot."
    exit 1
fi
log "bring-up complete. Verify with: bash $STACK_DIR/provision/healthcheck.sh"
