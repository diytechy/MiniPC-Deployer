#!/usr/bin/env bash
# icedrive-profile.sh — carry the IceDrive client's identity across a reflash.
#
# WHAT A REIMAGE DESTROYS THAT NOTHING ELSE REPLACES. The IceDrive client's sync
# PAIRS are server-side — measured 2026-08-29, a rebooted client asked the API
# and got them back with no local state involved. What is NOT server-side is the
# ability to sign in: `~/.config/Icedrive/Icedrive.conf` holds `icedrive_login`,
# the `icedrivet` API token and `icedrive_stored_cred`, and the QtWebEngine
# profile under `~/.local/share/Icedrive/` holds the web session cookies. The
# account has 2FA, so without those a reflashed box cannot resume the offsite
# copy until a human sits down at it.
#
# ── THIS SET IS ENCRYPTED, AND THE REASON IS SPECIFIC ────────────────────────
#
# Raised as a high-risk design decision and reviewed adversarially (gpt-5.6-sol,
# 2026-08-29). Verdict: APPROVE WITH CHANGES, and the reasoning is worth keeping
# because it is the part that is easy to get wrong.
#
# The Owner's standing threat-model ruling — "a credential is not a finding for
# living where the deployment mechanism already puts it" (2026-08-09) — does NOT
# cover this. That ruling was about a password on argv for two seconds, inside
# the hub's existing privilege boundary, readable anyway by the only account
# that could look. This proposal creates a DURABLE SECOND COPY IN A DIFFERENT
# TRUST BOUNDARY: a removable, unencrypted exFAT drive that is designed to be
# carried away and is planned to move to other hardware. Theft of that drive
# today costs documents and finance data; with a plaintext profile on it, it
# would also hand over a live bearer session to the cloud account — no password
# reset, no mail loop, no 2FA.
#
# So the archive is encrypted with a key that lives ONLY in the deploy secret
# store and reaches the box in `.env` (0600 root). The two halves then travel on
# different media: the CIPHERTEXT rides the backup drive, the KEY rides the
# install stick. Either alone is useless.
#
# The other four gates the review required are implemented here or named where
# they live:
#   * QUIESCE BEFORE CAPTURE — SQLite plus a WAL copied live is not a
#     guaranteed-consistent copy. --capture stops the client, copies, restarts.
#   * RESTORE ONLY ONTO A CLEAN PROFILE, 0600, correct ownership. A restore over
#     an existing profile would overwrite state the client has already rebuilt.
#   * FAIL CLOSED. A missing key, a failed decrypt, a failed integrity check or
#     an unsafe path leaves the client NOT running and says so. Half a profile
#     is worse than none.
#   * DO NOT START THE CLIENT AGAINST AN UNVERIFIED MOUNT — that gate lives in
#     `remote-ui/icedrive-gate.sh`, and it is independently mandatory: pairs
#     carry ABSOLUTE local paths, and a two-way pair scanning an unmounted
#     mountpoint sees an empty tree. "Empty" and "deleted" are the same
#     observation to a sync engine.
#
# Usage: icedrive-profile.sh --capture | --restore | --verify [options]
#   --capture   stop the client, archive + encrypt the profile, restart it
#   --restore   decrypt + unpack onto a CLEAN profile (refuses otherwise)
#   --verify    decrypt to a scratch dir and check the archive is readable and
#               holds what it should — proves the capture is restorable without
#               touching the live profile
#   --user U    the account that owns the profile (default $ICEDRIVE_PROFILE_USER
#               or `hub`)
#   --out DIR   where the encrypted archive lives (default /var/lib/homehub/icedrive)
#   --force     --restore only: allow restoring over a non-empty profile
#
# Exit: 0 ok, 1 failed (loudly), 2 usage, 3 nothing to do (feature off / no
# profile / no archive) — 3 is distinct because "there is no IceDrive on this
# box" must never look like "the recovery is broken".
set -uo pipefail

MODE=""; FORCE=0
PROF_USER="${ICEDRIVE_PROFILE_USER:-hub}"
OUTDIR="${ICEDRIVE_PROFILE_DIR:-/var/lib/homehub/icedrive}"
ENV_FILE="${ICEDRIVE_ENV_FILE:-/opt/homehub/stack/.env}"
ARCHIVE_NAME="icedrive-profile.tar.gz.gpg"

while [ $# -gt 0 ]; do
    case "$1" in
        --capture) MODE=capture; shift ;;
        --restore) MODE=restore; shift ;;
        --verify)  MODE=verify;  shift ;;
        --user)    PROF_USER="$2"; shift 2 ;;
        --out)     OUTDIR="$2";  shift 2 ;;
        --env)     ENV_FILE="$2"; shift 2 ;;
        --force)   FORCE=1; shift ;;
        -h|--help) sed -n '2,66p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done
[ -n "$MODE" ] || { echo "usage: icedrive-profile.sh --capture|--restore|--verify" >&2; exit 2; }

log()  { printf '%s [icedrive-profile] %s\n' "$(date -u +%FT%TZ)" "$*"; }
warn() { printf '%s [icedrive-profile] WARN: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }
die()  { printf '%s [icedrive-profile] ERROR: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; exit 1; }

HOMEDIR="$(getent passwd "$PROF_USER" 2>/dev/null | cut -d: -f6)"
[ -n "$HOMEDIR" ] || { log "no account '$PROF_USER' on this box — nothing to do"; exit 3; }

# THE THREE PATHS, and why exactly these. `~/.cache/Icedrive` is deliberately
# excluded: it is an HTTP cache, it is large, and it is re-creatable.
REL_PATHS=(".config/Icedrive" ".local/share/Icedrive" ".config/autostart")

# ── the key ──────────────────────────────────────────────────────────────────
# From .env, which is 0600 root:root and is materialised onto the install stick
# from the deploy store. NEVER read from the backup drive, never logged, never
# written next to the ciphertext.
read_key() {
    local k=""
    if [ -n "${ICEDRIVE_PROFILE_KEY:-}" ]; then
        printf '%s' "$ICEDRIVE_PROFILE_KEY"; return 0
    fi
    [ -r "$ENV_FILE" ] || return 1
    # Deliberately not `source`: a compose .env holds bcrypt hashes full of `$`,
    # and sourcing one is how first boot died (see backup/common.sh load_env_file).
    k="$(awk -F= '$1=="ICEDRIVE_PROFILE_KEY"{sub(/^[^=]*=/,""); gsub(/^"|"$/,""); print; exit}' "$ENV_FILE" 2>/dev/null)"
    [ -n "$k" ] || return 1
    printf '%s' "$k"
}

command -v gpg >/dev/null 2>&1 || die "gpg is not installed — this archive is encrypted and there is no unauthenticated fallback by design"

KEY="$(read_key)" || KEY=""
if [ -z "$KEY" ]; then
    # FAIL CLOSED, AND SAY WHICH HALF IS MISSING. A capture that silently wrote
    # plaintext because the key was absent would defeat the entire review this
    # design went through.
    case "$MODE" in
        capture) die "ICEDRIVE_PROFILE_KEY is not set in $ENV_FILE. Refusing to capture: the only alternative to encrypting is writing an IceDrive session token onto a removable drive in the clear, which is the thing this file exists to not do." ;;
        *)       die "ICEDRIVE_PROFILE_KEY is not set in $ENV_FILE — the archive cannot be decrypted. The key rides the INSTALL STICK; the ciphertext rides the BACKUP DRIVE. You need both." ;;
    esac
fi

# gpg reads the passphrase from a fd so it never appears in argv or in the
# process table. --batch/--pinentry-mode loopback keep it non-interactive.
gpg_common=(--batch --yes --quiet --pinentry-mode loopback --passphrase-fd 3)

# ── the desktop session's environment, and why this is load-bearing ──────────
#
# MEASURED THE HARD WAY, 2026-08-29. The first cut of this script stopped the
# client, captured, and restarted it with `DISPLAY=:0`. The capture worked; the
# restart did not, and the offsite sync stayed DOWN. This box's session is xrdp,
# so the display is `:10.0` — and a GUI client launched at the wrong display
# exits immediately, quietly, with the gate's journal line still saying
# "starting /opt/icedrive/Icedrive.AppImage". Everything looked like it had
# worked.
#
# So the environment is READ OFF THE RUNNING SESSION rather than assumed, and
# the whole quiesce is gated on being able to read it: a capture that cannot put
# the client back must not take it down. That is the same fail-closed shape as
# the rest of this file, applied to the one action here with a lasting cost.
SESSION_PID=""
session_pid() {
    [ -n "$SESSION_PID" ] && { printf '%s' "$SESSION_PID"; return 0; }
    local p
    for pat in xfce4-session gnome-session-binary mate-session lxsession; do
        p="$(pgrep -u "$PROF_USER" -x "$pat" 2>/dev/null | head -1)"
        [ -n "$p" ] && { SESSION_PID="$p"; printf '%s' "$p"; return 0; }
    done
    return 1
}

# session_env — echoes `KEY=VALUE` lines for the four variables a GUI app on this
# box needs. Empty output (and nonzero) means we could not find a session.
session_env() {
    local p; p="$(session_pid)" || return 1
    [ -r "/proc/$p/environ" ] || return 1
    tr '\0' '\n' < "/proc/$p/environ" 2>/dev/null | \
        grep -E '^(DISPLAY|XAUTHORITY|DBUS_SESSION_BUS_ADDRESS|XDG_RUNTIME_DIR)=' || return 1
}

# client_pids — the client's processes, and NOT this script's own ancestry.
#
# MEASURED, 2026-08-29, and it is the nastiest kind of bug: a bare
# `pgrep -u hub -f Icedrive` matched THE SSH COMMAND LINE THAT WAS RUNNING THIS
# SCRIPT, because that command line contained the word "Icedrive". So the
# capture reported "quiescing: TERM to IceDrive pid(s) 108144" against a box
# where the client was not running at all, and then reported "client is back
# (after 1s)" against the same false match. Both lines were wrong and both read
# as success.
#
# Two defences, because either alone still lies:
#   * ANCHOR THE PATTERN to the paths the client actually runs from — the
#     AppImage, the gate that execs it, and the AppImage's own FUSE mount
#     (/tmp/.mount_Icedri*, which is where its QtWebEngine helpers live).
#   * SUBTRACT THIS PROCESS'S ANCESTRY. An `ssh … homehub-icedrive-profile`
#     invocation is a parent of this script, so pgrep's own self-exclusion does
#     not cover it.
ANCESTORS=""
_ancestors() {
    [ -n "$ANCESTORS" ] && { printf '%s' "$ANCESTORS"; return 0; }
    local p=$$ ppid n=0
    while [ "$p" -gt 1 ] && [ "$n" -lt 40 ]; do
        ANCESTORS="${ANCESTORS:+$ANCESTORS }$p"
        # PARSED AFTER THE LAST ')', NOT BY FIELD NUMBER. /proc/PID/stat's second
        # field is the comm in parentheses and it can contain SPACES, which
        # shifts every later field - so `awk '{print $4}'` reads the wrong
        # column for exactly the processes (a `bash -c ...`) most likely to be
        # in this ancestry.
        ppid="$(sed 's/.*) //' "/proc/$p/stat" 2>/dev/null | awk '{print $2}')" || break
        [ -n "$ppid" ] || break
        p="$ppid"; n=$((n+1))
    done
    printf '%s' "$ANCESTORS"
}

client_pids() {
    local anc; anc=" $(_ancestors) "
    pgrep -u "$PROF_USER" -f '/opt/icedrive/(Icedrive\.AppImage|icedrive-gate\.sh)|/tmp/\.mount_Icedri' 2>/dev/null |
        while IFS= read -r pid; do
            case "$anc" in *" $pid "*) continue ;; esac
            printf '%s\n' "$pid"
        done
}


stop_client() {
    local pids; pids="$(client_pids)"
    [ -n "$pids" ] || { log "the client is not running — nothing to quiesce"; return 0; }
    # REFUSE TO STOP WHAT WE CANNOT START. See the note above: this is the only
    # irreversible thing this script does, and it costs the household's offsite
    # copy until somebody notices.
    if ! session_env >/dev/null 2>&1; then
        die "IceDrive is running but no desktop session was found for '$PROF_USER', so this capture could stop the client and not be able to restart it. Refusing. Nothing was touched. (Capture from within the session, or stop the client deliberately first.)"
    fi
    log "quiescing: TERM to IceDrive pid(s) $(printf '%s' "$pids" | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill -TERM $pids 2>/dev/null || true
    local w=0
    while [ "$w" -lt 20 ] && [ -n "$(client_pids)" ]; do sleep 1; w=$((w+1)); done
    if [ -n "$(client_pids)" ]; then
        # NOT `kill -9` AND CARRY ON. A SIGKILL mid-write is precisely the
        # inconsistent copy the quiesce exists to avoid, so a client that will
        # not stop means this capture does not happen.
        die "IceDrive did not exit within 20s. Refusing to copy a live SQLite database plus WAL — that is not a guaranteed-consistent copy, and a capture nobody can trust is worse than no capture."
    fi
    log "client stopped after ${w}s"
    return 0
}

start_client() {
    # Started the way the desktop session starts it, THROUGH THE GATE, so a
    # capture can never bypass the mount check.
    local desktop="$HOMEDIR/.config/autostart/icedrive.desktop" exec_line="" env_lines=""
    [ -f "$desktop" ] || { warn "no $desktop — not restarting the client (it will come back with the next desktop session)"; return 0; }
    exec_line="$(awk -F= '$1=="Exec"{sub(/^[^=]*=/,""); print; exit}' "$desktop")"
    [ -n "$exec_line" ] || { warn "no Exec= in $desktop — not restarting"; return 0; }
    env_lines="$(session_env)" || { warn "no desktop session for $PROF_USER — not restarting the client here. It starts with the session."; return 0; }
    log "restarting the client into the live session ($(printf '%s' "$env_lines" | grep '^DISPLAY=' || echo 'DISPLAY=?'))"
    # setsid so it survives this script; runuser so it runs as the profile's
    # owner without a login shell rewriting the environment we just measured.
    # shellcheck disable=SC2086
    setsid runuser -u "$PROF_USER" -- env $(printf '%s ' $env_lines) nohup sh -c "$exec_line" >/dev/null 2>&1 &
    disown 2>/dev/null || true
    # VERIFY IT IS ACTUALLY THERE. "started" and "stayed up" are different
    # claims, and the first cut of this made only the first one.
    local w=0
    while [ "$w" -lt 15 ] && [ -z "$(client_pids)" ]; do sleep 1; w=$((w+1)); done
    if [ -n "$(client_pids)" ]; then
        log "client is back (after ${w}s)"
    else
        warn "the client did NOT come back after ${w}s. The offsite sync is DOWN until the next session start."
        warn "  restart it by hand from the desktop, or reboot. The capture itself succeeded."
    fi
    return 0
}

case "$MODE" in
capture)
    have=0
    for r in "${REL_PATHS[@]}"; do [ -e "$HOMEDIR/$r" ] && have=1; done
    [ "$have" = 1 ] || { log "no IceDrive profile under $HOMEDIR — nothing to capture"; exit 3; }
    mkdir -p "$OUTDIR" || die "cannot create $OUTDIR"
    chmod 0700 "$OUTDIR" 2>/dev/null || true

    # WAS THE CLIENT RUNNING? Recorded so it can be put back the way it was, and
    # so a capture taken while it was already down is not silently different.
    WAS_RUNNING=0; [ -n "$(client_pids)" ] && WAS_RUNNING=1
    stop_client

    tmp="$(mktemp -d)" || die "mktemp failed"
    # An if, not `A && B`: as the last command of an EXIT trap that shape sets
    # the trap's status from a test nobody is reading, and this project has
    # already been bitten once by `||`/`&&` associativity today.
    trap 'rm -rf "$tmp"; if [ "${WAS_RUNNING:-0}" = 1 ]; then start_client; fi' EXIT
    # -h is NOT used: symlinks are stored as symlinks and the restore refuses
    # any that escape the profile. Storing their targets would silently pull in
    # whatever they point at.
    ( cd "$HOMEDIR" && tar -czf "$tmp/p.tar.gz" --ignore-failed-read -- "${REL_PATHS[@]}" 2>/dev/null ) || \
        die "tar failed over ${REL_PATHS[*]} in $HOMEDIR"
    printf '%s' "$KEY" | gpg "${gpg_common[@]}" --symmetric --cipher-algo AES256 \
        -o "$tmp/p.gpg" "$tmp/p.tar.gz" 3<&0 </dev/null || die "gpg encryption failed"

    # PROVE IT DECRYPTS BEFORE REPLACING THE LAST GOOD ONE. An archive that
    # cannot be read is worse than a stale one, because it looks like a backup.
    printf '%s' "$KEY" | gpg "${gpg_common[@]}" -d "$tmp/p.gpg" 3<&0 </dev/null 2>/dev/null | tar -tzf - >/dev/null 2>&1 || \
        die "the archive just written does not decrypt and list — refusing to replace the previous capture"

    # MODE FIRST, OWNERSHIP SEPARATELY AND ONLY AS ROOT. `install -o root -g root`
    # fails outright for a non-root caller - and it fails AFTER copying the file,
    # so the archive lands, the command returns nonzero, and the run dies having
    # half-succeeded. In production this runs as root from a timer; in a test it
    # does not, and a script that can only be exercised as root is a script that
    # does not get exercised. 0600 is the part that matters either way.
    install -m 0600 "$tmp/p.gpg" "$OUTDIR/$ARCHIVE_NAME" || die "could not install the archive"
    if [ "$(id -u)" = 0 ]; then chown root:root "$OUTDIR/$ARCHIVE_NAME" 2>/dev/null || warn "could not chown the archive to root"; fi
    # A plaintext sidecar of NON-SECRET facts, so a human (or a check) can see
    # when the capture last succeeded without holding the key.
    {
        echo "captured_utc=$(date -u +%FT%TZ)"
        echo "user=$PROF_USER"
        echo "paths=${REL_PATHS[*]}"
        echo "bytes=$(stat -c '%s' "$OUTDIR/$ARCHIVE_NAME" 2>/dev/null || echo 0)"
        echo "sha256=$(sha256sum "$OUTDIR/$ARCHIVE_NAME" 2>/dev/null | cut -d' ' -f1)"
        echo "client_was_running=$WAS_RUNNING"
    } >"$OUTDIR/capture.info.tmp" && mv "$OUTDIR/capture.info.tmp" "$OUTDIR/capture.info"
    chmod 0644 "$OUTDIR/capture.info" 2>/dev/null || true
    log "captured $(stat -c '%s' "$OUTDIR/$ARCHIVE_NAME") byte(s) -> $OUTDIR/$ARCHIVE_NAME (encrypted, quiesced)"
    exit 0
    ;;

verify)
    [ -f "$OUTDIR/$ARCHIVE_NAME" ] || { log "no archive at $OUTDIR/$ARCHIVE_NAME — nothing to verify"; exit 3; }
    tmp="$(mktemp -d)" || die "mktemp failed"
    trap 'rm -rf "$tmp"' EXIT
    printf '%s' "$KEY" | gpg "${gpg_common[@]}" -d "$OUTDIR/$ARCHIVE_NAME" 3<&0 </dev/null 2>"$tmp/gpg.err" >"$tmp/p.tar.gz" || \
        die "decrypt FAILED: $(tr -s ' \n' ' ' <"$tmp/gpg.err" | cut -c1-200)"
    tar -tzf "$tmp/p.tar.gz" >"$tmp/list" 2>/dev/null || die "the decrypted bytes are not a readable tar"
    n="$(grep -c . "$tmp/list" || true)"
    # THE CONFIG FILE IS THE POINT OF THE WHOLE ARCHIVE. An archive that unpacks
    # cleanly but holds no Icedrive.conf restores a box that still cannot sign in.
    if grep -q '^\.config/Icedrive/Icedrive\.conf$' "$tmp/list"; then
        log "verify OK: $n entr(ies), and .config/Icedrive/Icedrive.conf is among them"
        exit 0
    fi
    die "the archive decrypts and lists $n entr(ies) but holds NO .config/Icedrive/Icedrive.conf — restoring it would leave the box still unable to sign in"
    ;;

restore)
    [ -f "$OUTDIR/$ARCHIVE_NAME" ] || { log "no archive at $OUTDIR/$ARCHIVE_NAME — nothing to restore"; exit 3; }
    # ONLY ONTO A CLEAN PROFILE. On a reimage the home directory is new and this
    # is trivially true; anywhere else, a restore would overwrite state the
    # client has already rebuilt — including a token it refreshed after the
    # capture. --force exists for a deliberate operator, not for the timer.
    if [ "$FORCE" != 1 ]; then
        for r in ".config/Icedrive" ".local/share/Icedrive"; do
            if [ -n "$(ls -A "$HOMEDIR/$r" 2>/dev/null)" ]; then
                log "$HOMEDIR/$r is not empty — declining (this is not a fresh install). --force overrides."
                exit 3
            fi
        done
    fi
    tmp="$(mktemp -d)" || die "mktemp failed"
    trap 'rm -rf "$tmp"' EXIT
    printf '%s' "$KEY" | gpg "${gpg_common[@]}" -d "$OUTDIR/$ARCHIVE_NAME" 3<&0 </dev/null 2>"$tmp/gpg.err" >"$tmp/p.tar.gz" || \
        die "decrypt FAILED: $(tr -s ' \n' ' ' <"$tmp/gpg.err" | cut -c1-200). Nothing was written."
    tar -tzf "$tmp/p.tar.gz" >"$tmp/list" 2>/dev/null || die "the decrypted bytes are not a readable tar. Nothing was written."

    # PATH VALIDATION BEFORE ANY WRITE. tar is being asked to unpack into a home
    # directory as root; an absolute path, a `..` component or a symlink whose
    # target escapes the profile would write outside it. Checked on the LISTING,
    # so nothing has been created yet when the refusal happens.
    bad="$(awk '
        /^\// { print "absolute: " $0; next }
        /(^|\/)\.\.(\/|$)/ { print "traversal: " $0; next }
        $0 !~ /^\.config\/(Icedrive|autostart)(\/|$)/ && $0 !~ /^\.local\/share\/Icedrive(\/|$)/ { print "outside the profile: " $0 }
    ' "$tmp/list" | head -5)"
    [ -z "$bad" ] || die "the archive holds paths that do not belong in an IceDrive profile — refusing to unpack: $(printf '%s' "$bad" | tr '\n' '; ')"

    tar -xzf "$tmp/p.tar.gz" -C "$HOMEDIR" || die "unpack failed — the profile may be partial; delete $HOMEDIR/.config/Icedrive and $HOMEDIR/.local/share/Icedrive before retrying"
    # OWNERSHIP AND MODE, EXPLICITLY. Unpacked as root, these would otherwise be
    # root-owned and the client (running as the profile's user) could not read
    # its own token — a failure that presents as "the sign-in did not survive".
    for r in "${REL_PATHS[@]}"; do
        [ -e "$HOMEDIR/$r" ] || continue
        chown -R "$PROF_USER:$PROF_USER" "$HOMEDIR/$r" 2>/dev/null || warn "could not chown $HOMEDIR/$r"
    done
    chmod 0700 "$HOMEDIR/.config/Icedrive" "$HOMEDIR/.local/share/Icedrive" 2>/dev/null || true
    chmod 0600 "$HOMEDIR/.config/Icedrive/Icedrive.conf" 2>/dev/null || true
    log "restored $(grep -c . "$tmp/list") entr(ies) into $HOMEDIR as $PROF_USER"
    log "  the client will sign in from the carried token. If IceDrive rejects it as a new device,"
    log "  that is an EXPECTED fallback, not a failure of this step: sign in once by hand and re-capture."
    exit 0
    ;;
esac
