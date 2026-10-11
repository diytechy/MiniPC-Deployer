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
- `totalUsage` and the sub-scores are **not requested at all**. The plan's
  "derived night end from `startDate` + `totalUsage`" alternative was not proven
  meaningful by S0b, so it is not used.

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
cannot show that it **fetches nothing and posts nothing new** (a status line in the
journal, exit non-zero):

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

**`MYAIR_PASSWORD` must not contain `\` or `"`** (systemd's `EnvironmentFile=` unescapes
a backslash and strips double quotes; see Known limits (c)).

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

Only `startDate` and `sleepScore` are requested (the upstream query also asks for
the sub-scores and usage; this feeder does not use them, so fewer health values
cross the wire and sit in memory).

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
or prints them). `firstboot.sh` does **not** yet call it — see the report.

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
  The current password has none (verified). A `$` is safe: the feeder collapses the
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
