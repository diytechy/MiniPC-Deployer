# Project Status — Blackboard

Live coordination for the gated process (see [process.md](process.md)). Keep the
**Current State** header short and current; append the audit log below (newest
last) — it is the record, not required reading for every pass.

---

## Current State

- **Active gate:** G1 — Requirements, UX & constraints. This is a config/infra
  repo delivered against a ratified brief (HOMELAB_RESTRUCTURE_PLAN.md); the
  requirement spine is intentionally **high-level** (proportionality doctrine).
- **Round:** 1
- **Open items:**
  - **Needs the Owner** _(ratification / manual steps — dial=HIGH)_:
    - OI-1 — **Google OAuth client** must be created in Google Cloud (needs
      the Owner's account); redirect URI to register:
      `https://tracker.<domain>/oauth2/callback` →
      [stack/.env.example](../stack/.env.example)
    - OI-2 — **Reimage-over-LAN ladder** is HIGH-RISK; the memo presents options
      with checkboxes. Nothing destructive is implemented until the Owner checks one
      → [REMOTE_MANAGEMENT.md](../REMOTE_MANAGEMENT.md)
    - OI-3 — **Push** each commit (agents lack the SSH key) →
      this repo
    - OI-5 — **Run the V3 gate** (WI-10.18): enable Hyper-V (elevated,
      machine-level, needs a reboot), create the VM, do the one-time GRUB
      edit (light path), and confirm compose-up →
      [vmtest/README.md](../vmtest/README.md). **The HUB half is DONE** — the
      gate ran 2026-07-31 and again 2026-08-01 (see the audit entries; the
      first one PASSED while running a three-week-stale tracker image).
    - OI-17 — ~~**Boot the WALL ISO**~~ **CLOSED 2026-08-05.** The A19 gate
      booted it and **the shell painted** — the narrow point this item stayed
      open on. See the 2026-08-05 audit entry. Original text follows.

      OI-17 (as written 2026-08-02; ATTEMPTED 2026-08-03):
      the ISO was booted for the first time and got as far as `late-command_9`
      before dying on the light path's missing payload (see that day's entry —
      it was a real defect in BOTH images, now fixed). **Subiquity, the disk
      pin and all 40 packages including the 23 Electron libraries are now
      PROVEN on a real install.** What is still unproven is everything after
      the late-commands: `wall-firstboot`, `getty@tty1`, `cage`, and whether
      the shell paints. A repacked (zero-keypress) wall ISO is built at
      `D:\vmtest-out-wall\wall-repacked.iso` and `Wall-VMTest` exists; the
      re-run needs **one elevated approval** →
      [vmtest/README.md §11](../vmtest/README.md).
    - OI-19 — **The hub's site-staging late-command had the same `/cdrom`
      bug — FIXED 2026-08-03, and NEVER INSTALLED FROM.** The defect:
      `stack/autoinstall/user-data`'s site step read
      `/cdrom/deploy-payload/site`, fell back to `/media/...`, and then
      `[ -d "$S" ] || exit 0` — a **silent success**. On the LIGHT path neither
      exists (that is the defect fixed for the payload copy the same day), so a
      PRODUCTION hub would install **none** of its six real files: `.env`,
      `backup.env`, `cifs.creds`, `samba-users.creds`, `smb.conf.fragment`,
      `library-mounts.fstab`. Severity, precisely: `.env` is the only one with a
      downstream check, and `firstboot.sh` step 1 only **WARNs** on
      `REPLACE_WITH` before bringing the stack up anyway — on a headless box
      that warning is in the journal and nowhere else. The other five have no
      check at all, so the drives would not mount and Samba/backup would be
      unconfigured; the box would come up looking exactly like a SIM build
      while believing it is production. **Exposure, stated exactly:**
      `Build-VentoyStick.ps1` — the only wired production path — calls
      `build-repacked-iso.sh`, where `/cdrom` IS the combined ISO and does carry
      `/deploy-payload`, so no stick ever built has been bitten. What was one
      command away from it is the equally supported
      `SITE_DIR=… bash vmtest/build-seed.sh`.
      **The fix (see the audit entry):** the step no longer searches at all — it
      reads `/target/opt/homehub/site`, the payload late-command 3 has already
      copied, so there stays exactly ONE discovery mechanism (the `/cdrom`,
      `/media`, `/media/*`, `/run/media/*`, then CIDATA-by-label search) and it
      is the one that was fixed and exercised. Same shape as the wall image's
      late-command 4a. It also **refuses** now: the tracked user-data carries
      `BUILD_PROFILE=production` and `render_seed_tree` rewrites it to `sim` for
      a vmtest image, so a production image whose payload lost `site/` fails the
      install loudly instead of exiting 0.
      **A SECOND HEAD, found by adversarial review 2026-08-03 and fixed the
      same day:** the refusal only ever asked whether `site/` was a DIRECTORY.
      A payload whose `site/` existed but held nothing that mattered — the
      builder counted the mandatory `user-data.filled` as a staged site file,
      so a `SITE_DIR` containing only that built a clean production ISO —
      walked past it, logged six MISSING lines nobody reads on a headless box,
      touched `.site-present` so `firstboot.sh`'s own "broken production stick"
      branch stayed quiet, and exited 0. Byte for byte the outcome above, by a
      different road. Measured, not reasoned: the build succeeded and 4b
      returned 0. Now the FILES are checked, in both places — the builder
      refuses to stage, and a missing `required` file makes 4b exit 1.
      **PROVEN — behavioural, still never installed:** `vmtest/test-hub-seed.sh`
      (**28 checks, 0 failed, 0 skipped**, as root in WSL2) builds real sim and
      production seeds and then RUNS the late-commands extracted from the
      user-data each build produced — step 3 (find and copy the payload)
      followed by 4b, against a real payload under a real `/media` entry, a
      real loop-mounted CIDATA seed ISO, and a forced `cp` failure. Until
      2026-08-03 the case advertised as "the light path" hand-created
      `/target/opt/homehub/site` and never ran step 3 at all.
      **NOT PROVEN: nothing has been installed from any of it.** Subiquity has
      never run these late-commands, `curtin` has never placed `/target` for
      them, and no ISO was built or booted here. OI-19 is FIXED-BUT-UNVERIFIED,
      not closed; it closes on a hub install.
      **NEEDS THE OWNER (one ruling now, one decision to ratify):**
      (a) **DECIDED IN THE REFUSE DIRECTION, 2026-08-03, pending ratification:**
      a production build whose `site/` arrives but is missing a `required` file
      now HALTS the install (it was LOUD-BUT-NOT-FATAL). Same call the
      missing-directory branch and the wall image already make. The lever to
      reverse it is moving a file from `required` to `optional` in 4b's own
      table. (b) `drive-identity.conf` is now installed with the other six: the
      builder and `Build-VentoyStick.ps1` both stage it and **nothing ever
      installed it**, so the drive-identity file has never reached a box. Both
      are recorded as **A10** in the Assumptions log below.
    - OI-20 — **`config.json` now has a path onto the hub — and its MODE needs
      a ruling** (2026-08-03, new): Personal's `Materialize-Deploy.ps1` emits
      `out\homehub\config.json` (the kiosk shell's runtime config: `FEED_TOKEN`,
      a Kuma push token, the Subsonic password). It is a **hub** artifact — the
      shell fetches `./config.json` relative to its own origin, and that origin
      is Caddy's kiosk site, document root `/opt/homehub/stack/wall-shell/`.
      This repo now stages it (`render_seed_tree`) and installs it
      (`firstboot.sh` step 3e, **after** step 3d's untar). **The ruling owed:**
      it is installed **0600 root:root**, the most restrictive mode that
      actually serves — measured, not assumed: the pinned `caddy:2.11.4-alpine`
      declares no `USER`, `docker-compose.yml` sets no `user:`, so the container
      is uid 0 and reads the read-only bind mount as root. It was NOT widened to
      0644. What the Owner should rule on is the **posture**, not the digits: a
      file carrying live credentials is now **served over HTTP** — behind the
      kiosk site's `remote_ip {$PANEL_IP}/32` matcher, so not open to the LAN,
      but that is an allow-list, not a secret store.
      **The silent-degradation half is now closed (2026-08-03, adversarial
      review):** nothing verified that caddy could actually READ the file. Its
      healthcheck probes the admin API, which is up whenever the process is, so
      a `user:`, a `USER` in a newer image or daemon userns-remap would leave
      caddy `healthy` while the site 403/404s and `loadConfig` — which never
      throws — falls back to defaults. (`:ro`/`read_only:` are NOT the risk:
      they restrict writes, and this is a read.) `firstboot.sh` **step 4b** now
      asserts the read **as the container** (`docker exec` inherits the
      service's user) whenever the production source exists, and a failure makes
      the whole unit exit non-zero so `systemctl status homehub-firstboot` is
      RED. **UNPROVEN: step 4b has never run** — no hub, no compose bring-up.
      **The stick half is now guarded from this side too:** a production build
      with no `site/config.json` is REFUSED by `render_seed_tree`. Personal's
      `Build-VentoyStick.ps1` still assembles `out\site\` from a hardcoded
      `$wanted` list; the two halves are deliberately independent — supplying
      the file and refusing to build without it are different failures with
      different owners. **Consequence to expect:** until Personal's half lands,
      a `Build-VentoyStick.ps1` run whose `out\` tree has no `config.json` now
      FAILS instead of quietly producing a stick without one.
    - OI-21 — ~~**`/opt/homehub` installed WORLD-WRITABLE**~~ **CLOSED
      2026-08-05:** measured on two freshly installed boxes — 0 group- or
      world-writable paths under `/opt/homehub` (was 230) and `/opt/wall-panel`
      (was 215), `stack/.env` `0600 root:root`. Original text follows.

      OI-21 (as written) — **`/opt/homehub` installed WORLD-WRITABLE — every hub ISO ever
      built here. FIXED 2026-08-04, NOT YET REBUILT.** Found by BOOTING the hub
      gate VM, not by review: `drwxrwxrwx root:root /opt/homehub` (and `stack/`,
      `images/`), `-rwxrwxrwx stack/.env`, `-rwxrwxrwx docker-compose.yml`, 230
      world-writable paths, with `sudo -u nobody` able to READ every credential
      in `.env` and WRITE the compose file root brings up on the next boot — a
      straightforward local privilege escalation. The chain was three honest
      links: DrvFs reports 0777 for everything on an NTFS mount, `-rock` records
      that faithfully, `cp -a` copies it faithfully; **nobody ever decided the
      mode.** Now decided in the builder (`normalize_payload_modes` +
      `assert_payload_modes`, which FAILS the build), imposed by the ISO writer
      when the staging filesystem cannot hold it, and asserted again at install
      time (hub late-command 3b, wall 3a). **What the Owner owes:** nothing to
      rule — but **no ISO has been rebuilt**, so the fix is proven on staged
      trees, on a freshly-written seed ISO and on executed late-commands, and
      **not** on an installed box. The next hub/wall install is what closes it.
      Same family as OI-19 and the gitignored-payload leak: a latent defect in
      every artifact, invisible to every static check, surfaced by an install.
      **2026-08-04 (later): BOTH ISOs HAVE NOW BEEN REBUILT** from the fixed
      builder, into `D:\vmtest-out-hub-a19` and `D:\vmtest-out-wall-a19`, and
      `assert_iso_payload_modes` passed on both artifacts. Still not an
      installed box — that is the A19 run (OI-22).
    - OI-22 — ~~**RUN THE A19 TWO-VM GATE**~~ **CLOSED 2026-08-05 — PASSED,
      UNAIDED.** Every §3 "Done when" row met with no hand-patching: the panel
      painted red with both drive lanes named, the per-ITEM colours agreed, and
      all three ratified guards held (403 to a non-panel, a forged
      `X-Forwarded-User` replaced — including when sent twice — and `WALL_PORT`
      bound to the LAN leg only). Capture:
      `D:mtest-out-wall-a19\panel-a19-GATE.png`. **It closes OI-17 and OI-21
      as well — but NOT OI-19, OI-20 or the hostname fix, which need a
      PRODUCTION build and are therefore blocked on C17.** See the 2026-08-05
      audit entry, which corrects an earlier claim that one install would close
      all five. The original text follows.

      OI-22 (as written 2026-08-04). Everything it needs is
      now built and no decision is outstanding; it needs elevation and about
      three hours of wall-clock, which is why it is the Owner's. Three
      commands, in order, from an elevated shell in the MiniPC-Deployer
      checkout:
      `.\vmtest\Start-A19Gate.ps1 -Stage Lab`, then `-Stage Hub -Force -Watch`,
      then — only once the hub answers — `-Stage Panel -Force -Watch`. The two
      installs must not overlap (two VMs on one host and one disk turned a
      45-60 minute wall install into ~2h10m on 2026-08-04). See
      [vmtest/README.md](../vmtest/README.md) §12 for the addressing plan and
      the three sim deltas it introduces, and Personal's
      `WALL_PANEL_BRINGUP_PLAN.md` §3 for what to assert once both are up.
      **This single run is what closes OI-17 (the shell has never painted),
      OI-19, OI-20, OI-21 and the hostname fix — all five are code-complete and
      INSTALL-UNVERIFIED, and an install is the only thing that can speak to
      any of them.**
    - OI-18 — **RULED (b) 2026-08-03, BUILT THE SAME DAY, and HARDENED 2026-08-04
      after an adversarial review refuted its headline claim;
      INSTALL-UNVERIFIED — NOTHING HAS EVER BEEN MOUNTED.** The panel has **two**
      media sources on **two** hosts (storage-map §3 rows 1-2, §3b, §4d) and one
      credential cannot authenticate on both, so no set of values made a
      production panel work. The Owner ruled **exit (b) — two UNC/credential
      pairs** (exit (a), consolidating behind one host, would have re-opened
      HOMELAB_TOPOLOGY.md decision 2). What now exists:
      - `wall-sync.sh` carries a **flow map** (`flow_spec()`) with two flows:
        music from HOMEHUB via `MEDIA_MUSIC_SHARE_UNC` + the `Music` subdir
        **under** the mount, frame video from Mini-serv via
        `MEDIA_FRAME_SHARE_UNC` at the **share root**.
      - **THE KNOB-CONTAINMENT PROPERTY IS NOW A MECHANISM, NOT A COMMENT.** The
        2026-08-04 review found FIVE ways round it: `wall.env` exported every
        identifier it contained (so `FLOWS=music` dropped the frame flow),
        `MEDIA_CIFS_EXTRA` could re-point the subtree/host/credential, a
        symlinked cache leaf redirected `rsync --delete`, the bench override
        could copy both credential files into the kiosk-readable cache, and a
        frame UNC with a path tail could widen the mirror. There is now an
        eight-key **allowlist** with the internals `readonly` after the load, a
        validated SMB-dialect **enum**, symlink refusal on the destination,
        `//host/share`-only UNCs, and the bench hook as a **command-line mode**
        (`--bench-source`, refused under systemd, fixtures under
        `/var/lib/wall-sync/bench`).
      - **Two failure policies, and a probe with THREE answers.** Music: a
        refused mount FAILS the unit. Frame: 445 is probed first, and the script
        now distinguishes **OFFLINE** (nothing answered at all, or no route —
        the designed "asleep" state, skipped calmly) from **INDETERMINATE** (name
        will not resolve, this panel has no default route, or the box answered
        with a REFUSAL — skipped too, but reported as unknown, never as asleep).
        A box that *answers* and then refuses the mount is **fatal**. No magic
        packet is ever sent for this flow.
      - **Two cadences, two units.** `wall-sync.service` keeps boot + resume +
        on-demand and syncs BOTH flows, with a **per-flow time budget** so a
        wedged music mount can no longer eat the unit's timeout and leave the
        frame flow unrun; `wall-sync-frame.{service,timer}` runs `--only frame`
        **every minute** (`OnUnitInactiveSec`, so a slow run cannot re-fire on
        top of itself). Per-flow `flock`, held through the manifest and the
        stamp.
      - **Two credential files**: `/etc/wall-panel/cifs-{music,frame}.creds`,
        `0600 root:root` — now **enforced**, not merely claimed — installed by
        wall late-command 4a, which also **deletes the payload copies** (they
        were builder-owned, and the ordinary builder uid is the panel user's).
        The inline `MEDIA_CIFS_USER`/`_PASS` fallback is **retired**.
      - **THE `share` USERNAME COLLISION IS GUARDED, NOT JUST OBEYED.** RULED
        2026-08-04: the HOMEHUB music account is ALSO called `share` — two
        accounts, two hosts, two passwords. `wall-sync.sh` refuses two UNCs on
        one host and two flows sharing one credentials file, and no wording
        anywhere says "the `share` account" without naming its host.
      - A **staleness ladder** that measures and reports (a stamp at the cache
        root, `WALL_FRAME_STALE_WARN_HOURS`, default 24) but deliberately
        **never escalates a skip into a failed unit** (ruled 2026-08-04:
        reporting-only, and never to the tracker).
      - Personal's half: store key `PanelMusicCifsCredential`, two UNCs in
        `config.wall.psd1`, and `Materialize-Deploy.ps1 -Image wall` emitting
        **four files for the first time in the project's life**.
      - **Coverage: `vmtest/test-wall-builder.sh` is 26 cases -> 85**, runs from
        an isolated sandbox checkout (it used to rewrite tracked files), and has
        a static gate that never skips. Proven: with `wall-sync.sh` present but
        unparseable, the previous suite exited **0**; this one exits 1. All 22
        stubs of the new guards turned exactly their intended case(s) red.
      **STILL FOR THE OWNER:** (a) the HOMEHUB music account is ruled to be
      `share`, but its §2 status — a real §2 identity needing a §2 row and a
      `Samba<Name>Password` key, or a panel-only account made by hand at the box
      — is **unruled**; (b) **is Mini-serv's NIC set to wake on a magic packet
      only, or on any traffic pattern?** The code sends no magic packet, but a
      wake-on-any-pattern adapter could be woken by the every-minute probe, which
      §4d does not want; (c) ratify the new same-host refusal (it is stricter
      than OI-18 said in words). **Nothing here has been mounted** — "emits
      successfully" is not "mounts successfully", and the reachability tests use
      `127.0.0.1`/`192.0.2.1`, which prove the decision, not the mount.
    - OI-7 — **Tier-2 catalog ratifications (2026-07-10):** ~~(a) confirm the
      tier-2-NOT-baked ISO boundary (profiles excluded from the payload unless
      `EXTRA_PROFILES` at export) as the standing Q10.9 B+ interpretation;~~
      **(a) is CLOSED, and answered the other way, 2026-08-07.** Running it
      settled it: that interpretation is incompatible with Q10.9 B+ itself. A
      hub whose `.env` enabled five profiles shipped with 9 of the 15 images it
      needed, so first boot went to the registry — and because `compose up -d`
      is all-or-nothing, one unreachable **optional** image took the whole core
      stack down with it. `export-images.sh` now derives the bake set from
      `COMPOSE_PROFILES` in the `.env` it is bundling: what is enabled is baked,
      and an unresolvable tag is a refused build. The boundary is the profile
      switch, not the bake. (b) decide where `MEDIA_ROOT` physically lives (must
      NOT be the WI-10.10 backup drives); (c) the oauth2-proxy **security pin
      bump** v7.6.0→v7.15.2 needs a V1 sim re-run + re-export before any real
      flash → [stack/README.md §9](../stack/README.md)
    - OI-10 — **Disk encryption decision (2026-07-11):** the AWOW's disk is
      UNENCRYPTED — physical theft/disposal exposes `.env` secrets + the
      finance volumes. Proposed: autoinstall LUKS (Subiquity supports
      `storage: layout: {name: lvm, password: …}`) + unattended unlock via
      TPM2 enroll at first boot **if the AK41 BIOS exposes Intel PTT (Owner:
      check BIOS)**, else dropbear-initramfs (SSH unlock over LAN — fits
      headless). Touches the autoinstall `storage:` layout = the
      REMOTE_MANAGEMENT **hard line**: not implemented until the Owner ratifies.
      Companion decision: LUKS on the backup drives too (finance snapshots
      land there in plaintext otherwise).
    - OI-8 — **Backup gap — RESOLVED in-repo 2026-07-10 (SN-010/SR-013,
      Owner-ratified single-table design):** `BACKUP_SOURCES` now accepts
      `volume:VOL[@CONTAINER]` and `path:/dir` specs; sim-proven
      (`run-volume-sim.sh` a–d GREEN + full `run-backup-sim.sh` regression
      GREEN). **Remaining for the Owner:** uncomment the volume lines in the real
      `/etc/homehub-backup/backup.env` (+ add `actual tracker` to `OFFSITE_SETS`)
      when configuring the box — they ship commented in `backup.env.example`.
    - OI-11 — **Offsite leg — the Owner CORRECTED the model on 2026-07-29: the
      backup service has NO offsite step in the target state.** The IceDrive
      client (SN-012/SR-015 opt-in desktop session) is pointed **directly at
      chosen library paths in its own GUI** and syncs them itself; the service
      stages, copies and prunes nothing for it. `OFFSITE_ENABLED=false` is now
      the documented target state (`backup.env.example`), the step-5 code is
      kept working as legacy, and the pipeline instead **ingests** network
      shares into the library so there is one current copy for the client to
      sync (see the 2026-07-29 ingest entry). **Mini-serv leaves the offsite
      path entirely** (unchanged from the 2026-07-25 ratification).
      **What this makes MOOT:** the "local-path target" build half done earlier
      on 2026-07-29 (`OFFSITE_PATH`) — it stays in the tree as harmless legacy,
      needs no sim leg, and there is no `OFFSITE_PATH` for the Owner to set;
      likewise the old OI-11(a) "a local-target sim leg is owed".
      **Still outstanding for the Owner:** stand the IceDrive client up on-box
      per `stack/remote-ui/README.md` and create its sync pairs against the
      library paths named in `Personal\deploy\storage-map.md` §4e — never one
      holding raw finance data. **Two consequences to accept:** the client is a
      GUI app, so **sync is down after every reboot until a session is opened**
      (SR-015), and with no offsite step the backup's NagLight report **cannot
      see** a stale cloud copy at all — a green backup says nothing about
      IceDrive.
    - OI-14 — **Ingest + exclusions need the Owner's real values
      (2026-07-29, OWNER):** the ratified INGEST step is inert until
      `INGEST_SOURCES` in the real `/etc/homehub-backup/backup.env` names the real
      share and the real library destination, and the `BACKUP_SOURCES` `path:`
      entry points at that library folder — the repo ships the fictional
      `mini-serv` / `/srv/library/NonDocs/MiniServ` placeholders only (SN-007).
      The **authoritative** library paths come from
      `Personal\deploy\storage-map.md`, not from the example. Same for
      exclusions: `BACKUP_EXCLUDE="*.bak"` ships as the Owner's stated default,
      but the **named very-large folders** he wants skipped are per-set
      (`name.exclude=` lines) and only he knows their names. Note the mirror
      contract before filling it in: `rsync -a --delete` means a file deleted on
      the share is deleted from the library copy on the next run (history lives
      in the dated run snapshots, `BACKUP_KEEP`).
    - OI-13 — **Wake-on-LAN needs the real values + the Windows-side
      settings (2026-07-29, OWNER):** the backup now wakes the sleeping game box
      before pulling and fails the run loudly on a wake timeout, but it is OFF
      until `BACKUP_WAKE_MAC` is filled in (with `BACKUP_WAKE_HOST` /
      `BACKUP_WAKE_TIMEOUT`) in the real `/etc/homehub-backup/backup.env` — the
      repo ships placeholders only (SN-007). On the **Windows** side: enable
      "Wake on Magic Packet" + "Allow this device to wake the computer" on the
      WIRED adapter and turn **Fast Startup OFF** (hybrid shutdown leaves the
      NIC unable to wake). Worth verifying at the same time whether the AWOW's
      `/dev/udp` broadcast is accepted or whether `apt-get install wakeonlan` is
      needed for the fallback — bash cannot set `SO_BROADCAST`, and the WSL
      kernel used for this session's testing REFUSED it.
    - OI-12 — **RATIFIED 2026-07-29 (the Owner) — the wall-panel image lane is
      GO**, in the belt-and-braces variant he chose: the kiosk auth site on a
      **LAN-bound alternate port the router never forwards, PLUS the `/32`
      allow-list** (not either one alone). Proposed 2026-07-25; brief:
      `Personal\homelab\OFFICEWALL_BOOTSTRAP.md`. **BUILT 2026-07-29** — see the
      audit entry below. The D-W0 split holds: the *image* is now a second target
      in this repo (`stack/autoinstall/wall/`), the panel's *shell app* stays in
      `OfficeWallNaglight` and is consumed as a built artifact (IF-005), so the
      "no product source" constraint is intact. What landed, against the three
      things this item said would:
      (a) the kiosk site — DONE (SR-016), and the forged-header strip + the 403
      default are **sim-proven** (`validate-sim.sh` checks 7-8). The **off-LAN**
      403 from a real WAN vantage remains the documented hardware test;
      (b) the graphical autoinstall variant + its own spine rows — DONE
      (SR-017, SN-013 per OI-12b: lighter gates, the panel is disposable);
      (c) `navidrome` is **no longer a gate** — D-W8's rider has the panel playing
      from its local synced copy, so streaming is optional, and `MEDIA_ROOT`'s
      ratification (2026-07-29) closed OI-7(b) anyway. The Caddyfile ships the
      `/music` proxy commented.
      **Still needing the Owner:** the values (`WALL_HOST`, `PANEL_IP`,
      `PANEL_USER_SUB`, `WIFI_*` — all T1/T3, placeholders only in this repo per
      SN-007), the panel's **DHCP reservation on its hardware MAC**, the
      `EXTRA_SUBDOMAINS` label, registering the wall templates in
      `Personal\homelab\deploy\FieldSchema.psd1` (see the audit entry for the
      exact snippet and two cross-repo consequences), and the whole of
      `stack/autoinstall/wall/WALL-BURN-IN.md`.
    - OI-15 — **RESOLVED 2026-07-29 (the Owner), and BUILT the same evening.**
      The ruling: *panel media is an AWOW network share; the **panel PULLS** —
      once after boot and on demand via a dedicated SSH-invocable command — with
      **MIRROR semantics** (`--delete`: content removed from the LAN source
      disappears from the panel cache); `/media/*` is then served **panel-locally**
      by the shell's Electron host.* So the question the item asked ("which origin
      serves `/media/*`?") is answered *panel-side*, and this repo owes the pull,
      not a route: the `TODO(OI-15)` stub is gone from `stack/caddy/Caddyfile`,
      replaced by a one-line statement that the kiosk site serves no `/media`
      route. Built here: `stack/autoinstall/wall/wall-sync.{sh,service}` (cifs
      mount → `rsync -a --delete` of ONLY `Music/` + `FrameVideos/` →unmount) plus
      `wall-media-manifest.py`, which emits the shell's two contracts
      (`music/index.json`, `frame/playlist.json`) into the cache as the sync's
      post-step. The dedicated command is `sudo systemctl start
      wall-sync.service`; there is deliberately **no timer**. See the audit entry
      below for what ran for real. **Still owed by the OTHER side** (not this
      repo): the Electron host actually mapping `/media/*` onto the cache dir —
      OfficeWallNaglight's half of the same ruling.
    - OI-16 — **RESOLVED 2026-07-29 (evening, the Owner): option (a) — the
      wall-sync also fires on RESUME from suspend.** The panel suspends nightly
      (`SLEEP_MODE=suspend`) and resumes without booting, so boot-only sync went
      stale by design; now every wake behaves like a boot for freshness. Built
      the same evening: `stack/autoinstall/wall/wall-sync-resume.service` — the
      standard systemd resume hook (`After=suspend.target` +
      `WantedBy=suspend.target`, so it starts when the suspend transaction
      completes, i.e. at WAKE) running `systemctl start --no-block
      wall-sync.service`. `--no-block` is what keeps the wake imperceptible: the
      hook only enqueues and exits, and the sync runs detached with its own
      journal/timeout/failure status. Wi-Fi wrinkle handled in `wall-sync.sh`:
      `network-online.target` is not re-evaluated on resume, so the script does
      a bounded non-fatal `nm-online` wait (30 s) before mounting; a still-down
      network fails the mount loudly and the retry is the on-demand command.
      Zero new knobs. See the audit entry below; the real suspend→wake firing is
      hardware-only (WALL-BURN-IN.md §8).
  - **In flight** _(driver; no approval needed)_:
    - OI-4 — layering WI-10.2/10.11/10.12 onto the migrated base →
      [stack/docker-compose.yml](../stack/docker-compose.yml)
    - OI-9 — **RESOLVED 2026-07-29.** (Found 2026-07-10 during the SR-013 red
      run: a `die` — e.g. a cifs mount failure — exited 1 WITHOUT posting
      `ok=false`, so only ERR-trap failures fed the tracker.) `die` now calls a
      registered reporter (`DIE_REPORTER`) before exiting; `backup.sh` registers
      `report_failure`, the single idempotent failure path shared with the ERR
      trap. A failing report is swallowed into a WARNING so it can never mask
      the original error, and an unset `DIE_REPORTER` (restore.sh,
      backup-standby.sh) is a clean no-op. Verified for real on four die paths
      — see the 2026-07-29 audit entry. Still owed: an assertion inside the
      committed sim legs (they exercise ERR-trap failures, not `die`).
- **Assumptions (unattended):** see the Assumptions log below.
- **Next action:** the Owner reviews + pushes; **for the newly-built wall lane
  (OI-12): fills in `WALL_HOST`/`PANEL_IP`/`PANEL_USER_SUB`/`WIFI_*`, gives the
  panel a DHCP reservation on its hardware MAC, confirms the router forwards
  :80/:443 and NOTHING else, registers the two wall templates in
  `Personal\homelab\deploy\FieldSchema.psd1`, and works
  `stack/autoinstall/wall/WALL-BURN-IN.md` on a desk before the panel is
  mounted** — which now includes filling in `MEDIA_SHARE_UNC` + the share
  credentials and working `WALL-BURN-IN.md` §8, since an unconfigured panel fails
  `wall-sync.service` on every boot by design — §8 now also carries the OI-16a
  hardware proof (suspend, wake, watch `journalctl -u wall-sync-resume -b` show
  the sync fired); fills in the wake values + the
  Windows-side WoL settings (OI-13) and the real ingest/exclusion values
  (OI-14); points the on-box IceDrive client at the chosen library paths
  (OI-11); creates the Google OAuth client
  (OI-1); runs the V3 boot (OI-5); ratifies the tier-2 decisions (OI-7) —
  then the "V3.5 dress rehearsal" (real secrets in the VM: External vSwitch,
  TLS decision, backup VHDX) discussed 2026-07-10 turns the sim-GREENs into
  real evidence.
- **UPDATE 2026-07-03 (WI-10.13):** the "no Docker on the dev machine" constraint
  above is now LIFTED — WSL2 + Ubuntu 24.04 + docker-ce is installed on the dev
  PC (Docker Desktop explicitly NOT installed, per the Owner's pick). `naglight:local`
  now builds for real and `docker compose config` resolves this stack's full
  compose file. See audit log entry below for versions/detail. Wave 2 (V1
  homehub-sim, WI-10.14/10.15) is now unblocked.

## Scope (restated from the brief)

- **Goal:** the deploy repo for the headless AWOW AK41 always-on box — an
  unattended, self-healing Docker stack (DNS, reverse proxy + TLS, the NagLight
  tracker behind Google sign-in, Actual Budget, LAN observability) plus an
  Ubuntu autoinstall image and full LAN remote management. **Config only** — app
  code lives in NagLight / Finance-Auditor / MinecraftKeeper.
- **Stakeholders / end user(s):** the Owner (homelab operator); the tracker's hosted
  end-users reach it only through oauth2-proxy.
- **Active hats:** Stakeholder, UX/Docs, System Engineer, Software Engineer, Test
  Engineer, **Network**, **Security/Ops** (the domain hats this infra scope
  needs).
- **Supported platforms:** the deploy target is **Linux** (Ubuntu 24.04 on the
  AWOW box); authored on Windows. Not a launchable product.
- **Constraints:**
  - ~~No Docker on the dev machine~~ **SUPERSEDED 2026-07-03 (WI-10.13):** WSL2 +
    Ubuntu + docker-ce now installed on the dev PC. `naglight:local` build and
    `docker compose config` are now verified for real (see audit log). Full
    runtime bring-up (containers actually running end-to-end) is still PENDING
    the WI-10.14 homehub-sim harness.
  - **Public-facing repo (Q10.6):** only `*.example` templates tracked; no real
    secret/hash/email/LAN detail/personal name. Local commit identity pinned to
    `diytechy <diytechy@users.noreply.github.com>`.
  - **Kit:** minimum profile, decision dial **HIGH** (secrets-adjacent infra).
  - **No product source in this repo** → the Python `ruff`/`pytest`/arch-map
    steps are dropped (not left passing vacuously, ADOPTING.md §3). The
    product-layer check is `scripts/validate_config.py`.
- **Non-goals:** a container registry / CI publishing (deferred, Q10.2 — local
  builds for now); the NagLight app code and its multi-user engine (NagLight
  repo, WI-10.4/10.5); the secret-handoff script (WI-10.3, the Owner ratifies).
- **Definition of done:** the repo's G1 gate is green (`check.py`), `.env.example`
  enumerates every knob, config coverage validates, and the honest validation
  ledger records what remains PENDING a Docker host.

## Honest validation ledger (WI-4.7 note carried forward; updated 2026-07-11)

| Check | State |
|---|---|
| `docker compose config` (core + all tier-2 profiles) | PASS (WSL docker-ce; core = the original 8 services with no profiles) |
| Live bring-up + curl health + `dig` + OAuth round-trip + tear-down | **GREEN in the V1 sim** (vs Dex/internal-CA/fixtures — `validate-sim.sh` 6 checks); **real-Google/real-TLS/host-:53 PENDING V3 boot + hardware** |
| Backup pipeline (cifs + offsite + feed + restore drill) | GREEN (`run-backup-sim.sh`, re-run 2026-07-29 after the ingest/exclusion change); drive-power + volume-source call contracts GREEN (mock-shim legs) — drive spin-down physics + real-docker volume copy are V3/burn-in |
| **INGEST step (library mirror) + EXCLUSIONS** | **GREEN — committed sim leg `run-ingest-sim.sh` (2026-07-29), 28 checks over the REAL cifs path:** mirror byte-identical to the live share + restore byte-equal, `--delete` deletion propagation asserted, global+per-set patterns kept out of archive AND `files.tsv` while every excluded path is named in the log/`<set>.excluded.log`/MANIFEST, empty-share refusal + its override, 3 loud config failures each posting `ok=false`. Untested by anything: a real Windows share as the ingest source, and real library-scale volumes (V3/burn-in) |
| Wake-on-LAN pre-step + OI-9 `die` reporting | **Exercised for real on WSL2 (2026-07-29)** — magic-packet bytes, probe, timeout-dies-loudly, `die` paths posting `ok=false` — the wake half from a THROWAWAY harness, **not a committed sim leg**; the `die`-reports-`ok=false` contract is now asserted for real in `run-ingest-sim.sh` (e1–e3, plus the empty-share refusal). A real magic packet has never woken a real box (V3/hardware). `OFFSITE_PATH` needs no leg — the offsite step is retired (OI-11) |
| **Wall kiosk site (SR-016) — the identity swap + the 403 default** | **GREEN — committed sim legs, `validate-sim.sh` checks 7-8 (2026-07-29):** a FORGED `X-Forwarded-User` arriving from the panel's `/32` reaches the tracker as `PANEL_USER_SUB` (read back off `/api/export`'s filename, so the identity the tracker actually saw is asserted, not inspected); the panel's `/api/today` is 200 and `/` serves the shell build without swallowing `/api/*`; the same two requests from a NON-panel source address get 403 with **Caddy's own body**, proving refusal at the edge rather than at the tracker. Untested by anything below hardware: the **off-LAN** 403 from a real WAN vantage, and whether Docker's port publish preserves the panel's source IP on the real box (it fails CLOSED if not) |
| **Wall panel image (SR-017)** | **CONFIG-LEVEL ONLY, and that is all it claims.** A throwaway container harness ran `wall-firstboot.sh` twice against a bare `ubuntu:24.04` root with stub `systemctl`/`udevadm`/`netplan` — 27 assertions PASS on what it writes (lid conf, iio mask, the udev rule from both piped names + its re-enable hint, NM powersave/MAC pinning, netplan rendered 0600 with no `@@TOKEN@@` left, both timers re-rendered from a changed schedule, tty1-only autologin, mem_sleep reporting) — **not a committed sim leg.** NOTHING physical has run: no graphical session, no `cage`, no Wi-Fi association, no suspend/resume, no quirk verified against the actual panel. `WALL-BURN-IN.md` is the list |
| **Wall media pull + manifests (OI-15)** | **Exercised FOR REAL on WSL2 Ubuntu (2026-07-29) — 67 assertions PASS from a THROWAWAY harness, not a committed sim leg.** The real `wall-sync.sh` ran via its supported `MEDIA_SOURCE_OVERRIDE` bench hook (a local fixture library instead of a cifs mount; everything after the mount is the production path): first sync, idempotent re-run, `--delete` propagation, the empty-subtree and missing-subtree REFUSALS with the cache proven untouched, the `WALL_SYNC_ALLOW_EMPTY` override clearing the cache, a non-UTF-8 filename skipped-and-counted, and 5 loud config guards. Both manifests pass `python3 -m json.tool`, and the emitted `index.json` was fed to **OfficeWallNaglight's real `normalizeManifest()` under node** (20 more assertions: stations, once-only URL encoding, quotes/`&`/`#`/non-ASCII round-tripping). **NOT tested by anything:** the cifs mount itself, Wi-Fi, library-scale volumes, the kiosk user reading the cache on a real box, and the *other* half of the ruling (the Electron host serving `/media/*`) — `WALL-BURN-IN.md` §8 is the list |
| Q10.9 B+ image payload: `export-images.sh` save + `docker load` all 9 | PASS (WSL; loads idempotent) — first-boot load-at-VM awaits V3; **oauth2-proxy v7.15.2 pin bump needs a re-export + sim re-run (OI-7c)** |
| Tier-2 pins exist on their registries (`docker manifest inspect`) | PASS — but tier-2 services have never been STARTED anywhere (enable-time validation, stack/README §9) |
| Shell scripts `bash -n` | PASS |
| `docker-compose.yml`, `meta-data`, `user-data` YAML parse | PASS (PyYAML) |
| Every compose `${VAR}` has an `.env.example` key | PASS (`validate_config.py`) |
| Every Caddy `{$VAR}` passed by the caddy service env | PASS |
| Bind-mount sources + autoinstall-referenced files exist | PASS |
| `check.py` (G1: config-validate + registry-integrity + doc-navigability) | PASS |

What only V3/hardware can still prove: real Google consent, publicly-trusted
ACME certs, Technitium on the host's real `:53`, the `extra_hosts` dns.<domain>
fix, drive spin-down physics, thermals — then the burn-in checklist
(stack/README §6) signs the box off. For the **panel**, add: the off-LAN 403, the
graphical session, Wi-Fi, S3 suspend/resume, and all six hardware quirks
(`stack/autoinstall/wall/WALL-BURN-IN.md`).

## Gate Sign-offs

Add columns for any active domain hats. Drop the `G-Release` row for a one-off
deliverable.

| Gate | Stakeholder | UX/Docs | System Eng | Test Eng | Human |
|---|---|---|---|---|---|
| G1 — Requirements/UX/Constraints | PENDING | PENDING | PENDING | n/a | PENDING |
| G2 — Decomposition & Test Coverage | n/a | n/a | PENDING | PENDING | PENDING |
| G3 — Implementation | n/a | n/a | PENDING | PENDING | PENDING |
| G-Release — Release readiness | n/a | n/a | n/a | PENDING | PENDING |
| G-Final — Acceptance | PENDING | n/a | n/a | (evidence) | PENDING |

---

## Audit log

<!-- Append verdict blocks here per process.md §5. Newest at the bottom. -->

### DRIVER — G1 — Round 1 — 2026-07-03
Scaffolding created. Starting G1.

### Assumptions log (unattended, dial=HIGH — the Owner to confirm/revert)
- A1 — Layout: migrated `life-tracker/deploy/*` under `stack/` at the repo root
  (kept the kit's `docs/`/`scripts/` roots). On-box path renamed `deploy/` →
  `stack/` under `/opt/homehub/`; the box hostname/opt-dir keep the `homehub`
  name (faithful to the source; the Owner knows it).
- A2 — This repo is treated as gate **G1** (requirements-agreed) — config is the
  deliverable, there is no compiled source to carry to G2/G3. The Python
  product/arch-map steps are dropped, not left vacuous.
- A3 — oauth2-proxy image pinned to `v7.6.0`, Google provider, allow-list via
  `authenticated-emails.txt` (Q10.5). Redirect URI `…/oauth2/callback`.
- A4 — SSH is **key-only** by default in the autoinstall (locked password),
  per WI-10.12 "key-only auth". Cockpit installed but **not** proxied publicly.
- A5 — Kept the `TRACKER_DATA_REMOTE` clone/pull entrypoint behaviour from the
  source; multi-user (D3) sets it blank + `TRACKER_COMMIT=false` (documented).
- A6 — SN-012/SR-015 shape (2026-07-20, the Owner asked for "opt-in light UI +
  SN-001 rescope"; details decided unattended): xrdp + **minimal** XFCE
  (`--no-install-recommends`, no desktop meta-package) rather than a full DE
  or VNC; the script never downloads the AppImage (vendor URL churn on a
  public repo — the Owner scp's it, `ICEDRIVE_APPIMAGE=` path knob); sync-resume
  is the documented post-reboot one-RDP-touch (NO autologin/virtual-display
  hack — that would fake self-healing the vendor app can't honestly offer);
  priority C, Verification=Inspection, no sim leg (a host-level GUI can't be
  exercised in the compose sim; the V3.5 rehearsal VM is where it could be
  tried for real). Revert any of these at the next gate if wrong.
- A7 — Wake/offsite shape (2026-07-29; the decisions themselves were ratified,
  these mechanics were not): the wake probe targets **tcp/445** because SMB is
  the port the pull actually needs — "awake" is defined as "can serve the
  share", not "answers ping"; `BACKUP_WAKE_TIMEOUT` defaults to **120 s** and
  the packet is re-sent every 15 s while waiting (a sleeping NIC can miss one);
  the timeout is a **budget, not a deadline** — a run can overshoot it by up to
  one probe+sleep cycle (~5 s); two OPTIONAL knobs beyond the three asked for
  (`BACKUP_WAKE_BROADCAST`, `BACKUP_WAKE_IFACE`) exist only because the
  no-dependency `/dev/udp` send cannot set `SO_BROADCAST` and the fallbacks
  need somewhere to read a subnet/interface from; `OFFSITE_PATH` must **already
  exist** (the run fails rather than creating it — a typo'd path would
  otherwise report green with the files where nothing syncs). Revert any of
  these at the next gate if wrong.
- A8 — Ingest/exclusion shape (2026-07-29; the two steps themselves were
  ratified, these mechanics were not): the ingest table is its own knob
  (`INGEST_SOURCES`) rather than a fourth `BACKUP_SOURCES` spec kind, because an
  ingest entry needs a DESTINATION and is not a backup set; the grammar is
  `name=//host/share -> /abs/dest` with the arrow, and both a non-UNC source and
  a relative destination are rejected; the destination **leaf** is created on
  first ingest but a **missing parent is fatal** (that is a typo or an unmounted
  library filesystem); a share that mounts but holds **no files** while the
  library copy does **refuses to mirror** — new knob `INGEST_ALLOW_EMPTY=false`
  is the explicit override (this is the one guard that exists purely because
  `--delete` is irreversible); `--dry-run` passes `--dry-run` to the mirror too;
  ingest reuses the existing wake pre-step rather than owning a second one, and
  exclusions deliberately do **not** apply to ingest (they filter the BACKUP,
  not the library). Per-set exclusions are `name.exclude=` lines **inside the
  one `BACKUP_SOURCES` table** (one table, one place to look — the SR-013
  doctrine), which costs one rule: a set name may not end in `.exclude`; a
  `name.exclude=` line naming a set that does not exist **fails the run**;
  patterns go to rsync (saves the copy) AND tar (the archive-level promise), and
  visibility is implemented with rsync's own `--debug=FILTER` decisions
  (`<set>.excluded.log`) plus a new `excludes` MANIFEST column — the column is
  additive, so runs written before it restore unchanged. Revert any of these at
  the next gate if wrong.

- A9 — Wall-lane shape (2026-07-29; OI-12 and the LAN-port+/32 variant were
  ratified, these mechanics were not): `WALL_PORT` defaults to **8443** and is a
  **T0** knob (no FieldSchema entry — the default is the public value); the site
  address carries no `bind` directive because `LAN_IP` is not an address the caddy
  container owns, so the LAN-binding is the compose **publish** instead; the 403
  body is the distinct string `wall: panel only` **specifically so the sim can tell
  an edge refusal from the tracker's own no-identity 403**; the shell's document
  root lives at `stack/wall-shell/` (i.e. `/opt/homehub/stack/wall-shell` on the
  box) rather than a sibling of the stack dir, so the existing bind-mount coverage
  check applies to it; `/api/*` is the only proxied prefix (`/drill` is left to the
  shell, which renders its own from `items[]`); the panel's payload lands at
  **`/opt/wall-panel/`** and its env file at `/etc/wall-panel/wall.env` (0600),
  mirroring the AWOW's layout without sharing it; `PANEL_USER_SUB` is deliberately
  **absent** from `wall.env.example` (the panel never learns its own identity — the
  site injects it) and `NAVIDROME_*`/`PANDORA_*` ship **commented**, because
  un-commenting them makes the household emitter demand store keys for an optional
  feature; the kiosk session is reached by a **tty1 autologin + profile hook**
  rather than a system unit, because `cage` needs a logind seat (with `seatd`
  installed as the documented fallback); `SLEEP_RTC_WAKE` is a new knob beyond the
  ratified two, and if the RTC alarm cannot be armed the panel **refuses to
  suspend** and degrades to backlight-off for that window (a reachable panel beats
  a dark one); `mem_sleep_default=deep` is **reported, never silently written** to
  the kernel cmdline; the quirk-3 udev rule is generated from a **pipe-separated**
  knob and is not written at all when that knob is empty. Revert any of these at
  the next gate if wrong.

- A10 — OI-19 shape (2026-08-03; the `/cdrom` defect and the "refuse a
  production build that lost its secrets" doctrine were both stated in the
  brief, these three mechanics were not): (i) the site step **does not search
  for the payload at all** — it reads `/target/opt/homehub/site`, which
  late-command 3 has already copied, so the CIDATA-by-label search exists in
  exactly one place rather than two that can drift (the wall image's
  late-command 4a already worked this way); (ii) a build declares itself with
  `BUILD_PROFILE=production` **inside the user-data**, defaulting to the SAFE
  value so the SIM path is the one that must opt out and a silent no-op there
  is caught by an assertion — it lives in user-data rather than the payload
  precisely because a payload that went missing would take a payload-borne
  marker with it; (iii) a required site file that is **absent** is loud but not
  fatal, because `Build-VentoyStick.ps1` marks `cifs.creds`,
  `samba-users.creds` and `drive-identity.conf` `Required=$false` and halting a
  household's install over an optional file would be worse — but a file that is
  PRESENT and fails to install IS fatal. Whether a missing `.env` specifically
  should halt is the Owner's call, and (iv) `drive-identity.conf` is now
  installed at all, which it never was: both stagers emit it, three consumers
  read `/etc/homehub-samba/drive-identity.conf`, and no late-command ever put
  it there. Revert any of these at the next gate if wrong.
- A11 — OI-18 shape (2026-08-03; the Owner ruled **exit (b), two UNC/credential
  pairs**, and the storage map already fixed the addresses, shapes, cadences and
  failure policies — these six mechanics were still ours to choose, and each is
  a place a different agent would reasonably have chosen differently):
  (i) **the frame skip is gated on a REACHABILITY PROBE, not on the mount's
  return code** — a 4 s TCP connect to 445 before mounting. `mount.cifs` returns
  the same rc for "asleep" and "wrong password", so inferring the state from the
  failure would have made a wrong credential permanently invisible on a share
  that is *designed* to fail quietly. A box that answers and then refuses is
  therefore FATAL; only "nothing answering" is a skip.
  (ii) **configuration defects are fatal for BOTH flows** — an unset or
  placeholder UNC, or a missing/unreadable credentials file, is not a sleeping
  box, and the frame flow's licence to be quiet does not extend to "nobody
  filled this in".
  (iii) **two units, not one, and not two scripts** — `wall-sync.service` keeps
  OI-15/OI-16a's boot+resume+on-demand and now syncs both flows;
  `wall-sync-frame.{service,timer}` is the same script with `--only frame` on
  §4d's one-minute clock. One script keeps the `--delete` guards in one place;
  two units are unavoidable because the cadences and the timeouts differ by an
  order of magnitude (3600 s vs 120 s). A per-flow `flock` and a per-flow
  `--only` on the manifest generator stop the two from colliding.
  (iv) **the staleness ladder MEASURES but never ESCALATES** — a stamp at the
  cache root and a journal line that becomes a WARNING past
  `WALL_FRAME_STALE_WARN_HOURS` (default 24), but never a failed unit however
  old the content gets. "Mini-serv has been off for a week" is an allowed state
  per §4d and at what age it stops being allowed is a ruling nobody has made.
  **Whether it should ever alert, and whether it should post to the tracker, is
  the Owner's.**
  (v) **the inline `MEDIA_CIFS_USER`/`MEDIA_CIFS_PASS` fallback is RETIRED** —
  one inline pair cannot serve two hosts, and duplicating it per flow would have
  added four knobs whose only purpose is to hold a password somewhere less safe
  than the credentials file that already exists. A root-only credentials file is
  now the only supported form.
  (vi) **`MEDIA_CIFS_EXTRA` stays ONE knob** shared by both mounts — it is a
  protocol choice and both hosts speak SMB3. Split it only if a real box turns
  out to need two versions.
  Revert any of these at the next gate if wrong.

### DRIVER — G1 — Round 1 — 2026-07-03 (migration + spine)
Migrated the deploy stack, wired the tracker to `naglight:local`, authored the
high-level SN/SR spine (8 SN, 11 SR), and added `scripts/validate_config.py` as
the config-repo product check. `check.py` (G1) green; integrity 0. WI-10.2 (oauth
+ Caddy re-route), WI-10.11 (aux containers), WI-10.12 (remote mgmt) layered in
subsequent commits.

### DRIVER — G1 — Round 1 — 2026-07-03 (WI-10.2 oauth2-proxy + Caddy re-route)
Added the oauth2-proxy service (Google provider, allow-list file, cookie secret,
identity headers to the tracker). Re-routed the Caddyfile: tracker host →
oauth2-proxy (no basic_auth); Actual + dns keep basic_auth (split into
per-service snippets since Caddy resolves {$VAR} once at load). Firstboot now
materializes the allow-list from `OAUTH2_PROXY_ALLOWED_EMAILS`. Documented the
OWNER MANUAL STEP (Google OAuth client) in .env.example + stack/README. Redirect
URI to register: `https://tracker.<domain>/oauth2/callback`. config-validate
green (25 compose vars covered, 8 Caddy vars passed).

### DRIVER — G1 — Round 1 — 2026-07-03 (WI-10.11 auxiliary containers)
Added Uptime-Kuma, Dozzle, and optional ntfy (compose `profiles: [ntfy]`), all
LAN-only (published bound to `LAN_IP`, never proxied publicly), all
healthchecked, every knob in `.env.example`. config-validate green (31 compose
vars covered).

### DRIVER — G1 — Round 1 — 2026-07-03 (WI-10.12 LAN remote management)
Implemented now, in the autoinstall: SSH **key-only** (allow-pw false, locked
password, authorized-keys placeholder), **Cockpit** host package (LAN-only :9090,
not proxied), and **unattended-upgrades**. Documented the remote `docker compose`
ops workflow. Wrote `REMOTE_MANAGEMENT.md` — the reimage-over-LAN ladder as a
decision memo with checkboxes (recommended: **B GRUB recovery partition** primary
+ **D smart-plug/USB** fallback). **HIGH-RISK line:** nothing destructive /
reimage-related (no `storage:` recovery-partition change, no GRUB reinstall entry,
no PXE) is implemented until the Owner checks a box.

<!-- agent-setup --> Agent setup (2026-07-03): agents=`claude`; skills materialized: downstream-resync, gate-advance, registry-hygiene. AGENTS.md remains the canonical, agent-neutral guide (skills are opt-in accelerators, not a process gate).

### DRIVER — G1 — Round 1 — 2026-07-03 (WI-10.13 dev-PC container runtime)
The Owner picked **(a) WSL2 + docker engine inside Ubuntu**, explicitly over Docker
Desktop (not installed; no other tooling touched). This closes Wave 1's
unverified-image-build honesty gap for real.

**Installed on the dev PC (machine-level, Owner-consented):**
- WSL2 itself was already enabled/functional (a pre-existing
  `podman-machine-default` WSL2 distro was running) — no VirtualMachinePlatform
  enable + reboot was needed.
- `Ubuntu` distro registered via the pre-existing `CanonicalGroupLimited.Ubuntu`
  appx package (was installed but never first-run) — ran non-interactively via
  `ubuntu.exe install --root`, avoiding the interactive username/password
  prompt. Result: **Ubuntu 24.04.1 LTS (Noble)**, WSL version 2. Default WSL
  user is `root` (a consequence of the `--root` non-interactive path). A
  secondary non-root user `<owner>` (placeholder — the real login name is
  redacted per SR-010) was also created and added to the `docker` +
  `sudo` groups for future interactive use, but is NOT the WSL default (no
  extra restart was spent switching it — root already has full docker access).
- `/etc/wsl.conf` → `[boot] systemd=true`; confirmed via `wsl --shutdown` +
  relaunch that `systemd` is PID 1.
- **docker-ce from Docker's official apt repo** (not `docker.io`, not Docker
  Desktop): `docker-ce docker-ce-cli containerd.io docker-buildx-plugin
  docker-compose-plugin`. Versions: **Docker 29.6.1** (build 8900f1d),
  **Docker Compose v5.3.0** (plugin). `docker.service` enabled + running under
  systemd.

**Verification, in order (all real, all honest):**
1. `docker run --rm hello-world` → **PASS** (pulled + ran, full expected output).
2. `docker build -t naglight:local /mnt/c/Projects/NagLight` → **PASS**. Built
   clean: Go 1.26-alpine build stage → alpine:3.20 runtime stage, final image
   `naglight:local` (14.1MB content, 47.8MB disk). No errors, no warnings beyond
   Docker's own advisory notices. This closes NagLight's TC-044
   unverified-build gap.
3. `docker compose --env-file <scratchpad>/sim.env config` from `stack/` →
   **PASS** (exit 0). Used a placeholder-but-syntactically-valid `.env`
   (fictional domains/keys/tokens; generated fresh, kept in the agent's
   scratchpad, never written into this repo). All 8 services resolved
   correctly: `actual`, `caddy`, `ddns`, `dozzle`, `oauth2-proxy`, `technitium`,
   `tracker` (confirmed `image: naglight:local` — the just-built image),
   `uptime-kuma`. Volumes/networks/healthchecks all present in the rendered
   config. Minor test-data artifact (not a stack bug): the placeholder bcrypt
   hash values contained unescaped `$` characters, which docker compose's own
   `.env` interpolation partially consumed (`$2a$14$...` truncated to `$2a$14`
   in the rendered output) — a property of how I wrote the throwaway env file,
   not of the compose file itself.

**Tooling note for future sessions:** invoking `wsl.exe` through this agent's
Bash/PowerShell tools silently mangles any `$VAR` in the command string (an
outer shell layer pre-expands it before the real command runs) and Git Bash's
MSYS layer rewrites leading `/mnt/c/...` paths unless `MSYS_NO_PATHCONV=1` is
set. Workaround used throughout: write scripts to files (Write tool, no shell
involved) and execute them via `wsl.exe -d Ubuntu -- bash /mnt/c/...script.sh`
with `MSYS_NO_PATHCONV=1` set on any command touching `/mnt/c/...` paths
directly.

**Remaining for the Owner:** none — no reboot, no interactive prompt was needed.
Wave 2's V1 homehub-sim (WI-10.14/10.15) is now unblocked.

### DRIVER — G1 — Round 1 — 2026-07-04 (WI-10.14 homehub-sim harness + V1 gate)
Built `sim/` as a compose **overlay over the real `stack/docker-compose.yml`**
(never a fork): `docker-compose.sim.yml` + `.env.sim` (all fictional), a mock
**Dex** OIDC provider (2 static users) swapped in for Google, Caddy on an
**internal CA**, Technitium moved to the bridge with an alt API port, ddns + aux
containers neutralised, a `simclient` probe box, and multi-user fixture seed
data. `sim/run-sim.sh` brings it up and provisions the Technitium split-horizon
zone; `sim/validate-sim.sh` is the V1 gate. **RAN IT FOR REAL** on WSL2/docker
29.6.1 — full gate output:

```
-- (1) service health --   technitium/caddy/tracker/actual healthy; init-perms exit=0;
                           oauth2-proxy /ping=200 & dex discovery=200 (distroless, no HC)
-- (2) split-horizon DNS -- dig @technitium {tracker,actual,apex}.homelab.sim -> 10.99.0.10
-- (3) Caddy vhosts + auth -- tracker (internal CA) -> 302 to Dex; actual -> 401 no-auth, 200 w/ auth
-- (4) oauth2-proxy + Dex -- FULL headless login (curl cookie-jar) -> authenticated tracker 200
-- (5) multi-user isolation -- A/B see only own data; no-identity -> 403; A export = only A's items
-- (6) /api/feed round-trip -- ok=true->done, ok=false->cleared, ok=true->done
== V1 GATE: PASS (all checks green) ==
```

**Real bugs the sim caught in the wave-1 config (all FIXED in the base compose,
never before RUN):**
- **tracker healthcheck** was `/api/today` — returns **403** in multi-user mode
  (the D3 default), so the container could never report healthy. Fixed to
  `/healthz` (identity-free in both modes; matches the NagLight Dockerfile).
- **oauth2-proxy healthcheck** used `wget` but the stock image is **distroless**
  (no shell/wget) — the probe could never run and blocked Caddy. Removed the
  container healthcheck (probe `/ping` from outside) and changed Caddy's
  dependency to `service_started`.
- **technitium + actual healthchecks** used `wget`, absent from both images
  (they ship bash, not wget). Fixed to a tool-independent bash `/dev/tcp` probe.

**FLAGGED FOR OWNER (a NagLight repo fix, out of this repo's scope):** the
`naglight:local` image runs as `USER tracker` (uid 1000) but the `tracker_data`
named volume initialises **root-owned**, so multi-user `mkdir /data/<sub>` fails
with EACCES and every request 500s. Correct fix = `mkdir -p /data && chown
tracker:tracker /data` before `VOLUME` in NagLight's Dockerfile. The sim
reproduces that end-state with an `init-perms` one-shot so the tracker still runs
at uid 1000 (faithful to prod) — but the real image should be fixed.

**Sim-vs-real deltas the sim cannot cover (for the hardware burn-in):** real
Google OAuth consent; publicly-trusted ACME/TLS certs; Technitium binding the
host's real `:53` (systemd-resolved owns loopback :53 on WSL, hence the bridge +
alt-port approach — a split-horizon test still runs, `dig @technitium` from the
client); the AWOW hardware. Aux containers (Kuma/Dozzle/ntfy) and ddns are
disabled in the sim (LAN_IP binds / zero external calls) — config-validated in
wave 1, out of the V1 gate scope. Full delta table in `sim/README.md`.

### DRIVER — G1 — Round 1 — 2026-07-04 (WI-10.15 Mini-serv-sim + bash backup service)
Built the **real bash backup service** at `stack/backup/` (ASSUMPTION confirmed
in use: it lives in MiniPC-Deployer as box-plumbing, not its own repo) —
`backup.sh` + `restore.sh` + `common.sh` + `backup.env.example` + systemd
`.service`/`.timer`, implementing HOMELAB_TOPOLOGY.md's six steps in pure bash
(no .bat/.ps1). Recovery MANIFEST format (the `*FilesHashTable.csv` successor):
per run `MANIFEST.tsv` (one row/set: set·source·archive·algo·archive_sha256·
files·bytes·reason) + `<set>.files.tsv` (sha256·size·mtime·relpath per file) +
the `.tar`/`.tar.zst` archive + `RUN.json`. Auto-compression-where-applicable
decides zstd-vs-plain-tar per set by already-compressed byte ratio (FileBackup
exemption, lifted to archive granularity). Hash = **sha256** (coreutils-native)
rather than FileBackup's xxHash128 — documented internal-integrity delta.

Built `sim/mini-serv-sim/`: a `dperson/samba` container (Mini-serv stand-in)
exposing three fictional committed shares — `minecraft` (Paper tree: realistic
`paper-1.20.4-435.jar`, 3 plugin jars with **parseable** `plugin.yml`,
`server.properties` with a fake rcon "secret", a `world/` tree), `satisfactory`
(save tree), and an empty writable `icedrive` offsite target — plus a privileged
`backup-runner` that cifs-mounts them and runs the REAL service.

**RAN THE FULL CYCLE + RESTORE DRILL FOR REAL** (`run-backup-sim.sh`):
```
backup.sh: minecraft -> zstd (.tar.zst, 9% already-compressed < 60% threshold), 13 files
           satisfactory -> plain .tar (98% already-compressed >= threshold), 3 files
           total 16 files / 33429 bytes; retention keep=3
step5 offsite: pushed 5 files into //mini-serv/icedrive/homehub-backup/run_<ts>
step6 feed: POST /api/feed ok=true -> HTTP 200; tracker /api/today shows
            backup-files done=true (round-trip confirmed)
RESTORE DRILL: reconstruct minecraft from archive+manifest -> RESTORE OK 13/13
            byte-exact (sha256+size); diff -r vs live share IDENTICAL; delete
            plugins/ subtree, reconstruct again -> IDENTICAL (recovered); fake
            rcon.password round-tripped intact; 3 plugin jars restored.
== BACKUP LEG: PASS (all checks green) ==
```

**Real bug the sim caught + FIXED in the service:** `compression_decision`
`printf`'d without a trailing newline, so the `read` consuming it returned
nonzero and (under `set -o errtrace`) tripped the never-silent-green ERR trap —
every run failed at step 2 and correctly posted ok=false (proving the
never-silent-green path works). Fixed by emitting the trailing newline; the
next run went green end-to-end.

**Fixture shares are UP for the WI-10.16 MinecraftKeeper session.** Start them
standalone with `sim/mini-serv-sim/run-backup-sim.sh --shares-only` (needs
`sim/run-sim.sh` first for the shared `homehub-sim_default` network); shares are
`//mini-serv/{minecraft,satisfactory,icedrive}`, user `awow` / `simpass`,
minecraft+satisfactory exported READ-ONLY (live-share-stays-read-only rule).

### DRIVER — G1 — Round 1 — 2026-07-04 (WI-10.18 V3 gate — ISO/VM scripts)
Built `vmtest/` (agent delivers scripts + docs; **the boot itself is the Owner's** —
needs an elevated PowerShell session + the Hyper-V Windows feature, both
machine-level, neither touched here):

- **`vmtest/build-seed.sh`** — LIGHT path (default): renders the REAL
  `stack/autoinstall/user-data`+`meta-data` with SIM values (ephemeral
  ed25519 keypair, random SIM console password, unchanged storage/late-
  commands logic), copies the repo into a `deploy-payload/` tree with a SIM
  `.env` (fictional domain/OAuth client, real-shaped throwaway oauth2-proxy
  cookie secret + Technitium password, real Caddy bcrypt basic_auth hashes via
  `docker run caddy hash-password`), and burns a small `CIDATA`-labeled seed
  ISO with `genisoimage`/`xorriso`. No repack of the stock ISO — Ubuntu's
  NoCloud datasource auto-detects any attached CIDATA-labeled media (same
  mechanism `stack/README.md`'s "Second USB" already documents).
- **`vmtest/build-repacked-iso.sh`** — HEAVIER fallback: same SIM rendering,
  but bakes `autoinstall ds=nocloud;s=/cdrom/nocloud/` into a patched copy of
  the stock ISO's `/boot/grub/grub.cfg` (via `xorriso -boot_image any
  replay`, which reuses the ORIGINAL hybrid BIOS+UEFI El Torito boot catalog
  rather than hand-rebuilding one) plus embedded `/nocloud/` +
  `/deploy-payload/` — truly zero-keypress, at the cost of ~3.4GB
  copied/rewritten per build.
- **`vmtest/New-HomeHubVm.ps1`** / **`Remove-HomeHubVm.ps1`** — Hyper-V Gen2 VM
  (4 vCPU/8GB static RAM stand-in for the AK41, 64GB dynamic VHDX,
  `MicrosoftUEFICertificateAuthority` Secure Boot template for the Ubuntu
  shim, Default Switch/NAT by default with a documented External-switch
  option for real LAN exposure). Idempotent, `-WhatIf` support, elevation
  asserted at the top. **Never run** (elevation + Hyper-V are the Owner's call).
- **`vmtest/README.md`** — the V3 runbook: ISO strategy write-up (why the
  light path needs one manual GRUB keypress and the heavy path doesn't, with
  the exact edit to make), the 24.04.4 download URL + SHA256 (verified for
  real, see below), Hyper-V enable steps, run order, what "success" looks
  like (tracker legitimately can't reach healthy without staging
  `naglight:local` separately — a pre-existing gap, not new; documented which
  `stack/README.md` §6 burn-in items do/don't apply in a VM), and the
  VM-vs-hardware deltas (NAT vs LAN IP, no real ACME/OAuth/DDNS, no USB
  backup drives, no thermal/storage checks).

**RAN FOR REAL (honest ledger):**
- `build-seed.sh` — run twice (fresh + idempotent re-run). Output validated:
  `user-data` parses as YAML (`python3 -c 'yaml.safe_load(...)'`), the SSH
  placeholder/password/hostname substitutions land correctly, `meta-data` gets
  a fresh `instance-id`, the SIM `.env` renders correct values (spot-checked
  `DOMAIN`, `LAN_IP`, `ACME_EMAIL`, real Caddy bcrypt hashes, cookie secret),
  `isoinfo` confirms volume label `CIDATA` and the right files at the ISO
  root. Idempotent re-run reused the SSH key + all SIM secrets unchanged
  (verified byte-identical across runs) — this caught and fixed a real bug
  (below).
- `build-repacked-iso.sh` — downloaded the real
  `ubuntu-24.04.4-live-server-amd64.iso` (SHA256
  `e907d92eeec9df64163a7e454cbc8d7755e8ddc7ed42f99dbc80c40f1a138433`, verified
  byte-for-byte against `releases.ubuntu.com/24.04/SHA256SUMS`) and ran the
  full repack. Confirmed: `/boot/grub/grub.cfg` in the output carries
  `autoinstall ds=nocloud;s=/cdrom/nocloud/` on both boot entries;
  `xorriso -report_el_torito plain` shows BOTH a BIOS and a UEFI boot image
  still present (the "replay" trick preserved the hybrid boot catalog);
  `/nocloud/{user-data,meta-data}` and `/deploy-payload/` present and correct
  inside the ISO.
- `python scripts/check.py` / `validate_config.py` still PASS unchanged (G1
  green) after adding `vmtest/`.

**NOT run (honest gap, by design/scope):** `New-HomeHubVm.ps1`,
`Remove-HomeHubVm.ps1` — need elevation + the Hyper-V feature, an agent doesn't
make that call. **No VM has been booted from either ISO.** The GRUB-edit
mechanism (light path) and the El-Torito-preserving repack (heavy path) are
verified at the ISO-structure level only, not by an actual boot.

**Real bug the smoke test caught + FIXED:** the shared secrets file
(`vmtest/.out/secrets/creds.env`) was being re-read via `. "$creds_file"`
(bash `source`) on idempotent re-runs; the SHA-512 password hash it holds
contains `$6$...` crypt syntax, which bash tried to expand as positional
parameters under `set -u`, aborting with `line 8: $6: unbound variable`.
Fixed by extracting values with `grep`/`cut` instead of sourcing. Also folded
what had been a per-call, unbounded-growth `>>` append of the Technitium
password + oauth2-proxy cookie secret into the same once-generated,
idempotently-reused block as the SSH key and console password.

**Side effect flagged for the Owner (OI-6 above):** downloading + repacking the
~3.4GB ISO (even under a WSL-native path, not `/mnt/c`) grew WSL2's
`ext4.vhdx` — which itself lives on `C:` — from ~21GB free down to ~9GB, and
deleting the files afterward did **not** give the space back (a known WSL2
quirk: the sparse vhdx doesn't auto-shrink). Reclaim steps are in
`vmtest/README.md` §2.

### DRIVER — G1 — Round 1 — 2026-07-04 (Q10.9 B+ ALL-IMAGES — bake every image into the ISO)

Implemented the Owner's locked **Q10.9 B+** decision (HOMELAB_RESTRUCTURE_PLAN.md):
every stack image is `docker save`d into the ISO deploy payload and `docker
load`ed at first boot, so a freshly-imaged AWOW comes up "from infancy" with
**zero registry/internet dependency for container images**, versions pinned to
exactly what the homehub-sim validated.

**What was built:**
- `vmtest/export-images.sh` — resolves the full image set via `docker compose
  config --images` (from `docker-compose.yml` + the PINNED tags in
  `.env.example`, ntfy profile included), pulls any image not already local at
  its pinned tag, and `docker save`s each into `vmtest/.out/images/*.tar` with an
  `images.manifest.tsv` (ref/id/digest/file/bytes). Fails LOUDLY if any image is
  missing/unpullable; `naglight:local` (no registry home, Q10.2) must be
  pre-built or the script aborts — it is the one image the box can never fetch.
- **Per-image plain `.tar`, no zstd** (measured, justified): `docker save` under
  the containerd/OCI image store already writes compressed layer blobs — a 526MB
  actual-server image saves to a ~106MB tar; a zstd pass buys ~nothing and would
  add an `apt-get install zstd` dependency. Per-image (vs one combined tar) is
  composable + idempotent and gives firstboot per-image load logging + graceful
  per-image degrade. (`--zstd` remains available if ever wanted.)
- `stack/autoinstall/firstboot.sh` — new **step 3**: before `docker compose up`,
  `docker load` every tar found in `/opt/homehub/images` (with fallbacks
  `$STACK_DIR/images`, `/cdrom/deploy-payload/images`,
  `/media/deploy-payload/images` so it works in either ISO layout). Idempotent;
  per-tar failures warn-and-continue (compose can still pull). **Graceful
  degrade:** no payload present → loud NOTICE + fall back to the pre-Q10.9
  pull-at-compose-up behaviour.
- Payload wired into **BOTH ISO paths** via a shared `stage_images_into_payload`
  in `vmtest/lib/common.sh`: it folds `vmtest/.out/images/*.tar` into
  `deploy-payload/images/` (hardlinked when the fs allows — saves ~470MB of C:,
  OI-6). The **light** path (`build-seed.sh`) burns that into the CIDATA seed ISO
  (~1MB → ~470MB); the **repacked** path (`build-repacked-iso.sh`) maps the same
  `deploy-payload/` dir into the ISO's `/deploy-payload/` (~3.4GB → ~3.9GB).
  Either way the tars land at `/opt/homehub/images` for firstboot.

**PIN SET (`latest`/floating → concrete, Q10.9 B+).** `latest` was fine for
bring-up; B+ makes what-boots == what-was-validated, so floating tags are now
wrong. Digest = registry index digest as saved (`docker save` under the
containerd store; see `images.manifest.tsv`):

| Service | `.env` var | was | pinned | registry digest |
|---|---|---|---|---|
| technitium | `TECHNITIUM_IMAGE_TAG` | `latest` | `15.2.0` | `sha256:23d3b63d959e997800b095fe93009b3fae271b5258234ff2ade8535cb33682c8` |
| caddy | `CADDY_IMAGE_TAG` | `2-alpine` | `2.11.4-alpine` | `sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648` |
| oauth2-proxy | `OAUTH2_PROXY_IMAGE_TAG` | `v7.6.0` | **`v7.15.2`** (SECURITY bump 2026-07-10 — auth-bypass CVEs fixed since v7.6.0; NOT yet sim-validated/exported — re-run V1 sim + export-images.sh; digest recorded at that export) | *(pending re-export; v7.6.0 was `sha256:dcb6ff8d…`)* |
| tracker | `TRACKER_IMAGE_TAG` | `local` | `local` (local build, no registry) | id `sha256:9a573d4367032a4d872736718f5dd68872bcf5b1d01d727359ff36413f8f4112` |
| actual | `ACTUAL_IMAGE_TAG` | `latest` | `26.7.0` | `sha256:e18b7fbfec6157a368fad4146563f397502e9da70a120aeaeac63b4977405d1c` |
| ddns | `DDNS_IMAGE_TAG` | `latest` | `v2.10.0` | `sha256:3e2aa558946b5a293def4d73008fa4651c072b2c12932cecd02126fb23979831` |
| uptime-kuma | `UPTIMEKUMA_IMAGE_TAG` | `1` | `1.23.17` | `sha256:3d632903e6af34139a37f18055c4f1bfd9b7205ae1138f1e5e8940ddc1d176f9` |
| dozzle | `DOZZLE_IMAGE_TAG` | `v8` | `v8.14.12` | `sha256:0df89c904da71e94a0c9ed3c89a890f01488321b5f10ac1e0c0bedcead9af6e4` |
| ntfy | `NTFY_IMAGE_TAG` | `latest` | `v2.25.0` | `sha256:cfbbb1bac9196cb711e29ef0ac4adaeb033be6235f1df857705dc39c14384a1d` |

**How the concrete tags were derived (honest):** the 5 **core** images ran in
the V1 homehub-sim; each concrete pin was verified to be the SAME image V1 ran —
technitium `latest`'s linux/amd64 sub-manifest is byte-identical to `15.2.0`'s
(`sha256:85c2cfd4…`), caddy `2-alpine` and actual `latest` share their exact
index digest with `2.11.4-alpine` / `26.7.0`, oauth2-proxy was already `v7.6.0`,
naglight is the locally-built `naglight:local`. The 4 **aux** images (ddns,
uptime-kuma, dozzle, ntfy) were **disabled in the V1 sim** (LAN_IP binds / zero
external calls), so they have no sim-validated version — they were pinned to
their current newest concrete releases and exported now; **they are first
validated at V3 boot / hardware burn-in.** (ddns note: Docker Hub's `latest`
tag is a differently-built multi-arch index than `v2.10.0`; the named release
`v2.10.0` was chosen for reproducibility.)

**RAN FOR REAL (WSL2 / docker 29.6.1):**
- `export-images.sh` end-to-end → pulled the 4 aux images at their pinned tags,
  re-tagged the 3 core floating→concrete (layers already present, near-instant),
  saved all 9. **Total payload = 469 MB** across 9 tars (well under the Owner's
  ~1–2GB estimate). Manifest written.
- `docker load` of all 9 tars → each restored to its exact pinned `repo:tag`
  and re-loading is an idempotent no-op (proves firstboot step 3's guarantee:
  compose finds every image locally, no pull).
- `docker compose config --images` with the pinned `.env.example` → lists
  exactly the 9 concrete pinned refs (what compose asks for == what's baked).
- `build-seed.sh` (light path) with the payload → 471 MB seed ISO, volume id
  `CIDATA`; `isoinfo` confirms all 9 tars + manifest at
  `/deploy-payload/images/`. Hardlink staging worked (link count 2 — no C: bloat).
- Repacked path: the Q10.9 addition (images folded into `/deploy-payload/`) was
  verified via the exact `xorriso -map <dir> /deploy-payload` codepath —
  `xorriso -lsl` confirms all 9 tars land at `/deploy-payload/images/`. The full
  3.4GB stock-ISO repack + El-Torito/GRUB handling was already verified in
  WI-10.18; not re-downloaded (proportionality; C: had 16GB free but the
  mechanism + trivial one-line addition were already covered).
- `scripts/check.py` + `validate_config.py` → **PASS** (G1 green; 35 compose
  vars covered).

**AWAITS V3 (the Owner's boot, unchanged):** the actual first-boot `docker load` +
`compose up` run inside a booted VM/hardware. No VM has been booted. Everything
above is the pre-boot smoke test the dev PC can run headlessly.

**OI-6 update:** C: now shows ~16GB free (was ~9GB at the snapshot); the image
tars + seed ISO were written to `vmtest/.out` on `/mnt/c` (real C: via 9p), which
does **not** grow the WSL `ext4.vhdx`. Only the 4 aux-image pulls (~200MB into
the docker store) touched the vhdx.

### DRIVER — G1 — Round 1 — 2026-07-04 (WI-10.10 DRIVE POWER DESIGN — dynamic standby)

Implemented the Owner's ratified DRIVE POWER DESIGN in the bash backup service. The
backup drives are the box's biggest electrical lever (5–8 W each spinning ≈ the
whole CPU), so the policy is **dynamic standby**, two pieces:

**What was built:**
- **Boot-time default standby** — a per-boot oneshot
  `stack/backup/backup-standby.service` + `backup-standby.sh` applies a
  conservative `hdparm -S` spin-down timeout to each configured backup drive
  (`hdparm -S` does not persist across power cycles, so it re-applies every boot,
  like `powertune.service`). Shipped/enabled via autoinstall `late-commands`
  exactly like powertune (runs in place from the stack dir so it can source
  `common.sh`, matching `homehub-backup.service`). Added `hdparm` to the autoinstall
  packages list (NOT guaranteed on Ubuntu Server).
- **Dynamic hold in the run** — `backup.sh` disables standby (`hdparm -S 0`) on
  its target drive(s) at run start and **restores the configured timeout on any
  exit via an `EXIT` trap** — fires on success, on the ERR-trap's `exit 1`, on a
  `die`, and on interrupt. The EXIT trap fires AFTER the ERR trap, so it never
  disturbs the never-silent-green `ok=false` reporting path; it only re-arms the
  drives. Prevents both wear modes: no start/stop churn during long no-write
  phases (hashing/verify), no aggressive-timeout cycling.
- **Knobs** (`backup.env.example`, placeholders only): `BACKUP_DRIVE_DEVICES`
  (space-separated `/dev/disk/by-id/...` paths — **by-id, never sdX** which
  renumbers) and `BACKUP_DRIVE_STANDBY` (default `241` = 30 min). **Empty device
  list = the whole feature is a clean no-op.** The confusing `hdparm -S` encoding
  (`1..240` = n×5 s so 240 = 20 min; `241..251` = (n−240)×30 min so 241 = 30 min)
  is documented once in `common.sh` and in `backup.env.example`.
- **HARD RULE honored:** power management NEVER fails a backup — missing `hdparm`,
  absent device path, or an enclosure rejecting the command → logged WARNING and
  continue. `drive_standby_set` always returns 0 so it composes with the ERR-trap
  machinery without tripping it. USB-enclosure caveat noted in comments +
  `backup.env.example`; per-drive verification is a burn-in step (`hdparm -C`).

**RAN FOR REAL (WSL2 / docker 29.6.1) — the CALL CONTRACT proven with mock shims**
(platters can't spin in a container, but the sequence of `hdparm` calls can be
asserted). New `sim/mini-serv-sim/run-drivepower-sim.sh` puts a mock `hdparm`
(logs every invocation) and mock `curl` (captures each NagLight POST body) on
PATH in the runner, uses fake by-id device files, and runs REAL backup cycles
against the Samba fixtures. **All four assertions GREEN:**
- **(a)** `-S 0` (standby disabled) issued to **each** configured device at run
  start.
- **(b)** the configured timeout (`-S 241`) re-issued to **each** device on
  normal exit (exactly 4 calls; all disables precede all restores).
- **(c)** on a FORCED mid-run failure (mock `rsync` exits 1) the restore **still
  fires** via the EXIT trap AND the run **still posts `ok=false`** (mock curl
  captured `"ok":false`; `on_err` ran → `BACKUP FAILED` + `RUN.json=failed`;
  nonzero exit).
- **(d)** with **no devices** configured: **zero** `hdparm` calls and an
  unchanged green cycle.

Also validated the boot oneshot directly (`docker run` throwaway, 5 cases):
`-S 241`→"30min" and `-S 240`→"1200s" (=20 min) encoding correct; empty list =
no-op; absent device = WARN + skip + exit 0; missing `hdparm` = WARN + exit 0
(never fails boot).

**No regression:** re-ran `run-backup-sim.sh` end-to-end — full six-step cycle +
restore drill **PASS** (backup.env.sim has `BACKUP_DRIVE_DEVICES=""`, so the
standard cycle logs zero drive-power lines — the clean no-op path). `bash -n`
clean on all scripts; `scripts/check.py` (G1) + `validate_config.py` **PASS**
(the file-presence check now also asserts the powertune + `backup-standby`
unit/script exist, and `user-data` still parses as valid `#cloud-config` YAML).

**HARDWARE-ONLY REMAINDER (honest):** whether the actual USB-SATA backup
enclosure **honors** `hdparm` standby at all — many bridge chips swallow/fake the
command — cannot be known in sim; it is a per-drive **burn-in** check
(`hdparm -C /dev/disk/by-id/...` to read active/idle vs standby; `hdparm -y` to
force a spin-down and confirm). The real electrical/spindle-wear benefit is
likewise a hardware measurement. The sim proves the *call contract and its
failure-path composition*, not the drive's physical response.

### DRIVER — G1 — Round 1 — 2026-07-10 (STACK REVIEW + TIER-2 OPT-IN CATALOG — SN-009/SR-012)

The Owner asked for (a) a review of the deployer stack and (b) an opt-in catalog of
additional self-hosted services (photos ×2, media, music, podcasts, home
automation, passwords, etc.). Both delivered this session.

**Review findings → fixes applied (config-only):**
- **BUG (real-box): `dns.<domain>` would 502.** The Caddyfile proxies
  `host.docker.internal:5380`, a Docker-DESKTOP-only name — plain docker-ce
  never resolves it. Fixed with `extra_hosts: host.docker.internal:host-gateway`
  on the caddy service. The V1 sim could not catch this: `Caddyfile.sim`
  deliberately proxies the bridge container name instead. **Needs V3/hardware
  verification.**
- **SECURITY: oauth2-proxy pin bumped v7.6.0 → v7.15.2** (multiple 2026
  auth-bypass advisories fixed in between: CVE-2026-34457, CVE-2026-40575,
  GHSA-7x63-xv5r-3p2x, GHSA-pxq7-h93f-9jrg, GHSA-c5c4-8r6x-56w3 email-validation
  — the last one weakens exactly our allow-list gate). NOT sim-validated at the
  new tag yet — OI-7(c); pin-ledger row updated with digest PENDING re-export.
- **Docker log rotation** was unconfigured (unbounded json-file on a small
  eMMC) — autoinstall now writes `/etc/docker/daemon.json` with the `local`
  driver (rotates by default).
- **Backup gap recorded as OI-8** (stack volumes — incl. `actual_data` — backed
  up by nothing); deliberately NOT fixed inline: `backup.sh` is
  WI-10.15-validated behavior and a change there needs its own sim assertions.
- Minor (recorded, not acted on): Dozzle has no auth (LAN-bind is the only
  gate); ntfy topics are LAN-open by default; Technitium :5380 is cleartext
  HTTP on the LAN; no mem_limits yet (matters as tier-2 services get enabled).

**Tier-2 catalog added (SN-009/SR-012, all OFF by default, ntfy-profile
pattern):** immich(+ml)/photoprism (photos — at most ONE), jellyfin (QSV via
/dev/dri), navidrome (music), audiobookshelf (podcasts/audiobooks), vaultwarden
(Caddy-site-only ingress by design), homeassistant+mosquitto (host-net HA;
committed no-secret mosquitto.conf), syncthing, freshrss/mealie/homepage, diun
(update NOTIFIER — closes the stale-pin gap within the pins-never-drift
philosophy; `diun.enable=false` label on the registry-less naglight:local).
Wiring: `COMPOSE_PROFILES` in `.env` is the enable switch (firstboot's plain
`compose up -d` honors it); commented Caddyfile sites + `EXTRA_SUBDOMAINS`
(provision script loop) for public exposure; `export-images.sh` gained
`EXTRA_PROFILES` and its default behavior — tier-2 excluded from the baked
payload — falls out of profiles being off (OI-7(a) to ratify).
**[SUPERSEDED 2026-08-07 — see the last entry in this log.** That default was
ratified the other way, by running it: a box whose `.env` enabled five profiles
shipped 9 of the 15 images it needed. `export-images.sh` now bakes what
`COMPOSE_PROFILES` enables. Left in place because this entry records what was
decided on 2026-07-10, and rewriting it would falsify the log.] Docs:
stack/README §9 (incl. the pre-build configuration chain + RAM/storage ground
rules); root README table row.

**Versions (honest):** Immich layout verified against the v3.0.2 release
compose (server/ml `v3.0.2`, `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-
pgvectors0.2.0`, valkey `9`); PhotoPrism upstream publishes `latest` as its
stable tag (documented exception). Remaining tier-2 pins are best-effort
known-good tags, marked VERIFY-at-enable in `.env.example`; none are
sim-validated and none carry custom healthchecks yet (WI-10.14 lesson — probe
tooling per image is checked empirically at enable time).

**Assumptions (unattended, to confirm at next gate):** tier-2-not-baked is the
right Q10.9 B+ boundary (OI-7a); `MEDIA_ROOT=/srv/media` default pending the
storage decision (OI-7b); FreshRSS chosen over Miniflux (single container +
SQLite); Zigbee2MQTT deferred until a coordinator stick exists; subdomain
labels fixed as vault/photos/prism/jellyfin/music/audio/home.

**RAN FOR REAL (this session, dev box):** `validate_config.py` (72 compose vars
covered, all PASS) + `check.py` G1 (config-validate, registry-integrity with
SN=9/SR=12, doc-navigability) → **PASS**; `bash -n` clean on the two changed
shell scripts; WSL-Ubuntu `docker compose config` — with **no profiles** it
resolves exactly the original 8 core services (tier-2 exclusion proven for the
export path), with **all 15 profiles** the config is VALID and lists the full
26-image set; `docker manifest inspect` confirmed **every** tier-2 pin AND
oauth2-proxy `v7.15.2` exists on its registry. **NOT run:** any container —
tier-2 services have never been started anywhere (enable-time validation per
README §9), and the core changes (extra_hosts, log rotation, oauth2-proxy tag)
await the V1-sim re-run / V3 boot (OI-7c).

### DRIVER — G1 — Round 1 — 2026-07-10 (OI-8 → SN-010/SR-013: docker-volume sources in the ONE backup table)

The Owner ratified the single-table design ("multiple sources/destinations
configured for a single backup execution sequence"): extend the existing
`BACKUP_SOURCES` grammar rather than bolt on a second mechanism. Destinations
stay as-is (one drive target + the `OFFSITE_SETS` subset selector).

**What was built (TDD — sim first, red, then green):**
- **Grammar (SR-013):** `name=SPEC` where SPEC is `//host/share` (cifs,
  unchanged), `volume:VOL` (live copy from the volume's mountpoint via
  `docker volume inspect` — no helper image needed, the service runs as root),
  `volume:VOL@CONTAINER` (quiesce: stop → copy → restart immediately), or
  `path:/abs/dir`. Comment lines in the table are skipped, so
  `backup.env.example` ships ready-to-uncomment lines for `actual_data`,
  `technitium_config`, `caddy_data`, `tracker_data`.
- **Quiesce safety (the OI-8 hard case):** `QUIESCED` tracking + a
  `quiesce_restore` EXIT trap (runs BEFORE drive-power restore) — no failure
  path can leave a service stopped; an EXIT-trap restart failure warns LOUDLY
  naming the manual command. Inline restart failure fails the run (a stopped
  service is worse than a missed backup). Steps 2–6 untouched — volume sets
  ride the same archive/hash/manifest/retention/offsite/report pipeline.
- **New sim leg `sim/mini-serv-sim/run-volume-sim.sh`** (mock-`docker` +
  mock-`curl` shims, the run-drivepower-sim.sh pattern; feed mocked so the leg
  runs without the homehub-sim tracker).

**RAN FOR REAL (WSL2 / docker, this session):**
- **RED first:** pre-implementation run — scenarios (a)–(c) failed exactly as
  expected (volume: spec treated as a cifs UNC), (d) green. Then GREEN:
  **all 15 checks PASS** — (a) volume set archived+hashed+in-manifest, restore
  drill BYTE-EQUAL vs the fixture volume, zero stop/start without @container,
  comment line skipped, ok=true; (b) exactly one stop + one start, stop
  precedes start; (c) forced mid-copy failure (mock rsync) → container STILL
  restarted via the EXIT trap, ok=false posted, nonzero exit, on_err logged;
  (d) cifs-only table → ZERO docker calls.
- **No-regression:** full `run-backup-sim.sh` re-run against the live homehub-sim
  tracker — **BACKUP LEG: PASS** (cycle, offsite push, real NagLight feed
  round-trip, minecraft restore drill incl. post-loss reconstruct).
- `bash -n` clean; `check.py` G1 re-run after the registry additions (SN=10,
  SR=13) — see below.

**FINDING logged as OI-9 (not fixed here — stay in lane):** the RED run exposed
that `die` paths (e.g. cifs mount failure) exit 1 WITHOUT posting ok=false —
only ERR-trap failures feed NagLight. Pre-existing; small targeted fix + its
own sim assertion later.

**HARDWARE-ONLY REMAINDER (honest):** real-docker semantics (an actual
`docker stop` latency, a volume mountpoint under /var/lib/docker) are proven
only by the call contract here; first real exercise happens on a Docker host /
the AWOW with the volume lines uncommented. The sim shim answers `volume
inspect` with a fixture dir by design.

### DRIVER — G1 — Round 1 — 2026-07-10 (SR-006 resolver chain + .env QUOTING BUG fix)

**The Owner's ask:** NagLight and Finance-Auditor are private — the dev package
should take each locally-built container from a sister folder of the same name,
else grab a declared public one.

**Built:** `scripts/ensure-local-images.sh` — resolves each locally-built image
via **present → sibling build (`../NagLight`) → declared public image
(`TRACKER_PUBLIC_IMAGE` in `.env`, empty until the app repo publishes) → loud
failure naming all three fixes**. Downstream consumers are untouched: compose,
the sim overlay, and export-images.sh keep consuming the same `naglight:local`
ref regardless of which path supplied it (a public image is pulled + retagged).
`sim/run-sim.sh` now calls the resolver before bring-up; export-images.sh's
missing-tracker error points at it. Finance-Auditor has a ready-to-uncomment
entry (no compose service consumes it yet — Actual Budget is the running
finance app). `--rebuild` forces a sibling rebuild; `--dry-run` prints
decisions; `SIBLING_ROOT` overrides the parent-dir convention (CI-friendly).
SR-006 requirement/acceptance updated to cover the chain.

**BUG FOUND AND FIXED while testing (real-box first-boot breaker):**
`.env.example` carried `TRACKER_GIT_NAME=naglight bot` — an UNQUOTED space.
The file is BOTH a compose env-file AND shell-sourced by `firstboot.sh` (step
2) and `provision-technitium.sh`; sourcing that line executes `bot` as a
command → command-not-found → `set -e` kills first boot before compose up.
Never caught because the sim/seed substitute space-free values — the REAL
hand-filled-.env path was the one that would break. Fixed: quoted
`TRACKER_GIT_NAME`, quoted the space-separated knobs (`TECHNITIUM_FORWARDERS`,
`TECHNITIUM_BLOCKLISTS`), added the QUOTING RULE to the file header (compose
strips double quotes, so quoting is safe for both readers). The resolver
itself now greps only the keys it needs instead of shell-sourcing the file
(defense in depth).

**RAN FOR REAL (WSL2 / docker):** all four resolver paths — (1) image present
→ no-op; (2) `--rebuild --dry-run` → sibling build from `/mnt/c/Projects/
NagLight`; (3) no sibling + knob set → public pull+retag decision; (4) neither
→ loud failure, exit 1. Firstboot-style `set -a; . .env.example` now sources
clean (was: `bot: command not found`, exit 127); `docker compose config`
confirms quote-stripping (`TRACKER_GIT_NAME: naglight bot`). `bash -n` clean on
all touched scripts; `check.py` G1 PASS.

### DRIVER — G1 — Round 1 — 2026-07-11 (DOC CURRENCY PASS — the Owner's ask)

The Owner asked for all docs to be brought current (the tier-2 catalog and other
recent work were missing from the root README and elsewhere). Swept every doc
against the repo's actual state:

- **Root README:** intro now names the backup service + the tier-2 opt-in
  catalog; "Run it" points at the SR-006 resolver and the zero-secrets sim
  path; V1 gate row lists the drive-power + volume sim legs; "Configuring the
  REAL box" adds the QUOTING RULE, `COMPOSE_PROFILES`, the backup `volume:`
  lines, and the tier-2/EXTRA_PROFILES bake boundary.
- **AGENTS.md:** the Project section (never filled since scaffolding) now
  carries the one-line purpose, users, layout, run/check commands, non-goals,
  and sibling repos.
- **docs/architecture.md:** topology diagram gains ddns, the backup service,
  and the tier-2 subgraph; new bullets for DDNS/backup/tier-2/resolver; layout
  table adds stack/backup, stack/mosquitto, sim/, vmtest/, the resolver; the
  STALE "no Docker on the dev machine" validation model replaced with the
  three-tier static → V1 sim → V3/hardware model.
- **docs/status.md:** the honest-validation ledger no longer claims
  "PENDING — no Docker" (superseded 2026-07-03); rows now record what the V1
  sim proved vs what V3/hardware still must; Next action refreshed.
- **docs/requirements/interfaces.csv + docs/interfaces.md:** the EXAMPLE row
  replaced with the three real NagLight contracts — IF-001 (naglight:local
  image), IF-002 (trusted identity headers), IF-003 (/api/feed) — and a
  human-readable index; matching IF-IDs still need recording on the NagLight
  side. Clears the pre-existing check_docs orphan warning.
- **vmtest/README:** Q10.9 "bake EVERY container" qualified with the SR-012
  tier-2 boundary + EXTRA_PROFILES. **REMOTE_MANAGEMENT.md:** tier-2
  enable/disable added to the day-2 compose workflow. **sim/README:**
  run-volume-sim.sh listed. **stack/README §2:** QUOTING RULE + tier-2 knob
  pointer. **stack/tracker/README:** resolver pointer.

`check.py` G1 PASS (config-validate, registry-integrity, doc-navigability —
now 0 warnings). Docs-only change; no config/script behavior touched.

### DRIVER — G1 — Round 1 — 2026-07-11 (STATE & CREDENTIALS clarity — the Owner's ask)

The Owner asked where credentials live across updates/reimages and how the budget
app's bank feed wires in. Docs now say it in one place:

- **REMOTE_MANAGEMENT.md "State & credentials"** — the survives-what table:
  NO credential lives in a container/image; state = named volumes (Actual
  server pw + SimpleFIN credential + budgets, Technitium config, Caddy certs,
  tracker data, Vaultwarden) + host files (`.env`, allow-list, `.token`,
  `/etc/homehub-backup/*`). Container updates (`compose pull && up -d` / pin
  bumps) are credential-safe by construction; a reimage wipes volumes → they
  return via the SR-013 volume backups, `.env` re-seeds from the USB payload.
  Plus "What runs where": on the AWOW everything is a container except the
  backup service, powertune/backup-standby oneshots, Cockpit, sshd,
  unattended-upgrades; Mini-serv runs nothing from this stack.
- **stack/README §2** — new OWNER MANUAL STEP: SimpleFIN bank sync is a
  one-time, in-app Actual setup (Actual OWNS the SimpleFIN relationship;
  Finance-Auditor will only trigger its sync). Stored server-side in
  `actual_data`; not doable from the hermetic sim (a real bank credential must
  never enter a throwaway fictional volume) — real box or the real-secrets
  rehearsal VM, whose `actual_data` volume can be carried to hardware via the
  backup/restore path ("authenticate once" literally once).
- **Root README** — real-box step 4 points at both.

`check.py` G1 PASS. Docs only.

### DRIVER — G1 — Round 1 — 2026-07-11 (SN-011/SR-014: Finance-Auditor ingested, profile-gated)

The Owner confirmed FA's deploy artifacts landed (its commits bbea348/59d9f27:
Dockerfile + deploy/compose.service.example.yml + the ACTUAL_API_VERSION
build-arg). Verified ready and ingested per its IF-003 spec:

- **Compose service `finance-auditor`** (profile `finance-auditor` — OPT-IN
  until FA passes G-Release/G-Final, then promote to core + baked payload):
  bridge-internal to actual:5006 + tracker:8787, finance_snapshots +
  finance_actual_data volumes, FINANCE_* knobs in .env.example, TZ-honored
  RUN_AT, no ports/healthcheck (FA's documented daemon design; the tracker's
  automated-lane aging is the backstop).
- **Resolver:** ensure_image grew optional docker-build args; the
  finance-auditor entry is ACTIVE and pins
  `--build-arg ACTUAL_API_VERSION=${ACTUAL_IMAGE_TAG}` — the IF-002 coupling
  is now mechanical (bump the Actual pin → `--rebuild` rebuilds FA against it).
- **Privacy (FA §3 firewall):** backup.env.example gained
  `finance=volume:finance_snapshots@finance-auditor` (commented) + a LOUD
  never-OFFSITE_SETS warning — raw finance data rides the LOCAL drive only,
  never the IceDrive-synced share.
- **Registries:** SN-011 + SR-014; IF-004 (↔ FA IF-003; ids are repo-local —
  FA numbered first — each contract cites the counterpart id). Docs: stack +
  root READMEs, architecture topology, interfaces.md index.

**RAN FOR REAL (WSL2/docker):** validate_config.py + compose config PASS (core
still exactly 8 services without the profile; finance-auditor appears with it);
`ensure-local-images.sh` BUILT `finance-auditor:local` end-to-end (two-stage
build, @actual-app/api@26.7.0 installed in the throwaway stage); `docker run`
of the image boots under Node 24 native type-stripping and exits with the
documented config-invalid fatal naming the missing key (trackerFeedUrl) —
FA's startup contract observed. NOT run: a live pipeline cycle (that is FA's
TC-033 G-Release Demonstration — the homehub-sim's actual+tracker are a
ready-made environment for it; sim runs Actual `latest`, so pin the sim to
26.7.0 for a faithful rehearsal per FA's spec note).

**NagLight cleanup (its repo, recorded not fixed):** /api/feed accepts only
{check,ok,note} — FA posts {check,color,reason,at}; the color-lane extension
FA's IF-001 calls "pending" is still unbuilt tracker-side (and a
finance-audit item must exist in definitions or the post 400s). Also
NagLight's IF registry still claims the deploy/caddy/technitium interfaces
that migrated HERE (WI-10.1) — needs re-homing + counterpart ids; its G1
human sign-off is still pending (process debt). FA multi-user note: its feed
client sends bearer only, no X-Forwarded-User — fine single-user, a gap if
the tracker runs multi-user (D3).

### DRIVER — G1 — Round 1 — 2026-07-11 (NagLight color lane VERIFIED end-to-end + sim interpolation fix)

The Owner said NagLight was updated; verified in its working tree (NOTE: that work
is **UNCOMMITTED in NagLight** — the Owner to commit there): /api/feed now takes
three lanes ({check,ok,note} boolean; {check,color,reason,at} severity;
{check,rgb,...}), field names pinned to FA's contract (NagLight IF-006 ↔ FA
IF-001); its IF registry is re-homed with counterpart ids matching ours
(their IF-004↔our IF-003, IF-005↔our IF-002); G1 human sign-off still pending
(the Owner's).

**RAN FOR REAL (sim):** rebuilt `naglight:local` from the updated tree,
recreated the sim tracker, added the fictional `finances.md` severity-item
fixture to `sim/tracker-seed/` (mirrors NagLight's example-data), probed as a
fresh sim user with FA's exact payload shape:
`POST /api/feed {check:finance-audit, color:yellow, reason, at}` → **HTTP 200**,
`/api/today` shows `reportColor/reportReason/reportAt`, and the day's
aggregate flipped yellow with the de-identified reason in the overlay. The
FA↔NagLight seam is proven in the sim. (Also learned: a color post to a
boolean-lane item 400s by design — lanes are typed per item definition.)

**REGRESSION found+fixed en route:** `docker compose up` with
`--env-file sim/.env.sim` REFUSED to parse after the tier-2/finance additions
— unset `${MEDIA_ROOT}` renders a volume spec `:/media` (compose interpolates
the whole file before profile filtering; `config -q` tolerated it, `up` did
not). Fixed: `sim/.env.sim` gained a fictional interpolation-defaults block
for every tier-2/finance knob. Sim bring-up works again.

**Follow-ons recorded:** `naglight:local` image id changed → the baked payload
needs a re-export before any flash (folds into the OI-7c re-export); OI-10
added (disk-encryption decision); USB-wipe note added to stack/README §3.

### DRIVER — G1 — Round 1 — 2026-07-20 (SN-012/SR-015: opt-in remote light UI; SN-001 rescoped — the Owner's ask)

The Owner asked (following the IceDrive-headless discussion — the current client is
GUI-only, no daemon/CLI, WebDAV sunsetting since 2026-04) to make an on-box
light UI an **opt-in option** and to rescope SN-001: zero-click is guaranteed
for **core** services; opt-in secondary services may need UI/manual config —
always remote, restricted/minimized.

- **SN-001 rescoped** (need + acceptance intent now say "core"; opt-ins that
  can't meet the bar must be OFF by default, document their manual steps and
  what doesn't self-heal, and be fully set-up-able over the LAN).
- **SN-012 + SR-015 added** (+3 SN-012 edge rows): opt-in, off-by-default
  xrdp + minimal-XFCE layer solely for GUI-only vendor apps; first case
  IceDrive Mount & Sync. Priority C, Verification=Inspection.
- **`stack/remote-ui/`**: `setup-remote-ui.sh` (idempotent, non-interactive,
  root-checked; installs xrdp+minimal XFCE+libfuse2t64, wires `~/.xsession`,
  optional `ICEDRIVE_APPIMAGE=` install + autostart; referenced by NOTHING in
  autoinstall/first-boot) + README (LAN-only rule, what does NOT self-heal:
  post-reboot one-RDP-touch, GUI state lost on reimage + re-setup checklist,
  uninstall steps). REMOTE_MANAGEMENT.md gained the opt-in section;
  architecture layout table row added.
- **Scope line held:** backup step 5 offsite stays cifs-only — pointing it at
  an on-box synced folder is OI-11 (needs the Owner; small backup.sh extension +
  sim legs). A6 records the unattended shape decisions (minimal XFCE not full
  DE; no AppImage auto-download; no autologin sync-resume hack).

**RAN FOR REAL:** `check.py` G1 **PASS** (config-validate 77 vars,
registry-integrity `SN=12 SR=15 orphans=21 integrity=0` — the +1 orphan line
(20→21) is SR-015's standard pre-G2 "no TC" state, same as every SR
(Inspection exempts it from the no-LLR finding);
doc-navigability 48 links 0 broken); `bash -n` on the new script OK. NOT run:
the script itself (needs the real box / rehearsal VM — host-level GUI is
outside the compose sim; recorded in A6).

### DRIVER — G1 — Round 1 — 2026-07-29 (WoL wake pre-step, local-path offsite (OI-11 build), OI-9 closed + staleness sweep)

Absorbed three ratified decisions this repo had not caught up with, plus the
driver-side OI-9 fix. No new decisions were taken; two things that would need
one are named at the bottom.

**What was built**

- **Wake-on-LAN pre-step (step 1).** The Windows game box now exposes ONE share
  and is allowed to SLEEP, so `backup.sh` wakes it and waits for **tcp/445**
  before the first cifs mount. `BACKUP_WAKE_MAC` (empty = feature off),
  `BACKUP_WAKE_HOST`, `BACKUP_WAKE_TIMEOUT` (+ optional `BACKUP_WAKE_BROADCAST`
  / `BACKUP_WAKE_IFACE`, A7). The packet is best-effort; the **wait is the
  truth**, and a timeout is a LOUD failure — never-silent-green means a source
  that failed to wake must never look like a source with nothing new. No new
  dependency: the magic packet goes out over bash's `/dev/udp`, with
  `wakeonlan`/`etherwake` as the documented fallback.
- **OI-11 build half — `OFFSITE_PATH`.** Step 5 takes a LOCAL directory (the
  folder the on-box IceDrive client syncs) as the primary offsite target;
  `OFFSITE_UNC` survives as the legacy cifs push. Exactly one may be set —
  both is a config error caught at run start, not a precedence puzzle — and one
  staging routine serves both, so "which files go offsite" stays one fact.
- **OI-9 closed.** `die` now invokes a registered `DIE_REPORTER` before exiting.
  `backup.sh` registers `report_failure`: the single, idempotent failure path
  shared with the ERR trap, so whichever fires first owns the verdict. A failing
  report degrades to a WARNING (it must never mask the original error) and an
  unset reporter is a clean no-op, so `restore.sh` / `backup-standby.sh` are
  untouched.
- **Docs currency:** backup README (wake contract, offsite target choice, step
  table); `backup.env.example` (single-share example, wake section incl. the
  Windows-side settings, OFFSITE_PATH-primary offsite section);
  REMOTE_MANAGEMENT (one share, sleeps, no IceDrive role); remote-ui README
  ("nothing forces a switch" → the switch IS ratified); architecture (backup
  bullet + topology diagram: wake-then-pull, local offsite folder → on-box
  IceDrive → cloud); stack/README "Local validation status" and SN-008/SR-011
  (the "no Docker on the build machine" premise died 2026-07-03, WI-10.13);
  the two committed `\<box>\setup` references reworded so the token-rotation
  warning survives without the literal UNC path.

**RAN FOR REAL (WSL2 Ubuntu + docker-ce, Windows dev PC)**

- `python scripts/check.py` — **G1 PASS** (config-validate 77 vars;
  registry-integrity `SN=12 SR=15 orphans=21 integrity=0`; doc-navigability
  48 links 0 broken). `bash -n` clean on all four backup scripts.
- **`sim/mini-serv-sim/run-backup-sim.sh` — PASS, all checks green**, run
  against the changed service (the runner bind-mounts `stack/backup` live):
  full six-step cycle over the Samba fixtures, offsite push landed 20 files,
  NagLight feed round-trip visible in `/api/today`, restore drill byte-equal
  including the post-loss reconstruct. This is the regression net for the
  **legacy** `OFFSITE_UNC` leg — it still works.
- **`run-drivepower-sim.sh` — PASS (a–d)** and **`run-volume-sim.sh` — PASS
  (a–d)**, i.e. the forced-mid-run-failure scenarios still post `ok=false`,
  still restore drive standby, and still restart a quiesced container after the
  `report_failure` refactor.
- **A throwaway Linux harness** (scratch, not committed) drove the new paths
  with `path:` sources and a stub feed endpoint: happy run with `OFFSITE_PATH`
  → files staged under `<OFFSITE_PATH>/homehub-backup/run_<ts>` + `ok=true` +
  restore byte-identical; `OFFSITE_ENABLED=false` still a clean skip; legacy
  `OFFSITE_UNC` branch still selected and failing loudly when the share is
  unreachable; `--dry-run` unchanged. **Four `die` paths each POSTED
  `ok=false`** before exit 1 (both offsite forms set; enabled-with-no-target;
  wake timeout; unreachable cifs share) — the OI-9 contract, observed rather
  than asserted. Wake helpers checked byte-level: magic packet 102 bytes,
  `ff*6` + MAC ×16, NULs intact for MACs containing `00`; MAC parser accepts
  `:`/`-`/bare and rejects short/non-hex; probe true/false/timeout correct;
  already-awake fast path skips the packet; timeout path dies loudly.

**NOT run (honest gap)**

- **No real box was woken.** Every wake test used a loopback listener or a
  blackhole address. Whether a magic packet actually resumes the game box —
  and whether its adapter/Fast-Startup settings allow it — is V3/hardware
  (OI-13).
- **The `/dev/udp` broadcast send was REFUSED by the WSL kernel** (bash cannot
  set `SO_BROADCAST`), so only the fallback *decision path* was observed, never
  a successful send. `wakeonlan`/`etherwake` are not installed here either.
  Which method the AWOW ends up using is unknown until the box is tried.
- **No sim leg was added** for either new path. `OFFSITE_PATH` and the wake
  step were exercised from a throwaway harness that is not in the repo; the
  committed sim still drives `OFFSITE_UNC`. Recorded as OI-11(a).
- **shellcheck is not installed** on this machine (Windows) or in the WSL
  Ubuntu — not run, not claimed. `bash -n` is all the static shell checking
  that happened.
- The V1 stack sim was already up from a previous session and was **reused**,
  not rebuilt from scratch; `sim/run-sim.sh` itself was not re-run.

**For the Owner / next gate**

- **OI-13** (new): the wake feature is inert until the real MAC lands in
  `/etc/homehub-backup/backup.env`, and it needs two Windows-side settings.
- **OI-11(b)**: set `OFFSITE_PATH` once IceDrive is running on-box; the run
  fails if that directory is missing, deliberately.
- **A7** records the mechanics decided unattended (probe port, default timeout,
  re-send interval, must-already-exist rule, the two optional knobs).
- Left alone on purpose: OI-12 (wall panel) — nothing built, no wall site or
  image lane created.

---

### DRIVER — G1 — Round 1 — 2026-07-29 (SR-010 enforcement — personal name swept out of every tracked file)

The Owner sanctioned (2026-07-29) replacing the personal first name in tracked
files with the generic role term, closing the last gap against **SR-010** /
**SN-007** ("no ... personal-name in any tracked file"). Name tokens only —
no decision IDs, dates, semantics, or surrounding wording were changed, and no
git history was rewritten.

- **21 tracked files** swept (docs, requirements CSVs, READMEs, `AGENTS.md`,
  `REMOTE_MANAGEMENT.md`, compose/sim YAML, `Caddyfile`, `.env.example`,
  autoinstall + backup + remote-ui shell, `vmtest/*`) — plus this log, which the
  Owner explicitly sanctioned despite its append-only rule.
- Mapping used: possessive → "the Owner's"; sentence subject → "the Owner";
  table-cell/label/attribution forms → "Owner"/"Owner:"; the all-caps banner
  became **`OWNER MANUAL STEP`** (same 5-character name token, so the
  `.env.example` ASCII box border needed no re-drawing); hyphenated compounds
  → `Owner-gated`/`Owner-ratified`/`Owner-consented`.
- **Two conflicts, handled deliberately:**
  - The `.env.example` OAuth box had one comment line that no longer fit the
    76-column border once the name expanded; that sentence was re-wrapped across
    its existing two lines. Box width and content are otherwise identical.
  - The 2026-07-03 WI-10.13 WSL2 entry recorded a real *Linux login name* (a
    literal system identifier, not prose). Renaming it to a role phrase would
    have falsified the record, so it is now the placeholder `` `<owner>` ``
    with an inline note that the real login name is redacted per SR-010. The
    account itself is unchanged on the dev PC.
- The pseudonym `diytechy`, the `AuroLeap` org, hostnames (`mini-serv`,
  `homehub`), and all `Co-Authored-By` trailers were left untouched.

**RAN FOR REAL**

- A case-insensitive `git grep` for the name over tracked files: **102 hits
  across 21 files → 0 hits**.
- CSV structure re-parsed with Python `csv` before/after: `interfaces.csv`
  6 rows and `system-requirements.csv` 16 rows, **every row's column count
  identical** — the edits touched cell text only, never a delimiter or quote.
- `python scripts/check.py` — **G1 PASS** (config-validate: 77 compose vars vs
  `.env.example`, 8 Caddyfile vars, all bind-mounts/autoinstall files present,
  YAML parses; registry-integrity `SN=12 SR=15 orphans=21 integrity=0`;
  doc-navigability 11 docs / 48 links / 0 broken). Identical to the previous
  round's numbers, i.e. the rename moved no requirement and broke no link.

**NOT run (honest gap)**

- No sim, no container, no box. This pass is text-only; nothing executable
  changed behavior, so only the static gate was exercised.
- Commit metadata was **not** rewritten. Author identity was already `diytechy`
  (verified via `git config user.name`), and history rewriting was out of scope.

---

### DRIVER — G1 — Round 1 — 2026-07-29 (three Owner rulings: INGEST step, archive EXCLUSIONS, OFFSITE retired)

Implemented the Owner's three rulings of 2026-07-29 in `stack/backup/`. Two are
new pipeline capability; the third **removes** a design half-built earlier the
same day. No new decisions were taken — the mechanics decided unattended are
listed as **A8** and the things only the Owner can supply are **OI-14**.

**What was built**

- **INGEST (step 1b) — ratified.** A new `INGEST_SOURCES` table
  (`name=//host/share -> /abs/library/dest`, one per line, same hand-edited style
  as `BACKUP_SOURCES`) mirror-syncs each network source **into the library tree**
  BEFORE any archiving: wake (the existing `wake_and_wait`, unchanged) → cifs
  mount ro → `rsync -a --delete` → unmount. The library folder is then covered by
  an ordinary `path:` `BACKUP_SOURCES` entry, so one archive flow serves ingested
  and native folders identically. **Mirror semantics are documented loudly in
  three places** (`.example`, README, the run log itself): source deletions
  PROPAGATE, and history lives in the dated run snapshots, not the library.
  Failures are loud per OI-9 — a refused mount, an rsync error, a malformed line
  and a missing library parent each post `ok=false` and exit nonzero. One guard
  exists purely because `--delete` is irreversible: a share that **mounts but
  holds no files** while the library copy does **refuses to mirror**
  (`INGEST_ALLOW_EMPTY=true` overrides). The old pattern (a `//host/share`
  straight in `BACKUP_SOURCES`) still works; the `.example` now presents the
  ingest pair as the intended one.
- **EXCLUSIONS (step 2) — new requirement.** `BACKUP_EXCLUDE` (global,
  space-separated globs, ships as the Owner's `"*.bak"`) plus per-set
  `name.exclude=PATTERN …` lines **inside the one `BACKUP_SOURCES` table** — one
  table, one place to look. Patterns go to `rsync` at pull time (so the copy is
  never made) and to `tar` (so the archive cannot contain them). **Nothing is
  excluded silently:** the run logs the effective pattern list per set, logs each
  path the patterns actually hid (first five inline, all of them in a new
  `<set>.excluded.log` next to the archive — rsync's own `--debug=FILTER`
  decisions), records the patterns in a new MANIFEST `excludes` column and in
  `RUN.json`, and `restore.sh` states plainly that such a set is a **FILTERED
  copy** of its source. A `name.exclude=` line naming a set that does not exist
  **fails the run** rather than quietly filtering nothing.
- **OFFSITE (step 5) — RETIRED from the target state.** The Owner's corrected
  model: the IceDrive client is pointed **directly at library paths in its own
  GUI**, the service stages nothing. `backup.env.example` now documents
  `OFFSITE_ENABLED=false` as the target state with the corrected model spelled
  out and both target knobs demoted to commented legacy; step 5's code is
  unchanged and still works. Docs reworded: backup README (step-5 table row plus
  a rewritten "Offsite — retired from the target state" section), architecture
  (topology diagram now shows *backup → library → IceDrive client → cloud*, and
  the backup bullet says there is no offsite step), REMOTE_MANAGEMENT (Mini-serv
  is ingested, not staged-for), remote-ui README (the sync pairs the Owner
  creates ARE the offsite configuration).
- **New sim leg `sim/mini-serv-sim/run-ingest-sim.sh`** — the ingest/exclusion
  regression net, over the REAL cifs path (only the NagLight feed is mocked, so
  it needs no homehub-sim stack). The compose file gains one fixture: the
  intentionally always-empty share `//mini-serv/empty`, needed to prove an empty
  share cannot mirror-delete a good library copy.

**RAN FOR REAL (WSL2 Ubuntu + docker-ce, Windows dev PC)**

- `python scripts/check.py` — **G1 PASS** (config-validate 77 compose vars + 8
  Caddyfile vars, bind-mounts/autoinstall files present, YAML parses;
  registry-integrity `SN=12 SR=15 orphans=21 integrity=0`; doc-navigability
  11 docs / 48 links / **0 broken**). Same numbers as the previous round.
- `bash -n` clean on all four `stack/backup` scripts **and** all four sim legs
  (bash 5.2.21, rsync 3.2.7, GNU tar 1.35).
- **`sim/mini-serv-sim/run-ingest-sim.sh` — INGEST LEG: PASS, 28/28 checks**,
  first run of the new leg against the real service: (a) the mirror of
  `//mini-serv/minecraft` into `/srv/library/NonDocs/MiniServ` created the leaf,
  came out **byte-identical to the live share** (`diff -r`), was archived by the
  ordinary `path:` flow and **restored byte-equal**, and is recorded in
  `RUN.json`; (b) a planted library-only file **and** folder were **deleted** by
  the next mirror — the `--delete` contract asserted, not trusted; (c) `*.bak`
  globally + `docs.exclude=Downloads` kept 3 paths out of both the archive and
  `docs.files.tsv` while the keepers stayed, the **source still holds** the
  excluded files, both pattern lists appear in the log, every excluded path is
  named in the log and in `docs.excluded.log`, the MANIFEST `excludes` column
  carries them, a set with no per-set line still got the global pattern, and
  `restore.sh` flagged the FILTERED copy; (d) the empty-share refusal fired
  (library survived at 13 files, `ok=false` posted) and `INGEST_ALLOW_EMPTY=true`
  then cleared it as instructed; (e) three loud config failures (malformed
  ingest line, exclude line for an unknown set, missing library parent) each
  died nonzero **and posted `ok=false`**.
- **Regression, all re-run against the changed service:**
  `run-backup-sim.sh` — **PASS** (full cycle over the Samba fixtures, legacy
  `OFFSITE_UNC` push landed 5 files, NagLight round-trip visible in
  `/api/today`, restore drill byte-equal including the post-loss reconstruct);
  `run-volume-sim.sh` — **PASS (a–d)**; `run-drivepower-sim.sh` — **PASS (a–d)**.
  So the loop restructure (the sources table is now parsed in a pre-pass) and the
  new MANIFEST column broke nothing.
- **A throwaway WSL harness** (scratch, not committed) drove the paths the sim
  cannot reach cheaply: **glob safety** — a decoy `*.bak` in the working
  directory does NOT hijack the pattern list (patterns are split with `read -a`,
  never pathname-expanded); `--dry-run` still writes no archive and no library
  change; the `MANIFEST` / `RUN.json` / `restore.sh` fields verified by eye on a
  plain `.tar` set; the exclude-unknown-set and malformed-ingest `die`s each
  posted `ok=false` through a mock `curl`.
- **Checked the bash semantics the new code leans on** rather than assuming them:
  with `errtrace`, the ERR trap does **not** fire for a failing command
  substitution whose status is tested (`x="$(f)" || die`) but **does** for a bare
  assignment — which is why `ingest_parse` may return 1 while
  `exclude_line_name` never does. `rsync --debug=FILTER`'s exact output
  (`[sender] hiding file X because of pattern Y`) was probed before being made
  the visibility mechanism.

**NOT run (honest gap)**

- **No real box, no real share.** Every ingest test used the sim's Samba
  container as the network source; the sleeping Windows box was never woken and
  its share was never mirrored. Real-share behaviour (SMB quirks, permissions,
  file names Linux dislikes) is V3/hardware.
- **Library-scale data was never involved.** Fixtures are kilobytes, so mirror
  duration, the effect of excluding a genuinely very-large folder, and the
  interaction with the drive spin-down policy at that size are burn-in checks.
- **`zstd` is not installed on the WSL host**, so the throwaway harness ran the
  plain-`.tar` path only; the `.tar.zst` path was exercised inside the sim runner
  container (which has zstd) by the ingest + backup legs.
- **shellcheck is still not installed** on this machine or in the WSL Ubuntu —
  not run, not claimed. `bash -n` is the only static shell checking done.
- The V1 stack sim was **already up from a previous session and reused**;
  `sim/run-sim.sh` was not re-run.
- **The offsite target state is documented, not demonstrated.** Nobody pointed an
  IceDrive client at a library path — that is Owner-side GUI work this repo
  cannot exercise (SR-015).
- **No requirement-registry rows were added or edited** (`SN=12 SR=15`
  unchanged), following the precedent of this morning's wake/offsite change.
  See the two stale wordings flagged below.

**For the Owner / next gate**

- **OI-14** (new): ingest + exclusions are inert until the real share, the real
  library destination and the real "very large folder" names land in
  `/etc/homehub-backup/backup.env`; the authoritative paths are in
  `Personal\deploy\storage-map.md`, and the mirror deletes whatever the share
  deletes.
- **OI-11 rewritten** for the corrected model; the `OFFSITE_PATH` build half is
  recorded as **moot** (harmless legacy, no sim leg owed).
- **A8** records the mechanics decided unattended (the arrow grammar, the
  leaf-created/parent-fatal rule, `INGEST_ALLOW_EMPTY`, exclusions living in the
  sources table and the `.exclude` name restriction, the additive MANIFEST
  column).
- **Two now-stale requirement wordings, left for the Owner** because editing
  ratified rows is his call: SR-013's text still says sets flow "through
  archive/hash/manifest/retention/**offsite**/report", and SR-015's acceptance
  criteria still contains the parenthetical "backup.sh offsite is cifs-only
  today - a local-path offsite target is a separate ratified change". Both
  describe a step that is now retired.
- **A monitoring hole worth a decision:** with no offsite step, the backup's
  never-silent-green report **cannot** see a stale cloud copy — after a reboot
  IceDrive is down until someone opens a session, and the backup will still post
  `ok=true`. If that should be watched it needs its own check (something that
  looks at the client/cloud and feeds NagLight); that is a new decision, not
  built here.
- **A privacy-posture change worth noticing:** the Finance-Auditor §3 firewall
  used to be enforceable by config review (`finance` must not appear in
  `OFFSITE_SETS` — a line in a file). In the corrected model the cloud selection
  lives in the IceDrive **GUI**, so nothing in this repo can prove raw finance
  data is not being synced. The `.example`, the backup README and the remote-ui
  README now say so in words, which is all a config repo can do.
- Left alone on purpose: OI-12 (wall panel) — nothing built.

### DRIVER — G1 — Round 1 — 2026-07-29 (SR-013/SR-015 wording currency — the Owner's sanction)

The two stale ratified wordings flagged above are now current per the Owner's 2026-07-29 sanction (SR-013 flow drops offsite → README step 5 legacy; SR-015's deferred offsite question marked settled; SN-010/SN-012 sentences matched) — wording only, nothing ran; `scripts/check.py` PASS.

### DRIVER — G1 — Round 1 — 2026-07-29 (OI-12 RATIFIED: the wall-panel image lane, built)

The Owner ratified OI-12 with a specific, stronger variant than either option on
the table: the kiosk auth site sits on a **LAN-bound alternate port the router
never forwards, PLUS the `/32` allow-list** — belt and braces, not either alone.
Built here. New decisions taken unattended are recorded as **A9**; the things only
the Owner can supply are folded into OI-12 above, and one genuine cross-repo
decision surfaced and was deliberately NOT taken (**OI-15**).

**What was built**

- **The kiosk site (SR-016)** — `{$WALL_HOST}:{$WALL_PORT}` in
  `stack/caddy/Caddyfile`. Four guards, each independent and each documented as
  load-bearing in the block's own banner: the injected `X-Forwarded-User`
  **replaces** any client-supplied identity; `remote_ip {$PANEL_IP}/32` (one
  address, never the LAN CIDR — guest Wi-Fi, IoT gear and an inbound-facing
  Minecraft server share that network); the port published bound to `{$LAN_IP}`
  with the router forwarding only :80/:443; and `respond 403` for everything else.
  Inside, it serves the shell's static build from `stack/wall-shell/` **and**
  proxies `/api/*` to `tracker:8787` — the same origin, because NagLight sends no
  CORS headers (OfficeWallNaglight needs doc §3.2). `/music` ships as a commented
  Navidrome stub. **The Caddyfile deliberately carries no `bind` directive:**
  `LAN_IP` is not an address the container owns, so `bind` there would fail to
  listen at all — the LAN-binding is the compose publish.
- **The wall autoinstall variant (SR-017)** — `stack/autoinstall/wall/`: graphical
  target (`cage` + tty1 autologin, no display manager), Wi-Fi-only netplan
  rendered from placeholders, **no Docker and no Cockpit**, and every §3 quirk
  expressed as config: logind lid ignore (1), `iio-sensor-proxy` masked (2), a
  udev rule generated from `WALL_DISABLE_INPUT` (3), NM powersave-off +
  `cloned-mac-address=permanent` (5). D-W4 is `SLEEP_MODE=suspend|backlight`
  sharing ONE schedule, with the RTC alarm armed **before** suspending and a
  per-boot unit re-enabling the ACPI `XHC` + USB device wakeup flags that do not
  persist. Quirks 4b and 6 are not config at all (mount geometry, vent clearance,
  a measured thermal baseline) and say so.
- **The spine** — SN-013 + 8 edge rows, SR-016 (Demonstration) and SR-017
  (Inspection), IF-005 `Planned` → **`Partial`** with the four remaining gaps
  named. `validate_config.py` gained check 5: every namespaced knob a wall script
  reads must be declared in `wall.env.example` (compose cannot see an
  env-file-configured image), plus the wall variant's files and YAML.
- **Docs** — architecture ("two images from one pipeline", the third auth model),
  `stack/README` §10 (enable steps, the guards as a table, the cert analysis),
  `REMOTE_MANAGEMENT` (the panel is reimage-not-repair), and
  `WALL-BURN-IN.md` for everything only the hardware can settle.

**RAN FOR REAL (WSL2 Ubuntu + docker-ce, Windows dev PC)**

- **`sim/validate-sim.sh` — V1 GATE PASS, all 8 checks**, including the two new
  wall legs. The stack was brought up fresh (`sim/run-sim.sh`) because the overlay
  now adds a network. The wall legs reach the site from two source addresses out
  of ONE container: `simclient` sits on both the default network and a new
  sim-only `simlan` (fixed subnet, static leases), so dialling Caddy's `simlan`
  address arrives as `PANEL_IP` and dialling its default-network address does not.
  Asserted: `forged X-Forwarded-User=sim-user-attacker-9999 from the panel /32
  reached the tracker as sim-user-wallpanel-0003`; panel `/api/today` → 200;
  panel `/` → the shell fixture and `/config.json` → 200; and both `/` and
  `/api/today` from the non-panel route → **403 with Caddy's own body** (which is
  what distinguishes an edge refusal from the tracker's own no-identity 403).
- **THE SIM CAUGHT A REAL BUG ON ITS FIRST RUN, and it was the important one.**
  The design brief's literal "strip then inject" — `header_up -X-Forwarded-User`
  followed by a set of the same field — **does not work in Caddy**: header ops are
  applied in a fixed order (add → set → **delete** → replace) regardless of the
  order written, so the delete erased the injected identity, the tracker saw no
  identity, and every panel request 403'd. Verified by reading the adapted JSON
  (`caddy adapt` showed both a `delete` and a `set` of the same field) rather than
  guessed. The site failed **closed**, which is the right direction — but it would
  never have worked, and on hardware this would have looked like a panel fault.
  Fixed by relying on set-replaces-all (asserted with the forged header, not
  trusted) and a DO-NOT-ADD banner so it cannot come back.
- **`caddy validate`** on the real (not sim) Caddyfile with placeholder env:
  **Valid configuration** — and it confirmed automatic HTTPS treats the
  alternate-port site as a normal HTTPS server (`srv1`, redirects enabled).
- **A throwaway container harness** (scratch, not committed) ran
  `wall-firstboot.sh` **twice** against a bare `ubuntu:24.04` root with stub
  `systemctl`/`udevadm`/`netplan` on PATH — the mock-shim pattern this repo
  already uses for `hdparm`/`docker`. **27 assertions PASS**, and it caught two
  real bugs, both fixed: `printf '%s'` without a trailing newline made `read` skip
  the **last** piped device name (the touchpad would have kept working), and a
  redirect into a missing `/etc/udev/rules.d` / `/etc/netplan` aborted the whole
  script on a minimal root. It also proves the honest-degradation paths: the
  shipped placeholder template exits 0 while announcing that quirk 3 and netplan
  were skipped, and writes no udev rule from nothing.
- **The new config-validate check was negative-tested**: an undeclared
  `${WALL_BOGUS_KNOB}` added to a wall script makes the run FAIL, so check 5 is
  not vacuous.
- **`python scripts/check.py` — G1 PASS**: config-validate (81 compose vars, 12
  Caddyfile vars, 10 wall knobs, both `user-data` files parse), registry-integrity
  `SN=13 SR=17 orphans=24 integrity=0`, doc-navigability 11 docs / 48 links /
  **0 broken**.
- **The certificate claim was verified against Caddy's docs AND source, not
  assumed** (the ask was explicit about honesty here): automatic HTTPS activates on
  the *hostname*, not the port, so a `:8443` site still gets a publicly-trusted
  cert; ACME CAs **never** contact non-standard ports (HTTP-01 is always :80,
  TLS-ALPN-01 always :443); and Caddy's ACME challenge handler runs in every HTTP
  server ahead of route matching, dispatching on the requested hostname
  process-wide — so the existing :80 listener answers for a name that has no
  port-80 site block. **That last point is clear in Caddy's source but is NOT
  stated in its documentation**, and `stack/README` §10 says exactly that rather
  than presenting it as documented. The dependency it creates (inbound :80 must
  stay forwarded, or renewal for this name breaks) is written down, with DNS-01
  named as the fallback since a Cloudflare token already exists for DDNS.

**NOT run (honest gap)**

- **No panel, no hardware, nothing physical.** `cage` has never been started, no
  Wi-Fi has been associated, no suspend or resume has happened, no lid has been
  folded, no udev rule has been applied to a real input device, no backlight has
  been dimmed and no RTC alarm has woken anything. Every §3 quirk fix is
  *asserted as written config*, never as observed behaviour.
- **The off-LAN 403 is still an assumption.** Every wall-site probe came from
  inside a docker network. Nobody has curled the panel's hostname from cellular,
  and nobody has confirmed the router's forward list. This is the single test the
  design brief itself called out as load-bearing, and it remains owed.
- **Docker's source-IP preservation is unproven on the real box.** The `/32` match
  relies on the panel's real address reaching Caddy through the port publish. In
  the sim the probes are on the same bridge, so this is untested; if it fails, the
  site 403s the panel (fails closed, not open).
- **The wall image harness is a throwaway, not a committed sim leg.** A graphical
  kiosk session cannot be exercised in a compose sim, so unlike the wall *site*
  there is no permanent regression net for the wall *image* — the same honesty
  position as SR-015's opt-in RDP layer.
- **IF-005 has no artifact**, so `WALL_APP_CMD` points at a placeholder and the
  kiosk shows an explicit "not installed" screen. Nothing has ever run under
  `cage`, including a stand-in.
- **The payload-bake path was not extended to the wall image.** The wall variant is
  not yet wired into `vmtest/export-images.sh` or the ISO builders; the panel needs
  no container images, but which files a wall USB carries has not been implemented
  or tested.
- **shellcheck is still not installed** on this machine or in the WSL Ubuntu — not
  run, not claimed. `bash -n` (clean on all four wall scripts) is the only static
  shell checking done.

**For the Owner / next gate**

- **OI-12 rewritten as RATIFIED + BUILT**, with what still needs him listed there:
  the four T1/T3 values, the panel's DHCP reservation on its hardware MAC, the
  `EXTRA_SUBDOMAINS` label, the FieldSchema registration, and the burn-in.
- **OI-15 (new) — `/media/*` has no origin.** A genuine decision, left untaken:
  the shell's manifest paths must be same-origin, but the media is on the panel's
  cache while the origin is a site on the AWOW. Marked `TODO(OI-15)` in the
  Caddyfile with a commented stub. The likely answer is panel-side (Electron
  intercepting `/media/*`), which belongs to OfficeWallNaglight.
- **Two cross-repo consequences of registering the wall templates** in
  `Personal\homelab\deploy\FieldSchema.psd1` (Personal is not edited from here —
  reported instead): (i) `WALL_HOST`, `PANEL_IP` and `PANEL_USER_SUB` now appear in
  the **AWOW's** `.env.example` too, because the kiosk site runs on the AWOW — so
  they must move from `config.wall.psd1` to `config.common.psd1`, or the `awow`
  image's completeness rule will refuse to emit; (ii) the wall `user-data` needs
  the SSID and PSK substituted, but the emitter's `userdata` format substitutes
  only the password hash and the SSH key — the placeholders were named
  `REPLACE_WITH_WIFI_SSID` / `REPLACE_WITH_WIFI_PSK` so a generic
  `REPLACE_WITH_<KNOB>` pass is a small change rather than a new format.
- **A note on the panel-down alert:** it is required, not optional (quirk 4b), and
  it is **not** in this repo — it is an Uptime-Kuma push monitor plus a Kuma→ntfy
  notifier, configured in Kuma's UI, and its maintenance window must be taught the
  sleep window or it will cry wolf every single night.
- **`WALL_PORT` is a new T0 knob** (public default, `8443`) and needs no
  FieldSchema entry; unlisted knobs are T0 by that schema's own rule.
- Left alone on purpose: the ISO/payload wiring for the wall image, and OI-15.

### DRIVER — G1 — Round 1 — 2026-07-29 (OI-15 RESOLVED by the Owner: the panel PULLS its media — built)

The Owner ruled OI-15 the same evening it was opened, and the ruling is narrower
and better than the "which origin serves `/media/*`" framing it answered:

> Panel media is an AWOW network share; **the panel PULLS** — once after boot and
> on demand via a dedicated SSH-invocable command — with **MIRROR semantics**
> (`--delete`: content removed from the LAN source disappears from the panel
> cache). `/media/*` is then served panel-locally by the shell's Electron host.

So there is no `/media` route to design on the AWOW at all, and this repo owes the
**pull**. Built here; the shell-side half (Electron mapping `/media/*` onto the
cache) stays OfficeWallNaglight's.

**What was built**

- **`stack/autoinstall/wall/wall-sync.sh`** — mount `MEDIA_SHARE_UNC` read-only
  over cifs (same option shape and credentials-file-first precedence as
  `stack/backup/common.sh`'s `mount_cifs`), `rsync -a --delete` **only** the
  share's `Music/` and `FrameVideos/` subtrees into
  `WALL_MEDIA_CACHE/{music,frame}` (default `/var/cache/wall-media`), unmount via
  an EXIT trap so no failure path leaves a mount behind. The subtree map is a
  constant, not a knob: widening what the panel pulls is a decision, and a knob
  would let a typo widen it silently onto a 256 GB disk.
- **The guards are the ingest step's lessons, transplanted** (that code learned
  them the expensive way): an **empty** source subtree, and separately an
  **absent** one, does not get to mirror-delete a populated cache — the run
  refuses, names the override, and leaves the cache untouched;
  `WALL_SYNC_ALLOW_EMPTY=true` is the deliberate escape hatch and mirrors the
  emptiness *through the same rsync* (an empty temp dir as the source) rather than
  through a second deletion mechanism. Every failure is fatal and nonzero: the
  panel has no NagLight feed of its own, so "loud" means a failed unit plus
  journal lines.
- **`wall-media-manifest.py`** — the sync's post-step, and the reason the
  generator is Python rather than a bash JSON writer: the one thing that must not
  be got wrong is string escaping, and a real library is full of quotes,
  ampersands, `#` and non-ASCII. It emits `music/index.json` in
  `LocalLibraryProvider`'s documented shape (albums from folders, `Artist/Album`
  giving artist + album, `cover.jpg` as art, a leading track number parsed off the
  title, root-level files as the documented **flat** `tracks` form) and
  `frame/playlist.json` as `[{url,title}]`.
- **THE ASYMMETRY THAT WOULD HAVE BITTEN**, found by reading both consumers rather
  than assuming they matched: music `path` values must be **RAW** (`local.js`'s
  `joinUrl()` percent-encodes every segment itself, so encoding here would
  double-encode every space), while frame `url` values must be **ALREADY ENCODED**
  (`frame.js` assigns them straight to `video.src`). Both are asserted, in both
  directions.
- `ensure_ascii=True` on both manifests, so a non-ASCII filename ships as `\uXXXX`
  and cannot depend on the Electron host guessing a charset; a filename whose
  bytes are not valid UTF-8 (a Windows library will produce one eventually) is
  **skipped and counted** rather than emitted as a lone surrogate that would break
  the whole manifest for one bad name.
- **`wall-sync.service`** — `Type=oneshot`, `After=network-online.target` (the
  panel is Wi-Fi-only, so that is load-bearing, not decorative) and
  `After=wall-firstboot.service`, `WantedBy=multi-user.target`,
  `TimeoutStartSec=3600` for the first full copy over 802.11,
  `IOSchedulingClass=idle` so a sync cannot make the wall stutter. **No `.timer`**
  — the ruling is boot + on demand, and re-`start`ing a oneshot IS the on-demand
  path, so `sudo systemctl start wall-sync.service` is the whole documented
  interface.
- **The Caddyfile `TODO(OI-15)` stub is gone**, replaced by the one line the ruling
  makes true: `/media/*` is panel-local, this site serves no `/media` route.
- **Knobs + coverage**: `MEDIA_SHARE_UNC`, `WALL_MEDIA_CACHE`,
  `WALL_SYNC_ALLOW_EMPTY`, `MEDIA_CIFS_CREDENTIALS`/`_USER`/`_PASS`/`_EXTRA` and
  the commented `MEDIA_SOURCE_OVERRIDE` bench hook in `wall.env.example` (10 → 18
  declared wall knobs); `validate_config.py` gained the `MEDIA_` namespace, the
  three new files in its autoinstall-file list, and `wall-sync.sh` as a knob
  consumer. `wall-firstboot.sh` gained a step 8 that creates the cache dirs,
  enables the unit, and reports whether the share is configured; the wall
  `user-data` grew `cifs-utils`, `rsync` and (explicitly) `python3`, plus the
  late-commands that install the three files.

**RAN FOR REAL (WSL2 Ubuntu + node on the Windows host, 2026-07-29)**

- **A THROWAWAY harness — 67 assertions PASS, and it caught two real bugs.** The
  harness drives the REAL `wall-sync.sh` through its `MEDIA_SOURCE_OVERRIDE` bench
  hook against a fixture library with deliberately nasty filenames (apostrophe,
  double quotes, `&`, `#`, non-ASCII, spaces), under `env -i` so nothing ambient
  props it up. Covered: first sync into an empty cache; **`--delete` propagation**
  (a file removed from the source disappears from the cache AND from
  `index.json`); an idempotent no-op re-run; the **empty-subtree refusal** and the
  **missing-subtree refusal**, each with the cache asserted file-count-unchanged
  afterwards; `WALL_SYNC_ALLOW_EMPTY=true` clearing the cache and still emitting
  valid JSON (`[]`); the non-UTF-8 filename skipped-and-counted; five loud config
  guards (unset share, the shipped placeholder, a non-UNC share, a relative cache
  path, a bogus override); and the generator-not-found path failing instead of
  leaving synced media unlisted.
- **THE HARNESS'S FIRST RUN FOUND THE BUG THAT MATTERED**, and it was a
  self-inflicted one: the manifests live *inside* the directories the mirror
  refreshes, so `--delete` removed them on every run and the file counts included
  them — a no-op re-run therefore logged "1 file(s) were DELETED" and the
  cache-clearing message was off by one. Both were fixed properly rather than by
  adjusting the message: the mirror now `--exclude`s the top-level manifest
  (anchored, so a same-named file inside an album is still mirrored) and every
  logged count is a MEDIA count. The side benefit is real — a run that dies before
  the post-step now leaves the last complete manifest in place instead of nothing.
- **The emitted manifest was validated against the REAL CONSUMER, not against my
  reading of it**: `node` importing `normalizeManifest`/`joinUrl` straight out of
  `OfficeWallNaglight/js/music/local.js` (read-only; nothing in that repo was
  touched) — **20 assertions PASS**: 4 tracks / 2 albums / 3 stations with the
  endless shuffle first, `artUrl` and every track URL encoded exactly once
  (`%2520` asserted absent), quotes/`&`/`#`/`Å` round-tripping into playable URLs,
  album+artist inherited by tracks, and the flat form resolving. The frame playlist
  was checked segment-by-segment and by `new URL(...)` resolution back to the real
  filenames.
- **`python3 -m json.tool` on both manifests**, in three states: populated, empty
  (`[]`), and after the non-UTF-8 skip. All valid.
- **`wall-firstboot.sh` was re-checked for real after gaining step 8** — a second
  throwaway container harness (bare `ubuntu:24.04`, stub
  `systemctl`/`udevadm`/`netplan`), **12 assertions PASS**: the shipped placeholder
  template still exits 0, the cache dirs are created, the unit is enabled, the
  placeholder share is warned about *with its consequence stated*, a filled share
  is reported instead, and a missing `wall-sync.service` is called out rather than
  silently skipped.
- **`caddy validate` on the real Caddyfile after removing the `TODO(OI-15)` stub**
  (pinned `caddy:2.11.4-alpine`, placeholder env + a valid bcrypt so provisioning
  gets that far): **Valid configuration**, both servers still adapting.
- **`bash -n` clean** on all five wall scripts; `py_compile` clean on the
  generator (it is written to run on Python 3.8+, though the panel has 3.12).
- **The new config-validate coverage was negative-tested**: an undeclared
  `${MEDIA_BOGUS_KNOB}` in `wall-sync.sh` makes the run FAIL with exactly that
  name, so the `MEDIA_` namespace is not vacuous.
- **`python scripts/check.py` — G1 PASS**: config-validate (18 wall knobs, both
  `user-data` files parse), registry-integrity `SN=13 SR=17 orphans=24
  integrity=0`, doc-navigability 11 docs / 48 links / **0 broken**.

**NOT run (honest gap)**

- **No cifs mount was performed by any of this.** Every real run used the bench
  hook, so `mount -t cifs`, the credentials file, `vers=3.0`, and the behaviour of
  a share that vanishes mid-rsync are all untested here. The backup service's
  ingest leg exercises the same `mount_cifs` shape against a real Samba container,
  which is evidence for the *pattern* but not for this script.
- **No panel, no Wi-Fi, no library-scale data.** Fixtures are kilobytes on ext4;
  the first-sync duration over 802.11, the 256 GB disk budget with a real
  `FrameVideos/`, and the interaction with the D-W4 sleep window are hardware.
- **Nothing has ever read these manifests in the shell.** The consumer check ran
  `normalizeManifest` in node, not the Electron host — and the *other half* of the
  ruling (mapping `/media/*` onto the cache) does not exist yet in
  OfficeWallNaglight, so end-to-end playback is unproven by construction.
- **The kiosk user has never read the cache.** `--chmod=D755,F644` is asserted as
  written config, not as an observed `sudo -u panel` read (burn-in §8 has the
  check).
- **Throwaway, not a committed sim leg** — same position as the wall image
  harness: a cifs + Wi-Fi + graphical path cannot be exercised in the compose sim,
  and a leg that only re-ran the bench hook would test the hook, not the pull.
- **shellcheck is still not installed** on this machine or in the WSL Ubuntu — not
  run, not claimed.
- **No requirement-registry rows were added or edited** (`SN=13 SR=17` unchanged),
  following this lane's precedent: the pull is the mechanism of an already-ratified
  need (SR-017 / D-W8 rider / OWN-D5), not a new one. If the Owner wants the media
  pull to carry its own SR row with its own verification method, that is a spine
  edit for him.

**For the Owner / next gate**

- **OI-15 is closed on this side and half-open on the other.** The panel pulls; the
  Electron host must serve `/media/*` from `WALL_MEDIA_CACHE`. That is one line in
  `IF-005`'s contract and it is now the *only* `/media` question left.
- **OI-16 (new) — does a RESUME also sync?** Implemented exactly as ruled (boot +
  on demand), but `SLEEP_MODE=suspend` means the panel may not boot for weeks, so
  in practice the cache is as fresh as the last SSH command. Three options are
  written up in the item; none was taken unattended.
- **A freshly imaged panel FAILS `wall-sync.service` on every boot** until
  `MEDIA_SHARE_UNC` and the credentials are filled in. That is deliberate (a green
  unit on a music-less wall would be a lie), but it is a thing to expect rather
  than to debug — and it means `MEDIA_SHARE_UNC` + a root-only
  `/etc/wall-panel/cifs.creds` join the panel's fill-in list, with the share's
  identity coming from `Personal\deploy\storage-map.md`, not from a guess here.
- **The share must have `Music/` and `FrameVideos/` AT ITS ROOT.** If the household
  library's layout puts them somewhere else, that is a one-line change (a subpath
  knob, or a different share) — say which, rather than letting the sync refuse.
- **FieldSchema consequence:** `MEDIA_SHARE_UNC` is a T1-ish value and the share
  password is T3. If the wall templates are registered as planned, the password
  belongs in the secret store, not in `wall.env` — the script prefers a credentials
  FILE for exactly that reason and warns when it falls back to inline.
- **A disk-space question nobody has answered:** mirroring the whole `Music/` +
  `FrameVideos/` tree onto a 256 GB panel disk is fine for music and possibly not
  fine for video. If `FrameVideos/` is large, the honest fix is a curated subfolder
  on the share rather than a filter here — that is the Owner's call about what the
  wall should show.

### DRIVER — G1 — Round 1 — 2026-07-29 (OI-16 RESOLVED by the Owner: wall-sync also fires on RESUME — built)

The Owner decided OI-16 the same evening it was opened: **option (a)** — the
media sync also fires on resume from suspend. The reasoning the item stated
holds: with `SLEEP_MODE=suspend` the panel suspends nightly and resumes without
booting, so "once after boot" was, in practice, "approximately never". Now every
wake behaves like a boot for freshness. Zero new knobs, one new file.

**What was built**

- **`stack/autoinstall/wall/wall-sync-resume.service`** — the standard systemd
  resume hook: `WantedBy=suspend.target` pulls it into the suspend transaction,
  `After=suspend.target` orders it after that target is *reached* — which only
  happens when `systemd-suspend.service` completes, i.e. **at wake**. So it
  starts on resume, never at suspend time; `Type=oneshot` with no
  `RemainAfterExit` drops it back to inactive so every later cycle fires again.
- **`ExecStart=/usr/bin/systemctl start --no-block wall-sync.service`** — and
  `--no-block` is load-bearing, not a flourish: the hook runs *inside* the
  resume transaction, a plain `start` waits for the started job, and a first
  full mirror is allowed up to an hour (`TimeoutStartSec=3600`). Blocking would
  hold the resume transaction open for the whole sync; `--no-block` only
  enqueues the job and returns, so the hook finishes in milliseconds, the wake
  is never perceptibly delayed, and the sync runs detached as its own job with
  its own journal, timeout and pass/fail (`journalctl -u wall-sync`). Same
  unit, same code path as boot and on-demand — no second entry point to drift.
- **The Wi-Fi race, handled where ordering cannot reach:**
  `network-online.target` was reached at boot and is NOT re-evaluated on
  resume, while re-association takes a few seconds after wake — so no unit
  ordering can cover it. `wall-sync.sh` therefore gained a short **bounded**
  wait before the real mount: `nm-online -q --timeout=30` (network-manager is
  already in the wall image), **non-fatal in every branch** — success proceeds
  silently, timeout warns and proceeds, a missing `nm-online` warns and
  proceeds. The cifs mount remains the loud arbiter, and the documented retry
  is the on-demand command. Boot/on-demand runs lose nothing (`nm-online`
  returns immediately when the network is already up); the bench-hook path
  skips the wait entirely (it touches no network).
- **Only `suspend.target`** — the panel's one sleep path is `systemctl suspend`
  from `wall-sleep.sh`; no code path can reach hibernate/hybrid-sleep, so those
  targets are deliberately not listed rather than cargo-culted in.
- **Wiring, consistent with wall-sync's own install:** the wall `user-data`
  late-commands cp the unit and `systemctl enable` it (enabling is what plants
  the `suspend.target.wants` symlink — an installed-but-disabled hook never
  fires); `wall-firstboot.sh` step 8 re-enables it idempotently and warns, with
  the consequence stated, if the file is missing. `validate_config.py`'s
  autoinstall-file list gained the one new file; no knobs were added so the
  knob coverage is unchanged (18 declared, still all accounted for).
- **Docs:** `wall/README.md` (table row + the "no timer" bullet rewritten to
  "a resume DOES sync", with the `--no-block` and Wi-Fi reasoning);
  `WALL-BURN-IN.md` §8's "decide OI-16" checkbox replaced by the two hardware
  proofs (an `rtcwake` suspend→wake with `journalctl -u wall-sync-resume -b`
  showing the sync fired after the wake, and the same via the real
  `wall-sleep.sh start` nightly path); the OI-16 item above → RESOLVED.

**RAN FOR REAL (this machine + WSL, 2026-07-29)**

- **`systemd-analyze verify` on the new unit** (systemd 255 in WSL): exit 0.
  (The one warning — "marked executable" — is an artifact of the drvfs copy,
  not of the unit.)
- **`bash -n`** clean on both edited scripts (`wall-sync.sh`,
  `wall-firstboot.sh`).
- **The edited `wall-sync.sh` re-run end-to-end in WSL Ubuntu** through the
  `MEDIA_SOURCE_OVERRIDE` bench hook under `env -i`: mirror + both manifests
  regenerated, `python3 -m json.tool` valid on both, exit 0.
- **All three nm-online branches exercised for real** with a stub on `PATH`:
  called with exactly `-q --timeout=30`; exit-0 proceeds silently; exit-1
  prints the "network still not up after 30s" warning and proceeds; absent
  binary prints the skip warning. In each case the run still died LOUDLY at the
  refused cifs mount (exit 1, cache untouched) — the arbiter is unchanged.
- **`python scripts/check.py` — G1 PASS**: config-validate (the new file
  present, 18 wall knobs, both `user-data` files still parse as YAML),
  registry-integrity `SN=13 SR=17 orphans=24 integrity=0`, doc-navigability
  11 docs / 48 links / 0 broken.

**NOT run (honest gap)**

- **No real suspend/resume fired this hook.** WSL cannot suspend, so
  `WantedBy=suspend.target` actually triggering at wake — the entire point —
  is asserted from the documented systemd semantics, not observed. Burn-in §8
  now carries the two-step hardware proof.
- **No real Wi-Fi re-association raced the sync.** The 30 s `nm-online` cap is
  a judgment, not a measurement; if this radio routinely takes longer, burn-in
  says to report it rather than tune silently.
- **`--no-block`'s "wake never delayed" claim** is by construction (enqueue and
  return), verified only as unit semantics — the perceptibility check is on the
  burn-in list, on the panel.
- **shellcheck is still not installed** on this machine or in the WSL Ubuntu —
  not run, not claimed.

**For the Owner / next gate**

- OI-16 is **closed**: boot + resume + on demand, still no timer. The only
  remainders are hardware (burn-in §8): see the hook fire after a real
  suspend→wake, and once from the real nightly `wall-sleep.sh` path.
- Freshness statement, updated and honest: the cache is now as fresh as the
  **last wake or boot**, whichever is later — plus whatever `sudo systemctl
  start wall-sync.service` was run in between. Music added during the panel's
  awake hours still needs the on-demand command (or waits for tomorrow's
  resume); that is the shape of option (a), stated rather than hidden.

---

### 2026-07-30 — Owner-directed automation sweep (Personal open-items A9) + feed-transport defect fix

Four changes landed this pass, driven by the Owner's ruling that box/LAN-local
configuration should self-configure wherever the trust model allows:

- **`FINANCE_ACTUAL_SYNC_ID` is now optional** (`stack/.env.example`): empty =
  finance-auditor auto-discovers the sole budget file at startup (its own repo
  change, `getBudgets()`/`cloudFileId` verified against the pinned
  `@actual-app/api` 26.7.0 types); several files = fatal naming them.
- **Backup feeder transport fixed** (`stack/backup/common.sh feed_naglight` +
  `backup.env.example`): the shipped example URL pointed at the public
  tracker route, which oauth2-proxy would bounce and whose `X-Forwarded-User`
  it would overwrite — and the tracker is deliberately bridge-only (D2), so a
  host-side curl cannot reach it at all. New `NAGLIGHT_FEED_CONTAINER` knob
  (default `tracker`): the POST runs inside the tracker container via
  `docker exec` + its busybox wget (the same binary its healthcheck proves)
  against its own loopback. Port stays closed. **Sim-untested**: `bash -n`
  clean; the homehub-sim backup lane is the gate.
- **`provision/provision-actual.sh` (NEW) + firstboot step 5b**: sets the
  dev-PC-minted Actual server password on the un-bootstrapped server
  (`POST /account/bootstrap`, checked via `GET /account/needs-bootstrap`
  first; idempotent — an already-bootstrapped server is a logged no-op, with
  mismatch guidance). Runs inside the `actual` container via
  `docker exec node -e` + fetch (image ships no curl/wget, WI-10.14; password
  via exec env, never argv). **HONEST STATE: the endpoint shape is from
  actual-server source reading, NOT yet exercised against the pinned
  `ACTUAL_IMAGE_TAG` in the sim — that sim run is the acceptance gate for
  this script.** `bash -n` clean.
- **`provision/list-tracker-users.sh` (NEW, read-only)**: prints the per-user
  dir names (= Google `sub` values) under the tracker volume so the Owner can
  fill `NAGLIGHT_USER`/`PANEL_USER_SUB` without spelunking. Deliberately NOT
  auto-discovery for `PANEL_USER_SUB` — that is the unauthenticated identity
  the kiosk injects (security-critical per the Caddyfile banner); the ruling
  is the human matches sub → person. `bash -n` clean.

Companion changes in `Personal\homelab\deploy\` (same pass): basic-auth
plaintexts + Technitium/Actual server passwords are now machine-minted
(`GeneratedPassword`), the two Caddy bcrypt hashes derive automatically at
prep time (WSL python3-bcrypt — the `docker run caddy hash-password`
instruction printed here was unrunnable on the dev PC, no docker), and a new
`Show-DeploySecret.ps1` reads one key back for browser prompts.

---

### 2026-08-01 — dozzle + uptime-kuma healthchecks fixed (V3_GATE_HANDOFF §5 item 1)

The last two never-passable healthchecks — the same class of bug the homehub-sim
caught for tracker/oauth2-proxy/technitium/actual in WI-10.14, but in the aux
containers the V1 sim never ran (LAN_IP binds), so nothing had ever executed
them. Both sat permanently red. **Verified against the PINNED images, not
assumed** — `amir20/dozzle:v8.14.12` and `louislam/uptime-kuma:1.23.17`:

- **uptime-kuma** ships **no wget** (it has curl + bash), so
  `CMD-SHELL wget …:3001/` could never run. The image already declares its own
  `HEALTHCHECK CMD-SHELL extra/healthcheck` — a 6.8 MB compiled Go binary at
  `/app/extra/healthcheck` (source `extra/healthcheck.go` sits beside it) that
  GETs `http://127.0.0.1:3001` and exits 0 on 200. Now
  `test: ["CMD", "/app/extra/healthcheck"]` — the image's own probe, absolute
  path so it does not depend on WorkingDir, keeping this stack's 30s cadence
  rather than the image's slower 60s/180s defaults.
- **dozzle** is **distroless** — no `/bin/sh`, so *any* `CMD-SHELL` form is
  unrunnable. Unlike kuma it declares **no HEALTHCHECK of its own**, so one had
  to be supplied: the binary ships a `healthcheck` subcommand ("checks if the
  server is running") that requests its own `/healthcheck` endpoint. Now
  `test: ["CMD", "/dozzle", "healthcheck"]` — exec form is mandatory here.
  Bonus: the subcommand reads the same `DOZZLE_ADDR` config as the server, so it
  follows the listen port instead of hardcoding 8080.

**RAN FOR REAL (WSL, podman 5.3.1 — this dev PC no longer has the Docker Engine
of WI-10.13):** pulled both pinned images; confirmed the missing/​present tooling
above by exec; ran each probe as a **container-runtime healthcheck**
(`podman healthcheck run`) → **both report healthy**, and unhealthy when the
service is down (dozzle's negative case checked with no server running).
`scripts/validate_config.py` → ALL CONFIG CHECKS PASSED. Compose YAML re-parsed
in a throwaway python container (the validator's own YAML step SKIPs here — no
PyYAML on this interpreter): 27 services, both `test:` arrays as intended.

Two test artifacts worth recording so the next person does not re-chase them:
**dozzle exits 1 without a docker socket**, so a socket-less test container is
dead, not unhealthy — the passing run mounts the socket like production does;
and podman's `--health-cmd` does **not** parse a JSON-array string (it stored
`[["CMD",…]]` and tried to exec that literally) — its `CMD …` prefix form is the
CLI equivalent of compose's `test:` list. Neither affects the compose file.

**Also checked, and NOT broken:** `ntfy` v2.25.0 — its `CMD-SHELL wget …
/v1/health` probe is fine (image has wget + sh; ran it, `{"healthy":true}`).
Worth confirming because the 2026-07-29 telemetry ruling made ntfy load-bearing
for the Kuma→ntfy notifier. `caddy` was already proven healthy in the V1 gate.
`ddns` defines no healthcheck by design. That closes the audit of every
healthcheck in the stack.

Still hardware-gated: these two aux images remain otherwise first-validated at
V3 boot / burn-in (image *behaviour* under the real LAN_IP binds is unchanged by
this fix).

---

### DRIVER — G1 — Round 1 — 2026-08-01 (TIER-2 PROMOTED TO THE HOMEHUB DEFAULT SET — SR-012)

Owner's call: `immich` (+`immich-ml`), `jellyfin` and `finance-auditor` become
the **default** opt-in set for the AWOW image, opt-out-able. Nothing is promoted
to core here — `docker-compose.yml` still ships every tier-2 service profiled
OFF and `.env.example` still enables none. The image's set lives in Personal's
`config.homehub.psd1`, so the public default is untouched and disabling one is
still a word removed from `COMPOSE_PROFILES` + `docker compose up -d
--remove-orphans`.

**Three defects the change surfaced — all latent, none introduced by it:**

1. **`MEDIA_ROOT` was ratified and never wired.** D-W8/OI-7b settled it on
   2026-07-29 (`/srv/library/NonDocs/Media`); the generator emitted
   `media-root.txt` saying "nothing consumes this file until then", and the
   materialised `.env` kept the template's `/srv/media` — a directory on the
   119 GB system disk that no fstab line mounts. Every media profile would have
   served an empty library and written photos to the wrong drive. Wired now
   (Personal side); this closes the pathing half of **OI-7(b)**.

2. **Drives mounted AFTER `docker compose up -d`** (firstboot step 5c vs step 4).
   Docker creates a missing bind-mount source itself, on whatever filesystem is
   present at container-start time; the ntfs3 mount then lands on top and
   **shadows** it. The container keeps writing to an invisible directory on the
   system disk while every read through the share sees an empty library — silent
   until the system disk fills. Harmless while nothing bind-mounted inside
   `/srv/library`; live the moment a media profile is on. Mounting moved to a new
   **step 3b**, ahead of compose. The comment block at 5c already argued exactly
   this ordering for Samba — it just had not been applied to containers.

3. **`devices: /dev/dri` cannot live in a shared compose file.** A device entry
   for an absent node fails container *creation*, and compose reports that as a
   failed `up` for the whole run — so the file as written took the entire stack
   down on any box without an iGPU, the V3 gate VM included. New
   `provision-compose-overrides.sh` (**step 3c**) generates
   `docker-compose.override.yml` with the device when `/dev/dri` exists and
   removes it when it does not; compose auto-loads that filename, so a later
   manual `docker compose up -d` over SSH behaves the same. It refuses to touch
   an override lacking its generated header.

**Also in this pass:**
- `JELLYFIN_LIBRARY_DIR` — hand Jellyfin a subtree instead of the whole media
  root (empty = whole root, the generic default). The AWOW gets
  `NonDocs/Media/Movies` per storage-map §3 row 3; music stays Navidrome's and
  the panel's. In-container path is always `/media`.
- `IMMICH_ML_MEM_LIMIT` — the tier-2 banner's "or cap memory", made real. On
  7.6 GiB usable the ML container is the one that can OOM the box; capped, the
  kernel kills it rather than picking a victim from the core stack, and compose
  restarts it. AWOW starts at `2g`.
- `SIM_ENV_OVERRIDES` (`vmtest/lib/common.sh`) — lets the V3 gate boot a real
  image's profile set without changing `.env.example` or staging real secrets.
  Fails the build on a key that is not already a knob, so a typo cannot make the
  gate silently test the default set and report success.
- Docs: tier-2 caveats gained the mount-ordering rule, the case-sensitivity trap
  (`Music` ≠ `music` on ntfs3/ext4 — docker creates an empty sibling, no error),
  and the hardware-passthrough rule.

**Verified (WSL, Docker 29.6.1 — the Engine is back on this dev PC):** all five
new pinned tags resolve on the registry (immich-server/ML `v3.0.2`, immich
postgres `14-vectorchord0.4.3-pgvectors0.2.0`, `valkey:9`, `jellyfin:10.10.7`);
`docker compose config` against the real materialised homehub `.env` resolves 15
services with the bind at `/srv/library/NonDocs/Media/Movies -> /media` read-only
and **no** `devices:` key; the same file against `.env.example` still falls back
to `/srv/media` with no `mem_limit`, i.e. the public default is byte-for-byte
unchanged in behaviour. `scripts/check.sh` → PASS (config-validate,
registry-integrity, doc-navigability).

**Owner-carried risk, recorded not resolved** (Personal `open-items.md` **A17**):
Finance-Auditor is enabled *before* its own G-Release/G-Final, and its tracker
feed cannot authenticate on this box — D3's multi-user tracker wants an
`X-Forwarded-User` its feed client does not send, so the daily status post is
lost while the audit runs fine. FA carries no healthcheck *because* that post is
its liveness signal, so this fails silently and looks healthy. Immich's photo
store also has no `arch-` row in storage-map §4b, where absence-of-row is the
documented way to say "not backed up".

### DRIVER — G1 — Round 1 — 2026-08-01 (V3 GATE RE-RUN — booted, and it found three more)

Ran the full gate against the tier-2 default set: repacked ISO (5.59 GB, 15
baked images / 2473 MB payload), zero-keypress boot, hands-off install, first
boot, verified over SSH. **`homehub-firstboot` → SUCCESS.**

| Service | Result |
|---|---|
| technitium / caddy / actual / tracker | **healthy** (the four gate criteria) |
| jellyfin | **healthy**, `/health` → 200 |
| immich-server | **healthy**, `/api/server/ping` → 200 — but see (1) |
| immich-db / immich-machine-learning | **healthy** |
| ntfy / dozzle / uptime-kuma | healthy |
| oauth2-proxy / immich-redis | Up, no healthcheck by design |
| ddns | unhealthy — expected, SIM Cloudflare token |
| finance-auditor | **restart loop** — see (2) and (3) |

Bind path resolved to `/srv/library/NonDocs/Media/Movies -> /media` read-only;
`IMMICH_ML_MEM_LIMIT` landed as exactly 2147483648 bytes; step 3b mounted before
compose; step 3c wrote the override. **Memory with all 15 up: 2.4 GiB used of
7.8, 5.3 GiB available** — the 8 GB budget holds with room, though the AWOW will
also be serving Samba and running backups.

**Three findings, none of them the thing the run was aimed at:**

1. **`/dev/dri` EXISTS in Hyper-V** — `hyperv_drm` publishes `card1` (no
   `renderD*`). This repo's compose comment asserted the opposite ("container
   creation FAILS ... e.g. a Hyper-V test VM"). Corrected in three places. The
   override is still right — it just means the gate VM does **not** exercise the
   no-device branch, which is covered by direct tests instead.

2. **A stale locally-built image was baked into the ISO and nothing noticed.**
   `finance-auditor:local` in the payload was built 2026-07-11 — **19 days older
   than its repo HEAD**, from before the A8 auto-discovery work — and crash-
   looped on `ACTUAL_SYNC_ID is required`, a knob the current source does not
   require. `naglight:local` was stale too (missing 75b3e3a, the `/api/feed`
   severity colour lane), which means **the 2026-07-31 gate that PASSED was also
   running a stale tracker.** Root cause: a `*:local` image has no registry and
   no version in its tag, and every resolver treats "present" as "done" —
   `ensure-local-images.sh` skips it, `export-images.sh` saves it.
   **Fixed, and the fix took two passes.** `ensure-local-images.sh` now stamps
   `homehub.source.revision` at build time and `export-images.sh` refuses to bake
   an image whose stamp ≠ sibling HEAD (unstamped → loud warning, dirty tree →
   note, `ALLOW_STALE_LOCAL=1` to override). The **first** cut compared
   `.Created` against the sibling's HEAD date and was WRONG: a cache-identical
   rebuild reuses the image record and keeps its original `.Created`, so a
   just-rebuilt image still reported stale. Commit shas are exact; timestamps
   are not. A second layer of the same bug: `export-images.sh` skipped re-saving
   a tar that already existed — fine when the tag pins content, useless for
   `*:local`, so those are now re-saved every run.

3. **finance-auditor cannot start on a fresh box, by design, and will restart
   forever.** With the current image the error becomes the intended one:
   `ACTUAL_SYNC_ID is unset and the server has 0 budget files — cannot
   auto-pick`. Auto-discovery works; there is simply nothing to discover until
   somebody creates a budget in Actual's UI. `provision-actual.sh` sets the
   server password but no budget. So on the real AWOW this profile will sit in
   `restart: unless-stopped` backoff from first boot until that manual step
   happens — visible in `compose ps` and Uptime-Kuma as a broken service.
   Recorded, not fixed: the durable answer is FA treating "no budget yet" as
   wait-and-retry rather than fatal.

**Method note for the next session:** `Get-VMNetworkAdapter | IPAddresses` never
reported an address for this guest — Ubuntu Server does not run the Hyper-V KVP
daemon by default — so an automated runner must read the IP from the console
thumbnail or scan the Default Switch subnet, not from the Hyper-V integration
data. Verification was done over SSH from the host (WSL2 cannot route to the
Default Switch subnet; use Windows-side `ssh`/`scp`). The `hub` account is in the
`docker` group, so none of the verification needs `sudo`.

### DRIVER — G1 — Round 1 — 2026-08-01 (BACKUP DRIVE: FALSE-GREEN CLOSED — Owner question)

The Owner asked whether a disconnected **backup** drive produced a NagLight
report, or whether the only check was "did the backup run". Answer: neither, and
the gap was worse than unreported.

There were exactly two check ids in the system — `library-mounted`
(`samba/library-guard.sh`, `/srv/library`, on a 10-minute timer AND as `root
preexec` on every Samba connect) and `backup` (posted by a run). The backup
drive had **no presence check at all**: the only thing that ever looked at it was
the 03:30 run.

**And the run could not tell an absent drive from an empty directory.** The
generated fstab uses `nofail` (mandatory — a missing USB disk must not hold up
`local-fs.target` on a headless box), so with the drive unplugged
`BACKUP_TARGET=/mnt/backup-drive` is an ordinary empty directory on the system
disk. `mkdir -p "$RUN_DIR"` succeeded there, rsync copied into it, verification
passed (the files genuinely were present), retention pruned, and step 6 posted
**`ok=true`** — a green backup lane writing the household's backups to the 119 GB
system disk until it filled. Grep confirmed no `mountpoint`/`findmnt`/mountinfo
check against `BACKUP_TARGET` and no `RequiresMountsFor=` on the unit.

That is the exact silent-green shape `library-guard.sh`'s own header forbids for
the library. The guard had simply never been pointed at the second drive, while
`Generate-FromStorageMap.ps1` emits fstab lines for both.

**Fixed, in two halves:**
- **backup.sh step 0 — target preflight.** Refuses to run unless `BACKUP_TARGET`
  is a real mountpoint, and refuses on a `ro` mount (ntfs3's dirty-bit
  fallback). Posts `ok=false` first, exits 1. Placed BEFORE the `mkdir`, which is
  the whole point. NOT implemented as `RequiresMountsFor=`: systemd would refuse
  to start the unit, so nothing would reach NagLight at all — an unreported
  non-run is worse than a red one. Escape hatch `BACKUP_TARGET_REQUIRE_MOUNT=false`
  for a target that is deliberately a plain directory, which then warns loudly
  every run.
- **`homehub-backup-drive-health.timer`** — check id `backup-drive-mounted`, the
  twin of `library-mounted`, every 10 minutes. Reuses `library-guard.sh` via its
  existing `--library` plus a new `--label` (so a red check names the right
  drive); the library's own wording and check id are byte-identical to before.
  The unit reads `BACKUP_TARGET` out of `backup.env` rather than hardcoding a
  site path, and no-ops cleanly on an unprovisioned/sim box.

**Why a 10-minute cadence is safe on a parked drive:** the only probe is
`/proc/self/mountinfo`, a kernel pseudo-file. The answer comes from the VFS mount
table and **no request reaches the device**, so the check cannot wake a
spun-down disk — `df`/`stat`/`ls`/touch-tests all can, and none are used. That is
what makes this compatible with WI-10.10's `hdparm -S` policy. Factored into
`common.sh mount_options_for` so backup.sh and the guard share one implementation.

**Verified on the running V3 VM**, not just locally: units installed and enabled,
timer registered and firing; report **UNHEALTHY** with the path unmounted →
**healthy** after mounting a tmpfs there → UNHEALTHY again after unmount, with
`library-mounted` unaffected throughout; `backup.sh` preflight passes on the
mounted path, and on the unmounted one exits **1** with nothing created under the
target. Also unit-tested in WSL: the three preflight branches, the guard's
label/check-id wiring, and the unit's `BACKUP_TARGET` parsing (inline comment,
quoted value, missing file, unset key).

*Method note:* the VM's Default Switch lease moved mid-session (WSL recreated its
virtual network on a new range), so the gate VM is now at a different address —
find it by scanning the current `vEthernet (Default Switch)` subnet for port 22
rather than trusting a recorded IP.

### DRIVER — G1 — Round 1 — 2026-08-01 (MOUNT BY LABEL, VERIFY BY SERIAL — three-state drive health)

Owner's requirement: run the first days of service on **plain flash drives**, to
prove the backup, the mounts and the shares before 12 TB of real disk is
committed to them — and have that state read as **yellow**, not green, until the
real drives go in.

That is not achievable with one identifier. A by-id serial is unforgeable but a
stand-in can never carry it; a label a stand-in CAN carry proves nothing about
which disk answered to it. So the two are now split:

- **fstab mounts by `LABEL=`** (`Library`, `PriBackup`), fstype **`auto`** —
  flash drives are usually exFAT/FAT32, and `uid`/`gid`/`umask` are honoured by
  ntfs3, exfat and vfat alike, so the ownership Samba depends on is identical
  whichever turns up.
- **`drive-identity.conf`** (new generator emission) carries the expected by-id
  serial per mountpoint, and `library-guard.sh` asserts it separately.

**Three states replace the old boolean:** `green` = mounted rw + expected serial ·
`yellow` = mounted rw, right label, **wrong disk** ("stand-in drive") · `red` =
not mounted or read-only. Posted via NagLight's severity lane (`color`, one of
green|yellow|orange|red) rather than `ok`, because a boolean cannot say "working,
but on the wrong disk" — which is the entire state this exists to surface. Note
that lane arrived in NagLight `75b3e3a`, the commit the stale `naglight:local`
was missing: this only works because that image got rebuilt earlier today.

**The identity check does no disk I/O.** mountinfo field 3 gives the mounted
device's major:minor; `/sys/class/block/*/dev` and `readlink` on
`/dev/disk/by-id/*` resolve it to a stable name. Nothing opens the block device,
so the 10-minute cadence still cannot wake a parked drive — `blkid`/`lsblk -f`
would have read the superblock and could have.

**Tested on real block devices**, not mocks: two loopback filesystems both
labelled `Library` with distinct fabricated by-id names → red with neither
mounted, **yellow** with the stand-in mounted (naming both the expected and the
actual serial), **green** with the real one, and green-with-the-gap-named when
no identity file exists. Degradation is graceful throughout: no
`drive-identity.conf` = presence-only reporting, and the check says so in its own
note rather than going quiet.

`backup.sh` logs a NOTICE when the archive is landing on a stand-in but does NOT
refuse — proving the backup on a cheap disk is the point of the period. The
composite signal is the honest one: `backup` green (the run worked) +
`backup-drive-mounted` yellow (on a substitute).

**Flagged to the Owner, unresolved** (Personal `storage-map.md` §1, open-items
**A23**): the map's `dev-pc` row lists its volume labels as `Library`,
`PriBackup`, `LPBackup` and says they are "**not** the hub's main-library /
backup-drive — do not conflate them" — but those are now exactly the two labels
the hub mounts by. Either they are the same physical drives and that note is
wrong, or two volumes share each name and label mounting is ambiguous. Also
carried: the legacy FileBackup PowerShell on the dev PC matches volumes with
`-like "*<label>*"`, so a stand-in stick labelled `Library` must not be plugged
into the dev PC while it exists.

### DRIVER — G1 — Round 1 — 2026-08-01 (THE DRIVE CHECKS HAD NO CHECK DEFINITIONS)

Owner asked which project owns drive-presence reporting. Four do, and the fourth
link was missing:

1. **this repo** — sensing + reporting (`stack/samba/library-guard.sh`, both
   `homehub-*-health` units, `backup/common.sh`, `backup.sh` step 0, firstboot)
2. **Personal `homelab/deploy`** — the facts (storage-map §1 → generator →
   `library-mounts.fstab` + `drive-identity.conf` → USB)
3. **NagLight** — transport + rendering (`/api/feed`, the severity lane, auth)
4. **Personal `tracker/definitions`** — the check registry ← **was empty of these**

`/api/feed` rejects a POST whose `check` matches no automated item with
`400 unknown feeder check id`, and only `backup`, `video-stub` and `mc-update`
were ever defined. So **`library-mounted` had been posting into the void since
2026-07-30**, and `backup-drive-mounted` would have too. Silently: the reporter
logs the HTTP code and continues — right, because a reporting failure must not
mask the drive's real state, but the consequence is that a vanished drive would
be red in the journal and ABSENT from the tracker. Fixed Personal-side
(`library-drive-present` / `backup-drive-present`), verified end to end against a
running tracker: an undefined id 400s, both new ids record their colour report,
and the day's ambient colour follows.

**`tracker healthy` is one of the four V3 gate criteria and it does not mean the
tracker works.** The container healthcheck probes `/healthz`, which is
deliberately identity-free and data-free (SR-040 — a probe must not provision a
phantom user dir). Measured in the gate VM: tracker `healthy` for hours with an
EMPTY `/data`, its nightly run failing every night —
`scheduler: nightly run failed … reading definitions dir "/data/definitions"`.
Nothing in the gate noticed, because nothing looks. Recorded as Personal
**A24(ii)**; changing it is the Owner's call, since any data-bearing probe makes
a freshly-imaged box unhealthy until definitions exist.

Third, NagLight-side (**A24(iii)**): with `TRACKER_COMMIT=true` and a `/data`
that is not a git repo, a feed POST **records the report and then returns 500**
because the git commit fails afterwards. A feeder reads 500 as failure and, under
never-silent-green, reports red or retries — for data that was stored. The
homehub image is not exposed (D3 forces `TRACKER_COMMIT=false`), but the sim runs
in exactly that configuration, which is how it was found.

### DRIVER — G1 — Round 1 — 2026-08-02 (E0's WIRING HALF — THE WALL IMAGE NOW EXISTS)

`grep -rln "wall" vmtest/*.sh vmtest/*.ps1` returned **nothing** before this
session. The repo had a complete, sim-validated wall autoinstall
(`stack/autoinstall/wall/`, SR-016/017) and no way to turn it into a bootable
image, and OfficeWallNaglight had — since 2026-08-02, PKG-1 — an artifact that
**nothing took**. Two halves of E0, neither connected to the other.

**What landed.**

1. **`vmtest/build-wall-seed.sh`** — the second image target's **seed** ISO
   (~112 MB; it carries the 111 MB shell tarball). Not a bootable ISO: it is the
   light path's CIDATA seed, attached as a second DVD beside the stock Ubuntu
   ISO, exactly like the hub's. The heavier one-ISO repack path is hub-only.
2. **Shared mechanism, separate renderers — deliberately.** What both targets share — CIDATA discovery,
   the ephemeral SSH key, the repo-into-payload copy, the assert-every-
   substitution discipline — moved into `lib/common.sh` helpers that
   `build-seed.sh` now calls too. `render_seed_tree` and `render_wall_seed_tree`
   stay separate implementations and a `--wall` flag was rejected deliberately:
   one code path with two sets of load-bearing assertions is how one of them
   quietly stops biting. **The hub did change**, in three ways, and "no behaviour
   change" would have been false: its payload gained `wall-site/`, its firstboot
   gained step 3d, and its payload copy now carries **tracked files only** (see
   the review section below — that one is a fix, not a side effect).
3. **The panel installs the app.** Wall `user-data` late-command 3b untars the
   payload's shell tarball into `/opt/wall-panel/app`. **As root, with `tar`** —
   `chrome-sandbox` must arrive `4755 root:root` or Electron refuses to start
   (24.04's `apparmor_restrict_unprivileged_userns=1` closed the alternative),
   and a `cp`/unzip/rsync drops the bit silently.
4. **The hub serves the renderer.** `firstboot.sh` step 3d unpacks the site
   tarball into `stack/wall-shell/` before compose up, so the kiosk site has a
   real document root instead of 404ing at `/`. Two payloads exist at all
   because NagLight sends no CORS headers; both carry the same source commit, so
   a mismatched deploy is now visible with `cat`.
5. **A SIM `wall.env`**, installed 0600 by late-command 4a — the same `site/`
   seam shape the hub image uses.

**The `ldd` check, moved to where it can still be acted on.** A missing shared
library is a **black wall**: Electron exits before painting, `wall-kiosk.sh`
restarts it every 3 s, and `[ -x ]` stays true so the NOT INSTALLED screen never
fires either. `stack/autoinstall/wall/electron-runtime-deps.tsv` maps every
soname the shipped binary declares to its noble package (resolved with `dpkg -S`
and `apt-cache policy`, not guessed — note the `t64` renames), the builder reads
`DT_NEEDED` out of the artifact it is about to bake and refuses to build if
anything is unmapped or uninstalled, and `validate_config.py` keeps the table and
the `packages:` list in step on every commit.

**Measured, not reasoned** (a bare `ubuntu:24.04` with exactly this image's
package list, plus the installer's own late-commands run verbatim against a fake
`/target`):

- the artifact unpacks, `chrome-sandbox` is `4755 root:root`, `[ -x
  /opt/wall-panel/app/wall-shell ]` is **true**;
- `ldd` on the Electron runtime resolves **everything** — the package list is
  sufficient, not merely declared (without it, it stops at `libnspr4.so`, which
  is exactly where PKG-1's session left it);
- run as an unprivileged user the wrapper execs and Electron reaches **Ozone
  platform init**, stopping only for want of a display.

**Twelve refusals, and a suite that re-runs them.** They were exercised by hand
first, and an adversarial review made the obvious objection: a transcript in this
ledger is not a check. `vmtest/test-wall-builder.sh` now runs all twelve — the
artifact gate (absent, `-dirty`, ambiguous, and the documented
`ALLOW_MISSING_SHELL=1` way past it), the dependency gate, the five production
guards, and the `--clean` brake — in about a minute, and reports skips as skips
rather than quietly shrinking. `vmtest/test-wall-artifact.sh` does the same for
the `ldd` claim below.

**Containment, the hub's rule applied to the panel.** The sim rewrites the disk
match to `model: Virtual_Disk` and refuses to build if that `sed` no-ops or if
the panel's real `KINGSTON` model survives in an active setting. A sim ISO
written to a USB stick cannot wipe the real panel. (The first version of that
guard was a plain substring grep and refused a good build — the user-data
*explains* the pin in a comment naming the disk. Anchored to an active YAML
setting, which is the same lesson `user-data.filled`'s guards learned on
2026-07-30.)

**NOT PROVEN, and it is the whole of the next gate.** Nobody has booted the wall
ISO, so nothing below the build has ever executed: Subiquity has not run, `apt`
has not installed those packages on a real system, the late-commands have not
run under curtin, no systemd unit has been enabled, `getty@tty1` has not
autologged anyone in, and `cage` has never started. What IS verified is the
floor underneath all of that — the archive's contents and modes, the extraction
as root, the predicate, `ldd` against the real package list, and Electron
reaching Ozone init in a container. An earlier draft of this entry said
"everything short of a display is verified", which was the largest false-green
sentence in it. Nothing has watched Electron come up under `cage` on a display; the
container run above stops at "Missing X server or $DISPLAY", and it did so via
X11 — with no `WAYLAND_DISPLAY` set, `--ozone-platform-hint=auto` chose X11,
which is consistent with the concern that made PKG-1's wrapper force
`--ozone-platform=wayland` outright. Also unproven: the panel's Wi-Fi path,
which the sim **removes** (Hyper-V cannot emulate a radio and the installer needs
apt, so the wall's `wifis:` block is swapped for the hub's `e*` ethernet
matcher). A gate run this way proves the kiosk/identity/render path and nothing
about `macaddress: permanent`, powersave-off, or the DHCP reservation the `/32`
allow-list is keyed to. Those stay hardware-only (C7).

**Assumptions recorded** (AGENTS.md "running unattended"), all four in the SIM
`wall.env` and all four reversible with `WALL_ENV_OVERRIDES`:
`SLEEP_MODE=backlight` (a VM that suspends at 22:00 looks identical to a VM that
died; with no `/sys/class/backlight` in a guest it is additionally inert, so the
screen stays up for a capture); `--disable-gpu` on `WALL_APP_CMD` (`hyperv_drm`
gives `card1` with no `renderD*`); Wi-Fi values filled but unused;
`WALL_DISABLE_INPUT` empty (quirk 3 would disable the synthetic keyboard and
mouse — the console needed for the GRUB edit).

**TWO ADVERSARIAL PASSES, and they were worth more than the build was.**
Read-only, OpenAI CLI: a diff review and a claim-by-claim refutation attempt.
Twelve findings, all triaged. The six that were real are fixed and are in the
commit; three of them were defects this session introduced, and three were
older:

1. **The payload carried gitignored secrets.** `copy_repo_into_payload` archived
   the whole worktree — so `stack/provision/.token` (64 bytes, non-expiring) was
   found *inside a built payload*, and any dev box that has run the real stack
   also has `stack/.env` sitting there. Pre-dates this session and shipped on
   every hub ISO ever built here. Now `git ls-files`: tracked files only.
2. **A freshly imaged panel did not enter the kiosk on its first boot.**
   `wall-firstboot` writes the tty1 autologin drop-in but runs
   `After=network-online.target`, by which time `getty@tty1` is already up —
   and `daemon-reload` does not restart a running unit. The panel showed a login
   prompt on a machine whose account password is **locked**. It would have read
   as a failed image at the A19 gate.
3. **A real Wi-Fi PSK could break or corrupt first boot.** SSID and PSK went
   straight into a `sed` replacement: a `|` aborts firstboot *before* the
   autologin is installed; an `&` silently writes the wrong network. Both
   unreachable on a Wi-Fi-only box. The sim's values contain neither character,
   so no amount of VM testing would have found it.
4. **Four false greens in this session's own checks** — the worst kind, since
   each one passes hardest when it can see least: `readelf` failing inside a
   here-doc produced an empty dependency list and logged OK; `-dirty` was judged
   from the filename while the message claimed to have read `build-info.json`;
   rows marked `bundled` were taken on trust; and the disk pin was a `grep`, so
   a decoy `model: Virtual_Disk` anywhere in the document satisfied it (now
   parsed structurally: `autoinstall.storage.layout.match` must be *exactly*
   that mapping for a sim, and must *not* be it for production).
5. **`tar … | grep -q` under `pipefail`** reports failure when grep finds its
   match early and SIGPIPEs tar — so the check failed on a *correct* site
   tarball. Found by it actually happening. Fixed in both places with that shape.
6. **`--clean` was `rm -rf $OUT_DIR`** on an environment variable the README
   tells you to set; `OUT_DIR=/mnt/d … --clean` aimed a recursive delete at a
   drive. It now refuses any directory this builder did not create. The wall
   output also moved to `.out-wall`, a **sibling**: nested under `.out`, an
   ordinary hub `--clean` deleted the wall ISO, its SSH key and its credentials.

The rest were overclaims in the writeup rather than defects, and this entry has
been corrected for them rather than left standing.

**A correction found while writing this, not by the reviewers:** the first draft
refused `WALL_SITE_DIR` outright on the grounds that "Personal's materialiser
covers the hub only". That was **wrong** — `FieldSchema.psd1` registers both
`wall.env` and `user-data.filled` for `-Image wall`, and has since 2026-07-29.
The production seam is built instead, with the hub's guards adapted.

**Open after this.** `config.json` is IF-005's last **shell-configuration** gap
and nobody renders it — it is not the last gap overall; the unbooted ISO is.
There is no production path for a wall image: `WALL_SITE_DIR` is refused because
half a production build — real secrets on a sim-substituted `user-data` — is the
silent downgrade the hub's guards exist to prevent. A real panel is still imaged
by hand.

### DRIVER — G1 — Round 1 — 2026-08-03 (THE WALL ISO WAS BOOTED, AND IT FOUND A REAL ONE)

**First boot of the wall image, ever.** It did not reach the kiosk. It found a
defect that had been latent in **both** images since the light path existed, and
that is worth more than a pass would have been.

**What the boot proved** (all of it new — nothing above the build had ever run):

1. **Subiquity accepts the sim `user-data`.** No parse error, no interactive
   drop-out; the repacked path booted hands-off and the light path needed only
   the documented single GRUB keypress.
2. **The disk pin works in the direction it must.** Subiquity partitioned
   `lvm_volgroup-0` on the *virtual* disk — `model: Virtual_Disk` matched exactly
   what it is meant to match and nothing else.
3. **THE PACKAGE LIST IS REAL.** All 40 `packages:` entries installed on noble,
   including every one of the 23 Electron runtime libraries and all six `t64`
   renames (`libasound2t64`, `libatk1.0-0t64`, `libatk-bridge2.0-0t64`,
   `libatspi2.0-0t64`, `libcups2t64`, `libglib2.0-0t64`). The static check said
   they were declared; apt has now said they exist.
4. **Cost, for §3's planning:** Subiquity installs each `packages:` entry as its
   own `curtin system-install`, so the 23 extra libraries add roughly 20 minutes.
   A wall install is ~45-60 minutes, not the hub's ~20.

**What it found.** The install died at `late-command_9` — the **pre-existing**
"seed `wall.env` from the example" step — unable to read `wall.env.example` out
of a payload that was not there:

> **`/cdrom` is not the seed.** On the REPACKED path `/cdrom` is the combined
> ISO and carries `/deploy-payload`. On the **LIGHT** path — *the one this repo
> recommends by default* — `/cdrom` is the STOCK Ubuntu ISO, which has no such
> directory, and the CIDATA seed that does is mounted only transiently by
> cloud-init to read `user-data`. The payload rode along and **nothing ever read
> it.**

Every V3 gate has used the repacked ISO (2026-08-01: *"repacked ISO (5.59 GB)"*),
so the hub never exercised the light path's payload — and could not have
noticed if it had: `firstboot.sh` logs *"no baked image payload found"* and pulls
from registries instead, which reads as a slow first boot rather than a bug.
**~470 MB of baked container images have been going along for the ride unused.**
The wall cannot degrade that way — its units, its scripts and `wall.env.example`
all live in the payload — so it crashed, four commands downstream of the cause,
naming none of it.

**Diagnosed without a shell**, because Hyper-V needs elevation and none was
available: the installer's `command_N` numbering aligns exactly with the
`late-commands` list, so `command_9` is identifiable as the `wall.env.example`
step, and *both* halves of its `||` are explained only by a missing payload.

**Fixed.** Both images now try `/cdrom`, `/media`, `/run/media/*`, then mount the
CIDATA volume **by label**. The wall FAILS LOUDLY when none of that works; the
hub keeps its documented degraded mode but says so instead of `|| true`. All
three branches were exercised against fakes before rebuilding.
`build-repacked-iso.sh` also gained `--target wall`, so the wall gets the
zero-keypress path — and there `/deploy-payload` is simply present, which is the
branch that has actually been exercised. The repacked wall ISO is built (3.3 GB,
BIOS+UEFI intact, `/nocloud` + `/deploy-payload/wall-app/` verified present).

**STILL NOT PROVEN — the fixed image has not been booted.** `cage` has still
never started, the shell has never painted, and the crash-loop screen and the
`journalctl -t wall-kiosk` tag have still only been exercised against a fake
`cage` in a container. The VM is created and one UAC approval away; OI-17 stays
open, and it is now a *narrower* gap than it was this morning rather than a
closed one.

**A third instance of the same bug, NOT fixed** (hub production path, and
changing it untested in a wall session is exactly what AGENTS.md warns against):
`stack/autoinstall/user-data`'s **site-staging** late-command reads
`/cdrom/deploy-payload/site` with the same assumption and `exit 0`s when it is
absent. On the light path a PRODUCTION hub would therefore install **none** of
its real secrets and come up on `.env.example` values — silently. Same one-line
shape of fix; needs a hub install to verify.

### DRIVER — G1 — Round 1 — 2026-08-03 (§2's GATE CRITERION MET — and three more defects only a real panel could show)

**`journalctl -t wall-kiosk` shows `starting: cage -- /opt/wall-panel/app/wall-shell`.**
That is §2's stated bar, on a real boot, from an image built by the tracked
scripts. The panel does not yet render anything, which is §3's gate, not this
one — but getting here found three defects that no amount of container testing
could have.

**Proven on the panel, in addition to yesterday's list:**

- the payload fix works — `/opt/wall-panel/` carries the repo, `site/wall.env`
  and `wall-app/`, and the **SIM `wall.env` landed** (not the example);
- the artifact installed: `-rwxr-xr-x root root app/wall-shell`, and
  **`chrome-sandbox` is still `4755 root:root`** after NTFS → ISO → tar → ext4;
- `wall-firstboot` reports **`IF-005: ldd resolves every library the Electron
  runtime needs`** — the package list is sufficient on the real machine;
- tty1 autologin works and the kiosk starts on the **first** boot (the
  `getty@tty1` restart fix), captured from `/dev/fb0`;
- the wrapper picks Wayland from a real `WAYLAND_DISPLAY=wayland-0`, and bridges
  `MEDIA_CACHE_DIR` from `WALL_MEDIA_CACHE`;
- Electron runs and reaches `net::ERR_NAME_NOT_RESOLVED` for
  `wall.vmtest.sim.invalid` — **the correct failure**: that host is deliberately
  unresolvable and there is no hub yet.

**Defect 1 — THE FAILURE SCREENS NEVER RENDERED.** Both of them, for the whole
life of `wall-kiosk.sh`. They ran `cage -- /bin/sh -c 'printf …'`, and `cage`
displays exactly one **Wayland client**; a shell running `printf` is not one. It
writes to stdout, cage shows an empty surface, and because cage does the KMS
modeset it *also hides the text console underneath*. Measured both ways — `grim`
inside the session and Hyper-V's thumbnail — uniform black while the message sat
in the journal. **So "a dead panel must be a visible event, not silence" was
false in both directions**, and the crash-loop screen added earlier the same day
inherited the bug. It survived testing because a stand-in `cage` that simply
execs its client makes the text appear on stdout and everything look right; it
needed a compositor to expose. Fixed without any new package —
`wall-kiosk.sh` **is** the tty1 session leader, so its stdout is the console, and
with no compositor running the console is what the panel scans out. **Verified by
capturing `/dev/fb0`: the NOT INSTALLED screen renders, clean and readable, for
the first time ever.**

**Defect 2 — the kiosk could not read its own configuration.**
`/etc/wall-panel/wall.env` is `0600 root:root` (it holds the Wi-Fi PSK); the
kiosk runs as `panel`. `load_env_file` treats unreadable exactly like absent, so
it read **nothing** and every value fell back to a default: the panel logged
`PANEL_URL=https://:8443/` — an empty `WALL_HOST` — and silently dropped the
flags configured in `WALL_APP_CMD`. **On real hardware that is a panel that can
never reach its hub, with nothing anywhere saying why.** Not fixed by loosening
`wall.env`: `wall-firstboot.sh` now renders the four non-secret knobs into
`/etc/wall-panel/kiosk.env` (0644) and the PSK stays exactly where it was.

**Defect 3 — `cage` refuses to start without a GPU.** wlroots requires
`WLR_RENDERER_ALLOW_SOFTWARE=1` when EGL lands on llvmpipe:
`[render/egl.c:320] Software rendering detected`. Hyper-V's `hyperv_drm` gives
`/dev/dri/card1` and **no `renderD*`**, so this is every VM. Set **only** when
there is genuinely no render node — a panel whose iGPU regressed must still fail
loudly rather than quietly cook itself on CPU rendering inside a sealed wall
mount (quirk 6).

**How to see the panel's screen — §3 needs this and the plan's method is
incomplete.** Three capture routes, and they do not show the same thing:

| route | shows | needs |
|---|---|---|
| Hyper-V thumbnail (RGB565 → PNG) | whatever is scanned out, incl. a cage session | **elevation**; VM only |
| `/dev/fb0` (dd + convert) | the **text console** — invisible once cage takes over KMS | ssh + sudo |
| **`grim`** (wlr-screencopy, now installed) | **what cage is actually showing** | ssh + the session's `XDG_RUNTIME_DIR`/`WAYLAND_DISPLAY` |

On **real hardware there is no thumbnail API at all**, so `grim` is the only
route that works on the panel itself:
`sudo -u panel env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 grim /tmp/panel.png`

**NOT PROVEN: the shell has never painted anything.** It starts, runs, and
correctly fails to resolve its origin. Nothing has rendered the tracker's UI on
a panel, and it cannot until a hub serves the kiosk site — that is A19/§3. Also
still unproven: the fixes above are verified **on the running VM** (scripts
pushed and re-run); a clean rebuild proving the IMAGE delivers them has not been
done.

### DRIVER — G1 — Round 1 — 2026-08-03 (OI-19 — THE THIRD `/cdrom`, AND THE FIRST REFUSAL THE HUB HAS)

**Fixed, and NOT verified.** Everything below is static and structural. No ISO
was built, no VM was created, nothing was installed — and the whole defect lives
in a late-command that only Subiquity runs. Read the last paragraph before
quoting any of this as evidence.

**The defect.** `stack/autoinstall/user-data`'s site-staging step read
`/cdrom/deploy-payload/site`, fell back to `/media/...`, and then
`[ -d "$S" ] || exit 0`. That is the same wrong assumption the wall boot
exposed this morning — **`/cdrom` is the boot medium, not the seed** — with a
worse ending: the wall crashed, the hub exits 0. On the light path a PRODUCTION
hub would install **none** of `.env`, `backup.env`, `cifs.creds`,
`samba-users.creds`, `smb.conf.fragment`, `library-mounts.fstab`, and come up on
`.env.example` placeholders. `firstboot.sh` would then log
*"no smb.conf.fragment and no site payload marker — expected ONLY on a
sim/vmtest build"*, which on a real box is a **false statement in the journal**,
not a warning.

**How exposed it actually was, stated exactly** (the honest half): the only
wired production path, `Build-VentoyStick.ps1`, calls `build-repacked-iso.sh` —
and on the repacked ISO `/cdrom` really is the combined image and really does
carry `/deploy-payload`. **No stick ever built has been bitten by this.** What
sits one command away is `SITE_DIR=… bash vmtest/build-seed.sh`, which is an
equally supported way to build a production hub and is the path the README
recommends by default.

**Two halves to the fix.**

1. **The search is gone, not duplicated.** Late-command 3 already tries
   `/cdrom`, `/media`, `/media/*`, `/run/media/*` and then mounts the CIDATA
   volume **by label** — and it copies the WHOLE payload, `site/` included, to
   `/target/opt/homehub/`. So the site step now reads
   `/target/opt/homehub/site` and searches for nothing. One discovery
   mechanism, one place it can rot; a second copy of that loop is how one of
   them quietly stops matching the other. This is exactly the shape the wall
   image already uses (its late-command 4a reads
   `/opt/wall-panel/site/wall.env` rather than searching again).
2. **`exit 0` became a refusal — for production builds only.** The tracked
   user-data carries `BUILD_PROFILE=production`; `render_seed_tree` rewrites it
   to `sim` for a vmtest image and **asserts the rewrite applied**, both
   directions. The safe value is therefore the default and the SIM path is the
   one that has to opt out — a sim build legitimately has no `site/` and must
   stay a clean no-op, while a production image that lost its secrets now halts
   the install saying so. `Materialize-Deploy.ps1` renders `user-data.filled`
   from this very file (`FieldSchema.psd1` Images.homehub), so the marker
   arrives on a real stick for free — and a materialised tree that predates
   this change carries the OLD silent late-command, so the builder refuses it
   by name and tells you to re-run the materialiser.

**A seventh file, which was never installed at all.** `drive-identity.conf` is
staged into `site/` by both `render_seed_tree` and `Build-VentoyStick.ps1`, and
three consumers read `/etc/homehub-samba/drive-identity.conf` (`firstboot.sh`,
`backup.sh` step 0, `library-guard.sh`). No late-command ever copied it there.
The degrade is graceful and says so — "health checks report presence only (a
stand-in drive will read as healthy)" — which is precisely why nobody noticed
that the honest-sounding message was the ONLY outcome available. It is in the
install table now.

**Per-file absence is loud but not fatal, and that line is UNRATIFIED.**
`Build-VentoyStick.ps1` marks `cifs.creds`, `samba-users.creds` and
`drive-identity.conf` `Required=$false`, so halting a household's install over
one of them would refuse legitimate builds. A file that is PRESENT and fails to
install IS fatal. Whether a **missing `.env`** should halt as well is the
Owner's call (A10, above) — not decided here.

**What was run, and its output.** `vmtest/test-hub-seed.sh` is new, in the shape
`test-wall-builder.sh` established: negative paths, refusals that must bite,
skips counted as skips. It builds real sim and production seeds and then
**extracts late-command 4b from the user-data each build produced** and runs it
against a fake `/target` — so what is exercised is the artifact, not a
paraphrase of it.

```
=== the production seam (builder) ===        SITE_DIR w/o user-data.filled refused
                                             a pre-OI-19 user-data.filled refused
=== the SIM build ===                        BUILD_PROFILE=sim; meta-data homehub-vmtest
=== the PRODUCTION build ===                 BUILD_PROFILE=production kept; site/ staged
=== the substitution assertions ===          a user-data that lost the marker fails the build
=== the late-command itself (OI-19) ===      no /cdrom or /media left in it
                                             PRODUCTION + no site/  -> REFUSES (was exit 0)
                                             SIM + no site/         -> clean, loud no-op
                                             PRODUCTION + payload-borne site/ -> all seven
                                                                     files installed 0600
                                             absent OPTIONAL file -> named, not fatal
                                             absent REQUIRED file -> named, not fatal (A10)
hub seed guards: 13 passed, 0 failed, 0 skipped
```

Also green, unchanged: `python scripts/check.py` → PASS (config-validate,
registry-integrity, doc-navigability), `scripts/validate_config.py` → ALL CONFIG
CHECKS PASSED, `vmtest/test-wall-builder.sh` → 12 passed / 0 failed / 0 skipped.
`bash -n` on every shell file touched, plus `bash -n` **and `dash -n`** on all
four inline late-command bodies in both images' user-data, plus a PyYAML parse
of all four autoinstall files.

**NOT PROVEN — and this is the whole of what OI-19 still is.** Nothing here has
installed anything. Subiquity has never executed this late-command; `curtin` has
never mounted a `/target` for it; the CIDATA-by-label mount it now depends on
has been exercised on the WALL image only, and on the repacked path where
`/cdrom` was present anyway. No ISO was built in this session and no VM was
touched. The refusal is proven to fire **in bash, against a directory named
`/target` that this suite created**. OI-19 is FIXED-BUT-UNVERIFIED; it closes on
a hub install, not before.

### DRIVER — G1 — Round 1 — 2026-08-03 (the hub's production meta-data was stamped `homehub-vmtest`)

The wall handoff's §5 item 7, closed. `render_seed_tree` applied **both**
meta-data seds unconditionally, so a `SITE_DIR` (production) build got
`local-hostname: homehub-vmtest` and `instance-id: homehub-vmtest-<ts>` in the
seed it burns onto a real stick. Cosmetic — Subiquity's `identity.hostname` is
what the installed box answers to, and that was always `homehub` — but wrong,
and wrong in the direction that gets quoted back later as evidence ("the seed
says vmtest, so this must be the sim stick"). The wall builder had already
learned this (`render_wall_seed_tree` marks the hostname on the sim path only);
the hub had not, and it was deliberately left alone in a wall session.

**Fixed by reading the answer off the file we just rendered**, rather than by
adding a second constant: `autoinstall_hostname` parses `identity.hostname` out
of the rendered user-data — **structurally, with PyYAML**, because the shipped
user-data explains the hostname choice in a comment four lines above the setting
and any line-based read is one comment edit away from the wrong string — and
both meta-data values are written from it. Sim and production now agree BY
CONSTRUCTION in both modes, and a production `user-data.filled` that named some
other host would carry that name through instead of being overwritten with
either constant.

**The assertion habit is kept, and strengthened.** The old seds were anchored to
the literal source strings (`instance-id: homehub-001`, `local-hostname:
homehub`) and the result was asserted against the literal `homehub-vmtest`. The
new ones rewrite the KEYS and assert the RESULT equals the hostname this build
intends — so a renamed or deleted key in `stack/autoinstall/meta-data` still
fails the build loudly, which is the failure the old assertion existed for.
`autoinstall_hostname` additionally refuses a name that is empty or carries
anything outside `[A-Za-z0-9.-]`, since it is interpolated into a `sed`
replacement.

**Run:** `vmtest/test-hub-seed.sh` → **16 passed, 0 failed, 0 skipped** (the
13 from the OI-19 entry above plus three new: a production seed's
`local-hostname` is `homehub`, its `instance-id` is `homehub-<ts>`, and a
`meta-data` with its `local-hostname:` line deleted fails the build).
`vmtest/test-wall-builder.sh` 12/12 unchanged, `scripts/check.py` PASS,
`bash -n` clean.

**Not proven:** the same thing as everything else on this page — no ISO was
built and nothing was installed. What is asserted is the content of a rendered
`meta-data` file on disk.

### DRIVER — G1 — Round 1 — 2026-08-03 (`config.json` — nothing staged the panel's credentials, and nothing said so)

Personal's `Materialize-Deploy.ps1` started emitting `out\homehub\config.json`
(its commit `bdf9aba`) and **this repo had nowhere to put it.** The hub's
site-file list is hand-maintained in two places — the staging loop in
`vmtest/lib/common.sh` and the install table in `user-data` late-command 4b —
and `config.json` was in neither, so it rode nothing and reached nothing.

**It is a HUB artifact, not a panel one**, and that is the non-obvious part:
`js/config.js` `loadConfig` fetches `./config.json` **relative to the page
origin**, and the origin is Caddy's `{$WALL_HOST}:{$WALL_PORT}` site
(`root * /srv/wall-shell` ← `/opt/homehub/stack/wall-shell/`, filled by
`firstboot.sh` step 3d). The panel's Electron host intercepts `/media/*` and
nothing else, so a copy on the panel's disk would never be read.

**Why the gap was invisible, and would have stayed so:** `loadConfig` never
throws. A 404 or a parse error returns the `js/config.js` DEFAULTS plus a
`console.warn` — on a wall, in a browser nobody has a console for. The panel
comes up looking like it works, with no `FEED_TOKEN` (its feed posts are
unattributed), no heartbeat, and no music credentials. Same family as the
2026-08-03 failure screens: the absence has no symptom.

**Staged in the builder, installed by firstboot — and the ordering is the
design decision.** `config.json` joins `render_seed_tree`'s staging loop, so it
rides the payload like the other site files. It does **not** join late-command
4b: its destination is a **web root**, not `/etc`, and step 3d untars the site
tarball **over** that directory hours later. Of the two options — copy after the
untar, or assert the tarball can never contain one — this took the first:
`firstboot.sh` gains **step 3e**, immediately after 3d. "Today's site tarball
ships no config.json" is a promise about a private sibling repo's future
releases, and a release that started shipping a `config.example.json`-shaped one
would silently overwrite real credentials with placeholders. Copying afterwards
makes the ordering correct **by construction**; the collision, if it ever
happens, is logged rather than assumed away. Both site-file lists now
cross-reference each other in comments, because they are hand-maintained and
drifting apart is exactly how this file went missing.

**MODE — 0600 root:root, and OWED A RULING (OI-20 above).** Worked out, not
guessed: `docker image inspect caddy:2.11.4-alpine` shows no `USER`,
`docker-compose.yml` sets no `user:` for caddy, and `docker run --entrypoint id`
on the pinned image reports `uid=0(root)`. So the container reads the read-only
bind mount as root and the most restrictive mode that serves is the same 0600
the other site files get. **It was not widened to 0644.** What needs the Owner
is the posture rather than the digits: this file carries `FEED_TOKEN`, a Kuma
push token and the Subsonic password, and it is now **served over HTTP** —
behind the kiosk site's `remote_ip {$PANEL_IP}/32` matcher, so not open to the
LAN, but an allow-list is not a secret store. Two consequences to accept: a
`user:` added to caddy later makes the file unreadable and the panel degrades to
defaults **silently**, and anything that can spoof the panel's source address
gets the token.

**Flagged, not fixed (Personal's, and that repo is clean):**
`Build-VentoyStick.ps1` assembles `out\site\` from a hardcoded `$wanted` list
that does not include `config.json`. So a real Ventoy stick still will not carry
one; `SITE_DIR=<a directory containing it>` builds now do.

**Run:** `vmtest/test-hub-seed.sh` → **20 passed, 0 failed, 0 skipped** (4 new:
`config.json` rides the payload; firstboot installs it at a line strictly AFTER
the untar; it is installed `-m 0600 -o root -g root`; and it is NOT in
late-command 4b's table). `test-wall-builder.sh` 12/12, `scripts/check.py` PASS,
`validate_config.py` ALL PASSED, `bash -n` on all three files touched plus
`dash -n` on the late-command bodies.

**Not proven:** `firstboot.sh` step 3e has never run. Nothing has served this
file, no panel has fetched it, and the uid finding is from the image on this dev
box — not from the AWOW.

### DRIVER — G1 — Round 1 — 2026-08-03 (an adversarial review of yesterday's fixes: eleven findings, ten real)

An independent read-only reviewer (OpenAI CLI) was pointed at the four commits
that fixed OI-19, the production meta-data hostname, `config.json` staging and
the A19 sim fixture, and asked the question this repo asks of everything: *would
it notice?* Ten times the answer was no. Four of those were tests that report
success while proving nothing — and those are the reason the other six survived
a green run, so they were fixed with the same weight.

**OI-19 HAD A SECOND HEAD, and it was the same head.** The refusal added
yesterday asks whether the payload's `site/` is a DIRECTORY. `render_seed_tree`'s
staging loop counted `user-data.filled` — which it has already made MANDATORY
thirty lines earlier — as a staged site file. So a `SITE_DIR` holding nothing
else satisfied `staged > 0`, built a clean production ISO, created
`deploy-payload/site/`, and handed 4b exactly the directory it was looking for.
4b then logged every required file missing (they only printed and `continue`d),
touched `.site-present` so `firstboot.sh`'s "site payload was present but the
fragment is missing" branch stayed quiet, and exited 0. Measured on this box
before touching anything: build rc=0, `site/` holding one file, 4b rc=0,
`.site-present` created. The directory's existence was never the property worth
checking. Both ends now check the FILES: the builder refuses to stage a
production image missing any of `.env`, `backup.env`, `smb.conf.fragment`,
`library-mounts.fstab`, `config.json`, and a missing `required` file makes 4b
exit 1. **That last one closes A10(a) in the REFUSE direction** — recorded, not
assumed; the lever to reverse it is moving a file from `required` to `optional`
in 4b's own table.

**`exit 0` AFTER A FAILED COPY.** Late-command 3's two payload branches read
`cp -a … && echo …; exit 0`, and `exit 0` is a separate command after the `;` —
it ran whether or not the copy worked. One I/O error partway through ~470 MB of
baked images left a half-copied `/target/opt/homehub`, printed "payload copied",
and handed 4b a `site/` holding whatever made it across before the error. "No
payload" is a recoverable state the box knows how to describe and says so
loudly; "some of the payload" is not a state at all. Now fatal, along with a
CIDATA seed that will not unmount after a successful copy.

**BUILD_PROFILE WAS A SUBSTRING SEARCH.** `grep -q BUILD_PROFILE=production`
over the whole file matches the four lines of comment that explain the marker as
happily as the assignment. A hand-edited `user-data.filled` could keep every
comment while the ACTIVE assignment said `sim` — the guard accepts it, and 4b
then silently permits a missing `site/` on a machine whose meta-data, hostname
and disk pin all say production. `assert_build_profile` parses the document
(`yaml.safe_load` discards comments outright), looks only at `late-commands`,
and requires EXACTLY ONE assignment with the expected value: zero means 4b reads
an unset variable and cannot refuse anything; two means the last one wins and
the file no longer says what it does. Third member of the family that already
holds `assert_storage_pin` and `autoinstall_hostname`, and for the third time
the reason is the same — a line-based read is one comment edit from the wrong
answer.

**NOTHING CHECKED THAT CADDY CAN READ THE FILE IT SERVES (OI-20).** The
healthcheck probes the admin API on :2019, which is up whenever the process is.
So the 0600 root-owned `config.json` becoming unreadable — a `user:` in
`docker-compose.yml`, a `USER` in a newer caddy image, daemon userns-remap —
leaves caddy `healthy`, the kiosk site 403/404s on `/config.json`, and
`loadConfig` NEVER THROWS: the panel paints on `js/config.js` defaults with no
`FEED_TOKEN`, no heartbeat, no music credentials. `:ro` and `read_only:` are
explicitly not the risk; both restrict writes and this is a read. `firstboot.sh`
gains **step 4b**: whenever the production source exists, assert the read AS THE
CONTAINER (`docker exec` inherits the service's user, so it fails precisely when
caddy would), and carry a failure to a non-zero exit at step 7 so the unit shows
FAILED rather than letting a silently-degraded panel look like a clean boot.

**A COLLISION WARNING THAT WAS TRUE ON EVERY REBOOT.** `firstboot.sh` step 3e
detected "the site tarball shipped its own `config.json`" by testing the
DESTINATION after the untar. `homehub-firstboot.service` has no marker guard and
no `ConditionPath*`; `RemainAfterExit=yes` only stops a second start within one
boot, and the unit is `WantedBy=multi-user.target`. So from the second boot
onward the file it found was the one IT had installed, and the warning fired
forever, on every hub, whether or not a tarball ever carried one — which is how
the real collision would have gone past. Answered from `tar -tzf` before
extracting now, written to a FILE first because `tar | grep -q` under `pipefail`
reports failure exactly when grep FINDS the match. The unit file's "becomes a
no-op on later boots" comment was simply false and is corrected.

**FOUR TESTS THAT PASSED WHETHER OR NOT THE CODE WAS THERE.** This is the part
that matters most, because it is why the above survived yesterday's green run.

- The suite **edited the repository's own tracked `user-data` and `meta-data`**
  and restored them from an EXIT trap. A trap is not a transaction: a kill at
  the wrong moment leaves a deliberately-corrupted template in the checkout, and
  a concurrent `build-seed.sh` would bake it. The builders now run against an
  ISOLATED COPY (`git ls-files` → `tar` → `git init`, so the payload copy takes
  the same `git ls-files` path a real build takes rather than the loud
  whole-worktree fallback) and the corruption happens there.
- **`extract_4b` exited 0 when it found no command.** So both negative
  assertions built on it — "no `/cdrom` left in the site step", "no
  `config.json` in 4b's table" — passed when the command was MISSING, which is
  exactly what a reverted OI-19 fix leaves behind. Extraction now demands
  exactly one match. **PROVEN by stubbing:** with 4b reverted to a pre-OI-19
  body the suite goes 28/0/0 → **7 passed, 14 FAILED, 1 skipped, exit 1**.
- **The case advertised as "the light-path case" never ran the light path.** It
  hand-created `/target/opt/homehub/site` and ran 4b alone — no late-command 3,
  no discovery, no mount, no copy. The light path is what OI-19 was about, so
  this was the test that most needed to bite and the one that structurally
  could not. It now runs EXTRACTED STEP 3 then 4b, three ways: a real payload
  under a real `/media` entry; a FORCED `cp` failure (a regular file where the
  directory must be); and the CIDATA branch **for real** — `losetup` the seed
  ISO this suite just built, `blkid -L CIDATA`, the command's own
  `mount -o ro`. Loop mounts, `/media` and `install -o root` need root, so a
  non-root run SKIPS them — and **skips now make the suite exit non-zero**,
  because "we could not look" is not a pass. **PROVEN by stubbing:** with step 3
  replaced by `exit 0` the suite reports **25 passed, 3 FAILED** (media path,
  cp-failure, CIDATA path); with only the `cp`-failure fatality removed,
  **27 passed, 1 FAILED**, and the failure line shows the command printing
  "payload copied" immediately after `cp` said "Not a directory".
- **Config installation was checked by line number and grep**, and "all seven
  files 0600" stat'd `.env` alone — so unreachable code, a widened
  `drive-identity.conf` (the file carrying disk serials, which nothing installed
  at all before yesterday) or any later chmod regression all passed.
  `firstboot.sh`'s 3d/3e region is now CARVED OUT AND EXECUTED against a scratch
  tree under `set -euo pipefail` (so the `tar | grep -q` trap is real), in three
  cases: a clean run, **the rerun**, and a tarball that really does ship a
  `config.json`. Every one of the seven destinations is `stat`'d for
  `600:root:root` individually.

**THE SIM PASSED WITH THE A19 FIXTURE DELETED.** `sim/validate-sim.sh` never
posted to `library-mounted` or `backup-drive-mounted`, so deleting
`sim/tracker-seed/definitions/drives.md` outright left the gate green — the same
defect the fixture exists to close, one level up. New **check 6b** posts a colour
report for both ids and asserts per-ITEM `reportColor` on
`library-drive-present` / `backup-drive-present`. Per item, not the ambient band,
and that is a measured fact rather than a preference: `engine.Aggregate` is a MAX
over lane scores, so one red report reaches red alone and `color_weight` only
orders the overlay's offenders — meaning **a red screen does not prove these two
lanes are red**. It then flips ONE lane green and asserts the other stays red, so
the assertion cannot be satisfied by a field that merely exists. Feed POST codes
are reported, not asserted, because with `TRACKER_COMMIT=true` on a non-git
`/data` a post stores the report and THEN returns 500 (A24(iii)).

**Runs (all real output, WSL2/Ubuntu as root):**
`vmtest/test-hub-seed.sh` → **28 passed, 0 failed, 0 skipped** (up from 20; the
new ones are the comment-vs-active BUILD_PROFILE case, the `SITE_DIR` holding
only `user-data.filled`, the missing `config.json`, three executed 3d/3e cases,
the light path via `/media`, the forced `cp` failure, the CIDATA seed, and the
`site/`-with-only-`user-data.filled` refusal).
`vmtest/test-wall-builder.sh` → **12 passed, 0 failed, 0 skipped**, unchanged.
`python scripts/check.py` → **RESULT: PASS** (config-validate, registry-integrity,
doc-navigability). `scripts/validate_config.py` → **ALL CONFIG CHECKS PASSED**.
`bash -n` on every shell file touched; `bash -n` AND `dash -n` on both inline
late-command bodies extracted from the rendered `user-data` — they run under
`sh` via curtin, not bash.
`sim/validate-sim.sh` → check 6b PASS on both cases against the RUNNING sim, i.e.
against NagLight's real loader and engine; **and PROVEN to bite**: with
`drives.md` deleted both cases FAIL with POST 400/400 and no `reportColor` on
either id, and pass again when it is restored. Checks 2-8 green. **Check 1's five
failures in that run are a stale environment, not a regression** — the containers
on this box were created before commit `85401f6` renamed the compose project
`awow-sim` → `homehub-sim`, so `docker compose -p homehub-sim ps -q` finds
nothing while `docker ps` shows every one of them healthy. A `sim/run-sim.sh`
recreate clears it.

**The reviewer's list of SURVIVORS was re-checked rather than taken on trust**
and holds: the hostname is parsed off the rendered file after substitution,
PyYAML's absence hard-fails during validation, multi-document input is rejected,
late-command 3 does precede 4b and does copy `site/`, `drive-identity.conf`
lands 0600 root-owned, `drives.md` parses under NagLight's YAML subset, and all
four seed files' item ids are unique. Nothing was changed on account of them.

**Not proven, and the list has not got shorter in the way that counts:** nothing
here has been installed. `firstboot.sh` step 4b has never run — no hub, no
compose bring-up — so the caddy-readability assertion is asserted-in-source
only. Subiquity has never run either late-command; what IS new is that they have
now been EXECUTED — by this suite, as root, against a fake `/target`, a real
`/media` entry and a real loop-mounted copy of the seed ISO this repo builds.
That is a strictly larger claim than yesterday's and still a strictly smaller
one than an install. OI-19 closes on a hub install; OI-20's mode ruling, and the
A10(a) refuse-direction decision made here, are the Owner's.

### DRIVER — G1 — Round 1 — 2026-08-03 (OI-18 ruled (b): the panel gets two mounts, two credentials and two failure policies)

The Owner ruled **exit (b)** — two UNC/credential pairs — and this is the half
of it that lives here. Exit (a), consolidating both trees behind one host, was
rejected because it would have re-opened `HOMELAB_TOPOLOGY.md` decision 2 ("the
panel pulls frame videos from Mini-serv directly").

**The contract was implemented against `Personal\homelab\deploy\storage-map.md`,
not against the old `SUBTREES` string, and the map was re-read first to confirm
the derived table in `WALL_GATE_HANDOFF.md` §5 rather than trusted.** It agreed
on every row, and `Generate-FromStorageMap.ps1 -Preview` independently derives
the same two flows from the same map — `sync-frame-videos
mini-serv://mini-serv/PictureFrameVideos -> wall-frame-pc:frame/` and
`sync-music main-library:/srv/library/NonDocs/Media/Music ->
wall-frame-pc:music/ (via //<AWOW>/Media)`. No edit to the map was needed and
none was made.

**THE TWO THINGS A SYMMETRIC IMPLEMENTATION GETS WRONG, and how each landed.**

*They are not the same shape.* Music is reached **through** the `Media` share
(§3 row 1 exports `NonDocs\Media`; §3 row 2 puts the music at
`NonDocs\Media\Music`), so the mirror descends into a `Music` subdirectory under
the mount. `PictureFrameVideos` is a **dedicated** share (§3b) whose content is
at its **root**, with no subdirectory at all. `flow_spec()` carries an empty
`F_SUBDIR` for frame and the mirror uses the mountpoint directly. Give frame a
subdirectory it does not have and the mirror looks one level too deep and finds
nothing — which, but for GUARD 1, would have mirrored emptiness over the cache.
The suite proves both directions: a stub that gives music no subdir and a stub
that gives frame the retired `FrameVideos/` one each turn a case red.

*They have different FAILURE POLICIES.* §4d: Mini-serv may sleep and is never
woken for this, so an unreachable frame share is skipped; the AWOW is always-on,
so a refused music mount is an alert. The old script had exactly one policy and
it was the music one.

**The decision that mattered most here is HOW the skip is decided.**
`mount.cifs` returns the same rc for "the box is asleep" and "the password is
wrong", so inferring the state from the mount failure would have made a wrong
credential permanently invisible on the one share designed to fail quietly.
`wall-sync.sh` therefore **probes 445 with a 4 s bounded TCP connect before
mounting**: nothing answering is a skip; a box that ANSWERS and then refuses the
mount is **fatal**, and says so in those words. Configuration defects — an unset
or placeholder UNC, a missing or unreadable credentials file — are fatal for
**both** flows. The frame flow's licence to be quiet does not extend to "nobody
filled this in".

**The silent skip still owes a consequence, so it reports one.** Every
successful flow stamps its finish time at the cache **root** — outside the
mirrored leaf, where `--delete` cannot remove it and the manifest walker cannot
see it — and every skip logs how old the cached content now is: a log line below
`WALL_FRAME_STALE_WARN_HOURS` (default 24), a WARNING above it, and "this panel
has NEVER completed a frame sync" when there is no stamp at all. It **never**
escalates into a failed unit however old the content gets: "Mini-serv has been
off for a week" is an allowed state per §4d, and at what age it stops being
allowed is a ruling nobody has made. Recorded as **A11(iv)** — whether it should
ever alert, and whether it should post to the tracker, is the Owner's.

**TWO UNITS, ONE SCRIPT, and that split was made deliberately rather than by
default.** §4d puts the frame flow on a one-minute accessibility-checked timer;
OI-15 gave the music pull boot + resume + on demand and *no* timer. One unit
cannot carry both — re-walking a music library over 802.11 sixty times an hour
is absurd, and the first music sync is allowed a full hour while a frame run
that is still going after two minutes is stuck. So `wall-sync.service` keeps its
three triggers and now syncs BOTH flows (the documented on-demand command means
exactly what it always meant), and `wall-sync-frame.{service,timer}` is the same
script with `--only frame`. One script, because every `--delete` guard is
identical between the flows and a second script is a second place for them to
drift. Two collision guards, because the units genuinely overlap: a per-flow
`flock`, and a new `--only` on `wall-media-manifest.py` so the every-minute
frame run cannot rewrite `music/index.json` from a music cache the boot run is
still filling.

**One flow's failure no longer cancels the other.** Each flow is attempted, each
gets its own verdict, and the exit status is non-zero if any failed — so a
HOMEHUB outage cannot also stop the frame videos refreshing in the same run.

**`SUBTREES` is gone as a string but kept as a property.** The comment at its
old line said a knob there would let a typo silently widen the mirror onto a
256 GB laptop disk. The map is now a `case` in `flow_spec()` with seven fields
per flow; only the UNC, the credentials file and the bench hook name knobs — the
subtree, cache leaf, manifest and failure policy are code. A `case` rather than
a longer colon-delimited string because two of the seven fields are prose.

**Retired, and each for a reason rather than for tidiness:** `MEDIA_SHARE_UNC`
and `MEDIA_CIFS_CREDENTIALS` (one address and one credential cannot reach two
hosts — that IS OI-18); the inline `MEDIA_CIFS_USER`/`MEDIA_CIFS_PASS` fallback
(same reason, and duplicating it per flow would have added four knobs whose only
purpose is to keep a password somewhere less safe than the file that already
exists); and the single `MEDIA_SOURCE_OVERRIDE` bench hook, now one per flow
because one directory cannot be both "has `Music/` under it" and "is the
content". `MEDIA_CIFS_EXTRA` stays **one** knob shared by both mounts: it is a
protocol choice and both hosts speak SMB3.

**The image half.** Wall late-command 4a now installs **two** credential files,
`0600 root:root`, naming each absence individually — "one of two" is a panel
with half its media, and which half decides whether the wall is silent or blank.
The new units are cp'd and the **timer** (not the service) is enabled;
`wall-firstboot.sh` enables it too and reports **both** sources separately.
`validate_config.py`'s referenced-file list grew the two units, because a frame
timer that is missing looks — from the journal — exactly like a frame flow whose
source is asleep, which is the state it is designed to be quiet in.

**COVERAGE, AND IT WAS PROVEN THE HARD WAY.** `vmtest/test-wall-builder.sh` goes
from 12 cases to **26**, and the 14 new ones were each verified to BITE by
breaking the code they watch and re-running the whole suite:

| stub | case that turned red |
|---|---|
| silence the missing-music-creds NOTE | names the MUSIC source as the casualty |
| silence the missing-frame-creds NOTE | names the FRAME source as the casualty |
| drop `cifs-frame.creds` from the staging loop | both credential files are staged |
| disable the stale-`cifs.creds` refusal | a stale PRE-OI-18 `cifs.creds` is refused |
| stop filling `MEDIA_MUSIC_SHARE_UNC` in the sim env | sim build refuses if the example loses it |
| stop filling `MEDIA_FRAME_SHARE_UNC` in the sim env | sim build refuses if the example loses it |
| give music no subdir | music comes from `Music/` UNDER the mount |
| give frame the retired `FrameVideos/` subdir | frame comes from the share ROOT |
| make the manifest generator ignore `--only` | `--only frame` leaves `music/index.json` alone |
| give frame the ALERT policy | an unreachable frame source is a silent skip |
| give music the SILENT-SKIP policy | an unreachable music source is an ALERT |
| remove both mirror-delete refusals | an EMPTY music source still refuses |
| drop the frame flow from `FLOWS` | a full run mirrors BOTH flows |
| make `--only` always select music | `--only frame` refreshes the frame flow |

The first pass at that verification is worth recording because it is the same
trap this suite exists for: three of the stubs were line-range deletes that also
ate a line-continuation backslash and an `fi`, so `common.sh` stopped parsing
and *every* case went red. The target case failed, but it proved nothing — a
broken file is not a disabled guard. They were re-run surgically (one anchored
replacement each, `bash -n` asserted before the run) and each then produced
exactly **one** FAIL line. A fourth stub renamed the frame flow's cache leaf,
which leaves the `frame: manifest` log line intact and therefore never touched
the case it was aimed at; it was replaced with one that makes `--only` always
select music.

**Two of the new sections need root** (the script takes a `/run` lock and calls
`mount`) and one needs 445 closed on loopback; both are `skip_case`d loudly and
counted, per this file's existing "a suite that quietly shrinks is the same
false green" rule.

**Verification, all real output:** `test-wall-builder.sh` 26 passed / 0 failed /
0 skipped; `test-hub-seed.sh` 28 passed / 0 failed / 0 skipped;
`python scripts/check.py` PASS (config-validate, registry-integrity,
doc-navigability); `validate_config.py` ALL CONFIG CHECKS PASSED with 16 wall
knobs declared; `bash -n` + `dash -n` clean on `wall-sync.sh` (it is now
dash-PARSEABLE, having lost its herestring, though it stays a bash script —
`${!var}` and `printf -v` are bash) and on `test-wall-builder.sh`; all **35**
wall late-commands `dash -n` clean as the outer shell sees them, and every
`bash -c` body `bash -n` clean. `wall-firstboot.sh` and `vmtest/lib/common.sh`
still fail `dash -n`, on pre-existing process substitutions this change did not
touch and that never run under dash.

**WHAT IS NOT PROVEN. Nothing here has mounted anything.** No ISO was built from
this, no panel was installed, and neither credential has ever authenticated
against a real Samba service. The two policy tests use 127.0.0.1, which answers
nothing on 445 — they prove the *decision*, not the mount. Read "emits" and
"refuses" literally; neither is "works".

### DRIVER — G1 — Round 1 — 2026-08-04 (the OI-18 adversarial review: the knob-containment claim did not survive, and the suite meant to defend it exited zero with the code disabled)

A second read-only adversarial review (OpenAI CLI) returned **40 findings**
against the OI-18 build. The headline claim of the previous entry —
*"`SUBTREES` is gone as a string but its property is kept — only the UNC, the
credentials file and the bench hook are knobs; subtree / cache leaf / manifest /
failure policy are code"* — **was refuted five different ways, and that property
is the entire reason `SUBTREES` was never a knob.** Everything below was
reproduced before it was fixed; three findings were partly wrong and are recorded
as such rather than quietly dropped.

**THE KNOB-CONTAINMENT PROPERTY WAS A COMMENT, NOT A MECHANISM** (findings 1-5).
`load_env_file` exported **every valid identifier** it found in `wall.env`, and
it ran *after* the script's own constants were set. So `wall.env` could set
`FLOWS=music` (dropping the frame flow out of every boot and resume run),
`PROBE_PORT=1` (making Mini-serv permanently "asleep"), or redirect `RUNDIR`,
`PATH` or `TMPDIR`. Four more routes existed beside it:

- `MEDIA_CIFS_EXTRA` was a free-text option string appended **after** the
  code-built cifs options, so `prefixpath=Movies` re-pointed the subtree, `ip=`
  the host, `rw` the read-only guarantee and a second `credentials=` the account.
- `WALL_MEDIA_CACHE` accepted any absolute path, including one whose `music` or
  `frame` leaf is a symlink — and `rsync -a --delete DEST/` **follows** a
  symlinked destination (proven on the bench: mirroring into `link/` writes and
  deletes inside `real/`).
- `MEDIA_FRAME_SOURCE_OVERRIDE` was an unrestricted **production** setting.
  `=/` mirrored the panel's filesystem; `=/etc/wall-panel` copied `wall.env` and
  **both credential files** into the kiosk-readable cache at 0644. That is a
  credential disclosure through a debug knob.
- the frame flow consumes the **share root**, so a typo'd
  `//MINI-SERV/NetworkShare` passed the generic `//?*/?*` check and would have
  mirrored an entire general-purpose share onto a 256 GB panel disk.

Now: an explicit `CONFIG_KEYS` **allowlist** (eight keys), every internal
constant `readonly` after the file is read, retired keys **refused loudly**
rather than ignored, `MEDIA_CIFS_VERS` as a validated **enum** with every other
mount option built in code, symlink-component refusal on the cache root and on
each leaf, `//host/share` **only** (no path tail, no traversal), and the bench
hook moved out of configuration entirely — it is `--bench-source FLOW=DIR`,
refused when `INVOCATION_ID` says systemd is the caller, and constrained to
root-owned fixtures under `/var/lib/wall-sync/bench`.

**A RULING TURNED INTO A GUARD.** RULED 2026-08-04 (the Owner): the HOMEHUB music
account is **also named `share`** — two accounts, two hosts, two passwords, one
username, made by hand at each box. That is now engineered against, not merely
obeyed: `wall-sync.sh` **refuses** a config whose two UNCs name the same host, or
whose two flows point at the same credentials file, because with a shared
username one credential could otherwise satisfy both mounts and mirror the wrong
share with nothing failing anywhere. Every place this change touched that said
"the `share` account" now names its **host**.

**CORRECTNESS AND SAFETY** (6-9, 13-16, 19, 20). Each was reproduced first:

| finding | what actually happened on the bench |
|---|---|
| 6 | `open(path + ".tmp", "w")` **follows a symlink**: a source containing `playlist.json.tmp -> victim` made this root process truncate, rewrite and chmod 0644 that victim. Now `mkstemp` + `lstat`, under a random `.wall-manifest.*` name the mirror excludes. |
| 7 | `set -e` is **disabled for the whole dynamic extent** of `if ! sync_flow` (proven: a `false` inside the function did not stop it). A failed `mktemp -d` made `"$from/"` the string `/`. Every fallible command in the function is now checked explicitly. |
| 8 | the empty-source guard counted files rsync then **excludes**, so a source holding only `Music/index.json` passed it and `--delete` emptied the cache with `WALL_SYNC_ALLOW_EMPTY=false`. |
| 9 | the cache count excluded the manifest basename at **every depth**, so a cache holding only `Album/index.json` counted as zero and the refusal never fired. Both helpers now exclude by EXACT top-level path — the same rule rsync uses. |
| 13 | the lock was released **immediately after rsync**, before the manifest walk and the stamp. Unmount and unlock are now separate steps and only end-of-flow releases the lock. |
| 14 | a failed unmount was warned and then converted into **success**. It now fails the flow. |
| 15 | **every** `flock` failure was read as contention. Proven: a bad descriptor exits **65**, contention exits 1 — and a failed `exec 9>` does not stop the function, so a lock file that cannot be created ended the run as `sync complete — nothing to do`. Now `flock -n -E 100`, and 100 alone means contention. |
| 16 | one `TimeoutStartSec` covered both flows in sequence, so a wedged music mount meant the frame flow **never ran**. Each flow now carries its own mount/rsync/unmount budget; the unit timeout is a backstop above their documented sum. |
| 19 | `08` is all digits and an invalid **octal** literal: `$(( now - 08 ))` aborts (proven). Stamps are now length-bounded and parsed base 10, and a **future** stamp is reported as unknown instead of being clamped to "0h old" forever. |
| 20 | "root-only 0600" was **claimed by the error message and never checked** — only root *readability* was. Now: regular non-symlink file, uid/gid 0, mode exactly 0600. |

**THE PROBE NOW HAS THREE ANSWERS, NOT TWO** (10-12), and this is the part the
standing ruling constrains. §4d says an unreachable frame share is skipped and
never woken, and **RULED 2026-08-04 (the Owner): the staleness ladder stays
reporting-only** — it may not fail the unit and may not post to the tracker. So
nothing here escalates; what changed is that the script no longer *claims* a
thing it has not established. The old probe called everything that was not a
completed TCP connect "Mini-serv is asleep" — Samba stopped, a firewall REJECT,
a DNS failure, the panel's own Wi-Fi down. Now:

- **AWAKE** — the connect completed. Mount; a refusal from here is FATAL.
- **OFFLINE** — nothing answered inside the probe window (`timeout` rc 124), or
  the link layer said there is no route (no ARP reply). This is the
  positively-established state §4d designs for, and the only one that earns the
  calm skip.
- **INDETERMINATE** — the name will not resolve, this panel has no default
  route, or the box answered with a **refusal** (an RST means it is *awake*).
  Still skipped, still exit 0, but logged as a WARNING that says the state is
  unknown. Proven on the bench: `127.0.0.1` gives `Connection refused` in
  milliseconds, `192.0.2.1` gives rc 124.

Finding 12 is a **wording** fix and it stands: a TCP SYN is not a "never wake"
primitive. Every claim now says *no magic packet is sent*, which is what the code
guarantees. **Whether Mini-serv's NIC is set to wake on a magic packet only, or
on any traffic pattern, is UNVERIFIED and is the Owner's to settle.**

**UNITS AND FIRSTBOOT** (17, 18). Firstboot's swallowed `systemctl enable … ||
warn` followed by an unconditional "…enabled" log line **is this repo's signature
bug**, and it was still there for the frame timer and the resume hook: a panel
whose every-minute cadence failed to enable reported a green first boot that
explicitly claimed the cadence exists. Enabling now goes through one judged
helper, the success line prints only on success, and a failure makes the unit RED
and **withholds the provisioning marker**. `wall-sync-frame.timer` used
`OnUnitActiveSec=1min`, which measures from **activation** — a run longer than a
minute re-fires immediately — while a comment in the same file claimed it meant
"a minute after the last run FINISHED". It is `OnUnitInactiveSec` now, and the
comment is true.

**AN ON-BOX CREDENTIAL EXPOSURE** (21). The staged credentials are 0600 but owned
by the **builder's uid**; the ISO is written with `-rock`, which preserves
uid/gid; `cp -a` carries that into `/opt/wall-panel/site`. The late-command
`chmod 0600`'d the payload copies — fixing a mode that was already right and
leaving the owner alone. With the ordinary builder uid and the panel's `panel`
user both **1000**, the kiosk user could read both Samba passwords for the life
of the install. The payload copies (`wall.env` and both `cifs-*.creds`) are now
**deleted** after installation; `/etc/wall-panel` holds the only copies.

**THE TEST SUITE WAS THE OTHER HALF OF THE FINDING** (26-33). Proven, not
asserted: with `wall-sync.sh` present but **unparseable** and loopback 445
occupied, the previous suite reported **14 passed, 0 failed, 2 skipped and exited
0**. This one, in the identical situation, reports **49 passed, 1 failed** and
exits 1. It goes from 26 cases to **85**:

- a **STATIC GATE** that never skips: the scripts must exist and parse, the units
  must pass `systemd-analyze verify` and say what the design says (exact
  `ExecStart … --only frame`, the cadence directive, the *absence* of
  `OnUnitActiveSec`), and `user-data` must actually cp, enable and — new —
  **delete** the staged credentials;
- **per-flow failure aggregation** is exercised for the first time (music fails,
  frame must still mirror, the run must still exit non-zero); the previous
  suite's only two-flow case had both flows succeed, so an abort-on-first-error
  orchestration passed it;
- the case the suite **explicitly skipped** — 445 answers and the mount is then
  refused, the only path that can ever expose a wrong frame credential — now runs
  against a deliberate loopback listener;
- `--only frame` **mutates the source** first, so a no-op mirror fails it;
- the two missing-credential cases use **asymmetric** states and assert the other
  message is ABSENT (they previously ran against the identical both-absent state,
  so a predicate that named both, or named the wrong one, passed both);
- the cross-repo cases now also read **Personal's** deploy half, and SKIP loudly
  when the sibling checkout is absent rather than silently proving nothing;
- the `wall.env` allowlist is driven through a **real `wall.env`**, in a private
  mount namespace (`unshare -m` + a bind mount over `/etc/wall-panel`), because
  the path not being a knob is part of the property being tested;
- everything runs against an **isolated sandbox checkout** (`git ls-files` → tar →
  `git init`). The old suite rewrote **tracked** files — `wall.env.example`,
  `electron-runtime-deps.tsv`, `user-data` — with an EXIT trap that removed only
  the temp dir, so an interrupt left the repo damaged.

**EVERY GUARD WAS PROVEN TO BITE**, one surgical anchored stub at a time on an
isolated copy, `bash -n` / `py_compile` asserted clean before each run (a broken
file is not a disabled guard — that is the mistake the last pass made). **22 of
22 stubs turned exactly their intended case(s) red and nothing else:**

| stub | case(s) that turned red |
|---|---|
| widen `load_env_file` back to every identifier | FLOWS / PROBE_PORT / RUNDIR containment (3) |
| ignore a retired key instead of refusing | `MEDIA_CIFS_EXTRA` is REFUSED |
| accept any `MEDIA_CIFS_VERS` | it is an ENUM, not an option string |
| drop the symlinked-leaf refusal | symlinked cache leaf (2, incl. the victim directory) |
| accept any `--bench-source` directory | fixture OUTSIDE the fixture root |
| allow `--bench-source` under systemd | refused when systemd is the caller |
| restore the old `//?*/?*` UNC check | a frame UNC with a PATH TAIL |
| skip the two-distinct-sources check | two UNCs on the SAME host |
| restore the predictable `.tmp` manifest name | manifest-temp symlink (2, incl. the victim file) |
| count emptiness without the rsync exclusions | a source holding ONLY the excluded manifest (2) |
| restore the two-outcome probe | REFUSED is INDETERMINATE (3) |
| accept a credentials file on readability alone | a 0644 credentials file is refused |
| `break` on the first failing flow | the FRAME flow ran anyway |
| restore `systemctl enable … \|\| warn` | firstboot judges the enable (2) |
| `OnUnitActiveSec` back in the timer | the cadence assertions (2) |
| `chmod` the staged creds instead of deleting | payload copies are REMOVED |
| drop `--only frame` from the frame unit | ExecStart is EXACTLY `--only frame` |
| make the mirror copy nothing new | the new frame file reached cache + playlist |
| report the music credential's absence always | …does NOT also blame the MUSIC source |
| point `PERSONAL_REPO` at an empty tree | all four cross-repo name cases |
| count the manifest by name at any depth | `Album/index.json` counts as POPULATED |
| drop the base-10 stamp coercion | an octal-looking stamp still reports |
| clamp a future stamp to zero | a FUTURE stamp is UNKNOWN, not fresh |

**FINDINGS THAT DID NOT SURVIVE CONTACT, recorded rather than quietly dropped:**

- **26 is right about "unparseable" and wrong about "deleted".** With
  `wall-sync.sh` *deleted*, the old suite exited **1** — not because any
  wall-sync assertion fired (they all skipped) but because the builder cases
  failed for an unrelated reason. The false green needed the file to be present
  and broken. Both are fixed; only one was real.
- **11 is half-cured, and the half that remains is the ruling.** The `port=` /
  `ip=` divergence it describes is gone with `MEDIA_CIFS_EXTRA`. But "wrong
  credentials can be permanently invisible behind a persistently failing probe"
  is what §4d *asks for* — an unreachable frame share is skipped and never woken.
  The mitigation is finding 10's INDETERMINATE class: a box that is up and
  refusing is no longer reported as asleep.
- **the claims listed as SURVIVED were left alone**, including the cross-repo
  names (35-38, 40): nothing was renamed.

**Verification, all real output, run in WSL Ubuntu as root against an isolated
checkout:** `test-wall-builder.sh` **85 passed / 0 failed / 1 skipped** (the skip
is the shell-artifact-dependent group; there is no `OfficeWallNaglight/dist` on
this host); `test-hub-seed.sh` **28 passed / 0 failed / 0 skipped**;
`python scripts/check.py` **RESULT: PASS** (config-validate, registry-integrity,
doc-navigability); `validate_config.py` **ALL CONFIG CHECKS PASSED** with **18**
wall knobs declared; `bash -n` **and** `dash -n` clean on `wall-sync.sh`;
`bash -n` clean on `wall-firstboot.sh` and `test-wall-builder.sh`;
`wall-media-manifest.py` compiles; **all 35 wall late-commands `dash -n` clean as
the outer `/bin/sh` sees them and all 7 inner `bash -c` bodies `bash -n` clean**
(and the hub's 28 + 10, unchanged). `test-wall-builder.sh` no longer passes
`dash -n` — it uses bash arrays to keep the bench arguments and the environment
apart — which matches `test-hub-seed.sh`; both are `#!/usr/bin/env bash` and
nothing routes either through `/bin/sh`.

**WHAT IS STILL NOT PROVEN, and it is most of it. NOTHING HAS EVER BEEN
MOUNTED.** No ISO was built from this change, no panel was installed, and neither
credential has authenticated against a real Samba service. Specifically:

- the reachability cases use `127.0.0.1` and `192.0.2.1`. They prove the
  **decision** — skip / warn / fail, and which — not the mount.
- findings **13, 14, 15 and 16** (lock lifetime, unmount failure, flock error vs
  contention, per-flow time budgets) are implemented and reviewed but have **no
  automated coverage**: each needs a real cifs mount, a stuck unmount or a hang to
  trigger. They are code changes on an untravelled path.
- the credential-exposure fix (21) is a change to a **late-command that has never
  run under Subiquity**.
- `systemd-analyze verify` parses the units; it has not scheduled them.

**FOR THE OWNER — four decisions this change made on its own, and one it could
not make:**

1. **`wall-sync.sh` now REFUSES two UNCs that name the same host** (and two flows
   sharing one credentials file). This is engineered against the 2026-08-04
   `share`-username ruling and is stricter than OI-18 said in words. If a
   single-host topology is ever wanted, this refusal has to be lifted
   deliberately — it will not fail quietly.
2. **A bench run now needs `/var/lib/wall-sync/bench`** and a command-line flag;
   the old `wall.env` override keys are refused. Any local habit built on them
   breaks loudly.
3. **`WALL_SYNC_ALLOW_EMPTY` must now be exactly `true` or `false`** — anything
   else is refused rather than read as "not true", because it is the switch that
   permits an irreversible mirror-delete.
4. **An INDETERMINATE frame probe warns once a minute** while it persists (a
   stopped Samba, say). It never fails the unit — the ladder stays
   reporting-only, as ruled — but it is a journal line a minute.
5. **UNVERIFIED, and only the Owner can settle it: is Mini-serv's NIC set to wake
   on a magic packet only, or on any traffic pattern?** The code guarantees it
   sends no magic packet. If the adapter wakes on arbitrary traffic, the
   every-minute probe can wake the box, which §4d does not want. Also still open
   from the previous entry: **what the HOMEHUB music account is called** — now
   ruled to be `share`, but its §2 status (a real §2 identity with a
   `Samba<Name>Password` key, or a panel-only account made by hand at the box) is
   still unruled.

---

### DRIVER — G1 — Round 1 — 2026-08-04 (`/opt/homehub` installed WORLD-WRITABLE — in every hub ISO ever built here, and only an install could see it)

**A boot found it, and nothing else could have.** The hub gate VM, installed from
a built ISO, was measured on 2026-08-04:

```
drwxrwxrwx root:root /opt/homehub
drwxrwxrwx root:root /opt/homehub/stack
drwxrwxrwx root:root /opt/homehub/images
-rwxrwxrwx root:root /opt/homehub/stack/.env
-rwxrwxrwx root:root /opt/homehub/stack/docker-compose.yml
230 world-writable paths under /opt/homehub
```

and confirmed from an unprivileged account on the box: `sudo -u nobody test -r
/opt/homehub/stack/.env` **readable**, `sudo -u nobody test -w
/opt/homehub/stack/docker-compose.yml` **writable**. `stack/.env` carries
`OAUTH2_PROXY_CLIENT_SECRET`, `TECHNITIUM_ADMIN_PASSWORD`,
`FINANCE_ACTUAL_PASSWORD`, `FINANCE_TRACKER_FEED_TOKEN`, `CLOUDFLARE_API_TOKEN`
and more; a writable `docker-compose.yml` on a box that runs it as root is a
straightforward **local privilege escalation** — any local account, or any
compromised container with a host bind-mount, can add a privileged service or a
host mount and own the machine at the next bring-up.

**THIS IS THE SAME FAMILY AS OI-19 AND THE GITIGNORED-PAYLOAD LEAK: a latent
defect present in every artifact this repo has ever produced, invisible to every
static check, surfaced by an install.** It is worth saying plainly why it
survived: **nothing in this repo had ever looked at a mode.** Every assertion
here reads text — a substituted hostname, a disk pin, a `BUILD_PROFILE` marker,
a soname table — and a permission bit is not text. Three suites, a full sim and
multiple ISO builds all passed over it.

**NOTHING WAS MALFUNCTIONING.** The chain is three honest links:

| link | what it does | what it therefore records |
|---|---|---|
| the worktree, as WSL sees it | DrvFs/9p has no metadata for an NTFS mount | `-rwxrwxrwx` for **every** file on `/mnt/c` |
| `git ls-files` -> `tar` -> `genisoimage -rock` | Rock Ridge preserves mode/uid/gid faithfully | 311 group- or world-writable entries in the ISO payload |
| late-command 3's `cp -a` | preserves mode/uid/gid faithfully | `/opt/homehub` exactly as the ISO said |

The mode was never **decided** anywhere. It is decided now, in three places,
each with a job the others cannot do.

**1. THE BUILDER — `normalize_payload_modes` (`vmtest/lib/common.sh`), the
primary fix.** One table, applied to the whole staged payload after every stager
and before the ISO is written, on both images:

```
directories                 0755      site/  (materialised config)  0700
files beginning `#!`        0755      every file in site/           0600
every other ordinary file   0644      stack/.env, any *.creds       0600
```

**The executable bit comes from the `#!` line, not from git's index, and that is
a deliberate refusal of the obvious source.** `core.filemode` is **false** on
this Windows checkout, so only **10 of the repo's 58 scripts** are recorded
`100755` — `stack/provision/*.sh`, `stack/samba/library-guard.sh` and
`stack/autoinstall/firstboot.sh` are all `100644` in the index. Keying off it
would have shipped them non-executable: the mirror of the bug being fixed. A
shebang is in the file's own content, cannot drift out of step with a checkout
setting, and gives a new script the bit for free. The precedent for stating modes
as a table and then asserting them is `assert_wall_artifact_contract`, which
reads `-rwxr-xr-x app/wall-shell` and `4755 root:root chrome-sandbox` straight
out of the shell tarball rather than trusting the extraction.

**2. THE ISO WRITER — because on the default build host the builder fix CANNOT
BIND, and that is the finding underneath the finding.** WSL mounts a Windows
drive without metadata, so `chmod 0600 f` **succeeds, reports nothing, and
changes nothing** — verified here. `OUT_DIR` defaults to `vmtest/.out` (on C:)
and the README recommends `/mnt/d`, so the **default and recommended build paths
are both ones where a staged mode cannot be stored at all.** A normaliser that
assumed otherwise would print a tidy summary and ship the same ISO. So
`normalize_payload_modes` **probes** the staging filesystem and hands enforcement
on: `write_seed_iso` switches `-rock` to **`-r` (rational rock: uid/gid 0, read
bits set, execute bits normalised, every write bit cleared)**, and
`build-repacked-iso.sh` adds `-chmod_r go-w` + `-chown_r 0` + `-chgrp_r 0` scoped
to `/deploy-payload` (subtractive, so it is unconditional and can only ever
remove a bit). `assert_iso_payload_modes` then judges **the artifact**, which is
the only layer that can speak on this host.

**3. INSTALL TIME — hub late-command 3b, wall late-command 3a.** Not merely
defence in depth, given (2): a payload can still arrive from a build route that
never applied the policy. Both take ownership (`chown -R 0:0` — `-rock` preserves
the **builder's** uid, and the ordinary builder uid 1000 is the hub's `hub` and
the panel's `panel`; this is OI-18's ownership lesson generalised), clear
group/other write, tighten `site/` to 0700 and its files plus `stack/.env` and
any `*.creds` to 0600, and then **re-check and refuse the install** if anything
group- or world-writable survives. **The wall's step is ordered BEFORE the shell
untar on purpose:** a recursive chmod after it would strip `chrome-sandbox`'s
setuid bit and leave a permanently dark panel whose `[ -x ]` is still true.
It does not fight OI-18 or 4b — 4a/4b still delete or reinstall the staged
credential copies; this narrows the window before they run and agrees with them
about `site/` being 0700.

**THE GUARD THAT WOULD HAVE CAUGHT IT.** `assert_payload_modes` **fails the
build** on any group- or world-writable staged path, and has exactly two
outcomes, never a silent third: judge the tree, or — when the filesystem cannot
carry modes — require that the ISO layer is armed and let
`assert_iso_payload_modes` judge the result. New cases in
`vmtest/test-hub-seed.sh` (5) and `vmtest/test-wall-builder.sh` (8, three of them
static, including that the wall's chmod is ordered before the untar).

**PROVEN TO BITE, and it bit its own author first.** With
`normalize_payload_modes` stubbed out in the sandbox checkout the hub build now
refuses — *"the staged deploy payload has 168 group- or world-writable path(s)
out of 218"* — and goes green again when it is restored; the wall does the same.
The first version of that stub run exited **141 with no message at all**:
`find … | head -n 10` under `set -o pipefail` SIGPIPEs `find`, so the refusal
fired hardest as an unexplained exit. That is the third time this exact shape has
cost this repo something (`assert_electron_runtime_deps`,
`stage_wall_site_into_payload`); both mode checks now count and sample in one
`awk` pass.

**MEASURED, before and after, on a payload staged the way a build stages one:**

| | before | after |
|---|---|---|
| staged tree (ext4) | 172 of 220 paths group/world-writable | **0 of 220** |
| `stack/docker-compose.yml` | 0777 | 0644 |
| `stack/provision/provision-samba.sh` | 0777 | 0755 |
| `site/` | 0777 | 0700 |
| `site/.env`, `site/cifs.creds`, `stack/.env` | 0777 | 0600 |
| seed ISO built from a **DrvFs** tree | 311 group/world-writable entries | **0** (`-r`; `dr-xr-xr-x` / `-r-xr-xr-x`, uid/gid 0) |

**WHAT IS PROVEN AND WHAT IS NOT.** Proven: the staged-tree policy, the
assertion (by stub), the recorded modes of a freshly written seed ISO on the real
DrvFs path, and both install-time steps **executed** against a fake `/target`
deliberately staged 0777 (0 writable paths left, `.env` `600:root`, `site/` 0700,
`*.creds` 0600 — plus the refusal, forced with `chattr +i`). **Not proven: no ISO
has been rebuilt and no box has been installed since the fix** — the Owner runs
the verification build. Suites: `test-hub-seed.sh` **35 passed, 0 failed, 0
skipped**; `test-wall-builder.sh` **97 passed, 0 failed, 0 skipped**;
`python scripts/check.py` PASS; `scripts/validate_config.py` ALL PASSED.

**ONE OUT-OF-LANE FIX, recorded rather than smuggled.** `test-wall-builder.sh`
section 2 (the Electron dependency gate) invoked the builder with **no
`WALL_SHELL_DIST`**, so it resolved the sibling repo relative to the temp
sandbox, found nothing, and refused for *"no shell artifact"* instead of the
reason under test. On a machine without the private sibling the whole section
SKIPS; on one with it, both cases **FAIL — at HEAD `718b092`, before this
change** (reproduced: 83 passed, 2 failed). So that gate — *an Electron bump that
needs a library this image does not install* — was unexercised on both kinds of
machine. Fixed by passing `WALL_SHELL_DIST=$DIST_REAL`.

**FOR THE OWNER — three consequences, none needing a ruling but all worth
knowing:**

1. **`vmtest/build-seed.sh` and `build-wall-seed.sh` now require `xorriso`**, not
   just one of `genisoimage`/`xorriso`, because `assert_iso_payload_modes` reads
   the burned image with it (it is the only lister that prints whole paths, and
   naming the offender is most of a refusal's value). Both are already in this
   repo's documented install line.
2. **On the DrvFs build path secrets arrive `0444` in the ISO, not `0600`** —
   `-r` sets all read bits. That is not a regression (they were `0777`), the
   install-time steps tighten them to `0600 root:root` immediately after the
   copy, and late-command 4b then installs the real ones `0600 root:root`
   anyway. A build staged on a Linux filesystem keeps the precise `0600` end to
   end. The clean way to get that on Windows is `[automount] options="metadata"`
   in `/etc/wsl.conf` — a host setting, deliberately not something the builder
   demands.
3. **`/opt/homehub/stack/.env` is now `0600 root:root` on a SIM box too.** A
   production box already had that (4b installs it so), so this only makes the
   sim match production — but `docker compose` in `/opt/homehub/stack` must now
   be run as root there, as it always had to be on the real hub.

---

### DRIVER — G1 — Round 1 — 2026-08-04 (A19 is buildable at last, and finding out what it needed found two more defects)

**WHAT THIS ENTRY IS.** The wall gate's §5 said A19 was blocked on "wiring
between the two VMs: the Internal switch, static addresses, `WALL_HOST`
overridden, `PANEL_IP` set, and the sim tracker definitions seeded". Every item
on that list is now delivered *by the ISOs and by tracked scripts*. Working out
what each one actually required surfaced **two defects that no amount of review
would have found**, because neither is reachable until a client has to talk to a
gate hub — and nothing ever had.

**DEFECT: THE KIOSK SITE HAS NEVER SERVED TLS, ON ANY GATE VM, AND CADDY WAS
HEALTHY THE WHOLE TIME.** The sim `DOMAIN` is `vmtest.sim.invalid`. `.invalid`
is reserved by RFC 6761 precisely so that it can never resolve publicly, which
also means no ACME challenge over it can ever be validated. The real Caddyfile's
only global option is `email {$ACME_EMAIL}`, so **every** site on a gate VM —
including `{$WALL_HOST}:{$WALL_PORT}`, the one A19 exists to render — has been
retrying a certificate it can never get, and failing every handshake for want of
one.

Nothing noticed because **every V3 gate asserted container health**, and Caddy is
genuinely healthy: the process is up, its config loaded, its healthcheck passes.
It is serving nothing. That is the same shape as `/healthz` on the tracker and
`exit 0` after a failed copy — a green that answers a question nobody was asking.

The fix was sitting in the repo. `sim/caddy/Caddyfile.sim` has carried
`local_certs` since WI-10.14 as a documented REAL-vs-SIM delta; the **installed**
hub simply never inherited it, because the sim compose file forks the Caddyfile
and `render_seed_tree` does not. `apply_sim_caddy_local_certs` now applies that
one delta to the payload copy on the sim path, asserts it landed **inside the
global block** (a stray `local_certs` in a site body is a parse error and a
restart loop), and refuses if the tracked file ever declares it — a hub minting
its own certs for a publicly-resolvable name is a browser warning on every
household device.

**AND THE CONSEQUENCE IS A STATED NON-PROOF, NOT A SECOND FIX.** Nothing can put
that internal root into the wall image: it does not exist until the hub's first
boot, which is after the panel's ISO is written. So the sim `WALL_APP_CMD` gains
`--ignore-certificate-errors` beside `--disable-gpu`, **this gate proves nothing
about TLS trust**, and the builder now **refuses** a production `wall.env`
carrying that flag — the only way it reaches a real panel is a copy-paste from a
sim one, and the origin it would stop validating is the one that injects this
household's identity header.

**DEFECT: THE GATE HUB'S TRACKER HAS NO ITEMS, AND A WALL CANNOT SHOW THE
DIFFERENCE.** A19's whole assertion is that `library-mounted` and
`backup-drive-mounted` report RED **on the panel's screen**.
`sim/tracker-seed/definitions/drives.md` is the only committable thing that
declares those two ids — and it was mounted only by `docker-compose.sim.yml`,
the *container* sim. An installed hub runs `stack/docker-compose.yml`, whose
tracker had no seed at all. Against an empty `/data` there is nothing to be red;
the container still reports healthy because the healthcheck probes `/healthz`,
which is data-free by design (SR-040); and on a wall **"no red drive check" and
"the drive check is green" are the same picture.** Measured 2026-08-01 in a
different guise: the gate VM's tracker ran healthy for hours with an empty
`/data` and its nightly run failing every night.

`TRACKER_SEED_DIR` + `TRACKER_SEED_SRC` are now knobs, and both are **inert by
default**: blank `_DIR` means the entrypoint passes no `--seed`, and the default
`_SRC` (`stack/tracker-seed/`) ships a README and deliberately **no
`definitions/`**, so a box that sets the env var by mistake still seeds nothing.
A real household's definitions arrive with the person, not from a fixture.

**THE LAB ITSELF — `apply_sim_lab_netplan`, and the three things that make a
second NIC harder than it sounds.** Both images ship one network stanza,
`ethernets: any-eth: match: name: "e*"` with DHCP, and keeping ONE match for
production and the VM is deliberate. It cannot express the lab: two NICs both
match `e*`, so netplan would DHCP the Internal leg too, where nothing answers.

1. **The legs can only be told apart by MAC.** Both come up as `eth0`/`eth1` in
   an order VMBus decides, so both stanzas match on `macaddress:` and
   `New-HomeHubVm.ps1` sets those MACs **statically** — a dynamic MAC changes on
   recreate, so the netplan that matched it yesterday matches nothing today.
   That is two places holding the same three values, which is drift waiting to
   happen, so the builder writes `$OUT_DIR/a19-lab.env` and `Start-A19Gate.ps1`
   reads it. Nobody retypes a MAC. A build with no lab knobs **deletes** any
   stale manifest rather than leaving one that outlives the ISO it describes.
2. **The Internal switch has no internet and the installer needs one.** Each VM
   keeps a Default Switch leg purely so `apt` can fetch 40 packages; the lab leg
   carries **no gateway**, so the default route stays where the packages are.
3. **An unscoped nameserver on the lab leg breaks the install.** The panel must
   resolve `WALL_HOST` through the hub's Technitium — but pointing it there for
   *every* name sends `archive.ubuntu.com` to a box that does not exist yet.
   `nameservers.search` makes it a **routing** domain in systemd-resolved, so
   only lab names go to the hub. `SIM_LAB_DNS` without `SIM_LAB_SEARCH` is
   refused rather than accepted.

**NO OVERRIDE WAS NEEDED FOR `WALL_HOST`, and that is worth recording** because
both the plan and the handoff said one was. The panel's sim default is
`wall.vmtest.sim.invalid` and the hub's sim `DOMAIN` is `vmtest.sim.invalid`, so
`EXTRA_SUBDOMAINS=wall` makes Technitium authoritative for exactly that name.
The containment property is *stronger* than overriding to `wall.home.arpa`: an
`.invalid` name can resolve inside the lab and nowhere else, so a sim panel still
cannot reach anything real.

**PROVEN, AND WHAT IS NOT.** Both ISOs were **rebuilt** — the first build from
`79f4ed5`, which closes the "no ISO has been rebuilt" half of OI-21 for the
*artifact*: `assert_iso_payload_modes` passed on both, "nothing under
/deploy-payload is group- or world-writable". The rendered netplan, the staged
`.env`, the `local_certs` placement, the seed fixture in the payload and the
panel's two flags were all read back off the built trees. Suites:
`test-hub-seed.sh` 35 to **47 passed, 0 failed, 0 skipped**;
`test-wall-builder.sh` 97 to **107 passed, 0 failed, 0 skipped**;
`scripts/check.py` PASS; `scripts/validate_config.py` ALL PASSED.

**NOT proven: nothing has been installed.** No VM has booted either ISO. Both
new defects are fixed the way the last three were — in the builder, with a
refusal — and both stay INSTALL-UNVERIFIED until OI-22 runs. That run is the
Owner's: it needs elevation and about three hours, and it is the single thing
that speaks to OI-17, OI-19, OI-20, OI-21 and the hostname fix at once.

**ALSO CLOSED, AND SMALL:** the screenshot watcher. The scratch
`watch-wall-vm.ps1` had a hard 90-minute deadline against a ~2h10m install,
exited quietly, and left a console that simply stopped updating — silence
indistinguishable from a hung VM, from a process an unelevated session cannot
restart. `vmtest/Watch-VmConsole.ps1` is tracked, budgeted in hours,
`-UntilOff`-capable, and **every** exit path writes why it stopped, including
"the install may STILL BE RUNNING; this is not evidence of a hung VM".

---

### DRIVER — G1 — Round 1 — 2026-08-04 (A19 RAN: the shell painted, and it took two hand-patches to get there)

**THE PANEL PAINTED.** A red wall reading `100%`, with an overlay naming both
drive lanes by their real reasons — *"Library drive mounted: /srv/library is NOT
MOUNTED … · Backup drive mounted: /mnt/backup-drive is NOT MOUNTED …"*. Captured
with `grim` from inside the cage session and saved to
`D:\vmtest-out-wall-a19\panel-a19.png`. Nothing has ever rendered the tracker's
UI on a panel before; OI-17's narrow point is answered.

**AND IT IS THE STRONG FORM OF THE ASSERTION, not just a red screen.** `140785e`
warned that a red screen proves nothing on its own, because any stale habit
reddens it. `/api/today` carried the per-ITEM colours at the same moment:

```
ambient: red   score: 100
  library-drive-present        red      Library drive mounted
  backup-drive-present         red      Backup drive mounted
```

**PROVEN BY THE IMAGE, no patching, on two clean installs:**

- two VMs on ONE Internal switch at static, MAC-pinned addresses — the lab leg
  came up during the *installer* phase, which is the first real-boot evidence
  `apply_sim_lab_netplan` has;
- **the kiosk site served TLS** — cert issued by `CN = Caddy Local Authority -
  ECC Intermediate`, `SAN: DNS:wall.vmtest.sim.invalid`. That site has never
  served a byte on a gate VM before (this session's earlier entry, defect #14);
- **the `/32` guard**, answering `403 wall: panel only` to the hub itself and
  `200` to the panel — §3 step 7's first assertion, and its third too:
  `ss -ltn` shows `10.99.7.10:8443`, not `0.0.0.0:8443`;
- **the tracker seeded itself from the fixture** on the panel's FIRST request —
  defect #15's fix, working end to end;
- `cage -d -- /opt/wall-panel/app/wall-shell --disable-gpu
  --ignore-certificate-errors`, Electron 38.8.6 up with its zygotes and
  `chrome-sandbox`, `IF-005: ldd resolves every library`;
- **defect #13 on BOTH images, on installed boxes at last: 0 group- or
  world-writable paths** under `/opt/homehub` (was 230) and `/opt/wall-panel`
  (was 215); `stack/.env` `-rw------- root:root`.

**NOT PROVEN — two hand-patches, and the gate does not pass until they are
gone.** Same posture as 2026-08-03's defects 10-12: this proves the FIXES, not
the IMAGE.

---

#### DEFECT: `.invalid` is unresolvable BY DESIGN, and the design is CLIENT-SIDE

The A19 lab was built on `wall.vmtest.sim.invalid` on the reasoning that
`.invalid` can never resolve publicly (true), that Technitium authoritative for
it inside the lab therefore resolves it locally (also true — `dig @10.99.7.10`
returned the address), and that this was *stronger* containment than a
real-looking name (true as well). The panel still died on
`ERR_NAME_NOT_RESOLVED`.

**systemd-resolved implements RFC 6761 §6.4 and synthesises NXDOMAIN for
anything under `invalid` without ever querying the link's DNS server.** The
server was never the problem; the stub refuses to ask. Measured on the panel:

```
dig wall.vmtest.sim.invalid @10.99.7.10   -> 10.99.7.10      (Technitium: correct)
dig wall.vmtest.sim.invalid @127.0.0.53   -> (empty)         (the stub the shell uses)
curl --resolve …                          -> 200 + real JSON (DNS bypassed)
```

So the property that makes `.invalid` safe as a sim default — nothing will ever
resolve it — is precisely what makes it unusable as a lab name. Both follow from
the same sentence in the RFC, which is why the reasoning felt sound and was not.
**And the plan had said so:** *"`WALL_HOST` MUST be overridden — the sim default
is deliberately unresolvable."* That instruction was overridden on the strength
of an argument about the server, in a failure that lives in the client.

Fixed as `assert_stub_resolvable`, which refuses a lab build whose search domain
or `WALL_HOST` sits under `.invalid`, `.localhost` or `.local`, and names
resolved and the RFC in the message — a refusal that only says "no" trains the
next person to try `.local`. **The non-lab default stays `.invalid`:** a lone sim
panel must still be unable to reach anything real, and only a lab has a reason
to give that up. `.sim` is the replacement, which is what `sim/.env.sim` has used
since WI-10.14.

---

#### DEFECT: a SIM hub cannot report either drive lane, so A19's assertion was unreachable

Both lanes detected the fault perfectly and told nobody:

```
[library-guard] UNHEALTHY: /srv/library is NOT MOUNTED — the library drive is absent…
[library-guard] NAGLIGHT_FEED_URL unset — journal only (red; check id would be 'library-mounted')
[backup-drive-health] no /etc/homehub-backup/backup.env — backup not provisioned, nothing to check
```

`library-guard.sh --report` takes its feed configuration from
`/etc/homehub-backup/backup.env`, and a **sim** build installs none — it is a
production `site/` file. The library lane therefore logs the right band and the
right check id into a journal nobody reads; the backup lane exits before it
checks at all.

**NEITHER IS A PRODUCT DEFECT, and this entry nearly said they were.** Both are
correct for an unprovisioned box, and on a real hub the materialised
`backup.env` carries `NAGLIGHT_FEED_CONTAINER=tracker`, which runs the POST
*inside* the container against its own loopback — necessary because the tracker
is bridge-only by ratification (D2: no host publish, since trusted headers are
forgeable if the port is open). Measured on the gate hub, confirming that design
rather than contradicting it: from the host, `127.0.0.1:8787` is refused,
`tracker:8787` does not resolve, `docker port tracker` is empty. The mechanism
was already right; only a SIM equivalent was missing.

`render_sim_gate_backup_env` renders one into `deploy-payload/sim-gate/` on lab
builds only, and firstboot installs it `0600 root:root` — **refusing outright if
a real `backup.env` already exists**, because a gate fixture on a provisioned hub
would repoint a household's drive reports at a test identity.

**`NAGLIGHT_USER` is the load-bearing field.** The tracker runs multi-user, so a
report attributed to anyone but `PANEL_USER_SUB` lands in a different user's data
directory and the panel never sees it — a red lane posted to the wrong user is
indistinguishable from no lane at all, which is the exact failure being fixed.
The value is read back out of the RENDERED sim `.env` rather than from the
overrides, so the two cannot disagree, and a lab build with no `PANEL_USER_SUB`
is refused.

---

#### THE GUARD THAT WAS DROPPED, AND COST AN INSTALL THE SAME NIGHT

`recreate-hub-vm.ps1` **refused to boot an ISO older than the newest commit** —
"the mechanised form of *verify the artifact, never the exit code*".
`Start-A19Gate.ps1` replaced it and only **printed** the build timestamp. Hours
later, with the builder fixed and the ISO not yet rebuilt, `-Stage Hub` booted
the pre-fix image and began a 25-minute install of the defect it was meant to
test. A timestamp a human is expected to read and compare is not a check.

Restored, and judged against the newest commit touching **`vmtest/` or
`stack/`** rather than any commit — a docs-only change does not invalidate an
ISO, and refusing on one would train the operator to pass `-AllowStaleIso` every
time, which is how a guard becomes a keystroke.

**Also:** `Set-VMFirmware -SecureBootTemplate MicrosoftUEFICertificateAuthority`
succeeded on one VM and, twenty minutes later on the same host with the same
parameters, failed on the next with *"matches none of the secure boot
templates"*. The NAME LOOKUP is the flaky part, not the template, so the call is
now tried by name and then by the same template's well-known id — one thing
attempted two ways, announced when it takes the second route. Secure Boot stayed
ON for the panel (`shim-signed` and `grub-efi-amd64-signed` were installed), so
there is no `-DisableSecureBoot` delta to record.

---

**SUITES:** `test-hub-seed.sh` 47 → **54 passed, 0 failed, 0 skipped**;
`test-wall-builder.sh` 107 → **110 passed, 0 failed, 0 skipped**. One existing
wall case had to be corrected rather than the guard: section 4c asked for a lab
build on the `.invalid` default, which is now refused — the case predated the
lesson.

**WHAT THIS RUN STILL DOES NOT PROVE:** TLS trust (the panel runs with
`--ignore-certificate-errors`), Wi-Fi, either CIFS mount, and — until the
rebuild is booted — that the two fixes above are delivered by the image rather
than by hand. `wall-sync` and `wall-sync-frame` failed on the panel, which is
correct: both UNCs are `.invalid` and media is out of scope for this gate.

---

### DRIVER — G1 — Round 1 — 2026-08-05 (A19 PASSED, unaided, and three open items close — not five)

**EVERY "Done when" ROW IS MET, WITH NO HAND-PATCHING.** The re-run booted both
ISOs built from the committed tree at `0c735f2` and needed nothing done to
either box.

| §3 Done when | |
|---|---|
| Two VMs boot from tracked-script ISOs, **no hand-patching** | **YES.** `/etc/hosts` on the panel is clean; `/etc/homehub-backup/backup.env` was installed by firstboot, which logged that it did. |
| A screen capture of the **panel** shows both drive checks **red** | **YES** — `D:\vmtest-out-wall-a19\panel-a19-GATE.png`, captured with `grim` from inside the cage session. |
| The three guard assertions | **ALL THREE.** See below. |
| The record | this entry. |

**DEFECT #16 IS FIXED IN THE IMAGE, and the measurement is the one that matters
— through the STUB, not against the server:**

```
resolvectl query wall.vmtest.sim   ->  10.99.7.10   -- link: eth1
getent hosts wall.vmtest.sim       ->  10.99.7.10 wall.vmtest.sim
GET /            -> 200
GET /api/today   -> 200
```

`-- link: eth1` is the part worth reading: the answer came back over the **lab
leg**, via the scoped search domain, exactly as `apply_sim_lab_netplan` intends.
The previous run's `ERR_NAME_NOT_RESOLVED` is gone and no line replaced it.

**DEFECT #17 IS FIXED IN THE IMAGE.** `firstboot` logged *"SIM GATE: installed a
test backup.env so the two drive-presence lanes REPORT"*, the file is
`-rw------- root:root`, and both lanes posted without being asked. `/api/today`
carried, per ITEM:

```
ambient: red   score: 100
  library-drive-present      red    Library drive mounted
  backup-drive-present       red    Backup drive mounted
```

**THE THREE GUARDS (§3 step 7), the half that matters because it was ratified as
security-critical:**

| assertion | result |
|---|---|
| a request from an address that is not `PANEL_IP` gets 403 | **PASS** — `403 wall: panel only` from the hub itself; `200` from the panel |
| a client-supplied `X-Forwarded-User` is **replaced**, not honoured | **PASS** — a forged sub returns byte-identical data to the baseline, so the injected identity won. Repeated the test with the header sent **twice**, which is the shape a single `delete` would miss: also identical. `X-Forwarded-Email` (plain delete, no injected value): identical. |
| `WALL_PORT` reachable only on the LAN leg | **PASS** — `ss -ltn` shows `10.99.7.10:8443`, not `0.0.0.0:8443` |

The forgery test is a real measurement of a behaviour a reader cannot see: Caddy
applies header ops in a fixed order regardless of writing order, and
`header_up <Name> <value>` is a SET, which replaces every existing value. That
reasoning has been in the Caddyfile's banner since WI-10.14 and has now been
checked against a live panel rather than against the source.

**DEFECT #13 ON BOTH INSTALLED BOXES:** 0 group- or world-writable paths under
`/opt/homehub` (was 230) and `/opt/wall-panel` (was 215); `stack/.env`
`-rw------- root:root`; `/opt/homehub/sim-gate` `drwx------ root:root` and
unreadable to the unprivileged user, which is `0c735f2` proven on a box.
`chrome-sandbox` survives `-rwsr-xr-x` after NTFS → ISO → tar → ext4.

---

#### A CORRECTION: this run closes THREE open items, not five

Earlier entries — and the handoff — said one install would speak to OI-17,
OI-19, OI-20, OI-21 and the hostname fix at once. **That was wrong, and the
reason is structural rather than incidental: three of those five can only be
exercised by a PRODUCTION build, and A19 is a SIM gate.**

| item | status after this run |
|---|---|
| **OI-17** — the wall ISO boots and the shell paints | **CLOSED.** It painted, unaided. |
| **OI-21** — `/opt/homehub` world-writable | **CLOSED.** Measured on two installed boxes. |
| **OI-22** — run the A19 gate | **CLOSED.** |
| **OI-19** — the hub's site-staging late-command | **STILL OPEN.** A sim build has no `site/`, so 4b took its documented no-op branch. The *refusal* path — a `production` marker with no `site/` — remains unexercised on real media, which is exactly what OI-19 is. |
| **OI-20** — the caddy-readability assertion on `config.json` | **STILL OPEN**, and for the same reason: the check is conditional on a production `config.json`, and a sim build ships none. |
| the production meta-data hostname fix | **STILL UNVERIFIED** — the sim path stamps `homehub-vmtest` by design, so this run says nothing about the production branch. |

**What actually closes those three is a PRODUCTION build**, which needs
Personal's materialiser to emit a full `site/` — and that is blocked on
**C17**, one `smbpasswd -a share` at the AWOW. Not a design gap; a task at a box.

---

**STILL NOT PROVEN, and every one of them deliberate:**

- **TLS trust.** The panel runs `--ignore-certificate-errors` because the hub
  serves from an internal CA whose root cannot exist before the panel's ISO is
  written. The gate proves the render path, not the trust path.
- **Wi-Fi** — `macaddress: permanent`, powersave-off, and the DHCP reservation
  the `/32` is keyed to. Hardware-only (C7), by construction.
- **Either CIFS mount.** `wall-sync.service` and `wall-sync-frame.service` are
  both `failed` on the panel, which is **correct**: both UNCs are `.invalid` and
  media is out of scope for this gate. Nothing has ever been mounted with either
  credential; that is OI-18's standing gap and this run does not touch it.
- **A production panel or hub.** Everything here is a sim image.

### DRIVER — G1 — Round 1 — 2026-08-07 (an installed hub, interrogated: sudo could never have worked, and the offline install stopped at the install)

**The first lab run whose VM outlived it.** HomeHub's endpoint gained a
`Hub-Keep` variant, so for the first time an installed hub was still running
when the launcher finished and could be asked questions instead of being
destroyed with its evidence. Three defects came out of one box, and none of them
was reachable by review.

**1. `sudo` could never have succeeded, on either image.** `identity.password`
is `"!"` — a locked hash, correct for a key-only box — and neither image ever
created a sudoers drop-in. So `sudo` demanded a password that cannot exist:

```
sudo: a password is required
```

No `apt`, no `systemctl`, no `/etc` edit, and `healthcheck.sh` could not read
its own root-only `.env`. Cockpit and the console were dead for the same reason,
both being PAM against the same locked hash. **This is the standard cloud-image
posture with one of its three parts missing** — Ubuntu's `cloud.cfg` ships
`lock_passwd: True` *together with* `sudo: ["ALL=(ALL) NOPASSWD:ALL"]`, and
subiquity's `identity:` block has no way to express the second, so adopting the
posture silently dropped it. Fixed in both images at late-command 3c-bis,
validated with `visudo -cf` before installing because a malformed sudoers file
on a locked-password box is the no-way-in state that cost a GRUB rescue on
2026-08-06.

Worth recording plainly: **on the hub this changed no exposure.** `hub` is in
the `docker` group, and the docker socket is root by design — demonstrated on
the live box by reading the 0600 root:root `.env` from a container as uid 0,
with no password. What the drop-in adds is auditability, since `sudo` journals
every command and `docker run` does not. On the **panel**, which has no docker
group and no Cockpit, it is the difference between maintainable and
reimage-only: `systemctl reboot` — the one action unattended-upgrades
periodically requires — was not remotely performable at all.

**2. The offline install was defeated by configuration, one step past the
install.** `export-images.sh` hardcoded `PROFILE_ARGS=(--profile ntfy)` while
the materialised hub `.env` carried
`COMPOSE_PROFILES=ntfy,immich,immich-ml,jellyfin,finance-auditor`. Nine images
baked; fifteen needed. First boot went to `registry-1.docker.io`, the
`jellyfin` pull failed, `compose up -d` is all-or-nothing, and
`homehub-firstboot.service` exited 1 — so **DNS, Caddy, the tracker and Actual
never started because one OPTIONAL image was unreachable.** Confirmed from
outside: `:5380`, `:3001` and `:8081` all refused.

Note what was *not* wrong: the refusal machinery was sound and always had been
(a missing local-only image and an unpullable tag are both `die`, not warnings).
It was being asked the wrong question. It now derives the profile set from the
`.env` it is bundling, so drift is a refused build. This closes OI-7(a) against
its original interpretation.

**3. `Wait-GuestAddress` reported failure about a box that was up** (HomeHub's
launcher; recorded here because it is what made 1 and 2 look like a dead
install). It watched only the host neighbour table for the VM's MAC — an
ARP cache that fills when the host *talks* to the guest, and nothing was talking
to it. Sixty minutes of nothing, about a hub answering ping and ssh at its
reservation throughout. It now probes tcp/22 at the expected address first
(15 ms, measured) and keeps the neighbour scan as the fallback it should always
have been, for the DHCP-gave-a-different-address case.

**What this run did prove**, and it is the assertion the whole 2026-08-06 effort
was for: **8004 MB installed with the network adapter disconnected**, and SSH
accepting the operator key unattended afterwards.

### DRIVER — G1 — Round 1 — 2026-08-08 (the panel installs at last; and the mount check could never have passed)

**Three lab runs, both machines installed offline, five defects.** The
2026-08-07 entry above covers the first two; this one closes the day.

**THE PANEL INSTALLED, END TO END, FOR THE FIRST TIME.** `wall-firstboot.service`
Result=success, the shell artifact present at `/opt/wall-panel/app/wall-shell`, a
kiosk session running, sshd enabled — and it did it with the VM's network adapter
disconnected, finishing **4.8 minutes** before the cable went back in. The hub
did the same with 1.1–1.2 minutes of margin. `Assert-OfflineInstall` has now
returned PASS on both targets, which it had never done for either.

**AND `sudo` WORKS**, verified on a rebuilt image: `SUDO-OK: the drop-in landed`.
Both boxes are remotely maintainable rather than merely readable.

**DEFECT: `provision-mounts.sh` could never find a data drive.** It tested
`[ -e "$dev" ]` where `$dev` is the fstab line's FIRST FIELD — `LABEL=Library`, a
mount spec, not a path. Always false. So the mount below it was never attempted,
and `provision-samba` — which refuses to export an unmounted path, correctly —
killed firstboot with a FATAL. **Every first boot, on every box, since the fstab
moved to `LABEL=` for A23.** Measured both directions on a live hub:
`[ -e "LABEL=Library" ]` false, `blkid -L Library` → `/dev/sdb2`, and
`mount /srv/library` succeeded instantly by hand.

It hid because systemd's fstab-generator mounts these on the NEXT boot anyway, so
the drives are up by the time anyone logs in — only firstboot, the one pass that
provisions Samba, ever saw them missing. **A lab run that reconnected LATE saw
mounted drives and passed; the run that reconnected promptly did not.** The
defect was masked by the timing of the observer, and fixing the heartbeat is what
exposed it. `resolve_mount_spec` now handles LABEL=/UUID=/PARTLABEL=/PARTUUID=
and bare paths, via `blkid` (which probes devices rather than waiting on udev's
symlinks) with a bounded wait — the real hub's drives are USB and slow, which the
fstab already admits with `x-systemd.device-timeout=15s`.

**DEFECT: the image bake read the wrong .env, twice.** First the profile list was
hardcoded `--profile ntfy` here; then, once that was fixed, `Build-VentoyStick.ps1`
called this script with no `--env-file` at all, so it resolved against
`stack/.env.example` (COMPOSE_PROFILES empty) while the ISO shipped a
materialised `.env` enabling five. Nine images baked, fifteen needed, and the six
missing were exactly the six that failed to pull. `docker compose up -d` is
all-or-nothing, so one unreachable OPTIONAL image stopped DNS, Caddy, the tracker
and Actual from starting at all.

Twice in one day, **the thing that DECIDED the image set was not the thing that
RAN the box.** `export-images.sh` now writes `profiles.stamp` recording which
`.env` it was asked about, and `stage_images_into_payload` compares it against
the `.env` staged beside it, naming any enabled-but-unbaked profile. It warns
where the apt stager dies, deliberately: a short image set has a degraded mode
and refusing would break every deliberately-narrow bake (the sim, the wall
target, `EXTRA_PROFILES`). What it must never be is silent, which it was.

Consequence to weigh, recorded for the Owner: the hub ISO went from 3.8 GB to
**5.8 GB**, because `immich`, `immich-ml`, `jellyfin` and `finance-auditor` are
genuinely enabled in `config.homehub.psd1` and are now genuinely baked. That was
always the configuration; it simply never shipped.

**NOT YET RUN:** the `provision-mounts` fix has never executed inside a firstboot.
The next `-Stage All` is what settles it.

### 2026-08-09 — three defects from lab run `20260809-145118`, and a static guard

The run failed **6 checks of 13**. Two defects, both in `firstboot`, plus a
third found while diagnosing them. All three fixed here; all three proven on the
live lab hub before any rebuild.

**1. `homehub-firstboot.service` SIGTERMed itself at step 1b.**

```
20:21:17  1b: carving /var/lib/docker onto its own LV (15993 free extents)
20:21:17  Logical volume "docker-data" created
20:21:18  Main process exited, code=killed, status=15/TERM
```

The unit declared `Requires=docker.service`, and step 1b must
`systemctl stop docker` to move `/var/lib/docker`. systemd propagates stops
across `Requires=`, so the unit ordered its own death. **Two files each correct
in isolation; the pair fatal** — which is why no reader caught it.

Everything else followed: 0 of 15 image tars loaded → no stack → no Technitium →
no LAN DNS → the panel's firstboot failed too (it resolves everything by name) →
both kiosk interconnect checks failed → `/srv/library` and `/mnt/backup-drive`
never mounted, because the generated fstab is written later in the same script.
**One cause, six failing checks.** Fix: `Wants=` + `After=`. `After=` was always
what delivered "runs after docker is up"; `Requires=` only added "and dies with
it".

**2. The step could not repair itself, which was worse.** Its idempotence guard
asked *"does the LV exist"* — its own output. The kill left a formatted,
unmounted LV, so every later boot said "already exists — nothing to do" and
returned. `homehub-firstboot.service`'s own header warns that a step must ask of
an INPUT, never of its own output; this was that warning made flesh. Re-running
the script — the documented repair for every other step — was the one thing that
could never work. The guard now asks the END STATE (*is `/var/lib/docker`
mounted from that LV*) and **resumes** from wherever the last attempt stopped,
re-running `mkfs` only if the volume has no filesystem.

**3. The hub had no `cifs-utils`.** `packages.list` gave it `samba` (serve) and
`smbclient` (test) but never `mount.cifs` (**mount**). The kernel module ships
in the stock image, so `mount -t cifs` exists and fails misleadingly:
`credentials=<file>` is parsed by mount.cifs, not the kernel, so no credential is
sent (`STATUS_ACCESS_DENIED`); and kernel-side name resolution needs the same
helper's upcall, so a resolvable FQDN gives `Unable to determine destination
address` — printed as **`No route to host`**. HomeHub had spent two runs
concluding that a Mini-serv share was missing, while `smbclient` on the same box
read that share throughout. Same class as the 2026-08-07 `smbclient`-is-not-in-
`samba-common-bin` miss.

**A static guard, so this class fails the build instead of a three-hour run.**
`validate_config.py` check **5b**: no firstboot script may `systemctl stop` a
unit its own unit `Requires=`. Verified both directions — restoring `Requires=`
makes it FAIL with the explanation, the fixed tree PASSes.

**Verified first-hand:**

- `bash -n stack/autoinstall/firstboot.sh` clean; `python scripts/check.py`
  (gate G1) **PASS** — config-validate, registry-integrity, doc-navigability.
- **On the live lab hub, in its broken state:** the repaired unit + script
  resumed correctly — `1b: ubuntu-vg/docker-data exists but /var/lib/docker is
  not mounted from it — finishing the move`, then `1b: /var/lib/docker is now
  36.6G, separate from root`. firstboot ran to `Result=success`, **15 of 15
  images loaded**, the full stack came up healthy, and fstab gained all three
  mounts. After `cifs-utils`, the previously failing
  `mount -t cifs //mini-serv.<domain>/Snapshots -o credentials=…` **mounts**, and
  `backup.sh --dry-run` reaches `ingest: done`.
- HomeHub's `Invoke-LabVerify.ps1 -IncludeExternal` then reported **3 failures
  of 146** (from 5), all three lab-fixture limits rather than product defects.
- The **panel's** media-cache LV (unchanged this round) proved its purpose: the
  43 GB frame overflow that filled run 2's ROOT filesystem now lands in the
  cache LV — root 47%, cache 97%. Contained.

**NOT proven:** none of this has been through a clean image build. The lab hub
was patched in place, so what is demonstrated is *the fixes work*, not *a fresh
install produces them*. The next full lab run is what closes that.

**Also this date, unrelated to the run:** compose + `.env.example` plumbing for
NagLight's one-way traceability mirror (`TRACKER_MIRROR_*`), shipping blank/OFF.
See HomeHub `DECISIONS_RATIFIED.md` → *Ratified 2026-08-09*.

### 2026-08-09 (later) — the nightly backup had never worked: four stacked defects

Chasing one missing directory found four independent faults, **each of which
alone produces the identical error line** `path source missing for …`. The Owner
ruled the behaviour first: *"the setup should include all folders, but tests
should also verify this exact item. A missing folder shouldn't block the backup
of other folders."*

1. **CRLF in the emitted config** (HomeHub `Materialize-Deploy.ps1`). Its comment
   promised LF — *"CRLF or a BOM breaks both on the box"* — and nothing ever
   converted anything; output was LF only because the templates were. A
   multi-line knob value from a CRLF `.psd1` carried `\r\n` through, so
   `backup.env` had exactly 13 CR bytes (the internal breaks of the 14-line
   `BACKUP_SOURCES`) while `.env` and `cifs.creds` had none — which is why it
   read as backup-specific. Every path ended in `\r`, and **`\r` is invisible in
   a log**, so the error named a path that looked correct.
2. **`load_env_file` read only the FIRST of fourteen sets** (`common.sh`). It is
   line-based on purpose (sourcing would expand bcrypt `$2a$14$…`), and stripped
   quotes only when a value opened and closed on one line. Line 1 kept its
   opening quote — naming the set `"media-music`, which also stopped its
   `.exclude=` line matching — and every later line was rejected as a malformed
   key and skipped **silently**, because `continue` on a bad key is correct for
   comments and indistinguishable from this. Now supports multi-line quoted
   values; the bcrypt-literal property is preserved and unit-tested.
3. **No `volume:` source ever resolved** (`volume_mountpoint`). They name compose
   volumes unprefixed (`actual_data`); docker's are project-prefixed
   (`stack_actual_data`). Only visible once (2) let them be read. Resolved via
   compose's own `com.docker.compose.volume` label — **not** a suffix match,
   which was the first attempt and matched both `stack_actual_data` and
   `stack_finance_actual_data`: it would have backed up a finance volume as the
   budget one, silently. A second bug hid here too: `docker volume inspect -f` on
   a missing volume writes an empty line to stdout, so echoing the probe through
   produced `"\n/var/lib/docker/…"` and failed `[ -d ]` for volumes that HAD been
   found.
4. **`NonDocs/Media/Music` was created by nothing.** Step 4a-pre-2 pre-creates
   library dirs only for Docker bind-mount sources; nothing containerised serves
   music (Samba + CIFS to the panel), so no bind mount names it. New step
   **4a-pre-2b** pre-creates every `BACKUP_SOURCES` path, **gated on
   `/srv/library` being a real mountpoint** — it mounts `nofail`, so an
   ungated `mkdir -p` would build a convincing fake library on the system disk
   and hide a missing drive.

**Behaviour change (`backup.sh`), per the ruling.** A missing source now SKIPS
its own set and the run continues; it still posts `ok=false`, still exits
non-zero and still names the set — after protecting the data it could reach.
Previously the first absent path ended the run, so **one directory cost every
other set, including all five Private trees**. Deliberately narrow: only
"source absent" is tolerated; rsync/archive/integrity failures still abort,
because those mean the machinery is broken rather than an input being absent.

**Verified first-hand on the live lab hub, in sequence:**

| | sets seen | verdict |
|---|---|---|
| before | 1 of 14 (`"media-music`) | abort |
| after CRLF fix | 9 path sources, names clean | abort at volumes |
| after parser + volume fix | **14 of 14** | skips `media-music`, archives 13, exits 1 |
| after creating `Music` | 14 of 14 | **dry-run exits 0** |

`bash -n` clean on `backup.sh`, `common.sh`, `firstboot.sh`; the multi-line
parser unit-tested (multi-line value, parsing resumes after it, bcrypt stays
literal, comment-stripping intact). HomeHub's hub suite now reports
**ALL CHECKS PASSED — 92 passed, 9 not proven, 0 failed**, including the new
`every backup source exists: 14 of 14 reachable (TC-H-M10)`.

**Tests added** (HomeHub `verify-hub.sh` + `LAB_TEST_PLAN.md`): **TC-H-M10**
reports a COUNT (`N of N`) rather than stopping at the first failure — it would
have shown "1 of 14" immediately — and **TC-H-M11** covers skip-one-not-all.

**NOT proven:** none of this has been through a clean image build.

### 2026-08-27 — the GUI is removed, because IceDrive never needed it

The Owner: *"GUI can be removed... from the image and from the box, along with
the auto-desktop startup. The HomeHubDesktop can stay with clear comments that
this is no longer required."* And, on the credential: *"ideally that just
becomes another secret pair"* — the fresh ruling open-items E1 asked for.

**The whole graphical layer existed for one app, and the premise was never
checked.** `stack/remote-ui/README.md`, open-items E1 and SR-015 all asserted
that IceDrive's Linux client is "GUI-only - no headless daemon, no CLI". Three
things were built on that: nine packages in `packages.list` (defaulted ON only
the day before, 2026-08-26), an AppImage pin, and
`homehub-desktop-session.service`, which logged a desktop in at boot so the GUI
client would keep syncing with nobody connected. E1's own text records why
nobody found out: *"Nothing can be built until there is a live client to
inspect."*

**Measured on the real binary this date, with no account needed:**

- `IcedriveCLI-v3.62` links **no** X11, xcb, Wayland or Qt GUI library at all -
  only `libfuse.so.2`, `libz`, `libglib-2.0`, `libstdc++`, `libm`, `libgcc_s`,
  `libc`. It behaves identically with `DISPLAY` unset, dead, or live.
- `-login <user> -password <pw>` is fully non-interactive: with stdin closed it
  reaches the vendor API and returns a real verdict (`code 1005`, invalid email
  or password).
- **The password cannot be kept off argv.** Three routes tried, all three answer
  `cli: no password given`: a pipe on stdin, a pty via `script`, and omitting
  `-password` so it prompts. Pre-seeding is not available either - the stored
  credential is encrypted by the app itself.
- **`icedrive_sessId` is NOT the persisted session**, contradicting what the
  remote-ui README claimed on 2026-08-27's own predecessor commit: the conf gets
  a fresh one after a login that FAILED. The key that matters is
  `icedrive_stored_cred`.
- **`--help` is not the option list.** The binary also parses `-hash`,
  `-clearsettings`, `-newsync`, `-sync`, `-share`, `-publink`, `-history`,
  `-requestfiles`, `-mount`, `-quit`, `-dir`, `-startup` and more. Most are IPC
  to a running instance and no-op without one (verified: `-newsync` with no
  instance falls straight through to the username prompt).
- **Sync pairs live in the ACCOUNT**, not on the box: `sync-list-pairs` carries
  `path_local`, `path_remote`, `folder_id`, `syncId`, `time_last`, `ini_done`,
  with `processSyncPairList` and `runSyncThreads` behind it. So a reimaged box
  fetches the list rather than starting empty - which contradicts the re-setup
  checklist this project has been carrying. **Creating** a pair from the CLI is
  not supported: the path is `showSyncDialog -> ... -> sync-pair-add`, and this
  binary has no toolkit to draw a dialog with.
- **2FA is a hard stop**: `2FA method isn't supported in CLI`.

**Changed, in this repo:**

- `packages.list`: the nine names moved to a re-created
  `packages.optional.list` - baked into `/opt/homehub/apt`, installed by
  nothing, so `HomeHubDesktop.cmd` still works on an offline hub.
  `libfuse2t64` deliberately STAYED installed: the CLI needs `libfuse.so.2`.
- `stack/remote-ui/`: `homehub-desktop-session.{sh,service}` **deleted**;
  `setup-remote-ui.sh` reverted to a pure manual opt-in that installs no vendor
  app and creates no boot session; README rewritten, listing the three claims
  that turned out to be wrong rather than quietly deleting them.
- `stack/icedrive/` (new): the pinned CLI, `setup-icedrive.sh` (install, ONE
  non-interactive sign-in, mount unit), `homehub-icedrive.service`, and a README
  that separates what was measured from what is still unproven.
- `icedrive.pin` now pins the **CLI** (9.5 MB) instead of the 118 MB AppImage;
  `export-icedrive.sh` and `stage_icedrive_into_payload` follow it.
- firstboot step 6c provisions IceDrive instead of a desktop, and is a no-op
  with no credential.
- `.env.example` gains `ICEDRIVE_USER`/`ICEDRIVE_PASSWORD` **and
  `OPERATOR_PASSWORD`** - see below.

**A defect found on the way, and it is the useful kind.** `OPERATOR_PASSWORD`
had **never been emitted by the pipeline**. HomeHub's `FieldSchema.psd1` had the
knob, `setup-remote-ui.sh` and the deleted session script both read it, and
`stack/.env.example` never carried the KEY - and the env emitter only fills keys
a template already names. So the row was inert, nothing reported it, and the
value on the bench box had been put there by hand during a bench session. **A
knob with no template line is emitted by nothing and reported by nothing.**

**Verified first-hand:**

- `bash -n` clean on `firstboot.sh`, `setup-remote-ui.sh`, `setup-icedrive.sh`,
  `export-icedrive.sh`, `lib/common.sh`; `python3 scripts/check.py` (gate G1)
  **PASS** - config-validate, registry-integrity, doc-navigability.
- `export-icedrive.sh --from` verified, cached and staged the CLI (9.5 M,
  version 3.62) against the new pin.
- HomeHub `Materialize-Deploy.ps1 -Preview` reports **all knobs resolve**, with
  `OPERATOR_PASSWORD` now sourced `store` and the two `ICEDRIVE_*` knobs
  resolving blank from the template (the shipped OFF state).
- **On the live bench box:** units disabled, `xrdp xorgxrdp xfce4-session xfwm4
  xfce4-panel xfce4-terminal thunar xvfb freerdp2-x11` removed with
  `--autoremove`, the running session killed, session detritus cleaned, the
  stale `xrdp-sesman` failed unit reset. Afterwards: **tcp/3389 does not listen,
  no Xorg process exists, `libfuse2t64` is still installed, 15 containers
  running with none unhealthy, and `systemctl --failed` is empty.**

**NOT PROVEN, and it is the whole remaining question:** nobody has ever signed
the CLI in. Whether the session persists (so the mount unit can carry no
password), whether the mount survives a reboot, whether the CLI acts on the
pairs it can list, and whether root can traverse a `hub`-owned FUSE mount are
all open. None of it was testable without an account. `setup-icedrive.sh` and
the mount unit are therefore written but **have never run against a real
credential** - they ship inert, and the icedrive README names the five questions
one login session would settle.

**Also NOT proven:** none of this has been through a clean image build.
