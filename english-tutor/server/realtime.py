"""Speech-to-speech realtime model clients.

Every provider exposes the same small interface and yields *normalized* events,
so the device / browser gateways never depend on one vendor's wire format:

    {"type": "speech_started"}                 child started talking (barge-in point)
    {"type": "speech_stopped"}
    {"type": "user_transcript", "text": str}   what the child said
    {"type": "response_started"}
    {"type": "audio", "pcm": bytes}            24 kHz / 16-bit / mono PCM
    {"type": "text_delta", "text": str}        subtitle of what the tutor is saying
    {"type": "text_done", "text": str}
    {"type": "response_done", "status": str, "usage": dict | None}
    {"type": "error", "message": str}

Input audio is always 16 kHz / 16-bit / mono PCM.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import struct
from typing import AsyncIterator, Optional

import aiohttp

from .config import Config
from .prompts import build_instructions

log = logging.getLogger("realtime")

INPUT_RATE = 16000
OUTPUT_RATE = 24000


class RealtimeSession:
    async def connect(self) -> None: ...
    async def append_audio(self, pcm16k: bytes) -> None: ...
    async def commit_and_respond(self) -> None: ...
    async def clear_input(self) -> None: ...
    async def say(self, text: str) -> None: ...
    async def cancel(self) -> None: ...
    async def close(self) -> None: ...
    def events(self) -> AsyncIterator[dict]: ...


def create_session(cfg: Config) -> RealtimeSession:
    if cfg.provider == "mock":
        return MockSession(cfg)
    if cfg.provider == "qwen":
        if not cfg.dashscope_api_key:
            raise RuntimeError("DASHSCOPE_API_KEY 未设置（或设置 PROVIDER=mock 离线调试）")
        return QwenOmniSession(cfg)
    raise RuntimeError(f"unknown PROVIDER {cfg.provider!r}")


# --------------------------------------------------------------------------
# 阿里云百炼 Qwen-Omni-Realtime（协议与 OpenAI Realtime 类似）
# --------------------------------------------------------------------------

_AUDIO_DELTA = {"response.audio.delta", "response.output_audio.delta"}
_TEXT_DELTA = {
    "response.audio_transcript.delta",
    "response.output_audio_transcript.delta",
    "response.text.delta",
    "response.output_text.delta",
}
_TEXT_DONE = {
    "response.audio_transcript.done",
    "response.output_audio_transcript.done",
    "response.text.done",
    "response.output_text.done",
}


class QwenOmniSession(RealtimeSession):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._http: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._send_lock = asyncio.Lock()

    def session_payload(self) -> dict:
        cfg = self.cfg
        turn_detection = None
        if cfg.turn_mode == "server_vad":
            turn_detection = {
                "type": "server_vad",
                "threshold": cfg.vad_threshold,
                "prefix_padding_ms": cfg.vad_prefix_ms,
                "silence_duration_ms": cfg.vad_silence_ms,
            }
        session: dict = {
            "modalities": ["text", "audio"],
            "instructions": build_instructions(cfg),
            "turn_detection": turn_detection,
        }
        if cfg.is_qwen38:
            output: dict = {"format": {"type": "pcm", "sample_rate": OUTPUT_RATE}}
            if cfg.voice:
                output["voice"] = cfg.voice
            session["audio"] = {
                "input": {"format": {"type": "pcm", "sample_rate": INPUT_RATE}},
                "output": output,
            }
            session["input_audio_transcription"] = {
                "model": cfg.transcription_model or "qwen3-asr-flash-realtime"
            }
        else:  # qwen3-omni-flash-realtime and earlier
            session["voice"] = cfg.voice or "Cherry"
            session["input_audio_format"] = "pcm16"
            session["output_audio_format"] = "pcm16"
            session["input_audio_transcription"] = {
                "model": cfg.transcription_model or None
            }
        return session

    async def _send(self, event: dict) -> None:
        if self._ws is None or self._ws.closed:
            return
        async with self._send_lock:
            await self._ws.send_str(json.dumps(event, ensure_ascii=False))

    async def connect(self) -> None:
        url = f"{self.cfg.realtime_url}?model={self.cfg.model}"
        headers = {"Authorization": f"Bearer {self.cfg.dashscope_api_key}"}
        self._http = aiohttp.ClientSession()
        self._ws = await self._http.ws_connect(url, headers=headers, heartbeat=20, max_msg_size=0)
        await self._send({"type": "session.update", "session": self.session_payload()})
        log.info("connected to %s", self.cfg.model)

    async def append_audio(self, pcm16k: bytes) -> None:
        await self._send(
            {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm16k).decode()}
        )

    async def _create_response(self) -> None:
        event: dict = {"type": "response.create"}
        if not self.cfg.is_qwen38:
            event["response"] = {"instructions": None}
        await self._send(event)

    async def commit_and_respond(self) -> None:
        await self._send({"type": "input_audio_buffer.commit"})
        await self._create_response()

    async def clear_input(self) -> None:
        await self._send({"type": "input_audio_buffer.clear"})

    async def say(self, text: str) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self._create_response()

    async def cancel(self) -> None:
        await self._send({"type": "response.cancel"})

    async def close(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._http is not None:
            await self._http.close()

    async def events(self) -> AsyncIterator[dict]:
        assert self._ws is not None
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            ev = json.loads(msg.data)
            kind = ev.get("type", "")
            if kind in _AUDIO_DELTA:
                yield {"type": "audio", "pcm": base64.b64decode(ev.get("delta", ""))}
            elif kind in _TEXT_DELTA:
                yield {"type": "text_delta", "text": ev.get("delta", "")}
            elif kind in _TEXT_DONE:
                yield {"type": "text_done", "text": ev.get("transcript", ev.get("text", ""))}
            elif kind == "input_audio_buffer.speech_started":
                yield {"type": "speech_started"}
            elif kind == "input_audio_buffer.speech_stopped":
                yield {"type": "speech_stopped"}
            elif kind == "conversation.item.input_audio_transcription.completed":
                yield {"type": "user_transcript", "text": ev.get("transcript", "")}
            elif kind == "response.created":
                yield {"type": "response_started"}
            elif kind == "response.done":
                resp = ev.get("response") or {}
                yield {
                    "type": "response_done",
                    "status": resp.get("status", ""),
                    "usage": resp.get("usage"),
                }
            elif kind == "error":
                err = ev.get("error") or {}
                yield {"type": "error", "message": err.get("message") or json.dumps(ev)}
            elif kind in ("session.created", "session.updated"):
                log.info("%s", kind)


# --------------------------------------------------------------------------
# 离线 Mock：不花钱、不联网，用来调通设备/网页链路
# --------------------------------------------------------------------------


def _tone(seconds: float, freq: float = 440.0) -> bytes:
    n = int(OUTPUT_RATE * seconds)
    return b"".join(
        struct.pack("<h", int(6000 * math.sin(2 * math.pi * freq * i / OUTPUT_RATE)))
        for i in range(n)
    )


class MockSession(RealtimeSession):
    """Simple energy VAD + canned reply (a short beep) so the full pipeline can be tested."""

    SPEECH_RMS = 500
    REPLY = ["Great job! ", "What is your favorite animal?"]

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._queue: asyncio.Queue = asyncio.Queue()
        self._speaking = False
        self._heard_ms = 0
        self._silence_ms = 0
        self._reply_task: Optional[asyncio.Task] = None
        self.turns = 0

    async def connect(self) -> None:
        return None

    async def append_audio(self, pcm16k: bytes) -> None:
        if not pcm16k:
            return
        count = len(pcm16k) // 2
        samples = struct.unpack(f"<{count}h", pcm16k[: count * 2])
        rms = math.sqrt(sum(s * s for s in samples) / count)
        ms = count * 1000 // INPUT_RATE
        if self.cfg.turn_mode != "server_vad":
            self._heard_ms += ms if rms > self.SPEECH_RMS else 0
            return
        if rms > self.SPEECH_RMS:
            if not self._speaking:
                self._speaking = True
                self._heard_ms = 0
                await self.cancel()
                await self._queue.put({"type": "speech_started"})
            self._heard_ms += ms
            self._silence_ms = 0
        elif self._speaking:
            self._silence_ms += ms
            if self._silence_ms >= self.cfg.vad_silence_ms:
                self._speaking = False
                await self._queue.put({"type": "speech_stopped"})
                self._start_reply(f"(mock) I spoke for {self._heard_ms} ms")

    def _start_reply(self, transcript: Optional[str]) -> None:
        self._reply_task = asyncio.create_task(self._reply(transcript))

    async def _reply(self, transcript: Optional[str]) -> None:
        self.turns += 1
        if transcript:
            await self._queue.put({"type": "user_transcript", "text": transcript})
        await self._queue.put({"type": "response_started"})
        audio = _tone(0.6)
        chunk = OUTPUT_RATE * 2 // 10  # 100 ms
        for i, part in enumerate(self.REPLY):
            await self._queue.put({"type": "text_delta", "text": part})
            half = audio[i * len(audio) // 2 : (i + 1) * len(audio) // 2]
            for j in range(0, len(half), chunk):
                await self._queue.put({"type": "audio", "pcm": half[j : j + chunk]})
                await asyncio.sleep(0.01)
        await self._queue.put({"type": "text_done", "text": "".join(self.REPLY)})
        await self._queue.put({"type": "response_done", "status": "completed", "usage": None})

    async def commit_and_respond(self) -> None:
        heard, self._heard_ms = self._heard_ms, 0
        self._start_reply(f"(mock) I spoke for {heard} ms")

    async def clear_input(self) -> None:
        self._heard_ms = 0

    async def say(self, text: str) -> None:
        self._start_reply(None)

    async def cancel(self) -> None:
        if self._reply_task and not self._reply_task.done():
            self._reply_task.cancel()
            await self._queue.put({"type": "response_done", "status": "cancelled", "usage": None})

    async def close(self) -> None:
        await self.cancel()
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[dict]:
        while True:
            ev = await self._queue.get()
            if ev is None:
                return
            yield ev
