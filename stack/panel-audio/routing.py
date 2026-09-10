"""Pure policy for the panel audio broker; performs no device or process I/O.

The policy intentionally models only aliases provisioned in host-private state.
Hardware addresses never cross IF-015, and no implicit capture source is valid.
Implements: SR-023, LLR-007.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Mapping


ALIAS = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
MUTATING_METHODS = frozenset(
    {"discover", "cancel", "pair", "connect", "disconnect", "forget",
     "select_input", "select_output", "set_visualizer"}
)
METHODS = MUTATING_METHODS | {"status", "telemetry"}
HARDWARE_ADDRESS = re.compile(
    r"(?i)(?<![0-9a-f])(?:(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|"
    r"(?:[0-9a-f]{2}_){5}[0-9a-f]{2}|[0-9a-f]{12})(?![0-9a-f])"
)


class PolicyError(ValueError):
    """A request violates the fixed route/device policy."""


@dataclass(frozen=True)
class Device:
    """Sanitized device facts used by policy; alias is not a hardware address."""

    alias: str
    kind: str
    trusted: bool = False
    connected: bool = False

    def __post_init__(self):
        if not ALIAS.fullmatch(self.alias) or HARDWARE_ADDRESS.search(self.alias):
            raise PolicyError("invalid device alias")
        if self.kind not in {"input", "output"}:
            raise PolicyError("invalid device kind")


def validate_action(method: str, params: Mapping[str, object]) -> None:
    """Validate one IF-015 method before a backend can observe it.

    Inputs: method from METHODS; params JSON object.
    Raises: PolicyError for unknown keys, aliases or implicit capture selection.
    Implements: SR-023, LLR-007.
    """
    if method not in METHODS:
        raise PolicyError("unknown method")
    allowed = {
        "status": set(), "telemetry": set(), "discover": {"timeoutSeconds"}, "cancel": set(),
        "pair": {"alias", "confirmation"}, "connect": {"alias"},
        "disconnect": {"alias"}, "forget": {"alias"},
        "select_input": {"alias", "explicit"}, "select_output": {"alias"},
        "set_visualizer": {"enabled"},
    }[method]
    if set(params) - allowed:
        raise PolicyError("unknown parameter")
    alias = params.get("alias")
    if method in {"pair", "connect", "disconnect", "forget", "select_input", "select_output"} and alias is None:
        raise PolicyError("device alias is required")
    if alias is not None and (not isinstance(alias, str) or not ALIAS.fullmatch(alias) or
                              HARDWARE_ADDRESS.search(alias)):
        raise PolicyError("invalid device alias")
    if method == "select_input" and params.get("explicit") is not True:
        raise PolicyError("input selection must be explicit")
    if method == "discover":
        timeout = params.get("timeoutSeconds", 30)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 120:
            raise PolicyError("discovery timeout outside 1..120 seconds")
    if method == "set_visualizer" and not isinstance(params.get("enabled"), bool):
        raise PolicyError("enabled must be boolean")
    if method == "pair" and "confirmation" in params:
        confirmation = params["confirmation"]
        if not isinstance(confirmation, str) or not 1 <= len(confirmation) <= 16:
            raise PolicyError("pairing confirmation is invalid")


def validate_inventory_action(
    method: str, params: Mapping[str, object], devices: Iterable[Device]
) -> None:
    """Refuse device actions whose alias, kind, or trust is not current.

    Pair may target a discovered untrusted device. Connect/disconnect/forget
    and route selection require a trusted device; input/output selection also
    requires the matching kind. Implements: SR-023, LLR-007.
    """
    alias = params.get("alias")
    if alias is None:
        return
    matches = [device for device in devices if device.alias == alias]
    if len(matches) != 1:
        raise PolicyError("device alias is not in the current inventory")
    device = matches[0]
    if method != "pair" and not device.trusted:
        raise PolicyError("device is not trusted")
    expected_kind = {"select_input": "input", "select_output": "output"}.get(method)
    if expected_kind and device.kind != expected_kind:
        raise PolicyError("device kind does not match route")


def choose_restored_route(
    devices: Iterable[Device], preferred_alias: str | None, fallback_aliases: Iterable[str]
) -> str | None:
    """Choose a trusted output preference or the first trusted fallback.

    Untrusted and input devices are never auto-restored. None means the backend
    must retain its safe local/default output rather than guessing.
    Implements: SR-023, LLR-007.
    """
    eligible = {d.alias for d in devices if d.kind == "output" and d.trusted}
    candidates = ([preferred_alias] if preferred_alias else []) + list(fallback_aliases)
    return next((alias for alias in candidates if alias in eligible), None)
