"""
Админка: источники, стоп-слова, статистика, настройки.

Всё управление — из бота, как требует ТЗ: после первого запуска конфиги руками не правятся.
Поэтому модель, критерии отбора и режим медиа живут в таблице settings, а не в .env.

Удаление источника спрашивает подтверждение: это единственное действие здесь, которое
нельзя отменить одной кнопкой.
"""
from __future__ import annotations

import logging
import time
from datetime import date

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import filters as flt
from ..collector import KIND_TEXT, STATUS_TEXT, Collector
from ..db import Database, Group, day_bounds
from ..gemini import DEFAULT_CRITERIA, Gemini, GeminiError
from ..pipeline import current_model
from .publisher import MEDIA_MODE_KEY, NO_PREVIEW
from .texts import fmt_when

log = logging.getLogger(__name__)

EVENT_TEXT = {
    "collected": "собрано записей", "duplicate": "дублей", "old": "всплыло старых",
    "filtered": "отсеяно стоп-словами", "skipped": "Gemini: не для канала", "failed": "сломалось",
    "drafted": "черновиков", "published": "опубликовано", "rejected": "отклонено",
}
STATS_ORDER = ("collected", "duplicate", "old", "filtered", "skipped", "drafted",
               "published", "rejected", "failed")
MODELS_SHOWN = 12


class SourceAction(CallbackData, prefix="s"):
    action: str        # pause / resume / check / delete / delete_yes / delete_no
    group_id: int


class FilterAction(CallbackData, prefix="f"):
    action: str        # add / rm
    filter_id: int = 0


class SettingAction(CallbackData, prefix="set"):
    action: str        # media / model / models / criteria
    value: str = ""


class Adding(StatesGroup):
    waiting_url = State()


class AddingFilter(StatesGroup):
    waiting_word = State()


class EditingCriteria(StatesGroup):
    waiting_text = State()


# ---------------------------------------------------------------------------
# Источники
# ---------------------------------------------------------------------------

def source_line(g: Group) -> str:
    mark = {"active": "🟢", "paused": "⏸", "unavailable": "🔴"}.get(g.status, "•")
    head = f"{mark} <b>#{g.id} {g.title}</b> · {KIND_TEXT.get(g.kind, g.kind)}"
    last = STATUS_TEXT.get(g.last_status or "", g.last_status or "—")
    body = f"{g.url}\nпроверен {fmt_when(g.last_checked_at)}: {last}"
    if g.fail_streak:
        body += f"\nпровалов подряд: {g.fail_streak}"
    if g.status == "unavailable":
        body += "\nперепроверю сам раз в сутки"
    return f"{head}\n{body}"


def source_keyboard(g: Group):
    b = InlineKeyboardBuilder()
    if g.status == "paused":
        b.button(text="▶️ Включить", callback_data=SourceAction(action="resume", group_id=g.id))
    else:
        b.button(text="⏸ Пауза", callback_data=SourceAction(action="pause", group_id=g.id))
    b.button(text="🔄 Проверить сейчас", callback_data=SourceAction(action="check", group_id=g.id))
    b.button(text="🗑 Удалить", callback_data=SourceAction(action="delete", group_id=g.id))
    b.adjust(2, 1)
    return b.as_markup()


async def cmd_groups(message: Message, db: Database) -> None:
    groups = await db.list_groups()
    if not groups:
        await message.answer("Источников нет. Добавить: /add")
        return
    await message.answer(f"Источников: {len(groups)}.")
    for g in groups:
        await message.answer(source_line(g), reply_markup=source_keyboard(g), link_preview_options=NO_PREVIEW)


async def cmd_add(message: Message, state: FSMContext) -> None:
    await state.set_state(Adding.waiting_url)
    await message.answer("Пришли ссылку: группа <code>facebook.com/groups/…</code> или адрес RSS-ленты.\n"
                         "Проверю, читается ли она, и только потом добавлю. Отмена — /cancel")


async def on_add_url(message: Message, state: FSMContext, bot: Bot, collector: Collector) -> None:
    await state.clear()
    await bot.send_chat_action(message.chat.id, "typing")
    result = await collector.add_source(message.text or "")
    if not result.ok or result.group is None:
        await message.answer(f"Не добавил. {result.reason}")
        return
    await message.answer(
        f"✅ Добавил #{result.group.id} «{result.group.title}» ({KIND_TEXT.get(result.group.kind)}).\n"
        f"Видимые сейчас записи ({result.seen}) помечены как уже увиденные — в канал пойдёт только новое.",
        reply_markup=source_keyboard(result.group), link_preview_options=NO_PREVIEW)


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменил.")


async def on_source_action(query: CallbackQuery, callback_data: SourceAction, bot: Bot,
                           db: Database, collector: Collector) -> None:
    group = await db.get_group(callback_data.group_id)
    if group is None:
        await query.answer("Источник не найден", show_alert=True)
        return
    action = callback_data.action

    if action in ("pause", "resume"):
        status = "paused" if action == "pause" else "active"
        await db.update_group(group.id, status=status, fail_streak=0)
        await query.answer("На паузе" if action == "pause" else "Включил")
        await _refresh(query, await db.get_group(group.id))
    elif action == "check":
        await query.answer("Проверяю…")
        await bot.send_chat_action(query.message.chat.id, "typing")
        outcome = await collector.check_group(group.id)
        counts = dict(outcome.counts) if outcome else {}
        tail = (f"новых {counts.get('new', 0)}, дублей {counts.get('duplicate', 0)}, "
                f"уже известных {counts.get('known', 0)}") if outcome and outcome.result.ok \
            else STATUS_TEXT.get(outcome.result.status if outcome else "error", "не вышло")
        await query.message.answer(f"#{group.id} «{group.title}»: {tail}")
        await _refresh(query, await db.get_group(group.id))
    elif action == "delete":
        b = InlineKeyboardBuilder()
        b.button(text="🗑 Да, удалить", callback_data=SourceAction(action="delete_yes", group_id=group.id))
        b.button(text="Оставить", callback_data=SourceAction(action="delete_no", group_id=group.id))
        await query.answer()
        await query.message.answer(f"Удалить «{group.title}»? Записи и их ID останутся в базе — "
                                   f"они защищают от повторов, если добавишь источник снова.",
                                   reply_markup=b.as_markup())
    elif action == "delete_yes":
        await db.delete_group(group.id)
        await query.answer("Удалил")
        await query.message.edit_text(f"🗑 «{group.title}» удалён.")
    elif action == "delete_no":
        await query.answer("Оставил")
        await query.message.edit_text(f"Оставил «{group.title}».")


async def _refresh(query: CallbackQuery, group: Group | None) -> None:
    if group is None:
        return
    try:
        await query.message.edit_text(source_line(group), reply_markup=source_keyboard(group),
                                      link_preview_options=NO_PREVIEW)
    except Exception:  # noqa: BLE001 — текст мог не измениться, это не ошибка
        log.debug("карточка источника не обновилась", exc_info=True)


# ---------------------------------------------------------------------------
# Стоп-слова
# ---------------------------------------------------------------------------

async def cmd_filters(message: Message, db: Database) -> None:
    text, markup = await _filters_view(db)
    await message.answer(text, reply_markup=markup)


async def _filters_view(db: Database):
    rows = await db.list_filters()
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить", callback_data=FilterAction(action="add"))
    lines = ["<b>Стоп-слова</b> — отсев до обращения к Gemini.",
             "Сравнение без учёта регистра и диакритики, по подстроке: «prodam» ловит и «Prodám», и «prodáme».", ""]
    if rows:
        lines += [f"• {word}" for _, word, _ in rows]
        for fid, word, _ in rows:
            b.button(text=f"🗑 {word[:20]}", callback_data=FilterAction(action="rm", filter_id=fid))
    else:
        lines.append("Пока пусто.")
    b.adjust(1, *([2] * ((len(rows) + 1) // 2)))
    return "\n".join(lines), b.as_markup()


async def on_filter_action(query: CallbackQuery, callback_data: FilterAction, state: FSMContext,
                           db: Database) -> None:
    if callback_data.action == "add":
        await state.set_state(AddingFilter.waiting_word)
        await query.answer()
        await query.message.answer("Пришли слово. Отмена — /cancel")
        return
    removed = await db.remove_filter(callback_data.filter_id)
    await query.answer("Удалил" if removed else "Уже нет")
    text, markup = await _filters_view(db)
    await query.message.edit_text(text, reply_markup=markup)


async def on_filter_word(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    word = (message.text or "").strip()
    if not flt.is_valid(word):
        await message.answer(f"Слишком короткое: «{word}». Минимум {flt.MIN_WORD} буквы, "
                             f"иначе будет цеплять лишнее внутри других слов.")
        return
    added = await db.add_filter(word)
    await message.answer(f"✅ Добавил «{word}»." if added else f"«{word}» уже есть.")
    text, markup = await _filters_view(db)
    await message.answer(text, reply_markup=markup)


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------

async def cmd_stats(message: Message, db: Database) -> None:
    now = time.time()
    lines = ["<b>Статистика</b>"]
    for label, since in (("За сутки", now - 86400), ("За неделю", now - 7 * 86400)):
        counts = await db.event_counts(since)
        body = [f"  {EVENT_TEXT.get(event, event)}: {counts[event]}"
                for event in STATS_ORDER if counts.get(event)]
        lines += [f"\n<b>{label}</b>"] + (body or ["  событий не было"])

    statuses = await db.post_status_counts()
    queue = len(await db.drafts(status="pending", limit=999))
    lines += ["", f"<b>Сейчас</b>\n  черновиков ждёт решения: {queue}"
                  f"\n  записей не разобрано: {statuses.get('new', 0)}"
                  f"\n  всего записей в базе: {sum(statuses.values())}"]

    coverage = await _coverage(db)
    if coverage:
        lines += ["", "<b>Покрытие Facebook</b> (поймано / вышло в группе, оценка)"] + coverage
    await message.answer("\n".join(lines))


async def _coverage(db: Database) -> list[str]:
    """Сколько записей группы мы поймали против счётчика «Dnes N» на её странице."""
    start, end = day_bounds(date.today())
    lines = []
    for g in await db.list_groups():
        if g.kind != "fb":
            continue
        got = await db.posts_created_between(g.id, start, end)
        samples = [today for ts, today in await db.activity(g.id, start) if today is not None and ts < end]
        peak = max(samples) if samples else None
        share = f"{got / peak:.0%}" if peak else "нет данных"
        lines.append(f"  {g.title}: {got} из {peak if peak is not None else '?'} → {share}")
    return lines


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

async def cmd_settings(message: Message, db: Database, gemini: Gemini | None = None) -> None:
    text, markup = await _settings_view(db, getattr(gemini, "model", ""))
    await message.answer(text, reply_markup=markup)


async def _settings_view(db: Database, cfg_model: str):
    media = await db.get_setting(MEDIA_MODE_KEY, "copy")
    model = await current_model(db, cfg_model)
    criteria = await db.get_setting("relevance", DEFAULT_CRITERIA)
    b = InlineKeyboardBuilder()
    b.button(text="🖼 Только ссылка" if media == "copy" else "🖼 Переносить медиа",
             callback_data=SettingAction(action="media"))
    b.button(text="🤖 Сменить модель", callback_data=SettingAction(action="models"))
    b.button(text="🎯 Изменить критерии", callback_data=SettingAction(action="criteria"))
    b.adjust(1)
    text = (f"<b>Настройки</b>\n\n"
            f"Медиа: <b>{'переносить в канал' if media == 'copy' else 'только ссылка'}</b>\n"
            f"Модель: <b>{model}</b>\n\n"
            f"<b>Критерии отбора</b>\n<i>{criteria}</i>")
    return text, b.as_markup()


async def on_setting_action(query: CallbackQuery, callback_data: SettingAction, state: FSMContext,
                            db: Database, gemini: Gemini | None) -> None:
    action, value = callback_data.action, callback_data.value
    if action == "media":
        current = await db.get_setting(MEDIA_MODE_KEY, "copy")
        await db.set_setting(MEDIA_MODE_KEY, "link" if current == "copy" else "copy")
        await query.answer("Переключил")
        text, markup = await _settings_view(db, getattr(gemini, "model", ""))
        await query.message.edit_text(text, reply_markup=markup)
    elif action == "criteria":
        await state.set_state(EditingCriteria.waiting_text)
        await query.answer()
        await query.message.answer("Пришли новые критерии одним сообщением: что публиковать, "
                                   "а что отсеивать. Отмена — /cancel")
    elif action == "models":
        await query.answer("Спрашиваю у API…")
        await _show_models(query.message, db, gemini)
    elif action == "model" and value:
        await db.set_setting("gemini_model", value)
        if gemini is not None:
            gemini.model = value            # и текущий разбор идёт уже новой моделью
        await query.answer(f"Теперь {value}")
        text, markup = await _settings_view(db, value)
        await query.message.edit_text(text, reply_markup=markup)


async def cmd_models(message: Message, db: Database, gemini: Gemini | None) -> None:
    await _show_models(message, db, gemini)


async def _show_models(message: Message, db: Database, gemini: Gemini | None) -> None:
    if gemini is None:
        await message.answer("Нет ключа Gemini.")
        return
    try:
        names = await gemini.list_models()
    except GeminiError as e:
        await message.answer(f"API не ответил: {e}")
        return
    current = await current_model(db, gemini.model)
    short = [n for n in names if "flash" in n and "image" not in n and "tts" not in n][:MODELS_SHOWN]
    b = InlineKeyboardBuilder()
    for name in short:
        b.button(text=("• " if name == current else "") + name,
                 callback_data=SettingAction(action="model", value=name))
    b.adjust(1)
    note = "" if current in names else "\n⚠️ Текущей модели нет в списке API."
    await message.answer(f"Сейчас: <b>{current}</b>{note}\n\nМоделей у API: {len(names)}. "
                         f"Показываю flash-линейку — она дешевле и быстрее для пересказов.",
                         reply_markup=b.as_markup())


async def on_criteria_text(message: Message, state: FSMContext, db: Database) -> None:
    await state.clear()
    text = (message.text or "").strip()
    if len(text) < 20:
        await message.answer("Слишком коротко — модель не поймёт, что отбирать. Попробуй ещё раз: /settings")
        return
    await db.set_setting("relevance", text)
    await message.answer("✅ Критерии обновлены. Применятся со следующего разбора.")
    text, markup = await _settings_view(db, "")
    await message.answer(text, reply_markup=markup)


# ---------------------------------------------------------------------------

def make_router() -> Router:
    router = Router(name="admin")
    router.message.register(cmd_cancel, Command("cancel"))
    router.message.register(cmd_groups, Command("groups"))
    router.message.register(cmd_add, Command("add"))
    router.message.register(cmd_filters, Command("filters"))
    router.message.register(cmd_stats, Command("stats"))
    router.message.register(cmd_settings, Command("settings"))
    router.message.register(cmd_models, Command("models"))
    router.message.register(on_add_url, Adding.waiting_url, F.text)
    router.message.register(on_filter_word, AddingFilter.waiting_word, F.text)
    router.message.register(on_criteria_text, EditingCriteria.waiting_text, F.text)
    router.callback_query.register(on_source_action, SourceAction.filter())
    router.callback_query.register(on_filter_action, FilterAction.filter())
    router.callback_query.register(on_setting_action, SettingAction.filter())
    return router
