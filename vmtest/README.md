# V3 gate — local Hyper-V VM smoke test (WI-10.18)

The last gate before flashing the real AWOW: boot the REAL `stack/autoinstall/`
in a disposable local Hyper-V VM and reach `docker compose up -d` with the core
healthchecks green. This directory holds the **scripts + docs**; the actual
boot run is **Peter's** (needs an elevated PowerShell session and the Hyper-V
Windows feature — machine-level changes an agent doesn't make unilaterally).

**Honest status:** the scripts below were written and smoke-tested in WSL —
`build-seed.sh` was run for real (twice, including its idempotent re-run path)
and its output (user-data YAML, meta-data, deploy-payload, SIM `.env`) was
inspected and validated. `build-repacked-iso.sh` was also run for real against
an actual `ubuntu-24.04.4-live-server-amd64.iso` (SHA256-verified download) —
the repacked ISO was confirmed to still carry both a BIOS and a UEFI El Torito
boot image and to contain `/nocloud/` + `/deploy-payload/` correctly. **Nobody
has booted a VM from either ISO** — `New-AwowVm.ps1` / `Remove-AwowVm.ps1` need
elevation + the Hyper-V feature and were deliberately never run (see
docs/status.md, WI-10.18 entry, for the full ledger).

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

# LIGHT path (default, recommended):
bash vmtest/build-seed.sh
# -> vmtest/.out/seed.iso

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
  `late-commands` expect at `/cdrom/deploy-payload/`), with a **SIM `.env`**
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
   `deploy-payload/` to `/opt/awow-core/`, seeds `.env` (already filled with
   SIM values — no placeholder-seed step triggers), installs + enables
   `awow-firstboot.service`, and reboots on its own (`shutdown: reboot` in
   `user-data` — no confirmation).
4. On first real boot, `awow-firstboot.service` runs `firstboot.sh`
   automatically (systemd `oneshot`, `TimeoutStartSec=1800`).

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
  awow-firstboot` — oneshot, `RemainAfterExit=yes`).
- `docker compose ps` shows **technitium**, **caddy**, and **actual**
  `healthy` (their healthchecks don't depend on anything the VM can't
  provide).
- **tracker will legitimately fail to come up** — `docker-compose.yml`'s
  `tracker` service is `image: naglight:local`, a purely local tag with no
  registry. `deploy-payload/` only carries MiniPC-Deployer, not a NagLight
  checkout (matches production: `stack/README.md` §1 already calls "Build the
  NagLight image" a separate manual step done before bring-up). **This is
  expected, not a VM-test failure.** To also get tracker green, before/after
  first boot, on the VM:
  ```sh
  git clone https://github.com/<you>/NagLight /opt/NagLight   # or scp a checkout in
  docker build -t naglight:local /opt/NagLight
  cd /opt/awow-core/stack && docker compose up -d tracker oauth2-proxy caddy
  ```
  (or `docker save`/`docker load` the `naglight:local` image already built on
  the dev PC's WSL per WI-10.13, if you'd rather not clone again inside the
  VM).
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
| 48h uptime, no restarts | Yes (technitium/caddy/actual); tracker excluded per above |
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
  build-seed.sh           LIGHT path: stock ISO + small CIDATA seed ISO
  build-repacked-iso.sh   HEAVIER path: one self-contained ISO (fallback)
  lib/common.sh           shared rendering logic (sourced, not run directly)
  New-AwowVm.ps1          create the Hyper-V VM (elevation required; NOT run by an agent)
  Remove-AwowVm.ps1       companion teardown (elevation required; NOT run by an agent)
  .out/                   gitignored — everything the build scripts generate
```
