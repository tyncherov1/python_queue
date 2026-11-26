from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, Update

from app.bot import handlers_admin, handlers_student
from app.bot.middleware import WhitelistMiddleware
from app.config import Settings
from app.queue_service import QueueService

logger = logging.getLogger(__name__)


class BotRuntime:
    """Manages aiogram Bot + Dispatcher lifecycle in either polling or webhook mode."""

    def __init__(self, settings: Settings, queue_service: QueueService) -> None:
        self.settings = settings
        self.queue_service = queue_service
        self.bot: Bot | None = None
        self.dp: Dispatcher | None = None
        self._polling_task: asyncio.Task[Any] | None = None

    def _build_dispatcher(self) -> Dispatcher:
        dp = Dispatcher(storage=MemoryStorage())
        whitelist = WhitelistMiddleware(self.queue_service)
        dp.message.outer_middleware(whitelist)
        dp.callback_query.outer_middleware(whitelist)
        dp.include_router(handlers_admin.router)
        dp.include_router(handlers_student.router)
        return dp

    async def start(self) -> None:
        if not self.settings.bot_token:
            logger.warning(
                "BOT_TOKEN is empty — starting in web-only mode. "
                "Add BOT_TOKEN to .env to activate the Telegram bot."
            )
            return

        self.bot = Bot(
            token=self.settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = self._build_dispatcher()

        async def _send_notification(telegram_id: int, text: str) -> None:
            if self.bot is not None:
                await self.bot.send_message(chat_id=telegram_id, text=text)

        self.queue_service.set_notifier(_send_notification)

        try:
            me = await self.bot.get_me()
            if me.username:
                self.queue_service.set_bot_username(me.username)
                logger.info("Telegram bot authenticated as @%s", me.username)
            await self.bot.set_my_commands(
                [
                    BotCommand(command="start", description="Открыть меню бота"),
                    BotCommand(command="join", description="Записаться в очередь"),
                    BotCommand(command="leave", description="Отказаться от очереди"),
                    BotCommand(command="status", description="Моё место в очереди"),
                    BotCommand(command="queue", description="Посмотреть всю очередь"),
                    BotCommand(command="admin", description="Панель администратора"),
                    BotCommand(command="help", description="Справка по командам"),
                ]
            )
        except Exception as exc:
            logger.error("Failed to initialize Telegram bot metadata: %s", exc)

        if self.settings.bot_mode == "webhook" and self.settings.webhook_url:
            await self.bot.set_webhook(
                url=self.settings.webhook_url,
                secret_token=self.settings.resolved_webhook_secret,
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query"],
            )
            logger.info("Telegram bot configured in WEBHOOK mode: %s", self.settings.webhook_url)
        else:
            if self.settings.bot_mode == "webhook":
                logger.warning("BOT_MODE=webhook requested, but PUBLIC_URL is empty; falling back to polling.")
            await self.bot.delete_webhook(drop_pending_updates=True)
            self._polling_task = asyncio.create_task(
                self.dp.start_polling(
                    self.bot,
                    handle_signals=False,
                    allowed_updates=["message", "callback_query"],
                ),
                name="telegram-bot-polling",
            )
            logger.info("Telegram bot started in POLLING mode")

    async def feed_webhook_update(self, payload: dict[str, Any]) -> None:
        if self.bot is None or self.dp is None:
            return
        update = Update.model_validate(payload, context={"bot": self.bot})
        await self.dp.feed_update(self.bot, update)

    async def stop(self) -> None:
        self.queue_service.set_notifier(None)
        if self._polling_task is not None:
            if self.dp is not None:
                with contextlib.suppress(Exception):
                    await self.dp.stop_polling()
            self._polling_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._polling_task
            self._polling_task = None

        if self.bot is not None:
            with contextlib.suppress(Exception):
                await self.bot.session.close()
            self.bot = None
