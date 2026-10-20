This directory is where the hub's private access gateway runs from: `app/gateway`
is the live copy the compose overlay bind-mounts. Do not populate it by hand.

- On a running hub, `../gateway-release.py install --archive <tarball>` publishes
  a verified OfficeWallNaglight gateway release here, keeps the previous one
  under `../releases/`, and recreates the container (a hand copy keeps neither
  the retention nor the rollback markers). Procedure:
  `docs/runbooks/panel-access-gateway.md`; summary: `../README.md`.
- At first boot, `../stage-gateway.sh` stages it from a delivered gateway
  tarball, and firstboot removes this directory when no tarball is present.

Application source belongs to OfficeWallNaglight and is not copied into this
repository. Never stage gateway state, PINs or credentials here.

(Rewritten 2026-10-19. Until then this file said to place the artifact's
`gateway/` directory here by hand, which `../README.md` records as superseded
by `gateway-release.py`.)
