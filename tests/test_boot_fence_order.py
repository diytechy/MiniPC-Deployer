"""The INPUT fences have an order at boot, and each unit has to state its half.

Three fences insert at the head of INPUT, and two of them assert a position:
homehub-game-isolation checks it is first the moment it has inserted, and
homehub-dns-console must stay above ts-input, which its two-minute check enforces.
Left to start side by side, another fence lands between the game fence's insert and
its check, the unit fails, and the relay goes with it. That happened on 2026-11-04.
"""
from __future__ import annotations

import re
from pathlib import Path

STACK = Path(__file__).resolve().parents[1] / "stack"


def _after(unit: str) -> set[str]:
    text = (STACK / unit).read_text(encoding="utf-8")
    names: set[str] = set()
    for line in re.findall(r"^After=(.*)$", text, flags=re.M):
        names.update(line.split())
    return names


def test_the_game_fence_runs_after_the_fences_that_insert_beside_it():
    after = _after("game-isolation/homehub-game-isolation.service")
    assert "homehub-llm-isolation.service" in after
    assert "homehub-rustdesk-isolation.service" in after


def test_the_dns_console_fence_runs_last_so_it_stays_above_ts_input():
    after = _after("dns-console/homehub-dns-console.service")
    assert "homehub-game-isolation.service" in after
    assert "homehub-llm-isolation.service" in after


def test_neither_earlier_fence_waits_for_the_game_fence():
    # Either of these would be an ordering cycle, and systemd breaks a cycle by
    # dropping a unit from the boot transaction without failing anything.
    assert "homehub-game-isolation.service" not in _after("llm-isolation/homehub-llm-isolation.service")
    assert "homehub-game-isolation.service" not in _after("rustdesk/homehub-rustdesk-isolation.service")


def test_the_relay_reasserts_the_fence_after_its_precheck():
    # tailscaled inserts ts-input above the fence after the fence has run, and the
    # relay's postcheck requires the fence to be first. Without firstboot to restart
    # it, only this line puts it back before the relay starts.
    text = (STACK / "game-isolation/homehub-gunmaster3-relay.service").read_text(encoding="utf-8")
    pre = re.findall(r"^ExecStartPre=(.*)$", text, flags=re.M)
    assert pre[-1].endswith("game-isolation/game-isolation.sh"), pre
    assert any(line.endswith("gunmaster3-relay.sh precheck") for line in pre[:-1]), pre
