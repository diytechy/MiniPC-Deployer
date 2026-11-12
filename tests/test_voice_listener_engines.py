"""The hub voice listener on the real engines and against the real action
interface (SR-167, SR-168). OPT-IN: every test here skips unless it is told
where the pinned runtime, models and synthetic speech are, so the ordinary
suites never need them.

    VOICE_LISTENER_MODELS        the folder holding the two model folders named
                                 in stack/voice-listener/models.manifest.json
    VOICE_LISTENER_CLIPS         a folder of synthetic commands as EntertainmentPC's
                                 scripts/voice/New-SyntheticCommands.ps1 writes
                                 them: wake/ and nowake/, each WAV with a .json
                                 (text, request, wake). Windows' stock voices
                                 saying invented phrases: no household sound.
    VOICE_LISTENER_SCENESERVICE  EntertainmentPC's built SceneService.exe (the
                                 interface's own host; Windows only)

and, for the first two, an interpreter that can import the pinned sherpa_onnx
(requirements.lock; CPython 3.12 on x86-64 Linux, e.g. WSL with the three
wheels unpacked on PYTHONPATH).

What this proves: the pinned files are the pinned bytes; the wake phrase's
tokens are the model's; the whole path (gate, wake word, request, speech-to-
text, post) hears every synthetic command once and no phrase without the wake
word; and the document the hub sends is one MINI-SERV's own code accepts. What
it does not: a real voice, a real room, the relay, the hub's CPU.
Implements: TC-1801, TC-1802.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "stack"
for sub in ("voice-listener", "camera-presence", "camera-relay", "camera-motion", "door", "monitoring"):
    sys.path.insert(0, str(STACK / sub))
import voice_engines as ve  # noqa: E402
import voice_listener as vl  # noqa: E402
import voice_post as vp  # noqa: E402

MODELS = os.environ.get("VOICE_LISTENER_MODELS", "")
CLIPS = os.environ.get("VOICE_LISTENER_CLIPS", "")
SCENESERVICE = os.environ.get("VOICE_LISTENER_SCENESERVICE", "")

needs_engines = pytest.mark.skipif(
    not (MODELS and CLIPS and ve.engine_version()),
    reason="set VOICE_LISTENER_MODELS and VOICE_LISTENER_CLIPS and run where the pinned sherpa_onnx imports")
needs_service = pytest.mark.skipif(not (SCENESERVICE and os.path.isfile(SCENESERVICE)),
                                   reason="set VOICE_LISTENER_SCENESERVICE to EntertainmentPC's built SceneService.exe")


@pytest.fixture(autouse=True)
def no_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HOMEHUB_MONITOR_OVERRIDE_FILE", str(tmp_path / "no-override"))


def settings(**over):
    env = {"CAMERA_LIVINGROOM_LISTENER_MODEL_DIR": MODELS}
    env.update({"CAMERA_LIVINGROOM_LISTENER_" + k: v for k, v in over.items()})
    return vl.ListenerSettings(env, "livingroom")


def clip_set(folder):
    out = []
    for wav in sorted(Path(CLIPS, folder).glob("*.wav")):
        info = json.loads(wav.with_suffix(".json").read_text(encoding="utf-8-sig"))
        out.append((wav, info))
    return out


def plain(text):
    return " ".join(re.sub(r"[^a-z0-9 ]", "", text.lower()).split())


@pytest.fixture(scope="module")
def engines(tmp_path_factory):
    return vl.build_engines(settings(), str(tmp_path_factory.mktemp("keywords")))


# ── TC-1801 the real engines on synthetic speech ─────────────────────────────


@needs_engines
def test_tc1801_the_installed_engine_and_files_are_the_pinned_ones():
    assert ve.engine_version() == ve.ENGINE_VERSION
    manifest = ve.read_manifest()
    for model in manifest.values():
        ve.model_files(MODELS, manifest, model["id"], [f["use"] for f in model["files"]
                                                        if not f["use"].startswith(("self-test", "licence", "stock"))])


@needs_engines
def test_tc1801_hey_bruno_is_spelt_in_the_model_s_own_tokens(engines):
    wake, _stt = engines
    assert wake.keyword_text == "▁HE Y ▁B RU N O @HEY_BRUNO"


@needs_engines
def test_tc1801_every_synthetic_command_is_heard_once_and_transcribed(engines):
    wake, stt = engines
    s = settings()
    exact, rows = 0, clip_set("wake")
    assert len(rows) >= 20
    for wav, info in rows:
        got = vl.run_clip(s, wake, stt, vl.read_wav(str(wav)), lead_ms=2000, keep_text=True)
        assert got["wake"] and got["posted"] == 1 and got["wake_checked"] is True, wav.name
        assert got["holds_no_audio"] is True or got["ended_ms"], wav.name
        exact += 1 if plain(got["text"]) == plain(info["request"]) else 0
    assert exact >= len(rows) - 4, "only %d of %d transcripts were the words spoken" % (exact, len(rows))


@needs_engines
def test_tc1801_nothing_without_the_wake_phrase_is_heard_as_a_request(engines):
    wake, stt = engines
    s = settings()
    rows = clip_set("nowake")
    assert len(rows) >= 20
    for wav, _info in rows:
        got = vl.run_clip(s, wake, stt, vl.read_wav(str(wav)), lead_ms=2000)
        assert got["wake"] is False and got["posted"] == 0 and got["requests"] == 0, wav.name


@needs_engines
def test_tc1801_with_pre_roll_the_transcript_starts_with_the_wake_phrase(engines):
    wake, stt = engines
    s = settings(PRE_ROLL_MS="1500")
    rows = clip_set("wake")[:8]
    starts = 0
    for wav, _info in rows:
        got = vl.run_clip(s, wake, stt, vl.read_wav(str(wav)), lead_ms=2000, keep_text=True)
        assert got["wake"] and got["wake_checked"] is False, wav.name
        starts += 1 if plain(got["text"]).startswith(("hey bruno", "hey bruna", "hey bruneau")) else 0
    assert starts >= len(rows) - 2


@needs_engines
def test_tc1801_the_gate_changes_nothing_about_what_is_heard_only_how_long_the_engine_runs(engines):
    wake, stt = engines
    rows = clip_set("wake")[:6]
    for wav, _info in rows:
        audio = vl.read_wav(str(wav))
        gated = vl.run_clip(settings(), wake, stt, audio, lead_ms=4000)
        open_ = vl.run_clip(settings(GATE="false"), wake, stt, audio, lead_ms=4000)
        assert gated["wake"] and open_["wake"], wav.name
        assert gated["gate_open_pct"] < 70.0 and open_["gate_open_pct"] == 100.0


@needs_engines
def test_tc1801_measure_tells_the_standing_cost_from_the_bursts(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("CAMERA_LIVINGROOM_LISTENER_MODEL_DIR=%s\n" % MODELS, encoding="utf-8")
    wav = str(clip_set("wake")[0][0])
    assert vl.measure(str(env_file), "livingroom", wav, 30, False, str(tmp_path / "work")) == 0
    got = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert got["audio_sec"] >= 30 and got["wake_fires"] >= 3 and got["requests"] >= 3 and got["stt_calls"] >= 3
    assert 0 < got["stt_cpu_sec"] < got["cpu_sec"] and got["standing_cpu_sec_per_audio_min"] > 0
    assert got["pct_one_core"] is None and got["engine"] == ve.ENGINE_VERSION      # not paced: no percentage claimed


# ── TC-1802 the document against MINI-SERV's own host ────────────────────────


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def scene_service(tmp_path):
    token = "contract-test-" + "0123456789abcdef" * 2
    token_file = tmp_path / "token.txt"
    token_file.write_text(token + "\n", encoding="ascii")
    port = free_port()
    proc = subprocess.Popen([SCENESERVICE, "--prefix", "http://127.0.0.1:%d/" % port, "--token-file", str(token_file),
                             "--allow", "127.0.0.1"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL)
    client = vp.ActionClient("127.0.0.1", port, token, deadline_sec=4.0)
    try:
        for _ in range(100):
            if client.get_state()[0]:
                break
            assert proc.poll() is None, "SceneService ended at start (exit %s)" % proc.returncode
            time.sleep(0.1)
        yield client, port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


@needs_service
def test_tc1802_the_hub_s_document_is_one_the_interface_s_own_host_accepts(scene_service):
    client, port = scene_service
    reachable, state, voice = client.get_state()
    assert reachable and state in ("up", "ready", "low_power") and voice in ("absent", "armed", "muted", "off")
    first = client.post_utterance("show games", True, request_id="hub-contract-0001")
    assert first.status == 200 and first.outcome in vp.OUTCOMES and first.attempts == 1 and not first.replayed
    again = client.post_utterance("show games", True, request_id="hub-contract-0001")
    assert again.outcome == first.outcome and again.replayed is True            # the same id runs nothing twice
    other = client.post_utterance("show movies", True, request_id="hub-contract-0001")
    assert other.outcome == "rejected-409"                                      # an id is never reused for other words
    assert client.post_utterance("hey bruno show games", False).outcome in vp.OUTCOMES
    wrong = vp.ActionClient("127.0.0.1", port, "w" * 40, deadline_sec=2.0)
    assert wrong.post_utterance("show games", True).outcome == "rejected-401" and wrong.get_state()[0] is False
