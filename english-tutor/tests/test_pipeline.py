import asyncio
import base64
import json
import math
import struct

import opuslib
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from server.app import build_app
from server.config import Config
from server.realtime import QwenOmniSession

TOKEN = "test-token"


def make_cfg(**kw) -> Config:
    cfg = Config()
    cfg.provider = "mock"
    cfg.device_token = TOKEN
    cfg.vad_silence_ms = 300
    cfg.public_ws_url = ""
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def pcm16k(seconds: float, loud: bool) -> bytes:
    n = int(16000 * seconds)
    amp = 8000 if loud else 0
    return b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 300 * i / 16000))) for i in range(n))


def opus_frames(pcm: bytes) -> list[bytes]:
    enc = opuslib.Encoder(16000, 1, opuslib.APPLICATION_VOIP)
    step = 960 * 2
    return [enc.encode(pcm[i : i + step], 960) for i in range(0, len(pcm) - step + 1, step)]


async def client_for(cfg: Config) -> TestClient:
    client = TestClient(TestServer(build_app(cfg)))
    await client.start_server()
    return client


async def collect_until(ws, pred, timeout=5.0):
    """Read messages until pred(json_msg) is true. Returns (json msgs, binary frames)."""
    msgs, frames = [], []
    async with asyncio.timeout(timeout):
        while True:
            m = await ws.receive()
            if m.type == WSMsgType.BINARY:
                frames.append(m.data)
            elif m.type == WSMsgType.TEXT:
                data = json.loads(m.data)
                msgs.append(data)
                if pred(data):
                    return msgs, frames
            else:
                raise AssertionError(f"socket closed: {m}")


async def device_connect(client: TestClient):
    ws = await client.ws_connect(
        "/xiaozhi/v1/",
        headers={"Authorization": f"Bearer {TOKEN}", "Protocol-Version": "1", "Device-Id": "aa:bb", "Client-Id": "x"},
    )
    await ws.send_str(json.dumps({
        "type": "hello", "version": 1, "transport": "websocket",
        "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60},
    }))
    hello = json.loads((await ws.receive()).data)
    return ws, hello


async def test_ota_points_device_to_websocket():
    client = await client_for(make_cfg())
    try:
        r = await client.post("/xiaozhi/ota/", json={"application": {"version": "1.2.3"}})
        body = await r.json()
        assert body["websocket"]["token"] == TOKEN
        assert body["websocket"]["url"].endswith("/xiaozhi/v1/")
        assert body["firmware"]["version"] == "1.2.3"
    finally:
        await client.close()


async def test_device_rejects_bad_token():
    client = await client_for(make_cfg())
    try:
        with pytest.raises(Exception):
            await client.ws_connect("/xiaozhi/v1/", headers={"Authorization": "Bearer nope"})
    finally:
        await client.close()


async def test_device_hands_free_turn():
    client = await client_for(make_cfg())
    try:
        ws, hello = await device_connect(client)
        assert hello["type"] == "hello" and hello["transport"] == "websocket"
        assert hello["audio_params"]["sample_rate"] == 24000
        sid = hello["session_id"]
        await ws.send_str(json.dumps({"session_id": sid, "type": "listen", "state": "start", "mode": "auto"}))
        for f in opus_frames(pcm16k(0.6, True) + pcm16k(0.6, False)):
            await ws.send_bytes(f)
        msgs, frames = await collect_until(ws, lambda m: m.get("type") == "tts" and m.get("state") == "stop")
        types = [(m["type"], m.get("state")) for m in msgs]
        assert ("stt", None) in types
        assert ("tts", "start") in types
        assert any(m.get("state") == "sentence_start" and "Great job" in m["text"] for m in msgs)
        assert all(m["session_id"] == sid for m in msgs)
        # 0.6 s reply at 60 ms frames => ~10 decodable 24 kHz Opus frames
        assert len(frames) >= 9
        dec = opuslib.Decoder(24000, 1)
        assert len(dec.decode(frames[0], 1440)) == 1440 * 2
        await ws.close()
    finally:
        await client.close()


async def test_device_push_to_talk_and_wake_word():
    client = await client_for(make_cfg(turn_mode="push_to_talk"))
    try:
        ws, hello = await device_connect(client)
        sid = hello["session_id"]
        await ws.send_str(json.dumps({"session_id": sid, "type": "listen", "state": "detect", "text": "你好小智"}))
        msgs, _ = await collect_until(ws, lambda m: m.get("type") == "tts" and m.get("state") == "stop")
        assert msgs[0] == {"session_id": sid, "type": "stt", "text": "你好小智"}

        await ws.send_str(json.dumps({"session_id": sid, "type": "listen", "state": "start", "mode": "manual"}))
        for f in opus_frames(pcm16k(0.5, True)):
            await ws.send_bytes(f)
        await ws.send_str(json.dumps({"session_id": sid, "type": "listen", "state": "stop"}))
        msgs, frames = await collect_until(ws, lambda m: m.get("type") == "tts" and m.get("state") == "stop")
        stt = [m for m in msgs if m["type"] == "stt"]
        assert stt and "spoke for" in stt[0]["text"]
        assert frames
        await ws.close()
    finally:
        await client.close()


async def test_device_abort_stops_speaking():
    client = await client_for(make_cfg(turn_mode="push_to_talk"))
    try:
        ws, hello = await device_connect(client)
        sid = hello["session_id"]
        await ws.send_str(json.dumps({"session_id": sid, "type": "listen", "state": "detect", "text": "hi"}))
        await collect_until(ws, lambda m: m.get("type") == "tts" and m.get("state") == "start")
        await ws.send_str(json.dumps({"session_id": sid, "type": "abort", "reason": "wake_word_detected"}))
        msgs, _ = await collect_until(ws, lambda m: m.get("type") == "tts" and m.get("state") == "stop", timeout=2)
        # after abort no more audio should arrive
        await asyncio.sleep(0.3)
        leftovers = []
        while True:
            try:
                m = await asyncio.wait_for(ws.receive(), 0.05)
            except asyncio.TimeoutError:
                break
            leftovers.append(m)
        assert not [m for m in leftovers if m.type == WSMsgType.BINARY]
        await ws.close()
    finally:
        await client.close()


async def test_browser_greeting_and_turn():
    client = await client_for(make_cfg())
    try:
        ws = await client.ws_connect("/ws/browser")
        ready = json.loads((await ws.receive()).data)
        assert ready["type"] == "ready"
        await ws.send_str(json.dumps({"type": "greet"}))
        msgs, audio = await collect_until(ws, lambda m: m.get("type") == "response_done")
        assert sum(len(a) for a in audio) == int(24000 * 0.6) * 2
        assert msgs[-1]["latency_ms"] is not None

        await ws.send_bytes(pcm16k(0.5, True))
        await ws.send_bytes(pcm16k(0.5, False))
        msgs, _ = await collect_until(ws, lambda m: m.get("type") == "response_done")
        kinds = [m["type"] for m in msgs]
        assert kinds[:3] == ["speech_started", "speech_stopped", "user_transcript"]
        await ws.close()
    finally:
        await client.close()


# ---------- Qwen wire protocol against a fake DashScope server ----------


def test_qwen38_session_payload():
    cfg = make_cfg(provider="qwen", model="qwen3.8-omni-flash-realtime", voice="longanlingxin")
    s = QwenOmniSession(cfg).session_payload()
    assert s["audio"]["input"]["format"] == {"type": "pcm", "sample_rate": 16000}
    assert s["audio"]["output"] == {"format": {"type": "pcm", "sample_rate": 24000}, "voice": "longanlingxin"}
    assert s["turn_detection"]["type"] == "server_vad"
    assert s["input_audio_transcription"]["model"] == "qwen3-asr-flash-realtime"
    assert "voice" not in s and "input_audio_format" not in s


def test_legacy_omni_session_payload():
    cfg = make_cfg(provider="qwen", model="qwen3-omni-flash-realtime", turn_mode="push_to_talk")
    s = QwenOmniSession(cfg).session_payload()
    assert s["voice"] == "Cherry" and s["input_audio_format"] == "pcm16"
    assert s["turn_detection"] is None
    assert "audio" not in s


async def test_qwen_client_against_fake_server():
    received = []

    async def fake(request):
        assert request.headers["Authorization"] == "Bearer sk-test"
        assert request.query["model"] == "qwen3.8-omni-flash-realtime"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for m in ws:
            ev = json.loads(m.data)
            received.append(ev)
            if ev["type"] == "session.update":
                await ws.send_json({"type": "session.updated", "session": {"id": "s1"}})
            if ev["type"] == "input_audio_buffer.append":
                for out in (
                    {"type": "input_audio_buffer.speech_started"},
                    {"type": "input_audio_buffer.speech_stopped"},
                    {"type": "conversation.item.input_audio_transcription.completed", "transcript": "I like dog"},
                    {"type": "response.created", "response": {"id": "r1"}},
                    {"type": "response.audio_transcript.delta", "delta": "You like dogs!"},
                    {"type": "response.audio.delta", "delta": base64.b64encode(b"\x01\x00" * 10).decode()},
                    {"type": "response.done", "response": {"id": "r1", "status": "completed", "usage": {"total_tokens": 9}}},
                ):
                    await ws.send_json(out)
                await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/api-ws/v1/realtime", fake)
    server = TestServer(app)
    await server.start_server()
    try:
        cfg = make_cfg(provider="qwen", dashscope_api_key="sk-test",
                       realtime_url=str(server.make_url("/api-ws/v1/realtime")).replace("http", "ws"))
        rt = QwenOmniSession(cfg)
        await rt.connect()
        await rt.append_audio(b"\x00\x00" * 160)
        events = [ev async for ev in rt.events()]
        await rt.close()
        assert received[0]["type"] == "session.update"
        assert received[1]["type"] == "input_audio_buffer.append"
        assert [e["type"] for e in events] == [
            "speech_started", "speech_stopped", "user_transcript", "response_started",
            "text_delta", "audio", "response_done",
        ]
        assert events[2]["text"] == "I like dog"
        assert events[5]["pcm"] == b"\x01\x00" * 10
        assert events[6]["usage"] == {"total_tokens": 9}
    finally:
        await server.close()
