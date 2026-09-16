"""
Хендлеры: черновик с кнопками, публикация, отклонение, переписывание.

Кнопка «Переписать» спрашивает, что поправить, и даёт кнопку «просто заново»: так одним
действием закрываются оба случая — и «не то написал», и «попробуй ещё раз».
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..db import Database
from ..gemini import GeminiError
from ..pipeline import Summarizer, rewrite_draft
from . import texts
from .keyboards import DraftAction, draft_keyboard, rewrite_keyboard
from .publisher import NO_PREVIEW, Publisher

log = logging.getLogger(__name__)

PENDING_LIMIT = 5

HELP = ("Я собираю записи из источников, пересказываю их через Gemini и показываю тебе черновики.\n\n"
        "/pending — очередь черновиков\n"
        "/stats — что происходило за сутки и неделю\n\n"
        "Остальное управление появится на этапе 6.")


class Rewriting(StatesGroup):
    waiting_instruction = State()


async def send_draft(bot: Bot, db: Database, chat_id: int, draft_id: int) -> int | None:
    """Показать черновик с кнопками. Возвращает id сообщения — по нему потом убираем кнопки."""
    draft = await db.get_draft(draft_id)
    post = await db.get_post(draft.post_id) if draft else None
    if draft is None or post is None:
        return None
    group = await db.get_group(post.group_id) if post.group_id else None
    message = await bot.send_message(chat_id, texts.draft_card(draft, post, group),
                                     reply_markup=draft_keyboard(draft.id, post.permalink),
                                     link_preview_options=NO_PREVIEW)
    await db.set_draft_message(draft.id, message.message_id)
    return message.message_id


async def cmd_start(message: Message, db: Database) -> None:
    counts = await db.post_status_counts()
    queue = len(await db.drafts(status="pending", limit=99))
    await message.answer(f"{HELP}\n\nСейчас в очереди черновиков: {queue}. "
                         f"Записей в базе: {sum(counts.values())}.")


async def cmd_pending(message: Message, bot: Bot, db: Database) -> None:
    drafts = await db.drafts(status="pending", limit=99)
    if not drafts:
        await message.answer("Очередь пуста.")
        return
    await message.answer(f"Черновиков в очереди: {len(drafts)}."
                         + (f" Показываю первые {PENDING_LIMIT}." if len(drafts) > PENDING_LIMIT else ""))
    for draft in drafts[:PENDING_LIMIT]:
        await send_draft(bot, db, message.chat.id, draft.id)


async def on_publish(query: CallbackQuery, callback_data: DraftAction, publisher: Publisher) -> None:
    result = await publisher.publish(callback_data.draft_id)
    if not result.ok:
        await query.answer(result.reason or "не получилось", show_alert=True)
        await _drop_buttons(query)
        return
    draft = await publisher.db.get_draft(result.draft_id)
    await query.answer("Опубликовано")
    await _drop_buttons(query)
    await query.message.answer(texts.published_note(draft, result.link, commented=not result.with_spoiler))


async def on_reject(query: CallbackQuery, callback_data: DraftAction, publisher: Publisher) -> None:
    done = await publisher.reject(callback_data.draft_id)
    await query.answer("Отклонено" if done else "Черновик уже не в очереди", show_alert=not done)
    await _drop_buttons(query)


async def on_rewrite(query: CallbackQuery, callback_data: DraftAction, state: FSMContext) -> None:
    await state.set_state(Rewriting.waiting_instruction)
    await state.update_data(draft_id=callback_data.draft_id)
    await query.answer()
    await query.message.answer(f"Что поправить в черновике #{callback_data.draft_id}? "
                               f"Напиши одним сообщением — или просто перегенерирую.",
                               reply_markup=rewrite_keyboard(callback_data.draft_id))


async def on_again(query: CallbackQuery, callback_data: DraftAction, state: FSMContext,
                   bot: Bot, db: Database, gemini: Summarizer | None) -> None:
    await state.clear()
    await query.answer("Переписываю")
    await _rewrite(bot, db, gemini, query.message, callback_data.draft_id, "")


async def on_instruction(message: Message, state: FSMContext, bot: Bot, db: Database,
                         gemini: Summarizer | None) -> None:
    draft_id = (await state.get_data()).get("draft_id")
    await state.clear()
    if draft_id:
        await _rewrite(bot, db, gemini, message, int(draft_id), message.text or "")


async def on_channel_forward(message: Message, publisher: Publisher) -> None:
    """Пересылка поста канала в группу обсуждений: отвечаем на неё переводом."""
    await publisher.on_channel_forward(message)


async def _rewrite(bot: Bot, db: Database, gemini: Summarizer | None, message: Message,
                   draft_id: int, instruction: str) -> None:
    if gemini is None:
        await message.answer("Нет ключа Gemini — переписать не могу.")
        return
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        new_id = await rewrite_draft(db, gemini, draft_id, instruction=instruction)
    except GeminiError as e:
        await message.answer(f"Gemini не ответил: {e}")
        return
    if new_id is None:
        await message.answer(f"Не переписал: черновик #{draft_id} уже не в очереди, "
                             f"либо модель решила, что запись каналу не подходит.")
        return
    await send_draft(bot, db, message.chat.id, new_id)


def make_router() -> Router:
    """Новый роутер на каждый диспетчер: один общий объект aiogram повторно включить не даёт,
    да и глобальное состояние в тестах только мешает."""
    router = Router(name="drafts")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_start, Command("help"))
    router.message.register(cmd_pending, Command("pending"))
    router.message.register(on_channel_forward, F.is_automatic_forward)
    router.message.register(on_instruction, Rewriting.waiting_instruction, F.text)
    router.callback_query.register(on_publish, DraftAction.filter(F.action == "publish"))
    router.callback_query.register(on_reject, DraftAction.filter(F.action == "reject"))
    router.callback_query.register(on_rewrite, DraftAction.filter(F.action == "rewrite"))
    router.callback_query.register(on_again, DraftAction.filter(F.action == "again"),
                                   Rewriting.waiting_instruction)
    return router


async def _drop_buttons(query: CallbackQuery) -> None:
    """Убрать кнопки у карточки: решение принято, нажимать больше нечего."""
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001 — сообщение могли удалить, это не повод падать
        log.debug("не удалось убрать кнопки", exc_info=True)
