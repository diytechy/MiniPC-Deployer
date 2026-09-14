# Runbook: the hub's panel access gateway

Group C0a deliverable, 2026-09-13. The gateway is the enforcement boundary for
the panel's private tracker data: with `PANEL_ACCESS_ENABLED=true` Caddy refuses
every legacy `/api/*` request on the wall site and proxies `/v1/*` to this
service, which checks a device credential and a PIN session before it puts the
Owner's identity on an upstream tracker call.

**Nothing in this document has been executed on the hub.** Every command is for
the coordinator or the Owner. Steps marked **[Owner present]** type a secret and
cannot be delegated to a session at all.

Read alongside `stack/panel-access/README.md` (the cutover and its safety
argument), `stack/panel-access/PROVISIONING.md` (the panel half) and the
payload's own `gateway/README.md` (protocol, recovery, limits).

## What the thing is

| | |
|---|---|
| Source | OfficeWallNaglight `gateway/` — six files, Node 22+, **zero npm dependencies** |
| Payload | `officewall-gateway-<version>-g<short>.tar.gz`, third artifact of the same build that emits app and site; root entry `access/` |
| Runs as | container `panel-access`, uid 1000, read-only rootfs, all capabilities dropped, no published host port |
| Listens on | `0.0.0.0:8788` **inside the container network only**; Caddy is the only thing that reaches it |
| Panel reaches it via | `https://<wall host>:<wall port>/v1/*` — the same origin as the kiosk site, `gatewayUrl` in the panel's `/etc/wall-panel/host.json` |
| Needs at start | `feed-token`, `state.key` and `state.json.enc` in `/var/lib/panel-access`, each 0600 and owned by uid 1000, plus `PANEL_USER_SUB` in the hub `.env` |
| Health | `GET /health` → `{ok, protocolVersion}`; `ok` goes false on a storage fault |

There is no "not yet configured" mode. `loadStore()` refuses state that carries
no PIN record, so **creating the PIN is creating the gateway**: an install
before the bootstrap stages code and starts nothing, by design.

## Install or update

The install path is `stack/panel-access/gateway-release.py`. It is idempotent:
running it twice with the same tarball stages nothing the second time and only
reconciles the container.

**What is on the hub, and why the first install is not step 1.** As inspected on
2026-09-13 the served site was `fd64d10`, while the only gateway tarball ever
delivered to `/opt/homehub/wall-gateway` — and the application staged by hand at
`panel-access/app/gateway` — was `f1ac1b7`. `install` refuses that tarball, and
is right to. The gateway you install must be the one built from the **revision
the hub is serving**, so read that first rather than trusting this paragraph:

```sh
sudo python3 /opt/homehub/stack/panel-access/gateway-release.py status   # "site" line
```

then take `officewall-gateway-<version>-g<that short revision>.tar.gz` out of
`OfficeWallNaglight/dist`. If it is not there, rebuild that revision: the
builder emits app, site and gateway together, so the matching payload always
exists for any revision the site could be at.

The gateway never drags the site with it: this lane installs a gateway to match
whatever site is already served. If the site is the thing that is behind, the
paired lane (`HomeHub/scripts/deploy/PANEL_RELEASE.md`) owns it, and it is that
lane's gateway pre-flight — the one that refuses to stage while the hub's
gateway is behind — that this install path exists to satisfy.

1. **Build.** OfficeWallNaglight's clean-source builder emits all three payloads
   from one commit into `dist/`. A `-dirty` suffix in the filename is a refusal,
   not a warning.
2. **Deliver.** Copy the tarball to the hub's existing drop, `/opt/homehub/wall-gateway/`.
3. **Prepare state, once per machine.** Creates the directory; installs the feed
   token from a private file you supply. It writes no PIN, key or credential.

   ```sh
   sudo bash /opt/homehub/stack/panel-access/install-gateway-state.sh \
     --feed-token-file /run/private/feed-token
   ```

4. **Install.**

   ```sh
   sudo python3 /opt/homehub/stack/panel-access/gateway-release.py install \
     --archive /opt/homehub/wall-gateway/officewall-gateway-<version>-g<short>.tar.gz
   ```

   It refuses a payload whose source revision is not **exactly** the revision of
   the site in `/opt/homehub/stack/wall-shell/build-info.json`; that skew is what
   makes `panel_release.py --deploy` refuse to stage, so fixing it here is what
   unblocks the paired lane.
5. **Confirm.**

   ```sh
   sudo python3 /opt/homehub/stack/panel-access/gateway-release.py status
   ```

   `match yes` and, once access is enabled and state exists, `container running`
   with `endpoint /health ok`.

### What install actually does, in order

Validate the archive (root prefix, traversal, symlinks, required members,
oversized stamp, site agreement) → unpack to a scratch tree → compare against
the live tree and stop if identical → publish to `panel-access/releases/<rev>/`
→ stop the container and **prove it is gone** → replace `panel-access/app/gateway`
→ recreate the container → probe `/health` **and** hash `server.mjs` as the
container sees it → **roll back automatically if either fails**, and only then
record the release as active. Retention is two: the active release and the
previous one.

The second half of that proof is not redundant. A detached bind mount answers
`/health` perfectly while serving the old code from a deleted inode, and from
the host the directory looks correct — only the container can say what it is
actually reading.

The container is recreated rather than restarted because the overlay
bind-mounts `panel-access/app/gateway`. Replacing that directory's inode under
a running container detaches the mount, and the container then serves the old
code out of a deleted inode while reporting healthy. Never swap it live.

## Bootstrap the PIN **[Owner present]**

With the container stopped (`status` shows `absent` before the first
bootstrap, which is correct):

```sh
sudo node /opt/homehub/stack/panel-access/app/gateway/setup.mjs \
  --state /var/lib/panel-access/state.json.enc \
  --key   /var/lib/panel-access/state.key \
  --device wall-panel \
  --credential-out /var/lib/panel-access/panel-credential.json \
  --pin-file /run/private/owner-pin
sudo chown 1000:1000 /var/lib/panel-access/state.json.enc \
  /var/lib/panel-access/state.key /var/lib/panel-access/panel-credential.json
```

**Run it as root, then hand the outputs over.** Running it as uid 1000 cannot
work: the PIN file is root-only 0600 by construction, so the service account
could not read its own input. `setup.mjs` writes its three outputs 0600 owned by
whoever ran it, and the gateway's `requirePrivate()` needs them readable by uid
1000 — which is what the `chown` is for, and why skipping it produces a
container that restarts forever with no useful message.

* 6–12 digits. `setup.mjs` refuses a TTY, so the digits are never echoed: write
  them to a root-only file on a tmpfs and remove it immediately afterwards.
  **A session must not generate, suggest, store or read this value.**
* It refuses to overwrite existing state, key or credential files. That refusal
  is the safety net; the only way back is the uninstall below.
* Output is `panel-credential.json` — `{"deviceId","credential"}`. Move it off
  the state directory once the panel has it.

### Provision the panel BEFORE flipping the knob

Do not enable the protected route yet. `PANEL_ACCESS_ENABLED=true` makes Caddy
403 every `/api/*` on the wall site, and a panel that has no `deviceCredential`
cannot use `/v1/*` instead — so flipping the knob first takes the checklist
away from a panel that has no way back. Install the panel's private host JSON
first (next section, `PROVISIONING.md` §1c), confirm the kiosk still works on
the legacy route, and only then continue here.

Then enable and start:

```sh
sudo sed -i 's/^PANEL_ACCESS_ENABLED=false/PANEL_ACCESS_ENABLED=true/' /opt/homehub/stack/.env
sudo python3 /opt/homehub/stack/panel-access/validate-panel-access.py \
  --env /opt/homehub/stack/.env --config /opt/homehub/stack/wall-shell/config.json
cd /opt/homehub/stack && sudo docker compose -f docker-compose.yml \
  -f panel-access/docker-compose.access.yml up -d --force-recreate panel-access caddy
sudo python3 /opt/homehub/stack/panel-access/gateway-release.py status
```

`validate-panel-access.py` must pass **before** Caddy is recreated: the hub env
and the renderer `config.json` have to agree, and the renderer config must carry
no `FEED_TOKEN`. Recreating Caddy is what swaps the `wall_api_false` snippet for
`wall_api_true`; there is no automatic rollback to the unauthenticated route.

### Acceptance PIN for the coordinator

This is the **one** exception to "only the Owner creates the PIN", and it
exists because the Owner wrote it, not because it is convenient. Recorded
verbatim in `HomeHub/docs/PANEL_CURRENT_2026-09-13.md`, Owner feedback row C1:
*"I'm not at the panel; perform the checks by virtualizing key presses.
Provision a PIN as you need."* Without that sentence this section does not
apply and the bootstrap waits for the Owner.

Ruling C1 (Owner, 2026-09-13): while the Owner is away the coordinator may
bootstrap a **temporary** PIN by the same command, from a file it writes to a
root-only tmpfs path and deletes in the same command, and the Owner replaces it
at the panel later with `recover.mjs --action pin`. The temporary value goes in
no report, no commit, no scratchpad and no chat message.

## Issue the panel device credential

The `credential` field of `panel-credential.json` goes into the panel's private
host JSON (`deviceId`, `deviceCredential`, `gatewayUrl`, `accessMode`,
`sensorSocket`, `faceEnabled`) and is installed there by
`install-wall-capabilities.sh`. That whole step is
`stack/panel-access/PROVISIONING.md` §1c; do not duplicate it here. Carry the
file on removable media or a root-only path — never through a repo, a chat
message or a command line.

Additional devices, and rotation without stopping the gateway, go through the
running service instead: `POST /v1/admin/device` with `{pin, action, deviceId}`.
Every admin call verifies a fresh PIN and invalidates all sessions.

## Rotate

| What | How | Who |
|---|---|---|
| PIN, gateway running | `POST /v1/admin/pin` `{pin,newPin}` from the panel | Owner |
| PIN, forgotten | stop the container, `recover.mjs --action pin --pin-file …`, start | **[Owner present]** |
| Device credential | `POST /v1/admin/device` `{pin,action:"rotate",deviceId}`, or offline `recover.mjs --action rotate --credential-out …` | Owner |
| Revoke a lost panel | `POST /v1/admin/device` `{pin,action:"revoke",…}` or `recover.mjs --action revoke` | Owner |
| Feed token | re-run `install-gateway-state.sh --feed-token-file …`, then `docker compose -f docker-compose.yml -f panel-access/docker-compose.access.yml up -d --force-recreate panel-access` (the token is read once at start) | coordinator |
| Gateway code | `gateway-release.py install --archive …` | coordinator |

**Never run `recover.mjs` against a running gateway.** Rotation and revocation
clear the face gallery's wrapping key and require re-enrolment.

## Roll back

```sh
sudo python3 /opt/homehub/stack/panel-access/gateway-release.py rollback
```

Restores the retained previous release, recreates the container and re-probes
`/health`. A failed install rolls back on its own and exits non-zero; this
command is for a release that came up healthy but behaved badly. Retention is
two, so a rollback is available for exactly one generation — take the next
install only after the current one has been accepted.

If `status` prints a **`PENDING`** line, an install was interrupted between
persisting its intent and finishing: the tree on disk may be either release.
Run `rollback`, which re-publishes the previous release deterministically
instead of trusting what happens to be there. If a rollback is itself unhealthy
the script stops and says `recovery required` rather than swapping back and
forth — both release directories are still retained, so read
`docker compose logs panel-access` before deciding which way to go.

Gateway **state** is not versioned by this and must not be restored from a
snapshot on a whim: an old `state.json.enc` restores revoked credentials.
Prefer recovery and re-enrolment (gateway README, "Persistence, limits and
recovery"). Keep it out of the flat config backup.

## Uninstall

```sh
sudo sed -i 's/^PANEL_ACCESS_ENABLED=true/PANEL_ACCESS_ENABLED=false/' /opt/homehub/stack/.env
cd /opt/homehub/stack && sudo docker compose up -d --force-recreate caddy
sudo python3 /opt/homehub/stack/panel-access/gateway-release.py uninstall
```

Order matters: turn the knob and recreate Caddy **first**. The protected route
502s rather than falling back, so removing the application while `true` is still
selected leaves the panel's checklist unreachable instead of merely unprotected.
`uninstall` tears the container down regardless of what the knob now says — by
this point it says `false` while the container is still up, and deleting its
bind-mount source from under it would leave a gateway running that nobody
believes exists.

`uninstall` removes the application and the release history and deliberately
leaves `/var/lib/panel-access` alone. Destroying the PIN, the device registry
and every enrolment is a separate Owner act: `rm -rf /var/lib/panel-access`.
Revert the panel in the same window (`PROVISIONING.md` §1c reversal) or it will
sit on "Unlock service unavailable", which is the correct fail-closed behaviour
and is not a fault to chase.

## What is still not proven

No Docker, Caddy or live gateway run is claimed by this document. The tests
cover the install path's file handling, refusals, idempotency and rollback with
a stubbed docker. The first real install is watched, and its evidence is the
`status` output plus a `/v1/public-status` fetch from the panel.
