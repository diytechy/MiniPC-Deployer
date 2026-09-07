# Panel capabilities integration, 2026-09-06

The Owner-approved OfficeWallNaglight plan now has opt-in deployment support.
No live machine, credential, biometric enrollment, Docker service or ISO was
changed. The active project gate remains G1; no release gate was advanced.

Implemented under SR-016/SR-017:

- `PANEL_ACCESS_ENABLED` selects mutually exclusive legacy/protected Caddy wall
  API routes. Protected mode denies the old `/api/*` route and forwards `/v1/*`
  to the session gateway without injecting identity. Default remains false.
- The gateway has a separate compose overlay so its optional runtime image is
  absent from the default core-stack bake/start set. A policy validator refuses
  mismatched hub/renderer protection and public access credentials.
- The panel's explicit offline installer uses a complete hash-locked wheelhouse,
  dedicated sensor account, private state, narrow D-Bus policy and UID-checked
  socket. Python venv, ffmpeg and BlueZ apt prerequisites are in the wall package
  list; Python wheels and digest-pinned models remain offline build inputs.
- `WALL_HOST_CONFIG` carries only a path through kiosk.env to Electron. The host
  credential file is panel-owned 0600; renderer JSON holds no access credential
  in protected mode.
- Camera-off stops the service's camera owner before unloading uvcvideo. A busy
  module now fails verification explicitly; it is never reported successfully
  disabled. The service restarts with the false hardware gate for Bluetooth use.

Exact artifact staging, private provisioning, coordinated Caddy cutover, recovery
constraints and panel installation are in [the operator runbook](../stack/panel-access/README.md).
That runbook is also the list of work still required for an installed deployment:
source support and a stopped/disabled feature are not proof of runtime enforcement.

Validation performed: nine targeted pytest cases pass; `validate_config.py` reports
`ALL CONFIG CHECKS PASSED`; all three changed/new shell scripts parse with Bash;
the active G1 smoke harness reports PASS. That harness has one pre-existing skipped
smoke test and 24 traceability orphans at G1, so this is not a full release test.
No Docker/Caddy adaptation or route run, systemd/D-Bus install, offline Linux wheel
install, seeded reimage, camera hardware or Bluetooth/audio coexistence test was
performed. Presence/backlight arbitration and trusted backlight-only lock events
remain explicit integration dependencies in the runbook.
