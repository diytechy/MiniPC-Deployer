#!/usr/bin/env python3
"""homehub-watch - notice, within minutes, that a job which must stay up is not.

WHY THIS EXISTS (SN-048). The offsite sync client was down for most of
2026-09-19..10-21 and nothing noticed for a month. Every watch here is a row in
watch.table (one table, in the payload, edited as data); this script reads it
every 5 minutes (homehub-watch.timer), judges each row, and alerts once when a
watch fires and once when it clears.

THE KINDS (watch.table header has the row syntax):
  process        absent for THRESHOLD, counted on the monotonic clock within a
                 boot. A reboot restarts the count: a client that has not come
                 back 15 minutes after boot is the alert, not the reboot.
  unit-success   no successful finish for THRESHOLD. systemd's record
                 (Result, ExecMainStatus, ExecMainExitTimestamp) is read every
                 run and the newest success is kept in the state file, so the
                 age survives a reboot. A unit whose LAST run failed has not
                 succeeded since that run started, so a failure older than the
                 threshold is stale at once; a unit with no record at all is
                 judged from when this watch first saw it.
  docker-health  each container `unhealthy` for THRESHOLD, one alert each.
                 `docker ps` runs under a timeout; when docker cannot answer the
                 row is "unknown" and nothing changes (the liveness probe owns
                 "docker is not answering").
  docker-restarts each container whose RestartCount rose by RESTART_LOOP_MIN or
                 more within THRESHOLD, one alert each; clear once a whole
                 THRESHOLD passes with fewer. A crash-looping container is
                 INVISIBLE to docker-health: between deaths it is "starting",
                 never "unhealthy", and `docker ps` says "Up". Immich ran like
                 that for 2,327 restarts in 2026-08 (open-items C58) and only
                 the counter told the truth (LLR-1211).

NEVER ACTS. It reads /proc, asks systemd and docker, and alerts. It starts and
stops nothing, including the synthetic drill process.

State: /var/lib/homehub-monitor/watch-state.json. Status (what each row looked
like, with `ts`): /var/lib/homehub-monitor/watch-status.json.

Exit: 0 when the table was read and every row judged (fired or not); 1 when the
table is missing or unreadable.

Implements: SR-088, LLR-1156, LLR-1157, LLR-1158, LLR-1211
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import monitor_common as mc  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
DEFAULT_TABLE = os.path.join(HERE, "watch.table")
KINDS = ("process", "unit-success", "docker-health", "docker-restarts")
# docker-restarts: this many restarts inside the row's window is a loop. Three,
# because one restart is a crash that recovered and two can be a deploy; the
# loop C58 recorded added ~20 every five minutes.
RESTART_LOOP_MIN = 3
# Every alert of one run must have finished this long after the run started;
# with monitor_common.SAVE_RESERVE_SEC it fits the unit's TimeoutStartSec=120,
# however many containers turn unhealthy at once while ntfy (itself a
# container) hangs.
ALERT_DEADLINE_SEC = 100
SUBST = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


# ── the table (pure) ─────────────────────────────────────────────────────────


def parse_threshold(text: str) -> Optional[int]:
    """`Nm` / `Nh` -> seconds; None when malformed or zero."""
    m = re.fullmatch(r"([0-9]{1,5})([mh])", text)
    if not m or int(m.group(1)) == 0:
        return None
    return int(m.group(1)) * (60 if m.group(2) == "m" else 3600)


def _subst(text: str, env: Dict[str, str]) -> str:
    return SUBST.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), text)


def condition_holds(when: str, env: Dict[str, str]) -> bool:
    """Every comma-separated KEY, KEY=VALUE, KEY!=VALUE term holds (`-` = always)."""
    if when in ("", "-"):
        return True
    for term in when.split(","):
        if "!=" in term:
            key, _, value = term.partition("!=")
            if env.get(key, "") == value:
                return False
        elif "=" in term:
            key, _, value = term.partition("=")
            if env.get(key, "") != value:
                return False
        elif not env.get(term, ""):
            return False
    return True


def parse_table(text: str, env: Dict[str, str]) -> Tuple[List[dict], List[str]]:
    """The rows that apply on this box, and a list of problems.

    Contract:
      Inputs:  text: watch.table; env: the stack .env.
      Outputs: ([{"name","kind","target","threshold_s"}], [problem strings]).
               A malformed row is a problem, never a guess. A row whose `when`
               does not hold, or that HOMEHUB_WATCH_DISABLE names, is left out.
    Implements: LLR-1156
    """
    rows, problems, seen = [], [], set()
    disabled = set(mc.knob_words(env, "HOMEHUB_WATCH_DISABLE", ""))
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if not line:
            continue
        cols = line.split()
        if len(cols) not in (4, 5):
            problems.append(
                "line %d: expected 4 or 5 columns, got %d" % (number, len(cols))
            )
            continue
        name, kind, target, threshold = cols[:4]
        when = cols[4] if len(cols) == 5 else "-"
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", name) or name in seen:
            problems.append("line %d: bad or duplicate name %r" % (number, name))
            continue
        seen.add(name)
        if kind not in KINDS:
            problems.append("line %d: unknown kind %r" % (number, kind))
            continue
        if name in disabled or not condition_holds(when, env):
            continue
        target, seconds = _subst(target, env), parse_threshold(_subst(threshold, env))
        if seconds is None or not target:
            problems.append(
                "line %d (%s): empty target or bad threshold %r"
                % (number, name, threshold)
            )
            continue
        rows.append(
            {"name": name, "kind": kind, "target": target, "threshold_s": seconds}
        )
    return rows, problems


# ── the judgements (pure; each takes what was observed and the row's state) ──


def judge_process(
    row: dict, st: dict, comms: Optional[List[str]], boot: str, mono: float
) -> Tuple[Optional[bool], dict, str]:
    """(bad|None if unknown, new state, description)."""
    st = dict(st)
    if comms is None:
        return None, st, "cannot read /proc"
    want = row["target"][:15]
    if (
        st.get("boot_id") != boot
        or st.get("since_mono") is None
        or mono < st["since_mono"]
    ):
        st.update(boot_id=boot, since_mono=mono)
    if want in comms:
        st["since_mono"] = mono
        return False, st, "running"
    absent = int(mono - st["since_mono"])
    return absent >= row["threshold_s"], st, "absent for %ds" % absent


def parse_systemctl_show(text: str) -> Dict[str, str]:
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key] = value
    return out


def _unix(value: str) -> Optional[float]:
    m = re.fullmatch(r"@([0-9]+)(?:\.[0-9]+)?", (value or "").strip())
    return float(m.group(1)) if m else None


def judge_unit_success(
    row: dict, st: dict, show: Optional[Dict[str, str]], now: float
) -> Tuple[Optional[bool], dict, str]:
    """Age of the newest observed success against the threshold (wall clock).

    `show` is `systemctl show --timestamp=unix` output parsed, or None when
    systemctl could not answer (unknown: nothing changes).
    """
    st = dict(st)
    if show is None:
        return None, st, "systemctl did not answer"
    st.setdefault("first_seen", now)
    if st["first_seen"] > now + 60:  # the clock went backwards past it
        st["first_seen"] = now
    exit_ts = _unix(show.get("ExecMainExitTimestamp", ""))
    start_ts = _unix(show.get("ExecMainStartTimestamp", ""))
    succeeded = (
        show.get("Result") == "success" and show.get("ExecMainStatus", "") == "0"
    )
    if succeeded and exit_ts is not None:
        st["last_success"] = max(st.get("last_success") or 0, exit_ts)
    baseline = st["first_seen"]
    if (
        not succeeded
        and start_ts is not None
        and show.get("ActiveState") != "activating"
    ):
        # The last run failed: no success since it STARTED.
        baseline = min(baseline, start_ts)
    reference = st.get("last_success") or baseline
    age = int(now - reference)
    what = (
        ("last success %ds ago" % age)
        if st.get("last_success")
        else ("no success seen for %ds" % age)
    )
    if show.get("LoadState") == "not-found":
        what += " (unit not installed)"
    return age > row["threshold_s"], st, what


def judge_containers(
    row: dict, st: dict, unhealthy: Optional[List[str]], mono: float, boot: str
) -> Tuple[Optional[Dict[str, bool]], dict, str]:
    """Per-container verdicts {name: bad}; None when docker could not answer."""
    st = dict(st)
    if unhealthy is None:
        return None, st, "docker did not answer"
    if st.get("boot_id") != boot:
        st = {"boot_id": boot, "since": {}}
    since = dict(st.get("since", {}))
    verdicts = {}
    for name in unhealthy:
        since.setdefault(name, mono)
        if mono < since[name]:
            since[name] = mono
        verdicts[name] = mono - since[name] >= row["threshold_s"]
    for name in list(since):
        if name not in unhealthy:
            del since[name]
            verdicts[name] = False
    st["since"] = since
    return (
        verdicts,
        st,
        ("unhealthy: " + ", ".join(sorted(unhealthy))) if unhealthy else "all healthy",
    )


def judge_restarts(
    row: dict, st: dict, counts: Optional[Dict[str, int]], mono: float, boot: str
) -> Tuple[Optional[Dict[str, bool]], dict, str]:
    """Per-container verdicts {name: looping}; None when docker could not answer.

    `counts` is {container: RestartCount} for the containers docker lists now.
    Each container keeps the (monotonic time, count) samples of the last window;
    it is looping when the count rose by RESTART_LOOP_MIN or more since the
    oldest sample in that window. A count that went DOWN is a recreated
    container (a fresh container starts at 0), so its history restarts; a new
    boot restarts every history. A container no longer listed is cleared.
    Pure: no I/O.
    Implements: SR-088, LLR-1211
    """
    st = dict(st)
    if counts is None:
        return None, st, "docker did not answer"
    if st.get("boot_id") != boot:
        st = {"boot_id": boot, "samples": {}}
    window = row["threshold_s"]
    samples = {k: [list(x) for x in v] for k, v in st.get("samples", {}).items()}
    verdicts, rising = {}, []
    for name, count in counts.items():
        history = [x for x in samples.get(name, []) if 0 <= mono - x[0] <= window]
        if history and count < history[-1][1]:
            history = []
        history.append([mono, count])
        samples[name] = history
        rise = count - history[0][1]
        verdicts[name] = rise >= RESTART_LOOP_MIN
        if rise > 0:
            rising.append("%s +%d" % (name, rise))
    for name in list(samples):
        if name not in counts:
            del samples[name]
            verdicts[name] = False
    st["samples"] = samples
    return (
        verdicts,
        st,
        ("restarts in the last %dm: %s" % (window // 60, ", ".join(sorted(rising))))
        if rising else "no restarts",
    )


# ── the shell ────────────────────────────────────────────────────────────────


def systemctl_show(unit: str) -> Optional[Dict[str, str]]:
    props = "Result,ExecMainStatus,ExecMainExitTimestamp,ExecMainStartTimestamp,ActiveState,LoadState"
    done = mc.run_bounded(
        ["systemctl", "show", "--timestamp=unix", "-p", props, unit], 20, 65536
    )
    if not done.ok:
        return None
    return parse_systemctl_show(done.output.decode("utf-8", "replace"))


def docker_unhealthy() -> Optional[List[str]]:
    done = mc.run_bounded(
        ["docker", "ps", "--filter", "health=unhealthy", "--format", "{{.Names}}"],
        20,
        65536,
    )
    if not done.ok:
        return None
    return [
        n
        for n in done.output.decode("utf-8", "replace").split()
        if re.fullmatch(r"[A-Za-z0-9_.-]+", n)
    ]


def docker_restart_counts() -> Optional[Dict[str, int]]:
    """{name: RestartCount} for every container, stopped ones included (`-a`):
    one caught between a death and its restart must not drop out of the
    history and clear; a stopped one's count simply stops rising. None when
    docker cannot answer within its bound."""
    ids = mc.run_bounded(["docker", "ps", "-a", "-q", "--no-trunc"], 20, 65536)
    if not ids.ok:
        return None
    wanted = [i for i in ids.output.decode("utf-8", "replace").split() if re.fullmatch(r"[0-9a-f]{12,64}", i)]
    if not wanted:
        return {}
    done = mc.run_bounded(
        ["docker", "inspect", "--format", "{{.Name}} {{.RestartCount}}"] + wanted, 20, 262144
    )
    if not done.ok:
        return None
    counts = {}
    for line in done.output.decode("utf-8", "replace").splitlines():
        m = re.fullmatch(r"/?([A-Za-z0-9_.-]+) ([0-9]+)", line.strip())
        if m:
            counts[m.group(1)] = int(m.group(2))
    return counts


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="HomeHub watched jobs (SN-048)")
    p.add_argument(
        "--table", default=os.environ.get("HOMEHUB_WATCH_TABLE", DEFAULT_TABLE)
    )
    p.add_argument(
        "--state-dir",
        default=os.environ.get("HOMEHUB_MONITOR_STATE_DIR", mc.DEFAULT_STATE_DIR),
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
    try:
        with open(args.table, encoding="utf-8") as handle:
            rows, problems = parse_table(handle.read(), env)
    except OSError as exc:
        mc.log("watch: cannot read %s (%s) - nothing watched" % (args.table, exc), True)
        return 1
    for problem in problems:
        mc.log("watch: table problem: " + problem, True)

    state_path = os.path.join(args.state_dir, "watch-state.json")
    state = mc.load_json(state_path, {})
    rows_state, episodes = state.get("rows", {}), state.get("episodes", {})
    now, mono = time.time(), time.monotonic()
    boot = mc.read_boot_id(args.proc_root)
    comms = mc.process_comms(args.proc_root)
    repeat = mc.knob_int(env, "HOMEHUB_ALERT_REPEAT_SEC", 1800, 0, 86400)

    def sender(key, prio, title, body):
        return mc.send_alert(key, prio, title, body, args.env_file, args.state_dir, helper=args.alert_helper,
                             repeat_sec=repeat, log_tag="homehub-watch", budget=budget)  # fmt: skip

    status, live_keys, pending = [], set(), []
    for row in rows:
        name, st = row["name"], rows_state.get(row["name"], {})
        if row["kind"] in ("docker-health", "docker-restarts"):
            looping = row["kind"] == "docker-restarts"
            if looping:
                verdicts, st, what = judge_restarts(row, st, docker_restart_counts(), mono, boot)
            else:
                verdicts, st, what = judge_containers(
                    row, st, docker_unhealthy(), mono, boot
                )
            if verdicts is not None:
                # A container that recovered (or vanished) while its clear could
                # not be delivered still owes that clear: keep stepping it.
                prefix = "watch-%s-" % name
                for key in episodes:
                    if key.startswith(prefix) and key[len(prefix) :] not in verdicts:
                        verdicts[key[len(prefix) :]] = False
            for cname, bad in (verdicts or {}).items():
                key = "watch-%s-%s" % (name, cname)
                live_keys.add(key)
                if looping:
                    fire = ("high", "HomeHub: container %s is restarting in a loop (%d+ restarts in %dm)"
                            % (cname, RESTART_LOOP_MIN, row["threshold_s"] // 60),
                            "%s was restarted at least %d times in %dm while `docker ps` can still say Up. Look at: "
                            "docker inspect -f '{{.RestartCount}}' %s; docker logs --tail 50 %s"
                            % (cname, RESTART_LOOP_MIN, row["threshold_s"] // 60, cname, cname))
                    clear = ("HomeHub: container %s stopped restarting" % cname,
                             "%s has not restarted %d times in the last %dm." % (cname, RESTART_LOOP_MIN, row["threshold_s"] // 60))
                else:
                    fire = ("high", "HomeHub: container %s unhealthy for %dm" % (cname, row["threshold_s"] // 60),
                            "docker reports %s unhealthy. Look at: docker inspect --format '{{json .State.Health}}' %s"
                            % (cname, cname))
                    clear = ("HomeHub: container %s is healthy again" % cname, "%s is no longer unhealthy." % cname)
                pending.append(mc.plan_episode(episodes, key, bad, fire, clear))  # fmt: skip
            bad = None if verdicts is None else any(verdicts.values())
        else:
            if row["kind"] == "process":
                bad, st, what = judge_process(row, st, comms, boot, mono)
                fire_title = "HomeHub: %s is not running (watch %s)" % (
                    row["target"],
                    name,
                )
                clear_title = "HomeHub: %s is running again (watch %s)" % (
                    row["target"],
                    name,
                )
            else:
                bad, st, what = judge_unit_success(
                    row, st, systemctl_show(row["target"]), now
                )
                fire_title = "HomeHub: %s has not succeeded in %s (watch %s)" % (
                    row["target"], "%dh" % (row["threshold_s"] // 3600) if row["threshold_s"] >= 3600
                    else "%dm" % (row["threshold_s"] // 60), name)  # fmt: skip
                clear_title = "HomeHub: %s succeeded again (watch %s)" % (
                    row["target"],
                    name,
                )
            key = "watch-" + name
            live_keys.add(key)
            if bad is not None:
                pending.append(mc.plan_episode(episodes, key, bad,
                               ("high", fire_title, "%s: %s. Look at: journalctl -u homehub-watch; the watch table "
                                "is stack/monitoring/watch.table" % (row["target"], what)),
                               (clear_title, "%s: %s." % (row["target"], what))))  # fmt: skip
        rows_state[name] = st
        verdict = "unknown" if bad is None else ("FIRING" if bad else "ok")
        mc.log("watch: %-17s %-8s %s" % (name, verdict, what), warning=bool(bad))
        status.append(
            {"name": name, "kind": row["kind"], "verdict": verdict, "detail": what}
        )
    mc.drop_stale_episodes(episodes, live_keys)
    for name in list(rows_state):
        if name not in {r["name"] for r in rows}:
            del rows_state[name]
    # State (every owed alert pending) and status are written before any alert
    # is attempted, and the state again after each attempt: a slow ntfy can cost
    # this run its alerts, never its record.
    state = {"rows": rows_state, "episodes": episodes}
    saved = {"ok": mc.save_json_atomic(state_path, state)}
    mc.save_json_atomic(os.path.join(args.state_dir, "watch-status.json"),
                        {"ts": int(now), "rows": status, "problems": problems})  # fmt: skip

    def checkpoint():
        saved["ok"] = mc.save_json_atomic(state_path, state)

    mc.deliver_pending(episodes, pending, sender, checkpoint)
    if not saved["ok"]:
        mc.log("watch: WARN could not save %s" % state_path, True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
