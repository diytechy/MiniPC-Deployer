# devpc-wake — measured readiness for the dev PC

Implements **SN-044**, **SR-044**, **LLR-966** … **LLR-970**, **LLR-974**,
**LLR-978**. Interfaces **IF-021**, **IF-022**, **IF-023**, **IF-025**, **IF-026**.

Use the dev PC's GPU for local inference without leaving it powered
continuously. A request wakes it; it goes back to sleep when nobody is using it.

---

## `up` is not `ready`, and that is the whole design

A woken machine answers ICMP **tens of seconds** before it can answer a prompt,
because the weights still have to reach VRAM. A caller told `ready` when the
truth is `up` will block on a completion and then blame the model.

Two layers, never collapsed into one truthiness check:

| Layer | Question | Answers |
|---|---|---|
| reachability | ICMP / TCP to the inference port | `off` / `up` |
| readiness | a model listing that names the target model | `up` / `ready` |

**The two timeouts look identical in a log and mean opposite things.** A
*reachability* timeout is `off` — a powered-off box times out, and calling that
`up` suppresses the very wake this service exists to fire. A *listing* timeout
against a host already answering is `up` — the box is demonstrably alive and
merely not servable yet.

Precedence when the layers disagree is fixed: **expired deadline → reachability
→ readiness.** An expired wake must never be masked by a stale probe.

`waking` is the only estimated state. It is entered solely by firing a wake and
carries a hard deadline, after which it becomes `failed` — it is structurally
incapable of persisting, because a state that can stay `waking` forever never
tells a caller to stop waiting.

## One wake, however many requests

A wake is a **single-flight** operation keyed to the target. Concurrent requests
join the in-flight wake; they do not each fire a packet. The flight owns the
deadline and a joiner inherits the remaining time rather than refreshing it — a
deadline any newcomer can refresh never expires under load.

**Nothing blocking happens under the lock.** Under it: read state, join or
start, publish the terminal state, detach the flight, snapshot the waiters.
Outside it: fire, probe, publish, notify. Firing under the lock would deadlock a
waiter continuation that re-enters, and probing under it would turn a bounded
critical section into a wait as long as the slowest network operation —
stalling every reader including the health endpoint that would explain why.

## The wake path is unverified, and may never work

The dev PC's physical NIC is bound to a Hyper-V **external vSwitch**, and the
host's LAN address lives on a `vEthernet` adapter, not the NIC. External-switch
binding has historically caused the vSwitch to consume the magic packet before
the NIC's wake filter matches it.

**The driver reports every wake filter armed either way, so the arming evidence
is not evidence.** This is proven only by a physical test, from S3 and again
from S5.

Two consequences baked in:

- `DEVPC_WAKE_MAC` must be the **physical adapter's** MAC. Resolving one from
  `DEVPC_HOST` returns the vEthernet adapter's and wakes nothing.
- Actuation sits behind a seam. `DEVPC_WAKE_COMMAND` selects an alternative —
  a smart plug with firmware restore-on-power-loss, say — **without touching the
  state machine**. If the physical test fails, that costs one provider, not the
  service.

## Sleep: the hub decides, the dev PC acts

Earlier drafts contradicted themselves here for four review rounds. The split is
now explicit:

- **The hub owns policy.** It publishes a permit/deny verdict (IF-026) and
  **never actuates**. It holds no credential that could suspend the dev PC.
- **The dev PC owns actuation.** It *polls* the verdict — opening no inbound
  endpoint — and re-checks its own in-flight work immediately before suspending.

That re-check is why polling beats a command: a sleep command is evaluated when
**sent** and applied when it **arrives**, and a completion that started in
between would be lost.

**The session signal fails closed to *attached*.** Absent, stale, malformed or
unreachable all inhibit sleep. The asymmetry is the requirement: a false
`attached` costs idle watts until the next poll; a false `detached` suspends a
machine somebody is using.

Note the producer is a **service-mode agent**, not the remote-desktop product:
that product observes only its own sessions and cannot see a **local console**
session, so you sitting at the physical keyboard would have read as detached.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/state` | IF-023 readiness document, with the instant it was determined |
| `GET` | `/sleep-verdict` | IF-026 permit/deny, polled by the dev PC |
| `GET` | `/health` | **this service**, not the dev PC |
| `POST` | `/wake` | join or start the single flight |

`/health` deliberately does **not** report the dev PC's state: uptime-kuma
watches this service, and a sleeping dev PC is the normal case. Conflating them
would make the monitor red every night and train you to ignore it.

## Known gap — CLOSED

SR-044 requires a request for a local model on a cold box to be **held** and to
fall back rather than error. The gateway is a third-party binary and cannot host
that logic, so the hold belongs in a shim in front of the dev PC.

**That shim is built**: `stack/litellm/devpc_hold_hook.py`, a LiteLLM
`CustomLogger` whose `decide_hold()` is a pure function over this service's
`/state`. Read its header before changing any error string — the gateway
classifies retryability by **substring-matching the message**, so `"503"` and
`"unavailable"` are load-bearing and `TC-1025` asserts them.

## Known defects

**`/sleep-verdict` throws on every call.** In `devpc_wake_service.py`:

```python
Handler.session_reader = lambda: read_session(cfg.session_url, cfg.probe_timeout)
```

That assigns a plain function as a **class attribute**, so `self.session_reader()`
binds it as a method and passes `self` — giving
`TypeError: <lambda>() takes 0 positional arguments but 1 was given`. It needs
`staticmethod(...)`. Confirmed live on the hub 2026-09-19.

It is not currently breaking anything: nothing polls that endpoint yet, and with
`DEVPC_SESSION_URL` empty the verdict fail-closes to "attached" regardless. It
**is** a prerequisite for the dev-PC sleep agent, so it is Phase 0.1 of the
lifecycle plan (in the `HomeHub` repo,
`docs/DEVPC_INFERENCE_STATE_AND_LIFECYCLE_PLAN_2026-09-19.md`).

## Deployment status, 2026-09-19

Enabled and running on the hub: `DEVPC_WAKE_ENABLED=true`,
`homehub-devpc-wake.service` enabled and active, serving on `:8799`. `/health`
answers, `POST /wake` fires the magic packet, and the state machine settles
`waking → failed → off` when nothing answers.

Both the knob and `systemctl enable` were required — `firstboot.sh` **disables**
the unit whenever the knob is not `true`, so a hand-start alone would not have
survived a reboot.

**The physical S3/S5 wake is still unproven**, and §"The wake path is unverified"
above still governs. `DEVPC_WAKE_URL` is deliberately still empty, so the hold
hook does not yet consult this service.
