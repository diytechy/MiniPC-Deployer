"""The hub detector's door event feed (IF-079, SR-146): stack/camera-motion/event_feed.py.

Real local sockets throughout, no mocks of the transport: the listener binds
127.0.0.1 and the clients are ordinary TCP sockets, so a stuck client, a client
that never reads and a port already in use are the real thing. The feed is
driven the way camera_motion.py run drives it: select over feed.read_fds() and
feed.write_fds(), then feed.service().

The standing claim is that a feed fault never touches detection or the clip
records. It is proved by running one signal sequence through
camera_motion.dispatch_signal into a SignalSpooler with the feed absent, healthy
and in each failure, and comparing what the spooler emitted.
Implements: TC-1668..TC-1685.
"""

from __future__ import annotations

import ast
import json
import select
import socket
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
for sub in ("camera-motion", "clips", "door", "scratch", "monitoring"):
    sys.path.insert(0, str(STACK / sub))
import camera_motion as cm  # noqa: E402
import event_feed as ef  # noqa: E402

LOCAL = "127.0.0.1"


def free_port() -> int:
    probe = socket.socket()
    probe.bind((LOCAL, 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def opened():
    """A listening feed (peer 127.0.0.1) and the clients to close afterwards."""
    made = {"feeds": [], "socks": []}

    def build(peer=LOCAL, port=None, camera="door", **kw):
        feed = ef.EventFeed(camera, LOCAL, port or free_port(), peer, **kw)
        assert feed.open(), "the feed could not listen"
        made["feeds"].append(feed)
        return feed

    def client(feed, rcvbuf=None):
        sock = socket.socket()
        if rcvbuf:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        sock.connect((LOCAL, feed.port))
        sock.setblocking(False)
        made["socks"].append(sock)
        return sock

    build.client = client
    yield build
    for sock in made["socks"]:
        sock.close()
    for feed in made["feeds"]:
        feed.close()


def pump(feed, rounds=6, wait=0.05):
    """The detector's select pass, a few times."""
    for _ in range(rounds):
        reads, writes = feed.read_fds(), feed.write_fds()
        if not reads and not writes:
            return
        ready, writable, _x = select.select(reads, writes, [], wait)
        feed.service(set(ready), set(writable))


def take(sock, feed=None, timeout=1.0, idle=0.1):
    """Everything that arrives: until `idle` seconds pass after some data, or `timeout`."""
    data, last, end = b"", time.time(), time.time() + timeout
    while time.time() < end:
        if feed is not None:
            pump(feed, rounds=1, wait=0.01)
        ready, _w, _x = select.select([sock], [], [], 0.02)
        if ready:
            try:
                chunk = sock.recv(65536)
            except (BlockingIOError, ConnectionError):
                chunk = b""
            if not chunk:
                break
            data += chunk
            last = time.time()
        elif data and time.time() - last > idle:
            break
    return data


def lines(data: bytes):
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line]


def closed_by_peer(sock, feed, timeout=3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        pump(feed, rounds=1, wait=0.01)
        ready, _w, _x = select.select([sock], [], [], 0.02)
        if ready:
            try:
                return sock.recv(1024) == b""
            except (ConnectionError, OSError):
                return True
    return False


# ── TC-1668 the wire ─────────────────────────────────────────────────────────


def test_tc1668_every_line_is_json_at_most_256_bytes_with_the_documented_shape(opened):
    feed = opened(camera="a" + "b" * 14 + "c")   # the longest camera name the store allows (16)
    sock = opened.client(feed)
    pump(feed)
    for cause in ef.CAUSES:
        feed.publish(cause, 1794013296333 + 99999999)
    feed.tick(feed.last_beat + 5.0, 12345678901)
    data = take(sock, feed=feed)
    raw_lines = data.split(b"\n")[:-1]
    assert len(raw_lines) == 1 + len(ef.CAUSES) + 1
    assert all(len(line) + 1 <= ef.MAX_LINE for line in raw_lines)
    assert data.endswith(b"\n")
    got = [json.loads(line) for line in raw_lines]
    assert got[0] == {"v": 1, "type": "hello", "camera": "a" + "b" * 14 + "c"}
    motions = got[1:-1]
    assert [m["cause"] for m in motions] == list(ef.CAUSES)
    assert all(set(m) == {"v", "type", "camera", "cause", "id", "utc_ms"} and m["type"] == "motion" and m["v"] == 1
               for m in motions)
    assert [m["id"] for m in motions] == [1, 2, 3, 4, 5]
    assert set(got[-1]) == {"v", "type", "utc_ms", "frame_age_ms"} and got[-1]["type"] == "beat"


def test_tc1668_an_oversize_line_is_refused_not_truncated():
    with pytest.raises(ValueError):
        ef.encode({"v": 1, "type": "hello", "camera": "x" * 300})


def test_tc1668_an_unknown_cause_is_never_put_on_the_wire(opened):
    feed = opened()
    sock = opened.client(feed)
    pump(feed)
    take(sock, feed=feed)
    feed.publish("picture", 1)
    feed.publish("", 1)
    feed.publish("approach\n{}", 1)
    assert take(sock, feed=feed, timeout=0.3) == b""
    assert feed.next_id == 0


def test_tc1669_the_wire_carries_no_image_zone_or_credential(opened, tmp_path):
    zone = "0.31,0.62;0.77,0.61;0.78,0.91"
    secret = "s3cr3t-pass"
    env = {"LAN_IP": "192.0.2.10", "CAMERAS": "door", "CAMERA_DOOR_MOTION_APPROACH_POLYGON": zone,
           "CAMERA_DOOR_MOTION_FEED_PORT": "8556", "PANEL_IP": LOCAL,
           "DOOR_RELAY_UPSTREAM_PASSWORD": secret}
    settings = cm.MotionSettings(env, "door")
    feed = opened(camera=settings.camera)
    sock = opened.client(feed)
    pump(feed)
    for cause in ef.CAUSES:
        feed.publish(cause, int(time.time() * 1000))
    feed.tick(feed.last_beat + 5.0, 180)
    data = take(sock, feed=feed)
    assert data
    text = data.decode("ascii")                      # never a byte outside ASCII: no image
    for forbidden in (zone, secret, "0.31", "polygon", "zone", "password", "rtsp", "frame\":", "jpeg", "base64"):
        assert forbidden not in text
    allowed = {"v", "type", "camera", "cause", "id", "utc_ms", "frame_age_ms"}
    for obj in lines(data):
        assert set(obj) <= allowed
        assert all(isinstance(value, (int, str)) for value in obj.values())
    assert len(data) < 1024


# ── TC-1670 connect, backlog, beat ───────────────────────────────────────────


def test_tc1670_a_connection_is_greeted_with_hello(opened):
    feed = opened()
    sock = opened.client(feed)
    assert lines(take(sock, feed=feed)) == [{"v": 1, "type": "hello", "camera": "door"}]


def test_tc1670_a_late_connection_sees_no_backlog(opened):
    feed = opened()
    first = opened.client(feed)
    pump(feed)
    for _ in range(4):
        feed.publish("approach", 1)
    late = opened.client(feed)
    pump(feed)
    assert lines(take(late, feed=feed)) == [{"v": 1, "type": "hello", "camera": "door"}]
    feed.publish("visitor", 2)
    got = lines(take(late, feed=feed))
    assert [(g["type"], g.get("cause")) for g in got] == [("motion", "visitor")]
    assert len([g for g in lines(take(first, feed=feed)) if g["type"] == "motion"]) == 5


def test_tc1670_nothing_published_with_no_client_is_replayed_to_the_next(opened):
    feed = opened()
    for _ in range(3):
        feed.publish("approach", 1)
    sock = opened.client(feed)
    assert [g["type"] for g in lines(take(sock, feed=feed))] == ["hello"]


def test_tc1671_a_beat_every_five_seconds_with_the_frame_age(opened):
    clock = Clock()
    feed = opened(mono=clock, wall=lambda: 1794013296.333)
    sock = opened.client(feed)
    pump(feed)
    take(sock, feed=feed)
    clock.now += 4.9
    feed.tick(clock.now, 180)
    assert take(sock, feed=feed, timeout=0.2) == b""
    clock.now += 0.1
    feed.tick(clock.now, 180)
    assert lines(take(sock, feed=feed)) == [{"v": 1, "type": "beat", "utc_ms": 1794013296333, "frame_age_ms": 180}]
    clock.now += 4.9
    feed.tick(clock.now, 250)
    assert take(sock, feed=feed, timeout=0.2) == b""
    clock.now += 0.1
    feed.tick(clock.now, 250)
    assert lines(take(sock, feed=feed))[0]["frame_age_ms"] == 250


# ── TC-1672 who may connect ──────────────────────────────────────────────────


def test_tc1672_a_peer_that_is_not_panel_ip_is_closed_at_accept(opened):
    feed = opened(peer="192.0.2.99")
    sock = opened.client(feed)
    assert closed_by_peer(sock, feed)
    assert feed.status()["clients"] == 0
    assert feed.sent == 0                     # not even a hello went out


def test_tc1672_the_listener_binds_the_given_address_only(opened):
    feed = opened()
    assert feed.listener.getsockname()[0] == LOCAL


def test_tc1672_a_client_gets_two_seats_and_the_third_is_closed(opened):
    feed = opened()
    one, two = opened.client(feed), opened.client(feed)
    pump(feed)
    three = opened.client(feed)
    assert closed_by_peer(three, feed)
    assert feed.status()["clients"] == ef.MAX_CLIENTS == 2
    feed.publish("approach", 1)
    for sock in (one, two):
        assert [g["type"] for g in lines(take(sock, feed=feed))] == ["hello", "motion"]


def test_tc1673_client_input_is_discarded_never_parsed(opened):
    feed = opened()
    sock = opened.client(feed)
    pump(feed)
    take(sock, feed=feed)
    sock.setblocking(True)
    sock.sendall(b'{"v":1,"type":"hello"}\n' + b"\xff\xfe not json\n" + b"x" * 5000)
    sock.setblocking(False)
    pump(feed, rounds=10)
    assert feed.status()["clients"] == 1 and feed.sent == 1
    feed.publish("approach", 1)
    assert [g["type"] for g in lines(take(sock, feed=feed))] == ["motion"]


def test_tc1674_a_client_that_closes_is_cleaned_up(opened):
    feed = opened()
    sock = opened.client(feed)
    pump(feed)
    assert feed.status()["clients"] == 1
    take(sock, feed=feed)                          # read the hello: the close below is a clean FIN, not a reset
    sock.close()
    pump(feed, rounds=10)
    assert feed.status()["clients"] == 0
    assert feed.read_fds() == [feed.listener] and feed.write_fds() == []
    feed.publish("approach", 1)               # and publishing to nobody is fine
    again = opened.client(feed)
    assert [g["type"] for g in lines(take(again, feed=feed))] == ["hello"]


# ── TC-1675 a slow client costs only itself ──────────────────────────────────


def test_tc1675_overflow_drops_only_the_client_that_fell_behind(opened):
    feed = opened()
    stuck = opened.client(feed, rcvbuf=2048)       # never read
    live = opened.client(feed)
    pump(feed)
    seen = take(live, feed=feed)
    count = 1500
    for i in range(count):
        feed.publish("visitor", i)
        if i % 50 == 0:
            seen += take(live, feed=feed, timeout=0.02, idle=0.01)
    seen += take(live, feed=feed, timeout=1.0)
    status = feed.status()
    assert status["dropped"] == 1 and status["clients"] == 1
    got = lines(seen)
    assert [g["id"] for g in got if g["type"] == "motion"] == list(range(1, count + 1))
    assert stuck
    assert len(feed.clients) == 1 and all(len(c.out) <= ef.CLIENT_BUFFER for c in feed.clients)


def test_tc1675_the_queue_never_exceeds_its_bound(opened):
    feed = opened()
    stuck = opened.client(feed, rcvbuf=2048)
    pump(feed)
    highest = 0
    for i in range(20000):
        feed.publish("visitor", i)
        for client in feed.clients:
            highest = max(highest, len(client.out))
        if not feed.clients:
            break
    assert highest <= ef.CLIENT_BUFFER
    assert feed.clients == [] and feed.dropped == 1
    assert stuck


# ── TC-1676 port 0 and failed binds ──────────────────────────────────────────


def test_tc1676_port_zero_means_no_socket_at_all(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a socket was created for port 0")
    monkeypatch.setattr(ef.socket, "socket", refuse)
    feed = ef.EventFeed("door", LOCAL, 0, LOCAL)
    assert feed.open() is False
    feed.publish("approach", 1)
    feed.tick(10 ** 6, 5)
    feed.service(set(), set())
    assert feed.read_fds() == [] and feed.write_fds() == [] and feed.listener is None
    assert feed.status() == {"port": 0, "clients": 0, "sent": 0, "dropped": 0}


def test_tc1676_a_port_already_in_use_is_a_warning_and_no_feed(opened):
    held = socket.socket()
    held.bind((LOCAL, 0))
    held.listen(1)
    try:
        logged = []
        feed = ef.EventFeed("door", LOCAL, held.getsockname()[1], LOCAL,
                            log=lambda message, warning=False: logged.append((message, warning)))
        assert feed.open() is False
        assert feed.listener is None and feed.read_fds() == []
        assert logged and logged[0][1] is True and "runs on" in logged[0][0]
        assert feed.status()["port"] == 0
    finally:
        held.close()


@pytest.mark.parametrize("host, peer, port", [
    ("203.0.113.77", LOCAL, 8556),      # an address this machine does not own: bind fails
    (LOCAL, "not-an-address", 8556),
    (LOCAL, "", 8556),
    ("", LOCAL, 8556),
    (LOCAL, LOCAL, 80),                 # under 1024
    (LOCAL, LOCAL, 70000),
])
def test_tc1676_a_bad_bind_or_setting_is_no_feed_and_never_raises(host, peer, port):
    feed = ef.EventFeed("door", host, port, peer)
    assert feed.open() is False
    feed.publish("approach", 1)
    feed.tick(10 ** 6, 5)
    assert feed.status()["port"] == 0


def test_tc1676_the_log_names_no_address(opened):
    logged = []
    feed = ef.EventFeed("door", LOCAL, free_port(), "192.0.2.99",
                        log=lambda message, warning=False: logged.append(message))
    assert feed.open()
    try:
        sock = socket.create_connection((LOCAL, feed.port))
        closed_by_peer(sock, feed)
        sock.close()
    finally:
        feed.close()
    assert logged and not any(LOCAL in m or "192.0.2" in m for m in logged)


# ── TC-1677 status counts ────────────────────────────────────────────────────


def test_tc1677_the_status_block_counts_clients_lines_and_drops(opened):
    feed = opened()
    assert set(feed.status()) == {"port", "clients", "sent", "dropped"}
    assert feed.status() == {"port": feed.port, "clients": 0, "sent": 0, "dropped": 0}
    sock = opened.client(feed)
    pump(feed)
    assert feed.status() == {"port": feed.port, "clients": 1, "sent": 1, "dropped": 0}   # the hello
    feed.publish("approach", 1)
    feed.publish("visitor", 2)
    feed.tick(feed.last_beat + 5.0, 1)
    assert feed.status()["sent"] == 4 and feed.status()["clients"] == 1
    other = opened.client(feed)
    pump(feed)
    feed.publish("bridge", 3)                     # two clients: two lines
    assert feed.status()["sent"] == 4 + 1 + 2
    assert sock and other


def test_tc1677_the_status_json_carries_the_feed_block(opened):
    feed = opened()
    record = cm.status_record({"camera": "door", "frames": 3}, {"event": 1}, feed, stopped_utc_ms=7)
    assert record["feed"] == feed.status() and record["written"] == {"event": 1}
    assert record["frames"] == 3 and record["stopped_utc_ms"] == 7
    off = cm.status_record({"camera": "door"}, {}, ef.EventFeed("door", LOCAL, 0, LOCAL))
    assert off["feed"] == {"port": 0, "clients": 0, "sent": 0, "dropped": 0}


# ── TC-1678 every signal, unthrottled, before the store ──────────────────────


def test_tc1678_every_signal_reaches_the_feed_while_the_spooler_throttles(opened):
    feed = opened()
    sock = opened.client(feed)
    pump(feed)
    take(sock, feed=feed)
    written = []
    spool = cm.SignalSpooler(lambda cause, kind, utc: written.append((cause, kind, utc)))
    for i in range(8):                             # eight renewals in under one throttle window
        cm.dispatch_signal(feed, spool, "visitor", 100.0 + i * 0.25, 5000 + i)
    got = lines(take(sock, feed=feed))
    assert [g["utc_ms"] for g in got] == [5000 + i for i in range(8)]
    assert len(written) < 8                        # the store's own throttle is unchanged


def test_tc1678_the_feed_is_published_before_the_spooler_runs(opened):
    order = []

    class Probe:
        def publish(self, cause, utc_ms):
            order.append("feed")

    spool = cm.SignalSpooler(lambda *a: order.append("store"))
    cm.dispatch_signal(Probe(), spool, "approach", 1.0, 1)
    assert order == ["feed", "store"]


def test_tc1678_run_uses_dispatch_signal_and_never_calls_the_spooler_directly():
    tree = ast.parse((STACK / "camera-motion" / "camera_motion.py").read_text(encoding="utf-8"))
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_service")
    calls = [n for n in ast.walk(run) if isinstance(n, ast.Call)]

    def name(call):
        f = call.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            return f.value.id + "." + f.attr
        return f.id if isinstance(f, ast.Name) else ""
    names = [name(c) for c in calls]
    assert names.count("dispatch_signal") == 1
    assert "spool.signal" not in names
    for needed in ("panel_feed.open", "panel_feed.service", "panel_feed.tick", "panel_feed.close",
                   "panel_feed.read_fds", "panel_feed.write_fds"):
        assert needed in names, needed


# ── TC-1679 a feed fault never touches detection or the records ──────────────

SEQUENCE = [("approach", 0.0), ("visitor", 0.4), ("visitor", 1.4), ("visitor", 2.4), ("bridge", 3.4),
            ("road-stop", 9.0), ("road-hold", 10.0), ("road-hold", 11.0), ("road-hold", 12.0),
            ("approach", 30.0), ("visitor", 31.0)]


def run_sequence(panel_feed, pace=None):
    written = []
    spool = cm.SignalSpooler(lambda cause, kind, utc: written.append((cause, kind, utc)))
    for i, (cause, mono) in enumerate(SEQUENCE):
        cm.dispatch_signal(panel_feed, spool, cause, mono, 1_000_000 + i)
        spool.tick(mono + 0.5)
        if pace:
            pace(i)
    for t in range(40, 50):
        spool.tick(float(t))
    spool.flush()
    return written


BASELINE = run_sequence(None)


def test_tc1679_the_baseline_has_events_and_renewals():
    assert [w[1] for w in BASELINE].count("event") == 3 and BASELINE[-1][1] == "renewal"


def test_tc1679_a_healthy_feed_leaves_the_spooler_output_unchanged(opened):
    feed = opened()
    opened.client(feed)
    pump(feed)
    assert run_sequence(feed) == BASELINE


def test_tc1679_a_stuck_client_leaves_the_spooler_output_unchanged(opened):
    feed = opened()
    stuck = opened.client(feed, rcvbuf=2048)
    pump(feed)
    # A client that connected and went silent: the connection stays open and is
    # never read, while the feed keeps publishing into it.
    for i in range(3000):
        feed.publish("visitor", i)
    assert run_sequence(feed) == BASELINE
    assert stuck


def test_tc1679_a_client_that_never_reads_leaves_the_spooler_output_unchanged(opened):
    feed = opened()
    opened.client(feed, rcvbuf=2048)
    pump(feed)
    repeated = []
    for _ in range(40):                            # the sequence over and over into a full pipe
        repeated = run_sequence(feed)
    assert repeated == BASELINE
    assert feed.dropped == 1                       # and that client was let go


def test_tc1679_a_bind_failure_leaves_the_spooler_output_unchanged():
    feed = ef.EventFeed("door", "203.0.113.77", 8556, LOCAL)
    assert feed.open() is False
    assert run_sequence(feed) == BASELINE


def test_tc1679_a_port_in_use_leaves_the_spooler_output_unchanged():
    held = socket.socket()
    held.bind((LOCAL, 0))
    held.listen(1)
    try:
        feed = ef.EventFeed("door", LOCAL, held.getsockname()[1], LOCAL)
        assert feed.open() is False
        assert run_sequence(feed) == BASELINE
    finally:
        held.close()


def test_tc1679_an_exception_inside_the_feed_disables_it_and_not_the_detector(opened, monkeypatch):
    feed = opened()
    opened.client(feed)
    pump(feed)

    def boom(self, line):
        raise RuntimeError("the feed broke")
    monkeypatch.setattr(ef.EventFeed, "_broadcast", boom)
    assert run_sequence(feed) == BASELINE
    assert feed.disabled and feed.listener is None and feed.clients == []
    feed.tick(10 ** 6, 1)                          # every later call is a no-op
    feed.service({1}, {1})
    assert feed.read_fds() == []


def test_tc1679_a_fault_while_serving_disables_the_feed_quietly(opened, monkeypatch):
    feed = opened()
    sock = opened.client(feed)
    monkeypatch.setattr(ef.EventFeed, "_accept", lambda self: (_ for _ in ()).throw(RuntimeError("x")))
    pump(feed, rounds=3)
    assert feed.disabled and feed.listener is None
    assert sock


def test_tc1679_a_select_failure_on_a_dead_feed_socket_is_survivable(opened):
    """read_fds never names a closed socket, so select cannot be handed one."""
    feed = opened()
    sock = opened.client(feed)
    pump(feed)
    sock.close()
    pump(feed, rounds=10)
    for fd in feed.read_fds() + feed.write_fds():
        assert fd.fileno() != -1
    feed.close()
    assert feed.read_fds() == []


# ── TC-1680 the knob ─────────────────────────────────────────────────────────


def settings(**over):
    env = {"LAN_IP": "192.0.2.10", "CAMERAS": "door", "PANEL_IP": "192.0.2.50"}
    env.update(over)
    return cm.MotionSettings(env, "door")


def test_tc1680_the_feed_port_defaults_to_off():
    s = settings()
    assert s.feed_port == 0 and s.feed_port_invalid is False and s.panel_ip == "192.0.2.50"
    assert settings(CAMERA_DOOR_MOTION_FEED_PORT="0").feed_port_invalid is False
    assert settings(CAMERA_DOOR_MOTION_FEED_PORT="").feed_port == 0


@pytest.mark.parametrize("value", ["1024", "8556", "65535"])
def test_tc1680_a_port_in_range_is_taken(value):
    s = settings(CAMERA_DOOR_MOTION_FEED_PORT=value)
    assert s.feed_port == int(value) and s.feed_port_invalid is False


@pytest.mark.parametrize("value", ["1023", "80", "65536", "99999999", "abc", "85 56", "-1", "8556.0"])
def test_tc1680_a_bad_port_is_off_with_a_flag_and_never_refuses_the_detector(value):
    s = settings(CAMERA_DOOR_MOTION_FEED_PORT=value)
    assert s.feed_port == 0 and s.feed_port_invalid is True


def test_tc1680_the_knob_is_per_camera():
    s = settings(CAMERA_OTHER_MOTION_FEED_PORT="8556")
    assert s.feed_port == 0


def test_tc1680_the_public_env_example_ships_the_feed_off():
    text = (STACK / ".env.example").read_text(encoding="utf-8")
    assert "\nCAMERA_DOOR_MOTION_FEED_PORT=0\n" in text
