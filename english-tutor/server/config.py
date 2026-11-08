"""Runtime configuration, all from environment variables (see .env.example)."""

import os
from dataclasses import dataclass, field

from aiohttp import web


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass
class Config:
    # "qwen" = 阿里云百炼 Qwen-Omni 实时语音; "mock" = 离线假模型（无需 key，用于联调）
    provider: str = field(default_factory=lambda: _env("PROVIDER", "qwen"))

    dashscope_api_key: str = field(default_factory=lambda: _env("DASHSCOPE_API_KEY", ""))
    # 北京地域。国际站用 wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime
    realtime_url: str = field(
        default_factory=lambda: _env(
            "REALTIME_URL", "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
        )
    )
    model: str = field(default_factory=lambda: _env("MODEL", "qwen3.8-omni-flash-realtime"))
    voice: str = field(default_factory=lambda: _env("VOICE", ""))
    # 转写模型（给家长端显示孩子说了什么、发 stt 给设备屏幕）。留空用模型默认。
    transcription_model: str = field(default_factory=lambda: _env("TRANSCRIPTION_MODEL", ""))

    # 轮次检测：server_vad（免按键，自动判断说完）或 push_to_talk（按住说话，最稳）
    turn_mode: str = field(default_factory=lambda: _env("TURN_MODE", "server_vad"))
    # 小孩说话停顿多、声音小：阈值偏低、静音时长偏长，避免话说一半被截断
    vad_threshold: float = field(default_factory=lambda: float(_env("VAD_THRESHOLD", "0.3")))
    vad_silence_ms: int = field(default_factory=lambda: int(_env("VAD_SILENCE_MS", "900")))
    vad_prefix_ms: int = field(default_factory=lambda: int(_env("VAD_PREFIX_MS", "300")))

    # 孩子信息，会写进系统提示词
    child_name: str = field(default_factory=lambda: _env("CHILD_NAME", "小朋友"))
    child_age: int = field(default_factory=lambda: int(_env("CHILD_AGE", "7")))
    english_level: str = field(default_factory=lambda: _env("ENGLISH_LEVEL", "beginner"))
    tutor_name: str = field(default_factory=lambda: _env("TUTOR_NAME", "Lily"))

    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8000")))
    # 设备连接用的 token；设备 OTA 时会自动拿到
    device_token: str = field(default_factory=lambda: _env("DEVICE_TOKEN", "change-me"))
    # 设备访问本服务的地址（OTA 下发给设备），如 ws://192.168.1.10:8000/xiaozhi/v1/
    public_ws_url: str = field(default_factory=lambda: _env("PUBLIC_WS_URL", ""))

    @property
    def is_qwen38(self) -> bool:
        return self.model.startswith("qwen3.8-")


CFG_KEY = web.AppKey("cfg", Config)
