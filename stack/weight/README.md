# The weight feeder (SR-022, SN-040, IF-014)

One body-weight reading, posted to NagLight as the `weight` gauge, against the
goal the person declared **in their own definitions**. A plain hub service on a
15-minute timer. Ships **OFF** (`WEIGHT_ENABLED=false`).

It **replaces** NagLight's `feeders/weight-manual.ps1` by posting the same gauge
id: NagLight upserts by id, so a different id would stand a second weight bar
beside the first and the two would disagree the moment one source went quiet.
The manual script remains the degraded path.

---

## Status: the gate is CLEARED — the parser is written against a real body

**`stack/weight/weight_feeder.py` now carries `parse_weight_datapoint`, and it
was written against a response body a human actually saw.** On **2026-09-09**
the Owner ran `weight_oauth.py capture` against the live API and got **HTTP 200,
712 bytes**, with one data point. B7's standard — "one real call, verified,
*before* the parser is written" — was met, not waived: from the block's start
until that capture this file said *no parser exists*, and the tests asserted the
absence by name. Those absence assertions have been **replaced by correctness
assertions**, not quietly dropped; `docs/status.md` carries the record.

The reason the gate mattered, restated because it is the reason the parser looks
the way it does: a usage parser that is one field off posts an obviously silly
percentage, while **a weight parser that reads grams as kilograms posts a
confident, plausible, wrong body weight, and nothing on the wall could tell
anyone**.

### What the captured body settled

**The body itself is not in this repo and never will be** — it carries the
Owner's Google user id and their real body weight. Its *shape* is recorded, in
`weight_feeder.py`'s module docstring and in the test fixtures, with a
placeholder id and a made-up weight.

| what was in doubt | what the body said |
|---|---|
| the top-level key | **`dataPoints`, plural.** Earlier prose in this repo (and the troubleshooting table below, now corrected) guessed `dataPoint`. The observation wins. |
| the unit of `weightGrams` | **grams.** The captured value, divided by 453.59237, is a weight the Owner recognises. |
| which stamp is `observed_at` | `weight.sampleTime.physicalTime` — the instant the reading was *true*, which is what NagLight's staleness rule keys off. Never "now". |
| whether `civilTime` matters | **Yes, and it is a real trap.** See below. |
| pagination | the response carried **no `nextPageToken`**. What the feeder does with one is an *assumption*, marked as one in `list_weight_data_points`. |

### The `civilTime` trap, in one line

The captured `physicalTime` is `2026-09-09T01:24Z`; the captured `civilTime` is
**2026-09-08 20:24 local** (`utcOffset: -18000s`). The weigh-in was a **Monday
evening**; in UTC it is **Tuesday**.

* The **gauge** uses `physicalTime`. That is correct: it is when the reading was
  true, and staleness is measured in elapsed time, not in calendar days.
* Anything that ever needs the reading's **calendar day** — the auto-check-off of
  the `weigh-in` habit that SN-040 gestures at next — **must** use `civilTime`
  (or `physicalTime` shifted by `utcOffset`), *never* `physicalTime`. Otherwise
  it ticks off Tuesday for a Monday-evening weigh-in, every time anyone in this
  timezone stands on a scale after 7pm.

The check-off **is** built now — see *[The automated check-off](#the-automated-check-off-the-second-post)*
below — and it uses `civil_date`, never `observed_at`. What that section also
records is the half of the day problem this repo **cannot** fix: the lane the
tick is posted on carries no back-dating at all.

### What it does when it cannot read a weight

Every failure is a `SourceFailure`, every `SourceFailure` becomes the ordinary
**unavailable** gauge, and none of them can produce a number.

| what happened | what the feeder does |
|---|---|
| 200, and the account has **never logged a weight** | `NoWeightYet` — a `SourceFailure` **subclass**, so the outcome is the unavailable gauge, and a distinct *type* and sentence so a human is not sent hunting a fault that is not there. **Not** a reading of 0. |
| the body carries **no `dataPoints` key at all** | refused, and *not* read as an empty history: it is a shape nobody has seen, so it is not interpreted. |
| a point with no `weight`, no `weightGrams`, no `sampleTime`, or a number that is not a body weight | that point is skipped; the reading comes from the others. Only if **nothing** on the page is usable does the page fail, and the message then carries every reason. |
| `physicalTime` missing, unparseable, zoneless, before 2025, or **in the future** | refused. A future stamp is what keeps a dead source rendering green, so it is refused rather than clamped to `now`. |
| several readings | the **latest by `physicalTime`** wins. Array order is not trusted. Two points sharing the newest instant with *different* weights are refused rather than ranked. |
| a `nextPageToken` | followed, up to 20 pages; a history still not exhausted after that is refused, because "the latest" would be a claim the code cannot support. **Assumption, not observation.** |
| a converted weight outside **40–1000 lb** | refused. The same band a *goal* is held to, for the same reason — a units error, not a judgement about anyone's body. |

Nothing on any of those paths puts the token, the response body, the Google user
id or the weight itself into a message: `run_cycle` prints a `SourceFailure`'s
message and systemd writes it to the journal, so the rule is that a message names
a **field** and a **type** and never a **value**.

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
   household's one existing OAuth client — **oauth2-proxy's**, in
   `OAUTH2_PROXY_CLIENT_ID` / `OAUTH2_PROXY_CLIENT_SECRET`. (Reuse that client,
   do not mint a second: a second copy of the secret is a second thing to
   rotate, and the two drift silently.) **DONE by the Owner, 2026-09-09.**

   > **Corrected 2026-09-09, against the live hub.** An earlier version of this
   > line said the client was "the one `oauth2-proxy` and `TRACKER_DRIVE_CLIENT_ID`
   > share". **There is no `TRACKER_DRIVE_CLIENT_ID`.** The deployed
   > `/opt/homehub/stack/.env` was listed by key name and holds
   > `OAUTH2_PROXY_CLIENT_ID`, `OAUTH2_PROXY_CLIENT_SECRET`,
   > `TRACKER_DRIVE_USER`, `TRACKER_DRIVE_SHEET_ID` and an empty
   > `TRACKER_DRIVE_FOLDER_ID` — and no `TRACKER_DRIVE_CLIENT_*` and no
   > `GOOGLE_CLIENT_*` of any kind. The tracker's Drive sync reaches straight
   > for the `OAUTH2_PROXY_*` pair rather than keeping its own copy. So the
   > household has **exactly one** Google OAuth client, which is the good
   > outcome — one secret to rotate — and it is oauth2-proxy's.
2. **Add the scope above** to that client's consent screen, and add the Owner's
   account to the project's **Test users** list. Projects start capped at 100
   test users; going beyond that needs a third-party security review, which a
   household never will. **DONE by the Owner, 2026-09-09.**
3. **Consent in a browser** and mint a refresh token into `WEIGHT_TOKEN_FILE`.
   **BUILT, never yet run against Google:** `weight_oauth.py mint`, below.
4. **Make ONE real `dataPoints.list` call** and keep the body.
   **BUILT, never yet run against Google:** `weight_oauth.py capture`, below.
5. **Write `parse_weight_datapoint` against that captured body.**
   **DONE, 2026-09-09**, against the HTTP 200 the Owner captured — its *shape*
   is in `weight_feeder.py`'s module docstring, with a placeholder id and a
   made-up weight, because the real body carries the Owner's Google user id and
   their real weight. It was written from the body and not from the schema, and
   the schema turned out to be right about `weightGrams` being *grams* and this
   repo's prose turned out to be wrong about the key being `dataPoint`.

Steps 3 and 4 are two subcommands of one Owner-run tool, `weight_oauth.py`, and
**neither of them parses a weight** — a test still asserts that of that file,
because a capture tool that interpreted a body would make the capture pointless. It is a separate file from the feeder on purpose: the
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
`OAUTH2_PROXY_CLIENT_ID`/`_SECRET`. The tool reads that file **key by key** —
it never `source`s it, because the same file carries
`TECHNITIUM_ADMIN_PASSWORD`, `CLOUDFLARE_API_TOKEN` and the finance
credentials — and hands the finished token to the `homehub-weight` account so
the feeder can read it.

**Which variables it looks for, in order.** `OAUTH2_PROXY_CLIENT_ID` +
`OAUTH2_PROXY_CLIENT_SECRET` first — this is the pair the hub actually has —
then `TRACKER_DRIVE_CLIENT_ID` + `TRACKER_DRIVE_CLIENT_SECRET` as a fallback
for a differently-provisioned box. **A pair counts only when both halves are
set**; half a pair is refused rather than mixed with the other pair's half,
because a mismatched id and secret fails at Google as `invalid_client` and
reads as Google's problem. If nothing is found, the refusal **names all four
variables and says which of them are set**, so the fix is one look at the
file.

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
   session, and press Enter. **The whole URL, not just the code** — the tool
   refuses a bare code (2026-09-09, see below), because the `state=` it must
   check against this run is in the part you would have left behind.

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
| `that paste is not a URL ... A bare code is no longer accepted` | paste the whole address bar. There is no state in a bare code, so there is nothing to check. |
| `that URL carries no state= parameter` | not the page this run opened. Start `mint` again. |
| `that URL carries the code parameter 2 times` | the paste carries two codes; which one Google issued is not guessable, so none is exchanged. Start `mint` again. |
| `NOTE - --token-file overrides WEIGHT_TOKEN_FILE` | not an error: you passed `--token-file` naming something other than the configured token. It is allowed and it is printed. The feeder reads `WEIGHT_TOKEN_FILE`, so a token written elsewhere is a copy the service will not use. |
| `REFUSED: ... outside the service's own state directory` | `WEIGHT_TOKEN_FILE` points somewhere that is not under `/var/lib/homehub-weight`. The guard refuses; fix the knob. |

### What the paste is checked against (changed 2026-09-09)

A cross-review found that `mint` **claimed** a PKCE + `state` check it did not
always make: a bare pasted code was returned before the parser ran, and a URL
carrying `code=` with no `state=` was accepted. Both are now refused.

* **`state` is mandatory.** PKCE binds the code to *this process's* verifier;
  `state` is the half that binds the *response* to the request this run made.
  There is no weaker fallback — there is "checked" and "not checked".
* **A bare code is refused.** It carries no `state`, and it is the paste a
  person is most likely to produce by selecting part of a string.
* **A duplicated `code=` or `state=` is refused, not ranked.** `?code=A&code=B`
  is a shape nobody can see the danger in, and first-wins means the tool can
  exchange a code the Owner is not looking at.
* **The check is before the exchange.** A state check that runs after the
  exchange is not a check: the code is single-use, so by the time it fails the
  thing it guarded has happened. A test asserts the fake token endpoint records
  *no request at all* on a state mismatch.

### `--token-file` is an override, and it says so (changed 2026-09-09)

The one-path allow-list is `WEIGHT_TOKEN_FILE`. `mint` used to hand the
*effective* path to the guard as both the path and the allow-list, so passing
`--token-file` compared it to itself and the allow-list was vacuous exactly when
it had something to decide. The flag stays — it and `--state-root` are
documented operator flags on a tool run under `sudo`, and a root operator can
write anywhere regardless — but it now **prints** that it is overriding, names
the configured path, and says which guards still bind the write: containment in
the service's own `StateDirectory=`, the refusal to be the feeder's state file,
and the no-symlink open below.

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
| `HTTP 200` but only a handful of bytes (`{}` or an empty `dataPoints` list) | the call worked and the account simply has no weight data in Google Health. The feeder handles this as `NoWeightYet` and the panel reads "unavailable"; weigh in on a scale that feeds Fitbit/Pixel. (The key is `dataPoints`, **plural** — an earlier version of this table said `dataPoint`, and the captured body corrected it.) |
| `the list call was refused: refused an HTTP 302 redirect` | something answered for `health.googleapis.com` that is not Google. Do **not** retry with redirects allowed: urllib carries `Authorization` across a cross-host redirect, and that header is this whole scope. |
| `could not read the token file ... Run mint first.` | step 3 has not been done on this box, or `WEIGHT_TOKEN_FILE` disagrees with where it was written. |

### Step 5, now done

The captured file was handed back on 2026-09-09 and `parse_weight_datapoint` was
written against it — see **Status** at the top of this file for what the body
settled and what it left as an assumption. The captured body itself was **not**
committed: what is in the repo is its shape, with a placeholder user id and a
weight that is not the Owner's.

`weight_oauth.py` still may not grow a parser; a test asserts that by name, and
also fails if that file so much as mentions `weightGrams` or `sampleTime`. It
captures a body; it does not interpret one.

### What has still NEVER run against Google

Every test in this repo runs against a **loopback HTTP server it starts**. What
HAS happened against Google, once, on 2026-09-09: a consent screen, an
authorization code, a token exchange, a refresh, and **one** `dataPoints.list`
call that returned 200 with one point. What remains **unexercised**: the real
401/403 bodies, a multi-point history, a `nextPageToken`, and the refresh grant
as the *feeder* performs it (as opposed to as `capture` performs it). Every one
of those fails towards the same place — a named `SourceFailure`, the unavailable
gauge, and the last real reading at its original stamp — so none of them can
invent a number. What *is* exercised against real behaviour: the egress
guards (a real 302 and a real proxy variable, on real sockets), the write guard
(a real symlink on a real filesystem), the refusal to overwrite, and the
no-secret-printed property (sentinel values carried through the whole flow).
## The goal lives in the user's definitions (SN-040), and here is exactly how

`WEIGHT_DEFINITIONS_DIR` names the **directory**, never the number. In
multi-user mode that is `<tracker data root>/<the Google sub>/definitions` - the
same directory NagLight's `internal/defs.Load` reads and the Drive sync keeps in
step.

**The goal is the `target` of ONE ITEM**, with its `unit` beside it:

```yaml
---
category: Health
color_weight: 1.5
items:
  - id: weigh-in
    title: Step on the scale
    type: habit
    recur: weekly
    horizon: long
    target: 170
    unit: lb
---
```

**Which item is configuration, not code.** `WEIGHT_ITEM_CATEGORY` (default
`Health`) and `WEIGHT_ITEM_ID` (default `weigh-in`) name the item's **location**.
Neither can hold a number, so the household's intent still lives only in the
person's own definitions; renaming or moving the item is an `.env` edit rather
than a code change. Both halves match case-insensitively and trimmed, because
both are typed by hand into a spreadsheet cell.

**No NagLight change is needed.** `target` and `unit` are already item columns
that round-trip through both sync modes today.

### The frontmatter reader is depth-aware (changed 2026-09-09)

A cross-review found four ways to make the reader source a **wrong goal**, all
one defect: it ignored indentation *depth*, so anything shaped like `id:` /
`target:` / `unit:` was read as a direct item field wherever it sat.

```yaml
items:                          items:
  - id: weigh-in                  - id: take-vitamins
    metadata:                       alternatives:
      target: 170                     - id: weigh-in
      unit: lb                          target: 170
                                        unit: lb
```

Neither declares a 170 lb goal to any YAML parser alive — the first target
belongs to `metadata`, the second to a nested list under a *different* item —
and both used to yield 170 lb on the wall. So did a second `items:` block, and
so did a tab-indented block that `yaml.v3` refuses to parse at all.

The rule now is one rule, not four patches: **a field belongs to an item only at
that item's own field column.** The sequence's indent is fixed by its first
`- ` entry, the field column by the first field on that entry; anything deeper
is a nested container's content and is not the item's. **Ambiguity is refused**
— a second `items:` key, an inline `items: [...]`, or a tab in the indentation
all raise rather than resolve, the same way two files declaring the goal already
did.

It is still a hand reader and not PyYAML, on purpose: the service is
**stdlib-only** (a plain unit under `ProtectSystem=strict`, no venv), so a real
parser means a new apt package name and the offline apt export re-run that goes
with it — and a full parser is the wrong *shape* anyway, because anchors,
aliases and merge keys let a goal arrive from a line the person cannot see
beside the number. Where PyYAML happens to be installed, the test suite uses it
as an **oracle** on every one of these fixtures, so "narrow" cannot quietly
become "different".

**Definitions symlinks.** The definitions directory is the tracker's own docker
volume and is inside the trust boundary, so `WEIGHT_DEFINITIONS_DIR` may itself
be a symlink and still be read. An individual `*.md` that resolves *outside*
that directory is refused rather than read: "the goal came from a file that is
not in the household's definitions" is a sentence this feeder should not be able
to say, and one `realpath` is what it costs.

### The service sees ONE definitions file, not the directory (changed 2026-09-09)

`WEIGHT_DEFINITIONS_DIR` in `.env` names the **host** path. The service does not
see that path at all. `setup-weight.sh` binds the **single category file the
goal lives in**, read-only, to
`/var/lib/homehub-weight/definitions/<that file's name>`, and points the
service's `WEIGHT_DEFINITIONS_DIR` at that directory instead.

**Scope is the reason, and it comes first.** The definitions directory is one
household member's subtree of a volume holding **every** member's tracker data.
A gauge about one person's body must not hand a service account read access to
all of it, so what is exposed is the one file that carries the goal and nothing
else. If the modes were wide open this would still be the shape.

**It is also the only shape that works**, and that was measured on the hub with
`systemd-run` as the real service account rather than reasoned about:

| what was bound | result |
|---|---|
| the definitions directory, at the same path | **NOT-READABLE** |
| the definitions directory, at a target the account owns | **NOT-READABLE** |
| the one category **file** (0644), into the account's `StateDirectory` | **READABLE**, and `touch` denied |

The directory is `drwx------ hub hub`, and its ancestors are root-only
(`/var/lib/docker` is `drwx--x---`, nothing for "other"), so no mount target
rescues a directory bind: the account cannot enter the directory, and cannot
traverse to its own path either. An ACL is not the escape hatch — `setfacl` is
not installed (a new apt package name, and the §5 apt export re-run with it),
and it would be **destroyed on the next sync** regardless: NagLight's
`internal/store/definitions.go` applies definitions by `os.MkdirTemp` +
populate + **rename into place**, so the `definitions` inode is replaced
wholesale.

**Which file is OBSERVED, never derived from the category.** `setup-weight.sh`
runs as root, can read the directory, and finds the `.md` whose **top-level**
`category:` matches `WEIGHT_ITEM_CATEGORY` — trimmed and case-folded, the same
rule `find_goal_item` applies, with the same `clean_scalar` handling of quotes
and a trailing `# comment`. It does **not** lowercase `Health` into `health.md`:
the filename is NagLight's slug rule, it lives in another repo, and it can
change without telling us. The test tree is booby-trapped both ways round — the
file carrying category `Health` is called `tracker-2b.md`, and there *is* a
`health.md` declaring something else — so a script that guessed the name binds
another member's tracker and reports success.

Only the frontmatter counts, and only column zero: a `category:` indented under
`items:` is a **field of an item**, and one in the prose body is a person
thinking out loud. Both are the lines `internal/defs` draws, and both are
asserted.

**Two files declaring the category is REFUSED, not resolved.** Picking one would
silently follow directory order, which is the same refusal the feeder already
makes when two files hold the item. So is one file declaring `category:` twice.

**A missing source must not wedge the unit**, so the bind carries systemd's `-`
prefix:

```
BindReadOnlyPaths=-/var/lib/docker/volumes/.../definitions/tracker-2b.md:/var/lib/homehub-weight/definitions/tracker-2b.md
```

Measured on systemd 255: **without** the `-`, a source that has been renamed
away fails the unit at `226/NAMESPACE` *before the feeder runs*, and the timer
repeats that every fifteen minutes with nothing in the journal about why.
**With** it, the mount is skipped, the feeder runs, and the person gets the
feeder's own named refusal naming the item and the category. Note honestly what
that path still is: `GoalMissing` exits **2**, so the unit is still recorded as
failed — but it is a *diagnosed* failure from the feeder's own mouth, on its
existing tested "no goal ⇒ nothing posted" path, not an undiagnosed namespace
error. If no file declares the category at provisioning time, no bind line is
written at all, `setup-weight.sh` says so loudly and exits **0**: a tracker that
has not synced yet is a normal state at firstboot and must not stop it.

**`EnvironmentFile=`, not `Environment=` — and that ordering was measured, not
assumed.** The obvious construction does not work. On systemd 255,
`EnvironmentFile=` assignments are applied **after every `Environment=`
assignment, regardless of order**: an `Environment=WEIGHT_DEFINITIONS_DIR=…` in
the drop-in *loses* to the unit's `EnvironmentFile=/opt/homehub/stack/.env`,
even though drop-ins are parsed later, and even with both lines in one file and
the `Environment=` second. All four orders were run. Two `EnvironmentFile=`
lines, though, **are** applied in parse order with the last one winning, and
drop-ins are parsed after the unit. So the override is a file —
`homehub-weight.service.d/20-definitions.env`, holding one line — and
`10-account.conf` points `EnvironmentFile=` at it. (systemd reads only `*.conf`
as drop-ins, so the `.env` sitting beside it is inert.)

**A stale inode is not a concern here, and the oneshot shape is why.** The
tracker replaces the whole `definitions` directory on every sync, so a
long-lived process would be holding a retired inode. This unit's namespace is
torn down and rebuilt on **every timer tick**, so each run resolves the bind
against whatever is there now.

**The mount target is root-owned.** `/var/lib/homehub-weight/definitions` is
`root:root 0755` inside the account's own `StateDirectory` — the account must
**read** what is mounted there and must never be able to drop a file of its own
beside it and have the feeder read that as the household's goal. systemd creates
a missing bind destination and leaves it behind as an empty file, so
`setup-weight.sh` sweeps stale `*.md` stubs out of that directory before it
writes the drop-in.

### It used to be a top-level `weight_goal_lb` key. That was changed on 2026-09-09.

The first version put the goal in a top-level frontmatter key and *deliberately
refused* an item-level goal. Two facts killed that design, and they are recorded
here so nobody restores it:

* **Sheet mode erases top-level keys, and this household runs sheet mode**
  (`TRACKER_DRIVE_SHEET_ID` set, `TRACKER_DRIVE_FOLDER_ID` deliberately blanked
  2026-09-08). Definitions reach the hub two ways: folder mode
  (`drive.applyFolder`) stages the `.md` bytes verbatim, but **sheet mode
  regenerates** each `.md` from CSV through `internal/defsheet`, whose `columns`
  list is the *item* field set - an unrecognised column is collected into
  `unknown` and **dropped**. So `weight_goal_lb` would be deleted by the first
  sync after anyone edited the sheet, silently, leaving the panel dark.
* **There is no vendor fallback.** Google Health v4 has no goal or target
  concept anywhere: `DataPoint` has 43 members and none is a goal, `Profile` and
  `Settings` carry none, and the only two occurrences of "goal" in the 292 KB
  discovery document (revision 20260908) are a UI settings enum.

`target` and `unit` already round-trip, so the goal now lives where the sync
will actually carry it.

**A file still carrying the old key is REFUSED, not ignored** - the message
names the key, says it is no longer read, and says where the number goes. And if
**both** the legacy key and an item `target` are present, the feeder **refuses
rather than ranking them**: silent precedence would leave a person looking at a
bar drawn around one number while a different number sat in their file looking
equally authoritative, and in sheet mode the top-level one is about to be deleted
underneath them, so "the newest edit wins" is not even stable. It is the same
rule this module already applies to two files declaring a goal.

**There is deliberately no `WEIGHT_GOAL` knob.** A goal on the hub would need an
SSH session and a redeploy to change, would not travel with the rest of the
person's tracker, and would be a second home for the household's intent - which
is how this repo's `/opt/homehub` drift started. A test asserts the negative
twice: a cycle with every plausible goal knob set (including the two *location*
knobs set to `170`) and an empty definitions tree refuses, and no goal-shaped
name may be **declared** in `.env.example` or `FieldSchema.psd1` at all.

### The unit is CHECKED, never assumed

If `unit` is missing, or is anything but `lb`, the feeder **refuses**. It does
**not** convert. This is the sharpest edge in the block: `target: 77` with
`unit: kg` is 170 lb, and **77 sits inside the 40..1000 lb sanity band**, so the
band cannot catch it - the panel would show "77 lb" against a real 191 lb
reading and paint it full red. A wrong unit here is the same failure class as a
vendor parser written from a schema: confident, plausible, and wrong about
someone's body, with nothing on the wall able to tell anyone.

**A `target` that is blank, absent, non-numeric, non-finite or outside
40..1000 lb is no goal**, and nothing is posted.

**No goal => nothing is posted at all**, and that is a *different* refusal from
"no source":

| | what the panel shows | why |
|---|---|---|
| no **source** | an unavailable gauge | the panel must say "we do not know what you weigh" rather than leave a hole where a bar belongs |
| no **goal** | nothing | the target line **is** the goal; NagLight refuses a gauge without a target, and the only way to satisfy it would be to invent one - drawing a 50 lb bar around a number nobody chose and colouring a real body weight green or red against it |

### Owed to HomeHub

`WEIGHT_ITEM_CATEGORY` / `WEIGHT_ITEM_ID` are **not** yet in
`scripts/deploy/FieldSchema.psd1`, which lives in the HomeHub repo. That is safe
rather than broken - an undeclared knob is simply absent from the emitted `.env`,
a blank knob takes the default, and the default is the shape the Owner's sheet
already syncs - but until it is added, *moving* the item needs an `.env` edit on
the hub rather than a deploy-config change.

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

## The automated check-off: the second post

The Owner changed the tracker item from `type: habit` to **`type: automated`**
with **`check: weight`**, which is what makes this possible at all. Each cycle
the feeder now posts **two** bodies to the same `/api/feed`, in this order:

```
1.  {"kind": "gauge", "id": "weight", "unit": "lb", "value": …, "target": …, "observed_at": …}
2.  {"check": "weight", "ok": true}
```

The second is on NagLight's **legacy lane**, which is a different lane from the
gauge — `kind` selects the gauge, and `case "":` (no `kind` at all) routes a
body to the boolean/colour/rgb struct. The handler counts "signals" among
`ok != nil`, `color != ""`, `rgb != ""` and refuses anything but **exactly
one**, so `ok` travels alone. It locates the item by
`it.Type == model.TypeAutomated && it.Check == body.Check`, first match.

### When the tick is sent, and when it is deliberately not

`should_check_off` has four gates, and **all four** must pass:

1. **A genuinely fresh read this cycle.** Not "the cycle worked" — a source
   that fails makes the feeder re-post the last real weight *at its original
   stamp*, and that path must never tick. A tick is a claim that a person stood
   on a scale, and a source that is down is precisely the circumstance in which
   this feeder knows nothing about whether they did.
2. **A sample time strictly newer than the one already ticked.** The scale
   syncs one weigh-in a week; the timer fires every 15 minutes. Without this,
   the item would be re-ticked 96 times a day.
3. **A day we can name** — the reading carries both a `civilTime.date` (or a
   `utcOffset` to derive one) *and* an offset. "We cannot say what day this was"
   is an answer, and the answer is silence.
4. **That day is today, in the reading's own local frame.** `now + utcOffset`,
   not UTC, and not the hub's `$TZ` — the offset comes from the reading, so this
   needs no timezone database and no guess about where the household lives.

Two more things it never does: it never posts `ok: false` (nothing here can
know somebody did *not* weigh in, and `ok: false` calls `engine.Uncheck`, which
would silently erase a tick the Owner made by hand), and it never posts a
`note`. The gauge already carries the number; a note would be the obvious way a
body weight leaks into the tracker's **log lines**, which the traceability
mirror pushes to a private repo hourly — and it would be dead weight anyway,
because `handleAPIFeed` decodes `Note` and then never reads it.

### Which day the tick lands on — and what this repo cannot fix

**The legacy `ok` lane cannot back-date.** This was read off
`internal/web/handlers.go`, not assumed:

* `body.At` is parsed (`time.RFC3339`, into a `logfile.Report`) **only** inside
  `if body.Color != "" || body.RGB != ""`. The boolean path never touches it and
  falls through to `date := s.Now()`.
* `s.Now()` defaults to `todayString` = `time.Now().Format("2006-01-02")` — the
  **tracker container's local date**.
* `stack/docker-compose.yml`'s `tracker:` service sets **no `TZ:`**, while every
  other service that cares sets `TZ: ${TIMEZONE}`. So that date is **UTC today**.

So for the shape the capture proved — a weigh-in at **20:24 local on Monday**,
which is **01:24 UTC on Tuesday** — the tick lands on **Tuesday's** log. Gate 4
above stops the *large* errors (a three-day-old reading recovered after an
outage is not ticked onto today at all), but it cannot stop this ≤1-day one:
sending an `at` would be silently dropped, which is worse than not sending it,
because it would look as though back-dating worked.

**The fix is not this feeder's to make.** Adding `TZ: ${TIMEZONE}` to the
`tracker:` service would make `s.Now()` the household's local date and put
evening weigh-ins on the right day — NagLight's own Dockerfile installs `tzdata`
for exactly this ("correct local *today* for the nightly materialize"), so the
missing `TZ:` looks like a pre-existing gap rather than a decision. But it moves
the day boundary for **every** item in the tracker, not just this one, so it is
a coordinator call and is written up in `docs/status.md` rather than slipped in
here. A test asserts the `tracker:` block still has no `TZ:`, so whoever adds
one is sent back to this section.

### The two posts fail independently, and the gauge goes first

Each post has its own `try`/`except` and its own failure line. A 400 on the tick
cannot stop the wall being told what the person weighs; a 500 on the gauge
cannot swallow the tick. **Gauge first**, because the gauge is what SN-040
promises and the tick is the extra — putting the extra first would put its
latency and its failure modes in front of the thing that must always happen.
`check_id_from_definitions` is not even consulted until the gauge has been
posted, and not at all on a cycle that could not read the source.

The sample time is recorded as ticked **only after a 200**. A tick that failed
to post is retried next cycle rather than remembered as done.

### There is no `WEIGHT_CHECK_ENABLED`, on purpose

`type:` and `check:` **are** the declaration. They belong to the person, they
round-trip through Drive sheet mode as item columns, and they are what NagLight
itself reads to decide an item is feeder-driven. A hub knob beside them would be
a second place the same intent lives, and the two would disagree the first time
the Owner changed their mind from a phone: a knob saying *yes* over a
`type: habit` item earns a 400 per weigh-in, and a knob saying *no* over
`type: automated` leaves an item nothing can ever tick and no hint why.

So **nothing** is added to `stack/.env.example`, and **nothing** is owed to
HomeHub's `FieldSchema.psd1`. Reverting the item to `type: habit` from a phone
turns the tick off by itself on the next sync. Note that a revert changes the
*type* column and leaves `check: weight` sitting beside it — sheet mode
round-trips both — which is why `type:` is read as well as `check:`, and why the
test for it uses that exact shape. (A first mutation round missed this: the
plain `HEALTH_MD` fixture has no `check:` at all, so it was being refused for
the wrong reason and a mutant that deleted the `type:` gate survived.)

### The mark lives in the existing state file

`{"checked": {"weight": <epoch sample time>}}` sits alongside the gauge's own
entry in `weight-state.json`, written by the same `save_state` through the same
`open_for_write` guard — one allow-listed path plus its `.tmp`, `realpath`
-resolved, contained in the `StateDirectory`, opened `O_NOFOLLOW`. **A second
state file would be a second blessed path**, and the allow-list is the guard.

`load_state` validates the marks the way it validates readings — the state file
is *input*, not memory. A mark in the future is dropped (it would suppress every
real tick until the clock caught up). A dropped mark is safe only because gate 4
is independent of it: with no mark at all, the worst that can happen is a
re-tick of a reading from *today*, onto *today*, on an item already done.

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
   feeder must still **read** the token — and binds ONE definitions file
   `BindReadOnlyPaths=` read-only, never the directory (see "The service sees
   ONE definitions file");
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

**Every component, not just the last one (changed 2026-09-09).** `O_NOFOLLOW`
protects the *final* component only, so replacing an intermediate directory
after the verdict resolved — swapping `.../tokens` for a link — sent the write
outside the state directory with every guard above it already passed. The open
now walks from the state root **one component at a time**: each intermediate
directory is opened relative to the previous one with `O_DIRECTORY|O_NOFOLLOW`
(`dir_fd=`, stdlib, Linux), so a component that has become a link is refused by
the kernel *at the hop*, and the final create happens against a directory handle
rather than a name that can be re-pointed underneath it. Windows has no `dir_fd`
opens, so the dev PC checks each component with `lstat` instead; that fallback
is check-then-use and does not close the race, and is not claimed to — the hub
is Linux.

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
| `setup-weight.sh` | idempotent install; resolves the ONE category file by frontmatter and generates the drop-in; refuses rather than guessing |
| `../../tests/test_weight_feeder.py` | TC-006 |
| `../../tests/test_weight_oauth.py` | TC-006 - the no-leak and credential-guard properties of the two tools above |

Adds **no apt package** — python3 stdlib only, and `python3` is already in
`packages.list`. No apt export is owed.
