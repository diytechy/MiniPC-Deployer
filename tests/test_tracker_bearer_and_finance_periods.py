"""Static contracts for the tracker's bearer token and Finance-Auditor's periods input.

Verifies: SR-097, SR-098, LLR-1200..LLR-1203, TC-1345..TC-1348.

SR-097 (open-items C51): the tracker requires a bearer on every /api/* POST, from
the hub's one minted feed token, and every poster on the hub sends it. A poster
that sent nothing would have its lane refused 401 the moment the token is set, so
the poster inventory is DISCOVERED from the tree, not listed from memory: a new
file that talks to the tracker fails here until it is classified.

SR-098 (W12 task 5): Finance-Auditor reads its serving-periods DIRECTORY through
a read-only mount and its expected weekly net from a knob.

These prove authored wiring only; nothing here runs a container or a tracker.
"""
from pathlib import Path
import re
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.full, pytest.mark.smoke]

# Every file under stack/ that names a tracker POST endpoint or a feed-token /
# feed-URL knob, and what it is. A SENDER posts to the tracker and must attach
# the bearer from the named knob; anything else names the knob or endpoint
# without posting, and the reason is written beside it.
SENDERS = {
    "stack/ai-usage/ai_usage_feeder.py": "AI_USAGE_FEED_TOKEN",
    "stack/weight/weight_feeder.py": "WEIGHT_FEED_TOKEN",
    "stack/sleep/sleep_feeder.py": "SLEEP_FEED_TOKEN",
    "stack/backup/common.sh": "NAGLIGHT_TOKEN",
    "stack/samba/library-guard.sh": "NAGLIGHT_TOKEN",
    "stack/tracker/tracker-defs-guard.sh": "NAGLIGHT_TOKEN",
    "stack/tracker/frame-freshness.sh": "NAGLIGHT_TOKEN",
}
NOT_SENDERS = {
    "stack/docker-compose.yml": "wiring: hands each container its token (asserted below)",
    "stack/panel-access/docker-compose.access.yml": "wiring: the panel gateway reads its token from a file",
    "stack/autoinstall/firstboot.sh": "log text naming the knobs",
    "stack/autoinstall/wall/render-wall-host-config.py": "renders the panel's FEED_TOKEN into host.json",
    "stack/ai-usage/setup-ai-usage.sh": "installer: validates the feed URL",
    "stack/sleep/setup-sleep.sh": "installer: validates the feed URL",
    "stack/sleep/apply-env-lines.py": "installer: copies the knobs into the feeder env",
    "stack/weight/setup-weight.sh": "installer: validates the feed URL",
    "stack/backup/backup.sh": "comment only; posts through common.sh",
    "stack/backup/library-backup.sh": "comment only; posts through common.sh",
    "stack/backup/library-backup-legacy.sh": "comment only; posts through common.sh (the legacy layout, SR-133)",
}
DISCOVER = re.compile(
    r"api/(feed|backup-state|check|count|catchup|undo)|NAGLIGHT_(TOKEN|FEED_URL)|[A-Z]+_FEED_(TOKEN|URL)")
SKIP = re.compile(r"(\.md|\.example|\.deprecated|\.json|\.lock|\.png|\.svg|\.txt)$|/tests/")


def stack():
    """The committed Compose template, without substituting any value."""
    return yaml.safe_load((ROOT / "stack/docker-compose.yml").read_text(encoding="utf-8"))


def example_lines():
    """KEY=value lines of stack/.env.example; never the private .env."""
    text = (ROOT / "stack/.env.example").read_text(encoding="utf-8")
    return re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", text, re.M)


def tracked_stack_files():
    out = subprocess.run(["git", "ls-files", "stack"], cwd=ROOT, check=True,
                         capture_output=True, text=True).stdout.split()
    return [p for p in out if not SKIP.search(p)]


def test_tracker_requires_the_shared_token_tc1345():
    """LLR-1200: compose passes TRACKER_API_TOKEN to the tracker; the template ships it blank."""
    settings = stack()["services"]["tracker"]["environment"]
    assert settings["TRACKER_API_TOKEN"] == "${TRACKER_API_TOKEN:-}"
    keys = [k for k, _ in example_lines()]
    assert keys.count("TRACKER_API_TOKEN") == 1
    assert dict(example_lines())["TRACKER_API_TOKEN"] == ""
    # Finance-Auditor's post carries the same token under its own knob.
    fa = stack()["services"]["finance-auditor"]["environment"]
    assert fa["TRACKER_FEED_TOKEN"] == "${FINANCE_TRACKER_FEED_TOKEN}"


def test_every_tracker_poster_is_classified_tc1346():
    """LLR-1201: the poster inventory is discovered, so a new poster cannot slip past."""
    found = set()
    for rel in tracked_stack_files():
        text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        if DISCOVER.search(text):
            found.add(rel)
    unclassified = found - set(SENDERS) - set(NOT_SENDERS)
    assert not unclassified, "classify these as a sender (with its token knob) or not: %s" % sorted(unclassified)
    gone = (set(SENDERS) | set(NOT_SENDERS)) - found
    assert not gone, "listed but no longer matched (stale inventory): %s" % sorted(gone)


@pytest.mark.parametrize("rel,knob", sorted(SENDERS.items()))
def test_every_sender_attaches_its_bearer_tc1347(rel, knob):
    """LLR-1201: each sender reads its token knob and puts it in an Authorization: Bearer header."""
    text = (ROOT / rel).read_text(encoding="utf-8")
    if rel.endswith(".py"):
        assert re.search(r'env\.get\("%s"\)' % knob, text), "%s does not read %s" % (rel, knob)
        assert re.search(r'headers\["Authorization"\] = "Bearer " \+ token', text), \
            "%s builds no feed bearer header" % rel
    else:
        bearer = re.findall(r'Authorization: Bearer \$\{%s\}' % knob, text)
        assert bearer, "%s sends no Authorization: Bearer ${%s}" % (rel, knob)


def test_finance_auditor_reads_periods_dir_read_only_tc1348():
    """LLR-1202/LLR-1203: a read-only DIRECTORY mount at /config, and the weekly net knob."""
    service = stack()["services"]["finance-auditor"]
    settings = service["environment"]
    assert settings["PERIODS_CONFIG_DIR"] == "/config"
    assert settings["EXPECTED_WEEKLY_NET_CENTS"] == "${FINANCE_EXPECTED_WEEKLY_NET_CENTS:-10000}"
    mounts = [m for m in service["volumes"] if m.endswith(":/config:ro")]
    assert mounts == ["${FINANCE_PERIODS_CONFIG_DIR:-/etc/homehub-finance}:/config:ro"]
    # Never a file mount: a replaced file would detach it (FA SR-051).
    assert not any("serving-periods.json" in str(m) for m in service["volumes"])
    knobs = dict(example_lines())
    assert knobs["FINANCE_PERIODS_CONFIG_DIR"] == "/etc/homehub-finance"
    assert knobs["FINANCE_EXPECTED_WEEKLY_NET_CENTS"] == "10000"
    # No periods content in this public repo: no serving-periods.json is tracked here.
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, check=True,
                             capture_output=True, text=True).stdout.split()
    assert not [p for p in tracked if p.endswith("serving-periods.json")]
