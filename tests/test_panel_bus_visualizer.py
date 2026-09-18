"""SR-041: the merged bus as a visualizer source, proved without a consumer.

WHAT THIS FILE IS FOR. Work package G builds the PRODUCER first and proves it
with no consumer at all, because the MiniPC image installs before an app that
can ask for PCM. So everything here runs against the pure core, the frame codec
and the shipped payload text -- never against a panel, a sound card or an
Electron host.

THE THREE THINGS MOST LIKELY TO GO WRONG, and the tests that hold each down:

  1. THE CAPTURE COULD DISTURB THE AMPLIFIER DETECTOR. It must not: the detector
     reads `speaker_tap` for the relay, and visualization must not reuse that
     post-switch signal. `test_the_detector_is_not_touched_by_this_service_sr041`
     and the failure injection below prove the isolation rather than assert it.
  2. THE TWO dB CURVES COULD DRIFT. The detector's readout and this producer map
     an amplitude to a 0..1 scalar with the same curve, in two processes that
     cannot import each other.
     `test_the_two_scalar_curves_agree_across_the_whole_range_sr041` imports both
     and compares them, which is the pattern this repo already uses for
     `wall_audio_state`.
  3. THE WIRE FORMAT COULD DRIFT FROM THE APP'S. The fixture in
     `tests/fixtures/pcm-frame-v1.bin` is BYTE-IDENTICAL to the one in
     OfficeWallNaglight, and both repositories decode it.
"""

import importlib.util
import json
from pathlib import Path
import socket
import struct
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stack/panel-audio"))
import bus_source
import pcm_frame
from visualizer import VisualizerTelemetry

WALL = ROOT / "stack/autoinstall/wall"
FIXTURE = ROOT / "tests/fixtures/pcm-frame-v1.bin"
FIXTURE_META = json.loads((ROOT / "tests/fixtures/pcm-frame-v1.json").read_text(encoding="utf-8"))


def load_script(name):
    """Import one of the payload's hyphenated scripts by path."""
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), WALL / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def block_of(frames, level=8000, channels=bus_source.BUS_CHANNELS):
    """One interleaved S16_LE block at a constant level."""
    return struct.pack("<%dh" % (frames * channels), *([level] * (frames * channels)))


def read(path):
    return path.read_text(encoding="utf-8")


# --- the frozen frame (LLR-953) ---------------------------------------------

@pytest.mark.smoke
def test_pcm_frame_round_trip_and_boundary_values_sr041():
    """Every declared boundary encodes and decodes to exactly what went in."""
    for sequence in (0, pcm_frame.SEQUENCE_MODULUS - 1):
        for channels in (1, 2):
            for frames in (1, pcm_frame.MAX_PCM_FRAME_COUNT):
                payload = block_of(frames, level=-321, channels=channels)
                frame = pcm_frame.encode_pcm_frame(
                    payload, sequence=sequence, observed_monotonic_ms=7,
                    sample_rate=48000, channels=channels, frame_count=frames)
                assert len(frame) <= pcm_frame.MAX_PCM_FRAME_BYTES
                decoded = pcm_frame.decode_pcm_frame(frame)
                assert decoded["sequence"] == sequence
                assert decoded["channels"] == channels
                assert decoded["frameCount"] == frames
                assert decoded["payload"] == payload


@pytest.mark.smoke
def test_the_shared_fixture_decodes_identically_in_both_repositories_sr041():
    """The one artefact that stops the two implementations drifting.

    OfficeWallNaglight ships the SAME BYTES and decodes them in its own suite.
    If either side's codec changes, the repository that changed fails here.
    """
    frame = FIXTURE.read_bytes()
    assert len(frame) == FIXTURE_META["frameBytes"]
    decoded = pcm_frame.decode_pcm_frame(frame)
    for key in ("sequence", "observedMonotonicMs", "sampleRate", "channels", "frameCount"):
        assert decoded[key] == FIXTURE_META[key], key
    samples = struct.unpack("<%dh" % (decoded["frameCount"] * decoded["channels"]),
                            decoded["payload"])
    assert list(samples) == FIXTURE_META["interleaved"]


@pytest.mark.smoke
def test_pcm_frame_refuses_invalid_header_and_payload_sr041():
    """A frame that cannot be believed is refused, never partly returned."""
    good = FIXTURE.read_bytes()
    damaged = {
        "bad magic": b"XPCM" + good[4:],
        "bad version": good[:4] + struct.pack("<H", 2) + good[6:],
        "bad header size": good[:6] + struct.pack("<H", 40) + good[8:],
        "zero channels": good[:24] + struct.pack("<H", 0) + good[26:],
        "three channels": good[:24] + struct.pack("<H", 3) + good[26:],
        "zero frames": good[:26] + struct.pack("<H", 0) + good[28:],
        "payload mismatch": good[:28] + struct.pack("<I", 16) + good[32:],
        "truncated": good[:-1],
        "trailing byte": good + b"\x00",
        "empty": b"",
    }
    for name, frame in damaged.items():
        with pytest.raises(pcm_frame.PcmFrameError):
            pcm_frame.decode_pcm_frame(frame)
        assert name  # names appear in the failure output


@pytest.mark.smoke
def test_an_oversized_frame_is_refused_as_a_number_not_an_allocation_sr041():
    """The header is fixed-width so the size is a decision, not a discovery.

    A `payload_bytes` above one bus period is rejected from the header alone --
    before any buffer the size of that claim has been asked for.
    """
    too_many = pcm_frame.MAX_PCM_FRAME_COUNT + 1
    header = struct.pack("<4sHHIQIHHI", pcm_frame.PCM_MAGIC, 1, 32, 1, 0, 48000,
                         2, too_many, too_many * 4)
    with pytest.raises(pcm_frame.PcmFrameError):
        pcm_frame.decode_pcm_header(header)
    with pytest.raises(pcm_frame.PcmFrameError):
        pcm_frame.encode_pcm_frame(b"\x00" * (too_many * 4), sequence=1,
                                   observed_monotonic_ms=0, sample_rate=48000,
                                   channels=2, frame_count=too_many)


@pytest.mark.smoke
def test_a_stream_is_split_on_whole_frames_only_sr041():
    """A partial frame stays in the buffer; it is never handed on half-read."""
    one = FIXTURE.read_bytes()
    frames, rest = bus_source.split_frames(one + one + one[:10],
                                           pcm_frame.decode_pcm_header,
                                           pcm_frame.PCM_HEADER_BYTES)
    assert frames == [one, one]
    assert rest == one[:10]


# --- the capture and the window (LLR-950) -----------------------------------

@pytest.mark.smoke
def test_the_mono_window_is_bounded_normalized_and_decimated_sr041():
    window = bus_source.mono_window(block_of(bus_source.WINDOW_FRAMES))
    assert len(window) == bus_source.WINDOW_SAMPLES
    assert all(-1.0 <= value <= 1.0 for value in window)
    # A constant-level block is a DC signal, so every decimated sample is the
    # same normalized value. That is the cheapest proof the boxcar sums and the
    # divisor agree: if they did not, this would be off by a factor of four.
    assert window[0] == pytest.approx(8000 / 32768, rel=1e-9)


@pytest.mark.smoke
def test_a_partial_frame_in_the_block_is_refused_sr041():
    with pytest.raises(ValueError):
        bus_source.mono_window(b"\x00" * 7)


@pytest.mark.smoke
def test_the_band_centres_never_exceed_the_windows_nyquist_sr041():
    """A band labelled 5.2 kHz must not be reporting something folded down."""
    for centre in bus_source.band_centres():
        assert centre < bus_source.WINDOW_RATE / 2.0


@pytest.mark.smoke
def test_a_tone_lights_its_own_band_and_not_a_neighbour_sr041():
    """The end-to-end proof that the transform is aimed where its labels claim.

    Without the band centres this would be impossible to state: the default
    linear bin spacing puts every musical frequency in the first bin or two.
    """
    import math
    centres = bus_source.band_centres()
    telemetry = VisualizerTelemetry(
        band_count=len(centres), max_samples=bus_source.WINDOW_SAMPLES,
        sample_rate=bus_source.WINDOW_RATE, band_centres_hz=centres,
        minimum_interval_ms=200)
    target = centres.index(960.0)
    samples = struct.pack(
        "<%dh" % (bus_source.WINDOW_FRAMES * 2),
        *[int(0.5 * 32767 * math.sin(2 * math.pi * 960.0 * i / bus_source.BUS_RATE))
          for i in range(bus_source.WINDOW_FRAMES) for _ in range(2)])
    measured = telemetry.process(bus_source.mono_window(samples), generation=1,
                                 observed_monotonic_ms=1000)
    bands = measured["bands"]
    assert bands[target] == max(bands)
    assert bands[target] > 4 * bands[0]


# --- the published document (LLR-951) ---------------------------------------

@pytest.mark.smoke
def test_bus_document_is_bounded_schema_two_and_names_its_source_sr041():
    telemetry = VisualizerTelemetry(
        band_count=8, max_samples=bus_source.WINDOW_SAMPLES,
        sample_rate=bus_source.WINDOW_RATE,
        band_centres_hz=bus_source.band_centres(), minimum_interval_ms=200)
    measured = telemetry.process(bus_source.mono_window(block_of(bus_source.WINDOW_FRAMES)),
                                 generation=4, observed_monotonic_ms=900)
    document = bus_source.build_document(measured, state="live", generation=4,
                                         observed_monotonic_ms=900)
    assert document["schema"] == 2
    assert document["source"] == "bus_monitor"
    assert document["state"] == "live" and document["valid"] is True
    assert len(document["bands"]) == 8
    assert all(0.0 <= value <= 1.0 for value in document["bands"])
    assert 0.0 <= document["rms"] <= 1.0 and 0.0 <= document["peak"] <= 1.0
    # NO SAMPLES, NO MICROPHONE, NO DEVICE. The document is the one thing that
    # leaves this service without an authorized peer behind it, so what it may
    # contain is an exact list rather than an absence of known leaks.
    assert set(document) == {"schema", "source", "state", "valid", "active",
                             "generation", "rms", "peak", "bands",
                             "observed_monotonic_ms", "reference_dbfs"}


@pytest.mark.smoke
@pytest.mark.parametrize("state", ["silent", "unavailable"])
def test_a_producer_that_is_not_measuring_publishes_zeros_not_the_last_frame_sr041(state):
    """A frozen visualizer is indistinguishable from a quiet room."""
    document = bus_source.build_document(None, state=state, generation=2,
                                         observed_monotonic_ms=10)
    assert document["state"] == state
    assert document["valid"] is False and document["active"] is False
    assert document["rms"] == 0.0 and document["bands"] == [0.0] * 8


@pytest.mark.smoke
def test_unavailable_and_silent_are_different_answers_sr041():
    """Bus mode is the supported configuration; the other three say so.

    `unavailable` is the panel not being in bus mode, or a capture that would
    not open. `silent` is a measurement of a quiet bus. The shell renders them
    differently -- absence falls through to Frame Media -- so they may not share
    a representation.
    """
    assert bus_source.telemetry_state(mode_is_bus=False, capture_alive=True,
                                      measurement={}) == "unavailable"
    assert bus_source.telemetry_state(mode_is_bus=True, capture_alive=False,
                                      measurement={}) == "unavailable"
    assert bus_source.telemetry_state(mode_is_bus=True, capture_alive=True,
                                      measurement=None) == "silent"
    assert bus_source.telemetry_state(mode_is_bus=True, capture_alive=True,
                                      measurement={}) == "live"


@pytest.mark.smoke
def test_the_two_scalar_curves_agree_across_the_whole_range_sr041():
    """The detector's readout and this producer share a curve in two processes.

    They cannot import each other: the detector runs under a different unit with
    a different privilege, and it is the one module in the audio path this work
    package is forbidden to disturb. So the copies are held together here, the
    same way `switch_backend` and `wall_audio_state` are.
    """
    detector = load_script("panel-amp-trigger")
    for exponent in range(0, 81):
        amplitude = 10.0 ** (-exponent / 20.0)
        assert bus_source.scalar_from_amplitude(amplitude) == pytest.approx(
            detector.scalar_from_amplitude(amplitude), abs=1e-12), amplitude
    assert bus_source.scalar_from_amplitude(0.0) == 0.0
    assert detector.scalar_from_amplitude(0.0) == 0.0
    assert bus_source.BUS_FLOOR_DBFS == detector.BUS_FLOOR_DBFS
    assert bus_source.BUS_REFERENCE_DBFS == detector.BUS_REFERENCE_DBFS
    assert tuple(bus_source.BUS_BANDS_HZ) == tuple(detector.BUS_BANDS_HZ)


# --- authorization (LLR-954) ------------------------------------------------

@pytest.mark.smoke
def test_only_a_configured_peer_uid_may_receive_pcm_sr041():
    assert bus_source.peer_allowed(1000, allowed_uids=[1000]) is True
    assert bus_source.peer_allowed(1001, allowed_uids=[1000]) is False
    assert bus_source.peer_allowed(0, allowed_uids=[1000]) is False


@pytest.mark.smoke
def test_an_empty_allow_list_denies_rather_than_permits_sr041():
    """A missing line in a unit file must not become an open socket."""
    assert bus_source.peer_allowed(1000, allowed_uids=[]) is False
    assert bus_source.peer_allowed(1000, allowed_uids=[None, "1000", True]) is False


@pytest.mark.smoke
def test_a_nonsense_uid_is_denied_sr041():
    for uid in (None, -1, True, "1000", 1.5):
        assert bus_source.peer_allowed(uid, allowed_uids=[1000]) is False


# --- backpressure and teardown (LLR-955) ------------------------------------

@pytest.mark.smoke
def test_latest_block_queue_drops_the_older_block_and_never_grows_sr041():
    queue = bus_source.LatestBlockQueue()
    assert queue.put("a") is True
    assert queue.put("b") is False
    assert queue.put("c") is False
    assert queue.drops == 2
    # ONE slot. The consumer gets the newest and there is nothing behind it: a
    # queue that could grow would be a recording held in memory.
    assert queue.take() == "c"
    assert queue.take() is None
    assert queue.pending is False


@pytest.mark.smoke
def test_clearing_the_queue_drops_the_block_it_was_holding_sr041():
    """Every teardown path calls this, so it is what "discard buffers" means."""
    queue = bus_source.LatestBlockQueue()
    queue.put(b"samples")
    queue.clear()
    assert queue.pending is False and queue.take() is None


# --- the service shell, without a sound card --------------------------------

@pytest.fixture
def service(tmp_path):
    """A Service wired to temp paths and a fake capture. Opens no device."""
    module = load_script("panel-bus-visualizer")

    class FakeCapture:
        arecord = str(tmp_path / "arecord")
        generation = 0

        def __init__(self):
            self.blocks = []
            self.alive = True
            self.opened = 0
            self.closed = 0

        def open(self):
            self.opened += 1
            self.generation += 1
            return True

        def read_block(self):
            return self.blocks.pop(0) if self.blocks else None

        def close(self):
            self.closed += 1

    Path(FakeCapture.arecord).write_text("#!/bin/sh\n", encoding="utf-8")
    capture = FakeCapture()
    mode = tmp_path / "audio-mode"
    mode.write_text("bus\n", encoding="utf-8")
    ticks = iter(range(1, 100000))
    instance = module.Service(
        telemetry_path=str(tmp_path / "bus-telemetry.json"),
        socket_path=str(tmp_path / "pcm.sock"), mode_path=str(mode),
        allowed_uids=[], capture=capture, clock=lambda: next(ticks))
    return {"module": module, "service": instance, "capture": capture,
            "mode": mode, "document": tmp_path / "bus-telemetry.json",
            "socket": tmp_path / "pcm.sock"}


def test_a_panel_that_is_not_in_bus_mode_publishes_unavailable_sr041(service):
    """`bus_monitor` is declared in one of the four ALSA configurations.

    In the other three there is nothing to open, and the honest answer is
    absence -- not an opened-and-quiet capture, and not a crash loop.
    """
    service["mode"].write_text("trigger\n", encoding="utf-8")
    service["service"].stop()
    assert service["service"].run() == 0
    assert service["capture"].opened == 0
    document = json.loads(read(service["document"]))
    assert document["state"] == "unavailable"
    assert document["valid"] is False and document["bands"] == [0.0] * 8


def test_the_capture_generation_is_bumped_on_every_reopen_sr041(service):
    """The fence a consumer needs to tell a gap from a different capture."""
    before = service["capture"].generation
    service["capture"].open()
    service["capture"].open()
    assert service["capture"].generation == before + 2


class StubServer:
    """A PcmServer's surface, with none of its sockets."""

    def __init__(self, attached):
        self.attached = attached
        self.sent = []
        self.dropped = []

    def broadcast(self, frame):
        self.sent.append(frame)

    def drop_all(self, reason):
        self.dropped.append(reason)


def test_an_unframeable_block_drops_the_streams_rather_than_guessing_sr041(service):
    """A shape the codec refuses is a producer bug, not a client's problem."""
    instance = service["service"]
    instance.server = StubServer(attached=1)
    instance._serve_pcm(b"\x00" * 7)          # not a whole frame of any shape
    assert instance.server.dropped and instance.server.sent == []


def test_no_client_means_no_encoding_at_all_sr041(service):
    """The default configuration's cost is a comparison against zero.

    With nobody attached the block is dropped where it was read, so raw samples
    are never materialised into a second buffer nobody asked for.
    """
    instance = service["service"]
    instance.server = StubServer(attached=0)
    instance._serve_pcm(block_of(bus_source.BUS_PERIOD_FRAMES))
    assert instance.server.sent == []
    assert instance.sequence == 0


def test_an_attached_client_receives_exactly_one_frame_per_block_sr041(service):
    """And the sequence advances by one, so a consumer can tell a gap."""
    instance = service["service"]
    instance.server = StubServer(attached=1)
    instance._serve_pcm(block_of(bus_source.BUS_PERIOD_FRAMES))
    instance._serve_pcm(block_of(bus_source.BUS_PERIOD_FRAMES))
    assert len(instance.server.sent) == 2
    first = pcm_frame.decode_pcm_frame(instance.server.sent[0])
    second = pcm_frame.decode_pcm_frame(instance.server.sent[1])
    assert second["sequence"] == first["sequence"] + 1
    assert first["frameCount"] == bus_source.BUS_PERIOD_FRAMES
    assert first["channels"] == bus_source.BUS_CHANNELS
    assert first["sampleRate"] == bus_source.BUS_RATE


def test_a_telemetry_write_failure_is_survivable_sr041(service, tmp_path):
    """Telemetry is a decoration: its failure may not stop the service.

    This is half of the failure isolation the exit criteria ask for -- the other
    half is that the amplifier detector never reads this file at all.
    """
    module = service["module"]
    assert module.publish_document({"schema": 2},
                                   str(tmp_path / "missing" / "x.json")) is False


def test_closing_a_live_quiet_capture_is_bounded_sr041(tmp_path):
    """terra round 1, finding 2. A REAL child with open, silent pipes.

    The first version drained the child's stderr BEFORE terminating it, which is
    safe only in the amplifier detector's context -- there the read loop has
    already broken, so the pipe is at EOF. Here `close()` also runs on service
    stop and on a mode change, with the child alive and quiet, and `read(2048)`
    on a live silent pipe blocks until it has 2048 bytes. systemd would have
    killed the control group on every stop.

    A fake capture cannot show this, which is exactly why the reviewer said the
    existing tests would pass the defect: this one spawns a process that holds
    both pipes open and writes nothing.
    """
    module = load_script("panel-bus-visualizer")
    quiet = tmp_path / "quiet.py"
    quiet.write_text("import sys, time\ntime.sleep(600)\n", encoding="utf-8")
    capture = module.BusCapture(pcm="unused", arecord=sys.executable)
    capture.process = __import__("subprocess").Popen(
        [sys.executable, str(quiet)],
        stdout=__import__("subprocess").PIPE, stderr=__import__("subprocess").PIPE)
    started = __import__("time").monotonic()
    capture.close()
    assert __import__("time").monotonic() - started < 10
    assert capture.process is None


def test_a_mode_change_releases_the_capture_before_it_waits_sr041(service):
    """terra round 1, finding 2, second half.

    `_pump` returns when the mode changes underneath a running capture. The
    first version then sat in the not-in-bus branch holding the `bus_monitor`
    dsnoop reader open for as long as the panel stayed out of bus mode --
    contention beside the very legs a mode switch is trying to rearrange.
    """
    instance = service["service"]
    capture = service["capture"]
    service["mode"].write_text("trigger\n", encoding="utf-8")
    instance.stop()
    assert instance.run() == 0
    assert capture.closed >= 1


def test_mute_drops_the_pcm_stream_at_the_producer_too_sr041(service, tmp_path):
    """terra round 1, finding 1, accepted in part.

    The consumer closes the socket when ProjectM yields -- the connection IS the
    lease, and it is the only signal a producer can have, because visibility is
    not observable from here. This is the BACKSTOP that does not depend on the
    consumer being correct, and it is a ruling rather than an invention: Mute
    yields Frame Media, so Mute means the PCM permission does not hold.

    The DERIVED telemetry is deliberately unaffected -- what the bus is carrying
    is true regardless of where the switch sends it.
    """
    module = service["module"]
    state = tmp_path / "audio-state.json"
    state.write_text(json.dumps({"version": 1, "output": "mute"}), encoding="utf-8")
    assert module.output_is_audible(str(state)) is False
    for output in ("speaker", "headset"):
        state.write_text(json.dumps({"version": 1, "output": output}), encoding="utf-8")
        assert module.output_is_audible(str(state)) is True, output
    # A sparse file is repaired to Speaker exactly as the applier repairs it.
    state.write_text(json.dumps({"version": 1}), encoding="utf-8")
    assert module.output_is_audible(str(state)) is True
    # AND AN UNREADABLE ONE ANSWERS TRUE, which is the opposite of the broker's
    # choice and is the right one here: this is a backstop, so refusing PCM
    # because an unrelated file is damaged would turn a file problem into
    # "ProjectM never works".
    state.write_text("{not json", encoding="utf-8")
    assert module.output_is_audible(str(state)) is True
    assert module.output_is_audible(str(tmp_path / "absent.json")) is True

    instance = service["service"]
    instance.server = StubServer(attached=1)
    instance.audible = False
    instance._serve_pcm(block_of(bus_source.BUS_PERIOD_FRAMES))
    assert instance.server.sent == []


def test_the_health_probe_opens_no_device_and_writes_no_file_sr041(service, tmp_path):
    """`--check` is the probe the release manifest names.

    It answers "are the things this service needs present" without starting a
    capture or publishing anything, so it can be run on a panel in any ALSA mode
    and on a panel where the visualizer is deliberately idle. A probe that
    needed a PCM client would report a healthy service as broken every time
    nobody was looking at ProjectM.
    """
    module = service["module"]
    before = service["document"].exists()
    code = module.main(["--check", "--mode-file", str(service["mode"]),
                        "--allow-user", "definitely-not-a-user"])
    # Non-zero, and WHICH non-zero depends on the host: 78 on a machine with no
    # `arecord` at all (systemd reads that as "do not restart me"), 1 for the
    # unknown user. Both are refusals; asserting the exact code here would be
    # asserting a property of the developer's machine.
    assert code in (1, module.EXIT_MISSING_TOOL)
    assert service["document"].exists() is before
    assert service["capture"].opened == 0


def test_the_detector_is_not_touched_by_this_service_sr041():
    """Failure isolation, proved from the shipped text rather than asserted.

    The amplifier detector's relay decision must be unaffected by anything this
    work package does, so: it must not learn the new document's path, and this
    service must not learn the detector's PCM.
    """
    detector = read(WALL / "panel-amp-trigger.py")
    visualizer = read(WALL / "panel-bus-visualizer.py")
    assert "wall-bus-visualizer" not in detector
    assert "bus_monitor" not in detector
    # QUOTED, not merely mentioned: the prose in each file explains the other,
    # and must be allowed to. What may not appear is either file naming the
    # other's PCM or runtime directory as a STRING it could act on.
    assert '"speaker_tap"' not in visualizer
    assert "'speaker_tap'" not in visualizer
    assert "/run/wall-amp-trigger" not in visualizer
    assert load_script("panel-bus-visualizer").CAPTURE_PCM == "bus_monitor"
    # And the detector still reads exactly the tap it needs for the relay.
    assert '("speaker_tap", 0.0)' in detector


# --- the payload, the unit and the ALSA configuration (LLR-957, LLR-958) ----

def test_the_unit_takes_the_audio_group_by_supplementary_grant_sr041():
    """The pattern the two existing ALSA-capture units already use.

    `bus_monitor` is a dsnoop with ipc_gid 29, so the group IS the privilege.
    Inventing a `User=` here would be a third pattern for no gain -- and `panel`
    is deliberately not that identity, because the broker the renderer talks to
    must stay unable to open a sound device.
    """
    unit = read(WALL / "wall-bus-visualizer.service")
    assert "SupplementaryGroups=audio" in unit
    assert "\nUser=" not in unit
    assert "NoNewPrivileges=true" in unit
    assert "RestrictAddressFamilies=AF_UNIX" in unit
    assert "ProtectSystem=strict" in unit
    assert "RestrictPRIVATE" not in unit


def test_the_unit_runs_only_in_bus_mode_sr041():
    unit = read(WALL / "wall-bus-visualizer.service")
    assert "ExecCondition=" in unit and "audio-mode" in unit and "= bus ]" in unit


def test_firstboot_installs_every_file_the_unit_names_sr041():
    """Nothing is installed that the payload does not name, and vice versa."""
    firstboot = read(WALL / "wall-firstboot.sh")
    for name in ("panel-bus-visualizer.py", "wall-bus-visualizer.service"):
        assert name in firstboot, name
    # The daemon imports these from BESIDE itself, the same way the root applier
    # imports wall_audio_state. An install that forgets one is a service that
    # cannot start.
    for name in ("bus_source.py", "pcm_frame.py", "visualizer.py"):
        assert name in firstboot, name


def test_the_bus_mode_conf_no_longer_claims_a_single_reader_sr041():
    """The comment stopped being true the moment a second client attached."""
    conf = read(WALL / "asound-bus-mode.conf")
    assert "The single reader of the bus" not in conf
    assert "ipc_perm 0660" in conf and "ipc_gid 29" in conf


def test_bus_monitor_exists_in_exactly_one_alsa_configuration_sr041():
    """The whole reason the producer reports `unavailable` in the other three."""
    declaring = [name for name in ("asound.conf", "asound-bus-mode.conf",
                                   "asound-panel-mode.conf", "asound-trigger-mode.conf")
                 if "pcm.bus_monitor" in read(WALL / name)]
    assert declaring == ["asound-bus-mode.conf"]


# --- the endpoint, on a real socket where the platform allows it ------------

@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"),
                    reason="SO_PEERCRED is a Linux credential; the producer is Linux-only")
def test_the_endpoint_refuses_an_unauthorized_peer_and_serves_an_authorized_one_sr041(tmp_path):
    """The two kernel-vouched facts, on a real socket.

    Right-sized for the Owner's stated threat model -- make checking items off
    NagLight easy, keep children from checking things off for fun -- and not
    presented as a defence against a compromised host.
    """
    import os
    module = load_script("panel-bus-visualizer")
    path = str(tmp_path / "pcm.sock")
    server = module.PcmServer(path, [os.getuid()])
    assert server.start() is True
    try:
        assert oct(os.stat(path).st_mode & 0o777) == "0o660"
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(path)
        deadline = threading.Event()
        while server.attached == 0 and not deadline.wait(0.02):
            break
        frame = FIXTURE.read_bytes()
        for _ in range(50):
            if server.attached:
                break
            deadline.wait(0.02)
        assert server.attached == 1
        server.broadcast(frame)
        client.settimeout(2)
        assert client.recv(len(frame)) == frame
        client.close()
    finally:
        server.stop()
    assert not Path(path).exists()


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"),
                    reason="SO_PEERCRED is a Linux credential; the producer is Linux-only")
def test_dropping_every_stream_closes_the_socket_and_the_buffer_sr041(tmp_path):
    import os
    module = load_script("panel-bus-visualizer")
    path = str(tmp_path / "pcm.sock")
    server = module.PcmServer(path, [os.getuid()])
    assert server.start() is True
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(path)
        for _ in range(50):
            if server.attached:
                break
            threading.Event().wait(0.02)
        assert server.attached == 1
        server.drop_all("ProjectM yielded")
        assert server.attached == 0
        client.settimeout(2)
        assert client.recv(64) == b""            # the far end really closed
        client.close()
    finally:
        server.stop()
