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

## Hub preparation and coordinated cutover

1. Build the matching OfficeWallNaglight site, app and gateway payloads. Stage
   the gateway artifact's `gateway/` folder at `stack/panel-access/app/gateway/`.
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
and `sensorSocket:"/run/wall-sensors/service.sock"`. Transfer it through the
private administration path, then run on the panel:

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

The kernel camera switch still belongs to `WALL_CAMERA_ENABLED`. When switched
off, firstboot stops the sensor camera owner before unloading uvcvideo, treats a
busy module as an explicit failure to verify off, and restarts the sensor service
with the new hardware gate (Bluetooth may continue). A sensor config cannot
override the hardware switch. Enabling camera features requires the configured
model manifest and actual hardware calibration, then owner PIN-protected Settings.

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
