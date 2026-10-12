# Opt-in panel private access and sensors

Implements the Owner-approved 2026-09-06 panel plan under SR-016/SR-017. The normal
image remains in its existing legacy mode; no service, PIN, camera capture,
enrollment or Bluetooth discovery is enabled by the source change alone.

`PANEL_ACCESS_ENABLED=false` in the hub `.env` selects Caddy's existing wall API
route. `true` selects the protected route: every `/api/*` request is refused and
`/v1/*` reaches the access gateway without Caddy injecting tracker identity.
The gateway sets that identity after checking its session/device registry. An
invalid selector fails Caddy adaptation; it cannot silently select the legacy
route. The separate authenticated tracker UI and feeder services are unchanged.
The mechanism uses Caddy's [environment substitution](https://caddyserver.com/docs/caddyfile/concepts#environment-variables)
before [snippet import](https://caddyserver.com/docs/caddyfile/directives/import).

The gateway is in `docker-compose.access.yml`, an explicit overlay outside the
default stack's image set. An optional gateway image missing from an ISO therefore
cannot block the core stack. This overlay is also not automatically used by the
existing ISO firstboot path: enabling private access remains an explicit,
coordinated install until the provisioning secrets and offline artifacts have
been rehearsed in the lab. Do not set the knob in a reimage payload without that
complete installation path. The protected route refuses/502s if its gateway is
absent; it never falls back to direct tracker access.

First boot treats gateway artifact ambiguity, validation failure and publication
failure as a loud optional-feature failure. It removes any previously staged
gateway application and continues to core Compose startup. This prevents stale
private-access code from surviving a rejected release without making an optional
accessory a boot dependency for DNS, Caddy, Actual or the tracker.

The ISO payload is authoritative for `panel-access/app` on every boot. First boot
revalidates and republishes it even when that directory already exists. Editing
the staged application in place is unsupported because the next boot restores
the reviewed ISO version; build and stage a new coherent release instead.

The shell, site and gateway source archives do not close the gateway container's
runtime image. `PANEL_ACCESS_IMAGE` remains a major-version example until an
operator pins a digest, exports that exact image into the offline image payload
and rehearses loading it. Protected access is outside offline closure until those
steps are complete.

## The operator install path on a running hub

The two paths above are for an ISO: `install-gateway.py` is the validator and
`stage-gateway.sh` is firstboot's wrapper around it. Neither can update the
gateway on a hub that is already up, which is how that hub came to hold one
unextracted tarball in `/opt/homehub/wall-gateway`, a staged application at a
different revision than the served site, and no container at all.

`gateway-release.py` is that missing path: one tarball in, a validated release
out, the previous release kept, activation gated on the gateway's own `/health`
and rolled back automatically when it fails. `install-gateway-state.sh` prepares
`/var/lib/panel-access` and installs the feed token — and only the feed token.

    sudo bash   panel-access/install-gateway-state.sh --feed-token-file <private path>
    sudo python3 panel-access/gateway-release.py install --archive <tarball>
    sudo python3 panel-access/gateway-release.py status

The full procedure, including the Owner-only PIN bootstrap, device credential
issue, rotation and uninstall, is `docs/runbooks/panel-access-gateway.md`.
Manual unpacking into `app/gateway` is superseded by it: the release script
keeps the retention and the markers that make a rollback possible, and a hand
copy leaves neither.

## Derived AI rings (`gaugeRings`)

`install-renderer-config.py` also derives the panel's AI ring list from the hub
`.env`, so adding a usage source is the only edit (SR-081, LLR-1121..1125). The
panel reads `gaugeRings` from the hub-served `config.json` once at boot, so a
change reaches the wall only after the kiosk shell restarts.

- **Input.** `AI_USAGE_ENABLED` and `AI_USAGE_SOURCES` from `--env`. When the
  file mentions `AI_USAGE_`, EVERY line must be one of: blank; a comment (first
  non-space character `#` or `;`, where "space" is only space, tab and CR, so a
  Unicode space makes the line an ordinary non-canonical line); or a canonical assignment `NAME=VALUE` at
  column 0 whose VALUE is exactly one of: bare (no quote or backslash anywhere),
  one double-quoted string, or one single-quoted string, with nothing outside the
  quotes. This is a positive whitelist: it is what the materialiser and
  `stack/sleep/apply-env-lines.py` write, and `stack/.env.example` passes it (a
  test asserts that). Of the lines that pass, only the FINAL assignment of each key
  counts. An unquoted value is trimmed of space, tab and CR only (what systemd trims,
  not Python's Unicode whitespace), and the values of `AI_USAGE_ENABLED` and
  `AI_USAGE_SOURCES` must be printable ASCII (plus tab and space) or the file is
  treated as ambiguous; other keys may hold any text. Unless
  `AI_USAGE_ENABLED` is exactly `true` the destination is byte-identical to the
  source (a value other than `true`/`false` logs a line).
- **Source list.** The renderer calls the feeder's own `enabled_sources()`
  (`stack/ai-usage/ai_usage_feeder.py`, loaded by path), so it cannot disagree
  with what runs: unset, empty or unquoted-whitespace gives the feeder's default
  list; quoted whitespace or commas-only gives an empty list, and every derivable
  block then becomes `{"slots": [], "unlisted": "hide", "derivedFrom": ...}`.
- **Fail-safe.** If any line fails the whitelist (indentation, `export`, a space
  before `=`, any backslash, a stray, unmatched or multiline quote, a line that is
  not an assignment), assignment boundaries are ambiguous and nothing is parsed.
  The same fail-safe applies when the feeder refuses the list (an unknown or
  mis-cased name) or cannot be loaded: a derivable block (absent, marked, or the
  shipped default) is replaced by the shipped default `{"slots": [], "unlisted":
  "append"}` WITHOUT the marker, so the wall shows every served ring and no stale
  derived block survives. One `WARNING` line names the first failing line number
  and its key (or "non-assignment line"), never a value. An Owner-written block
  is left alone. systemd accepts some of these forms; the renderer deliberately
  does not try to follow it there. Consequence: a materialised secret containing a
  quote or backslash makes the whole `.env` fail the whitelist on a hub that uses
  AI usage, and the rings then show unfiltered.
- **What disables derivation.** A backslash (comment lines included, because
  systemd continues a comment that ends in one onto the next line), a quote, or
  any other non-canonical line anywhere in a `.env` that mentions `AI_USAGE_`
  disables derivation on EVERY boot. A `WARNING` is logged, and the wall then shows
  every served ring (the unmarked append default). Fix the offending line, then
  re-run the converge command below.
- **Output.** `"gaugeRings": {"slots": [...], "unlisted": "hide", "derivedFrom":
  "AI_USAGE_SOURCES"}`. Slot order is claude, claude2, codex, grok, gemini,
  opencode, freellmapi, filtered to the sources the feeder runs (names the feeder
  refuses never get this far; see Fail-safe). Names are de-duplicated. `claude`
  and `claude2` carry `mark: "claude"`, and badges `"1"`/`"2"` only when both are
  listed. `"hide"` makes the rings follow the sources exactly. FreeLLMAPI gets no
  special handling here.
- **Authority.** The SOURCE file (`/opt/homehub/site/config.json`) is never
  modified. The destination is regenerated from source plus `.env` on every run
  (firstboot runs it on every boot). Everything else in the document is
  preserved in order; the output uses the source's indentation.
- **Ownership.** A source `gaugeRings` block is derived over only when it is
  absent, carries `derivedFrom`, or equals the shipped default
  `{"slots": [], "unlisted": "append"}`. Any other block, including
  `{"slots": [], "unlisted": "hide"}`, is Owner-written: it is installed exactly
  as written and one `gaugeRings:` line is logged.
- **Faithfulness.** When a block is derived, the result is value-faithful, not
  byte-faithful: JSON is re-serialised with ASCII `\u` escapes for non-ASCII
  text, LF line endings, no BOM, and the source's indentation. The output is
  re-validated; if the derivation or that validation fails for any reason the
  source is installed unchanged with a `WARNING` line, so firstboot never aborts
  on it.
- **Hand-authoring rings.** Put your own `gaugeRings` block in the source copy,
  without `derivedFrom`. Slot fields are `provider`, optional `mark`, optional
  `badge`; see OfficeWallNaglight `docs/config-reference.md`.
- **Converge in place:**

      sudo python3 install-renderer-config.py --env /opt/homehub/stack/.env \
          --source /opt/homehub/site/config.json \
          --destination /opt/homehub/stack/wall-shell/config.json

  then restart the kiosk shell to see it.

## Hub preparation and coordinated cutover

1. Build the matching OfficeWallNaglight site, app and gateway payloads. Stage
   the gateway artifact's `gateway/` folder at `stack/panel-access/app/gateway/`
   **through `gateway-release.py`, not by hand.**
   The tarball root is `access/`: inspect its members/digest, then strip exactly
   that one root while unpacking into `stack/panel-access/app/` (the resulting
   entry must be `app/gateway/server.mjs`, not `app/access/gateway/server.mjs`).
   Application source stays in that repo. Obtain and pin `PANEL_ACCESS_IMAGE`
   to a reviewed Node 22+ image digest, save it with the offline payload, and
   load it before cutover. No image pull is required at first boot.
2. Follow that artifact's `gateway/README.md` to establish a PIN and device
   registration through the private SSH administration path. Mount persistent
   state at `/var/lib/panel-access`; owner uid/gid 1000:1000, directory 0700,
   `state.json.enc`, `state.key`, and `feed-token` each 0600. `feed-token` is the
   existing tracker mutation credential, delivered as a private file rather than
   renderer config. Keep the gateway wrapping/state key and encrypted state out
   of the web root. Never put a PIN directly in command arguments or shell history.
3. Prepare candidate hub env with `PANEL_ACCESS_ENABLED=true` and renderer JSON
   with `ACCESS_ENABLED:true` and no `FEED_TOKEN`, device credential or session.
   HomeHub/Personal materialization must produce the pair together. Preflight
   without changing the live files:

   ```sh
   python3 panel-access/validate-panel-access.py --env /private/candidate.env \
     --config /private/candidate-config.json
   docker compose --env-file /private/candidate.env -f docker-compose.yml \
     -f panel-access/docker-compose.access.yml config --quiet
   ```

4. Complete the panel preparation below before cutover. Schedule a brief proxy
   interruption: stop Caddy, install the validated env/config pair and matching
   static site, then use both compose files to start the gateway and recreate
   Caddy. Stopping Caddy before replacing the pair prevents an interval where a
   renderer advertises protection while the legacy route remains reachable.
   Validate Caddy with the candidate environment and pinned image before startup.
   No automatic rollback to the unauthenticated route is provided. A failed
   cutover stays closed until the owner fixes it over SSH.
5. From the panel, prove legacy `/api/today`, `/api/check`, `/api/count`, feed,
   export and arbitrary paths are refused; prove `/v1/today` and mutation routes
   refuse absent/expired/revoked sessions; test a real PIN unlock and lock during
   an outstanding read. The gateway has no published host port. Preserve the
   Caddy LAN-only port and actual-peer `/32` gate during all tests.

The runtime image is currently a configurable input with a major-version example,
not a digest pinned by this source change. Gateway state backup/recovery also
needs its own encrypted-artifact policy; blindly restoring old state can restore
revoked credentials. Do not add it to generic flat config backups by accident.

## Panel preparation

The app artifact now carries `runtime/resources/app/sensors/`. Its Python runtime
is a separate offline wheelhouse: on a matching Linux/Python target resolve the
artifact's `sensors/requirements.txt` into a complete `requirements.lock` including
all transitive dependencies, exact versions and wheel hashes; stage every wheel.
The installer uses `--no-index --require-hashes --only-binary=:all:` and fails if
anything is absent. Model binaries are a separate, digest-pinned private build
input. No models, biometric templates or setup credentials ship in this repo.

The wall apt list includes `python3-venv`, `ffmpeg` and `bluez`; rebuild its baked
apt closure when preparing the new image. The existing image builder already
stages the whole tracked `stack/` payload, so the explicit installer and unit are
available without adding an automatic enabled service.

Prepare a private 0600 host JSON using the app's broker schema, including
`enabled:true`, the HTTPS gateway origin, its registered device ID/credential,
`accessMode` set to the same `read-protected` or `write-only` value as
`PANEL_ACCESS_MODE`, and `sensorSocket:"/run/wall-sensors/service.sock"`.
The host-side mode is used only to keep public music visible during a cold-start
gateway outage; tracker reads stay read-protected until the gateway confirms its
policy. The installer rejects an absent or unknown value rather than guessing.
Transfer it through the private administration path, then run on the panel:

```sh
sudo bash /opt/wall-panel/stack/autoinstall/wall/install-wall-capabilities.sh \
  --host-config /private/host.json --wheelhouse /private/wall-wheels
```

The installer verifies prerequisites, installs only offline wheels, creates a
dedicated `wall-sensors` account, installs the narrowly scoped BlueZ D-Bus policy,
and starts its unit. It grants the panel account socket-group access but keeps
sensor state 0700 under the service account. The socket also checks the configured
panel UID. Broker host config is copied to `/etc/wall-panel/host.json`, owner
panel:panel and mode 0600; the renderer has no Node/file access to it.

Set `WALL_HOST_CONFIG=/etc/wall-panel/host.json` in the private `wall.env`, rerun
`wall-firstboot.sh`, and restart the kiosk login session so its supplementary
groups refresh. `kiosk.env` contains only the path; `wall-kiosk.sh` passes it as
`PANEL_HOST_CONFIG`. No secret is put in the app command line or renderer JSON.
An unreadable/invalid enabled host config fails private access closed.

The image now makes camera hardware and the sensor service available regardless
of gateway configuration. Firstboot removes only its own retired
`wall-camera-off.conf`; it never opens the device. Saved schema-v2 sensor config
and `cameraConsentVersion:1` control capture. Face also requires both private
models to match `/opt/wall-sensors/models/manifest.json`; missing models disable
face while motion, Bluetooth and PIN remain available.

## Evidence and remaining hardware work

`validate-panel-access.py` tests cover policy mismatch, invalid booleans and
credential rejection without printing their values. The config checker verifies
knob propagation and payload presence; shell files are parse-checked. These are
configuration checks, not an installed gateway/Caddy or Linux sensor run.

Still required before deployment is claimed complete: Docker/Caddy route tests,
Linux service/socket/D-Bus checks, an offline dependency install, a seeded image
rehearsal, and actual panel capture/model performance, face rejection and Bluetooth
audio coexistence. Presence-to-backlight arbitration is still a separate
integration dependency: `wall-sleep.sh` is currently the schedule brightness
writer. Backlight-only sleep must generate a trusted lock/wake event as well;
it does not trigger Electron's suspend event. No generic sensor daemon can be
assumed to provide either capability merely by being installed.
