"""Gateway for ESP32 hardware running the open-source xiaozhi-esp32 firmware.

Implements the xiaozhi WebSocket protocol (docs/websocket.md in that repo):
device -> server: hello / listen(start|stop|detect) / abort + Opus 16 kHz 60 ms frames
server -> device: hello / stt / llm / tts(start|sentence_start|stop) + Opus 24 kHz 60 ms frames
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
import uuid
from typing import Optional

import opuslib
from aiohttp import WSMsgType, web

from .bridge import Bridge, SentenceSplitter
from .config import CFG_KEY, Config
from .prompts import GREETING_TRIGGER
from .realtime import INPUT_RATE, OUTPUT_RATE, create_session

log = logging.getLogger("device")

FRAME_MS = 60
IN_FRAME = INPUT_RATE * FRAME_MS // 1000  # 960 samples
OUT_FRAME = OUTPUT_RATE * FRAME_MS // 1000  # 1440 samples
OUT_FRAME_BYTES = OUT_FRAME * 2
# How far ahead of real time we push audio into the device's buffer.
# Enough to absorb Wi-Fi jitter, small enough that interruptions feel instant.
LEAD_SECONDS = 0.3


def unpack_audio(data: bytes, version: int) -> Optional[bytes]:
    """Strip the optional binary protocol header (v2 / v3). Returns Opus payload or None for non-audio."""
    if version == 2:
        if len(data) < 16:
            return None
        _ver, typ, _res, _ts, size = struct.unpack(">HHIII", data[:16])
        return data[16 : 16 + size] if typ == 0 else None
    if version == 3:
        if len(data) < 4:
            return None
        typ, _res, size = struct.unpack(">BBH", data[:4])
        return data[4 : 4 + size] if typ == 0 else None
    return data


def pack_audio(payload: bytes, version: int, timestamp_ms: int = 0) -> bytes:
    if version == 2:
        return struct.pack(">HHIII", 2, 0, 0, timestamp_ms & 0xFFFFFFFF, len(payload)) + payload
    if version == 3:
        return struct.pack(">BBH", 0, 0, len(payload)) + payload
    return payload


class DeviceBridge(Bridge):
    def __init__(self, cfg: Config, ws: web.WebSocketResponse, version: int, device_id: str):
        super().__init__(cfg, create_session(cfg), f"device {device_id}")
        self.ws = ws
        self.version = version
        self.session_id = str(uuid.uuid4())
        self.decoder = opuslib.Decoder(INPUT_RATE, 1)
        self.encoder = opuslib.Encoder(OUTPUT_RATE, 1, opuslib.APPLICATION_VOIP)
        self.listening = False
        self.listen_mode = "auto"
        self.speaking = False
        self.splitter = SentenceSplitter()
        self._out: asyncio.Queue = asyncio.Queue()
        self._out_generation = 0
        self._tasks: list[asyncio.Task] = []

    # ---------- device -> model ----------

    async def send_json(self, msg: dict) -> None:
        msg.setdefault("session_id", self.session_id)
        if not self.ws.closed:
            await self.ws.send_str(json.dumps(msg, ensure_ascii=False))

    async def start(self) -> None:
        await self.rt.connect()
        self._tasks = [
            asyncio.create_task(self._pump_model_events()),
            asyncio.create_task(self._audio_sender()),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.rt.close()

    async def on_text(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "listen":
            state = msg.get("state")
            if state == "start":
                self.listening = True
                self.listen_mode = msg.get("mode", "auto")
                if self.cfg.turn_mode == "push_to_talk":
                    await self.rt.clear_input()
            elif state == "stop":
                self.listening = False
                if self.cfg.turn_mode == "push_to_talk":
                    self.mark_turn_end()
                    await self.rt.commit_and_respond()
            elif state == "detect":
                # Wake word heard on device: greet the child right away.
                await self.send_json({"type": "stt", "text": msg.get("text", "")})
                self.mark_turn_end()
                await self.rt.say(GREETING_TRIGGER)
        elif kind == "abort":
            await self.interrupt()
            await self._stop_speaking()
        else:
            log.debug("ignored device message %s", kind)

    async def on_audio(self, data: bytes) -> None:
        if not self.listening:
            return
        # In "auto" mode the device mic is half-duplex; in "realtime" mode (with AEC)
        # the child can talk over the tutor and the model VAD will interrupt.
        payload = unpack_audio(data, self.version)
        if not payload:
            return
        try:
            pcm = self.decoder.decode(payload, IN_FRAME)
        except opuslib.OpusError as e:
            log.warning("opus decode failed: %s", e)
            return
        await self.rt.append_audio(pcm)

    # ---------- model -> device ----------

    async def _pump_model_events(self) -> None:
        try:
            async for ev in self.rt.events():
                await self._on_model_event(ev)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("model event loop crashed")
        finally:
            if not self.ws.closed:
                await self.ws.close()

    async def _on_model_event(self, ev: dict) -> None:
        kind = ev["type"]
        if kind == "speech_started":
            if self.speaking or self.response_active:
                await self.interrupt()
                await self._stop_speaking()
        elif kind == "speech_stopped":
            self.mark_turn_end()
        elif kind == "user_transcript":
            log.info("[%s] 孩子: %s", self.name, ev["text"])
            await self.send_json({"type": "stt", "text": ev["text"]})
        elif kind == "response_started":
            self.response_active = True
            self.muted = False
            self.splitter = SentenceSplitter()
            self._out_generation += 1
            self.speaking = True
            await self.send_json({"type": "llm", "emotion": "happy", "text": "😊"})
            await self.send_json({"type": "tts", "state": "start"})
        elif kind == "text_delta" and not self.muted:
            for sentence in self.splitter.feed(ev["text"]):
                await self.send_json({"type": "tts", "state": "sentence_start", "text": sentence})
        elif kind == "text_done":
            log.info("[%s] 老师: %s", self.name, ev["text"])
        elif kind == "audio" and not self.muted:
            self.note_audio()
            await self._out.put((self._out_generation, ev["pcm"]))
        elif kind == "response_done":
            self.response_active = False
            self.note_usage(ev)
            rest = self.splitter.flush()
            if rest and not self.muted:
                await self.send_json({"type": "tts", "state": "sentence_start", "text": rest})
            await self._out.put((self._out_generation, None))  # end-of-response marker
        elif kind == "error":
            log.error("[%s] model error: %s", self.name, ev["message"])

    async def _stop_speaking(self) -> None:
        self._out_generation += 1  # drops everything still queued
        if self.speaking:
            self.speaking = False
            await self.send_json({"type": "tts", "state": "stop"})

    async def _audio_sender(self) -> None:
        """Encode 24 kHz PCM into 60 ms Opus frames and pace them to the device."""
        residual = b""
        clock_start: Optional[float] = None
        sent_seconds = 0.0
        generation = -1
        while True:
            gen, pcm = await self._out.get()
            if gen != self._out_generation:
                continue  # stale audio from an interrupted answer
            if gen != generation:
                generation, residual, clock_start, sent_seconds = gen, b"", None, 0.0
            if pcm is None:
                if residual:
                    await self._send_frame(residual.ljust(OUT_FRAME_BYTES, b"\0"))
                    residual = b""
                if self.speaking and gen == self._out_generation:
                    self.speaking = False
                    await self.send_json({"type": "tts", "state": "stop"})
                continue
            residual += pcm
            while len(residual) >= OUT_FRAME_BYTES:
                frame, residual = residual[:OUT_FRAME_BYTES], residual[OUT_FRAME_BYTES:]
                if clock_start is None:
                    clock_start = time.monotonic()
                ahead = sent_seconds - (time.monotonic() - clock_start)
                if ahead > LEAD_SECONDS:
                    await asyncio.sleep(ahead - LEAD_SECONDS)
                if gen != self._out_generation:
                    break  # interrupted while waiting
                await self._send_frame(frame)
                sent_seconds += FRAME_MS / 1000

    async def _send_frame(self, pcm_frame: bytes) -> None:
        packet = self.encoder.encode(pcm_frame, OUT_FRAME)
        if not self.ws.closed:
            await self.ws.send_bytes(pack_audio(packet, self.version, int(time.monotonic() * 1000)))


async def device_ws_handler(request: web.Request) -> web.WebSocketResponse:
    cfg: Config = request.app[CFG_KEY]
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if cfg.device_token and token != cfg.device_token:
        log.warning("rejected device %s: bad token", request.headers.get("Device-Id"))
        raise web.HTTPUnauthorized()
    try:
        version = int(request.headers.get("Protocol-Version", "1"))
    except ValueError:
        version = 1
    device_id = request.headers.get("Device-Id", "unknown")

    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
    await ws.prepare(request)
    bridge: Optional[DeviceBridge] = None
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == "hello":
                    if bridge is None:
                        bridge = DeviceBridge(cfg, ws, version, device_id)
                        await bridge.send_json(
                            {
                                "type": "hello",
                                "transport": "websocket",
                                "audio_params": {
                                    "format": "opus",
                                    "sample_rate": OUTPUT_RATE,
                                    "channels": 1,
                                    "frame_duration": FRAME_MS,
                                },
                            }
                        )
                        await bridge.start()
                        log.info("device %s connected (protocol v%d)", device_id, version)
                elif bridge is not None:
                    await bridge.on_text(data)
            elif msg.type == WSMsgType.BINARY and bridge is not None:
                await bridge.on_audio(msg.data)
    finally:
        if bridge is not None:
            await bridge.stop()
        log.info("device %s disconnected", device_id)
    return ws


async def ota_handler(request: web.Request) -> web.Response:
    """xiaozhi firmware asks this URL at boot; we tell it where our WebSocket is."""
    cfg: Config = request.app[CFG_KEY]
    current_version = ""
    if request.method == "POST":
        try:
            body = await request.json()
            current_version = (body.get("application") or {}).get("version", "")
        except Exception:
            pass
    ws_url = cfg.public_ws_url or f"ws://{request.host}/xiaozhi/v1/"
    return web.json_response(
        {
            "server_time": {
                "timestamp": int(time.time() * 1000),
                "timezone_offset": 480,
            },
            # Same version as the device reports => no firmware upgrade is pushed.
            "firmware": {"version": current_version, "url": ""},
            "websocket": {"url": ws_url, "token": cfg.device_token},
        }
    )
