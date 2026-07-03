# Stakeholder Needs (SN-###)

Owned by the **Stakeholder** hat (Peter, the homelab operator). Plain-language
needs + edge-case expectations. Engineering translations live in
`system-requirements.csv` (referenced by `SN-Refs`); do not restate them here.
Priority: **M**=Must · **S**=Should · **C**=Could.

This repo is a **config/infra deliverable** (compose, Caddy, autoinstall,
provisioning), scaffolded at the **minimum profile** with a **HIGH** decision
dial (secrets-adjacent infra — ratify often). The spine is kept deliberately
**high-level**: a handful of SN/SR rows about the stack, the image, auth, and
remote management — not deep LLR/TC rows per config file (proportionality
doctrine; the tracker binary's own requirements live in NagLight).

## Core needs

| SN-ID | Need (plain language) | Why it matters | Priority | Acceptance intent (how we'd know it's met) |
|---|---|---|---|---|
| SN-001 | Flash a USB, boot the headless AWOW box, and the whole stack comes up on its own with zero clicks; a reboot or single crash self-heals. | The box is always-on infrastructure; manual babysitting defeats the purpose. | M | Autoinstall + first-boot bring the stack up unattended; every service is `restart: unless-stopped` and health-checked; a power-cycle returns all services within ~1 min. |
| SN-002 | The box is the LAN's DNS + reverse proxy with valid TLS at home and away (split-horizon), plus ad-blocking. | One box owns name resolution and edge TLS for the home services. | M | Technitium serves split-horizon records → LAN IP; Caddy obtains a public cert that validates on-LAN with no warning; blocklists load. |
| SN-003 | The tracker (NagLight) is multi-user and gated by Google sign-in; only allow-listed accounts get in; the app never sees an unauthenticated request. | Multi-user in v1 (Q10.1) needs real identity without building an auth system (D1/D2/D3). | M | oauth2-proxy runs the Google flow, rejects non-allow-listed accounts, and forwards verified identity headers to the tracker whose port is never host-exposed. |
| SN-004 | Peter's single-user services (Actual Budget, the DNS console) stay behind basic_auth — only the multi-user tracker uses OAuth. | Not every service needs Google; keep the simple gate where it fits (D2). | M | Caddy applies basic_auth to actual/dns hosts; the tracker host has none (oauth2-proxy owns it). |
| SN-005 | The headless box is fully manageable over the LAN — navigate, read logs, accept updates, reboot, and ultimately reimage — so Peter never has to physically visit it. | Explicitly a KEY PIECE (Peter, 2026-07-03); the AWOW box is headless. | M | SSH (key-only) + Cockpit web console reachable on the LAN; unattended OS security upgrades; a documented remote `docker compose` workflow; a decision memo for the reimage-over-LAN ladder. |
| SN-006 | Auxiliary LAN-only observability — uptime checks, container log viewing, optional local push notifications — without extra hosting. | Cheap operability wins for a self-hosted box. | S | Uptime-Kuma, Dozzle, and optional ntfy run LAN-only, health-checked, every knob in `.env.example`. |
| SN-007 | The repo is public-facing: no real secret, password, hash, personal email, LAN detail, or Peter's name ever committed — placeholders only. | Q10.6: proceed as if history could go public; WI-4.12 lesson (author metadata leaks too). | M | Only `*.example` templates tracked; `.env`/tokens/allow-list/marker gitignored; commit identity is `diytechy`, not Peter's name. |
| SN-008 | Because there is no Docker on the dev machine, validation is honest about what was and was not exercised. | Overclaiming runtime validation would be a false green. | M | Config-level coverage checks run green; runtime bring-up is recorded as PENDING a Docker host in `docs/status.md` and the stack README. |

## Edge-case expectations

Delete-only rows that genuinely cannot apply; others are marked n/a explicitly.

| SN-ID | Lifecycle | Scenario | Expected behavior |
|---|---|---|---|
| SN-001 | Provision | Autoinstall media missing the payload | `late-commands` copy is best-effort (`|| true`); the box still installs and the operator can drop the stack manually. |
| SN-007 | Provision | First-run docs / discoverability | Stack README + root README + `.env.example` comments name every knob and the Peter manual step; a quick-reference table lists the minimum keys. |
| SN-008 | Startup | `.env` still has `REPLACE_WITH` placeholders | First-boot warns loudly, brings the stack up anyway, and tells the operator to edit `.env` and re-run the idempotent first-boot script. |
| SN-001 | Startup→Runtime | Unattended run must never block on a prompt | Autoinstall is fully non-interactive; provisioning + first-boot are idempotent and non-interactive; failures exit nonzero and land in the journal. |
| SN-001 | Runtime | Power loss / crash mid-operation | `restart: unless-stopped` + the idempotent first-boot unit reconverge on the next boot; Docker volumes persist state. |
| SN-003 | Runtime | A non-allow-listed Google account tries to sign in | oauth2-proxy rejects it; the tracker never receives the request. |
| SN-003 | Runtime | The tracker port is probed directly (bypass attempt) | The port is `expose`-only (bridge), never host-published; trusted headers are only honoured from oauth2-proxy. |
| SN-005 | Runtime | Peter needs to fix a broken box remotely | SSH + Cockpit for live ops; the reimage ladder memo (`REMOTE_MANAGEMENT.md`) covers the worst case — nothing destructive is enabled without Peter's checkbox. |
| SN-002 | Runtime | The AWOW box goes down | Burn-in checklist requires picking a secondary/failover DNS so an outage degrades gracefully rather than killing LAN resolution. |
| SN-005 | Runtime | Reimage-over-LAN is inherently destructive on an ambiguous target | HIGH-RISK: the memo presents options with checkboxes; no GRUB-reinstall/PXE/wipe path is implemented until Peter ratifies one. |
