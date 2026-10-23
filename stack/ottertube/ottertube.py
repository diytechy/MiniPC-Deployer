#!/usr/bin/env python3
"""ottertube.py - the hub's OtterTube downloader and catalogue: the host side (SN-051, SR-113..SR-117).

The hub downloads a CURATED, FINITE list of children's videos onto the NVMe
scratch volume, builds OtterTube's catalogue from them with OtterTube's own
indexer, and serves both (with the web build) on one LAN-only name. The list is
private HomeHub config; this repository holds only the mechanism.

    condition      exit 0 when OTTERTUBE_ENABLED is exactly `true`, 1 otherwise:
                   every OtterTube unit's ExecCondition (a clean skip).
    check-list     validate the list (--list PATH, default the installed one)
                   and print the plan: collections, items, the size estimate
                   against the budget. Read-only; no network, no scratch.
    run            the timer's job (homehub-ottertube-download.service):
                   guard -> one ytdl-sub container PER ITEM not yet present,
                   each behind its own guard -> titles and thumbnails -> prune
                   what the list no longer names -> guard -> the indexer ->
                   logs/last-run.json.
    serve-check    the media server's ExecStartPre: the guard, then the
                   directories it binds read-only must exist on the volume.
    status         read-only JSON for deployment-state: the last run, the
                   catalogue's age, the tree's size.

FINITE BY CONSTRUCTION (SR-114). An item is ONE video: its URL must match the
single-item rule of a host in OTTERTUBE_ALLOWED_HOSTS (no channel, playlist or
search URL passes), every item names a size estimate, the estimates' sum must
fit OTTERTUBE_BUDGET_MIB, the list holds at most OTTERTUBE_MAX_ITEMS, each run
caps ytdl-sub at one download and yt-dlp at OTTERTUBE_MAX_FILESIZE_MIB, and an
item whose file is already present is not fetched again. A host enters the
allow-list only after its terms were reviewed (HomeHub's starter-list record).

THE SCRATCH RULES (SR-103, IF-071). Every write lands under
/srv/scratch/ottertube/, in the ottertube quota project. The guard runs before
the run, before EVERY item's container and before the indexer; a refusal stops
the run (exit 76) with an alert, and nothing further is done on the volume:
not even work/ is emptied. Every cleanup of work/ after the start of the run
asks the guard again first and leaves work/ as it is when the guard refuses.
EDQUOT or ENOSPC, seen in a container's output or raised here, stops the run
cleanly (exit 75): the work directory (its own temporary files, a partial
download among them) is emptied, an alert is sent, nothing is written anywhere
else, and the unit does not retry.

Python 3.8-compatible. Implements: SR-113..SR-117, LLR-1260..LLR-1283.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import random
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "monitoring"))
sys.path.insert(0, os.path.join(HERE, "..", "scratch"))
import monitor_common as mc  # noqa: E402
import homehub_scratch as scratch  # noqa: E402

WRITER = "ottertube"
EXIT_OK, EXIT_FAILED, EXIT_INVALID, EXIT_QUOTA, EXIT_GUARD = 0, 1, 2, 75, 76
MIB = 1024 * 1024

# The age bands OtterTube knows (app/src/catalog/validate.ts BANDS).
BANDS = ("toddler", "prek", "k2", "g34", "g56")
ORDERS = ("newest", "oldest", "title")
KINDS = ("video", "music")
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
VIDEO_EXT = (".mp4", ".m4v", ".webm", ".mkv", ".mov")
AUDIO_EXT = (".m4a", ".mp3", ".ogg", ".opus", ".oga", ".flac", ".wav", ".aac")
THUMB_EXT = (".jpg", ".jpeg", ".png", ".webp")
THUMB_MAX_BYTES = 4 * MIB
# The plan's cap on every video (W8c task 10): enforced on the downloaded file.
MAX_HEIGHT = 480

# SINGLE-ITEM RULES, per host: a URL passes only when it names ONE video. A host
# that is allowed but has no rule here cannot be used at all, so widening the
# downloader needs a code change and a review, not only a knob. Each value is
# (path regex, required query keys, forbidden query keys).
HOST_RULES: Dict[str, Tuple[str, Tuple[str, ...], Tuple[str, ...]]] = {
    # NASA Image and Video Library assets: one file of one asset.
    "images-assets.nasa.gov": (r"^/video/[^/]+/[^/]+\.(mp4|m4v)$", (), ()),
    # PeerTube watch pages (Blender Studio's official instance among them).
    "video.blender.org": (r"^/(w|videos/watch)/[A-Za-z0-9-]{8,64}$", (), ()),
    # YouTube: a single watch URL only, never a list. NOT in the default
    # allow-list: its terms are an Owner decision (HomeHub starter-list record).
    "www.youtube.com": (r"^/watch$", ("v",), ("list",)),
}
THUMB_PATH_RE = re.compile(r"^/[^?#]*\.(jpg|jpeg|png|webp)$", re.IGNORECASE)

QUOTA_PATTERNS = (re.compile(r"Errno 122\b"), re.compile(r"Disk quota exceeded", re.IGNORECASE),
                  re.compile(r"\bEDQUOT\b"))
SPACE_PATTERNS = (re.compile(r"Errno 28\b"), re.compile(r"No space left on device", re.IGNORECASE),
                  re.compile(r"\bENOSPC\b"))

DL_CONTAINER = "homehub-ottertube-dl"
INDEX_CONTAINER = "homehub-ottertube-index"
# What `docker inspect` says about a container that does not exist.
NO_SUCH_CONTAINER = re.compile(r"No such (object|container)", re.IGNORECASE)

# An item is COMPLETE only with <stem><DONE_EXT>, written (atomically) after its
# 480p probe, its title and its thumbnail step all succeeded. Media without it
# was left by a run that stopped in between, and is checked again (LLR-1280).
DONE_EXT = ".ok"


class Invalid(Exception):
    """The list or a knob is not acceptable; nothing was changed."""


class QuotaStop(Exception):
    """EDQUOT/ENOSPC: the ottertube budget is full. Stop, never write elsewhere."""


class GuardStop(Exception):
    """The scratch guard refused: the volume is not the one we may write to."""


class ContainerStop(Exception):
    """A run container could not be confirmed gone after its client stopped
    (a timeout, a missing binary or any other non-zero exit): it may still be
    writing, so the run stops rather than start the next container beside it."""


# ── knobs ────────────────────────────────────────────────────────────────────


class Settings:
    """Every knob the downloader reads, resolved once from the stack .env.

    Contract:
      Config:  OTTERTUBE_ENABLED (exactly `true` to run), OTTERTUBE_ALLOWED_HOSTS
               (space-separated; each must have a HOST_RULES entry),
               OTTERTUBE_BUDGET_MIB (sum of the list's estimates, 3072),
               OTTERTUBE_MAX_ITEMS (40), OTTERTUBE_MAX_FILESIZE_MIB (400: yt-dlp
               skips a bigger file), OTTERTUBE_ITEM_TIMEOUT_SEC (1800),
               OTTERTUBE_PAUSE_SEC (20: between two downloads),
               OTTERTUBE_GRACE_DAYS (7: the indexer's grace for removed media).
               [stack/.env]
    """

    def __init__(self, env: Dict[str, str]):
        self.env = env
        self.enabled = env.get("OTTERTUBE_ENABLED", "").strip() == "true"
        self.allowed_hosts = tuple(mc.knob_words(env, "OTTERTUBE_ALLOWED_HOSTS",
                                                 "images-assets.nasa.gov video.blender.org"))
        self.budget_mib = mc.knob_int(env, "OTTERTUBE_BUDGET_MIB", 3072, 1, 1 << 20)
        self.max_items = mc.knob_int(env, "OTTERTUBE_MAX_ITEMS", 40, 1, 1000)
        self.max_filesize_mib = mc.knob_int(env, "OTTERTUBE_MAX_FILESIZE_MIB", 400, 1, 1 << 16)
        self.item_timeout = mc.knob_int(env, "OTTERTUBE_ITEM_TIMEOUT_SEC", 1800, 60, 86400)
        self.pause_sec = mc.knob_int(env, "OTTERTUBE_PAUSE_SEC", 20, 0, 3600)
        self.grace_days = mc.knob_int(env, "OTTERTUBE_GRACE_DAYS", 7, 0, 365)


class Paths:
    """Every host path; tests point `root` elsewhere (the same shape as homehub_scratch.Paths)."""

    def __init__(self, root: str = "/", stack: str = "/opt/homehub/stack"):
        def at(p: str) -> str:
            return os.path.join(root, p.lstrip("/"))
        self.root = root
        self.base = at("/srv/scratch/ottertube")
        self.lib = os.path.join(self.base, "lib")
        self.publish = os.path.join(self.lib, "publish")
        self.catalog = os.path.join(self.base, "catalog")
        self.work = os.path.join(self.base, "work")
        self.logs = os.path.join(self.base, "logs")
        self.stack = stack
        self.list_file = os.path.join(stack, "ottertube", "config", "list.json")
        self.indexer = os.path.join(stack, "ottertube", "indexer")

    def kind_root(self, kind: str) -> str:
        return os.path.join(self.lib, kind)

    def dirs(self) -> List[str]:
        """The directories the run creates (all inside the ottertube quota project)."""
        return [self.lib, self.kind_root("video"), self.kind_root("music"), self.publish,
                self.catalog, self.work, self.logs]


# ── pure core: the list ──────────────────────────────────────────────────────


def single_item_problem(url: object, allowed_hosts: Sequence[str]) -> Optional[str]:
    """None when `url` names ONE video on an allowed host, else the reason (SR-114).

    Contract:
      Inputs:  url: the item's URL; allowed_hosts: OTTERTUBE_ALLOWED_HOSTS.
      Outputs: None, or a reason that names the rule, never the URL itself.
    Implements: SR-114, LLR-1261.
    """
    if not isinstance(url, str) or not url or len(url) > 2048:
        return "the url is missing or too long"
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        return "the url is not https"
    host = (parts.hostname or "").lower()
    if parts.username or parts.password or parts.port not in (None, 443):
        return "the url carries a credential or a port"
    if host not in allowed_hosts:
        return "the host %s is not in OTTERTUBE_ALLOWED_HOSTS" % host
    rule = HOST_RULES.get(host)
    if rule is None:
        return "the host %s has no single-item rule" % host
    path_re, required, forbidden = rule
    if not re.fullmatch(path_re, urllib.parse.unquote(parts.path)):
        return "the url is not a single video on %s (channel, playlist or other page)" % host
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    if any(k not in query for k in required) or any(k in query for k in forbidden):
        return "the url's query is not a single video's"
    if parts.fragment:
        return "the url has a fragment"
    return None


def thumb_problem(url: object, allowed_hosts: Sequence[str]) -> Optional[str]:
    """None when `url` is an https image on an allowed host, else the reason."""
    if not isinstance(url, str) or len(url) > 2048:
        return "the thumb is not a url"
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in allowed_hosts:
        return "the thumb is not https on an allowed host"
    if parts.username or parts.password or parts.port not in (None, 443):
        return "the thumb carries a credential or a port"
    if not THUMB_PATH_RE.match(urllib.parse.unquote(parts.path)):
        return "the thumb is not a .jpg, .png or .webp"
    return None


def _text(value: object, low: int, high: int) -> bool:
    return isinstance(value, str) and low <= len(value.strip()) <= high


def validate_list(data: object, settings: Settings) -> Tuple[Dict[str, object], List[str]]:
    """Check the curated list and return (plan, problems). Pure (SR-114).

    Contract:
      Inputs:  data: the decoded list.json; settings: the knobs.
      Outputs: plan {collections, items, estimate_mib, budget_mib}; problems: every
               reason the list is refused (empty = accepted). Problems name ids
               and field names, never titles or URLs.
      Shape:   {"version": 1, "collections": [{"id", "title", "bands": [...],
               "kind": "video"|"music" (default video), "order": newest|oldest|title,
               "items": [{"id", "title", "url", "estimateMiB", "licence", "basis",
               "source", "thumb"?, "note"?}]}], "comment"?: any}
    Implements: SR-114, LLR-1260, LLR-1262.
    """
    problems: List[str] = []
    plan: Dict[str, object] = {"collections": 0, "items": 0, "estimate_mib": 0, "budget_mib": settings.budget_mib}
    if not isinstance(data, dict):
        return plan, ["the list is not a JSON object"]
    for key in data:
        if key not in ("version", "collections", "comment"):
            problems.append("unknown top-level key %r" % key)
    if data.get("version") != 1:
        problems.append("version must be 1")
    collections = data.get("collections")
    if not isinstance(collections, list) or not collections:
        return plan, problems + ["collections must be a non-empty list"]
    seen_collections, seen_items = set(), set()
    total_items = estimate = 0
    for ci, col in enumerate(collections):
        at = "collections[%d]" % ci
        if not isinstance(col, dict):
            problems.append("%s is not an object" % at)
            continue
        for key in col:
            if key not in ("id", "title", "bands", "kind", "order", "items", "comment"):
                problems.append("%s: unknown key %r" % (at, key))
        cid = col.get("id")
        if not isinstance(cid, str) or not ID_RE.match(cid) or len(cid) > 48:
            problems.append("%s: id must be kebab-case, at most 48 characters" % at)
        elif cid in seen_collections:
            problems.append("%s: duplicate collection id %s" % (at, cid))
        else:
            seen_collections.add(cid)
            at = "collection %s" % cid
        if not _text(col.get("title"), 1, 120):
            problems.append("%s: title must be 1-120 characters" % at)
        bands = col.get("bands")
        if not isinstance(bands, list) or not bands or any(b not in BANDS for b in bands) or len(set(bands)) != len(bands):
            problems.append("%s: bands must be a non-empty list of distinct %s" % (at, "|".join(BANDS)))
        if col.get("kind", "video") not in KINDS:
            problems.append("%s: kind must be video or music" % at)
        if col.get("order", "oldest") not in ORDERS:
            problems.append("%s: order must be newest, oldest or title" % at)
        items = col.get("items")
        if not isinstance(items, list) or not items:
            problems.append("%s: items must be a non-empty list" % at)
            continue
        if len(items) > 25:
            problems.append("%s: more than 25 items" % at)
        for ii, item in enumerate(items):
            iat = "%s items[%d]" % (at, ii)
            if not isinstance(item, dict):
                problems.append("%s is not an object" % iat)
                continue
            for key in item:
                if key not in ("id", "title", "url", "estimateMiB", "licence", "basis", "source", "thumb", "note"):
                    problems.append("%s: unknown key %r" % (iat, key))
            iid = item.get("id")
            if not isinstance(iid, str) or not ID_RE.match(iid) or len(iid) > 48:
                problems.append("%s: id must be kebab-case, at most 48 characters" % iat)
            elif iid in seen_items:
                problems.append("%s: duplicate item id %s" % (iat, iid))
            else:
                seen_items.add(iid)
                iat = "item %s" % iid
            if not _text(item.get("title"), 1, 120):
                problems.append("%s: title must be 1-120 characters" % iat)
            reason = single_item_problem(item.get("url"), settings.allowed_hosts)
            if reason:
                problems.append("%s: %s" % (iat, reason))
            est = item.get("estimateMiB")
            if isinstance(est, bool) or not isinstance(est, int) or not 1 <= est <= settings.max_filesize_mib:
                problems.append("%s: estimateMiB must be a whole number 1..%d (OTTERTUBE_MAX_FILESIZE_MIB)"
                                % (iat, settings.max_filesize_mib))
            else:
                estimate += est
            # The licence record travels with the item: no basis, no download.
            if not _text(item.get("licence"), 2, 120) or not _text(item.get("source"), 2, 200):
                problems.append("%s: licence and source must be recorded" % iat)
            basis = item.get("basis")
            if not isinstance(basis, str) or urllib.parse.urlsplit(basis).scheme != "https":
                problems.append("%s: basis must be the https URL of the terms relied on" % iat)
            if "thumb" in item:
                reason = thumb_problem(item.get("thumb"), settings.allowed_hosts)
                if reason:
                    problems.append("%s: %s" % (iat, reason))
            total_items += 1
    if total_items > settings.max_items:
        problems.append("%d items, more than OTTERTUBE_MAX_ITEMS=%d" % (total_items, settings.max_items))
    if estimate > settings.budget_mib:
        problems.append("the estimates total %d MiB, more than OTTERTUBE_BUDGET_MIB=%d" % (estimate, settings.budget_mib))
    plan.update(collections=len(seen_collections), items=total_items, estimate_mib=estimate)
    return plan, problems


def iter_items(data: Dict[str, object]):
    """(collection, item) pairs in list order. Only for a list validate_list accepted."""
    for col in data["collections"]:  # type: ignore[index]
        for item in col["items"]:
            yield col, item


def subscription_for(col: Dict[str, object], item: Dict[str, object], settings: Settings) -> Dict[str, object]:
    """ONE item's ytdl-sub subscription file (JSON, which ytdl-sub reads as YAML).

    The name is the item id; the stem of every file it writes is the item id;
    ytdl-sub stops after one download and yt-dlp skips a file over the cap.
    Implements: SR-114, LLR-1263.
    """
    preset = "ottertube_music" if col.get("kind", "video") == "music" else "ottertube_video"
    return {preset: {str(item["id"]): {
        "overrides": {"ottertube_url": item["url"], "ottertube_folder": col["id"], "ottertube_stem": item["id"]},
        "ytdl_options": {"max_filesize": settings.max_filesize_mib * MIB},
        "throttle_protection": {"max_downloads_per_subscription": {"min": 1, "max": 1}},
    }}}


def approved_for(data: Dict[str, object]) -> Dict[str, object]:
    """OtterTube's approved.json (tools/catalog/lib.mjs checkApproved) from the list.

    One approved entry per collection: folder = collection id, so the catalogue
    holds exactly the list's collections, with the list's titles and bands.
    Implements: SR-115, LLR-1266.
    """
    out: Dict[str, object] = {"version": 1, "mediaBase": "media/", "videos": [], "music": []}
    for col in data["collections"]:  # type: ignore[index]
        entry = {"folder": col["id"], "id": col["id"], "title": col["title"].strip(),
                 "bands": list(col["bands"]), "order": col.get("order", "oldest")}
        out["music" if col.get("kind", "video") == "music" else "videos"].append(entry)
    return out


def classify_output(text: str) -> Optional[str]:
    """"quota" (EDQUOT), "space" (ENOSPC) or None, from a container's output.

    Implements: SR-116, LLR-1268.
    """
    if any(p.search(text) for p in QUOTA_PATTERNS):
        return "quota"
    if any(p.search(text) for p in SPACE_PATTERNS):
        return "space"
    return None


def media_exts(kind: str) -> Tuple[str, ...]:
    return AUDIO_EXT if kind == "music" else VIDEO_EXT


def _names(folder: str) -> set:
    try:
        return set(os.listdir(folder))
    except OSError:
        return set()


def downloaded(folder: str, stem: str, kind: str) -> bool:
    """True when the item's media file and info.json are on the volume (checked or not)."""
    names = _names(folder)
    return (stem + ".info.json") in names and any((stem + ext) in names for ext in media_exts(kind))


def present(folder: str, stem: str, kind: str) -> bool:
    """True when the item is COMPLETE: its media file, its info.json and its
    completion marker, which only a run whose checks all passed writes.

    Implements: SR-113, SR-114, LLR-1270, LLR-1280.
    """
    return (stem + DONE_EXT) in _names(folder) and downloaded(folder, stem, kind)


def catalog_items(path: str) -> int:
    """Items (videos + tracks) in a catalogue file, as OtterTube's indexer counts
    them for its shrink guard (tools/catalog/lib.mjs itemCount); 0 when unreadable.

    Implements: LLR-1281.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            c = json.load(handle)
    except (OSError, ValueError):
        return 0
    if not isinstance(c, dict):
        return 0

    def count(key: str, inner: str) -> int:
        rows = c.get(key)
        if not isinstance(rows, list):
            return 0
        return sum(len(x[inner]) for x in rows if isinstance(x, dict) and isinstance(x.get(inner), list))
    return count("videos", "items") + count("music", "tracks")


def file_stem(name: str) -> str:
    return name.split(".", 1)[0]


def item_files(folder: str, stem: str) -> List[str]:
    """Every file one item wrote: <stem>.* (media, info.json, thumbnail) and its
    subscription's download archive."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    archive = ".ytdl-sub-%s-download-archive.json" % stem
    return [os.path.join(folder, n) for n in names if file_stem(n) == stem or n == archive]


def parse_height(text: str) -> Optional[int]:
    """The first whole number on its own line of ffprobe's `csv=p=0` output."""
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if line.isdigit():
            return int(line)
    return None


def prune_plan(lib: str, data: Dict[str, object]) -> List[str]:
    """Paths under lib/video and lib/music the list no longer names (pure listing).

    Keeps every collection folder and item file the list names; a removed item's
    files and a removed collection's whole folder are returned for deletion. The
    publication is the indexer's (its grace period covers served copies).
    Only regular files and plain directories are ever returned; a symbolic link
    is returned as itself, never followed.
    Implements: SR-115, LLR-1267.
    """
    wanted: Dict[Tuple[str, str], set] = {}
    for col in data["collections"]:  # type: ignore[index]
        kind = "music" if col.get("kind", "video") == "music" else "video"
        wanted[(kind, col["id"])] = {it["id"] for it in col["items"]}
    out: List[str] = []
    for kind in KINDS:
        root = os.path.join(lib, kind)
        try:
            folders = sorted(os.listdir(root))
        except OSError:
            continue
        for folder in folders:
            fpath = os.path.join(root, folder)
            if (kind, folder) not in wanted or not stat.S_ISDIR(os.lstat(fpath).st_mode):
                out.append(fpath)
                continue
            keep = wanted[(kind, folder)]
            for name in sorted(os.listdir(fpath)):
                archive = re.fullmatch(r"\.ytdl-sub-(.+)-download-archive\.json", name)
                if archive:
                    if archive.group(1) not in keep:
                        out.append(os.path.join(fpath, name))
                elif file_stem(name) not in keep:
                    out.append(os.path.join(fpath, name))
    return out


def run_summary(started: float, finished: float, result: str, counts: Dict[str, int], **extra) -> Dict[str, object]:
    """logs/last-run.json: what deployment-state reads (SR-117)."""
    out: Dict[str, object] = {"schema": 1, "started": int(started), "finished": int(finished),
                              "result": result, "items": dict(counts)}
    out.update(extra)
    return out


# ── the shell ────────────────────────────────────────────────────────────────


def is_quota_error(exc: OSError) -> bool:
    return exc.errno in (errno.EDQUOT, errno.ENOSPC)


def empty_dir(path: str) -> None:
    """Remove everything inside `path` (our own work directory), never `path` itself."""
    try:
        names = os.listdir(path)
    except FileNotFoundError:
        return
    for name in names:
        p = os.path.join(path, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.unlink(p)
            except OSError:
                pass


def write_json_atomic(path: str, data) -> None:
    """Replace `path` atomically; EDQUOT/ENOSPC propagate (the caller stops the run)."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def retitle(info_path: str, title: str) -> bool:
    """Set the item's title in its info.json to the list's (sources such as a
    bare file URL give the file name as the title). info.json is never
    published, so rewriting it cannot touch a served file. True if changed."""
    with open(info_path, encoding="utf-8") as handle:
        meta = json.load(handle)
    if not isinstance(meta, dict) or (meta.get("title") == title and meta.get("fulltitle") == title):
        return False
    meta["title"] = meta["fulltitle"] = title
    write_json_atomic(info_path, meta)
    return True


def fetch_thumb(url: str, dest_stem: str, timeout: float = 30.0) -> Optional[str]:
    """Download the list's thumbnail next to the item (only when the source gave
    none): https, at most THUMB_MAX_BYTES, written to a temporary name and renamed.
    Returns the path written, or None when it was not usable."""
    ext = os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower()
    if ext not in THUMB_EXT:
        return None
    dest = dest_stem + (".jpg" if ext == ".jpeg" else ext)
    req = urllib.request.Request(url, headers={"User-Agent": "homehub-ottertube/1"})
    fd, tmp = tempfile.mkstemp(prefix=".thumb-", dir=os.path.dirname(dest_stem))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, os.fdopen(fd, "wb") as out:
            got = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                got += len(chunk)
                if got > THUMB_MAX_BYTES:
                    raise ValueError("thumbnail larger than %d bytes" % THUMB_MAX_BYTES)
                out.write(chunk)
        os.replace(tmp, dest)
        return dest
    except OSError as exc:
        if is_quota_error(exc):
            raise
        return None
    except ValueError:
        return None
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def scratch_identity(scratch_settings, root: Optional[str] = None) -> Tuple[bool, str]:
    """Is the mounted volume still OUR scratch volume (the guard's identity and
    placement checks)? The question before a cleanup, which frees space rather
    than taking it: a write probe that fails because the ottertube budget is
    full (EDQUOT, ENOSPC) does not make it someone else's volume. Any other
    refusal stands, a failed probe for another reason among them.

    Implements: LLR-1283.
    """
    facts = scratch.gather_guard_facts(WRITER, scratch_settings, scratch.Paths(root) if root else scratch.Paths())
    if facts.get("probe_error") in ("EDQUOT", "ENOSPC"):
        facts["probe_error"] = None
    return scratch.judge_guard(facts)


class Runner:
    """One download-and-index run. Every side effect goes through an injectable
    callable so tests drive it with fakes: guard, docker, alert, sleep, fetch."""

    def __init__(self, settings: Settings, paths: Paths, scratch_settings, env_file: str,
                 guard: Optional[Callable[[], Tuple[bool, str]]] = None,
                 identity: Optional[Callable[[], Tuple[bool, str]]] = None,
                 docker: Optional[Callable[[List[str], float], Tuple[int, str]]] = None,
                 alert: Optional[Callable[[str, str, str], None]] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 fetch: Callable[[str, str], Optional[str]] = fetch_thumb,
                 clock: Callable[[], float] = time.time):
        self.s, self.p, self.env_file = settings, paths, env_file
        self.guard = guard or (lambda: scratch.guard(WRITER, scratch_settings))
        # The cleanup's question (scratch_identity). An injected guard answers
        # it too unless an identity is injected as well (main injects both).
        self.identity = identity or (guard if guard else (lambda: scratch_identity(scratch_settings)))
        self.docker = docker or self._docker
        self.alert = alert or self._alert
        self.sleep, self.fetch, self.clock = sleep, fetch, clock
        self.counts = {"present": 0, "downloaded": 0, "failed": 0, "pruned": 0}
        self.current: Optional[str] = None
        # A guard refusal anywhere in the run: nothing further touches the
        # volume, the cleanup of work/ included (LLR-1269, LLR-1283).
        self.refused = False
        self.work_cleaned = False
        self.docker_bin: List[str] = ["docker"]  # tests point it at a fake client

    # side effects ------------------------------------------------------------

    def _docker(self, argv: List[str], timeout: float) -> Tuple[int, str]:
        res = mc.run_bounded(argv, timeout, 4 * MIB)
        text = res.output.decode("utf-8", "replace")
        if res.error is not None:
            return 127, text + "\n(could not run docker: %s)" % res.error
        if res.timed_out:
            return 124, text + "\n(stopped after %ds)" % timeout
        return (res.rc if res.rc is not None else 1), text

    def _alert(self, key: str, title: str, body: str) -> None:
        mc.send_alert(key, "high", title, body, self.env_file, mc.DEFAULT_STATE_DIR, log_tag="homehub-ottertube")

    def check_guard(self, before: str) -> None:
        ok, reason = self.guard()
        if not ok:
            self.refused = True
            raise GuardStop("before %s: %s" % (before, reason))

    def clean_work(self, when: str) -> bool:
        """Empty work/, but only on the volume that is still ours NOW
        (scratch_identity): never after any guard refusal in this run, and not
        when the identity check refuses here. True when work/ was emptied.

        Implements: LLR-1283.
        """
        if self.refused:
            mc.log("ottertube: work/ left as it is (%s): the scratch guard refused the volume" % when, warning=True)
            return False
        ok, reason = self.identity()
        if not ok:
            self.refused = True
            mc.log("ottertube: work/ left as it is (%s): the scratch volume check refused: %s" % (when, reason), warning=True)
            return False
        empty_dir(self.p.work)
        self.work_cleaned = True
        return True

    def compose(self, *args: str) -> List[str]:
        # Explicit file and project directory: the stack .env interpolates the
        # compose file whatever the caller's working directory is.
        return self.docker_bin + ["compose", "--project-directory", self.p.stack,
                "-f", os.path.join(self.p.stack, "docker-compose.yml"), "--profile", "ottertube"] + list(args)

    def run_container(self, name: str, argv: List[str], timeout: float) -> Tuple[int, str]:
        """One `docker compose run --rm --name NAME`. The container belongs to the
        daemon, not to the client run_bounded kills, so after ANY non-zero exit
        (124 timeout, 127 no client, the container's own failure) it is removed
        and confirmed gone before the run goes on; ContainerStop when it cannot be.

        Implements: SR-116, LLR-1282.
        """
        self.current = name
        rc, out = self.docker(argv, timeout)
        if rc != 0:
            self.reap(name)
        self.current = None
        return rc, out

    def reap(self, name: str) -> None:
        """`docker rm -f NAME` until `docker inspect NAME` says there is no such container."""
        rc, out = 0, ""
        for _attempt in range(3):
            self.docker(self.docker_bin + ["rm", "-f", name], 60)
            rc, out = self.docker(self.docker_bin + ["inspect", "--format", "{{.State.Status}}", name], 30)
            if rc != 0 and NO_SUCH_CONTAINER.search(out):
                return
        raise ContainerStop("the container %s could not be confirmed gone (docker inspect: exit %d, %s)"
                            % (name, rc, (out.strip().splitlines() or ["no output"])[-1][:200]))

    def discard(self, folder: str, stem: str) -> None:
        """Every file of one item: an incomplete item never reaches the indexer."""
        for path in item_files(folder, stem):
            os.unlink(path)

    # steps -------------------------------------------------------------------

    def load_list(self) -> Dict[str, object]:
        try:
            with open(self.p.list_file, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            raise Invalid("no list at %s (HomeHub scripts/deploy/ottertube/list.json, installed by the image or the coordinator)" % self.p.list_file)
        except (OSError, ValueError) as exc:
            raise Invalid("the list could not be read: %s" % exc.__class__.__name__)
        plan, problems = validate_list(data, self.s)
        if problems:
            raise Invalid("the list was refused: " + "; ".join(problems[:8]))
        mc.log("ottertube: list accepted - %(collections)d collections, %(items)d items, "
               "estimate %(estimate_mib)d MiB of a %(budget_mib)d MiB budget" % plan)
        return data  # type: ignore[return-value]

    def ensure_dirs(self) -> None:
        for d in self.p.dirs():
            os.makedirs(d, mode=0o750, exist_ok=True)

    def item_place(self, col: Dict[str, object], item: Dict[str, object]) -> Tuple[str, str, str]:
        kind = "music" if col.get("kind", "video") == "music" else "video"
        return kind, os.path.join(self.p.kind_root(kind), str(col["id"])), str(item["id"])

    def is_present(self, col: Dict[str, object], item: Dict[str, object]) -> bool:
        kind, folder, stem = self.item_place(col, item)
        return present(folder, stem, kind)

    def download(self, col: Dict[str, object], item: Dict[str, object]) -> str:
        """One item: 'present', 'downloaded' or 'failed'. Raises QuotaStop/GuardStop/ContainerStop.

        Media and info.json without the completion marker (a run stopped after the
        container and before the checks) are checked in place; any other leftover
        (a partial, a download archive alone) is removed and fetched afresh.
        Implements: SR-114, LLR-1263, LLR-1271, LLR-1280.
        """
        kind, folder, stem = self.item_place(col, item)
        if present(folder, stem, kind):
            return "present"
        in_place = downloaded(folder, stem, kind)
        if in_place:
            mc.log("ottertube: item %s is on the volume without its completion marker (an earlier run stopped "
                   "before its checks); checking it in place" % stem, warning=True)
        else:
            self.check_guard("item %s" % stem)
            # A leftover download archive would make ytdl-sub skip the item.
            self.discard(folder, stem)
            write_json_atomic(os.path.join(self.p.work, "sub.json"), subscription_for(col, item, self.s))
            rc, out = self.run_container(DL_CONTAINER, self.compose(
                "run", "--rm", "--no-deps", "-T", "--name", DL_CONTAINER, "ottertube-ytdl-sub",
                "--config", "/config/config.yaml", "sub", "/work/sub.json"), self.s.item_timeout)
            what = classify_output(out)
            if what:
                raise QuotaStop("%s while downloading %s" % ("EDQUOT" if what == "quota" else "ENOSPC", stem))
            if rc != 0 or not downloaded(folder, stem, kind):
                mc.log("ottertube: item %s FAILED (exit %d); the run continues with the next item" % (stem, rc), warning=True)
                # The tail of ytdl-sub's own account (no credential is ever on its
                # command line or in its config), so the journal says why.
                for line in out.splitlines()[-12:]:
                    mc.log("ottertube: item %s: %s" % (stem, line[:400]), warning=True)
                self.discard(folder, stem)
                return "failed"
        # The container ran for minutes; the probe, the title and the thumbnail
        # are new files on the volume, so the guard runs again before them.
        self.check_guard("item %s metadata" % stem)
        height = None
        if kind == "video":
            height = self.probe_height(col, folder, stem)
            if height is None or height > MAX_HEIGHT:
                self.discard(folder, stem)
                mc.log("ottertube: item %s DISCARDED - %s, the cap is %dp; the run continues"
                       % (stem, "its height could not be read" if height is None else "%dp" % height, MAX_HEIGHT),
                       warning=True)
                return "failed"
        info = os.path.join(folder, stem + ".info.json")
        try:
            changed = retitle(info, str(item["title"]).strip())
        except ValueError:
            self.discard(folder, stem)
            mc.log("ottertube: item %s DISCARDED - its info.json could not be read; the run continues" % stem,
                   warning=True)
            return "failed"
        if changed:
            mc.log("ottertube: item %s: title set from the list" % stem)
        if item.get("thumb") and not any(os.path.exists(os.path.join(folder, stem + e)) for e in THUMB_EXT):
            if self.fetch(str(item["thumb"]), os.path.join(folder, stem)) is None:
                mc.log("ottertube: item %s: the list's thumbnail could not be fetched" % stem, warning=True)
        # Last, and atomically: from here on the item counts as present.
        write_json_atomic(os.path.join(folder, stem + DONE_EXT), {"schema": 1, "height": height})
        mc.log("ottertube: item %s %s" % (stem, "checked in place" if in_place else "downloaded"))
        return "downloaded"

    def probe_height(self, col: Dict[str, object], folder: str, stem: str) -> Optional[int]:
        """The downloaded video's real height, read with the pinned image's ffprobe
        (a bare file URL declares no height, so the format filter cannot hold the
        480p cap by itself). None when it cannot be read."""
        media = next((stem + e for e in VIDEO_EXT if os.path.exists(os.path.join(folder, stem + e))), None)
        if media is None:
            return None
        rc, out = self.run_container(DL_CONTAINER, self.compose(
            "run", "--rm", "--no-deps", "-T", "--name", DL_CONTAINER, "--entrypoint", "ffprobe", "ottertube-ytdl-sub",
            "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height",
            "-of", "csv=p=0", "/library/video/%s/%s" % (col["id"], media)), 120)
        return parse_height(out) if rc == 0 else None

    def prune(self, data: Dict[str, object]) -> None:
        for path in prune_plan(self.p.lib, data):
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(path)
            else:
                os.unlink(path)
            self.counts["pruned"] += 1
            mc.log("ottertube: removed %s (no longer in the list)" % os.path.relpath(path, self.p.lib))

    def shrink_explained(self, listed: int, complete: int) -> bool:
        """True when the indexer may cut the catalogue by more than half because
        the LIST shrank: it names fewer items than the served catalogue holds, and
        at least half of what it names is complete on the volume. Downloads that
        went missing unexpectedly keep the indexer's protection (SR-115).

        Implements: SR-115, LLR-1281.
        """
        before = catalog_items(os.path.join(self.p.catalog, "catalog.json"))
        if before <= 0 or listed >= before:
            return False
        if 2 * complete < listed:
            mc.log("ottertube: the list names %d items (the catalogue holds %d) but only %d are complete: "
                   "the indexer's shrink protection stays on" % (listed, before, complete), warning=True)
            return False
        mc.log("ottertube: the list names %d items, the catalogue holds %d: the indexer may shrink it (--allow-shrink)"
               % (listed, before))
        return True

    def index(self, allow_shrink: bool = False) -> int:
        self.check_guard("the indexer")
        args = ["node", "tools/catalog/build-catalog.mjs", "--approved", "/work/approved.json",
                "--video-root", "/library/video", "--music-root", "/library/music", "--publish", "/library/publish",
                "--out", "/catalog/catalog.json", "--require-hardlink", "--allow-missing",
                "--grace-days", str(self.s.grace_days)] + (["--allow-shrink"] if allow_shrink else [])
        rc, out = self.run_container(INDEX_CONTAINER, self.compose(
            "run", "--rm", "--no-deps", "-T", "--name", INDEX_CONTAINER, "ottertube-indexer") + args, 900)
        for line in out.splitlines()[-30:]:
            mc.log("ottertube indexer: %s" % line)
        if classify_output(out):
            raise QuotaStop("the indexer hit the quota")
        return rc

    def record(self, started: float, result: str, **extra) -> None:
        if self.refused:  # logs/ is on the volume the guard refused (LLR-1283)
            mc.log("ottertube: logs/last-run.json not written: the scratch guard refused the volume", warning=True)
            return
        try:
            write_json_atomic(os.path.join(self.p.logs, "last-run.json"),
                              run_summary(started, self.clock(), result, self.counts, **extra))
        except OSError as exc:
            mc.log("ottertube: could not write logs/last-run.json (%s)" % exc.strerror, warning=True)

    def run(self) -> int:
        """The whole run. Exit: 0 ok, 1 an item or the indexer failed, 2 invalid
        list, 75 quota/space, 76 guard refused."""
        started = self.clock()
        try:
            self.check_guard("the run")
            data = self.load_list()
            self.ensure_dirs()
            empty_dir(self.p.work)
            fetched = False
            for col, item in iter_items(data):
                if fetched and not self.is_present(col, item):
                    # Polite to the source: a pause between two downloads, never
                    # before the first and never for an item already here.
                    self.sleep(self.s.pause_sec * (0.5 + random.random()))
                state = self.download(col, item)
                self.counts[state] += 1
                fetched = fetched or state != "present"
            self.prune(data)
            listed = complete = 0
            for col, item in iter_items(data):
                listed += 1
                complete += int(self.is_present(col, item))
            allow_shrink = self.shrink_explained(listed, complete)
            self.check_guard("approved.json")
            write_json_atomic(os.path.join(self.p.work, "approved.json"), approved_for(data))
            rc = self.index(allow_shrink)
            result = "ok" if rc == 0 and self.counts["failed"] == 0 else "failed"
            if rc != 0:
                self.alert("ottertube-index", "HomeHub: OtterTube catalogue not rebuilt",
                           "the indexer exited %d; the previous catalogue is still served" % rc)
            elif self.counts["failed"]:
                self.alert("ottertube-items", "HomeHub: OtterTube downloads incomplete",
                           "%d item(s) failed; see journalctl -u homehub-ottertube-download" % self.counts["failed"])
            self.record(started, result, indexer_exit=rc)
            mc.log("ottertube: run %s - %s" % (result, ", ".join("%s %d" % kv for kv in sorted(self.counts.items()))),
                   warning=result != "ok")
            return EXIT_OK if result == "ok" else EXIT_FAILED
        except Invalid as exc:
            mc.log("ottertube: REFUSED - %s" % exc, warning=True)
            self.alert("ottertube-list", "HomeHub: OtterTube list refused", str(exc))
            return EXIT_INVALID
        except GuardStop as exc:
            mc.log("ottertube: STOPPED - the scratch guard refused %s" % exc, warning=True)
            self.alert("ottertube-guard", "HomeHub: OtterTube downloads stopped (scratch volume)", str(exc))
            return EXIT_GUARD
        except ContainerStop as exc:
            mc.log("ottertube: STOPPED - %s; no further container is started" % exc, warning=True)
            self.alert("ottertube-container", "HomeHub: OtterTube run stopped (a container would not go)",
                       "%s. The unit's ExecStopPost removes it; check docker ps." % exc)
            self.record(started, "failed", reason=str(exc))
            return EXIT_FAILED
        except (QuotaStop, OSError) as exc:
            if isinstance(exc, OSError) and not is_quota_error(exc):
                raise
            # Our own temporary files go (a partial download among them), on the
            # volume the guard still accepts; nothing is written anywhere else,
            # and the unit does not retry (exit 75).
            self.clean_work("after the quota stop")
            what = str(exc) if isinstance(exc, QuotaStop) else "%s writing on the volume" % errno.errorcode.get(exc.errno, exc.errno)
            mc.log("ottertube: STOPPED - %s; the ottertube budget on /srv/scratch is full" % what, warning=True)
            self.alert("ottertube-quota", "HomeHub: OtterTube downloads stopped (budget full)",
                       "%s. Remove items from the list or raise HOMEHUB_SCRATCH_QUOTA_OTTERTUBE_MIB." % what)
            self.record(started, "quota", reason=what)
            return EXIT_QUOTA
        finally:
            # Never under a container that may still be writing (ContainerStop),
            # never after a guard refusal, and only on the volume the guard
            # accepts now: the next run empties work/ before it starts anything.
            if self.current is None and not self.work_cleaned:
                self.clean_work("after the run")


def serve_check(paths: Paths, guard: Callable[[], Tuple[bool, str]]) -> Tuple[bool, str]:
    """The media server's ExecStartPre: the volume is ours and its two read-only
    sources exist (they are long-syntax binds with create_host_path: false, so a
    missing one would fail the start anyway; this says which). SR-116."""
    ok, reason = guard()
    if not ok:
        return False, "scratch guard: " + reason
    for d in (paths.publish, paths.catalog):
        if not os.path.isdir(d):
            return False, "%s does not exist yet (the first download run creates it)" % d
    return True, "ok"


def status(paths: Paths, clock: Callable[[], float] = time.time) -> Dict[str, object]:
    """Read-only facts for deployment-state (SR-117)."""
    out: Dict[str, object] = {"schema": 1}
    last = mc.load_json(os.path.join(paths.logs, "last-run.json"), {})
    out["last_run"] = last or None
    if last.get("finished"):
        out["last_run_age_sec"] = int(clock() - last["finished"])
    try:
        out["catalog_age_sec"] = int(clock() - os.stat(os.path.join(paths.catalog, "catalog.json")).st_mtime)
    except OSError:
        out["catalog_age_sec"] = None
    # Each inode once: the publication is hard links to the downloads.
    seen, total = set(), 0
    for top, _dirs, files in os.walk(paths.lib):
        for name in files:
            try:
                st = os.lstat(os.path.join(top, name))
            except OSError:
                continue
            if (st.st_dev, st.st_ino) not in seen:
                seen.add((st.st_dev, st.st_ino))
                blocks = getattr(st, "st_blocks", None)  # absent on Windows (the dev-PC tests)
                total += blocks * 512 if blocks is not None else st.st_size
    out["lib_mib"] = total // MIB
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--env-file", default=mc.DEFAULT_ENV_FILE)
    p.add_argument("--root", default="/", help="prefix for /srv/scratch (tests only)")
    p.add_argument("--stack", default="/opt/homehub/stack")
    sub = p.add_subparsers(dest="verb")
    sub.add_parser("condition")
    cl = sub.add_parser("check-list")
    cl.add_argument("--list")
    sub.add_parser("run")
    sub.add_parser("serve-check")
    sub.add_parser("status")
    args = p.parse_args(argv)
    env = mc.read_knobs(args.env_file)
    settings = Settings(env)
    paths = Paths(args.root, args.stack)
    if args.verb == "condition":
        mc.log("ottertube: OTTERTUBE_ENABLED is %s" % ("true - starting" if settings.enabled else "not true - clean skip"))
        return 0 if settings.enabled else 1
    if args.verb == "check-list":
        path = args.list or paths.list_file
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "problems": ["the list could not be read: %s" % exc.__class__.__name__]}))
            return EXIT_INVALID
        plan, problems = validate_list(data, settings)
        print(json.dumps(dict(plan, ok=not problems, problems=problems), sort_keys=True))
        return EXIT_OK if not problems else EXIT_INVALID
    scratch_settings = scratch.Settings(env)
    guard = lambda: scratch.guard(WRITER, scratch_settings, scratch.Paths(args.root))  # noqa: E731
    if args.verb == "run":
        runner = Runner(settings, paths, scratch_settings, args.env_file, guard=guard,
                        identity=lambda: scratch_identity(scratch_settings, args.root))

        def on_term(signum, _frame):
            # A stop (BindsTo= when the mount goes, or systemctl stop) must not
            # leave a container writing: remove it, then end the run.
            if runner.current:
                subprocess.run(["docker", "rm", "-f", runner.current], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30, check=False)
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGHUP, on_term)
        return runner.run()
    if args.verb == "serve-check":
        ok, reason = serve_check(paths, guard)
        mc.log("ottertube: media server %s" % ("may start" if ok else "REFUSED - " + reason), warning=not ok)
        return EXIT_OK if ok else EXIT_GUARD
    if args.verb == "status":
        print(json.dumps(status(paths), sort_keys=True))
        return EXIT_OK
    p.print_help()
    return EXIT_INVALID


if __name__ == "__main__":
    sys.exit(main())
