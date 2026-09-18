# ProjectM preset payload (plan item Q)

This directory is the panel's **preset payload slot**. It follows the same
pattern as `../sensor-wheelhouse`: a committed `presets.lock` holds the identity
of every file; the files themselves are build output fetched once on a
networked machine and never fetched on the panel.

**RULED IN by the Owner, 2026-09-18.** The whole
`projectM-visualizer/presets-cream-of-the-crop` pack is selected -- all 9795
presets, mirroring the Owner's desktop projectMSDL setup -- and the licence
question below is answered for the only act this repository performs: installing
them on one private wall panel that never redistributes them. **No preset is
committed here or in OfficeWallNaglight**, which is what keeps that act distinct
from vendoring an unlicensed pack into a public repository. `presets.select` and
`presets.lock` are the only committed artefacts, and neither contains a preset.

## What the runtime needs from this directory

The panel renders ProjectM through the WebAssembly build of libprojectM 4.1.7
that ships inside the app/site artifact (OfficeWallNaglight
`vendor/projectm/`, provenance in that directory's `PROVENANCE.md`). That
artifact brings **no apt package, no shared library, no systemd unit and no new
runtime dependency to this image** — the whole hosting decision turned on that
(the kiosk compositor `cage` is single-surface, so a second native GL client
cannot be composed under the shell, and a native host would have to open its own
ALSA capture of the music bus, which item Q forbids).

What it does need is presets. libprojectM ships none; every frontend supplies
its own. So this directory is the ONLY new deployment input item Q creates.

## The licence question, unanswered

The pack projectM itself has used as its default since 2022,
`projectM-visualizer/presets-cream-of-the-crop` (~9 800 Milkdrop presets),
carries **no licence**. Its own `LICENSE.md` states the presets "were not
released under any specific license", that the authors "hold the full
copyright", and that it is "safe to assume" they are public domain, with a
takedown contact for authors who object.

That is an assumption, not a grant. Installing a few dozen of them on a private
wall panel that never redistributes them is a different act from vendoring them
into a public-facing repository, and the first may well be fine where the second
is not — but which of those the Owner is willing to do is a ruling, not a
default.

**That ruling was given on 2026-09-18: the whole pack, installed on the one
private panel.** What it did NOT change is everything below, which is what keeps
the private install from quietly becoming redistribution:

* no preset is committed to the public OfficeWallNaglight repository;
* no preset is committed here either, so this repository does not become the
  quiet second copy;
* `presets.select` names all 9795 by their UPSTREAM path and `presets.lock`
  pins a sha256 for each. Together they are the curation, and they are what lets
  any machine reproduce the payload from git -- **no deployment depends on
  anyone's local copy** (Owner's question, 2026-09-18).

## Reproducing the payload on a new machine

```sh
# From WSL Ubuntu or any Linux box. NOT Git-for-Windows: these filenames contain
# characters Windows cannot represent and a Windows checkout silently drops ~169
# of the 9795.
export PRESET_REPO=https://github.com/projectM-visualizer/presets-cream-of-the-crop
export PRESET_COMMIT=0180df21f5e0bd39b9060cc5de420ed2f1f9e509
bash stack/autoinstall/wall/build-projectm-presets.sh --refresh   # fetches, needs network
bash stack/autoinstall/wall/build-projectm-presets.sh                # verifies, no network
```

The second form is the one every later build runs: it checks every locked
sha256 and fails rather than installing bytes the lock does not name.

**Verified 2026-09-18:** that commit is byte-identical to the Owner's working
copy once CRLF is normalised -- all 9795 files, same paths, same content. The
local copy is a Windows checkout and carries CRLF; upstream is LF, and LF is
what ships.

## What the panel does with no presets

libprojectM renders its built-in idle output. The trial is still runnable and
still measurable in that state — it is a real GPU load — it simply does not
exercise per-pixel preset equations, which are the expensive part. The trial
document says so explicitly rather than presenting an idle-preset measurement as
a ProjectM measurement.

## Populating it, once the Owner has ruled

1. Choose a SMALL vetted set (the initial proposal is 20–40 presets, chosen for
   low per-pixel cost, from one named upstream pack at one named revision).
2. Run `../build-projectm-presets.sh --refresh` on a networked machine. It
   fetches that revision, copies the chosen files, and writes `presets.lock`
   with a SHA-256 per file plus the upstream repository and commit.
3. Review the lock, commit only the lock.
4. Every later run without `--refresh` reproduces from the lock under
   `--require-hashes` semantics: same bytes or a hard failure.

## The install transaction does not exist yet (SR-031, LLR-019)

**Nothing installs this payload today.** Neither `wall-firstboot.sh` nor
`user-data` stages, copies or verifies it, so the intended location below is a
PROPOSAL, not a description of the panel. That is currently harmless because the
lock is empty by design and the renderer is built to run presetless -- but it
means the day the Owner rules a pack in, the payload would be reproduced into
`.out-wall/` and go no further, silently. Building the install step is open D
release-lane work and is recorded as SR-031 / LLR-019 / TC-016. Found by the
integrated terra review, 2026-09-14, finding 5.

Intended location on the panel, once that step exists:
`/opt/wall-panel/projectm-presets/`, owner `root:root`, directories `0755`,
files `0644`. The renderer would read them through the hub-served site like any
other payload asset; nothing on the panel executes them.
