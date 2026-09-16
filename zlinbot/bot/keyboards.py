"""
Кнопки черновика.

В callback_data у Telegram всего 64 байта, поэтому туда идёт только id строки в БД —
ни текста, ни ссылок. Ссылка на оригинал — отдельная кнопка-URL, она вообще не callback.
"""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class DraftAction(CallbackData, prefix="d"):
    action: str   # publish / reject / rewrite / again
    draft_id: int


def draft_keyboard(draft_id: int, permalink: str | None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Опубликовать", callback_data=DraftAction(action="publish", draft_id=draft_id))
    b.button(text="✏️ Переписать", callback_data=DraftAction(action="rewrite", draft_id=draft_id))
    b.button(text="🚫 Отклонить", callback_data=DraftAction(action="reject", draft_id=draft_id))
    if permalink and permalink.startswith("http"):
        b.button(text="🔗 Оригинал", url=permalink)
    b.adjust(2, 2)
    return b.as_markup()


def rewrite_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔁 Просто заново", callback_data=DraftAction(action="again", draft_id=draft_id))
    return b.as_markup()
