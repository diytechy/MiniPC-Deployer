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
    manifest, _n, _skipped, _unresolved = wmm.walk_music(str(root), "/media/music/")
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
    pls, _unresolved = wmm.read_playlists(str(tmp_path), planted)

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
    # AND the playlist must actually have been PRODUCED. Without this line the
    # assertion above passes with every scrap of playlist support deleted -
    # `.m3u` was never in AUDIO_EXT - so it proved only the old behaviour while
    # calling itself a regression test for the new. Adversarial review,
    # 2026-08-28.
    assert [p["id"] for p in manifest["playlists"]] == ["Artist/Album/Mix.m3u"]


def test_a_bom_does_not_silently_empty_the_playlist(tmp_path):
    # A Windows-written .m3u8 routinely carries a UTF-8 BOM. Read as plain
    # utf-8 it stays glued to the FIRST entry, which then matches no track -
    # and because every entry after it reads fine, the failure looks like
    # "this playlist has one bad line" rather than "the file was misdecoded".
    # With a single-entry playlist it vanishes entirely and says nothing.
    _library(tmp_path)
    (tmp_path / "Artist" / "Album" / "Bom.m3u8").write_bytes(
        b"\xef\xbb\xbf01 One.mp3\n")

    pls = _build(tmp_path)["playlists"]

    assert pls[0]["tracks"] == ["Artist/Album/01 One.mp3"]


def test_a_legacy_cp1252_playlist_resolves(tmp_path):
    # `.m3u` is whatever machine wrote it; `.m3u8` is UTF-8 by definition, and
    # that split is why both extensions exist. Decoded as utf-8 with
    # errors="replace", an accented filename became U+FFFD and matched nothing.
    (tmp_path / "A").mkdir()
    name = "Caf\u00e9.mp3"
    (tmp_path / "A" / name).write_bytes(b"\0")
    (tmp_path / "A" / "Legacy.m3u").write_bytes(name.encode("cp1252"))

    pls = _build(tmp_path)["playlists"]

    assert pls[0]["tracks"] == ["A/" + name]


def test_playlist_files_that_resolve_to_nothing_are_counted(tmp_path):
    # THE SILENCE THAT HID BOTH ENCODING DEFECTS. A playlist resolving to
    # nothing is indistinguishable from a library that has no playlists unless
    # something counts it, so the count is the test.
    _library(tmp_path)
    _playlist(tmp_path, "Artist/Album/Dead.m3u", "Gone.mp3\n")
    _playlist(tmp_path, "Artist/Album/Good.m3u", "01 One.mp3\n")

    manifest, _n, _skipped, unresolved = wmm.walk_music(
        str(tmp_path), "/media/music/")

    assert len(manifest["playlists"]) == 1
    assert unresolved == 1


def test_a_symlinked_playlist_is_refused(tmp_path):
    # `rsync -a` preserves links from a share the panel does not own, so a
    # `loop.m3u -> /dev/zero` arrives intact. Following it hangs or OOM-kills
    # the sync unit.
    _library(tmp_path)
    target = tmp_path / "Artist" / "Album" / "Real.m3u"
    target.write_text("01 One.mp3\n", encoding="utf-8")
    link = tmp_path / "Artist" / "Album" / "Link.m3u"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("this platform will not create symlinks unprivileged")

    names = [p["name"] for p in _build(tmp_path)["playlists"]]

    assert names == ["Real"]


def test_an_oversized_playlist_is_refused_whole(tmp_path):
    # Refused rather than truncated: half a playlist is a playlist that has
    # silently lost tracks, which is the failure this file keeps designing out.
    _library(tmp_path)
    body = "01 One.mp3\n" * 8
    padding = "# " + ("x" * (wmm.MAX_PLAYLIST_BYTES)) + "\n"
    _playlist(tmp_path, "Artist/Album/Huge.m3u", body + padding)
    _playlist(tmp_path, "Artist/Album/Small.m3u", "02 Two.mp3\n")

    manifest, _n, _skipped, unresolved = wmm.walk_music(
        str(tmp_path), "/media/music/")

    assert [p["name"] for p in manifest["playlists"]] == ["Small"]
    assert unresolved == 1
