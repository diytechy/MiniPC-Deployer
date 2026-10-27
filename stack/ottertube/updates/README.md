# updates/ - OtterTube's update channel (SR-125, IF-075)

Caddy serves three names from this directory, read-only, at
`https://<OTTERTUBE_HOST>/ottertube/` (docker-compose.yml mounts
`./ottertube/updates`; Caddyfile `@ot_update`, `@ot_update_exe`):

| URL | File | Headers |
|---|---|---|
| `/ottertube/update.json` | the manifest OtterTube's `npm run update:manifest` wrote: `{ schema 1, product, version, commit, dirty, builtAt, file, size, sha256 }` | `Cache-Control: no-cache`, no `Content-Encoding` |
| `/ottertube/update.json.sig` | the base64 ed25519 signature of `update.json`'s exact bytes | `Cache-Control: no-cache`, no `Content-Encoding` |
| `/ottertube/OtterTube-<commit7>.exe` | the portable exe the manifest names (`^OtterTube-[0-9a-f]{7}\.exe$`) | no `Content-Encoding` (the app checks Content-Length against `size`) |

`update-<os>-<arch>.json` and its `.sig` (`linux|darwin|windows`,
`x64|arm64`) are **reserved** for later platforms and served once present;
`update.json` stays the Windows schema-1 file for good. Every other name here
(this README, a publish's temporary file) has no route.

It ships with this README only (`.gitignore`). Nothing on the hub writes it but
the publisher, HomeHub `scripts/deploy/Publish-OtterTubeUpdate.ps1`, which copies
the three files byte for byte (scp, never a text tool: the signature covers the
manifest's exact bytes), **the exe first, then `update.json.sig`, then
`update.json`**, each to a temporary name and renamed into place, and then
compares their sha256 on the dev PC, on the hub and over https.

Write INTO this directory; never replace the directory itself (that detaches
Caddy's bind mount until caddy is recreated). The site knob
(`OTTERTUBE_SITE_ENABLED`, empty: `OTTERTUBE_ENABLED`) gates these routes with
the rest of the site; the video library's knob does not.

**Not backed up** (HomeHub `scripts/deploy/storage-map.md`): after a reimage the
directory is back to this README and every OtterTube simply starts as it is.
Re-send the last release from the dev PC (the publisher with the release's
`release/hub/` folder).

**Rollback by hand:** delete `update.json` and `update.json.sig` (clients then
see "not-found" and start as they are), or publish the previous release's three
files again - clients never take a build older than their own, so a re-published
older release only stops newer devices being offered anything.
