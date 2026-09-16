"""
Тексты бота и формат поста в канале — в одном месте, чтобы правки не пришлось искать по коду.

Разметка — HTML: всё, что пришло из источника или от модели, обязательно экранируется,
иначе первая же угловая скобка в тексте новости положит отправку.
"""
from __future__ import annotations

from html import escape

from ..config import TZ
from ..db import Draft, Group, StoredPost

# Лимиты Telegram
MAX_TEXT = 4096
MAX_CAPTION = 1024   # пригодится на этапе 5: у media group подпись короче

SOURCE_MARK = "📍"
LINK_MARK = "🔗"
# Переводы помечаем кодом языка, а не флагом: флаг обозначает страну, а не язык,
# и в этом канале такой подтекст ни к чему.
LANG_MARKS = (("ru", "RU"), ("ua", "UA"))


def channel_post(draft: Draft, post: StoredPost, group: Group | None, *, with_spoiler: bool) -> str:
    """Формат из ТЗ: пересказ, источник, ссылка на оригинал. Указание источника не отключается.

    with_spoiler — к каналу не привязана группа обсуждений, поэтому перевод кладём
    под спойлер прямо в пост: иначе он потеряется совсем.
    """
    parts = [escape(draft.summary).strip()]
    source = escape(group.title if group else (post.author or "источник"))
    parts.append(f"{SOURCE_MARK} {source}\n{LINK_MARK} {escape(post.permalink)}")
    if with_spoiler and (body := translations(draft)):
        parts.append(f"<tg-spoiler>{body}</tg-spoiler>")
    return _fit("\n\n".join(parts), MAX_TEXT)


def translations(draft: Draft) -> str:
    """Переводы одним блоком: «RU: …» и «UA: …». Пусто, если переводов нет."""
    return "\n\n".join(f"{mark}: {escape(text)}"
                       for code, mark in LANG_MARKS
                       if (text := getattr(draft, f"summary_{code}", None)))


def translation_comment(draft: Draft) -> str:
    return _fit(translations(draft), MAX_TEXT)


def draft_card(draft: Draft, post: StoredPost, group: Group | None, *, media_ready: int = 0) -> str:
    when = fmt_when(post.created_at or post.seen_at)
    source = escape(group.title if group else "источник")
    lines = [f"📨 <b>Черновик #{draft.id}</b> · {source} · {when}",
             "", escape(draft.summary)]
    if body := translations(draft):
        lines += ["", body]
    if draft.facts:
        lines += ["", "<b>Сверь с оригиналом:</b>"] + [f"• {escape(f)}" for f in draft.facts]
    if post.media:
        kinds = ", ".join(sorted({m["kind"] for m in post.media}))
        note = (f"пойдёт в пост: {media_ready} из {len(post.media)}" if media_ready
                else "не переносится — уйдёт только текст со ссылкой")
        lines += ["", f"<i>Медиа ({kinds}): {note}</i>"]
    lines += ["", f"<i>Модель: {escape(draft.model or '?')}</i>"]
    return _fit("\n".join(lines), MAX_TEXT)


def published_note(draft: Draft, link: str | None, *, commented: bool) -> str:
    tail = "перевод ушёл первым комментарием" if commented else "перевод под спойлером в посте"
    return f"✅ Черновик #{draft.id} опубликован ({tail})." + (f"\n{link}" if link else "")


def fmt_when(ts: float | None) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M") if ts else "—"


def _fit(text: str, limit: int) -> str:
    """Обрезаем по границе строки и честно помечаем обрез — молча терять хвост нельзя."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    return cut[: cut.rfind("\n") if "\n" in cut[limit // 2:] else len(cut)].rstrip() + "…"
