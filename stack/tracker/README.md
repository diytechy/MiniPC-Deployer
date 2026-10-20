# `tracker/` — the hub lanes that report into the tracker (plus two reference files)

> **Rewritten 2026-10-19.** This file used to be titled "DEPRECATED reference
> only", which stopped being true when the two live lanes below were added
> here (`tracker-defs-guard.sh` on 2026-08-29, `frame-freshness.sh` on
> 2026-09-07). The deprecated-files text is kept unchanged in the last section.

## Live: two hub lanes, installed by firstboot

`stack/autoinstall/firstboot.sh` installs both on every hub image (the
`install -m` block around line 1876) and enables their timers (around line
1990). Each script's header comment is its spec; read it before changing a
threshold or a band.

| Files | Installed as | What it reports | Interface |
|---|---|---|---|
| `tracker-defs-guard.sh`, `homehub-tracker-defs-health.{service,timer}` (every 30 min) | `/usr/local/sbin/homehub-tracker-defs-guard` | Whether the tracker's item definitions are intact. It reads the definition files straight off the docker volume (so a stopped tracker is still assessable) and compares them with a recorded baseline: anything that disappeared is RED, pure growth refreshes the baseline. It leaves its verdict in `/var/lib/homehub/tracker-defs.state` for `verify-hub.sh` and posts the `tracker-definitions` check to the tracker feed. | IF-036 |
| `frame-freshness.sh`, `homehub-frame-freshness.{service,timer}` (hourly) | `/usr/local/sbin/homehub-frame-freshness` | Whether the picture-frame videos are current with the media that feeds them. GREEN or YELLOW only, and it must post on every run (silence goes RED after three days). Posts the `frame-freshness` check. Spec: `VIDEO_FRAME_GEN_CHECK_PLAN.md` in the HomeHub repo (archived there under `docs/archive/2026-09-18/`). | IF-035 |
| `tests/defs-guard.test.sh` | — | Hermetic test of the guard: a temp directory, no docker, no tracker. | — |

The guard's first run on a fresh box is YELLOW on purpose: there is no
baseline yet, and recording one automatically would hide a later deletion.
Take it deliberately, once the definitions are what the household wants:

```sh
sudo homehub-tracker-defs-guard --baseline
```

## Reference only: the migrated container build

These files (`Dockerfile.deprecated`, `entrypoint.sh.deprecated`) are the
**migrated originals** from `life-tracker/deploy/tracker/`. They are kept for
history and are **not** part of any build here.

The canonical NagLight web-container build now lives in the **NagLight** repo
(WI-10.4, decision D1: the container is NagLight's own release artifact). This
stack consumes the resulting image as `naglight:local`:

```sh
docker build -t naglight:local ../NagLight     # from a sibling NagLight checkout
docker compose up -d                            # in ../ (the stack dir)
```

Or uncomment the `build:` fallback in `../docker-compose.yml` to have compose
build the image straight from a sibling `../../NagLight` checkout.

Dev boxes can let `scripts/ensure-local-images.sh` (repo root) do this instead
— it resolves `naglight:local` via present → sibling build → the declared
`TRACKER_PUBLIC_IMAGE` fallback (SR-006), and fails loudly naming the fixes.
