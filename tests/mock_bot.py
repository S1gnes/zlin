"""
Мок бота: настоящий aiogram.Bot с подменённой сессией, без сети.

Все вызовы API складываются в self.calls, а ответы подставляются заранее — так апдейты
можно прогонять через настоящий диспетчер (со всеми фильтрами, middleware и FSM)
и проверять, что именно бот отправил бы в Telegram.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

BOT_ID = 424242
ADMIN_ID = 111
STRANGER_ID = 999
CHANNEL_ID = -1001234567890
DISCUSSION_ID = -1009876543210
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


class MockSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self.responses: dict[str, Any] = {}
        self.errors: dict[str, Exception] = {}
        self._next_message_id = 1000

    def set_response(self, method: str, value: Any) -> None:
        """Список — очередь ответов по порядку вызовов (например, два разных get_chat_member)."""
        self.responses[method] = value

    def set_error(self, method: str, error: Exception) -> None:
        self.errors[method] = error

    def names(self) -> list[str]:
        return [type(c).__name__ for c in self.calls]

    def last(self, method: str) -> Any:
        return next(c for c in reversed(self.calls) if type(c).__name__ == method)

    def count(self, method: str) -> int:
        return sum(1 for c in self.calls if type(c).__name__ == method)

    async def close(self) -> None:
        return None

    async def stream_content(self, url: str, headers: dict[str, Any] | None = None, timeout: int = 30,
                             chunk_size: int = 65536, raise_for_status: bool = True) -> AsyncGenerator[bytes, None]:
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        name = type(method).__name__
        self.calls.append(method)
        if name in self.errors:
            raise self.errors[name]
        if name in self.responses:
            prepared = self.responses[name]
            if isinstance(prepared, list):
                return prepared.pop(0) if len(prepared) > 1 else prepared[0]
            return prepared
        if name in ("SendMessage", "SendPhoto", "SendMediaGroup"):
            self._next_message_id += 1
            return make_message(self._next_message_id, chat_id=getattr(method, "chat_id", ADMIN_ID),
                                text=getattr(method, "text", ""))
        return True


def make_bot() -> tuple[Bot, MockSession]:
    session = MockSession()
    bot = Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return bot, session


def make_message(message_id: int, *, chat_id: int = ADMIN_ID, text: str = "", user_id: int = ADMIN_ID,
                 **extra: Any) -> Message:
    chat_type = "private" if chat_id > 0 else "supergroup"
    return Message(message_id=message_id, date=NOW, chat=Chat(id=chat_id, type=chat_type),
                   from_user=User(id=user_id, is_bot=False, first_name="Тест"), text=text, **extra)


def message_update(text: str, *, user_id: int = ADMIN_ID, update_id: int = 1) -> Update:
    return Update(update_id=update_id, message=make_message(1, text=text, user_id=user_id))


def callback_update(data: str, *, user_id: int = ADMIN_ID, message_id: int = 500,
                    update_id: int = 1) -> Update:
    card = make_message(message_id, text="черновик", user_id=BOT_ID)
    return Update(update_id=update_id,
                  callback_query=CallbackQuery(id="cb1", from_user=User(id=user_id, is_bot=False, first_name="Тест"),
                                               chat_instance="ci", data=data, message=card))


def forward_update(channel_message_id: int, *, update_id: int = 1) -> Update:
    """Служебная пересылка поста канала в группу обсуждений — её делает сам Telegram."""
    forwarded = Message(
        message_id=7001, date=NOW, chat=Chat(id=DISCUSSION_ID, type="supergroup"),
        sender_chat=Chat(id=CHANNEL_ID, type="channel"), is_automatic_forward=True, text="пост",
        forward_origin={"type": "channel", "date": NOW, "message_id": channel_message_id,
                        "chat": {"id": CHANNEL_ID, "type": "channel", "title": "Канал"}})
    return Update(update_id=update_id, message=forwarded)
