"""TC-1118..TC-1129 — the AI-usage feeder's second account, Gemini, the Codex
error message, and the expired-login rule (plan R2 / G2, AI half).

Everything here goes through the module's PUBLIC seams (`run_cycle` with injected
readers, `read_*` with `http_json` replaced, the shipped unit files and the real
`setup-ai-usage.sh` run against stubbed system commands). The assertions that
matter are made against the OTHER side of each contract:

  * the wire body NagLight receives, not the dict the reader built;
  * the state file on disk, re-read by a fresh `load_state`;
  * the panel's own provider regex, quoted from `js/views/gauges.js`;
  * Gemini CLI's `BucketInfo` / `LoadCodeAssistResponse` shapes, quoted from
    gemini-cli `types.ts` (HEAD 9b6e026) -- the only evidence there is, because
    no live call was possible.

Verifies: TC-1118..TC-1129 (SR-067, SR-068, SR-069; LLR-1047..LLR-1057)
"""

import ast
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AI_DIR = REPO / "stack" / "ai-usage"

_spec = importlib.util.spec_from_file_location("ai_usage_feeder_sources", AI_DIR / "ai_usage_feeder.py")
feeder = importlib.util.module_from_spec(_spec)
sys.modules["ai_usage_feeder_sources"] = feeder
_spec.loader.exec_module(feeder)

_kspec = importlib.util.spec_from_file_location("gemini_auth_refresh", AI_DIR / "gemini_auth_refresh.py")
keeper = importlib.util.module_from_spec(_kspec)
sys.modules["gemini_auth_refresh"] = keeper
_kspec.loader.exec_module(keeper)

# 2026-09-09 08:53 UTC: an instant at which every window in the fixtures below
# is open (the same instant tests/test_ai_usage_feeder.py derives).
NOW = 1788944000

CODEX_RESULT = json.loads("""
{"rateLimits":{"limitId":"codex","primary":{"usedPercent":34,"windowDurationMins":10080,
  "resetsAt":1789435411},"secondary":null,"planType":"prolite"}}
""")

CLAUDE_BODY = json.loads("""
{"five_hour":{"utilization":79.0,"resets_at":"2026-09-09T09:10:00.468815+00:00"},
 "seven_day":{"utilization":87.0,"resets_at":"2026-09-12T09:00:00.468835+00:00"},
 "limits":[{"kind":"weekly_scoped","group":"weekly","percent":12,"severity":"normal",
   "resets_at":"2026-09-12T09:00:00.468835+00:00",
   "scope":{"model":{"id":null,"display_name":"Fable"},"surface":null}}]}
""")

OPENCODE_BODY = json.loads("""
{"usage":{"weekly":{"status":"ok","percent":58,"resetsAt":"2026-09-14T00:00:00.458Z"},
          "monthly":{"status":"ok","percent":89,"resetsAt":"2026-09-19T00:11:19.458Z"}}}
""")

SENTINEL = "SENTINEL-TOKEN-f3a91c7e5d2b40a8"      # must never reach a message, a body or the state


def base_env(tmp_path, **extra):
    env = {
        "AI_USAGE_ENABLED": "true",
        "AI_USAGE_USER": "108000000000000000001",
        "AI_USAGE_STATE_FILE": str(tmp_path / "usage-state.json"),
        "_identity": "108000000000000000001",
        "_feed_url": "http://127.0.0.1:8787/api/feed",
        "STATE_DIRECTORY": str(tmp_path),
    }
    env.update(extra)
    return env


class Recorder(object):
    def __init__(self):
        self.bodies = []

    def __call__(self, body, url, env, timeout):
        self.bodies.append(body)
        return True, "HTTP 200"

    def by_id(self):
        return {b["id"]: b for b in self.bodies}


def cycle(env, readers, now):
    rec = Recorder()
    posted, failures = feeder.run_cycle(env, now=now, readers=readers, poster=rec)
    return rec, posted, failures


def saved_state(env):
    with open(env["AI_USAGE_STATE_FILE"], encoding="utf-8") as handle:
        return json.load(handle)


def stale_on_arrival(body):
    return "observed_at" not in body


# ═══════════════════════════════════════════════════════════════════════════
# TC-1118 — Claude account 2 (LLR-1047)
# ═══════════════════════════════════════════════════════════════════════════

# The panel derives a ring's provider from the gauge id with this regex
# (OfficeWallNaglight js/views/gauges.js, `/^ai-usage-([a-z0-9]+)(?:-|$)/`, plan
# 1 "How a ring is chosen today"). It is quoted, not imported, so a change on
# either side is a deliberate edit.
PANEL_PROVIDER_REGEX = re.compile(r"^ai-usage-([a-z0-9]+)(?:-|$)")


def test_claude2_gauges_group_under_their_own_provider_not_claude_sr067():
    providers = {}
    for spec in feeder.GAUGE_SPECS:
        match = PANEL_PROVIDER_REGEX.match(spec.key)
        assert match, spec.key
        providers.setdefault(spec.source, set()).add(match.group(1))
    # One provider per reader, and the readers' providers are all different: two
    # accounts sharing `claude` would merge into one ring on the panel.
    assert providers == {"codex": {"codex"}, "claude": {"claude"},
                         "claude2": {"claude2"}, "gemini": {"gemini"},
                         "opencode": {"opencode"}}


def test_account_one_keeps_exactly_the_ids_it_always_had_sr067():
    ids = [s.key for s in feeder.GAUGE_SPECS if s.source == "claude"]
    assert ids == ["ai-usage-claude-session", "ai-usage-claude-weekly",
                   "ai-usage-claude-weekly-fable"]
    assert feeder.CLAUDE_BUCKETS[0][1] == "ai-usage-claude-session"


def test_claude2_ids_and_the_fable_band_sort_after_the_weekly_headline_sr067():
    ids = [s.key for s in feeder.GAUGE_SPECS if s.source == "claude2"]
    assert ids == ["ai-usage-claude2-session", "ai-usage-claude2-weekly",
                   "ai-usage-claude2-weekly-fable"]
    assert ids == sorted(ids), "NagLight serves in id order; the Fable band must stay second"


def test_both_claude_accounts_post_independently_each_with_its_own_fable_band_sr067(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="claude,claude2")
    readers = {
        "claude": lambda e: feeder.parse_claude(CLAUDE_BODY, e["_now"]),
        "claude2": lambda e: feeder.parse_claude(
            CLAUDE_BODY, e["_now"], feeder.CLAUDE2_BUCKETS,
            feeder.CLAUDE2_SCOPED_MODELS, "claude2"),
    }
    rec, _posted, failures = cycle(env, readers, NOW)
    assert failures == []
    assert set(rec.by_id()) == {
        "ai-usage-claude-session", "ai-usage-claude-weekly", "ai-usage-claude-weekly-fable",
        "ai-usage-claude2-session", "ai-usage-claude2-weekly", "ai-usage-claude2-weekly-fable"}
    assert rec.by_id()["ai-usage-claude2-session"]["label"] == "Claude 2 session"


def test_claude2_failure_messages_name_claude2_not_claude_sr067():
    with pytest.raises(feeder.SourceFailure) as caught:
        feeder.parse_claude({}, NOW, feeder.CLAUDE2_BUCKETS,
                            feeder.CLAUDE2_SCOPED_MODELS, "claude2")
    assert str(caught.value).startswith("claude2:")


def test_claude2_reads_its_own_credential_file_and_never_account_ones_sr067(monkeypatch, tmp_path):
    one = tmp_path / "one.json"
    two = tmp_path / "two.json"
    one.write_text('{"claudeAiOauth":{"accessToken":"TOKEN-ONE"}}', encoding="utf-8")
    two.write_text('{"claudeAiOauth":{"accessToken":"TOKEN-TWO"}}', encoding="utf-8")
    sent = []

    def fake_http_json(url, headers, timeout, data=None):
        sent.append(headers["Authorization"])
        return CLAUDE_BODY

    monkeypatch.setattr(feeder, "http_json", fake_http_json)
    env = {"AI_USAGE_CLAUDE_CREDENTIALS": str(one),
           "AI_USAGE_CLAUDE2_CREDENTIALS": str(two), "_now": NOW}
    out = feeder.read_claude2(env)
    assert sent == ["Bearer TOKEN-TWO"]
    assert set(out) == {"ai-usage-claude2-session", "ai-usage-claude2-weekly",
                        "ai-usage-claude2-weekly-fable"}
    # With only account one's knob set, claude2 must not fall back to it.
    with pytest.raises(feeder.SourceFailure, match="AI_USAGE_CLAUDE2_CREDENTIALS unset"):
        feeder.read_claude2({"AI_USAGE_CLAUDE_CREDENTIALS": str(one), "_now": NOW})


# ═══════════════════════════════════════════════════════════════════════════
# TC-1119 — the templated refresh unit, and migrating a box that has the old one
# (LLR-1048)
# ═══════════════════════════════════════════════════════════════════════════

def test_the_untemplated_refresh_unit_is_gone_and_the_template_exists_sr067():
    assert not (AI_DIR / "homehub-claude-refresh.service").exists()
    assert not (AI_DIR / "homehub-claude-refresh.timer").exists()
    assert (AI_DIR / "homehub-claude-refresh@.service").exists()
    assert (AI_DIR / "homehub-claude-refresh@.timer").exists()


def test_the_template_carries_no_per_account_path_so_an_instance_cannot_inherit_one_sr067():
    """Every path an instance may write comes from ITS drop-in. A ReadWritePaths=
    in the template would be inherited by account 2's instance too -- a grant of
    account 1's files to the unit that must not touch them."""
    text = (AI_DIR / "homehub-claude-refresh@.service").read_text(encoding="utf-8")
    directives = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    assert not [l for l in directives if l.startswith("ReadWritePaths=")]
    assert not [l for l in directives if l.startswith(("User=", "Group="))]
    assert not [l for l in directives if "CLAUDE_CONFIG_DIR" in l]
    assert [l for l in directives if l.startswith("Description=") and "%i" in l]


def _bash_can_see(path):
    bash = shutil.which("bash")
    if not bash:
        return None
    probe = subprocess.run([bash, "-c", 'test -d "$1" && echo yes', "_", path.as_posix()],
                           capture_output=True, text=True)
    return bash if probe.stdout.strip() == "yes" else None


STUBS = {
    "id": '#!/usr/bin/env bash\nexit 0\n',
    "getent": '#!/usr/bin/env bash\necho "homehub-ai:x:1001:1001::/home/homehub-ai:/bin/bash"\n',
    "systemctl": '#!/usr/bin/env bash\necho "$*" >> "$SANDBOX/systemctl.log"\nexit 0\n',
    # Only paths under the sandbox are made; everything else (/var/lib/..., the
    # account's real home) is a no-op so the test can run unprivileged anywhere.
    "install": """#!/usr/bin/env bash
dirmode=0; args=()
while [ $# -gt 0 ]; do
  case "$1" in
    -d) dirmode=1 ;;
    -m|-o|-g) shift ;;
    -*) ;;
    *) args+=("$1") ;;
  esac
  shift
done
if [ "$dirmode" = 1 ]; then
  for d in "${args[@]}"; do case "$d" in "$SANDBOX"*) mkdir -p "$d" ;; esac; done
else
  cp "${args[0]}" "${args[1]}"
fi
""",
}


def run_setup(tmp_path, sources, legacy=False):
    """Run the REAL setup-ai-usage.sh with stubbed id/getent/install/systemctl."""
    bash = _bash_can_see(tmp_path)
    if not bash:
        pytest.skip("no POSIX bash that can see the test sandbox")
    sandbox = tmp_path / "sandbox"
    stubs = tmp_path / "stubs"
    units = sandbox / "units"
    stubs.mkdir(parents=True, exist_ok=True)
    units.mkdir(parents=True, exist_ok=True)
    for name, body in STUBS.items():
        path = stubs / name
        with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
        path.chmod(0o755)
    if legacy:
        (units / "homehub-claude-refresh.service").write_text("[Service]\n# OLD\n", encoding="utf-8")
        (units / "homehub-claude-refresh.timer").write_text("[Timer]\n# OLD\n", encoding="utf-8")
        (units / "homehub-claude-refresh.service.d").mkdir()
        (units / "homehub-claude-refresh.service.d" / "10-account.conf").write_text(
            "[Service]\nUser=homehub-ai\n", encoding="utf-8")
    env_file = tmp_path / "stack.env"
    env_file.write_text(
        "AI_USAGE_ENABLED=true\nAI_USAGE_USER=108000000000000000001\n"
        "AI_USAGE_FEED_URL=http://127.0.0.1:8787/api/feed\nAI_USAGE_SOURCES=%s\n" % sources,
        encoding="utf-8")
    env = dict(os.environ)
    env.update({"PATH": str(stubs) + os.pathsep + env["PATH"],
                "SANDBOX": sandbox.as_posix(),
                "AI_USAGE_ENV_FILE": env_file.as_posix(),
                "AI_USAGE_UNIT_DIR": units.as_posix()})
    for key in ("AI_USAGE_SOURCES", "AI_USAGE_USER_ACCOUNT"):
        env.pop(key, None)
    done = subprocess.run([bash, (AI_DIR / "setup-ai-usage.sh").as_posix()],
                          env=env, capture_output=True, text=True)
    log = sandbox / "systemctl.log"
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return done, units, calls


def test_setup_installs_account_one_exactly_as_the_old_unit_did_sr067(tmp_path):
    done, units, calls = run_setup(tmp_path, "codex,claude,opencode")
    assert done.returncode == 0, done.stdout + done.stderr
    dropin = (units / "homehub-claude-refresh@claude.service.d" / "10-account.conf").read_text(encoding="utf-8")
    assert "User=homehub-ai" in dropin and "Group=homehub-ai" in dropin
    # THE SAME TWO PATHS the un-templated unit granted, from the account's real home.
    assert "ReadWritePaths=/home/homehub-ai/.claude /home/homehub-ai/.claude.json" in dropin
    assert "CLAUDE_CONFIG_DIR" not in dropin, "account 1 uses the CLI's default config"
    assert "enable --now homehub-claude-refresh@claude.timer" in calls
    # Nothing opt-in leaks onto a box that did not ask for it.
    assert not (units / "homehub-claude-refresh@claude2.service.d").exists()
    assert not (units / "homehub-gemini-refresh.timer").exists()
    assert not [c for c in calls if c.startswith("enable") and ("claude2" in c or "gemini" in c)]


def test_setup_migrates_the_old_unit_and_never_has_both_timers_enabled_sr067(tmp_path):
    done, units, calls = run_setup(tmp_path, "codex,claude,opencode", legacy=True)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (units / "homehub-claude-refresh.service").exists()
    assert not (units / "homehub-claude-refresh.timer").exists()
    assert not (units / "homehub-claude-refresh.service.d").exists()
    assert "Migrated" in done.stdout
    old_off = calls.index("disable --now homehub-claude-refresh.timer")
    new_on = calls.index("enable --now homehub-claude-refresh@claude.timer")
    assert old_off < new_on, "the old timer must be stopped BEFORE the new one starts"
    assert "daemon-reload" in calls[old_off:new_on]


def test_setup_is_idempotent_on_a_migrated_box_sr067(tmp_path):
    first, units, _ = run_setup(tmp_path, "codex,claude,opencode", legacy=True)
    before = {p.relative_to(units).as_posix(): p.read_text(encoding="utf-8")
              for p in units.rglob("*") if p.is_file()}
    second, units2, calls = run_setup(tmp_path, "codex,claude,opencode")
    assert first.returncode == second.returncode == 0
    after = {p.relative_to(units2).as_posix(): p.read_text(encoding="utf-8")
             for p in units2.rglob("*") if p.is_file()}
    assert before == after
    assert "Migrated" not in second.stdout


def test_setup_gives_account_two_only_its_own_directory_sr067(tmp_path):
    done, units, calls = run_setup(tmp_path, "claude,claude2,codex,gemini,opencode")
    assert done.returncode == 0, done.stdout + done.stderr
    two = (units / "homehub-claude-refresh@claude2.service.d" / "10-account.conf").read_text(encoding="utf-8")
    assert "Environment=CLAUDE_CONFIG_DIR=/home/homehub-ai/.claude-2" in two
    rw = [l for l in two.splitlines() if l.startswith("ReadWritePaths=")]
    assert rw == ["ReadWritePaths=/home/homehub-ai/.claude-2"], rw
    one = (units / "homehub-claude-refresh@claude.service.d" / "10-account.conf").read_text(encoding="utf-8")
    assert ".claude-2" not in one, "account 1's instance must not be able to write account 2's"
    assert "enable --now homehub-claude-refresh@claude2.timer" in calls
    assert "enable --now homehub-gemini-refresh.timer" in calls
    gem = (units / "homehub-gemini-refresh.service.d" / "10-account.conf").read_text(encoding="utf-8")
    assert "ReadWritePaths=/home/homehub-ai/.gemini" in gem
    # The operator is told the right sign-in command for every source.
    assert "claude auth login" in done.stdout
    assert "CLAUDE_CONFIG_DIR=/home/homehub-ai/.claude-2" in done.stdout
    assert "NO_BROWSER=true" in done.stdout and "gemini" in done.stdout


def test_dropping_a_source_removes_its_keeper_sr067(tmp_path):
    run_setup(tmp_path, "claude,claude2,codex,gemini,opencode")
    done, units, calls = run_setup(tmp_path, "claude,codex,opencode")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (units / "homehub-claude-refresh@claude2.service.d").exists()
    assert not (units / "homehub-gemini-refresh.timer").exists()
    assert "disable --now homehub-claude-refresh@claude2.timer" in calls
    assert "disable --now homehub-gemini-refresh.timer" in calls


def test_setup_no_longer_tells_the_operator_to_run_setup_token_sr067():
    """`claude setup-token` mints an automation token WITHOUT the user:profile
    scope the usage endpoint needs (open-items C70): the gauge stays unavailable
    behind a login that looks fine. The right command is `claude auth login`."""
    setup = (AI_DIR / "setup-ai-usage.sh").read_text(encoding="utf-8")
    assert "claude auth login" in setup
    suggested = [l for l in setup.splitlines()
                 if "claude setup-token" in l and "NOT" not in l and not l.lstrip().startswith("#")
                 and "say" in l and "THE COMMAND IS" not in l]
    commands = [l for l in setup.splitlines() if "&& claude setup-token" in l]
    assert not commands, "setup must not RUN-suggest setup-token: %r" % commands
    assert not [l for l in suggested if "sh -c" in l]


def test_setup_script_parses_sr067():
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash")
    done = subprocess.run([bash, "-n", (AI_DIR / "setup-ai-usage.sh").as_posix()],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


# ═══════════════════════════════════════════════════════════════════════════
# TC-1120 — the Gemini quota parser (LLR-1049)
# ═══════════════════════════════════════════════════════════════════════════
#
# The fixture shape is gemini-cli types.ts `RetrieveUserQuotaResponse`:
#   buckets?: [{remainingAmount?: string, remainingFraction?: number,
#               resetTime?: string, tokenType?: string, modelId?: string}]

def bucket(model, fraction, reset_in=5 * 3600, **extra):
    out = {"modelId": model, "remainingFraction": fraction,
           "resetTime": feeder.rfc3339_utc(NOW + reset_in), "tokenType": "REQUESTS"}
    out.update(extra)
    return out


def quota(*buckets):
    return {"buckets": list(buckets)}


def test_gemini_reports_the_most_consumed_bucket_inverted_from_remaining_sr067():
    out = feeder.parse_gemini_quota(quota(
        bucket("gemini-2.5-pro", 0.60), bucket("gemini-2.5-flash", 0.25),
        bucket("gemini-2.5-flash-lite", 0.90)), NOW)
    reading = out["ai-usage-gemini-daily"]
    assert reading.percent == pytest.approx(75.0)         # 25 % remaining = 75 % consumed
    assert reading.window_end == NOW + 5 * 3600
    assert reading.window_seconds == 24 * 3600


def test_gemini_wire_body_is_remaining_daily_and_windowed_sr067(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="gemini")
    rec, _p, failures = cycle(env, {"gemini": lambda e: feeder.parse_gemini_quota(
        quota(bucket("gemini-2.5-pro", 0.25)), e["_now"])}, NOW)
    assert failures == []
    body = rec.bodies[0]
    assert body["id"] == "ai-usage-gemini-daily"
    assert body["value"] == pytest.approx(25.0), "the wire carries REMAINING"
    assert body["window"]["kind"] == "daily"
    assert body["direction"] == "down" and body["unit"] == "%"
    assert body["observed_at"] == feeder.rfc3339_utc(NOW)


def test_gemini_buckets_the_cli_itself_skips_are_skipped_sr067():
    """config.ts refreshUserQuota: `if (!bucket.modelId || remainingFraction == null) continue`."""
    out = feeder.parse_gemini_quota(quota(
        {"modelId": "no-fraction", "resetTime": feeder.rfc3339_utc(NOW + 3600)},
        {"remainingFraction": 0.01, "resetTime": feeder.rfc3339_utc(NOW + 3600)},   # no modelId
        bucket("gemini-2.5-pro", 0.5)), NOW)
    assert out["ai-usage-gemini-daily"].percent == pytest.approx(50.0)


def test_gemini_a_configured_model_list_narrows_the_minimum_sr067():
    payload = quota(bucket("gemini-2.5-pro", 0.60), bucket("gemini-2.5-flash", 0.05))
    assert feeder.parse_gemini_quota(payload, NOW, ["gemini-2.5-pro"])[
        "ai-usage-gemini-daily"].percent == pytest.approx(40.0)
    assert feeder.parse_gemini_quota(payload, NOW)[
        "ai-usage-gemini-daily"].percent == pytest.approx(95.0)


def test_gemini_a_configured_model_with_no_bucket_refuses_the_whole_reading_sr067():
    """The answer is a MINIMUM: a quietly absent model could be the lowest one."""
    with pytest.raises(feeder.SourceFailure) as caught:
        feeder.parse_gemini_quota(quota(bucket("gemini-2.5-pro", 0.5)), NOW,
                                  ["gemini-2.5-pro", "gemini-3-ultra"])
    message = str(caught.value)
    assert "gemini-3-ultra" in message and "gemini-2.5-pro" in message   # names what WAS returned


@pytest.mark.parametrize("bad", [1.5, -0.1, float("nan"), float("inf")])
def test_gemini_an_out_of_range_fraction_refuses_the_reading_sr067(bad):
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_gemini_quota(quota(bucket("a", 0.5), bucket("b", bad)), NOW)


def test_gemini_a_bucket_with_an_unusable_window_is_never_quietly_dropped_sr067():
    """Dropping it could drop the lowest bucket and report headroom that is not there."""
    low = bucket("gemini-2.5-pro", 0.01)
    low["resetTime"] = "not a time"
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_gemini_quota(quota(low, bucket("gemini-2.5-flash", 0.9)), NOW)
    no_reset = bucket("gemini-2.5-pro", 0.01)
    del no_reset["resetTime"]
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_gemini_quota(quota(no_reset, bucket("gemini-2.5-flash", 0.9)), NOW)


def test_gemini_a_non_daily_bucket_is_refused_not_mis_sized_as_daily_sr067():
    weekly = bucket("gemini-2.5-pro", 0.5, reset_in=5 * 24 * 3600)
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_gemini_quota(quota(weekly), NOW)
    past = bucket("gemini-2.5-pro", 0.5, reset_in=-3 * 3600)
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_gemini_quota(quota(past), NOW)


@pytest.mark.parametrize("payload", [None, [], {}, {"buckets": None}, {"buckets": "x"},
                                     {"buckets": []}, {"buckets": [{}, 3, None]}])
def test_gemini_anything_unrecognised_is_unavailable_sr067(payload):
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_gemini_quota(payload, NOW)


def test_gemini_the_label_says_which_bucket_the_number_is_sr067():
    assert feeder.gemini_label([]) == "Gemini daily (all models)"
    assert feeder.gemini_label(["gemini-2.5-pro", "gemini-2.5-flash"]) == \
        "Gemini daily (gemini-2.5-pro, gemini-2.5-flash)"
    long_list = ["gemini-model-number-%02d-preview-long-name" % i for i in range(8)]
    label = feeder.gemini_label(long_list)
    assert label == "Gemini daily (lowest of 8 models)", "never a silently truncated list"
    assert len(label) <= feeder.RUNE_LIMIT


def test_gemini_the_posted_label_follows_the_configured_models_sr067(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="gemini",
                   AI_USAGE_GEMINI_MODELS="gemini-2.5-pro, gemini-2.5-flash,gemini-2.5-pro")
    rec, _p, _f = cycle(env, {"gemini": lambda e: feeder.parse_gemini_quota(
        quota(bucket("gemini-2.5-pro", 0.5), bucket("gemini-2.5-flash", 0.4)), e["_now"],
        feeder.parse_gemini_models(e["AI_USAGE_GEMINI_MODELS"]))}, NOW)
    assert rec.bodies[0]["label"] == "Gemini daily (gemini-2.5-pro, gemini-2.5-flash)"


def test_gemini_a_malformed_model_list_fails_the_reading_not_the_cycle_sr067(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="gemini", AI_USAGE_GEMINI_MODELS="ok-model, bad model!")
    assert feeder.gauge_specs(env)           # never raises
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_gemini_models(env["AI_USAGE_GEMINI_MODELS"])


# ═══════════════════════════════════════════════════════════════════════════
# TC-1121 — the Gemini reader: two calls, the CLI's token, no refresh (LLR-1050)
# ═══════════════════════════════════════════════════════════════════════════

def gemini_creds(tmp_path, expiry_ms=None, **extra):
    path = tmp_path / ".gemini" / "oauth_creds.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"access_token": "AT-" + SENTINEL, "refresh_token": "RT-" + SENTINEL,
            "token_type": "Bearer", "scope": "x"}
    data["expiry_date"] = (NOW + 1800) * 1000 if expiry_ms is None else expiry_ms
    data.update(extra)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


LOAD_OK = {"currentTier": {"id": "free-tier", "name": "Gemini Code Assist"},
           "cloudaicompanionProject": "projects-abc-123"}


class Calls(object):
    """http_json replaced: records (url, headers, data) and answers by method."""

    def __init__(self, answers):
        self.answers = answers
        self.seen = []

    def __call__(self, url, headers, timeout, data=None):
        self.seen.append((url, dict(headers), data))
        for suffix, answer in self.answers.items():
            if url.endswith(suffix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError("unexpected call: " + url)


def test_gemini_reader_makes_the_two_calls_the_cli_makes_and_nothing_else_sr067(monkeypatch, tmp_path):
    creds = gemini_creds(tmp_path)
    before = creds.read_bytes()
    calls = Calls({":loadCodeAssist": LOAD_OK,
                   ":retrieveUserQuota": quota(bucket("gemini-2.5-pro", 0.4))})
    monkeypatch.setattr(feeder, "http_json", calls)
    out = feeder.read_gemini({"AI_USAGE_GEMINI_CREDENTIALS": str(creds), "_now": NOW})
    assert out["ai-usage-gemini-daily"].percent == pytest.approx(60.0)

    urls = [u for u, _h, _d in calls.seen]
    assert urls == ["https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
                    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"]
    # server.ts: loadCodeAssist first, with the CLI's own client metadata ...
    load_body = calls.seen[0][2]
    assert load_body == {"metadata": {"ideType": "IDE_UNSPECIFIED",
                                      "platform": "PLATFORM_UNSPECIFIED",
                                      "pluginType": "GEMINI"}}
    # ... then retrieveUserQuota with the project it named (config.ts).
    assert calls.seen[1][2] == {"project": "projects-abc-123"}
    for _u, headers, _d in calls.seen:
        assert headers["Authorization"] == "Bearer AT-" + SENTINEL
        assert "Python-urllib" not in headers["User-Agent"]
    # SR-021: it reads the CLI's file and never touches it, and never SENDS the
    # refresh token anywhere.
    assert creds.read_bytes() == before
    for _u, headers, data in calls.seen:
        assert "RT-" + SENTINEL not in json.dumps([headers, data])


def test_gemini_reader_never_mints_or_refreshes_a_google_token_sr067():
    """Asserted against the module's CODE (docstrings and comments may explain the
    rule): no token endpoint, no grant type, no refresh token anywhere a program
    could use one."""
    tree = ast.parse((AI_DIR / "ai_usage_feeder.py").read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    strings = [n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and id(n) not in docstrings]
    offenders = [s for s in strings
                 if re.search(r"oauth2\.googleapis|grant_type|refresh_token|client_secret|accounts\.google", s)]
    assert not offenders, offenders


def test_gemini_reader_stops_at_an_expired_token_and_says_so_without_calling_sr067(monkeypatch, tmp_path):
    creds = gemini_creds(tmp_path, expiry_ms=(NOW - 60) * 1000)
    calls = Calls({})
    monkeypatch.setattr(feeder, "http_json", calls)
    with pytest.raises(feeder.AuthFailure) as caught:
        feeder.read_gemini({"AI_USAGE_GEMINI_CREDENTIALS": str(creds), "_now": NOW})
    assert caught.value.code == "access_token_expired"
    assert calls.seen == [], "an expired token must not be spent on a request"


def test_gemini_reader_without_an_expiry_lets_the_server_decide_sr067(monkeypatch, tmp_path):
    path = tmp_path / ".gemini" / "oauth_creds.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"access_token": "AT"}), encoding="utf-8")
    monkeypatch.setattr(feeder, "http_json", Calls({
        ":loadCodeAssist": LOAD_OK, ":retrieveUserQuota": quota(bucket("m", 0.5))}))
    assert feeder.read_gemini({"AI_USAGE_GEMINI_CREDENTIALS": str(path), "_now": NOW})


def test_gemini_reader_never_onboards_an_account_sr067(monkeypatch, tmp_path):
    """No `currentTier` is the case where the CLI would call onboardUser. The
    feeder refuses, names the fix, and makes no mutating call."""
    creds = gemini_creds(tmp_path)
    calls = Calls({":loadCodeAssist": {"allowedTiers": [{"id": "free-tier", "isDefault": True}]}})
    monkeypatch.setattr(feeder, "http_json", calls)
    with pytest.raises(feeder.SourceFailure, match="not been onboarded"):
        feeder.read_gemini({"AI_USAGE_GEMINI_CREDENTIALS": str(creds), "_now": NOW})
    assert [u for u, _h, _d in calls.seen] == [
        "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"]
    assert not [u for u, _h, _d in calls.seen if "onboardUser" in u]


def test_gemini_project_comes_from_the_server_then_the_operator_then_nowhere_sr067():
    named = dict(LOAD_OK)
    assert feeder.parse_gemini_project(named, "operator-proj") == "projects-abc-123"
    unnamed = {"currentTier": {"id": "standard-tier"}}
    assert feeder.parse_gemini_project(unnamed, "operator-proj") == "operator-proj"
    with pytest.raises(feeder.SourceFailure, match="no project id"):
        feeder.parse_gemini_project(unnamed, None)
    with pytest.raises(feeder.SourceFailure):
        feeder.parse_gemini_project({"currentTier": {}, "cloudaicompanionProject": 12345}, None)


def test_gemini_a_401_is_an_authentication_failure_sr068(monkeypatch, tmp_path):
    creds = gemini_creds(tmp_path)
    monkeypatch.setattr(feeder, "http_json", Calls({
        ":loadCodeAssist": feeder.AuthFailure("x: HTTP 401", status=401)}))
    with pytest.raises(feeder.AuthFailure):
        feeder.read_gemini({"AI_USAGE_GEMINI_CREDENTIALS": str(creds), "_now": NOW})


class _LoopbackOpener(object):
    """vendor_opener replaced by one that sends the request to a LOCAL server."""

    def __init__(self, base, status=200, body=b"{}"):
        self.base, self.status, self.body = base, status, body

    def open(self, request, timeout=None):
        import urllib.request
        rewritten = urllib.request.Request(
            self.base + "/x", data=request.data, headers=dict(request.header_items()),
            method=request.get_method())
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(rewritten, timeout=5)


def test_http_json_posts_json_with_the_bearer_and_refuses_http_sr067(monkeypatch):
    from conftest import loopback_server

    def answer(handler):
        payload = b'{"ok": true}'
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    with loopback_server(answer) as (base, seen):
        monkeypatch.setattr(feeder, "vendor_opener", lambda: _LoopbackOpener(base))
        out = feeder.http_json("https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
                               {"Authorization": "Bearer T"}, 5, data={"project": "p"})
    assert out == {"ok": True}
    request = seen[0]
    assert json.loads(request["body"]) == {"project": "p"}
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["headers"]["Authorization"] == "Bearer T"
    with pytest.raises(feeder.SourceFailure, match="must be https"):
        feeder.http_json("http://cloudcode-pa.googleapis.com/x", {}, 5, data={})


# ═══════════════════════════════════════════════════════════════════════════
# TC-1122 — the Gemini token keeper (LLR-1051)
# ═══════════════════════════════════════════════════════════════════════════

GEMINI_ACCESS_TOKEN_LIFE_SECONDS = 3600     # Google's standard access-token life


def test_the_gemini_refresh_interval_beats_the_token_life_with_a_missed_run_of_margin_sr067():
    timer = (AI_DIR / "homehub-gemini-refresh.timer").read_text(encoding="utf-8")
    line = [l for l in timer.splitlines() if l.startswith("OnUnitActiveSec=")][0]
    value = line.split("=", 1)[1].strip()
    assert value.endswith("min"), value
    seconds = int(value[:-3]) * 60
    assert seconds * 2 <= GEMINI_ACCESS_TOKEN_LIFE_SECONDS
    assert "Persistent=true" in timer


def test_the_gemini_keeper_unit_costs_no_quota_and_keeps_the_posture_sr067():
    service = (AI_DIR / "homehub-gemini-refresh.service").read_text(encoding="utf-8")
    directives = [l for l in service.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    exec_line = [l for l in directives if l.startswith("ExecStart=")][0]
    assert "gemini_auth_refresh.py" in exec_line
    # Not `gemini -p ...`: a headless prompt (even "/stats") reaches the MODEL.
    assert " -p " not in exec_line and "/stats" not in exec_line
    assert "ProtectHome=read-only" in directives
    assert not [l for l in directives if "%h" in l]
    assert "NO_BROWSER" in service


class FakeCli(object):
    """A script that speaks the ACP subset the keeper uses, recording what it saw."""

    def __init__(self, tmp_path, mode="ok"):
        self.log = tmp_path / "fake-cli.log"
        self.script = tmp_path / "fake_cli.py"
        self.script.write_text(
            "import json, os, sys, time\n"
            "log = open(%r, 'a')\n"
            "mode = %r\n"
            "open(%r, 'w').write(os.environ.get('NO_BROWSER', ''))\n"
            "if mode == 'login':\n"
            "    print('Please visit the following URL to authorize the application:'); sys.stdout.flush()\n"
            "for line in sys.stdin:\n"
            "    try:\n"
            "        frame = json.loads(line)\n"
            "    except ValueError:\n"
            "        continue\n"
            "    log.write(frame['method'] + ' ' + json.dumps(frame.get('params')) + '\\n'); log.flush()\n"
            "    if mode == 'silent':\n"
            "        continue\n"
            "    if frame['method'] == 'initialize':\n"
            "        out = {'jsonrpc': '2.0', 'id': frame['id'], 'result': {'protocolVersion': 1,\n"
            "               'agentInfo': {'name': 'x', 'email': 'LEAK-ME-account-detail'}}}\n"
            "    elif mode == 'refuse':\n"
            "        out = {'jsonrpc': '2.0', 'id': frame['id'],\n"
            "               'error': {'code': -32000, 'message': 'LEAK-ME token SENTINEL'}}\n"
            "    else:\n"
            "        out = {'jsonrpc': '2.0', 'id': frame['id'], 'result': {}}\n"
            "    print(json.dumps(out)); sys.stdout.flush()\n"
            % (str(self.log), mode, str(self.log) + ".env"),
            encoding="utf-8")
        self.command = [sys.executable, str(self.script)]

    def methods(self):
        if not self.log.exists():
            return []
        return [l.split(" ", 1)[0] for l in self.log.read_text(encoding="utf-8").splitlines()]


def keeper_home(tmp_path, selected="oauth-personal", creds=True, expiry=1000):
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    if selected is not None:
        (home / ".gemini" / "settings.json").write_text(
            json.dumps({"security": {"auth": {"selectedType": selected}}}), encoding="utf-8")
    if creds:
        (home / ".gemini" / "oauth_creds.json").write_text(
            json.dumps({"access_token": "AT", "refresh_token": "RT", "expiry_date": expiry}),
            encoding="utf-8")
    return home


def test_the_keeper_sends_initialize_then_authenticate_and_nothing_that_prompts_sr067(tmp_path, capsys):
    cli = FakeCli(tmp_path)
    home = keeper_home(tmp_path)
    code, message = keeper.refresh(cli.command, str(home), timeout=20)
    assert code == 0, message
    assert cli.methods() == ["initialize", "authenticate"], \
        "no session/new, no session/prompt: nothing that could reach the model"
    params = json.loads(cli.log.read_text(encoding="utf-8").splitlines()[1].split(" ", 1)[1])
    assert params == {"methodId": "oauth-personal"}


def test_the_keeper_prints_nothing_the_cli_said_sr067(tmp_path):
    cli = FakeCli(tmp_path, mode="refuse")
    home = keeper_home(tmp_path)
    code, message = keeper.refresh(cli.command, str(home), timeout=20)
    assert code == 1
    assert "LEAK-ME" not in message and "SENTINEL" not in message
    assert "-32000" in message, "the numeric code is the diagnostic"


def test_the_keeper_does_not_echo_a_successful_initialize_reply_either_sr067(tmp_path):
    cli = FakeCli(tmp_path)
    home = keeper_home(tmp_path)
    code, message = keeper.refresh(cli.command, str(home), timeout=20)
    assert code == 0 and "LEAK-ME" not in message


def test_the_keeper_reports_whether_the_expiry_moved_without_printing_it_sr067(tmp_path):
    cli = FakeCli(tmp_path)
    home = keeper_home(tmp_path, expiry=1000)
    creds = home / ".gemini" / "oauth_creds.json"
    # The fake CLI does not touch the file, so the expiry cannot have moved.
    _code, message = keeper.refresh(cli.command, str(home), timeout=20)
    assert "did not change" in message and "1000" not in message
    assert keeper.credential_expiry(str(home)) == 1000
    assert creds.exists()


def test_the_keeper_refuses_when_authenticate_would_clear_the_credentials_sr067(tmp_path):
    cli = FakeCli(tmp_path)
    for index, selected in enumerate(("gemini-api-key", "vertex-ai", None)):
        sub = tmp_path / ("h%d" % index)
        sub.mkdir()
        home = keeper_home(sub, selected=selected)
        code, message = keeper.refresh(cli.command, str(home), timeout=20)
        assert code == 3 and message.startswith("REFUSED"), (selected, message)
    assert cli.methods() == [], "the CLI must not even be started"


def test_the_keeper_refuses_without_a_cached_credential_sr067(tmp_path):
    cli = FakeCli(tmp_path)
    home = keeper_home(tmp_path, creds=False)
    code, message = keeper.refresh(cli.command, str(home), timeout=20)
    assert code == 3 and "interactive login" in message
    assert cli.methods() == []


def test_the_keeper_gives_up_on_a_silent_cli_and_kills_it_sr067(tmp_path):
    cli = FakeCli(tmp_path, mode="silent")
    home = keeper_home(tmp_path)
    code, message = keeper.refresh(cli.command, str(home), timeout=2)
    assert code == 1 and "timed out" in message


def test_the_keeper_recognises_the_interactive_sign_in_prompt_as_a_dead_login_sr067(tmp_path):
    """OBSERVED against the real CLI (0.63.0, fake credential): with a credential
    it cannot renew, `gemini --acp` does not speak ACP at all; it prints a
    sign-in prompt on stdout and reads stdin for a code. That is the dead-login
    signature, and the keeper must say so rather than time out."""
    cli = FakeCli(tmp_path, mode="login")
    home = keeper_home(tmp_path)
    code, message = keeper.refresh(cli.command, str(home), timeout=20)
    assert code == 4 and "sign in again" in message and message.startswith("gemini-refresh: DEAD LOGIN")


def test_the_keeper_suppresses_the_browser_for_the_cli_it_starts_sr067(tmp_path):
    """NO_BROWSER is what makes the CLI renew the token at STARTUP (gemini.tsx),
    and what stops a renewal that fails from trying to open a browser."""
    cli = FakeCli(tmp_path)
    keeper.refresh(cli.command, str(keeper_home(tmp_path)), timeout=20)
    assert (tmp_path / "fake-cli.log.env").read_text(encoding="utf-8") == "true"


def test_the_keeper_survives_a_missing_binary_sr067(tmp_path):
    home = keeper_home(tmp_path)
    code, message = keeper.refresh([str(tmp_path / "no-such-gemini")], str(home), timeout=5)
    assert code == 1 and "cannot launch" in message


# ═══════════════════════════════════════════════════════════════════════════
# TC-1123 — the Codex reader says why it failed (LLR-1052)
# ═══════════════════════════════════════════════════════════════════════════

class FakeStdin(object):
    def write(self, _text):
        return 0

    def flush(self):
        pass


class FakeChild(object):
    def __init__(self, frames):
        self.stdin = FakeStdin()
        self.stdout = io.StringIO("".join(json.dumps(f) + "\n" for f in frames))

    def kill(self):
        pass


def run_codex_with(monkeypatch, frames):
    monkeypatch.setattr(feeder.subprocess, "Popen", lambda *a, **k: FakeChild(frames))
    real_sleep = feeder.time.sleep
    monkeypatch.setattr(feeder.time, "sleep", lambda s: real_sleep(0.001))
    return feeder.read_codex({"AI_USAGE_TIMEOUT_SECONDS": "5", "_now": NOW})


TOKEN_EXPIRED_FRAME = {"jsonrpc": "2.0", "id": 2, "error": {
    "code": -32603,
    "message": ("failed to fetch rate limits: 401 Unauthorized; body={\"error\":{"
                "\"code\":\"token_expired\",\"message\":\"Provided authentication token "
                "is expired. Please try signing in again.\"}}")}}


def test_a_token_expired_error_frame_shows_its_message_sr069(monkeypatch):
    with pytest.raises(feeder.SourceFailure) as caught:
        run_codex_with(monkeypatch, [TOKEN_EXPIRED_FRAME])
    text = str(caught.value)
    assert text.startswith("codex: JSON-RPC error -32603: ")
    assert "token_expired" in text, "this word was invisible for six days"
    assert "Please try signing in again" in text


def test_a_token_expired_frame_is_an_authentication_failure_sr068(monkeypatch):
    with pytest.raises(feeder.AuthFailure) as caught:
        run_codex_with(monkeypatch, [TOKEN_EXPIRED_FRAME])
    assert caught.value.code == "token_expired"


@pytest.mark.parametrize("message", [
    "internal error: the sandbox could not start",
    "failed to read 401 bytes from the rollout file",           # a bare 401 is not a login
    "rate limit endpoint returned HTTP 500",
])
def test_other_codex_errors_are_plain_failures_sr068(monkeypatch, message):
    frame = {"jsonrpc": "2.0", "id": 2, "error": {"code": -32603, "message": message}}
    with pytest.raises(feeder.SourceFailure) as caught:
        run_codex_with(monkeypatch, [frame])
    assert not isinstance(caught.value, feeder.AuthFailure)
    assert message in str(caught.value)


@pytest.mark.parametrize("secret", [
    "Bearer " + SENTINEL,
    "eyJhbGciOiJSUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + SENTINEL,
    "sk-proj-" + SENTINEL,
    "ya29." + SENTINEL,
    SENTINEL * 2,
    "1//0g" + SENTINEL,
])
def test_a_token_shaped_string_in_the_message_is_redacted_sr069(monkeypatch, secret):
    frame = {"jsonrpc": "2.0", "id": 2, "error": {
        "code": -32603, "message": "token_expired for request with header %s end" % secret}}
    with pytest.raises(feeder.AuthFailure) as caught:
        run_codex_with(monkeypatch, [frame])
    text = str(caught.value)
    assert SENTINEL not in text, text
    assert "token_expired" in text


def test_redaction_happens_before_truncation_sr069():
    """Cutting first could leave a prefix of a token that no pattern recognises."""
    text = "x" * 150 + " Bearer " + SENTINEL + SENTINEL
    out = feeder.redact_remote_text(text)
    assert SENTINEL[:12] not in out
    assert len(out) <= 200


def test_the_error_message_is_truncated_and_cannot_forge_a_journal_line_sr069(monkeypatch):
    frame = {"jsonrpc": "2.0", "id": 2, "error": {
        "code": -32603, "message": ("a b " * 200) + "\nai-usage: gemini UNAVAILABLE\r\n"}}
    with pytest.raises(feeder.SourceFailure) as caught:
        run_codex_with(monkeypatch, [frame])
    text = str(caught.value)
    assert "\n" not in text and "\r" not in text
    assert len(text) < 260


def test_a_malformed_error_object_still_fails_closed_sr069(monkeypatch):
    for error in (None, "boom", 7, {}):
        with pytest.raises(feeder.SourceFailure):
            run_codex_with(monkeypatch, [{"jsonrpc": "2.0", "id": 2, "error": error}])


# ═══════════════════════════════════════════════════════════════════════════
# TC-1124 — what counts as authentication, and what must not (LLR-1053)
# ═══════════════════════════════════════════════════════════════════════════

class _Opener(object):
    def __init__(self, code):
        self.code = code

    def open(self, request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, self.code, "x", {}, io.BytesIO(b"SENTINEL body"))


@pytest.mark.parametrize("code,auth", [(401, True), (403, False), (404, False),
                                       (429, False), (500, False), (503, False)])
def test_only_an_http_401_is_authentication_sr068(monkeypatch, code, auth):
    monkeypatch.setattr(feeder, "vendor_opener", lambda: _Opener(code))
    with pytest.raises(feeder.SourceFailure) as caught:
        feeder.http_json("https://example.invalid/x", {}, 5)
    assert isinstance(caught.value, feeder.AuthFailure) is auth
    if auth:
        assert caught.value.status == 401
    assert "SENTINEL" not in str(caught.value), "a response body never reaches the message"


def test_auth_failure_is_a_source_failure_so_old_handlers_still_work_sr068():
    assert issubclass(feeder.AuthFailure, feeder.SourceFailure)


def test_claude_no_usable_key_is_authentication_for_claude_only_sr068(monkeypatch, tmp_path):
    creds = tmp_path / ".credentials.json"
    creds.write_text('{"claudeAiOauth":{"scopes":["user:profile"]}}', encoding="utf-8")    # no accessToken
    auth = tmp_path / "auth.json"
    auth.write_text('{"someprovider":{"type":"api","key":"K"}}', encoding="utf-8")        # renamed schema
    monkeypatch.setattr(feeder, "http_json", lambda *a, **k: pytest.fail("no network call expected"))

    with pytest.raises(feeder.AuthFailure) as caught:
        feeder.read_claude({"AI_USAGE_CLAUDE_CREDENTIALS": str(creds), "_now": NOW})
    assert caught.value.code == "no_usable_key"
    with pytest.raises(feeder.AuthFailure):
        feeder.read_claude2({"AI_USAGE_CLAUDE2_CREDENTIALS": str(creds), "_now": NOW})

    # The SAME words from the OpenCode reader are NOT authentication: a renamed
    # schema once produced them from a perfectly good credential.
    with pytest.raises(feeder.SourceFailure) as other:
        feeder.read_opencode({"AI_USAGE_OPENCODE_AUTH": str(auth), "_now": NOW})
    assert "carries no usable key" in str(other.value)
    assert not isinstance(other.value, feeder.AuthFailure)


def test_an_unreadable_credential_file_is_not_authentication_sr068(tmp_path):
    with pytest.raises(feeder.SourceFailure) as caught:
        feeder.read_claude({"AI_USAGE_CLAUDE_CREDENTIALS": str(tmp_path / "missing.json"), "_now": NOW})
    assert not isinstance(caught.value, feeder.AuthFailure)


def test_no_reader_rewraps_an_authfailure_as_a_plain_failure_sr068():
    """The class must REACH run_cycle. A handler like `except SourceFailure as e:
    raise SourceFailure(...)` in any reader would turn a dead login back into a
    network blip. Checked structurally, per reader."""
    tree = ast.parse((AI_DIR / "ai_usage_feeder.py").read_text(encoding="utf-8"))
    readers = {"read_codex", "_read_claude_account", "read_claude", "read_claude2",
               "read_gemini", "read_opencode", "http_json"}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in readers:
            for handler in [n for n in ast.walk(node) if isinstance(n, ast.ExceptHandler)]:
                names = ast.dump(handler.type) if handler.type is not None else ""
                catches_plain = "SourceFailure" in names and "AuthFailure" not in names
                reraises_plain = any(
                    isinstance(r, ast.Raise) and r.exc is not None
                    and isinstance(r.exc, ast.Call)
                    and getattr(r.exc.func, "id", "") == "SourceFailure"
                    for r in ast.walk(handler))
                assert not (catches_plain and reraises_plain), \
                    "%s re-wraps a SourceFailure (and so an AuthFailure)" % node.name


def test_run_cycle_catches_authfailure_before_sourcefailure_sr068():
    tree = ast.parse((AI_DIR / "ai_usage_feeder.py").read_text(encoding="utf-8"))
    run = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_cycle"][0]
    handlers = [h for h in ast.walk(run) if isinstance(h, ast.ExceptHandler) and h.type is not None]
    order = [ast.dump(h.type) for h in handlers]
    first_auth = next(i for i, d in enumerate(order) if "AuthFailure" in d)
    first_plain = next(i for i, d in enumerate(order) if "SourceFailure" in d and "AuthFailure" not in d)
    assert first_auth < first_plain


# ═══════════════════════════════════════════════════════════════════════════
# TC-1125 — the two-cycle count lives in the state file (LLR-1054)
# ═══════════════════════════════════════════════════════════════════════════

def auth_dead(e):
    raise feeder.AuthFailure("codex: HTTP 401", status=401)


def blip(e):
    raise feeder.SourceFailure("codex: URLError")


def good_codex(e):
    return feeder.parse_codex(CODEX_RESULT, e["_now"])


def test_one_auth_failure_still_reposts_the_last_reading_sr068(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex")
    cycle(env, {"codex": good_codex}, NOW)
    rec, _p, failures = cycle(env, {"codex": auth_dead}, NOW + 60)
    assert rec.bodies[0]["observed_at"] == feeder.rfc3339_utc(NOW), "not confirmed yet"
    assert rec.bodies[0]["value"] == 66.0
    assert saved_state(env)[feeder.AUTH_COUNTS_KEY] == {"codex": 1}
    assert any("authentication failure 1 of 2" in line for line in failures)


def test_two_consecutive_auth_failures_post_no_observed_at_and_mark_the_entry_sr068(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex")
    cycle(env, {"codex": good_codex}, NOW)
    cycle(env, {"codex": auth_dead}, NOW + 60)
    rec, _p, failures = cycle(env, {"codex": auth_dead}, NOW + 120)
    assert stale_on_arrival(rec.bodies[0]) and not feeder.is_fresh(rec.bodies[0], NOW + 120)
    state = saved_state(env)
    assert state["ai-usage-codex"] == {"auth_failed": True, "at": NOW + 120}, \
        "marked, and the KEY is kept"
    assert state[feeder.AUTH_COUNTS_KEY] == {"codex": 2}
    assert any("CONFIRMED" in line for line in failures)


def test_the_count_survives_a_restart_sr068(tmp_path):
    """Save, load_state in a fresh process-equivalent, increment, reach 2."""
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex")
    cycle(env, {"codex": good_codex}, NOW)
    cycle(env, {"codex": auth_dead}, NOW + 60)
    reloaded = feeder.load_state(env["AI_USAGE_STATE_FILE"], NOW + 90)
    assert reloaded[feeder.AUTH_COUNTS_KEY] == {"codex": 1}
    rec, _p, _f = cycle(env, {"codex": auth_dead}, NOW + 120)
    assert stale_on_arrival(rec.bodies[0])
    assert feeder.load_state(env["AI_USAGE_STATE_FILE"], NOW + 150)[feeder.AUTH_COUNTS_KEY] == {"codex": 2}


def test_a_success_resets_the_count_sr068(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex")
    cycle(env, {"codex": good_codex}, NOW)
    cycle(env, {"codex": auth_dead}, NOW + 60)
    cycle(env, {"codex": good_codex}, NOW + 120)
    assert feeder.AUTH_COUNTS_KEY not in saved_state(env)
    rec, _p, _f = cycle(env, {"codex": auth_dead}, NOW + 180)
    assert rec.bodies[0]["observed_at"] == feeder.rfc3339_utc(NOW + 120), \
        "auth, success, auth is ONE failure, not two"


def test_a_non_auth_failure_between_two_auth_failures_resets_the_count_sr068(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex")
    cycle(env, {"codex": good_codex}, NOW)
    cycle(env, {"codex": auth_dead}, NOW + 60)
    cycle(env, {"codex": blip}, NOW + 120)
    assert feeder.AUTH_COUNTS_KEY not in saved_state(env)
    rec, _p, _f = cycle(env, {"codex": auth_dead}, NOW + 180)
    assert rec.bodies[0]["observed_at"] == feeder.rfc3339_utc(NOW), "count restarted at one"


def test_the_count_is_per_source_not_global_sr068(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex,opencode")
    ok_open = {"opencode": lambda e: feeder.parse_opencode(OPENCODE_BODY, e["_now"])}
    cycle(env, {"codex": good_codex, **ok_open}, NOW)
    cycle(env, {"codex": auth_dead, **ok_open}, NOW + 60)
    rec, _p, _f = cycle(env, {"codex": auth_dead, **ok_open}, NOW + 120)
    by_id = rec.by_id()
    assert stale_on_arrival(by_id["ai-usage-codex"])
    assert not stale_on_arrival(by_id["ai-usage-opencode-weekly"]), "opencode is healthy"
    assert saved_state(env)[feeder.AUTH_COUNTS_KEY] == {"codex": 2}


def test_a_source_that_is_switched_off_forgets_its_count_sr068(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex")
    cycle(env, {"codex": good_codex}, NOW)
    cycle(env, {"codex": auth_dead}, NOW + 60)
    off = base_env(tmp_path, AI_USAGE_SOURCES="opencode")
    cycle(off, {"opencode": lambda e: feeder.parse_opencode(OPENCODE_BODY, e["_now"])}, NOW + 120)
    assert feeder.AUTH_COUNTS_KEY not in saved_state(off)


@pytest.mark.parametrize("entry,expected", [
    ({"codex": 1}, {"codex": 1}),
    ({"codex": 2, "gemini": 1}, {"codex": 2, "gemini": 1}),
    ({"codex": 0}, {}), ({"codex": -1}, {}), ({"codex": 101}, {}),
    ({"codex": True}, {}), ({"codex": 1.5}, {}), ({"codex": "2"}, {}),
    ({"not-a-source": 1}, {}), ({}, {}), ([], {}), ("x", {}), (None, {}), (7, {}),
])
def test_the_count_has_its_own_validator_sr068(entry, expected):
    assert feeder.validate_auth_counts(entry, feeder.SOURCE_READERS) == expected


@pytest.mark.parametrize("entry,ok", [
    ({"auth_failed": True, "at": NOW}, True),
    ({"auth_failed": True, "at": float(NOW)}, True),
    ({"auth_failed": False, "at": NOW}, False),
    ({"auth_failed": 1, "at": NOW}, False),
    ({"auth_failed": True}, False),
    ({"auth_failed": True, "at": "now"}, False),
    ({"auth_failed": True, "at": True}, False),
    ({"auth_failed": True, "at": 5}, False),                       # before the epoch floor
    ({"auth_failed": True, "at": NOW + 10 * 3600}, False),         # from the future
    ({"auth_failed": True, "at": float("nan")}, False),
    ("auth_failed", False), (None, False), ([], False),
])
def test_the_marker_has_its_own_validator_sr068(entry, ok):
    got = feeder.validate_auth_marker(entry, NOW)
    assert (got is not None) is ok


def test_load_state_keeps_counts_and_markers_and_drops_junk_sr068(tmp_path):
    path = tmp_path / "usage-state.json"
    path.write_text(json.dumps({
        feeder.AUTH_COUNTS_KEY: {"codex": 2, "bogus": 1, "claude": "x"},
        "ai-usage-codex": {"auth_failed": True, "at": NOW},
        "ai-usage-claude-session": {"value": 12.0, "observed_at": NOW, "window_end": NOW + 3600,
                                    "window_seconds": 18000},
        "ai-usage-claude-weekly": {"auth_failed": "yes", "at": NOW},
        "junk": 5}), encoding="utf-8")
    state = feeder.load_state(str(path), NOW + 5)
    assert state[feeder.AUTH_COUNTS_KEY] == {"codex": 2}
    assert state["ai-usage-codex"] == {"auth_failed": True, "at": NOW}
    assert "ai-usage-claude-session" in state
    assert "ai-usage-claude-weekly" not in state and "junk" not in state


def test_the_state_file_holds_counts_and_stamps_only_sr068(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="claude")
    creds = tmp_path / ".credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": SENTINEL}}), encoding="utf-8")
    env["AI_USAGE_CLAUDE_CREDENTIALS"] = str(creds)
    readers = {"claude": feeder.read_claude}
    import unittest.mock as mock
    with mock.patch.object(feeder, "http_json",
                           side_effect=feeder.AuthFailure("claude: HTTP 401", status=401)):
        cycle(env, readers, NOW)
        cycle(env, readers, NOW + 60)
    text = (tmp_path / "usage-state.json").read_text(encoding="utf-8")
    assert SENTINEL not in text and "Bearer" not in text
    assert json.loads(text)[feeder.AUTH_COUNTS_KEY] == {"claude": 2}


# ═══════════════════════════════════════════════════════════════════════════
# TC-1126 — marked, sticky, and the optional gauge still posts (LLR-1055)
# ═══════════════════════════════════════════════════════════════════════════

def claude_ok(e):
    return feeder.parse_claude(CLAUDE_BODY, e["_now"])


def claude_dead(e):
    raise feeder.AuthFailure("claude: credential x carries no usable key", code="no_usable_key")


def test_after_a_confirmed_auth_failure_the_optional_fable_gauge_still_posts_stale_sr068(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="claude")
    rec0, _p, _f = cycle(env, {"claude": claude_ok}, NOW)
    assert "ai-usage-claude-weekly-fable" in rec0.by_id()
    cycle(env, {"claude": claude_dead}, NOW + 60)
    rec, _p, _f = cycle(env, {"claude": claude_dead}, NOW + 120)
    by_id = rec.by_id()
    assert set(by_id) == {"ai-usage-claude-session", "ai-usage-claude-weekly",
                          "ai-usage-claude-weekly-fable"}, "the Fable band must not vanish"
    for body in by_id.values():
        assert stale_on_arrival(body)
    state = saved_state(env)
    assert state["ai-usage-claude-weekly-fable"]["auth_failed"] is True, "key kept, entry marked"


def test_the_fable_gauge_still_posts_after_a_following_network_failure_sr068(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="claude")

    def network(e):
        raise feeder.SourceFailure("claude: URLError")

    cycle(env, {"claude": claude_ok}, NOW)
    cycle(env, {"claude": claude_dead}, NOW + 60)
    cycle(env, {"claude": claude_dead}, NOW + 120)
    rec, _p, _f = cycle(env, {"claude": network}, NOW + 180)
    by_id = rec.by_id()
    assert "ai-usage-claude-weekly-fable" in by_id
    for body in by_id.values():
        assert stale_on_arrival(body)


def test_auth_then_auth_then_network_stays_stale_on_arrival_sr068(tmp_path):
    """THE STICKY RULE: build_post re-posts a stored reading on ANY later failure,
    so without the marker a network blip would bring the old colour back."""
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex")
    cycle(env, {"codex": good_codex}, NOW)
    cycle(env, {"codex": auth_dead}, NOW + 60)
    cycle(env, {"codex": auth_dead}, NOW + 120)
    rec, _p, _f = cycle(env, {"codex": blip}, NOW + 180)
    assert stale_on_arrival(rec.bodies[0])
    rec2, _p, _f = cycle(env, {"codex": blip}, NOW + 240)
    assert stale_on_arrival(rec2.bodies[0]), "and it stays that way"


def test_a_network_failure_with_no_auth_failure_keeps_todays_ageing_sr068(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex")
    cycle(env, {"codex": good_codex}, NOW)
    for step in (60, 120, 180):
        rec, _p, _f = cycle(env, {"codex": blip}, NOW + step)
        assert rec.bodies[0]["observed_at"] == feeder.rfc3339_utc(NOW)
        assert rec.bodies[0]["value"] == 66.0
    assert feeder.AUTH_COUNTS_KEY not in saved_state(env)


def test_a_real_reading_recovers_a_confirmed_gauge_sr068(tmp_path):
    env = base_env(tmp_path, AI_USAGE_SOURCES="codex")
    cycle(env, {"codex": good_codex}, NOW)
    cycle(env, {"codex": auth_dead}, NOW + 60)
    cycle(env, {"codex": auth_dead}, NOW + 120)
    rec, _p, _f = cycle(env, {"codex": good_codex}, NOW + 180)
    assert rec.bodies[0]["observed_at"] == feeder.rfc3339_utc(NOW + 180)
    assert saved_state(env)["ai-usage-codex"]["value"] == 34.0
    assert feeder.AUTH_COUNTS_KEY not in saved_state(env)


def test_a_gauge_never_seen_is_not_conjured_by_a_confirmed_failure_sr068(tmp_path):
    """Marking an optional gauge nobody has ever posted would make an
    'unavailable' Fable band appear out of nothing."""
    env = base_env(tmp_path, AI_USAGE_SOURCES="claude")
    cycle(env, {"claude": claude_dead}, NOW)
    rec, _p, _f = cycle(env, {"claude": claude_dead}, NOW + 60)
    assert "ai-usage-claude-weekly-fable" not in rec.by_id()
    assert "ai-usage-claude-weekly-fable" not in saved_state(env)


def test_a_marker_is_not_a_reading_so_build_post_gives_the_stale_body_sr068():
    spec = [s for s in feeder.GAUGE_SPECS if s.key == "ai-usage-codex"][0]
    body, fresh = feeder.build_post(spec, None, {"auth_failed": True, "at": NOW}, NOW)
    assert stale_on_arrival(body) and fresh is False
    stored = {"value": 34.0, "observed_at": NOW - 60, "window_end": NOW + 3600,
              "window_seconds": 7 * 24 * 3600}
    assert not stale_on_arrival(feeder.build_post(spec, None, stored, NOW)[0])
    # The flag alone is enough even if the marker was somehow lost.
    flagged, _ = feeder.build_post(spec, None, stored, NOW, auth_confirmed=True)
    assert stale_on_arrival(flagged)
    # ... and never at the expense of a reading this very cycle.
    live, fresh = feeder.build_post(
        spec, feeder.Reading(10.0, NOW + 3600, 7 * 24 * 3600), stored, NOW, auth_confirmed=True)
    assert fresh and not stale_on_arrival(live)


def test_claude_mid_rotation_blip_does_not_grey_the_gauge_sr068(monkeypatch, tmp_path):
    """The reason for the two-cycle rule: the refresh unit rewrites the credential
    file, and a read in the middle can find a file with no key in it."""
    env = base_env(tmp_path, AI_USAGE_SOURCES="claude")
    creds = tmp_path / ".credentials.json"
    env["AI_USAGE_CLAUDE_CREDENTIALS"] = str(creds)
    monkeypatch.setattr(feeder, "http_json", lambda *a, **k: CLAUDE_BODY)
    readers = {"claude": feeder.read_claude}

    creds.write_text('{"claudeAiOauth":{"accessToken":"T1"}}', encoding="utf-8")
    cycle(env, readers, NOW)
    creds.write_text('{"claudeAiOauth":{}}', encoding="utf-8")                 # caught mid-rotation
    rec, _p, failures = cycle(env, readers, NOW + 60)
    assert not stale_on_arrival(rec.by_id()["ai-usage-claude-session"])
    assert any("no usable key" in line for line in failures)
    creds.write_text('{"claudeAiOauth":{"accessToken":"T2"}}', encoding="utf-8")   # rotation finished
    rec, _p, _f = cycle(env, readers, NOW + 120)
    assert rec.by_id()["ai-usage-claude-session"]["observed_at"] == feeder.rfc3339_utc(NOW + 120)
    assert feeder.AUTH_COUNTS_KEY not in saved_state(env)


# ═══════════════════════════════════════════════════════════════════════════
# TC-1127 — a whole cycle with the new credentials writes nothing but the state
# (LLR-1050, SR-021)
# ═══════════════════════════════════════════════════════════════════════════

def test_a_whole_cycle_with_claude2_and_gemini_writes_only_the_state_file_sr067(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".claude-2").mkdir(parents=True)
    claude2 = home / ".claude-2" / ".credentials.json"
    claude2.write_text(json.dumps({"claudeAiOauth": {"accessToken": "C2-" + SENTINEL}}), encoding="utf-8")
    gemini = gemini_creds(home)
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    def fake_http_json(url, headers, timeout, data=None):
        if url.endswith(":loadCodeAssist"):
            return LOAD_OK
        if url.endswith(":retrieveUserQuota"):
            return quota(bucket("gemini-2.5-pro", 0.4))
        return CLAUDE_BODY

    monkeypatch.setattr(feeder, "http_json", fake_http_json)
    env = base_env(state_dir, AI_USAGE_SOURCES="claude2,gemini",
                   AI_USAGE_CLAUDE2_CREDENTIALS=str(claude2),
                   AI_USAGE_GEMINI_CREDENTIALS=str(gemini))
    env["AI_USAGE_STATE_FILE"] = str(state_dir / "usage-state.json")
    env["STATE_DIRECTORY"] = str(state_dir)
    snapshot = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    rec, _posted, failures = cycle(env, {"claude2": feeder.read_claude2,
                                         "gemini": feeder.read_gemini}, NOW)
    assert failures == [], failures
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == snapshot
    assert sorted(p.name for p in state_dir.iterdir()) == ["usage-state.json"]
    assert SENTINEL not in (state_dir / "usage-state.json").read_text(encoding="utf-8")
    assert "ai-usage-gemini-daily" in rec.by_id()
    assert "ai-usage-claude2-weekly" in rec.by_id()


# ═══════════════════════════════════════════════════════════════════════════
# TC-1128 — the knobs are documented where operators look (LLR-1057)
# ═══════════════════════════════════════════════════════════════════════════

def test_env_example_documents_the_new_knobs_and_the_six_ring_example_sr067():
    text = (REPO / "stack" / ".env.example").read_text(encoding="utf-8")
    for knob in ("AI_USAGE_CLAUDE2_CREDENTIALS", "AI_USAGE_GEMINI_CREDENTIALS",
                 "AI_USAGE_GEMINI_MODELS", "AI_USAGE_GEMINI_PROJECT"):
        assert re.search(r"^%s=" % knob, text, re.M), knob
    assert "AI_USAGE_SOURCES=claude,claude2,codex,gemini,opencode" in text
    # An absent knob still means the original three: the new sources are opt-in.
    assert re.search(r"^AI_USAGE_SOURCES=codex,claude,opencode$", text, re.M)


def test_config_reference_covers_every_ai_usage_knob_in_env_example_sr067():
    reference = (REPO / "docs" / "config-reference.md").read_text(encoding="utf-8")
    example = (REPO / "stack" / ".env.example").read_text(encoding="utf-8")
    knobs = set(re.findall(r"^(AI_USAGE_[A-Z0-9_]+)=", example, re.M))
    # The feeder's own plumbing knobs are documented in .env.example alone.
    plumbing = {"AI_USAGE_ENABLED", "AI_USAGE_USER", "AI_USAGE_USER_ACCOUNT",
                "AI_USAGE_FEED_URL", "AI_USAGE_FEED_TOKEN", "AI_USAGE_OPENCODE_AUTH"}
    missing = sorted(k for k in knobs - plumbing if k not in reference)
    assert not missing, missing
