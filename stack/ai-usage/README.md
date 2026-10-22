# The AI-usage feeder (SR-021, SN-016, IF-013)

Reads how much of each AI subscription has been consumed and posts it to
NagLight as gauges. A plain hub service on a systemd timer — **no container**,
**off by default**, and it **never writes a vendor credential file**.

## The first three sources, and the one real call each was built from

The build plan made this a gate rather than a preference: **a parser written
from documentation posts fiction, and fiction looks exactly like a healthy
subscription.** Each parser below was written *after* the call beside it, from
what actually came back on 2026-09-09.

### Codex — `codex app-server`, JSON-RPC

    codex app-server --stdio
    -> {"jsonrpc":"2.0","id":1,"method":"initialize",
        "params":{"clientInfo":{"name":"homehub-ai-usage","version":"1"}}}
    -> {"jsonrpc":"2.0","method":"initialized","params":null}
    -> {"jsonrpc":"2.0","id":2,"method":"account/rateLimits/read"}

Observed reply (trimmed):

    {"id":2,"result":{"rateLimits":{"limitId":"codex",
      "primary":{"usedPercent":34,"windowDurationMins":10080,"resetsAt":1789435411},
      "secondary":null,"planType":"prolite","rateLimitReachedType":null},
      "rateLimitsByLimitId":{"codex":{...},"codex_bengalfox":{...},
                             "base_model_inference":{...}}}}

`usedPercent` is an **integer percent**; `windowDurationMins` 10080 is seven
days, so the codex gauge is a **weekly** one.

**This source handles no credential at all.** The child process reads its own
`~/.codex/auth.json`; the feeder writes two JSON-RPC frames to its stdin and
reads one result off its stdout. That is why there is no `AI_USAGE_CODEX_*`
credential knob.

**Only `rateLimits` is read, not `rateLimitsByLimitId`.** The per-limit-id
buckets are a vendor implementation detail — the observed set included
`codex_bengalfox` and `base_model_inference` — and gauges keyed off them would
appear on and vanish from the panel as the vendor renames a model.

### Claude — the OAuth usage endpoint

    GET https://api.anthropic.com/api/oauth/usage
    Authorization: Bearer <the CLI's OAuth access token, read-only>
    anthropic-beta: oauth-2025-04-20
    User-Agent: claude-code/<version>

Observed **200** (trimmed):

    {"five_hour":{"utilization":79.0,"resets_at":"2026-09-09T09:10:00.468815+00:00"},
     "seven_day":{"utilization":87.0,"resets_at":"2026-09-12T09:00:00.468835+00:00"},
     "limits":[{"kind":"session","percent":79,"severity":"warning",...},
               {"kind":"weekly_all","percent":87,"severity":"warning",...}],
     "spend":{...}}

`utilization` is a **float percent**. The payload also carries `severity`
strings, and **the feeder discards them**: NagLight owns severity and colour,
and forwarding a vendor's opinion of "warning" would make the panel's authority
ambiguous. The window lengths are read off the **field names**, because the body
carries no duration field.

### OpenCode Go — the Zen usage endpoint

    GET https://opencode.ai/zen/go/v1/usage
    Authorization: Bearer <workspace key, read-only>

Observed **200**:

    {"usage":{"rolling":{"status":"ok","percent":0,"resetsAt":"2026-09-09T13:15:25.458Z"},
              "weekly":{"status":"ok","percent":58,"resetsAt":"2026-09-14T00:00:00.458Z"},
              "monthly":{"status":"ok","percent":89,"resetsAt":"2026-09-19T00:11:19.458Z"}}}

**`rolling` is deliberately not posted.** It names no window length and the
endpoint supplies none. The feed contract refuses `direction` without a
`window`, and a windowless gauge takes the 7-day staleness horizon — so the
choice was between inventing a window length and shipping a gauge that stays
green for a week after the feeder dies. Both are the fabricated reading the
contract was tightened to stop, so the bucket is skipped and this paragraph is
the record of why. A bucket whose `status` is not `"ok"` is skipped for the same
reason: a number the vendor warned us about must not become a green gauge.

### The header that turned out to be load-bearing

`read_opencode` sends a named `User-Agent`. That is not decoration: the first
cut sent none, urllib's default `Python-urllib/3.x` went out, and the endpoint
answered **403** where the verification call — which sent a named agent — had
answered 200. It was found by running one real cycle end-to-end after the unit
tests were green, and it is the argument for doing that: the feeder handled the
403 *correctly* (two "unavailable" gauges and a named failure line, never a
green one), so nothing about the behaviour looked wrong. Claude's
`claude-code/<version>` agent and its `anthropic-beta` header are asserted by
the same test.

## Two more sources (2026-10-11, plan R2) — both opt-in

`AI_USAGE_SOURCES` defaults to `codex,claude,opencode`. `claude2` and `gemini`
need their own sign-ins, so neither switches itself on. The full set is
`claude,claude2,codex,gemini,opencode`; `docs/config-reference.md` has the knobs
and the exact edit-and-reload steps.

### Claude account 2 — `claude2`

The same endpoint and parser as account 1 (`parse_claude` takes the gauge table
as a parameter), with its own credential file (`AI_USAGE_CLAUDE2_CREDENTIALS`)
and its own ids: `ai-usage-claude2-session`, `-weekly` and `-weekly-fable`. The
panel groups rings by the provider id inside the gauge id, which is why the ids
are `claude2` and not `claude-2`: the panel's regex is
`/^ai-usage-([a-z0-9]+)(?:-|$)/`, and `claude-2-…` would parse as provider
`claude` and merge into account 1's ring.

**The refresher is a template** (`homehub-claude-refresh@.service` and
`@.timer`), one instance per account, named after the reader: `@claude` and
`@claude2`. What differs per instance is a drop-in `setup-ai-usage.sh` writes
from the account's real home:

| Instance | `ReadWritePaths=` | Extra |
|---|---|---|
| `@claude` | `~/.claude` and `~/.claude.json` — the paths the un-templated unit always had | none (the CLI's default config) |
| `@claude2` | `~/.claude-2` | `Environment=CLAUDE_CONFIG_DIR=~/.claude-2` |

**Does `.claude.json` move under `CLAUDE_CONFIG_DIR`? On Windows, yes —
measured; on the hub's Linux build, unverified.** On 2026-10-11, with Claude
Code 2.1.296 on the Windows dev PC, running
`claude auth status` with `CLAUDE_CONFIG_DIR` pointed at an empty directory
created `.claude.json` (holding `machineID`, `firstStartTime` and so on) **inside
that directory**, and left the real `~/.claude.json` byte-for-byte untouched
(mtime unchanged). So account 2's entire state is one directory, and its unit is
granted that directory and not a byte of account 1's. That was measured on
Windows only; the hub's CLI is a different build on Linux, so the first live
sign-in should confirm `ls -a ~/.claude-2` shows `.claude.json` and that
`~/.claude.json`'s mtime did not move.

**Migration of a box that has the old units.** `setup-ai-usage.sh` installs the
template and the `@claude` drop-in first, then — only if
`homehub-claude-refresh.{timer,service}` or its `.service.d` exist — stops and
disables the old timer, deletes the old files and drop-in, and only then runs
`daemon-reload` and enables `homehub-claude-refresh@claude.timer`. The two
timers are never both enabled (two processes refreshing one rotating credential
is the thing that must not happen). Account 1's user, command and writable paths
are unchanged; only the unit name differs. On a box that never had the old pair
every migration line is a no-op. The new timer's `OnBootSec=3min` is already
elapsed on a long-running hub, so the first renewal runs as soon as the timer is
enabled — harmless, `/usage` costs nothing.

### Gemini — `gemini`

    POST https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist
         {"metadata":{"ideType":"IDE_UNSPECIFIED","platform":"PLATFORM_UNSPECIFIED","pluginType":"GEMINI"}}
      -> {"currentTier":{...},"cloudaicompanionProject":"<project id>",...}
    POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota
         {"project":"<project id>"}
      -> {"buckets":[{"modelId","remainingFraction","resetTime","remainingAmount","tokenType"},...]}

Authorization is `Bearer <access_token>` read from the CLI's
`~/.gemini/oauth_creds.json`. Everything above is **read from the Gemini CLI
source** (HEAD `9b6e026`, 2026-10-09: `code_assist/server.ts`, `types.ts`,
`setup.ts`, `oauth2.ts`, `config/config.ts` `refreshUserQuota`), **not from an
observed response** — no live call was possible when it was written. That is
the opposite of this README's rule for the other three sources, so the parser is
stricter than they are (below) and the list of what a first live run must
confirm is at the end of this section.

The plan's four open questions, answered from that source:

1. **Where the project id comes from.** `loadCodeAssist` (`setup.ts`
   `_doSetupUser`): an onboarded account's answer carries `currentTier` and
   `cloudaicompanionProject`, and that is the id the CLI passes to
   `retrieveUserQuota`. If the answer names none, the CLI falls back to the
   operator's `GOOGLE_CLOUD_PROJECT`; the feeder's equivalent is
   `AI_USAGE_GEMINI_PROJECT`. An answer with **no `currentTier`** is an account
   the CLI would *onboard* (`onboardUser`, a mutating call); the feeder never
   makes one and reports "not onboarded" instead.
2. **Which bucket.** Buckets are per model. The gauge is the **lowest
   `remainingFraction`** (the most-consumed one) among the models named in
   `AI_USAGE_GEMINI_MODELS` — every bucket when it is blank — and **the label
   says which** (`Gemini daily (all models)`, or the list). A named model with no
   bucket refuses the whole reading, because the answer is a minimum and a quietly
   missing bucket could be the lowest. The window is the 24 hours ending at that
   bucket's own `resetTime`, kind `daily`; `check_window` refuses a bucket whose
   reset is more than a day out, so a weekly bucket cannot be mis-sized as daily.
3. **A refresh that costs no quota.** There is **no free CLI command**, unlike
   Claude's `/usage`: in headless mode a slash command whose action returns
   nothing falls through and **the literal text `/stats` is sent to the model**
   (`nonInteractiveCli.ts`), one billed request per run against the very quota
   being reported. What does renew the token without a model call is the Agent
   Client Protocol: `gemini --acp`. With `selectedType` = `oauth-personal` and
   the browser suppressed (`NO_BROWSER=true`), the CLI loads the cached
   credential and **renews an expired access token at startup, before the
   protocol loop begins** (`gemini.tsx`: "Do oauth before app renders" →
   `getOauthClient`, whose google-auth-library `tokens` hook rewrites
   `oauth_creds.json`); `authenticate` (`acp/acpRpcDispatcher.ts`) runs
   `config.refreshAuth` once more. `gemini_auth_refresh.py` starts it, sends
   `initialize` (the reply proves startup finished) then `authenticate`, and
   stops, under `homehub-gemini-refresh.{service,timer}` (every 4 minutes,
   because google-auth-library only renews inside a 5-minute window before
   expiry: `DEFAULT_EAGER_REFRESH_THRESHOLD_MILLIS = 5 * 60 * 1000`,
   `isTokenExpiring()` is `expiry_date <= now + threshold`, and the CLI does not
   override it — read from google-auth-library 10.9.0 as bundled in
   `@google/gemini-cli` 0.63.0; a 30-minute interval would renew nothing at +30
   and land after expiry at about +62). It refuses to run unless
   `security.auth.selectedType` is `oauth-personal` (because `authenticate`
   *clears* the cached credentials to switch method) and unless a cached
   credential exists, and prints one status line and nothing the CLI said.
   **Observed against the real CLI** (`@google/gemini-cli` 0.63.0 from npm, dev
   PC, an isolated `GEMINI_CLI_HOME` holding a *fake* credential): `--acp` is a
   real flag, and a credential the CLI cannot renew does **not** produce an ACP
   session at all — the CLI prints "Please visit the following URL to authorize
   the application" on stdout and waits on stdin for a code, before the protocol
   loop. The keeper recognises that prompt and exits **4** within seconds
   (`gemini-refresh: DEAD LOGIN`), which is the Gemini dead-login journal
   signature. **The happy path — a real, renewable credential — was not run**
   (it needs the Owner's account): see below.
4. **The feeder never mints or refreshes a Google token (SR-021).** It reads
   `access_token` and `expiry_date` and spends the token on the two calls above.
   It never reads `refresh_token`, never writes the file, and an access token
   past its own `expiry_date` is reported (`gemini: the CLI's access token has
   expired and has not been renewed`) rather than renewed. Using the CLI's
   OAuth client from a second program is a grey area; the mitigation is that the
   *CLI* is the only thing that ever uses it, from the account the CLI already
   uses, and the feeder only borrows the resulting bearer token for the same
   two calls the CLI makes.

**Not proven (the first live run must confirm each):**

* the two response shapes above against a real answer — particularly that
  `remainingFraction` is *remaining* (it is named so in `types.ts`, and the CLI
  multiplies it into "remaining"), that `resetTime` is RFC 3339, and that every
  bucket for the models in use is daily;
* what `tokenType` distinguishes, and whether buckets of two token types for one
  model should be minimised together (they are today);
* that `gemini --acp` renews the token on a **real** credential **without a model
  call** and without opening a browser, on the hub's CLI build (the dead-login
  half is observed, above; this is the live half). The proof is one
  command: note `expiry_date` (the number only), run
  `sudo systemctl start homehub-gemini-refresh.service`, and expect the journal
  line `… the token's expiry moved forward`, and no new request in the quota.
  If it does not hold the ring reads unavailable (fail closed), and the fallback
  is signing in by hand every hour — i.e. Gemini is then not deployable
  unattended, and the plan should be told;
* that `ProtectHome=read-only` plus `ReadWritePaths=~/.gemini` is enough for the
  CLI to run (it may want another path; a 226/NAMESPACE or a read-only error in
  the journal would say which);
* account risk from using an unofficial endpoint.

## What it posts (IF-012, amended by v1.1 on 2026-09-12)

Up to ten gauges, upserted by id: `ai-usage-codex`, `ai-usage-claude-session`,
`ai-usage-claude-weekly`, `ai-usage-claude-weekly-fable`,
`ai-usage-opencode-weekly`, `ai-usage-opencode-monthly`, and — only for the
opt-in sources — `ai-usage-claude2-session`, `ai-usage-claude2-weekly`,
`ai-usage-claude2-weekly-fable` and `ai-usage-gemini-daily`. Each body is
`kind:"gauge"` with `unit:"%"`, `min:0`, `max:100`, `target:0`,
`direction:"down"` and a `window`.

### The wire carries REMAINING, and the pairing is load-bearing

The feeder used to post `target: 0` **with** `direction: "up"`. NagLight runs an
"up" pace from `min` to `target` — which is 0 to 0 — so the served `pace` was
**zero at every point of every window, for every gauge this feeder ever
posted**. The dashed line sat on the floor of each bar and the tracker's
deviation collapsed to `value / 50`, making colour a pure function of percent
consumed: full red at 50 % used wherever the window was. OpenCode weekly showed
red at 78 % used on day six of seven — a healthy burn — exactly as it would at
51 % on day one. Measured on the wire 2026-09-12.

Counting **down** from a full bar fixes it with no contract change: `pace`
becomes `100 * (1 - elapsed)`, the plan you ought to have left right now, and
the bar drains as the window burns.

Every reader still parses, and the state file still stores, the **consumed**
percent the vendor actually stated. The inversion happens in exactly one place —
the body assembly in `build_gauge` — so a fresh reading and a re-posted stored
one are flipped identically and no stored state needed migrating.

### Fable is a scoped limit inside `limits[]`, not a top-level key

There is no `seven_day_fable`. `seven_day_opus` and `seven_day_sonnet` are
`null`, and `seven_day_breakdown` is a split by **surface** (Claude Code /
Chats / Cowork / Other) whose rows are shares of usage summing to 100 — not
shares of a quota. The only per-model signal is a `limits[]` entry:

    {"kind":"weekly_scoped","group":"weekly","percent":0,"resets_at":"...",
     "scope":{"model":{"id":null,"display_name":"Fable"}}}

`scope.model.id` is `null`, so the **display name is the only handle there is**.
The body also carries `nimbus_quill`, `cinder_cove`, `copper_kite`,
`harbor_lantern`, `amber_ladder`, `juniper_tide`, `tangelo`, `iguana_necktie`,
`omelette_promotional`, `seven_day_cowork` and `seven_day_omelette` —
unreleased-model placeholders, mostly null, **whose names are not contractual**.
Keying on one would break silently the day it ships or is renamed.

That gauge is **optional** (`GaugeSpec.optional`): whether a per-model limit
exists at all depends on the plan, and a gauge reading "unavailable" every
cycle forever is worse than no gauge, because it teaches people to ignore the
word. It is posted once there is something to say — a reading this cycle, or a
stored one — and keeps being posted after that, so a limit that **disappears**
goes stale in plain sight instead of silently leaving the wall. The skip asks
which keys the state file *mentions* (`state_keys`), not which ones parse: a
corrupt entry must not look like a gauge that was never seen.

Its id sorts immediately **after** `ai-usage-claude-weekly`, and that is
load-bearing rather than tidy — NagLight serves gauges in id order and the panel
draws the first gauge of a window as the headline and any later one sharing that
window as the narrow sub-column beside it.

Three rules are enforced in `build_gauge` rather than trusted, because the
2026-09 tightening turned each of them into a 400:

* **`value` and `target` are always present.** An omitted `value` used to be
  stored as a fabricated 0.
* **`min`/`max` are always sent.** Omission only infers a range for units with
  an agreed width, and `%` is not one of them.
* **`direction` is emitted if and only if `window` is** — required with,
  refused without.

**Numbers only.** No `severity`, no `css`, no colour, ever.

## How "unavailable" is said without inventing a number

`observed_at` is **when the value was true**, not when it was posted. That one
rule does all the work:

| situation | what is posted | what NagLight shows |
|---|---|---|
| source read fine | the real number, stamped now | a live gauge |
| source failed, a previous reading exists | that number, stamped **when it was true** | stale → unavailable, once the horizon passes |
| source failed and never succeeded | **0 % remaining** with **no `observed_at` at all** | stale on arrival → unavailable |

The invariant in `build_post` is one line: **`observed_at == now` if and only if
this cycle actually read the source.** Every failure class — transport error,
timeout, 401, unparseable body, a well-formed body with no usable bucket, a
percent outside 0..100, and a bug in one of our own parsers — collapses to that
same path, so there is no second door through which a fresh gauge can escape.

The third row is the only number in the file nobody measured, and it exists so
the panel can say "unavailable" instead of showing nothing at all. It is
unreachable as a *displayed* value by construction, because a body with no
`observed_at` is stale the moment it arrives.

**It is deliberately the pessimistic end of the bar** (`UNAVAILABLE_CONSUMED =
GAUGE_MAX`). Under the old count-up shape the sentinel was the literal `0`,
meaning nothing consumed. Inverting to remaining turned that same literal into
**100 % remaining** — a full, green, reassuring bar for a source that has never
answered, with only staleness standing between it and the wall. Naming it in
consumed terms keeps the intent where the flip happens: an unmeasured gauge is
an empty bar, and it fails safe if staleness ever stops protecting it.

**The horizons still bound the cadence.** A `weekly` gauge goes stale 24 h
after `observed_at`, and three of the six are weekly. The timer now runs every
minute (next section), and `tests/test_ai_usage_feeder.py` still reads the
shipped timer file and fails if that interval is ever loosened past a quarter of
the tightest horizon.

## Cadence: every minute, each source on its own schedule (SR-095, SR-096)

The Owner asked on 2026-10-22 for the usage gauges to update every minute,
because they fell behind.

### What the lag was, measured before the change

The lag on the wall is the sum of two halves, measured separately on
2026-10-22:

| Half | How it was measured | Before |
|---|---|---|
| Feeder: how old `observed_at` is in what NagLight serves | 24 h of the unit's journal (each live post's run start), sampled every second; cross-checked by reading `/api/today` on the hub's loopback every 20 s for 15 min | timer runs 610-702 s apart (median 652 s). Served age p50 **330 s**, p95 **~630 s**, max 1328 s (a Claude 401 cycle and two Codex timeouts re-posted older stamps) |
| Panel: how long after NagLight has a new value the wall draws it | OfficeWallNaglight `js/main.js`: unlocked, `/api/events` fires on every feed POST (NagLight bumps its revision in `handleFeedGauge`) and the shell re-polls within a second; locked (the usual state: the panel's journal showed `authed=false`), it polls `/v1/gauges` every `POLL_SECONDS` = **60** (hub-served `config.json`, measured) | unlocked ~0 s (read from the code, not timed); locked +30 s p50, +31 s p95 (modelled from the poll period) |
| On the wall (locked) | the two combined | p50 **363 s**, p95 **667 s** |

So the feeder's half was over 95 % of the lag, and the panel needed no change:
its 60-second locked poll is inside the 3-minute target below with the feeder
at a minute.

### What a cycle does now

The timer (`homehub-ai-usage.timer`) fires every minute, up to 10 s late.
Each source -- each Claude **account** too, since `claude` and `claude2` are
separate sources -- has its own entry in the state file under the reserved key
`__schedule__` (`SCHEDULE_KEY`), validated by its own validator in `load_state`:
when it was last **attempted**, when it is next due, whether that due time is a
vendor's Retry-After (`hold`), its backoff level, its authentication history and
the vendor's rate-limit headers last seen. A cycle:

1. **skips** a source that is not due. A skip is its own outcome: nothing in the
   entry changes, nothing is posted for its gauges (NagLight keeps the body it
   was last sent, so the wall keeps the true reading at its true
   `observed_at`), and one line says why --
   `INFO codex: skipped - not due for 45s (reads every 120s)` or
   `INFO claude: skipped - backing off, 240s left (level 2, Retry-After)`;
2. reads every due source, exactly as before, and posts its gauges;
3. writes the source's next entry (`next_schedule`, a pure function):

| Outcome | Next due | Backoff | Auth history |
|---|---|---|---|
| read fine | + interval | cleared | **cleared** |
| authentication failure | + interval | cleared | first attempt kept, attempts + 1 |
| 429 or any 5xx (`Throttled`) | the vendor's `Retry-After` if it sent one (seconds or HTTP date, honoured to the second and past ten minutes, clamped only at 24 h); else interval x 2^level, ceiling 10 min | + 1 | untouched |
| any other failure, or "login owed" | + interval | cleared | untouched |

A healthy source is due 15 s early (`DUE_SLACK_SECONDS`), because cycles land
60-75 s apart and a source due at +60 s that the next cycle reaches at +59 s
would otherwise wait a whole extra minute. A vendor's Retry-After gets no slack.
Each throttled attempt prints `<source>: HTTP 429 (rate-limit headers: …)` and
`<source>: backing off Ns (level n, …)`. Only rate-limit header **names** are
recorded (`retry-after`, `x-ratelimit-*`, `ratelimit*`, `anthropic-ratelimit-*`),
and only values that look like numbers or dates.

**Clock changes.** A schedule entry whose last attempt is in the future (past the
5-minute skew allowance) is a clock that stepped back; it is dropped, so the
source is read at once instead of being skipped for as long as the clock went
back. A clock that steps forward needs no rule: every due time is in the past.

### Per-source intervals, and why Codex is every other cycle

`AI_USAGE_<SOURCE>_INTERVAL` (seconds, 60..600, refused by name otherwise) sets
each one; blank is the default. The HTTP sources default to **60 s**. Codex
defaults to **120 s**, chosen by measurement: per-process accounting of the
unit's cgroup on 2026-10-22 put one normal cycle at 3.1 s of CPU, of which the
`codex app-server` tree was 1.5 s, the cycle's Python 1.1 s and the unit's
`--check` pre-start 0.45 s; a Codex read that timed out cost 4.2 s, and spawned
over a hundred `lsb_release` shells. Reading Codex every minute would be ~2.5 %
of one core for a weekly gauge; every second cycle halves it and still meets the
freshness target. The alternative -- a long-lived, separately supervised
`app-server` -- was not chosen: its idle cost could not be measured without
running it against the vendor, it would need its own credential-reload logic,
and the oneshot already bounds every child (below). Setting
`AI_USAGE_CODEX_INTERVAL=60` reads it every minute at that cost.

**The read never leaves a process behind.** `read_codex` starts the app-server
in its own session and `end_child` kills the whole process group, then reaps it
(the class of fault W1 fixed). Before, only the app-server itself was killed,
and its helpers waited for systemd to clean the unit up.

### The freshness target

**At steady state the panel shows each usage gauge with an `observed_at` no more
than 3 minutes old at the 95th percentile.** Modelled from the shipped timer
(60 s + up to 10 s + up to 5 s), the due slack and a locked panel's 60-second
poll: the per-minute sources p50 ~66 s, p95 **~110 s**; Codex at 120 s p50 ~100 s,
p95 **~170 s**. The live check below measures it.

### A dead login, confirmed over attempts and time

See the next section for the marker. What changed with the minute timer is the
confirmation rule, because "two consecutive cycles" would have greyed a gauge
two minutes into any blip.

### Verifying the cadence on the hub (the 24-hour check)

Run after deploying, against the W3a CPU baseline:

1. **Cycles every ~60 s**: `journalctl -u homehub-ai-usage.service --since -24h
   -o short-unix | grep 'Starting homehub-ai-usage'` -- the gaps between starts
   should be 60-75 s.
2. **Freshness p95**: sample what the panel reads. On the hub, every 20 s for
   24 h, GET `http://127.0.0.1:8787/api/today` with `X-Forwarded-User` set to the
   tracker identity (read from `/opt/homehub/stack/.env`, never printed), and
   record `now - observed_at` per `ai-usage-*` gauge; add the panel's poll
   (uniform 0..60 s while locked). The p95 per gauge must be <= 180 s. The same
   numbers can be derived from the journal alone: each `live posted` line's run
   start is its `observed_at`.
3. **429s and backoff**: `journalctl -u homehub-ai-usage.service --since -24h |
   grep -E 'HTTP (429|5[0-9][0-9])|backing off|skipped - backing off'` -- zero
   lines, or each names its source and the delay it honoured. A source that
   backed off has its default raised (its `AI_USAGE_<SOURCE>_INTERVAL`) and the
   change is recorded.
4. **Feeder CPU**: `journalctl -u homehub-ai-usage.service --since -24h | grep
   -c Consumed` and the sum of the `Consumed …s CPU time` values, against the W3a
   baseline.
5. **No spurious authentication classification**: `grep 'authentication
   failure'` -- any `CONFIRMED` line must match a login that really is dead
   (see the refresh-gauge-logins skill); an unavailable or stale gauge must
   carry its reason in the same cycle's lines.

### The Claude refresher now renews before the token lapses

Measured on the hub between 2026-10-12 and 10-21: all 14 Claude 401s the feeder
logged came 237-242 minutes after the previous `homehub-claude-refresh@claude`
run and cleared at the next one. The CLI renews the 8-hour access token only
when a run lands near its expiry, not on every run, so a refresher every 4 h left
the token lapsed for minutes at a time, about twice a day -- once for at least 11
minutes (two consecutive 401 samples on 10-12). On a minute timer that is enough
to confirm an authentication failure that is not one.

`homehub-claude-refresh@.timer` now checks every 4 minutes, and the service's new
`ExecCondition` (`claude_refresh_due.py`) lets the CLI run only once the token
has under 20 minutes left, has expired, or its expiry cannot be read. The helper
exits 3 for "not due" and the unit maps exactly that to the skip; a crash, a
missing helper or a missing python all **run** the CLI, so the gate can only add
runs, never take the last one away. It reads one number (`expiresAt`), never
writes, and prints nothing about the credential.

## A dead login goes grey, and stays grey (plan 3.6, G2)

Until 2026-10-11 a source that failed re-posted its last real reading at its
original `observed_at`, so the wall showed that number **in colour** until
NagLight's horizon passed (24 h weekly, 36 h daily, 48 h monthly). Codex was
rejected server-side for six days and Claude's refresh token lapsed for a day;
both looked healthy. Now a failure **classified as authentication** greys the
gauge, while every other failure keeps exactly the old behaviour.

**Typed.** `AuthFailure(SourceFailure)` carries `status` and `code`. It is still
a `SourceFailure`, so every old handler works, but `run_cycle` catches it
*first* — listed second it would be swallowed by the plain handler. No reader
may catch an `AuthFailure` and re-raise a plain one. What counts:

| Source | Authentication |
|---|---|
| any HTTP source | an HTTP **401** (`http_json`) |
| `codex` | the server's `token_expired` (or a 401 stated as such) in the JSON-RPC error frame |
| `claude`, `claude2` | the credential file parsed but carries **no usable key** — *Claude readers only*; the same words once came from a renamed OpenCode schema (`NoUsableKey` is a plain failure everywhere else) |
| `gemini` | a 401, or the cached access token past its own `expiry_date` |

**Confirmed over attempts and time** (`AUTH_CONFIRM_ATTEMPTS` = 2,
`AUTH_CONFIRM_SECONDS` = 600; revised 2026-10-22 for the minute timer). A
source's gauges grey only after failed authentication **attempts** that number
at least two and span at least ten minutes, from the first of the run to the
latest. That absorbs the Claude refresh unit rewriting the credential mid-read
("no usable key" once) and an access token that lapses a few minutes before its
renewal (measured: section above). Only attempts count: a skipped cycle adds
neither an attempt nor any span. A success resets the history; a failure of
any other kind -- a network error, a 5xx, "login owed" -- is evidence neither way
and leaves it as it is. Until confirmation the gauge keeps its last reading; the
journal says `authentication failure, attempt N over Ns - last reading kept
until confirmed (2 attempts over 600s)`, then `… - CONFIRMED, its gauges are
posted unavailable`. On the minute timer a dead login greys about 10 minutes
after its first failed read (on the old 10-minute timer the two-cycle rule took
up to about 20).

**The history lives in the state file**, in the source's schedule entry
(`auth_since`, `auth_attempts`; previous section). A state file written before
2026-10-22 holds the old counter under `__auth_failures__`; it has no validator
any more, so the first new cycle drops it and the history restarts, while every
reading and every marker survives.

**Marked, never deleted.** On the confirming cycle each stored reading of that
source is *replaced* by `{"auth_failed": true, "at": <epoch>}` — the key stays.
`state_keys` answers "have we ever seen this gauge", and an optional gauge
(Fable) is skipped when its key is absent: deleting would make the Fable band
*vanish* instead of going grey. The marker has its own validator in `load_state`
(otherwise the next load would drop it, key and all). `build_post` treats it as
no usable reading, so it posts the never-measured body (value 0, no
`observed_at`, stale on arrival) — and, being stored, it keeps doing so after a
later network failure (**sticky**). A real reading overwrites it when the source
recovers. A gauge the box has never posted is not marked (that would conjure it).

## The Codex reader says why it failed (plan 3.5, RR2-14)

`codex app-server` answered a revoked login with a bare JSON-RPC error code and
the feeder printed only `codex: JSON-RPC error -32603`; the server's `token_expired`
was invisible for six days. The failure now carries the server's **message**
(and `data`) — redacted, then cut to 200 characters. Redaction runs *first*
(cutting first could leave a prefix of a token that no pattern recognises) and
replaces `Bearer …`, JWTs, `sk-`/`ghp_`/`ya29.`-style keys and any run of 24+
base64/hex characters, and flattens control characters so a hostile server cannot
forge a journal line. This is the only place a remote's own words reach a
message; `post_gauge` still never logs a response body.

## Never writes a vendor credential file

`open_for_write` is the **only** door in the module through which a file is
opened for writing, and it is an **allow-list of one path** (the state file)
plus its `.tmp` sibling. That is deliberate: a deny-list of known credential
filenames cannot survive the next CLI version putting its token somewhere new.
The credential basenames are a *second* check whose only job is to make the
error message name the real hazard if someone points the state file at a token.

Three further lines back it up:

* `read_secret_file` has no write mode; a test greps the function for one.
* the unit mounts the home the credentials live in **read-only**
  (`ProtectHome=read-only` — not `yes`, because the feeder must still read it).
* a test runs a whole cycle inside a throwaway HOME holding all three real-shaped
  credential files and fails if **any byte under it changed**, if more than one
  file was written, or if the file that was written contains a token.

The state file holds **percentages and timestamps only**, and that is asserted.

**The allow-list resolves symlinks, and it is bounded twice.** `os.path.abspath`
is string arithmetic — it does not resolve symlinks — so pre-creating
`<state>.tmp` as a link pointing at a token passed the allow-list unchanged (the
*string* matched) and the write truncated the token. The guard now uses
`os.path.realpath`, **and** requires the resolved path to sit inside the
service's own `StateDirectory=` — a bound the `.env` cannot move, because a knob
naming a token still names something outside `/var/lib/homehub-ai`. The open
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
  identity and the body, to a host the *response* named;
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
vendor endpoint would hand out the household's Claude OAuth access token or the
OpenCode workspace key. Both endpoints answered **200 directly** during the
2026-09-09 verification calls, so refusing costs nothing that has ever been
observed, and the price if a vendor starts redirecting is an "unavailable"
gauge — which is the outcome this feeder exists to produce when it cannot read
a source honestly. A vendor URL must also be `https`, because the URL is a knob
and the headers carry a bearer token.

The peer check runs inside `connect()`, after the TCP handshake and **before**
`http.client` writes the request line, so a connection that lands somewhere it
should not is dropped with the token and the body still unsent.

**Nothing a remote sends is written to the journal.** `post_gauge` reports the
status code and never the response body: `main` prints failures to stderr and
systemd persists them, so an error body a responding server or an interposed
proxy chose to reflect — a token echoed back included — used to end up on disk.

## One identity, and it refuses to guess

`AI_USAGE_USER` is the stable Google `sub`, the same value as `NAGLIGHT_USER` /
`TRACKER_MIRROR_USER` / `TRACKER_DRIVE_USER`. **Blank is a refusal, not a
default**: `setup-ai-usage.sh` refuses to install and `resolve_identity` refuses
to run, both saying why. There is no "the only user" fallback, because a
single-user box today is a two-user box after one oauth2-proxy login, and a
usage gauge on the wrong person's board is a lie about their own subscription.

`AI_USAGE_FEED_URL` refuses in the same spirit: loopback or an address a local
docker bridge is **actually carrying**, never a LAN or public host and never the
https route through oauth2-proxy, which would overwrite `X-Forwarded-User`. That
is the lesson `ai_cli_service.resolve_bind` learned in the SR-019 cross-review —
`172.16.0.0/12` contains real household LANs, so membership of the range proves
nothing.

## Off by default

`AI_USAGE_ENABLED=false` ships. There is no compose profile to join because this
is not a container, so the knob **is** the gate — the same shape as
`AI_CLI_ENABLED` and `REMOTE_UI_ENABLED`. With it false, no unit is installed,
no timer exists and nothing is read. Only a literal `true` enables it; `True`,
`yes` and `1` are all off, because a feeder that starts on an ambiguous value
starts on a box nobody asked.

## Running it by hand

    sudo bash /opt/homehub/stack/ai-usage/setup-ai-usage.sh   # install
    sudo -u homehub-ai python3 /opt/homehub/stack/ai-usage/ai_usage_feeder.py --check
    sudo -u homehub-ai python3 /opt/homehub/stack/ai-usage/ai_usage_feeder.py

`--check` re-runs the identity and destination refusals and exits without
touching a vendor or the tracker.

## Known gaps

* **Gemini** — built from the CLI's source, not an observed response; see "Not
  proven" under Gemini above.
* **Claude 2's `.claude.json` placement** is measured on Windows (Claude Code
  2.1.296), not yet on the hub's Linux build.
* **OpenCode `rolling`** — no window length is published, see above.
* **The minute cadence's vendor-side cost is unmeasured** until the 24-hour
  check: none of these endpoints publishes a rate limit, and 10x the requests is
  a risk designed against (backoff, Retry-After), not one disproved.
* **`stack/run-hermetic-tests.sh` cannot run on the dev PC** (`missing tool(s):
  zstd rsync`), so this feeder's shell-level carriage has never been exercised
  there. The Python suite is the bar that does run.
