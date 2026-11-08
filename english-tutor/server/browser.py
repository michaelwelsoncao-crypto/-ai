"""Gateway for the browser test page (no hardware needed).

browser -> server: binary 16 kHz PCM16 mono; JSON {"type": "ptt_start"|"ptt_stop"|"interrupt"|"greet"}
server -> browser: binary 24 kHz PCM16 mono; JSON events (see realtime.py) except audio
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import WSMsgType, web

from .bridge import Bridge
from .config import CFG_KEY, Config
from .prompts import GREETING_TRIGGER
from .realtime import create_session

log = logging.getLogger("browser")


class BrowserBridge(Bridge):
    def __init__(self, cfg: Config, ws: web.WebSocketResponse):
        super().__init__(cfg, create_session(cfg), "browser")
        self.ws = ws

    async def send_json(self, msg: dict) -> None:
        if not self.ws.closed:
            await self.ws.send_str(json.dumps(msg, ensure_ascii=False))

    async def pump(self) -> None:
        try:
            async for ev in self.rt.events():
                kind = ev["type"]
                if kind == "audio":
                    if self.muted:
                        continue
                    self.note_audio()
                    if not self.ws.closed:
                        await self.ws.send_bytes(ev["pcm"])
                    continue
                if kind == "speech_started" and self.response_active:
                    await self.interrupt()
                    await self.send_json({"type": "interrupt"})
                elif kind == "speech_stopped":
                    self.mark_turn_end()
                elif kind == "response_started":
                    self.response_active = True
                    self.muted = False
                elif kind == "response_done":
                    self.response_active = False
                    self.note_usage(ev)
                    ev = {**ev, "latency_ms": self.last_latency_ms}
                elif kind == "error":
                    log.error("model error: %s", ev["message"])
                await self.send_json(ev)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("model event loop crashed")
        finally:
            if not self.ws.closed:
                await self.ws.close()


async def browser_ws_handler(request: web.Request) -> web.WebSocketResponse:
    cfg: Config = request.app[CFG_KEY]
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
    await ws.prepare(request)
    bridge = BrowserBridge(cfg, ws)
    try:
        await bridge.rt.connect()
    except Exception as e:
        log.exception("cannot connect to model")
        await bridge.send_json({"type": "error", "message": f"连接大模型失败: {e}"})
        await ws.close()
        return ws
    await bridge.send_json({"type": "ready", "turn_mode": cfg.turn_mode, "model": cfg.model})
    pump = asyncio.create_task(bridge.pump())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                await bridge.rt.append_audio(msg.data)
            elif msg.type == WSMsgType.TEXT:
                kind = json.loads(msg.data).get("type")
                if kind == "ptt_start":
                    if bridge.response_active:
                        await bridge.interrupt()
                    await bridge.rt.clear_input()
                elif kind == "ptt_stop":
                    bridge.mark_turn_end()
                    await bridge.rt.commit_and_respond()
                elif kind == "interrupt":
                    await bridge.interrupt()
                elif kind == "greet":
                    bridge.mark_turn_end()
                    await bridge.rt.say(GREETING_TRIGGER)
    finally:
        pump.cancel()
        await bridge.rt.close()
    return ws
