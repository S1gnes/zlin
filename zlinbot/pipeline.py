"""
Обработка накопленных записей: стоп-слова -> Gemini -> черновик.

Порядок важен: сначала бесплатный отсев по стоп-словам, и только потом Gemini —
у бесплатного тарифа считается каждый вызов.

Отдельно разведены три вида сбоя модели, потому что реагировать на них надо по-разному:
- суточная квота: ретраи бессмысленны, встаём до сброса и говорим в личку;
- 400 (модель снята, ключ не принят): круг останавливаем, само не пройдёт;
- фильтр безопасности или мусор вместо JSON: это про одну запись, помечаем её и идём дальше.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import filters
from .db import Database, StoredPost
from .gemini import (DEFAULT_CRITERIA, DEFAULT_MODEL, GeminiBadRequest, GeminiBlocked, GeminiError,
                     GeminiQuotaExhausted, Verdict)
from .textnorm import significant

log = logging.getLogger(__name__)

MAX_PER_ROUND = 8   # из ТЗ: не более 8 записей за круг
MIN_TEXT = 20       # значащих символов; короче — пересказывать нечего


@dataclass
class Decision:
    post: StoredPost
    status: str                     # filtered / skipped / pending / failed
    note: str | None = None
    draft_id: int | None = None
    verdict: Verdict | None = None


@dataclass
class ProcessReport:
    decisions: list[Decision] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    paused_until: float | None = None   # квота Gemini кончилась — до этого времени не дёргаем

    def count(self, status: str) -> int:
        return sum(1 for d in self.decisions if d.status == status)


class Summarizer:
    """Минимум, который нужен конвейеру от Gemini (в тестах подменяется)."""

    model: str

    async def summarize(self, text: str, *, source: str = "", criteria: str = DEFAULT_CRITERIA,
                        extra: str = "") -> Verdict: ...


class Processor:
    def __init__(self, db: Database, gemini: Summarizer | None, *,
                 clock: Callable[[], float] = time.time, limit: int = MAX_PER_ROUND) -> None:
        self.db = db
        self.gemini = gemini
        self.clock = clock
        self.limit = limit
        self._paused_until: float | None = None  # пауза до сброса суточной квоты

    async def run(self) -> ProcessReport:
        report = ProcessReport(paused_until=self._paused_until)
        now = self.clock()
        if self._paused_until and now < self._paused_until:
            return report
        if self._paused_until:
            report.alerts.append("✅ Суточная квота Gemini обновилась, продолжаю разбор.")
            self._paused_until = report.paused_until = None
        if self.gemini is None:
            return report

        stop_words = [norm for _, _, norm in await self.db.list_filters()]
        criteria = await self.db.get_setting("relevance", DEFAULT_CRITERIA)
        posts = await self.db.posts(status="new", limit=self.limit, oldest_first=True)
        for post in posts:  # с самых старых: хвост очереди не должен застаиваться
            decision = await self._process(post, stop_words, criteria, report)
            if decision is None:
                break
            report.decisions.append(decision)
        return report

    async def _process(self, post: StoredPost, stop_words: list[str], criteria: str,
                       report: ProcessReport) -> Decision | None:
        """None — круг надо прервать (квота или неверные настройки)."""
        text = "\n".join(part for part in (post.text, post.shared_text) if part)
        if word := filters.match(text, stop_words):
            return await self._decide(post, "filtered", f"стоп-слово «{word}»")
        if len(significant(text)) < MIN_TEXT:
            return await self._decide(post, "skipped", "нечего пересказывать: пустой или почти пустой текст")

        source = await self._source_name(post)
        try:
            verdict = await self.gemini.summarize(text, source=source, criteria=criteria)
        except GeminiQuotaExhausted as e:
            self._paused_until = report.paused_until = e.reset_at
            report.alerts.append(
                f"⏸ {e} Разбор встал до сброса квоты; собирать записи продолжаю, они подождут в очереди.")
            return None
        except GeminiBadRequest as e:
            report.alerts.append(f"🚨 Gemini не принимает запрос: {e}\nПроверь ключ и название модели "
                                 f"(актуальный список — команда models). Разбор остановлен.")
            return None
        except GeminiBlocked as e:
            return await self._decide(post, "failed", f"модель не дала ответ: {e}")
        except GeminiError as e:
            return await self._decide(post, "failed", f"ошибка разбора ответа: {e}")

        if verdict.skip or not verdict.post:
            note = "Gemini: не для канала" if verdict.skip else "Gemini: пустой пересказ"
            return await self._decide(post, "skipped", note, verdict=verdict)

        draft_id = await self.db.add_draft(post_id=post.post_id, summary=verdict.post,
                                           summary_ru=verdict.post_ru or None, facts=verdict.facts,
                                           model=verdict.model, now=self.clock())
        decision = await self._decide(post, "pending", None, verdict=verdict)
        decision.draft_id = draft_id
        await self.db.log("drafted", post_id=post.post_id, now=self.clock())
        return decision

    async def _decide(self, post: StoredPost, status: str, note: str | None, *,
                      verdict: Verdict | None = None) -> Decision:
        await self.db.set_post_status(post.post_id, status, note=note, expect=["new"], now=self.clock())
        if status in ("filtered", "skipped", "failed"):
            await self.db.log(status, group_id=post.group_id, post_id=post.post_id, now=self.clock())
        return Decision(post, status, note, verdict=verdict)

    async def _source_name(self, post: StoredPost) -> str:
        if post.group_id is None:
            return ""
        group = await self.db.get_group(post.group_id)
        return group.title if group else ""


async def rewrite_draft(db: Database, gemini: Summarizer, draft_id: int, *, instruction: str = "",
                        clock: Callable[[], float] = time.time) -> int | None:
    """Переписать черновик: новая версия — новая строка, старая помечается superseded.
    None — модель на этот раз решила, что запись каналу не подходит."""
    draft = await db.get_draft(draft_id)
    post = await db.get_post(draft.post_id) if draft else None
    if draft is None or post is None or draft.status != "pending":
        return None
    group = await db.get_group(post.group_id) if post.group_id else None
    text = "\n".join(part for part in (post.text, post.shared_text) if part)
    criteria = await db.get_setting("relevance", DEFAULT_CRITERIA)
    verdict = await gemini.summarize(text, source=group.title if group else "", criteria=criteria,
                                     extra=instruction)
    if verdict.skip or not verdict.post:
        return None
    new_id = await db.add_draft(post_id=post.post_id, summary=verdict.post,
                                summary_ru=verdict.post_ru or None, facts=verdict.facts,
                                model=verdict.model, now=clock())
    await db.set_draft_status(draft_id, "superseded", expect=["pending"], now=clock())
    return new_id


async def current_model(db: Database, default: str = DEFAULT_MODEL) -> str:
    """Модель из настроек (правится из бота), иначе значение по умолчанию из .env."""
    return await db.get_setting("gemini_model", default)
