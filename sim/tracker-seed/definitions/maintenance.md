---
category: Maintenance
color_weight: 1.0
items:
  - id: backup-files
    title: File backup ran
    type: automated
    recur: daily
    check: backup           # the AWOW bash backup service POSTs here (/api/feed)
    horizon: daily
    notes: Automated item — the WI-10.15 backup service POSTs ok=true/false to
      /api/feed with this check id; the row is read-only in the UI. The AWOW-sim
      feed round-trip check (check 6) exercises this path.
---

# Maintenance (sim fixture)

Fictional automated item so the sim exercises the full /api/feed -> /api/today
round-trip. The `check: backup` id is the successor to the legacy backup-ran
feeder (HOMELAB_TOPOLOGY.md step 6). Throwaway sim fixture, not personal data.
