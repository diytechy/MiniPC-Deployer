"""Static contracts for what Finance-Auditor waits for at start, and its rollback settings.

Verifies: SR-099, LLR-1204..LLR-1206, TC-1349..TC-1351.

SR-099 (the 2026-10-22 cutover defect): the hub reads Sure since the cutover, yet
`finance-auditor` would only start once Actual was healthy and never waited for
Sure. Compose cannot make a dependency conditional on DATA_SOURCE, so both
ledgers are OPTIONAL `service_healthy` dependencies (`required: false`): compose
waits for whichever one is starting in the same invocation, and an unhealthy,
absent or profile-disabled ledger never blocks the auditor or invalidates the
project. The tracker stays a required dependency: every source posts to it.

These read the committed template only: nothing here runs compose or a container.
The compose behaviour the shape relies on (S1..S12 in the docker-compose.yml
comment) was measured with a toy project under Docker Compose v5.3.0.
"""
from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.full, pytest.mark.smoke]

COMPOSE = ROOT / "stack/docker-compose.yml"
EXAMPLE = ROOT / "stack/.env.example"
SERVICE_KEY = re.compile(r"^  [a-z][a-z0-9-]*:\s*$")


def auditor():
    """The finance-auditor service from the committed template, uninterpolated."""
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["finance-auditor"]


def example_knobs():
    """KEY=value literals of .env.example; never the private .env."""
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", EXAMPLE.read_text(encoding="utf-8"), re.M))


def auditor_block():
    """The raw lines of the finance-auditor service: its comments are part of the contract."""
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    start = lines.index("  finance-auditor:")
    end = next(i for i in range(start + 1, len(lines)) if SERVICE_KEY.match(lines[i]))
    return lines[start:end]


def marked_rollback(lines, needle):
    """True when the line holding `needle`, or the comment run directly above it, says ROLLBACK."""
    index = next(i for i, line in enumerate(lines) if needle in line)
    if "ROLLBACK" in lines[index]:
        return True
    above = index - 1
    while above >= 0 and lines[above].lstrip().startswith("#"):
        if "ROLLBACK" in lines[above]:
            return True
        above -= 1
    return False


def test_both_ledgers_are_optional_healthy_dependencies_tc1349():
    """LLR-1204: actual and sure-web optional-healthy; tracker required-healthy; nothing else."""
    depends = auditor()["depends_on"]
    assert depends == {
        "actual": {"condition": "service_healthy", "required": False},
        "sure-web": {"condition": "service_healthy", "required": False},
        "tracker": {"condition": "service_healthy"},
    }
    # A required dependency on sure-web makes every project with the `sure`
    # profile off invalid ("depends on undefined service"): that is the hub's
    # bulk `docker compose up -d` at every boot, and firstboot exits on it.
    assert depends["sure-web"]["required"] is False
    # A required dependency on actual leaves the auditor Created-not-started, and
    # the whole `up -d` exiting 1, whenever Actual is unhealthy - in sure mode too.
    assert depends["actual"]["required"] is False
    # The sure-web dependency is only worth waiting for if the auditor can reach it.
    assert "sure" in auditor()["networks"]


def test_the_unsolved_sure_residual_is_stated_not_waved_away_tc1349():
    """LLR-1204: the comment and the runtime flow state what the optional dependency leaves open.

    On the hub the `sure` profile is off, and after a reboot the daemon restarts
    containers without reading depends_on, so nothing waits for sure-web. A
    RUN_AT that arrives before Sure answers loses that day's run (FA retries
    HTTP 429 only, and stays up, so its restart policy never fires). An earlier
    draft called that cost "nothing"; this pins the honest statement.
    """
    comment = " ".join(line.strip().lstrip("#").strip() for line in auditor_block()
                       if line.lstrip().startswith("#"))
    for claim in ("cost is nothing", "costs nothing"):
        assert claim not in comment
    for fact in ("restarts containers without reading depends_on", "RUN_AT arrives before Sure answers", "next day's",
                 "belongs in Finance-Auditor"):
        assert fact in comment, "compose comment no longer states: %s" % fact
    doc = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    start = doc.index("### What Finance-Auditor waits for at start")
    section = doc[start:doc.index("\n### ", start + 1)]
    flat = " ".join(section.split())
    assert "costs nothing" not in flat
    for fact in ("nothing reads `depends_on`", "next day's `RUN_AT`", "belongs in Finance-Auditor",
                 "Sure not answering yet"):
        assert fact in flat, "architecture SR-099 section no longer states: %s" % fact


def test_actual_settings_are_marked_rollback_only_and_still_passed_tc1350():
    """LLR-1205: Actual's lines stay wired (rollback is a one-knob flip) and say so."""
    service = auditor()
    settings = service["environment"]
    assert settings["ACTUAL_URL"] == "http://actual:5006"
    assert settings["ACTUAL_SYNC_ID"] == "${FINANCE_ACTUAL_SYNC_ID}"
    assert settings["ACTUAL_PASSWORD"] == "${FINANCE_ACTUAL_PASSWORD}"
    assert "finance_actual_data:/data/actual" in service["volumes"]
    block = auditor_block()
    for needle in ("ACTUAL_URL: http://actual:5006", "ACTUAL_SYNC_ID:", "ACTUAL_PASSWORD:",
                   "finance_actual_data:/data/actual"):
        assert marked_rollback(block, needle), "%s is not marked rollback-only" % needle
    assert any("SR-099" in line for line in block)
    example = EXAMPLE.read_text(encoding="utf-8").splitlines()
    for key in ("FINANCE_ACTUAL_SYNC_ID=", "FINANCE_ACTUAL_PASSWORD="):
        assert marked_rollback(example, key), ".env.example %s is not marked rollback-only" % key


def test_template_default_source_stays_actual_tc1351():
    """LLR-1206: the template default stays actual; the hub's own config selects sure."""
    settings = auditor()["environment"]
    assert settings["DATA_SOURCE"] == "${FINANCE_DATA_SOURCE:-actual}"
    assert example_knobs()["FINANCE_DATA_SOURCE"] == "actual"
    # The template ships no Sure key (a T3 secret minted in Sure's UI), so a sure
    # default would fail Finance-Auditor's startup config check on a bare template.
    assert example_knobs()["FINANCE_SURE_API_KEY"] == ""
