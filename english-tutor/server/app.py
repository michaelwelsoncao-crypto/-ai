"""Entry point: python -m server.app"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from aiohttp import web

from .browser import browser_ws_handler
from .config import CFG_KEY, Config
from .device import device_ws_handler, ota_handler

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


async def index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html")


def build_app(cfg: Config) -> web.Application:
    app = web.Application()
    app[CFG_KEY] = cfg
    app.router.add_get("/", index)
    app.router.add_get("/ws/browser", browser_ws_handler)
    app.router.add_get("/xiaozhi/v1/", device_ws_handler)
    app.router.add_route("*", "/xiaozhi/ota/", ota_handler)
    app.router.add_static("/static/", WEB_DIR)
    return app


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config()
    logging.getLogger("app").info(
        "provider=%s model=%s turn_mode=%s", cfg.provider, cfg.model, cfg.turn_mode
    )
    web.run_app(build_app(cfg), host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
