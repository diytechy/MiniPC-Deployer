# Design note: keep the boot copy of firstboot in step with the delivered tree

Date: 2026-10-09. Status: proposal, no code written. Item: HomeHub FOLLOWUPS A7, bullet 1 ("design first").

## Problem

`scripts/deploy_stack.py` delivers `stack/` to `/opt/homehub/stack/` and verifies every file by sha256. It never touches `/usr/local/sbin/homehub-firstboot.sh`, which is the copy the hub runs at boot (`stack/autoinstall/homehub-firstboot.service:41`). On the hub that copy was the 2026-09-10 version while `/opt/.../firstboot.sh` was current, so each `--run-firstboot` converged the box once and the next boot ran older code (HomeHub `FOLLOWUPS.md` A7, bullet 1). The reported state is the trap: the verified tree is current, `plan()` finds nothing to do, and the boot copy is never looked at.

## Facts found

**1. Who installs the boot copy.** Only `stack/autoinstall/user-data`, at reimage time:
- `:889` installs the unit file to `/etc/systemd/system/homehub-firstboot.service` with `install -m 0644`.
- `:890` `cp /target/opt/homehub/stack/autoinstall/firstboot.sh /target/usr/local/sbin/homehub-firstboot.sh`.
- `:891` `curtin in-target ... chmod +x` on that path.

`firstboot.sh` itself never installs or refreshes it. A grep for `install`/`cp` against `homehub-firstboot` finds nothing in `firstboot.sh`. The operator hint at `firstboot.sh:47` and `REMOTE_MANAGEMENT.md:135` both tell the operator to run the sbin path by hand.

**2. The unit.** `homehub-firstboot.service`: `After=network-online.target docker.service` and `Wants=` both (`:35-36`), `Type=oneshot` (`:39`), `RemainAfterExit=yes` (`:40`), `ExecStart=/usr/local/sbin/homehub-firstboot.sh` (`:41`), `WantedBy=multi-user.target` (`:49`). There is no `ConditionPathExists=` and no `RequiresMountsFor=`. It runs on every boot; the header says so at `:4-11`. `firstboot.sh:2` still says "Invoked once", which contradicts `firstboot.sh:26`.

**3. The lane.** `deploy_stack.py`:
- `:82` `STACK_REMOTE = /opt/homehub/stack`. `remote_hashes` (`:162-195`) lists only that tree.
- `build_payload` (`:133-159`) reads `git archive <rev> stack`, so bytes come from a commit. `:155` treats a shebang as executable whatever the index says.
- `classify_stale` (`:198-242`) strips CR in the remote shell (`:222-226`) and compares LF-normalised hashes (`:239-241`). This exists only to label line-ending-only drift in the plan; it must not be used to accept a boot copy.
- `plan` (`:245-254`) returns missing/stale/same for the payload only.
- `deliver` (`:276-418`): `safe_paths` (`:257-273`), placement script (`:329-412`), per-file rollback (`:354-363`), mode rule "never take a bit away" (`:403-408`). The docstring's partial-failure case is "new compose, old firstboot" (`:300-301`).
- `run_firstboot` (`:421-487`) runs `sudo -n bash /opt/homehub/stack/autoinstall/firstboot.sh` (`:481-484`). It runs the `/opt` copy, not the boot copy.
- `_write_report` (`:490-501`).
- `main()` (`:577-718`): dirty-tree note (`:603-607`); payload and plan (`:609-612`); `if not todo` prints "Nothing to do" and skips everything else (`:667-668`); dry run gate (`:669-671`); deliver (`:672-679`); verify re-reads hashes but checks only `todo` (`:681-695`); `run_firstboot` gated on `--apply` and `verified is not False` (`:706-716`); report (`:717`).

The boot copy is therefore outside every step of `main()`. Verification is scoped to `todo`, and the incident state has `todo` empty.

**4. Mode.** `stack/autoinstall/firstboot.sh` is committed `100644` (`git ls-files -s`). The boot copy gets its exec bit only from `user-data:891`. Ownership is inferred as root because the `cp` runs in-target as root (not measured). `deploy_stack` never changes the boot copy's mode.

**5. Other `/usr/local/sbin` installers from the stack tree.** A grep for `install`, `cp`, `ln -s` and redirects into `/usr/local/sbin` across `stack/`:

Hub side, installed by `firstboot.sh` (`install -m 0755`, so they refresh from `/opt` on every firstboot run that reaches the line):
- `:281` `homehub-fstrim` (from `autoinstall/homehub-fstrim.sh`)
- `:1710` `homehub-library-guard` (from `samba/library-guard.sh`)
- `:1711` `homehub-retire-legacy-backup-health`
- `:1739` `homehub-tracker-defs-guard` (from `tracker/`)
- `:1742` `homehub-frame-freshness` (from `tracker/`)
- `:2013` `homehub-icedrive-profile` (only when `ICEDRIVE_MODE != off` and the file exists)

Hub side, installed by `user-data` only (reimage-time copies, never refreshed by the lane):
- `:890` `homehub-firstboot.sh` (the boot copy)
- `:931` `powertune.sh` (`cp` plus `chmod +x`), from `autoinstall/powertune.sh`, which `powertune.service:9` runs.

Panel side, excluded from the hub lane by `PANEL_PREFIX` (`deploy_stack.py:108`): `stack/autoinstall/wall/user-data` `:580, :603, :608-609, :619, :628-629` (cp), `:793` (`install -D`, EFI sync); `wall-firstboot.sh` `:883` (loop), `:904`, `:1513`, `:1519` (symlink `wall-audio-output`), `:1618`; `configure-bluetooth.sh` `:31-33, :57`.

So `firstboot.sh` is not the only orphan. `powertune.sh` has the identical shape, and the unit file at `user-data:889` is a third copy that nothing refreshes. Firstboot-installed tools are not stale in the same way: they are re-copied from `/opt` on each run, so their risk is a missing install line in an old boot copy, not old bytes. Hub contents were not measured here.

## Options

**A. Lane refreshes the boot copies after a verified delivery.** Covers `firstboot.sh` and `powertune.sh` from a declared list (`BOOT_COPIES = [(payload rel, dest, mode)]`).
- Runs in `main()` after the verify block (`:681-695`) and before the `run_firstboot` block (`:706`). It runs even when `todo` is empty, which is the incident state. It is gated on `--apply`.
- Compares by exact sha256 of the payload bytes. No CR stripping.
- Writes to `<dest>.new` in the same directory, sets mode and owner, then `mv -f` over the destination. Rename rather than truncate-in-place, because a running bash reads the script incrementally and an in-place write under a live process can corrupt it. The inode rule in `deploy_stack.py:56-61` is about bind mounts; nothing in `/usr/local/sbin` is bind-mounted (to confirm).
- Pros: small; uses the existing verification shape; the boot path does not change; the operator's sbin paths stay valid; the lane gets evidence in the report.
- Cons: drift is bounded by lane runs, not eliminated. A hand edit to `/opt` without the lane still diverges. The two copies remain two copies. Adding a new boot-time script means editing the list.
- Failure modes: (a) the verified `/opt` delivery succeeds, the boot copy install fails: the hub is in the exact incident state, now with a report line saying so. (b) `.prev` lost or overwritten on a second run: rollback target gone. (c) the list drifts from `user-data`'s install lines: two definitions of "boot copy" (see test plan).

**B. `firstboot.sh` refreshes its own boot copy at the end of a successful run.** The script copies its own bytes (`$0`) to the sbin path.
- Pros: converges whenever the `/opt` copy runs, from any entry point (lane, hand run, reimage).
- Cons: only the `/opt` copy can do it, and the copy at boot is the old one, so it never self-updates until a run of the new code happens by hand. It cannot refresh `powertune.sh` or the unit. It runs only on success; `firstboot.sh` exits 1 in several paths (`:2319` area, the `KIOSK_CONFIG_UNREADABLE` exit), so a run that fails is precisely the one that would not refresh. It overwrites a script that may be executing (the boot run itself, if it is the sbin copy) unless it renames.
- Failure modes: self-overwrite during the boot run; no refresh on a failing run; the lane still needs `--run-firstboot` to converge.

**C. Boot runs `/opt` directly; the sbin copy is retired.** Either change `ExecStart` to `/bin/bash /opt/homehub/stack/autoinstall/firstboot.sh` (C), or keep the sbin path with a constant three-line shim that execs the `/opt` script (C').
- Pros: C' removes the logic copy, so the boot path cannot drift from the tree by construction. C removes the second copy outright.
- Cons: (1) Boot now depends on `/opt` being complete at boot. The unit has no `RequiresMountsFor=` and no `ConditionPathExists=`; whether `/opt` is a separate filesystem on the hub was not checked. (2) A reboot during a delivery runs a mixed tree once. (3) C needs the `/etc` unit file changed on every existing box, and the lane cannot deliver that today (it does not write outside `/opt/homehub/stack`), so C has the same bootstrap problem one level over. C' avoids that because the unit is untouched, but the shim itself must be placed once on each box. (4) Operator docs (`firstboot.sh:47`, `REMOTE_MANAGEMENT.md:135`), `user-data:889-891` and `validate_config.py:663` change or need review. (5) A broken `/opt` tree now blocks boot bring-up with no fallback.

## Recommendation

Adopt **A**, scoped to a declared list, now. It closes the reported gap on the next `--apply` without changing what boots, and it reuses the verify-from-the-box discipline the lane already has. C' is the option that cannot drift, and it is the better end state, but it changes the boot path on a live household server and needs a one-time placement on each box, so it is an Owner decision and should not arrive as a side effect of this item. Under A the boot copy is never staler than the `/opt` tree the lane last verified; the remaining drift is `/opt` itself diverging from HEAD, which is already visible in the plan.

Partial failure under A: if the `/opt` delivery verified and a boot-copy install fails, exit non-zero and do not run `--run-firstboot`. Running firstboot from `/opt` would converge the box once and leave the boot copy stale, which is the defect. The message should name the boot copy and say re-run is idempotent.

## Design details for A

Plan (always, read-only, also under dry run): for each boot copy, read `sha256`, mode, owner and type (`[ -L ]`, `[ -f ]`) in one `ssh`. Report `current | stale | absent | symlink (refuse)`. Add `boot_copies` to the JSON report.

Apply (`--apply`, after `verified is True`, before firstboot):
1. Refuse if the destination or its directory is a symlink.
2. Copy the current file (if any) to `/var/lib/homehub/boot-copies/<name>.prev`. Never write `.prev` if it already exists from a previous failed attempt in the same run.
3. `cat` the payload bytes to `<dest>.new`; `chmod 0755`; `chown root:root`.
4. `mv -f <dest>.new <dest>`.
5. Re-read the box. Require sha256 equal to the payload hash, and mode `755`, owner `root`. On mismatch, `mv -f .prev` back and exit non-zero.

Report: `boot_copies: [{path, before, expected, after, mode, action}]`, with `action` one of `unchanged | refreshed | refused | failed`.

Manual rollback: `sudo -n mv -f /var/lib/homehub/boot-copies/<name>.prev /usr/local/sbin/<name>`.

## Test plan

Existing tests in `tests/test_deploy_stack.py` cover placement only (`:279-313`, with `_fake_box` and `DEPLOY_SUDO=`). No test covers `main()`, `run_firstboot` or `classify_stale`. The boot-copy step needs a path override in the same style as `DEPLOY_SUDO` (for example `DEPLOY_SBIN_DIR`), so the step runs against a temp directory under plain `sh`.

| Test | Setup | Must catch |
|---|---|---|
| refreshes when `/opt` is current and boot copy is stale | payload unchanged, `todo` empty, sbin copy old | gating the step on `todo` (the incident) |
| unchanged when hashes match | sbin copy equal | rewriting needlessly; `.prev` churn |
| dry run writes nothing | `--apply` absent | side effect outside `--apply` |
| CRLF-only boot copy is stale | sbin copy = LF payload plus CRLF | reusing `classify_stale`'s CR stripping to accept it |
| verify mismatch restores `.prev` and exits non-zero | injected write corruption | no restore; exit 0 on mismatch |
| symlink at destination refused | sbin path is a symlink to a temp file | writing through the link (cf. `deliver` symlink test) |
| mode and owner enforced | old copy 0644 | `cat` in place leaving 0644 |
| ordering: after verify, before firstboot | stub `run_firstboot`, stub `deliver` | step moved before verification; firstboot run despite failed refresh |
| `.prev` not clobbered by a failed second attempt | two runs, second fails | losing the rollback target |
| `powertune.sh` covered by the same list | second entry | list not applied to every entry |

Fakes: `DEPLOY_SUDO=` and `DEPLOY_SBIN_DIR` for the box side; monkeypatched `subprocess.run` for `ssh` in the `main()` tests. Each mutation should be run once to confirm the matching test fails, as the existing suite was.

## Not proven

- The hub's current sbin copies were not measured. A7's 2026-09-10 figure is the only evidence.
- Whether `/opt` is a separate filesystem at boot, and whether `/usr/local/sbin` is bind-mounted by any compose file.
- That the lane's sudo rule permits `cat`, `chmod`, `chown` and `mv` in `/usr/local/sbin` (it already permits `sudo -n` for `tar`, `cat` and `sha256sum` under `/opt`).
- No tests were run; this note changes no code.

## Owner decisions

1. A now, or C' as the end state (a boot-path change)?
2. Should the `/etc` unit file join the lane? It needs `daemon-reload` and is a separate risk.
3. Should a failed boot-copy refresh block `--run-firstboot` (recommended) or only warn?
4. Should `.prev` live in `/var/lib/homehub/boot-copies/` (proposed) or next to the destination?
