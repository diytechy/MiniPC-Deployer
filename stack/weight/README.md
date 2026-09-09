# The weight feeder (SR-022, SN-040, IF-014)

One body-weight reading, posted to NagLight as the `weight` gauge, against the
goal the person declared **in their own definitions**. A plain hub service on a
15-minute timer. Ships **OFF** (`WEIGHT_ENABLED=false`).

It **replaces** NagLight's `feeders/weight-manual.ps1` by posting the same gauge
id: NagLight upserts by id, so a different id would stand a second weight bar
beside the first and the two would disagree the moment one source went quiet.
The manual script remains the degraded path.

---

## Status: the vendor half is BLOCKED, and this file contains no parser

**`stack/weight/weight_feeder.py` has no Google Health parser in it, on
purpose.** B7 made "one real call, verified, *before* the parser is written" the
standard for this build, and body weight is the worst possible place to break
it. A usage parser that is one field off posts an obviously silly percentage. A
weight parser that reads kilograms as pounds posts a confident, plausible,
**wrong** body weight, and nothing on the wall could tell anyone.

So `read_google_health` raises a `SourceFailure` naming what is missing, that
flows into the ordinary unavailable path, and the panel says **unavailable** —
which is the truth. A test asserts that no `parse_google_health` /
`parse_weight_datapoint` symbol exists, exactly as B7 asserts there is no
`parse_gemini`.

### What was verified, by calling Google, on 2026-09-09

The build plan named "Google Health API v4". **It exists, under exactly that
name, and it is reachable.** It is *not* Google Fit (`fitness:v1`, closed to new
sign-ups) and *not* Health Connect (Android-device-local, no server REST path).
It is the successor to the **Fitbit Web API**, fed from Fitbit, Pixel Watch and
partner apps.

| Check | Call | Result |
|---|---|---|
| does the API exist under that name | `GET https://www.googleapis.com/discovery/v1/apis?preferred=false` | **200**, 531 APIs, including `health:v4` and `health:v4beta` with `discoveryRestUrl https://health.googleapis.com/$discovery/rest?version=v4` |
| is there a Weight type, and what shape | `GET https://health.googleapis.com/$discovery/rest?version=v4` | **200**, 292 545 bytes, `revision 20260907`. `DataPoint.weight` → `Weight {sampleTime: ObservationSampleTime (required), weightGrams: double (required), notes: string}` |
| does the read route resolve | `GET https://health.googleapis.com/v4/users/me/dataTypes/weight/dataPoints` (no auth) | **401 UNAUTHENTICATED**, `details[0].metadata = {service: health.googleapis.com, method: google.devicesandservices.health.v4.DataPointsService.ListDataPoints}` |
| …and with a junk bearer | same URL, `Authorization: Bearer not-a-real-token` | **401**, "Request had invalid authentication credentials" |

Routing happens **before** authentication, so a 401 that names the backend RPC
method is proof that the route resolves to a real service and that authentication
is the only gate left.

**The prose docs are wrong about the path.** `developers.google.com/health/
migration/api-specifications` renders the read as `/v4/users/me/dataPoints/
weight`. The discovery document — the machine-readable contract a client is
actually routed by — says `v4/users/{usersId}/dataTypes/{dataTypesId}/dataPoints`,
and its own `parent` parameter documentation gives `users/me/dataTypes/weight` as
a worked example. This is exactly the class of error the "verify first" gate
exists to catch.

### The scope is NOT what the plan assumed

The plan said Weight has "its own OAuth scope". **It does not.** The discovery
document lists 21 scopes and there is no weight-specific one. The only scope that
admits the Weight data type is

```
https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly
```

which also grants **blood glucose, body fat, oxygen saturation, core body
temperature and heart-rate metrics**. Consenting to it hands this box all of
them. That is a real widening of what the household gives up for a weight bar,
and it is the Owner's call, not this feeder's.

### What is still owed, and only the Owner can clear it

1. **Enable `health.googleapis.com`** on the Google Cloud project that owns the
   existing OAuth client — the one `oauth2-proxy` and `TRACKER_DRIVE_CLIENT_ID`
   share. (Reuse that client, do not mint a second: the tracker's Drive sync
   already learned that a second copy of the secret is a second thing to rotate.)
2. **Add the scope above** to that client's consent screen, and add the Owner's
   email to the project's **Test users** list. Projects start capped at 100 test
   users; going beyond that needs a third-party security review, which a
   household never will.
3. **Consent in a browser** and mint a refresh token into `WEIGHT_TOKEN_FILE`.

Then: make **one** `dataPoints.list` call by hand, paste the response body into
`weight_feeder.py`'s module docstring the way B7 pasted its three, and only then
write `parse_weight_datapoint` against it.

---

## The goal lives in the user's definitions (SN-040), and here is exactly how

`WEIGHT_DEFINITIONS_DIR` names the **directory**, never the number. In
multi-user mode that is `<tracker data root>/<the Google sub>/definitions` — the
same directory NagLight's `internal/defs.Load` reads and the Drive sync keeps in
step. The goal is a **top-level frontmatter key**:

```yaml
---
category: health
color_weight: 1.5
weight_goal_lb: 180
items:
  - id: weigh-in
    title: Step on the scale
    type: habit
    recur: daily
---
```

**No NagLight change is needed to store it there.** `internal/defs/yaml.go`
parses top-level frontmatter scalars and its `default:` branch is literally
`// ignore unknown top-level keys (forward-compatible)`. That file loads exactly
as it did before; the tracker does not need to understand the goal, because this
feeder is what turns it into the gauge's target line.

**There is deliberately no `WEIGHT_GOAL` knob.** A goal on the hub would need an
SSH session and a redeploy to change, would not travel with the rest of the
person's tracker, and would be a second home for the household's intent — which
is how this repo's `/opt/homehub` drift started. A test asserts the negative: a
cycle with every plausible goal knob set and an empty definitions tree refuses.

**No goal ⇒ nothing is posted at all**, and that is a *different* refusal from
"no source":

| | what the panel shows | why |
|---|---|---|
| no **source** | an unavailable gauge | the panel must say "we do not know what you weigh" rather than leave a hole where a bar belongs |
| no **goal** | nothing | the target line **is** the goal; NagLight refuses a gauge without a target, and the only way to satisfy it would be to invent one — drawing a 50 lb bar around a number nobody chose and colouring a real body weight green or red against it |

### KNOWN GAP — a real NagLight dependency, reported rather than worked around

Definitions reach the hub two ways:

* **Folder mode** (`drive.applyFolder`) stages the `.md` bytes **verbatim**. The
  goal rides along untouched. Works today.
* **Sheet mode** exports the sheet to CSV and **regenerates** the `.md` files
  through `internal/defsheet`, whose `columns` list is the *item* field set.
  An unrecognised column is collected into `unknown` and **dropped**.

**This household runs sheet mode** (`TRACKER_DRIVE_SHEET_ID` set,
`TRACKER_DRIVE_FOLDER_ID` deliberately blanked on 2026-09-08). So on the deployed
box the goal would be erased by the first sync after someone edits the sheet.

Making it survive needs a change in **NagLight**, a repo this block may not edit.
The smallest change that would do it: let `defsheet` carry non-item, file-level
keys through the round trip (a `settings`-shaped row, or preserving unknown
top-level frontmatter keys per category file), so `weight_goal_lb` survives
sheet → CSV → `.md`. Writing the goal to a hub knob "for now" was rejected: it
would make the acceptance criterion *false* while looking like it passed.

---

## The wire shape, and how it differs from the usage feeder's

Three differences, each a 400 or a silent lie if wrong:

* **`unit: lb`, and no `min`/`max`.** `lb` is the **one** unit NagLight infers a
  range for (`internal/gauge.unitRangeSpan`: a 50 lb bar, goal ±25). Omitting
  both ends asks the tracker for the agreed bar. Computing `goal±25` here would
  put a second range authority on the producer side, so a later change to the
  household's agreed width would apply to the manual feeder and not to this one.
  The usage feeder must send `min`/`max` for the opposite reason: `%` is not in
  that table.
* **No `window`, therefore no `direction`.** A goal is a standing line you are
  measured against on any day, not a pace across a period — and NagLight
  **refuses** a direction without a window. B5's manual feeder posts neither for
  the same reason.
* **`observed_at` is an RFC3339 string**, not the integer stamp the usage feeder
  sends. Getting this wrong does not fail loudly; it renders as a permanently
  stale gauge.

`value` and `target` are **always present**. This is the case the 2026-09
contract change was made for: an omitted value used to be stored as a fabricated
`0`, and *0 lb against a 180 lb goal* is the most alarming-looking lie the panel
could tell.

The feeder sends **numbers only** — never a colour, a band or a severity.
NagLight owns those, and a test greps a built body for `css`/`severity`/`colour`.

## "Stale, never green" is one invariant, not a scatter of checks

`build_post` says: **the body carries a stamp from this cycle if and only if this
cycle actually read the source.** Everything else falls out of NagLight's own
rule that `observed_at` is when the value was TRUE:

* source read → the real weight at the time it was **measured**;
* source failed, history exists → that weight, at its **original** stamp. It goes
  stale on the `static` 7-day horizon rather than sitting green on last month's
  number. A failed cycle does **not** write state, so the stamp cannot decay;
* source failed, no history → value `0` with **no `observed_at` at all**, stale on
  arrival. The gauge exists so the panel can say "unavailable"; the `0` is
  unreachable as a *displayed* reading by construction.

Note that **a real but old reading is still stale**, and must be. Someone who
last weighed themselves a fortnight ago has an honest reading that the panel must
render stale. Re-stamping it to `now` to "fix" an unavailable gauge is the single
easiest mistake to make here, and there is a test for it.

**The cadence follows from the horizon.** No window means the `static` horizon:
**seven days**. Left to that alone, a feeder that died on Monday would keep the
panel green on Monday's number until the following Monday. The timer is
therefore 15 minutes — 1/672nd of the horizon — and a test reads the shipped
timer file and fails if it is ever loosened past a quarter of it.

## "Never writes a credential" is an allow-list, not a deny-list

`open_for_write` is the only writing door in the module and admits exactly one
path plus its `.tmp` sibling. A list of known credential filenames cannot survive
the next tool putting its token somewhere new. Backed by three lines:

1. the allow-list itself;
2. the unit mounts the home `ProtectHome=read-only` — deliberately not `yes`, the
   feeder must still **read** the token — and binds the definitions directory
   `BindReadOnlyPaths=`;
3. a test runs a whole cycle inside a throwaway HOME holding a real-shaped
   refresh-token file and fails if **any byte under it changed**, if more than
   one file was written, or if the written file contains the token.

## The token's home — the `tracker_drive_tokens` shape, for a service

The plan asked for "tokens in a separate volume like `tracker_drive_tokens`".
That volume exists because the tracker is a **container** and its `/data` is
swept up hourly by the traceability mirror's `git add -A`, so a refresh token
under `/data` would be committed and pushed off the box.

This feeder is not a container, so the equivalent is a **separate directory with
the same property**: `WEIGHT_TOKEN_FILE` defaults to
`/var/lib/homehub-weight/tokens/`, which is *not* the state file's directory and
is not inside anything that commits, syncs or backs up by directory sweep. No
docker volume is declared, because nothing would mount it — declaring one would
be carriage for a container that does not exist.

## Files

| File | What it is |
|---|---|
| `weight_feeder.py` | the feeder: pure core above the SHELL banner, thin I/O below |
| `homehub-weight.service` | oneshot unit, hardened, `ProtectHome=read-only` |
| `homehub-weight.timer` | 15 min, justified against the `static` horizon |
| `setup-weight.sh` | idempotent install; refuses rather than guessing |
| `../../tests/test_weight_feeder.py` | TC-006 |

Adds **no apt package** — python3 stdlib only, and `python3` is already in
`packages.list`. No apt export is owed.
