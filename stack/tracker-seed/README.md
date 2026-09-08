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
`TRACKER_SEED_SRC` at `../sim/tracker-seed`. SN-014/SR-018 reserve one
`file-share-backup-health` item for the file-share/whole-library-backup
contract; no legacy per-drive or backup-run definition is target state.

## Required manual inventory rebaseline after this migration

Replacing several definitions with one intentionally makes the external
`homehub-tracker-defs-guard` inventory **shrink** and report red. That is
expected evidence of a changed definition set, not a reason to weaken or
automatically reset the guard. After the deployed tracker definitions have been
inspected and the single combined item is confirmed, the operator must run:

```sh
sudo homehub-tracker-defs-guard --check
sudo homehub-tracker-defs-guard --baseline
sudo homehub-tracker-defs-guard --check
```

The first command records the pre-baseline discrepancy; the final command must
be green. Never run `--baseline` before inspecting the actual tracker set, and
never automate it in first boot or the timer: that would launder an accidental
definition deletion.
