"""
Сборка бота: проверки при старте, зависимости хендлеров, запуск.

Проверки при старте отвечают на вопросы, которые иначе всплывут молчанием в канале:
админ ли бот в канале, может ли постить, привязана ли группа обсуждений и видит ли он
в ней служебные пересылки. Всё найденное уходит владельцу в личку одним сообщением.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage

from ..collector import Collector
from ..db import Database
from ..media import MediaStore
from ..pipeline import Summarizer
from .access import AdminOnly
from . import admin
from .handlers import make_router
from .publisher import Publisher

log = logging.getLogger(__name__)


@dataclass
class ChannelInfo:
    chat_id: int | None = None
    username: str | None = None
    title: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.chat_id is not None


async def check_channel(bot: Bot, channel_id: str | int) -> ChannelInfo:
    info = ChannelInfo()
    try:
        chat = await bot.get_chat(channel_id)
    except TelegramAPIError as e:
        info.problems.append(f"Канал {channel_id} недоступен: {e}. Проверь CHANNEL_ID и что бот добавлен в канал.")
        return info
    info.chat_id, info.username, info.title = chat.id, chat.username, chat.title

    me = await bot.get_me()
    try:
        member = await bot.get_chat_member(chat.id, me.id)
    except TelegramAPIError as e:
        info.problems.append(f"Не вижу себя в канале: {e}")
        return info
    if member.status != "administrator":
        info.problems.append("Бот не администратор канала — публиковать не сможет.")
    elif getattr(member, "can_post_messages", True) is False:
        info.problems.append("У бота в канале нет права публиковать сообщения.")
    return info


def startup_report(info: ChannelInfo) -> str:
    where = f"«{info.title}»" + (f" (@{info.username})" if info.username else "")
    head = f"🤖 Запустился. Канал: {where}." if info.ok else "🤖 Запустился, но до канала не достучался."
    if not info.problems:
        return head + " Проблем не вижу."
    return head + "\n\n" + "\n".join(f"⚠️ {p}" for p in info.problems)


def build_dispatcher(db: Database, publisher: Publisher, gemini: Summarizer | None, *,
                     admin_id: int, store: MediaStore | None = None,
                     collector: Collector | None = None) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(AdminOnly(admin_id))
    dp["db"] = db
    dp["publisher"] = publisher
    dp["gemini"] = gemini
    dp["store"] = store
    dp["collector"] = collector
    dp.include_router(make_router())
    dp.include_router(admin.make_router())
    return dp


def make_bot(token: str) -> Bot:
    return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
