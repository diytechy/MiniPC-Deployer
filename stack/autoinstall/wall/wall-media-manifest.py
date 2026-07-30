#!/usr/bin/env python3
"""Emit the shell's two media contracts from the panel's synced cache.

Run as the POST-STEP of `wall-sync.sh` (OI-15, ruled by the Owner 2026-07-29):
the mirror puts the media in `WALL_MEDIA_CACHE/{music,frame}`, and this walks
what actually landed there and writes

    <cache>/music/index.json    LocalLibraryProvider's manifest
    <cache>/frame/playlist.json the frame playlist

The consumer is `OfficeWallNaglight/js/music/local.js` (music) and its
`js/views/frame.js` (frame); this file is the only producer. Both contracts are
documented in that repo and NOT restated here beyond what the code needs — read
`local.js`'s header block for the manifest spec.

WHY PYTHON AND NOT BASH: the one thing that must not be got wrong is JSON string
escaping. A music library is full of quotes, backslashes, `#`, `&`, newlines-in-
filenames and non-ASCII, and a hand-rolled bash JSON writer is how you ship a
manifest that parses on the bench and not in the living room. `json.dumps` is
already on every Ubuntu server (cloud-init needs python3), so this costs nothing.

THE TWO URL RULES, WHICH ARE NOT THE SAME — this is the subtlety worth stating:
  * the MUSIC manifest carries RAW, UNENCODED `path` values relative to `base`,
    because `local.js`'s `joinUrl()` percent-encodes each segment itself (it says
    so explicitly). Encoding here would double-encode every space.
  * the FRAME playlist carries FINISHED `url` values, because `frame.js` assigns
    them straight to `video.src` with no encoding step. So they ARE
    percent-encoded here, per segment.

Also deliberate:
  * `ensure_ascii=True` — the manifests are fetched by `fetch()` from a host that
    may not send `charset=utf-8`, so every non-ASCII character ships as a `\\uXXXX`
    escape. Valid JSON under any content-type guess.
  * a filename whose bytes are not valid UTF-8 (a Windows library will eventually
    produce one) cannot be represented in JSON at all. Such files are SKIPPED and
    COUNTED in the summary line, never silently dropped and never emitted as a
    lone surrogate that breaks the whole manifest for one bad name.
  * ids are the cache-relative paths, which are stable across syncs — the shell
    keeps a station selected by id, and a churning id would reset it every sync.
  * written LAST and atomically (temp + `os.replace`), which the manifest contract
    requires: a half-written manifest is the one failure the provider cannot
    detect.
  * no tag reading. `local.js` asks the producer to stay dumb ("a directory walk
    plus whatever tags the walker already has"), and the walker has none — so
    album/artist come from the directory layout and title/track from the
    filename. Nothing here shells out to ffprobe.

Usage (wall-sync.sh calls exactly this):
    wall-media-manifest.py --cache /var/cache/wall-media
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import quote

MANIFEST_VERSION = 1

# Extensions the panel's Chromium/Electron host can actually play. Anything else
# in the cache (playlists, .nfo, stray archives) is not a track and is counted as
# skipped rather than emitted as an unplayable entry.
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wav"}
VIDEO_EXT = {".mp4", ".m4v", ".webm", ".mov", ".mkv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
# Cover art, best first. Anything else in IMAGE_EXT is the fallback.
ART_STEMS = ("cover", "folder", "front", "albumart", "album", "art")

# A leading track number in a filename: "01 Title", "01. Title", "3 - Title",
# "007_Title". Deliberately narrow — a bare "1984.mp3" keeps its name because the
# remainder would be empty, and "2001 A Space Odyssey.mp4" is not a music file.
TRACK_RE = re.compile(r"^(\d{1,3})\s*[-._)\]]?\s+(.+)$")


def json_safe(name):
    """True iff NAME survives a round trip to UTF-8 (see the module docstring)."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def split_track(stem):
    """('01 Title') -> (1, 'Title'); ('Title') -> (0, 'Title')."""
    m = TRACK_RE.match(stem)
    if not m:
        return 0, stem
    return int(m.group(1)), m.group(2).strip() or stem


def rel_posix(root, path):
    """PATH relative to ROOT with forward slashes — the manifest's path shape."""
    return os.path.relpath(path, root).replace(os.sep, "/")


def pick_art(root, dirpath, names):
    """Cache-relative path to this folder's cover image, or None."""
    images = sorted(n for n in names if os.path.splitext(n)[1].lower() in IMAGE_EXT)
    if not images:
        return None
    for stem in ART_STEMS:
        for n in images:
            if os.path.splitext(n)[0].lower() == stem:
                return rel_posix(root, os.path.join(dirpath, n))
    return rel_posix(root, os.path.join(dirpath, images[0]))


def walk_music(root, base_url):
    """Build the manifest body: albums (folders) + flat tracks (root files).

    Returns (manifest_dict, n_tracks, n_skipped). Every directory holding audio
    files becomes ONE album whose id is its cache-relative path — so
    `Artist/Album/` yields artist + album, a flat `Album/` yields just the album
    name, and files sitting directly in `music/` become the manifest's top-level
    `tracks` (the flat form `local.js` documents as equally valid).
    """
    albums = []
    flat = []
    skipped = 0
    n_tracks = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        audio = []
        for n in sorted(filenames):
            if os.path.splitext(n)[1].lower() not in AUDIO_EXT:
                continue
            if not json_safe(n):
                skipped += 1
                continue
            audio.append(n)
        if not audio:
            continue

        reldir = rel_posix(root, dirpath)
        tracks = []
        for n in audio:
            path = n if reldir == "." else "{}/{}".format(reldir, n)
            num, title = split_track(os.path.splitext(n)[0])
            # `path` is RAW here on purpose — the shell encodes it (joinUrl).
            t = {"id": path, "title": title, "path": path}
            if num:
                t["track"] = num
            tracks.append(t)
        tracks.sort(key=lambda t: (t.get("track", 0), t["title"]))
        n_tracks += len(tracks)

        if reldir == ".":
            flat = tracks
            continue

        parts = reldir.split("/")
        album = {
            "id": reldir,
            "name": parts[-1],
            "tracks": tracks,
        }
        if len(parts) >= 2:
            album["artist"] = parts[-2]
        art = pick_art(root, dirpath, filenames)
        if art and json_safe(art):
            album["art"] = art
        albums.append(album)

    albums.sort(key=lambda a: a["id"])
    manifest = {
        "version": MANIFEST_VERSION,
        # `now(timezone.utc)`, not the deprecated `utcnow()` — on Ubuntu's 3.12
        # the latter emits a DeprecationWarning straight into the journal.
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base": base_url,
        "albums": albums,
    }
    if flat:
        manifest["tracks"] = flat
    return manifest, n_tracks, skipped


def walk_frame(root, base_url):
    """Build the frame playlist: [{"url": …, "title": …}, …].

    Returns (items, n_skipped). URLs are FINISHED (percent-encoded per segment) —
    `frame.js` assigns them to `video.src` untouched. Playability is Chromium's
    call and `frame.js` already advances past an item it cannot play, so the
    extension list is permissive rather than clever.
    """
    items = []
    skipped = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for n in sorted(filenames):
            if os.path.splitext(n)[1].lower() not in VIDEO_EXT:
                continue
            if not json_safe(n):
                skipped += 1
                continue
            rel = rel_posix(root, os.path.join(dirpath, n))
            items.append(
                {
                    "url": base_url + quote(rel, safe="/"),
                    "title": os.path.splitext(os.path.basename(n))[0],
                }
            )
    items.sort(key=lambda i: i["url"])
    return items, skipped


def write_atomic(path, payload):
    """Write PAYLOAD as JSON to PATH atomically, 0644 (the kiosk user reads it)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=True, indent=1, sort_keys=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Generate the wall panel's media manifests.")
    ap.add_argument("--cache", required=True, help="WALL_MEDIA_CACHE (the mirror destination)")
    ap.add_argument("--url-base", default="/media", help="the URL prefix the Electron host maps to --cache (default: /media)")
    ap.add_argument("--music-subdir", default="music")
    ap.add_argument("--frame-subdir", default="frame")
    args = ap.parse_args()

    cache = os.path.abspath(args.cache)
    url_base = args.url_base.rstrip("/")
    music_root = os.path.join(cache, args.music_subdir)
    frame_root = os.path.join(cache, args.frame_subdir)

    # The sync creates both dirs before calling this, so an absent one means the
    # caller is not wall-sync.sh (or someone deleted the cache mid-run). Say so
    # rather than writing a manifest into thin air.
    for d in (music_root, frame_root):
        if not os.path.isdir(d):
            sys.stderr.write("wall-media-manifest: not a directory: {}\n".format(d))
            return 1

    manifest, n_tracks, music_skipped = walk_music(
        music_root, "{}/{}/".format(url_base, args.music_subdir)
    )
    items, frame_skipped = walk_frame(
        frame_root, "{}/{}/".format(url_base, args.frame_subdir)
    )

    write_atomic(os.path.join(music_root, "index.json"), manifest)
    write_atomic(os.path.join(frame_root, "playlist.json"), items)

    print(
        "wall-media-manifest: music/index.json = {} track(s) in {} album(s)"
        " (+{} loose); frame/playlist.json = {} video(s)".format(
            n_tracks,
            len(manifest["albums"]),
            len(manifest.get("tracks", [])),
            len(items),
        )
    )
    if music_skipped or frame_skipped:
        # Not a failure — but never silent. A name that cannot be represented in
        # JSON is a real file the panel will not play, and the operator should be
        # able to find it from the journal.
        print(
            "wall-media-manifest: WARNING: skipped {} music and {} video file(s)"
            " whose names are not valid UTF-8 and cannot be put in JSON".format(
                music_skipped, frame_skipped
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
