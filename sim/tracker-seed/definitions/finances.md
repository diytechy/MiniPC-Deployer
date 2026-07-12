---
category: Finances
color_weight: 1.5
items:
  - id: finance-audit
    title: Finance audit reported
    type: automated
    recur: daily
    check: finance-audit    # Finance-Auditor POSTs {check, color, reason, at} (IF-004 here / FA IF-001 / NagLight IF-006)
    horizon: daily
    notes: Color-lane automated item — the finance-auditor container reports a
      daily severity band with a terse de-identified reason. Sim fixture
      mirroring NagLight's example-data/definitions/finances.md so the AWOW-sim
      exercises the severity-report seam end to end (a boolean-lane post to a
      color item, or vice versa, is a 400 by design).
---

# Finances (sim fixture)

The finance-health lane: one automated item fed over the /api/feed color lane.
Fictional, committable — used to prove the Finance-Auditor↔NagLight seam in
the sim; no real finance data is ever involved (FA §3 firewall).
