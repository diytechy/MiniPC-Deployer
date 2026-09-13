# The AI-usage feeder (SR-021, SN-016, IF-013)

Reads how much of each AI subscription has been consumed and posts it to
NagLight as gauges. A plain hub service on a systemd timer — **no container**,
**off by default**, and it **never writes a vendor credential file**.

## The three sources, and the one real call each was built from

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

**Gemini is out** — deferred as E11.

## What it posts (IF-012, amended by v1.1 on 2026-09-12)

Six gauges, upserted by id: `ai-usage-codex`, `ai-usage-claude-session`,
`ai-usage-claude-weekly`, `ai-usage-claude-weekly-fable`,
`ai-usage-opencode-weekly`, `ai-usage-opencode-monthly`. Each body is
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

**The cadence follows from the horizons, not from taste.** A `weekly` gauge goes
stale 24 h after `observed_at`, and three of the six are weekly. The timer runs
every 10 minutes — 1/144th of the tightest horizon — and
`tests/test_ai_usage_feeder.py` reads the shipped timer file and fails if that
interval is ever loosened past a quarter of it.

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

* **Gemini** — deferred as E11; no source, no parser.
* **OpenCode `rolling`** — no window length is published, see above.
* **`stack/run-hermetic-tests.sh` cannot run on the dev PC** (`missing tool(s):
  zstd rsync`), so this feeder's shell-level carriage has never been exercised
  there. The Python suite is the bar that does run.
