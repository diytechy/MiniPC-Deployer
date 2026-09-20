#!/usr/bin/env python3
"""Every leg of a call the panel is carrying, and the state of that call.

IT STARTED AS ONE DIRECTION and this docstring has been rewritten twice
because of it. It began as the selected microphone back to a phone (item 23
spec C); A3 added the far end onto the merged bus; B7 added the two legs that
carry the whole thing out to a Bluetooth headset the panel is itself the
gateway for. Four forwarders and three states now, which is why the file is
named for the CALL rather than for the microphone.

WHY THIS IS A SUPERVISOR AND NOT AN alsaloop UNIT LIKE EVERY OTHER LEG. The
other legs address a PCM that exists whenever its card is plugged in, so
`BindsTo=` a udev device alias is enough to start and stop them. An HFP SCO PCM
is not like that: it comes and goes with the CALL, it is named after the
phone's Bluetooth address, and BlueZ has no device node for it. There is nothing
for systemd to bind to. So this process watches BlueALSA's own D-Bus objects and
runs exactly one alsaloop for exactly as long as a call is up.

AND "A CALL IS UP" IS A PROPERTY, NOT THE PRESENCE OF AN OBJECT. That is the
one thing this file got wrong for the whole of its first life. BlueALSA v3
published an SCO PCM only while a call was running, so the object WAS the
answer; v4 publishes the objects for every supported profile the moment the
device connects, and `org.bluealsa.PCM1.Running` is what now separates
'connected' from 'on a call'. Starting a forwarder against an unacquired
transport does not fail cleanly -- it overruns the capture, dies, and gets
restarted forever. See `sink_is_running`.

WHAT THE PANEL IS IN THIS LINK. `hfp-hf` -- the panel is the phone's Hands-Free
unit, i.e. the headset. Item 23 A: "Bluetooth connects as headset mic input and
as speaker output". The panel's microphone is therefore what the far end of the
call hears, and `bluealsa:DEV=<addr>,PROFILE=sco` PLAYBACK is where it goes.

AND `hfp-ag` TOWARD A HEADSET IT RESOLVED (B7). The same adapter holds both
roles, because Bluetooth negotiates per peer: the laptop connects to the
panel's hands-free unit and the headset connects to the panel's gateway. That
is what makes a BRIDGE possible -- the panel on a call AND the Headset switch
position resolving to a Bluetooth headset -- and it is the only arrangement in
which this panel's own mute and echo canceller sit inside somebody's call.
Two simultaneous eSCO links were measured on this adapter 2026-09-19.

WHAT IT CARRIES, AND WHERE EACH LEG GOES:

  1. the gateway's far end        -> `bus`      (behind WALL_BT_SCO_PLAYBACK)
  2. `mic_selected`               -> the gateway
  3. `bus_monitor`                -> the headset's gateway-role SCO sink
  4. the headset's own microphone -> `btmic_in`, which `mic_bt` snoops

Legs 1 and 2 are a CALL; all four are a BRIDGE. The far end lands on the bus
rather than on an output so that it follows the Mute/Headset/Speaker switch
like every other bus source -- which is also why the bridge depends on that
knob: with it off, leg 3 would carry a bus with no far end on it.

ACCEPTED, AND ITEM 23 REVIEW FINDING 4 SAYS SO: while a call is up the link is
HFP, which is mono at 8 or 16 kHz, and A2DP is suspended. "Bluetooth speaker"
and "Bluetooth mic" are the same radio link at different times, not both at
once. The Owner accepted that on 2026-09-13.

Contract:
  Inputs:  the BlueALSA D-Bus object tree (read with busctl); the switch state;
           the applier's published headset resolution and microphone selection
  Outputs: up to four alsaloop children; /run/wall-panel/call-state.json (0644,
           aliases only) and the bridge marker beside it
  Config:  WALL_BT_MIC_POLL_SECONDS, WALL_BT_SCO_PLAYBACK
  Raises:  nothing; every failure is a journal line, an answer of `idle`, and
           another poll
Implements: SR-029, SR-048, LLR-017, LLR-021
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import wall_audio_state as policy
except ImportError:  # pragma: no cover - firstboot installs the pair together
    policy = None

BUSCTL = "/usr/bin/busctl"
# The switch state, read on EVERY poll. See mic_allowed.
STATE_FILE = "/etc/wall-panel/audio-state.json"
ALSALOOP = "/usr/bin/alsaloop"
GUARD = "/usr/local/lib/wall-panel/wall-alsaloop-guard.py"
# The single named alias every mic consumer opens; the AEC step replaces what it
# resolves to and nothing here changes. See asound-bus-mode.conf.
MIC_PCM = "mic_selected"
# WHERE THE FAR END LANDS. The merged bus, so a call is a bus source like any
# other and follows the switch. See voice_argv.
BUS_PCM = "bus"
MODE_FILE = "/etc/wall-panel/audio-mode"
# A3's knob. The far-end leg stays OFF unless this is set, because it puts a
# new writer on the bus that carries the room's music and the Owner asked for
# that to be a decision rather than a side effect of a deploy.
SCO_PLAYBACK_VAR = "WALL_BT_SCO_PLAYBACK"
# Aliases only ever leave this process; the address is what it works in.
BLUETOOTH_STATE = "/run/wall-bluetooth/state.json"
CALL_STATE = "/run/wall-panel/call-state.json"

# ── B7, the bridge ──────────────────────────────────────────────────────────
# WHAT THE HEADSET POSITION RESOLVED TO, RE-READ ON EVERY POLL. The unit also
# takes this file as an EnvironmentFile, and that is NOT the same thing: an
# EnvironmentFile is read once, when the unit starts, and the resolution moves
# whenever a headset connects or walks away. A supervisor that trusted its own
# start-time environment would bridge a call to a headset that left the room
# twenty minutes ago.
HEADSET_ENV = "/run/wall-panel/audio-headset.env"
# THE SAME FILE, FOR THE SAME REASON, FOR THE MICROPHONE. `mic_selected`
# resolves through `@func getenv` in the process that OPENS it -- which is this
# supervisor's alsaloop child, inheriting this process's environment. The
# applier restarts the MIC legs when the source changes and only `start`s this
# unit, so a source change while the supervisor is alive would otherwise leave
# the gateway listening to whichever microphone was selected when the unit
# started. Read per poll, passed to the child, and a change restarts the leg.
MIC_SOURCE_ENV = "/run/wall-panel/audio-mic-source.env"
MIC_SOURCE_VAR = "WALL_AUDIO_MIC_SOURCE"
# Where the bridge puts the headset's microphone: the writing side of the
# loopback `mic_bt` snoops. See asound-bus-mode.conf.
BT_MIC_IN_PCM = "btmic_in"
# The A2DP leg the applier runs to the same headset. A2DP and SCO are exclusive
# on one peer, so the bridge must take it down for the duration; the unit's own
# ExecCondition refuses to start while this process says `bridge`, which is what
# keeps an apply during a call from fighting this one.
BT_HEADSET_UNIT = "wall-bus-bt-headset.service"
SYSTEMCTL = "/usr/bin/systemctl"
# The same fact as `call-state.json`'s `bridge`, in the one shape a unit file
# can test without quoting a JSON fragment through systemd's parser AND sh's.
# Written before the A2DP leg is stopped and removed after it is started, so
# the unit's ExecCondition is never the last to know.
BRIDGE_MARKER = "/run/wall-panel/call-bridge"
# THERE IS NO SILENCE PUMP, AND THERE WAS ONE FOR AN AFTERNOON. It existed on
# the belief that a loopback capture with no writer BLOCKS rather than
# returning silence, which would have made the mic leg reading `mic_bt` an
# xrun storm whenever the Headset position resolved to Bluetooth outside a
# call. MEASURED ON THE PANEL 2026-09-19 and it is not true here: with nothing
# writing, `mic_bt` delivered ten seconds of exact zeros, an `alsaloop` in the
# real leg's shape ran twelve seconds with no error of any kind, and a reader
# held across a writer ARRIVING and then LEAVING -- which is a call starting
# and ending -- survived both transitions with an empty log. snd-aloop's
# capture side free-runs on its own timer.
#
# So the pump is gone, along with the coordination it needed: it and the
# bridge's leg 4 would have been two writers on one loopback substream, and
# the supervisor had to stop one before starting the other. What it was there
# to guarantee is simply true without it. Do not re-add it without repeating
# those three measurements, because a component whose stated reason is false
# is worse than no component.

# /org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/hfphf/sink -- the object path
# BlueALSA publishes for the playback half of an HFP link the panel is the
# HANDS-FREE unit of. `source` is the other half (the far end's voice) and is
# deliberately not used here; that direction is A3's.
#
# THE PROFILE SEGMENT IS `hfphf`, NOT `sco`, AND THIS SHIPPED MATCHING `sco`
# (measured 2026-09-19). BlueALSA v3 named the middle segment after the
# TRANSPORT; v4 -- v4.1.1 is what the panel runs -- names it after the PROFILE,
# so a real tree reads:
#
#   /org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsnk/source
#   /org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/hfphf/sink
#   /org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/hfphf/source
#
# The old pattern matched none of it, so the supervisor never started a loop --
# and "no SCO sink" is its deliberate quiet answer, so it never said so either.
# `hciconfig` is the proof it had been silent all along: RX sco 239690, TX sco
# 0. The panel had received call audio and never sent one byte of microphone.
# The test could not catch it because the fixture was an INVENTED `sco/sink`
# string rather than a captured tree; the fixture is now a real one.
#
# `sco` is still accepted so a v3 BlueALSA is not broken by the fix. `hfpag` is
# NOT: on an AG link the sink is what the panel plays TO a headset, and sending
# the panel's microphone there would be a different leg pointed the wrong way.
# Goal 2 adds it deliberately or not at all.
#
# The PCM NAME is unaffected and was verified on the panel rather than assumed:
# `bluealsa:DEV=<addr>,PROFILE=sco` still resolves, and the plugin logs that it
# opened `.../hfphf/sink`. v4 accepts only `a2dp` and `sco` there.
SCO_SINK = re.compile(
    r"/org/bluealsa/(?P<hci>hci\d+)/dev_(?P<dev>[0-9A-Fa-f_]{17})/(?:sco|hfphf)/sink\b")

# THE OTHER ROLE'S OBJECTS (B7). `hfpag` is the panel as hands-free GATEWAY --
# what it is toward a headset it selected -- and it is the exact mirror of the
# pair above: `sink` is what the panel PLAYS to the headset, `source` is the
# headset's microphone coming back. The comment above says `hfpag` is "NOT"
# accepted, and that remains true of SCO_SINK: sending the panel's microphone
# to a headset would be a leg pointed the wrong way. These are the legs Goal 2
# adds deliberately, which is what that sentence was holding the door open for.
#
# Only the SINK path is matched and the source is derived from it, because the
# two are published together for one device and one regex is one thing to keep
# right. BlueALSA has no v3 spelling to be compatible with here: the AG role
# was never enabled on this panel before B1.
AG_SINK = re.compile(
    r"/org/bluealsa/(?P<hci>hci\d+)/dev_(?P<dev>[0-9A-Fa-f_]{17})/hfpag/sink\b")

DEFAULT_POLL_SECONDS = 5.0
# A poll that is cheap enough to be frequent and slow enough not to matter: the
# cost is one busctl call. Bounded at both ends so a typo in wall.env cannot
# turn this into a spin loop or into a leg that takes a minute to notice a call.
POLL_MIN, POLL_MAX = 1.0, 60.0


def log(message):
    """One line to the journal. stderr, so systemd stamps the unit identity."""
    sys.stderr.write("%s\n" % message)
    sys.stderr.flush()


def sco_object_paths(text):
    """Every HF-role SCO playback OBJECT in a `busctl tree` dump, path and all.

    Paths rather than addresses, because the caller has to ask each one a
    second question -- see `read_sinks`. Sorted for the same reason
    `parse_sco_sinks` sorts: the choice below must not change between polls.
    """
    found = {}
    for match in SCO_SINK.finditer(text or ""):
        found[match.group(0)] = match.group("dev").replace("_", ":").upper()
    return sorted(found.items())


def parse_sco_sinks(text):
    """Every HFP SCO playback endpoint in a `busctl tree` dump, as addresses.

    Pure, so the whole of "is there a call up" is testable without Bluetooth.
    Returns a sorted list of `AA:BB:CC:DD:EE:FF` strings; sorted because the
    choice below has to be deterministic when two phones are somehow connected,
    and an arbitrary one would make this leg pick a different device on
    different polls.
    """
    found = set()
    for match in SCO_SINK.finditer(text or ""):
        found.add(match.group("dev").replace("_", ":").upper())
    return sorted(found)


def ag_object_paths(text):
    """Every AG-role SCO playback OBJECT in a `busctl tree` dump, path and all.

    The mirror of `sco_object_paths`, and separate from it rather than a
    parameter, because the two answer different questions and a caller that
    mixed them up would bridge a call into the gateway it came from.
    """
    found = {}
    for match in AG_SINK.finditer(text or ""):
        found[match.group(0)] = match.group("dev").replace("_", ":").upper()
    return sorted(found.items())


def pcm_name(address):
    """The BlueALSA PCM for one device's SCO playback."""
    return "bluealsa:DEV=%s,PROFILE=sco" % address


def read_env_file(path, key):
    """One value out of a systemd EnvironmentFile, or None. Never raises.

    A deliberately small parser: these files are written by `write_atomic` in
    `wall-audio-output`, one `KEY=value` per line and nothing else -- no
    quoting, no continuations, no comments. Anything more would be a second,
    less honest copy of systemd's parser.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                name, sep, value = line.partition("=")
                if sep and name.strip() == key:
                    value = value.strip()
                    return value or None
    except OSError:
        return None
    return None


def headset_resolution(path=HEADSET_ENV):
    """(via, address) for what the Headset position resolves to right now.

    `via` is `bluetooth`, `usb` or None, exactly as `wall_audio_state.headset_via`
    answered it on the applier's last pass; the address is only ever present
    with `bluetooth`.

    THE POLICY IS NOT RE-DERIVED HERE and must not be. The Owner's priority
    (Bluetooth over USB, always) has one spelling, in `wall_audio_state`, and
    this process reads the answer the applier published rather than asking BlueZ
    the same question a second way. Two spellings of that rule is how the room's
    music ends up in one headset and somebody's voice in another.

    Missing or unreadable answers None, which is "no bridge is possible" -- the
    same direction everything else in this file fails in.
    """
    via = read_env_file(path, "WALL_AUDIO_HEADSET_VIA")
    if via not in ("bluetooth", "usb"):
        return None, None
    if via != "bluetooth":
        return via, None
    address = read_env_file(path, "WALL_AUDIO_HEADSET_BT_DEV")
    if not address:
        # Resolved to Bluetooth with no address is a contradiction the applier
        # cannot write -- `headset_via` answers bluetooth only from a presence
        # that came with one -- so treat it as nothing rather than guessing.
        return None, None
    return via, address.upper()


def mic_source_name(path=MIC_SOURCE_ENV):
    """Which capture PCM `mic_selected` should resolve to, or None.

    Read per poll and handed to the child's environment; see MIC_SOURCE_ENV.
    """
    return read_env_file(path, MIC_SOURCE_VAR)


def poll_seconds(raw=None):
    """The poll interval, held inside POLL_MIN..POLL_MAX.

    Clamped rather than refused: this daemon has no operator standing in front
    of it, and a leg that will not start because a number in a config file is
    silly is worse than a leg that polls at a sensible rate instead.
    """
    if raw is None:
        raw = os.environ.get("WALL_BT_MIC_POLL_SECONDS")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_POLL_SECONDS
    if value != value:
        return DEFAULT_POLL_SECONDS
    return max(POLL_MIN, min(POLL_MAX, value))


def bus_mode_active(path=MODE_FILE):
    """Whether the panel is in bus mode. Anything unreadable answers False.

    The same ExecCondition every other leg carries, made a runtime check because
    this one is a long-lived process: `wall-audio-mode trigger` is the Owner's
    one-command rollback, and a supervisor that kept a microphone open across it
    would be the single thing in the graph that the rollback did not roll back.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip() == "bus"
    except OSError:
        return False


def mic_allowed(path=STATE_FILE):
    """Whether the switch's own state authorises a live microphone RIGHT NOW.

    THIS IS THE FIX FOR A REAL FAIL-OPEN, FOUND BY REVIEW (terra, 2026-09-14).
    Before it, this supervisor knew only that it had been started, and it is
    `Restart=always`. An operator restart, a `daemon-reload` workflow, a crash,
    or a `systemctl stop` that failed during an apply would each have reopened
    the microphone into a live call while `audio-state.json` said `input_muted`.
    A privacy control cannot be a command somebody once sent; it has to be a
    fact this process re-checks, which is what this does on every poll.

    The policy itself is NOT duplicated here. `wall_audio_state.mic_live` is the
    single definition of when a microphone may run -- the input mute of ruling E
    and the "nothing tunnelled" of ruling 7 -- and firstboot installs that module
    into this script's own directory precisely so both halves can share it.

    EVERY FAILURE ANSWERS FALSE. An unreadable state file, damaged JSON, a
    missing policy module: all of them mean "do not open a microphone". That is
    the opposite of the direction the rest of this file fails in, and
    deliberately: a leg that does not run is a degraded panel, while a leg that
    runs against a mute is the failure nobody in the room can see.
    """
    if policy is None:
        return False
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return False
    try:
        return bool(policy.mic_live(policy.normalize(raw)))
    except Exception:  # noqa: BLE001 - a policy that raises must not open a mic
        return False


def loop_argv(address, rate=None):
    """The microphone leg: the selected mic to the gateway. `rate` is ignored.

    THE RATE IS A CONSTANT HERE ON PURPOSE, and it is the one thing in this
    file that looks like a bug and is not. The link is usually CVSD at 8 kHz
    while this asks for 16 kHz; measured on the panel with the transport
    acquired, 16000 and 8000 carried 3995 and 4000 bytes in twelve seconds.
    The playback side is a RAW BlueALSA PCM and its plugin converts. The
    parameter exists only so both legs share one call shape.
    """
    return [GUARD, "--max-errors", "20", "--window", "10", "--",
            ALSALOOP,
            "--cdevice", MIC_PCM,
            "--pdevice", pcm_name(address),
            "--format", "S16_LE", "--rate", "16000", "--channels", "1",
            "--tlatency", "50000", "--sync", "5"]


def voice_argv(address, rate):
    """The far-end leg: the gateway's voice onto the merged bus. A3.

    WHY THE RATE IS NOT A CONSTANT HERE, when it is for the microphone. This
    leg's CAPTURE device is the raw BlueALSA PCM, and a capture device must be
    opened at its own rate: wrapping it in `plug` -- which works on the
    playback side -- fails with `Poll FD initialization failed`, because plug
    does not give alsaloop the poll descriptors it needs from a capture. So
    the negotiated rate is read from the PCM (`pcm_sampling`) and passed. Mono
    for the same reason; `bus` accepts the conversion, the SCO PCM does not.

    IT LANDS ON `bus`, NOT ON AN OUTPUT. That is the whole point: the far end
    becomes another bus source, so it follows the Mute/Headset/Speaker switch
    like the library, Pandora and the dev PC, the visualizer sees it, and in
    the Speaker position the echo canceller removes it from the microphone
    before it goes back up the link. Owner ruling 2026-09-19, §17 Q2.
    """
    return [GUARD, "--max-errors", "20", "--window", "10", "--",
            ALSALOOP,
            "--cdevice", pcm_name(address),
            "--pdevice", BUS_PCM,
            "--format", "S16_LE", "--rate", str(int(rate)), "--channels", "1",
            "--tlatency", "50000", "--sync", "5"]


def headset_voice_argv(address, rate=None):
    """Bridge leg 3: the merged bus to the Bluetooth headset's SCO sink.

    THIS REPLACES THE HEADSET'S A2DP LEG FOR THE DURATION, and it is not a
    choice: A2DP and SCO are exclusive on one peer, so while the panel holds
    the AG link the headset hears the whole bus at 8 or 16 kHz mono. Library
    music in the background of a call is CVSD-quality; that is the profile,
    not a defect, and it is why the bridge exists only while a call is up.

    `rate` is accepted and IGNORED, exactly as it is on the microphone leg
    toward a gateway, and for the same measured reason: the playback side is a
    raw BlueALSA PCM whose plugin converts, so a fixed 16000 carries against an
    8 kHz CVSD link. The parameter is there so every leg shares one call shape.
    """
    return [GUARD, "--max-errors", "20", "--window", "10", "--",
            ALSALOOP,
            "--cdevice", "bus_monitor",
            "--pdevice", pcm_name(address),
            "--format", "S16_LE", "--rate", "16000", "--channels", "1",
            "--tlatency", "50000", "--sync", "5"]


def headset_mic_argv(address, rate):
    """Bridge leg 4: the Bluetooth headset's microphone into `mic_bt`.

    The rate is READ AND PASSED for the reason `voice_argv` documents: this
    leg's capture device is a raw BlueALSA PCM, and `plug` over a capture fails
    with `Poll FD initialization failed`. The destination is the loopback
    writing side, which IS wrapped in `plug`, so the conversion happens where
    it is allowed to.

    Nothing downstream learns that a radio is involved: the mic legs open
    `mic_selected`, which the applier has already resolved to `mic_bt`.
    """
    return [GUARD, "--max-errors", "20", "--window", "10", "--",
            ALSALOOP,
            "--cdevice", pcm_name(address),
            "--pdevice", BT_MIC_IN_PCM,
            "--format", "S16_LE", "--rate", str(int(rate)), "--channels", "1",
            "--tlatency", "50000", "--sync", "5"]


def parse_running(text):
    """`busctl get-property ... Running` as a bool. Anything else is False.

    The property is a plain variant, so the answer is the literal line `b true`
    or `b false`. Unparseable is False for the reason everything in this file
    fails toward False: the failure that matters is a microphone that stays
    open.
    """
    return (text or "").strip() == "b true"


def sink_is_running(path, run=subprocess.run):
    """Whether this SCO PCM has its transport ACQUIRED -- i.e. a call is up.

    THE OBJECT EXISTING IS NOT A CALL, AND ASSUMING IT WAS IS WHY THIS LEG
    FLAPPED (measured 2026-09-19). Under BlueALSA v3 an SCO PCM appeared only
    for the duration of a call, which is what this file's docstring still says
    and what `read_sinks` was built on. Under v4 -- v4.1.1 is what the panel
    runs -- the PCM objects for every supported profile are published as soon
    as the device CONNECTS, and `Running` is what separates the two.
    
    Measured, with the panel as the hands-free unit and the dev PC as gateway:
    merely connected gives `hcitool con` with an ACL and no eSCO, `Running
    false`, and an alsaloop started against it dies in seconds with `overrun
    for capture mic_selected` then `Poll FD initialization failed` -- so the
    supervisor restarted it, forever, while `TX sco` stayed 0. With the gateway
    holding its hands-free microphone open there is an eSCO link, `Running`
    reads true, and the SAME alsaloop argv carries audio: `TX sco` 0 -> 4999 in
    fifteen seconds, the first microphone bytes this panel has ever sent.
    
    THE RATE WAS NEVER THE FAULT, which is worth writing down because it looks
    like it should have been. The link negotiates CVSD at 8 kHz and the leg
    asks for 16 kHz; with SCO up, 16000 and 8000 carry identically (3995 and
    4000 bytes in twelve seconds). ALSA converts. Do not "fix" the rate.
    """
    try:
        done = run([BUSCTL, "--system", "get-property", "org.bluealsa", path,
                    "org.bluealsa.PCM1", "Running"],
                   check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log("bluealsa PCM %s unreadable (%s): treated as no call" % (path, exc))
        return False
    if done.returncode != 0:
        return False
    return parse_running(done.stdout)


def parse_sampling(text):
    """`busctl get-property ... Sampling` as an int, or None.

    The property is a `u`, so the answer is the literal line `u 8000`. None for
    anything else, and the caller then leaves the leg alone rather than opening
    a PCM at a rate nobody confirmed.
    """
    parts = (text or "").split()
    if len(parts) != 2 or parts[0] != "u" or not parts[1].isdigit():
        return None
    value = int(parts[1])
    # CVSD is 8000 and mSBC is 16000; nothing else is an HFP rate, and a number
    # outside that set means this property is not what it is assumed to be.
    return value if value in (8000, 16000) else None


def pcm_sampling(path, run=subprocess.run):
    """The rate the SCO transport actually negotiated, or None.

    THE FAR-END LEG CANNOT GUESS THIS, and unlike the microphone leg it cannot
    be given a constant either. Measured on the panel: the CAPTURE side of an
    alsaloop must be the RAW BlueALSA PCM at its own rate. Wrapping it in
    `plug` -- which works perfectly on the playback side -- fails with `Poll FD
    initialization failed`, because the plug plugin does not expose the poll
    descriptors alsaloop needs from a capture device. So the rate has to be
    right, and CVSD (8000) and mSBC (16000) are chosen per call.
    """
    try:
        done = run([BUSCTL, "--system", "get-property", "org.bluealsa", path,
                    "org.bluealsa.PCM1", "Sampling"],
                   check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log("bluealsa PCM %s rate unreadable (%s)" % (path, exc))
        return None
    if done.returncode != 0:
        return None
    return parse_sampling(done.stdout)


def read_sinks(run=subprocess.run):
    """Every address whose SCO sink is RUNNING. [] on any failure, never raises.

    A BlueALSA that is not running, a busctl that is missing, a D-Bus that is
    slow: all of them mean "no call is up", which is the safe answer, because
    the failure direction that matters is a microphone that stays open, not one
    that does not.

    TWO QUESTIONS, NOT ONE, and the second is the one that was missing. The
    tree says which SCO sinks EXIST; `sink_is_running` says which of them have
    a transport. See `sink_is_running` for what it cost to leave that out. One
    extra busctl call per candidate, and there is normally either no candidate
    or one.
    """
    try:
        done = run([BUSCTL, "--system", "tree", "org.bluealsa"],
                   check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log("bluealsa object tree unreadable (%s): no HFP mic return this poll" % exc)
        return []
    if done.returncode != 0:
        return []
    return sorted(read_calls(run=run))


def read_calls(run=subprocess.run):
    """{address: negotiated rate} for every gateway actually on a call.

    The rate is carried alongside because the far-end leg needs it and asking
    for it here costs nothing: the object has already been found and already
    answered one property.

    A PCM that is running but will not say its rate is DROPPED rather than
    guessed. Losing the microphone for a poll is recoverable; opening a PCM at
    a rate nobody confirmed is how the leg flapped in the first place.
    """
    try:
        done = run([BUSCTL, "--system", "tree", "org.bluealsa"],
                   check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log("bluealsa object tree unreadable (%s): no HFP mic return this poll" % exc)
        return {}
    if done.returncode != 0:
        return {}
    calls = {}
    for path, address in sco_object_paths(done.stdout):
        if not sink_is_running(path, run=run):
            continue
        rate = pcm_sampling(path, run=run)
        if rate is not None:
            calls[address] = rate
    return calls


def ag_link_rate(address, run=subprocess.run):
    """The negotiated rate of the AG SCO link to `address`, or None (B7).

    `Running` IS NOT ASKED HERE, and that is the difference from `read_calls`.
    On the HF side the panel is waiting for a gateway to raise SCO, so Running
    is the only honest answer to "is there a call". On the AG side the panel is
    the gateway: the link comes up because THIS process opens the PCM, so
    waiting for it to be running first would be waiting for something only this
    process can cause. What is asked instead is that the object exists -- the
    headset is connected and offers the profile -- and at what rate it would
    open.

    None means "do not bridge to this headset", which leaves the ordinary
    call state and the far end on the room's speakers. Same direction as
    everything else here.
    """
    try:
        done = run([BUSCTL, "--system", "tree", "org.bluealsa"],
                   check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log("bluealsa object tree unreadable (%s): no bridge this poll" % exc)
        return None
    if done.returncode != 0:
        return None
    for path, found in ag_object_paths(done.stdout):
        if found == str(address).upper():
            return pcm_sampling(path, run=run)
    return None


class Leg:
    """At most one alsaloop child, started and stopped by address.

    TWO INSTANCES NOW, NOT ONE, and they are deliberately independent. The
    microphone leg carries the panel to the gateway; the voice leg carries the
    gateway's far end to the merged bus. They start and stop on DIFFERENT
    conditions -- muting the microphone must not silence the person you are
    listening to, which is what a muted headset does -- so a single leg with
    two directions would have had to re-derive that distinction internally.

    `name` is only for the journal, and it is worth the field: two legs
    logging "started" with no way to tell which is the failure mode this file
    already had once, in a different costume.
    """

    def __init__(self, name, argv, popen=subprocess.Popen):
        self._popen = popen
        self._argv = argv
        self.name = name
        self.address = None
        self.child = None
        # WHAT `mic_selected` RESOLVED TO WHEN THIS CHILD WAS SPAWNED. ALSA
        # reads `@func getenv` in the process that OPENS the PCM, so a child
        # holds whatever it inherited for as long as it lives; `ensure` treats
        # a change here exactly as it treats a change of address, because both
        # mean this leg is carrying the wrong audio.
        self.mic_source = None

    def running(self):
        return self.child is not None and self.child.poll() is None

    def start(self, address, rate=None, mic_source=None):
        self.stop()
        environment = None
        if mic_source:
            environment = dict(os.environ)
            environment[MIC_SOURCE_VAR] = mic_source
        try:
            self.child = self._popen(self._argv(address, rate), env=environment)
        except OSError as exc:
            log("could not start the %s leg to %s: %s" % (self.name, address, exc))
            self.child = None
            return False
        self.address = address
        self.mic_source = mic_source
        log("%s leg started (%s%s)"
            % (self.name, pcm_name(address) if address else "no device",
               "" if not mic_source else ", mic %s" % mic_source))
        return True

    def ensure(self, address, rate=None, mic_source=None):
        """Start if it is not already on this address and alive. Idempotent,
        because the poll loop calls it every interval and a restart drops
        audio for as long as ALSA takes to reopen."""
        if (address != self.address or self.mic_source != mic_source
                or not self.running()):
            return self.start(address, rate, mic_source)
        return True

    def stop(self):
        if self.child is None:
            return
        address = self.address
        try:
            if self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # A forwarder wedged on an ALSA close must not keep the
                    # microphone open: there is nobody here to notice.
                    self.child.kill()
                    self.child.wait(timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            log("%s leg would not stop cleanly (%s)" % (self.name, exc))
        self.child = None
        self.address = None
        if address:
            log("%s leg stopped (%s)" % (self.name, address))


ROUTE_PATH = "/run/wall-bluetooth/route.json"


def preferred_input(path=ROUTE_PATH):
    """The device the Owner chose as the panel's Bluetooth mic, or None.

    WSN-024 gave `select_input` to the panel, and this is where that choice
    lands: the applier writes the route document and this supervisor reads it.
    It is a PREFERENCE and not a command -- the leg still only runs against a
    device whose SCO transport is actually ACQUIRED (`sink_is_running`) --
    because a selection that could point the leg at a phone that is not on a
    call would just stop the microphone working.

    Missing, unreadable or malformed all mean "no preference", which is the
    behaviour that shipped before the route document existed.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return None
    value = stored.get("input") if isinstance(stored, dict) else None
    return value if isinstance(value, str) and value else None


def decide(sinks, current, preferred=None):
    """Which address the leg should be on, given what exists. Pure.

    First by sorted order, so the answer does not change between polls for the
    same set of devices, and the CURRENT one is kept if it is still there --
    a call in progress is never moved to another phone because a second one
    happened to connect.

    THE PREFERENCE OUTRANKS THE CURRENT LEG, and only that. It is an explicit
    choice somebody made at the panel, so it must be able to MOVE a leg -- a
    preference that could only ever apply to the next call would look like it
    had been ignored. It cannot conjure one: a preferred device with no SCO
    sink is not in `sinks`, and the ordinary rules then decide, which keeps a
    working microphone working rather than silencing it in favour of a phone
    that is not on a call.
    """
    if not sinks:
        return None
    if preferred in sinks:
        return preferred
    if current in sinks:
        return current
    return sinks[0]


def sco_playback_enabled(env=None):
    """Whether A3's far-end leg is switched on. Default OFF.

    A new writer on the bus that carries the room's music is a decision, not
    something a deploy should turn on underneath a working panel. Same
    reasoning the knob shipped with; what changed is that there is now a leg
    behind it.
    """
    value = (os.environ if env is None else env).get(SCO_PLAYBACK_VAR, "")
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def alias_of(address, path=BLUETOOTH_STATE):
    """The observer's alias for an address, or None. NEVER the address.

    LLR-979: no hardware address reaches a published document. This process
    works in addresses because BlueALSA's PCM names are addresses; everything
    it PUBLISHES is an alias, and an alias it cannot find is `null` rather
    than a fallback that leaks the thing the rule exists to keep out.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return None
    table = stored.get("aliasesByAddress") if isinstance(stored, dict) else None
    if not isinstance(table, dict):
        return None
    value = table.get(address)
    return value if isinstance(value, str) and value else None


def _same_fact(left, right):
    """Two call documents describing the same situation. `revision` excluded,
    because it is the counter and would make every document differ from the
    one before it."""
    keys = set(left) | set(right)
    return all(left.get(key) == right.get(key) for key in keys if key != "revision")


def call_document(state, gateway_alias, since, mic, revision,
                  headset_alias=None):
    """The published fact, as §20a defines it. Pure, so the shape is testable.

    `headset` is the alias of the Bluetooth headset a `bridge` is carrying the
    call to, and None in every other state. An alias, never an address
    (LLR-979).
    """
    return {"state": state, "gateway": gateway_alias,
            "headset": headset_alias if state == "bridge" else None,
            "since": since, "mic": mic, "revision": revision}


def set_bridge_marker(present, path=BRIDGE_MARKER):
    """Create or remove the marker `wall-bus-bt-headset.service` tests.

    Every failure is a journal line and nothing else: a marker that cannot be
    written means the A2DP leg may be restarted under the bridge, which is an
    audible glitch, and taking the call down to avoid it would be worse.
    """
    try:
        target = pathlib.Path(path)
        if present:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
            os.chmod(target, 0o644)
        else:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
    except OSError as exc:
        log("could not %s the bridge marker (%s)"
            % ("write" if present else "remove", exc))


def _systemctl(action, unit, run=subprocess.run):
    """Best-effort `systemctl <action> <unit>`. Never raises, always logs.

    WHY THIS PROCESS TOUCHES A UNIT AT ALL. A2DP and SCO are exclusive on one
    peer, so the bridge has to take the headset's A2DP leg down for the
    duration and put it back afterwards, and this is the only thing that knows
    when a bridge begins and ends. The applier cannot: "bridging" is a runtime
    fact about a radio, not a state anybody stored.

    It is not a fight with the applier, because `wall-bus-bt-headset.service`
    carries an ExecCondition that refuses to start while this process says
    `bridge`. An apply during a call therefore no-ops on that unit instead of
    restarting it under a live SCO link.
    """
    try:
        done = run([SYSTEMCTL, action, unit], check=False,
                   capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        log("could not %s %s (%s)" % (action, unit, exc))
        return False
    if done.returncode != 0:
        log("%s %s returned %d" % (action, unit, done.returncode))
        return False
    return True


def touch_call_state(path=CALL_STATE):
    """Mark the call document fresh without rewriting it.

    The broker measures this file's age to decide whether to believe it, and
    the supervisor writes it only on a CHANGE -- so the heartbeat is a
    timestamp and not a rewrite. Every failure is silent, for the same reason
    `publish_call_state`'s is: a status line is not worth a call.
    """
    try:
        os.utime(path, None)
    except OSError:
        pass


def publish_call_state(document, path=CALL_STATE):
    """Write /run/wall-panel/call-state.json atomically, 0644.

    EVERY FAILURE IS SILENT AND THE CALL STILL WORKS. This file is an
    observation for the glass; a panel that cannot write it is a panel with a
    missing status line, and taking the audio down to report that would be the
    diagnostic failing the thing it reports on.
    """
    try:
        target = pathlib.Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".new")
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except OSError as exc:
        log("could not publish the call state (%s)" % exc)


def main(argv=None):
    interval = poll_seconds()
    mic_leg = Leg("HFP mic return", loop_argv)
    voice_leg = Leg("HFP far-end", voice_argv)
    # ── the bridge's own three children (B7) ───────────────────────────────
    # Leg 3 and leg 4 of §20b, plus the silence pump that keeps `mic_bt` from
    # blocking its readers when leg 4 is not running. All three address the
    # HEADSET; the two above address the GATEWAY.
    headset_voice_leg = Leg("bridge to headset", headset_voice_argv)
    headset_mic_leg = Leg("bridge headset mic", headset_mic_argv)
    stopping = {"now": False}
    published = {"document": None, "since": None, "revision": 0}
    # Whether THIS process has taken the headset's A2DP leg down. Held rather
    # than re-derived, so the unit is stopped and started exactly once per
    # bridge instead of on every poll.
    bridging = {"now": False}

    def handle(signum, frame):  # noqa: ARG001 - the signal API's shape
        stopping["now"] = True

    def stop_bridge_legs():
        """End the bridge and give the headset its A2DP leg back."""
        headset_voice_leg.stop()
        headset_mic_leg.stop()
        if bridging["now"]:
            bridging["now"] = False
            # The unit's ExecCondition reads `call-state.json`, so the state
            # has to have stopped saying `bridge` before this start is tried.
            publish_call_state(call_document(
                "call", published["document"].get("gateway")
                if published["document"] else None,
                published["since"], None, published["revision"]))
            set_bridge_marker(False)
            _systemctl("start", BT_HEADSET_UNIT)

    def announce(state, address, mic, headset_alias=None):
        """Publish only when something CHANGED, so the revision counts events
        rather than polls and the journal does not fill with sameness."""
        since = published["since"]
        if state == "idle":
            since = None
        elif since is None:
            since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        document = call_document(state, alias_of(address) if address else None,
                                 since, mic, published["revision"],
                                 headset_alias=headset_alias)
        # The revision counts EVENTS, not polls, so a call that stays up does
        # not churn a file the broker re-reads on a 30 s staleness rule.
        if published["document"] is not None and _same_fact(document, published["document"]):
            # THE FACT HAS NOT CHANGED AND THE FILE STILL HAS TO BE FRESH
            # (terra, 2026-09-19, finding 3). The broker rejects this document
            # once its mtime is older than its staleness bound, so a call that
            # simply CONTINUES -- which is what a call mostly does -- would be
            # reported as idle within half a minute while both legs were still
            # running. The revision still counts EVENTS, so the broker's
            # readers see no churn; only the timestamp moves.
            if state != "idle":
                touch_call_state()
            return
        published["revision"] += 1
        document["revision"] = published["revision"]
        published["since"] = since
        published["document"] = document
        publish_call_state(document)

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    log("HFP call supervisor watching for calls every %.1f s" % interval)
    try:
        while not stopping["now"]:
            # WHAT HEADSET MEANS RIGHT NOW, READ BEFORE ANYTHING ELSE. Both the
            # silence pump and the bridge turn on it, and it moves whenever a
            # headset connects or walks away.
            via, headset_address = headset_resolution()
            source = mic_source_name()
            if not bus_mode_active():
                mic_leg.stop(); voice_leg.stop()
                stop_bridge_legs()
                announce("idle", None, None)
            else:
                calls = read_calls()
                wanted = decide(sorted(calls), mic_leg.address or voice_leg.address,
                                preferred_input())
                # A bridge needs a call, a Bluetooth headset behind the Headset
                # position, and an AG SCO object for it. Anything missing and
                # this is an ordinary call: the far end goes to the bus and out
                # of whatever the switch points at.
                bridge_rate = None
                if wanted is not None and via == "bluetooth" and headset_address:
                    bridge_rate = ag_link_rate(headset_address)
                if wanted is None:
                    mic_leg.stop(); voice_leg.stop(); stop_bridge_legs()
                    announce("idle", None, None)
                elif bridge_rate is not None:
                    # ── BRIDGE ──────────────────────────────────────────────
                    # The A2DP leg goes first and the state file says `bridge`
                    # before it does, so the unit's own ExecCondition holds it
                    # down against an apply that arrives mid-call.
                    if not bridging["now"]:
                        bridging["now"] = True
                        announce("bridge", wanted, None,
                                 headset_alias=alias_of(headset_address))
                        set_bridge_marker(True)
                        _systemctl("stop", BT_HEADSET_UNIT)
                    # THE HEADSET'S MICROPHONE OBEYS THE MUTE TOO (terra,
                    # 2026-09-19, finding 1). It shipped outside the gate for
                    # a moment on the reasoning that leg 2 is what SENDS -- but
                    # a microphone this panel has opened against a mute is the
                    # failure nobody in the room can see, whatever happens to
                    # the samples afterwards, and `wall-mic-rear` reads the
                    # same loopback. Muted, the pump takes the loopback back so
                    # nothing downstream stalls on a writer that left.
                    if mic_allowed():
                        headset_mic_leg.ensure(headset_address, bridge_rate)
                    else:
                        headset_mic_leg.stop()
                    # The far end reaches the headset through the BUS, exactly
                    # as it reaches the room's speakers: `voice_leg` puts it on
                    # the bus and leg 3 carries the bus to the headset. So this
                    # one is still behind WALL_BT_SCO_PLAYBACK -- without it the
                    # bus has no far end on it and leg 3 would carry silence.
                    if sco_playback_enabled():
                        voice_leg.ensure(wanted, calls[wanted])
                        headset_voice_leg.ensure(headset_address)
                    else:
                        voice_leg.stop(); headset_voice_leg.stop()
                    if mic_allowed():
                        mic_leg.ensure(wanted, mic_source=source)
                    else:
                        mic_leg.stop()
                    announce("bridge", wanted,
                             (source or MIC_PCM) if mic_leg.running() else None,
                             headset_alias=alias_of(headset_address))
                else:
                    # ── ORDINARY CALL ───────────────────────────────────────
                    stop_bridge_legs()
                    # THE FAR END FIRST, AND INDEPENDENTLY OF THE MUTE. Muting
                    # the panel stops what it SENDS; it does not stop the other
                    # person being heard, which is what a muted headset does
                    # (WSN-027, §20a).
                    if sco_playback_enabled():
                        voice_leg.ensure(wanted, calls[wanted])
                    else:
                        voice_leg.stop()
                    if mic_allowed():
                        mic_leg.ensure(wanted, mic_source=source)
                    else:
                        mic_leg.stop()
                    announce("call", wanted,
                             (source or MIC_PCM) if mic_leg.running() else None)
            # Sleep in short slices so SIGTERM is answered promptly rather than
            # after a whole poll interval: systemd's stop timeout is not long.
            #
            # AND SO THE MUTE IS. The microphone used to be stopped by the
            # APPLIER stopping this whole unit, which was immediate; now that
            # the unit also carries the far end it must survive a mute, so the
            # mute is enforced in here instead (see CALL_LEGS in
            # wall_audio_state). Re-checked on every tick rather than once per
            # poll, because "no sample leaves the panel" (WSN-027, Owner
            # ruling E) is not a promise that can be kept up to five seconds
            # late. Discovery stays on the slow interval -- that is the part
            # that costs a busctl call; this is one small read of a file the
            # applier has already written.
            #
            # THE ASYMMETRY IS DELIBERATE AND WAS MEASURED: muting stops the
            # leg within a tick, un-muting brings it back on the next POLL, so
            # up to the interval later. Stopping fast and resuming slowly is
            # the right way round for a microphone -- the failure that matters
            # is one that stays open -- and resuming is not a promise anyone
            # made. Do not "fix" this by starting the leg from the tick: that
            # would put a leg start inside the loop that exists to answer
            # SIGTERM promptly.
            waited = 0.0
            while waited < interval and not stopping["now"]:
                time.sleep(0.25)
                waited += 0.25
                if (mic_leg.running() or headset_mic_leg.running()) and not mic_allowed():
                    mic_leg.stop()
                    # BOTH microphones, on the same tick. The bridge opens the
                    # headset's capture as well as the gateway-bound leg, and a
                    # mute that stopped one of them would be the cosmetic kind.
                    # The pump is NOT started from here: this loop exists to
                    # answer SIGTERM promptly and must not grow a leg start
                    # (see the paragraph above). The next poll starts it, which
                    # is the same asymmetry the microphone already has --
                    # stopping fast and resuming slowly is the right way round.
                    headset_mic_leg.stop()
                    # THE STATE SURVIVES THE MUTE. A muted headset is still on
                    # a call and still bridging; what stops is the microphone,
                    # and that is what the `mic: null` says.
                    announce("bridge" if bridging["now"] else "call",
                             voice_leg.address or mic_leg.address, None,
                             headset_alias=alias_of(headset_mic_leg.address)
                             if bridging["now"] else None)
    finally:
        mic_leg.stop()
        voice_leg.stop()
        # THE HEADSET'S A2DP LEG IS PUT BACK BEFORE THIS PROCESS GOES. Leaving
        # it stopped would leave a Bluetooth headset silent in the Headset
        # position after a `systemctl restart` of this unit, with nothing in
        # the journal to connect the two.
        headset_voice_leg.stop()
        headset_mic_leg.stop()
        # The last word is always "no call". A stale `call` left in /run would
        # have the chrome showing a handset for a process that is gone.
        publish_call_state(call_document("idle", None, None, None,
                                         published["revision"] + 1))
        set_bridge_marker(False)
        if bridging["now"]:
            bridging["now"] = False
            _systemctl("start", BT_HEADSET_UNIT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
