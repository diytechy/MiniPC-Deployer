# Stakeholder Needs (SN-###)

Owned by the **Stakeholder** hat — whoever the system serves: an end user, an
operator, or **another system** (represented by its owner/integrator).
Plain-language needs + edge-case expectations. Engineering translations live in
`system-requirements.csv` (referenced by `SN-Refs`); do not restate them here.
Priority: **M**=Must · **S**=Should · **C**=Could.

> **Cover the whole lifecycle, not just steady state.** For each need, ask *when
> in the running product's life must this hold?* — **Provision** (before it runs:
> install, dependencies), **Startup** (once per launch: config, migrations), or
> **Runtime** (steady-state serving). Most authors write only Runtime *needs* and
> discover the install/first-run ones late; tag the non-runtime ones with an
> optional `Lifecycle` value (process.md §4 "Lifecycle phase"). The *edge cases*
> below invert the bias — see that section's note.
>
> **Consider the cost, not just the behavior.** Where the scope warrants it, also
> capture **non-functional** needs — performance, memory/size, **cost** (unit/BOM,
> licensing, cloud spend), reliability, security, observability — and route each
> to its home (process.md §9). It's a prompt, not a mandate: skip the categories
> the scope doesn't need.

## Core needs

| SN-ID | Need (plain language) | Why it matters | Priority | Acceptance intent (how we'd know it's met) |
|---|---|---|---|---|
| SN-000 | _EXAMPLE — replace this row; number real needs sequentially (the `-000` id is a placeholder the tooling ignores)._ | | | |

## Edge-case expectations

How the system should behave when things go wrong (the highest-value part — be
specific; the System Engineer turns each into measurable SRs). **Cover every
lifecycle phase — which phase gets neglected depends on the product.** Tool
authors under-write **Provision/Startup** rows (first-run, missing dependency);
authors of products that operate in a live environment — a robot, a server,
anything with bystanders or concurrent actors — under-write **Runtime** rows,
because those failures live in the *operating environment*, not the codebase.
Fill in every phase below or mark it an explicit n/a; delete only rows that
genuinely cannot apply.

| SN-ID | Lifecycle | Scenario | Expected behavior |
|---|---|---|---|
| SN-0xx | Provision | Missing dependency / wrong version | |
| SN-0xx | Provision | First-run setup & discoverable docs / quick-reference | |
| SN-0xx | Startup | Invalid / corrupt / missing config at launch | |
| SN-0xx | Startup→Runtime | Unattended/automated run (never blocks on a prompt at launch **or mid-run**; clear failure) | |
| SN-0xx | Runtime | Interruption / power loss / killed mid-operation | |
| SN-0xx | Runtime | Invalid / corrupt / unsupported input | |
| SN-0xx | Runtime | Resource exhaustion (disk / memory full) | |
| SN-0xx | Runtime | Output target removed / locked / unwritable | |
| SN-0xx | Runtime | Environment changed under the product mid-operation (file edited mid-run; object moved mid-action) | |
| SN-0xx | Runtime | A third party interferes during operation (another process takes the lock; a person or pet enters the workspace) | |
| SN-0xx | Runtime | An intended action is irreversible and its target is ambiguous (delete / overwrite / discard / physical alteration) | |
| SN-0xx | Runtime | Input degraded but not absent (truncated stream, noisy or dirty sensor, partial data) | |
| SN-0xx | Runtime | Task must be abandoned safely partway (user stop, shutdown mid-task) | |
