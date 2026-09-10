# `wall-shell/` — the office-wall shell's static build (payload dir)

**This directory is deliberately empty of product code.** It is the mount point
the `{$WALL_HOST}:{$WALL_PORT}` kiosk site serves as its document root
(`stack/caddy/Caddyfile`, SR-016), bind-mounted read-only into the caddy
container as `/srv/wall-shell`. On the hub box it is
`/opt/homehub/stack/wall-shell/`.

Why it exists at all: NagLight sends **no CORS headers**, so the panel's shell
can only call `/api/*` if it is served from the **same origin** that proxies the
tracker (OfficeWallNaglight `docs/requirements/stakeholder-needs.md` §3.2). This
repo therefore has to be the origin — but it must not contain the app
(the "no product source" constraint, `AGENTS.md`).

## What goes here, and who puts it here

The **built** static shell from `OfficeWallNaglight` — `index.html`, `css/`,
`js/`, and a filled `config.json`. It is consumed as a built artifact exactly as
`naglight:local` is (the IF-001 pattern), registered as **IF-005**.

**The question this file used to ask out loud is answered.** It asked "whether
the *Electron* half is packaged in the same artifact as these static files".
**PKG-1 (2026-08-02, `OfficeWallNaglight docs/design/packaging.md`): one build,
two payloads, one source-commit stamp.**

```
officewall-site-<ver>-g<sha7>.tar.gz            -> HERE  (tar --strip-components=1)
officewall-shell-<ver>-g<sha7>-linux-x64.tar.gz -> the panel, /opt/wall-panel/app
```

Nothing in this repo builds, vendors, or vendors-in the shell — it is **staged**:

- `vmtest/build-seed.sh` folds the **site** tarball into
  `deploy-payload/wall-site/` (`stage_wall_site_into_payload`), and
  `stack/autoinstall/firstboot.sh` step 3d unpacks it into **this directory**
  before `docker compose up -d`, so caddy's read-only bind mount has a real
  document root.
- Absent, it degrades exactly as before: the kiosk site serves 404 at `/` while
  `/api/*` works. That is expected on a checkout without the private sibling
  (this repo is public), and the build says so rather than failing.

**IF-005 secret boundary, resolved 2026-09-10:** this origin may serve only
nonsecret renderer configuration. Panel secrets originate in root-only
`wall.env`, are materialized into the panel-owned 0600 Electron host JSON and
reach only the exact trusted panel document through named IPC. The deployment
preflight rejects those values here in both legacy and protected modes.

## Two things NOT served from here

- **`/media/*`** — the panel's music/frame cache. **Settled by the Owner
  2026-07-29 (OI-15): it is PANEL-LOCAL.** The panel pulls the media itself
  (`stack/autoinstall/wall/wall-sync.sh`, mirror semantics) and the shell's
  Electron host serves `/media/*` from that cache, so this site has no `/media`
  route and never will — a hub-served fallback would be streaming, which is what
  D-W8 chose against.
- **`/music`** — optional Navidrome streaming, a commented `handle_path` stub in
  the Caddyfile gated on the `navidrome` tier-2 profile.
