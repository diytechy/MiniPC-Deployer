#!/usr/bin/env python3
"""The Bluetooth half of item 23 spec C: the selected mic back to the phone.

WHY THIS IS A SUPERVISOR AND NOT AN alsaloop UNIT LIKE EVERY OTHER LEG. The
other legs address a PCM that exists whenever its card is plugged in, so
`BindsTo=` a udev device alias is enough to start and stop them. An HFP SCO PCM
is not like that: it exists only while a CALL is up, it is named after the
phone's Bluetooth address, and BlueZ has no device node for it. There is nothing
for systemd to bind to. So this process watches BlueALSA's own D-Bus object tree
and runs exactly one alsaloop for exactly as long as there is a sink to run it
into.

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
MODE_FILE = "/etc/wall-panel/audio-mode"

# /org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/sco/sink -- the object path BlueALSA
# publishes for the playback half of an HFP link. `source` is the other half
# (the far end's voice) and is deliberately not used here.
SCO_SINK = re.compile(
    r"/org/bluealsa/(?P<hci>hci\d+)/dev_(?P<dev>[0-9A-Fa-f_]{17})/sco/sink\b")

DEFAULT_POLL_SECONDS = 5.0
# A poll that is cheap enough to be frequent and slow enough not to matter: the
# cost is one busctl call. Bounded at both ends so a typo in wall.env cannot
# turn this into a spin loop or into a leg that takes a minute to notice a call.
POLL_MIN, POLL_MAX = 1.0, 60.0


def log(message):
    """One line to the journal. stderr, so systemd stamps the unit identity."""
    sys.stderr.write("%s\n" % message)
    sys.stderr.flush()


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


def loop_argv(address):
    """The alsaloop command line, under the same guard every other leg uses."""
    return [GUARD, "--max-errors", "20", "--window", "10", "--",
            ALSALOOP,
            "--cdevice", MIC_PCM,
            "--pdevice", pcm_name(address),
            "--format", "S16_LE", "--rate", "16000", "--channels", "1",
            "--tlatency", "50000", "--sync", "5"]


def read_sinks(run=subprocess.run):
    """Ask BlueALSA what SCO sinks exist. [] on any failure, never an exception.

    A BlueALSA that is not running, a busctl that is missing, a D-Bus that is
    slow: all of them mean "no call is up", which is the safe answer, because
    the failure direction that matters is a microphone that stays open, not one
    that does not.
    """
    try:
        done = run([BUSCTL, "--system", "tree", "org.bluealsa"],
                   check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log("bluealsa object tree unreadable (%s): no HFP mic return this poll" % exc)
        return []
    if done.returncode != 0:
        return []
    return parse_sco_sinks(done.stdout)


class Leg:
    """At most one alsaloop child, started and stopped by address."""

    def __init__(self, popen=subprocess.Popen):
        self._popen = popen
        self.address = None
        self.child = None

    def running(self):
        return self.child is not None and self.child.poll() is None

    def start(self, address):
        self.stop()
        try:
            self.child = self._popen(loop_argv(address))
        except OSError as exc:
            log("could not start the HFP mic return to %s: %s" % (address, exc))
            self.child = None
            return False
        self.address = address
        log("HFP mic return started: %s -> %s" % (MIC_PCM, pcm_name(address)))
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
            log("HFP mic return would not stop cleanly (%s)" % exc)
        self.child = None
        self.address = None
        if address:
            log("HFP mic return stopped (%s)" % address)


def decide(sinks, current):
    """Which address the leg should be on, given what exists. Pure.

    First by sorted order, so the answer does not change between polls for the
    same set of devices, and the CURRENT one is kept if it is still there --
    a call in progress is never moved to another phone because a second one
    happened to connect.
    """
    if not sinks:
        return None
    if current in sinks:
        return current
    return sinks[0]


def main(argv=None):
    interval = poll_seconds()
    leg = Leg()
    stopping = {"now": False}

    def handle(signum, frame):  # noqa: ARG001 - the signal API's shape
        stopping["now"] = True

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    log("HFP mic return watching for SCO sinks every %.1f s" % interval)
    try:
        while not stopping["now"]:
            if not bus_mode_active() or not mic_allowed():
                leg.stop()
            else:
                wanted = decide(read_sinks(), leg.address)
                if wanted is None:
                    leg.stop()
                elif wanted != leg.address or not leg.running():
                    leg.start(wanted)
            # Sleep in short slices so SIGTERM is answered promptly rather than
            # after a whole poll interval: systemd's stop timeout is not long.
            waited = 0.0
            while waited < interval and not stopping["now"]:
                time.sleep(0.25)
                waited += 0.25
    finally:
        leg.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
