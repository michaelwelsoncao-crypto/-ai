"""Shared turn bookkeeping for device and browser connections."""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

from .config import Config
from .realtime import RealtimeSession

log = logging.getLogger("bridge")

_SENTENCE_END = re.compile(r"[.!?。！？]\s*")


class SentenceSplitter:
    """Turns streaming text deltas into whole sentences for subtitles."""

    def __init__(self) -> None:
        self.buf = ""

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out = []
        while True:
            m = _SENTENCE_END.search(self.buf)
            if not m:
                break
            out.append(self.buf[: m.end()].strip())
            self.buf = self.buf[m.end() :]
        return [s for s in out if s]

    def flush(self) -> Optional[str]:
        rest, self.buf = self.buf.strip(), ""
        return rest or None


class Bridge:
    """Base for one client connection talking to one realtime model session."""

    def __init__(self, cfg: Config, rt: RealtimeSession, name: str):
        self.cfg = cfg
        self.rt = rt
        self.name = name
        self.response_active = False
        # Audio of a response the child interrupted must never be played.
        self.muted = False
        self._turn_end_at: Optional[float] = None
        self._first_audio_logged = False
        self.last_latency_ms: Optional[float] = None

    def mark_turn_end(self) -> None:
        """Child finished speaking (VAD stop or button release)."""
        self._turn_end_at = time.monotonic()
        self._first_audio_logged = False

    def note_audio(self) -> None:
        if self._turn_end_at is not None and not self._first_audio_logged:
            self._first_audio_logged = True
            self.last_latency_ms = (time.monotonic() - self._turn_end_at) * 1000
            log.info("[%s] 首包延迟（孩子说完→开始出声）: %.0f ms", self.name, self.last_latency_ms)

    def note_usage(self, ev: dict) -> None:
        if ev.get("usage"):
            log.info("[%s] usage: %s", self.name, ev["usage"])

    async def interrupt(self) -> None:
        """Child talked over the tutor: stop the current answer immediately."""
        self.muted = True
        if self.response_active:
            await self.rt.cancel()
