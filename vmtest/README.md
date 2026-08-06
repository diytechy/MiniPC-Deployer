# V3 gate — local Hyper-V VM smoke test (WI-10.18)

The last gate before flashing the real AWOW: boot the REAL `stack/autoinstall/`
in a disposable local Hyper-V VM and reach `docker compose up -d` with the core
healthchecks green. This directory holds the **scripts + docs**; the actual
boot run is **the Owner's** (needs an elevated PowerShell session and the Hyper-V
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
-map` codepath. The **hub** ISO has since been booted for real — the V3 gate ran
on 2026-07-31 and again on 2026-08-01, and both runs are in docs/status.md
(including what the first one got wrong while reporting PASS).

**The WALL ISO (§11) has never been booted.** Its builder was written and run
for real on 2026-08-02 — the ISO, the rendered sim `user-data`, the SIM
`wall.env` and the staged 111 MB shell artifact were all inspected, the
installer's own late-commands were executed verbatim against a fake `/target`
(the artifact unpacks, `chrome-sandbox` keeps 4755 root:root, `[ -x
WALL_APP_CMD ]` is true), and the shell was run in a bare `ubuntu:24.04`
carrying exactly this image's package list, where `ldd` resolves everything and
Electron reaches Ozone init. **Nothing has watched it come up under `cage` on a
display.** That is the A19 gate's job. See docs/status.md for the full ledger.

## 0a. The offline install — bake every PACKAGE too (2026-08-06)

§0b below makes the *stack* independent of a registry. This makes the *install*
independent of the Ubuntu archive, and it is the newer and sharper of the two:
on 2026-08-06 a production stick installed a **bare Ubuntu** onto the real hub
and reported success — no `/opt/homehub`, no `openssh-server`, no Docker, no
Cockpit, and a password-locked console account, so no way in at all. One cause,
five symptoms, three steps apart: the install had no working network when apt
ran, `packages:` died on `cockpit` with exit 100, subiquity errored, and every
late-command after it never ran.

`packages:` runs **before** any late-command, so no local apt source could ever
have rescued it. Making the network more reliable is not the fix; removing the
dependency is.

```
stack/autoinstall[/wall]/packages.list      the ONE list. Nothing retypes it.
   │
   ├─► vmtest/export-apt.sh                 resolve the closure in a clean
   │      │                                 ubuntu:24.04 container, then PROVE it
   │      │                                 in a second one: --network none,
   │      │                                 every archive source deleted. Refuses
   │      ▼                                 to ship a repo that cannot resolve.
   │   .out/apt/{*.deb, Packages, packages.baked.list}
   │      │
   │      ▼  stage_apt_into_payload() — and it checks the baked stamp against
   │         packages.list, so the repo and the list cannot describe different
   │         installs
   │   deploy-payload/apt/
   │      ▼
   │   autoinstall late-command 3  copies the whole payload to <root>/apt
   └─► autoinstall late-command 3c `deb [trusted=yes] file:///<root>/apt ./`
                                   + `xargs -a packages.baked.list apt-get install`
```

Both images ship `packages: []` and `ssh: install-server: false`. Measured:
**hub 224 debs / 176 MB**, **wall 340 debs / 129 MB** — both offline-verified,
and the hub ISO is 3.9 GB on an 8 GB stick.

- **The repo is a local path INSIDE the target**, never `file:///cdrom/...`.
  Whether `/cdrom` is visible in the chroot during curtin's package phase is
  Ubuntu's business; by late-commands the payload is already on `/target`, so
  the question does not arise. Do not reintroduce a `/cdrom` apt source.
- **Nothing is written to `/etc/apt`.** The source line and an empty parts
  directory live in `/run` for the duration of the command and apt is pointed at
  them with `-o Dir::Etc::*`. The target's `ubuntu.sources` is never moved,
  disabled or restored, so `unattended-upgrades` tracks the archive normally
  from first boot.
- **Frozen debs.** You install the set you tested, not whatever the archive
  holds that day. Re-bake when you rebuild the image; a stale repo is a refused
  BUILD, not a bad box.
- **Build order:** `export-apt.sh` before any `build-*.sh`. Unlike the image
  payload this is not a warning — the build REFUSES, because `packages:` is
  empty and an ISO without the repo installs nothing at all. `ALLOW_MISSING_APT=1`
  opts out, loudly, for exercising the seed machinery alone.

```sh
bash vmtest/export-apt.sh --target hub  --out vmtest/.out/apt
bash vmtest/export-apt.sh --target wall --out vmtest/.out-wall/apt
```

---

## 0b. Q10.9 B+ — the image payload (bake EVERY container "from infancy")

Per the Owner's locked Q10.9 B+ decision, a freshly-imaged AWOW comes up with EVERY
stack container image already present — **zero registry/internet dependency for
container images at first boot**, versions pinned to exactly what the homehub-sim
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
autoinstall late-commands        # cp -a /cdrom/deploy-payload/. -> /opt/homehub/
   ▼
/opt/homehub/images/*.tar
   ▼
firstboot.sh step 3              # docker load each tar (idempotent) BEFORE compose up
```

**Size:** the current pinned set is **9 images ≈ 470 MB of tars** (docker save
already writes compressed layer blobs, so it is far smaller than the ~1–2 GB
The Owner estimated — a plain `.tar` per image, no zstd needed; see
`export-images.sh` header for the measurement). The **light** seed ISO therefore
grows from ~1 MB to **~470 MB** (still a small add-on to the unmodified stock
ISO); the **repacked** ISO grows from ~3.4 GB to **~3.9 GB**. Both fit a USB with
room to spare.

**Build order:** run `export-images.sh` **before** `build-seed.sh` /
`build-repacked-iso.sh`. If you skip it, the build still succeeds but warns
loudly that the payload carries no images, and the VM falls back to pulling at
first boot (needs internet — and `naglight:local` has no registry home, so the
tracker would fail). Rebuild the payload whenever you bump a pinned tag.

**Tier-2 boundary (SR-012):** "EVERY container" means the **core+ntfy set** —
the opt-in tier-2 catalog (stack/README §9) sits behind compose profiles, so
`export-images.sh` never sees those images by default and the ISO stays
core-sized; enabled opt-ins pull at enable time instead. To bake an enabled set
anyway: `EXTRA_PROFILES="navidrome vaultwarden" bash vmtest/export-images.sh`.

### Gating an image whose profile set is NOT the default

A real image's opt-in set lives in its own config (the AWOW's is Personal's
`config.homehub.psd1`), which never comes near this repo — the sim `.env` is a
copy of `stack/.env.example`, i.e. the generic defaults with every tier-2
profile off. Two env vars make the gate boot the set the real box will boot,
without changing the public default and without staging real secrets:

```sh
# 1. bake the extra images into the payload
EXTRA_PROFILES="immich immich-ml jellyfin finance-auditor" \
  IMAGES_OUT=/mnt/d/vmtest-out/images bash vmtest/export-images.sh

# 2. turn the same set on in the SIM .env
SIM_ENV_OVERRIDES='COMPOSE_PROFILES=ntfy,immich,immich-ml,jellyfin,finance-auditor
IMMICH_ML_ENABLED=true
IMMICH_ML_MEM_LIMIT=2g' bash vmtest/build-seed.sh
```

`SIM_ENV_OVERRIDES` takes newline- or semicolon-separated `KEY=VALUE` pairs and
**fails the build** if a key is not already a knob in `.env.example` — a typo
would otherwise append a line compose ignores, and the gate would test the
default set while reporting success.

**Payload size scales with what you bake.** The core+ntfy set is 9 images
≈ 470 MB; adding immich + immich-ml + jellyfin + finance-auditor takes it to
15 images and roughly **2.5–3 GB**, so the light `seed.iso` grows to about that
and the repacked ISO to ~6 GB. Build with `OUT_DIR=/mnt/d/...` unless `C:` has
room to spare (§2's disk-space gotcha, now much easier to hit).

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

**There is one entry that does not autoinstall: `Diagnostic Shell (no
autoinstall)`, appended last in the menu.** It boots the stock live installer
with no `autoinstall` and no seed, so nothing is written to disk; from it, Help
→ *Enter shell*, or Ctrl+Alt+F2, gives you a console. It exists because an
autoinstall that halts — most often a `storage: match:` disk pin that finds no
such device — offers "press enter to start a shell" and then reboots out from
under you before you can answer, leaving no way to ask the machine what it
actually has. The unattended entry is still index 0 and still what boots when
nobody touches the keyboard (`set default=0`, asserted at build time); the
5-second timeout is your window to choose otherwise. First things to run:

```sh
lsblk -o NAME,SIZE,MODEL,SERIAL
udevadm info --query=property --name=/dev/nvme0n1 | grep ID_SERIAL
```

**And one that installs, but asks first: `Install - choose target disk manually
(unpinned)`.** It boots the same payload and the same credentials from a second
seed (`/nocloud-confirm/`, generated from the pinned one at build time) whose
storage section is interactive and carries no `match:`. Subiquity stops at its
guided-storage screen — every disk with model, serial and size, explicit
selection, then its own destructive-action confirmation — instead of choosing
for you. This is the escape hatch for "I am standing at the right machine and
the pin is wrong": no rebuild, no hand-edited kernel line.

> **It changes what the pin guarantees.** Before, the stick physically could not
> install anywhere but the pinned machine. Now it can, given one deliberate menu
> choice inside a 5-second window. That is judged acceptable because the pin was
> never protecting the *secrets* — the payload is readable by anyone who mounts
> the ISO — it protects against destroying the wrong machine's disk, which an
> interactive storage screen addresses directly. If you want the hard lock back,
> delete the `menuentry` and the `/nocloud-confirm` mapping in
> `build-repacked-iso.sh`; the build asserts they are consistent, so removing
> one without the other fails loudly rather than shipping a dead menu line.

The build refuses to produce an ISO where the two seeds differ by anything other
than the storage selector and `interactive-sections`, so the unpinned entry can
never quietly become a *different install* — same user, same payload, same
late-commands, only the disk choice moves.

**The ISO carries its own volume label** (`--volid`, default `HOMEHUB` /
`WALLPANEL`; HomeHub passes its `ImageSpec.StickLabel` so the two cannot drift).
This matters for the raw-written stick, not the VM: a hybrid ISO written to USB
exposes a large **read-only ISO9660** partition — the one Windows letters and
shows you, and which therefore *cannot* be relabelled afterwards — plus a ~5 MB
EFI System Partition that is writable but which Windows hides. A post-write
relabel had, at best, a partition nobody ever sees. Baking the label into the
Primary Volume Descriptor puts it on the partition you actually look at, and it
survives the raw write with no post-hoc step. The build reads the volume id back
off the finished ISO and fails if it isn't what was asked for.

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
  happens later via **SECRET_HANDOFF** (WI-10.3, **RATIFIED 2026-07-25**;
  the tooling itself is not written yet) — these scripts say so in their own
  header comments.

If `C:` is tight (see §2), point the output elsewhere:
`OUT_DIR=/mnt/d/vmtest-out bash vmtest/build-seed.sh`.

---

## 4. Enable Hyper-V (machine-level — the Owner's consent, one-time)

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

**Easiest: right-click `vmtest\Run-V3Gate.cmd` → "Run as administrator".** It
wraps everything below — checks elevation (and re-launches itself through UAC if
you merely double-clicked), verifies both ISOs exist and that Hyper-V answers,
creates + starts the VM, prints the one-time GRUB edit from §6, and opens
`vmconnect`. Defaults: stock ISO from `D:\iso\`, seed from `.out\seed.iso`, VHDX
to `D:\HyperV\HomeHub-VMTest` (**D: on purpose — C: is the tight drive**). Optional
switches: `/force` (delete an existing VM **and its VHDX** first), `/whatif`
(preview only), `/noconn` (skip `vmconnect`); an explicit ISO path can be passed
as the first argument. The manual equivalent:

```powershell
# Elevated PowerShell, from the MiniPC-Deployer checkout

# LIGHT path:
.\vmtest\New-HomeHubVm.ps1 `
    -UbuntuIsoPath D:\iso\ubuntu-24.04.4-live-server-amd64.iso `
    -SeedIsoPath   .\vmtest\.out\seed.iso

# HEAVIER path (single ISO carries everything):
.\vmtest\New-HomeHubVm.ps1 `
    -UbuntuIsoPath .\vmtest\.out\repacked.iso `
    -SeedIsoPath   .\vmtest\.out\repacked.iso `
    -SkipSecondDvd
```

Defaults: `HomeHub-VMTest`, Gen2, 4 vCPU / 8GB static RAM (a reasonable stand-in
for the AK41's Celeron J4125 / 8GB — not an exact clone), 64GB dynamic VHDX,
**Default Switch** (Windows' built-in NAT), Secure Boot ON with the
`MicrosoftUEFICertificateAuthority` template (the template Microsoft ships
specifically for signed Linux bootloaders — Ubuntu's `shimx64` needs this, not
the Windows-only default template). See `Get-Help .\vmtest\New-HomeHubVm.ps1
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
-Name HomeHub-VMTest`, or via Hyper-V Manager) once you're ready to watch the
console for §6.

---

## 6. Boot it — connect + (LIGHT path only) the one-time GRUB edit

```powershell
Start-VM -Name HomeHub-VMTest
vmconnect localhost HomeHub-VMTest
```

1. GRUB menu appears ("Try or Install Ubuntu Server" highlighted).
2. **LIGHT path only:** press **`e`** to edit. Find the line starting
   `linux	/casper/vmlinuz` (ends in ` ---`). Click into it and type the single
   word `autoinstall` right before the trailing `---`, so it reads:
   `linux	/casper/vmlinuz  autoinstall ---`
   Then press **Ctrl+X** (or F10) to boot with the edited line. **This is the
   only manual step in the whole gate** — nothing else needs typing.
   (HEAVIER path: skip this, the repacked ISO already boots straight through.)

   > **Type `autoinstall` and NOTHING else on the light path.** This step used
   > to read `autoinstall ds=nocloud;s=/cdrom/nocloud/` — copied from the
   > repacked path, where it is correct. On the light path it is wrong:
   > `/cdrom` is the *stock* ISO, which has no `/nocloud/` directory (only
   > `build-repacked-iso.sh` creates one), so that argument points cloud-init
   > at a path that does not exist. The seed does not need it — `seed.iso` is
   > labelled **CIDATA** and the NoCloud datasource finds it by label on its
   > own, which is the entire reason the light path works without a repack
   > (see `build-seed.sh`'s header). The bare `autoinstall` is only there to
   > skip the "Continue with autoinstall?" confirmation prompt.
3. Subiquity partitions the disk (whole-disk LVM), creates the `hub`
   user, installs Docker + Cockpit + unattended-upgrades, copies
   `deploy-payload/` to `/opt/homehub/` (**including `images/` — the baked
   container image tars, Q10.9 B+**), seeds `.env` (already filled with SIM
   values — no placeholder-seed step triggers), installs + enables
   `homehub-firstboot.service`, and reboots on its own (`shutdown: reboot` in
   `user-data` — no confirmation).
4. On first real boot, `homehub-firstboot.service` runs `firstboot.sh`
   automatically (systemd `oneshot`, `TimeoutStartSec=1800`): it **`docker
   load`s every tar from `/opt/homehub/images/` (step 3 of firstboot) before
   `docker compose up -d`**, so the stack starts entirely from the baked images
   with no registry pulls.

You can log in at the console at any point with user `hub` and either the
SIM password from `vmtest/.out/secrets/creds.env`, or
`ssh -i vmtest/.out/ssh/homehub-vmtest-ed25519 hub@<vm-ip>` once networking
is up (find the IP via the console: `ip -4 addr show` or Hyper-V Manager's
VM summary pane — Default Switch NAT hands out a `172.x`-range address).

---

## 7. What "success" looks like

> **The A19/V3 gates below are SIM gates, and everything in them is read by a
> human.** They build with `SITE_DIR` unset, so `common.sh` emits a sim image —
> a different user-data, a different disk pin, a different `.env` — and nothing
> is checked after `Start-VM`. A green run means "two VMs were created and are
> Running".
>
> **HomeHub's `VirtualHomeHub.cmd` is the automated counterpart, and it boots the
> PRODUCTION images on the REAL LAN.** Added 2026-08-06, after a production stick
> installed a bare Ubuntu onto the real hub and reported success: no
> `/opt/homehub`, no `openssh-server`, no Docker, and no way in. Every check in
> the repo passed, because all of them run before the ISO is written.
>
> It uses this directory's pieces — `make-gate-iso.sh`, `Send-VmConsoleKeys.ps1`,
> `assert-installed.sh`, `New-HomeHubVm.ps1` — but owns the sequencing itself, on
> an **external** switch with the VMs carrying the real machines' MAC addresses
> so DHCP hands them the production reservations. It refuses to start while the
> real hub or panel is powered on.
>
> Two runs of the same image, asserting opposite properties:
>
> - **Run A — containment.** Boot the shipped ISO untouched. The disk pin must
>   refuse this machine, and **nothing may be written** — measured on the VHDX,
>   because the installer's own claim about whether it wrote is the thing under
>   test.
> - **Run B — completeness.** Boot the gate ISO from `make-gate-iso.sh` (same
>   payload, one extra unattended-unpinned entry, equivalence asserted at build
>   time), install, find the guest by MAC in the host's neighbour table (no KVP
>   daemon on these guests), SSH in, and run `assert-installed.sh` then
>   `stack/provision/healthcheck.sh`.
>
> The VHDX carries materialised credentials once Run B finishes and is destroyed
> on every exit path unless `-KeepVhdxForDebug`, which says so and names the
> file. The gate ISO installs unattended to any disk it finds and **must never be
> written to physical media** — that is exactly why the shipped image does not
> carry such an entry.
>
> **THE INSTALL ITSELF NEEDS NO NETWORK (2026-08-06), and the gate proves it
> without being asked.** Both images ship `packages: []` and `ssh:
> install-server: false`; every package comes from `deploy-payload/apt/`, a repo
> of frozen `.deb` files `export-apt.sh` resolves and then verifies in a
> container with `--network none`.
>
> Stages 5–7 **disconnect the VM's network adapter before the VM is started** and
> reconnect it once the install is done. Not the vSwitch — that is External with
> `-AllowManagementOS`, so disconnecting it would take the host's networking and
> the other VM with it; a per-VM adapter disconnect is exactly "someone unplugged
> the Ethernet from this machine".
>
> Knowing *when* the install finished, with no channel to ask over, is a
> heuristic — a heartbeat cycle through the guest's reboot, or the VHDX going
> quiet, floored and ceilinged. **The verdict does not depend on it.** After the
> reconnect the launcher reads `/var/log/installer/`'s newest timestamp off the
> installed box and compares it with the moment the cable went back in: earlier
> means the install completed with no link, later is a named FAILURE. An early
> reconnect cannot produce a pass. `-OnlineInstall` opts out and says so in the
> summary.
>
> The link is restored before the stack comes up, deliberately: offline covers
> the *install*, not first-boot service bring-up (ACME, DDNS, OAuth, Cloudflare
> all need the internet), and `healthcheck.sh` is asking a different question.

Watch first-boot bring-up:

```sh
journalctl -u homehub-firstboot -f      # follow the oneshot's log
docker compose -f /opt/homehub/stack/docker-compose.yml ps
```

**Minimum V3 success (the actual gate):**
- `homehub-firstboot.service` reports `SUCCESS` (`systemctl status
  homehub-firstboot` — oneshot, `RemainAfterExit=yes`). Its log (`journalctl -u
  homehub-firstboot`) shows **"loading N baked image tar(s)"** and a `docker load`
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

- **The install target disk is pinned differently in the VM — deliberately.**
  Production pins `storage.layout.match.path: /dev/nvme0n1` (the AK41's
  internal NVMe). Hyper-V Gen2 has no NVMe controller — the VHDX hangs off the
  synthetic SCSI controller as `/dev/sda` — so the production pin matches
  nothing in a VM and the install **halts**. `lib/common.sh`'s sim branch
  therefore rewrites the match to `model: Virtual_Disk` for vmtest builds only.
  That value is the udev `ID_MODEL` of a Hyper-V synthetic disk (note the
  UNDERSCORE — Subiquity matches `ID_MODEL` via probert, not the prettified
  `ID_MODEL_ENC` that `lsblk` prints as "Virtual Disk").
  **Why not simply `path: /dev/sda`:** `/dev/sda` is a REAL disk on real
  hardware, so a sim ISO that ever met a physical machine — or a USB stick
  someone wrote it to — would wipe it unattended. `model: Virtual_Disk` can
  only ever select a virtual disk; on real hardware it matches nothing and the
  install fail-safe halts. The containment holds even if the sim ISO escapes
  the VM. The production `user-data` is **not** modified by any of this, and
  the build refuses to proceed if the substitution silently no-ops or if any
  `path:`/`serial:`/`wwn:` match survives into the sim user-data.
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
- **A DRM node exists, but it cannot transcode.** Measured 2026-08-01, and it
  contradicts what this repo previously assumed: Hyper-V's synthetic
  `hyperv_drm` driver DOES create `/dev/dri`, but with **`card1` only and no
  `renderD*` node**. So firstboot step 3c writes the QSV override here and
  Jellyfin gets the device — while VA-API has nothing to bind to. The AWOW is
  the opposite shape (`i915`, `/dev/dri/renderD128`, confirmed in
  `baselines/awow/`), which is where transcoding actually works.
  Consequence for this gate: the **no-`/dev/dri`** branch of
  `provision-compose-overrides.sh` is NOT exercised by a Hyper-V run — it is
  covered by that script's own direct tests instead (`RENDER_NODE=` override).
- **No data drives** — `provision-mounts.sh` finds no fstab fragment on a sim
  build and skips, so `MEDIA_ROOT` is a plain directory on the VM's system disk.
  The mount-before-compose ordering (step 3b) is therefore exercised only as a
  no-op here; the shadowing failure it prevents needs real drives to reproduce.

---

## 9. Teardown

```powershell
.\vmtest\Remove-HomeHubVm.ps1                 # stop + remove VM + delete its VHDX
.\vmtest\Remove-HomeHubVm.ps1 -KeepDisk        # keep the VHDX (e.g. to re-attach later)
.\vmtest\Remove-HomeHubVm.ps1 -WhatIf          # preview only
```

Idempotent — running it against a VM that doesn't exist is a no-op, not an
error.

---

## 10. Files in here

```
vmtest/
  README.md               this file
  export-images.sh        Q10.9 B+: docker save every pinned stack image -> .out/images/*.tar
  export-apt.sh           §0a: every .deb the image installs -> .out[-wall]/apt/, proven offline
  build-seed.sh           HUB, LIGHT path: stock ISO + CIDATA seed ISO (folds in the image payload)
  build-repacked-iso.sh   HUB, HEAVIER path: one self-contained ISO (fallback; folds in the payload)
  build-wall-seed.sh      WALL PANEL seed ISO (§11) — the second image target, SR-017
  test-wall-builder.sh    do the wall builder's 26 guards actually bite? (§11)
  test-wall-artifact.sh   is the image's package list SUFFICIENT for the shell? (§11, needs docker)
  test-hub-seed.sh        do the HUB seed's refusals actually bite? (needs root)
  lib/common.sh           shared rendering + the payload stagers (sourced, not run directly)
  Run-V3Gate.cmd          right-click "Run as administrator" wrapper for New-HomeHubVm.ps1
  New-HomeHubVm.ps1          create the Hyper-V VM (elevation required; NOT run by an agent)
  Remove-HomeHubVm.ps1       companion teardown (elevation required; NOT run by an agent)
  New-A19Lab.ps1             §12: the A19 Internal switch + the host's address on it
  Start-A19Gate.ps1          §12: the three-stage two-VM gate (Lab -> Hub -> Panel)
  Watch-VmConsole.ps1        console capture on a loop, budgeted in hours, never silent
  .out/                   gitignored — everything the HUB build scripts generate
  .out/images/            gitignored — the docker-save image tars + manifest
  .out/wall/              gitignored — everything the WALL builder generates
```

---

## 11. The WALL PANEL image (SR-017 / E0)

The repo builds **two** images. Everything above is the hub. This is the
office wall panel: one fullscreen app under `cage`, no Docker, no stack.

```sh
# in WSL (Ubuntu). Needs the OfficeWallNaglight artifact to exist first:
#   cd ../OfficeWallNaglight && npm install && npm run dist
bash vmtest/build-wall-seed.sh
# -> vmtest/.out/wall/wall-seed.iso  (~112 MB — it carries the 111 MB shell tarball)
```

Then the same VM script as the hub, with a different name and disk (it is fully
parameterised — there is no separate wall VM script, and there should not be):

```powershell
# Elevated PowerShell
.\vmtest\New-HomeHubVm.ps1 -VMName Wall-VMTest `
    -VMPath D:\HyperV\Wall-VMTest `
    -UbuntuIsoPath D:\iso\ubuntu-24.04.4-live-server-amd64.iso `
    -SeedIsoPath   .\vmtest\.out\wall\wall-seed.iso `
    -MemoryGB 4 -CPUCount 2 -DiskGB 32
```

The GRUB one-time edit (§6) applies identically — type the single word
`autoinstall`, nothing else.

> **`wall-seed.iso` is named that way for a reason.** Both targets' seeds are
> labelled `CIDATA` — they have to be; that label is how cloud-init finds them —
> so the label cannot tell them apart. Attach `seed.iso` to the panel VM and you
> will install a hub. The filename is the only thing between you and that.

### What "success" looks like on the panel

```sh
journalctl -t wall-kiosk          # the kiosk session
journalctl -u wall-firstboot      # the quirk config + the IF-005 report
```

- `wall-kiosk` shows **`starting: cage -- /opt/wall-panel/app/wall-shell --disable-gpu`**
  and **not** the `NOT INSTALLED` screen.
- `wall-firstboot` shows `IF-005: shell artifact present` and
  `IF-005: ldd resolves every library the Electron runtime needs`.
- `wall-sync.service` is **FAILED**, and that is correct — see the deltas below.

### Wall-specific deltas (do not mistake these for bugs)

- **No Wi-Fi, by substitution.** The shipped `user-data` declares `wifis:`
  because the R5-471T has no RJ45. Hyper-V cannot emulate a radio and the
  *installer* needs the network for apt, so the sim swaps the whole block for
  the hub's `e*` ethernet matcher. **A gate run this way proves the kiosk,
  identity and render path and proves NOTHING about the Wi-Fi path** — not
  `macaddress: permanent`, not powersave-off, not the DHCP reservation the
  kiosk site's `/32` allow-list is keyed to. Those stay hardware-only (C7).
- **`WALL_HOST` defaults to something that cannot resolve** —
  `wall.vmtest.sim.invalid`, on purpose, so a sim panel can never accidentally
  point at a real host. It also means **the default build cannot reach any hub**:
  the A19 gate MUST override it with a name the hub's Technitium answers for.
  That is not optional polish, it is the difference between a panel that renders
  and a panel that shows a connection error.
- **Three more SIM `wall.env` values are deliberate.** `SLEEP_MODE=backlight`
  (a VM that suspends itself at 22:00 is indistinguishable from a VM that
  died — and with no `/sys/class/backlight` in a guest, backlight mode is inert
  and the screen stays up for a capture); `WALL_APP_CMD=… --disable-gpu`
  (`hyperv_drm` gives `/dev/dri/card1` with no `renderD*`, so hardware GL has
  nothing to bind to); `WALL_DISABLE_INPUT` **empty** (quirk 3 would disable the
  synthetic keyboard and mouse, i.e. the console you need for the GRUB edit).
- **The `WIFI_*` values are filled but pointless — not "unused".** They are
  non-placeholder so `wall-firstboot.sh` does not warn about `REPLACE_WITH`, and
  firstboot then *does* render `/etc/netplan/60-wall-wifi.yaml` from them, for a
  `wl*` device that does not exist in a VM. It is inert (firstboot does not
  `netplan apply`, and NetworkManager simply never activates a missing
  interface), but it is a file on the box, so do not be surprised by it.
- **`wall-sync.service` FAILS at boot.** BOTH `MEDIA_MUSIC_SHARE_UNC` and
  `MEDIA_FRAME_SHARE_UNC` point at obviously-invalid `.invalid` hosts because sim
  hub builds skip Samba. Frame video and the local music library are **out of
  scope** for this gate — a green sync unit with no media would be a lie. Note
  the two flows fail differently even here: the music mount is refused and the
  unit fails, while the frame flow's reachability probe finds nothing answering
  and *skips* — reporting that the panel has never completed a frame sync, which
  is the staleness ladder doing its job (OI-18).
- **The panel needs a hub to render anything.** The renderer is served by the
  hub's kiosk site (same-origin: NagLight sends no CORS headers), so the hub ISO
  must have been built **with** the site payload — `build-seed.sh` logs
  `deploy-payload/wall-site/ = …` when it was. Both halves carry the same source
  commit; a hub and a panel whose stamps differ are a mismatched deploy.
- **Networking for the A19 two-VM gate is not the Default Switch.** The kiosk
  site's guard is `remote_ip {$PANEL_IP}/32`, which needs a known panel address,
  and the Internal switch has no DHCP. That is the next session's work — note
  that the *installer* still needs internet, so the panel VM wants the Default
  Switch attached during the install or a second adapter.

### Override the SIM values

```sh
WALL_ENV_OVERRIDES='WALL_HOST=wall.home.arpa
WALL_PORT=8443' bash vmtest/build-wall-seed.sh
```

Same discipline as `SIM_ENV_OVERRIDES`: a key that is not already in
`wall.env.example` **fails the build** rather than appending a line nothing
reads. `WALL_SHELL_DIST=` points at a `dist/` elsewhere;
`ALLOW_MISSING_SHELL=1` builds the image layer alone, loudly.

### Building a REAL panel image

Personal's `Materialize-Deploy.ps1 -Image wall` writes `user-data.filled`,
`wall.env` and `cifs-frame.creds` into `homelab\deploy\out\wall`. Point the builder at that directory and it takes them
verbatim instead of substituting anything:

```sh
WALL_SITE_DIR=/mnt/c/Projects/Personal/homelab/deploy/out/wall \
  bash vmtest/build-wall-seed.sh
```

The result **carries the real Wi-Fi PSK and the panel's real disk pin — treat
the ISO as a secret artifact, and note that it WILL wipe a disk matching that
pin.** Five guards stand between you and a bad one, and all five refuse rather
than warn: a `WALL_SITE_DIR` with no `user-data.filled` (staging the real
`wall.env` onto a sim-substituted `user-data` is the silent downgrade — a
"production" stick with `allow-pw: true` and a known sim password); no
`wall.env`; `allow-pw: true`; a `storage.layout.match` of `model: Virtual_Disk`
(the SIM pin — on the real panel it matches nothing and every install halts with
nothing on screen); and a network block with no `wifis:` (the panel has no RJ45,
so that image would come up unreachable).

**ONE credential file since Q-S7 (2026-08-05), and both a stale single
`cifs.creds` AND a stale `cifs-music.creds` are REFUSED.** The panel mounts two
media sources on two hosts, but only one of them asks it to authenticate:
`cifs-frame.creds` is the Mini-serv `share` account, and the music mount reads
`//homehub/Media`, which is now an ANONYMOUS read-only share — it presents no
credential and no file is emitted for it. The builder stages the frame
credential when it is there and names its absence.

Two stale shapes are refused rather than quietly worked around, because both
would produce a wrong image without saying so:

* a pre-OI-18 single `cifs.creds` — one credential cannot authenticate on two
  hosts, and staging nothing while logging an absence hides that the directory
  is stale rather than incomplete;
* a pre-Q-S7 `cifs-music.creds` — that file is a HOMEHUB Samba password, and
  baking it would put a credential for the box holding the private document
  trees onto a wall-mounted panel, to authenticate a mount that no longer asks
  for one.

Re-run the emitter in either case.

**There is no remaining gap on Personal's side.** Until 2026-08-05 this section
recorded one: the store key `PanelMusicCifsCredential` had no value and
`-Image wall` refused with exactly one violation naming it. The Owner ruled the
`Media` share anonymous (storage-map Q-S7), which removed the need for the
account rather than filling it. The key is retired and `-Image wall` resolves
every knob.

### Testing the builder itself

```sh
bash vmtest/test-wall-builder.sh    # 26 cases — do the guards actually bite?
sudo bash vmtest/test-wall-builder.sh   # …including the 6 wall-sync flow cases
                                        #   (they take a /run lock and call mount)
bash vmtest/test-wall-artifact.sh   # needs docker: is the package list SUFFICIENT?
```

The second one is the interesting one. It reads the package list out of the wall
`user-data`, installs exactly that into a bare `ubuntu:24.04`, unpacks the
artifact **as root with tar**, and checks that `chrome-sandbox` is still
`4755 root:root`, that `[ -x …/wall-shell ]` holds, and that `ldd` resolves every
library. Neither is wired into `scripts/check.py` — both need WSL plus docker,
which the Windows harness does not have. Run them by hand when the artifact,
the package list, or the dependency table changes.

---

## 12. The A19 two-VM lab — hub and panel on one switch

Everything above builds and boots **one** VM at a time. A19 is the gate where
both run together: the hub serves the kiosk site, the panel renders it, and the
assertion is that two RED drive checks are visible **on the panel's screen**.

Three things make that not-just-boot-two-VMs, and each has a knob here.

### 12.1 The addressing plan (one place; everything else reads it)

| | address | wan MAC (Default Switch) | lab MAC (`A19-Lab`) |
|---|---|---|---|
| host (Windows) | `10.99.7.1/24` | — | — |
| hub | `10.99.7.10/24` | `00:15:5D:A1:90:10` | `00:15:5D:A1:91:10` |
| panel | `10.99.7.50/24` | `00:15:5D:A1:90:50` | `00:15:5D:A1:91:50` |

`10.99.7.50` is `PANEL_IP`, and the kiosk site's `remote_ip {$PANEL_IP}/32`
guard is why static addressing is required rather than merely tidy: the Default
Switch is NAT with quasi-random leases, and a `/32` allow-list keyed to a lease
that moves is not a guard at all.

**Every VM keeps a Default Switch leg.** `A19-Lab` is Internal — no uplink, no
DHCP server, no route off it, which is what §3 asks for — but the *installer*
still has to fetch 40 packages. Two NICs, planned before the VM is created, not
after apt exits 100.

**The guest can only tell the legs apart by MAC.** Both come up as `eth0`/`eth1`
in an order VMBus decides, so the sim netplan matches on `macaddress:` and
Hyper-V is told to use exactly those. That is two places holding the same three
values, so the builder writes `$OUT_DIR/a19-lab.env` and `Start-A19Gate.ps1`
reads it — nobody retypes a MAC.

### 12.2 Build the two ISOs

```sh
# HUB. LAN_IP is the hub's lab address (compose binds WALL_PORT to it, and
# Technitium's split-horizon A records point at it). EXTRA_SUBDOMAINS=wall is
# what makes wall.vmtest.sim resolve.
#
# `.sim`, NOT the sim default `.invalid`, and the difference is not cosmetic:
# systemd-resolved synthesises NXDOMAIN for anything under `invalid` (RFC 6761
# §6.4) WITHOUT ever querying the link's DNS server. Technitium answers
# correctly and the panel still fails — measured on a real boot, 2026-08-04.
# The builder now REFUSES a lab build under .invalid / .localhost / .local.
OUT_DIR=/mnt/d/vmtest-out-hub-a19 \
SIM_LAB_WAN_MAC=00:15:5D:A1:90:10 SIM_LAB_MAC=00:15:5D:A1:91:10 \
SIM_LAB_ADDR=10.99.7.10/24 \
SIM_ENV_OVERRIDES='DOMAIN=vmtest.sim
DNS_HOSTNAME=dns.vmtest.sim
LAN_IP=10.99.7.10
WALL_HOST=wall.vmtest.sim
WALL_PORT=8443
PANEL_IP=10.99.7.50
PANEL_USER_SUB=sim-user-wallpanel-0003
EXTRA_SUBDOMAINS=wall
TRACKER_MULTI_USER=true
TRACKER_COMMIT=false
TRACKER_SEED_DIR=/seed
TRACKER_SEED_SRC=../sim/tracker-seed' \
bash vmtest/build-repacked-iso.sh --src-iso /mnt/d/iso/ubuntu-24.04.4-live-server-amd64.iso

# PANEL. SIM_LAB_DNS/SEARCH point the panel at the hub's Technitium — SCOPED by
# a search domain, so `archive.ubuntu.com` still resolves over the NAT leg
# during the install and only lab names go to a box that does not exist yet.
OUT_DIR=/mnt/d/vmtest-out-wall-a19 \
SIM_LAB_WAN_MAC=00:15:5D:A1:90:50 SIM_LAB_MAC=00:15:5D:A1:91:50 \
SIM_LAB_ADDR=10.99.7.50/24 \
SIM_LAB_DNS=10.99.7.10 SIM_LAB_SEARCH=vmtest.sim \
WALL_SIM_HOST=wall.vmtest.sim \
WALL_SHELL_DIST=/mnt/c/Projects/OfficeWallNaglight/dist \
bash vmtest/build-repacked-iso.sh --target wall --src-iso /mnt/d/iso/ubuntu-24.04.4-live-server-amd64.iso
```

### 12.3 Run it — three stages, in order

```powershell
# Elevated PowerShell, from the MiniPC-Deployer checkout
.\vmtest\Start-A19Gate.ps1 -Stage Lab                 # the Internal switch + the host's address
.\vmtest\Start-A19Gate.ps1 -Stage Hub   -Force -Watch # ~20-30 min
# …wait for the hub to answer ssh and its containers to come up…
.\vmtest\Start-A19Gate.ps1 -Stage Panel -Force -Watch # ~45-60 min
```

**Do not overlap the two installs.** Measured 2026-08-04: two VMs on one host
and one disk turned a 45-60 minute wall install into ~2h10m. They are
independent in logic, not in cost.

`-Watch` opens `Watch-VmConsole.ps1` on the VM being installed. Its budget is
hours and every exit path says why it stopped — the scratch watcher it replaces
gave up silently at 90 minutes during a 2h10m install, and silence looked
exactly like a hung VM.

### 12.4 Three sim deltas this gate introduces, all stated rather than hidden

- **TLS is an internal CA.** The sim `DOMAIN` ends in `.invalid` (RFC 6761), so
  public ACME can never validate it and the kiosk site has never served TLS on a
  gate VM — Caddy was healthy and serving nothing. The sim payload's Caddyfile
  now gets `local_certs`; the tracked file keeps public ACME.
- **The panel therefore ignores certificate errors.** Nothing can put that root
  into the panel image: it does not exist until the hub's first boot, which is
  after the ISO is written. So the sim `WALL_APP_CMD` carries
  `--ignore-certificate-errors`, **this gate proves nothing about TLS trust**,
  and a production `wall.env` carrying that flag is refused by the builder.
- **The tracker is seeded from a fixture.** `sim/tracker-seed/definitions/` is
  the only committable thing that declares `library-mounted` and
  `backup-drive-mounted`. Without it the gate hub reports `healthy` with an
  empty `/data` (the healthcheck probes `/healthz`, which is data-free by
  design), and the panel renders a tracker with no items — where "no red drive
  check" and "the drive check is green" look identical on a wall. Seeding is
  **off** by default; `TRACKER_SEED_DIR` blank means the entrypoint passes no
  `--seed` at all.
