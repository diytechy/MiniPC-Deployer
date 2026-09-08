#!/usr/bin/env bash
# common.sh — shared helpers for the AWOW bash backup service (WI-10.10/10.15).
# Sourced by backup.sh and restore.sh. Pure bash + coreutils/tar/zstd/rsync/
# mount.cifs/curl — no PowerShell, no .bat (HOMELAB_TOPOLOGY.md "100% bash").
# The docker CLI is additionally needed IF the sources table uses volume:
# specs (SR-013) — a given on the AWOW, where docker runs the stack anyway.
# Wake-on-LAN (a source box that may sleep) also needs nothing new: the magic
# packet goes out over bash's /dev/udp, with wakeonlan/etherwake used only if
# the kernel refuses that socket a broadcast destination (see wol_send).
#
# Behavioral spec = the FileBackup repo (hash tracking, auto-compression-where-
# applicable, recovery/reconstruct). This is a reproduction in bash, not a port.
# Hash: sha256 (coreutils-native — no extra dependency). FileBackup uses xxHash128
# for speed; the algorithm is an internal integrity choice, so sha256 is fine for
# a self-contained backup+restore leg. (Documented delta in README.md.)

set -uo pipefail

# ── logging ──────────────────────────────────────────────────────────────────
LOG_FILE="${LOG_FILE:-}"
# ALWAYS RETURNS 0, and that is load-bearing rather than tidy. Without the final
# `return 0` the function's status is the `[ -n "$LOG_FILE" ]` test, so `log`
# reported FAILURE on every call made before the run directory exists — which is
# every call in restore.sh (it never sets LOG_FILE at all) and every call in
# backup.sh before line ~145.
#
# THE RETURN STATUS IS ONLY HALF OF IT, and the other half is not fixable here.
# backup.sh runs `set -o errtrace`, which makes the ERR trap fire on a failing
# command INSIDE a function — so if $LOG_FILE ever becomes unwritable the
# `printf >>` trips the trap directly, whatever this function returns. That is
# not hypothetical: with BACKUP_KEEP=0 retention deletes the very run directory
# the log lives in, and the run then dies reporting `backup failed at line 30`,
# naming this logger for a fault three steps upstream in retention. See
# run-backup-cycle-sim.sh S9, which asserts exactly that misdirection.
log()  { local m="$*"; printf '%s %s\n' "$(date -u +%FT%TZ)" "$m"; [ -n "$LOG_FILE" ] && printf '%s %s\n' "$(date -u +%FT%TZ)" "$m" >>"$LOG_FILE"; return 0; }
warn() { log "WARN: $*"; }

# ── die + the failure-report hook (OI-9: never-silent-green on `die` paths) ────
# `die` is shared by backup.sh, restore.sh and backup-standby.sh, so it cannot
# assume a NagLight feed exists. Instead a caller that HAS a feed contract
# registers the NAME of a reporter function in DIE_REPORTER; die invokes it once,
# best-effort, immediately before exiting. Before OI-9 a `die` (bad config, a
# failed cifs mount, a wake timeout) exited 1 with NOTHING posted, so only
# ERR-trap failures fed the tracker — a silent-ish failure the feed contract
# forbids.
#
# Two guards, because a failing report must NEVER mask the original failure:
#   - the reporter's own non-zero status is swallowed into a WARNING;
#   - _DIE_REPORTING blocks re-entry, so a `die` raised *inside* the reporter
#     cannot loop or overwrite the first verdict.
# An unset DIE_REPORTER (restore.sh, backup-standby.sh, or backup.sh before its
# run dir exists) is a clean no-op — exactly the old behaviour.
DIE_REPORTER=""
_DIE_REPORTING=0
die() {
    log "ERROR: $*"
    if [ -n "$DIE_REPORTER" ] && [ "$_DIE_REPORTING" = 0 ]; then
        _DIE_REPORTING=1
        "$DIE_REPORTER" "$*" \
            || warn "die: the failure report itself failed — the ERROR above is the real one"
    fi
    exit 1
}

# ── text helpers ─────────────────────────────────────────────────────────────
# str_trim S : S without leading/trailing whitespace. Every table in backup.env is
# one-entry-per-line and hand-edited, so trimming is the first thing done to every
# line (BACKUP_SOURCES, INGEST_SOURCES, the per-set exclude lines).
str_trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; printf '%s' "${s%"${s##*[![:space:]]}"}"; }

# ── config ───────────────────────────────────────────────────────────────────
# load_config PATH : source a backup.env (KEY=VALUE). Values are literal — do NOT
# put unescaped $ in secrets here; a cifs credentials file is the safer home.
load_config() {
    local f="$1"
    [ -f "$f" ] || die "config not found: $f (copy backup.env.example)"
    # shellcheck disable=SC1090
# load_env_file FILE — export every KEY=VALUE in FILE **literally**.
#
# NEVER `source` a compose .env. Its values are literal text, and every
# basic_auth hash in this project is bcrypt — `$2a$14$…`. Sourcing makes bash
# expand them: under `set -u` it aborts on the unbound `$2` (which is exactly
# how first boot died), and WITHOUT `set -u` it is worse — `$2`/`$1` expand to
# nothing, the hash is silently corrupted, and auth then fails with nothing
# anywhere explaining why.
load_env_file() {
    local __f="$1" __line __k __v
    [ -f "$__f" ] || return 0
    while IFS= read -r __line || [ -n "$__line" ]; do
        case "$__line" in ''|'#'*) continue ;; esac
        case "$__line" in *=*) ;; *) continue ;; esac
        __k=${__line%%=*}
        __v=${__line#*=}
        __k=${__k#"${__k%%[![:space:]]*}"}
        __k=${__k%"${__k##*[![:space:]]}"}
        case "$__k" in ''|*[!A-Za-z0-9_]*) continue ;; esac
        case "$__v" in
            # ── QUOTED, POSSIBLY ACROSS SEVERAL LINES ──────────────────────
            # THE OPENING QUOTE IS THE SIGNAL; THE CLOSING ONE MAY BE PAGES
            # AWAY. This used to be a single `\"*\"` pattern, which matches
            # only a value that opens AND closes on one line — and the failure
            # on anything else was total and silent:
            #
            #   BACKUP_SOURCES="media-music=path:/srv/library/…/Music
            #   media-movies=path:/srv/library/…/Movies
            #   …twelve more…
            #   finance=volume:finance_snapshots@finance-auditor"
            #
            # Line 1 kept its leading `"` (no closing quote to match), so the
            # first set was named `"media-music` — which also stopped its
            # `media-music.exclude=` line from ever matching. Every LATER line
            # was then read as its own KEY=VALUE, rejected as a malformed key
            # (`media-movies` has a hyphen), and CONTINUED PAST. So the backup
            # silently saw ONE source instead of fourteen, and the thirteen it
            # dropped included every Private tree.
            #
            # Nothing reported it: `continue` on a bad key is correct for
            # comments and prose, and indistinguishable from this.
            # Measured 2026-08-09 on the lab hub.
            \"*)
                __v=${__v#\"}
                # Read on until a line ends with the closing quote. The outer
                # loop's `< "$__f"` redirect covers this read too, so it
                # consumes the continuation lines and they are never re-parsed
                # as keys of their own.
                until case "$__v" in *\") true ;; *) false ;; esac; do
                    IFS= read -r __more || {
                        # EOF with the quote still open: the file is malformed.
                        # Say so — a truncated BACKUP_SOURCES is exactly the
                        # silent partial this whole block exists to stop.
                        printf '%s\n' "load_env_file: WARNING: unterminated quote in $__f for key '$__k' — value truncated at EOF" >&2
                        break
                    }
                    __v="$__v
$__more"
                done
                __v=${__v%\"}
                ;;
            \'*\') __v=${__v#\'}; __v=${__v%\'} ;;
            # UNQUOTED: strip a trailing ` # comment`, exactly as shell
            # sourcing and docker compose's own .env parser both do. Without
            # this, LAN_IP=0.0.0.0 followed by an explanatory comment reached
            # Technitium's API as part of the address and curl rejected the
            # URL. A `#` with no space before it is kept — it may be part of a
            # password.
            *) __v=${__v%%[[:space:]]#*}
               __v=${__v%"${__v##*[![:space:]]}"} ;;
        esac
        # Compose stores a literal '$' as '$$' (see compose_escape) because it
        # interpolates .env values. Collapse it back, so a script reading this
        # file sees exactly what the containers receive.
        __v=${__v//\$\$/\$}
        printf -v "$__k" '%s' "$__v" 2>/dev/null && export "$__k"
    done < "$__f"
}
    load_env_file "$f"
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

# ── source-spec parsing (SR-013) ──────────────────────────────────────────────
# BACKUP_SOURCES line grammar: `name=SPEC`, one per line, where SPEC is
#   //host/share            cifs pull over the LAN (the original form)
#   volume:VOL              docker named volume on THIS box, copied live
#   volume:VOL@CONTAINER    stop CONTAINER first, copy, restart right after
#   path:/abs/dir           a local directory on this box
# Lines starting with # are skipped by backup.sh, so the table can carry
# commented-out entries. One table drives one execution sequence — every set,
# whatever its source kind, flows through the same archive/hash/manifest/
# retention/offsite/report pipeline.

# source_kind SPEC : echoes cifs|volume|path|bad. Pure — no I/O, unit-greppable.
source_kind() {
    case "$1" in
        //*)      echo cifs ;;
        volume:*) echo volume ;;
        path:/*)  echo path ;;
        *)        echo bad ;;
    esac
}

# newest_run_with_set BASE SET : the newest sibling run directory whose MANIFEST
# actually lists SET, or nothing. Run names are run_%Y%m%d_%H%M%S so a plain
# sort is chronological (the same assumption retention makes). Read-only.
#
# `run_*` EXCLUDES PLAN DIRECTORIES BY CONSTRUCTION since 2026-08-29 (Q1), which
# is a real strengthening and not just tidiness: a plan run writes a header-only
# MANIFEST.tsv, so before the rename this function relied on the awk row-match
# failing to reject one. It still does - belt and braces - but the glob now
# cannot even offer a plan directory as a candidate, and firstboot ACME restore
# is the caller that would have paid for a mistake here.
#
# LIVES HERE, NOT IN restore.sh, since 2026-08-28: firstboot's ACME restore needs
# the identical question answered, and two copies of "which run actually has this
# set" is exactly the kind of drift that makes one of them quietly wrong.
#
# IT ALSO ANSWERS FOR THE FLAT LAYOUT (BACKUP_LAYOUT=flat, 2026-09-01), and that
# is why every reimage-restore caller kept working when the target moved. In the
# flat layout there are no run_ directories to choose between: BASE itself IS the
# run, holding one current copy whose history is the library backup's
# Snapshot_<date> series. So BASE is the answer when it carries a MANIFEST.tsv
# listing the set - checked SECOND, so a dated target (which has no manifest of
# its own and never reaches that branch) behaves byte-identically to before.
newest_run_with_set() {
    local base="$1" s="$2" d newest
    newest="$(
        while IFS= read -r d; do
            [ -f "$d/MANIFEST.tsv" ] || continue
            awk -F'\t' -v s="$s" 'NR>1 && $1==s {found=1} END {exit !found}' "$d/MANIFEST.tsv" || continue
            printf '%s\n' "$d"
        done < <(find "$base" -mindepth 1 -maxdepth 1 -type d -name 'run_*' 2>/dev/null | sort) | tail -1
    )"
    if [ -n "$newest" ]; then printf '%s\n' "$newest"; return 0; fi
    if [ -f "$base/MANIFEST.tsv" ] &&
       awk -F'\t' -v s="$s" 'NR>1 && $1==s {found=1} END {exit !found}' "$base/MANIFEST.tsv"; then
        printf '%s\n' "$base"
    fi
    return 0
}


# volume_mountpoint VOL : echoes the volume's host mountpoint (empty + nonzero
# on failure — the caller decides how loud to be). Reading the mountpoint
# directly (the service runs as root) avoids depending on any helper image.
volume_mountpoint() {
    # THE NAME IN BACKUP_SOURCES IS THE COMPOSE NAME; DOCKER'S IS PREFIXED.
    # `volumes: actual_data:` in docker-compose.yml becomes the real volume
    # `stack_actual_data`, because compose namespaces by PROJECT and the project
    # defaults to the directory name (`stack/`). BACKUP_SOURCES is written
    # against the compose file — that is the name a human reads and the one the
    # storage map uses — so a bare inspect misses every volume set.
    #
    # Found 2026-08-09, and only visible once the multi-line parser fix let the
    # volume entries be READ at all: all five (actual, tracker, technitium,
    # caddy, finance) failed identically.
    #
    # Literal first: an operator who writes the real prefixed name, or renames
    # the project, must keep working.
    #
    # CAPTURED, NOT ECHOED DIRECTLY. `docker volume inspect -f` on a MISSING
    # volume still writes an empty line to stdout before failing, and echoing
    # that through made the mountpoint "\n/var/lib/docker/…" — which then failed
    # `[ -d "$vmp" ]` and reported "volume not found" for volumes that had just
    # been found. Two bugs wearing one error message.
    # ── FOUR OUTCOMES, NOT TWO (2026-08-09) ──────────────────────────────────
    #   0  resolved — the mountpoint is on stdout
    #   1  the volume is genuinely ABSENT (the daemon answered; there is no such
    #      volume by literal name and no compose label matches)
    #   2  AMBIGUOUS — the compose label matched more than one volume
    #   3  could not ASK — the docker daemon did not answer
    #
    # This used to return 1 for all three failures, and that is why the caller
    # could not act on any of them. The Owner's ruling (a missing source must not
    # block the other sets) needs "absent" to be separable from the other two,
    # because they are not the same event and must not have the same outcome:
    #
    #   * a dead daemon returning "absent" would SKIP all five volume sets in one
    #     night, under five log lines each saying the volume does not exist;
    #   * an ambiguous match returning "absent" would skip the very case the
    #     label lookup exists to refuse — `_actual_data` matching both
    #     `stack_actual_data` and `stack_finance_actual_data`, i.e. the choice
    #     between a budget volume and a finance auditor's.
    #
    # Same shape as the four faults behind one "path source missing" line: one
    # status for several causes is one message for several causes.
    local mp name count
    mp="$(docker volume inspect -f '{{ .Mountpoint }}' "$1" 2>/dev/null)" || mp=""
    if [ -n "$mp" ]; then printf '%s\n' "$mp"; return 0; fi

    # Then ask COMPOSE, which records the mapping itself: every volume it
    # creates carries `com.docker.compose.volume=<name as written in the compose
    # file>`, which is exactly the name BACKUP_SOURCES uses. No prefix to guess
    # and no string surgery.
    #
    # NOT A SUFFIX MATCH, which was the first attempt and was wrong: `_actual_data`
    # matches BOTH `stack_actual_data` and `stack_finance_actual_data`, so the
    # backup would have had to choose between a budget volume and a finance
    # auditor's — silently, and wrongly half the time. The label is exact.
    #
    # The `if !` form is required, not stylistic: backup.sh runs with an ERR trap
    # and `set -o errtrace`, so a bare failing assignment here would abort the
    # whole run instead of letting the caller decide.
    if ! name="$(docker volume ls -q --filter "label=com.docker.compose.volume=$1" 2>/dev/null)"; then
        return 3                      # the daemon did not answer — we cannot tell
    fi
    count="$(printf '%s\n' "$name" | grep -c . || true)"
    [ "${count:-0}" -le 1 ] || return 2                      # more than one match
    [ "${count:-0}" -eq 1 ] || return 1                      # daemon answered: absent
    mp="$(docker volume inspect -f '{{ .Mountpoint }}' "$name" 2>/dev/null)" || return 1
    [ -n "$mp" ] || return 1
    printf '%s\n' "$mp"
}

# ── INGEST-spec parsing (step 1b, ratified 2026-07-29) ────────────────────────
# INGEST_SOURCES line grammar — one per line, same hand-edited style as
# BACKUP_SOURCES, but with a DESTINATION because ingest writes into the library:
#
#   name=//host/share -> /abs/library/dest
#
# `name` names the ingest in the log (it is NOT a backup set name — the library
# folder is covered by an ordinary `path:` BACKUP_SOURCES entry afterwards).
# Whitespace around `=` and `->` is free.
#
# ingest_parse LINE : echoes "name<TAB>src<TAB>dest"; returns 1 (silently) on
# anything malformed. Callers MUST test the status (`x="$(ingest_parse "$l")" ||
# die …`): a tested command substitution does not trip backup.sh's ERR trap, so
# the caller owns the error message.
ingest_parse() {
    local line="$1" lhs rhs name src dest
    case "$line" in *'->'*) ;; *) return 1 ;; esac
    lhs="${line%%->*}"; rhs="${line#*->}"
    case "$lhs" in *=*) ;; *) return 1 ;; esac
    name="$(str_trim "${lhs%%=*}")"
    src="$(str_trim "${lhs#*=}")"
    dest="$(str_trim "$rhs")"
    # The source is a UNC share (that is what "network source" means here) and the
    # destination is an ABSOLUTE library path — a relative dest would resolve
    # against whatever cwd systemd handed us, which is nobody's intent.
    case "$src"  in //?*/?*) ;; *) return 1 ;; esac
    case "$dest" in /?*)     ;; *) return 1 ;; esac
    printf '%s\t%s\t%s' "$name" "$src" "$dest"
}

# dir_has_files DIR : 0 iff DIR contains at least one regular file (at any depth).
# Used by the ingest MIRROR SAFETY guard — `rsync --delete` from a share that
# mounted but came up EMPTY would otherwise erase the library copy.
dir_has_files() { [ -n "$(find "$1" -type f -print -quit 2>/dev/null)" ]; }

# ── per-set exclude lines (step 2 exclusions) ─────────────────────────────────
# Exclusions live in the SAME BACKUP_SOURCES table as the sources they filter —
# one table, one place to look (the SR-013 doctrine) — as extra lines:
#
#   name.exclude=PATTERN [PATTERN …]
#
# exclude_line_name LINE : echoes the SET NAME for such a line, or NOTHING when
# LINE is an ordinary `name=SPEC` source. Pure, and never nonzero — the caller
# branches on the emptiness, so this composes inside a `while read` loop without
# any ERR-trap interaction. (A set name may therefore not END in `.exclude`.)
# Anchored on the FIRST `=` so a source spec that merely contains the text
# (`name=path:/srv/x.exclude=y`) can never be mistaken for an exclude line.
exclude_line_name() {
    local lhs
    case "$1" in *=*) lhs="${1%%=*}" ;; *) return 0 ;; esac
    case "$lhs" in *.exclude) printf '%s' "${lhs%.exclude}" ;; esac
}

# ── container quiesce (SR-013: volume:VOL@CONTAINER) ──────────────────────────
# Stop a container so its volume is copied at rest, restart it IMMEDIATELY after
# the copy (not at run end — downtime is the copy, seconds not minutes).
# QUIESCED tracks every container this run has stopped and not yet restarted;
# quiesce_restore (wired into backup.sh's EXIT trap) restarts any survivor, so
# no failure path can leave a service down. UNLIKE drive power, restart problems
# are LOUD: quiesce_start propagates docker's status and the caller fails the
# run (a stopped service is worse than a missed backup); the EXIT-trap pass can
# only WARN (the exit status is already decided) and names the manual fix.
QUIESCED=()
quiesce_stop() {
    local c="$1"
    log "quiesce: docker stop $c"
    docker stop "$c" >/dev/null || return 1
    QUIESCED+=("$c")
}
quiesce_start() {
    local c="$1" i left=()
    log "quiesce: docker start $c"
    docker start "$c" >/dev/null || return 1
    for i in "${QUIESCED[@]:-}"; do [ -n "$i" ] && [ "$i" != "$c" ] && left+=("$i"); done
    QUIESCED=("${left[@]:-}")
}
quiesce_restore() {
    local c
    for c in "${QUIESCED[@]:-}"; do
        [ -n "$c" ] || continue
        warn "quiesce: EXIT-trap restart of still-stopped container: $c"
        docker start "$c" >/dev/null 2>&1 \
            || warn "quiesce: RESTART FAILED for $c — start it manually: docker start $c"
    done
    QUIESCED=()
}

# ── Wake-on-LAN pre-step (step 1: a source box that is ALLOWED TO SLEEP) ──────
# The Windows game box exposes its one share but may be ASLEEP when the nightly
# timer fires, so the backup wakes it over the LAN and waits for its SMB port
# before mounting anything.
#
# CONTRACT (never-silent-green): the magic packet is best-effort — the WAIT is
# the truth. The run proceeds only once the host actually answers tcp/445; on
# timeout the caller MUST die, because a source that failed to WAKE must never
# be mistaken for a source with nothing new to copy.
#
# Config (backup.env): BACKUP_WAKE_MAC (empty = feature off), BACKUP_WAKE_HOST,
# BACKUP_WAKE_TIMEOUT (seconds), BACKUP_WAKE_BROADCAST (optional override).
WOL_UDP_PORT=9              # the conventional discard/WoL port (7 is also seen)
WOL_RESEND_SECONDS=15       # a sleeping NIC can miss one packet; re-send while waiting
WAKE_PROBE_PORT=445         # SMB — the port we actually need, so it is what we probe
WAKE_PROBE_TIMEOUT=3        # per-attempt TCP connect timeout, seconds

# wol_mac_hex MAC : normalise `aa:bb:cc:dd:ee:ff` / `AA-BB-…` / `aabbccddeeff`
# to 12 lowercase hex chars on stdout; returns 1 (quietly) on anything else.
# Pure — no I/O — so the parsing can be reasoned about (and tested) on its own.
wol_mac_hex() {
    local hex
    hex="$(printf '%s' "$1" | tr -d ':.-' | tr '[:upper:]' '[:lower:]')"
    { [ "${#hex}" -eq 12 ] && [ -z "${hex//[0-9a-f]/}" ]; } || return 1
    printf '%s' "$hex"
}

# wol_magic_packet_escapes HEX12 : the magic packet as `printf '%b'` escapes —
# 6 sync bytes of 0xFF followed by the target MAC repeated 16 times (the AMD
# Magic Packet layout). Pure; split out so the byte layout is readable without
# reading the socket code. `%b` (not a computed format string) keeps the NUL
# bytes of MACs containing 00 intact and keeps shellcheck quiet.
wol_magic_packet_escapes() {
    local hex="$1" mac_esc="" rep="" i
    for (( i = 0; i < 12; i += 2 )); do mac_esc="$mac_esc\\x${hex:i:2}"; done
    for (( i = 0; i < 16; i++ )); do rep="$rep$mac_esc"; done
    printf '%s' "\\xff\\xff\\xff\\xff\\xff\\xff$rep"
}

# wol_send MAC : broadcast one magic packet. Best-effort by design — returns 0
# if some method reported success, 1 if none did, and the CALLER must not treat
# either as the verdict (the tcp probe decides).
#
# Method order — no new dependency first, packaged tools as the documented
# fallback:
#   1. bash's own /dev/udp — zero dependencies. GOTCHA: bash cannot set
#      SO_BROADCAST on that socket, so some kernels refuse a broadcast
#      destination with EACCES. That is precisely why 2/3 exist.
#   2. `wakeonlan` (apt: wakeonlan) — the portable choice; sets SO_BROADCAST.
#   3. `etherwake` (apt: etherwake) — raw layer-2, needs root and uses its own
#      default interface unless BACKUP_WAKE_IFACE names one.
wol_send() {
    local mac="$1" hex pkt bcast="${BACKUP_WAKE_BROADCAST:-255.255.255.255}"
    hex="$(wol_mac_hex "$mac")" || { warn "wake: BACKUP_WAKE_MAC is not a MAC address: '$mac'"; return 1; }
    pkt="$(wol_magic_packet_escapes "$hex")"
    if ( exec 3<>"/dev/udp/$bcast/$WOL_UDP_PORT" && printf '%b' "$pkt" >&3 ) 2>/dev/null; then
        log "wake: magic packet -> $bcast:$WOL_UDP_PORT (bash /dev/udp, no dependency)"
        return 0
    fi
    if command -v wakeonlan >/dev/null 2>&1 && wakeonlan -i "$bcast" "$mac" >/dev/null 2>&1; then
        log "wake: magic packet -> $bcast (wakeonlan)"
        return 0
    fi
    if command -v etherwake >/dev/null 2>&1 \
       && etherwake ${BACKUP_WAKE_IFACE:+-i "$BACKUP_WAKE_IFACE"} "$mac" >/dev/null 2>&1; then
        log "wake: magic packet -> layer 2 (etherwake${BACKUP_WAKE_IFACE:+ on $BACKUP_WAKE_IFACE})"
        return 0
    fi
    return 1
}

# tcp_port_open HOST PORT [TIMEOUT_S] : true iff a TCP connect succeeds within
# TIMEOUT_S. Pure bash /dev/tcp + coreutils `timeout` — no nc/nmap dependency.
# HOST/PORT are passed as ARGUMENTS to the inner shell, never interpolated into
# its script text.
tcp_port_open() {
    local host="$1" port="$2" t="${3:-$WAKE_PROBE_TIMEOUT}"
    timeout "$t" bash -c 'exec 3<>/dev/tcp/"$1"/"$2"' _ "$host" "$port" 2>/dev/null
}

# wake_and_wait MAC HOST TIMEOUT_S : wake HOST and BLOCK until it answers
# tcp/445, re-sending the packet every WOL_RESEND_SECONDS. Returns 0 as soon as
# the port answers (immediately, if the box was never asleep), 1 on timeout —
# and 1 MUST be fatal for the caller (see the contract above).
wake_and_wait() {
    local mac="$1" host="$2" timeout_s="$3" start next_send
    if tcp_port_open "$host" "$WAKE_PROBE_PORT"; then
        log "wake: $host already awake (tcp/$WAKE_PROBE_PORT answering) — no packet needed"
        return 0
    fi
    log "wake: $host is not answering tcp/$WAKE_PROBE_PORT — sending Wake-on-LAN, waiting up to ${timeout_s}s"
    wol_send "$mac" \
        || warn "wake: no working magic-packet method (bash /dev/udp refused; wakeonlan/etherwake not installed) — still waiting in case the box is already coming up"
    start=$SECONDS
    next_send=$(( SECONDS + WOL_RESEND_SECONDS ))
    while [ $(( SECONDS - start )) -lt "$timeout_s" ]; do
        if tcp_port_open "$host" "$WAKE_PROBE_PORT"; then
            log "wake: $host answered tcp/$WAKE_PROBE_PORT after $(( SECONDS - start ))s"
            return 0
        fi
        if [ "$SECONDS" -ge "$next_send" ]; then
            wol_send "$mac" || true
            next_send=$(( SECONDS + WOL_RESEND_SECONDS ))
        fi
        sleep 2
    done
    return 1
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

# ── drive power management (WI-10.10 DRIVE POWER DESIGN) ──────────────────────
# The backup drive(s) are the box's biggest electrical lever (5–8 W each while
# spinning ≈ the whole CPU). Policy = DYNAMIC standby: backup-standby.service
# sets a conservative default spin-down timeout at boot, and the backup RUN
# disables standby on its target drive(s) at start (drive_standby_set 0) then
# RESTORES the configured timeout on exit via a trap (fires on failure/interrupt
# too). These helpers are shared by backup.sh (the run) and backup-standby.sh
# (the boot oneshot).
#
# hdparm -S <value> spin-down encoding — this is the notoriously confusing part,
# so it lives here, documented once:
#     0        standby DISABLED  (drive never auto-spins-down; used mid-run)
#     1..240   value × 5 seconds        → 240 = 1200 s = 20 min
#     241..251 (value − 240) × 30 min   → 241 = 30 min, 242 = 60 min … 251 = 5.5 h
#     252..255 vendor/special           (avoid)
# Recommended conservative default: 241 (= 30 min); 240 (= 20 min) is also fine.
#
# HARD RULE: power management must NEVER fail a backup. Every path below logs a
# WARNING and returns 0 on any problem — hdparm not installed, device path
# absent, or the enclosure rejecting the command. Many USB-SATA bridge chips
# quietly ignore hdparm APM/standby (they swallow or fake the ioctl); when that
# happens this is a harmless no-op. Per-drive behaviour is verified at hardware
# burn-in with `hdparm -C /dev/disk/by-id/...` (shows active/idle vs standby).

# standby_desc VALUE : human-readable text for an hdparm -S value (for logs).
# Guarded so a non-numeric value never errors under errtrace.
standby_desc() {
    local v="$1"
    case "$v" in
        ''|*[!0-9]*) printf 'value %s' "$v"; return 0 ;;
    esac
    if   [ "$v" -eq 0 ];   then printf 'standby disabled'
    elif [ "$v" -le 240 ]; then printf '%ss' "$(( v * 5 ))"
    elif [ "$v" -le 251 ]; then printf '%smin' "$(( (v - 240) * 30 ))"
    else printf 'value %s' "$v"; fi
}

# have_hdparm : true iff hdparm is on PATH; warns ONCE if missing. Never fails.
_HDPARM_WARNED=0
have_hdparm() {
    command -v hdparm >/dev/null 2>&1 && return 0
    if [ "$_HDPARM_WARNED" = 0 ]; then
        warn "drive-power: hdparm not installed — spin-down management skipped (apt-get install hdparm)"
        _HDPARM_WARNED=1
    fi
    return 1
}

# drive_standby_set VALUE DEVICE... : apply `hdparm -S VALUE` to each DEVICE.
#   VALUE 0    → disable standby (run start, no mid-backup spin-down)
#   VALUE 1..251 → the configured spin-down timeout (boot default / run exit)
# NEVER fails: missing hdparm, absent device, or a rejecting enclosure → WARNING
# and continue. Always returns 0 so it composes with backup.sh's ERR-trap /
# never-silent-green machinery without ever tripping it.
drive_standby_set() {
    local val="$1"; shift
    have_hdparm || return 0
    local dev
    for dev in "$@"; do
        [ -n "$dev" ] || continue
        if [ ! -e "$dev" ]; then
            warn "drive-power: device not present, skipping: $dev"
            continue
        fi
        if hdparm -S "$val" "$dev" >/dev/null 2>&1; then
            log "drive-power: hdparm -S $val $dev ($(standby_desc "$val"))"
        else
            warn "drive-power: hdparm -S $val REJECTED on $dev — enclosure may ignore standby (verify at burn-in); continuing"
        fi
    done
    return 0
}

# ── mount presence (zero disk I/O — safe on a spun-down drive) ────────────────
# mount_options_for PATH : echo PATH's mount options, or nothing if PATH is not
# a mountpoint. Nonzero when it is not mounted.
#
# WHY IT READS /proc/self/mountinfo AND NOTHING ELSE: the backup drive is
# deliberately parked (hdparm -S, WI-10.10), and the whole point of that policy
# is that a 3.5" platter drive is not woken to answer questions. mountinfo is a
# kernel-generated pseudo-file — the answer comes from the VFS mount table, with
# no request ever reaching the device. `df`, `stat`, `ls` and a touch-test all
# CAN reach the platters; none of them are used here. That makes this check
# cheap enough to run every 10 minutes against a sleeping drive, forever.
#
# Same parsing as samba/library-guard.sh (mountinfo field 5 = mountpoint,
# field 6 = options; last match wins because a path can be mounted over).
mount_options_for() {
    local path="$1" opts
    [ -r /proc/self/mountinfo ] || return 2
    opts="$(awk -v p="$path" '$5 == p { o = $6 } END { print o }' /proc/self/mountinfo)"
    [ -n "$opts" ] || return 1
    printf '%s' "$opts"
}

# enclosing_mountpoint PATH : echo the deepest mountpoint AT OR ABOVE PATH.
#
# WHY THIS EXISTS (2026-09-01): the backup target stopped being a whole drive.
# The storage-map 4c service-state archives now land in a FOLDER on the library
# drive (/srv/library/Configs) instead of at the root of the backup drive, so
# `mount_options_for "$BACKUP_TARGET"` - which answers only for a path that IS
# itself a mountpoint - began answering "not mounted" for a perfectly healthy
# box. Walking up gives the SAME answer as before for a target that is its own
# mountpoint (/mnt/backup-drive) and the right one for a folder on a drive.
#
# REACHING `/` IS THE FAILURE, and callers treat it as one: it means no data
# drive carries the path, so the run would be writing to the SYSTEM disk - the
# silent-green shape the target preflight has always existed to refuse.
#
# Zero disk I/O: mount_options_for reads /proc/self/mountinfo and nothing else,
# so this is still safe against a spun-down drive and never wakes one.
enclosing_mountpoint() {
    local p="${1%/}"
    [ -n "$p" ] || p=/
    case "$p" in /*) ;; *) return 2 ;; esac        # a relative path has no answer
    while :; do
        if mount_options_for "$p" >/dev/null 2>&1; then printf '%s' "$p"; return 0; fi
        case "$p" in /) return 1 ;; esac
        p="${p%/*}"; [ -n "$p" ] || p=/
    done
}

# backup_target_ready TARGET : true when TARGET exists AND sits on a real data
# mount rather than on the root filesystem.
#
# The one-line form of the preflight, for the callers that only want to know
# whether there is anything to READ - firstboot's restore steps and the reimage
# drill. All of them used `mountpoint -q "$TARGET"`, which is exactly the test a
# folder-on-a-drive target breaks: it would report "no backup drive" on a box
# whose drive is mounted and full of archives, and every one of those callers
# fails OPEN (skips the restore), so the loss would have been silent.
backup_target_ready() {
    local t="$1" mp
    mp="$(enclosing_mountpoint "$t")" || return 1
    [ "$mp" != "/" ] || return 1
    [ -d "$t" ]
}

# fstab_mount_for PATH FSTAB : the deepest ancestor of PATH (or PATH itself)
# that FSTAB names as a mountpoint; nothing + nonzero when none does.
#
# The BEFORE-IT-IS-MOUNTED counterpart of enclosing_mountpoint, for firstboot.
# On a fresh install nothing has mounted the data drives yet (C32), so the
# question is not "what carries this path" but "which fstab line is the one to
# mount for it". Field 2 is compared as a FIELD and never as a regex - the
# 2026-08-28 review's finding, which applies to every ancestor equally.
fstab_mount_for() {
    local p="${1%/}" f="$2"
    [ -n "$p" ] || p=/
    [ -f "$f" ] || return 1
    while :; do
        if awk -v t="$p" '$0 !~ /^[[:space:]]*#/ && $2 == t { found = 1 }
                          END { exit !found }' "$f" 2>/dev/null; then
            printf '%s' "$p"; return 0
        fi
        case "$p" in /) return 1 ;; esac
        p="${p%/*}"; [ -n "$p" ] || p=/
    done
}

# ── NagLight /api/feed reporting (step 6) — never-silent-green ────────────────
# feed_naglight OK NOTE : POST {check,ok,note}. ok=false on ANY failure so a
# broken backup is never a silent green. Uses the multi-user trust model (direct
# to the tracker with X-Forwarded-User) when NAGLIGHT_USER is set; single-user
# otherwise. A reporting failure is logged but does not mask the backup's own
# exit status.
#
# Transport: the tracker is BRIDGE-ONLY by design (D2/WI-10.5 — no host
# publish, or the trusted headers would be forgeable from the LAN). A host-side
# curl therefore cannot reach it, and the public tracker.<domain> route would
# bounce through oauth2-proxy and overwrite X-Forwarded-User (defect found
# 2026-07-30, Personal A9). When NAGLIGHT_FEED_CONTAINER is set the POST runs
# INSIDE that container via docker exec + its wget (present — the healthcheck
# uses it), keeping the port closed; unset = direct curl (sim / single-user
# setups where the URL is host-reachable).
#
# IT IS NOT BUSYBOX WGET, which this comment claimed until 2026-08-29. The image
# ships **GNU wget 1.24.5 on musl**. The distinction cost real debugging time in
# library-guard.sh, which copies this transport: with `-q` and no `-S`, a 4xx
# exits 8 and prints NOTHING, so a report that was being rejected with a
# perfectly clear HTTP 400 looked like a dead network. If this function ever
# needs the status code rather than a boolean, it needs `-S` (headers) and
# `--content-on-error` (the body) — see library-guard.sh's feed block.
#
# FILE_SHARE_BACKUP_STATE_LAST_CODE carries the outcome of the last unified
# state update — the HTTP code, `000` when nothing answered, or `skipped` when
# no endpoint is configured.  The endpoint deliberately accepts exactly one
# state dimension per POST: share health from the monitor OR a verified-success
# timestamp from FileBackup.  It is not `/api/feed`: that endpoint's day-log
# semantics would make a recovered share look like a fresh backup.
FILE_SHARE_BACKUP_STATE_LAST_CODE=""
post_file_share_backup_state() {
    local field="$1" value="$2"
    FILE_SHARE_BACKUP_STATE_LAST_CODE="skipped"
    local url="${FILE_SHARE_BACKUP_STATE_URL:-}"
    [ -n "$url" ] || { log "file-share/backup state: FILE_SHARE_BACKUP_STATE_URL unset — skipping update"; return 0; }
    local id="${FILE_SHARE_BACKUP_FEED_ID:-file-share-backup-health}"
    local body
    case "$field" in
        shareHealth)
            case "$value" in red|clear) ;; *) warn "file-share/backup state: invalid shareHealth '$value'"; return 0;; esac
            body="$(printf '{"id":"%s","shareHealth":"%s"}' "$id" "$value")"
            ;;
        lastSuccess)
            # The server also validates this.  Keep the producer strict so a
            # local clock/config regression cannot manufacture a fresh backup.
            case "$value" in *T*Z) ;; *) warn "file-share/backup state: lastSuccess must be RFC3339 UTC, got '$value'"; return 0;; esac
            body="$(printf '{"id":"%s","lastSuccess":"%s"}' "$id" "$value")"
            ;;
        runState)
            # A PHASE, NOT A VERDICT.  It says where the run is, never that one
            # succeeded: the server keeps lastSuccess untouched, so when the
            # attempt ends the age underneath is exactly what it was.  `idle` is
            # therefore safe on the failure path — it withdraws a claim about
            # being in progress, and claims nothing else.
            case "$value" in
                starting|backing-up|verifying|idle) ;;
                *) warn "file-share/backup state: invalid runState '$value'"; return 0;;
            esac
            body="$(printf '{"id":"%s","runState":"%s"}' "$id" "$value")"
            ;;
        *) warn "file-share/backup state: unknown field '$field'"; return 0;;
    esac
    local code
    if [ -n "${NAGLIGHT_FEED_CONTAINER:-}" ]; then
        local whdr=(--header "Content-Type: application/json")
        [ -n "${NAGLIGHT_TOKEN:-}" ] && whdr+=(--header "Authorization: Bearer ${NAGLIGHT_TOKEN}")
        [ -n "${NAGLIGHT_USER:-}" ]  && whdr+=(--header "X-Forwarded-User: ${NAGLIGHT_USER}")
        # BOUNDED. Without a deadline a stalled tracker holds this call — and with
        # it the backup lock its caller may be holding — open indefinitely, which
        # under TimeoutStartSec=infinity means forever. This is a TRANSPORT
        # deadline and expires no state: it never shortens a legitimately long
        # run, it only stops one HTTP request from hanging the box.
        if docker exec "$NAGLIGHT_FEED_CONTAINER" wget -q -O /dev/null -T 10 --tries=1 "${whdr[@]}" \
                --post-data "$body" "$url" 2>/dev/null; then
            code=200                       # wget: exit 0 == HTTP 2xx. NOTE: a 4xx
                                           # lands in the else below as a bare
                                           # 000, which cannot be told from "no
                                           # answer". Enough for ok/fail here;
                                           # not enough to diagnose. See above.
        else
            code=000
        fi
    else
        local hdr=(-H "Content-Type: application/json")
        [ -n "${NAGLIGHT_TOKEN:-}" ] && hdr+=(-H "Authorization: Bearer ${NAGLIGHT_TOKEN}")
        [ -n "${NAGLIGHT_USER:-}" ]  && hdr+=(-H "X-Forwarded-User: ${NAGLIGHT_USER}")
        # `|| echo 000` was WRONG and printed `000000` (seen 2026-08-09): on a
        # connection failure curl writes its own `000` from -w AND exits 7, so
        # the fallback APPENDED to it. The logged "HTTP 000000" then looked like
        # a transport oddity rather than "nothing answered" — a diagnostic number
        # that pointed away from the fault. Take curl's status separately.
        if ! code="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 15 -X POST "${hdr[@]}" -d "$body" "$url" 2>/dev/null)"; then
            code=""
        fi
        [ -n "$code" ] || code=000
    fi
    FILE_SHARE_BACKUP_STATE_LAST_CODE="$code"
    if [ "$code" = "200" ]; then log "file-share/backup state: reported $field=$value (HTTP 200)"; else warn "file-share/backup state: $field=$value got HTTP $code"; fi
}

# The bash config-archive service still calls this through its existing refusal
# and ERR paths.  Keep that service operational, but make the retired visible
# lane an intentional no-op: it is neither share health nor a whole-library
# FileBackup success, so posting it could only corrupt the unified clock.
feed_naglight() {
    log "legacy config-backup feed suppressed: $2"
    return 0
}
