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
# that could look. This creates a DURABLE SECOND COPY IN A DIFFERENT TRUST
# BOUNDARY: a removable, unencrypted exFAT drive that is designed to be carried
# away and is planned to move to other hardware. Theft of that drive today costs
# documents and finance data; with a plaintext profile on it, it would also hand
# over a live bearer session to the cloud account — no password reset, no mail
# loop, no 2FA.
#
# So the archive is encrypted with a key that lives ONLY in the deploy secret
# store and reaches the box in `.env` (0600 root). The two halves then travel on
# different media: the CIPHERTEXT rides the backup drive, the KEY rides the
# install stick. Either alone is useless.
#
# ── AND A SECOND REVIEW PASS, WHICH FOUND FOURTEEN THINGS ────────────────────
#
# The first working version of this file passed 28 hermetic assertions and was
# still wrong in ways only a second adversarial read surfaced. The three that
# mattered most, because each turned a safety claim above into a false sentence:
#
#   * THE PATH VALIDATION WAS NAME-ONLY. It read `tar -tzf` and checked member
#     NAMES, so a member named `.config/Icedrive` that is a SYMLINK to `/etc`
#     passed — and this extracts as root into a home directory. The archive is
#     now unpacked into a scratch directory, inspected there for links and
#     device nodes, and only then copied in; and the destination roots are
#     refused if they are themselves links.
#   * `tar --ignore-failed-read` MEANT A CAPTURE COULD SUCCEED WITHOUT THE
#     CREDENTIAL. It is gone, and capture, verify and restore all now require
#     `.config/Icedrive/Icedrive.conf` before they commit to anything.
#   * A FAILED RESTART REPORTED SUCCESS. The client is stopped to get a
#     consistent copy; if it does not come back, the household's offsite sync is
#     down. That now exits non-zero, and "came back" means the APPIMAGE is still
#     running three seconds later — not that the gate was seen once, which is a
#     process whose job includes exiting.
#
# The gates the first review demanded, all still here and all asserted in
# `tests/profile.test.sh`:
#   * QUIESCE BEFORE CAPTURE — SQLite plus a WAL copied live is not a
#     guaranteed-consistent copy.
#   * RESTORE ONLY ONTO A CLEAN PROFILE, 0600, correct ownership — and ownership
#     and mode failures are now FATAL rather than warnings, because a profile the
#     client cannot read is a restore that did not happen.
#   * FAIL CLOSED. A missing key, a failed decrypt, a failed integrity check or
#     an unsafe path leaves the client NOT running and says so.
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
# profile / no archive / another run holds the lock) — 3 is distinct because
# "there is no IceDrive on this box" must never look like "the recovery is
# broken".
set -uo pipefail

MODE=""; FORCE=0
PROF_USER="${ICEDRIVE_PROFILE_USER:-hub}"
OUTDIR="${ICEDRIVE_PROFILE_DIR:-/var/lib/homehub/icedrive}"
ENV_FILE="${ICEDRIVE_ENV_FILE:-/opt/homehub/stack/.env}"
ARCHIVE_NAME="icedrive-profile.tar.gz.gpg"
# `--user`, `--out` and `--env` each take a value; without this check an option
# in final position expands an unset $2 and dies on `set -u` with an
# unbound-variable message instead of the documented usage exit.
need_arg() { [ "$1" -ge 2 ] || { echo "$2 needs a value" >&2; exit 2; }; }

while [ $# -gt 0 ]; do
    case "$1" in
        --capture) MODE=capture; shift ;;
        --restore) MODE=restore; shift ;;
        --verify)  MODE=verify;  shift ;;
        --user)    need_arg $# --user; PROF_USER="$2"; shift 2 ;;
        --out)     need_arg $# --out;  OUTDIR="$2";   shift 2 ;;
        --env)     need_arg $# --env;  ENV_FILE="$2"; shift 2 ;;
        --force)   FORCE=1; shift ;;
        -h|--help) sed -n '2,88p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done
[ -n "$MODE" ] || { echo "usage: icedrive-profile.sh --capture|--restore|--verify" >&2; exit 2; }

log()  { printf '%s [icedrive-profile] %s\n' "$(date -u +%FT%TZ)" "$*"; }
warn() { printf '%s [icedrive-profile] WARN: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }
die()  { printf '%s [icedrive-profile] ERROR: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; exit 1; }

# ICEDRIVE_PROFILE_HOME IS A TEST SEAM, and it exists because the alternative
# was worse. tests/profile.test.sh has to build a profile somewhere this script
# will look at; without an override that means the REAL home directory, stashed
# aside and restored afterwards — which is a test that can lose a household's
# cloud sign-in if it dies at the wrong moment. It ran that way exactly once, on
# the production box, and the profile survived. Once was enough.
HOMEDIR="${ICEDRIVE_PROFILE_HOME:-}"
if [ -z "$HOMEDIR" ]; then
    HOMEDIR="$(getent passwd "$PROF_USER" 2>/dev/null | cut -d: -f6)"
fi
[ -n "$HOMEDIR" ] || { log "no account '$PROF_USER' on this box — nothing to do"; exit 3; }
[ -d "$HOMEDIR" ] || { log "$HOMEDIR is not a directory — nothing to do"; exit 3; }

# THE PATHS, and why exactly these. `~/.cache/Icedrive` is deliberately
# excluded: it is an HTTP cache, it is large, and it is re-creatable.
#
# THE AUTOSTART ENTRY IS NAMED, NOT ITS DIRECTORY. Capturing `.config/autostart`
# wholesale archives every other application's autostart entry, restores them
# over a live desktop, and hands the whole directory to a `chown -R` that has no
# business touching it. One file is what this feature owns.
REL_PATHS=(".config/Icedrive" ".local/share/Icedrive" ".config/autostart/icedrive.desktop")
# The member that makes the archive worth having. An archive that unpacks
# cleanly without it restores a box that still cannot sign in.
REQUIRED_MEMBER=".config/Icedrive/Icedrive.conf"

# ── one at a time ────────────────────────────────────────────────────────────
# The nightly timer and a hand run can overlap, and every mode here either
# replaces the archive or stops the client. Two at once can leave a half-written
# archive, a client stopped by one and restarted by the other, or a restore
# reading a file the capture is replacing.
#
# /run FIRST, AND NOT /tmp, AND THAT IS NOT TIDINESS. Measured on the box: with
# the lock in /tmp, root got `Permission denied` opening a file `hub` had created
# there — Ubuntu ships `fs.protected_regular=1`, which stops root following into
# another user's file inside a sticky world-writable directory. So the lock lived
# in the one place where the service that needs it cannot take it. /run is
# root-owned and tmpfs; the TMPDIR fallback keeps an unprivileged test working.
#
# `exec 9>FILE 2>/dev/null` IS A TRAP AND THE FIRST CUT FELL IN IT. `exec` with
# only redirections applies them to the CURRENT shell PERMANENTLY, so a
# `2>/dev/null` written to hide one possible error silenced every warn and die
# for the rest of the run. Measured: the no-key refusal printed nothing at all.
# Probe writability separately, then open the fd with no stray redirection.
if [ -n "${ICEDRIVE_PROFILE_LOCK:-}" ]; then
    LOCKFILE="$ICEDRIVE_PROFILE_LOCK"
else
    LOCKFILE="/run/icedrive-profile.$PROF_USER.lock"
    # `2>/dev/null` BEFORE the append: redirections are set up left to right, and
    # a failure of the append is reported while stderr is still the terminal.
    if ! : 2>/dev/null >>"$LOCKFILE"; then
        LOCKFILE="${TMPDIR:-/tmp}/.icedrive-profile.$(id -u).$PROF_USER.lock"
    fi
fi
if command -v flock >/dev/null 2>&1 && : 2>/dev/null >>"$LOCKFILE"; then
    exec 9>>"$LOCKFILE"
    if ! flock -n 9; then
        log "another icedrive-profile run holds $LOCKFILE — declining rather than racing it"
        exit 3
    fi
else
    warn "no lock at $LOCKFILE — running unserialised. A concurrent capture could race this one."
fi

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
# MEASURED THE HARD WAY, 2026-08-29. The first cut stopped the client, captured,
# and restarted it with `DISPLAY=:0`. The capture worked; the restart did not,
# and the offsite sync stayed DOWN. This box's session is xrdp, so the display is
# `:10.0` — and a GUI client launched at the wrong display exits immediately,
# quietly, with the gate's journal line still saying "starting …". Everything
# looked like it had worked.
#
# So the environment is READ OFF THE RUNNING SESSION rather than assumed, and the
# whole quiesce is gated on being able to read it: a capture that cannot put the
# client back must not take it down.
#
# DISPLAY IS THE REQUIRED ONE. The others are passed when present and are not
# demanded, because this box's xfce4-session genuinely carries no XAUTHORITY —
# requiring all four (the obvious reading) would refuse to capture on the only
# machine this runs on.
SESSION_PID=""
session_pid() {
    [ -n "$SESSION_PID" ] && { printf '%s' "$SESSION_PID"; return 0; }
    local p pat
    for pat in xfce4-session gnome-session-binary mate-session lxsession; do
        p="$(pgrep -u "$PROF_USER" -x "$pat" 2>/dev/null | head -1)"
        [ -n "$p" ] && { SESSION_PID="$p"; printf '%s' "$p"; return 0; }
    done
    return 1
}

# session_env_load — fills SESSION_ENV with KEY=VALUE strings, 0 if DISPLAY is
# among them. AN ARRAY, NOT A STRING: `env $(printf '%s ' $lines)` word-splits
# and globs, so a value holding a space or a `*` arrives mangled or expanded.
SESSION_ENV=()
session_env_load() {
    SESSION_ENV=()
    local p line e; p="$(session_pid)" || return 1
    [ -r "/proc/$p/environ" ] || return 1
    while IFS= read -r -d '' line; do
        case "$line" in
            DISPLAY=*|XAUTHORITY=*|DBUS_SESSION_BUS_ADDRESS=*|XDG_RUNTIME_DIR=*)
                SESSION_ENV+=("$line") ;;
        esac
    done < "/proc/$p/environ"
    for e in ${SESSION_ENV[@]+"${SESSION_ENV[@]}"}; do
        case "$e" in DISPLAY=?*) return 0 ;; esac
    done
    return 1
}

# ── which processes are the client ───────────────────────────────────────────
#
# MEASURED, 2026-08-29, and it is the nastiest kind of bug: a bare
# `pgrep -u hub -f Icedrive` matched THE SSH COMMAND LINE THAT WAS RUNNING THIS
# SCRIPT, because that command line contained the word "Icedrive". So the capture
# reported "quiescing: TERM to IceDrive pid(s) 108144" on a box where the client
# was not running at all, and then "client is back (after 1s)" on the same false
# match. Both lines were wrong and both read as success.
#
# Two defences, because either alone still lies: anchor the pattern to the paths
# the client actually runs from, and subtract this process's own ancestry (pgrep
# excludes itself, not its parents).
#
# TWO PATTERNS, NOT ONE. `client_pids` INCLUDES the gate, because for stopping
# purposes the gate is the client (it execs into it). `app_pids` EXCLUDES it,
# because for "did it come back" purposes a gate that is about to refuse the
# mount and exit is not a running client — it is a process that will be gone in a
# moment, and counting it made "client is back" a sentence about the wrong thing.
CLIENT_RE='/opt/icedrive/(Icedrive\.AppImage|icedrive-gate\.sh)|/tmp/\.mount_Icedri'
APP_RE='/opt/icedrive/Icedrive\.AppImage|/tmp/\.mount_Icedri'
ANCESTORS=""
_ancestors() {
    [ -n "$ANCESTORS" ] && { printf '%s' "$ANCESTORS"; return 0; }
    local p=$$ ppid n=0
    while [ "$p" -gt 1 ] && [ "$n" -lt 40 ]; do
        ANCESTORS="${ANCESTORS:+$ANCESTORS }$p"
        # PARSED AFTER THE LAST ')', NOT BY FIELD NUMBER. /proc/PID/stat's second
        # field is the comm in parentheses and it can contain SPACES, which
        # shifts every later field — for exactly the `bash -c …` processes most
        # likely to be in this ancestry.
        ppid="$(sed 's/.*) //' "/proc/$p/stat" 2>/dev/null | awk '{print $2}')" || break
        [ -n "$ppid" ] || break
        p="$ppid"; n=$((n+1))
    done
    printf '%s' "$ANCESTORS"
}
_pids_matching() {
    local anc pid; anc=" $(_ancestors) "
    pgrep -u "$PROF_USER" -f "$1" 2>/dev/null |
        while IFS= read -r pid; do
            case "$anc" in *" $pid "*) continue ;; esac
            printf '%s\n' "$pid"
        done
}
client_pids() { _pids_matching "$CLIENT_RE"; }
app_pids()    { _pids_matching "$APP_RE"; }

stop_client() {
    local pids w=0; pids="$(client_pids)"
    [ -n "$pids" ] || { log "the client is not running — nothing to quiesce"; return 0; }
    # REFUSE TO STOP WHAT WE CANNOT START. This is the only irreversible thing
    # this script does, and it costs the household's offsite copy until somebody
    # notices.
    if ! session_env_load; then
        die "IceDrive is running but no desktop session with a DISPLAY was found for '$PROF_USER', so this capture could stop the client and not be able to restart it. Refusing. Nothing was touched."
    fi
    log "quiescing: TERM to IceDrive pid(s) $(printf '%s' "$pids" | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill -TERM $pids 2>/dev/null || true
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

# start_client — 0 only if the app came back AND reached a WORKING state.
#
# ── WHY THIS IS NOT "IS IT ALIVE AT T+3" ANY MORE (C36, 2026-08-29) ──────────
# The previous version slept 3s and re-checked for a pid. It reported SUCCESS
# over a client that was dying as it looked. Measured, from the unit journal and
# the client's own log lined up to the second:
#
#   20:44:28    restart issued
#   20:44:30-32 client starts, authenticates, reads storage stats, gets its pairs
#   20:44:33.20 `WS client message: RemoteHostClosedError` — and the log ends
#   20:44:33    this function logged "still running (after 1s)" and returned 0
#
# The assertion passed in the SAME SECOND the client died. The unit exited 0, the
# nightly timer would have done the same, and the household's offsite copy was
# down until verify-hub.sh's TC-H-S09 noticed. A liveness probe that samples one
# instant cannot tell "started" from "started and about to die"; the fix is to
# wait for evidence the client is DOING ITS JOB, not that it exists.
#
# ── THE TWO OUTCOMES ARE DELIBERATELY NOT SYMMETRIC ─────────────────────────
# A longer window would fail a box that is simply quiet. So:
#
#   * THE PROCESS DIES during the window  -> HARD FAIL. This is the observed bug
#     and the one that must never again be reported as success.
#   * ALIVE, but no readiness marker      -> SUCCEED, LOUDLY, naming what was not
#     observed. A hub with no sync pairs yet (any freshly reimaged one, before
#     the Owner creates a pair) may legitimately never log one, and refusing to
#     capture there would break the recovery story for the exact machines that
#     need it most. Sixty seconds alive is already twenty times the old bar.
#
# The readiness markers are what a working client logs once its pairs are live —
# observed on this box: `start watch on "/srv/..."`, `waiting for events`,
# `checking pending uploads`. Matching is scoped to bytes written by THIS run
# (see log_start), so a previous run's steady state can never satisfy it.
start_client() {
    local desktop="$HOMEDIR/.config/autostart/icedrive.desktop" exec_line="" w=0
    local applog="$HOMEDIR/.local/share/Icedrive/logdata.txt" log_start=0
    local ready_timeout=60
    [ -f "$desktop" ] || { warn "no $desktop — cannot restart the client"; return 1; }
    exec_line="$(awk -F= '$1=="Exec"{sub(/^[^=]*=/,""); print; exit}' "$desktop")"
    [ -n "$exec_line" ] || { warn "no Exec= in $desktop — cannot restart"; return 1; }
    session_env_load || { warn "no desktop session for $PROF_USER — cannot restart the client here"; return 1; }
    # Where the client's log ends BEFORE we start it. Every readiness scan below
    # reads only past this offset, so a marker left by an EARLIER run — including
    # the run we just stopped — can never be mistaken for this one starting.
    [ -f "$applog" ] && log_start="$(wc -c <"$applog" 2>/dev/null || echo 0)"

    log "restarting the client into the live session ($(printf '%s\n' ${SESSION_ENV[@]+"${SESSION_ENV[@]}"} | grep '^DISPLAY=' || echo 'DISPLAY=?'))"
    setsid runuser -u "$PROF_USER" -- env ${SESSION_ENV[@]+"${SESSION_ENV[@]}"} nohup sh -c "$exec_line" >/dev/null 2>&1 &
    disown 2>/dev/null || true

    # Phase 1 — a process appears at all.
    while [ "$w" -lt 25 ] && [ -z "$(app_pids)" ]; do sleep 1; w=$((w+1)); done
    if [ -z "$(app_pids)" ]; then
        warn "the client did NOT come back after ${w}s (the gate may have refused — check: journalctl -t icedrive-gate)"
        return 1
    fi

    # Phase 2 — it stays up AND reaches a working state. This is the C36 fix.
    local waited=0
    while [ "$waited" -lt "$ready_timeout" ]; do
        if [ -z "$(app_pids)" ]; then
            warn "the client started and then EXITED after ~$((w + waited))s — it is NOT running."
            warn "  the last lines it wrote (this run only):"
            tail -c "+$((log_start + 1))" "$applog" 2>/dev/null | tail -5 | while IFS= read -r l; do warn "    $l"; done
            warn "  a RemoteHostClosedError here means the restart raced the old instance's teardown (C36)."
            return 1
        fi
        if tail -c "+$((log_start + 1))" "$applog" 2>/dev/null \
             | grep -qE 'waiting for events|checking pending uploads|start watch on'; then
            log "client is back and WORKING (up ${w}s, reached a working state after ${waited}s)"
            return 0
        fi
        sleep 2; waited=$((waited + 2))
    done

    # Alive but quiet. Not a failure — see the banner — but never silent either.
    log "client is back and has stayed up ${ready_timeout}s, but wrote no readiness"
    log "  marker (no sync pair yet?). Treating as OK; it did NOT die, which is"
    log "  the failure this check exists to catch."
    return 0
}

# archive_lists ARCHIVE -> the tar listing on stdout, nonzero if it will not read.
archive_lists() {
    printf '%s' "$KEY" | gpg "${gpg_common[@]}" -d "$1" 3<&0 </dev/null 2>/dev/null | tar -tzf - 2>/dev/null
}

case "$MODE" in
capture)
    have=0
    for r in "${REL_PATHS[@]}"; do [ -e "$HOMEDIR/$r" ] && have=1; done
    [ "$have" = 1 ] || { log "no IceDrive profile under $HOMEDIR — nothing to capture"; exit 3; }
    [ -f "$HOMEDIR/$REQUIRED_MEMBER" ] || die "$HOMEDIR/$REQUIRED_MEMBER does not exist. There is nothing here that would let a reflashed box sign in, so a capture would be a file that looks like a backup and is not one."
    mkdir -p "$OUTDIR" || die "cannot create $OUTDIR"
    chmod 0700 "$OUTDIR" 2>/dev/null || true

    WAS_RUNNING=0; [ -n "$(client_pids)" ] && WAS_RUNNING=1
    stop_client

    tmp="$(mktemp -d)" || die "mktemp failed"
    # An if, not `A && B`: as the last command of an EXIT trap that shape sets
    # the trap's status from a test nobody is reading. This trap covers the
    # FAILURE paths; the success path fires the restart itself, below, so its
    # verdict can reach the exit status.
    trap 'rm -rf "$tmp"; if [ "${WAS_RUNNING:-0}" = 1 ]; then start_client || true; fi' EXIT

    # NO `--ignore-failed-read`. It lets tar exit 0 having skipped a file it
    # could not read — including the credential — so a capture could quietly
    # replace a good archive with a useless one. Optional members are handled by
    # naming only what EXISTS, rather than by telling tar to forgive itself.
    present=()
    for r in "${REL_PATHS[@]}"; do [ -e "$HOMEDIR/$r" ] && present+=("$r"); done
    ( cd "$HOMEDIR" && tar -czf "$tmp/p.tar.gz" -- "${present[@]}" ) || \
        die "tar failed over ${present[*]} in $HOMEDIR"
    printf '%s' "$KEY" | gpg "${gpg_common[@]}" --symmetric --cipher-algo AES256 \
        -o "$tmp/p.gpg" "$tmp/p.tar.gz" 3<&0 </dev/null || die "gpg encryption failed"

    # PROVE IT DECRYPTS *AND HOLDS THE CREDENTIAL* BEFORE REPLACING THE LAST GOOD
    # ONE. Listing alone only proves it is a tar; this archive exists for exactly
    # one file, and its absence is the failure worth catching.
    archive_lists "$tmp/p.gpg" >"$tmp/list" || die "the archive just written does not decrypt — refusing to replace the previous capture"
    grep -qx "$REQUIRED_MEMBER" "$tmp/list" || die "the archive just written holds no $REQUIRED_MEMBER — refusing to replace the previous capture with one that could not sign a box in"

    # ATOMIC REPLACEMENT. `install` straight onto the final name can truncate the
    # last good archive and then fail, leaving neither. Land it beside, then
    # rename — rename is atomic within a filesystem.
    newf="$(mktemp "$OUTDIR/.$ARCHIVE_NAME.XXXXXX")" || die "cannot create a temporary file in $OUTDIR"
    cat "$tmp/p.gpg" >"$newf" || { rm -f "$newf"; die "could not write $newf"; }
    chmod 0600 "$newf" || { rm -f "$newf"; die "could not set 0600 on $newf"; }
    if [ "$(id -u)" = 0 ]; then chown root:root "$newf" 2>/dev/null || warn "could not chown the archive to root"; fi
    mv "$newf" "$OUTDIR/$ARCHIVE_NAME" || { rm -f "$newf"; die "could not install the archive"; }

    # A plaintext sidecar of NON-SECRET facts, so a human (or a check) can see
    # when the capture last succeeded without holding the key. A failure to write
    # it is fatal: a sidecar describing a DIFFERENT archive is worse than none.
    infotmp="$(mktemp "$OUTDIR/.capture.info.XXXXXX")" || die "cannot create a temporary file in $OUTDIR"
    {
        echo "captured_utc=$(date -u +%FT%TZ)"
        echo "user=$PROF_USER"
        echo "paths=${present[*]}"
        echo "bytes=$(stat -c '%s' "$OUTDIR/$ARCHIVE_NAME" 2>/dev/null || echo 0)"
        echo "sha256=$(sha256sum "$OUTDIR/$ARCHIVE_NAME" 2>/dev/null | cut -d' ' -f1)"
        echo "entries=$(grep -c . "$tmp/list")"
        echo "client_was_running=$WAS_RUNNING"
    } >"$infotmp" && chmod 0644 "$infotmp" && mv "$infotmp" "$OUTDIR/capture.info" || {
        rm -f "$infotmp"; die "the archive was written but its capture.info could not be updated — the sidecar would describe the PREVIOUS archive"
    }
    log "captured $(stat -c '%s' "$OUTDIR/$ARCHIVE_NAME") byte(s) -> $OUTDIR/$ARCHIVE_NAME (encrypted, quiesced)"

    # THE RESTART IS PART OF THE JOB. Do it here rather than in the trap, so its
    # verdict can reach the exit status: a capture that leaves the household's
    # offsite sync stopped is not a success, however good the archive is.
    trap - EXIT
    rm -rf "$tmp"
    if [ "$WAS_RUNNING" = 1 ] && ! start_client; then
        warn "THE CAPTURE SUCCEEDED AND THE CLIENT IS DOWN. The archive at $OUTDIR/$ARCHIVE_NAME is"
        warn "  good; the offsite sync is not running. Start it from the desktop session, or reboot."
        exit 1
    fi
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
    if grep -qx "$REQUIRED_MEMBER" "$tmp/list"; then
        log "verify OK: $n entr(ies), and $REQUIRED_MEMBER is among them"
        exit 0
    fi
    die "the archive decrypts and lists $n entr(ies) but holds NO $REQUIRED_MEMBER — restoring it would leave the box still unable to sign in"
    ;;

restore)
    [ -f "$OUTDIR/$ARCHIVE_NAME" ] || { log "no archive at $OUTDIR/$ARCHIVE_NAME — nothing to restore"; exit 3; }
    # NEVER OVER A RUNNING CLIENT. It holds those SQLite files open; replacing
    # them underneath it produces mixed state, and whatever it writes next
    # overwrites what was just restored. Firstboot runs this BEFORE the desktop
    # session starts, which is the case it is for.
    if [ -n "$(client_pids)" ]; then
        die "the IceDrive client is RUNNING. Restoring its profile underneath it would produce mixed state and then be overwritten by whatever it writes next. Stop it first."
    fi
    # THE DESTINATION ROOTS MUST NOT BE LINKS, AND THIS IS CHECKED FIRST. A
    # pre-existing `~/.config/Icedrive -> /etc` redirects the copy however clean
    # the archive is — and it is checked BEFORE the emptiness test because
    # `ls -A` follows the link and reports the TARGET's contents, so the run
    # would decline with "not empty" and never mention the symlink at all. A
    # refusal that names the wrong reason sends the reader somewhere else.
    # (Found by the test written for the review's finding, 2026-08-29.)
    for r in ".config" ".local" ".local/share" "${REL_PATHS[@]}"; do
        if [ -L "$HOMEDIR/$r" ]; then
            die "$HOMEDIR/$r is a SYMLINK. Refusing to restore through it; nothing was written."
        fi
    done
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
    grep -qx "$REQUIRED_MEMBER" "$tmp/list" || \
        die "the archive holds no $REQUIRED_MEMBER — restoring it would leave the box still unable to sign in, so it is refused rather than reported as a restore. Nothing was written."

    # ── UNPACK INTO A SCRATCH DIRECTORY AND INSPECT IT THERE ─────────────────
    #
    # THE FIRST CUT VALIDATED MEMBER NAMES ONLY, and that is a different check. A
    # member named `.config/Icedrive` can BE A SYMLINK to `/etc`; the name passes,
    # tar follows it on extraction, and this runs as root into a home directory.
    # Names are still checked, but the load-bearing part is that nothing reaches
    # $HOMEDIR until the tree has been unpacked somewhere harmless and examined.
    bad="$(awk '
        /^\// { print "absolute: " $0; next }
        /(^|\/)\.\.(\/|$)/ { print "traversal: " $0; next }
        $0 !~ /^\.config\/(Icedrive|autostart)(\/|$)/ && $0 !~ /^\.local\/share\/Icedrive(\/|$)/ { print "outside the profile: " $0 }
    ' "$tmp/list" | head -5)"
    [ -z "$bad" ] || die "the archive holds paths that do not belong in an IceDrive profile — refusing to unpack: $(printf '%s' "$bad" | tr '\n' '; ')"

    stage="$tmp/stage"; mkdir -p "$stage"
    tar -xzf "$tmp/p.tar.gz" -C "$stage" || die "the archive did not unpack into a scratch directory — nothing was written to $HOMEDIR"
    # ANY link or device node at all is refused. A profile has no legitimate use
    # for one, and "is this target inside the tree" has more wrong answers than
    # right ones once `..`, absolute paths and link chains are in play.
    links="$(find "$stage" \( -type l -o -type p -o -type s -o -type b -o -type c \) -printf '%P (%y)\n' 2>/dev/null | head -5)"
    [ -z "$links" ] || die "the archive contains links or device nodes, which an IceDrive profile does not: $(printf '%s' "$links" | tr '\n' '; '). Refusing; nothing was written to $HOMEDIR."
    cp -a "$stage/." "$HOMEDIR/" || die "copying the staged profile into $HOMEDIR failed — it may be partial; delete $HOMEDIR/.config/Icedrive and $HOMEDIR/.local/share/Icedrive before retrying"
    # OWNERSHIP AND MODE, AND THESE ARE FATAL. Unpacked as root, a profile the
    # client cannot read is a restore that did not happen — and it would present
    # as "the sign-in did not survive", which sends the reader somewhere else.
    for r in "${REL_PATHS[@]}"; do
        [ -e "$HOMEDIR/$r" ] || continue
        chown -R "$PROF_USER:$PROF_USER" "$HOMEDIR/$r" || die "could not chown $HOMEDIR/$r to $PROF_USER — the client could not read its own token. The profile is in place but unusable; fix ownership and re-check."
    done
    chmod 0700 "$HOMEDIR/.config/Icedrive" || die "could not set 0700 on $HOMEDIR/.config/Icedrive"
    if [ -d "$HOMEDIR/.local/share/Icedrive" ]; then
        chmod 0700 "$HOMEDIR/.local/share/Icedrive" || die "could not set 0700 on $HOMEDIR/.local/share/Icedrive"
    fi
    chmod 0600 "$HOMEDIR/$REQUIRED_MEMBER" || die "could not set 0600 on $HOMEDIR/$REQUIRED_MEMBER — it holds an API token"
    log "restored $(grep -c . "$tmp/list") entr(ies) into $HOMEDIR as $PROF_USER"
    log "  the client will sign in from the carried token. If IceDrive rejects it as a new device,"
    log "  that is an EXPECTED fallback, not a failure of this step: sign in once by hand and re-capture."
    exit 0
    ;;
esac
