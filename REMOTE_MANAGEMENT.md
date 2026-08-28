# Remote management of the headless hub box (WI-10.12)

The AWOW AK41 is **headless** and the Owner never wants to physically visit it
(Q10.7 + the 2026-07-03 requirement: "trivial remote debugging/resolution is a
KEY PIECE, not a nicety"). This memo has two parts:

1. **Implemented now** — the day-to-day remote-ops surface (SSH, Cockpit,
   unattended-upgrades, the remote `docker compose` workflow). No decisions
   needed; it ships in the autoinstall.
2. **A decision memo** — the **reimage-over-LAN ladder** for the worst case (a
   box too broken to fix over SSH). Every option is presented with a checkbox.
   **Nothing destructive / reimage-related is implemented until the Owner checks a
   box** — that is the high-risk line this repo will not cross unattended.

---

## Part 1 — Implemented now (day-to-day remote ops)

Provisioned by `stack/autoinstall/user-data`:

- **SSH, key-only.** `ssh.allow-pw: false` + `authorized-keys`; the operator
  account's password is locked (`"!"`). No password login surface at all.
  - Fill your public key into `user-data` before flashing (the box is
    unreachable over SSH until a valid key is present — that is deliberate).
- **Passwordless `sudo`**, and it is the third leg of the same design rather
  than a separate concession. `/etc/sudoers.d/90-homehub-ops` grants
  `hub ALL=(ALL) NOPASSWD:ALL`; the wall image installs the equivalent for
  `panel`. **Added 2026-08-07 — until then it was missing, and both boxes were
  read-only over SSH.** A locked password means `sudo` asks for something that
  cannot exist, so `apt`, `systemctl`, every `/etc` edit and even
  `healthcheck.sh` (it must read a root-only `.env`) all failed with
  *"sudo: a password is required"*. Cockpit and the console were dead for the
  same reason — both are PAM against that same locked hash.
  - **This is the standard cloud-image posture, and the image had two of its
    three parts.** Ubuntu's own `/etc/cloud/cloud.cfg` ships
    `lock_passwd: True` together with `sudo: ["ALL=(ALL) NOPASSWD:ALL"]`, and
    AWS, GCP, Azure, DigitalOcean and Hetzner all image that way. Key-only SSH,
    a locked password and passwordless sudo are one design: **the SSH key is
    the credential**, and a password behind it protects nothing from anyone who
    already holds the key — while making unattended maintenance impossible.
    Subiquity's `identity:` block cannot express a sudoers rule, which is how
    the third part got dropped without anyone choosing to drop it.
  - **On the hub this changed no exposure.** `hub` is in the `docker` group and
    the docker socket is root by design — `docker run -v /:/host` already read
    and wrote anything, with no password. What it adds is **auditability**:
    `sudo` journals every command, where the docker path logs only that a
    container started. On the **panel** it is the difference between
    maintainable and reimage-only: no docker group, no Cockpit, so its previous
    only root path was a keyboard at the wall and `init=/bin/bash` at GRUB —
    which meant `systemctl reboot`, the one action unattended-upgrades
    periodically requires, could not be performed remotely at all.
  - The drop-in is validated with `visudo -cf` **before** it is installed. A
    malformed sudoers file breaks `sudo` for every user on a box whose only
    account has a locked password, which is exactly the no-way-in state that
    cost a GRUB rescue on 2026-08-06.
- **Cockpit web console** (host package, not a container — the plan's preferred
  form) on `https://<LAN_IP>:9090`: terminal, logs, service control, updates,
  reboot, metrics. **LAN-only** — it is *not* proxied through Caddy to the
  internet, and the router must not forward :9090.
- **unattended-upgrades** — hands-off OS **security** patching so the box stays
  current without a visit. (Kernel/livepatch and reboot-on-kernel-update are a
  separate opt-in; unattended-upgrades handles security packages by default.)
- **Dozzle** (WI-10.11) on `http://<LAN_IP>:8081` — live container logs in a
  browser, no SSH needed. **Uptime-Kuma** on `http://<LAN_IP>:3001` watches the
  services and can alert.

### The remote workflow (from the Owner's workstation, over the LAN)

```sh
# 1. Get on the box
ssh hub@<LAN_IP>              # key-only

# 2. Inspect
cd /opt/homehub/stack
docker compose ps                  # health of every service
docker compose logs -f caddy       # or use Dozzle in a browser
bash provision/healthcheck.sh --env .env

# 3. Update / restart the stack
git -C /opt/homehub pull          # if the box carries a repo checkout
docker build -t naglight:local ../NagLight   # if updating the tracker image
docker compose pull                 # refresh the stock images
docker compose up -d                # apply — restart:unless-stopped keeps them up
docker compose restart oauth2-proxy # after editing the allow-list

# 4. Reconverge DNS / config after an .env edit (idempotent)
sudo /usr/local/sbin/homehub-firstboot.sh

# 5. Enable / disable a tier-2 opt-in service (stack/README §9, SR-012)
$EDITOR .env                        # add/remove the profile in COMPOSE_PROFILES
docker compose up -d                # enable: pulls the image, starts it
docker compose up -d --remove-orphans   # disable: removes de-profiled containers
```

For anything a browser can do instead of a shell: **Cockpit** (system) and
**Dozzle** (logs) cover "navigate, read logs, accept updates, reboot" without a
terminal.

### State & credentials — what survives an update vs a reimage

The question this answers: *"will updating containers trash my credentials?"*
**No — no credential lives inside a container or image.** Containers are
disposable code; everything stateful sits in exactly two places on the host:

| Where | What lives there | container update (`compose pull` + `up -d`, or a pin bump) | reimage (USB re-flash) |
|---|---|---|---|
| **Named Docker volumes** | Actual's server password + **SimpleFIN bank-sync credential** + budget files (`actual_data`) · Technitium zones/settings (`technitium_config`) · Caddy certs + ACME account (`caddy_data`) · tracker user data (`tracker_data`) · Vaultwarden vault + other tier-2 state | **survives** — pull/up recreates containers *around* unchanged volumes | **wiped** — restore from the volume backups (the `volume:` lines in `backup.env`, SR-013 + `restore.sh`), or re-do the small set of one-time in-app auths |
| **Host config files** | `/opt/homehub/stack/.env` (OAuth client secret, Cloudflare token, Technitium admin password, basic_auth hashes) · `oauth2-proxy/authenticated-emails.txt` · `provision/.token` · `/etc/homehub-backup/{backup.env,cifs.creds}` | survives | `.env` + the allow-list are **re-seeded from the USB payload** (carry your filled `.env` on the stick); `provision/.token` re-mints itself idempotently; `/etc/homehub-backup/*` must be restored by hand |

Consequences worth internalizing:

- **Individual container updates are credential-safe by construction.** The §3
  workflow above (`compose pull && up -d`, or bumping one `*_IMAGE_TAG` in
  `.env`) never touches a volume — including Actual: the SimpleFIN link,
  server password, and budgets all ride `actual_data` untouched.
- **One-time in-app authentications live server-side in volumes, not `.env`:**
  Actual's SimpleFIN credential is entered in Actual's own UI (stack/README §2
  manual steps) and stored in `actual_data`. Volumes are portable — an
  `actual_data` backed up from the V3.5 rehearsal VM restores onto the real
  box, so "authenticate once" can genuinely mean once.
- **A reimage is the deliberate destructive case** — the disk (volumes
  included) is wiped. The recovery story is: the USB payload re-seeds `.env`,
  the baked images re-load, and the volumes come back from the SR-013 backup
  drive — which is exactly why the stack's own volumes belong in
  `BACKUP_SOURCES`.

### What runs where (containers vs host)

On the hub, **every service is a container** except these host-level pieces:
the **backup service** (pure bash + systemd timer — deliberately not a
container, it mounts cifs and manages drives), the **powertune** and
**backup-standby** per-boot oneshots, **Cockpit**, **sshd**,
**unattended-upgrades**, and Docker itself — plus, **only if opted in**, the
SR-015 light RDP layer below. The **wall panel** runs no containers at all (see
"reimage-not-repair" below). **Mini-serv** (the Windows box) runs nothing
from this stack — it serves **one** Samba share that the backup service
**ingests** (mirrors into this box's library tree, ratified 2026-07-29), and it is
allowed to **sleep**: the backup wakes it over the LAN (Wake-on-LAN) and fails
loudly if it does not come up. It has **no offsite role** any more — the IceDrive
client runs here, in the opt-in desktop session, pointed straight at chosen
library paths (Owner 2026-07-29, correcting OI-11: the backup service stages
nothing for it).

### Opt-in: light remote desktop (RDP) for GUI-only vendor apps (SR-015)

**SHIPPED OFF BY THE DEPLOYER, ACTIVATED BY HOMEHUB (2026-08-27).** Some vendor
apps have no headless mode, and the **IceDrive Mount & Sync** client is the
first case. IceDrive *does* also ship a Linux CLI — it works, and it stays
available as `ICEDRIVE_MODE='cli'` — but it is a *mount* client rather than a
*sync* client and the vendor documents it almost not at all, so the Owner chose
the supported GUI: *"if there is no documentation of this, it's likely safest
just to drop back to the desktop."*

`stack/remote-ui/setup-remote-ui.sh` installs an xrdp + minimal-XFCE layer, and
`homehub-desktop-session.service` creates a session **at boot with nobody
connected** so the GUI client runs unattended — the sanctioned SN-001 exception:
secondary services may need UI configuration, but it is always remote and
minimized.

**MiniPC-Deployer defaults it OFF.** A hub built straight from this repo has no
X and nothing on tcp/3389. HomeHub's `config.homehub.psd1` sets
`REMOTE_UI_ENABLED` / `ICEDRIVE_MODE`, and that ONE declaration drives both what
the USB carries and what firstboot activates — `Materialize-Deploy.ps1` refuses
to emit an activation whose carriage is absent. That gate exists because the two
halves silently disagreed for a month: eleven of the AppImage's runtime
libraries were in no image at all, so "RDP in and sign in to IceDrive" was an
instruction nobody could follow. See
[stack/remote-ui/README.md](stack/remote-ui/README.md).

**LAN-only like Cockpit** — never proxied through Caddy, never port-forwarded.

> **WireGuard is the later remote-access answer** (D5): once set up, the same LAN
> workflow works from anywhere. Until then this is LAN / VPN-to-LAN only — do not
> expose SSH, Cockpit, or the aux UIs to the public internet.

### The wall panel is REIMAGE-NOT-REPAIR (SN-013/SR-017, 2026-07-29)

Everything above is about the hub box, which holds state worth protecting. The
**office wall panel** — this repo's second image target — is the opposite, and
the difference is deliberate rather than an oversight:

- **It holds nothing.** A thin client with a disposable media cache. There is no
  volume to back up, and it is not in `BACKUP_SOURCES`.
- **So the recovery ladder is one rung: reflash it.** Do not spend time repairing
  a panel. Rebuild the USB, reimage, restore the one-time Pandora sign-in from
  its runbook, done. That is why the reimage-over-LAN decision memo below —
  written for a headless box you cannot afford to lose — does not need to grow a
  panel column.
- **It gets a lighter management surface on purpose:** SSH (key-only) and
  unattended-upgrades, but **no Cockpit** — one less always-listening web surface
  on a box that is asleep for part of the day anyway.
- **It is unreachable while it sleeps.** `SLEEP_MODE=suspend` means no LAN
  presence at all during the window: no SSH, no fixing it, until the RTC alarm
  fires. That is the accepted cost of L2 (D-W4), and the RTC alarm is what bounds
  the worst case. If you need it reachable overnight, `SLEEP_MODE=backlight` keeps
  the machine up.
- **After a mains blip it may simply stay off.** No battery (D-W5) means no UPS,
  and this BIOS has no AC-recovery setting — a smart plug restores power but
  cannot press the power button. Recovery is a **physical press**, which is why
  the panel-down alert (an Uptime-Kuma push monitor, configured server-side) is
  required rather than optional, and why the mount must leave the power button
  reachable.

---

## Part 2 — DECISION MEMO: reimage-over-LAN ladder (the Owner checks one)

**The scenario:** the box is so broken that SSH/Cockpit can't fix it (bad kernel
update, corrupted rootfs, botched change) — but the Owner still doesn't want to drive
to it. How do we re-lay-down the known-good autoinstall image **without physical
presence**? Reimaging **wipes the box**, so this is the high-risk line.

**Recommendation:** **Option B (GRUB recovery entry + autoinstall on a recovery
partition)** as the primary, with **Option D (smart-plug power-cycle + USB) as
the always-works fallback**. Rationale below. **None of these is implemented yet
— check the box(es) you want and I (or the next agent) will build exactly that,
nothing more.**

### Option A — PXE / netboot.xyz from another LAN box
Stand up a PXE/TFTP+DHCP-proxy (or run [`netboot.xyz`](https://netboot.xyz)) on
another always-on LAN machine; set the hub to network-boot first; on failure,
netboot into the installer and re-run autoinstall.
- **Pros:** nothing stored on the hub itself; re-imageable even with a wiped
  disk; reusable for other machines.
- **Cons:** needs a second always-on box + DHCP-proxy config (can fight the
  router's DHCP); BIOS must reliably attempt netboot; most setup effort.
- **Firmware cost — priced in 2026-07-26.** This is the only option that
  depends on a **firmware** setting (the UEFI network stack / PXE), and
  firmware is the one layer no remote path can reach: there is no vendor tool
  to change this box's BIOS from Linux, and the generic alternatives are a good
  way to brick a mini PC. So if the network stack is ever disabled, re-enabling
  it for this option costs a physical visit — which is precisely what the
  ladder exists to avoid. **Also note the circularity here:** the "second
  always-on LAN box" serving TFTP would have to be something *other* than the
  target, and in this homelab the target IS the always-on box; the remaining
  candidates are the legacy machine being retired and a panel that sleeps
  half the day. Options B–D depend on no firmware settings at all.
- [ ] **Build Option A.**

### Option B — GRUB "reinstall" entry seeded from a recovery partition  ★ recommended primary
Carve a small **recovery partition**, store the Ubuntu autoinstall ISO +
`user-data` there, and add a **custom GRUB menu entry** that boots it and runs
the unattended install against the main disk.
- **Pros:** entirely self-contained on the hub (no second box); triggered
  remotely by `grub-reboot "Reinstall"` + `reboot` over SSH; survives a trashed
  root as long as GRUB + the recovery partition are intact.
- **Cons:** the storage layout (currently whole-disk LVM) must reserve the
  recovery partition **at install time** — a change to `storage:` in
  `user-data`; doesn't help if the disk itself dies; the GRUB entry is fiddly to
  get right and is **destructive** when triggered.
- [ ] **Build Option B.** (Requires changing the autoinstall `storage:` layout to
      reserve a recovery partition — a HIGH-RISK change I will not make unattended.)

### Option C — Second bootable rescue disk / A-B root
Keep a second small OS (or an A/B root pair) the box can fall back to, from which
a script re-images the primary.
- **Pros:** very robust; the rescue system is independent of the main root.
- **Cons:** most complex; needs a second disk or a partitioning scheme + boot
  logic to maintain; overkill for a single hobby box.
- [ ] **Build Option C.**

### Option D — Smart-plug power-cycle + USB fallback  ★ recommended fallback
A Wi-Fi smart plug (e.g. Tasmota/Home-Assistant-controlled) power-cycles the box
remotely; a **pre-inserted autoinstall USB** (BIOS set to boot USB first, or a
one-time boot override) re-images on the forced reboot. Remove/relabel the USB
after a successful install so it doesn't loop.
- **Pros:** dead simple; no second server, no partition surgery; the smart plug
  is independently useful (hard-reset a hung box). Always works if a USB is left
  in.
- **Cons:** requires a USB physically present in the box (one-time visit to
  insert it) and BIOS boot-order cooperation; the smart plug is another device on
  the LAN; a stuck installer can loop until the USB is pulled.
- [ ] **Build Option D** (document the smart-plug + USB procedure; optionally a
      small helper to prep the USB).

### Option E — `kexec` straight into the installer, nothing staged on the target disk
**PROVEN ON THE REAL HUB, 2026-08-28.** The AK41 was reimaged end to end over
Ethernet - triggered from an SSH session, no boot media, no reserved partition,
nothing staged on the disk being wiped. Jump at 23:06:46, installed and
answering on the operator key at 23:24:07 (17 minutes), `assert-installed.sh`
section 5c **ALL CHECKS PASSED**, `/srv/library` and `/mnt/backup-drive` intact.

**IT DOES NOT WORK WITH THE STOCK INITRD, AND THE REASON IS ONE LINE OF SHELL.**
casper parses `nfsroot=`, `netboot=`, `ip=`, `uuid=`, `toram=` and friends off the
kernel command line - and has **no `nfsopts=` case at all**. So `do_cifsmount`
always takes its hardcoded fallback,

    CIFSOPTS="-ouser=root,password="

a Windows share answers `STATUS_LOGON_FAILURE`, casper falls through to NFS,
finds no server, and panics with *"Unable to find a live file system on the
network"*. Every attempt before the fix failed exactly there, identically on real
hardware and in a VM - which is the signature of a configuration cause rather
than a hardware one. Measured from the lab VM's console:

```
Trying mount.cifs //192.168.117.243/HubISO /cdrom -ouser=root,password= ...
CIFS: Status code returned 0xc000006d STATUS_LOGON_FAILURE
mount error(13): Permission denied
Trying nfsmount ... nfsmount: need a server        (x40)
```

**THE FIX IS 2 KB AND CARRIES NO SECRET.** `scripts/functions run_scripts()`
SOURCES the hook directory's `ORDER`, and `ORDER` sources `/conf/param.conf`
after each hook - casper's own channel for a hook to set variables in its scope,
running inside `mountroot()` before the netboot block. A prepended cpio segment
adds that file; it copies `cifsopts=` off the command line into `NFSOPTS`. The
credential still rides the cmdline, where it already had to live.
See [remote-reimage/](remote-reimage/) for the builder and the trigger.

The rung this ladder was missing, and the one that answers "reimage without
repartitioning" directly. The **running OS** loads the installer's kernel and
initrd into RAM and jumps into them (`kexec -l` / `kexec -e`) — no reboot
through firmware, and **nothing read from the disk**. Subiquity is then free to
wipe the whole disk, including where the kernel came from, because it is already
in RAM.

**The framing that makes this work:** the requirement was never *a partition*.
It is that **the installer's root must not live on the disk being written**.
Exactly four things can satisfy that — RAM, the network, another device, or a
reserved region. Options A/D/B/C pick three of them; this picks the first two
and reserves nothing.

- **Pros:**
  - **No `storage:` change.** Nothing reserved, nothing to preserve — the
    single highest-risk edit on this ladder is avoided entirely.
  - **No firmware dependency, which is the whole objection to Option A.** The
    UEFI network stack is never used; the *running OS* does the netboot. It
    also dodges A's circularity — no always-on TFTP box, since the source can
    be any machine that is up at the moment you trigger it.
  - **Fully SSH-triggerable**, like B, with no fiddly GRUB entry that can wipe
    the box on an ordinary reboot.
  - **Retest is a loop you would actually use:** drop a new ISO on the source
    host, run one command over SSH, wait.

- **Cons, as measured rather than predicted:**
  - **THE SHARE IS THE LIVE ROOT FOR THE WHOLE INSTALL.** casper does not copy
    the medium into RAM - it loop-mounts the layered squashfs stack straight off
    the share and runs from it. Measured on the server side mid-install: four
    open handles on `casper/ubuntu-server-minimal*.squashfs`. **So the machine
    serving the share must stay awake for the entire run.** If it sleeps or the
    switch drops after partitioning has begun, that is the one path to a
    genuinely half-wiped disk. Everything else fails safe.
  - **`kexec_file_load` faults on 6.8.0-138 once the IMA measurement list grows.**
    The oops is `ima_measurements_show -> ima_dump_measurement_list ->
    ima_add_kexec_buffer`, and `kexec` is SIGKILLed. It is **cumulative, not
    random**: a fresh boot loads fine, and each load grows the list until it
    faults - measured 2026-08-28 succeeding on a 1-minute-old boot and failing on
    the very next call. **The rule is: reboot, then jump once.** 6.8.0-100 was
    reliable across several loads. This will bite again after a kernel upgrade,
    and it presents as a bare `Killed` with no explanation unless you read dmesg.

    **AND THE FAULT POISONS THE WHOLE BOOT, WHICH IS WORSE THAN THE SIGKILL.**
    The faulting task dies **holding `kexec_lock`** (`note: kexec[...] exited
    with irqs disabled`), so every kexec operation for the rest of that boot
    returns **`EBUSY`** rather than faulting - a different symptom, and a
    misleading one. `kexec_loaded` still reads `0`, so this script's own step 1
    "not already armed" check passes, and the run gets all the way through the
    mount and the 88 MB stage before failing at the load with `Device or
    resource busy` over a trace that names IMA rather than the lock. Measured on
    the hub 2026-08-28: one load succeeded, the very next faulted, and every
    attempt after that - across two launcher runs and a third by hand - returned
    EBUSY until a reboot. **Only a reboot clears it.**

    **The exact probe is `cat /sys/kernel/kexec_crash_size`.** It takes the same
    mutex, so it returns a number on a healthy boot and `Device or resource busy`
    on a poisoned one; it costs one round trip and arms nothing. Worth checking
    BEFORE a run rather than diagnosing after one.
  - **THE CONSOLE GOES DARK AND STAYS DARK.** kexec skips firmware POST, so the
    kernel never re-inits the GPU. `nomodeset` did **not** recover it on the
    AK41 (tried). The initramfs writes `casper.log` and never gets to persist it,
    so a failed netboot leaves no evidence anywhere. Diagnosing this cost most of
    an evening; the fix was to reproduce in a lab VM, whose console the lab
    watcher screenshots. **Do not attempt to debug Option E on headless hardware.**
  - **Same precondition as B:** the OS must boot and be reachable. E is a peer of
    B, not a rung below it - it does not cover "deeply broken".

#### Does this change the ISO? No — provided the whole ISO tree is the medium.
The question is worth answering exactly, because the cheap-looking shortcut is
the one that breaks it. Measured from `repacked-gate.iso` on 2026-08-28, every
autoinstall entry seeds from a path **on the mounted medium**:

```
linux  /casper/vmlinuz autoinstall "ds=nocloud;s=/cdrom/nocloud/" ---
initrd /casper/initrd
```

and `/deploy-payload` is found by the same `/cdrom`-first search OI-19
deliberately consolidated to **one** discovery mechanism. Both facts point the
same way:

- **Deliver the whole ISO as the installer's medium** — NFS root, or an
  HTTP-fetched ISO — and `/cdrom` is populated exactly as it is when booting
  from a stick. The seed resolves, the payload search finds `/deploy-payload`
  on its first try, and **the ISO is byte-identical to the one you flash
  today**. `/casper/vmlinuz` and `/casper/initrd` are already on it; E only
  extracts them. The delta is entirely **one kernel cmdline parameter naming
  the medium**, plus a script on the hub and a file server. *On 8 GB, NFS is
  the viable half of this: it streams, where a fetched ISO must fit in RAM.*
- **Do NOT take the `fetch=…/filesystem.squashfs` shortcut.** It pulls only the
  squashfs, so `/cdrom` is never populated — `ds=nocloud;s=/cdrom/nocloud/`
  fails to resolve **and** the payload search misses. That variant *would*
  force a redesign of both the seed reference and the payload discovery, i.e.
  the one part of this repo that was hardest to get right.

**ONE CORRECTION, measured after the above was first written:** the image needs
**`kexec-tools` baked in**. A freshly installed hub has no `kexec` binary, and E
cannot depend on apt-installing it at the moment it is needed — the whole
scenario is a box too broken to fix normally, quite possibly with no working
package management. That is a one-line addition to the baked package list, and
it is a **real, if small, change to the image**. It changes no seed path, no
payload discovery, and no partition layout.

So: **E is a deployment mechanism, not an image redesign** — one extra package
baked in, and otherwise the same ISO you flash today. The only thing that
would push a change back into the build is wanting a *RAM-resident* (atomic,
blip-proof) install, which on 8 GB needs a much slimmer ISO — and slimming it
by fetching packages over the network would forfeit the **offline-install
property the gate deliberately tests** (the runner unplugs the adapter before
every install). If that is ever wanted, the payload must be pulled into RAM
first and installed offline from there; do not trade that property away quietly.

- [x] **Build Option E. BUILT AND PROVEN 2026-08-28.** `remote-reimage/` holds
      the initrd builder and the SSH-triggered script. `kexec-tools` is baked
      into the hub image (`stack/autoinstall/packages.list`), so a hub can always
      start its own reimage. **The credential is no longer a placeholder
      (2026-08-28):** `HubIsoSharePassword` is a HomeHub `FieldSchema.psd1`
      entry, minted into the DPAPI store, and the `hubread` account is set to
      it - so the whole path runs unattended. The share itself is still
      hand-made and still wants documenting as standing infrastructure.

### Why B primary + D fallback

**E changes this calculus, and its Secure Boot prerequisite now holds.** E covers
what B covers (software-broken, SSH-triggerable, no extra hardware) **without**
the `storage:` change that is the riskiest edit here — so the pairing to
consider is **E primary + D fallback, and no B at all**. What B keeps that E
gives up is a medium that cannot fail mid-install; what E keeps that B gives up
is a disk layout nobody had to redesign. D is unchanged either way, and remains
the only rung that survives a dead GRUB and a wiped disk.

B needs no extra hardware and is fully SSH-triggerable, matching "never visit the
box" best — **once** the recovery partition exists. D is the pragmatic safety net
that works even when B's assumptions (intact GRUB, healthy disk) fail, at the
cost of one USB left in the box. Together they cover "software-broken" (B) and
"deeply broken / B's preconditions gone" (D) without the standing infrastructure
of A or the complexity of C.

### Where the remote/physical boundary actually sits
Everything **above** the firmware is remotely manageable once the OS boots:
config, packages, containers, upgrades — and a full reimage too, since a
healthy OS can arm Option B's entry with `grub-reboot` and reboot into it. What
no rung can reach is the **firmware itself**: BIOS settings need a keyboard at
the machine. Two consequences worth designing around:

1. **Batch firmware work into one visit.** Anything firmware-level — boot
   order, Secure Boot state, the UEFI network stack, TPM/PCR-bank settings —
   should be settled while someone is physically there, because the next
   chance is another trip. (TPM PCR banks especially: changing them
   invalidates anything already sealed, so they must be right *before* disk
   encryption is enrolled, not after.)
2. **Every rung here assumes the OS boots and is reachable.** That is the
   real precondition, not any firmware setting. Auto-unlocking disk
   encryption preserves it after an unattended reboot or power cut; a setup
   that needs a passphrase typed at boot does not, and pushes recovery down
   the ladder every time the power blips.

### The hard line (do not cross unattended)
- No change to the autoinstall **`storage:`** layout (recovery partition) is made
  until Option B is checked.
- No GRUB reinstall entry, PXE server, or auto-boot-to-installer is wired until
  its option is checked — a mis-seeded recovery/PXE path can **wipe the box on an
  ordinary reboot**, which is exactly the failure this memo exists to avoid.
- Until then, the safe manual recovery remains: re-flash a USB and boot it
  (stack/README §7 "Rollback → Whole box"). Data lives in named Docker volumes and
  the tracker's remote data repo.

---

### The Owner's decision

- Primary reimage path: [ ] A  [ ] B  [ ] C  [ ] D  [x] **E** (chosen and proven 2026-08-28)
- Fallback: [x] **D** - a written USB stick. Keep one: E's precondition is a
  booting, reachable OS, and nothing on this ladder covers a dead GRUB or a
  wiped disk.
- Notes / constraints: _______________________________________________
