# Remote management of the headless AWOW box (WI-10.12)

The AWOW AK41 is **headless** and Peter never wants to physically visit it
(Q10.7 + the 2026-07-03 requirement: "trivial remote debugging/resolution is a
KEY PIECE, not a nicety"). This memo has two parts:

1. **Implemented now** — the day-to-day remote-ops surface (SSH, Cockpit,
   unattended-upgrades, the remote `docker compose` workflow). No decisions
   needed; it ships in the autoinstall.
2. **A decision memo** — the **reimage-over-LAN ladder** for the worst case (a
   box too broken to fix over SSH). Every option is presented with a checkbox.
   **Nothing destructive / reimage-related is implemented until Peter checks a
   box** — that is the high-risk line this repo will not cross unattended.

---

## Part 1 — Implemented now (day-to-day remote ops)

Provisioned by `stack/autoinstall/user-data`:

- **SSH, key-only.** `ssh.allow-pw: false` + `authorized-keys`; the operator
  account's password is locked (`"!"`). No password login surface at all.
  - Fill your public key into `user-data` before flashing (the box is
    unreachable over SSH until a valid key is present — that is deliberate).
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

### The remote workflow (from Peter's workstation, over the LAN)

```sh
# 1. Get on the box
ssh operator@<LAN_IP>              # key-only

# 2. Inspect
cd /opt/awow-core/stack
docker compose ps                  # health of every service
docker compose logs -f caddy       # or use Dozzle in a browser
bash provision/healthcheck.sh --env .env

# 3. Update / restart the stack
git -C /opt/awow-core pull          # if the box carries a repo checkout
docker build -t naglight:local ../NagLight   # if updating the tracker image
docker compose pull                 # refresh the stock images
docker compose up -d                # apply — restart:unless-stopped keeps them up
docker compose restart oauth2-proxy # after editing the allow-list

# 4. Reconverge DNS / config after an .env edit (idempotent)
sudo /usr/local/sbin/awow-firstboot.sh

# 5. Enable / disable a tier-2 opt-in service (stack/README §9, SR-012)
$EDITOR .env                        # add/remove the profile in COMPOSE_PROFILES
docker compose up -d                # enable: pulls the image, starts it
docker compose up -d --remove-orphans   # disable: removes de-profiled containers
```

For anything a browser can do instead of a shell: **Cockpit** (system) and
**Dozzle** (logs) cover "navigate, read logs, accept updates, reboot" without a
terminal.

> **WireGuard is the later remote-access answer** (D5): once set up, the same LAN
> workflow works from anywhere. Until then this is LAN / VPN-to-LAN only — do not
> expose SSH, Cockpit, or the aux UIs to the public internet.

---

## Part 2 — DECISION MEMO: reimage-over-LAN ladder (Peter checks one)

**The scenario:** the box is so broken that SSH/Cockpit can't fix it (bad kernel
update, corrupted rootfs, botched change) — but Peter still doesn't want to drive
to it. How do we re-lay-down the known-good autoinstall image **without physical
presence**? Reimaging **wipes the box**, so this is the high-risk line.

**Recommendation:** **Option B (GRUB recovery entry + autoinstall on a recovery
partition)** as the primary, with **Option D (smart-plug power-cycle + USB) as
the always-works fallback**. Rationale below. **None of these is implemented yet
— check the box(es) you want and I (or the next agent) will build exactly that,
nothing more.**

### Option A — PXE / netboot.xyz from another LAN box
Stand up a PXE/TFTP+DHCP-proxy (or run [`netboot.xyz`](https://netboot.xyz)) on
another always-on LAN machine; set the AWOW to network-boot first; on failure,
netboot into the installer and re-run autoinstall.
- **Pros:** nothing stored on the AWOW itself; re-imageable even with a wiped
  disk; reusable for other machines.
- **Cons:** needs a second always-on box + DHCP-proxy config (can fight the
  router's DHCP); BIOS must reliably attempt netboot; most setup effort.
- [ ] **Build Option A.**

### Option B — GRUB "reinstall" entry seeded from a recovery partition  ★ recommended primary
Carve a small **recovery partition**, store the Ubuntu autoinstall ISO +
`user-data` there, and add a **custom GRUB menu entry** that boots it and runs
the unattended install against the main disk.
- **Pros:** entirely self-contained on the AWOW (no second box); triggered
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

### Peter's decision

- Primary reimage path: [ ] A  [ ] **B**  [ ] C  [ ] D  [ ] none yet
- Fallback: [ ] **D**  [ ] other: ______
- Notes / constraints: _______________________________________________
