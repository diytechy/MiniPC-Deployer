# voice-listener — the hub's ear for the living-room assistant (SN-056)

Implements **SR-163..SR-169**. Interfaces: **IF-085** (MINI-SERV's action
interface, consumed) and **IF-086** (the listener's state, provided). The plan
is HomeHub `docs/HUB_VOICE_LISTENER_PLAN_2026-11-11.md`; the hub tests, not yet
run, are HomeHub `docs/HUB_VOICE_LISTENER_TEST_PLAN_2026-11-11.md`.

**Status (2026-11-11): built and tested on the dev PC; deployed nowhere; it has
never read a microphone.** It ships off and cannot hear the room until the
Owner has ruled (below).

`homehub-voice-listener@<camera>` reads the camera's **sound** from the camera's
own relay ([`../camera-relay/README.md`](../camera-relay/README.md)), waits for
the wake phrase ("hey Bruno"), turns the request that follows into text and
posts the text to MINI-SERV (`POST /v1/utterance`), whose own matcher decides
and acts. The hub matches nothing, speaks nothing and keeps no sound.

## Three locks, all shut as shipped

| Key | Shipped | What it takes |
|---|---|---|
| `CAMERA_<C>_LISTENER_ENABLED` | `false` | `true`: the unit's `ExecCondition` |
| `CAMERA_<C>_LISTENER_SOURCE` | `none` | `relay`: the room's sound |
| `CAMERA_<C>_LISTENER_ROOM_AUDIO_RULING` | empty | `ratified-YYYY-MM-DD`: the date of the Owner's recorded ruling |

The hub's standing rule is that **no hub process takes the living-room
camera's audio** (SR-156; HomeHub `DECISIONS_RATIFIED.md`, 2026-11-10). Only the
Owner changes it (HomeHub open-items A86). Until that ruling is recorded and its
date is in the third key, `run` exits 78 before a reader, an engine or a socket
starts. Nobody else may fill that key in. The first key is checked by `run` and
`levels` themselves as well as by the unit's `ExecCondition`: started by hand
with it false, both refuse the room's sound (review 2026-11-12). When the ruling comes, SR-156 and
SN-055 are amended in the same change.

`CAMERA_<C>_LISTENER_UNTIL` gives a trial an end: at that UTC instant the
listener stops itself, and a restart or a reboot after it starts nothing (the
training capture's rule: it cannot be forgotten on).

`SOURCE=file` plays a WAV of **synthetic** speech instead of a microphone: the
cost test's source. It needs no ruling, asks MINI-SERV nothing and posts
nothing.

## When it hears

Every pass it decides whether it is armed. Disarmed, the reader container is
stopped and removed: no sound reaches the listener at all. First match wins:

| Shown as | Why it is not hearing |
|---|---|
| `ended` | `UNTIL` has passed: the run ends and nothing starts it again |
| `hold` | `/run/homehub-voice/<camera>/hold` exists (`touch` it to switch listening off; `rm` it to switch it back) |
| `peer-unreachable` | MINI-SERV's `GET /v1/state` failed twice in a row: a transcript would have nowhere to go |
| `peer-muted` | MINI-SERV reports its voice `muted`: the household's mute there holds here too |
| `peer-listening` | MINI-SERV reports its voice `armed`: its own listener is on, and two would act twice on one sentence |
| `presence-unknown` | `PRESENCE=required` and the presence detector is stopped, stale or has not judged yet |
| `room-empty` | `PRESENCE=required` and the presence detector says nobody is in the room |

## What happens to the sound

| Step | What | Cost gate |
|---|---|---|
| Reader | the pinned relay image's ffmpeg: the relay's audio track only, to 16 kHz mono on a pipe; `--cpus 0.05`, lowest weight, read-only, no log driver | runs only while armed |
| Level | RMS of each 80 ms frame against a learned background (down in 1 s, up in 10 s) | |
| Activity gate | open when two of three frames stand out by 8 dB; the 480 ms before come first; closed 1.2 s after the last sound. Steady sound becomes background in about 5 s | the wake word runs only while open |
| Duty ceiling | `DUTY_MAX_PCT` below 100: a hard share of the time for the engine; deaf while spent | optional |
| Wake word | sherpa-onnx keyword spotting, the phrase as the model's own tokens; one stream as shipped (`WAKE_STREAMS=2` hears a phrase said straight after other speech more reliably, at about twice the engine's cost) | |
| Request | EntertainmentPC Core's timings: ends 0.7 s after speech stops, at 8 s, or after 3 s of nothing | |
| Text | Moonshine tiny; the audio is zeroed | a burst of about half a second on the hub |
| Post | one `POST /v1/utterance` to MINI-SERV with the token; a 503 or a lost answer is retried with the **same** `request_id` for 8 s in all (an answer that arrives too slowly is cut off at that deadline); then dropped, never queued | |
| Hold | nothing is heard for 3 s after the post call returns, whatever its outcome, however long it took (the reply is not a request) | |

**Measured on the hub (2026-11-11, synthetic audio, one stream, no gate):** the
wake word costs 15.7 % of one core and the reader 0.8 %; a command's text
takes about 0.5 s. That is why the gates exist. What each is expected to save,
and what only a hub test can decide, is the plan's section 5.

## What it keeps

Nothing. Sound lives in a frame, the gate's pre-roll, the session's pre-roll
(`PRE_ROLL_MS`, 0 as shipped) and the request being spoken; each is zeroed when
handed on, at every return to idle and on disarming. The unit has no swap and
no core file, and its own sockets reach only this machine and private (home
network) addresses: the speech code cannot reach the internet. The only files written are the keyword line (configuration), the
status file and the token's import into `.env`. A transcript goes to MINI-SERV
and nowhere else: the journal, the status file and the feed carry states,
reasons, counts and outcome codes.

## Output

- **Status:** `/var/lib/homehub-voice/<camera>/status.json`: the state and its
  reason, counts (arms, gate openings, wake phrases, requests, outcomes), the
  share of the time the gate was open, the engine's seconds, the background
  level in dBFS.
- **Journal:** `journalctl -t homehub-voice-listener`: armed, disarmed and why,
  each wake phrase, each request's outcome code.
- **Feed (IF-086):** JSON lines on `LAN_IP:CAMERA_<C>_LISTENER_FEED_PORT` (0 =
  off), only to `CAMERA_<C>_LISTENER_FEED_PEERS` (default `MAIN_BOX_IP`), so
  MINI-SERV can show "listening" on the TV:

```
{"v":1,"type":"state","event":"living_room.listener","state":"listening","detail":"","since":"2026-11-11T20:14:03Z"}
```

States: `disarmed` (with the reason above), `armed`, `hearing` (the engine is
running), `listening` (the wake phrase was heard), `working`.

## The runtime

sherpa-onnx 2.0.1 in `/opt/homehub-voice/venv`, from `requirements.lock`
(three wheels, exact versions and hashes: the ones the hub's cost test ran).
The models are **not committed**: `models.manifest.json` pins both archives and
every file used by size and SHA-256, the same files as EntertainmentPC's
Windows listener. `install-voice-runtime.sh` verifies an archive before opening
it, takes out only the listed files and verifies each; the listener verifies
them again at every start. Nothing of it is fetched on the hub; the one system
package it needs, `python3.12-venv` (deploy step 0), comes from Ubuntu's archive
if the hub does not have it yet.

On the dev PC (WSL), once, into a folder outside every checkout:

```sh
pip download --only-binary=:all: --platform manylinux2014_x86_64 --python-version 312 \
    --no-deps --require-hashes -r stack/voice-listener/requirements.lock -d wheelhouse/
# the two archives named in models.manifest.json ("archive.url"), checked against "archive.sha256"
```

## Deploy steps (the coordinator's; none has been run)

Every step waits on HomeHub open-items A86. The order and the proof expected
are the test plan's.

```sh
# 0. the relay is up with CAMERA_LIVINGROOM_RELAY_AUDIO=true; presence is deployed (or PRESENCE=ignored)
sudo apt-get install --no-install-recommends python3.12-venv
# 1. the wheelhouse and the two archives onto the hub's /tmp, then:
sudo bash /opt/homehub/stack/voice-listener/install-voice-runtime.sh --wheelhouse /tmp/wh --models /tmp/models
# 2. the CAMERA_LIVINGROOM_LISTENER_* and MINISERV_ACTION_PORT keys into .env; the token through a pipe, never argv:
#      <store value> | ssh hub@<hub> 'sudo -n python3 /opt/homehub/stack/voice-listener/voice_listener.py \
#          --camera livingroom import-token'
sudo /opt/homehub-voice/venv/bin/python /opt/homehub/stack/voice-listener/voice_listener.py --camera livingroom check
# 3. the cost test FIRST, on synthetic audio (SOURCE=file): the test plan, section 3
# 4. only after the ruling: SOURCE=relay and the ruling's date; then
sudo install -m 0644 /opt/homehub/stack/voice-listener/homehub-voice-listener@.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now homehub-voice-listener@livingroom
journalctl -t homehub-voice-listener -f
```

## The room as numbers

```sh
sudo /opt/homehub-voice/venv/bin/python /opt/homehub/stack/voice-listener/voice_listener.py --camera livingroom levels --seconds 60
```

What the microphone hears, as loudness only: the level's spread in dBFS, the
background the gate learned, how often and for how long the gate would have
opened. It reads the room, so it sits behind the same three keys as the service
(`ENABLED=true`, `SOURCE=relay`, the ruling's date); it needs no MINI-SERV, loads no engine, posts nothing
and keeps nothing. It is the level test nobody has run: can this microphone
tell speech from the room, with the television on?

## Trying it without a hub

```sh
# each WAV of synthetic speech through the whole path (no post); --show-text prints the words
python3 voice_listener.py --env-file ENV --camera livingroom clips --wav cmd-01.wav --show-text
# the listener's own CPU on a looped WAV, paced to the clock
python3 voice_listener.py --env-file ENV --camera livingroom measure --wav loop.wav --seconds 420
```

`ENV` needs only `CAMERA_LIVINGROOM_LISTENER_MODEL_DIR` (and any knob under
trial). The interpreter must import the pinned sherpa_onnx (CPython 3.12 on
x86-64 Linux; under WSL the lock's three wheels unpacked on `PYTHONPATH` do).

## Tests

`tests/test_voice_listener.py` (pytest, TC-1780..TC-1800, TC-1803, TC-1807): the locks
and every refusal, the gate, the ceiling, presence and the arm decision, the
session against Core's timings, the whole path with stand-in engines, the
document and the client against a stand-in of MINI-SERV that applies the
interface's own rules, the model pin, the wake phrase's tokens, the reader's
argv, the loop, that no transcript reaches a log, the status or the feed, and
the unit, template, lock and installer.

`tests/test_voice_listener_engines.py` (opt-in; skips unless told where things
are): TC-1801, the real engines on synthetic commands; TC-1802, the client
against EntertainmentPC's built SceneService on loopback.

Not covered anywhere yet: a real voice, a real room, the relay's audio, the
hub's CPU with the gates, MINI-SERV installed as a service. Those are TC-1804
to TC-1806, the test plan.
