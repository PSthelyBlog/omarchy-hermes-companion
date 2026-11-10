"""Voice I/O: on-demand capture (right-click / hotkey) -> VAD -> local whisper -> callback; streaming TTS out.

Reuses Hermes' own pipeline (hermes_cli.voice, tools.tts_tool) so STT/TTS
provider, voice and model config come from ~/.hermes/config.yaml.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

HERMES_ROOT = Path(os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes/hermes-agent"))
sys.path.insert(0, str(HERMES_ROOT))

log = logging.getLogger("companion.voice")


class Voice:
    def __init__(
        self,
        on_request: Callable[[str], None],
        on_status: Callable[[str], None],
        silence_duration: float = 1.6,
        max_utterance_seconds: float = 30.0,
    ):
        self.on_request = on_request
        self.on_status = on_status
        self.silence_duration = silence_duration
        self.max_utterance_seconds = max_utterance_seconds
        self._speaking = threading.Event()
        self._stop_speech = threading.Event()
        self._capturing = threading.Event()
        # Warm the STT model so the first request isn't slow.
        threading.Thread(target=self._warm_stt, daemon=True).start()

    @staticmethod
    def _warm_stt():
        try:
            import tempfile
            import wave

            from tools.transcription_tools import transcribe_audio

            p = tempfile.mktemp(suffix=".wav")
            with wave.open(p, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\x00\x00" * 16000)
            transcribe_audio(p)
            os.unlink(p)
            log.info("STT warmed")
        except Exception:
            log.debug("STT warm-up skipped", exc_info=True)

    @property
    def capturing(self) -> bool:
        return self._capturing.is_set()

    def hush(self):
        """Interrupt any current speech."""
        self._stop_speech.set()

    # ---------------------------------------------------------------- listen
    def listen_for(self, timeout: float, parse, cancel: threading.Event | None = None):
        """Capture one short utterance and return parse(transcript) (None on silence/timeout).
        Used for yes/no approvals; does not go to the agent. Setting `cancel` (the approval was
        settled some other way) stops the capture and discards what it heard, so the microphone
        is free for the next request."""
        if self._capturing.is_set():
            return None
        from hermes_cli import voice as hv

        self._capturing.set()
        got: list[str] = []
        done = threading.Event()
        try:
            ok = hv.start_continuous(on_transcript=lambda t: (got.append(t), done.set()), on_status=lambda s: None,
                                     on_silent_limit=done.set, silence_duration=1.0, auto_restart=False,
                                     max_recording_seconds=min(8.0, timeout))
            if not ok:
                return None
            deadline = time.monotonic() + timeout
            while not done.wait(0.1):
                if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
                    break
            try:
                hv.stop_continuous()
            except Exception:
                pass
            self._release_recorder(hv)
        finally:
            self._capturing.clear()
        if cancel is not None and cancel.is_set():
            return None
        return parse(" ".join(got))

    def listen(self) -> bool:
        """Start one voice-request capture (returns False if one is already running)."""
        if self._capturing.is_set():
            return False
        if self._speaking.is_set():
            self.hush()
            time.sleep(0.3)
        threading.Thread(target=self._capture_turn, daemon=True, name="companion-turn").start()
        return True

    def _capture_turn(self):
        from hermes_cli import voice as hv

        self._capturing.set()
        self.on_status("listening-request")
        hv._play_beep(880, 1)
        done = threading.Event()
        transcript: list[str] = []

        def on_transcript(text: str):
            transcript.append(text)
            done.set()

        def on_status(s: str):
            if s == "transcribing":
                self.on_status("thinking")

        try:
            ok = hv.start_continuous(
                on_transcript=on_transcript,
                on_status=on_status,
                on_silent_limit=done.set,
                silence_duration=self.silence_duration,
                auto_restart=False,
                max_recording_seconds=self.max_utterance_seconds,
            )
            if not ok:
                log.warning("start_continuous refused (busy)")
                done.set()
            done.wait(timeout=self.max_utterance_seconds + 15)
            try:
                hv.stop_continuous()
            except Exception:
                pass
            self._release_recorder(hv)
        finally:
            self._capturing.clear()
        text = " ".join(t.strip() for t in transcript if t and t.strip()).strip()
        if text:
            log.info("voice request: %r", text)
            try:
                self.on_request(text)
            except Exception:
                log.exception("on_request")
        else:
            hv._play_beep(440, 1)
            self.on_status("watching")

    @staticmethod
    def _release_recorder(hv):
        """Hermes keeps the recorder's InputStream open for reuse; a daemon should free the mic."""
        deadline = time.time() + 10
        while getattr(hv, "_continuous_stopping", False) and time.time() < deadline:
            time.sleep(0.05)
        rec = getattr(hv, "_continuous_recorder", None)
        if rec is None:
            return
        try:
            rec.shutdown()
        except Exception:
            log.exception("recorder shutdown")
        hv._continuous_recorder = None

    # ---------------------------------------------------------------- speak
    def speak(self, text: str):
        """Blocking; streams TTS sentence by sentence."""
        if not text or not text.strip():
            return
        from hermes_cli import voice as hv

        self._stop_speech.clear()
        self._speaking.set()
        try:
            self.on_status("speaking")
            hv.speak_text(text, stop_event=self._stop_speech)
        except Exception:
            log.exception("speak")
        finally:
            self._speaking.clear()
            self.on_status("watching")
