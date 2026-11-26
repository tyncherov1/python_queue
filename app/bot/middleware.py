from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from app.queue_service import QueueService

logger = logging.getLogger(__name__)


class WhitelistMiddleware(BaseMiddleware):
    """
    Strictly allows only students present in students.csv and configured ADMIN_IDS.
    All other Telegram users are completely ignored with ZERO reply or callback answer.
    """

    def __init__(self, queue_service: QueueService) -> None:
        super().__init__()
        self.queue_service = queue_service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: User | None = None
        if isinstance(event, (Message, CallbackQuery)):
            tg_user = event.from_user

        if tg_user is None or tg_user.is_bot:
            return None

        student = await self.queue_service.db.resolve_telegram_user(
            telegram_id=tg_user.id,
            username=tg_user.username,
            fallback_full_name=tg_user.full_name or f"Admin {tg_user.id}",
        )
        if student is None:
            logger.debug(
                "Silently ignored unauthorized user id=%s username=%s",
                tg_user.id,
                tg_user.username,
            )
            return None

        data["student"] = student
        data["queue_service"] = self.queue_service
        return await handler(event, data)
