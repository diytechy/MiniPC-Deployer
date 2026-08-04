# `stack/tracker-seed/` — the default (empty) tracker seed source

This directory exists so `docker-compose.yml`'s

```yaml
- ${TRACKER_SEED_SRC:-./tracker-seed}:/seed:ro
```

always has something to bind. It deliberately contains **no `definitions/`
subdirectory**, which is what makes it inert: NagLight seeds a brand-new user
from `$TRACKER_SEED_DIR/definitions/`, so a box that mounts this one seeds
nothing even if `TRACKER_SEED_DIR` is set by mistake.

**Do not put a household's real definitions here.** They belong in the data
repo (`TRACKER_DATA_REMOTE`) or in the per-user data dir, not in this repo —
the "no product source" constraint applies to definitions as much as to code.

The seeded path this knob exists for is the **A19 two-VM gate**, which points
`TRACKER_SEED_SRC` at `../sim/tracker-seed` — a committable fixture whose
`definitions/drives.md` declares `library-mounted` and `backup-drive-mounted`,
the two check ids the gate asserts the panel renders red. Without it the gate
hub comes up with an empty `/data`, reports `healthy` (the healthcheck probes
`/healthz`, which is data-free by design — SR-040), and the panel renders a
tracker with no items: "no red drive check" and "the drive check is green" look
identical on a wall.
