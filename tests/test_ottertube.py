"""The OtterTube downloader's host side and its stack contract (SN-051, SR-113..SR-117).

What runs here, on the dev PC: the curated list's finite-by-construction rules,
the one-item subscription and approved.json it generates, the run's guard
before every item and the indexer, the EDQUOT/ENOSPC stop, the 480p cap, the
titles, thumbnails and pruning, the completion marker, no cleanup of a refused volume, a list reduction
through OtterTube's real indexer and a container outliving its client - with
fake docker, guard and alert - and the
compose/unit/firstboot/bake/Caddy/DNS contract, read from the files.

What does not: the real kernel quota (stack/ottertube/tests/
ottertube-quota.root-test.sh under the hub's kernel in QEMU, TC-1440) and the
real images, clip and site (stack/ottertube/tests/ottertube-lab.sh,
TC-1441..TC-1443).
Implements: TC-1430..TC-1439, TC-1444..TC-1454.
"""

from __future__ import annotations

import copy
import errno
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
OT_DIR = STACK / "ottertube"
for sub in ("ottertube", "scratch", "monitoring"):
    sys.path.insert(0, str(STACK / sub))
import ottertube as ot  # noqa: E402

NASA = "https://images-assets.nasa.gov/video/abc/abc~small.mp4"
BLENDER = "https://video.blender.org/w/x3QKwgFXGZQ7yvCTD2wHJA"


def item(i, **over):
    out = {"id": "item-%d" % i, "title": "Item %d" % i, "url": NASA.replace("abc", "abc%d" % i), "estimateMiB": 10,
           "licence": "Public domain", "source": "NASA", "basis": "https://www.nasa.gov/nasa-brand-center/images-and-media/"}
    out.update(over)
    return out


def a_list(*items, **col):
    c = {"id": "space", "title": "Space", "bands": ["k2", "g34"], "items": list(items) or [item(1)]}
    c.update(col)
    return {"version": 1, "collections": [c]}


def settings(**env):
    base = {"OTTERTUBE_ENABLED": "true"}
    base.update(env)
    return ot.Settings(base)


@pytest.fixture(autouse=True)
def no_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))


# ── TC-1430..TC-1432 the list: finite by construction ────────────────────────


def test_tc1430_a_good_list_is_accepted_with_its_plan():
    data = a_list(item(1), item(2, url=BLENDER, estimateMiB=20, thumb="https://images-assets.nasa.gov/video/a/a~thumb.jpg"))
    plan, problems = ot.validate_list(data, settings())
    assert problems == []
    assert plan == {"collections": 1, "items": 2, "estimate_mib": 30, "budget_mib": 3072}


@pytest.mark.parametrize("url, words", [
    ("https://www.youtube.com/@SomeChannel", "not in OTTERTUBE_ALLOWED_HOSTS"),
    ("http://images-assets.nasa.gov/video/a/a~small.mp4", "not https"),
    ("https://user:pw@images-assets.nasa.gov/video/a/a~small.mp4", "credential"),
    ("https://images-assets.nasa.gov:8443/video/a/a~small.mp4", "port"),
    ("https://images-assets.nasa.gov/video/a/", "not a single video"),
    ("https://images-assets.nasa.gov/search?q=moon", "not a single video"),
    ("https://video.blender.org/c/blender_open_movies/videos", "not a single video"),
    ("https://video.blender.org/w/p/abcdefgh12", "not a single video"),
    ("https://video.blender.org/w/x3QKwgFXGZQ7yvCTD2wHJA#t=10", "fragment"),
    ("", "missing"),
    (None, "missing"),
])
def test_tc1431_anything_but_one_video_on_a_reviewed_host_is_refused(url, words):
    _plan, problems = ot.validate_list(a_list(item(1, url=url)), settings())
    assert any(words in p for p in problems), problems


def test_tc1431_youtube_needs_the_owner_and_then_only_a_single_watch_url():
    yt = {"OTTERTUBE_ALLOWED_HOSTS": "www.youtube.com"}
    assert "not in OTTERTUBE_ALLOWED_HOSTS" in ot.single_item_problem("https://www.youtube.com/watch?v=abcdefghijk", settings().allowed_hosts)
    assert ot.single_item_problem("https://www.youtube.com/watch?v=abcdefghijk", settings(**yt).allowed_hosts) is None
    assert "query" in ot.single_item_problem("https://www.youtube.com/watch?v=abcdefghijk&list=PL1", settings(**yt).allowed_hosts)
    assert "not a single video" in ot.single_item_problem("https://www.youtube.com/playlist?list=PL1", settings(**yt).allowed_hosts)
    assert "not a single video" in ot.single_item_problem("https://www.youtube.com/@channel/videos", settings(**yt).allowed_hosts)


def test_tc1431_an_allowed_host_without_a_single_item_rule_cannot_be_used():
    s = settings(OTTERTUBE_ALLOWED_HOSTS="archive.org")
    assert "no single-item rule" in ot.single_item_problem("https://archive.org/details/x", s.allowed_hosts)


@pytest.mark.parametrize("mutate, words", [
    (lambda d: d["collections"][0]["items"][0].pop("estimateMiB"), "estimateMiB"),
    (lambda d: d["collections"][0]["items"][0].update(estimateMiB=401), "estimateMiB"),
    (lambda d: d["collections"][0]["items"][0].update(estimateMiB=True), "estimateMiB"),
    (lambda d: d["collections"][0]["items"][0].pop("licence"), "licence and source"),
    (lambda d: d["collections"][0]["items"][0].update(basis="http://x.example/terms"), "basis"),
    (lambda d: d["collections"][0]["items"][0].update(thumb="https://evil.example/a.jpg"), "thumb"),
    (lambda d: d["collections"][0]["items"][0].update(thumb="https://images-assets.nasa.gov/a.gif"), "thumb"),
    (lambda d: d["collections"][0]["items"][0].update(id="Bad Id"), "kebab-case"),
    (lambda d: d["collections"][0]["items"][0].update(extra=1), "unknown key"),
    (lambda d: d["collections"][0].update(bands=["teen"]), "bands"),
    (lambda d: d["collections"][0].update(bands=[]), "bands"),
    (lambda d: d["collections"][0].update(order="random"), "order"),
    (lambda d: d["collections"][0].update(kind="podcast"), "kind"),
    (lambda d: d["collections"][0].update(items=[]), "non-empty"),
    (lambda d: d.update(version=2), "version"),
    (lambda d: d.update(collections=[]), "non-empty"),
    (lambda d: d.update(other=1), "unknown top-level key"),
])
def test_tc1432_each_rule_refuses_by_name(mutate, words):
    data = a_list(item(1))
    mutate(data)
    _plan, problems = ot.validate_list(data, settings())
    assert any(words in p for p in problems), problems


def test_tc1432_duplicates_the_budget_and_the_item_cap():
    dup = a_list(item(1), item(1))
    assert any("duplicate item id" in p for p in ot.validate_list(dup, settings())[1])
    two = {"version": 1, "collections": [a_list()["collections"][0], a_list()["collections"][0]]}
    assert any("duplicate collection id" in p for p in ot.validate_list(two, settings())[1])
    big = a_list(*[item(i, estimateMiB=300) for i in range(11)])
    assert any("more than OTTERTUBE_BUDGET_MIB" in p for p in ot.validate_list(big, settings())[1])
    many = a_list(*[item(i, estimateMiB=1) for i in range(5)])
    assert any("OTTERTUBE_MAX_ITEMS" in p for p in ot.validate_list(many, settings(OTTERTUBE_MAX_ITEMS="4"))[1])
    over25 = a_list(*[item(i, estimateMiB=1) for i in range(26)])
    assert any("more than 25 items" in p for p in ot.validate_list(over25, settings())[1])


def test_tc1432_problems_name_ids_never_titles_or_urls():
    secret_title, secret_url = "A Household Title", "https://www.youtube.com/@a-household-channel"
    _plan, problems = ot.validate_list(a_list(item(1, title=secret_title, url=secret_url)), settings())
    assert problems and not any(secret_title in p or "a-household-channel" in p for p in problems)


# ── TC-1433, TC-1434 what the run generates ──────────────────────────────────


def test_tc1433_one_item_one_subscription_one_download_at_most():
    col = a_list()["collections"][0]
    sub = ot.subscription_for(col, item(7), settings())
    assert list(sub) == ["ottertube_video"]
    (name, body), = sub["ottertube_video"].items()
    assert name == "item-7"
    assert body["overrides"] == {"ottertube_url": item(7)["url"], "ottertube_folder": "space", "ottertube_stem": "item-7"}
    assert body["throttle_protection"]["max_downloads_per_subscription"] == {"min": 1, "max": 1}
    assert body["ytdl_options"]["max_filesize"] == 400 * 1024 * 1024
    music = ot.subscription_for(dict(col, kind="music"), item(7), settings())
    assert list(music) == ["ottertube_music"]


def test_tc1433_the_public_ytdl_sub_config_matches_what_the_runner_writes():
    text = (OT_DIR / "ytdl-sub" / "config.yaml").read_text(encoding="utf-8")
    for needle in ("ottertube_video:", "ottertube_music:", 'download: "{ottertube_url}"',
                   '"/library/video/{ottertube_folder}"', '"/library/music/{ottertube_folder}"',
                   'file_name: "{ottertube_stem}.{ext}"', "info_json_name: \"{ottertube_stem}.{info_json_ext}\"",
                   "working_directory: /work/ytdl-sub-working", "lock_directory: /tmp", "logs_directory: /work/logs",
                   "cachedir: false"):
        assert needle in text, needle
    fmt = re.search(r'^\s+format: "(b\[height<=\?480\][^"]+)"', text, re.M).group(1)
    assert all("height<=?480" in alt for alt in fmt.split("/")), fmt
    assert "/lib/" not in text.replace("/library/", "")


def test_tc1434_approved_json_is_one_entry_per_collection_with_the_lists_bands():
    data = {"version": 1, "collections": [
        a_list(item(1))["collections"][0],
        {"id": "songs", "title": " Songs ", "bands": ["toddler"], "kind": "music", "order": "title", "items": [item(2)]}]}
    assert ot.approved_for(data) == {
        "version": 1, "mediaBase": "media/",
        "videos": [{"folder": "space", "id": "space", "title": "Space", "bands": ["k2", "g34"], "order": "oldest"}],
        "music": [{"folder": "songs", "id": "songs", "title": "Songs", "bands": ["toddler"], "order": "title"}]}


OTTERTUBE = Path(os.environ.get("OTTERTUBE_CHECKOUT") or ROOT.parent / "OtterTube")


@pytest.mark.skipif(not (OTTERTUBE / "tools" / "catalog" / "lib.mjs").exists() or not shutil.which("node"),
                    reason="needs the sibling OtterTube checkout and node (the other side of the contract)")
def test_tc1434_otterTubes_own_checkApproved_accepts_what_we_generate(tmp_path):
    data = {"version": 1, "collections": [a_list(item(1))["collections"][0],
                                          {"id": "songs", "title": "Songs", "bands": ["toddler"], "kind": "music", "items": [item(2)]}]}
    path = tmp_path / "approved.json"
    path.write_text(json.dumps(ot.approved_for(data)))
    lib = (OTTERTUBE / "tools" / "catalog" / "lib.mjs").as_uri()
    script = "import(%r).then(m => { m.readApproved(process.argv[1]); console.log('ok'); })" % lib
    res = subprocess.run(["node", "-e", script, str(path)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0 and res.stdout.strip() == "ok", res.stderr


def test_tc1436_output_classification():
    assert ot.classify_output("ERROR: unable to write data: [Errno 122] Disk quota exceeded") == "quota"
    assert ot.classify_output("OSError: [Errno 28] No space left on device: '/work/x'") == "space"
    assert ot.classify_output("ERROR: Requested format is not available") is None


# ── TC-1435..TC-1439 the run, with fakes ─────────────────────────────────────


class Rig:
    """A Runner over a temp tree with fake guard, docker, alert, sleep and fetch."""

    def __init__(self, tmp_path, data, guard_results=None, env=None, sizes=None, heights=None, outputs=None,
                 orphans=(), stuck=False, indexer=None):
        self.tmp = tmp_path
        self.paths = ot.Paths(str(tmp_path / "root"), str(tmp_path / "stack"))
        os.makedirs(os.path.dirname(self.paths.list_file), exist_ok=True)  # a second Rig reuses the tree
        with open(self.paths.list_file, "w") as handle:
            json.dump(data, handle)
        self.guard_results = list(guard_results or [])
        self.guard_calls = 0
        self.calls, self.alerts, self.sleeps, self.fetches = [], [], [], []
        self.heights = heights or {}
        self.outputs = outputs or {}
        # The daemon's side: containers that exist. `orphans` are items whose
        # client times out (124) while their container keeps running; `stuck`
        # makes `docker rm -f` leave every container in place.
        self.containers, self.orphans, self.stuck = set(), set(orphans), stuck
        self.indexer = indexer  # None: a fake success; "real": OtterTube's own build-catalog.mjs
        env = dict({"OTTERTUBE_ENABLED": "true", "OTTERTUBE_PAUSE_SEC": "10"}, **(env or {}))
        self.runner = ot.Runner(ot.Settings(env), self.paths, None, "/nonexistent",
                                guard=self.guard, docker=self.docker,
                                alert=lambda k, t, b: self.alerts.append(k), sleep=self.sleeps.append,
                                fetch=self.fetch, clock=lambda: 1000.0)

    def guard(self):
        self.guard_calls += 1
        return self.guard_results.pop(0) if self.guard_results else (True, "ok")

    def fetch(self, url, stem):
        self.fetches.append(url)
        with open(stem + ".jpg", "wb") as handle:
            handle.write(b"jpg")
        return stem + ".jpg"

    def docker(self, argv, timeout):
        self.calls.append(argv)
        if argv[1] == "rm":
            if not self.stuck:
                self.containers.discard(argv[-1])
            return 0, ""
        if argv[1] == "inspect":
            return (0, "running") if argv[-1] in self.containers else (1, "Error: No such object: %s" % argv[-1])
        name = argv[argv.index("--name") + 1]
        if name in self.containers:
            return 125, 'docker: Error response from daemon: Conflict. The container name "/%s" is already in use.' % name
        if "ottertube-indexer" in argv:
            return self.real_indexer(argv) if self.indexer == "real" else (0, "written: /catalog/catalog.json")
        if "--entrypoint" in argv:
            stem = os.path.basename(argv[-1]).split(".")[0]
            return 0, "%d\n" % self.heights.get(stem, 360)
        sub = json.load(open(os.path.join(self.paths.work, "sub.json")))
        (preset, subs), = sub.items()
        (name, body), = subs.items()
        if name in self.outputs:
            return self.outputs[name]
        kind = "music" if preset == "ottertube_music" else "video"
        folder = os.path.join(self.paths.kind_root(kind), body["overrides"]["ottertube_folder"])
        os.makedirs(folder, exist_ok=True)
        stem = body["overrides"]["ottertube_stem"]
        open(os.path.join(folder, stem + (".m4a" if kind == "music" else ".mp4")), "wb").write(b"x")
        json.dump({"id": stem + "~small", "title": stem + "~small"}, open(os.path.join(folder, stem + ".info.json"), "w"))
        open(os.path.join(folder, ".ytdl-sub-%s-download-archive.json" % stem), "w").write("{}")
        if stem in self.orphans:
            self.containers.add(ot.DL_CONTAINER)  # the client gave up; the daemon's container did not
            return 124, "(stopped after %ds)" % timeout
        return 0, "[ytdl-sub] Download Summary: +1"

    def real_indexer(self, argv):
        """OtterTube's own indexer on the host, its container paths mapped onto the temp tree."""
        tail = argv[argv.index("ottertube-indexer") + 1:]
        assert tail[:2] == ["node", "tools/catalog/build-catalog.mjs"]
        where = {"/work": self.paths.work, "/library": self.paths.lib, "/catalog": self.paths.catalog}

        def host(arg):
            for prefix, real in where.items():
                if arg == prefix or arg.startswith(prefix + "/"):
                    return real + arg[len(prefix):].replace("/", os.sep)
            return arg
        cmd = ["node", str(OTTERTUBE / "tools" / "catalog" / "build-catalog.mjs")] + [host(a) for a in tail[2:]]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return res.returncode, res.stdout + res.stderr

    def downloads(self):
        return [c for c in self.calls if "ottertube-ytdl-sub" in c and "--entrypoint" not in c]

    def last_run(self):
        return json.load(open(os.path.join(self.paths.logs, "last-run.json")))


def test_tc1435_the_guard_runs_before_the_run_every_item_and_the_indexer(tmp_path):
    rig = Rig(tmp_path, a_list(item(1), item(2)))
    assert rig.runner.run() == 0
    # run, item-1 and its metadata, item-2 and its metadata, approved.json, the
    # indexer, and the cleanup of work/ after the run (LLR-1283)
    assert rig.guard_calls == 8
    assert len(rig.downloads()) == 2 and any("ottertube-indexer" in c for c in rig.calls)


def test_tc1435_a_refusal_before_the_run_starts_nothing(tmp_path):
    rig = Rig(tmp_path, a_list(item(1)), guard_results=[(False, "/srv/scratch is not a mount point")])
    assert rig.runner.run() == ot.EXIT_GUARD
    assert rig.calls == [] and rig.alerts == ["ottertube-guard"]
    assert not os.path.exists(rig.paths.base)


def test_tc1435_a_refusal_before_the_second_item_stops_there(tmp_path):
    rig = Rig(tmp_path, a_list(item(1), item(2)), guard_results=[(True, "ok"), (True, "ok"), (True, "ok"), (False, "wrong device")])
    assert rig.runner.run() == ot.EXIT_GUARD
    assert len(rig.downloads()) == 1 and not any("ottertube-indexer" in c for c in rig.calls)
    # The refused volume is not touched again: item-1's subscription stays in
    # work/ until the next run's guard has passed (LLR-1283).
    assert os.listdir(rig.paths.work) == ["sub.json"]


def test_tc1435_a_refusal_after_the_container_writes_no_metadata(tmp_path):
    rig = Rig(tmp_path, a_list(item(1, thumb="https://images-assets.nasa.gov/video/a/a~thumb.jpg")),
              guard_results=[(True, "ok"), (True, "ok"), (False, "not a mount point")])
    assert rig.runner.run() == ot.EXIT_GUARD
    assert rig.fetches == [] and not any("--entrypoint" in c for c in rig.calls)
    info = json.load(open(os.path.join(rig.paths.kind_root("video"), "space", "item-1.info.json")))
    assert info["title"] == "item-1~small"


def test_tc1436_edquot_in_the_output_stops_the_run_empties_work_and_alerts(tmp_path):
    out = (1, "[ytdl-sub:yt-dlp] ERROR: unable to write data: [Errno 122] Disk quota exceeded")
    rig = Rig(tmp_path, a_list(item(1), item(2), item(3)), outputs={"item-2": out})
    os.makedirs(rig.paths.work)
    open(os.path.join(rig.paths.work, "leftover.part"), "w").write("x")
    assert rig.runner.run() == ot.EXIT_QUOTA
    assert [c[-1] for c in rig.downloads()] == ["/work/sub.json"] * 2  # item-3 never attempted
    assert os.listdir(rig.paths.work) == [] and rig.alerts == ["ottertube-quota"]
    assert rig.last_run()["result"] == "quota" and "EDQUOT" in rig.last_run()["reason"]
    assert not any("ottertube-indexer" in c for c in rig.calls)


def test_tc1436_enospc_too(tmp_path):
    rig = Rig(tmp_path, a_list(item(1)), outputs={"item-1": (1, "OSError: [Errno 28] No space left on device")})
    assert rig.runner.run() == ot.EXIT_QUOTA and rig.alerts == ["ottertube-quota"]


def test_tc1436_an_edquot_raised_here_stops_the_same_way(tmp_path, monkeypatch):
    rig = Rig(tmp_path, a_list(item(1)))
    real = ot.write_json_atomic

    def full(path, data):
        if path.endswith("approved.json"):
            raise OSError(errno.EDQUOT, "Disk quota exceeded")
        return real(path, data)
    monkeypatch.setattr(ot, "write_json_atomic", full)
    assert rig.runner.run() == ot.EXIT_QUOTA and rig.alerts == ["ottertube-quota"]


def test_tc1436_any_other_os_error_is_not_mistaken_for_a_full_budget(tmp_path, monkeypatch):
    rig = Rig(tmp_path, a_list(item(1)))
    monkeypatch.setattr(ot, "approved_for", lambda data: (_ for _ in ()).throw(OSError(errno.EIO, "I/O error")))
    with pytest.raises(OSError):
        rig.runner.run()
    assert rig.alerts == []


def test_tc1437_present_items_are_not_fetched_and_the_pause_is_only_between_downloads(tmp_path):
    data = a_list(item(1), item(2), item(3))
    rig = Rig(tmp_path, data)
    assert rig.runner.run() == 0
    assert len(rig.sleeps) == 2 and all(5 <= s <= 15 for s in rig.sleeps)
    again = Rig(tmp_path / "again", data)
    shutil.copytree(rig.paths.base, again.paths.base)
    assert again.runner.run() == 0
    assert again.downloads() == [] and again.sleeps == [] and again.runner.counts["present"] == 3


def test_tc1438_a_failed_item_does_not_stop_the_others_and_is_reported(tmp_path, capsys):
    rig = Rig(tmp_path, a_list(item(1), item(2)), outputs={"item-1": (1, "ERROR: [generic] Requested format is not available")})
    assert rig.runner.run() == ot.EXIT_FAILED
    assert len(rig.downloads()) == 2 and rig.alerts == ["ottertube-items"]
    assert rig.last_run()["items"] == {"present": 0, "downloaded": 1, "failed": 1, "pruned": 0}
    assert "Requested format is not available" in capsys.readouterr().out


def test_tc1438_a_video_over_480p_is_discarded(tmp_path):
    rig = Rig(tmp_path, a_list(item(1), item(2)), heights={"item-1": 720})
    assert rig.runner.run() == ot.EXIT_FAILED
    folder = os.path.join(rig.paths.kind_root("video"), "space")
    assert sorted(os.listdir(folder)) == [".ytdl-sub-item-2-download-archive.json", "item-2.info.json", "item-2.mp4", "item-2.ok"]


def test_tc1438_an_unreadable_height_is_discarded_too(tmp_path):
    rig = Rig(tmp_path, a_list(item(1)))
    rig.heights = {"item-1": 0}
    rig.docker_real = rig.docker

    def docker(argv, timeout):
        if "--entrypoint" in argv:
            return 1, "error"
        return rig.docker_real(argv, timeout)
    rig.runner.docker = docker
    assert rig.runner.run() == ot.EXIT_FAILED


def test_tc1439_titles_come_from_the_list_and_a_thumbnail_is_fetched_once(tmp_path):
    rig = Rig(tmp_path, a_list(item(1, title="Moon Phases", thumb="https://images-assets.nasa.gov/video/a/a~thumb.jpg")))
    assert rig.runner.run() == 0
    info = json.load(open(os.path.join(rig.paths.kind_root("video"), "space", "item-1.info.json")))
    assert info["title"] == info["fulltitle"] == "Moon Phases"
    assert rig.fetches == ["https://images-assets.nasa.gov/video/a/a~thumb.jpg"]


def test_tc1439_what_the_list_no_longer_names_is_pruned(tmp_path):
    rig = Rig(tmp_path, a_list(item(1), item(2)))
    assert rig.runner.run() == 0
    shrunk = Rig(tmp_path / "next", a_list(item(2)))
    shutil.copytree(rig.paths.base, shrunk.paths.base)
    gone = os.path.join(shrunk.paths.kind_root("video"), "old-collection")
    os.makedirs(gone)
    open(os.path.join(gone, "x.mp4"), "w").write("x")
    assert shrunk.runner.run() == 0
    folder = os.path.join(shrunk.paths.kind_root("video"), "space")
    assert sorted(os.listdir(folder)) == [".ytdl-sub-item-2-download-archive.json", "item-2.info.json", "item-2.mp4", "item-2.ok"]
    assert not os.path.exists(gone) and shrunk.runner.counts["pruned"] == 5  # 4 files of item-1 (its .ok too) + the folder


def test_tc1439_prune_never_follows_a_link(tmp_path):
    lib = tmp_path / "lib"
    (lib / "video" / "space").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    try:
        os.symlink(str(outside), str(lib / "video" / "linked"))
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    plan = ot.prune_plan(str(lib), a_list(item(1)))
    assert plan == [str(lib / "video" / "linked")]


def test_tc1435_an_invalid_list_refuses_with_an_alert_and_writes_nothing(tmp_path):
    rig = Rig(tmp_path, a_list(item(1, url="https://www.youtube.com/@x")))
    assert rig.runner.run() == ot.EXIT_INVALID
    assert rig.calls == [] and rig.alerts == ["ottertube-list"]


def test_tc1435_a_missing_list_says_where_it_comes_from(tmp_path, capsys):
    rig = Rig(tmp_path, a_list())
    os.unlink(rig.paths.list_file)
    assert rig.runner.run() == ot.EXIT_INVALID
    assert "HomeHub scripts/deploy/ottertube/list.json" in capsys.readouterr().out


def test_tc1435_the_indexer_publishes_by_hard_link_from_the_generated_approved_json(tmp_path):
    rig = Rig(tmp_path, a_list(item(1)))
    assert rig.runner.run() == 0
    idx = next(c for c in rig.calls if "ottertube-indexer" in c)
    tail = idx[idx.index("ottertube-indexer") + 1:]
    assert tail[:2] == ["node", "tools/catalog/build-catalog.mjs"]
    for flag in ("--require-hardlink", "--allow-missing"):
        assert flag in tail
    assert tail[tail.index("--approved") + 1] == "/work/approved.json"
    assert tail[tail.index("--publish") + 1] == "/library/publish"
    assert "--project-directory" in idx and idx[idx.index("--profile") + 1] == "ottertube"


# ── TC-1454 a refused volume is never cleaned up (LLR-1283) ─────────────────


SENTINEL = "sentinel.part"


def test_tc1454_a_refusal_at_the_start_leaves_an_existing_work_tree_alone(tmp_path):
    rig = Rig(tmp_path, a_list(item(1)), guard_results=[(False, "wrong device")])
    os.makedirs(os.path.join(rig.paths.work, "ytdl"))
    open(os.path.join(rig.paths.work, SENTINEL), "w").write("x")
    open(os.path.join(rig.paths.work, "ytdl", "frag.part"), "w").write("x")
    assert rig.runner.run() == ot.EXIT_GUARD
    assert rig.calls == [] and rig.alerts == ["ottertube-guard"]
    assert os.path.exists(os.path.join(rig.paths.work, SENTINEL))
    assert os.path.exists(os.path.join(rig.paths.work, "ytdl", "frag.part"))
    assert rig.guard_calls == 1 and not os.path.exists(os.path.join(rig.paths.logs, "last-run.json"))


def test_tc1454_a_refusal_mid_run_before_an_item_leaves_work_alone(tmp_path):
    # The start-of-run cleanup passed its guard; then the volume changes under
    # the run (a remount, a different device) and the guard before item-1
    # refuses. What is in work/ now is on the refused filesystem.
    rig = Rig(tmp_path, a_list(item(1)))
    calls = []

    def guard():
        calls.append(1)
        if len(calls) == 1:
            return True, "ok"
        open(os.path.join(rig.paths.work, SENTINEL), "w").write("x")
        return False, "wrong device"
    rig.runner.guard = guard
    assert rig.runner.run() == ot.EXIT_GUARD
    assert rig.calls == [] and rig.alerts == ["ottertube-guard"]  # no container started
    assert os.path.exists(os.path.join(rig.paths.work, SENTINEL))
    assert len(calls) == 2  # nothing asks the refused volume again


def test_tc1454_the_cleanup_after_a_good_run_asks_the_guard_first(tmp_path):
    rig = Rig(tmp_path, a_list(item(1)), guard_results=[(True, "ok")] * 5 + [(False, "not a mount point")])
    assert rig.runner.run() == 0
    assert rig.guard_calls == 6 and sorted(os.listdir(rig.paths.work)) == ["approved.json", "sub.json"]


def test_tc1454_the_quota_stop_cleans_only_where_the_guard_still_agrees(tmp_path):
    out = (1, "[ytdl-sub:yt-dlp] ERROR: unable to write data: [Errno 122] Disk quota exceeded")
    rig = Rig(tmp_path, a_list(item(1)), outputs={"item-1": out},
              guard_results=[(True, "ok"), (True, "ok"), (False, "wrong device")])
    assert rig.runner.run() == ot.EXIT_QUOTA and rig.alerts == ["ottertube-quota"]
    assert os.listdir(rig.paths.work) == ["sub.json"] and rig.guard_calls == 3
    assert not os.path.exists(os.path.join(rig.paths.logs, "last-run.json"))  # logs/ is on that volume too


def scratch_facts(**over):
    facts = dict(writer="ottertube", known_writer=True, expected_uuid="0f0e0d0c-0b0a-4908-8706-050403020100",
                 uuid_malformed=False, mounted=True, mount_majmin="253:3", uuid_majmin="253:3", fstype="ext4",
                 superopts="rw,prjquota", read_only=False, has_cap_sys_resource=False, dir_kind="dir",
                 dir_dev_ok=True, project_id=ot.scratch.PROJECT_ID["ottertube"], project_inherit=True, probe_error=None)
    facts.update(over)
    return facts


@pytest.mark.parametrize("over, ok, words", [
    ({}, True, "ok"),
    # The budget is full: the guard refuses a writer, but the volume is still ours to clean.
    ({"probe_error": "EDQUOT"}, True, "ok"),
    ({"probe_error": "ENOSPC"}, True, "ok"),
    ({"probe_error": "EIO"}, False, "not writable (EIO)"),
    ({"uuid_majmin": "8:1"}, False, "wrong device"),
    ({"mounted": False}, False, "not a mount point"),
    ({"dir_dev_ok": False}, False, "not on the scratch filesystem"),
    ({"project_id": 7101}, False, "quota project"),
])
def test_tc1454_the_cleanup_asks_identity_not_writability(monkeypatch, over, ok, words):
    monkeypatch.setattr(ot.scratch, "gather_guard_facts", lambda writer, s, paths: scratch_facts(**over))
    got = ot.scratch_identity(None, "/")
    assert got[0] is ok and words in got[1]
    # ... while the writer guard itself still refuses a full budget
    if over.get("probe_error") in ("EDQUOT", "ENOSPC"):
        assert ot.scratch.judge_guard(scratch_facts(**over))[0] is False


def test_tc1454_a_full_budget_still_frees_work(tmp_path):
    # The real guard's write probe fails at the quota; the cleanup's identity check passes.
    out = (1, "[ytdl-sub:yt-dlp] ERROR: unable to write data: [Errno 122] Disk quota exceeded")
    rig = Rig(tmp_path, a_list(item(1)), outputs={"item-1": out},
              guard_results=[(True, "ok"), (True, "ok"), (False, "/srv/scratch/ottertube is not writable (EDQUOT)")])
    rig.runner.identity = lambda: (True, "ok")
    assert rig.runner.run() == ot.EXIT_QUOTA
    assert os.listdir(rig.paths.work) == [] and rig.last_run()["result"] == "quota"


def test_tc1454_the_quota_stop_still_empties_work_when_the_guard_agrees(tmp_path):
    out = (1, "[ytdl-sub:yt-dlp] ERROR: unable to write data: [Errno 122] Disk quota exceeded")
    rig = Rig(tmp_path, a_list(item(1)), outputs={"item-1": out})
    assert rig.runner.run() == ot.EXIT_QUOTA
    assert os.listdir(rig.paths.work) == [] and rig.guard_calls == 3  # run, item-1, the quota cleanup; not again
    assert rig.last_run()["result"] == "quota"


# ── TC-1451 an item is complete only with its marker (LLR-1280) ─────────────

THUMB = "https://images-assets.nasa.gov/video/a/a~thumb.jpg"


def interrupted(tmp_path):
    """A first run whose guard refuses after item-1's container wrote its files:
    exit 76, the media and info.json on the volume, no probe, no marker."""
    rig = Rig(tmp_path / "first", a_list(item(1, title="Moon Phases", thumb=THUMB)),
              guard_results=[(True, "ok"), (True, "ok"), (False, "not a mount point")])
    assert rig.runner.run() == ot.EXIT_GUARD
    folder = os.path.join(rig.paths.kind_root("video"), "space")
    assert sorted(os.listdir(folder)) == [".ytdl-sub-item-1-download-archive.json", "item-1.info.json", "item-1.mp4"]
    return rig


def test_tc1451_files_left_by_an_interrupted_run_are_checked_before_they_count(tmp_path):
    first = interrupted(tmp_path)
    again = Rig(tmp_path / "again", a_list(item(1, title="Moon Phases", thumb=THUMB)))
    shutil.copytree(first.paths.base, again.paths.base)
    assert again.runner.run() == 0
    probes = [c for c in again.calls if "--entrypoint" in c]
    assert len(probes) == 1 and probes[0][-1] == "/library/video/space/item-1.mp4"
    assert again.downloads() == [] and again.runner.counts["present"] == 0  # checked in place, not counted present
    folder = os.path.join(again.paths.kind_root("video"), "space")
    assert json.load(open(os.path.join(folder, "item-1.info.json")))["title"] == "Moon Phases"
    assert again.fetches == [THUMB]
    assert json.load(open(os.path.join(folder, "item-1.ok"))) == {"schema": 1, "height": 360}
    third = Rig(tmp_path / "third", a_list(item(1, title="Moon Phases", thumb=THUMB)))
    shutil.copytree(again.paths.base, third.paths.base)
    assert third.runner.run() == 0 and third.runner.counts["present"] == 1
    assert not any("ottertube-ytdl-sub" in c for c in third.calls)


def test_tc1451_an_interrupted_item_over_480p_is_discarded_on_the_next_run(tmp_path):
    first = interrupted(tmp_path)
    again = Rig(tmp_path / "again", a_list(item(1, title="Moon Phases", thumb=THUMB)), heights={"item-1": 720})
    shutil.copytree(first.paths.base, again.paths.base)
    assert again.runner.run() == ot.EXIT_FAILED
    assert again.runner.counts == {"present": 0, "downloaded": 0, "failed": 1, "pruned": 0}
    assert os.listdir(os.path.join(again.paths.kind_root("video"), "space")) == []


def test_tc1451_a_partial_leftover_is_removed_and_fetched_afresh(tmp_path):
    rig = Rig(tmp_path, a_list(item(1)))
    folder = os.path.join(rig.paths.kind_root("video"), "space")
    os.makedirs(folder)
    # A download archive alone would make ytdl-sub skip the item; media without info.json is not an item.
    for name in (".ytdl-sub-item-1-download-archive.json", "item-1.mp4"):
        open(os.path.join(folder, name), "w").write("{}")
    seen = []
    real = rig.docker

    def docker(argv, timeout):
        if "sub" in argv and "--entrypoint" not in argv:
            seen.append(sorted(os.listdir(folder)))
        return real(argv, timeout)
    rig.runner.docker = docker
    assert rig.runner.run() == 0
    assert seen == [[]] and os.path.exists(os.path.join(folder, "item-1.ok"))


def test_tc1451_a_failed_container_leaves_nothing_for_the_indexer(tmp_path):
    rig = Rig(tmp_path, a_list(item(1)))
    real = rig.docker

    def docker(argv, timeout):
        rc, out = real(argv, timeout)
        if "sub" in argv and "--entrypoint" not in argv:
            return 1, "ERROR: postprocessing failed"  # the files were written, the exit says otherwise
        return rc, out
    rig.runner.docker = docker
    assert rig.runner.run() == ot.EXIT_FAILED
    assert os.listdir(os.path.join(rig.paths.kind_root("video"), "space")) == []


def test_tc1451_the_marker_is_kept_with_its_item_and_pruned_with_it():
    assert ot.file_stem("item-1" + ot.DONE_EXT) == "item-1"


# ── TC-1452 a list reduction reaches the catalogue (LLR-1281) ────────────────


def write_catalog(paths, n):
    os.makedirs(paths.catalog, exist_ok=True)
    ot.write_json_atomic(os.path.join(paths.catalog, "catalog.json"),
                         {"videos": [{"id": "space", "items": [{"id": "v%d" % i} for i in range(n)]}],
                          "music": [{"id": "songs", "tracks": []}]})


@pytest.mark.parametrize("before, listed, complete, allow", [
    (0, 1, 1, False),    # no catalogue yet
    (3, 3, 0, False),    # the list did not shrink: downloads went missing, protection stays
    (3, 4, 4, False),    # the list grew
    (3, 1, 1, True),     # the Owner cut 3 to 1
    (10, 6, 3, True),    # cut to 6, half of them complete: within the indexer's own rule
    (10, 6, 2, False),   # cut to 6 but most of them missing: protection stays
])
def test_tc1452_allow_shrink_only_when_the_list_explains_it(tmp_path, before, listed, complete, allow):
    rig = Rig(tmp_path, a_list(item(1)))
    if before:
        write_catalog(rig.paths, before)
    assert rig.runner.shrink_explained(listed, complete) is allow


def test_tc1452_catalog_items_counts_as_the_indexer_does(tmp_path):
    paths = ot.Paths(str(tmp_path), str(tmp_path / "stack"))
    write_catalog(paths, 4)
    assert ot.catalog_items(os.path.join(paths.catalog, "catalog.json")) == 4
    assert ot.catalog_items(os.path.join(paths.catalog, "absent.json")) == 0
    (Path(paths.catalog) / "bad.json").write_text("{")
    assert ot.catalog_items(os.path.join(paths.catalog, "bad.json")) == 0


needs_indexer = pytest.mark.skipif(
    not (OTTERTUBE / "tools" / "catalog" / "build-catalog.mjs").exists() or not shutil.which("node"),
    reason="needs the sibling OtterTube checkout and node (the other side of the contract)")


def published_state(rig):
    return json.load(open(os.path.join(rig.paths.publish, ".publish-state.json")))["files"]


@needs_indexer
def test_tc1452_the_real_indexer_takes_a_list_cut_from_3_to_1(tmp_path):
    rig = Rig(tmp_path, a_list(item(1), item(2), item(3)), indexer="real")
    assert rig.runner.run() == 0, rig.calls[-1]
    catalog = os.path.join(rig.paths.catalog, "catalog.json")
    assert ot.catalog_items(catalog) == 3
    assert len([k for k in published_state(rig) if not k.endswith("-thumb.jpg")]) == 3
    cut = Rig(tmp_path, a_list(item(2)), indexer="real")
    assert cut.runner.run() == 0
    assert "--allow-shrink" in next(c for c in cut.calls if "ottertube-indexer" in c)
    assert ot.catalog_items(catalog) == 1
    state = published_state(cut)
    kept = [k for k, e in state.items() if "unreferencedSince" not in e]
    marked = [k for k, e in state.items() if "unreferencedSince" in e]
    assert len(kept) == 1 and len(marked) == 2  # the two removed items wait out the grace period, unreferenced
    assert json.load(open(catalog))["videos"][0]["items"][0]["file"].endswith(kept[0].split("/")[1])


@needs_indexer
def test_tc1452_the_real_indexer_still_refuses_downloads_that_went_missing(tmp_path, capsys):
    rig = Rig(tmp_path, a_list(item(1), item(2), item(3), item(4)), indexer="real")
    assert rig.runner.run() == 0
    catalog = os.path.join(rig.paths.catalog, "catalog.json")
    # The list is cut to 2, and both of those vanish and fail to download again.
    for name in os.listdir(os.path.join(rig.paths.kind_root("video"), "space")):
        os.unlink(os.path.join(rig.paths.kind_root("video"), "space", name))
    fail = (1, "ERROR: unable to download")
    cut = Rig(tmp_path, a_list(item(1), item(2)), indexer="real", outputs={"item-1": fail, "item-2": fail})
    assert cut.runner.run() == ot.EXIT_FAILED
    assert "--allow-shrink" not in next(c for c in cut.calls if "ottertube-indexer" in c)
    assert cut.last_run()["indexer_exit"] == 1 and ot.catalog_items(catalog) == 4
    assert "the catalogue would shrink from 4 to 0 videos and songs: nothing written" in capsys.readouterr().out


# ── TC-1453 a container outliving its client (LLR-1282) ──────────────────────


def test_tc1453_a_timed_out_client_s_container_is_removed_before_the_next_item(tmp_path):
    rig = Rig(tmp_path, a_list(item(1), item(2)), orphans={"item-1"})
    assert rig.runner.run() == ot.EXIT_FAILED
    assert rig.runner.counts == {"present": 0, "downloaded": 1, "failed": 1, "pruned": 0}
    i_rm = next(i for i, c in enumerate(rig.calls) if c[1:3] == ["rm", "-f"])
    i_next = [i for i, c in enumerate(rig.calls) if "sub" in c and "--entrypoint" not in c][1]
    assert rig.calls[i_rm][-1] == ot.DL_CONTAINER and i_rm < i_next
    assert rig.containers == set()
    folder = os.path.join(rig.paths.kind_root("video"), "space")
    assert not any(n.startswith("item-1") for n in os.listdir(folder))  # the timed-out item's files went too


def test_tc1453_a_container_that_will_not_go_stops_the_run(tmp_path):
    rig = Rig(tmp_path, a_list(item(1), item(2)), orphans={"item-1"}, stuck=True)
    os.makedirs(rig.paths.work)
    assert rig.runner.run() == ot.EXIT_FAILED
    assert len(rig.downloads()) == 1 and not any("ottertube-indexer" in c for c in rig.calls)
    assert len([c for c in rig.calls if c[1:3] == ["rm", "-f"]]) == 3
    assert rig.alerts == ["ottertube-container"] and rig.last_run()["result"] == "failed"
    assert "could not be confirmed gone" in rig.last_run()["reason"]
    assert os.listdir(rig.paths.work) != []  # never emptied under a container that may still write


def test_tc1453_the_indexer_and_the_probe_are_reaped_too(tmp_path):
    rig = Rig(tmp_path, a_list(item(1)))
    real = rig.docker

    def docker(argv, timeout):
        if ("ottertube-indexer" in argv or "--entrypoint" in argv) and "--name" in argv:
            rig.calls.append(argv)
            rig.containers.add(argv[argv.index("--name") + 1])
            return 124, "(stopped)"
        return real(argv, timeout)
    rig.runner.docker = docker
    assert rig.runner.run() == ot.EXIT_FAILED
    removed = [c[-1] for c in rig.calls if c[1:3] == ["rm", "-f"]]
    assert removed == [ot.DL_CONTAINER, ot.INDEX_CONTAINER] and rig.containers == set()


FAKE_CLIENT = r'''
import json, os, subprocess, sys, time
state, args = os.environ["FAKE_DOCKER_STATE"], sys.argv[1:]
beat = os.path.join(state, "heartbeat")
pidfile = lambda name: os.path.join(state, name + ".pid")
def log(line):
    with open(os.path.join(state, "client.log"), "a") as h:
        h.write(line + "\n")
if args[0] == "rm":
    p = pidfile(args[-1])
    if os.path.exists(p):
        alive = os.path.exists(beat) and time.time() - os.path.getmtime(beat) < 1.0
        log("rm %s alive=%s" % (args[-1], alive))
        try:
            os.kill(int(open(p).read()), 15)
        except OSError:
            pass
        time.sleep(0.3)
        os.unlink(p)
    sys.exit(0)
if args[0] == "inspect":
    if os.path.exists(pidfile(args[-1])):
        print("running"); sys.exit(0)
    print("Error: No such object: " + args[-1]); sys.exit(1)
name = args[args.index("--name") + 1]
if os.path.exists(pidfile(name)):
    print('Conflict. The container name "/%s" is already in use' % name); sys.exit(125)
if "ottertube-indexer" in args:
    sys.exit(0)
if "--entrypoint" in args:
    print("360"); sys.exit(0)
sub = json.load(open(os.path.join(os.environ["FAKE_DOCKER_WORK"], "sub.json")))
body = list(list(sub.values())[0].values())[0]["overrides"]
folder = os.path.join(os.environ["FAKE_DOCKER_LIB"], "video", body["ottertube_folder"])
os.makedirs(folder, exist_ok=True)
stem = body["ottertube_stem"]
open(os.path.join(folder, stem + ".mp4"), "wb").write(b"x")
json.dump({"id": stem, "title": stem}, open(os.path.join(folder, stem + ".info.json"), "w"))
log("run " + stem)
if stem == "item-1":
    # The "container": a process of its own, outside the client's process group,
    # as a daemon-owned container is. It keeps writing after the client dies.
    flags = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000008 | 0x00000200}
    code = "import time\nwhile True:\n    open(%r, 'a').write('.')\n    time.sleep(0.1)\n" % beat
    child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, **flags)
    open(pidfile(name), "w").write(str(child.pid))
    time.sleep(120)
sys.exit(0)
'''


def test_tc1453_a_real_container_process_outlives_its_killed_client_and_is_reaped(tmp_path, monkeypatch):
    """Through the real run_bounded: the client is killed at the timeout, the
    separate 'container' process keeps writing, and the runner removes it before
    item-2 starts (which would otherwise hit the fixed --name)."""
    rig = Rig(tmp_path, a_list(item(1), item(2)))
    state = tmp_path / "daemon"
    state.mkdir()
    client = tmp_path / "fake_docker.py"
    client.write_text(FAKE_CLIENT)
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    monkeypatch.setenv("FAKE_DOCKER_WORK", rig.paths.work)
    monkeypatch.setenv("FAKE_DOCKER_LIB", rig.paths.lib)
    rig.runner.docker = rig.runner._docker
    rig.runner.docker_bin = [sys.executable, str(client)]
    rig.runner.s.item_timeout = 3
    try:
        assert rig.runner.run() == ot.EXIT_FAILED
        log = (state / "client.log").read_text().splitlines()
        assert log == ["run item-1", "rm %s alive=True" % ot.DL_CONTAINER, "run item-2"], log
        assert rig.runner.counts["downloaded"] == 1 and rig.runner.counts["failed"] == 1
        size = (state / "heartbeat").stat().st_size
        import time
        time.sleep(0.6)
        assert (state / "heartbeat").stat().st_size == size, "the container process is still writing"
    finally:
        pid = state / (ot.DL_CONTAINER + ".pid")
        if pid.exists():
            try:
                os.kill(int(pid.read_text()), 15)
            except OSError:
                pass


# ── TC-1449, TC-1450 serve-check, status, docker, the CLI ────────────────────


def test_tc1449_serve_check_needs_the_guard_and_both_sources(tmp_path):
    paths = ot.Paths(str(tmp_path), str(tmp_path / "stack"))
    assert ot.serve_check(paths, lambda: (False, "not a mount point")) == (False, "scratch guard: not a mount point")
    ok, reason = ot.serve_check(paths, lambda: (True, "ok"))
    assert not ok and "does not exist yet" in reason
    os.makedirs(paths.publish)
    os.makedirs(paths.catalog)
    assert ot.serve_check(paths, lambda: (True, "ok")) == (True, "ok")


def test_tc1449_status_counts_a_hard_link_once(tmp_path):
    paths = ot.Paths(str(tmp_path), str(tmp_path / "stack"))
    for d in paths.dirs():
        os.makedirs(d, exist_ok=True)
    src = os.path.join(paths.kind_root("video"), "a.mp4")
    with open(src, "wb") as handle:
        handle.write(b"\0" * (3 * 1024 * 1024))
    try:
        os.link(src, os.path.join(paths.publish, "a.mp4"))
    except OSError:
        pytest.skip("no hard links here")
    ot.write_json_atomic(os.path.join(paths.logs, "last-run.json"), {"finished": 900, "result": "ok"})
    st = ot.status(paths, clock=lambda: 1000.0)
    assert st["lib_mib"] == 3 and st["last_run_age_sec"] == 100 and st["catalog_age_sec"] is None


def test_tc1449_the_docker_shell_maps_exit_timeout_and_a_missing_binary(tmp_path):
    r = ot.Runner(settings(), ot.Paths(str(tmp_path)), None, "/nonexistent", guard=lambda: (True, "ok"))
    rc, out = r._docker([sys.executable, "-c", "print('hello'); raise SystemExit(3)"], 30)
    assert rc == 3 and "hello" in out
    rc, out = r._docker([sys.executable, "-c", "import time; time.sleep(30)"], 1)
    assert rc == 124 and "stopped after" in out
    rc, out = r._docker([str(tmp_path / "no-such-docker")], 5)
    assert rc == 127 and "could not run docker" in out


@pytest.mark.parametrize("value, rc", [("true", 0), ("false", 1), ("True", 1), ("yes", 1), ("", 1)])
def test_tc1450_the_condition_is_exactly_true(tmp_path, value, rc):
    env = tmp_path / "env"
    env.write_text("OTTERTUBE_ENABLED=%s\n" % value)
    assert ot.main(["--env-file", str(env), "condition"]) == rc


def test_tc1450_check_list_prints_the_plan_and_refuses_a_bad_list(tmp_path, capsys):
    env = tmp_path / "env"
    env.write_text("OTTERTUBE_ENABLED=true\n")
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps(a_list(item(1))))
    bad.write_text(json.dumps(a_list(item(1, url="https://www.youtube.com/@x"))))
    assert ot.main(["--env-file", str(env), "check-list", "--list", str(good)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert ot.main(["--env-file", str(env), "check-list", "--list", str(bad)]) == ot.EXIT_INVALID
    assert json.loads(capsys.readouterr().out)["ok"] is False


# ── TC-1444..TC-1448 the stack contract, read from the files ─────────────────


def compose_services():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((STACK / "docker-compose.yml").read_text(encoding="utf-8"))


def test_tc1444_every_ottertube_service_follows_the_scratch_writer_rules():
    doc = compose_services()
    names = ("ottertube-ytdl-sub", "ottertube-indexer", "ottertube-media")
    for name in names:
        svc = doc["services"][name]
        assert svc["profiles"] == ["ottertube"], name
        assert svc["restart"] == "no", name
        assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name
        assert svc["logging"] == {"driver": "none"}, name
        assert "SYS_RESOURCE" not in str(svc.get("cap_add", [])), name
        for vol in svc["volumes"]:
            assert isinstance(vol, dict) and vol["type"] == "bind", (name, vol)
            assert vol["bind"]["create_host_path"] is False, (name, vol)
            assert not vol["target"].rstrip("/") in ("/lib", "/usr", "/bin", "/etc"), (name, vol)
            if vol["source"].startswith("/srv/scratch"):
                assert vol["source"].startswith("/srv/scratch/ottertube/"), (name, vol)
    # nothing else in the stack binds the ottertube tree
    for other, svc in doc["services"].items():
        if other not in names:
            assert "/srv/scratch/ottertube" not in json.dumps(svc.get("volumes", [])), other
    assert doc["services"]["ottertube-indexer"]["network_mode"] == "none"
    assert doc["services"]["ottertube-ytdl-sub"]["networks"] == ["ottertube"]
    assert doc["services"]["ottertube-media"]["container_name"] == "homehub-ottertube-media"
    assert all(v["read_only"] for v in doc["services"]["ottertube-media"]["volumes"])
    assert doc["services"]["ottertube-media"]["cap_add"] == ["NET_BIND_SERVICE"]
    caddy = doc["services"]["caddy"]
    assert "./ottertube/web:/srv/ottertube/web:ro" in caddy["volumes"]
    assert caddy["environment"]["OTTERTUBE_HOST"] == "${OTTERTUBE_SUBDOMAIN:-ottertube}.${DOMAIN}"
    assert "/srv/scratch" not in json.dumps(caddy["volumes"])


def unit(name):
    return (OT_DIR / name).read_text(encoding="utf-8")


def test_tc1445_the_units_cannot_start_ahead_of_the_mount_or_outlive_it():
    for name in ("homehub-ottertube-download.service", "homehub-ottertube-media.service"):
        text = unit(name)
        for line in ("RequiresMountsFor=/srv/scratch", "BindsTo=srv-scratch.mount", "CapabilityBoundingSet=~CAP_SYS_RESOURCE",
                     "ExecCondition=/usr/bin/python3 /opt/homehub/stack/ottertube/ottertube.py condition"):
            assert line in text, (name, line)
        assert not re.search(r"^Requires=.*docker", text, re.M), name
    dl = unit("homehub-ottertube-download.service")
    assert "ExecStartPre=/usr/bin/python3 /opt/homehub/stack/scratch/homehub_scratch.py guard --writer ottertube" in dl
    assert "Type=oneshot" in dl and "Restart=" not in dl
    assert "ExecStopPost=-/usr/bin/docker rm -f homehub-ottertube-dl homehub-ottertube-index" in dl
    media = unit("homehub-ottertube-media.service")
    assert "ottertube.py serve-check" in media and "WantedBy=multi-user.target srv-scratch.mount" in media
    timer = unit("homehub-ottertube-download.timer")
    assert re.search(r"^OnCalendar=\*-\*-\* 0[0-5]:\d\d:00$", timer, re.M), "daily and off-peak"
    assert "Persistent=false" in timer and "WantedBy=timers.target" in timer


def test_tc1446_firstboot_installs_all_three_and_enables_them_only_with_the_knob():
    text = (STACK / "autoinstall" / "firstboot.sh").read_text(encoding="utf-8")
    block = text[text.index("# ── 6j. OtterTube"):text.index("# ── 7. done")]
    for u in ("homehub-ottertube-download.service", "homehub-ottertube-download.timer", "homehub-ottertube-media.service"):
        assert u in block
    on, off = block.split('if [ "$__ot_on" = true ]; then', 1)[1].split("else", 1)
    assert "systemctl enable homehub-ottertube-download.timer homehub-ottertube-media.service" in on
    assert "systemctl disable --now homehub-ottertube-download.timer homehub-ottertube-media.service" in off
    assert "--no-block" in on


def test_tc1446_the_bake_follows_the_knob_and_refuses_the_profile_in_compose_profiles():
    text = (ROOT / "vmtest" / "export-images.sh").read_text(encoding="utf-8")
    block = text[text.index("# ── OTTERTUBE:"):]
    assert 'env_file_value "$ENV_FILE" OTTERTUBE_ENABLED' in block
    assert "PROFILE_ARGS+=(--profile ottertube)" in block
    assert re.search(r'\*" ottertube "\*\)\s+die ', block)


def test_tc1446_the_private_list_rides_the_site_payload_optionally():
    text = (STACK / "autoinstall" / "user-data").read_text(encoding="utf-8")
    assert '"ottertube-list.json:/target/opt/homehub/stack/ottertube/config/list.json:optional"' in text


def test_tc1446_the_dns_record_exists_only_with_the_knob():
    text = (STACK / "provision" / "provision-technitium.sh").read_text(encoding="utf-8")
    assert re.search(r'if \[ "\$\{OTTERTUBE_ENABLED:-\}" = true \]; then\s+add_a "\$TOKEN" "\$\{OTTERTUBE_SUBDOMAIN:-ottertube\}\.\$\{DOMAIN\}" "\$LAN_IP"', text)
    assert "*." not in re.sub(r"#.*", "", text.split("OTTERTUBE (SN-051")[1].split("fi")[0])


def pin():
    out = {}
    for line in (OT_DIR / "ottertube-images.pin").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def env_example():
    out = {}
    for line in (STACK / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v.strip('"')
    return out


def test_tc1447_both_images_are_pinned_by_version_and_index_digest_and_the_knobs_are_built_from_the_pin():
    p, e = pin(), env_example()
    for prefix, knob in (("YTDL_SUB", "OTTERTUBE_YTDL_SUB_IMAGE"), ("INDEXER", "OTTERTUBE_INDEXER_IMAGE")):
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", p[prefix + "_DIGEST"])
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", p[prefix + "_AMD64_MANIFEST"])
        assert e[knob] == "%s:%s@%s" % (p[prefix + "_IMAGE"], p[prefix + "_VERSION"], p[prefix + "_DIGEST"])
    assert p["YTDL_SUB_VERSION"] == "2026.08.26.post1"  # the OtterTube kit's tag, confirmed to exist
    assert e["OTTERTUBE_ENABLED"] == "false"


def test_tc1448_the_caddy_site_is_lan_only_and_serves_only_the_publication():
    text = (STACK / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    block = text[text.index("{$OTTERTUBE_HOST} {"):]
    block = block[:block.index("\n}\n") + 3]
    assert "import acme_cloudflare" in block
    assert "@lan remote_ip {$LAN_CIDR}" in block and 'respond "ottertube: LAN only" 403' in block
    assert block.count("reverse_proxy homehub-ottertube-media:8080") == 2
    assert r"path_regexp ^/(video|music)/[a-z0-9-]+\.(mp4|" in block
    assert "header Origin https://{$OTTERTUBE_HOST}" in block and "header Origin app://ottertube" in block
    assert "root * /srv/ottertube/web" in block
    assert "/srv/scratch" not in block and "basic_auth" not in block and "forward_auth" not in block


def test_tc1446_validate_config_enrols_both_services():
    text = (ROOT / "scripts" / "validate_config.py").read_text(encoding="utf-8")
    assert '("ottertube/homehub-ottertube-download.service", "autoinstall/firstboot.sh")' in text
    assert '("ottertube/homehub-ottertube-media.service", "autoinstall/firstboot.sh")' in text
