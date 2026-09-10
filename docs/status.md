# Project Status — Blackboard

Live coordination for the gated process (see [process.md](process.md)). Keep the
**Current State** header short and current; append the audit log below (newest
last) — it is the record, not required reading for every pass.

---

## Current State

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

<!-- Append verdict blocks here per process.md §5. Newest at the bottom. -->

### DRIVER — G1 — Round 1 — 2026-07-03
Scaffolding created. Starting G1.

### Assumptions log (unattended, dial=HIGH — the Owner to confirm/revert)
- A1 — Layout: migrated `life-tracker/deploy/*` under `stack/` at the repo root
  (kept the kit's `docs/`/`scripts/` roots). On-box path renamed `deploy/` →
  `stack/` under `/opt/homehub/`; the box hostname/opt-dir keep the `homehub`
  name (faithful to the source; the Owner knows it).
- A2 — This repo is treated as gate **G1** (requirements-agreed) — config is the
  deliverable, there is no compiled source to carry to G2/G3. The Python
  product/arch-map steps are dropped, not left vacuous.
- A3 — oauth2-proxy image pinned to `v7.6.0`, Google provider, allow-list via
  `authenticated-emails.txt` (Q10.5). Redirect URI `…/oauth2/callback`.
- A4 — SSH is **key-only** by default in the autoinstall (locked password),
  per WI-10.12 "key-only auth". Cockpit installed but **not** proxied publicly.
- A5 — Kept the `TRACKER_DATA_REMOTE` clone/pull entrypoint behaviour from the
  source; multi-user (D3) sets it blank + `TRACKER_COMMIT=false` (documented).
- A6 — SN-012/SR-015 shape (2026-07-20, the Owner asked for "opt-in light UI +
  SN-001 rescope"; details decided unattended): xrdp + **minimal** XFCE
  (`--no-install-recommends`, no desktop meta-package) rather than a full DE
  or VNC; the script never downloads the AppImage (vendor URL churn on a
  public repo — the Owner scp's it, `ICEDRIVE_APPIMAGE=` path knob); sync-resume
  is the documented post-reboot one-RDP-touch (NO autologin/virtual-display
  hack — that would fake self-healing the vendor app can't honestly offer);
  priority C, Verification=Inspection, no sim leg (a host-level GUI can't be
  exercised in the compose sim; the V3.5 rehearsal VM is where it could be
  tried for real). Revert any of these at the next gate if wrong.
- A7 — Wake/offsite shape (2026-07-29; the decisions themselves were ratified,
  these mechanics were not): the wake probe targets **tcp/445** because SMB is
  the port the pull actually needs — "awake" is defined as "can serve the
  share", not "answers ping"; `BACKUP_WAKE_TIMEOUT` defaults to **120 s** and
  the packet is re-sent every 15 s while waiting (a sleeping NIC can miss one);
  the timeout is a **budget, not a deadline** — a run can overshoot it by up to
  one probe+sleep cycle (~5 s); two OPTIONAL knobs beyond the three asked for
  (`BACKUP_WAKE_BROADCAST`, `BACKUP_WAKE_IFACE`) exist only because the
  no-dependency `/dev/udp` send cannot set `SO_BROADCAST` and the fallbacks
  need somewhere to read a subnet/interface from; `OFFSITE_PATH` must **already
  exist** (the run fails rather than creating it — a typo'd path would
  otherwise report green with the files where nothing syncs). Revert any of
  these at the next gate if wrong.
- A8 — Ingest/exclusion shape (2026-07-29; the two steps themselves were
  ratified, these mechanics were not): the ingest table is its own knob
  (`INGEST_SOURCES`) rather than a fourth `BACKUP_SOURCES` spec kind, because an
  ingest entry needs a DESTINATION and is not a backup set; the grammar is
  `name=//host/share -> /abs/dest` with the arrow, and both a non-UNC source and
  a relative destination are rejected; the destination **leaf** is created on
  first ingest but a **missing parent is fatal** (that is a typo or an unmounted
  library filesystem); a share that mounts but holds **no files** while the
  library copy does **refuses to mirror** — new knob `INGEST_ALLOW_EMPTY=false`
  is the explicit override (this is the one guard that exists purely because
  `--delete` is irreversible); `--dry-run` passes `--dry-run` to the mirror too;
  ingest reuses the existing wake pre-step rather than owning a second one, and
  exclusions deliberately do **not** apply to ingest (they filter the BACKUP,
  not the library). Per-set exclusions are `name.exclude=` lines **inside the
  one `BACKUP_SOURCES` table** (one table, one place to look — the SR-013
  doctrine), which costs one rule: a set name may not end in `.exclude`; a
  `name.exclude=` line naming a set that does not exist **fails the run**;
  patterns go to rsync (saves the copy) AND tar (the archive-level promise), and
  visibility is implemented with rsync's own `--debug=FILTER` decisions
  (`<set>.excluded.log`) plus a new `excludes` MANIFEST column — the column is
  additive, so runs written before it restore unchanged. Revert any of these at
  the next gate if wrong.

- A9 — Wall-lane shape (2026-07-29; OI-12 and the LAN-port+/32 variant were
  ratified, these mechanics were not): `WALL_PORT` defaults to **8443** and is a
  **T0** knob (no FieldSchema entry — the default is the public value); the site
  address carries no `bind` directive because `LAN_IP` is not an address the caddy
  container owns, so the LAN-binding is the compose **publish** instead; the 403
  body is the distinct string `wall: panel only` **specifically so the sim can tell
  an edge refusal from the tracker's own no-identity 403**; the shell's document
  root lives at `stack/wall-shell/` (i.e. `/opt/homehub/stack/wall-shell` on the
  box) rather than a sibling of the stack dir, so the existing bind-mount coverage
  check applies to it; `/api/*` is the only proxied prefix (`/drill` is left to the
  shell, which renders its own from `items[]`); the panel's payload lands at
  **`/opt/wall-panel/`** and its env file at `/etc/wall-panel/wall.env` (0600),
  mirroring the AWOW's layout without sharing it; `PANEL_USER_SUB` is deliberately
  **absent** from `wall.env.example` (the panel never learns its own identity — the
  site injects it) and `NAVIDROME_*`/`PANDORA_*` ship **commented**, because
  un-commenting them makes the household emitter demand store keys for an optional
  feature; the kiosk session is reached by a **tty1 autologin + profile hook**
  rather than a system unit, because `cage` needs a logind seat (with `seatd`
  installed as the documented fallback); `SLEEP_RTC_WAKE` is a new knob beyond the
  ratified two, and if the RTC alarm cannot be armed the panel **refuses to
  suspend** and degrades to backlight-off for that window (a reachable panel beats
  a dark one); `mem_sleep_default=deep` is **reported, never silently written** to
  the kernel cmdline; the quirk-3 udev rule is generated from a **pipe-separated**
  knob and is not written at all when that knob is empty. Revert any of these at
  the next gate if wrong.

- A10 — OI-19 shape (2026-08-03; the `/cdrom` defect and the "refuse a
  production build that lost its secrets" doctrine were both stated in the
  brief, these three mechanics were not): (i) the site step **does not search
  for the payload at all** — it reads `/target/opt/homehub/site`, which
  late-command 3 has already copied, so the CIDATA-by-label search exists in
  exactly one place rather than two that can drift (the wall image's
  late-command 4a already worked this way); (ii) a build declares itself with
  `BUILD_PROFILE=production` **inside the user-data**, defaulting to the SAFE
  value so the SIM path is the one that must opt out and a silent no-op there
  is caught by an assertion — it lives in user-data rather than the payload
  precisely because a payload that went missing would take a payload-borne
  marker with it; (iii) a required site file that is **absent** is loud but not
  fatal, because `Build-VentoyStick.ps1` marks `cifs.creds`,
  `samba-users.creds` and `drive-identity.conf` `Required=$false` and halting a
  household's install over an optional file would be worse — but a file that is
  PRESENT and fails to install IS fatal. Whether a missing `.env` specifically
  should halt is the Owner's call, and (iv) `drive-identity.conf` is now
  installed at all, which it never was: both stagers emit it, three consumers
  read `/etc/homehub-samba/drive-identity.conf`, and no late-command ever put
  it there. Revert any of these at the next gate if wrong.
- A11 — OI-18 shape (2026-08-03; the Owner ruled **exit (b), two UNC/credential
  pairs**, and the storage map already fixed the addresses, shapes, cadences and
  failure policies — these six mechanics were still ours to choose, and each is
  a place a different agent would reasonably have chosen differently):
  (i) **the frame skip is gated on a REACHABILITY PROBE, not on the mount's
  return code** — a 4 s TCP connect to 445 before mounting. `mount.cifs` returns
  the same rc for "asleep" and "wrong password", so inferring the state from the
  failure would have made a wrong credential permanently invisible on a share
  that is *designed* to fail quietly. A box that answers and then refuses is
  therefore FATAL; only "nothing answering" is a skip.
  (ii) **configuration defects are fatal for BOTH flows** — an unset or
  placeholder UNC, or a missing/unreadable credentials file, is not a sleeping
  box, and the frame flow's licence to be quiet does not extend to "nobody
  filled this in".
  (iii) **two units, not one, and not two scripts** — `wall-sync.service` keeps
  OI-15/OI-16a's boot+resume+on-demand and now syncs both flows;
  `wall-sync-frame.{service,timer}` is the same script with `--only frame` on
  §4d's one-minute clock. One script keeps the `--delete` guards in one place;
  two units are unavoidable because the cadences and the timeouts differ by an
  order of magnitude (3600 s vs 120 s). A per-flow `flock` and a per-flow
  `--only` on the manifest generator stop the two from colliding.
  (iv) **the staleness ladder MEASURES but never ESCALATES** — a stamp at the
  cache root and a journal line that becomes a WARNING past
  `WALL_FRAME_STALE_WARN_HOURS` (default 24), but never a failed unit however
  old the content gets. "Mini-serv has been off for a week" is an allowed state
  per §4d and at what age it stops being allowed is a ruling nobody has made.
  **Whether it should ever alert, and whether it should post to the tracker, is
  the Owner's.**
  (v) **the inline `MEDIA_CIFS_USER`/`MEDIA_CIFS_PASS` fallback is RETIRED** —
  one inline pair cannot serve two hosts, and duplicating it per flow would have
  added four knobs whose only purpose is to hold a password somewhere less safe
  than the credentials file that already exists. A root-only credentials file is
  now the only supported form.
  (vi) **`MEDIA_CIFS_EXTRA` stays ONE knob** shared by both mounts — it is a
  protocol choice and both hosts speak SMB3. Split it only if a real box turns
  out to need two versions.
  Revert any of these at the next gate if wrong.

### DRIVER — G1 — Round 1 — 2026-07-03 (migration + spine)
Migrated the deploy stack, wired the tracker to `naglight:local`, authored the
high-level SN/SR spine (8 SN, 11 SR), and added `scripts/validate_config.py` as
the config-repo product check. `check.py` (G1) green; integrity 0. WI-10.2 (oauth
+ Caddy re-route), WI-10.11 (aux containers), WI-10.12 (remote mgmt) layered in
subsequent commits.

### DRIVER — G1 — Round 1 — 2026-07-03 (WI-10.2 oauth2-proxy + Caddy re-route)
Added the oauth2-proxy service (Google provider, allow-list file, cookie secret,
identity headers to the tracker). Re-routed the Caddyfile: tracker host →
oauth2-proxy (no basic_auth); Actual + dns keep basic_auth (split into
per-service snippets since Caddy resolves {$VAR} once at load). Firstboot now
materializes the allow-list from `OAUTH2_PROXY_ALLOWED_EMAILS`. Documented the
OWNER MANUAL STEP (Google OAuth client) in .env.example + stack/README. Redirect
URI to register: `https://tracker.<domain>/oauth2/callback`. config-validate
green (25 compose vars covered, 8 Caddy vars passed).

### DRIVER — G1 — Round 1 — 2026-07-03 (WI-10.11 auxiliary containers)
Added Uptime-Kuma, Dozzle, and optional ntfy (compose `profiles: [ntfy]`), all
LAN-only (published bound to `LAN_IP`, never proxied publicly), all
healthchecked, every knob in `.env.example`. config-validate green (31 compose
vars covered).

### DRIVER — G1 — Round 1 — 2026-07-03 (WI-10.12 LAN remote management)
Implemented now, in the autoinstall: SSH **key-only** (allow-pw false, locked
password, authorized-keys placeholder), **Cockpit** host package (LAN-only :9090,
not proxied), and **unattended-upgrades**. Documented the remote `docker compose`
ops workflow. Wrote `REMOTE_MANAGEMENT.md` — the reimage-over-LAN ladder as a
decision memo with checkboxes (recommended: **B GRUB recovery partition** primary
+ **D smart-plug/USB** fallback). **HIGH-RISK line:** nothing destructive /
reimage-related (no `storage:` recovery-partition change, no GRUB reinstall entry,
no PXE) is implemented until the Owner checks a box.

<!-- agent-setup --> Agent setup (2026-07-03): agents=`claude`; skills materialized: downstream-resync, gate-advance, registry-hygiene. AGENTS.md remains the canonical, agent-neutral guide (skills are opt-in accelerators, not a process gate).

### DRIVER — G1 — Round 1 — 2026-07-03 (WI-10.13 dev-PC container runtime)
The Owner picked **(a) WSL2 + docker engine inside Ubuntu**, explicitly over Docker
Desktop (not installed; no other tooling touched). This closes Wave 1's
unverified-image-build honesty gap for real.

**Installed on the dev PC (machine-level, Owner-consented):**
- WSL2 itself was already enabled/functional (a pre-existing
  `podman-machine-default` WSL2 distro was running) — no VirtualMachinePlatform
  enable + reboot was needed.
- `Ubuntu` distro registered via the pre-existing `CanonicalGroupLimited.Ubuntu`
  appx package (was installed but never first-run) — ran non-interactively via
  `ubuntu.exe install --root`, avoiding the interactive username/password
  prompt. Result: **Ubuntu 24.04.1 LTS (Noble)**, WSL version 2. Default WSL
  user is `root` (a consequence of the `--root` non-interactive path). A
  secondary non-root user `<owner>` (placeholder — the real login name is
  redacted per SR-010) was also created and added to the `docker` +
  `sudo` groups for future interactive use, but is NOT the WSL default (no
  extra restart was spent switching it — root already has full docker access).
- `/etc/wsl.conf` → `[boot] systemd=true`; confirmed via `wsl --shutdown` +
  relaunch that `systemd` is PID 1.
- **docker-ce from Docker's official apt repo** (not `docker.io`, not Docker
  Desktop): `docker-ce docker-ce-cli containerd.io docker-buildx-plugin
  docker-compose-plugin`. Versions: **Docker 29.6.1** (build 8900f1d),
  **Docker Compose v5.3.0** (plugin). `docker.service` enabled + running under
  systemd.

**Verification, in order (all real, all honest):**
1. `docker run --rm hello-world` → **PASS** (pulled + ran, full expected output).
2. `docker build -t naglight:local /mnt/c/Projects/NagLight` → **PASS**. Built
   clean: Go 1.26-alpine build stage → alpine:3.20 runtime stage, final image
   `naglight:local` (14.1MB content, 47.8MB disk). No errors, no warnings beyond
   Docker's own advisory notices. This closes NagLight's TC-044
   unverified-build gap.
3. `docker compose --env-file <scratchpad>/sim.env config` from `stack/` →
   **PASS** (exit 0). Used a placeholder-but-syntactically-valid `.env`
   (fictional domains/keys/tokens; generated fresh, kept in the agent's
   scratchpad, never written into this repo). All 8 services resolved
   correctly: `actual`, `caddy`, `ddns`, `dozzle`, `oauth2-proxy`, `technitium`,
   `tracker` (confirmed `image: naglight:local` — the just-built image),
   `uptime-kuma`. Volumes/networks/healthchecks all present in the rendered
   config. Minor test-data artifact (not a stack bug): the placeholder bcrypt
   hash values contained unescaped `$` characters, which docker compose's own
   `.env` interpolation partially consumed (`$2a$14$...` truncated to `$2a$14`
   in the rendered output) — a property of how I wrote the throwaway env file,
   not of the compose file itself.

**Tooling note for future sessions:** invoking `wsl.exe` through this agent's
Bash/PowerShell tools silently mangles any `$VAR` in the command string (an
outer shell layer pre-expands it before the real command runs) and Git Bash's
MSYS layer rewrites leading `/mnt/c/...` paths unless `MSYS_NO_PATHCONV=1` is
set. Workaround used throughout: write scripts to files (Write tool, no shell
involved) and execute them via `wsl.exe -d Ubuntu -- bash /mnt/c/...script.sh`
with `MSYS_NO_PATHCONV=1` set on any command touching `/mnt/c/...` paths
directly.

**Remaining for the Owner:** none — no reboot, no interactive prompt was needed.
Wave 2's V1 homehub-sim (WI-10.14/10.15) is now unblocked.

### DRIVER — G1 — Round 1 — 2026-07-04 (WI-10.14 homehub-sim harness + V1 gate)
Built `sim/` as a compose **overlay over the real `stack/docker-compose.yml`**
(never a fork): `docker-compose.sim.yml` + `.env.sim` (all fictional), a mock
**Dex** OIDC provider (2 static users) swapped in for Google, Caddy on an
**internal CA**, Technitium moved to the bridge with an alt API port, ddns + aux
containers neutralised, a `simclient` probe box, and multi-user fixture seed
data. `sim/run-sim.sh` brings it up and provisions the Technitium split-horizon
zone; `sim/validate-sim.sh` is the V1 gate. **RAN IT FOR REAL** on WSL2/docker
29.6.1 — full gate output:

```
-- (1) service health --   technitium/caddy/tracker/actual healthy; init-perms exit=0;
                           oauth2-proxy /ping=200 & dex discovery=200 (distroless, no HC)
-- (2) split-horizon DNS -- dig @technitium {tracker,actual,apex}.homelab.sim -> 10.99.0.10
-- (3) Caddy vhosts + auth -- tracker (internal CA) -> 302 to Dex; actual -> 401 no-auth, 200 w/ auth
-- (4) oauth2-proxy + Dex -- FULL headless login (curl cookie-jar) -> authenticated tracker 200
-- (5) multi-user isolation -- A/B see only own data; no-identity -> 403; A export = only A's items
-- (6) /api/feed round-trip -- ok=true->done, ok=false->cleared, ok=true->done
== V1 GATE: PASS (all checks green) ==
```

**Real bugs the sim caught in the wave-1 config (all FIXED in the base compose,
never before RUN):**
- **tracker healthcheck** was `/api/today` — returns **403** in multi-user mode
  (the D3 default), so the container could never report healthy. Fixed to
  `/healthz` (identity-free in both modes; matches the NagLight Dockerfile).
- **oauth2-proxy healthcheck** used `wget` but the stock image is **distroless**
  (no shell/wget) — the probe could never run and blocked Caddy. Removed the
  container healthcheck (probe `/ping` from outside) and changed Caddy's
  dependency to `service_started`.
- **technitium + actual healthchecks** used `wget`, absent from both images
  (they ship bash, not wget). Fixed to a tool-independent bash `/dev/tcp` probe.

**FLAGGED FOR OWNER (a NagLight repo fix, out of this repo's scope):** the
`naglight:local` image runs as `USER tracker` (uid 1000) but the `tracker_data`
named volume initialises **root-owned**, so multi-user `mkdir /data/<sub>` fails
with EACCES and every request 500s. Correct fix = `mkdir -p /data && chown
tracker:tracker /data` before `VOLUME` in NagLight's Dockerfile. The sim
reproduces that end-state with an `init-perms` one-shot so the tracker still runs
at uid 1000 (faithful to prod) — but the real image should be fixed.

**Sim-vs-real deltas the sim cannot cover (for the hardware burn-in):** real
Google OAuth consent; publicly-trusted ACME/TLS certs; Technitium binding the
host's real `:53` (systemd-resolved owns loopback :53 on WSL, hence the bridge +
alt-port approach — a split-horizon test still runs, `dig @technitium` from the
client); the AWOW hardware. Aux containers (Kuma/Dozzle/ntfy) and ddns are
disabled in the sim (LAN_IP binds / zero external calls) — config-validated in
wave 1, out of the V1 gate scope. Full delta table in `sim/README.md`.

### DRIVER — G1 — Round 1 — 2026-07-04 (WI-10.15 Mini-serv-sim + bash backup service)
Built the **real bash backup service** at `stack/backup/` (ASSUMPTION confirmed
in use: it lives in MiniPC-Deployer as box-plumbing, not its own repo) —
`backup.sh` + `restore.sh` + `common.sh` + `backup.env.example` + systemd
`.service`/`.timer`, implementing HOMELAB_TOPOLOGY.md's six steps in pure bash
(no .bat/.ps1). Recovery MANIFEST format (the `*FilesHashTable.csv` successor):
per run `MANIFEST.tsv` (one row/set: set·source·archive·algo·archive_sha256·
files·bytes·reason) + `<set>.files.tsv` (sha256·size·mtime·relpath per file) +
the `.tar`/`.tar.zst` archive + `RUN.json`. Auto-compression-where-applicable
decides zstd-vs-plain-tar per set by already-compressed byte ratio (FileBackup
exemption, lifted to archive granularity). Hash = **sha256** (coreutils-native)
rather than FileBackup's xxHash128 — documented internal-integrity delta.

Built `sim/mini-serv-sim/`: a `dperson/samba` container (Mini-serv stand-in)
exposing three fictional committed shares — `minecraft` (Paper tree: realistic
`paper-1.20.4-435.jar`, 3 plugin jars with **parseable** `plugin.yml`,
`server.properties` with a fake rcon "secret", a `world/` tree), `satisfactory`
(save tree), and an empty writable `icedrive` offsite target — plus a privileged
`backup-runner` that cifs-mounts them and runs the REAL service.

**RAN THE FULL CYCLE + RESTORE DRILL FOR REAL** (`run-backup-sim.sh`):
```
backup.sh: minecraft -> zstd (.tar.zst, 9% already-compressed < 60% threshold), 13 files
           satisfactory -> plain .tar (98% already-compressed >= threshold), 3 files
           total 16 files / 33429 bytes; retention keep=3
step5 offsite: pushed 5 files into //mini-serv/icedrive/homehub-backup/run_<ts>
step6 feed: POST /api/feed ok=true -> HTTP 200; tracker /api/today shows
            backup-files done=true (round-trip confirmed)
RESTORE DRILL: reconstruct minecraft from archive+manifest -> RESTORE OK 13/13
            byte-exact (sha256+size); diff -r vs live share IDENTICAL; delete
            plugins/ subtree, reconstruct again -> IDENTICAL (recovered); fake
            rcon.password round-tripped intact; 3 plugin jars restored.
== BACKUP LEG: PASS (all checks green) ==
```

**Real bug the sim caught + FIXED in the service:** `compression_decision`
`printf`'d without a trailing newline, so the `read` consuming it returned
nonzero and (under `set -o errtrace`) tripped the never-silent-green ERR trap —
every run failed at step 2 and correctly posted ok=false (proving the
never-silent-green path works). Fixed by emitting the trailing newline; the
next run went green end-to-end.

**Fixture shares are UP for the WI-10.16 MinecraftKeeper session.** Start them
standalone with `sim/mini-serv-sim/run-backup-sim.sh --shares-only` (needs
`sim/run-sim.sh` first for the shared `homehub-sim_default` network); shares are
`//mini-serv/{minecraft,satisfactory,icedrive}`, user `awow` / `simpass`,
minecraft+satisfactory exported READ-ONLY (live-share-stays-read-only rule).

### DRIVER — G1 — Round 1 — 2026-07-04 (WI-10.18 V3 gate — ISO/VM scripts)
Built `vmtest/` (agent delivers scripts + docs; **the boot itself is the Owner's** —
needs an elevated PowerShell session + the Hyper-V Windows feature, both
machine-level, neither touched here):

- **`vmtest/build-seed.sh`** — LIGHT path (default): renders the REAL
  `stack/autoinstall/user-data`+`meta-data` with SIM values (ephemeral
  ed25519 keypair, random SIM console password, unchanged storage/late-
  commands logic), copies the repo into a `deploy-payload/` tree with a SIM
  `.env` (fictional domain/OAuth client, real-shaped throwaway oauth2-proxy
  cookie secret + Technitium password, real Caddy bcrypt basic_auth hashes via
  `docker run caddy hash-password`), and burns a small `CIDATA`-labeled seed
  ISO with `genisoimage`/`xorriso`. No repack of the stock ISO — Ubuntu's
  NoCloud datasource auto-detects any attached CIDATA-labeled media (same
  mechanism `stack/README.md`'s "Second USB" already documents).
- **`vmtest/build-repacked-iso.sh`** — HEAVIER fallback: same SIM rendering,
  but bakes `autoinstall ds=nocloud;s=/cdrom/nocloud/` into a patched copy of
  the stock ISO's `/boot/grub/grub.cfg` (via `xorriso -boot_image any
  replay`, which reuses the ORIGINAL hybrid BIOS+UEFI El Torito boot catalog
  rather than hand-rebuilding one) plus embedded `/nocloud/` +
  `/deploy-payload/` — truly zero-keypress, at the cost of ~3.4GB
  copied/rewritten per build.
- **`vmtest/New-HomeHubVm.ps1`** / **`Remove-HomeHubVm.ps1`** — Hyper-V Gen2 VM
  (4 vCPU/8GB static RAM stand-in for the AK41, 64GB dynamic VHDX,
  `MicrosoftUEFICertificateAuthority` Secure Boot template for the Ubuntu
  shim, Default Switch/NAT by default with a documented External-switch
  option for real LAN exposure). Idempotent, `-WhatIf` support, elevation
  asserted at the top. **Never run** (elevation + Hyper-V are the Owner's call).
- **`vmtest/README.md`** — the V3 runbook: ISO strategy write-up (why the
  light path needs one manual GRUB keypress and the heavy path doesn't, with
  the exact edit to make), the 24.04.4 download URL + SHA256 (verified for
  real, see below), Hyper-V enable steps, run order, what "success" looks
  like (tracker legitimately can't reach healthy without staging
  `naglight:local` separately — a pre-existing gap, not new; documented which
  `stack/README.md` §6 burn-in items do/don't apply in a VM), and the
  VM-vs-hardware deltas (NAT vs LAN IP, no real ACME/OAuth/DDNS, no USB
  backup drives, no thermal/storage checks).

**RAN FOR REAL (honest ledger):**
- `build-seed.sh` — run twice (fresh + idempotent re-run). Output validated:
  `user-data` parses as YAML (`python3 -c 'yaml.safe_load(...)'`), the SSH
  placeholder/password/hostname substitutions land correctly, `meta-data` gets
  a fresh `instance-id`, the SIM `.env` renders correct values (spot-checked
  `DOMAIN`, `LAN_IP`, `ACME_EMAIL`, real Caddy bcrypt hashes, cookie secret),
  `isoinfo` confirms volume label `CIDATA` and the right files at the ISO
  root. Idempotent re-run reused the SSH key + all SIM secrets unchanged
  (verified byte-identical across runs) — this caught and fixed a real bug
  (below).
- `build-repacked-iso.sh` — downloaded the real
  `ubuntu-24.04.4-live-server-amd64.iso` (SHA256
  `e907d92eeec9df64163a7e454cbc8d7755e8ddc7ed42f99dbc80c40f1a138433`, verified
  byte-for-byte against `releases.ubuntu.com/24.04/SHA256SUMS`) and ran the
  full repack. Confirmed: `/boot/grub/grub.cfg` in the output carries
  `autoinstall ds=nocloud;s=/cdrom/nocloud/` on both boot entries;
  `xorriso -report_el_torito plain` shows BOTH a BIOS and a UEFI boot image
  still present (the "replay" trick preserved the hybrid boot catalog);
  `/nocloud/{user-data,meta-data}` and `/deploy-payload/` present and correct
  inside the ISO.
- `python scripts/check.py` / `validate_config.py` still PASS unchanged (G1
  green) after adding `vmtest/`.

**NOT run (honest gap, by design/scope):** `New-HomeHubVm.ps1`,
`Remove-HomeHubVm.ps1` — need elevation + the Hyper-V feature, an agent doesn't
make that call. **No VM has been booted from either ISO.** The GRUB-edit
mechanism (light path) and the El-Torito-preserving repack (heavy path) are
verified at the ISO-structure level only, not by an actual boot.

**Real bug the smoke test caught + FIXED:** the shared secrets file
(`vmtest/.out/secrets/creds.env`) was being re-read via `. "$creds_file"`
(bash `source`) on idempotent re-runs; the SHA-512 password hash it holds
contains `$6$...` crypt syntax, which bash tried to expand as positional
parameters under `set -u`, aborting with `line 8: $6: unbound variable`.
Fixed by extracting values with `grep`/`cut` instead of sourcing. Also folded
what had been a per-call, unbounded-growth `>>` append of the Technitium
password + oauth2-proxy cookie secret into the same once-generated,
idempotently-reused block as the SSH key and console password.

**Side effect flagged for the Owner (OI-6 above):** downloading + repacking the
~3.4GB ISO (even under a WSL-native path, not `/mnt/c`) grew WSL2's
`ext4.vhdx` — which itself lives on `C:` — from ~21GB free down to ~9GB, and
deleting the files afterward did **not** give the space back (a known WSL2
quirk: the sparse vhdx doesn't auto-shrink). Reclaim steps are in
`vmtest/README.md` §2.

### DRIVER — G1 — Round 1 — 2026-07-04 (Q10.9 B+ ALL-IMAGES — bake every image into the ISO)

Implemented the Owner's locked **Q10.9 B+** decision (HOMELAB_RESTRUCTURE_PLAN.md):
every stack image is `docker save`d into the ISO deploy payload and `docker
load`ed at first boot, so a freshly-imaged AWOW comes up "from infancy" with
**zero registry/internet dependency for container images**, versions pinned to
exactly what the homehub-sim validated.

**What was built:**
- `vmtest/export-images.sh` — resolves the full image set via `docker compose
  config --images` (from `docker-compose.yml` + the PINNED tags in
  `.env.example`, ntfy profile included), pulls any image not already local at
  its pinned tag, and `docker save`s each into `vmtest/.out/images/*.tar` with an
  `images.manifest.tsv` (ref/id/digest/file/bytes). Fails LOUDLY if any image is
  missing/unpullable; `naglight:local` (no registry home, Q10.2) must be
  pre-built or the script aborts — it is the one image the box can never fetch.
- **Per-image plain `.tar`, no zstd** (measured, justified): `docker save` under
  the containerd/OCI image store already writes compressed layer blobs — a 526MB
  actual-server image saves to a ~106MB tar; a zstd pass buys ~nothing and would
  add an `apt-get install zstd` dependency. Per-image (vs one combined tar) is
  composable + idempotent and gives firstboot per-image load logging + graceful
  per-image degrade. (`--zstd` remains available if ever wanted.)
- `stack/autoinstall/firstboot.sh` — new **step 3**: before `docker compose up`,
  `docker load` every tar found in `/opt/homehub/images` (with fallbacks
  `$STACK_DIR/images`, `/cdrom/deploy-payload/images`,
  `/media/deploy-payload/images` so it works in either ISO layout). Idempotent;
  per-tar failures warn-and-continue (compose can still pull). **Graceful
  degrade:** no payload present → loud NOTICE + fall back to the pre-Q10.9
  pull-at-compose-up behaviour.
- Payload wired into **BOTH ISO paths** via a shared `stage_images_into_payload`
  in `vmtest/lib/common.sh`: it folds `vmtest/.out/images/*.tar` into
  `deploy-payload/images/` (hardlinked when the fs allows — saves ~470MB of C:,
  OI-6). The **light** path (`build-seed.sh`) burns that into the CIDATA seed ISO
  (~1MB → ~470MB); the **repacked** path (`build-repacked-iso.sh`) maps the same
  `deploy-payload/` dir into the ISO's `/deploy-payload/` (~3.4GB → ~3.9GB).
  Either way the tars land at `/opt/homehub/images` for firstboot.

**PIN SET (`latest`/floating → concrete, Q10.9 B+).** `latest` was fine for
bring-up; B+ makes what-boots == what-was-validated, so floating tags are now
wrong. Digest = registry index digest as saved (`docker save` under the
containerd store; see `images.manifest.tsv`):

| Service | `.env` var | was | pinned | registry digest |
|---|---|---|---|---|
| technitium | `TECHNITIUM_IMAGE_TAG` | `latest` | `15.2.0` | `sha256:23d3b63d959e997800b095fe93009b3fae271b5258234ff2ade8535cb33682c8` |
| caddy | `CADDY_IMAGE_TAG` | `2-alpine` | `2.11.4-alpine` | `sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648` |
| oauth2-proxy | `OAUTH2_PROXY_IMAGE_TAG` | `v7.6.0` | **`v7.15.2`** (SECURITY bump 2026-07-10 — auth-bypass CVEs fixed since v7.6.0; NOT yet sim-validated/exported — re-run V1 sim + export-images.sh; digest recorded at that export) | *(pending re-export; v7.6.0 was `sha256:dcb6ff8d…`)* |
| tracker | `TRACKER_IMAGE_TAG` | `local` | `local` (local build, no registry) | id `sha256:9a573d4367032a4d872736718f5dd68872bcf5b1d01d727359ff36413f8f4112` |
| actual | `ACTUAL_IMAGE_TAG` | `latest` | `26.7.0` | `sha256:e18b7fbfec6157a368fad4146563f397502e9da70a120aeaeac63b4977405d1c` |
| ddns | `DDNS_IMAGE_TAG` | `latest` | `v2.10.0` | `sha256:3e2aa558946b5a293def4d73008fa4651c072b2c12932cecd02126fb23979831` |
| uptime-kuma | `UPTIMEKUMA_IMAGE_TAG` | `1` | `1.23.17` | `sha256:3d632903e6af34139a37f18055c4f1bfd9b7205ae1138f1e5e8940ddc1d176f9` |
| dozzle | `DOZZLE_IMAGE_TAG` | `v8` | `v8.14.12` | `sha256:0df89c904da71e94a0c9ed3c89a890f01488321b5f10ac1e0c0bedcead9af6e4` |
| ntfy | `NTFY_IMAGE_TAG` | `latest` | `v2.25.0` | `sha256:cfbbb1bac9196cb711e29ef0ac4adaeb033be6235f1df857705dc39c14384a1d` |

**How the concrete tags were derived (honest):** the 5 **core** images ran in
the V1 homehub-sim; each concrete pin was verified to be the SAME image V1 ran —
technitium `latest`'s linux/amd64 sub-manifest is byte-identical to `15.2.0`'s
(`sha256:85c2cfd4…`), caddy `2-alpine` and actual `latest` share their exact
index digest with `2.11.4-alpine` / `26.7.0`, oauth2-proxy was already `v7.6.0`,
naglight is the locally-built `naglight:local`. The 4 **aux** images (ddns,
uptime-kuma, dozzle, ntfy) were **disabled in the V1 sim** (LAN_IP binds / zero
external calls), so they have no sim-validated version — they were pinned to
their current newest concrete releases and exported now; **they are first
validated at V3 boot / hardware burn-in.** (ddns note: Docker Hub's `latest`
tag is a differently-built multi-arch index than `v2.10.0`; the named release
`v2.10.0` was chosen for reproducibility.)

**RAN FOR REAL (WSL2 / docker 29.6.1):**
- `export-images.sh` end-to-end → pulled the 4 aux images at their pinned tags,
  re-tagged the 3 core floating→concrete (layers already present, near-instant),
  saved all 9. **Total payload = 469 MB** across 9 tars (well under the Owner's
  ~1–2GB estimate). Manifest written.
- `docker load` of all 9 tars → each restored to its exact pinned `repo:tag`
  and re-loading is an idempotent no-op (proves firstboot step 3's guarantee:
  compose finds every image locally, no pull).
- `docker compose config --images` with the pinned `.env.example` → lists
  exactly the 9 concrete pinned refs (what compose asks for == what's baked).
- `build-seed.sh` (light path) with the payload → 471 MB seed ISO, volume id
  `CIDATA`; `isoinfo` confirms all 9 tars + manifest at
  `/deploy-payload/images/`. Hardlink staging worked (link count 2 — no C: bloat).
- Repacked path: the Q10.9 addition (images folded into `/deploy-payload/`) was
  verified via the exact `xorriso -map <dir> /deploy-payload` codepath —
  `xorriso -lsl` confirms all 9 tars land at `/deploy-payload/images/`. The full
  3.4GB stock-ISO repack + El-Torito/GRUB handling was already verified in
  WI-10.18; not re-downloaded (proportionality; C: had 16GB free but the
  mechanism + trivial one-line addition were already covered).
- `scripts/check.py` + `validate_config.py` → **PASS** (G1 green; 35 compose
  vars covered).

**AWAITS V3 (the Owner's boot, unchanged):** the actual first-boot `docker load` +
`compose up` run inside a booted VM/hardware. No VM has been booted. Everything
above is the pre-boot smoke test the dev PC can run headlessly.

**OI-6 update:** C: now shows ~16GB free (was ~9GB at the snapshot); the image
tars + seed ISO were written to `vmtest/.out` on `/mnt/c` (real C: via 9p), which
does **not** grow the WSL `ext4.vhdx`. Only the 4 aux-image pulls (~200MB into
the docker store) touched the vhdx.

### DRIVER — G1 — Round 1 — 2026-07-04 (WI-10.10 DRIVE POWER DESIGN — dynamic standby)

Implemented the Owner's ratified DRIVE POWER DESIGN in the bash backup service. The
backup drives are the box's biggest electrical lever (5–8 W each spinning ≈ the
whole CPU), so the policy is **dynamic standby**, two pieces:

**What was built:**
- **Boot-time default standby** — a per-boot oneshot
  `stack/backup/backup-standby.service` + `backup-standby.sh` applies a
  conservative `hdparm -S` spin-down timeout to each configured backup drive
  (`hdparm -S` does not persist across power cycles, so it re-applies every boot,
  like `powertune.service`). Shipped/enabled via autoinstall `late-commands`
  exactly like powertune (runs in place from the stack dir so it can source
  `common.sh`, matching `homehub-backup.service`). Added `hdparm` to the autoinstall
  packages list (NOT guaranteed on Ubuntu Server).
- **Dynamic hold in the run** — `backup.sh` disables standby (`hdparm -S 0`) on
  its target drive(s) at run start and **restores the configured timeout on any
  exit via an `EXIT` trap** — fires on success, on the ERR-trap's `exit 1`, on a
  `die`, and on interrupt. The EXIT trap fires AFTER the ERR trap, so it never
  disturbs the never-silent-green `ok=false` reporting path; it only re-arms the
  drives. Prevents both wear modes: no start/stop churn during long no-write
  phases (hashing/verify), no aggressive-timeout cycling.
- **Knobs** (`backup.env.example`, placeholders only): `BACKUP_DRIVE_DEVICES`
  (space-separated `/dev/disk/by-id/...` paths — **by-id, never sdX** which
  renumbers) and `BACKUP_DRIVE_STANDBY` (default `241` = 30 min). **Empty device
  list = the whole feature is a clean no-op.** The confusing `hdparm -S` encoding
  (`1..240` = n×5 s so 240 = 20 min; `241..251` = (n−240)×30 min so 241 = 30 min)
  is documented once in `common.sh` and in `backup.env.example`.
- **HARD RULE honored:** power management NEVER fails a backup — missing `hdparm`,
  absent device path, or an enclosure rejecting the command → logged WARNING and
  continue. `drive_standby_set` always returns 0 so it composes with the ERR-trap
  machinery without tripping it. USB-enclosure caveat noted in comments +
  `backup.env.example`; per-drive verification is a burn-in step (`hdparm -C`).

**RAN FOR REAL (WSL2 / docker 29.6.1) — the CALL CONTRACT proven with mock shims**
(platters can't spin in a container, but the sequence of `hdparm` calls can be
asserted). New `sim/mini-serv-sim/run-drivepower-sim.sh` puts a mock `hdparm`
(logs every invocation) and mock `curl` (captures each NagLight POST body) on
PATH in the runner, uses fake by-id device files, and runs REAL backup cycles
against the Samba fixtures. **All four assertions GREEN:**
- **(a)** `-S 0` (standby disabled) issued to **each** configured device at run
  start.
- **(b)** the configured timeout (`-S 241`) re-issued to **each** device on
  normal exit (exactly 4 calls; all disables precede all restores).
- **(c)** on a FORCED mid-run failure (mock `rsync` exits 1) the restore **still
  fires** via the EXIT trap AND the run **still posts `ok=false`** (mock curl
  captured `"ok":false`; `on_err` ran → `BACKUP FAILED` + `RUN.json=failed`;
  nonzero exit).
- **(d)** with **no devices** configured: **zero** `hdparm` calls and an
  unchanged green cycle.

Also validated the boot oneshot directly (`docker run` throwaway, 5 cases):
`-S 241`→"30min" and `-S 240`→"1200s" (=20 min) encoding correct; empty list =
no-op; absent device = WARN + skip + exit 0; missing `hdparm` = WARN + exit 0
(never fails boot).

**No regression:** re-ran `run-backup-sim.sh` end-to-end — full six-step cycle +
restore drill **PASS** (backup.env.sim has `BACKUP_DRIVE_DEVICES=""`, so the
standard cycle logs zero drive-power lines — the clean no-op path). `bash -n`
clean on all scripts; `scripts/check.py` (G1) + `validate_config.py` **PASS**
(the file-presence check now also asserts the powertune + `backup-standby`
unit/script exist, and `user-data` still parses as valid `#cloud-config` YAML).

**HARDWARE-ONLY REMAINDER (honest):** whether the actual USB-SATA backup
enclosure **honors** `hdparm` standby at all — many bridge chips swallow/fake the
command — cannot be known in sim; it is a per-drive **burn-in** check
(`hdparm -C /dev/disk/by-id/...` to read active/idle vs standby; `hdparm -y` to
force a spin-down and confirm). The real electrical/spindle-wear benefit is
likewise a hardware measurement. The sim proves the *call contract and its
failure-path composition*, not the drive's physical response.

### DRIVER — G1 — Round 1 — 2026-07-10 (STACK REVIEW + TIER-2 OPT-IN CATALOG — SN-009/SR-012)

The Owner asked for (a) a review of the deployer stack and (b) an opt-in catalog of
additional self-hosted services (photos ×2, media, music, podcasts, home
automation, passwords, etc.). Both delivered this session.

**Review findings → fixes applied (config-only):**
- **BUG (real-box): `dns.<domain>` would 502.** The Caddyfile proxies
  `host.docker.internal:5380`, a Docker-DESKTOP-only name — plain docker-ce
  never resolves it. Fixed with `extra_hosts: host.docker.internal:host-gateway`
  on the caddy service. The V1 sim could not catch this: `Caddyfile.sim`
  deliberately proxies the bridge container name instead. **Needs V3/hardware
  verification.**
- **SECURITY: oauth2-proxy pin bumped v7.6.0 → v7.15.2** (multiple 2026
  auth-bypass advisories fixed in between: CVE-2026-34457, CVE-2026-40575,
  GHSA-7x63-xv5r-3p2x, GHSA-pxq7-h93f-9jrg, GHSA-c5c4-8r6x-56w3 email-validation
  — the last one weakens exactly our allow-list gate). NOT sim-validated at the
  new tag yet — OI-7(c); pin-ledger row updated with digest PENDING re-export.
- **Docker log rotation** was unconfigured (unbounded json-file on a small
  eMMC) — autoinstall now writes `/etc/docker/daemon.json` with the `local`
  driver (rotates by default).
- **Backup gap recorded as OI-8** (stack volumes — incl. `actual_data` — backed
  up by nothing); deliberately NOT fixed inline: `backup.sh` is
  WI-10.15-validated behavior and a change there needs its own sim assertions.
- Minor (recorded, not acted on): Dozzle has no auth (LAN-bind is the only
  gate); ntfy topics are LAN-open by default; Technitium :5380 is cleartext
  HTTP on the LAN; no mem_limits yet (matters as tier-2 services get enabled).

**Tier-2 catalog added (SN-009/SR-012, all OFF by default, ntfy-profile
pattern):** immich(+ml)/photoprism (photos — at most ONE), jellyfin (QSV via
/dev/dri), navidrome (music), audiobookshelf (podcasts/audiobooks), vaultwarden
(Caddy-site-only ingress by design), homeassistant+mosquitto (host-net HA;
committed no-secret mosquitto.conf), syncthing, freshrss/mealie/homepage, diun
(update NOTIFIER — closes the stale-pin gap within the pins-never-drift
philosophy; `diun.enable=false` label on the registry-less naglight:local).
Wiring: `COMPOSE_PROFILES` in `.env` is the enable switch (firstboot's plain
`compose up -d` honors it); commented Caddyfile sites + `EXTRA_SUBDOMAINS`
(provision script loop) for public exposure; `export-images.sh` gained
`EXTRA_PROFILES` and its default behavior — tier-2 excluded from the baked
payload — falls out of profiles being off (OI-7(a) to ratify).
**[SUPERSEDED 2026-08-07 — see the last entry in this log.** That default was
ratified the other way, by running it: a box whose `.env` enabled five profiles
shipped 9 of the 15 images it needed. `export-images.sh` now bakes what
`COMPOSE_PROFILES` enables. Left in place because this entry records what was
decided on 2026-07-10, and rewriting it would falsify the log.] Docs:
stack/README §9 (incl. the pre-build configuration chain + RAM/storage ground
rules); root README table row.

**Versions (honest):** Immich layout verified against the v3.0.2 release
compose (server/ml `v3.0.2`, `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-
pgvectors0.2.0`, valkey `9`); PhotoPrism upstream publishes `latest` as its
stable tag (documented exception). Remaining tier-2 pins are best-effort
known-good tags, marked VERIFY-at-enable in `.env.example`; none are
sim-validated and none carry custom healthchecks yet (WI-10.14 lesson — probe
tooling per image is checked empirically at enable time).

**Assumptions (unattended, to confirm at next gate):** tier-2-not-baked is the
right Q10.9 B+ boundary (OI-7a); `MEDIA_ROOT=/srv/media` default pending the
storage decision (OI-7b); FreshRSS chosen over Miniflux (single container +
SQLite); Zigbee2MQTT deferred until a coordinator stick exists; subdomain
labels fixed as vault/photos/prism/jellyfin/music/audio/home.

**RAN FOR REAL (this session, dev box):** `validate_config.py` (72 compose vars
covered, all PASS) + `check.py` G1 (config-validate, registry-integrity with
SN=9/SR=12, doc-navigability) → **PASS**; `bash -n` clean on the two changed
shell scripts; WSL-Ubuntu `docker compose config` — with **no profiles** it
resolves exactly the original 8 core services (tier-2 exclusion proven for the
export path), with **all 15 profiles** the config is VALID and lists the full
26-image set; `docker manifest inspect` confirmed **every** tier-2 pin AND
oauth2-proxy `v7.15.2` exists on its registry. **NOT run:** any container —
tier-2 services have never been started anywhere (enable-time validation per
README §9), and the core changes (extra_hosts, log rotation, oauth2-proxy tag)
await the V1-sim re-run / V3 boot (OI-7c).

### DRIVER — G1 — Round 1 — 2026-07-10 (OI-8 → SN-010/SR-013: docker-volume sources in the ONE backup table)

The Owner ratified the single-table design ("multiple sources/destinations
configured for a single backup execution sequence"): extend the existing
`BACKUP_SOURCES` grammar rather than bolt on a second mechanism. Destinations
stay as-is (one drive target + the `OFFSITE_SETS` subset selector).

**What was built (TDD — sim first, red, then green):**
- **Grammar (SR-013):** `name=SPEC` where SPEC is `//host/share` (cifs,
  unchanged), `volume:VOL` (live copy from the volume's mountpoint via
  `docker volume inspect` — no helper image needed, the service runs as root),
  `volume:VOL@CONTAINER` (quiesce: stop → copy → restart immediately), or
  `path:/abs/dir`. Comment lines in the table are skipped, so
  `backup.env.example` ships ready-to-uncomment lines for `actual_data`,
  `technitium_config`, `caddy_data`, `tracker_data`.
- **Quiesce safety (the OI-8 hard case):** `QUIESCED` tracking + a
  `quiesce_restore` EXIT trap (runs BEFORE drive-power restore) — no failure
  path can leave a service stopped; an EXIT-trap restart failure warns LOUDLY
  naming the manual command. Inline restart failure fails the run (a stopped
  service is worse than a missed backup). Steps 2–6 untouched — volume sets
  ride the same archive/hash/manifest/retention/offsite/report pipeline.
- **New sim leg `sim/mini-serv-sim/run-volume-sim.sh`** (mock-`docker` +
  mock-`curl` shims, the run-drivepower-sim.sh pattern; feed mocked so the leg
  runs without the homehub-sim tracker).

**RAN FOR REAL (WSL2 / docker, this session):**
- **RED first:** pre-implementation run — scenarios (a)–(c) failed exactly as
  expected (volume: spec treated as a cifs UNC), (d) green. Then GREEN:
  **all 15 checks PASS** — (a) volume set archived+hashed+in-manifest, restore
  drill BYTE-EQUAL vs the fixture volume, zero stop/start without @container,
  comment line skipped, ok=true; (b) exactly one stop + one start, stop
  precedes start; (c) forced mid-copy failure (mock rsync) → container STILL
  restarted via the EXIT trap, ok=false posted, nonzero exit, on_err logged;
  (d) cifs-only table → ZERO docker calls.
- **No-regression:** full `run-backup-sim.sh` re-run against the live homehub-sim
  tracker — **BACKUP LEG: PASS** (cycle, offsite push, real NagLight feed
  round-trip, minecraft restore drill incl. post-loss reconstruct).
- `bash -n` clean; `check.py` G1 re-run after the registry additions (SN=10,
  SR=13) — see below.

**FINDING logged as OI-9 (not fixed here — stay in lane):** the RED run exposed
that `die` paths (e.g. cifs mount failure) exit 1 WITHOUT posting ok=false —
only ERR-trap failures feed NagLight. Pre-existing; small targeted fix + its
own sim assertion later.

**HARDWARE-ONLY REMAINDER (honest):** real-docker semantics (an actual
`docker stop` latency, a volume mountpoint under /var/lib/docker) are proven
only by the call contract here; first real exercise happens on a Docker host /
the AWOW with the volume lines uncommented. The sim shim answers `volume
inspect` with a fixture dir by design.

### DRIVER — G1 — Round 1 — 2026-07-10 (SR-006 resolver chain + .env QUOTING BUG fix)

**The Owner's ask:** NagLight and Finance-Auditor are private — the dev package
should take each locally-built container from a sister folder of the same name,
else grab a declared public one.

**Built:** `scripts/ensure-local-images.sh` — resolves each locally-built image
via **present → sibling build (`../NagLight`) → declared public image
(`TRACKER_PUBLIC_IMAGE` in `.env`, empty until the app repo publishes) → loud
failure naming all three fixes**. Downstream consumers are untouched: compose,
the sim overlay, and export-images.sh keep consuming the same `naglight:local`
ref regardless of which path supplied it (a public image is pulled + retagged).
`sim/run-sim.sh` now calls the resolver before bring-up; export-images.sh's
missing-tracker error points at it. Finance-Auditor has a ready-to-uncomment
entry (no compose service consumes it yet — Actual Budget is the running
finance app). `--rebuild` forces a sibling rebuild; `--dry-run` prints
decisions; `SIBLING_ROOT` overrides the parent-dir convention (CI-friendly).
SR-006 requirement/acceptance updated to cover the chain.

**BUG FOUND AND FIXED while testing (real-box first-boot breaker):**
`.env.example` carried `TRACKER_GIT_NAME=naglight bot` — an UNQUOTED space.
The file is BOTH a compose env-file AND shell-sourced by `firstboot.sh` (step
2) and `provision-technitium.sh`; sourcing that line executes `bot` as a
command → command-not-found → `set -e` kills first boot before compose up.
Never caught because the sim/seed substitute space-free values — the REAL
hand-filled-.env path was the one that would break. Fixed: quoted
`TRACKER_GIT_NAME`, quoted the space-separated knobs (`TECHNITIUM_FORWARDERS`,
`TECHNITIUM_BLOCKLISTS`), added the QUOTING RULE to the file header (compose
strips double quotes, so quoting is safe for both readers). The resolver
itself now greps only the keys it needs instead of shell-sourcing the file
(defense in depth).

**RAN FOR REAL (WSL2 / docker):** all four resolver paths — (1) image present
→ no-op; (2) `--rebuild --dry-run` → sibling build from `/mnt/c/Projects/
NagLight`; (3) no sibling + knob set → public pull+retag decision; (4) neither
→ loud failure, exit 1. Firstboot-style `set -a; . .env.example` now sources
clean (was: `bot: command not found`, exit 127); `docker compose config`
confirms quote-stripping (`TRACKER_GIT_NAME: naglight bot`). `bash -n` clean on
all touched scripts; `check.py` G1 PASS.

### DRIVER — G1 — Round 1 — 2026-07-11 (DOC CURRENCY PASS — the Owner's ask)

The Owner asked for all docs to be brought current (the tier-2 catalog and other
recent work were missing from the root README and elsewhere). Swept every doc
against the repo's actual state:

- **Root README:** intro now names the backup service + the tier-2 opt-in
  catalog; "Run it" points at the SR-006 resolver and the zero-secrets sim
  path; V1 gate row lists the drive-power + volume sim legs; "Configuring the
  REAL box" adds the QUOTING RULE, `COMPOSE_PROFILES`, the backup `volume:`
  lines, and the tier-2/EXTRA_PROFILES bake boundary.
- **AGENTS.md:** the Project section (never filled since scaffolding) now
  carries the one-line purpose, users, layout, run/check commands, non-goals,
  and sibling repos.
- **docs/architecture.md:** topology diagram gains ddns, the backup service,
  and the tier-2 subgraph; new bullets for DDNS/backup/tier-2/resolver; layout
  table adds stack/backup, stack/mosquitto, sim/, vmtest/, the resolver; the
  STALE "no Docker on the dev machine" validation model replaced with the
  three-tier static → V1 sim → V3/hardware model.
- **docs/status.md:** the honest-validation ledger no longer claims
  "PENDING — no Docker" (superseded 2026-07-03); rows now record what the V1
  sim proved vs what V3/hardware still must; Next action refreshed.
- **docs/requirements/interfaces.csv + docs/interfaces.md:** the EXAMPLE row
  replaced with the three real NagLight contracts — IF-001 (naglight:local
  image), IF-002 (trusted identity headers), IF-003 (/api/feed) — and a
  human-readable index; matching IF-IDs still need recording on the NagLight
  side. Clears the pre-existing check_docs orphan warning.
- **vmtest/README:** Q10.9 "bake EVERY container" qualified with the SR-012
  tier-2 boundary + EXTRA_PROFILES. **REMOTE_MANAGEMENT.md:** tier-2
  enable/disable added to the day-2 compose workflow. **sim/README:**
  run-volume-sim.sh listed. **stack/README §2:** QUOTING RULE + tier-2 knob
  pointer. **stack/tracker/README:** resolver pointer.

`check.py` G1 PASS (config-validate, registry-integrity, doc-navigability —
now 0 warnings). Docs-only change; no config/script behavior touched.

### DRIVER — G1 — Round 1 — 2026-07-11 (STATE & CREDENTIALS clarity — the Owner's ask)

The Owner asked where credentials live across updates/reimages and how the budget
app's bank feed wires in. Docs now say it in one place:

- **REMOTE_MANAGEMENT.md "State & credentials"** — the survives-what table:
  NO credential lives in a container/image; state = named volumes (Actual
  server pw + SimpleFIN credential + budgets, Technitium config, Caddy certs,
  tracker data, Vaultwarden) + host files (`.env`, allow-list, `.token`,
  `/etc/homehub-backup/*`). Container updates (`compose pull && up -d` / pin
  bumps) are credential-safe by construction; a reimage wipes volumes → they
  return via the SR-013 volume backups, `.env` re-seeds from the USB payload.
  Plus "What runs where": on the AWOW everything is a container except the
  backup service, powertune/backup-standby oneshots, Cockpit, sshd,
  unattended-upgrades; Mini-serv runs nothing from this stack.
- **stack/README §2** — new OWNER MANUAL STEP: SimpleFIN bank sync is a
  one-time, in-app Actual setup (Actual OWNS the SimpleFIN relationship;
  Finance-Auditor will only trigger its sync). Stored server-side in
  `actual_data`; not doable from the hermetic sim (a real bank credential must
  never enter a throwaway fictional volume) — real box or the real-secrets
  rehearsal VM, whose `actual_data` volume can be carried to hardware via the
  backup/restore path ("authenticate once" literally once).
- **Root README** — real-box step 4 points at both.

`check.py` G1 PASS. Docs only.

### DRIVER — G1 — Round 1 — 2026-07-11 (SN-011/SR-014: Finance-Auditor ingested, profile-gated)

The Owner confirmed FA's deploy artifacts landed (its commits bbea348/59d9f27:
Dockerfile + deploy/compose.service.example.yml + the ACTUAL_API_VERSION
build-arg). Verified ready and ingested per its IF-003 spec:

- **Compose service `finance-auditor`** (profile `finance-auditor` — OPT-IN
  until FA passes G-Release/G-Final, then promote to core + baked payload):
  bridge-internal to actual:5006 + tracker:8787, finance_snapshots +
  finance_actual_data volumes, FINANCE_* knobs in .env.example, TZ-honored
  RUN_AT, no ports/healthcheck (FA's documented daemon design; the tracker's
  automated-lane aging is the backstop).
- **Resolver:** ensure_image grew optional docker-build args; the
  finance-auditor entry is ACTIVE and pins
  `--build-arg ACTUAL_API_VERSION=${ACTUAL_IMAGE_TAG}` — the IF-002 coupling
  is now mechanical (bump the Actual pin → `--rebuild` rebuilds FA against it).
- **Privacy (FA §3 firewall):** backup.env.example gained
  `finance=volume:finance_snapshots@finance-auditor` (commented) + a LOUD
  never-OFFSITE_SETS warning — raw finance data rides the LOCAL drive only,
  never the IceDrive-synced share.
- **Registries:** SN-011 + SR-014; IF-004 (↔ FA IF-003; ids are repo-local —
  FA numbered first — each contract cites the counterpart id). Docs: stack +
  root READMEs, architecture topology, interfaces.md index.

**RAN FOR REAL (WSL2/docker):** validate_config.py + compose config PASS (core
still exactly 8 services without the profile; finance-auditor appears with it);
`ensure-local-images.sh` BUILT `finance-auditor:local` end-to-end (two-stage
build, @actual-app/api@26.7.0 installed in the throwaway stage); `docker run`
of the image boots under Node 24 native type-stripping and exits with the
documented config-invalid fatal naming the missing key (trackerFeedUrl) —
FA's startup contract observed. NOT run: a live pipeline cycle (that is FA's
TC-033 G-Release Demonstration — the homehub-sim's actual+tracker are a
ready-made environment for it; sim runs Actual `latest`, so pin the sim to
26.7.0 for a faithful rehearsal per FA's spec note).

**NagLight cleanup (its repo, recorded not fixed):** /api/feed accepts only
{check,ok,note} — FA posts {check,color,reason,at}; the color-lane extension
FA's IF-001 calls "pending" is still unbuilt tracker-side (and a
finance-audit item must exist in definitions or the post 400s). Also
NagLight's IF registry still claims the deploy/caddy/technitium interfaces
that migrated HERE (WI-10.1) — needs re-homing + counterpart ids; its G1
human sign-off is still pending (process debt). FA multi-user note: its feed
client sends bearer only, no X-Forwarded-User — fine single-user, a gap if
the tracker runs multi-user (D3).

### DRIVER — G1 — Round 1 — 2026-07-11 (NagLight color lane VERIFIED end-to-end + sim interpolation fix)

The Owner said NagLight was updated; verified in its working tree (NOTE: that work
is **UNCOMMITTED in NagLight** — the Owner to commit there): /api/feed now takes
three lanes ({check,ok,note} boolean; {check,color,reason,at} severity;
{check,rgb,...}), field names pinned to FA's contract (NagLight IF-006 ↔ FA
IF-001); its IF registry is re-homed with counterpart ids matching ours
(their IF-004↔our IF-003, IF-005↔our IF-002); G1 human sign-off still pending
(the Owner's).

**RAN FOR REAL (sim):** rebuilt `naglight:local` from the updated tree,
recreated the sim tracker, added the fictional `finances.md` severity-item
fixture to `sim/tracker-seed/` (mirrors NagLight's example-data), probed as a
fresh sim user with FA's exact payload shape:
`POST /api/feed {check:finance-audit, color:yellow, reason, at}` → **HTTP 200**,
`/api/today` shows `reportColor/reportReason/reportAt`, and the day's
aggregate flipped yellow with the de-identified reason in the overlay. The
FA↔NagLight seam is proven in the sim. (Also learned: a color post to a
boolean-lane item 400s by design — lanes are typed per item definition.)

**REGRESSION found+fixed en route:** `docker compose up` with
`--env-file sim/.env.sim` REFUSED to parse after the tier-2/finance additions
— unset `${MEDIA_ROOT}` renders a volume spec `:/media` (compose interpolates
the whole file before profile filtering; `config -q` tolerated it, `up` did
not). Fixed: `sim/.env.sim` gained a fictional interpolation-defaults block
for every tier-2/finance knob. Sim bring-up works again.

**Follow-ons recorded:** `naglight:local` image id changed → the baked payload
needs a re-export before any flash (folds into the OI-7c re-export); OI-10
added (disk-encryption decision); USB-wipe note added to stack/README §3.

### DRIVER — G1 — Round 1 — 2026-07-20 (SN-012/SR-015: opt-in remote light UI; SN-001 rescoped — the Owner's ask)

The Owner asked (following the IceDrive-headless discussion — the current client is
GUI-only, no daemon/CLI, WebDAV sunsetting since 2026-04) to make an on-box
light UI an **opt-in option** and to rescope SN-001: zero-click is guaranteed
for **core** services; opt-in secondary services may need UI/manual config —
always remote, restricted/minimized.

- **SN-001 rescoped** (need + acceptance intent now say "core"; opt-ins that
  can't meet the bar must be OFF by default, document their manual steps and
  what doesn't self-heal, and be fully set-up-able over the LAN).
- **SN-012 + SR-015 added** (+3 SN-012 edge rows): opt-in, off-by-default
  xrdp + minimal-XFCE layer solely for GUI-only vendor apps; first case
  IceDrive Mount & Sync. Priority C, Verification=Inspection.
- **`stack/remote-ui/`**: `setup-remote-ui.sh` (idempotent, non-interactive,
  root-checked; installs xrdp+minimal XFCE+libfuse2t64, wires `~/.xsession`,
  optional `ICEDRIVE_APPIMAGE=` install + autostart; referenced by NOTHING in
  autoinstall/first-boot) + README (LAN-only rule, what does NOT self-heal:
  post-reboot one-RDP-touch, GUI state lost on reimage + re-setup checklist,
  uninstall steps). REMOTE_MANAGEMENT.md gained the opt-in section;
  architecture layout table row added.
- **Scope line held:** backup step 5 offsite stays cifs-only — pointing it at
  an on-box synced folder is OI-11 (needs the Owner; small backup.sh extension +
  sim legs). A6 records the unattended shape decisions (minimal XFCE not full
  DE; no AppImage auto-download; no autologin sync-resume hack).

**RAN FOR REAL:** `check.py` G1 **PASS** (config-validate 77 vars,
registry-integrity `SN=12 SR=15 orphans=21 integrity=0` — the +1 orphan line
(20→21) is SR-015's standard pre-G2 "no TC" state, same as every SR
(Inspection exempts it from the no-LLR finding);
doc-navigability 48 links 0 broken); `bash -n` on the new script OK. NOT run:
the script itself (needs the real box / rehearsal VM — host-level GUI is
outside the compose sim; recorded in A6).

### DRIVER — G1 — Round 1 — 2026-07-29 (WoL wake pre-step, local-path offsite (OI-11 build), OI-9 closed + staleness sweep)

Absorbed three ratified decisions this repo had not caught up with, plus the
driver-side OI-9 fix. No new decisions were taken; two things that would need
one are named at the bottom.

**What was built**

- **Wake-on-LAN pre-step (step 1).** The Windows game box now exposes ONE share
  and is allowed to SLEEP, so `backup.sh` wakes it and waits for **tcp/445**
  before the first cifs mount. `BACKUP_WAKE_MAC` (empty = feature off),
  `BACKUP_WAKE_HOST`, `BACKUP_WAKE_TIMEOUT` (+ optional `BACKUP_WAKE_BROADCAST`
  / `BACKUP_WAKE_IFACE`, A7). The packet is best-effort; the **wait is the
  truth**, and a timeout is a LOUD failure — never-silent-green means a source
  that failed to wake must never look like a source with nothing new. No new
  dependency: the magic packet goes out over bash's `/dev/udp`, with
  `wakeonlan`/`etherwake` as the documented fallback.
- **OI-11 build half — `OFFSITE_PATH`.** Step 5 takes a LOCAL directory (the
  folder the on-box IceDrive client syncs) as the primary offsite target;
  `OFFSITE_UNC` survives as the legacy cifs push. Exactly one may be set —
  both is a config error caught at run start, not a precedence puzzle — and one
  staging routine serves both, so "which files go offsite" stays one fact.
- **OI-9 closed.** `die` now invokes a registered `DIE_REPORTER` before exiting.
  `backup.sh` registers `report_failure`: the single, idempotent failure path
  shared with the ERR trap, so whichever fires first owns the verdict. A failing
  report degrades to a WARNING (it must never mask the original error) and an
  unset reporter is a clean no-op, so `restore.sh` / `backup-standby.sh` are
  untouched.
- **Docs currency:** backup README (wake contract, offsite target choice, step
  table); `backup.env.example` (single-share example, wake section incl. the
  Windows-side settings, OFFSITE_PATH-primary offsite section);
  REMOTE_MANAGEMENT (one share, sleeps, no IceDrive role); remote-ui README
  ("nothing forces a switch" → the switch IS ratified); architecture (backup
  bullet + topology diagram: wake-then-pull, local offsite folder → on-box
  IceDrive → cloud); stack/README "Local validation status" and SN-008/SR-011
  (the "no Docker on the build machine" premise died 2026-07-03, WI-10.13);
  the two committed `\<box>\setup` references reworded so the token-rotation
  warning survives without the literal UNC path.

**RAN FOR REAL (WSL2 Ubuntu + docker-ce, Windows dev PC)**

- `python scripts/check.py` — **G1 PASS** (config-validate 77 vars;
  registry-integrity `SN=12 SR=15 orphans=21 integrity=0`; doc-navigability
  48 links 0 broken). `bash -n` clean on all four backup scripts.
- **`sim/mini-serv-sim/run-backup-sim.sh` — PASS, all checks green**, run
  against the changed service (the runner bind-mounts `stack/backup` live):
  full six-step cycle over the Samba fixtures, offsite push landed 20 files,
  NagLight feed round-trip visible in `/api/today`, restore drill byte-equal
  including the post-loss reconstruct. This is the regression net for the
  **legacy** `OFFSITE_UNC` leg — it still works.
- **`run-drivepower-sim.sh` — PASS (a–d)** and **`run-volume-sim.sh` — PASS
  (a–d)**, i.e. the forced-mid-run-failure scenarios still post `ok=false`,
  still restore drive standby, and still restart a quiesced container after the
  `report_failure` refactor.
- **A throwaway Linux harness** (scratch, not committed) drove the new paths
  with `path:` sources and a stub feed endpoint: happy run with `OFFSITE_PATH`
  → files staged under `<OFFSITE_PATH>/homehub-backup/run_<ts>` + `ok=true` +
  restore byte-identical; `OFFSITE_ENABLED=false` still a clean skip; legacy
  `OFFSITE_UNC` branch still selected and failing loudly when the share is
  unreachable; `--dry-run` unchanged. **Four `die` paths each POSTED
  `ok=false`** before exit 1 (both offsite forms set; enabled-with-no-target;
  wake timeout; unreachable cifs share) — the OI-9 contract, observed rather
  than asserted. Wake helpers checked byte-level: magic packet 102 bytes,
  `ff*6` + MAC ×16, NULs intact for MACs containing `00`; MAC parser accepts
  `:`/`-`/bare and rejects short/non-hex; probe true/false/timeout correct;
  already-awake fast path skips the packet; timeout path dies loudly.

**NOT run (honest gap)**

- **No real box was woken.** Every wake test used a loopback listener or a
  blackhole address. Whether a magic packet actually resumes the game box —
  and whether its adapter/Fast-Startup settings allow it — is V3/hardware
  (OI-13).
- **The `/dev/udp` broadcast send was REFUSED by the WSL kernel** (bash cannot
  set `SO_BROADCAST`), so only the fallback *decision path* was observed, never
  a successful send. `wakeonlan`/`etherwake` are not installed here either.
  Which method the AWOW ends up using is unknown until the box is tried.
- **No sim leg was added** for either new path. `OFFSITE_PATH` and the wake
  step were exercised from a throwaway harness that is not in the repo; the
  committed sim still drives `OFFSITE_UNC`. Recorded as OI-11(a).
- **shellcheck is not installed** on this machine (Windows) or in the WSL
  Ubuntu — not run, not claimed. `bash -n` is all the static shell checking
  that happened.
- The V1 stack sim was already up from a previous session and was **reused**,
  not rebuilt from scratch; `sim/run-sim.sh` itself was not re-run.

**For the Owner / next gate**

- **OI-13** (new): the wake feature is inert until the real MAC lands in
  `/etc/homehub-backup/backup.env`, and it needs two Windows-side settings.
- **OI-11(b)**: set `OFFSITE_PATH` once IceDrive is running on-box; the run
  fails if that directory is missing, deliberately.
- **A7** records the mechanics decided unattended (probe port, default timeout,
  re-send interval, must-already-exist rule, the two optional knobs).
- Left alone on purpose: OI-12 (wall panel) — nothing built, no wall site or
  image lane created.

---

### DRIVER — G1 — Round 1 — 2026-07-29 (SR-010 enforcement — personal name swept out of every tracked file)

The Owner sanctioned (2026-07-29) replacing the personal first name in tracked
files with the generic role term, closing the last gap against **SR-010** /
**SN-007** ("no ... personal-name in any tracked file"). Name tokens only —
no decision IDs, dates, semantics, or surrounding wording were changed, and no
git history was rewritten.

- **21 tracked files** swept (docs, requirements CSVs, READMEs, `AGENTS.md`,
  `REMOTE_MANAGEMENT.md`, compose/sim YAML, `Caddyfile`, `.env.example`,
  autoinstall + backup + remote-ui shell, `vmtest/*`) — plus this log, which the
  Owner explicitly sanctioned despite its append-only rule.
- Mapping used: possessive → "the Owner's"; sentence subject → "the Owner";
  table-cell/label/attribution forms → "Owner"/"Owner:"; the all-caps banner
  became **`OWNER MANUAL STEP`** (same 5-character name token, so the
  `.env.example` ASCII box border needed no re-drawing); hyphenated compounds
  → `Owner-gated`/`Owner-ratified`/`Owner-consented`.
- **Two conflicts, handled deliberately:**
  - The `.env.example` OAuth box had one comment line that no longer fit the
    76-column border once the name expanded; that sentence was re-wrapped across
    its existing two lines. Box width and content are otherwise identical.
  - The 2026-07-03 WI-10.13 WSL2 entry recorded a real *Linux login name* (a
    literal system identifier, not prose). Renaming it to a role phrase would
    have falsified the record, so it is now the placeholder `` `<owner>` ``
    with an inline note that the real login name is redacted per SR-010. The
    account itself is unchanged on the dev PC.
- The pseudonym `diytechy`, the `AuroLeap` org, hostnames (`mini-serv`,
  `homehub`), and all `Co-Authored-By` trailers were left untouched.

**RAN FOR REAL**

- A case-insensitive `git grep` for the name over tracked files: **102 hits
  across 21 files → 0 hits**.
- CSV structure re-parsed with Python `csv` before/after: `interfaces.csv`
  6 rows and `system-requirements.csv` 16 rows, **every row's column count
  identical** — the edits touched cell text only, never a delimiter or quote.
- `python scripts/check.py` — **G1 PASS** (config-validate: 77 compose vars vs
  `.env.example`, 8 Caddyfile vars, all bind-mounts/autoinstall files present,
  YAML parses; registry-integrity `SN=12 SR=15 orphans=21 integrity=0`;
  doc-navigability 11 docs / 48 links / 0 broken). Identical to the previous
  round's numbers, i.e. the rename moved no requirement and broke no link.

**NOT run (honest gap)**

- No sim, no container, no box. This pass is text-only; nothing executable
  changed behavior, so only the static gate was exercised.
- Commit metadata was **not** rewritten. Author identity was already `diytechy`
  (verified via `git config user.name`), and history rewriting was out of scope.

---

### DRIVER — G1 — Round 1 — 2026-07-29 (three Owner rulings: INGEST step, archive EXCLUSIONS, OFFSITE retired)

Implemented the Owner's three rulings of 2026-07-29 in `stack/backup/`. Two are
new pipeline capability; the third **removes** a design half-built earlier the
same day. No new decisions were taken — the mechanics decided unattended are
listed as **A8** and the things only the Owner can supply are **OI-14**.

**What was built**

- **INGEST (step 1b) — ratified.** A new `INGEST_SOURCES` table
  (`name=//host/share -> /abs/library/dest`, one per line, same hand-edited style
  as `BACKUP_SOURCES`) mirror-syncs each network source **into the library tree**
  BEFORE any archiving: wake (the existing `wake_and_wait`, unchanged) → cifs
  mount ro → `rsync -a --delete` → unmount. The library folder is then covered by
  an ordinary `path:` `BACKUP_SOURCES` entry, so one archive flow serves ingested
  and native folders identically. **Mirror semantics are documented loudly in
  three places** (`.example`, README, the run log itself): source deletions
  PROPAGATE, and history lives in the dated run snapshots, not the library.
  Failures are loud per OI-9 — a refused mount, an rsync error, a malformed line
  and a missing library parent each post `ok=false` and exit nonzero. One guard
  exists purely because `--delete` is irreversible: a share that **mounts but
  holds no files** while the library copy does **refuses to mirror**
  (`INGEST_ALLOW_EMPTY=true` overrides). The old pattern (a `//host/share`
  straight in `BACKUP_SOURCES`) still works; the `.example` now presents the
  ingest pair as the intended one.
- **EXCLUSIONS (step 2) — new requirement.** `BACKUP_EXCLUDE` (global,
  space-separated globs, ships as the Owner's `"*.bak"`) plus per-set
  `name.exclude=PATTERN …` lines **inside the one `BACKUP_SOURCES` table** — one
  table, one place to look. Patterns go to `rsync` at pull time (so the copy is
  never made) and to `tar` (so the archive cannot contain them). **Nothing is
  excluded silently:** the run logs the effective pattern list per set, logs each
  path the patterns actually hid (first five inline, all of them in a new
  `<set>.excluded.log` next to the archive — rsync's own `--debug=FILTER`
  decisions), records the patterns in a new MANIFEST `excludes` column and in
  `RUN.json`, and `restore.sh` states plainly that such a set is a **FILTERED
  copy** of its source. A `name.exclude=` line naming a set that does not exist
  **fails the run** rather than quietly filtering nothing.
- **OFFSITE (step 5) — RETIRED from the target state.** The Owner's corrected
  model: the IceDrive client is pointed **directly at library paths in its own
  GUI**, the service stages nothing. `backup.env.example` now documents
  `OFFSITE_ENABLED=false` as the target state with the corrected model spelled
  out and both target knobs demoted to commented legacy; step 5's code is
  unchanged and still works. Docs reworded: backup README (step-5 table row plus
  a rewritten "Offsite — retired from the target state" section), architecture
  (topology diagram now shows *backup → library → IceDrive client → cloud*, and
  the backup bullet says there is no offsite step), REMOTE_MANAGEMENT (Mini-serv
  is ingested, not staged-for), remote-ui README (the sync pairs the Owner
  creates ARE the offsite configuration).
- **New sim leg `sim/mini-serv-sim/run-ingest-sim.sh`** — the ingest/exclusion
  regression net, over the REAL cifs path (only the NagLight feed is mocked, so
  it needs no homehub-sim stack). The compose file gains one fixture: the
  intentionally always-empty share `//mini-serv/empty`, needed to prove an empty
  share cannot mirror-delete a good library copy.

**RAN FOR REAL (WSL2 Ubuntu + docker-ce, Windows dev PC)**

- `python scripts/check.py` — **G1 PASS** (config-validate 77 compose vars + 8
  Caddyfile vars, bind-mounts/autoinstall files present, YAML parses;
  registry-integrity `SN=12 SR=15 orphans=21 integrity=0`; doc-navigability
  11 docs / 48 links / **0 broken**). Same numbers as the previous round.
- `bash -n` clean on all four `stack/backup` scripts **and** all four sim legs
  (bash 5.2.21, rsync 3.2.7, GNU tar 1.35).
- **`sim/mini-serv-sim/run-ingest-sim.sh` — INGEST LEG: PASS, 28/28 checks**,
  first run of the new leg against the real service: (a) the mirror of
  `//mini-serv/minecraft` into `/srv/library/NonDocs/MiniServ` created the leaf,
  came out **byte-identical to the live share** (`diff -r`), was archived by the
  ordinary `path:` flow and **restored byte-equal**, and is recorded in
  `RUN.json`; (b) a planted library-only file **and** folder were **deleted** by
  the next mirror — the `--delete` contract asserted, not trusted; (c) `*.bak`
  globally + `docs.exclude=Downloads` kept 3 paths out of both the archive and
  `docs.files.tsv` while the keepers stayed, the **source still holds** the
  excluded files, both pattern lists appear in the log, every excluded path is
  named in the log and in `docs.excluded.log`, the MANIFEST `excludes` column
  carries them, a set with no per-set line still got the global pattern, and
  `restore.sh` flagged the FILTERED copy; (d) the empty-share refusal fired
  (library survived at 13 files, `ok=false` posted) and `INGEST_ALLOW_EMPTY=true`
  then cleared it as instructed; (e) three loud config failures (malformed
  ingest line, exclude line for an unknown set, missing library parent) each
  died nonzero **and posted `ok=false`**.
- **Regression, all re-run against the changed service:**
  `run-backup-sim.sh` — **PASS** (full cycle over the Samba fixtures, legacy
  `OFFSITE_UNC` push landed 5 files, NagLight round-trip visible in
  `/api/today`, restore drill byte-equal including the post-loss reconstruct);
  `run-volume-sim.sh` — **PASS (a–d)**; `run-drivepower-sim.sh` — **PASS (a–d)**.
  So the loop restructure (the sources table is now parsed in a pre-pass) and the
  new MANIFEST column broke nothing.
- **A throwaway WSL harness** (scratch, not committed) drove the paths the sim
  cannot reach cheaply: **glob safety** — a decoy `*.bak` in the working
  directory does NOT hijack the pattern list (patterns are split with `read -a`,
  never pathname-expanded); `--dry-run` still writes no archive and no library
  change; the `MANIFEST` / `RUN.json` / `restore.sh` fields verified by eye on a
  plain `.tar` set; the exclude-unknown-set and malformed-ingest `die`s each
  posted `ok=false` through a mock `curl`.
- **Checked the bash semantics the new code leans on** rather than assuming them:
  with `errtrace`, the ERR trap does **not** fire for a failing command
  substitution whose status is tested (`x="$(f)" || die`) but **does** for a bare
  assignment — which is why `ingest_parse` may return 1 while
  `exclude_line_name` never does. `rsync --debug=FILTER`'s exact output
  (`[sender] hiding file X because of pattern Y`) was probed before being made
  the visibility mechanism.

**NOT run (honest gap)**

- **No real box, no real share.** Every ingest test used the sim's Samba
  container as the network source; the sleeping Windows box was never woken and
  its share was never mirrored. Real-share behaviour (SMB quirks, permissions,
  file names Linux dislikes) is V3/hardware.
- **Library-scale data was never involved.** Fixtures are kilobytes, so mirror
  duration, the effect of excluding a genuinely very-large folder, and the
  interaction with the drive spin-down policy at that size are burn-in checks.
- **`zstd` is not installed on the WSL host**, so the throwaway harness ran the
  plain-`.tar` path only; the `.tar.zst` path was exercised inside the sim runner
  container (which has zstd) by the ingest + backup legs.
- **shellcheck is still not installed** on this machine or in the WSL Ubuntu —
  not run, not claimed. `bash -n` is the only static shell checking done.
- The V1 stack sim was **already up from a previous session and reused**;
  `sim/run-sim.sh` was not re-run.
- **The offsite target state is documented, not demonstrated.** Nobody pointed an
  IceDrive client at a library path — that is Owner-side GUI work this repo
  cannot exercise (SR-015).
- **No requirement-registry rows were added or edited** (`SN=12 SR=15`
  unchanged), following the precedent of this morning's wake/offsite change.
  See the two stale wordings flagged below.

**For the Owner / next gate**

- **OI-14** (new): ingest + exclusions are inert until the real share, the real
  library destination and the real "very large folder" names land in
  `/etc/homehub-backup/backup.env`; the authoritative paths are in
  `Personal\deploy\storage-map.md`, and the mirror deletes whatever the share
  deletes.
- **OI-11 rewritten** for the corrected model; the `OFFSITE_PATH` build half is
  recorded as **moot** (harmless legacy, no sim leg owed).
- **A8** records the mechanics decided unattended (the arrow grammar, the
  leaf-created/parent-fatal rule, `INGEST_ALLOW_EMPTY`, exclusions living in the
  sources table and the `.exclude` name restriction, the additive MANIFEST
  column).
- **Two now-stale requirement wordings, left for the Owner** because editing
  ratified rows is his call: SR-013's text still says sets flow "through
  archive/hash/manifest/retention/**offsite**/report", and SR-015's acceptance
  criteria still contains the parenthetical "backup.sh offsite is cifs-only
  today - a local-path offsite target is a separate ratified change". Both
  describe a step that is now retired.
- **A monitoring hole worth a decision:** with no offsite step, the backup's
  never-silent-green report **cannot** see a stale cloud copy — after a reboot
  IceDrive is down until someone opens a session, and the backup will still post
  `ok=true`. If that should be watched it needs its own check (something that
  looks at the client/cloud and feeds NagLight); that is a new decision, not
  built here.
- **A privacy-posture change worth noticing:** the Finance-Auditor §3 firewall
  used to be enforceable by config review (`finance` must not appear in
  `OFFSITE_SETS` — a line in a file). In the corrected model the cloud selection
  lives in the IceDrive **GUI**, so nothing in this repo can prove raw finance
  data is not being synced. The `.example`, the backup README and the remote-ui
  README now say so in words, which is all a config repo can do.
- Left alone on purpose: OI-12 (wall panel) — nothing built.

### DRIVER — G1 — Round 1 — 2026-07-29 (SR-013/SR-015 wording currency — the Owner's sanction)

The two stale ratified wordings flagged above are now current per the Owner's 2026-07-29 sanction (SR-013 flow drops offsite → README step 5 legacy; SR-015's deferred offsite question marked settled; SN-010/SN-012 sentences matched) — wording only, nothing ran; `scripts/check.py` PASS.

### DRIVER — G1 — Round 1 — 2026-07-29 (OI-12 RATIFIED: the wall-panel image lane, built)

The Owner ratified OI-12 with a specific, stronger variant than either option on
the table: the kiosk auth site sits on a **LAN-bound alternate port the router
never forwards, PLUS the `/32` allow-list** — belt and braces, not either alone.
Built here. New decisions taken unattended are recorded as **A9**; the things only
the Owner can supply are folded into OI-12 above, and one genuine cross-repo
decision surfaced and was deliberately NOT taken (**OI-15**).

**What was built**

- **The kiosk site (SR-016)** — `{$WALL_HOST}:{$WALL_PORT}` in
  `stack/caddy/Caddyfile`. Four guards, each independent and each documented as
  load-bearing in the block's own banner: the injected `X-Forwarded-User`
  **replaces** any client-supplied identity; `remote_ip {$PANEL_IP}/32` (one
  address, never the LAN CIDR — guest Wi-Fi, IoT gear and an inbound-facing
  Minecraft server share that network); the port published bound to `{$LAN_IP}`
  with the router forwarding only :80/:443; and `respond 403` for everything else.
  Inside, it serves the shell's static build from `stack/wall-shell/` **and**
  proxies `/api/*` to `tracker:8787` — the same origin, because NagLight sends no
  CORS headers (OfficeWallNaglight needs doc §3.2). `/music` ships as a commented
  Navidrome stub. **The Caddyfile deliberately carries no `bind` directive:**
  `LAN_IP` is not an address the container owns, so `bind` there would fail to
  listen at all — the LAN-binding is the compose publish.
- **The wall autoinstall variant (SR-017)** — `stack/autoinstall/wall/`: graphical
  target (`cage` + tty1 autologin, no display manager), Wi-Fi-only netplan
  rendered from placeholders, **no Docker and no Cockpit**, and every §3 quirk
  expressed as config: logind lid ignore (1), `iio-sensor-proxy` masked (2), a
  udev rule generated from `WALL_DISABLE_INPUT` (3), NM powersave-off +
  `cloned-mac-address=permanent` (5). D-W4 is `SLEEP_MODE=suspend|backlight`
  sharing ONE schedule, with the RTC alarm armed **before** suspending and a
  per-boot unit re-enabling the ACPI `XHC` + USB device wakeup flags that do not
  persist. Quirks 4b and 6 are not config at all (mount geometry, vent clearance,
  a measured thermal baseline) and say so.
- **The spine** — SN-013 + 8 edge rows, SR-016 (Demonstration) and SR-017
  (Inspection), IF-005 `Planned` → **`Partial`** with the four remaining gaps
  named. `validate_config.py` gained check 5: every namespaced knob a wall script
  reads must be declared in `wall.env.example` (compose cannot see an
  env-file-configured image), plus the wall variant's files and YAML.
- **Docs** — architecture ("two images from one pipeline", the third auth model),
  `stack/README` §10 (enable steps, the guards as a table, the cert analysis),
  `REMOTE_MANAGEMENT` (the panel is reimage-not-repair), and
  `WALL-BURN-IN.md` for everything only the hardware can settle.

**RAN FOR REAL (WSL2 Ubuntu + docker-ce, Windows dev PC)**

- **`sim/validate-sim.sh` — V1 GATE PASS, all 8 checks**, including the two new
  wall legs. The stack was brought up fresh (`sim/run-sim.sh`) because the overlay
  now adds a network. The wall legs reach the site from two source addresses out
  of ONE container: `simclient` sits on both the default network and a new
  sim-only `simlan` (fixed subnet, static leases), so dialling Caddy's `simlan`
  address arrives as `PANEL_IP` and dialling its default-network address does not.
  Asserted: `forged X-Forwarded-User=sim-user-attacker-9999 from the panel /32
  reached the tracker as sim-user-wallpanel-0003`; panel `/api/today` → 200;
  panel `/` → the shell fixture and `/config.json` → 200; and both `/` and
  `/api/today` from the non-panel route → **403 with Caddy's own body** (which is
  what distinguishes an edge refusal from the tracker's own no-identity 403).
- **THE SIM CAUGHT A REAL BUG ON ITS FIRST RUN, and it was the important one.**
  The design brief's literal "strip then inject" — `header_up -X-Forwarded-User`
  followed by a set of the same field — **does not work in Caddy**: header ops are
  applied in a fixed order (add → set → **delete** → replace) regardless of the
  order written, so the delete erased the injected identity, the tracker saw no
  identity, and every panel request 403'd. Verified by reading the adapted JSON
  (`caddy adapt` showed both a `delete` and a `set` of the same field) rather than
  guessed. The site failed **closed**, which is the right direction — but it would
  never have worked, and on hardware this would have looked like a panel fault.
  Fixed by relying on set-replaces-all (asserted with the forged header, not
  trusted) and a DO-NOT-ADD banner so it cannot come back.
- **`caddy validate`** on the real (not sim) Caddyfile with placeholder env:
  **Valid configuration** — and it confirmed automatic HTTPS treats the
  alternate-port site as a normal HTTPS server (`srv1`, redirects enabled).
- **A throwaway container harness** (scratch, not committed) ran
  `wall-firstboot.sh` **twice** against a bare `ubuntu:24.04` root with stub
  `systemctl`/`udevadm`/`netplan` on PATH — the mock-shim pattern this repo
  already uses for `hdparm`/`docker`. **27 assertions PASS**, and it caught two
  real bugs, both fixed: `printf '%s'` without a trailing newline made `read` skip
  the **last** piped device name (the touchpad would have kept working), and a
  redirect into a missing `/etc/udev/rules.d` / `/etc/netplan` aborted the whole
  script on a minimal root. It also proves the honest-degradation paths: the
  shipped placeholder template exits 0 while announcing that quirk 3 and netplan
  were skipped, and writes no udev rule from nothing.
- **The new config-validate check was negative-tested**: an undeclared
  `${WALL_BOGUS_KNOB}` added to a wall script makes the run FAIL, so check 5 is
  not vacuous.
- **`python scripts/check.py` — G1 PASS**: config-validate (81 compose vars, 12
  Caddyfile vars, 10 wall knobs, both `user-data` files parse), registry-integrity
  `SN=13 SR=17 orphans=24 integrity=0`, doc-navigability 11 docs / 48 links /
  **0 broken**.
- **The certificate claim was verified against Caddy's docs AND source, not
  assumed** (the ask was explicit about honesty here): automatic HTTPS activates on
  the *hostname*, not the port, so a `:8443` site still gets a publicly-trusted
  cert; ACME CAs **never** contact non-standard ports (HTTP-01 is always :80,
  TLS-ALPN-01 always :443); and Caddy's ACME challenge handler runs in every HTTP
  server ahead of route matching, dispatching on the requested hostname
  process-wide — so the existing :80 listener answers for a name that has no
  port-80 site block. **That last point is clear in Caddy's source but is NOT
  stated in its documentation**, and `stack/README` §10 says exactly that rather
  than presenting it as documented. The dependency it creates (inbound :80 must
  stay forwarded, or renewal for this name breaks) is written down, with DNS-01
  named as the fallback since a Cloudflare token already exists for DDNS.

**NOT run (honest gap)**

- **No panel, no hardware, nothing physical.** `cage` has never been started, no
  Wi-Fi has been associated, no suspend or resume has happened, no lid has been
  folded, no udev rule has been applied to a real input device, no backlight has
  been dimmed and no RTC alarm has woken anything. Every §3 quirk fix is
  *asserted as written config*, never as observed behaviour.
- **The off-LAN 403 is still an assumption.** Every wall-site probe came from
  inside a docker network. Nobody has curled the panel's hostname from cellular,
  and nobody has confirmed the router's forward list. This is the single test the
  design brief itself called out as load-bearing, and it remains owed.
- **Docker's source-IP preservation is unproven on the real box.** The `/32` match
  relies on the panel's real address reaching Caddy through the port publish. In
  the sim the probes are on the same bridge, so this is untested; if it fails, the
  site 403s the panel (fails closed, not open).
- **The wall image harness is a throwaway, not a committed sim leg.** A graphical
  kiosk session cannot be exercised in a compose sim, so unlike the wall *site*
  there is no permanent regression net for the wall *image* — the same honesty
  position as SR-015's opt-in RDP layer.
- **IF-005 has no artifact**, so `WALL_APP_CMD` points at a placeholder and the
  kiosk shows an explicit "not installed" screen. Nothing has ever run under
  `cage`, including a stand-in.
- **The payload-bake path was not extended to the wall image.** The wall variant is
  not yet wired into `vmtest/export-images.sh` or the ISO builders; the panel needs
  no container images, but which files a wall USB carries has not been implemented
  or tested.
- **shellcheck is still not installed** on this machine or in the WSL Ubuntu — not
  run, not claimed. `bash -n` (clean on all four wall scripts) is the only static
  shell checking done.

**For the Owner / next gate**

- **OI-12 rewritten as RATIFIED + BUILT**, with what still needs him listed there:
  the four T1/T3 values, the panel's DHCP reservation on its hardware MAC, the
  `EXTRA_SUBDOMAINS` label, the FieldSchema registration, and the burn-in.
- **OI-15 (new) — `/media/*` has no origin.** A genuine decision, left untaken:
  the shell's manifest paths must be same-origin, but the media is on the panel's
  cache while the origin is a site on the AWOW. Marked `TODO(OI-15)` in the
  Caddyfile with a commented stub. The likely answer is panel-side (Electron
  intercepting `/media/*`), which belongs to OfficeWallNaglight.
- **Two cross-repo consequences of registering the wall templates** in
  `Personal\homelab\deploy\FieldSchema.psd1` (Personal is not edited from here —
  reported instead): (i) `WALL_HOST`, `PANEL_IP` and `PANEL_USER_SUB` now appear in
  the **AWOW's** `.env.example` too, because the kiosk site runs on the AWOW — so
  they must move from `config.wall.psd1` to `config.common.psd1`, or the `awow`
  image's completeness rule will refuse to emit; (ii) the wall `user-data` needs
  the SSID and PSK substituted, but the emitter's `userdata` format substitutes
  only the password hash and the SSH key — the placeholders were named
  `REPLACE_WITH_WIFI_SSID` / `REPLACE_WITH_WIFI_PSK` so a generic
  `REPLACE_WITH_<KNOB>` pass is a small change rather than a new format.
- **A note on the panel-down alert:** it is required, not optional (quirk 4b), and
  it is **not** in this repo — it is an Uptime-Kuma push monitor plus a Kuma→ntfy
  notifier, configured in Kuma's UI, and its maintenance window must be taught the
  sleep window or it will cry wolf every single night.
- **`WALL_PORT` is a new T0 knob** (public default, `8443`) and needs no
  FieldSchema entry; unlisted knobs are T0 by that schema's own rule.
- Left alone on purpose: the ISO/payload wiring for the wall image, and OI-15.

### DRIVER — G1 — Round 1 — 2026-07-29 (OI-15 RESOLVED by the Owner: the panel PULLS its media — built)

The Owner ruled OI-15 the same evening it was opened, and the ruling is narrower
and better than the "which origin serves `/media/*`" framing it answered:

> Panel media is an AWOW network share; **the panel PULLS** — once after boot and
> on demand via a dedicated SSH-invocable command — with **MIRROR semantics**
> (`--delete`: content removed from the LAN source disappears from the panel
> cache). `/media/*` is then served panel-locally by the shell's Electron host.

So there is no `/media` route to design on the AWOW at all, and this repo owes the
**pull**. Built here; the shell-side half (Electron mapping `/media/*` onto the
cache) stays OfficeWallNaglight's.

**What was built**

- **`stack/autoinstall/wall/wall-sync.sh`** — mount `MEDIA_SHARE_UNC` read-only
  over cifs (same option shape and credentials-file-first precedence as
  `stack/backup/common.sh`'s `mount_cifs`), `rsync -a --delete` **only** the
  share's `Music/` and `FrameVideos/` subtrees into
  `WALL_MEDIA_CACHE/{music,frame}` (default `/var/cache/wall-media`), unmount via
  an EXIT trap so no failure path leaves a mount behind. The subtree map is a
  constant, not a knob: widening what the panel pulls is a decision, and a knob
  would let a typo widen it silently onto a 256 GB disk.
- **The guards are the ingest step's lessons, transplanted** (that code learned
  them the expensive way): an **empty** source subtree, and separately an
  **absent** one, does not get to mirror-delete a populated cache — the run
  refuses, names the override, and leaves the cache untouched;
  `WALL_SYNC_ALLOW_EMPTY=true` is the deliberate escape hatch and mirrors the
  emptiness *through the same rsync* (an empty temp dir as the source) rather than
  through a second deletion mechanism. Every failure is fatal and nonzero: the
  panel has no NagLight feed of its own, so "loud" means a failed unit plus
  journal lines.
- **`wall-media-manifest.py`** — the sync's post-step, and the reason the
  generator is Python rather than a bash JSON writer: the one thing that must not
  be got wrong is string escaping, and a real library is full of quotes,
  ampersands, `#` and non-ASCII. It emits `music/index.json` in
  `LocalLibraryProvider`'s documented shape (albums from folders, `Artist/Album`
  giving artist + album, `cover.jpg` as art, a leading track number parsed off the
  title, root-level files as the documented **flat** `tracks` form) and
  `frame/playlist.json` as `[{url,title}]`.
- **THE ASYMMETRY THAT WOULD HAVE BITTEN**, found by reading both consumers rather
  than assuming they matched: music `path` values must be **RAW** (`local.js`'s
  `joinUrl()` percent-encodes every segment itself, so encoding here would
  double-encode every space), while frame `url` values must be **ALREADY ENCODED**
  (`frame.js` assigns them straight to `video.src`). Both are asserted, in both
  directions.
- `ensure_ascii=True` on both manifests, so a non-ASCII filename ships as `\uXXXX`
  and cannot depend on the Electron host guessing a charset; a filename whose
  bytes are not valid UTF-8 (a Windows library will produce one eventually) is
  **skipped and counted** rather than emitted as a lone surrogate that would break
  the whole manifest for one bad name.
- **`wall-sync.service`** — `Type=oneshot`, `After=network-online.target` (the
  panel is Wi-Fi-only, so that is load-bearing, not decorative) and
  `After=wall-firstboot.service`, `WantedBy=multi-user.target`,
  `TimeoutStartSec=3600` for the first full copy over 802.11,
  `IOSchedulingClass=idle` so a sync cannot make the wall stutter. **No `.timer`**
  — the ruling is boot + on demand, and re-`start`ing a oneshot IS the on-demand
  path, so `sudo systemctl start wall-sync.service` is the whole documented
  interface.
- **The Caddyfile `TODO(OI-15)` stub is gone**, replaced by the one line the ruling
  makes true: `/media/*` is panel-local, this site serves no `/media` route.
- **Knobs + coverage**: `MEDIA_SHARE_UNC`, `WALL_MEDIA_CACHE`,
  `WALL_SYNC_ALLOW_EMPTY`, `MEDIA_CIFS_CREDENTIALS`/`_USER`/`_PASS`/`_EXTRA` and
  the commented `MEDIA_SOURCE_OVERRIDE` bench hook in `wall.env.example` (10 → 18
  declared wall knobs); `validate_config.py` gained the `MEDIA_` namespace, the
  three new files in its autoinstall-file list, and `wall-sync.sh` as a knob
  consumer. `wall-firstboot.sh` gained a step 8 that creates the cache dirs,
  enables the unit, and reports whether the share is configured; the wall
  `user-data` grew `cifs-utils`, `rsync` and (explicitly) `python3`, plus the
  late-commands that install the three files.

**RAN FOR REAL (WSL2 Ubuntu + node on the Windows host, 2026-07-29)**

- **A THROWAWAY harness — 67 assertions PASS, and it caught two real bugs.** The
  harness drives the REAL `wall-sync.sh` through its `MEDIA_SOURCE_OVERRIDE` bench
  hook against a fixture library with deliberately nasty filenames (apostrophe,
  double quotes, `&`, `#`, non-ASCII, spaces), under `env -i` so nothing ambient
  props it up. Covered: first sync into an empty cache; **`--delete` propagation**
  (a file removed from the source disappears from the cache AND from
  `index.json`); an idempotent no-op re-run; the **empty-subtree refusal** and the
  **missing-subtree refusal**, each with the cache asserted file-count-unchanged
  afterwards; `WALL_SYNC_ALLOW_EMPTY=true` clearing the cache and still emitting
  valid JSON (`[]`); the non-UTF-8 filename skipped-and-counted; five loud config
  guards (unset share, the shipped placeholder, a non-UNC share, a relative cache
  path, a bogus override); and the generator-not-found path failing instead of
  leaving synced media unlisted.
- **THE HARNESS'S FIRST RUN FOUND THE BUG THAT MATTERED**, and it was a
  self-inflicted one: the manifests live *inside* the directories the mirror
  refreshes, so `--delete` removed them on every run and the file counts included
  them — a no-op re-run therefore logged "1 file(s) were DELETED" and the
  cache-clearing message was off by one. Both were fixed properly rather than by
  adjusting the message: the mirror now `--exclude`s the top-level manifest
  (anchored, so a same-named file inside an album is still mirrored) and every
  logged count is a MEDIA count. The side benefit is real — a run that dies before
  the post-step now leaves the last complete manifest in place instead of nothing.
- **The emitted manifest was validated against the REAL CONSUMER, not against my
  reading of it**: `node` importing `normalizeManifest`/`joinUrl` straight out of
  `OfficeWallNaglight/js/music/local.js` (read-only; nothing in that repo was
  touched) — **20 assertions PASS**: 4 tracks / 2 albums / 3 stations with the
  endless shuffle first, `artUrl` and every track URL encoded exactly once
  (`%2520` asserted absent), quotes/`&`/`#`/`Å` round-tripping into playable URLs,
  album+artist inherited by tracks, and the flat form resolving. The frame playlist
  was checked segment-by-segment and by `new URL(...)` resolution back to the real
  filenames.
- **`python3 -m json.tool` on both manifests**, in three states: populated, empty
  (`[]`), and after the non-UTF-8 skip. All valid.
- **`wall-firstboot.sh` was re-checked for real after gaining step 8** — a second
  throwaway container harness (bare `ubuntu:24.04`, stub
  `systemctl`/`udevadm`/`netplan`), **12 assertions PASS**: the shipped placeholder
  template still exits 0, the cache dirs are created, the unit is enabled, the
  placeholder share is warned about *with its consequence stated*, a filled share
  is reported instead, and a missing `wall-sync.service` is called out rather than
  silently skipped.
- **`caddy validate` on the real Caddyfile after removing the `TODO(OI-15)` stub**
  (pinned `caddy:2.11.4-alpine`, placeholder env + a valid bcrypt so provisioning
  gets that far): **Valid configuration**, both servers still adapting.
- **`bash -n` clean** on all five wall scripts; `py_compile` clean on the
  generator (it is written to run on Python 3.8+, though the panel has 3.12).
- **The new config-validate coverage was negative-tested**: an undeclared
  `${MEDIA_BOGUS_KNOB}` in `wall-sync.sh` makes the run FAIL with exactly that
  name, so the `MEDIA_` namespace is not vacuous.
- **`python scripts/check.py` — G1 PASS**: config-validate (18 wall knobs, both
  `user-data` files parse), registry-integrity `SN=13 SR=17 orphans=24
  integrity=0`, doc-navigability 11 docs / 48 links / **0 broken**.

**NOT run (honest gap)**

- **No cifs mount was performed by any of this.** Every real run used the bench
  hook, so `mount -t cifs`, the credentials file, `vers=3.0`, and the behaviour of
  a share that vanishes mid-rsync are all untested here. The backup service's
  ingest leg exercises the same `mount_cifs` shape against a real Samba container,
  which is evidence for the *pattern* but not for this script.
- **No panel, no Wi-Fi, no library-scale data.** Fixtures are kilobytes on ext4;
  the first-sync duration over 802.11, the 256 GB disk budget with a real
  `FrameVideos/`, and the interaction with the D-W4 sleep window are hardware.
- **Nothing has ever read these manifests in the shell.** The consumer check ran
  `normalizeManifest` in node, not the Electron host — and the *other half* of the
  ruling (mapping `/media/*` onto the cache) does not exist yet in
  OfficeWallNaglight, so end-to-end playback is unproven by construction.
- **The kiosk user has never read the cache.** `--chmod=D755,F644` is asserted as
  written config, not as an observed `sudo -u panel` read (burn-in §8 has the
  check).
- **Throwaway, not a committed sim leg** — same position as the wall image
  harness: a cifs + Wi-Fi + graphical path cannot be exercised in the compose sim,
  and a leg that only re-ran the bench hook would test the hook, not the pull.
- **shellcheck is still not installed** on this machine or in the WSL Ubuntu — not
  run, not claimed.
- **No requirement-registry rows were added or edited** (`SN=13 SR=17` unchanged),
  following this lane's precedent: the pull is the mechanism of an already-ratified
  need (SR-017 / D-W8 rider / OWN-D5), not a new one. If the Owner wants the media
  pull to carry its own SR row with its own verification method, that is a spine
  edit for him.

**For the Owner / next gate**

- **OI-15 is closed on this side and half-open on the other.** The panel pulls; the
  Electron host must serve `/media/*` from `WALL_MEDIA_CACHE`. That is one line in
  `IF-005`'s contract and it is now the *only* `/media` question left.
- **OI-16 (new) — does a RESUME also sync?** Implemented exactly as ruled (boot +
  on demand), but `SLEEP_MODE=suspend` means the panel may not boot for weeks, so
  in practice the cache is as fresh as the last SSH command. Three options are
  written up in the item; none was taken unattended.
- **A freshly imaged panel FAILS `wall-sync.service` on every boot** until
  `MEDIA_SHARE_UNC` and the credentials are filled in. That is deliberate (a green
  unit on a music-less wall would be a lie), but it is a thing to expect rather
  than to debug — and it means `MEDIA_SHARE_UNC` + a root-only
  `/etc/wall-panel/cifs.creds` join the panel's fill-in list, with the share's
  identity coming from `Personal\deploy\storage-map.md`, not from a guess here.
- **The share must have `Music/` and `FrameVideos/` AT ITS ROOT.** If the household
  library's layout puts them somewhere else, that is a one-line change (a subpath
  knob, or a different share) — say which, rather than letting the sync refuse.
- **FieldSchema consequence:** `MEDIA_SHARE_UNC` is a T1-ish value and the share
  password is T3. If the wall templates are registered as planned, the password
  belongs in the secret store, not in `wall.env` — the script prefers a credentials
  FILE for exactly that reason and warns when it falls back to inline.
- **A disk-space question nobody has answered:** mirroring the whole `Music/` +
  `FrameVideos/` tree onto a 256 GB panel disk is fine for music and possibly not
  fine for video. If `FrameVideos/` is large, the honest fix is a curated subfolder
  on the share rather than a filter here — that is the Owner's call about what the
  wall should show.

### DRIVER — G1 — Round 1 — 2026-07-29 (OI-16 RESOLVED by the Owner: wall-sync also fires on RESUME — built)

The Owner decided OI-16 the same evening it was opened: **option (a)** — the
media sync also fires on resume from suspend. The reasoning the item stated
holds: with `SLEEP_MODE=suspend` the panel suspends nightly and resumes without
booting, so "once after boot" was, in practice, "approximately never". Now every
wake behaves like a boot for freshness. Zero new knobs, one new file.

**What was built**

- **`stack/autoinstall/wall/wall-sync-resume.service`** — the standard systemd
  resume hook: `WantedBy=suspend.target` pulls it into the suspend transaction,
  `After=suspend.target` orders it after that target is *reached* — which only
  happens when `systemd-suspend.service` completes, i.e. **at wake**. So it
  starts on resume, never at suspend time; `Type=oneshot` with no
  `RemainAfterExit` drops it back to inactive so every later cycle fires again.
- **`ExecStart=/usr/bin/systemctl start --no-block wall-sync.service`** — and
  `--no-block` is load-bearing, not a flourish: the hook runs *inside* the
  resume transaction, a plain `start` waits for the started job, and a first
  full mirror is allowed up to an hour (`TimeoutStartSec=3600`). Blocking would
  hold the resume transaction open for the whole sync; `--no-block` only
  enqueues the job and returns, so the hook finishes in milliseconds, the wake
  is never perceptibly delayed, and the sync runs detached as its own job with
  its own journal, timeout and pass/fail (`journalctl -u wall-sync`). Same
  unit, same code path as boot and on-demand — no second entry point to drift.
- **The Wi-Fi race, handled where ordering cannot reach:**
  `network-online.target` was reached at boot and is NOT re-evaluated on
  resume, while re-association takes a few seconds after wake — so no unit
  ordering can cover it. `wall-sync.sh` therefore gained a short **bounded**
  wait before the real mount: `nm-online -q --timeout=30` (network-manager is
  already in the wall image), **non-fatal in every branch** — success proceeds
  silently, timeout warns and proceeds, a missing `nm-online` warns and
  proceeds. The cifs mount remains the loud arbiter, and the documented retry
  is the on-demand command. Boot/on-demand runs lose nothing (`nm-online`
  returns immediately when the network is already up); the bench-hook path
  skips the wait entirely (it touches no network).
- **Only `suspend.target`** — the panel's one sleep path is `systemctl suspend`
  from `wall-sleep.sh`; no code path can reach hibernate/hybrid-sleep, so those
  targets are deliberately not listed rather than cargo-culted in.
- **Wiring, consistent with wall-sync's own install:** the wall `user-data`
  late-commands cp the unit and `systemctl enable` it (enabling is what plants
  the `suspend.target.wants` symlink — an installed-but-disabled hook never
  fires); `wall-firstboot.sh` step 8 re-enables it idempotently and warns, with
  the consequence stated, if the file is missing. `validate_config.py`'s
  autoinstall-file list gained the one new file; no knobs were added so the
  knob coverage is unchanged (18 declared, still all accounted for).
- **Docs:** `wall/README.md` (table row + the "no timer" bullet rewritten to
  "a resume DOES sync", with the `--no-block` and Wi-Fi reasoning);
  `WALL-BURN-IN.md` §8's "decide OI-16" checkbox replaced by the two hardware
  proofs (an `rtcwake` suspend→wake with `journalctl -u wall-sync-resume -b`
  showing the sync fired after the wake, and the same via the real
  `wall-sleep.sh start` nightly path); the OI-16 item above → RESOLVED.

**RAN FOR REAL (this machine + WSL, 2026-07-29)**

- **`systemd-analyze verify` on the new unit** (systemd 255 in WSL): exit 0.
  (The one warning — "marked executable" — is an artifact of the drvfs copy,
  not of the unit.)
- **`bash -n`** clean on both edited scripts (`wall-sync.sh`,
  `wall-firstboot.sh`).
- **The edited `wall-sync.sh` re-run end-to-end in WSL Ubuntu** through the
  `MEDIA_SOURCE_OVERRIDE` bench hook under `env -i`: mirror + both manifests
  regenerated, `python3 -m json.tool` valid on both, exit 0.
- **All three nm-online branches exercised for real** with a stub on `PATH`:
  called with exactly `-q --timeout=30`; exit-0 proceeds silently; exit-1
  prints the "network still not up after 30s" warning and proceeds; absent
  binary prints the skip warning. In each case the run still died LOUDLY at the
  refused cifs mount (exit 1, cache untouched) — the arbiter is unchanged.
- **`python scripts/check.py` — G1 PASS**: config-validate (the new file
  present, 18 wall knobs, both `user-data` files still parse as YAML),
  registry-integrity `SN=13 SR=17 orphans=24 integrity=0`, doc-navigability
  11 docs / 48 links / 0 broken.

**NOT run (honest gap)**

- **No real suspend/resume fired this hook.** WSL cannot suspend, so
  `WantedBy=suspend.target` actually triggering at wake — the entire point —
  is asserted from the documented systemd semantics, not observed. Burn-in §8
  now carries the two-step hardware proof.
- **No real Wi-Fi re-association raced the sync.** The 30 s `nm-online` cap is
  a judgment, not a measurement; if this radio routinely takes longer, burn-in
  says to report it rather than tune silently.
- **`--no-block`'s "wake never delayed" claim** is by construction (enqueue and
  return), verified only as unit semantics — the perceptibility check is on the
  burn-in list, on the panel.
- **shellcheck is still not installed** on this machine or in the WSL Ubuntu —
  not run, not claimed.

**For the Owner / next gate**

- OI-16 is **closed**: boot + resume + on demand, still no timer. The only
  remainders are hardware (burn-in §8): see the hook fire after a real
  suspend→wake, and once from the real nightly `wall-sleep.sh` path.
- Freshness statement, updated and honest: the cache is now as fresh as the
  **last wake or boot**, whichever is later — plus whatever `sudo systemctl
  start wall-sync.service` was run in between. Music added during the panel's
  awake hours still needs the on-demand command (or waits for tomorrow's
  resume); that is the shape of option (a), stated rather than hidden.

---

### 2026-07-30 — Owner-directed automation sweep (Personal open-items A9) + feed-transport defect fix

Four changes landed this pass, driven by the Owner's ruling that box/LAN-local
configuration should self-configure wherever the trust model allows:

- **`FINANCE_ACTUAL_SYNC_ID` is now optional** (`stack/.env.example`): empty =
  finance-auditor auto-discovers the sole budget file at startup (its own repo
  change, `getBudgets()`/`cloudFileId` verified against the pinned
  `@actual-app/api` 26.7.0 types); several files = fatal naming them.
- **Backup feeder transport fixed** (`stack/backup/common.sh feed_naglight` +
  `backup.env.example`): the shipped example URL pointed at the public
  tracker route, which oauth2-proxy would bounce and whose `X-Forwarded-User`
  it would overwrite — and the tracker is deliberately bridge-only (D2), so a
  host-side curl cannot reach it at all. New `NAGLIGHT_FEED_CONTAINER` knob
  (default `tracker`): the POST runs inside the tracker container via
  `docker exec` + its busybox wget (the same binary its healthcheck proves)
  against its own loopback. Port stays closed. **Sim-untested**: `bash -n`
  clean; the homehub-sim backup lane is the gate.
- **`provision/provision-actual.sh` (NEW) + firstboot step 5b**: sets the
  dev-PC-minted Actual server password on the un-bootstrapped server
  (`POST /account/bootstrap`, checked via `GET /account/needs-bootstrap`
  first; idempotent — an already-bootstrapped server is a logged no-op, with
  mismatch guidance). Runs inside the `actual` container via
  `docker exec node -e` + fetch (image ships no curl/wget, WI-10.14; password
  via exec env, never argv). **HONEST STATE: the endpoint shape is from
  actual-server source reading, NOT yet exercised against the pinned
  `ACTUAL_IMAGE_TAG` in the sim — that sim run is the acceptance gate for
  this script.** `bash -n` clean.
- **`provision/list-tracker-users.sh` (NEW, read-only)**: prints the per-user
  dir names (= Google `sub` values) under the tracker volume so the Owner can
  fill `NAGLIGHT_USER`/`PANEL_USER_SUB` without spelunking. Deliberately NOT
  auto-discovery for `PANEL_USER_SUB` — that is the unauthenticated identity
  the kiosk injects (security-critical per the Caddyfile banner); the ruling
  is the human matches sub → person. `bash -n` clean.

Companion changes in `Personal\homelab\deploy\` (same pass): basic-auth
plaintexts + Technitium/Actual server passwords are now machine-minted
(`GeneratedPassword`), the two Caddy bcrypt hashes derive automatically at
prep time (WSL python3-bcrypt — the `docker run caddy hash-password`
instruction printed here was unrunnable on the dev PC, no docker), and a new
`Show-DeploySecret.ps1` reads one key back for browser prompts.

---

### 2026-08-01 — dozzle + uptime-kuma healthchecks fixed (V3_GATE_HANDOFF §5 item 1)

The last two never-passable healthchecks — the same class of bug the homehub-sim
caught for tracker/oauth2-proxy/technitium/actual in WI-10.14, but in the aux
containers the V1 sim never ran (LAN_IP binds), so nothing had ever executed
them. Both sat permanently red. **Verified against the PINNED images, not
assumed** — `amir20/dozzle:v8.14.12` and `louislam/uptime-kuma:1.23.17`:

- **uptime-kuma** ships **no wget** (it has curl + bash), so
  `CMD-SHELL wget …:3001/` could never run. The image already declares its own
  `HEALTHCHECK CMD-SHELL extra/healthcheck` — a 6.8 MB compiled Go binary at
  `/app/extra/healthcheck` (source `extra/healthcheck.go` sits beside it) that
  GETs `http://127.0.0.1:3001` and exits 0 on 200. Now
  `test: ["CMD", "/app/extra/healthcheck"]` — the image's own probe, absolute
  path so it does not depend on WorkingDir, keeping this stack's 30s cadence
  rather than the image's slower 60s/180s defaults.
- **dozzle** is **distroless** — no `/bin/sh`, so *any* `CMD-SHELL` form is
  unrunnable. Unlike kuma it declares **no HEALTHCHECK of its own**, so one had
  to be supplied: the binary ships a `healthcheck` subcommand ("checks if the
  server is running") that requests its own `/healthcheck` endpoint. Now
  `test: ["CMD", "/dozzle", "healthcheck"]` — exec form is mandatory here.
  Bonus: the subcommand reads the same `DOZZLE_ADDR` config as the server, so it
  follows the listen port instead of hardcoding 8080.

**RAN FOR REAL (WSL, podman 5.3.1 — this dev PC no longer has the Docker Engine
of WI-10.13):** pulled both pinned images; confirmed the missing/​present tooling
above by exec; ran each probe as a **container-runtime healthcheck**
(`podman healthcheck run`) → **both report healthy**, and unhealthy when the
service is down (dozzle's negative case checked with no server running).
`scripts/validate_config.py` → ALL CONFIG CHECKS PASSED. Compose YAML re-parsed
in a throwaway python container (the validator's own YAML step SKIPs here — no
PyYAML on this interpreter): 27 services, both `test:` arrays as intended.

Two test artifacts worth recording so the next person does not re-chase them:
**dozzle exits 1 without a docker socket**, so a socket-less test container is
dead, not unhealthy — the passing run mounts the socket like production does;
and podman's `--health-cmd` does **not** parse a JSON-array string (it stored
`[["CMD",…]]` and tried to exec that literally) — its `CMD …` prefix form is the
CLI equivalent of compose's `test:` list. Neither affects the compose file.

**Also checked, and NOT broken:** `ntfy` v2.25.0 — its `CMD-SHELL wget …
/v1/health` probe is fine (image has wget + sh; ran it, `{"healthy":true}`).
Worth confirming because the 2026-07-29 telemetry ruling made ntfy load-bearing
for the Kuma→ntfy notifier. `caddy` was already proven healthy in the V1 gate.
`ddns` defines no healthcheck by design. That closes the audit of every
healthcheck in the stack.

Still hardware-gated: these two aux images remain otherwise first-validated at
V3 boot / burn-in (image *behaviour* under the real LAN_IP binds is unchanged by
this fix).

---

### DRIVER — G1 — Round 1 — 2026-08-01 (TIER-2 PROMOTED TO THE HOMEHUB DEFAULT SET — SR-012)

Owner's call: `immich` (+`immich-ml`), `jellyfin` and `finance-auditor` become
the **default** opt-in set for the AWOW image, opt-out-able. Nothing is promoted
to core here — `docker-compose.yml` still ships every tier-2 service profiled
OFF and `.env.example` still enables none. The image's set lives in Personal's
`config.homehub.psd1`, so the public default is untouched and disabling one is
still a word removed from `COMPOSE_PROFILES` + `docker compose up -d
--remove-orphans`.

**Three defects the change surfaced — all latent, none introduced by it:**

1. **`MEDIA_ROOT` was ratified and never wired.** D-W8/OI-7b settled it on
   2026-07-29 (`/srv/library/NonDocs/Media`); the generator emitted
   `media-root.txt` saying "nothing consumes this file until then", and the
   materialised `.env` kept the template's `/srv/media` — a directory on the
   119 GB system disk that no fstab line mounts. Every media profile would have
   served an empty library and written photos to the wrong drive. Wired now
   (Personal side); this closes the pathing half of **OI-7(b)**.

2. **Drives mounted AFTER `docker compose up -d`** (firstboot step 5c vs step 4).
   Docker creates a missing bind-mount source itself, on whatever filesystem is
   present at container-start time; the ntfs3 mount then lands on top and
   **shadows** it. The container keeps writing to an invisible directory on the
   system disk while every read through the share sees an empty library — silent
   until the system disk fills. Harmless while nothing bind-mounted inside
   `/srv/library`; live the moment a media profile is on. Mounting moved to a new
   **step 3b**, ahead of compose. The comment block at 5c already argued exactly
   this ordering for Samba — it just had not been applied to containers.

3. **`devices: /dev/dri` cannot live in a shared compose file.** A device entry
   for an absent node fails container *creation*, and compose reports that as a
   failed `up` for the whole run — so the file as written took the entire stack
   down on any box without an iGPU, the V3 gate VM included. New
   `provision-compose-overrides.sh` (**step 3c**) generates
   `docker-compose.override.yml` with the device when `/dev/dri` exists and
   removes it when it does not; compose auto-loads that filename, so a later
   manual `docker compose up -d` over SSH behaves the same. It refuses to touch
   an override lacking its generated header.

**Also in this pass:**
- `JELLYFIN_LIBRARY_DIR` — hand Jellyfin a subtree instead of the whole media
  root (empty = whole root, the generic default). The AWOW gets
  `NonDocs/Media/Movies` per storage-map §3 row 3; music stays Navidrome's and
  the panel's. In-container path is always `/media`.
- `IMMICH_ML_MEM_LIMIT` — the tier-2 banner's "or cap memory", made real. On
  7.6 GiB usable the ML container is the one that can OOM the box; capped, the
  kernel kills it rather than picking a victim from the core stack, and compose
  restarts it. AWOW starts at `2g`.
- `SIM_ENV_OVERRIDES` (`vmtest/lib/common.sh`) — lets the V3 gate boot a real
  image's profile set without changing `.env.example` or staging real secrets.
  Fails the build on a key that is not already a knob, so a typo cannot make the
  gate silently test the default set and report success.
- Docs: tier-2 caveats gained the mount-ordering rule, the case-sensitivity trap
  (`Music` ≠ `music` on ntfs3/ext4 — docker creates an empty sibling, no error),
  and the hardware-passthrough rule.

**Verified (WSL, Docker 29.6.1 — the Engine is back on this dev PC):** all five
new pinned tags resolve on the registry (immich-server/ML `v3.0.2`, immich
postgres `14-vectorchord0.4.3-pgvectors0.2.0`, `valkey:9`, `jellyfin:10.10.7`);
`docker compose config` against the real materialised homehub `.env` resolves 15
services with the bind at `/srv/library/NonDocs/Media/Movies -> /media` read-only
and **no** `devices:` key; the same file against `.env.example` still falls back
to `/srv/media` with no `mem_limit`, i.e. the public default is byte-for-byte
unchanged in behaviour. `scripts/check.sh` → PASS (config-validate,
registry-integrity, doc-navigability).

**Owner-carried risk, recorded not resolved** (Personal `open-items.md` **A17**):
Finance-Auditor is enabled *before* its own G-Release/G-Final, and its tracker
feed cannot authenticate on this box — D3's multi-user tracker wants an
`X-Forwarded-User` its feed client does not send, so the daily status post is
lost while the audit runs fine. FA carries no healthcheck *because* that post is
its liveness signal, so this fails silently and looks healthy. Immich's photo
store also has no `arch-` row in storage-map §4b, where absence-of-row is the
documented way to say "not backed up".

### DRIVER — G1 — Round 1 — 2026-08-01 (V3 GATE RE-RUN — booted, and it found three more)

Ran the full gate against the tier-2 default set: repacked ISO (5.59 GB, 15
baked images / 2473 MB payload), zero-keypress boot, hands-off install, first
boot, verified over SSH. **`homehub-firstboot` → SUCCESS.**

| Service | Result |
|---|---|
| technitium / caddy / actual / tracker | **healthy** (the four gate criteria) |
| jellyfin | **healthy**, `/health` → 200 |
| immich-server | **healthy**, `/api/server/ping` → 200 — but see (1) |
| immich-db / immich-machine-learning | **healthy** |
| ntfy / dozzle / uptime-kuma | healthy |
| oauth2-proxy / immich-redis | Up, no healthcheck by design |
| ddns | unhealthy — expected, SIM Cloudflare token |
| finance-auditor | **restart loop** — see (2) and (3) |

Bind path resolved to `/srv/library/NonDocs/Media/Movies -> /media` read-only;
`IMMICH_ML_MEM_LIMIT` landed as exactly 2147483648 bytes; step 3b mounted before
compose; step 3c wrote the override. **Memory with all 15 up: 2.4 GiB used of
7.8, 5.3 GiB available** — the 8 GB budget holds with room, though the AWOW will
also be serving Samba and running backups.

**Three findings, none of them the thing the run was aimed at:**

1. **`/dev/dri` EXISTS in Hyper-V** — `hyperv_drm` publishes `card1` (no
   `renderD*`). This repo's compose comment asserted the opposite ("container
   creation FAILS ... e.g. a Hyper-V test VM"). Corrected in three places. The
   override is still right — it just means the gate VM does **not** exercise the
   no-device branch, which is covered by direct tests instead.

2. **A stale locally-built image was baked into the ISO and nothing noticed.**
   `finance-auditor:local` in the payload was built 2026-07-11 — **19 days older
   than its repo HEAD**, from before the A8 auto-discovery work — and crash-
   looped on `ACTUAL_SYNC_ID is required`, a knob the current source does not
   require. `naglight:local` was stale too (missing 75b3e3a, the `/api/feed`
   severity colour lane), which means **the 2026-07-31 gate that PASSED was also
   running a stale tracker.** Root cause: a `*:local` image has no registry and
   no version in its tag, and every resolver treats "present" as "done" —
   `ensure-local-images.sh` skips it, `export-images.sh` saves it.
   **Fixed, and the fix took two passes.** `ensure-local-images.sh` now stamps
   `homehub.source.revision` at build time and `export-images.sh` refuses to bake
   an image whose stamp ≠ sibling HEAD (unstamped → loud warning, dirty tree →
   note, `ALLOW_STALE_LOCAL=1` to override). The **first** cut compared
   `.Created` against the sibling's HEAD date and was WRONG: a cache-identical
   rebuild reuses the image record and keeps its original `.Created`, so a
   just-rebuilt image still reported stale. Commit shas are exact; timestamps
   are not. A second layer of the same bug: `export-images.sh` skipped re-saving
   a tar that already existed — fine when the tag pins content, useless for
   `*:local`, so those are now re-saved every run.

3. **finance-auditor cannot start on a fresh box, by design, and will restart
   forever.** With the current image the error becomes the intended one:
   `ACTUAL_SYNC_ID is unset and the server has 0 budget files — cannot
   auto-pick`. Auto-discovery works; there is simply nothing to discover until
   somebody creates a budget in Actual's UI. `provision-actual.sh` sets the
   server password but no budget. So on the real AWOW this profile will sit in
   `restart: unless-stopped` backoff from first boot until that manual step
   happens — visible in `compose ps` and Uptime-Kuma as a broken service.
   Recorded, not fixed: the durable answer is FA treating "no budget yet" as
   wait-and-retry rather than fatal.

**Method note for the next session:** `Get-VMNetworkAdapter | IPAddresses` never
reported an address for this guest — Ubuntu Server does not run the Hyper-V KVP
daemon by default — so an automated runner must read the IP from the console
thumbnail or scan the Default Switch subnet, not from the Hyper-V integration
data. Verification was done over SSH from the host (WSL2 cannot route to the
Default Switch subnet; use Windows-side `ssh`/`scp`). The `hub` account is in the
`docker` group, so none of the verification needs `sudo`.

### DRIVER — G1 — Round 1 — 2026-08-01 (BACKUP DRIVE: FALSE-GREEN CLOSED — Owner question)

> **Historical design, superseded 2026-09-08 by SR-018.** The separate
> `library-mounted` and `backup-drive-mounted` panel lanes and the latter's
> timer/service are retired. Backup-target mount/identity checks remain internal
> preflight safeguards; one `/srv/library` + Samba monitor owns `shareHealth`.

The Owner asked whether a disconnected **backup** drive produced a NagLight
report, or whether the only check was "did the backup run". Answer: neither, and
the gap was worse than unreported.

There were exactly two check ids in the system — `library-mounted`
(`samba/library-guard.sh`, `/srv/library`, on a 10-minute timer AND as `root
preexec` on every Samba connect) and `backup` (posted by a run). The backup
drive had **no presence check at all**: the only thing that ever looked at it was
the 03:30 run.

**And the run could not tell an absent drive from an empty directory.** The
generated fstab uses `nofail` (mandatory — a missing USB disk must not hold up
`local-fs.target` on a headless box), so with the drive unplugged
`BACKUP_TARGET=/mnt/backup-drive` is an ordinary empty directory on the system
disk. `mkdir -p "$RUN_DIR"` succeeded there, rsync copied into it, verification
passed (the files genuinely were present), retention pruned, and step 6 posted
**`ok=true`** — a green backup lane writing the household's backups to the 119 GB
system disk until it filled. Grep confirmed no `mountpoint`/`findmnt`/mountinfo
check against `BACKUP_TARGET` and no `RequiresMountsFor=` on the unit.

That is the exact silent-green shape `library-guard.sh`'s own header forbids for
the library. The guard had simply never been pointed at the second drive, while
`Generate-FromStorageMap.ps1` emits fstab lines for both.

**Fixed, in two halves:**
- **backup.sh step 0 — target preflight.** Refuses to run unless `BACKUP_TARGET`
  is a real mountpoint, and refuses on a `ro` mount (ntfs3's dirty-bit
  fallback). Posts `ok=false` first, exits 1. Placed BEFORE the `mkdir`, which is
  the whole point. NOT implemented as `RequiresMountsFor=`: systemd would refuse
  to start the unit, so nothing would reach NagLight at all — an unreported
  non-run is worse than a red one. Escape hatch `BACKUP_TARGET_REQUIRE_MOUNT=false`
  for a target that is deliberately a plain directory, which then warns loudly
  every run.
- **`homehub-backup-drive-health.timer`** — check id `backup-drive-mounted`, the
  twin of `library-mounted`, every 10 minutes. Reuses `library-guard.sh` via its
  existing `--library` plus a new `--label` (so a red check names the right
  drive); the library's own wording and check id are byte-identical to before.
  The unit reads `BACKUP_TARGET` out of `backup.env` rather than hardcoding a
  site path, and no-ops cleanly on an unprovisioned/sim box.

**Why a 10-minute cadence is safe on a parked drive:** the only probe is
`/proc/self/mountinfo`, a kernel pseudo-file. The answer comes from the VFS mount
table and **no request reaches the device**, so the check cannot wake a
spun-down disk — `df`/`stat`/`ls`/touch-tests all can, and none are used. That is
what makes this compatible with WI-10.10's `hdparm -S` policy. Factored into
`common.sh mount_options_for` so backup.sh and the guard share one implementation.

**Verified on the running V3 VM**, not just locally: units installed and enabled,
timer registered and firing; report **UNHEALTHY** with the path unmounted →
**healthy** after mounting a tmpfs there → UNHEALTHY again after unmount, with
`library-mounted` unaffected throughout; `backup.sh` preflight passes on the
mounted path, and on the unmounted one exits **1** with nothing created under the
target. Also unit-tested in WSL: the three preflight branches, the guard's
label/check-id wiring, and the unit's `BACKUP_TARGET` parsing (inline comment,
quoted value, missing file, unset key).

*Method note:* the VM's Default Switch lease moved mid-session (WSL recreated its
virtual network on a new range), so the gate VM is now at a different address —
find it by scanning the current `vEthernet (Default Switch)` subnet for port 22
rather than trusting a recorded IP.

### DRIVER — G1 — Round 1 — 2026-08-01 (MOUNT BY LABEL, VERIFY BY SERIAL — three-state drive health)

Owner's requirement: run the first days of service on **plain flash drives**, to
prove the backup, the mounts and the shares before 12 TB of real disk is
committed to them — and have that state read as **yellow**, not green, until the
real drives go in.

That is not achievable with one identifier. A by-id serial is unforgeable but a
stand-in can never carry it; a label a stand-in CAN carry proves nothing about
which disk answered to it. So the two are now split:

- **fstab mounts by `LABEL=`** (`Library`, `PriBackup`), fstype **`auto`** —
  flash drives are usually exFAT/FAT32, and `uid`/`gid`/`umask` are honoured by
  ntfs3, exfat and vfat alike, so the ownership Samba depends on is identical
  whichever turns up.
- **`drive-identity.conf`** (new generator emission) carries the expected by-id
  serial per mountpoint, and `library-guard.sh` asserts it separately.

**Three states replace the old boolean:** `green` = mounted rw + expected serial ·
`yellow` = mounted rw, right label, **wrong disk** ("stand-in drive") · `red` =
not mounted or read-only. Posted via NagLight's severity lane (`color`, one of
green|yellow|orange|red) rather than `ok`, because a boolean cannot say "working,
but on the wrong disk" — which is the entire state this exists to surface. Note
that lane arrived in NagLight `75b3e3a`, the commit the stale `naglight:local`
was missing: this only works because that image got rebuilt earlier today.

**The identity check does no disk I/O.** mountinfo field 3 gives the mounted
device's major:minor; `/sys/class/block/*/dev` and `readlink` on
`/dev/disk/by-id/*` resolve it to a stable name. Nothing opens the block device,
so the 10-minute cadence still cannot wake a parked drive — `blkid`/`lsblk -f`
would have read the superblock and could have.

**Tested on real block devices**, not mocks: two loopback filesystems both
labelled `Library` with distinct fabricated by-id names → red with neither
mounted, **yellow** with the stand-in mounted (naming both the expected and the
actual serial), **green** with the real one, and green-with-the-gap-named when
no identity file exists. Degradation is graceful throughout: no
`drive-identity.conf` = presence-only reporting, and the check says so in its own
note rather than going quiet.

`backup.sh` logs a NOTICE when the archive is landing on a stand-in but does NOT
refuse — proving the backup on a cheap disk is the point of the period. The
composite signal is the honest one: `backup` green (the run worked) +
`backup-drive-mounted` yellow (on a substitute).

**Flagged to the Owner, unresolved** (Personal `storage-map.md` §1, open-items
**A23**): the map's `dev-pc` row lists its volume labels as `Library`,
`PriBackup`, `LPBackup` and says they are "**not** the hub's main-library /
backup-drive — do not conflate them" — but those are now exactly the two labels
the hub mounts by. Either they are the same physical drives and that note is
wrong, or two volumes share each name and label mounting is ambiguous. Also
carried: the legacy FileBackup PowerShell on the dev PC matches volumes with
`-like "*<label>*"`, so a stand-in stick labelled `Library` must not be plugged
into the dev PC while it exists.

### DRIVER — G1 — Round 1 — 2026-08-01 (THE DRIVE CHECKS HAD NO CHECK DEFINITIONS)

Owner asked which project owns drive-presence reporting. Four do, and the fourth
link was missing:

1. **this repo** — sensing + reporting (`stack/samba/library-guard.sh`, both
   `homehub-*-health` units, `backup/common.sh`, `backup.sh` step 0, firstboot)
2. **Personal `homelab/deploy`** — the facts (storage-map §1 → generator →
   `library-mounts.fstab` + `drive-identity.conf` → USB)
3. **NagLight** — transport + rendering (`/api/feed`, the severity lane, auth)
4. **Personal `tracker/definitions`** — the check registry ← **was empty of these**

`/api/feed` rejects a POST whose `check` matches no automated item with
`400 unknown feeder check id`, and only `backup`, `video-stub` and `mc-update`
were ever defined. So **`library-mounted` had been posting into the void since
2026-07-30**, and `backup-drive-mounted` would have too. Silently: the reporter
logs the HTTP code and continues — right, because a reporting failure must not
mask the drive's real state, but the consequence is that a vanished drive would
be red in the journal and ABSENT from the tracker. Fixed Personal-side
(`library-drive-present` / `backup-drive-present`), verified end to end against a
running tracker: an undefined id 400s, both new ids record their colour report,
and the day's ambient colour follows.

**`tracker healthy` is one of the four V3 gate criteria and it does not mean the
tracker works.** The container healthcheck probes `/healthz`, which is
deliberately identity-free and data-free (SR-040 — a probe must not provision a
phantom user dir). Measured in the gate VM: tracker `healthy` for hours with an
EMPTY `/data`, its nightly run failing every night —
`scheduler: nightly run failed … reading definitions dir "/data/definitions"`.
Nothing in the gate noticed, because nothing looks. Recorded as Personal
**A24(ii)**; changing it is the Owner's call, since any data-bearing probe makes
a freshly-imaged box unhealthy until definitions exist.

Third, NagLight-side (**A24(iii)**): with `TRACKER_COMMIT=true` and a `/data`
that is not a git repo, a feed POST **records the report and then returns 500**
because the git commit fails afterwards. A feeder reads 500 as failure and, under
never-silent-green, reports red or retries — for data that was stored. The
homehub image is not exposed (D3 forces `TRACKER_COMMIT=false`), but the sim runs
in exactly that configuration, which is how it was found.

### DRIVER — G1 — Round 1 — 2026-08-02 (E0's WIRING HALF — THE WALL IMAGE NOW EXISTS)

`grep -rln "wall" vmtest/*.sh vmtest/*.ps1` returned **nothing** before this
session. The repo had a complete, sim-validated wall autoinstall
(`stack/autoinstall/wall/`, SR-016/017) and no way to turn it into a bootable
image, and OfficeWallNaglight had — since 2026-08-02, PKG-1 — an artifact that
**nothing took**. Two halves of E0, neither connected to the other.

**What landed.**

1. **`vmtest/build-wall-seed.sh`** — the second image target's **seed** ISO
   (~112 MB; it carries the 111 MB shell tarball). Not a bootable ISO: it is the
   light path's CIDATA seed, attached as a second DVD beside the stock Ubuntu
   ISO, exactly like the hub's. The heavier one-ISO repack path is hub-only.
2. **Shared mechanism, separate renderers — deliberately.** What both targets share — CIDATA discovery,
   the ephemeral SSH key, the repo-into-payload copy, the assert-every-
   substitution discipline — moved into `lib/common.sh` helpers that
   `build-seed.sh` now calls too. `render_seed_tree` and `render_wall_seed_tree`
   stay separate implementations and a `--wall` flag was rejected deliberately:
   one code path with two sets of load-bearing assertions is how one of them
   quietly stops biting. **The hub did change**, in three ways, and "no behaviour
   change" would have been false: its payload gained `wall-site/`, its firstboot
   gained step 3d, and its payload copy now carries **tracked files only** (see
   the review section below — that one is a fix, not a side effect).
3. **The panel installs the app.** Wall `user-data` late-command 3b untars the
   payload's shell tarball into `/opt/wall-panel/app`. **As root, with `tar`** —
   `chrome-sandbox` must arrive `4755 root:root` or Electron refuses to start
   (24.04's `apparmor_restrict_unprivileged_userns=1` closed the alternative),
   and a `cp`/unzip/rsync drops the bit silently.
4. **The hub serves the renderer.** `firstboot.sh` step 3d unpacks the site
   tarball into `stack/wall-shell/` before compose up, so the kiosk site has a
   real document root instead of 404ing at `/`. Two payloads exist at all
   because NagLight sends no CORS headers; both carry the same source commit, so
   a mismatched deploy is now visible with `cat`.
5. **A SIM `wall.env`**, installed 0600 by late-command 4a — the same `site/`
   seam shape the hub image uses.

**The `ldd` check, moved to where it can still be acted on.** A missing shared
library is a **black wall**: Electron exits before painting, `wall-kiosk.sh`
restarts it every 3 s, and `[ -x ]` stays true so the NOT INSTALLED screen never
fires either. `stack/autoinstall/wall/electron-runtime-deps.tsv` maps every
soname the shipped binary declares to its noble package (resolved with `dpkg -S`
and `apt-cache policy`, not guessed — note the `t64` renames), the builder reads
`DT_NEEDED` out of the artifact it is about to bake and refuses to build if
anything is unmapped or uninstalled, and `validate_config.py` keeps the table and
the `packages:` list in step on every commit.

**Measured, not reasoned** (a bare `ubuntu:24.04` with exactly this image's
package list, plus the installer's own late-commands run verbatim against a fake
`/target`):

- the artifact unpacks, `chrome-sandbox` is `4755 root:root`, `[ -x
  /opt/wall-panel/app/wall-shell ]` is **true**;
- `ldd` on the Electron runtime resolves **everything** — the package list is
  sufficient, not merely declared (without it, it stops at `libnspr4.so`, which
  is exactly where PKG-1's session left it);
- run as an unprivileged user the wrapper execs and Electron reaches **Ozone
  platform init**, stopping only for want of a display.

**Twelve refusals, and a suite that re-runs them.** They were exercised by hand
first, and an adversarial review made the obvious objection: a transcript in this
ledger is not a check. `vmtest/test-wall-builder.sh` now runs all twelve — the
artifact gate (absent, `-dirty`, ambiguous, and the documented
`ALLOW_MISSING_SHELL=1` way past it), the dependency gate, the five production
guards, and the `--clean` brake — in about a minute, and reports skips as skips
rather than quietly shrinking. `vmtest/test-wall-artifact.sh` does the same for
the `ldd` claim below.

**Containment, the hub's rule applied to the panel.** The sim rewrites the disk
match to `model: Virtual_Disk` and refuses to build if that `sed` no-ops or if
the panel's real `KINGSTON` model survives in an active setting. A sim ISO
written to a USB stick cannot wipe the real panel. (The first version of that
guard was a plain substring grep and refused a good build — the user-data
*explains* the pin in a comment naming the disk. Anchored to an active YAML
setting, which is the same lesson `user-data.filled`'s guards learned on
2026-07-30.)

**NOT PROVEN, and it is the whole of the next gate.** Nobody has booted the wall
ISO, so nothing below the build has ever executed: Subiquity has not run, `apt`
has not installed those packages on a real system, the late-commands have not
run under curtin, no systemd unit has been enabled, `getty@tty1` has not
autologged anyone in, and `cage` has never started. What IS verified is the
floor underneath all of that — the archive's contents and modes, the extraction
as root, the predicate, `ldd` against the real package list, and Electron
reaching Ozone init in a container. An earlier draft of this entry said
"everything short of a display is verified", which was the largest false-green
sentence in it. Nothing has watched Electron come up under `cage` on a display; the
container run above stops at "Missing X server or $DISPLAY", and it did so via
X11 — with no `WAYLAND_DISPLAY` set, `--ozone-platform-hint=auto` chose X11,
which is consistent with the concern that made PKG-1's wrapper force
`--ozone-platform=wayland` outright. Also unproven: the panel's Wi-Fi path,
which the sim **removes** (Hyper-V cannot emulate a radio and the installer needs
apt, so the wall's `wifis:` block is swapped for the hub's `e*` ethernet
matcher). A gate run this way proves the kiosk/identity/render path and nothing
about `macaddress: permanent`, powersave-off, or the DHCP reservation the `/32`
allow-list is keyed to. Those stay hardware-only (C7).

**Assumptions recorded** (AGENTS.md "running unattended"), all four in the SIM
`wall.env` and all four reversible with `WALL_ENV_OVERRIDES`:
`SLEEP_MODE=backlight` (a VM that suspends at 22:00 looks identical to a VM that
died; with no `/sys/class/backlight` in a guest it is additionally inert, so the
screen stays up for a capture); `--disable-gpu` on `WALL_APP_CMD` (`hyperv_drm`
gives `card1` with no `renderD*`); Wi-Fi values filled but unused;
`WALL_DISABLE_INPUT` empty (quirk 3 would disable the synthetic keyboard and
mouse — the console needed for the GRUB edit).

**TWO ADVERSARIAL PASSES, and they were worth more than the build was.**
Read-only, OpenAI CLI: a diff review and a claim-by-claim refutation attempt.
Twelve findings, all triaged. The six that were real are fixed and are in the
commit; three of them were defects this session introduced, and three were
older:

1. **The payload carried gitignored secrets.** `copy_repo_into_payload` archived
   the whole worktree — so `stack/provision/.token` (64 bytes, non-expiring) was
   found *inside a built payload*, and any dev box that has run the real stack
   also has `stack/.env` sitting there. Pre-dates this session and shipped on
   every hub ISO ever built here. Now `git ls-files`: tracked files only.
2. **A freshly imaged panel did not enter the kiosk on its first boot.**
   `wall-firstboot` writes the tty1 autologin drop-in but runs
   `After=network-online.target`, by which time `getty@tty1` is already up —
   and `daemon-reload` does not restart a running unit. The panel showed a login
   prompt on a machine whose account password is **locked**. It would have read
   as a failed image at the A19 gate.
3. **A real Wi-Fi PSK could break or corrupt first boot.** SSID and PSK went
   straight into a `sed` replacement: a `|` aborts firstboot *before* the
   autologin is installed; an `&` silently writes the wrong network. Both
   unreachable on a Wi-Fi-only box. The sim's values contain neither character,
   so no amount of VM testing would have found it.
4. **Four false greens in this session's own checks** — the worst kind, since
   each one passes hardest when it can see least: `readelf` failing inside a
   here-doc produced an empty dependency list and logged OK; `-dirty` was judged
   from the filename while the message claimed to have read `build-info.json`;
   rows marked `bundled` were taken on trust; and the disk pin was a `grep`, so
   a decoy `model: Virtual_Disk` anywhere in the document satisfied it (now
   parsed structurally: `autoinstall.storage.layout.match` must be *exactly*
   that mapping for a sim, and must *not* be it for production).
5. **`tar … | grep -q` under `pipefail`** reports failure when grep finds its
   match early and SIGPIPEs tar — so the check failed on a *correct* site
   tarball. Found by it actually happening. Fixed in both places with that shape.
6. **`--clean` was `rm -rf $OUT_DIR`** on an environment variable the README
   tells you to set; `OUT_DIR=/mnt/d … --clean` aimed a recursive delete at a
   drive. It now refuses any directory this builder did not create. The wall
   output also moved to `.out-wall`, a **sibling**: nested under `.out`, an
   ordinary hub `--clean` deleted the wall ISO, its SSH key and its credentials.

The rest were overclaims in the writeup rather than defects, and this entry has
been corrected for them rather than left standing.

**A correction found while writing this, not by the reviewers:** the first draft
refused `WALL_SITE_DIR` outright on the grounds that "Personal's materialiser
covers the hub only". That was **wrong** — `FieldSchema.psd1` registers both
`wall.env` and `user-data.filled` for `-Image wall`, and has since 2026-07-29.
The production seam is built instead, with the hub's guards adapted.

**Open after this.** `config.json` is IF-005's last **shell-configuration** gap
and nobody renders it — it is not the last gap overall; the unbooted ISO is.
There is no production path for a wall image: `WALL_SITE_DIR` is refused because
half a production build — real secrets on a sim-substituted `user-data` — is the
silent downgrade the hub's guards exist to prevent. A real panel is still imaged
by hand.

### DRIVER — G1 — Round 1 — 2026-08-03 (THE WALL ISO WAS BOOTED, AND IT FOUND A REAL ONE)

**First boot of the wall image, ever.** It did not reach the kiosk. It found a
defect that had been latent in **both** images since the light path existed, and
that is worth more than a pass would have been.

**What the boot proved** (all of it new — nothing above the build had ever run):

1. **Subiquity accepts the sim `user-data`.** No parse error, no interactive
   drop-out; the repacked path booted hands-off and the light path needed only
   the documented single GRUB keypress.
2. **The disk pin works in the direction it must.** Subiquity partitioned
   `lvm_volgroup-0` on the *virtual* disk — `model: Virtual_Disk` matched exactly
   what it is meant to match and nothing else.
3. **THE PACKAGE LIST IS REAL.** All 40 `packages:` entries installed on noble,
   including every one of the 23 Electron runtime libraries and all six `t64`
   renames (`libasound2t64`, `libatk1.0-0t64`, `libatk-bridge2.0-0t64`,
   `libatspi2.0-0t64`, `libcups2t64`, `libglib2.0-0t64`). The static check said
   they were declared; apt has now said they exist.
4. **Cost, for §3's planning:** Subiquity installs each `packages:` entry as its
   own `curtin system-install`, so the 23 extra libraries add roughly 20 minutes.
   A wall install is ~45-60 minutes, not the hub's ~20.

**What it found.** The install died at `late-command_9` — the **pre-existing**
"seed `wall.env` from the example" step — unable to read `wall.env.example` out
of a payload that was not there:

> **`/cdrom` is not the seed.** On the REPACKED path `/cdrom` is the combined
> ISO and carries `/deploy-payload`. On the **LIGHT** path — *the one this repo
> recommends by default* — `/cdrom` is the STOCK Ubuntu ISO, which has no such
> directory, and the CIDATA seed that does is mounted only transiently by
> cloud-init to read `user-data`. The payload rode along and **nothing ever read
> it.**

Every V3 gate has used the repacked ISO (2026-08-01: *"repacked ISO (5.59 GB)"*),
so the hub never exercised the light path's payload — and could not have
noticed if it had: `firstboot.sh` logs *"no baked image payload found"* and pulls
from registries instead, which reads as a slow first boot rather than a bug.
**~470 MB of baked container images have been going along for the ride unused.**
The wall cannot degrade that way — its units, its scripts and `wall.env.example`
all live in the payload — so it crashed, four commands downstream of the cause,
naming none of it.

**Diagnosed without a shell**, because Hyper-V needs elevation and none was
available: the installer's `command_N` numbering aligns exactly with the
`late-commands` list, so `command_9` is identifiable as the `wall.env.example`
step, and *both* halves of its `||` are explained only by a missing payload.

**Fixed.** Both images now try `/cdrom`, `/media`, `/run/media/*`, then mount the
CIDATA volume **by label**. The wall FAILS LOUDLY when none of that works; the
hub keeps its documented degraded mode but says so instead of `|| true`. All
three branches were exercised against fakes before rebuilding.
`build-repacked-iso.sh` also gained `--target wall`, so the wall gets the
zero-keypress path — and there `/deploy-payload` is simply present, which is the
branch that has actually been exercised. The repacked wall ISO is built (3.3 GB,
BIOS+UEFI intact, `/nocloud` + `/deploy-payload/wall-app/` verified present).

**STILL NOT PROVEN — the fixed image has not been booted.** `cage` has still
never started, the shell has never painted, and the crash-loop screen and the
`journalctl -t wall-kiosk` tag have still only been exercised against a fake
`cage` in a container. The VM is created and one UAC approval away; OI-17 stays
open, and it is now a *narrower* gap than it was this morning rather than a
closed one.

**A third instance of the same bug, NOT fixed** (hub production path, and
changing it untested in a wall session is exactly what AGENTS.md warns against):
`stack/autoinstall/user-data`'s **site-staging** late-command reads
`/cdrom/deploy-payload/site` with the same assumption and `exit 0`s when it is
absent. On the light path a PRODUCTION hub would therefore install **none** of
its real secrets and come up on `.env.example` values — silently. Same one-line
shape of fix; needs a hub install to verify.

### DRIVER — G1 — Round 1 — 2026-08-03 (§2's GATE CRITERION MET — and three more defects only a real panel could show)

**`journalctl -t wall-kiosk` shows `starting: cage -- /opt/wall-panel/app/wall-shell`.**
That is §2's stated bar, on a real boot, from an image built by the tracked
scripts. The panel does not yet render anything, which is §3's gate, not this
one — but getting here found three defects that no amount of container testing
could have.

**Proven on the panel, in addition to yesterday's list:**

- the payload fix works — `/opt/wall-panel/` carries the repo, `site/wall.env`
  and `wall-app/`, and the **SIM `wall.env` landed** (not the example);
- the artifact installed: `-rwxr-xr-x root root app/wall-shell`, and
  **`chrome-sandbox` is still `4755 root:root`** after NTFS → ISO → tar → ext4;
- `wall-firstboot` reports **`IF-005: ldd resolves every library the Electron
  runtime needs`** — the package list is sufficient on the real machine;
- tty1 autologin works and the kiosk starts on the **first** boot (the
  `getty@tty1` restart fix), captured from `/dev/fb0`;
- the wrapper picks Wayland from a real `WAYLAND_DISPLAY=wayland-0`, and bridges
  `MEDIA_CACHE_DIR` from `WALL_MEDIA_CACHE`;
- Electron runs and reaches `net::ERR_NAME_NOT_RESOLVED` for
  `wall.vmtest.sim.invalid` — **the correct failure**: that host is deliberately
  unresolvable and there is no hub yet.

**Defect 1 — THE FAILURE SCREENS NEVER RENDERED.** Both of them, for the whole
life of `wall-kiosk.sh`. They ran `cage -- /bin/sh -c 'printf …'`, and `cage`
displays exactly one **Wayland client**; a shell running `printf` is not one. It
writes to stdout, cage shows an empty surface, and because cage does the KMS
modeset it *also hides the text console underneath*. Measured both ways — `grim`
inside the session and Hyper-V's thumbnail — uniform black while the message sat
in the journal. **So "a dead panel must be a visible event, not silence" was
false in both directions**, and the crash-loop screen added earlier the same day
inherited the bug. It survived testing because a stand-in `cage` that simply
execs its client makes the text appear on stdout and everything look right; it
needed a compositor to expose. Fixed without any new package —
`wall-kiosk.sh` **is** the tty1 session leader, so its stdout is the console, and
with no compositor running the console is what the panel scans out. **Verified by
capturing `/dev/fb0`: the NOT INSTALLED screen renders, clean and readable, for
the first time ever.**

**Defect 2 — the kiosk could not read its own configuration.**
`/etc/wall-panel/wall.env` is `0600 root:root` (it holds the Wi-Fi PSK); the
kiosk runs as `panel`. `load_env_file` treats unreadable exactly like absent, so
it read **nothing** and every value fell back to a default: the panel logged
`PANEL_URL=https://:8443/` — an empty `WALL_HOST` — and silently dropped the
flags configured in `WALL_APP_CMD`. **On real hardware that is a panel that can
never reach its hub, with nothing anywhere saying why.** Not fixed by loosening
`wall.env`: `wall-firstboot.sh` now renders the four non-secret knobs into
`/etc/wall-panel/kiosk.env` (0644) and the PSK stays exactly where it was.

**Defect 3 — `cage` refuses to start without a GPU.** wlroots requires
`WLR_RENDERER_ALLOW_SOFTWARE=1` when EGL lands on llvmpipe:
`[render/egl.c:320] Software rendering detected`. Hyper-V's `hyperv_drm` gives
`/dev/dri/card1` and **no `renderD*`**, so this is every VM. Set **only** when
there is genuinely no render node — a panel whose iGPU regressed must still fail
loudly rather than quietly cook itself on CPU rendering inside a sealed wall
mount (quirk 6).

**How to see the panel's screen — §3 needs this and the plan's method is
incomplete.** Three capture routes, and they do not show the same thing:

| route | shows | needs |
|---|---|---|
| Hyper-V thumbnail (RGB565 → PNG) | whatever is scanned out, incl. a cage session | **elevation**; VM only |
| `/dev/fb0` (dd + convert) | the **text console** — invisible once cage takes over KMS | ssh + sudo |
| **`grim`** (wlr-screencopy, now installed) | **what cage is actually showing** | ssh + the session's `XDG_RUNTIME_DIR`/`WAYLAND_DISPLAY` |

On **real hardware there is no thumbnail API at all**, so `grim` is the only
route that works on the panel itself:
`sudo -u panel env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 grim /tmp/panel.png`

**NOT PROVEN: the shell has never painted anything.** It starts, runs, and
correctly fails to resolve its origin. Nothing has rendered the tracker's UI on
a panel, and it cannot until a hub serves the kiosk site — that is A19/§3. Also
still unproven: the fixes above are verified **on the running VM** (scripts
pushed and re-run); a clean rebuild proving the IMAGE delivers them has not been
done.

### DRIVER — G1 — Round 1 — 2026-08-03 (OI-19 — THE THIRD `/cdrom`, AND THE FIRST REFUSAL THE HUB HAS)

**Fixed, and NOT verified.** Everything below is static and structural. No ISO
was built, no VM was created, nothing was installed — and the whole defect lives
in a late-command that only Subiquity runs. Read the last paragraph before
quoting any of this as evidence.

**The defect.** `stack/autoinstall/user-data`'s site-staging step read
`/cdrom/deploy-payload/site`, fell back to `/media/...`, and then
`[ -d "$S" ] || exit 0`. That is the same wrong assumption the wall boot
exposed this morning — **`/cdrom` is the boot medium, not the seed** — with a
worse ending: the wall crashed, the hub exits 0. On the light path a PRODUCTION
hub would install **none** of `.env`, `backup.env`, `cifs.creds`,
`samba-users.creds`, `smb.conf.fragment`, `library-mounts.fstab`, and come up on
`.env.example` placeholders. `firstboot.sh` would then log
*"no smb.conf.fragment and no site payload marker — expected ONLY on a
sim/vmtest build"*, which on a real box is a **false statement in the journal**,
not a warning.

**How exposed it actually was, stated exactly** (the honest half): the only
wired production path, `Build-VentoyStick.ps1`, calls `build-repacked-iso.sh` —
and on the repacked ISO `/cdrom` really is the combined image and really does
carry `/deploy-payload`. **No stick ever built has been bitten by this.** What
sits one command away is `SITE_DIR=… bash vmtest/build-seed.sh`, which is an
equally supported way to build a production hub and is the path the README
recommends by default.

**Two halves to the fix.**

1. **The search is gone, not duplicated.** Late-command 3 already tries
   `/cdrom`, `/media`, `/media/*`, `/run/media/*` and then mounts the CIDATA
   volume **by label** — and it copies the WHOLE payload, `site/` included, to
   `/target/opt/homehub/`. So the site step now reads
   `/target/opt/homehub/site` and searches for nothing. One discovery
   mechanism, one place it can rot; a second copy of that loop is how one of
   them quietly stops matching the other. This is exactly the shape the wall
   image already uses (its late-command 4a reads
   `/opt/wall-panel/site/wall.env` rather than searching again).
2. **`exit 0` became a refusal — for production builds only.** The tracked
   user-data carries `BUILD_PROFILE=production`; `render_seed_tree` rewrites it
   to `sim` for a vmtest image and **asserts the rewrite applied**, both
   directions. The safe value is therefore the default and the SIM path is the
   one that has to opt out — a sim build legitimately has no `site/` and must
   stay a clean no-op, while a production image that lost its secrets now halts
   the install saying so. `Materialize-Deploy.ps1` renders `user-data.filled`
   from this very file (`FieldSchema.psd1` Images.homehub), so the marker
   arrives on a real stick for free — and a materialised tree that predates
   this change carries the OLD silent late-command, so the builder refuses it
   by name and tells you to re-run the materialiser.

**A seventh file, which was never installed at all.** `drive-identity.conf` is
staged into `site/` by both `render_seed_tree` and `Build-VentoyStick.ps1`, and
three consumers read `/etc/homehub-samba/drive-identity.conf` (`firstboot.sh`,
`backup.sh` step 0, `library-guard.sh`). No late-command ever copied it there.
The degrade is graceful and says so — "health checks report presence only (a
stand-in drive will read as healthy)" — which is precisely why nobody noticed
that the honest-sounding message was the ONLY outcome available. It is in the
install table now.

**Per-file absence is loud but not fatal, and that line is UNRATIFIED.**
`Build-VentoyStick.ps1` marks `cifs.creds`, `samba-users.creds` and
`drive-identity.conf` `Required=$false`, so halting a household's install over
one of them would refuse legitimate builds. A file that is PRESENT and fails to
install IS fatal. Whether a **missing `.env`** should halt as well is the
Owner's call (A10, above) — not decided here.

**What was run, and its output.** `vmtest/test-hub-seed.sh` is new, in the shape
`test-wall-builder.sh` established: negative paths, refusals that must bite,
skips counted as skips. It builds real sim and production seeds and then
**extracts late-command 4b from the user-data each build produced** and runs it
against a fake `/target` — so what is exercised is the artifact, not a
paraphrase of it.

```
=== the production seam (builder) ===        SITE_DIR w/o user-data.filled refused
                                             a pre-OI-19 user-data.filled refused
=== the SIM build ===                        BUILD_PROFILE=sim; meta-data homehub-vmtest
=== the PRODUCTION build ===                 BUILD_PROFILE=production kept; site/ staged
=== the substitution assertions ===          a user-data that lost the marker fails the build
=== the late-command itself (OI-19) ===      no /cdrom or /media left in it
                                             PRODUCTION + no site/  -> REFUSES (was exit 0)
                                             SIM + no site/         -> clean, loud no-op
                                             PRODUCTION + payload-borne site/ -> all seven
                                                                     files installed 0600
                                             absent OPTIONAL file -> named, not fatal
                                             absent REQUIRED file -> named, not fatal (A10)
hub seed guards: 13 passed, 0 failed, 0 skipped
```

Also green, unchanged: `python scripts/check.py` → PASS (config-validate,
registry-integrity, doc-navigability), `scripts/validate_config.py` → ALL CONFIG
CHECKS PASSED, `vmtest/test-wall-builder.sh` → 12 passed / 0 failed / 0 skipped.
`bash -n` on every shell file touched, plus `bash -n` **and `dash -n`** on all
four inline late-command bodies in both images' user-data, plus a PyYAML parse
of all four autoinstall files.

**NOT PROVEN — and this is the whole of what OI-19 still is.** Nothing here has
installed anything. Subiquity has never executed this late-command; `curtin` has
never mounted a `/target` for it; the CIDATA-by-label mount it now depends on
has been exercised on the WALL image only, and on the repacked path where
`/cdrom` was present anyway. No ISO was built in this session and no VM was
touched. The refusal is proven to fire **in bash, against a directory named
`/target` that this suite created**. OI-19 is FIXED-BUT-UNVERIFIED; it closes on
a hub install, not before.

### DRIVER — G1 — Round 1 — 2026-08-03 (the hub's production meta-data was stamped `homehub-vmtest`)

The wall handoff's §5 item 7, closed. `render_seed_tree` applied **both**
meta-data seds unconditionally, so a `SITE_DIR` (production) build got
`local-hostname: homehub-vmtest` and `instance-id: homehub-vmtest-<ts>` in the
seed it burns onto a real stick. Cosmetic — Subiquity's `identity.hostname` is
what the installed box answers to, and that was always `homehub` — but wrong,
and wrong in the direction that gets quoted back later as evidence ("the seed
says vmtest, so this must be the sim stick"). The wall builder had already
learned this (`render_wall_seed_tree` marks the hostname on the sim path only);
the hub had not, and it was deliberately left alone in a wall session.

**Fixed by reading the answer off the file we just rendered**, rather than by
adding a second constant: `autoinstall_hostname` parses `identity.hostname` out
of the rendered user-data — **structurally, with PyYAML**, because the shipped
user-data explains the hostname choice in a comment four lines above the setting
and any line-based read is one comment edit away from the wrong string — and
both meta-data values are written from it. Sim and production now agree BY
CONSTRUCTION in both modes, and a production `user-data.filled` that named some
other host would carry that name through instead of being overwritten with
either constant.

**The assertion habit is kept, and strengthened.** The old seds were anchored to
the literal source strings (`instance-id: homehub-001`, `local-hostname:
homehub`) and the result was asserted against the literal `homehub-vmtest`. The
new ones rewrite the KEYS and assert the RESULT equals the hostname this build
intends — so a renamed or deleted key in `stack/autoinstall/meta-data` still
fails the build loudly, which is the failure the old assertion existed for.
`autoinstall_hostname` additionally refuses a name that is empty or carries
anything outside `[A-Za-z0-9.-]`, since it is interpolated into a `sed`
replacement.

**Run:** `vmtest/test-hub-seed.sh` → **16 passed, 0 failed, 0 skipped** (the
13 from the OI-19 entry above plus three new: a production seed's
`local-hostname` is `homehub`, its `instance-id` is `homehub-<ts>`, and a
`meta-data` with its `local-hostname:` line deleted fails the build).
`vmtest/test-wall-builder.sh` 12/12 unchanged, `scripts/check.py` PASS,
`bash -n` clean.

**Not proven:** the same thing as everything else on this page — no ISO was
built and nothing was installed. What is asserted is the content of a rendered
`meta-data` file on disk.

### DRIVER — G1 — Round 1 — 2026-08-03 (`config.json` — nothing staged the panel's credentials, and nothing said so)

Personal's `Materialize-Deploy.ps1` started emitting `out\homehub\config.json`
(its commit `bdf9aba`) and **this repo had nowhere to put it.** The hub's
site-file list is hand-maintained in two places — the staging loop in
`vmtest/lib/common.sh` and the install table in `user-data` late-command 4b —
and `config.json` was in neither, so it rode nothing and reached nothing.

**It is a HUB artifact, not a panel one**, and that is the non-obvious part:
`js/config.js` `loadConfig` fetches `./config.json` **relative to the page
origin**, and the origin is Caddy's `{$WALL_HOST}:{$WALL_PORT}` site
(`root * /srv/wall-shell` ← `/opt/homehub/stack/wall-shell/`, filled by
`firstboot.sh` step 3d). The panel's Electron host intercepts `/media/*` and
nothing else, so a copy on the panel's disk would never be read.

**Why the gap was invisible, and would have stayed so:** `loadConfig` never
throws. A 404 or a parse error returns the `js/config.js` DEFAULTS plus a
`console.warn` — on a wall, in a browser nobody has a console for. The panel
comes up looking like it works, with no `FEED_TOKEN` (its feed posts are
unattributed), no heartbeat, and no music credentials. Same family as the
2026-08-03 failure screens: the absence has no symptom.

**Staged in the builder, installed by firstboot — and the ordering is the
design decision.** `config.json` joins `render_seed_tree`'s staging loop, so it
rides the payload like the other site files. It does **not** join late-command
4b: its destination is a **web root**, not `/etc`, and step 3d untars the site
tarball **over** that directory hours later. Of the two options — copy after the
untar, or assert the tarball can never contain one — this took the first:
`firstboot.sh` gains **step 3e**, immediately after 3d. "Today's site tarball
ships no config.json" is a promise about a private sibling repo's future
releases, and a release that started shipping a `config.example.json`-shaped one
would silently overwrite real credentials with placeholders. Copying afterwards
makes the ordering correct **by construction**; the collision, if it ever
happens, is logged rather than assumed away. Both site-file lists now
cross-reference each other in comments, because they are hand-maintained and
drifting apart is exactly how this file went missing.

**MODE — 0600 root:root, and OWED A RULING (OI-20 above).** Worked out, not
guessed: `docker image inspect caddy:2.11.4-alpine` shows no `USER`,
`docker-compose.yml` sets no `user:` for caddy, and `docker run --entrypoint id`
on the pinned image reports `uid=0(root)`. So the container reads the read-only
bind mount as root and the most restrictive mode that serves is the same 0600
the other site files get. **It was not widened to 0644.** What needs the Owner
is the posture rather than the digits: this file carries `FEED_TOKEN`, a Kuma
push token and the Subsonic password, and it is now **served over HTTP** —
behind the kiosk site's `remote_ip {$PANEL_IP}/32` matcher, so not open to the
LAN, but an allow-list is not a secret store. Two consequences to accept: a
`user:` added to caddy later makes the file unreadable and the panel degrades to
defaults **silently**, and anything that can spoof the panel's source address
gets the token.

**Flagged, not fixed (Personal's, and that repo is clean):**
`Build-VentoyStick.ps1` assembles `out\site\` from a hardcoded `$wanted` list
that does not include `config.json`. So a real Ventoy stick still will not carry
one; `SITE_DIR=<a directory containing it>` builds now do.

**Run:** `vmtest/test-hub-seed.sh` → **20 passed, 0 failed, 0 skipped** (4 new:
`config.json` rides the payload; firstboot installs it at a line strictly AFTER
the untar; it is installed `-m 0600 -o root -g root`; and it is NOT in
late-command 4b's table). `test-wall-builder.sh` 12/12, `scripts/check.py` PASS,
`validate_config.py` ALL PASSED, `bash -n` on all three files touched plus
`dash -n` on the late-command bodies.

**Not proven:** `firstboot.sh` step 3e has never run. Nothing has served this
file, no panel has fetched it, and the uid finding is from the image on this dev
box — not from the AWOW.

### DRIVER — G1 — Round 1 — 2026-08-03 (an adversarial review of yesterday's fixes: eleven findings, ten real)

An independent read-only reviewer (OpenAI CLI) was pointed at the four commits
that fixed OI-19, the production meta-data hostname, `config.json` staging and
the A19 sim fixture, and asked the question this repo asks of everything: *would
it notice?* Ten times the answer was no. Four of those were tests that report
success while proving nothing — and those are the reason the other six survived
a green run, so they were fixed with the same weight.

**OI-19 HAD A SECOND HEAD, and it was the same head.** The refusal added
yesterday asks whether the payload's `site/` is a DIRECTORY. `render_seed_tree`'s
staging loop counted `user-data.filled` — which it has already made MANDATORY
thirty lines earlier — as a staged site file. So a `SITE_DIR` holding nothing
else satisfied `staged > 0`, built a clean production ISO, created
`deploy-payload/site/`, and handed 4b exactly the directory it was looking for.
4b then logged every required file missing (they only printed and `continue`d),
touched `.site-present` so `firstboot.sh`'s "site payload was present but the
fragment is missing" branch stayed quiet, and exited 0. Measured on this box
before touching anything: build rc=0, `site/` holding one file, 4b rc=0,
`.site-present` created. The directory's existence was never the property worth
checking. Both ends now check the FILES: the builder refuses to stage a
production image missing any of `.env`, `backup.env`, `smb.conf.fragment`,
`library-mounts.fstab`, `config.json`, and a missing `required` file makes 4b
exit 1. **That last one closes A10(a) in the REFUSE direction** — recorded, not
assumed; the lever to reverse it is moving a file from `required` to `optional`
in 4b's own table.

**`exit 0` AFTER A FAILED COPY.** Late-command 3's two payload branches read
`cp -a … && echo …; exit 0`, and `exit 0` is a separate command after the `;` —
it ran whether or not the copy worked. One I/O error partway through ~470 MB of
baked images left a half-copied `/target/opt/homehub`, printed "payload copied",
and handed 4b a `site/` holding whatever made it across before the error. "No
payload" is a recoverable state the box knows how to describe and says so
loudly; "some of the payload" is not a state at all. Now fatal, along with a
CIDATA seed that will not unmount after a successful copy.

**BUILD_PROFILE WAS A SUBSTRING SEARCH.** `grep -q BUILD_PROFILE=production`
over the whole file matches the four lines of comment that explain the marker as
happily as the assignment. A hand-edited `user-data.filled` could keep every
comment while the ACTIVE assignment said `sim` — the guard accepts it, and 4b
then silently permits a missing `site/` on a machine whose meta-data, hostname
and disk pin all say production. `assert_build_profile` parses the document
(`yaml.safe_load` discards comments outright), looks only at `late-commands`,
and requires EXACTLY ONE assignment with the expected value: zero means 4b reads
an unset variable and cannot refuse anything; two means the last one wins and
the file no longer says what it does. Third member of the family that already
holds `assert_storage_pin` and `autoinstall_hostname`, and for the third time
the reason is the same — a line-based read is one comment edit from the wrong
answer.

**NOTHING CHECKED THAT CADDY CAN READ THE FILE IT SERVES (OI-20).** The
healthcheck probes the admin API on :2019, which is up whenever the process is.
So the 0600 root-owned `config.json` becoming unreadable — a `user:` in
`docker-compose.yml`, a `USER` in a newer caddy image, daemon userns-remap —
leaves caddy `healthy`, the kiosk site 403/404s on `/config.json`, and
`loadConfig` NEVER THROWS: the panel paints on `js/config.js` defaults with no
`FEED_TOKEN`, no heartbeat, no music credentials. `:ro` and `read_only:` are
explicitly not the risk; both restrict writes and this is a read. `firstboot.sh`
gains **step 4b**: whenever the production source exists, assert the read AS THE
CONTAINER (`docker exec` inherits the service's user, so it fails precisely when
caddy would), and carry a failure to a non-zero exit at step 7 so the unit shows
FAILED rather than letting a silently-degraded panel look like a clean boot.

**A COLLISION WARNING THAT WAS TRUE ON EVERY REBOOT.** `firstboot.sh` step 3e
detected "the site tarball shipped its own `config.json`" by testing the
DESTINATION after the untar. `homehub-firstboot.service` has no marker guard and
no `ConditionPath*`; `RemainAfterExit=yes` only stops a second start within one
boot, and the unit is `WantedBy=multi-user.target`. So from the second boot
onward the file it found was the one IT had installed, and the warning fired
forever, on every hub, whether or not a tarball ever carried one — which is how
the real collision would have gone past. Answered from `tar -tzf` before
extracting now, written to a FILE first because `tar | grep -q` under `pipefail`
reports failure exactly when grep FINDS the match. The unit file's "becomes a
no-op on later boots" comment was simply false and is corrected.

**FOUR TESTS THAT PASSED WHETHER OR NOT THE CODE WAS THERE.** This is the part
that matters most, because it is why the above survived yesterday's green run.

- The suite **edited the repository's own tracked `user-data` and `meta-data`**
  and restored them from an EXIT trap. A trap is not a transaction: a kill at
  the wrong moment leaves a deliberately-corrupted template in the checkout, and
  a concurrent `build-seed.sh` would bake it. The builders now run against an
  ISOLATED COPY (`git ls-files` → `tar` → `git init`, so the payload copy takes
  the same `git ls-files` path a real build takes rather than the loud
  whole-worktree fallback) and the corruption happens there.
- **`extract_4b` exited 0 when it found no command.** So both negative
  assertions built on it — "no `/cdrom` left in the site step", "no
  `config.json` in 4b's table" — passed when the command was MISSING, which is
  exactly what a reverted OI-19 fix leaves behind. Extraction now demands
  exactly one match. **PROVEN by stubbing:** with 4b reverted to a pre-OI-19
  body the suite goes 28/0/0 → **7 passed, 14 FAILED, 1 skipped, exit 1**.
- **The case advertised as "the light-path case" never ran the light path.** It
  hand-created `/target/opt/homehub/site` and ran 4b alone — no late-command 3,
  no discovery, no mount, no copy. The light path is what OI-19 was about, so
  this was the test that most needed to bite and the one that structurally
  could not. It now runs EXTRACTED STEP 3 then 4b, three ways: a real payload
  under a real `/media` entry; a FORCED `cp` failure (a regular file where the
  directory must be); and the CIDATA branch **for real** — `losetup` the seed
  ISO this suite just built, `blkid -L CIDATA`, the command's own
  `mount -o ro`. Loop mounts, `/media` and `install -o root` need root, so a
  non-root run SKIPS them — and **skips now make the suite exit non-zero**,
  because "we could not look" is not a pass. **PROVEN by stubbing:** with step 3
  replaced by `exit 0` the suite reports **25 passed, 3 FAILED** (media path,
  cp-failure, CIDATA path); with only the `cp`-failure fatality removed,
  **27 passed, 1 FAILED**, and the failure line shows the command printing
  "payload copied" immediately after `cp` said "Not a directory".
- **Config installation was checked by line number and grep**, and "all seven
  files 0600" stat'd `.env` alone — so unreachable code, a widened
  `drive-identity.conf` (the file carrying disk serials, which nothing installed
  at all before yesterday) or any later chmod regression all passed.
  `firstboot.sh`'s 3d/3e region is now CARVED OUT AND EXECUTED against a scratch
  tree under `set -euo pipefail` (so the `tar | grep -q` trap is real), in three
  cases: a clean run, **the rerun**, and a tarball that really does ship a
  `config.json`. Every one of the seven destinations is `stat`'d for
  `600:root:root` individually.

**THE SIM PASSED WITH THE A19 FIXTURE DELETED.** `sim/validate-sim.sh` never
posted to `library-mounted` or `backup-drive-mounted`, so deleting
`sim/tracker-seed/definitions/drives.md` outright left the gate green — the same
defect the fixture exists to close, one level up. New **check 6b** posts a colour
report for both ids and asserts per-ITEM `reportColor` on
`library-drive-present` / `backup-drive-present`. Per item, not the ambient band,
and that is a measured fact rather than a preference: `engine.Aggregate` is a MAX
over lane scores, so one red report reaches red alone and `color_weight` only
orders the overlay's offenders — meaning **a red screen does not prove these two
lanes are red**. It then flips ONE lane green and asserts the other stays red, so
the assertion cannot be satisfied by a field that merely exists. Feed POST codes
are reported, not asserted, because with `TRACKER_COMMIT=true` on a non-git
`/data` a post stores the report and THEN returns 500 (A24(iii)).

**Runs (all real output, WSL2/Ubuntu as root):**
`vmtest/test-hub-seed.sh` → **28 passed, 0 failed, 0 skipped** (up from 20; the
new ones are the comment-vs-active BUILD_PROFILE case, the `SITE_DIR` holding
only `user-data.filled`, the missing `config.json`, three executed 3d/3e cases,
the light path via `/media`, the forced `cp` failure, the CIDATA seed, and the
`site/`-with-only-`user-data.filled` refusal).
`vmtest/test-wall-builder.sh` → **12 passed, 0 failed, 0 skipped**, unchanged.
`python scripts/check.py` → **RESULT: PASS** (config-validate, registry-integrity,
doc-navigability). `scripts/validate_config.py` → **ALL CONFIG CHECKS PASSED**.
`bash -n` on every shell file touched; `bash -n` AND `dash -n` on both inline
late-command bodies extracted from the rendered `user-data` — they run under
`sh` via curtin, not bash.
`sim/validate-sim.sh` → check 6b PASS on both cases against the RUNNING sim, i.e.
against NagLight's real loader and engine; **and PROVEN to bite**: with
`drives.md` deleted both cases FAIL with POST 400/400 and no `reportColor` on
either id, and pass again when it is restored. Checks 2-8 green. **Check 1's five
failures in that run are a stale environment, not a regression** — the containers
on this box were created before commit `85401f6` renamed the compose project
`awow-sim` → `homehub-sim`, so `docker compose -p homehub-sim ps -q` finds
nothing while `docker ps` shows every one of them healthy. A `sim/run-sim.sh`
recreate clears it.

**The reviewer's list of SURVIVORS was re-checked rather than taken on trust**
and holds: the hostname is parsed off the rendered file after substitution,
PyYAML's absence hard-fails during validation, multi-document input is rejected,
late-command 3 does precede 4b and does copy `site/`, `drive-identity.conf`
lands 0600 root-owned, `drives.md` parses under NagLight's YAML subset, and all
four seed files' item ids are unique. Nothing was changed on account of them.

**Not proven, and the list has not got shorter in the way that counts:** nothing
here has been installed. `firstboot.sh` step 4b has never run — no hub, no
compose bring-up — so the caddy-readability assertion is asserted-in-source
only. Subiquity has never run either late-command; what IS new is that they have
now been EXECUTED — by this suite, as root, against a fake `/target`, a real
`/media` entry and a real loop-mounted copy of the seed ISO this repo builds.
That is a strictly larger claim than yesterday's and still a strictly smaller
one than an install. OI-19 closes on a hub install; OI-20's mode ruling, and the
A10(a) refuse-direction decision made here, are the Owner's.

### DRIVER — G1 — Round 1 — 2026-08-03 (OI-18 ruled (b): the panel gets two mounts, two credentials and two failure policies)

The Owner ruled **exit (b)** — two UNC/credential pairs — and this is the half
of it that lives here. Exit (a), consolidating both trees behind one host, was
rejected because it would have re-opened `HOMELAB_TOPOLOGY.md` decision 2 ("the
panel pulls frame videos from Mini-serv directly").

**The contract was implemented against `Personal\homelab\deploy\storage-map.md`,
not against the old `SUBTREES` string, and the map was re-read first to confirm
the derived table in `WALL_GATE_HANDOFF.md` §5 rather than trusted.** It agreed
on every row, and `Generate-FromStorageMap.ps1 -Preview` independently derives
the same two flows from the same map — `sync-frame-videos
mini-serv://mini-serv/PictureFrameVideos -> wall-frame-pc:frame/` and
`sync-music main-library:/srv/library/NonDocs/Media/Music ->
wall-frame-pc:music/ (via //<AWOW>/Media)`. No edit to the map was needed and
none was made.

**THE TWO THINGS A SYMMETRIC IMPLEMENTATION GETS WRONG, and how each landed.**

*They are not the same shape.* Music is reached **through** the `Media` share
(§3 row 1 exports `NonDocs\Media`; §3 row 2 puts the music at
`NonDocs\Media\Music`), so the mirror descends into a `Music` subdirectory under
the mount. `PictureFrameVideos` is a **dedicated** share (§3b) whose content is
at its **root**, with no subdirectory at all. `flow_spec()` carries an empty
`F_SUBDIR` for frame and the mirror uses the mountpoint directly. Give frame a
subdirectory it does not have and the mirror looks one level too deep and finds
nothing — which, but for GUARD 1, would have mirrored emptiness over the cache.
The suite proves both directions: a stub that gives music no subdir and a stub
that gives frame the retired `FrameVideos/` one each turn a case red.

*They have different FAILURE POLICIES.* §4d: Mini-serv may sleep and is never
woken for this, so an unreachable frame share is skipped; the AWOW is always-on,
so a refused music mount is an alert. The old script had exactly one policy and
it was the music one.

**The decision that mattered most here is HOW the skip is decided.**
`mount.cifs` returns the same rc for "the box is asleep" and "the password is
wrong", so inferring the state from the mount failure would have made a wrong
credential permanently invisible on the one share designed to fail quietly.
`wall-sync.sh` therefore **probes 445 with a 4 s bounded TCP connect before
mounting**: nothing answering is a skip; a box that ANSWERS and then refuses the
mount is **fatal**, and says so in those words. Configuration defects — an unset
or placeholder UNC, a missing or unreadable credentials file — are fatal for
**both** flows. The frame flow's licence to be quiet does not extend to "nobody
filled this in".

**The silent skip still owes a consequence, so it reports one.** Every
successful flow stamps its finish time at the cache **root** — outside the
mirrored leaf, where `--delete` cannot remove it and the manifest walker cannot
see it — and every skip logs how old the cached content now is: a log line below
`WALL_FRAME_STALE_WARN_HOURS` (default 24), a WARNING above it, and "this panel
has NEVER completed a frame sync" when there is no stamp at all. It **never**
escalates into a failed unit however old the content gets: "Mini-serv has been
off for a week" is an allowed state per §4d, and at what age it stops being
allowed is a ruling nobody has made. Recorded as **A11(iv)** — whether it should
ever alert, and whether it should post to the tracker, is the Owner's.

**TWO UNITS, ONE SCRIPT, and that split was made deliberately rather than by
default.** §4d puts the frame flow on a one-minute accessibility-checked timer;
OI-15 gave the music pull boot + resume + on demand and *no* timer. One unit
cannot carry both — re-walking a music library over 802.11 sixty times an hour
is absurd, and the first music sync is allowed a full hour while a frame run
that is still going after two minutes is stuck. So `wall-sync.service` keeps its
three triggers and now syncs BOTH flows (the documented on-demand command means
exactly what it always meant), and `wall-sync-frame.{service,timer}` is the same
script with `--only frame`. One script, because every `--delete` guard is
identical between the flows and a second script is a second place for them to
drift. Two collision guards, because the units genuinely overlap: a per-flow
`flock`, and a new `--only` on `wall-media-manifest.py` so the every-minute
frame run cannot rewrite `music/index.json` from a music cache the boot run is
still filling.

**One flow's failure no longer cancels the other.** Each flow is attempted, each
gets its own verdict, and the exit status is non-zero if any failed — so a
HOMEHUB outage cannot also stop the frame videos refreshing in the same run.

**`SUBTREES` is gone as a string but kept as a property.** The comment at its
old line said a knob there would let a typo silently widen the mirror onto a
256 GB laptop disk. The map is now a `case` in `flow_spec()` with seven fields
per flow; only the UNC, the credentials file and the bench hook name knobs — the
subtree, cache leaf, manifest and failure policy are code. A `case` rather than
a longer colon-delimited string because two of the seven fields are prose.

**Retired, and each for a reason rather than for tidiness:** `MEDIA_SHARE_UNC`
and `MEDIA_CIFS_CREDENTIALS` (one address and one credential cannot reach two
hosts — that IS OI-18); the inline `MEDIA_CIFS_USER`/`MEDIA_CIFS_PASS` fallback
(same reason, and duplicating it per flow would have added four knobs whose only
purpose is to keep a password somewhere less safe than the file that already
exists); and the single `MEDIA_SOURCE_OVERRIDE` bench hook, now one per flow
because one directory cannot be both "has `Music/` under it" and "is the
content". `MEDIA_CIFS_EXTRA` stays **one** knob shared by both mounts: it is a
protocol choice and both hosts speak SMB3.

**The image half.** Wall late-command 4a now installs **two** credential files,
`0600 root:root`, naming each absence individually — "one of two" is a panel
with half its media, and which half decides whether the wall is silent or blank.
The new units are cp'd and the **timer** (not the service) is enabled;
`wall-firstboot.sh` enables it too and reports **both** sources separately.
`validate_config.py`'s referenced-file list grew the two units, because a frame
timer that is missing looks — from the journal — exactly like a frame flow whose
source is asleep, which is the state it is designed to be quiet in.

**COVERAGE, AND IT WAS PROVEN THE HARD WAY.** `vmtest/test-wall-builder.sh` goes
from 12 cases to **26**, and the 14 new ones were each verified to BITE by
breaking the code they watch and re-running the whole suite:

| stub | case that turned red |
|---|---|
| silence the missing-music-creds NOTE | names the MUSIC source as the casualty |
| silence the missing-frame-creds NOTE | names the FRAME source as the casualty |
| drop `cifs-frame.creds` from the staging loop | both credential files are staged |
| disable the stale-`cifs.creds` refusal | a stale PRE-OI-18 `cifs.creds` is refused |
| stop filling `MEDIA_MUSIC_SHARE_UNC` in the sim env | sim build refuses if the example loses it |
| stop filling `MEDIA_FRAME_SHARE_UNC` in the sim env | sim build refuses if the example loses it |
| give music no subdir | music comes from `Music/` UNDER the mount |
| give frame the retired `FrameVideos/` subdir | frame comes from the share ROOT |
| make the manifest generator ignore `--only` | `--only frame` leaves `music/index.json` alone |
| give frame the ALERT policy | an unreachable frame source is a silent skip |
| give music the SILENT-SKIP policy | an unreachable music source is an ALERT |
| remove both mirror-delete refusals | an EMPTY music source still refuses |
| drop the frame flow from `FLOWS` | a full run mirrors BOTH flows |
| make `--only` always select music | `--only frame` refreshes the frame flow |

The first pass at that verification is worth recording because it is the same
trap this suite exists for: three of the stubs were line-range deletes that also
ate a line-continuation backslash and an `fi`, so `common.sh` stopped parsing
and *every* case went red. The target case failed, but it proved nothing — a
broken file is not a disabled guard. They were re-run surgically (one anchored
replacement each, `bash -n` asserted before the run) and each then produced
exactly **one** FAIL line. A fourth stub renamed the frame flow's cache leaf,
which leaves the `frame: manifest` log line intact and therefore never touched
the case it was aimed at; it was replaced with one that makes `--only` always
select music.

**Two of the new sections need root** (the script takes a `/run` lock and calls
`mount`) and one needs 445 closed on loopback; both are `skip_case`d loudly and
counted, per this file's existing "a suite that quietly shrinks is the same
false green" rule.

**Verification, all real output:** `test-wall-builder.sh` 26 passed / 0 failed /
0 skipped; `test-hub-seed.sh` 28 passed / 0 failed / 0 skipped;
`python scripts/check.py` PASS (config-validate, registry-integrity,
doc-navigability); `validate_config.py` ALL CONFIG CHECKS PASSED with 16 wall
knobs declared; `bash -n` + `dash -n` clean on `wall-sync.sh` (it is now
dash-PARSEABLE, having lost its herestring, though it stays a bash script —
`${!var}` and `printf -v` are bash) and on `test-wall-builder.sh`; all **35**
wall late-commands `dash -n` clean as the outer shell sees them, and every
`bash -c` body `bash -n` clean. `wall-firstboot.sh` and `vmtest/lib/common.sh`
still fail `dash -n`, on pre-existing process substitutions this change did not
touch and that never run under dash.

**WHAT IS NOT PROVEN. Nothing here has mounted anything.** No ISO was built from
this, no panel was installed, and neither credential has ever authenticated
against a real Samba service. The two policy tests use 127.0.0.1, which answers
nothing on 445 — they prove the *decision*, not the mount. Read "emits" and
"refuses" literally; neither is "works".

### DRIVER — G1 — Round 1 — 2026-08-04 (the OI-18 adversarial review: the knob-containment claim did not survive, and the suite meant to defend it exited zero with the code disabled)

A second read-only adversarial review (OpenAI CLI) returned **40 findings**
against the OI-18 build. The headline claim of the previous entry —
*"`SUBTREES` is gone as a string but its property is kept — only the UNC, the
credentials file and the bench hook are knobs; subtree / cache leaf / manifest /
failure policy are code"* — **was refuted five different ways, and that property
is the entire reason `SUBTREES` was never a knob.** Everything below was
reproduced before it was fixed; three findings were partly wrong and are recorded
as such rather than quietly dropped.

**THE KNOB-CONTAINMENT PROPERTY WAS A COMMENT, NOT A MECHANISM** (findings 1-5).
`load_env_file` exported **every valid identifier** it found in `wall.env`, and
it ran *after* the script's own constants were set. So `wall.env` could set
`FLOWS=music` (dropping the frame flow out of every boot and resume run),
`PROBE_PORT=1` (making Mini-serv permanently "asleep"), or redirect `RUNDIR`,
`PATH` or `TMPDIR`. Four more routes existed beside it:

- `MEDIA_CIFS_EXTRA` was a free-text option string appended **after** the
  code-built cifs options, so `prefixpath=Movies` re-pointed the subtree, `ip=`
  the host, `rw` the read-only guarantee and a second `credentials=` the account.
- `WALL_MEDIA_CACHE` accepted any absolute path, including one whose `music` or
  `frame` leaf is a symlink — and `rsync -a --delete DEST/` **follows** a
  symlinked destination (proven on the bench: mirroring into `link/` writes and
  deletes inside `real/`).
- `MEDIA_FRAME_SOURCE_OVERRIDE` was an unrestricted **production** setting.
  `=/` mirrored the panel's filesystem; `=/etc/wall-panel` copied `wall.env` and
  **both credential files** into the kiosk-readable cache at 0644. That is a
  credential disclosure through a debug knob.
- the frame flow consumes the **share root**, so a typo'd
  `//MINI-SERV/NetworkShare` passed the generic `//?*/?*` check and would have
  mirrored an entire general-purpose share onto a 256 GB panel disk.

Now: an explicit `CONFIG_KEYS` **allowlist** (eight keys), every internal
constant `readonly` after the file is read, retired keys **refused loudly**
rather than ignored, `MEDIA_CIFS_VERS` as a validated **enum** with every other
mount option built in code, symlink-component refusal on the cache root and on
each leaf, `//host/share` **only** (no path tail, no traversal), and the bench
hook moved out of configuration entirely — it is `--bench-source FLOW=DIR`,
refused when `INVOCATION_ID` says systemd is the caller, and constrained to
root-owned fixtures under `/var/lib/wall-sync/bench`.

**A RULING TURNED INTO A GUARD.** RULED 2026-08-04 (the Owner): the HOMEHUB music
account is **also named `share`** — two accounts, two hosts, two passwords, one
username, made by hand at each box. That is now engineered against, not merely
obeyed: `wall-sync.sh` **refuses** a config whose two UNCs name the same host, or
whose two flows point at the same credentials file, because with a shared
username one credential could otherwise satisfy both mounts and mirror the wrong
share with nothing failing anywhere. Every place this change touched that said
"the `share` account" now names its **host**.

**CORRECTNESS AND SAFETY** (6-9, 13-16, 19, 20). Each was reproduced first:

| finding | what actually happened on the bench |
|---|---|
| 6 | `open(path + ".tmp", "w")` **follows a symlink**: a source containing `playlist.json.tmp -> victim` made this root process truncate, rewrite and chmod 0644 that victim. Now `mkstemp` + `lstat`, under a random `.wall-manifest.*` name the mirror excludes. |
| 7 | `set -e` is **disabled for the whole dynamic extent** of `if ! sync_flow` (proven: a `false` inside the function did not stop it). A failed `mktemp -d` made `"$from/"` the string `/`. Every fallible command in the function is now checked explicitly. |
| 8 | the empty-source guard counted files rsync then **excludes**, so a source holding only `Music/index.json` passed it and `--delete` emptied the cache with `WALL_SYNC_ALLOW_EMPTY=false`. |
| 9 | the cache count excluded the manifest basename at **every depth**, so a cache holding only `Album/index.json` counted as zero and the refusal never fired. Both helpers now exclude by EXACT top-level path — the same rule rsync uses. |
| 13 | the lock was released **immediately after rsync**, before the manifest walk and the stamp. Unmount and unlock are now separate steps and only end-of-flow releases the lock. |
| 14 | a failed unmount was warned and then converted into **success**. It now fails the flow. |
| 15 | **every** `flock` failure was read as contention. Proven: a bad descriptor exits **65**, contention exits 1 — and a failed `exec 9>` does not stop the function, so a lock file that cannot be created ended the run as `sync complete — nothing to do`. Now `flock -n -E 100`, and 100 alone means contention. |
| 16 | one `TimeoutStartSec` covered both flows in sequence, so a wedged music mount meant the frame flow **never ran**. Each flow now carries its own mount/rsync/unmount budget; the unit timeout is a backstop above their documented sum. |
| 19 | `08` is all digits and an invalid **octal** literal: `$(( now - 08 ))` aborts (proven). Stamps are now length-bounded and parsed base 10, and a **future** stamp is reported as unknown instead of being clamped to "0h old" forever. |
| 20 | "root-only 0600" was **claimed by the error message and never checked** — only root *readability* was. Now: regular non-symlink file, uid/gid 0, mode exactly 0600. |

**THE PROBE NOW HAS THREE ANSWERS, NOT TWO** (10-12), and this is the part the
standing ruling constrains. §4d says an unreachable frame share is skipped and
never woken, and **RULED 2026-08-04 (the Owner): the staleness ladder stays
reporting-only** — it may not fail the unit and may not post to the tracker. So
nothing here escalates; what changed is that the script no longer *claims* a
thing it has not established. The old probe called everything that was not a
completed TCP connect "Mini-serv is asleep" — Samba stopped, a firewall REJECT,
a DNS failure, the panel's own Wi-Fi down. Now:

- **AWAKE** — the connect completed. Mount; a refusal from here is FATAL.
- **OFFLINE** — nothing answered inside the probe window (`timeout` rc 124), or
  the link layer said there is no route (no ARP reply). This is the
  positively-established state §4d designs for, and the only one that earns the
  calm skip.
- **INDETERMINATE** — the name will not resolve, this panel has no default
  route, or the box answered with a **refusal** (an RST means it is *awake*).
  Still skipped, still exit 0, but logged as a WARNING that says the state is
  unknown. Proven on the bench: `127.0.0.1` gives `Connection refused` in
  milliseconds, `192.0.2.1` gives rc 124.

Finding 12 is a **wording** fix and it stands: a TCP SYN is not a "never wake"
primitive. Every claim now says *no magic packet is sent*, which is what the code
guarantees. **Whether Mini-serv's NIC is set to wake on a magic packet only, or
on any traffic pattern, is UNVERIFIED and is the Owner's to settle.**

**UNITS AND FIRSTBOOT** (17, 18). Firstboot's swallowed `systemctl enable … ||
warn` followed by an unconditional "…enabled" log line **is this repo's signature
bug**, and it was still there for the frame timer and the resume hook: a panel
whose every-minute cadence failed to enable reported a green first boot that
explicitly claimed the cadence exists. Enabling now goes through one judged
helper, the success line prints only on success, and a failure makes the unit RED
and **withholds the provisioning marker**. `wall-sync-frame.timer` used
`OnUnitActiveSec=1min`, which measures from **activation** — a run longer than a
minute re-fires immediately — while a comment in the same file claimed it meant
"a minute after the last run FINISHED". It is `OnUnitInactiveSec` now, and the
comment is true.

**AN ON-BOX CREDENTIAL EXPOSURE** (21). The staged credentials are 0600 but owned
by the **builder's uid**; the ISO is written with `-rock`, which preserves
uid/gid; `cp -a` carries that into `/opt/wall-panel/site`. The late-command
`chmod 0600`'d the payload copies — fixing a mode that was already right and
leaving the owner alone. With the ordinary builder uid and the panel's `panel`
user both **1000**, the kiosk user could read both Samba passwords for the life
of the install. The payload copies (`wall.env` and both `cifs-*.creds`) are now
**deleted** after installation; `/etc/wall-panel` holds the only copies.

**THE TEST SUITE WAS THE OTHER HALF OF THE FINDING** (26-33). Proven, not
asserted: with `wall-sync.sh` present but **unparseable** and loopback 445
occupied, the previous suite reported **14 passed, 0 failed, 2 skipped and exited
0**. This one, in the identical situation, reports **49 passed, 1 failed** and
exits 1. It goes from 26 cases to **85**:

- a **STATIC GATE** that never skips: the scripts must exist and parse, the units
  must pass `systemd-analyze verify` and say what the design says (exact
  `ExecStart … --only frame`, the cadence directive, the *absence* of
  `OnUnitActiveSec`), and `user-data` must actually cp, enable and — new —
  **delete** the staged credentials;
- **per-flow failure aggregation** is exercised for the first time (music fails,
  frame must still mirror, the run must still exit non-zero); the previous
  suite's only two-flow case had both flows succeed, so an abort-on-first-error
  orchestration passed it;
- the case the suite **explicitly skipped** — 445 answers and the mount is then
  refused, the only path that can ever expose a wrong frame credential — now runs
  against a deliberate loopback listener;
- `--only frame` **mutates the source** first, so a no-op mirror fails it;
- the two missing-credential cases use **asymmetric** states and assert the other
  message is ABSENT (they previously ran against the identical both-absent state,
  so a predicate that named both, or named the wrong one, passed both);
- the cross-repo cases now also read **Personal's** deploy half, and SKIP loudly
  when the sibling checkout is absent rather than silently proving nothing;
- the `wall.env` allowlist is driven through a **real `wall.env`**, in a private
  mount namespace (`unshare -m` + a bind mount over `/etc/wall-panel`), because
  the path not being a knob is part of the property being tested;
- everything runs against an **isolated sandbox checkout** (`git ls-files` → tar →
  `git init`). The old suite rewrote **tracked** files — `wall.env.example`,
  `electron-runtime-deps.tsv`, `user-data` — with an EXIT trap that removed only
  the temp dir, so an interrupt left the repo damaged.

**EVERY GUARD WAS PROVEN TO BITE**, one surgical anchored stub at a time on an
isolated copy, `bash -n` / `py_compile` asserted clean before each run (a broken
file is not a disabled guard — that is the mistake the last pass made). **22 of
22 stubs turned exactly their intended case(s) red and nothing else:**

| stub | case(s) that turned red |
|---|---|
| widen `load_env_file` back to every identifier | FLOWS / PROBE_PORT / RUNDIR containment (3) |
| ignore a retired key instead of refusing | `MEDIA_CIFS_EXTRA` is REFUSED |
| accept any `MEDIA_CIFS_VERS` | it is an ENUM, not an option string |
| drop the symlinked-leaf refusal | symlinked cache leaf (2, incl. the victim directory) |
| accept any `--bench-source` directory | fixture OUTSIDE the fixture root |
| allow `--bench-source` under systemd | refused when systemd is the caller |
| restore the old `//?*/?*` UNC check | a frame UNC with a PATH TAIL |
| skip the two-distinct-sources check | two UNCs on the SAME host |
| restore the predictable `.tmp` manifest name | manifest-temp symlink (2, incl. the victim file) |
| count emptiness without the rsync exclusions | a source holding ONLY the excluded manifest (2) |
| restore the two-outcome probe | REFUSED is INDETERMINATE (3) |
| accept a credentials file on readability alone | a 0644 credentials file is refused |
| `break` on the first failing flow | the FRAME flow ran anyway |
| restore `systemctl enable … \|\| warn` | firstboot judges the enable (2) |
| `OnUnitActiveSec` back in the timer | the cadence assertions (2) |
| `chmod` the staged creds instead of deleting | payload copies are REMOVED |
| drop `--only frame` from the frame unit | ExecStart is EXACTLY `--only frame` |
| make the mirror copy nothing new | the new frame file reached cache + playlist |
| report the music credential's absence always | …does NOT also blame the MUSIC source |
| point `PERSONAL_REPO` at an empty tree | all four cross-repo name cases |
| count the manifest by name at any depth | `Album/index.json` counts as POPULATED |
| drop the base-10 stamp coercion | an octal-looking stamp still reports |
| clamp a future stamp to zero | a FUTURE stamp is UNKNOWN, not fresh |

**FINDINGS THAT DID NOT SURVIVE CONTACT, recorded rather than quietly dropped:**

- **26 is right about "unparseable" and wrong about "deleted".** With
  `wall-sync.sh` *deleted*, the old suite exited **1** — not because any
  wall-sync assertion fired (they all skipped) but because the builder cases
  failed for an unrelated reason. The false green needed the file to be present
  and broken. Both are fixed; only one was real.
- **11 is half-cured, and the half that remains is the ruling.** The `port=` /
  `ip=` divergence it describes is gone with `MEDIA_CIFS_EXTRA`. But "wrong
  credentials can be permanently invisible behind a persistently failing probe"
  is what §4d *asks for* — an unreachable frame share is skipped and never woken.
  The mitigation is finding 10's INDETERMINATE class: a box that is up and
  refusing is no longer reported as asleep.
- **the claims listed as SURVIVED were left alone**, including the cross-repo
  names (35-38, 40): nothing was renamed.

**Verification, all real output, run in WSL Ubuntu as root against an isolated
checkout:** `test-wall-builder.sh` **85 passed / 0 failed / 1 skipped** (the skip
is the shell-artifact-dependent group; there is no `OfficeWallNaglight/dist` on
this host); `test-hub-seed.sh` **28 passed / 0 failed / 0 skipped**;
`python scripts/check.py` **RESULT: PASS** (config-validate, registry-integrity,
doc-navigability); `validate_config.py` **ALL CONFIG CHECKS PASSED** with **18**
wall knobs declared; `bash -n` **and** `dash -n` clean on `wall-sync.sh`;
`bash -n` clean on `wall-firstboot.sh` and `test-wall-builder.sh`;
`wall-media-manifest.py` compiles; **all 35 wall late-commands `dash -n` clean as
the outer `/bin/sh` sees them and all 7 inner `bash -c` bodies `bash -n` clean**
(and the hub's 28 + 10, unchanged). `test-wall-builder.sh` no longer passes
`dash -n` — it uses bash arrays to keep the bench arguments and the environment
apart — which matches `test-hub-seed.sh`; both are `#!/usr/bin/env bash` and
nothing routes either through `/bin/sh`.

**WHAT IS STILL NOT PROVEN, and it is most of it. NOTHING HAS EVER BEEN
MOUNTED.** No ISO was built from this change, no panel was installed, and neither
credential has authenticated against a real Samba service. Specifically:

- the reachability cases use `127.0.0.1` and `192.0.2.1`. They prove the
  **decision** — skip / warn / fail, and which — not the mount.
- findings **13, 14, 15 and 16** (lock lifetime, unmount failure, flock error vs
  contention, per-flow time budgets) are implemented and reviewed but have **no
  automated coverage**: each needs a real cifs mount, a stuck unmount or a hang to
  trigger. They are code changes on an untravelled path.
- the credential-exposure fix (21) is a change to a **late-command that has never
  run under Subiquity**.
- `systemd-analyze verify` parses the units; it has not scheduled them.

**FOR THE OWNER — four decisions this change made on its own, and one it could
not make:**

1. **`wall-sync.sh` now REFUSES two UNCs that name the same host** (and two flows
   sharing one credentials file). This is engineered against the 2026-08-04
   `share`-username ruling and is stricter than OI-18 said in words. If a
   single-host topology is ever wanted, this refusal has to be lifted
   deliberately — it will not fail quietly.
2. **A bench run now needs `/var/lib/wall-sync/bench`** and a command-line flag;
   the old `wall.env` override keys are refused. Any local habit built on them
   breaks loudly.
3. **`WALL_SYNC_ALLOW_EMPTY` must now be exactly `true` or `false`** — anything
   else is refused rather than read as "not true", because it is the switch that
   permits an irreversible mirror-delete.
4. **An INDETERMINATE frame probe warns once a minute** while it persists (a
   stopped Samba, say). It never fails the unit — the ladder stays
   reporting-only, as ruled — but it is a journal line a minute.
5. **UNVERIFIED, and only the Owner can settle it: is Mini-serv's NIC set to wake
   on a magic packet only, or on any traffic pattern?** The code guarantees it
   sends no magic packet. If the adapter wakes on arbitrary traffic, the
   every-minute probe can wake the box, which §4d does not want. Also still open
   from the previous entry: **what the HOMEHUB music account is called** — now
   ruled to be `share`, but its §2 status (a real §2 identity with a
   `Samba<Name>Password` key, or a panel-only account made by hand at the box) is
   still unruled.

---

### DRIVER — G1 — Round 1 — 2026-08-04 (`/opt/homehub` installed WORLD-WRITABLE — in every hub ISO ever built here, and only an install could see it)

**A boot found it, and nothing else could have.** The hub gate VM, installed from
a built ISO, was measured on 2026-08-04:

```
drwxrwxrwx root:root /opt/homehub
drwxrwxrwx root:root /opt/homehub/stack
drwxrwxrwx root:root /opt/homehub/images
-rwxrwxrwx root:root /opt/homehub/stack/.env
-rwxrwxrwx root:root /opt/homehub/stack/docker-compose.yml
230 world-writable paths under /opt/homehub
```

and confirmed from an unprivileged account on the box: `sudo -u nobody test -r
/opt/homehub/stack/.env` **readable**, `sudo -u nobody test -w
/opt/homehub/stack/docker-compose.yml` **writable**. `stack/.env` carries
`OAUTH2_PROXY_CLIENT_SECRET`, `TECHNITIUM_ADMIN_PASSWORD`,
`FINANCE_ACTUAL_PASSWORD`, `FINANCE_TRACKER_FEED_TOKEN`, `CLOUDFLARE_API_TOKEN`
and more; a writable `docker-compose.yml` on a box that runs it as root is a
straightforward **local privilege escalation** — any local account, or any
compromised container with a host bind-mount, can add a privileged service or a
host mount and own the machine at the next bring-up.

**THIS IS THE SAME FAMILY AS OI-19 AND THE GITIGNORED-PAYLOAD LEAK: a latent
defect present in every artifact this repo has ever produced, invisible to every
static check, surfaced by an install.** It is worth saying plainly why it
survived: **nothing in this repo had ever looked at a mode.** Every assertion
here reads text — a substituted hostname, a disk pin, a `BUILD_PROFILE` marker,
a soname table — and a permission bit is not text. Three suites, a full sim and
multiple ISO builds all passed over it.

**NOTHING WAS MALFUNCTIONING.** The chain is three honest links:

| link | what it does | what it therefore records |
|---|---|---|
| the worktree, as WSL sees it | DrvFs/9p has no metadata for an NTFS mount | `-rwxrwxrwx` for **every** file on `/mnt/c` |
| `git ls-files` -> `tar` -> `genisoimage -rock` | Rock Ridge preserves mode/uid/gid faithfully | 311 group- or world-writable entries in the ISO payload |
| late-command 3's `cp -a` | preserves mode/uid/gid faithfully | `/opt/homehub` exactly as the ISO said |

The mode was never **decided** anywhere. It is decided now, in three places,
each with a job the others cannot do.

**1. THE BUILDER — `normalize_payload_modes` (`vmtest/lib/common.sh`), the
primary fix.** One table, applied to the whole staged payload after every stager
and before the ISO is written, on both images:

```
directories                 0755      site/  (materialised config)  0700
files beginning `#!`        0755      every file in site/           0600
every other ordinary file   0644      stack/.env, any *.creds       0600
```

**The executable bit comes from the `#!` line, not from git's index, and that is
a deliberate refusal of the obvious source.** `core.filemode` is **false** on
this Windows checkout, so only **10 of the repo's 58 scripts** are recorded
`100755` — `stack/provision/*.sh`, `stack/samba/library-guard.sh` and
`stack/autoinstall/firstboot.sh` are all `100644` in the index. Keying off it
would have shipped them non-executable: the mirror of the bug being fixed. A
shebang is in the file's own content, cannot drift out of step with a checkout
setting, and gives a new script the bit for free. The precedent for stating modes
as a table and then asserting them is `assert_wall_artifact_contract`, which
reads `-rwxr-xr-x app/wall-shell` and `4755 root:root chrome-sandbox` straight
out of the shell tarball rather than trusting the extraction.

**2. THE ISO WRITER — because on the default build host the builder fix CANNOT
BIND, and that is the finding underneath the finding.** WSL mounts a Windows
drive without metadata, so `chmod 0600 f` **succeeds, reports nothing, and
changes nothing** — verified here. `OUT_DIR` defaults to `vmtest/.out` (on C:)
and the README recommends `/mnt/d`, so the **default and recommended build paths
are both ones where a staged mode cannot be stored at all.** A normaliser that
assumed otherwise would print a tidy summary and ship the same ISO. So
`normalize_payload_modes` **probes** the staging filesystem and hands enforcement
on: `write_seed_iso` switches `-rock` to **`-r` (rational rock: uid/gid 0, read
bits set, execute bits normalised, every write bit cleared)**, and
`build-repacked-iso.sh` adds `-chmod_r go-w` + `-chown_r 0` + `-chgrp_r 0` scoped
to `/deploy-payload` (subtractive, so it is unconditional and can only ever
remove a bit). `assert_iso_payload_modes` then judges **the artifact**, which is
the only layer that can speak on this host.

**3. INSTALL TIME — hub late-command 3b, wall late-command 3a.** Not merely
defence in depth, given (2): a payload can still arrive from a build route that
never applied the policy. Both take ownership (`chown -R 0:0` — `-rock` preserves
the **builder's** uid, and the ordinary builder uid 1000 is the hub's `hub` and
the panel's `panel`; this is OI-18's ownership lesson generalised), clear
group/other write, tighten `site/` to 0700 and its files plus `stack/.env` and
any `*.creds` to 0600, and then **re-check and refuse the install** if anything
group- or world-writable survives. **The wall's step is ordered BEFORE the shell
untar on purpose:** a recursive chmod after it would strip `chrome-sandbox`'s
setuid bit and leave a permanently dark panel whose `[ -x ]` is still true.
It does not fight OI-18 or 4b — 4a/4b still delete or reinstall the staged
credential copies; this narrows the window before they run and agrees with them
about `site/` being 0700.

**THE GUARD THAT WOULD HAVE CAUGHT IT.** `assert_payload_modes` **fails the
build** on any group- or world-writable staged path, and has exactly two
outcomes, never a silent third: judge the tree, or — when the filesystem cannot
carry modes — require that the ISO layer is armed and let
`assert_iso_payload_modes` judge the result. New cases in
`vmtest/test-hub-seed.sh` (5) and `vmtest/test-wall-builder.sh` (8, three of them
static, including that the wall's chmod is ordered before the untar).

**PROVEN TO BITE, and it bit its own author first.** With
`normalize_payload_modes` stubbed out in the sandbox checkout the hub build now
refuses — *"the staged deploy payload has 168 group- or world-writable path(s)
out of 218"* — and goes green again when it is restored; the wall does the same.
The first version of that stub run exited **141 with no message at all**:
`find … | head -n 10` under `set -o pipefail` SIGPIPEs `find`, so the refusal
fired hardest as an unexplained exit. That is the third time this exact shape has
cost this repo something (`assert_electron_runtime_deps`,
`stage_wall_site_into_payload`); both mode checks now count and sample in one
`awk` pass.

**MEASURED, before and after, on a payload staged the way a build stages one:**

| | before | after |
|---|---|---|
| staged tree (ext4) | 172 of 220 paths group/world-writable | **0 of 220** |
| `stack/docker-compose.yml` | 0777 | 0644 |
| `stack/provision/provision-samba.sh` | 0777 | 0755 |
| `site/` | 0777 | 0700 |
| `site/.env`, `site/cifs.creds`, `stack/.env` | 0777 | 0600 |
| seed ISO built from a **DrvFs** tree | 311 group/world-writable entries | **0** (`-r`; `dr-xr-xr-x` / `-r-xr-xr-x`, uid/gid 0) |

**WHAT IS PROVEN AND WHAT IS NOT.** Proven: the staged-tree policy, the
assertion (by stub), the recorded modes of a freshly written seed ISO on the real
DrvFs path, and both install-time steps **executed** against a fake `/target`
deliberately staged 0777 (0 writable paths left, `.env` `600:root`, `site/` 0700,
`*.creds` 0600 — plus the refusal, forced with `chattr +i`). **Not proven: no ISO
has been rebuilt and no box has been installed since the fix** — the Owner runs
the verification build. Suites: `test-hub-seed.sh` **35 passed, 0 failed, 0
skipped**; `test-wall-builder.sh` **97 passed, 0 failed, 0 skipped**;
`python scripts/check.py` PASS; `scripts/validate_config.py` ALL PASSED.

**ONE OUT-OF-LANE FIX, recorded rather than smuggled.** `test-wall-builder.sh`
section 2 (the Electron dependency gate) invoked the builder with **no
`WALL_SHELL_DIST`**, so it resolved the sibling repo relative to the temp
sandbox, found nothing, and refused for *"no shell artifact"* instead of the
reason under test. On a machine without the private sibling the whole section
SKIPS; on one with it, both cases **FAIL — at HEAD `718b092`, before this
change** (reproduced: 83 passed, 2 failed). So that gate — *an Electron bump that
needs a library this image does not install* — was unexercised on both kinds of
machine. Fixed by passing `WALL_SHELL_DIST=$DIST_REAL`.

**FOR THE OWNER — three consequences, none needing a ruling but all worth
knowing:**

1. **`vmtest/build-seed.sh` and `build-wall-seed.sh` now require `xorriso`**, not
   just one of `genisoimage`/`xorriso`, because `assert_iso_payload_modes` reads
   the burned image with it (it is the only lister that prints whole paths, and
   naming the offender is most of a refusal's value). Both are already in this
   repo's documented install line.
2. **On the DrvFs build path secrets arrive `0444` in the ISO, not `0600`** —
   `-r` sets all read bits. That is not a regression (they were `0777`), the
   install-time steps tighten them to `0600 root:root` immediately after the
   copy, and late-command 4b then installs the real ones `0600 root:root`
   anyway. A build staged on a Linux filesystem keeps the precise `0600` end to
   end. The clean way to get that on Windows is `[automount] options="metadata"`
   in `/etc/wsl.conf` — a host setting, deliberately not something the builder
   demands.
3. **`/opt/homehub/stack/.env` is now `0600 root:root` on a SIM box too.** A
   production box already had that (4b installs it so), so this only makes the
   sim match production — but `docker compose` in `/opt/homehub/stack` must now
   be run as root there, as it always had to be on the real hub.

---

### DRIVER — G1 — Round 1 — 2026-08-04 (A19 is buildable at last, and finding out what it needed found two more defects)

**WHAT THIS ENTRY IS.** The wall gate's §5 said A19 was blocked on "wiring
between the two VMs: the Internal switch, static addresses, `WALL_HOST`
overridden, `PANEL_IP` set, and the sim tracker definitions seeded". Every item
on that list is now delivered *by the ISOs and by tracked scripts*. Working out
what each one actually required surfaced **two defects that no amount of review
would have found**, because neither is reachable until a client has to talk to a
gate hub — and nothing ever had.

**DEFECT: THE KIOSK SITE HAS NEVER SERVED TLS, ON ANY GATE VM, AND CADDY WAS
HEALTHY THE WHOLE TIME.** The sim `DOMAIN` is `vmtest.sim.invalid`. `.invalid`
is reserved by RFC 6761 precisely so that it can never resolve publicly, which
also means no ACME challenge over it can ever be validated. The real Caddyfile's
only global option is `email {$ACME_EMAIL}`, so **every** site on a gate VM —
including `{$WALL_HOST}:{$WALL_PORT}`, the one A19 exists to render — has been
retrying a certificate it can never get, and failing every handshake for want of
one.

Nothing noticed because **every V3 gate asserted container health**, and Caddy is
genuinely healthy: the process is up, its config loaded, its healthcheck passes.
It is serving nothing. That is the same shape as `/healthz` on the tracker and
`exit 0` after a failed copy — a green that answers a question nobody was asking.

The fix was sitting in the repo. `sim/caddy/Caddyfile.sim` has carried
`local_certs` since WI-10.14 as a documented REAL-vs-SIM delta; the **installed**
hub simply never inherited it, because the sim compose file forks the Caddyfile
and `render_seed_tree` does not. `apply_sim_caddy_local_certs` now applies that
one delta to the payload copy on the sim path, asserts it landed **inside the
global block** (a stray `local_certs` in a site body is a parse error and a
restart loop), and refuses if the tracked file ever declares it — a hub minting
its own certs for a publicly-resolvable name is a browser warning on every
household device.

**AND THE CONSEQUENCE IS A STATED NON-PROOF, NOT A SECOND FIX.** Nothing can put
that internal root into the wall image: it does not exist until the hub's first
boot, which is after the panel's ISO is written. So the sim `WALL_APP_CMD` gains
`--ignore-certificate-errors` beside `--disable-gpu`, **this gate proves nothing
about TLS trust**, and the builder now **refuses** a production `wall.env`
carrying that flag — the only way it reaches a real panel is a copy-paste from a
sim one, and the origin it would stop validating is the one that injects this
household's identity header.

**DEFECT: THE GATE HUB'S TRACKER HAS NO ITEMS, AND A WALL CANNOT SHOW THE
DIFFERENCE.** A19's whole assertion is that `library-mounted` and
`backup-drive-mounted` report RED **on the panel's screen**.
`sim/tracker-seed/definitions/drives.md` is the only committable thing that
declares those two ids — and it was mounted only by `docker-compose.sim.yml`,
the *container* sim. An installed hub runs `stack/docker-compose.yml`, whose
tracker had no seed at all. Against an empty `/data` there is nothing to be red;
the container still reports healthy because the healthcheck probes `/healthz`,
which is data-free by design (SR-040); and on a wall **"no red drive check" and
"the drive check is green" are the same picture.** Measured 2026-08-01 in a
different guise: the gate VM's tracker ran healthy for hours with an empty
`/data` and its nightly run failing every night.

`TRACKER_SEED_DIR` + `TRACKER_SEED_SRC` are now knobs, and both are **inert by
default**: blank `_DIR` means the entrypoint passes no `--seed`, and the default
`_SRC` (`stack/tracker-seed/`) ships a README and deliberately **no
`definitions/`**, so a box that sets the env var by mistake still seeds nothing.
A real household's definitions arrive with the person, not from a fixture.

**THE LAB ITSELF — `apply_sim_lab_netplan`, and the three things that make a
second NIC harder than it sounds.** Both images ship one network stanza,
`ethernets: any-eth: match: name: "e*"` with DHCP, and keeping ONE match for
production and the VM is deliberate. It cannot express the lab: two NICs both
match `e*`, so netplan would DHCP the Internal leg too, where nothing answers.

1. **The legs can only be told apart by MAC.** Both come up as `eth0`/`eth1` in
   an order VMBus decides, so both stanzas match on `macaddress:` and
   `New-HomeHubVm.ps1` sets those MACs **statically** — a dynamic MAC changes on
   recreate, so the netplan that matched it yesterday matches nothing today.
   That is two places holding the same three values, which is drift waiting to
   happen, so the builder writes `$OUT_DIR/a19-lab.env` and `Start-A19Gate.ps1`
   reads it. Nobody retypes a MAC. A build with no lab knobs **deletes** any
   stale manifest rather than leaving one that outlives the ISO it describes.
2. **The Internal switch has no internet and the installer needs one.** Each VM
   keeps a Default Switch leg purely so `apt` can fetch 40 packages; the lab leg
   carries **no gateway**, so the default route stays where the packages are.
3. **An unscoped nameserver on the lab leg breaks the install.** The panel must
   resolve `WALL_HOST` through the hub's Technitium — but pointing it there for
   *every* name sends `archive.ubuntu.com` to a box that does not exist yet.
   `nameservers.search` makes it a **routing** domain in systemd-resolved, so
   only lab names go to the hub. `SIM_LAB_DNS` without `SIM_LAB_SEARCH` is
   refused rather than accepted.

**NO OVERRIDE WAS NEEDED FOR `WALL_HOST`, and that is worth recording** because
both the plan and the handoff said one was. The panel's sim default is
`wall.vmtest.sim.invalid` and the hub's sim `DOMAIN` is `vmtest.sim.invalid`, so
`EXTRA_SUBDOMAINS=wall` makes Technitium authoritative for exactly that name.
The containment property is *stronger* than overriding to `wall.home.arpa`: an
`.invalid` name can resolve inside the lab and nowhere else, so a sim panel still
cannot reach anything real.

**PROVEN, AND WHAT IS NOT.** Both ISOs were **rebuilt** — the first build from
`79f4ed5`, which closes the "no ISO has been rebuilt" half of OI-21 for the
*artifact*: `assert_iso_payload_modes` passed on both, "nothing under
/deploy-payload is group- or world-writable". The rendered netplan, the staged
`.env`, the `local_certs` placement, the seed fixture in the payload and the
panel's two flags were all read back off the built trees. Suites:
`test-hub-seed.sh` 35 to **47 passed, 0 failed, 0 skipped**;
`test-wall-builder.sh` 97 to **107 passed, 0 failed, 0 skipped**;
`scripts/check.py` PASS; `scripts/validate_config.py` ALL PASSED.

**NOT proven: nothing has been installed.** No VM has booted either ISO. Both
new defects are fixed the way the last three were — in the builder, with a
refusal — and both stay INSTALL-UNVERIFIED until OI-22 runs. That run is the
Owner's: it needs elevation and about three hours, and it is the single thing
that speaks to OI-17, OI-19, OI-20, OI-21 and the hostname fix at once.

**ALSO CLOSED, AND SMALL:** the screenshot watcher. The scratch
`watch-wall-vm.ps1` had a hard 90-minute deadline against a ~2h10m install,
exited quietly, and left a console that simply stopped updating — silence
indistinguishable from a hung VM, from a process an unelevated session cannot
restart. `vmtest/Watch-VmConsole.ps1` is tracked, budgeted in hours,
`-UntilOff`-capable, and **every** exit path writes why it stopped, including
"the install may STILL BE RUNNING; this is not evidence of a hung VM".

---

### DRIVER — G1 — Round 1 — 2026-08-04 (A19 RAN: the shell painted, and it took two hand-patches to get there)

**THE PANEL PAINTED.** A red wall reading `100%`, with an overlay naming both
drive lanes by their real reasons — *"Library drive mounted: /srv/library is NOT
MOUNTED … · Backup drive mounted: /mnt/backup-drive is NOT MOUNTED …"*. Captured
with `grim` from inside the cage session and saved to
`D:\vmtest-out-wall-a19\panel-a19.png`. Nothing has ever rendered the tracker's
UI on a panel before; OI-17's narrow point is answered.

**AND IT IS THE STRONG FORM OF THE ASSERTION, not just a red screen.** `140785e`
warned that a red screen proves nothing on its own, because any stale habit
reddens it. `/api/today` carried the per-ITEM colours at the same moment:

```
ambient: red   score: 100
  library-drive-present        red      Library drive mounted
  backup-drive-present         red      Backup drive mounted
```

**PROVEN BY THE IMAGE, no patching, on two clean installs:**

- two VMs on ONE Internal switch at static, MAC-pinned addresses — the lab leg
  came up during the *installer* phase, which is the first real-boot evidence
  `apply_sim_lab_netplan` has;
- **the kiosk site served TLS** — cert issued by `CN = Caddy Local Authority -
  ECC Intermediate`, `SAN: DNS:wall.vmtest.sim.invalid`. That site has never
  served a byte on a gate VM before (this session's earlier entry, defect #14);
- **the `/32` guard**, answering `403 wall: panel only` to the hub itself and
  `200` to the panel — §3 step 7's first assertion, and its third too:
  `ss -ltn` shows `10.99.7.10:8443`, not `0.0.0.0:8443`;
- **the tracker seeded itself from the fixture** on the panel's FIRST request —
  defect #15's fix, working end to end;
- `cage -d -- /opt/wall-panel/app/wall-shell --disable-gpu
  --ignore-certificate-errors`, Electron 38.8.6 up with its zygotes and
  `chrome-sandbox`, `IF-005: ldd resolves every library`;
- **defect #13 on BOTH images, on installed boxes at last: 0 group- or
  world-writable paths** under `/opt/homehub` (was 230) and `/opt/wall-panel`
  (was 215); `stack/.env` `-rw------- root:root`.

**NOT PROVEN — two hand-patches, and the gate does not pass until they are
gone.** Same posture as 2026-08-03's defects 10-12: this proves the FIXES, not
the IMAGE.

---

#### DEFECT: `.invalid` is unresolvable BY DESIGN, and the design is CLIENT-SIDE

The A19 lab was built on `wall.vmtest.sim.invalid` on the reasoning that
`.invalid` can never resolve publicly (true), that Technitium authoritative for
it inside the lab therefore resolves it locally (also true — `dig @10.99.7.10`
returned the address), and that this was *stronger* containment than a
real-looking name (true as well). The panel still died on
`ERR_NAME_NOT_RESOLVED`.

**systemd-resolved implements RFC 6761 §6.4 and synthesises NXDOMAIN for
anything under `invalid` without ever querying the link's DNS server.** The
server was never the problem; the stub refuses to ask. Measured on the panel:

```
dig wall.vmtest.sim.invalid @10.99.7.10   -> 10.99.7.10      (Technitium: correct)
dig wall.vmtest.sim.invalid @127.0.0.53   -> (empty)         (the stub the shell uses)
curl --resolve …                          -> 200 + real JSON (DNS bypassed)
```

So the property that makes `.invalid` safe as a sim default — nothing will ever
resolve it — is precisely what makes it unusable as a lab name. Both follow from
the same sentence in the RFC, which is why the reasoning felt sound and was not.
**And the plan had said so:** *"`WALL_HOST` MUST be overridden — the sim default
is deliberately unresolvable."* That instruction was overridden on the strength
of an argument about the server, in a failure that lives in the client.

Fixed as `assert_stub_resolvable`, which refuses a lab build whose search domain
or `WALL_HOST` sits under `.invalid`, `.localhost` or `.local`, and names
resolved and the RFC in the message — a refusal that only says "no" trains the
next person to try `.local`. **The non-lab default stays `.invalid`:** a lone sim
panel must still be unable to reach anything real, and only a lab has a reason
to give that up. `.sim` is the replacement, which is what `sim/.env.sim` has used
since WI-10.14.

---

#### DEFECT: a SIM hub cannot report either drive lane, so A19's assertion was unreachable

> **Historical failure record, superseded 2026-09-08.** The two lanes shown
> below were replaced by the reserved `file-share-backup-health` state item and
> the old backup-drive unit is actively removed during upgrade.

Both lanes detected the fault perfectly and told nobody:

```
[library-guard] UNHEALTHY: /srv/library is NOT MOUNTED — the library drive is absent…
[library-guard] NAGLIGHT_FEED_URL unset — journal only (red; check id would be 'library-mounted')
[backup-drive-health] no /etc/homehub-backup/backup.env — backup not provisioned, nothing to check
```

`library-guard.sh --report` takes its feed configuration from
`/etc/homehub-backup/backup.env`, and a **sim** build installs none — it is a
production `site/` file. The library lane therefore logs the right band and the
right check id into a journal nobody reads; the backup lane exits before it
checks at all.

**NEITHER IS A PRODUCT DEFECT, and this entry nearly said they were.** Both are
correct for an unprovisioned box, and on a real hub the materialised
`backup.env` carries `NAGLIGHT_FEED_CONTAINER=tracker`, which runs the POST
*inside* the container against its own loopback — necessary because the tracker
is bridge-only by ratification (D2: no host publish, since trusted headers are
forgeable if the port is open). Measured on the gate hub, confirming that design
rather than contradicting it: from the host, `127.0.0.1:8787` is refused,
`tracker:8787` does not resolve, `docker port tracker` is empty. The mechanism
was already right; only a SIM equivalent was missing.

`render_sim_gate_backup_env` renders one into `deploy-payload/sim-gate/` on lab
builds only, and firstboot installs it `0600 root:root` — **refusing outright if
a real `backup.env` already exists**, because a gate fixture on a provisioned hub
would repoint a household's drive reports at a test identity.

**`NAGLIGHT_USER` is the load-bearing field.** The tracker runs multi-user, so a
report attributed to anyone but `PANEL_USER_SUB` lands in a different user's data
directory and the panel never sees it — a red lane posted to the wrong user is
indistinguishable from no lane at all, which is the exact failure being fixed.
The value is read back out of the RENDERED sim `.env` rather than from the
overrides, so the two cannot disagree, and a lab build with no `PANEL_USER_SUB`
is refused.

---

#### THE GUARD THAT WAS DROPPED, AND COST AN INSTALL THE SAME NIGHT

`recreate-hub-vm.ps1` **refused to boot an ISO older than the newest commit** —
"the mechanised form of *verify the artifact, never the exit code*".
`Start-A19Gate.ps1` replaced it and only **printed** the build timestamp. Hours
later, with the builder fixed and the ISO not yet rebuilt, `-Stage Hub` booted
the pre-fix image and began a 25-minute install of the defect it was meant to
test. A timestamp a human is expected to read and compare is not a check.

Restored, and judged against the newest commit touching **`vmtest/` or
`stack/`** rather than any commit — a docs-only change does not invalidate an
ISO, and refusing on one would train the operator to pass `-AllowStaleIso` every
time, which is how a guard becomes a keystroke.

**Also:** `Set-VMFirmware -SecureBootTemplate MicrosoftUEFICertificateAuthority`
succeeded on one VM and, twenty minutes later on the same host with the same
parameters, failed on the next with *"matches none of the secure boot
templates"*. The NAME LOOKUP is the flaky part, not the template, so the call is
now tried by name and then by the same template's well-known id — one thing
attempted two ways, announced when it takes the second route. Secure Boot stayed
ON for the panel (`shim-signed` and `grub-efi-amd64-signed` were installed), so
there is no `-DisableSecureBoot` delta to record.

---

**SUITES:** `test-hub-seed.sh` 47 → **54 passed, 0 failed, 0 skipped**;
`test-wall-builder.sh` 107 → **110 passed, 0 failed, 0 skipped**. One existing
wall case had to be corrected rather than the guard: section 4c asked for a lab
build on the `.invalid` default, which is now refused — the case predated the
lesson.

**WHAT THIS RUN STILL DOES NOT PROVE:** TLS trust (the panel runs with
`--ignore-certificate-errors`), Wi-Fi, either CIFS mount, and — until the
rebuild is booted — that the two fixes above are delivered by the image rather
than by hand. `wall-sync` and `wall-sync-frame` failed on the panel, which is
correct: both UNCs are `.invalid` and media is out of scope for this gate.

---

### DRIVER — G1 — Round 1 — 2026-08-05 (A19 PASSED, unaided, and three open items close — not five)

**EVERY "Done when" ROW IS MET, WITH NO HAND-PATCHING.** The re-run booted both
ISOs built from the committed tree at `0c735f2` and needed nothing done to
either box.

| §3 Done when | |
|---|---|
| Two VMs boot from tracked-script ISOs, **no hand-patching** | **YES.** `/etc/hosts` on the panel is clean; `/etc/homehub-backup/backup.env` was installed by firstboot, which logged that it did. |
| A screen capture of the **panel** shows both drive checks **red** | **YES** — `D:\vmtest-out-wall-a19\panel-a19-GATE.png`, captured with `grim` from inside the cage session. |
| The three guard assertions | **ALL THREE.** See below. |
| The record | this entry. |

**DEFECT #16 IS FIXED IN THE IMAGE, and the measurement is the one that matters
— through the STUB, not against the server:**

```
resolvectl query wall.vmtest.sim   ->  10.99.7.10   -- link: eth1
getent hosts wall.vmtest.sim       ->  10.99.7.10 wall.vmtest.sim
GET /            -> 200
GET /api/today   -> 200
```

`-- link: eth1` is the part worth reading: the answer came back over the **lab
leg**, via the scoped search domain, exactly as `apply_sim_lab_netplan` intends.
The previous run's `ERR_NAME_NOT_RESOLVED` is gone and no line replaced it.

**DEFECT #17 IS FIXED IN THE IMAGE.** `firstboot` logged *"SIM GATE: installed a
test backup.env so the two drive-presence lanes REPORT"*, the file is
`-rw------- root:root`, and both lanes posted without being asked. `/api/today`
carried, per ITEM:

```
ambient: red   score: 100
  library-drive-present      red    Library drive mounted
  backup-drive-present       red    Backup drive mounted
```

**THE THREE GUARDS (§3 step 7), the half that matters because it was ratified as
security-critical:**

| assertion | result |
|---|---|
| a request from an address that is not `PANEL_IP` gets 403 | **PASS** — `403 wall: panel only` from the hub itself; `200` from the panel |
| a client-supplied `X-Forwarded-User` is **replaced**, not honoured | **PASS** — a forged sub returns byte-identical data to the baseline, so the injected identity won. Repeated the test with the header sent **twice**, which is the shape a single `delete` would miss: also identical. `X-Forwarded-Email` (plain delete, no injected value): identical. |
| `WALL_PORT` reachable only on the LAN leg | **PASS** — `ss -ltn` shows `10.99.7.10:8443`, not `0.0.0.0:8443` |

The forgery test is a real measurement of a behaviour a reader cannot see: Caddy
applies header ops in a fixed order regardless of writing order, and
`header_up <Name> <value>` is a SET, which replaces every existing value. That
reasoning has been in the Caddyfile's banner since WI-10.14 and has now been
checked against a live panel rather than against the source.

**DEFECT #13 ON BOTH INSTALLED BOXES:** 0 group- or world-writable paths under
`/opt/homehub` (was 230) and `/opt/wall-panel` (was 215); `stack/.env`
`-rw------- root:root`; `/opt/homehub/sim-gate` `drwx------ root:root` and
unreadable to the unprivileged user, which is `0c735f2` proven on a box.
`chrome-sandbox` survives `-rwsr-xr-x` after NTFS → ISO → tar → ext4.

---

#### A CORRECTION: this run closes THREE open items, not five

Earlier entries — and the handoff — said one install would speak to OI-17,
OI-19, OI-20, OI-21 and the hostname fix at once. **That was wrong, and the
reason is structural rather than incidental: three of those five can only be
exercised by a PRODUCTION build, and A19 is a SIM gate.**

| item | status after this run |
|---|---|
| **OI-17** — the wall ISO boots and the shell paints | **CLOSED.** It painted, unaided. |
| **OI-21** — `/opt/homehub` world-writable | **CLOSED.** Measured on two installed boxes. |
| **OI-22** — run the A19 gate | **CLOSED.** |
| **OI-19** — the hub's site-staging late-command | **STILL OPEN.** A sim build has no `site/`, so 4b took its documented no-op branch. The *refusal* path — a `production` marker with no `site/` — remains unexercised on real media, which is exactly what OI-19 is. |
| **OI-20** — the caddy-readability assertion on `config.json` | **STILL OPEN**, and for the same reason: the check is conditional on a production `config.json`, and a sim build ships none. |
| the production meta-data hostname fix | **STILL UNVERIFIED** — the sim path stamps `homehub-vmtest` by design, so this run says nothing about the production branch. |

**What actually closes those three is a PRODUCTION build**, which needs
Personal's materialiser to emit a full `site/` — and that is blocked on
**C17**, one `smbpasswd -a share` at the AWOW. Not a design gap; a task at a box.

---

**STILL NOT PROVEN, and every one of them deliberate:**

- **TLS trust.** The panel runs `--ignore-certificate-errors` because the hub
  serves from an internal CA whose root cannot exist before the panel's ISO is
  written. The gate proves the render path, not the trust path.
- **Wi-Fi** — `macaddress: permanent`, powersave-off, and the DHCP reservation
  the `/32` is keyed to. Hardware-only (C7), by construction.
- **Either CIFS mount.** `wall-sync.service` and `wall-sync-frame.service` are
  both `failed` on the panel, which is **correct**: both UNCs are `.invalid` and
  media is out of scope for this gate. Nothing has ever been mounted with either
  credential; that is OI-18's standing gap and this run does not touch it.
- **A production panel or hub.** Everything here is a sim image.

### DRIVER — G1 — Round 1 — 2026-08-07 (an installed hub, interrogated: sudo could never have worked, and the offline install stopped at the install)

**The first lab run whose VM outlived it.** HomeHub's endpoint gained a
`Hub-Keep` variant, so for the first time an installed hub was still running
when the launcher finished and could be asked questions instead of being
destroyed with its evidence. Three defects came out of one box, and none of them
was reachable by review.

**1. `sudo` could never have succeeded, on either image.** `identity.password`
is `"!"` — a locked hash, correct for a key-only box — and neither image ever
created a sudoers drop-in. So `sudo` demanded a password that cannot exist:

```
sudo: a password is required
```

No `apt`, no `systemctl`, no `/etc` edit, and `healthcheck.sh` could not read
its own root-only `.env`. Cockpit and the console were dead for the same reason,
both being PAM against the same locked hash. **This is the standard cloud-image
posture with one of its three parts missing** — Ubuntu's `cloud.cfg` ships
`lock_passwd: True` *together with* `sudo: ["ALL=(ALL) NOPASSWD:ALL"]`, and
subiquity's `identity:` block has no way to express the second, so adopting the
posture silently dropped it. Fixed in both images at late-command 3c-bis,
validated with `visudo -cf` before installing because a malformed sudoers file
on a locked-password box is the no-way-in state that cost a GRUB rescue on
2026-08-06.

Worth recording plainly: **on the hub this changed no exposure.** `hub` is in
the `docker` group, and the docker socket is root by design — demonstrated on
the live box by reading the 0600 root:root `.env` from a container as uid 0,
with no password. What the drop-in adds is auditability, since `sudo` journals
every command and `docker run` does not. On the **panel**, which has no docker
group and no Cockpit, it is the difference between maintainable and
reimage-only: `systemctl reboot` — the one action unattended-upgrades
periodically requires — was not remotely performable at all.

**2. The offline install was defeated by configuration, one step past the
install.** `export-images.sh` hardcoded `PROFILE_ARGS=(--profile ntfy)` while
the materialised hub `.env` carried
`COMPOSE_PROFILES=ntfy,immich,immich-ml,jellyfin,finance-auditor`. Nine images
baked; fifteen needed. First boot went to `registry-1.docker.io`, the
`jellyfin` pull failed, `compose up -d` is all-or-nothing, and
`homehub-firstboot.service` exited 1 — so **DNS, Caddy, the tracker and Actual
never started because one OPTIONAL image was unreachable.** Confirmed from
outside: `:5380`, `:3001` and `:8081` all refused.

Note what was *not* wrong: the refusal machinery was sound and always had been
(a missing local-only image and an unpullable tag are both `die`, not warnings).
It was being asked the wrong question. It now derives the profile set from the
`.env` it is bundling, so drift is a refused build. This closes OI-7(a) against
its original interpretation.

**3. `Wait-GuestAddress` reported failure about a box that was up** (HomeHub's
launcher; recorded here because it is what made 1 and 2 look like a dead
install). It watched only the host neighbour table for the VM's MAC — an
ARP cache that fills when the host *talks* to the guest, and nothing was talking
to it. Sixty minutes of nothing, about a hub answering ping and ssh at its
reservation throughout. It now probes tcp/22 at the expected address first
(15 ms, measured) and keeps the neighbour scan as the fallback it should always
have been, for the DHCP-gave-a-different-address case.

**What this run did prove**, and it is the assertion the whole 2026-08-06 effort
was for: **8004 MB installed with the network adapter disconnected**, and SSH
accepting the operator key unattended afterwards.

### DRIVER — G1 — Round 1 — 2026-08-08 (the panel installs at last; and the mount check could never have passed)

**Three lab runs, both machines installed offline, five defects.** The
2026-08-07 entry above covers the first two; this one closes the day.

**THE PANEL INSTALLED, END TO END, FOR THE FIRST TIME.** `wall-firstboot.service`
Result=success, the shell artifact present at `/opt/wall-panel/app/wall-shell`, a
kiosk session running, sshd enabled — and it did it with the VM's network adapter
disconnected, finishing **4.8 minutes** before the cable went back in. The hub
did the same with 1.1–1.2 minutes of margin. `Assert-OfflineInstall` has now
returned PASS on both targets, which it had never done for either.

**AND `sudo` WORKS**, verified on a rebuilt image: `SUDO-OK: the drop-in landed`.
Both boxes are remotely maintainable rather than merely readable.

**DEFECT: `provision-mounts.sh` could never find a data drive.** It tested
`[ -e "$dev" ]` where `$dev` is the fstab line's FIRST FIELD — `LABEL=Library`, a
mount spec, not a path. Always false. So the mount below it was never attempted,
and `provision-samba` — which refuses to export an unmounted path, correctly —
killed firstboot with a FATAL. **Every first boot, on every box, since the fstab
moved to `LABEL=` for A23.** Measured both directions on a live hub:
`[ -e "LABEL=Library" ]` false, `blkid -L Library` → `/dev/sdb2`, and
`mount /srv/library` succeeded instantly by hand.

It hid because systemd's fstab-generator mounts these on the NEXT boot anyway, so
the drives are up by the time anyone logs in — only firstboot, the one pass that
provisions Samba, ever saw them missing. **A lab run that reconnected LATE saw
mounted drives and passed; the run that reconnected promptly did not.** The
defect was masked by the timing of the observer, and fixing the heartbeat is what
exposed it. `resolve_mount_spec` now handles LABEL=/UUID=/PARTLABEL=/PARTUUID=
and bare paths, via `blkid` (which probes devices rather than waiting on udev's
symlinks) with a bounded wait — the real hub's drives are USB and slow, which the
fstab already admits with `x-systemd.device-timeout=15s`.

**DEFECT: the image bake read the wrong .env, twice.** First the profile list was
hardcoded `--profile ntfy` here; then, once that was fixed, `Build-VentoyStick.ps1`
called this script with no `--env-file` at all, so it resolved against
`stack/.env.example` (COMPOSE_PROFILES empty) while the ISO shipped a
materialised `.env` enabling five. Nine images baked, fifteen needed, and the six
missing were exactly the six that failed to pull. `docker compose up -d` is
all-or-nothing, so one unreachable OPTIONAL image stopped DNS, Caddy, the tracker
and Actual from starting at all.

Twice in one day, **the thing that DECIDED the image set was not the thing that
RAN the box.** `export-images.sh` now writes `profiles.stamp` recording which
`.env` it was asked about, and `stage_images_into_payload` compares it against
the `.env` staged beside it, naming any enabled-but-unbaked profile. It warns
where the apt stager dies, deliberately: a short image set has a degraded mode
and refusing would break every deliberately-narrow bake (the sim, the wall
target, `EXTRA_PROFILES`). What it must never be is silent, which it was.

Consequence to weigh, recorded for the Owner: the hub ISO went from 3.8 GB to
**5.8 GB**, because `immich`, `immich-ml`, `jellyfin` and `finance-auditor` are
genuinely enabled in `config.homehub.psd1` and are now genuinely baked. That was
always the configuration; it simply never shipped.

**NOT YET RUN:** the `provision-mounts` fix has never executed inside a firstboot.
The next `-Stage All` is what settles it.

### 2026-08-09 — three defects from lab run `20260809-145118`, and a static guard

The run failed **6 checks of 13**. Two defects, both in `firstboot`, plus a
third found while diagnosing them. All three fixed here; all three proven on the
live lab hub before any rebuild.

**1. `homehub-firstboot.service` SIGTERMed itself at step 1b.**

```
20:21:17  1b: carving /var/lib/docker onto its own LV (15993 free extents)
20:21:17  Logical volume "docker-data" created
20:21:18  Main process exited, code=killed, status=15/TERM
```

The unit declared `Requires=docker.service`, and step 1b must
`systemctl stop docker` to move `/var/lib/docker`. systemd propagates stops
across `Requires=`, so the unit ordered its own death. **Two files each correct
in isolation; the pair fatal** — which is why no reader caught it.

Everything else followed: 0 of 15 image tars loaded → no stack → no Technitium →
no LAN DNS → the panel's firstboot failed too (it resolves everything by name) →
both kiosk interconnect checks failed → `/srv/library` and `/mnt/backup-drive`
never mounted, because the generated fstab is written later in the same script.
**One cause, six failing checks.** Fix: `Wants=` + `After=`. `After=` was always
what delivered "runs after docker is up"; `Requires=` only added "and dies with
it".

**2. The step could not repair itself, which was worse.** Its idempotence guard
asked *"does the LV exist"* — its own output. The kill left a formatted,
unmounted LV, so every later boot said "already exists — nothing to do" and
returned. `homehub-firstboot.service`'s own header warns that a step must ask of
an INPUT, never of its own output; this was that warning made flesh. Re-running
the script — the documented repair for every other step — was the one thing that
could never work. The guard now asks the END STATE (*is `/var/lib/docker`
mounted from that LV*) and **resumes** from wherever the last attempt stopped,
re-running `mkfs` only if the volume has no filesystem.

**3. The hub had no `cifs-utils`.** `packages.list` gave it `samba` (serve) and
`smbclient` (test) but never `mount.cifs` (**mount**). The kernel module ships
in the stock image, so `mount -t cifs` exists and fails misleadingly:
`credentials=<file>` is parsed by mount.cifs, not the kernel, so no credential is
sent (`STATUS_ACCESS_DENIED`); and kernel-side name resolution needs the same
helper's upcall, so a resolvable FQDN gives `Unable to determine destination
address` — printed as **`No route to host`**. HomeHub had spent two runs
concluding that a Mini-serv share was missing, while `smbclient` on the same box
read that share throughout. Same class as the 2026-08-07 `smbclient`-is-not-in-
`samba-common-bin` miss.

**A static guard, so this class fails the build instead of a three-hour run.**
`validate_config.py` check **5b**: no firstboot script may `systemctl stop` a
unit its own unit `Requires=`. Verified both directions — restoring `Requires=`
makes it FAIL with the explanation, the fixed tree PASSes.

**Verified first-hand:**

- `bash -n stack/autoinstall/firstboot.sh` clean; `python scripts/check.py`
  (gate G1) **PASS** — config-validate, registry-integrity, doc-navigability.
- **On the live lab hub, in its broken state:** the repaired unit + script
  resumed correctly — `1b: ubuntu-vg/docker-data exists but /var/lib/docker is
  not mounted from it — finishing the move`, then `1b: /var/lib/docker is now
  36.6G, separate from root`. firstboot ran to `Result=success`, **15 of 15
  images loaded**, the full stack came up healthy, and fstab gained all three
  mounts. After `cifs-utils`, the previously failing
  `mount -t cifs //mini-serv.<domain>/Snapshots -o credentials=…` **mounts**, and
  `backup.sh --dry-run` reaches `ingest: done`.
- HomeHub's `Invoke-LabVerify.ps1 -IncludeExternal` then reported **3 failures
  of 146** (from 5), all three lab-fixture limits rather than product defects.
- The **panel's** media-cache LV (unchanged this round) proved its purpose: the
  43 GB frame overflow that filled run 2's ROOT filesystem now lands in the
  cache LV — root 47%, cache 97%. Contained.

**NOT proven:** none of this has been through a clean image build. The lab hub
was patched in place, so what is demonstrated is *the fixes work*, not *a fresh
install produces them*. The next full lab run is what closes that.

**Also this date, unrelated to the run:** compose + `.env.example` plumbing for
NagLight's one-way traceability mirror (`TRACKER_MIRROR_*`), shipping blank/OFF.
See HomeHub `DECISIONS_RATIFIED.md` → *Ratified 2026-08-09*.

### 2026-08-09 (later) — the nightly backup had never worked: four stacked defects

Chasing one missing directory found four independent faults, **each of which
alone produces the identical error line** `path source missing for …`. The Owner
ruled the behaviour first: *"the setup should include all folders, but tests
should also verify this exact item. A missing folder shouldn't block the backup
of other folders."*

1. **CRLF in the emitted config** (HomeHub `Materialize-Deploy.ps1`). Its comment
   promised LF — *"CRLF or a BOM breaks both on the box"* — and nothing ever
   converted anything; output was LF only because the templates were. A
   multi-line knob value from a CRLF `.psd1` carried `\r\n` through, so
   `backup.env` had exactly 13 CR bytes (the internal breaks of the 14-line
   `BACKUP_SOURCES`) while `.env` and `cifs.creds` had none — which is why it
   read as backup-specific. Every path ended in `\r`, and **`\r` is invisible in
   a log**, so the error named a path that looked correct.
2. **`load_env_file` read only the FIRST of fourteen sets** (`common.sh`). It is
   line-based on purpose (sourcing would expand bcrypt `$2a$14$…`), and stripped
   quotes only when a value opened and closed on one line. Line 1 kept its
   opening quote — naming the set `"media-music`, which also stopped its
   `.exclude=` line matching — and every later line was rejected as a malformed
   key and skipped **silently**, because `continue` on a bad key is correct for
   comments and indistinguishable from this. Now supports multi-line quoted
   values; the bcrypt-literal property is preserved and unit-tested.
3. **No `volume:` source ever resolved** (`volume_mountpoint`). They name compose
   volumes unprefixed (`actual_data`); docker's are project-prefixed
   (`stack_actual_data`). Only visible once (2) let them be read. Resolved via
   compose's own `com.docker.compose.volume` label — **not** a suffix match,
   which was the first attempt and matched both `stack_actual_data` and
   `stack_finance_actual_data`: it would have backed up a finance volume as the
   budget one, silently. A second bug hid here too: `docker volume inspect -f` on
   a missing volume writes an empty line to stdout, so echoing the probe through
   produced `"\n/var/lib/docker/…"` and failed `[ -d ]` for volumes that HAD been
   found.
4. **`NonDocs/Media/Music` was created by nothing.** Step 4a-pre-2 pre-creates
   library dirs only for Docker bind-mount sources; nothing containerised serves
   music (Samba + CIFS to the panel), so no bind mount names it. New step
   **4a-pre-2b** pre-creates every `BACKUP_SOURCES` path, **gated on
   `/srv/library` being a real mountpoint** — it mounts `nofail`, so an
   ungated `mkdir -p` would build a convincing fake library on the system disk
   and hide a missing drive.

**Behaviour change (`backup.sh`), per the ruling.** A missing source now SKIPS
its own set and the run continues; it still posts `ok=false`, still exits
non-zero and still names the set — after protecting the data it could reach.
Previously the first absent path ended the run, so **one directory cost every
other set, including all five Private trees**. Deliberately narrow: only
"source absent" is tolerated; rsync/archive/integrity failures still abort,
because those mean the machinery is broken rather than an input being absent.

**Verified first-hand on the live lab hub, in sequence:**

| | sets seen | verdict |
|---|---|---|
| before | 1 of 14 (`"media-music`) | abort |
| after CRLF fix | 9 path sources, names clean | abort at volumes |
| after parser + volume fix | **14 of 14** | skips `media-music`, archives 13, exits 1 |
| after creating `Music` | 14 of 14 | **dry-run exits 0** |

`bash -n` clean on `backup.sh`, `common.sh`, `firstboot.sh`; the multi-line
parser unit-tested (multi-line value, parsing resumes after it, bcrypt stays
literal, comment-stripping intact). HomeHub's hub suite now reports
**ALL CHECKS PASSED — 92 passed, 9 not proven, 0 failed**, including the new
`every backup source exists: 14 of 14 reachable (TC-H-M10)`.

**Tests added** (HomeHub `verify-hub.sh` + `LAB_TEST_PLAN.md`): **TC-H-M10**
reports a COUNT (`N of N`) rather than stopping at the first failure — it would
have shown "1 of 14" immediately — and **TC-H-M11** covers skip-one-not-all.

**NOT proven:** none of this has been through a clean image build.

### 2026-08-27 — the GUI is removed, because IceDrive never needed it

The Owner: *"GUI can be removed... from the image and from the box, along with
the auto-desktop startup. The HomeHubDesktop can stay with clear comments that
this is no longer required."* And, on the credential: *"ideally that just
becomes another secret pair"* — the fresh ruling open-items E1 asked for.

**The whole graphical layer existed for one app, and the premise was never
checked.** `stack/remote-ui/README.md`, open-items E1 and SR-015 all asserted
that IceDrive's Linux client is "GUI-only - no headless daemon, no CLI". Three
things were built on that: nine packages in `packages.list` (defaulted ON only
the day before, 2026-08-26), an AppImage pin, and
`homehub-desktop-session.service`, which logged a desktop in at boot so the GUI
client would keep syncing with nobody connected. E1's own text records why
nobody found out: *"Nothing can be built until there is a live client to
inspect."*

**Measured on the real binary this date, with no account needed:**

- `IcedriveCLI-v3.62` links **no** X11, xcb, Wayland or Qt GUI library at all -
  only `libfuse.so.2`, `libz`, `libglib-2.0`, `libstdc++`, `libm`, `libgcc_s`,
  `libc`. It behaves identically with `DISPLAY` unset, dead, or live.
- `-login <user> -password <pw>` is fully non-interactive: with stdin closed it
  reaches the vendor API and returns a real verdict (`code 1005`, invalid email
  or password).
- **The password cannot be kept off argv.** Three routes tried, all three answer
  `cli: no password given`: a pipe on stdin, a pty via `script`, and omitting
  `-password` so it prompts. Pre-seeding is not available either - the stored
  credential is encrypted by the app itself.
- **`icedrive_sessId` is NOT the persisted session**, contradicting what the
  remote-ui README claimed on 2026-08-27's own predecessor commit: the conf gets
  a fresh one after a login that FAILED. The key that matters is
  `icedrive_stored_cred`.
- **`--help` is not the option list.** The binary also parses `-hash`,
  `-clearsettings`, `-newsync`, `-sync`, `-share`, `-publink`, `-history`,
  `-requestfiles`, `-mount`, `-quit`, `-dir`, `-startup` and more. Most are IPC
  to a running instance and no-op without one (verified: `-newsync` with no
  instance falls straight through to the username prompt).
- **Sync pairs live in the ACCOUNT**, not on the box: `sync-list-pairs` carries
  `path_local`, `path_remote`, `folder_id`, `syncId`, `time_last`, `ini_done`,
  with `processSyncPairList` and `runSyncThreads` behind it. So a reimaged box
  fetches the list rather than starting empty - which contradicts the re-setup
  checklist this project has been carrying. **Creating** a pair from the CLI is
  not supported: the path is `showSyncDialog -> ... -> sync-pair-add`, and this
  binary has no toolkit to draw a dialog with.
- **2FA is a hard stop**: `2FA method isn't supported in CLI`.

**Changed, in this repo:**

- `packages.list`: the nine names moved to a re-created
  `packages.optional.list` - baked into `/opt/homehub/apt`, installed by
  nothing, so `HomeHubDesktop.cmd` still works on an offline hub.
  `libfuse2t64` deliberately STAYED installed: the CLI needs `libfuse.so.2`.
- `stack/remote-ui/`: `homehub-desktop-session.{sh,service}` **deleted**;
  `setup-remote-ui.sh` reverted to a pure manual opt-in that installs no vendor
  app and creates no boot session; README rewritten, listing the three claims
  that turned out to be wrong rather than quietly deleting them.
- `stack/icedrive/` (new): the pinned CLI, `setup-icedrive.sh` (install, ONE
  non-interactive sign-in, mount unit), `homehub-icedrive.service`, and a README
  that separates what was measured from what is still unproven.
- `icedrive.pin` now pins the **CLI** (9.5 MB) instead of the 118 MB AppImage;
  `export-icedrive.sh` and `stage_icedrive_into_payload` follow it.
- firstboot step 6c provisions IceDrive instead of a desktop, and is a no-op
  with no credential.
- `.env.example` gains `ICEDRIVE_USER`/`ICEDRIVE_PASSWORD` **and
  `OPERATOR_PASSWORD`** - see below.

**A defect found on the way, and it is the useful kind.** `OPERATOR_PASSWORD`
had **never been emitted by the pipeline**. HomeHub's `FieldSchema.psd1` had the
knob, `setup-remote-ui.sh` and the deleted session script both read it, and
`stack/.env.example` never carried the KEY - and the env emitter only fills keys
a template already names. So the row was inert, nothing reported it, and the
value on the bench box had been put there by hand during a bench session. **A
knob with no template line is emitted by nothing and reported by nothing.**

**Verified first-hand:**

- `bash -n` clean on `firstboot.sh`, `setup-remote-ui.sh`, `setup-icedrive.sh`,
  `export-icedrive.sh`, `lib/common.sh`; `python3 scripts/check.py` (gate G1)
  **PASS** - config-validate, registry-integrity, doc-navigability.
- `export-icedrive.sh --from` verified, cached and staged the CLI (9.5 M,
  version 3.62) against the new pin.
- HomeHub `Materialize-Deploy.ps1 -Preview` reports **all knobs resolve**, with
  `OPERATOR_PASSWORD` now sourced `store` and the two `ICEDRIVE_*` knobs
  resolving blank from the template (the shipped OFF state).
- **On the live bench box:** units disabled, `xrdp xorgxrdp xfce4-session xfwm4
  xfce4-panel xfce4-terminal thunar xvfb freerdp2-x11` removed with
  `--autoremove`, the running session killed, session detritus cleaned, the
  stale `xrdp-sesman` failed unit reset. Afterwards: **tcp/3389 does not listen,
  no Xorg process exists, `libfuse2t64` is still installed, 15 containers
  running with none unhealthy, and `systemctl --failed` is empty.**

**NOT PROVEN, and it is the whole remaining question:** nobody has ever signed
the CLI in. Whether the session persists (so the mount unit can carry no
password), whether the mount survives a reboot, whether the CLI acts on the
pairs it can list, and whether root can traverse a `hub`-owned FUSE mount are
all open. None of it was testable without an account. `setup-icedrive.sh` and
the mount unit are therefore written but **have never run against a real
credential** - they ship inert, and the icedrive README names the five questions
one login session would settle.

**Also NOT proven:** none of this has been through a clean image build.

### 2026-08-27 (later) — the GUI comes back, and the deployer gets a light core

The morning's direction was reversed the same day, and the reversal is more
useful than either endpoint.

**The reversal.** IceDrive's headless CLI works — proven on the bench box with a
live account: signed in non-interactively, the session persists so the password
is needed exactly once, and it FUSE-mounts with no display at all. It was still
rejected. It is a **mount** client, not a **sync** client (on a live
authenticated session it made no sync request whatsoever), and the vendor
documents it essentially not at all: one unanswered community thread asking for
the option list, and a release announcement that never mentions a CLI build. The
Owner: *"if there is no documentation of this, it's likely safest just to drop
back to the desktop."* The supported client over the clever one. The CLI stays
as `ICEDRIVE_MODE='cli'` rather than being deleted.

**THE FINDING THAT MADE THE WHOLE DAY WORTH IT.** Restoring the desktop meant
launching the AppImage, which nobody had ever done on a hub. **It does not
start.** Eleven runtime libraries are missing from every image this repo has
ever produced:

```
libwebpmux.so.3 / libwebpdemux.so.2 / libXss.so.1      -> aborts before Qt starts
libxcb-icccm.so.4  libxcb-image.so.0  libxcb-keysyms.so.1
libxcb-render-util.so.0  libxcb-shape.so.0  libxcb-xinerama.so.0
libxcb-xkb.so.1  libxkbcommon-x11.so.0
    -> "Could not load the Qt platform plugin xcb ... even though it was found",
       then a core dump
```

Each hides behind the one in front of it, one per launch. The AppImage bundles
Qt but not the system libraries Qt's xcb plugin links against, and the failure
names a PLUGIN rather than a PACKAGE - so it reads like a corrupt download.
Only `libfuse2t64` was ever listed. **Every version of this project that said
"RDP in, sign in to IceDrive, create sync pairs" was describing a step that
could not have completed**, and nothing reported it because nothing had ever
launched the app. Same class as the wall image's Electron soname check, which
already exists and which the hub has no equivalent of - that check is open work.

**The structural change (the Owner):** *"Deployer I'm okay with as long as it
defaults IceDrive and remote desktop to off from its side, and gets configured
to active from the HomeHub."* An optional feature has TWO halves that happen at
different times in different repos, and they used to be independent:

|  | carriage (build, dev PC) | activation (first boot, box) |
|---|---|---|
| desktop | `packages.optional.list`, baked only when `BAKE_OPTIONAL=1` | `REMOTE_UI_ENABLED=true` |
| IceDrive | `icedrive.pin` + `export-icedrive.sh --artifact` | `ICEDRIVE_MODE=appimage\|cli` |

Both now derive from ONE declaration in HomeHub's `config.homehub.psd1`, and
`Materialize-Deploy.ps1` refuses to emit an activation whose carriage is absent.

**Changed here:**

- `packages.list` is **28 core names** - no X, no RDP, no FUSE shim, no
  IceDrive. `packages.optional.list` carries **22**: the RDP/XFCE set, the
  eleven Qt/xcb libraries, and `libfuse2t64`.
- `export-apt.sh` bakes the optional set only on `BAKE_OPTIONAL=1`, which this
  repo never sets.
- `.env.example`: `REMOTE_UI_ENABLED=false`, `ICEDRIVE_MODE=off`,
  `ICEDRIVE_MOUNTPOINT=/srv/icedrive`.
- firstboot step 6c reads both knobs, does nothing when off, and itself refuses
  `appimage` without a display (a payload can be edited by hand where no dev-PC
  gate can see).
- `icedrive.pin` pins BOTH artifacts (`APPIMAGE_*`, `CLI_*`);
  `export-icedrive.sh --artifact appimage|cli|off` stages exactly one and writes
  `artifact.kind`, so `stage_icedrive_into_payload` picks the payload directory
  by KIND rather than by sniffing a filename.
- `homehub-desktop-session.{sh,service}` restored from history; the
  AppImage-installing `setup-remote-ui.sh` restored and its header rewritten for
  the new model.

**Verified first-hand:**

- gate G1 **PASS** (config-validate, registry-integrity, doc-navigability);
  `bash -n` clean across firstboot, both setup scripts, the session script and
  all three `vmtest` scripts.
- `export-icedrive.sh` staged `off`, `appimage` and `cli` correctly, each with
  the right `artifact.kind`.
- HomeHub's `Materialize-Deploy -Preview` resolves every knob with the extras
  on, and **refuses all three bad shapes**: an unknown mode; `appimage` without
  `REMOTE_UI_ENABLED`; `appimage` with `APPIMAGE_SHA256` blank.
- **On the bench box:** desktop reinstalled, the eleven libraries resolved one
  at a time until Qt initialised (8 plugins load, no missing sonames), the
  AppImage autostarted inside the boot-time session with nobody connected, and
  the CLI mount stopped and disabled so two clients could never run together.
  Final state before shutdown: xrdp active+enabled, desktop-session
  active+enabled, IceDrive GUI running, 15 containers up, 0 unhealthy, 0 failed
  units.
- **The GUI inherited the CLI's login.** Both clients share
  `~/.config/Icedrive/Icedrive.conf`; the GUI came up already authenticated and
  mounted, listing real cloud content, on a box where only the CLI had signed
  in. It also writes `icedrive_stored_cred`, which the CLI never does - the key
  this layer originally guarded on, wrongly.

**NOT PROVEN, and it is the first thing to do next:** no clean image build has
been through the new carriage path. The bench box was restored **by hand from
the Ubuntu archive**, not from a baked repo - its `/opt/homehub/apt` still holds
238 debs with none of the GUI set, and no apt source was ever registered for it
(that box predates 3c-keep). So `BAKE_OPTIONAL=1`, the optional-set offline
resolve, and firstboot's knob routing have all been reasoned about and none has
been executed end to end.

**Also not proven:** IceDrive has **zero test cases** at any tier - the largest
single coverage gap in HomeHub's app table.

**The box is powered off** (2026-08-27 04:53 UTC), stack stopped cleanly, for a
physical move.

### 2026-08-27 (night) — both images built, and the artifact is now interrogated

The morning's work was reasoned about; tonight it was **run**. Both ISOs were
built from the light-core arrangement and then asserted. Three defects fell out,
and the thing they have in common is the point of the entry: **every one lived
in a code path that had never executed**, and every one was invisible to a build
log that reported success at every step.

**What was built, and what it proves:**

| | ISO | assertions |
|---|---|---|
| hub  | `vmtest/.out/repacked.iso` — 6.13 GiB | **14 / 14** |
| wall | `vmtest/.out-wall/wall-repacked.iso` — 3.41 GiB | **5 / 5** |

**The wall ISO had never been built in production form before.** It built clean
on the shared code, which is what the carriage split was supposed to buy and had
never been shown to.

**Defect 1 — baking the optional set silently dropped two systemd packages, and
apt's proposed fix was to delete the box.** `systemd-resolved` and
`systemd-timesyncd` depend on `systemd (= <exact version>)`, so every bake pulls
the archive's current systemd (255.4-1ubuntu8.17) against the 24.04.4 base's
8.12 — which obliges apt to upgrade the version-locked siblings in lockstep. Two
were not in the repo, and the two failures look nothing alike:

- `systemd-sysv` absent → `init : PreDepends: systemd-sysv but it is not going
  to be installed`, and the offline resolve simply dies.
- `libpam-systemd` absent → **apt does not fail.** It plans to REMOVE it, and
  that cascades into `dbus-user-session`, `polkitd`, `packagekit`,
  `modemmanager`, `software-properties-common`, `snapd`, `ubuntu-standard`,
  `ubuntu-server`, `ubuntu-server-minimal`. Naming one package deletes the boot
  path. `export-apt.sh`'s `--no-remove` guard is the only thing between that
  plan and an ISO, and it earned its keep.

**The part worth carrying forward:** whether apt downloads a transitive
dependency **depends on what else is in the closure**. Measured both ways with
identical systemd versions — the core list alone fetched `systemd-sysv` (238
debs, resolve OK); the same core list plus the 22 optional packages did not (430
debs, resolve BROKEN). Nothing about the core list changed. So switching on an
unrelated feature can silently remove a core dependency, and it surfaces in a
message naming neither. Rather than iterate on error messages, the complete set
was derived from the ISO's own dpkg status — every base package carrying a
strict `systemd (=)` or `libsystemd-shared (=)` dependency — and written into
`packages.list` as a re-checkable invariant.

**Defect 2 — the AppImage never reached the ISO.**
`stage_icedrive_into_payload` wrote to `$out_dir/deploy-payload/…` while its four
sibling stagers all write to `$out_dir/`**`iso-root`**`/deploy-payload/…`. So the
118 MB AppImage was installed into a stray tree, `mkdir -p` created it without
complaint, `install` succeeded, the log said "staged", and the ISO carried
nothing. **Every signal was green.** Pre-existing from `ef826bc` and faithfully
preserved through the later rewrite; it survived because `icedrive.pin` was
unset, so the function always returned at its "nothing to stage" branch. **The
first build with an artifact to place is the build that found it.** The fix adds
the guard that would have caught it instantly: the payload ROOT must already
exist before anything stages into it, because `mkdir -p` will create any path
you ask for.

**Defect 3 — (that morning) the eleven missing runtime libraries.** Recorded in
the previous entry. The app could not have started even if it had arrived.

Any ONE of the three alone made *"RDP in and sign in to IceDrive"* impossible.

**What was added so this cannot recur quietly.** `vmtest/assert-iso-payload.sh`
(new) asks the **artifact**, not the build log — the house rule applied to the
one output that actually gets flashed. It asserts the payload root; that
`ICEDRIVE_MODE` is known and coherent with `REMOTE_UI_ENABLED`; that the
artifact the mode names is present, matches `icedrive.pin`, and agrees with its
travelling `.sha256`; that the OTHER client is absent; that the bake-only
packages are in the repo and NOT in `packages.baked.list`; and that
`OPERATOR_PASSWORD` is non-empty. It reads the expected state out of **the
payload's own `.env`**, so it cannot be pointed at the wrong expectation.

**One assertion was wrong the first time, and the correction is the more useful
half.** It required `systemd-sysv`/`libpam-systemd` to be **install-listed** —
the hub's fix — and so failed a wall ISO that was perfectly correct. The real
invariant is **repo membership**: if the repo carries a systemd newer than the
install base, every version-locked sibling must be present, *however it got
there*. The check now reports which, and the distinction is itself the finding:

- **hub** — named in `packages.list`. Robust.
- **wall** — **transitively**, via the graphical closure. It works today.

**A LIVE FRAGILITY, DELIBERATELY NOT FIXED.** The wall is one dependency change
away from the identical break the hub just had, and defect 1 is the proof that
transitive inclusion is not a property you can rely on — it changed under the
hub when an unrelated feature was switched on. Naming the two packages in the
wall list would cost nothing (they are already downloaded). It is left alone
because panel work is scoped to "next" and the wall build currently passes;
raised as **A27** in HomeHub's `open-items.md` for the Owner rather than decided
here.

**Verified first-hand tonight (re-run independently of the build):**

- `assert-iso-payload.sh` against both finished ISOs: **hub 14/14, wall 5/5**,
  exit 0 both times, run under `wsl -d Ubuntu`.
- `BAKE_OPTIONAL=1` bakes 432 debs (262 M) and **both** offline proofs pass —
  the core list and, for the first time, the bake-only optional set.

**A defect found in the wiring itself, and fixed here (2026-08-27, night).** The
assertion was wired into `Build-VentoyStick.ps1` as step 6b — and **it could
never have fired.** The committed file carried a literal **BEL byte (0x07)**
where `\a` had been intended, so the guard read
`Join-Path $DeployerRepo 'vmtest<BEL>ssert-iso-payload.sh'`; `Test-Path` was
always false and the step silently took its else-branch, printing a note that
the ISO is not verified. The counts above came from running the script by
hand — **the wiring had never executed.** Exactly the failure mode the whole
day was about: a guard that reports success by not running. Fixed in HomeHub
(two lines); `Test-Path` now resolves True and the file still parses clean.

**NOT PROVEN, and it is the whole remaining question:** all of this proves what
the images **contain**. **None of it proves they boot and converge.** No ISO
built tonight has been booted. `Get-VM` refuses without elevation, so
`VirtualHomeHub.cmd` needs the Owner's UAC prompt — the one test that closes the
gap and the one an agent cannot run here.

**Also still not proven:** IceDrive has **zero test cases** at any tier, the
largest single coverage gap in HomeHub's app table.

### 2026-08-27 (night, later) — an adversarial review of the new guard, and what it broke

The ISO checker written earlier this night was put through an adversarial review
(OpenAI `gpt-5.6-sol`, medium effort, read-only) with one instruction: refute,
do not praise. It landed. **A guard written to abolish vacuous green had four
vacuous-green paths of its own**, and the checker's own caller treated it as
optional.

**Findings acted on, in severity order:**

1. **The production build treated the checker as optional.** `$needed`
   (preflight) never listed `assert-iso-payload.sh`, and step 6b's else-branch
   printed a note and carried on. So a deployer checkout without the script
   built an ISO, skipped verification entirely, and step 7 copied it to the
   stick — **the same skip-quietly shape as the BEL byte fixed an hour
   earlier, one level up.** The file is now named in preflight (the run stops
   before building) and an absent checker **throws**.
2. **A supported configuration shipped a client that could not work.**
   `ICEDRIVE_MODE='cli'` with `REMOTE_UI_ENABLED='false'` is a shape
   `Materialize-Deploy` offers by name, but `BAKE_OPTIONAL` was derived from
   `REMOTE_UI_ENABLED` alone — and `libfuse2t64`, which the CLI links directly
   for its mount, lives in `packages.optional.list`. That combination baked no
   optional set at all. Carriage now follows **any** active extra.
3. **A missing activation knob was read as "the feature is off."**
   `: "${ICE_MODE:=off}"` turned "the .env never said" into "off", so a payload
   carrying neither client passed by expecting nothing — which **falsified this
   file's own claim** that reading the payload's `.env` means the check cannot
   be handed the wrong expectation. An absent knob is now a failure.
4. **A missing apt index was reported as proof no systemd upgrade existed.**
   If `Packages` could not be extracted, the script emitted
   `PASS the repo carries no systemd upgrade` — not being able to look, scored
   as having looked. Now a failure.
5. **The lockstep check compared names, not versions.** The dependency is
   `systemd (= exact version)`, so a sibling pinned at the *old* version
   satisfied `grep -qx` and still breaks the resolve. Versions are now compared
   against systemd's, and a `.deb` must back the index entry.
6. **`--target` accepted any string**, and everything that was not exactly `hub`
   selected the weaker wall checks — so `--target hubb` ran five assertions
   against a hub ISO and called it clean. Now validated.
7. **"The bake-only packages are present" was a four-name spot check** out of
   twenty-two, reported as `17/17`. The count read as exhaustive and was not.
   The checker now reads `packages.optional.list` **off the ISO** — the repo is
   copied into the payload, so the authoritative list travels with the artifact
   being judged — and checks **all 22**.

**Two claims the review made that did NOT survive checking**, recorded because
the checking is the point: it reported another mangled-backslash byte in this
file (true — fixed) but characterised the exit-code path as sound *and* the
docs as claiming more than the code did. On the first it was right and I had
briefly doubted it: a nested-quoting artifact in my own test harness made the
script look like it exited 0 on 65 failures. Measured through the process exit
code instead, it returns **65**, and `Invoke-Wsl` throws on it. The guard was
never broken; my measurement was.

**Verified after every change** (re-run against the same two finished ISOs):

- hub **14 / 14**, wall **5 / 5**, both exit 0; a typo target is rejected with
  exit 2. The hub count fell from 17 to 14 because four per-package lines
  collapsed into one aggregate — **coverage rose from 4 packages to 22** and
  gained version comparison.
- `bash -n` clean; `Build-VentoyStick.ps1` parses clean.

**The honest scorecard:** of the nine findings, seven were real defects in code
written the same night, one was a real stale byte, and one was a doc/code
mismatch that the fixes themselves resolved. The lesson is not subtle — the
guard against unverified artifacts was itself unverified, and it took an
adversary to say so.

### 2026-08-27 (afternoon) — the gate PASSED, and three of the night's fixes are now proven on a real install

`Start-LabRun -Stage All` was triggered through the JEA endpoint (unelevated) and
ran **54 minutes, 12:54 → 13:48**, ending **PASSED — 13 assertions**. Both images
were rebuilt from source into the lab directories, both machines installed
unattended and offline, and both were torn down afterwards.

**What this proves that nothing before it did.** Everything up to now was
carriage: what the ISO CONTAINS. This is the install path executing.

- **The systemd cascade fix is real.** `PASS package installed: systemd-sysv`
  and `PASS package installed: libpam-systemd` on the installed box. That is the
  defect whose other branch was an apt plan to delete `snapd`, `polkitd` and
  `ubuntu-server` — now proven resolved offline, on a box, from the baked repo.
- **The IceDrive staging-path fix is real.**
  `deploy-payload/stack/remote-ui/Icedrive.AppImage = 112 MB (+ its pinned
  sha256)` in the build log. The artifact reached the payload at the path the
  ISO is built from — the bug that had the AppImage writing one directory above
  it, silently, since `ef826bc`.
- **Step 6b fired, for the first time ever, on both builds.**
  `[6b/7] Asserting the ISO payload` → `the ISO carries what the build declared`,
  hub and wall. Until this morning the step could not run at all (the BEL byte)
  and then could be skipped (the optional checker). It is now wired, mandatory,
  and exercised inside the real build pipeline rather than by hand.
- **The disk pin held.** Stage 5 booted the SHIPPED image at the lab VM and
  `PASS nothing was written (0 MB)` — the containment property that keeps a
  production stick from installing on the wrong machine.
- **The install genuinely had no network:** `hub INSTALLED WITH NO NETWORK — the
  install finished 9.0 min before the cable went back in`, then
  `homehub-firstboot.service is active (Result=success)`, `docker holds 16
  image(s)`, Technitium answering, tracker healthy.
- **The panel installed and the interconnect works:** `wall-firstboot.service
  completed`, a kiosk session running, `200 from https://wall.<domain>:8443/`
  for the panel and `403` for a non-panel address.

**The FileBackup container was refreshed first, and the reason matters.** The
`filebackup:local` sitting in the cache carried an EMPTY
`homehub.source.revision` label and dated from 2026-08-26 — the signature of a
hand-loaded Podman export, which `export-images.sh` only WARNS about. It would
have baked silently stale. Rebuilt through `scripts/ensure-local-images.sh
--rebuild`, it now stamps `680136bc35f3`, and the box carries it. The `+dirty`
suffix is the known WSL false positive: WSL git reports 28,713 insertions and
28,713 deletions — identical counts, pure CRLF/LF noise — while Windows git
reports the tree clean.

**WHAT THIS RUN DID NOT PROVE, and it is exactly the night's headline feature.**
The extras were **carried and never exercised**. The build log shows the whole
carriage half working — `extras: remote desktop = ON; IceDrive = appimage;
optional set = BAKED (22 packages)`, the AppImage staged and placed — and then
**after build time the words `icedrive`, `xrdp`, `remote-ui` and `appimage` do
not appear again anywhere in the run.** `assert-installed.sh` asserts the CORE
package list and nothing else, so whether firstboot step 6c actually installed
the desktop, started the boot-time session and launched the AppImage is still
unknown. The gate passed without ever looking.

That is the "IceDrive has zero test cases at any tier" gap, now with a sharper
edge: **the one run that could have answered it wasn't asked the question.** And
because `-Stage All` tears the VMs down on every exit path, the box that could
have been inspected no longer exists. `All-Keep` is the key that would have left
it standing.

**Two caveats to read the result with:**

- **The heartbeat did not fire on either VM.** The link came back on the LATE
  signal (VHDX quiet for 4 minutes) rather than on the reboot signal, so the
  stack came up with no network and ACME/DDNS retried into that. The launcher
  says to read a health failure after this as that rather than as an image
  defect — and `WARN Actual not reachable via https://actual.<domain>/` is the
  one health warning, consistent with exactly that.
- **These are not the ISOs that were asserted 14/14 and 5/5.** `-Stage All`
  rebuilt them, so the booted images include today's FileBackup container and
  the eleven other changed inputs. The invariants held on both, but the bytes
  differ.

### 2026-08-27 (evening) — the activation assertion found the feature never worked

The extras assertion (section 5c) was added and the gate re-run with `All-Keep`.
It **FAILED — 16 checks**, and every failure was real. The headline feature of
the last two days had never worked on a real box, and three gates had passed
over it.

**What the assertion found, and then what the box confirmed directly:**

1. **Thirteen of the twenty-two optional packages were NEVER INSTALLED** — all
   eleven Qt/xcb runtime libraries, plus `xvfb` and `freerdp2-x11`. They were
   baked into the offline repo and asserted present on the ISO; nothing ever
   installed them. `setup-remote-ui.sh` carried a **hardcoded list of nine
   names** — exactly the nine that passed — and it was never updated when the
   eleven were added to `packages.optional.list` on 2026-08-27.
2. **The AppImage still could not start.** Run by hand on the box:
   `libwebpmux.so.3: cannot open shared object file`. That is the FIRST library
   in the original chain. The 2026-08-27 fix was **carriage-only**; the defect it
   was written to fix was never actually fixed, and the docs said it was.
3. **`xvfb`/`freerdp2-x11` missing meant the boot-time session could not run**,
   so there was no desktop at all. `setup-remote-ui.sh` even WARNED about this —
   into a firstboot log nobody reads — and the warning was itself stale, saying
   "they are in packages.list" when they are in `packages.optional.list`.
4. **`enable` is not `start`.** The session unit was `enabled, inactive (dead)`
   with **no journal entries at all**. It is `WantedBy=multi-user.target`, which
   the box passed long before firstboot reached it, so a fresh install got a
   session scheduled for the NEXT boot and none at the time. The comment
   justifying that cited "the reboot that usually follows" — a claim already
   corrected in `f783ff3`, because there is no such reboot.

**Fixed here, and both fixes verified on the live box:**

- `setup-remote-ui.sh` now **reads `packages.optional.list`** instead of a
  hardcoded list, and refuses rather than falling back to a built-in default —
  a silent default is precisely what drifted. Measured after the change:
  `22 package(s) from packages.optional.list`, installed **offline from the
  baked repo**, which also proves the carriage half was right all along.
- The session unit is now **started as well as enabled**, with a WARNING that
  names the diagnosis if it will not start.

**Proven on the box after a clean reboot:** all 22 optional packages installed,
`xrdp` enabled + active, tcp/3389 listening, the session unit enabled AND
active, and **an Xorg session running with nobody connected** — the SR-015
auto-login working, unattended, for the first time. Launched into that display
the AppImage **starts and stays up** (banner: `Icedrive Mount/Sync App`,
version 3.62, alive past 45 s). The ALSA errors it prints are a VM with no sound
device.

**STILL OPEN, and it is a DIFFERENT defect from the one fixed.** After a clean
reboot the XFCE autostart entry does **not** produce a running IceDrive:
`~/.config/autostart/icedrive.desktop` is present and correct,
`xfce4-session` is running, `.xsession-errors` says nothing, and the same binary
launched by hand into the same display works. So this is an **autostart
mechanism** problem, not the missing-library problem. The assertion stays RED on
it, which is exactly what it is for. `~/.config/Icedrive/` does not exist, so the
app has still never been signed in — the SN-001 hands-on step.

**The lesson, stated plainly, because it has now repeated three times:** a
feature has a carriage half and an activation half, and this project keeps
proving the first and assuming the second. Carriage was asserted on the ISO;
activation was asserted nowhere; three green gates ran over a feature that had
never once functioned. The assertion that found it took twenty minutes to write.

### 2026-08-27 (late) — signed in once, rebooted, and it came back by itself

The Owner signed IceDrive in over RDP — with 2FA, interactively, exactly the
SN-001 step this feature has always carried — and the box was then rebooted to
settle the question that has been open since the feature was designed: **does the
sign-in survive, or is it a step per reboot?**

**It survives, and it is not a partial result.** Ten seconds after boot, with
**nobody connected to the box**, IceDrive's own log records:

```
start login sequence (2)
login set; showing main tab...
request: auth;   ->   auth request finished
Storage statistics: usage = <real> ; quota = <real>
{"error":false,"count":0,"pairs":[]}
```

It authenticated **non-interactively against the live account** and read back the
real quota figures. No 2FA prompt, no window, no human. The whole chain came up
on its own: `homehub-desktop-session` active, Xorg running, `xfce4-session`
running, the AppImage running.

**Which keys carried it, measured rather than assumed:** `icedrive_login` and
`icedrivet` (the token) were present before and after; **`icedrive_stored_cred`
was ABSENT throughout** — the "remember my password" key was never written. So
persistence rests on the token pair, which is exactly the pair the CLI writes.
That is the first independent confirmation of the 2026-08-27 bench finding, now
on a GUI-only sign-in with 2FA, where the CLI could not have been used at all.

**`vmtest/assert-installed.sh` section 5c: ALL CHECKS PASSED (12/12)** after the
reboot — 22/22 optional packages, xrdp enabled + active, tcp/3389 listening, the
session unit active, an Xorg session with nobody connected, the AppImage matching
its pin, and **IceDrive RUNNING with nobody connected**.

**What this settles, and it is the whole feature:** *"RDP in and sign in to
IceDrive"* is a **one-time step per reimage**, not per reboot. The claim this
project repeated for a month — and which was false the entire time, because the
app could not start at all — is now true and asserted.

**What is still open, and it is small and new:** `"pairs":[]` — the account has
**no sync pairs**, so the client is authenticated and syncing nothing. Creating a
pair needs the GUI dialog (the CLI cannot: `showSyncDialog` has no toolkit), so
it is a second hands-on step, and nobody has done it yet. Until then the offsite
copy exists as an authenticated client and no data movement.

**The 2FA question is settled too, in the other direction.** The Owner asked
whether the CLI could seed the GUI's credential to remove the manual step. It
cannot, for an account with 2FA: the binary carries
`2FA method isn't supported in CLI` and prompts interactively for the codes it
does support. So the manual sign-in is not an unfixed gap — it is the deliberate
price of 2FA on the account, and worth it.

### 2026-08-27 (night) — the offsite copy is real: a pair, a reboot, and a file that arrived

The Owner created a sync pair over RDP and the box was rebooted. This is the
last unproven link in the chain, and it holds.

**After the reboot, with nobody connected**, IceDrive's own log:

```
{"error":false,"count":1,"pairs":[{"id":63501,"type":"sync","crypto":1,
                                   "ini_done":1,"folder_id":79889533, ...}]}
run sync threads (1)
starting sync thread for syncId 63501
localScan:: localPath: /home/hub/Documents; remote path: /2DEL_BACKUP_Repo/TestT
start watch on "/home/hub/Documents" id: 63501
waiting for events
```

The pair came back, the sync thread started, and the filesystem watcher armed
itself — none of it touched by a human.

**Then data actually moved.** A file written into the watched directory over SSH
was uploaded **six seconds later**, unattended:

```
rec:add: "/home/hub/Documents/reboot-sync-proof.txt"
sync: pending upload: "/2DEL_BACKUP_Repo/TestT/reboot-sync-proof.txt"
uploading crypto chunk 1 / 1
response OK: {"error":false,"message":"Upload Successful","id":682011607, ... "crypto":1}
File upload complete (sync)
```

`crypto: 1` throughout: the pair is an **Encrypted-folder** pair, so the payload
and the filename are encrypted client-side before they leave the box. That is
the arrangement the 2026-08-27 CLI investigation said was automatable, now
demonstrated on the GUI client.

**What the whole chain now proves, end to end and unattended:** boot → the
session unit creates a desktop with nobody connected → XFCE starts → the
autostart entry launches the AppImage → it authenticates from the stored token
(no 2FA prompt) → it restores the pair → it watches the directory → a new file is
encrypted and uploaded. Every link was broken at some point today, and every one
is now asserted rather than believed.

**The remaining hands-on steps are exactly two, both once per REIMAGE**: sign in
(2FA makes this unavoidable — the CLI cannot authenticate a 2FA account) and
create the pair (the CLI cannot draw the dialog). Neither is per-reboot.

**Housekeeping the Owner may want:** the proof file
`reboot-sync-proof.txt` is now in the account under
`2DEL_BACKUP_Repo/TestT` — a test area by its own name, but it is real cloud
content and nothing here will remove it.

**Still true and unchanged:** this is the LAB VM, not the real hub. Its VHDX
carries these credentials and the pair, and `Clear-LabVms` destroys it. The
sequence above is what a real hub will do; it is not the real hub having done it.

### 2026-08-27 (late) — the backup path ran for the first time, and a restore came back identical

The Owner asked whether the Library and PriBackup drives could be virtualised and
tested. They were already virtualised — the gate formats two 8 GB exFAT stand-ins
and mounts them **by label** (`Library` → `/srv/library`, `PriBackup` →
`/mnt/backup-drive`) — but nothing had ever run **through** them. That is C18, and
this is the first time the container has moved a byte.

**Why they were invisible in the desktop, since it is a fair question:** they
mount from the generated fstab at `/srv/library` and `/mnt/backup-drive`, not
under `/media`, so a file manager sidebar never lists them; and `/mnt/backup-drive`
is `uid=65532,dmask=0077`, i.e. **deliberately unreadable to the operator
account** — it belongs to the backup service, not the person.

**The preflight refused first, correctly.** 8 GB free against a 100 GB floor:
*"REFUSING TO RUN: the backup drive is below its floor… THERE IS NO AUTOMATIC
RETENTION (Q-FB2) — nothing here deletes a snapshot, so this will not clear
itself."* It also reported the drive-identity mismatch as a visible NOTE rather
than a stop — the designed stand-in verdict. Floor lowered to 1 GB **on the box
only**; the repo default stays 100.

**Then it ran.** 10 files (four seeded, six pre-existing `.immich` markers),
stored as content-addressed 7z objects with per-file dedup, a `DIRECTORIES.csv`
sidecar for empty directories, and `MANIFEST.csv` + its `.meta` witness. Container
**exit 0**, `Backup set 'library' completed`, 8.8 MB on the drive.

**AND THE RESTORE CAME BACK BYTE-IDENTICAL.** The drive carries its own restore
tooling (`reconstruct.sh`, `RECONSTRUCT.ps1`, `.bat`, the module and the hashing
DLL), so the test used the ON-DRIVE script, not the repo:

```
Manifest verified against its witness (version 2, rows=10, bytes=1754).
Directory sidecar: 10 row(s), 10 directory(ies) created.
Reconstruction complete: 10 file(s).
```

`diff -r` between `/srv/library` and the restored tree: **no differences.** The
8 MB random blob's sha256 matches exactly. Exit 0.

**THE ONE FAILURE, AND IT IS THE CONTRACT WORKING.** The wrapper exited
**non-zero** even though the backup succeeded, because the NagLight POST returned
HTTP 000:

> *the library backup itself succeeded, but the NagLight POST did not land.
> Exiting non-zero so this unit reports FAILED. A backup nobody was told about is
> not a backup that reported — from outside this box it looks exactly like a run
> that never happened.*

NagLight is not reachable from this lab VM, so that is an environment artifact
rather than a defect — but it is worth recording that the never-silent-green
contract fired exactly as written, and that it distinguishes "the backup failed"
from "the backup worked and could not be reported".

**What this does and does not close.** It proves the CODE PATH end to end:
preflight, capacity floor, mount identity, the container, dedup, the manifest
witness, and a byte-identical restore from the drive's own tooling. It does NOT
close **C18**, which is about physical stand-ins and then the real 4 TB + 8 TB
disks on the real hub — USB enclosures, `hdparm` standby and multi-day runs are
untouched here. The `drive-power` step even said so on exit: *"device not
present, skipping"*.

### 2026-08-27 (late) — a flashable ISO that actually contains the day's fixes

The ISO the lab gate built at **13:09** does not contain most of what was fixed
afterwards, and that mattered enough to rebuild before anything is flashed:

| fix | committed |
|---|---|
| install the whole `packages.optional.list`, not a hardcoded nine | 14:48 |
| `enable` is not `start` for the session unit | 14:48 |
| `~/.config` owned at every level (the failsafe-session dialog) | 16:05 |
| xrdp restart made conditional + `After=homehub-firstboot.service` | 16:50 |

**Everything proven on the VM yesterday evening was proven by hand-patching a
running box.** Flashing the 13:09 image would have reproduced all four defects on
hardware, where they are far more expensive to diagnose.

**Built:** `Z:\vmtest-out-hub-flash\repacked.iso`, 6.1 GiB, from
MiniPC-Deployer `39fab45` / HomeHub `380245d`, both clean.

**Asserted 14/14** by step 6b — which now runs because it is wired correctly and
mandatory: payload root, `.env` activation coherent
(`REMOTE_UI_ENABLED=true`, `ICEDRIVE_MODE=appimage`), `OPERATOR_PASSWORD` set,
the AppImage aboard matching its pin with the travelling `.sha256` agreeing, the
CLI correctly absent, **all 22** bake-only packages in the repo and none
install-listed, and the systemd lockstep siblings present at matching versions.

**And the fixes were confirmed INSIDE the ISO**, not merely in the working tree —
`setup-remote-ui.sh` and `homehub-desktop-session.service` extracted straight out
of `/deploy-payload` and checked for all five changes. That is the distinction
the 13:09 ISO failed, so it is the one worth making explicitly.

**Carries today's FileBackup container** (`filebackup:local` stamped
`680136bc35f3`, the `+dirty` being the known WSL CRLF false positive), and the
image is **pinned to the real hub's disk serial** — it installs on no other
machine.

**WHAT IS STILL NOT PROVEN, and it is the honest headline.** No box has ever been
installed from THIS image. The four fixes above were demonstrated by patching a
running VM; a clean install exercising them through firstboot's step 6c has not
happened. `vmtest/assert-installed.sh` section 5c is written and would catch a
regression, but it has only ever run against a hand-repaired box, never against
one built from this ISO.

**The order that follows from that:** `Clear-LabVms` (the running VMs hold the
hub's DHCP reservation and their gate ISOs open), re-run the gate against this
image so the fixes are proven from a clean install, and only then flash. Flashing
first is defensible but skips the one test that has never been run.

### 2026-08-27 (end of day) — the gate caught a deadlock the whole day had hidden

The rebuilt ISO was put through the gate, and it **failed 3 checks**. Not a
regression in what they assert — **a boot-wedging deadlock introduced by two
fixes that are each correct alone.**

**The cycle.** `setup-remote-ui.sh` runs INSIDE `homehub-firstboot.service`, and
earlier that day it gained `systemctl start homehub-desktop-session.service`.
The same day that unit gained `After=homehub-firstboot.service`. A blocking
`systemctl start` therefore waits for a unit that cannot begin until this
script's own service finishes — and that service is waiting on the call.

Measured on a clean install, and it is not ambiguous:

```
10932  systemctl start homehub-desktop-session.service   <- blocked
 6684  bash .../setup-remote-ui.sh                        <- waiting on it
    2  multi-user.target  start waiting                   <- the boot itself
```

firstboot sat in `activating` for **18 minutes**; the session unit never started,
no Xorg existed, and `/opt/icedrive` was absent because the AppImage install
comes later in the same blocked script. All three gate failures were downstream
of that one cycle.

**Fixed with `--no-block`**, which enqueues the start and returns, so systemd
runs it the moment ordering allows — exactly when firstboot completes. The
ordering guarantee is kept and the cycle cannot form. Verified after a reboot:
firstboot **active** (it had never once completed), **zero** queued jobs, session
active, Xorg up with nobody connected, AppImage installed, IceDrive running, and
section 5c **ALL CHECKS PASSED**.

**THE LESSON, AND IT IS THE DAY'S SECOND-BEST ONE.** Neither fix could have
revealed this alone; only running the pair on a clean boot could. Every green
result before this came from a box repaired by hand, and a hand-repaired box has
already passed the step where the deadlock lives. **That is exactly why the
"rebuild and re-gate before flashing" step existed, and it paid for itself the
first time it ran.**

**Two false readings, corrected, because both cost time.** `pgrep -f
Icedrive.AppImage` matches firstboot's own `ICEDRIVE_APPIMAGE=…` command line, so
"IceDrive RUNNING" was the grep seeing itself — twice. The real check is
`pgrep -f "[I]cedrive.AppImage"`. And earlier, `pkill -f Icedrive.AppImage` over
SSH killed its own session for the same reason.

**Also worth recording as a guard that worked:** the first attempt at this run
refused in 7 seconds — *"the hub gate ISO is STALE (the production ISO it is
derived from is newer) and -Stage Hub does not re-derive it"* — rather than
quietly booting the old image and reporting a pass that meant nothing.

**Where this leaves the artifact:** `Z:\vmtest-out-hub-flash\repacked.iso` is
asserted 14/14 and **must not be flashed** — it carries the deadlocking script.
The fix is committed and in no image. The Current State header at the top of this
file carries the exact commands to rebuild, re-derive and re-gate.

## Audit — 2026-09-07 unified file-share/backup feed requirement

The Owner's new presentation ruling is recorded as SN-014/SR-018 and IF-006;
the old IDs were not redefined. The planned producer contract keeps
share-health override state independent from FileBackup's last verified success
and adds the previously missing Samba availability probe. Physical mount,
identity and backup preflight safeguards remain valid internal controls, but
they no longer earn separate panel indicators. The current multi-lane services,
sim fixture and shell tests are explicitly implementation work, not silently
described as already migrated. The registry-integrity check reported SN=14,
SR=18, LLR=0 and TC=0 with integrity=0; the 26 expected G1 decomposition
orphans remain. G1 remains active.

## Audit — 2026-09-08 tracker image deployed; the backup-state producers were not

The hub's `naglight:local` was rebuilt from NagLight `8814dba` and deployed,
replacing a 2026-08-29 build. `scripts/ensure-local-images.sh` was the path used,
with only the stale `naglight:local` tag removed first so the resolver rebuilt
that one image and skipped the other four; the resulting image carries
`homehub.source.revision=8814dbaadd8d…` with no `+dirty`. The offline payload
`images/naglight_local.tar` was replaced and its `images.manifest.tsv` row
updated to match, preserving that file's existing `repo_digest = <repo>@ + image_id`
convention. The replaced tar was verified byte-identical to the build artifact
after the fact; it was written with `cat new > file` rather than the `.part`-plus-
rename this repo's own `vmtest/export-images.sh` uses, and the exporter's
approach is the one to follow next time.

**The consumer half of the run-phase work is now live and the producer half is
not.** The tracker answers `/api/backup-state`, but `6e7e970` and `185a435` — the
commits that emit run phases — are not on the hub: `grep -rl backup-state` over
the deployed `stack/` and `scripts/` trees returns nothing, while this repo has
it under `stack/backup/` and `stack/samba/`. Until those are deployed the panel
will show no run phases, because nothing posts them. That deploy edits files
under the deployed tree and is therefore subject to the bind-mount inode rule:
overwrite in place, because replacing an inode leaves the container serving the
old content while a reload silently no-ops.

Two live-configuration observations, neither requiring a change here. The
`protocols h1 h2` fix in `stack/caddy/Caddyfile` is deployed and effective — no
`alt-svc` header is served — but browsers that cached the earlier
`alt-svc: h3=":443"` promise keep attempting QUIC for up to 30 days, per origin,
and present as `ERR_CONNECTION_TIMED_OUT` on one hostname while another works.
The remedy is client-side. Separately, `*.<domain>` resolves to the WAN address
for any client that bypasses the LAN resolver, so a device using DNS-over-HTTPS
cannot reach any of these LAN-only hostnames; explicit A records for the LAN-only
names would fix it zone-wide, at the cost of publishing a private address, and
the DDNS updater manages only the apex and the wildcard so it would not fight
them. Not acted on.

The wall panel was separately updated to OfficeWallNaglight `c7c9358`; its site
half was installed into the bind-mounted `stack/wall-shell` with the directory
inode and the live `config.json` both verified unchanged. No ISO was built or
flashed and nothing was pushed.

## Audit — 2026-09-08 (later) the oauth2-proxy allow-list was inert; wildcard removed

**`OAUTH2_PROXY_EMAIL_DOMAINS: "*"` in `stack/docker-compose.yml` defeated the
allow-list it sat beside.** Its own trailing comment read "gate on the allow-list
file, not the domain": the intent was right and the setting contradicted it. In
oauth2-proxy's validator the domain check and the authenticated-emails file are
OR'd, and a `*` domain sets `allowAll = true` and returns before the file is
consulted. The file — one entry — was inert, and any Google account would have
been admitted to the tracker, which in multi-user provisions a per-user data dir
on first request. Verified against the v7.15.2 source rather than assumed; it is
oauth2-proxy issue #73.

What was actually protecting the host was not in this repo: the Google OAuth app
sat in "Testing" publishing status, so only approved test users could complete
consent — an accident of the Google console, and the plan of record was to publish
the app, which would have removed it. The hub is also not WAN-reachable, so the
exposure was LAN-scoped rather than internet-scoped.

**Fixed in the source tree here AND on the deployed box.** Fixing only the running
box would have been worse than useless: `/opt/homehub` is not the source of truth,
so the next reimage would have silently reintroduced it. Both copies now carry a
comment block explaining why the key must not come back, how to grant access
(the emails file, watched at runtime, plus `OAUTH2_PROXY_ALLOWED_EMAILS` for
reimage survival), the single-file bind-mount inode rule for editing it, and the
fact that the allow-list is evaluated at sign-in rather than per request — so
revoking someone needs a `OAUTH2_PROXY_COOKIE_SECRET` rotation, not just a line
removal.

Verified after restarting oauth2-proxy only: `validator.go: using authenticated
emails file`, `watcher.go: watching … for updates`, no `EMAIL_DOMAINS` in the
container, and a cookie-less request to `/` and `/api/today` returns 403 with the
sign-in page. Existing sessions survived, as expected — the cookie secret was not
rotated.

**Two WAN prerequisites recorded, neither acted on.** oauth2-proxy logs
`--reverse-proxy is enabled but no --trusted-proxy-ip CIDRs were configured`;
that is not an auth bypass and is unreachable except through Caddy today (4180
unpublished), but once 443 is forwarded a spoofed `X-Forwarded-For` would
undermine the rate limit and the fail2ban 401 filter. And forwarding 443 exposes
every site block: `actual` and `dns` fail closed on `@lan remote_ip`, `wall` is on
8443, the apex is a static string, `game` is public by intent — leaving `tracker`
carrying the whole load, which is why the above had to be fixed first. Adding a
`@lan` gate to `tracker` was considered and rejected: it would mask the defect and
would have to be removed on exposure.

## Audit — 2026-09-08 (later still) the Drive config existed only on the running box

Porting the tracker's Drive block into `stack/docker-compose.yml` and
`stack/.env.example`. It had been added directly to `/opt/homehub` when the
integration was deployed and never landed here, so **a reimage would have
silently dropped the whole integration** — the token-dir volume included, which
is the part that keeps a refresh token out of the hourly GitHub mirror. Found by
grepping this repo for a knob that was being added to the deployed stack; the
same `/opt/homehub` drift the oauth2-proxy fix ran into earlier, in the same
direction.

Adds `TRACKER_DRIVE_USER`, the one identity the sync runs for in multi-user
mode. Blank means the sync stays off and only connect/callback work: the tracker
will not guess which household member's sheet to apply, because applying the
wrong one overwrites another person's definitions. Same shape and reasoning as
`TRACKER_MIRROR_USER`.

`docker compose --env-file .env.example config tracker` renders with Drive fully
off (no source, no user), which is the correct default for a clone.

## Audit — 2026-09-09 the `panel-capabilities-2026-09-06` branch is reconciled and gone

Three commits had diverged onto that branch at `eb1b012` while this branch moved
19 commits past it. **Two were cherry-picked; one was deliberately dropped.** The
branch and its linked worktree are removed, so this cannot drift a third time.

**Kept — `bd0977e`, the frame lane asserts freshness of the videos**, not that a
generator ran (open item E9). Applied clean. Adds `stack/tracker/frame-freshness.sh`
with its service and timer.

**Kept — `35ac0b9`, config archives leave the library drive for a cascading
Fibonacci ladder.** Seven rolling dailies plus rungs refilled F(n) from F(n-1)
every F(n-2) days, out to 233. One conflict, in `stack/backup/README.md`, resolved
by reading both sides rather than taking either: this branch's **Panel health**
row is kept because it is the post-unification answer to "which feed", and the
incoming **Retention** row replaces ours because ours said *"none — nothing is
pruned"*, which the ladder makes false. The incoming **Feed lane** row (`backup` /
`library-backup`) was dropped as the pre-unification form of the same question.

**DROPPED — `973b527`, "the backup-drive health lane read `BACKUP_TARGET`, which
the flat layout moved onto the library drive".** Recorded here so it reads as a
decision rather than an oversight. It repairs
`stack/samba/homehub-backup-drive-health.service`, which `31d3d72` on this branch
**deleted** when it implemented the unified share-and-backup health producers —
the ratified SN-014/SN-037 ruling that replaced per-drive telemetry with one
indicator. Git surfaced it as a modify/delete conflict, but the substance is that
the component no longer exists by decision. Applying the fix would have
resurrected it. `stack/samba/backup-drive-health.sh` is likewise absent, verified
after the picks.

**Evidence.** `stack/backup/tests/fib-ladder.test.sh` — **41 pass, 0 fail**, run
under WSL Ubuntu because the test needs `rsync`, which the dev PC does not have
on PATH. `bash -n` clean on `frame-freshness.sh`, `backup.sh` and `common.sh`.
`scripts/check.py`: config-validate, registry-integrity and doc-navigability all
pass; **unit-tests fails for want of `pytest` on this machine**, which is a
pre-existing environment gap (`import pytest` fails at the interpreter) and not
attributable to these commits. It is still an unrun step, not a passing one.

Rollback point kept as the tag `pre-b15-2026-09-09` (`bf74c67`).

## Audit — 2026-09-09 the AI CLI service lands, contained and off (B12)

**What it is.** `stack/ai-cli/` — `ai_cli_service.py`, `agents.registry.csv`,
`routes-enabled`, `homehub-ai-cli.service`, `setup-ai-cli.sh`, a README and a
hermetic guards suite — plus firstboot step 6d, nine `AI_CLI_*` knobs in
`stack/.env.example` and the matching T0 rows in HomeHub's `FieldSchema.psd1`.
Spine: **SR-019, LLR-002, TC-002, IF-011**, under SN-016, implementing HomeHub
`open-items.md` **A40 as ratified 2026-09-09**.

**No container, and that is the ruling rather than a shortcut.** A container was
only ever wanted for containment and delivers none here: the subscription
credential must come from a home directory a human logged into interactively, so
a container has to mount that home and the isolation is punctured at the only
point that mattered.

**The four acceptance properties are the block, and none of them is asserted by
eye.** Each is a refusal in code with a test behind it:

| Property | Enforced by | Asserted by |
|---|---|---|
| dedicated unprivileged account, never `hub` | `assert_service_account`; `setup-ai-cli.sh` refusals; `User=homehub-ai` | 8 pytest cases + guards A2/A3 |
| read-only tool use, no `--dangerously-*` anywhere | `assert_safe_template`, run at startup **and every launch** | 9 pytest cases + guards A1/A9/A11 |
| loopback or the docker bridge, never the LAN | `resolve_bind`, before the socket exists | 18 pytest cases + guards A4/A5/A6/A8 |
| a scratch working directory per request | `request_scratch` + `StateDirectory` 0700 | 4 pytest cases + guards A10 |

Two of those deserve naming. The account check does not stop at the name: a
dedicated account that has **since** been put in `sudo`/`docker`/`adm`, or named
in any sudoers rule, is refused — and `setup-ai-cli.sh` re-checks on every boot,
because an account created without sudo can be given it later. The
`--dangerously-*` check is by **prefix**, so a flag a future CLI version invents
is caught before this repo is updated, and it is a **tree-wide grep**, so a
copy-paste out of `ai-template` fails the build rather than shipping. The grep
pattern is `--dangerously-[a-z]`, not the bare prefix, so prose that forbids the
flag (which writes the glob) does not fail the check that enforces the ban;
three files that must write a real flag name — the two test files and
`stack/ai-cli/README.md` — are excluded by name, and nothing in them executes.

**What was reused from `ai-template`, and what was not.** Reused: the pair-row
registry and its loader (its IF-045 schema), the per-row cooldown, and the
session-launch decisions (its IF-064) — prompt on stdin when the template
carries no `{prompt}` (WI-216), codex's `--output-last-message` because it
echoes the prompt into stdout (WI-217), a reader thread, a per-session timeout,
stdin never left open. **Not reused: its flags.** That repo carries them because
it *is* the consented unattended run; this service's work is chosen by a caller,
so the consent does not transfer.

**THE MEASURED LATENCY FLOOR, because it is what sizes every consumer.**
`--bare` — the fast startup meant for scripts — ignores the OAuth token and
requires an API key, so a service on the subscription cannot use it and pays
full startup per call. Measured 2026-09-09, `claude 2.1.201`, trivial prompt
(`-p … --output-format json --max-turns 1`), three runs from an empty working
directory: wall **4125 / 2914 / 3730 ms** against the transcript's own
`duration_ms` of **1897 / 1234 / 2087 ms** — so roughly **1.6–2.2 s of non-model
startup, and ~3–4 s wall for the cheapest possible call**. Two caveats that make
the real number worse, not better: this was the **dev PC, not the hub** (the
AK41 is slower), and the directory was empty so no project `CLAUDE.md` or skills
loaded. Consequence: nothing may put this on a sub-second synchronous path, and
`AI_CLI_TIMEOUT_SECONDS` should be set against this figure rather than a guess.
`--bare` is on the banned-token list for that reason as well as for safety, so
the floor cannot be "optimised" away by quietly moving to a metered key.

**Evidence.** `python scripts/check.py` — **PASS** at gate G1, all four steps:
config-validate, unit-tests (**207 passed, 4 skipped**, of which 53 passed and 1
skipped are the new `tests/test_ai_cli_service.py`), registry-integrity
(SN=16 SR=19 LLR=2 TC=2, **integrity=0**), doc-navigability.
`python scripts/trace.py --strict` — clean, orphans 26 → **25** (SR-019 has a
TC). `python scripts/check_flows.py --no-placeholders` — **OK, 1 diagram, 2 ids,
all known**; it was FAILING before this change for want of a "Runtime flows"
heading, and the `/v1/ask` sequence diagram added to `docs/architecture.md`
clears it. `bash stack/ai-cli/tests/ai-cli-guards.test.sh` — **16 PASS, 0 FAIL**
run standalone; the whole `run-hermetic-tests.sh` runner still **refuses on this
machine** for want of `zstd` and `rsync`, which is the pre-existing dev-PC gap,
not a result.

**Correcting the previous block's report:** `pytest` *is* available to
`check.py` on this machine — the harness invokes `C:\Python38\python.exe -m
pytest` and the unit-tests step ran and passed here, both before and after this
change. B15's note that it "fails for want of `pytest`" does not reproduce.

**Nothing live changed.** No account was created, no unit installed, no service
started; deployment stays a separate Owner-run step, and the sign-in
(`claude setup-token` over RDP as `homehub-ai`) is still owed by a human. No apt
package name was added — the service is Python 3 stdlib only and the CLIs are
not apt packages (SN-016) — so no apt export re-run is owed.


## Audit - 2026-09-09 (later) B12 fix round: the AI CLI service re-earns its four criteria

**Why there was a fix round.** B12 reported all four of A40's security
acceptance criteria as MET with evidence. A cross-review by a different model
family returned **REJECT with 16 confirmed findings**, and the coordinator
independently verified the three worst in source. Nothing in the review
contradicted ratified scope - the acceptance could be met as written; it simply
had not been. The gap between what the block claimed and what its guards
enforced was the largest of this build, and the four criteria are the whole
point of the block.

**The shape of every finding, worth naming once:** the guards checked that a
**string was present** where the property was about **identity**. A template
that carried `--permission-mode plan` was treated as contained without anyone
asking what the command was. An account name in a config file was treated as
the account that runs. Membership of a /12 was treated as evidence of a bridge.

**What was changed** (`stack/ai-cli/ai_cli_service.py`, `setup-ai-cli.sh`,
`homehub-ai-cli.service`, `agents.registry.csv`, `stack/.env.example`, HomeHub
`FieldSchema.psd1`):

* **V1, the executable pin.** `FAMILY_CONTRACTS` declares each family's
  executable, subcommands, whole flag vocabulary, per-flag value constraints,
  the flags the *service* appends (refused inside a template), and how that
  family receives the caller's schema. `assert_safe_template` walks every token
  against it; `assert_safe_env` allow-lists `Env=`; `resolve_executable`
  resolves the pinned name on `AI_CLI_BIN_PATH` only and refuses a
  group/other-writable binary. Adding a flag is now a reviewed edit to the
  service, not a registry cell nobody reads.
* **V2, the account.** `assert_effective_account` compares the configured
  account with this process's own effective account and refuses a mismatch or
  an undeterminable identity; `setup-ai-cli.sh --emit-dropin` generates the
  unit's `User=`/`Group=` from `AI_CLI_USER`, and the install path now runs
  `--check` under `runuser -u "$AI_CLI_USER"` rather than as root.
  `collect_sudoers_lines` follows includes; an unreadable one is kept verbatim
  so the guard refuses to certify rather than reporting "no rule found" about a
  file nobody opened.
* **V3, the bridge.** `docker_bridge_addresses` reads `/sys/class/net`, keeps
  only `docker0`/`br-*` interfaces that really are bridges, and takes the
  addresses they carry; `resolve_bind` accepts loopback or one of those.
* Plus, each with its own test: duplicate/late flags, `--allowedTools` values,
  `Env=` injection, sudoers includes, `::1` bindability (`bind_family` +
  `make_server`), concurrency/body/socket/connection bounds (`RouteGate`,
  `make_handler`, `make_server`), the cooldown race and success pacing,
  process-group kill, the typed-result assertion, codex's schema flag, and
  visible scratch-cleanup failure.

**Knobs added** (declared in `stack/.env.example` **and** HomeHub's
`FieldSchema.psd1`, all `T0`): `AI_CLI_SUCCESS_COOLDOWN_SECONDS`,
`AI_CLI_MAX_CONCURRENT`, `AI_CLI_MAX_BODY_BYTES`,
`AI_CLI_SOCKET_TIMEOUT_SECONDS`, `AI_CLI_MAX_CONNECTIONS`, `AI_CLI_BIN_PATH`.

**Spine.** SR-019 unchanged (the acceptance criteria are verbatim what they
were). LLR-002 rewritten; **LLR-004** and **TC-004** pulled for the bounds.
TC-002's permutations widened to the new refusals.

**THE MUTATION EVIDENCE, which is the standard this round is held to.** Every
guard was deliberately broken, its test confirmed RED, the guard restored, and
the test confirmed GREEN. 24 of 24 bite:

| Guard broken | Test | Broken | Restored |
|---|---|---|---|
| executable pin removed | `TestPinnedCommand` | 1 failed | 10 passed |
| vocabulary allow-list bypassed | `...undeclared_flag...` | 1 failed | 1 passed |
| duplicate-flag refusal disabled | `...duplicate_flag...` | 1 failed | 1 passed |
| `--allowedTools` validator stubbed | `...allowed_tools_VALUE...` | 1 failed | 1 passed |
| `--disallowedTools` validator stubbed | `...deny_list...` | 1 failed | 1 passed |
| `Env=` allow-list bypassed | `TestEnvIsAnAllowList` | 1 failed | 8 passed |
| binary mode check disabled | `...world_writable_binary...` | 1 failed | 1 passed |
| effective-account assertion removed | `...wrong_account...` | 1 failed | 1 passed |
| sudoers include treated as comment | `...include_is_followed...` | 1 failed | 1 passed |
| 172.16/12 re-admitted | `...household_lan...` | 1 failed | 1 passed |
| server forced to AF_INET | `TestTheServiceCanServeWhatItAccepts` | 1 failed | 3 passed |
| cleanup back to `ignore_errors` | `...failed_cleanup_is_visible...` | 1 failed | 1 passed |
| in-flight claim removed | `TestPacingIsAtomic` | 1 failed | 6 passed |
| success no longer cools | `...successful_call_also_paces...` | 1 failed | 1 passed |
| group kill back to `proc.kill()` | `...process_group_is_killed...` | 1 failed | 1 passed |
| body ceiling removed | `...oversized_body...` | 1 failed | 1 passed |
| connection ceiling removed | `...connection_ceiling...` | 1 failed | 1 passed |
| result `type` check removed | `...non_result_object...` | 1 failed | 1 passed |
| result-null check removed | `...null_result...` | 1 failed | 1 passed |
| `is_error` check removed | `...is_error...` | 1 failed | 1 passed |
| codex last-message check removed | `...last_message_family...` | 1 failed | 1 passed |
| codex schema flag not appended | `...codex_is_actually_handed...` | 1 failed | 1 passed |

and against the shell suite (baseline **21 PASS 0 FAIL**), each mutation taking
it red on the check that is supposed to notice:

| Guard broken | Shell check that went red |
|---|---|
| effective-account assertion removed | A9 (`--check` accepted an account nobody runs as) |
| scratch removal disabled | A10 |
| one shared scratch dir instead of per-request | A10 |
| drop-in hard-coded to `homehub-ai` | A2 |
| 172.16/12 re-admitted | A4 **and** A6 |
| executable pin removed | A12 |

The three vacuous checks the review named are gone: **A9** now runs `--check`
twice in a process whose real identity is known and requires the two answers to
differ for exactly one reason; **A10** drives the real request path with a fake
CLI runner and inspects the directory the child was handed (fresh, 0700, holding
only this request's schema, gone afterwards, different next time); **A2** runs
the drop-in generator with an unusual account name and reads what it wrote,
instead of greping a name out of the shipped unit.

**Evidence.** `python scripts/check.py` - **PASS** at gate G1, all four steps:
config-validate, unit-tests (**282 passed, 5 skipped**, up from 224/4 after B9;
`tests/test_ai_cli_service.py` went from 53 cases to **111 passed, 2 skipped**
standalone), registry-integrity (SN=16 SR=20 LLR=4 TC=4, **integrity=0**),
doc-navigability. `python scripts/trace.py --strict-integrity` - clean.
`python scripts/trace.py --strict` - **exit 1, pre-existing**: 24 legacy SRs
carry no LLR/TC, unchanged by this round. `python scripts/check_flows.py
--no-placeholders` - **OK, 2 diagrams, 6 ids, all known**.
`bash stack/ai-cli/tests/ai-cli-guards.test.sh` - **21 PASS, 0 FAIL** run
standalone. `stack/run-hermetic-tests.sh` was **NOT run**: it refuses on this
dev PC for want of `zstd` and `rsync`, which is the pre-existing gap and is
reported as UNRUN, not as passing.

**Two skips, named rather than buried.** `test_the_directory_is_private_sr019`
(POSIX mode bits) and `test_a_grandchild_does_not_survive_the_timeout_sr019`
(real process groups) do not run on the Windows dev PC. Both properties are
still asserted here through injected syscalls, so the DECISION is tested
everywhere and only the syscall is skipped - but the real grandchild-kill has
not been exercised on Linux and should be watched for on the box.

**Nothing live changed.** No account was created, no unit installed, no service
started. No apt package name was added, so no apt export re-run is owed.

**2026-09-09 — B11, the weight feeder (SR-022/LLR-006/TC-006/IF-014).** PARTIAL. Google Health API v4 verified as real and reachable by real calls (discovery revision 20260907; an unauthenticated GET on the weight dataPoints route returns 401 naming google.devicesandservices.health.v4.DataPointsService.ListDataPoints). NO PARSER WRITTEN - no authenticated call is possible until the Owner enables the API, adds googlehealth.health_metrics_and_measurements.readonly (there is no weight-specific scope, and that one also grants blood glucose, body fat and heart-rate metrics) to the existing OAuth client, and mints a refresh token at a browser. Everything not depending on that shipped: the gauge plumbing, the stale-never-green invariant, the definitions-resident goal, the token directory, 10 knobs, firstboot hook 6f and a fourth runtime flow. Found a NagLight dependency: internal/defsheet drops unknown columns, so on this sheet-synced household the goal would be erased by the first sync after a sheet edit. check.py 423 passed / 5 skipped (baseline 342/5, not the 341 the plan recorded); trace.py --strict-integrity 0, orphans 24 = baseline; check_flows --no-placeholders OK, 4 diagrams; run-hermetic-tests.sh UNRUN (missing zstd, rsync). 27 mutants, 26 killed.

**2026-09-09 — B7+B11 cross-review fix round (REJECT, all findings applied).** Symlink-bypassable write guard (realpath + StateDirectory bound + O_EXCL/O_NOFOLLOW), redirect/proxy egress on all four HTTP call sites (feed_opener / vendor_opener), windowless-gauge and replayed-200 freshness, per-gauge failure isolation, state validated on load and on use, no remote body or exception message in the journal. Mirrored across both feeders with tests/test_feeder_egress_parity.py as the enforcement. check.py 517 passed / 5 skipped (baseline 423/5); trace --strict-integrity 0, orphans 24; check_flows OK, 4 diagrams; run-hermetic-tests.sh UNRUN. 28 mutants, 28 killed after two passes; the first pass had 7 survivors and every one was a test defect. No knob changed, no live state touched.

**2026-09-09 — B9 cross-review fix round (REJECT, 10 findings, all applied).** The RTC frame established from timedatectl/adjtime instead of asserting `-l` (the panel would not have woken); the alarm targeted at the next local calendar occurrence instead of now+86400 (DST); `SLEEP_END` defaulted per path so the ratified 06:45 occupancy wake and the unchanged 06:30 schedule both hold from one knob (flagged for the Owner); the absence clock moved into the decision and cleared inside the on-period, so the hour is an hour spent OUTSIDE it; the clock written atomically and range-checked; NaN/Infinity refused in the presence file; the RTC alarm read back after arming; a failed backlight-off blocking the suspend. A11/A12 rewritten to assert properties rather than spellings. check.py 525 passed / 5 skipped (baseline 517/5); trace --strict-integrity 0, orphans 24; check_flows OK, 4 diagrams; occupancy-power.test.sh 73 PASS / 0 FAIL standalone; run-hermetic-tests.sh UNRUN (missing zstd, rsync). 11 mutants, 11 killed, no first-pass survivors. No live state touched, no apt package added.

**2026-09-09 — Owner rulings on two flagged items (SLEEP_END's default, and the success cooldown).** Both were flagged by their fix rounds rather than decided; both are now decided, and both are written down here rather than silently absorbed.

**Ruling 1 — `SLEEP_END` collapses to ONE 06:45 default.** B9 shipped it path-dependent (06:45 with `WALL_ABSENCE_ENABLED=true`, 06:30 with it false) so that SN-015's ratified 06:45 occupancy wake and its "with detection off the existing schedule stands completely unchanged" line could both hold. The Owner ruled that two shipped defaults for one knob is a thing nobody will remember in a year: one `if` removed in `wall-sleep.sh`, one in `wall-firstboot.sh`, and A2's expectation back at 06:45. **The acceptance line is knowingly RELAXED on this one value: the schedule-only path's morning wake moves 06:30 -> 06:45 too.** That is the Owner's decision, not a defect — the SHAPE of the disabled path is still untouched (A1: an occupancy tick writes nothing; A2: `SLEEP_START` still suspends on the clock with no presence consulted), and only the minute the RTC alarm is armed for has changed. `SLEEP_END` remains ONE knob doing all three jobs (RTC target, wake timer, on-period start) and A12 still asserts behaviourally that exactly two variables in the wall tree hold a wall-clock time. `wall.env.example` now ships `SLEEP_END=06:45` **uncommented**: the only reason it was commented out was to let the path-dependent default govern, that reason is gone, and writing it out puts both bounds of the window — and so both bounds of the on-period — on the page next to `SLEEP_START`, which was already uncommented. The script defaults still cover a `wall.env` that predates the knob. A12 gained two checks closing a real gap: each script must declare exactly ONE `SLEEP_END` default (a reintroduced `if/else` lists two) and both scripts must declare the SAME one — nothing else in the suite runs `wall-firstboot.sh`, so its default was previously asserted by nothing at all.

**Ruling 2 — a cooling route must tell the caller it is cooling.** The Owner accepted B12's "every completed call cools the route" (`AI_CLI_SUCCESS_COOLDOWN_SECONDS`, 5 s) on the condition that a caller can tell a cooling route from a broken one and learn when it is next available. `RouteGate.acquire` now returns a `GateDecision` instead of a bare string, and `refusal_response` turns it into a machine-readable refusal: **429 `{status:"cooling", reason:"success-pacing"|"failure-backoff", retry_after_seconds, retry_at, retryable:true}` with a `Retry-After` header mirrored from the body field** (the header as well as the field, not instead of it, and from one place so they cannot disagree); **429 `{status:"running", reason:"in-flight"}`** with `retry_after_seconds` as an honest FLOOR and deliberately no `retry_at`, because nobody can know when a running session ends; **503 `{status:"busy", reason:"at-capacity"}`** with no retry hint to invent, kept distinct because the *box* is full and a different route will not help. The cooldown reason is stored WITH the deadline (`cooldowns[id] = (until, reason)`), so a caller retrying into a 120 s failure backoff is never told "try again shortly". None of B12's four security criteria was touched — the dedicated account, the read-only tool use, the loopback/bridge-only bind and the per-request scratch dir are all unchanged, and their guards were re-mutated to confirm they still kill.

**Two mutation survivors found, both test defects, both fixed.** Deleting `self._lock` from `acquire` outright left the 8-thread burst test GREEN three runs running — the check-then-claim window is a few bytecodes wide, so the interleaving simply never occurred; the test was asserting the outcome of a race that never ran. (Widening it with a `sleep` made it worse: the sleep staggered the threads and serialised them by accident.) It now holds the window open deterministically at the point between the membership check and the claim, and the lock being held is what makes that point unreachable for the other threads. Separately, moving `cool()` out of `release`'s lock was killed by nothing; that window is unreachable by construction and so untestable behaviourally, and is now asserted structurally on the parse tree of `acquire` and `release`.

check.py 534 passed / 5 skipped (baseline 525/5); trace --strict-integrity 0, orphans 24; check_flows --no-placeholders OK, 4 diagrams; occupancy-power.test.sh 75 PASS / 0 FAIL standalone (baseline 73); ai-cli-guards.test.sh 21 PASS / 0 FAIL; run-hermetic-tests.sh UNRUN on this dev PC (missing zstd, rsync). 18 mutants: 16 killed on the first pass, 2 survivors (both test defects, described above), both killed after the tests were fixed. No live hub or panel state touched; no apt package added, so no apt export is owed.

**2026-09-09 — B14 Door image integration (SR-017 / WSN-019).** Added the
image-owned half of the explicit-start Door preview. `wall-door-stream.service`
runs as a dedicated non-login account with the panel group only as a
supplementary group; its runtime directory is group-traversable only after a
privileged `chgrp`, and the resulting socket is the broker's sole renderer
surface. `LoadCredential=wall.env:/etc/wall-panel/wall.env` lets PID 1 read the
already-materialized 0600 file and provide a read-only RAM-backed copy to the
unprivileged process. This deliberately does not create a second on-disk secret
file and does not use `EnvironmentFile=`. Filesystem/device/kernel controls are
removed through the unit hardening, and stdout/stderr are null: camera
credentials and FFmpeg diagnostics cannot enter the journal from this unit.

The autoinstall installs the unit but does not enable it early. Firstboot creates
the account only after the payload and root-only config exist, verifies the
packaged `doorstream/service.py`, enables/starts the broker, and explicitly
restarts it on every supported firstboot rerun so an edited `wall.env` replaces
systemd's credential snapshot. The service has no ordering dependency on
`wall-firstboot.service`: adding that dependency would deadlock when firstboot
starts it. Starting the broker opens no RTSP connection; the application socket
request remains the only source-start event.

The tracked wall template now carries the reserved-address and T3 password
placeholders, fixed Remo RTSP defaults, geometry controls, and bounded stale/
start timings. The VM renderer substitutes an unreachable `.invalid` endpoint
and fictional password so a gate image is complete but cannot contact a real
camera. `door-stream-v1` is mirrored into the exact private artifact contract;
its app-only declaration prevents the Python broker and Electron bridge from
entering the public site artifact.

**Evidence:** red-first `tests/test_door_stream_service.py` failed 4/4 before
the unit/config/wiring existed, then passed 4/4. The focused deployer set is
**144 passed / 1 skipped**. `python scripts/validate_config.py` reports **ALL
CONFIG CHECKS PASSED**, including YAML parse, tracked late-command inputs,
complete wall knobs, and no orphan systemd units. Both edited shell files pass
`bash -n`. `systemd-analyze verify` accepted the unit; it warned only about the
Windows checkout's DrvFs executable/world-writable projection, while user-data
installs it 0644 on the target. The first `python scripts/check.py` attempt was
UNRUN as a product verdict: the managed sandbox denied its configured shared
`C:\Projects\.pytest-tmp` and `docs/test/report.md` writes, producing fixture
setup errors rather than test failures. A second normal-permission attempt then
selected WindowsApps' broken `bash.exe` shim; with Git Bash put first explicitly,
the complete gate passed: **547 passed / 5 skipped**, trace integrity 0 with the
unchanged 24 legacy orphans, and doc navigation clean apart from its two known
orphan warnings. No ISO was built, no panel state changed, and no apt package
was added (the image already declares `python3` and `ffmpeg`).

**2026-09-09 — B14 display-power lifecycle follow-up.** `wall-sleep.sh` now
owns the image half of Door teardown as part of the same function that owns the
backlight: before any successful display-off it must stop
`wall-door-stream.service`, and the scheduled suspend path repeats that
precondition before calling `systemctl suspend`. A failed stop therefore leaves
the panel awake and lit rather than allowing an unseen camera/decoder session.
Turning the display on starts only the idle broker/socket; the script contains
no FFmpeg or RTSP action, so waking cannot become an implicit camera start.
Red-first tests failed 2/2 before the lifecycle existed and pass 6/6 after it;
the existing occupancy power harness remains **75 PASS / 0 FAIL**. No live
state changed and no package/image rebuild occurred. The complete gate remains
green at **549 passed / 5 skipped**, trace integrity 0 with the unchanged 24
legacy orphans, and clean doc navigation apart from its two known warnings.

**2026-09-09 — B14 review-1 credential-boundary fix.** The independent Terra
review correctly rejected the first unit shape: `LoadCredential` isolated the
broker from the renderer but handed the broker the complete `wall.env`, including
unrelated Wi-Fi/share/music secrets. Firstboot now extracts exactly fourteen
Door fields into a 0700 root-owned directory under `/run`, with 0600 value files;
the unit has one `LoadCredential` per field and its Python broker reads only that
credential directory. The directory is volatile, so the Door unit deliberately
has no install target and firstboot disables any prior enablement before starting
it after the sources exist on every boot. Missing required Door values stop the
broker rather than retaining stale volatile values. There is still no second
persistent password file and no secret in argv, logs or renderer scope.

Evidence after the fix: focused image tests **6/6**, application credential and
lifecycle tests **20/20**, `bash -n` clean, `validate_config.py` all checks
passed, and the complete G1 gate **549 passed / 5 skipped** with trace integrity
0 and the unchanged 24 legacy orphans. `systemd-analyze verify` accepted the
unit; its only messages are the known DrvFS executable/world-writable projection,
while autoinstall writes the target unit 0644. No ISO was built and no live panel
state changed.

**2026-09-09 — B14 review-2 stale-credential fix.** The second independent
review found that a supported rerun with host/password removed stopped the unit
but left the previous `/run` files. A later display-on could therefore start the
broker with stale credentials. Firstboot now deletes each exact Door allowlist
file before validating and rendering the replacement. Missing required values
leave no reusable source and stop the unit; `wall-sleep.sh` also requires current
host and password files before its display-on path starts the idle broker. This
is defense in depth around the same volatile-source invariant. Review 3 remains
required. Focused tests are **6/6**, both edited shell scripts parse cleanly, and
the complete G1 gate remains **549 passed / 5 skipped**, trace integrity 0 with
the unchanged 24 legacy orphans. No image or live-panel change has occurred.

**2026-09-09 — B14 independent review 4 ACCEPT.** The fourth Terra pass verified
the stale-credential and expiry repairs plus the application broker cleanup and
protocol fixes, then found no new in-scope concrete failure. The image work is
review-complete and remains undeployed; only the Owner-run physical panel checks
remain for B14.

**2026-09-09 — B14 restart record aligned.** The Current State header no longer
says review fixes are awaiting verification. It records the accepted image and
application boundary, the later accepted full-height Door task surface, and the
remaining Owner-run physical checks. No source, image or live-panel state
changed. The full G1 gate passes with **549 passed / 5 skipped**, trace integrity
0 with the unchanged 24 legacy orphans, and documentation navigation at 0 broken
links with its two known orphan warnings. The first Windows run selected the
Microsoft Store bash shim and produced seven path-conversion failures; putting
Git Bash first on `PATH` produced the recorded green result.

### DRIVER — B11 steps 3-4 — 2026-09-09 (token minting + one-shot capture, side branch)

**What changed.** `stack/weight/weight_oauth.py` (new) with two subcommands and
no parser; `tests/test_weight_oauth.py` (new); `stack/weight/README.md` now
carries the Owner's exact commands, what each prints, and a failure table for
each; `weight_feeder.read_google_health`'s refusal message points at the new
tool instead of restating steps the Owner has already cleared;
`stack/.env.example` says where `WEIGHT_TOKEN_FILE` comes from.

**The decisions, and why.** (1) The EXISTING OAuth client is reused — the tool
reads only `OAUTH2_PROXY_CLIENT_ID`/`_SECRET` out of `stack/.env`, key by key,
never `source`-ing a file that also holds the DNS admin password and the
Cloudflare token. (2) The minted file holds **no client secret** (the feeder
gets the pair from its unit's `EnvironmentFile=`, so the secret stays in one
place) and **no access token** (worthless in an hour). (3) The token write goes
through the feeder's guard with its sign reversed — one allow-listed path,
resolved with `realpath`, contained in the service's own `StateDirectory=`,
never the state file, and opened by the feeder's own
unlink-then-`O_CREAT|O_EXCL|O_NOFOLLOW` door at 0600 — in a separate module,
because a credential-writing door does not belong inside the module whose
guarantee is that it has none. (4) Every outbound call uses
`weight_feeder.vendor_opener()`: redirects refused, no proxy inherited, because
urllib does not strip `Authorization` across a cross-host 302 and this token
also grants blood glucose, body fat, oxygen saturation, core body temperature
and heart rate. (5) Nothing secret is printed anywhere; a remote's error body is
never echoed, only a single short `error` enum lifted through an allow-list
pattern.

**Evidence.** `python scripts/check.py` → **RESULT: PASS**, **574 passed / 6
skipped** (this session's own baseline on this worktree was 547 / 5),
`scripts/trace.py --strict-integrity` integrity=0 with the unchanged 24 legacy
orphans, `check_flows.py --no-placeholders` OK (5 diagrams, 11 ids),
`validate_config.py` ALL CONFIG CHECKS PASSED. `stack/run-hermetic-tests.sh` is
**UNRUN** on this dev PC (it refuses without zstd/rsync) and is reported as
unrun, not as passing. **Mutation runs: 19 deliberate defects, 19 RED, restored
byte-identical and green** — including one first-pass SURVIVOR that was a test
defect (the capture output's credential-name refusal was asserted only through
`capture_output_verdict`, so deleting the call in `open_capture_output` stayed
green; the test now opens the door itself).

**What has never run against Google, stated plainly.** Every test drives a
loopback HTTP server this repo starts. The consent screen, a real authorization
code, a real token exchange, a real refresh, a real `dataPoints.list` response
and the real 401/403 bodies are **unexercised**. Real behaviour IS exercised for
the egress guards (a real 302 and real proxy variables on real sockets), the
write guard (a real symlink on a real filesystem), the overwrite refusal, and
the no-secret-printed property (sentinels carried through the whole flow).

**Not done here.** No parser, no push, no deployment, no panel or hub state
touched, no apt package added. This work sits on `b11-weight-token` and is owed
a merge into `IceDrive-DesktopDirection`.

### DRIVER — B11 correction — 2026-09-09 (the OAuth client variables the hub REALLY has)

**Found against the live hub, not inferred.** The coordinator listed the key
names in the deployed `/opt/homehub/stack/.env` over SSH. It holds
`OAUTH2_PROXY_CLIENT_ID` (set), `OAUTH2_PROXY_CLIENT_SECRET` (set),
`TRACKER_DRIVE_USER` (set), `TRACKER_DRIVE_SHEET_ID` (set) and
`TRACKER_DRIVE_FOLDER_ID` (**empty**). It has **no `TRACKER_DRIVE_CLIENT_ID`,
no `TRACKER_DRIVE_CLIENT_SECRET` and no `GOOGLE_CLIENT_*` of any kind.** The
household therefore has **exactly one** Google OAuth client — oauth2-proxy's —
which is the good outcome: one secret to rotate, nothing to drift.

**What was wrong.** The prose in `stack/weight/README.md`, in
`weight_feeder.py`'s docstring and in `weight_oauth.py`'s own docstring all
asserted that the one client was "the one `oauth2-proxy` and
`TRACKER_DRIVE_CLIENT_ID` share". That variable has never existed; the
tracker's Drive sync reaches for the `OAUTH2_PROXY_*` pair directly. The
refusal message for a missing client compounded it by naming only keys that do
not exist on this box, which is what would have sent the Owner looking for a
variable nobody had ever set.

**What changed.** `weight_oauth.resolve_client` is now the one place the client
is settled, and the order is explicit and injectable: `OAUTH2_PROXY_CLIENT_ID`
+ `OAUTH2_PROXY_CLIENT_SECRET` first, `TRACKER_DRIVE_CLIENT_ID` +
`TRACKER_DRIVE_CLIENT_SECRET` second, **first complete pair wins, and a pair is
complete only when both halves are set** — half a pair is refused rather than
completed from the other pair, because a mismatched id/secret fails at Google
as `invalid_client` and reads as Google's problem. The refusal names **all
four** variables and says which are set, inside a fenced `Looked for, in
order: … .` clause so a test can assert against the search list itself. The
minted token file records **which** id variable was actually used, so a
fallback box does not claim a client it did not use. The `.env` reader is
unchanged: key by key, last wins, never `source`d, no value ever printed.

**Prose corrected.** `stack/weight/README.md` step 1 now names oauth2-proxy's
client plainly and carries a dated note saying what the live `.env` really
holds; its step 3 states the lookup order. `weight_feeder.py`'s blocker list
says the same. `stack/.env.example`'s Drive-sync block now says outright that
no `TRACKER_DRIVE_CLIENT_*` variable exists anywhere.

**Evidence.** `python scripts/check.py` → **RESULT: PASS**, **578 passed / 6
skipped** (baseline at `af726b4` on this worktree: 574 / 6; the four new tests
are the whole difference). `scripts/trace.py --strict-integrity` integrity=0
with the unchanged 24 legacy orphans; `check_flows.py --no-placeholders` OK;
`validate_config.py` ALL CONFIG CHECKS PASSED. `stack/run-hermetic-tests.sh`
**UNRUN** on this dev PC (it refuses without zstd/rsync).

**Mutation runs: 5 deliberate defects, 5 RED, restored byte-identical and
green** — including **one first-pass SURVIVOR that was again a test defect**.
Cutting the search list back to the tracker pair left the whole suite green,
because the refusal's later sentence names the oauth2-proxy pair for an
unrelated reason and the test searched the whole paragraph. The message now
fences its looked-for clause between `LOOKED_FOR_PREFIX`/`LOOKED_FOR_SUFFIX`
and the test asserts inside that clause; the mutation is now RED.

**Not done here.** No parser (step 5 is still owed a real captured body), no
push, no hub state touched. Source and docs only, on `b11-weight-token`.

## Audit — 2026-09-09 B11 cross-review fixes (`b11-weight-token`, local only)

A cross-review by a different model family returned **REJECT, 10 CONFIRMED + 1
SUSPECTED** against `8a1d2a5`. The coordinator accepted all of it. Everything
below is source, tests and docs on the side branch; **nothing was pushed and no
hub state was touched.**

**PRIORITY 1 — the hand parser could source a WRONG GOAL. One root cause, four
doors.** `parse_definitions_file` ignored indentation **depth**, so anything
shaped like `id:` / `target:` / `unit:` was read as a direct item field wherever
it sat. A nested `metadata:` mapping (P1), a nested `alternatives:` list under a
*different* item (P2), a second `items:` block (P3) and a **tab-indented** block
that `yaml.v3` will not parse at all (P4) each yielded a confident 170 lb goal.
This is the plausible-but-wrong-number failure class the whole block exists to
prevent, and it defeated the "found by **id**, not by shape" property the
previous round tested for.

The fix is **one rule, not four patches**: a field belongs to an item only at
that item's own field column; the sequence indent is fixed by its first `- `
entry and the field column by the first field on that entry; anything deeper is
a nested container's content and is not the item's, and a `- ` deeper than the
sequence indent is a nested list's entry and is not an item. **Ambiguity is
refused rather than resolved** — a second `items:`, an inline `items: [...]`, or
a tab in the indentation all raise, the same way two files declaring the goal
already did. Two blind spots were closed on the way: a sequence written at
column zero (valid YAML the old reader could not see at all) is now read, and an
item written with `-` alone on its line is read.

**Hand-parsing was KEPT, deliberately.** The service is stdlib-only by design (a
plain unit under `ProtectSystem=strict`, no venv), so PyYAML would mean a new
apt package name and the offline apt export re-run §5 requires — and a full
parser is the wrong *shape* anyway: anchors, aliases and merge keys let a goal
arrive from a line the person cannot see beside the number, and what this reader
owes the household is to read the narrow block subset the sheet generates and
**refuse** everything else. PyYAML is used as a **test-only oracle** where it
happens to be installed (the precedent is `validate_config.py`, which SKIPs
cleanly without it), so "narrow" cannot quietly become "different".

**PRIORITY 2 — the OAuth flow now enforces what it claims.** `code_from_paste`
returned a **bare code before the parser ran**, and checked `state` only when
one happened to be present — so the two easiest pastes skipped the check the
tool documents. `state` is now **mandatory**; a bare code is **refused** (PKCE
binds the code to this process, but `state` is the half that binds the
*response* to the request this run made, and a bare code carries none — there is
no weaker fallback, only "checked" and "not checked"; the cost is one browser
trip the Owner is already at). Duplicated `code=` / `state=` / `error=` are
**refused, not ranked first-wins**, so the tool cannot exchange a code the Owner
is not looking at. The ordering is asserted behaviourally: on a state mismatch
the fake token endpoint records **no request at all**.

**PRIORITY 3 — the containment claim is now true.** `O_NOFOLLOW` protects the
**final** component only, so replacing an intermediate directory after the
verdict resolved sent the write outside the state directory with every guard
above already passed (C1). The open now walks from the state root **one
component at a time** with `O_DIRECTORY|O_NOFOLLOW` and `dir_fd=` (stdlib,
Linux), creating the leaf against a directory handle; Windows has no `dir_fd`
opens, so the dev PC checks each component with `lstat` — that fallback is
check-then-use, does not close the race, and is not claimed to. (C2)
`token_write_verdict()` was handed the `--token-file` override as **both** the
path and the allow-list, so the one-path allow-list compared it **to itself** and
was vacuous exactly whenever the flag was used. The allow-list is now the
**configured** `WEIGHT_TOKEN_FILE`; the flag remains an operator escape hatch —
per the ruling, a documented flag on a tool run under `sudo` is not a
vulnerability — but it now **announces itself**, names the configured path, and
says which guards still bind the write. A guard that silently stops deciding was
the defect; the hatch was not.

**PRIORITY 4 — definitions symlinks.** The directory is the tracker's own docker
volume and stays inside the trust boundary: `WEIGHT_DEFINITIONS_DIR` may itself
be a symlink and is still read. The cheap half was taken — an individual `*.md`
resolving **outside** that directory is refused rather than read.

**Nothing was weakened.** All six vendor-absence assertions are green and no
parser was written; the no-leak property, `vendor_opener()`, the client
resolution order, the unit-before-number rule, the both-keys-present refusal,
the two distinct refusals (no source → unavailable gauge; no goal → nothing
posted) and the freshness invariant are unchanged and still asserted.

**Evidence.** `python scripts/check.py` → **RESULT: PASS**, **626 passed / 6
skipped** (baseline at `8a1d2a5`: 599 / 6 — the 27 new tests are the whole
difference). `scripts/trace.py --strict-integrity` integrity=0 with the
unchanged 24 legacy orphans; `check_flows.py --no-placeholders` OK, 5 diagrams;
`validate_config.py` ALL CONFIG CHECKS PASSED. `stack/run-hermetic-tests.sh`
**UNRUN** on this dev PC (it refuses without zstd/rsync).

**Mutation runs: 17 deliberate defects (M46–M62), all RED, restored
byte-identical and green** — including **one first-pass SURVIVOR that was, for
the fourth round running, a TEST defect**. M62 dropped the state root from the
token write's open and the suite stayed green: the test planted its symlink on a
path pointing *out* of the root, so `token_write_verdict` refused it before the
open was ever reached — the **verdict was carrying the check**. The link now
points at a directory *inside* the root, which is precisely the case the verdict
cannot refuse (both sides resolve to the same contained file), leaving the open
as the only thing that can. M62 is now RED.

**One check is carried by another, and it is named rather than hidden.**
`test_open_for_write_hands_the_state_root_to_the_open_sr022` is a **wiring**
assertion (it records what the caller passes); the containment behaviour itself
is proved against the real filesystem by
`test_an_intermediate_directory_swapped_after_the_verdict_is_refused_sr022`. And
at the `mint` level the allow-list has **no observable behaviour** — it can only
differ from the effective path when `--token-file` is used, which is the
announced override — so C2 is carried by the verdict's own unit test plus the
notice test, not by an end-to-end refusal.

**Not done here.** No parser (step 5 is still owed a real captured body), no
push, no hub state touched. Source, tests and docs only, on `b11-weight-token`.

## Audit - 2026-09-09 (later) B11 step 5: the Google Health weight parser (`IceDrive-DesktopDirection`, local only)

**THE GATE WAS CLEARED BY A REAL BODY, AND THAT IS THE HEADLINE.** From the
start of B11 this repo refused to write a Google Health parser, and
`tests/test_weight_feeder.py` asserted the refusal by name in six places
(`parse_google_health` / `parse_weight_datapoint` / `parse_weight` /
`parse_datapoints`, across two tests). On **2026-09-09** the Owner ran
`weight_oauth.py capture` against the live API and got **HTTP 200, 712 bytes**,
one data point, from
`GET https://health.googleapis.com/v4/users/me/dataTypes/weight/dataPoints` with
no query parameters. B7's standard was **met, not waived**. The six absence
assertions are now **replaced by correctness assertions** - this paragraph is
the record that they were removed deliberately and why, rather than a later
reader finding them simply gone. The absence assertion in
`tests/test_weight_oauth.py` **stays**: the capture tool must still not
interpret a body, and it still fails if that file so much as mentions
`weightGrams` or `sampleTime`.

**THE CAPTURED BODY IS NOT IN THIS REPO AND NEVER WILL BE.** It carries the
Owner's Google user id (in `name`) and their real body weight. What is recorded,
in the module docstring and in the test fixtures, is its **shape**, with a
placeholder id and a weight that is not the Owner's. The `name` field is not read
by the parser at all, which is why the sentinel a test looks for cannot appear.

**WHAT THE BODY SETTLED, AND WHAT IT DID NOT.**

| observation | consequence |
|---|---|
| the top-level key is **`dataPoints`, plural** | earlier prose in this repo and in `README.md`'s troubleshooting table guessed `dataPoint`. Both corrected; the observation wins. |
| **`weightGrams` is grams** | `grams_to_pounds` (already present, already tested) is the one conversion, reached from a vendor body only through `check_vendor_grams`. |
| `observed_at` is `weight.sampleTime.physicalTime` | the instant the reading was TRUE, which is what NagLight's staleness rule keys off. Never `now`. |
| `civilTime` is **2026-09-08 20:24** while `physicalTime` is **2026-09-09T01:24Z** (`utcOffset: -18000s`) | a **proven** trap. The gauge uses `physicalTime`; anything needing the reading's **calendar day** - the weigh-in auto-check-off SN-040 gestures at next - must use `civilTime`, or it ticks off Tuesday for a Monday-evening weigh-in every time. `WeightReading` carries `civil_date` and `utc_offset_seconds` so the next author is handed them. No check-off was built. |
| the response carried **no `nextPageToken`** | so paging is an **ASSUMPTION**, marked as one in `list_weight_data_points`: a token is followed up to `MAX_LIST_PAGES` (20) and a deeper history is **refused** rather than answered from a partial walk, because "the latest reading" would then be a claim the code cannot support. Refusing costs nothing real - the failure path re-posts the last real reading at its original stamp. |

**LATEST, NOT FIRST.** The captured body held exactly one point, so "the first
element" and "the newest" were indistinguishable in the only evidence anybody
has. The parser orders by `physicalTime` explicitly. Two points sharing the
newest instant with **different** weights are refused rather than ranked - the
rule this module already applies to two files declaring a goal and to a target
declared twice.

**AN EMPTY HISTORY IS NOT A BROKEN SOURCE, AND THE DISTINCTION IS A TYPE.**
`NoWeightYet` is a **subclass** of `SourceFailure`. The subclassing is what keeps
the outcome identical - the ordinary unavailable gauge, value 0 with **no**
`observed_at`, never a reading of 0 - and the distinct type and sentence are what
stop a human reading "your token expired" as "you have never weighed yourself".
A body with **no `dataPoints` key at all** is a different thing again and is
refused rather than read as an empty history: it is a shape nobody has seen.

**NOTHING WAS WEAKENED.** The freshness invariant (`observed_at == now` **iff**
this cycle read the source), the credential write guard, `vendor_opener()`, the
OAuth client resolution order, the unit-before-number rule, the both-keys
refusal, the two distinct refusals (no source -> unavailable gauge; no goal ->
nothing posted) and the depth-aware definitions reader are all unchanged and
still asserted. Two things were **added** to the no-leak property rather than
subtracted from it: the parser's messages name a **field** and a **type** and
never a **value** (a well-formed `weightGrams` *is* the Owner's body weight, and
`run_cycle` journals a `SourceFailure`'s message), and `WeightReading.__repr__`
prints neither the weight nor the stamp, because a repr reaches tracebacks. The
feeder duplicates `weight_oauth`'s OAuth client resolution order and token
endpoint - it cannot import that module, which imports *it* - and a parity test
asserts the two copies equal, the same shape `tests/test_feeder_egress_parity.py`
uses for the two feeders.

**MUTATION: 36 mutants over three rounds, 0 survivors - after two TEST defects
were found and fixed, the fifth round running that a first-pass survivor was a
test problem and not a code problem.** Round 1 (26 mutants, including
grams-as-pounds, grams-as-kilograms, an inverted conversion and a rounded 2.2046
factor) went green immediately, which this build has learned to distrust. Round 2
aimed ten mutants at the credential and no-leak surface and **two survived**,
both because the FIXTURE ON THE FAILING PATH DID NOT CARRY THE SECRET: a mutant
printing the token file's parsed contents survived against a blank `{}` file, and
a mutant printing the token endpoint's whole payload survived against a response
with nothing sensitive in it. Neither test could have failed. The fixtures now
carry a sentinel on every path they drive, and both mutants are RED.

**One assertion was found carried by a bystander sentence, by inspection rather
than by mutation.** The vendor redirect test asserted `"refused" in message` -
but if the redirect were **followed**, the second server answers `{}` and the
parser's absent-`dataPoints` refusal *also* contains the word "refused", so the
assertion would have passed on the exact behaviour it exists to forbid. It now
asserts the 302 by status code and the redirect refusal's own words, which
nothing else can produce; the "the other server received nothing at all"
assertion was always the load-bearing one.

**One check is carried by another, named rather than hidden.** In
`test_the_vendor_read_never_writes_the_token_file`, the closing `open_for_write`
refusal is carried by `CREDENTIAL_BASENAMES` (the fixture is named
`google-health-token.json`) - a pre-existing guard with its own tests. The new
assertion in that test is the file's bytes and mtime being unchanged across a
whole successful read.

**Evidence.** `python scripts/check.py` - **PASS**, 675 passed / 6 skipped
(baseline on this branch before the change: 628 / 6). `scripts/trace.py
--strict-integrity` - integrity 0. `check_flows.py --no-placeholders` - OK.
`stack/run-hermetic-tests.sh` - **UNRUN**: it refuses on this dev PC (missing
`zstd` and `rsync`).

**Not done here.** No push, no hub state touched, no deploy. The weigh-in
auto-check-off was **not** built - the parser only stops throwing away what such
a caller would need. The `filter` query parameter (`GOOGLE_HEALTH_FILTER`) is
still an unused constant: adding an unexercised query parameter to the single
request shape that is KNOWN to work is the bet this gate exists to refuse.

## Audit - 2026-09-09 (later still) B11 - scoped definitions read access (`IceDrive-DesktopDirection`, local only)

**THE FEEDER COULD NOT READ THE GOAL ON THE HUB, AND THE FIX IS A NARROWER
MOUNT, NOT A WIDER ONE.** `setup-weight.sh` wrote
`BindReadOnlyPaths=$WEIGHT_DEFINITIONS_DIR` - the definitions directory, mounted
at its own path. Established empirically on the hub with `systemd-run` as the
real service account, and not re-derived here:

| what was bound | result |
|---|---|
| the definitions directory, at the same path | **NOT-READABLE** |
| the definitions directory, at a target the account owns | **NOT-READABLE** |
| the one category **file** (0644), into the account's `StateDirectory` | **READABLE**, `touch` denied |

The directory is `drwx------ hub hub`, and its ancestors are root-only
(`/var/lib/docker` is `drwx--x---`, nothing for "other"), so no mount target
rescues a directory bind. `setfacl` is **not installed** - installing it would
add an apt package name and owe the §5 apt-export re-run - and an ACL would be
**destroyed on the next sync** anyway, because NagLight's
`internal/store/definitions.go` applies definitions by `os.MkdirTemp` + populate
+ **rename into place**, replacing the `definitions` inode wholesale.

**SCOPE IS THE REASON, NOT THE WORKAROUND.** The volume holds **every** household
member's tracker data. A gauge about one person's body must not hand a service
account read access to all of it. The drop-in now exposes exactly one file:

```
BindReadOnlyPaths=-<the category file>:/var/lib/homehub-weight/definitions/<its name>
```

**WHICH FILE IS OBSERVED, NEVER DERIVED.** `setup-weight.sh` runs as root, can
read the directory, and finds the `.md` whose **top-level** `category:` matches
`WEIGHT_ITEM_CATEGORY` - trimmed and case-folded, with the same `clean_scalar`
quote/`# comment` handling `find_goal_item` uses. It does **not** lowercase
`Health` into `health.md`: that slug rule lives in NagLight and can change
without telling us, and this build's standing lesson is to observe rather than
infer. The test tree is booby-trapped both ways round (the file carrying
category `Health` is `tracker-2b.md`, and a `health.md` exists declaring
something else), so a guessing script binds another member's tracker and reports
success. Only the frontmatter counts, and only column zero - a `category:` under
`items:` is an item field, one in the prose body is a person thinking out loud.
**Two files declaring the category is REFUSED**, and so is one file declaring
`category:` twice: the same "which one is authoritative is not guessable" the
feeder already applies.

**THE ORDERING WAS MEASURED, AND THE OBVIOUS CONSTRUCTION IS WRONG.** The task
proposed `EnvironmentFile=` supplying the source value and a drop-in
`Environment=` overriding it. Run on real systemd 255 (WSL Ubuntu on the dev PC;
no hub state touched), **`EnvironmentFile=` assignments are applied AFTER every
`Environment=` assignment regardless of order** - all four orders were run, and
`Environment=` lost in every one, including the drop-in case and the
same-file-Environment-second case. Two `EnvironmentFile=` lines **are** applied
in parse order, last wins, and drop-ins parse after the unit. So the override is
a generated file, `homehub-weight.service.d/20-definitions.env`, and
`10-account.conf` points `EnvironmentFile=` at it. A test fails if anyone
"simplifies" it back to `Environment=`.

**A MISSING SOURCE MUST NOT WEDGE THE UNIT**, so the bind carries systemd's `-`
prefix. Measured on 255: without it, a renamed-away source fails the unit at
**226/NAMESPACE** before the feeder runs, repeating every fifteen minutes with
nothing in the journal about why; with it the mount is skipped and the feeder
reaches its own named refusal. **Recorded honestly:** that path is `GoalMissing`,
which exits **2**, so the unit is still recorded failed - but it is a diagnosed
failure naming the item and the category, on the existing tested "no goal ⇒
nothing posted" path, not an undiagnosed namespace error. Changing that exit code
would weaken one of the two distinct refusals and was not done. If **no** file
declares the category at provisioning time, no bind line is written,
`setup-weight.sh` warns loudly and exits **0** - an unsynced tracker is a normal
state at firstboot and must not stop it.

**STALE INODES DO NOT APPLY, AND THE ONESHOT SHAPE IS WHY.** The tracker replaces
the whole `definitions` directory on each sync; this unit's namespace is torn
down and rebuilt on every timer tick, so each run resolves the bind against
whatever is there now. Recorded in the unit and in the README so the next reader
does not re-open it.

**A MEASURED SIDE EFFECT, HANDLED.** systemd creates a missing bind destination
and **leaves it behind as an empty file on disk**. So `setup-weight.sh` sweeps
stale `*.md` out of `/var/lib/homehub-weight/definitions` before writing the
drop-in, and creates that directory `root:root 0755` inside the account's own
`StateDirectory` - the account must READ what is mounted there and must never be
able to drop a file of its own beside it and have the feeder read that as the
household's goal.

**END-TO-END ON REAL SYSTEMD, not only in unit tests.** The generated drop-in was
dropped beside the shipped unit on WSL systemd 255, with the definitions
directory in the hub's real shape (0700, owned by another account, under a
0710 parent) and a second member's tracker beside the goal file. As the service
account: `WEIGHT_DEFINITIONS_DIR=/var/lib/homehub-weight/definitions`, the host
directory unreachable, **only** `tracker-2b.md` listed, `target: 170` readable,
write denied (`Read-only file system`). With the source deleted, the unit's
namespace still came up and only an empty stub was visible.

**Evidence.** `python scripts/check.py` **694 passed / 6 skipped** (baseline 676/6
at `9c017d2`), RESULT: PASS; `trace.py --strict-integrity` integrity 0, orphans
24 = baseline; `check_flows.py --no-placeholders` OK; `stack/run-hermetic-tests.sh`
**UNRUN** on this dev PC (missing zstd/rsync), as before. **18 mutants, 18
killed - but only after a second round.** The first 14 all died on the first
pass, which this build has learned to distrust, so a second set was aimed at
what the first set could not reach and produced **four survivors, every one a
TEST defect**: the .env-file read path (every test handed the script its knobs
through the process environment with `WEIGHT_ENV_FILE=/nonexistent`, which is
NOT how firstboot runs it - dropping `WEIGHT_ITEM_CATEGORY` from the keys the
script reads out of `stack/.env` left the whole suite green while a household
that had moved its goal would silently get `Health`), the two install-path
hardening lines (root-owned mount target, stale-stub sweep), and the
`--emit-dropin` argument guard. Tests added for all four; all now killed. Two
checks are carried by a helper rather than by the test that names them:
`_one_bind` asserts both "exactly one bind" and the `-` prefix, so several
tests go red for M3/M4, and the install-path test is a TEXT assertion (that
code runs only as root on the hub) - labelled as weaker in its own docstring
rather than dressed up.

**Not done here.** No push, no hub state touched, no deploy, no new apt package,
no new knob (`WEIGHT_ITEM_CATEGORY` already shipped in `.env.example`). The
dedicated unprivileged account, the generated account drop-in,
`ProtectHome=read-only`, `ProtectSystem=strict`, the credential write guard, the
no-leak property, the freshness invariant, the parser and its unit conversion,
the depth-aware definitions reader and the two distinct refusals are all
unchanged, and the drop-in still derives `User=`/`Group=` from the knob.

**Owed to the coordinator.** `/opt/homehub` is not git and lags the repos, so the
deploy is: copy `stack/weight/setup-weight.sh` and
`stack/weight/homehub-weight.service` to the hub, then run
`sudo bash /opt/homehub/stack/weight/setup-weight.sh` (it rewrites the unit, the
drop-in and the override, and reloads), then `systemctl start
homehub-weight.service` once and read the journal. The script prints which file
it resolved; if it prints the WARNING instead, the category file has not synced
and nothing is bound yet.

---

**2026-09-09 B11's automated check-off — BUILT (SR-022/LLR-006/TC-006). This
SUPERSEDES the "Auto-check-off: what it would take, and why it is not built"
section above.** That section named three hazards and refused to build on the
first two; the item changing to `type: automated` / `check: weight` settled the
route, and all three are now answered — but **not** in the shape that section
guessed at. It assumed `POST /api/check {id, done, expectedDate}`. The actual
route is `/api/feed`'s **legacy lane**, and it carries no `expectedDate` and no
back-dating of any kind.

**What is posted.** Two bodies per cycle to one endpoint, gauge first:

```
1.  {"kind": "gauge", "id": "weight", "unit": "lb", "value": …, "target": …, "observed_at": …}
2.  {"check": "weight", "ok": true}
```

The second carries **no `kind`** (that selects the gauge lane; `case "":` is what
routes to the boolean struct), **no `color`/`rgb`** (the handler counts signals
among `ok != nil`, `color != ""`, `rgb != ""` and refuses anything but exactly
one), **no `note`** and **no `at`**.

**Hazard 1 — "fire on a new `sampleTime`, not on a successful read" — answered
as four gates, all required.** A genuinely fresh read this cycle (not "the cycle
worked", not the gauge's own `fresh` flag); a `sampleTime` strictly newer than
the one already ticked; a day the reading can actually name; and that day being
today in the reading's **own** local frame (`now + utcOffset`, so no timezone
database and no guess about where the household lives). The source-failure
re-post path — which reposts the last real weight at its original stamp — can
never tick. The feeder also never posts `ok: false`: it can never know somebody
did *not* weigh in, and `ok: false` routes to `engine.Uncheck`, which would
silently erase a tick the Owner made by hand.

**Hazard 3 — idempotency — read rather than guessed.** `engine.Check` sets
`li.Done = true`, clears any report and calls `SaveDay` unconditionally. So a
repeat is **not** a toggle (the retry hazard that section feared is not real),
but it is also not free: every repeat is a `SaveDay`, a panel wake via
`bumpRevision`, and on a single-user box a git commit. Gate 2 above is what
keeps that to one write per weigh-in rather than 96 a day.

**Hazard 2 — the DAY — is the one that is NOT fully fixed, and that is the
finding.** The legacy `ok` path **cannot back-date**: `body.At` is parsed only
inside `if body.Color != "" || body.RGB != ""`, and the boolean path falls
through to `date := s.Now()`. `s.Now()` defaults to `todayString` =
`time.Now().Format("2006-01-02")`, the **tracker container's** local date — and
`stack/docker-compose.yml`'s `tracker:` service sets **no `TZ:`**, while every
other service that cares sets `TZ: ${TIMEZONE}`. So the tick lands on **UTC
today**. For the shape the capture proved — 20:24 local on Monday = 01:24 UTC on
Tuesday — the tick lands on **Tuesday's** box. Gate 4 stops the large errors (a
reading recovered days later after an outage is not ticked at all); it cannot
stop this ≤1-day one. Sending an `at` was rejected as *worse* than not sending
one: it would be silently dropped and would look as though back-dating worked.

**SUPERSEDED 2026-09-10 — the decision was made and the `TZ:` was added; see
the entry at the foot of this file.** The paragraph below is kept as the record
of what was owed and why.

**OWED TO THE COORDINATOR — one decision.** Adding `TZ: ${TIMEZONE}` to the
`tracker:` service in `stack/docker-compose.yml` would make `s.Now()` the
household's local date and put evening weigh-ins on the right day. NagLight's
own Dockerfile installs `tzdata` for exactly that reason ("correct local *today*
for the nightly materialize"), so the missing `TZ:` reads as a pre-existing gap
rather than a decision — **but it moves the day boundary for every item in the
tracker, not just this one**, and other sessions are in NagLight and HomeHub.
That is why it was not done here. A test asserts the `tracker:` block still has
no `TZ:`, so whoever adds one is sent to `stack/weight/README.md` first.

**No new knob, and that is an argued choice, not an omission.** `type:` and
`check:` are the declaration: they belong to the person, they round-trip through
Drive sheet mode as item columns, and they are what NagLight itself reads. A
`WEIGHT_CHECK_ENABLED` beside them would be a second place the same intent
lives, and the two would disagree the first time the Owner changed their mind
from a phone. So **nothing is added to `stack/.env.example` and nothing is owed
to HomeHub's `FieldSchema.psd1`** (which is out of this block's bounds anyway) —
a test scans both for `WEIGHT_CHECK_*` names and fails on one.

**State.** `{"checked": {"weight": <epoch sample time>}}` lives in the **existing**
`weight-state.json`, written by the same `save_state` through the same
`open_for_write` guard. A second state file would be a second path the
credential allow-list has to bless, so there is not one. The mark is written
**only after a 200**, so a tick that failed to post is retried rather than
remembered as done. `load_state` validates the marks the way it validates
readings — a mark in the future is dropped, because it would suppress every real
tick until the clock caught up.

**One pre-existing decision was revisited.** `read_google_health` used to return
`(pounds, observed_at)` and deliberately drop `civil_date`/`utc_offset_seconds`,
on the reasoning that a caller needing the calendar day would call
`parse_weight_datapoint` itself. That caller is `run_cycle`, which reaches the
vendor only through `read_google_health` — so honouring the old shape would have
meant a **second** `dataPoints.list` call carrying the access token every 15
minutes to recover a field the first call already had. It now returns the whole
`WeightReading`, and `reading_pair` takes the gauge's two numbers off it; a test
asserts exactly two requests reach the loopback.

**Nothing was weakened.** The freshness invariant, the credential write guard,
the no-leak property (the tick body carries no weight, no id, no token — the
handler's `Note` field is decoded and never read by anything, so a note would
have been a leak for no gain), `vendor_opener()`/`feed_opener()`, the OAuth
client resolution order, the unit-before-number rule, the parser and its grams
conversion, the depth-aware definitions reader, the scoped single-file
definitions bind, and the two distinct refusals (no source → unavailable gauge;
no goal → nothing posted) are all unchanged. The definitions walk was **factored
out**, not duplicated: `scan_definitions` is the one containment rule, and both
the goal loader and the check-id reader call it.

**Mutation runs: 17 mutants, and the first pass had two survivors.**

| # | mutation | test that must go red | result |
|---|---|---|---|
| M1 | the "strictly newer sample" gate deleted | `…same_weigh_in_is_ticked_once…` | RED |
| M2 | the mark written whether or not the tick landed | `…refused_tick_leaves_the_gauge_posted…` | RED |
| M3a | `run_cycle`'s outer "did we read?" gate removed | `…dead_source_does_not_even_read…` | **survived first pass**, RED after |
| M3b | both "did we read?" gates removed | `…dead_source_reposts_the_gauge_and_never_ticks…` | RED |
| M4 | the day gate always passes | `…another_day_is_never_ticked…` | RED |
| M5 | "today" taken in UTC, not the reading's local frame | `…fresh_weigh_in_posts_the_gauge_and_then_the_tick…` | RED |
| M6 | a `note` added to the tick body | `…tick_body_carries_no_kind…` | RED |
| M6b | a `kind` added (routes to the gauge lane) | `…tick_body_carries_no_kind…` | RED |
| M13 | an `at` added (silently dropped by the ok path) | `…cannot_be_back_dated…` | RED |
| M7 | the tick posted BEFORE the gauge | `…fresh_weigh_in_posts_the_gauge_and_then_the_tick…` | RED |
| M8 | a failed gauge post suppresses the tick | `…refused_gauge_does_not_prevent_the_tick…` | RED |
| M9 | the marks get a state file of their own | `…tick_mark_lives_in_the_one_existing_state_file…` | RED |
| M10 | `load_state` drops the marks again | `…same_weigh_in_is_ticked_once…` | RED |
| M11 | `type:` not read, so a habit item is ticked | `…reverting_the_item_to_a_habit…` | **survived first pass**, RED after |
| M12 | the check id hardcoded instead of read | `…check_id_is_whatever_the_item_declares…` | RED |
| M14 | a tick mark in the FUTURE trusted | `…tick_mark_that_is_not_credible…` | RED |
| M15 | the vendor read drops the day facts again | `…fresh_weigh_in…` / `…whole_vendor_read…` | RED |

**M11 was a TEST defect, and it is the one worth reading.** The test asserted
"`type: habit` does not tick" against the plain `HEALTH_MD` fixture — which has
**no `check:` field at all**, so it was being refused for the wrong reason and
the `type:` gate was never exercised. The shape a revert actually produces is
`type: habit` sitting **next to** a lingering `check: weight`, because Drive
sheet mode round-trips every item column it knows: changing the type column does
not delete the check column. A `HABIT_BUT_STILL_CHECKED_MD` fixture now covers
it, end to end as well as at the unit.

**M3a was a check carried by another.** `run_cycle` gates the definitions read
on `reading is not None` and `should_check_off` gates the tick on the same fact,
so removing either alone changed no observable behaviour. That is deliberate
defence in depth, but the outer gate does buy something real (95 cycles a week
do no definitions I/O), so it is now asserted directly with an injected
`check_loader` that counts calls, rather than left as an untested redundancy.

**Evidence.** `python scripts/check.py` — **715 passed / 6 skipped**, RESULT
PASS (baseline at `23c3f04` was 694/6, so +21 tests). `scripts/trace.py
--strict-integrity` integrity=0. `check_flows.py --no-placeholders` and
`check_docs.py` clean. Every cycle test in the new section runs against a **real
loopback HTTP server**, because a stubbed `urlopen` would prove nothing about
two posts arriving in an order. `stack/run-hermetic-tests.sh` is **UNRUN** — it
refuses on this dev PC (missing `zstd`/`rsync`).

**Not done here.** No push, no hub state touched, no deploy, no new knob, no new
apt package, no change to `stack/.env.example`, no change to HomeHub's
`FieldSchema.psd1`, and no change to `docker-compose.yml`.

---

## 2026-09-10 — the tracker gets the household's timezone (B11, Owner-approved)

**One line of compose, and it moves the day boundary for every item in the
tracker.** `stack/docker-compose.yml`'s `tracker:` service now sets
`TZ: ${TIMEZONE}` at the top of its `environment:` block. `TIMEZONE` is already
in the hub's `.env` (`America/Chicago`) and was already spelled that way by the
**eight** other services in that file that care. The tracker was the only
service that cares and had never been given one — NagLight's own Dockerfile
installs `tzdata`, so the container was built expecting a `TZ` it was never
handed. A pre-existing omission, not a decision.

**What actually changes.** NagLight resolves "today" through Go's `time.Now()`
— `s.Now()` = `time.Now().Format("2006-01-02")` — and that is the day boundary
for **every** item in the tracker: habits, todos, rollovers, streaks,
catch-ups, and the legacy `ok` check-off lane the weight feeder posts on. In
UTC that boundary fell at **19:00 local** in `America/Chicago`. So anything the
Owner ticked between **19:00 and midnight** had been landing on the **next**
day's log — every evening, for every item, not only the weigh-in. After this it
rolls at **local midnight**. Nothing historical is rewritten: the boundary moves
forward only, and the evening of the restart is the seam.

**The weight case this surfaced through.** The feeder's automated check-off
posts `{"check": ..., "ok": true}` on the legacy lane, which **cannot**
back-date: `body.At` is parsed only inside the `color`/`rgb` branch, and the
boolean path falls through to `date := s.Now()`. The captured weigh-in — 20:24
local, 01:24 UTC the next day — therefore ticked the **following** day's box.
With the tracker on the household's zone the tick lands on the day the person
stood on the scale. The feeder is **unchanged**: it still sends no `at` (one
would be silently dropped), and gate 4 still bounds a reading whose own
`utcOffset` is some other zone's.

**A RESTART IS REQUIRED, and it is a RECREATE, not `docker restart`.**
Established, not assumed, in two independent halves:

1. **Docker**: a container's environment is fixed when the container is
   created. `docker restart tracker` re-runs the same container with the same
   environment and would add nothing.
2. **Go**: read out of the toolchain source on this box
   (`.../go/src/time/zoneinfo_unix.go` and `zoneinfo.go`) — `initLocal()`
   consults `$TZ` via `syscall.Getenv` and falls back to `/etc/localtime` when
   `TZ` is unset, and it is called through `localOnce sync.Once`. So the zone
   is resolved **once per process**, and a long-running tracker would keep its
   old clock even if the variable could be changed under it. (A `time.Local`
   probe was written and run here, but the dev PC is Windows, where Go takes
   the zone from the OS and ignores `TZ` outright — so the probe could not
   settle the Linux question and the source was read instead. Recorded because
   a probe that cannot decide is not evidence.)

**What the coordinator runs on the hub**, from the deployed stack directory:
`docker compose up -d tracker` — it recreates the container because the
environment changed. Confirm with `docker exec tracker date` (it should print
the household's local time, not UTC) and `docker inspect tracker` showing
`TZ=America/Chicago` in `Config.Env`.

**The tripwire test was inverted, not deleted.** `eebea9e` had added an
assertion that the `tracker:` block carried **no** `TZ:`, precisely so whoever
added one was sent to read `stack/weight/README.md` first. That has now
happened. The assertion now requires the opposite and requires it to mean
something: the tracker's **environment** must carry `TZ`, and it must be
spelled `${TIMEZONE}` rather than a literal zone, so it cannot drift onto a
different day boundary from the services that share that one value. It is
checked structurally (PyYAML, `services.tracker.environment`) as well as by
text, because a `TZ:` under `labels:` is not a `TZ` the process ever sees — a
mutation that did exactly that **survived** the text-only form.

**Not done here, deliberately.** No push. No hub state touched, no deploy. No
other service given a `TZ:` — the Owner approved the tracker, and widening it
is a separate call. `weight_feeder.py` is untouched.

## 2026-09-10 — SR-023 bounded panel-audio image slice, backend deliberately unavailable

The image now carries the narrow half of the Bluetooth/audio design without
claiming the hardware half. SN-017/SR-023/LLR-007/TC-007/IF-015 trace one
Unix-only, newline-framed JSON protocol; pure trusted-only route restoration and
explicit-input policy; and a stateless visualizer transform that returns only
bounded RMS/peak/bands/activity with generation and monotonic observation time.
The shipped backend exposes an honest `probe-required` status and refuses every
mutation. It executes no command, calls no D-Bus API, opens no audio source and
retains no raw samples.

The unit is stopped by default through `WALL_AUDIO_ENABLED=false`, accepts only
AF_UNIX, and is filesystem/kernel hardened. It provisionally runs as `panel`:
the read-only probe did not find `wpctl`/`pactl`, so neither a PipeWire session
nor a safe cross-user access mechanism has been established. No BlueZ policy,
WirePlumber profile, or additional package was guessed. A dedicated identity is
still the desired least-privilege shape if physical evidence proves it can reach
the selected user-session graph narrowly. Exclusive explicit input selection
and all-routed-non-silent visualizer eligibility are conservative assumptions,
recorded for review rather than presented as physical proof.

Evidence: `python -m pytest tests/test_panel_audio.py -q` — **8 passed**;
`python scripts/check_flows.py --no-placeholders` — **6 flows / 13 ids, OK**.
`python scripts/trace.py` reports the new SR-023 chain complete and the repo's
pre-existing 24 legacy orphans unchanged in class. `validate_config.py` passes
every functional/config/YAML check and fails only its intentional ISO guard
because the new unit is untracked in this uncommitted handoff; it must pass once
the coordinator stages the final patch. No ISO, VM, live-panel route, latency,
radio, suspend/resume, or 30-minute behavior was tested. G1 does not advance.

With the handoff staged temporarily so the ISO-carriage guard could inspect the
real final file set, `python scripts/check.py` passed: **723 tests passed / 7
skipped; RESULT PASS**. The index was then restored to an uncommitted handoff.

### Adversarial broker hardening follow-up

The external review rejected the first slice on nine concrete safety/evidence
gaps. All actionable findings were accepted and implemented: slow same-UID
clients now have read deadlines and bounded concurrent handling; response data
uses positive method-specific schemas with MAC, BlueZ-path and encoded-audio
rejection; mutations have an explicit authorization seam that defaults deny;
aliases are checked against a bounded trusted/kind-correct backend inventory;
and generation check, backend mutation and successful increment are serialized.
The telemetry core validates generation/time types, rejects backwards monotonic
time, applies the configured silence hold and cadence cap, and retains only
timestamps/generation. Offline carriage now accounts for every panel-audio
payload, and first boot fails safe on an incomplete staged unit even while the
feature is disabled.

The review's performance concern was treated as suspected rather than asserted:
a sustained maximum-window test offers 100 windows at 10 ms intervals, proves
the 50 ms cadence admits only 20 transforms, and places a generous bounded
runtime ceiling around the exercised reference host. This is software evidence,
not panel CPU, thermal or latency evidence. Socket behavior is exercised through
real AF_UNIX connections on Linux, including a stalled partial client,
oversized wire request, and mutation authorization. The authorization seam is
not yet wired to a host identity mechanism; until that and the physical backend
are reviewed, the shipped service remains default-off and mutation-denying.

Post-review evidence: `python3 tests/test_panel_audio_socket.py` under Linux —
**3 passed**; focused Windows pytest — **12 passed / 4 platform skips**; and the
staged final image gate `python scripts/check.py` — **727 passed / 11 skipped,
RESULT PASS** (config, registry integrity and documentation checks all passed;
the pre-existing orphan-document warning remains). No physical proof was added,
and G1 does not advance.

## 2026-09-10 — SR-024 Door-motion image boundary (application detector pending artifact)

The image-owned Door-motion slice now traces SN-018 → SR-024 → LLR-008 →
TC-008 and IF-016. `python3-opencv` is named in the wall package SSOT and was
confirmed present in the existing Ubuntu apt metadata; the ordinary resolver,
baked-list stamp, offline installer and installed-package assertion therefore
own its carriage. Release capability validation now refuses an application
artifact missing `doorstream/motion.py`, while the existing ambient capability
also requires `js/ambient-priority.js` in app and site artifacts.

The unprivileged Door unit receives the existing camera fields plus eleven public
detector values through an exact systemd credential allowlist, never through
`wall.env` or argv. Tracked/SIM zones are normalized and topology-free, the SIM
camera host is `.invalid`, diagnostics default false, and the unit has explicit
CPU, memory, task and stop ceilings. Existing power control was confirmed and
made an explicit cross-lifecycle test: display-off and suspend complete a
bounded broker stop
before the power transition; display-on may ready only the idle broker. Motion
still cannot wake a dark panel.

Assumptions pending application/review: detector values use credential names
`motion-enabled`, `motion-calibrated`, `motion-sample-fps`, `motion-min-area-ratio`, `motion-persistence-seconds`,
`motion-dwell-seconds`, `motion-stationary-ratio`, `motion-trigger-zone`,
`motion-road-zone`, `motion-masks`, and `motion-diagnostics`; zones are
`x,y,width,height` normalized tuples and masks are a bounded application-parsed
list of those tuples. Firstboot now validates every range before publication; the private application must emit
only sanitized generation/sequence observations, retain no frames, and keep
visible-frame subscription separate from sampler lifetime.

Focused evidence: the red-first Door/occupancy tests failed five cases before
the image changes, then passed **177 tests / 1 platform skip** with the broader
release-contract fixture set. Ubuntu WSL `apt-cache show python3-opencv`
confirmed the package name for amd64. Physical day/night tuning, camera access,
CPU/thermal load, reconnect, latency and media coexistence remain unproven. No
ISO, VM or live panel was changed, and G1 does not advance.

Final image evidence: Ubuntu WSL `vmtest/test-wall-builder.sh` passed **121
guards / 0 failed / 2 explicit skips**; the skips were the absent current private
shell artifact and absent sibling Personal checkout, not silent passes. It
exercised every new SIM knob-removal refusal and inspected rendered topology-
free values. With the final handoff staged so offline carriage checks saw the
real tracked set, `python scripts/check.py` passed **733 tests / 11 skipped,
RESULT PASS**; config validation, strict registry integrity and documentation
checks all passed (the pre-existing orphan-doc warning remains). The Docker
artifact/runtime dependency test could not run without a new private artifact
containing `motion.py`; its script syntax and new package/capability preflight
are covered, and the capability unit fixture refuses the missing file.

Adversarial review remediation: the earlier full-frame syntax default is no
longer eligibility. Both tracked and SIM configuration default motion disabled
and uncalibrated; firstboot publishes effective true only when both booleans are
explicitly true and every numeric, zone, mask and diagnostic value is valid.
Invalid configuration makes firstboot red and substitutes disabled safe values.
Incomplete application/unit/camera input now stops and disables the prior unit
and purges stale credential/socket state. The hermetic power harness injects a
failed and a 30-second-hung Door stop: neither permits the backlight write or
suspend, and the latter is cut off by the seven-second outer ceiling. The
artifact test now imports `cv2` and executes the actual artifact `motion.py`
inside its clean Ubuntu package-list closure. That Docker/artifact proof remains
**unrun** because no current artifact containing `motion.py` is available; no
offline runtime or physical detector claim is made from syntax/static checks.

Post-review verification on 2026-09-10: with Git Bash explicitly placed on the
Windows `PATH`, `python scripts/check.py` passed **747 tests / 11 skipped,
RESULT PASS**, including the fake-backed stop-failure and 30-second-hang cases;
configuration, strict trace integrity and documentation gates also passed.
`bash -n` accepted the edited firstboot, sleep and artifact scripts. A separate
attempt to rerun `vmtest/test-wall-builder.sh` in the Windows shell was stopped
after it repeatedly reported the same host-bootstrap limitations (`python3`,
`genisoimage` and `xorriso` unavailable); none of that partial run is counted as
new builder or artifact evidence. The prior WSL builder result above predates
this remediation, so the changed builder assertions still require a suitable
Ubuntu/WSL runner, and the actual OpenCV artifact execution remains unproven.

Second Door review remediation on 2026-09-10 closes two additional static and
fake-backed gaps. Masks are now capped at 32 normalized rectangles and 4096
ASCII bytes, with exact-boundary and over-limit tests. Incomplete-installation
cleanup no longer relies on an unbounded, ignored `disable --now`: it performs
a seven-second stop, a bounded dedicated-account kill fallback, and positive
process-inactivity verification before disabling and unlinking credentials or
the socket. A surviving process makes firstboot red and leaves the runtime
evidence intact. Recording-fake cases cover stop timeout with successful kill
and persistent activity after kill. This does not change the outstanding
release evidence: the clean offline artifact test that imports `cv2` and runs
the actual carried `motion.py` remains **unrun** until such a private artifact
exists, and no physical calibration/load claim is made. The focused Door suite
passed **28 tests**; shell parsing, configuration validation and strict trace
integrity passed; and the full G1 gate passed **756 tests / 12 skipped, RESULT
PASS**.

## 2026-09-10 — SR-023 second adversarial hardening

The audio service no longer inherits `/etc/wall-panel/wall.env`. Firstboot now
atomically materializes a root-owned `0600` audio-only environment containing
only the fixed local socket, and the unit reads that file. The unit also owns a
private state directory. Before future device I/O the broker fsyncs a mutation
intent; after a confirmed accepted result it atomically records the exact
response and incremented JavaScript-safe generation. An identical lost-reply
retry is deduplicated across restart. A timeout, crash, invalid result or write
failure after intent remains a persistent `mutation_uncertain` refusal until an
operator reconciles device state, while status and telemetry stay available.

Backend inventory and action calls now have finite deadlines and run in
killable bounded child processes. Only mutations share the serialization lock,
so a stuck device action cannot starve status/telemetry. Alias and result
validation rejects compact, colon, hyphen, underscore and Cisco-dotted
hardware-address forms plus BlueZ paths. Numeric protocol and
telemetry counters are bounded to JavaScript-safe integers, booleans are not
accepted as numbers, and a zero silence floor still classifies zero RMS as
silent.

IF-015 now includes a `telemetry` read with one positive derived-only schema:
either `{available:false}` or bounded generation/time/activity/RMS/peak/bands.
The shipped unavailable backend returns the former. No capture adapter,
PipeWire/WirePlumber package/session, radio route, latency, coexistence or
physical visualizer coverage is claimed.

Evidence: focused Windows audio tests **15 passed / 5 platform skips** across
the pytest and socket files (AF_UNIX socket cases require Linux); full `python
scripts/check.py` with Git Bash on `PATH` **750 passed / 12 skipped, RESULT
PASS**; config, strict trace integrity, flow validation, docs and edited shell
syntax passed. Linux socket, VM actual routing and physical telemetry evidence
remain required unrun.


The next adversarial pass found that cooperative cancellation did not reclaim a
slot from a backend that ignored it. Production operations now run in bounded
spawned child processes; timeout expiry terminates and reaps the child before
returning capacity. A regression drives six consecutive permanent hangs (past
the former four-slot ceiling) and proves a later healthy status succeeds with
no backend child left alive. Every exception after durable intent, including
`backend_unavailable`, now leaves the journal sticky uncertain across restart.
The broker overwrites backend telemetry generations with its own epoch, and
request/result/pair-confirmation validation now also rejects Cisco-dotted
addresses. Linux socket coverage asserts mode 0660 and uses an actual
`SO_PEERCRED` connection against a deliberately mismatched configured UID; that
case remains platform-skipped in the Windows run and must execute in the Linux
release gate. Focused evidence is **17 passed / 6 platform skips**; configuration,
Python parsing and strict trace integrity passed; and the fresh full G1 gate is
**758 passed / 13 skipped, RESULT PASS**. No physical routing or telemetry claim
is added.

The third Door teardown review closed two more fail-open edges. Numeric, zone
and mask validators now consume literal stdin bytes; backslash-escaped AWK
punctuation is rejected and the mask byte/count limits measure the value that is
actually written. Teardown runtime-masks the unit before bounded stop/kill/reset
handling, then requires both systemd inactivity and no dedicated-account process
for a full settle interval; a simulated delayed `Restart=on-failure` therefore
cannot race credential/socket unlink. The focused Door suite passes **32 tests**.
The full G1 gate passes **762 tests / 13 skipped, RESULT PASS**. The private
OpenCV artifact execution remains unrun pending `motion.py`.

The final audio epoch review found one cross-lane race: telemetry ran outside
the mutation lock and could be relabelled with a newer generation. The broker
now retains the request epoch, rejects a sample if mutation advances it while
capture is in flight, and labels accepted telemetry only with that retained
epoch. A coordinated fake proves generation-zero telemetry cannot escape as
generation one after a concurrent route mutation. Future real backends must
spawn no descendants unless isolation is extended to own and terminate their
process group.

The narrow root-owned audio environment now also materializes the validated,
nonsecret `WALL_AUDIO_ENABLED` value alongside the fixed socket path. This lets
installed-state verification determine intended state without reading the broad
credential-bearing `wall.env`; its firstboot test fixes the two-key allowlist
and root:root 0600 materialization. The burn-in procedure records direct checks
for that file, the panel-owned 0660 socket, peer-UID refusal, and default-denied
mutation authorization.
The focused audio gate passes **18 tests / 6 platform skips**; the full G1 gate
passes **763 tests / 13 skipped, RESULT PASS**, with strict trace integrity zero.
