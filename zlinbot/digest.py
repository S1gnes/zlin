"""
Ежедневная сводка.

Смысл не в цифрах, а в самом факте письма: если сводка не пришла в обычное время,
значит, бот не работает — машина спит, упал процесс, кончился интернет. Молчание канала
само по себе ни о чём не говорит (может, просто новостей нет), а молчание сводки говорит.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from .collector import KIND_TEXT, STATUS_TEXT
from .config import TZ
from .db import Database

DIGEST_HOUR = 9          # по пражскому времени
DIGEST_MINUTE = 0
PERIOD = 24 * 3600

EVENTS = (("collected", "собрано"), ("duplicate", "дублей"), ("filtered", "отсеяно стоп-словами"),
          ("skipped", "Gemini: не для канала"), ("drafted", "черновиков"), ("published", "опубликовано"),
          ("rejected", "отклонено"), ("failed", "сломалось"))


def seconds_until_digest(now: float | None = None, *, hour: int = DIGEST_HOUR,
                         minute: int = DIGEST_MINUTE) -> float:
    """Сколько ждать до ближайших HH:MM по Праге."""
    current = datetime.fromtimestamp(now or time.time(), TZ)
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return (target - current).total_seconds()


async def build(db: Database, *, now: float | None = None, period: float = PERIOD) -> str:
    now = now or time.time()
    counts = await db.event_counts(now - period)
    lines = [f"📋 <b>Сводка за сутки</b> ({datetime.fromtimestamp(now, TZ):%d.%m %H:%M})", ""]

    body = [f"  {label}: {counts[event]}" for event, label in EVENTS if counts.get(event)]
    lines += body or ["  за сутки ничего не происходило — проверь источники"]

    queue = len(await db.drafts(status="pending", limit=999))
    statuses = await db.post_status_counts()
    lines += ["", f"Ждут решения: {queue} черновиков, не разобрано записей: {statuses.get('new', 0)}"]

    sources = await db.list_groups()
    if sources:
        lines += ["", "<b>Источники</b>"]
    for g in sources:
        mark = {"active": "🟢", "paused": "⏸", "unavailable": "🔴"}.get(g.status, "•")
        last = STATUS_TEXT.get(g.last_status or "", g.last_status or "—")
        silent = _silence(g.last_ok_at, now)
        lines.append(f"  {mark} {g.title} ({KIND_TEXT.get(g.kind, g.kind)}): {last}{silent}")
    return "\n".join(lines)


def _silence(last_ok_at: int | None, now: float) -> str:
    if last_ok_at is None:
        return ", ни разу не читался"
    hours = (now - last_ok_at) / 3600
    return f", молчит {hours / 24:.0f} сут." if hours >= 24 else ""
