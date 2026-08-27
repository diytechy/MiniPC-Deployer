# IceDrive on the hub — the CLI, and why the GUI was chosen anyway

**DIRECTION SETTLED 2026-08-27: the hub runs the GUI AppImage
(`ICEDRIVE_MODE='appimage'`), not this CLI.** The CLI is real, it works, and it
stays available as `ICEDRIVE_MODE='cli'` — everything below is what one session
with a live account established about it. It was not chosen because it is a
**mount** client rather than a **sync** client, and because the vendor documents
it essentially not at all. The Owner: *"if there is no documentation of this,
it's likely safest just to drop back to the desktop."*

This page is therefore two things: the record of a road not taken, and the
reference for anyone who takes it later. Everything here was **measured** on
`IcedriveCLI-v3.62` on 2026-08-27 — not read from documentation, which barely
exists. Where something is unproven it says so in those words.

**Neither client is on by default.** MiniPC-Deployer ships IceDrive off;
HomeHub's `config.homehub.psd1` selects `off` / `appimage` / `cli`, and that one
declaration drives both what the USB carries and what firstboot activates. See
[../remote-ui/README.md](../remote-ui/README.md), "The two halves".

---

## The claim that was wrong, and how long it stood

`stack/remote-ui/README.md` and open-items E1 both asserted, for a month, that
IceDrive's Linux client is **"GUI-only — no headless daemon, no CLI"**. Two
things were built on that: an xrdp + XFCE session in the image, and a systemd
unit that logged a graphical session in at boot so the GUI client would keep
running with nobody connected.

There is a CLI. It has existed the whole time. E1's own text records why nobody
found out — *"Nothing can be built until there is a live client to inspect"* —
the claim was inherited and repeated rather than checked, in a project whose
rules say to measure.

| | GUI client | CLI |
|---|---|---|
| file | `IcedriveMounted-v3.62-x86_64.AppImage`, 118 MB | `IcedriveCLI-v3.62`, **9.8 MB native ELF** |
| needs X | yes — and therefore a session, and therefore xrdp | **no** |
| login | typed into a dialog | `-login <user> -password <pw>`, non-interactive |
| mount | FUSE, in the session | FUSE, `-mp <path>` |

## What is proven

Run against the real binary, with no account:

- **It is genuinely headless.** `ldd` resolves **no** X11, xcb, Wayland or Qt
  GUI library — only `libfuse.so.2`, `libz`, `libglib-2.0`, `libstdc++`, `libm`,
  `libgcc_s`, `libc`. It behaves identically with `DISPLAY` unset, with
  `DISPLAY` pointing at a dead display, and with a live one.
- **`-login`/`-password` is fully non-interactive.** With stdin closed it
  reaches the vendor API and returns a real verdict:
  `{"error":true,"code":1005,"message":"Invalid email or password. Excessive
  attempts will result in a 30 minute block (1)"}`.
- **There is no way to keep the password off argv.** Three were tried and all
  three fail with `cli: no password given`: a pipe on stdin, a pty (via
  `script`), and omitting `-password` so it prompts. The pty attempt was re-run
  later with a delay, to rule out a race, and still failed — which matters,
  because the *crypto* prompt in the same binary **does** accept a pty (see
  below). Pre-seeding the config is not available either. See
  `setup-icedrive.sh` for why argv is acceptable *here* and the one condition
  that would change that.
- **`icedrive_sessId` is NOT the persisted session.** The remote-ui README said
  it was, and that the password is therefore needed once. The conf file gets a
  fresh `icedrive_sessId` after a login that **failed**, so it cannot be an
  auth token. (The keys that *are* the session turned out to be `icedrivet` and
  `icedrive_login` — but that was established later, on a live account, and the
  first guess made from the binary alone was wrong. See the live-account section
  below, which is the reason this page separates measured from inferred.)
- **The published option list is not the whole option list.** `--help` prints
  nine options. The binary parses at least these too, none of them documented:

  | flag | what the binary does with it |
  |---|---|
  | `-hash <file>` | prints the file's SHA256 and exits. Works offline, no login |
  | `-clearsettings` | wipes the local settings |
  | `-newsync` | fires the `icedrive_new_sync` IPC — the *create a sync pair* action |
  | `-sync`, `-share`, `-publink`, `-history`, `-requestfiles` | the same IPC channel, one per tray/context-menu action |
  | `-mount`, `-quit`, `-logout`, `-lockcrypto` | ditto |
  | `-dir <folder>` | the cloud folder an upload lands in |
  | `-startup`, `-no-ecocaching`, `-advanced-logging` | settings toggles |

  **The IPC ones need a running instance** and do nothing without one: with no
  instance up, `-newsync` and `-sync` fall straight through to the normal
  start-up path and prompt for a username.
- **2FA is a hard stop.** The binary carries the string
  `2FA method isn't supported in CLI`, and prompts interactively for the
  Google-Authenticator and SMS codes it does support. An account with 2FA cannot
  be signed in unattended. `setup-icedrive.sh` detects this and says so rather
  than hanging.

## Signed in on the real box, 2026-08-27 — what a live account settled

The five questions this page used to end with were answered in one session on
the bench box, against the Owner's real account (3.91 TB of 5.00 TB used).

| question | answer |
|---|---|
| Does the session persist, so the password is needed once? | **YES.** After one `-login`/`-password` run, a second run with **no credentials at all** authenticated and mounted |
| Does the mount work with no session and no human? | **YES.** `homehub-icedrive.service` mounts it at boot, as `hub`, with nobody connected |
| Can `root` (so `backup.sh`) traverse the mount? | **NO** — and this is the one blocker left. See below |
| Does the CLI act on the account's sync pairs? | **No sign of it** — and the reason is structural. See "So what IS the CLI for?" |
| What is the persisted credential actually called? | `icedrivet` (the token) + `icedrive_login`. **Not** `icedrive_stored_cred` |

**The credential key was wrong in the first version of this layer, and the way
it was wrong is the lesson.** `setup-icedrive.sh` guarded on
`icedrive_stored_cred` — a key read out of the binary rather than observed — and
so reported *"the login left no stored credential behind"* after a login that had
provably succeeded: the same run mounted the drive, read the account's storage
statistics, and unmounted cleanly. **Guarding on a key the app does not use is
indistinguishable from the layer being broken.** The guard now asks for
`icedrivet` and `icedrive_login`, both observed. It deliberately does not ask
for `icedrive_sessId`, which is written even after a login that FAILED.

### Two failures worth keeping, because both reported success

**1. The unit was `active` and the mount did not exist.** The service originally
carried `ProtectSystem=full`, `ProtectKernelTunables`, `ProtectControlGroups`,
`NoNewPrivileges` and `RestrictSUIDSGID`. Any one of those makes systemd give
the service its own **mount namespace**, and a FUSE mount made inside a private
namespace does not propagate out:

```
systemctl is-active homehub-icedrive   ->  active
mount | grep /srv/icedrive             ->  nothing
```

The unit healthy, the app genuinely mounted, and the filesystem existing for
nobody but the service itself — with nothing anywhere reporting a fault.
`NoNewPrivileges` is separately fatal here: `fusermount` is setuid root. The
directives are gone, and `setup-icedrive.sh` now asks `findmnt` **from outside
the service** rather than believing `systemctl`.

**2. The script could not re-run on the box it had just provisioned.** Step 2
does `install -d` on the mount point; once the service is up that path is a live
FUSE filesystem that root cannot touch, so the step failed with `Permission
denied` and took the whole run with it. It now asks whether the path is already
a mount first.

### The blocker: root cannot read the mount

```
$ sudo -u hub ls /srv/icedrive     ->  2DEL_SYNC  CAN_BE_DELETED_ARCHIVE  Shared  ...
$ sudo ls /srv/icedrive            ->  ls: cannot access '/srv/icedrive': Permission denied
```

FUSE grants access to the mounting user and nobody else unless the filesystem is
mounted with `allow_other`, and **this binary offers no way to pass mount
options** — there is no `-o`, and `/etc/fuse.conf`'s `user_allow_other` is
useless without the app asking for it.

So `backup.sh`, which runs as root, **cannot write into this mount as it
stands.** The offsite plan below is not dead, but it needs a decision that has
not been made: run that one copy step as `hub` (via `runuser`/`setpriv`), which
is the clean answer and needs the sources to be readable by `hub`; or run the
mount itself as root, which puts a vendor binary at uid 0 for no other reason.

## Sync pairs — the CLI does not appear to run them

**Sync pairs live in the ACCOUNT, not on the box.** That is legible in the
binary's own request names and JSON fields:

| request | fields |
|---|---|
| `sync-list-pairs` | `path_local`, `path_remote`, `folder_id`, `syncId`, `time_last`, `ini_done` |
| `sync-pair-add` | `os_ext` (plus the pair) |
| `sync-pair-remove` | |

with `processSyncPairList` on the receiving side and `runSyncThreads` after it.
So a reimaged box that signs in does not start from nothing — which means the
old re-setup checklist (*"re-create sync pairs after every reimage"*) was very
likely wrong, the third inherited claim about IceDrive to come apart.

**Creating a pair from the CLI is not supported.** The path is
`showSyncDialog → newSyncPair → createSyncPair → addSyncPair → sync-pair-add`,
and its only trigger is the `-newsync` IPC — a *dialog*. This binary links no
widget toolkit, so it has nothing to show, and there is no flag that takes a
local and a remote path.

**And on a live, authenticated session it made no sync request at all.** A full
run with a real account logs the `Sync:` settings block and then nothing:
no `sync-list-pairs`, no `processSyncPairList`, no `runSyncThreads`, not even
`no folders to sync!`. **The honest reading is that the Linux CLI is a MOUNT
client, not a SYNC client** — the 118 MB GUI AppImage is what runs sync.

> **One confound, and it is worth closing.** If the account has no sync pairs
> defined, silence is what you would see either way. Confirming whether pairs
> exist on this account distinguishes "the CLI ignores them" from "there was
> nothing to fetch". Nothing else about this layer depends on the answer.

## The Encrypted folder — no flag, but the prompt IS drivable

IceDrive's Encrypted ("Crypto") storage has a **second, separate passphrase**,
and it is client-side by design: the key is derived on the device and never
reaches the vendor.

**There is no flag for it, and that is now proven rather than inferred.** Every
NUL-terminated argument literal in the binary was enumerated; the real CLI flag
set is `login password crypto mp verbose hash help clearcache clearsettings dir
download history logout lockcrypto mount newsync publink quit requestfiles share
startup sync` (each in both `-x` and `/x` form), plus `-no-ecocaching`,
`-disable-sandbox`, `-advanced-logging` and the Qt/OpenGL options. **No
`passphrase` among them.** The only match anywhere is `-it-revPassphrase`, which
is an OpenSSL PKIX OID name, not an argument.

The app's own error message names one anyway — *"Unable to access Encrypted
folder: the `-passphrase` parameter was not specified"* — so the message
references a parameter this build does not implement. And `-crypto` does **not**
take it as an argument either: given `-crypto SOMETHING`, the app logs
`! file/folder "SOMETHING" doesn't exist !` — it fell through to the trailing
upload-path list — and then says `cli: no passphrase given`.

**But the interactive prompt can be driven, and that is the useful finding.**
Fed through a pty (`script`, or `expect`), `Enter crypto passphrase:` is read,
and the app takes it seriously — a deliberately wrong value produced a real
`crypto-auth` request and `passphrase NOT validated..`. So the Encrypted folder
**is** automatable; it just needs a pty rather than an argument.

> **This page said the opposite on its first pass, and the correction is the
> point.** "It cannot be automated on this version" was written from the flag
> enumeration alone, without trying the prompt. The flag half was right and the
> conclusion was wrong.

**And the asymmetry is real: the same trick does NOT work for the account
password.** Fed the same way, with a delay to rule out a race, the password
prompt still answers `cli: no password given`. Password: argv only. Passphrase:
prompt only. Two credentials, two opposite injection paths, in one binary.

Nothing on the hub uses the Encrypted folder today, so **no store key exists for
the passphrase** — adding one before something consumes it is the
`DataRepoDeployKey` shape. If that changes, the shape to build is a one-time
pty-driven unlock at provisioning, mirroring the login. Whether the passphrase
persists afterwards is **untested**; the binary has `store-crypto-hash` and logs
`no stored encryption credentials; running validation now...`, which suggests it
does.

Note also `pair_list: no encryption key set!` — the sync-pair list is itself
gated on the crypto key for encrypted pairs.

## So what IS the CLI for?

Worth stating plainly, because "it can log in but cannot make a sync pair" reads
like a half-finished tool. It is not: **it is the GUI application compiled
without its toolkit, shipped for machines that have no desktop.** Its purpose is
the FUSE mount.

That explains every observation on this page. The sync engine is *in* the binary
(`runSyncThreads`, `processSyncPairList`, `sync-pair-add`) because it is the same
codebase — but the only thing that ever calls the create path is a dialog, and
this build links no widget library. The `-newsync`, `-sync`, `-share`,
`-publink`, `-history` and `-requestfiles` flags are the desktop build's
tray and file-manager context-menu actions; in the CLI they message a running
instance which then tries to open a window it does not have. They are vestigial,
not broken.

What survives is exactly what needs no window: **sign in, mount, upload, hash,
logout, cache control** — and crypto, which survives because it falls back to a
terminal prompt.

**Vendor documentation does not exist.** The community has one thread asking for
a list of command-line options with no staff reply, and one user running
IcedriveCLI on a headless Debian server purely as a mount. The 2024 "all-new
Mount & Sync" announcement covers Windows/Mac/Linux GUI and never mentions a CLI
build at all. The CLI *is* maintained — a November 2024 bug report that v3.22
lacked an eco-cache flag is answered by `-no-ecocaching` existing in v3.62 — it
is simply undocumented.

**For HomeHub this is fine, and arguably better than sync.** A mount plus a
copy step driven by `backup.sh` is ordered, scriptable and visible to NagLight;
vendor sync running on its own schedule is none of those things.

## What this buys, beyond deleting a desktop

`backup.sh`'s retired step 5 still supports `OFFSITE_PATH` as a local directory.
Pointed at `/srv/icedrive`, the offsite copy becomes **ordered after the backup,
scriptable, and visible to NagLight** — which closes the gap the old design
admitted in writing: *"a green backup says nothing about the cloud copy."* A GUI
client syncing on its own schedule can never report that. This is deliberately
**not wired up yet**: it depends on questions 2 and 4 above.

## Shipping the binary (once per version)

Not in this repo, not fetched by the build — `icedrive.net` is behind Cloudflare
and answers **403 to anything that is not a browser**, including for the CLI
installer, so the widely-quoted

```sh
curl -s https://icedrive.net/download/linux/cli/install.sh | bash    # DO NOT
```

**pipes an HTML challenge page into bash.** Download it in a browser, once:

```sh
# 1. Download the Linux CLI from icedrive.net in a browser.
bash vmtest/export-icedrive.sh --print-hash --from ~/Downloads/IcedriveCLI-v3.62
# 2. Put VERSION= and SHA256= into stack/icedrive/icedrive.pin
# 3. Verify, cache and stage it:
bash vmtest/export-icedrive.sh --out vmtest/.out/icedrive --from ~/Downloads/IcedriveCLI-v3.62
# 4. Build the ISO as usual - later builds reuse the cache.
```

The hash travels beside the binary onto the payload and **`setup-icedrive.sh`
re-checks it on the box before installing** — an ISO can be re-burned and a
payload can be edited, so "we verified it at build time" is not the same claim
as "these bytes are pinned".

## The credential

**Ruled by the Owner, 2026-08-27: it IS a deploy secret**, held as
`IcedriveCredential` in the DPAPI store and emitted to `.env` as
`ICEDRIVE_USER` / `ICEDRIVE_PASSWORD`.

This **replaces the opposite ruling of 2026-08-09**, and it replaces it on that
ruling's own terms. The old one turned entirely on *"there is no CLI or API to
hand it to, so storing it buys no automation — it would only add a rotation
obligation and a store key that looks like unfinished work."* `-login`/
`-password` is exactly that API. The credential is consumed once at
provisioning, which is the shape every other minted secret here already has.

It is **Optional**: with the key absent, `.env` carries nothing,
`setup-icedrive.sh` installs the binary and stops, and no unit is enabled.

## Disable / remove

```sh
sudo systemctl disable --now homehub-icedrive
sudo -u hub /opt/icedrive/icedrive -logout
sudo rm -rf /opt/icedrive /home/hub/.config/Icedrive /home/hub/.local/share/Icedrive
sudo rmdir /srv/icedrive
```

Then clear the store key so it does not rot: `PrepDeploySecrets.ps1 -Forget
IcedriveCredential`.
