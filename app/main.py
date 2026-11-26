from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.bot.runner import BotRuntime
from app.config import Settings, load_settings
from app.database import Database
from app.queue_service import QueueService
from app.web.routes import STATIC_DIR, router as web_router
from app.ws_manager import WebSocketManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, start_bot: bool = True) -> FastAPI:
    cfg = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(cfg)
        await db.connect()
        await db.sync_students_from_csv()

        ws_manager = WebSocketManager()
        queue_service = QueueService(db=db, settings=cfg, ws_manager=ws_manager)
        bot_runtime = BotRuntime(settings=cfg, queue_service=queue_service)

        app.state.settings = cfg
        app.state.db = db
        app.state.queue_service = queue_service
        app.state.bot_runtime = bot_runtime

        if start_bot:
            await bot_runtime.start()

        logger.info("Seminar Queue server ready on http://%s:%s", cfg.host, cfg.port)
        try:
            yield
        finally:
            await bot_runtime.stop()
            await db.close()
            logger.info("Seminar Queue server stopped cleanly")

    app = FastAPI(
        title="Seminar Queue Bot & Whiteboard",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(web_router)
    return app


app = create_app()
