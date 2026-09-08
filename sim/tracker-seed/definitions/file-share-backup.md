---
category: Health
color_weight: 1.0
items:
  - id: file-share-backup-health
    title: File share and whole-library backup
    type: automated
    recur: daily
    horizon: daily
    notes: The one panel-visible file-share/backup health item (SN-014/SR-018).
      POST /api/backup-state accepts exactly one independent state dimension:
      the monitor sends shareHealth red when /srv/library is missing or its
      representative Samba read fails, and clear on recovery; FileBackup alone
      sends lastSuccess after artifact verification. Drive identity and archive
      target preflight remain internal safeguards and are never separate items.
---

# File share and whole-library backup (sim fixture)

Fictional, committable fixture for the single `file-share-backup-health` item.
It has no legacy `check:` alias: `/api/backup-state` validates the reserved id,
and `/api/today` returns `panelRole: file_share_backup` so clients never infer
the role from title, category, or a generic check id.
