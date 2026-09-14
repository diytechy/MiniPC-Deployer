"""SR-028 rocker privilege boundary and readout event regression tests."""
import importlib.machinery
import importlib.util
from pathlib import Path
from unittest.mock import Mock
import pytest

WALL = Path(__file__).resolve().parents[1] / "stack/autoinstall/wall"

def module(name, filename):
    loader = importlib.machinery.SourceFileLoader(name, str(WALL / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result

@pytest.mark.parametrize("command", [b"up\n", b"down\n"])
def test_broker_only_maps_exact_directions_sr028(command):
    broker = module("volume_request", "panel-volume-request.py")
    apply = Mock(return_value=0)
    assert broker.handle_request(command, apply) == b"ok\n"
    apply.assert_called_once_with(["volume", command[:-1].decode()])

@pytest.mark.parametrize("command", [b"up", b"up\nset speaker\n", b"status\n", b"", b"UP\n", b"down\nX"])
def test_broker_refuses_other_requests_sr028(command):
    broker = module("volume_request", "panel-volume-request.py")
    apply = Mock()
    assert broker.handle_request(command, apply) == b"error\n"
    apply.assert_not_called()

def test_broker_reports_failed_apply_sr028():
    broker = module("volume_request", "panel-volume-request.py")
    assert broker.handle_request(b"up\n", Mock(return_value=1)) == b"error\n"


@pytest.mark.parametrize("sequence", [-1, True, 9007199254740992])
def test_invalid_rocker_readout_sequence_repairs_to_muted_sr028(sequence):
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    state["input_muted"] = False
    state["volume_event_seq"] = sequence

    normalized = policy.normalize(state)
    assert normalized["volume_event_seq"] == 0
    assert normalized["input_muted"] is True


@pytest.mark.parametrize("level,louder", [(0, False), (100, True), (40, True)])
def test_every_rocker_event_has_readout_sequence_sr028(level, louder):
    policy = module("volume_policy", "wall_audio_state.py")
    state = policy.default_state()
    state["volume"]["speaker"] = level
    for seq in (1, 2):
        state, _ = policy.apply_event(state, {"kind": "nudge_volume", "louder": louder})
        assert state["volume_event_seq"] == seq
    assert policy.normalize(state)["volume_event_seq"] == 2

def test_rocker_keeps_dynamic_identity_and_only_unix_socket_sr028():
    unit = (WALL / "wall-volume-keys.service").read_text()
    assert "DynamicUser=true" in unit
    assert "RestrictAddressFamilies=AF_UNIX" in unit
    assert "ProtectSystem=strict" in unit
    sock = (WALL / "wall-volume-request.socket").read_text()
    assert "SocketGroup=input" in sock
    assert "SocketMode=0660" in sock
