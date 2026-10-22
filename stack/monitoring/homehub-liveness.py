#!/usr/bin/env python3
"""homehub-liveness - is the hub still serving, and if not, what was it doing?

WHY THIS EXISTS (SN-048). The 2026-10-04 wedge left nothing behind. This probe
asks the three questions a household notices first - does DNS answer, does the
front door answer, does docker answer - every 60 s (homehub-liveness.timer).
After HOMEHUB_LIVENESS_FAILURES consecutive failed runs (default 3) it captures
ONE evidence bundle for the incident (homehub_evidence) and then alerts. The
incident ends at the first fully passing run, which alerts once more.

THE CHECKS, each under its own timeout:
  dns     a UDP query to Technitium on HOMEHUB_LIVENESS_DNS_SERVER (default
          LAN_IP), port 53, for HOMEHUB_LIVENESS_DNS_NAME (default
          <TRACKER_SUBDOMAIN>.<DOMAIN>, a name the hub serves itself). NOERROR
          or NXDOMAIN is an answer; SERVFAIL, REFUSED and silence are not.
  https   a TLS connection to Caddy on HOMEHUB_LIVENESS_HTTPS_HOST (default
          127.0.0.1) port HOMEHUB_LIVENESS_HTTPS_PORT (default 443), SNI the same
          name, and one HEAD request. ANY HTTP status line is an answer: the
          question is whether the listener is serving, not what the site says.
          The certificate is not verified; that is not this probe's question.
  docker  `docker ps -q` under a 15 s timeout.

NEVER ACTS. No restart, no kill, no reboot; homehub-recoveryd (opt-in, off) is
the only thing on this box that may act, and this probe is not on its path.

NETWORK. Only those two named local targets, plus ntfy through the alert
helper. Pointing a knob at a target that cannot answer (an unused local port)
is how the drill exercises the whole path (stack/monitoring/README.md).

State: /var/lib/homehub-monitor/liveness-state.json; status (last run, with
`ts`): liveness-status.json beside it. Bundles: /var/lib/homehub-evidence/.

Exit: 0 whether the hub passed or failed (a failure is a finding and is
alerted); the unit fails only if this script itself breaks.

Implements: SR-089, LLR-1159, LLR-1160, LLR-1161
"""

from __future__ import annotations

import argparse
import os
import random
import re
import socket
import ssl
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import homehub_evidence as ev  # noqa: E402
import monitor_common as mc  # noqa: E402

# Every alert of one run must have finished this long after the run started -
# after the checks and any evidence capture (its own 90 s deadline) - and with
# monitor_common.SAVE_RESERVE_SEC it fits the unit's TimeoutStartSec=180.
ALERT_DEADLINE_SEC = 160


def targets_from_env(env: Dict[str, str]) -> dict:
    """The probe's targets and thresholds from the stack .env (pure)."""
    domain = env.get("DOMAIN", "")
    sub = env.get("TRACKER_SUBDOMAIN", "") or "tracker"
    default_name = "%s.%s" % (sub, domain) if domain else "localhost"
    name = env.get("HOMEHUB_LIVENESS_DNS_NAME", "") or default_name
    return {
        "dns_server": env.get("HOMEHUB_LIVENESS_DNS_SERVER", "")
        or env.get("LAN_IP", "")
        or "127.0.0.1",
        "dns_name": name,
        "https_host": env.get("HOMEHUB_LIVENESS_HTTPS_HOST", "") or "127.0.0.1",
        "https_port": mc.knob_int(env, "HOMEHUB_LIVENESS_HTTPS_PORT", 443, 1, 65535),
        "https_name": env.get("HOMEHUB_LIVENESS_HTTPS_NAME", "") or name,
        "failures": mc.knob_int(env, "HOMEHUB_LIVENESS_FAILURES", 3, 1, 60),
        "units": mc.knob_words(
            env, "HOMEHUB_EVIDENCE_JOURNAL_UNITS", ev.DEFAULT_JOURNAL_UNITS
        ),
        "tags": mc.knob_words(
            env, "HOMEHUB_EVIDENCE_JOURNAL_TAGS", ev.DEFAULT_JOURNAL_TAGS
        ),
    }


# ── the checks (thin; each returns (ok, detail)) ─────────────────────────────


def check_dns(server: str, name: str, timeout: float = 3.0) -> Tuple[bool, str]:
    if not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}", server) or not re.fullmatch(
        r"[A-Za-z0-9.-]{1,253}", name
    ):
        return False, "bad target %r / %r" % (server, name)
    qid = random.randint(0, 0xFFFF)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(mc.dns_query_packet(name, qid), (server, 53))
            reply, _ = sock.recvfrom(4096)
    except OSError as exc:
        return False, "no answer from %s: %s" % (server, exc)
    ok, why = mc.dns_answer_ok(reply, qid)
    return ok, "%s %s -> %s" % (server, name, why)


def check_https(
    host: str, port: int, name: str, timeout: float = 5.0
) -> Tuple[bool, str]:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=name) as tls:
                tls.settimeout(timeout)
                tls.sendall(
                    (
                        "HEAD / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n"
                        % name
                    ).encode("ascii")
                )
                head = tls.recv(256)
    except (OSError, ssl.SSLError, UnicodeError) as exc:
        return False, "%s:%d (%s): %s" % (host, port, name, exc)
    m = re.match(rb"HTTP/[0-9.]+ ([0-9]{3})", head)
    if not m:
        return False, "%s:%d answered without an HTTP status line" % (host, port)
    return True, "%s:%d (%s) -> HTTP %s" % (host, port, name, m.group(1).decode())


def check_docker(runner=mc.run_bounded) -> Tuple[bool, str]:
    done = runner(["docker", "ps", "-q"], 15, 65536)
    if done.timed_out:
        return False, "docker ps did not return in 15 s"
    if not done.ok:
        return False, "docker ps failed (%s)" % (done.error or "exit %s" % done.rc)
    return True, "docker ps answered (%d containers)" % len(done.output.split())


# ── the incident state machine (pure) ────────────────────────────────────────


def advance(state: dict, all_ok: bool, threshold: int) -> Tuple[dict, Optional[str]]:
    """One run's verdict into the incident state.

    Outputs: (new_state, event) with event "open" (capture a bundle now: the
    threshold was just reached and no bundle exists for this incident), "close"
    (the first passing run after an incident) or None.
    Implements: LLR-1160
    """
    st = dict(state or {})
    st.setdefault("failures", 0)
    st.setdefault("incident", False)
    if all_ok:
        was = st["incident"]
        st.update(failures=0, incident=False)
        return st, ("close" if was else None)
    st["failures"] += 1
    if not st["incident"] and st["failures"] >= threshold:
        st["incident"] = True
        st["seq"] = int(st.get("seq", 0)) + 1
        return st, "open"
    return st, None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="HomeHub liveness probe with evidence (SN-048)"
    )
    p.add_argument(
        "--state-dir",
        default=os.environ.get("HOMEHUB_MONITOR_STATE_DIR", mc.DEFAULT_STATE_DIR),
    )
    p.add_argument(
        "--evidence-dir",
        default=os.environ.get("HOMEHUB_EVIDENCE_DIR", ev.DEFAULT_ROOT),
    )
    p.add_argument(
        "--env-file",
        default=os.environ.get("HOMEHUB_MONITOR_ENV_FILE", mc.DEFAULT_ENV_FILE),
    )
    p.add_argument(
        "--alert-helper",
        default=os.environ.get("HOMEHUB_ALERT_HELPER", mc.DEFAULT_ALERT_HELPER),
    )
    p.add_argument(
        "--proc-root", default=os.environ.get("HOMEHUB_MONITOR_PROC_ROOT", "/proc")
    )
    args = p.parse_args(argv)
    budget = mc.AlertBudget(ALERT_DEADLINE_SEC)
    env = mc.read_knobs(args.env_file)
    t = targets_from_env(env)

    results: Dict[str, Tuple[bool, str]] = {
        "dns": check_dns(t["dns_server"], t["dns_name"]),
        "https": check_https(t["https_host"], t["https_port"], t["https_name"]),
        "docker": check_docker(),
    }
    all_ok = all(ok for ok, _ in results.values())
    failing: List[str] = [k for k, (ok, _) in results.items() if not ok]
    # One line a minute while healthy (it runs 1440 times a day); every check
    # gets its own line, at warning priority for a failure, once anything fails.
    if all_ok:
        mc.log(
            "liveness: ok - "
            + "; ".join("%s: %s" % (k, d) for k, (_, d) in results.items())
        )
    else:
        for key, (ok, detail) in results.items():
            mc.log(
                "liveness: %-6s %s %s" % (key, "ok  " if ok else "FAIL", detail),
                warning=not ok,
            )

    state_path = os.path.join(args.state_dir, "liveness-state.json")
    state = mc.load_json(state_path, {})
    inc, event = advance(state.get("incident", {}), all_ok, t["failures"])
    episodes = state.get("episodes", {})
    repeat = mc.knob_int(env, "HOMEHUB_ALERT_REPEAT_SEC", 1800, 0, 86400)

    def sender(key, prio, title, body):
        return mc.send_alert(key, prio, title, body, args.env_file, args.state_dir, helper=args.alert_helper,
                             repeat_sec=repeat, log_tag="homehub-liveness", budget=budget)  # fmt: skip

    if event == "open":
        checks = {k: {"ok": ok, "detail": d} for k, (ok, d) in results.items()}
        checks["consecutive_failures"] = inc["failures"]
        manifest = ev.capture(args.evidence_dir, inc["seq"],
                              ev.capture_plan(args.proc_root, t["units"], t["tags"]), checks)  # fmt: skip
        inc["bundle"] = manifest.get("path", "(none: %s)" % manifest.get("error"))
        inc["bundle_complete"] = bool(manifest.get("complete"))
        mc.log("liveness: INCIDENT - %d consecutive failed runs (%s); evidence %s%s"
               % (inc["failures"], ", ".join(failing), inc["bundle"],
                  "" if inc["bundle_complete"] else " (INCOMPLETE - see its manifest.json)"), True)  # fmt: skip
        for problem in manifest.get("prune", {}).get("errors", []):
            mc.log("liveness: evidence pruning failed: " + problem, True)
    # Stepped on EVERY run, so a fire or a clear that ntfy did not take is
    # retried by the next run rather than lost.
    fire_body = "%d consecutive liveness runs failed: %s. Evidence: %s%s" % (
        inc.get("failures", 0), "; ".join("%s: %s" % (k, results[k][1]) for k in failing) or "see journal",
        inc.get("bundle", "?"), "" if inc.get("bundle_complete", True) else " (incomplete)")  # fmt: skip
    pending = [mc.plan_episode(
        episodes, "liveness", bool(inc.get("incident")),
        ("urgent", "HomeHub: liveness failing (%s)" % (", ".join(failing) or "see journal"), fire_body),
        ("HomeHub: liveness restored", "DNS, HTTPS and docker all answered again. Evidence from the "
         "incident: %s" % inc.get("bundle", "?")),
    )]  # fmt: skip
    # State (the owed alert pending) and status are written before the alert is
    # attempted, and the state again after it: a slow ntfy can cost this run its
    # alert, never its incident record.
    state.update(incident=inc, episodes=episodes)
    saved = {"ok": mc.save_json_atomic(state_path, state)}
    mc.save_json_atomic(os.path.join(args.state_dir, "liveness-status.json"), {
        "ts": int(time.time()), "ok": all_ok,
        "checks": {k: {"ok": ok, "detail": d} for k, (ok, d) in results.items()},
        "incident": bool(inc.get("incident")), "bundle": inc.get("bundle"),
    })  # fmt: skip

    def checkpoint():
        saved["ok"] = mc.save_json_atomic(state_path, state)

    mc.deliver_pending(episodes, pending, sender, checkpoint)
    if not saved["ok"]:
        mc.log("liveness: WARN could not save %s" % state_path, True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
