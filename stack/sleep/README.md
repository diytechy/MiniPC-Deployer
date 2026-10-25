# The sleep feeder (SR-070..SR-072)

Reads the CPAP's own **ResMed myAir sleep score** for last night and posts it to
NagLight as the `sleep` gauge. A plain hub service on a 30-minute systemd timer —
**no container**, **off by default** (`SLEEP_ENABLED=false`), **no new package**
(Python standard library only).

It computes **nothing**. The number on the wall is myAir's own; the colour is
NagLight's (target 80, favourable high). All this feeder decides is *which night's*
number is true right now, and *when* it became true.

The plan it implements is `docs/GAUGE_RINGS_AND_SLEEP_PLAN_2026-10-11.md` in the
HomeHub repo (revision 5): §3.6 (a dead login goes grey), §4.2–4.5 (source, feeder,
**the nightly data contract**, the wire). The NagLight side already exists:
IF-012 (the feed contract) and the 36-hour `daily` staleness horizon.

## What it posts

```json
{ "kind": "gauge", "id": "sleep", "label": "Sleep", "icon": "sleep", "unit": "pts",
  "value": 82, "min": 0, "max": 100, "target": 80, "favourable": "high",
  "window": { "kind": "daily" }, "observed_at": "2026-10-12T07:00:00-05:00" }
```

- `unit: "pts"` is not `"%"`, so it lands in the life row, not the AI row.
- `window: {"kind": "daily"}` with **no start, end or direction** is deliberate:
  NagLight's `Validate` refuses only a half window, and a direction would be
  refused (a standing line has none). `Stale()` then gives the 36 h horizon.
- **No `previous_value`**: NagLight derives the trend baseline itself.
- *Unavailable* is the same body with `value: 0` and **no `observed_at`**, which
  NagLight treats as stale on arrival. The 0 is never shown as a reading.

## The nightly data contract (plan §4.4)

| Rule | Where |
|---|---|
| **Night key = `startDate` + 1 calendar day** (`startDate` is the evening the night *began*; S0b, confirmed by the Owner in the app) | `night_key_for` |
| One entry per night key, **upserted** | `upsert_nights` |
| Only a **finite `sleepScore` within 0..100** is a score — including myAir's own **0**. Null, missing, a string, a bool, NaN or out of range is *no data*, **never 0** | `valid_score` |
| **`observed_at` = the EARLIER of 12:00 local on the night-key date and the first publication**, then **frozen**. A record arriving at 07:00 is stamped 07:00 (NagLight treats a *future* stamp as stale); one arriving at 16:45 is stamped 12:00 — **never the later fetch time** | `upsert_nights` |
| A **correction** changes the value and never `observed_at`; a later null or missing record never erases a published night | `upsert_nights` |
| The **expected night** is **today's local date** (`TIMEZONE`) | `expected_night` |
| **Before 14:00** local, with no record for the expected night, the **previous night's** score stays and ages normally — **but only a night the feeder actually PUBLISHED** (the state file's `published` set, recorded when a score was accepted by the tracker). A night a late fetch merely staged after an outage or a dead-login period was never on the wall and is not "the score that stays". Anything older is never shown | `decide_night` |
| **From 14:00** local, with no record for the expected night, the gauge posts **unavailable**. This also covers "the newest record is not for the expected night": an old night is never shown as last night | `decide_night` |
| The expected night is **never first published before 06:00** local: a record found earlier is *staged* with its frozen stamp floored at 06:00 (a stamp in the future is *quarantined*, never displayed, until the clock reaches it), so the stamp lands in the right NagLight tracking day (which starts at 02:00) | `upsert_nights`, `decide_night` |
| **Once today's night has been published, no earlier night is ever selected for display that day.** If today's record is unavailable (a same-day backwards clock step quarantined it) the gauge posts unavailable, not yesterday's score. Future-stamped observations are kept, not destroyed, and shown again when the clock passes them | `decide_night`, `validate_night_entry` |
| The clock is **read again after the fetch**, before selecting and posting, so a cycle that starts at 13:59 and ends at 14:01 applies the 14:00 rule | `run_cycle` |
| If the local date is **earlier than the newest published night** the clock went backwards: nothing is selected or posted and the journal says so (status only) | `clock_went_backwards` |
| All time arithmetic goes through the zone's own offset **for the instant in question** — never `+ 24 h` — so the 23- and 25-hour days are right | `noon_epoch`, `past_grace` |

Choices where the plan left a point open (the smallest consistent one):

- Only the expected night and the previous night are ever **stored**; the other
  ~38 records myAir returns for 30 days are read and dropped. Nothing older can be
  shown, so nothing older is kept.
- A record whose night key is **in the future** (`startDate` = today) neither
  publishes nor blocks today's night.
- Two records for one night with **different** valid scores are ambiguous and are
  skipped for that cycle.
- The stamp is frozen when the night is first **decided for publication**, which
  can be a cycle before a failed POST succeeds. It is therefore never *later* than
  the real first publication, and never in the future.
- **`totalUsage` (minutes) is requested and KEPT, never posted** (2026-10-25,
  the Owner's request; where it is shown and how it is used is still open). It
  is stored per night key in the state file's `usage` part for 60 nights,
  validated as whole minutes within 0..1440 including 0, and a night with two
  different figures is left unchanged. The journal says only how many nights
  were recorded. It is NOT used to derive a night's end. The sub-scores are
  still not requested.

## When myAir is asked

The timer ticks every 30 minutes and **every tick posts from stored state** (so the
14:00 rule and the frozen stamp keep working). The **network** is used far less:
every sign-in is a full login to a health vendor with no refresh token, so myAir is
asked **only**

- while the expected night is **not yet stored**, between **06:00 and 16:00** local,
  at most once per two hours;
- once it **is** stored, for a **correction re-fetch** from **14:30** and another
  from **20:00** local (each at most once per local day; a missed slot is made up
  with one fetch);
- at **20:00** local **even if the night is still missing** — one catch-up attempt a
  day (one extra sign-in on a late day), so a night uploaded between 16:00 and
  20:00 is fetched and published that evening;
- on the very next tick after a **first** authentication failure (so the two-cycle
  confirmation takes about 30 minutes);
- after a **confirmed** dead login, only when the back-off below has elapsed, and
  then only between **06:00 and 22:00** local (a recovery never lands at 00:30).

A correction or catch-up slot is marked **done only after a read that did not
fail**, so a transient failure at 20:00 does not consume the evening catch-up. A
failed (non-authentication) read of a slot is retried: at most three attempts in
all, 30 minutes apart, none started at or after 22:00. The authentication back-off
overrides all of it. Every attempt records when myAir was last asked.

A normal day is therefore about five sign-ins (06:00, 08:00, 10:00 when the night
arrives around then, 14:30, 20:00), not 48. A night that uploads after 20:00 is not
fetched that night (see Known limits). The windows are local wall-clock times, so
they hold on the 23- and 25-hour days; skipped ticks are *not* an outcome and never
reset the failure count.

## Clock synchronisation

Night keys, the 06:00 floor, the noon stamp and the 14:00 rule are *local dates read
off the host clock*, and `After=time-sync.target` does not by itself mean the clock
is set. Before anything else a cycle checks that it is synchronised, and if it
cannot show that it **asks myAir nothing and publishes no timestamped score** — but it
**still posts the unavailable (grey) body on every unsynchronised tick, at any hour**.
That body has no `observed_at`, so it needs no trusted time, and it is what keeps the
14:00 cutoff true after a reboot with NTP down (yesterday's green score must not sit
on the wall past 14:00 because the clock cannot be trusted). A hub with no
synchronisation signal at all therefore shows a grey gauge, not none. The journal says
so (exit non-zero) and names what to check: `systemd-timesyncd` and `timedatectl`.
`setup-sleep.sh` probes for a signal at install time and prints one `clock sync:` line
(a `WARNING` if neither exists). State is neither read nor written on those ticks.

1. systemd-timesyncd's flag file `/run/systemd/timesync/synchronized` — present
   once it has synchronised. **This is the method that applies on the hub** (Ubuntu
   with `systemd-timesyncd`, per the image).
2. otherwise `timedatectl show -p NTPSynchronized --value` (5 s timeout): `yes` is
   synchronised, `no` is not.
3. if neither is available the answer is *unknown*, which counts as not synchronised.

**Not verified:** that the flag file is present on the real hub; that `timedatectl`
works inside the unit's sandbox (it needs the system bus — it is only the fallback);
nothing here was run on the hub. The feeder and NagLight share one host clock, so
skew *between* clocks is out of scope.

## Knobs (`stack/.env`)

| Knob | Default | Meaning |
|---|---|---|
| `SLEEP_ENABLED` | `false` | Only a literal `true` installs/runs it. |
| `SLEEP_USER` | blank → **refuses** | The Google `sub` the gauge is posted for (same value as `NAGLIGHT_USER`). Never guessed. |
| `SLEEP_USER_ACCOUNT` | `homehub-sleep` | The unix account. No home. `hub` and `root` are refused. |
| `SLEEP_FEED_URL` | loopback tracker | **Refused unless loopback or a local docker bridge.** Redirects are refused. |
| `SLEEP_FEED_TOKEN` | blank | Bearer for `/api/feed`; blank sends none. |
| `SLEEP_PUBLISH` | `1` | `0`/`false`/`no`/`off` is an operator off switch: nothing is read, nothing is posted. |
| `MYAIR_EMAIL` | blank → **refuses** | From the identity table (`household.MyAirEmail`). |
| `MYAIR_PASSWORD` | blank → **refuses** | From the secret store (`MyAirPassword`). |
| `SLEEP_STATE_FILE` | `/var/lib/homehub-sleep/sleep-state.json` | Must stay inside `StateDirectory=`. |
| `SLEEP_TIMEOUT_SECONDS` | `30` | Per request (a cycle is five). |
| `TIMEZONE` | `Etc/UTC` in the example | **Must be the hub's real zone** — night keys are local dates. Blank/unknown is refused. |

**`MYAIR_PASSWORD` must not contain `\` or `"`, and a password that is not
whitespace-containing must not START with `'`** (systemd's `EnvironmentFile=`
unescapes a backslash, strips double quotes and drops a bare leading quote; an
apostrophe inside, at the end, or anywhere in a password that contains spaces - which
the materialiser wraps in double quotes - is fine; see Known limits (c)).

`$` handling: the materialiser writes every `$` in a `.env` value as `$$`
(compose interpolates), and systemd's `EnvironmentFile=` does **not** undo it. The
feeder collapses `$$` in `MYAIR_PASSWORD` itself, the way `firstboot.sh`'s
`env_value` does for the hub's own scripts. A password containing a **backslash**
or a **double quote** is not safe through `EnvironmentFile=` (systemd unescapes
them) and is not supported — it would fail as an authentication failure, which
reads exactly like an expired login.

## How it signs in (and the S0b facts)

Re-implemented on the standard library from the MIT-licensed
[`prestomation/resmed_myair_sensors`](https://github.com/prestomation/resmed_myair_sensors)
Home Assistant integration at commit `e2c52a30`
(`custom_components/resmed_myair/client/`: `auth.py`, `graphql.py`,
`rest_client.py`, `regions.py`). The NA region's public client identifiers in the
code are that project's; they identify the myAir web app, not the Owner.

> MIT License — Copyright (c) 2021 (upstream `LICENSE`, reproduced as the licence
> requires)
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of
> this software and associated documentation files (the "Software"), to deal in
> the Software without restriction, including without limitation the rights to
> use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
> the Software, and to permit persons to whom the Software is furnished to do so,
> subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
> FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
> COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
> IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
> CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

**Facts established by the S0b probe on 2026-10-11 against the Owner's real
account** (field names and meanings only — the scores went to the Owner directly):

- **Sign-in, NA region, through Okta:** `GET resmed-ext-1.okta.com/oauth2/<server>/v1/authorize`
  (a 400 that hands out the remembered-device cookie), `POST …/api/v1/authn` (200,
  `status: SUCCESS`), `GET …/authorize` with the session token (302, the
  authorization code in the `Location` **fragment**, PKCE S256), `POST …/v1/token`
  (200). GraphQL is `POST graphql.myair-prd.dht.live/graphql` (200).
- **No email MFA** — not on the first sign-in, not on a remembered-device re-sign-in.
- **Tokens last 3600 s. There is no refresh token** (`offline_access` is not
  requested), so the feeder **signs in from scratch every cycle** and holds nothing
  afterwards. It therefore writes **no session or token file**; the device cookie
  is kept in memory for the one cycle (it was not needed by the probe's account).
- **Record keys:** `startDate` (bare `YYYY-MM-DD`), `totalUsage` (int, minutes),
  `sleepScore` (int 0..100), `usageScore`, `ahiScore`, `maskScore`, `leakScore`
  (int), `ahi` (float), `leakPercentile` (float), `maskPairCount` (int),
  `sleepRecordPatientId` (str). The device record has `lastSleepDataReportTime`
  (ISO with offset) — **not** a per-record upload time, and not used.
- 40 records came back for the 30-day query, none duplicated; `sleepScore` was
  present on 40 of 40 and always within 0..100.
- The device reported last night by 09:59 local, well before the 14:00 grace.

Only `startDate`, `sleepScore` and `totalUsage` are requested (the upstream
query also asks for the sub-scores, leak and mask figures; this feeder does not
use them, so fewer health values cross the wire and sit in memory).

## NOT PROVEN

- **Unused nights.** Every one of the 40 probed nights had usage, so it is
  **unknown** whether myAir returns an unused night as a record with `totalUsage`
  0 (and a score of its own, possibly 0) **or as no record at all**. §4.4 is written
  so both are right: a record with a score publishes it; no record posts
  *unavailable* from 14:00. **If myAir omits unused nights, the wall goes grey after
  14:00 on a night CPAP was not used, which hides a missed night** — plan §4.4
  asks the Owner to accept that before this publishes.
- **How often sign-in needs a person** (an emailed code). A probe cannot prove
  long-term frequency; the journal says when it happens (below) and it is watched
  after deployment (plan S5).
- **Months of daily sign-ins.** There is no refresh token, so every fetch is a full
  sign-in to a health vendor. The schedule keeps that to about five a day, but
  nothing in the probe showed whether myAir rate-limits, sends a "new device" email
  or locks an account after months of it, and a short probe cannot rule it out.
- **The upstream's EU region** is not supported (the Owner is NA).
- **Whether a night's score is revised after upload** — the contract handles it
  (a correction changes the value, not the stamp); no revision has been observed.
- **The instant `startDate` + `totalUsage` ends a night** — not used.

## The journal (status only)

The journal carries **which source answered, which night key, and whether it was
published — never a score, a component or any health value.** `published night
<date>` is the audit line.

```
sleep: source cpap-myair answered
sleep: night key(s) updated: 2026-10-12
sleep: published night 2026-10-12
sleep: expected night 2026-10-12: no score yet - posted unavailable
sleep: expected night 2026-10-12: login confirmed dead - posted unavailable
sleep: cpap-myair: okta-authn: HTTP 401                              (stderr)
sleep: cpap-myair: authentication failure 1 of 2 - last score kept until confirmed
sleep: cpap-myair: authentication failure 2 of 2 - CONFIRMED, the gauge is posted unavailable until a sign-in succeeds
sleep: cpap-myair: myAir now asks for an emailed code - a person must sign in (see refresh-gauge-logins)
```

Remote text (an Okta error summary, a GraphQL message) is **never** reproduced:
every message is this file's own words, with at most an allow-listed status word
(`LOCKED_OUT`, `PASSWORD_EXPIRED`, `MFA_REQUIRED` …).

## A dead login (plan §3.6 G2)

- **What counts as authentication:** an Okta HTTP 401 (authn or token); any authn
  `status` other than `SUCCESS` (wrong password, `LOCKED_OUT`, `PASSWORD_EXPIRED`,
  `MFA_REQUIRED`); a GraphQL 401 or `unauthorized` after a good sign-in. A 5xx, a
  429, a timeout and a malformed body are **not** — those keep the last score until
  its horizon.
- **Confirmed over two consecutive sign-in attempts** (about 30 minutes: the first
  refusal makes the next tick ask again). The first failed attempt behaves as today
  (the last score is re-posted at its original stamp); the second posts the
  unavailable body.
- The count lives **in the state file** under the reserved key `__auth_failures__`
  with its own validator (a one-shot unit has no memory); any non-authentication
  outcome resets it.
- **Sticky:** the confirmation sets `__auth_failed__`. While it is set, **every**
  cycle posts unavailable, including after a later network failure, until a
  sign-in **succeeds**.
- **Back-off:** while the marker is set myAir is **not asked at all** — zero Okta
  calls, the wall stays grey from stored state — for **6 h** after the last refusal,
  or **24 h** when the authn status was `LOCKED_OUT`. A wrong or expired password
  retried at every tick would be 48 failed attempts a day, enough to lock the
  account, the phone app included. After the back-off one attempt is made; if it is
  refused the back-off restarts from that refusal, if it succeeds the marker clears.
- **`MFA_REQUIRED` never triggers anything.** This feeder has no mail code at all
  (asserted by test). It posts unavailable and logs the line above.

### When sign-in needs a person

`journalctl -u homehub-sleep` shows which. Then follow the HomeHub
`refresh-gauge-logins` skill (its myAir row):

1. *Wrong/expired password:* sign in to myAir in a browser with the account; if
   the password changed, update the vault entry `MyAirPassword`
   (`PrepDeploySecrets.ps1 -ImportKey`, piped, never typed into an echoing
   terminal) and re-materialise the hub `.env`. The email is the identity row
   `household.MyAirEmail` — change it there, not in the vault.
2. *Emailed code:* a person signs in to myAir from a browser and completes the
   code; if myAir keeps asking, the account's "remember this device" is not holding
   for the hub's address and **this feeder cannot supply a code** — by design, it
   reads nobody's mailbox.
3. After the `.env` changes, run the feeder with **`--retry-now`**, which clears
   the marker (and any published night key dated later than today) and asks myAir at
   once, outside the windows and the back-off. **It refuses to run as root** (or on a
   state file owned by another user) with a one-line reason and exit status 2, because
   a root-owned state file could never be replaced by the unit. Under the unit's
   account and environment:

   ```
   sudo systemd-run --wait --pipe --collect -p User=homehub-sleep \
       -p EnvironmentFile=/opt/homehub/stack/.env -p StateDirectory=homehub-sleep \
       -p StateDirectoryMode=0700 \
       /usr/bin/python3 /opt/homehub/stack/sleep/sleep_feeder.py --retry-now
   ```

   (`systemctl start homehub-sleep.service` alone does **not** do this: inside the
   back-off it asks nothing. The `systemd-run` line has not been run on a real hub —
   see Known limits.) One refused `--retry-now` starts the count again at one and
   the next tick confirms it, so a still-wrong password costs two attempts, not
   a stream. If the sign-in is simply fixed and there is no hurry, do nothing: the
   back-off ends by itself.

## Install

```
sudo bash /opt/homehub/stack/sleep/setup-sleep.sh        # needs SLEEP_ENABLED=true in .env
sudo bash /opt/homehub/stack/sleep/setup-sleep.sh --check-only
```

Idempotent. Creates the `homehub-sleep` account (no shell, no password, **no
home**), installs the unit and timer, writes a drop-in so the unit's `User=` is the
`SLEEP_USER_ACCOUNT` knob, and enables **the timer** (never the oneshot service).
It refuses on a blank `SLEEP_USER`, `SLEEP_FEED_URL`, `MYAIR_EMAIL` or
`MYAIR_PASSWORD` (it asks only *whether* the last two are non-blank; it never reads
or prints them). `stack/autoinstall/firstboot.sh` (step 6f-2) calls it on a fresh
install whenever `.env` says `SLEEP_ENABLED=true`, modelled on the weight block: a
refusal there is a logged WARNING, never a failed first boot. The whole `stack/` tree
rides the ISO payload (it is a `git archive` of tracked files), so `stack/sleep/` is
carried without being named anywhere; `tests/test_sleep_wiring.py` asserts all six
files are tracked.

## Installing on the RUNNING hub (no reimage)

Written down, **not run**. `/opt/homehub` is **not a git checkout** (it is a copied
tree), so never sync the whole repo over it: diff and copy only `stack/sleep/`.
Nothing here restarts a container; the feeder is a host systemd unit.

1. **Dev PC: materialise the hub config** as usual. The generated `scripts\deploy\out\homehub\.env`
   must carry the keys below (check **names only**; never print the file or a
   value): `SLEEP_ENABLED=true`, `SLEEP_USER`, `SLEEP_USER_ACCOUNT`, `SLEEP_FEED_URL`,
   `SLEEP_FEED_TOKEN`, `SLEEP_PUBLISH`, `SLEEP_STATE_FILE`, `SLEEP_TIMEOUT_SECONDS`,
   `MYAIR_EMAIL`, `MYAIR_PASSWORD`, `HEART_*`. `SLEEP_ENABLED` and `SLEEP_USER` come
   from HomeHub `config.homehub.psd1`; `MYAIR_EMAIL` from the identity row
   `household.MyAirEmail`; `MYAIR_PASSWORD` from the store key `MyAirPassword`.
   `HEART_ENABLED` stays `0` (no heart device exists yet).
2. **Hub: back the `.env` up.**
   `sudo cp -p /opt/homehub/stack/.env /opt/homehub/stack/.env.bak-$(date +%Y%m%d-%H%M%S)`
3. **Hub: copy only the sleep tree, after diffing it.** Stage the six files from the
   release commit (`git archive <commit> stack/sleep` on the dev PC, piped over ssh
   on stdin into a scratch directory such as `/tmp/sleep-stage`), then
   `diff -r /tmp/sleep-stage/stack/sleep /opt/homehub/stack/sleep` - on a first
   install the destination does not exist and every file is new. Then
   `sudo install -d -m 0755 /opt/homehub/stack/sleep` and `sudo install -m 0644` each
   of `README.md`, `apply-env-lines.py`, `homehub-sleep.service`,
   `homehub-sleep.timer`, `setup-sleep.sh`, `sleep_feeder.py` into it, and remove the
   scratch directory. Copy nothing else.
4. **Hub: set the keys with the helper from step 3.** Do **not** "append
   what is missing": a hub `.env` that already carries the template defaults
   (`SLEEP_ENABLED=false`, a blank `SLEEP_USER`, blank myAir values) would keep them,
   and `setup-sleep.sh` would then exit "disabled" having installed nothing. A second
   copy of a key is no better: systemd and `setup-sleep.sh` take the **last** line,
   firstboot's `_env_val` the **first**. The helper replaces the first line of each
   key, drops any further copies (including `export KEY=` and indented ones),
   appends keys that are absent, and writes the result **atomically**: a temp file in
   the same directory with the original's mode and owner, `fsync`, `os.replace`, then
   an `fsync` of the directory, all under an exclusive `flock`. A failure at any
   point removes the temp file and leaves the original untouched; the file is *not*
   rewritten in place, because a half-written `.env` would corrupt every hub secret
   and nothing bind-mounts it (compose reads it for interpolation only, systemd as an
   `EnvironmentFile`), so keeping its inode buys nothing. It accepts only the exact
   sleep, myAir, heart and `TIMEZONE` key names (a typo is refused), **refuses a blank
   value for a required key** (so a store key that was never imported cannot blank a
   good hub value), and prints only key names and `replaced` / `added` / `unchanged`,
   never a value.

   **Transport: the bytes, not the pipeline.** PowerShell 5.1 re-encodes anything
   piped to a native program (ASCII by default, with `?` for what it cannot encode),
   so a non-ASCII password would arrive corrupted *and look valid*. Do not pipe. Write
   the wanted lines to a file as UTF-8 **without a BOM** and let `cmd.exe` redirect
   the file's bytes into ssh; this behaves the same on PowerShell 5.1 and 7. The
   helper then rejects a BOM or anything that is not valid UTF-8. From the dev PC, in
   the HomeHub repo (the file holds secrets: it lives in the already-restricted
   `out\` directory and is deleted at once):

   ```
   $utf8 = New-Object System.Text.UTF8Encoding($false)
   $src  = "scripts\deploy\out\homehub\.env"
   $tmp  = "scripts\deploy\out\homehub\sleep-lines.env"
   $keep = [IO.File]::ReadAllLines((Resolve-Path $src), $utf8) |
       Where-Object { $_ -match '^(SLEEP_|MYAIR_|HEART_)[A-Z_]*=' }
   try {
       [IO.File]::WriteAllText("$PWD\$tmp", (($keep -join "`n") + "`n"), $utf8)
       cmd /c "ssh <hub> sudo -n python3 /opt/homehub/stack/sleep/apply-env-lines.py --apply < $tmp"
       if ($LASTEXITCODE -ne 0) { throw "apply failed with exit code $LASTEXITCODE - do NOT continue" }
   } finally {
       Remove-Item $tmp -ErrorAction SilentlyContinue      # the file holds secrets: always delete it
   }
   ```

   The helper writes the flag keys (`*_ENABLED`, `SLEEP_PUBLISH`) **unquoted**
   whatever the input had, because the setup script and first boot compare the
   literal value. If only the final directory `fsync` fails after the replace, the
   file *is* changed: the helper prints a WARNING that durability is uncertain and
   exits 0. At the start of every apply or restore, under the lock, it also deletes
   any stale `.tmp-.env-*` file a hard-killed earlier run left beside `.env` (temp
   names are scoped to the target's own name, so a different env file in the same
   directory is never swept).

   `SLEEP_FEED_URL` is the loopback tracker URL, the same value the weight and AI
   feeders use (the tracker publishes `127.0.0.1:8787:8787`). The password must not
   contain `\` or `"`. The materialiser wraps a password that contains whitespace in
   double quotes (`MYAIR_PASSWORD="two words"`); that is normal and the helper
   accepts it, because it parses the surrounding quotes first (as systemd does) and
   applies these rules to the **decoded** value. It refuses a decoded value with a
   backslash or double quote, any value starting with a quote that has no matching
   closing quote, and any other wrapped form (single-quoted, or double-quoted
   without whitespace), which the materialiser never emits and systemd would alter
   or which is ambiguous. An apostrophe anywhere in the decoded value is allowed
   (measured, see Known limits (c)). All three parsers - the helper, `setup-sleep.sh` and first boot -
   strip exactly one matching pair of surrounding quotes and nothing else.
5. **Hub: verify the effective values before installing.**
   `sudo -n python3 /opt/homehub/stack/sleep/apply-env-lines.py --report` prints, for
   each key, only `set` / `empty` / `absent`, plus the literal value of the two
   `*_ENABLED` flags. It reads the file the way systemd does (last line wins, one pair
   of single or double quotes stripped, so `MYAIR_PASSWORD=''` is *empty*). It exits
   non-zero and says why if a required key is empty, a key is duplicated or written
   as `export` / indented, `SLEEP_ENABLED` is not the literal `true`, or
   `HEART_ENABLED` is not the literal `0` (the heart gauge stays off in this rollout).
6. **Hub: check, then install.**
   `sudo bash /opt/homehub/stack/sleep/setup-sleep.sh --check-only` (refuses on a
   blank identity, destination or sign-in, and prints one `clock sync:` line), then
   `sudo bash /opt/homehub/stack/sleep/setup-sleep.sh`.
7. **Hub: verify.**
   - `systemctl list-timers homehub-sleep.timer` shows a next and a last run.
   - `sudo systemctl start homehub-sleep.service` runs one cycle now (it makes a
     real myAir sign-in, one of the day's five); then
     `journalctl -u homehub-sleep -n 40 --no-pager`. A healthy first cycle prints
     `sleep: source cpap-myair answered` and either `sleep: published night <date>`
     or `sleep: expected night <date>: no score yet - posted unavailable` (before the
     night has uploaded, or after 14:00 with none). An `authentication failure` or
     `asks for an emailed code` line is the sign-in, not the install: see "When
     sign-in needs a person".
   - A post landed in NagLight: the journal's `published night` line is the audit
     trail; the `sleep` bar then appears in the life row on the panel. The unavailable
     body is also a post (a grey, empty bar), so a grey bar proves delivery too.
   - `sudo systemctl is-enabled homehub-sleep.timer` prints `enabled`.
   - No stale temp file holds secrets next to the config:
     `sudo find /opt/homehub/stack -maxdepth 1 -name '.tmp-.env-*'` must print
     nothing. A hard-killed run can leave one; `sudo rm` it (the next apply or
     restore would also sweep it).
8. **Roll back.** Stop **both** units - a oneshot that is already mid-run can still
   publish after its timer is gone:
   `sudo systemctl disable --now homehub-sleep.timer && sudo systemctl stop homehub-sleep.service`.
   Then restore the `.env` from the step 2 backup with the helper:
   `sudo -n python3 /opt/homehub/stack/sleep/apply-env-lines.py --restore /opt/homehub/stack/.env.bak-<stamp>`.
   It checks that the backup exists, is a regular file, is readable, is non-empty
   and is valid UTF-8 **before** it touches `.env`, then restores with the same
   atomic writer. Do **not** use `sh -c 'cat backup > .env'`: the shell empties
   `.env` before it opens the backup, so a mistyped backup name destroys the config.
   (`mv`, `cp --remove-destination` and `sed -i` are not atomic-with-validation
   either: use the helper.) Afterwards re-run the `find` for `.tmp-.env-*` from step 7.
   The account and state directory are inert and may stay.

Not proven: every step above, on a real hub (see "Known limits"). The helper itself
is covered by `tests/test_sleep_wiring.py` (exact bytes, atomicity under simulated
failure, mode and owner calls, no value printed, refusals, restore validation, flag
canonicalisation, stale-temp sweep, and - on Linux only - two helpers blocked on the
`flock` and a waiter re-reading a file replaced under it), not by a run against the
hub's own `.env`. `fchown` with a real different owner has not been exercised.

The unit: `User=homehub-sleep`, `StateDirectory=homehub-sleep` mode `0700`, the
state file `0600`, `ProtectHome=yes`, `ProtectSystem=strict`, `NoNewPrivileges`,
empty capability set, `SystemCallFilter=@system-service`, `UMask=0077`,
`ReadWritePaths=/var/lib/homehub-sleep`, no `Restart=` (a rejected password counts
toward a lockout).

## Tests

```
python -m pytest tests -q -k sleep
```

`tests/test_sleep_schedule.py` (the fetch windows incl. the DST days, the back-off
and `--retry-now`, publication tracking, the fresh-clock and backwards-clock rules),
`tests/test_sleep_contract.py` (the data contract, DST days, 13:59 vs 14:00,
restart), `tests/test_sleep_signin.py` (the stdlib flow against loopback Okta and
AppSync fakes that check PKCE, the cookie and the bearer token; the dead-login
rules), `tests/test_sleep_deploy.py` (unit, timer, setup script, knobs, egress,
logging). The time tests run under a hand-written US Central zone and, when the
Python has `zoneinfo` and tz data, `America/Chicago` as well; every DST expectation
is a literal UTC instant.

## Known limits

Each is accepted, with the reason.

- **(a) If the feeder itself is down, nothing enforces the 14:00 cutoff.** The
  "unavailable from 14:00" rule is applied by the feeder when it posts; with the
  feeder dead, NagLight keeps showing the last score, coloured, until its own
  36 h `daily` horizon passes. NagLight cannot know that 14:00 passed. This is a
  limit of the design (the horizon is NagLight's), not of the code; the unit's
  journal and the 30-minute timer are the only watch on it.
- **(b) Losing the state file can re-stamp a night later, by up to 5 h.** The frozen
  `observed_at` and the `published` set live only in the state file. If it is lost
  (a reimage, a corrupt file) the next fetch re-stamps a night with the *earlier of
  12:00 local and now*, so a night first published at 07:00 can come back stamped
  as late as 12:00. That is the cost of having no per-record upload time in myAir's
  nightly query; it never makes a night fresher than noon on its own date.
- **(c) systemd's `EnvironmentFile=` mangles a myAir password containing `\` or
  `"`.** systemd unescapes a backslash and strips double quotes in `.env` values, so
  such a password reaches the feeder altered, and the sign-in is refused exactly as
  an expired login would be. Choose a myAir password without those two characters.
  **Apostrophes, measured 2026-10-11** on systemd 255.4 (WSL Ubuntu) with fake
  values through `systemd-run -p EnvironmentFile=`: `pa'ss`, `pass'` and
  `pa'ss'word` arrive unchanged; a value that **starts** with `'` loses that quote
  even with no closing one (`'pass` becomes `pass`), and `'pass'` becomes `pass`;
  `''` is empty. So an apostrophe is safe anywhere except as the first character,
  which the helper refuses. **Double-quoted with whitespace** (what the materialiser
  emits for a password containing spaces), same method: `"a b"`, `"  a b  "`,
  `"a b'c"`, `"'a b"`, `"a b'"`, `"a#b c"`, `"a;b c"`, `"a=b c"` and `"a b$$c"` all
  arrive as the text inside the quotes, intact; `"a "b" c"` and `"a b"c` are altered;
  `'a'b'` becomes `ab'`; `\"` is an escape. `#`, `$`, `=` and `;` inside a value are
  literal. Other systemd versions were not measured.
  The current password has none of these (verified). A `$` is safe: the feeder collapses the
  materialiser's `$$`.
- **(d) The unused-night shape is unproven.** Every probed night had usage, so it is
  unknown whether myAir returns an unused night as a record or as no record. Both
  are handled (a record with a score publishes it; no record is unavailable from
  14:00), but if myAir omits unused nights the wall goes grey on them.
- **A night that uploads after 20:00 is not fetched that night**, so the wall is grey
  from 14:00 until the new night arrives the next morning. The next morning's fetch
  does stage the late night, but it was never on the wall, so the rule "before
  14:00 only a PUBLISHED previous night stays" keeps the gauge **unavailable** until
  today's night arrives. That rule is the outage rule and stays; the 20:00 catch-up
  is what makes this the exception (a night uploaded 16:00–20:00 is published that
  evening).
- **After a multi-day outage that recovers two nights at once, the trend arrow
  compares against the last *published* night, which may be days old, not
  yesterday.** NagLight derives the baseline from what was last posted, and a night
  that was never published is not a reading. This is by design and matches the
  weight gauge's "vs last reading" behaviour.
- **Not run on a real hub:** the `systemd-run` `--retry-now` line, `time-sync.target`
  ordering, and the setup script's `useradd`/`systemctl` paths.
- **Cross-clock skew is out of scope.** The feeder and NagLight share one host
  clock; the only clock rule is that a local date earlier than the newest published
  night refuses to select or publish.

## Limits, said plainly

- **Not medical advice, not a diagnostic.** It re-posts a vendor's number.
- The state file holds the nightly score (a health value) at mode `0600` in a
  `0700` directory; the score also reaches the wall and the Owner's own tracker
  account (plan §4.6).
- **myAir's score is about 70% usage hours** (plan §4.5): a long night with poor
  breathing numbers can stay green. The target of 80 is not calibrated to the
  Owner's actual scores.
- Nothing here reads, writes or touches the CPAP or its SD card.
