#!/usr/bin/env python3
"""The Bluetooth half of item 23 spec C: the selected mic back to the phone.

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

WHAT THIS DELIBERATELY DOES NOT DO. It does not carry the far end's voice back
to the bus. That direction is `bluealsa-aplay --profile-sco` and it is a change
to a unit that is carrying the room's music today, so it is a separate,
flagged knob (WALL_BT_SCO_PLAYBACK in wall.env) and not something this step
turns on underneath a working panel.

ACCEPTED, AND ITEM 23 REVIEW FINDING 4 SAYS SO: while a call is up the link is
HFP, which is mono at 8 or 16 kHz, and A2DP is suspended. "Bluetooth speaker"
and "Bluetooth mic" are the same radio link at different times, not both at
once. The Owner accepted that on 2026-09-13.

Contract:
  Inputs:  the BlueALSA D-Bus object tree (read with busctl), WALL_BT_MIC_*
  Outputs: at most one alsaloop child at a time
  Config:  WALL_BT_MIC_POLL_SECONDS, WALL_AUDIO_MIC_SOURCE (through ALSA)
  Raises:  nothing; every failure is a journal line and another poll
Implements: SR-029, LLR-017
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


def pcm_name(address):
    """The BlueALSA PCM for one device's SCO playback."""
    return "bluealsa:DEV=%s,PROFILE=sco" % address


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

    def running(self):
        return self.child is not None and self.child.poll() is None

    def start(self, address, rate=None):
        self.stop()
        try:
            self.child = self._popen(self._argv(address, rate))
        except OSError as exc:
            log("could not start the %s leg to %s: %s" % (self.name, address, exc))
            self.child = None
            return False
        self.address = address
        log("%s leg started (%s)" % (self.name, pcm_name(address)))
        return True

    def ensure(self, address, rate=None):
        """Start if it is not already on this address and alive. Idempotent,
        because the poll loop calls it every interval and a restart drops
        audio for as long as ALSA takes to reopen."""
        if address != self.address or not self.running():
            return self.start(address, rate)
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


def call_document(state, gateway_alias, since, mic, revision):
    """The published fact, as §20a defines it. Pure, so the shape is testable."""
    return {"state": state, "gateway": gateway_alias, "headset": None,
            "since": since, "mic": mic, "revision": revision}


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
    stopping = {"now": False}
    published = {"document": None, "since": None, "revision": 0}

    def handle(signum, frame):  # noqa: ARG001 - the signal API's shape
        stopping["now"] = True

    def announce(state, address, mic):
        """Publish only when something CHANGED, so the revision counts events
        rather than polls and the journal does not fill with sameness."""
        since = published["since"]
        if state == "idle":
            since = None
        elif since is None:
            since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        document = call_document(state, alias_of(address) if address else None,
                                 since, mic, published["revision"])
        # The revision counts EVENTS, not polls, so a call that stays up does
        # not churn a file the broker re-reads on a 30 s staleness rule.
        if published["document"] is not None and _same_fact(document, published["document"]):
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
            if not bus_mode_active():
                mic_leg.stop(); voice_leg.stop(); announce("idle", None, None)
            else:
                calls = read_calls()
                wanted = decide(sorted(calls), mic_leg.address or voice_leg.address,
                                preferred_input())
                if wanted is None:
                    mic_leg.stop(); voice_leg.stop(); announce("idle", None, None)
                else:
                    # THE FAR END FIRST, AND INDEPENDENTLY OF THE MUTE. Muting
                    # the panel stops what it SENDS; it does not stop the other
                    # person being heard, which is what a muted headset does
                    # (WSN-027, §20a).
                    if sco_playback_enabled():
                        voice_leg.ensure(wanted, calls[wanted])
                    else:
                        voice_leg.stop()
                    if mic_allowed():
                        mic_leg.ensure(wanted)
                    else:
                        mic_leg.stop()
                    announce("call", wanted,
                             MIC_PCM if mic_leg.running() else None)
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
            waited = 0.0
            while waited < interval and not stopping["now"]:
                time.sleep(0.25)
                waited += 0.25
                if mic_leg.running() and not mic_allowed():
                    mic_leg.stop()
                    announce("call", voice_leg.address, None)
    finally:
        mic_leg.stop()
        voice_leg.stop()
        # The last word is always "no call". A stale `call` left in /run would
        # have the chrome showing a handset for a process that is gone.
        publish_call_state(call_document("idle", None, None, None,
                                         published["revision"] + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
