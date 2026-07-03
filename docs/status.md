# Project Status — Blackboard

Live coordination for the gated process (see [process.md](process.md)). Keep the
**Current State** header short and current; append the audit log below (newest
last) — it is the record, not required reading for every pass.

---

## Current State

- **Active gate:** G1 — Requirements, UX & constraints _(mirror it in the
  one-line `docs/gate` file — `check.py`/CI read that; see process.md §7)_
- **Round:** 1
- **Open items:** _(the few things blocking the current gate — **one bullet per
  item, never inline-enumerated prose**. Give each a stable short id (OI-1,
  OI-2, … — ids are never renumbered; closed items are removed or struck
  through) so a human can cite it from memory; end every bullet with a link to
  the artifact it concerns; keep the two sub-lists below. Any
  deferrals/decisions list follows the same bullet discipline.)_
  - **Needs <human>** _(state the decision wanted, per item)_:
    - OI-1 — decide: keep or drop the legacy export flag →
      [system-requirements.csv](requirements/system-requirements.csv)
  - **In flight** _(driver; no approval needed)_:
    - OI-2 — pinning SR-000's acceptance predicate →
      [system-requirements.csv](requirements/system-requirements.csv)
- **Assumptions (unattended):** _(decisions taken without sign-off while running
  unattended — each to confirm or revert at the next gate; see AGENTS.md "Ask,
  don't assume")_
- **Next action:** _(what happens next + who must approve)_

## Scope (restated from the brief)

- **Goal:**
- **Stakeholders / end user(s):** _(who or what the system serves — humans,
  operators, or another system, represented by its owner)_
- **Active hats:** Stakeholder, UX/Docs, System Engineer, Software Engineer, Test
  Engineer _(+ any domain hats this scope needs, e.g. Network / Security / Data /
  Hardware — see process.md §1)_
- **Supported platforms:** _(Linux / macOS / Windows — drives which setup/check
  launchers must exist)_
- **Constraints:**
- **Non-goals:**
- **Definition of done:**

## Gate Sign-offs

Add columns for any active domain hats. Drop the `G-Release` row for a one-off
deliverable.

| Gate | Stakeholder | UX/Docs | System Eng | Test Eng | Human |
|---|---|---|---|---|---|
| G1 — Requirements/UX/Constraints | PENDING | PENDING | PENDING | n/a | PENDING |
| G2 — Decomposition & Test Coverage | n/a | n/a | PENDING | PENDING | PENDING |
| G3 — Implementation | n/a | n/a | PENDING | PENDING | PENDING |
| G-Release — Release readiness | n/a | n/a | n/a | PENDING | PENDING |
| G-Final — Acceptance | PENDING | n/a | n/a | (evidence) | PENDING |

---

## Audit log

<!-- Append verdict blocks here per process.md §5. Newest at the bottom. -->

### DRIVER — G1 — Round 1 — <YYYY-MM-DD>
Scaffolding created. Starting G1.

<!-- agent-setup --> Agent setup (2026-07-03): agents=`claude`; skills materialized: downstream-resync, gate-advance, registry-hygiene. AGENTS.md remains the canonical, agent-neutral guide (skills are opt-in accelerators, not a process gate).
