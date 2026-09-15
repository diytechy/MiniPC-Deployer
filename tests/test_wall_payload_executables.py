"""The panel system payload's executables must be executable IN GIT.

WHY THIS FILE EXISTS. On 2026-09-14 the first live run of HomeHub's panel
system-install phase refreshed `/opt/wall-panel/stack/autoinstall/wall` from a
`git archive` of this repository and firstboot died on its own payload:

    /usr/local/sbin/wall-firstboot.sh: line 542:
      /opt/wall-panel/stack/autoinstall/wall/install-wall-capabilities.sh:
      Permission denied

Every script in that tree was mode `100644` in the index, so `git archive`
wrote them into the payload tar at `0664` and the panel got them unexecutable.
It had never shown before only because the live tree still carried `0755` bits
a human had applied by hand in an earlier session, and the release lane had
never actually replaced that tree. A repository that cannot reproduce its own
install is the defect; a `chmod` on the panel would only hide it.

Two rules are executed here:

* every tracked file in the payload that starts with `#!` is `100755` in the
  index -- the mode is what `git archive` carries, so the index is the only
  place this can be true; and
* every script firstboot (or a helper firstboot runs) executes DIRECTLY out of
  the payload -- no `bash`/`python3` in front of it, which is the form that
  needs the bit at run time -- is one of those files.

The second rule is what keeps the first honest: it is derived from the way the
installer actually invokes the payload, so a newly executed script is covered
the moment it is written, without anyone remembering this file exists.

The second heredoc defect of the same run is guarded here too: an unquoted
heredoc body is shell, and prose inside one gets executed.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = "stack/autoinstall/wall"
BACKTICK = chr(96)


def tracked_modes():
    """path -> git index mode, for the payload subtree."""
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", PAYLOAD],
                         capture_output=True, text=True, check=True).stdout
    modes = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        modes[path.strip()] = meta.split()[0]
    return modes


MODES = tracked_modes()


def shebang_files():
    found = []
    for path in sorted(MODES):
        blob = (ROOT / path)
        try:
            head = blob.open("rb").read(2)
        except OSError:
            continue
        if head == b"#!":
            found.append(path)
    return found


@pytest.mark.parametrize("path", shebang_files())
def test_every_payload_script_is_executable_in_the_index(path):
    assert MODES[path] == "100755", (
        "%s carries a #! but is %s in the git index; `git archive` would put it "
        "into the panel system payload unexecutable and firstboot would fail on "
        "it (2026-09-14)." % (path, MODES[path]))


def test_the_git_archive_the_release_ships_carries_the_executable_bit():
    """The index is not the artefact; the tar is.

    HomeHub's release lane builds the panel system payload with `git archive`
    of this subtree and extracts it onto the panel. The index modes above are
    the cause, but the 2026-09-14 failure happened at THIS boundary, so it is
    checked here directly rather than inferred. git archive applies the group
    bit itself (0664/0775, not 0644/0755), so the assertion is on the
    executable claim, which is what firstboot needs and what the transport
    actually preserves.
    """
    import io
    import tarfile

    blob = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar",
                           "HEAD", PAYLOAD], capture_output=True, check=True).stdout
    modes = {}
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        for member in tar:
            if member.isfile() and member.name.startswith(PAYLOAD + "/"):
                modes[member.name] = member.mode
    assert modes, "git archive produced no payload files"
    for path in shebang_files():
        assert modes.get(path, 0) & 0o111, (
            "%s reaches the panel at %04o; firstboot cannot exec it"
            % (path, modes.get(path, 0)))
    for name in payload_scripts_executed_directly():
        assert modes.get("%s/%s" % (PAYLOAD, name), 0) & 0o111


def test_the_payload_tree_actually_has_scripts_to_check():
    # A rule that silently checks nothing is not a rule.
    assert len(shebang_files()) >= 30


# A word that makes the quoted payload path an ARGUMENT rather than the command:
# an interpreter, a copier, or a file test, anywhere in this simple command.
CARRIER = re.compile(
    r'(?:\[|\b(?:bash|sh|python|python3|install|cp|cmp|rsync|tar|cat|source|test|'
    r'dirname|basename|read|export)\b)[^;&|]*$')


def payload_scripts_executed_directly():
    """Payload-relative names invoked as a command, with no interpreter in front.

    `"$PAYLOAD/x.sh" args` needs the executable bit; `bash "$PAYLOAD/x.sh"` and
    `python3 "$payload/x.py"` do not. Only the first form is required here, so
    the rule says what run time actually demands.
    """
    pattern = re.compile(
        r'(?P<lead>^|[;&|]|\bif\b|\bthen\b|\belse\b|\bdo\b|!)\s*'
        r'(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*'
        r'"\$\{?(?:PAYLOAD|payload)\}?/(?P<name>[A-Za-z0-9._/-]+)"')
    names = set()
    for script in sorted(ROOT.joinpath(PAYLOAD).glob("*.sh")):
        for line in script.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for match in pattern.finditer(stripped):
                # An interpreter, a copier or a file test standing between the
                # start of this simple command and the quoted path means the
                # path is an ARGUMENT, and an argument needs no executable bit.
                before = stripped[:match.start("name")]
                if CARRIER.search(before):
                    continue
                names.add(match.group("name"))
    return names


def test_the_scripts_firstboot_runs_out_of_the_payload_are_executable():
    executed = payload_scripts_executed_directly()
    # The one that actually failed on 2026-09-14 must be in the derived set, or
    # the derivation has stopped seeing the form that broke the release.
    assert "install-wall-capabilities.sh" in executed, sorted(executed)
    for name in sorted(executed):
        path = "%s/%s" % (PAYLOAD, name)
        assert path in MODES, "%s is executed out of the payload but is not tracked" % path
        assert MODES[path] == "100755", \
            "%s is executed directly out of the payload but is %s in the index" % (path, MODES[path])


# --- the heredoc hazard -------------------------------------------------


def _unescaped(body, token):
    hits, i = 0, 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
            continue
        if body.startswith(token, i):
            hits += 1
            i += len(token)
            continue
        i += 1
    return hits


def heredoc_hazards(text):
    """Lines inside an UNQUOTED heredoc carrying a live ` or $( .

    Bash runs those. On 2026-09-14 a prose comment reading "and `mic_clean`
    snoops it back out" sat inside `cat > /etc/wall-panel/audio-cards.conf
    <<EOF`, so firstboot logged `mic_clean: command not found` and wrote the
    generated ALSA config with the word deleted. `audio-cards.conf` is declared
    `generated`, so the release verifier checks presence, mode and owner and
    could never have caught it.
    """
    lines = text.split("\n")
    hazards, i = [], 0
    while i < len(lines):
        opener = re.search(r'<<-?\s*(["\']?)([A-Za-z_][A-Za-z0-9_]*)\1', lines[i])
        if not opener:
            i += 1
            continue
        quoted, delim = bool(opener.group(1)), opener.group(2)
        end = i + 1
        while end < len(lines) and lines[end].strip() != delim:
            end += 1
        if not quoted:
            for k in range(i + 1, min(end, len(lines))):
                for token in (BACKTICK, "$("):
                    if _unescaped(lines[k], token):
                        hazards.append((k + 1, token, lines[k].strip()))
        i = end + 1
    return hazards


def shell_sources():
    root = ROOT / PAYLOAD
    return sorted(set(list(root.glob("*.sh")) + list(root.glob("tests/*.sh"))
                      + [root / "wall-audio-mode", root / "wall-audio-output",
                         root / "wall-touch-filter-sleep"]))


@pytest.mark.parametrize("script", shell_sources(), ids=lambda p: p.name)
def test_no_unquoted_heredoc_carries_live_substitution(script):
    hazards = heredoc_hazards(script.read_text(encoding="utf-8", errors="replace"))
    assert hazards == [], "%s: unquoted heredoc bodies execute their own prose: %s" % (
        script.name, hazards)


def test_the_heredoc_scanner_would_have_caught_the_2026_09_14_defect():
    # Mutation guard: the rule above passes trivially if the scanner is broken.
    sample = "cat > /tmp/x <<EOF\n# and %smic_clean%s snoops it back out\nEOF\n" % (
        BACKTICK, BACKTICK)
    assert heredoc_hazards(sample)
    assert heredoc_hazards(sample.replace("<<EOF", "<<'EOF'")) == []
    assert heredoc_hazards(sample.replace(BACKTICK, "\\" + BACKTICK)) == []


@pytest.mark.skipif(shutil.which("bash") is None,
                    reason="bash is required to render the heredoc")
def test_the_audio_cards_comment_survives_rendering(tmp_path):
    """Render firstboot's audio-cards.conf heredoc through a real bash.

    The comment is the evidence: the defect did not fail the run, it silently
    deleted a word from a generated file nobody hashes.
    """
    text = (ROOT / PAYLOAD / "wall-firstboot.sh").read_text(encoding="utf-8")
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines)
                 if "audio-cards.conf <<" in line)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "EOF")
    # A bare relative name, rendered with cwd=tmp_path: a Windows absolute path
    # handed to a POSIX bash lands somewhere neither side can find again.
    block = "\n".join(lines[start:end + 1]).replace(
        "/etc/wall-panel/audio-cards.conf", "audio-cards.conf")
    script = ("_adapter=ADAPTERCARD\n_builtin=BUILTINCARD\n_loopback=LOOPCARD\n"
              + block + "\n")
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          cwd=str(tmp_path))
    assert done.returncode == 0, done.stderr
    assert "command not found" not in done.stderr, done.stderr
    rendered = (tmp_path / "audio-cards.conf").read_text(encoding="utf-8")
    # The literal comment, backticks and all.
    assert "%smic_clean%s snoops it back out" % (BACKTICK, BACKTICK) in rendered
    # ...and the expansions the block genuinely needs are still expansions.
    assert 'card "ADAPTERCARD"' in rendered
    assert 'card "BUILTINCARD"' in rendered
    assert 'card "LOOPCARD"' in rendered
    assert "$_loopback" not in rendered
