#!/usr/bin/env python3
"""The merged-bus visualizer source: one capture, two bounded consumers.

ONE RESPONSIBILITY: open `bus_monitor` once and fan each captured block into
(i) a bounded derived-telemetry document every other process reads, and
(ii) an OPTIONAL bounded PCM stream for a local, authorized Electron host.

WHY THIS SERVICE EXISTS AT ALL. Until now the panel's visualizer bands came from
`wall-amp-trigger`, which reads `speaker_tap`. That tap is POST-switch and
exists for the amplifier relay: it sees audio only while the switch is on
Speaker. So the wall showed nothing in Headset, and could show nothing that did
not reach the speaker leg. The Owner asked for a visualizer that works from
every source on the merged bus, which is `bus_monitor` -- PRE-switch, carrying
Library, Pandora, Bluetooth and the desktop's S/PDIF input together.

WHAT THIS SERVICE MUST NOT DO, and the failure injection in
`tests/test_panel_bus_visualizer.py` proves each one:

  * It must not disturb the amplifier detector. `panel-amp-trigger.py` still
    reads `speaker_tap` and only `speaker_tap`, and this process never opens
    that PCM, never writes the detector's runtime directory and never touches
    the relay. If this service dies, the relay is unaffected.
  * It must not alter routing. It opens ONE capture, in read-only terms it is a
    second reader of an existing dsnoop, and it starts, stops and configures
    nothing else.
  * It must not open a microphone. `bus_monitor` is the only device name it
    knows, and the unit's sandbox leaves it no way to reach another.

BUS MODE IS THE SUPPORTED CONFIGURATION (Owner, 2026-09-17). `bus_monitor` is
declared only in `asound-bus-mode.conf`; the panel, trigger and default
configurations do not have it. In any other mode this service publishes
`unavailable` -- which is a DIFFERENT answer from silence, and the shell renders
it differently -- and never tries to open a device that is not there.

RAW AUDIO. The telemetry document carries derived scalars and nothing else. The
PCM socket is the one explicitly permitted exception and it is narrow: local
AF_UNIX only, memory only, only while an authorized local host has asked, at
most one unsent block per client, never written to a file, never journaled,
never in diagnostics, never uploaded, and dropped the instant the client goes
away. The journal gets lifecycle lines and counters; it never gets a sample.

Implements: SR-041, LLR-950, LLR-951, LLR-953, LLR-954, LLR-955, LLR-956, LLR-957.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

# The pure core and the shared transform are installed BESIDE this script by
# wall-firstboot.sh, exactly as `wall_audio_state.py` is installed beside the
# root applier and for the same reason: the daemon lives in
# /usr/local/lib/wall-panel and must not reach into the app's mirrored tree,
# which is a different release lane and goes stale on its own schedule.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bus_source                                                  # noqa: E402
import pcm_frame                                                   # noqa: E402
from visualizer import VisualizerTelemetry                         # noqa: E402

MODE_FILE = "/etc/wall-panel/audio-mode"
# The applier's own record of where the switch is. READ, never written, and read
# for exactly one reason (terra, review round 1, finding 1, accepted in part):
# Mute means ProjectM is not the visible fullscreen owner, so it means the PCM
# permission does not hold. The consumer closes the socket on Mute of its own
# accord -- the connection IS the lease -- and this is the backstop that does
# not depend on the consumer being correct. It gates the raw stream ONLY; the
# derived telemetry keeps reporting what the bus is carrying, because that is
# true regardless of where the switch sends it, and the broker is what ANDs the
# switch into the activity claim the shell gates on.
STATE_FILE = "/etc/wall-panel/audio-state.json"
# The two positions that send the bus somewhere audible. Duplicated from the
# applier, like switch_backend's copy and for the same reason: that module lives
# in a tree this sandbox does not import.
AUDIBLE_OUTPUTS = ("headset", "speaker")
DEFAULT_OUTPUT = "speaker"
RUNTIME_DIR = "/run/wall-bus-visualizer"
TELEMETRY_FILE = RUNTIME_DIR + "/bus-telemetry.json"
SOCKET_FILE = RUNTIME_DIR + "/pcm.sock"
CAPTURE_PCM = "bus_monitor"
ARECORD = "/usr/bin/arecord"
# 5 Hz, matching the document this one replaces. The renderer polls at 4 Hz, so
# publishing faster would be work nobody reads.
PUBLISH_INTERVAL = 0.2
# Two clients: the Electron host, and the one that has not finished going away
# yet. Not a tuning knob -- a third simultaneous local host is not a state this
# panel has, and a bound that cannot be exceeded is better than a queue.
MAX_CLIENTS = 2
# How long a client's socket write may block before the client is dropped. A
# consumer that cannot take 4 KB in a quarter of a second is not going to catch
# up, and holding the capture loop for it would starve the telemetry every other
# process depends on.
CLIENT_WRITE_TIMEOUT = 0.25
# 78 = a tool it needs is missing. Matches wall-amp-trigger's contract with
# systemd, which sets RestartPreventExitStatus=78: restarting will not conjure
# an `arecord`.
EXIT_MISSING_TOOL = 78


def log(message):
    """One line to the journal. Lifecycle and counters only -- never a sample."""
    sys.stdout.write("wall-bus-visualizer: %s\n" % message)
    sys.stdout.flush()


def current_mode(path=MODE_FILE):
    """The stored ALSA mode, defaulting to `trigger` exactly as the units do."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip() or "trigger"
    except OSError:
        return "trigger"


def output_is_audible(path=STATE_FILE):
    """Whether the CONFIRMED switch position sends the bus anywhere audible.

    An unreadable or absent state file answers True, and that asymmetry is
    deliberate: this is a BACKSTOP behind the consumer's own gate, not the gate
    itself, and a panel whose state file cannot be read is a panel where this
    function knows nothing. Refusing PCM there would turn an unrelated file
    problem into "ProjectM never works", which is a worse failure than the one
    this guards. The broker's `_output_is_audible` makes the opposite choice for
    the opposite reason: it owns the activity claim the shell gates on, and there
    an unprovable claim must not be made.

    Implements: SR-041, LLR-954.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.loads(handle.read(16384))
    except (OSError, ValueError):
        return True
    if not isinstance(raw, dict):
        return True
    output = raw.get("output")
    if output not in ("mute",) + AUDIBLE_OUTPUTS:
        output = DEFAULT_OUTPUT           # exactly the applier's own repair
    return output in AUDIBLE_OUTPUTS


def publish_document(document, path=TELEMETRY_FILE):
    """Write one bounded document, atomically. Never raises.

    NO fsync, DELIBERATELY, and for the same reason the detector's readout does
    not: /run is tmpfs, the document is republished five times a second and
    means nothing after a reboot. The atomic rename stays, because a reader must
    never see half a document.

    Implements: SR-041, LLR-951, LLR-956.
    """
    try:
        payload = json.dumps(document, sort_keys=True) + "\n"
        temporary = path + ".new"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temporary, path)
        # 0644: the broker runs as `panel` and has to read it. There is nothing
        # secret in a loudness figure.
        os.chmod(path, 0o644)
        return True
    except (OSError, ValueError, TypeError) as exc:
        # NEVER FATAL. Telemetry is a decoration; the capture and the socket go
        # on without it, and nothing about the relay or the routing depends on
        # this file existing.
        log("telemetry publish failed: %s" % exc)
        return False


class PcmClient:
    """One connected local host, and the single block it is allowed to be owed.

    Implements: SR-041, LLR-955.
    """

    def __init__(self, connection, uid):
        self.connection = connection
        self.uid = uid
        self.queue = bus_source.LatestBlockQueue()
        self.wake = threading.Condition()
        self.closed = False

    def offer(self, frame):
        """Hand this client the newest frame, displacing any unsent one."""
        with self.wake:
            if self.closed:
                return
            self.queue.put(frame)
            self.wake.notify()

    def close(self):
        """Stop delivery and drop every buffer this client can still reach."""
        with self.wake:
            if self.closed:
                return
            self.closed = True
            # THE BUFFER GOES FIRST. A closed connection whose mailbox still
            # held a block would keep raw samples reachable for as long as the
            # object lived, which is precisely what "never kept after the
            # consumer yields" forbids.
            self.queue.clear()
            self.wake.notify_all()
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.connection.close()
        except OSError:
            pass


class PcmServer:
    """The optional AF_UNIX PCM endpoint. No client by default.

    AUTHORIZATION IS TWO FACTS THE KERNEL VOUCHES FOR: the socket's own 0660
    group permission, and the peer uid SO_PEERCRED reports. Both are right-sized
    for the Owner's stated threat model -- make checking items off NagLight
    easy, keep children from checking things off for fun -- and neither is
    presented as a defence against a compromised host.

    Implements: SR-041, LLR-954, LLR-955.
    """

    def __init__(self, path, allowed_uids, group=None, mode=0o660):
        self.path = path
        self.allowed_uids = list(allowed_uids)
        self.group = group
        self.mode = mode
        self.listener = None
        self.clients = []
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.refused = 0
        self.served = 0
        self._threads = []

    def start(self):
        """Bind, permission and accept. Returns False if it could not listen."""
        try:
            if os.path.exists(self.path):
                os.unlink(self.path)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(self.path)
            # PERMISSION BEFORE LISTEN. Between bind and chmod the node exists
            # with whatever umask left behind; nothing may connect in that
            # window, so the backlog is opened last.
            if self.group is not None:
                os.chown(self.path, 0, self.group)
            os.chmod(self.path, self.mode)
            listener.listen(MAX_CLIENTS)
            listener.settimeout(0.5)
        except (OSError, AttributeError) as exc:
            # AttributeError is not defensive padding: `socket.AF_UNIX` does not
            # exist off POSIX, and the pure core beside this file is unit-tested
            # on the developer's Windows machine. It is also the honest answer
            # on any host that cannot have this endpoint at all -- unavailable,
            # not crashed, and the telemetry half goes on working.
            log("pcm endpoint unavailable: %s" % exc)
            return False
        self.listener = listener
        thread = threading.Thread(target=self._accept_loop, name="pcm-accept",
                                  daemon=True)
        thread.start()
        self._threads.append(thread)
        return True

    def _accept_loop(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if not self.stopping.is_set() and exc.errno != errno.EBADF:
                    log("pcm accept failed: %s" % exc)
                return
            self._admit(connection)

    def _admit(self, connection):
        try:
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                        struct.calcsize("3i"))
            _, uid, _ = struct.unpack("3i", raw)
        except OSError as exc:
            log("pcm peer credentials unreadable: %s" % exc)
            connection.close()
            return
        if not bus_source.peer_allowed(uid, allowed_uids=self.allowed_uids):
            # COUNTED, NOT DESCRIBED. The refusal is a number in the journal;
            # naming the peer would put an identity in a log that exists to
            # carry lifecycle only.
            self.refused += 1
            connection.close()
            return
        with self.lock:
            if len(self.clients) >= MAX_CLIENTS:
                self.refused += 1
                connection.close()
                return
            client = PcmClient(connection, uid)
            self.clients.append(client)
            self.served += 1
        log("pcm client attached (uid %d, %d attached)" % (uid, len(self.clients)))
        thread = threading.Thread(target=self._serve, args=(client,),
                                  name="pcm-client", daemon=True)
        thread.start()
        self._threads.append(thread)

    def _serve(self, client):
        client.connection.settimeout(CLIENT_WRITE_TIMEOUT)
        try:
            while not self.stopping.is_set():
                with client.wake:
                    while not client.closed and not client.queue.pending:
                        client.wake.wait(0.5)
                        if self.stopping.is_set():
                            break
                    if client.closed or self.stopping.is_set():
                        break
                    frame = client.queue.take()
                if frame is None:
                    continue
                try:
                    client.connection.sendall(frame)
                except (OSError, socket.timeout):
                    break
                finally:
                    # The local reference dies with this iteration either way;
                    # naming it here is what makes "dropped on the failure path"
                    # a statement a reader can check rather than infer.
                    frame = None
        finally:
            self._retire(client)

    def _retire(self, client):
        client.close()
        with self.lock:
            if client in self.clients:
                self.clients.remove(client)
        log("pcm client detached (%d dropped blocks)" % client.queue.drops)

    @property
    def attached(self):
        with self.lock:
            return len(self.clients)

    def broadcast(self, frame):
        """Offer one frame to every attached client. Depth one, latest wins."""
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            client.offer(frame)

    def drop_all(self, reason):
        """Close every stream and discard every buffer. Idempotent."""
        with self.lock:
            clients = list(self.clients)
            self.clients = []
        if clients:
            log("pcm streams closed: %s" % reason)
        for client in clients:
            client.close()

    def stop(self):
        self.stopping.set()
        self.drop_all("service stopping")
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
            self.listener = None
        try:
            os.unlink(self.path)
        except OSError:
            pass


class BusCapture:
    """`arecord` on `bus_monitor`, restarted with backoff, generation-fenced.

    THE SAME SHAPE AS THE DETECTOR'S CAPTURE, deliberately: a child `arecord`
    reading raw blocks on stdout is the pattern already proven on this panel,
    and it keeps the ALSA open in a process that can die without taking the
    service with it.

    Implements: SR-041, LLR-950.
    """

    def __init__(self, pcm=CAPTURE_PCM, arecord=ARECORD):
        self.pcm = pcm
        self.arecord = arecord
        self.process = None
        # THE GENERATION IS THE FENCE. Every reopen bumps it, so a consumer that
        # is holding a window from before a mode change, a suspend or a capture
        # failure can tell that its samples belong to a different capture rather
        # than to a gap in this one.
        self.generation = 0

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None

    def open(self):
        """Start one capture child. Returns False if it could not be started."""
        self.close()
        try:
            self.process = subprocess.Popen(
                [self.arecord, "-D", self.pcm, "-f", "S16_LE",
                 "-r", str(bus_source.BUS_RATE), "-c", str(bus_source.BUS_CHANNELS),
                 "-q", "-t", "raw", "--period-size", str(bus_source.BUS_PERIOD_FRAMES),
                 "--buffer-size", str(8 * bus_source.BUS_PERIOD_FRAMES)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            self.process = None
            log("capture %s could not start: %s" % (self.pcm, exc))
            return False
        self.generation += 1
        return True

    def read_block(self):
        """One whole period, or None when the capture has ended."""
        if self.process is None or self.process.stdout is None:
            return None
        raw = self.process.stdout.read(bus_source.BUS_BLOCK_BYTES)
        if not raw or len(raw) < bus_source.BUS_BLOCK_BYTES:
            return None
        return raw

    def close(self):
        """End the capture child, bounded, and always reap it.

        THE ORDER IS THE WHOLE FIX (terra, review round 1, finding 2). The first
        version drained the child's stderr BEFORE terminating it, copying the
        amplifier detector's pattern out of the one context where it is safe:
        the detector only ever drains after its read loop has already broken, so
        the child is gone and the pipe is at EOF. Here `close()` is also called
        on service stop and on a mode change, with `arecord` running and quiet --
        and `read(2048)` on a live, silent pipe blocks until it has 2048 bytes or
        EOF, which is to say forever. systemd would have killed the control
        group after its stop timeout on every single stop.

        So: terminate first, which closes the pipe from the other end, THEN
        drain what the child had already written, with the wait bounded and a
        kill behind it. ALSA reports open failures on the child's stderr, so the
        drain still has to happen -- discarding it would make a bad PCM
        completely silent in the journal while the service reported itself
        active.
        """
        if self.process is None:
            return
        process, self.process = self.process, None
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            if process.stderr is not None:
                message = process.stderr.read(2048).decode("utf-8", "replace").strip()
                if message:
                    log("capture %s: %s" % (self.pcm, message.splitlines()[0]))
        except (OSError, ValueError):
            pass
        for closer in (process.stdout, process.stderr):
            try:
                if closer is not None:
                    closer.close()
            except OSError:
                pass


def resolve_uids(names):
    """Turn user names into uids, dropping the ones this image does not have.

    `pwd` and `grp` are imported HERE rather than at module scope because they
    are POSIX-only and this file's pure neighbours are unit-tested on the
    developer's Windows machine. The daemon itself never runs anywhere but the
    panel; the import being late costs nothing and keeps the test suite able to
    load the module at all.
    """
    import pwd
    uids = []
    for name in names:
        try:
            uids.append(pwd.getpwnam(name).pw_uid)
        except KeyError:
            log("pcm endpoint: no such user %r; it will not be authorized" % name)
    return uids


def resolve_gid(name):
    try:
        import grp
        return grp.getgrnam(name).gr_gid
    except (ImportError, KeyError):
        log("pcm endpoint: no such group %r; the socket keeps its owning group" % name)
        return None


class Service:
    """The loop that owns the capture, the document and the endpoint."""

    def __init__(self, *, telemetry_path=TELEMETRY_FILE, socket_path=SOCKET_FILE,
                 mode_path=MODE_FILE, state_path=STATE_FILE, allowed_uids=(),
                 group=None, capture=None, clock=time.monotonic):
        self.telemetry_path = telemetry_path
        self.mode_path = mode_path
        self.state_path = state_path
        self.clock = clock
        self.capture = capture if capture is not None else BusCapture()
        self.centres = bus_source.band_centres()
        self.telemetry = VisualizerTelemetry(
            band_count=len(self.centres), max_samples=bus_source.WINDOW_SAMPLES,
            sample_rate=bus_source.WINDOW_RATE, band_centres_hz=self.centres,
            minimum_interval_ms=int(PUBLISH_INTERVAL * 1000))
        self.server = PcmServer(socket_path, allowed_uids, group=group)
        self.stopping = threading.Event()
        self.sequence = 0
        self.pending = b""
        self.published_state = None
        # Refreshed on the publish beat; True until the first check, so a panel
        # that has not published yet is not silently refusing PCM.
        self.audible = True

    def stop(self, *_):
        self.stopping.set()

    def run(self):
        """Capture, publish and serve until stopped. Returns a process exit code."""
        if not os.path.exists(self.capture.arecord):
            log("%s is missing; the merged-bus visualizer cannot run"
                % self.capture.arecord)
            return EXIT_MISSING_TOOL
        # THE ENDPOINT IS OPTIONAL AND ITS FAILURE IS NOT FATAL. A panel whose
        # socket could not be bound still publishes telemetry, and the standard
        # visualizer still works; only ProjectM falls through to Frame Media.
        self.server.start()
        backoff = 1.0
        try:
            while not self.stopping.is_set():
                if current_mode(self.mode_path) != "bus":
                    # THE CAPTURE IS RELEASED BEFORE THE WAIT, NOT AFTER IT
                    # (terra, review round 1, finding 2). `_pump` returns here
                    # when the mode changes underneath a running capture, and
                    # the first version then sat in this branch holding the
                    # `bus_monitor` dsnoop reader open for as long as the panel
                    # stayed out of bus mode -- contention beside the very legs
                    # a mode switch is trying to rearrange. `close()` is
                    # idempotent, so the ordinary "never started" pass through
                    # here costs nothing.
                    self.capture.close()
                    self._go_unavailable("this panel is not in bus mode")
                    self.stopping.wait(2.0)
                    continue
                if not self.capture.open():
                    self._go_unavailable("the capture would not open")
                    self.stopping.wait(backoff)
                    backoff = min(backoff * 2, 8.0)
                    continue
                backoff = 1.0
                self._pump()
                self.capture.close()
                self._go_unavailable("the capture ended")
        finally:
            self.capture.close()
            self.server.stop()
            self._go_unavailable("service stopping")
        return 0

    def _pump(self):
        """Read blocks until the capture ends, publishing and serving as we go."""
        self.pending = b""
        self.telemetry.reset()
        last_publish = 0.0
        while not self.stopping.is_set():
            if current_mode(self.mode_path) != "bus":
                return
            block = self.capture.read_block()
            if block is None:
                return
            self._serve_pcm(block)
            self.pending = (self.pending + block)[-bus_source.WINDOW_FRAMES
                                                  * bus_source.BUS_CHANNELS
                                                  * bus_source.BUS_SAMPLE_BYTES:]
            now = self.clock()
            if now - last_publish < PUBLISH_INTERVAL:
                continue
            last_publish = now
            # CHECKED ON THE PUBLISH BEAT, NOT PER BLOCK. Five times a second is
            # forty times faster than anybody can move the switch, and a stat
            # plus a small read on every one of the forty-seven blocks a second
            # would be real cost for no extra truth.
            self.audible = output_is_audible(self.state_path)
            if not self.audible:
                self.server.drop_all("the output switch is on Mute")
            self._publish_window(now)

    def _serve_pcm(self, block):
        """Encode one block and offer it to every attached client.

        ENCODED ONLY WHEN SOMEBODY IS ATTACHED. With no client this is a
        comparison against zero and the block is dropped where it was read,
        which is what keeps the default configuration's cost at nothing and
        keeps raw samples from being materialised into a second buffer nobody
        asked for.
        """
        if self.server.attached == 0:
            return
        if not self.audible:
            # Belt and braces on the beat between publishes: a switch that moved
            # to Mute has already dropped the streams above, and this keeps the
            # blocks in between from being encoded at all.
            return
        self.sequence = (self.sequence + 1) % pcm_frame.SEQUENCE_MODULUS
        try:
            frame = pcm_frame.encode_pcm_frame(
                block, sequence=self.sequence,
                observed_monotonic_ms=int(self.clock() * 1000),
                sample_rate=bus_source.BUS_RATE, channels=bus_source.BUS_CHANNELS,
                frame_count=bus_source.BUS_PERIOD_FRAMES)
        except pcm_frame.PcmFrameError as exc:
            # A block that cannot be framed is a bug in the capture shape, not a
            # client's problem. Drop the streams rather than send something the
            # far end would have to guess about.
            self.server.drop_all("a block could not be framed: %s" % exc)
            return
        self.server.broadcast(frame)

    def _publish_window(self, now):
        observed = int(now * 1000)
        try:
            window = bus_source.mono_window(self.pending)
            measurement = self.telemetry.process(
                window, generation=self.capture.generation,
                observed_monotonic_ms=observed)
        except ValueError as exc:
            log("window rejected: %s" % exc)
            return
        if measurement is None:
            return
        state = bus_source.telemetry_state(
            mode_is_bus=True, capture_alive=self.capture.alive,
            measurement=measurement)
        document = bus_source.build_document(
            measurement, state=state, generation=self.capture.generation,
            observed_monotonic_ms=observed, bands=len(self.centres))
        self.published_state = state
        publish_document(document, self.telemetry_path)

    def _go_unavailable(self, reason):
        """Publish absence, and close every PCM stream that is still open.

        ONE FUNCTION FOR EVERY TEARDOWN PATH -- mode change, capture failure,
        capture end, stop -- because the requirement is that they all do the
        same three things, and five copies of that is five chances to forget
        one of them.
        """
        self.telemetry.reset()
        self.server.drop_all(reason)
        self.pending = b""
        self.audible = True
        document = bus_source.build_document(
            None, state=bus_source.STATE_UNAVAILABLE,
            generation=self.capture.generation,
            observed_monotonic_ms=int(self.clock() * 1000),
            bands=len(self.centres))
        if self.published_state != bus_source.STATE_UNAVAILABLE:
            log("unavailable: %s" % reason)
        self.published_state = bus_source.STATE_UNAVAILABLE
        publish_document(document, self.telemetry_path)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--telemetry", default=TELEMETRY_FILE,
                        help="where to publish the bounded document")
    parser.add_argument("--socket", default=SOCKET_FILE,
                        help="the local PCM endpoint")
    parser.add_argument("--mode-file", default=MODE_FILE,
                        help="the file naming the active ALSA mode")
    parser.add_argument("--state-file", default=STATE_FILE,
                        help="the applier's switch state, read to stop the PCM "
                             "stream on Mute")
    parser.add_argument("--allow-user", action="append", default=[],
                        help="a user permitted to open the PCM endpoint "
                             "(repeatable; the default is `panel`)")
    parser.add_argument("--socket-group", default="panel",
                        help="the group that owns the PCM endpoint")
    parser.add_argument("--check", action="store_true",
                        help="verify the tools and the mode, then exit. This is "
                             "the health probe the release manifest names: it "
                             "opens no device and writes no file.")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    names = args.allow_user or ["panel"]
    if args.check:
        if not os.path.exists(ARECORD):
            log("check: %s is missing" % ARECORD)
            return EXIT_MISSING_TOOL
        if not resolve_uids(names):
            log("check: none of %s exists on this image" % ", ".join(names))
            return 1
        log("check: tools present, mode is %r" % current_mode(args.mode_file))
        return 0
    os.makedirs(os.path.dirname(args.telemetry), exist_ok=True)
    service = Service(telemetry_path=args.telemetry, socket_path=args.socket,
                      mode_path=args.mode_file, state_path=args.state_file,
                      allowed_uids=resolve_uids(names),
                      group=resolve_gid(args.socket_group))
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, service.stop)
    return service.run()


if __name__ == "__main__":
    sys.exit(main())
