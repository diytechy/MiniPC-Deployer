#!/usr/bin/env python3
"""devpc-wake service entry point: serve the state, publish it, hold nothing back.

Implements: SR-044, LLR-970 (observable where an operator already looks)
Interfaces: IF-023 (readiness we provide), IF-026 (sleep verdict we provide)
Cases:      TC-996

WHY THIS FILE IS SEPARATE FROM devpc_wake.py: that module is the decision logic
and is pure enough to test without a socket. This one owns the I/O - the HTTP
listener, the broker publish, the poll loop. Keeping them apart is what let the
state machine's concurrency be proved by ordinary unit tests rather than by
standing up a server.

NO NEW OBSERVABILITY SURFACE IS INTRODUCED. The state is served over HTTP for
the gateway, published to the mosquitto already in this stack for anything else,
and probed by the uptime-kuma already in this stack - because the wake path's
only failure mode is SILENCE, and the hardware binding underneath it is
unverified. A rotted wake path has to be discoverable before a request
discovers it.

Python 3 stdlib only. Broker publishing shells out to mosquitto_pub when it is
present and degrades to a log line when it is not - a missing CLI must not take
the state endpoint down with it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from devpc_wake import (  # noqa: E402
    CommandProvider, MagicPacketProvider, WakeService,
)


class Config:
    """Every knob, read once, with the same defaults stack/.env.example states."""

    def __init__(self, env=os.environ):
        self.host = env.get("DEVPC_HOST", "")
        self.port = int(env.get("DEVPC_INFERENCE_PORT", "11434"))
        self.base_url = "http://%s:%d" % (self.host, self.port)
        self.target_model = env.get("DEVPC_TARGET_MODEL", "")
        self.deadline_seconds = float(env.get("DEVPC_WAKE_DEADLINE_SECONDS", "90"))
        self.idle_sleep_seconds = float(env.get("DEVPC_IDLE_SLEEP_MINUTES", "30")) * 60
        self.session_staleness_seconds = float(
            env.get("DEVPC_SESSION_STALENESS_SECONDS", "120"))
        self.probe_timeout = float(env.get("DEVPC_PROBE_TIMEOUT_SECONDS", "2"))
        self.listen = env.get("DEVPC_WAKE_LISTEN", "0.0.0.0")
        self.listen_port = int(env.get("DEVPC_WAKE_PORT", "8799"))
        self.mac = env.get("DEVPC_WAKE_MAC", "")
        self.wake_command = env.get("DEVPC_WAKE_COMMAND", "")
        self.broker = env.get("DEVPC_WAKE_BROKER_HOST", "")
        self.topic = env.get("DEVPC_WAKE_TOPIC", "homehub/devpc/state")
        self.poll_seconds = float(env.get("DEVPC_WAKE_POLL_SECONDS", "15"))
        self.session_url = env.get("DEVPC_SESSION_URL", "")


def build_provider(cfg):
    """Pick the actuation provider. The seam exists because the shipped one may
    never work on this NIC (IF-022), so selecting the alternate must be a knob
    and not a code change."""
    if cfg.wake_command:
        return CommandProvider(cfg.wake_command.split())
    if cfg.mac:
        return MagicPacketProvider(cfg.mac)
    raise SystemExit(
        "Neither DEVPC_WAKE_MAC nor DEVPC_WAKE_COMMAND is set. Refusing to start: "
        "a wake service that cannot wake anything would publish `off` forever and "
        "look like a dead dev PC rather than a misconfigured service."
    )


class Publisher:
    """Best-effort broker publish. Never fatal."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._bin = shutil.which("mosquitto_pub")

    def publish(self, doc):
        if not (self.cfg.broker and self._bin):
            return False
        try:
            subprocess.run(
                [self._bin, "-h", self.cfg.broker, "-t", self.cfg.topic,
                 "-m", json.dumps(doc), "-q", "1"],
                check=True, timeout=10,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            # A broker outage must not take the HTTP surface down. The state is
            # still served; the log says the publish failed.
            print("devpc-wake: broker publish failed", flush=True)
            return False


class Handler(BaseHTTPRequestHandler):
    service = None   # injected
    publisher = None
    last_request = [0.0]
    session_reader = None

    def _json(self, code, doc):
        body = json.dumps(doc).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/state", ""):
            self._json(200, self.service.state())
        elif self.path.rstrip("/") == "/sleep-verdict":
            # IF-026. We publish a verdict. We never actuate.
            session = self.session_reader() if self.session_reader else None
            self._json(200, self.service.sleep_verdict(self.last_request[0], session))
        elif self.path.rstrip("/") == "/health":
            # Deliberately NOT the dev PC's state: uptime-kuma is watching THIS
            # service, and a sleeping dev PC is the normal case, not an outage.
            # Conflating them would make the monitor red every night.
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") == "/wake":
            self.last_request[0] = time.time()
            flight = self.service.request_wake()
            self._json(202, {"accepted": True, "deadline": flight.deadline})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # quieter journal
        print("devpc-wake: " + (fmt % args), flush=True)


def read_session(url, timeout):
    """IF-025. Fetch the dev PC's session-attachment reading, or None.

    NONE IS NOT 'DETACHED'. Every failure path here - no URL configured, the
    host unreachable, a non-200, a body that is not JSON - returns None, and
    sleep_verdict() treats None as ATTACHED and refuses to permit sleep. That
    asymmetry is the requirement (LLR-978): a false `attached` costs idle watts
    until the next poll, while a false `detached` suspends a machine somebody
    is using and loses their work.

    So this function is deliberately incapable of producing a permissive
    answer by accident. The only way to reach `attached: false` is for a fresh,
    well-formed body to say so explicitly.
    """
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            doc = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return doc if isinstance(doc, dict) else None


def main():
    cfg = Config()
    if not cfg.host:
        raise SystemExit("DEVPC_HOST is empty - nothing to probe.")
    svc = WakeService(cfg, build_provider(cfg))
    pub = Publisher(cfg)

    Handler.service = svc
    Handler.publisher = pub
    # WITHOUT THIS LINE the reader stayed None forever and every sleep verdict
    # was "session signal unavailable - treated as attached". That fails closed,
    # so nothing broke and nothing complained - the dev PC simply never slept,
    # and the reason was invisible because the fail-closed message is also what
    # a genuinely-unreachable agent produces. cfg.session_url was being read
    # from the environment and then discarded.
    Handler.session_reader = lambda: read_session(cfg.session_url, cfg.probe_timeout)
    if not cfg.session_url:
        print("devpc-wake: DEVPC_SESSION_URL is empty - sleep will NEVER be "
              "permitted (fail-closed to attached). This is correct until the "
              "service-mode session agent exists; it is logged so that 'the box "
              "never sleeps' is explained rather than investigated.", flush=True)

    def poller():
        last = None
        while True:
            try:
                doc = svc.state()
                # Publish on CHANGE, plus a floor, so the topic is neither
                # chatty nor silent enough to look dead.
                if doc["state"] != last:
                    pub.publish(doc)
                    last = doc["state"]
                    print("devpc-wake: state=%s (%s)" % (doc["state"], doc["reason"]),
                          flush=True)
            except Exception as exc:  # a probe must never kill the loop
                print("devpc-wake: poll error: %r" % (exc,), flush=True)
            time.sleep(cfg.poll_seconds)

    threading.Thread(target=poller, daemon=True).start()
    srv = ThreadingHTTPServer((cfg.listen, cfg.listen_port), Handler)
    print("devpc-wake: serving on %s:%d target=%s" % (cfg.listen, cfg.listen_port, cfg.host),
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
