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

### The five steps, and where each one stands

1. **Enable `health.googleapis.com`** on the Google Cloud project that owns the
   existing OAuth client — the one `oauth2-proxy` and `TRACKER_DRIVE_CLIENT_ID`
   share. (Reuse that client, do not mint a second: the tracker's Drive sync
   already learned that a second copy of the secret is a second thing to rotate.)
   **DONE by the Owner, 2026-09-09.**
2. **Add the scope above** to that client's consent screen, and add the Owner's
   account to the project's **Test users** list. Projects start capped at 100
   test users; going beyond that needs a third-party security review, which a
   household never will. **DONE by the Owner, 2026-09-09.**
3. **Consent in a browser** and mint a refresh token into `WEIGHT_TOKEN_FILE`.
   **BUILT, never yet run against Google:** `weight_oauth.py mint`, below.
4. **Make ONE real `dataPoints.list` call** and keep the body.
   **BUILT, never yet run against Google:** `weight_oauth.py capture`, below.
5. **Write `parse_weight_datapoint` against that captured body**, after pasting
   it into `weight_feeder.py`'s module docstring the way B7 pasted its three.
   **STILL OWED, and deliberately not started.** It is written by whoever holds
   a real response body, never from the schema: the discovery document says
   `weightGrams` is *grams*, and a parser that assumes kilograms posts a
   confident, plausible, wrong body weight that nothing on the wall could
   contradict.

Steps 3 and 4 are two subcommands of one Owner-run tool, `weight_oauth.py`, and
**neither of them parses a weight** — a test asserts that of this file too, not
only of the feeder. It is a separate file from the feeder on purpose: the
feeder's central guarantee is that it **never writes a credential** — an
allow-list, a systemd mount option and three tests stand behind that sentence —
and one of these commands writes exactly the credential the feeder may not. A
credential-writing door does not belong inside the module whose guarantee is
that it has none.

---

## Steps 3 and 4: the exact commands, and what each one prints

### First, ONE line to add in the Google console

Add this to the OAuth client's **Authorized redirect URIs**:

```
http://localhost:8117/
```

**ALONGSIDE the two already there** —
`https://<TRACKER_SUBDOMAIN>.<DOMAIN>/oauth2/callback` and
`https://<TRACKER_SUBDOMAIN>.<DOMAIN>/api/drive/callback`. Adding must not
replace: removing the first breaks Google sign-in for the whole household, and
removing the second breaks the tracker's Drive sync. The trailing slash is part
of the value — Google matches a web client's redirect URI byte for byte, and a
missing slash is the `redirect_uri_mismatch` below.

Google permits `http://` **only** for `localhost`/`127.0.0.1`, which is why this
one is not https. If the console refuses the value, try `http://127.0.0.1:8117/`
and pass it to `mint` as `--redirect-uri`; if it refuses both, **stop and report
it** rather than creating a Desktop-type client — that would be a second
credential to rotate, and that is the Owner's call.

### Why THIS flow, when the hub has no browser

The consent screen has to open on a machine with a browser — the Owner's PC —
while the process waiting for the code runs on the hub over SSH. Three flows
were possible and the choice is deliberate:

| flow | why not / why yes |
|---|---|
| `urn:ietf:wg:oauth:2.0:oob` (the old copy-the-code page) | **Not available.** Google shut it down in October 2022; new use answers `invalid_request`. |
| a loopback **listener** on the hub | Needs `ssh -L 8117:127.0.0.1:8117` up **before** consent, because the browser resolves `localhost` on the *PC*. Forget the tunnel and the code is spent and lost. It also puts a live authorization code on a hub socket any other local account may connect to. |
| a registered loopback redirect with **nothing listening** | **Chosen.** The browser lands on an error page, the code sits in the address bar, and it travels from the clipboard to the SSH session's stdin without touching a socket. No tunnel, no port, no race. |

The one cost is that **the browser shows a failure page**, which looks like the
flow broke. It has not: that page *is* the success case, and the step below says
so where you will be looking. PKCE (S256) is sent as well, so a code seen in an
address bar, a clipboard or a scrollback is useless to anyone without the
verifier, which never leaves the minting process.

### Step 3 — mint the refresh token

```bash
ssh hub
sudo /usr/bin/python3 /opt/homehub/stack/weight/weight_oauth.py mint
```

**Why `sudo`:** `stack/.env` is mode 0600 root:root and holds
`OAUTH2_PROXY_CLIENT_ID`/`_SECRET`. The tool reads **only those two keys** — it
never `source`s that file — and hands the finished token to the
`homehub-weight` account so the feeder can read it.

It should print a long `https://accounts.google.com/o/oauth2/v2/auth?...` URL,
then wait at `Paste the address-bar URL here:`.

1. Open that URL in a browser **on your PC** and sign in as the household
   account.
2. Grant the health-metrics scope. Remember what it includes: blood glucose,
   body fat, oxygen saturation, core body temperature and heart rate, because
   there is no weight-only scope.
3. The browser lands on **"This site can't be reached" /
   `ERR_CONNECTION_REFUSED` at `localhost:8117`. THAT IS THE SUCCESS CASE.**
4. Copy the **whole address bar** of that failed page, paste it into the SSH
   session, and press Enter.

On success it prints exactly three lines:

```
weight: refresh token written to /var/lib/homehub-weight/tokens/google-health-token.json (mode 0600, owner homehub-weight).
weight: the token itself was not printed, and the client secret was not copied into it.
weight: next, capture ONE response body - see stack/weight/README.md.
```

**Nothing secret is ever printed** — not the token, not the client secret, not
the authorization code — on success, in an error, or in a traceback (there are
no tracebacks: every failure prints one refusal line and exits 2).

When it prints something else:

| what it says | what it means, what to do |
|---|---|
| `could not read /opt/homehub/stack/.env (PermissionError)` | it was not run under `sudo`. |
| `... already exists and --force was not given` | a token is already there. Pass `--force` only if you mean to replace a working one. |
| `the token exchange failed: HTTP 400, redirect_uri_mismatch` | the console value and `--redirect-uri` differ — usually the trailing slash. |
| `the token exchange failed: HTTP 400, invalid_grant` | the code expired (they last minutes) or was already used. Run `mint` again. |
| `Google returned an error instead of a code: access_denied` | consent was declined, **or the account is not on the project's Test users list**. |
| `the token exchange failed: HTTP 401, invalid_client` | the id/secret pair in `.env` is not the client the redirect URI is registered on. |
| `Google's reply carried no refresh token` | Google treated this as an already-granted consent. Remove the app under `https://myaccount.google.com/permissions` and run `mint` again. |
| `the state in that URL is not the one this run generated` | a stale browser tab from an earlier attempt. Start `mint` again. |
| `REFUSED: ... outside the service's own state directory` | `WEIGHT_TOKEN_FILE` points somewhere that is not under `/var/lib/homehub-weight`. The guard refuses; fix the knob. |

### Step 4 — capture ONE response body

```bash
sudo /usr/bin/python3 /opt/homehub/stack/weight/weight_oauth.py capture --out /home/hub/weight-datapoints.json
```

`--out` must **not already exist** — it is created `O_EXCL`, mode 0600, and
handed back to the account that typed `sudo`. On success:

```
weight: HTTP 200 from https://health.googleapis.com/v4/users/me/dataTypes/weight/dataPoints
weight: 1234 bytes saved to /home/hub/weight-datapoints.json (mode 0600; the body is NOT printed - it is health data).
weight: sha256 <64 hex characters>
weight: hand that file back. The parser is written against it, not before it.
```

**The body is never printed**, on purpose: it is a real body weight with a
timestamp. The byte count and the sha256 are there so the file handed back can
be shown to be the file that was captured.

Exactly **one** call is made to the health API. (The token refresh that precedes
it is a separate call to Google's *token* endpoint, not to the health API.)

| what it says | what it means, what to do |
|---|---|
| `the list call failed: HTTP 403, PERMISSION_DENIED` | either `health.googleapis.com` is not enabled on the project that owns **this** client, or the scope is not on its consent screen. A scope added *after* consent is not in an already-minted token: fix the console, then `mint --force` again. |
| `the list call failed: HTTP 401` | the token is not valid for this API. `mint --force`. |
| `the list call failed: HTTP 404` | the route is wrong. It is `/v4/users/me/dataTypes/weight/dataPoints` — the prose docs' `/v4/users/me/dataPoints/weight` is the error this whole gate exists to catch. |
| `HTTP 200` but only a handful of bytes (`{}` or an empty `dataPoint` list) | the call worked and the account simply has no weight data in Google Health. That is still a real captured body and worth keeping, but **a parser cannot be written from an empty list** — weigh in on a scale that feeds Fitbit/Pixel and capture again. |
| `the list call was refused: refused an HTTP 302 redirect` | something answered for `health.googleapis.com` that is not Google. Do **not** retry with redirects allowed: urllib carries `Authorization` across a cross-host redirect, and that header is this whole scope. |
| `could not read the token file ... Run mint first.` | step 3 has not been done on this box, or `WEIGHT_TOKEN_FILE` disagrees with where it was written. |

### Then, and only then, step 5

Hand the captured file back. The parser is written against it, in its own task,
by whoever holds that body. Nothing in this repo may grow a
`parse_weight_datapoint` before that — two tests fail if it does.

### What has NEVER run against Google

Every test of these two tools runs against a **loopback HTTP server this repo
starts**. The consent screen, a real authorization code, a real token exchange,
a real refresh, a real `dataPoints.list` response and the real 401/403 bodies
are **unexercised**. What *is* exercised against real behaviour: the egress
guards (a real 302 and a real proxy variable, on real sockets), the write guard
(a real symlink on a real filesystem), the refusal to overwrite, and the
no-secret-printed property (sentinel values carried through the whole flow).
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

**The allow-list resolves symlinks, and it is bounded twice.** `os.path.abspath`
is string arithmetic — it does not resolve symlinks — so pre-creating
`<state>.tmp` as a link pointing at a token passed the allow-list unchanged (the
*string* matched) and the write truncated the token. The guard now uses
`os.path.realpath`, **and** requires the resolved path to sit inside the
service's own `StateDirectory=` — a bound the `.env` cannot move, because a knob
naming a token still names something outside `/var/lib/homehub-weight`. The open
itself does not trust the check that preceded it: the file is unlinked first
(which destroys a planted *link*, never the file it points at), then created
`O_CREAT|O_EXCL|O_NOFOLLOW`, so anything that appears in the gap is refused by
the kernel rather than by our confidence.

**A refused write costs the history, never the gauge.** The cycle has already
posted by then; the refusal is a named failure in the journal.


## The POST and the vendor GETs may not leave by a route nobody chose

The feed URL is validated to loopback or an address a local docker bridge is
really carrying (below), and for a while that was mistaken for the whole
property. It is not, because `urllib.request.urlopen` uses urllib's **default
opener**, which does two things nobody asked it to:

* **it follows redirects**, copying the request's headers onto the new request —
  `Authorization` included. So the validated loopback endpoint could answer
  `302 Location: http://attacker.example/feed` and urllib would obediently
  re-send the POST, carrying the feed bearer token, the `X-Forwarded-User`
  identity and a body whose one number is the Owner's body weight, to a host the *response* named;
* **it honours `http_proxy` / `https_proxy`**, so an exported proxy variable
  routes the same POST through a LAN proxy that then sees all of it — without
  the feed URL changing at all.

Both are closed, and the two directions are decided **separately** because they
are not the same problem:

| | feed POST (`feed_opener`) | vendor GET (`vendor_opener`) |
|---|---|---|
| redirects | refused | refused |
| proxies | none installed | none installed, and no knob to opt back in |
| peer address | re-checked on the socket before the request is written | not checked — these calls go to the internet by design |

The vendor half is the deliberate one. Those calls are *supposed* to leave the
box, so a peer check would be nonsense — but urllib does not strip
`Authorization` across a cross-host redirect, so a 302 from an impersonated
vendor endpoint would hand out the household's Google Health token — which grants blood glucose, body fat,
oxygen saturation, core temperature and heart-rate metrics as well as weight,
because there is no weight-only scope. `vendor_opener` exists before the reader that will use it, because
the egress defect happened by each call site reaching for the convenient
function, and the Google Health reader is the one call site still unwritten.
The price if a vendor starts redirecting is an "unavailable"
gauge — which is the outcome this feeder exists to produce when it cannot read
a source honestly. 
The peer check runs inside `connect()`, after the TCP handshake and **before**
`http.client` writes the request line, so a connection that lands somewhere it
should not is dropped with the token and the body still unsent.

**Nothing a remote sends is written to the journal.** `post_gauge` reports the
status code and never the response body: `main` prints failures to stderr and
systemd persists them, so an error body a responding server or an interposed
proxy chose to reflect — a token echoed back included — used to end up on disk.

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
| `weight_oauth.py` | the two Owner-run tools: `mint` (browser consent -> refresh token) and `capture` (ONE dataPoints.list call -> a body on disk). No parser, and no writing door the feeder shares |
| `homehub-weight.service` | oneshot unit, hardened, `ProtectHome=read-only` |
| `homehub-weight.timer` | 15 min, justified against the `static` horizon |
| `setup-weight.sh` | idempotent install; refuses rather than guessing |
| `../../tests/test_weight_feeder.py` | TC-006 |
| `../../tests/test_weight_oauth.py` | TC-006 - the no-leak and credential-guard properties of the two tools above |

Adds **no apt package** — python3 stdlib only, and `python3` is already in
`packages.list`. No apt export is owed.
