#!/usr/bin/env bash
# common.sh — shared helpers for the AWOW bash backup service (WI-10.10/10.15).
# Sourced by backup.sh and restore.sh. Pure bash + coreutils/tar/zstd/rsync/
# mount.cifs/curl — no PowerShell, no .bat (HOMELAB_TOPOLOGY.md "100% bash").
#
# Behavioral spec = the FileBackup repo (hash tracking, auto-compression-where-
# applicable, recovery/reconstruct). This is a reproduction in bash, not a port.
# Hash: sha256 (coreutils-native — no extra dependency). FileBackup uses xxHash128
# for speed; the algorithm is an internal integrity choice, so sha256 is fine for
# a self-contained backup+restore leg. (Documented delta in README.md.)

set -uo pipefail

# ── logging ──────────────────────────────────────────────────────────────────
LOG_FILE="${LOG_FILE:-}"
log()  { local m="$*"; printf '%s %s\n' "$(date -u +%FT%TZ)" "$m"; [ -n "$LOG_FILE" ] && printf '%s %s\n' "$(date -u +%FT%TZ)" "$m" >>"$LOG_FILE"; }
warn() { log "WARN: $*"; }
die()  { log "ERROR: $*"; exit 1; }

# ── config ───────────────────────────────────────────────────────────────────
# load_config PATH : source a backup.env (KEY=VALUE). Values are literal — do NOT
# put unescaped $ in secrets here; a cifs credentials file is the safer home.
load_config() {
    local f="$1"
    [ -f "$f" ] || die "config not found: $f (copy backup.env.example)"
    # shellcheck disable=SC1090
    set -a; . "$f"; set +a
}

# ── hashing ──────────────────────────────────────────────────────────────────
sha256_of() { sha256sum -- "$1" 2>/dev/null | awk '{print $1}'; }

# ── compression policy — auto-compression-where-applicable (FileBackup spec) ──
# Already-compressed content is exempt: if a large fraction of a set's BYTES are
# in already-compressed extensions, store the tar uncompressed (.tar); otherwise
# zstd it (.tar.zst). Mirrors FileBackup's "already-compressed extensions are
# exempt", lifted to archive granularity. Echoes "ALGO<TAB>RATIO<TAB>REASON".
INCOMPRESSIBLE_EXT="${BACKUP_INCOMPRESSIBLE_EXT:-jar zip 7z gz tgz bz2 xz zst rar png jpg jpeg gif webp mp4 mkv webm mov mp3 ogg flac sav pack}"
compression_decision() {
    local dir="$1" threshold="${BACKUP_INCOMPRESSIBLE_THRESHOLD:-60}"
    local total=0 incomp=0 f sz ext
    while IFS= read -r -d '' f; do
        sz="$(stat -c '%s' -- "$f" 2>/dev/null || echo 0)"
        total=$(( total + sz ))
        ext="${f##*.}"; ext="${ext,,}"
        case " $INCOMPRESSIBLE_EXT " in *" $ext "*) incomp=$(( incomp + sz )) ;; esac
    done < <(find "$dir" -type f -print0 2>/dev/null)
    local ratio=0
    (( total > 0 )) && ratio=$(( incomp * 100 / total ))
    if (( ratio >= threshold )); then
        printf 'none\t%s\tstore .tar: %s%% already-compressed >= %s%% threshold\n' "$ratio" "$ratio" "$threshold"
    else
        printf 'zstd\t%s\tzstd .tar.zst: %s%% already-compressed < %s%% threshold\n' "$ratio" "$ratio" "$threshold"
    fi
}

# ── cifs mount helpers (step 1 pulls / step 5 push) ──────────────────────────
# mount_cifs UNC MOUNTPOINT [rw|ro] : mount a Samba share. Needs root + mount.cifs
# (systemd runs the service as root on the AWOW; the sim runner is privileged).
MOUNTS=()
mount_cifs() {
    local unc="$1" mp="$2" mode="${3:-ro}"
    mkdir -p "$mp"
    local opts="${mode},iocharset=utf8,${BACKUP_CIFS_EXTRA:-vers=3.0}"
    if [ -n "${BACKUP_CIFS_CREDENTIALS:-}" ]; then
        opts="credentials=${BACKUP_CIFS_CREDENTIALS},${opts}"
    else
        opts="username=${BACKUP_CIFS_USER:-guest},password=${BACKUP_CIFS_PASS:-},${opts}"
    fi
    log "mount cifs $unc -> $mp ($mode)"
    mount -t cifs "$unc" "$mp" -o "$opts" || die "cifs mount failed: $unc"
    MOUNTS+=("$mp")
}
umount_all() { local mp; for mp in "${MOUNTS[@]:-}"; do [ -n "$mp" ] && umount "$mp" 2>/dev/null || true; done; MOUNTS=(); }

# ── NagLight /api/feed reporting (step 6) — never-silent-green ────────────────
# feed_naglight OK NOTE : POST {check,ok,note}. ok=false on ANY failure so a
# broken backup is never a silent green. Uses the multi-user trust model (direct
# to the tracker with X-Forwarded-User) when NAGLIGHT_USER is set; single-user
# otherwise. A reporting failure is logged but does not mask the backup's own
# exit status.
feed_naglight() {
    local ok="$1" note="$2"
    [ -n "${NAGLIGHT_FEED_URL:-}" ] || { log "feed: NAGLIGHT_FEED_URL unset — skipping report"; return 0; }
    local check="${NAGLIGHT_FEED_CHECK:-backup}"
    note="${note//\"/\'}"                                   # keep the JSON valid
    local body; body="$(printf '{"check":"%s","ok":%s,"note":"%s"}' "$check" "$ok" "$note")"
    local hdr=(-H "Content-Type: application/json")
    [ -n "${NAGLIGHT_TOKEN:-}" ] && hdr+=(-H "Authorization: Bearer ${NAGLIGHT_TOKEN}")
    [ -n "${NAGLIGHT_USER:-}" ]  && hdr+=(-H "X-Forwarded-User: ${NAGLIGHT_USER}")
    local code; code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${hdr[@]}" -d "$body" "$NAGLIGHT_FEED_URL" 2>/dev/null || echo 000)"
    if [ "$code" = "200" ]; then log "feed: reported ok=$ok (HTTP 200)"; else warn "feed: report ok=$ok got HTTP $code"; fi
}
