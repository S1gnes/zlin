"""
Публикация черновика в канал.

Двойное нажатие «Опубликовать» ловится не проверкой в памяти, а атомарной сменой статуса
в БД: pending -> publishing. Кто успел — тот и публикует, второй получает отказ. Если
Telegram отказал, статус возвращается в pending, чтобы можно было нажать ещё раз.

Медиа: подпись у media group — всего 1024 символа против 4096 у обычного сообщения,
поэтому длинный пересказ уходит отдельным сообщением следом за альбомом. Если Telegram
медиа не принял (а он привередлив к форматам), пост всё равно выходит текстом: терять
новость из-за картинки нельзя.

Переводы идут в самом посте, отдельными абзацами. Комментарий в группе обсуждений больше
не пишется: половина читателей его не открывала, а пост и так самодостаточен.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (FSInputFile, InputMediaPhoto, InputMediaVideo, LinkPreviewOptions, Message,
                           ReplyParameters)

from ..db import Database
from ..media import LocalMedia, MediaStore
from . import texts

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)  # превью ссылки FB для незалогиненного — страница входа
MEDIA_MODE_KEY = "media_mode"                      # copy — переносить файлы, link — только ссылка


@dataclass(frozen=True, slots=True)
class PublishResult:
    ok: bool
    draft_id: int
    link: str | None = None
    media_sent: int = 0
    media_failed: bool = False   # медиа не приняли — вышел текст
    reason: str | None = None


class Publisher:
    def __init__(self, bot: Bot, db: Database, channel_id: str | int, *,
                 channel_username: str | None = None,
                 store: MediaStore | None = None, clock: Callable[[], float] = time.time) -> None:
        self.bot = bot
        self.db = db
        self.channel_id = channel_id
        self.channel_username = channel_username
        self.store = store
        self.clock = clock

    async def publish(self, draft_id: int) -> PublishResult:
        if not await self.db.set_draft_status(draft_id, "publishing", expect=["pending"], now=self.clock()):
            return PublishResult(False, draft_id, reason=await self._why_not(draft_id))
        draft = await self.db.get_draft(draft_id)
        post = await self.db.get_post(draft.post_id) if draft else None
        if draft is None or post is None:
            return PublishResult(False, draft_id, reason="черновик или исходная запись потерялись")
        group = await self.db.get_group(post.group_id) if post.group_id else None

        text = texts.channel_post(draft, post, group)
        media = await self._media_for(draft_id)
        try:
            message, sent, failed = await self._send(text, media)
        except TelegramAPIError as e:
            log.exception("черновик #%s: канал не принял пост", draft_id)
            await self.db.set_draft_status(draft_id, "pending", expect=["publishing"], now=self.clock())
            return PublishResult(False, draft_id, reason=f"Telegram отказал: {e}")

        await self.db.set_draft_status(draft_id, "published", expect=["publishing"], now=self.clock(),
                                       channel_msg_id=message.message_id)
        await self.db.set_post_status(draft.post_id, "published", expect=["pending"], now=self.clock())
        await self.db.log("published", group_id=post.group_id, post_id=post.post_id, now=self.clock())
        if self.store:
            self.store.clear(draft_id)
        return PublishResult(True, draft_id, self._link(message.message_id),
                             media_sent=sent, media_failed=failed)

    async def reject(self, draft_id: int) -> bool:
        if not await self.db.set_draft_status(draft_id, "rejected", expect=["pending"], now=self.clock()):
            return False
        draft = await self.db.get_draft(draft_id)
        if draft:
            await self.db.set_post_status(draft.post_id, "rejected", expect=["pending"], now=self.clock())
            await self.db.log("rejected", post_id=draft.post_id, now=self.clock())
        if self.store:
            self.store.clear(draft_id)
        return True

    # -- внутреннее ----------------------------------------------------------

    async def _media_for(self, draft_id: int) -> list[LocalMedia]:
        if self.store is None:
            return []
        if await self.db.get_setting(MEDIA_MODE_KEY, "copy") != "copy":
            return []                      # режим «только ссылка»
        return self.store.local(draft_id)

    async def _send(self, text: str, media: Sequence[LocalMedia]) -> tuple[Message, int, bool]:
        """Возвращает (первое сообщение поста, сколько файлов ушло, был ли откат на текст)."""
        if not media:
            return await self._send_text(text), 0, False
        caption_fits = len(text) <= texts.MAX_CAPTION
        try:
            first = await self._send_album(text if caption_fits else None, media)
        except TelegramAPIError as e:
            log.warning("медиа не приняты Telegram (%s) — публикую текстом", e)
            return await self._send_text(text), 0, True
        if not caption_fits:               # подпись альбома короче сообщения — текст идёт следом
            await self.bot.send_message(self.channel_id, text, link_preview_options=NO_PREVIEW,
                                        reply_parameters=ReplyParameters(message_id=first.message_id))
        return first, len(media), False

    async def _send_text(self, text: str) -> Message:
        return await self.bot.send_message(self.channel_id, text, link_preview_options=NO_PREVIEW)

    async def _send_album(self, caption: str | None, media: Sequence[LocalMedia]) -> Message:
        if len(media) == 1:
            item = media[0]
            send = self.bot.send_video if item.kind == "video" else self.bot.send_photo
            return await send(self.channel_id, FSInputFile(item.path), caption=caption)
        group = []
        for index, item in enumerate(media):
            cls = InputMediaVideo if item.kind == "video" else InputMediaPhoto
            group.append(cls(media=FSInputFile(item.path), caption=caption if index == 0 else None))
        messages = await self.bot.send_media_group(self.channel_id, group)
        return messages[0]

    async def _why_not(self, draft_id: int) -> str:
        draft = await self.db.get_draft(draft_id)
        if draft is None:
            return "черновик не найден"
        return {
            "publishing": "публикация уже идёт",
            "published": "этот черновик уже опубликован",
            "rejected": "черновик был отклонён",
            "superseded": "черновик устарел: есть переписанная версия",
        }.get(draft.status, f"статус черновика — {draft.status}")

    def _link(self, message_id: int) -> str | None:
        if self.channel_username:
            return f"https://t.me/{self.channel_username}/{message_id}"
        raw = str(self.channel_id)
        return f"https://t.me/c/{raw[4:]}/{message_id}" if raw.startswith("-100") else None
