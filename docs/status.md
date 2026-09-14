> **2026-09-13 panel audit:** [current findings](../../HomeHub/docs/PANEL_CURRENT_2026-09-13.md) take precedence over
> older deployment claims below. No gate or requirement was changed.
> Detailed coordination awaits Owner feedback.

# Project Status — Blackboard

Live coordination for the gated process (see [process.md](process.md)). Keep the
**Current State** header short and current; append the audit log below (newest
last) — it is the record, not required reading for every pass.

---

## Current State

**2026-09-14 — group A, panel audio: intent epoch, coupled mute, echo canceller
and real telemetry (branch `p3-a`, LOCAL ONLY, nothing installed on the panel).**
Four source changes, each with tests, against the coordinator plan's section A.

* **Contract.** The root applier now stamps a backend EPOCH (`generation`) into
  `/etc/wall-panel/audio-state.json`, advanced once per boot by `apply-state`
  finding no marker in `/run/wall-panel/audio-epoch.json`. A broker request may
  name the epoch it was minted against and one naming a dead epoch is refused
  inside the apply lock, leaving the state byte-identical. The reply now echoes
  `seq`, `generation` and the applied state (`effective`), all behind the
  existing `echoSeq` opt-in so an un-upgraded renderer is unaffected. Field
  names of record: OfficeWallNaglight
  `docs/design/audio-intent-contract-2026-09-14.md`.
* **Item J** (supersedes item-23 ruling E). Selecting output Mute now also mutes
  the microphone, as a LATCH on the stored flag rather than a mask, because the
  Owner's second half — the mute is retained after leaving Mute until an
  explicit unmute — is only consistent with a latch. `normalize` applies it, so
  it holds for boot, resume, udev, the rocker and the CLI, not only for broker
  requests. An independent unmute while Mute is selected is refused with a
  reason. `mic_legs_running` is the applier's OBSERVATION after its pass, and
  the broker reports `inputMutedConfirmed: false` whenever a leg may still be
  transmitting.
* **Step 6.** `stack/autoinstall/wall/aec/` is a SpeexDSP echo canceller built
  from the AEC spike's section 6: a pure-C policy core (108 assertions, no ALSA
  and no Speex) plus an I/O shell that owns both PCMs in one process. Built on
  the panel by firstboot, INSTALLED BUT NOT ENABLED — the mic seam moves to
  `mic_clean` only with `WALL_AUDIO_AEC=1`, because acceptance needs a person in
  the room.
* **Item L.** `telemetry` no longer answers `{"available": false}`
  unconditionally. That was structural — the broker cannot open a sound device —
  so two services that already hold their captures publish what they measured:
  `wall-amp-trigger` (already reading `speaker_tap` for the relay) and
  `wall-audio-aec`. No second capture and no routing change.

**Assumptions recorded for the next gate.** (a) Item J's latch means a person
who was live on Speaker, taps Mute, then taps Speaker again comes back MUTED;
that is the Owner's ruling read literally and is flagged for review. (b) The
canceller's bands and ERLE are proven only against a synthetic fixture: the
spike's retained evidence is PNGs and JSON summaries, so no replay against real
captures was possible. (c) Everything hardware-dependent — the rocker chain,
coupled mic silence on both returns, AEC double talk, the amplifier-knob move —
is explicitly unproven and listed in the group's report.

**Installed baseline, read-only inspection 2026-09-14.** The panel's
`wall_audio_state.py` and `switch_backend.py` are one commit behind `c4136e6`;
`panel-volume-request.py` and `wall-volume-request.socket` are absent, as the
plan already records. The state file carries neither `generation` nor
`mic_legs_running`, which is the compatibility case this work handles.


**2026-09-14 — local panel capability provisioning is implemented and
independently reviewed in the isolated checkout; not deployed.** The image now
stages and installs the verified sensor runtime without gateway registration,
publishes a digest-verified model manifest path, removes only its retired camera
blacklist without opening capture, and provides an atomic root local-setup/wake
helper. Saved camera/Bluetooth wake forces display-off instead of S3. Existing
gateway fields and panel PIN/enrollment state are preserved. Active gate remains
G1; independent adversarial review is closed and the next action is coordinator
integration.

**2026-09-14 — panel follow-up D/F source continuation (not deployed).**
The volume rocker uses a narrow up/down-only socket helper to reach the root
applier under its existing DynamicUser restrictions. Successful presses carry
`volume_event_seq`, including at a volume limit. Firstboot carries/enables the
helper payload. Independent terra medium review found normalization drift for
invalid sequence values: canonical and backend normalization now both mark
repair and force input mute. Cross-implementation regression cases cover
negative, boolean and unsafe-integer counters. WSL targeted audio suite:
366 passed. Active gate remains G1; no live deployment or gate advancement.
The coordinator's final validation and hardware limitations are recorded in
HomeHub `docs/PANEL_FOLLOWUP_2026-09-14.md`.

**2026-09-13 — the AI-usage feeder is DEPLOYED.** The gauge-rail redesign's
feeder half (`stack/ai-usage/ai_usage_feeder.py`, plus its README and timer)
went to the hub during the tracker event-log cutover, in the required order:
NagLight, then the feeder, then the panel. Deployed and repo now read the same
sha; the feeder posted all six gauges on its first run — codex, Claude session,
Claude weekly, **Claude weekly Fable** (the scoped sub-column), OpenCode weekly
and monthly. `pace` is a live number on the wire (36.9 / 80.3 / 98.3) where every
gauge used to serve 0, and the rail draws on the wall.

Two things learned doing it, both worth more than the deploy. The deployed copy
was **behind** the repo by 194 lines and a project note said the two were in
sync — that note had by then been wrong in both directions on successive days, so
the habit, not the note, is the answer: compare `sha256sum` on both sides before
believing anything about what is deployed. And `/etc/systemd/system/` holds a
**copy** of the timer unit, not a symlink to `stack/`, so updating the stack copy
alone changes nothing that runs.

**2026-09-13 — LCUS-2 amplifier actuation is PROVEN ON THE PANEL, except the
amplifier itself.** The actuator defaults to the measured CH340 LCUS-2 board
through a stable `/dev/wall-amp-relay` udev link, `audio-jack` is retired and
refused (2026-09-13),
and SN-020/SR-026/LLR-010/TC-010/IF-017 are implemented. On the panel the board
enumerates, the shipped transport drives it, and a full audio-to-relay cycle
runs to the exact configured timings. **The panel now carries hand-installed
copies of four artifacts and is AHEAD of the image** — no image has been rebuilt,
and nothing was pushed. Still unproven: COM/NO amplifier wiring, amplifier
audibility, S3 on hardware, USB-disconnect recovery and long-run behaviour.

**2026-09-11 — Finance-Auditor snapshot recovery is BUILT and LOCAL-ONLY (not
pushed, deployed, or included on existing media).** A reimage now restores the
local-only `finance_snapshots` volume through the existing byte-verifying
`restore-volumes.sh` path. Its profile is named for `docker compose create`
only, so restoration creates the volume without starting the profile-gated
Finance-Auditor service or a bank-sync attempt. `finance_actual_data` remains
excluded because it is a re-creatable Actual API cache. The hermetic
`restore-volumes.test.sh` suite was run on the HomeHub from a temporary source
copy: 38 PASS / 0 FAIL, including a byte-for-byte finance snapshot restore and
the profile-only create assertion. The temporary copy was removed. This joins
the next ISO/USB rebuild; the live hub retains its older manual-finance restore
behavior until a full reimage or a separately approved SSH deployment.

**2026-09-09 — B11's automated check-off is BUILT and LOCAL-ONLY (not pushed,
not deployed).** The Owner changed the `weigh-in` item to `type: automated` with
`check: weight`, and it has synced, so the feeder now posts **two** bodies per
cycle to the same `/api/feed`: the existing `kind: gauge` body for the bar, and
`{"check": "weight", "ok": true}` on the **legacy lane** to tick the item. The
two posts fail independently and the **gauge goes first** — the gauge is what
SN-040 promises, the tick is the extra. The tick fires only on a genuinely fresh
read whose `sampleTime` is strictly newer than the one already ticked **and**
whose civil date is today in the reading's own local frame; the source-failure
re-post path can never tick, which is the whole point. There is **no
`WEIGHT_CHECK_ENABLED` knob** and none is wanted: `type:`/`check:` in the
person's own definitions are the declaration, so nothing is owed to
`stack/.env.example` or to HomeHub's `FieldSchema.psd1`. **The DAY question is
now DECIDED and applied** — see the 2026-09-10 entry below and the next
paragraph. Gate remains G1.

**2026-09-10 — THE TRACKER NOW RUNS IN THE HOUSEHOLD'S TIMEZONE
(Owner-approved). This moves the day boundary for EVERY item in the tracker, not
just weight.** `stack/docker-compose.yml`'s `tracker:` service gained
`TZ: ${TIMEZONE}` (`TIMEZONE=America/Chicago` is already in the hub's `.env`).
NagLight's `time.Now()` — which is what `s.Now()`, the nightly materialize and
the legacy `ok` check-off lane all resolve "today" through — was running in
**UTC**, because that one service was the only one that cares and never got a
`TZ:` while eight others already had one. So **habits, todos, rollovers,
streaks and catch-ups all rolled over at midnight UTC = 19:00 local**; anything
the Owner ticked between 19:00 and midnight was landing on the **next** day's
log. From now on they roll at **local midnight**. Nothing historical is
rewritten — the boundary moves forward only, so the evening of the restart is
the seam. **NOT YET DEPLOYED and it needs a container restart**: Go reads
`TZ` once, when it builds `time.Local` at process start, so an already-running
tracker keeps its UTC clock until it is recreated
(`docker compose up -d tracker` on the hub, which recreates it because the
environment changed). Local commit only, never pushed.

**2026-09-09 — B11 steps 3 and 4 are BUILT on a SIDE BRANCH (`b11-weight-token`),
CROSS-REVIEW FIXES APPLIED, pending merge into `IceDrive-DesktopDirection`, and
neither has ever spoken to Google.** A cross-review by another model family
returned REJECT (10 confirmed + 1 suspected); all of it is fixed here — the
frontmatter reader is now **depth-aware** (four ways a nested block could source
a *wrong body weight* were one defect), the OAuth `state` check is **mandatory**
and duplicated parameters are **refused rather than ranked**, the containment
claim is now true at **every path component** and the token allow-list compares
against the **configured** `WEIGHT_TOKEN_FILE` instead of against itself. Detail
in the audit entry at the bottom of this file. The Owner has cleared steps 1 and 2 (health.googleapis.com enabled on
the project that owns the shared OAuth client; the health-metrics scope on its
consent screen; the Owner a Test user). `stack/weight/weight_oauth.py` now
carries the two Owner-run commands that follow: `mint` walks the Owner through
browser consent once and writes a refresh token into `WEIGHT_TOKEN_FILE`, and
`capture` makes exactly ONE `dataPoints.list` call and saves the raw body to a
file. The chosen consent flow is a registered loopback redirect with **nothing
listening** — the browser is on the PC and the process is on the hub, OOB was
shut down by Google in 2022, and a hub-side listener would need an SSH tunnel
raised before consent or the single-use code is lost. **The Owner must add
`http://localhost:8117/` to the OAuth client's redirect URIs, alongside — never
replacing — the existing `/oauth2/callback` and `/api/drive/callback`.**
**Step 5 is now DONE - see the 2026-09-09 audit entry at the bottom of this
file.** The Owner ran `weight_oauth.py capture` against the live API and got
HTTP 200 with one data point, so the gate B7 set - "one real call, verified,
*before* the parser is written" - was **met, not waived**, and
`parse_weight_datapoint` is written against that observed body. The six "no
parser exists" absence assertions in `tests/test_weight_feeder.py` have been
**replaced by correctness assertions**, deliberately and on the record, rather
than quietly dropped; the equivalent assertion in `tests/test_weight_oauth.py`
**remains**, because the capture tool still must not interpret a body. The
captured body, the Owner's Google user id and their real weight are **not** in
this repo: what is recorded is the body's shape, with a placeholder id and a
made-up weight. Gate remains G1.

**2026-09-09 — B14 Door image integration is implemented and independently
reviewed, not deployed.** The
wall image now installs a hardened `wall-door-stream.service`, creates its
non-login service account, and starts an idle local broker only after the
packaged application exists. Root firstboot extracts only the Door allowlist
from `wall.env` into root-only `/run` files; systemd exposes those to the account
through a RAM-backed credential mount. The broker receives no unrelated panel
secrets, there is no second persistent password file or credential-bearing
process argument, and Electron can reach only `/run/wall-door-stream/service.sock`.
Every image-owned display-off and suspend path stops that broker first; display
on readies only its idle socket, never a camera connection.
The wall template declares the reserved camera address and T3 password
placeholders plus public RTSP/geometry knobs; the SIM uses an unreachable
`.invalid` fixture. The application capability contract now includes
`door-stream-v1`. Application lifecycle/synthetic coverage is complete. Four
independent review rounds closed the image/application boundary findings; the
later task-surface review also accepted the full-height Door geometry and fresh
selection requirement after idle. No panel deployment has occurred. Owner-run
load, latency, reconnect, filter comparison, long-run/concurrent behavior and
Pandora-coexistence checks remain. Gate remains G1.

**2026-09-09 — two Owner rulings applied (see the audit entry at the foot of
this file).** (1) `SLEEP_END` now has **one** default, **06:45**, on both power
paths; the per-path default is gone and SN-015's "the disabled path is
completely unchanged" line is **knowingly relaxed on this one value** — the
schedule-only morning wake moves 06:30 -> 06:45. (2) The ai-cli route cooldown
is accepted at 5 s **on the condition that a refusal says which refusal it is**:
a cooling route now answers 429 with `status`/`reason`
(`success-pacing` vs `failure-backoff`), `retry_after_seconds`/`retry_at` and a
`Retry-After` header; an in-flight row is `status:"running"`; the box-wide
ceiling stays 503 `at-capacity`. None of B12's four security criteria moved.
Next action awaiting approval: the gate is unchanged (G1).

**2026-09-09 cross-review fix round on B9, the wall panel's occupancy power —
verdict REJECT, 10 confirmed findings, all applied.** This is the
highest-consequence block in the build: the panel is wall-mounted with no
battery and no UPS, so a wrong suspend, or a suspend with no working wake,
costs the panel *and* LAN reach to it until somebody physically walks over and
touches it. Every fix below therefore leans one way — **when in doubt, stay
awake with the backlight off.**

**V1 — the panel would not have woken.** `rtcwake -m no -l -t` asserted that
the RTC keeps LOCAL time. It does not: nothing in this image runs `timedatectl
set-local-rtc 1` and nothing writes `/etc/adjtime`, so a stock Ubuntu
autoinstall leaves the RTC in **UTC** — and `-l` therefore programmed the alarm
a whole UTC offset away (a 06:45 alarm firing at 00:45 or 12:45 in Chicago).
The frame is now **established, in order of authority**: `timedatectl show -p
LocalRTC`, then `/etc/adjtime`'s third line, then UTC as the default — and the
alarm is armed `-u` or `-l` to match. The armed epoch is logged in local
wall-clock terms, which is what the requirement is written in.

**V2 — a fixed 86400-second day.** Across a DST boundary that lands at 05:45 or
07:45, not 06:45. The target is now the **next local calendar occurrence** of
the wake time (`date -d "<today> <wake> tomorrow"`), and A18 asserts it by
running the real script at a fixed instant on the night the clocks go back.

**V3 — `SLEEP_END`'s shipped default. Flagged for the Owner, and the Owner
ruled: ONE default, 06:45.** The B9 fix round made the shipped default
path-dependent — 06:45 with `WALL_ABSENCE_ENABLED=true`, the pre-existing 06:30
with it false — so that SN-015's ratified 06:45 occupancy wake and its "the
disabled path is completely unchanged" line could both hold from one knob. It
was flagged rather than silently picked, and on **2026-09-09 the Owner ruled it
collapsed to a single 06:45 default on both paths**: two shipped defaults for
one knob is a rule nobody will remember in a year. See the ruling entry below.

**The block's central rule was broken, and is now the decider's to enforce.**
The absence clock was the shell's own bookkeeping ("absent, so start counting"),
which counted straight *through* the on-period: absent from 12:00 and still away
at 22:00 arrived at the boundary already carrying 600 minutes and suspended on
the very first tick outside — the required hour **outside** the on-period never
observed at all. `decide()` now returns `absence_clock` alongside the backlight
and the power action, and it is CLEARED for every minute that is present or
inside the on-period.

**The other accepted findings, each with a test that fails without it.**

* **A truncated absence clock read as decades of absence.** A `1` left by an
  interrupted write is 1970, i.e. an immediate suspend. It is now written
  **atomically** (temp file + rename) and **range-checked** on read
  (`MIN_PLAUSIBLE_EPOCH`, plus a future-stamp bound); an unusable clock restarts
  the timer rather than being believed or left in place to be rejected forever.
* **`json.loads` accepts `NaN`** — and every comparison against NaN is false, so
  a presence file stamped `"observedAt": NaN` passed the staleness check, passed
  the skew check, and was believed when it said "absent". That inverts the whole
  fail-safe direction. The parse now refuses the three non-finite constants, and
  an in-range literal that overflows to infinity (`1e999`) is refused as well.
* **A zero exit from `rtcwake` was taken as proof of an alarm.** Firmware or a
  wrapper can return success and program nothing. The alarm is now **read back**
  from `/sys/class/rtc/rtc0/wakealarm` (with the UTC-frame shift accounted for
  when the RTC is local) and an unverifiable alarm degrades to backlight-off on
  BOTH paths. The suite's fake could not previously express this failure — it
  only recorded arguments — so the old "armed" assertion passed in exactly the
  mode it was meant to catch; the fake now programs a fake RTC, and can lie.
* **A missing or read-only backlight did not stop the suspend.** The decision
  being applied is "nobody is here: go dark, THEN sleep", and `backlight_set`
  now **reads the brightness back**. If the dark half did not happen the suspend
  is refused: lit AND unreachable is the one outcome worth avoiding above all,
  and a lit awake panel is something somebody can SSH into. The SCHEDULED path
  is deliberately untouched by this — it never claimed to dim anything.

**Both test-quality findings rewritten to assert the property, not the
spelling.** A11 greped the literals `class/backlight` and `systemctl suspend`;
it now scans **every** file in the wall tree (units included, comment lines
stripped) for **any** mechanism that could dim a screen or sleep a machine, and
counts the decider invocations at RUNTIME. A12 greped `wall.env.example` for
alternative knob names; it now **moves** `SLEEP_END` to an unusual value and
requires the same value to appear in both jobs in the same run, and separately
requires that exactly two variables in the wall scripts hold a wall-clock time.
Both mutants the review described — `loginctl suspend` elsewhere, a
`WALL_ON_START` defaulted from `SLEEP_END` — now go red.

**Tests.** `python scripts/check.py` **525 passed / 5 skipped** (baseline
517/5); `scripts/trace.py --strict-integrity` **0, orphans 24** (= baseline);
`check_flows.py --no-placeholders` **OK, 4 diagrams**;
`occupancy-power.test.sh` **73 PASS / 0 FAIL** run standalone (was 42/0).
`stack/run-hermetic-tests.sh` was **NOT run** — it refuses on this dev PC for
want of `zstd` and `rsync`, reported as UNRUN, never as passing.
**11 deliberate defects, 11 killed, no first-pass survivors** — and M1 (the RTC
frame forced back to `-l`) was killed by the readback guard as well as by its
own case, which is the cross-check the last round's lesson asked for.

**Nothing live changed.** No panel or hub state touched, no unit installed, no
service started. No apt package name added, so no apt export is owed.

---

**2026-09-09 cross-review fix round on BOTH feeders (B7 SR-021 + B11 SR-022) —
verdict REJECT, all findings applied.** A different model family reviewed the
usage feeder and the weight feeder together; the coordinator verified the two
worst in source and accepted the rest. These are the most serious defects this
build has produced, because they act on the household's **real vendor
credentials** rather than on fixtures.

**V1 — the credential guard was bypassable by symlink.** `open_for_write`
compared `os.path.abspath`, which is string arithmetic and does **not** resolve
symlinks. Pre-creating `<state>.tmp` as a link pointing at a vendor token
therefore passed the allow-list unchanged — the *string* matched — and
`save_state` opened it `"w"` and truncated the token. The allow-list SHAPE was
right; the resolution was wrong. Now:

* `writable_path_verdict` resolves with `os.path.realpath`, and it is a **pure
  decision with an injected resolver**, so the rule can be asserted on a
  filesystem that will not grant symlinks as well as on one that will;
* the resolved path must sit **inside the service's own `StateDirectory=`**
  (`$STATE_DIRECTORY`, falling back to the literal `/var/lib/homehub-{ai,weight}`
  for a hand-run). The allow-list alone is derived from a path the *config*
  names, so it can only ever say "the feeder wrote where it was told"; this
  bound is the one the `.env` cannot move;
* `open_no_follow` does not trust the check that preceded it. The file is
  unlinked first — which destroys a planted *link* and never the file it points
  at, and clears a `.tmp` left by a killed cycle — then opened
  `O_CREAT|O_EXCL|O_NOFOLLOW`, so a symlink planted **between** the check and
  the open is refused by the kernel. `O_NOFOLLOW` is absent on Windows and
  resolves to 0 there; the hub is Linux, so the deployed guard is whole and the
  dev PC still exercises the unlink, `O_EXCL` and the resolving verdict.

A refused write costs the **history**, never the gauge: the cycle has already
posted, and the refusal is a named failure in the journal.

**One redundancy was found and removed rather than reported as depth.** The
first cut bounded both `state_path` and the path being opened. The mutation run
killed neither — each hid the other's absence — because the allow-list pins the
opened path to the state file or its `.tmp` anyway. It is now **one** check, on
the path actually about to be opened, and deleting it goes red.

**V2 — the feed could leave the box two ways, and did not need the URL to
change.** All four call sites (two readers, two posters) used a bare
`urllib.request.urlopen(Request(...))`, and urllib's **default opener** follows
redirects *and* honours `http_proxy`/`https_proxy`. So the validated loopback
endpoint could answer `302 Location: http://attacker.example/feed` and urllib
would re-send the POST — feed bearer token, `X-Forwarded-User` identity, and a
body that for the weight feeder is **the Owner's body weight** — to a host the
*response* chose; and an exported proxy variable routed the same POST through a
LAN proxy that saw all of it. urllib does **not** strip `Authorization` across a
cross-host redirect, so the vendor GETs leaked in the same way.

Two openers now, and the difference between them is a decision rather than an
oversight:

| | feed POST (`feed_opener`) | vendor GET (`vendor_opener`) |
|---|---|---|
| redirects | refused | refused |
| proxies | `ProxyHandler({})` — none installed | none installed, **and no knob to opt back in** |
| peer address | re-checked on the socket **before the request is written** | not checked — outbound by design |

**The vendor decision, stated because it was asked for.** Those calls are
*supposed* to leave the box, so a peer check would be nonsense. Redirects are
still refused, because a 302 from an impersonated endpoint hands out the
household's Claude OAuth access token, the OpenCode workspace key, or (once the
weight half is unblocked) a Google Health token that grants blood glucose, body
fat, oxygen saturation, core temperature and heart-rate metrics as well as
weight — there is no weight-only scope. All three endpoints answered **200
directly** during the 2026-09-09 verification calls, so refusing costs nothing
that has ever been observed, and the price if a vendor starts redirecting is an
"unavailable" gauge — the outcome this feeder exists to produce. Proxies are
**not** made opt-in-able: a proxy knob nobody needs is a second way for a token
to leave, and an egress proxy would be a requirement change rather than a
setting. A vendor URL must also be `https`, because the URL is a knob and the
headers carry a bearer token.

The peer check runs inside `connect()`, after the handshake and **before**
`http.client` writes the request line, so a connection that lands somewhere it
should not is dropped with the token and the body unsent. That is what
"re-validate the address actually connected to" means here; the feed host is
already required to be an IP literal, so the URL check and the socket check
agree by construction and the socket check is what survives a future change.

**The other accepted findings, each fixed and each with a test that fails
without it.**

* **A malformed-but-200 payload became a 7-day-fresh gauge.** `window.kind`
  *selects* NagLight's staleness horizon, so a Claude bucket with `utilization`
  and no `resets_at`, or an OpenCode bucket missing `resetsAt`, produced a
  windowless gauge that inherited the **static** horizon — seven days — instead
  of the 24–48 h its real window implies. A source that died on Monday was
  still green the following Sunday. `check_window` now refuses a window it
  cannot determine, so that bucket becomes **unavailable**; `build_gauge`
  refuses to construct a body that carries `observed_at` without a window at
  all. The one legitimately windowless body is the never-measured sentinel, and
  it is exactly the one with no stamp.
* **A replayed or cached vendor 200 was re-stamped `now`.** The vendors do not
  say when the value was true, but they do say when the window resets, and that
  is the evidence there is: the window must **contain** `now`. A reset already
  in the past means the body describes a finished window and is a replay or a
  cache; a window that has not begun is the same evidence from the other side
  (a clock error, or milliseconds read as seconds). `MAX_CLOCK_SKEW_SECONDS`
  (300) is the only slack. The weight feeder already used the source's own
  sample time rather than `now` — the fix there is `check_observed_at`, which
  **refuses** a future stamp rather than clamping it, because clamping would
  invent the stamp the invariant forbids.
* **An invalid Codex window aborted the whole cycle.** `usedPercent: 34` with
  `windowDurationMins` of `0`, `-1`, `NaN` or infinity parsed cleanly and only
  raised later in `build_post`, **outside** the per-source catch — so the cycle
  exited before any gauge was posted and every other source's previously fresh
  value stayed green until it expired. Two fixes, and both were needed: the
  window is validated **inside the reader**, and each gauge is now built and
  posted in **its own try**, falling back to the unavailable sentinel so a
  refusable body costs one gauge rather than the cycle.
* **Loaded state was never validated.** Types, ranges and timestamps were all
  trusted; `build_gauge` asked only whether the number was finite. Corrupted or
  tampered state posted fabricated fresh readings — `-500 lb`, `100000` percent,
  or a stamp in the **future** that made a dead source render live.
  `validate_stored_reading` now runs **at the load and at the point of use**,
  which are separated by the whole source read. A nonsensical stored reading is
  not history, it is a failure, and it gets the answer a source that never
  succeeded gets. Weight also gained a plausibility band (40–1000 lb, the same
  numbers the goal is held to, now with one home and two names) applied to any
  body that carries a stamp — the sentinel `0` is the only unmeasured number
  either file may emit and it only ever appears with no stamp.
* **Error bodies landed in the journal.** Both posters returned
  `exc.read()[:200]` and `main` prints it to stderr, so anything a responding
  server or an interposed proxy chose to reflect — a token echoed back in an
  error body included — was persisted by systemd. The status code is ours to
  read; the body is the remote's to write, and it does not get a journal. The
  same reasoning removed the one remaining message interpolation: a
  `SystemExit` echoing an unparseable `AI_USAGE_FEED_URL` could have printed
  `http://user:token@host/`.
* **B11's broad `except Exception` recorded `str(exc)`.** B7 logged only the
  exception TYPE and that pattern is now copied. This is the branch the real
  OAuth reader will fall into, and an exception raised inside urllib carries the
  request object — `str(exc)` on one of those prints an `Authorization` header.

**THE INVARIANT WAS NOT BROKEN, and it is asserted whole in both suites.**
`observed_at == now` **iff** this cycle actually read the source. A failure with
history reposts the last real value at its ORIGINAL stamp; no history posts
value 0 with **no** `observed_at`. Every change above tightens what counts as
"read the source" and none of them creates a new way to stamp a reading this
cycle did not take. The wire contract is unchanged: `value`/`target` always
present, `direction` required with a `window` and refused without, `min`/`max`
together or both omitted (weight still omits them so NagLight infers its 50 lb
range), numbers only and never a colour.

**B11 IS STILL BLOCKED BY CONSTRUCTION.** No parser exists, `read_google_health`
still refuses by name, and the test asserting no `parse_google_health` /
`parse_weight_datapoint` symbol still passes — with a new test that re-asserts
both *after* this round, because a fix round is exactly when a blocked path
quietly acquires a way through. `vendor_opener` was written for that blocked
half deliberately: the egress defect happened because every call site reached
for the convenient function, and the Google Health reader is the one call site
still unwritten.

**SHARED VS MIRRORED: mirrored, deliberately, with a parity test as the price.**
The two feeders ship as standalone scripts under separate units, separate
unprivileged accounts and separate `StateDirectory=` roots, run as
`/usr/bin/python3 /opt/homehub/stack/<service>/<file>.py` under
`ProtectSystem=strict`; there is no importable module between them and adding
one is a carriage change (a third install path, a `sys.path` two units must
agree on, and a shared failure surface for two services required to fail
independently). So every guard was applied twice — and the cost of that decision
is `tests/test_feeder_egress_parity.py`, which asserts the shared properties
against **both** modules by behaviour rather than by comparing source text (a
text comparison passes on two identically broken copies, which is the state the
review found). It also refuses a bare `urlopen` anywhere in either file, which
is what will fail if the session that finally writes the Google Health reader
reaches for the default opener. Only the genuinely divergent parts differ:
`check_window` is B7-only (weight has no window by design) and the plausible-lb
band is B11-only.

**Evidence.** `python scripts/check.py` — **PASS** at gate G1, all four steps:
config-validate, unit-tests **517 passed / 5 skipped** (baseline 423/5),
registry-integrity SN=16 SR=22 LLR=6 TC=6 **integrity=0**, doc-navigability OK.
`python scripts/trace.py --strict-integrity` — **exit 0**, orphans 24 =
baseline. `python scripts/check_flows.py --no-placeholders` — **OK, 4 diagrams**,
10 ids, all known (both feeder flows were updated to show the new refusals).
`stack/run-hermetic-tests.sh` — **UNRUN**: it REFUSES on this dev PC for want of
`zstd` and `rsync`, which is the pre-existing gap and is reported as UNRUN, not
as passing.

**Mutation: 28 deliberate defects, 28 killed** — after two passes that found
real gaps rather than confirming the first set. The first pass had **seven**
survivors and each one was a defect in the *tests*:

| Survivor | What it exposed | Fix |
|---|---|---|
| state-root bound (×2) | the two containment checks were the same check twice | collapsed to one; deleting it now goes red |
| `open_no_follow` → plain `open` | the mutant left the `unlink` in place, so it removed nothing | mutant restated to remove unlink + `O_EXCL`; weight lacked the planted-symlink test entirely |
| missing-window branch | `window_kind_for(None)` raised a *type* error one line later — the right outcome by accident of ordering, with a journal message describing the wrong problem | the refusal message is now asserted |
| state validation (×3) | `load_state` and `build_post` each hid the other's absence — a guard only the other guard proves is a guard nobody has tested | each layer is now asserted where it acts |

The final table, every mutant taking the suite red on the check meant to notice
and green again on restore: `realpath`→`abspath` (both feeders) · state-root
containment dropped (both) · `open_no_follow` → plain truncating open · feed
opener follows redirects (both) · feed opener honours `http_proxy` (both) · peer
check removed (both) · vendor opener back to the default (both) · a stamped
gauge may be windowless · a missing vendor window accepted · an already-passed
reset accepted · the codex window no longer validated in the reader · per-gauge
build/post isolation removed (both) · loaded state trusted (both) · stored
reading trusted at the point of use · remote error body journalled (both) · the
broad catch journals the message (weight) · a future stamp accepted · the
plausible-weight band removed · a vendor URL may be `http`.

**One test constant had to be re-derived rather than copied — the B11 lesson
applied to B7.** `NOW` in `test_ai_usage_feeder.py` was `1789000000`, which is
2026-09-10 00:26 UTC — an hour and a half **after** Claude's five-hour window had
already reset. It was never an instant at which the recorded fixture body could
have been returned, and it only stopped being harmless when the parsers began
checking that a window contains `now`. It is now `1788944000`, an instant that
sits inside all five observed windows, and the reason is written beside it.

**Nothing live changed.** No hub or panel state was touched, no service started,
no account created. **No knob was added or removed**, so HomeHub's
`FieldSchema.psd1` is untouched and no apt export is owed; `.env.example` gained
only a comment on each `*_STATE_FILE` saying the path must stay inside the
service's `StateDirectory=`, which the shipped defaults already do.

---

**2026-09-09 the goal moved from a file-level key to the weigh-in item's
`target` (B11 correction, SR-022/LLR-006/TC-006/IF-014). READ THIS BEFORE
"RESTORING" `weight_goal_lb`.** The entry below describes the first version,
which put the goal in a top-level frontmatter key `weight_goal_lb` and
**deliberately refused an item-level goal** — mutation **M10**, "item-level
goal", was killed by a test that enforced the refusal. The Owner has ruled that
design out and it is now reversed. It did not survive this household's
configuration, and neither of the two reasons is a preference:

* **Drive SHEET mode erases top-level keys, and this household runs sheet mode.**
  `TRACKER_DRIVE_FOLDER_ID` is **empty** and `TRACKER_DRIVE_SHEET_ID` is **set**
  on the live hub. In folder mode (`drive.applyFolder`) the `.md` bytes are
  staged verbatim and any key rides along; in sheet mode NagLight's
  `internal/defsheet` **regenerates** each `.md` from a fixed `columns` list
  which is the **item** field set, and a column it does not recognise is
  collected into `unknown` and **dropped**. So `weight_goal_lb` would be deleted
  by the first sync after anyone edited the sheet — silently, leaving the panel
  dark with nothing saying why. The old entry called this a "known gap needing a
  NagLight change". It was not a gap; it was the design being wrong for the
  deployment that exists.
* **There is no vendor fallback.** Google Health v4 has **no goal or target
  concept at all**: `DataPoint` has 43 members and none is a goal, `Profile` and
  `Settings` carry none, and the only two occurrences of "goal" in the 292 KB
  discovery document (revision 20260908) are a UI settings enum. Nothing could
  have supplied a goal if the definitions lost it.

`target` and `unit` are **already** sheet columns that round-trip today, so the
goal now lives where the sync actually carries it. The Owner has added the row
and it has already synced to the hub:

```yaml
category: Health
color_weight: 1.5
  - id: weigh-in
    title: Step on the scale
    type: habit
    recur: weekly
    horizon: long
    target: 170
    unit: lb
```

**WHICH ITEM IS CONFIGURATION, NOT A HARDCODED STRING.** `WEIGHT_ITEM_CATEGORY`
(default `Health`) and `WEIGHT_ITEM_ID` (default `weigh-in`) name the item's
**location**. Both match case-insensitively and trimmed, because both are typed
by hand into a spreadsheet cell and `Health` against `health` must not be the
difference between a goal and a dark panel.

**THE "NO GOAL KNOB ON THE HUB" PROPERTY IS INTACT AND STILL MEANS SOMETHING.**
The two new knobs name a **place** and can never carry a number, so the
household's intent still lives only in the person's own definitions and still
syncs with them. They are deliberately **not** called `WEIGHT_GOAL_*`, so the
existing file scan — no `WEIGHT_GOAL` / `WEIGHT_TARGET` / `GOAL_WEIGHT` /
`TARGET_WEIGHT` name may be **declared** in `.env.example` or `FieldSchema.psd1`
— is not quietly satisfied by a rename. And the runtime negative now sets the
two location knobs to `170` as well as every goal-shaped knob, and still gets no
goal.

**THE UNIT IS CHECKED, NEVER ASSUMED, AND THAT IS THE SHARPEST EDGE IN THE
BLOCK.** A missing `unit`, or any unit but `lb`, is a **refusal**; nothing is
converted. `target: 77` with `unit: kg` is 170 lb, and **77 sits inside the
40..1000 lb plausibility band**, so the band *cannot* catch it — the panel would
show "77 lb" against a real 191 lb reading and paint it full red. The unit is
therefore checked **before the number is even parsed**, so a kilogram target
fails on the unit rather than misleadingly on the band. This is the same failure
class as a vendor parser written from a schema, which this block still refuses to
write: confident, plausible, and wrong about a person's body, with nothing on the
wall able to tell anyone.

**BOTH PRESENT IS A REFUSAL, NOT A PRECEDENCE.** If a file still carries the
superseded `weight_goal_lb` **and** the item carries a `target`, the feeder
refuses rather than picking one. Silent precedence is the trap: whichever way it
fell, the person would be looking at a bar drawn around one number while a
different number sat in their file looking equally authoritative — and in sheet
mode the top-level one is about to be deleted underneath them, so "the newest
edit wins" is not even stable. It is the same rule this module already applies to
two files declaring a goal. A file carrying **only** the legacy key is also
refused, with a message that names the key, says it is **no longer read**, and
says where the number goes — not "no goal declared", which would leave a person
upgrading staring at a `weight_goal_lb: 180` line while the journal said nothing
was declared. An item that exists but carries no target *beside* a lingering
legacy key gets the migration message rather than the both-present one, because
that person is mid-migration and "delete one" would leave them with no goal at
all.

**A blank, absent, non-numeric, non-finite or out-of-band `target` is no goal,
and nothing is posted.** Whether it surfaces as `GoalMissing` (nothing was
declared) or `ValueError` (something was declared that cannot be used), the
outcome is the same and is the one that matters — the poster is never called and
the journal names the file. The line between the two exception types is
deliberate: *nothing declared* is missing; *something declared we cannot use* is
a refusal that names the file, because a typo'd `1700` for `170` must not decay
into "no goal declared".

**The two refusals that must not collapse into one are preserved.** No **source**
still posts an unavailable gauge; no **goal** still posts **nothing**. Their
tests are unchanged in intent and both still assert the poster was never called.

**FIXTURE: THE OWNER'S REAL FILE, NOT ONE SHAPED TO SUIT THE PARSER.**
`HEALTH_MD` in `tests/test_weight_feeder.py` mirrors what actually synced —
`category: Health`, `color_weight: 1.5`, `horizon: long`, `recur: weekly`, and a
sibling item on **either side** of `weigh-in`, one of which carries a `target`
and `unit` of its own (a step count). Finding the goal by shape rather than by id
would post a step goal as a body weight, and a test asserts it does not.

**THE FieldSchema HALF IS OWED TO HomeHub.** `WEIGHT_ITEM_CATEGORY` /
`WEIGHT_ITEM_ID` are in `stack/.env.example` with their defaults but are **not**
in `scripts/deploy/FieldSchema.psd1`, which lives in the HomeHub repo — a repo
this worktree may not edit. That is safe rather than broken: an undeclared knob
is simply absent from the emitted `.env`, a blank knob takes the default, and the
default *is* the shape the Owner's sheet already syncs. What it costs is that
**moving** the item currently needs an `.env` edit on the hub rather than a
deploy-config change. A test records this explicitly so it is not "fixed" by
adding the knobs to the list that would turn the FieldSchema assertion red.

**STILL NO PARSER, AND ALL SIX ABSENCE ASSERTIONS ARE STILL GREEN.** Nothing in
this change touched the blocked vendor half: `read_google_health` still refuses
by name, and no `parse_google_health` / `parse_weight_datapoint` / `parse_weight`
/ `parse_datapoints` symbol exists. `grams_to_pounds` is untouched and is **not**
reachable from the goal path — it converts a vendor **reading**, and no unit
conversion exists anywhere on the goal path by construction.

**The redundancy that carried M25 is gone, and that is recorded rather than
silent.** The old top-level scanner had three overlapping guards against reading
an indented line as the household's goal, so removing any one of them left the
suite green (M25) and only removing all three was killed (M27). The frontmatter
is now parsed once, into top-level keys and items, so the indent rule exists in
exactly one place and is asserted directly. The "top-level keys stop at `items:`"
rule likewise survives as one line with its own test — the test that the earlier
mutation run had to be written to add.

**THE OWNER'S OPEN QUESTION — AUTO-CHECK-OFF — IS ASSESSED, NOT BUILT.** See
"Auto-check-off: what it would take" below.

**Tests:** 599 passed, 6 skipped (baseline before this change: 578/6);
`trace.py --strict-integrity` clean, `check_flows.py --no-placeholders` clean
(5 diagrams). **UNRUN:** `stack/run-hermetic-tests.sh` still refuses on this dev
PC (`missing tool(s): zstd rsync`).

**Mutation run: 18 deliberate defects (M28–M45), all 18 killed — but TWO
SURVIVED THE FIRST PASS AND BOTH WERE TEST DEFECTS.** That is the third round
running in this worktree where the first-pass survivor was a bad assertion
rather than a missing guard, so it is worth naming the shape: both survivors
were tests that asserted the *outcome the person sees* ("nothing was posted",
"it refused") when a **different guard downstream** was already producing that
outcome, so the line under test was carrying nothing.

* **M37 — the prose body read as frontmatter.** Survived. The test put a bare
  `- id:` block after the closing fence, which the parser skips anyway because a
  column-zero line ends the item sequence, so the fence check was never what
  refused it. Rewritten to re-open `items:` at column zero in the prose — the
  only shape that actually reaches the item reader — and it now dies.
* **M45 — a blank target silently becomes `0`.** Survived. The refusal was being
  carried by the **40..1000 lb plausibility band**, which catches the zero, so
  "nothing was posted" stayed true and the parametrised test could not tell. A
  new test asserts the exception TYPE through the real loader: a blank target
  must be `GoalMissing`, not a band violation, or the journal tells the person
  their goal is out of range when what they have done is not set one.

| # | deliberate defect | killed by |
|---|---|---|
| M28 | unit check removed — any unit accepted | `test_a_plausible_kilogram_target_is_refused_by_the_unit_not_the_band_sr022` (+ all 7 `..._is_refused_and_never_converted_sr022` params) |
| M29 | a missing `unit` assumed to be `lb` | `test_a_target_with_no_unit_at_all_is_refused_sr022` |
| M30 | item found by shape (first item with a `target`) instead of by id | `test_the_siblings_targets_are_not_the_weight_goal_sr022` |
| M31 | both keys present → the item silently wins (precedence, not refusal) | `test_both_a_legacy_key_and_an_item_target_are_refused_not_ranked_sr022` |
| M32 | a legacy-only key silently ignored, falls through to "no goal" | `test_the_superseded_top_level_key_is_refused_not_silently_ignored_sr022` |
| M33 | the 40..1000 lb plausibility band removed | `test_an_unusable_target_posts_nothing_at_all_sr022` (4 params) |
| M34 | the location knobs ignored — `Health`/`weigh-in` hardcoded | `test_the_location_knobs_reach_the_loader_through_a_whole_cycle_sr022` |
| M35 | two files holding the item → take the first | `test_two_files_holding_the_item_are_refused_not_ordered_sr022` |
| M36 | a field declared twice → last one wins | `test_a_target_declared_twice_on_the_item_is_refused_sr022` |
| M37 | the prose body read as frontmatter | `test_a_target_in_the_prose_body_is_not_the_goal_sr022` **(survived pass 1 — test defect, rewritten)** |
| M38 | top-level keys do not stop at `items:` | `test_a_top_level_key_after_the_items_sequence_is_not_a_declaration_sr022` |
| M39 | indented lines treated as top-level keys | `test_an_indented_legacy_key_is_an_item_field_not_a_top_level_one_sr022` (+29 others) |
| M40 | `WEIGHT_GOAL_LB=180` **declared** in `.env.example` | `test_no_goal_shaped_knob_is_declared_anywhere_deploy_reads_sr022` |
| M41 | no goal → invent 180 and post anyway | `test_a_missing_goal_posts_nothing_at_all_sr022` (+ `test_main_reports_the_refusal_and_exits_nonzero_without_posting_sr022`) |
| M42 | category/id matched case-sensitively | `test_the_category_and_id_match_the_way_a_person_types_them_sr022` |
| M43 | the `.env.example` default drifts from the code default | `test_the_item_location_knobs_are_declared_in_env_example_sr022` |
| M44 | duplicate item ids in one file → take the first | `test_one_file_holding_the_item_twice_is_refused_sr022` |
| M45 | a blank target becomes `0` rather than "no goal" | `test_a_blank_target_is_MISSING_not_a_zero_sr022` **(survived pass 1 — test defect, new test added)** |

**One check IS carried by another, and it is named rather than left implicit.**
M29 (a missing `unit` assumed to be `lb`) and M28 (the unit compared at all) are
two lines guarding one property, and each has its own test, so neither is
carried. But the **indent** rule (M39) and the **stop-at-`items:`** rule (M38)
are now single lines in one shared parser rather than the three overlapping
guards the old scanner had — the redundancy that let M25 survive last round is
gone, and each is asserted directly. Nothing else in this block is defended by
more than one mechanism.

### Auto-check-off: what it would take, and why it is not built

**SUPERSEDED 2026-09-09 — the check-off IS built; see the audit entry at the
end of this file. Keep reading for the hazards it names, but note that the
route guessed at here (`POST /api/check {id, done, expectedDate}`) is NOT the
one used: the legacy `/api/feed` lane carries no `expectedDate` and cannot
back-date at all.**

The Owner asked whether the feeder could tick the `weigh-in` habit off when
Google reports a new weight sample, via `POST /api/check {id, done,
expectedDate}`. Three hazards, and the first is the one that decides it:

* **It must fire on a new `sampleTime`, not on a successful read.** The feeder
  reads every 15 minutes and reposts the last known reading when the source
  fails; a check-off keyed to "the cycle worked" would tick the habit off
  hundreds of times against a weigh-in that happened days ago. The trigger is a
  `sampleTime` strictly newer than the one in the state file — which means the
  state file grows a second responsibility, and it is the file this block spent
  a mutation round proving is only ever written on a successful read.
* **`expectedDate` must be the sample's own local date, not today's.** A Sunday
  evening weigh-in read on Monday morning would otherwise tick off Monday and
  leave Sunday's box empty — the feeder would be recording a fact about the
  wrong day. That needs the household's timezone applied to the sample's
  instant, which this feeder does not currently carry.
* **Idempotency is not established.** This repo has never exercised the
  `{id, done, expectedDate}` shape at all: the only check-off it drives is
  `sim/validate-sim.sh`'s `POST /api/check {"id": "..."}` with
  `X-Forwarded-User`, which flips **today's** box and returns 200. Whether a
  repeat POST is a no-op or a toggle is a NagLight-side fact that has not been
  read, and a toggle would make a retry *un*-check a habit the person already
  did. That has to be verified with one real call before anything is written —
  the same gate that is keeping the vendor parser unwritten.

**A separate interaction worth naming: `recur: weekly` against a windowless
gauge's static 7-day staleness horizon.** The gauge sends no `window`, so
NagLight renders it stale after **7 days** — and the item's declared cadence is
**weekly**. Those are the same number, so a person weighing in exactly on
schedule has **zero margin**: the gauge goes stale in the hours before each
weigh-in, and any slip to day eight shows "unavailable" for a reading that is
perfectly current by the household's own rule. The 15-minute timer does not help
— it governs how fast an *absent source* becomes visible, not the horizon. This
is not caused by the change above and is not fixed by it; the honest options are
to accept the stale window before each weigh-in, or to raise it with NagLight as
a horizon that should follow the item's cadence. Sending a `window` to buy a
different horizon is **not** an option: `weekly` maps to a **24-hour** horizon,
which is far worse, and a `window` would drag `direction` in with it.

---

**2026-09-09 (later still) B11's deployment blocker is FIXED IN SOURCE, NOT YET
DEPLOYED.** On the hub the feeder could not read the goal: the drop-in bound the
definitions DIRECTORY at its own path, and that directory is `drwx------ hub hub`
under root-only ancestors. `setup-weight.sh` now resolves WHICH `.md` declares
`WEIGHT_ITEM_CATEGORY` by reading its frontmatter (never by guessing a filename)
and binds THAT ONE FILE read-only into the account's `StateDirectory`, with the
service's `WEIGHT_DEFINITIONS_DIR` overridden by a generated `EnvironmentFile=` -
`Environment=` does NOT win over the unit's `EnvironmentFile=`, measured on
systemd 255. Scope is the reason as much as the mechanism: the volume holds every
household member's tracker data. See the audit entry at the end of this file for
the deploy order the coordinator owes.

---

**2026-09-09 the weight feeder (B11, SR-022/LLR-006/TC-006/IF-014) — PARTIAL,
and the blocked half is a finding, not a gap.** The hub gains a third plain
service — no container, on a 15-minute timer — that posts one body-weight gauge
to NagLight under the same id B5's manual `feeders/weight-manual.ps1` uses, so
it REPLACES that feed by upsert rather than standing a second bar beside it. It
ships **OFF** (`WEIGHT_ENABLED=false`) and declares no credential.

**THE API IS REAL, AND THE ONE REAL CALL WAS MADE BEFORE ANY PARSER — SO NO
PARSER EXISTS YET.** The build plan named "Google Health API v4" and told this
block to verify that before building on it. It verified:

* `GET https://www.googleapis.com/discovery/v1/apis?preferred=false` → **200**,
  531 APIs, including `health:v4` and `health:v4beta`. It is **not** Google Fit
  (`fitness:v1`, closed to new sign-ups) and **not** Health Connect
  (Android-device-local, no server REST path). It is the successor to the
  **Fitbit Web API**, fed from Fitbit, Pixel Watch and partner apps.
* `GET https://health.googleapis.com/$discovery/rest?version=v4` → **200**,
  292 545 bytes, `revision 20260907`. The Weight type is real and exact:
  `Weight {sampleTime: ObservationSampleTime (required), weightGrams: double
  (required), notes: string}`, reached as the `weight` member of the `DataPoint`
  union.
* `GET https://health.googleapis.com/v4/users/me/dataTypes/weight/dataPoints`
  unauthenticated → **401 UNAUTHENTICATED**, whose `ErrorInfo` names
  `service health.googleapis.com` and
  `method google.devicesandservices.health.v4.DataPointsService.ListDataPoints`.
  **Routing happens before authentication**, so a 401 naming the backend RPC is
  proof the route resolves and that auth is the only gate left.

**THE PLAN'S SCOPE ASSUMPTION IS WRONG AND THAT MATTERS TO THE OWNER.** The
plan said Weight has "its own OAuth scope". It does not — there is no
weight-specific scope in the 21 the discovery document lists. The only scope
that admits the Weight data type is
`googlehealth.health_metrics_and_measurements.readonly`, which **also grants
blood glucose, body fat, oxygen saturation, core body temperature and
heart-rate metrics**. Consenting to a weight bar consents to all of those.

**The prose docs are also wrong about the path.** `developers.google.com`
renders the read as `/v4/users/me/dataPoints/weight`; the discovery document —
which is what a client is routed by — says
`v4/users/{usersId}/dataTypes/{dataTypesId}/dataPoints`. Exactly the class of
error the verify-first gate exists to catch.

**BLOCKED ON THE OWNER, AND THEREFORE NO PARSER WAS WRITTEN.** No authenticated
call has ever been made, because that needs (1) `health.googleapis.com` enabled
on the Cloud project that owns the existing OAuth client, (2) the scope added to
that client's consent screen plus the Owner on its Test users list, and (3) the
Owner at a browser minting a refresh token. B7 made "one real call before the
parser" this build's standard, and **body weight is the worst place to break
it**: a usage parser one field off posts a silly percentage; a weight parser
that reads kilograms as pounds posts a confident, plausible, wrong body weight
and nothing on the wall could tell anyone. So `read_google_health` refuses with
a named blocker, that flows into the ordinary unavailable path, and the panel
says "unavailable" — the truth. A test asserts no `parse_google_health` /
`parse_weight_datapoint` symbol exists (B7's `parse_gemini` precedent), and a
deliberate defect that stubs a plausible fake reading is killed by it.

**SUPERSEDED 2026-09-09 — the goal is now the weigh-in item's `target`; see
the entry above. The paragraph below describes the design that was replaced and
is kept so the reasoning is not lost.**

**"THE GOAL LIVES IN THE USER'S DEFINITIONS" IS BUILT, AND IT UNCOVERED A REAL
NagLight DEPENDENCY.** B5 recorded this half as explicitly not built. It is now:
`WEIGHT_DEFINITIONS_DIR` names the **directory**, never the number, and the goal
is a top-level `weight_goal_lb:` in a definitions file's frontmatter.
`internal/defs/yaml.go` ignores unknown top-level keys (`default:` branch, "//
ignore unknown top-level keys (forward-compatible)"), so **no NagLight change is
needed to store it there** and it rides the existing Drive sync. There is
deliberately **no `WEIGHT_GOAL` knob**, and a test asserts the negative twice
over: a cycle with every plausible goal-shaped knob set and an empty definitions
tree refuses, and no goal-shaped knob may be **declared** in `.env.example` or
`FieldSchema.psd1` at all.

**THE GAP, AND IT NEEDS NagLight — SUPERSEDED: it was not a gap, it was the
design being wrong for the deployment that exists, and the goal moved onto an
item's `target` instead. See the entry above.** Definitions reach the hub two ways. Folder
mode (`drive.applyFolder`) stages the `.md` bytes verbatim and the key rides
along. **Sheet mode regenerates the `.md` from CSV through `internal/defsheet`,
whose `columns` list is the item field set, and drops unknown columns.** This
household runs sheet mode (`TRACKER_DRIVE_SHEET_ID` set, `FOLDER_ID` blanked
2026-09-08), so on the deployed box the goal would be **erased by the first sync
after someone edits the sheet**. Smallest fix: let `defsheet` carry file-level,
non-item keys through the round trip. That is a NagLight change, a repo this
block may not edit, so it is reported rather than worked around — writing the
goal to a hub knob "for now" would have made the acceptance criterion false
while looking like it passed.

**TWO REFUSALS THAT MUST NOT COLLAPSE INTO ONE.** No **source** posts an
unavailable gauge, because the panel must say "we do not know what you weigh"
rather than leave a hole where a bar belongs. No **goal** posts **nothing**,
because the target line *is* the goal: NagLight refuses a gauge without a
target, and satisfying it would mean inventing one and colouring a real body
weight against a number nobody chose.

**"STALE, NEVER GREEN" IS ONE INVARIANT.** `build_post` says the body carries a
stamp from this cycle **if and only if** this cycle read the source. A failed
source reposts its last real reading at its **original** stamp and goes stale on
NagLight's `static` 7-day horizon; a source that never succeeded posts value 0
with **no `observed_at` at all**, stale on arrival, so the panel says
"unavailable" rather than showing nothing. A failed cycle does **not** write
state, so that stamp cannot decay into permanent freshness. And a **real but
old** reading is still stale and must be — re-stamping it to `now` to "fix" an
unavailable gauge is the easiest mistake here and has its own test.

**THE WIRE SHAPE DIFFERS FROM IF-013 IN THREE WAYS, EACH A 400 OR A SILENT
LIE.** No `min`/`max`: `lb` is the ONE unit NagLight infers a range for
(`unitRangeSpan`, a 50 lb bar around the goal), so the range rule stays in the
tracker — **decided deliberately**, and computing `goal±25` here would put a
second range authority on the producer side. No `window` and therefore no
`direction`, which NagLight refuses without one. And `observed_at` is an RFC3339
**string**, not IF-013's integer — getting that wrong renders as a permanently
stale gauge rather than failing loudly.

**THE CADENCE EXPOSED A VACUOUS ASSERTION, WHICH IS THE FIFTH IN THIS BUILD AND
THE SECOND FOUND HERE.** The usage feeder's timer test asserts the interval is
within a quarter of the tightest horizon; copied across, that is a quarter of
**seven days** — forty-two hours — so it passed on a three-hourly timer and was
enforcing almost nothing. The number had to be re-derived, not the shape copied:
the timer is 15 minutes and the test now also bounds it at one hour, because
what matters is how long the wall can keep showing a number after the source
went away. The second vacuous assertion was the same shape as B7's: the unit
test read `"ProtectHome=read-only" in unit`, which passes on the **comment that
explains the choice**, so setting the real directive to `ProtectHome=no` left the
suite green. Both now match directives at the start of a line.

**Mutation runs: 27 deliberate defects, 26 killed.** The one survivor (M25,
removing the indent check in the goal parser) survives **by construction** —
three redundant guards defend that property and any one alone suffices; M27
removes all three at once and is killed, so the property is covered. Recorded
in the code rather than left as a mystery.

**UNRUN:** `stack/run-hermetic-tests.sh` still refuses on this dev PC
(`missing tool(s): zstd rsync`), so the shell-side suites were not exercised.
This block adds no hermetic suite and no apt package (python3 stdlib only,
already in `packages.list`), so no apt export is owed.

**2026-09-09 the AI-usage feeder (B7, SR-021/LLR-005/TC-005/IF-013):** the hub
gains a second plain service - no container, on a 10-minute timer - that reads
how much of each AI subscription has been consumed and posts it to NagLight as
five gauges. It ships **OFF** (`AI_USAGE_ENABLED=false`) and declares no
credential.

**All three sources were verified with one real call BEFORE any parser existed**,
which the build plan made a hard gate rather than a preference, because a parser
written from documentation posts fiction and fiction looks exactly like a healthy
subscription. None of the three was blocked:

* **Codex** - `codex app-server --stdio`, JSON-RPC `initialize` then
  `account/rateLimits/read`. Returned `rateLimits.primary.usedPercent 34`,
  `windowDurationMins 10080` (so the codex gauge is WEEKLY), `resetsAt`
  1789435411, plus a `rateLimitsByLimitId` map. **No credential passes through
  the feeder for this source at all** - the child reads its own
  `~/.codex/auth.json` and we read stdout. Only the compatible `rateLimits` view
  is parsed: the per-limit-id buckets (`codex_bengalfox`,
  `base_model_inference`) are vendor implementation detail and gauges keyed off
  them would appear and vanish as a model is renamed.
* **Claude** - `GET https://api.anthropic.com/api/oauth/usage` with
  `anthropic-beta: oauth-2025-04-20` and a `claude-code/<version>` UA. **200**,
  carrying `five_hour.utilization 79.0` and `seven_day.utilization 87.0` as
  FLOAT percents, plus a `limits[]` array. That array carries the vendor's own
  `severity` words and **the feeder discards them** - NagLight owns severity and
  colour, and a feeder that forwarded "warning" would make the panel's authority
  ambiguous. A test greps a built body for `severity`/`css`/`colour`.
* **OpenCode Go** - `GET https://opencode.ai/zen/go/v1/usage` with the workspace
  bearer key from `auth.json`. **200**, `{"usage":{"rolling":…,"weekly":
  {"percent":58},"monthly":{"percent":89}}}`. **`rolling` is deliberately NOT
  posted**: it names no window length and the endpoint publishes none, and since
  `direction` is refused without a `window` the alternatives were to invent a
  window length or to ship a gauge with the 7-day horizon that stays green for a
  week after the feeder dies. Both are the fabricated reading the feed contract
  was tightened to stop.

**Gemini is out** - deferred as E11, and a test asserts no `parse_gemini` exists,
because a speculative parser is exactly the failure this block was written to
prevent.

**"UNAVAILABLE, NEVER A GREEN GAUGE" IS ONE INVARIANT, NOT A SCATTER OF
CHECKS.** `build_post` says: `observed_at == now` **if and only if** this cycle
actually read the source. Everything else falls out of NagLight's own rule that
`observed_at` is when the value was TRUE - a failed source reposts its last real
reading at its **original** stamp and goes stale on its horizon, and a source
that has never succeeded posts value 0 with **no `observed_at` at all**, which is
stale on arrival so the panel says "unavailable" rather than showing nothing.
Six failure classes (transport error, timeout, 401, garbage body, well-formed
body with no usable bucket, and a bug in one of our own parsers) collapse to that
single path, and each is asserted against NagLight's horizon table rather than
against our intent. **The cadence follows from the horizons:** two gauges are
`weekly`, whose horizon is 24 h, so the timer is 10 minutes and a test reads the
shipped timer file and fails if that is ever loosened past a quarter of it.

**"Never writes a vendor credential file" is an ALLOW-LIST, not a deny-list.**
`open_for_write` is the only writing door in the module and admits exactly one
path plus its `.tmp` sibling, because a list of known credential filenames
cannot survive the next CLI version putting its token somewhere new. Backed by
three independent lines: `read_secret_file` has no write mode (a test greps it),
the unit mounts that home `ProtectHome=read-only` (not `yes` - it must still
READ it), and a test runs a whole cycle inside a throwaway HOME holding all
three real-shaped credential files and fails if **any byte under it changed**,
if more than one file was written, or if the written file contains a token.

**It refuses to guess, in the manner of `TRACKER_DRIVE_USER`.** `AI_USAGE_USER`
ships blank and blank is a **refusal** - `setup-ai-usage.sh` will not install and
`resolve_identity` will not run, both saying why. No "the only user" fallback: a
single-user box is a two-user box after one oauth2-proxy login, and a usage gauge
on the wrong person's board is a lie about their own subscription.
`AI_USAGE_FEED_URL` refuses in the same spirit - loopback or an address a local
docker bridge is **actually carrying**, never a LAN or public host and never the
https route through oauth2-proxy, which would overwrite `X-Forwarded-User`. That
is `resolve_bind`'s SR-019 lesson reused: `172.16.0.0/12` contains real household
LANs, so membership of the range proves nothing.

**AND THE END-TO-END SMOKE FOUND ANOTHER, WHICH IS WHY IT WAS RUN.** With all
three parsers green against captured bodies, one cycle was run against the three
LIVE sources with the POST recorded rather than sent (no hub state touched).
OpenCode answered **403** where the verification call had answered 200: the
verification sent a named `User-Agent` and the first cut of `read_opencode` sent
none, so urllib's default `Python-urllib/3.x` went out. **The feeder behaved
exactly as specified while broken** - two "unavailable" gauges and a named
failure line, never a green one - which is the only reason the fault was visible
rather than silent. Fixed, and three more mutations (M26-M28: drop the agent,
replace the `claude-code/<version>` agent, drop the `anthropic-beta` header) all
turn the suite red. The re-run posts **all five gauges live**: codex 34%
(weekly), Claude session and weekly 87% (daily/weekly), OpenCode 58% weekly and
89% monthly, each with `min:0 max:100 target:0 direction:"up"` and no colour
field anywhere.

**MUTATION-TESTED, AND IT FOUND ONE.** 25 deliberate defects were run against
`tests/test_ai_usage_feeder.py`; 24 turned it red immediately and **M15 did
not**. M15 made a failing source discard every reading collected so far, and the
test passed only because it read the failing source FIRST - there was nothing
collected yet to discard. The test now runs BOTH orders and M15 is red. That is
the fourth block in this build to find a vacuous assertion, and the first cut of
this one would have shipped it.

**Assumption recorded for the next gate (AGENTS.md "Ask, don't assume").** The
plan says "profile-gated". There is no compose profile to join, because this is
not a container - so `AI_USAGE_ENABLED` **is** the gate, the same shape as
`AI_CLI_ENABLED` and `REMOTE_UI_ENABLED`, and only a literal `true` enables it.

**Not done, and named rather than buried:** `stack/run-hermetic-tests.sh`
REFUSES on this dev PC (`missing tool(s): zstd rsync`), so the shell carriage
(`setup-ai-usage.sh`, the firstboot hook) has been syntax-checked with `bash -n`
and reviewed, but never executed. No live hub or panel state changed. No apt
package added - python3 stdlib only, and python3 is already in `packages.list` -
so no apt export is owed.

**2026-09-09 occupancy power in the wall image (B9, SR-020/LLR-003/TC-003/IF-012):**
the panel's backlight and its suspend/RTC path now come out of **one** evaluation
of **one** knob set (`SLEEP_MODE`, `SLEEP_START`, `SLEEP_END`,
`WALL_ABSENCE_ENABLED`, `WALL_ABSENCE_TIMEOUT_MIN`, `WALL_PRESENCE_FILE`).
`wall-occupancy.py decide()` is a pure function returning both halves in one
record; `wall-sleep.sh occupancy` applies that record and re-derives neither, so
the two behaviours cannot drift into a dark screen on an awake machine or a
suspend with somebody at the panel. **`SLEEP_END` is now 06:45** (ratified
2026-09-08, replacing 06:30; one default on both power paths by Owner ruling
2026-09-09) and is one value doing three jobs: the RTC wake target, the morning
wake timer, and the start of the on-period.
**Off by default** — with `WALL_ABSENCE_ENABLED=false` nothing is read and
nothing is written, and the 22:00 `SLEEP_START` schedule stands unchanged. On:
backlight off whenever nobody is present; suspend only after an hour of absence
**outside** the on-period, arming 06:45; **never** a suspend inside the
on-period, so a walk-in is a backlight write and not a resume. With detection on,
22:00 stops being a suspend and hands over to the absence timer instead of
sleeping on somebody standing there. Presence arrives as a file (IF-012) written
by the panel shell, not from the sensor socket — that socket checks SO_PEERCRED
against the panel uid and refuses root by design, so the app senses and the image
powers. **The reader fails safe in one direction only:** missing, unreadable,
malformed, wrong-version, stale or future-stamped all read as PRESENT, so an
image whose shell does not yet write the file is inert rather than dangerous.
**SN-013 still holds and is asserted, not assumed:** an `rtcwake` that cannot arm
refuses the suspend and degrades to backlight-off on *both* paths (one guard,
`suspend_now`), and a mains blip loses the tmpfs absence clock so the panel comes
back lit, awake and reachable and must serve a full timeout again. The tests
assert the **artifacts** rather than a green timer — the brightness file really
containing 0, the epoch actually handed to `rtcwake` converting back to 06:45,
a recorded `systemctl suspend`. No live panel or hub state changed; no apt
package added, so no apt export is owed.

**2026-09-09 the AI CLI service, after a security cross-review REJECTED it
(B12 fix round, SR-019/LLR-002/LLR-004/TC-002/TC-004/IF-011):** the hub gains a
plain service - no container - that answers `POST /v1/ask` from on-box callers
by running one headless CLI session on the household subscription: messages in,
model and depth from a registry row rather than the request, the answer
constrained by a supplied schema. It ships **OFF** (`AI_CLI_ENABLED=false`) and
declares no credential.

**The block first reported all four of A40's containments as MET. They were
not.** A cross-review by a different model family returned 16 confirmed
findings and the coordinator verified the worst three in source. The four
criteria are unchanged; what changed is that each guard now checks an
**identity** where it used to check that a **string was present**:

* **A dedicated unprivileged account, never `hub`.** The unit hard-coded
  `User=homehub-ai` while every check validated the `AI_CLI_USER` knob, so
  `AI_CLI_USER=alice` had setup create and certify `alice` while systemd still
  started `homehub-ai`. Now `setup-ai-cli.sh` **generates** the unit's
  `User=`/`Group=` drop-in from that same knob, and the service asserts its own
  **effective identity** at startup and refuses a mismatch. The sudoers search
  also follows `#include`/`#includedir`/`@include` and refuses to certify an
  include it could not read - the old parser treated `#include` as a comment,
  so a NOPASSWD grant one file away was invisible.
* **Read-only tool use, no permission-skipping flag.** The old guard never
  pinned the executable, so a registry row of `python3 -c '...'` followed by
  contained-looking flags passed everything and ran arbitrary code as the
  service account. The registry is editable on the box, which is the whole
  reason this is a runtime guard, so this was the ballgame. Now argv[0] is
  **pinned per family**, resolved only on `AI_CLI_BIN_PATH` (and refused if the
  binary is group/other-writable), and **every token must be in that
  executable's declared vocabulary** with a value this repo constrains -
  duplicates refused (the old check read only the first occurrence, and the CLI
  takes the last), `--allowedTools` values actually inspected, and the `Env=`
  cell an allow-list so a row cannot set `PATH`/`HOME`/`CODEX_HOME` and move
  the ground the pin stands on.
* **Loopback or the docker bridge, never the LAN.** "The docker bridge" was the
  whole of `172.16.0.0/12` - which contains real household LANs, so a home on
  `172.20.0.0/16` was accepted as a bridge. Now a non-loopback bind must be an
  address a local `docker0`/`br-*` interface **is actually carrying**; on a box
  with no bridge, only loopback binds. `setup-ai-cli.sh` no longer carries its
  own copy of the rule - it calls the service's `--bind-check`.
* **A scratch working directory per request.** Unchanged in behaviour, but a
  failed cleanup is now reported and raised instead of swallowed by
  `ignore_errors=True`, so "removed on every exit path" is observable.

**Also bounded, because this is a small always-on box** (new LLR-004/TC-004):
one lock now takes the availability decision, the launch claim and the cooldown
together, so a burst cannot launch N sessions in the gap the old code left
between `available()` and `cool()`; successful calls pace the row too (they
never cooled at all before); at most `AI_CLI_MAX_CONCURRENT` sessions run at
once; a body with no `Content-Length` or above `AI_CLI_MAX_BODY_BYTES` is
refused before it is read; connections are capped and idle ones time out; a
timed-out session is killed as a **process group** so no grandchild survives;
`::1` is now actually servable (it was accepted and then failed to bind); codex
is actually handed the caller's schema (the file was written and never passed);
and a zero exit with a non-result frame is a 502, never a 200 carrying `null`.

**The test standard is the other half of the fix.** Three shipped checks were
vacuous - shell A10 greped `StateDirectory=` out of the unit and would have
passed with `request_scratch` deleted; A9 never ran under the unit's `User=`;
the account checks were artifact greps. They are rewritten to observe
behaviour, and **every guard was mutation-checked**: broken deliberately,
confirmed red, restored, confirmed green (24 of 24 bite; the evidence is in the
audit entry below). No live hub state changed; no apt package added, so no apt
export is owed.

**2026-09-07 unified file-share/backup feed requirement:** Owner retired the
separate visible library-drive, backup-drive and backup-run model. New
SN-014/SR-018 and repo-local IF-006 consume NagLight IF-010: a missing library
mount or failed representative Samba read is an immediate red override;
otherwise one item ages from the last artifact-verified FileBackup success.
Health recovery cannot advance that success time. The producer, fresh
provisioning, sim fixture, and registered TC-001 hermetic producer/full-wrapper
tests now use the new contract; the
legacy backup-drive unit and fixture definitions are removed. A live deployment
also runs an idempotent stop/remove migration for an already-installed legacy
timer, and the new guard refuses its old backup-target invocation even during a
race. It still needs the explicit tracker-definition inventory inspection/rebaseline
documented in `stack/tracker-seed/README.md`; no deployment or process gate
changed.

**2026-09-06 panel capabilities implementation:** Owner-approved app plan has
opt-in deploy configuration, protected Caddy routing, offline sensor installation,
private host config and camera-off verification changes. Defaults remain disabled;
no live deploy or ISO build occurred. Active gate stays G1. Nine targeted tests,
config validation, Bash parsing and G1 smoke pass; hardware/Linux/Caddy runtime
proof remains open. [Implementation and operator handoff](panel-capabilities-implementation-2026-09-06.md).

The older cold-session block below is historical; HomeHub's current operating
brief supersedes its dated live-box state.

> **RESUMING FROM A COLD SESSION? THIS BLOCK IS THE WHOLE HANDOVER (2026-08-29, evening).**
>
> ### 0. THE ONE THING THAT NEEDS A HUMAN
>
> **IceDrive is STOPPED and will not start on its own, deliberately.** The
> FileBackup permutation drill clears `/srv/library` (its own header says "do not
> point it at anything you care about"), and it took two things with it: the
> local half of IceDrive sync pair 63605 — `/srv/library/permtest`, which is in no
> backup set, so the CLOUD SIDE IS THE ONLY COPY — and `/srv/library/.homehub-library`,
> the marker the gate now requires.
>
> The gate refusing is the guard working. A two-way pair whose local path does
> not exist may propagate a mass delete, and which way IceDrive jumps is
> unmeasured. Decide about the pair, then:
>
>     sudo touch /srv/library/.homehub-library   # bless the volume
>     sudo systemctl restart homehub-desktop-session
>
> Everything else on the box is up. The library tree was recreated after the
> drill; `verify-hub.sh` reports **16 of 16 backup sources reachable**. The only
> other red is **C21** (`finance-auditor` restart-looping on an unset
> `ACTUAL_SYNC_ID`), which is unchanged and needs one browser visit.
>
> ### 1. WHAT LANDED, AND WHY IT IS SAFE TO BUILD ON
>
> Four Owner rulings, built and exercised:
>
> | | |
> |---|---|
> | **Q1** | A `--plan` run writes `plan_<ts>/`, not `run_<ts>/`, with its own retention budget (`BACKUP_PLAN_KEEP`) paid by PLAN runs — hanging it off the nightly would bound the litter by the nightly's health |
> | **Q2** | `uptimekuma_data` is a backup set, quiesced like `actual` |
> | **Q3** | `--dry-run` is gone. The `dry_run=1` LOG token in `restore.sh` stays: that is not the flag, it is what is written on the drive |
> | **Q4** | The changed-host-key path has nine Pester cases, lifted out of the shipped launcher by AST |
>
> **The reimage restore covers five sets, not one** — `caddy`, `tracker`,
> `actual`, `uptimekuma` and the encrypted IceDrive profile. The loop lives in
> `provision/restore-volumes.sh` rather than inline in firstboot, because inline
> it could only be tested by reimaging a box, and a restore path that has never
> executed is exactly how C22 happened.
>
> **A third guard watches the tracker's definitions from OUTSIDE the tracker.**
> `/api/today` was measured returning `{"items":null}` while the panel rendered
> GREEN with score 0. The tracker cannot raise that alarm — the item that would
> carry it is deleted by the same `rm` — so the guard reads the files off the
> volume and writes a verdict to a state file `verify-hub.sh` asserts on.
>
> ### 2. THE TESTS, WHICH ARE THE POINT
>
>     158 hermetic assertions   stack/run-hermetic-tests.sh   green in WSL AND on the hub
>     160 Pester tests          scripts/lab/tests/            green
>      58 PASS / 0 FAIL / 2 SKIP   FileBackup permutation drill, production wrapper
>      19 PASS / 0 FAIL         reimage-recovery-drill.sh — the archives really do reconstruct
>
> The hermetic suites need bash, tar, gzip, zstd, rsync, sha256sum, gpg and awk —
> **no docker, no VM, no drive**. That is deliberate: every other suite here is
> expensive, which is why the cheap properties went unasserted for months.
> `tests/test_hermetic_shell_suites.py` drives them from pytest so CI gates on
> them, and `scripts/check.py` now has a `unit-tests` step — **it never ran pytest
> at all before**, so `tests/` had been in the tree unexecuted by any gate.
>
> ### 3. WHAT THE ADVERSARIAL REVIEW FOUND
>
> Four passes through `codex exec -m gpt-5.6-sol` (medium), files inlined rather
> than explored. **Twenty-six findings, twenty-four real.** The three worst were
> a tar member-NAME validation that ignored link targets in a restore running as
> root into a home directory; `restore.sh` verifying counts rather than
> membership; and `retention_prune` classifying runs by an unanchored grep over a
> file whose `note` is free text. Writing the tests for those fixes found four
> more, including `exec 9>FILE 2>/dev/null` silencing every later warn and die.
>
> Two findings were REJECTED and are written up in HomeHub's
> `DECISIONS_FOR_REVIEW_2026-08-29.md`: making the drive-identity guard fatal
> would stop IceDrive on the stand-in drives, and closing the gate's unmount race
> needs a systemd mount binding rather than another check.
>
> ### 4. THE BOX IS AHEAD OF ITS IMAGE
>
> Everything above is committed here, so the next image carries it — but it was
> installed by hand on the hub to be tested against reality. The running box and
> the last-built ISO disagree until you rebuild. See HomeHub's
> `REIMAGE_PERSISTENCE_PLAN.md` for the list.
>
> ### 5. THE HABIT, AGAIN
>
> Every defect above was found by RUNNING something. The `PLAN_FLAG_USED`
> reference that killed every plan run, the `DISPLAY=:0` restart that left the
> cloud sync down while logging success, the `pgrep` that matched its own ssh
> command line, the state write that failed on every ordinary run: none was
> visible to a reader, and all four were visible in the first execution.

<details><summary>The previous handover (2026-08-28, afternoon) — kept because C21 and C27 are still open. Its C25 row is corrected in place; everything else in it should be read as "true that afternoon".</summary>

> **RESUMING FROM A COLD SESSION? THIS BLOCK IS THE WHOLE HANDOVER (2026-08-28, afternoon).**
>
> ### 0. THE HUB IS UP, GREEN, AND NOT YET MOVED
>
> `systemctl is-system-running` = **running**, 0 failed units, the stack up. It
> was shut down for relocation earlier in the day and then **booted again** so the
> FileBackup drills could run against the stand-in drives — so the "powered off,
> being moved" note that stood here this morning is spent.
>
> **The two USB stand-ins are attached but DELIBERATELY UNMOUNTED**, cleanly, so
> the Owner can read them on a PC. The drive-health timers are running again, so
> they will report **red** until the drives are remounted or pulled — that is the
> guard doing its job, not a fault. To bring them back:
>
>     sudo mount /srv/library && sudo mount /mnt/backup-drive
>
> ⚠️ **Before it is powered on at its new location, confirm a WIRED ETHERNET
> DROP.** The hub is `eno1`-only, no wifi in netplan. See HomeHub **C2**.
>
> ### 1. THE ONE THING BLOCKING THE ETHERNET DEPLOY (C28)
>
> There is now a fifth entry point that automates the whole Option E path:
>
>     DeployHomeHubImageOverEthernet.cmd          build -> publish -> preflight
>                                                 -> consent -> reboot -> jump
>     DeployHomeHubImageOverEthernet.cmd -PublishOnly    stops before the box
>
> **The ISO is already built (14/14 assertions) and the share already published
> and verified** — 1540 files, four witness hashes matching. What is missing is
> one elevated command the Owner must run once, because it sets a Windows local
> account password:
>
>     $p = & 'C:\Projects\HomeHub\scripts\deploy\Show-DeploySecret.ps1' HubIsoSharePassword
>     Set-LocalUser -Name hubread -Password (ConvertTo-SecureString $p -AsPlainText -Force)
>
> That reads the minted secret and sets the account without displaying it. After
> it, the deploy is unattended and the Ethernet install is one command.
>
> ### 2. WHAT THE TEST WORK FOUND, AND WHAT IS STILL OPEN
>
> The suite went **103 checks -> 167 defined / 149 cited**, with three new areas
> (**R** boot integrity, **S** the graphical layer + IceDrive, **T** the tier-2
> catalogue) and new cases D11, J05, L06, M17, B02. Eight open items came out of
> it, and **none is a regression** — every one is something that had always been
> true and nothing had ever asked:
>
> | Item | What |
> |---|---|
> | **C21** | `finance-auditor` restart-loops — `ACTUAL_SYNC_ID` unset, 0 budget files in Actual. One browser visit |
> | **C22** | Three of five vhosts hold no certificate — Let's Encrypt 429. Clears itself; re-run the suite after **2026-08-29 04:12 UTC** and 11 failures become 3 |
> | **C25** | `backup.sh --dry-run` was **not dry**: it writes a `run_<ts>/` directory to the BACKUP DRIVE, mounts CIFS and issues `hdparm -S`. **Ruled 2026-08-29: the behaviour stays, the NAME goes.** The mode is `--plan`, it names what it wrote at both ends of the run, and `--dry-run` still works. **Both halves are now CLOSED (2026-08-29 evening):** the directory is `plan_<ts>`, with its own `BACKUP_PLAN_KEEP` budget pruned by plan runs, and `--dry-run` was removed once a sweep of both repos found no caller |
> | **C26** | **The household Samba accounts have never existed on any hub.** One regex in `Materialize-Deploy.ps1:890` filters on schema key NAMES, and three of the four are `@identity:` tokens that do not match. Five `Private` shares are served to nobody |
> | **C29** | The drive-health guard could not report: `/api/feed` answered **400** because the tracker declared no item for the check ids. Not a transport fault — `docker exec tracker wget …/healthz` returned `ok`. Four item definitions added on the box; both lanes now report. The guard prints the server's own explanation instead of guessing |
> | **C32** | The `caddy_data` ACME restore had **never fired**: `provision-mounts` writes the backup drive's fstab line seven minutes after the step that reads it. It now mounts the drive itself, READ-ONLY, from the generated fragment. Proven — 21 files, ACME keys and certs |
> | **C27** | The **Library** stand-in dropped off the bus 5 times on port `1-8`; moving it to `1-3` gave a clean 8 MB drill run with zero disconnects. Evidence, not yet a verdict |
> | **C28** | The share credential — now in the schema and minted; the elevated step above is what remains |
>
> ### 3. WHAT CHANGED THAT ONLY A REIMAGE WILL DELIVER
>
> `packages.list` gained three, and **the running hub does not have them yet**:
>
> - **`wakeonlan`** — measured, load-bearing. bash `/dev/udp` broadcast is refused
>   by this kernel (EACCES), and with `wakeonlan`/`etherwake` both absent
>   `wol_send` puts **nothing on the wire**. The Mini-serv ingest could never have
>   worked while Mini-serv was asleep.
> - **`exfatprogs`** — the backup drive asks for a fsck the box could not run.
> - **`smartmontools`** — nothing could read a drive's health.
>
> `library-guard.sh --report` also now **reattaches a dropped drive** (fstab entry
> only, never in `--check`, re-assesses afterwards, inhibited by
> `/run/homehub-no-remount`). All of it lands on the next install.
>
> ### 4. FILEBACKUP IS SOUND — ALL THREE DRILLS PASSED
>
>     roundtrip     49 PASS  0 FAIL  exit 0   10 states reconstructed byte-exactly
>     permutation   59 PASS  0 FAIL  exit 0   15 mutation cycles, production wrapper
>     defect regr    6 passed, 2 refused-for-preconditions (never-silent-green working)
>
> Two things were needed to get there and both are written down in
> `BENCH_BRINGUP_HANDOFF.md`: `backup.bench.env` must be recreated after a reimage
> (it is residue). **Superseded 2026-09-08:** the combined-state contract no
> longer uses `LIBRARY_BACKUP_FEED_CHECK`; `NAGLIGHT_FEED_URL` is retained only
> for the independent tracker-definition guard, while FileBackup uses
> `FILE_SHARE_BACKUP_STATE_URL`. A feed-less backup drill must override the new
> state URL without disabling the definitions guard's transport.
>
> ### 5. OPTION E IS NOT A RESCUE PATH
>
> Its own precondition is *"the OS must boot and be reachable"*, so it cannot
> recover the case a reimage is usually wanted for. Two standing caveats: the
> kernel is still `6.8.0-138` (`kexec_file_load` faults cumulatively — **reboot,
> then jump once**, which the new launcher does for you), and the CIFS share is
> hand-made. A verified USB stick exists as the fallback; **it has not been
> booted**, which is the one thing neither path establishes.
>
> ### 6. STATE OF THE MACHINES
>
> - **The real HOMEHUB: UP and green.** Stand-in drives attached, unmounted.
> - **HomeHub-Lab / WallPanel-Lab: absent.**
> - Branch on both repos: **`IceDrive-DesktopDirection`**. Everything is committed
>   locally; **the Owner pushes** (OI-3).
>
> ### 7. THE HABIT THIS WEEK KEEPS REWARDING
>
> **Six documentation-vs-reality drifts in one session, every one found by RUNNING
> something rather than reading it** — the tier-2 catalogue described as off while
> five profiles ran; Samba K04/K05 called "written only" while passing; a ratified
> decision describing a relabel that stopped happening in August; a "never writes"
> contract that wrote to the backup drive on every run; household Samba accounts
> that never existed; and `FieldSchema.psd1`, which has **never** parsed under
> Windows PowerShell 5.1 because it carried non-ASCII with no BOM. If you find
> yourself reasoning about whether something works instead of executing it, that
> is the habit this project keeps punishing.


</details>

- **Active gate:** G1 — Requirements, UX & constraints. This is a config/infra
  repo delivered against a ratified brief (HOMELAB_RESTRUCTURE_PLAN.md); the
  requirement spine is intentionally **high-level** (proportionality doctrine).
- **Round:** 1
- **Open items:**
  - **Needs the Owner** _(ratification / manual steps — dial=HIGH)_:
    - OI-1 — **Google OAuth client** must be created in Google Cloud (needs
      the Owner's account); redirect URI to register:
      `https://tracker.<domain>/oauth2/callback` →
      [stack/.env.example](../stack/.env.example)
    - OI-2 — **Reimage-over-LAN ladder — RESOLVED 2026-08-28, ticked here
      2026-08-30.** This entry said "nothing destructive is implemented until the
      Owner checks one". That is no longer true: Option E (the SSH rung) is BUILT
      AND PROVEN, `remote-reimage/` holds the initrd builder and the kexec script,
      `kexec-tools` is baked into the image — and it has been *used*: the box
      reimaged itself over Ethernet from an SSH command, jump 23:06:46, green
      23:24:07, no stick written. PXE stays off the table.
      → [REMOTE_MANAGEMENT.md](../REMOTE_MANAGEMENT.md) Option E; HomeHub
      open-items §E6
    - OI-3 — **Push** each commit (agents lack the SSH key) →
      this repo
    - OI-5 — **Run the V3 gate** (WI-10.18): enable Hyper-V (elevated,
      machine-level, needs a reboot), create the VM, do the one-time GRUB
      edit (light path), and confirm compose-up →
      [vmtest/README.md](../vmtest/README.md). **The HUB half is DONE** — the
      gate ran 2026-07-31 and again 2026-08-01 (see the audit entries; the
      first one PASSED while running a three-week-stale tracker image).
    - OI-17 — ~~**Boot the WALL ISO**~~ **CLOSED 2026-08-05.** The A19 gate
      booted it and **the shell painted** — the narrow point this item stayed
      open on. See the 2026-08-05 audit entry. Original text follows.

      OI-17 (as written 2026-08-02; ATTEMPTED 2026-08-03):
      the ISO was booted for the first time and got as far as `late-command_9`
      before dying on the light path's missing payload (see that day's entry —
      it was a real defect in BOTH images, now fixed). **Subiquity, the disk
      pin and all 40 packages including the 23 Electron libraries are now
      PROVEN on a real install.** What is still unproven is everything after
      the late-commands: `wall-firstboot`, `getty@tty1`, `cage`, and whether
      the shell paints. A repacked (zero-keypress) wall ISO is built at
      `D:\vmtest-out-wall\wall-repacked.iso` and `Wall-VMTest` exists; the
      re-run needs **one elevated approval** →
      [vmtest/README.md §11](../vmtest/README.md).
    - OI-19 — **The hub's site-staging late-command had the same `/cdrom`
      bug — FIXED 2026-08-03, and NEVER INSTALLED FROM.** The defect:
      `stack/autoinstall/user-data`'s site step read
      `/cdrom/deploy-payload/site`, fell back to `/media/...`, and then
      `[ -d "$S" ] || exit 0` — a **silent success**. On the LIGHT path neither
      exists (that is the defect fixed for the payload copy the same day), so a
      PRODUCTION hub would install **none** of its six real files: `.env`,
      `backup.env`, `cifs.creds`, `samba-users.creds`, `smb.conf.fragment`,
      `library-mounts.fstab`. Severity, precisely: `.env` is the only one with a
      downstream check, and `firstboot.sh` step 1 only **WARNs** on
      `REPLACE_WITH` before bringing the stack up anyway — on a headless box
      that warning is in the journal and nowhere else. The other five have no
      check at all, so the drives would not mount and Samba/backup would be
      unconfigured; the box would come up looking exactly like a SIM build
      while believing it is production. **Exposure, stated exactly:**
      `Build-VentoyStick.ps1` — the only wired production path — calls
      `build-repacked-iso.sh`, where `/cdrom` IS the combined ISO and does carry
      `/deploy-payload`, so no stick ever built has been bitten. What was one
      command away from it is the equally supported
      `SITE_DIR=… bash vmtest/build-seed.sh`.
      **The fix (see the audit entry):** the step no longer searches at all — it
      reads `/target/opt/homehub/site`, the payload late-command 3 has already
      copied, so there stays exactly ONE discovery mechanism (the `/cdrom`,
      `/media`, `/media/*`, `/run/media/*`, then CIDATA-by-label search) and it
      is the one that was fixed and exercised. Same shape as the wall image's
      late-command 4a. It also **refuses** now: the tracked user-data carries
      `BUILD_PROFILE=production` and `render_seed_tree` rewrites it to `sim` for
      a vmtest image, so a production image whose payload lost `site/` fails the
      install loudly instead of exiting 0.
      **A SECOND HEAD, found by adversarial review 2026-08-03 and fixed the
      same day:** the refusal only ever asked whether `site/` was a DIRECTORY.
      A payload whose `site/` existed but held nothing that mattered — the
      builder counted the mandatory `user-data.filled` as a staged site file,
      so a `SITE_DIR` containing only that built a clean production ISO —
      walked past it, logged six MISSING lines nobody reads on a headless box,
      touched `.site-present` so `firstboot.sh`'s own "broken production stick"
      branch stayed quiet, and exited 0. Byte for byte the outcome above, by a
      different road. Measured, not reasoned: the build succeeded and 4b
      returned 0. Now the FILES are checked, in both places — the builder
      refuses to stage, and a missing `required` file makes 4b exit 1.
      **PROVEN — behavioural, still never installed:** `vmtest/test-hub-seed.sh`
      (**28 checks, 0 failed, 0 skipped**, as root in WSL2) builds real sim and
      production seeds and then RUNS the late-commands extracted from the
      user-data each build produced — step 3 (find and copy the payload)
      followed by 4b, against a real payload under a real `/media` entry, a
      real loop-mounted CIDATA seed ISO, and a forced `cp` failure. Until
      2026-08-03 the case advertised as "the light path" hand-created
      `/target/opt/homehub/site` and never ran step 3 at all.
      **NOT PROVEN: nothing has been installed from any of it.** Subiquity has
      never run these late-commands, `curtin` has never placed `/target` for
      them, and no ISO was built or booted here. OI-19 is FIXED-BUT-UNVERIFIED,
      not closed; it closes on a hub install.
      **NEEDS THE OWNER (one ruling now, one decision to ratify):**
      (a) **DECIDED IN THE REFUSE DIRECTION, 2026-08-03, pending ratification:**
      a production build whose `site/` arrives but is missing a `required` file
      now HALTS the install (it was LOUD-BUT-NOT-FATAL). Same call the
      missing-directory branch and the wall image already make. The lever to
      reverse it is moving a file from `required` to `optional` in 4b's own
      table. (b) `drive-identity.conf` is now installed with the other six: the
      builder and `Build-VentoyStick.ps1` both stage it and **nothing ever
      installed it**, so the drive-identity file has never reached a box. Both
      are recorded as **A10** in the Assumptions log below.
    - OI-20 — **`config.json` now has a path onto the hub — and its MODE needs
      a ruling** (2026-08-03, new): Personal's `Materialize-Deploy.ps1` emits
      `out\homehub\config.json` (the kiosk shell's runtime config: `FEED_TOKEN`,
      a Kuma push token, the Subsonic password). It is a **hub** artifact — the
      shell fetches `./config.json` relative to its own origin, and that origin
      is Caddy's kiosk site, document root `/opt/homehub/stack/wall-shell/`.
      This repo now stages it (`render_seed_tree`) and installs it
      (`firstboot.sh` step 3e, **after** step 3d's untar). **The ruling owed:**
      it is installed **0600 root:root**, the most restrictive mode that
      actually serves — measured, not assumed: the pinned `caddy:2.11.4-alpine`
      declares no `USER`, `docker-compose.yml` sets no `user:`, so the container
      is uid 0 and reads the read-only bind mount as root. It was NOT widened to
      0644. What the Owner should rule on is the **posture**, not the digits: a
      file carrying live credentials is now **served over HTTP** — behind the
      kiosk site's `remote_ip {$PANEL_IP}/32` matcher, so not open to the LAN,
      but that is an allow-list, not a secret store.
      **The silent-degradation half is now closed (2026-08-03, adversarial
      review):** nothing verified that caddy could actually READ the file. Its
      healthcheck probes the admin API, which is up whenever the process is, so
      a `user:`, a `USER` in a newer image or daemon userns-remap would leave
      caddy `healthy` while the site 403/404s and `loadConfig` — which never
      throws — falls back to defaults. (`:ro`/`read_only:` are NOT the risk:
      they restrict writes, and this is a read.) `firstboot.sh` **step 4b** now
      asserts the read **as the container** (`docker exec` inherits the
      service's user) whenever the production source exists, and a failure makes
      the whole unit exit non-zero so `systemctl status homehub-firstboot` is
      RED. **UNPROVEN: step 4b has never run** — no hub, no compose bring-up.
      **The stick half is now guarded from this side too:** a production build
      with no `site/config.json` is REFUSED by `render_seed_tree`. Personal's
      `Build-VentoyStick.ps1` still assembles `out\site\` from a hardcoded
      `$wanted` list; the two halves are deliberately independent — supplying
      the file and refusing to build without it are different failures with
      different owners. **Consequence to expect:** until Personal's half lands,
      a `Build-VentoyStick.ps1` run whose `out\` tree has no `config.json` now
      FAILS instead of quietly producing a stick without one.
    - OI-21 — ~~**`/opt/homehub` installed WORLD-WRITABLE**~~ **CLOSED
      2026-08-05:** measured on two freshly installed boxes — 0 group- or
      world-writable paths under `/opt/homehub` (was 230) and `/opt/wall-panel`
      (was 215), `stack/.env` `0600 root:root`. Original text follows.

      OI-21 (as written) — **`/opt/homehub` installed WORLD-WRITABLE — every hub ISO ever
      built here. FIXED 2026-08-04, NOT YET REBUILT.** Found by BOOTING the hub
      gate VM, not by review: `drwxrwxrwx root:root /opt/homehub` (and `stack/`,
      `images/`), `-rwxrwxrwx stack/.env`, `-rwxrwxrwx docker-compose.yml`, 230
      world-writable paths, with `sudo -u nobody` able to READ every credential
      in `.env` and WRITE the compose file root brings up on the next boot — a
      straightforward local privilege escalation. The chain was three honest
      links: DrvFs reports 0777 for everything on an NTFS mount, `-rock` records
      that faithfully, `cp -a` copies it faithfully; **nobody ever decided the
      mode.** Now decided in the builder (`normalize_payload_modes` +
      `assert_payload_modes`, which FAILS the build), imposed by the ISO writer
      when the staging filesystem cannot hold it, and asserted again at install
      time (hub late-command 3b, wall 3a). **What the Owner owes:** nothing to
      rule — but **no ISO has been rebuilt**, so the fix is proven on staged
      trees, on a freshly-written seed ISO and on executed late-commands, and
      **not** on an installed box. The next hub/wall install is what closes it.
      Same family as OI-19 and the gitignored-payload leak: a latent defect in
      every artifact, invisible to every static check, surfaced by an install.
      **2026-08-04 (later): BOTH ISOs HAVE NOW BEEN REBUILT** from the fixed
      builder, into `D:\vmtest-out-hub-a19` and `D:\vmtest-out-wall-a19`, and
      `assert_iso_payload_modes` passed on both artifacts. Still not an
      installed box — that is the A19 run (OI-22).
    - OI-22 — ~~**RUN THE A19 TWO-VM GATE**~~ **CLOSED 2026-08-05 — PASSED,
      UNAIDED.** Every §3 "Done when" row met with no hand-patching: the panel
      painted red with both drive lanes named, the per-ITEM colours agreed, and
      all three ratified guards held (403 to a non-panel, a forged
      `X-Forwarded-User` replaced — including when sent twice — and `WALL_PORT`
      bound to the LAN leg only). Capture:
      `D:\vmtest-out-wall-a19\panel-a19-GATE.png`. **It closes OI-17 and OI-21
      as well — but NOT OI-19, OI-20 or the hostname fix, which need a
      PRODUCTION build and are therefore blocked on C17.** See the 2026-08-05
      audit entry, which corrects an earlier claim that one install would close
      all five. The original text follows.

      OI-22 (as written 2026-08-04). Everything it needs is
      now built and no decision is outstanding; it needs elevation and about
      three hours of wall-clock, which is why it is the Owner's. Three
      commands, in order, from an elevated shell in the MiniPC-Deployer
      checkout:
      `.\vmtest\Start-A19Gate.ps1 -Stage Lab`, then `-Stage Hub -Force -Watch`,
      then — only once the hub answers — `-Stage Panel -Force -Watch`. The two
      installs must not overlap (two VMs on one host and one disk turned a
      45-60 minute wall install into ~2h10m on 2026-08-04). See
      [vmtest/README.md](../vmtest/README.md) §12 for the addressing plan and
      the three sim deltas it introduces, and Personal's
      `WALL_PANEL_BRINGUP_PLAN.md` §3 for what to assert once both are up.
      **This single run is what closes OI-17 (the shell has never painted),
      OI-19, OI-20, OI-21 and the hostname fix — all five are code-complete and
      INSTALL-UNVERIFIED, and an install is the only thing that can speak to
      any of them.**
    - OI-18 — **RULED (b) 2026-08-03, BUILT THE SAME DAY, and HARDENED 2026-08-04
      after an adversarial review refuted its headline claim;
      INSTALL-UNVERIFIED — NOTHING HAS EVER BEEN MOUNTED.** The panel has **two**
      media sources on **two** hosts (storage-map §3 rows 1-2, §3b, §4d) and one
      credential cannot authenticate on both, so no set of values made a
      production panel work. The Owner ruled **exit (b) — two UNC/credential
      pairs** (exit (a), consolidating behind one host, would have re-opened
      HOMELAB_TOPOLOGY.md decision 2). What now exists:
      - `wall-sync.sh` carries a **flow map** (`flow_spec()`) with two flows:
        music from HOMEHUB via `MEDIA_MUSIC_SHARE_UNC` + the `Music` subdir
        **under** the mount, frame video from Mini-serv via
        `MEDIA_FRAME_SHARE_UNC` at the **share root**.
      - **THE KNOB-CONTAINMENT PROPERTY IS NOW A MECHANISM, NOT A COMMENT.** The
        2026-08-04 review found FIVE ways round it: `wall.env` exported every
        identifier it contained (so `FLOWS=music` dropped the frame flow),
        `MEDIA_CIFS_EXTRA` could re-point the subtree/host/credential, a
        symlinked cache leaf redirected `rsync --delete`, the bench override
        could copy both credential files into the kiosk-readable cache, and a
        frame UNC with a path tail could widen the mirror. There is now an
        eight-key **allowlist** with the internals `readonly` after the load, a
        validated SMB-dialect **enum**, symlink refusal on the destination,
        `//host/share`-only UNCs, and the bench hook as a **command-line mode**
        (`--bench-source`, refused under systemd, fixtures under
        `/var/lib/wall-sync/bench`).
      - **Two failure policies, and a probe with THREE answers.** Music: a
        refused mount FAILS the unit. Frame: 445 is probed first, and the script
        now distinguishes **OFFLINE** (nothing answered at all, or no route —
        the designed "asleep" state, skipped calmly) from **INDETERMINATE** (name
        will not resolve, this panel has no default route, or the box answered
        with a REFUSAL — skipped too, but reported as unknown, never as asleep).
        A box that *answers* and then refuses the mount is **fatal**. No magic
        packet is ever sent for this flow.
      - **Two cadences, two units.** `wall-sync.service` keeps boot + resume +
        on-demand and syncs BOTH flows, with a **per-flow time budget** so a
        wedged music mount can no longer eat the unit's timeout and leave the
        frame flow unrun; `wall-sync-frame.{service,timer}` runs `--only frame`
        **every minute** (`OnUnitInactiveSec`, so a slow run cannot re-fire on
        top of itself). Per-flow `flock`, held through the manifest and the
        stamp.
      - **Two credential files**: `/etc/wall-panel/cifs-{music,frame}.creds`,
        `0600 root:root` — now **enforced**, not merely claimed — installed by
        wall late-command 4a, which also **deletes the payload copies** (they
        were builder-owned, and the ordinary builder uid is the panel user's).
        The inline `MEDIA_CIFS_USER`/`_PASS` fallback is **retired**.
      - **THE `share` USERNAME COLLISION IS GUARDED, NOT JUST OBEYED.** RULED
        2026-08-04: the HOMEHUB music account is ALSO called `share` — two
        accounts, two hosts, two passwords. `wall-sync.sh` refuses two UNCs on
        one host and two flows sharing one credentials file, and no wording
        anywhere says "the `share` account" without naming its host.
      - A **staleness ladder** that measures and reports (a stamp at the cache
        root, `WALL_FRAME_STALE_WARN_HOURS`, default 24) but deliberately
        **never escalates a skip into a failed unit** (ruled 2026-08-04:
        reporting-only, and never to the tracker).
      - Personal's half: store key `PanelMusicCifsCredential`, two UNCs in
        `config.wall.psd1`, and `Materialize-Deploy.ps1 -Image wall` emitting
        **four files for the first time in the project's life**.
      - **Coverage: `vmtest/test-wall-builder.sh` is 26 cases -> 85**, runs from
        an isolated sandbox checkout (it used to rewrite tracked files), and has
        a static gate that never skips. Proven: with `wall-sync.sh` present but
        unparseable, the previous suite exited **0**; this one exits 1. All 22
        stubs of the new guards turned exactly their intended case(s) red.
      **STILL FOR THE OWNER:** (a) the HOMEHUB music account is ruled to be
      `share`, but its §2 status — a real §2 identity needing a §2 row and a
      `Samba<Name>Password` key, or a panel-only account made by hand at the box
      — is **unruled**; (b) **is Mini-serv's NIC set to wake on a magic packet
      only, or on any traffic pattern?** The code sends no magic packet, but a
      wake-on-any-pattern adapter could be woken by the every-minute probe, which
      §4d does not want; (c) ratify the new same-host refusal (it is stricter
      than OI-18 said in words). **Nothing here has been mounted** — "emits
      successfully" is not "mounts successfully", and the reachability tests use
      `127.0.0.1`/`192.0.2.1`, which prove the decision, not the mount.
    - OI-7 — **Tier-2 catalog ratifications (2026-07-10):** ~~(a) confirm the
      tier-2-NOT-baked ISO boundary (profiles excluded from the payload unless
      `EXTRA_PROFILES` at export) as the standing Q10.9 B+ interpretation;~~
      **(a) is CLOSED, and answered the other way, 2026-08-07.** Running it
      settled it: that interpretation is incompatible with Q10.9 B+ itself. A
      hub whose `.env` enabled five profiles shipped with 9 of the 15 images it
      needed, so first boot went to the registry — and because `compose up -d`
      is all-or-nothing, one unreachable **optional** image took the whole core
      stack down with it. `export-images.sh` now derives the bake set from
      `COMPOSE_PROFILES` in the `.env` it is bundling: what is enabled is baked,
      and an unresolvable tag is a refused build. The boundary is the profile
      switch, not the bake. (b) decide where `MEDIA_ROOT` physically lives (must
      NOT be the WI-10.10 backup drives); (c) the oauth2-proxy **security pin
      bump** v7.6.0→v7.15.2 needs a V1 sim re-run + re-export before any real
      flash → [stack/README.md §9](../stack/README.md)
    - OI-10 — **Disk encryption decision (2026-07-11):** the AWOW's disk is
      UNENCRYPTED — physical theft/disposal exposes `.env` secrets + the
      finance volumes. Proposed: autoinstall LUKS (Subiquity supports
      `storage: layout: {name: lvm, password: …}`) + unattended unlock via
      TPM2 enroll at first boot. **THE BIOS QUESTION IS ANSWERED — 2026-07-26:
      Intel PTT is PRESENT AND ENABLED** (vendor INTC, firmware 402.0), so
      LUKS+TPM2 is on the table and this entry should stop asking. Still
      unimplemented: the disk is plain LVM, no `crypt` device anywhere. Ratified
      in principle; tracked as HomeHub open-items §E5. The fallback if PTT had
      been absent was dropbear-initramfs (SSH unlock over LAN — fits
      headless); it is not needed. Touches the autoinstall `storage:` layout = the
      REMOTE_MANAGEMENT **hard line**: not implemented until the Owner ratifies.
      Companion decision: LUKS on the backup drives too (finance snapshots
      land there in plaintext otherwise).
    - OI-8 — **Backup gap — RESOLVED in-repo 2026-07-10 (SN-010/SR-013,
      Owner-ratified single-table design):** `BACKUP_SOURCES` now accepts
      `volume:VOL[@CONTAINER]` and `path:/dir` specs; sim-proven
      (`run-volume-sim.sh` a–d GREEN + full `run-backup-sim.sh` regression
      GREEN). **Remaining for the Owner:** uncomment the volume lines in the real
      `/etc/homehub-backup/backup.env` (+ add `actual tracker` to `OFFSITE_SETS`)
      when configuring the box — they ship commented in `backup.env.example`.
    - OI-11 — **Offsite leg — the Owner CORRECTED the model on 2026-07-29: the
      backup service has NO offsite step in the target state.** The IceDrive
      client (SN-012/SR-015 opt-in desktop session) is pointed **directly at
      chosen library paths in its own GUI** and syncs them itself; the service
      stages, copies and prunes nothing for it. `OFFSITE_ENABLED=false` is now
      the documented target state (`backup.env.example`), the step-5 code is
      kept working as legacy, and the pipeline instead **ingests** network
      shares into the library so there is one current copy for the client to
      sync (see the 2026-07-29 ingest entry). **Mini-serv leaves the offsite
      path entirely** (unchanged from the 2026-07-25 ratification).
      **What this makes MOOT:** the "local-path target" build half done earlier
      on 2026-07-29 (`OFFSITE_PATH`) — it stays in the tree as harmless legacy,
      needs no sim leg, and there is no `OFFSITE_PATH` for the Owner to set;
      likewise the old OI-11(a) "a local-target sim leg is owed".
      **Still outstanding for the Owner:** stand the IceDrive client up on-box
      per `stack/remote-ui/README.md` and create its sync pairs against the
      library paths named in `Personal\deploy\storage-map.md` §4e — never one
      holding raw finance data. **Two consequences to accept:** the client is a
      GUI app, so **sync is down after every reboot until a session is opened**
      (SR-015), and with no offsite step the backup's NagLight report **cannot
      see** a stale cloud copy at all — a green backup says nothing about
      IceDrive.
    - OI-14 — **Ingest + exclusions need the Owner's real values
      (2026-07-29, OWNER):** the ratified INGEST step is inert until
      `INGEST_SOURCES` in the real `/etc/homehub-backup/backup.env` names the real
      share and the real library destination, and the `BACKUP_SOURCES` `path:`
      entry points at that library folder — the repo ships the fictional
      `mini-serv` / `/srv/library/NonDocs/MiniServ` placeholders only (SN-007).
      The **authoritative** library paths come from
      `Personal\deploy\storage-map.md`, not from the example. Same for
      exclusions: `BACKUP_EXCLUDE="*.bak"` ships as the Owner's stated default,
      but the **named very-large folders** he wants skipped are per-set
      (`name.exclude=` lines) and only he knows their names. Note the mirror
      contract before filling it in: `rsync -a --delete` means a file deleted on
      the share is deleted from the library copy on the next run (history lives
      in the dated run snapshots, `BACKUP_KEEP`).
    - OI-13 — **Wake-on-LAN needs the real values + the Windows-side
      settings (2026-07-29, OWNER):** the backup now wakes the sleeping game box
      before pulling and fails the run loudly on a wake timeout, but it is OFF
      until `BACKUP_WAKE_MAC` is filled in (with `BACKUP_WAKE_HOST` /
      `BACKUP_WAKE_TIMEOUT`) in the real `/etc/homehub-backup/backup.env` — the
      repo ships placeholders only (SN-007). On the **Windows** side: enable
      "Wake on Magic Packet" + "Allow this device to wake the computer" on the
      WIRED adapter and turn **Fast Startup OFF** (hybrid shutdown leaves the
      NIC unable to wake). Worth verifying at the same time whether the AWOW's
      `/dev/udp` broadcast is accepted or whether `apt-get install wakeonlan` is
      needed for the fallback — bash cannot set `SO_BROADCAST`, and the WSL
      kernel used for this session's testing REFUSED it.
    - OI-12 — **RATIFIED 2026-07-29 (the Owner) — the wall-panel image lane is
      GO**, in the belt-and-braces variant he chose: the kiosk auth site on a
      **LAN-bound alternate port the router never forwards, PLUS the `/32`
      allow-list** (not either one alone). Proposed 2026-07-25; brief:
      `Personal\homelab\OFFICEWALL_BOOTSTRAP.md`. **BUILT 2026-07-29** — see the
      audit entry below. The D-W0 split holds: the *image* is now a second target
      in this repo (`stack/autoinstall/wall/`), the panel's *shell app* stays in
      `OfficeWallNaglight` and is consumed as a built artifact (IF-005), so the
      "no product source" constraint is intact. What landed, against the three
      things this item said would:
      (a) the kiosk site — DONE (SR-016), and the forged-header strip + the 403
      default are **sim-proven** (`validate-sim.sh` checks 7-8). The **off-LAN**
      403 from a real WAN vantage remains the documented hardware test;
      (b) the graphical autoinstall variant + its own spine rows — DONE
      (SR-017, SN-013 per OI-12b: lighter gates, the panel is disposable);
      (c) `navidrome` is **no longer a gate** — D-W8's rider has the panel playing
      from its local synced copy, so streaming is optional, and `MEDIA_ROOT`'s
      ratification (2026-07-29) closed OI-7(b) anyway. The Caddyfile ships the
      `/music` proxy commented.
      **Still needing the Owner:** the values (`WALL_HOST`, `PANEL_IP`,
      `PANEL_USER_SUB`, `WIFI_*` — all T1/T3, placeholders only in this repo per
      SN-007), the panel's **DHCP reservation on its hardware MAC**, the
      `EXTRA_SUBDOMAINS` label, registering the wall templates in
      `Personal\homelab\deploy\FieldSchema.psd1` (see the audit entry for the
      exact snippet and two cross-repo consequences), and the whole of
      `stack/autoinstall/wall/WALL-BURN-IN.md`.
    - OI-15 — **RESOLVED 2026-07-29 (the Owner), and BUILT the same evening.**
      The ruling: *panel media is an AWOW network share; the **panel PULLS** —
      once after boot and on demand via a dedicated SSH-invocable command — with
      **MIRROR semantics** (`--delete`: content removed from the LAN source
      disappears from the panel cache); `/media/*` is then served **panel-locally**
      by the shell's Electron host.* So the question the item asked ("which origin
      serves `/media/*`?") is answered *panel-side*, and this repo owes the pull,
      not a route: the `TODO(OI-15)` stub is gone from `stack/caddy/Caddyfile`,
      replaced by a one-line statement that the kiosk site serves no `/media`
      route. Built here: `stack/autoinstall/wall/wall-sync.{sh,service}` (cifs
      mount → `rsync -a --delete` of ONLY `Music/` + `FrameVideos/` →unmount) plus
      `wall-media-manifest.py`, which emits the shell's two contracts
      (`music/index.json`, `frame/playlist.json`) into the cache as the sync's
      post-step. The dedicated command is `sudo systemctl start
      wall-sync.service`; there is deliberately **no timer**. See the audit entry
      below for what ran for real. **Still owed by the OTHER side** (not this
      repo): the Electron host actually mapping `/media/*` onto the cache dir —
      OfficeWallNaglight's half of the same ruling.
    - OI-16 — **RESOLVED 2026-07-29 (evening, the Owner): option (a) — the
      wall-sync also fires on RESUME from suspend.** The panel suspends nightly
      (`SLEEP_MODE=suspend`) and resumes without booting, so boot-only sync went
      stale by design; now every wake behaves like a boot for freshness. Built
      the same evening: `stack/autoinstall/wall/wall-sync-resume.service` — the
      standard systemd resume hook (`After=suspend.target` +
      `WantedBy=suspend.target`, so it starts when the suspend transaction
      completes, i.e. at WAKE) running `systemctl start --no-block
      wall-sync.service`. `--no-block` is what keeps the wake imperceptible: the
      hook only enqueues and exits, and the sync runs detached with its own
      journal/timeout/failure status. Wi-Fi wrinkle handled in `wall-sync.sh`:
      `network-online.target` is not re-evaluated on resume, so the script does
      a bounded non-fatal `nm-online` wait (30 s) before mounting; a still-down
      network fails the mount loudly and the retry is the on-demand command.
      Zero new knobs. See the audit entry below; the real suspend→wake firing is
      hardware-only (WALL-BURN-IN.md §8).
  - **In flight** _(driver; no approval needed)_:
    - OI-4 — layering WI-10.2/10.11/10.12 onto the migrated base →
      [stack/docker-compose.yml](../stack/docker-compose.yml)
    - OI-9 — **RESOLVED 2026-07-29.** (Found 2026-07-10 during the SR-013 red
      run: a `die` — e.g. a cifs mount failure — exited 1 WITHOUT posting
      `ok=false`, so only ERR-trap failures fed the tracker.) `die` now calls a
      registered reporter (`DIE_REPORTER`) before exiting; `backup.sh` registers
      `report_failure`, the single idempotent failure path shared with the ERR
      trap. A failing report is swallowed into a WARNING so it can never mask
      the original error, and an unset `DIE_REPORTER` (restore.sh,
      backup-standby.sh) is a clean no-op. Verified for real on four die paths
      — see the 2026-07-29 audit entry. Still owed: an assertion inside the
      committed sim legs (they exercise ERR-trap failures, not `die`).
- **Assumptions (unattended):** see the Assumptions log below.
- **Next action:** the Owner reviews + pushes; **for the newly-built wall lane
  (OI-12): fills in `WALL_HOST`/`PANEL_IP`/`PANEL_USER_SUB`/`WIFI_*`, gives the
  panel a DHCP reservation on its hardware MAC, confirms the router forwards
  :80/:443 and NOTHING else, registers the two wall templates in
  `Personal\homelab\deploy\FieldSchema.psd1`, and works
  `stack/autoinstall/wall/WALL-BURN-IN.md` on a desk before the panel is
  mounted** — which now includes filling in `MEDIA_SHARE_UNC` + the share
  credentials and working `WALL-BURN-IN.md` §8, since an unconfigured panel fails
  `wall-sync.service` on every boot by design — §8 now also carries the OI-16a
  hardware proof (suspend, wake, watch `journalctl -u wall-sync-resume -b` show
  the sync fired); fills in the wake values + the
  Windows-side WoL settings (OI-13) and the real ingest/exclusion values
  (OI-14); points the on-box IceDrive client at the chosen library paths
  (OI-11); creates the Google OAuth client
  (OI-1); runs the V3 boot (OI-5); ratifies the tier-2 decisions (OI-7) —
  then the "V3.5 dress rehearsal" (real secrets in the VM: External vSwitch,
  TLS decision, backup VHDX) discussed 2026-07-10 turns the sim-GREENs into
  real evidence.
- **UPDATE 2026-07-03 (WI-10.13):** the "no Docker on the dev machine" constraint
  above is now LIFTED — WSL2 + Ubuntu 24.04 + docker-ce is installed on the dev
  PC (Docker Desktop explicitly NOT installed, per the Owner's pick). `naglight:local`
  now builds for real and `docker compose config` resolves this stack's full
  compose file. See audit log entry below for versions/detail. Wave 2 (V1
  homehub-sim, WI-10.14/10.15) is now unblocked.

## Scope (restated from the brief)

- **Goal:** the deploy repo for the headless AWOW AK41 always-on box — an
  unattended, self-healing Docker stack (DNS, reverse proxy + TLS, the NagLight
  tracker behind Google sign-in, Actual Budget, LAN observability) plus an
  Ubuntu autoinstall image and full LAN remote management. **Config only** — app
  code lives in NagLight / Finance-Auditor / MinecraftKeeper.
- **Stakeholders / end user(s):** the Owner (homelab operator); the tracker's hosted
  end-users reach it only through oauth2-proxy.
- **Active hats:** Stakeholder, UX/Docs, System Engineer, Software Engineer, Test
  Engineer, **Network**, **Security/Ops** (the domain hats this infra scope
  needs).
- **Supported platforms:** the deploy target is **Linux** (Ubuntu 24.04 on the
  AWOW box); authored on Windows. Not a launchable product.
- **Constraints:**
  - ~~No Docker on the dev machine~~ **SUPERSEDED 2026-07-03 (WI-10.13):** WSL2 +
    Ubuntu + docker-ce now installed on the dev PC. `naglight:local` build and
    `docker compose config` are now verified for real (see audit log). Full
    runtime bring-up (containers actually running end-to-end) is still PENDING
    the WI-10.14 homehub-sim harness.
  - **Public-facing repo (Q10.6):** only `*.example` templates tracked; no real
    secret/hash/email/LAN detail/personal name. Local commit identity pinned to
    `diytechy <diytechy@users.noreply.github.com>`.
  - **Kit:** minimum profile, decision dial **HIGH** (secrets-adjacent infra).
  - **No product source in this repo** → the Python `ruff`/`pytest`/arch-map
    steps are dropped (not left passing vacuously, ADOPTING.md §3). The
    product-layer check is `scripts/validate_config.py`.
- **Non-goals:** a container registry / CI publishing (deferred, Q10.2 — local
  builds for now); the NagLight app code and its multi-user engine (NagLight
  repo, WI-10.4/10.5); the secret-handoff script (WI-10.3, the Owner ratifies).
- **Definition of done:** the repo's G1 gate is green (`check.py`), `.env.example`
  enumerates every knob, config coverage validates, and the honest validation
  ledger records what remains PENDING a Docker host.

## Honest validation ledger (WI-4.7 note carried forward; updated 2026-07-11)

| Check | State |
|---|---|
| `docker compose config` (core + all tier-2 profiles) | PASS (WSL docker-ce; core = the original 8 services with no profiles) |
| Live bring-up + curl health + `dig` + OAuth round-trip + tear-down | **GREEN in the V1 sim** (vs Dex/internal-CA/fixtures — `validate-sim.sh` 6 checks); **real-Google/real-TLS/host-:53 PENDING V3 boot + hardware** |
| Backup pipeline (cifs + offsite + feed + restore drill) | GREEN (`run-backup-sim.sh`, re-run 2026-07-29 after the ingest/exclusion change); drive-power + volume-source call contracts GREEN (mock-shim legs) — drive spin-down physics + real-docker volume copy are V3/burn-in |
| **INGEST step (library mirror) + EXCLUSIONS** | **GREEN — committed sim leg `run-ingest-sim.sh` (2026-07-29), 28 checks over the REAL cifs path:** mirror byte-identical to the live share + restore byte-equal, `--delete` deletion propagation asserted, global+per-set patterns kept out of archive AND `files.tsv` while every excluded path is named in the log/`<set>.excluded.log`/MANIFEST, empty-share refusal + its override, 3 loud config failures each posting `ok=false`. Untested by anything: a real Windows share as the ingest source, and real library-scale volumes (V3/burn-in) |
| Wake-on-LAN pre-step + OI-9 `die` reporting | **Exercised for real on WSL2 (2026-07-29)** — magic-packet bytes, probe, timeout-dies-loudly, `die` paths posting `ok=false` — the wake half from a THROWAWAY harness, **not a committed sim leg**; the `die`-reports-`ok=false` contract is now asserted for real in `run-ingest-sim.sh` (e1–e3, plus the empty-share refusal). A real magic packet has never woken a real box (V3/hardware). `OFFSITE_PATH` needs no leg — the offsite step is retired (OI-11) |
| **Wall kiosk site (SR-016) — the identity swap + the 403 default** | **GREEN — committed sim legs, `validate-sim.sh` checks 7-8 (2026-07-29):** a FORGED `X-Forwarded-User` arriving from the panel's `/32` reaches the tracker as `PANEL_USER_SUB` (read back off `/api/export`'s filename, so the identity the tracker actually saw is asserted, not inspected); the panel's `/api/today` is 200 and `/` serves the shell build without swallowing `/api/*`; the same two requests from a NON-panel source address get 403 with **Caddy's own body**, proving refusal at the edge rather than at the tracker. Untested by anything below hardware: the **off-LAN** 403 from a real WAN vantage, and whether Docker's port publish preserves the panel's source IP on the real box (it fails CLOSED if not) |
| **Wall panel image (SR-017)** | **CONFIG-LEVEL ONLY, and that is all it claims.** A throwaway container harness ran `wall-firstboot.sh` twice against a bare `ubuntu:24.04` root with stub `systemctl`/`udevadm`/`netplan` — 27 assertions PASS on what it writes (lid conf, iio mask, the udev rule from both piped names + its re-enable hint, NM powersave/MAC pinning, netplan rendered 0600 with no `@@TOKEN@@` left, both timers re-rendered from a changed schedule, tty1-only autologin, mem_sleep reporting) — **not a committed sim leg.** NOTHING physical has run: no graphical session, no `cage`, no Wi-Fi association, no suspend/resume, no quirk verified against the actual panel. `WALL-BURN-IN.md` is the list |
| **Wall media pull + manifests (OI-15)** | **Exercised FOR REAL on WSL2 Ubuntu (2026-07-29) — 67 assertions PASS from a THROWAWAY harness, not a committed sim leg.** The real `wall-sync.sh` ran via its supported `MEDIA_SOURCE_OVERRIDE` bench hook (a local fixture library instead of a cifs mount; everything after the mount is the production path): first sync, idempotent re-run, `--delete` propagation, the empty-subtree and missing-subtree REFUSALS with the cache proven untouched, the `WALL_SYNC_ALLOW_EMPTY` override clearing the cache, a non-UTF-8 filename skipped-and-counted, and 5 loud config guards. Both manifests pass `python3 -m json.tool`, and the emitted `index.json` was fed to **OfficeWallNaglight's real `normalizeManifest()` under node** (20 more assertions: stations, once-only URL encoding, quotes/`&`/`#`/non-ASCII round-tripping). **NOT tested by anything:** the cifs mount itself, Wi-Fi, library-scale volumes, the kiosk user reading the cache on a real box, and the *other* half of the ruling (the Electron host serving `/media/*`) — `WALL-BURN-IN.md` §8 is the list |
| Q10.9 B+ image payload: `export-images.sh` save + `docker load` all 9 | PASS (WSL; loads idempotent) — first-boot load-at-VM awaits V3; **oauth2-proxy v7.15.2 pin bump needs a re-export + sim re-run (OI-7c)** |
| Tier-2 pins exist on their registries (`docker manifest inspect`) | PASS — but tier-2 services have never been STARTED anywhere (enable-time validation, stack/README §9) |
| Shell scripts `bash -n` | PASS |
| `docker-compose.yml`, `meta-data`, `user-data` YAML parse | PASS (PyYAML) |
| Every compose `${VAR}` has an `.env.example` key | PASS (`validate_config.py`) |
| Every Caddy `{$VAR}` passed by the caddy service env | PASS |
| Bind-mount sources + autoinstall-referenced files exist | PASS |
| `check.py` (G1: config-validate + registry-integrity + doc-navigability) | PASS |

What only V3/hardware can still prove: real Google consent, publicly-trusted
ACME certs, Technitium on the host's real `:53`, the `extra_hosts` dns.<domain>
fix, drive spin-down physics, thermals — then the burn-in checklist
(stack/README §6) signs the box off. For the **panel**, add: the off-LAN 403, the
graphical session, Wi-Fi, S3 suspend/resume, and all six hardware quirks
(`stack/autoinstall/wall/WALL-BURN-IN.md`).

## Gate Sign-offs

Add columns for any active domain hats. Drop the `G-Release` row for a one-off
deliverable.

| Gate | Stakeholder | UX/Docs | System Eng | Test Eng | Human |
|---|---|---|---|---|---|
| G1 — Requirements/UX/Constraints | PENDING | PENDING | PENDING | n/a | PENDING |
| G2 — Decomposition & Test Coverage | n/a | n/a | PENDING | PENDING | PENDING |
| G3 — Implementation | n/a | n/a | PENDING | PENDING | PENDING |
| G-Release — Release readiness | n/a | n/a | n/a | PENDING | PENDING |
| G-Final — Acceptance | PENDING | n/a | n/a | (evidence) | PENDING |

---

## Audit log

[Earlier session log — archived, may be out of date](archive/2026-09-13/docs/status.md)

2026-09-13: documentation audit archived dated implementation narratives
and historical session logs. Operating rules, gate records, active
requirements and runbooks were retained. See the shared current findings
for deployment mismatch, provisioning gaps and all 17 Owner items.

2026-09-14 (D-BACKEND, item 23 step 5): the broker's switch backend. The shell
gained its upper-left microphone button and Mute/Headset/Speaker switch on the
renderer side, but the shipped broker backend was still `UnavailableBackend`, so
every tap on the glass was refused honestly and did nothing. `switch_backend.py`
is now the shipped backend: it turns a validated `set_output`, `set_input_mute`,
`set_volume` or the legacy `set_mute` into one sequenced request file that the
root applier picks up, and it routes no device at all. `routing.validate_action`
gained `set_volume` with a closed 0..100 percentage and an optional output guard,
and the broker returns the sequence it minted so a client can correlate its own
request exactly rather than by value.

Four decisions worth reviewing, all recorded at the code. (1) The `seq` in the
reply is OPT-IN, through an optional `echoSeq` request field, because the
shipped renderer validates the action result as an exact key set and an
unconditional extra key would have broken it in whichever order the two repos
were deployed. (2) The minted sequence moved from nanoseconds to MICROSECONDS: a
nanosecond epoch is past JavaScript's safe-integer range, and the renderer
compares it against the applier's mark with `Number.isSafeInteger`, so the
comparison it depends on would have been skipped in silence. It is also floored
above the applier's recorded mark and any unconsumed request, so it increases
across a restart, across two taps in one tick and across a clock stepped
backwards. (3) Authorization ships allowing the switch verbs alone; every verb
that names a device keeps its deny-by-default, so the routed-device backend
stays shut. (4) A level applies to whichever output is selected when the applier
runs, and the request wire names no output, so a caller that means a particular
one sends `output` and the request is refused with `switch_moved` if the switch
has already left it. The residual race, between that read and the applier's run,
is stated at the code rather than papered over.


2026-09-14 (D-BACKEND, terra rounds 1-5): eight findings, seven fixed and two
rejected with their reasons recorded at the code and in tests. Fixed: an
unreadable state file read as an empty one, so three mutation decisions were
taken from `{}` and a backward clock could mint below the applier's mark while
the backend answered accepted; `switch_moved` preserved across the backend seam
by its token alone, so a backend could answer `set_output` or `status` with a
sentence only ever true of a guarded `set_volume`; a sparse state file reported
as an unsupported switch rather than normalized as the applier normalizes it,
which drew an unknown switch and refused reconciliation on a panel whose switch
works; LLR-015 naming `AudioRouter.*` for a class called `AudioBroker`; the
completed-mutation journal outliving the volatile request file it acknowledged,
so a restart in the moment before the applier ran left a journal saying the
switch had moved and told the retry it had already succeeded; and a redo being
refused by the generation ceiling it deliberately does not advance.

REJECTED, with the reasons at the code. (1) That `_state_strict` accepts a
valid but partial object: the applier reads the same file through a field-wise
fallback, so an absent or damaged `request_seq` is -1 on both sides, and
validating a stricter schema here than the applier validates would refuse
requests on a panel whose switch works. A test now asserts the two readers
agree rather than leaving it a coincidence. (2) That a second tap overwriting an
unconsumed first request lets the landing check settle the first one: the
request file is a one-slot mailbox holding the latest intent, and that is the
design. The residue is real and is stated rather than papered over: replaying
the superseded request's completed reply reports success for an intent that was
overtaken. Both alternatives are worse (an exact match makes the redo rewrite
the older command over the newer one; refusing while a request is unconsumed
fails the second tap of a double-tap). Closing it properly needs a per-sequence
queue in the APPLIER's protocol, which this work does not own. Owed follow-up.

2026-09-14 panel follow-up validation: source commit c4136e6 is local; no push
or deployment. Config validation and firstboot Bash syntax pass. Targeted audio
tests pass (366), and the native Linux hermetic audio-switch suite reports
103 PASS. A disposable unprivileged Linux clone ran 1,414 passing tests with
10 skipped before the five-failure stop. Remaining failures involve the wider
hermetic suites, reset-versus-EOF socket expectations, /run permissions/systemd
host verification and the absent weight service account. Its doc check also
cannot resolve HomeHub sibling links inside the disposable clone; the original
checkout doc-navigability check passes. Registry integrity reports zero integrity
errors and 23 existing orphans; no gate advancement is claimed. Full logs are
in HomeHub/build/panel-followup-minipc-native-check-20260914.txt. No broad-suite
green or hardware acceptance is claimed.
