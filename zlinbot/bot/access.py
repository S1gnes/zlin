"""
Доступ: боту отвечает только владелец, всё остальное — отказ.

Проверка стоит внешним middleware на всём диспетчере, а не фильтром на каждом хендлере:
забыть повесить фильтр на новый хендлер легко, а пропустить middleware — нет.

Исключение ровно одно: служебная пересылка поста канала в привязанную группу обсуждений.
Её присылает сам Telegram, не человек, и без неё нельзя оставить перевод первым комментарием.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject, Update

log = logging.getLogger(__name__)

DENY_TEXT = "Этот бот личный: он слушается только владельца."


class AdminOnly(BaseMiddleware):
    def __init__(self, admin_id: int, discussion_chat_id: int | None = None) -> None:
        self.admin_id = admin_id
        self.discussion_chat_id = discussion_chat_id

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        if isinstance(event, Update):
            if self._is_channel_forward(event):
                return await handler(event, data)
            user = event.event.from_user if hasattr(event.event, "from_user") else None
            if user is None or user.id != self.admin_id:
                log.warning("отказ: обновление от %s", getattr(user, "id", "неизвестно"))
                if isinstance(event.event, CallbackQuery):
                    await event.event.answer(DENY_TEXT, show_alert=True)
                return None
        return await handler(event, data)

    def _is_channel_forward(self, update: Update) -> bool:
        message = update.message
        return bool(self.discussion_chat_id and message and message.is_automatic_forward
                    and message.chat.id == self.discussion_chat_id)
