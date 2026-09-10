"""Linux AF_UNIX integration tests for SR-023's bounded broker shell."""

import json
import os
from pathlib import Path
import socket
import stat
import sys
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "stack/panel-audio"))
from audio_router import AudioBroker, BoundedUnixServer, MAX_REQUEST_BYTES, UnavailableBackend
from routing import Device


def wire(method="status", params=None, generation=0):
    return (json.dumps({"id": "socket-test", "method": method,
                        "params": params or {}, "generation": generation}) + "\n").encode()


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "requires AF_UNIX")
class PanelAudioSocketTests(unittest.TestCase):
    def setUp(self):
        self.path = Path("/tmp") / ("panel-audio-%s-%s.sock" % (os.getpid(), id(self)))
        self.server = BoundedUnixServer(
            str(self.path), AudioBroker(UnavailableBackend()),
            max_clients=2, client_timeout_seconds=0.2,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 2
        while not self.path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)

    @unittest.skipUnless(os.name == "posix" and hasattr(socket, "SO_PEERCRED"),
                         "requires Linux SO_PEERCRED")
    def test_socket_mode_and_actual_peer_uid_mismatch_are_enforced(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o660)
        self.server.allowed_uid = os.getuid() + 1
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(1); client.connect(str(self.path)); client.sendall(wire())
        self.assertEqual(client.recv(65536), b"")
        client.close()

    def tearDown(self):
        self.server.close()
        self.thread.join(timeout=1)

    def exchange(self, body):
        deadline = time.monotonic() + 1
        while True:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(1)
            try:
                client.connect(str(self.path))
                break
            except ConnectionRefusedError:
                client.close()
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        client.sendall(body)
        reply = json.loads(client.recv(65_536)); client.close()
        return reply

    def test_slow_client_does_not_block_a_second_status(self):
        slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        slow.connect(str(self.path)); slow.sendall(b"{")
        self.assertFalse(self.exchange(wire())["result"]["available"])
        slow.close()

    def test_oversized_wire_request_is_refused(self):
        reply = self.exchange(b"{" + b"x" * MAX_REQUEST_BYTES + b"}\n")
        self.assertEqual("request_too_large", reply["error"]["code"])

    def test_mutation_is_authorization_gated_over_the_real_socket(self):
        reply = self.exchange(wire("discover"))
        self.assertEqual("authorization_required", reply["error"]["code"])

    def test_unavailable_telemetry_uses_the_bounded_if015_wire_shape(self):
        reply = self.exchange(wire("telemetry"))
        self.assertTrue(reply["ok"])
        self.assertEqual({"available": False}, reply["result"])
        self.assertNotIn("samples", json.dumps(reply))


if __name__ == "__main__":
    unittest.main()
