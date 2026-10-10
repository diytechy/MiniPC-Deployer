# Design note: keep the boot copy of firstboot in step with the delivered tree

Date: 2026-10-09. Revised after review 2026-10-09. Item: HomeHub FOLLOWUPS A7, bullet 1 ("design first").

## Problem

`scripts/deploy_stack.py` delivers `stack/` to `/opt/homehub/stack/` and verifies every file by sha256. It never touches `/usr/local/sbin/homehub-firstboot.sh`, which is the copy the hub runs at boot (`stack/autoinstall/homehub-firstboot.service:41`). On the hub that copy was the 2026-09-10 version while `/opt/.../firstboot.sh` was current, so each `--run-firstboot` converged the box once and the next boot ran older code (HomeHub `FOLLOWUPS.md` A7, bullet 1). The reported state is the trap: the verified tree is current, `plan()` finds nothing to do, and the boot copy is never looked at.

## Facts found

**1. Who installs the boot copy.** Only `stack/autoinstall/user-data`, at reimage time:
- `:889` installs the unit file to `/etc/systemd/system/homehub-firstboot.service` with `install -m 0644`.
- `:890` `cp /target/opt/homehub/stack/autoinstall/firstboot.sh /target/usr/local/sbin/homehub-firstboot.sh`.
- `:891` `curtin in-target ... chmod +x` on that path.

`firstboot.sh` never installed or refreshed it before this revision.

**2. The unit.** `homehub-firstboot.service`: `After=network-online.target docker.service` and `Wants=` both (`:35-36`), `Type=oneshot` (`:39`), `RemainAfterExit=yes` (`:40`), `ExecStart=/usr/local/sbin/homehub-firstboot.sh` (`:41`), `WantedBy=multi-user.target` (`:49`). It runs on every boot.

**3. What `--run-firstboot` runs.** `deploy_stack.py` `run_firstboot` (`:481-484`) runs `sudo -n bash /opt/homehub/stack/autoinstall/firstboot.sh`: the NEW copy from `/opt`, not the boot copy. So whatever the new `firstboot.sh` does early, it does with current code.

**4. Early exits in `firstboot.sh`.** A normal run can reach these; only the first two come before any boot-copy work could sensibly go:
- `:38` `cd "$STACK_DIR" || exit 1` (tree missing).
- `:42` `.env` missing, `exit 1`.

Both are guards against a broken tree and fire before any install. The other exits (`:981`, `:1040-1042`, `:2029-2338`) are later, inside steps or backup and restore paths. The fstrim install (`:281`) sits after steps 1 and 1b, so nothing before it had yet installed anything to `/usr/local/sbin`.

**5. Sudo is not a constraint.** `REMOTE_MANAGEMENT.md:27` records `hub ALL=(ALL) NOPASSWD:ALL`. Root writes to `/usr/local/sbin` are available to the lane and to firstboot.

**6. The lane.** `deploy_stack.py`:
- `:82` `STACK_REMOTE = /opt/homehub/stack`. `remote_hashes` lists only that tree.
- `classify_stale` strips CR in the remote shell and compares LF-normalised hashes. That exists to label line-ending drift in the plan; it must not be used to accept a boot copy.
- `main()`: `if not todo` prints "Nothing to do" and skips to the report; verification re-reads hashes but checks only `todo`; `run_firstboot` is gated on `--apply` and `verified is not False` (`:706`). With nothing to deliver `verified` stays `None`, which is exactly the incident state.
- `deploy_stack.py`'s "preserve inodes" rule (`:56-61`) exists for compose bind mounts. Nothing in `/usr/local/sbin` is bind-mounted, and it is deliberately NOT applied to boot copies: `install` replaces the inode, and a bash that is already executing the old script keeps reading its old, unlinked inode, which is safe.

**7. Other `/usr/local/sbin` installers.** Hub side, installed by `firstboot.sh` with `install -m 0755` and so refreshed on every run that reaches the line: `homehub-fstrim` (`:281`), `homehub-library-guard`, `homehub-retire-legacy-backup-health`, `homehub-tracker-defs-guard`, `homehub-frame-freshness`, `homehub-icedrive-profile` (conditional). Hub side, installed by `user-data` only: `homehub-firstboot.sh` and `powertune.sh` (`:931`, `cp` plus `chmod +x`, run by `powertune.service`). The panel-side installers are excluded from the hub lane by `PANEL_PREFIX` and are out of scope.

**8. Mode.** `stack/autoinstall/firstboot.sh` is committed `100644`. Today the boot copy's exec bit comes only from `user-data:891`.

## Options

**A. Lane writes the boot copies after a verified delivery** (the previous recommendation). Needs a write path from the lane into `/usr/local/sbin`, a `.prev` rollback file, and a second definition of "boot copy" that must match `user-data`. Superseded: see B-early.

**B-early. `firstboot.sh` refreshes its own boot copies early in every run.** The script installs `$STACK_DIR/autoinstall/firstboot.sh` and `$STACK_DIR/autoinstall/powertune.sh` over the sbin paths with `install -m 0755`, next to the existing installer lines. Placed after the `.env` guard (`:38`, `:42`) and before any step that can fail or exit, so a failing run still refreshes the boot copies.
- Converges from every entry point that runs the `/opt` copy: `--run-firstboot`, a hand run, a reimage.
- The incident path is closed by the content of the tree itself: `--run-firstboot` runs the new `/opt` copy, and the new copy installs itself.
- `install` replaces the inode, so a bash executing the old boot copy is not disturbed.
- Costs nothing new in the lane: no write path, no rollback file, no second list of boot copies in Python.
- Cons, stated plainly: the box runs the OLD boot copy until the first `--run-firstboot` after delivery. That is the design, not a defect; the lane's report (below) says so.

**B. `firstboot.sh` refreshes at the END of a successful run.** Rejected: a failing run would not refresh, and a failing run is the one most in need of current code.

**C'. Constant exec shim.** The sbin path becomes a three-line shim that execs the `/opt` script. It removes the logic copy by construction. It changes the boot path on a live household server and needs a one-time placement on each box. **This is the Owner decision (below).**

**C. Boot runs `/opt` directly.** Requires changing `/etc` unit files on every box, which the lane cannot deliver, and makes boot depend on `/opt` being complete. Not recommended.

## Recommendation

Adopt **B-early** in `firstboot.sh`, plus a **verify-only report** in `deploy_stack.py`:
- After a delivery (or with nothing to deliver), the lane reads the sha256 and mode of both boot copies from the box and compares them EXACTLY with the payload bytes. It records each as `current | stale | stale-line-endings | missing` and prints a line for each stale one. It does not fail the run and it has no write path to `/usr/local/sbin`. Firstboot is the only writer.
- The gate is `result.get("verified") is not False`, mirroring `:706`. It must not be `is True`: with nothing to deliver `verified` stays `None`, and that is precisely the incident state the report exists to show.
- A dry run performs the same read-only check, because the dry run already talks to the box for `remote_hashes`.

**CRLF strictness.** The comparison is exact. A boot copy with CRLF line endings is genuinely broken, because the kernel looks for an interpreter whose name ends in a carriage return. So `stale-line-endings` is labelled as a state for the operator to see, never as current. Line-ending-only drift is not accepted.

## Design details

**Placement in `firstboot.sh`** (after the `.env` guard, before the `REPLACE_WITH` warning and step 1b):

```
install -m 0755 "$STACK_DIR/autoinstall/firstboot.sh" /usr/local/sbin/homehub-firstboot.sh
install -m 0755 "$STACK_DIR/autoinstall/powertune.sh" /usr/local/sbin/powertune.sh
```

Both sources are in the same tree this run is executing from, and the `.env` guard has already proved the tree is present. The line is unconditional, like the fstrim line, under `set -e`.

**Lane report in `deploy_stack.py`:** one pure classifier and one read-only remote read.
- `boot_copy_state(expected_sha, expected_lf_sha, actual_sha, actual_lf_sha) -> str` returns `missing` when the box has no file, `current` on exact equality, `stale-line-endings` when the box's CR-stripped bytes equal the payload's LF form, and `stale` otherwise.
- One `ssh` call, `sudo -n sha256sum` and `sudo -n stat -c %a`, using the same pattern as `remote_hashes`.
- The result is `result["boot_copies"] = [{path, expected_sha256, actual_sha256, mode, state}]`.
- When `--run-firstboot` runs, the boot copies are re-read afterwards: `boot_copies_before` holds the pre-firstboot snapshot, `boot_copies_after` the re-read, and `boot_copies` follows the after state (or keeps the stale snapshot plus `boot_copies_after_error` if the re-read fails). The re-read happens even when the pre-firstboot read failed: then `boot_copies_before` is absent, that failure is carried as `boot_copies_before_error`, and `boot_copies`/`boot_copies_error` always describe the final state. Without firstboot only `boot_copies` is written.
- An ssh failure is a warning, not a `die()`. A diagnostic must not fail the thing it reports on.

## Deferred (not in this change)

- Refreshing `/etc/systemd/system/homehub-firstboot.service`. It needs `daemon-reload` and is a separate risk.
- The hub's current sbin copies were not measured here. The first `--run-firstboot` after delivery is what converges them, and the lane report will show the before and after.

## Not proven

- Nothing has run on the hub. The first `--run-firstboot` after delivery is the convergence test.
- Whether `/opt` is a separate filesystem at boot. Relevant to C only; B-early does not depend on it.
- Test coverage is static ordering plus pure-function classification. The `install` lines themselves are not executed by any test here.

## Owner decisions

1. C' as the end state (a boot-path change). Only this one. B-early does not need a decision.
