---
category: Drives
color_weight: 1.0
items:
  - id: library-drive-present
    title: Library drive mounted
    type: automated
    recur: daily
    check: library-mounted  # homehub-library-health.timer POSTs {check, color, reason}
    horizon: daily
    notes: Color-lane automated item. stack/samba/library-guard.sh sends a band,
      not ok=true/false, because a boolean cannot express "mounted and writable,
      but on the wrong disk" — red = not mounted or read-only, yellow = writable
      but the disk identity did not match, green = identity confirmed. The check
      id here is the contract with library-guard.sh's LIBRARY_FEED_CHECK default;
      an id no item declares is a 400 at /api/feed, which is exactly how these
      two checks posted into the void from 2026-07-30 (A24(i)).

  - id: backup-drive-present
    title: Backup drive mounted
    type: automated
    recur: daily
    check: backup-drive-mounted  # homehub-backup-drive-health.timer POSTs the same shape
    horizon: daily
    notes: The same guard script under a second unit with
      LIBRARY_FEED_CHECK=backup-drive-mounted. It probes /proc/self/mountinfo
      only, so a 10-minute timer never wakes a spun-down disk. Distinct from
      check "backup" (maintenance.md), which is the nightly backup RUN on the
      boolean lane — this one is the DRIVE's presence.
---

# Drives (sim fixture)

> **Legacy migration fixture, not target state.** Owner ruling 2026-09-07 and
> SN-014/SR-018 replace these two visible lanes with one combined
> file-share/whole-library-backup item. They remain temporarily so the current
> sim exposes the implementation work instead of pretending it is complete.

The two drive-presence lanes the current A19 two-VM gate is built around: with no data
drives attached to the hub, both must report **red**, and that red must be
visible on the panel.

Fictional and committable — a throwaway sim fixture in the NagLight
example-data pattern, not personal data. The item ids are sim-flavoured; the
`check:` ids are **not** free to change, because they are the wire contract with
`library-guard.sh` and `homehub-backup-drive-health.service`.

**Why no weight tuning.** `engine.Aggregate` is a **max** over lane scores — the
weights only order the 1-3 offenders named in the overlay text, and never move
the band. One `color: red` report scores 100 and takes the ambient to red on its
own. The corollary matters more than the convenience: **a red screen does not
prove these two lanes are red**, because any stale habit reddens it too. The
gate's assertion is per-item — `reportColor: "red"` on both ids in
`/api/today` — and the screen capture is corroboration, not the criterion.
