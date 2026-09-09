"""SN-015's truth table, asserted on the ONE function that decides it.

WHY THIS FILE IS SHAPED LIKE THIS. The acceptance criterion for occupancy power
is that *one knob set drives both the backlight and the suspend/RTC path*. The
way that is made true in the code is that `decide()` returns BOTH halves of the
answer in one record, and `wall-sleep.sh` applies that record without re-testing
presence. So the way it is made true in the tests is that every case below reads
BOTH `backlight` and `power` out of a single call — if the two halves ever came
from different logic, no assertion here could pass by accident.

WHAT THESE TESTS DO NOT PROVE, said plainly because this repo has been bitten by
the opposite claim: nothing here touches a real backlight, a real RTC or a real
suspend. That is `stack/autoinstall/wall/tests/occupancy-power.test.sh`, which
runs the shell against a fake sysfs tree and fake `rtcwake`/`systemctl` binaries
and asserts the ARTIFACTS — a brightness file containing 0, a wakealarm epoch
that resolves to 06:45, a recorded suspend call. A green unit test here says the
decision is right; it says nothing about whether anything was written.

Verifies: SR-020, LLR-003 (TC-003)
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "stack" / "autoinstall" / "wall" / "wall-occupancy.py"
)


def _load():
    """Load the decider by path — its filename is hyphenated, so it is a script
    rather than an importable module name (same as wall-media-manifest.py)."""
    spec = importlib.util.spec_from_file_location("wall_occupancy", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


occ = _load()

# The ratified schedule, in the units decide() takes. 06:45 is SLEEP_END and is
# ALSO the on-period start and the RTC wake target — one value, three jobs.
ON_START = 6 * 60 + 45     # 06:45, SLEEP_END
ON_END = 22 * 60           # 22:00, SLEEP_START
NOON = 12 * 60
MIDNIGHT_THIRTY = 30
HOUR = 3600
NOW = 1_757_000_000        # any fixed epoch; only differences matter


def _decide(**kwargs):
    """decide() with the ratified schedule and the common defaults filled in."""
    args = dict(
        now_epoch=NOW,
        minute_of_day=NOON,
        presence=occ.PRESENT,
        absent_since=None,
        absence_enabled=True,
        absence_timeout_min=60,
        on_start=ON_START,
        on_end=ON_END,
    )
    args.update(kwargs)
    return occ.decide(**args)


# ── the three states SN-015 names, and nothing collapsed between them ────────


@pytest.mark.smoke
def test_detection_disabled_leaves_both_halves_alone_sr020():
    """State 3: with the knob off, occupancy writes NEITHER half.

    "Unchanged" and not "on" is the whole point: `SLEEP_MODE=backlight` turns
    the screen off at 22:00 today, and an occupancy tick that helpfully turned
    it back on would be this feature silently rewriting the schedule it was
    explicitly told to leave alone.
    """
    for minute in (MIDNIGHT_THIRTY, NOON, ON_END, ON_END + 1):
        for presence in (occ.PRESENT, occ.ABSENT):
            got = _decide(
                absence_enabled=False,
                minute_of_day=minute,
                presence=presence,
                absent_since=NOW - 10 * HOUR,
            )
            assert got["backlight"] == "unchanged", got
            assert got["power"] == "stay", got


@pytest.mark.smoke
def test_absent_an_hour_outside_the_on_period_suspends_sr020():
    """State 1: the only combination in the whole table that suspends."""
    got = _decide(minute_of_day=MIDNIGHT_THIRTY, presence=occ.ABSENT,
                  absent_since=NOW - 60 * 60)
    assert got["power"] == "suspend", got
    # BOTH halves come from this one record — the backlight is off in the same
    # answer that ordered the suspend, not from a separate presence test.
    assert got["backlight"] == "off", got
    assert got["on_period"] is False


@pytest.mark.smoke
def test_presence_inside_the_on_period_never_suspends_sr020():
    """State 2, and the walk-in case: lit, awake, nothing to resume from."""
    got = _decide(minute_of_day=NOON, presence=occ.PRESENT, absent_since=None)
    assert got == {
        "backlight": "on",
        "power": "stay",
        "on_period": True,
        "absence_clock": "clear",
        "reason": got["reason"],
    }


def test_absence_inside_the_on_period_dims_but_never_suspends_sr020():
    """However long the absence, the on-period forbids the suspend.

    Ten hours is far past any timeout anyone would configure, and it still must
    not suspend — the on-period is a hard gate, not a longer timer. This is what
    makes the walk-in a backlight write rather than a resume: there was never
    anything to resume from.
    """
    for absent_hours in (1, 2, 10):
        got = _decide(minute_of_day=NOON, presence=occ.ABSENT,
                      absent_since=NOW - absent_hours * HOUR)
        assert got["backlight"] == "off", got
        assert got["power"] == "stay", (absent_hours, got)


def test_absence_outside_the_on_period_waits_the_full_hour_sr020():
    """Under the timeout it dims and stays awake; at the boundary it suspends."""
    for minutes, expected in ((0, "stay"), (1, "stay"), (59, "stay"),
                              (60, "suspend"), (61, "suspend")):
        got = _decide(minute_of_day=MIDNIGHT_THIRTY, presence=occ.ABSENT,
                      absent_since=NOW - minutes * 60)
        assert got["power"] == expected, (minutes, got)
        assert got["backlight"] == "off", (minutes, got)


def test_presence_outside_the_on_period_stays_awake_sr020():
    """23:30 with somebody there is not a suspend: the HOUR is required first.

    This is the case the old 22:00 schedule got wrong by construction — it slept
    the panel on the clock regardless of who was standing at it.
    """
    got = _decide(minute_of_day=23 * 60 + 30, presence=occ.PRESENT)
    assert got["power"] == "stay", got
    assert got["backlight"] == "on", got


def test_no_recorded_absence_start_cannot_suspend_sn013():
    """The mains-blip shape: absent, outside the on-period, clock forgotten.

    /run is tmpfs, so a blip-induced reboot loses the absence start. The panel
    must then count a FULL timeout again rather than suspending itself out of
    reach seconds after coming back — an unreachable panel is the failure this
    project mounts the power button for.
    """
    got = _decide(minute_of_day=MIDNIGHT_THIRTY, presence=occ.ABSENT, absent_since=None)
    assert got["power"] == "stay", got


# ── the on-period boundaries ─────────────────────────────────────────────────


def test_on_period_is_half_open_at_both_ends_sr020():
    assert occ.in_on_period(ON_START, ON_START, ON_END) is True       # 06:45 in
    assert occ.in_on_period(ON_START - 1, ON_START, ON_END) is False  # 06:44 out
    assert occ.in_on_period(ON_END - 1, ON_START, ON_END) is True     # 21:59 in
    assert occ.in_on_period(ON_END, ON_START, ON_END) is False        # 22:00 out


def test_a_wrapping_on_period_and_a_degenerate_one_fail_safe_sr020():
    # A window that crosses midnight is legal configuration.
    assert occ.in_on_period(23 * 60, 22 * 60, 6 * 60) is True
    assert occ.in_on_period(12 * 60, 22 * 60, 6 * 60) is False
    # Equal bounds mean ON all day, so a mistyped pair cannot suspend the panel
    # around the clock.
    for minute in (0, NOON, ON_END, 1439):
        assert occ.in_on_period(minute, ON_END, ON_END) is True


def test_parse_hhmm_refuses_rather_than_defaulting_sr020():
    assert occ.parse_hhmm("06:45") == ON_START
    assert occ.parse_hhmm("22:00") == ON_END
    assert occ.parse_hhmm("00:00") == 0
    for bad in ("6:45pm", "24:00", "06:60", "0645", "", "  ", "-1:00", "06:5a", "06"):
        with pytest.raises(ValueError):
            occ.parse_hhmm(bad)


# ── the fail-safe: absence is believed ONLY when positively asserted ─────────


def _write(path: Path, payload):
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                    encoding="utf-8")


@pytest.mark.smoke
def test_a_missing_presence_file_reads_as_present_sr020(tmp_path):
    """The un-integrated image: no writer installed, so nothing may sleep.

    An image whose shell does not publish presence must behave exactly like one
    with the feature off — and it does, because absence is never asserted, so
    the absence timer never starts.
    """
    state, reason = occ.read_presence(str(tmp_path / "nope.json"), NOW * 1000)
    assert state == occ.PRESENT
    assert "no presence file" in reason


def test_every_malformed_presence_file_reads_as_present_sr020(tmp_path):
    path = tmp_path / "state.json"
    fresh = {"schemaVersion": 1, "presence": "absent", "observedAt": NOW * 1000,
             "ttlMs": 30000, "source": "test"}
    broken = [
        "not json at all",
        "[]",
        json.dumps({**fresh, "schemaVersion": 2}),
        json.dumps({**fresh, "schemaVersion": "1"}),
        json.dumps({k: v for k, v in fresh.items() if k != "observedAt"}),
        json.dumps({k: v for k, v in fresh.items() if k != "ttlMs"}),
        json.dumps({**fresh, "ttlMs": 0}),
        json.dumps({**fresh, "ttlMs": -1}),
        json.dumps({**fresh, "observedAt": "yesterday"}),
        json.dumps({**fresh, "presence": "unknown"}),
        json.dumps({**fresh, "presence": None}),
        json.dumps({**fresh, "presence": "Absent"}),   # case matters, on purpose
    ]
    for payload in broken:
        _write(path, payload)
        state, reason = occ.read_presence(str(path), NOW * 1000)
        assert state == occ.PRESENT, (payload, reason)


def test_a_stale_or_future_reading_reads_as_present_sr020(tmp_path):
    path = tmp_path / "state.json"
    now_ms = NOW * 1000
    _write(path, {"schemaVersion": 1, "presence": "absent",
                  "observedAt": now_ms - 30_001, "ttlMs": 30_000, "source": "t"})
    assert occ.read_presence(str(path), now_ms)[0] == occ.PRESENT
    # A dead writer must wake the screen, not sleep the panel.
    assert "stale" in occ.read_presence(str(path), now_ms)[1]
    _write(path, {"schemaVersion": 1, "presence": "absent",
                  "observedAt": now_ms + 10 * 60_000, "ttlMs": 30_000, "source": "t"})
    assert occ.read_presence(str(path), now_ms)[0] == occ.PRESENT


def test_a_fresh_well_formed_file_is_believed_both_ways_sr020(tmp_path):
    """The fail-safe must not be so eager that the feature never works."""
    path = tmp_path / "state.json"
    now_ms = NOW * 1000
    for word, expected in (("absent", occ.ABSENT), ("present", occ.PRESENT)):
        _write(path, {"schemaVersion": 1, "presence": word,
                      "observedAt": now_ms - 1000, "ttlMs": 30_000, "source": "t"})
        assert occ.read_presence(str(path), now_ms)[0] == expected


def test_normalise_presence_only_ever_trusts_the_word_absent_sr020():
    assert occ.normalise_presence("absent") == occ.ABSENT
    for value in ("present", "unknown", "", None, 0, 1, True, False, "ABSENT", []):
        assert occ.normalise_presence(value) == occ.PRESENT, value


# ── the CLI contract the shell depends on ────────────────────────────────────


def test_cli_emits_both_halves_and_the_rtc_wake_from_one_call_sr020(tmp_path, capsys):
    """The shell reads BACKLIGHT, POWER and RTC_WAKE out of ONE invocation.

    RTC_WAKE is echoed back from --on-start rather than re-read by the shell,
    so the alarm is armed from the same value that defined the on-period.
    """
    path = tmp_path / "state.json"
    _write(path, {"schemaVersion": 1, "presence": "absent",
                  "observedAt": NOW * 1000, "ttlMs": 30_000, "source": "t"})
    rc = occ.main([
        "--now-epoch", str(NOW), "--minute-of-day", str(MIDNIGHT_THIRTY),
        "--presence-file", str(path), "--absent-since", str(NOW - 3600),
        "--absence-enabled", "true", "--absence-timeout-min", "60",
        "--on-start", "06:45", "--on-end", "22:00",
    ])
    assert rc == 0
    out = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert out["PRESENCE"] == "absent"
    assert out["BACKLIGHT"] == "off"
    assert out["POWER"] == "suspend"
    assert out["ON_PERIOD"] == "false"
    assert out["RTC_WAKE"] == "06:45"


def test_cli_refuses_an_unparseable_boundary_rather_than_defaulting_sr020(tmp_path):
    """A typo'd boundary is loud. A silently defaulted midnight would be a panel
    sleeping at the wrong hour with nothing anywhere saying so."""
    for on_start, on_end in (("6:45pm", "22:00"), ("06:45", "25:00")):
        rc = occ.main([
            "--now-epoch", str(NOW), "--minute-of-day", "0",
            "--presence-file", str(tmp_path / "absent.json"),
            "--absence-enabled", "true",
            "--on-start", on_start, "--on-end", on_end,
        ])
        assert rc == 2, (on_start, on_end)


# ── the absence clock: the hour is an hour spent OUTSIDE the on-period ───────
#
# The cross-review's central finding. The clock used to be the shell's own
# bookkeeping ("absent, so start counting"), which counted straight through the
# on-period: somebody who left at midday arrived at 22:00 already carrying ten
# hours, and the panel suspended on the very first tick outside — the hour
# outside, which is the rule, never observed at all. The clock is now part of
# the one decision, and these are the cases that keep it there.


@pytest.mark.smoke
def test_the_absence_clock_does_not_run_inside_the_on_period_sr020():
    """Inside the on-period, absence accumulates NOTHING to carry across."""
    inside = _decide(presence=occ.ABSENT, minute_of_day=NOON,
                     absent_since=NOW - 10 * HOUR)
    assert inside["on_period"] is True
    assert inside["power"] == "stay"
    assert inside["absence_clock"] == "clear"


def test_a_ten_hour_absence_cannot_suspend_the_first_minute_outside_sr020():
    """The 22:00 boundary on a midday absence: dark, awake, clock restarted.

    This is the exact sequence the review named. The minute before the
    boundary the clock is cleared (above); the minute after, there is no
    recorded start, so the panel stays awake and the hour begins HERE.
    """
    outside = _decide(presence=occ.ABSENT, minute_of_day=ON_END,
                      absent_since=None, absence_timeout_min=60)
    assert outside["on_period"] is False
    assert outside["power"] == "stay"
    assert outside["backlight"] == "off"
    assert outside["absence_clock"] == "restart"


def test_the_clock_is_cleared_by_presence_and_untouched_when_disabled_sr020():
    assert _decide(presence=occ.PRESENT)["absence_clock"] == "clear"
    assert _decide(absence_enabled=False, presence=occ.ABSENT,
                   absent_since=NOW - 10 * HOUR)["absence_clock"] == "untouched"


def test_an_absence_clock_from_the_future_cannot_suspend_sr020():
    """A clock stamped ahead of now is nonsense arriving at a suspend decision.

    parse_absent_since already refuses it; decide() refuses it again, because
    the input that makes a suspend reachable is the one worth guarding twice.
    """
    result = _decide(presence=occ.ABSENT, minute_of_day=MIDNIGHT_THIRTY,
                     absent_since=NOW + 10 * HOUR, absence_timeout_min=60)
    assert result["power"] == "stay"
    assert result["absence_clock"] == "restart"


# ── the absence clock as it is READ: a truncated write is not an epoch ───────


@pytest.mark.smoke
def test_a_truncated_absence_clock_is_not_five_decades_of_absence_sr020():
    """`1` is what an interrupted write leaves behind, and it reads as 1970.

    Believed, it is ~2.9 million minutes of absence and therefore an IMMEDIATE
    suspend the first tick outside the on-period. None restarts the timer, so
    the panel serves a full hour before it may sleep.
    """
    assert occ.parse_absent_since("1", NOW)[0] is None
    assert occ.parse_absent_since("17", NOW)[0] is None
    assert occ.parse_absent_since(str(occ.MIN_PLAUSIBLE_EPOCH - 1), NOW)[0] is None
    assert "truncated" in occ.parse_absent_since("1", NOW)[1]


def test_the_absence_clock_accepts_only_a_plausible_epoch_sr020():
    assert occ.parse_absent_since(str(NOW - 3600), NOW)[0] == NOW - 3600
    assert occ.parse_absent_since("", NOW)[0] is None
    assert occ.parse_absent_since("   ", NOW)[0] is None
    assert occ.parse_absent_since("not-a-number", NOW)[0] is None
    assert occ.parse_absent_since("-1", NOW)[0] is None
    assert occ.parse_absent_since("1757000000.5", NOW)[0] is None
    # Beyond the tolerated skew, i.e. a clock that has not happened yet.
    assert occ.parse_absent_since(str(NOW + 3600), NOW)[0] is None


# ── NaN: the one value that defeats every freshness check at once ────────────


@pytest.mark.smoke
def test_a_non_finite_presence_reading_is_malformed_not_fresh_sr020(tmp_path):
    """`json.loads` accepts NaN, and every comparison against NaN is FALSE.

    So `age > ttl` is false, `age < -skew` is false, and a file stamped
    `"observedAt": NaN` is treated as a fresh reading — and then believed when
    it says "absent". That inverts this module's whole fail-safe direction, so
    the parse itself refuses the three non-finite constants, and an in-range
    literal that OVERFLOWS to infinity (`1e999` is legal JSON) is refused on the
    way past as well.
    """
    path = tmp_path / "state.json"
    now_ms = NOW * 1000
    for raw in (
        '{"schemaVersion":1,"presence":"absent","observedAt":NaN,"ttlMs":30000}',
        '{"schemaVersion":1,"presence":"absent","observedAt":Infinity,"ttlMs":30000}',
        '{"schemaVersion":1,"presence":"absent","observedAt":-Infinity,"ttlMs":30000}',
        '{"schemaVersion":1,"presence":"absent","observedAt":%d,"ttlMs":NaN}' % now_ms,
        '{"schemaVersion":1,"presence":"absent","observedAt":1,"ttlMs":1e999}',
        '{"schemaVersion":1,"presence":"absent","observedAt":1e999,"ttlMs":30000}',
    ):
        path.write_text(raw, encoding="utf-8")
        state, reason = occ.read_presence(str(path), now_ms)
        assert state == occ.PRESENT, (raw, reason)


def test_the_cli_reports_what_to_do_with_the_absence_clock_sr020(tmp_path, capsys):
    """The clock travels WITH the decision, for the same reason both halves do.

    A shell that ran the clock on its own rule would be a second decider, and
    the clock is the single input that makes a suspend reachable.
    """
    path = tmp_path / "state.json"
    _write(path, {"schemaVersion": 1, "presence": "absent",
                  "observedAt": NOW * 1000, "ttlMs": 30_000, "source": "t"})
    rc = occ.main([
        "--now-epoch", str(NOW), "--minute-of-day", str(MIDNIGHT_THIRTY),
        "--presence-file", str(path), "--absent-since", "1",
        "--absence-enabled", "true", "--absence-timeout-min", "60",
        "--on-start", "06:45", "--on-end", "22:00",
    ])
    assert rc == 0
    out = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    # The truncated clock was rejected, so this tick may NOT suspend.
    assert out["POWER"] == "stay"
    assert out["ABSENCE_CLOCK"] == "restart"
    assert "truncated" in out["CLOCK_REASON"]
