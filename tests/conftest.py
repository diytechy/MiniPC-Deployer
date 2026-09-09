"""Shared test helpers.

ONE RESPONSIBILITY: stand up a real HTTP server on loopback so the feeders'
egress guards can be tested against real sockets.

WHY A REAL SOCKET AND NOT A STUBBED `urlopen`. The cross-review defect these
helpers exist for is that `urllib.request.urlopen` follows redirects and
honours `http_proxy` — behaviour that lives entirely in urllib's default
opener. A test that patched `urlopen` would happily "prove" a redirect was
refused while the real code followed it, which is a vacuous assertion of
exactly the kind this build keeps finding.
"""

import contextlib
import http.server
import threading


@contextlib.contextmanager
def loopback_server(respond):
    """Run an HTTP server on 127.0.0.1 and yield (base_url, seen).

    `respond(handler)` writes the reply. `seen` collects one dict per request
    that ARRIVED — path, headers, body — which is how "the second host received
    nothing at all" is asserted rather than assumed.
    """
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def _record(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen.append({"path": self.path,
                         "headers": dict(self.headers),
                         "body": self.rfile.read(length) if length else b""})
            respond(self)

        do_GET = do_POST = _record

        def log_message(self, *args):
            pass                        # keep pytest output clean

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def plain_200(handler):
    """The uninteresting reply, for the servers whose CONTENT is not the point."""
    handler.send_response(200)
    handler.send_header("Content-Length", "2")
    handler.end_headers()
    handler.wfile.write(b"{}")


def redirect_to(target):
    """A responder that answers 302 pointing at `target`."""
    def respond(handler):
        handler.send_response(302)
        handler.send_header("Location", target)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
    return respond
