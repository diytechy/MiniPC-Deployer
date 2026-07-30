# `wall-shell/` — the office-wall shell's static build (payload dir)

**This directory is deliberately empty of product code.** It is the mount point
the `{$WALL_HOST}:{$WALL_PORT}` kiosk site serves as its document root
(`stack/caddy/Caddyfile`, SR-016), bind-mounted read-only into the caddy
container as `/srv/wall-shell`. On the AWOW box it is
`/opt/awow-core/stack/wall-shell/`.

Why it exists at all: NagLight sends **no CORS headers**, so the panel's shell
can only call `/api/*` if it is served from the **same origin** that proxies the
tracker (OfficeWallNaglight `docs/requirements/stakeholder-needs.md` §3.2). This
repo therefore has to be the origin — but it must not contain the app
(the "no product source" constraint, `AGENTS.md`).

## What goes here, and who puts it here

The **built** static shell from `OfficeWallNaglight` — `index.html`, `css/`,
`js/`, and a filled `config.json`. It is consumed as a built artifact exactly as
`naglight:local` is (the IF-001 pattern), registered as **IF-005**.

**IF-005 is still `Planned`, and this is what remains:** OfficeWallNaglight ships
source plus a `package.json`, *not* an installer or a release tarball, so there
is no agreed artifact yet — no build command to invoke, no versioned filename, no
place to fetch it from, and no answer to whether the *Electron* half (which runs
on the panel, not here) is packaged in the same artifact as these static files.
Until that contract is written down in `docs/requirements/interfaces.csv`, this
directory stays empty and the kiosk site serves 404s at `/` while `/api/*`
already works. That is the intended half-built state, not a bug.

Nothing in this repo builds, vendors, or vendors-in the shell.

## Two things NOT served from here

- **`/media/*`** — the panel's music/frame cache. **Settled by the Owner
  2026-07-29 (OI-15): it is PANEL-LOCAL.** The panel pulls the media itself
  (`stack/autoinstall/wall/wall-sync.sh`, mirror semantics) and the shell's
  Electron host serves `/media/*` from that cache, so this site has no `/media`
  route and never will — an AWOW-served fallback would be streaming, which is what
  D-W8 chose against.
- **`/music`** — optional Navidrome streaming, a commented `handle_path` stub in
  the Caddyfile gated on the `navidrome` tier-2 profile.
