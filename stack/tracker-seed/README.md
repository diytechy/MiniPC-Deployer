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
`TRACKER_SEED_SRC` at `../sim/tracker-seed`. That fixture still carries the
superseded two-drive presentation solely to expose the current implementation's
migration gap. SN-014/SR-018 require its replacement with one combined
file-share/backup item under NagLight IF-010 (this repo's IF-006). A new gate
must distinguish "one healthy combined item" from an empty tracker; it must not
restore `library-mounted` or `backup-drive-mounted` as target-state ids.
