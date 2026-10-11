# Configuration reference

This file holds one section so far: the AI-usage feeder's knobs. It exists so a
knob has one place that says what it is, what reads it and what happens when it
is wrong. The authoritative defaults and the long reasons are the comments in
`stack/.env.example`; the design is `stack/ai-usage/README.md`.

## AI-usage feeder (`stack/ai-usage/`, SR-021, SR-067, SR-068, SR-069)

All knobs live in the hub's `/opt/homehub/stack/.env` (the file
`homehub-ai-usage.service` loads with `EnvironmentFile=`). **No credential is
ever a knob**: the feeder reads the sign-ins a person made as
`AI_USAGE_USER_ACCOUNT` and never writes one.

### Choosing the rings: `AI_USAGE_SOURCES`

Comma-separated, whitespace ignored, an unknown name **refused** (the feeder
exits, it does not skip).

| Source | Posts | Credential the service reads | Opt-in? |
|---|---|---|---|
| `codex` | `ai-usage-codex` | none: the child `codex app-server` reads its own `CODEX_HOME` | in the default |
| `claude` | `ai-usage-claude-session`, `-weekly`, `-weekly-fable` | `AI_USAGE_CLAUDE_CREDENTIALS` | in the default |
| `claude2` | `ai-usage-claude2-session`, `-weekly`, `-weekly-fable` | `AI_USAGE_CLAUDE2_CREDENTIALS` | **yes** |
| `gemini` | `ai-usage-gemini-daily` | `AI_USAGE_GEMINI_CREDENTIALS` | **yes** |
| `opencode` | `ai-usage-opencode-weekly`, `-monthly` | `AI_USAGE_OPENCODE_AUTH` | in the default |

The absent-knob default is `codex,claude,opencode`. The five-ring set the wall is
built for is:

```
AI_USAGE_SOURCES=claude,claude2,codex,gemini,opencode
```

`claude2` and `gemini` never switch themselves on, because each needs its own
sign-in first (printed by `setup-ai-usage.sh`; the commands are also in the
`refresh-gauge-logins` skill).

**Changing the set** (the panel draws whatever is served, so this is the whole
feeder-side edit):

1. Edit `AI_USAGE_SOURCES` in `/opt/homehub/stack/.env`.
2. `sudo bash /opt/homehub/stack/ai-usage/setup-ai-usage.sh`. This installs or
   removes the matching token keepers (`homehub-claude-refresh@claude2.timer`,
   `homehub-gemini-refresh.timer`); skipping it leaves a new source with nothing
   keeping its token alive.
3. `sudo systemctl restart homehub-ai-usage.timer`, or run one cycle now with
   `sudo systemctl start homehub-ai-usage.service`.

The panel's own ring list (`gaugeRings` in the hub-served panel config) is the
panel builder's and is not described here.

### Per-source knobs

| Knob | Default | Meaning |
|---|---|---|
| `AI_USAGE_CLAUDE_CREDENTIALS` | `/home/homehub-ai/.claude/.credentials.json` | Account 1's credential file. |
| `AI_USAGE_CLAUDE2_CREDENTIALS` | `/home/homehub-ai/.claude-2/.credentials.json` | Account 2's file, inside the directory it was signed in under with `CLAUDE_CONFIG_DIR`. **Never a copy of account 1's**: the refresh token is single-use and rotating. |
| `AI_USAGE_CLAUDE_VERSION` | `2.0.0` | The `claude-code/<version>` user agent both accounts send. |
| `AI_USAGE_GEMINI_CREDENTIALS` | `/home/homehub-ai/.gemini/oauth_creds.json` | The file Gemini CLI maintains. Read for `access_token` and `expiry_date` only; the feeder never refreshes or writes it. |
| `AI_USAGE_GEMINI_MODELS` | blank | Model ids, comma-separated, **exactly as the quota call names them**. The gauge is the lowest remaining fraction among them, and the label says so. Blank means every bucket returned (label: "all models"). A named model with no bucket **refuses the reading**. |
| `AI_USAGE_GEMINI_PROJECT` | blank | Only for an account whose `loadCodeAssist` answer names no project. |
| `AI_USAGE_GEMINI_URL` | `https://cloudcode-pa.googleapis.com/v1internal` | Base of the two Gemini calls. Must be `https`. Not in `.env.example`: there is no reason to move it except a test. |
| `AI_USAGE_CODEX_BIN` | `codex` | The binary the Codex reader launches. |
| `AI_USAGE_TIMEOUT_SECONDS` | `30` | Per-source timeout, whole seconds. |
| `AI_USAGE_STATE_FILE` | `/var/lib/homehub-ai/usage-state.json` | The one file the feeder writes. Must stay inside `/var/lib/homehub-ai`. |

### What a failure looks like in the journal

`journalctl -u homehub-ai-usage.service` prints one line per failure, prefixed by
the source name.

| Line | Meaning |
|---|---|
| `claude: credential … carries no usable key` followed by `claude: authentication failure 1 of 2 - last reading kept until confirmed` | An authentication-class failure. The gauge keeps its last reading for one cycle. |
| `… authentication failure 2 of 2 - CONFIRMED, its gauges are posted unavailable` | The second consecutive one: the source's gauges go grey (posted with no `observed_at`) and **stay grey** until a real reading arrives. |
| `codex: JSON-RPC error -32603: … token_expired …` | Codex rejected the login server-side. The message is truncated to 200 characters and long opaque strings are replaced by `[redacted]`. |
| `gemini: the CLI's access token has expired and has not been renewed …` | The token keeper is not renewing it. Check `systemctl status homehub-gemini-refresh.service`. |
| `gemini-refresh: DEAD LOGIN - the CLI asked for an interactive sign-in …` (from `homehub-gemini-refresh.service`, exit 4) | Gemini CLI's stored login cannot be renewed. Sign in again; the ring is grey (or about to be) until then. |
| `<url>: HTTP 401` | The vendor refused the token. Authentication class. |
| anything else (`HTTP 500`, `…: URLError`, `body is not JSON`) | An ordinary failure. The last reading is re-posted at its original stamp and ages out on its own horizon. |

What counts as authentication, and nothing else: an HTTP 401; Codex's
`token_expired` (or a 401 stated as such in its error frame); Claude's
credential file carrying no usable key (Claude readers only, because the same
words once came from a renamed OpenCode schema); Gemini's access token being past
its own `expiry_date`.

The count of consecutive authentication-failed cycles is kept in the state file
under the reserved key `__auth_failures__` (counts only), so it survives the
feeder being a one-shot unit; it does not survive a reimage. Any cycle in which
the source succeeds, or fails some other way, resets it.
