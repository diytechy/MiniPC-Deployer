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

**IT HAS NO TENANT, AND NOTHING INSTALLS IT (2026-08-27).** This layer existed
for the **IceDrive Mount & Sync** client, believed to be GUI-only. That was
never checked and it is false: `IcedriveCLI` is a headless native ELF with a
non-interactive login and a FUSE mount. The Owner removed the GUI from the image
and from the box, along with the boot-time desktop session, the same day.
IceDrive now lives in [stack/icedrive/](stack/icedrive/README.md) and needs no
session at all.

The layer is **kept as an opt-in, not deleted**, for a future vendor app that is
genuinely GUI-only: `stack/remote-ui/setup-remote-ui.sh` installs an
**off-by-default** xrdp + minimal-XFCE layer over the LAN — the sanctioned
SN-001 exception: secondary services may need UI configuration, but it is always
remote, and restricted/minimized. Its packages are baked into the offline apt
repo without being installed (`packages.optional.list`), so the opt-in still
works on a hub with no internet. Nothing in the autoinstall/first-boot path
references it, and **SN-001's zero-click core now carries no exception at all**.
**LAN-only like Cockpit** — never proxied through Caddy, never port-forwarded.
See [stack/remote-ui/README.md](stack/remote-ui/README.md), which also lists the
three claims about IceDrive that turned out to be wrong.

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

### Why B primary + D fallback
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

- Primary reimage path: [ ] A  [ ] **B**  [ ] C  [ ] D  [ ] none yet
- Fallback: [ ] **D**  [ ] other: ______
- Notes / constraints: _______________________________________________
