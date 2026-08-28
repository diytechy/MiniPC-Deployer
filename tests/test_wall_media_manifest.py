"""The music manifest's playlist contract, from the producing side.

`js/music/local.js` has consumed a `playlists` array since it was written -
shell-architecture.md §5 specifies it and `local-library.test.mjs` pins it from
the CONSUMING side, dangling ids included. Nothing ever produced the key, so the
panel showed albums and tracks and no playlists, and the gap read as "streaming
is required for playlists" when the producer was simply skipping files the sync
had already delivered.

These are the producer's half of that contract. They need no panel, no share and
no network: the builder walks a directory, so a tmp_path IS the cache.
"""

import importlib.util
import os
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "stack" / "autoinstall" / "wall" / "wall-media-manifest.py"
)


def _load():
    """Load the builder by path - its filename is hyphenated, so it is not a
    module name, and it is a script rather than a package."""
    spec = importlib.util.spec_from_file_location("wall_media_manifest", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wmm = _load()


def _track(root: Path, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0")


def _playlist(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _library(root: Path) -> None:
    _track(root, "Artist/Album/01 One.mp3")
    _track(root, "Artist/Album/02 Two.mp3")
    _track(root, "Other/03 Three.mp3")


def _build(root: Path):
    manifest, _n, _skipped = wmm.walk_music(str(root), "/media/music/")
    return manifest


def test_a_playlist_file_becomes_a_manifest_entry(tmp_path):
    _library(tmp_path)
    _playlist(tmp_path, "Artist/Album/Mix.m3u", "01 One.mp3\n02 Two.mp3\n")

    pls = _build(tmp_path)["playlists"]

    assert len(pls) == 1
    assert pls[0]["name"] == "Mix"
    # Path-as-id, the shape albums and tracks already use.
    assert pls[0]["id"] == "Artist/Album/Mix.m3u"
    assert pls[0]["tracks"] == ["Artist/Album/01 One.mp3", "Artist/Album/02 Two.mp3"]


def test_entries_resolve_relative_to_the_playlist_and_may_point_up(tmp_path):
    # The .m3u convention, and the shape a Playlists/ folder produces.
    _library(tmp_path)
    _playlist(tmp_path, "Playlists/Morning.m3u",
              "../Artist/Album/01 One.mp3\n../Other/03 Three.mp3\n")

    pls = _build(tmp_path)["playlists"]

    assert pls[0]["tracks"] == ["Artist/Album/01 One.mp3", "Other/03 Three.mp3"]


def test_dangling_entries_are_dropped_and_the_rest_survive(tmp_path):
    # An id the shell cannot resolve renders as a row that plays nothing, which
    # is worse than an absent playlist - but one bad line must not cost the
    # whole playlist.
    _library(tmp_path)
    _playlist(tmp_path, "Artist/Album/Mix.m3u",
              "01 One.mp3\nGone.mp3\n02 Two.mp3\n")

    pls = _build(tmp_path)["playlists"]

    assert pls[0]["tracks"] == ["Artist/Album/01 One.mp3", "Artist/Album/02 Two.mp3"]


def test_traversal_is_refused(tmp_path):
    # NON-VACUOUS ON PURPOSE, and it was not on the first attempt: written the
    # obvious way this test passed with the traversal guard DELETED, because the
    # `tid in track_ids` membership check drops an outside path on its own. A
    # guard nothing can fail is the defect this repo keeps re-finding, so the
    # escaping id is PLANTED in the known-id set here - which leaves the guard as
    # the only thing between an untrusted .m3u and a path outside the cache.
    outside = tmp_path.parent / "outside.mp3"
    outside.write_bytes(b"\0")
    _library(tmp_path)
    _playlist(tmp_path, "Artist/Album/Mix.m3u",
              "../../../outside.mp3\n01 One.mp3\n")

    planted = {"Artist/Album/01 One.mp3", "../outside.mp3", "../../outside.mp3"}
    pls = wmm.read_playlists(str(tmp_path), planted)

    assert pls[0]["tracks"] == ["Artist/Album/01 One.mp3"]

def test_urls_absolute_paths_comments_and_duplicates_are_all_handled(tmp_path):
    _library(tmp_path)
    _playlist(tmp_path, "Artist/Album/Mix.m3u", "\n".join([
        "#EXTM3U",
        "#EXTINF:123,Artist - One",
        "01 One.mp3",
        "https://example.invalid/stream.mp3",   # streaming entry, out of scope
        "/etc/passwd",                          # absolute
        "01 One.mp3",                           # duplicate
        "",
        "02 Two.mp3",
    ]) + "\n")

    pls = _build(tmp_path)["playlists"]

    assert pls[0]["tracks"] == ["Artist/Album/01 One.mp3", "Artist/Album/02 Two.mp3"]


def test_a_playlist_with_nothing_resolvable_is_omitted(tmp_path):
    _library(tmp_path)
    _playlist(tmp_path, "Artist/Album/Empty.m3u", "#EXTM3U\nGone.mp3\n")

    assert "playlists" not in _build(tmp_path)


def test_a_library_with_no_playlists_emits_no_key(tmp_path):
    # An absent key keeps the manifest byte-identical to what it was before
    # playlists existed, which is what makes this change safe to ship to a panel
    # whose library has none.
    _library(tmp_path)

    assert "playlists" not in _build(tmp_path)


def test_order_is_deterministic(tmp_path):
    _library(tmp_path)
    _playlist(tmp_path, "Zeta.m3u", "Other/03 Three.mp3\n")
    _playlist(tmp_path, "Alpha.m3u", "Other/03 Three.mp3\n")

    ids = [p["id"] for p in _build(tmp_path)["playlists"]]

    assert ids == sorted(ids) == ["Alpha.m3u", "Zeta.m3u"]


def test_playlist_files_are_not_mistaken_for_tracks(tmp_path):
    # The regression this replaces: .m3u used to be counted as "skipped" and
    # nothing else. It must still never appear as a playable track.
    _library(tmp_path)
    _playlist(tmp_path, "Artist/Album/Mix.m3u", "01 One.mp3\n")

    manifest = _build(tmp_path)
    every_track = [
        t["id"]
        for album in manifest["albums"]
        for t in album["tracks"]
    ] + [t["id"] for t in manifest.get("tracks", [])]

    assert not any(tid.endswith(".m3u") for tid in every_track)
