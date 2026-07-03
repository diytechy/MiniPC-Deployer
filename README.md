# MiniPC-Deployer

<!-- Build this README out from the PROJECT BRIEF at kickoff (the kit's
     KICKOFF_PROMPT.md): one-line purpose, who it serves, what it does, and how
     to evaluate it. The README is the human front door — it exists from day
     one and grows with the project. Replace every "fill in", then delete this
     comment. -->

*(fill in: one-line purpose — who this serves and what it does)*

## Run it

Double-click the launcher for your platform — no commands to remember:

| Platform | Launcher |
|---|---|
| Windows | [run.cmd](run.cmd) |
| Linux | [run.sh](run.sh) |
| macOS | [run.command](run.command) |

Each is a short, readable script that starts the product from the repo root.
The underlying command lives in the launcher's `RUN_CMD` slot — document it
here too once wired: *(fill in: the launch command and what to expect)*. For a
pure library, delete the launchers and this section and describe usage instead.

## Getting started (contributors)

The onboarding ladder (docs/process.md §7) — each rung a readable,
consent-first script that explains itself before acting:

1. **Fresh machine → checkout:** double-click `scripts/onboard.*` (`.cmd`
   Windows · `.sh` Linux · `.command` macOS).
2. **Workstation:** `scripts/dev-setup.*` — detects and reports by default;
   installs only with consent.
3. **Product toolchain:** `scripts/setup.*` — dependencies + the pre-commit
   hook.
4. **Verify:** `scripts/check.*` — the gate harness; green means you're set.

## Development

This repo follows a gated, requirement-traced process. The working brief is
[AGENTS.md](AGENTS.md); the method is [docs/process.md](docs/process.md). Start
with the code map in [docs/architecture.md](docs/architecture.md) and the
current state in [docs/status.md](docs/status.md).
