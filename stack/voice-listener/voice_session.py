#!/usr/bin/env python3
"""voice_session.py - one spoken request on the hub, from the wake word to the post.

PURE CORE: no I/O and no engine of its own. The wake word, speech-to-text and
the post to MINI-SERV are handed in, so the whole path is tested with stand-ins
and a clock that is passed in.

THE BEHAVIOUR REFERENCE is EntertainmentPC's Windows session
(src/ControllerOverlay.Core/VoiceSession.cs, EPN-029: behaviour shared between
the two listeners must match). Same states, same timing rules and defaults:

    idle --wake word--> wake_heard --speech--> listening
         --quiet, or too long--> the request is transcribed and posted --> idle

What differs on the hub: two gates stand in front of the wake word
(voice_gate.py), and the hub matches nothing itself. The transcript goes to
MINI-SERV's POST /v1/utterance, whose own matcher decides (one authority for
the fixed actions).

ROOM AUDIO IN THIS FILE lives in exactly two buffers: the session's pre-roll
(only when PRE_ROLL_MS is above 0, and only while the activity gate is open)
and the request being spoken (at most PRE_ROLL_MS + MAX_UTTERANCE_MS). Both are
zeroed on every return to idle and on disarm. A TRANSCRIPT is text about the
household: it is handed to the poster and to nothing else, and no event,
counter or message here ever carries it.

Python 3.8-compatible (the dev PC's test runner imports this module).
Implements: SR-165, SR-166, SR-167, LLR-1536, LLR-1537.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

import voice_gate as vg

IDLE, WAKE_HEARD, LISTENING = "idle", "wake_heard", "listening"

# Why a request ended (Core's VoiceEndReason, in snake case).
END_SILENCE = "silence"
END_MAX_LENGTH = "max_length"
END_NO_SPEECH = "no_speech"
END_AUDIO_STALLED = "audio_stalled"
END_NOTHING_HEARD = "nothing_heard"
END_ENGINE_ERROR = "engine_error"

# What the household is shown (Core's VoiceIndicator, plus the hub's gate).
SHOW_ARMED = "armed"          # the microphone is read; the wake word is not being run (quiet)
SHOW_HEARING = "hearing"      # sound stands out: the wake-word engine is running
SHOW_LISTENING = "listening"  # the wake word was heard: a request is being taken in
SHOW_WORKING = "working"      # the request is being turned into text and sent


class SessionOptions:
    """Core's VoiceSessionOptions, by the same names and defaults."""

    def __init__(self, pre_roll_ms: int = 0, end_silence_ms: int = 700, no_speech_timeout_ms: int = 3000,
                 max_utterance_ms: int = 8000, stall_timeout_ms: int = 2000, hold_after_post_ms: int = 3000):
        if not 0 <= pre_roll_ms <= 3000:
            raise ValueError("pre_roll_ms must be 0..3000")
        if not 1000 <= max_utterance_ms <= 30000:
            raise ValueError("max_utterance_ms must be 1000..30000")
        self.pre_roll_ms = pre_roll_ms
        self.end_silence_ms = max(100, end_silence_ms)
        self.no_speech_timeout_ms = max(200, no_speech_timeout_ms)
        self.max_utterance_ms = max_utterance_ms
        self.stall_timeout_ms = stall_timeout_ms
        self.hold_after_post_ms = max(0, hold_after_post_ms)


class RequestEnd:
    """A request that ended: why, and its audio (None when nothing was said).
    The audio is the receiver's to wipe."""

    __slots__ = ("reason", "audio")

    def __init__(self, reason: str, audio: Optional[bytearray]):
        self.reason, self.audio = reason, audio


class ListenSession:
    """The state machine around one spoken request.

    Contract:
      Inputs:  idle_frame(frame) while waiting for the wake word (kept only as
               pre-roll); wake(now_ms) when the wake word fired;
               capture(frame, is_speech, now_ms) for every frame after it;
               stalled(now_ms) when no frame came.
      Outputs: capture and stalled return a RequestEnd when the request ended.
      Config:  SessionOptions.
    Implements: LLR-1536.
    """

    def __init__(self, options: Optional[SessionOptions] = None):
        self.o = options or SessionOptions()
        self.state = IDLE
        self._pre_roll = vg.WipedBuffer(vg.bytes_for(self.o.pre_roll_ms))
        self._request = bytearray()
        self._captured_ms = 0.0
        self._quiet_ms = 0.0
        self._speech_seen = False
        self._last_audio_ms = 0

    @property
    def includes_wake_phrase(self) -> bool:
        """Whether the transcript is expected to start with the wake phrase (only with pre-roll)."""
        return self.o.pre_roll_ms > 0

    @property
    def holds_no_audio(self) -> bool:
        return self._pre_roll.is_cleared and len(self._request) == 0

    @property
    def buffered_bytes(self) -> int:
        return len(self._pre_roll) + len(self._request)

    def idle_frame(self, frame) -> None:
        if self.state == IDLE:
            self._pre_roll.write(frame)

    def wake(self, now_ms: int) -> None:
        held = self._pre_roll.take()
        self._request = bytearray(held)
        vg.wipe_bytes(held)
        self._captured_ms, self._quiet_ms, self._speech_seen = 0.0, 0.0, False
        self._last_audio_ms = now_ms
        self.state = WAKE_HEARD

    def capture(self, frame, is_speech: bool, now_ms: int) -> Optional[RequestEnd]:
        if self.state == IDLE:
            return None
        self._request.extend(frame)
        ms = vg.ms_for(len(frame))
        self._captured_ms += ms
        self._last_audio_ms = now_ms
        if is_speech:
            self._quiet_ms = 0.0
            self._speech_seen = True
            self.state = LISTENING
        else:
            self._quiet_ms += ms
        if self.state == WAKE_HEARD:
            if self._captured_ms >= min(self.o.max_utterance_ms, self.o.no_speech_timeout_ms):
                return self._end(END_NO_SPEECH, keep=False)
        elif self._quiet_ms >= self.o.end_silence_ms:
            return self._end(END_SILENCE, keep=True)
        elif self._captured_ms >= self.o.max_utterance_ms:
            return self._end(END_MAX_LENGTH, keep=True)
        return None

    def stalled(self, now_ms: int) -> Optional[RequestEnd]:
        """The audio stopped arriving while a request was open."""
        if self.state == IDLE or now_ms - self._last_audio_ms < self.o.stall_timeout_ms:
            return None
        return self._end(END_AUDIO_STALLED, keep=self._speech_seen)

    def _end(self, reason: str, keep: bool) -> RequestEnd:
        audio = None
        if keep:
            audio, self._request = self._request, bytearray()
        self.release()
        return RequestEnd(reason, audio)

    def release(self) -> None:
        """Let go of everything: both buffers zeroed, back to idle."""
        self._pre_roll.wipe()
        vg.wipe_bytes(self._request)
        del self._request[:]
        self._captured_ms, self._quiet_ms, self._speech_seen = 0.0, 0.0, False
        self.state = IDLE


class Pipeline:
    """Frames in, at most one posted transcript out: gate, limiter, wake word,
    request, speech-to-text, post.

    Contract:
      Inputs:  wake: .process(frame) -> bool, .reset(); stt: .transcribe(audio)
               -> str; post: callable(text, wake_checked) -> outcome code (a
               short word, never the text). feed(frame, now_ms) per frame;
               tick(now_ms) when none came; disarm() when the listener may no
               longer hear.
      Outputs: feed/tick return state events: dicts with `show`, and `reason`
               or `outcome` codes. Never a transcript, a level or a sample.
               `flush_source` is set when the audio that queued up while the
               request was worked on must be thrown away unheard.
    Implements: LLR-1537.
    """

    def __init__(self, gate: vg.ActivityGate, limiter: vg.DutyLimiter, session: ListenSession, wake, stt,
                 post: Callable[[str, bool], str]):
        self.gate, self.limiter, self.session = gate, limiter, session
        self.wake, self.stt, self.post = wake, stt, post
        self.show = SHOW_ARMED
        self.flush_source = False
        self.hold_until_ms = 0
        self._speech_level = 0.0
        self.engine_errors_in_a_row = 0
        self.counts = {"frames": 0, "gate_opens": 0, "wake_fires": 0, "requests": 0, "posted": 0,
                       "no_speech": 0, "nothing_heard": 0, "engine_errors": 0, "duty_stops": 0}   # type: Dict[str, int]
        self.outcomes = {}   # type: Dict[str, int]

    # -- the household-visible state --------------------------------------

    def _set(self, show: str, events: List[Dict[str, object]], **detail) -> None:
        if show != self.show or detail:
            self.show = show
            events.append(dict({"show": show}, **detail))

    @property
    def holds_no_audio(self) -> bool:
        return self.gate.holds_no_audio and self.session.holds_no_audio

    @property
    def request_open(self) -> bool:
        return self.session.state != IDLE

    # -- frames -------------------------------------------------------------

    def feed(self, frame, now_ms: int) -> List[Dict[str, object]]:
        events = []   # type: List[Dict[str, object]]
        self.counts["frames"] += 1
        if now_ms < self.hold_until_ms:
            return events          # Bruno's own reply is playing: not heard, not kept
        try:
            self._feed(frame, now_ms, events)
        except Exception:  # noqa: BLE001 - an engine is somebody else's code: it ends the request, not the service
            self.counts["engine_errors"] += 1
            self.engine_errors_in_a_row += 1
            self._to_idle(events, reason=END_ENGINE_ERROR)
        return events

    def _feed(self, frame, now_ms: int, events: List[Dict[str, object]]) -> None:
        level = vg.frame_rms(frame)
        if self.session.state != IDLE:
            self._capture(frame, level, now_ms, events)
            return
        threshold = self.gate.threshold()
        if not self.gate.is_open and not self.limiter.ready(now_ms):
            self.gate.floor.learn(level, vg.ms_for(len(frame)))
            return                 # the duty ceiling is spent: deaf until it refills
        passed, change = self.gate.feed(frame, level, now_ms)
        if change == "opened":
            self.counts["gate_opens"] += 1
            self._set(SHOW_HEARING, events)
        try:
            for piece in passed:
                if self.flush_source:
                    break              # a request just ended inside this batch: the rest is not heard
                if self.session.state != IDLE:
                    self._capture(piece, vg.frame_rms(piece), now_ms, events)
                elif not self.limiter.allow(now_ms, vg.ms_for(len(piece))):
                    self.counts["duty_stops"] += 1
                    self._to_idle(events, reason="duty")
                    return
                else:
                    self.session.idle_frame(piece)
                    fired = self.wake.process(piece)
                    self.engine_errors_in_a_row = 0    # the engine answered: the run of faults is over
                    if fired:
                        self.counts["wake_fires"] += 1
                        self._speech_level = threshold     # frozen for the request, as Core does
                        self.session.wake(now_ms)
                        self.gate.release()
                        self._set(SHOW_LISTENING, events)
        finally:
            self._wipe_pieces(passed, frame)
        if change == "closed" and self.session.state == IDLE and not self.flush_source:
            self.wake.reset()
            self._set(SHOW_ARMED, events)

    @staticmethod
    def _wipe_pieces(pieces, frame) -> None:
        for piece in pieces:
            if piece is not frame and isinstance(piece, bytearray):
                vg.wipe_bytes(piece)

    def _capture(self, frame, level: float, now_ms: int, events: List[Dict[str, object]]) -> None:
        end = self.session.capture(frame, level >= self._speech_level, now_ms)
        if end is not None:
            self._finish(end, now_ms, events)

    def tick(self, now_ms: int) -> List[Dict[str, object]]:
        """No frame came: end a request whose audio stopped."""
        events = []   # type: List[Dict[str, object]]
        end = self.session.stalled(now_ms)
        if end is not None:
            try:
                self._finish(end, now_ms, events)
            except Exception:  # noqa: BLE001
                self.counts["engine_errors"] += 1
                self.engine_errors_in_a_row += 1
                self._to_idle(events, reason=END_ENGINE_ERROR)
        return events

    # -- a request that ended ----------------------------------------------

    def _finish(self, end: RequestEnd, now_ms: int, events: List[Dict[str, object]]) -> None:
        self.flush_source = True          # nothing said while we are busy is kept
        if end.audio is None:
            self.counts["no_speech"] += 1
            self._to_idle(events, reason=end.reason)
            return
        self.counts["requests"] += 1
        self._set(SHOW_WORKING, events, reason=end.reason)
        try:
            text = (self.stt.transcribe(end.audio) or "").strip()
            self.engine_errors_in_a_row = 0
        finally:
            vg.wipe_bytes(end.audio)
            del end.audio[:]
        if not text:
            self.counts["nothing_heard"] += 1
            self._to_idle(events, reason=END_NOTHING_HEARD)
            return
        outcome = self.post(text, not self.session.includes_wake_phrase)
        text = ""                                      # noqa: F841 - the only reference is dropped here
        self.counts["posted"] += 1
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
        self.hold_until_ms = now_ms + self.session.o.hold_after_post_ms
        self._to_idle(events, outcome=outcome)

    def _to_idle(self, events: List[Dict[str, object]], **detail) -> None:
        self.session.release()
        self.gate.release()
        try:
            self.wake.reset()
        except Exception:  # noqa: BLE001 - a reset that throws is counted by the caller's next frame
            pass
        self._set(SHOW_ARMED, events, **detail)

    def disarm(self) -> None:
        """The listener may no longer hear: every buffer zeroed, the engine's own
        history thrown away. No event: the caller reports the disarm itself."""
        self.session.release()
        self.gate.release()
        self.hold_until_ms = 0
        self.flush_source = False
        try:
            self.wake.reset()
        except Exception:  # noqa: BLE001
            pass
        self.show = SHOW_ARMED
