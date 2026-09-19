#!/usr/bin/env python3
"""deploy_stack.py - deliver the tracked `stack/` tree to a running hub.

Implements: SR-047 (delivery), and the general gap it exposed.

─────────────────────────────────────────────────────────────────────────────
WHY THIS EXISTS: THERE WAS NO LANE, AND "ENABLE A SERVICE" LOOKED LIKE ONE.
─────────────────────────────────────────────────────────────────────────────

`/opt/homehub` is a plain deployed tree, not a git checkout. It arrived from the
autoinstall ISO and there was no incremental path back to it. The four lanes
that existed are the paired panel release, the panel system manifest, the
NagLight image, and single-config-file-in-place; none delivers a hub service.

`stack/README.md`'s "Enabling a service on a RUNNING box" reads exactly like the
answer and is not:

    $EDITOR .env && docker compose up -d

That is correct for the tier-2 catalog, because those services are a compose
profile plus an UPSTREAM IMAGE and need nothing new on disk. It is wrong the
moment a service ships files - `stack/litellm/` carries two BIND-MOUNTED files
the container cannot start without, and `stack/llm-isolation/` a script and two
units. Following the running-box instructions for those produces a container
that cannot start and a fence that was never installed.

Measured on the live hub 2026-09-19, the drift was never only the new lane:
21 tracked files absent and 18 stale, including `docker-compose.yml` (no
`litellm` service, no `llmprivate` network), `autoinstall/firstboot.sh` (so the
idempotent re-run that installs units would have installed nothing new), and
`devpc-wake/devpc_wake_service.py`. A hand-delivery of "the LLM lane" would
have left the box partially updated against its own repo and nothing would have
said so.

─────────────────────────────────────────────────────────────────────────────
WHAT IT DOES, AND THE THREE RULES IT WILL NOT BREAK
─────────────────────────────────────────────────────────────────────────────

Source of truth is `git archive HEAD stack/` - TRACKED FILES AT A COMMIT, with
git's own LF normalisation. Not the working tree: a dirty tree is how you
deploy half of somebody's unfinished change, and this repo has already paid for
that once with six requirement rows.

  1. IT NEVER WRITES `.env`, `*.creds`, or anything under `secrets/`.
     The box's `.env` is the only copy of live configuration and secrets, it is
     NOT in the repo, and `.env.example` is a template with placeholder values.
     Overwriting one with the other would take the hub down and lose
     credentials that exist nowhere else. The knobs a release needs are the
     operator's job, and the report names the ones that are missing.

  2. IT NEVER DELETES. Files on the box that the repo does not track are left
     alone: `docker-compose.override.yml`, `llm-gateway.env`, the deployed
     `panel-access/app/` bundle, `__pycache__`. A sync that "tidied" those
     would remove live configuration and a deployed application.

  3. IT PRESERVES INODES. Compose bind-mounts bind the inode, not the path, so
     `install`/`mv`/`cp` - and `tar -x`, which unlinks before recreating -
     detach the mount: the container keeps serving the old bytes and a reload
     is a silent no-op. Every file is written with `cat staged > target`, which
     truncates in place and keeps the inode. This does not matter on first
     delivery (nothing is mounted yet) and matters on every one after.

DRY RUN IS THE DEFAULT. `--apply` is required to change anything.

VERIFY, DO NOT TRUST. The hashes are read back FROM THE BOX after writing and
compared to what was intended. `scp` returning 0 means bytes moved, not that
the right bytes are in the right place.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
import tarfile
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
STACK_REMOTE = "/opt/homehub/stack"

# NEVER WRITTEN. See rule 1. Matched against the path relative to stack/.
NEVER_WRITE_EXACT = {".env"}
NEVER_WRITE_SUFFIX = (".creds",)
NEVER_WRITE_PREFIX = ("secrets/",)

# THE PANEL'S PAYLOAD, WHICH IS NOT THE SAME AS "DIRECTORIES WITH PANEL NAMES".
#
# The paired release derives the panel's system payload from a MiniPC COMMIT and
# delivers it dev-PC -> panel. The hub's copies of these are vestigial: keeping
# them current changes nothing about the panel, and shipping them here would
# imply a delivery path that does not exist.
#
# The membership test is "does the HUB use it", and it was measured rather than
# guessed from the names, because the names mislead in both directions:
#
#   autoinstall/wall/  panel   - the panel's firstboot and units
#   panel-audio/       panel   - NOT mounted by hub compose, NOT installed by
#                                hub firstboot. Installed by the PANEL's
#                                wall-firstboot.sh. Inert on the hub.
#   panel-access/      HUB     - installed by the hub's firstboot
#   wall-shell/        HUB     - caddy bind-mounts it (`./wall-shell:/srv/...`)
#                                and serves the panel's SITE from the hub
#
# So two of the four panel-sounding directories are hub-side and must ship.
PANEL_PREFIX = ("autoinstall/wall/", "panel-audio/")


def die(msg: str) -> "NoReturn":       # noqa: F821
    print("deploy-stack: ERROR: %s" % msg, file=sys.stderr)
    raise SystemExit(1)


def run(argv, **kw):
    return subprocess.run(argv, check=True, capture_output=True, **kw)


def excluded(rel: str, include_panel: bool) -> str | None:
    """Why this path is not delivered, or None if it is."""
    if rel in NEVER_WRITE_EXACT:
        return "protected (live config/secrets, never overwritten)"
    if rel.endswith(NEVER_WRITE_SUFFIX):
        return "protected (credentials)"
    if rel.startswith(NEVER_WRITE_PREFIX):
        return "protected (secrets tree)"
    if rel.startswith(PANEL_PREFIX) and not include_panel:
        return "panel payload (its own lane; --include-panel-payload to force)"
    return None


def build_payload(revision: str, include_panel: bool):
    """{relpath: (sha256, bytes, is_executable)} from git, not the working tree."""
    out = run(["git", "-C", str(REPO), "archive", "--format=tar", revision, "stack"])
    payload, skipped = {}, {}
    with tarfile.open(fileobj=__import__("io").BytesIO(out.stdout)) as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            rel = m.name.split("stack/", 1)[1] if m.name.startswith("stack/") else m.name
            why = excluded(rel, include_panel)
            if why:
                skipped[rel] = why
                continue
            data = tf.extractfile(m).read()
            # A SHEBANG MEANS EXECUTABLE, WHATEVER THE INDEX SAYS. Several of
            # these scripts are committed 100644 - `firstboot.sh`,
            # `llm-isolation.sh`, `run-hermetic-tests.sh` - and honouring that
            # literally STRIPPED the bit off files on the live box that had it.
            # Nothing on the hub actually needs it (everything is run as
            # `bash <script>`, sourced, or installed to sbin with an explicit
            # mode), which is the only reason that was cosmetic rather than an
            # outage. It is still not this tool's business to take a bit away.
            executable = bool(m.mode & 0o111) or data[:2] == b"#!"
            payload[rel] = (hashlib.sha256(data).hexdigest(), data, executable)
    if not payload:
        die("git archive %s produced no stack/ files - wrong revision?" % revision)
    return payload, skipped


def remote_hashes(target: str) -> dict:
    """sha256 of every file currently under the deployed stack tree."""
    # ONE `sha256sum` INVOCATION, NOT ONE PER FILE. The first version spawned a
    # `sudo sha256sum` per path inside a `while read` loop - roughly 300 sudo
    # calls against the live tree, and it read filenames through a shell, so a
    # path with a space or a glob character would have been mangled. `-exec ...
    # {} +` batches them and passes paths as argv, never as shell words.
    #
    # sha256sum's own output is `<hash>  <path>`, and it escapes a path
    # containing a newline or a backslash by prefixing the line with `\\`. Such
    # a line is dropped rather than guessed at: this tool must never write to a
    # path it has misparsed.
    script = ("cd %s 2>/dev/null || exit 3; "
              "sudo -n find . -type f -exec sha256sum {} +" % STACK_REMOTE)
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", target, script],
                       capture_output=True, text=True)
    if r.returncode == 3:
        die("%s does not exist on %s - this tool updates a provisioned hub, "
            "it does not provision one" % (STACK_REMOTE, target))
    if r.returncode != 0:
        die("could not read the deployed tree: %s" % (r.stderr.strip() or r.returncode))
    out = {}
    for line in r.stdout.splitlines():
        if line.startswith("\\"):
            # An escaped path (newline or backslash in the name). Refuse to
            # guess; it will show up as absent and be reported, not written.
            continue
        h, _, f = line.partition("  ")
        f = f.strip()
        if f.startswith("./"):
            f = f[2:]
        if f:
            out[f] = h.strip()
    return out


def classify_stale(target: str, stale: list, payload: dict) -> dict:
    """Split stale files into real content drift and line-ending-only.

    WHY THIS IS WORTH A SECOND ROUND TRIP. `git archive` emits LF; files
    authored on Windows and deployed from an ISO can sit on the box with CRLF.
    Those compare as different and ARE different, but replacing them changes no
    behaviour, and an operator reading a plan cannot tell the two apart from a
    hash. Measured on the live hub 2026-09-19: 13 of 15 stale files were real
    content drift and 2 were line endings alone - so this is not a way of
    dismissing the list, it is a way of making the real 13 visible.

    Both are still delivered. LF is the correct form on the target, and a CRLF
    shebang on an executable makes the kernel look for an interpreter whose
    name ends in a carriage return. (Neither of the two found was executed that
    way - both are invoked as `/usr/bin/python3 <file>` - so this normalisation
    fixes nothing today and forecloses it tomorrow.)
    """
    if not stale:
        return {}
    # One call, not one per file.
    # Paths go in on STDIN and are read with `read -r`, so they are never
    # parsed as shell words. Interpolating them into the script - as the first
    # version did - would have broken on a space and would have executed
    # anything a filename contained, as root.
    script = ("cd %s || exit 3\n"
              "while IFS= read -r f; do\n"
              "  printf '%%s  %%s\\n' "
              "\"$(sudo -n tr -d '\\r' < \"$f\" | sha256sum | cut -d' ' -f1)\" \"$f\"\n"
              "done" % STACK_REMOTE)
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", target, script],
                       input="\n".join(stale) + "\n",
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {rel: "unknown" for rel in stale}
    box_lf = {}
    for line in r.stdout.splitlines():
        h, _, f = line.partition("  ")
        if f.strip():
            box_lf[f.strip()] = h.strip()
    out = {}
    for rel in stale:
        want_lf = hashlib.sha256(
            payload[rel][1].replace(b"\r\n", b"\n")).hexdigest()
        out[rel] = ("line-endings" if box_lf.get(rel) == want_lf else "content")
    return out


def plan(payload: dict, current: dict):
    missing, stale, same = [], [], []
    for rel, (h, _data, _x) in sorted(payload.items()):
        if rel not in current:
            missing.append(rel)
        elif current[rel] != h:
            stale.append(rel)
        else:
            same.append(rel)
    return missing, stale, same


def safe_paths(todo: list) -> None:
    """Refuse a payload whose paths the remote shell cannot carry safely.

    The placement loop is POSIX `while IFS= read -r`, which survives spaces but
    NOT a newline in a path - and a path with a leading dash or a control
    character has no business in this repo either. Rather than build an
    elaborate NUL-delimited protocol for filenames that do not exist, the tool
    VALIDATES and refuses. These paths come from `git archive` on a repo we
    control, so a hit here means something is wrong upstream, not that the
    protocol needs widening.
    """
    bad = [r for r in todo
           if ("\n" in r or "\r" in r or "\0" in r
               or r.startswith("-") or r != r.strip())]
    if bad:
        die("refusing to deliver path(s) the placement protocol cannot carry "
            "safely: %r" % bad[:5])


def deliver(target: str, payload: dict, todo: list):
    """Stream a tar of only what changed, then place each file inode-safely.

    ─────────────────────────────────────────────────────────────────────────
    THREE THINGS THIS GOT WRONG, ALL FOUND IN REVIEW, ALL ON A TOOL THAT
    WRITES TO A LIVE HOUSEHOLD SERVER AS ROOT.
    ─────────────────────────────────────────────────────────────────────────

    1. IT REQUIRED BASH AND ASKED FOR AN UNSPECIFIED SHELL. The loop used
       `read -r -d ''` and process substitution `< <(...)`, which are bash, and
       `ssh host <command>` runs the login shell - `dash` on plenty of Ubuntu
       accounts. The delivery would have failed to PARSE. Worse, it was
       introduced while "hardening" a version that had been POSIX. This now
       runs an explicitly POSIX loop and validates the paths instead.

    2. A SYMLINK AT THE DESTINATION COULD HAVE TRUNCATED `.env`. `find -type f`
       omits symlinks, so a tracked path that exists on the box AS A SYMLINK
       reads as ABSENT, and `cat > dst` then follows it as root. A
       `litellm/config.yaml -> ../.env` - from a stale repair, or an operator's
       tidy-up - would have destroyed the one file this tool promises never to
       write, while reporting a clean delivery. Every destination leaf and
       every ancestor directory is now checked with `[ -L ]` and the run
       REFUSES rather than writing through a link.

    3. A FAILURE MID-PLACEMENT LEFT A MIXED REVISION WITH NO WAY BACK. Two
       files, ssh dies after the first: new compose, old firstboot, no report,
       staging left behind. Each target is now COPIED ASIDE before it is
       overwritten, and any failure restores every file already touched before
       exiting non-zero. Re-running is still the normal repair; rollback is for
       the case where re-running cannot work, such as a full disk.
    """
    safe_paths(todo)

    buf = __import__("io").BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        # The manifest travels WITH the payload, so the placement loop reads
        # paths from a file instead of re-deriving them with `find` - which is
        # what let a symlink masquerade as an absent regular file.
        manifest = ("\n".join(todo) + "\n").encode("utf-8")
        mi = tarfile.TarInfo(name=".deploy-manifest")
        mi.size = len(manifest)
        mi.mode = 0o644
        mi.mtime = int(time.time())
        tf.addfile(mi, __import__("io").BytesIO(manifest))
        for rel in todo:
            h, data, is_exec = payload[rel]
            info = tarfile.TarInfo(name=rel)
            info.size = len(data)
            info.mode = 0o755 if is_exec else 0o644
            info.mtime = int(time.time())
            tf.addfile(info, __import__("io").BytesIO(data))
    blob = buf.getvalue()

    place = r"""
set -eu
D="%s"
# PRIVILEGE ESCALATION IS A VARIABLE SO THIS SCRIPT CAN BE RUN, NOT ONLY READ.
# The tests execute it against a fake tree with DEPLOY_SUDO= (empty), which is
# the only way to check the things that matter here - that the inode survives,
# that a symlink is refused, that a mid-run failure rolls back. It also does the
# right thing when the caller is already root. Unquoted on purpose: it must
# split into `sudo -n`.
SUDO="${DEPLOY_SUDO-sudo -n}"
S="$(mktemp -d /tmp/deploy-stack.XXXXXX)"
B="$S/.rollback"
# CLEAN UP AS ROOT, AND NEVER LET CLEANUP DECIDE THE OUTCOME.
# `tar -x` runs under $SUDO, so the staged files are root-owned; an unprivileged
# `rm -rf` on them fails, the trap's status becomes the script's status, and a
# DELIVERY THAT FULLY SUCCEEDED is reported as a failure. That happened on the
# first real run: every file landed and verified, and the tool said verified
# False because it could not tidy /tmp. `|| true` because a leftover staging
# directory is litter, not a failure - the verification is the authority on
# whether the delivery worked.
trap '$SUDO rm -rf "$S" 2>/dev/null || true' EXIT
$SUDO mkdir -p "$B"
$SUDO tar -x -C "$S" -f -

# ---- restore anything already placed, then fail ---------------------------
rollback() {
    echo "deploy-stack: FAILED at '$1' - restoring files already written" >&2
    if [ -f "$B/.done" ]; then
        while IFS= read -r d; do
            [ -f "$B/files/$d" ] || continue
            $SUDO sh -c 'cat "$1" > "$2"' _ "$B/files/$d" "$D/$d"                 || echo "deploy-stack: ROLLBACK FAILED for $d" >&2
        done < "$B/.done"
    fi
    exit 1
}

$SUDO touch "$B/.done"
$SUDO mkdir -p "$B/files"

while IFS= read -r f; do
    [ -n "$f" ] || continue

    # ---- NO WRITE MAY FOLLOW A SYMLINK ------------------------------------
    # The leaf first, then every ancestor inside the stack tree. A link
    # anywhere on the path can redirect a root write out of the tree entirely.
    if [ -L "$D/$f" ]; then
        echo "deploy-stack: REFUSING: $D/$f is a symlink" >&2
        rollback "$f"
    fi
    d=$(dirname "$f")
    while [ "$d" != "." ] && [ "$d" != "/" ]; do
        if [ -L "$D/$d" ]; then
            echo "deploy-stack: REFUSING: ancestor $D/$d is a symlink" >&2
            rollback "$f"
        fi
        d=$(dirname "$d")
    done

    $SUDO mkdir -p "$D/$(dirname "$f")" || rollback "$f"

    # Copy the current bytes aside BEFORE touching them, so a later failure can
    # put them back. A file that does not exist yet needs no backup and simply
    # is not recorded.
    if [ -f "$D/$f" ]; then
        $SUDO mkdir -p "$B/files/$(dirname "$f")" || rollback "$f"
        $SUDO sh -c 'cat "$1" > "$2"' _ "$D/$f" "$B/files/$f" || rollback "$f"
    fi

    # `cat "$1" > "$2"` TRUNCATES IN PLACE and keeps the inode, so a compose
    # bind mount on this path survives; install/mv/cp/tar-x would replace the
    # inode and the container would go on serving the old bytes through a
    # reload that reports success. The paths are POSITIONAL ARGUMENTS, never
    # interpolated text.
    $SUDO sh -c 'cat "$1" > "$2"' _ "$S/$f" "$D/$f" || rollback "$f"
    # NEVER TAKE A BIT AWAY. If the staged file is executable, ensure the
    # target is; otherwise LEAVE THE TARGET'S MODE ALONE. Setting 0644
    # unconditionally is what stripped +x from eleven files on the first real
    # run - a mode change nobody asked for, on a live box, as a side effect of
    # a content sync.
    if [ -x "$S/$f" ]; then $SUDO chmod 0755 "$D/$f" || rollback "$f"; fi

    printf '%%s\n' "$f" | $SUDO tee -a "$B/.done" >/dev/null
done < "$S/.deploy-manifest"
""" % STACK_REMOTE

    r = subprocess.run(["ssh", "-o", "BatchMode=yes", target, place],
                       input=blob, capture_output=True)
    if r.returncode != 0:
        return r.stderr.decode("utf-8", "replace").strip()
    return None


def run_firstboot(target: str, force: bool) -> int:
    """Re-run firstboot, which is what actually installs systemd units.

    ───────────────────────────────────────────────────────────────────────────
    TWO DEFECTS HERE, BOTH FOUND IN REVIEW, AND THE FIRST IS ONE THIS PROJECT
    HAS ALREADY PAID FOR ONCE IN A DIFFERENT FILE.
    ───────────────────────────────────────────────────────────────────────────

    THE EXIT STATUS WAS BEING THROWN AWAY BY A PIPE. The command was
    `firstboot.sh 2>&1 | tail -40`, and a pipeline's status is its LAST
    command - so a firstboot that failed, followed by a `tail` that succeeded,
    returned ZERO. The tool recorded `firstboot_rc: 0` and told the caller the
    deployment had succeeded while no unit had been installed. That is exactly
    the class of bug the llm-isolation fence was rewritten for in round 6, one
    directory over, arrived at from the opposite direction. The output is now
    captured whole and trimmed IN PYTHON, after the status is in hand.

    FIRSTBOOT CAN STOP DOCKER, and calling it "idempotent" hid that. Step 1b
    moves /var/lib/docker onto its own LV, and to do that it stops the daemon -
    which on this box means DNS, TLS, the tracker and every media service go
    down for the duration. It is idempotent in the sense that re-running
    converges; it is NOT harmless on a live, in-service hub.

    So the risk is MEASURED rather than described: if /var/lib/docker is
    already mounted from the docker-data LV, step 1b returns early and this
    cannot happen. If it is not, the run is REFUSED unless forced, because a
    household losing DNS deserves better than a footnote.
    """
    probe = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", target,
         "findmnt -n -o SOURCE --target /var/lib/docker 2>/dev/null || true"],
        capture_output=True, text=True)
    source = probe.stdout.strip()
    migrated = "docker--data" in source or "docker-data" in source

    if migrated:
        print("deploy-stack: /var/lib/docker is already on %s, so firstboot's "
              "LV migration returns early and will NOT stop docker." % source)
    elif force:
        print("deploy-stack: WARNING /var/lib/docker is on %s, NOT the "
              "docker-data LV. firstboot may STOP DOCKER to move it - DNS, TLS "
              "and every container go down while it does. Proceeding on "
              "--force-firstboot." % (source or "an unknown device"))
    else:
        die("/var/lib/docker is on %s, not the docker-data LV, so re-running "
            "firstboot may STOP DOCKER to migrate it and take DNS, TLS and "
            "every service down with it.\n"
            "The files are delivered and verified; only the unit install is "
            "skipped. Re-run with --force-firstboot when the box can afford "
            "an outage, or install the units by hand."
            % (source or "an unknown device"))

    print("deploy-stack: re-running firstboot (idempotent, but not silent)")
    # NO PIPE. The status must survive.
    # `bash <script>`, NOT the script directly. firstboot.sh is committed 100644
    # and every caller inside the stack already invokes its siblings as
    # `bash "$STACK_DIR/provision/..."`; running it directly made the first real
    # deployment fail with `command not found` on a file that was present and
    # correct. Going through bash is also what makes this independent of
    # whatever mode the file happens to carry.
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", target,
         "sudo -n bash /opt/homehub/stack/autoinstall/firstboot.sh 2>&1"],
        capture_output=True, text=True)
    tail = r.stdout.splitlines()[-40:]
    print("\n".join("    " + l for l in tail))
    return r.returncode


def _write_report(path, result) -> None:
    """Evidence must survive the failure it is evidence OF.

    The first version wrote the report at the very end, so every path that
    called `die()` - a failed transfer, a file that did not land - exited with
    no record at all. That is precisely when somebody needs to know which files
    were touched.
    """
    if not path:
        return
    pathlib.Path(path).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("deploy-stack: report -> %s" % path)


def env_gap(target: str, payload: dict) -> dict:
    """Knobs the delivered `.env.example` declares that the box's `.env` lacks.

    THE DOCSTRING ABOVE PROMISED THIS AND THE CODE DID NOT DO IT, which is the
    defect this repo keeps finding in its own work: a comment describing a
    control that is not implemented is worse than no comment.

    It matters because rule 1 means this tool CANNOT write `.env` - so a
    release that needs a new knob delivers the files, reports a clean verified
    landing, and leaves a hub that cannot start the thing it just installed.
    `stack/litellm/` is exactly that case: without LITELLM_CONTAINER_IP the
    fence refuses (correctly, it fails closed) and without LITELLM_IMAGE_DIGEST
    compose cannot even resolve the image, and neither failure names the cause.

    KEY NAMES ONLY, NEVER VALUES. The box's `.env` is the only copy of its
    credentials; printing a value into a terminal, a log or a --report file
    would be a disclosure the tool has no reason to risk. An EMPTY value is
    reported as present, because empty is a legitimate deliberate setting here
    (DEVPC_WAKE_URL empty disables the hold on purpose) and second-guessing it
    would be noise.
    """
    if ".env.example" not in payload:
        return {}
    # A key whose EXAMPLE carries a default is one the example thinks needs a
    # value. A key the example leaves empty is one where empty is a legitimate
    # setting - DEVPC_WAKE_URL empty deliberately disables the hold. That
    # distinction is what lets the box's empties be judged without reading a
    # single value.
    wanted, defaulted = [], []
    for raw in payload[".env.example"][1].decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        wanted.append(key)
        if val.strip():
            defaulted.append(key)

    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", target,
         "sudo -n sed -n 's/^\\([A-Za-z_][A-Za-z0-9_]*\\)=.*/\\1/p' "
         "%s/.env 2>/dev/null" % STACK_REMOTE],
        capture_output=True, text=True)
    if r.returncode != 0:
        return {"_error": "could not read the box's .env key list"}
    have = {k.strip() for k in r.stdout.splitlines() if k.strip()}

    # EMPTY-BUT-REQUIRED, reported as a BOOLEAN and never as a value. `sed`
    # matches only lines whose value is literally empty and prints the KEY, so
    # nothing secret can reach stdout, a log, or the --report file.
    #
    # This exists because "present" is not the same as "set". DEVPC_TARGET_MODEL
    # sits on the box as an empty key: it passes a presence check, and compose
    # builds the model id as "openai/${DEVPC_TARGET_MODEL}", so an empty value
    # resolves to "openai/" and the route cannot work at all. A delivery that
    # reported every knob present would have been telling the truth and still
    # leaving a lane that could not answer.
    r2 = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", target,
         "sudo -n sed -n 's/^\\([A-Za-z_][A-Za-z0-9_]*\\)=[[:space:]]*$/\\1/p' "
         "%s/.env 2>/dev/null" % STACK_REMOTE],
        capture_output=True, text=True)
    blank = {k.strip() for k in r2.stdout.splitlines() if k.strip()} \
        if r2.returncode == 0 else set()

    return {
        "missing": [k for k in wanted if k not in have],
        "empty_but_defaulted": [k for k in defaulted if k in blank],
        "declared": len(wanted),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", required=True, metavar="USER@HOST")
    ap.add_argument("--revision", default="HEAD",
                    help="the commit to deliver (default HEAD). Tracked files "
                         "at a COMMIT, never the working tree.")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without it this is a dry run.")
    ap.add_argument("--include-panel-payload", action="store_true",
                    help="also deliver the panel payload (autoinstall/wall/, panel-audio/). Its own lane normally carries these; the hub does not use them.")
    ap.add_argument("--run-firstboot", action="store_true",
                    help="after a verified delivery, re-run firstboot so new "
                         "units are installed. Refuses if firstboot could stop "
                         "docker to migrate /var/lib/docker.")
    ap.add_argument("--force-firstboot", action="store_true",
                    help="run firstboot even though it may STOP DOCKER to "
                         "migrate /var/lib/docker, taking DNS, TLS and every "
                         "container down while it does.")
    ap.add_argument("--report", metavar="PATH",
                    help="write JSON evidence here. Must not exist.")
    args = ap.parse_args()

    if args.report and pathlib.Path(args.report).exists():
        die("--report %s exists. Evidence is never overwritten; pick a new path."
            % args.report)

    dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        print("deploy-stack: NOTE the working tree is dirty; delivering %s, "
              "not what is on disk." % args.revision)

    payload, skipped = build_payload(args.revision, args.include_panel_payload)
    current = remote_hashes(args.target)
    missing, stale, same = plan(payload, current)
    todo = missing + stale

    print("deploy-stack: %s -> %s @ %s" % (args.revision, args.target, STACK_REMOTE))
    print("  tracked and deliverable : %d" % len(payload))
    print("  protected / other lane  : %d" % len(skipped))
    print("  already identical       : %d" % len(same))
    print("  ABSENT on the hub       : %d" % len(missing))
    for rel in missing:
        print("      + %s" % rel)
    kinds = classify_stale(args.target, stale, payload)
    real = [r for r in stale if kinds.get(r) != "line-endings"]
    eol = [r for r in stale if kinds.get(r) == "line-endings"]
    print("  STALE on the hub        : %d (%d content, %d line-endings only)"
          % (len(stale), len(real), len(eol)))
    for rel in real:
        print("      ~ %s" % rel)
    for rel in eol:
        print("      ~ %s   [line endings only]" % rel)

    untracked = sorted(set(current) - set(payload) - set(skipped))
    print("  on the hub, not tracked : %d (LEFT ALONE, never deleted)" % len(untracked))

    # THE KNOBS, which this tool may not write and must therefore NAME.
    gap = env_gap(args.target, payload)
    miss = gap.get("missing", [])
    if gap.get("_error"):
        print("  .env knobs              : %s" % gap["_error"])
    elif miss:
        print("  .env knobs MISSING      : %d of %d declared - THE OPERATOR MUST "
              "ADD THESE" % (len(miss), gap.get("declared", 0)))
        for k in miss:
            print("      ! %s" % k)
        print("      (names only; this tool never reads or writes .env values)")
    else:
        print("  .env knobs              : all %d declared keys are present"
              % gap.get("declared", 0))
    blanks = gap.get("empty_but_defaulted", [])
    if blanks:
        print("  .env knobs EMPTY        : %d key(s) the example gives a default "
              "for are blank on the box" % len(blanks))
        for k in blanks:
            print("      ? %s" % k)
        print("      (present is not set - an empty value can be a working "
              "config or a dead one, so these are named rather than judged)")

    result = {
        "revision": args.revision, "target": args.target,
        "applied": bool(args.apply), "missing": missing, "stale": stale,
        "identical": len(same), "skipped": skipped,
        "stale_kind": kinds,
        "untracked_left_alone": untracked, "verified": None,
        "env_keys_missing": miss,
        "env_keys_empty_but_defaulted": gap.get("empty_but_defaulted", []),
    }

    if not todo:
        print("deploy-stack: the hub already matches %s. Nothing to do." % args.revision)
    elif not args.apply:
        print("deploy-stack: DRY RUN. %d file(s) would be written. Re-run with "
              "--apply." % len(todo))
    else:
        err = deliver(args.target, payload, todo)
        if err:
            result["verified"] = False
            result["delivery_error"] = err
            _write_report(args.report, result)
            die("delivery failed (files already written were rolled back on "
                "the box; read the message above):\n%s" % err)

        # VERIFY FROM THE BOX. Re-read, do not trust the transfer.
        after = remote_hashes(args.target)
        bad = [rel for rel in todo
               if after.get(rel) != payload[rel][0]]
        result["verified"] = not bad
        if bad:
            for rel in bad:
                print("  MISMATCH %s (want %s, got %s)"
                      % (rel, payload[rel][0][:12], str(after.get(rel))[:12]))
            result["mismatched"] = bad
            _write_report(args.report, result)
            die("%d file(s) did not land as intended - the hub is PARTIALLY "
                "updated. Re-run; this tool is idempotent." % len(bad))
        print("deploy-stack: %d file(s) delivered and verified by sha256 on the box."
              % len(todo))

    # FIRSTBOOT IS ASKED FOR SEPARATELY, AND IS RUN WHENEVER IT IS ASKED FOR.
    #
    # It used to live inside the "we wrote something" branch, so a tree that was
    # already up to date skipped it - and that is exactly the state a retry is
    # in. Delivering the files and installing the units are two steps; doing the
    # first successfully, then re-running to do the second, printed "Nothing to
    # do" and installed nothing. Found on the first real deployment.
    #
    # A dry run still runs nothing: --apply gates every side effect.
    if args.run_firstboot and args.apply and result.get("verified") is not False:
        rc = run_firstboot(args.target, args.force_firstboot)
        result["firstboot_rc"] = rc
        if rc != 0:
            _write_report(args.report, result)
            die("firstboot exited %d. The files are delivered and verified, "
                "but units may not be installed - read the log above." % rc)
    elif args.run_firstboot and not args.apply:
        print("deploy-stack: --run-firstboot ignored in a dry run; it is a side "
              "effect and needs --apply.")

    _write_report(args.report, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
