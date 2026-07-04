# V3 gate — local Hyper-V VM smoke test (WI-10.18)

The last gate before flashing the real AWOW: boot the REAL `stack/autoinstall/`
in a disposable local Hyper-V VM and reach `docker compose up -d` with the core
healthchecks green. This directory holds the **scripts + docs**; the actual
boot run is **Peter's** (needs an elevated PowerShell session and the Hyper-V
Windows feature — machine-level changes an agent doesn't make unilaterally).

**Honest status:** the scripts below were written and smoke-tested in WSL —
`build-seed.sh` was run for real (including with the Q10.9 B+ image payload; its
output — user-data YAML, meta-data, deploy-payload, SIM `.env`, and the 9 baked
image tars — was inspected and validated), and `export-images.sh` was run
end-to-end (all 9 stack images docker-saved + docker-load-verified as idempotent
no-ops). `build-repacked-iso.sh` was run for real in WI-10.18 against an actual
`ubuntu-24.04.4-live-server-amd64.iso` (SHA256-verified) — the repacked ISO was
confirmed to still carry both a BIOS and a UEFI El Torito boot image and to
contain `/nocloud/` + `/deploy-payload/`; the Q10.9 B+ addition (images folded
into `/deploy-payload/images/`) was separately verified via the exact `xorriso
-map` codepath. **Nobody has booted a VM from either ISO** — `New-AwowVm.ps1` /
`Remove-AwowVm.ps1` need elevation + the Hyper-V feature and were deliberately
never run. The actual first-boot `docker load` run is part of the V3 boot
(Peter's step). See docs/status.md for the full ledger.

## 0. Q10.9 B+ — the image payload (bake EVERY container "from infancy")

Per Peter's locked Q10.9 B+ decision, a freshly-imaged AWOW comes up with EVERY
stack container image already present — **zero registry/internet dependency for
container images at first boot**, versions pinned to exactly what the AWOW-sim
validated. The flow:

```
vmtest/export-images.sh          # docker save every pinned image -> vmtest/.out/images/*.tar
   │  (resolves the set from docker-compose.yml + the PINNED tags in .env.example)
   ▼
build-seed.sh / build-repacked-iso.sh
   │  stage_images_into_payload() folds the tars into deploy-payload/images/
   ▼
seed.iso / repacked.iso          # /deploy-payload/images/*.tar rides on the ISO
   ▼
autoinstall late-commands        # cp -a /cdrom/deploy-payload/. -> /opt/awow-core/
   ▼
/opt/awow-core/images/*.tar
   ▼
firstboot.sh step 3              # docker load each tar (idempotent) BEFORE compose up
```

**Size:** the current pinned set is **9 images ≈ 470 MB of tars** (docker save
already writes compressed layer blobs, so it is far smaller than the ~1–2 GB
Peter estimated — a plain `.tar` per image, no zstd needed; see
`export-images.sh` header for the measurement). The **light** seed ISO therefore
grows from ~1 MB to **~470 MB** (still a small add-on to the unmodified stock
ISO); the **repacked** ISO grows from ~3.4 GB to **~3.9 GB**. Both fit a USB with
room to spare.

**Build order:** run `export-images.sh` **before** `build-seed.sh` /
`build-repacked-iso.sh`. If you skip it, the build still succeeds but warns
loudly that the payload carries no images, and the VM falls back to pulling at
first boot (needs internet — and `naglight:local` has no registry home, so the
tracker would fail). Rebuild the payload whenever you bump a pinned tag.

---

## 1. ISO strategy — LIGHT path (default) vs. HEAVIER path (fallback)

### LIGHT path (recommended): `build-seed.sh` — stock ISO + a small seed ISO

Ubuntu's Subiquity installer uses cloud-init's **NoCloud datasource**, which
auto-detects ANY attached CD-ROM/USB filesystem whose **volume label is
`CIDATA`** (case-insensitive) and reads `user-data`/`meta-data` from its root.
This is the exact mechanism `stack/README.md`'s "Second USB (simplest)" already
documents for real hardware — `build-seed.sh` just burns it to an ISO instead
of a USB stick, so Hyper-V can attach it as a second virtual DVD drive. **No
repack of the 2.5-3GB stock ISO is needed** for the datasource to be found.

**The one caveat (read this — it's the honest bit):** finding the seed is not
the same as running fully hands-off. The stock ISO's GRUB menu does **not**
carry the `autoinstall` kernel argument by default (verified by extracting
`/boot/grub/grub.cfg` from a real `ubuntu-24.04.4-live-server-amd64.iso`: the
default entry is `linux /casper/vmlinuz  ---`, nothing else). Even with a valid
CIDATA seed attached, Subiquity will pause **once** for a confirmation prompt
("Continue with autoinstall?" — it does **not** ask you to type any values,
just to confirm) unless `autoinstall` is on the kernel command line. Getting
that one keypress out of the way needs a **one-time manual GRUB edit at the VM
console** — see §5 below. If you need truly zero-keypress automation (e.g.
scripted/repeated VM runs with nobody at the console), use the heavier path
instead.

### HEAVIER path (fallback): `build-repacked-iso.sh` — one self-contained ISO

Produces a single ISO: the stock Ubuntu ISO with `/boot/grub/grub.cfg` patched
to add `autoinstall ds=nocloud;s=/cdrom/nocloud/` to the boot entries, plus a
new `/nocloud/` directory (user-data + meta-data) and `/deploy-payload/`
(the repo copy) added at the ISO root. This is genuinely hands-off from
power-on — no GRUB edit needed.

It does **not** rebuild the ISO's boot catalog from scratch (which is fiddly
and easy to get subtly wrong for a hybrid BIOS+UEFI ISO). Instead it uses
`xorriso`'s `-boot_image any replay`, which reuses the **original** El Torito
boot catalog + hybrid MBR/GPT and only swaps in the files that changed. This
was verified structurally on a real download: `xorriso -report_el_torito
plain` on the repacked ISO still reports both a BIOS and a UEFI boot image,
and `/boot/grub/grub.cfg` / `/nocloud/` / `/deploy-payload/` are all present
and correct — `build-repacked-iso.sh` runs this exact check itself and refuses
to hand you a broken ISO.

Trade-off: ~3.4GB copied + rewritten per build (a few minutes), vs. `seed.iso`
being a ~1MB add-on to a stock, unmodified ISO. **Default to the light path;
reach for this one only if you actually need the zero-keypress property.**

---

## 2. Download the Ubuntu Server LTS ISO

`stack/README.md` targets **Ubuntu Server 24.04 LTS**. At the time this gate
was built, the current 24.04 point release was **24.04.4**:

```
URL:    https://releases.ubuntu.com/24.04/ubuntu-24.04.4-live-server-amd64.iso
SHA256: e907d92eeec9df64163a7e454cbc8d7755e8ddc7ed42f99dbc80c40f1a138433
```

That SHA256 was verified for real against a fresh download (`sha256sum`
matched exactly). **Point releases move on** — before you download, cross-check
the current filename + hash at
<https://releases.ubuntu.com/24.04/SHA256SUMS> (and ideally
`SHA256SUMS.gpg`/`SHA256SUMS.sig` against Ubuntu's signing key, for
belt-and-suspenders). `build-repacked-iso.sh --expected-sha256 <hash>` will
verify it for you; for the light path there's no built-in check, so verify by
hand:

```sh
# in WSL
curl -fsSLO https://releases.ubuntu.com/24.04/ubuntu-24.04.4-live-server-amd64.iso
sha256sum ubuntu-24.04.4-live-server-amd64.iso
# compare against https://releases.ubuntu.com/24.04/SHA256SUMS
```

**Disk-space gotcha (learned the hard way while building this gate):**
downloading/repacking a multi-GB ISO from a path INSIDE WSL's native
filesystem (e.g. `~`, `/root`, `/home/...`) grows WSL2's `ext4.vhdx` file,
which itself lives on a **Windows drive (commonly C:)** and does **NOT**
auto-shrink when you delete the files afterward (a well-known WSL2 quirk —
freeing space inside the ext4 filesystem doesn't return blocks to the sparse
vhdx on the host without an explicit compact). If `C:` is tight (check with
`Get-Volume` in PowerShell), either:
- do the ISO download/build under a Windows-mounted path instead, e.g.
  `OUT_DIR=/mnt/d/vmtest-out bash vmtest/build-seed.sh` (writes straight to
  the real `D:` drive over the WSL 9p mount — never touches the vhdx), or
- afterward, reclaim the vhdx: `wsl --shutdown`, then (elevated) either
  `Optimize-VHD -Path <path-to-ext4.vhdx> -Mode Full` (Hyper-V module) or
  `diskpart` → `select vdisk file="<path>"` → `attach vdisk readonly` →
  `compact vdisk` → `detach vdisk`. Find the path with:
  `Get-ChildItem "$env:LOCALAPPDATA\Packages" -Filter ext4.vhdx -Recurse`.

---

## 3. Build the seed / ISO

```sh
# in WSL (Ubuntu) — needs genisoimage or xorriso, openssl, ssh-keygen
# (all installed already if you followed WI-10.13; otherwise:
#   sudo apt-get install -y genisoimage xorriso openssl openssh-client)

cd /path/to/MiniPC-Deployer

# STEP 0 (Q10.9 B+): bake every stack image into the payload FIRST.
# Build naglight:local beforehand (it has no registry home):
#   docker build -t naglight:local ../NagLight
bash vmtest/export-images.sh
# -> vmtest/.out/images/*.tar (9 images ≈ 470MB) + images.manifest.tsv

# LIGHT path (default, recommended):
bash vmtest/build-seed.sh
# -> vmtest/.out/seed.iso   (now ~470MB — carries the image payload)

# HEAVIER path (only if you need zero-keypress):
bash vmtest/build-repacked-iso.sh --src-iso /mnt/d/iso/ubuntu-24.04.4-live-server-amd64.iso \
    --expected-sha256 e907d92eeec9df64163a7e454cbc8d7755e8ddc7ed42f99dbc80c40f1a138433
# -> vmtest/.out/repacked.iso
```

Both scripts:
- Materialize `user-data`/`meta-data` from the REAL `stack/autoinstall/` files
  — same identity/ssh/storage/late-commands logic as production, with only
  the operator's SSH key placeholder, console password, and hostname swapped
  for disposable VM-test values (an ephemeral ed25519 keypair generated fresh
  under `vmtest/.out/ssh/`, and a random SIM console password — see
  `vmtest/.out/secrets/creds.env`, gitignored, `chmod 600`).
- Copy the whole repo into a `deploy-payload/` tree (what the real
  `late-commands` expect at `/cdrom/deploy-payload/`), **plus fold the
  docker-save image tars from `export-images.sh` into `deploy-payload/images/`**
  (Q10.9 B+ — hardlinked when the filesystem allows, to save C: space per OI-6),
  with a **SIM `.env`**
  materialized from `stack/.env.example` — fictional domain, fictional Google
  OAuth client, a real-shaped (but throwaway) oauth2-proxy cookie secret, a
  random Technitium admin password, and real Caddy bcrypt basic_auth hashes
  (generated via `docker run --rm caddy:2-alpine caddy hash-password`, if
  Docker is available in WSL — it is, per WI-10.13).
- Are **idempotent**: re-running reuses the existing SSH key + SIM secrets
  (pass `CLEAN=1` in the environment, or `--clean`, to force fresh ones).
- **Never** touch a real secret. Real materialization for the physical AWOW
  happens later via **SECRET_HANDOFF_PROPOSAL**, once Peter ratifies it
  (WI-10.3, still open) — these scripts say so in their own header comments.

If `C:` is tight (see §2), point the output elsewhere:
`OUT_DIR=/mnt/d/vmtest-out bash vmtest/build-seed.sh`.

---

## 4. Enable Hyper-V (machine-level — Peter's consent, one-time)

Skip if already enabled (`Get-Command Get-VM` succeeds in PowerShell).

```powershell
# Elevated PowerShell
Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All
# Reboot when prompted.
```

Hyper-V and WSL2 coexist fine on modern Windows (both use the same
hypervisor) — no need to disable WSL2 first.

---

## 5. Create the VM

```powershell
# Elevated PowerShell, from the MiniPC-Deployer checkout

# LIGHT path:
.\vmtest\New-AwowVm.ps1 `
    -UbuntuIsoPath D:\iso\ubuntu-24.04.4-live-server-amd64.iso `
    -SeedIsoPath   .\vmtest\.out\seed.iso

# HEAVIER path (single ISO carries everything):
.\vmtest\New-AwowVm.ps1 `
    -UbuntuIsoPath .\vmtest\.out\repacked.iso `
    -SeedIsoPath   .\vmtest\.out\repacked.iso `
    -SkipSecondDvd
```

Defaults: `AWOW-VMTest`, Gen2, 4 vCPU / 8GB static RAM (a reasonable stand-in
for the AK41's Celeron J4125 / 8GB — not an exact clone), 64GB dynamic VHDX,
**Default Switch** (Windows' built-in NAT), Secure Boot ON with the
`MicrosoftUEFICertificateAuthority` template (the template Microsoft ships
specifically for signed Linux bootloaders — Ubuntu's `shimx64` needs this, not
the Windows-only default template). See `Get-Help .\vmtest\New-AwowVm.ps1
-Full` for every parameter (RAM/CPU/disk size, `-DynamicMemory`,
`-DisableSecureBoot`, `-Force` to recreate, `-KeepDisk`, `-Start`). Supports
`-WhatIf` — run that first if you want to preview without creating anything.

**Real LAN exposure (optional):** pass `-SwitchName "<your External switch>"`
to bridge the VM onto your real LAN instead of NAT — needed if you want to
`dig` the VM's Technitium from another physical device (§7's DNS-client-check
delta). Create the External switch yourself first (Hyper-V Manager → Virtual
Switch Manager → New → External, bound to your real NIC) — this script
deliberately does not create one (a host-networking change with more blast
radius than a VM-local NAT switch).

The VM is created **stopped**. Start it yourself (`-Start`, or `Start-VM
-Name AWOW-VMTest`, or via Hyper-V Manager) once you're ready to watch the
console for §6.

---

## 6. Boot it — connect + (LIGHT path only) the one-time GRUB edit

```powershell
Start-VM -Name AWOW-VMTest
vmconnect localhost AWOW-VMTest
```

1. GRUB menu appears ("Try or Install Ubuntu Server" highlighted).
2. **LIGHT path only:** press **`e`** to edit. Find the line starting
   `linux	/casper/vmlinuz` (ends in ` ---`). Click into it and type
   `autoinstall ds=nocloud;s=/cdrom/nocloud/` right before the trailing `---`,
   so it reads:
   `linux	/casper/vmlinuz  autoinstall ds=nocloud;s=/cdrom/nocloud/ ---`
   Then press **Ctrl+X** (or F10) to boot with the edited line. **This is the
   only manual step in the whole gate** — nothing else needs typing.
   (HEAVIER path: skip this, the repacked ISO already boots straight through.)
3. Subiquity partitions the disk (whole-disk LVM), creates the `operator`
   user, installs Docker + Cockpit + unattended-upgrades, copies
   `deploy-payload/` to `/opt/awow-core/` (**including `images/` — the baked
   container image tars, Q10.9 B+**), seeds `.env` (already filled with SIM
   values — no placeholder-seed step triggers), installs + enables
   `awow-firstboot.service`, and reboots on its own (`shutdown: reboot` in
   `user-data` — no confirmation).
4. On first real boot, `awow-firstboot.service` runs `firstboot.sh`
   automatically (systemd `oneshot`, `TimeoutStartSec=1800`): it **`docker
   load`s every tar from `/opt/awow-core/images/` (step 3 of firstboot) before
   `docker compose up -d`**, so the stack starts entirely from the baked images
   with no registry pulls.

You can log in at the console at any point with user `operator` and either the
SIM password from `vmtest/.out/secrets/creds.env`, or
`ssh -i vmtest/.out/ssh/awow-vmtest-ed25519 operator@<vm-ip>` once networking
is up (find the IP via the console: `ip -4 addr show` or Hyper-V Manager's
VM summary pane — Default Switch NAT hands out a `172.x`-range address).

---

## 7. What "success" looks like

Watch first-boot bring-up:

```sh
journalctl -u awow-firstboot -f      # follow the oneshot's log
docker compose -f /opt/awow-core/stack/docker-compose.yml ps
```

**Minimum V3 success (the actual gate):**
- `awow-firstboot.service` reports `SUCCESS` (`systemctl status
  awow-firstboot` — oneshot, `RemainAfterExit=yes`). Its log (`journalctl -u
  awow-firstboot`) shows **"loading N baked image tar(s)"** and a `docker load`
  line per image (Q10.9 B+) BEFORE `docker compose up -d`.
- `docker compose ps` shows **technitium**, **caddy**, **actual**, AND
  **tracker** `healthy` — every image (including `naglight:local`) was baked
  into the payload and `docker load`ed at first boot, so nothing needs a
  registry.
- **Q10.9 B+ closed the old tracker gap.** Previously this doc warned that
  tracker would "legitimately fail to come up" because `naglight:local` has no
  registry and the payload didn't carry it. That gap is now **closed by
  design**: `export-images.sh` docker-saves `naglight:local` (and every other
  stack image) into `deploy-payload/images/`, and `firstboot.sh` docker-loads
  them before compose up. No manual NagLight clone/build inside the VM is needed
  anymore — provided you ran `export-images.sh` before building the ISO (with
  `naglight:local` already built on the dev PC, WI-10.13). If you skip
  `export-images.sh`, firstboot logs the loud no-payload fallback and tracker
  reverts to the old failure mode.
- **oauth2-proxy** has no container healthcheck (by design, see
  `docker-compose.yml` comment — the stock image is distroless); it should be
  `Up`, not crash-looping.
- **ddns** will fail Cloudflare auth (SIM token) and sit `Restarting` —
  **expected**, it carries no healthcheck so it doesn't gate the burn-in
  checklist's "all healthy" bar.
- Caddy stays `healthy` even though it can never get a real ACME cert for
  `vmtest.sim.invalid` (its healthcheck only probes its own admin API, not
  issuance state) — TLS itself will show a self-signed/invalid cert; that's
  the "no real ACME in a VM" delta, not a bug.

**Which `stack/README.md` §6 burn-in checklist items apply in this VM:**

| Check | Applies in VM? |
|---|---|
| 48h uptime, no restarts | Yes — technitium/caddy/actual/tracker all baked & loaded (Q10.9 B+) |
| DNS under load (`dig`/`dnsperf`) | Yes, **from inside the VM** (`dig @127.0.0.1`) |
| Thermals / throttling | **No** — no real hardware to thermal-throttle |
| Storage health (`smartctl`) | **No** — virtual disk, not eMMC/SSD |
| Reboot resilience | Yes — power-cycle the VM, confirm auto-recovery |
| TLS on LAN, no cert warning | **No** (Default Switch/NAT) — needs an External switch + a real domain to mean anything |
| OAuth round-trip | **No** — SIM Google OAuth client isn't real; this VM proves compose/health plumbing, not the Google flow (that needs OI-1's real client, see docs/status.md) |
| Pick secondary/failover DNS | **No** — a fleet decision, not a VM concern |

---

## 8. Known VM-vs-hardware deltas (don't mistake these for bugs)

- **No real LAN `:53` client test** — Default Switch is NAT; another physical
  device can't `dig` this VM. Use an External switch (§5) if you need that.
- **No USB backup drives** — `stack/backup/` (WI-10.15) targets real
  USB-attached storage on the AK41; a VM has no USB passthrough story here.
  Not exercised by this gate.
- **NAT IP, not the LAN reservation IP** — `.env`'s `LAN_IP` is set to
  `0.0.0.0` by the build scripts (harmless bind-all in a NAT'd test VM);
  production MUST set the real DHCP-reserved LAN IP.
- **No real ACME/TLS** — `vmtest.sim.invalid` can't get a publicly-trusted
  cert; Caddy keeps retrying in the background without crashing.
- **No real Google OAuth** — the SIM client ID/secret let oauth2-proxy start,
  but nobody can actually complete a Google sign-in against them.
- **No real Cloudflare DDNS** — the SIM token fails auth; `ddns` sits
  restarting (no healthcheck, doesn't gate the V3 result).

---

## 9. Teardown

```powershell
.\vmtest\Remove-AwowVm.ps1                 # stop + remove VM + delete its VHDX
.\vmtest\Remove-AwowVm.ps1 -KeepDisk        # keep the VHDX (e.g. to re-attach later)
.\vmtest\Remove-AwowVm.ps1 -WhatIf          # preview only
```

Idempotent — running it against a VM that doesn't exist is a no-op, not an
error.

---

## 10. Files in here

```
vmtest/
  README.md               this file
  export-images.sh        Q10.9 B+: docker save every pinned stack image -> .out/images/*.tar
  build-seed.sh           LIGHT path: stock ISO + CIDATA seed ISO (folds in the image payload)
  build-repacked-iso.sh   HEAVIER path: one self-contained ISO (fallback; folds in the payload)
  lib/common.sh           shared rendering + stage_images_into_payload (sourced, not run directly)
  New-AwowVm.ps1          create the Hyper-V VM (elevation required; NOT run by an agent)
  Remove-AwowVm.ps1       companion teardown (elevation required; NOT run by an agent)
  .out/                   gitignored — everything the build scripts generate
  .out/images/            gitignored — the docker-save image tars + manifest
```
