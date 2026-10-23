# ottertube — OtterTube on the hub (SN-051)

Implements **SR-113** (the downloader), **SR-114** (finite by construction,
480p), **SR-115** (the catalogue), **SR-116** (the scratch writer rules) and
**SR-117** (the LAN-only site). Interfaces: **IF-072** (the private list this
consumes), **IF-073** (the site OtterTube's apps read), **IF-071** (the scratch
writer contract it follows).

The hub downloads a **curated, finite** list of children's videos onto the NVMe
scratch volume (`/srv/scratch/ottertube/`, a 6 GiB hard project quota), builds
OtterTube's catalogue with OtterTube's **own** indexer, and serves the app, the
catalogue and the videos at `https://<OTTERTUBE_SUBDOMAIN>.<DOMAIN>/ottertube/`
to the LAN only. The list is private HomeHub config; this directory holds only
the mechanism. Default **off** (`OTTERTUBE_ENABLED=false`).

## What runs

| Piece | What |
|---|---|
| `homehub-ottertube-download.timer` | daily at 01:20 (+ up to 10 min), off-peak, not Persistent |
| `homehub-ottertube-download.service` | oneshot: the guard, then `ottertube.py run`. A scratch writer (RequiresMountsFor, BindsTo the mount, no CAP_SYS_RESOURCE). Never restarted. |
| `homehub-ottertube-media.service` | the read-only media and catalogue server (`homehub-ottertube-media`, the hub's caddy-cloudflare image, `caddy file-server` on :8080), bound to the mount |
| compose `ottertube-ytdl-sub` | ytdl-sub, pinned; started by `ottertube.py` ONCE PER ITEM with a one-item subscription file |
| compose `ottertube-indexer` | node, pinned; runs OtterTube's `tools/catalog/build-catalog.mjs` from `indexer/`, no network |
| Caddy `{$OTTERTUBE_HOST}` | the site: `/ottertube/` the web build (`web/`), `/ottertube/catalog.json`, `/ottertube/media/...` proxied to the media server |

All three compose services sit behind `profiles: ["ottertube"]` only to stay out
of firstboot's bulk `up -d`; **never** put `ottertube` in `COMPOSE_PROFILES`
(export-images refuses it). Firstboot (step 6j) installs the units always and
enables the timer and media server only while the knob is `true`.

## On the hub

| Path | Holds | From |
|---|---|---|
| `/opt/homehub/stack/ottertube/` | this directory: `ottertube.py`, units, `ytdl-sub/config.yaml` (presets, public) | the image (this repo) |
| `…/ottertube/config/list.json` | the curated list (IF-072), 0600 root | HomeHub `scripts/deploy/ottertube/list.json`: the image's site/ payload (`ottertube-list.json`), or the coordinator's copy |
| `…/ottertube/indexer/` | `tools/catalog/` + `app/src/catalog/` of ONE OtterTube commit | the coordinator's copy (below); **a reimage does not carry it** |
| `…/ottertube/web/` | OtterTube's `dist/web` of the same commit (`build-info.json` names it) | the coordinator's copy; **a reimage does not carry it** |
| `/srv/scratch/ottertube/lib/{video,music}/<collection>/` | downloads: `<item id>.<ext>`, `.info.json`, thumbnail | ytdl-sub |
| `/srv/scratch/ottertube/lib/publish/` | the publication: hard links of what the catalogue references | the indexer |
| `/srv/scratch/ottertube/catalog/catalog.json` | the catalogue | the indexer |
| `/srv/scratch/ottertube/work/` | the run's temporary files (subscription, approved.json, ytdl-sub's working dir); emptied before and after every run | ottertube.py |
| `/srv/scratch/ottertube/logs/last-run.json` | the last run's result and counts (deployment-state reads it) | ottertube.py |

Scratch: never backed up, never shared (HomeHub `scripts/deploy/storage-map.md`
3c); a reimage recreates the volume empty and the next run downloads the list
again.

## Finite by construction (SR-114)

An item is **one video**: its URL must be https on a host in
`OTTERTUBE_ALLOWED_HOSTS` **and** match that host's single-item rule in
`ottertube.py HOST_RULES` (a NASA asset file; a PeerTube watch page; a YouTube
watch URL without `list=` — YouTube is **not** in the default allow-list, its
terms are the Owner's decision). Every item records `estimateMiB`, `licence`,
`source` and the `basis` URL of the terms relied on; the estimates must total at
most `OTTERTUBE_BUDGET_MIB` (3072, half the quota: a download in flight is
copied once from the work directory). One download per item, yt-dlp skips a
file over `OTTERTUBE_MAX_FILESIZE_MIB`, and a video taller than 480p (read from
the file with ffprobe) is discarded. `python3 ottertube.py check-list --list F`
validates a list without touching anything.

## Coordinator procedures

**Deploying the web build and the indexer** (from ONE recorded OtterTube
commit; repeat for every new OtterTube package):

```bash
# on the dev PC: the artifacts recorded for the commit (dist/web tree hash and the indexer tar)
scp -r dist-web hub@<hub>:/tmp/ot-web && scp ottertube-indexer-<commit>.tar hub@<hub>:/tmp/
# on the hub
sudo install -d -m 0755 /opt/homehub/stack/ottertube/web /opt/homehub/stack/ottertube/indexer
sudo rsync -a --delete --exclude README.md /tmp/ot-web/ /opt/homehub/stack/ottertube/web/   # INTO the dir: never replace it (detaches Caddy's bind)
sudo tar -C /opt/homehub/stack/ottertube/indexer -xf /tmp/ottertube-indexer-<commit>.tar
cat /opt/homehub/stack/ottertube/web/build-info.json                                         # the commit, dirty:false
(cd /opt/homehub/stack/ottertube/web && sudo find . -type f ! -name README.md | LC_ALL=C sort | sudo xargs -d '\n' sha256sum) | sha256sum   # = the recorded tree hash
rm -rf /tmp/ot-web /tmp/ottertube-indexer-*.tar
```

**Turning it on**: `OTTERTUBE_ENABLED=true` in the stack `.env` (and HomeHub
config), install the list, recreate Caddy (new env and bind), re-run DNS
provisioning (its record appears only with the knob), enable the units:

```bash
sudo install -D -m 0600 /tmp/list.json /opt/homehub/stack/ottertube/config/list.json
sudo python3 /opt/homehub/stack/ottertube/ottertube.py check-list         # {"ok": true, ...}
cd /opt/homehub/stack && sudo docker compose up -d --no-deps --force-recreate caddy
sudo bash /opt/homehub/stack/provision/provision-technitium.sh           # "A  ottertube.<domain> → <LAN_IP>"
sudo docker compose --profile ottertube pull ottertube-ytdl-sub ottertube-indexer   # by digest; skipped if baked
sudo systemctl enable --now homehub-ottertube-download.timer homehub-ottertube-media.service
sudo systemctl start homehub-ottertube-download.service                  # the first run, by hand
journalctl -u homehub-ottertube-download -n 80 --no-pager
```

Expected: `list accepted - N collections, M items, estimate E MiB`, one
`item … downloaded` per item, the indexer's `written: /catalog/catalog.json`,
`run ok`; `ottertube.py status` shows the run and the library size. Then from a
LAN client: `curl -sI https://ottertube.<domain>/ottertube/catalog.json` (200),
`curl -s -r 0-99 -o /dev/null -w '%{http_code}' https://ottertube.<domain>/ottertube/media/video/<id>.mp4` (206).

**Rollback**: `sudo systemctl disable --now homehub-ottertube-download.timer
homehub-ottertube-media.service`, `OTTERTUBE_ENABLED=false`, re-run the DNS
provisioning (the record stays until removed by hand in Technitium: delete the
`ottertube.<domain>` zone), recreate Caddy. The downloads stay on scratch until
`sudo rm -rf /srv/scratch/ottertube/{lib,catalog,work,logs}` (data this feature
made).

**Rotating the image** (yt-dlp breaks routinely): resolve the new ytdl-sub
release's index digest against ghcr.io, update `ottertube-images.pin` and the
`.env.example` line together, run `stack/ottertube/tests/ottertube-lab.sh`, commit.

## Tests

* `tests/test_ottertube.py` (pytest, dev PC; TC-1430..TC-1439, TC-1444..TC-1450).
* `tests/ottertube-quota.root-test.sh` (root; TC-1440): the real scratch volume
  and EDQUOT under the hub's own kernel —
  `bash stack/scratch/tests/run-root-suite-in-qemu.sh <vmlinuz> <quota modules> ottertube/tests/ottertube-quota.root-test.sh`.
  SKIPS on WSL2 (no ext4 quota support).
* `tests/ottertube-lab.sh` (root + docker + network; TC-1441..TC-1443): the
  pinned images, one public-domain NASA clip, the real Caddy block. Never on the hub.

## What was measured (2026-10-22, dev PC WSL + QEMU with the hub's 6.8.0-147 kernel)

* The kit's ytdl-sub tag `2026.08.26.post1` exists (ghcr.io HEAD 200; equal to
  `latest` that day). Its image's s6 init writes into `/config`, so the service
  calls `ytdl-sub` directly; the lock file defaults to `/config` and needed
  `lock_directory: /tmp`; `max_downloads 1` aborts before the first download
  (`MaxDownloadsReached`), so the cap is `max_downloads_per_subscription`.
* A bare file URL (NASA) declares neither height nor codec and gets its file
  name as the title: hence `<=?` formats, the ffprobe height check and the list's titles.
* A bind at `/lib` shadows the node image's loader (`exec /usr/local/bin/node: no
  such file or directory`): the library mounts at `/library`.
* The caddy binary carries `cap_net_bind_service=ep`; with `cap_drop: ALL` alone
  the kernel refuses to exec it (exit 255).
* BindsTo= a mount unit that does not exist: systemd refuses to queue the unit
  (`Unit srv-scratch.mount not found`), hence the knob-gated enable.
* Lab: 10/10 (download 240p H.264, retitled, thumbnail; hard-linked publication;
  no refetch; catalogue 200 no-cache; Range 206; CORS; 404s; 302; off-LAN 403;
  real ENOSPC → exit 75). Root suite under the hub's kernel: 8/8.
