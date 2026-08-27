# IceDrive on the hub — the headless CLI

**Replaced the GUI client and the whole graphical layer on 2026-08-27.** The
Owner: *"GUI can be removed… from the image and from the box, along with the
auto-desktop startup."* What made that possible is that the premise the
graphical layer was built on turned out to be false.

Everything on this page below the first section was **measured**, on
`IcedriveCLI-v3.62`, on 2026-08-27 — not read from vendor documentation, which
barely exists. Where something is unproven it says so in those words.

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
  `script`), and omitting `-password` so it prompts. Pre-seeding the config is
  not available either — the stored credential is encrypted by the app with a
  key we do not hold. See `setup-icedrive.sh` for why argv is acceptable *here*
  and the one condition that would change that.
- **`icedrive_sessId` is NOT the persisted session.** The remote-ui README said
  it was, and that the password is therefore needed once. The conf file gets a
  fresh `icedrive_sessId` after a login that **failed**, so it cannot be an
  auth token. The key that matters is **`icedrive_stored_cred`**, which the
  binary encrypts and decrypts itself (`secureStoreString`,
  `couldn't decrypt user password`). Everything here keys off that one instead.
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

## Sync pairs — what could be established, and what could not

**Sync pairs live in the ACCOUNT, not on the box.** That is the finding that
matters, and it is legible in the binary's own request names and JSON fields:

| request | fields |
|---|---|
| `sync-list-pairs` | `path_local`, `path_remote`, `folder_id`, `syncId`, `time_last`, `ini_done` |
| `sync-pair-add` | `os_ext` (plus the pair) |
| `sync-pair-remove` | |

with `processSyncPairList` on the receiving side and `runSyncThreads` after it.
So a reimaged box that signs in **does not start from nothing** — it fetches the
pair list the account already holds. The remote-ui README's re-setup checklist
(*"re-create sync pairs"* after every reimage) is very likely wrong on that
point, which is the third inherited claim on this subject to come apart.

**But creating a pair from the CLI is not supported, on the evidence.** The
create path is `showSyncDialog → newSyncPair → createSyncPair → addSyncPair →
sync-pair-add`, and its only trigger is the `-newsync` IPC — a *dialog*. This
binary links no widget toolkit at all, so it has nothing to show. There is no
flag that takes a local and a remote path.

**NOT CONFIRMED — do not build on it:** whether the CLI *runs* the pairs it can
list. `runSyncThreads`, `SyncThread` and the deletion-policy settings are all
present in the binary, and a no-credential run prints a `Sync:` block, but that
is settings, not proof of continuous two-way sync. Nothing further is testable
without an account.

### What one login session would settle

An hour with a real account, in one sitting, answers all of it:

1. Does `icedrive_stored_cred` appear, and does a second run then need no
   password? (This is what lets the mount unit carry no credential.)
2. Does the mount at `-mp /srv/icedrive` survive with no session and across a
   reboot?
3. Does `sync-list-pairs` return pairs created on another device, and does the
   CLI act on them?
4. Can `root` (so, `backup.sh`) traverse a FUSE mount owned by `hub`, or does
   that need `allow_other` — which this binary offers no way to pass?
5. What does `-logout` do to a running mount?

Until then this layer ships **installed but signed out**, and nothing runs.

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
