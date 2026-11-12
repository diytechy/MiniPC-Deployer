#!/usr/bin/env python3
"""voice_engines.py - the pinned models, and the two sherpa-onnx engines behind
the hub voice listener's wake word and speech-to-text.

THE ENGINE is sherpa-onnx 2.0.1 (requirements.lock): the one the hub's cost
test measured and the one EntertainmentPC's Windows listener uses, so the two
listeners can be compared and swapped.

THE MODELS are not in this public repository. models.manifest.json pins each
file by size and SHA-256: the same files as EntertainmentPC's
src/Voice.Engines/voice-models.json. A file is checked every time an engine
loads it, and other bytes are refused.

THE WAKE PHRASE is configured as the keyword-spotting model's own tokens, with
"^" standing for the tokens' word-start mark (U+2581), so the stack .env stays
ASCII: "^HE Y ^B RU N O" is "hey bruno" (the encoding EntertainmentPC's
SentencePiece code derives from the model's bpe.model and tests against it).
Every token must be in the model's tokens.txt.

TWO QUIRKS OF THIS MODEL, measured by EntertainmentPC lane J on 2026-11-11 and
reproduced here: after quiet, a phrase that starts inside a window of about
300 ms recurring every 1.6 s from the stream's start is missed; and exact
digital silence makes it miss more. The second is met the same way, by a faint
fixed hiss under the input (`dither_dbfs`). The first is met here by the
activity gate, which replaces the stream at each sound so the phrase always
starts at the same place in it (dev PC, synthetic voices: one stream heard 320
of 320 phrases said after quiet, against 262 of 320 with no gate). Lane J's own
remedy stays available: `streams` 2 feeds a second stream started `stagger_ms`
of synthetic quiet ahead, which also covers a phrase said straight after other
speech (320 of 320 against one stream's 292) at about twice the engine's cost.

ROOM AUDIO HERE: each frame is converted into one scratch array, handed to the
spotter and zeroed. The spotter keeps a short history of features to work at
all (its encoder's left context, 1.28 s); reset() throws its streams away. The
native library's debug output stays off. Nothing is written except the keyword
line (configuration, not sound) into the listener's runtime directory.

numpy and sherpa_onnx are imported only when an engine is built, so the
manifest and token rules are tested without either.
Python 3.8-compatible. Implements: SR-168, LLR-1541..LLR-1543.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Dict, List, Sequence

import voice_gate as vg

HERE = os.path.dirname(os.path.realpath(__file__))
MANIFEST_FILE = os.path.join(HERE, "models.manifest.json")
MANIFEST_SCHEMA = "voice-models/1"
WAKE_MODEL = "kws-gigaspeech-3.3m"
STT_MODEL = "moonshine-tiny-en-int8"
WORD_START = "▁"       # the tokens' word-start mark
ASCII_WORD_START = "^"      # stands for it in configuration
ENGINE_VERSION = "2.0.1"


class Invalid(ValueError):
    """A model, the manifest or the wake phrase cannot be used; names the file or rule only."""


# ── the manifest ─────────────────────────────────────────────────────────────


def read_manifest(path: str = MANIFEST_FILE) -> Dict[str, Dict[str, object]]:
    """{model id: entry} from models.manifest.json; Invalid on another schema."""
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    if doc.get("schema") != MANIFEST_SCHEMA:
        raise Invalid("models.manifest.json is not schema %s" % MANIFEST_SCHEMA)
    return {str(m["id"]): m for m in doc["models"]}


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def model_files(model_dir: str, manifest: Dict[str, Dict[str, object]], model_id: str,
                uses: Sequence[str]) -> Dict[str, str]:
    """{use: path} for the named files of one model, each the pinned bytes.

    Contract:
      Inputs:  model_dir: the folder holding one folder per model (the
               manifest's `folder`); uses: e.g. ("encoder", "decoder").
      Raises:  Invalid when the model or a use is not in the manifest, a file
               is missing, or its size or SHA-256 differs.
    Implements: LLR-1541.
    """
    entry = manifest.get(model_id)
    if not entry:
        raise Invalid("model %s is not in models.manifest.json" % model_id)
    by_use = {str(f["use"]): f for f in entry["files"]}   # type: ignore[union-attr]
    out = {}   # type: Dict[str, str]
    for use in uses:
        pinned = by_use.get(use)
        if not pinned:
            raise Invalid("model %s has no '%s' file in models.manifest.json" % (model_id, use))
        path = os.path.join(model_dir, str(entry["folder"]), *str(pinned["name"]).split("/"))
        try:
            size = os.path.getsize(path)
        except OSError:
            raise Invalid("model file %s is missing" % pinned["name"])
        if size != pinned["size"] or sha256_file(path) != pinned["sha256"]:
            raise Invalid("model file %s is not the pinned bytes (models.manifest.json)" % pinned["name"])
        out[use] = path
    return out


# ── the wake phrase ──────────────────────────────────────────────────────────

LABEL_RE = re.compile(r"[A-Z][A-Z0-9_]{0,31}")
TOKEN_RE = re.compile(r"\^?[A-Z']{1,16}")


def read_tokens(tokens_path: str) -> List[str]:
    """The symbols of a sherpa-onnx tokens.txt (`symbol id` per line)."""
    out = []
    with open(tokens_path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").rsplit(" ", 1)
            if len(parts) == 2 and parts[0]:
                out.append(parts[0])
    return out


def keyword_line(wake_tokens: str, label: str, known_tokens: Sequence[str]) -> str:
    """The spotter's keyword line for a phrase given as the model's tokens.

    Contract:
      Inputs:  wake_tokens: e.g. "^HE Y ^B RU N O" ("^" is the word-start
               mark); label: what the spotter reports, e.g. HEY_BRUNO;
               known_tokens: read_tokens() of the model.
      Outputs: "<tokens with the real mark> @<label>".
      Raises:  Invalid for a malformed token or label, a token the model does
               not have, or a model whose tokens already use "^".
    Implements: LLR-1542.
    """
    if not LABEL_RE.fullmatch(label or ""):
        raise Invalid("the wake label must match [A-Z][A-Z0-9_]{0,31}")
    pieces = (wake_tokens or "").split()
    if not 2 <= len(pieces) <= 24 or any(not TOKEN_RE.fullmatch(p) for p in pieces):
        raise Invalid("the wake tokens must be 2 to 24 of the model's tokens, '^' marking a word start")
    if not pieces[0].startswith(ASCII_WORD_START):
        raise Invalid("the wake tokens must begin with a word start ('^')")
    known = set(known_tokens)
    if any(ASCII_WORD_START in t for t in known):
        raise Invalid("the model's tokens already use '^'")
    real = [p.replace(ASCII_WORD_START, WORD_START) for p in pieces]
    for token in real:
        if token not in known:
            raise Invalid("the wake tokens name a token the model does not have")
    return " ".join(real) + " @" + label


# ── the engines ──────────────────────────────────────────────────────────────


class SherpaWakeWord:
    """sherpa-onnx keyword spotting behind `process(frame) -> bool` and `reset()`.

    Contract:
      Inputs:  files: model_files(..., WAKE_MODEL, ("encoder", "decoder",
               "joiner", "tokens")); keywords_path: a file this writes the
               keyword line to (configuration; the runtime directory);
               threshold, score, trailing_blanks, max_active_paths: the
               spotter's own settings, at sherpa-onnx's defaults; threads;
               streams (1..4), stagger_ms; dither_dbfs (-120: none).
      Outputs: process: True when the phrase completed in this frame (one
               phrase, one fire). fires, samples: counters.
    Implements: LLR-1542.
    """

    def __init__(self, files: Dict[str, str], wake_tokens: str, label: str, keywords_path: str,
                 threshold: float = 0.25, score: float = 1.0, trailing_blanks: int = 1,
                 max_active_paths: int = 4, threads: int = 1, streams: int = 1, stagger_ms: int = 800,
                 dither_dbfs: float = -70.0):
        if not 1 <= streams <= 4:
            raise Invalid("streams must be 1..4")
        if not 0 <= stagger_ms <= 5000:
            raise Invalid("stagger_ms must be 0..5000")
        import numpy as np
        import sherpa_onnx
        self._np = np
        self.keyword_text = keyword_line(wake_tokens, label, read_tokens(files["tokens"]))
        with open(keywords_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(self.keyword_text + "\n")
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=files["tokens"], encoder=files["encoder"], decoder=files["decoder"], joiner=files["joiner"],
            keywords_file=keywords_path, num_threads=threads, max_active_paths=max_active_paths,
            keywords_score=score, keywords_threshold=threshold, num_trailing_blanks=trailing_blanks,
            provider="cpu")
        self.stream_count, self.stagger_ms = streams, stagger_ms
        # One second of fixed hiss, tiled: the same input every run.
        peak = 0.0 if dither_dbfs <= -120 else float(10 ** (dither_dbfs / 20.0) * 3 ** 0.5)
        self._hiss = (np.random.RandomState(0x9E3779B9 & 0x7FFFFFFF).uniform(-peak, peak, vg.SAMPLE_RATE)
                      .astype(np.float32)) if peak else None
        self._hiss_at = 0
        self._scratch = np.zeros(vg.FRAME_SAMPLES, dtype=np.float32)
        self._streams = []   # type: List[object]
        self.fires = 0
        self.samples = 0
        self._new_streams()

    def _feed(self, stream, frame) -> bool:
        np = self._np
        count = len(frame) // vg.SAMPLE_BYTES
        if len(self._scratch) != count:
            self._scratch.fill(0)
            self._scratch = np.zeros(count, dtype=np.float32)
        scratch = self._scratch
        scratch[:] = np.frombuffer(frame, dtype="<i2", count=count)
        scratch /= vg.FULL_SCALE
        if self._hiss is not None:
            at = self._hiss_at % len(self._hiss)
            if at + count <= len(self._hiss):
                scratch += self._hiss[at:at + count]
            else:
                scratch += np.resize(np.roll(self._hiss, -at), count)
        try:
            stream.accept_waveform(vg.SAMPLE_RATE, scratch)
        finally:
            scratch.fill(0)
        fired = False
        while self._spotter.is_ready(stream):
            self._spotter.decode_stream(stream)
            if self._spotter.get_result(stream):
                fired = True
                self._spotter.reset_stream(stream)
        return fired

    def _new_streams(self) -> None:
        quiet = bytes(vg.FRAME_BYTES)
        self._streams = []
        for index in range(self.stream_count):
            stream = self._spotter.create_stream()
            for _ in range(int(index * self.stagger_ms / vg.FRAME_MS)):
                self._feed(stream, quiet)     # synthetic quiet (the hiss alone): no room audio primes a stream
            self._streams.append(stream)

    def process(self, frame) -> bool:
        if not len(frame):
            return False
        fired = False
        for stream in self._streams:
            fired = self._feed(stream, frame) or fired
        self._hiss_at += len(frame) // vg.SAMPLE_BYTES
        self.samples += len(frame) // vg.SAMPLE_BYTES
        if fired:
            self.fires += 1
            for stream in self._streams:
                self._spotter.reset_stream(stream)
        return fired

    def reset(self) -> None:
        """Forget everything heard: the streams, with their features and search state, are replaced."""
        self._new_streams()


class SherpaSpeechToText:
    """Moonshine tiny behind `transcribe(audio) -> str`. Implements: LLR-1543."""

    def __init__(self, files: Dict[str, str], threads: int = 1):
        import numpy as np
        import sherpa_onnx
        self._np = np
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_moonshine_v2(
            encoder=files["encoder"], decoder=files["merged-decoder"], tokens=files["tokens"],
            num_threads=threads, debug=False)

    def transcribe(self, audio) -> str:
        """The text of one request (16 kHz mono signed 16-bit bytes). The float
        copy made for the engine is zeroed before this returns."""
        np = self._np
        samples = np.frombuffer(audio, dtype="<i2").astype(np.float32)
        samples /= vg.FULL_SCALE
        try:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(vg.SAMPLE_RATE, samples)
            self._recognizer.decode_stream(stream)
            return str(stream.result.text or "").strip()
        finally:
            samples.fill(0)


def engine_version() -> str:
    """The installed sherpa-onnx version, or "" when it is not importable."""
    try:
        import sherpa_onnx
        return str(getattr(sherpa_onnx, "__version__", "?"))
    except ImportError:
        return ""
